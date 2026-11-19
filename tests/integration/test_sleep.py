"""A lane whose command ran while this Mac slept, and that failed, is unmeasured, the host's fault and not the commits':
a test's time limit runs on through the sleep while nothing runs (vocaro/queuekeeper#198). No test puts a Mac to
sleep: each fakes remote.asleep(), the seconds this Mac has slept, which a clock that runs through sleep less one that
stops for it gives."""
import contextlib
from io import StringIO
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src/python"))
from queuekeeper import cli, lane_results, remote
import host_isolation  # noqa: F401


def sleeping(seconds):
    """A stand-in for remote.asleep() whose first reading is 1000 s and every later one seconds more, as if this Mac
    slept that long after the lane's command started."""
    readings = []
    def asleep():
        readings.append(None)
        return 1000.0 if len(readings) == 1 else 1000.0 + seconds
    return asleep


FAIL = [sys.executable, "-c", "raise SystemExit(1)"]
PASS = [sys.executable, "-c", "pass"]


class ClockTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "darwin", "the clocks are macOS's")
    def test_the_clock_that_stops_in_sleep_is_monotonic_and_the_other_runs_on(self):
        """time.monotonic() is mach_absolute_time(), CLOCK_UPTIME_RAW, which stops while the Mac sleeps; the
        difference CLOCK_MONOTONIC_RAW less it is asleep()'s reading, never negative."""
        self.assertEqual(time.get_clock_info("monotonic").implementation, "mach_absolute_time()")
        before = time.clock_gettime(time.CLOCK_UPTIME_RAW)
        now = time.monotonic()
        self.assertLess(abs(now - before), 1)
        self.assertGreaterEqual(remote.asleep(), 0)

    def test_a_sleep_shorter_than_the_noted_one_or_unreadable_is_none(self):
        with patch.object(cli.remote, "asleep", return_value=1000.0 + remote.SLEEP_NOTED - 1):
            self.assertEqual(cli.slept_since(1000.0), 0)
        with patch.object(cli.remote, "asleep", return_value=1600.0):
            self.assertEqual(cli.slept_since(1000.0), 600)
        with patch.object(cli.remote, "asleep", return_value=None):
            self.assertEqual(cli.slept_since(1000.0), 0)
        self.assertEqual(cli.slept_since(None), 0)


class LaneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def project(self, **lanes):
        (self.root / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Sleep", "lanes": lanes}))

    def run_lane(self, name, slept):
        with patch.object(cli, "source_identity", return_value={}), \
                patch.object(cli.remote, "asleep", side_effect=sleeping(slept)), \
                contextlib.redirect_stderr(StringIO()) as said:
            status = cli.main(["--project", str(self.root), "run", name])
        return status, said.getvalue()

    def receipt(self, name):
        return json.loads((self.root / ".build/queuekeeper" / name / "source.json").read_text())

    def test_a_lane_that_fails_through_a_sleep_is_unmeasured_and_names_the_sleep(self):
        """On 2026-11-16 a Pagepour gate sent back a configuration change on two Swift tests that timed out across
        this Mac's clamshell sleep."""
        self.project(tests={"command": FAIL})
        status, said = self.run_lane("tests", 600)
        self.assertEqual(status, lane_results.UNMEASURED, said)
        self.assertIn("queuekeeper: unmeasured: this Mac (", said)
        self.assertIn("slept for 600 s while tests ran", said)
        recorded = self.receipt("tests")
        self.assertEqual(recorded["outcome"], "unmeasured")
        self.assertEqual(recorded["sleptSeconds"], 600)
        # The receipt's error is the cause the merge queue's hold gives (Worker.receipt_error).
        self.assertIn("slept for 600 s while tests ran", recorded["error"])
        self.assertEqual(recorded["unmeasured"], [recorded["error"]])

    def test_a_failure_with_no_sleep_stays_the_lanes(self):
        """Negative control: a failure with no sleep, or a sleep too short to note, still goes back to the entry."""
        self.project(tests={"command": FAIL})
        for slept in (0, remote.SLEEP_NOTED - 1):
            with self.subTest(slept=slept):
                status, said = self.run_lane("tests", slept)
                self.assertNotIn(status, (0, lane_results.UNMEASURED), said)
                self.assertNotIn("slept", said)
                recorded = self.receipt("tests")
                self.assertEqual(recorded["outcome"], "failed")
                self.assertNotIn("sleptSeconds", recorded)

    def test_a_pass_through_a_sleep_keeps_its_pass(self):
        self.project(tests={"command": PASS})
        status, said = self.run_lane("tests", 600)
        self.assertEqual(status, 0, said)
        self.assertNotIn("unmeasured", said)
        recorded = self.receipt("tests")
        self.assertEqual(recorded["outcome"], "passed")
        self.assertEqual(recorded["sleptSeconds"], 600)

    def test_a_build_that_failed_through_a_sleep_stays_failed(self):
        """A failed build never reads as unmeasured (cli.compile_first): a compiler's error is no time limit."""
        self.project(tests={"command": PASS, "compileFirst": FAIL})
        status, said = self.run_lane("tests", 600)
        self.assertNotIn(status, (0, lane_results.UNMEASURED), said)
        self.assertEqual(self.receipt("tests")["outcome"], "build-failed")

    def test_a_group_whose_lane_fails_through_a_sleep_is_unmeasured(self):
        """The 2026-11-16 failures were in mac-tests, a lane of the mac-suites group."""
        self.project(suites={"parallel": ["one", "two"]}, one={"command": PASS}, two={"command": FAIL})
        status, said = self.run_lane("suites", 900)
        self.assertEqual(status, lane_results.UNMEASURED, said)
        self.assertIn("slept for 900 s while suites ran", said)
        recorded = self.receipt("suites")
        self.assertEqual(recorded["outcome"], "unmeasured")
        self.assertIn("slept for 900 s", recorded["error"])
        # Negative control: the same group with no sleep fails.
        status, said = self.run_lane("suites", 0)
        self.assertNotIn(status, (0, lane_results.UNMEASURED), said)
        self.assertEqual(self.receipt("suites")["outcome"], "failed")

    def test_a_lane_that_reaches_its_limit_through_a_sleep_says_how_long_the_mac_slept(self):
        self.project(slow={"command": [sys.executable, "-c", "import time; time.sleep(60)"], "timeout": 1})
        status, said = self.run_lane("slow", 700)
        self.assertEqual(status, lane_results.UNMEASURED, said)
        self.assertIn("slept for 700 s of that", said)


if __name__ == "__main__":
    unittest.main()
