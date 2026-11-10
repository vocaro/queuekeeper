"""Independent work side by side: a lane group's lanes, and a unittest suite's modules in processes
of their own. guides/parallel-tests.md says when each is safe and how to bound it.

A lane group runs inside the one admission its own lane took, as nested work does, so its lanes
take no slot of their own. Each lane writes its own log and receipt and keeps its own outcome; the
group fails when any lane failed, and is otherwise unmeasured when any lane was.
"""
import argparse
import ast
from concurrent.futures import ThreadPoolExecutor
import fnmatch
import glob
import json
import os
from pathlib import Path
import re
import shutil
import sys
import threading
import time
import unittest

from . import evidence, processes, stale_modules

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
# What eng unittest repeats after its summary, so that output cut by `tail` still says why each test failed
# (vocaro-engineering#264): at most this many failures, each name and reason at most this many characters. Together
# they stay well under the TAIL lines a failed lane shows, and that lane's summary stays among them.
REASONS_SHOWN = 20
REASON_WIDTH = 300


def outcome(status):
    return "PASS" if status == 0 else "UNMEASURED" if status == evidence.UNMEASURED else "FAIL"


def lane_timeout(lane, arguments=()):
    """The direct lane's command limit, scaled by the requested number of repeats."""
    return lane.get("timeout", processes.DEFAULT_LIMIT) * (evidence.repeats_asked(arguments) or 1)


def combined(statuses):
    """1 when any status failed, else UNMEASURED when any was unmeasured, else 0."""
    if any(status not in (0, evidence.UNMEASURED) for status in statuses): return 1
    return evidence.UNMEASURED if evidence.UNMEASURED in statuses else 0


def results_environment(directory, base=None):
    """The environment of a lane's command, with RESULTS_VARIABLE naming directory, its results/, made if missing."""
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    return {**(os.environ if base is None else base), RESULTS_VARIABLE: str(directory)}


def hand_up(results, lane, environment=None):
    """Copy a nested lane's results/ into results/LANE/ of the lane it runs inside, which RESULTS_VARIABLE
    names in environment (os.environ), and return that folder, or None when there is nothing to copy or
    no lane outside it. A gate whose script runs `eng run --here suites` then brings the suites' lanes'
    results, such as each iOS shard's per-test lines, back with its own dispatch from the second Mac and
    keeps them by commit with its receipt (vocaro-engineering#218)."""
    outer = (os.environ if environment is None else environment).get(RESULTS_VARIABLE)
    if not outer: return None
    results, outer = Path(results).resolve(), Path(outer).resolve()
    # A lane's own results/, or one inside them, is not another lane's to copy into.
    if outer == results or results in outer.parents or outer in results.parents: return None
    try:
        if not results.is_dir() or not any(results.iterdir()): return None
        target = outer / lane
        shutil.copytree(results, target, symlinks=True, dirs_exist_ok=True)
    except OSError as error:
        print(f"eng: {lane}'s results were not copied into {outer}: {error}", file=sys.stderr, flush=True)
        return None
    return target


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
                                   env=stale_modules.environment(results_environment(results_directory / name)
                                       if results_directory is not None else os.environ, root))
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


def shards(suite, split, origins=None, members=None, *, batch_size=None):
    """{shard name: [test ids]}, in discovery order. A shard is a module, class or test, as split
    says, or finer where a module sets shard_by to "class" or "test": a module that alone bounds a
    run splits itself without every module splitting. Tests whose class or module sets serial_group
    share one shard, so they run one after another in one process. A module that failed to import is a shard of its own, which reports
    the import error when it runs. A module whose load_tests loads a module that a fresh process
    cannot import by name cannot be split: it runs whole in one shard, addressed by its own name,
    which stands in for each of its tests. With batch_size, bound the number of tests in each class shard.
    A members dict gets each shard's test ids."""
    tests, origins = list(flattened(suite)), origins or {}
    found = {}
    class_counts = {}

    def add(shard, entry, test):
        found.setdefault(shard, []).append(entry)
        if members is not None: members.setdefault(shard, []).append(test.id())

    whole = {origins[id(test)] for test in tests if id(test) in origins and not addressable(test)}
    for test in tests:
        cls = type(test)
        module = sys.modules.get(cls.__module__)
        if cls.__module__ == "unittest.loader":
            # A _FailedTest's name is the module or package that failed to load.
            name = test.id().rsplit(".", 1)[-1]
            add(name, name, test)
            continue
        origin = origins.get(id(test))
        if origin in whole:
            group = getattr(sys.modules.get(origin), "serial_group", None)
            add(f"group:{group}" if group else origin, origin, test)
            continue
        finest = max(split, getattr(module, "shard_by", "module"), key=SPLITS.index)
        group = getattr(module, "serial_group", None) if finest == "module" else \
            getattr(cls, "serial_group", getattr(module, "serial_group", None))
        unit = {"module": cls.__module__, "class": f"{cls.__module__}.{cls.__qualname__}", "test": test.id()}[finest]
        if batch_size is not None and finest == "class" and not group:
            count = class_counts.get(unit, 0)
            class_counts[unit] = count + 1
            unit = f"{unit}[{count // batch_size + 1}]"
        add(f"group:{group}" if group else unit, test.id(), test)
    return found


RAN = re.compile(r"^Ran (\d+) tests? in ", re.M)
RESULT = re.compile(r"^(OK|FAILED)\b.*$", re.M)
# unittest's heading of one failure, between its two separator lines, with a subtest's parameters after it.
FAILURE = re.compile(r"^(?:FAIL|ERROR): (\S+) \(([^()\s]+)\)(.*)$")
TRACEBACK = "Traceback (most recent call last):"
CHAINED = ("During handling of the above exception, another exception occurred:",
           "The above exception was the direct cause of the following exception:")


# `python3 -m unittest` with its first argument, the top-level directory, first on sys.path, and
# its second, a JSON list of fnmatch patterns or null, as its loader's testNamePatterns: unlike
# -k, a pattern is used as it is, so an escaped test id selects that test alone.
SHARD = "import json, sys, unittest; sys.path.insert(0, sys.argv.pop(1)); " \
    "unittest.defaultTestLoader.testNamePatterns = json.loads(sys.argv.pop(1)); " \
    "sys.argv[0] = 'python -m unittest'; unittest.main(module=None)"


def selection(patterns):
    """unittest's -k patterns as fnmatch patterns: one without a * matches as a substring."""
    return [pattern if "*" in pattern else f"*{pattern}*" for pattern in patterns]


def selected(test, patterns, names):
    """Whether a discovered test is selected: its id matches one of patterns, fnmatch patterns, when
    there are any, and is one of names, or under one, when there are any. A module that failed to
    import is always selected, so that its error reports as it would without a selection."""
    if type(test).__module__ == "unittest.loader": return True
    identity = test.id()
    return (not patterns or any(fnmatch.fnmatchcase(identity, pattern) for pattern in patterns)) and \
        (not names or any(identity == name or identity.startswith(f"{name}.") for name in names))


def run_shard(name, tests, top, verbose, descriptors=(), timeout=PROCESS_LIMIT, chosen=None):
    """Run a shard's tests in a process of its own, which inherits descriptors and no other. It runs
    in this process's directory, with top and then that directory on its sys.path, as `python3 -m
    unittest discover` does. A name listed more than once runs once. With chosen, the ids of the
    shard's selected tests, a module the shard names whole runs only those. One still running after
    timeout seconds is stopped, and its status is None."""
    patterns = None if chosen is None else [glob.escape(identity) for identity in dict.fromkeys(chosen)]
    command = [sys.executable, "-c", SHARD, str(top), json.dumps(patterns), *(["-v"] if verbose else []),
               *dict.fromkeys(tests)]
    began = time.monotonic()
    status, output = processes.bounded(command, timeout=timeout, pass_fds=descriptors)
    summary = RESULT.search(output)
    return {"name": name, "tests": list(tests), "status": status, "output": output,
            "ran": sum(int(count) for count in RAN.findall(output)),
            "result": f"timed out after {timeout:g}s" if status is None
                      else summary.group(0) if summary else "no result",
            "seconds": time.monotonic() - began}


def last_exception(lines):
    """The first line of the exception that ended a failure's traceback, the last of a chain of them, from lines, the
    failure's text; or its last line that says anything when it holds no traceback of that form; or None."""
    last = next((line for line in reversed(lines) if line.strip()), None)
    start = next((index for index, line in enumerate(lines) if line.startswith(TRACEBACK)), None)
    while start is not None:
        index = start + 1
        while index < len(lines) and lines[index].startswith(" "): index += 1
        if index == len(lines) or not lines[index].strip(): return last
        marker = next((number for number in range(index + 1, len(lines)) if lines[number] in CHAINED), None)
        if marker is None or marker + 2 >= len(lines) or not lines[marker + 2].startswith(TRACEBACK):
            return lines[index]
        start = marker + 2
    return last


def failure_reasons(output, tests):
    """[(failed test, the first line of why)] for each failure unittest reports in output, the output of a process
    that ran tests, its module, class or test names. A test's failure message can quote the output of a run it made,
    and so another run's failures: those name a test that is not among tests, and are left out, unless none is left."""
    lines, found, opener, closer = output.splitlines(), [], unittest.TextTestResult.separator1, \
        unittest.TextTestResult.separator2
    for index, line in enumerate(lines):
        heading = FAILURE.match(lines[index + 1]) if line == opener and index + 1 < len(lines) else None
        if not heading: continue
        # A test's docstring takes a line of its own under the heading.
        start = next((number for number in range(index + 2, min(index + 4, len(lines))) if lines[number] == closer), None)
        if start is None: continue
        end = next((number for number in range(start + 1, len(lines)) if lines[number] in (opener, closer)), len(lines))
        method, where, parameters = heading.groups()
        # unittest reports a module that did not load as a test of its loader named after the module.
        loader = where.startswith("unittest.")
        identity = method if loader else where if where.endswith(f".{method}") else f"{where}.{method}"
        own = method in tests if loader else \
            any(name == where or where.startswith(f"{name}.") or name.startswith(f"{where}.") for name in tests)
        found.append((own, identity + parameters, last_exception(lines[start + 1:end]) or "it printed no reason"))
    return [item[1:] for item in found if item[0]] or [item[1:] for item in found]


def clipped(text, width):
    """text, at most about width characters. What is cut is said, and it ends at a word, as a path cut short could
    read as another path to the merge queue, which looks in a failing test's output for the paths a change touches."""
    if len(text) <= width: return text
    kept = text[:width].rsplit(None, 1)[0] if len(text[:width].split(None, 1)) > 1 else ""
    return f"{kept} [cut: {len(text) - len(kept)} more characters]".lstrip()


def reason_lines(reasons, saved):
    """What eng unittest prints after its summary for reasons, [(failed test, why)]; saved says where the whole output
    is. Nothing for no reasons. Each reason is one line, indented, so that nothing in it can start a line the merge
    queue reads as a process's header, a test's failure or a summary (merge_queue.failed_tests, UNITTEST_SUMMARY)."""
    if not reasons: return []
    shown = reasons[:REASONS_SHOWN]
    lines = [f"Why each failed, the first line of each reason; {saved}:"]
    lines += [f"    {clipped(name, REASON_WIDTH)}: {clipped(why, REASON_WIDTH)}" for name, why in shown]
    if len(reasons) > len(shown):
        lines.append(f"    [cut: {len(reasons) - len(shown)} more failures of {len(reasons)}; the whole output has them]")
    return lines


def unittest_main(argv):
    parser = argparse.ArgumentParser(prog="eng unittest", description="Discover a unittest suite as `python3 -m "
        "unittest discover` does, and run its modules in processes of their own, JOBS at a time.")
    parser.add_argument("-s", "--start-directory", default=".")
    # Repeatable: each -p adds the modules it matches. A second -p once replaced the first without a word, so a run
    # meant for three modules ran one and reported its count as the whole (vocaro-engineering, 2026-11-06).
    parser.add_argument("-p", "--pattern", action="append", metavar="PATTERN",
                        help="a module file pattern, as unittest discover takes; repeat it for several (default test*.py)")
    parser.add_argument("-t", "--top-level-directory")
    parser.add_argument("-j", "--jobs", type=int, default=DEFAULT_JOBS,
                        help=f"processes at a time (default {DEFAULT_JOBS}); 1 runs them one after another")
    parser.add_argument("--split", choices=SPLITS, default="module",
                        help="what one process runs (default: one module)")
    parser.add_argument("--batch-size", type=int, metavar="TESTS",
                        help="maximum tests in one class process with --split class")
    parser.add_argument("--timeout", type=float, default=PROCESS_LIMIT, metavar="SECONDS",
                        help=f"seconds one process may run before it is stopped and left unmeasured "
                        f"(default {PROCESS_LIMIT})")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("-k", dest="selectors", action="append", default=[], metavar="PATTERN",
                        help="run only tests whose id matches PATTERN, as unittest's -k does: an fnmatch "
                        "pattern, or a substring when it has no *; repeat it to run tests matching any")
    parser.add_argument("names", nargs="*", metavar="NAME",
                        help="run only this module, class or test and what it holds, such as "
                        "test_merge_queue.Landing; each must name a discovered test")
    args = parser.parse_args(argv)
    if args.jobs < 1: parser.error("--jobs needs at least 1")
    if args.batch_size is not None and (args.batch_size < 1 or args.split != "class"):
        parser.error("--batch-size needs a positive count and --split class")
    if args.timeout <= 0: parser.error("--timeout needs a positive number of seconds")
    start = Path(args.start_directory).resolve()
    top = Path(args.top_level_directory).resolve() if args.top_level_directory else start
    # Before discovery imports any of them: a suite that keeps its tests off the real hosts in host_isolation.py
    # runs no module that skips it, which otherwise only the full suite's own check found, in the merge queue.
    refused = refusals(start, args.pattern)
    if refused:
        for line in refused: print(line, file=sys.stderr)
        return 1
    # Discovery imports every module here, as unittest's own would, and each shard imports its own
    # again: from the top-level directory, then the directory eng runs in (#74).
    for directory in (os.getcwd(), str(top)):
        if directory not in sys.path: sys.path.insert(0, directory)
    loader, patterns = OriginLoader(), args.pattern or ["test*.py"]
    # A test two patterns both match runs once: the first pattern's copy of it is kept.
    tests, seen = [], set()
    for pattern in patterns:
        for test in flattened(loader.discover(str(start), pattern, str(top))):
            if test.id() not in seen:
                seen.add(test.id())
                tests.append(test)
    matching, selectors, chosen = " or ".join(patterns), selection(args.selectors), None
    if selectors or args.names:
        ids = {test.id() for test in tests}
        unmatched = [name for name in args.names
                     if not any(identity == name or identity.startswith(f"{name}.") for identity in ids)]
        if unmatched:
            print(f"eng unittest: no test discovered in {start} matching {matching} is or is under "
                  f"{', '.join(unmatched)}", file=sys.stderr)
            return NO_TESTS
        tests = [test for test in tests if selected(test, selectors, args.names)]
        if not any(type(test).__module__ != "unittest.loader" for test in tests):
            print(f"eng unittest: no test discovered in {start} matching {matching} is selected by "
                  + " and ".join(([f"-k {' or '.join(args.selectors)}"] if args.selectors else [])
                                 + ([" or ".join(args.names)] if args.names else [])), file=sys.stderr)
            return NO_TESTS
        # Each shard's selected ids, which a module it names whole passes to its process.
        chosen = {}
    found = shards(unittest.TestSuite(tests), args.split, loader.origins, chosen, batch_size=args.batch_size)
    if not found:
        print(f"eng unittest: no tests found in {start} matching {' or '.join(patterns)}", file=sys.stderr)
        return NO_TESTS
    expected = {name: len(tests) for name, tests in found.items()}
    # Each shard holds the admission lock descriptors this process holds and no others, as a test in
    # `python3 -m unittest discover` does, so a nested admission in it reuses the admission; given
    # the token alone, one was refused (#70).
    descriptors = processes.held_locks(os.environ)
    began = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = [pool.submit(run_shard, name, tests, top, args.verbose, descriptors, args.timeout,
                               None if chosen is None else chosen[name])
                   for name, tests in found.items()]
        results, report = [], []
        for future in futures:
            result = future.result()
            results.append(result)
            # Each shard's own output, whole and in discovery order, under one line that names it.
            report += [f"== {result['name']}: {result['result']} ({result['ran']} tests, {result['seconds']:.1f}s)",
                       result["output"].rstrip("\n")]
            print(*report[-2:], sep="\n", flush=True)
    failed, unmeasured, reasons = [], [], []

    def fail(result, detail):
        failed.append(f"{result['name']} ({detail})")
        ended = next((line for line in reversed(result["output"].splitlines()) if line.strip()), None)
        reasons.extend(failure_reasons(result["output"], result["tests"])
                       or [(result["name"], f"its output ends: {ended}" if ended else "it printed nothing")])

    for result in results:
        if result["status"] is None: unmeasured.append(f"{result['name']} ({result['result']})")
        elif result["status"] != 0: fail(result, f"{result['result']}, status {result['status']}")
        elif result["ran"] != expected[result["name"]]:
            # A shard that ran other than the tests discovery found for it did not run what it was given.
            fail(result, f"ran {result['ran']} of {expected[result['name']]} tests")
    total = sum(result["ran"] for result in results)
    skipped = sum(evidence.skipped_tests(result["output"]) for result in results)
    # Not "Ran N tests": the shards' own lines already count every test once.
    summary = (f"eng unittest: {total} tests ({skipped} skipped) in {len(results)} processes, {args.jobs} at a time, "
               f"{time.monotonic() - began:.1f}s: " + verdict(failed, unmeasured))
    print(summary, flush=True)
    if failed:
        for line in reason_lines(reasons, saved_output(report + [summary], reasons)): print(line, flush=True)
    return 1 if failed else evidence.UNMEASURED if unmeasured else 0


def saved_output(report, reasons):
    """Keep report, a failed run's lines, as a failure record of the directory eng runs in, which the record's rule
    prunes, and return words saying where it is, or why it is not: either way the run's result stands."""
    from . import failure_records  # failure_records imports this module
    try: return f"the whole output is in {failure_records.keep_output(Path.cwd(), 'unittest', report, reasons)}"
    except (OSError, ValueError) as error: return f"the whole output was not saved: {error}"


ISOLATION = "host_isolation.py"


def refusals(start, patterns=None):
    """The lines eng unittest refuses a suite with before discovery imports any of its modules, each ending `nothing
    ran`, or none. In a suite that keeps its tests off the real hosts in host_isolation.py, a module patterns match
    that does not parse, or that does not import host_isolation at its top level, refuses the run. eng land and `eng
    queue add` run these on an entry's tree before queuing it, as they take seconds and a refusal costs a whole gate
    (merge_queue.prerun, vocaro-engineering#290)."""
    if not (start / ISOLATION).is_file(): return []
    paths, broken = sorted({path for pattern in patterns or ["test*.py"] for path in start.glob(pattern)}), {}
    for path in paths:
        try: ast.parse(path.read_text(), str(path))
        except (SyntaxError, ValueError) as error:
            line = getattr(error, "lineno", None)
            broken[path] = f"{path.name} does not parse ({getattr(error, 'msg', error)}" + \
                (f", line {line})" if line else ")")
    lines = [f"eng unittest: {why}; nothing ran" for why in broken.values()]
    forgot = unisolated([path for path in paths if path not in broken])
    if forgot:
        lines.append(f"eng unittest: {', '.join(forgot)} must import {ISOLATION.removesuffix('.py')} at its top level, "
                     f"as every test module beside {start / ISOLATION} does; nothing ran")
    return lines


def unisolated(paths):
    """The names of the test modules among paths that do not import host_isolation at their top level."""
    found = []
    for path in paths:
        tree = ast.parse(Path(path).read_text(), str(path))
        if not any(isinstance(node, ast.Import) and any(alias.name == "host_isolation" for alias in node.names)
                   for node in tree.body):
            found.append(Path(path).name)
    return found


def verdict(failed, unmeasured):
    """A runner's last words: OK, or what failed and what a timeout left unmeasured."""
    words = ([f"FAILED: {', '.join(failed)}"] if failed else []) + \
        ([f"UNMEASURED: {', '.join(unmeasured)}"] if unmeasured else [])
    return "; ".join(words) or "OK"
