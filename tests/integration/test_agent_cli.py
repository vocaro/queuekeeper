"""The queue's JSON mutation and wait interface against real Git entry records."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
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
                patch.object(mq.Queue, "start"), patch.object(cli.plugins, "recover"), contextlib.chdir(self.repo):
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

    def test_a_closed_queue_refuses_with_its_closure_in_the_structured_result(self):
        """vocaro/queuekeeper#32: `qk land --wait --json` and `qk queue add --json` say who closed the queue and why."""
        commit = self.commit("feature", "feature")
        mq.Queue(self.repo).close("a host goes away", "the maintenance session")
        for command, operation in ((("land", "feature", "--wait"), "land"), (("land", "feature"), "land"),
                                   (("queue", "add", "feature"), "queue.add")):
            with self.subTest(command=command):
                status, result, progress = self.invoke(*command, "--json")
                self.assertEqual((status, result["exitStatus"], result["operation"]), (1, 1, operation))
                self.assertEqual((result["id"], result["commit"], result["state"], result["terminal"]),
                                 (None, commit, "refused", True))
                self.assertIn("closed to new entries by the maintenance session", result["failure"]["message"])
                self.assertIn("a host goes away", result["failure"]["message"])
                advice = result["recovery"]
                self.assertEqual((advice["reasonCode"], advice["action"], advice["retryable"]),
                                 ("queue-closed", "wait-for-reopen", False))
                self.assertEqual((advice["closed"]["why"], advice["closed"]["by"]),
                                 ("a host goes away", "the maintenance session"))
                self.assertEqual(advice["suggestedCommands"],
                                 [["qk", "--project", str(self.repo.resolve()), "queue", "status", "--json"]])
                self.assertIn("is not queued: the queue is closed to new entries", progress)
        self.assertEqual(mq.Queue(self.repo).all(), [])
        mq.Queue(self.repo).reopen()
        status, result, _ = self.invoke("land", "feature", "--json")
        self.assertEqual((status, result["state"]), (0, mq.QUEUED))

    def test_runtime_and_configuration_errors_have_json_results(self):
        for error in (ValueError("Invalid project configuration"), OSError("Cannot read repository")):
            with patch.object(mq, "add", side_effect=error):
                status, result, progress = self.invoke("land", "--json")
            self.assertEqual(status, 2)
            self.assertEqual(result["failure"]["message"], str(error))
            self.assertEqual(result["recovery"]["reasonCode"], "usage-or-configuration-error")
            self.assertIn(str(error), progress)


class Outcomes(Fixture):
    """`qk queue remove --why`, nextAction and the outcome inbox, which a session that died or lost its IDs reads."""
    invoke = AgentCli.invoke

    def text(self, *words):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()), \
                patch.object(mq.Queue, "start"), patch.object(cli.plugins, "recover"), contextlib.chdir(self.repo):
            status = cli.main(["--project", str(self.repo), *words])
        return status, out.getvalue()

    def test_a_removal_reason_is_in_the_result_the_wait_and_the_recovery(self):
        self.commit("feature", "feature")
        [identifier] = self.add("feature")
        status, said = self.text("queue", "remove", identifier, "--why", "  superseded by   the owner's fix ")
        self.assertEqual(status, 0)
        self.assertEqual(self.state(identifier)["state"], mq.REMOVED)
        self.assertEqual(self.state(identifier)["removedWhy"], "superseded by the owner's fix")
        status, result, _ = self.invoke("queue", "result", identifier, "--json")
        self.assertEqual(result["removedWhy"], "superseded by the owner's fix")
        self.assertIn("superseded by the owner's fix", result["recovery"]["message"])
        status, waited, _ = self.invoke("queue", "wait", identifier, "--json")
        self.assertEqual((status, waited["removedWhy"]), (1, "superseded by the owner's fix"))
        self.assertIn("superseded by the owner's fix", waited["recovery"]["message"])
        status, said = self.text("queue", "wait", identifier)
        self.assertIn("removed: superseded by the owner's fix", said)

    def test_next_action_follows_the_entry_through_its_states(self):
        self.commit("feature", "feature")
        [identifier] = self.add("feature")
        project = ["qk", "--project", str(self.repo.resolve())]
        _, result, _ = self.invoke("queue", "result", identifier, "--json")
        self.assertEqual(result["nextAction"]["command"], [*project, "queue", "wait", identifier, "--json"])
        _, state, _ = self.invoke("queue", "status", "--json")
        self.assertEqual(state["nextAction"]["command"], [*project, "queue", "wait", identifier, "--json"])
        queue = mq.Queue(self.repo)
        queue.settle(queue.read(identifier), mq.FAILED, why="A test failed")
        _, result, _ = self.invoke("queue", "result", identifier, "--json")
        self.assertEqual(result["nextAction"]["command"], [*project, "queue", "logs", identifier, "--json"])
        _, state, _ = self.invoke("queue", "status", "--json")
        self.assertEqual(state["nextAction"], result["nextAction"])
        entry = queue.read(identifier)
        entry.update(state=mq.LANDED, landedAs="a" * 40)
        queue.save(entry)
        _, result, _ = self.invoke("queue", "result", identifier, "--json")
        self.assertEqual(result["nextAction"]["command"], [*project, "queue", "confirm", identifier, "--json"])
        self.assertEqual(self.text("queue", "confirm", identifier)[0], 0)
        _, result, _ = self.invoke("queue", "result", identifier, "--json")
        self.assertIsNone(result["nextAction"])
        _, state, _ = self.invoke("queue", "status", "--json")
        self.assertIsNone(state["nextAction"])

    def test_a_hold_is_the_next_action_before_any_entry(self):
        self.commit("feature", "feature")
        self.add("feature")
        queue = mq.Queue(self.repo)
        queue.hold("for the owner: a host needs repair")
        _, state, _ = self.invoke("queue", "status", "--json")
        self.assertEqual(state["nextAction"]["command"][-3:], ["queue", "doctor", "--json"])
        _, found, _ = self.invoke("queue", "inbox", "--json")
        self.assertEqual([item["outcome"] for item in found["entries"]], ["held"])
        self.assertIn("a host needs repair", found["entries"][0]["reason"])

    def test_the_inbox_lists_unconfirmed_outcomes_until_each_is_confirmed(self):
        for branch in ("landed", "failed", "removed", "elsewhere"): self.commit(branch, branch)
        landed, failed, removed, elsewhere = self.add("landed", "failed", "removed", "elsewhere")
        queue = mq.Queue(self.repo)
        queue.settle(queue.read(landed), mq.LANDED, landedAs="a" * 40)
        queue.settle(queue.read(failed), mq.CONFLICT, why="Conflicts with main")
        with patch.object(mq.resources, "coding_agent", return_value={"agent": "claude", "agentSession": "owner"}):
            self.text("queue", "remove", removed, "--why", "the owner took it out")
        subprocess.run(["git", "-C", str(self.repo), "checkout", "-q", "--detach"], check=True)
        other = queue.read(elsewhere)
        other.update(directory=str(self.base / "another-worktree"))
        queue.save(other)
        queue.settle(other, mq.LANDED, landedAs="b" * 40)
        status, found, _ = self.invoke("queue", "inbox", "--json")
        self.assertEqual((status, found["operation"]), (0, "queue.inbox"))
        self.assertEqual({item["id"]: (item["outcome"], item["reason"]) for item in found["entries"]},
                         {landed: ("landed", None), failed: ("sent-back", "Conflicts with main"),
                          removed: ("removed", "the owner took it out")})
        self.assertEqual(found["nextAction"]["command"][-3:], ["queue", "inbox", "--json"])
        for identifier in (landed, removed):
            status, confirmed, _ = self.invoke("queue", "confirm", identifier, "--json")
            self.assertEqual((status, confirmed["alreadyConfirmed"]), (0, False))
        status, confirmed, _ = self.invoke("queue", "confirm", landed, "--json")
        self.assertEqual((status, confirmed["alreadyConfirmed"]), (0, True))
        _, found, _ = self.invoke("queue", "inbox", "--json")
        self.assertEqual([item["id"] for item in found["entries"]], [failed])
        self.assertEqual(found["nextAction"], found["entries"][0]["nextAction"])
        # The session that submitted an entry knows when it removes it.
        self.commit("mine", "mine")
        [mine] = self.add("mine")
        entry = queue.read(mine)
        entry["agentSession"] = "author"
        queue.save(entry)
        with patch.object(mq.resources, "coding_agent", return_value={"agent": "claude", "agentSession": "author"}):
            self.text("queue", "remove", mine)
        self.assertTrue(queue.read(mine)["confirmed"])
        # Landing the branch again replaces the entry it sent back.
        self.commit("failed-again", "failed")
        [again] = self.add("failed")
        _, found, _ = self.invoke("queue", "inbox", "--json")
        self.assertEqual(found["entries"], [])
        status, confirmed, _ = self.invoke("queue", "confirm", again, "--json")
        self.assertEqual((status, confirmed["state"]), (1, mq.QUEUED))
        status, said = self.text("queue", "inbox")
        self.assertIn("no unconfirmed outcome", said)

    def test_old_outcomes_and_unknown_ids_stay_out_of_the_inbox(self):
        self.commit("feature", "feature")
        [identifier] = self.add("feature")
        queue = mq.Queue(self.repo)
        queue.settle(queue.read(identifier), mq.LANDED, landedAs="a" * 40)
        entry = queue.read(identifier)
        entry["settled"] -= 15 * 86400
        queue.save(entry)
        _, found, _ = self.invoke("queue", "inbox", "--json")
        self.assertEqual(found["entries"], [])
        status, result, _ = self.invoke("queue", "confirm", "missing-entry", "--json")
        self.assertEqual((status, result["state"]), (2, "error"))
