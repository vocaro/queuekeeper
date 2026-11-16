"""Build a scheme's tests once and run them on several members of the project's simulator pool side by side.

    qk simulator-tests --scheme SCHEME --runtime iOS-27-0 --device iPhone-17-Pro \\
        --device iPad-Air-13-inch-M4 [--pool NAME] [--build-jobs 4] [--forward-env PREFIX] \\
        [-- TEST-ARGUMENTS such as -only-testing:T/C]

Each device is a member of the pool, `POOL LABEL N`: a free one of its kind is reserved in the
host's device record and reset from its kind's warmed template (erased with --erase, or when no settled
template can be copied), or, with --keep-state, kept as earlier runs left it, or, when none is
free, the next is cloned from that template, or created, and kept in the pool, after members idle
past the pruning rule's limits are pruned (doc/guides/simulator-pool.md, Reserved simulator pool).
With --app-accessibility each member's app processes get UIKit's and SwiftUI's own accessibility, for tests
hosted in an app that read system views (doc/guides/simulator-pool.md, App accessibility). With --voiceover each
member's VoiceOver sound cues are switched off and its volume held at 0 before its tests, for a probe that
switches VoiceOver on, and its volume is watched while they run (AXVerify's guides/voiceover-simulator.md, Muted speech
capture). With --xcodegen SPEC
the Xcode project is generated from SPEC before the build. Every device boots while the tests build, and is shut down and released however the run ends. The
build is one `xcodebuild build-for-testing` for the first device's destination; each device
then runs `xcodebuild test-without-building` on the same .xctestrun, with its own log, result
bundle and derived data folder, removed when its tests end. Each device's outcome stands apart: a boot that reached its limit leaves that
device unmeasured, and the run fails when any device failed, and is otherwise unmeasured when
any device was. doc/guides/parallel-tests.md says how to bound it. A build that fails on a module older
than a header it was built from clears that module cache and builds once more, and says so.

One run at a time builds into a derived data folder and writes a logs folder: a later run that
names either waits for the earlier to end, and says so. Simulators test from the build's products
in place, and a build can replace a test bundle's embedded resource bundle whole, so a build beside
another run's tests can take the files they read from under them (vocaro/EPUBLib#872).

With --shards N each device's kind takes N members, and each runs one test process, a share of the
tests: Swift Testing runs a test process's suites at once, and xcodebuild's parallel workers do not
share Swift Testing suites between clones (Xcode 27.2, 2026-11-05), so a suite on the main actor would
take turns with every other at each await. The tests are listed once, with
`xcodebuild test-without-building -enumerate-tests` and the run's own -only-testing and -skip-testing,
and packed by class or suite into the shards, longest first by the seconds the last run's result
bundles gave each, kept in the logs folder; each member then runs its share, with parallel testing
off, by -only-testing. The device's line adds up its shards.

With --build-only it builds and tests nothing: it takes the first device's member without erasing or
booting it, only to name the build's destination, and returns it once the build ends. A gate can then
build while its earlier stages run, and the full run that follows finds the build up to date
(vocaro/EPUBLib#958).

Under a lane, whose command finds its results/ in QUEUEKEEPER_RESULTS_DIR, each test run, a shard or a device,
also keeps there, in simulator-tests/, the lines of its log that give each test's and suite's outcome and
seconds, KIND.log, and its result bundle's summary and each class's or suite's seconds, KIND.json: they
come back from the second Mac with the lane's dispatch, and are kept by commit beside its receipt, where
the logs folder's are overwritten by the next run (vocaro-engineering#218). `qk results keep` writes the
same two files for a script that runs xcodebuild itself (vocaro-engineering#260).

With --repeat N the tests are built once and run N times, each run on a member of each device's kind taken from the
pool afresh, by the same take, and so the same reset, as any other run: erased with --erase, a copy of the warmed
template with --template, which leaves a run unmeasured when the pool could only erase. The options may follow the
--, so that `qk run LANE --repeat N` reaches this through the arguments a lane appends, on either Mac; --only TEST
then replaces the lane's own selection. Each run keeps its own outcome, test counts and one-minute load at its start
and end, printed one line each, added up on the "Repeat series:" line and kept in series.json. Any failed run fails
the series, and a run that did not happen is unmeasured, never passed (apple-engineering's guides/simulator-testing.md, Repeating a test
to show a fix works).
"""
import argparse
import contextlib
from concurrent.futures import ThreadPoolExecutor
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import uuid

from queuekeeper import lane_report, lane_results, processes, resources
from queuekeeper.parallel import RESULTS_VARIABLE, TAIL, combined, outcome
from .. import stale_modules
from . import pool as simulator_pool, speech
from .pool import DEVICE_TYPE, RUNTIME, qualified

# Seconds simctl boot itself may take; bootstatus has --boot-timeout.
BOOT_COMMAND = 120
# Seconds one test may run before XCTest fails it, records a spindump and goes on to the next, unless
# --test-allowance or the test's own executionTimeAllowance says otherwise: XCTest's own default once
# -test-timeouts-enabled is on, which it rounds up to whole minutes.
TEST_ALLOWANCE = 600
# What clang says of an explicit module built before a dependency changed a header, and the folder of
# derived data whose removal let the next build pass (vocaro/vocaro-engineering#85).
STALE_MODULE = stale_modules.STALE_MODULE
# What the build-tool wrapper says when it has rebuilt already.
STALE_NOTICE = stale_modules.NOTICE
EXPLICIT_MODULES = "Build/Intermediates.noindex/SwiftExplicitPrecompiledModules"


class _ClockArguments(argparse.ArgumentParser):
    """Read a lane's clock without printing or exiting on arguments its own process will handle."""
    def __init__(self, *args, **options):
        options["add_help"] = False
        super().__init__(*args, **options)

    def error(self, message):
        raise ValueError(message)


def default_test_allowance(argv):
    """The XCTest default the simulator runner supplies, when the command owns that setting."""
    try:
        args = parse(argv, parser_factory=_ClockArguments)
    except ValueError:
        return None
    if args.build_only or any(word.split("=", 1)[0] in {
            "-test-timeouts-enabled", "-default-test-execution-time-allowance", "-maximum-test-execution-time-allowance"
    } for word in args.passed):
        return None
    return args.test_allowance


def parse(argv, *, parser_factory=argparse.ArgumentParser):
    split = argv.index("--") if "--" in argv else len(argv)
    parser = parser_factory(prog="qk simulator-tests", description=__doc__.split("\n\n")[0])
    parser.add_argument("-C", "--directory", type=Path, help="run in this directory, as for a package's scheme, "
                        "which xcodebuild finds in its working directory; other paths are relative to it")
    parser.add_argument("--scheme", required=True)
    where = parser.add_mutually_exclusive_group()
    where.add_argument("--xcode-project", help="the .xcodeproj; a package's scheme needs neither this nor --workspace")
    where.add_argument("--workspace")
    parser.add_argument("--xcodegen", type=Path, metavar="SPEC", help="generate the --xcode-project from this "
                        "XcodeGen spec before the build, for a project whose generated .xcodeproj is not tracked, "
                        "such as a test-only host app's")
    parser.add_argument("--device", action="append", required=True, metavar="TYPE",
                        help="a simulator device type, such as iPhone-17-Pro; repeat for each device")
    parser.add_argument("--runtime", required=True, help="a simulator runtime, such as iOS-27-0")
    parser.add_argument("--jobs", type=int, help="devices tested at a time (default: every device)")
    parser.add_argument("--build-jobs", type=int, default=4, help="xcodebuild -jobs for the build (default 4)")
    parallel = parser.add_mutually_exclusive_group()
    parallel.add_argument("--workers", type=int, help="xcodebuild's parallel test workers on each device, each a "
                          "clone of it; without this each device runs one test process")
    parallel.add_argument("--shards", type=int, help="pool members of each device's kind, each running a share of "
                          "the tests, by class or suite, in a test process of its own, one test at a time")
    parser.add_argument("--boot-timeout", type=int, default=600, help="seconds a boot may take (default 600)")
    fresh = parser.add_mutually_exclusive_group()
    fresh.add_argument("--keep-state", action="store_true", help="take each member without resetting or erasing it, "
                       "for tests that need no fresh installation: it keeps what earlier runs installed and skips "
                       "the first boot's migration (doc/guides/simulator-pool.md, Reserved simulator pool)")
    fresh.add_argument("--erase", action="store_true", help="erase each member in place of the reset from its "
                       "kind's warmed template, for tests that want pristine data (doc/guides/simulator-pool.md, "
                       "Warmed templates)")
    fresh.add_argument("--template", action="store_true", help="with --repeat: every run must get a copy of its "
                       "kind's warmed template; a run the pool could only erase is left unmeasured")
    parser.add_argument("--repeat", type=int, metavar="N", help="build once and run the tests N times, each run on a "
                        "member taken from the pool afresh, one outcome, set of test counts and one-minute load "
                        "each; it may also follow -- (apple-engineering's guides/simulator-testing.md, Repeating a test to show a fix "
                        "works). A lane's --shards is set aside: each run is one test process on each member")
    parser.add_argument("--only", action="append", default=[], metavar="TEST", help="with --repeat: run only this "
                        "test, in place of the run's own -only-testing and -skip-testing arguments; repeat for each")
    parser.add_argument("--app-accessibility", action="store_true", help="switch UIKit's and SwiftUI's own "
                        "accessibility on in each member's app processes once it has booted, for tests hosted in an "
                        "app that read system views; a device whose setting does not read on fails "
                        "(doc/guides/simulator-pool.md, App accessibility)")
    parser.add_argument("--voiceover", action="store_true", help="for tests that switch VoiceOver on, such as a "
                        "speech probe's: switch each member's VoiceOver sound cues off and hold its volume at 0 before "
                        "its tests, read it each second while they run, switch off a VoiceOver they left on, and print "
                        "their VOICEOVER_ lines; a member that could not be silenced, or was heard, fails "
                        "(AXVerify's guides/voiceover-simulator.md, Muted speech capture)")
    parser.add_argument("--build-only", action="store_true", help="build the tests for the first device and "
                        "test nothing, so that a later run with the same options finds the build up to date; the "
                        "member is taken without erasing or booting it, only to name the build's destination")
    parser.add_argument("--test-timeout", type=int, default=processes.DEFAULT_LIMIT, help="seconds after which a "
                        "device's test run is stopped and left unmeasured, a backstop for a hang (default "
                        f"{processes.DEFAULT_LIMIT})")
    parser.add_argument("--test-allowance", type=int, default=TEST_ALLOWANCE, help="seconds one test may run "
                        "before XCTest fails it with a spindump and runs the rest, as xcodebuild's "
                        f"-default-test-execution-time-allowance (default {TEST_ALLOWANCE}); test arguments "
                        "that set -test-timeouts-enabled themselves replace it")
    parser.add_argument("--derived-data", type=Path, default=Path(".build/simulator-tests/derived"))
    parser.add_argument("--logs", type=Path, default=Path(".build/simulator-tests/logs"))
    parser.add_argument("--pool", help="the simulator pool, the start of every member's name (default: the "
                        "project's simulatorPool in queuekeeper.json)")
    parser.add_argument("--setting", action="append", default=[], metavar="NAME=VALUE",
                        help="a build setting for the build, after CODE_SIGNING_ALLOWED=NO")
    parser.add_argument("--forward-env", action="append", default=[], metavar="PREFIX",
                        help="give the test process each variable starting with PREFIX: it goes to xcodebuild as "
                        "TEST_RUNNER_ + its name, unless that is already set; repeat for each prefix")
    args, unknown = parser.parse_known_args(argv[:split])
    if unknown:
        # qk run LANE -- ARGUMENTS appends them to the lane's command, which reach the tests only
        # when that command ends with --.
        parser.error(f"unrecognized arguments: {' '.join(unknown)}. Test arguments such as -only-testing go after "
                     "--, so a lane whose command runs simulator-tests ends with \"--\"")
    args.passed = argv[split + 1:]
    series_options(args, parser_factory)
    for name in ("jobs", "build_jobs", "workers", "shards", "boot_timeout", "test_timeout", "test_allowance", "repeat"):
        if getattr(args, name) is not None and getattr(args, name) < 1:
            parser.error(f"--{name.replace('_', '-')} needs at least 1")
    if args.repeat is None and (args.template or args.only):
        parser.error("--template and --only say how a series runs: give --repeat N too")
    if args.repeat is not None and args.build_only: parser.error("--repeat runs tests, and --build-only runs none")
    args.set_aside_shards = None
    if args.repeat is not None and args.shards:
        args.set_aside_shards, args.shards = args.shards, None
    if args.only:
        args.passed = [*[a for a in args.passed if not selecting(a)], *(f"-only-testing:{test}" for test in args.only)]
    if len(set(args.device)) != len(args.device): parser.error("name each device type once")
    if args.xcodegen is not None and not args.xcode_project:
        parser.error("--xcodegen generates an Xcode project: name it with --xcode-project")
    if not all(args.forward_env): parser.error("--forward-env needs a prefix: an empty one would forward everything")
    if args.voiceover and (args.workers or args.shards or args.build_only):
        # A clone or a shard's member would speak unmuted; a build plays nothing.
        parser.error("--voiceover runs one test process on each member: not with --workers, --shards or --build-only")
    return args


def series_options(args, parser_factory):
    """Move a series' options, from a `--repeat` among the test arguments, onto args, and leave the rest as test
    arguments. They come there from `qk run LANE --repeat N`, which appends them to the lane's command, after its --,
    and which reach this on either Mac as a lane's other arguments do. Their reset replaces the lane's own."""
    if "--repeat" not in args.passed: return
    at = args.passed.index("--repeat")
    parser = parser_factory(prog="qk simulator-tests", add_help=False, allow_abbrev=False)
    parser.add_argument("--repeat", type=int)
    fresh = parser.add_mutually_exclusive_group()
    fresh.add_argument("--erase", action="store_true")
    fresh.add_argument("--template", action="store_true")
    parser.add_argument("--only", action="append", default=[])
    asked, rest = parser.parse_known_args(args.passed[at:])
    args.passed = [*args.passed[:at], *rest]
    args.repeat, args.only = asked.repeat, [*args.only, *asked.only]
    if asked.erase or asked.template:
        args.erase, args.template, args.keep_state = asked.erase, asked.template, False


def forwarded(prefixes, environment):
    """The TEST_RUNNER_ variables that give the test process each variable starting with a prefix. A test
    process in a simulator sees only what xcodebuild is given that way (apple-engineering's guides/simulator-testing.md, Test
    environment), and a TEST_RUNNER_ variable already set is the caller's and stays."""
    if not prefixes: return {}
    return {f"TEST_RUNNER_{name}": value for name, value in environment.items()
            if name.startswith(tuple(prefixes)) and f"TEST_RUNNER_{name}" not in environment}


# What tells the golden API that the run promised UIKit's and SwiftUI's own views to every fixture, so that a
# test process that reads none fails each golden instead of comparing an empty tree (AccessibilityGolden).
SYSTEM_VIEWS = "TEST_RUNNER_ACCESSIBILITY_SNAPSHOT_SYSTEM_VIEWS"


def system_views(app_accessibility, environment):
    """The variable a run with app accessibility gives its test processes, unless the caller set it."""
    return {SYSTEM_VIEWS: "1"} if app_accessibility and SYSTEM_VIEWS not in environment else {}


# Each class's or suite's seconds in the last sharded run, Bundle/Suite, which packs the next one's shards.
SHARD_TIMINGS = "shard-timings.json"
# The folder under a lane's results/ in which each test run keeps its per-test lines and summary (kept).
KEPT = "simulator-tests"
# A log's lines that give one test's, suite's or run's outcome, with its seconds, or an XCTest failure: Swift
# Testing's `✔ Test "t" passed after 0.7 seconds.` and `✔ Suite S passed after 4.0 seconds.`, XCTest's
# `Test Case '-[B.C t]' passed (0.7 seconds).`, `Test Suite 'C' passed at ...` and `Executed N tests`.
RESULT_LINE = re.compile(r"^(?:\S+ (?:Test|Suite) |Test [Cc]ase '|Test Suite '|\s*Executed \d+ tests?\b|.*: error: -\[)")
STARTED_LINE = re.compile(r"(?: started\.| started at [-0-9: .]+)$")
# Each result a bundle's test tree gives a test case, as a kept record names it. Anything else is unmeasured.
OUTCOMES = {"Passed": "passed", "Failed": "failed", "Skipped": "skipped", "Expected Failure": "expected failure"}
# What a test that could not measure its subject begins its skip message with.
UNMEASURED_SKIP = "Test skipped - Unmeasured:"
# Why a kept record has no test's outcome, though its run may have passed.
UNREAD = "xcresulttool could not read the result bundle's test results"
NO_CASES = "the result bundle records no test case"
# The -only-testing and -skip-testing arguments, which a sharded run applies when it lists the tests.
SELECTING = ("-only-testing", "-skip-testing")


def selecting(argument):
    return argument.startswith(SELECTING)


def enumerated(listing):
    """(enabled test ids, units with a disabled test) from xcodebuild -enumerate-tests' flat JSON."""
    enabled, partial = [], set()
    for value in listing.get("values", []):
        enabled += [test["identifier"] for test in value.get("enabledTests", [])]
        partial |= {unit(test["identifier"]) for test in value.get("disabledTests", [])}
    return enabled, partial


def unit(identifier):
    """A test's class or suite, Bundle/Suite, from its identifier, Bundle/Suite/test()."""
    return "/".join(identifier.split("/")[:2])


def packed(tests, partial, seconds, count):
    """count lists of -only-testing arguments, longest first into the one with least: each unit whole, unless
    the run left some of its tests out, when its tests one by one. A unit with no recorded seconds counts
    the median of those that have one."""
    units = {}
    for test in tests: units.setdefault(unit(test), []).append(test)
    known = sorted(seconds[name] for name in units if name in seconds)
    guess = known[len(known) // 2] if known else 1.0
    bins = [[0.0, []] for _ in range(count)]
    for name in sorted(units, key=lambda name: (-seconds.get(name, guess), name)):
        bin = min(bins, key=lambda entry: entry[0])
        bin[0] += seconds.get(name, guess)
        bin[1] += [f"-only-testing:{test}" for test in units[name]] if name in partial else [f"-only-testing:{name}"]
    return [names for _, names in bins]


def suite_seconds(bundle, record=None):
    """{Bundle/Suite: seconds} over a result bundle's test cases, or {} when xcresulttool cannot read it.
    record is the bundle's, as bundle_record read it, when it has been read already."""
    record = record if record is not None else bundle_record(bundle)
    if record is None: return {}
    tests = record[1]
    seconds = {}
    def walk(node, path):
        kind = node.get("nodeType")
        if kind in ("Unit test bundle", "UI test bundle", "Test Suite") and len(path) < 2: path = [*path, node.get("name")]
        if kind == "Test Case" and len(path) == 2:
            seconds["/".join(path)] = seconds.get("/".join(path), 0.0) + float(node.get("durationInSeconds") or 0)
            return
        for child in node.get("children", []): walk(child, path)
    for node in tests.get("testNodes", []): walk(node, [])
    return seconds


def test_cases(record, activity_timings=None):
    """Each test case in a bundle's test tree (bundle_record), as {test, outcome, seconds}. outcome is passed,
    failed, skipped, expected failure or unmeasured, the last with its reason: a skip that says it could not
    measure, or a result this does not know. seconds is None when the bundle gives none.
    An optional activity reader gets the case's identifier and supplies its selected timings."""
    found = []
    def skip_reason(node):
        if node.get("nodeType") == "Skip Message" and UNMEASURED_SKIP in node.get("name", ""):
            return node["name"].split(UNMEASURED_SKIP, 1)[1].strip()
        return next(filter(None, map(skip_reason, node.get("children", []))), None)
    def walk(node, path):
        name = node.get("name", "")
        if node.get("nodeType") != "Test Case":
            for child in node.get("children", []): walk(child, path if node.get("nodeType") == "Test Plan" else [*path, name])
            return
        try: seconds = float(node["durationInSeconds"])
        except (KeyError, TypeError, ValueError): seconds = None
        case = {"test": node.get("nodeIdentifier") or "/".join([*path, name]),
                "outcome": OUTCOMES.get(node.get("result"), "unmeasured"), "seconds": seconds}
        reason = skip_reason(node) if case["outcome"] == "skipped" else None
        if reason is not None: case["outcome"] = "unmeasured"
        if case["outcome"] == "unmeasured": case["reason"] = reason or f"the bundle gives its result as {node.get('result')!r}"
        if activity_timings is not None:
            case["activities"] = activity_timings(node.get("nodeIdentifierURL") or case["test"])
        found.append(case)
    for node in record[1].get("testNodes", []): walk(node, [])
    return found


def kept(kind, log, record, status, seconds, results=None, unread=None, by="qk simulator-tests", *, cases=None):
    """Keep what a reader cites of one test run in its lane's results/, KEPT/KIND.log and KIND.json, and
    return that folder, or None outside a lane (no RESULTS_VARIABLE): the log's lines that give each test's
    and suite's outcome and seconds (RESULT_LINE), and the result bundle's summary, as xcresulttool prints
    it, with each class's or suite's seconds and each test case's outcome and seconds (test_cases). record is
    the bundle's (bundle_record), None when it could not be read, and then bundleUnread says why, as unread
    when given, as it does for a bundle with no test case; a log that is missing keeps no lines, and its JSON
    says so. status is None for a run stopped at its limit. by names the command that kept it.
    A caller can supply cases already read, including any selected activity timings."""
    results = results if results is not None else os.environ.get(RESULTS_VARIABLE)
    if not results: return None
    folder = Path(results) / KEPT
    tests = test_cases(record) if record is not None and cases is None else cases
    try:
        folder.mkdir(parents=True, exist_ok=True)
        lines = None
        if log.exists():
            lines = [line for line in log.read_text(errors="replace").splitlines()
                     if RESULT_LINE.match(line) and not STARTED_LINE.search(line)]
            (folder / f"{kind}.log").write_text("".join(f"{line}\n" for line in lines))
        (folder / f"{kind}.json").write_text(json.dumps({
            "run": kind, "keptBy": by, "status": status, "seconds": seconds, "log": log.name if lines is not None else None,
            "resultLines": None if lines is None else len(lines),
            "summary": record[0] if record is not None else None,
            "suiteSeconds": suite_seconds(None, record) if record is not None else None,
            "tests": tests, "bundleUnread": unread or (UNREAD if tests is None else None if tests else NO_CASES)},
            indent=1) + "\n")
    except OSError as error:
        print(f"queuekeeper simulator-tests: {kind}'s results were not kept in {folder}: {error}", file=sys.stderr, flush=True)
        return None
    return folder


class Run:
    def __init__(self, args):
        self.args, self.lock, self.results = args, threading.Lock(), {}
        self.logs = args.logs.resolve()
        # The listing has a lock of its own: say() takes self.lock, and the listing says what it found.
        self.shards, self.listing, self.seconds, self.listing_lock = {}, None, {}, threading.Lock()
        # Each test run's own, for a series: what finished it, its seconds, its test counts and its loads.
        self.details, self.durations, self.tallies, self.loads = {}, {}, {}, {}

    def say(self, *lines):
        with self.lock: print(*lines, sep="\n", flush=True)

    def finish(self, kind, status, detail, log=None, seconds=None):
        self.results[kind] = status
        self.details[kind], self.durations[kind] = detail, seconds
        timing = f" in {seconds}s" if seconds is not None else ""
        lines = [f"{outcome(status)} {kind}: {detail}{timing}" + (f"; log: {log}" if log else "")]
        if outcome(status) == "FAIL" and log is not None and log.exists():
            lines += ["    " + line for line in log.read_text(errors="replace").splitlines()[-TAIL:]]
        self.say(*lines)

    def boot(self, kind, device):
        """None once the device has booted; otherwise its outcome is already reported."""
        log = self.logs / f"{kind}-boot.log"
        self.say(f"BOOT {kind} ({device.udid}), limit {self.args.boot_timeout}s; log: {log}")
        started = time.monotonic()
        try:
            with log.open("w") as stream:
                status = processes.supervise(["xcrun", "simctl", "boot", device.udid], cwd=Path.cwd(),
                                             stdout=stream, timeout=BOOT_COMMAND)
                if status == 0:
                    status = processes.supervise(["xcrun", "simctl", "bootstatus", device.udid, "-b"], cwd=Path.cwd(),
                                                 stdout=stream, timeout=self.args.boot_timeout)
        except processes.TimedOut:
            # A boot still going at its limit says nothing about the tests. A first boot can stall
            # in data migration while other simulators boot.
            self.finish(kind, lane_results.UNMEASURED, f"the boot exceeded its limit, so no test ran", log)
            return False
        if status != 0:
            self.finish(kind, 1, f"the boot failed with status {status}", log)
            return False
        self.say(f"BOOTED {kind} ({device.udid}) in {round(time.monotonic() - started)}s")
        why = simulator_pool.set_app_accessibility(simulator_pool.simctl, device.udid, self.args.app_accessibility,
                                                   kept=device.kept)
        if why is not None and self.args.app_accessibility:
            # Its hosted tests would read no system views, and a golden of them fails its precondition.
            self.finish(kind, 1, f"its app accessibility could not be switched on, so no test ran: {why}", log)
            return False
        if why is not None:
            self.say(f"APP ACCESSIBILITY {kind}: an earlier holder's setting could not be removed: {why}")
        elif self.args.app_accessibility:
            self.say(f"APP ACCESSIBILITY {kind}: on ({simulator_pool.APP_ACCESSIBILITY_DOMAIN} "
                     f"{simulator_pool.APP_ACCESSIBILITY_KEY} reads 1)")
        return self.silence(kind, device, log) if self.args.voiceover else True

    def silence(self, kind, device, log):
        """With --voiceover, VoiceOver's sound cues off and the volume held at 0 before any test runs: a
        simulator's VoiceOver speaks through the Mac's own output, and its cues play past the mute. False,
        with the device failed, when either could not be done (AXVerify's guides/voiceover-simulator.md, Muted speech
        capture)."""
        try:
            off, why = speech.silence_cues(device.udid), "it did not read back off"
        except (RuntimeError, OSError, subprocess.SubprocessError) as error:
            off, why = False, str(error).strip() or type(error).__name__
        if not off:
            self.finish(kind, 1, f"VoiceOver's sound cues could not be switched off ({why}), so no test ran", log)
            return False
        try:
            seconds, unmuted = speech.hold_mute(device.udid)
        except (RuntimeError, OSError, subprocess.SubprocessError, ValueError, KeyError) as error:
            self.finish(kind, 1, f"it could not be muted ({error}), so no test ran", log)
            return False
        again = f", muted again after reading {', '.join(map(str, unmuted))}" if unmuted else ""
        self.say(f"MUTED {kind}: VoiceOver's sound cues off; the volume read 0 {speech.MUTE_HELD} times in a row "
                 f"in {round(seconds)}s{again}")
        return True

    @contextlib.contextmanager
    def heard(self, kind, device):
        """With --voiceover, the volume read each second while the device's tests run and muted again when
        it comes back, then VoiceOver switched off if they left it on; the device fails when either
        happened. Its log's VOICEOVER_ lines are printed after its result, so that a run whose result is
        its output carries the transcript."""
        session = SimpleNamespace(udid=device.udid, unmuted=[], unread=[])
        done = threading.Event()
        def watch():
            while not done.wait(1): speech.keep_muted(session)
        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
        try:
            yield
        finally:
            done.set()
            watcher.join(timeout=30)
            problems = list(session.unmuted)
            try:
                if speech.state(device.udid, "voiceover")["enabled"]:
                    still = speech.state(device.udid, "voiceover", "--disable")["enabled"]
                    problems.append(f"VoiceOver was left on; it {'is still on' if still else 'has been switched off'}")
                volume = speech.state(device.udid, "audio")["volume"]
                if volume != 0: problems.append(f"the volume read {volume} after the tests")
            except (RuntimeError, ValueError, KeyError) as error:
                problems.append(f"VoiceOver's state is unknown: {error}")
            log = self.logs / f"{kind}.log"
            lines = [line for line in (log.read_text(errors="replace").splitlines() if log.exists() else [])
                     if line.startswith(("VOICEOVER_EVENT ", "VOICEOVER_TREE "))]
            unread = f"; {len(session.unread)} volume checks got no reading" if session.unread else ""
            self.say(*(f"{kind} {line}" for line in lines),
                     f"VOICEOVER {kind}: {len(lines)} lines; " + ("; ".join(problems) if problems else
                                                                 "VoiceOver off and the volume 0 after the tests") + unread)
            if problems and kind in self.results:
                self.finish(kind, 1, "the simulator was heard or left speaking: " + "; ".join(problems))

    def build(self, destination):
        """The .xctestrun the build wrote, or None after reporting why there is none."""
        args, log = self.args, self.logs / "build.log"
        location = (["-project", args.xcode_project] if args.xcode_project else
                    ["-workspace", args.workspace] if args.workspace else [])
        command = ["xcodebuild", "build-for-testing", "-scheme", args.scheme, *location, "-destination", destination,
                   "-derivedDataPath", str(args.derived_data), "-jobs", str(args.build_jobs),
                   "CODE_SIGNING_ALLOWED=NO", *args.setting]
        if args.xcodegen is not None:
            generated = self.logs / "xcodegen.log"
            self.say(f"GENERATE {args.xcode_project} from {args.xcodegen}; log: {generated}")
            try:
                status = processes.run(["xcodegen", "generate", "--quiet", "--spec", str(args.xcodegen)],
                                       cwd=Path.cwd(), log_path=generated, timeout=300)
            except processes.TimedOut:
                status = "a timeout after 300 s"
            if status != 0:
                self.say(f"FAIL build: xcodegen ended with status {status}; log: {generated}",
                         *("    " + line for line in generated.read_text(errors="replace").splitlines()[-TAIL:]))
                return None
        self.say(f"BUILD {args.scheme} for {destination}; log: {log}")
        started = time.time()
        status = processes.run(command, cwd=Path.cwd(), log_path=log)
        stale = self.logs / "build-stale-modules.log"
        stale.unlink(missing_ok=True)
        text = log.read_text(errors="replace")
        # Under qk run, the xcodebuild wrapper has removed the stale modules and rebuilt once already (#267).
        if status != 0 and STALE_MODULE in text and STALE_NOTICE not in text:
            # Reused derived data can outlive a dependency change, and only the modules are out of date.
            # One rebuild, not a loop: a second occurrence is a failure the cache does not explain.
            log.replace(stale)
            modules = args.derived_data / EXPLICIT_MODULES
            removed = modules.exists()
            if removed: shutil.rmtree(modules)
            self.say(f"STALE MODULES: the build failed with \"{STALE_MODULE}\"; "
                     f"{'removed' if removed else 'found no'} {modules}, rebuilding once; first log: {stale}")
            started = time.time()
            status = processes.run(command, cwd=Path.cwd(), log_path=log)
            self.say(f"STALE MODULES: the rebuild ended with status {status}" +
                     (f", the same error again, so it is not rebuilt again" if status != 0 and STALE_MODULE in
                      log.read_text(errors="replace") else ""))
        products = args.derived_data / "Build/Products"
        written = sorted((path for path in products.glob(f"{args.scheme}_*.xctestrun")
                          if path.stat().st_mtime >= started - 1), key=lambda path: path.stat().st_mtime)
        if status != 0 or not written:
            reason = f"status {status}" if status != 0 else f"it wrote no {args.scheme}_*.xctestrun in {products}"
            self.say(f"FAIL build: {reason}; log: {log}",
                     *("    " + line for line in log.read_text(errors="replace").splitlines()[-TAIL:]))
            return None
        self.say(f"BUILT {written[-1]} in {round(time.time() - started)}s")
        return written[-1]

    def test(self, kind, device, xctestrun, selection=None):
        args, log = self.args, self.logs / f"{kind}.log"
        bundle = self.logs / f"{kind}.xcresult"
        # xcodebuild refuses a result bundle path that exists; each run's bundle is new.
        shutil.rmtree(bundle, ignore_errors=True)
        # Without a derived data folder of its own, test-without-building writes logs, test results and
        # coverage data to a new folder under ~/Library/Developer/Xcode/DerivedData on every run, which
        # records no workspace and nothing removes: 1850 of them, 15 GiB, on 2026-11-04
        # (vocaro-engineering#158). The result bundle keeps what the run reports; this folder goes.
        scratch = self.logs / f"{kind}.derived"
        shutil.rmtree(scratch, ignore_errors=True)
        self.loads[kind] = {"start": resources.one_minute_load(), "end": None}
        try: return self.run_test(kind, device, xctestrun, log, bundle, scratch, selection)
        finally:
            self.loads[kind]["end"] = resources.one_minute_load()
            shutil.rmtree(scratch, ignore_errors=True)

    def run_test(self, kind, device, xctestrun, log, bundle, scratch, selection=None):
        """With selection, a shard's -only-testing arguments in place of the run's own selecting ones, and
        (status, executed, skipped, unmeasured lines, detail) returned, not reported."""
        args = self.args
        workers = ["-parallel-testing-enabled", "YES", "-parallel-testing-worker-count", str(args.workers)] \
            if args.workers else ["-parallel-testing-enabled", "NO"]
        passed = args.passed if selection is None else [*[a for a in args.passed if not selecting(a)], *selection]
        # A hung test fails by itself, named, and the rest still run, well before the run's own backstop.
        allowance = [] if "-test-timeouts-enabled" in args.passed else \
            ["-test-timeouts-enabled", "YES", "-default-test-execution-time-allowance", str(args.test_allowance)]
        command = ["xcodebuild", "test-without-building", "-xctestrun", str(xctestrun),
                   "-destination", f"platform=iOS Simulator,id={device.udid}", "-destination-timeout", "600",
                   *workers, *allowance, "-collect-test-diagnostics", "never", "-resultBundlePath", str(bundle),
                   "-derivedDataPath", str(scratch), *passed]
        self.say(f"TEST {kind} ({device.udid}); log: {log}")
        started = time.monotonic()
        try:
            status = processes.run(command, cwd=Path.cwd(), log_path=log, timeout=args.test_timeout,
                                   env={**os.environ, **forwarded(args.forward_env, os.environ),
                                        **system_views(args.app_accessibility, os.environ)})
        except processes.TimedOut:
            kept(kind, log, bundle_record(bundle), None, None)
            if selection is not None: return lane_results.UNMEASURED, 0, 0, [], f"{kind}'s tests exceeded {args.test_timeout}s"
            return self.finish(kind, lane_results.UNMEASURED, f"the tests exceeded {args.test_timeout}s", log)
        seconds = round(time.monotonic() - started)
        text = log.read_text(errors="replace")
        # Tests that skipped ran nothing, so they are reported beside the count, not in it. The result
        # bundle's record counts them; the console, with no bundle to read, is the fallback.
        record = bundle_record(bundle)
        kept(kind, log, record, status, seconds)
        recorded = bundle_results(bundle, kind, record)
        if recorded is not None: executed, skipped, from_bundle = recorded
        else: executed, skipped, from_bundle = lane_results.executed_tests(text) or 0, lane_results.skipped_tests(text), []
        failed = failed_tests(record, text)
        # The failed tests are counted in this line, which is all a lane reads of the device's run: the device's own
        # lines that name them are in its log alone (vocaro/queuekeeper#81).
        counted = f"Executed {executed} tests" + (f", {skipped} skipped" if skipped else "") + \
            (f", {failed} failed" if failed else "")
        console = lane_results.unmeasured_tests(text)
        # The bundle names each skipped XCTest test once, so the console's lines for them would repeat it.
        if recorded is not None: console = [line for line in console if "Test skipped - Unmeasured:" not in line]
        self.tallies[kind] = tally(status, executed, skipped, len(console) + len(from_bundle), failed)
        if selection is not None:
            with self.lock: self.seconds.update(suite_seconds(bundle, record))
            # Not "Executed N tests", which a lane would count beside the device's own total.
            self.say(f"SHARD {kind}: status {status}, ran {executed} tests" + (f", {skipped} skipped" if skipped else "")
                     + f" in {seconds}s; log: {log}")
            detail = f"{kind} failed with status {status}" if status != 0 else \
                f"{kind} ran no tests" if executed == 0 and selection else None
            return (1 if detail else 0), executed, skipped, [*console, *from_bundle], detail
        if status != 0:
            return self.finish(kind, 1, f"the tests failed with status {status}, {counted}", log, seconds)
        if executed == 0:
            # A filter that selects nothing, or only tests that skip, passes xcodebuild and establishes nothing.
            return self.finish(kind, 1, f"no tests ran ({counted})", log, seconds)
        unmeasured = [*console, *from_bundle]
        # The wrapper's "Executed N tests" is what a lane counts: the device's own totals are in its log.
        self.finish(kind, lane_results.UNMEASURED if unmeasured else 0, counted, log, seconds)
        if unmeasured: self.say(*unmeasured)

    def listed(self, device, xctestrun):
        """(enabled test ids, partial units) for the run's selection, listed once, on the first member to ask.

        The shards start as soon as the listing is written: the xcodebuild that wrote it can outlive it by
        a minute and a half while its leftover programs are stopped (EPUBLib, 2026-11-05, 95 s), and is
        waited for at the run's end (listing_done). It has a derived data folder and result bundle of its
        own, removed then, as each test run has (vocaro-engineering#158)."""
        with self.listing_lock:
            if self.listing is not None: return self.listing
            path, log = self.logs / "enumerated.json", self.logs / "enumerate.log"
            scratch, bundle = self.logs / "enumerate.derived", self.logs / "enumerate.xcresult"
            for stale in (scratch, bundle): shutil.rmtree(stale, ignore_errors=True)
            path.unlink(missing_ok=True)
            command = ["xcodebuild", "test-without-building", "-xctestrun", str(xctestrun),
                       "-destination", f"platform=iOS Simulator,id={device.udid}", "-destination-timeout", "600",
                       "-enumerate-tests", "-test-enumeration-style", "flat", "-test-enumeration-format", "json",
                       "-test-enumeration-output-path", str(path), "-derivedDataPath", str(scratch),
                       "-resultBundlePath", str(bundle), *[a for a in self.args.passed if selecting(a)]]
            ended = []
            def enumerate_tests():
                try: ended.append(processes.run(command, cwd=Path.cwd(), log_path=log, timeout=self.args.test_timeout))
                except Exception as error: ended.append(error)
                finally:
                    for folder in (scratch, bundle): shutil.rmtree(folder, ignore_errors=True)
            self.listing_thread = threading.Thread(target=enumerate_tests, daemon=True)
            self.listing_thread.start()
            listing = None
            while listing is None:
                try: listing = enumerated(json.loads(path.read_text()))
                except (OSError, ValueError):
                    if not self.listing_thread.is_alive():
                        # Ended: one last read, in case it wrote the file as it ended.
                        try: listing = enumerated(json.loads(path.read_text())) if ended == [0] else ([], set())
                        except (OSError, ValueError): listing = ([], set())
                    else: time.sleep(0.5)
            self.listing = listing
            self.say(f"LISTED {len(self.listing[0])} tests to share among {self.args.shards} members of each device; "
                     f"log: {log}")
            return self.listing

    def listing_done(self):
        """Wait for the listing's xcodebuild, which the shards did not wait for, before the members go back."""
        thread = getattr(self, "listing_thread", None)
        if thread is not None: thread.join()

    def shard(self, kind, number, device, built):
        """Run one member's share of a sharded device's tests; its outcome goes to self.shards[kind]."""
        label = f"{kind}-{number + 1}"
        record = lambda result: self.shards.setdefault(kind, {}).__setitem__(number, result)
        if not self.boot(label, device):
            return record((self.results.pop(label), 0, 0, [], f"{label} did not boot"))
        xctestrun = built.result()
        if xctestrun is None: return record((1, 0, 0, [], "not tested: the build failed"))
        tests, partial = self.listed(device, xctestrun)
        if not tests: return record((1, 0, 0, [], "no tests were listed"))
        try: seconds = json.loads((self.logs / SHARD_TIMINGS).read_text())
        except (OSError, ValueError): seconds = {}
        shares = packed(tests, partial, seconds, self.args.shards)
        if not shares[number]: return record((0, 0, 0, [], None))
        record(self.test(label, device, xctestrun, shares[number]))

    def shards_finished(self, kind):
        results = [self.shards.get(kind, {}).get(number, (1, 0, 0, [], "no result")) for number in range(self.args.shards)]
        executed, skipped = sum(r[1] for r in results), sum(r[2] for r in results)
        failed = sum((self.tallies.get(f"{kind}-{number + 1}") or {}).get("failed", 0) for number in range(self.args.shards))
        unmeasured = [line for r in results for line in r[3]]
        problems = [r[4] for r in results if r[4]]
        counted = f"Executed {executed} tests" + (f", {skipped} skipped" if skipped else "") + \
            (f", {failed} failed" if failed else "") + f" in {self.args.shards} shards"
        status = combined([r[0] for r in results])
        if status == 0 and unmeasured: status = lane_results.UNMEASURED
        self.finish(kind, status, counted + (f"; {'; '.join(problems)}" if problems else ""))
        if unmeasured: self.say(*unmeasured)

    def keep_timings(self):
        if not self.seconds: return
        path = self.logs / SHARD_TIMINGS
        try: kept = json.loads(path.read_text())
        except (OSError, ValueError): kept = {}
        path.write_text(json.dumps({**kept, **self.seconds}, indent=1, sort_keys=True) + "\n")

    def device(self, kind, device, built):
        if not self.boot(kind, device): return
        xctestrun = built.result()
        if xctestrun is None:
            return self.finish(kind, 1, "not tested: the build failed")
        with self.heard(kind, device) if self.args.voiceover else contextlib.nullcontext():
            self.test(kind, device, xctestrun)


def bundle_json(bundle, what, *options, timeout=300):
    """Read one xcresulttool test-results view, or None when unavailable."""
    try:
        found = subprocess.run(["xcrun", "xcresulttool", "get", "test-results", what, "--path", str(bundle), *options],
                               capture_output=True, text=True, timeout=timeout, check=True)
        return json.loads(found.stdout)
    except (OSError, ValueError, subprocess.SubprocessError): return None


def bundle_record(bundle):
    """(summary, tests) as xcresulttool prints a result bundle's, or None when it cannot read them."""
    summary = bundle_json(bundle, "summary")
    if summary is None: return None
    tests = bundle_json(bundle, "tests")
    return (summary, tests) if tests is not None else None


def bundle_results(bundle, kind, record=None):
    """(executed, skipped, unmeasured lines) as a result bundle records them, or None when xcresulttool
    cannot read it. Once parallel testing is on, xcodebuild's console prints each test's outcome but
    no totals and no skip reason (Xcode 27.2, 2026-10-31), so the bundle is what counts, either way.
    Each skip whose message begins "Test skipped - Unmeasured:" is a line lane_results.UNMEASURED_TEST
    reads, naming the device and the test. record is the bundle's (bundle_record) when read already."""
    record = record if record is not None else bundle_record(bundle)
    if record is None: return None
    summary, tests = record
    unmeasured = []
    def walk(node, test=None):
        if node.get("nodeType") == "Test Case": test = node.get("nodeIdentifier") or node.get("name")
        if node.get("nodeType") == "Skip Message" and UNMEASURED_SKIP in node.get("name", ""):
            unmeasured.append(f"{kind}: -[{test}] : {node['name'][node['name'].index('Test skipped'):]}")
        for child in node.get("children", []): walk(child, test)
    for node in tests.get("testNodes", []): walk(node)
    try: total, skipped = int(summary["totalTestCount"]), int(summary["skippedTests"])
    except (KeyError, TypeError, ValueError): return None
    return total - skipped, skipped, list(dict.fromkeys(unmeasured))


# The file in a derived data or logs folder whose flock a run holds from before its build until its last
# test ends.
FOLDER_LOCK = ".eng-simulator-tests.lock"


@contextlib.contextmanager
def folders_held(folders, say):
    """Hold each folder's lock for the run, waiting while another run holds it.

    Simulators test from the build's products in place, and a build can replace a test bundle's embedded
    resource bundle whole, so a build into derived data that another run is testing from can take files
    from under its tests (vocaro/EPUBLib#872); a logs folder's files are each run's own. The locks are taken in
    path order, so that two runs never each hold one the other waits for. An flock ends with the process
    that holds it, however it ends; the wait is bounded by the run's own limit."""
    descriptors = []
    try:
        for folder in sorted({Path(folder).resolve() for folder in folders}):
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / FOLDER_LOCK
            descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
            descriptors.append(descriptor)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                holder = path.read_text(errors="replace").strip() or "it has not said which"
                say(f"WAIT: another qk simulator-tests run holds {folder} ({holder}); waiting for it to end")
                waited = time.monotonic()
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                say(f"HELD {folder} after waiting {round(time.monotonic() - waited)}s")
            os.ftruncate(descriptor, 0)
            os.pwrite(descriptor, f"PID {os.getpid()} since {time.strftime('%Y-%m-%d %H:%M:%S')}\n".encode(), 0)
        yield
    finally:
        for descriptor in descriptors: os.close(descriptor)


@contextlib.contextmanager
def taken(pool, run, kind, runtime, purpose, keep_state=False, erase=False, label=None):
    """A member of the kind for this run, shut down and released however the run ends. label names the run's result,
    a series' run of the kind; by default the kind."""
    lease = pool.take(qualified(DEVICE_TYPE, kind), qualified(RUNTIME, runtime), purpose=purpose,
                      keep_state=keep_state, erase=erase)
    try:
        yield lease
    finally:
        status = run.results.get(label or kind)
        pool.release(lease, outcome=f"queuekeeper simulator-tests: {outcome(status) if status is not None else 'stopped'}")


# What a run's console says of its failures when no result bundle does.
FAILURES = re.compile(r"Executed \d+ tests?, with (?:\d+ tests? skipped and )?(\d+) failures?")
# Each run's record and the series' summary, beside its logs and, under a lane, in results/simulator-tests/.
SERIES_RECORD = "series.json"
SERIES_OUTCOMES = ("passed", "failed", "skipped", "unmeasured")


def failed_tests(record, text):
    """The tests a run failed, by its result bundle (bundle_record) or else its console: the largest XCTest count it
    prints and each Swift Testing test it names failed, which no XCTest total counts. A run of Swift Testing tests
    alone still prints an XCTest total, of 0 tests with 0 failures, for its bundle (vocaro/queuekeeper#81)."""
    if record is not None:
        try: return int(record[0]["failedTests"])
        except (KeyError, TypeError, ValueError): pass
    return max((int(found) for found in FAILURES.findall(text)), default=0) + \
        len(set(lane_report.SWIFT_TESTING_FAILED.findall(text)))


def tally(status, executed, skipped, unmeasured, failed):
    """A run's tests by outcome, or None when a failed run's counts name no failure, as a crash's do: then they say
    nothing. Skipped excludes the unmeasured, which are skips of their own; an unmeasured test not among the skips
    came out of the executed count."""
    if status != 0 and failed == 0: return None
    among_skipped = min(unmeasured, skipped)
    return {"passed": max(0, executed - failed - (unmeasured - among_skipped)), "failed": failed,
            "skipped": skipped - among_skipped, "unmeasured": unmeasured}


def run_outcome(status, tests):
    """A run's outcome: unmeasured when it has no status, as a run that did not happen; skipped when every test it
    ran skipped, which still fails the series; never passed without a status of 0."""
    if status == 0: return "passed"
    if status is None or status == lane_results.UNMEASURED: return "unmeasured"
    if tests is not None and tests["skipped"] and not (tests["passed"] or tests["failed"] or tests["unmeasured"]):
        return "skipped"
    return "failed"


def reset_of(lease):
    """How the take left a member: copied from its template, cloned from it new, erased, created new or kept."""
    return ("kept" if lease.kept else "template" if lease.reset else "template clone" if lease.cloned
            else "created" if lease.created else "erased")


def load_words(loads):
    """A run's one-minute load, start and end; unread when the host gave none, so that no number stands in for it."""
    loads = loads or {}
    word = lambda value: f"{value:g}" if value is not None else "unread"
    return f"load {word(loads.get('start'))} at start, {word(loads.get('end'))} at end"


def series_summary(args, kinds, runs):
    """The series' totals over its runs, each run's outcome counted once and each run's tests beside it."""
    outcomes = {name: sum(run["outcome"] == name for run in runs) for name in SERIES_OUTCOMES}
    known = [run["tests"] for run in runs if run["tests"] is not None]
    return {"asked": args.repeat, "devices": kinds, "runsAsked": args.repeat * len(kinds), "reset": asked_reset(args),
            "runs": outcomes, "tests": {name: sum(tests[name] for tests in known) for name in SERIES_OUTCOMES},
            "runsWithoutTestCounts": len(runs) - len(known)}


def asked_reset(args):
    return "erase" if args.erase else "template" if args.template else "keep" if args.keep_state else "the pool's own"


def series_line(summary):
    """The series' summary line, which qk run reads (lane_results.SERIES) and a lane's log keeps."""
    runs, tests = summary["runs"], summary["tests"]
    uncounted = summary["runsWithoutTestCounts"]
    return (f"Repeat series: asked {summary['asked']} repeat{'' if summary['asked'] == 1 else 's'} "
            f"of {', '.join(summary['devices'])} ({summary['runsAsked']} runs); runs: {runs['passed']} passed, "
            f"{runs['failed']} failed, {runs['skipped']} skipped, {runs['unmeasured']} unmeasured; tests: "
            f"{tests['passed']} passed, {tests['failed']} failed, {tests['skipped']} skipped, {tests['unmeasured']} "
            f"unmeasured" + (f"; {uncounted} run{'' if uncounted == 1 else 's'} gave no test counts" if uncounted else ""))


def keep_series(run, record):
    """Write the series' record beside its logs and, under a lane, in its results/, and say where."""
    text = json.dumps(record, indent=1) + "\n"
    folders = [run.logs]
    if os.environ.get(RESULTS_VARIABLE): folders.append(Path(os.environ[RESULTS_VARIABLE]) / KEPT)
    for folder in folders:
        try:
            folder.mkdir(parents=True, exist_ok=True)
            (folder / SERIES_RECORD).write_text(text)
            run.say(f"SERIES RECORD {folder / SERIES_RECORD}")
        except OSError as error:
            print(f"queuekeeper simulator-tests: the series record was not kept in {folder}: {error}", file=sys.stderr, flush=True)


def series(run, pool, args, kinds):
    """Run the tests args.repeat times, every device of each round on a member just taken from the pool, the tests
    built once, on the first round's destination. Each run's outcome stands apart, a run that could not take its
    member, or that the series stopped before, counts unmeasured, and a failed build ends the series there. Returns
    the series' status, and reports it however the series ended."""
    purpose = f"qk simulator-tests {args.scheme}, repeat series"
    asked = [(number, kind, f"{kind}-run{number}") for number in range(1, args.repeat + 1) for kind in kinds]
    members, built = {}, None
    def take_and_test(number):
        nonlocal built
        with contextlib.ExitStack() as stack:
            leases = []
            for kind in kinds:
                label = f"{kind}-run{number}"
                lease = stack.enter_context(taken(pool, run, kind, args.runtime, purpose, args.keep_state, args.erase, label))
                members[label] = lease
                leases.append((label, lease))
            if built is None: built = builder.submit(run.build, f"platform=iOS Simulator,id={leases[0][1].udid}")
            with ThreadPoolExecutor(max_workers=args.jobs or len(kinds)) as testers:
                futures = []
                for label, lease in leases:
                    how = reset_of(lease)
                    if args.template and how in ("erased", "created") or args.erase and how not in ("erased", "created"):
                        run.finish(label, lane_results.UNMEASURED, f"{asked_reset(args)} was asked for and the member was "
                                   f"{how}, so no test ran")
                    else: futures.append(testers.submit(run.device, label, lease, built))
            errors = [future.exception() for future in futures if future.exception() is not None]
            if errors: raise errors[0]
    def report():
        runs = []
        for number, kind, label in asked:
            lease, status = members.get(label), run.results.get(label)
            tests = run.tallies.get(label)
            result = {"run": number, "device": kind, "label": label, "member": lease.key if lease else None,
                      "udid": lease.udid if lease else None, "reset": reset_of(lease) if lease else None,
                      "outcome": run_outcome(status, tests), "status": status, "seconds": run.durations.get(label),
                      "load": run.loads.get(label), "tests": tests,
                      "detail": run.details.get(label) or ("this run did not happen" if status is None else None)}
            runs.append(result)
            counts = ", ".join(f"{tests[name]} {name}" for name in SERIES_OUTCOMES) if tests is not None else "no test counts"
            where = f"{result['member']} ({result['udid']}), {result['reset']}" if lease else "no member"
            run.say(f"SERIES {kind} run {number} of {args.repeat}: {result['outcome'].upper()} on {where}; {counts}; "
                    f"{load_words(result['load'])}" + (f"; {result['seconds']}s" if result["seconds"] is not None else "")
                    + (f"; {result['detail']}" if result["detail"] else ""))
        summary = series_summary(args, kinds, runs)
        keep_series(run, {**summary, "host": os.uname().nodename, "scheme": args.scheme, "testArguments": args.passed,
                          "eachRun": runs})
        run.say(series_line(summary))
    try:
        with ThreadPoolExecutor(max_workers=1) as builder:
            for number in range(1, args.repeat + 1):
                if built is not None and built.done() and built.exception() is None and built.result() is None:
                    run.say(f"SERIES: the build failed, so runs {number} to {args.repeat} did not happen")
                    break
                try: take_and_test(number)
                except (OSError, ValueError, RuntimeError, TimeoutError) as error:
                    for kind in kinds:
                        if f"{kind}-run{number}" not in run.results:
                            run.results[f"{kind}-run{number}"] = lane_results.UNMEASURED
                            run.details[f"{kind}-run{number}"] = f"the run could not start or finish: {error}"
    finally:
        report()
    return combined([run.results.get(label, lane_results.UNMEASURED) for _, _, label in asked])


def main(argv, project=None):
    args = parse(argv)
    name = args.pool or simulator_pool.running(project)
    if args.directory is not None: os.chdir(args.directory)
    if not name:
        print("queuekeeper simulator-tests: name the simulator pool with --pool, or in queuekeeper.json's simulatorPool",
              file=sys.stderr)
        return 2
    run = Run(args)
    run.logs.mkdir(parents=True, exist_ok=True)
    # Names only: a value can be private, and the log travels.
    if args.forward_env:
        names = sorted(forwarded(args.forward_env, os.environ))
        run.say(f"FORWARD {', '.join(args.forward_env)}: {', '.join(names) if names else 'nothing to forward'}")
    kinds = args.device[:1] if args.build_only else args.device
    started = time.monotonic()
    pool = simulator_pool.Pool(name, owner=simulator_pool.default_owner() or f"simulator-tests-{uuid.uuid4()}",
                               log=run.say, alone=True)
    try:
        with processes.forwarding_signals(), contextlib.ExitStack() as stack:
            # Before any device is taken, so that a run that waits holds none.
            stack.enter_context(folders_held([args.derived_data, run.logs], run.say))
            if args.repeat is not None:
                if args.set_aside_shards:
                    run.say(f"SERIES: --shards {args.set_aside_shards} set aside: each run is one test process")
                status = series(run, pool, args, kinds)
                print(f"Simulator tests (wall-clock {round(time.monotonic() - started)}s), series of "
                      f"{args.repeat}: {outcome(status)}", flush=True)
                return status
            devices = []
            for kind in kinds:
                purpose = f"qk simulator-tests {args.scheme}" + (", build only" if args.build_only else "")
                for number in range(1 if args.build_only else args.shards or 1):
                    device = stack.enter_context(taken(pool, run, kind, args.runtime, purpose,
                                                       args.keep_state or args.build_only, args.erase))
                    devices.append((kind, number, device))
            if args.build_only:
                # The member names the destination alone: nothing boots, installs or runs on it.
                xctestrun = run.build(f"platform=iOS Simulator,id={devices[0][2].udid}")
                run.results[kinds[0]] = 0 if xctestrun is not None else 1
                print(f"Simulator build (wall-clock {round(time.monotonic() - started)}s): "
                      f"{'BUILT' if xctestrun is not None else 'FAIL'}, no test run", flush=True)
                return run.results[kinds[0]]
            # The build runs while every device boots; tests wait for both.
            with ThreadPoolExecutor(max_workers=1) as builder, \
                    ThreadPoolExecutor(max_workers=(args.jobs or len(kinds)) * (args.shards or 1)) as testers:
                built = builder.submit(run.build, f"platform=iOS Simulator,id={devices[0][2].udid}")
                futures = [testers.submit(run.shard, kind, number, device, built) if args.shards else
                           testers.submit(run.device, kind, device, built) for kind, number, device in devices]
            if args.shards:
                run.listing_done()
                for kind in kinds: run.shards_finished(kind)
                run.keep_timings()
            errors = [future.exception() for future in [built, *futures] if future.exception() is not None]
            if errors:
                uncertain = [error for error in errors if not isinstance(error, processes.Interrupted)]
                raise uncertain[0] if uncertain else errors[0]
    except (OSError, ValueError, RuntimeError, TimeoutError) as error:
        print(f"queuekeeper simulator-tests: {error}", file=sys.stderr)
        return 1
    status = combined([run.results.get(kind, 1) for kind in kinds])
    print(f"Simulator tests (wall-clock {round(time.monotonic() - started)}s, "
          f"{args.jobs or len(kinds)} at a time): " + ", ".join(f"{kind} {outcome(run.results.get(kind, 1))}"
                                                                 for kind in kinds), flush=True)
    return status
