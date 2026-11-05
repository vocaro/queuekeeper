from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/python"))
from vocaro_engineering.simulators import speech
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
