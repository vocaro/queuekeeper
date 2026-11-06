"""Build a scheme's tests once and run them on several members of the project's simulator pool side by side.

    eng simulator-tests --scheme SCHEME --runtime iOS-27-0 --device iPhone-17-Pro \\
        --device iPad-Air-13-inch-M4 [--pool NAME] [--build-jobs 4] [--forward-env PREFIX] \\
        [-- TEST-ARGUMENTS such as -only-testing:T/C]

Each device is a member of the pool, `POOL LABEL N`: a free one of its kind is reserved in the
host's device record and reset from its kind's warmed template (erased with --erase, or when no settled
template can be copied), or, with --keep-state, kept as earlier runs left it, or, when none is
free, the next is cloned from that template, or created, and kept in the pool, after members unused
for 14 days are pruned (guides/simulator-testing.md, Reserved simulator pool).
With --app-accessibility each member's app processes get UIKit's and SwiftUI's own accessibility, for tests
hosted in an app that read system views (guides/simulator-testing.md, App accessibility). With --xcodegen SPEC
the Xcode project is generated from SPEC before the build. Every device boots while the tests build, and is shut down and released however the run ends. The
build is one `xcodebuild build-for-testing` for the first device's destination; each device
then runs `xcodebuild test-without-building` on the same .xctestrun, with its own log, result
bundle and derived data folder, removed when its tests end. Each device's outcome stands apart: a boot that reached its limit leaves that
device unmeasured, and the run fails when any device failed, and is otherwise unmeasured when
any device was. guides/parallel-tests.md says how to bound it. A build that fails on a module older
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
"""
import argparse
import contextlib
from concurrent.futures import ThreadPoolExecutor
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
import uuid

from .. import evidence, processes
from ..parallel import TAIL, combined, outcome
from . import pool as simulator_pool
from .pool import DEVICE_TYPE, RUNTIME, qualified

# Seconds simctl boot itself may take; bootstatus has --boot-timeout.
BOOT_COMMAND = 120
# Seconds one test may run before XCTest fails it, records a spindump and goes on to the next, unless
# --test-allowance or the test's own executionTimeAllowance says otherwise: XCTest's own default once
# -test-timeouts-enabled is on, which it rounds up to whole minutes.
TEST_ALLOWANCE = 600
# What clang says of an explicit module built before a dependency changed a header, and the folder of
# derived data whose removal let the next build pass (vocaro/vocaro-engineering#85).
STALE_MODULE = "has been modified since the module file"
EXPLICIT_MODULES = "Build/Intermediates.noindex/SwiftExplicitPrecompiledModules"


def parse(argv):
    split = argv.index("--") if "--" in argv else len(argv)
    parser = argparse.ArgumentParser(prog="eng simulator-tests", description=__doc__.split("\n\n")[0])
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
                       "the first boot's migration (guides/simulator-testing.md, Reserved simulator pool)")
    fresh.add_argument("--erase", action="store_true", help="erase each member in place of the reset from its "
                       "kind's warmed template, for tests that want pristine data (guides/simulator-testing.md, "
                       "Warmed templates)")
    parser.add_argument("--app-accessibility", action="store_true", help="switch UIKit's and SwiftUI's own "
                        "accessibility on in each member's app processes once it has booted, for tests hosted in an "
                        "app that read system views; a device whose setting does not read on fails "
                        "(guides/simulator-testing.md, App accessibility)")
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
                        "project's simulatorPool in engineering.json)")
    parser.add_argument("--setting", action="append", default=[], metavar="NAME=VALUE",
                        help="a build setting for the build, after CODE_SIGNING_ALLOWED=NO")
    parser.add_argument("--forward-env", action="append", default=[], metavar="PREFIX",
                        help="give the test process each variable starting with PREFIX: it goes to xcodebuild as "
                        "TEST_RUNNER_ + its name, unless that is already set; repeat for each prefix")
    args, unknown = parser.parse_known_args(argv[:split])
    if unknown:
        # eng run LANE -- ARGUMENTS appends them to the lane's command, which reach the tests only
        # when that command ends with --.
        parser.error(f"unrecognized arguments: {' '.join(unknown)}. Test arguments such as -only-testing go after "
                     "--, so a lane whose command runs simulator-tests ends with \"--\"")
    args.passed = argv[split + 1:]
    for name in ("jobs", "build_jobs", "workers", "shards", "boot_timeout", "test_timeout", "test_allowance"):
        if getattr(args, name) is not None and getattr(args, name) < 1:
            parser.error(f"--{name.replace('_', '-')} needs at least 1")
    if len(set(args.device)) != len(args.device): parser.error("name each device type once")
    if args.xcodegen is not None and not args.xcode_project:
        parser.error("--xcodegen generates an Xcode project: name it with --xcode-project")
    if not all(args.forward_env): parser.error("--forward-env needs a prefix: an empty one would forward everything")
    return args


def forwarded(prefixes, environment):
    """The TEST_RUNNER_ variables that give the test process each variable starting with a prefix. A test
    process in a simulator sees only what xcodebuild is given that way (guides/simulator-testing.md, Test
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


def suite_seconds(bundle):
    """{Bundle/Suite: seconds} over a result bundle's test cases, or {} when xcresulttool cannot read it."""
    try:
        found = subprocess.run(["xcrun", "xcresulttool", "get", "test-results", "tests", "--path", str(bundle)],
                               capture_output=True, text=True, timeout=300, check=True)
        tests = json.loads(found.stdout)
    except (OSError, ValueError, subprocess.SubprocessError): return {}
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


class Run:
    def __init__(self, args):
        self.args, self.lock, self.results = args, threading.Lock(), {}
        self.logs = args.logs.resolve()
        # The listing has a lock of its own: say() takes self.lock, and the listing says what it found.
        self.shards, self.listing, self.seconds, self.listing_lock = {}, None, {}, threading.Lock()

    def say(self, *lines):
        with self.lock: print(*lines, sep="\n", flush=True)

    def finish(self, kind, status, detail, log=None, seconds=None):
        self.results[kind] = status
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
            self.finish(kind, evidence.UNMEASURED, f"the boot exceeded its limit, so no test ran", log)
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
        return True

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
        if status != 0 and STALE_MODULE in log.read_text(errors="replace"):
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
        try: return self.run_test(kind, device, xctestrun, log, bundle, scratch, selection)
        finally: shutil.rmtree(scratch, ignore_errors=True)

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
            if selection is not None: return evidence.UNMEASURED, 0, 0, [], f"{kind}'s tests exceeded {args.test_timeout}s"
            return self.finish(kind, evidence.UNMEASURED, f"the tests exceeded {args.test_timeout}s", log)
        seconds = round(time.monotonic() - started)
        text = log.read_text(errors="replace")
        # Tests that skipped ran nothing, so they are reported beside the count, not in it. The result
        # bundle's record counts them; the console, with no bundle to read, is the fallback.
        recorded = bundle_results(bundle, kind)
        if recorded is not None: executed, skipped, from_bundle = recorded
        else: executed, skipped, from_bundle = evidence.executed_tests(text) or 0, evidence.skipped_tests(text), []
        counted = f"Executed {executed} tests" + (f", {skipped} skipped" if skipped else "")
        if selection is not None:
            console = evidence.unmeasured_tests(text)
            if recorded is not None: console = [line for line in console if "Test skipped - Unmeasured:" not in line]
            with self.lock: self.seconds.update(suite_seconds(bundle))
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
        console = evidence.unmeasured_tests(text)
        # The bundle names each skipped XCTest test once, so the console's lines for them would repeat it.
        if recorded is not None: console = [line for line in console if "Test skipped - Unmeasured:" not in line]
        unmeasured = [*console, *from_bundle]
        # The wrapper's "Executed N tests" is what a lane counts: the device's own totals are in its log.
        self.finish(kind, evidence.UNMEASURED if unmeasured else 0, counted, log, seconds)
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
        unmeasured = [line for r in results for line in r[3]]
        problems = [r[4] for r in results if r[4]]
        counted = f"Executed {executed} tests" + (f", {skipped} skipped" if skipped else "") + \
            f" in {self.args.shards} shards"
        status = combined([r[0] for r in results])
        if status == 0 and unmeasured: status = evidence.UNMEASURED
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
        self.test(kind, device, xctestrun)


def bundle_results(bundle, kind):
    """(executed, skipped, unmeasured lines) as a result bundle records them, or None when xcresulttool
    cannot read it. Once parallel testing is on, xcodebuild's console prints each test's outcome but
    no totals and no skip reason (Xcode 27.2, 2026-10-31), so the bundle is what counts, either way.
    Each skip whose message begins "Test skipped - Unmeasured:" is a line evidence.UNMEASURED_TEST
    reads, naming the device and the test."""
    def read(what):
        found = subprocess.run(["xcrun", "xcresulttool", "get", "test-results", what, "--path", str(bundle)],
                               capture_output=True, text=True, timeout=300, check=True)
        return json.loads(found.stdout)
    try: summary, tests = read("summary"), read("tests")
    except (OSError, ValueError, subprocess.SubprocessError): return None
    unmeasured = []
    def walk(node, test=None):
        if node.get("nodeType") == "Test Case": test = node.get("nodeIdentifier") or node.get("name")
        if node.get("nodeType") == "Skip Message" and "Test skipped - Unmeasured:" in node.get("name", ""):
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
                say(f"WAIT: another eng simulator-tests run holds {folder} ({holder}); waiting for it to end")
                waited = time.monotonic()
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                say(f"HELD {folder} after waiting {round(time.monotonic() - waited)}s")
            os.ftruncate(descriptor, 0)
            os.pwrite(descriptor, f"PID {os.getpid()} since {time.strftime('%Y-%m-%d %H:%M:%S')}\n".encode(), 0)
        yield
    finally:
        for descriptor in descriptors: os.close(descriptor)


@contextlib.contextmanager
def taken(pool, run, kind, runtime, purpose, keep_state=False, erase=False):
    """A member of the kind for this run, shut down and released however the run ends."""
    lease = pool.take(qualified(DEVICE_TYPE, kind), qualified(RUNTIME, runtime), purpose=purpose,
                      keep_state=keep_state, erase=erase)
    try:
        yield lease
    finally:
        status = run.results.get(kind)
        pool.release(lease, outcome=f"eng simulator-tests: {outcome(status) if status is not None else 'stopped'}")


def main(argv, project=None):
    args = parse(argv)
    # The launcher names its project; a lane runs in its project's root, which may name the pool instead.
    name = args.pool or simulator_pool.configured(project or Path.cwd()) or simulator_pool.configured(Path.cwd())
    if args.directory is not None: os.chdir(args.directory)
    if not name:
        print("eng simulator-tests: name the simulator pool with --pool, or in engineering.json's simulatorPool",
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
            devices = []
            for kind in kinds:
                purpose = f"eng simulator-tests {args.scheme}" + (", build only" if args.build_only else "")
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
        print(f"eng simulator-tests: {error}", file=sys.stderr)
        return 1
    status = combined([run.results.get(kind, 1) for kind in kinds])
    print(f"Simulator tests (wall-clock {round(time.monotonic() - started)}s, "
          f"{args.jobs or len(kinds)} at a time): " + ", ".join(f"{kind} {outcome(run.results.get(kind, 1))}"
                                                                 for kind in kinds), flush=True)
    return status
