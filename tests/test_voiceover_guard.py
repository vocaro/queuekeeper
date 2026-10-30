import os
from pathlib import Path
import signal
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/python"))
from vocaro_engineering import processes
from vocaro_engineering.voiceover import guard


class GuardTests(unittest.TestCase):
    def test_a_phrase_naming_the_banner_stops_the_session_and_others_do_not(self):
        guard.check_banner(["text word0001", "Next Page button"], "before left")
        with self.assertRaisesRegex(guard.BannerNamed, "Automation Running banner before left"):
            guard.check_banner(["Next Page button", "application Automation Running"], "before left")
        # A step's failure handler, `except Exception`, never takes a stop.
        self.assertTrue(issubclass(guard.BannerNamed, Exception))
        self.assertFalse(issubclass(guard.Stopped, Exception))

    def test_a_stop_raises_once_inside_the_block_and_the_previous_handlers_come_back(self):
        before = {number: signal.getsignal(number) for number in guard.STOP_SIGNALS}
        restored = []
        with self.assertRaisesRegex(guard.Stopped, "Stopped by SIGTERM"):
            with guard.stop_signals():
                try:
                    os.kill(os.getpid(), signal.SIGTERM)
                    self.fail("the stop did not interrupt the session")
                except guard.Stopped:
                    # A second stop during the restoration is ignored, and the restoration completes.
                    os.kill(os.getpid(), signal.SIGTERM)
                    restored.append("VoiceOver off")
                    raise
        self.assertEqual(restored, ["VoiceOver off"])
        self.assertEqual({number: signal.getsignal(number) for number in guard.STOP_SIGNALS}, before)
        # Outside the block nothing is changed.
        with guard.stop_signals():
            pass
        self.assertEqual({number: signal.getsignal(number) for number in guard.STOP_SIGNALS}, before)

    def test_a_session_the_banner_stopped_runs_again_within_its_bounds(self):
        def runs(statuses, may=lambda: True, attempts=guard.BANNER_ATTEMPTS):
            ran = []
            def session(attempt):
                ran.append(attempt)
                return statuses[attempt - 1], f"transcript-{attempt}"
            return guard.run_again(session, may, attempts), ran
        stop = guard.BANNER_STOP
        self.assertEqual(runs([0]), ((0, "transcript-1", []), [1]))
        self.assertEqual(runs([stop, 0]), ((0, "transcript-2", ["transcript-1"]), [1, 2]))
        self.assertEqual(runs([stop, stop, stop, 0]), ((stop, "transcript-3", ["transcript-1", "transcript-2"]),
                                                       [1, 2, 3]))
        # Any other stop is the session's outcome at once, and a run out of time runs nothing more.
        self.assertEqual(runs([125, 0]), ((125, "transcript-1", []), [1]))
        self.assertEqual(runs([stop, 0], may=lambda: False), ((stop, "transcript-1", []), [1]))
        self.assertEqual(runs([stop, 0], attempts=1), ((stop, "transcript-1", []), [1]))

    def test_a_foreground_run_has_longer_than_any_other_between_sigterm_and_sigkill(self):
        self.assertGreater(processes.FOREGROUND_KILL_AFTER, processes.KILL_AFTER)


if __name__ == "__main__":
    unittest.main()
