"""`eng simulator-pool reap` (offload_host.reap) over fake ps, lsof and simctl listings of a Mac whose home
is a temporary directory, with its own device record, admission guard and log. No test runs simctl."""
from contextlib import redirect_stdout
import fcntl
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/python"))
from vocaro_engineering import devices, offload_host, remote
import host_isolation
from vocaro_engineering.simulators import pool as simulator_pool

UDID = "AAAAAAAA-BBBB-4CCC-8DDD-EEEEEEEEEEEE"
OTHER = "11111111-2222-4333-8444-555555555555"
NAME = "Pagepour Duo 1"
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
        self.table, self.directories, self.names = {}, {}, {UDID: NAME}
        self.states, self.looks, self.between, self.stops, self.outcome = {}, 0, None, [], Process()
        self.simctl_fails = False
        self.simulator()

    def simulator(self, udid=UDID, pid=LAUNCHD, age=3 * 3600):
        devices_directory = self.home / offload_host.DEVICES
        self.process(pid, 1, f"launchd_sim {devices_directory}/{udid}/data/var/run/launchd_bootstrap.plist", age)
        self.process(pid + 1, pid, f"{devices_directory}/{udid}/data/Containers/Bundle/Application/X/Example.app/Example", age)

    def process(self, pid, parent, args, age=3 * 3600, start=None):
        self.table[pid] = (parent, self.uid, start or ago(age), args)

    def listed(self, command):
        if command[0] == "ps":
            self.looks += 1
            if self.looks == 2 and self.between: self.between()
            return "".join(f"{pid:>6} {parent:>6} {uid:>5} {start} {args}\n"
                           for pid, (parent, uid, start, args) in self.table.items())
        if command[0] == "lsof":
            return f"p{os.getpid()}\nfcwd\nn{self.home}\n"
        if command[:3] == ["xcrun", "simctl", "list"]:
            if self.simctl_fails: return ""
            return json.dumps({"devices": {"iOS": [
                {"udid": udid, "name": name, "state": self.states.get(udid, "Booted")}
                for udid, name in self.names.items()]}})
        self.fail(f"an unexpected command: {command}")

    def begin(self, udid):
        self.stops.append(udid)
        return self.outcome

    def reap(self, **options):
        options.setdefault("scan", False)
        return offload_host.reap(remote.ROOT, ["Pagepour"], listed=self.listed, begin=self.begin, **options)

    def reserve(self, **entry):
        self.record.write_text(json.dumps({"vocaroDeviceHandoff": 1, "revision": 1, "devices": {NAME: {
            "revision": 1, "device": UDID, "ownerChatID": "session", "state": "reserved", "activeRun": False,
            "purpose": "tests", **entry}}}))

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

    def assertNamed(self, report, key=NAME):
        self.assertEqual([item["key"] for item in report["recoverable"]], [key], report)
        line = next(line for line in remote.reap_lines("fake", report) if "stays held" in line)
        self.assertIn(f"owner session", line)
        self.assertIn(f"due {self.DUE}", line)
        self.assertIn(f"python3 -m vocaro_engineering.devices recover '{key}' --by SESSION --revision 7 "
                      "--verified 'WHAT YOU CHECKED'", line)

    def test_reap_names_a_held_entry_whose_process_is_gone_and_whose_release_has_passed_and_leaves_it(self):
        self.reserve(**self.held_entry())
        before = self.record.read_bytes()
        report = self.reap()
        self.assertStopped(report)
        self.assertNamed(report)
        self.assertEqual(self.record.read_bytes(), before, "reap releases nothing")
        # It stopped this entry's simulator, so a dispatch's reap says so too.
        self.assertIn("stays held", "\n".join(remote.reap_lines("fake", report, listing=False)))

    def test_reap_names_one_with_nothing_booted_but_a_dispatch_says_only_what_it_stopped(self):
        self.table.clear()
        self.process(5000, 1, "/usr/bin/other")
        self.reserve(**self.held_entry())
        report = self.reap()
        self.assertEqual(self.stops, [])
        self.assertNamed(report)
        self.assertEqual(remote.reap_lines("fake", report, listing=False), [])

    def test_a_dry_run_names_it_too(self):
        self.reserve(**self.held_entry())
        self.assertNamed(self.reap(dry_run=True))

    def test_a_reused_pid_with_another_start_is_a_process_that_has_gone(self):
        self.process(HOLDER, 1, "/usr/bin/other", start="Mon Feb  2 00:00:00 2026")
        self.reserve(**self.held_entry())
        self.assertNamed(self.reap())

    def test_a_live_holder_is_not_named(self):
        self.process(HOLDER, 1, "/usr/bin/holder", start="Thu Jan  1 00:00:00 2026")
        self.reserve(**self.held_entry())
        report = self.reap()
        self.assertEqual(report["recoverable"], [])
        self.assertNotIn("stays held", "\n".join(remote.reap_lines("fake", report)))

    def test_what_may_still_be_held_is_not_named(self):
        future = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() + 3600))
        for name, entry in (("release not due", self.held_entry(due=future)),
                            ("no release", self.held_entry(due=None)),
                            ("unreadable release", self.held_entry(due="soon")),
                            ("no process", {"expectedReleaseBy": self.DUE, "revision": 7}),
                            ("no start for a running PID", self.held_entry(pid=LAUNCHD, start=None)),
                            ("uncertain", self.held_entry(uncertain=True)),
                            ("released", self.held_entry(state="released", ownerChatID=None))):
            with self.subTest(name):
                self.reserve(**entry)
                self.assertEqual(self.reap(dry_run=True)["recoverable"], [])

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
            self.assertIn("left over", line["reason"])
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
                self.assertKept(self.reap(), "no member of a simulator pool")

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
