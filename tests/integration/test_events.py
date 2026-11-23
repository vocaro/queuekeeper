"""The queue's event journal (queuekeeper.events), and what the merge queue writes to it.

The queue's own behavior is in test_merge_queue.py, whose fixture this module's queue cases use."""
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src/python"))
import host_isolation  # noqa: F401 -- first, as every test module
from queuekeeper import cli, events, plugins, merge_queue as mq
from test_support import queue as test_merge_queue


class Journal(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.directory, True)

    def test_an_event_is_one_line_with_the_schema_keys_and_its_own(self):
        event = events.emit(self.directory, "entry.queued", entry="a", subject="Add a", note=None)
        self.assertEqual(json.loads((self.directory / events.FILE).read_text()), event)
        self.assertEqual({key: event[key] for key in ("v", "kind", "entry", "subject")},
                         {"v": events.VERSION, "kind": "entry.queued", "entry": "a", "subject": "Add a"})
        self.assertNotIn("note", event)
        self.assertAlmostEqual(event["time"], time.time(), delta=5)

    def test_an_events_time_is_its_millisecond_and_never_after_it(self):
        # Rounded, 0.0006 s past a second would be dated after the moment a reader that came next takes as now (#222).
        with patch.object(events.time, "time", return_value=1000.0006):
            self.assertEqual(events.emit(self.directory, "entry.queued", entry="a")["time"], 1000.0)

    def test_events_are_read_in_the_order_written_with_ids_of_their_own(self):
        for number in range(20): events.emit(self.directory, "entry.queued", entry=str(number))
        found = events.read(self.directory)
        self.assertEqual([event["entry"] for event in found], [str(number) for number in range(20)])
        self.assertEqual(len({event["id"] for event in found}), 20)

    def test_a_journal_that_cannot_be_written_stops_nothing(self):
        self.assertIsNone(events.emit(self.directory / "missing" / "folder", "entry.queued", entry="a"))
        (self.directory / events.FILE).mkdir()
        self.assertIsNone(events.emit(self.directory, "entry.queued", entry="a"))
        self.assertIsNone(events.emit(self.directory, "entry.queued", entry=object))  # json writes it with str

    def test_a_full_journal_starts_a_generation_and_both_are_read(self):
        with patch.object(events, "LIMIT", 400):
            for number in range(12): events.emit(self.directory, "entry.queued", entry=str(number))
        self.assertTrue((self.directory / events.PREVIOUS).is_file())
        self.assertLess((self.directory / events.FILE).stat().st_size, 400)
        kept = [event["entry"] for event in events.read(self.directory)]
        # The generation before the previous one is gone; what is kept is the newest, in order.
        self.assertEqual(kept, [str(number) for number in range(12 - len(kept), 12)])
        self.assertGreater(len(kept), 3)

    def test_each_hook_is_told_of_an_event_once_it_is_written_and_one_that_raises_stops_nothing(self):
        told = []
        def raising(directory, event): raise RuntimeError("a hook's fault")
        def reading(directory, event): told.append((directory, event, events.read(directory)))
        with patch.object(plugins, "AFTER_EVENT", [raising, reading]):
            event = events.emit(self.directory, "queue.resumed")
        self.assertEqual(told, [(self.directory, event, [event])])
        # An event that was not written is told to none.
        with patch.object(plugins, "AFTER_EVENT", [reading]):
            self.assertIsNone(events.emit(self.directory / "missing", "queue.resumed"))
        self.assertEqual(len(told), 1)

    def test_a_line_that_is_not_an_event_is_skipped(self):
        events.emit(self.directory, "queue.resumed")
        with (self.directory / events.FILE).open("a") as journal: journal.write('{"cut short\n[1]\n{"v": 1}\n')
        events.emit(self.directory, "queue.held", why="for a test")
        self.assertEqual([event["kind"] for event in events.read(self.directory)], ["queue.resumed", "queue.held"])

    def test_reading_selects_by_time_and_kind(self):
        with patch.object(events.time, "time", return_value=100.0): events.emit(self.directory, "entry.queued", entry="a")
        with patch.object(events.time, "time", return_value=200.0):
            events.emit(self.directory, "entry.landed", entry="a")
            events.emit(self.directory, "queue.held", why="w")
        kinds = lambda **how: [event["kind"] for event in events.read(self.directory, **how)]
        self.assertEqual(kinds(since=150), ["entry.landed", "queue.held"])
        self.assertEqual(kinds(kinds=["entry."]), ["entry.queued", "entry.landed"])
        self.assertEqual(kinds(kinds=["entry.landed", "queue.held"]), ["entry.landed", "queue.held"])
        self.assertEqual(kinds(kinds=["entry"]), [])

    def follow(self, write, count, **how):
        """The first count events a follower yields while write() runs, the follower started first."""
        found, done = [], threading.Event()
        def reader():
            for event in events.follow(self.directory, poll=0.01, stop=done.is_set, **how):
                found.append(event)
                if len(found) == count: break
        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        time.sleep(0.1)
        write()
        thread.join(host_isolation.stretched(10))
        done.set()
        self.assertFalse(thread.is_alive(), f"followed only {found}")
        return found

    def test_following_yields_only_what_is_written_from_then_on(self):
        events.emit(self.directory, "entry.queued", entry="before")
        def write():
            events.emit(self.directory, "entry.queued", entry="a")
            events.emit(self.directory, "queue.held", why="w")
            events.emit(self.directory, "entry.landed", entry="a")
        found = self.follow(write, 2, kinds=["entry."])
        self.assertEqual([(event["kind"], event["entry"]) for event in found],
                         [("entry.queued", "a"), ("entry.landed", "a")])

    def test_following_a_journal_that_does_not_exist_yet_yields_its_first_event(self):
        found = self.follow(lambda: events.emit(self.directory, "entry.queued", entry="first"), 1)
        self.assertEqual(found[0]["entry"], "first")

    def test_following_goes_on_into_a_new_generation(self):
        events.emit(self.directory, "entry.queued", entry="before")
        def write():
            with patch.object(events, "LIMIT", 300):
                for number in range(8):
                    events.emit(self.directory, "entry.queued", entry=str(number))
                    time.sleep(0.05)
        found = self.follow(write, 8)
        self.assertEqual([event["entry"] for event in found], [str(number) for number in range(8)])
        self.assertTrue((self.directory / events.PREVIOUS).is_file())

    def test_every_kind_has_a_line_in_words_and_an_unknown_one_its_name(self):
        for kind in events.KINDS:
            with self.subTest(kind=kind):
                line = events.words({"kind": kind, "time": 0})
                self.assertRegex(line, r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d \S")
        said = events.words({"kind": "entry.landed", "time": 0, "entry": "e1", "landedAs": "0123456789abcdef0123"})
        self.assertTrue(said.endswith("e1 landed as 0123456789ab"), said)
        self.assertTrue(events.words({"kind": "gate.ended", "time": 0, "lane": "gate", "status": 1,
                                      "mac": "this Mac (desk)"}).endswith("gate exited 1 on this Mac (desk)"))
        self.assertTrue(events.words({"kind": "later.kind", "time": 0}).endswith(" later.kind"))


class QueueEvents(test_merge_queue.Fixture):
    """What the merge queue writes to its journal as its state changes."""

    def kinds(self, **how):
        return [event["kind"] for event in self.events(**how)]

    def events(self, **how):
        return events.read(mq.Queue(self.repo).directory, **how)

    def test_a_landing_is_journaled_from_queued_to_landed(self):
        self.commit("a.txt", "one")
        identifier, = self.add("one")
        with patch("builtins.print"): self.assertEqual(self.work(), 0)
        self.assertEqual(self.state(identifier)["state"], mq.LANDED)
        gates = self.events(kinds=["gate."])
        # Each lane the round runs, a preflight lane before the gate among them, begins and ends in turn.
        self.assertEqual([event["kind"] for event in gates], ["gate.began", "gate.ended"] * (len(gates) // 2))
        self.assertEqual([event["lane"] for event in gates[-2:]], ["gate", "gate"])
        self.assertEqual({event["status"] for event in gates if event["kind"] == "gate.ended"}, {0})
        self.assertEqual([kind for kind in self.kinds() if not kind.startswith("gate.")],
                         ["entry.queued", "entries.testing", "round.began", "batch.passed", "batch.pushed",
                          "entry.landed"])
        by_kind = {event["kind"]: event for event in self.events()}
        self.assertEqual(by_kind["entry.queued"]["subject"], "Add a.txt")
        self.assertEqual(by_kind["entry.queued"]["commits"], 1)
        self.assertEqual(by_kind["round.began"]["entries"], [identifier])
        self.assertEqual(by_kind["entries.testing"]["entries"], [identifier])
        self.assertTrue(Path(by_kind["gate.ended"]["log"]).is_file())
        landed = by_kind["entry.landed"]
        self.assertEqual((landed["entry"], landed["landedAs"]), (identifier, self.state(identifier)["landedAs"]))
        self.assertEqual(by_kind["batch.pushed"]["head"], landed["landedTip"])

    def test_an_entry_that_fails_the_gate_is_journaled_with_why_and_its_log(self):
        self.status.write_text("1")
        self.commit("a.txt", "one")
        identifier, = self.add("one")
        with patch("builtins.print"): self.work()
        self.assertEqual(self.state(identifier)["state"], mq.FAILED)
        failed, = self.events(kinds=["entry.failed"])
        self.assertEqual((failed["entry"], failed["tip"]), (identifier, self.state(identifier)["tip"]))
        self.assertEqual((failed["why"], failed["log"]), (self.state(identifier)["why"], self.state(identifier)["log"]))
        self.assertEqual([event["status"] for event in self.events(kinds=["gate.ended"])][-1], 1)
        self.assertEqual(self.kinds(kinds=["entry.landed", "batch.pushed"]), [])

    def test_rebased_retries_settle_with_the_same_patch_identity(self):
        run = test_merge_queue.run
        original = self.commit("a.txt", "change")
        first, = self.add("change")
        queue = mq.Queue(self.repo)
        queue.settle(self.state(first), mq.FAILED)

        self.commit("unrelated.txt", "published")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        run(self.repo, "git", "fetch", "-q", "origin")
        run(self.repo, "git", "checkout", "-q", "change")
        run(self.repo, "git", "rebase", "origin/main")
        rebased = run(self.repo, "git", "rev-parse", "HEAD")
        self.assertNotEqual(original, rebased)
        second, = self.add("change")
        queue.settle(self.state(second), mq.CONFLICT)

        failed, conflict = self.events(kinds=["entry.failed", "entry.conflict"])
        self.assertEqual((failed["tip"], conflict["tip"]), (original, rebased))
        self.assertTrue(failed["patchId"])
        self.assertEqual(failed["patchId"], conflict["patchId"])

    def test_a_patch_identity_timeout_does_not_refuse_submission_or_settlement(self):
        self.commit("a.txt", "one")
        with patch.object(mq, "patch_of", side_effect=subprocess.TimeoutExpired("git patch-id", 1)):
            identifier, = self.add("one")
        queue = mq.Queue(self.repo)
        queue.settle(self.state(identifier), mq.FAILED)
        failed, = self.events(kinds=["entry.failed"])
        self.assertEqual(failed["tip"], self.state(identifier)["tip"])
        self.assertNotIn("patchId", failed)

    def test_removing_holding_and_resuming_are_journaled(self):
        self.commit("a.txt", "one")
        identifier, = self.add("one")
        queue = mq.Queue(self.repo)
        with patch("builtins.print"), patch.object(mq.Queue, "start"):
            mq.remove(queue, identifier)
            queue.hold(mq.worded(mq.OWNER, "a refused key"))
            queue.hold(mq.worded(mq.SESSION, "a cause"))
            mq.resume(self.repo)
            mq.resume(self.repo)  # no hold to clear, so nothing to say
        told = self.events()[1:]
        self.assertEqual([event["kind"] for event in told], ["entry.removed", "queue.held", "queue.held", "queue.resumed"])
        self.assertEqual([event.get("for") for event in told[1:3]], [mq.OWNER, mq.SESSION])
        self.assertIn("a refused key", told[1]["why"])

    def test_marking_tells_only_the_entries_whose_state_changed(self):
        self.commit("a.txt", "one")
        self.commit("b.txt", "two")
        first, second = self.add("one", "two")
        queue = mq.Queue(self.repo)
        queue.mark([self.state(first)], mq.TESTING)
        queue.mark([self.state(first), self.state(second)], mq.TESTING)
        queue.mark([self.state(first), self.state(second)], mq.QUEUED)
        queue.mark([self.state(first), self.state(second)], mq.QUEUED)
        told = [(event["kind"], event["entries"]) for event in self.events(kinds=["entries."])]
        self.assertEqual(told, [("entries.testing", [first]), ("entries.testing", [second]),
                                ("entries.requeued", [first, second])])

    def test_a_queue_whose_journal_cannot_be_written_still_lands(self):
        self.commit("a.txt", "one")
        (mq.Queue(self.repo).directory / events.FILE).mkdir()
        identifier, = self.add("one")
        with patch("builtins.print"): self.assertEqual(self.work(), 0)
        self.assertEqual(self.state(identifier)["state"], mq.LANDED)

    def test_the_snapshot_is_what_status_prints_as_json(self):
        self.commit("a.txt", "one")
        self.add("one")
        printed = io.StringIO()
        with patch("sys.stdout", printed): mq.status(self.repo, as_json=True)
        self.assertEqual(json.loads(printed.getvalue()), json.loads(json.dumps(mq.snapshot(self.repo))))
        self.assertEqual(sorted(mq.snapshot(self.repo)),
                         ["closed", "held", "idle", "nextAction", "recent", "recovery", "round", "schemaVersion", "ungated",
                          "waiting", "worker"])

    def run_command(self, *words):
        printed = io.StringIO()
        with patch("sys.stdout", printed):
            status = cli.main(["--project", str(self.repo), "queue", "events", *words])
        return status, printed.getvalue().splitlines()

    def test_the_command_prints_the_journal_in_words_or_as_json(self):
        self.commit("a.txt", "one")
        identifier, = self.add("one")
        with patch("builtins.print"), patch.object(mq.Queue, "start"): mq.remove(mq.Queue(self.repo), identifier)
        status, lines = self.run_command()
        self.assertEqual(status, 0)
        self.assertEqual([line.split(" ", 2)[2] for line in lines],
                         [f"{identifier} queued: Add a.txt", f"{identifier} removed"])
        status, lines = self.run_command("--json", "--kind", "entry.removed")
        self.assertEqual([json.loads(line) for line in lines], self.events(kinds=["entry.removed"]))
        status, lines = self.run_command("--since", "2999-01-01")
        self.assertEqual((status, lines), (0, []))


if __name__ == "__main__":
    unittest.main()
