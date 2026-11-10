"""What a lane waits for, as eng run records it: each kind of wait, its clearing, and its coming back from the second
Mac (lane_wait, offload_host.relay_wait). tests/test_merge_queue.py's RoundAccount reads it as `eng queue status` does."""
import contextlib
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src/python"))
from vocaro_engineering import cli, foreground, lane_wait, offload_host, remote, resources
import host_isolation


class Named(unittest.TestCase):
    """A test with a lane's record named, as eng run names it."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.path = lane_wait.path_of(self.folder, "gate")
        patcher = patch.dict(os.environ, {lane_wait.VARIABLE: str(self.path)})
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(lane_wait, "_offer", None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def said(self):
        return lane_wait.words(lane_wait.current(self.path))


class Recorded(Named):
    def test_a_wait_says_what_and_since_when_and_goes_on_under_other_words(self):
        with patch.object(lane_wait.time, "time", return_value=1000.0):
            lane_wait.waiting(lane_wait.ADMISSION, "all 3 guaranteed slots are busy")
        with patch.object(lane_wait.time, "time", return_value=1060.0):
            lane_wait.waiting(lane_wait.ADMISSION, "all 3 guaranteed slots are busy and CPU is 90%")
        record = lane_wait.current(self.path)
        self.assertEqual(record["since"], 1000.0)
        self.assertEqual(self.said(), "admission on this Mac: all 3 guaranteed slots are busy and CPU is 90%")

    def test_admission_clears_the_wait(self):
        lane_wait.waiting(lane_wait.ADMISSION, "all 3 guaranteed slots are busy")
        lane_wait.running()
        record = lane_wait.current(self.path)
        self.assertIsNone(record["kind"])
        self.assertEqual(record["mac"], "this Mac")
        self.assertIsNone(self.said())

    def test_each_kind_of_wait_in_plain_words(self):
        for kind, reason, expected in [
                (lane_wait.ADMISSION, "all 3 are busy", "admission on this Mac: all 3 are busy"),
                (lane_wait.SCREEN, "this Mac's is locked", "an unlocked screen: this Mac's is locked"),
                (lane_wait.FOREGROUND, "held by s-1 for gate", "the second Mac's foreground, held by s-1 for gate"),
                (lane_wait.UNREACHABLE, "No route to host",
                 "the second Mac cannot be reached, and is asked again at the next offer: No route to host"),
                (lane_wait.ALONE, "no reading", "this Mac alone, as the second Mac cannot take it: no reading"),
                (lane_wait.OVERFLOW, "the second Mac is busy",
                 "whichever Mac admits it first, the second Mac asked again: the second Mac is busy")]:
            with self.subTest(kind=kind):
                lane_wait.waiting(kind, reason)
                self.assertEqual(self.said(), expected)
                lane_wait.running()

    def test_a_wait_for_this_mac_says_why_the_second_mac_did_not_take_it(self):
        lane_wait.waiting(lane_wait.OVERFLOW, "the second Mac is busy")
        lane_wait.waiting(lane_wait.ADMISSION, "all 3 are busy")
        self.assertEqual(self.said(), "admission on this Mac: all 3 are busy; whichever Mac admits it first, the second "
                                      "Mac asked again: the second Mac is busy")
        lane_wait.running()
        # Admitted, the offer no longer applies to a later wait.
        lane_wait.waiting(lane_wait.ADMISSION, "all 3 are busy")
        self.assertEqual(self.said(), "admission on this Mac: all 3 are busy")

    def test_a_relayed_wait_names_the_second_mac_and_keeps_no_offer(self):
        lane_wait.waiting(lane_wait.OVERFLOW, "the second Mac is busy")
        lane_wait.relayed(json.dumps({"kind": "admission", "reason": "busy", "stranded": True}).encode(), "build-host")
        record = lane_wait.current(self.path)
        self.assertEqual(record["mac"], "the second Mac (build-host)")
        self.assertIs(record["stranded"], True)
        self.assertEqual(self.said(), "admission on the second Mac (build-host): busy")
        lane_wait.relayed(b"null", "build-host")
        self.assertIsNone(self.said())
        self.assertEqual(lane_wait.current(self.path)["mac"], "the second Mac (build-host)")
        # Garbled frames change nothing.
        lane_wait.relayed(b"{oops", "build-host")
        self.assertEqual(lane_wait.current(self.path)["mac"], "the second Mac (build-host)")

    def test_a_send_keeps_a_wait_going_but_says_where_a_lane_with_none_went(self):
        lane_wait.sent("build-host")
        self.assertEqual(lane_wait.current(self.path)["mac"], "the second Mac (build-host)")
        lane_wait.waiting(lane_wait.ADMISSION, "busy")
        lane_wait.sent("build-host")
        self.assertEqual(lane_wait.current(self.path)["kind"], lane_wait.ADMISSION)

    def test_a_record_is_no_change_to_the_checkouts_tree(self):
        """A lane's receipt identifies its tree after admission, with the record in place: it must still be clean,
        in a checkout that does not ignore .build, or the pass would not count for its commit."""
        git = lambda *arguments: subprocess.run(["git", "-C", str(self.folder), *arguments], check=True,
                                                capture_output=True, text=True).stdout
        git("init", "-q")
        # The control: a file beside the record's folder is seen as a change.
        (self.path.parent.parent / "other").parent.mkdir(parents=True)
        (self.path.parent.parent / "other").write_text("x")
        self.assertIn(".build/", git("status", "--porcelain"))
        (self.path.parent.parent / "other").unlink()
        lane_wait.waiting(lane_wait.ADMISSION, "busy")
        self.assertTrue(self.path.exists())
        self.assertEqual(git("status", "--porcelain"), "")

    def test_nothing_is_written_without_a_named_record(self):
        os.environ.pop(lane_wait.VARIABLE)
        lane_wait.waiting(lane_wait.ADMISSION, "busy")
        lane_wait.running()
        lane_wait.sent("build-host")
        self.assertFalse(self.path.exists())
        self.assertFalse(self.path.parent.exists())

    def test_a_record_that_cannot_be_read_says_nothing(self):
        self.assertIsNone(lane_wait.current(self.path))
        self.path.parent.mkdir(parents=True)
        for text in ("{not json", "[]", '{"schemaVersion": 99, "kind": null}', '{"schemaVersion": 1, "kind": "unknown"}'):
            self.path.write_text(text)
            self.assertIsNone(lane_wait.current(self.path), text)

    def test_begin_and_finish_own_the_record_of_the_outermost_run_only(self):
        os.environ.pop(lane_wait.VARIABLE)
        self.path.parent.mkdir(parents=True)
        self.path.write_text('{"left": "by an earlier run"}')
        with patch.object(lane_wait, "_owned", None):
            lane_wait.begin(self.folder, "gate")
            self.assertEqual(os.environ[lane_wait.VARIABLE], str(self.path))
            self.assertFalse(self.path.exists())
            # A run nested in it reports into the same record, and does not end it.
            lane_wait.begin(self.folder, "nested")
            self.assertEqual(os.environ[lane_wait.VARIABLE], str(self.path))
            lane_wait.waiting(lane_wait.ADMISSION, "busy")
            lane_wait.finish()
            self.assertFalse(self.path.exists())
            self.assertNotIn(lane_wait.VARIABLE, os.environ)
            lane_wait.finish()


class Waits(Named):
    """Each wait, as the code that waits records it."""

    def admission(self, attempts, timeout=30):
        """An Admission that attempts as attempts says, without touching this Mac's queue."""
        admission = resources.Admission.__new__(resources.Admission)
        admission.waiting, admission.refusal, admission.poll, admission.timeout = None, None, 0.0, timeout
        admission.descriptors, admission.held_model, admission.label, admission.counted = [(1, 0)], None, "gate", []
        admission.identity, admission.exclusive, admission.model = "test", False, False
        def attempt():
            step = attempts.pop(0)
            if step is True: return True
            admission.refusal, admission.waiting = step
            return False
        admission._attempt, admission._count, admission._withdraw = attempt, lambda: None, lambda error: None
        self.addCleanup(resources._held.pop, "test", None)
        return admission

    def test_admission_records_its_wait_and_clears_it_when_admitted(self):
        seen = []
        admission = self.admission([("slots", "all 3 guaranteed slots are busy"), True])
        real = lane_wait.running
        def looked(*arguments, **options):
            seen.append(self.said())
            return real(*arguments, **options)
        with patch.object(lane_wait, "running", looked), patch.object(resources, "inherited", return_value=False), \
                contextlib.redirect_stderr(StringIO()):
            admission.__enter__()
        self.assertEqual(seen, ["admission on this Mac: all 3 guaranteed slots are busy"])
        record = lane_wait.current(self.path)
        self.assertIsNone(record["kind"])
        self.assertEqual(record["mac"], "this Mac")

    def test_admission_says_when_stranded_entries_hold_it(self):
        admission = self.admission([("stranded", "3 are busy; the guard entry is dead"), True])
        real, stranded = lane_wait.running, []
        def looked(*arguments, **options):
            stranded.append(lane_wait.current(self.path)["stranded"])
            return real(*arguments, **options)
        with patch.object(lane_wait, "running", looked), patch.object(resources, "inherited", return_value=False), \
                contextlib.redirect_stderr(StringIO()):
            admission.__enter__()
        self.assertEqual(stranded, [True])

    def test_a_lane_admitted_at_once_never_records_a_wait(self):
        """Negative control: the first attempt admits it."""
        admission = self.admission([True])
        waited = []
        real = lane_wait.waiting
        with patch.object(lane_wait, "waiting", lambda *a, **o: (waited.append(a), real(*a, **o))), \
                patch.object(resources, "inherited", return_value=False), contextlib.redirect_stderr(StringIO()):
            admission.__enter__()
        self.assertEqual(waited, [])
        self.assertIsNone(self.said())

    def test_inherited_admission_records_nothing(self):
        admission = self.admission([])
        with patch.object(resources, "inherited", return_value=True):
            admission.__enter__()
        self.assertFalse(self.path.exists())

    def offer(self):
        offered = remote.Offer("gate", "`eng run --here gate`", lambda host, sent: 0)
        offered.host, offered.busy = "fake", "the second Mac is busy: fake did not take it"
        return offered

    def test_overflow_says_whichever_mac_admits_it_first(self):
        offered = self.offer()
        with contextlib.redirect_stderr(StringIO()):
            self.assertEqual(remote.overflow(offered, lambda seconds: None, once=True), remote.BUSY)
        self.assertEqual(self.said(), "whichever Mac admits it first, the second Mac asked again: the second Mac is busy: "
                                      "fake did not take it")

    def test_overflow_says_a_second_mac_it_cannot_reach(self):
        offered = self.offer()
        offered.away = "fake cannot be reached: it is asleep"
        with contextlib.redirect_stderr(StringIO()):
            remote.overflow(offered, lambda seconds: None, once=True)
        self.assertEqual(self.said(), "the second Mac cannot be reached, and is asked again at the next offer: fake cannot "
                                      "be reached: it is asleep")

    def test_a_second_mac_that_can_no_longer_take_it_leaves_this_mac_alone(self):
        offered = self.offer()
        with patch.object(remote, "reachable", side_effect=[remote.Unavailable("names a device")]), \
                contextlib.redirect_stderr(StringIO()):
            self.assertIsNone(offered.again())
        self.assertEqual(self.said(), "this Mac alone, as the second Mac cannot take it: names a device")

    def test_the_second_mac_taking_it_after_all_clears_the_wait(self):
        """The cleared-wait case for a wait that crossed Macs."""
        lane_wait.waiting(lane_wait.OVERFLOW, "busy")
        lane_wait.relayed(json.dumps({"kind": None, "reason": None}).encode(), "fake")
        self.assertIsNone(self.said())
        self.assertEqual(lane_wait.current(self.path)["mac"], "the second Mac (fake)")

    def test_a_locked_screen_is_recorded_while_the_lane_waits_for_it(self):
        readings = [{"state": "locked", "reason": "the console is locked"}, {"state": "unlocked"}]
        seen = []
        def reading():
            seen.append(self.said())
            return readings.pop(0)
        with patch.object(cli.offload_host, "screen", reading), patch.object(cli, "SCREEN_POLL", 0.0), \
                patch.object(cli, "this_mac", return_value="this Mac (t)"), contextlib.redirect_stderr(StringIO()):
            self.assertIsNone(cli.screen_wait("gate", 30, []))
        self.assertIsNone(seen[0])
        self.assertRegex(seen[1], r"^an unlocked screen: .*this Mac \(t\)'s is locked")

    def test_a_foreground_someone_holds_is_recorded(self):
        record = Mock()
        record.entry.return_value = {"ownerChatID": "s-2", "purpose": "voiceover", "expectedReleaseBy": "soon"}
        with patch.object(foreground, "second_mac", return_value=True), \
                patch.object(foreground.devices, "condition", return_value="held"), \
                patch.object(foreground.offload_host, "mac_name", return_value="here"), self.assertRaises(TimeoutError):
            with foreground.slot("gate", timeout=0, record=record, ready=lambda: [], said=lambda text: None):
                self.fail("ran while another run held the foreground")
        self.assertEqual(self.said(), "the second Mac's foreground, held by s-2 for voiceover, expected release soon")


class Relay(unittest.TestCase):
    """offload_host, which runs alone on the second Mac and so cannot import lane_wait, sending the record back."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "wait.json"

    def record(self, kind, reason="busy", pid=None, admitted=False):
        # Replaced whole, as lane_wait.write does: a relay that read a file being rewritten would see no record.
        staged = self.path.with_suffix(".staged")
        staged.write_text(json.dumps({"schemaVersion": 1, "kind": kind, "reason": reason, "stranded": False,
                                      "pid": os.getpid() if pid is None else pid, "admitted": admitted}))
        os.replace(staged, self.path)

    def run_relay(self, change, work=True):
        """The frames relay_wait sends while change runs, then the relay is told the step is done."""
        frames, done = [], threading.Event()
        waiter = threading.Event()
        def emit(kind, data):
            frames.append((kind, data))
            waiter.set()
        with patch.object(offload_host, "emit", emit), patch.object(offload_host, "WAIT_POLL", 0.01):
            thread = threading.Thread(target=offload_host.relay_wait, args=(self.path, done, work))
            thread.start()
            try: change(waiter)
            finally:
                done.set()
                thread.join(host_isolation.stretched(30))
        self.assertFalse(thread.is_alive())
        return frames

    def wait_for_frame(self, waiter):
        self.assertTrue(waiter.wait(host_isolation.stretched(30)))
        waiter.clear()

    def test_a_wait_comes_back_as_it_changes_and_clears(self):
        def change(waiter):
            self.record("admission", "all 3 are busy")
            self.wait_for_frame(waiter)
            self.record("admission", "2 are busy")
            self.wait_for_frame(waiter)
            self.record(None, None, admitted=True)
            self.wait_for_frame(waiter)
        frames = self.run_relay(change)
        self.assertEqual([json.loads(data) for kind, data in frames],
                         [{"kind": "admission", "reason": "all 3 are busy", "stranded": False, "admitted": False},
                          {"kind": "admission", "reason": "2 are busy", "stranded": False, "admitted": False},
                          {"kind": None, "reason": None, "stranded": False, "admitted": True}])
        self.assertEqual({kind for kind, _ in frames}, {b"W"})

    def test_a_lane_that_waits_for_nothing_sends_no_wait(self):
        """Negative control: no record, and a running record from the provision lane, send nothing."""
        self.assertEqual(self.run_relay(lambda waiter: None), [])
        self.record(None, None, admitted=True)
        self.assertEqual(self.run_relay(lambda waiter: None, work=False), [])
        # The lane's own step says so: it runs.
        self.assertEqual(len(self.run_relay(lambda waiter: None)), 1)

    def test_a_wait_whose_process_is_gone_is_sent_as_over(self):
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        def change(waiter):
            self.record("admission")
            self.wait_for_frame(waiter)
            self.record("admission", pid=dead.pid)
            self.wait_for_frame(waiter)
        frames = self.run_relay(change)
        self.assertEqual(json.loads(frames[-1][1]), None)
        self.assertEqual(len(frames), 2)

    def test_a_wait_left_as_the_step_ends_is_not_sent_as_over(self):
        """A run on the second Mac that declined has exited with its wait recorded: a wait that goes on across
        offers keeps its start, so the end of the step clears nothing (the test above is its control)."""
        frames = []
        done = Mock()
        done.is_set.side_effect = [False, True]
        waiting = {"kind": "admission", "reason": "busy", "stranded": False}
        with patch.object(offload_host, "emit", lambda kind, data: frames.append((kind, data))), \
                patch.object(offload_host, "wait_state", side_effect=[waiting, None]), \
                patch.object(offload_host, "WAIT_POLL", 0):
            offload_host.relay_wait(self.path, done, True)
        self.assertEqual(frames, [(b"W", json.dumps(waiting).encode())])

    def test_a_garbled_record_sends_nothing(self):
        self.path.write_text("{oops")
        self.assertEqual(self.run_relay(lambda waiter: None), [])


if __name__ == "__main__":
    unittest.main()
