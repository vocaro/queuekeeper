import contextlib
from datetime import datetime, timedelta
from io import StringIO
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/python"))
from vocaro_engineering import cli, config, devices, foreground, offload_host, remote
import host_isolation
from host_isolation import stretched
from vocaro_engineering.voiceover import state as voiceover_state


class Later(datetime):
    """Past a slot's expected release: EXPECTED, three hours, plus one."""

    @classmethod
    def now(cls, tz=None):
        return datetime.now(tz) + timedelta(seconds=foreground.EXPECTED + 3600)


SECOND = "Trevors-MacBook-Pro-715"


def setUpModule():
    # No test reaches the owner's second Mac, its device record or this host's admission.
    environment = patch.dict(os.environ, {"VOCARO_OFFLOAD_HOST": "none"})
    environment.start()
    unittest.addModuleCleanup(environment.stop)


class SlotTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.record = devices.Record(self.root / "device-handoff.json")
        self.said = []
        for patcher in (patch.dict(os.environ, {devices.OVERRIDE: str(self.record.path)}),
                        patch.object(foreground, "front", return_value="Finder (ASN:0x0-0x1:)"),
                        # eng's guard always settles; short for offline fakes.
                        patch.object(voiceover_state, "SETTLE", 0.3)):
            patcher.start()
            self.addCleanup(patcher.stop)
        os.environ.pop(devices.OWNER, None)

    def second_mac(self):
        self.record.defer(SECOND)

    def slot(self, **options):
        options.setdefault("ready", lambda: [])
        options.setdefault("voiceover", lambda: False)
        options.setdefault("said", self.said.append)
        return foreground.slot("Project voiceover-mac", timeout=options.pop("timeout", stretched(5)), record=self.record,
                               poll=0.05, **options)

    def test_only_the_second_mac_takes_its_foreground_and_only_once_it_is_ready(self):
        # This Mac's foreground needs the owner's window, which the session reserves itself.
        with self.assertRaisesRegex(devices.Refused, "only on the second Mac"):
            with self.slot(): self.fail("ran on this Mac")
        self.assertIsNone(self.record.entry(devices.FOREGROUND))
        self.second_mac()
        lacking = [("a command started over SSH is not trusted for accessibility", "add sshd-keygen-wrapper")]
        with self.assertRaisesRegex(devices.Refused, "the owner's .*\n- a command started over SSH is not trusted "
                                                     "for accessibility: add sshd-keygen-wrapper"):
            with self.slot(ready=lambda: lacking): self.fail("ran on a second Mac that is not ready")
        # Nothing was taken.
        self.assertIsNone(self.record.entry(devices.FOREGROUND))

    def test_a_run_holds_the_foreground_without_asking_and_releases_it(self):
        self.second_mac()
        with self.slot() as owner:
            entry = self.record.entry(devices.FOREGROUND)
            self.assertEqual(os.environ[devices.OWNER], owner)
            # Drivers' own check passes for the run, as `devices check` does.
            self.assertEqual(devices.require_foreground(owner)["ownerChatID"], owner)
            self.assertEqual(devices.condition(entry), "active")
            self.assertEqual(entry["hostProcessIdentifier"], os.getpid())
            # The second Mac is not a workstation: its reservation records no approval (#207).
            self.assertNotIn("approval", entry)
            # A run there may outlast its expected release.
            with patch.object(devices, "datetime", Later):
                self.assertEqual(devices.require_foreground(owner)["ownerChatID"], owner)
            self.assertEqual(entry["purpose"], "Project voiceover-mac")
        self.assertNotIn(devices.OWNER, os.environ)
        entry = self.record.entry(devices.FOREGROUND)
        self.assertEqual((entry["state"], entry["lastOwnerChatID"], entry["run"], entry["frontmost"]),
                         ("released", owner, "the run's work ended", "restored"))
        self.assertIn("Finder", entry["restoration"])
        # A run that left another application in front did not restore it.
        with patch.object(foreground, "front", side_effect=["Finder (ASN:0x0-0x1:)", "Safari (ASN:0x0-0x2:)"]):
            with self.slot(): pass
        self.assertEqual(self.record.entry(devices.FOREGROUND)["frontmost"], "not-ours")

    def test_voiceover_on_is_a_valid_baseline_after_acquiring_the_lease(self):
        self.second_mac()
        with patch.object(voiceover_state, "_toggle") as toggle:
            with self.slot(voiceover=lambda: True):
                self.assertEqual(devices.condition(self.record.entry(devices.FOREGROUND)), "active")
            toggle.assert_not_called()
        entry = self.record.entry(devices.FOREGROUND)
        self.assertEqual((entry["state"], entry["run"]), ("released", "the run's work ended"))
        self.assertNotIn(devices.OWNER, os.environ)
        with self.slot(): pass

    def test_initial_unknown_is_untouched_and_released_but_final_unknown_is_retained(self):
        self.second_mac()
        with self.assertRaises(voiceover_state.UnknownState):
            with self.slot(voiceover=lambda: None): self.fail("unknown baseline ran")
        self.assertEqual(self.record.entry(devices.FOREGROUND)["state"], "released")
        known = [False]
        with self.assertRaises(voiceover_state.TransitionFailed):
            with self.slot(voiceover=lambda: known[0]): known[0] = None
        self.assertEqual(devices.condition(self.record.entry(devices.FOREGROUND)), "uncertain")
        self.assertTrue(self.record.entry(devices.FOREGROUND)["activeRun"])


    def test_a_timed_out_toggle_whose_original_state_is_verified_releases_the_foreground(self):
        # #137: a driver's Command-F5 timed out on a loaded second Mac, VoiceOver came on later, and the
        # foreground stayed uncertain for attended recovery. Now the guard turns it off again and releases.
        self.second_mac()
        enabled, sent = [False], []
        def read(): return enabled[0]
        def timed_out():
            sent.append(not enabled[0])
            if len(sent) == 1:
                enabled[0] = True  # Applied, but its reply never came.
                raise subprocess.TimeoutExpired("osascript", voiceover_state.WAIT)
            enabled[0] = not enabled[0]
        with patch.object(voiceover_state, "SETTLE", 0.3), patch.object(voiceover_state, "_read", read), \
                patch.object(voiceover_state, "_toggle", timed_out), \
                patch.object(voiceover_state, "_ready_to_enable", return_value=True):
            with self.assertRaises(voiceover_state.TransitionFailed) as failed:
                with self.slot(voiceover=read) as owner:
                    voiceover_state.set_enabled(True, owner)
            self.assertTrue(failed.exception.restored)
            self.assertEqual((enabled[0], sent), (False, [True, False]))
            entry = self.record.entry(devices.FOREGROUND)
            self.assertEqual((entry["state"], entry["run"]), ("released", "ended by an exception"))
            # Negative control: when no key restores the original, the foreground stays uncertain.
            enabled[0], sent[:] = False, []
            def never_restores():
                sent.append(not enabled[0])
                enabled[0] = True
                raise subprocess.TimeoutExpired("osascript", voiceover_state.WAIT)
            with patch.object(voiceover_state, "_toggle", never_restores), \
                    self.assertRaisesRegex(voiceover_state.TransitionFailed, "original state was not restored") as failed:
                with self.slot(voiceover=read) as owner:
                    voiceover_state.set_enabled(True, owner)
            self.assertFalse(failed.exception.restored)
        entry = self.record.entry(devices.FOREGROUND)
        self.assertEqual(devices.condition(entry), "uncertain")
        # The next run names the exact recovery command, not an `eng` subcommand.
        with self.assertRaisesRegex(devices.Refused, re.escape(
                "python3 -m vocaro_engineering.devices recover mac-foreground --by SESSION "
                f"--revision {entry['revision']} --verified WHAT")):
            with self.slot(): self.fail("ran over an uncertain foreground")

    def killed_child(self, toggle):
        """A run whose child driver sent Command-F5, timed out and was killed before its own restoration: the
        parent hears only an abnormal end. The key arrives late, on the third read after the run's first."""
        self.second_mac()
        enabled, reads, sent = [False], [0], []
        def read():
            reads[0] += 1
            if reads[0] == 3: enabled[0] = True  # The dead child's late key.
            return enabled[0]
        def send():
            sent.append(not enabled[0])
            toggle(enabled)
        with patch.object(voiceover_state, "_toggle", send), \
                patch.object(voiceover_state, "_ready_to_enable", return_value=True):
            try:
                with self.slot(voiceover=read):
                    raise RuntimeError("driver killed by SIGKILL; no report")
            except (RuntimeError, voiceover_state.TransitionFailed) as error:
                failed = error
        return enabled, read, sent, failed

    def test_a_killed_childs_late_key_is_undone_by_the_settled_outer_guard_and_released(self):
        def works(enabled): enabled[0] = not enabled[0]
        enabled, read, sent, failed = self.killed_child(works)
        self.assertRegex(str(failed), "driver killed")
        self.assertEqual(sent, [False])
        self.assertIs(read(), False)
        entry = self.record.entry(devices.FOREGROUND)
        self.assertEqual((entry["state"], entry["run"]), ("released", "ended by an exception"))

    def test_a_killed_childs_late_key_the_outer_guard_cannot_undo_leaves_the_foreground_uncertain(self):
        # Negative control: before the outer guard settled, it read the original once, released, and the late key
        # then left VoiceOver on behind a released foreground.
        def fails(enabled): raise subprocess.TimeoutExpired("osascript", voiceover_state.WAIT)
        enabled, read, sent, failed = self.killed_child(fails)
        self.assertIsInstance(failed, voiceover_state.TransitionFailed)
        self.assertFalse(failed.restored)
        self.assertTrue(enabled[0])
        # The first attempt finds the original and sends nothing; settling sees the late key; each later attempt
        # sends a key that fails.
        self.assertEqual(len(sent), voiceover_state.RESTORE_ATTEMPTS - 1)
        self.assertEqual(devices.condition(self.record.entry(devices.FOREGROUND)), "uncertain")

    def test_a_run_that_fails_still_releases_the_foreground(self):
        self.second_mac()
        with self.assertRaisesRegex(RuntimeError, "driver failed"):
            with self.slot():
                raise RuntimeError("driver failed")
        entry = self.record.entry(devices.FOREGROUND)
        self.assertEqual((entry["state"], entry["run"]), ("released", "ended by an exception"))

    def test_a_run_waits_while_another_holds_the_foreground_and_takes_it_once_released(self):
        self.second_mac()
        held = self.record.reserve(devices.FOREGROUND, owner="other run", revision=0, purpose="Other voiceover-mac",
                                   release_by=(time.strftime("%Y") + "-12-31T00:00:00+00:00"))
        # The holder's release comes once this run says it waits, not after a time a slow host can outlast (#119);
        # until then, it takes nothing.
        waiting = threading.Event()
        def said(text):
            self.said.append(text)
            if "held by other run" in text: waiting.set()
        def release():
            if not waiting.wait(stretched(10)): return
            self.assertEqual(self.record.entry(devices.FOREGROUND)["ownerChatID"], "other run")
            self.record.release(devices.FOREGROUND, owner="other run", revision=held["revision"], outcome="done",
                                restoration="checked", frontmost="restored")
        releaser = threading.Thread(target=release)
        releaser.start()
        self.addCleanup(releaser.join)
        def baseline():
            entry = self.record.entry(devices.FOREGROUND)
            self.assertNotEqual(entry["ownerChatID"], "other run")
            self.assertEqual(devices.condition(entry), "active")
            return False
        # A run that starts late, as on a busy host, still finds the holder.
        def late():
            time.sleep(0.5)
            return []
        with self.slot(voiceover=baseline, ready=late, said=said, timeout=stretched(10)) as owner:
            self.assertEqual(self.record.entry(devices.FOREGROUND)["ownerChatID"], owner)
        self.assertEqual(len([line for line in self.said if "held by other run" in line]), 1)

    def test_a_run_that_waits_too_long_or_finds_an_orphan_takes_nothing(self):
        self.second_mac()
        held = self.record.reserve(devices.FOREGROUND, owner="other run", revision=0, purpose="Other voiceover-mac",
                                   release_by=(time.strftime("%Y") + "-12-31T00:00:00+00:00"))
        with self.assertRaisesRegex(TimeoutError, "held by other run for 0.2s"):
            with patch.object(voiceover_state, "_read") as read, patch.object(voiceover_state, "_toggle") as toggle:
                # Fixed: the 0.2 s wait is the behavior under test, and the holder never releases.
                with self.slot(timeout=0.2, voiceover=read): self.fail("ran while another run held the foreground")
        read.assert_not_called()
        toggle.assert_not_called()
        # A run whose process is gone left an orphan, which needs attended recovery.
        self.record.update(devices.FOREGROUND, owner="other run", revision=held["revision"], activeRun=True,
                           hostProcessIdentifier=os.getpid())
        with patch.object(devices, "process_start", return_value="another start"):
            with self.assertRaisesRegex(devices.Refused, "orphan.*attended recovery"):
                with self.slot(): self.fail("ran over an orphan")
        self.assertEqual(self.record.entry(devices.FOREGROUND)["ownerChatID"], "other run")


class ReadinessTests(unittest.TestCase):
    def problems(self, console="trevor", trusted=True, capture=True, settings=None):
        settings = {"lsappinfo front": "ASN:0x0-0x2:", "lsappinfo info -only name ASN:0x0-0x2:": '"Safari" ASN',
                    "automationmodetool": "This device DOES NOT REQUIRE user authentication",
                    "defaults read com.apple.VoiceOverTraining doNotShowSplashScreen": "1",
                    "defaults read com.apple.HIToolbox AppleDictationAutoEnable": "0", **(settings or {})}
        user = type("User", (), {"pw_name": "trevor"})
        with patch.object(offload_host, "console_user", return_value=console), \
             patch.object(offload_host, "accessibility_trusted", return_value=trusted), \
             patch.object(offload_host, "screen_capture_allowed", return_value=capture), \
             patch.object(offload_host.pwd, "getpwuid", return_value=user), \
             patch.object(offload_host, "output", side_effect=lambda command, *_: settings.get(" ".join(command))):
            return [problem for problem, _fix in offload_host.foreground_problems()]

    def test_each_missing_setting_is_reported_alone_and_a_ready_mac_has_none(self):
        self.assertEqual(self.problems(), [])
        cases = {"logged in on the screen": dict(console="root"),
                 "screen is locked": dict(settings={"lsappinfo info -only name ASN:0x0-0x2:": '"loginwindow" ASN'}),
                 "not trusted for accessibility": dict(trusted=False),
                 "may not capture the screen": dict(capture=False),
                 "automation mode asks for authentication": dict(settings={"automationmodetool": "REQUIRES"}),
                 "welcome dialog": dict(settings={"defaults read com.apple.VoiceOverTraining doNotShowSplashScreen": None}),
                 "enable Dictation": dict(settings={"defaults read com.apple.HIToolbox AppleDictationAutoEnable": "1"})}
        for words, case in cases.items():
            with self.subTest(words):
                found = self.problems(**case)
                self.assertEqual(len(found), 1, found)
                self.assertIn(words, found[0])

    def test_eng_remote_foreground_reports_the_owner_fixes_and_the_holder(self):
        answers = [{"mac": "Second", "problems": [["the screen is locked", "unlock it"]], "voiceover": True,
                    "entry": None},
                   {"mac": "Second", "problems": [], "entry": {"state": "reserved", "ownerChatID": "eng 7 on Second",
                                                                "purpose": "P lane", "expectedReleaseBy": "later"}}]
        for answer, status, words in zip(answers, [remote.MISCONFIGURED, 0],
                                         ["no run holds it\n- the screen is locked: unlock it",
                                          "eng 7 on Second holds it for P lane, expected release later\nReady"]):
            with self.subTest(status), patch.object(remote, "ask", return_value=answer), \
                 contextlib.redirect_stdout(StringIO()) as output:
                self.assertEqual(remote.foreground_report("build-host"), status)
            self.assertIn(words, output.getvalue())


class RoutingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.record = devices.Record(self.root / "device-handoff.json")
        self.during = self.root / "during.json"
        script = ("import json, os, sys; entry = json.load(open(sys.argv[1]))['devices']['mac-foreground']; "
                  "json.dump({'owner': os.environ.get('VOCARO_DEVICE_OWNER'), 'entry': entry}, open(sys.argv[2], 'w'));"
                  " print('Ran 1 tests')")
        self.lanes = {"voiceover-mac": {"command": [sys.executable, "-c", script, str(self.record.path),
                                                    str(self.during)], "resource": "heavy", "foreground": True}}
        (self.project / "engineering.json").write_text(json.dumps({"schemaVersion": 1, "name": "Project",
                                                                   "lanes": self.lanes}))
        for patcher in (patch.dict(os.environ, {devices.OVERRIDE: str(self.record.path),
                                                "HEAVY_SLOT_DIR": str(self.root / "slots"), "HEAVY_POLL_SECONDS": "0.01",
                                                "SW_GATE_LOCK_FILE": str(self.root / "gate")}),
                        patch.object(cli, "source_identity", return_value={}),
                        patch.object(cli, "refresh_policy", return_value=None),
                        patch.object(voiceover_state, "_read", return_value=False),
                        # eng's guard around a foreground run always settles, for a stretched SETTLE. VoiceOver is
                        # a fake here that never changes, so no settle can show a late key: the real 5 s, stretched
                        # by load, held each run 40 to 120 s on a loaded host, and 0.3 s still 7 s at the stretch cap.
                        patch.object(voiceover_state, "SETTLE", 0.05),
                        patch.object(foreground, "front", return_value=None)):
            patcher.start()
            self.addCleanup(patcher.stop)
        os.environ.pop(devices.OWNER, None)

    def eng(self, *arguments):
        with contextlib.redirect_stderr(StringIO()) as error, contextlib.redirect_stdout(StringIO()):
            return cli.main(["--project", str(self.project), *arguments]), error.getvalue()

    def test_a_foreground_lane_goes_to_the_second_mac_and_never_falls_back_to_this_mac(self):
        sent = []
        with patch.object(remote, "foreground_lane", side_effect=lambda *a: sent.append(a) or 0):
            self.assertEqual(self.eng("run", "voiceover-mac", "--", "steps=x")[0], 0)
            self.assertEqual(self.eng("run", "--offload-only", "voiceover-mac")[0], 0)
        self.assertEqual([a[2:4] for a in sent], [("voiceover-mac", ["steps=x"]), ("voiceover-mac", [])])
        # With no second Mac to send it to, it stops and says how this Mac's foreground is used instead.
        status, error = self.eng("run", "voiceover-mac")
        self.assertEqual(status, 2)
        self.assertIn("no second Mac is configured", error)
        self.assertIn("window the owner agrees first", error)
        self.assertFalse(self.during.exists())

    def test_an_unreachable_second_mac_offers_a_window_here_for_foreground_work_alone(self):
        self.lanes["plain"] = {"command": [sys.executable, "-c", "print('Ran 1 tests')"], "resource": "heavy"}
        (self.project / "engineering.json").write_text(json.dumps({"schemaVersion": 1, "name": "Project",
                                                                   "lanes": self.lanes}))
        away = remote.Unreachable("build-host cannot be reached: ssh: connect to host mac.local port 22: Host is down",
                                  "wake build-host and connect it to the network")
        with patch.object(remote, "configured", return_value="build-host"), \
                patch.object(remote, "named_host", return_value="build-host"), \
                patch.object(remote, "execute", side_effect=away):
            ran = {"lane": self.eng("run", "voiceover-mac"), "remote lane": self.eng("remote", "run", "voiceover-mac"),
                   "admit": self.eng("remote", "admit", "--foreground", "--", "true")}
            plain = {"lane": self.eng("remote", "run", "plain"), "admit": self.eng("remote", "admit", "--", "true")}
        for name, here in (("lane", "eng run --here voiceover-mac"), ("remote lane", "eng run --here voiceover-mac"),
                           ("admit", "eng admit --foreground -- COMMAND")):
            with self.subTest(name):
                status, error = ran[name]
                self.assertEqual(status, 2)
                self.assertIn("Host is down; to fix it, wake build-host", error)
                self.assertIn("ask the owner for a window on this Mac's foreground", error)
                self.assertIn(f"`{here}`", error)
        # Work that does not take the screen is told nothing of a window.
        for name, (status, error) in plain.items():
            with self.subTest(f"plain {name}"):
                self.assertEqual(status, 2)
                self.assertIn("Host is down", error)
                self.assertNotIn("window", error)
        self.assertFalse(self.during.exists())

    def test_a_misconfigured_second_mac_offers_no_window_here(self):
        stop = remote.Misconfigured("build-host could not run the toolkit", "give it a python3")
        with patch.object(remote, "configured", return_value="build-host"), \
                patch.object(remote, "named_host", return_value="build-host"), \
                patch.object(remote, "execute", side_effect=stop):
            status, error = self.eng("remote", "admit", "--foreground", "--", "true")
        self.assertEqual(status, 2)
        self.assertIn("could not run the toolkit", error)
        self.assertNotIn("window", error)

    def test_a_foreground_lane_gets_the_longer_grace_before_sigkill_and_another_lane_does_not(self):
        from vocaro_engineering import processes
        self.record.defer(SECOND)
        self.lanes["plain"] = {"command": [sys.executable, "-c", "print('Ran 1 tests')"], "resource": "heavy"}
        (self.project / "engineering.json").write_text(json.dumps({"schemaVersion": 1, "name": "Project",
                                                                   "lanes": self.lanes}))
        graces = []
        real = processes.run
        def run(command, **options):
            graces.append(options.get("kill_after"))
            return real(command, **options)
        with patch.object(foreground, "problems", return_value=[]), patch.object(cli.processes, "run", side_effect=run):
            self.assertEqual(self.eng("run", "--here", "voiceover-mac")[0], 0)
            self.assertEqual(self.eng("run", "--here", "plain")[0], 0)
        self.assertEqual(graces, [processes.FOREGROUND_KILL_AFTER, processes.KILL_AFTER])

    def test_on_this_mac_a_foreground_lane_runs_here_only_within_the_owner_window(self):
        self.assertIn("name the session", self.eng("run", "--here", "voiceover-mac")[1])
        with patch.dict(os.environ, {devices.OWNER: "session-a"}):
            status, error = self.eng("run", "--here", "voiceover-mac")
        self.assertEqual(status, 2)
        self.assertIn(f"nobody holds {devices.FOREGROUND}", error)
        self.assertFalse(self.during.exists())
        later = lambda minutes: time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() + 60 * minutes))
        self.record.reserve(devices.FOREGROUND, owner="session-a", revision=0, purpose="approved VoiceOver",
                            release_by=later(30))
        with patch.dict(os.environ, {devices.OWNER: "session-a"}):
            self.assertEqual(self.eng("run", "--here", "voiceover-mac")[0], 0)
        self.assertEqual(json.loads(self.during.read_text())["owner"], "session-a")

    def test_on_the_second_mac_eng_holds_its_foreground_for_the_lane_and_for_admit(self):
        self.record.defer(SECOND)
        with patch.object(foreground, "problems", return_value=[]):
            status, error = self.eng("run", "--here", "voiceover-mac")
        self.assertEqual(status, 0, error)
        during = json.loads(self.during.read_text())
        self.assertEqual(during["entry"]["ownerChatID"], during["owner"])
        self.assertNotIn("approval", during["entry"])
        self.assertTrue(during["entry"]["activeRun"])
        self.assertEqual(self.record.entry(devices.FOREGROUND)["state"], "released")
        # eng admit --foreground holds it the same way; --verify never sends foreground work.
        self.during.unlink()
        with patch.object(foreground, "problems", return_value=[]):
            status, error = self.eng("admit", "--foreground", "--", *self.lanes["voiceover-mac"]["command"])
        self.assertEqual(status, 0, error)
        self.assertTrue(json.loads(self.during.read_text())["entry"]["activeRun"])
        self.assertIn("--verify never sends foreground work",
                      self.eng("admit", "--foreground", "--verify", "--", "true")[1])
        # A second Mac that is not ready stops the lane before it takes anything.
        self.during.unlink()
        with patch.object(foreground, "problems", return_value=[("the screen is locked", "unlock it")]):
            status, error = self.eng("run", "--here", "voiceover-mac")
        self.assertEqual(status, 2)
        self.assertIn("the screen is locked: unlock it", error)
        self.assertFalse(self.during.exists())


class ResultsTests(unittest.TestCase):
    def test_every_run_of_a_lane_begins_with_its_results_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            # The command finds its results/ in ENG_RESULTS_DIR, made for it.
            write = ("import os, pathlib, sys\nif sys.argv[1:] != ['skip']:\n"
                     "    folder = pathlib.Path(os.environ['ENG_RESULTS_DIR'])\n"
                     "    (folder / 'transcript.txt').write_text('VO')\nprint('Ran 1 tests')")
            (project / "engineering.json").write_text(json.dumps({"schemaVersion": 1, "name": "P", "lanes": {
                "note": {"command": [sys.executable, "-c", write]}}}))
            results = project / ".build/engineering/note/results"
            with patch.object(cli, "source_identity", return_value={}), contextlib.redirect_stdout(StringIO()), \
                 contextlib.redirect_stderr(StringIO()):
                self.assertEqual(cli.main(["--project", directory, "run", "note"]), 0)
                self.assertEqual((results / "transcript.txt").read_text(), "VO")
                # The earlier run's file is not this run's.
                self.assertEqual(cli.main(["--project", directory, "run", "note", "--", "skip"]), 0)
            self.assertEqual(list(results.iterdir()), [])


class ConfigurationTests(unittest.TestCase):
    def load(self, value):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "engineering.json").write_text(json.dumps({"schemaVersion": 1, "name": "P", **value}))
            return config.load(directory)

    def test_hosted_runner_configuration_is_refused_and_a_foreground_lane_is_admitted_work(self):
        for keys in [{"hostedRepositories": ["vocaro/P"]}, {"voiceover": {"workflows": ["voiceover-mac.yml"]}}]:
            with self.subTest(keys), self.assertRaisesRegex(ValueError, f"{next(iter(keys))}, which configured "
                                                                         "GitHub's hosted Mac runner"):
                self.load(keys)
        with self.assertRaisesRegex(ValueError, "foreground needs an admitted lane"):
            self.load({"lanes": {"ui": {"command": ["true"], "resource": "self", "foreground": True}}})
        # One that gives no resource, or light, is admitted as heavy work (the owner's decision, 2026-11-07).
        for resource in ({}, {"resource": "light"}):
            self.load({"lanes": {"ui": {"command": ["true"], "foreground": True, **resource}}})
        # The positive controls: a heavy foreground lane goes to the second Mac, and one that says why stays here.
        lanes = self.load({"lanes": {"ui": {"command": ["true"], "resource": "heavy", "foreground": True},
                                     "device": {"command": ["true"], "resource": "heavy", "foreground": True,
                                                "here": "the paired iPhone"}}})["lanes"]
        self.assertEqual([config.stays(lanes, name) for name in lanes], [None, "the lane says: the paired iPhone"])


if __name__ == "__main__":
    unittest.main()
