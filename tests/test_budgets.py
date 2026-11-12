"""The warnings that keep the split between the landing and the release gate (vocaro-engineering#135): the gate
over its time budget, main past its release commit limit, and queuekeeper doctor's gate without a budget or with a device."""
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
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src/python"))
from queuekeeper import budgets, cli, config as configuration, merge_queue as mq
import host_isolation  # noqa: F401
from test_merge_queue import Fixture, run

GATE = {"command": ["true"], "timeout": 60}


def landed(number, seconds, parts=(), landed_as=None, **busy):
    return {"id": f"entry-{number}", "state": "landed", "settled": float(number), "landedAs": landed_as or f"c{number}",
            "gate": {"seconds": seconds, "parts": [list(part) for part in parts], **busy}}


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
        self.assertIn("median wall time over its last 3 passing runs in the merge queue is 55.0 min, over its budget "
                      "of 50 min (gate.budgetMinutes)", warning)
        # The newest run that reports its parts.
        self.assertIn("slowest parts in its latest run that reports them: test_remote 1200s, test_queue 900s", warning)
        self.assertNotIn("old", warning)
        self.assertIn("guides/releasing.md, Keeping the split", warning)

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
                                                   [{**e, "gate": {"seconds": e["gate"]["seconds"]}} for e in unloaded + heavy])), 1)
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
        self.assertEqual(budgets.busy_words({}), "the CPU busy share of the Mac it ran on could not be read")

    def test_the_median_reads_only_the_recent_runs(self):
        old = [landed(n, 9000) for n in range(10)]
        recent = [landed(n, 60) for n in range(10, 10 + budgets.RECENT_RUNS)]
        self.assertEqual(budgets.gate_warnings(self.config(budgetMinutes=2), old + recent), [])
        self.assertEqual(len(budgets.gate_warnings(self.config(budgetMinutes=2), old)), 1)

    def test_the_budget_is_the_gate_lanes_alone_and_a_positive_integer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def load(lanes):
                (root / "engineering.json").write_text(json.dumps({"schemaVersion": 1, "name": "Example", "lanes": lanes}))
                return configuration.load(root)
            self.assertEqual(load({"gate": {**GATE, "budgetMinutes": 30}})["lanes"]["gate"]["budgetMinutes"], 30)
            load({"mac": GATE, "gate": {"parallel": ["mac"], "budgetMinutes": 30}})
            for lanes, reason in [({"tests": {**GATE, "budgetMinutes": 30}}, "only the gate lane"),
                                  ({"gate": {**GATE, "budgetMinutes": 0}}, "positive integer"),
                                  ({"gate": {**GATE, "budgetMinutes": 2.5}}, "positive integer"),
                                  ({"gate": {**GATE, "budgetMinutes": True}}, "positive integer")]:
                with self.subTest(lanes=lanes), self.assertRaisesRegex(ValueError, reason): load(lanes)
        schema = json.loads((configuration.TOOLKIT_ROOT / "schemas/engineering.schema.json").read_text())
        self.assertIn("budgetMinutes", schema["properties"]["lanes"]["additionalProperties"]["properties"])


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

    def test_eng_doctor_gives_them(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "engineering.json").write_text(json.dumps({"schemaVersion": 1, "name": "Example",
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
    """The worker records each landed entry's gate run, and queuekeeper land warns after its result."""

    def setUp(self):
        super().setUp()
        (self.repo / "lane.py").write_text("print('== test_slow: OK (1 tests, 9.0s)')\n"
                                           "print('== test_fast: OK (1 tests, 1.0s)')\n")
        run(self.repo, "git", "add", "lane.py")
        run(self.repo, "git", "commit", "-qm", "Report parts")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")

    def budget(self, minutes):
        declared = json.loads((self.repo / "engineering.json").read_text())
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
        self.commit("a", "a")
        [identifier] = self.add("a")
        with patch.object(mq, "BUSY_SAMPLE", 0.4), patch.object(mq.remote, "dispatched", side_effect=dispatched), \
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
        with contextlib.redirect_stdout(io.StringIO()): self.work()
        said = self.land(identifier)
        result = said.index(f"landed: {self.state(identifier)['landedAs']}")
        warning = said.index("warning: gate's median wall time over its last 4 passing runs")
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
