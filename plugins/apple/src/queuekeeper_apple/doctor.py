"""What the Apple extension adds to `doctor`: the simulator runtimes it evens out between the Macs, the stale pool
simulators it shuts down, and Xcode's default simulators, which it deletes and warns of."""
import contextlib
import json
import subprocess
import sys

from queuekeeper import disk, extensions, remote
from . import disk as apple_disk
from .simulators import pool as simulator_pool


def fix_hosts(root, hosts):
    """What `doctor` fixes of remote.compare_hosts' report itself (the owner's decision, 2026-11-03): runtimes that
    differ, which it evens out (remote.match_runtimes) and then compares again; stale pool simulators on the
    Macs, which it shuts down (simulators.pool.reap_reports) and lists in reaped; and Xcode's default simulators
    on this Mac, which it deletes as `disk --delete` does, checking each again first, and lists in
    deletedDefaults. Its progress goes to standard error, so that standard output stays the report."""
    if hosts.pop("runtimesDiffer", False):
        with contextlib.redirect_stdout(sys.stderr): remote.match_runtimes(hosts["host"], check=False)
        hosts = remote.compare_hosts(hosts["host"])
        hosts.pop("runtimesDiffer", None)
    # Another Mac only when it answered the comparison: one away is no error, and is not waited for.
    # A diagnostic stops no leftover process: it lists those reap would stop (vocaro-engineering#353).
    reaped = simulator_pool.reap_reports(None, root, dry_run=False, here="remote" not in hosts, stop_leftovers=False)
    # With each held entry whose process is gone that reap could not release, and why, so that no leftover
    # waits unseen behind an empty leftovers (vocaro-engineering#156).
    hosts["reaped"] = [line for where, report in reaped
                       for line in remote.reap_lines(where, report, listing=False, held=True)]
    hosts["deletedDefaults"] = delete_defaults()
    return hosts


def delete_defaults(run=subprocess.run):
    """Delete Xcode's default simulators on this Mac that `qk disk` finds deletable, each checked again
    first and recorded in its ledger, and return what was said of each."""
    def defaults():
        found = apple_disk.Host.current(0, None, run)
        return found, [item for item in disk.inventory(found, disk.Sizes(run, False), ["simulator"])
                       if item.status == disk.CANDIDATE and item.reason.startswith("Xcode's default device")]
    try:
        host, items = defaults()
        if not items: return []
        said = []
        disk.delete(items, lambda category: defaults()[1], disk.ledger_path(host.home), out=said.append, run=run)
        return said
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as error:
        return [f"could not delete Xcode's default simulators: {type(error).__name__}: {error}"]


def default_simulators(config):
    """A warning when Xcode's default simulators exist on this Mac and the project uses simulators.

    A script or Xcode run that picks a device by name, or `booted`, finds one of them instead of a
    pool member and fills it with the project's apps (vocaro-engineering#91). Xcode makes them again
    when it installs a runtime. `qk doctor` deletes each one first (fix_hosts), so this names only
    those still there: booted, in use or named by a process."""
    if not config.get("simulatorPool"):
        return []
    try:
        result = simulator_pool.simctl("list", "devices", "-j", timeout=120)
        found = simulator_pool.listed_defaults(json.loads(result.stdout)) if result.returncode == 0 else []
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
        return []
    if not found:
        return []
    shown = ", ".join(found[:5]) + (f" and {len(found) - 5} more" if len(found) > 5 else "")
    return [f"{len(found)} of Xcode's default simulators exist on this Mac: {shown}. A script or Xcode run that "
            "picks a device by name or `booted` installs on one instead of a pool member; `qk doctor` deletes each "
            "one that is shut down and unused, and `qk disk` says why it kept these (doc/guides/simulator-pool.md, "
            "Reserved simulator pool)"]


TEST_DIMENSIONS = "apple-engineering's guides/app-testing.md, Test dimensions and the snapshot layer"


def idioms(command):
    """The idioms of the simulator types a command names with --device: iPhone, iPad or both. A device pass's
    --device names an entry of the device record, a physical device, so it has none here."""
    if "device-pass" in command: return set()
    named = [word.split("=", 1)[1] if word.startswith("--device=") else command[index + 1]
             for index, word in enumerate(command)
             if word.startswith("--device=") or (word == "--device" and index + 1 < len(command))]
    return {idiom for name in named for idiom in ("iPhone", "iPad") if name.startswith(idiom)}


def idiom_warnings(config):
    """A warning of the lanes whose one command names an iPhone and an iPad simulator type, so that it runs one
    selection on both: a test whose result depends on no idiom then runs twice (vocaro/queuekeeper#105). Only the
    lane's own words are read. A project's script that picks its devices itself is not seen, and one lane for each
    idiom is the project's split, not this. A lane of release.lanes, or of a group there, runs every class on
    every simulator as the control for platform divergence, and a lane whose "idioms" says why its whole
    selection depends on the idiom has said so."""
    lanes = config.get("lanes", {})
    release = {member for name in config.get("release", {}).get("lanes", [])
               for member in [name, *lanes.get(name, {}).get("parallel", [])]}
    found = sorted(name for name, lane in lanes.items() if "command" in lane and name not in release
                   and "idioms" not in lane and len(idioms(lane["command"])) > 1)
    if not found: return []
    return [f"{len(found)} lane(s) run one selection on an iPhone and an iPad simulator, so each test whose result "
            f"does not depend on the idiom runs twice: {', '.join(found)}; run those tests once, on the Mac where "
            "the code allows it, and on each simulator only the classes that depend on its idiom, or say in the "
            "lane's \"idioms\" why its whole selection depends on the idiom; a lane in release.lanes, the control "
            f"for platform divergence, is not counted ({TEST_DIMENSIONS})"]


def simulator_warnings(root, config):
    return default_simulators(config) + idiom_warnings(config)


def required_commands(config):
    """Xcode's tools, for a project that uses simulators or keeps an app."""
    return {"xcrun", "xcodebuild"} if config.get("simulatorPool") or config.get("app") else set()


def declared_devices(root, config):
    """(errors, warnings) of the devices the owner declared, as each Mac reads them now (reach.findings): a device
    declared always reachable that no Mac has reached for the settle time is an error."""
    from queuekeeper import offload_host
    from . import reach
    try: return reach.findings(offload_host.record_path())
    except (OSError, ValueError, RuntimeError) as error:
        return [], [f"the declared devices could not be read: {error} (doc/guides/devices.md, Declared devices)"]


def register():
    """Add these checks and fixes to `doctor`."""
    if declared_devices not in extensions.DOCTOR_CHECKS: extensions.DOCTOR_CHECKS.append(declared_devices)
    if required_commands not in extensions.REQUIRED_COMMANDS: extensions.REQUIRED_COMMANDS.append(required_commands)
    if fix_hosts not in extensions.HOST_FIXES: extensions.HOST_FIXES.append(fix_hosts)
    if simulator_warnings not in extensions.DOCTOR: extensions.DOCTOR.append(simulator_warnings)
