"""Preflight lanes: the project's own cheap checks, which queue.preflight names and the queue runs before it queues an
entry and first in every round. Queuekeeper has no spelling check of its own (vocaro/queuekeeper#69)."""
import contextlib
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src/python"))
import host_isolation  # noqa: F401 -- first, as every test module
from queuekeeper import config, merge_queue as mq, preflight

# A lane that prints a finding and exits 1 when the file named bad exists, and passes otherwise.
FINDING = ["python3", "-c", "import os, sys; bad = os.path.exists('bad'); bad and print('bad:1: found it'); "
                            "sys.exit(1 if bad else 0)"]


def project(lanes, queue=None):
    """A project's configuration as queuekeeper.json would hold it, checked by config.parse."""
    return config.parse(json.dumps({"schemaVersion": 1, "name": "p", "lanes": {
        "gate": {"command": ["true"], "timeout": 60}, **lanes}, **({"queue": queue} if queue is not None else {})}))


def check(command):
    return {"command": command, "timeout": 60}


class Configuration(unittest.TestCase):
    def test_preflight_names_the_lanes_in_order(self):
        given = project({"lint": check(["true"]), "format": check(["true"])}, {"preflight": ["format", "lint"]})
        self.assertEqual(config.preflight(given), ["format", "lint"])

    def test_an_empty_or_absent_preflight_runs_none(self):
        self.assertEqual(config.preflight(project({}, {"preflight": []})), [])
        self.assertEqual(config.preflight(project({})), [])

    def test_a_lane_named_spelling_is_no_special_case(self):
        # Listed, it runs as any preflight lane; not listed, it is an ordinary lane the queue does not run first.
        self.assertEqual(config.preflight(project({"spelling": check(["true"])}, {"preflight": ["spelling"]})),
                         ["spelling"])
        self.assertEqual(config.preflight(project({"spelling": check(["true"])})), [])
        self.assertEqual(mq.lanes_of(project({"spelling": check(["true"])})), ["gate"])

    def test_the_old_spelling_settings_are_refused_with_where_the_check_went(self):
        for what, given in {"spelling": {"spelling": {"skip": {"vendor/": "third-party code"}}},
                            "queue.spelling": {"queue": {"spelling": False}}}.items():
            with self.subTest(what), self.assertRaisesRegex(ValueError, f"^{what} is no longer a Queuekeeper setting"):
                config.parse(json.dumps({"schemaVersion": 1, "name": "p", "lanes": {}, **given}))

    def test_what_preflight_refuses(self):
        lanes = {"lint": check(["true"]), "provision": check(["true"]), "shown": {**check(["true"]), "foreground": True},
                 "whole": {**check(["true"]), "wholeHost": True}, "member": check(["true"]),
                 "both": {"parallel": ["member"]}, "self": {**check(["true"]), "resource": "self"}}
        refused = {
            "a lane that is not configured": ({"preflight": ["missing"]}, "no configured lane"),
            "the gate": ({"preflight": ["gate"]}, "runs after the preflight lanes"),
            "provision": ({"preflight": ["provision"]}, "runs after the preflight lanes"),
            "one of queue.lanes": ({"preflight": ["lint"], "lanes": ["lint"]}, "runs after the preflight lanes"),
            "a foreground lane": ({"preflight": ["shown"]}, "it has foreground"),
            "a whole-host lane": ({"preflight": ["whole"]}, "it has wholeHost"),
            "a lane group": ({"preflight": ["both"]}, "it has parallel"),
            "a self lane": ({"preflight": ["self"]}, "it is a self lane"),
            "a name twice": ({"preflight": ["lint", "lint"]}, "distinct lane names"),
            "not a list": ({"preflight": "lint"}, "distinct lane names"),
        }
        for what, (queue, words) in refused.items():
            with self.subTest(what), self.assertRaisesRegex(ValueError, words): project(lanes, queue)
        # The control: the same lanes, with an acceptable list.
        self.assertEqual(config.preflight(project(lanes, {"preflight": ["lint"]})), ["lint"])

    def test_a_release_lane_cannot_be_a_preflight_lane(self):
        with self.assertRaisesRegex(ValueError, "release.lanes names lint, which a landing runs too"):
            config.parse(json.dumps({"schemaVersion": 1, "name": "p", "lanes": {"gate": check(["true"]),
                         "lint": check(["true"])}, "queue": {"preflight": ["lint"]}, "release": {"lanes": ["lint"]}}))

    def test_a_preflight_lane_without_a_timeout_is_stopped_at_the_preflight_limit(self):
        given = project({"lint": {"command": ["true"]}, "other": {"command": ["true"]}}, {"preflight": ["lint"]})
        self.assertEqual(config.lane_to_run(given, "lint", Path("."))["timeout"], config.PREFLIGHT_LIMIT)
        self.assertNotIn("timeout", config.lane_to_run(given, "other", Path(".")))


class QueueLanes(unittest.TestCase):
    def setUp(self):
        self.lanes = {"lint": check(["true"]), "format": check(["true"]), "provision": check(["true"]),
                      "docs": check(["true"])}

    def test_a_round_runs_the_preflight_lanes_then_provision_then_the_gate(self):
        given = project(self.lanes, {"preflight": ["format", "lint"]})
        self.assertEqual(mq.lanes_of(given), ["format", "lint", "provision", "gate"])
        self.assertEqual(mq.lanes_of(project(self.lanes, {"preflight": []})), ["provision", "gate"])

    def test_an_ungated_landing_runs_only_the_preflight_lanes(self):
        self.assertEqual(mq.ungated_lanes(project(self.lanes, {"preflight": ["lint"]})), ["lint"])
        self.assertEqual(mq.ungated_lanes(project(self.lanes, {"preflight": []})), [])

    def test_a_docs_only_batch_runs_the_preflight_lanes_then_its_docs_lanes(self):
        given = project(self.lanes, {"preflight": ["lint"]})
        self.assertEqual(mq.docs_lanes(given, ["README.md"]), ["lint", "docs"])
        self.assertEqual(mq.quick_lanes(given), {"lint", "docs"})

    def test_a_batch_of_documents_and_code_runs_each_preflight_lane_once(self):
        given = project(self.lanes, {"preflight": ["lint"]})
        self.assertEqual(mq.lanes_of(given, ["README.md", "src/a.py"]), ["lint", "provision", "docs", "gate"])

    def test_only_main_gives_the_preflight_lanes(self):
        # A batch read while main has no configuration gives no preflight lanes of its own: only main says which run.
        batch = mq.as_batch(json.loads(json.dumps(project(self.lanes, {"preflight": []}))))
        self.assertNotIn("preflight", batch["queue"])


class Command(unittest.TestCase):
    """qk preflight: the lanes run here, on the working tree, with every finding shown."""
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)

    def run_lanes(self, lanes, queue, names=()):
        said = StringIO()
        with contextlib.redirect_stderr(StringIO()):
            status = preflight.run(self.root, project(lanes, queue), names, out=lambda line: said.write(line + "\n"))
        return status, said.getvalue()

    def test_every_lane_passes(self):
        status, said = self.run_lanes({"lint": check(FINDING), "format": check(["true"])},
                                      {"preflight": ["lint", "format"]})
        self.assertEqual(status, 0)
        self.assertIn("lint: passed", said)
        self.assertIn("2 passed, 0 failed, 0 could not run", said)

    def test_a_finding_fails_and_the_rest_still_run(self):
        (self.root / "bad").write_text("")
        status, said = self.run_lanes({"lint": check(FINDING), "format": check(["true"])},
                                      {"preflight": ["lint", "format"]})
        self.assertEqual(status, 1)
        self.assertIn("lint: FAILED", said)
        self.assertIn("  bad:1: found it", said)
        self.assertIn("format: passed", said)

    def test_a_lane_that_could_not_run_is_never_a_pass(self):
        status, said = self.run_lanes({"lint": check(["python3", "-c", "raise SystemExit(2)"])},
                                      {"preflight": ["lint"]})
        self.assertEqual(status, preflight.UNMEASURED)
        self.assertIn("lint: could not run", said)

    def test_a_finding_outranks_a_lane_that_could_not_run(self):
        (self.root / "bad").write_text("")
        status, _ = self.run_lanes({"lint": check(FINDING), "broken": check(["python3", "-c", "raise SystemExit(2)"])},
                                   {"preflight": ["broken", "lint"]})
        self.assertEqual(status, 1)

    def test_one_lane_by_name_and_a_name_it_does_not_list(self):
        (self.root / "bad").write_text("")
        lanes, queue = {"lint": check(FINDING), "format": check(["true"])}, {"preflight": ["lint", "format"]}
        status, said = self.run_lanes(lanes, queue, ["format"])
        self.assertEqual(status, 0)
        self.assertNotIn("lint", said)
        status, said = self.run_lanes(lanes, queue, ["gate"])
        self.assertEqual(status, 2)
        self.assertIn("not among the project's preflight lanes", said)

    def test_no_preflight_lanes(self):
        self.assertEqual(self.run_lanes({}, {"preflight": []}), (0, "qk preflight: the project has no preflight lanes "
                                                                    "(queue.preflight)\n"))

    def test_a_lane_finds_the_projects_main_checkout(self):
        printing = ["python3", "-c", f"import os; print(os.environ['{config.PROJECT_CHECKOUT_VARIABLE}']); "
                                     "raise SystemExit(1)"]
        status, said = self.run_lanes({"where": check(printing)}, {"preflight": ["where"]})
        self.assertEqual(status, 1)
        self.assertIn(f"  {self.root.resolve()}", said)


if __name__ == "__main__":
    unittest.main()
