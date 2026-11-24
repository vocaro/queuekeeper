"""The Apple checks and fixes in `doctor` and `remote setup`: Xcode versions, simulator runtimes, and stale or
default simulators."""
import contextlib
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys

from queuekeeper import disk, offload_host, plugins, remote
from queuekeeper.config import qk_said
from . import disk as apple_disk
from .simulators import pool as simulator_pool, runtimes as simulator_runtimes


def remote_setup_xcode():
    """Check the Xcode at this Mac's developer directory in the receiving Mac's login shell."""
    developer = remote.developer_directory()
    here = " ".join((offload_host.xcode(developer) or "").split())
    selected = f"DEVELOPER_DIR={shlex.quote(developer)} " if developer else ""
    prefix = "xcode   "
    probe = f'echo "{prefix}$({selected}xcrun xcodebuild -version 2>/dev/null | tr \'\\n\' \' \')"'

    def report(value):
        there = " ".join(value.split())
        if here and there != here:
            return f"MISSING {here} at {developer}, as on this Mac (found {there or 'none'})", False
        return f"ok      {there or 'no Xcode'}" + (f" at {developer}" if developer else ""), True

    return prefix, probe, report


def compare_toolchains(report, developer):
    """Report Xcode and simulator runtime differences using the standalone host inventories."""
    host, here, there = report["host"], report["local"], report.get("remote")
    if there is None:
        report["unchecked"].append(f"{host}'s default Xcode, its Xcode at this Mac's developer directory and its "
                                   "simulator runtimes, which it did not report")
        return
    version = lambda text: ", ".join((text or "no Xcode").splitlines())
    default = lambda side: (side.get("defaultDeveloperDir"), side.get("defaultXcode"))
    # When each Mac's default is this Mac's developer directory, the next check covers its version.
    shared = here.get("defaultDeveloperDir") == there.get("defaultDeveloperDir") == developer
    if default(here) != default(there) and not shared:
        report["errors"].append(
            f"the default Xcodes differ: `xcode-select -p` is {here.get('defaultDeveloperDir')} "
            f"({version(here.get('defaultXcode'))}) on this Mac and {there.get('defaultDeveloperDir')} "
            f"({version(there.get('defaultXcode'))}) on {host}, so whatever reads {host}'s default, such as "
            f"`ssh {host} xcodebuild -version` or a script without DEVELOPER_DIR, sees another Xcode than this Mac's; "
            "to fix it, the owner chooses the Xcode the Macs use and selects it with `sudo xcode-select -s PATH` "
            "on the Mac that differs")
    if developer is None or not here.get("xcode"):
        report["unchecked"].append("the Xcode at this Mac's developer directory and its simulator runtimes, since "
                                   "this Mac has no Xcode selected")
        return
    if here["xcode"] != there.get("xcode"):
        report["errors"].append(
            f"the Xcode at this Mac's developer directory, {developer}, is {version(here['xcode'])} on this Mac and "
            f"{version(there.get('xcode')) if there.get('xcode') else 'missing'} on {host}, so qk remote refuses "
            f"every run there; to fix it, install this Mac's Xcode at that path on {host}, "
            "or select with DEVELOPER_DIR an Xcode the Macs have at the same path")
    if None in (here.get("runtimes"), there.get("runtimes")):
        report["unchecked"].append(f"the simulator runtimes, which simctl did not list on "
                                   f"{'this Mac' if here.get('runtimes') is None else host}")
    elif here["runtimes"] != there["runtimes"]:
        differences = simulator_runtimes.runtime_differences(here["runtimes"], there["runtimes"], host)
        report["runtimesDiffer"] = True
        report["errors"].append(f"the simulator runtimes of the Xcode at {developer} differ: {'; '.join(differences)}; "
                                "`qk doctor` evens them out itself, deleting a runtime that no simulator but Xcode's "
                                "defaults uses and copying any other to the Mac that lacks it, and it could not here: "
                                f"see its `runtimes:` lines, or run `{qk_said('remote setup --runtimes')}`")


def fix_hosts(root, hosts):
    """What `doctor` fixes of remote.compare_hosts' report itself (the owner's decision, 2026-11-03): runtimes that
    differ, which it evens out (simulators.runtimes.match_runtimes) and then compares again; stale pool simulators on
    the Macs, which it shuts down (simulators.pool.reap_reports) and lists in reaped; and Xcode's default simulators
    on this Mac, which it deletes as `disk --delete` does, checking each again first, and lists in
    deletedDefaults. Its progress goes to standard error, so that standard output stays the report."""
    if hosts.pop("runtimesDiffer", False):
        with contextlib.redirect_stdout(sys.stderr): simulator_runtimes.match_runtimes(hosts["host"], check=False)
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
            "one that is shut down and unused, and `qk disk` says why it kept these"]


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
            "for platform divergence, is not counted"]


# The words that name a value of a test dimension in a lane's command: an option a project's script or runner takes,
# simctl's `ui DEVICE appearance|content_size VALUE`, and the launch arguments that set the app's own.
DIMENSION_OPTIONS = {"--appearance": "appearance", "-AppleInterfaceStyle": "appearance",
                     "--content-size": "content size", "--content-size-category": "content size",
                     "--dynamic-type": "content size", "-UIPreferredContentSizeCategoryName": "content size"}
SIMCTL_DIMENSIONS = {"appearance": "appearance", "content_size": "content size"}


def dimension_words(command):
    """The values of appearance and content size a command names, as {dimension: {value}}, and the command without
    them, which is its selection. A word with spaces, such as a `bash -c` script, is read word by word too."""
    words = []
    for word in command:
        try: words += shlex.split(word) if isinstance(word, str) and " " in word else [str(word)]
        except ValueError: words.append(str(word))
    named, rest, index = {}, [], 0
    while index < len(words):
        word = words[index]
        option, _, value = word.partition("=")
        if option in DIMENSION_OPTIONS and (value or index + 1 < len(words)):
            named.setdefault(DIMENSION_OPTIONS[option], set()).add(value or words[index + 1])
            index += 1 if value else 2
            continue
        if word == "ui" and index + 3 < len(words) and words[index + 2] in SIMCTL_DIMENSIONS:
            # The whole simctl step is the setting, not the selection: its xcrun simctl and the && after it too.
            named.setdefault(SIMCTL_DIMENSIONS[words[index + 2]], set()).add(words[index + 3])
            while rest and Path(rest[-1]).name in {"xcrun", "simctl"}: rest.pop()
            index += 4
            if index < len(words) and words[index] in {"&&", ";"}: index += 1
            continue
        rest.append(word)
        index += 1
    return named, tuple(rest)


def dimension_warnings(config):
    """A warning of the lanes that run one selection once per appearance or content size (vocaro/queuekeeper#105):
    only the snapshot tests of a view should multiply, and every other test then runs again for each. A lane is
    counted whose one command names two values of either, or whose command is another lane's but for its value of
    either. Only the lanes' own words are read: a script that sets the simulator's appearance itself, as one with a
    deliberately narrow second selection does, is not seen. A lane of release.lanes, or of a group there, is the full
    control, and a lane whose "dimensions" says why its whole selection depends on them has said so."""
    lanes = config.get("lanes", {})
    release = {member for name in config.get("release", {}).get("lanes", [])
               for member in [name, *lanes.get(name, {}).get("parallel", [])]}
    read = {name: dimension_words(lane["command"]) for name, lane in lanes.items()
            if isinstance(lane.get("command"), list) and name not in release and "dimensions" not in lane}
    found = {name for name, (named, _) in read.items() if any(len(values) > 1 for values in named.values())}
    for name, (named, selection) in read.items():
        for other, (theirs, same) in read.items():
            if other != name and named and same == selection and any(
                    theirs.get(dimension, set()) != values for dimension, values in named.items()):
                found |= {name, other}
    if not found: return []
    return [f"{len(found)} lane(s) run one selection once per appearance or content size, so each test whose result "
            f"depends on neither runs again for each: {', '.join(sorted(found))}; multiply only the snapshot tests of "
            "a view and run the rest once, or say in the lane's \"dimensions\" why its whole selection depends on "
            "them, such as that it selects only snapshot tests; a lane in release.lanes, the full control, is not "
            "counted"]


# What marks a direct XCTest wait in a UI test that keeps XCTest's own wait, with the reason after it, such as one
# inside a measured window or one that watches for a change and has nothing to check first.
XCTEST_WAIT = "xctest-wait:"
DIRECT_WAIT = re.compile(r"(?<!func )\bwaitFor(?:Non)?Existence\s*\(")


def direct_waits(text):
    """The line numbers of a Swift file's direct waitForExistence and waitForNonExistence calls, less those whose line,
    or the line before, carries `// xctest-wait: REASON`. A call in a comment is not one."""
    lines = text.splitlines()
    return [number for number, line in enumerate(lines, 1)
            if DIRECT_WAIT.search(line.split("//", 1)[0])
            and not any(XCTEST_WAIT in near for near in lines[max(number - 2, 0):number])]


def ui_test_target(path):
    """The folder that stands for a UI test file's target: its nearest folder whose name ends in Tests, else its own."""
    return next((str(Path(*path.parts[:index + 1])) for index in range(len(path.parts) - 2, -1, -1)
                 if path.parts[index].endswith("Tests")), str(path.parent))


def wait_helper_warnings(root, config=None, shown=5):
    """A warning of the UI test targets whose direct XCTest waits are spread over more than one file
    (vocaro/queuekeeper#105). XCTest first evaluates a waitForExistence predicate a second after the wait starts, so
    each wait for an element already on screen costs a second; a target puts its waits in one helper file that checks
    first, and a wait that keeps XCTest's says so with `// xctest-wait: REASON`. A UI test file is a tracked Swift
    file that imports XCTest and uses XCUIApplication or XCUIElement; its target is ui_test_target's guess."""
    listed = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--", "*.swift"], capture_output=True,
                            stdin=subprocess.DEVNULL)
    if listed.returncode: return []
    targets = {}
    for name in sorted(filter(None, listed.stdout.decode().split("\0"))):
        path = Path(name)
        if "evidence" in path.parts: continue
        try: text = (Path(root) / path).read_text(errors="replace")
        except OSError: continue
        if "import XCTest" not in text or not re.search(r"\bXCUI(?:Application|Element)\b", text): continue
        numbers = direct_waits(text)
        if numbers: targets.setdefault(ui_test_target(path), {})[name] = len(numbers)
    spread = {target: files for target, files in sorted(targets.items()) if len(files) > 1}
    if not spread: return []
    said = "; ".join(f"{target}: {sum(files.values())} in {len(files)} files, "
                     + ", ".join(sorted(files, key=lambda name: (-files[name], name))[:shown])
                     + (f" and {len(files) - shown} more" if len(files) > shown else "")
                     for target, files in spread.items())
    return [f"{len(spread)} UI test target(s) call XCTest's waitForExistence or waitForNonExistence directly from more "
            f"than one file, and each wait for an element already there costs a second before XCTest first checks: "
            f"{said}. Put the waits in one helper file of the target that checks the condition first, each with the "
            f"limit it replaced; a wait that keeps XCTest's, inside a measured window or watching for a change, says "
            f"so with `// {XCTEST_WAIT} REASON` on its line or the line before"]


def simulator_warnings(root, config):
    return (default_simulators(config) + idiom_warnings(config) + dimension_warnings(config)
            + wait_helper_warnings(root, config))


def required_commands(config):
    """Xcode's tools, for a project that uses simulators or keeps an app."""
    return {"xcrun", "xcodebuild"} if config.get("simulatorPool") or config.get("app") else set()


def register():
    """Register Apple checks and fixes for doctor and remote setup."""
    if remote_setup_xcode not in plugins.REMOTE_SETUP_CHECKS:
        plugins.REMOTE_SETUP_CHECKS.append(remote_setup_xcode)
    if required_commands not in plugins.REQUIRED_COMMANDS: plugins.REQUIRED_COMMANDS.append(required_commands)
    inventory_checks = getattr(plugins, "HOST_INVENTORY_CHECKS", None)
    # An older core compares the Apple inventory directly.
    if inventory_checks is not None and compare_toolchains not in inventory_checks:
        inventory_checks.append(compare_toolchains)
    setup_runtimes = getattr(plugins, "REMOTE_SETUP_RUNTIMES", None)
    # An older core evens out the runtimes for `qk remote setup --runtimes` itself.
    if setup_runtimes is not None and simulator_runtimes.match_runtimes not in setup_runtimes:
        setup_runtimes.append(simulator_runtimes.match_runtimes)
    if fix_hosts not in plugins.HOST_FIXES: plugins.HOST_FIXES.append(fix_hosts)
    if simulator_warnings not in plugins.DOCTOR: plugins.DOCTOR.append(simulator_warnings)
    plugins.DOCTOR_WITHOUT_REPAIRS[simulator_warnings] = simulator_warnings
