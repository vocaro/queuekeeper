"""`eng simulator-pool reap` (offload_host.reap) over fake ps, lsof and simctl listings of a Mac whose home
is a temporary directory, with its own device record, admission guard and log. No test runs simctl."""
import contextlib
from contextlib import redirect_stdout
import fcntl
from io import StringIO
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/python"))
from vocaro_engineering import devices, offload_host, remote
import host_isolation
from vocaro_engineering.simulators import pool as simulator_pool

UDID = "AAAAAAAA-BBBB-4CCC-8DDD-EEEEEEEEEEEE"
OTHER = "11111111-2222-4333-8444-555555555555"
NAME = "Pagepour Duo 1"
# The pool's warmed template of the kind, on its own runtime, as simulators/pool.py names and lists one.
TEMPLATE = "Pagepour Duo template (iOS-27-2)"
RUNTIME = "com.apple.CoreSimulator.SimRuntime.iOS-27-2"
LAUNCHD = 7000
HOLDER = 6000


def ago(seconds):
    return " ".join(time.strftime(offload_host.STARTED, time.localtime(time.time() - seconds)).split())


class Process:
    """What start_shutdown returns: a shutdown that ends at once with status, or never finishes."""

    def __init__(self, status=0, error="", hangs=False):
        self.returncode, self.error, self.hangs, self.killed = status, error, hangs, False

    def communicate(self, timeout=None):
        if self.hangs and not self.killed: raise subprocess.TimeoutExpired("simctl", timeout)
        return "", self.error

    def kill(self):
        self.killed = True


class ReapTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name).resolve()
        self.home, self.slots = base / "home", base / "slots"
        self.home.mkdir()
        self.slots.mkdir()
        self.record = base / "device-handoff.json"
        environment = patch.dict(os.environ, {"HOME": str(self.home), "TMPDIR": "/var/folders/zz/fake/T/",
                                              "HEAVY_SLOT_DIR": str(self.slots), "SW_GATE_LOCK_FILE": str(base / "gate"),
                                              "VOCARO_DEVICE_RECORD": str(self.record)})
        environment.start()
        self.addCleanup(environment.stop)
        # Any path past these fakes fails rather than reaching this host's simulators.
        for name in ("start_shutdown", "listing"):
            guard = patch.object(offload_host, name, side_effect=AssertionError(f"{name} reached this host"))
            guard.start()
            self.addCleanup(guard.stop)
        self.guard = Path(os.environ["SW_GATE_LOCK_FILE"] + ".warm-push-guard")
        self.log = self.slots / offload_host.REAP_LOG
        self.uid = os.getuid()
        self.table, self.directories, self.names, self.runtimes = {}, {}, {UDID: NAME}, {}
        self.states, self.looks, self.between, self.stops, self.outcome = {}, 0, None, [], Process()
        self.simctl_fails, self.listed_self = False, True
        # Each process's environment, by PID, as `ps -E` appends it to its command line.
        self.environments = {os.getpid(): "PATH=/usr/bin:/bin"}
        self.simulator()

    def simulator(self, udid=UDID, pid=LAUNCHD, age=3 * 3600):
        devices_directory = self.home / offload_host.DEVICES
        self.process(pid, 1, f"launchd_sim {devices_directory}/{udid}/data/var/run/launchd_bootstrap.plist", age)
        self.process(pid + 1, pid, f"{devices_directory}/{udid}/data/Containers/Bundle/Application/X/Example.app/Example", age)

    def process(self, pid, parent, args, age=3 * 3600, start=None):
        self.table[pid] = (parent, self.uid, start or ago(age), args)

    def listed(self, command):
        if command == list(offload_host.ENVIRONMENTS):
            own = {os.getpid(): (1, self.uid, ago(60), "python3 -m unittest")} if self.listed_self else {}
            return "".join(f"{pid:>6} {parent:>6} {uid:>5} {start} {args} {self.environments.get(pid, '')}\n"
                           for pid, (parent, uid, start, args) in {**own, **self.table}.items())
        if command[0] == "ps":
            self.looks += 1
            if self.looks == 2 and self.between: self.between()
            # The reap's own process, which a real ps lists, unless a test leaves it out.
            own = {os.getpid(): (1, self.uid, ago(60), "python3 -m unittest")} if self.listed_self else {}
            return "".join(f"{pid:>6} {parent:>6} {uid:>5} {start} {args}\n"
                           for pid, (parent, uid, start, args) in {**own, **self.table}.items())
        if command[0] == "lsof":
            return f"p{os.getpid()}\nfcwd\nn{self.home}\n"
        if command[:3] == ["xcrun", "simctl", "list"]:
            if self.simctl_fails: return ""
            groups = {}
            for udid, name in self.names.items():
                groups.setdefault(self.runtimes.get(udid, "iOS"), []).append(
                    {"udid": udid, "name": name, "state": self.states.get(udid, "Booted")})
            return json.dumps({"devices": groups})
        self.fail(f"an unexpected command: {command}")

    def begin(self, udid):
        self.stops.append(udid)
        if not self.outcome.returncode and not self.outcome.hangs: self.states[udid] = "Shutdown"
        return self.outcome

    def reap(self, **options):
        options.setdefault("scan", False)
        return offload_host.reap(remote.ROOT, ["Pagepour"], listed=self.listed, begin=self.begin, **options)

    def reserve(self, key=NAME, **entry):
        self.record.write_text(json.dumps({"vocaroDeviceHandoff": 1, "revision": 1, "devices": {key: {
            "revision": 1, "device": UDID, "ownerChatID": "session", "state": "reserved", "activeRun": False,
            "purpose": "tests", **entry}}}))

    def template(self):
        """setUp's simulator as the pool's warmed template instead of a member."""
        self.names[UDID], self.runtimes[UDID] = TEMPLATE, RUNTIME

    def assertKept(self, report, reason):
        self.assertEqual(self.stops, [], report)
        self.assertEqual(report["stopped"], [], report)
        self.assertRegex(" ".join(item["hold"] for item in report["kept"] + report["skipped"]), reason)
        self.assertFalse(self.log.exists(), "a simulator kept running is no stop to log")

    def assertStopped(self, report):
        self.assertIsNone(report["problem"], report)
        self.assertEqual(self.stops, [UDID], report)
        self.assertEqual([item["udid"] for item in report["stopped"]], [UDID])

    DUE = "2026-01-01T00:00:00+00:00"

    def held_entry(self, pid=HOLDER, start="Thu Jan  1 00:00:00 2026", due=DUE, **more):
        return {"holderProcess": {"pid": pid, "start": start}, "expectedReleaseBy": due, "revision": 7, **more}

    def assertNamed(self, report, key=NAME, hold=None):
        """The entry stays held, and is named with why and its recover command, which on the second Mac runs
        there (vocaro-engineering#156)."""
        self.assertEqual([item["key"] for item in report["recoverable"]], [key], report)
        self.assertEqual(report["released"], [], report)
        if hold: self.assertRegex(report["recoverable"][0]["hold"], hold)
        command = (f"python3 -m vocaro_engineering.devices recover '{key}' --by SESSION --revision 7 "
                   "--verified 'WHAT YOU CHECKED'")
        line = next(line for line in remote.reap_lines(remote.HERE, report) if "stays held" in line)
        self.assertIn("owner session", line)
        self.assertIn(f"due {self.DUE}", line)
        if hold: self.assertRegex(line, "reap did not release it: .*" + hold)
        self.assertIn(f"recovers it: {command}", line)
        there = next(line for line in remote.reap_lines("build-host", report) if "stays held" in line)
        self.assertIn("recovers it: ssh build-host " + shlex.quote(f"cd {offload_host.TOOLKIT} && {command}"), there)

    def assertReleased(self, key=NAME, condition="held", pids=(HOLDER,)):
        """The entry released as `devices recover --by reap` records a recovery, at the next revision, and
        logged, with what reap checked (vocaro-engineering#156)."""
        entry = devices.Record(self.record).entry(key)
        self.assertEqual(devices.condition(entry), "released", entry)
        self.assertEqual((entry["revision"], entry["ownerChatID"], entry["lastOwnerChatID"], entry["state"]),
                         (8, None, "session", "released"))
        recovery = entry["recovery"]
        self.assertEqual((recovery["by"], recovery["condition"]), ("reap", condition))
        for pid in pids: self.assertIn(f"PID {pid}", recovery["verified"])
        self.assertIn(f"simctl showed {UDID} Shutdown", recovery["verified"])
        self.assertIn(f"expected release, {self.DUE}, had passed", recovery["verified"])
        lines = [json.loads(line) for line in self.log.read_text().splitlines()]
        releases = [line for line in lines if line.get("key") == key]
        self.assertEqual([line["event"] for line in releases], ["releasing", "released"])
        self.assertEqual(releases[-1]["verified"], recovery["verified"])
        return entry

    def test_reap_releases_a_held_entry_whose_process_is_gone_and_whose_release_has_passed(self):
        # The regression test of vocaro-engineering#156: before it, reap stopped the simulator and left the
        # entry held for a session to recover by hand.
        self.reserve(**self.held_entry())
        report = self.reap()
        self.assertStopped(report)
        self.assertEqual(([item["key"] for item in report["released"]], report["recoverable"]), ([NAME], []))
        self.assertReleased()
        # A dispatch's reap says so too, and the next reservation of the member takes it as usual.
        said = "\n".join(remote.reap_lines("fake", report, listing=False))
        self.assertIn(f"on fake, released {NAME} ({UDID}), held for owner session: reap on this Mac found PID 6000",
                      said)
        self.assertNotIn("stays held", said)
        future = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() + 3600))
        devices.Record(self.record).reserve(NAME, owner="next", revision=8, purpose="tests", release_by=future,
                                            device=UDID)

    def test_one_already_shut_down_is_released_without_a_stop(self):
        self.table.clear()
        self.process(5000, 1, "/usr/bin/other")
        self.states[UDID] = "Shutdown"
        self.reserve(**self.held_entry())
        report = self.reap()
        self.assertEqual((self.stops, report["problem"]), ([], None), report)
        self.assertReleased()
        self.assertIn(f"released {NAME}", "\n".join(remote.reap_lines("fake", report, listing=False)))

    def test_a_reaped_templates_dead_entry_is_released(self):
        self.template()
        self.reserve(TEMPLATE, **self.held_entry())
        report = self.reap()
        self.assertStopped(report)
        self.assertReleased(TEMPLATE)

    def test_a_reused_pid_with_another_start_is_a_process_that_has_gone(self):
        self.process(HOLDER, 1, "/usr/bin/other", start="Mon Feb  2 00:00:00 2026")
        self.reserve(**self.held_entry())
        self.reap()
        self.assertReleased()

    def test_an_orphaned_active_run_is_released_as_an_orphan(self):
        self.reserve(**self.held_entry(activeRun=True, hostProcessIdentifier=4242, hostProcessStart=ago(3600),
                                       ownedProcesses=[{"pid": 4243, "start": ago(3600)}]))
        self.reap()
        self.assertFalse(self.assertReleased(condition="orphan", pids=(HOLDER, 4242, 4243))["activeRun"])

    def test_a_dry_run_says_what_it_would_release_and_changes_nothing(self):
        self.states[UDID] = "Shutdown"
        self.reserve(**self.held_entry())
        before = self.record.read_bytes()
        report = self.reap(dry_run=True)
        self.assertEqual([item["key"] for item in report["released"]], [NAME])
        self.assertIn(f"would release {NAME}", "\n".join(remote.reap_lines("fake", report)))
        self.assertEqual(self.record.read_bytes(), before)
        self.assertFalse(self.log.exists())
        self.assertFalse(self.record.with_name(self.record.name + ".lock").exists())

    def test_a_live_holder_is_neither_named_nor_released(self):
        self.process(HOLDER, 1, "/usr/bin/holder", start="Thu Jan  1 00:00:00 2026")
        self.states[UDID] = "Shutdown"
        self.reserve(**self.held_entry())
        before = self.record.read_bytes()
        report = self.reap()
        self.assertEqual((report["recoverable"], report["released"]), ([], []))
        self.assertNotIn("stays held", "\n".join(remote.reap_lines("fake", report)))
        self.assertEqual(self.record.read_bytes(), before)

    def test_what_may_still_be_held_is_neither_named_nor_released(self):
        future = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() + 3600))
        self.states[UDID] = "Shutdown"
        for name, entry in (("release not due", self.held_entry(due=future)),
                            ("no release", self.held_entry(due=None)),
                            ("unreadable release", self.held_entry(due="soon")),
                            ("no process", {"expectedReleaseBy": self.DUE, "revision": 7}),
                            ("no start for a running PID", self.held_entry(pid=LAUNCHD, start=None)),
                            ("a live owned process", self.held_entry(ownedProcesses=[
                                {"pid": LAUNCHD, "start": self.table[LAUNCHD][2]}])),
                            ("uncertain", self.held_entry(uncertain=True)),
                            ("restoration unverified", self.held_entry(state="restoration-unverified")),
                            ("released", self.held_entry(state="released", ownerChatID=None))):
            with self.subTest(name):
                self.reserve(**entry)
                before = self.record.read_bytes()
                for dry_run in (True, False):
                    report = self.reap(dry_run=dry_run)
                    # One not yet due is named in a listing, as a take's still held by its session.
                    named = [item for item in report["recoverable"] if not (name == "release not due" and
                             item.get("early") and "does not record that only its holder" in item["hold"])]
                    self.assertEqual((named, report["released"]), ([], []), report)
                self.assertEqual(self.record.read_bytes(), before)

    def test_what_reap_cannot_prove_dead_here_is_named_with_why_and_left(self):
        def unlisted(): self.names.pop(UDID)
        def renamed(): self.names[UDID] = "Pagepour Duo 2"
        def outside(): self.names[UDID] = "Elsewhere Duo 1"
        def unlisting(): self.listed_self = False
        def booted():
            self.simulator(age=3600)
            self.states[UDID] = "Booted"
        cases = [
            ("still booted, under 2 hours", booted, {}, "simctl shows it Booted, not Shutdown"),
            ("not listed by simctl here", unlisted, {}, f"simctl here does not list {UDID}"),
            ("listed under another name", renamed, {}, "simctl lists .* as 'Pagepour Duo 2'"),
            ("no pool's", outside, {"key": "Elsewhere Duo 1"}, "neither a member nor a warmed template"),
            ("its run went to the other Mac", None, {"run": {"host": "build-host", "startedAt": self.DUE}},
             "its run went to build-host, whose processes this Mac cannot check"),
            ("a physical device", None, {"device": "00008110-0011223344556677"}, "records no simulator"),
            ("a listing without this reap", unlisting, {}, "does not show this reap's own process"),
        ]
        for name, arrange, more, hold in cases:
            with self.subTest(name):
                self.setUp()
                self.table.clear()
                self.process(5000, 1, "/usr/bin/other")
                self.states[UDID] = "Shutdown"
                if arrange: arrange()
                key = more.pop("key", NAME)
                self.reserve(key, **self.held_entry(**more))
                before = self.record.read_bytes()
                report = self.reap()
                self.assertNamed(report, key=key, hold=hold)
                self.assertEqual(self.record.read_bytes(), before)

    def test_another_entry_holding_the_device_leaves_both(self):
        self.states[UDID] = "Shutdown"
        self.record.write_text(json.dumps({"vocaroDeviceHandoff": 1, "revision": 1, "devices": {
            NAME: {"device": UDID, "ownerChatID": "session", "state": "reserved", "activeRun": False,
                   **self.held_entry()},
            "another key": {"device": UDID, "ownerChatID": "other", "state": "reserved", "revision": 3}}}))
        before = self.record.read_bytes()
        report = self.reap()
        self.assertNamed(report, hold="its device is also held as another key")
        self.assertEqual(self.record.read_bytes(), before)

    def test_simctl_that_cannot_list_releases_nothing(self):
        self.table.clear()
        self.process(5000, 1, "/usr/bin/other")
        self.simctl_fails = True
        self.reserve(**self.held_entry())
        before = self.record.read_bytes()
        self.assertNamed(self.reap(), hold="simctl did not list its devices")
        self.assertEqual(self.record.read_bytes(), before)

    def test_a_reap_with_a_problem_or_no_pool_releases_nothing(self):
        self.states[UDID] = "Shutdown"
        self.reserve(**self.held_entry())
        before = self.record.read_bytes()
        self.guard.write_text("not a guard")
        self.assertNamed(self.reap(), hold="this reap met a problem")
        self.guard.unlink()
        report = offload_host.reap(remote.ROOT, [], listed=self.listed, begin=self.begin, scan=False)
        self.assertNamed(report, hold="no simulator pool was named or found")
        self.assertEqual(self.record.read_bytes(), before)

    def test_an_entry_that_changes_before_the_lock_is_left(self):
        self.states[UDID] = "Shutdown"
        self.reserve(**self.held_entry())
        lock = offload_host.record_lock

        @contextlib.contextmanager
        def moved():
            # Another session's change lands between reap's read and its lock.
            record = json.loads(self.record.read_text())
            record["devices"][NAME]["revision"] = 8
            self.record.write_text(json.dumps(record))
            with lock(): yield

        with patch.object(offload_host, "record_lock", moved):
            report = self.reap()
        self.assertEqual(report["released"], [])
        self.assertEqual(report["recoverable"][0]["hold"], "its entry changed after reap read it")
        self.assertEqual(devices.Record(self.record).entry(NAME)["state"], "reserved")
        self.assertFalse(self.log.exists())

    def test_a_release_that_cannot_be_logged_releases_nothing(self):
        self.table.clear()
        self.process(5000, 1, "/usr/bin/other")
        self.states[UDID] = "Shutdown"
        self.reserve(**self.held_entry())
        before = self.record.read_bytes()
        self.log.mkdir()
        report = self.reap()
        self.assertIn("Error", report["problem"])
        self.assertEqual(self.record.read_bytes(), before)

    def test_a_doctor_names_every_dead_entry_reap_left(self):
        self.table.clear()
        self.process(5000, 1, "/usr/bin/other")
        self.reserve(**self.held_entry())
        report = self.reap()
        self.assertEqual(remote.reap_lines("fake", report, listing=False), [])
        self.assertIn("stays held", "\n".join(remote.reap_lines("fake", report, listing=False, held=True)))

    # Before its expected release (vocaro-engineering#156, the owner's decision of 2026-11-04 in chat).

    OWNER = "5f9a6753-0000-4000-8000-000000000001"

    def early_entry(self, **more):
        """A run's reservation, which only its holder held, not due for an hour, whose holder is gone."""
        future = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() + 3600))
        return self.held_entry(due=future, ownerChatID=self.OWNER, **{offload_host.ALONE: True, **more})

    def early(self, **more):
        """setUp's member shut down, with nothing else running, and its early entry reserved."""
        self.table.clear()
        self.process(5000, 1, "/usr/bin/other")
        self.states[UDID] = "Shutdown"
        self.reserve(**self.early_entry(**more))
        return self.record.read_bytes()

    def assertHeldEarly(self, report, hold, before):
        self.assertEqual(report["released"], [], report)
        self.assertEqual([item["key"] for item in report["recoverable"]], [NAME], report)
        self.assertTrue(report["recoverable"][0]["early"])
        self.assertRegex(report["recoverable"][0]["hold"], hold)
        self.assertEqual(self.record.read_bytes(), before)
        listed = "\n".join(remote.reap_lines(remote.HERE, report))
        self.assertIn("its release is due", listed)
        self.assertRegex(listed, "reap did not release it: .*" + hold)
        # Only a listing names one not yet due: before a dispatch and in doctor most are a take's, still held.
        self.assertNotIn("stays held", "\n".join(remote.reap_lines(remote.HERE, report, listing=False, held=True)))

    def test_a_dead_runs_reservation_is_released_before_its_release_when_nothing_accounts_for_its_owner(self):
        self.early()
        report = self.reap()
        self.assertEqual(([item["key"] for item in report["released"]], report["recoverable"], report["problem"]),
                         ([NAME], [], None), report)
        entry = devices.Record(self.record).entry(NAME)
        self.assertEqual((devices.condition(entry), entry["revision"], entry["lastOwnerChatID"]),
                         ("released", 8, self.OWNER))
        recovery = entry["recovery"]
        self.assertEqual((recovery["by"], recovery["qualified"]), ("reap", "early: nothing accounts for its owner"))
        self.assertIn("had not passed, but it recorded that only its holder held it", recovery["verified"])
        self.assertIn(f"no live admission or other process of the account names its owner {self.OWNER}",
                      recovery["verified"])
        self.assertIn(f"simctl showed {UDID} Shutdown", recovery["verified"])
        lines = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual([(line["event"], line["qualified"]) for line in lines],
                         [("releasing", "early: nothing accounts for its owner"),
                          ("released", "early: nothing accounts for its owner")])
        self.assertIn(f"released {NAME}", "\n".join(remote.reap_lines("fake", report, listing=False)))

    def test_one_whose_release_passed_says_so(self):
        self.table.clear()
        self.process(5000, 1, "/usr/bin/other")
        self.states[UDID] = "Shutdown"
        self.reserve(**self.held_entry())
        self.reap()
        self.assertEqual(self.assertReleased()["recovery"]["qualified"], "its release passed")
        self.assertEqual(json.loads(self.log.read_text().splitlines()[-1])["qualified"], "its release passed")

    def test_a_dry_run_says_it_would_release_one_early_and_changes_nothing(self):
        before = self.early()
        report = self.reap(dry_run=True)
        self.assertEqual([item["key"] for item in report["released"]], [NAME])
        self.assertIn(f"would release {NAME}", "\n".join(remote.reap_lines("fake", report)))
        self.assertEqual(self.record.read_bytes(), before)
        self.assertFalse(self.log.exists())

    def test_a_take_that_its_session_still_holds_is_never_released_early(self):
        # `eng simulator-pool take` records no ALONE: its session holds the member by its word between commands.
        before = self.early(**{offload_host.ALONE: None})
        self.assertHeldEarly(self.reap(), "does not record that only its holder held it", before)

    def test_a_live_admission_that_names_the_owner_keeps_it(self):
        before = self.early()
        self.process(6100, 900, "python3 scripts/eng admit -- swift test")
        self.process(6101, 6100, "swift test")
        self.environments[6101] = f"PATH=/usr/bin CLAUDE_CODE_SESSION_ID={self.OWNER}"
        self.guard.write_text(json.dumps({"vocaroEngineering": 1, "owners": {"a" * 32: {
            "pid": 6100, "start": self.table[6100][2], "label": "swift test"}}}))
        self.assertHeldEarly(self.reap(), "live admission PID 6100 accounts for its owner: PID 6101 names", before)

    def test_an_unended_admission_in_the_log_that_names_the_owner_keeps_it(self):
        before = self.early()
        self.process(6100, 900, "python3 scripts/eng run gate", age=600)
        self.environments[6100] = f"VOCARO_DEVICE_OWNER={self.OWNER}"
        (self.slots / "admissions.jsonl").write_text(json.dumps({
            "event": "admitted", "identity": "b" * 32, "pid": 6100,
            "time": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())}) + "\n")
        self.assertHeldEarly(self.reap(), "live admission PID 6100 accounts for its owner", before)

    def test_any_process_naming_the_owner_or_the_device_keeps_it(self):
        for name, arrange, hold in (
                ("its environment", lambda: self.environments.update({5000: f"CODEX_THREAD_ID={self.OWNER}"}),
                 f"PID 5000 names its owner {self.OWNER}"),
                ("its command line", lambda: self.process(5100, 1, f"tool --owner {self.OWNER}"),
                 f"PID 5100 names its owner"),
                ("this reap's own", lambda: self.environments.update({os.getpid(): f"PATH=/bin X={self.OWNER}"}),
                 "names its owner"),
                ("the device", lambda: self.process(5100, 1, f"xcrun simctl io {UDID.lower()} screenshot x.png"),
                 f"PID 5100 names its device {UDID}")):
            with self.subTest(name):
                self.setUp()
                before = self.early()
                arrange()
                self.assertHeldEarly(self.reap(), hold, before)

    def test_what_cannot_be_checked_keeps_it(self):
        def unreadable_log(): (self.slots / "admissions.jsonl").mkdir()
        def no_environments(): self.environments[os.getpid()] = ""
        def short_owner():
            entry = json.loads(self.record.read_text())
            entry["devices"][NAME]["ownerChatID"] = "session"
            self.record.write_text(json.dumps(entry))
        for name, arrange, hold in (
                ("an unreadable admission log", unreadable_log, "this reap met a problem"),
                ("a listing without environments", no_environments, "ps did not show this reap's own environment"),
                ("an owner too short to search for", short_owner, "too short to search processes for")):
            with self.subTest(name):
                self.setUp()
                self.early()
                arrange()
                before = self.record.read_bytes()
                self.assertHeldEarly(self.reap(), hold, before)

    def test_admissions_that_cannot_be_read_for_the_owner_keep_it(self):
        before = self.early()
        with patch.object(offload_host, "live_admissions", side_effect=offload_host.ReapProblem("the admission log broke")):
            report = self.reap()
        self.assertHeldEarly(report, "its owner could not be checked: the admission log broke", before)

    def test_an_early_one_still_booted_is_kept_running_and_held(self):
        before = self.early()
        self.simulator()
        self.states[UDID] = "Booted"
        report = self.reap()
        self.assertEqual(self.stops, [])
        self.assertKept(report, "not due for release")
        self.assertHeldEarly(report, "simctl shows it Booted", before)

    def test_a_runs_pool_marks_its_reservations_alone_and_a_take_does_not(self):
        future = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() + 3600))
        for alone in (True, False):
            with self.subTest(alone=alone):
                pool = simulator_pool.Pool("Pagepour", owner=self.OWNER, record=devices.Record(self.record),
                                           alone=alone)
                key = f"Pagepour Duo {2 if alone else 3}"
                with patch.object(simulator_pool.Pool, "_release_by", staticmethod(lambda: future)):
                    pool._reserve(key, "tests")
                entry = devices.Record(self.record).entry(key)
                self.assertIn(devices.HOLDER, entry)
                self.assertEqual(entry.get(devices.ALONE), True if alone else None)
        made = []

        class Recording:
            def __init__(self, *arguments, **options): made.append(options.get("alone"))
            take = lambda self, *arguments, **options: unittest.mock.Mock(udid=UDID)

        with patch.object(simulator_pool, "Pool", Recording), patch.object(simulator_pool, "boot", return_value=None), \
                redirect_stdout(StringIO()):
            simulator_pool.main(["--pool", "Pagepour", "--owner", self.OWNER, "take", "--device", "iPhone-17-Pro",
                                 "--runtime", "iOS-27-2"])
        self.assertEqual(made, [False])

    def test_an_unlistable_host_names_nothing_and_says_so(self):
        self.reserve(**self.held_entry())
        listed = lambda command: None if command[0] == "ps" else self.listed(command)
        report = offload_host.reap(remote.ROOT, [], listed=listed, scan=False)
        self.assertEqual(report["recoverable"], [])
        self.assertIn("could not be listed", report["problem"])

    def test_a_stale_member_is_stopped_once_and_logged_with_its_udid_name_age_and_reason(self):
        report = self.reap()
        self.assertStopped(report)
        lines = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual([line["event"] for line in lines], ["stopping", "stopped"])
        for line in lines:
            self.assertEqual((line["udid"], line["name"], line["pid"]), (UDID, NAME, LAUNCHD))
            self.assertGreaterEqual(line["seconds"], 3 * 3600 - 5)
            self.assertTrue(line["reason"].startswith("member of pool Pagepour, left over"), line)
        said = "\n".join(remote.reap_lines("fake", report, listing=False))
        self.assertIn(f"on fake, stopped {NAME} ({UDID})", said)
        self.assertIn("has run 3.0 h", said)

    def test_a_dry_run_lists_and_stops_writes_and_locks_nothing(self):
        report = self.reap(dry_run=True)
        self.assertEqual((self.stops, report["stopped"]), ([], []))
        self.assertEqual([item["udid"] for item in report["eligible"]], [UDID])
        self.assertIn("would stop", remote.reap_lines("fake", report)[0])
        self.assertFalse(self.log.exists())
        self.assertFalse(self.record.with_name(self.record.name + ".lock").exists())

    # Condition 1: a member of a pool.

    def test_a_simulator_outside_every_pool_is_never_stopped(self):
        for name in ("iPhone 17 Pro", "Pagepour", "Pagepour Duo", "PagepourX Duo 1", "Pagepour Duo 0"):
            with self.subTest(name):
                self.names[UDID] = name
                self.assertKept(self.reap(), "neither a member of a simulator pool nor a pool's warmed template")

    # Condition 1 also takes in the pool's warmed template, by the owner's decision of 2026-11-04.

    def test_a_stale_template_is_stopped_and_logged_as_the_pools_warmed_template(self):
        self.template()
        report = self.reap()
        self.assertStopped(report)
        lines = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual([(line["event"], line["udid"], line["name"]) for line in lines],
                         [("stopping", UDID, TEMPLATE), ("stopped", UDID, TEMPLATE)])
        for line in lines:
            self.assertTrue(line["reason"].startswith("warmed template of pool Pagepour, left over"), line)
        said = "\n".join(remote.reap_lines("fake", report, listing=False))
        self.assertIn(f"on fake, stopped {TEMPLATE} ({UDID}), PID {LAUNCHD}: warmed template of pool Pagepour", said)

    def test_a_name_like_a_templates_that_the_pool_does_not_list_as_one_is_never_stopped(self):
        for name, runtime in ((TEMPLATE, "com.apple.CoreSimulator.SimRuntime.iOS-27-1"), (TEMPLATE, "iOS"),
                              ("Pagepour template (iOS-27-2)", RUNTIME), ("Pagepour Duo template", RUNTIME),
                              ("Pagepour Duo template ()", RUNTIME), ("PagepourX Duo template (iOS-27-2)", RUNTIME),
                              ("Pagepour Duo template (iOS-27-2) copy", RUNTIME)):
            with self.subTest(name=name, runtime=runtime):
                self.names[UDID], self.runtimes[UDID] = name, runtime
                self.assertKept(self.reap(), "neither a member of a simulator pool nor a pool's warmed template")

    def test_it_reads_a_template_as_the_pool_does(self):
        self.assertEqual(offload_host.TEMPLATE.pattern, simulator_pool.TEMPLATE.pattern)
        for name, runtime in ((TEMPLATE, RUNTIME), (TEMPLATE, "com.apple.CoreSimulator.SimRuntime.iOS-27-1"),
                              ("Pagepour Duo 3", RUNTIME), ("Pagepour template (iOS-27-2)", RUNTIME)):
            with self.subTest(name=name, runtime=runtime):
                listing = {"devices": {runtime: [{"udid": UDID, "name": name, "deviceTypeIdentifier": "Duo"}]}}
                listed = bool(simulator_pool.templates("Pagepour", listing))
                self.assertEqual(offload_host.pool_of(name, ["Pagepour"], runtime)[1] == "template", listed)

    def test_a_template_under_2_hours_is_kept(self):
        self.template()
        self.simulator(age=offload_host.REAP_AFTER - 60)
        self.assertKept(self.reap(), "under 2 h")

    def test_a_template_whose_warm_up_still_runs_is_kept(self):
        # Pool.template reserves the template in the warming process's name, its holder, before it boots it.
        self.template()
        self.process(HOLDER, 900, "python3 -m vocaro_engineering.simulators.pool template --device iPhone-Duo "
                                  "--runtime iOS-27-2")
        self.reserve(TEMPLATE, holderProcess={"pid": HOLDER, "start": self.table[HOLDER][2]}, expectedReleaseBy=self.DUE)
        self.assertKept(self.reap(), f"its reservation {TEMPLATE.replace('(', '[(]').replace(')', '[)]')} is live: "
                                     f"PID {HOLDER} still runs")

    def test_a_killed_warm_ups_template_is_kept_until_its_expected_release(self):
        self.template()
        future = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() + 3600))
        self.reserve(TEMPLATE, **self.held_entry(due=future))
        self.assertKept(self.reap(), "is not due for release until")

    def test_a_template_another_session_holds_by_name_or_udid_is_kept(self):
        self.template()
        self.process(HOLDER, 900, "python3 tool")
        live = {"holderProcess": {"pid": HOLDER, "start": self.table[HOLDER][2]}}
        for key, device in ((TEMPLATE, None), ("another key", UDID)):
            with self.subTest(key=key):
                self.reserve(key, device=device, **live)
                self.assertKept(self.reap(), f"is live: PID {HOLDER}")

    def test_no_pool_named_or_found_lists_nothing_and_asks_nothing_of_simctl(self):
        self.simctl_fails = True
        report = offload_host.reap(remote.ROOT, [], listed=self.listed, begin=self.begin)
        self.assertEqual((report["pools"], report["kept"], report["problem"], self.looks), ([], [], None, 0))

    def test_a_scan_finds_the_pool_a_checkout_names(self):
        checkout = self.home / remote.ROOT / "checkouts/pagepour/0/pagepour"
        checkout.mkdir(parents=True)
        (checkout / "engineering.json").write_text(json.dumps({"simulatorPool": "Pagepour"}))
        report = offload_host.reap(remote.ROOT, [], listed=self.listed, begin=self.begin)
        self.assertStopped(report)

    # Condition 2: left over.

    def test_a_live_admission_that_holds_it_keeps_it_out_of_the_census(self):
        self.process(HOLDER, 900, "python3 eng simulator-tests")
        self.guard.write_text(json.dumps({"vocaroEngineering": 1, "owners": {"a" * 32: {
            "pid": HOLDER, "start": self.table[HOLDER][2]}}}))
        self.reserve(holderProcess={"pid": HOLDER, "start": self.table[HOLDER][2]})
        report = self.reap()
        self.assertEqual((self.stops, report["kept"], report["problem"]), ([], [], None), report)

    def test_a_process_outside_it_that_names_its_udid_keeps_it(self):
        self.process(6100, 900, f"xcodebuild test -destination id={UDID}")
        self.assertKept(self.reap(), "PID 6100 names its UDID")

    def test_its_own_processes_naming_it_do_not_keep_it(self):
        # setUp's simulator has a descendant whose path names the UDID: the positive control above stops it too.
        self.assertStopped(self.reap())

    # Condition 3: no live reservation.

    def test_a_live_reservation_keeps_it(self):
        self.process(HOLDER, 900, "python3 tool")
        self.reserve(holderProcess={"pid": HOLDER, "start": self.table[HOLDER][2]})
        self.assertKept(self.reap(), f"is live: PID {HOLDER}")

    def test_a_reservation_of_its_name_with_no_device_recorded_yet_keeps_it(self):
        self.reserve(device=None)
        self.assertKept(self.reap(), "records no process")

    def test_a_reservation_that_records_no_process_keeps_it(self):
        self.reserve()
        self.assertKept(self.reap(), "records no process")

    def test_an_uncertain_reservation_keeps_it(self):
        self.reserve(uncertain=True)
        self.assertKept(self.reap(), "uncertain")

    def test_a_live_owned_process_keeps_it_though_the_holder_is_gone(self):
        self.process(HOLDER, 900, "python3 tool")
        self.reserve(holderProcess={"pid": 4, "start": "x"},
                     ownedProcesses=[{"pid": HOLDER, "start": self.table[HOLDER][2]}])
        self.assertKept(self.reap(), f"PID {HOLDER} still runs")

    def test_a_dead_reservation_not_yet_due_for_release_keeps_it(self):
        self.reserve(holderProcess={"pid": 4242, "start": ago(3600)}, expectedReleaseBy="2999-01-01T00:00:00Z")
        self.assertKept(self.reap(), "not due for release")

    def test_a_dead_reservation_past_its_release_does_not_count(self):
        self.reserve(holderProcess={"pid": 4242, "start": ago(3600)}, expectedReleaseBy="2020-01-01T00:00:00Z")
        self.assertStopped(self.reap())

    def test_a_dead_reservation_with_an_unreadable_release_keeps_it(self):
        self.reserve(holderProcess={"pid": 4242, "start": ago(3600)}, expectedReleaseBy="soon")
        self.assertKept(self.reap(), "cannot be read")

    def test_a_dead_reservation_does_not_count_and_its_entry_stays(self):
        self.reserve(holderProcess={"pid": 4242, "start": ago(3600)})
        before = self.record.read_text()
        report = self.reap()
        self.assertStopped(report)
        self.assertIn("recorded process is gone", report["stopped"][0]["reason"])
        self.assertEqual(self.record.read_text(), before)

    def test_an_orphaned_active_run_does_not_count(self):
        self.reserve(activeRun=True, hostProcessIdentifier=4242, hostProcessStart=ago(3600))
        self.assertStopped(self.reap())

    def test_a_recycled_pid_does_not_count(self):
        self.process(HOLDER, 900, "someone else's process", age=60)
        self.reserve(holderProcess={"pid": HOLDER, "start": ago(3600)})
        self.assertStopped(self.reap())

    def test_a_released_entry_does_not_count(self):
        self.reserve(state="released")
        self.assertStopped(self.reap())

    # Condition 4: its age.

    def test_a_younger_simulator_is_kept_and_one_just_past_the_limit_is_not(self):
        self.simulator(age=offload_host.REAP_AFTER - 60)
        self.assertKept(self.reap(), "under 2 h")
        self.simulator(age=offload_host.REAP_AFTER + 60)
        self.assertStopped(self.reap())

    def test_only_a_launchd_sim_is_stopped_and_other_leftovers_stay_report_only(self):
        devices_directory = self.home / offload_host.DEVICES
        self.table.clear()
        self.process(LAUNCHD, 1, f"/Applications/Xcode.app/Contents/DTServiceHub {devices_directory}/{UDID}/x")
        report = self.reap()
        self.assertEqual((self.stops, report["kept"], report["problem"]), ([], [], None), report)

    # The second check.

    def taken(self):
        self.process(HOLDER, 900, "python3 eng simulator-pool take")
        self.reserve(holderProcess={"pid": HOLDER, "start": self.table[HOLDER][2]})

    def test_a_device_taken_between_the_two_checks_is_not_stopped(self):
        self.between = self.taken
        report = self.reap()
        self.assertEqual(self.looks, 2)
        self.assertKept(report, "second check.*is live")

    def test_a_simulator_gone_or_rebooted_between_the_checks_is_not_stopped(self):
        self.between = lambda: self.table.clear() or self.process(1, 0, "launchd")
        self.assertKept(self.reap(), "no longer lists it")
        self.looks, self.table = 0, {}
        self.simulator()
        self.between = lambda: self.simulator(age=3 * 3600 + 500)
        self.assertKept(self.reap(), "another launchd_sim runs")

    def test_the_second_check_holds_the_record_lock_a_reservation_takes(self):
        def locked():
            descriptor = os.open(self.record.with_name(self.record.name + ".lock"), os.O_RDWR)
            self.addCleanup(os.close, descriptor)
            with self.assertRaises(BlockingIOError): fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.between = locked
        self.assertStopped(self.reap())
        # Once the stop has started, the lock is free again for the next reservation.
        record = devices.Record(self.record)
        self.assertEqual(record.reserve(NAME, owner="next", revision=0, purpose="tests", device=UDID,
                                        release_by="2999-01-01T00:00:00+00:00")["ownerChatID"], "next")

    def test_a_lock_another_change_holds_stops_nothing_and_says_so(self):
        lock = self.record.with_name(self.record.name + ".lock")
        descriptor = os.open(lock, os.O_RDWR | os.O_CREAT)
        self.addCleanup(os.close, descriptor)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        with patch.object(offload_host, "REAP_LOCK_SECONDS", 0.05):
            report = self.reap()
        self.assertEqual(self.stops, [])
        self.assertIn("stayed locked", report["problem"])

    # Failures.

    def test_a_failed_shutdown_is_reported_logged_and_tried_once(self):
        self.outcome = Process(status=149, error="Unable to shutdown")
        report = self.reap()
        self.assertEqual(self.stops, [UDID])
        self.assertEqual(report["stopped"], [])
        self.assertIn("exited 149: Unable to shutdown", report["failed"][0]["error"])
        self.assertIn("failed to stop", remote.reap_lines("fake", report, listing=False)[0])
        self.assertEqual([json.loads(line)["event"] for line in self.log.read_text().splitlines()], ["stopping", "failed"])

    def test_a_shutdown_past_its_time_ends_only_xcrun_and_says_its_state_is_unknown(self):
        self.outcome = Process(hangs=True)
        report = self.reap()
        self.assertTrue(self.outcome.killed)
        self.assertIn("state is unknown", report["failed"][0]["error"])

    def test_what_cannot_be_read_stops_nothing_and_says_so(self):
        cases = {"a device record that is no JSON": lambda: self.record.write_text("{"),
                 "a record of another schema": lambda: self.record.write_text('{"devices": {}}'),
                 "an entry that is no object": lambda: self.record.write_text(
                     json.dumps({"vocaroDeviceHandoff": 1, "devices": {"x": 3}})),
                 "a guard that is no JSON": lambda: self.guard.write_text("{"),
                 "simctl": lambda: setattr(self, "simctl_fails", True)}
        for name, breaking in cases.items():
            with self.subTest(name):
                breaking()
                report = self.reap()
                self.assertEqual((self.stops, report["stopped"]), ([], []), report)
                self.assertTrue(report["problem"], report)
                self.assertFalse(self.log.exists())
                self.record.unlink(missing_ok=True)
                self.guard.unlink(missing_ok=True)
                self.simctl_fails = False

    def test_a_census_that_cannot_list_its_processes_or_their_directories_stops_nothing(self):
        for missing in ("ps", "lsof"):
            with self.subTest(missing):
                report = offload_host.reap(remote.ROOT, ["Pagepour"], scan=False, begin=self.begin,
                                           listed=lambda command: None if command[0] == missing else self.listed(command))
                self.assertEqual(self.stops, [])
                self.assertTrue(report["problem"], report)

    def test_a_log_that_cannot_be_written_stops_nothing(self):
        self.log.mkdir()
        report = self.reap()
        self.assertEqual(self.stops, [])
        self.assertTrue(report["problem"], report)

    def test_an_unexpected_error_is_reported_and_never_raised(self):
        with patch.object(offload_host, "leftovers", side_effect=OSError("broken")):
            self.assertEqual(self.reap()["problem"], "OSError: broken")

    def test_time_used_up_leaves_the_rest_for_later(self):
        self.names[OTHER] = "Pagepour Duo 2"
        self.simulator(OTHER, 7100)
        moments = iter([0, *[offload_host.REAP_SECONDS - offload_host.REAP_STOP_SECONDS + 1] * 20])
        report = self.reap(clock=lambda: next(moments))
        self.assertEqual(self.stops, [])
        self.assertEqual(len(report["skipped"]), 2)
        self.assertIn("were used up", report["skipped"][0]["hold"])

    def test_reading_is_bounded_by_what_the_reap_has_left(self):
        with patch.object(offload_host, "listing", return_value=None) as listing:
            report = offload_host.reap(remote.ROOT, ["Pagepour"], scan=False, begin=self.begin)
        self.assertLessEqual(listing.call_args[0][1], 60)
        self.assertIn("ps listed no processes", report["problem"])

    # The census's connection.

    def test_a_census_request_reaps_first_and_a_failure_leaves_the_census(self):
        with patch.object(offload_host, "reap", return_value={"problem": "broken"}) as reap, \
                patch.object(offload_host, "leftovers", return_value={"leftovers": []}):
            found = offload_host.answer({"leftovers": remote.ROOT, "reap": {"pools": ["Pagepour"]}})
        reap.assert_called_once_with(remote.ROOT, pools=["Pagepour"])
        self.assertEqual(found, {"leftovers": [], "reap": {"problem": "broken"}})
        with patch.object(offload_host, "reap") as reap, patch.object(offload_host, "leftovers", return_value={"leftovers": []}):
            self.assertEqual(offload_host.answer({"leftovers": remote.ROOT}), {"leftovers": []})
        reap.assert_not_called()

    def test_the_second_mac_failing_or_not_answering_is_a_problem_in_the_report(self):
        cases = [(subprocess.CompletedProcess([], 255, "", "no route to host"), "exit 255: no route to host"),
                 (subprocess.TimeoutExpired("ssh", 1), "did not answer"), (OSError("no ssh"), "no ssh")]
        for outcome, said in cases:
            with self.subTest(said), patch.object(remote, "ssh", side_effect=[outcome]):
                report = remote.reap_there("fake", {"pools": []})
                self.assertIn(said, report["problem"])
                self.assertEqual(report["stopped"], [])

    def test_the_second_macs_report_comes_back(self):
        answer = subprocess.CompletedProcess([], 0, json.dumps({"leftovers": [], "reap": {"stopped": [], "problem": None}}) + "\n", "")
        with patch.object(remote, "ssh", return_value=answer) as ssh:
            self.assertEqual(remote.reap_there("fake", {"pools": ["Pagepour"], "dry_run": True}),
                             {"stopped": [], "problem": None})
        self.assertIn('"dry_run": true', ssh.call_args[0][1])


class ReapCommandTests(unittest.TestCase):
    """The command's options, over offload_host.reap and remote.reap_there."""
    EMPTY = {"stopped": [], "failed": [], "skipped": [], "eligible": [], "kept": [], "problem": None}

    def run_command(self, *arguments, here=None, there=None, host="fake"):
        with tempfile.TemporaryDirectory() as project:
            Path(project, "engineering.json").write_text(json.dumps({"simulatorPool": "Example"}))
            output = StringIO()
            with patch.object(offload_host, "reap", return_value=here or self.EMPTY) as local, \
                    patch.object(remote, "reap_there", return_value=there or self.EMPTY) as far, \
                    patch.object(remote, "configured", return_value=host), \
                    patch("vocaro_engineering.disk.pools", return_value=frozenset({"Pagepour"})), redirect_stdout(output):
                status = simulator_pool.main(["reap", *arguments], project)
        return status, output.getvalue(), local, far

    def test_with_no_options_it_acts_on_both_macs_for_every_pool(self):
        status, output, local, far = self.run_command()
        request = {"pools": ["Example", "Pagepour"], "dry_run": False, "scan": True}
        local.assert_called_once_with(remote.ROOT, **request)
        far.assert_called_once_with("fake", request)
        self.assertEqual(status, 0)
        self.assertIn("on this Mac, no stale pool simulator", output)
        self.assertIn("on fake, no stale pool simulator", output)

    def test_its_options_narrow_it(self):
        _, _, local, far = self.run_command("--dry-run", "--here")
        self.assertTrue(local.call_args.kwargs["dry_run"])
        far.assert_not_called()
        status, output, local, far = self.run_command(host=None)
        far.assert_not_called()
        self.assertEqual(status, 0)
        with tempfile.TemporaryDirectory() as project, patch.object(offload_host, "reap", return_value=self.EMPTY) as local, \
                patch.object(remote, "configured", return_value=None), redirect_stdout(StringIO()):
            simulator_pool.main(["--pool", "Solo", "reap"], project)
        local.assert_called_once_with(remote.ROOT, pools=["Solo"], dry_run=False, scan=False)

    def test_a_problem_on_either_mac_exits_1(self):
        for side in ("here", "there"):
            for failing in ({"problem": "broken"}, {"failed": [{"name": "n", "udid": "u", "pid": 1, "error": "e"}]}):
                with self.subTest(side=side, failing=failing):
                    status, output, _, _ = self.run_command(**{side: {**self.EMPTY, **failing}})
                    self.assertEqual(status, 1)

    def test_a_dispatch_asks_for_the_projects_pool(self):
        with tempfile.TemporaryDirectory() as project:
            self.assertEqual(remote.reap_request(project), {"pools": []})
            Path(project, "engineering.json").write_text(json.dumps({"simulatorPool": "Example"}))
            self.assertEqual(remote.reap_request(project), {"pools": ["Example"]})
        self.assertIn('"reap": {"pools": ["Example"]}', remote.census_command(reap={"pools": ["Example"]}))


if __name__ == "__main__":
    unittest.main()
