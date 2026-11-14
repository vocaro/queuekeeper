"""Recovery distinguishes a pending operation from a cause that needs repair."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/python"))
import host_isolation  # noqa: F401
from queuekeeper import merge_queue as mq, recovery


class EntryRecovery(unittest.TestCase):
    def test_suggested_commands_keep_the_target_queue_when_called_from_another_repository(self):
        detail = recovery.for_entry({"id": "one", "state": "testing"})
        target = Path(tempfile.gettempdir()) / "another checkout"
        qualified = recovery.in_project(detail, target)
        self.assertEqual(qualified["suggestedCommands"], [
            ["qk", "--project", str(target.resolve()), "queue", "wait", "one", "--json"]])
        self.assertEqual(detail["suggestedCommands"], [["qk", "queue", "wait", "one", "--json"]])
        self.assertEqual(recovery.in_project(qualified, Path(".")), qualified)
        self.assertIsNone(recovery.in_project(None, target))

    def test_project_qualification_preserves_explicit_projects_and_other_tools(self):
        commands = [["qk", "--project=/explicit", "queue", "status"], ["git", "fetch"]]
        detail = recovery.advice("example", "session", True, "wait", "Example", commands)
        self.assertEqual(recovery.in_project(detail, "/other")["suggestedCommands"], commands)

    def test_expired_wait_can_be_repeated_but_a_hold_must_be_repaired(self):
        entry = {"id": "a;echo bad", "state": "testing"}
        expired = recovery.for_entry(entry, limit_reached=True)
        self.assertEqual(expired["reasonCode"], "wait-limit")
        self.assertTrue(expired["retryable"])
        self.assertEqual(expired["suggestedCommands"], [["qk", "queue", "wait", "a;echo bad", "--json"]])
        hold = recovery.for_entry(entry, "for the owner, since nobody else can fix it: refused key",
                                  limit_reached=True)
        self.assertEqual(hold["reasonCode"], "queue-held")
        self.assertEqual(hold["responsibleParty"], "owner")
        self.assertFalse(hold["retryable"])
        self.assertEqual(hold["suggestedCommands"], [["qk", "queue", "doctor", "--json"]])

    def test_a_settled_entry_does_not_inherit_an_unrelated_hold(self):
        for state, code in (("landed", "entry-landed"), ("failed", "entry-failed"),
                            ("conflict", "entry-conflict"), ("removed", "entry-removed")):
            with self.subTest(state=state):
                result = recovery.for_entry({"id": "old", "state": state}, "for the owner: newer failure")
                self.assertEqual(result["reasonCode"], code)
                self.assertFalse(result["retryable"])


class HoldRecovery(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "hold"

    def test_structured_responsibility_survives_a_change_in_explanation(self):
        why = "The signing key needs attention"
        recovery.write_hold(self.path, why, "owner")
        self.assertEqual(recovery.read_hold(self.path, why)["responsibleParty"], "owner")
        # A later worker that writes only text must never inherit this owner's classification.
        changed = "for a session: fix the test runner"
        self.assertEqual(recovery.read_hold(self.path, changed)["responsibleParty"], "session")
        self.assertIsNone(recovery.read_hold(self.path, None))

    def test_legacy_or_corrupt_metadata_does_not_guess_an_owner_from_arbitrary_words(self):
        self.path.with_name("hold.json").write_text("{broken")
        result = recovery.read_hold(self.path, "the owner mentioned a locked screen")
        self.assertEqual(result["responsibleParty"], "unknown")
        self.assertFalse(result["retryable"])

    def test_queue_hold_keeps_the_original_text_and_records_structured_recovery(self):
        queue = object.__new__(mq.Queue)
        queue.hold_file = self.path
        queue.emit = Mock()
        why = mq.worded(mq.OWNER, "a refused key")
        with contextlib.redirect_stdout(io.StringIO()): queue.hold(why)
        self.assertEqual(queue.held(), why)
        record = json.loads(self.path.with_name("hold.json").read_text())
        self.assertEqual(record["recovery"]["responsibleParty"], "owner")
        queue.emit.assert_called_once_with("queue.held", why=why, **{"for": "owner"})

    def test_status_keeps_the_hold_string_and_exposes_recovery_separately(self):
        why = "for a session: the lane could not start"
        queue = Mock(hold_file=self.path)
        queue.all.return_value = [{"id": "one", "state": "queued"}]
        queue.working.return_value = False
        queue.held.return_value = why
        with patch.object(mq, "Queue", return_value=queue):
            snapshot = mq.snapshot(Path("."))
        self.assertEqual(snapshot["schemaVersion"], 1)
        self.assertEqual(snapshot["held"], why)
        self.assertEqual(snapshot["recovery"]["reasonCode"], "queue-held")
        self.assertEqual(snapshot["recovery"]["responsibleParty"], "session")
        self.assertEqual(snapshot["recovery"]["suggestedCommands"], [
            ["qk", "--project", str(Path(".").resolve()), "queue", "doctor", "--json"]])
        self.assertFalse(snapshot["idle"])

    def test_only_a_missing_worker_with_waiting_work_suggests_starting_one(self):
        result = recovery.for_queue(None, working=False, waiting=[{"id": "one"}])
        self.assertEqual(result["reasonCode"], "worker-not-running")
        self.assertEqual(result["suggestedCommands"], [["qk", "queue", "resume"]])
        self.assertIsNone(recovery.for_queue(None, working=False, waiting=[]))
        self.assertIsNone(recovery.for_queue(None, working=True, waiting=[{"id": "one"}]))


if __name__ == "__main__":
    unittest.main()
