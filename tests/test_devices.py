import contextlib
from datetime import datetime, timedelta, timezone
import fcntl
from io import StringIO
import json
import os
from pathlib import Path
import pwd
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "src/python"
sys.path.insert(0, str(SOURCE))
from vocaro_engineering import cli, device_notifications, devices, resources
import host_isolation
from host_isolation import stretched
from vocaro_engineering.resources import process_start

# Well-formed, invented identifiers: hardware UDIDs and a simulator UUID.
PHONE = "00008130-001A2C3E0C38001C"
TABLET = "00008103-000E4C1A3E90801E"
DUO = "5B0E6C51-2D3A-4F7B-9C1E-0A4B8D2F6E13"
# A sandbox that refuses to start ps, as an app's sandbox does, and allows the rest.
SANDBOX_EXEC = "/usr/bin/sandbox-exec"
DENY_PS = '(version 1)(allow default)(deny process-exec (literal "/bin/ps"))'
# Writes the entry it is given, as the record shows it during the run, to a file.
PROBE = ("import json, sys; entry = json.load(open(sys.argv[1]))['devices'][sys.argv[2]]; "
         "open(sys.argv[3], 'w').write(json.dumps(entry))")

WRITER = r"""
import contextlib, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from vocaro_engineering import devices
path, key, ready, count, mode, release_by, window = sys.argv[2:9]
record = devices.Record(path)
if mode == "unlocked":
    record._exclusive = contextlib.nullcontext
load = record.load
def composing():
    value = load()
    time.sleep(float(window))
    return value
record.load = composing
Path(ready, key).touch()
while len(list(Path(ready).iterdir())) < int(count):
    time.sleep(0.005)
record.reserve(key, owner=key, revision=0, purpose="concurrent claim", release_by=release_by)
"""


def later(minutes=30):
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


def live():
    """A process that stands in for a live run: it outlives the test that ends it, however slowly the host runs it."""
    return subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({stretched(60)})"])


def approval(**changes):
    """The owner's approval of a foreground window: given a minute ago, for the next hour."""
    return {"by": "owner", "at": later(-1), "until": later(60), **changes}


class Later(datetime):
    """Two hours from now, for a deadline or an approval that has passed."""

    @classmethod
    def now(cls, tz=None):
        return datetime.now(tz) + timedelta(hours=2)


def scratch():
    """A test directory outside /private/tmp, which the record refuses, even when TMPDIR is there."""
    base = Path(tempfile.gettempdir())
    if devices.restart_emptied(base):
        if sys.platform == "darwin":
            base = Path(subprocess.check_output(["/usr/bin/getconf", "DARWIN_USER_TEMP_DIR"], text=True).strip())
        else:
            base = Path(__file__).resolve().parents[1] / ".build"
            base.mkdir(exist_ok=True)
    return tempfile.TemporaryDirectory(dir=base)


class DeviceRecordTests(unittest.TestCase):
    def setUp(self):
        temp = scratch()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = self.root / "device-handoff.json"
        self.record = devices.Record(self.path)
        for patcher in (patch.dict(os.environ, {devices.OVERRIDE: str(self.path)}),
                        patch.object(devices, "LEGACY", str(self.root / "legacy-*device-handoff*.json"))):
            patcher.start()
            self.addCleanup(patcher.stop)
        # Run as on the owner's Mac, whatever runs the tests.
        for name in ("RUNNER_ENVIRONMENT", devices.OWNER):
            os.environ.pop(name, None)

    def reserve(self, key="iPhone", owner="session-a", revision=0, **options):
        # A physical device's reservation records the window the owner agreed.
        if devices.physical(options.get("device")):
            options.setdefault("approval", approval())
        return self.record.reserve(key, owner=owner, revision=revision, purpose="bounded UI test",
                                   release_by=options.pop("release_by", later()), **options)

    def foreground(self, owner="session-a", revision=0, **options):
        return self.record.reserve(devices.FOREGROUND, owner=owner, revision=revision, purpose="attended Mac XCUITest",
                                   release_by=options.pop("release_by", later()),
                                   approval=options.pop("approval", approval()), **options)

    def cli(self, *arguments):
        with contextlib.redirect_stdout(StringIO()) as output, contextlib.redirect_stderr(StringIO()) as error:
            code = devices.main(list(arguments))
        return code, output.getvalue(), error.getvalue()

    def legacy(self, name, value):
        path = self.root / f"legacy-{name}-device-handoff.json"
        path.write_text(json.dumps(value, indent=2))
        return path

    def test_the_default_record_survives_a_restart_and_ignores_a_session_home_or_tmpdir(self):
        account = Path(pwd.getpwuid(os.getuid()).pw_dir)
        with patch.dict(os.environ, {"HOME": str(self.root), "TMPDIR": str(self.root)}):
            del os.environ[devices.OVERRIDE]
            chosen = devices.record_path()
        base = "Library/Application Support" if sys.platform == "darwin" else ".local/state"
        self.assertEqual(chosen, account / base / "vocaro-engineering/device-handoff.json")
        self.assertFalse(devices.restart_emptied(chosen))
        temporary = [Path(tempfile.gettempdir()).resolve()]
        if sys.platform == "darwin":
            temporary.append(Path(subprocess.check_output(["/usr/bin/getconf", "DARWIN_USER_TEMP_DIR"], text=True).strip()).resolve())
        for directory in temporary:
            self.assertNotIn(directory, chosen.resolve().parents)

    def test_locations_a_restart_empties_are_refused(self):
        for path in ["/private/tmp/shared-device-handoff.json", "/tmp/pagepour-studywright-device-handoff.json"]:
            with self.subTest(path):
                with self.assertRaisesRegex(ValueError, "emptied by a restart"):
                    devices.Record(path)
                with patch.dict(os.environ, {devices.OVERRIDE: path}):
                    code, _output, error = self.cli("status")
                self.assertEqual(code, 2)
                self.assertIn("emptied by a restart", error)
        self.assertEqual(devices.Record().path, self.path)

    def concurrent_claims(self, mode, count=4):
        ready = self.root / f"ready-{mode}"
        ready.mkdir()
        path = self.root / f"{mode}.json"
        # The window in which a writer composes its change stretches by load, so that unlocked writers still overlap.
        writers = [subprocess.Popen([sys.executable, "-c", WRITER, str(SOURCE), str(path), f"device-{index}",
                                     str(ready), str(count), mode, later(), str(stretched(0.3))],
                                    stderr=subprocess.PIPE, text=True)
                   for index in range(count)]
        errors = [writer.communicate(timeout=stretched(60))[1] for writer in writers]
        self.assertEqual([writer.returncode for writer in writers], [0] * count, errors)
        return devices.Record(path).load()

    def test_concurrent_writers_keep_every_entry_and_without_the_lock_lose_some(self):
        record = self.concurrent_claims("locked")
        self.assertEqual(sorted(record["devices"]), [f"device-{index}" for index in range(4)])
        self.assertEqual(record["revision"], 4)
        # Negative control: the same claims without the lock overwrite each other, so a loss is visible.
        self.assertLess(len(self.concurrent_claims("unlocked")["devices"]), 4)

    def test_a_stale_revision_or_another_owner_changes_nothing(self):
        peer = self.reserve("iPad", owner="session-b")
        first = self.reserve(device=PHONE)
        second = self.record.update("iPhone", owner="session-a", revision=first["revision"], run="smallest case")
        before = self.path.read_bytes()
        with self.assertRaises(devices.Stale):
            self.record.update("iPhone", owner="session-a", revision=first["revision"], run="stale read")
        with self.assertRaises(devices.Stale):
            self.reserve(owner="session-c", revision=0)
        with self.assertRaisesRegex(devices.Refused, "not held by session-b"):
            self.record.update("iPhone", owner="session-b", revision=second["revision"], run="taken")
        with self.assertRaisesRegex(devices.Refused, "held by session-a"):
            self.reserve(owner="session-b", revision=second["revision"])
        with self.assertRaisesRegex(ValueError, "ownerChatID"):
            self.record.update("iPhone", owner="session-a", revision=second["revision"], ownerChatID="session-b")
        self.assertEqual(self.path.read_bytes(), before)
        third = self.record.update("iPhone", owner="session-a", revision=second["revision"], run="current read")
        self.assertEqual(third["revision"], second["revision"] + 1)
        self.assertEqual(self.record.entry("iPad"), peer)

    def test_a_passed_deadline_releases_nothing(self):
        entry = self.reserve()
        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        entry = self.record.update("iPhone", owner="session-a", revision=entry["revision"], expectedReleaseBy=past)
        self.assertEqual(devices.condition(entry), "held")
        with self.assertRaisesRegex(devices.Refused, "held by session-a"):
            self.reserve(owner="session-b", revision=entry["revision"])
        code, output, _error = self.cli("status")
        self.assertEqual(code, 0)
        self.assertIn("overdue, which releases nothing", output)
        with self.assertRaisesRegex(ValueError, "future"):
            self.reserve("iPad", release_by=past)

    def test_a_vanished_run_is_an_orphan_kept_for_attended_recovery(self):
        child = live()
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        entry = self.reserve(device=PHONE)
        with self.assertRaisesRegex(ValueError, "hostProcessIdentifier"):
            self.record.update("iPhone", owner="session-a", revision=entry["revision"], activeRun=True)
        entry = self.record.update("iPhone", owner="session-a", revision=entry["revision"], state="testing",
                                   activeRun=True, hostProcessIdentifier=child.pid)
        self.assertEqual(devices.condition(entry), "active")
        with self.assertRaisesRegex(devices.Refused, "active run"):
            self.record.release("iPhone", owner="session-a", revision=entry["revision"], outcome="o", restoration="r")
        with self.assertRaisesRegex(devices.Refused, "still running"):
            self.record.recover("iPhone", by="session-b", revision=entry["revision"], verified="nothing yet")
        child.kill()
        child.wait()
        self.assertEqual(devices.condition(entry), "orphan")
        with self.assertRaisesRegex(devices.Refused, "orphan"):
            self.reserve(owner="session-b", revision=entry["revision"])
        code, output, _error = self.cli("status")
        self.assertIn("kept for attended recovery", output)
        released = self.record.recover("iPhone", by="session-b", revision=entry["revision"],
                                       verified="no owned processes on the device; VoiceOver as recorded")
        self.assertEqual((released["state"], released["lastOwnerChatID"]), ("released", "session-a"))
        self.assertEqual(released["recovery"]["condition"], "orphan")
        self.reserve(owner="session-b", revision=released["revision"], approval=approval())

    def test_recovering_a_run_sent_to_the_second_mac_waits_until_nothing_there_may_use_the_device(self):
        child = live()
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        entry = self.reserve(device=PHONE)
        started = devices.now()
        entry = self.record.update("iPhone", owner="session-a", revision=entry["revision"], activeRun=True,
                                   hostProcessIdentifier=child.pid, run={"host": "build-host", "startedAt": started})
        child.kill()
        child.wait()
        self.assertEqual(devices.condition(entry), "orphan")
        asked = []

        def users(found):
            def census(host, udid, since):
                asked.append((host, udid, since))
                if isinstance(found, Exception): raise found
                return found
            return census

        recover = lambda census, **options: self.record.recover(
            "iPhone", by="owner", revision=entry["revision"], verified="nothing on the device", users=census, **options)
        before = self.path.read_bytes()
        with self.assertRaisesRegex(devices.Refused, "where these may still use its device.*\n  PID 7, names it"):
            recover(users(["PID 7, names it"]))
        with self.assertRaisesRegex(devices.Refused, "could not be checked: build-host cannot be reached"):
            recover(users(RuntimeError("build-host cannot be reached")))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(asked[0], ("build-host", PHONE, devices.moment(started).timestamp()))
        recovered = recover(users([]))
        self.assertEqual(recovered["recovery"]["offload"]["host"], "build-host")
        self.assertIn("checkedAt", recovered["recovery"]["offload"])
        # The owner can recover without the second Mac, recording why.
        entry = self.reserve(owner="session-b", revision=recovered["revision"], approval=approval())
        entry = self.record.update("iPhone", owner="session-b", revision=entry["revision"], state="uncertain-run",
                                   uncertain=True, run={"host": "build-host", "startedAt": started})
        asked.clear()
        code, output, error = self.cli("recover", "iPhone", "--by", "owner", "--revision", str(entry["revision"]),
                                       "--verified", "checked by hand", "--offload-unchecked", "it is gone")
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)["entry"]["recovery"]["offload"], {"host": "build-host",
                                                                              "unchecked": "it is gone"})
        self.assertEqual((error, asked), ("", []))
        # A run that stayed on this Mac asks no second Mac.
        entry = self.reserve(owner="session-c", revision=self.record.entry("iPhone")["revision"], approval=approval())
        entry = self.record.update("iPhone", owner="session-c", revision=entry["revision"], uncertain=True,
                                   run="a local result bundle")
        recovered = self.record.recover("iPhone", by="owner", revision=entry["revision"], verified="v",
                                        users=users(RuntimeError("never asked")))
        self.assertNotIn("offload", recovered["recovery"])
        self.assertEqual(asked, [])

    def test_recovery_refuses_the_old_name_of_offload_unchecked_with_the_new_one_and_hides_it_from_help(self):
        refused = ("devices: --build-host-unchecked was --offload-unchecked's name until vocaro-engineering#58, and "
                   "is no longer read: use --offload-unchecked (guides/shared-host.md, Earlier names)\n")
        entry = self.reserve(device=PHONE)
        entry = self.record.update("iPhone", owner="session-a", revision=entry["revision"], uncertain=True,
                                   run={"host": "build-host", "startedAt": devices.now()})
        before = self.path.read_bytes()
        recover = ["recover", "iPhone", "--by", "owner", "--revision", str(entry["revision"]), "--verified",
                   "checked by hand"]
        # Refused alone, with an empty reason, and beside the new name, and the record is unchanged.
        for given in (["--build-host-unchecked", "it is gone"], ["--build-host-unchecked", ""],
                      ["--build-host-unchecked", "old", "--offload-unchecked", "new"]):
            with self.subTest(given):
                self.assertEqual(self.cli(*recover, *given), (2, "", refused))
        self.assertEqual(self.path.read_bytes(), before)
        with contextlib.redirect_stdout(StringIO()) as shown, self.assertRaises(SystemExit):
            devices.main(["recover", "--help"])
        self.assertIn("--offload-unchecked", shown.getvalue())
        self.assertNotIn("build-host", shown.getvalue())
        # The positive control: the new name recovers the entry, recording the owner's reason.
        code, output, error = self.cli(*recover, "--offload-unchecked", "it is gone")
        self.assertEqual((code, error), (0, ""), error)
        self.assertEqual(json.loads(output)["entry"]["recovery"]["offload"], {"host": "build-host",
                                                                              "unchecked": "it is gone"})

    def test_explicit_uncertainty_without_a_known_pid_cannot_be_auto_released(self):
        entry = self.reserve()
        entry = self.record.update("iPhone", owner="session-a", revision=entry["revision"],
                                   uncertain=True, run={"uncertainty": "launch may have completed without its PID"})
        before = self.path.read_bytes()
        with self.assertRaisesRegex(devices.Refused, "unresolved uncertainty"):
            self.record.release("iPhone", owner="session-a", revision=entry["revision"],
                                outcome="caller ended", restoration="no PID was observed")
        self.assertEqual(self.path.read_bytes(), before)
        recovered = self.record.recover("iPhone", by="owner", revision=entry["revision"],
                                        verified="exact executable census is empty and request ended")
        self.assertEqual(recovered["state"], "released")
        self.assertNotIn("uncertain", recovered)
        self.assertIn("without its PID", recovered["run"]["uncertainty"])

    def test_foreground_finally_retains_explicit_unknown_launch_uncertainty(self):
        from vocaro_engineering import foreground
        from vocaro_engineering.voiceover import state
        with patch.object(foreground, "second_mac", return_value=True), \
             patch.object(foreground, "front", return_value="owned control"), \
             patch.object(foreground.offload_host, "mac_name", return_value="test Mac"):
            # Independent positive control: the normal context still releases.
            with foreground.slot("positive", timeout=stretched(1), record=self.record,
                                 ready=lambda: [], voiceover=lambda: False, said=lambda _: None):
                pass
            self.assertEqual(self.record.entry(devices.FOREGROUND)["state"], "released")
            with self.assertRaisesRegex(state.TransitionFailed, "original state was not restored"):
                # The state guard refuses restoration over unrelated process uncertainty.
                with foreground.slot("unknown launch", timeout=stretched(1), record=self.record,
                                     ready=lambda: [], voiceover=lambda: False, said=lambda _: None) as owner:
                    entry = self.record.entry(devices.FOREGROUND)
                    self.record.update(devices.FOREGROUND, owner=owner, revision=entry["revision"],
                                       uncertain=True, run={"uncertainty": "launch completion is unknown"})
            retained = self.record.entry(devices.FOREGROUND)
            self.assertEqual(devices.condition(retained), "uncertain")
            self.assertTrue(retained["activeRun"])
            self.assertTrue(retained["uncertain"])
            self.assertIsNotNone(retained["ownerChatID"])

    def test_unverified_restoration_keeps_the_device_until_its_owner_releases_it(self):
        entry = self.reserve()
        entry = self.record.update("iPhone", owner="session-a", revision=entry["revision"], state="restoration-unverified")
        self.assertEqual(devices.condition(entry), "uncertain")
        with self.assertRaisesRegex(devices.Refused, "uncertain"):
            self.reserve(owner="session-b", revision=entry["revision"])
        for change in [{"uncertain": False}, {"state": "released"}]:
            with self.assertRaises(ValueError):
                self.record.update("iPhone", owner="session-a", revision=entry["revision"], **change)
        with self.assertRaisesRegex(ValueError, "outcome"):
            self.record.release("iPhone", owner="session-a", revision=entry["revision"], outcome="", restoration="")
        released = self.record.release("iPhone", owner="session-a", revision=entry["revision"], outcome="Xcode exit 0",
                                       restoration="VoiceOver as recorded; no owned processes")
        self.assertEqual(devices.condition(released), "released")
        self.assertIsNone(released["ownerChatID"])
        self.assertEqual(released["lastOwnerChatID"], "session-a")

    def test_one_device_is_never_held_under_two_keys_or_named_loosely(self):
        self.reserve(device=PHONE)
        with self.assertRaisesRegex(devices.Refused, "same device"):
            self.reserve("Phone alias", owner="session-b", device=PHONE)
        for name in ["booted", PHONE.lower(), "Owner's iPhone"]:
            with self.assertRaisesRegex(ValueError, "UDID"):
                self.reserve("iPad", device=name)
        entry = self.record.entry("iPhone")
        self.record.release("iPhone", owner="session-a", revision=entry["revision"], outcome="o", restoration="r")
        self.reserve("Phone alias", owner="session-b", device=PHONE)

    def test_a_hardware_udid_is_physical_and_a_simulators_is_not(self):
        for udid in [PHONE, PHONE.lower(), "A" * 40]:
            self.assertTrue(devices.physical(udid), udid)
        for udid in [DUO, None, "", "booted", "00008130-001A2C3E"]:
            self.assertFalse(devices.physical(udid), udid)

    def test_a_physical_device_is_reserved_and_worked_on_only_within_the_owners_window(self):
        with self.assertRaisesRegex(devices.Refused, "iPhone is a physical device: its reservation records the window"):
            self.reserve(device=PHONE, approval=None)
        self.assertFalse(self.path.exists())
        # A simulator needs no window, and an entry without one is not given a physical device.
        duo = self.reserve("Example Duo 1", device=DUO)
        with self.assertRaisesRegex(devices.Refused, "Example Duo 1 is a physical device"):
            self.record.update("Example Duo 1", owner="session-a", revision=duo["revision"], device=PHONE)
        with self.assertRaisesRegex(devices.Refused, "approval of Example Duo 1 was never recorded"):
            devices.require_window("Example Duo 1", duo)
        entry = self.reserve(device=PHONE)
        self.assertIs(devices.require_window("iPhone", entry), entry)
        self.assertNotIn("work on the device is refused", self.cli("status")[1])
        with patch.object(devices, "datetime", Later):
            with self.assertRaisesRegex(devices.Refused, re.escape(f"approval of iPhone ended at {entry['approval']['until']}")):
                devices.require_window("iPhone", self.record.entry("iPhone"))
            output = self.cli("status")[1]
        self.assertIn("iPhone: held", output)
        self.assertIn("approval ended or never recorded; work on the device is refused", output)

    def test_a_record_that_defers_physical_devices_refuses_them_and_keeps_its_simulators(self):
        # Before the deferral, the record holds a physical device like any other.
        self.reserve(device=PHONE)
        entry = self.record.entry("iPhone")
        self.record.release("iPhone", owner="session-a", revision=entry["revision"], outcome="o", restoration="r")
        self.assertTrue(self.record.defer("workstation"))
        with self.assertRaisesRegex(devices.Refused, "reserved in workstation's device record.*--device iPhone"):
            self.reserve(revision=self.record.entry("iPhone")["revision"])
        with self.assertRaisesRegex(devices.Refused, "workstation's device record"):
            self.reserve("iPad", device=PHONE.replace("C", "D"))
        duo = self.reserve("Example Duo 1", device=DUO)
        with self.assertRaisesRegex(devices.Refused, "workstation's device record"):
            self.record.update("Example Duo 1", owner="session-a", revision=duo["revision"], device=PHONE)
        self.assertEqual(self.record.entry("Example Duo 1")["revision"], duo["revision"])
        self.assertIsNone(self.record.entry("iPad"))
        self.assertIn("Physical devices are reserved in workstation's device record", self.cli("status")[1])
        self.record.defer(None)
        self.reserve(revision=self.record.entry("iPhone")["revision"], approval=approval())
        self.assertNotIn("Physical devices are reserved", self.cli("status")[1])

    def test_which_mac_holds_physical_devices_changes_only_by_the_owners_decision(self):
        code, output, _error = self.cli("defer", "workstation")
        self.assertEqual((code, output.strip()), (0, f"{self.path}: physical devices are reserved in workstation's "
                                                     "device record from now on"))
        revision = self.record.load()["revision"]
        # Deferring to the same Mac again writes nothing and says nothing.
        self.assertEqual(self.cli("defer", "workstation")[:2], (0, ""))
        self.assertEqual(self.record.load()["revision"], revision)
        code, _output, error = self.cli("defer", "laptop")
        self.assertEqual(code, 2)
        self.assertIn("defers physical devices to workstation, not laptop", error)
        self.assertEqual(self.record.load()[devices.DEFERRED], "workstation")
        self.assertEqual(self.cli("defer", "laptop", "--replace")[0], 0)
        self.assertEqual(self.record.load()[devices.DEFERRED], "laptop")
        self.assertIn("reserved here again", self.cli("defer", "--end")[1])
        self.assertNotIn(devices.DEFERRED, self.record.load())
        # A physical device held here is released or recovered before the record defers it.
        self.reserve(device=PHONE)
        with self.assertRaisesRegex(devices.Refused, "iPhone holds a physical device"):
            self.record.defer("workstation")
        self.assertNotIn(devices.DEFERRED, self.record.load())
        entry = self.record.entry("iPhone")
        self.record.release("iPhone", owner="session-a", revision=entry["revision"], outcome="o", restoration="r")
        self.assertTrue(self.record.defer("workstation"))

    def test_a_held_lock_refuses_after_its_timeout_without_writing(self):
        self.reserve()
        before = self.path.read_bytes()
        descriptor = os.open(self.record.lock, os.O_RDWR)
        self.addCleanup(os.close, descriptor)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        waiting = devices.Record(self.path, timeout=0.1)  # fixed: the refusal at 0.1 s is the behavior under test
        with self.assertRaisesRegex(TimeoutError, "nothing was changed"):
            waiting.update("iPhone", owner="session-a", revision=1, run="blocked")
        self.assertEqual(self.path.read_bytes(), before)
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        self.assertEqual(waiting.update("iPhone", owner="session-a", revision=1, run="after")["revision"], 2)

    def test_an_unreadable_or_foreign_record_is_never_replaced(self):
        for text in ["{not json", json.dumps({"devices": {}})]:
            with self.subTest(text):
                self.path.write_text(text)
                with self.assertRaises(devices.Refused):
                    self.reserve()
                self.assertEqual(self.path.read_text(), text)

    def test_import_keeps_every_entry_from_both_records_once(self):
        newer = self.legacy("pair", {"kind": "handoff", "updatedAt": "2026-10-21T06:50:00+00:00", "devices": {
            "iPhone": {"device": PHONE, "ownerChatID": None, "state": "released", "activeRun": False,
                       "leaseRevision": "2026-10-20T16:48:00+00:00", "releasedAt": "2026-10-20T17:00:00+00:00",
                       "run": "newer"},
            "iPad": {"ownerChatID": "session-c", "state": "released", "activeRun": False,
                     "leaseRevision": "2026-10-20T07:26:00+00:00", "releasedAt": "2026-10-20T08:00:00+00:00"},
            "Duo": {"device": DUO, "ownerChatID": "session-d", "state": "reserved", "activeRun": False,
                    "leaseRevision": "2026-10-21T06:37:00+00:00"}},
            "peerReport": {"chatID": "session-c", "completedBothRuns": True}, "preservation": "owner data"})
        older = self.legacy("shared", {"devices": {
            "iPhone": {"device": PHONE, "ownerChatID": "session-b", "state": "released", "activeRun": False,
                       "leaseRevision": "2026-10-20T07:00:00+00:00", "releasedAt": "2026-10-20T07:30:00+00:00",
                       "run": "older"},
            "iPad": {"session": "session-e", "state": "reserved", "activeRun": False,
                     "reservedAt": "2026-10-20T09:00:00+00:00"}}})
        originals = {path: path.read_bytes() for path in (newer, older)}
        # Negative control: the earlier rule, reserving in the record updated last, misses session-e's hold.
        self.assertNotIn("session-e", newer.read_text())
        self.assertEqual(self.cli("status")[1].count("not imported"), 2)
        report = self.record.import_legacy([newer, older], by="session-x")
        self.assertEqual(report, [f"{newer}: 3 added, 0 merged, 0 already present",
                                  f"{older}: 0 added, 2 merged, 0 already present"])
        value = self.record.load()
        entries = value["devices"]
        self.assertEqual(set(entries), {"iPhone", "iPad", "Duo"})
        self.assertEqual(entries["iPhone"]["run"], "newer")
        self.assertEqual([version["run"] for version in entries["iPhone"]["importedVersions"]], ["older"])
        self.assertEqual(entries["iPad"]["session"], "session-e")
        self.assertEqual(entries["iPad"]["importedVersions"][0]["ownerChatID"], "session-c")
        self.assertEqual([devices.condition(entries[key]) for key in ("iPhone", "iPad", "Duo")],
                         ["released", "held", "held"])
        self.assertNotIn("uncertain", entries["iPad"])
        self.assertEqual(value["imports"][0]["fields"]["peerReport"], {"chatID": "session-c", "completedBothRuns": True})
        self.assertEqual({path: path.read_bytes() for path in (newer, older)}, originals)
        self.assertNotIn("not imported", self.cli("status")[1])
        self.assertEqual(self.record.import_legacy([newer, older], by="session-x"),
                         [f"{newer}: already imported", f"{older}: already imported"])
        self.assertEqual(self.record.load()["revision"], value["revision"])
        with self.assertRaisesRegex(devices.Refused, "held by None"):
            self.reserve("iPad", owner="session-y", revision=entries["iPad"]["revision"])

    def test_import_marks_conflicting_holds_uncertain_and_keeps_both(self):
        self.record.import_legacy([self.legacy("first", {"devices": {
            "iPad": {"ownerChatID": "session-e", "state": "reserved", "reservedAt": "2026-10-20T09:00:00+00:00"},
            "Duo": {"device": DUO, "ownerChatID": "session-d", "state": "reserved"}}})], by="session-x")
        second = self.legacy("second", {"devices": {
            "iPad": {"ownerChatID": "session-f", "state": "testing", "reservedAt": "2026-10-20T10:00:00+00:00"},
            "Duo spare": {"device": DUO, "ownerChatID": "session-g", "state": "reserved"}}})
        self.record.import_legacy([second], by="session-x")
        entries = self.record.load()["devices"]
        self.assertEqual(entries["iPad"]["ownerChatID"], "session-f")
        self.assertEqual(entries["iPad"]["importedVersions"][0]["ownerChatID"], "session-e")
        self.assertEqual([devices.condition(entries[key]) for key in ("iPad", "Duo", "Duo spare")], ["uncertain"] * 3)
        self.assertIn("Duo spare", entries["Duo"]["uncertainReason"])
        with self.assertRaisesRegex(devices.Refused, "uncertain"):
            self.reserve("iPad", owner="session-y", revision=entries["iPad"]["revision"])
        released = self.record.recover("iPad", by="session-x", revision=entries["iPad"]["revision"],
                                       verified="both sessions confirmed their runs ended")
        self.assertEqual(released["recovery"]["uncertainReason"], entries["iPad"]["uncertainReason"])
        before = self.path.read_bytes()
        broken = self.legacy("broken", {"devices": ["not", "entries"]})
        with self.assertRaisesRegex(devices.Refused, "nothing was imported"):
            self.record.import_legacy([second, broken], by="session-x")
        self.assertEqual(self.path.read_bytes(), before)

    def test_status_lists_the_mac_foreground_entry_free_then_with_its_holder_and_approval(self):
        self.assertIn(f"{devices.FOREGROUND}: free", self.cli("status")[1])
        entry = self.foreground(approval=approval(words="Use the screen for the Mac UI tests until 6"))
        self.assertNotIn("device", entry)
        self.assertEqual(entry["approval"]["words"], "Use the screen for the Mac UI tests until 6")
        code, output, _error = self.cli("status")
        self.assertEqual(code, 0)
        self.assertIn(f"{devices.FOREGROUND}: held; owner session-a, state reserved", output)
        self.assertIn(f"approved by owner at {entry['approval']['at']} until {entry['approval']['until']}", output)

    def held_by(self, pid, start, due):
        """A reserved simulator whose holder and expected release are these."""
        entry = devices.Record(self.path).reserve("Pool iPhone 1", owner="session-a", revision=0, purpose="tests",
                                                  release_by=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(), device=DUO)
        record = json.loads(self.path.read_text())
        record["devices"]["Pool iPhone 1"].update(holderProcess={"pid": pid, "start": start}, expectedReleaseBy=due)
        self.path.write_text(json.dumps(record))
        return entry

    def test_status_names_a_held_entry_whose_process_is_gone_and_whose_release_has_passed(self):
        gone = subprocess.Popen(["/usr/bin/true"])
        gone.wait()
        entry = self.held_by(gone.pid, "Thu Jan  1 00:00:00 2026", "2026-01-01T00:00:00+00:00")
        before = self.path.read_bytes()
        output = self.cli("status")[1]
        self.assertIn(f"Recoverable: Pool iPhone 1 stays held for owner session-a, though its process {gone.pid} is gone",
                      output)
        self.assertIn("python3 -m vocaro_engineering.devices recover 'Pool iPhone 1' --by SESSION "
                      f"--revision {entry['revision']} --verified 'WHAT YOU CHECKED'", output)
        self.assertEqual(self.path.read_bytes(), before, "status changes nothing")

    def test_status_does_not_name_a_live_holder_or_a_release_not_yet_due(self):
        start = process_start(os.getpid())
        self.held_by(os.getpid(), start, "2026-01-01T00:00:00+00:00")
        self.assertNotIn("Recoverable", self.cli("status")[1])
        self.path.unlink()
        gone = subprocess.Popen(["/usr/bin/true"])
        gone.wait()
        due = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        self.held_by(gone.pid, "Thu Jan  1 00:00:00 2026", due)
        self.assertNotIn("Recoverable", self.cli("status")[1])

    def test_status_says_when_it_cannot_tell(self):
        with patch.object(devices.offload_host, "listing", return_value=None):
            self.assertIn("could not be listed", self.cli("status")[1])

    def test_a_second_foreground_reservation_is_refused(self):
        entry = self.foreground()
        before = self.path.read_bytes()
        with self.assertRaisesRegex(devices.Refused, "held by session-a"):
            self.foreground(owner="session-b", revision=entry["revision"])
        self.assertEqual(self.path.read_bytes(), before)

    def test_the_approval_window_is_required_checked_and_never_changed(self):
        invalid = [({"approval": None}, "owner's approval"), ({"device": PHONE}, "names no device"),
                   ({"approval": {"by": "owner", "at": later(-1)}}, "by, at and until"),
                   ({"approval": approval(at=later(5))}, "when the owner approved"),
                   ({"approval": approval(until=later(-5))}, "until"),
                   ({"approval": approval(until=later(20)), "release_by": later(30)}, "after the owner's approval ends")]
        for options, message in invalid:
            with self.subTest(message), self.assertRaisesRegex(ValueError, message):
                self.foreground(**options)
        self.assertFalse(self.path.exists())
        entry = self.foreground(approval=approval(until="released"), release_by=later(600))
        for change in [{"approval": approval(until=later(600))}, {"ownedProcesses": []}]:
            with self.subTest(change=sorted(change)), self.assertRaisesRegex(ValueError, "itself"):
                self.record.update(devices.FOREGROUND, owner="session-a", revision=entry["revision"], **change)
        code, _output, error = self.cli("update", devices.FOREGROUND, "--owner", "session-a", "--revision",
                                        str(entry["revision"]), "--set", f"approval={json.dumps(approval())}")
        self.assertEqual(code, 2)
        self.assertIn("itself", error)

    def test_a_passed_approval_or_deadline_releases_nothing_and_refuses_foreground_work(self):
        entry = self.foreground()
        self.assertEqual(devices.require_foreground("session-a")["ownerChatID"], "session-a")
        with patch.object(devices, "datetime", Later):
            self.assertEqual(devices.condition(self.record.entry(devices.FOREGROUND)), "held")
            with self.assertRaisesRegex(devices.Refused, "approval .* ended"):
                devices.require_foreground("session-a")
            with self.assertRaisesRegex(devices.Refused, "held by session-a"):
                self.foreground(owner="session-b", revision=entry["revision"], release_by=later(300),
                                approval=approval(until=later(600)))
            output = self.cli("status")[1]
        self.assertIn("overdue, which releases nothing", output)
        self.assertIn("approval ended; foreground actions are refused", output)

    def test_the_foreground_check_refuses_other_sessions_and_orphans_on_any_runner(self):
        with self.assertRaisesRegex(devices.Refused, "nobody holds"):
            devices.require_foreground("session-a")
        entry = self.foreground()
        for owner, message in [("session-b", "held by session-a"), (None, "name the session")]:
            with self.subTest(owner=owner), self.assertRaisesRegex(devices.Refused, message):
                devices.require_foreground(owner)
        self.assertEqual(self.cli("check", "--owner", "session-a")[0], 0)
        self.assertEqual(self.cli("check", "--owner", "session-b")[0], 2)
        with patch.dict(os.environ, {devices.OWNER: "session-a"}):
            self.assertEqual(self.cli("check")[0], 0)
        child = live()
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        self.record.update(devices.FOREGROUND, owner="session-a", revision=entry["revision"], state="testing",
                           activeRun=True, hostProcessIdentifier=child.pid)
        self.assertEqual(devices.require_foreground("session-a")["state"], "testing")
        child.kill()
        child.wait()
        with self.assertRaisesRegex(devices.Refused, "orphan"):
            devices.require_foreground("session-a")
        # A GitHub-hosted runner once passed without a reservation; no runner does now.
        with patch.dict(os.environ, {"RUNNER_ENVIRONMENT": "github-hosted"}), \
             self.assertRaisesRegex(devices.Refused, "held by session-a"):
            devices.require_foreground("session-b")

    @unittest.skipUnless(Path(SANDBOX_EXEC).exists(), "no sandbox-exec to deny ps with")
    def test_a_sandboxed_runner_that_may_not_start_ps_still_checks_an_active_runs_foreground(self):
        # As eng holds the second Mac's foreground: the entry's active run is a live process. A
        # sandboxed XCTest runner then checks it, and its sandbox refuses to start ps.
        entry = self.foreground()
        child = live()
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        self.record.update(devices.FOREGROUND, owner="session-a", revision=entry["revision"], state="testing",
                           activeRun=True, hostProcessIdentifier=child.pid)
        check = ("import subprocess\nfrom vocaro_engineering import devices\n"
                 "try:\n    subprocess.run(['ps', '-p', '1'], capture_output=True)\n    print('ps ran')\n"
                 "except PermissionError:\n    print('ps refused')\n"
                 "try:\n    print(devices.require_foreground('session-a')['state'])\n"
                 "except devices.Refused as refused:\n    print('refused:', refused)\n")

        def sandboxed():
            environment = {**os.environ, "PYTHONPATH": str(SOURCE), devices.OVERRIDE: str(self.path)}
            return subprocess.run([SANDBOX_EXEC, "-p", DENY_PS, sys.executable, "-c", check], env=environment,
                                  capture_output=True, text=True, timeout=stretched(60))

        found = sandboxed()
        self.assertEqual(found.returncode, 0, found.stderr)
        self.assertEqual(found.stdout.splitlines(), ["ps refused", "testing"])
        child.kill()
        child.wait()
        found = sandboxed()
        self.assertEqual(found.returncode, 0, found.stderr)
        self.assertEqual(found.stdout.splitlines()[0], "ps refused")
        self.assertRegex(found.stdout.splitlines()[1], "^refused: .*orphan")

    def test_release_waits_for_owned_processes_and_records_the_frontmost_restoration(self):
        entry = self.foreground()
        child = live()
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        with self.assertRaisesRegex(devices.Refused, "not running"):
            self.record.update(devices.FOREGROUND, owner="session-a", revision=entry["revision"], own=[99999999])
        code, output, _error = self.cli("update", devices.FOREGROUND, "--owner", "session-a",
                                        "--revision", str(entry["revision"]), "--own", str(child.pid))
        self.assertEqual(code, 0)
        entry = json.loads(output)["entry"]
        self.assertEqual([item["pid"] for item in entry["ownedProcesses"]], [child.pid])
        with self.assertRaisesRegex(devices.Refused, "still running"):
            self.record.release(devices.FOREGROUND, owner="session-a", revision=entry["revision"], outcome="o",
                                restoration="r", frontmost="restored")
        with self.assertRaisesRegex(devices.Refused, "still running"):
            self.record.recover(devices.FOREGROUND, by="session-b", revision=entry["revision"], verified="v")
        child.kill()
        child.wait()
        with self.assertRaisesRegex(ValueError, "frontmost"):
            self.record.release(devices.FOREGROUND, owner="session-a", revision=entry["revision"], outcome="o",
                                restoration="r")
        code, output, _error = self.cli("release", devices.FOREGROUND, "--owner", "session-a", "--revision",
                                        str(entry["revision"]), "--outcome", "UI tests passed", "--restoration",
                                        "Keyboard Navigation and VoiceOver as recorded", "--frontmost", "not-ours")
        self.assertEqual(code, 0)
        released = json.loads(output)["entry"]
        self.assertEqual((released["state"], released["frontmost"]), ("released", "not-ours"))

    def test_a_foreground_lane_runs_only_for_the_session_holding_mac_foreground(self):
        project = self.root / "project"
        project.mkdir()
        (project / "engineering.json").write_text(json.dumps({"schemaVersion": 1, "name": "Project", "lanes": {
            "ui": {"command": [sys.executable, "-c", "open('ran', 'a').write('ran')"], "resource": "heavy",
                   "foreground": True}}}))
        def run(owner):
            # On this Mac, only with --here: otherwise a foreground lane goes to the second Mac (test_foreground).
            with patch.dict(os.environ, {devices.OWNER: owner} if owner else {}), \
                 patch.dict(os.environ, {"HEAVY_SLOT_DIR": str(self.root / "slots"),
                                         "SW_GATE_LOCK_FILE": str(self.root / "gate")}), \
                 patch.object(cli, "source_identity", return_value={}), \
                 patch.object(cli, "refresh_policy", return_value=None), contextlib.redirect_stderr(StringIO()) as error:
                return cli.main(["--project", str(project), "run", "--here", "ui"]), error.getvalue()
        self.assertEqual(run("session-a"), (2, f"eng: nobody holds {devices.FOREGROUND}; reserve it with the owner's "
                                               "approval before foreground work\n"))
        self.foreground()
        for owner in ["session-b", None]:
            with self.subTest(owner=owner):
                self.assertEqual(run(owner)[0], 2)
        self.assertFalse((project / "ran").exists())
        self.assertEqual(run("session-a")[0], 0)
        self.assertEqual((project / "ran").read_text(), "ran")

    def test_command_line_reports_revisions_and_refusals(self):
        code, output, _error = self.cli("reserve", "iPhone", "--owner", "session-a", "--revision", "0",
            "--purpose", "smoke", "--release-by", later(), "--device", PHONE, "--approved-by", "owner",
            "--approved-at", later(-1), "--approved-until", later(60),
            "--set", 'originalSettings={"voiceoverEnabled": false}', "--set", "preservation=owner apps and data")
        self.assertEqual(code, 0)
        shown = json.loads(output)
        self.assertEqual((shown["revision"], shown["condition"]), (1, "held"))
        self.assertEqual(shown["entry"]["originalSettings"], {"voiceoverEnabled": False})
        self.assertEqual(shown["entry"]["preservation"], "owner apps and data")
        code, _output, error = self.cli("release", "iPhone", "--owner", "session-b", "--revision", "1",
                                        "--outcome", "o", "--restoration", "r")
        self.assertEqual(code, 2)
        self.assertIn("nothing was changed", error)
        code, output, _error = self.cli("release", "iPhone", "--owner", "session-a", "--revision", "1",
                                        "--outcome", "Xcode exit 0", "--restoration", "verified")
        self.assertEqual(json.loads(output)["condition"], "released")
        self.assertIn("iPhone: released; last owner session-a", self.cli("status")[1])
        self.assertEqual(json.loads(self.cli("show", "iPad")[1])["revision"], 0)
        self.assertEqual(self.cli("path")[1].strip(), str(self.path))

    def waiter(self):
        """A running process to wait in line as, stopped at the test's end."""
        process = live()
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        return process

    def released_tablet(self):
        entry = self.reserve("iPad", device=TABLET)
        return self.record.release("iPad", owner="session-a", revision=entry["revision"], outcome="o", restoration="r")

    def test_a_device_with_standing_approval_is_taken_in_turn_without_asking(self):
        held = self.reserve("iPad", device=TABLET)
        said = []
        result = {}

        def take():
            try:
                result["entry"] = self.record.take("iPad", owner="session-b", purpose="UI tests", release_by=later(),
                                                   timeout=stretched(30), poll=0.01, said=said.append)
            except BaseException as error:
                result["error"] = error
        thread = threading.Thread(target=take)
        thread.start()
        deadline = time.monotonic() + stretched(10)
        while not devices.waiting(self.record.load(), "iPad") and time.monotonic() < deadline:
            time.sleep(0.01)
        # Joining the line leaves the holder's entry, and so its next change, alone.
        self.assertEqual(self.record.entry("iPad")["revision"], held["revision"])
        self.assertIn("waiting: session-b (UI tests)", self.cli("status")[1])
        with self.assertRaisesRegex(devices.Refused, "held by session-a"):
            self.reserve("iPad", owner="session-c", revision=held["revision"])
        self.record.release("iPad", owner="session-a", revision=held["revision"], outcome="o", restoration="r")
        thread.join(stretched(10))
        self.assertNotIn("error", result)
        entry = result["entry"]
        self.assertEqual((entry["ownerChatID"], entry["device"]), ("session-b", TABLET))
        self.assertEqual(entry["approval"], {**devices.STANDING["iPad"], "until": "released"})
        self.assertIs(devices.require_window("iPad", entry), entry)
        self.assertTrue(any("held by session-a" in line for line in said))
        self.assertEqual(devices.waiting(self.record.load(), "iPad"), [])

    def test_while_sessions_wait_only_the_first_in_line_may_reserve(self):
        entry = self.released_tablet()
        first, second = self.waiter(), self.waiter()
        self.assertEqual(self.record.join("iPad", owner="session-b", purpose="first", pid=first.pid), 1)
        self.assertEqual(self.record.join("iPad", owner="session-c", purpose="second", pid=second.pid), 2)
        with self.assertRaisesRegex(devices.Refused, "already waits"):
            self.record.join("iPad", owner="session-c", purpose="again", pid=second.pid)
        # Even a window the owner agreed waits its turn.
        for owner in ("session-a", "session-c"):
            with self.assertRaisesRegex(devices.Refused, "2 waiting for iPad, first session-b"):
                self.reserve("iPad", owner=owner, revision=entry["revision"], device=TABLET)
        # A waiter whose process is gone drops out of line.
        first.kill()
        first.wait()
        self.assertEqual([item["owner"] for item in devices.waiting(self.record.load(), "iPad")], ["session-c"])
        taken = self.reserve("iPad", owner="session-c", revision=entry["revision"], device=TABLET)
        self.assertEqual(taken["ownerChatID"], "session-c")
        self.assertEqual(devices.waiting(self.record.load(), "iPad"), [])
        self.assertEqual(self.record.load()[devices.QUEUES]["iPad"], [])

    def test_only_standing_devices_that_record_their_device_are_taken(self):
        # Their 1 s timeouts stay fixed: each take is refused before it waits.
        with self.assertRaisesRegex(ValueError, "take waits only for iPad or iPhone"):
            self.record.take("Example Duo 1", owner="s", purpose="p", release_by=later(), timeout=1)
        with self.assertRaisesRegex(devices.Refused, "records no physical device yet"):
            self.record.take("iPad", owner="s", purpose="p", release_by=later(), timeout=1, poll=0.01,
                             said=lambda _text: None)
        self.assertEqual(devices.waiting(self.record.load(), "iPad"), [])
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(StringIO()) as error:
            devices.main(["take", "Example Duo 1", "--owner", "s", "--purpose", "p", "--release-by", later()])
        self.assertIn("invalid choice", error.getvalue())

    def test_a_take_that_waits_too_long_leaves_the_line_and_reserves_nothing(self):
        held = self.reserve("iPad", device=TABLET)
        with self.assertRaisesRegex(TimeoutError, "nothing was reserved"):
            self.record.take("iPad", owner="session-b", purpose="p", release_by=later(), timeout=0.05, poll=0.01,
                             said=lambda _text: None)
        self.assertEqual(devices.waiting(self.record.load(), "iPad"), [])
        self.assertEqual(self.record.entry("iPad")["revision"], held["revision"])
        self.assertEqual(self.record.entry("iPad")["ownerChatID"], "session-a")

    def released_phone(self):
        entry = self.reserve("iPhone", device=PHONE)
        return self.record.release("iPhone", owner="session-a", revision=entry["revision"], outcome="o",
                                   restoration="r")

    def test_the_ipad_is_taken_with_no_window_and_the_iphone_is_not(self):
        # The owner's decision of 2026-11-01: the iPhone, the owner's personal phone, has no standing approval.
        self.assertEqual(set(devices.STANDING), {"iPad"})
        self.assertEqual(set(devices.WINDOWED), {"iPhone"})
        self.released_tablet()
        phone = self.released_phone()
        taken = self.record.take("iPad", owner="session-b", purpose="p", release_by=later(), timeout=stretched(5), poll=0.01,
                                 said=lambda _text: None)
        self.assertEqual(taken["approval"], {**devices.STANDING["iPad"], "until": "released"})
        with self.assertRaisesRegex(devices.Refused, r"iPhone has no standing approval\. Ask the owner for a window "
                                    r"first.*devices reserve iPhone --approved-by.*Nothing was changed"):
            self.record.take("iPhone", owner="session-b", purpose="p", release_by=later(), timeout=stretched(5), poll=0.01,
                             said=lambda _text: None)
        self.assertEqual(self.record.entry("iPhone"), phone)
        self.assertEqual(devices.waiting(self.record.load(), "iPhone"), [])
        code, output, error = self.cli("take", "iPhone", "--owner", "session-b", "--purpose", "p",
                                       "--release-by", later())
        self.assertEqual((code, output), (2, ""))
        self.assertIn("Ask the owner for a window first", error)
        self.assertEqual(self.record.entry("iPhone"), phone)
        self.assertIn("iPhone: released", self.cli("status")[1])
        self.assertIn("no standing approval: ask the owner for a window", self.cli("status")[1])

    def test_the_iphone_is_reserved_or_taken_within_the_owners_window(self):
        phone = self.released_phone()
        # A window that has ended, or ends before the expected release, is no window.
        for window, release_by in ((approval(at=later(-90), until=later(-30)), later()),
                                   (approval(until=later(10)), later(30))):
            with self.assertRaises(ValueError):
                self.record.take("iPhone", owner="session-b", purpose="p", release_by=release_by, timeout=stretched(5),
                                 poll=0.01, said=lambda _text: None, approval=window)
        self.assertEqual(self.record.entry("iPhone"), phone)
        self.assertEqual(devices.waiting(self.record.load(), "iPhone"), [])
        window = approval(words="ok, the iphone is yours for the next hour")
        taken = self.record.take("iPhone", owner="session-b", purpose="p", release_by=later(), timeout=stretched(5), poll=0.01,
                                 said=lambda _text: None, approval=window)
        self.assertEqual((taken["ownerChatID"], taken["device"], taken["approval"]), ("session-b", PHONE, window))
        self.assertIs(devices.require_window("iPhone", taken), taken)
        released = self.record.release("iPhone", owner="session-b", revision=taken["revision"], outcome="o",
                                       restoration="r")
        reserved = self.record.reserve("iPhone", owner="session-c", revision=released["revision"], purpose="p",
                                       release_by=later(), approval=window)
        self.assertEqual((reserved["ownerChatID"], reserved["approval"]), ("session-c", window))
        released = self.record.release("iPhone", owner="session-c", revision=reserved["revision"], outcome="o",
                                       restoration="r")
        code, output, _error = self.cli("take", "iPhone", "--owner", "session-d", "--purpose", "p",
                                        "--release-by", later(), "--approved-by", "owner", "--approved-at", later(-1),
                                        "--approved-until", later(60))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["entry"]["ownerChatID"], "session-d")

    def test_either_device_has_one_holder_at_a_time(self):
        for key, device, window in (("iPad", TABLET, None), ("iPhone", PHONE, approval())):
            held = self.reserve(key, device=device)
            with self.assertRaisesRegex(devices.Refused, "held by session-a"):
                self.record.reserve(key, owner="session-b", revision=held["revision"], purpose="p",
                                    release_by=later(), approval=approval())
            with self.assertRaisesRegex(TimeoutError, "nothing was reserved"):
                self.record.take(key, owner="session-b", purpose="p", release_by=later(), timeout=0.05, poll=0.01,
                                 said=lambda _text: None, approval=window)
            self.assertEqual(self.record.entry(key), held)
            self.assertEqual(devices.waiting(self.record.load(), key), [])

    def test_an_orphaned_device_is_not_taken(self):
        held = self.reserve("iPad", device=TABLET)
        gone = self.waiter()
        self.record.update("iPad", owner="session-a", revision=held["revision"], activeRun=True,
                           hostProcessIdentifier=gone.pid)
        gone.kill()
        gone.wait()
        with self.assertRaisesRegex(devices.Refused, "orphan.*attended recovery"):
            self.record.take("iPad", owner="session-b", purpose="p", release_by=later(), timeout=stretched(5), poll=0.01,
                             said=lambda _text: None)
        self.assertEqual(devices.waiting(self.record.load(), "iPad"), [])

    def test_a_release_says_how_the_apps_the_session_launched_were_quit(self):
        entry = self.reserve("iPad", device=TABLET)
        code, output, _error = self.cli("update", "iPad", "--owner", "session-a", "--revision", str(entry["revision"]),
                                        "--launched", "com.example.Host", "--launched", "com.example.Host")
        self.assertEqual(code, 0)
        entry = json.loads(output)["entry"]
        self.assertEqual(entry["launchedApps"], ["com.example.Host"])
        self.assertIn("launched apps: com.example.Host", self.cli("status")[1])
        with self.assertRaisesRegex(ValueError, "writes launchedApps itself"):
            self.record.update("iPad", owner="session-a", revision=entry["revision"], launchedApps=[])
        with self.assertRaisesRegex(devices.Refused, "launched apps com.example.Host.*--apps-quit"):
            self.record.release("iPad", owner="session-a", revision=entry["revision"], outcome="o", restoration="r")
        released = self.record.release("iPad", owner="session-a", revision=entry["revision"], outcome="o",
                                       restoration="r", apps_quit="devicectl terminated com.example.Host")
        self.assertEqual(released["appsQuit"], "devicectl terminated com.example.Host")
        # The next holder's entry starts without them, and keeps them in its previous lease.
        taken = self.reserve("iPad", owner="session-b", revision=released["revision"], approval=approval())
        self.assertNotIn("launchedApps", taken)
        self.assertEqual(taken["previousLease"]["launchedApps"], ["com.example.Host"])


class BoundedRunTests(unittest.TestCase):
    def setUp(self):
        temp = scratch()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = self.root / "device-handoff.json"
        self.record = devices.Record(self.path)
        self.marker = self.root / "ran"
        patcher = patch.dict(os.environ, {devices.OVERRIDE: str(self.path)})
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in (devices.OWNER, devices.LEASE, "CODEX_THREAD_ID", "CLAUDE_CODE_SESSION_ID"):
            os.environ.pop(name, None)

    def hold(self, key="iPad", device=TABLET, owner="session-a", release_by=None, **options):
        """A reservation within the owner's approval of a window until it ends."""
        entry = self.record.entry(key)
        release_by = release_by or later()
        options.setdefault("approval", approval(until=release_by))
        return self.record.reserve(key, owner=owner, revision=(entry or {}).get("revision", 0), purpose="bounded run",
                                   release_by=release_by, device=device, **options)

    def mark(self, *words):
        """A command that records that it ran, naming words."""
        return [sys.executable, "-c", "import sys; open(sys.argv[1], 'a').write('ran')", str(self.marker), *words]

    def cli(self, *arguments):
        with contextlib.redirect_stdout(StringIO()) as output, contextlib.redirect_stderr(StringIO()) as error:
            code = devices.main(list(arguments))
        return code, output.getvalue(), error.getvalue()

    def test_a_run_on_the_held_device_is_recorded_in_its_entry_which_stays_reserved(self):
        held = self.hold()
        during = self.root / "during.json"
        # The bound is read from the clock when the run begins, between these two readings however slow the host
        # (#119).
        keep = devices.TERMINATION + devices.RESTORATION
        before = (devices.moment(held["expectedReleaseBy"]) - datetime.now(timezone.utc)).total_seconds()
        # The command may name its own device in any case.
        code, _output, error = self.cli("run", "iPad", "--owner", "session-a", "--", sys.executable, "-c", PROBE,
                                        str(self.path), "iPad", str(during), f"id={TABLET.lower()}")
        after = (devices.moment(held["expectedReleaseBy"]) - datetime.now(timezone.utc)).total_seconds()
        self.assertEqual(code, 0, error)
        self.assertIn("exited with status 0; iPad stays reserved", error)
        running = json.loads(during.read_text())
        self.assertTrue(running["activeRun"])
        self.assertEqual(running["hostProcessIdentifier"], os.getpid())
        [command] = running["ownedProcesses"]
        self.assertNotEqual(command["pid"], os.getpid())
        entry = self.record.entry("iPad")
        self.assertEqual(devices.condition(entry), "held")
        self.assertEqual((entry["ownerChatID"], entry["state"], entry["run"]["outcome"]), ("session-a", "reserved", "exit 0"))
        self.assertEqual(entry["run"]["command"][:2], [sys.executable, "-c"])
        self.assertLessEqual(entry["run"]["boundSeconds"], before - keep)
        self.assertGreater(entry["run"]["boundSeconds"], after - keep - 1)
        self.assertEqual(entry["revision"], held["revision"] + 3)
        self.assertFalse(devices.running(entry))
        # The command's status is the run's, and the device stays reserved after a failure too.
        code, _output, _error = self.cli("run", "iPad", "--owner", "session-a", "--", sys.executable, "-c",
                                         "raise SystemExit(3)")
        self.assertEqual(code, 3)
        entry = self.record.entry("iPad")
        self.assertEqual((devices.condition(entry), entry["run"]["outcome"]), ("held", "exit 3"))

    def test_a_command_finds_its_process_recorded_however_late_the_record_comes(self):
        self.hold()
        during = self.root / "during.json"
        real = devices.BoundedRun.own
        def late(bounded, pid):
            # As a busy host can delay it: the record waits for the command to have read the entry,
            # which it must not get to do before the record names it (#94).
            deadline = time.monotonic() + stretched(2)
            while not during.exists() and time.monotonic() < deadline: time.sleep(0.01)
            real(bounded, pid)
        with patch.object(devices.BoundedRun, "own", late):
            code, _output, error = self.cli("run", "iPad", "--owner", "session-a", "--", sys.executable, "-c", PROBE,
                                            str(self.path), "iPad", str(during))
        self.assertEqual(code, 0, error)
        [command] = json.loads(during.read_text())["ownedProcesses"]
        self.assertNotEqual(command["pid"], os.getpid())

    def test_a_launcher_that_dies_before_recording_its_command_leaves_it_unrun(self):
        # Recovery trusts ownedProcesses to know what still runs, so a command nothing records must not run.
        self.hold()
        started = self.root / "root"
        script = ("import os, signal, sys; from vocaro_engineering import devices\n"
                  "def own(_bounded, pid):\n"
                  f"    open({str(started)!r}, 'w').write(str(pid)); os.kill(os.getpid(), signal.SIGKILL)\n"
                  "devices.BoundedRun.own = own\n"
                  "devices.main(sys.argv[1:])\n")
        launcher = subprocess.run([sys.executable, "-c", script, "run", "iPad", "--owner", "session-a", "--", *self.mark()],
                                  env={**os.environ, "PYTHONPATH": str(SOURCE)}, capture_output=True, text=True,
                                  timeout=stretched(60))
        self.assertEqual(launcher.returncode, -signal.SIGKILL, launcher.stderr)
        root = int(started.read_text())
        deadline = time.monotonic() + stretched(30)
        while process_start(root) is not None and time.monotonic() < deadline: time.sleep(0.05)
        self.assertIsNone(process_start(root))
        self.assertFalse(self.marker.exists())
        entry = self.record.entry("iPad")
        self.assertNotIn("ownedProcesses", entry)
        self.assertEqual(devices.condition(entry), "orphan")

    def test_a_command_that_names_another_device_is_refused_and_runs_nothing(self):
        self.hold()
        self.hold("iPhone", device=PHONE, owner="session-b")
        self.hold("Example Duo 1", device=DUO, owner="session-b")
        # Another entry's device, by destination or option, a hardware UDID no entry holds, and another
        # entry's simulator are each a device the run does not hold.
        for word, holder in [(f"platform=iOS,id={PHONE}", "iPhone"), (PHONE.lower(), "iPhone"),
                             ("00008020-0011223344556677", None), ("0123456789abcdef0123456789abcdef01234567", None),
                             (DUO, "Example Duo 1")]:
            with self.subTest(word):
                before = self.path.read_bytes()
                code, _output, error = self.cli("run", "iPad", "--owner", "session-a", "--", *self.mark(word))
                self.assertEqual(code, 2)
                udid = word.upper().rpartition("=")[2]
                self.assertIn(f"the command names {udid}" + (f", {holder}'s device," if holder else " but"), error)
                self.assertIn(f"holds only iPad ({TABLET})", error)
                self.assertFalse(self.marker.exists())
                self.assertEqual(self.path.read_bytes(), before)
        # Positive controls: a UUID no entry holds, such as a store's, and a SHA-256 are not devices.
        code, _output, error = self.cli("run", "iPad", "--owner", "session-a", "--",
                                        *self.mark("C6E2A1F4-1D2B-4E5F-8A9B-0C1D2E3F4A5B", "ab" * 32, TABLET))
        self.assertEqual(code, 0, error)
        self.assertEqual(self.marker.read_text(), "ran")

    def test_a_code_signing_identity_is_no_device_where_a_signing_setting_or_option_takes_it(self):
        # Invented: a signing identity's SHA-1 and an older device's UDID, both forty hex digits (#86).
        identity, older = "96DAB4C8A3D308C37BCC5C7E987AB6FE05F16008", "AB12" * 10
        for words in [["env", f"SIGN_IDENTITY={identity}", "bash", "probe.sh"],
                      ["xcodebuild", f'CODE_SIGN_IDENTITY="{identity.lower()}"'],
                      ["codesign", "--force", "-s", identity, "App.app"], ["codesign", f"--sign={identity}"],
                      ["bash", "-c", f"codesign -f -s '{identity}' App.app"],
                      ["security", "find-certificate", "--identity", identity]]:
            with self.subTest(words):
                self.assertEqual(devices.named(words), set())
        # Negative controls: the same digits anywhere else, -s outside codesign's command, a shorter
        # UDID a signing setting names, and a UDID beside a signing identity.
        for words, udid in [(["echo", identity], identity), (["adb", "-s", identity], identity),
                            (["echo", "codesign;", "tool", "-s", identity], identity),
                            ([f"platform=iOS,id={identity}"], identity), (["IDENTITY", identity], identity),
                            ([f"SIGN_IDENTITY={PHONE}"], PHONE),
                            (["env", f"SIGN_IDENTITY={identity}", "probe", f"id={older}"], older)]:
            with self.subTest(words):
                self.assertEqual(devices.named(words), {udid})
        # A device the record knows is named wherever it appears.
        self.assertEqual(devices.named([f"SIGN_IDENTITY={identity}"], [identity.lower()]), {identity})

    def test_only_the_holder_with_no_run_active_within_the_owners_approval_may_run(self):
        command = ["--", *self.mark()]
        def refused(message, *arguments, key="iPad", owner="session-a"):
            code, _output, error = self.cli("run", key, *(["--owner", owner] if owner else []), *arguments, *command)
            self.assertEqual(code, 2)
            self.assertRegex(error, message)
            self.assertFalse(self.marker.exists())
        refused("iPad: the device record shows it absent, not held by session-a")
        held = self.hold(owner="session-b")
        refused("shows it held by session-b, not held by session-a")
        refused("name the session that holds iPad", owner=None)
        self.record.release("iPad", owner="session-b", revision=held["revision"], outcome="o", restoration="r")
        refused("shows it released")
        held = self.hold()
        refused("at revision .*, not 1: it changed after the session verified it", "--revision", "1")
        child = live()
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        active = self.record.update("iPad", owner="session-a", revision=held["revision"], activeRun=True,
                                    hostProcessIdentifier=child.pid)
        refused("shows it active by session-a")
        child.kill()
        child.wait()
        refused("shows it orphan by session-a")
        self.record.recover("iPad", by="session-a", revision=active["revision"], verified="the run ended")
        self.hold("Example Duo 1", device=DUO)
        refused(f"holds {DUO}, not a physical device", key="Example Duo 1")
        self.hold()
        with patch.object(devices, "datetime", Later):
            refused("the owner's approval of iPad ended at")
        # An entry from before reservations recorded the window has none to run in.
        value = json.loads(self.path.read_text())
        del value["devices"]["iPad"]["approval"]
        self.path.write_text(json.dumps(value))
        refused("the owner's approval of iPad was never recorded")
        value["devices"]["iPad"]["approval"] = approval(until=value["devices"]["iPad"]["expectedReleaseBy"])
        self.path.write_text(json.dumps(value))
        # Positive control: the same reservation, within its approval.
        code, _output, error = self.cli("run", "iPad", "--owner", "session-a", *command)
        self.assertEqual(code, 0, error)
        self.assertTrue(self.marker.exists())
        # The session's own name comes from the environment too.
        with patch.dict(os.environ, {devices.OWNER: "session-a"}):
            self.assertEqual(self.cli("run", "iPad", *command)[0], 0)

    def test_the_bound_is_the_reservations_remaining_time_less_the_reserve_and_ends_the_command(self):
        keep = devices.TERMINATION + devices.RESTORATION
        self.hold(release_by=later((keep + 300) / 60))
        bounded = devices.BoundedRun(["iPad"], "session-a", [], record=self.record)
        self.assertAlmostEqual(bounded.seconds(), 300, delta=stretched(5))
        self.assertEqual(devices.BoundedRun(["iPad"], "session-a", [], record=self.record, limit=40).seconds(), 40)
        self.assertAlmostEqual(devices.BoundedRun(["iPad"], "session-a", [], record=self.record,
                                                  restoration=0).seconds(), 300 + devices.RESTORATION, delta=stretched(5))
        with self.assertRaisesRegex(ValueError, "at least 30s"):
            devices.BoundedRun(["iPad"], "session-a", [], record=self.record, limit=10)
        # A later expected release leaves the run within the owner's approval, which ends first.
        entry = self.record.entry("iPad")
        self.record.update("iPad", owner="session-a", revision=entry["revision"], expectedReleaseBy=later(600))
        self.assertAlmostEqual(devices.BoundedRun(["iPad"], "session-a", [], record=self.record).seconds(), 300, delta=stretched(5))
        # Too little time left refuses the run, and nothing runs.
        entry = self.record.entry("iPad")
        self.record.update("iPad", owner="session-a", revision=entry["revision"], expectedReleaseBy=later((keep + 10) / 60))
        code, _output, error = self.cli("run", "iPad", "--owner", "session-a", "--", *self.mark())
        self.assertEqual(code, 2)
        self.assertRegex(error, r"leaving \d+s for a run after the \d+s kept for its termination and restoration; "
                                r"a run needs 30s")
        # The 10 s left less what passed since it was set, a span stretched by load.
        left = int(re.search(r"leaving (\d+)s", error).group(1))
        self.assertTrue(10 - stretched(1) <= left <= 10, error)
        self.assertFalse(self.marker.exists())
        # A command that outlasts its bound is stopped, and its outcome recorded; the device stays reserved.
        entry = self.record.update("iPad", owner="session-a", revision=self.record.entry("iPad")["revision"],
                                   expectedReleaseBy=later(30))
        with patch.object(devices, "MINIMUM", 1):
            started = datetime.now(timezone.utc)
            code, _output, error = self.cli("run", "iPad", "--owner", "session-a", "--limit", "1", "--",
                                            sys.executable, "-c", "import time; time.sleep(60)")
        self.assertEqual(code, devices.TIMED_OUT, error)
        # The 1 s limit is the behavior under test and stays fixed; only the ceiling on the stop stretches.
        self.assertLess((datetime.now(timezone.utc) - started).total_seconds(), stretched(30))
        entry = self.record.entry("iPad")
        self.assertEqual(devices.condition(entry), "held")
        self.assertIn("timed out after 1s", entry["run"]["outcome"])
        self.assertEqual(entry["run"]["boundSeconds"], 1)

    def test_a_command_that_cannot_start_leaves_no_active_run(self):
        self.hold()
        code, _output, error = self.cli("run", "iPad", "--owner", "session-a", "--", str(self.root / "missing-tool"))
        self.assertEqual(code, 2)
        self.assertIn("did not start", error)
        entry = self.record.entry("iPad")
        self.assertEqual(devices.condition(entry), "held")
        self.assertIn("did not start", entry["run"]["outcome"])

    def test_an_unconfirmed_end_keeps_the_run_active_and_its_entry_uncertain(self):
        self.hold()
        child = live()
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        def stuck(command, *, cwd, stdout, timeout, on_start):
            on_start(child.pid)
            raise RuntimeError("Timed out, but owned descendants have no confirmed terminal result; reservation retained")
        bounded = devices.BoundedRun(["iPad"], "session-a", [], record=self.record)
        with patch.object(devices.processes, "supervise", side_effect=stuck), \
                self.assertRaisesRegex(RuntimeError, "no confirmed terminal result"):
            bounded.execute(["xcodebuild"])
        entry = self.record.entry("iPad")
        self.assertEqual((entry["ownerChatID"], entry["activeRun"], entry["uncertain"]), ("session-a", True, True))
        self.assertEqual(devices.condition(entry), "uncertain")
        self.assertIn("the run stays active for attended recovery", entry["run"]["outcome"])
        self.assertEqual([item["pid"] for item in devices.running(entry)], [child.pid])
        with self.assertRaisesRegex(devices.Refused, "active run"):
            self.record.release("iPad", owner="session-a", revision=entry["revision"], outcome="o", restoration="r")
        self.assertEqual(bounded.revisions["iPad"], entry["revision"])

    def test_a_stop_ends_the_command_and_is_recorded_as_the_runs_outcome(self):
        self.hold()
        started = self.root / "started"
        launcher = subprocess.Popen(
            [sys.executable, "-m", "vocaro_engineering.devices", "run", "iPad", "--owner", "session-a", "--",
             sys.executable, "-c", f"import sys, time; open(sys.argv[1], 'w').close(); time.sleep({stretched(60)})",
             str(started)],
            env={**os.environ, "PYTHONPATH": str(SOURCE)}, stderr=subprocess.PIPE, text=True)
        self.addCleanup(launcher.kill)
        # The command can start before the launcher's record names it, so both are awaited.
        recorded = lambda: started.exists() and "ownedProcesses" in self.record.entry("iPad")
        deadline = time.monotonic() + stretched(30)
        while not recorded() and launcher.poll() is None and time.monotonic() < deadline: time.sleep(0.05)
        self.assertTrue(recorded(), launcher.stderr.read() if launcher.poll() is not None else "not started")
        [command] = self.record.entry("iPad")["ownedProcesses"]
        launcher.send_signal(signal.SIGTERM)
        _output, error = launcher.communicate(timeout=stretched(60))
        self.assertEqual(launcher.returncode, -signal.SIGTERM, error)
        entry = self.record.entry("iPad")
        self.assertEqual(devices.condition(entry), "held")
        self.assertIn("Stopped by SIGTERM", entry["run"]["outcome"])
        # The stop ended the command before the launcher recorded the outcome.
        self.assertNotEqual(process_start(command["pid"]), command["start"])

    def test_on_the_second_mac_a_run_takes_its_reservation_from_the_lease_and_changes_no_record(self):
        self.record.defer("workstation")
        lease = {"record": "workstation", "devices": {"iPad": TABLET}, "until": later()}
        def run(*words, key="iPad", carried=lease):
            environment = {devices.LEASE: json.dumps(carried)} if carried is not None else {}
            with patch.dict(os.environ, environment):
                return self.cli("run", key, "--", *self.mark(*words))
        before = self.path.read_bytes()
        refusals = [(run(key="iPhone"), "the run's lease carries iPad, not iPhone"),
                    (run(PHONE), f"the command names {PHONE} but the run holds only iPad"),
                    (run(carried={**lease, "record": "another-mac"}), "lease is from another-mac's record"),
                    (run(carried={**lease, "until": later(2)}), "leaving 0s for a run"),
                    (run(carried=None), "physical devices are reserved in workstation's device record")]
        for (code, _output, error), message in refusals:
            with self.subTest(message):
                self.assertEqual(code, 2)
                self.assertIn(message, error)
        self.assertFalse(self.marker.exists())
        code, _output, error = run(TABLET)
        self.assertEqual(code, 0, error)
        self.assertTrue(self.marker.exists())
        self.assertEqual(self.path.read_bytes(), before)


class Devicectl:
    """devicectl as the notification shortcuts reach it: each command it is given, answered by answers in turn."""

    def __init__(self, *answers):
        self.answers, self.commands = list(answers), []

    def __call__(self, command):
        self.commands.append(command)
        return self.answers.pop(0) if self.answers else (0, "")

    def shortcuts(self):
        """The shortcut each command had Shortcuts run, with the device it named."""
        named = []
        for command in self.commands:
            named.append((command[command.index("--device") + 1], command[command.index("--payload-url") + 1],
                          command[-1]))
        return named


class NotificationTests(unittest.TestCase):
    """The owner's notifications during a run on a paired device (#144)."""

    def setUp(self):
        temp = scratch()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = self.root / "device-handoff.json"
        self.record = devices.Record(self.path)
        self.device = Devicectl()
        for patcher in (patch.dict(os.environ, {devices.OVERRIDE: str(self.path)}),
                        patch.object(device_notifications, "run", self.device)):
            patcher.start()
            self.addCleanup(patcher.stop)
        for name in (devices.OWNER, devices.LEASE, "CODEX_THREAD_ID", "CLAUDE_CODE_SESSION_ID"):
            os.environ.pop(name, None)

    def cli(self, *arguments):
        with contextlib.redirect_stdout(StringIO()) as output, contextlib.redirect_stderr(StringIO()) as error:
            code = devices.main(list(arguments))
        return code, output.getvalue(), error.getvalue()

    def first(self, key="iPad", device=TABLET):
        """key's first reservation, which records its device, released again."""
        entry = self.record.reserve(key, owner="first", revision=0, purpose="p", release_by=later(), device=device,
                                    approval=approval())
        return self.record.release(key, owner="first", revision=entry["revision"], outcome="o", restoration="r")

    def setup(self, key="iPad"):
        code, output, error = self.cli("notifications", "setup", key, "--by", "owner", "--at", later(-1),
                                       "--words", "the shortcuts are on the iPad")
        self.assertEqual(code, 0, error)
        return json.loads(output)["setup"]

    def take(self, key="iPad", owner="session-a"):
        window = () if key == "iPad" else ("--approved-by", "owner", "--approved-at", later(-1),
                                           "--approved-until", later(60))
        code, output, error = self.cli("take", key, "--owner", owner, "--purpose", "VoiceOver run",
                                       "--release-by", later(), *window)
        self.assertEqual(code, 0, error)
        return json.loads(output)["entry"], error

    def silence_url(self):
        return "shortcuts://run-shortcut?name=Vocaro%20Silence%20Notifications"

    def restore_url(self):
        return "shortcuts://run-shortcut?name=Vocaro%20Restore%20Notifications"

    def test_the_shortcuts_are_run_by_url_through_devicectl_on_the_named_device(self):
        self.assertEqual(device_notifications.url(device_notifications.SILENCE), self.silence_url())
        self.assertEqual(device_notifications.command(TABLET, device_notifications.RESTORE),
                         ["xcrun", "devicectl", "--timeout", str(device_notifications.TIMEOUT), "device", "process",
                          "launch", "--device", TABLET, "--payload-url", self.restore_url(), "com.apple.shortcuts"])
        self.assertEqual(device_notifications.trigger(TABLET, device_notifications.SILENCE)[0], True)
        self.device.answers.append((1, "ERROR: The device has not been unlocked recently"))
        opened, said = device_notifications.trigger(TABLET, device_notifications.SILENCE)
        self.assertFalse(opened)
        self.assertIn("(exit 1): ERROR: The device has not been unlocked recently", said)
        self.device.answers.append((None, "no answer within 150s"))
        self.assertIn("it did not finish", device_notifications.trigger(TABLET, device_notifications.SILENCE)[1])

    def test_a_take_silences_the_device_and_its_release_puts_it_back(self):
        self.first()
        self.setup()
        entry, error = self.take()
        self.assertEqual(self.device.shortcuts(), [(TABLET, self.silence_url(), "com.apple.shortcuts")])
        notifications = entry["notifications"]
        self.assertEqual((notifications["silence"], notifications["restore"], notifications["confirmed"]),
                         ("requested", "pending", False))
        self.assertEqual(entry["launchedApps"], ["com.apple.shortcuts"])
        self.assertIn("not confirmed", error)
        self.assertIn("silencing shortcut run, not confirmed", self.cli("status")[1])
        # A session that releases through the record alone, skipping the restore, is refused.
        with self.assertRaisesRegex(devices.Refused, "not yet put back"):
            self.record.release("iPad", owner="session-a", revision=entry["revision"], outcome="o", restoration="r",
                                apps_quit="q")
        code, output, error = self.cli("release", "iPad", "--owner", "session-a", "--revision", str(entry["revision"]),
                                       "--outcome", "exit 0", "--restoration", "verified", "--apps-quit", "terminated")
        self.assertEqual(code, 0, error)
        released = json.loads(output)["entry"]
        self.assertEqual(self.device.shortcuts()[1], (TABLET, self.restore_url(), "com.apple.shortcuts"))
        self.assertEqual((released["state"], released["notifications"]["restore"]), ("released", "done"))
        self.assertNotIn("notifications", self.cli("status")[1].split("iPad: released")[1].split("\n")[0])

    def test_a_failed_run_still_puts_the_notifications_back(self):
        self.first()
        self.setup()
        entry, _error = self.take()
        code, _output, _error = self.cli("run", "iPad", "--owner", "session-a", "--", sys.executable, "-c",
                                         "raise SystemExit(3)")
        self.assertEqual(code, 3)
        entry = self.record.entry("iPad")
        self.assertEqual((entry["run"]["outcome"], entry["run"]["notifications"]), ("exit 3", "requested"))
        code, output, error = self.cli("release", "iPad", "--owner", "session-a", "--revision", str(entry["revision"]),
                                       "--outcome", "exit 3", "--restoration", "r", "--apps-quit", "terminated")
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)["entry"]["notifications"]["restore"], "done")
        self.assertEqual([url for _device, url, _app in self.device.shortcuts()],
                         [self.silence_url(), self.restore_url()])

    def test_a_run_whose_process_died_is_put_back_at_attended_recovery(self):
        self.first()
        self.setup()
        entry, _error = self.take()
        gone = live()
        entry = self.record.update("iPad", owner="session-a", revision=entry["revision"], activeRun=True,
                                   hostProcessIdentifier=gone.pid)
        gone.kill()
        gone.wait()
        self.assertEqual(devices.condition(entry), "orphan")
        code, output, error = self.cli("recover", "iPad", "--by", "session-b", "--revision", str(entry["revision"]),
                                       "--verified", "no owned processes")
        self.assertEqual(code, 0, error)
        recovered = json.loads(output)["entry"]
        self.assertEqual((recovered["state"], recovered["notifications"]["restore"]), ("released", "done"))
        self.assertEqual(self.device.shortcuts()[-1][1], self.restore_url())

    def test_a_recovery_refused_while_the_run_lives_runs_no_shortcut(self):
        self.first()
        self.setup()
        entry, _error = self.take()
        child = live()
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        entry = self.record.update("iPad", owner="session-a", revision=entry["revision"], activeRun=True,
                                   hostProcessIdentifier=child.pid)
        code, _output, error = self.cli("recover", "iPad", "--by", "session-b", "--revision", str(entry["revision"]),
                                        "--verified", "v")
        self.assertEqual(code, 2)
        self.assertIn("still running", error)
        self.assertEqual(len(self.device.shortcuts()), 1)

    def test_a_restore_that_fails_is_recorded_and_said_and_the_release_goes_on(self):
        self.first()
        self.setup()
        entry, _error = self.take()
        self.device.answers.append((1, "ERROR: The device is unavailable"))
        code, output, error = self.cli("release", "iPad", "--owner", "session-a", "--revision", str(entry["revision"]),
                                       "--outcome", "o", "--restoration", "r", "--apps-quit", "terminated")
        self.assertEqual(code, 0, error)
        released = json.loads(output)["entry"]
        self.assertEqual(released["notifications"]["restore"], "failed")
        self.assertIn("Do Not Disturb may still be on there: tell the owner", error)
        self.assertIn("not put back after the last run", self.cli("status")[1])

    def test_without_the_owners_setup_nothing_runs_and_the_session_is_told_what_to_ask(self):
        self.first()
        entry, error = self.take()
        self.assertEqual(self.device.commands, [])
        # Nothing was done, so the entry records nothing.
        self.assertNotIn("notifications", entry)
        self.assertIn("not silenced: the owner has recorded no notification shortcuts on iPad's device", error)
        self.assertIn("in the same message as the window, to turn on Do Not Disturb", error)
        self.assertIn("notifications: not silenced (nothing was done)", self.cli("status")[1])
        # The run says so before it starts, and records it.
        code, _output, error = self.cli("run", "iPad", "--owner", "session-a", "--", sys.executable, "-c", "pass")
        self.assertEqual(code, 0)
        self.assertIn("iPad's notifications are not silenced (nothing was done)", error)
        entry = self.record.entry("iPad")
        self.assertEqual(entry["run"]["notifications"], "nothing was done")
        # The owner turning Do Not Disturb on himself, recorded, lets the run start quietly; release runs nothing.
        code, output, error = self.cli("notifications", "by-owner", "iPad", "--owner", "session-a", "--revision",
                                       str(entry["revision"]), "--by", "owner", "--at", later(-1))
        self.assertEqual(code, 0, error)
        entry = json.loads(output)["entry"]
        code, _output, error = self.cli("run", "iPad", "--owner", "session-a", "--", sys.executable, "-c", "pass")
        self.assertNotIn("not silenced", error)
        entry = self.record.entry("iPad")
        code, _output, error = self.cli("release", "iPad", "--owner", "session-a", "--revision", str(entry["revision"]),
                                        "--outcome", "o", "--restoration", "r")
        self.assertEqual(code, 0, error)
        self.assertEqual(self.device.commands, [])

    def test_a_silencing_that_fails_is_said_still_put_back_and_can_be_tried_again(self):
        self.first()
        self.setup()
        self.device.answers.append((1, "ERROR: The device has not been unlocked recently"))
        entry, error = self.take()
        self.assertEqual((entry["notifications"]["silence"], entry["notifications"]["restore"]), ("failed", "pending"))
        self.assertIn("not silenced: devicectl could not open Shortcuts", error)
        code, output, error = self.cli("notifications", "silence", "iPad", "--owner", "session-a", "--revision",
                                       str(entry["revision"]))
        self.assertEqual(code, 0, error)
        entry = json.loads(output)["entry"]
        self.assertEqual(entry["notifications"]["silence"], "requested")
        self.assertEqual([url for _device, url, _app in self.device.shortcuts()], [self.silence_url()] * 2)
        # Silenced already, asking again runs nothing.
        code, output, _error = self.cli("notifications", "silence", "iPad", "--owner", "session-a", "--revision",
                                        str(entry["revision"]))
        self.assertEqual((code, len(self.device.commands)), (0, 2))

    def notifications_command(self, action, entry, key="iPad"):
        code, output, error = self.cli("notifications", action, key, "--owner", "session-a", "--revision",
                                       str(entry["revision"]))
        self.assertEqual(code, 0, error)
        return json.loads(output)["entry"]

    def release_cli(self, entry, key="iPad"):
        code, output, error = self.cli("release", key, "--owner", "session-a", "--revision", str(entry["revision"]),
                                       "--outcome", "o", "--restoration", "r", "--apps-quit", "terminated")
        self.assertEqual(code, 0, error)
        return json.loads(output)["entry"]

    def test_a_silencing_after_a_restore_in_one_hold_is_put_back_again_at_release(self):
        self.first()
        self.setup()
        entry, _error = self.take()
        entry = self.notifications_command("restore", entry)
        self.assertEqual(entry["notifications"]["restore"], "done")
        entry = self.notifications_command("silence", entry)
        self.assertEqual((entry["notifications"]["silence"], entry["notifications"]["restore"]),
                         ("requested", "pending"))
        # Pending again, so the record alone refuses a release that skips the restore.
        with self.assertRaisesRegex(devices.Refused, "not yet put back"):
            self.record.release("iPad", owner="session-a", revision=entry["revision"], outcome="o", restoration="r",
                                apps_quit="q")
        released = self.release_cli(entry)
        self.assertEqual((released["state"], released["notifications"]["restore"]), ("released", "done"))
        self.assertEqual([url for _device, url, _app in self.device.shortcuts()],
                         [self.silence_url(), self.restore_url(), self.silence_url(), self.restore_url()])

    def test_a_silencing_after_a_restore_in_one_hold_is_put_back_again_at_recovery(self):
        self.first()
        self.setup()
        entry, _error = self.take()
        entry = self.notifications_command("silence", self.notifications_command("restore", entry))
        gone = live()
        entry = self.record.update("iPad", owner="session-a", revision=entry["revision"], activeRun=True,
                                   hostProcessIdentifier=gone.pid)
        gone.kill()
        gone.wait()
        code, output, error = self.cli("recover", "iPad", "--by", "session-b", "--revision", str(entry["revision"]),
                                       "--verified", "no owned processes")
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)["entry"]["notifications"]["restore"], "done")
        self.assertEqual([url for _device, url, _app in self.device.shortcuts()],
                         [self.silence_url(), self.restore_url(), self.silence_url(), self.restore_url()])

    def test_a_restore_with_no_silencing_after_it_is_not_run_again_at_release(self):
        self.first()
        self.setup()
        entry, _error = self.take()
        entry = self.notifications_command("restore", entry)
        # Restoring again with nothing pending runs nothing.
        entry = self.notifications_command("restore", entry)
        released = self.release_cli(entry)
        self.assertEqual((released["state"], released["notifications"]["restore"]), ("released", "done"))
        self.assertEqual([url for _device, url, _app in self.device.shortcuts()],
                         [self.silence_url(), self.restore_url()])

    def test_the_setup_belongs_to_the_device_it_was_made_on(self):
        with self.assertRaisesRegex(devices.Refused, "records no physical device"):
            self.record.notification_setup("iPad", by="owner", at=later(-1))
        with self.assertRaisesRegex(ValueError, "past ISO 8601"):
            self.record.notification_setup("iPad", by="owner", at=later(5))
        self.first()
        self.assertEqual(self.setup()["device"], TABLET)
        # Another device under the key, as after a replacement, is not covered.
        self.record.load()
        value = json.loads(self.path.read_text())
        value[devices.SETUP]["iPad"]["device"] = PHONE
        self.path.write_text(json.dumps(value))
        entry, error = self.take()
        self.assertEqual(self.device.commands, [])
        self.assertIn("recorded on another device", error)
        self.assertNotIn("notifications", entry)

    def test_a_simulator_or_the_mac_foreground_is_never_silenced(self):
        self.cli("notifications", "setup", "Example Duo 1", "--by", "owner", "--at", later(-1))
        code, output, error = self.cli("reserve", "Example Duo 1", "--owner", "s", "--revision", "0", "--purpose", "p",
                                       "--release-by", later(), "--device", DUO)
        self.assertEqual(code, 0, error)
        self.assertNotIn("notifications", json.loads(output)["entry"])
        self.assertEqual(self.device.commands, [])


LOGS = Path(__file__).resolve().parent / "fixtures/DeviceLog"


def alive(pid):
    try: os.kill(pid, 0)
    except ProcessLookupError: return False
    return True


class Watches:
    """device_notifications.watch as a run reaches it: each watch's stop answers what it saw, in turn."""

    def __init__(self, *seen):
        self.seen, self.devices = list(seen), []

    def __call__(self, udid):
        self.devices.append(udid)
        seen = self.seen.pop(0) if self.seen else {"watched": True, "presented": 0}
        return type("Watch", (), {"stop": lambda _self: seen})()


BANNER = {"watched": True, "presented": 1, "first": "00:52:13"}


class NotificationWatchTests(unittest.TestCase):
    """A notification that reaches the device during a run despite its silencing (#144, item 2)."""

    def setUp(self):
        temp = scratch()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = self.root / "device-handoff.json"
        self.record = devices.Record(self.path)
        for patcher in (patch.dict(os.environ, {devices.OVERRIDE: str(self.path)}),
                        patch.object(device_notifications, "run", Devicectl())):
            patcher.start()
            self.addCleanup(patcher.stop)
        for name in (devices.OWNER, devices.LEASE, "CODEX_THREAD_ID", "CLAUDE_CODE_SESSION_ID"):
            os.environ.pop(name, None)
        entry = self.record.reserve("iPad", owner="session-a", revision=0, purpose="p", release_by=later(),
                                    device=TABLET, approval=approval())
        self.revision = entry["revision"]
        self.runs = self.root / "runs"

    def cli(self, *arguments):
        with contextlib.redirect_stdout(StringIO()) as output, contextlib.redirect_stderr(StringIO()) as error:
            code = devices.main(list(arguments))
        return code, output.getvalue(), error.getvalue()

    def run_with(self, watches, status=0):
        """`devices run` of a command that counts its runs and exits with status, watched by watches."""
        with patch.object(device_notifications, "watch", watches):
            return self.cli("run", "iPad", "--owner", "session-a", "--", sys.executable, "-c",
                            f"open({str(self.runs)!r}, 'a').write('x'); raise SystemExit({status})")

    def count(self):
        return len(self.runs.read_text()) if self.runs.exists() else 0

    def test_springboard_presenting_a_banner_is_found_and_nothing_else_is(self):
        banner = (LOGS / "springboard-banner.log").read_text()
        quiet = (LOGS / "springboard-quiet.log").read_text()
        # The owner's iPhone's lines (iOS 27.2): one banner, by the clock it appeared at, and not its posting or
        # its "will appear".
        self.assertEqual(device_notifications.presentations(banner), ["17:03:49"])
        self.assertTrue(device_notifications.lines(banner))
        # A notification Do Not Disturb kept from a banner, though posted to the banner destination, is none.
        self.assertIn("SBNotificationBannerDestination", quiet)
        self.assertIn("do not disturb: 1", quiet)
        self.assertEqual(device_notifications.presentations(quiet), [])
        self.assertTrue(device_notifications.lines(quiet))
        self.assertFalse(device_notifications.lines("[connected:00008130-001A2C3E0C38001C]\n"))
        self.assertFalse(device_notifications.lines(
            "Waiting for device with UDID 00008150-00164DE1342B401C to become available...\n"))
        # Another process saying the same words is none.
        other = banner.replace(" SpringBoard[", " bulletinboard[")
        self.assertEqual(device_notifications.presentations(other), [])
        # A relay that puts the device's name, spaces and all, before the process.
        named = banner.replace(" SpringBoard", " Owner's iPad Pro SpringBoard")
        self.assertEqual(device_notifications.presentations(named), ["17:03:49"])

    def test_a_run_a_notification_reached_is_unmeasured_and_run_once_more(self):
        watches = Watches(BANNER, {"watched": True, "presented": 0})
        code, _output, error = self.run_with(watches, status=0)
        self.assertEqual((code, self.count()), (0, 2))
        self.assertEqual(watches.devices, [TABLET, TABLET])
        self.assertIn("unmeasured: a notification was presented as a banner, the first at 00:52:13", error)
        self.assertIn("the rerun saw no notification; its status 0 is the run's", error)
        run = self.record.entry("iPad")["run"]
        self.assertEqual((run["outcome"], run["notificationWatch"]["presented"]), ("exit 0", 0))
        self.assertEqual((run["rerunOf"]["measured"], run["rerunOf"]["notificationWatch"]), (False, BANNER))
        self.assertEqual(run["rerunOf"]["outcome"], "exit 0")
        self.assertFalse(self.record.entry("iPad")["activeRun"])

    def test_the_reruns_status_is_the_runs(self):
        watches = Watches(BANNER)
        with patch.object(device_notifications, "watch", watches):
            code, _output, error = self.cli("run", "iPad", "--owner", "session-a", "--", sys.executable, "-c",
                                            f"import os; p = {str(self.runs)!r}; first = not os.path.exists(p); "
                                            "open(p, 'a').write('x'); raise SystemExit(0 if first else 1)")
        self.assertEqual((code, self.count()), (1, 2))

    def test_a_rerun_a_notification_reaches_too_is_unmeasured_and_not_run_a_third_time(self):
        code, _output, error = self.run_with(Watches(BANNER, {**BANNER, "presented": 2, "first": "01:02:03"}),
                                             status=0)
        self.assertEqual((code, self.count()), (devices.UNMEASURED, 2))
        self.assertIn("unmeasured again: 2 notifications were presented as a banner, the first at 01:02:03", error)
        self.assertIn("Not run a third time", error)
        self.assertIn("the run is unmeasured; iPad stays reserved", error)
        entry = self.record.entry("iPad")
        self.assertEqual((entry["state"], entry["activeRun"]), (self.record.entry("iPad")["state"], False))
        self.assertNotEqual(devices.condition(entry), "released")

    def test_no_time_left_for_a_rerun_leaves_the_run_unmeasured(self):
        seconds = devices.BoundedRun.seconds
        calls = []

        def once(run):
            calls.append(1)
            if len(calls) > 2: raise devices.Refused("no time left")
            return seconds(run)
        with patch.object(devices.BoundedRun, "seconds", once):
            code, _output, error = self.run_with(Watches(BANNER), status=0)
        self.assertEqual((code, self.count()), (devices.UNMEASURED, 1))
        self.assertIn("unmeasured, not run again: no time left", error)

    def test_a_clean_or_unwatched_run_keeps_its_status_and_runs_once(self):
        code, _output, error = self.run_with(Watches({"watched": True, "presented": 0}), status=4)
        self.assertEqual((code, self.count()), (4, 1))
        self.assertNotIn("notification was presented", error)
        self.assertNotIn("could not be read", error)
        code, _output, error = self.run_with(Watches({"watched": False, "presented": 0, "detail": "why"}), status=0)
        self.assertEqual((code, self.count()), (0, 2))
        self.assertIn("iPad's log could not be read during the run (why)", error)
        self.assertEqual(self.record.entry("iPad")["run"]["notificationWatch"]["watched"], False)

    def test_tests_never_relay_a_devices_log(self):
        self.assertEqual(os.environ.get(device_notifications.WATCH), "off")
        with patch.object(device_notifications.subprocess, "Popen") as started:
            seen = device_notifications.Watch(TABLET).stop()
        started.assert_not_called()
        self.assertEqual(seen, {"watched": False, "presented": 0,
                                "detail": f"{device_notifications.WATCH} is off"})

    def relaying(self, *texts):
        """A watch whose relays print texts, over USB and the network in turn, and then wait to be ended."""
        printed = iter(texts)

        def relay(udid, network):
            return [sys.executable, "-c", "import sys, time; sys.stdout.write(sys.argv[1]); sys.stdout.flush(); "
                    f"time.sleep({stretched(60)})", next(printed)]
        for patcher in (patch.dict(os.environ, {device_notifications.WATCH: ""}),
                        patch.object(device_notifications, "relay", relay),
                        patch.object(device_notifications.shutil, "which", return_value="/usr/bin/true")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_watch_reads_its_relays_once_says_nothing_of_what_they_said_and_deletes_it(self):
        banner = (LOGS / "springboard-banner.log").read_text()
        self.relaying(banner, banner)
        watch = device_notifications.Watch(TABLET)
        directory = Path(watch.directory)
        self.assertEqual(oct(directory.stat().st_mode & 0o777), "0o700")
        self.assertEqual([oct(Path(path).stat().st_mode & 0o777) for path, _ in watch.relays], ["0o600", "0o600"])
        seen = watch.stop()
        # The same banners over both relays are counted once, and only their device clock is kept.
        self.assertEqual(seen, {"watched": True, "presented": 1, "first": "17:03:49"})
        self.assertFalse(directory.exists())
        self.assertTrue(all(process.poll() is not None for _path, process in watch.relays))

    def test_a_watch_that_reached_the_device_by_neither_route_says_so(self):
        self.relaying("[connected]\n", "")
        self.assertEqual(device_notifications.Watch(TABLET).stop(),
                         {"watched": False, "presented": 0, "detail": f"{device_notifications.RELAY} relayed no line "
                                                                      f"of {TABLET}'s log over USB or the network"})
        quiet = (LOGS / "springboard-quiet.log").read_text()
        self.relaying("", quiet)
        self.assertEqual(device_notifications.Watch(TABLET).stop(), {"watched": True, "presented": 0})

    def test_a_relay_whose_starter_dies_ends_and_deletes_what_it_relayed(self):
        script = (f"import sys, time; sys.path.insert(0, {str(Path(__file__).resolve().parents[1] / 'src/python')!r})\n"
                  "from vocaro_engineering import device_notifications as d\n"
                  f"d.relay = lambda udid, network: [sys.executable, '-c', 'import time; time.sleep({stretched(60)})']\n"
                  "d.shutil.which = lambda name: '/usr/bin/true'\n"
                  "watch = d.Watch(sys.argv[1])\n"
                  "print(watch.directory, *[process.pid for _path, process in watch.relays], flush=True)\n"
                  "time.sleep(600)\n")
        starter = subprocess.Popen([sys.executable, "-c", script, TABLET], stdout=subprocess.PIPE, text=True,
                                   env={**os.environ, device_notifications.WATCH: ""})
        self.addCleanup(lambda: starter.poll() is None and starter.kill())
        directory, *guards = starter.stdout.readline().split()
        self.assertTrue(Path(directory).is_dir())
        starter.kill()
        starter.wait()
        deadline = time.monotonic() + stretched(20)
        while time.monotonic() < deadline and (Path(directory).exists() or any(alive(int(pid)) for pid in guards)):
            time.sleep(0.2)
        self.assertFalse(Path(directory).exists())
        self.assertFalse(any(alive(int(pid)) for pid in guards))


class ProcessStartTests(unittest.TestCase):
    """process_start reads a start as ps prints it, and from the kernel where ps cannot be started."""

    def test_the_kernel_gives_each_process_the_start_ps_gives_it(self):
        child = live()
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        for pid in (os.getpid(), os.getppid(), 1, child.pid):
            with self.subTest(pid=pid):
                self.assertIsNotNone(process_start(pid))
                self.assertEqual(resources.kernel_start(pid), process_start(pid))
        child.kill()
        child.wait()
        for pid in (child.pid, 0, -1, "x", None):
            with self.subTest(pid=pid):
                self.assertIsNone(resources.kernel_start(pid))

    def test_a_process_that_may_not_start_ps_reads_the_start_from_the_kernel(self):
        expected = process_start(os.getpid())
        refused = PermissionError(1, "Operation not permitted")
        with patch.object(resources.subprocess, "run", side_effect=refused) as run:
            self.assertEqual(process_start(os.getpid()), expected)
            self.assertIsNone(process_start(99999999))
        self.assertEqual(run.call_count, 2)


if __name__ == "__main__":
    unittest.main()
