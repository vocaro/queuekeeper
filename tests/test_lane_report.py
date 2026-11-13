"""The summary that ends every `qk run` (vocaro-engineering#274): what it reads from a run's own log and records,
that passed, failed, skipped and unmeasured stay apart, and that it is the last thing the run says."""
import contextlib
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/python"))
import host_isolation  # noqa: F401 (keeps the tests off the real hosts)
from host_isolation import stretched
from queuekeeper import cli, lane_results, lane_report, merge_queue

SOURCE = Path(__file__).resolve().parents[1] / "src/python"
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

    def test_a_log_that_says_nothing_failed_quotes_nothing(self):
        for text in ("", "the build broke\n", "queuekeeper: the build failed: compileFirst exited 65, and no test ran\n"):
            self.assertIsNone(lane_report.before_tests(text), text)


class Summary(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.lane_dir = self.root / ".build/queuekeeper/lane"

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
        self.assertEqual(lines[-5:], ["qk run lane: failed (exit 1), on this Mac",
                                      "  tests: 3 passed, 2 failed, 1 skipped, 0 unmeasured",
                                      "  failed: tests.test_x.X.test_b at /project/tests/test_x.py:7",
                                      "  failed: tests.test_x.X.test_c at /project/tests/test_x.py:20",
                                      f"  receipt: {self.lane_dir / 'source.json'}"])

    def test_a_passing_lane_is_passed_and_names_no_failure(self):
        self.configure(lane=self.printing("Ran 2 tests in 0.1s\n\nOK"))
        status, lines = self.qk("lane")
        self.assertEqual(status, 0)
        self.assertEqual(lines[-3:], ["qk run lane: passed, on this Mac", "  tests: 2 passed, 0 failed, 0 skipped, 0 unmeasured",
                                      f"  receipt: {self.lane_dir / 'source.json'}"])

    def test_a_lane_with_no_test_counts_says_so(self):
        self.configure(lane=self.printing("just a build"))
        self.assertEqual(self.qk("lane")[1][-3:-1], ["qk run lane: passed, on this Mac", "  tests: no test counts in its log"])

    def test_an_unmeasured_lane_is_never_passed(self):
        text = "test_a (m.T.test_a) ... skipped 'Unmeasured: no model'\nRan 2 tests in 0.1s\n\nOK (skipped=1)"
        self.configure(lane=self.printing(text))
        status, lines = self.qk("lane")
        self.assertEqual(status, lane_results.UNMEASURED)
        self.assertEqual(lines[-3:-1], ["qk run lane: unmeasured (exit 3), on this Mac",
                                        "  tests: 1 passed, 0 failed, 0 skipped, 1 unmeasured"])

    def test_a_lane_past_its_limit_is_unmeasured_and_counts_nothing_from_its_cut_off_log(self):
        self.configure(lane={"command": self.command("print('Ran 2 tests in 0.1s\\n\\nOK', flush=True)\nimport time\ntime.sleep(60)"),
                             "timeout": 1})
        status, lines = self.qk("lane")
        self.assertEqual(status, lane_results.UNMEASURED)
        self.assertEqual(lines[-4], "qk run lane: unmeasured (exit 3), on this Mac")
        self.assertEqual(lines[-3], "  tests: no counts, as the run did not finish and its log is incomplete")
        self.assertIn("  why: ", lines[-2])

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
        self.assertEqual(lines[-4:], ["qk run lane: failed (exit 65), on this Mac",
                                      "  tests: 7 passed, 1 failed, 2 skipped, 0 unmeasured",
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
        self.assertEqual(lines[-2], f"  no failing test is named; the log is {self.lane_dir / 'run.log'}")

    def test_a_lane_that_failed_before_any_test_is_told_apart_from_a_test_failure(self):
        self.configure(lane=self.printing(SETUP_FAILED, 1))
        status, lines = self.qk("lane")
        self.assertEqual(status, lane_results.BEFORE_TESTS)
        self.assertNotIn(status, (1, lane_results.UNMEASURED))
        self.assertEqual(lines[-4:], ["qk run lane: failed before any test ran (exit 4), on this Mac",
                                      "  tests: none ran; the last error in its log: "
                                      "Failed: project generation or canonical package-lock seeding failed",
                                      f"  the log is {self.lane_dir / 'run.log'}",
                                      f"  receipt: {self.lane_dir / 'source.json'}"])
        receipt = json.loads((self.lane_dir / "source.json").read_text())
        self.assertEqual((receipt["outcome"], receipt["exitStatus"], receipt["beforeTests"]),
                         ("failed", 1, "Failed: project generation or canonical package-lock seeding failed"))
        # It is a failure to the queue, as it was: whether it gives no verdict is the owner's call.
        self.assertNotIn(lane_results.BEFORE_TESTS, merge_queue.NO_VERDICT)
        summary = "\n".join(lines[-4:])
        self.assertIsNone(lane_results.counted(summary))
        self.assertEqual(merge_queue.FAILED_TEST.findall(summary), [])

    def test_a_lane_whose_tests_crashed_before_their_counts_stays_failed(self):
        crashed = ("Test Suite 'All tests' started at 2026-11-11 08:34:00.000\n"
                   "Test Case '-[Mod.ReaderTests testPage]' started.\nFatal error: index out of range\n")
        for text, status in ((crashed, 1), ("test_a (m.T.test_a) ... Fatal Python error: Segmentation fault\n", 139)):
            with self.subTest(status=status):
                self.configure(lane=self.printing(text, status))
                result, lines = self.qk("lane")
                self.assertEqual(result, status)
                self.assertEqual(lines[-4:-2], [f"qk run lane: failed (exit {status}), on this Mac",
                                                "  tests: no test counts in its log"])
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

    def test_a_too_short_run_says_why_in_its_summary(self):
        self.configure(lane={**self.printing("Ran 1 test in 0.1s\n\nOK"), "minimumTests": 2})
        status, lines = self.qk("lane")
        self.assertEqual(status, 2)
        self.assertEqual(lines[-5], "qk run lane: failed (exit 2), on this Mac")
        self.assertEqual(lines[-3], "  why: Expected at least 2 executed tests; observed 1")

    def test_a_group_counts_its_lanes_together_and_names_the_one_that_gave_no_counts(self):
        group = {"parallel": ["good", "bad", "build"], "jobs": 1}
        self.configure(good=self.printing("Ran 2 tests in 0.1s\n\nOK"), bad=self.printing(UNITTEST_FAILED, 1),
                       build=self.printing("no tests here"), lane=group)
        status, lines = self.qk("lane")
        self.assertEqual(status, 1)
        self.assertEqual(lines[-6:-3], ["qk run lane: failed (exit 1), on this Mac",
                                        "  tests: 5 passed, 2 failed, 1 skipped, 0 unmeasured; no test counts from build",
                                        "  lanes: 2 passed, 1 failed (bad), 0 unmeasured"])

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

    def test_the_second_macs_own_run_leaves_the_summary_to_the_mac_that_sent_it(self):
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

    def test_a_summary_that_cannot_be_made_never_changes_the_status(self):
        self.configure(lane=self.printing(""))
        with patch.object(lane_report.Report, "lines", side_effect=OSError("disk gone")):
            status, lines = self.qk("lane")
        self.assertEqual(status, 0)
        self.assertEqual(lines[-1], "qk run lane: exit 0; its summary could not be made: OSError: disk gone")


if __name__ == "__main__":
    unittest.main()
