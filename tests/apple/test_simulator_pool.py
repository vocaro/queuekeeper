from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import uuid
from xml.parsers.expat import ExpatError
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src/python"))
from queuekeeper import lane_results, offload_host, resources
from queuekeeper_mobile import devices
import host_isolation
from queuekeeper_apple.simulators import pool as simulator_pool

IPHONE = "com.apple.CoreSimulator.SimDeviceType.iPhone-17-Pro"
DUO = "com.apple.CoreSimulator.SimDeviceType.iPhone-Duo"
RUNTIME = "com.apple.CoreSimulator.SimRuntime.iOS-27-1"
OTHER_RUNTIME = "com.apple.CoreSimulator.SimRuntime.iOS-27-2"
NOW = datetime.now(timezone.utc)


class Simctl:
    """A stand-in for xcrun simctl over a dictionary of devices, recording every call. runtimes names each
    runtime's installed builds. Each device has a data folder under root, as CoreSimulator keeps one, whose
    `origin` file says what the data is: fresh (never booted), erased, or warmed by the template it came from;
    an erase resets it, and a clone copies its source's entry and data under a new name."""

    def __init__(self, root=None):
        self.devices, self.calls, self.fail = {}, [], {}
        self.runtimes = {RUNTIME: ["24B8117"]}
        self.root = Path(root) if root else Path(tempfile.mkdtemp(prefix="pool-devices-"))

    def data(self, udid):
        return self.root / udid / "data"

    def origin(self, udid):
        return (self.data(udid) / "origin").read_text()

    def cue_preferences(self, udid):
        return self.data(udid) / "Library/Preferences/com.apple.VoiceOverTouch.plist"

    def cues_at_rest(self, udid):
        """What VoiceOver's sound-cue switch holds in the device's data folder, or None with no key or usable file."""
        path = self.cue_preferences(udid)
        try:
            return plistlib.loads(path.read_bytes()).get("VoiceOverUseSoundEffects")
        except (OSError, plistlib.InvalidFileException, ExpatError):
            return None

    def add(self, name, device_type=DUO, runtime=RUNTIME, state="Shutdown", days=0, available=True, origin="fresh"):
        udid = str(uuid.uuid4()).upper()
        self.data(udid).mkdir(parents=True)
        (self.data(udid) / "origin").write_text(origin)
        self.devices[udid] = {"name": name, "deviceTypeIdentifier": device_type, "runtime": runtime, "state": state,
                              "lastUsedAt": (NOW - timedelta(days=days)).isoformat(), "isAvailable": available,
                              "dataPath": str(self.data(udid))}
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
            # As simctl clone does, links are copied as they are, the source's absolute ones among them.
            shutil.copytree(self.data(rest[0]), self.data(udid), symlinks=True)
            self.devices[udid] = {**source, "name": rest[1], "lastUsedAt": None, "dataPath": str(self.data(udid))}
            output = udid
        elif action == "erase":
            if self.devices[rest[0]]["state"] != "Shutdown":
                return subprocess.CompletedProcess(arguments, 1, "", "Unable to erase device in current state: Booted")
            shutil.rmtree(self.data(rest[0]))
            self.data(rest[0]).mkdir()
            (self.data(rest[0]) / "origin").write_text("erased")
        elif action == "boot":
            self.devices[rest[0]]["state"] = "Booted"
            self.devices[rest[0]]["cuesAtBoot"] = self.cues_at_rest(rest[0])
        elif action == "spawn" and rest[1:2] == ("defaults",):
            # Any other setting lives in the data folder, as the simulator's preferences do: an erase clears it,
            # a reset copies the template's, and a member that keeps its state keeps it. Unset, it reads as
            # `defaults` says, with status 1.
            path = self.data(rest[0]) / "defaults" / f"{rest[3]} {rest[4]}"
            if rest[2] == "write":
                path.parent.mkdir(exist_ok=True)
                path.write_text({"false": "0", "true": "1"}.get(rest[-1], rest[-1]))
            elif not path.exists():
                return subprocess.CompletedProcess(arguments, 1, "", f"The domain/default pair of ({rest[3]}, "
                                                                     f"{rest[4]}) does not exist")
            elif rest[2] == "delete": path.unlink()
            else: output = path.read_text() + "\n"
        elif action == "shutdown": self.devices[rest[0]]["state"] = "Shutdown"
        elif action == "delete":
            del self.devices[rest[0]]
            shutil.rmtree(self.root / rest[0], ignore_errors=True)
        return subprocess.CompletedProcess(arguments, 0, output, "")

    def deleted(self):
        return [call[1] for call in self.calls if call[0] == "delete"]

    def named(self, *udids):
        """Every call that names one of these devices."""
        return [call for call in self.calls if set(call) & set(udids)]

    def created(self, action):
        """The names of the devices made by simctl create, or by clone."""
        return [call[1] if action == "create" else call[2] for call in self.calls if call[0] == action]


def plant(data, udid):
    """Give a data folder what a booted simulator leaves naming its own device: absolute links into its logs
    and its data, a relative link, and LaunchServices' store."""
    (data / "Library").mkdir(parents=True, exist_ok=True)
    (data / "Media").mkdir(exist_ok=True)
    (data / "var/db/lsd").mkdir(parents=True, exist_ok=True)
    for path in (data / "Library/Logs", data / "Media/Preview.jpg", data / "Media/Relative"):
        if path.is_symlink(): path.unlink()
    os.symlink(f"{LOGS}/{udid}", data / "Library/Logs")
    os.symlink(f"{DEVICES}/{udid}/data/Media/Thumb.jpg", data / "Media/Preview.jpg")
    os.symlink("Thumb.jpg", data / "Media/Relative")
    (data / "var/db/lsd/com.apple.LaunchServices-1-v2.csstore").write_text(f"{DEVICES}/{udid}/data/Containers")


def links(data):
    return {name: os.readlink(data / name) for name in ("Library/Logs", "Media/Preview.jpg", "Media/Relative")}


LOGS = "/Users/someone/Library/Logs/CoreSimulator"
DEVICES = "/Users/someone/Library/Developer/CoreSimulator/Devices"


def save_voiceover(data, enabled):
    """Save VoiceOver's setting in a data folder as the system does, in a binary property list."""
    path = data / simulator_pool.ACCESSIBILITY_SETTINGS
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps({simulator_pool.VOICEOVER_SETTING: int(enabled), "AccessibilityEnabled": enabled},
                                    fmt=plistlib.FMT_BINARY))


class Warm:
    """A stand-in for template.warm: it boots the template through the pool's simctl, marks its data as warmed
    and says what it heard. off and warmed say whether VoiceOver was confirmed off and spoke, settled whether
    the system's voices answered both uses; limits records each call's limits."""

    def __init__(self, build="24B8117", failure=None, off=True, warmed=True, settled=True, saved=None):
        self.calls, self.limits, self.build, self.failure, self.off, self.warmed = [], [], build, failure, off, warmed
        self.settled, self.saved = settled, saved

    def __call__(self, udid, runtime, *, simctl, boot_timeout=600, voiceover_limit=600, voices_limit=900):
        self.calls.append((udid, runtime))
        self.limits.append((boot_timeout, voiceover_limit, voices_limit))
        if self.failure: raise simulator_pool.warmed.WarmFailed(self.failure)
        simctl("boot", udid)
        # What the template's data carries after the warm-up, for a reset or a clone to copy.
        Path(simctl.devices[udid]["dataPath"], "origin").write_text(f"warmed {udid}")
        # And what the system leaves in it naming the template's own folders: absolute links into its logs and
        # data, and LaunchServices' store of its container paths (vocaro-engineering#216).
        plant(Path(simctl.devices[udid]["dataPath"]), udid)
        # What the template saved of VoiceOver, whatever devicectl confirmed: saved says which, None nothing.
        if self.saved is not None: save_voiceover(Path(simctl.devices[udid]["dataPath"]), self.saved)
        said = "spoke 50.0 s after it was switched on" if self.warmed else f"silent for {voiceover_limit} s, so it is not warmed"
        voices = {"limit": voices_limit, "settled": True, "firstUse": {"seconds": 57.0, "count": 180},
                  "rebootSeconds": 6.0, "secondUse": {"seconds": 24.0, "count": 180},
                  "outcome": "settled: 180 voices listed 57.0 s into the first use and 24.0 s into the second, after a "
                             "reboot"} if self.settled else \
            {"limit": voices_limit, "settled": False, "firstUse": {"seconds": None, "outcome": f"no answer in {voices_limit} s"},
             "outcome": f"the first use gave no answer: no answer in {voices_limit} s"}
        return {"bootSeconds": 40.0, "runtimeBuild": self.build, "runtimeVersion": "27.1",
                "voiceOver": said + ("" if self.off else "; it could not be confirmed off"),
                "voiceOverWarmed": self.warmed, "voiceOverOff": self.off, "voices": voices}


class PoolTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "device-handoff.json"
        patcher = mock.patch.dict(os.environ, {devices.OVERRIDE: str(self.path)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.simctl, self.log, self.sleeps = Simctl(Path(directory.name) / "Devices"), [], []
        # The pool the command and member() make warms its templates with template.warm, which would reach
        # the real devicectl: they get this stand-in, and a test that needs another names its own.
        self.warm = Warm()
        patcher = mock.patch.object(simulator_pool.warmed, "warm", self.warm)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.pool = self.make("me")

    def make(self, owner, warm=None):
        """A pool of these devices; warm, a stand-in for template.warm, makes it keep templates: new members are
        cloned from them and taken ones reset from them. Its waits for a held template are counted, not slept."""
        return simulator_pool.Pool("Project", owner=owner, record=devices.Record(self.path), run=self.simctl,
                                   log=self.log.append, clock=lambda: NOW, warm=warm, sleep=self.sleeps.append)

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
        cases = {"Project Duo 2": dict(days=20), "Project Duo 3": dict(days=1 / 48), "Project Duo 4": dict(days=20, state="Booted"),
                 "Project Duo 5": dict(days=20), "Project Duo 6": dict(days=20), "Project Duo 7": dict(days=20),
                 "Project Duo 8": dict(days=20, available=False), "Project Duo 9": dict(days=20)}
        udids = {name: self.simctl.add(name, **values) for name, values in cases.items()}
        # Another kind: its only member is the lowest of that kind, used within the 7 days that keep a kind.
        self.simctl.add("Project Duo 10", runtime=OTHER_RUNTIME, days=5)
        self.simctl.add("Other Duo 2", days=40)
        self.release_entry("Project Duo 5", udids["Project Duo 5"], days=1 / 48)
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
        self.assertIn("within 1 hour", reasons["Project Duo 3"][1])
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

    def test_any_member_but_the_lowest_goes_an_hour_after_its_use_and_release(self):
        # vocaro-engineering#259: no idle limit beyond an hour's grace keeps a member, however recently a burst of
        # runs used it, while every other condition still holds; the lowest-numbered of each kind stays.
        minutes = lambda count: count / 1440
        self.assertEqual(simulator_pool.PRUNE_AFTER, timedelta(hours=1))
        cases = {"Project Duo 1": dict(days=0), "Project Duo 2": dict(days=minutes(90)),
                 "Project Duo 3": dict(days=minutes(50)), "Project Duo 4": dict(days=minutes(90)),
                 "Project Duo 5": dict(days=minutes(90)), "Project Duo 6": dict(days=minutes(90), state="Booted")}
        udids = {name: self.simctl.add(name, **values) for name, values in cases.items()}
        self.release_entry("Project Duo 4", udids["Project Duo 4"], days=minutes(30))
        self.release_entry("Project Duo 5", udids["Project Duo 5"], days=minutes(70))
        reasons = self.reasons()
        self.assertEqual({name for name, (allowed, _) in reasons.items() if allowed}, {"Project Duo 2", "Project Duo 5"})
        self.assertIn("lowest-numbered", reasons["Project Duo 1"][1])
        self.assertIn("more than 1 hour", reasons["Project Duo 2"][1])
        self.assertIn("within 1 hour", reasons["Project Duo 3"][1])
        self.assertIn("was released", reasons["Project Duo 4"][1])
        self.assertIn("Booted", reasons["Project Duo 6"][1])
        self.assertEqual(self.pool.prune(), ["Project Duo 5", "Project Duo 2"])

    def test_the_lowest_member_goes_on_request_once_its_whole_kind_is_unused_for_7_days(self):
        # A kind a project left, such as one on a runtime it moved off, kept its lowest member for good.
        self.assertEqual(simulator_pool.KIND_PRUNE_AFTER, timedelta(days=7))
        left = self.simctl.add("Project Duo 3", runtime=OTHER_RUNTIME, days=8)
        self.simctl.add("Project Duo 1", days=8)
        self.simctl.add("Project Duo 2", days=6)
        ipad = "com.apple.CoreSimulator.SimDeviceType.iPad-Air-13-inch-M4"
        self.simctl.add("Project iPad 1", ipad, days=6)
        self.release_entry("Project iPad 2", self.simctl.add("Project iPad 2", ipad, runtime=OTHER_RUNTIME, days=8), days=6)
        reasons = self.reasons()
        self.assertEqual({name for name, (allowed, _) in reasons.items() if allowed}, {"Project Duo 3", "Project Duo 2"})
        self.assertIn("more than 7 days, the last member of a kind unused as long", reasons["Project Duo 3"][1])
        # A member of the kind used within the 7 days, its own use, or its entry's release, keeps the lowest.
        self.assertIn("lowest-numbered member of its kind, which keeps the kind and its approval while Project Duo 2",
                      reasons["Project Duo 1"][1])
        self.assertIn("within 7 days, and the lowest-numbered member", reasons["Project iPad 1"][1])
        self.assertIn("was released", reasons["Project iPad 2"][1])
        # A take's check of its kind never lets the lowest go: the take is about to use the kind.
        self.assertEqual([(allowed, why) for _, allowed, why in self.pool.eligible(DUO, OTHER_RUNTIME)],
                         [(False, "the lowest-numbered member of its kind, which keeps the kind and its approval")])
        self.assertEqual(self.pool.prune(DUO, OTHER_RUNTIME), [])
        self.assertEqual(self.pool.prune(udids={left}), ["Project Duo 3"])
        self.assertEqual(self.simctl.deleted(), [left])

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
        # Each names the process that holds it, by which another Mac's census of leftover
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
        entry = devices.Record(self.path).entry("Project Duo 3")
        # An error with a message keeps it.
        self.assertEqual((entry["state"], entry["run"]), ("released", "Not created: simctl create Project Duo 3 "
                                                                       "failed with status 1: no such runtime"))

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

    def edit_entry(self, key, **fields):
        """Change a record entry's fields in place, as another process's reservation would have left them."""
        value = json.loads(self.path.read_text())
        value["devices"][key].update(fields)
        self.path.write_text(json.dumps(value))

    def test_release_mine_releases_ended_takes_and_keeps_live_runs_and_others(self):
        taken = [self.simctl.add(f"Project Duo {number}") for number in (1, 2, 3, 4, 5)]
        for udid in taken[:4]:
            self.simctl("boot", self.pool.take(DUO, RUNTIME, purpose="Tests").udid)
        self.hold("Project Duo 5", taken[4])
        # 1: its take has ended. 2: no holder recorded, as before holderProcess. 3: its holder, this test, runs.
        # 4: an orphaned run of this owner. 5: another session's.
        self.edit_entry("Project Duo 1", holderProcess={"pid": 2 ** 22 + 7, "start": "Thu Jan  1 00:00:00 1970"})
        self.edit_entry("Project Duo 2", holderProcess=None)
        self.edit_entry("Project Duo 4", activeRun=True, hostProcessIdentifier=2 ** 22 + 7, hostProcessStart="x")
        released, kept = self.pool.release_mine()
        self.assertEqual([key for key, _, _ in released], ["Project Duo 1", "Project Duo 2"])
        self.assertEqual({key: why for key, _, why in kept},
                         {"Project Duo 3": f"its holder process {os.getpid()} still runs",
                          "Project Duo 4": "its reservation is orphan: recover it"})
        record = devices.Record(self.path)
        self.assertEqual([record.entry(f"Project Duo {n}")["state"] for n in (1, 2)], ["released", "released"])
        self.assertEqual(record.entry("Project Duo 1")["run"], "Released at the session's closeout")
        self.assertEqual([self.simctl.devices[udid]["state"] for udid in taken[:4]],
                         ["Shutdown", "Shutdown", "Booted", "Booted"])
        self.assertEqual(record.entry("Project Duo 5")["ownerChatID"], "another session")
        self.assertNotIn(("shutdown", taken[4]), self.simctl.calls)
        # Negative control: a shutdown that is not confirmed keeps the reservation for recovery.
        self.edit_entry("Project Duo 3", holderProcess=None)
        self.simctl.fail["shutdown"] = lambda rest: (1, "", "busy")
        released, kept = self.pool.release_mine()
        self.assertEqual(released, [])
        self.assertIn(("Project Duo 3", taken[2], "its shutdown or release was not confirmed; kept for recovery"),
                      kept)
        self.assertEqual(devices.Record(self.path).entry("Project Duo 3")["ownerChatID"], "me")

    def test_a_take_is_held_an_hour_unless_asked_and_extend_moves_it(self):
        udid = self.simctl.add("Project Duo 1")
        theirs = self.simctl.add("Project Duo 2")
        self.hold("Project Duo 2", theirs)
        def command(*arguments):
            out, err = StringIO(), StringIO()
            with mock.patch.object(simulator_pool, "simctl", self.simctl), redirect_stdout(out), redirect_stderr(err):
                try:
                    status = simulator_pool.main(["--pool", "Project", "--owner", "me", *arguments])
                except SystemExit as exit:
                    status = exit.code
            return status, out.getvalue(), err.getvalue()
        def held_for():
            due = datetime.fromisoformat(devices.Record(self.path).entry("Project Duo 1")["expectedReleaseBy"])
            return (due - datetime.now(timezone.utc)).total_seconds() / 60
        self.assertEqual(simulator_pool.TAKE_HOLD, timedelta(hours=1))
        self.assertEqual(command("take", "--device", "iPhone-Duo", "--runtime", "iOS-27-1")[1].strip(), udid)
        self.assertTrue(59 < held_for() <= 60, held_for())
        status, output, _ = command("extend", udid, "--hold", "180")
        self.assertEqual(status, 0)
        self.assertEqual(output.strip(), devices.Record(self.path).entry("Project Duo 1")["expectedReleaseBy"])
        self.assertTrue(179 < held_for() <= 180, held_for())
        self.assertEqual(devices.Record(self.path).entry("Project Duo 1")["ownerChatID"], "me")
        # Negative controls: another session's member, and a hold that is not a positive number of minutes.
        status, _, error = command("extend", theirs)
        self.assertEqual(status, 1)
        self.assertIn("holds no reservation", error)
        for hold in ("0", "-5"):
            self.assertEqual(command("extend", udid, "--hold", hold)[0], 2)
        self.assertTrue(command("release", udid)[0] == 0)
        status, _, error = command("extend", udid)
        self.assertEqual(status, 1)
        # A take that asks for longer, and a run's own pool, keep the longer hold.
        command("take", "--device", "iPhone-Duo", "--runtime", "iOS-27-1", "--hold", "240")
        self.assertTrue(239 < held_for() <= 240, held_for())
        command("release", udid)
        self.pool.take(DUO, RUNTIME, purpose="A run")
        self.assertTrue(479 < held_for() <= 480, held_for())

    def test_the_release_command_takes_a_udid_or_mine(self):
        udid = self.simctl.add("Project Duo 1")
        def command(*arguments):
            out, err = StringIO(), StringIO()
            with mock.patch.object(simulator_pool, "simctl", self.simctl), redirect_stdout(out), redirect_stderr(err):
                try:
                    status = simulator_pool.main(["--pool", "Project", "--owner", "me", *arguments])
                except SystemExit as exit:
                    status = exit.code
            return status, out.getvalue(), err.getvalue()
        self.assertEqual(command("release", "--mine"), (0, "me holds no Project pool member\n", ""))
        for arguments in ((), (udid, "--mine")):
            status, _, error = command("release", *arguments)
            self.assertEqual(status, 2)
            self.assertIn("one of them, not both", error)
        self.pool.take(DUO, RUNTIME, purpose="Tests")
        # This test is the holder, so the take is a live run's: kept, and the command fails.
        status, output, _ = command("release", "--mine")
        self.assertEqual((status, output), (1, f"KEPT Project Duo 1 ({udid}): its holder process {os.getpid()} "
                                               "still runs\n"))
        self.edit_entry("Project Duo 1", holderProcess=None)
        status, output, _ = command("release", "--mine", "--outcome", "Closeout of issue 179")
        self.assertEqual((status, output), (0, f"RELEASED Project Duo 1 ({udid}): shut down and released\n"))
        self.assertEqual(devices.Record(self.path).entry("Project Duo 1")["run"], "Closeout of issue 179")

    def pool_command(self, *arguments):
        out, err = StringIO(), StringIO()
        with mock.patch.object(simulator_pool, "simctl", self.simctl), redirect_stdout(out), redirect_stderr(err):
            try:
                status = simulator_pool.main(["--pool", "Project", "--owner", "me", *arguments])
            except SystemExit as exit:
                status = exit.code
        return status, out.getvalue(), err.getvalue()

    def slots(self):
        """A directory for the hold log, as HEAVY_SLOT_DIR names the reap log's."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        patcher = mock.patch.dict(os.environ, {"HEAVY_SLOT_DIR": directory.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        return Path(directory.name)

    def test_the_hold_log_has_a_line_for_each_take_extend_and_release(self):
        log = self.slots() / offload_host.HOLD_LOG
        udid = self.simctl.add("Project Duo 1")
        theirs = self.simctl.add("Project Duo 2")
        self.hold("Project Duo 2", theirs)
        def lines(): return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        def due(): return devices.Record(self.path).entry("Project Duo 1")["expectedReleaseBy"]
        self.assertEqual(self.pool_command("take", "--device", "iPhone-Duo", "--runtime", "iOS-27-1")[1].strip(), udid)
        entry = devices.Record(self.path).entry("Project Duo 1")
        self.assertEqual(entry[offload_host.TAKEN], "Project")
        taken = due()
        self.assertEqual(self.pool_command("extend", udid, "--hold", "180")[0], 0)
        extended = due()
        self.assertEqual(self.pool_command("release", udid)[0], 0)
        self.assertEqual(devices.Record(self.path).entry("Project Duo 1")["run"], "Released")
        take, extend, release = lines()
        fields = {"time", "event", "pool", "key", "udid", "owner", "reservedAt", "heldSeconds",
                  "expectedReleaseBefore", "expectedReleaseAfter", "ended"}
        for line in (take, extend, release):
            self.assertLessEqual(fields, line.keys(), line)
            self.assertEqual((line["pool"], line["key"], line["udid"], line["owner"], line["reservedAt"]),
                             ("Project", "Project Duo 1", udid, "me", entry["reservedAt"]))
            self.assertGreaterEqual(line["heldSeconds"], 0)
        self.assertEqual([line["event"] for line in (take, extend, release)], ["take", "extend", "release"])
        self.assertEqual((take["expectedReleaseBefore"], take["expectedReleaseAfter"], take["ended"]),
                         (None, taken, None))
        self.assertEqual((extend["expectedReleaseBefore"], extend["expectedReleaseAfter"], extend["ended"]),
                         (taken, extended, None))
        self.assertEqual((release["expectedReleaseBefore"], release["expectedReleaseAfter"], release["ended"],
                          release["form"]), (extended, None, "Released", "udid"))
        # release --mine, with the default outcome and with an explicit one equal to the single form's default.
        for outcome, said in (((), "Released at the session's closeout"), (("--outcome", "Released"), "Released")):
            self.pool_command("take", "--device", "iPhone-Duo", "--runtime", "iOS-27-1")
            self.edit_entry("Project Duo 1", holderProcess=None)
            self.assertEqual(self.pool_command("release", "--mine", *outcome)[0], 0)
            self.assertEqual(devices.Record(self.path).entry("Project Duo 1")["run"], said)
            self.assertEqual([(line["event"], line["ended"], line.get("form")) for line in lines()[-2:]],
                             [("take", None, None), ("release", said, "mine")])
        # Negative controls: nothing is logged of an extend or release refused, a release whose shutdown is not
        # confirmed, a take whose boot fails, or a run's own take.
        count = len(lines())
        self.assertEqual(self.pool_command("extend", theirs)[0], 1)
        self.assertEqual(self.pool_command("release", theirs)[0], 1)
        self.pool_command("take", "--device", "iPhone-Duo", "--runtime", "iOS-27-1")
        self.simctl.fail["shutdown"] = lambda rest: (1, "", "busy")
        self.assertEqual(self.pool_command("release", udid)[0], 1)
        del self.simctl.fail["shutdown"]
        self.assertEqual(len(lines()), count + 1)
        self.assertEqual(self.pool_command("release", udid)[0], 0)
        count = len(lines())
        self.simctl.fail["bootstatus"] = lambda rest: (124, "", "timed out")
        self.assertEqual(self.pool_command("take", "--device", "iPhone-Duo", "--runtime", "iOS-27-1")[0],
                         lane_results.UNMEASURED)
        del self.simctl.fail["bootstatus"]
        run = simulator_pool.Pool("Project", owner="me", record=devices.Record(self.path), run=self.simctl,
                                  log=self.log.append, warm=None, alone=True)
        lease = run.take(DUO, RUNTIME, purpose="A run")
        self.assertNotIn(offload_host.TAKEN, devices.Record(self.path).entry(lease.key))
        self.assertTrue(run.release(lease, outcome="done"))
        self.assertEqual(len(lines()), count)

    def test_a_hold_log_that_cannot_be_written_fails_no_command(self):
        slots = self.slots()
        (slots / offload_host.HOLD_LOG).mkdir()
        udid = self.simctl.add("Project Duo 1")
        status, output, error = self.pool_command("take", "--device", "iPhone-Duo", "--runtime", "iOS-27-1")
        self.assertEqual((status, output.strip()), (0, udid))
        self.assertIn(f"the hold log {offload_host.HOLD_LOG} was not written", error)
        status, _, error = self.pool_command("extend", udid)
        self.assertEqual(status, 0)
        self.assertIn("was not written", error)
        status, _, error = self.pool_command("release", udid)
        self.assertEqual(status, 0)
        self.assertIn("was not written", error)
        self.assertEqual(devices.Record(self.path).entry("Project Duo 1")["state"], "released")
        # A directory that cannot be made is the same.
        file = slots / "a file"
        file.write_text("")
        error = StringIO()
        with redirect_stderr(error):
            self.assertFalse(offload_host.log_hold({"event": "take"}, file / "below"))
        self.assertIn("was not written", error.getvalue())

    def test_the_hold_log_keeps_one_previous_generation(self):
        slots = self.slots()
        with mock.patch.object(offload_host, "HOLD_LOG_LIMIT", 40):
            for number in range(3):
                self.assertTrue(offload_host.log_hold({"event": "take", "number": number}))
        current = [json.loads(line)["number"] for line in (slots / offload_host.HOLD_LOG).read_text().splitlines()]
        previous = [json.loads(line)["number"]
                    for line in (slots / offload_host.PREVIOUS_HOLD_LOG).read_text().splitlines()]
        self.assertEqual((previous, current), ([1], [2]))

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

    def test_a_member_block_runs_on_another_member_when_the_first_fails_to_boot(self):
        first = self.simctl.add("Project Duo 1")
        second = self.simctl.add("Project Duo 2")
        self.simctl.fail["bootstatus"] = lambda rest: (1, "", "status 60") if rest[0] == first else (0, "", "")
        with mock.patch.object(simulator_pool, "simctl", self.simctl):
            with simulator_pool.member("Project", "iPhone-Duo", "iOS-27-1", purpose="Gate", owner="me",
                                       log=self.log.append) as device:
                self.assertEqual(device.udid, second)
                self.assertEqual(self.simctl.devices[second]["state"], "Booted")
        failed, used = (devices.Record(self.path).entry(f"Project Duo {number}") for number in (1, 2))
        self.assertEqual((failed["state"], failed["run"]), ("released", "Gate: the boot failed with status 1"))
        self.assertEqual((used["state"], used["run"]), ("released", "Gate: finished"))
        self.assertEqual({self.simctl.devices[udid]["state"] for udid in (first, second)}, {"Shutdown"})
        self.assertTrue(any("Project Duo 1 did not finish booting (status 1), so another member is taken "
                            "(attempt 2 of 2)" in line for line in self.log))
        # With no other free member, the next attempt creates one; a single attempt gives up on the first.
        self.simctl.fail["bootstatus"] = lambda rest: (1, "", "status 60") if rest[0] in (first, second) else (0, "", "")
        with mock.patch.object(simulator_pool, "simctl", self.simctl):
            with self.assertRaisesRegex(RuntimeError, "Project Duo 1 failed to boot"):
                with simulator_pool.member("Project", "iPhone-Duo", "iOS-27-1", purpose="Gate", owner="me",
                                           log=self.log.append, attempts=1):
                    self.fail("the block ran without a booted device")
            self.simctl.devices[second]["state"] = "Booted"  # Not free: the retry must create a member.
            with simulator_pool.member("Project", "iPhone-Duo", "iOS-27-1", purpose="Gate", owner="me",
                                       log=self.log.append) as device:
                self.assertEqual(self.simctl.devices[device.udid]["name"], "Project Duo 3")
        self.assertEqual(devices.Record(self.path).entry("Project Duo 3")["run"], "Gate: finished")
        self.assertEqual(self.simctl.deleted(), [])

    def test_a_member_block_fails_as_before_when_every_attempt_fails_to_boot(self):
        self.simctl.add("Project Duo 1")
        self.simctl.fail["bootstatus"] = lambda rest: (1, "", "status 60")
        with mock.patch.object(simulator_pool, "simctl", self.simctl):
            with self.assertRaisesRegex(RuntimeError, "Project Duo 2 failed to boot \\(status 1\\)"):
                with simulator_pool.member("Project", "iPhone-Duo", "iOS-27-1", purpose="Gate", owner="me",
                                           log=self.log.append):
                    self.fail("the block ran without a booted device")
        for number in (1, 2):
            entry = devices.Record(self.path).entry(f"Project Duo {number}")
            self.assertEqual((entry["state"], entry["run"]), ("released", "Gate: the boot failed with status 1"))
        self.assertEqual({device["state"] for device in self.simctl.devices.values()}, {"Shutdown"})
        with mock.patch.object(simulator_pool, "simctl", self.simctl), self.assertRaises(ValueError):
            with simulator_pool.member("Project", "iPhone-Duo", "iOS-27-1", purpose="Gate", owner="me",
                                       log=self.log.append, attempts=0):
                self.fail("the block ran with no attempt")

    def test_the_take_command_boots_another_member_when_the_first_fails(self):
        first = self.simctl.add("Project Duo 1")
        second = self.simctl.add("Project Duo 2")
        self.simctl.fail["bootstatus"] = lambda rest: (124, "", "timed out") if rest[0] == first else (0, "", "")
        status, output, error = self.pool_command("take", "--device", "iPhone-Duo", "--runtime", "iOS-27-1")
        self.assertEqual((status, output.strip()), (0, second), error)
        self.assertEqual(devices.Record(self.path).entry("Project Duo 1")["run"],
                         f"The boot ended with status {lane_results.UNMEASURED} before Simulator tests")
        self.assertEqual(self.pool_command("release", second)[0], 0)
        # One attempt leaves the command as it was: the member is released and the run is unmeasured.
        status, _, error = self.pool_command("take", "--device", "iPhone-Duo", "--runtime", "iOS-27-1",
                                             "--boot-attempts", "1")
        self.assertEqual(status, lane_results.UNMEASURED, error)
        self.assertIn("Project Duo 1 did not finish booting", error)

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
        entry = devices.Record(self.path).entry("Project Duo 2")
        # A stop with no message names itself.
        self.assertEqual((entry["state"], entry["run"]), ("released", "Not created: KeyboardInterrupt"))
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
        # The owner comes from QUEUEKEEPER_DEVICE_OWNER or the session when --owner is absent.
        self.assertEqual(command(held, owner=None, environment="me")[:2], (0, held + "\n"))
        status, output, error = command(held, owner="someone else")
        self.assertEqual((status, output), (1, ""))
        self.assertIn("someone else holds no reservation of; me holds it (Project Duo 1)", error)
        # --pool and --owner read the same after the action, and one given there wins over one before it.
        def placed(*arguments):
            out, err = StringIO(), StringIO()
            with mock.patch.object(simulator_pool, "simctl", self.simctl), \
                    mock.patch.object(simulator_pool, "default_devices", return_value=frozenset({default})), \
                    mock.patch.object(simulator_pool, "default_owner", return_value=None), \
                    redirect_stdout(out), redirect_stderr(err):
                try: status = simulator_pool.main(list(arguments))
                except SystemExit as stop: status = f"exit {stop.code}: {err.getvalue()}"
            return status, out.getvalue()
        self.assertEqual(placed("check", "--pool", "Project", "--owner", "me", "--held", held), (0, held + "\n"))
        self.assertEqual(placed("--pool", "Project", "--owner", "someone else", "check", "--owner", "me", "--held",
                                held), (0, held + "\n"))
        self.assertEqual(placed("--pool", "Project", "--owner", "me", "check", "--owner", "someone else", "--held",
                                held), (1, ""))
        status, _ = placed("--pool", "Project", "check", "--held", held)
        self.assertRegex(status, r"(?s)^exit 2: .*name the owner with --owner")
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
        self.assertEqual(status, lane_results.UNMEASURED)
        self.assertIn("did not finish booting", error)
        self.assertEqual(devices.Record(self.path).entry("Project Duo 1")["state"], "released")
        # A named member that stays, here the lowest of a kind just used, is status 1.
        self.assertEqual(command("prune", udid.strip())[0], 1)


    def cue_spawns(self, udid):
        return [call for call in self.simctl.named(udid) if call[0] == "spawn" and "VoiceOverUseSoundEffects" in call]

    def test_a_member_boots_with_voiceovers_sound_cues_already_off_in_its_data(self):
        udid = self.simctl.add("Project Duo 1")
        lease = self.pool.take(DUO, RUNTIME, purpose="VoiceOver")
        self.assertIsNone(simulator_pool.boot(self.pool, lease, 600))
        # In the shut-down member's data, where the boot finds it: an erase clears the setting.
        self.assertIn(("erase", udid), self.simctl.named(udid))
        self.assertIs(self.simctl.devices[udid]["cuesAtBoot"], False)
        self.assertEqual(self.cue_spawns(udid), [])
        self.assertFalse([line for line in self.log if line.startswith("CUES")])
        self.assertTrue(self.pool.release(lease, outcome="done"))

    def test_a_simulator_too_loaded_to_answer_spawn_still_has_its_sound_cues_off(self):
        # vocaro-engineering#310: at a load average of 38 to 47, the spawned write and read timed out on every take.
        udid = self.simctl.add("Project Duo 1")
        self.simctl.fail["spawn"] = lambda rest: (124, "", "timed out after 30 s")
        lease = self.pool.take(DUO, RUNTIME, purpose="VoiceOver")
        self.assertIsNone(simulator_pool.boot(self.pool, lease, 600))
        self.assertIs(self.simctl.devices[udid]["cuesAtBoot"], False)
        self.assertEqual(self.cue_spawns(udid), [])
        self.assertFalse([line for line in self.log if line.startswith("CUES")], self.log)
        self.assertTrue(self.pool.release(lease, outcome="done"))

    def test_a_kept_members_other_voiceover_settings_stay_and_a_setting_already_off_is_not_rewritten(self):
        udid = self.simctl.add("Project Duo 1")
        path = self.simctl.cue_preferences(udid)
        path.parent.mkdir(parents=True)
        path.write_bytes(plistlib.dumps({"VoiceOverUseSoundEffects": True, "SomeoneElsesSetting": 7}))
        lease = self.pool.take(DUO, RUNTIME, purpose="VoiceOver", keep_state=True)
        self.assertIsNone(simulator_pool.boot(self.pool, lease, 600))
        self.assertEqual(plistlib.loads(path.read_bytes()), {"VoiceOverUseSoundEffects": False, "SomeoneElsesSetting": 7})
        self.assertTrue(self.pool.release(lease, outcome="done"))
        # A reset or clone already holds it, in blocks shared with its template: nothing writes them again.
        written = path.stat()
        lease = self.pool.take(DUO, RUNTIME, purpose="VoiceOver", keep_state=True)
        self.assertIsNone(simulator_pool.boot(self.pool, lease, 600))
        self.assertEqual((path.stat().st_ino, path.stat().st_mtime_ns), (written.st_ino, written.st_mtime_ns))
        self.assertTrue(self.pool.release(lease, outcome="done"))

    def test_sound_cues_that_cannot_be_written_are_logged_and_a_take_without_voiceover_still_boots(self):
        # The pool does not know whether the holder will run VoiceOver, so an unreadable
        # preference file is reported and the take goes on.
        def no_folder(path):
            path.parent.parent.mkdir(parents=True, exist_ok=True)
            path.parent.write_text("a file, where the folder would be")

        def not_a_list(path):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("junk")

        def malformed_xml(path):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'<?xml version="1.0"?><plist version="1.0"><dict></plist>')

        for damage in (no_folder, not_a_list, malformed_xml):
            with self.subTest(damage.__name__):
                self.simctl, self.log = Simctl(), []
                self.warm = Warm()
                self.pool = self.make("me")
                udid = self.simctl.add("Project Duo 1")
                lease = self.pool.take(DUO, RUNTIME, purpose="no VoiceOver", keep_state=True)
                path = self.simctl.cue_preferences(udid)
                damage(path)
                original = path.read_bytes() if path.is_file() else None
                self.assertIsNone(simulator_pool.boot(self.pool, lease, 600))
                self.assertEqual(self.simctl.devices[udid]["state"], "Booted")
                if original is not None:
                    self.assertEqual(path.read_bytes(), original)
                cues = [line for line in self.log if line.startswith("CUES")]
                self.assertEqual(len(cues), 1, self.log)
                self.assertIn("Project Duo 1: VoiceOver's sound cues could not be switched off", cues[0])
                self.assertEqual(self.cue_spawns(udid), [])
                self.assertTrue(self.pool.release(lease, outcome="done"))

    def failed_take(self, action, status, stderr):
        """A take of one attempt whose simctl action fails: its exit status and what it wrote to stderr, one line
        each."""
        self.simctl.add("Project Duo 1")
        self.simctl.fail[action] = lambda rest: (status, "", stderr)
        code, output, error = self.pool_command("take", "--device", "iPhone-Duo", "--runtime", "iOS-27-1",
                                                "--boot-attempts", "1")
        self.assertEqual(output, "")
        self.assertEqual(devices.Record(self.path).entry("Project Duo 1")["state"], "released")
        return code, error.splitlines()

    def boot_failure_line(self, lines):
        said = [line for line in lines if "BOOT FAILED" in line]
        self.assertEqual(len(said), 1, lines)
        return said[0]

    def test_a_failed_simctl_boot_is_named_with_its_error_text_and_keeps_its_status(self):
        text = "An error was encountered (domain=NSPOSIXErrorDomain, code=60):\nlaunchd failed to respond.\n"
        code, lines = self.failed_take("boot", 60, text)
        self.assertEqual(code, 60)
        self.assertEqual(self.boot_failure_line(lines),
                         "Project Duo 1: BOOT FAILED: `simctl boot` exited 60: An error was encountered "
                         "(domain=NSPOSIXErrorDomain, code=60): launchd failed to respond.")
        self.assertIn("queuekeeper simulator-pool: Project Duo 1 did not finish booting (status 60)", lines)
        self.assertFalse([call for call in self.simctl.calls if call[0] == "bootstatus"])
        self.assertEqual(devices.Record(self.path).entry("Project Duo 1")["run"],
                         "The boot ended with status 60 before Simulator tests")

    def test_a_failed_bootstatus_is_named_with_its_error_text_and_keeps_its_status(self):
        code, lines = self.failed_take("bootstatus", 60, "Unable to boot (domain=com.apple.SimError, code=60)")
        self.assertEqual(code, 60)
        self.assertEqual(self.boot_failure_line(lines),
                         "Project Duo 1: BOOT FAILED: `simctl bootstatus -b` exited 60: "
                         "Unable to boot (domain=com.apple.SimError, code=60)")
        self.assertIn("queuekeeper simulator-pool: Project Duo 1 did not finish booting (status 60)", lines)

    def test_a_bootstatus_past_its_limit_is_named_and_stays_unmeasured(self):
        code, lines = self.failed_take("bootstatus", 124, "timed out after 600 s")
        self.assertEqual(code, lane_results.UNMEASURED)
        self.assertEqual(self.boot_failure_line(lines),
                         "Project Duo 1: BOOT FAILED: `simctl bootstatus -b` exited 124: timed out after 600 s")

    def test_a_long_boot_error_is_bounded_and_keeps_its_start_and_end(self):
        for action, command in (("boot", "simctl boot"), ("bootstatus", "simctl bootstatus -b")):
            with self.subTest(action=action):
                self.simctl, self.log = Simctl(), []
                text = "DOMAIN-FIRST " + "x" * 50000 + " UNDERLYING-LAST"
                code, lines = self.failed_take(action, 60, text)
                line = self.boot_failure_line(lines)
                self.assertEqual(code, 60)
                self.assertIn(command, line)
                self.assertIn("DOMAIN-FIRST", line)
                self.assertTrue(line.endswith("UNDERLYING-LAST"), line[-40:])
                cut = len(text) - simulator_pool.BOOT_ERROR_SHOWN
                self.assertIn(f"[cut: {cut} characters]", line)
                self.assertLess(len(line), simulator_pool.BOOT_ERROR_SHOWN + 100)

    def test_a_boot_error_at_the_limit_is_not_cut(self):
        text = "x" * simulator_pool.BOOT_ERROR_SHOWN
        line = self.boot_failure_line(self.failed_take("boot", 60, text)[1])
        self.assertTrue(line.endswith(": " + text))
        self.assertNotIn("[cut:", line)

    def test_an_empty_boot_error_says_there_was_none(self):
        for stderr in ("", " \n\t"):
            with self.subTest(stderr=stderr):
                self.simctl, self.log = Simctl(), []
                code, lines = self.failed_take("bootstatus", 60, stderr)
                self.assertEqual(code, 60)
                self.assertEqual(self.boot_failure_line(lines),
                                 "Project Duo 1: BOOT FAILED: `simctl bootstatus -b` exited 60: no error text")

    def test_a_boot_that_succeeds_logs_nothing(self):
        self.simctl.add("Project Duo 1")
        lease = self.pool.take(DUO, RUNTIME, purpose="VoiceOver")
        before = list(self.log)
        self.assertIsNone(simulator_pool.boot(self.pool, lease, 600))
        self.assertEqual(self.log, before)
        self.assertTrue(self.pool.release(lease, outcome="done"))

    def test_a_take_that_boots_writes_no_boot_failure(self):
        udid = self.simctl.add("Project Duo 1")
        code, output, error = self.pool_command("take", "--device", "iPhone-Duo", "--runtime", "iOS-27-1")
        self.assertEqual((code, output.strip()), (0, udid))
        self.assertNotIn("BOOT FAILED", error)
        self.assertNotIn("did not finish booting", error)

    APP_ACCESSIBILITY = ("com.apple.Accessibility", "ApplicationAccessibilityEnabled")

    def app_accessibility_calls(self, udid):
        return [call[3] for call in self.simctl.named(udid)
                if call[0] == "spawn" and call[2] == "defaults" and call[4:6] == self.APP_ACCESSIBILITY]

    def test_a_boot_that_asks_for_app_accessibility_writes_it_on_every_take_and_reads_it_back(self):
        udid = self.simctl.add("Project Duo 1")
        for keep_state in (False, True, False):
            with self.subTest(keep_state=keep_state):
                lease = self.pool.take(DUO, RUNTIME, purpose="Hosted snapshots", keep_state=keep_state)
                before = len(self.simctl.named(udid))
                self.assertIsNone(simulator_pool.boot(self.pool, lease, 600, app_accessibility=True))
                calls = self.simctl.named(udid)[before:]
                status = calls.index(("bootstatus", udid, "-b"))
                write = calls.index(("spawn", udid, "defaults", "write", *self.APP_ACCESSIBILITY, "-bool", "true"))
                read = calls.index(("spawn", udid, "defaults", "read", *self.APP_ACCESSIBILITY))
                # Once the boot has finished, on the member's data as this take left it.
                self.assertTrue(status < write < read, calls)
                self.assertEqual((self.simctl.data(udid) / "defaults" / " ".join(self.APP_ACCESSIBILITY)).read_text(), "1")
                self.assertFalse([line for line in self.log if line.startswith("APP ACCESSIBILITY")], self.log)
                self.assertTrue(self.pool.release(lease, outcome="done"))
        # The erase of each default take cleared it, so each take wrote it again.
        self.assertEqual(self.app_accessibility_calls(udid), ["write", "read"] * 3)

    def test_app_accessibility_that_does_not_read_on_fails_the_boot(self):
        # Negative controls: a write that fails, and one that leaves the key unset or off.
        for failure in ({"spawn": lambda rest: (1, "", "spawn failed")},
                        {"spawn": lambda rest: (0, "", "") if rest[2] == "write" else
                         (1, "", "The domain/default pair does not exist")},
                        {"spawn": lambda rest: (0, "0\n" if rest[2] == "read" else "", "")}):
            with self.subTest(failure=failure):
                self.simctl, self.log = Simctl(), []
                self.pool = self.make("me")
                self.simctl.add("Project Duo 1")
                self.simctl.fail.update(failure)
                lease = self.pool.take(DUO, RUNTIME, purpose="Hosted snapshots")
                self.assertEqual(simulator_pool.boot(self.pool, lease, 600, app_accessibility=True), 1)
                lines = [line for line in self.log if line.startswith("APP ACCESSIBILITY")]
                self.assertEqual(len(lines), 1, self.log)
                self.assertIn("Project Duo 1: could not be switched on, so a hosted test reads no system views", lines[0])
                self.assertTrue(self.pool.release(lease, outcome="done"))

    def test_a_boot_that_does_not_ask_leaves_app_accessibility_unset_and_removes_a_kept_one(self):
        udid = self.simctl.add("Project Duo 1")
        lease = self.pool.take(DUO, RUNTIME, purpose="Unit tests")
        self.assertIsNone(simulator_pool.boot(self.pool, lease, 600))
        # An erased or reset member cannot carry it, so nothing is spawned for it.
        self.assertEqual(self.app_accessibility_calls(udid), [])
        self.assertTrue(self.pool.release(lease, outcome="done"))
        # An earlier holder that asked for it left it on a member the next take keeps.
        self.simctl("spawn", udid, "defaults", "write", *self.APP_ACCESSIBILITY, "-bool", "true")
        lease = self.pool.take(DUO, RUNTIME, purpose="Unit tests", keep_state=True)
        self.assertIsNone(simulator_pool.boot(self.pool, lease, 600))
        self.assertEqual(self.app_accessibility_calls(udid)[-2:], ["delete", "read"])
        self.assertFalse((self.simctl.data(udid) / "defaults" / " ".join(self.APP_ACCESSIBILITY)).exists())
        self.assertFalse([line for line in self.log if line.startswith("APP ACCESSIBILITY")], self.log)
        self.assertTrue(self.pool.release(lease, outcome="done"))
        # A kept member that never had it: the delete fails, the reading confirms it unset, and the boot counts.
        lease = self.pool.take(DUO, RUNTIME, purpose="Unit tests", keep_state=True)
        self.assertIsNone(simulator_pool.boot(self.pool, lease, 600))
        self.assertFalse([line for line in self.log if line.startswith("APP ACCESSIBILITY")], self.log)
        self.assertTrue(self.pool.release(lease, outcome="done"))
        # The negative control: a key that will not go is logged, and the boot still counts, as nobody asked for it.
        self.simctl("spawn", udid, "defaults", "write", *self.APP_ACCESSIBILITY, "-bool", "true")
        self.simctl.fail["spawn"] = lambda rest: (0, "1\n" if rest[2] == "read" else "", "")
        lease = self.pool.take(DUO, RUNTIME, purpose="Unit tests", keep_state=True)
        self.assertIsNone(simulator_pool.boot(self.pool, lease, 600))
        lines = [line for line in self.log if line.startswith("APP ACCESSIBILITY")]
        self.assertEqual(len(lines), 1, self.log)
        self.assertIn("an earlier holder's setting could not be removed", lines[0])
        self.assertTrue(self.pool.release(lease, outcome="done"))

    def test_the_take_command_switches_app_accessibility_on_only_when_asked(self):
        udid = self.simctl.add("Project Duo 1")
        with mock.patch.object(simulator_pool, "simctl", self.simctl), mock.patch.object(simulator_pool.signal, "signal"):
            for arguments, expected in (([], []), (["--app-accessibility"], ["write", "read"])):
                with self.subTest(arguments=arguments), redirect_stdout(StringIO()) as output, redirect_stderr(StringIO()):
                    before = len(self.app_accessibility_calls(udid))
                    status = simulator_pool.main(["--pool", "Project", "--owner", "me", "take", "--device", "iPhone-Duo",
                                                  "--runtime", "iOS-27-1", *arguments])
                    self.assertEqual((status, output.getvalue().strip()), (0, udid))
                    self.assertEqual(self.app_accessibility_calls(udid)[before:], expected)
                    self.assertTrue(self.make("me").release(self.make("me").held(udid), outcome="done"))
            # The negative control: a setting that does not read on fails the take and releases the member.
            self.simctl.fail["spawn"] = lambda rest: (0, "", "") if rest[2] == "write" else (1, "", "does not exist")
            with redirect_stdout(StringIO()) as output, redirect_stderr(StringIO()) as error:
                status = simulator_pool.main(["--pool", "Project", "--owner", "me", "take", "--device", "iPhone-Duo",
                                              "--runtime", "iOS-27-1", "--app-accessibility"])
            self.assertEqual((status, output.getvalue()), (1, ""))
            self.assertIn("its app accessibility could not be switched on", error.getvalue())
            self.assertEqual(devices.Record(self.path).entry("Project Duo 1")["state"], "released")

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
        self.assertIn(f"no installed build of {RUNTIME} could be read, so no template is warmed or cloned, so the new "
                      "member is created instead", self.log)

    def test_a_stop_while_a_template_warms_shuts_it_down_and_releases_both_names(self):
        def stopped(udid, runtime, *, simctl, **limits):
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

    def test_a_template_reaped_after_its_warm_up_was_killed_is_replaced_once_recovered(self):
        """A warm-up killed outright (SIGKILL, or the Mac stopping) leaves its template booted and its entry held
        with no template details. Reap shuts it down once its release is past and it has run 2 hours, and leaves
        the entry held; until it is recovered, members are created instead. Once recovered, the next creation
        of the kind reads it as never warmed to the end, deletes it and warms a new one."""
        template = "Project iPhone Duo template (iOS-27-1)"
        killed = {}

        def killed_while_warming(udid, runtime, *, simctl, **limits):
            # Provenance is outside the copied data and exists before the first boot.
            marker = self.simctl.data(udid).parent / ".queuekeeper-pool-template.json"
            origin = offload_host.template_provenance({**self.simctl.devices[udid], "udid": udid})
            self.assertEqual(json.loads(marker.read_text()), origin)
            self.assertEqual((origin["pool"], origin["name"], origin["holder"]["pid"]),
                             ("Project", template, os.getpid()))
            simctl("boot", udid)
            # What the record holds when the warming process dies with no chance to release anything.
            killed.update(udid=udid, record=self.path.read_text())
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            self.make("a killed session", warm=killed_while_warming).take(DUO, RUNTIME, purpose="Tests")
        self.path.write_text(killed["record"])
        udid = killed["udid"]
        entry = devices.Record(self.path).entry(template)
        self.assertEqual((entry["state"], entry["ownerChatID"], entry["device"], entry.get("template")),
                         ("reserved", "a killed session", udid, None))
        # Reap's one change: simctl shutdown.
        self.simctl.devices[udid]["state"] = "Shutdown"
        warm, before = Warm(), len(self.simctl.calls)
        pool = self.make("me", warm=warm)
        held = pool.take(DUO, RUNTIME, purpose="Tests")
        self.assertEqual((held.created, held.cloned), (True, None))
        self.assertEqual([call for call in self.simctl.calls[before:] if udid in call], [])
        self.assertEqual(warm.calls, [])
        self.assertIn(f"{template} is held by a killed session, so the new member is created instead", self.log)
        self.assertTrue(pool.release(held, outcome="done"))
        devices.Record(self.path).recover(template, by="me", revision=entry["revision"],
                                          verified="its warm-up process is gone and reap shut it down")
        self.assertEqual([(found.name, current, why) for found, current, why in pool.templates()],
                         [(template, False, "it was never warmed to the end")])
        cloned = pool.create(DUO, RUNTIME, purpose="Tests")
        self.assertEqual(self.simctl.deleted(), [udid])
        self.assertIn(f"REPLACED the template {template} ({udid}): it was never warmed to the end", self.log)
        replacement = devices.Record(self.path).entry(template)
        self.assertNotEqual(replacement["device"], udid)
        self.assertEqual((replacement["state"], replacement["template"]["udid"], replacement["replaced"]["udid"]),
                         ("released", replacement["device"], udid))
        self.assertEqual(warm.calls, [(replacement["device"], RUNTIME)])
        self.assertEqual(cloned.cloned, template)
        self.assertTrue(pool.release(cloned, outcome="done"))

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
        self.assertRegex(output, r"^Project Duo template \(iOS-27-1\): warmed on runtime build 24B8117 at \S+: "
                                 r"spoke 50\.0 s after it was switched on; voices: settled: 180 voices listed 57\.0 s into "
                                 r"the first use and 24\.0 s into the second, after a reboot\n$")
        status, listed, _ = command("list")
        self.assertIn("Project Duo template (iOS-27-1)", listed)
        self.assertIn("template: warmed on runtime build 24B8117", listed)
        self.assertIn("; voices: settled: 180 voices", listed)
        # Warming it again finds it current.
        self.assertEqual(command("template", "--device", "iPhone-Duo", "--runtime", "iOS-27-1")[0], 0)
        self.assertEqual(len(warm.calls), 1)
        self.assertEqual(devices.Record(self.path).entry("Project Duo template (iOS-27-1)")["state"], "released")

    def test_a_template_whose_voiceover_was_not_confirmed_off_is_never_cloned(self):
        warm = Warm(off=False)
        pool = self.make("me", warm=warm)
        template = "Project iPhone Duo template (iOS-27-1)"
        lease = pool.take(DUO, RUNTIME, purpose="Tests")
        # Warmed, recorded, but not cloned: a clone would boot with VoiceOver on.
        self.assertEqual((lease.key, lease.cloned), ("Project iPhone Duo 1", None))
        self.assertEqual(self.simctl.created("clone"), [])
        self.assertEqual(self.simctl.created("create"), [template, "Project iPhone Duo 1"])
        entry = devices.Record(self.path).entry(template)
        self.assertEqual((entry["state"], entry["template"]["voiceOverOff"]), ("released", False))
        self.assertIn("Warmed, not cloned: VoiceOver could not be confirmed off", entry["run"])
        self.assertEqual(self.simctl.devices[entry["device"]]["state"], "Shutdown")
        self.assertEqual([(current, why) for _, current, why in pool.templates()],
                         [(False, "VoiceOver could not be confirmed off, so a clone would boot with it on")])
        # The next creation replaces it rather than cloning it.
        warm.off = True
        old = entry["device"]
        cloned = pool.take(DUO, RUNTIME, purpose="Tests")
        self.assertEqual(self.simctl.deleted(), [old])
        self.assertEqual(cloned.cloned, template)
        self.assertTrue(any(f"REPLACED the template {template} ({old}): VoiceOver could not be confirmed off" in line
                            for line in self.log), self.log)
        # A record written before voiceOverOff existed says it in its voiceOver.
        value = json.loads(self.path.read_text())
        details = value["devices"][template]["template"]
        del details["voiceOverOff"]
        details["voiceOver"] = "spoke 50.0 s after it was switched on; it could not be confirmed off"
        self.path.write_text(json.dumps(value))
        self.assertEqual([current for _, current, _ in pool.templates()], [False])
        del details["voiceOver"]
        details["voiceOver"] = "spoke 50.0 s after it was switched on"
        self.path.write_text(json.dumps(value))
        self.assertEqual([current for _, current, _ in pool.templates()], [True])

    def test_a_template_saved_with_voiceover_on_is_never_copied_whatever_its_record_says(self):
        # Warmed with VoiceOver confirmed off, yet saved on (vocaro-engineering#263): neither cloned nor copied.
        warm = Warm(saved=True)
        pool = self.make("me", warm=warm)
        free = self.simctl.add("Project Duo 1")
        template = "Project Duo template (iOS-27-1)"
        reason = "its saved settings have VoiceOver on, so a clone or copy would boot with it on"
        lease = pool.take(DUO, RUNTIME, purpose="Tests")
        self.assertEqual((lease.udid, lease.reset), (free, None))
        self.assertEqual(self.simctl.created("clone"), [])
        self.assertNotEqual(self.simctl.origin(free).split()[0], "warmed")
        entry = devices.Record(self.path).entry(template)
        self.assertEqual((entry["state"], entry["template"]["voiceOverOff"]), ("released", True))
        self.assertIn(f"Warmed, not cloned: {reason}", entry["run"])
        self.assertEqual([(current, why) for _, current, why in pool.templates()], [(False, reason)])
        self.assertTrue(pool.release(lease, outcome="done"))
        # The next take replaces it, and a template saved with VoiceOver off is copied.
        warm.saved = False
        old = entry["device"]
        again = pool.take(DUO, RUNTIME, purpose="Tests")
        self.assertEqual(self.simctl.deleted(), [old])
        self.assertTrue(any(f"REPLACED the template {template} ({old}): {reason}" in line for line in self.log), self.log)
        self.assertEqual((again.udid, again.reset), (free, template))
        # With the member still reserved, the next take needs a clone. A template that was current when
        # warmed, such as one from before this check, must be replaced before the pool clones from it.
        source, = [udid for udid, device in self.simctl.devices.items() if device["name"] == template]
        save_voiceover(self.simctl.data(source), True)
        self.assertEqual([(current, why) for _, current, why in pool.templates()], [(False, reason)])
        cloned = pool.take(DUO, RUNTIME, purpose="Other tests")
        replacement = devices.Record(self.path).entry(template)["device"]
        self.assertEqual(self.simctl.deleted(), [old, source])
        self.assertEqual([call for call in self.simctl.calls if call[0] == "clone"],
                         [("clone", replacement, cloned.key)])
        self.assertFalse(simulator_pool.saved_voiceover_on(self.simctl.data(cloned.udid)))

    def test_saved_voiceover_reads_only_a_setting_that_is_on(self):
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder)
            self.assertFalse(simulator_pool.saved_voiceover_on(data))
            save_voiceover(data, False)
            self.assertFalse(simulator_pool.saved_voiceover_on(data))
            save_voiceover(data, True)
            self.assertTrue(simulator_pool.saved_voiceover_on(data))
            path = data / simulator_pool.ACCESSIBILITY_SETTINGS
            path.write_bytes(plistlib.dumps({"AccessibilityEnabled": True}))
            self.assertFalse(simulator_pool.saved_voiceover_on(data))
            for broken in (b"not a property list", b"<?xml version='1.0'?><plist><dict>", plistlib.dumps(["a"])):
                path.write_bytes(broken)
                self.assertFalse(simulator_pool.saved_voiceover_on(data), broken)

    def test_a_stop_before_the_template_has_a_udid_releases_its_name(self):
        template = "Project iPhone Duo template (iOS-27-1)"
        original = self.simctl.__call__
        def stop_creating(rest):
            if rest[0] == template: raise KeyboardInterrupt
            return 0, original("create", *rest).stdout, ""
        self.simctl.fail["create"] = stop_creating
        pool = self.make("me", warm=Warm())
        with self.assertRaises(KeyboardInterrupt):
            pool.take(DUO, RUNTIME, purpose="Tests")
        entry = devices.Record(self.path).entry(template)
        self.assertEqual((entry["state"], entry["run"]), ("released", "Not warmed: KeyboardInterrupt"))
        self.assertEqual(devices.Record(self.path).entry("Project iPhone Duo 1")["state"], "released")
        self.assertEqual(self.simctl.devices, {})
        # So it is after a stale template's delete: the old one is gone and the name is free again.
        del self.simctl.fail["create"]
        made = pool.take(DUO, RUNTIME, purpose="Tests")
        old = devices.Record(self.path).entry(template)["device"]
        self.assertEqual(made.cloned, template)
        self.assertTrue(pool.release(made, outcome="done"))
        self.simctl.runtimes[RUNTIME] = ["24B8117", "24B9000"]
        self.hold("Project iPhone Duo 1", made.udid, owner="a third session")
        self.simctl.fail["create"] = stop_creating
        with self.assertRaises(KeyboardInterrupt):
            pool.take(DUO, RUNTIME, purpose="Tests")
        self.assertEqual(self.simctl.deleted(), [old])
        self.assertEqual(devices.Record(self.path).entry(template)["state"], "released")
        # A device simctl stored before the stop keeps the name held for recovery, as for a member.
        def stored_then_stopped(rest):
            if rest[0] == template:
                self.simctl.add(rest[0], rest[1], rest[2])
                raise KeyboardInterrupt
            return 0, original("create", *rest).stdout, ""
        self.simctl.fail["create"] = stored_then_stopped
        with self.assertRaises(KeyboardInterrupt):
            pool.take(DUO, RUNTIME, purpose="Tests")
        entry = devices.Record(self.path).entry(template)
        self.assertEqual((entry["state"], entry["ownerChatID"]), ("reserved", "me"))
        self.assertIn(f"{template}: stopped with a device of its name stored but not recorded; its reservation is "
                      "kept for recovery", self.log)

    def test_a_take_warms_within_the_shorter_limits_and_the_command_with_the_full_ones(self):
        warm = Warm(warmed=False)
        pool = self.make("me", warm=warm)
        template = "Project iPhone Duo template (iOS-27-1)"
        lease = pool.take(DUO, RUNTIME, purpose="Tests")
        self.assertEqual(warm.limits, [(simulator_pool.warmed.TAKE_BOOT, simulator_pool.warmed.TAKE_VOICEOVER_LIMIT,
                                        simulator_pool.warmed.TAKE_VOICES_LIMIT)])
        self.assertEqual((simulator_pool.warmed.TAKE_BOOT, simulator_pool.warmed.TAKE_VOICEOVER_LIMIT,
                          simulator_pool.warmed.TAKE_VOICES_LIMIT), (300, 180, 300))
        # VoiceOver stayed cold within the take's limit, yet the template has had its first boot: it is cloned.
        self.assertEqual(lease.cloned, template)
        self.assertEqual(devices.Record(self.path).entry(template)["template"]["voiceOverWarmed"], False)
        self.assertTrue(pool.release(lease, outcome="done"))
        cold = devices.Record(self.path).entry(template)["device"]
        def command(*arguments):
            out, err = StringIO(), StringIO()
            with mock.patch.object(simulator_pool, "simctl", self.simctl), \
                    mock.patch.object(simulator_pool.warmed, "warm", warm), redirect_stdout(out), redirect_stderr(err):
                status = simulator_pool.main(["--pool", "Project", "--owner", "me", *arguments])
            return status, out.getvalue(), err.getvalue()
        # The command replaces a template whose VoiceOver stayed cold, with the full limits.
        warm.warmed = True
        status, output, error = command("template", "--device", "iPhone-Duo", "--runtime", "iOS-27-1")
        self.assertEqual(status, 0, error)
        self.assertEqual(warm.limits[-1], (600, simulator_pool.warmed.VOICEOVER_LIMIT, simulator_pool.warmed.VOICES_LIMIT))
        self.assertEqual(self.simctl.deleted(), [cold])
        self.assertIn("spoke 50.0 s after it was switched on", output)
        # Warmed now, it is left as it is by the command and cloned by the next take.
        self.assertEqual(command("template", "--device", "iPhone-Duo", "--runtime", "iOS-27-1")[0], 0)
        self.assertEqual(len(warm.calls), 2)
    def command(self, warm, *arguments):
        """`qk simulator-pool` over the fake simctl with warm standing in for template.warm."""
        out, err = StringIO(), StringIO()
        with mock.patch.object(simulator_pool, "simctl", self.simctl), \
                mock.patch.object(simulator_pool.warmed, "warm", warm), redirect_stdout(out), redirect_stderr(err):
            status = simulator_pool.main(["--pool", "Project", "--owner", "me", *arguments])
        return status, out.getvalue(), err.getvalue()

    def test_a_take_resets_a_free_member_from_the_kinds_template_in_place_of_an_erase(self):
        warm = Warm()
        pool = self.make("me", warm=warm)
        free = self.simctl.add("Project Duo 1")
        template = "Project Duo template (iOS-27-1)"
        lease = pool.take(DUO, RUNTIME, purpose="Tests")
        # The kind had no template: the take made one within the take's limits, then copied it into the member.
        self.assertEqual((lease.key, lease.udid, lease.reset, lease.created, lease.kept),
                         ("Project Duo 1", free, template, False, False))
        self.assertEqual(warm.limits, [(simulator_pool.warmed.TAKE_BOOT, simulator_pool.warmed.TAKE_VOICEOVER_LIMIT,
                                        simulator_pool.warmed.TAKE_VOICES_LIMIT)])
        source, = [udid for udid, device in self.simctl.devices.items() if device["name"] == template]
        self.assertEqual(self.simctl.origin(free), f"warmed {source}")
        # The member kept its UDID and was erased first, so that a copy that fails leaves an erased member;
        # the template was only read, never erased, cloned or deleted.
        self.assertEqual([call for call in self.simctl.calls if call[0] in ("erase", "clone", "delete")], [("erase", free)])
        self.assertEqual(self.simctl.devices[free]["name"], "Project Duo 1")
        entry = devices.Record(self.path).entry("Project Duo 1")
        self.assertEqual({key: entry["resetFrom"][key] for key in ("template", "udid", "runtimeBuild", "warmedAt", "voices")},
                         {"template": template, "udid": source, "runtimeBuild": "24B8117", "warmedAt": NOW.isoformat(),
                          "voices": {"firstUse": 57.0, "secondUse": 24.0}})
        self.assertEqual(entry["preservation"],
                         simulator_pool.RESET.format(template=template, at=NOW.isoformat(), build="24B8117"))
        self.assertNotIn("erasedBecause", entry)
        self.assertIn(f"TOOK Project Duo 1 ({free}), reset from {template}", self.log)
        self.assertTrue(any(line.startswith(f"RESET Project Duo 1 ({free}) from {template}, made {NOW.isoformat()} on "
                                            "runtime build 24B8117, in ") for line in self.log), self.log)
        # The template was held for the copy alone and released, shut down, and is no member.
        held = devices.Record(self.path).entry(template)
        self.assertEqual((held["state"], held["run"]), ("released", "Copied into Project Duo 1 for Tests"))
        self.assertEqual(self.simctl.devices[source]["state"], "Shutdown")
        self.assertEqual([member.name for member in pool.members()], ["Project Duo 1"])
        self.assertTrue(pool.release(lease, outcome="done"))
        # The next take copies the same template, not warmed again, over what the tests left.
        (self.simctl.data(free) / "origin").write_text("used by tests")
        again = pool.take(DUO, RUNTIME, purpose="Tests")
        self.assertEqual((again.udid, again.reset, len(warm.calls)), (free, template, 1))
        self.assertEqual(self.simctl.origin(free), f"warmed {source}")
        self.assertTrue(pool.release(again, outcome="done"))
        # A take that keeps state copies nothing and never touches the template.
        before = len(self.simctl.calls)
        kept = pool.take(DUO, RUNTIME, purpose="VoiceOver", keep_state=True)
        self.assertEqual((kept.kept, kept.reset), (True, None))
        self.assertEqual([call for call in self.simctl.calls[before:] if source in call or call[0] == "erase"], [])
        self.assertEqual(self.simctl.origin(free), f"warmed {source}")
        self.assertEqual(self.sleeps, [])

    def test_relocate_points_a_copy_at_its_own_device_and_stray_finds_what_it_missed(self):
        data, own, other, older = Path(tempfile.mkdtemp(prefix="relocate-")), *(str(uuid.uuid4()).upper() for _ in range(3))
        self.addCleanup(shutil.rmtree, data)
        plant(data, other)
        os.symlink(f"{DEVICES}/{older}/data/Library/Shortcuts/Tools.sqlite", data / "Library/Tools-active")
        os.symlink(f"{DEVICES}/{own}/data/Media/Thumb.jpg", data / "Media/Own")
        self.assertIn("links to", simulator_pool.stray(data, own))
        # Links into any other device, the template's and a copy's earlier source's, become this device's; its
        # own and relative links stay, and LaunchServices' store of the source's paths is removed.
        self.assertEqual(simulator_pool.relocate(data, own), 3)
        self.assertEqual(links(data), {"Library/Logs": f"{LOGS}/{own}", "Media/Preview.jpg": f"{DEVICES}/{own}/data/Media/Thumb.jpg",
                                       "Media/Relative": "Thumb.jpg"})
        self.assertEqual(os.readlink(data / "Library/Tools-active"), f"{DEVICES}/{own}/data/Library/Shortcuts/Tools.sqlite")
        self.assertEqual(os.readlink(data / "Media/Own"), f"{DEVICES}/{own}/data/Media/Thumb.jpg")
        self.assertEqual(list((data / "var/db/lsd").iterdir()), [])
        self.assertIsNone(simulator_pool.stray(data, own))
        self.assertEqual(simulator_pool.relocate(data, own), 0)
        # Negative controls: a store left behind, or one link, is reported.
        (data / "var/db/lsd/com.apple.LaunchServices-1-v2.csstore").write_text("copied")
        self.assertIn("LaunchServices", simulator_pool.stray(data, own))
        (data / "var/db/lsd/com.apple.LaunchServices-1-v2.csstore").unlink()
        os.symlink(f"{LOGS}/{other}", data / "Library/Stray")
        self.assertEqual(simulator_pool.stray(data, own), f"Library/Stray links to {LOGS}/{other}")

    def test_a_reset_or_clone_member_points_at_its_own_folders_and_leaves_the_template_alone(self):
        warm = Warm()
        pool = self.make("me", warm=warm)
        lease = pool.take(DUO, RUNTIME, purpose="Tests")
        template = "Project iPhone Duo template (iOS-27-1)"
        source, = [udid for udid, device in self.simctl.devices.items() if device["name"] == template]
        # The clone: its links name it, not the template, and it carries no copied LaunchServices store.
        self.assertEqual(lease.cloned, template)
        self.assertEqual(links(self.simctl.data(lease.udid))["Library/Logs"], f"{LOGS}/{lease.udid}")
        self.assertFalse(any((self.simctl.data(lease.udid) / "var/db/lsd").iterdir()))
        self.assertTrue(pool.release(lease, outcome="done"))
        # The reset of the same member from the template, which copies the template's links again.
        again = pool.take(DUO, RUNTIME, purpose="Tests")
        self.assertEqual((again.udid, again.reset), (lease.udid, template))
        self.assertEqual(links(self.simctl.data(again.udid)), {
            "Library/Logs": f"{LOGS}/{again.udid}", "Media/Preview.jpg": f"{DEVICES}/{again.udid}/data/Media/Thumb.jpg",
            "Media/Relative": "Thumb.jpg"})
        self.assertIsNone(simulator_pool.stray(self.simctl.data(again.udid), again.udid))
        # The template keeps its own links and store: the copy and its relocation changed nothing in it.
        self.assertEqual(links(self.simctl.data(source))["Library/Logs"], f"{LOGS}/{source}")
        self.assertTrue(any((self.simctl.data(source) / "var/db/lsd").iterdir()))
        self.assertTrue(pool.release(again, outcome="done"))

    def test_a_copy_still_pointing_into_the_template_is_erased_not_used(self):
        warm = Warm()
        pool = self.make("me", warm=warm)
        free = self.simctl.add("Project Duo 1")
        template = "Project Duo template (iOS-27-1)"
        with mock.patch.object(simulator_pool, "relocate", return_value=0):
            lease = pool.take(DUO, RUNTIME, purpose="Tests")
        self.assertEqual((lease.udid, lease.reset), (free, None))
        self.assertEqual(self.simctl.origin(free), "erased")
        why = devices.Record(self.path).entry("Project Duo 1")["erasedBecause"]
        self.assertTrue(why.startswith(f"the copy of {template}'s data into Project Duo 1 still points into another "
                                       "device: "), why)
        self.assertEqual(devices.Record(self.path).entry(template)["run"], "Not copied into Project Duo 1 for Tests")
        self.assertTrue(pool.release(lease, outcome="done"))
        # A new clone that still points into the template is erased too, and recorded as created, not cloned.
        self.simctl.devices[free]["state"] = "Booted"
        with mock.patch.object(simulator_pool, "relocate", return_value=0):
            made = pool.take(DUO, RUNTIME, purpose="Tests")
        self.assertEqual((made.created, made.cloned), (True, None))
        self.assertEqual(self.simctl.origin(made.udid), "erased")
        self.assertNotIn("clonedFrom", devices.Record(self.path).entry(made.key))
        self.assertTrue(any("still pointed into another device" in line and "so it was erased" in line for line in self.log))

    def test_a_take_falls_back_to_an_erase_and_says_why_when_no_template_can_be_copied(self):
        warm = Warm()
        pool = self.make("me", warm=warm)
        free = self.simctl.add("Project Duo 1")
        template = "Project Duo template (iOS-27-1)"
        pool.release(pool.take(DUO, RUNTIME, purpose="Tests"), outcome="done")
        source = devices.Record(self.path).entry(template)["device"]
        # Held by another session past the wait: erased, and the record and the log say why.
        self.hold(template, source)
        lease = pool.take(DUO, RUNTIME, purpose="Tests")
        self.assertEqual((lease.udid, lease.reset), (free, None))
        self.assertEqual(self.simctl.origin(free), "erased")
        self.assertEqual(self.sleeps, [simulator_pool.TEMPLATE_POLL] * (simulator_pool.TEMPLATE_WAIT // simulator_pool.TEMPLATE_POLL))
        why = f"{template} is held by another session"
        self.assertEqual(devices.Record(self.path).entry("Project Duo 1")["erasedBecause"], why)
        self.assertIn(f"TOOK Project Duo 1 ({free}), erased: {why}", self.log)
        self.assertIn(f"WAITED {simulator_pool.TEMPLATE_WAIT} s for {template}, still held", self.log)
        self.assertEqual(devices.Record(self.path).entry(template)["ownerChatID"], "another session")
        self.assertTrue(pool.release(lease, outcome="done"))
        record = devices.Record(self.path)
        record.release(template, owner="another session", revision=record.entry(template)["revision"], outcome="done",
                       restoration="checked")
        # A copy that fails: erased again, the template released as not copied, with the clone's words in the record.
        self.sleeps.clear()
        erases = len([call for call in self.simctl.calls if call[0] == "erase"])
        with mock.patch.object(simulator_pool, "copy_data", side_effect=OSError(28, "clonefile: No space left on device")):
            lease = pool.take(DUO, RUNTIME, purpose="Tests")
        self.assertEqual((lease.reset, self.simctl.origin(free)), (None, "erased"))
        self.assertEqual(len([call for call in self.simctl.calls if call[0] == "erase"]), erases + 2)
        self.assertEqual(devices.Record(self.path).entry("Project Duo 1")["erasedBecause"],
                         f"the copy of {template}'s data into Project Duo 1 failed: [Errno 28] clonefile: No space left on device")
        self.assertEqual(devices.Record(self.path).entry(template)["run"], "Not copied into Project Duo 1 for Tests")
        self.assertEqual(self.sleeps, [])
        self.assertTrue(pool.release(lease, outcome="done"))
        # A template that will not warm: erased, with the warm-up's words.
        warm.failure = "the boot did not finish within 300 s"
        self.simctl.runtimes[RUNTIME] = ["24B8117", "24B9000"]
        lease = pool.take(DUO, RUNTIME, purpose="Tests")
        self.assertEqual((lease.reset, self.simctl.origin(free)), (None, "erased"))
        self.assertRegex(devices.Record(self.path).entry("Project Duo 1")["erasedBecause"],
                         rf"^{re.escape(template)} \(\S+\) was not warmed: the boot did not finish within 300 s$")
        self.assertTrue(pool.release(lease, outcome="done"))
        # No readable runtime catalog: erased, saying so in the reset's words.
        self.simctl.runtimes, kept = {}, self.simctl.runtimes
        lease = pool.take(DUO, RUNTIME, purpose="Tests")
        self.assertEqual(devices.Record(self.path).entry("Project Duo 1")["erasedBecause"],
                         f"no installed build of {RUNTIME} could be read, so no template is warmed or copied")
        self.assertTrue(pool.release(lease, outcome="done"))
        self.simctl.runtimes = kept
        # Templates off, as for a pool that keeps none: erased, with nothing to say.
        lease = self.make("me").take(DUO, RUNTIME, purpose="Tests")
        self.assertIn(f"TOOK Project Duo 1 ({free}), erased", self.log)
        self.assertNotIn("erasedBecause", devices.Record(self.path).entry("Project Duo 1"))
        self.assertTrue(self.make("me").release(lease, outcome="done"))
        # Asked for: erased without a look at the template, and the record says who asked.
        warm.failure = None
        pool.release(pool.take(DUO, RUNTIME, purpose="Tests"), outcome="done")
        source = devices.Record(self.path).entry(template)["device"]
        self.assertEqual(self.simctl.origin(free), f"warmed {source}")
        before, self.sleeps[:] = len(self.simctl.calls), []
        lease = pool.take(DUO, RUNTIME, purpose="Pristine data", erase=True)
        self.assertEqual((lease.udid, lease.reset, lease.kept, self.simctl.origin(free)), (free, None, False, "erased"))
        self.assertEqual([call for call in self.simctl.calls[before:] if source in call], [])
        self.assertEqual((devices.Record(self.path).entry(template)["state"], self.sleeps), ("released", []))
        self.assertEqual(devices.Record(self.path).entry("Project Duo 1")["erasedBecause"], simulator_pool.ERASE_ASKED)
        self.assertIn(f"TOOK Project Duo 1 ({free}), erased: {simulator_pool.ERASE_ASKED}", self.log)
        self.assertTrue(pool.release(lease, outcome="done"))
        with mock.patch.object(simulator_pool, "simctl", self.simctl):
            with simulator_pool.member("Project", "iPhone-Duo", "iOS-27-1", purpose="Gate", owner="me",
                                       log=self.log.append, erase=True) as device:
                self.assertEqual(device.udid, free)
        self.assertEqual(devices.Record(self.path).entry("Project Duo 1")["erasedBecause"], simulator_pool.ERASE_ASKED)

    def test_a_take_asked_to_erase_creates_its_new_member_rather_than_cloning_the_template(self):
        warm = Warm()
        pool = self.make("me", warm=warm)
        # A first take warms the template and clones member 1 from it; it stays held.
        first = pool.take(DUO, RUNTIME, purpose="Tests")
        template = "Project iPhone Duo template (iOS-27-1)"
        self.assertEqual(first.cloned, template)
        before = len(self.simctl.calls)
        # No member is free, so an erasing take creates member 2, with simctl create, not a clone (#316).
        lease = pool.take(DUO, RUNTIME, purpose="Pristine data", erase=True)
        self.assertEqual((lease.key, lease.created, lease.cloned), ("Project iPhone Duo 2", True, None))
        self.assertEqual(self.simctl.created("clone"), ["Project iPhone Duo 1"])
        self.assertEqual(self.simctl.created("create"), [template, "Project iPhone Duo 2"])
        source = devices.Record(self.path).entry(template)["device"]
        self.assertEqual([call for call in self.simctl.calls[before:] if source in call], [])
        entry = devices.Record(self.path).entry("Project iPhone Duo 2")
        self.assertEqual(entry["erasedBecause"], simulator_pool.ERASE_ASKED)
        self.assertNotIn("clonedFrom", entry)
        self.assertIn(f"Project iPhone Duo 2 is created, not cloned: {simulator_pool.ERASE_ASKED}", self.log)
        self.assertEqual(len(warm.calls), 1)
        # Without erase, the next new member is cloned as before.
        third = pool.take(DUO, RUNTIME, purpose="Tests")
        self.assertEqual((third.key, third.cloned), ("Project iPhone Duo 3", template))
        for held in (first, lease, third):
            self.assertTrue(pool.release(held, outcome="done"))

    def test_a_template_whose_voices_did_not_settle_is_cloned_from_but_never_copied_until_refreshed(self):
        warm = Warm(settled=False)
        pool = self.make("me", warm=warm)
        free = self.simctl.add("Project Duo 1")
        template = "Project Duo template (iOS-27-1)"
        lease = pool.take(DUO, RUNTIME, purpose="Tests")
        # Warmed within the take, VoiceOver spoke, the voices did not settle: kept, but the member is erased.
        self.assertEqual((lease.reset, self.simctl.origin(free), len(warm.calls)), (None, "erased", 1))
        hint = "refresh it with `qk simulator-pool template --device iPhone-Duo --runtime iOS-27-1 --refresh`"
        why = devices.Record(self.path).entry("Project Duo 1")["erasedBecause"]
        self.assertIn("its voices did not settle: the first use gave no answer: no answer in 300 s within the take's limits; "
                      + hint, why)
        (found, current, reason), = pool.templates()
        self.assertEqual((found.name, current), (template, True))
        self.assertIn("voices: its voices did not settle: the first use gave no answer", reason)
        self.assertTrue(pool.release(lease, outcome="done"))
        # The next take does not warm it again and says the same; a new member is still cloned from it.
        again = pool.take(DUO, RUNTIME, purpose="Tests")
        self.assertEqual((again.reset, len(warm.calls)), (None, 1))
        self.assertEqual(devices.Record(self.path).entry("Project Duo 1")["erasedBecause"],
                         f"{template}: its voices did not settle: the first use gave no answer: no answer in 300 s; {hint}")
        cloned = pool.create(DUO, RUNTIME, purpose="Tests")
        self.assertEqual((cloned.cloned, len(warm.calls)), (template, 1))
        for held in (again, cloned): self.assertTrue(pool.release(held, outcome="done"))
        # The command replaces it, with the full limits, and the next take copies the replacement.
        old = devices.Record(self.path).entry(template)["device"]
        warm.settled = True
        status, output, error = self.command(warm, "template", "--device", "iPhone-Duo", "--runtime", "iOS-27-1")
        self.assertEqual(status, 0, error)
        self.assertEqual(self.simctl.deleted(), [old])
        self.assertEqual(warm.limits[-1], (600, simulator_pool.warmed.VOICEOVER_LIMIT, simulator_pool.warmed.VOICES_LIMIT))
        self.assertIn("; voices: settled: 180 voices listed 57.0 s into the first use and 24.0 s into the second", output)
        new = devices.Record(self.path).entry(template)["device"]
        reset = pool.take(DUO, RUNTIME, purpose="Tests")
        self.assertEqual((reset.udid, reset.reset, self.simctl.origin(free)), (free, template, f"warmed {new}"))
        self.assertTrue(pool.release(reset, outcome="done"))
        # Settled and current, the command leaves it alone, unless asked to refresh it.
        self.assertEqual(self.command(warm, "template", "--device", "iPhone-Duo", "--runtime", "iOS-27-1")[0], 0)
        self.assertEqual((len(warm.calls), self.simctl.deleted()), (2, [old]))
        status, output, error = self.command(warm, "template", "--device", "iPhone-Duo", "--runtime", "iOS-27-1", "--refresh")
        self.assertEqual(status, 0, error)
        self.assertEqual((len(warm.calls), self.simctl.deleted()), (3, [old, new]))
        self.assertIn(f"REPLACED the template {template} ({new}): qk simulator-pool template --refresh asked for a new one",
                      error)
        self.assertEqual(devices.Record(self.path).entry(template)["state"], "released")

    def test_a_template_made_before_settled_voices_is_remade_at_the_next_take(self):
        warm = Warm()
        pool = self.make("me", warm=warm)
        free = self.simctl.add("Project Duo 1")
        template = "Project Duo template (iOS-27-1)"
        pool.release(pool.take(DUO, RUNTIME, purpose="Tests"), outcome="done")
        old = devices.Record(self.path).entry(template)["device"]
        # Its record from before this change: VoiceOver warmed, nothing of the voices.
        value = json.loads(self.path.read_text())
        del value["devices"][template]["template"]["voices"]
        self.path.write_text(json.dumps(value))
        self.assertEqual([(current, why) for _, current, why in pool.templates()],
                         [(False, "it was warmed before templates settled the system's voices (vocaro-engineering#172)")])
        lease = pool.take(DUO, RUNTIME, purpose="Tests")
        self.assertEqual(self.simctl.deleted(), [old])
        new = devices.Record(self.path).entry(template)["device"]
        self.assertNotEqual(new, old)
        self.assertEqual((lease.reset, self.simctl.origin(free), len(warm.calls)), (template, f"warmed {new}", 2))
        self.assertTrue(any(line.startswith(f"REPLACED the template {template} ({old}): it was warmed before templates "
                                            "settled the system's voices") for line in self.log), self.log)
        self.assertTrue(pool.release(lease, outcome="done"))

    def test_the_command_lists_each_members_template_and_when_it_was_made(self):
        free = self.simctl.add("Project Duo 1")
        other = self.simctl.add("Project Duo 2")
        warm = Warm()
        template = "Project Duo template (iOS-27-1)"
        self.assertEqual(self.command(warm, "template", "--device", "iPhone-Duo", "--runtime", "iOS-27-1")[0], 0)
        made = devices.Record(self.path).entry(template)["template"]["warmedAt"]
        status, listed, _ = self.command(warm, "list")
        self.assertRegex(listed, rf"Project Duo 1  {free}  iOS-27-1  Shutdown  kept: .*; no recorded take\n")
        status, udid, error = self.command(warm, "take", "--device", "iPhone-Duo", "--runtime", "iOS-27-1")
        self.assertEqual((status, udid.strip()), (0, free), error)
        self.assertEqual(self.command(warm, "release", free)[0], 0)
        self.assertEqual(self.simctl.origin(free), f"warmed {devices.Record(self.path).entry(template)['device']}")
        status, listed, _ = self.command(warm, "list")
        self.assertRegex(listed, rf"Project Duo 1  {free}  iOS-27-1  Shutdown  kept: .*; last take \S+: reset from "
                                 rf"{re.escape(template)}, made {re.escape(made)}\n")
        self.assertRegex(listed, rf"Project Duo 2  {other}  iOS-27-1  Shutdown  kept: .*; no recorded take\n")
        self.assertRegex(listed, rf"{re.escape(template)}  \S+  iOS-27-1  Shutdown  template: warmed on runtime build "
                                 rf"24B8117 at {re.escape(made)}: spoke 50\.0 s after it was switched on; voices: settled: ")
        # A take that kept its state, and one erased because the copy failed, say so.
        status, udid, error = self.command(warm, "take", "--device", "iPhone-Duo", "--runtime", "iOS-27-1", "--keep-state")
        self.assertEqual((status, udid.strip()), (0, free), error)
        self.assertEqual(self.command(warm, "release", free)[0], 0)
        self.assertRegex(self.command(warm, "list")[1], rf"Project Duo 1  {free}  .*; last take \S+: kept its state, not erased\n")
        with mock.patch.object(simulator_pool, "copy_data", side_effect=OSError(18, "clonefile: Cross-device link")):
            status, udid, error = self.command(warm, "take", "--device", "iPhone-Duo", "--runtime", "iOS-27-1")
        self.assertEqual((status, udid.strip()), (0, free), error)
        self.assertEqual(self.command(warm, "release", free)[0], 0)
        self.assertRegex(self.command(warm, "list")[1], rf"Project Duo 1  {free}  .*; last take \S+: erased: the copy of "
                                                        rf"{re.escape(template)}'s data into Project Duo 1 failed: "
                                                        r"\[Errno 18\] clonefile: Cross-device link\n")
        # And one erased because the caller asked.
        status, udid, error = self.command(warm, "take", "--device", "iPhone-Duo", "--runtime", "iOS-27-1", "--erase")
        self.assertEqual((status, udid.strip()), (0, free), error)
        self.assertEqual(self.command(warm, "release", free)[0], 0)
        self.assertRegex(self.command(warm, "list")[1], rf"Project Duo 1  {free}  .*; last take \S+: erased: asked for with --erase\n")
        with self.assertRaises(SystemExit) as refused:
            self.command(warm, "take", "--device", "iPhone-Duo", "--runtime", "iOS-27-1", "--erase", "--keep-state")
        self.assertEqual(refused.exception.code, 2)
        # An entry from before the template's details were recorded names the template alone.
        value = json.loads(self.path.read_text())
        value["devices"]["Project Duo 2"] = {"device": other, "state": "released", "reservedAt": "2026-11-01T00:00:00+00:00",
                                             "clonedFrom": "Project Duo template (iOS-27-1)", "revision": 1}
        self.path.write_text(json.dumps(value))
        self.assertRegex(self.command(warm, "list")[1], rf"Project Duo 2  {other}  .*; last take 2026-11-01T00:00:00\+00:00: "
                                                        rf"cloned from {re.escape(template)}\n")
        self.assertEqual(simulator_pool.origin({"reservedAt": "T", "resetFrom": "Old template"}), "last take T: reset from Old template")
        # A template whose runtime is gone is a stale one that pruning may delete.
        source = devices.Record(self.path).entry(template)["device"]
        self.simctl.devices[source]["isAvailable"] = False
        self.assertRegex(self.command(warm, "list")[1], rf"{re.escape(template)}  {source}  iOS-27-1  Shutdown  stale template, "
                                                        "prunable: its runtime is no longer installed\n")

    def test_pruning_and_reaping_leave_a_template_alone_until_its_runtime_is_gone(self):
        warm = Warm()
        pool = self.make("me", warm=warm)
        self.simctl.add("Project Duo 1", days=40)
        pool.release(pool.take(DUO, RUNTIME, purpose="Tests"), outcome="done")
        template = "Project Duo template (iOS-27-1)"
        source = devices.Record(self.path).entry(template)["device"]
        self.simctl.devices[source]["lastUsedAt"] = (NOW - timedelta(days=40)).isoformat()
        # Unused for 40 days, and no member: never prunable, even by UDID, and never listed among the members.
        self.assertEqual((pool.prune(), pool.prune(udids={source}), self.simctl.deleted()), ([], [], []))
        self.assertEqual([member.name for member in pool.members()], ["Project Duo 1"])
        self.assertEqual([(found.name, current) for found, current, _ in pool.templates()], [(template, True)])
        # Its runtime gone, its kind's member with it: a booted or held template stays, a shut-down one nobody
        # holds is deleted under a reservation, and the deletion is recorded.
        del self.simctl.runtimes[RUNTIME]
        for udid in (source, *[member.udid for member in pool.members()]):
            self.simctl.devices[udid]["isAvailable"] = False
        self.assertEqual([(current, why) for _, current, why in pool.templates()], [(False, "its runtime is no longer installed")])
        self.simctl.devices[source]["state"] = "Booted"
        self.assertEqual(pool.prune(), [])
        self.simctl.devices[source]["state"] = "Shutdown"
        self.hold(template, source)
        self.assertEqual(pool.prune(), [])
        self.assertEqual(self.simctl.deleted(), [])
        record = devices.Record(self.path)
        record.release(template, owner="another session", revision=record.entry(template)["revision"], outcome="done",
                       restoration="checked")
        # A take prunes only its kind's members: the template waits for `prune`.
        self.simctl.runtimes[OTHER_RUNTIME] = ["24C5000"]
        self.simctl.add("Project Duo 2", runtime=OTHER_RUNTIME)
        pool.release(pool.take(DUO, OTHER_RUNTIME, purpose="Tests"), outcome="done")
        self.assertEqual(self.simctl.deleted(), [])
        self.assertEqual(pool.prune(), [template])
        self.assertEqual(self.simctl.deleted(), [source])
        entry = devices.Record(self.path).entry(template)
        self.assertEqual((entry["state"], entry["pruned"]["udid"], entry["pruned"]["why"], entry["pruned"]["runtime"]),
                         ("released", source, "its runtime is no longer installed", RUNTIME))
        self.assertIn(f"PRUNED the template {template} ({source}): its runtime is no longer installed", self.log)
        self.assertEqual([found.runtime for found, _, _ in pool.templates()], [OTHER_RUNTIME])

    def test_a_template_goes_with_the_last_member_of_a_kind_unused_for_7_days(self):
        pool = self.make("me", warm=Warm())
        member = self.simctl.add("Project Duo 1", days=40)
        pool.release(pool.take(DUO, RUNTIME, purpose="Tests"), outcome="done")
        template = "Project Duo template (iOS-27-1)"
        source = devices.Record(self.path).entry(template)["device"]
        def age(days):
            for udid in (member, source): self.simctl.devices[udid]["lastUsedAt"] = (NOW - timedelta(days=days)).isoformat()
            value = json.loads(self.path.read_text())
            for entry in value["devices"].values(): entry["releasedAt"] = (NOW - timedelta(days=days)).isoformat()
            self.path.write_text(json.dumps(value))
        # Within the 7 days the kind keeps both, and a template whose kind has a member is never named away.
        age(6)
        self.assertEqual((pool.prune(), pool.prune(udids={source}), self.simctl.deleted()), ([], [], []))
        age(8)
        self.assertEqual((pool.prune(udids={source}), self.simctl.deleted()), ([], []))
        # Naming the member, as `qk disk` does, deletes the kind's template after it.
        self.assertEqual(pool.prune(udids={member}), ["Project Duo 1", template])
        self.assertEqual(self.simctl.deleted(), [member, source])
        entry = devices.Record(self.path).entry(template)
        self.assertEqual((entry["state"], entry["pruned"]["udid"]), ("released", source))
        self.assertIn("its kind has no member left and it is unused since", entry["pruned"]["why"])
        self.assertEqual(pool.templates(), [])

    def test_the_command_succeeds_when_a_template_follows_the_member_it_was_asked_to_prune(self):
        # `qk disk` names the member alone and reads the status: the template pruned after it is no failure.
        pool = self.make("me", warm=Warm())
        member = self.simctl.add("Project Duo 1", days=40)
        pool.release(pool.take(DUO, RUNTIME, purpose="Tests"), outcome="done")
        template = "Project Duo template (iOS-27-1)"
        source = devices.Record(self.path).entry(template)["device"]
        for udid in (member, source): self.simctl.devices[udid]["lastUsedAt"] = (NOW - timedelta(days=8)).isoformat()
        value = json.loads(self.path.read_text())
        for entry in value["devices"].values(): entry["releasedAt"] = (NOW - timedelta(days=8)).isoformat()
        self.path.write_text(json.dumps(value))
        def command(*arguments):
            out = StringIO()
            with mock.patch.object(simulator_pool, "simctl", self.simctl), redirect_stdout(out), redirect_stderr(StringIO()):
                return simulator_pool.main(["--pool", "Project", "--owner", "me", "prune", *arguments]), out.getvalue()
        # A UDID simctl does not list was never pruned.
        self.assertEqual(command(str(uuid.uuid4()).upper())[0], 1)
        self.assertEqual(self.simctl.deleted(), [])
        self.assertEqual(command(member), (0, f"Project Duo 1\n{template}\n"))
        self.assertEqual(self.simctl.deleted(), [member, source])


if __name__ == "__main__":
    unittest.main()
