"""What Queuekeeper puts right in the device record by itself: a run proven gone (vocaro/queuekeeper#230) and the
owner's notifications left silenced past a held device's window (vocaro/queuekeeper#174)."""
import contextlib
from datetime import datetime, timedelta, timezone
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

import host_isolation  # noqa: F401  (first: keeps every test out of the host's admission)
from host_isolation import stretched
from queuekeeper_apple import device_notifications
from queuekeeper_mac_desktop import owner_notice
from queuekeeper_mobile import device_pass, devices, doctor, upkeep
from queuekeeper.resources import process_start
from test_support.devices import PHONE, TABLET, Devicectl, Watches, alive, later, live, scratch

SILENCE = "shortcuts://run-shortcut?name=Vocaro%20Silence%20Notifications"
RESTORE = "shortcuts://run-shortcut?name=Vocaro%20Restore%20Notifications"


class Banners:
    """owner_notice.run as a banner reaches it: each banner's title and body, posted."""

    def __init__(self, status=0):
        self.posted, self.status = [], status

    def __call__(self, command):
        self.posted.append((command[-2], command[-1]))
        return self.status, ""


class Fixture(unittest.TestCase):
    def setUp(self):
        temp = scratch()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = self.root / "device-handoff.json"
        self.record = devices.Record(self.path)
        self.device = Devicectl()
        self.banners = Banners()
        for patcher in (patch.dict(os.environ, {devices.OVERRIDE: str(self.path)}),
                        patch.object(device_notifications, "run", self.device),
                        patch.object(device_notifications, "watch", Watches()),
                        patch.object(owner_notice, "run", self.banners)):
            patcher.start()
            self.addCleanup(patcher.stop)
        for name in (devices.OWNER, devices.LEASE, "CODEX_THREAD_ID", "CLAUDE_CODE_SESSION_ID", upkeep.GRACE_VARIABLE,
                     "QUEUEKEEPER_RESULTS_DIR"):
            os.environ.pop(name, None)
        # Each paired device's first reservation records its device, and the owner's shortcuts are on both.
        for key, device in (("iPad", TABLET), ("iPhone", PHONE)):
            entry = self.record.reserve(key, owner="first", revision=0, purpose="p", release_by=later(), device=device)
            self.record.release(key, owner="first", revision=entry["revision"], outcome="o", restoration="r")
            self.record.notification_setup(key, by="owner", at=later(-1))

    def cli(self, *arguments):
        with contextlib.redirect_stdout(StringIO()) as output, contextlib.redirect_stderr(StringIO()) as error:
            code = devices.main(list(arguments))
        return code, output.getvalue(), error.getvalue()

    def take(self, key="iPhone", owner="session-a", minutes=30, silenced=True):
        """key taken by owner until minutes from now, its notifications silenced: a restore is pending."""
        entry = self.record.take(key, owner=owner, purpose="VoiceOver run", release_by=later(minutes), timeout=1,
                                 said=lambda _text: None)
        return devices.silence_notifications(self.record, key, owner=owner, revision=entry["revision"],
                                             said=lambda _text: None) if silenced else entry

    def orphan(self, key="iPhone", silenced=True, **changes):
        """key's run marked active by a process that has since ended, as a run killed outright leaves it."""
        entry = self.take(key, silenced=silenced)
        gone = live()
        entry = self.record.update(key, owner="session-a", revision=entry["revision"], activeRun=True,
                                   hostProcessIdentifier=gone.pid,
                                   run={"startedAt": devices.now(), "processes": [], "command": ["xcodebuild"]},
                                   **changes)
        gone.kill()
        gone.wait()
        self.assertEqual(devices.condition(entry), "orphan")
        return entry

    def shortcuts(self):
        return [url for _device, url, _app in self.device.shortcuts()]

    def backdate(self, key, minutes):
        """Move key's expected release minutes into the past, as the clock passing it would."""
        value = json.loads(self.path.read_text())
        value["devices"][key]["expectedReleaseBy"] = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
        self.path.write_text(json.dumps(value))
        return value["devices"][key]


class RunRecovery(Fixture):
    """vocaro/queuekeeper#230: an entry whose run's process is proven gone is recovered by itself."""

    def test_a_run_whose_process_ended_is_ended_its_notifications_put_back_and_the_reservation_kept(self):
        self.orphan()
        said = upkeep.upkeep(self.record)
        entry = self.record.entry("iPhone")
        self.assertEqual((devices.condition(entry), entry["ownerChatID"], entry["activeRun"]),
                         ("held", "session-a", False), said)
        self.assertEqual(entry["run"]["outcome"], "its process ended without recording the run's end")
        self.assertEqual((entry["notifications"]["restore"], entry["notifications"]["silence"]), ("done", "put back"))
        self.assertEqual(self.shortcuts(), [SILENCE, RESTORE])
        recovery = entry[upkeep.RECOVERIES][-1]
        self.assertEqual(recovery["by"], "queuekeeper")
        self.assertRegex(recovery["proof"], r"its process \d+ has ended")
        self.assertIn("put the owner's notifications back", recovery["steps"])
        self.assertIn("recovered iPhone's run by itself", "\n".join(said))
        # Nothing is due any more, and the holder releases it as any held entry.
        self.assertEqual(upkeep.due(self.record.load()), [])
        released = self.record.release("iPhone", owner="session-a", revision=entry["revision"], outcome="o",
                                       restoration="r", apps_quit="Shortcuts, opened by queuekeeper")
        self.assertEqual(released["state"], "released")

    def test_a_live_run_is_not_recovered(self):
        entry = self.take()
        child = live()
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        entry = self.record.update("iPhone", owner="session-a", revision=entry["revision"], activeRun=True,
                                   hostProcessIdentifier=child.pid, run={"startedAt": devices.now(), "processes": []})
        before = self.path.read_bytes()
        self.assertEqual(upkeep.due(self.record.load()), [])
        self.assertEqual(upkeep.upkeep(self.record), [])
        self.assertIsNone(upkeep.recover_run(self.record, "iPhone", lambda _text: None))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(devices.condition(self.record.entry("iPhone")), "active")
        self.assertEqual(self.shortcuts(), [SILENCE])

    def test_a_pid_that_names_another_process_now_is_proven_gone(self):
        child = live()
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        entry = self.take()
        entry = self.record.update("iPhone", owner="session-a", revision=entry["revision"], activeRun=True,
                                   hostProcessIdentifier=child.pid, run={"startedAt": devices.now(), "processes": []})
        # The PID is alive, but with another start than the run's process had: the PID was used again.
        value = json.loads(self.path.read_text())
        value["devices"]["iPhone"]["hostProcessStart"] = "Mon Jan  1 00:00:00 2024"
        self.path.write_text(json.dumps(value))
        self.assertEqual(upkeep.due(self.record.load()), [("iPhone", "run")])
        upkeep.upkeep(self.record)
        entry = self.record.entry("iPhone")
        self.assertEqual((devices.condition(entry), entry["activeRun"]), ("held", False))
        self.assertIn(f"process {child.pid} is another process now", entry[upkeep.RECOVERIES][-1]["proof"])
        # The process that has the PID now is no business of the run's, and runs on.
        self.assertTrue(alive(child.pid))

    def test_a_pid_alive_with_no_start_recorded_is_left_for_attended_recovery(self):
        child = live()
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        entry = self.take()
        entry = self.record.update("iPhone", owner="session-a", revision=entry["revision"], activeRun=True,
                                   hostProcessIdentifier=child.pid)
        value = json.loads(self.path.read_text())
        value["devices"]["iPhone"].pop("hostProcessStart")
        self.path.write_text(json.dumps(value))
        self.assertEqual(devices.condition(self.record.entry("iPhone")), "orphan")
        self.assertEqual(upkeep.due(self.record.load()), [])
        self.assertIsNone(upkeep.recover_run(self.record, "iPhone", lambda _text: None))
        code, output, _error = self.cli("status")
        self.assertIn("not recovered by itself: its run's process", output)
        with self.assertRaisesRegex(devices.Refused, "orphan.*not proven gone.*attended recovery"):
            self.record.take("iPhone", owner="session-b", purpose="p", release_by=later(), timeout=stretched(2),
                             poll=0.01, said=lambda _text: None)
        self.assertEqual(self.shortcuts(), [SILENCE])

    def test_the_command_a_run_recorded_is_stopped_before_its_run_is_ended(self):
        command = subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({stretched(60)})"],
                                   start_new_session=True)
        self.addCleanup(command.wait)
        self.addCleanup(command.kill)
        # Reaped as it ends, as launchd reaps a command whose run was killed: a zombie keeps its start.
        threading.Thread(target=command.wait, daemon=True).start()
        entry = self.orphan(own=[command.pid])
        entry = self.record.entry("iPhone")
        value = json.loads(self.path.read_text())
        value["devices"]["iPhone"]["run"]["processes"] = [command.pid]
        self.path.write_text(json.dumps(value))
        upkeep.upkeep(self.record)
        command.wait(timeout=stretched(20))
        entry = self.record.entry("iPhone")
        self.assertEqual((devices.condition(entry), entry["activeRun"]), ("held", False))
        self.assertIn(f"stopped its process {command.pid}", entry[upkeep.RECOVERIES][-1]["steps"])

    def test_a_process_the_entry_records_that_the_run_does_not_name_leaves_it_to_its_owner(self):
        session = live()
        self.addCleanup(session.wait)
        self.addCleanup(session.kill)
        self.orphan(own=[session.pid])
        # An entry from before runs named their processes: whose the live one is cannot be told.
        value = json.loads(self.path.read_text())
        value["devices"]["iPhone"]["run"].pop("processes")
        self.path.write_text(json.dumps(value))
        said = upkeep.upkeep(self.record)
        entry = self.record.entry("iPhone")
        self.assertEqual(devices.condition(entry), "orphan")
        self.assertEqual(entry[upkeep.FALLBACK]["step"], "stopping its processes")
        self.assertIn("qk devices recover iPhone --by SESSION", "\n".join(said))
        self.assertTrue(alive(session.pid))
        self.assertEqual(self.shortcuts(), [SILENCE])
        # It is not tried again, and the attended recovery it names finishes it.
        self.assertEqual(upkeep.due(self.record.load()), [])
        self.assertIn("its automatic recovery stopped at stopping its processes", self.cli("status")[1])
        session.kill()
        session.wait()
        code, output, error = self.cli("recover", "iPhone", "--by", "owner", "--revision", str(entry["revision"]),
                                       "--verified", "nothing of the run's still runs")
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)["entry"]["state"], "released")

    def test_a_failed_restore_still_ends_the_run_and_leaves_the_restore_pending(self):
        self.orphan()
        self.device.answers.append((1, "ERROR: The device is locked"))
        said = upkeep.upkeep(self.record)
        entry = self.record.entry("iPhone")
        self.assertEqual((devices.condition(entry), entry["notifications"]["restore"]), ("held", "pending"), said)
        self.assertIn("The device is locked", entry["notifications"]["lastRestoreFailure"])
        self.assertIn("the restore stays pending", "\n".join(entry[upkeep.RECOVERIES][-1]["steps"]))
        # The holder's release runs the restoring shortcut again.
        code, output, error = self.cli("release", "iPhone", "--owner", "session-a", "--revision",
                                       str(entry["revision"]), "--outcome", "o", "--restoration", "r",
                                       "--apps-quit", "Shortcuts")
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)["entry"]["notifications"]["restore"], "done")

    def test_a_run_sent_to_another_mac_is_left_for_attended_recovery(self):
        self.orphan()
        value = json.loads(self.path.read_text())
        value["devices"]["iPhone"]["run"]["host"] = "build-host"
        self.path.write_text(json.dumps(value))
        self.assertEqual(upkeep.due(self.record.load()), [])
        self.assertIn("went to build-host", upkeep.unproven("iPhone", self.record.entry("iPhone")))

    def test_its_holder_releases_an_orphaned_entry_at_the_revision_it_read(self):
        entry = self.orphan()
        code, output, error = self.cli("release", "iPhone", "--owner", "session-a", "--revision",
                                       str(entry["revision"]), "--outcome", "o", "--restoration", "r",
                                       "--apps-quit", "Shortcuts")
        self.assertEqual(code, 0, error)
        released = json.loads(output)["entry"]
        self.assertEqual((released["state"], released["notifications"]["restore"]), ("released", "done"))
        self.assertEqual(released[upkeep.RECOVERIES][-1]["by"], "queuekeeper")

    def test_another_session_waiting_to_take_it_waits_for_the_holder_rather_than_being_refused(self):
        self.orphan()
        with self.assertRaisesRegex(TimeoutError, "held or claimed by session-a"):
            self.record.take("iPhone", owner="session-b", purpose="p", release_by=later(), timeout=stretched(2),
                             poll=0.01, said=lambda _text: None)
        self.assertEqual(devices.condition(self.record.entry("iPhone")), "held")


class NextPass(Fixture):
    """The next pass on a held iPhone after a run killed outright: it recovers the run and runs (#230)."""

    def run_killed_outright(self, entry):
        """`qk devices run iPhone` in its own process, whose command kills that process with SIGKILL and runs on: the
        run's end is never recorded, and its command outlives it."""
        script = ("import sys; from queuekeeper_apple import devices; "
                  "sys.exit(devices.main(['run', 'iPhone', '--owner', 'session-a', '--no-tests', '--', 'sh', '-c', "
                  f"'kill -9 $PPID; sleep {stretched(60)}']))")
        # To a file, not a pipe: the command that outlives the run keeps its output open.
        log = self.root / "run.log"
        with patch.dict(os.environ, {"PYTHONPATH": host_isolation.pythonpath()}), log.open("w") as output:
            done = subprocess.run([sys.executable, "-c", script], stdin=subprocess.DEVNULL, stdout=output,
                                  stderr=subprocess.STDOUT, timeout=stretched(60))
        self.assertEqual(done.returncode, -9, log.read_text())
        entry = self.record.entry("iPhone")
        self.addCleanup(lambda: [os.kill(pid, 9) for pid in entry["run"]["processes"] if alive(pid)])
        return entry

    def test_the_next_pass_ends_the_killed_run_stops_its_command_and_runs_silenced(self):
        entry = self.take()
        entry = self.run_killed_outright(entry)
        self.assertEqual(devices.condition(entry), "orphan")
        command = entry["run"]["processes"][0]
        self.assertTrue(alive(command))
        said = []
        probe = [sys.executable, "-c", "print('Executed 1 test, with 0 failures')"]
        parts, code = device_pass.run(["iPhone"], probe, owner="session-a", wait=0, record=self.record,
                                      said=said.append)
        self.assertEqual((code, parts[0].outcome, parts[0].took), (0, "passed", False), "\n".join(said))
        deadline = time.monotonic() + stretched(10)
        while alive(command) and time.monotonic() < deadline: time.sleep(0.1)
        self.assertIsNone(process_start(command))
        entry = self.record.entry("iPhone")
        self.assertEqual((devices.condition(entry), entry["ownerChatID"]), ("held", "session-a"))
        self.assertIn(f"stopped its process {command}", entry[upkeep.RECOVERIES][-1]["steps"])
        # Its notifications were put back with the recovery, and silenced again for this pass.
        self.assertEqual(self.shortcuts(), [SILENCE, RESTORE, SILENCE])
        self.assertEqual(entry["notifications"]["restore"], "pending")


class OverdueSilencing(Fixture):
    """vocaro/queuekeeper#174: notifications left silenced GRACE past a held device's expected release are put
    back by Queuekeeper, which tells the owner and keeps the reservation held."""

    def setUp(self):
        super().setUp()
        patcher = patch.dict(os.environ, {owner_notice.SWITCH: ""})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_past_the_grace_they_are_put_back_recorded_and_the_owner_told(self):
        self.take()
        self.backdate("iPhone", 20)
        self.assertEqual(upkeep.due(self.record.load()), [("iPhone", "overdue")])
        with contextlib.redirect_stdout(StringIO()) as output:
            devices.status(self.record, macs=[])
        entry = self.record.entry("iPhone")
        self.assertEqual((devices.condition(entry), entry["ownerChatID"]), ("held", "session-a"))
        notifications = entry["notifications"]
        self.assertEqual((notifications["restore"], notifications["silence"]), ("done", "put back"))
        self.assertEqual(notifications["putBack"]["by"], "queuekeeper")
        self.assertEqual(notifications["putBack"]["told"], "a banner on this Mac")
        self.assertEqual(self.shortcuts(), [SILENCE, RESTORE])
        [(title, body)] = self.banners.posted
        self.assertEqual(title, "Queuekeeper put back your notifications on the iPhone")
        self.assertIn("session-a still holds the iPhone", body)
        self.assertIn("Put right: put back the owner's notifications on iPhone", output.getvalue())
        self.assertNotIn("Overdue silencing", output.getvalue())
        # Done once: nothing is due, and a run on it says it is not silenced.
        self.assertEqual(upkeep.due(self.record.load()), [])
        self.assertIn("notifications: not silenced (Queuekeeper put them back", self.cli("status")[1])

    def test_within_the_grace_nothing_runs_and_doctor_says_when_it_will(self):
        self.take()
        self.backdate("iPhone", 5)
        before = self.path.read_bytes()
        self.assertEqual(upkeep.due(self.record.load()), [])
        warned = doctor.overdue_silences(None, {}, self.record)
        self.assertEqual(len(warned), 1, warned)
        self.assertIn("Queuekeeper puts them back by itself at", warned[0])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.shortcuts(), [SILENCE])
        self.assertEqual(self.banners.posted, [])

    def test_a_device_not_past_its_release_and_one_released_are_left(self):
        self.take("iPhone")
        entry = self.take("iPad")
        code, _output, error = self.cli("release", "iPad", "--owner", "session-a", "--revision",
                                        str(entry["revision"]), "--outcome", "o", "--restoration", "r")
        self.assertEqual(code, 0, error)
        self.backdate("iPad", 60)
        self.assertEqual(upkeep.upkeep(self.record), [])
        self.assertEqual(self.shortcuts(), [SILENCE, SILENCE, RESTORE])

    def test_a_shorter_grace_is_read_from_its_variable(self):
        self.take()
        self.backdate("iPhone", 2)
        self.assertEqual(upkeep.due(self.record.load()), [])
        with patch.dict(os.environ, {upkeep.GRACE_VARIABLE: "60"}):
            warned = doctor.overdue_silences(None, {}, self.record)
        self.assertIn("put right by itself: put back the owner's notifications on iPhone", "\n".join(warned))
        self.assertEqual(self.record.entry("iPhone")["notifications"]["restore"], "done")

    def test_a_restore_that_fails_stays_pending_is_told_once_and_tried_again_later(self):
        self.take()
        self.backdate("iPhone", 20)
        self.device.answers.append((1, "ERROR: The device is locked"))
        upkeep.upkeep(self.record)
        notifications = self.record.entry("iPhone")["notifications"]
        self.assertEqual(notifications["restore"], "pending")
        self.assertIn("The device is locked", notifications["lastRestoreFailure"])
        [(title, _body)] = self.banners.posted
        self.assertEqual(title, "Queuekeeper could not put back your notifications on the iPhone")
        self.assertEqual(upkeep.due(self.record.load()), [])
        self.assertIn("could not put them back by itself", devices.overdue_silences(self.record.load()["devices"])[0])
        later_on = datetime.now(timezone.utc) + timedelta(seconds=upkeep.RETRY + 1)
        self.assertEqual(upkeep.due(self.record.load(), later_on), [("iPhone", "overdue")])
        upkeep.upkeep(self.record, at=later_on)
        self.assertEqual(self.record.entry("iPhone")["notifications"]["restore"], "done")
        self.assertEqual(self.shortcuts(), [SILENCE, RESTORE, RESTORE])
        # The failure was told once; the success is told too.
        self.assertEqual([title for title, _body in self.banners.posted],
                         ["Queuekeeper could not put back your notifications on the iPhone",
                          "Queuekeeper put back your notifications on the iPhone"])

    def test_with_the_notice_switched_off_no_banner_is_posted_and_the_record_says_so(self):
        self.take()
        self.backdate("iPhone", 20)
        with patch.dict(os.environ, {owner_notice.SWITCH: "off"}):
            upkeep.upkeep(self.record)
        self.assertEqual(self.banners.posted, [])
        self.assertIn("is off", self.record.entry("iPhone")["notifications"]["putBack"]["told"])


class Spawn(Fixture):
    """A command that reaches the hosts starts an upkeep apart from itself only when one is due."""

    def setUp(self):
        super().setUp()
        self.started = []

    def spawn(self, switch=""):
        """upkeep.spawn, with each process it starts recorded and replaced by one that does nothing."""
        real = subprocess.Popen

        def started(command, **options):
            # Every other process, such as ps reading a run's process, starts as it would.
            if command[1:] != ["-m", "queuekeeper_mobile.upkeep"]: return real(command, **options)
            self.started.append(command)
            return real(["true"])
        with patch.dict(os.environ, {upkeep.SWITCH: switch}), patch.object(upkeep.subprocess, "Popen", started):
            upkeep.spawn()

    def test_nothing_due_starts_nothing(self):
        self.take()
        self.spawn()
        self.assertEqual(self.started, [])

    def test_something_due_starts_one_upkeep_a_minute(self):
        self.orphan(silenced=False)
        self.spawn()
        self.spawn()
        self.assertEqual(len(self.started), 1)
        self.assertEqual(self.started[0][1:], ["-m", "queuekeeper_mobile.upkeep"])

    def test_switched_off_it_starts_nothing(self):
        self.orphan(silenced=False)
        self.spawn("off")
        self.assertEqual(self.started, [])

    def test_the_upkeep_it_starts_recovers_the_run(self):
        # Unsilenced, so that the upkeep in its own process runs no notification shortcut, which only this one fakes.
        self.orphan(silenced=False)
        environment = {**os.environ, "PYTHONPATH": host_isolation.pythonpath()}
        done = subprocess.run([sys.executable, "-m", "queuekeeper_mobile.upkeep"], env=environment,
                              capture_output=True, text=True, timeout=stretched(60))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("recovered iPhone's run by itself", done.stderr)
        entry = self.record.entry("iPhone")
        self.assertEqual((devices.condition(entry), entry["activeRun"]), ("held", False))


if __name__ == "__main__":
    unittest.main()
