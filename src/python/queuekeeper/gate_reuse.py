"""Evidence a queue may use in place of running an opted-in gate lane again."""

import contextlib
import hashlib
import json
import platform
import subprocess

from . import count_history, offload_host, plugins, remote, shims
from .config import load, source_identity


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


HOST_REQUIREMENTS = ("timing", "labels", "foreground", "unlockedScreen", "devices")


def needs_live_host(definition):
    return any(definition.get(key) for key in HOST_REQUIREMENTS) or plugins.reads_screen(definition)


def inputs(root, config, lane, environment):
    """Read a lane's declared execution inputs. Store the digest, never the probe's output."""
    probe = config.get("queue", {}).get("reuseGate", {}).get(lane)
    definition = config.get("lanes", {}).get(lane, {})
    if not probe or not definition or needs_live_host(definition):
        return None
    # A whole group is eligible only if each member can use the same input declaration.
    members = {name: config["lanes"][name] for name in definition.get("parallel", ())}
    if any(needs_live_host(member) for member in members.values()):
        return None
    try:
        measured = subprocess.run(probe, cwd=root, env=shims.environment(environment, root),
                                  stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  timeout=10, check=True).stdout
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if not measured.strip():
        return None
    host = [offload_host.mac_name(), platform.machine(), platform.mac_ver()[0], platform.version()]
    return {"version": 1, "digest": digest([probe, definition, members, host, hashlib.sha256(measured).hexdigest()])}


def candidates(root, config, tip):
    """Identify existing local passes without asking the submitter for lane names."""
    found = {}
    for lane in config.get("queue", {}).get("reuseGate", {}):
        with contextlib.suppress(OSError, RuntimeError, ValueError, subprocess.SubprocessError):
            receipt = remote.passed(root, lane, tip)
            if receipt and receipt.get("ranOn") == remote.HERE and count_history.lane_receipt(receipt).get("gateInputs"):
                found[lane] = {"commit": tip, "receipt": digest(receipt)}
    return found


def valid_counts(root, lane, definition, receipt, head, group=None):
    tests = receipt.get("tests", {})
    if not isinstance(tests, dict):
        return False
    count = count_history.count(tests.get("executed"))
    minimum = definition.get("minimumTests")
    if definition.get("exactTests"):
        return count == minimum
    if (minimum or count is not None) and (count or 0) < (minimum or 1):
        return False
    return count_history.dropped(root, remote.store(root), lane,
                                 {**receipt, "project": {"revision": head}}, remote.HERE, group=group) is None


def reusable(root, config, lane, entries, head, toolkit, environment):
    """Return the source of a verified pass, or None so the worker runs the lane."""
    sources = [entry.get("gateCandidates", {}).get(lane) for entry in entries]
    if not any(sources):
        return None
    tree = count_history.git(root, "rev-parse", "--verify", f"{head}^{{tree}}")
    if not tree:
        return None
    with contextlib.suppress(OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        return verified(root, config, lane, filter(None, sources), head, tree, toolkit, environment)
    return None


def verified(root, config, lane, sources, head, tree, toolkit, environment):
    actual = load(root)
    if lane not in actual.get("lanes", {}):
        return None
    # The command runs from the candidate, but only main can authorize the input probe.
    actual["queue"] = {"reuseGate": config["queue"]["reuseGate"]}
    toolkit_identity = source_identity(toolkit)
    if not toolkit_identity["revision"] or toolkit_identity["workingChangesSHA256"] != remote.CLEAN:
        return None
    definition = actual["lanes"][lane]
    for source in sources:
        if not isinstance(source, dict) or not isinstance(commit := source.get("commit"), str):
            continue
        if count_history.git(root, "rev-parse", "--verify", f"{commit}^{{tree}}") != tree:
            continue
        kept = remote.passed(root, lane, commit)
        if not kept or kept.get("ranOn") != remote.HERE or digest(kept) != source.get("receipt"):
            continue
        receipt = count_history.lane_receipt(kept)
        if (receipt.get("project") != {"revision": commit, "workingChangesSHA256": remote.CLEAN}
                or receipt.get("toolkit") != toolkit_identity
                or receipt.get("mac") != offload_host.mac_name()
                or receipt.get("siblings") or receipt.get("policy") or not receipt.get("gateInputs")):
            continue
        if not valid_counts(root, lane, definition, receipt, head):
            continue
        if "parallel" in definition and not group_counts(root, lane, actual, definition["parallel"], receipt, head):
            continue
        if receipt["gateInputs"] == inputs(root, actual, lane, environment):
            # Keep the verified counts at the candidate commit too, without replacing an actual run.
            if commit != head:
                remote.keep(remote.store(root) / head / "reused", lane,
                            {**kept, "commit": head, "head": head, "reusedFrom": source})
            return source
    return None


def group_counts(root, lane, config, names, receipt, head):
    members = receipt.get("lanes")
    if receipt.get("notSelected") or not isinstance(members, dict):
        return False
    for name in names:
        member = members.get(name)
        if not isinstance(member, dict) or member.get("status") != 0 or member.get("outcome") != "passed":
            return False
        if not valid_counts(root, name, config["lanes"][name],
                            {**receipt, "tests": count_history.member_tests(member)}, head, group=lane):
            return False
    return True
