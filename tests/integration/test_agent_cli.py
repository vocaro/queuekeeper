"""The queue's JSON mutation and wait interface against real Git entry records."""
import contextlib
import io
import json
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src/python"))
import host_isolation  # noqa: F401 -- before queue imports
from queuekeeper import cli, merge_queue as mq, recovery
from test_support.queue import Fixture


class AgentCli(Fixture):
    def invoke(self, *words):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                patch.object(mq.Queue, "start"), patch.object(cli.extensions, "recover"), contextlib.chdir(self.repo):
            status = cli.main(["--project", str(self.repo), *words])
        # json.loads also rejects multiple result documents or stray text on stdout.
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["schemaVersion"], 1)
        self.assertEqual(payload.get("exitStatus", status), status)
        return status, payload, err.getvalue()

    def test_submission_duplicate_and_resume_have_one_json_result(self):
        commit = self.commit("feature", "feature")
        status, submitted, progress = self.invoke("land", "feature", "--json")
        self.assertEqual(status, 0)
        self.assertEqual((submitted["commit"], submitted["state"]), (commit, mq.QUEUED))
        self.assertIn("queued", progress)
        status, duplicate, progress = self.invoke("queue", "add", "feature", "--json")
        self.assertEqual(status, 0)
        self.assertEqual(duplicate["id"], submitted["id"])
        self.assertIn("already holds", progress)
        status, waiting, _ = self.invoke("queue", "wait", submitted["id"], "--json", "--limit", "0")
        self.assertEqual(status, 75)
        self.assertEqual(waiting["recovery"]["reasonCode"], "wait-limit")
        self.assertTrue(waiting["recovery"]["retryable"])

    def test_already_present_submission_and_refusal_are_structured(self):
        status, result, _ = self.invoke("land", "origin/main", "--json")
        self.assertEqual(status, 0)
        self.assertIsNone(result["id"])
        self.assertEqual(result["state"], "already_present")
        self.assertEqual(result["recovery"]["reasonCode"], "already-present")
        with patch.object(mq, "add", side_effect=mq.Refused("The submission does not apply")):
            for command in (("land",), ("queue", "add")):
                status, result, progress = self.invoke(*command, "--json")
                self.assertEqual(status, 1)
                self.assertEqual(result["state"], "refused")
                self.assertEqual(result["recovery"]["reasonCode"], "submission-refused")
                self.assertIn("does not apply", result["failure"]["message"])
                self.assertIn("does not apply", progress)

    def test_land_wait_and_queue_wait_return_landed_or_failed_entry(self):
        self.commit("feature", "feature")
        [identifier] = self.add("feature")
        queue = mq.Queue(self.repo)
        entry = queue.read(identifier)
        entry.update(state=mq.LANDED, landedAs="a" * 40)
        queue.save(entry)
        status, result, _ = self.invoke("queue", "wait", identifier, "--json")
        self.assertEqual(status, 0)
        self.assertEqual(result["landedAs"], "a" * 40)
        entry.update(state=mq.FAILED, why="A test failed")
        queue.save(entry)
        with patch.object(mq, "add", return_value=identifier):
            status, result, _ = self.invoke("land", "feature", "--wait", "--json")
        self.assertEqual(status, 1)
        self.assertEqual(result["failure"]["category"], "gate-failed")
        self.assertEqual(result["recovery"]["reasonCode"], "entry-failed")

    def test_a_hold_and_an_expired_wait_have_different_recovery(self):
        self.commit("feature", "feature")
        [identifier] = self.add("feature")
        queue = mq.Queue(self.repo)
        queue.hold("a host needs repair")
        recovery.write_hold(queue.hold_file, "a host needs repair", "owner")
        status, result, _ = self.invoke("queue", "wait", identifier, "--json", "--limit", "0")
        self.assertEqual(status, 75)
        self.assertEqual(result["recovery"]["reasonCode"], "queue-held")
        self.assertEqual(result["recovery"]["responsibleParty"], "owner")
        self.assertFalse(result["recovery"]["retryable"])

    def test_waits_that_complete_with_flakes_return_the_recorded_rerun_outcome(self):
        self.commit("feature", "feature")
        [identifier] = self.add("feature")
        queue = mq.Queue(self.repo)
        original = queue.read(identifier)
        flake = {"test": "test_example.Example.test_a", "commit": original["tip"],
                 "gateLog": "/pruned/gate.log", "rerunLogs": [f"/pruned/rerun-{n}.log" for n in range(3)],
                 "trackerError": "tracker unavailable", "mac": "build-host"}

        def settle(_seconds):
            queue.record(queue.read(identifier), state=mq.LANDED, landedAs="a" * 40, flakes=[flake])

        for command in (("queue", "wait", identifier), ("land", "feature", "--wait")):
            with self.subTest(command=command):
                queue.save(original)
                with patch.object(mq, "add", return_value=identifier), patch.object(mq.time, "sleep", settle):
                    status, result, _ = self.invoke(*command, "--json")
                self.assertEqual(status, 0)
                self.assertEqual(result["state"], mq.LANDED)
                self.assertTrue(result["terminal"])
                self.assertIsNone(result["failure"])
                self.assertIsNone(result["tests"])
                self.assertEqual(result["flakes"], [{**flake, "reruns": {"status": "passed", "count": 3}}])

    def test_multiple_entries_and_unknown_ids_are_structured(self):
        self.commit("first", "first")
        self.commit("second", "second")
        ids = self.add("first", "second")
        status, result, _ = self.invoke("queue", "wait", *ids, "--json", "--limit", "0")
        self.assertEqual(status, 75)
        self.assertEqual([entry["id"] for entry in result["entries"]], ids)
        self.assertEqual(result["requestedIds"], ids)
        status, result, _ = self.invoke("queue", "wait", "missing-entry", "--json")
        self.assertEqual(status, 2)
        self.assertEqual(result["state"], "error")
        self.assertIn("missing-entry", result["failure"]["message"])

    def test_land_refused_by_entries_ahead_can_time_out_before_submission(self):
        self.commit("feature", "feature")
        with patch.object(mq, "add", side_effect=mq.Refused("Conflicts with an entry ahead", ["ahead"])), \
                patch.object(mq, "wait_for", return_value=75):
            status, result, _ = self.invoke("land", "feature", "--wait", "--json", "--limit", "0")
        self.assertEqual(status, 75)
        self.assertIsNone(result["id"])
        self.assertEqual(result["state"], "waiting")
        self.assertEqual(result["waitsFor"], ["ahead"])
        self.assertEqual(result["recovery"]["reasonCode"], "wait-limit")
        self.assertEqual(result["recovery"]["suggestedCommands"][0][:3],
                         ["qk", "--project", str(self.repo)])

    def test_runtime_and_configuration_errors_have_json_results(self):
        for error in (ValueError("Invalid project configuration"), OSError("Cannot read repository")):
            with patch.object(mq, "add", side_effect=error):
                status, result, progress = self.invoke("land", "--json")
            self.assertEqual(status, 2)
            self.assertEqual(result["failure"]["message"], str(error))
            self.assertEqual(result["recovery"]["reasonCode"], "usage-or-configuration-error")
            self.assertIn(str(error), progress)
