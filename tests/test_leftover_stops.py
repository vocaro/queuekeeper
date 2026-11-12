"""offload_host.stop_leftovers and the Launches record behind it (vocaro-engineering#353), over fake ps and lsof
listings of a second Mac whose home is a temporary directory, with its own admission guard and log. No test signals
a process: every stop goes to a fake kill, and every fake PID lies above macOS's highest, 99998."""
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/python"))
from queuekeeper import offload_host, remote
from queuekeeper.apple.simulators import pool as simulator_pool
import host_isolation

RUN, LEFTOVER, OTHER_RUN, SHELL, CHILD = 105000, 108000, 105500, 109000, 108100


def ago(seconds):
    return " ".join(time.strftime(offload_host.STARTED, time.localtime(time.time() - seconds)).split())


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name).resolve()
        self.home = base / "home"
        self.checkouts = self.home / remote.ROOT / "checkouts/Example"
        self.slot = self.checkouts / "1"
        (self.slot / "Example").mkdir(parents=True)
        self.slots = base / "slots"
        self.slots.mkdir()
        self.environment = patch.dict(os.environ, {
            "HOME": str(self.home), "TMPDIR": "/var/folders/zz/fake/T/", "HEAVY_SLOT_DIR": str(self.slots),
            "SW_GATE_LOCK_FILE": str(base / "gate"), "VOCARO_DEVICE_RECORD": str(base / "device-handoff.json")})
        self.environment.start()
        self.uid = os.getuid()
        self.table, self.directories, self.killed = {}, {}, []
        # The Preview extension a UI test of the slot's app launched, which launchd adopted (parent 1).
        self.appex = (f"{self.slot}/Example/.build/Products/Debug/Example.app/Contents/PlugIns/Preview.appex/"
                      "Contents/MacOS/Preview")
        self.run_start, self.claimed, self.started, self.seen = ago(7300), time.time() - 7200, ago(7000), time.time() - 6990
        # ps lists something besides the leftover; a listing of nothing says nothing of whether it ended.
        self.process(SHELL, 1, "/bin/zsh -l", start=ago(8000))
        self.process(LEFTOVER, 1, self.appex, start=self.started)
        self.record(LEFTOVER, self.started, self.appex)
        self.lock(RUN, self.run_start)

    def tearDown(self):
        self.environment.stop()
        self.temp.cleanup()

    def process(self, pid, parent, args, cwd="/", start=None, uid=None):
        self.table[pid] = (parent, self.uid if uid is None else uid, start or ago(60), args)
        self.directories[pid] = cwd

    def listed(self, command):
        if command[0] == "ps":
            return "".join(f"{pid:>6} {parent:>6} {uid:>5} {start} {args}\n"
                           for pid, (parent, uid, start, args) in self.table.items())
        if command[0] == "lsof":
            return "".join(f"p{pid}\nfcwd\nn{cwd}\n" for pid, cwd in [(os.getpid(), self.home), *self.directories.items()])
        return None

    def record(self, pid, start, args, run=None, claimed=None, seen=None, write="a"):
        line = {"pid": pid, "start": start, "args": args, "run": run or {"pid": RUN, "start": self.run_start},
                "claimed": self.claimed if claimed is None else claimed, "seen": self.seen if seen is None else seen}
        with open(self.checkouts / f"1.{offload_host.LAUNCHED}", write) as stream: stream.write(json.dumps(line) + "\n")

    def lock(self, pid, start):
        (self.checkouts / "1.lock").write_text(json.dumps({"pid": pid, "start": start}) + "\n")

    def kill(self, pid, number):
        self.killed.append((pid, number))
        self.table.pop(pid, None)

    def stop(self, **options):
        return offload_host.stop_leftovers(remote.ROOT, listed=self.listed, kill=self.kill, sleep=lambda _: None,
                                           **options)

    def stops(self):
        try: lines = (self.slots / offload_host.STOP_LOG).read_text().splitlines()
        except FileNotFoundError: return []
        return [json.loads(line) for line in lines]

    def assertKept(self, report, why):
        self.assertEqual(self.killed, [])
        self.assertEqual(report["stopped"], [])
        self.assertEqual(self.stops(), [])
        self.assertIsNone(report["problem"])
        self.assertEqual([item["pid"] for item in report["kept"]], [LEFTOVER])
        self.assertRegex(report["kept"][0]["hold"], why)
        # A kept leftover is said with the evidence that is missing.
        said = "\n".join(remote.reap_lines("build-host", {"processes": report}, listing=False))
        self.assertIn(f"on build-host, did not stop PID {LEFTOVER}", said)


class StopTests(Fixture):
    def test_a_proven_orphan_of_an_ended_run_is_stopped_and_logged(self):
        report = self.stop()
        self.assertEqual(self.killed, [(LEFTOVER, signal.SIGTERM)])
        self.assertEqual([item["pid"] for item in report["stopped"]], [LEFTOVER])
        self.assertEqual((report["kept"], report["failed"], report["problem"]), ([], [], None))
        item = report["stopped"][0]
        self.assertEqual(item["executable"], self.appex)
        self.assertEqual(item["run"], {"pid": RUN, "start": self.run_start})
        self.assertIn(f"the run PID {RUN}", item["reason"])
        # Logged before the signal and once it ended, with why it qualified.
        self.assertEqual([line["event"] for line in self.stops()], ["stopping", "stopped"])
        self.assertTrue(all(line["pid"] == LEFTOVER and line["reason"] == item["reason"] for line in self.stops()))
        said = "\n".join(remote.reap_lines("build-host", {"processes": report}, listing=False))
        self.assertIn(f"on build-host, stopped PID {LEFTOVER}, started {self.started} ({self.appex})", said)
        self.assertIn(offload_host.STOP_LOG, said)

    def test_a_dry_run_names_what_it_would_stop_and_signals_nothing(self):
        report = self.stop(dry_run=True)
        self.assertEqual(self.killed, [])
        self.assertEqual([item["pid"] for item in report["eligible"]], [LEFTOVER])
        self.assertIn(f"would stop PID {LEFTOVER}", "\n".join(remote.reap_lines("build-host", {"processes": report})))

    def test_a_live_recording_run_keeps_it(self):
        self.process(RUN, 1, "python3 -c offload_host", start=self.run_start)
        self.assertKept(self.stop(), f"the run that saw it start, PID {RUN}, still runs")

    def test_a_live_run_holding_its_slot_keeps_it(self):
        self.process(OTHER_RUN, 1, "python3 -c offload_host", start=ago(30))
        self.lock(OTHER_RUN, ago(30))
        self.assertKept(self.stop(), f"a live run, PID {OTHER_RUN}, holds its checkout slot")

    def test_a_slot_lock_without_a_readable_holder_keeps_it(self):
        # A claim truncates its lock before it writes its holder.
        (self.checkouts / "1.lock").write_text("")
        self.assertKept(self.stop(), "names no holder that can be read")

    def test_a_live_admission_accounts_for_it_and_nothing_is_signaled(self):
        # A live admission whose owner works in the slot, admitted before the extension started, holds it.
        owner = 104000
        self.process(owner, 1, "python3 bin/eng run ui-tests", cwd=str(self.slot / "Example"), start=ago(7100))
        Path(os.environ["SW_GATE_LOCK_FILE"] + ".warm-push-guard").write_text(json.dumps({
            "vocaroEngineering": 1, "owners": {"a" * 32: {"pid": owner, "start": ago(7100), "label": "ui-tests",
                                                          "exclusive": False}}}))
        report = self.stop()
        self.assertEqual((self.killed, report["stopped"], report["kept"], report["problem"]), ([], [], [], None))

    def test_a_process_no_run_recorded_is_kept_as_started_by_hand(self):
        # The same extension, launched by hand (a Finder preview) after the run ended: no run saw it start.
        (self.checkouts / f"1.{offload_host.LAUNCHED}").unlink()
        self.assertKept(self.stop(), "no run's record names this process")
        self.record(LEFTOVER + 1, self.started, self.appex)
        self.assertKept(self.stop(), "no run's record names this process")

    def test_a_reused_pid_is_not_the_recorded_process(self):
        # The run recorded PID LEFTOVER at another start; the PID now runs the same executable started later.
        self.table[LEFTOVER] = (1, self.uid, ago(100), self.appex)
        self.assertKept(self.stop(), "no run's record names this process with its start")

    def test_a_reused_process_group_ties_nothing_to_the_run(self):
        # As in vocaro-engineering#240: after the run ended, a launcher took a recorded process's PID, led a
        # group of that ID, and exited, leaving its child, which the owner opened, in the slot. Neither the
        # child nor anything sharing that ID is recorded, so the shared ID proves nothing.
        del self.table[LEFTOVER]
        self.process(LEFTOVER + 100, 1, self.appex, start=ago(50))
        report = self.stop()
        self.assertEqual(self.killed, [])
        self.assertEqual([item["pid"] for item in report["kept"]], [LEFTOVER + 100])
        self.assertIn("no run's record names this process", report["kept"][0]["hold"])

    def test_a_record_naming_another_command_or_run_keeps_it(self):
        self.record(LEFTOVER, self.started, self.appex + " --other", write="w")
        self.assertKept(self.stop(), "more than one run, or with another command line")
        self.record(LEFTOVER, self.started, self.appex, write="w")
        self.record(LEFTOVER, self.started, self.appex, run={"pid": OTHER_RUN, "start": ago(9000)})
        self.assertKept(self.stop(), "more than one run")

    def test_a_start_outside_the_runs_claim_keeps_it(self):
        self.record(LEFTOVER, self.started, self.appex, claimed=time.time() - 60, seen=time.time() - 30, write="w")
        self.assertKept(self.stop(), "does not lie between its run's claim")

    def test_a_record_that_cannot_be_read_keeps_it(self):
        path = self.checkouts / f"1.{offload_host.LAUNCHED}"
        path.unlink()
        path.mkdir()
        self.assertKept(self.stop(), "cannot be read")

    def test_a_parent_other_than_launchd_keeps_it(self):
        self.process(SHELL, 1, "/bin/zsh -l", start=ago(8000))
        self.table[LEFTOVER] = (SHELL, self.uid, self.started, self.appex)
        self.assertKept(self.stop(), f"its parent is PID {SHELL}, not launchd")

    def test_descendants_keep_it(self):
        self.process(CHILD, LEFTOVER, "/usr/bin/helper", start=ago(6000))
        self.assertKept(self.stop(), "it has 1 descendants")

    def test_an_executable_outside_the_slot_keeps_it(self):
        # It works in the slot, but runs a program from elsewhere.
        self.table[LEFTOVER] = (1, self.uid, self.started, "/usr/bin/python3 -m http.server")
        self.directories[LEFTOVER] = str(self.slot / "Example")
        self.assertKept(self.stop(), "does not start with a bundle executable in the checkout slot")

    def test_an_uncertain_census_stops_nothing(self):
        listed = lambda command: None if command[0] == "lsof" else self.listed(command)
        report = offload_host.stop_leftovers(remote.ROOT, listed=listed, kill=self.kill)
        self.assertEqual((self.killed, report["stopped"]), ([], []))
        self.assertIn("the census could not check the processes' working directories", report["problem"])
        self.assertIn("stopped no leftover process", "\n".join(remote.reap_lines("build-host", {"processes": report})))

    def test_a_change_before_the_second_check_keeps_it(self):
        proofs, calls = offload_host.proofs, []

        def changing(*arguments):
            calls.append(1)
            # Between the checks the process ended and another took its PID.
            if len(calls) == 2: self.table[LEFTOVER] = (1, self.uid, ago(5), self.appex)
            return proofs(*arguments)

        with patch.object(offload_host, "proofs", side_effect=changing):
            report = self.stop()
        self.assertEqual(self.killed, [])
        self.assertEqual(len(calls), 2)
        self.assertIn("it changed before the second check passed", report["kept"][0]["hold"])

    def test_a_log_that_cannot_be_written_stops_nothing(self):
        with patch.object(offload_host, "log_stop", side_effect=OSError("read-only")):
            report = self.stop()
        self.assertEqual(self.killed, [])
        self.assertIn("could not be written, so it was not stopped", report["failed"][0]["error"])

    def test_one_that_outlives_sigterm_is_reported_and_sent_nothing_more(self):
        ticks = iter(range(0, 1000))
        # This kill leaves the process running.
        report = offload_host.stop_leftovers(remote.ROOT, listed=self.listed,
                                             kill=lambda pid, number: self.killed.append((pid, number)),
                                             clock=lambda: next(ticks), sleep=lambda _: None)
        self.assertEqual(self.killed, [(LEFTOVER, signal.SIGTERM)])
        self.assertIn("still ran", report["failed"][0]["error"])
        self.assertEqual([line["event"] for line in self.stops()], ["stopping", "failed"])
        said = "\n".join(remote.reap_lines("build-host", {"processes": report}, listing=False))
        self.assertIn(f"failed to stop PID {LEFTOVER}", said)


class RequestTests(Fixture):
    """The second Mac's answer to the requests `eng doctor` and `eng simulator-pool reap` send there
    (simulators.pool.reap_reports), over the same fixture: the sent command is answered here with fakes."""

    def reap_reports(self, **options):
        real_stop, real_census, sent = offload_host.stop_leftovers, offload_host.leftovers, []

        def answered(host, command, **_):
            request = json.loads(shlex.split(command)[-1])
            sent.append(request)
            return subprocess.CompletedProcess([], 0, json.dumps(offload_host.answer(request)) + "\n", "")

        with patch.object(remote, "configured", return_value="build-host"), patch.object(remote, "ssh", answered), \
                patch.object(offload_host, "reap", return_value={"stopped": [], "problem": None}), \
                patch.object(offload_host, "leftovers", side_effect=lambda root, listed=None, written=None:
                             real_census(root, self.listed, written or (lambda pid: None))), \
                patch.object(offload_host, "stop_leftovers", side_effect=lambda root, dry_run=False: real_stop(
                    root, dry_run=dry_run, listed=self.listed, kill=self.kill, sleep=lambda _: None)):
            reports = simulator_pool.reap_reports("Example", None, dry_run=False, here=False, **options)
        return sent, dict(reports)["build-host"]["processes"]

    def test_doctor_lists_what_reap_would_stop_and_signals_nothing(self):
        # As cli.fix_hosts asks.
        sent, report = self.reap_reports(stop_leftovers=False)
        self.assertEqual(sent[0]["leftoverStops"], "list")
        self.assertEqual((self.killed, report["stopped"], self.stops()), ([], [], []))
        self.assertEqual([item["pid"] for item in report["eligible"]], [LEFTOVER])
        said = "\n".join(remote.reap_lines("build-host", {"processes": report}, listing=False, held=True))
        self.assertIn(f"on build-host, would stop PID {LEFTOVER}", said)
        # The same fixture under reap is stopped.
        sent, report = self.reap_reports()
        self.assertNotIn("leftoverStops", sent[0])
        self.assertEqual(self.killed, [(LEFTOVER, signal.SIGTERM)])
        self.assertEqual([item["pid"] for item in report["stopped"]], [LEFTOVER])


class LaunchesTests(Fixture):
    def launches(self):
        with patch.object(offload_host, "output", return_value=self.run_start), \
                patch.object(offload_host.os, "getpid", return_value=RUN):
            return offload_host.Launches(self.checkouts, 1, listed=self.listed, clock=lambda: self.claimed)

    def lines(self):
        return [json.loads(line) for line in (self.checkouts / f"1.{offload_host.LAUNCHED}").read_text().splitlines()]

    def test_a_claim_keeps_only_records_of_processes_that_still_run(self):
        self.record(LEFTOVER + 1, ago(9000), self.appex)
        self.launches()
        self.assertEqual([line["pid"] for line in self.lines()], [LEFTOVER])

    def test_it_records_what_launchd_starts_from_its_slot_after_the_claim_and_nothing_else(self):
        (self.checkouts / f"1.{offload_host.LAUNCHED}").unlink()
        del self.table[LEFTOVER]
        launches = self.launches()
        other = str(self.checkouts / "2") + self.appex[len(str(self.slot)):]
        self.process(LEFTOVER, 1, self.appex, start=ago(7100))
        self.process(LEFTOVER + 1, 1, other, start=ago(7100))                       # another slot's
        self.process(LEFTOVER + 2, 1, self.appex, start=ago(7300))                  # before the claim
        self.process(LEFTOVER + 3, 1, self.appex, start=ago(7100), uid=self.uid + 1)  # another account's
        self.process(LEFTOVER + 4, RUN, self.appex, start=ago(7100))                # the run's own child
        self.process(LEFTOVER + 5, 1, f"/usr/bin/true {self.slot}", start=ago(7100))  # no bundle executable
        launches.sample()
        launches.sample()
        [line] = self.lines()
        self.assertEqual((line["pid"], line["start"], line["args"]), (LEFTOVER, ago(7100), self.appex))
        self.assertEqual(line["run"], {"pid": RUN, "start": self.run_start})
        self.assertEqual(line["claimed"], self.claimed)

    def test_a_sample_that_fails_records_nothing_more_and_never_raises(self):
        launches = self.launches()
        launches.listed = lambda command: (_ for _ in ()).throw(OSError("ps broke"))
        with patch.object(offload_host, "say") as said:
            launches.sample()
            launches.sample()
        self.assertTrue(launches.broken)
        said.assert_called_once()


if __name__ == "__main__":
    unittest.main()
