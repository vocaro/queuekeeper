"""The summary that ends every `qk run` (vocaro-engineering#274): what it reads from a run's own log and records,
that passed, failed, skipped and unmeasured stay apart, and that it is the last thing the run says."""
import contextlib
from io import StringIO
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src/python"))
import host_isolation  # noqa: F401 (keeps the tests off the real hosts)
from host_isolation import stretched
from queuekeeper import cli, lane_results, lane_report, merge_queue
from queuekeeper.waiting import wait_until

SOURCE = Path(__file__).resolve().parents[2] / "src/python"
RULE = "=" * 70
LINE = "-" * 70

UNITTEST_FAILED = f"""{RULE}
FAIL: test_b (tests.test_x.X.test_b)
{LINE}
Traceback (most recent call last):
  File "/project/tests/test_x.py", line 12, in test_b
    self.helper()
  File "/project/tests/test_x.py", line 7, in helper
    self.fail("no")
AssertionError: no

{RULE}
ERROR: test_c (tests.test_x.X.test_c)
{LINE}
Traceback (most recent call last):
  File "/project/tests/test_x.py", line 20, in test_c
    raise OSError
OSError

{LINE}
Ran 6 tests in 0.1s

FAILED (failures=1, errors=1, skipped=1)
"""
PYTEST_FAILED = """____________ Reader.test_page[1] ____________

    def test_page():
tests/test_reader.py:15: AssertionError
=========== short test summary info ===========
FAILED tests/test_reader.py::Reader::test_page[1] - AssertionError: no
========= 1 failed, 4 passed, 2 skipped in 0.51s =========
"""
XCTEST_FAILED = """Test Case '-[Mod.ReaderTests testPage]' started.
/project/Tests/ReaderTests.swift:42: error: -[Mod.ReaderTests testPage] : XCTAssertEqual failed
/project/Tests/ReaderTests.swift:43: error: -[Mod.ReaderTests testPage] : XCTAssertTrue failed
Test Case '-[Mod.ReaderTests testPage]' failed (0.1 seconds).
Test Suite 'All tests' failed at 2026-11-07 10:00:00.000.
\t Executed 5 tests, with 1 test skipped and 2 failures (2 unexpected) in 0.1 (0.2) seconds
"""
SETUP_FAILED = """Generating BreezEPUB.xcodeproj from project.yml
Spec validation error: Invalid local package "EngineeringTestSupport"
Failed: project generation or canonical package-lock seeding failed
"""
SWIFT_TESTING_FAILED = """✘ Test page() recorded an issue at PageTests.swift:9:4: Expectation failed
✘ Test page() failed after 0.1 seconds with 1 issue.
✘ Test run with 3 tests in 1 suite failed after 0.2 seconds with 1 issue.
"""


def record(results, name, **fields):
    directory = results / "failures" / name
    directory.mkdir(parents=True)
    (directory / "failures.json").write_text(json.dumps({"failedTests": [], "logged": [], "failingTests": [], **fields}))
    (directory / "failures.txt").write_text("what failed\n")
    return directory / "failures.txt"

# The summary's line for the one reading Summary's runs take of this Mac's load, at their end (RunLoad).
LOAD = "  load: the one-minute load was highest 12.5, last 12.5 over 1 reading, and the CPU was at most 40% busy"

SWIFT_TESTING_BESIDE_EMPTY_XCTEST = """Test Suite 'EPUBViewingTests.xctest' started at 2026-11-11 20:14:18.000.
Test Suite 'EPUBViewingTests.xctest' passed at 2026-11-11 20:14:18.001.
\t Executed 0 tests, with 0 failures (0 unexpected) in 0.000 (0.001) seconds
\u25c7 Test "testACellKeepsItsWeight()" started.
\u2718 Test "testACellKeepsItsWeight()" recorded an issue at InkTests.swift:12:5: Expectation failed: weight == 700
\u2718 Test "testACellKeepsItsWeight()" failed after 65.382 seconds with 1 issue.
\u2718 Test run with 1 test in 1 suite failed after 65.400 seconds with 1 issue.
"""


class Parsing(unittest.TestCase):
    def assert_counts(self, text, passed, failed, skipped, unmeasured=0):
        found = lane_report.counts(text)
        self.assertEqual((found["passed"], found["failed"], found["skipped"], found["unmeasured"]),
                         (passed, failed, skipped, unmeasured))

    def test_unittest_counts_and_each_failure_with_its_innermost_frame(self):
        self.assert_counts(UNITTEST_FAILED, 3, 2, 1)
        self.assertEqual(lane_report.failing(UNITTEST_FAILED), [("tests.test_x.X.test_b", "/project/tests/test_x.py:7"),
                                                                ("tests.test_x.X.test_c", "/project/tests/test_x.py:20")])

    def test_a_passing_unittest_log_names_no_failure(self):
        text = "Ran 2 tests in 0.1s\n\nOK (skipped=1)\n"
        self.assert_counts(text, 1, 0, 1)
        self.assertEqual(lane_report.failing(text), [])

    def test_pytest_counts_and_a_failure_with_the_place_its_report_gives(self):
        self.assert_counts(PYTEST_FAILED, 4, 1, 2)
        self.assertEqual(lane_report.failing(PYTEST_FAILED),
                         [("tests/test_reader.py::Reader::test_page[1]", "tests/test_reader.py:15")])

    def test_pytest_errors_count_as_failed_though_pytest_counts_them_apart_from_its_tests(self):
        self.assert_counts("=== 2 passed, 1 error in 0.1s ===\n", 2, 1, 0)

    def test_xctest_counts_a_test_with_two_assertions_once(self):
        self.assert_counts(XCTEST_FAILED, 3, 1, 1)
        self.assertEqual(lane_report.failing(XCTEST_FAILED),
                         [("-[Mod.ReaderTests testPage]", "/project/Tests/ReaderTests.swift:42")])

    def test_xctest_counts_each_iteration_of_a_repeated_test_by_its_own_outcome(self):
        # xcodebuild's -test-iterations runs a test again under the same name, and its total counts each run; five
        # failed iterations read as 4 passed, 1 failed (vocaro/queuekeeper#140).
        name = "-[UITests.LargestTextAccessibilityTests testAvailableSupportAccessibility]"
        def iterations(outcomes):
            failures = outcomes.count("failed")
            lines = [f"Test Case '{name}' started (Iteration {number} of {len(outcomes)}).\n"
                     + (f"/project/UITests/Largest.swift:30: error: {name} : XCTAssertTrue failed\n" if outcome == "failed" else "")
                     + f"Test Case '{name}' {outcome} (2.0 seconds).\n" for number, outcome in enumerate(outcomes, 1)]
            return "".join(lines) + (f"Test Suite 'Selected tests' {'failed' if failures else 'passed'} at 2026-11-12 10:00:00.000.\n"
                                     f"\t Executed {len(outcomes)} tests, with {failures} failures ({failures} unexpected) "
                                     f"in 10.0 (10.1) seconds\n")
        self.assert_counts(iterations(["failed"] * 5), 0, 5, 0)
        self.assert_counts(iterations(["passed"] * 5), 5, 0, 0)
        self.assert_counts(iterations(["failed", "passed", "failed", "passed", "passed"]), 3, 2, 0)
        # The summary still names the test once.
        self.assertEqual(lane_report.failing(iterations(["failed"] * 5)), [(name, "/project/UITests/Largest.swift:30")])

    def test_swift_testing_failure_and_not_its_runs_own_line(self):
        self.assert_counts(SWIFT_TESTING_FAILED, 2, 1, 0)
        self.assertEqual(lane_report.failing(SWIFT_TESTING_FAILED), [("page()", "PageTests.swift:9")])

    def test_a_failing_swift_testing_test_beside_its_bundles_empty_xctest_run_is_failed(self):
        # The lines of a device's log for one filtered Swift Testing test that failed (vocaro/queuekeeper#81).
        self.assert_counts(SWIFT_TESTING_BESIDE_EMPTY_XCTEST, 0, 1, 0)

    def test_a_simulator_runs_device_lines_count_the_tests_that_failed_on_each_device(self):
        # What a sharded `qk simulator-tests` run of that test leaves in its lane's log; the lines that name the
        # failure are in each device's log alone (vocaro/queuekeeper#81).
        text = ("FAIL iPhone-17-Pro: Executed 1 tests, 1 failed in 2 shards; iPhone-17-Pro-1 failed with status 65\n"
                "FAIL iPad-Air-13-inch-M4: Executed 1 tests, 1 failed in 2 shards; iPad-Air-13-inch-M4-1 failed with status 65\n"
                "PASS iPhone-17-Pro: Executed 3 tests, 1 skipped in 40s\n")
        self.assert_counts(text, 3, 2, 0)

    def test_a_qk_unittest_gate_counts_what_its_runner_counts_whatever_its_tests_print(self):
        # A test's fake runner prints another runner's totals into its process's output (vocaro/queuekeeper#153).
        text = ("== test_device_pass.DevicePass[1]: OK (2 tests, 0.5s)\n"
                "test_one (test_device_pass.DevicePass.test_one) ... Executed 1 test, with 0 failures\nok\n"
                "test_two (test_device_pass.DevicePass.test_two) ... FAIL iPhone: Executed 5 tests, 1 skipped, 2 failed\n"
                "Executed 4 tests, with 1 failure (1 unexpected) in 0.1 (0.2) seconds\n=== 2 passed, 1 error in 0.1s ===\nok\n"
                f"\n{LINE}\nRan 2 tests in 0.670s\n\nOK\n"
                "queuekeeper unittest: 2 tests (0 skipped) in 1 processes, 8 at a time, 1.2s: OK\n")
        self.assert_counts(text, 2, 0, 0)
        # A device's line under the run's summary is the lane's own.
        self.assert_counts(text + "FAIL iPhone: Executed 5 tests, 1 skipped, 2 failed\n", 5, 2, 0)

    def test_a_total_that_says_tests_failed_and_names_none_leaves_passed_and_failed_unknown(self):
        found = lane_report.counts("Executed 4 tests, with 1 failure (1 unexpected) in 0.1 (0.2) seconds\n")
        self.assertIsNone(found["failed"])
        self.assertIn("4 ran, and the log does not say how many of them failed", lane_report.said_tests(found, 0))

    def test_an_unmeasured_test_is_counted_apart_from_the_skipped_and_from_the_passed(self):
        text = ("test_a (m.T.test_a) ... skipped 'Unmeasured: no model'\nRan 3 tests in 0.1s\n\nOK (skipped=2)\n")
        self.assert_counts(text, 1, 0, 1, 1)
        # A folded pytest skip stands for the count it gives.
        self.assert_counts("SKIPPED [2] tests/x.py:3: Unmeasured: busy\n== 1 passed, 2 skipped in 0.1s ==\n", 1, 0, 0, 2)

    def test_a_log_with_no_counts_says_so_rather_than_printing_zeros(self):
        for text in ("built the thing\n", "Ran 0 tests in 0.0s\n\nOK\n"):
            self.assertIsNone(lane_report.counts(text), text)
        self.assertEqual(lane_report.said_tests(None, 0), "no test counts in its log")
        self.assertNotIn("0 passed", lane_report.said_tests(None, 0))

    def test_records_name_a_test_once_across_their_lists_with_its_place(self):
        kept = [(Path("a"), {"failedTests": [{"test": "ReaderTests/testPage()", "failures": [{"message": "m"}]}],
                             "logged": [{"test": "ReaderTests/testPage", "file": "/R.swift", "line": 42, "message": "m"}],
                             "failingTests": ["Mod.ReaderTests/testPage()", "Other/testAlone()"]})]
        self.assertEqual(lane_report.recorded_failing(kept), [("ReaderTests/testPage()", "/R.swift:42"),
                                                               ("Other/testAlone()", None)])

    def test_nothing_that_reads_a_lanes_log_reads_a_summary_as_a_result(self):
        summary = ("qk run gate: failed (exit 1), on this Mac\n  tests: 3 passed, 2 failed, 1 skipped, 1 unmeasured\n"
                   "  failed: tests.test_x.X.test_b at /project/tests/test_x.py:7\n  details: /p/failures.txt\n")
        self.assertIsNone(lane_results.counted(summary))
        self.assertEqual(lane_results.unmeasured_tests(summary), [])
        self.assertEqual(merge_queue.FAILED_TEST.findall(summary), [])
        self.assertIsNone(merge_queue.UNITTEST_SUMMARY.search(summary))


class BeforeTests(unittest.TestCase):
    """A failed run whose log shows no test started, as when its project could not be generated (vocaro-engineering#393),
    and the logs of runs whose tests may have run, crashed or not, which stay failures."""

    def test_a_setup_that_failed_quotes_its_last_error_line(self):
        self.assertEqual(lane_report.before_tests(SETUP_FAILED),
                         "Failed: project generation or canonical package-lock seeding failed")

    def test_a_log_that_shows_a_test_started_is_not_before_tests(self):
        for started in ("Test Suite 'All tests' started at 2026-11-11 08:34:00.000\n",
                        "Test Case '-[Mod.ReaderTests testPage]' started.\n",
                        "◇ Test run started.\n", "◇ Suite \"Pages\" started.\n",
                        "Testing started\n", "test_a (m.T.test_a) ... ",
                        "============ test session starts ============\ncollected 3 items\n",
                        "Restarting after unexpected exit, crash, or test timeout\n",
                        "Segmentation fault: 11\n", "Fatal Python error: Aborted\n", "xctest crashed\n"):
            self.assertIsNone(lane_report.before_tests(started + "error: it stopped\n"), started)

    def test_a_log_that_counts_names_or_leaves_tests_unmeasured_is_not_before_tests(self):
        for text in (UNITTEST_FAILED, PYTEST_FAILED, XCTEST_FAILED, SWIFT_TESTING_FAILED,
                     "Ran 0 tests in 0.0s\n\nOK\nerror: nothing\n",
                     "/p/T.swift:3: -[M.T test] : Test skipped - Unmeasured: busy\nerror: then\n"):
            self.assertIsNone(lane_report.before_tests(text), text)

    def test_a_commands_own_last_output_is_its_last_lines_or_an_unmeasured_runs_own_reasons(self):
        log = "building\n\nUnmeasured: check A voided itself\n  indented \nqueuekeeper: its own note\nthe end\n"
        self.assertEqual(lane_report.own_output(log), ["building", "Unmeasured: check A voided itself", "  indented", "the end"])
        self.assertEqual(lane_report.own_output(log, unmeasured=True), ["Unmeasured: check A voided itself"])
        self.assertEqual(lane_report.own_output("no reason given\n", unmeasured=True), ["no reason given"])
        self.assertEqual(lane_report.own_output("\n \nqueuekeeper: only its own\n"), [])
        self.assertEqual(lane_report.own_output("".join(f"{number}\n" for number in range(20))),
                         [str(number) for number in range(20 - lane_report.OUTPUT_SHOWN, 20)])

    def test_a_log_that_says_nothing_failed_quotes_nothing(self):
        for text in ("", "the build broke\n", "queuekeeper: the build failed: compileFirst exited 65, and no test ran\n"):
            self.assertIsNone(lane_report.before_tests(text), text)


    def test_a_summary_from_before_the_rename_reads_with_its_names_places_and_a_why_over_lines(self):
        # EPUBLib's gate log of 2026-11-11 (vocaro/queuekeeper#83), with a why of two lines and a summary of SHOWN.
        log = ("Test run with 4737 tests failed\n"
               "eng run gate: failed (exit 1), on this Mac\n"
               "  tests: 4705 passed, 2 failed, 30 skipped, 0 unmeasured\n"
               "  why: one line\nand its second\n"
               '  failed: "testCellsTheReaderGoesToHaveTheirHighlightAndSelection" at TableScrolledCellTests.swift:298\n'
               '  failed: "testHighlightsOnCellRangesReachOnlyTheirCells" at TableCoverageInteractionTests.swift:749\n'
               "  failed: Pages/testTurn()\n"
               "  ... and 3 more\n"
               "  details: /p/failures.txt\n"
               "  failed: after.the.summary at /later.swift:1\n")
        self.assertEqual(lane_report.summary(log, "gate"), {
            "verdict": "failed (exit 1), on this Mac", "tests": "4705 passed, 2 failed, 30 skipped, 0 unmeasured",
            "failed": 2, "more": 3,
            "names": [('"testCellsTheReaderGoesToHaveTheirHighlightAndSelection"', "TableScrolledCellTests.swift:298"),
                      ('"testHighlightsOnCellRangesReachOnlyTheirCells"', "TableCoverageInteractionTests.swift:749"),
                      ("Pages/testTurn()", None)]})
        # A log cut off before the summary's last line counts only its first indented lines.
        cut = "qk run gate: failed (exit 1), on this Mac\n  tests: 1 passed, 1 failed\n  failed: a at A.swift:1\nother\n  failed: b"
        self.assertEqual(lane_report.summary(cut, "gate")["names"], [("a", "A.swift:1")])


class Summary(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.lane_dir = self.root / ".build/queuekeeper/lane"
        # The Mac's load, read as fixed figures, so that a summary's load line reads the same however busy it is.
        reading = patch.object(lane_report.RunLoad, "read", autospec=True,
                               side_effect=lambda sampler, last=False: sampler.readings.append([12.5, 40.0, 75.0]))
        reading.start()
        self.addCleanup(reading.stop)
        # And only at the run's end, however long a loaded Mac makes it.
        sparse = patch.object(lane_report, "LOAD_SAMPLE", 24 * 60 * 60)
        sparse.start()
        self.addCleanup(sparse.stop)

    def configure(self, **lanes):
        (self.root / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Example", "lanes": lanes}))

    def command(self, body):
        return [sys.executable, "-c", textwrap.dedent(body)]

    def printing(self, text, status=0):
        return {"command": self.command(f"import sys\nprint({text!r})\nsys.exit({status})")}

    def qk(self, *arguments):
        """qk run in this process, on a stream that joins stdout and stderr as a pipe to one file does."""
        stream = StringIO()
        with patch.object(cli, "refresh_policy", return_value="policy"), patch.object(cli, "source_identity", return_value={}), \
                contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
            status = cli.main(["--project", str(self.root), "run", "--here", *arguments])
        return status, stream.getvalue().splitlines()

    def test_a_failing_lane_ends_with_its_verdict_counts_failing_tests_and_receipt(self):
        self.configure(lane=self.printing(UNITTEST_FAILED, 1))
        status, lines = self.qk("lane")
        self.assertEqual(status, 1)
        self.assertEqual(lines[-6:], ["qk run lane: failed (exit 1), on this Mac",
                                      "  tests: 3 passed, 2 failed, 1 skipped, 0 unmeasured", LOAD,
                                      "  failed: tests.test_x.X.test_b at /project/tests/test_x.py:7",
                                      "  failed: tests.test_x.X.test_c at /project/tests/test_x.py:20",
                                      f"  receipt: {self.lane_dir / 'source.json'}"])

    def test_a_passing_lane_is_passed_and_names_no_failure(self):
        self.configure(lane=self.printing("Ran 2 tests in 0.1s\n\nOK"))
        status, lines = self.qk("lane")
        self.assertEqual(status, 0)
        self.assertEqual(lines[-4:], ["qk run lane: passed, on this Mac", "  tests: 2 passed, 0 failed, 0 skipped, 0 unmeasured",
                                      LOAD, f"  receipt: {self.lane_dir / 'source.json'}"])

    def test_a_lane_with_no_test_counts_says_so(self):
        self.configure(lane=self.printing("just a build"))
        self.assertEqual(self.qk("lane")[1][-4:-2], ["qk run lane: passed, on this Mac", "  tests: no test counts in its log"])

    def test_an_unmeasured_lane_is_never_passed(self):
        text = "test_a (m.T.test_a) ... skipped 'Unmeasured: no model'\nRan 2 tests in 0.1s\n\nOK (skipped=1)"
        self.configure(lane=self.printing(text))
        status, lines = self.qk("lane")
        self.assertEqual(status, lane_results.UNMEASURED)
        self.assertEqual(lines[-4:-1], ["qk run lane: unmeasured (exit 3), on this Mac",
                                        "  tests: 1 passed, 0 failed, 0 skipped, 1 unmeasured", LOAD])

    def test_a_lane_past_its_limit_is_unmeasured_and_counts_nothing_from_its_cut_off_log(self):
        self.configure(lane={"command": self.command("print('Ran 2 tests in 0.1s\\n\\nOK', flush=True)\nimport time\ntime.sleep(60)"),
                             "timeout": 1})
        status, lines = self.qk("lane")
        self.assertEqual(status, lane_results.UNMEASURED)
        self.assertEqual(lines[-5:-2], ["qk run lane: unmeasured (exit 3), on this Mac",
                                        "  tests: no counts, as the run did not finish and its log is incomplete", LOAD])
        self.assertIn("  why: ", lines[-2])

    def test_a_qk_unittest_lane_past_its_limit_names_what_it_did_not_finish_and_how_to_run_it_alone(self):
        """A gate stopped at its timeout gave its counts and a log that stopped mid-run, which read as a crash, and
        named no test it had not reached (vocaro/queuekeeper#37)."""
        suite = self.root / "suite"
        suite.mkdir()
        (suite / "test_a.py").write_text("import unittest\nclass Quick(unittest.TestCase):\n    def test_quick(self): pass\n")
        launcher = [sys.executable, str(SOURCE.parents[1] / "scripts/qk")]
        # Room for discovery and the quick module on a loaded Mac; the hung test sleeps far past it, by a sleep that
        # follows the limit: a one-minute load read at 60 or more per core stretched the limit past a fixed 600 s,
        # and the hung test finished and passed before the timeout came (vocaro/queuekeeper#202).
        limit = round(stretched(10))
        (suite / "test_b.py").write_text("import time, unittest\nclass Hung(unittest.TestCase):\n"
                                         f"    def test_hung(self): time.sleep({10 * limit + 600})\n")
        self.configure(lane={"command": [*launcher, "unittest", "-s", str(suite), "-j", "1"], "timeout": limit})
        status, lines = self.qk("lane")
        self.assertEqual(status, lane_results.UNMEASURED, lines)
        log = (self.lane_dir / "run.log").read_text()
        # The log says why it stops where it stops, and qk unittest still gave its summary.
        self.assertIn(f"supervise: timed out after {limit}s; sending SIGTERM to the command", log)
        self.assertIn("UNMEASURED: 1 of 2 processes not run or not finished (stopped by SIGTERM)", log)
        listing = self.lane_dir / "results/unittest/unreached.txt"
        self.assertIn("    test_b.Hung.test_hung", listing.read_text())
        end = lines.index("qk run lane: unmeasured (exit 3), on this Mac")
        self.assertEqual(lines[end + 1:], [
            "  tests: no counts, as the run did not finish and its log is incomplete", LOAD,
            f"  why: Owned command timed out after {limit}s: {sys.executable}; it and its observed descendants ended",
            f"  unreached: 1 tests did not finish (stopped by SIGTERM); the list is in {listing}",
            f"  run them: {shlex.join([*launcher, 'run', 'lane', '--', 'test_b'])}",
            f"  receipt: {self.lane_dir / 'source.json'}"])

    def test_a_dispatched_run_that_timed_out_says_so_and_names_what_it_did_not_finish(self):
        """Another Mac's supervise line went to the dispatch's output.log alone, and the summary here gave counts
        that read as a whole run's (vocaro/queuekeeper#37)."""
        runs = self.root / ".build/queuekeeper/remote" / lane_report.remote.DISPATCHES / "gate"
        ran = runs / f"20270115T080000Z-{os.getpid()}"
        (ran / "results/unittest").mkdir(parents=True)
        (ran / "run.log").write_text("== test_a.A: OK (1 tests, 0.1s)\nRan 1 test in 0.1s\n\nOK\n")
        (ran / "results/unittest/unreached.json").write_text(json.dumps(
            {"result": "stopped by SIGTERM", "tests": ["test_b.B.test_one", "test_c.C.test_two"], "names": ["test_b", "test_c.C"],
             "command": ["python3", "scripts/qk", "unittest", "-s", "tests", "test_b", "test_c.C"]}))
        timed_out = "Owned command timed out after 2400s: python3; it and its observed descendants ended"
        command = ["python3", "scripts/qk", "unittest", "-s", "tests", "--split", "class"]
        def receipt(arguments):
            (ran / "source.json").write_text(json.dumps({"host": "build-host", "arguments": arguments, "result": {
                "exitStatus": 3, "receipt": {"outcome": "unmeasured", "error": timed_out, "command": [*command, *arguments],
                                             "arguments": arguments}}}))
        report = lane_report.Report()
        report.root, report.lane, report.began = self.root, "gate", 1_800_000_000  # 2027-01-15T08:00:00Z
        receipt([])
        lines = report.lines(lane_results.UNMEASURED, None)
        self.assertEqual(lines[0], "qk run gate: unmeasured (exit 3), on build-host")
        self.assertIn(f"  why: {timed_out}", lines)
        listing = ran / "results/unittest/unreached.txt"
        self.assertEqual(lines[-3:-1], [f"  unreached: 2 tests did not finish (stopped by SIGTERM); the list is in {listing}",
                                        "  run them: python3 scripts/qk run gate -- test_b test_c.C"])
        # A run that had arguments of its own cannot be rerun as the lane by names alone: qk unittest's command stands.
        receipt(["-k", "slow"])
        self.assertEqual(report.lines(lane_results.UNMEASURED, None)[-2],
                         "  run them: python3 scripts/qk unittest -s tests test_b test_c.C")
        # The control: a dispatch that ended otherwise has no why of its own, and a passed one names nothing unreached.
        (ran / "source.json").write_text(json.dumps({"host": "build-host", "result": {"exitStatus": 3, "receipt": {}}}))
        self.assertFalse(any(line.startswith("  why: ") for line in report.lines(lane_results.UNMEASURED, None)))
        self.assertFalse(any(line.startswith("  unreached: ") for line in report.lines(0, None)))

    def test_a_failure_record_gives_the_failing_tests_their_places_and_the_path(self):
        script = textwrap.dedent(f"""\
            import json, os, pathlib
            directory = pathlib.Path(os.environ['QUEUEKEEPER_RESULTS_DIR']) / 'failures/one'
            directory.mkdir(parents=True)
            (directory / 'failures.txt').write_text('what failed')
            (directory / 'failures.json').write_text(json.dumps({{'counts': {{'passed': 7, 'failed': 1, 'skipped': 2}},
                'failedTests': [{{'test': 'Pages/testTurn()', 'failures': [{{'file': '/P.swift', 'line': 9, 'message': 'm'}}]}}]}}))
            raise SystemExit(65)
            """)
        self.configure(lane={"command": [sys.executable, "-c", script]})
        status, lines = self.qk("lane")
        self.assertEqual(status, 65)
        self.assertEqual(lines[-5:], ["qk run lane: failed (exit 65), on this Mac",
                                      "  tests: 7 passed, 1 failed, 2 skipped, 0 unmeasured", LOAD,
                                      "  failed: Pages/testTurn() at /P.swift:9",
                                      f"  details: {self.lane_dir / 'results/failures/one/failures.txt'}"])

    def test_an_earlier_runs_record_is_not_this_runs(self):
        self.configure(lane=self.printing("Ran 1 test in 0.1s\n\nOK"))
        record(self.lane_dir / "results", "old", failedTests=[{"test": "Old/testOld()", "failures": []}])
        lines = self.qk("lane")[1]
        self.assertFalse([line for line in lines if "Old" in line or "details:" in line], lines)

    def test_a_failed_lane_that_names_no_test_says_where_its_log_is(self):
        self.configure(lane=self.printing("the build broke", 65))
        lines = self.qk("lane")[1]
        self.assertEqual(lines[-4:], [f"  no failing test is named; the log is {self.lane_dir / 'run.log'}",
                                      "  the last of its output:", "    the build broke",
                                      f"  receipt: {self.lane_dir / 'source.json'}"])

    def test_a_lane_sent_to_the_farm_mac_whose_command_refused_its_arguments_says_what_the_command_said(self):
        """BreezEPUB's ways-in on 2026-11-13: the dispatch's run.log held the lane script's one line, and the summary
        said only that the log had no test counts and named no failing test (vocaro/queuekeeper#137)."""
        runs = self.root / ".build/queuekeeper/remote" / lane_report.remote.DISPATCHES / "ways-in"
        ran = runs / f"20270115T080000Z-{os.getpid()}"
        ran.mkdir(parents=True)
        refusal = "ways-in-native selects its own tests for each run and takes no arguments"
        (ran / "run.log").write_text(refusal + "\n")
        (ran / "source.json").write_text(json.dumps({"host": "build-host", "result": {"exitStatus": 2, "receipt": {
            "outcome": "failed", "load": {"readings": [[12.5, 40.0, 75.0]], "every": 60}}}}))
        report = lane_report.Report()
        report.root, report.lane, report.began = self.root, "ways-in", 1_800_000_000  # 2027-01-15T08:00:00Z
        lines = report.lines(2, None)
        self.assertEqual(lines, ["qk run ways-in: failed (exit 2), on build-host", "  tests: no test counts in its log", LOAD,
                                 f"  no failing test is named; the log is {ran / 'run.log'}",
                                 "  the last of its output:", f"    {refusal}",
                                 f"  receipt: {ran / 'source.json'}"])
        # The summary reads back whole, and nothing that reads a lane's log again reads the quoted lines as a result.
        said = lane_report.summary("\n".join(lines), "ways-in")
        self.assertEqual((said["tests"], said["failed"], said["names"]), ("no test counts in its log", None, []))
        # A longer log gives its last OUTPUT_SHOWN lines, none blank and none queuekeeper's own, each cut at QUOTED.
        (ran / "run.log").write_text("".join(f"step {number}\n\n" for number in range(9)) + "x" * 400
                                     + "\nqueuekeeper: a note of its own\n")
        lines = report.lines(2, None)
        at = lines.index(lane_report.OUTPUT_HEAD)
        self.assertEqual(lines[at + 1:-1], [*(f"    step {number}" for number in range(5, 9)), "    " + "x" * lane_report.QUOTED])
        # The controls: a passed run, a log that counts or names a test or is empty, and a run queuekeeper explains
        # quote nothing.
        self.assertNotIn(lane_report.OUTPUT_HEAD, report.lines(0, None))
        for log in (UNITTEST_FAILED, "Ran 2 tests in 0.1s\n\nOK\nthen it refused\n",
                    "/p/T.swift:3: -[M.T test] : Test skipped - Unmeasured: busy\nthen it refused\n", ""):
            (ran / "run.log").write_text(log)
            self.assertNotIn(lane_report.OUTPUT_HEAD, report.lines(2, None), log)
        (ran / "run.log").write_text(refusal + "\n")
        report.explain("Expected at least 2 executed tests; observed 0")
        self.assertNotIn(lane_report.OUTPUT_HEAD, report.lines(2, None))

    def test_an_unmeasured_run_that_names_no_test_quotes_the_projects_own_reason_or_its_last_lines(self):
        reason = "Unmeasured: the timing check voided its own measurement; rerun on an idle Mac"
        self.configure(lane=self.printing(f"building\n{reason}\nexit 3", lane_results.UNMEASURED))
        status, lines = self.qk("lane")
        self.assertEqual(status, lane_results.UNMEASURED)
        self.assertEqual(lines[-6:], ["qk run lane: unmeasured (exit 3), on this Mac", "  tests: no test counts in its log", LOAD,
                                      "  the last of its output:", f"    {reason}",
                                      f"  receipt: {self.lane_dir / 'source.json'}"])
        self.configure(lane=self.printing("the Mac is too busy to say", lane_results.UNMEASURED))
        self.assertEqual(self.qk("lane")[1][-3:-1], ["  the last of its output:", "    the Mac is too busy to say"])

    def test_a_lane_that_failed_before_any_test_is_told_apart_from_a_test_failure(self):
        self.configure(lane=self.printing(SETUP_FAILED, 1))
        status, lines = self.qk("lane")
        self.assertEqual(status, lane_results.BEFORE_TESTS)
        self.assertNotIn(status, (1, lane_results.UNMEASURED))
        self.assertEqual(lines[-5:], ["qk run lane: failed before any test ran (exit 4), on this Mac",
                                      "  tests: none ran; the last error in its log: "
                                      "Failed: project generation or canonical package-lock seeding failed", LOAD,
                                      f"  the log is {self.lane_dir / 'run.log'}",
                                      f"  receipt: {self.lane_dir / 'source.json'}"])
        receipt = json.loads((self.lane_dir / "source.json").read_text())
        self.assertEqual((receipt["outcome"], receipt["exitStatus"], receipt["beforeTests"]),
                         ("failed", 1, "Failed: project generation or canonical package-lock seeding failed"))
        # It is a failure to the queue, as it was: whether it gives no verdict is the owner's call.
        self.assertNotIn(lane_results.BEFORE_TESTS, merge_queue.NO_VERDICT)
        summary = "\n".join(lines[-5:])
        self.assertIsNone(lane_results.counted(summary))
        self.assertEqual(merge_queue.FAILED_TEST.findall(summary), [])

    def test_a_lane_that_could_not_start_says_so_and_not_that_it_failed(self):
        # vocaro/queuekeeper#108: python3 with no file to run exits 2, as a failure does.
        self.configure(lane={"command": ["python3", "scripts/qk", "spelling"]})
        status, lines = self.qk("lane")
        self.assertEqual(status, lane_results.NOT_STARTED)
        self.assertEqual(lines[-3:], ["qk run lane: could not start (exit 127), on this Mac",
                                      f"  tests: none ran, as its command never started: python3 has no scripts/qk to "
                                      f"run in {self.root}",
                                      f"  receipt: {self.lane_dir / 'source.json'}"])
        receipt = json.loads((self.lane_dir / "source.json").read_text())
        self.assertEqual((receipt["outcome"], receipt["exitStatus"]), ("not-started", lane_results.NOT_STARTED))
        self.assertNotIn(lane_results.NOT_STARTED, merge_queue.NO_VERDICT)

    def test_a_lane_whose_tests_crashed_before_their_counts_stays_failed(self):
        crashed = ("Test Suite 'All tests' started at 2026-11-11 08:34:00.000\n"
                   "Test Case '-[Mod.ReaderTests testPage]' started.\nFatal error: index out of range\n")
        for text, status in ((crashed, 1), ("test_a (m.T.test_a) ... Fatal Python error: Segmentation fault\n", 139)):
            with self.subTest(status=status):
                self.configure(lane=self.printing(text, status))
                result, lines = self.qk("lane")
                self.assertEqual(result, status)
                at = lines.index(f"qk run lane: failed (exit {status}), on this Mac")
                self.assertEqual(lines[at + 1:at + 3], ["  tests: no test counts in its log", LOAD])
                self.assertNotIn("beforeTests", json.loads((self.lane_dir / "source.json").read_text()))

    def test_a_lane_that_exits_unmeasured_or_a_failed_build_keeps_its_status(self):
        self.configure(lane=self.printing(SETUP_FAILED, lane_results.UNMEASURED))
        self.assertEqual(self.qk("lane")[0], lane_results.UNMEASURED)
        self.configure(lane={**self.printing("Ran 1 test in 0.1s\n\nOK"),
                             "compileFirst": self.printing("error: cannot find 'x' in scope", 65)["command"]})
        status, lines = self.qk("lane")
        self.assertEqual(status, 65)
        self.assertIn("qk run lane: failed (exit 65), on this Mac", lines)

    def test_a_lane_with_a_failure_record_is_a_test_failure_whatever_its_log_says(self):
        script = textwrap.dedent("""\
            import json, os, pathlib
            directory = pathlib.Path(os.environ['QUEUEKEEPER_RESULTS_DIR']) / 'failures/one'
            directory.mkdir(parents=True)
            (directory / 'failures.json').write_text(json.dumps({'failingTests': ['Pages/testTurn()']}))
            print('error: the test bundle stopped')
            raise SystemExit(65)
            """)
        self.configure(lane={"command": [sys.executable, "-c", script]})
        status, lines = self.qk("lane")
        self.assertEqual(status, 65)
        self.assertIn("  failed: Pages/testTurn()", lines)

    def test_a_command_that_exits_4_itself_after_its_tests_is_only_failed(self):
        self.configure(lane=self.printing(UNITTEST_FAILED, 4))
        status, lines = self.qk("lane")
        self.assertEqual(status, 4)
        self.assertIn("qk run lane: failed (exit 4), on this Mac", lines)

    def test_a_command_that_refused_its_usage_keeps_status_2(self):
        # simulator-tests refuses a lane command without its separator so, before any test (#393's gate failure).
        self.configure(lane=self.printing("error: a lane whose command runs simulator-tests ends with \"--\"\n", 2))
        status, lines = self.qk("lane")
        self.assertEqual(status, 2)
        self.assertIn("qk run lane: failed (exit 2), on this Mac", lines)
        # And its summary says what the command said (vocaro/queuekeeper#137).
        self.assertEqual(lines[-3:-1], ["  the last of its output:",
                                        "    error: a lane whose command runs simulator-tests ends with \"--\""])

    def test_a_too_short_run_says_why_in_its_summary(self):
        self.configure(lane={**self.printing("Ran 1 test in 0.1s\n\nOK"), "minimumTests": 2})
        status, lines = self.qk("lane")
        self.assertEqual(status, 2)
        self.assertEqual(lines[-6:-4], ["qk run lane: failed (exit 2), on this Mac",
                                        "  tests: 1 passed, 0 failed, 0 skipped, 0 unmeasured"])
        self.assertEqual(lines[-4:-2], [LOAD, "  why: Expected at least 2 executed tests; observed 1"])

    def test_a_group_counts_its_lanes_together_and_names_the_one_that_gave_no_counts(self):
        group = {"parallel": ["good", "bad", "build"], "jobs": 1}
        self.configure(good=self.printing("Ran 2 tests in 0.1s\n\nOK"), bad=self.printing(UNITTEST_FAILED, 1),
                       build=self.printing("no tests here"), lane=group)
        status, lines = self.qk("lane")
        self.assertEqual(status, 1)
        self.assertEqual(lines[-7:-3], ["qk run lane: failed (exit 1), on this Mac",
                                        "  tests: 5 passed, 2 failed, 1 skipped, 0 unmeasured; no test counts from build",
                                        "  lanes: 2 passed, 1 failed (bad), 0 unmeasured", LOAD])

    def test_a_summary_reads_back_what_it_wrote_and_nothing_its_tests_printed(self):
        # The lane's own output imitates a summary's lines, which are not this run's (vocaro/queuekeeper#83).
        printed = "  failed: not.this.test at /nowhere.swift:1\n" + UNITTEST_FAILED
        self.configure(lane=self.printing(printed, 1))
        lines = self.qk("lane")[1]
        said = lane_report.summary("\n".join(["queuekeeper: before", *lines, "what came after"]), "lane")
        self.assertEqual(said, {"verdict": "failed (exit 1), on this Mac",
                                "tests": "3 passed, 2 failed, 1 skipped, 0 unmeasured", "failed": 2,
                                "names": [("tests.test_x.X.test_b", "/project/tests/test_x.py:7"),
                                          ("tests.test_x.X.test_c", "/project/tests/test_x.py:20")], "more": 0})
        self.assertIsNone(lane_report.summary("\n".join(lines), "gate"))

    def test_a_summarys_counts_and_the_runs_own_summary_read_back(self):
        # vocaro/queuekeeper#193: the record of a group's gate reads the group's counts from its summary, not from the
        # qk unittest output of one of its lanes that the log also holds.
        self.configure(lane=self.printing(UNITTEST_FAILED, 1))
        lines = self.qk("lane")[1]
        text = "\n".join(lines + ["Ran 250 tests in 1.000s", "", "OK"])
        said = lane_report.run_summary(text, "20261115-160322-252a891e-lane.log")
        self.assertEqual(said, lane_report.run_summary(text))
        self.assertEqual(lane_report.summary_counts(said),
                         {"passed": 3, "failed": 2, "skipped": 1, "unmeasured": 0, "ran": 5})
        self.assertEqual(lane_report.summary_counts({"tests": "7 ran, and the log does not say how many of them "
                                                              "failed, 1 skipped, 2 unmeasured"}),
                         {"passed": None, "failed": None, "skipped": 1, "unmeasured": 2, "ran": 7})
        self.assertIsNone(lane_report.summary_counts({"tests": "no test counts in its log"}))
        self.assertIsNone(lane_report.run_summary("Ran 250 tests in 1.000s\n\nOK\n"))
        # Of two summaries, the one of the lane the log's file is named for.
        other = text + "\nqk run other: passed, on this Mac\n  tests: 1 passed, 0 failed, 0 skipped, 0 unmeasured\n"
        self.assertEqual(lane_report.run_summary(other, "round-lane.log")["failed"], 2)
        self.assertEqual(lane_report.run_summary(other)["failed"], 0)

    def test_a_summary_that_names_no_test_or_ran_none_reads_back_with_no_names(self):
        self.configure(lane=self.printing("the build broke", 65))
        said = lane_report.summary("\n".join(self.qk("lane")[1]), "lane")
        self.assertEqual((said["verdict"], said["tests"], said["failed"], said["names"]),
                         ("failed (exit 65), on this Mac", "no test counts in its log", None, []))
        self.configure()
        said = lane_report.summary("\n".join(self.qk("nothing")[1]), "nothing")
        self.assertEqual(said, {"verdict": "not run (exit 2); no tests ran", "tests": None, "failed": None, "names": [],
                                "more": 0})

    def test_a_lane_that_does_not_exist_ends_with_a_summary_that_nothing_ran(self):
        self.configure()
        status, lines = self.qk("nothing")
        self.assertEqual(status, 2)
        self.assertEqual(lines[-2:], ["queuekeeper: No configured lane: nothing", "qk run nothing: not run (exit 2); no tests ran"])

    def test_a_policy_that_cannot_be_fetched_is_unmeasured_and_never_passed(self):
        self.configure(lane=self.printing("Ran 1 test in 0.1s\n\nOK"))
        stream = StringIO()
        with patch.object(cli, "refresh_policy", side_effect=cli.extensions.Unavailable("no policy")), \
                patch.object(cli, "source_identity", return_value={}), \
                contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
            status = cli.main(["--project", str(self.root), "run", "--here", "lane"])
        self.assertEqual(status, lane_results.UNMEASURED)
        self.assertEqual(stream.getvalue().splitlines()[-1], "qk run lane: unmeasured (exit 3); no tests ran")

    def test_the_farm_macs_own_run_leaves_the_summary_to_the_mac_that_sent_it(self):
        self.configure(lane=self.printing("Ran 1 test in 0.1s\n\nOK"))
        with patch.dict("os.environ", {lane_report.remote.DISPATCHER: "this Mac"}):
            lines = self.qk("lane")[1]
        self.assertFalse([line for line in lines if line.startswith("qk run lane:")], lines)

    def test_only_a_lane_run_gets_a_summary(self):
        self.configure(lane=self.printing(""))
        stream = StringIO()
        with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
            self.assertIn(cli.main(["--project", str(self.root), "docs", "check"]), (0, 1))
        self.assertNotIn("qk run", stream.getvalue())

    def test_a_dispatched_run_reports_its_own_dispatch_and_not_one_sent_beside_it(self):
        """Two runs of one lane from one checkout side by side: the one that started second and ended first filed the
        newer dispatch, which the first run's summary printed as its own (vocaro/queuekeeper#9)."""
        runs = self.root / ".build/queuekeeper/remote" / lane_report.remote.DISPATCHES / "lane"
        def dispatched(name, log, receipt):
            (runs / name).mkdir(parents=True)
            (runs / name / "run.log").write_text(log)
            (runs / name / "source.json").write_text(json.dumps(receipt))
        own = os.getpid()
        dispatched(f"20270115T080000Z-{own}", UNITTEST_FAILED, {"host": "fake-mac"})
        dispatched(f"20270115T080011Z-{own + 1}", "Ran 2 tests in 0.1s\n\nOK", {"host": "fake-mac"})
        report = lane_report.Report()
        report.root, report.lane, report.began = self.root, "lane", 1_800_000_000  # 2027-01-15T08:00:00Z
        lines = report.lines(1, None)
        self.assertEqual(lines[:2], ["qk run lane: failed (exit 1), on fake-mac",
                                     "  tests: 3 passed, 2 failed, 1 skipped, 0 unmeasured"])
        self.assertEqual(lines[-1], f"  receipt: {runs / f'20270115T080000Z-{own}' / 'source.json'}")
        # A dispatch this process sent again in the same second, which file_results names NAME.2, is still its own.
        dispatched(f"20270115T080000Z-{own}.2", "Ran 1 test in 0.1s\n\nOK", {"host": "other-mac"})
        self.assertEqual(report.source(), ("on other-mac", runs / f"20270115T080000Z-{own}.2"))

    def test_two_runs_of_one_lane_here_side_by_side_each_keep_and_report_their_own(self):
        """Two runs of one lane on this Mac from one checkout shared .build/queuekeeper/LANE: the second emptied the
        first's results and wrote over its log, so the first counted the second's tests with its own
        (vocaro/queuekeeper#9). The second now keeps a directory of its own, which its summary names."""
        body = f"""
            import os, pathlib, sys, time
            here, results = pathlib.Path({str(self.root)!r}), pathlib.Path(os.environ['QUEUEKEEPER_RESULTS_DIR'])
            results.mkdir(parents=True, exist_ok=True)
            (results / (sys.argv[1] + '.txt')).write_text(sys.argv[1])
            if sys.argv[1] == 'second':
                print('Ran 2 tests in 0.1s\\n\\nOK')
                sys.exit(0)
            print('the first run began ' * 8, flush=True)
            (here / 'began').touch()
            end = time.monotonic() + {stretched(300)}
            while not (here / 'ended').exists() and time.monotonic() < end: time.sleep(0.05)
            print({UNITTEST_FAILED!r})
            sys.exit(1)
            """
        # A self lane takes no admission, so the two run side by side however many slots this Mac guarantees.
        self.configure(lane={"command": self.command(body), "resource": "self"})
        def run(name):
            return subprocess.Popen([sys.executable, "-c", "import sys\nfrom queuekeeper import cli\nsys.exit(cli.main(sys.argv[1:]))",
                                     "--project", str(self.root), "run", "--here", "lane", "--", name],
                                    env={**os.environ, "PYTHONPATH": str(SOURCE)},
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        first = run("first")
        self.addCleanup(first.kill)
        self.assertTrue(wait_until(lambda: (self.root / "began").exists() or first.poll() is not None, stall=120))
        second = run("second")
        self.addCleanup(second.kill)
        beside = second.communicate(timeout=stretched(300))[0].splitlines()
        (self.root / "ended").touch()
        lines = first.communicate(timeout=stretched(300))[0].splitlines()
        self.assertEqual((first.returncode, second.returncode), (1, 0), "\n".join([*lines, *beside]))
        [own] = (self.root / ".build/queuekeeper" / cli.RUNS / "lane").glob("*-*")
        self.assertEqual(own.name.rsplit("-", 1)[1], str(second.pid))
        self.assertIn(f"queuekeeper: another run of lane from this checkout is running on this Mac, so this run keeps "
                      f"its log, receipt and results in {own}, not {self.lane_dir}", beside)
        # Each run's summary counts its own tests and names its own receipt.
        self.assertIn("qk run lane: passed, on this Mac", beside)
        self.assertIn("  tests: 2 passed, 0 failed, 0 skipped, 0 unmeasured", beside)
        self.assertEqual(beside[-1], f"  receipt: {own / 'source.json'}")
        self.assertIn("qk run lane: failed (exit 1), on this Mac", lines)
        self.assertIn("  tests: 3 passed, 2 failed, 1 skipped, 0 unmeasured", lines)
        self.assertEqual(lines[-1], f"  receipt: {self.lane_dir / 'source.json'}")
        # And each directory holds its own run's log, receipt and results, whole.
        for directory, name, outcome in ((self.lane_dir, "first", "failed"), (own, "second", "passed")):
            with self.subTest(run=name):
                self.assertEqual(json.loads((directory / "source.json").read_text())["outcome"], outcome)
                self.assertEqual([path.name for path in (directory / "results").iterdir()], [f"{name}.txt"])
                self.assertEqual("the first run began" in (directory / "run.log").read_text(), name == "first")
                self.assertEqual("Ran 2 tests" in (directory / "run.log").read_text(), name == "second")
        # The lane's directory is the next run's again once the run that held it has ended.
        self.configure(lane=self.printing("Ran 1 test in 0.1s\n\nOK"))
        self.assertEqual(self.qk("lane")[1][-1], f"  receipt: {self.lane_dir / 'source.json'}")

    def test_a_lane_keeps_its_newest_runs_beside_another_and_any_still_running(self):
        runs = self.root / ".build/queuekeeper" / cli.RUNS / "lane"
        old = [runs / f"20200115T08000{number}Z-1" for number in range(cli.RUNS_KEPT + 1)]
        running = runs / f"20200115T070000Z-{os.getpid()}"
        for path in (*old, running): path.mkdir(parents=True)
        stream = StringIO()
        with contextlib.ExitStack() as first, contextlib.ExitStack() as second, contextlib.redirect_stderr(stream):
            self.assertEqual(cli.lane_directory(first, self.root, "lane"), self.lane_dir)
            own = cli.lane_directory(second, self.root, "lane")
            self.assertEqual((own.parent, own.name.rpartition("-")[2]), (runs, str(os.getpid())))
        # The newest eight, its own among them, and the older one whose process, this one, still runs.
        self.assertEqual(sorted(path for path in runs.iterdir() if path.is_dir()), sorted([running, *old[2:], own]))
        self.assertEqual(stream.getvalue().splitlines()[1:], [
            f"queuekeeper: removed {path}, each lane keeping its newest {cli.RUNS_KEPT} runs beside another" for path in old[:2]])
        # Released with the run that held it, the lane's directory is the next run's.
        with contextlib.ExitStack() as later: self.assertEqual(cli.lane_directory(later, self.root, "lane"), self.lane_dir)

    def test_a_summary_that_cannot_be_made_never_changes_the_status(self):
        self.configure(lane=self.printing(""))
        with patch.object(lane_report.Report, "lines", side_effect=OSError("disk gone")):
            status, lines = self.qk("lane")
        self.assertEqual(status, 0)
        self.assertEqual(lines[-1], "qk run lane: exit 0; its summary could not be made: OSError: disk gone")


class RunLoads(unittest.TestCase):
    """The load of the Mac a lane ran on, in its receipt and its summary (vocaro/queuekeeper#133): a lane that failed on
    another Mac on 2026-11-13 left it to be read by hand with `ssh build-host uptime`."""
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()

    def test_a_run_shorter_than_a_busy_reading_is_read_over_the_whole_run_without_waiting(self):
        ticks = iter([(100, 0, 100, 0), (160, 0, 140, 0)])
        with patch.object(lane_report.resources, "cpu_ticks", side_effect=lambda: next(ticks)), \
                patch.object(lane_report.resources, "cpu_busy", return_value=(None, 1.0)) as busy, \
                patch.object(lane_report.resources, "one_minute_load", return_value=21.72), \
                patch.object(lane_report.resources, "busy_limit", return_value=90.0):
            load = lane_report.RunLoad().start().finish()
        self.assertTrue(all(not call.kwargs and len(call.args) == 1 for call in busy.call_args_list), busy.call_args_list)
        # 60 of the 100 ticks between the run's start and its end were outside idle.
        self.assertEqual(load, {"readings": [[21.72, 60.0, 90.0]], "every": lane_report.LOAD_SAMPLE,
                                "oneMinuteHighest": 21.72, "oneMinuteLast": 21.72, "cpuBusyHighest": 60.0,
                                "busyLimit": 90.0})

    def test_what_the_mac_could_not_give_is_said_so_and_nothing_raised_reaches_the_lane(self):
        with patch.object(lane_report.resources, "cpu_ticks", side_effect=OSError("no ticks")), \
                patch.object(lane_report.resources, "cpu_busy", side_effect=OSError("no samples")), \
                patch.object(lane_report.resources, "busy_limit", side_effect=ValueError("no limit")), \
                patch.object(lane_report.resources, "one_minute_load", return_value=None):
            load = lane_report.RunLoad().start().finish()
        self.assertEqual(load, {"readings": [[None, None, None]], "every": lane_report.LOAD_SAMPLE})
        self.assertEqual(lane_report.load_line({"load": load}, "failed"),
                         "  load: the one-minute load could not be read, and the CPU busy share could not be read")

    def test_the_summary_of_a_lane_sent_to_the_farm_mac_gives_that_macs_load_from_its_receipt(self):
        runs = self.root / ".build/queuekeeper/remote" / lane_report.remote.DISPATCHES / "voiceover"
        ran = runs / f"20270115T080000Z-{os.getpid()}"
        ran.mkdir(parents=True)
        (ran / "run.log").write_text(UNITTEST_FAILED)
        def receipt(**fields):
            (ran / "source.json").write_text(json.dumps({"host": "build-host", "result": {
                "exitStatus": 1, "receipt": {"outcome": "failed", "ranOn": "second Mac", "mac": "build-host", **fields}}}))
        report = lane_report.Report()
        report.root, report.lane, report.began = self.root, "voiceover", 1_800_000_000  # 2027-01-15T08:00:00Z
        receipt(load={"readings": [[605.76, 100.0, 90.0], None, [21.72, 64.5, 90.0]], "every": 60})
        lines = report.lines(1, None)
        self.assertEqual(lines[:3], ["qk run voiceover: failed (exit 1), on build-host",
                                     "  tests: 3 passed, 2 failed, 1 skipped, 0 unmeasured",
                                     "  load: the one-minute load was highest 605.76, last 21.72 over 2 of 3 readings, "
                                     "and the CPU was at most 100% busy"])
        self.assertEqual(lane_report.summary("\n".join(lines), "voiceover")["failed"], 2)
        # The same run unmeasured says it too; a receipt from before the load was kept says that it keeps none, and a
        # passed run with none says nothing of it.
        self.assertIn(lines[2], report.lines(lane_results.UNMEASURED, None))
        receipt()
        self.assertEqual(report.lines(1, None)[2], "  load: its receipt keeps no reading of the load of the Mac it ran on")
        self.assertFalse(any(line.startswith("  load: ") for line in report.lines(0, None)))
        # What is not a figure is not read as one.
        receipt(load={"readings": [["high", True, 90.0], "nonsense"]})
        self.assertEqual(report.lines(1, None)[2], "  load: the one-minute load could not be read, and the CPU busy "
                                                   "share could not be read")

    def test_a_failed_lanes_receipt_keeps_the_load_of_this_mac_over_its_run(self):
        (self.root / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Example", "lanes": {
            "lane": {"command": [sys.executable, "-c", "import sys; print('no'); sys.exit(1)"]}}}))
        stream = StringIO()
        with patch.object(cli, "refresh_policy", return_value="policy"), \
                patch.object(cli, "source_identity", return_value={}), \
                patch.object(lane_report.resources, "one_minute_load", return_value=7.25), \
                contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
            status = cli.main(["--project", str(self.root), "run", "--here", "lane"])
        self.assertEqual(status, 1)
        receipt = json.loads((self.root / ".build/queuekeeper/lane/source.json").read_text())
        self.assertEqual((receipt["outcome"], receipt["load"]["oneMinuteHighest"], receipt["load"]["oneMinuteLast"],
                          len(receipt["load"]["readings"])), ("failed", 7.25, 7.25, 1))
        self.assertIn(lane_report.load_line(receipt, "failed"), stream.getvalue().splitlines())


if __name__ == "__main__":
    unittest.main()
