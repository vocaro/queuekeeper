"""The summary that ends every `qk run` (vocaro-engineering#274).

A lane's outcome used to be one `remote:` line in the middle of its output, and its counts and failing tests lay only
in a file. Each run now ends with its verdict, its passed, failed, skipped and unmeasured tests, each failing test
with its file and line, and the path to its failures.txt or its receipt. It reads only what this run wrote: a run's
log and results here (ran_here), or the dispatch that started after it began (remote.dispatch), and never an earlier
run's. Passed, failed, skipped and unmeasured stay apart, a lane is passed only when it ran and returned 0, and a lane
whose log counts no test says so rather than printing zeros.

The lines go to stderr with queuekeeper's other notes, after stdout is flushed, so that they are last however the two are
joined. They begin `qk run LANE:` or are indented, and quote no line of a test's own, since a lane's log is read
again by what runs it: the merge queue's log, lane_results.counted and failure_records read none of them as a result. The
second Mac's own run prints none: the Mac that sent it prints the summary from what came back.
"""
import contextlib
import json
import os
from pathlib import Path
import re
import sys
import time

from . import lane_results, remote

# The failing tests a summary names; the rest are in its failures.txt or log.
SHOWN = 20

# XCTest's line for a failed test, once per run of it, and its total, whose unexpected failures are assertions.
XCTEST_FAILED = re.compile(r"^Test [Cc]ase '([^'\n]+)' failed\b", re.M)
XCTEST_TOTAL = re.compile(r"Executed \d+ tests?, with [^\n(]*\((\d+) unexpected\)")
# FILE:LINE: error: -[Module.Class test] : message
XCTEST_ISSUE = re.compile(r"^([^\s:][^:\n]*):(\d+): error: -\[([^\]\n]+)\] : ", re.M)
# Swift Testing's lines for a failed test and an issue it recorded, not its line for the whole run (Swift 6.4).
SWIFT_TESTING_FAILED_RUN = re.compile(r"Test run with \d+ tests?(?: in \d+ suites?)? failed")
SWIFT_TESTING_FAILED = re.compile(r"^\S+ Test (?!run with \d)(.+?) failed after ", re.M)
SWIFT_TESTING_ISSUE = re.compile(r"^\S+ Test (?!run with \d)(.+?) recorded an issue at ([^:\n]+):(\d+)", re.M)
# unittest's heading for a failure or error, what ends its traceback, and a traceback's frame.
UNITTEST_FAILED = re.compile(r"^(?:FAIL|ERROR): (\S+) \(([^()\n]*)\)", re.M)
UNITTEST_END = re.compile(r"^={70}$|^-{70}\nRan \d", re.M)
FRAME = re.compile(r'^\s*File "([^"\n]+)", line (\d+)', re.M)
# pytest's short summary line, the heading of a failure's report, and the location in that report.
PYTEST_FAILED = re.compile(r"^[ \t]*(?:FAILED|ERROR) (\S+\.py(?:::\S+)?)(?: - .*)?$", re.M)
PYTEST_HEADING = re.compile(r"^_{3,} (?:ERROR [a-z ]*at [a-z]+ of )?(.+?) _{3,}$", re.M)
PYTEST_PLACE = re.compile(r"^(\S+\.py):(\d+): ", re.M)
# pytest's folded skip line, which stands for the count it gives.
FOLDED = re.compile(r"^SKIPPED \[(\d+)\]")
# Signs that a test may have started though a log counts none: XCTest's and Swift Testing's lines for a suite, test or
# run that started, xcodebuild's for a test session and a runner that exited early, unittest's -v line for a test,
# pytest's session heading and collection, and a process that crashed or was signaled, which a test can be.
STARTED = re.compile(r"^Test (?:Suite|Case) '[^'\n]+' started"
                     r"|^\S+ (?:Test|Suite) .+ started\.?$"
                     r"|Testing started|Test session results|unexpected exit"
                     r"|^\w+ \([^()\n]+\) \.\.\. "
                     r"|test session starts|^collect(?:ed|ing) \d+ items?"
                     r"|(?i:crash|segmentation fault|abort trap|illegal instruction|bus error|trace/bpt trap|"
                     r"fatal python error|killed: 9|\bsignal \d+|\bSIG[A-Z]{2,}\b)", re.M)
# A line that says something failed, the last of which a run that stopped before its tests quotes.
ERROR_LINE = re.compile(r"^.*\b(?:error|errors|fail|failed|failure|fatal)\b.*$", re.M | re.I)
# The most of that line a summary quotes.
QUOTED = 300


def failed_tests(text):
    """(the tests a log reports failed, or None when its totals say some failed but name none, the errors pytest
    counts apart from its tests)."""
    total, errors = 0, 0
    for match in lane_results.RAN.finditer(text):
        total += sum(int(n) for n in re.findall(r"\b(?:failures|errors)=(\d+)", match.group(2) or ""))
    for match in lane_results.PYTEST_SUMMARY.finditer(text):
        items = {word: int(n) for n, word in re.findall(r"(\d+) (\w+)", match.group(1))}
        total += items.get("failed", 0) + items.get("error", 0) + items.get("errors", 0)
        errors += items.get("error", 0) + items.get("errors", 0)
    cases, tests = set(XCTEST_FAILED.findall(text)), set(SWIFT_TESTING_FAILED.findall(text))
    if not cases and any(int(n) for n in XCTEST_TOTAL.findall(text)): return None, errors
    if not tests and SWIFT_TESTING_FAILED_RUN.search(text): return None, errors
    return total + len(cases) + len(tests), errors


def unmeasured_count(text):
    return sum(int(FOLDED.match(line).group(1)) if FOLDED.match(line) else 1 for line in lane_results.unmeasured_tests(text))


def counts(text):
    """A log's {passed, failed, skipped, unmeasured}, passed and failed None when it cannot tell them apart, or None
    when it counts no test. An unmeasured test is reported as a skip, so it comes out of the skipped."""
    found = lane_results.counted(text)
    if found is None or found[0] == 0: return None
    ran, skipped = found
    failed, errors = failed_tests(text)
    unmeasured = unmeasured_count(text)
    passed = None if failed is None else max(ran + errors - skipped - failed, 0)
    return {"passed": passed, "failed": failed, "skipped": max(skipped - unmeasured, 0), "unmeasured": unmeasured,
            "ran": ran + errors - skipped}


def failing(text):
    """Each failing test a log names, once, as (name, FILE:LINE or None)."""
    found = {}
    places = {name: f"{path}:{line}" for path, line, name in reversed(XCTEST_ISSUE.findall(text))}
    for name in XCTEST_FAILED.findall(text):
        found.setdefault(name, places.get(name.removeprefix("-[").removesuffix("]")))
    places = {name: f"{path}:{line}" for name, path, line in reversed(SWIFT_TESTING_ISSUE.findall(text))}
    for name in SWIFT_TESTING_FAILED.findall(text): found.setdefault(name, places.get(name))
    for match in UNITTEST_FAILED.finditer(text):
        end = UNITTEST_END.search(text, match.end())
        frames = FRAME.findall(text, match.end(), end.start() if end else len(text))
        found.setdefault(match.group(2) or match.group(1), ":".join(frames[-1]) if frames else None)
    reports = list(PYTEST_HEADING.finditer(text))
    places = {}
    for report, following in zip(reports, [*reports[1:], None]):
        place = PYTEST_PLACE.search(text, report.end(), following.start() if following else len(text))
        if place: places.setdefault(report.group(1), f"{place.group(1)}:{place.group(2)}")
    for name in PYTEST_FAILED.findall(text):
        found.setdefault(name, places.get(name.split("::", 1)[-1].replace("::", ".")))
    return list(found.items())


def before_tests(text):
    """The last line of a failed run's log that says what failed, when the log shows that no test started: it counts
    no test, names no failing or unmeasured test, and has no line in which a test, suite or session started or a
    process crashed (STARTED). None when a test may have run, or when no line says what failed, as a run whose tests
    crashed before printing counts can leave neither (vocaro-engineering#393). queuekeeper's own lines are not quoted."""
    if lane_results.counted(text) is not None or lane_results.unmeasured_tests(text) or failing(text) or STARTED.search(text):
        return None
    errors = [line.strip() for line in ERROR_LINE.findall(text) if not line.startswith("queuekeeper:")]
    return errors[-1][:QUOTED] if errors else None


def records(results):
    """Each failure record a run returned under its results, as (its failures.json's path, what it holds)."""
    kept = []
    for path in sorted(results.rglob("failures.json")) if results.is_dir() else ():
        try: record = json.loads(path.read_text())
        except (OSError, ValueError): continue
        if isinstance(record, dict): kept.append((path, record))
    return kept


def recorded_failing(kept):
    """Each failing test the records name, once, as (name, FILE:LINE or None). A test's name differs among a record's
    lists, as Class/test() and Module.Class/test, so its last two parts identify it."""
    found = {}
    def add(name, place):
        key = ".".join(re.split(r"[./]", name.replace("()", ""))[-2:])
        if key not in found or (found[key][1] is None and place): found[key] = (found[key][0] if key in found else name, place)
    for _path, record in kept:
        for test in record.get("failedTests") or []:
            add(test["test"], next((f"{item['file']}:{item['line']}" for item in test.get("failures") or []
                                    if item.get("file")), None))
        for item in record.get("logged") or []: add(item["test"], f"{item['file']}:{item['line']}")
        for name in record.get("failingTests") or []: add(name, None)
    return list(found.values())


def recorded_counts(kept):
    """The counts the records' result bundles hold, summed, or None when none holds any: a log can lack them."""
    bundles = [record["counts"] for _path, record in kept if isinstance(record.get("counts"), dict)]
    if not bundles: return None
    total = {key: sum(bundle.get(key) or 0 for bundle in bundles) for key in ("passed", "failed", "skipped")}
    return {**total, "unmeasured": 0, "ran": total["passed"] + total["failed"]} if any(total.values()) else None


def said_tests(found, unmeasured, absent=()):
    """The line that states a run's counts. Without counts it says so, and it never prints a zero it did not read."""
    if found is None:
        also = f"; {unmeasured} reported unmeasured" if unmeasured else ""
        return f"no test counts in its log{also}"
    tests = f"{found['passed']} passed, {found['failed']} failed" if found["failed"] is not None else \
        f"{found['ran']} ran, and the log does not say how many of them failed"
    return f"{tests}, {found['skipped']} skipped, {found['unmeasured']} unmeasured" + \
        (f"; no test counts from {', '.join(absent)}" if absent else "")


def combined(parts, status):
    """The sum of each log's counts (counts), None where a log told passed from failed for none of them."""
    if not parts: return None
    total = {key: sum(part[key] for part in parts) for key in ("skipped", "unmeasured", "ran")}
    unknown = any(part["failed"] is None for part in parts)
    # A run that returned 0 failed nothing, whatever its log left unsaid.
    if unknown and status == 0: return {**total, "failed": 0, "passed": total["ran"]}
    if unknown: return {**total, "failed": None, "passed": None}
    return {**total, "failed": sum(part["failed"] for part in parts), "passed": sum(part["passed"] for part in parts)}


class Report:
    """One `qk run`'s summary, inert until a lane run begins."""

    def __init__(self):
        self.lane = self.root = self.group = self.here = self.why = None
        self.began, self.complete = 0, True

    def begin(self, root, lane):
        self.root, self.lane, self.began = root, lane, time.time()

    def ran_here(self, directory):
        """The lane's work starts now on this Mac, writing to directory."""
        self.here = directory

    def explain(self, error, *, complete=True):
        """Why the run ended as it did. Without complete, its log was cut off and counts nothing."""
        self.why, self.complete = str(error), complete

    def source(self):
        """(where the lane ran, its directory), or (None, None) when no work of this run started."""
        if self.here is not None: return "on this Mac", self.here
        ran, host = remote.dispatch(self.root, self.lane, self.began)
        return (f"on {host}", ran) if ran is not None else (None, None)

    def lanes_line(self, directory):
        outcomes = {"passed": [], "failed": [], "unmeasured": []}
        for name in self.group:
            try: outcome = json.loads((directory / "lanes" / f"{name}.json").read_text()).get("outcome") or "no outcome"
            except (OSError, ValueError, AttributeError): outcome = "no receipt"
            outcomes.setdefault(outcome, []).append(name)
        parts = [f"{len(names)} {outcome}" + (f" ({', '.join(names)})" if names and outcome != "passed" else "")
                 for outcome, names in outcomes.items() if names or outcome in ("passed", "failed", "unmeasured")]
        return "  lanes: " + ", ".join(parts)

    def lines(self, status, error):
        where, directory = self.source()
        known = isinstance(status, int) and error is None
        if not known: verdict = "stopped" if error is not None else "not run"
        elif status == lane_results.UNMEASURED: verdict = "unmeasured"
        elif directory is None: verdict = "not run"
        else: verdict = "passed" if status == 0 else "failed"
        ending = f" (exit {status})" if isinstance(status, int) and status else ""
        # One line when nothing ran: the refusal or reason queuekeeper printed just before it says the rest.
        if directory is None: return [f"qk run {self.lane}: {verdict}{ending}; no tests ran"]
        lines = [f"qk run {self.lane}: {verdict}{ending}, {where}"]
        why = self.why or (str(error) if error is not None else None)
        if self.group is None: logs = {self.lane: directory / "run.log"}
        else: logs = {name: directory / "lanes" / f"{name}.log" for name in self.group}
        texts = {name: path.read_text(errors="replace") if path.is_file() else "" for name, path in logs.items()}
        kept = records(directory / "results")
        joined = "\n".join(texts.values())
        # qk run's status for a lane that stopped before its tests, which a command can also exit with itself.
        quoted = before_tests(joined) if verdict == "failed" and status == lane_results.BEFORE_TESTS and self.group is None \
            and not kept else None
        if quoted is not None:
            lines[0] = f"qk run {self.lane}: failed before any test ran{ending}, {where}"
            lines.append(f"  tests: none ran; the last error in its log: {quoted}")
        elif self.complete:
            parts = {name: counts(text) for name, text in texts.items()}
            present = [part for part in parts.values() if part]
            found = combined(present, status if known else None) or recorded_counts(kept)
            absent = [name for name, part in parts.items() if part is None] if present and self.group else ()
            lines.append(f"  tests: {said_tests(found, sum(map(unmeasured_count, texts.values())), absent)}")
        else:
            lines.append("  tests: no counts, as the run did not finish and its log is incomplete")
        if self.group is not None: lines.append(self.lanes_line(directory))
        if why: lines.append(f"  why: {why}")
        names = (recorded_failing(kept) or failing(joined)) if verdict != "passed" else []
        lines += [f"  failed: {name}" + (f" at {place}" if place else "") for name, place in names[:SHOWN]]
        if len(names) > SHOWN: lines.append(f"  ... and {len(names) - SHOWN} more")
        if quoted is not None:
            lines.append(f"  the log is {directory / 'run.log'}")
        elif verdict == "failed" and not names:
            log = directory / "run.log" if self.group is None else directory / "lanes"
            lines.append(f"  no failing test is named; the log is {log}")
        if kept:
            more = f" (and {len(kept) - 1} more under {directory / 'results'})" if len(kept) > 1 else ""
            lines.append(f"  details: {kept[0][0].with_name('failures.txt')}{more}")
        else:
            lines.append(f"  receipt: {directory / 'source.json'}")
        return lines

    def finish(self, status, error=None):
        """Print the summary, the last thing the run says. It never changes the run's result."""
        if self.lane is None or os.environ.get(remote.DISPATCHER): return
        try:
            lines = self.lines(status, error)
        except Exception as failure:
            lines = [f"qk run {self.lane}: exit {status}; its summary could not be made: {type(failure).__name__}: {failure}"]
        # A closed stream, as when a reader of the output has gone, must not turn the lane's status into a traceback.
        with contextlib.suppress(OSError, ValueError):
            sys.stdout.flush()
            print(*lines, sep="\n", file=sys.stderr, flush=True)
