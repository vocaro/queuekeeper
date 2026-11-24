"""A simulator's SpringBoard or backboardd that crashed under a test is the Mac's fault, not the commits', once a crash
report corroborates it.

On 2026-11-12 a BreezEPUB gate's smoke-iphone lane failed one UI test with "Critical process SpringBoard crashed in
(null)" while the Mac ran far over its busy limit, and sent back a test-only change that had nothing to do with it; the
same entry, added again unchanged, landed (vocaro/queuekeeper#12). The owner chose to give such a lane no verdict, gate
it again once the Mac settles, and hold the queue the second time it happens to the same lane at the same commit.

A failed run is a system process's crash (crash, queuekeeper.plugins.SYSTEM_CRASHES) only when both hold:
- every failure it names is XCTest's "Critical process NAME crashed" for SpringBoard or backboardd: each failed test of
  each result it kept (simulators.matrix.kept), each issue of those tests, each failure record, and each XCTest issue
  and failing test its log names. Any other failure, or a failed result whose tests are unknown, keeps the run failed.
- the simulator the result names has a crash report of that process timed inside the run: a report in a
  DiagnosticReports folder (REPORTS, or QUEUEKEEPER_DIAGNOSTIC_REPORTS, folders separated by colons) named for the
  process, naming the simulator's UDID, as its coalition com.apple.CoreSimulator.SimDevice.UDID does, and captured
  between the run's start and its end.
"""
from datetime import datetime
import json
import os
from pathlib import Path
import re

from queuekeeper import lane_report
from .simulators import matrix

PROCESSES = ("SpringBoard", "backboardd")
CRASHED = re.compile(r"\bCritical process (SpringBoard|backboardd) crashed\b")
# Where macOS writes crash reports, a simulator's processes' among them; QUEUEKEEPER_DIAGNOSTIC_REPORTS replaces it.
REPORTS = (Path.home() / "Library/Logs/DiagnosticReports",)
VARIABLE = "QUEUEKEEPER_DIAGNOSTIC_REPORTS"
# How long after the run's end a report may still say it was captured, for a clock read a moment apart.
SLACK = 5
ISSUE = "vocaro/queuekeeper#12"


def key(name):
    """A test's name by its last two parts, as Class.test, however a list spells it (lane_report.recorded_failing)."""
    return ".".join([part for part in re.split(r"[./ \[\]-]+", name.replace("()", "")) if part][-2:])


def crashed(message):
    """The process message names as crashed, or None for any other message."""
    found = CRASHED.search(message or "")
    return found.group(1) if found else None


def kept_runs(results):
    """Each result a run kept in results, at any depth, as what its KIND.json holds."""
    found = []
    for path in sorted(Path(results).rglob(f"{matrix.KEPT}/*.json")) if Path(results).is_dir() else ():
        if "failures" in path.relative_to(results).parts: continue
        try: record = json.loads(path.read_text())
        except (OSError, ValueError): continue
        if isinstance(record, dict) and "tests" in record: found.append(record)
    return found


def simulator(record):
    """The UDID of the one simulator a kept result ran on, or None."""
    summary = record.get("summary") or {}
    places = summary.get("devicesAndConfigurations") or []
    ids = {str((place.get("device") or {}).get("deviceId") or "") for place in places if isinstance(place, dict)}
    return ids.pop() if len(ids) == 1 and "" not in ids else None


def crashes_only(results, log):
    """{test key: (process, UDID)} of the failed tests of a failed run, when every failure it names is a system
    process's crash on a known simulator; None when any failure is something else, or none is named."""
    failed = {}
    for record in kept_runs(results):
        if record.get("bundleUnread"):
            if record.get("status") not in (0, None): return None
            continue
        tests = [case for case in record.get("tests") or [] if case.get("outcome") == "failed"]
        if record.get("status") not in (0, None) and not tests: return None
        udid = simulator(record) if tests else None
        for case in tests:
            issues = [issue for issue in case.get("issues") or [] if not issue.get("expected")]
            processes = {crashed(issue.get("message")) for issue in issues}
            if not issues or len(processes) != 1 or None in processes or udid is None: return None
            failed[key(case["test"])] = (processes.pop(), udid)
        for failure in (record.get("summary") or {}).get("testFailures") or []:
            if not isinstance(failure, dict) or not crashed(failure.get("failureText")): return None
    if not failed: return None
    for _path, record in lane_report.records(Path(results)):
        for test in record.get("failedTests") or []:
            if key(test.get("test", "")) not in failed: return None
            if any(not crashed(item.get("message")) for item in test.get("failures") or [] if "message" in item):
                return None
    if lane_report.SWIFT_TESTING_ISSUE.search(log): return None
    for line in log.splitlines():
        match = lane_report.XCTEST_ISSUE.match(line)
        if match and not crashed(line[match.end():]): return None
    if any(key(name) not in failed for name, _ in lane_report.failing(log)): return None
    return failed


def folders():
    said = os.environ.get(VARIABLE)
    return [Path(folder) for folder in said.split(":") if folder] if said else list(REPORTS)


def captured(text, path):
    """When the crash report whose text is text says it was captured, as an epoch time, else its file's time; and the
    process it names, or None."""
    header = text.split("\n", 1)[0]
    try: said = json.loads(header)
    except ValueError: said = {}
    if not isinstance(said, dict): said = {}
    name = said.get("app_name") or said.get("name")
    try: at = datetime.strptime(said["timestamp"], "%Y-%m-%d %H:%M:%S.%f %z").timestamp()
    except (KeyError, TypeError, ValueError):
        try: at = path.stat().st_mtime
        except OSError: return None, name
    return at, name


def report(process, udid, began, ended):
    """(path, capture time) of a crash report of process on the simulator udid captured between began and ended, or
    None."""
    for folder in folders():
        try: paths = sorted(folder.glob(f"{process}-*.ips")) + sorted(folder.glob(f"{process}_*.crash"))
        except OSError: continue
        for path in paths:
            try:
                if path.stat().st_mtime < began: continue
                text = path.read_text(errors="replace")
            except OSError: continue
            if udid.lower() not in text.lower(): continue
            at, name = captured(text, path)
            if name not in (None, process) or at is None: continue
            if began <= at <= ended + SLACK: return path, at
    return None


def crash(results, log, began, ended):
    """What corroborates that the failed run whose results folder is results, whose log is log and which ran from
    began to ended, epoch times, failed only for a simulator's system process that crashed, or None
    (queuekeeper.plugins.SYSTEM_CRASHES): {"words", "process", "simulator", "report", "captured", "tests"}."""
    failed = crashes_only(results, log)
    if not failed: return None
    reports = {}
    for process, udid in set(failed.values()):
        found = report(process, udid, began, ended)
        if found is None: return None
        reports[(process, udid)] = found
    (process, udid), (path, at) = min(reports.items(), key=lambda item: item[1][1])
    tests = sorted(failed)
    words = (f"{', '.join(tests)} failed only because the simulator {udid}'s {process} crashed, which its crash "
             f"report {path} from inside the run corroborates: that is the Mac's fault, not the commits' ({ISSUE})")
    return {"words": words, "process": process, "simulator": udid, "report": str(path), "captured": at,
            "tests": tests}
