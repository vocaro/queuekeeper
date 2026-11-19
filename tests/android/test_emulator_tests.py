"""How `qk emulator-tests` judges one emulator's run and keeps its results (queuekeeper_android.emulator_tests)."""
import contextlib
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import host_isolation  # noqa: F401
from queuekeeper import lane_results
from queuekeeper.parallel import RESULTS_VARIABLE
from queuekeeper_android import emulator_tests, instrumentation


def run_of(*outcomes, crashed=None, expected=None):
    run = instrumentation.Run()
    run.tests = [{"test": f"a.B#t{index}", "outcome": outcome, "seconds": 1.0} for index, outcome in enumerate(outcomes)]
    run.finished, run.crashed, run.expected = crashed is None, crashed, expected
    return run


class Judging(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        args = type("Args", (), {"logs": Path(self.temp.name), "test_timeout": 60})()
        self.run = emulator_tests.Run(args, Path(self.temp.name))

    def judged(self, run, status=0):
        with contextlib.redirect_stdout(StringIO()) as output, \
                patch.dict(os.environ, {RESULTS_VARIABLE: self.temp.name}):
            self.run.result("pixel_8", run, status, Path(self.temp.name) / "pixel_8.log", 3)
        return self.run.statuses["pixel_8"], output.getvalue()

    def test_a_run_whose_tests_passed_passes_and_says_its_count(self):
        status, said = self.judged(run_of("passed", "skipped"))
        self.assertEqual(status, 0)
        self.assertEqual(lane_results.counted(said), (2, 1))
        kept = json.loads((Path(self.temp.name) / emulator_tests.KEPT / "pixel_8.json").read_text())
        self.assertEqual((kept["keptBy"], len(kept["tests"])), ("qk emulator-tests", 2))
        self.assertEqual(self.run.seconds, {"a.B": 2.0})

    def test_a_failed_test_fails_the_run_and_is_named(self):
        status, said = self.judged(run_of("passed", "failed"))
        self.assertEqual(status, 1)
        self.assertIn("FAILED a.B#t1", said)

    def test_a_crash_a_short_run_a_timeout_and_no_test_run_are_unmeasured(self):
        for run, status in ((run_of("passed", "unmeasured", crashed="Process crashed."), 0),
                            (run_of("passed", expected=3), 0), (run_of("passed"), None),
                            (run_of("skipped", "skipped"), 0)):
            with self.subTest(run=[case["outcome"] for case in run.tests], status=status):
                self.assertEqual(self.judged(run, status)[0], lane_results.UNMEASURED)


if __name__ == "__main__":
    unittest.main()
