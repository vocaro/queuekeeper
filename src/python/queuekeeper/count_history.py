"""A lane's executed-test count, held against the count of its last passing whole run on main, so that a gate
that silently lost a bundle, a module or a file's tests fails without a floor typed by hand.

Every judged whole run of a lane is kept by commit in the repository's Git directory (remote.keep_here, and
remote.execute for a run on another Mac), and its lane's receipt records `tests`, the counts its log reports. A whole
run is held against the newest such run that passed at a commit of main below it on the same Mac: a Mac whose tests
skip apart from another's has its own count. A Mac with no such run is held against the newest that passed on any
Mac, by the tests each selected, executed and skipped together, so that a test that skips on one Mac and runs on the
other is no fall: on 2026-11-19 a gate on a Mac with no count of its own passed a batch that deleted 15 tests
(vocaro/queuekeeper#300). It fails when it executed fewer, unless a commit after that run says `Tests-Removed: LANE`,
or `*` for every lane. Additions need nothing. A run with arguments selects part of the lane and is compared with no
whole run.

Between 2026-10-01 and 2026-11-18 the projects committed about 100 edits to their lanes' minimumTests, 30 of them
Pagepour's, the number following the tests rather than guarding them.
"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

# The trailer by which a commit says its lane's count falls on purpose: `Tests-Removed: gate` or `Tests-Removed: *`.
TRAILER = "Tests-Removed"
EVERY_LANE = "*"
# How many commits of main below a run are searched for its last passing run, newest first.
DEPTH = 200
# The branch a run's history is read on, as the merge queue lands it: origin's first, else the local one.
MAINS = ("refs/remotes/origin/main", "refs/heads/main")
# A nested group on another Mac sends its verdict back through the outer lane's results.
GROUP_RECEIPT_PREFIX = ".queuekeeper-group-"


def counts(log_counts):
    """A receipt's `tests` for a log's (ran, skipped), as lane_results.counted reads them: the tests executed, which
    leave out the skipped, and those skipped; or None when the log reports no count."""
    if log_counts is None: return None
    ran, skipped = log_counts
    return {"executed": ran - skipped, "skipped": skipped}


def lane_receipt(kept):
    """The lane's own receipt within a run kept by commit: under result.receipt, for one kept here and one from another
    Mac alike."""
    result = kept.get("result") if isinstance(kept, dict) else None
    inner = result.get("receipt") if isinstance(result, dict) else None
    return inner if isinstance(inner, dict) else {}


def passing(kept, commit, lane, here):
    """Whether a run kept by commit is a pass of the whole lane at exactly commit: run with no arguments, not on a
    snapshot or a snapshot of the toolkit, nor a group's serial reference, and, for a run on this Mac (ranOn here),
    judged passed, which a zero exit that ran too few tests is not."""
    if not isinstance(kept, dict): return False
    result = kept.get("result") if isinstance(kept.get("result"), dict) else {}
    whole = kept.get("commit") == commit and kept.get("lane") == lane and not kept.get("arguments") \
        and not kept.get("snapshot") and not kept.get("toolkitSnapshot") and not kept.get("serial")
    judged = kept.get("ranOn") != here or result.get("outcome") == "passed"
    return whole and judged and result.get("exitStatus") == 0 and not result.get("stopped")


def git(root, *arguments):
    """git's output in root, or None when it fails: a run with no history to read is held against none."""
    try:
        done = subprocess.run(["git", "-C", str(root), *arguments], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def below(root, commit):
    """The commits of main at or below commit, newest first along main's first parents: those of a branch not yet
    landed are left out, since a run there passed no merge queue's check of its count."""
    for main in MAINS:
        base = git(root, "merge-base", commit, main)
        if base: return (git(root, "rev-list", "--first-parent", f"--max-count={DEPTH}", base) or "").split()
    return []


class Baseline:
    """A passing whole run a run is held to: its commit, the tests it executed and skipped, the Mac it ran on and the
    file that keeps it. own says it ran on the Mac of the run held to it."""
    def __init__(self, commit, executed, skipped, mac, path, own=True, correction=None):
        self.commit, self.executed, self.skipped, self.mac, self.path, self.own = \
            commit, executed, skipped, mac, path, own
        self.correction = correction


def count(value):
    """value when it is a count of tests, else None."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def member_tests(member):
    """Read a member's tests in the same shape as a standalone lane's counts."""
    return {"executed": member.get("executedTests"), "skipped": member.get("skippedTests")}


def correction_path(store, group, lane, original):
    """A correction belongs to one member of the exact kept group receipt."""
    key = hashlib.sha256(group.encode() + b"\0" + lane.encode() + b"\0" + original).hexdigest()
    return Path(store) / "count-corrections" / f"{key}.json"


def corrected(store, group, lane, original, mac, tests):
    path = correction_path(store, group, lane, original)
    try:
        record = json.loads(path.read_bytes())
        replacement = record["after"]
        if record["schemaVersion"] == 1 and record["mac"] == mac and record["before"] == tests \
                and same_selection(tests, replacement):
            return replacement["executed"], replacement["skipped"], path
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    return tests["executed"], tests["skipped"], None


def same_selection(before, after):
    """An execution-count correction accounts for the difference entirely through skips."""
    old, old_skips, new, new_skips = (count(tests.get(key)) for tests in (before, after)
                                    for key in ("executed", "skipped"))
    return None not in (old, old_skips, new, new_skips) and old_skips >= 0 \
        and 0 < new < old and old + old_skips == new + new_skips


def correction_plan(root, store, group, lane, revision, mac, here, reason, member_command, clean):
    """Validate both kept runs against published main and the member's command at the recorded revision."""
    reason = reason.strip()
    if not reason or not mac.strip():
        raise ValueError("name the execution Mac and give the reviewed reason for the correction")
    if any(not name or Path(name).name != name or name in {".", ".."} for name in (group, lane)):
        raise ValueError("the group and member must be lane names")
    published = git(root, "rev-parse", "--verify", MAINS[0])
    if not published or revision not in below(root, published):
        raise ValueError("the baseline must be a full revision in the recent first-parent history of origin/main")
    members, command = member_command(revision, group, lane)
    found = baseline(root, store, lane, mac, published, here, group=group)
    if found is None or not found.own or found.commit != revision:
        raise ValueError(f"{revision} is not the current {group}/{lane} baseline on {mac}")

    original = found.path.read_bytes()
    historical = json.loads(original)
    group_receipt = lane_receipt(historical)
    source_path = Path(store) / revision / f"{lane}.json"
    try:
        source = source_path.read_bytes()
    except FileNotFoundError as error:
        raise ValueError(f"no kept standalone evidence at {source_path}") from error
    evidence = json.loads(source)
    standalone = lane_receipt(evidence)
    for kept, inner, name in ((historical, group_receipt, group), (evidence, standalone, lane)):
        if not passing(kept, revision, name, here) or kept.get("arguments") != [] \
                or kept.get("expect") is not None or kept.get("mac") != mac or inner.get("mac") != mac \
                or kept["result"].get("stage", "work") != "work" or inner.get("outcome") != "passed" \
                or inner.get("exitStatus") != 0 or (inner.get("project") or {}).get("revision") != revision \
                or any((inner.get(key) or {}).get("workingChangesSHA256") != clean for key in ("project", "toolkit")):
            raise ValueError(f"{name} must be a clean, passing whole run on {mac} at {revision}")
    if historical.get("macId") and evidence.get("macId") and historical["macId"] != evidence["macId"]:
        raise ValueError("the receipts report different execution Mac identities")
    member = group_receipt.get("lanes", {}).get(lane, {})
    if group_receipt.get("parallel") != members or group_receipt.get("notSelected") \
            or member.get("status") != 0 or member.get("outcome") != "passed":
        raise ValueError("the historical receipt must record a pass of the configured whole group and member")
    if standalone.get("arguments") != [] or standalone.get("command") != command:
        raise ValueError("the standalone receipt command must match the configured whole group member")
    before, after = member_tests(member), standalone.get("tests") or {}
    if not isinstance(after, dict) or not same_selection(before, after):
        raise ValueError("the evidence must execute fewer tests and select the same total, accounting for the difference in skips")
    path = correction_path(store, group, lane, original)
    return path, {
        "schemaVersion": 1, "recordedAt": datetime.now(timezone.utc).isoformat(), "reason": reason,
        "publishedMain": published, "revision": revision, "group": group, "member": lane, "mac": mac,
        "command": command, "before": before, "after": after,
        "baseline": {"path": str(found.path), "sha256": hashlib.sha256(original).hexdigest(), "receipt": historical},
        "evidence": {"path": str(source_path), "sha256": hashlib.sha256(source).hexdigest(), "receipt": evidence},
    }


def record_correction(path, record):
    """Publish the complete audit record atomically and refuse to replace an earlier review."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent) as staged:
        json.dump(record, staged, indent=2)
        staged.write("\n")
        staged.flush()
        try:
            os.link(staged.name, path)
        except FileExistsError as error:
            raise ValueError(f"a correction already exists at {path}") from error


def baseline(root, store, lane, mac, commit, here, group=None):
    """The newest passing whole run of the lane at a commit of main at or below commit, on the Mac named mac, that
    recorded its count, as a Baseline; else the newest on any Mac that recorded both its executed and its skipped
    count, as a Baseline that is not its own; or None. A kept run written before counts were recorded is passed
    over. A group member reads the named member of the group's kept receipt, with the same eligibility rules."""
    if not mac or not commit: return None
    other = None
    recorded_lane = group or lane
    for older in below(root, commit):
        path = Path(store) / older / f"{recorded_lane}.json"
        try:
            try:
                original = path.read_bytes()
            except FileNotFoundError:
                # A verified local pass may have satisfied the lane on this exact landing tree.
                path = path.parent / "reused" / path.name
                original = path.read_bytes()
            kept = json.loads(original)
        except (OSError, ValueError):
            continue
        if not passing(kept, older, recorded_lane, here): continue
        inner = lane_receipt(kept)
        tests = inner.get("tests") if isinstance(inner.get("tests"), dict) else {}
        if group is not None:
            members = inner.get("lanes")
            member = members.get(lane) if isinstance(members, dict) else None
            if inner.get("notSelected") or not isinstance(member, dict) or member.get("outcome") != "passed" \
                    or member.get("status") != 0:
                continue
            tests = member_tests(member)
        executed, skipped = count(tests.get("executed")), count(tests.get("skipped"))
        if executed is None: continue
        if inner.get("mac") == mac:
            if group is not None:
                executed, skipped, correction = corrected(store, group, lane, original, mac, tests)
                return Baseline(older, executed, skipped, mac, path, correction=correction)
            return Baseline(older, executed, skipped, mac, path)
        if other is None and skipped is not None:
            other = Baseline(older, executed, skipped, inner.get("mac") or "another Mac", path, own=False)
    return other


def acknowledged(root, since, commit, lane):
    """Whether a commit after since, up to commit, says `Tests-Removed:` the lane, or every lane."""
    said = git(root, "log", f"--format=%(trailers:key={TRAILER},valueonly,separator=%x2C)", f"{since}..{commit}")
    named = {name.strip() for line in (said or "").splitlines() for name in line.split(",")}
    return lane in named or EVERY_LANE in named


def dropped(root, store, lane, receipt, here, say=None, group=None):
    """Why a whole run of the lane whose own receipt is receipt failed, having executed fewer tests than its last
    passing whole run on main on the same Mac, or None when it did not, or no run on main recorded a count. A Mac with
    no such run is held to the newest on any Mac: the tests that run selected, executed and skipped together, less
    those this run skipped, are the tests it should have executed. A run whose log reports no count at all is held as
    one that executed none. say, if given, is told of a fall a commit's trailer acknowledged."""
    tests = receipt.get("tests") if isinstance(receipt.get("tests"), dict) else {}
    executed = count(tests.get("executed")) or 0
    skipped = count(tests.get("skipped")) or 0
    commit = (receipt.get("project") or {}).get("revision")
    found = baseline(root, store, lane, receipt.get("mac"), commit, here, group=group)
    if found is None: return None
    # Another Mac's tests may skip apart from this one's: what it selected is what this Mac should select.
    expected = found.executed if found.own else max(found.executed + found.skipped - skipped, 0)
    if executed >= expected: return None
    fewer = expected - executed
    words = (f"{lane} executed {executed} tests, {fewer} fewer than the {expected} its last passing whole run on "
             + (f"{found.mac} executed at {found.commit[:12]} ({found.path})" if found.own else
                f"main on any Mac selected, less the {skipped} this run skipped: {receipt.get('mac')} has no passing "
                f"whole run on main with a count, so it is held to {found.mac}'s at {found.commit[:12]}, which "
                f"executed {found.executed} and skipped {found.skipped} ({found.path})"))
    if found.correction is not None:
        words += f", as corrected in {found.correction}"
    if acknowledged(root, found.commit, commit, lane):
        if say: say(f"{words}, which a commit since says with {TRAILER}: {lane}")
        return None
    return (f"{words}. If tests were removed on purpose, add the trailer `{TRAILER}: {lane}` to a commit after "
            f"{found.commit[:12]}; otherwise find what stopped running, such as a bundle, module or file the lane no "
            f"longer selects or whose tests now skip")
