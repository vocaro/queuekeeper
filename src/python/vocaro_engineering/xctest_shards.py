"""A Swift package's XCTest classes in several xctest processes side by side, from one build.

`swift test --parallel` reports a skipped XCTest test as passed and prints no total, so a skip,
an unmeasured result among them, would read as a pass (Swift 6.4, Xcode 27.2, October 2026). Each
process here runs `xcrun xctest -XCTest CLASSES BUNDLE`, one test at a time, and prints XCTest's
own lines whole: every skip, failure and suite total, which evidence.counted reads. Swift Testing
tests run once beside them, in `swift test --skip-build --disable-xctest`, which runs them
concurrently itself. Several packages build one after another and share one pool of processes.
A project passes what its run needs as options: a scratch path, a configuration, other swift
options. guides/parallel-tests.md says when this is safe and how to bound it.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import statistics
import subprocess
import time

from . import evidence, parallel, processes

# Processes at a time unless --jobs says otherwise, as eng unittest's.
DEFAULT_JOBS = parallel.DEFAULT_JOBS
# Shards per process: enough that a shard's misjudged length evens out, few enough that the
# bundle's load in each shard costs little.
SHARDS_PER_JOB = 3
# Each class's seconds in the last run of a configuration, which orders and packs the next one's
# shards, relative to the package.
TIMINGS = ".build/engineering/xctest/timings-{configuration}.json"
CASE = re.compile(r"^Test Case '-\[(\S+) (\S+)\]' (?:passed|failed|skipped) \((\d+(?:\.\d+)?) seconds\)", re.M)


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


def shards(selected, seconds, count):
    """[(estimated seconds, [xctest selectors])], longest first: the classes packed into at most
    count shards, each next class, longest first, into the shard with the least. A class whose every
    test is selected is named whole, Module.Class; otherwise each test is, Module.Class/test. A
    class with no recorded time counts the median of those that have one."""
    classes = {}
    for test in selected: classes.setdefault(test.split("/", 1)[0], []).append(test)
    known = [seconds[name] for name in classes if name in seconds]
    guess = statistics.median(known) if known else 1.0
    order = sorted(classes, key=lambda name: (-seconds.get(name, guess), name))
    bins = [[0.0, []] for _ in range(max(1, min(count, len(order))))]
    for name in order:
        bin = min(bins, key=lambda entry: entry[0])
        bin[0] += seconds.get(name, guess)
        bin[1].append(name)
    return sorted(((total, names) for total, names in bins if names), key=lambda entry: -entry[0]), classes


def run(name, command, directory, expected, timeout=parallel.PROCESS_LIMIT):
    """Run one process's tests; one still running after timeout seconds is stopped, and its status is None."""
    began = time.monotonic()
    status, output = processes.bounded(command, cwd=directory, timeout=timeout)
    found = evidence.counted(output)
    return {"name": name, "status": status, "output": output, "expected": expected, "directory": directory,
            "ran": found[0] if found else 0, "skipped": found[1] if found else 0,
            "seconds": time.monotonic() - began}


def planned(directory, options, filters, jobs, timings, label):
    """[(name, command, expected tests)] for one built package: its Swift Testing run first, as its
    length is unknown and it runs its tests concurrently itself, then its XCTest shards, longest
    first; and the classes' recorded seconds."""
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
    packed, classes = shards(selected, seconds, jobs * SHARDS_PER_JOB)
    work = []
    if swift_testing:
        command, _ = swift(directory, options, "test", "--skip-build", "--disable-xctest",
                           *[f"--filter={pattern}" for pattern in filters])
        work.append((f"{label}Swift Testing", command, len(swift_testing), None))
    sizes = {}
    for test in xctests: sizes[test.split("/", 1)[0]] = sizes.get(test.split("/", 1)[0], 0) + 1
    for number, (estimate, names) in enumerate(packed, 1):
        # One process per bundle the shard's classes are in: xctest loads one bundle.
        by_bundle = {}
        for name in names:
            entry = by_bundle.setdefault(bundle_for(name.split(".", 1)[0], found), [[], 0])
            entry[0].extend([name] if len(classes[name]) == sizes[name] else classes[name])
            entry[1] += len(classes[name])
        for bundle, (selectors, count) in by_bundle.items():
            command = ["xcrun", "xctest", "-XCTest", ",".join(selectors), str(bundle)]
            work.append((f"{label}shard {number} of {len(packed)}, {bundle.stem} (about {estimate:.0f}s)",
                         command, count, estimate))
    return work, seconds


def main(argv):
    parser = argparse.ArgumentParser(prog="eng xctest", description="Build Swift packages' tests once and run their "
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
                print(f"eng xctest: {label}the build exited {status}", flush=True)
                return status
        timings = directory / TIMINGS.format(configuration=args.configuration)
        try: planned_work, recorded[directory] = planned(directory, options, args.filter, args.jobs, timings, label)
        except (RuntimeError, subprocess.CalledProcessError) as error:
            print(f"eng xctest: {error}", flush=True)
            return 1
        work.extend((name, command, expected, estimate, directory) for name, command, expected, estimate in planned_work)
    if not work:
        print(f"eng xctest: no tests in {', '.join(map(str, directories))} match {' or '.join(args.filter)}", flush=True)
        return parallel.NO_TESTS
    # Each package's Swift Testing run first, then every package's shards together, longest first.
    work.sort(key=lambda entry: (entry[3] is not None, -(entry[3] or 0)))
    began = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = [pool.submit(run, name, command, directory, expected, args.timeout)
                   for name, command, expected, _, directory in work]
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
        for result in results:
            if result["directory"] == directory: kept.update(class_seconds(result["output"]))
        timings = directory / TIMINGS.format(configuration=args.configuration)
        try:
            timings.parent.mkdir(parents=True, exist_ok=True)
            timings.write_text(json.dumps(kept, indent=1, sort_keys=True) + "\n")
        except OSError as error:
            print(f"eng xctest: the classes' times were not kept: {error}", flush=True)
    # A test the host decided skips with a reason that begins "Unmeasured:"; its process still passes.
    decided = evidence.unmeasured_tests("\n".join(result["output"] for result in results))
    if decided: unmeasured.append(f"{len(decided)} tests the host decided, listed below")
    total = sum(result["ran"] for result in results)
    skipped = sum(result["skipped"] for result in results)
    # Not XCTest's or Swift Testing's own total: the processes' lines already count every test once.
    print(f"eng xctest: {total} tests ({skipped} skipped) in {len(results)} processes, {args.jobs} at a time, "
          f"{time.monotonic() - began:.1f}s: " + parallel.verdict(failed, unmeasured), flush=True)
    if decided and not failed: print("\n".join(decided), flush=True)
    return 1 if failed else evidence.UNMEASURED if unmeasured else 0
