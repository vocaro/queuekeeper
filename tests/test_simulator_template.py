import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/python"))
import host_isolation  # noqa: F401
from vocaro_engineering.simulators import template

UDID = "2BBC94CE-0D99-4A08-9763-269B5711AC97"
RUNTIME = "com.apple.CoreSimulator.SimRuntime.iOS-27-2"
IDENTITY = {"buildversion": "24B8117", "version": "27.2"}


class Clock:
    """Seconds that pass only when the warm-up sleeps."""
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Device:
    """Stand-ins for simctl, devicectl and the log stream of one simulator, recording each call in order.

    speaks_after: seconds after VoiceOver is switched on when the speech extension logs its first phrase,
    None for never. volumes: what the volume reads after each mute, in turn, then 0."""
    def __init__(self, clock, speaks_after=50, volumes=(), boot=0, bootstatus=0, stuck_on=False):
        self.clock, self.speaks_after, self.volumes = clock, speaks_after, list(volumes)
        self.calls, self.boot, self.bootstatus, self.stuck_on = [], boot, bootstatus, stuck_on
        self.voiceover, self.volume, self.switched, self.log = False, 60, None, None

    def simctl(self, *arguments, timeout=600):
        self.calls.append(("simctl", *arguments))
        status = {"boot": self.boot, "bootstatus": self.bootstatus}.get(arguments[0], 0)
        return subprocess.CompletedProcess(arguments, status, "", "failed" if status else "")

    def devicectl(self, *arguments, timeout=30):
        self.calls.append(("devicectl", *arguments))
        action, kind = arguments[0], arguments[1]
        if action == "settings" and kind == "audio":
            self.volume = self.volumes.pop(0) if self.volumes else 0
        elif action == "settings" and kind == "voiceover":
            self.voiceover = "--enable" in arguments or (self.stuck_on and self.voiceover)
            if "--enable" in arguments: self.switched = self.clock()
        elif action == "info":
            result = {"volume": self.volume} if kind == "audio" else {"enabled": self.voiceover}
            # The log exists only while the warm-up streams it.
            if self.log is not None and self.log.parent.exists() and self.voiceover and \
                    self.speaks_after is not None and self.clock() - self.switched >= self.speaks_after:
                self.log.write_text(f"MauiAUSP: {template.SPEECH}: en-US\n")
            return subprocess.CompletedProcess(arguments, 0, json.dumps({"result": result}), "")
        return subprocess.CompletedProcess(arguments, 0, "", "")

    def stream(self, udid, path):
        self.calls.append(("stream", udid))
        self.log = Path(path)
        self.log.write_text("")
        process = mock.Mock()
        process.wait.return_value = 0
        return process

    def order(self, *wanted):
        """The calls' positions, each the first call that starts with its parts."""
        return [next(index for index, call in enumerate(self.calls) if call[:len(part)] == part) for part in wanted]


class WarmTests(unittest.TestCase):
    def warm(self, device, clock, **options):
        with mock.patch.object(template.runtime_identity, "runtime_for_device", return_value=IDENTITY):
            return template.warm(UDID, RUNTIME, simctl=device.simctl, devicectl=device.devicectl,
                                 stream=device.stream, clock=clock, sleep=clock.sleep, **options)

    def test_voiceover_starts_only_once_muted_and_stops_once_it_spoke(self):
        clock = Clock()
        device = Device(clock, speaks_after=50)
        record = self.warm(device, clock)
        self.assertEqual((record["runtimeBuild"], record["runtimeVersion"], record["voiceOverWarmed"]),
                         ("24B8117", "27.2", True))
        self.assertTrue(record["voiceOver"].startswith("spoke "), record)
        self.assertNotIn("could not be confirmed off", record["voiceOver"])
        boot, status, mute, stream, on, off = device.order(
            ("simctl", "boot"), ("simctl", "bootstatus"), ("devicectl", "settings", "audio"), ("stream",),
            ("devicectl", "settings", "voiceover", "--device", UDID, "--enable"),
            ("devicectl", "settings", "voiceover", "--device", UDID, "--disable"))
        self.assertTrue(boot < status < mute < stream < on < off, device.calls)
        # The mute held MUTE_HELD readings before VoiceOver started.
        readings = [call for call in device.calls[mute:on] if call[:3] == ("devicectl", "info", "audio")]
        self.assertGreaterEqual(len(readings), template.MUTE_HELD)
        self.assertFalse(device.voiceover)
        # Nothing here shuts down, erases, clones or deletes: that is the pool's.
        self.assertEqual({call[1] for call in device.calls if call[0] == "simctl"}, {"boot", "bootstatus"})

    def test_a_volume_that_comes_back_is_muted_again_before_voiceover_starts(self):
        clock = Clock()
        # Muted, then back at 60 as a warm boot's audio service starts, then muted for good.
        device = Device(clock)
        original = device.devicectl
        readings = iter([0, 0, 60])
        def devicectl(*arguments, timeout=30):
            if arguments[:2] == ("info", "audio"):
                device.volume = next(readings, device.volume)
            return original(*arguments, timeout=timeout)
        device.devicectl = devicectl
        record = self.warm(device, clock)
        self.assertEqual(record["unmuted"], [60])
        mutes = [index for index, call in enumerate(device.calls) if call[:3] == ("devicectl", "settings", "audio")]
        on = device.order(("devicectl", "settings", "voiceover", "--device", UDID, "--enable"))[0]
        self.assertEqual(len([index for index in mutes if index < on]), 2)
        heard = [call for call in device.calls[mutes[-1]:on] if call[:3] == ("devicectl", "info", "audio")]
        self.assertGreaterEqual(len(heard), template.MUTE_HELD)

    def test_a_reading_with_no_answer_neither_counts_nor_breaks_the_mute_hold(self):
        clock = Clock()
        device = Device(clock)
        original = device.devicectl
        # Five zeros, three readings with no answer, then zeros: the hold completes after twenty zeros.
        answers = iter([0] * 5 + [None] * 3)
        def devicectl(*arguments, timeout=30):
            if arguments[:2] == ("info", "audio"):
                answer = next(answers, 0)
                if answer is None:
                    device.calls.append(("devicectl", *arguments))
                    return subprocess.CompletedProcess(arguments, 124, "", "timed out after 10 s")
            return original(*arguments, timeout=timeout)
        device.devicectl = devicectl
        record = self.warm(device, clock)
        self.assertEqual(record["voiceOverWarmed"], True)
        self.assertNotIn("unmuted", record)
        mute = device.order(("devicectl", "settings", "audio"))[0]
        on = device.order(("devicectl", "settings", "voiceover", "--device", UDID, "--enable"))[0]
        readings = [call for call in device.calls[mute:on] if call[:3] == ("devicectl", "info", "audio")]
        # Twenty zeros and the three without an answer: none of the three started the count again.
        self.assertEqual(len(readings), template.MUTE_HELD + 3)
        # And within MUTE_LIMIT still: readings that never answer never start VoiceOver.
        clock, device = Clock(), Device(clock)
        silent = device.devicectl
        device.devicectl = lambda *arguments, timeout=30: (
            subprocess.CompletedProcess(arguments, 124, "", "") if arguments[:2] == ("info", "audio")
            else silent(*arguments, timeout=timeout))
        record = self.warm(device, clock)
        self.assertIn("not started: the volume did not hold at 0", record["voiceOver"])
        self.assertLessEqual(clock(), template.MUTE_LIMIT + 1)
        self.assertFalse([call for call in device.calls if call[:3] == ("devicectl", "settings", "voiceover")])

    def test_voiceover_that_will_not_read_off_is_recorded_so(self):
        clock = Clock()
        device = Device(clock, stuck_on=True)
        record = self.warm(device, clock)
        self.assertEqual(record["voiceOverOff"], False)
        self.assertTrue(record["voiceOver"].endswith("; it could not be confirmed off"), record)
        # It was switched off once at the end and once more when the first reading said it was still on.
        disables = [call for call in device.calls if call[:3] == ("devicectl", "settings", "voiceover")
                    and "--disable" in call]
        self.assertEqual(len(disables), 2)
        clock = Clock()
        device = Device(clock)
        self.assertEqual(self.warm(device, clock)["voiceOverOff"], True)

    def test_voiceover_never_starts_unless_the_volume_holds_at_zero(self):
        clock = Clock()
        device = Device(clock, volumes=[60] * 1000)
        record = self.warm(device, clock)
        self.assertIn("not started: the volume did not hold at 0", record["voiceOver"])
        self.assertNotIn("voiceOverWarmed", record)
        self.assertFalse([call for call in device.calls if call[:3] == ("devicectl", "settings", "voiceover")])
        self.assertFalse([call for call in device.calls if call[0] == "stream"])

    def test_silent_voiceover_leaves_the_template_cold_and_voiceover_off(self):
        clock = Clock()
        device = Device(clock, speaks_after=None)
        record = self.warm(device, clock, voiceover_limit=30)
        self.assertEqual(record["voiceOverWarmed"], False)
        self.assertEqual(record["voiceOver"], "silent for 30 s, so it is not warmed")
        self.assertFalse(device.voiceover)
        self.assertGreaterEqual(clock(), 30)

    def test_a_boot_that_fails_or_reaches_its_limit_is_no_template(self):
        for name, options, message in (("boot", {"boot": 164}, "simctl boot exited 164"),
                                       ("limit", {"bootstatus": 124}, "did not finish within 600 s")):
            with self.subTest(name):
                clock = Clock()
                device = Device(clock, **options)
                with self.assertRaisesRegex(template.WarmFailed, message):
                    self.warm(device, clock)
                self.assertFalse([call for call in device.calls if call[0] != "simctl"])
        clock = Clock()
        device = Device(clock)
        with mock.patch.object(template.runtime_identity, "runtime_for_device",
                               side_effect=template.runtime_identity.RuntimeIdentityUnavailable("no root")):
            with self.assertRaisesRegex(template.WarmFailed, "its runtime build could not be read: no root"):
                template.warm(UDID, RUNTIME, simctl=device.simctl, devicectl=device.devicectl, stream=device.stream,
                              clock=clock, sleep=clock.sleep)
        self.assertFalse([call for call in device.calls if call[0] == "devicectl"])

    def test_a_stop_while_voiceover_is_on_switches_it_off(self):
        clock = Clock()
        device = Device(clock, speaks_after=None)
        def stop(seconds):
            if device.voiceover: raise KeyboardInterrupt
            clock.sleep(seconds)
        with mock.patch.object(template.runtime_identity, "runtime_for_device", return_value=IDENTITY):
            with self.assertRaises(KeyboardInterrupt):
                template.warm(UDID, RUNTIME, simctl=device.simctl, devicectl=device.devicectl, stream=device.stream,
                              clock=clock, sleep=stop)
        self.assertFalse(device.voiceover)
        self.assertEqual(device.calls[-1][:3], ("devicectl", "settings", "voiceover"))


if __name__ == "__main__":
    unittest.main()
