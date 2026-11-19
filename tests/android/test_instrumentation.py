"""`am instrument -r` output read test by test (queuekeeper_android.instrumentation)."""
import unittest

import host_isolation  # noqa: F401
from queuekeeper import lane_results
from queuekeeper_android import instrumentation


def block(test, code, cls="com.example.ReaderTest", **more):
    lines = [f"INSTRUMENTATION_STATUS: class={cls}", "INSTRUMENTATION_STATUS: current=1",
             "INSTRUMENTATION_STATUS: id=AndroidJUnitRunner", "INSTRUMENTATION_STATUS: numtests=4",
             "INSTRUMENTATION_STATUS: stream=", f"INSTRUMENTATION_STATUS: test={test}"]
    lines += [f"INSTRUMENTATION_STATUS: {key}={value}" for key, value in more.items()]
    return lines + [f"INSTRUMENTATION_STATUS_CODE: {code}"]


RUN = "\n".join([
    *block("opens", 1), *block("opens", 0),
    *block("turns", 1), *block("turns", -2, stack="java.lang.AssertionError: expected 2\n\tat com.example.ReaderTest.turns(ReaderTest.kt:12)"),
    *block("speaks", 1), *block("speaks", -3),
    *block("needsTablet", 1), *block("needsTablet", -4, stack="org.junit.AssumptionViolatedException: phone"),
    "INSTRUMENTATION_RESULT: stream=", "", "Time: 1.2", "", "FAILURES!!!", "Tests run: 3,  Failures: 1", "",
    "INSTRUMENTATION_CODE: -1"])


class Reading(unittest.TestCase):
    def test_each_test_s_end_gives_its_outcome_and_a_stack_s_first_line_its_reason(self):
        run = instrumentation.parse(RUN)
        self.assertEqual([(case["test"], case["outcome"]) for case in run.tests], [
            ("com.example.ReaderTest#opens", "passed"), ("com.example.ReaderTest#turns", "failed"),
            ("com.example.ReaderTest#speaks", "skipped"), ("com.example.ReaderTest#needsTablet", "skipped")])
        self.assertEqual(run.tests[1]["reason"], "java.lang.AssertionError: expected 2")
        self.assertTrue(run.tests[3]["reason"].startswith("assumption failed"))
        self.assertTrue(run.complete)
        self.assertEqual(run.counts(), (4, 2, 1, 0))

    def test_the_summary_is_a_count_lane_results_reads(self):
        run = instrumentation.parse(RUN)
        self.assertEqual(lane_results.counted(run.summary()), (4, 2))

    def test_seconds_come_from_when_each_test_s_lines_arrived(self):
        run = instrumentation.Run()
        for at, line in enumerate(block("opens", 1) + block("opens", 0)):
            run.feed(line + "\n", at=float(at))
        self.assertEqual(run.tests[0]["seconds"], 7.0)

    def test_a_process_that_died_mid_test_leaves_that_test_unmeasured_and_the_run_incomplete(self):
        text = "\n".join([*block("opens", 1), *block("opens", 0), *block("turns", 1),
                          "INSTRUMENTATION_RESULT: shortMsg=Process crashed.", "INSTRUMENTATION_CODE: 0",
                          "INSTRUMENTATION_FAILED: com.example.test/androidx.test.runner.AndroidJUnitRunner"])
        run = instrumentation.parse(text)
        self.assertEqual(run.tests[-1]["outcome"], "unmeasured")
        self.assertIn("AndroidJUnitRunner", run.tests[-1]["reason"])
        self.assertFalse(run.complete)

    def test_a_run_that_announced_more_tests_than_ended_is_incomplete(self):
        run = instrumentation.parse("\n".join([*block("opens", 1), *block("opens", 0), "INSTRUMENTATION_CODE: -1"]))
        self.assertEqual(run.expected, 4)
        self.assertFalse(run.complete)

    def test_the_command_runs_raw_and_waits_with_a_shard_and_a_selection(self):
        self.assertEqual(instrumentation.command("com.example.test", "androidx.test.runner.AndroidJUnitRunner",
                                                 shard=(1, 3), tests=["a.B", "a.C#d"], arguments={"log": "true"}),
                         ["am", "instrument", "-r", "-w", "-e", "numShards", "3", "-e", "shardIndex", "1",
                          "-e", "class", "a.B,a.C#d", "-e", "log", "true",
                          "com.example.test/androidx.test.runner.AndroidJUnitRunner"])


if __name__ == "__main__":
    unittest.main()
