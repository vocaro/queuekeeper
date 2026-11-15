"""The warnings that keep the split between the landing and the release gate (vocaro-engineering#135): the gate
over its time budget, main past its release commit limit, and qk doctor's gate without a budget or with a device."""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src/python"))
from queuekeeper import budgets, cli, config as configuration, merge_queue as mq
import host_isolation  # noqa: F401
from test_support.queue import Fixture, run

GATE = {"command": ["true"], "timeout": 60}


def landed(number, seconds, parts=(), landed_as=None, wait=0.0, **busy):
    """A landed entry whose gate's own work, from its admission to its end, took seconds after wait seconds waiting
    for admission (vocaro/queuekeeper#112)."""
    return {"id": f"entry-{number}", "state": "landed", "settled": float(number), "landedAs": landed_as or f"c{number}",
            "gate": {"seconds": seconds + wait, "workSeconds": seconds, "waitSeconds": wait,
                     "parts": [list(part) for part in parts], **busy}}


def recorded_before(entry):
    """entry as a record made before 2026-11-13 keeps it: its wall time alone, the wait for admission included."""
    gate = entry["gate"]
    return {**entry, "gate": {key: value for key, value in gate.items() if key not in ("workSeconds", "waitSeconds")}}


def loaded(number, seconds, busy=90.0, limit=75.0):
    """A gate run whose Mac's CPU was busy share busy against its limit."""
    return landed(number, seconds, cpuBusy=busy, busyLimit=limit, busyOn="this Mac")


class Parts(unittest.TestCase):
    def test_the_parts_eng_unittest_eng_xctest_and_a_lane_group_report_slowest_first(self):
        log = "\n".join([
            "queue: 12:00:00 gate on abc",
            "== test_core.Tests.test_fast: OK (1 tests, 0.4s)",
            "ok",
            "== test_remote.Tests.test_slow: FAILED (failures=1) (2 tests, 312.5s)",
            "== EPUBLibTests.ParserTests: PASS (40 tests, 2 skipped, 75.0s)",
            "Lane times (wall-clock 900s, 2 at a time):",
            "  mac-tests     610s  PASS",
            "  ios-tests     880s  FAIL",
            "PASS",
            # Negative controls: an indented time outside the group's block, and lines that only look alike.
            "  stray     999s  PASS",
            "== no time here: OK (3 tests)",
            "Ran 3 tests in 1.2s"])
        self.assertEqual(budgets.parts(log), [["ios-tests", 880.0], ["mac-tests", 610.0],
                                              ["test_remote.Tests.test_slow", 312.5],
                                              ["EPUBLibTests.ParserTests", 75.0], ["test_core.Tests.test_fast", 0.4]])
        self.assertEqual(budgets.parts("bad: []\n"), [])


class GateBudget(unittest.TestCase):
    def config(self, **gate):
        return {"name": "Example", "lanes": {"gate": {**GATE, **gate}}}

    def test_over_budget_warns_with_the_median_and_the_slowest_parts(self):
        entries = [landed(1, 3000, [("old", 1)]), landed(2, 3300), landed(3, 4000, [("test_remote", 1200), ("test_queue", 900)])]
        [warning] = budgets.gate_warnings(self.config(budgetMinutes=50), entries)
        self.assertIn("median time from its admission to its end over its last 3 passing runs in the merge queue is "
                      "55.0 min, over its budget of 50 min (gate.budgetMinutes)", warning)
        # The newest run that reports its parts.
        self.assertIn("slowest parts in its latest run that reports them: test_remote 1200s, test_queue 900s", warning)
        self.assertNotIn("old", warning)
        self.assertIn("the toolkit's guides/releasing.md, Keeping the split", warning)

    def test_a_run_that_reports_no_parts_says_so(self):
        [warning] = budgets.gate_warnings(self.config(budgetMinutes=1), [landed(n, 120) for n in range(3)])
        self.assertIn("its log reports no parts with their times", warning)

    def test_under_or_at_budget_no_budget_and_too_few_runs_give_no_warning(self):
        three = [landed(n, 3000) for n in range(3)]
        self.assertEqual(budgets.gate_warnings(self.config(budgetMinutes=50), three), [])  # at the budget
        self.assertEqual(budgets.gate_warnings(self.config(budgetMinutes=60), three), [])
        self.assertEqual(budgets.gate_warnings(self.config(), three), [])
        self.assertEqual(budgets.gate_warnings(self.config(budgetMinutes=1), three[:2]), [])
        # One batch's entries share one run, and an entry that did not land, or recorded no gate, is no run.
        batch = [landed(n, 3000, landed_as="same") for n in range(3)]
        others = [{**landed(5, 9000), "state": "failed"}, {"id": "x", "state": "landed", "landedAs": "d", "settled": 6}]
        self.assertEqual(budgets.gate_warnings(self.config(budgetMinutes=1), batch + others), [])
        self.assertEqual(len(budgets.gate_warnings(self.config(budgetMinutes=1), batch + three)), 1)
        # Since #184 each entry of a batch records its own commit and the batch's tip beside it: the tip is the run.
        own = [{**landed(n, 3000, landed_as=f"own{n}"), "landedTip": "tip"} for n in range(3)]
        self.assertEqual(budgets.gate_warnings(self.config(budgetMinutes=1), own + others), [])
        apart = [{**entry, "landedTip": f"tip{n}"} for n, entry in enumerate(own)]
        self.assertEqual(len(budgets.gate_warnings(self.config(budgetMinutes=1), apart)), 1)

    def test_runs_on_a_loaded_host_are_left_out_of_the_median_and_counted(self):
        """The owner's decision on #235: the budget assumes an unloaded machine. Settled is newest last."""
        unloaded = [loaded(n, 540, busy=40.0) for n in range(3)]
        heavy = [loaded(n, 1740) for n in range(3, 7)]
        # Every run counted, the 29-minute runs made under load put the median over a 15-minute budget.
        self.assertEqual(len(budgets.gate_warnings(self.config(budgetMinutes=15),
                                                   [{**e, "gate": {"seconds": e["gate"]["seconds"],
                                                                   "workSeconds": e["gate"]["workSeconds"]}}
                                                    for e in unloaded + heavy])), 1)
        # Left out, the 9-minute runs are within it, and nothing warns.
        self.assertEqual(budgets.gate_warnings(self.config(budgetMinutes=15), unloaded + heavy), [])
        # Over budget on unloaded runs alone, the warning says how many it left out.
        [warning] = budgets.gate_warnings(self.config(budgetMinutes=8), unloaded + heavy)
        self.assertIn("over its last 3 passing runs in the merge queue is 9.0 min, leaving out 4 runs made with the CPU "
                      "of the Mac it ran on at or above its busy limit, as the budget assumes an unloaded host, over its "
                      "budget of 8 min", warning)
        # Negative controls: none left out says nothing of loads; too few unloaded runs judge nothing.
        [warning] = budgets.gate_warnings(self.config(budgetMinutes=8), unloaded)
        self.assertNotIn("leaving out", warning)
        self.assertEqual(budgets.gate_warnings(self.config(budgetMinutes=1), unloaded[:2] + heavy), [])

    def test_a_run_is_loaded_at_or_above_its_own_macs_limit_and_unknown_counts(self):
        self.assertTrue(budgets.loaded({"cpuBusy": 75.0, "busyLimit": 75.0}))
        self.assertTrue(budgets.loaded({"cpuBusy": 61, "busyLimit": 60}))
        for run in ({"cpuBusy": 74.9, "busyLimit": 75.0}, {"cpuBusy": 90.0, "busyLimit": 95.0}, {}, {"cpuBusy": None, "busyLimit": 75.0},
                    {"cpuBusy": 90.0}, {"cpuBusy": True, "busyLimit": 0.5}, {"seconds": 60, "parts": []}):
            with self.subTest(run=run):
                self.assertFalse(budgets.loaded(run))
        # The window is the latest unloaded runs: a loaded run older than them is not counted as left out.
        entries = [loaded(0, 600)] + [loaded(n, 600, busy=10.0) for n in range(1, 1 + budgets.RECENT_RUNS)]
        [warning] = budgets.gate_warnings(self.config(budgetMinutes=1), entries)
        self.assertIn(f"over its last {budgets.RECENT_RUNS} passing runs", warning)
        self.assertNotIn("leaving out", warning)

    def test_the_worker_log_says_the_busy_share_and_when_a_run_is_left_out(self):
        said = budgets.busy_words({"cpuBusy": 91.5, "cpuBusyHighest": 99.0, "busyLimit": 75.0, "busyOn": "this Mac",
                                   "busyReadings": 29})
        self.assertEqual(said, "the CPU of this Mac was 91.5% busy on average over 29 reading(s), at most 99%, against its "
                               "busy limit of 75%: a loaded run, which the gate's budget median leaves out")
        self.assertNotIn("loaded run", budgets.busy_words({"cpuBusy": 30.0, "busyLimit": 75.0}))

    def test_a_whole_host_gates_run_is_never_left_out_for_its_own_busy_share(self):
        """Admission admits no other heavy work beside a whole-host lane, so its busy share is its own work
        (vocaro/queuekeeper#12)."""
        self.assertFalse(budgets.loaded({"cpuBusy": 91.5, "busyLimit": 70.0, "wholeHost": True}))
        # Negative controls: the same readings of any other lane are a loaded run, and only true says whole-host.
        for other in ({}, {"wholeHost": False}, {"wholeHost": "yes"}):
            self.assertTrue(budgets.loaded({"cpuBusy": 91.5, "busyLimit": 70.0, **other}))
        whole = [{**entry, "gate": {**entry["gate"], "wholeHost": True}} for entry in (loaded(n, 1740) for n in range(3))]
        [warning] = budgets.gate_warnings(self.config(budgetMinutes=15), whole)
        self.assertIn("over its last 3 passing runs in the merge queue is 29.0 min, over its budget of 15 min", warning)
        self.assertNotIn("leaving out", warning)
        self.assertEqual(budgets.gate_warnings(self.config(budgetMinutes=15), [loaded(n, 1740) for n in range(3)]), [])
        said = budgets.busy_words({"cpuBusy": 91.5, "cpuBusyHighest": 99.0, "busyLimit": 70.0, "busyOn": "this Mac",
                                   "busyReadings": 14, "wholeHost": True})
        self.assertTrue(said.endswith("against its busy limit of 70%: its own work, as a whole-host lane has the Mac's "
                                      "heavy work to itself, so the gate's budget median counts it"), said)
        self.assertNotIn("its own work", budgets.busy_words({"cpuBusy": 30.0, "busyLimit": 70.0, "wholeHost": True}))
        self.assertEqual(budgets.busy_words({}), "the CPU busy share of the Mac it ran on could not be read")

    def test_the_budget_judges_the_gates_own_work_and_gives_its_wait_for_admission_apart(self):
        """vocaro/queuekeeper#112: on 2026-11-12 three BreezEPUB gates of 4.7 to 6.7 min of their own work waited 2.6 to
        15.8 min for admission, and the wall time put them over a 15-minute budget."""
        waited = [landed(1, 399, wait=157), landed(2, 290, wait=947), landed(3, 283, wait=604)]
        self.assertEqual(budgets.gate_warnings(self.config(budgetMinutes=10), waited), [])
        # Negative control: judged by their wall time, as before, the same runs were over that budget.
        self.assertEqual(len(budgets.gate_warnings(self.config(budgetMinutes=10), [
            {**entry, "gate": {**entry["gate"], "workSeconds": entry["gate"]["seconds"]}} for entry in waited])), 1)
        # Over budget by its own work, the warning gives the median wait beside it.
        [warning] = budgets.gate_warnings(self.config(budgetMinutes=15),
                                          [landed(1, 1200, wait=600), landed(2, 1260, wait=300), landed(3, 1140, wait=900)])
        self.assertIn("is 20.0 min, over its budget of 15 min (gate.budgetMinutes); their median wait for admission "
                      "before it, which the budget leaves out, was 10.0 min", warning)
        # The landing latency, the wall time as the queue saw it, is given apart: medians of 1800, 1560 and 2040 s.
        self.assertIn("and their median landing latency, the gate's time as the queue saw it with that wait, was "
                      "30.0 min", warning)
        # The latency's median is its own, not the sum of the other two: latencies of 1200, 1900 and 2900 s give
        # 1900 s, where the medians of work (1200 s) and wait (600 s) sum to 1800 s.
        [warning] = budgets.gate_warnings(self.config(budgetMinutes=15),
                                          [landed(1, 1200, wait=0), landed(2, 1300, wait=600), landed(3, 1100, wait=1800)])
        self.assertIn("is 20.0 min,", warning)
        self.assertIn("was 10.0 min, and their median landing latency", warning)
        self.assertIn("with that wait, was 31.7 min", warning)

    def test_a_run_recorded_without_its_own_work_is_left_out_of_the_median(self):
        """A record made before 2026-11-13, or whose admission was unseen, holds only a wall time with its wait in it."""
        old = [recorded_before(landed(n, 9000)) for n in range(3)]
        new = [landed(n, 60) for n in range(3, 6)]
        self.assertEqual(budgets.gate_warnings(self.config(budgetMinutes=2), old + new), [])
        self.assertEqual(budgets.gate_warnings(self.config(budgetMinutes=2), old), [])
        # Positive control: the new runs alone, over a budget, still warn, and the old ones are not counted as loaded.
        [warning] = budgets.gate_warnings(self.config(budgetMinutes=0.5), old + new)
        self.assertIn("over its last 3 passing runs", warning)
        self.assertNotIn("leaving out", warning)
        # Two new runs judge nothing, however many old ones stand beside them.
        self.assertEqual(budgets.gate_warnings(self.config(budgetMinutes=0.5), old + new[:2]), [])

    def test_the_warning_sends_the_reader_to_the_open_proposal_rather_than_to_open_another(self):
        """vocaro/queuekeeper#96: a warning after every landing over budget had sessions file and repeat readings."""
        [warning] = budgets.gate_warnings(self.config(budgetMinutes=1), [landed(n, 120) for n in range(3)])
        self.assertIn("Search the project's issue tracker for its open proposal on the gate's budget and add to it, or "
                      "open one only if there is none, naming which check to move to release.lanes; a reading the "
                      "proposal already has need not be repeated", warning)
        self.assertIn("The owner approves a move or a higher budget (the toolkit's guides/releasing.md, Keeping the "
                      "split)", warning)
        self.assertNotIn("Propose on the project's issue tracker", warning)

    def test_a_runs_time_keeps_its_wall_time_with_its_own_work_and_wait_apart(self):
        self.assertEqual(budgets.timed(100.04, 1030.0, 1000.0), {"seconds": 100.0, "workSeconds": 70.0, "waitSeconds": 30.0})
        # Admitted at once, or by a record written before the lane began, it waited nothing.
        self.assertEqual(budgets.timed(100, 1000.0, 1000.0), {"seconds": 100, "workSeconds": 100, "waitSeconds": 0.0})
        self.assertEqual(budgets.timed(100, 990.0, 1000.0)["waitSeconds"], 0.0)
        # The wait is never longer than the run.
        self.assertEqual(budgets.timed(100, 1500.0, 1000.0), {"seconds": 100, "workSeconds": 0.0, "waitSeconds": 100})
        # Its admission unseen, only the wall time is kept, which the budget leaves out.
        self.assertEqual(budgets.timed(100, None, 1000.0), {"seconds": 100})
        self.assertIsNone(budgets.own(budgets.timed(100, None, 1000.0)))
        self.assertEqual(budgets.time_words({"seconds": 600, "workSeconds": 240, "waitSeconds": 360}),
                         "10.0 min of landing latency: 4.0 min of its own work after 6.0 min waiting for admission")
        self.assertEqual(budgets.time_words({"seconds": 600}), "10.0 min of landing latency, its admission unseen, so "
                         "the gate's budget median leaves it out")

    def test_the_median_reads_only_the_recent_runs(self):
        old = [landed(n, 9000) for n in range(10)]
        recent = [landed(n, 60) for n in range(10, 10 + budgets.RECENT_RUNS)]
        self.assertEqual(budgets.gate_warnings(self.config(budgetMinutes=2), old + recent), [])
        self.assertEqual(len(budgets.gate_warnings(self.config(budgetMinutes=2), old)), 1)

    def test_the_budget_is_the_gate_lanes_alone_and_a_positive_integer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def load(lanes):
                (root / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Example", "lanes": lanes}))
                return configuration.load(root)
            self.assertEqual(load({"gate": {**GATE, "budgetMinutes": 30}})["lanes"]["gate"]["budgetMinutes"], 30)
            load({"mac": GATE, "gate": {"parallel": ["mac"], "budgetMinutes": 30}})
            for lanes, reason in [({"tests": {**GATE, "budgetMinutes": 30}}, "only the gate lane"),
                                  ({"gate": {**GATE, "budgetMinutes": 0}}, "positive integer"),
                                  ({"gate": {**GATE, "budgetMinutes": 2.5}}, "positive integer"),
                                  ({"gate": {**GATE, "budgetMinutes": True}}, "positive integer")]:
                with self.subTest(lanes=lanes), self.assertRaisesRegex(ValueError, reason): load(lanes)
        schema = json.loads((configuration.TOOLKIT_ROOT / "schemas/queuekeeper.schema.json").read_text())
        self.assertIn("budgetMinutes", schema["properties"]["lanes"]["additionalProperties"]["properties"])


class AdmittedSpan(unittest.TestCase):
    """The gate's admission, read from its wait record, and the busy share read only after it (vocaro/queuekeeper#112)."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.checkout = Path(directory.name)
        record = mq.lane_wait.path_of(self.checkout, "gate")
        patcher = patch.dict(os.environ, {mq.lane_wait.VARIABLE: str(record)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.record = record

    def test_admission_is_when_the_wait_record_first_says_the_lane_was_admitted(self):
        began = time.time() - 1
        admission = mq.Admission(self.checkout, "gate", began)
        admission.look()
        self.assertIsNone(admission.at)
        mq.lane_wait.waiting(mq.lane_wait.ADMISSION, "both slots are held")
        admission.look()
        self.assertIsNone(admission.at)
        mq.lane_wait.running()
        written = json.loads(self.record.read_text())["written"]
        admission.look()
        self.assertEqual(admission.at, written)
        # A later write of the record does not move it, and finish gives it after the record is gone.
        mq.lane_wait.waiting(mq.lane_wait.ADMISSION, "a nested wait")
        mq.lane_wait.running()
        admission.look()
        self.assertEqual(admission.at, written)
        mq.lane_wait.remove(self.record)
        self.assertEqual(admission.finish(), written)
        # Sent to the second Mac and not yet admitted there, it has not been admitted.
        relayed = mq.Admission(self.checkout, "gate", time.time() - 1)
        mq.lane_wait.sent("build-host")
        relayed.look()
        self.assertIsNone(relayed.at)
        mq.lane_wait.relayed(json.dumps({"kind": None, "admitted": True}), "build-host")
        relayed.look()
        self.assertIsNotNone(relayed.at)
        # A record written before the lane began is an earlier run's.
        stale = mq.Admission(self.checkout, "gate", time.time() + 60)
        stale.look()
        self.assertIsNone(stale.at)

    def test_the_admission_poll_sees_it_and_stops(self):
        mq.lane_wait.running()
        with patch.object(mq, "ADMISSION_POLL", 0.05):
            admission = mq.Admission(self.checkout, "gate", time.time() - 1).start()
            admission.thread.join(timeout=5)
        self.assertFalse(admission.thread.is_alive())
        self.assertIsNotNone(admission.finish())

    def test_the_busy_share_is_read_only_once_the_lane_is_admitted(self):
        admission = argparse.Namespace(at=None)
        with patch.object(mq, "busy_of", return_value=(95.0, 75.0, "this Mac")) as busy_of, \
                patch.object(mq.remote, "dispatched", return_value=None):
            sampler = mq.BusySampler(self.checkout, "gate", time.time(), admission).start()
            sampler.read()
            self.assertEqual(busy_of.call_count, 0)
            admission.at = time.time()
            sampler.read()
            self.assertEqual(busy_of.call_count, 1)
            self.assertEqual(sampler.finish()["busyReadings"], 1)
            # A run that ended before any reading after its admission is read once at its end, as before.
            unseen = mq.BusySampler(self.checkout, "gate", time.time(), argparse.Namespace(at=None)).start()
            unseen.read()
            self.assertEqual(busy_of.call_count, 1)
            self.assertEqual(unseen.finish()["busyReadings"], 1)
            self.assertEqual(busy_of.call_count, 2)


    def test_a_gate_sent_to_the_second_mac_is_read_there_while_it_runs(self):
        """vocaro/queuekeeper#132: the dispatch's record is filed only when it ends, so while the gate ran there it named
        no Mac, and each reading was this Mac's. The wait record names the second Mac from the moment the lane is sent."""
        began = time.time() - 1
        with patch.object(mq, "busy_of", side_effect=lambda host: (60.0, 90.0, host or "this Mac")) as busy_of:
            sampler = mq.BusySampler(self.checkout, "gate", began, argparse.Namespace(at=began)).start()
            mq.lane_wait.sent("build-host")
            mq.lane_wait.relayed(json.dumps({"kind": None, "admitted": True}), "build-host")
            # The real dispatch record: none filed yet, as for a dispatch still running.
            self.assertIsNone(mq.remote.dispatched(self.checkout, "gate", began))
            sampler.read()
            sampler.read()
            # Waiting for admission there, it is still that Mac.
            mq.lane_wait.relayed(json.dumps({"kind": "admission", "reason": "a nested wait"}), "build-host")
            sampler.read()
            mq.lane_wait.remove(self.record)
            found = sampler.finish()
        self.assertEqual([call.args[0] for call in busy_of.call_args_list], ["build-host"] * 3)
        self.assertEqual((found["busyOn"], found["busyLimit"], found["busyReadings"]), ("build-host", 90.0, 3))
        # The negative control: a lane that runs here is read here.
        mq.lane_wait.running()
        with patch.object(mq, "busy_of", return_value=(20.0, 70.0, "this Mac")) as here:
            local = mq.BusySampler(self.checkout, "gate", began, argparse.Namespace(at=began))
            local.read()
        here.assert_called_once_with(None)

    def test_once_the_record_is_gone_the_dispatch_names_the_mac(self):
        began = time.time() - 1
        with patch.object(mq, "busy_of", return_value=(60.0, 90.0, "build-host")) as busy_of, \
                patch.object(mq.remote, "dispatched", return_value="build-host"):
            mq.BusySampler(self.checkout, "gate", began).read()
        busy_of.assert_called_once_with("build-host")


class DoctorChecks(unittest.TestCase):
    def test_a_direct_main_gate_with_no_budget_warns(self):
        warned = budgets.doctor_warnings({"lanes": {"gate": GATE}})
        self.assertEqual(len(warned), 1)
        self.assertIn("gate has no budgetMinutes", warned[0])
        # The negative controls: a budget, a pull-request project, no gate.
        self.assertEqual(budgets.doctor_warnings({"lanes": {"gate": {**GATE, "budgetMinutes": 30}}}), [])
        self.assertEqual(budgets.doctor_warnings({"integration": "pull-request", "lanes": {"gate": GATE}}), [])
        self.assertEqual(budgets.doctor_warnings({"lanes": {"tests": GATE}}), [])

    def test_a_gate_that_takes_a_physical_device_warns(self):
        budgeted = {"budgetMinutes": 30, "timeout": 60}
        taking = [["xcodebuild", "test", "--device", "iPad"], ["tool", "--device=iPhone"],
                  ["python3", "scripts/qk", "devices", "take", "iPad", "--", "run"],
                  ["python3", "scripts/qk", "devices", "reserve", "iPhone"]]
        for command in taking:
            with self.subTest(command=command):
                [warning] = budgets.doctor_warnings({"lanes": {"gate": {"command": command, **budgeted}}})
                self.assertIn("gate takes a physical device", warning)
        # Through a lane group's lane too.
        group = {"lanes": {"device": {"command": taking[0]}, "unit": GATE,
                           "gate": {"parallel": ["unit", "device"], "budgetMinutes": 30}}}
        self.assertEqual(len(budgets.doctor_warnings(group)), 1)
        # The negative controls: a simulator destination, devices named but not taken, a device lane outside gate.
        for command in [["xcodebuild", "test", "-destination", "id=ABC"], ["python3", "scripts/qk", "devices", "status"],
                        ["tool", "--devices-list"]]:
            with self.subTest(command=command):
                self.assertEqual(budgets.doctor_warnings({"lanes": {"gate": {"command": command, **budgeted}}}), [])
        self.assertEqual(budgets.doctor_warnings({"lanes": {"gate": {**GATE, **budgeted},
                                                            "device": {"command": taking[0]}}}), [])

    def test_a_gate_that_names_only_simulator_types_does_not_warn(self):
        # vocaro/queuekeeper#113: qk simulator-tests's --device names a simulator type the pool supplies.
        budgeted = {"budgetMinutes": 30, "timeout": 60}
        simulators = [["python3", "scripts/qk", "simulator-tests", "--scheme", "S", "--runtime", "iOS-27-0",
                       "--device", "iPhone-17-Pro", "--device", "iPad-Air-13-inch-M4", "--"],
                      ["qk", "simulator-tests", "--device=iPhone-17-Pro", "--runtime", "iOS-27-0"],
                      ["python3", "scripts/qk", "simulator-pool", "take", "--device", "iPhone-17-Pro",
                       "--runtime", "iOS-27-0"]]
        for command in simulators:
            with self.subTest(command=command):
                self.assertFalse(budgets.takes_device(command))
                self.assertEqual(budgets.doctor_warnings({"lanes": {"gate": {"command": command, **budgeted}}}), [])
        group = {"lanes": {"ios-tests": {"command": simulators[0]}, "unit": GATE,
                           "gate": {"parallel": ["unit", "ios-tests"], "budgetMinutes": 30}}}
        self.assertEqual(budgets.doctor_warnings(group), [])
        # The positive controls: a physical device taken beside, or after, a simulator command's options.
        for command in [["python3", "scripts/qk", "device-pass", "--device", "iPad", "--", "run"],
                        ["python3", "scripts/qk", "remote", "run", "--device", "iPhone", "ui-tests"],
                        ["python3", "scripts/qk", "devices", "take", "iPad", "--", "python3", "scripts/qk",
                         "simulator-tests", "--device", "iPhone-17-Pro"],
                        ["python3", "scripts/qk", "simulator-tests", "--device", "iPhone-17-Pro", "--", "tool",
                         "--device", "iPad"],
                        ["simulator-tests", "--device", "iPad"]]:
            with self.subTest(command=command):
                self.assertTrue(budgets.takes_device(command))
                warnings = budgets.doctor_warnings({"lanes": {"gate": {"command": command, **budgeted}}})
                self.assertTrue(any("gate takes a physical device" in warning for warning in warnings), warnings)
        mixed = {"lanes": {"ios-tests": {"command": simulators[0]}, "device": {"command": ["tool", "--device", "iPad"]},
                           "gate": {"parallel": ["ios-tests", "device"], "budgetMinutes": 30}}}
        self.assertEqual(len(budgets.doctor_warnings(mixed)), 1)

    def test_eng_doctor_gives_them(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Example",
                "integration": "direct-main", "lanes": {"gate": {"command": ["tool", "--device", "iPad"], "timeout": 60}}}))
            with patch.dict(os.environ, {"QUEUEKEEPER_OFFLOAD_HOST": "none"}), \
                    patch.object(cli.shutil, "which", return_value="/usr/bin/true"), \
                    patch.object(cli, "source_identity", return_value={}), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(cli.main(["--project", directory, "doctor"]), 0)
            warnings = json.loads(output.getvalue())["warnings"]
            self.assertTrue(any("gate has no budgetMinutes" in warning for warning in warnings), warnings)
            self.assertTrue(any("gate takes a physical device" in warning for warning in warnings), warnings)


class Landing(Fixture):
    """The worker records each landed entry's gate run, and qk land warns after its result."""

    def setUp(self):
        super().setUp()
        (self.repo / "lane.py").write_text("print('== test_slow: OK (1 tests, 9.0s)')\n"
                                           "print('== test_fast: OK (1 tests, 1.0s)')\n")
        run(self.repo, "git", "add", "lane.py")
        run(self.repo, "git", "commit", "-qm", "Report parts")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")

    def budget(self, minutes):
        declared = json.loads((self.repo / "queuekeeper.json").read_text())
        declared["lanes"]["gate"]["budgetMinutes"] = minutes
        self.publish(declared)

    def land(self, identifier):
        said = io.StringIO()
        arguments = argparse.Namespace(repo=None, reference=None, note=None, wait=True, limit=10)
        with patch.object(mq, "add", return_value=identifier), patch.object(mq.Queue, "start"), \
                contextlib.redirect_stdout(said):
            self.assertEqual(mq.land(self.repo, arguments), 0)
        return said.getvalue()

    def seed(self, seconds, count=3):
        queue = mq.Queue(self.repo)
        for number in range(count):
            queue.save({**landed(number, seconds), "id": f"20000101-00000{number}-seed", "submitted": 0.0})

    def test_a_landed_entry_records_its_gate_run_with_the_parts_its_log_reports(self):
        self.commit("a", "a")
        [identifier] = self.add("a")
        with contextlib.redirect_stdout(io.StringIO()): self.work()
        entry = self.state(identifier)
        self.assertEqual(entry["state"], "landed")
        self.assertGreater(entry["gate"]["seconds"], 0)
        self.assertEqual(entry["gate"]["parts"], [["test_slow", 9.0], ["test_fast", 1.0]])
        # Beside its wall time, the busy share of the Mac it ran on (#235): the fixture's one reading at the end.
        self.assertEqual({key: entry["gate"][key] for key in ("cpuBusy", "cpuBusyHighest", "busyLimit", "busyOn", "busyReadings")},
                         {"cpuBusy": 25.0, "cpuBusyHighest": 25.0, "busyLimit": 75.0, "busyOn": "this Mac", "busyReadings": 1})
        # The fixture's gate is no whole-host lane, so its record does not say it is one.
        self.assertNotIn("wholeHost", entry["gate"])

    def test_a_whole_host_gates_record_says_so(self):
        declared = json.loads((self.repo / "queuekeeper.json").read_text())
        declared["lanes"]["gate"].update(resource="heavy", wholeHost=True)
        self.publish(declared)
        self.commit("a", "a")
        [identifier] = self.add("a")
        with contextlib.redirect_stdout(io.StringIO()): self.work()
        entry = self.state(identifier)
        self.assertEqual(entry["state"], "landed")
        self.assertIs(entry["gate"]["wholeHost"], True)

    def test_the_busy_share_is_read_while_the_gate_runs_and_of_the_mac_its_dispatch_names(self):
        (self.repo / "lane.py").write_text("import time\ntime.sleep(2)\nprint('== test_slow: OK (1 tests, 2.0s)')\n")
        run(self.repo, "git", "add", "lane.py")
        run(self.repo, "git", "commit", "-qm", "Take a while")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        readings = iter([(80.0, 75.0, "fake-mac"), (90.0, 75.0, "fake-mac"), None])
        self.busy_of.side_effect = lambda host: next(readings, (100.0, 75.0, "fake-mac"))
        asked = []
        real = mq.remote.dispatched
        def dispatched(root, name, since):
            asked.append(name)
            return "fake-mac" if name == "gate" else real(root, name, since)
        # While it runs, the lane's wait record names the Mac, as the second Mac's relayed record does
        # (vocaro/queuekeeper#132): its dispatch's record is filed only at its end.
        current = mq.lane_wait.current
        def relayed(path, since=None):
            record = current(path, since)
            if record and Path(path).stem == "gate": record = {**record, "mac": mq.lane_wait.second_mac("fake-mac")}
            return record
        self.commit("a", "a")
        [identifier] = self.add("a")
        with patch.object(mq, "BUSY_SAMPLE", 0.4), patch.object(mq.remote, "dispatched", side_effect=dispatched), \
                patch.object(mq.lane_wait, "current", side_effect=relayed), \
                contextlib.redirect_stdout(io.StringIO()) as said:
            self.work()
        gate = self.state(identifier)["gate"]
        hosts = [call.args[0] for call in self.busy_of.call_args_list]
        self.assertGreaterEqual(len(hosts), 3)
        self.assertEqual(set(hosts), {"fake-mac"})
        # A reading that could not be taken is left out of the mean.
        self.assertGreaterEqual(gate["busyReadings"], 2)
        self.assertEqual(gate["busyReadings"], len(hosts) - 1)
        self.assertGreaterEqual(gate["cpuBusy"], 85.0)
        self.assertEqual((gate["busyLimit"], gate["busyOn"]), (75.0, "fake-mac"))
        self.assertIn("the CPU of fake-mac was", said.getvalue())
        self.assertIn("a loaded run, which the gate's budget median leaves out", said.getvalue())
        # Its admission, seen while it ran, splits its wall time into its own work and its wait (#112).
        self.assertGreaterEqual(gate["workSeconds"], 2.0)
        self.assertGreaterEqual(gate["waitSeconds"], 0.0)
        self.assertAlmostEqual(gate["workSeconds"] + gate["waitSeconds"], gate["seconds"], delta=0.11)
        self.assertIn("of its own work after", said.getvalue())

    def test_a_gate_records_its_wait_for_admission_apart_from_its_own_work(self):
        """vocaro/queuekeeper#112: the wait before the lane's admission is kept, and left out of its own work."""
        (self.repo / "lane.py").write_text("import time\ntime.sleep(1.5)\nprint('== test_slow: OK (1 tests, 1.5s)')\n")
        run(self.repo, "git", "add", "lane.py")
        run(self.repo, "git", "commit", "-qm", "Take a while")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        self.commit("a", "a")
        [identifier] = self.add("a")
        # As though the lane's wait record said it was admitted one second after it began.
        with patch.object(mq.Admission, "finish", lambda admission: (admission.stopped.set(), admission.began + 1.0)[1]), \
                contextlib.redirect_stdout(io.StringIO()) as said:
            self.work()
        gate = self.state(identifier)["gate"]
        self.assertEqual(gate["waitSeconds"], 1.0)
        self.assertAlmostEqual(gate["workSeconds"], gate["seconds"] - 1.0, delta=0.11)
        self.assertGreater(gate["seconds"], 1.5)
        self.assertIn(f"gate took {budgets.time_words(gate)}", said.getvalue())

    def test_a_gate_whose_busy_share_cannot_be_read_records_its_time_alone(self):
        self.busy_of.return_value = None
        self.commit("a", "a")
        [identifier] = self.add("a")
        with contextlib.redirect_stdout(io.StringIO()) as said: self.work()
        gate = self.state(identifier)["gate"]
        self.assertGreater(gate["seconds"], 0)
        self.assertNotIn("cpuBusy", gate)
        self.assertIn("the CPU busy share of the Mac it ran on could not be read", said.getvalue())

    def test_eng_land_warns_after_its_result_when_the_gate_is_over_budget(self):
        self.budget(1)
        self.seed(600)
        self.commit("a", "a")
        [identifier] = self.add("a")
        # Admitted at once, which a lane this quick may end before a reading of its wait record sees (Admission).
        with patch.object(mq.Admission, "finish", lambda admission: (admission.stopped.set(), admission.began)[1]), \
                contextlib.redirect_stdout(io.StringIO()):
            self.work()
        said = self.land(identifier)
        result = said.index(f"landed: {self.state(identifier)['landedAs']}")
        warning = said.index("warning: gate's median time from its admission to its end over its last 4 passing runs")
        self.assertLess(result, warning)
        self.assertIn("test_slow 9s, test_fast 1s", said)

    def test_eng_land_gives_no_warning_under_budget(self):
        self.budget(60)
        self.seed(600)
        self.commit("a", "a")
        [identifier] = self.add("a")
        with contextlib.redirect_stdout(io.StringIO()): self.work()
        said = self.land(identifier)
        self.assertIn("landed:", said)
        self.assertNotIn("warning:", said)


if __name__ == "__main__":
    unittest.main()
