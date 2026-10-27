"""Build a scheme's tests once and run them on several members of the project's simulator pool side by side.

    eng simulator-tests --scheme SCHEME --runtime iOS-27-0 --device iPhone-17-Pro \\
        --device iPad-Air-13-inch-M4 [--pool NAME] [--build-jobs 4] [-- TEST-ARGUMENTS such as -only-testing:T/C]

Each device is a member of the pool, `POOL LABEL N`: a free one of its kind is reserved in the
host's device record and erased, or, when none is free, the next is created and kept in the pool,
after members unused for 14 days are pruned (guides/simulator-testing.md, Reserved simulator pool).
Every device boots while the tests build, and is shut down and released however the run ends. The
build is one `xcodebuild build-for-testing` for the first device's destination; each device
then runs `xcodebuild test-without-building` on the same .xctestrun, with its own log and
result bundle. Each device's outcome stands apart: a boot that reached its limit leaves that
device unmeasured, and the run fails when any device failed, and is otherwise unmeasured when
any device was. guides/parallel-tests.md says how to bound it.
"""
import argparse
import contextlib
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import shutil
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
    parser.add_argument("--test-timeout", type=int, help="seconds after which a device's test run is stopped "
                        "and left unmeasured, a backstop for a hang")
    parser.add_argument("--derived-data", type=Path, default=Path(".build/simulator-tests/derived"))
    parser.add_argument("--logs", type=Path, default=Path(".build/simulator-tests/logs"))
    parser.add_argument("--pool", help="the simulator pool, the start of every member's name (default: the "
                        "project's simulatorPool in engineering.json)")
    parser.add_argument("--setting", action="append", default=[], metavar="NAME=VALUE",
                        help="a build setting for the build, after CODE_SIGNING_ALLOWED=NO")
    args, unknown = parser.parse_known_args(argv[:split])
    if unknown:
        # eng run LANE -- ARGUMENTS appends them to the lane's command, which reach the tests only
        # when that command ends with --.
        parser.error(f"unrecognized arguments: {' '.join(unknown)}. Test arguments such as -only-testing go after "
                     "--, so a lane whose command runs simulator-tests ends with \"--\"")
    args.passed = argv[split + 1:]
    for name in ("jobs", "build_jobs", "workers", "boot_timeout", "test_timeout"):
        if getattr(args, name) is not None and getattr(args, name) < 1:
            parser.error(f"--{name.replace('_', '-')} needs at least 1")
    if len(set(args.device)) != len(args.device): parser.error("name each device type once")
    return args


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
        workers = ["-parallel-testing-enabled", "YES", "-parallel-testing-worker-count", str(args.workers)] \
            if args.workers else ["-parallel-testing-enabled", "NO"]
        command = ["xcodebuild", "test-without-building", "-xctestrun", str(xctestrun),
                   "-destination", f"platform=iOS Simulator,id={device.udid}", "-destination-timeout", "600",
                   *workers, "-collect-test-diagnostics", "never", "-resultBundlePath", str(bundle), *args.passed]
        self.say(f"TEST {kind} ({device.udid}); log: {log}")
        started = time.monotonic()
        try:
            status = processes.run(command, cwd=Path.cwd(), log_path=log, timeout=args.test_timeout)
        except processes.TimedOut:
            return self.finish(kind, evidence.UNMEASURED, f"the tests exceeded {args.test_timeout}s", log)
        seconds = round(time.monotonic() - started)
        text = log.read_text(errors="replace")
        # Tests that skipped ran nothing, so they are reported beside the count, not in it.
        executed, skipped = evidence.executed_tests(text) or 0, evidence.skipped_tests(text)
        counted = f"Executed {executed} tests" + (f", {skipped} skipped" if skipped else "")
        if status != 0:
            return self.finish(kind, 1, f"the tests failed with status {status}, {counted}", log, seconds)
        if executed == 0:
            # A filter that selects nothing, or only tests that skip, passes xcodebuild and establishes nothing.
            return self.finish(kind, 1, f"no tests ran ({counted})", log, seconds)
        unmeasured = evidence.unmeasured_tests(text)
        # The wrapper's "Executed N tests" is what a lane counts: the device's own totals are in its log.
        self.finish(kind, evidence.UNMEASURED if unmeasured else 0, counted, log, seconds)
        if unmeasured: self.say(*unmeasured)

    def device(self, kind, device, built):
        if not self.boot(kind, device): return
        xctestrun = built.result()
        if xctestrun is None:
            return self.finish(kind, 1, "not tested: the build failed")
        self.test(kind, device, xctestrun)


@contextlib.contextmanager
def taken(pool, run, kind, runtime, purpose):
    """A member of the kind for this run, shut down and released however the run ends."""
    lease = pool.take(qualified(DEVICE_TYPE, kind), qualified(RUNTIME, runtime), purpose=purpose)
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
    kinds = args.device
    started = time.monotonic()
    pool = simulator_pool.Pool(name, owner=simulator_pool.default_owner() or f"simulator-tests-{uuid.uuid4()}",
                               log=run.say)
    try:
        with processes.forwarding_signals(), contextlib.ExitStack() as stack:
            devices = []
            for kind in kinds:
                device = stack.enter_context(taken(pool, run, kind, args.runtime, f"eng simulator-tests {args.scheme}"))
                devices.append((kind, device))
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
