from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
import json
import os
from pathlib import Path
import plistlib
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
    """A stand-in for xcrun simctl over a dictionary of devices, recording every call. runtimes names each
    runtime's installed builds; a clone copies its source's entry, data included, under a new name."""

    def __init__(self):
        self.devices, self.calls, self.fail = {}, [], {}
        self.runtimes = {RUNTIME: ["24B8117"]}

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
        elif action == "list" and rest[0] == "runtimes":
            output = json.dumps({"runtimes": [{"identifier": runtime, "buildversion": build, "isAvailable": True}
                                              for runtime, builds in self.runtimes.items() for build in builds]})
        elif action == "list":
            listing = {}
            for udid, device in self.devices.items():
                listing.setdefault(device["runtime"], []).append({"udid": udid, **device})
            output = json.dumps({"devices": listing})
        elif action == "create":
            udid = self.add(rest[0], rest[1], rest[2])
            self.devices[udid]["lastUsedAt"] = None
            output = udid
        elif action == "clone":
            source = self.devices[rest[0]]
            if source["state"] != "Shutdown":
                return subprocess.CompletedProcess(arguments, 1, "", "Unable to clone device in current state: Booted")
            udid = str(uuid.uuid4()).upper()
            self.devices[udid] = {**source, "name": rest[1], "lastUsedAt": None}
            output = udid
        elif action == "boot": self.devices[rest[0]]["state"] = "Booted"
        elif action == "shutdown": self.devices[rest[0]]["state"] = "Shutdown"
        elif action == "delete": del self.devices[rest[0]]
        return subprocess.CompletedProcess(arguments, 0, output, "")

    def deleted(self):
        return [call[1] for call in self.calls if call[0] == "delete"]

    def named(self, *udids):
        """Every call that names one of these devices."""
        return [call for call in self.calls if set(call) & set(udids)]

    def created(self, action):
        """The names of the devices made by simctl create, or by clone."""
        return [call[1] if action == "create" else call[2] for call in self.calls if call[0] == action]


class Warm:
    """A stand-in for template.warm: it boots the template through the pool's simctl and says what it heard."""

    def __init__(self, build="24B8117", failure=None):
        self.calls, self.build, self.failure = [], build, failure

    def __call__(self, udid, runtime, *, simctl):
        self.calls.append((udid, runtime))
        if self.failure: raise simulator_pool.warmed.WarmFailed(self.failure)
        simctl("boot", udid)
        return {"bootSeconds": 40.0, "runtimeBuild": self.build, "runtimeVersion": "27.1",
                "voiceOver": "spoke 50.0 s after it was switched on", "voiceOverWarmed": True}


class PoolTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "device-handoff.json"
        patcher = mock.patch.dict(os.environ, {devices.OVERRIDE: str(self.path)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.simctl, self.log = Simctl(), []
        # The pool the command and member() make warms its templates with template.warm, which would reach
        # the real devicectl: a test that creates a member through either names its own stand-in.
        patcher = mock.patch.object(simulator_pool.warmed, "warm", side_effect=AssertionError("template.warm ran"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.pool = self.make("me")

    def make(self, owner, warm=None):
        """A pool of these devices; warm, a stand-in for template.warm, makes it clone new members."""
        return simulator_pool.Pool("Project", owner=owner, record=devices.Record(self.path), run=self.simctl,
                                   log=self.log.append, clock=lambda: NOW, warm=warm)

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

    def test_check_passes_only_a_pool_member_by_its_udid(self):
        member = self.simctl.add("Project Duo 1")
        default = self.simctl.add("iPhone Duo")
        other = self.simctl.add("Other Duo 1")
        def command(*arguments, defaults=frozenset({default})):
            out, err = StringIO(), StringIO()
            # check needs no owner: a script checks a UDID before it takes anything.
            with mock.patch.object(simulator_pool, "simctl", self.simctl), \
                    mock.patch.object(simulator_pool, "default_devices", return_value=defaults), \
                    mock.patch.object(simulator_pool, "default_owner", return_value=None), \
                    redirect_stdout(out), redirect_stderr(err):
                status = simulator_pool.main(["--pool", "Project", "check", *arguments])
            return status, out.getvalue(), err.getvalue()
        self.assertEqual(command(member), (0, member + "\n", ""))
        self.assertEqual(command(member.lower())[:2], (0, member + "\n"))
        status, output, error = command(default)
        self.assertEqual((status, output), (1, ""))
        self.assertIn(f"iPhone Duo ({default}) is one of Xcode's default devices outside the Project simulator pool",
                      error)
        status, _, error = command(other)
        self.assertEqual(status, 1)
        self.assertIn(f"Other Duo 1 ({other}) is a device outside the Project simulator pool", error)
        for refused in ("iPhone Duo", "booted", "Project Duo 1"):
            with self.subTest(refused=refused):
                status, _, error = command(refused)
                self.assertEqual(status, 1)
                self.assertIn("is not a simulator UDID", error)
        status, _, error = command(str(uuid.uuid4()))
        self.assertEqual(status, 1)
        self.assertIn("simctl lists no simulator", error)
        # Nothing but listings: a check boots, reserves and erases nothing.
        self.assertEqual({call[0] for call in self.simctl.calls}, {"list"})
        self.assertFalse(self.path.exists())

    def test_check_held_passes_only_a_member_the_owner_holds(self):
        held = self.simctl.add("Project Duo 1", state="Booted")
        theirs = self.simctl.add("Project Duo 2", state="Booted")
        free = self.simctl.add("Project Duo 3")
        released = self.simctl.add("Project Duo 4")
        elsewhere = self.simctl.add("Project Duo 5", state="Booted")
        orphaned = self.simctl.add("Project Duo 6", state="Booted")
        default = self.simctl.add("iPhone Duo", state="Booted")
        self.hold("Project Duo 1", held, owner="me")
        self.hold("Project Duo 2", theirs)
        self.release_entry("Project Duo 4", released, days=1)
        # A reservation under another key that names the member's UDID holds it too.
        self.hold("old key", elsewhere, owner="me")
        self.hold("Project Duo 6", orphaned, owner="me")
        value = json.loads(self.path.read_text())
        value["devices"]["Project Duo 6"].update(activeRun=True, hostProcessIdentifier=999999999,
                                                 hostProcessStart="never")
        self.path.write_text(json.dumps(value))
        before = self.path.read_text()
        def command(*arguments, owner="me", environment=None):
            out, err = StringIO(), StringIO()
            with mock.patch.object(simulator_pool, "simctl", self.simctl), \
                    mock.patch.object(simulator_pool, "default_devices", return_value=frozenset({default})), \
                    mock.patch.object(simulator_pool, "default_owner", return_value=environment), \
                    redirect_stdout(out), redirect_stderr(err):
                status = simulator_pool.main(["--pool", "Project", *(["--owner", owner] if owner else []),
                                              "check", "--held", *arguments])
            return status, out.getvalue(), err.getvalue()
        self.assertEqual(command(held), (0, held + "\n", ""))
        self.assertEqual(command(held.lower())[:2], (0, held + "\n"))
        self.assertEqual(command(elsewhere)[:2], (0, elsewhere + "\n"))
        # The owner comes from VOCARO_DEVICE_OWNER or the session when --owner is absent.
        self.assertEqual(command(held, owner=None, environment="me")[:2], (0, held + "\n"))
        status, output, error = command(held, owner="someone else")
        self.assertEqual((status, output), (1, ""))
        self.assertIn("someone else holds no reservation of; me holds it (Project Duo 1)", error)
        status, output, error = command(theirs)
        self.assertEqual((status, output), (1, ""))
        self.assertIn(f"Project Duo 2 ({theirs}) is a Project pool member that me holds no reservation of; "
                      "another session holds it (Project Duo 2)", error)
        for unheld in (free, released):
            with self.subTest(unheld=unheld):
                status, output, error = command(unheld)
                self.assertEqual((status, output), (1, ""))
                self.assertIn("nobody holds it", error)
        status, _, error = command(orphaned)
        self.assertEqual(status, 1)
        self.assertIn("its reservation Project Duo 6 is orphan: recover it", error)
        status, _, error = command(default)
        self.assertEqual(status, 1)
        self.assertIn(f"iPhone Duo ({default}) is one of Xcode's default devices outside the Project simulator pool",
                      error)
        status, _, error = command("booted")
        self.assertEqual(status, 1)
        self.assertIn("is not a simulator UDID", error)
        # --held needs an owner, where a plain check does not.
        with self.assertRaises(SystemExit), redirect_stderr(StringIO()):
            command(held, owner=None)
        # Without --held, a member someone else holds still passes.
        with mock.patch.object(simulator_pool, "simctl", self.simctl), redirect_stdout(StringIO()) as out:
            self.assertEqual(simulator_pool.main(["--pool", "Project", "--owner", "me", "check", theirs]), 0)
        self.assertEqual(out.getvalue(), theirs + "\n")
        # Nothing but listings and reading the record: a check changes neither.
        self.assertEqual({call[0] for call in self.simctl.calls}, {"list"})
        self.assertEqual(self.path.read_text(), before)
        self.assertEqual(self.pool.check_held(held).name, "Project Duo 1")
        with self.assertRaises(devices.Refused):
            self.pool.check_held(theirs, defaults=frozenset())

    def test_default_devices_come_from_the_device_set(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(simulator_pool.default_devices(directory), frozenset())
            udid = str(uuid.uuid4())
            with open(Path(directory) / "device_set.plist", "wb") as handle:
                plistlib.dump({"DefaultDevices": {RUNTIME: {DUO: udid}, "version": 0}}, handle)
            self.assertEqual(simulator_pool.default_devices(directory), frozenset({udid.upper()}))
        listing = {"devices": {RUNTIME: [{"udid": udid.lower(), "name": "iPhone Duo"},
                                         {"udid": str(uuid.uuid4()), "name": "Project Duo 1"}]}}
        self.assertEqual(simulator_pool.listed_defaults(listing, frozenset({udid.upper()})),
                         [f"iPhone Duo (iOS-27-1, {udid.upper()})"])
        self.assertEqual(simulator_pool.listed_defaults(listing, frozenset()), [])

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


    def test_a_take_that_keeps_state_reserves_and_boots_without_erasing(self):
        booted = self.simctl.add("Project Duo 1", state="Booted")
        held = self.simctl.add("Project Duo 2")
        self.hold("Project Duo 2", held)
        free = self.simctl.add("Project Duo 3")
        lease = self.pool.take(DUO, RUNTIME, purpose="VoiceOver", keep_state=True)
        self.assertEqual((lease.key, lease.udid, lease.kept, lease.created), ("Project Duo 3", free, True, False))
        self.assertNotIn("erase", {call[0] for call in self.simctl.calls})
        entry = devices.Record(self.path).entry("Project Duo 3")
        self.assertEqual((entry["preservation"], entry["keptState"], entry["ownerChatID"]),
                         (simulator_pool.KEPT_STATE, True, "me"))
        self.assertIn("not erased", entry["preservation"])
        self.assertIn(f"TOOK Project Duo 3 ({free}), not erased: it keeps its state", self.log)
        # Another session's members, the booted one and the held one, are never named in a call.
        self.assertEqual(self.simctl.named(booted, held), [])
        self.assertEqual(devices.Record(self.path).entry("Project Duo 2")["ownerChatID"], "another session")
        self.assertTrue(self.pool.release(lease, outcome="done"))
        # The negative control: the default take of the same member erases it and says nothing of kept state.
        erased = self.make("me").take(DUO, RUNTIME, purpose="Fresh installation audit")
        self.assertEqual((erased.udid, erased.kept), (free, False))
        self.assertIn(("erase", free), self.simctl.calls)
        entry = devices.Record(self.path).entry("Project Duo 3")
        self.assertEqual(entry["preservation"], simulator_pool.PRESERVATION)
        self.assertNotIn("keptState", entry)
        self.assertEqual(self.simctl.named(booted, held), [])

    def test_a_take_that_keeps_state_never_boots_a_member_booted_since_its_listing(self):
        raced = self.simctl.add("Project Duo 1")
        original = self.pool.state
        def booted_meanwhile(udid):
            # Another session booted it between the listing and the reservation's fresh observation.
            if udid == raced: self.simctl.devices[raced]["state"] = "Booted"
            return original(udid)
        with mock.patch.object(self.pool, "state", side_effect=booted_meanwhile):
            lease = self.pool.take(DUO, RUNTIME, purpose="VoiceOver", keep_state=True)
        self.assertNotEqual(lease.udid, raced)
        self.assertEqual(lease.key, "Project Duo 2")
        self.assertEqual([call for call in self.simctl.named(raced) if call[0] != "list"], [])
        self.assertIn("Project Duo 1 is no longer shut down; its reservation is kept for recovery", self.log)

    def test_the_command_takes_without_erasing_with_keep_state(self):
        udid = self.simctl.add("Project Duo 1")
        def command(*arguments):
            out, err = StringIO(), StringIO()
            with mock.patch.object(simulator_pool, "simctl", self.simctl), redirect_stdout(out), redirect_stderr(err):
                status = simulator_pool.main(["--pool", "Project", "--owner", "me", *arguments])
            return status, out.getvalue(), err.getvalue()
        status, output, error = command("take", "--device", "iPhone-Duo", "--runtime", "iOS-27-1", "--keep-state")
        self.assertEqual((status, output), (0, udid + "\n"), error)
        self.assertEqual(self.simctl.devices[udid]["state"], "Booted")
        self.assertNotIn(("erase", udid), self.simctl.calls)
        self.assertEqual(devices.Record(self.path).entry("Project Duo 1")["preservation"], simulator_pool.KEPT_STATE)
        self.assertEqual(command("release", udid)[0], 0)
        status, output, _ = command("take", "--device", "iPhone-Duo", "--runtime", "iOS-27-1")
        self.assertEqual(status, 0)
        self.assertIn(("erase", udid), self.simctl.calls)

    def test_a_member_block_can_keep_state(self):
        udid = self.simctl.add("Project Duo 1")
        with mock.patch.object(simulator_pool, "simctl", self.simctl):
            with simulator_pool.member("Project", "iPhone-Duo", "iOS-27-1", purpose="Gate", owner="me",
                                       log=self.log.append, keep_state=True) as device:
                self.assertEqual(device.udid, udid)
        self.assertNotIn(("erase", udid), self.simctl.calls)
        self.assertEqual(devices.Record(self.path).entry("Project Duo 1")["state"], "released")

    def test_new_members_are_cloned_from_a_warmed_template_remade_when_the_runtime_moves(self):
        warm = Warm()
        pool = self.make("me", warm=warm)
        first = pool.take(DUO, RUNTIME, purpose="Tests")
        template = "Project iPhone Duo template (iOS-27-1)"
        found = [udid for udid, device in self.simctl.devices.items() if device["name"] == template]
        self.assertEqual(len(found), 1)
        original = found[0]
        # The template was created, warmed and shut down, and the member cloned from it, not created.
        self.assertEqual(self.simctl.created("create"), [template])
        self.assertEqual(self.simctl.created("clone"), ["Project iPhone Duo 1"])
        self.assertEqual(warm.calls, [(original, RUNTIME)])
        self.assertEqual(self.simctl.devices[original]["state"], "Shutdown")
        self.assertEqual((first.key, first.created, first.cloned), ("Project iPhone Duo 1", True, template))
        member = devices.Record(self.path).entry("Project iPhone Duo 1")
        self.assertEqual(member["clonedFrom"], {"template": template, "udid": original, "runtimeBuild": "24B8117",
                                                "warmedAt": NOW.isoformat()})
        self.assertIn(f"a clone of the warmed template {template}, booted once on runtime build 24B8117",
                      member["preservation"])
        entry = devices.Record(self.path).entry(template)
        self.assertEqual(entry["state"], "released")
        self.assertEqual((entry["device"], entry["template"]["udid"], entry["template"]["installedBuilds"],
                          entry["template"]["runtimeBuild"], entry["template"]["voiceOverWarmed"]),
                         (original, original, ["24B8117"], "24B8117", True))
        # The template is no member: never taken, pruned or passed by check.
        self.assertEqual([member.name for member in pool.members()], ["Project iPhone Duo 1"])
        with self.assertRaisesRegex(RuntimeError, "the pool's warmed template, which is only cloned"):
            pool.check(original)
        self.assertEqual([(found.name, current) for found, current, _ in pool.templates()], [(template, True)])
        # The next new member is cloned from the same template, which is not warmed again.
        second = pool.take(DUO, RUNTIME, purpose="Tests")
        self.assertEqual((second.key, second.cloned), ("Project iPhone Duo 2", template))
        self.assertEqual(len(warm.calls), 1)
        self.assertEqual(self.simctl.created("create"), [template])
        # A new build of the runtime makes the template stale: it is deleted, made and warmed again.
        self.simctl.runtimes[RUNTIME] = ["24B8117", "24B9000"]
        self.assertEqual([current for _, current, _ in pool.templates()], [False])
        self.assertIn("the runtime moved", pool.templates()[0][2])
        third = pool.take(DUO, RUNTIME, purpose="Tests")
        self.assertEqual(self.simctl.deleted(), [original])
        replacement = [udid for udid, device in self.simctl.devices.items() if device["name"] == template]
        self.assertEqual(len(replacement), 1)
        self.assertNotEqual(replacement[0], original)
        self.assertEqual(warm.calls[-1], (replacement[0], RUNTIME))
        self.assertEqual((third.key, third.cloned), ("Project iPhone Duo 3", template))
        entry = devices.Record(self.path).entry(template)
        self.assertEqual(entry["template"]["installedBuilds"], ["24B8117", "24B9000"])
        self.assertEqual(entry["replaced"]["udid"], original)
        self.assertTrue(any(line.startswith(f"REPLACED the template {template} ({original}): the runtime moved")
                            for line in self.log))
        for lease in (first, second, third):
            self.assertTrue(pool.release(lease, outcome="done"))
        # No member was ever deleted, and every member of the kind came from a clone.
        self.assertEqual(self.simctl.created("clone"), ["Project iPhone Duo 1", "Project iPhone Duo 2",
                                                        "Project iPhone Duo 3"])

    def test_a_template_another_session_holds_or_booted_is_never_touched(self):
        warm = Warm()
        pool = self.make("me", warm=warm)
        pool.release(pool.take(DUO, RUNTIME, purpose="Tests"), outcome="done")
        template = "Project iPhone Duo template (iOS-27-1)"
        udid = devices.Record(self.path).entry(template)["device"]
        before = len(self.simctl.calls)
        self.hold(template, udid)
        # Duo 1 is free, so hold it too: the take must create a member.
        self.hold("Project iPhone Duo 1", devices.Record(self.path).entry("Project iPhone Duo 1")["device"],
                  owner="a third session")
        lease = pool.take(DUO, RUNTIME, purpose="Tests")
        self.assertEqual((lease.key, lease.cloned), ("Project iPhone Duo 2", None))
        self.assertEqual(self.simctl.created("create")[-1], "Project iPhone Duo 2")
        touched = lambda: [call for call in self.simctl.calls[before:] if udid in call and call[0] != "list"]
        self.assertEqual(touched(), [])
        self.assertEqual(devices.Record(self.path).entry(template)["ownerChatID"], "another session")
        self.assertTrue(any("is held by another session, so the new member is created instead" in line
                            for line in self.log), self.log)
        self.assertEqual(len(warm.calls), 1)
        # Released but booted, as by another session's work outside the record: still never cloned or deleted.
        devices.Record(self.path).release(template, owner="another session",
                                          revision=devices.Record(self.path).entry(template)["revision"],
                                          outcome="done", restoration="checked")
        self.simctl.devices[udid]["state"] = "Booted"
        other = pool.take(DUO, RUNTIME, purpose="Tests")
        self.assertEqual(other.cloned, None)
        self.assertEqual(touched(), [])
        self.assertEqual(self.simctl.deleted(), [])
        self.assertEqual(len(warm.calls), 1)

    def test_a_template_that_does_not_warm_is_never_cloned(self):
        warm = Warm(failure="the boot did not finish within 600 s")
        pool = self.make("me", warm=warm)
        lease = pool.take(DUO, RUNTIME, purpose="Tests")
        template = "Project iPhone Duo template (iOS-27-1)"
        self.assertEqual((lease.key, lease.cloned), ("Project iPhone Duo 1", None))
        self.assertEqual(self.simctl.created("clone"), [])
        self.assertEqual(self.simctl.created("create"), [template, "Project iPhone Duo 1"])
        entry = devices.Record(self.path).entry(template)
        self.assertEqual((entry["state"], entry["run"]), ("released", "Not warmed: the boot did not finish within 600 s"))
        self.assertNotIn("template", entry)
        unwarmed = entry["device"]
        self.assertEqual(self.simctl.devices[unwarmed]["state"], "Shutdown")
        self.assertEqual([(current, why) for _, current, why in pool.templates()],
                         [(False, "it was never warmed to the end")])
        # The next creation replaces the unwarmed template and, warmed now, clones from it.
        warm.failure = None
        cloned = pool.take(DUO, RUNTIME, purpose="Tests")
        self.assertEqual(self.simctl.deleted(), [unwarmed])
        self.assertEqual(cloned.cloned, template)
        # Without a readable runtime catalog there is no template to warm or clone, and the member is created.
        self.simctl.runtimes = {}
        created = self.make("me", warm=warm).take(DUO, RUNTIME, purpose="Tests")
        self.assertEqual((created.cloned, self.simctl.created("create")[-1]), (None, created.key))
        self.assertIn(f"no installed build of {RUNTIME} could be read, so no template is warmed or cloned", self.log)

    def test_a_stop_while_a_template_warms_shuts_it_down_and_releases_both_names(self):
        def stopped(udid, runtime, *, simctl):
            simctl("boot", udid)
            raise KeyboardInterrupt
        pool = self.make("me", warm=stopped)
        with self.assertRaises(KeyboardInterrupt):
            pool.take(DUO, RUNTIME, purpose="Tests")
        template = devices.Record(self.path).entry("Project iPhone Duo template (iOS-27-1)")
        self.assertEqual((template["state"], template["run"]), ("released", "Stopped while preparing the template for Tests"))
        self.assertEqual(self.simctl.devices[template["device"]]["state"], "Shutdown")
        self.assertEqual(devices.Record(self.path).entry("Project iPhone Duo 1")["state"], "released")
        self.assertEqual(self.simctl.deleted(), [])

    def test_the_command_lists_and_warms_templates(self):
        self.simctl.add("Project Duo 1")
        warm = Warm()
        def command(*arguments):
            out, err = StringIO(), StringIO()
            with mock.patch.object(simulator_pool, "simctl", self.simctl), \
                    mock.patch.object(simulator_pool.warmed, "warm", warm), redirect_stdout(out), redirect_stderr(err):
                status = simulator_pool.main(["--pool", "Project", "--owner", "me", *arguments])
            return status, out.getvalue(), err.getvalue()
        status, output, error = command("template", "--device", "iPhone-Duo", "--runtime", "iOS-27-1")
        self.assertEqual(status, 0, error)
        # The pool's own label for the kind names the template.
        self.assertEqual(output, "Project Duo template (iOS-27-1): warmed on runtime build 24B8117: "
                                 "spoke 50.0 s after it was switched on\n")
        status, listed, _ = command("list")
        self.assertIn("Project Duo template (iOS-27-1)", listed)
        self.assertIn("template: warmed on runtime build 24B8117", listed)
        # Warming it again finds it current.
        self.assertEqual(command("template", "--device", "iPhone-Duo", "--runtime", "iOS-27-1")[0], 0)
        self.assertEqual(len(warm.calls), 1)
        self.assertEqual(devices.Record(self.path).entry("Project Duo template (iOS-27-1)")["state"], "released")

if __name__ == "__main__":
    unittest.main()
