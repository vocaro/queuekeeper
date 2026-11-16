"""A Swift package's XCTest classes in several xctest processes side by side, from one build.

`swift test --parallel` reports a skipped XCTest test as passed and prints no total, so a skip,
an unmeasured result among them, would read as a pass (Swift 6.4, Xcode 27.2, October 2026). Each
process here runs `xcrun xctest -XCTest CLASSES BUNDLE`, one test at a time, and prints XCTest's
own lines whole: every skip, failure and suite total, which lane_results.counted reads. Swift Testing
tests run once beside them, in `swift test --skip-build --disable-xctest`, which runs them
concurrently itself; with --split-swift-testing their suites are packed into the shards as the
classes are, each shard's in a `swift test --no-parallel` process of its own that runs one test at a
time, as xctest does. Several packages build one after another and share one pool of processes.
A project passes what its run needs as options: a scratch path, a configuration, other swift
options. doc/guides/parallel-tests.md says when this is safe and how to bound it.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import os
from pathlib import Path
import re
import statistics
import subprocess
import tempfile
import time

from queuekeeper import lane_results, parallel, processes

# Processes at a time unless --jobs says otherwise, as qk unittest's.
DEFAULT_JOBS = parallel.DEFAULT_JOBS
# Shards per process: enough that a shard's misjudged length evens out, few enough that the
# bundle's load in each shard costs little.
SHARDS_PER_JOB = 3
# Each class's seconds in the last run of a configuration, which orders and packs the next one's
# shards, relative to the package.
TIMINGS = ".build/queuekeeper/xctest/timings-{configuration}.json"
CASE = re.compile(r"^Test Case '-\[(\S+) (\S+)\]' (?:passed|failed|skipped) \((\d+(?:\.\d+)?) seconds\)", re.M)
# Swift Testing's line for a suite that ended, named by its type or, in quotes, its display name.
SUITE = re.compile(r"Suite (\S+|\"[^\"]*\") (?:passed|failed) after (\d+(?:\.\d+)?) seconds", re.M)
# Each Swift Testing test's wall-clock start and finish, from its process's event stream, which records an
# event as it happens whatever order the processes' output is printed in (vocaro-engineering#367). In the
# lane's results/ when qk run gives it one, so it comes back and is kept by commit with them (#218);
# otherwise in the first package's .build/queuekeeper/xctest/.
TEST_TIMES = "xctest/swift-testing-times.json"
LOCAL_TEST_TIMES = ".build/queuekeeper/xctest/swift-testing-times-{configuration}.json"
EVENT_STREAM_VERSION = "0"
# A test's id in the event stream goes on past the one `swift test list` prints with its source location.
SOURCE_LOCATION = re.compile(r"/[^/]+\.swift:\d+:\d+$")


def swift(directory, options, *arguments):
    """A swift command in the package, with the options every one of its commands takes."""
    return ["swift", *arguments, *options], directory


def listed(directory, options, *more):
    """The ids `swift test list` prints: Module.Class/test for XCTest, Module.function() or
    Module.Suite/function() for Swift Testing."""
    command, cwd = swift(directory, options, "test", "list", "--skip-build", *more)
    found = subprocess.run(command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                           errors="replace")
    if found.returncode != 0:
        raise RuntimeError(f"swift test list in {directory} exited {found.returncode}: {found.stderr.strip()[-2000:]}")
    return [line.strip() for line in found.stdout.splitlines() if line.strip() and not line.startswith("warning:")]


def bundles(directory, options):
    """{module: xctest bundle}. A build that makes one bundle for every test target, as SwiftPM's
    native build system does, names it PACKAGEPackageTests and runs every module's tests; one that
    makes a bundle per target names each after its module."""
    command, cwd = swift(directory, options, "build", "--show-bin-path")
    shown = subprocess.run(command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
    found = sorted(Path(shown.stdout.strip()).glob("*.xctest"))
    if not found: raise RuntimeError(f"no .xctest bundle in {shown.stdout.strip()}")
    return {bundle.stem: bundle for bundle in found}


def bundle_for(module, found):
    if module in found: return found[module]
    if len(found) == 1: return next(iter(found.values()))
    raise RuntimeError(f"no bundle for {module} among {', '.join(sorted(found))}")


def class_seconds(text):
    """{Module.Class: seconds} over XCTest's per-test lines in a log."""
    seconds = {}
    for match in CASE.finditer(text):
        seconds[match.group(1)] = seconds.get(match.group(1), 0.0) + float(match.group(3))
    return seconds


def suite_seconds(text, units):
    """{Module.Suite: seconds} over Swift Testing's suite lines in a log, for the units, Module.Suite,
    a process ran: a line names its suite without its module."""
    by_name = {unit.split(".", 1)[-1]: unit for unit in units}
    seconds = {}
    for match in SUITE.finditer(text):
        unit = by_name.get(match.group(1))
        if unit: seconds[unit] = seconds.get(unit, 0.0) + float(match.group(2))
    return seconds


def wall_clock(instant):
    """An event's instant as local ISO 8601 time with its offset, to the millisecond, or None."""
    since1970 = (instant or {}).get("since1970")
    if not isinstance(since1970, (int, float)): return None
    return datetime.fromtimestamp(since1970).astimezone().isoformat(timespec="milliseconds")


def test_times(events, shard, timed_out=False):
    """[{test, shard, verdict, seconds, started, finished}] for each Swift Testing test function in one
    process's event stream (JSON lines, `swift test --event-stream-output-path`, version 0): its id as
    `swift test list` prints it, the process's name, passed, failed, skipped, canceled or unmeasured, and
    its seconds and wall-clock start and finish. A test that started and never finished, as in a process
    stopped at its timeout, keeps its start, is unmeasured and has no finish or seconds. A line cut short by
    the stop is passed over."""
    functions, started, ended, failed, skipped, canceled, order = set(), {}, {}, set(), {}, {}, []
    for line in events.splitlines():
        try: record = json.loads(line)
        except ValueError: continue
        payload = record.get("payload") if isinstance(record, dict) else None
        if not isinstance(payload, dict): continue
        if record.get("kind") == "test":
            if payload.get("kind") == "function" and payload.get("id"): functions.add(payload["id"])
            continue
        if record.get("kind") != "event": continue
        kind, test, instant = payload.get("kind"), payload.get("testID"), payload.get("instant")
        if not test: continue
        if test not in started and test not in skipped: order.append(test)
        if kind == "testStarted": started[test] = instant
        elif kind == "testEnded": ended[test] = instant
        elif kind == "testSkipped": skipped[test] = instant
        elif kind == "testCancelled": canceled[test] = instant
        elif kind == "issueRecorded":
            issue = payload.get("issue") or {}
            # A known issue (withKnownIssue) or a warning does not fail its test.
            if not issue.get("isKnown") and issue.get("severity", "error") == "error": failed.add(test)
    # A suite's events carry its id too; without the stream's test records, a function's id ends in its
    # parentheses before its source location.
    function = (lambda test: test in functions) if functions \
        else (lambda test: SOURCE_LOCATION.sub("", test).endswith(")"))
    found = []
    for test in dict.fromkeys(order):
        if not function(test): continue
        began, finished = started.get(test), ended.get(test, canceled.get(test))
        if test in skipped and test not in started: verdict = "skipped"
        elif finished is None: verdict = "unmeasured"
        elif test in failed: verdict = "failed"
        elif test in canceled: verdict = "canceled"
        else: verdict = "passed"
        seconds = None
        if began and finished:
            for clock in ("absolute", "since1970"):
                if isinstance(began.get(clock), (int, float)) and isinstance(finished.get(clock), (int, float)):
                    seconds = round(finished[clock] - began[clock], 6)
                    break
        found.append({"test": SOURCE_LOCATION.sub("", test), "shard": shard, "verdict": verdict, "seconds": seconds,
                      "started": wall_clock(began), "finished": wall_clock(finished),
                      **({"reason": "timed out" if timed_out else "its process ended first"}
                         if verdict == "unmeasured" else {})})
    return found


def keep_test_times(found, directory, configuration):
    """Write found, every process's test_times, where TEST_TIMES says, and return its path or None. A second
    qk xctest in one lane writes beside the first, never over it."""
    results = os.environ.get(parallel.RESULTS_VARIABLE)
    path = Path(results) / TEST_TIMES if results else directory / LOCAL_TEST_TIMES.format(configuration=configuration)
    candidate, number = path, 1
    while results and candidate.exists():
        number += 1
        candidate = path.with_name(f"{path.stem}-{number}{path.suffix}")
    ordered = sorted(found, key=lambda entry: (entry["started"] is None,
                                               datetime.fromisoformat(entry["started"]).timestamp() if entry["started"] else 0,
                                               entry["test"]))
    try:
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_text(json.dumps({"tests": ordered}, indent=1) + "\n")
    except OSError as error:
        print(f"queuekeeper xctest: Swift Testing's test times were not kept: {error}", flush=True)
        return None
    return candidate


def unit(test):
    """What a test is packed with: its XCTest class or its top-level Swift Testing suite, Module.Name, or a
    Swift Testing function outside a suite by itself."""
    return test.split("/", 1)[0]


def swift_testing_filters(names, classes, sizes):
    """swift test --filter expressions that select exactly the tests of names: a unit whose every test is
    selected by its prefix, any other test by its whole id. Neither ends in $: Swift Testing matches a
    filter against an id that goes on past the one `swift test list` prints (Swift 6.4.2, 2026-11-05),
    and an id that ends in its function's parentheses is no other's prefix."""
    filters = []
    for name in names:
        if len(classes[name]) == sizes[name] and "/" in classes[name][0]: filters.append(f"^{re.escape(name)}/")
        else: filters.extend(f"^{re.escape(test)}" for test in classes[name])
    return filters


def shards(selected, seconds, count):
    """[(estimated seconds, [xctest selectors])], longest first: the classes packed into at most
    count shards, each next class, longest first, into the shard with the least. A class whose every
    test is selected is named whole, Module.Class; otherwise each test is, Module.Class/test. A
    class with no recorded time counts the median of those that have one."""
    classes = {}
    for test in selected: classes.setdefault(unit(test), []).append(test)
    known = [seconds[name] for name in classes if name in seconds]
    guess = statistics.median(known) if known else 1.0
    order = sorted(classes, key=lambda name: (-seconds.get(name, guess), name))
    bins = [[0.0, []] for _ in range(max(1, min(count, len(order))))]
    for name in order:
        bin = min(bins, key=lambda entry: entry[0])
        bin[0] += seconds.get(name, guess)
        bin[1].append(name)
    return sorted(((total, names) for total, names in bins if names), key=lambda entry: -entry[0]), classes


def run(name, command, directory, expected, timeout=parallel.PROCESS_LIMIT, events=None):
    """Run one process's tests; one still running after timeout seconds is stopped, and its status is None.
    With events, a path, a swift test process writes its event stream there, and its tests' times are read
    from it however the process ended."""
    if events: command = [*command, "--event-stream-output-path", str(events), "--event-stream-version",
                          EVENT_STREAM_VERSION]
    began = time.monotonic()
    status, output = processes.bounded(command, cwd=directory, timeout=timeout)
    found = lane_results.counted(output)
    times = []
    if events:
        try: times = test_times(Path(events).read_text(errors="replace"), name, timed_out=status is None)
        except OSError: pass
    return {"name": name, "status": status, "output": output, "expected": expected, "directory": directory,
            "ran": found[0] if found else 0, "skipped": found[1] if found else 0,
            "seconds": time.monotonic() - began, "times": times}


def planned(directory, options, filters, jobs, timings, label, split=False):
    """[(name, command, expected tests, estimate, units)] for one built package: its Swift Testing run
    first, as its length is unknown and it runs its tests concurrently itself, then its XCTest shards,
    longest first; and the classes' recorded seconds. With split, the Swift Testing suites are packed
    into the shards with the classes, and each shard runs its suites in a process of their own."""
    xctests = listed(directory, options, "--disable-swift-testing")
    everything = listed(directory, options)
    found = bundles(directory, options) if xctests else {}
    patterns = [re.compile(pattern) for pattern in filters]
    chosen = lambda test: not patterns or any(pattern.search(test) for pattern in patterns)
    selected = [test for test in xctests if chosen(test)]
    known = set(xctests)
    swift_testing = [test for test in everything if test not in known and chosen(test)]
    try: seconds = json.loads(timings.read_text())
    except (OSError, ValueError): seconds = {}
    packed, classes = shards(selected + (swift_testing if split else []), seconds, jobs * SHARDS_PER_JOB)
    work = []
    if swift_testing and not split:
        command, _ = swift(directory, options, "test", "--skip-build", "--disable-xctest",
                           *[f"--filter={pattern}" for pattern in filters])
        work.append((f"{label}Swift Testing", command, len(swift_testing), None, []))
    sizes = {}
    for test in everything: sizes[unit(test)] = sizes.get(unit(test), 0) + 1
    in_swift_testing = {unit(test) for test in swift_testing}
    for number, (estimate, names) in enumerate(packed, 1):
        # One process per bundle the shard's classes are in, as xctest loads one bundle, and one for its suites.
        by_bundle, suites = {}, [name for name in names if name in in_swift_testing]
        for name in names:
            if name in in_swift_testing: continue
            entry = by_bundle.setdefault(bundle_for(name.split(".", 1)[0], found), [[], 0])
            entry[0].extend([name] if len(classes[name]) == sizes[name] else classes[name])
            entry[1] += len(classes[name])
        for bundle, (selectors, count) in by_bundle.items():
            command = ["xcrun", "xctest", "-XCTest", ",".join(selectors), str(bundle)]
            work.append((f"{label}shard {number} of {len(packed)}, {bundle.stem} (about {estimate:.0f}s)",
                         command, count, estimate, []))
        if suites:
            # --ignore-lock: SwiftPM holds the package's .build lock for the whole of a swift test, so without
            # it each shard's process waits for the one before to end, and the shards run one at a time.
            command, _ = swift(directory, options, "test", "--skip-build", "--ignore-lock", "--disable-xctest", "--no-parallel",
                               *[f"--filter={pattern}" for pattern in swift_testing_filters(suites, classes, sizes)])
            work.append((f"{label}shard {number} of {len(packed)}, Swift Testing (about {estimate:.0f}s)",
                         command, sum(len(classes[name]) for name in suites), estimate, suites))
    return work, seconds


def main(argv):
    parser = argparse.ArgumentParser(prog="qk xctest", description="Build Swift packages' tests once and run their "
        "XCTest classes in xctest processes of their own, JOBS at a time, and their Swift Testing tests beside them.")
    parser.add_argument("-C", "--package-path", action="append", default=[],
                        help="a package to test (default: this directory); repeat for several, which build in turn "
                        "and share the processes")
    parser.add_argument("-j", "--jobs", type=int, default=DEFAULT_JOBS,
                        help=f"xctest processes at a time (default {DEFAULT_JOBS}); 1 runs them one after another")
    parser.add_argument("--build-jobs", type=int, help="the build's jobs, as swift build --jobs")
    parser.add_argument("-c", "--configuration", choices=["debug", "release"], default="debug",
                        help="the build configuration, as swift build -c (default debug)")
    parser.add_argument("--scratch-path", help="the build directory, as swift build --scratch-path; with several "
                        "packages, relative to each")
    parser.add_argument("--swift-option", action="append", default=[], metavar="OPTION",
                        help="an option every swift command of the run takes, such as --swift-option=-Xswiftc; "
                        "repeat for each")
    parser.add_argument("--skip-build", action="store_true", help="run the tests the last build built")
    parser.add_argument("--split-swift-testing", action="store_true",
                        help="pack Swift Testing suites into the shards with the XCTest classes, each shard's suites "
                        "in a process of their own that runs one test at a time, as xctest does: for suites that "
                        "share process state, such as views on the main actor")
    parser.add_argument("--filter", action="append", default=[], help="a regular expression a test's id, "
                        "Module.Class/test, must match, as swift test's; repeat for any of several")
    parser.add_argument("--timeout", type=float, default=parallel.PROCESS_LIMIT, metavar="SECONDS",
                        help=f"seconds one test process may run before it is stopped and left unmeasured "
                        f"(default {parallel.PROCESS_LIMIT})")
    args = parser.parse_args(argv)
    if args.jobs < 1: parser.error("--jobs needs at least 1")
    if args.timeout <= 0: parser.error("--timeout needs a positive number of seconds")
    directories = [Path(path).resolve() for path in args.package_path or ["."]]
    options = ["-c", args.configuration, *(["--scratch-path", args.scratch_path] if args.scratch_path else []),
               *args.swift_option]
    work, recorded = [], {}
    for directory in directories:
        label = f"{directory.name}: " if len(directories) > 1 else ""
        if not args.skip_build:
            command, cwd = swift(directory, options, "build", "--build-tests",
                                 *(["--jobs", str(args.build_jobs)] if args.build_jobs else []))
            status = subprocess.run(command, cwd=cwd).returncode
            if status != 0:
                print(f"queuekeeper xctest: {label}the build exited {status}", flush=True)
                return status
        timings = directory / TIMINGS.format(configuration=args.configuration)
        try: planned_work, recorded[directory] = planned(directory, options, args.filter, args.jobs, timings, label,
                                                         args.split_swift_testing)
        except (RuntimeError, subprocess.CalledProcessError) as error:
            print(f"queuekeeper xctest: {error}", flush=True)
            return 1
        work.extend((name, command, expected, estimate, directory, units)
                    for name, command, expected, estimate, units in planned_work)
    if not work:
        print(f"queuekeeper xctest: no tests in {', '.join(map(str, directories))} match {' or '.join(args.filter)}", flush=True)
        return parallel.NO_TESTS
    # Each package's Swift Testing run first, then every package's shards together, longest first.
    work.sort(key=lambda entry: (entry[3] is not None, -(entry[3] or 0)))
    began = time.monotonic()
    streams = tempfile.TemporaryDirectory(prefix="eng-xctest-events-")
    with streams, ThreadPoolExecutor(max_workers=args.jobs) as pool:
        # Each swift test process's event stream, for its tests' wall-clock start and finish.
        futures = [pool.submit(run, name, command, directory, expected, args.timeout,
                               Path(streams.name) / f"{number}.jsonl" if command[0] == "swift" else None)
                   for number, (name, command, expected, _, directory, _) in enumerate(work)]
        results = []
        for future in futures:
            result = future.result()
            results.append(result)
            # Each process's own output, whole, under one line that names it.
            shown = f"timed out after {args.timeout:g}s" if result["status"] is None \
                else parallel.outcome(result["status"])
            print(f"== {result['name']}: {shown} ({result['ran']} tests, "
                  f"{result['skipped']} skipped, {result['seconds']:.1f}s)", result["output"].rstrip("\n"),
                  sep="\n", flush=True)
    failed, unmeasured = [], []
    for result in results:
        if result["status"] is None: unmeasured.append(f"{result['name']} (timed out after {args.timeout:g}s)")
        elif result["status"] != 0: failed.append(f"{result['name']} (status {result['status']})")
        elif result["ran"] != result["expected"]:
            # A process that ran other than the tests it was given did not run what it was given.
            failed.append(f"{result['name']} (ran {result['ran']} of {result['expected']} tests)")
    for directory in directories:
        kept = dict(recorded[directory])
        for result, (_, _, _, _, _, units) in zip(results, work):
            if result["directory"] == directory:
                kept.update(class_seconds(result["output"]))
                kept.update(suite_seconds(result["output"], units))
        timings = directory / TIMINGS.format(configuration=args.configuration)
        try:
            timings.parent.mkdir(parents=True, exist_ok=True)
            timings.write_text(json.dumps(kept, indent=1, sort_keys=True) + "\n")
        except OSError as error:
            print(f"queuekeeper xctest: the classes' times were not kept: {error}", flush=True)
    found = [entry for result in results for entry in result["times"]]
    if found:
        kept = keep_test_times(found, directories[0], args.configuration)
        if kept: print(f"queuekeeper xctest: {len(found)} Swift Testing tests' start and finish times in {kept}", flush=True)
    # A test the host decided skips with a reason that begins "Unmeasured:"; its process still passes.
    decided = lane_results.unmeasured_tests("\n".join(result["output"] for result in results))
    if decided: unmeasured.append(f"{len(decided)} tests the host decided, listed below")
    total = sum(result["ran"] for result in results)
    skipped = sum(result["skipped"] for result in results)
    # Not XCTest's or Swift Testing's own total: the processes' lines already count every test once.
    print(f"queuekeeper xctest: {total} tests ({skipped} skipped) in {len(results)} processes, {args.jobs} at a time, "
          f"{time.monotonic() - began:.1f}s: " + parallel.verdict(failed, unmeasured), flush=True)
    if decided and not failed: print("\n".join(decided), flush=True)
    return 1 if failed else lane_results.UNMEASURED if unmeasured else 0
