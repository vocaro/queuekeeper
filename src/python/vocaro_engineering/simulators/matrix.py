"""Build a scheme's tests once and run them on several members of the project's simulator pool side by side.

    eng simulator-tests --scheme SCHEME --runtime iOS-27-0 --device iPhone-17-Pro \\
        --device iPad-Air-13-inch-M4 [--pool NAME] [--build-jobs 4] [--forward-env PREFIX] \\
        [-- TEST-ARGUMENTS such as -only-testing:T/C]

Each device is a member of the pool, `POOL LABEL N`: a free one of its kind is reserved in the
host's device record and reset from its kind's warmed template (erased with --erase, or when no settled
template can be copied), or, with --keep-state, kept as earlier runs left it, or, when none is
free, the next is cloned from that template, or created, and kept in the pool, after members unused
for 14 days are pruned (guides/simulator-testing.md, Reserved simulator pool).
Every device boots while the tests build, and is shut down and released however the run ends. The
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
    parser.add_argument("--device", action="append", required=True, metavar="TYPE",
                        help="a simulator device type, such as iPhone-17-Pro; repeat for each device")
    parser.add_argument("--runtime", required=True, help="a simulator runtime, such as iOS-27-0")
    parser.add_argument("--jobs", type=int, help="devices tested at a time (default: every device)")
    parser.add_argument("--build-jobs", type=int, default=4, help="xcodebuild -jobs for the build (default 4)")
    parser.add_argument("--workers", type=int, help="xcodebuild's parallel test workers on each device, each a "
                        "clone of it; without this each device runs one test process")
    parser.add_argument("--boot-timeout", type=int, default=600, help="seconds a boot may take (default 600)")
    fresh = parser.add_mutually_exclusive_group()
    fresh.add_argument("--keep-state", action="store_true", help="take each member without resetting or erasing it, "
                       "for tests that need no fresh installation: it keeps what earlier runs installed and skips "
                       "the first boot's migration (guides/simulator-testing.md, Reserved simulator pool)")
    fresh.add_argument("--erase", action="store_true", help="erase each member in place of the reset from its "
                       "kind's warmed template, for tests that want pristine data (guides/simulator-testing.md, "
                       "Warmed templates)")
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
    for name in ("jobs", "build_jobs", "workers", "boot_timeout", "test_timeout", "test_allowance"):
        if getattr(args, name) is not None and getattr(args, name) < 1:
            parser.error(f"--{name.replace('_', '-')} needs at least 1")
    if len(set(args.device)) != len(args.device): parser.error("name each device type once")
    if not all(args.forward_env): parser.error("--forward-env needs a prefix: an empty one would forward everything")
    return args


def forwarded(prefixes, environment):
    """The TEST_RUNNER_ variables that give the test process each variable starting with a prefix. A test
    process in a simulator sees only what xcodebuild is given that way (guides/simulator-testing.md, Test
    environment), and a TEST_RUNNER_ variable already set is the caller's and stays."""
    if not prefixes: return {}
    return {f"TEST_RUNNER_{name}": value for name, value in environment.items()
            if name.startswith(tuple(prefixes)) and f"TEST_RUNNER_{name}" not in environment}


class Run:
    def __init__(self, args):
        self.args, self.lock, self.results = args, threading.Lock(), {}
        self.logs = args.logs.resolve()

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
        return True

    def build(self, destination):
        """The .xctestrun the build wrote, or None after reporting why there is none."""
        args, log = self.args, self.logs / "build.log"
        location = (["-project", args.xcode_project] if args.xcode_project else
                    ["-workspace", args.workspace] if args.workspace else [])
        command = ["xcodebuild", "build-for-testing", "-scheme", args.scheme, *location, "-destination", destination,
                   "-derivedDataPath", str(args.derived_data), "-jobs", str(args.build_jobs),
                   "CODE_SIGNING_ALLOWED=NO", *args.setting]
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

    def test(self, kind, device, xctestrun):
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
        try: return self.run_test(kind, device, xctestrun, log, bundle, scratch)
        finally: shutil.rmtree(scratch, ignore_errors=True)

    def run_test(self, kind, device, xctestrun, log, bundle, scratch):
        args = self.args
        workers = ["-parallel-testing-enabled", "YES", "-parallel-testing-worker-count", str(args.workers)] \
            if args.workers else ["-parallel-testing-enabled", "NO"]
        # A hung test fails by itself, named, and the rest still run, well before the run's own backstop.
        allowance = [] if "-test-timeouts-enabled" in args.passed else \
            ["-test-timeouts-enabled", "YES", "-default-test-execution-time-allowance", str(args.test_allowance)]
        command = ["xcodebuild", "test-without-building", "-xctestrun", str(xctestrun),
                   "-destination", f"platform=iOS Simulator,id={device.udid}", "-destination-timeout", "600",
                   *workers, *allowance, "-collect-test-diagnostics", "never", "-resultBundlePath", str(bundle),
                   "-derivedDataPath", str(scratch), *args.passed]
        self.say(f"TEST {kind} ({device.udid}); log: {log}")
        started = time.monotonic()
        try:
            status = processes.run(command, cwd=Path.cwd(), log_path=log, timeout=args.test_timeout,
                                   env={**os.environ, **forwarded(args.forward_env, os.environ)})
        except processes.TimedOut:
            return self.finish(kind, evidence.UNMEASURED, f"the tests exceeded {args.test_timeout}s", log)
        seconds = round(time.monotonic() - started)
        text = log.read_text(errors="replace")
        # Tests that skipped ran nothing, so they are reported beside the count, not in it. The result
        # bundle's record counts them; the console, with no bundle to read, is the fallback.
        recorded = bundle_results(bundle, kind)
        if recorded is not None: executed, skipped, from_bundle = recorded
        else: executed, skipped, from_bundle = evidence.executed_tests(text) or 0, evidence.skipped_tests(text), []
        counted = f"Executed {executed} tests" + (f", {skipped} skipped" if skipped else "")
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
                               log=run.say)
    try:
        with processes.forwarding_signals(), contextlib.ExitStack() as stack:
            # Before any device is taken, so that a run that waits holds none.
            stack.enter_context(folders_held([args.derived_data, run.logs], run.say))
            devices = []
            for kind in kinds:
                purpose = f"eng simulator-tests {args.scheme}" + (", build only" if args.build_only else "")
                device = stack.enter_context(taken(pool, run, kind, args.runtime, purpose,
                                                   args.keep_state or args.build_only, args.erase))
                devices.append((kind, device))
            if args.build_only:
                # The member names the destination alone: nothing boots, installs or runs on it.
                xctestrun = run.build(f"platform=iOS Simulator,id={devices[0][1].udid}")
                run.results[kinds[0]] = 0 if xctestrun is not None else 1
                print(f"Simulator build (wall-clock {round(time.monotonic() - started)}s): "
                      f"{'BUILT' if xctestrun is not None else 'FAIL'}, no test run", flush=True)
                return run.results[kinds[0]]
            # The build runs while every device boots; tests wait for both.
            with ThreadPoolExecutor(max_workers=1) as builder, \
                    ThreadPoolExecutor(max_workers=args.jobs or len(kinds)) as testers:
                built = builder.submit(run.build, f"platform=iOS Simulator,id={devices[0][1].udid}")
                futures = [testers.submit(run.device, kind, device, built) for kind, device in devices]
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
