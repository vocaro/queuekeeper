"""A Swift package's XCTest classes in several xctest processes side by side, from one build.

`swift test --parallel` reports a skipped XCTest test as passed and prints no total, so a skip,
an unmeasured result among them, would read as a pass (Swift 6.4, Xcode 27.2, October 2026). Each
process here runs `xcrun xctest -XCTest CLASSES BUNDLE`, one test at a time, and prints XCTest's
own lines whole: every skip, failure and suite total, which evidence.counted reads. Swift Testing
tests run once beside them, in `swift test --skip-build --disable-xctest`, which runs them
concurrently itself. guides/parallel-tests.md says when this is safe and how to bound it.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import statistics
import subprocess
import time

from . import evidence, parallel

# Processes at a time unless --jobs says otherwise, as eng unittest's.
DEFAULT_JOBS = parallel.DEFAULT_JOBS
# Shards per process: enough that a shard's misjudged length evens out, few enough that the
# bundle's load in each shard costs little.
SHARDS_PER_JOB = 3
# Each class's seconds in the last run, which orders and packs the next one's shards.
TIMINGS = ".build/engineering/xctest/timings.json"
CASE = re.compile(r"^Test Case '-\[(\S+) (\S+)\]' (?:passed|failed|skipped) \((\d+(?:\.\d+)?) seconds\)", re.M)


def listed(directory, *options):
    """The ids `swift test list` prints: Module.Class/test for XCTest, Module.function() or
    Module.Suite/function() for Swift Testing."""
    found = subprocess.run(["swift", "test", "list", "--skip-build", *options], cwd=directory,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace")
    if found.returncode != 0:
        raise RuntimeError(f"swift test list exited {found.returncode}: {found.stderr.strip()[-2000:]}")
    return [line.strip() for line in found.stdout.splitlines() if line.strip() and not line.startswith("warning:")]


def bundles(directory):
    """{module: xctest bundle}. A build that makes one bundle for every test target, as SwiftPM's
    native build system does, names it PACKAGEPackageTests and runs every module's tests; one that
    makes a bundle per target names each after its module."""
    shown = subprocess.run(["swift", "build", "--show-bin-path"], cwd=directory, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, text=True, check=True)
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


def run(name, command, directory, expected):
    began = time.monotonic()
    finished = subprocess.run(command, cwd=directory, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, errors="replace")
    found = evidence.counted(finished.stdout)
    return {"name": name, "status": finished.returncode, "output": finished.stdout, "expected": expected,
            "ran": found[0] if found else 0, "skipped": found[1] if found else 0,
            "seconds": time.monotonic() - began}


def main(argv):
    parser = argparse.ArgumentParser(prog="eng xctest", description="Build a Swift package's tests once and run its "
        "XCTest classes in xctest processes of their own, JOBS at a time, and its Swift Testing tests beside them.")
    parser.add_argument("-C", "--package-path", default=".")
    parser.add_argument("-j", "--jobs", type=int, default=DEFAULT_JOBS,
                        help=f"xctest processes at a time (default {DEFAULT_JOBS}); 1 runs them one after another")
    parser.add_argument("--build-jobs", type=int, help="the build's jobs, as swift build --jobs")
    parser.add_argument("--skip-build", action="store_true", help="run the tests the last build built")
    parser.add_argument("--filter", action="append", default=[], help="a regular expression a test's id, "
                        "Module.Class/test, must match, as swift test's; repeat for any of several")
    args = parser.parse_args(argv)
    if args.jobs < 1: parser.error("--jobs needs at least 1")
    directory = Path(args.package_path).resolve()
    if not args.skip_build:
        build = ["swift", "build", "--build-tests", *(["--jobs", str(args.build_jobs)] if args.build_jobs else [])]
        status = subprocess.run(build, cwd=directory).returncode
        if status != 0: return status
    try:
        xctests = listed(directory, "--disable-swift-testing")
        everything = listed(directory)
        found = bundles(directory) if xctests else {}
    except (RuntimeError, subprocess.CalledProcessError) as error:
        print(f"eng xctest: {error}", flush=True)
        return 1
    patterns = [re.compile(pattern) for pattern in args.filter]
    chosen = lambda test: not patterns or any(pattern.search(test) for pattern in patterns)
    selected = [test for test in xctests if chosen(test)]
    known = set(xctests)
    swift_testing = [test for test in everything if test not in known and chosen(test)]
    if not selected and not swift_testing:
        print(f"eng xctest: no tests in {directory} match {' or '.join(args.filter)}", flush=True)
        return parallel.NO_TESTS
    timings = directory / TIMINGS
    try: seconds = json.loads(timings.read_text())
    except (OSError, ValueError): seconds = {}
    packed, classes = shards(selected, seconds, args.jobs * SHARDS_PER_JOB)
    work = []
    if swift_testing:
        # Started first: its length is unknown, and it runs its tests concurrently itself.
        command = ["swift", "test", "--skip-build", "--disable-xctest", *[f"--filter={p}" for p in args.filter]]
        work.append(("Swift Testing", command, len(swift_testing)))
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
            work.append((f"shard {number} of {len(packed)}, {bundle.stem} (about {estimate:.0f}s)", command, count))
    began = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = [pool.submit(run, name, command, directory, expected) for name, command, expected in work]
        results = []
        for future in futures:
            result = future.result()
            results.append(result)
            # Each process's own output, whole, under one line that names it.
            print(f"== {result['name']}: {parallel.outcome(result['status'])} ({result['ran']} tests, "
                  f"{result['skipped']} skipped, {result['seconds']:.1f}s)", result["output"].rstrip("\n"),
                  sep="\n", flush=True)
    failed = []
    for result in results:
        if result["status"] != 0: failed.append(f"{result['name']} (status {result['status']})")
        elif result["ran"] != result["expected"]:
            # A process that ran other than the tests it was given did not run what it was given.
            failed.append(f"{result['name']} (ran {result['ran']} of {result['expected']} tests)")
    recorded = dict(seconds)
    for result in results: recorded.update(class_seconds(result["output"]))
    try:
        timings.parent.mkdir(parents=True, exist_ok=True)
        timings.write_text(json.dumps(recorded, indent=1, sort_keys=True) + "\n")
    except OSError as error:
        print(f"eng xctest: the classes' times were not kept: {error}", flush=True)
    total = sum(result["ran"] for result in results)
    skipped = sum(result["skipped"] for result in results)
    # Not XCTest's or Swift Testing's own total: the processes' lines already count every test once.
    print(f"eng xctest: {total} tests ({skipped} skipped) in {len(results)} processes, {args.jobs} at a time, "
          f"{time.monotonic() - began:.1f}s: " + (f"FAILED: {', '.join(failed)}" if failed else "OK"), flush=True)
    return 1 if failed else 0
