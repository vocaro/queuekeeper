"""Build an Android app's instrumented tests once and run them on several members of the project's emulator pool
side by side: `qk emulator-tests --device pixel_8 --device pixel_tablet`.

Gradle's connectedAndroidTest runs on whichever devices adb lists, another agent's emulator among them, and builds
again for each invocation. This builds the app and its test APK once, with the project's Gradle wrapper, then gives
each device kind, or each shard of one, an emulator of its own from the pool (emulators/pool.py), installs both APKs
there and runs `am instrument -r -w`, reading each test's outcome as it streams (instrumentation.py).

- `--shards N` spreads one kind's test classes over N emulators, packed by how long each class took in the last run
  (queuekeeper_mobile.shards). The classes are listed once, on the first emulator, with the runner's `log` option,
  which lists the tests without running them.
- `--repeat N` runs the whole set N times on fresh emulators, from the one build.
- Each kind's outcome is reported separately, on a line lane_results counts: `Executed N tests, with K tests skipped`.
  The run fails when any kind failed, and is unmeasured when any was: a boot past its limit, an instrumentation that
  died with a test running, or a run that ran no test measures nothing.

Each kind's tests and their outcomes and seconds are kept in the lane's results/, as emulator-tests/KIND.json, beside
the log. A lane reports the per-test outcomes when it finishes, after bringing its results back from another Mac.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time

from queuekeeper import lane_results, processes
from queuekeeper.parallel import RESULTS_VARIABLE, combined, outcome
from queuekeeper_mobile import devices as device_record, shards
from . import adb, android_platform, instrumentation, sdk, settings  # noqa: F401 -- switches the platform on
from .emulators import pool as emulator_pool

KEPT = "emulator-tests"
KEPT_BY = "qk emulator-tests"
TIMINGS = "timings.json"
BUILD_TIMEOUT = 3600
TEST_TIMEOUT = 3600
LISTING_TIMEOUT = 600
INSTRUMENTATION = re.compile(r"instrumentation:(?P<package>[\w.]+)/(?P<runner>[\w.$]+) \(target=(?P<target>[\w.]+)\)")


class Run:
    def __init__(self, args, root):
        self.args, self.root, self.lock = args, root, threading.Lock()
        self.logs = args.logs.resolve()
        self.statuses, self.seconds, self.listing = {}, {}, None
        self.listing_lock = threading.Lock()

    def say(self, *lines):
        with self.lock: print(*lines, sep="\n", flush=True)

    # The build.
    def build(self):
        """(app APK, test APK) the build wrote, or None after saying why there are none."""
        android, args = self.args.android, self.args
        directory = (self.root / android["directory"]).resolve()
        wrapper = directory / "gradlew"
        if not wrapper.exists():
            self.say(f"FAIL build: {directory} has no gradlew; android.directory names the Gradle project's folder")
            return None
        variant = android["variant"]
        capital = variant[:1].upper() + variant[1:]
        module = ":" + android["module"]
        # No daemon: one left running is the run's leftover process, which its supervision stops a minute later.
        command = [str(wrapper), "-p", str(directory), "--no-daemon", f"{module}:assemble{capital}",
                   f"{module}:assemble{capital}AndroidTest", *android["gradleArguments"]]
        log = self.logs / "build.log"
        self.say(f"BUILD {module} {variant} and its instrumented tests; log: {log}")
        started = time.time()
        try:
            status = processes.run(command, cwd=directory, log_path=log, timeout=BUILD_TIMEOUT,
                                   env=sdk.environment())
        except processes.TimedOut:
            status = f"a timeout after {BUILD_TIMEOUT}s"
        if status != 0:
            self.say(f"FAIL build: status {status}; log: {log}",
                     *("    " + line for line in log.read_text(errors="replace").splitlines()[-40:]))
            return None
        # Gradle leaves the APKs of an up-to-date build as they were, so the newest of each are this build's.
        outputs = directory / android["module"].replace(":", "/") / "build/outputs/apk"
        fresh = list(outputs.rglob("*.apk"))
        tests = sorted((path for path in fresh if "androidTest" in path.relative_to(outputs).parts),
                       key=lambda path: path.stat().st_mtime)
        apps = sorted((path for path in fresh if "androidTest" not in path.relative_to(outputs).parts
                       and "unsigned" not in path.name), key=lambda path: path.stat().st_mtime)
        if not tests or not apps:
            self.say(f"FAIL build: it wrote no {'test ' if not tests else ''}APK under {outputs}")
            return None
        self.say(f"BUILT {apps[-1].name} and {tests[-1].name} in {round(time.time() - started)}s")
        return apps[-1], tests[-1]

    # One emulator's part.
    def prepare(self, kind, serial, built):
        """Install both APKs on serial; (test package, runner), or None after reporting why not."""
        app, test = built
        try:
            adb.install(serial, app)
            adb.install(serial, test, test=True)
            listed = adb.shell(serial, "pm", "list", "instrumentation", timeout=60)
        except adb.Failed as error:
            self.finish(kind, 1, f"the APKs could not be installed: {error}")
            return None
        found = [match for match in INSTRUMENTATION.finditer(listed)]
        own = [match for match in found if match.group("package") == self.package(test, found)]
        if not own:
            self.finish(kind, 1, f"no instrumentation of {test.name} is listed on {serial}")
            return None
        return own[0].group("package"), own[0].group("runner")

    def package(self, test, found):
        """The test APK's package: by aapt2 when the SDK has it, else the one instrumentation whose package ends in
        .test, as Gradle names a test APK's by default."""
        try:
            tools = sorted((sdk.root() or Path("/nonexistent")).glob("build-tools/*/aapt2"))
            if tools:
                done = subprocess.run([str(tools[-1]), "dump", "packagename", str(test)], capture_output=True,
                                      text=True, timeout=60)
                if done.returncode == 0 and done.stdout.strip(): return done.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
        tested = [match.group("package") for match in found if match.group("package").endswith(".test")]
        return tested[0] if len(tested) == 1 else None

    def instrument(self, kind, serial, package, runner, *, tests=(), log):
        """Run the instrumentation, keeping its output in log as it streams; the run (instrumentation.Run) and the
        exit status of adb, or None when it passed its limit."""
        words = instrumentation.command(package, runner, tests=tests, arguments=self.args.arguments)
        command = [sdk.tool("adb"), "-s", serial, "shell", *words]
        run = instrumentation.Run()
        with log.open("w") as stream, subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                                       text=True, errors="replace", env=sdk.environment()) as process:
            expired = threading.Event()
            def stop():
                expired.set()
                process.kill()
            timer = threading.Timer(self.args.test_timeout, stop)
            timer.start()
            try:
                for line in process.stdout:
                    stream.write(line)
                    run.feed(line, at=time.monotonic())
            finally:
                timer.cancel()
            status = process.wait()
        run.close(at=time.monotonic())
        return run, (None if expired.is_set() else status)

    def listed(self, serial, package, runner):
        """The test classes, listed once, on the first emulator to ask."""
        with self.listing_lock:
            if self.listing is not None: return self.listing
            log = self.logs / "listing.log"
            words = instrumentation.command(package, runner, arguments={**self.args.arguments, "log": "true"})
            try:
                text = adb.run(["shell", *words], serial=serial, timeout=LISTING_TIMEOUT).stdout
            except adb.Failed as error:
                text = ""
                self.say(f"LISTING failed: {error}")
            log.write_text(text)
            listed = instrumentation.parse(text)
            self.listing = sorted({case["test"].split("#")[0] for case in listed.tests})
            self.say(f"LISTED {len(listed.tests)} tests in {len(self.listing)} classes; log: {log}")
            return self.listing

    def part(self, kind, built, shard=None):
        """Take an emulator, run kind's tests or its shard's share on it, and release it."""
        args = self.args
        pool = emulator_pool.Pool(args.pool, args.owner, logs=self.logs / "emulators",
                                  said=lambda text: self.say(f"POOL {kind}: {text}"))
        try:
            lease = pool.take(args.image, args.kinds[kind], purpose=f"qk emulator-tests {kind}",
                              keep_state=args.keep_state, boot_timeout=args.boot_timeout)
        except (TimeoutError, RuntimeError, OSError, ValueError, device_record.Refused, sdk.Missing) as error:
            return self.finish(kind, lane_results.UNMEASURED, f"no emulator booted, so no test ran: {error}")
        try:
            if built.result() is None: return self.finish(kind, 1, "not tested: the build failed")
            prepared = self.prepare(kind, lease.serial, built.result())
            if prepared is None: return None
            package, runner = prepared
            selection = list(args.tests)
            if shard is not None:
                classes = self.listed(lease.serial, package, runner)
                timings = shards.read_timings(self.logs / TIMINGS)
                selection = shards.packed(classes, timings, args.shards)[shard]
                if not selection: return self.finish(kind, 0, "its share of the classes is empty")
            log = self.logs / f"{kind}.log"
            self.say(f"TEST {kind} on {lease.key} ({lease.serial}); log: {log}")
            started = time.monotonic()
            run, status = self.instrument(kind, lease.serial, package, runner, tests=selection, log=log)
            self.result(kind, run, status, log, round(time.monotonic() - started))
        finally:
            pool.release(lease, outcome=f"Tested {kind}")

    def result(self, kind, run, status, log, seconds):
        self.keep(kind, run, status, seconds)
        with self.lock:
            for case in run.tests:
                if case["seconds"] is not None:
                    unit = case["test"].split("#")[0]
                    self.seconds[unit] = self.seconds.get(unit, 0) + case["seconds"]
        ran, skipped, failed, unmeasured = run.counts()
        counted = run.summary()
        if status is None:
            return self.finish(kind, lane_results.UNMEASURED, f"the tests exceeded {self.args.test_timeout}s; "
                               f"{counted}", log, seconds)
        if failed:
            names = [case["test"] for case in run.tests if case["outcome"] == "failed"]
            self.finish(kind, 1, counted, log, seconds)
            return self.say(*(f"    FAILED {name}" for name in names[:50]))
        if run.crashed or unmeasured or not run.complete:
            why = run.crashed or "the instrumentation did not report every test it announced"
            return self.finish(kind, lane_results.UNMEASURED, f"{counted}; {why}", log, seconds)
        if ran - skipped == 0:
            return self.finish(kind, lane_results.UNMEASURED, f"no test ran ({counted})", log, seconds)
        self.finish(kind, 0, counted, log, seconds)

    def keep(self, kind, run, status, seconds):
        results = os.environ.get(RESULTS_VARIABLE)
        if not results: return
        folder = Path(results) / KEPT
        try:
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f"{kind}.json").write_text(json.dumps({
                "run": kind, "keptBy": KEPT_BY, "status": status, "seconds": seconds,
                "crashed": run.crashed, "tests": run.tests}, indent=1) + "\n")
        except OSError as error:
            self.say(f"queuekeeper emulator-tests: {kind}'s results were not kept in {folder}: {error}")

    def finish(self, kind, status, detail, log=None, seconds=None):
        self.statuses[kind] = status
        timing = f" in {seconds}s" if seconds is not None else ""
        self.say(f"{outcome(status)} {kind}: {detail}{timing}" + (f"; log: {log}" if log else ""))


def case_line(kind, case):
    seconds = "no time" if case["seconds"] is None else f"{case['seconds']:.3f}s"
    reason = f" ({case['reason']})" if case.get("reason") else ""
    return f"{kind}: {case['outcome']} {seconds} {case['test']}{reason}"


def report(results, out=print):
    """Print emulator-test records from this lane's returned results, including a group's members."""
    for path in sorted(Path(results).rglob(f"{KEPT}/*.json")):
        try:
            record = json.loads(path.read_text())
            if not isinstance(record, dict): raise ValueError("expected a result record")
            if record.get("keptBy") != KEPT_BY: continue
            kind = record["run"]
            lines = [case_line(kind, case) for case in record["tests"]]
        except (OSError, ValueError, KeyError, TypeError) as error:
            out(f"queuekeeper: {path} could not be read: {error!r}")
            continue
        out(f"queuekeeper: per-test results in {path}:")
        for line in lines: out(line)
        if not lines: out(f"{kind}: no test outcomes recorded")
        if crashed := record.get("crashed"): out(f"{kind}: {crashed}")


def parse(argv, project):
    parser = argparse.ArgumentParser(prog="qk emulator-tests", description=__doc__.split("\n\n")[0],
                                     epilog=__doc__.split("\n\n", 1)[1], formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--device", action="append", default=[], metavar="PROFILE",
                        help=f"a hardware profile to test on, as avdmanager names it (repeatable; default "
                             f"{emulator_pool.DEVICE})")
    parser.add_argument("--image", default=emulator_pool.IMAGE, help=f"the system image (default {emulator_pool.IMAGE})")
    parser.add_argument("--shards", type=int, default=1, help="emulators per profile, sharing its classes")
    parser.add_argument("--repeat", type=int, default=1, help="run the whole set this many times, from one build")
    parser.add_argument("--class", dest="tests", action="append", default=[], metavar="CLASS[#METHOD]",
                        help="run only these tests (repeatable)")
    parser.add_argument("--argument", action="append", default=[], metavar="KEY=VALUE",
                        help="an instrumentation argument, as am instrument -e takes it (repeatable)")
    parser.add_argument("--boot-timeout", type=float, default=emulator_pool.BOOT_TIMEOUT)
    parser.add_argument("--test-timeout", type=float, default=TEST_TIMEOUT)
    parser.add_argument("--keep-state", action="store_true", help="boot members as their last holder left them")
    parser.add_argument("--pool", default=None, help="the pool's name (default: emulatorPool in queuekeeper.json)")
    parser.add_argument("--owner", default=device_record.default_owner())
    parser.add_argument("--logs", type=Path, default=Path(".build/queuekeeper/emulator-tests"))
    args = parser.parse_args(argv)
    if args.shards < 1 or args.repeat < 1: parser.error("--shards and --repeat are at least 1")
    if args.shards > 1 and args.tests: parser.error("--shards packs every class; it takes no --class")
    try:
        args.arguments = dict(pair.split("=", 1) for pair in args.argument)
    except ValueError:
        parser.error("--argument takes KEY=VALUE")
    try:
        config = json.loads((project / "queuekeeper.json").read_text())
    except (OSError, ValueError):
        config = {}
    args.android = settings.configured(config)
    args.pool = args.pool or config.get("emulatorPool")
    if not args.pool: parser.error("name the pool: emulatorPool in queuekeeper.json, or --pool")
    if not args.owner: parser.error(f"name the session: --owner, or {device_record.OWNER}")
    return args


def main(argv, project=None):
    root = Path(project or Path.cwd()).resolve()
    args = parse(argv, root)
    args.logs.mkdir(parents=True, exist_ok=True)
    profiles = args.device or [emulator_pool.DEVICE]
    statuses = []
    with ThreadPoolExecutor(max_workers=1) as builder:
        run = Run(args, root)
        built = builder.submit(run.build)
        for repeat in range(args.repeat):
            run = Run(args, root) if repeat else run
            parts = []
            args.kinds = {}
            for profile in profiles:
                prefix = profile if args.repeat == 1 else f"{profile}-run{repeat + 1}"
                if args.shards == 1:
                    args.kinds[prefix] = profile
                    parts.append((prefix, None))
                else:
                    for shard in range(args.shards):
                        args.kinds[f"{prefix}-{shard + 1}"] = profile
                        parts.append((f"{prefix}-{shard + 1}", shard))
            with ThreadPoolExecutor(max_workers=len(parts)) as workers:
                for done in [workers.submit(run.part, kind, built, shard) for kind, shard in parts]: done.result()
            shards.keep_timings(args.logs / TIMINGS, run.seconds)
            statuses.extend(run.statuses.get(kind, 1) for kind, _shard in parts)
    status = combined(statuses)
    print(f"queuekeeper emulator-tests: {outcome(status)}", flush=True)
    return status
