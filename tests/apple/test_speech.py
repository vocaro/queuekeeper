from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src/python"))
from queuekeeper_apple.simulators import speech
import host_isolation


class SilenceCuesTests(unittest.TestCase):
    UDID = "2BBC94CE-0D99-4A08-9763-269B5711AC97"

    def silence(self, reads="0\n", fail=None):
        calls = []
        def run(*command, timeout=60):
            calls.append(command)
            if fail and command[5] == fail:
                raise RuntimeError(f"{' '.join(command)}: failed")
            return reads if command[5] == "read" else ""
        return speech.silence_cues(self.UDID, run_command=run), calls

    def test_the_switch_is_written_off_then_read_back_on_the_named_simulator(self):
        silenced, calls = self.silence()
        self.assertIs(silenced, True)
        self.assertEqual(calls, [
            ("xcrun", "simctl", "spawn", self.UDID, "defaults", "write", "com.apple.VoiceOverTouch",
             "VoiceOverUseSoundEffects", "-bool", "false"),
            ("xcrun", "simctl", "spawn", self.UDID, "defaults", "read", "com.apple.VoiceOverTouch",
             "VoiceOverUseSoundEffects")])

    def test_a_switch_that_still_reads_on_is_not_silenced(self):
        self.assertIs(self.silence(reads="1\n")[0], False)
        self.assertIs(self.silence(reads="")[0], False)

    def attempts(self, unanswered):
        """silence_cues, whose first `unanswered` spawned commands reach their limit; its result and calls."""
        calls = []
        def run(*command, timeout=60):
            calls.append(command[5])
            if len(calls) <= unanswered:
                raise speech.NoAnswer(f"No answer in {timeout} s")
            return "0\n" if command[5] == "read" else ""
        return speech.silence_cues(self.UDID, run_command=run), calls

    def test_a_write_with_no_answer_is_tried_again(self):
        self.assertEqual(self.attempts(1), (True, ["write", "write", "read"]))

    def test_a_read_with_no_answer_is_tried_again(self):
        def run(*command, timeout=60):
            calls.append(command[5])
            if calls == ["write", "read"]:
                raise speech.NoAnswer("No answer")
            return "0\n" if command[5] == "read" else ""
        calls = []
        self.assertIs(speech.silence_cues(self.UDID, run_command=run), True)
        self.assertEqual(calls, ["write", "read", "read"])

    def test_a_command_that_never_answers_raises_within_its_tries(self):
        calls = []
        def run(*command, timeout=60):
            calls.append(command[5])
            raise speech.NoAnswer("No answer")
        with self.assertRaises(RuntimeError):
            speech.silence_cues(self.UDID, run_command=run)
        self.assertEqual(calls, ["write"] * speech.CUE_ATTEMPTS)

    def test_a_command_that_fails_is_not_tried_again(self):
        calls = []
        def run(*command, timeout=60):
            calls.append(command[5])
            raise RuntimeError("failed")
        with self.assertRaises(RuntimeError):
            speech.silence_cues(self.UDID, run_command=run)
        self.assertEqual(calls, ["write"])

    def test_a_failed_write_or_read_raises(self):
        for step in ("write", "read"):
            with self.subTest(step=step), self.assertRaises(RuntimeError):
                self.silence(fail=step)


class SimulatorViewTests(unittest.TestCase):
    def check(self, running):
        calls = []
        def run(command, **kwargs):
            calls.append(command)
            found = command[:2] == ["pgrep", "-x"] and command[2] in running
            return subprocess.CompletedProcess(command, 0 if found else 1, b"4242\n" if found else b"", b"")
        with patch.object(speech.subprocess, "run", side_effect=run):
            result = speech.simulator_app()
        # A check that started a viewer would itself open the live view it looks for.
        self.assertTrue(all(command[:2] == ["pgrep", "-x"] for command in calls), calls)
        return result

    def test_xcode_27_device_hub_is_a_live_simulator_view(self):
        self.assertEqual(self.check({"DeviceHub"}), "Device Hub")

    def test_simulator_app_from_earlier_xcode_is_still_found(self):
        self.assertEqual(self.check({"Simulator"}), "Simulator.app")

    def test_no_viewer_and_unrelated_processes_are_not_a_view(self):
        self.assertIsNone(self.check(set()))
        # pgrep -x matches whole names only: Device Hub's helpers are not its window.
        self.assertIsNone(self.check({"DevicesTrampoline", "DevicesSystemUpdater", "CoreSimulatorService"}))


class HoldMuteTests(unittest.TestCase):
    UDID = "2BBC94CE-0D99-4A08-9763-269B5711AC97"

    def hold(self, volumes, readings=3, limit=90, publishes=(True,), publish_limit=300):
        """hold_mute against readings that give `volumes` in turn, then 0, and SpringBoard's publication checks that
        give `publishes` in turn, then the last of them; each sleep is a second."""
        calls, now, given, checks = [], [0.0], list(volumes), list(publishes)
        def state(udid, kind, *change, timeout=20):
            calls.append((udid, kind, *change))
            if change: return {"volume": 0}
            value = given.pop(0) if given else 0
            if isinstance(value, Exception): raise value
            return {"volume": value}
        def publisher(udid):
            calls.append((udid, "published"))
            return checks.pop(0) if len(checks) > 1 else checks[0]
        def sleep(seconds): now[0] += seconds
        result = speech.hold_mute(self.UDID, state_reader=state, publisher=publisher, readings=readings, limit=limit,
                                  publish_limit=publish_limit, sleep=sleep, clock=lambda: now[0])
        return result, calls

    def test_the_volume_is_muted_then_read_until_it_holds_at_zero(self):
        (seconds, unmuted, published), calls = self.hold([])
        self.assertEqual(calls[0], (self.UDID, "audio", "--volume", "0"))
        self.assertEqual(calls[1:], [(self.UDID, "published")] + [(self.UDID, "audio")] * 3)
        self.assertEqual((seconds, unmuted, published), (2, [], 0))

    def test_a_volume_that_comes_back_is_muted_again_and_the_count_starts_over(self):
        (_, unmuted, _), calls = self.hold([0, 60, 0])
        self.assertEqual(unmuted, [60])
        self.assertEqual(calls.count((self.UDID, "audio", "--volume", "0")), 2)
        self.assertEqual(len([call for call in calls if call == (self.UDID, "audio")]), 5)

    def test_a_reading_with_no_answer_neither_counts_nor_breaks_the_run(self):
        (_, unmuted, _), calls = self.hold([RuntimeError("no answer"), 0, 0, 0])
        self.assertEqual(unmuted, [])
        self.assertEqual(len([call for call in calls if call == (self.UDID, "audio")]), 4)

    def test_a_mute_that_never_holds_raises(self):
        with self.assertRaisesRegex(RuntimeError, "did not hold at 0 for 3 readings in 5 s"):
            self.hold([60] * 100, limit=5)

    def test_readings_of_zero_before_springboard_publishes_do_not_count(self):
        # #54: a reset member read 0 for 20 to 31 s until its SpringBoard started and put the volume at 60, after a
        # hold that counted those readings had ended.
        (seconds, unmuted, published), calls = self.hold([0] * 25 + [60], publishes=[False] * 25 + [True])
        self.assertEqual(unmuted, [60])
        self.assertEqual(published, 25)
        # Three readings of 0 after the 60 was muted again, none of the 25 before it.
        self.assertEqual(seconds, 28)
        self.assertEqual(calls.count((self.UDID, "published")), 26)

    def test_a_reading_other_than_zero_is_springboards_own_publication(self):
        # A ringer that is off, or a check with no answer, still leaves the volume SpringBoard put.
        (seconds, unmuted, published), _ = self.hold([0, 0, 60], publishes=[None])
        self.assertEqual((unmuted, published, seconds), ([60], 2, 5))

    def test_a_springboard_that_never_publishes_raises(self):
        with self.assertRaisesRegex(RuntimeError, "SpringBoard did not publish the volume in 10 s"):
            self.hold([], publishes=[False], publish_limit=10)

    def test_the_hold_limit_runs_from_springboards_publication(self):
        (seconds, _, published), _ = self.hold([], limit=5, publishes=[False] * 50 + [True])
        self.assertEqual((published, seconds), (50, 52))

    def test_publication_is_read_from_springboards_ringer_state(self):
        commands = []
        def answer(text):
            def run(*command, timeout):
                commands.append((command, timeout))
                if isinstance(text, Exception): raise text
                return text
            return run
        self.assertTrue(speech.published(self.UDID, run_command=answer("com.apple.springboard.ringerstate 1\n")))
        self.assertFalse(speech.published(self.UDID, run_command=answer("com.apple.springboard.ringerstate 0\n")))
        self.assertIsNone(speech.published(self.UDID, run_command=answer(speech.NoAnswer("no answer in 5 s"))))
        self.assertIsNone(speech.published(self.UDID, run_command=answer("")))
        self.assertEqual(commands[0], (("xcrun", "simctl", "spawn", self.UDID, "notifyutil", "-g",
                                        "com.apple.springboard.ringerstate"), 5))
