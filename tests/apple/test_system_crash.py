"""A failed run whose only failures are XCTest's "Critical process SpringBoard crashed", which a crash report of that
simulator's process from inside the run corroborates, is the Mac's fault; anything less stays failed
(vocaro/queuekeeper#12)."""
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch
import host_isolation
from queuekeeper import plugins
from queuekeeper_apple import system_crash

UDID = "6A1B2C3D-0000-4000-8000-00000000ABCD"
CRASH = "Critical process SpringBoard crashed in (null)"
LOG = "Test Case '-[ReadingTests testRelaunch]' failed (3.000 seconds).\n"


def kept(results, tests, udid=UDID, status=65, name="smoke"):
    """A result qk simulator-tests keeps (simulators.matrix.kept), of tests run on the simulator udid."""
    folder = Path(results) / "simulator-tests"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.json").write_text(json.dumps({
        "run": name, "keptBy": "qk simulator-tests", "status": status,
        "summary": {"devicesAndConfigurations": [{"device": {"deviceId": udid}}]}, "tests": tests}))


def case(test, *messages):
    return {"test": test, "outcome": "failed", "seconds": 1.0, "issues": [{"message": m} for m in messages]}


def report(folder, process="SpringBoard", udid=UDID, at=None):
    """A crash report of process on the simulator udid, captured at at, an epoch time, as macOS writes one."""
    at = time.time() if at is None else at
    stamp = datetime.fromtimestamp(at).astimezone().strftime("%Y-%m-%d %H:%M:%S.%f %z")
    path = Path(folder) / f"{process}-{time.time_ns()}.ips"
    path.write_text(json.dumps({"app_name": process, "timestamp": stamp, "name": process}) + "\n"
                    + json.dumps({"procName": process, "coalitionName": f"com.apple.CoreSimulator.SimDevice.{udid}"}))
    return path


class Crash(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="system-crash-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.results, self.reports = self.root / "results", self.root / "reports"
        self.reports.mkdir()
        patcher = patch.dict(os.environ, {system_crash.VARIABLE: str(self.reports)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.began = time.time() - 60

    def crash(self, log=LOG):
        return system_crash.crash(self.results, log, self.began, time.time())

    def test_a_corroborated_springboard_crash_is_the_macs_fault(self):
        kept(self.results, [case("ReadingTests/testRelaunch()", CRASH)])
        path = report(self.reports)
        found = self.crash()
        self.assertEqual((found["process"], found["simulator"], found["report"]), ("SpringBoard", UDID, str(path)))
        self.assertEqual(found["tests"], ["ReadingTests.testRelaunch"])
        self.assertIn("the Mac's fault, not the commits'", found["words"])

    def test_backboardd_counts_too(self):
        kept(self.results, [case("ReadingTests/testRelaunch()", "Critical process backboardd crashed")])
        report(self.reports, "backboardd")
        self.assertEqual(self.crash()["process"], "backboardd")

    def test_the_hook_reads_it_once_the_plugin_registers(self):
        with patch.object(plugins, "SYSTEM_CRASHES", [system_crash.crash]):
            kept(self.results, [case("ReadingTests/testRelaunch()", CRASH)])
            report(self.reports)
            self.assertEqual(plugins.system_crash(self.results, LOG, self.began, time.time())["simulator"], UDID)

    def test_a_crash_message_without_a_report_from_inside_the_run_stays_failed(self):
        kept(self.results, [case("ReadingTests/testRelaunch()", CRASH)])
        self.assertIsNone(self.crash())
        for made in (report(self.reports, udid="99999999-0000-4000-8000-000000000000"),
                     report(self.reports, at=self.began - 30), report(self.reports, "backboardd")):
            with self.subTest(report=made.name):
                self.assertIsNone(self.crash())

    def test_any_other_failure_keeps_the_run_failed(self):
        report(self.reports)
        mixed = {
            "another failed test": ([case("ReadingTests/testRelaunch()", CRASH),
                                     case("ReadingTests/testScroll()", "XCTAssertEqual failed")], LOG),
            "another issue in the crashed test": ([case("ReadingTests/testRelaunch()", CRASH, "XCTAssertTrue failed")],
                                                  LOG),
            "a failure only the log names": ([case("ReadingTests/testRelaunch()", CRASH)],
                                             LOG + "Test Case '-[ReadingTests testScroll]' failed (1.000 seconds).\n"),
            "an XCTest issue in the log": ([case("ReadingTests/testRelaunch()", CRASH)],
                                           LOG + "/s/ReadingTests.swift:12: error: -[ReadingTests testRelaunch] : "
                                                 "XCTAssertTrue failed\n"),
            "a unittest failure in the log": ([case("ReadingTests/testRelaunch()", CRASH)],
                                              LOG + "FAIL: test_x (m.Case.test_x)\n"),
            "no test named": ([], LOG),
        }
        for name, (tests, log) in mixed.items():
            with self.subTest(name):
                shutil.rmtree(self.results, ignore_errors=True)
                kept(self.results, tests)
                self.assertIsNone(self.crash(log))

    def test_a_failed_result_whose_tests_are_unknown_stays_failed(self):
        report(self.reports)
        kept(self.results, [case("ReadingTests/testRelaunch()", CRASH)])
        (self.results / "simulator-tests" / "other.json").write_text(json.dumps(
            {"run": "other", "status": 65, "tests": None, "bundleUnread": "xcresulttool could not read it"}))
        self.assertIsNone(self.crash())


if __name__ == "__main__":
    unittest.main()
