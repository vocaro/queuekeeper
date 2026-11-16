import contextlib
import contextvars
from datetime import datetime, timedelta, timezone
from io import StringIO
import json
import os
import re
import signal
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src/python"))
from queuekeeper import cli, config, offload_host, processes, remote, resources
from queuekeeper_apple import devices
from queuekeeper_mac_desktop import reservations
from queuekeeper_mac_desktop import foreground, opened_apps
import host_isolation
from test_support import farm as farm_fixture
from host_isolation import stretched
from test_support.devices import run_printed
from queuekeeper_apple.voiceover import state as voiceover_state


class Later(datetime):
    """Past a slot's expected release: EXPECTED, three hours, plus one."""

    @classmethod
    def now(cls, tz=None):
        return datetime.now(tz) + timedelta(seconds=foreground.EXPECTED + 3600)


SECOND = "Trevors-MacBook-Pro-715"


class SlotTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.record = devices.Record(self.root / "device-handoff.json")
        self.said = []
        for patcher in (patch.dict(os.environ, {devices.OVERRIDE: str(self.record.path)}),
                        patch.object(foreground, "front", return_value="Finder (ASN:0x0-0x1:)"),
                        # queuekeeper's guard always settles; short for offline fakes.
                        patch.object(voiceover_state, "SETTLE", 0.3)):
            patcher.start()
            self.addCleanup(patcher.stop)
        os.environ.pop(devices.OWNER, None)

    def farm_mac(self):
        self.record.defer(SECOND)

    def slot(self, **options):
        options.setdefault("ready", lambda: [])
        options.setdefault("voiceover", lambda: False)
        options.setdefault("said", self.said.append)
        return foreground.slot("Project voiceover-mac", timeout=options.pop("timeout", stretched(5)), record=self.record,
                               poll=0.05, **options)

    def test_only_the_farm_mac_takes_its_foreground_and_only_once_it_is_ready(self):
        # This Mac's foreground needs the owner's window, which the session reserves itself.
        with self.assertRaisesRegex(devices.Refused, "only on another Mac"):
            with self.slot(): self.fail("ran on this Mac")
        self.assertIsNone(self.record.entry(devices.FOREGROUND))
        self.farm_mac()
        lacking = [("a command started over SSH is not trusted for accessibility", "add sshd-keygen-wrapper")]
        with self.assertRaisesRegex(devices.Refused, "the owner's .*\n- a command started over SSH is not trusted "
                                                     "for accessibility: add sshd-keygen-wrapper"):
            with self.slot(ready=lambda: lacking): self.fail("ran on another Mac that is not ready")
        # Nothing was taken.
        self.assertIsNone(self.record.entry(devices.FOREGROUND))

    def test_a_run_holds_the_foreground_without_asking_and_releases_it(self):
        self.farm_mac()
        with self.slot() as owner:
            entry = self.record.entry(devices.FOREGROUND)
            self.assertEqual(os.environ[devices.OWNER], owner)
            # Drivers' own check passes for the run, as `devices check` does.
            self.assertEqual(devices.require_foreground(owner)["ownerChatID"], owner)
            self.assertEqual(devices.condition(entry), "active")
            self.assertEqual(entry["hostProcessIdentifier"], os.getpid())
            # Another Mac is not a workstation: its reservation records no approval (#207).
            self.assertNotIn("approval", entry)
            # A run there may outlast its expected release.
            with patch.object(devices, "datetime", Later), patch.object(reservations, "datetime", Later):
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
        self.farm_mac()
        with patch.object(voiceover_state, "_toggle") as toggle:
            with self.slot(voiceover=lambda: True):
                self.assertEqual(devices.condition(self.record.entry(devices.FOREGROUND)), "active")
            toggle.assert_not_called()
        entry = self.record.entry(devices.FOREGROUND)
        self.assertEqual((entry["state"], entry["run"]), ("released", "the run's work ended"))
        self.assertNotIn(devices.OWNER, os.environ)
        with self.slot(): pass

    def test_each_guard_holds_for_the_run_inside_the_lease_and_leaves_before_the_release(self):
        self.farm_mac()
        seen = []

        def guard(name):
            @contextlib.contextmanager
            def held(owner, *, record, said, **options):
                seen.append((f"enter {name}", devices.condition(record.entry(devices.FOREGROUND)), sorted(options)))
                try: yield
                finally: seen.append((f"leave {name}", devices.condition(record.entry(devices.FOREGROUND))))
            return held

        with patch.object(foreground, "GUARDS", [guard("one"), guard("two")]):
            with self.slot(): seen.append("run")
        self.assertEqual(seen, [("enter one", "active", ["voiceover"]), ("enter two", "active", ["voiceover"]), "run",
                                ("leave two", "active"), ("leave one", "active")])
        self.assertEqual(self.record.entry(devices.FOREGROUND)["state"], "released")

    def test_a_guard_that_could_not_restore_keeps_the_foreground_and_one_that_did_releases_it(self):
        self.farm_mac()

        class Failed(RuntimeError):
            def __init__(self, restored): self.restored = restored

        def guard(restored):
            @contextlib.contextmanager
            def held(owner, **_options):
                yield
                raise Failed(restored)
            return held

        with patch.object(foreground, "GUARDS", [guard(True)]), self.assertRaises(Failed):
            with self.slot(): pass
        self.assertEqual(self.record.entry(devices.FOREGROUND)["state"], "released")
        with patch.object(foreground, "GUARDS", [guard(False)]), self.assertRaises(Failed):
            with self.slot(): pass
        entry = self.record.entry(devices.FOREGROUND)
        self.assertNotEqual(entry["state"], "released")
        self.assertIn("queuekeeper: foreground retained: a guard's restoration or owned-process state is unverified", self.said)

    def test_the_apple_extension_s_guard_keeps_voiceover_s_state(self):
        from queuekeeper_apple import lane
        self.assertIn(lane.preserved_voiceover, foreground.GUARDS)

    def test_initial_unknown_is_untouched_and_released_but_final_unknown_is_retained(self):
        self.farm_mac()
        with self.assertRaises(voiceover_state.UnknownState):
            with self.slot(voiceover=lambda: None): self.fail("unknown baseline ran")
        self.assertEqual(self.record.entry(devices.FOREGROUND)["state"], "released")
        known = [False]
        with self.assertRaises(voiceover_state.TransitionFailed):
            with self.slot(voiceover=lambda: known[0]): known[0] = None
        self.assertEqual(devices.condition(self.record.entry(devices.FOREGROUND)), "uncertain")
        self.assertTrue(self.record.entry(devices.FOREGROUND)["activeRun"])


    def test_a_timed_out_toggle_whose_original_state_is_verified_releases_the_foreground(self):
        # #137: a driver's Command-F5 timed out on a loaded Mac, VoiceOver came on later, and the
        # foreground stayed uncertain for attended recovery. Now the guard turns it off again and releases.
        self.farm_mac()
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
        # The next run names the exact recovery command, which runs as printed on another Mac, where no import
        # path names the plugins (vocaro/queuekeeper#167).
        command = ("python3 ~/Development/queuekeeper/bin/qk --project ~/Development/queuekeeper devices recover "
                   f"mac-foreground --by SESSION --revision {entry['revision']} --verified WHAT")
        with self.assertRaisesRegex(devices.Refused, re.escape(f"`{command}`")) as refused:
            with self.slot(): self.fail("ran over an uncertain foreground")
        self.assertIn("or from another Mac the same with `--host`", str(refused.exception))
        ran = run_printed(str(refused.exception).partition("`")[2].partition("`")[0] + " --help")
        self.assertEqual(ran.returncode, 0, ran.stderr)
        self.assertIn("usage: qk devices recover", ran.stdout)

    def killed_child(self, toggle):
        """A run whose child driver sent Command-F5, timed out and was killed before its own restoration: the
        parent hears only an abnormal end. The key arrives late, on the third read after the run's first."""
        self.farm_mac()
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
        self.farm_mac()
        with self.assertRaisesRegex(RuntimeError, "driver failed"):
            with self.slot():
                raise RuntimeError("driver failed")
        entry = self.record.entry(devices.FOREGROUND)
        self.assertEqual((entry["state"], entry["run"]), ("released", "ended by an exception"))

    def test_a_session_that_leaves_voiceover_on_is_recovered_by_its_run_which_then_releases(self):
        # vocaro/EPUBLib#416, owner's decision 2026-11-12: a session whose own restoration could not be verified
        # leaves the lease uncertain; the run's runner sets the original back and its next session starts.
        self.farm_mac()
        enabled = [False]
        def toggle(): enabled[0] = not enabled[0]
        def cannot_toggle(): raise OSError("System Events refused the key")
        def session():
            # In a context of its own, as a session driver runs in its own process.
            with self.assertRaises(voiceover_state.TransitionFailed):
                with voiceover_state.preserved(None, record=self.record, read_state=lambda: enabled[0],
                                               toggle=cannot_toggle):
                    enabled[0] = True
        with patch.object(voiceover_state, "_ready_to_enable", return_value=True):
            with self.slot(voiceover=lambda: enabled[0]) as owner:
                entry = self.record.entry(devices.FOREGROUND)
                self.assertEqual(entry["voiceOverOriginal"], {"enabled": False, **{
                    name: entry[name] for name in ("reservedAt", "hostProcessIdentifier", "hostProcessStart")}})
                contextvars.Context().run(session)
                with self.assertRaisesRegex(devices.Refused, "uncertain"):
                    voiceover_state.read(owner, record=self.record)
                self.assertIs(voiceover_state.recover_restoration(record=self.record, read_state=lambda: enabled[0],
                                                                  toggle=toggle), False)
                self.assertIs(enabled[0], False)
                # The next session's driver passes the lease check it was refused at.
                self.assertEqual(devices.require_foreground(owner, self.record)["ownerChatID"], owner)
                self.assertEqual(devices.condition(self.record.entry(devices.FOREGROUND)), "active")
        entry = self.record.entry(devices.FOREGROUND)
        self.assertEqual((entry["state"], entry["run"]), ("released", "the run's work ended"))
        self.assertTrue(entry["voiceOverRestoration"]["verified"])
        self.assertEqual(entry["voiceOverRestoration"]["recoveredBy"], f"{owner} through process {os.getpid()}")

    def test_a_run_waits_while_another_holds_the_foreground_and_takes_it_once_released(self):
        self.farm_mac()
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

    def test_a_canceled_foreground_waiter_takes_nothing_and_leaves_the_holder_intact(self):
        self.farm_mac()
        held = self.record.reserve(devices.FOREGROUND, owner="other run", revision=0, purpose="Other run",
                                   release_by=(datetime.now().astimezone() + timedelta(hours=1)).isoformat())
        stopped = []
        def said(text):
            self.said.append(text)
            if "held by other run" in text: stopped.append(signal.SIGTERM)
        with patch.object(foreground, "time") as clock, \
                patch.object(processes, "requested", side_effect=lambda: stopped[0] if stopped else None), \
                patch.object(self.record, "reserve", wraps=self.record.reserve) as reserve, \
                patch.object(voiceover_state, "preserved") as preserve:
            # One held poll, then an expired deadline: cancellation must win on the next read.
            # No scheduler pause changes the order, and the old loop still reaches its timeout.
            clock.monotonic.return_value = 0
            def after_poll(_seconds):
                clock.monotonic.return_value = 1
            clock.sleep.side_effect = after_poll
            with self.assertRaisesRegex(processes.Interrupted, "SIGTERM.*waiting for.*foreground"):
                with self.slot(said=said, timeout=0.2): self.fail("canceled command started")
        reserve.assert_not_called()
        preserve.assert_not_called()
        self.assertEqual(self.record.entry(devices.FOREGROUND), held)
        self.assertNotIn(devices.OWNER, os.environ)

    def test_a_stop_seen_as_the_foreground_comes_free_does_not_reserve_it(self):
        self.farm_mac()
        # The stop arrives during the record read that reports a free foreground, before reserve.
        stopped = []
        def entry(_key):
            stopped.append(signal.SIGINT)
            return None
        with patch.object(self.record, "entry", side_effect=entry), \
                patch.object(processes, "requested", side_effect=lambda: stopped[0] if stopped else None), \
                patch.object(self.record, "reserve", side_effect=AssertionError("canceled waiter attempted reservation")) as reserve, \
                patch.object(voiceover_state, "preserved") as preserve:
            with self.assertRaisesRegex(processes.Interrupted, "SIGINT.*waiting for.*foreground"):
                with self.slot(): self.fail("canceled command started")
        reserve.assert_not_called()
        preserve.assert_not_called()

    def test_a_run_that_waits_too_long_or_finds_an_orphan_takes_nothing(self):
        self.farm_mac()
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
        with patch.object(reservations, "process_start", return_value="another start"):
            with self.assertRaisesRegex(devices.Refused, "orphan.*attended recovery"):
                with self.slot(): self.fail("ran over an orphan")
        self.assertEqual(self.record.entry(devices.FOREGROUND)["ownerChatID"], "other run")


class OpenedAppsTests(unittest.TestCase):
    """A foreground run quits the apps it opened before it releases the screen, and reports those it leaves
    (vocaro/queuekeeper#104). Every app here is a fake: the listing and the quit requests go to a stand-in for
    osascript, and whether a process still runs to a stand-in for os.kill, so no test quits an app on this Mac."""
    OWNERS = {"pid": 101, "name": "Mail", "bundle": "com.apple.mail", "path": "/System/Applications/Mail.app"}
    FINDER = {"pid": 102, "name": "Finder", "bundle": "com.apple.finder", "path": "/System/Library/CoreServices/Finder.app"}
    SAFARI = {"pid": 201, "name": "Safari", "bundle": "com.apple.Safari",
              "path": "/System/Volumes/Preboot/Cryptexes/App/System/Applications/Safari.app"}

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.checkout = self.root / "checkout"
        self.record = devices.Record(self.root / "device-handoff.json")
        self.record.defer(SECOND)
        self.said, self.asked, self.listings, self.running = [], [], [], set()
        environment = patch.dict(os.environ, {devices.OVERRIDE: str(self.record.path)})
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop(opened_apps.SWITCH, None)
        os.environ.pop(devices.OWNER, None)
        for patcher in (patch.object(foreground, "front", return_value="Finder (ASN:0x0-0x1:)"),
                        patch.object(opened_apps, "run", self.osascript),
                        patch.object(opened_apps, "alive", lambda pid: pid in self.running),
                        patch.object(opened_apps, "checkout", lambda directory=None: str(self.checkout)),
                        patch.object(opened_apps, "QUIT_WAIT", 0.3), patch.object(opened_apps, "STEP", 0.01),
                        patch.object(voiceover_state, "SETTLE", 0.3)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def osascript(self, command, environment=None):
        """The stand-in for osascript: the next listing, or a quit request, which ends the app unless it is stubborn."""
        self.assertEqual(command[:4], ["osascript", "-l", "JavaScript", "-e"])
        if command[-1] == opened_apps.LIST_SCRIPT:
            listed = self.listings.pop(0)
            if listed is None: return None
            self.running = {app["pid"] for app in listed}
            return json.dumps(listed)
        pid = int(re.search(r"WithProcessIdentifier\((\d+)\)", command[-1]).group(1))
        self.asked.append(pid)
        if pid not in self.stubborn: self.running.discard(pid)
        return "asked"

    stubborn = ()

    def slot(self, inside=None):
        with foreground.slot("Project voiceover-mac", timeout=stretched(5), record=self.record, poll=0.05,
                             ready=lambda: [], voiceover=lambda: False, said=self.said.append) as owner:
            if inside: inside(owner)
        return self.record.entry(devices.FOREGROUND)

    def test_a_run_quits_the_apps_it_opened_and_leaves_the_owners_and_others(self):
        built = {"pid": 202, "name": "Reader", "bundle": "com.example.Reader",
                 "path": str(self.checkout / ".build/Products/Debug/Reader.app")}
        host = {"pid": 203, "name": "Host", "bundle": "com.example.Host",
                "path": "/Users/me/Library/Developer/Xcode/DerivedData/Host-abc/Build/Products/Debug/Host.app"}
        hub = {"pid": 204, "name": "Device Hub", "bundle": "com.apple.dt.Devices",
               "path": "/Applications/Xcode.app/Contents/Applications/DeviceHub.app"}
        mail = {**self.OWNERS, "pid": 205}
        reporter = {"pid": 206, "name": "Problem Reporter", "bundle": "com.apple.ProblemReporter",
                    "path": "/System/Library/CoreServices/Problem Reporter.app"}
        self.listings = [[self.OWNERS, self.FINDER],
                         [self.OWNERS, self.FINDER, self.SAFARI, built, host, hub, mail, reporter]]
        entry = self.slot()
        # Safari, installed, and the app built in the run's own checkout are the run's: asked to quit, and gone.
        self.assertEqual(self.asked, [201, 202])
        self.assertIn("queuekeeper: quit Safari (com.apple.Safari, pid 201), which the run opened", self.said)
        self.assertIn("queuekeeper: quit Reader (com.example.Reader, pid 202), which the run opened", self.said)
        # The owner's apps, running before, are untouched; the others new since are reported and left.
        left = [line for line in self.said if line.startswith("queuekeeper: left running")]
        self.assertEqual(len(left), 4, self.said)
        self.assertIn("Host (com.example.Host, pid 203): it is neither installed", left[0])
        self.assertIn("such as a gate's test host", left[0])
        self.assertIn("Device Hub (com.apple.dt.Devices, pid 204): it is neither installed", left[1])
        self.assertIn("Mail (com.apple.mail, pid 205): a copy of it ran before the run took the foreground", left[2])
        self.assertIn("Problem Reporter (com.apple.ProblemReporter, pid 206): it is macOS's own", left[3])
        # The release records them, and released the foreground.
        self.assertEqual(entry["state"], "released")
        self.assertTrue(entry["appsQuit"].startswith("apps the run opened: quit Safari (com.apple.Safari, pid 201); "
                                                     "Reader (com.example.Reader, pid 202), and left running Host"),
                        entry["appsQuit"])
        self.assertIn(entry["appsQuit"], entry["restoration"])

    def test_the_quits_come_before_the_release_and_after_the_guards(self):
        order = []
        self.listings = [[self.FINDER], [self.FINDER, self.SAFARI]]
        def guard(owner, record=None, said=None, **options):
            @contextlib.contextmanager
            def held():
                yield
                order.append("guard left")
            return held()
        asking = self.osascript
        def osascript(command, environment=None):
            if command[-1] != opened_apps.LIST_SCRIPT:
                order.append(("asked", self.record.entry(devices.FOREGROUND)["state"]))
            return asking(command, environment)
        with patch.object(foreground, "GUARDS", [guard]), patch.object(opened_apps, "run", osascript):
            entry = self.slot()
        self.assertEqual(order, ["guard left", ("asked", "reserved")])
        self.assertEqual(entry["state"], "released")

    def test_an_app_that_will_not_quit_is_reported_and_never_forced(self):
        self.stubborn = (201,)
        self.listings = [[self.FINDER], [self.FINDER, self.SAFARI]]
        with patch.object(os, "kill") as kill:
            entry = self.slot()
        kill.assert_not_called()
        self.assertEqual(self.asked, [201])
        self.assertIn("Safari (com.apple.Safari, pid 201): it was asked to quit and still ran after 0.3s, perhaps "
                      "asking to save", entry["appsQuit"])
        self.assertTrue(any(line.startswith("queuekeeper: left running Safari") for line in self.said), self.said)
        self.assertEqual(entry["state"], "released")

    def test_a_bundle_the_session_recorded_as_launched_is_quit_wherever_it_was_built(self):
        host = {"pid": 203, "name": "Host", "bundle": "com.example.Host",
                "path": "/Users/me/Library/Developer/Xcode/DerivedData/Host-abc/Build/Products/Debug/Host.app"}
        self.listings = [[self.FINDER], [self.FINDER, host]]
        def launch(owner):
            current = self.record.entry(devices.FOREGROUND)
            self.record.update(devices.FOREGROUND, owner=owner, revision=current["revision"],
                               launched=["com.example.Host"])
        entry = self.slot(launch)
        self.assertEqual(self.asked, [203])
        self.assertEqual(entry["appsQuit"], "apps the run opened: quit Host (com.example.Host, pid 203)")
        # A launched bundle that no longer runs at the release still lets the release record how it went.
        self.listings = [[self.FINDER], [self.FINDER]]
        entry = self.slot(launch)
        self.assertEqual((entry["state"], entry["appsQuit"]),
                         ("released", "apps the run opened: none of com.example.Host ran at the release"))

    def test_nothing_new_quits_nothing_and_says_nothing(self):
        self.listings = [[self.OWNERS, self.FINDER], [self.OWNERS, self.FINDER]]
        entry = self.slot()
        self.assertEqual(self.asked, [])
        self.assertNotIn("appsQuit", entry)
        self.assertFalse([line for line in self.said if "quit" in line or "left running" in line], self.said)

    def test_apps_that_cannot_be_read_are_never_quit(self):
        for listings in ([None, [self.FINDER, self.SAFARI]], [[self.FINDER], None]):
            self.said.clear()
            self.listings = listings
            entry = self.slot()
            self.assertEqual(self.asked, [])
            self.assertEqual(entry["state"], "released")
            self.assertIn(f"could not read {offload_host.mac_name()}'s running apps", "\n".join(self.said))
            self.assertIn("the running apps could not be read", entry["appsQuit"])

    def test_a_run_that_no_longer_holds_the_lease_quits_nothing(self):
        self.listings = [[self.FINDER], [self.FINDER, self.SAFARI]]
        def lost(owner):
            # Attended recovery, then another run, took the foreground while this one still ran.
            record = self.record.load()
            record["devices"][devices.FOREGROUND]["ownerChatID"] = "other run"
            self.record.path.write_text(json.dumps(record))
        # The guard cannot restore VoiceOver under a lease that is no longer the run's, and says so.
        with self.assertRaises(voiceover_state.TransitionFailed):
            self.slot(lost)
        entry = self.record.entry(devices.FOREGROUND)
        self.assertEqual(self.asked, [])
        self.assertIn("queuekeeper: foreground retained: a guard's restoration or owned-process state is unverified",
                      self.said)
        self.assertEqual(entry["ownerChatID"], "other run")
        self.assertEqual(self.listings, [[self.FINDER, self.SAFARI]], "the release read no apps")

    def test_on_a_workstation_only_the_apps_the_run_is_shown_to_own_are_quit(self):
        # Any Mac but the second is the owner's workstation: he may open Safari or Mail while a run holds the screen.
        built = {"pid": 202, "name": "Reader", "bundle": "com.example.Reader",
                 "path": str(self.checkout / ".build/Products/Debug/Reader.app")}
        recorded = {"pid": 203, "name": "Host", "bundle": "com.example.Host",
                    "path": "/Users/me/Library/Developer/Xcode/DerivedData/Host-abc/Build/Products/Debug/Host.app"}
        mail = {**self.OWNERS, "pid": 207, "bundle": "com.apple.mail.other"}
        self.listings = [[self.FINDER], [self.FINDER, self.SAFARI, built, recorded, mail]]
        # The record stops deferring to another Mac's during the run: the release finds a workstation.
        workstation = patch.object(foreground, "farm_mac", side_effect=[True, False])
        def launch(owner):
            current = self.record.entry(devices.FOREGROUND)
            self.record.update(devices.FOREGROUND, owner=owner, revision=current["revision"],
                               launched=["com.example.Host"])
        with workstation:
            entry = self.slot(launch)
        self.assertEqual(self.asked, [202, 203])
        left = [line for line in self.said if line.startswith("queuekeeper: left running")]
        self.assertEqual(len(left), 2, self.said)
        self.assertIn("Safari (com.apple.Safari, pid 201): this Mac is a workstation", left[0])
        self.assertIn("Mail (com.apple.mail.other, pid 207): this Mac is a workstation", left[1])
        self.assertEqual(entry["state"], "released")
        self.assertIn("left running Safari (com.apple.Safari, pid 201): this Mac is a workstation", entry["appsQuit"])

    def test_the_farm_mac_quits_installed_apps_a_workstation_leaves(self):
        settled = {}
        for workstation in (False, True):
            self.asked, self.listings = [], [[self.FINDER, self.SAFARI]]
            settled[workstation] = opened_apps.settle([self.FINDER], str(self.checkout), said=self.said.append,
                                                      workstation=workstation)
        self.assertEqual(settled[False], {"quit": ["Safari (com.apple.Safari, pid 201)"], "left": []})
        self.assertEqual(settled[True], {"quit": [], "left": [
            f"Safari (com.apple.Safari, pid 201): {opened_apps.WORKSTATION}"]})
        self.assertEqual(self.asked, [])

    def test_the_suite_switch_reads_no_apps(self):
        os.environ[opened_apps.SWITCH] = "off"
        self.listings = []
        self.assertEqual(opened_apps.read(), [])

    def test_which_apps_count_as_installed(self):
        for path in ("/Applications/Safari.app", "/Applications/Utilities/Script Editor.app",
                     "/System/Applications/Mail.app", "/System/Applications/Utilities/Terminal.app",
                     "/System/Volumes/Preboot/Cryptexes/App/System/Applications/Safari.app",
                     str(Path.home() / "Applications/Tool.app")):
            self.assertTrue(opened_apps.installed(path), path)
        for path in ("/Applications/Xcode.app/Contents/Applications/DeviceHub.app", "/Applications/Folder/App.app",
                     "/tmp/Applications/App.app", "/Applications/tool"):
            self.assertFalse(opened_apps.installed(path), path)


class Clock:
    """A fake clock for foreground.slot's wait: its monotonic seconds and its wall time move only when the wait
    sleeps, and each sleep then calls the test's hook with the seconds waited so far. No test sleeps for real."""

    def __init__(self, hook=lambda seconds: None):
        self.seconds, self.hook, self.started = 0.0, hook, datetime.now().astimezone()
        clock = self

        class Now(datetime):
            @classmethod
            def now(cls, tz=None):
                return (clock.started + timedelta(seconds=clock.seconds)).astimezone(tz)
        self.datetime = Now

    def monotonic(self):
        return self.seconds

    def sleep(self, seconds):
        self.seconds += seconds
        self.hook(self.seconds)

    def at(self, seconds):
        """A wall time that many seconds after the wait began, as an entry records its expected release."""
        return (self.started + timedelta(seconds=seconds)).astimezone(timezone.utc).isoformat()


class DeadlineTests(unittest.TestCase):
    """A waiter whose deadline falls before the holder's expected release says so at once, looks for foreground.GRACE
    more in case the holder releases early, then ends with nothing run (vocaro/queuekeeper#95)."""
    TIMEOUT = 5400

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.record = devices.Record(Path(temp.name) / "device-handoff.json")
        self.said = []
        for patcher in (patch.dict(os.environ, {devices.OVERRIDE: str(self.record.path)}),
                        patch.object(foreground, "front", return_value="Finder (ASN:0x0-0x1:)")):
            patcher.start()
            self.addCleanup(patcher.stop)
        os.environ.pop(devices.OWNER, None)
        self.record.defer(SECOND)

    def hold(self, owner, release_by, revision=0):
        return self.record.reserve(devices.FOREGROUND, owner=owner, revision=revision, purpose=f"{owner}'s tests",
                                   release_by=release_by)

    def wait(self, clock):
        """Wait for the foreground on clock, as `qk run voiceover-mac` does: the TimeoutError that ends the wait, or
        the owner's name once the foreground is taken; and how many fake seconds it waited."""
        guarded = []
        guard = contextlib.contextmanager(lambda owner, **options: (yield guarded.append(owner)))
        with patch.object(foreground, "time", clock), patch.object(foreground, "datetime", clock.datetime), \
                patch.object(foreground, "GUARDS", [guard]):
            try:
                with foreground.slot("Project voiceover-mac", timeout=self.TIMEOUT, record=self.record,
                                     ready=lambda: [], said=self.said.append, again="qk run voiceover-mac") as owner:
                    self.assertEqual(guarded, [owner])
                    return owner, clock.seconds
            except TimeoutError as error:
                # A wait that ended took nothing, so it entered no guard.
                self.assertEqual(guarded, [])
                return error, clock.seconds

    def test_a_holder_released_after_the_deadline_is_named_at_once_and_the_wait_ends_after_the_grace(self):
        clock = Clock()
        # 2026-11-12: the holder's expected release was about 80 minutes after the waiter's deadline.
        due = clock.at(self.TIMEOUT + 80 * 60)
        held = self.hold("eng 21346", due)
        error, waited = self.wait(clock)
        self.assertIsInstance(error, TimeoutError)
        # The wait ended once the grace ran out, not at its 5,400 s deadline, and took nothing.
        self.assertEqual(waited, foreground.GRACE)
        self.assertEqual(self.record.entry(devices.FOREGROUND), held)
        # Its first line says so, with the release and the command to run after it.
        self.assertEqual(len(self.said), 1)
        self.assertIn(f"held by eng 21346 for eng 21346's tests, expected release {due}, after this wait's deadline, "
                      f"{clock.at(self.TIMEOUT)}: looking again for {foreground.GRACE}s", self.said[0])
        self.assertIn(f"run `qk run voiceover-mac` after {due}", self.said[0])
        self.assertIn(f"its expected release, {due}, falls after this wait's deadline", str(error))
        self.assertIn(f"Run `qk run voiceover-mac` after {due}", str(error))

    def test_a_late_holder_that_releases_within_the_grace_gives_the_foreground_to_the_waiter(self):
        held = self.hold("eng 21346", Clock().at(self.TIMEOUT + 3600))
        def release(seconds):
            if seconds == 60:
                self.record.release(devices.FOREGROUND, owner="eng 21346", revision=held["revision"], outcome="done",
                                    restoration="checked", frontmost="restored")
        owner, waited = self.wait(Clock(release))
        self.assertIsInstance(owner, str)
        self.assertEqual(waited, 60)

    def test_a_holder_released_within_the_deadline_or_with_no_release_keeps_the_whole_wait(self):
        def no_release(entry):
            self.record.update(devices.FOREGROUND, owner="eng 21346", revision=entry["revision"], expectedReleaseBy=None)
        for name, change in [("within the deadline", lambda entry: None), ("no expected release", no_release)]:
            with self.subTest(name):
                self.record.path.unlink()
                self.record.defer(SECOND)
                self.said.clear()
                clock = Clock()
                change(self.hold("eng 21346", clock.at(self.TIMEOUT - 60)))
                error, waited = self.wait(clock)
                self.assertIsInstance(error, TimeoutError)
                self.assertEqual(waited, self.TIMEOUT)
                self.assertEqual(str(error), f"{offload_host.mac_name()}'s foreground stayed held by eng 21346 for {self.TIMEOUT}s")
                self.assertEqual(len(self.said), 1)
                self.assertNotIn("deadline", self.said[0])

    def test_a_new_holder_is_judged_anew(self):
        clock = Clock()
        self.hold("eng 1", clock.at(self.TIMEOUT + 3600))
        swaps = {60: ("eng 2", clock.at(self.TIMEOUT - 600)), 1200: ("eng 3", clock.at(self.TIMEOUT + 7200))}
        def swap(seconds):
            if seconds not in swaps: return
            entry = self.record.entry(devices.FOREGROUND)
            entry = self.record.release(devices.FOREGROUND, owner=entry["ownerChatID"], revision=entry["revision"],
                                        outcome="done", restoration="checked", frontmost="restored")
            self.hold(*swaps[seconds], revision=entry["revision"])
        clock.hook = swap
        error, waited = self.wait(clock)
        # eng 1's late release started a grace that eng 2's in-time one ended; eng 3's late one started another,
        # from when it was first seen.
        self.assertIsInstance(error, TimeoutError)
        self.assertEqual(waited, 1200 + foreground.GRACE)
        self.assertIn("held by eng 3", str(error))
        self.assertEqual([[name in line for name in ("eng 1", "eng 2", "eng 3", "deadline")] for line in self.said],
                         [[True, False, False, True], [False, True, False, False], [False, False, True, True]])

    def test_a_wait_that_gives_up_is_busy_with_nothing_run_and_names_the_command_to_run_again(self):
        held = self.hold("eng 21346", Clock().at(self.TIMEOUT + 3600))
        clock = Clock()
        with patch.object(foreground, "time", clock), patch.object(foreground, "datetime", clock.datetime), \
                patch.object(foreground, "problems", return_value=[]), contextlib.ExitStack() as stack, \
                contextlib.redirect_stderr(StringIO()) as error:
            status = cli.extensions.FOREGROUND.foreground_held(stack, "voiceover-mac", "Project voiceover-mac",
                                                               self.TIMEOUT, again="qk run voiceover-mac")
        # The status a waiter that ran out of time ends with: 75, nothing ran, no verdict for the queue.
        self.assertEqual(status, resources.BUSY)
        self.assertEqual(status, 75)
        self.assertIn("queuekeeper: voiceover-mac not run on", error.getvalue())
        self.assertIn(f"Run `qk run voiceover-mac` after {held['expectedReleaseBy']}", error.getvalue())
        self.assertEqual(clock.seconds, foreground.GRACE)
        self.assertEqual(self.record.entry(devices.FOREGROUND), held)


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
                   {"mac": "Second", "problems": [], "entry": {"state": "reserved", "ownerChatID": "queuekeeper 7 on Second",
                                                                "purpose": "P lane", "expectedReleaseBy": "later"}}]
        for answer, status, words in zip(answers, [remote.MISCONFIGURED, 0],
                                         ["no run holds it\n- the screen is locked: unlock it",
                                          "queuekeeper 7 on Second holds it for P lane, expected release later\nReady"]):
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
                  "json.dump({'owner': os.environ.get('QUEUEKEEPER_DEVICE_OWNER'), 'entry': entry}, open(sys.argv[2], 'w'));"
                  " print('Ran 1 tests')")
        self.lanes = {"voiceover-mac": {"command": [sys.executable, "-c", script, str(self.record.path),
                                                    str(self.during)], "resource": "heavy", "foreground": True}}
        (self.project / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Project",
                                                                   "lanes": self.lanes}))
        for patcher in (patch.dict(os.environ, {devices.OVERRIDE: str(self.record.path),
                                                "HEAVY_SLOT_DIR": str(self.root / "slots"), "HEAVY_POLL_SECONDS": "0.01",
                                                "QUEUEKEEPER_GATE_LOCK_FILE": str(self.root / "gate")}),
                        patch.object(cli, "source_identity", return_value={}),
                        patch.object(cli, "refresh_policy", return_value=None),
                        patch.object(voiceover_state, "_read", return_value=False),
                        # queuekeeper's guard around a foreground run always settles, for a stretched SETTLE. VoiceOver is
                        # a fake here that never changes, so no settle can show a late key: the real 5 s, stretched
                        # by load, held each run 40 to 120 s on a loaded host, and 0.3 s still 7 s at the stretch cap.
                        patch.object(voiceover_state, "SETTLE", 0.05),
                        patch.object(foreground, "front", return_value=None)):
            patcher.start()
            self.addCleanup(patcher.stop)
        os.environ.pop(devices.OWNER, None)

    def qk(self, *arguments):
        with contextlib.redirect_stderr(StringIO()) as error, contextlib.redirect_stdout(StringIO()):
            return cli.main(["--project", str(self.project), *arguments]), error.getvalue()

    def test_a_foreground_lane_goes_to_the_farm_mac_and_never_falls_back_to_this_mac(self):
        sent = []
        with patch.object(remote, "foreground_lane", side_effect=lambda *a: sent.append(a) or 0):
            self.assertEqual(self.qk("run", "voiceover-mac", "--", "steps=x")[0], 0)
            self.assertEqual(self.qk("run", "--offload-only", "voiceover-mac")[0], 0)
        self.assertEqual([a[2:4] for a in sent], [("voiceover-mac", ["steps=x"]), ("voiceover-mac", [])])
        # With no other Mac to send it to, it stops and says how this Mac's foreground is used instead.
        status, error = self.qk("run", "voiceover-mac")
        self.assertEqual(status, 2)
        self.assertIn("the farm has no Mac that takes work now", error)
        self.assertIn("window the owner agrees first", error)
        self.assertFalse(self.during.exists())

    def test_an_unreachable_farm_mac_offers_a_window_here_for_foreground_work_alone(self):
        self.lanes["plain"] = {"command": [sys.executable, "-c", "print('Ran 1 tests')"], "resource": "heavy"}
        (self.project / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Project",
                                                                   "lanes": self.lanes}))
        away = remote.Unreachable("build-host cannot be reached: ssh: connect to host mac.local port 22: Host is down",
                                  "wake build-host and connect it to the network")
        with farm_fixture.only("build-host"), \
                patch.object(remote, "execute", side_effect=away):
            ran = {"lane": self.qk("run", "voiceover-mac"), "remote lane": self.qk("remote", "run", "voiceover-mac"),
                   "admit": self.qk("remote", "admit", "--foreground", "--", "true")}
            plain = {"lane": self.qk("remote", "run", "plain"), "admit": self.qk("remote", "admit", "--", "true")}
        for name, here in (("lane", "qk run --here voiceover-mac"), ("remote lane", "qk run --here voiceover-mac"),
                           ("admit", "qk admit --foreground -- COMMAND")):
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

    def test_a_misconfigured_farm_mac_offers_no_window_here(self):
        stop = remote.Misconfigured("build-host could not run the toolkit", "give it a python3")
        with farm_fixture.only("build-host"), \
                patch.object(remote, "execute", side_effect=stop):
            status, error = self.qk("remote", "admit", "--foreground", "--", "true")
        self.assertEqual(status, 2)
        self.assertIn("could not run the toolkit", error)
        self.assertNotIn("window", error)

    def test_a_foreground_lane_gets_the_longer_grace_before_sigkill_and_another_lane_does_not(self):
        from queuekeeper import processes
        self.record.defer(SECOND)
        self.lanes["plain"] = {"command": [sys.executable, "-c", "print('Ran 1 tests')"], "resource": "heavy"}
        (self.project / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Project",
                                                                   "lanes": self.lanes}))
        graces = []
        real = processes.run
        def run(command, **options):
            graces.append(options.get("kill_after"))
            return real(command, **options)
        with patch.object(foreground, "problems", return_value=[]), patch.object(cli.processes, "run", side_effect=run):
            self.assertEqual(self.qk("run", "--here", "voiceover-mac")[0], 0)
            self.assertEqual(self.qk("run", "--here", "plain")[0], 0)
        self.assertEqual(graces, [processes.FOREGROUND_KILL_AFTER, processes.KILL_AFTER])

    def test_on_this_mac_a_foreground_lane_runs_here_only_within_the_owner_window(self):
        self.assertIn("name the session", self.qk("run", "--here", "voiceover-mac")[1])
        with patch.dict(os.environ, {devices.OWNER: "session-a"}):
            status, error = self.qk("run", "--here", "voiceover-mac")
        self.assertEqual(status, 2)
        self.assertIn(f"nobody holds {devices.FOREGROUND}", error)
        self.assertFalse(self.during.exists())
        later = lambda minutes: time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() + 60 * minutes))
        self.record.reserve(devices.FOREGROUND, owner="session-a", revision=0, purpose="approved VoiceOver",
                            release_by=later(30))
        with patch.dict(os.environ, {devices.OWNER: "session-a"}):
            self.assertEqual(self.qk("run", "--here", "voiceover-mac")[0], 0)
        self.assertEqual(json.loads(self.during.read_text())["owner"], "session-a")

    def test_on_the_farm_mac_eng_holds_its_foreground_for_the_lane_and_for_admit(self):
        self.record.defer(SECOND)
        with patch.object(foreground, "problems", return_value=[]):
            status, error = self.qk("run", "--here", "voiceover-mac")
        self.assertEqual(status, 0, error)
        during = json.loads(self.during.read_text())
        self.assertEqual(during["entry"]["ownerChatID"], during["owner"])
        self.assertNotIn("approval", during["entry"])
        self.assertTrue(during["entry"]["activeRun"])
        self.assertEqual(self.record.entry(devices.FOREGROUND)["state"], "released")
        # qk admit --foreground holds it the same way; --verify never sends foreground work.
        self.during.unlink()
        with patch.object(foreground, "problems", return_value=[]):
            status, error = self.qk("admit", "--foreground", "--", *self.lanes["voiceover-mac"]["command"])
        self.assertEqual(status, 0, error)
        self.assertTrue(json.loads(self.during.read_text())["entry"]["activeRun"])
        self.assertIn("--verify never sends foreground work",
                      self.qk("admit", "--foreground", "--verify", "--", "true")[1])
        # Another Mac that is not ready stops the lane before it takes anything.
        self.during.unlink()
        with patch.object(foreground, "problems", return_value=[("the screen is locked", "unlock it")]):
            status, error = self.qk("run", "--here", "voiceover-mac")
        self.assertEqual(status, 2)
        self.assertIn("the screen is locked: unlock it", error)
        self.assertFalse(self.during.exists())


class ResultsTests(unittest.TestCase):
    def test_every_run_of_a_lane_begins_with_its_results_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            # The command finds its results/ in QUEUEKEEPER_RESULTS_DIR, made for it.
            write = ("import os, pathlib, sys\nif sys.argv[1:] != ['skip']:\n"
                     "    folder = pathlib.Path(os.environ['QUEUEKEEPER_RESULTS_DIR'])\n"
                     "    (folder / 'transcript.txt').write_text('VO')\nprint('Ran 1 tests')")
            (project / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "P", "lanes": {
                "note": {"command": [sys.executable, "-c", write]}}}))
            results = project / ".build/queuekeeper/note/results"
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
            (Path(directory) / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "P", **value}))
            return config.load(directory)

    def test_a_foreground_lane_is_admitted_work(self):
        with self.assertRaisesRegex(ValueError, "foreground needs an admitted lane"):
            self.load({"lanes": {"ui": {"command": ["true"], "resource": "self", "foreground": True}}})
        # One that gives no resource, or light, is admitted as heavy work (the owner's decision, 2026-11-07).
        for resource in ({}, {"resource": "light"}):
            self.load({"lanes": {"ui": {"command": ["true"], "foreground": True, **resource}}})
        # The positive controls: a heavy foreground lane goes to another Mac, and one that says why stays here.
        lanes = self.load({"lanes": {"ui": {"command": ["true"], "resource": "heavy", "foreground": True},
                                     "device": {"command": ["true"], "resource": "heavy", "foreground": True,
                                                "here": "the paired iPhone"}}})["lanes"]
        self.assertEqual([config.stays(lanes, name) for name in lanes], [None, "the lane says: the paired iPhone"])


if __name__ == "__main__":
    unittest.main()
