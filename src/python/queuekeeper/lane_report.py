"""The summary that ends every `qk run` (vocaro-engineering#274).

A lane's outcome used to be one `remote:` line in the middle of its output, and its counts and failing tests lay only
in a file. Each run now ends with its verdict, its passed, failed, skipped and unmeasured tests, each failing test
with its file and line, and the path to its failures.txt or its receipt. It reads only what this run wrote: a run's
log and results here (ran_here), in a directory of its own when a run of the same lane beside it holds the lane's
(cli.lane_directory), or the dispatch it sent after it began (remote.dispatch), and never an earlier run's or
one a run of the same lane beside it sent (vocaro/queuekeeper#9). Passed, failed, skipped and unmeasured stay apart, a
lane is passed only when it ran and returned 0, and a lane whose log counts no test says so rather than printing zeros.
It also gives the load of the Mac the lane ran on over its run, which the receipt keeps (RunLoad, load_line;
vocaro/queuekeeper#133).

The lines go to stderr with queuekeeper's other notes, after stdout is flushed, so that they are last however the two are
joined. They begin `qk run LANE:` or are indented, and quote no line of a test's own, since a lane's log is read
again by what runs it: the merge queue's log, lane_results.counted and failure_records read none of them as a result. A
run that ended with no verdict of a pass, and whose log counts no test and names none, has its command's own last
lines quoted, which are then no test's (own_output, vocaro/queuekeeper#137). Another
Mac's own run prints none: the Mac that sent it prints the summary from what came back. The merge queue reads a
summary back by its own lines alone (summary), to name a failed gate's tests in a send-back (vocaro/queuekeeper#83).
"""
import contextlib
import json
import os
from pathlib import Path
import re
import shlex
import sys
import threading
import time

from . import lane_results, remote, resources

# The failing tests a summary names; the rest are in its failures.txt or log.
SHOWN = 20

# XCTest's line for a failed test, once per run of it, and its total, whose unexpected failures are assertions. Each
# line counts as a failed test, as its total counts each run as a test: xcodebuild's -test-iterations N runs a test N
# times under one name, and 5 failed iterations read as 4 passed, 1 failed while the name counted once
# (vocaro/queuekeeper#140).
XCTEST_FAILED = re.compile(r"^Test [Cc]ase '([^'\n]+)' failed\b", re.M)
XCTEST_TOTAL = re.compile(r"Executed \d+ tests?, with [^\n(]*\((\d+) unexpected\)")
# FILE:LINE: error: -[Module.Class test] : message
XCTEST_ISSUE = re.compile(r"^([^\s:][^:\n]*):(\d+): error: -\[([^\]\n]+)\] : ", re.M)
# Swift Testing's lines for a failed test and an issue it recorded, not its line for the whole run (Swift 6.4).
SWIFT_TESTING_FAILED_RUN = re.compile(r"Test run with \d+ tests?(?: in \d+ suites?)? failed")
SWIFT_TESTING_FAILED = re.compile(r"^\S+ Test (?!run with \d)(.+?) failed after ", re.M)
SWIFT_TESTING_ISSUE = re.compile(r"^\S+ Test (?!run with \d)(.+?) recorded an issue at ([^:\n]+):(\d+)", re.M)
# qk simulator-tests' line for a device, which counts the tests that failed on it apart, as the device's result bundle or
# its console does: `Executed 5 tests, 1 skipped, 2 failed`. The lines that name them are in the device's log alone.
DEVICE_FAILED = re.compile(r"Executed \d+ tests(?:, \d+ skipped)?, (\d+) failed\b")
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
# The most lines of a lane's own output a summary quotes (own_output), each cut at QUOTED.
OUTPUT_SHOWN = 5
# The line a summary puts over them.
OUTPUT_HEAD = "  the last of its output:"
# A summary as Report.lines writes it, for what reads one back (summary): its first line, `qk run LANE: VERDICT...`,
# or `eng run LANE:` in logs from before the command's rename, and its lines indented by two spaces after it.
VERDICTS = ("passed", "failed", "stopped", "not run", "unmeasured")
SUMMARY_HEAD = re.compile(r"^(?:qk|eng) run (\S+): ((?:" + "|".join(VERDICTS) + r")\b.*)$", re.M)
SUMMARY_TESTS = re.compile(r"^  tests: (?:(\d+) passed, (\d+) failed\b)?")
SUMMARY_FAILED = re.compile(r"^  failed: (.+?)(?: at (\S[^\n]*:\d+))?$")
SUMMARY_MORE = re.compile(r"^  \.\.\. and (\d+) more$")
# The counts a summary's tests line states (said_tests), which the record of a failed gate reads back (summary_counts,
# vocaro/queuekeeper#193).
SUMMARY_COUNTS = re.compile(r"^(?:(\d+) passed, (\d+) failed|(\d+) ran, and the log does not say how many of them "
                            r"failed), (\d+) skipped, (\d+) unmeasured\b")
# Seconds between readings of the load of the Mac a lane runs on, which its receipt keeps (RunLoad), as a gate's are
# read for its send-back (merge_queue.BUSY_SAMPLE).
LOAD_SAMPLE = 60
# The end of the one line a summary is when nothing ran, and the starts of the line that ends any other.
NOTHING_RAN = "; no tests ran"
SUMMARY_ENDS = ("  details: ", "  receipt: ")


def failed_tests(text):
    """(the tests a log reports failed, or None when its totals say some failed but name none, the errors pytest
    counts apart from its tests). Within a qk unittest process's output only unittest's own totals count, as for the
    tests that ran (lane_results.outside_unittest)."""
    total, errors = 0, 0
    for match in lane_results.RAN.finditer(text):
        total += sum(int(n) for n in re.findall(r"\b(?:failures|errors)=(\d+)", match.group(2) or ""))
    text = lane_results.outside_unittest(text)
    for match in lane_results.PYTEST_SUMMARY.finditer(text):
        items = {word: int(n) for n, word in re.findall(r"(\d+) (\w+)", match.group(1))}
        total += items.get("failed", 0) + items.get("error", 0) + items.get("errors", 0)
        errors += items.get("error", 0) + items.get("errors", 0)
    total += sum(int(n) for n in DEVICE_FAILED.findall(text))
    cases, tests = XCTEST_FAILED.findall(text), set(SWIFT_TESTING_FAILED.findall(text))
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


def own_output(text, unmeasured=False):
    """What a lane's command itself last said, for the summary of a run that failed or ended unmeasured and whose log
    counts no test and names none: the last OUTPUT_SHOWN lines of text, its log, that are not blank and not
    queuekeeper's own, each cut at QUOTED. For an unmeasured run, the lines in which the log gives the project's own
    reason instead, when it has any (lane_results.unmeasured_reasons, vocaro/queuekeeper#30). On 2026-11-13 a lane
    whose script refused its arguments left its one line, the whole answer, only in its dispatch's run.log
    (vocaro/queuekeeper#137)."""
    said = lane_results.unmeasured_reasons(text) if unmeasured else []
    said = said or [line.rstrip() for line in text.splitlines() if line.strip() and not line.startswith("queuekeeper:")]
    return [line[:QUOTED] for line in said[-OUTPUT_SHOWN:]]


def not_started(receipt):
    """Why the lane a receipt is of could not start, on one line, or None: its outcome is not-started, with why under
    notStarted (cli.not_started). A dispatch's receipt holds the lane's own from another Mac under result, as
    receipt and as the result itself. The receipt says so, not the log, since a command may print or exit anything."""
    there = receipt.get("result") if isinstance(receipt, dict) else None
    for said in (there.get("receipt") if isinstance(there, dict) else None, there, receipt):
        if not isinstance(said, dict) or said.get("outcome") != "not-started": continue
        why = said.get("notStarted")
        return " ".join(why.split()) if isinstance(why, str) and why.strip() else "its command did not start"
    return None


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


def failed_line(name, place):
    """A summary's line for one failing test, which summary reads back."""
    return f"  failed: {name}" + (f" at {place}" if place else "")


def summary(text, lane):
    """The last summary of a run of lane that text, a log, holds, as Report.lines wrote it, or None when it holds
    none: {"verdict": what follows `LANE: `, "tests": its counts line's words or None, "failed": how many its counts
    say failed or None, "names": each failing test it names as (name, FILE:LINE or None), "more": how many more it
    left out (SHOWN)}. It reads only the summary's own lines, never a test's output (vocaro/queuekeeper#83)."""
    heads = [head for head in SUMMARY_HEAD.finditer(text) if head.group(1) == lane]
    if not heads: return None
    found = {"verdict": heads[-1].group(2), "tests": None, "failed": None, "names": [], "more": 0}
    if found["verdict"].endswith(NOTHING_RAN): return found
    following = text[heads[-1].end():].removeprefix("\n").splitlines()
    # Its last line gives the details or the receipt. A why can span lines, so its lines in between that are not
    # indented are passed over; without that last line, as in a log cut off, only its first indented lines count.
    end = next((number for number, line in enumerate(following) if line.startswith(SUMMARY_ENDS)), None)
    if end is None: end = next((number for number, line in enumerate(following) if not line.startswith("  ")), len(following))
    for line in following[:end + 1]:
        if not line.startswith("  "): continue
        tests, failed, more = SUMMARY_TESTS.match(line), SUMMARY_FAILED.match(line), SUMMARY_MORE.match(line)
        if tests:
            found["tests"] = line[len("  tests: "):]
            if tests.group(2) is not None: found["failed"] = int(tests.group(2))
        elif failed: found["names"].append(failed.groups())
        elif more: found["more"] = int(more.group(1))
    return found


def run_summary(text, name=None):
    """The summary of the run that wrote text, a log (summary): of the lane that name, the log's file name, ends with
    as `-LANE.log`, as a queue round's gate log is named, when the log holds that lane's summary, else of the lane whose
    summary comes last. None when the log holds none. A group's gate log holds its lanes' own output, and only the
    group's summary counts and names the failed tests of every lane in it (vocaro/queuekeeper#193)."""
    lanes = [head.group(1) for head in SUMMARY_HEAD.finditer(text)]
    if not lanes: return None
    named = [lane for lane in lanes if name and name.endswith(f"-{lane}.log")]
    return summary(text, max(named, key=len) if named else lanes[-1])


def summary_counts(found):
    """The counts a summary (summary) states, as counts gives a log's, or None when it states none."""
    match = SUMMARY_COUNTS.match((found or {}).get("tests") or "")
    if match is None: return None
    passed, failed, ran, skipped, unmeasured = match.groups()
    if ran is None:
        passed, failed = int(passed), int(failed)
        ran = passed + failed
    else:
        ran = int(ran)
    return {"passed": passed, "failed": failed, "skipped": int(skipped), "unmeasured": int(unmeasured), "ran": ran}


def readings_over(found, of):
    """How many of the readings of were read, found of them: `N readings` when all were, else `N of M readings`."""
    return f"{found} reading{'' if found == 1 else 's'}" if found == len(of) else f"{found} of {len(of)} readings"


def load_figures(readings):
    """What readings say of the load of a Mac, each (the one-minute load, the CPU busy share in percent, the busy limit
    it is judged by) with None for what was not read, or None for a reading that failed: (`the one-minute load was
    highest H, last L over N readings, and the CPU was at most B% busy`, with `could not be read` for either one that
    no reading gives, and whether any reading gave either). A queue gate's send-back says these (merge_queue.load_words),
    and so does the summary of a lane's run (load_line)."""
    loads = [reading[0] for reading in readings if reading and reading[0] is not None]
    shares = [reading[1] for reading in readings if reading and reading[1] is not None and reading[2] is not None]
    load = (f"was highest {max(loads):g}, last {loads[-1]:g} over {readings_over(len(loads), readings)}" if loads
            else "could not be read")
    busy = f"was at most {max(shares):g}% busy" if shares else "busy share could not be read"
    return f"the one-minute load {load}, and the CPU {busy}", bool(loads or shares)


class RunLoad:
    """The load of this Mac over a lane's run, for its receipt (vocaro/queuekeeper#133): the one-minute load and the
    CPU's busy share against its busy limit, read every LOAD_SAMPLE seconds while the lane runs and once at its end, as
    a queue gate's are for its send-back (merge_queue.LoadSampler). Another Mac's own run of a lane sent there reads
    that Mac, and its receipt comes back with the result. A busy share needs a sample at least resources.BUSY_MINIMUM
    seconds old, so the one at the end of a shorter run spans the whole run, and none waits. A measurement only: it
    decides nothing, and nothing a reading raises reaches the lane."""
    def __init__(self):
        self.readings, self.stopped = [], threading.Event()
        self.first = self.busy()[1]
        self.thread = threading.Thread(target=self.every, name="lane load", daemon=True)

    @staticmethod
    def busy():
        """(the busy share now, or None, as resources.cpu_busy reads it without waiting, the CPU's ticks now, or None),
        each reading also kept for later readings to span."""
        try: ticks = resources.cpu_ticks()
        except Exception: ticks = None  # noqa: BLE001
        try: share = resources.cpu_busy(resources.slot_directory())[0]
        except Exception: share = None  # noqa: BLE001
        return share, ticks

    def read(self, last=False):
        share, ticks = self.busy()
        if share is None and last and self.first is not None and ticks is not None:
            share = resources.busy_share(self.first, ticks)
        try: limit = resources.busy_limit()
        except Exception: limit = None  # noqa: BLE001
        self.readings.append([resources.one_minute_load(), share, limit])

    def every(self):
        while not self.stopped.wait(LOAD_SAMPLE):
            try: self.read()
            except Exception: pass  # noqa: BLE001

    def start(self):
        self.thread.start()
        return self

    def finish(self):
        """What the receipt keeps as load: every reading, [one-minute load, busy share, busy limit] with null for
        what was not read; the seconds between them, every; and of those read, oneMinuteHighest, oneMinuteLast,
        cpuBusyHighest and the last busyLimit."""
        self.stopped.set()
        if self.thread.ident is not None: self.thread.join(timeout=5)
        try: self.read(last=True)
        except Exception: pass  # noqa: BLE001
        found = {"readings": list(self.readings), "every": LOAD_SAMPLE}
        loads = [reading[0] for reading in self.readings if reading[0] is not None]
        shares = [reading[1:] for reading in self.readings if reading[1] is not None and reading[2] is not None]
        if loads: found.update(oneMinuteHighest=max(loads), oneMinuteLast=loads[-1])
        if shares: found.update(cpuBusyHighest=max(share for share, _ in shares), busyLimit=shares[-1][1])
        return found


def load_line(receipt, verdict):
    """The summary's line for the load of the Mac a lane ran on over its run, from its receipt's load (RunLoad), another
    Mac's for a run there; for a run that did not pass and whose receipt keeps none, that none was kept; else
    None. A lane that failed there on 2026-11-13 left its load to be read by hand (vocaro/queuekeeper#133)."""
    load = receipt.get("load")
    readings = load.get("readings") if isinstance(load, dict) else None
    if isinstance(readings, list):
        def figure(value):
            return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None
        kept = [[figure(value) for value in reading] if isinstance(reading, list) and len(reading) == 3 else None
                for reading in readings]
        return f"  load: {load_figures(kept)[0]}"
    if verdict != "passed": return "  load: its receipt keeps no reading of the load of the Mac it ran on"
    return None


def lane_receipt(directory):
    """The lane's own receipt in directory, a run's: its source.json, or for a dispatch the one another Mac wrote,
    which it holds under result; {} when there is none."""
    try: receipt = json.loads((Path(directory) / "source.json").read_text())
    except (OSError, ValueError): return {}
    if not isinstance(receipt, dict): return {}
    there = receipt.get("result")
    return there["receipt"] if isinstance(there, dict) and isinstance(there.get("receipt"), dict) else receipt


def stopped_there(directory):
    """Why another Mac's run of a dispatch in directory ended unmeasured when its timeout stopped it, as its receipt
    says, such as `Owned command timed out after 2400s: python3; ...`, or None. Another Mac's own summary is not
    printed, and its supervise line goes to the dispatch's output.log, so without it a log that stops mid-run read as a
    crash (vocaro/queuekeeper#37)."""
    receipt = lane_receipt(directory)
    error = receipt.get("error")
    return " ".join(error.split()) if receipt.get("outcome") == "unmeasured" and isinstance(error, str) and error.strip() \
        else None


def unittest_launcher(command):
    """The words of a lane's command that start qk, when what follows them is `unittest`, or None: a lane whose
    command is qk unittest, which `qk run LANE -- NAME...` runs for those names alone."""
    if not isinstance(command, list) or not all(isinstance(word, str) for word in command): return None
    return next((command[:index + 1] for index, word in enumerate(command[:-1])
                 if Path(word).name == "qk" and command[index + 1] == "unittest"), None)


def unreached_lines(directory, lane, group=None):
    """The lines that name what a run's qk unittest did not finish, from the record it left in the run's results
    (parallel.keep_unreached): how many tests, where they are listed, and the command that runs them alone. For a lane,
    or a group's lane, whose command is qk unittest and that ran with no arguments of its own, that command is
    `qk run LANE -- NAME...`; otherwise it is the qk unittest command the record gives. Nothing without a record."""
    from .parallel import UNREACHED_FILE, UNREACHED_RECORD
    results, lines = Path(directory) / "results", []
    for path in sorted(results.rglob(f"unittest/{UNREACHED_RECORD}")) if results.is_dir() else ():
        try: record = json.loads(path.read_text())
        except (OSError, ValueError): continue
        if not isinstance(record, dict) or not isinstance(record.get("tests"), list): continue
        # results/unittest/ is the lane's own; results/MEMBER/unittest/ a group's lane's.
        within = path.parent.parent.relative_to(results).parts
        member = lane if not within else within[0] if group and within[0] in group else None
        if member == lane and not group:
            receipt = lane_receipt(directory)
            launcher = None if receipt.get("arguments") else unittest_launcher(receipt.get("command"))
        elif member is not None:
            try: launcher = unittest_launcher(json.loads((Path(directory) / "lanes" / f"{member}.json").read_text()).get("command"))
            except (OSError, ValueError, AttributeError): launcher = None
        else:
            launcher = None
        names = [name for name in record.get("names") or [] if isinstance(name, str)]
        command = [*launcher, "run", member, "--", *names] if launcher and names else record.get("command")
        lines.append(f"  unreached: {len(record['tests'])} tests did not finish ({record.get('result') or 'unmeasured'}); "
                     f"the list is in {path.with_name(UNREACHED_FILE)}")
        if isinstance(command, list) and all(isinstance(word, str) for word in command):
            lines.append(f"  run them: {shlex.join(command)}")
    return lines


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
        # This process's own dispatch, not one that a run of the lane beside it, from this checkout, sent since
        # (vocaro/queuekeeper#9).
        ran, host = remote.dispatch(self.root, self.lane, self.began, os.getpid())
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
        elif directory is None or status == resources.BUSY: verdict = "not run"
        else: verdict = "passed" if status == 0 else "failed"
        ending = f" (exit {status})" if isinstance(status, int) and status else ""
        # One line when nothing ran: the refusal or reason queuekeeper printed just before it says the rest.
        if directory is None: return [f"qk run {self.lane}: {verdict}{ending}{NOTHING_RAN}"]
        lines = [f"qk run {self.lane}: {verdict}{ending}, {where}"]
        if verdict == "failed" and status == lane_results.NOT_STARTED and self.group is None:
            try: unstarted = not_started(json.loads((directory / "source.json").read_text()))
            except (OSError, ValueError): unstarted = None
            if unstarted is not None:
                return [f"qk run {self.lane}: could not start{ending}, {where}",
                        f"  tests: none ran, as its command never started: {unstarted}",
                        f"  receipt: {directory / 'source.json'}"]
        why = self.why or (str(error) if error is not None else None) or \
            (stopped_there(directory) if self.here is None and verdict == "unmeasured" else None)
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
        load = load_line(lane_receipt(directory), verdict)
        if load: lines.append(load)
        if why: lines.append(f"  why: {why}")
        names = (recorded_failing(kept) or failing(joined)) if verdict != "passed" else []
        lines += [failed_line(name, place) for name, place in names[:SHOWN]]
        if len(names) > SHOWN: lines.append(f"  ... and {len(names) - SHOWN} more")
        if quoted is not None:
            lines.append(f"  the log is {directory / 'run.log'}")
        elif verdict == "failed" and not names:
            log = directory / "run.log" if self.group is None else directory / "lanes"
            lines.append(f"  no failing test is named; the log is {log}")
        # The command's own last words, when they are all the run left: nothing here counts or names a test, and
        # queuekeeper gives no reason of its own (vocaro/queuekeeper#137).
        if known and verdict in ("failed", "unmeasured") and self.group is None and self.complete and quoted is None \
                and not why and not names and not kept and counts(joined) is None and not unmeasured_count(joined):
            said = own_output(joined, verdict == "unmeasured")
            if said: lines += [OUTPUT_HEAD, *(f"    {line}" for line in said)]
        if verdict != "passed": lines += unreached_lines(directory, self.lane, self.group)
        if kept:
            more = f" (and {len(kept) - 1} more under {directory / 'results'})" if len(kept) > 1 else ""
            lines.append(f"{SUMMARY_ENDS[0]}{kept[0][0].with_name('failures.txt')}{more}")
        else:
            lines.append(f"{SUMMARY_ENDS[1]}{directory / 'source.json'}")
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
