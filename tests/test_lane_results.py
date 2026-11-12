"""The lane-result vocabulary: the tests a log counts as run, skipped and unmeasured.

These cases came from the toolkit's test_core.py (EvidenceTests) with the vocabulary.

This module does not import `host_isolation` yet, as every test module does: that helper does not
import until `cli` and `disk` are split (PORTING.md). It reads text only."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/python"))
from queuekeeper import lane_results


class LaneResultTests(unittest.TestCase):
    def test_zero_tests_cannot_pass_but_a_swift_testing_run_is_not_its_zero_test_xctest_shim(self):
        for text in ["** TEST SUCCEEDED **", "Executed 0 tests, with 0 failures", "Ran 0 tests in 0.000s\nOK", "Test run with 0 tests in 0 suites passed"]:
            with self.assertRaises(ValueError): lane_results.require_tests(text)
        self.assertEqual(lane_results.require_tests("Executed 0 tests, with 0 failures\nTest run with 30 tests in 2 suites passed after 1s"), 30)

        self.assertEqual(lane_results.require_tests("Ran 43 tests in 0.5s\n\nOK",43),43)

    def test_a_run_counts_each_bundle_and_swift_testing_run_once(self):
        def xctest(top, bundles):
            """swift test and xcodebuild run each bundle under its own top suite (Xcode 27.1, October 2026)."""
            executed = "\t Executed {} tests, with 0 failures (0 unexpected) in 0.1 (0.1) seconds"
            return "".join(f"Test Suite '{top}' started at 2026-10-22 23:11:16.585.\n"
                           f"Test Suite '{name}.xctest' started at 2026-10-22 23:11:16.587.\n"
                           f"Test Suite '{name}Case' passed at 2026-10-22 23:11:16.700.\n{executed.format(count)}\n"
                           f"Test Suite '{name}.xctest' passed at 2026-10-22 23:11:16.744.\n{executed.format(count)}\n"
                           f"Test Suite '{top}' passed at 2026-10-22 23:11:16.744.\n{executed.format(count)}\n"
                           for name, count in bundles)
        swift_testing = "✔ Test run with 65 tests in 5 suites passed after 6.436 seconds.\n"
        whole = xctest("All tests", [("Writing", 42), ("Viewing", 2516), ("Reading", 732)]) + swift_testing + \
            "✔ Test run with 0 tests in 0 suites passed after 0.001 seconds.\n"
        self.assertEqual(lane_results.require_tests(whole, 3355, expected=3355), 3355)
        filtered = xctest("Selected tests", [("Writing", 0), ("Viewing", 0), ("Reading", 8)])
        self.assertEqual(lane_results.require_tests(filtered, expected=8), 8)
        # A wrapper's total for each simulator counts, but not beside the totals it sums.
        summaries = "Passed: iPhone (iOS 27.0): Executed 3252 tests\nPassed: iPad (iOS 27.0): Executed 3252 tests\n"
        self.assertEqual(lane_results.require_tests(summaries, expected=6504), 6504)
        device = xctest("All tests", [("Writing", 16), ("Viewing", 2439), ("Reading", 732)]) + swift_testing
        self.assertEqual(lane_results.require_tests(device + summaries.splitlines()[0], expected=3252), 3252)
        self.assertEqual(lane_results.require_tests("Ran 40 tests in 1.0s\n\nOK\nRan 3 tests in 0.1s\n\nOK\n", expected=43), 43)

    def test_skipped_tests_count_toward_no_minimum(self):
        # XCTest and unittest count their skips in their totals and name them apart (Xcode 27.1,
        # Python 3.14, October 2026).
        def xctest(top, executed, skipped):
            line = f"\t Executed {executed} tests, with {skipped} tests skipped and 0 failures (0 unexpected) in 0.3 (0.3) seconds"
            return (f"Test Suite '{top}' started at 2026-10-26 07:00:00.000.\nTest Suite 'P.xctest' passed at 2026-10-26 07:00:00.300.\n"
                    f"{line}\nTest Suite '{top}' passed at 2026-10-26 07:00:00.300.\n{line}\n")
        cases = [(xctest("All tests", 16, 9), 7, 9), (xctest("Selected tests", 6, 6), 0, 6),
                 ("\t Executed 4 tests, with 1 test skipped and 0 failures (0 unexpected) in 0.1 (0.1) seconds\n", 3, 1),
                 ("Ran 4 tests in 0.01s\n\nOK (skipped=4)\n", 0, 4),
                 ("Ran 5 tests in 0.01s\n\nFAILED (failures=1, skipped=2, expected failures=1)\n", 3, 2),
                 ("Ran 3 tests in 0.01s\n\nOK (expected failures=1)\n", 3, 0),
                 # A wrapper that counts skips apart already left them out of its N.
                 ("Passed: iPhone: Executed 7 tests, 9 skipped\n", 7, 0)]
        for text, executed, skipped in cases:
            with self.subTest(text):
                self.assertEqual((lane_results.executed_tests(text), lane_results.skipped_tests(text)), (executed, skipped))
        # A run whose every selected test skipped establishes no coverage, and says why.
        for text in [xctest("Selected tests", 6, 6), "Ran 4 tests in 0.01s\n\nOK (skipped=4)\n"]:
            with self.subTest(text), self.assertRaisesRegex(ValueError, r"observed 0 \(\d skipped, which do not count\)"):
                lane_results.require_tests(text)
        self.assertEqual(lane_results.require_tests(xctest("All tests", 16, 9), 7), 7)
        # Swift Testing's total counts what it skipped or canceled; each has its own line
        # (Swift 6.4, October 2026).
        swift_testing = ("◇ Test run started.\n➜ Test conditionallyOff() skipped.\n◇ Test cancels() started.\n"
                         '➜ Test disabled() skipped: "off"\n'
                         '➜ Test cancels() was cancelled after 0.001 seconds: "Unavailable here"\n'
                         "✔ Test runs() passed after 0.001 seconds.\n"
                         "✔ Test run with 4 tests in 1 suite passed after 0.001 seconds.\n")
        self.assertEqual((lane_results.executed_tests(swift_testing), lane_results.skipped_tests(swift_testing)), (1, 3))
        self.assertEqual(lane_results.executed_tests(xctest("All tests", 3, 2) + swift_testing), 2)
        with self.assertRaisesRegex(ValueError, "at least 16 executed tests; observed 7"):
            lane_results.require_tests(xctest("All tests", 16, 9), 16)

    def test_an_expected_count_must_match_exactly(self):
        self.assertEqual(lane_results.require_tests("Executed 0 tests\nTest run with 3 tests in 1 suite passed", expected=3), 3)
        for text in ["Ran 2 tests", "Ran 4 tests", "** TEST SUCCEEDED **"]:
            with self.subTest(text), self.assertRaisesRegex(ValueError, "exactly 3"):
                lane_results.require_tests(text, expected=3)


    # Tests selected as `eng run --expect 2 LANE -- --filter ...` selects them, in Swift 6.4's lines.
    CANCELED = '-> Test %s() was cancelled after 0.001 seconds: "Set CORPUS for the admitted corpus."\n'

    TWO_CANCELED = CANCELED % "a" + CANCELED % "b" + "Test run with 2 tests in 1 suite passed after 0.001 seconds.\n"

    ONE_CANCELED = CANCELED % "a" + "Test run with 2 tests in 1 suite passed after 0.001 seconds.\n"

    def test_an_expected_count_short_only_by_skips_is_not_run_and_any_other_miss_is_a_plain_failure(self):
        for text in [self.TWO_CANCELED, self.ONE_CANCELED, "Ran 2 tests in 0.1s\n\nOK (skipped=1)\n"]:
            with self.subTest(text), self.assertRaises(lane_results.NotRun):
                lane_results.require_tests(text, expected=2)
        # Controls: two canceled of three expected, one over, one short with nothing skipped, and no count at all.
        for text, expected in [(self.TWO_CANCELED, 3), ("Ran 3 tests", 2), ("Ran 1 tests", 2), ("** TEST SUCCEEDED **", 2)]:
            with self.subTest(text), self.assertRaises(ValueError) as raised:
                lane_results.require_tests(text, expected=expected)
            self.assertNotIsInstance(raised.exception, lane_results.NotRun)

    def test_a_skip_or_cancellation_whose_reason_begins_unmeasured_reports_an_unmeasured_test(self):
        # swift test and xcodebuild print the first two alike, and exit 0; unittest prints its reason
        # only when verbose (Swift 6.4, Xcode 27.1, Python 3.14, October 2026).
        xctest = "/p/Tests/T.swift:6: -[ProbeTests.ProbeXCTests testUnmeasured] : Test skipped - Unmeasured: the 80 s backstop passed"
        swift_testing = '➜ Test swiftTestingUnmeasured() was cancelled after 0.001 seconds: "Unmeasured: the 80 s backstop passed"'
        python = "test_unmeasured (test_probe.Probe.test_unmeasured) ... skipped 'Unmeasured: the 80 s backstop passed'"
        measured = ["/p/Tests/T.swift:9: -[ProbeTests.ProbeXCTests testOther] : Test skipped - needs a Mac",
                    "Test Case '-[ProbeTests.ProbeXCTests testUnmeasured]' skipped (0.002 seconds).",
                    '➜ Test other() was cancelled after 0.001 seconds: "the window closed"',
                    "test_other (test_probe.Probe.test_other) ... skipped 'needs a Mac'",
                    "Unmeasured: macOS: 1 tests a busy host decided; see mac.log"]
        # A wrapper's summary that repeats a line adds no test.
        log = "\n".join([*measured, xctest, swift_testing, python, xctest]) + "\n"
        self.assertEqual(lane_results.unmeasured_tests(log), [xctest, swift_testing, python])
        self.assertEqual(lane_results.unmeasured_tests("\n".join(measured)), [])


    # pytest 8.4.2's output, with and without pytest-xdist 3.8.0 (Python 3.12, 2026-11-03), on a
    # module of passes, skips, unittest skips, xfails, a failure, a fixture error and a warning.
    PYTEST_SKIPS = ("=========================== short test summary info ============================\n"
                    "SKIPPED test_sample.py::test_skip - Skipped: needs a fixture\n"
                    "SKIPPED test_sample.py::test_unmeasured_a - Skipped: Unmeasured: model unavailable\n"
                    "SKIPPED test_sample.py::test_unmeasured_b - Skipped: Unmeasured: model unavailable\n"
                    "SKIPPED test_sample.py::test_same_reason[1] - Skipped: Unmeasured: same line\n"
                    "SKIPPED test_sample.py::test_same_reason[2] - Skipped: Unmeasured: same line\n"
                    "SKIPPED test_sample.py::Case::test_ut_unmeasured - Skipped: Unmeasured: device away\n")

    def test_a_pytest_run_counts_from_its_summary_line(self):
        cases = [("========================= 1 passed, 1 skipped in 0.00s =========================\n", 1, 1),
                 # -q drops the frame; a gate that indents its steps indents it.
                 ("1 failed, 5 passed, 9 skipped, 1 xfailed, 1 xpassed, 1 warning, 1 error in 0.16s\n", 8, 9),
                 ("      1 failed, 10468 passed, 24 skipped in 589.31s (0:09:49)\n", 10469, 24),
                 # Deselected tests, warnings and errors are not tests that ran.
                 ("= 1 failed, 4 passed, 7 skipped, 1 deselected, 1 xfailed, 1 xpassed, 1 warning, 1 error in 0.02s =\n", 7, 7),
                 ("=== 3 passed, 2 warnings, 2 errors in 1.50s ===\n", 3, 0),
                 ("============================ 18 deselected in 0.01s ============================\n", 0, 0),
                 ("============================ no tests ran in 0.01s =============================\n", 0, 0)]
        for text, executed, skipped in cases:
            with self.subTest(text):
                log = "collected 18 items\n\ntest_ok.py .s    [100%]\n\n" + self.PYTEST_SKIPS + text
                self.assertEqual((lane_results.executed_tests(log), lane_results.skipped_tests(log)), (executed, skipped))
        self.assertEqual(lane_results.require_tests("10 passed in 0.5s\n", 10, expected=10), 10)
        with self.assertRaisesRegex(ValueError, r"observed 0 \(4 skipped, which do not count\)"):
            lane_results.require_tests("==== 4 skipped in 0.01s ====\n")
        # Beside a unittest run in the same log, each counts once.
        self.assertEqual(lane_results.require_tests("Ran 40 tests in 1.0s\n\nOK (skipped=2)\n3 passed, 1 skipped in 0.1s\n",
                                                expected=41), 41)

    def test_a_line_that_is_not_pytests_summary_is_no_count(self):
        for text in ["", self.PYTEST_SKIPS, "collected 16 items / 1 deselected / 15 selected\n",
                     "test_sample.py::test_pass PASSED                                         [  6%]\n",
                     "Bundle: 3 passed, 1 failed, 0 skipped\n", "the 3 passed checks in 2s were green\n",
                     "all 3 passed in 0.1s\n", "3 passed, 2 flaky in 0.1s\n", "3 passed\n",
                     "Lane: 3 passed in 0.1s\n"]:
            with self.subTest(text):
                self.assertIsNone(lane_results.counted(text))

    def test_a_pytest_skip_whose_reason_begins_unmeasured_reports_its_test(self):
        unmeasured = [line for line in self.PYTEST_SKIPS.splitlines() if "Unmeasured:" in line]
        self.assertEqual(lane_results.unmeasured_tests(self.PYTEST_SKIPS + "1 passed, 6 skipped in 0.1s\n"), unmeasured)
        # Folded by -rs, -ra or -rA without --no-fold-skipped, a line names no test but still
        # leaves the run unmeasured, an indented one too.
        folded = ["SKIPPED [1] test_sample.py:9: needs a fixture",
                  "SKIPPED [2] test_sample.py:43: Unmeasured: same line",
                  "      SKIPPED [1] tests/test_y.py: Unmeasured: the module's mark"]
        self.assertEqual(lane_results.unmeasured_tests("\n".join(folded)), [line.strip() for line in folded[1:]])
        measured = ["SKIPPED test_sample.py::test_skip - Skipped: needs a fixture",
                    "SKIPPED [1] test_sample.py:9: needs a fixture",
                    "SKIPPED test_sample.py::test_skip - Skipped: not Unmeasured: here",
                    # -v truncates a reason and xdist's -v drops it: neither names an unmeasured test.
                    "test_sample.py::test_unmeasured_a SKIPPED (Unmeasured: model unavail...) [ 25%]",
                    "[gw1] [ 25%] SKIPPED test_sample.py::test_unmeasured_a ",
                    "XFAIL test_sample.py::test_xfail - Unmeasured: reason",
                    "1 passed, 6 skipped in 0.1s"]
        self.assertEqual(lane_results.unmeasured_tests("\n".join(measured)), [])


if __name__ == "__main__":
    unittest.main()
