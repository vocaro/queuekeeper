"""The warnings that keep the split between the landing and the release gate (vocaro-engineering#135): the gate
over its time budget, main past its release commit limit, and eng doctor's gate without a budget or with a device."""
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
from vocaro_engineering import budgets, cli, config as configuration, merge_queue as mq, release
import host_isolation  # noqa: F401
from test_merge_queue import Fixture, run

GATE = {"command": ["true"], "timeout": 60}


def landed(number, seconds, parts=(), landed_as=None):
    return {"id": f"entry-{number}", "state": "landed", "settled": float(number), "landedAs": landed_as or f"c{number}",
            "gate": {"seconds": seconds, "parts": [list(part) for part in parts]}}


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
                  ["python3", "scripts/eng", "devices", "take", "iPad", "--", "run"],
                  ["python3", "scripts/eng", "devices", "reserve", "iPhone"]]
        for command in taking:
            with self.subTest(command=command):
                [warning] = budgets.doctor_warnings({"lanes": {"gate": {"command": command, **budgeted}}})
                self.assertIn("gate takes a physical device", warning)
        # Through a lane group's lane too.
        group = {"lanes": {"device": {"command": taking[0]}, "unit": GATE,
                           "gate": {"parallel": ["unit", "device"], "budgetMinutes": 30}}}
        self.assertEqual(len(budgets.doctor_warnings(group)), 1)
        # The negative controls: a simulator destination, devices named but not taken, a device lane outside gate.
        for command in [["xcodebuild", "test", "-destination", "id=ABC"], ["python3", "scripts/eng", "devices", "status"],
                        ["tool", "--devices-list"]]:
            with self.subTest(command=command):
                self.assertEqual(budgets.doctor_warnings({"lanes": {"gate": {"command": command, **budgeted}}}), [])
        self.assertEqual(budgets.doctor_warnings({"lanes": {"gate": {**GATE, **budgeted},
                                                            "device": {"command": taking[0]}}}), [])

    def test_eng_doctor_gives_them(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "engineering.json").write_text(json.dumps({"schemaVersion": 1, "name": "Example",
                "integration": "direct-main", "lanes": {"gate": {"command": ["tool", "--device", "iPad"], "timeout": 60}}}))
            with patch.dict(os.environ, {"VOCARO_OFFLOAD_HOST": "none"}), \
                    patch.object(cli.shutil, "which", return_value="/usr/bin/true"), \
                    patch.object(cli, "source_identity", return_value={}), patch.object(cli, "agent_entry", return_value=[]), \
                    patch.object(cli.skills, "warnings", return_value=[]), contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(cli.main(["--project", directory, "doctor"]), 0)
            warnings = json.loads(output.getvalue())["warnings"]
            self.assertTrue(any("gate has no budgetMinutes" in warning for warning in warnings), warnings)
            self.assertTrue(any("gate takes a physical device" in warning for warning in warnings), warnings)


class CommitLimit(unittest.TestCase):
    """main's commits since the last commit that passed every release lane, by the receipts eng release reads."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        environment = patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_AUTHOR_NAME": "t",
                                              "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
                                              "GIT_COMMITTER_EMAIL": "t@example.com", "VOCARO_OFFLOAD_HOST": "none"})
        environment.start()
        self.addCleanup(environment.stop)
        run(self.root, "git", "init", "-q", "-b", "main")
        self.commits = []
        for number in range(10):
            run(self.root, "git", "commit", "-q", "--allow-empty", "-m", f"commit {number}")
            self.commits.append(run(self.root, "git", "rev-parse", "HEAD"))
        run(self.root, "git", "update-ref", "refs/remotes/origin/main", self.commits[-1])

    def config(self, lanes=("device", "corpus"), limit=3):
        return {"name": "Example", "lanes": {"gate": GATE, "device": GATE, "corpus": GATE},
                "release": {"lanes": list(lanes), "maxCommits": limit}}

    def passed_remotely(self, commit, lane, status=0):
        directory = self.root / ".git/vocaro-engineering/remote" / commit
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{lane}.json").write_text(json.dumps({"commit": commit, "lane": lane, "arguments": [],
            "result": {"exitStatus": status, "host": {"name": "second"}}}))

    def passed_here(self, commit, lane, outcome="passed"):
        directory = self.root / ".build/engineering" / lane
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "source.json").write_text(json.dumps({"project": {"revision": commit,
            "workingChangesSHA256": release.CLEAN}, "arguments": [], "outcome": outcome}))

    def test_under_the_limit_gives_no_warning(self):
        # commits[6] passed both: three commits since, at the limit.
        self.passed_remotely(self.commits[6], "device")
        self.passed_here(self.commits[6], "corpus")
        self.assertEqual(budgets.commit_warnings(self.root, self.config()), [])

    def test_over_the_limit_names_the_count_and_the_last_commit_that_passed_every_lane(self):
        self.passed_remotely(self.commits[5], "device")
        self.passed_remotely(self.commits[5], "corpus")
        [warning] = budgets.commit_warnings(self.root, self.config())
        self.assertIn(f"origin/main holds 4 commits since {self.commits[5][:12]}, the last commit that passed every "
                      "release lane (device, corpus), over release.maxCommits, 3", warning)
        self.assertIn("Run a checkpoint", warning)
        self.assertIn("guides/releasing.md, Checkpoints", warning)
        self.assertNotIn("has not passed", warning)

    def test_no_release_lanes_gives_no_warning(self):
        self.assertEqual(budgets.commit_warnings(self.root, self.config(lanes=())), [])
        # Nor do names that are no lane, which eng doctor warns of on its own.
        self.assertEqual(budgets.commit_warnings(self.root, self.config(lanes=("absent",))), [])

    def test_no_passing_commit_yet_counts_all_of_main(self):
        # A failure and a run that passed only part of the lanes' commits are no pass.
        self.passed_remotely(self.commits[8], "device", status=1)
        self.passed_here(self.commits[8], "corpus", outcome="failed")
        [warning] = budgets.commit_warnings(self.root, self.config())
        self.assertIn("origin/main holds 10 commits and none since adoption passed every release lane", warning)
        self.assertEqual(budgets.commit_warnings(self.root, self.config(limit=10)), [])

    def test_a_checkpoint_that_passed_every_lane_but_one_names_the_lane_still_to_run(self):
        self.passed_remotely(self.commits[2], "device")
        self.passed_remotely(self.commits[2], "corpus")
        # The checkpoint on commits[8] passed device, and corpus has not run there.
        self.passed_remotely(self.commits[8], "device")
        [warning] = budgets.commit_warnings(self.root, self.config())
        self.assertIn(f"origin/main holds 7 commits since {self.commits[2][:12]}", warning)
        self.assertIn(f"{self.commits[8][:12]}, a newer commit, has not passed corpus", warning)
        # Once corpus passes there, the checkpoint counts and main is under its limit.
        self.passed_here(self.commits[8], "corpus")
        self.assertEqual(budgets.commit_warnings(self.root, self.config()), [])

    def test_a_pass_off_main_does_not_count(self):
        run(self.root, "git", "checkout", "-q", "-b", "side", self.commits[1])
        run(self.root, "git", "commit", "-q", "--allow-empty", "-m", "side")
        side = run(self.root, "git", "rev-parse", "HEAD")
        self.passed_remotely(side, "device")
        self.passed_remotely(side, "corpus")
        [warning] = budgets.commit_warnings(self.root, self.config())
        self.assertIn("none since adoption", warning)


class Landing(Fixture):
    """The worker records each landed entry's gate run, and eng land warns after its result."""

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
