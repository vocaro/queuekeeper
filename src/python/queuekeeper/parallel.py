"""Independent work side by side: a lane group's lanes, and a unittest suite's modules in processes
of their own. doc/guides/parallel-tests.md says when each is safe and how to bound it.

A lane group runs inside the one admission its own lane took, as nested work does, so its lanes
take no slot of their own. Each lane writes its own log and receipt and keeps its own outcome; the
group fails when any lane failed, and is otherwise unmeasured when any lane was.
"""
import argparse
import ast
import concurrent.futures
import contextlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import fnmatch
import glob
import io
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import unittest

from . import extensions, lane_results, processes, resources, run_progress, shims

# Lines of a failed lane's log printed under its result.
TAIL = 40
# Processes `qk unittest` runs at a time unless --jobs says otherwise (doc/guides/parallel-tests.md).
DEFAULT_JOBS = 4
# unittest's own exit status for a run that found no tests (Python 3.12 and later).
NO_TESTS = 5
# Seconds one test process of `qk unittest` or `qk xctest` may run before it is stopped and left
# unmeasured, unless --timeout says otherwise: a backstop for a hang, which would otherwise hold the
# whole run until its lane's limit. More than twice the slowest of the toolkit's own, test_remote's
# 817 s at one-minute load averages of 100 to 280 on this Mac (2026-10-31).
PROCESS_LIMIT = 1800
# The variable that gives a lane's command the absolute path of its results/ directory, which `qk run` empties
# before each run and brings back from another Mac (doc/guides/parallel-tests.md, A lane's results).
RESULTS_VARIABLE = "QUEUEKEEPER_RESULTS_DIR"
# Set in each test process qk unittest starts, so that a suite's shared setup can tell that run from a bare `python3 -m
# unittest`, which takes no slot (tests/host_isolation.py, vocaro/queuekeeper#36).
SHARD_VARIABLE = "QUEUEKEEPER_UNITTEST_PROCESS"
# What qk unittest repeats after its summary, so that output cut by `tail` still says why each test failed
# (vocaro-engineering#264): at most this many failures, each name and reason at most this many characters. Together
# they stay well under the TAIL lines a failed lane shows, and that lane's summary stays among them.
REASONS_SHOWN = 20
REASON_WIDTH = 300
# What a --fail-fast run's verdict adds, after what failed, once a failed test failed again alone and the run stopped
# without the rest, and what it calls the processes that stop left. The merge queue reads it: such a run's failed
# tests are blamed as any are, but a batch whose failures all pass alone gates again rather than land, as the rest of
# its suite never ran (merge_queue.failed_tests, Worker.rerun).
STOPPED = "STOPPED: "
HALTED = "stopped after a failed test failed again alone"
# Seconds the git command that finds a --fail-fast run's failure history may take (history_place).
GIT_SECONDS = 30
# The file in a repository's common git directory that keeps when each test a --fail-fast run saw fail last failed,
# by suite, and how long and how many of them it keeps (record_failures).
HISTORY_FILE = "queuekeeper-unittest-failures.json"
HISTORY_DAYS = 14
HISTORY_MOST = 500


def outcome(status):
    return "PASS" if status == 0 else "UNMEASURED" if status == lane_results.UNMEASURED else \
        "NOT RUN" if status == resources.BUSY else "FAIL"


def verdict_of(status):
    """A lane's outcome as its receipt records it: a lane that exited resources.BUSY was not run, for a reason of its
    host's such as a locked screen, and says nothing of the commits (vocaro/queuekeeper#200)."""
    return "passed" if status == 0 else "unmeasured" if status == lane_results.UNMEASURED else \
        "not run" if status == resources.BUSY else "failed"


def lane_timeout(lane, arguments=()):
    """The direct lane's command limit, scaled by the requested number of repeats."""
    return lane.get("timeout", processes.DEFAULT_LIMIT) * (lane_results.repeats_asked(arguments) or 1)


def combined(statuses):
    """1 when any status failed, else resources.BUSY when any was not run for its host's reason, such as a locked
    screen, else UNMEASURED when any was unmeasured, else 0. A lane that was not run says nothing of the commits, so
    the group gives no verdict rather than failing them (vocaro/queuekeeper#200)."""
    if any(status not in (0, lane_results.UNMEASURED, resources.BUSY) for status in statuses): return 1
    if resources.BUSY in statuses: return resources.BUSY
    return lane_results.UNMEASURED if lane_results.UNMEASURED in statuses else 0


def results_environment(directory, base=None):
    """The environment of a lane's command, with RESULTS_VARIABLE naming directory, its results/, made if missing."""
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    return {**(os.environ if base is None else base), RESULTS_VARIABLE: str(directory)}


def hand_up(results, lane, environment=None):
    """Copy a nested lane's results/ into results/LANE/ of the lane it runs inside, which RESULTS_VARIABLE
    names in environment (os.environ), and return that folder, or None when there is nothing to copy or
    no lane outside it. A gate whose script runs `qk run --here suites` then brings the suites' lanes'
    results, such as each iOS shard's per-test lines, back with its own dispatch from another Mac and
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
        print(f"queuekeeper: {lane}'s results were not copied into {outer}: {error}", file=sys.stderr, flush=True)
        return None
    return target


def lanes(root, members, directory, jobs, judge, receipt, on_stop=None, results_directory=None):
    """Run members, (name, lane) pairs, jobs at a time, each with its log at directory/NAME.log and
    its receipt at directory/NAME.json, and return the combined status. judge(lane, status, log) is
    a lane's status from its command's and its log, raising ValueError for a run that executed too
    few tests. With results_directory, the group's results/, each lane's command finds results/NAME/ in
    RESULTS_VARIABLE, so that what it writes there comes back with the group's from another Mac.

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
        found = lane_results.counted(log.read_text(errors="replace")) if log.exists() else None
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
                                   **({"stall": lane["stall"], "progress_path": results_directory / name / "progress.json"
                                      if results_directory is not None else directory / f"{name}.progress.json"}
                                      if "stall" in lane else {}),
                                   env=shims.environment(results_environment(results_directory / name)
                                       if results_directory is not None else os.environ, root))
            status = judge(lane, status, log.read_text(errors="replace"))
        except processes.TimedOut as error:
            # A lane's timeout is its backstop, so a run a busy host slowed past it is unmeasured.
            status, detail = lane_results.UNMEASURED, str(error)
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
        seconds = conclude(name, lane, log, began, status, verdict_of(status), detail)
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


def loaded_from(module, folders):
    """Whether module was loaded from a file or package folder under one of folders."""
    places = [getattr(module, "__file__", None), *(getattr(module, "__path__", None) or ())]
    return any(isinstance(place, str) and (place == folder or place.startswith(folder.rstrip(os.sep) + os.sep))
               for place in places for folder in folders)


@contextlib.contextmanager
def tree_imports(roots):
    """While discovery imports the modules of the tree under test, whose folders are roots: no extension folder outside
    them on the import path, and none of the modules loaded from one, as a shard's own process has neither. A linked
    worktree's extensions load from its main checkout (cli.load_extensions), so a package that is both the project's
    and an extension's was otherwise the main checkout's copy, cached, and a test module importing a module or name
    only the tree under test has failed to import here though it imports in its shard (vocaro/queuekeeper#240). The
    extensions' own copies come back afterwards, for their hooks, which still run the main checkout's code."""
    # Never the folder this Queuekeeper runs from, whose modules are running this.
    roots = [Path(root).resolve() for root in (*roots, Path(__file__).resolve().parents[1])]
    foreign = [folder for folder in extensions.FOLDERS
               if not any(Path(folder).resolve().is_relative_to(root) for root in roots)]
    if not foreign:
        yield
        return
    path = list(sys.path)
    evicted = {name: module for name, module in list(sys.modules.items()) if loaded_from(module, foreign)}
    for name in evicted: del sys.modules[name]
    sys.path[:] = [entry for entry in sys.path if entry not in foreign]
    try: yield
    finally:
        # What discovery added to the path stays, after what was there; the tree's copies of the evicted packages go.
        sys.path[:] = path + [entry for entry in sys.path if entry not in path]
        packages = {name.split(".", 1)[0] for name in evicted}
        for name in [name for name in sys.modules if name.split(".", 1)[0] in packages]: del sys.modules[name]
        sys.modules.update(evicted)


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
# -k, a pattern is used as it is, so an escaped test id selects that test alone. It first puts SIGINT
# and SIGQUIT back to Python's defaults, which a background job's shell ignores, so that a test run
# behaves the same however queuekeeper was started; qk admit and qk run still pass on their caller's
# (the owner's decision, 2026-11-11, vocaro-engineering#385). It sets SHARD_VARIABLE before any test module loads.
SHARD = f"import json, os, signal, sys, unittest; os.environ[{SHARD_VARIABLE!r}] = '1'; " \
    "signal.signal(signal.SIGINT, signal.default_int_handler); " \
    "signal.signal(signal.SIGQUIT, signal.SIG_DFL); sys.path.insert(0, sys.argv.pop(1)); " \
    "unittest.defaultTestLoader.testNamePatterns = json.loads(sys.argv.pop(1)); " \
    "sys.argv[0] = 'python -m unittest'; unittest.main(module=None)"


def own_result(output):
    """A process's own result line, the last RESULT line in its output, which unittest prints last, or None. An
    earlier one can be quoted in a failure message, such as another run's output (vocaro/queuekeeper#242)."""
    found = None
    for found in RESULT.finditer(output): pass
    return found


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


def run_shard(name, tests, top, verbose, descriptors=(), timeout=PROCESS_LIMIT, chosen=None, halt=None):
    """Run a shard's tests in a process of its own, which inherits descriptors and no other. It runs
    in this process's directory, with top and then that directory on its sys.path, as `python3 -m
    unittest discover` does. A name listed more than once runs once. With chosen, the ids of the
    shard's selected tests, a module the shard names whole runs only those. One still running after
    timeout seconds, or when a stop signal reaches this process (processes.forwarding_signals) or halt, a
    threading.Event, is set, is stopped, and its status is None. None when the stop came before it started."""
    patterns = None if chosen is None else [glob.escape(identity) for identity in dict.fromkeys(chosen)]
    command = [sys.executable, "-c", SHARD, str(top), json.dumps(patterns), *(["-v"] if verbose else []),
               *dict.fromkeys(tests)]
    began = time.monotonic()
    halted = (lambda: False) if halt is None else halt.is_set
    stopping = lambda: stop_requested() or halted()
    # A stop can come between the pool taking this shard and this process seeing it: then it never starts.
    if stopping(): return None
    status, output = processes.bounded(command, timeout=timeout, pass_fds=descriptors, stop=stopping)
    summary = own_result(output)
    return {"name": name, "tests": list(tests), "status": status, "output": output,
            "ran": sum(int(count) for count in RAN.findall(output)),
            "result": stopped_result() if status is None and stop_requested()
                      else HALTED if status is None and halted()
                      else f"timed out after {timeout:g}s" if status is None
                      else summary.group(0) if summary else "no result",
            "seconds": time.monotonic() - began}


def unimported_result(name, placeholders):
    """The result of a shard that stands for a module that failed to import in discovery, in a run that selects tests,
    without running it: its import error, as unittest reports one."""
    stream = io.StringIO()
    unittest.TextTestRunner(stream).run(unittest.TestSuite(placeholders))
    output = (f"{name} failed to import in discovery, so it is not run: under a selection its own process would "
              f"select none of its tests\n{stream.getvalue()}")
    return {"name": name, "tests": [name], "status": 1, "output": output, "ran": 0,
            "result": "FAILED (failed to import in discovery)", "seconds": 0.0}


def stop_requested():
    """Whether a stop signal has reached this process, such as the SIGTERM supervise sends a lane's command at its
    timeout."""
    return processes.requested() is not None


def stopped_result():
    """A process's result, or the run's, when a stop signal ended it."""
    return f"stopped by {signal.Signals(processes.requested()).name}"


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
    """What qk unittest prints after its summary for reasons, [(failed test, why)]; saved says where the whole output
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
    parser = argparse.ArgumentParser(prog="qk unittest", description="Discover a unittest suite as `python3 -m "
        "unittest discover` does, and run its modules in processes of their own, JOBS at a time.",
        epilog="Started outside an admission, the run is admitted and placed first, as `qk admit -- COMMAND` "
        "would run it. --unadmitted, after `unittest`, runs it with no admission, on this Mac.")
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
    parser.add_argument("--fail-fast", action="store_true",
                        help="run first the processes holding a class that failed in the repository's recent "
                        "--fail-fast runs, rerun alone the failed tests of a process that fails, and stop the run "
                        "once one fails again")
    parser.add_argument("names", nargs="*", metavar="NAME",
                        help="run only this module, class or test and what it holds, such as "
                        "test_merge_queue.Landing; each must name a discovered test")
    args = parser.parse_args(argv)
    if args.jobs < 1: parser.error("--jobs needs at least 1")
    if args.batch_size is not None and (args.batch_size < 1 or args.split != "class"):
        parser.error("--batch-size needs a positive count and --split class")
    if args.timeout <= 0: parser.error("--timeout needs a positive number of seconds")
    # Its first line names the folder that keeps its progress whatever its standard output is (run_progress).
    progress = run_progress.tracker(["qk", "unittest", *argv])
    status = None
    try:
        status = discover_and_run(args, progress)
        return status
    finally:
        if progress is not None:
            with contextlib.suppress(OSError): progress.end(status, getattr(progress, "summary", None))


def discover_and_run(args, progress):
    start = Path(args.start_directory).resolve()
    top = Path(args.top_level_directory).resolve() if args.top_level_directory else isolation_root(start) or start
    # Before discovery imports any of them: a suite that keeps its tests off the real hosts in host_isolation.py
    # runs no module that skips it, which otherwise only the full suite's own check found, in the merge queue.
    refused = refusals(start, args.pattern)
    if refused:
        for line in refused: print(line, file=sys.stderr)
        return 1
    # Discovery imports every module here, as unittest's own would, and each shard imports its own
    # again: from the top-level directory, then the directory queuekeeper runs in (#74). It sees the tree under
    # test's own copy of a package an extension loaded from elsewhere, as each shard does (tree_imports).
    with tree_imports((os.getcwd(), top, start)):
        discovered = discover(args, start, top)
    if isinstance(discovered, int): return discovered
    # A stop signal, such as the SIGTERM supervise sends a lane's command at its timeout, stops the processes; the run
    # still ends with its summary and what it did not reach, and then by that signal (vocaro/queuekeeper#37).
    with processes.forwarding_signals():
        return run_shards(args, top, *discovered, progress)


def discover(args, start, top):
    """(tests, {shard: tests}, {shard: test ids}, the same for a run that selects tests or None) for what args
    selects under start, or the status of a run that finds nothing to run."""
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
            print(f"queuekeeper unittest: no test discovered in {start} matching {matching} is or is under "
                  f"{', '.join(unmatched)}", file=sys.stderr)
            return NO_TESTS
        tests = [test for test in tests if selected(test, selectors, args.names)]
        if not any(type(test).__module__ != "unittest.loader" for test in tests):
            print(f"queuekeeper unittest: no test discovered in {start} matching {matching} is selected by "
                  + " and ".join(([f"-k {' or '.join(args.selectors)}"] if args.selectors else [])
                                 + ([" or ".join(args.names)] if args.names else [])), file=sys.stderr)
            return NO_TESTS
    # Each shard's test ids, by which unreached_lines names what a run left unfinished. With a selection, a module
    # a shard names whole passes its own to its process.
    members = {}
    found = shards(unittest.TestSuite(tests), args.split, loader.origins, members, batch_size=args.batch_size)
    chosen = members if selectors or args.names else None
    if not found:
        print(f"queuekeeper unittest: no tests found in {start} matching {' or '.join(patterns)}", file=sys.stderr)
        return NO_TESTS
    if args.fail_fast: found = recent_failures_first(found, members, start)
    return tests, found, members, chosen


# Seconds a stopped run waits for its running processes to end before its summary, each stopped at its next look
# at the stop (processes.STOP_POLL) or by the stop's own signal. supervise gives it KILL_AFTER after its SIGTERM.
STOP_WAIT = 4


def run_shards(args, top, tests, found, members, chosen, progress=None):
    """Run found, {shard: tests}, args.jobs at a time, print each one's output and then the run's summary, and return
    its status. tests are the discovered tests the run selected, members is {shard: test ids}, and chosen is
    members for a run that selects tests, else None. progress, a run_progress.Tracker, records each process as it
    starts and ends, in whatever order they end. With args.fail_fast, a process that fails has its failed tests run
    alone at once, and when one fails again the run stops what runs and starts nothing more (confirm)."""
    expected = {name: len(tests) for name, tests in found.items()}
    # The shards that stand for a module that failed to import in discovery, which unittest gave a placeholder.
    placeholders = {test.id(): test for test in tests if type(test).__module__ == "unittest.loader"}
    unimported = {name: [placeholders[identity] for identity in members[name]] for name in found
                  if members.get(name) and all(identity in placeholders for identity in members[name])}
    # Counted for the reader of an admitted run's output, whose progress folder on another Mac it cannot read.
    print(f"{run_progress.PREFIX}{len(tests)} tests in {len(found)} processes, {args.jobs} at a time",
          flush=True)
    if progress is not None:
        with contextlib.suppress(OSError): progress.planned(len(found), len(tests), args.jobs)

    halt = threading.Event() if getattr(args, "fail_fast", False) else None
    stopping = lambda: stop_requested() or (halt is not None and halt.is_set())

    def confirm(result):
        """Run alone the failed tests of result, a process that failed, and set halt once one fails again. A process
        whose failures are not each one of its own tests by its id, as a module that did not load is, or one that
        timed out, is left for the merge queue to weigh as any run's."""
        if halt.is_set() or not result["status"]: return
        ids = failed_ids(result["output"], found[result["name"]])
        if not ids: return
        again = run_shard(f"{result['name']} again", ids, top, False, descriptors, args.timeout, halt=halt)
        if again is None or halt.is_set(): return
        repeated = failed_ids(again["output"], ids) if again["status"] else []
        result["again"] = (f"-- {', '.join(repeated)} failed again alone ({again['result']}, {again['seconds']:.1f}s): "
                           "the run stops here" if repeated else
                           f"-- {', '.join(ids)} run alone: {again['result']} ({again['seconds']:.1f}s): the run goes on")
        if repeated: halt.set()

    def tracked(name, *arguments):
        if progress is not None:
            with contextlib.suppress(OSError): progress.started(name)
        result = None
        try:
            # Under a selection, its process would select none of the module's tests and say only that none ran.
            result = unimported_result(name, unimported[name]) if chosen is not None and name in unimported \
                else run_shard(name, *arguments, halt=halt)
            if halt is not None and result is not None: confirm(result)
            return result
        finally:
            if progress is not None:
                with contextlib.suppress(OSError): progress.finished(name, result)

    # Each shard holds the admission lock descriptors this process holds and no others, as a test in
    # `python3 -m unittest discover` does, so a nested admission in it reuses the admission; given
    # the token alone, one was refused (#70).
    descriptors = processes.held_locks(os.environ)
    began = time.monotonic()
    results, report = [], []

    def show(result):
        if result is None: return
        # supervise sends its SIGTERM to each process as well as to this one, after it, so a process can end by it
        # before it looks at the stop: one that gave no result then was cut off by the stop, not failed.
        if stop_requested() and result["status"] is not None and result["status"] < 0 and not own_result(result["output"]):
            result.update(status=None, result=stopped_result())
        results.append(result)
        # Each shard's own output, whole and in discovery order, under one line that names it.
        shown = [f"== {result['name']}: {result['result']} ({result['ran']} tests, {result['seconds']:.1f}s)",
                 result["output"].rstrip("\n"), *([result["again"]] if result.get("again") else [])]
        report.extend(shown)
        print(*shown, sep="\n", flush=True)

    pool = ThreadPoolExecutor(max_workers=args.jobs)
    try:
        futures = [pool.submit(tracked, name, tests, top, args.verbose, descriptors, args.timeout,
                               None if chosen is None else chosen[name])
                   for name, tests in found.items()]
        waiting = len(futures)
        for index, future in enumerate(futures):
            while not future.done() and not stopping():
                concurrent.futures.wait([future], timeout=processes.STOP_POLL)
            if not future.done():
                waiting = index
                break
            show(future.result())
        if stopping():
            # What has not started never starts (run_shard), and what runs ends at its next look at the stop.
            concurrent.futures.wait(futures, timeout=STOP_WAIT)
            for future in futures[waiting:]:
                if future.done() and future.exception() is None: show(future.result())
    finally:
        # A stopped run waits on no process that has not ended: its summary comes first.
        pool.shutdown(wait=not stopping(), cancel_futures=True)
    stopped = stopped_result() if stop_requested() else HALTED if stopping() else None
    failed, unmeasured, reasons = [], [], []

    def fail(result, detail):
        failed.append(f"{result['name']} ({detail})")
        ended = next((line for line in reversed(result["output"].splitlines()) if line.strip()), None)
        reasons.extend(failure_reasons(result["output"], result["tests"])
                       or [(result["name"], f"its output ends: {ended}" if ended else "it printed nothing")])

    for result in results:
        if result["status"] is None:
            if result["result"] != stopped: unmeasured.append(f"{result['name']} ({result['result']})")
        elif result["status"] != 0: fail(result, f"{result['result']}, status {result['status']}")
        elif result["ran"] != expected[result["name"]]:
            # A shard that ran other than the tests discovery found for it did not run what it was given.
            fail(result, f"ran {result['ran']} of {expected[result['name']]} tests"
                 + (": its module failed to import in discovery but not in its own process"
                    if result["name"] in unimported else ""))
    finished = {result["name"] for result in results if result["status"] is not None}
    unfinished = [name for name in found if name not in finished]
    # One item for what the stop left, not each process: a stop at a gate's timeout leaves hundreds, which
    # unreached_lines groups. A process that timed out by itself keeps its own.
    timed_out = {result["name"] for result in results if result["status"] is None and result["result"] != stopped}
    cut = [name for name in unfinished if name not in timed_out]
    left = f"{len(cut)} of {len(found)} processes not run or not finished ({stopped})" if cut else None
    # A run --fail-fast stopped measured what it set out to: a failure that repeats alone.
    if left and stopped != HALTED: unmeasured.append(left)
    if halt is not None:
        record_failures(Path(args.start_directory).resolve(),
                        [identity for result in results if result["status"]
                         for identity in failed_ids(result["output"], found[result["name"]])])
    total = sum(result["ran"] for result in results)
    skipped = sum(lane_results.skipped_tests(result["output"]) for result in results)
    # Not "Ran N tests": the shards' own lines already count every test once.
    summary = (f"queuekeeper unittest: {total} tests ({skipped} skipped) in {len(results)} processes, {args.jobs} at a time, "
               f"{time.monotonic() - began:.1f}s: " + verdict(failed, unmeasured, left if stopped == HALTED else None))
    print(summary, flush=True)
    if progress is not None: progress.summary = summary
    if failed:
        for line in reason_lines(reasons, saved_output(report + [summary], reasons)): print(line, flush=True)
    if unfinished:
        unreached = list(dict.fromkeys(identity for name in unfinished for identity in members[name]))
        for line in unreached_lines(args, tests, unreached, stopped): print(line, flush=True)
    return 1 if failed else lane_results.UNMEASURED if unmeasured else 0


def test_places(tests):
    """{test id: (its module, its class)} for tests, each the name a run selects it by, as `qk unittest NAME` takes
    it: a test whose id is under no module or class, as a module that failed to load is, stands for both itself."""
    places = {}
    for test in tests:
        identity, cls = test.id(), type(test)
        module, owner = cls.__module__, f"{cls.__module__}.{cls.__qualname__}"
        if module == "unittest.loader" or not identity.startswith(f"{module}."): module = owner = identity
        elif not identity.startswith(f"{owner}."): owner = identity
        places[identity] = (module, owner)
    return places


def rerun_names(unreached, places):
    """{module: [names]}, the fewest names under each module of unreached, test ids, that select them and no other of
    places (test_places), in discovery order: the module when all of its tests are unreached, else each class all of
    whose tests are, else each test."""
    left, modules, classes = set(unreached), {}, {}
    for identity, (module, owner) in places.items():
        modules.setdefault(module, []).append(identity)
        classes.setdefault(owner, []).append(identity)
    whole = {name for name, ids in {**modules, **classes}.items() if left.issuperset(ids)}
    found = {}
    for identity in unreached:
        module, owner = places.get(identity, (identity, identity))
        name = module if module in whole else owner if owner in whole else identity
        found.setdefault(module, {})[name] = None
    return {module: list(names) for module, names in found.items()}


def own_launcher():
    """The words that start this qk again from the directory it runs in: `python3 PATH` for a launcher named qk, as
    scripts/qk and bin/qk are, else `qk`."""
    path = Path(sys.argv[0])
    return ["python3", os.path.relpath(path)] if path.name == "qk" and path.is_file() else ["qk"]


def rerun(args, names):
    """The command that runs names alone, with args's suite and selection."""
    return [*own_launcher(), "unittest", "-s", args.start_directory,
            *(["-t", args.top_level_directory] if args.top_level_directory else []),
            *(word for pattern in args.pattern or () for word in ("-p", pattern)),
            *(word for selector in args.selectors for word in ("-k", selector)), *names]


def unreached_lines(args, tests, unreached, stopped):
    """What qk unittest prints after its summary for unreached, the ids of the tests in its processes that did not
    finish: those a stop left not started or cut off, and those in a process that timed out (vocaro/queuekeeper#37).
    tests are the run's selected tests and stopped the stop's result, or None. Grouped by module, at most
    REASONS_SHOWN of them, then the command that runs only those tests. The whole list and that command go to
    UNREACHED_FILE in the lane's results/unittest/ (RESULTS_VARIABLE), or outside a lane below the directory the run
    started in (keep_unreached)."""
    places = test_places(tests)
    grouped = rerun_names(unreached, places)
    command = rerun(args, [name for names in grouped.values() for name in names])
    counts = {module: sum(1 for identity in unreached if places.get(identity, (identity,))[0] == module)
              for module in grouped}
    totals = {module: sum(1 for place in places.values() if place[0] == module) for module in grouped}
    why = stopped or "timed out"
    record = {"result": why, "tests": unreached, "names": [name for names in grouped.values() for name in names],
              "command": command}
    listing = [f"# qk unittest did not finish these {len(unreached)} of {len(places)} tests ({why}). Run only them with:",
               f"# {shlex.join(command)}"]
    for module, names in grouped.items():
        listing.append(f"{module}: {counts[module]} of {totals[module]} tests")
        listing += [f"    {identity}" for identity in unreached if places.get(identity, (identity,))[0] == module]
    try: saved = f"the whole list is in {keep_unreached(listing, record)}"
    except (OSError, ValueError) as error: saved = f"the list was not saved: {error}"
    lines = [f"Not reached or not finished, {len(unreached)} of {len(places)} tests in {len(grouped)} modules ({why}); "
             f"{saved}:"]
    for module, names in list(grouped.items())[:REASONS_SHOWN]:
        within = f"all {totals[module]} tests" if names == [module] else \
            f"{counts[module]} of {totals[module]} tests, in " + ", ".join(name.removeprefix(f"{module}.") for name in names)
        lines.append(f"    {clipped(f'{module}: {within}', REASON_WIDTH)}")
    if len(grouped) > REASONS_SHOWN:
        lines.append(f"    [cut: {len(grouped) - REASONS_SHOWN} more modules of {len(grouped)}; the whole list has them]")
    lines.append(f"Run only these: {shlex.join(command)}")
    return lines


# The file in a lane's results/unittest/, or a kept folder outside a lane, that lists what a run did not finish, and
# its record beside it, which `qk run`'s summary reads (lane_report).
UNREACHED_FILE = "unreached.txt"
UNREACHED_RECORD = "unreached.json"
UNREACHED_RUNS = ".build/queuekeeper/unittest-unreached"


def keep_unreached(listing, record):
    """Write listing, the lines of UNREACHED_FILE, and record, its UNREACHED_RECORD, into the lane's results/unittest/,
    or outside a lane into a folder of their own under UNREACHED_RUNS below the directory the run started in, as a
    failed run's output is kept (kept_folder); return the path of the list. Raises OSError when they could not be
    written."""
    results = os.environ.get(RESULTS_VARIABLE)
    if results:
        directory = Path(results) / "unittest"
        directory.mkdir(parents=True, exist_ok=True)
    else:
        directory = kept_folder(Path.cwd() / UNREACHED_RUNS)
    (directory / UNREACHED_RECORD).write_text(json.dumps(record, indent=2) + "\n")
    path = directory / UNREACHED_FILE
    path.write_text("\n".join(listing) + "\n")
    return path


# Where a failed run's whole output is kept, below the directory it ran in, and how many runs' stay.
FAILED_RUNS = ".build/queuekeeper/unittest-failed"
KEPT_FAILED_RUNS = 5


def kept_folder(kept, now=None):
    """A new folder of its own under kept, named for now and this process, so that a run in the same second has its
    own, once all but the newest KEPT_FAILED_RUNS before it are removed."""
    now = now or datetime.now(timezone.utc)
    directory = kept / f"{now.strftime('%Y%m%dT%H%M%SZ')}-{os.getpid()}"
    directory.mkdir(parents=True, exist_ok=False)
    for old in sorted(path for path in kept.iterdir() if path.is_dir())[:-KEPT_FAILED_RUNS]:
        shutil.rmtree(old, ignore_errors=True)
    return directory


def keep_output(root, lines, reasons, now=None):
    """Keep lines, the whole output of a failed run, in a folder of its own under root's FAILED_RUNS (kept_folder),
    with its failed tests and why in failures.txt; return the path of its log. reasons is [(failed test, why)].
    Raises OSError when it could not be written."""
    directory = kept_folder(Path(root) / FAILED_RUNS, now)
    log = directory / "test.log"
    log.write_text("\n".join(lines) + "\n")
    (directory / "failures.txt").write_text("".join(f"Failed: {test}\n    {why}\n" for test, why in reasons))
    return log


def saved_output(report, reasons):
    """Keep report, a failed run's lines, below the directory the run started in (keep_output), and return words
    saying where it is, or why it is not: either way the run's result stands."""
    try: return f"the whole output is in {keep_output(Path.cwd(), report, reasons)}"
    except (OSError, ValueError) as error: return f"the whole output was not saved: {error}"


ISOLATION = "host_isolation.py"


def test_paths(start, patterns=None):
    """Matching test files throughout a suite, including component subpackages."""
    return sorted({path for pattern in patterns or ["test*.py"] for path in Path(start).rglob(pattern)
                   if path.is_file() and not path.is_symlink()})


def isolation_root(start):
    """The nearest suite bootstrap, also when running a component subdirectory."""
    start = Path(start).resolve()
    return next((path for path in (start, *start.parents) if (path / ISOLATION).is_file()), None)


def refusals(start, patterns=None):
    """The lines qk unittest refuses a suite with before discovery imports any of its modules, each ending `nothing
    ran`, or none. In a suite that keeps its tests off the real hosts in host_isolation.py, a module patterns match
    that does not parse, or that does not import host_isolation at its top level, refuses the run. qk land and `qk
    queue add` run these on an entry's tree before queuing it, as they take seconds and a refusal costs a whole gate
    (merge_queue.prerun, vocaro-engineering#290)."""
    start = Path(start).resolve()
    isolated = isolation_root(start)
    if isolated is None: return []
    paths, broken = test_paths(start, patterns), {}
    for path in paths:
        try: ast.parse(path.read_text(), str(path))
        except (SyntaxError, ValueError) as error:
            line = getattr(error, "lineno", None)
            broken[path] = f"{path.relative_to(isolated).as_posix()} does not parse ({getattr(error, 'msg', error)}" + \
                (f", line {line})" if line else ")")
    lines = [f"queuekeeper unittest: {why}; nothing ran" for why in broken.values()]
    forgot = unisolated([path for path in paths if path not in broken], isolated)
    if forgot:
        lines.append(f"queuekeeper unittest: {', '.join(forgot)} must import {ISOLATION.removesuffix('.py')} at its top level, "
                     f"as every test module beside {isolated / ISOLATION} does; nothing ran")
    return lines


def unisolated(paths, root=None):
    """The names of the test modules among paths that do not import host_isolation at their top level."""
    found = []
    for path in paths:
        tree = ast.parse(Path(path).read_text(), str(path))
        if not any(isinstance(node, ast.Import) and any(alias.name == "host_isolation" for alias in node.names)
                   for node in tree.body):
            found.append(Path(path).relative_to(root).as_posix() if root else Path(path).name)
    return found


def verdict(failed, unmeasured, halted=None):
    """A runner's last words: OK, or what failed, what a timeout left unmeasured and, last, what --fail-fast left
    unrun, halted, once a failed test failed again alone."""
    words = ([f"FAILED: {', '.join(failed)}"] if failed else []) + \
        ([f"UNMEASURED: {', '.join(unmeasured)}"] if unmeasured else []) + ([f"{STOPPED}{halted}"] if halted else [])
    return "; ".join(words) or "OK"


def failed_ids(output, entries):
    """The ids of the tests unittest reports failed in output, a process's, in order, when each is one of entries,
    the names that process was given, so that each can run alone by its id; else none, as for a module that did not
    load, or a whole module run by its own name (shards)."""
    lines, ids = output.splitlines(), []
    for index, line in enumerate(lines[1:], 1):
        heading = FAILURE.match(line) if lines[index - 1] == unittest.TextTestResult.separator1 else None
        if not heading: continue
        method, where, _ = heading.groups()
        identity = where if where.endswith(f".{method}") else f"{where}.{method}"
        if identity not in entries: return []
        ids.append(identity)
    return list(dict.fromkeys(ids))


def history_place(start):
    """(the failure history of the repository that holds start, the suite's key in it: start relative to the
    repository's root), or (None, None) when git cannot tell. The history is in the repository's common git directory,
    which its worktrees and its merge queue's checkouts on one Mac share, beside queuekeeper-queue/."""
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    try:
        common, root = subprocess.run(["git", "-C", str(start), "rev-parse", "--path-format=absolute", "--git-common-dir",
                                       "--show-toplevel"], capture_output=True, text=True, check=True,
                                      timeout=GIT_SECONDS, env=environment).stdout.split("\n")[:2]
        key = Path(start).resolve().relative_to(Path(root).resolve()).as_posix()
    except (OSError, subprocess.SubprocessError, ValueError): return None, None
    return Path(common) / HISTORY_FILE, key


def recent_failures(path, key, now):
    """{test id: when it last failed} of the suite key in the history at path, within HISTORY_DAYS of now; {} when
    there is none or it cannot be read."""
    try: suite = json.loads(path.read_text()).get(key)
    except (OSError, ValueError, AttributeError): return {}
    if not isinstance(suite, dict): return {}
    return {test: when for test, when in suite.items()
            if isinstance(when, (int, float)) and 0 <= now - when < HISTORY_DAYS * 86400}


def record_failures(start, ids, now=None):
    """Add ids, the tests a --fail-fast run saw fail, to the failure history of the repository that holds start at
    now, keeping the HISTORY_MOST that failed last within HISTORY_DAYS. Two runs that end at once can lose one's
    record, and a history that cannot be written fails nothing: it only orders runs (recent_failures_first)."""
    path, key = history_place(start)
    if path is None or not ids: return
    now = time.time() if now is None else now
    try: whole = json.loads(path.read_text())
    except (OSError, ValueError): whole = {}
    if not isinstance(whole, dict): whole = {}
    suite = {**recent_failures(path, key, now), **dict.fromkeys(ids, now)}
    whole[key] = dict(sorted(suite.items(), key=lambda item: -item[1])[:HISTORY_MOST])
    with contextlib.suppress(OSError):
        written = path.with_name(f"{path.name}.{os.getpid()}")
        written.write_text(json.dumps(whole, indent=1) + "\n")
        os.replace(written, path)


def recent_failures_first(found, members, start, now=None):
    """found, {shard: tests}, with the shards first that hold a test of a class a --fail-fast run saw fail within
    HISTORY_DAYS (record_failures), the class that failed last first, and the rest in discovery order. A failure
    tends to come back where one just was, as in a bisect's halves and in a batch that gates again: on the toolkit's
    failed gates of 2026-11-12 to 18 the first failure came a median 13% of the way into a run so ordered, against
    29% in discovery order. A class, not a module: one failure in a module as large as test_merge_queue would put half
    the suite first. found as it was with no history."""
    path, key = history_place(start)
    if path is None: return found
    classes = {}
    for test, when in recent_failures(path, key, time.time() if now is None else now).items():
        owner = test.rsplit(".", 1)[0]
        classes[owner] = max(when, classes.get(owner, 0))
    latest = {name: max((classes.get(identity.rsplit(".", 1)[0], 0) for identity in members.get(name, ())), default=0)
              for name in found}
    first = sorted((name for name in found if latest[name]), key=lambda name: -latest[name])
    if not first: return found
    print(f"qk unittest --fail-fast: {len(first)} of {len(found)} processes run first, as a class of each failed in this "
          f"repository's --fail-fast runs in the last {HISTORY_DAYS} days", flush=True)
    return {**{name: found[name] for name in first}, **{name: found[name] for name in found if not latest[name]}}
