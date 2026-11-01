from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import uuid
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/python"))
from vocaro_engineering import devices, evidence, offload_host, resources
import host_isolation
from vocaro_engineering.simulators import pool as simulator_pool

IPHONE = "com.apple.CoreSimulator.SimDeviceType.iPhone-17-Pro"
DUO = "com.apple.CoreSimulator.SimDeviceType.iPhone-Duo"
RUNTIME = "com.apple.CoreSimulator.SimRuntime.iOS-27-1"
OTHER_RUNTIME = "com.apple.CoreSimulator.SimRuntime.iOS-27-2"
NOW = datetime.now(timezone.utc)


class Simctl:
    """A stand-in for xcrun simctl over a dictionary of devices, recording every call."""

    def __init__(self):
        self.devices, self.calls, self.fail = {}, [], {}

    def add(self, name, device_type=DUO, runtime=RUNTIME, state="Shutdown", days=0, available=True):
        udid = str(uuid.uuid4()).upper()
        self.devices[udid] = {"name": name, "deviceTypeIdentifier": device_type, "runtime": runtime, "state": state,
                              "lastUsedAt": (NOW - timedelta(days=days)).isoformat(), "isAvailable": available}
        return udid

    def __call__(self, *arguments, timeout=600):
        self.calls.append(arguments)
        action, rest = arguments[0], arguments[1:]
        if action in self.fail:
            return subprocess.CompletedProcess(arguments, *self.fail[action](rest))
        output = ""
        if action == "list" and rest[0] == "devicetypes":
            output = json.dumps({"devicetypes": [{"identifier": DUO, "name": "iPhone Duo"}]})
        elif action == "list":
            listing = {}
            for udid, device in self.devices.items():
                listing.setdefault(device["runtime"], []).append({"udid": udid, **device})
            output = json.dumps({"devices": listing})
        elif action == "create":
            udid = self.add(rest[0], rest[1], rest[2])
            self.devices[udid]["lastUsedAt"] = None
            output = udid
        elif action == "boot": self.devices[rest[0]]["state"] = "Booted"
        elif action == "shutdown": self.devices[rest[0]]["state"] = "Shutdown"
        elif action == "delete": del self.devices[rest[0]]
        return subprocess.CompletedProcess(arguments, 0, output, "")

    def deleted(self):
        return [call[1] for call in self.calls if call[0] == "delete"]


class PoolTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "device-handoff.json"
        patcher = mock.patch.dict(os.environ, {devices.OVERRIDE: str(self.path)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.simctl, self.log = Simctl(), []
        self.pool = self.make("me")

    def make(self, owner):
        return simulator_pool.Pool("Project", owner=owner, record=devices.Record(self.path), run=self.simctl,
                                   log=self.log.append, clock=lambda: NOW)

    def release_entry(self, key, udid, days):
        """A released record entry for a member, released the given number of days ago."""
        record = devices.Record(self.path)
        entry = record.reserve(key, owner="earlier", revision=(record.entry(key) or {}).get("revision", 0),
                               device=udid, purpose="Earlier tests", release_by=(datetime.now(timezone.utc)
                                                                                 + timedelta(hours=1)).isoformat())
        record.release(key, owner="earlier", revision=entry["revision"], outcome="done", restoration="checked")
        value = json.loads(self.path.read_text())
        value["devices"][key]["releasedAt"] = (NOW - timedelta(days=days)).isoformat()
        self.path.write_text(json.dumps(value))

    def hold(self, key, udid, owner="another session"):
        record = devices.Record(self.path)
        record.reserve(key, owner=owner, revision=(record.entry(key) or {}).get("revision", 0), device=udid,
                       purpose="Its tests", release_by=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())

    def reasons(self):
        return {member.name: (allowed, reason) for member, allowed, reason in self.pool.eligible()}

    def test_pruning_needs_every_condition_and_keeps_the_lowest_of_each_kind(self):
        self.simctl.add("Project Duo 1", days=40)
        cases = {"Project Duo 2": dict(days=20), "Project Duo 3": dict(days=10), "Project Duo 4": dict(days=20, state="Booted"),
                 "Project Duo 5": dict(days=20), "Project Duo 6": dict(days=20), "Project Duo 7": dict(days=20),
                 "Project Duo 8": dict(days=20, available=False), "Project Duo 9": dict(days=20)}
        udids = {name: self.simctl.add(name, **values) for name, values in cases.items()}
        # Another kind: its only member is the lowest of that kind.
        self.simctl.add("Project Duo 10", runtime=OTHER_RUNTIME, days=40)
        self.simctl.add("Other Duo 2", days=40)
        self.release_entry("Project Duo 5", udids["Project Duo 5"], days=3)
        self.release_entry("Project Duo 6", udids["Project Duo 6"], days=20)
        self.hold("Project Duo 7", udids["Project Duo 7"])
        # An entry under another key that names the device counts too.
        self.hold("old key", udids["Project Duo 9"])
        reasons = self.reasons()
        self.assertNotIn("Other Duo 2", reasons)
        self.assertEqual({name for name, (allowed, _) in reasons.items() if allowed},
                         {"Project Duo 2", "Project Duo 6", "Project Duo 8"})
        self.assertIn("lowest-numbered", reasons["Project Duo 1"][1])
        self.assertIn("lowest-numbered", reasons["Project Duo 10"][1])
        self.assertIn("within 14 days", reasons["Project Duo 3"][1])
        self.assertIn("Booted", reasons["Project Duo 4"][1])
        self.assertIn("was released", reasons["Project Duo 5"][1])
        self.assertIn("held", reasons["Project Duo 7"][1])
        self.assertIn("old key", reasons["Project Duo 9"][1])
        self.assertIn("no longer installed", reasons["Project Duo 8"][1])
        self.assertEqual(self.pool.prune(), ["Project Duo 8", "Project Duo 6", "Project Duo 2"])
        self.assertEqual(self.simctl.deleted(), [udids["Project Duo 8"], udids["Project Duo 6"], udids["Project Duo 2"]])
        entry = devices.Record(self.path).entry("Project Duo 6")
        self.assertEqual(entry["state"], "released")
        self.assertEqual(entry["pruned"]["udid"], udids["Project Duo 6"])
        self.assertIn("Pruned: deleted", entry["run"])

    def test_a_member_used_after_it_was_found_eligible_is_not_pruned(self):
        self.simctl.add("Project Duo 1", days=40)
        stale = self.simctl.add("Project Duo 2", days=40)
        original, calls = self.pool.members, []
        def booted_meanwhile(listing=None):
            # The third listing is the check under the reservation: the member booted just before it.
            calls.append(listing)
            if len(calls) == 3: self.simctl.devices[stale]["lastUsedAt"] = NOW.isoformat()
            return original(listing)
        with mock.patch.object(self.pool, "members", side_effect=booted_meanwhile):
            self.assertEqual(self.pool.prune(), [])
        self.assertEqual(self.simctl.deleted(), [])
        self.assertEqual(devices.Record(self.path).entry("Project Duo 2")["run"], "Not pruned: it changed before deletion")

    def test_take_reuses_a_free_member_and_creates_the_lowest_free_number_when_none_is_free(self):
        self.simctl.add("Project Duo 1", state="Booted")
        held = self.simctl.add("Project Duo 2")
        self.hold("Project Duo 2", held)
        self.simctl.add("Project Duo 4", runtime=OTHER_RUNTIME)
        free = self.simctl.add("Project Duo 5")
        lease = self.pool.take(DUO, RUNTIME, purpose="Tests")
        self.assertEqual((lease.key, lease.udid, lease.created), ("Project Duo 5", free, False))
        self.assertIn(("erase", free), self.simctl.calls)
        # Now none is free: the next takes 3, the lowest number no device or held entry uses.
        created = self.make("me").take(DUO, RUNTIME, purpose="Tests")
        self.assertEqual((created.key, created.created), ("Project Duo 3", True))
        self.assertEqual(self.simctl.devices[created.udid]["name"], "Project Duo 3")
        self.assertNotIn(("erase", created.udid), self.simctl.calls)
        entry = devices.Record(self.path).entry("Project Duo 3")
        self.assertEqual((entry["device"], entry["ownerChatID"], entry["created"]), (created.udid, "me", True))
        # Each names the process that holds it, by which the second Mac's census of leftover
        # processes counts a booted member as a live run's.
        holder = {"pid": os.getpid(), "start": resources.process_start(os.getpid())}
        self.assertIsNotNone(holder["start"])
        self.assertEqual([devices.Record(self.path).entry(key).get(devices.HOLDER) for key in ("Project Duo 5", "Project Duo 3")],
                         [holder, holder])
        self.assertEqual(devices.HOLDER, offload_host.HOLDER)
        self.assertTrue(self.pool.release(created, outcome="done"))
        self.assertTrue(self.pool.release(lease, outcome="done"))
        self.assertEqual(self.simctl.deleted(), [])
        self.assertEqual(self.simctl.devices[created.udid]["state"], "Shutdown")

    def test_a_reservation_whose_start_cannot_be_read_leaves_its_holder_out(self):
        # EPUBLib's Duo tests run the pool with no `ps` on PATH. The holder only helps the census of
        # leftover processes, so the reservation goes ahead without it.
        cases = {"ps missing": FileNotFoundError("ps"), "ps refused": PermissionError("ps"), "no start": None}
        for name, outcome in cases.items():
            effect = {"side_effect": outcome} if isinstance(outcome, Exception) else {"return_value": None}
            with self.subTest(name), mock.patch.object(simulator_pool, "process_start", **effect):
                lease = self.make("me").take(DUO, RUNTIME, purpose="Tests")
                entry = devices.Record(self.path).entry(lease.key)
                self.assertEqual((entry["device"], entry["ownerChatID"]), (lease.udid, "me"))
                self.assertNotIn(devices.HOLDER, entry)
                self.assertTrue(self.pool.release(lease, outcome="done"))

    def test_a_number_whose_entry_another_session_holds_is_skipped(self):
        self.hold("Project iPhone 17 Pro 1", None)
        lease = self.pool.create(IPHONE, RUNTIME, purpose="Tests", label="iPhone 17 Pro")
        self.assertEqual(lease.key, "Project iPhone 17 Pro 2")
        # Without a label, the device type's display name is used.
        self.assertEqual(self.pool.create(DUO, RUNTIME, purpose="Tests").key, "Project iPhone Duo 1")

    def test_an_interrupted_creation_adopts_only_the_device_of_its_exact_name(self):
        def stored_then_failed(rest):
            self.simctl.add(rest[0], rest[1], rest[2])
            return 124, "", "timed out"
        self.simctl.fail["create"] = stored_then_failed
        self.simctl.add("Project Duo 1", state="Booted")
        lease = self.pool.create(DUO, RUNTIME, purpose="Tests")
        self.assertEqual(self.simctl.devices[lease.udid]["name"], "Project Duo 2")
        self.assertEqual(devices.Record(self.path).entry("Project Duo 2")["device"], lease.udid)
        # A failed creation that stored nothing releases its reservation and raises.
        self.simctl.fail["create"] = lambda rest: (1, "", "no such runtime")
        with self.assertRaisesRegex(RuntimeError, "no such runtime"):
            self.pool.create(DUO, RUNTIME, purpose="Tests")
        self.assertEqual(devices.Record(self.path).entry("Project Duo 3")["state"], "released")

    def test_a_release_keeps_a_changed_reservation_or_an_unconfirmed_shutdown(self):
        udid = self.simctl.add("Project Duo 1")
        lease = self.pool.take(DUO, RUNTIME, purpose="Tests")
        self.simctl("boot", udid)
        self.simctl.fail["shutdown"] = lambda rest: (1, "", "busy")
        self.assertFalse(self.pool.release(lease, outcome="done"))
        self.assertEqual(devices.Record(self.path).entry("Project Duo 1")["ownerChatID"], "me")
        del self.simctl.fail["shutdown"]
        self.assertFalse(self.make("someone else").release(lease, outcome="done"))
        # Another process of the same owner finds the lease in the record.
        self.assertTrue(self.make("me").release(self.make("me").held(udid), outcome="done"))
        self.assertEqual(devices.Record(self.path).entry("Project Duo 1")["state"], "released")

    def test_a_member_block_releases_its_device_however_the_block_ends(self):
        udid = self.simctl.add("Project Duo 1")
        with mock.patch.object(simulator_pool, "simctl", self.simctl):
            with self.assertRaisesRegex(RuntimeError, "work failed"):
                with simulator_pool.member("Project", "iPhone-Duo", "iOS-27-1", purpose="Gate", owner="me",
                                           log=self.log.append) as device:
                    self.assertEqual(device.udid, udid)
                    self.assertEqual(self.simctl.devices[udid]["state"], "Booted")
                    raise RuntimeError("work failed")
            self.simctl.fail["bootstatus"] = lambda rest: (124, "", "timed out")
            with self.assertRaises(simulator_pool.BootTimedOut):
                with simulator_pool.member("Project", "iPhone-Duo", "iOS-27-1", purpose="Gate", owner="me",
                                           log=self.log.append):
                    self.fail("the block ran without a booted device")
        entry = devices.Record(self.path).entry("Project Duo 1")
        self.assertEqual((entry["state"], entry["run"]), ("released", "Gate: the boot reached its limit"))
        self.assertEqual(self.simctl.devices[udid]["state"], "Shutdown")
        self.assertEqual(self.simctl.deleted(), [])

    def test_a_stop_before_a_lease_is_handed_back_releases_it(self):
        udid = self.simctl.add("Project Duo 1")
        def stop(rest): raise KeyboardInterrupt
        self.simctl.fail["erase"] = stop
        with self.assertRaises(KeyboardInterrupt):
            self.pool.take(DUO, RUNTIME, purpose="Tests")
        self.assertEqual(devices.Record(self.path).entry("Project Duo 1")["run"], "Stopped before Tests")
        # A stop during a create that stored nothing releases the reserved name.
        self.simctl.devices[udid]["state"] = "Booted"
        self.simctl.fail["create"] = stop
        with self.assertRaises(KeyboardInterrupt):
            self.pool.take(DUO, RUNTIME, purpose="Tests")
        self.assertEqual(devices.Record(self.path).entry("Project Duo 2")["state"], "released")
        self.assertEqual(self.simctl.deleted(), [])

    def test_a_member_block_whose_release_fails_fails(self):
        self.simctl.add("Project Duo 1")
        with mock.patch.object(simulator_pool, "simctl", self.simctl):
            with self.assertRaisesRegex(simulator_pool.ReleaseUnconfirmed, "could not be confirmed shut down"):
                with simulator_pool.member("Project", "iPhone-Duo", "iOS-27-1", purpose="Gate", owner="me",
                                           log=self.log.append):
                    self.simctl.fail["shutdown"] = lambda rest: (1, "", "busy")
        self.assertEqual(devices.Record(self.path).entry("Project Duo 1")["state"], "reserved")

    def test_a_signal_while_the_command_boots_releases_the_member(self):
        udid = self.simctl.add("Project Duo 1")
        def signaled(rest):
            simulator_pool.stopped(15, None)
        self.simctl.fail["bootstatus"] = signaled
        with mock.patch.object(simulator_pool, "simctl", self.simctl), mock.patch.object(simulator_pool.signal, "signal"), \
                redirect_stdout(StringIO()), redirect_stderr(StringIO()) as error:
            status = simulator_pool.main(["--pool", "Project", "--owner", "me", "take", "--device", "iPhone-Duo",
                                          "--runtime", "iOS-27-1"])
        self.assertEqual(status, 143, error.getvalue())
        self.assertEqual(devices.Record(self.path).entry("Project Duo 1")["state"], "released")
        self.assertEqual(self.simctl.devices[udid]["state"], "Shutdown")

    def test_the_command_takes_releases_lists_and_prunes(self):
        self.simctl.add("Project Duo 1", days=30)
        stale = self.simctl.add("Project Duo 2", days=30)
        def command(*arguments):
            out, err = StringIO(), StringIO()
            with mock.patch.object(simulator_pool, "simctl", self.simctl), redirect_stdout(out), redirect_stderr(err):
                status = simulator_pool.main(["--pool", "Project", "--owner", "me", *arguments])
            return status, out.getvalue(), err.getvalue()
        status, listed, _ = command("list")
        self.assertEqual(status, 0)
        self.assertIn("Project Duo 2", listed)
        self.assertIn("prunable", listed)
        status, output, _ = command("prune", stale)
        self.assertEqual((status, output.strip()), (0, "Project Duo 2"))
        status, udid, _ = command("take", "--device", "iPhone-Duo", "--runtime", "iOS-27-1")
        self.assertEqual(status, 0)
        self.assertEqual(self.simctl.devices[udid.strip()]["state"], "Booted")
        self.assertEqual(command("release", udid.strip())[0], 0)
        self.assertEqual(self.simctl.devices[udid.strip()]["state"], "Shutdown")
        self.simctl.fail["bootstatus"] = lambda rest: (124, "", "timed out")
        status, _, error = command("take", "--device", "iPhone-Duo", "--runtime", "iOS-27-1")
        self.assertEqual(status, evidence.UNMEASURED)
        self.assertIn("did not finish booting", error)
        self.assertEqual(devices.Record(self.path).entry("Project Duo 1")["state"], "released")
        # The lowest member is never pruned, even when asked for by UDID.
        self.assertEqual(command("prune", udid.strip())[0], 1)


if __name__ == "__main__":
    unittest.main()
