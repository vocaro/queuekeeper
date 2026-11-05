import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/python"))
import host_isolation  # noqa: F401
from vocaro_engineering.simulators import template

UDID = "2BBC94CE-0D99-4A08-9763-269B5711AC97"
RUNTIME = "com.apple.CoreSimulator.SimRuntime.iOS-27-2"
IDENTITY = {"buildversion": "24B8117", "version": "27.2"}
TOOL = "/build/simulator-voices"


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
    None for never. volumes: what the volume reads after each mute, in turn, then 0. cues: what VoiceOver's
    sound-cue switch reads after it is written, or a number for a write that exits so. voices: the seconds
    each use of the voices program takes to list the voices, in turn, None for no answer within its limit;
    reboot: what bootstatus exits after the second boot."""
    def __init__(self, clock, speaks_after=50, volumes=(), boot=0, bootstatus=0, stuck_on=False, cues="0",
                 voices=(57.0, 24.0), reboot=0):
        self.clock, self.speaks_after, self.volumes = clock, speaks_after, list(volumes)
        self.calls, self.boot, self.bootstatus, self.stuck_on = [], boot, bootstatus, stuck_on
        self.voiceover, self.volume, self.switched, self.log = False, 60, None, None
        self.cues, self.cue_setting = cues, "1"
        self.voices, self.reboot, self.boots, self.uses = list(voices), reboot, 0, []

    def simctl(self, *arguments, timeout=600):
        self.calls.append(("simctl", *arguments))
        if arguments[:1] == ("spawn",) and arguments[2:3] == ("defaults",):
            self.assertion(arguments[4:6] == (template.CUE_DOMAIN, template.CUE_KEY), arguments)
            if arguments[3] == "write":
                if isinstance(self.cues, int):
                    return subprocess.CompletedProcess(arguments, self.cues, "", "failed")
                self.cue_setting = self.cues
                return subprocess.CompletedProcess(arguments, 0, "", "")
            return subprocess.CompletedProcess(arguments, 0, self.cue_setting + "\n", "")
        if arguments[:1] == ("spawn",):
            # The voices program, timed by this clock: its lines, or no answer within the limit.
            self.assertion(arguments[1:] == (UDID, TOOL), arguments)
            seconds = self.voices.pop(0) if self.voices else 1.0
            self.uses.append((self.clock(), self.boots))
            if seconds is None or seconds > timeout:
                self.clock.sleep(timeout)
                return subprocess.CompletedProcess(arguments, 124, "", f"timed out after {timeout} s")
            self.clock.sleep(seconds + 0.9)
            return subprocess.CompletedProcess(arguments, 0, f"0.001 start pid 4242\n{seconds:.3f} speechVoices 180\n"
                                                             f"{seconds + 0.9:.3f} languages 40 of 42 have their own voice\n", "")
        if arguments[0] == "boot": self.boots += 1
        if arguments[0] == "bootstatus": self.clock.sleep(2)
        status = {"boot": self.boot, "bootstatus": self.bootstatus if self.boots < 2 else self.reboot}.get(arguments[0], 0)
        return subprocess.CompletedProcess(arguments, status, "", "failed" if status else "")

    @staticmethod
    def assertion(holds, detail):
        if not holds:
            raise AssertionError(f"an unexpected defaults call: {detail}")

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
        options.setdefault("tool", TOOL)
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
        # Nothing here erases, clones or deletes: that is the pool's. Its simctl boots, reads the boot, shuts
        # down and boots again between the voices' uses, and spawns only the sound-cue switch's write and read
        # and the voices program.
        self.assertEqual({call[1] for call in device.calls if call[0] == "simctl"}, {"boot", "bootstatus", "spawn", "shutdown"})
        self.assertEqual({call[4] for call in device.calls if call[:4] == ("simctl", "spawn", UDID, "defaults")}, {"write", "read"})
        self.assertEqual([call[3] for call in device.calls if call[:3] == ("simctl", "spawn", UDID) and call[3] != "defaults"],
                         [TOOL, TOOL])

    def test_sound_cues_are_switched_off_before_voiceover_starts(self):
        clock = Clock()
        device = Device(clock)
        record = self.warm(device, clock)
        self.assertIs(record["cueSoundsOff"], True)
        status, write, read, on = device.order(
            ("simctl", "bootstatus"), ("simctl", "spawn", UDID, "defaults", "write"),
            ("simctl", "spawn", UDID, "defaults", "read"),
            ("devicectl", "settings", "voiceover", "--device", UDID, "--enable"))
        self.assertTrue(status < write < read < on, device.calls)
        self.assertEqual(device.calls[write][4:], ("write", template.CUE_DOMAIN, template.CUE_KEY, "-bool", "false"))
        self.assertEqual(device.cue_setting, "0")

    def test_voiceover_never_starts_unless_the_sound_cues_read_off(self):
        # Negative controls: a switch that reads on after the write, and a write that fails.
        for cues in ("1", 1):
            with self.subTest(cues=cues):
                clock = Clock()
                device = Device(clock, cues=cues)
                record = self.warm(device, clock)
                self.assertIs(record["cueSoundsOff"], False)
                self.assertEqual(record["voiceOver"], "not started: VoiceOver's sound cues could not be switched off")
                self.assertIs(record["voiceOverOff"], True)
                self.assertNotIn("voiceOverWarmed", record)
                self.assertFalse([call for call in device.calls
                                  if call[:3] == ("devicectl", "settings", "voiceover") and "--enable" in call])
                self.assertFalse([call for call in device.calls if call[0] == "stream"])

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
                              clock=clock, sleep=clock.sleep, tool=TOOL)
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
                              clock=clock, sleep=stop, tool=TOOL)
        self.assertFalse(device.voiceover)
        self.assertEqual(device.calls[-1][:3], ("devicectl", "settings", "voiceover"))

    def test_the_voices_are_used_twice_with_a_reboot_between_once_voiceover_is_off(self):
        clock = Clock()
        device = Device(clock, voices=(57.0, 24.0))
        record = self.warm(device, clock)
        voices = record["voices"]
        self.assertEqual((voices["settled"], voices["limit"], voices["firstUse"]["seconds"], voices["firstUse"]["count"],
                          voices["secondUse"]["seconds"], voices["secondUse"]["count"]),
                         (True, template.VOICES_LIMIT, 57.0, 180, 24.0, 180))
        self.assertEqual((voices["firstUse"]["languages"], voices["firstUse"]["languagesWithAVoice"],
                          voices["firstUse"]["languagesSeconds"]), (42, 40, 57.9))
        self.assertEqual(voices["outcome"], "settled: 180 voices listed 57.0 s into the first use and 24.0 s into the "
                                            "second, after a reboot")
        self.assertEqual(voices["rebootSeconds"], 2.0)
        # VoiceOver was off before the first use; the second use came after a shutdown and a second boot.
        off = device.order(("devicectl", "settings", "voiceover", "--device", UDID, "--disable"))[0]
        uses = [index for index, call in enumerate(device.calls) if call[:4] == ("simctl", "spawn", UDID, TOOL)]
        boots = [index for index, call in enumerate(device.calls) if call[:2] == ("simctl", "boot")]
        downs = [index for index, call in enumerate(device.calls) if call[:2] == ("simctl", "shutdown")]
        self.assertEqual((len(uses), len(boots), len(downs)), (2, 2, 1))
        self.assertTrue(off < uses[0] < downs[0] < boots[1] < uses[1], device.calls)
        self.assertEqual([boots for _, boots in device.uses], [1, 2])
        # Both uses ran within the limit given, as the pool's take gives a shorter one.
        clock = Clock()
        device = Device(clock)
        self.warm(device, clock, voices_limit=template.TAKE_VOICES_LIMIT)
        self.assertEqual([call for call in device.calls if call[:4] == ("simctl", "spawn", UDID, TOOL)], [("simctl", "spawn", UDID, TOOL)] * 2)
        self.assertEqual(template.TAKE_VOICES_LIMIT, 300)

    def test_a_use_with_no_answer_or_a_reboot_that_fails_leaves_the_voices_unsettled(self):
        # The first use: no reboot, no second use.
        clock = Clock()
        device = Device(clock, voices=(None,))
        record = self.warm(device, clock, voices_limit=300)
        voices = record["voices"]
        self.assertEqual((voices["settled"], voices["firstUse"]["seconds"], voices["outcome"]),
                         (False, None, "the first use gave no answer: no answer in 300 s"))
        self.assertNotIn("secondUse", voices)
        self.assertEqual(len([call for call in device.calls if call[:2] == ("simctl", "shutdown")]), 0)
        self.assertEqual(len([call for call in device.calls if call[:4] == ("simctl", "spawn", UDID, TOOL)]), 1)
        self.assertEqual(record["voiceOverWarmed"], True)
        # The second use.
        clock = Clock()
        device = Device(clock, voices=(57.0, None))
        voices = self.warm(device, clock, voices_limit=300)["voices"]
        self.assertEqual((voices["settled"], voices["firstUse"]["seconds"], voices["secondUse"]["seconds"], voices["outcome"]),
                         (False, 57.0, None, "the second use, after a reboot, gave no answer: no answer in 300 s"))
        # The reboot between them.
        for reboot, detail in ((124, "reached its limit"), (165, "exited 165: failed")):
            with self.subTest(reboot=reboot):
                clock = Clock()
                device = Device(clock, reboot=reboot)
                voices = self.warm(device, clock)["voices"]
                self.assertEqual((voices["settled"], voices["outcome"]), (False, f"the reboot between the uses {detail}"))
                self.assertNotIn("secondUse", voices)
                self.assertEqual(len([call for call in device.calls if call[:4] == ("simctl", "spawn", UDID, TOOL)]), 1)
        # A program that lists no voice exits 1, and its words are kept.
        answer = lambda *arguments, timeout: subprocess.CompletedProcess(arguments, 1, "0.001 start pid 1\n0.500 speechVoices 0\n", "")
        self.assertEqual(template.use_voices(UDID, TOOL, simctl=answer, limit=10),
                         {"seconds": None, "outcome": "the voices program exited 1: 0.001 start pid 1\n0.500 speechVoices 0"})

    def test_the_voices_are_used_whatever_voiceover_did(self):
        for name, options in (("cues", dict(cues="1")), ("mute", dict(volumes=[60] * 1000)), ("silent", dict(speaks_after=None))):
            with self.subTest(name):
                clock = Clock()
                device = Device(clock, **options)
                record = self.warm(device, clock, voiceover_limit=30)
                self.assertNotEqual(record.get("voiceOverWarmed"), True)
                self.assertEqual(record["voices"]["settled"], True)
                self.assertEqual(len([call for call in device.calls if call[:4] == ("simctl", "spawn", UDID, TOOL)]), 2)

    def test_a_voices_program_that_cannot_be_built_is_recorded_and_voiceover_still_warms(self):
        clock = Clock()
        device = Device(clock)
        with mock.patch.object(template, "voices_tool", side_effect=template.ToolUnbuilt("the voices program did not build: no SDK")):
            record = self.warm(device, clock, tool=None)
        self.assertEqual(record["voices"], {"limit": template.VOICES_LIMIT, "settled": False,
                                            "outcome": "the voices program did not build: no SDK"})
        self.assertEqual(record["voiceOverWarmed"], True)
        self.assertFalse([call for call in device.calls if call[:3] == ("simctl", "spawn", UDID) and call[3] != "defaults"])
        self.assertFalse([call for call in device.calls if call[:2] == ("simctl", "shutdown")])

    def test_voices_tool_builds_once_per_source_target_and_sdk_and_signs_it(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        build, source = Path(directory.name) / "build", Path(directory.name) / "simulator-voices.swift"
        source.write_text("print(1)")
        calls = []
        def run(*command, timeout):
            calls.append(command)
            if command[3:4] == ("--show-sdk-path",):
                return subprocess.CompletedProcess(command, 0, "/SDKs/iPhoneSimulator.sdk\n", "")
            if command[3:4] == ("--show-sdk-build-version",):
                return subprocess.CompletedProcess(command, 0, "23A1\n", "")
            if command[3:4] == ("swiftc",):
                Path(command[command.index("-o") + 1]).write_text("binary")
                return subprocess.CompletedProcess(command, 0, "", "")
            if command[0] == "codesign":
                return subprocess.CompletedProcess(command, 0, "", "")
            raise AssertionError(command)
        built = template.voices_tool(build, source, run=run)
        self.assertTrue(built.is_file() and os.access(built, os.X_OK), built)
        self.assertTrue(built.name.startswith("simulator-voices-"), built)
        swiftc = [command for command in calls if command[3:4] == ("swiftc",)]
        self.assertEqual(len(swiftc), 1)
        self.assertEqual(swiftc[0][:7], ("xcrun", "--sdk", "iphonesimulator", "swiftc", "-O", "-target", template.VOICES_TARGET))
        self.assertIn(str(source), swiftc[0])
        self.assertIn(("codesign", "--force", "--sign", "-", "--identifier", "simulator-voices", mock.ANY), calls)
        # Built once: the second call finds it.
        self.assertEqual(template.voices_tool(build, source, run=run), built)
        self.assertEqual(len([command for command in calls if command[3:4] == ("swiftc",)]), 1)
        # Another source is another program; a compiler that fails is ToolUnbuilt, with its words, and leaves
        # nothing half made.
        source.write_text("print(2)")
        def failing(*command, timeout):
            if command[3:4] == ("swiftc",):
                return subprocess.CompletedProcess(command, 1, "", "error: no such module 'AVFAudio'")
            return run(*command, timeout=timeout)
        with self.assertRaisesRegex(template.ToolUnbuilt, "did not build: error: no such module 'AVFAudio'"):
            template.voices_tool(build, source, run=failing)
        self.assertEqual(sorted(path.name for path in build.iterdir()), [built.name])
        missing = lambda *command, timeout: subprocess.CompletedProcess(command, 1, "", "xcrun: error: SDK not found")
        with self.assertRaisesRegex(template.ToolUnbuilt, "no iOS Simulator SDK"):
            template.voices_tool(build, source, run=missing)


if __name__ == "__main__":
    unittest.main()
