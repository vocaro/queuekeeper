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
    differ, which it evens out (remote.match_runtimes) and then compares again; stale pool simulators on both
    Macs, which it shuts down (simulators.pool.reap_reports) and lists in reaped; and Xcode's default simulators
    on this Mac, which it deletes as `disk --delete` does, checking each again first, and lists in
    deletedDefaults. Its progress goes to standard error, so that standard output stays the report."""
    if hosts.pop("runtimesDiffer", False):
        with contextlib.redirect_stdout(sys.stderr): remote.match_runtimes(hosts["host"], check=False)
        hosts = remote.compare_hosts()
        hosts.pop("runtimesDiffer", None)
    # The second Mac only when it answered the comparison: one away is no error, and is not waited for.
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
            "one that is shut down and unused, and `qk disk` says why it kept these (guides/simulator-pool.md, "
            "Reserved simulator pool)"]


def simulator_warnings(root, config):
    return default_simulators(config)


def required_commands(config):
    """Xcode's tools, for a project that uses simulators or keeps an app."""
    return {"xcrun", "xcodebuild"} if config.get("simulatorPool") or config.get("app") else set()


def register():
    """Add these checks and fixes to `doctor`."""
    if required_commands not in extensions.REQUIRED_COMMANDS: extensions.REQUIRED_COMMANDS.append(required_commands)
    if fix_hosts not in extensions.HOST_FIXES: extensions.HOST_FIXES.append(fix_hosts)
    if simulator_warnings not in extensions.DOCTOR: extensions.DOCTOR.append(simulator_warnings)
