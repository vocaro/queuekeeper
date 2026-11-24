"""Flaky Xcode tests: the merge queue's rerun of a failed `qk simulator-tests` lane's tests, one at a time, and the
flakes it proved, which `qk results history` counts beside a test's kept outcomes (vocaro/queuekeeper#2).

A lane whose command is `qk simulator-tests` keeps each device's per-test outcomes, with Xcode's exact identifier URL,
in its results folder (matrix.kept). When its gate fails, reruns(lane) names the failed tests from those records as
TARGET/SUITE/METHOD selectors and reruns each with the lane's own command and `--repeat 1 --erase --exact --only TEST`:
on a freshly erased member of each of the lane's device types, a pass or failure counts only when that run's own result
bundle shows the one test alone with that outcome, and is otherwise unmeasured, which gives no verdict. The merge queue
then weighs the reruns as it does a `qk unity test` lane's (merge_queue.Worker.rerun_declared): a test that passes alone
every time is a flake, recorded as any, and the batch lands only when none of its commits changes a path the lane's
failedTests lists. Swift has no import graph the queue can read, so a lane that lists none bisects.

A run the records cannot account for names no test, and the batch bisects as before: a device or shard that kept no
record, an unreadable bundle, a run that failed or stopped without a failed test case, an unmeasured case, or a failed
case without an identifier URL that names one plain TARGET/SUITE/METHOD.
"""
import json
from pathlib import Path
import re
import subprocess
from urllib.parse import unquote, urlsplit

from queuekeeper import lane_results
from .simulators import matrix

# The command that keeps the records a rerun reads (matrix.kept).
KEPT_BY = "qk simulator-tests"
# What a landed tip's Flaky-test trailer and a test.flaky event begin with (merge_queue.flake_words).
PROVEN = re.compile(r"^(\S+) failed the gate at ([0-9a-f]{7,40})\b")


def selector(identity):
    """The TARGET/SUITE/METHOD selector of an Xcode identifier URL, test://com.apple.xcode/PROJECT/TARGET/SUITE/METHOD,
    or None when it names no plain method that -only-testing can run alone (matrix.ISOLATED_SELECTOR)."""
    if not isinstance(identity, str): return None
    try: address = urlsplit(identity)
    except ValueError: return None
    if (address.scheme, address.netloc, address.query, address.fragment) != ("test", "com.apple.xcode", "", ""):
        return None
    path = [unquote(part) for part in address.path.split("/")]
    if len(path) != 5 or path[0] or not path[1]: return None
    found = "/".join(path[2:])
    return found if matrix.ISOLATED_SELECTOR.fullmatch(found) else None


def expected_runs(args):
    """The kept record each test run of a lane's simulator-tests leaves: one per device, or one per shard."""
    if args.shards: return [f"{kind}-{number}" for kind in args.device for number in range(1, args.shards + 1)]
    return list(args.device)


def failed(results, expected):
    """([selector], None), the tests a failed run of a `qk simulator-tests` lane failed, in order, each once, from the
    records it kept in results, its results folder, for each of expected, its test runs; or (None, why it names none)."""
    folder = Path(results) / matrix.KEPT
    tests = []
    for name in expected:
        path = folder / f"{name}.json"
        try:
            record = json.loads(path.read_text())
            if not isinstance(record, dict) or record.get("keptBy") != KEPT_BY:
                return None, f"{path} is not a record qk simulator-tests kept"
        except FileNotFoundError:
            return None, f"{name} kept no record in {folder}, so whether its tests ran is not known"
        except (OSError, ValueError) as error:
            return None, f"{path} cannot be read: {error}"
        status, cases = record.get("status"), record.get("tests")
        if record.get("bundleUnread") or not isinstance(cases, list):
            return None, f"{name}'s result bundle was not read: {record.get('bundleUnread') or 'it kept no test cases'}"
        if status is None or status == lane_results.UNMEASURED:
            return None, f"{name} was unmeasured"
        if not all(isinstance(case, dict) for case in cases): return None, f"{path} has a test case that is not a record"
        unmeasured = [case.get("test") for case in cases if case.get("outcome") == "unmeasured"]
        if unmeasured: return None, f"{name} left {unmeasured[0]} unmeasured, so its run was not whole"
        failures = [case for case in cases if case.get("outcome") == "failed"]
        if status != 0 and not failures:
            return None, f"{name} failed (status {status}) with no failed test case in its result bundle"
        summary = record.get("summary") if isinstance(record.get("summary"), dict) else {}
        if isinstance(summary.get("failedTests"), int) and summary["failedTests"] != len(failures):
            return None, (f"{name}'s result bundle counts {summary['failedTests']} failed tests but names "
                          f"{len(failures)}")
        for case in failures:
            found = selector(case.get("identifierURL"))
            if found is None:
                return None, (f"{name}'s failed {case.get('test')} has no identifier URL that names one "
                              "TARGET/SUITE/METHOD, so it cannot be rerun alone")
            if found not in tests: tests.append(found)
    if not tests: return None, f"no test run in {folder} names a failed test"
    return tests, None


def reruns(lane):
    """A plugins.Reruns for a lane whose command is `qk simulator-tests` and runs each device's tests whole, with no
    test argument but -only-testing and -skip-testing, so that one failed test can run alone; else None. Its exits
    follow qk's: an unmeasured rerun gives no verdict (no_verdict)."""
    from queuekeeper import plugins
    command = lane.get("command") if isinstance(lane, dict) else None
    if not isinstance(command, list) or not all(isinstance(word, str) for word in command): return None
    at = next((index for index in range(len(command) - 1)
               if Path(command[index]).name == "qk" and command[index + 1] == "simulator-tests"), None)
    if at is None: return None
    words = command[at + 2:]
    try: args = matrix.parse(words, parser_factory=matrix._ClockArguments)
    except (ValueError, SystemExit): return None
    if args.repeat is not None or args.only or args.build_only or args.workers \
            or not all(matrix.selecting(word) for word in args.passed):
        return None
    expected = expected_runs(args)
    rerun = [*command, *([] if "--" in words else ["--"]), "--repeat", "1", "--erase", "--exact", "--only", "{test}"]
    found = plugins.Reruns(rerun, lambda results: failed(results, expected))
    # merge_queue.Worker.rerun_declared: an unmeasured or unstarted rerun gives no verdict, as a qk unittest one's.
    found.no_verdict = True
    return found


def proven(root):
    """[(test, commit)] of the flakes the merge queue proved, oldest first, each once: the Flaky-test trailers on
    origin/main's landed tips, which last, and the queue's test.flaky events, which also name the flakes of batches
    that did not land but which its journal keeps only so long (events.LIMIT)."""
    from queuekeeper import events
    found = []
    def git(*words):
        try: done = subprocess.run(["git", "-C", str(root), *words], capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError): return ""
        return done.stdout if done.returncode == 0 else ""
    trailers = git("log", "--reverse", "--grep=^Flaky-test: ", "--format=%(trailers:key=Flaky-test,valueonly)",
                   "origin/main", "--")
    for line in trailers.splitlines():
        match = PROVEN.match(line.strip())
        if match: found.append((match[1], match[2][:12]))
    common = git("rev-parse", "--path-format=absolute", "--git-common-dir").strip()
    if common:
        for event in events.read(Path(common) / "queuekeeper-queue", kinds=["test.flaky"]):
            if isinstance(event.get("test"), str) and isinstance(event.get("commit"), str):
                found.append((event["test"], event["commit"][:12]))
    return list(dict.fromkeys(found))


def register():
    """Have the merge queue rerun a failed qk simulator-tests lane's tests alone before a bisect."""
    from queuekeeper import plugins
    known = getattr(plugins, "RERUNS", None)
    # An older core reruns no plugin's tests.
    if known is not None and reruns not in known: known.append(reruns)
