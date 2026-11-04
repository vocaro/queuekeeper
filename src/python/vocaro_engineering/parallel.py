"""Independent work side by side: a lane group's lanes, and a unittest suite's modules in processes
of their own. guides/parallel-tests.md says when each is safe and how to bound it.

A lane group runs inside the one admission its own lane took, as nested work does, so its lanes
take no slot of their own. Each lane writes its own log and receipt and keeps its own outcome; the
group fails when any lane failed, and is otherwise unmeasured when any lane was.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
import unittest

from . import evidence, processes

# Lines of a failed lane's log printed under its result.
TAIL = 40
# Processes `eng unittest` runs at a time unless --jobs says otherwise (guides/parallel-tests.md).
DEFAULT_JOBS = 4
# unittest's own exit status for a run that found no tests (Python 3.12 and later).
NO_TESTS = 5
# Seconds one test process of `eng unittest` or `eng xctest` may run before it is stopped and left
# unmeasured, unless --timeout says otherwise: a backstop for a hang, which would otherwise hold the
# whole run until its lane's limit. More than twice the slowest of the toolkit's own, test_remote's
# 817 s at one-minute load averages of 100 to 280 on this Mac (2026-10-31).
PROCESS_LIMIT = 1800
# The variable that gives a lane's command the absolute path of its results/ directory, which `eng run` empties
# before each run and brings back from the second Mac (guides/parallel-tests.md, A lane's results).
RESULTS_VARIABLE = "ENG_RESULTS_DIR"


def outcome(status):
    return "PASS" if status == 0 else "UNMEASURED" if status == evidence.UNMEASURED else "FAIL"


def combined(statuses):
    """1 when any status failed, else UNMEASURED when any was unmeasured, else 0."""
    if any(status not in (0, evidence.UNMEASURED) for status in statuses): return 1
    return evidence.UNMEASURED if evidence.UNMEASURED in statuses else 0


def results_environment(directory, base=None):
    """The environment of a lane's command, with RESULTS_VARIABLE naming directory, its results/, made if missing."""
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    return {**(os.environ if base is None else base), RESULTS_VARIABLE: str(directory)}


def lanes(root, members, directory, jobs, judge, receipt, on_stop=None, results_directory=None):
    """Run members, (name, lane) pairs, jobs at a time, each with its log at directory/NAME.log and
    its receipt at directory/NAME.json, and return the combined status. judge(lane, status, log) is
    a lane's status from its command's and its log, raising ValueError for a run that executed too
    few tests. With results_directory, the group's results/, each lane's command finds results/NAME/ in
    RESULTS_VARIABLE, so that what it writes there comes back with the group's from the second Mac.

    Every lane runs to its own end, however the others end. A stop reaches every running lane and
    starts none of the rest; once all have ended the stop is raised, or the uncertainty of a lane
    whose processes were not seen to end, which keeps the admission's guard. A lane that a stop or
    that uncertainty ended has its receipt too, with no status.
    """
    directory.mkdir(parents=True, exist_ok=True)
    results, lock, started = {}, threading.Lock(), time.monotonic()

    def say(*lines):
        with lock: print(*lines, sep="\n", flush=True)

    def conclude(name, lane, log, began, status, verdict, detail):
        """Record a lane's end in the group's receipt and in its own, beside its log, with the tests
        its log counts, so that its counts read from its files wherever they are copied."""
        seconds = round(time.monotonic() - began)
        found = evidence.counted(log.read_text(errors="replace")) if log.exists() else None
        result = {"status": status, "outcome": verdict, "seconds": seconds, "log": str(log),
                  "executedTests": None if found is None else found[0] - found[1],
                  "skippedTests": None if found is None else found[1], **({"error": detail} if detail else {})}
        results[name] = result
        # The log's name, not its path: the receipt is read beside it, on another Mac too.
        (directory / f"{name}.json").write_text(json.dumps(
            {"lane": name, "command": lane["command"], **result, "log": log.name}, indent=2) + "\n")
        return seconds

    def one(name, lane):
        log = directory / f"{name}.log"
        say(f"RUN {name}")
        began = time.monotonic()
        try:
            status = processes.run(lane["command"], cwd=root, log_path=log,
                                   timeout=lane.get("timeout", processes.DEFAULT_LIMIT),
                                   on_stop=on_stop,
                                   env=results_environment(results_directory / name) if results_directory is not None else None)
            status = judge(lane, status, log.read_text(errors="replace"))
        except processes.TimedOut as error:
            # A lane's timeout is its backstop, so a run a busy host slowed past it is unmeasured.
            status, detail = evidence.UNMEASURED, str(error)
        except processes.NotStarted as error:
            status, detail = 2, str(error)
        except ValueError as error:
            status, detail = 2, str(error)
        except BaseException as error:
            # A stop, or processes not seen to end, leaves the lane with no status of its own.
            stopped = isinstance(error, processes.Interrupted)
            seconds = conclude(name, lane, log, began, None, "stopped" if stopped else "failed-or-interrupted",
                               str(error))
            say(f"{'STOPPED' if stopped else 'UNCERTAIN'} {name} ({seconds}s): {log}", f"    {error}")
            raise
        else:
            detail = None
        verdict = "passed" if status == 0 else "unmeasured" if status == evidence.UNMEASURED else "failed"
        seconds = conclude(name, lane, log, began, status, verdict, detail)
        lines = [f"{outcome(status)} {name} ({seconds}s, status {status}): {log}"]
        if detail: lines.append(f"    {detail}")
        if outcome(status) != "PASS":
            lines += ["    " + line for line in log.read_text(errors="replace").splitlines()[-TAIL:]] if log.exists() else []
        say(*lines)

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = [pool.submit(one, name, lane) for name, lane in members]
    failures = [future.exception() for future in futures if future.exception() is not None]
    receipt["lanes"] = results
    if failures:
        uncertain = [error for error in failures if not isinstance(error, processes.Interrupted)]
        raise uncertain[0] if uncertain else failures[0]
    status = combined([results[name]["status"] for name, _lane in members])
    width = max(len(name) for name, _lane in members)
    say(f"Lane times (wall-clock {round(time.monotonic() - started)}s, {jobs} at a time):",
        *(f"  {name:<{width}}  {results[name]['seconds']:>6}s  {outcome(results[name]['status'])}"
          for name, _lane in members), outcome(status))
    return status


def flattened(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite): yield from flattened(test)
        else: yield test


class OriginLoader(unittest.TestLoader):
    """A TestLoader that records, by each test's id(), the module discovery loaded it from: the
    test's own module, or the one whose load_tests loaded the test's module itself."""
    def __init__(self):
        super().__init__()
        self.origins, self.depth = {}, 0

    def loadTestsFromModule(self, module, *args, **kwargs):
        self.depth += 1
        try: suite = super().loadTestsFromModule(module, *args, **kwargs)
        finally: self.depth -= 1
        if not self.depth:
            for test in flattened(suite): self.origins.setdefault(id(test), module.__name__)
        return suite


def addressable(test):
    """Whether a fresh process can import the test's module by its name, as `python3 -m unittest`
    does with the test's id. A module that a load_tests loaded from its file is not in sys.modules."""
    return sys.modules.get(type(test).__module__) is not None


# What one process runs, from coarsest to finest, as --split and a module's shard_by name it.
SPLITS = ("module", "class", "test")


def shards(suite, split, origins=None):
    """{shard name: [test ids]}, in discovery order. A shard is a module, class or test, as split
    says, or finer where a module sets shard_by to "class" or "test": a module that alone bounds a
    run splits itself without every module splitting. Tests whose class or module sets serial_group
    share one shard, so they run one after another in one process. A module that failed to import is a shard of its own, which reports
    the import error when it runs. A module whose load_tests loads a module that a fresh process
    cannot import by name cannot be split: it runs whole in one shard, addressed by its own name,
    which stands in for each of its tests."""
    tests, origins = list(flattened(suite)), origins or {}
    whole = {origins[id(test)] for test in tests if id(test) in origins and not addressable(test)}
    found = {}
    for test in tests:
        cls = type(test)
        module = sys.modules.get(cls.__module__)
        if cls.__module__ == "unittest.loader":
            # A _FailedTest's name is the module or package that failed to load.
            name = test.id().rsplit(".", 1)[-1]
            found.setdefault(name, []).append(name)
            continue
        origin = origins.get(id(test))
        if origin in whole:
            group = getattr(sys.modules.get(origin), "serial_group", None)
            found.setdefault(f"group:{group}" if group else origin, []).append(origin)
            continue
        finest = max(split, getattr(module, "shard_by", "module"), key=SPLITS.index)
        group = getattr(module, "serial_group", None) if finest == "module" else \
            getattr(cls, "serial_group", getattr(module, "serial_group", None))
        unit = {"module": cls.__module__, "class": f"{cls.__module__}.{cls.__qualname__}", "test": test.id()}[finest]
        found.setdefault(f"group:{group}" if group else unit, []).append(test.id())
    return found


RAN = re.compile(r"^Ran (\d+) tests? in ", re.M)
RESULT = re.compile(r"^(OK|FAILED)\b.*$", re.M)


# `python3 -m unittest` with its first argument, the top-level directory, first on sys.path.
SHARD = "import sys, unittest; sys.path.insert(0, sys.argv.pop(1)); sys.argv[0] = 'python -m unittest'; " \
    "unittest.main(module=None)"


def run_shard(name, tests, top, verbose, descriptors=(), timeout=PROCESS_LIMIT):
    """Run a shard's tests in a process of its own, which inherits descriptors and no other. It runs
    in this process's directory, with top and then that directory on its sys.path, as `python3 -m
    unittest discover` does. A name listed more than once runs once. One still running after timeout
    seconds is stopped, and its status is None."""
    command = [sys.executable, "-c", SHARD, str(top), *(["-v"] if verbose else []), *dict.fromkeys(tests)]
    began = time.monotonic()
    status, output = processes.bounded(command, timeout=timeout, pass_fds=descriptors)
    summary = RESULT.search(output)
    return {"name": name, "status": status, "output": output,
            "ran": sum(int(count) for count in RAN.findall(output)),
            "result": f"timed out after {timeout:g}s" if status is None
                      else summary.group(0) if summary else "no result",
            "seconds": time.monotonic() - began}


def unittest_main(argv):
    parser = argparse.ArgumentParser(prog="eng unittest", description="Discover a unittest suite as `python3 -m "
        "unittest discover` does, and run its modules in processes of their own, JOBS at a time.")
    parser.add_argument("-s", "--start-directory", default=".")
    parser.add_argument("-p", "--pattern", default="test*.py")
    parser.add_argument("-t", "--top-level-directory")
    parser.add_argument("-j", "--jobs", type=int, default=DEFAULT_JOBS,
                        help=f"processes at a time (default {DEFAULT_JOBS}); 1 runs them one after another")
    parser.add_argument("--split", choices=SPLITS, default="module",
                        help="what one process runs (default: one module)")
    parser.add_argument("--timeout", type=float, default=PROCESS_LIMIT, metavar="SECONDS",
                        help=f"seconds one process may run before it is stopped and left unmeasured "
                        f"(default {PROCESS_LIMIT})")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    if args.jobs < 1: parser.error("--jobs needs at least 1")
    if args.timeout <= 0: parser.error("--timeout needs a positive number of seconds")
    start = Path(args.start_directory).resolve()
    top = Path(args.top_level_directory).resolve() if args.top_level_directory else start
    # Discovery imports every module here, as unittest's own would, and each shard imports its own
    # again: from the top-level directory, then the directory eng runs in (#74).
    for directory in (os.getcwd(), str(top)):
        if directory not in sys.path: sys.path.insert(0, directory)
    loader = OriginLoader()
    found = shards(loader.discover(str(start), args.pattern, str(top)), args.split, loader.origins)
    if not found:
        print(f"eng unittest: no tests found in {start} matching {args.pattern}", file=sys.stderr)
        return NO_TESTS
    expected = {name: len(tests) for name, tests in found.items()}
    # Each shard holds the admission lock descriptors this process holds and no others, as a test in
    # `python3 -m unittest discover` does, so a nested admission in it reuses the admission; given
    # the token alone, one was refused (#70).
    descriptors = processes.held_locks(os.environ)
    began = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = [pool.submit(run_shard, name, tests, top, args.verbose, descriptors, args.timeout)
                   for name, tests in found.items()]
        results = []
        for future in futures:
            result = future.result()
            results.append(result)
            # Each shard's own output, whole and in discovery order, under one line that names it.
            print(f"== {result['name']}: {result['result']} ({result['ran']} tests, {result['seconds']:.1f}s)",
                  result["output"].rstrip("\n"), sep="\n", flush=True)
    failed, unmeasured = [], []
    for result in results:
        if result["status"] is None: unmeasured.append(f"{result['name']} ({result['result']})")
        elif result["status"] != 0: failed.append(f"{result['name']} ({result['result']}, status {result['status']})")
        elif result["ran"] != expected[result["name"]]:
            # A shard that ran other than the tests discovery found for it did not run what it was given.
            failed.append(f"{result['name']} (ran {result['ran']} of {expected[result['name']]} tests)")
    total = sum(result["ran"] for result in results)
    skipped = sum(evidence.skipped_tests(result["output"]) for result in results)
    # Not "Ran N tests": the shards' own lines already count every test once.
    print(f"eng unittest: {total} tests ({skipped} skipped) in {len(results)} processes, {args.jobs} at a time, "
          f"{time.monotonic() - began:.1f}s: " + verdict(failed, unmeasured), flush=True)
    return 1 if failed else evidence.UNMEASURED if unmeasured else 0


def verdict(failed, unmeasured):
    """A runner's last words: OK, or what failed and what a timeout left unmeasured."""
    words = ([f"FAILED: {', '.join(failed)}"] if failed else []) + \
        ([f"UNMEASURED: {', '.join(unmeasured)}"] if unmeasured else [])
    return "; ".join(words) or "OK"
