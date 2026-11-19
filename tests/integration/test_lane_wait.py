"""What a lane waits for, as qk run records it: each kind of wait, its clearing, and its coming back from the second
Mac (lane_wait, offload_host.relay_wait). tests/test_merge_queue.py's RoundAccount reads it as `qk queue status` does."""
import contextlib
from datetime import datetime, timedelta, timezone
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

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src/python"))
from queuekeeper import cli, lane_wait, offload_host, remote, resources, wait_history
from queuekeeper_mac_desktop import foreground
import host_isolation


class Named(unittest.TestCase):
    """A test with a lane's record named, as qk run names it."""

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
                (lane_wait.FOREGROUND, "held by s-1 for gate", "another Mac's foreground, held by s-1 for gate"),
                (lane_wait.UNREACHABLE, "No route to host",
                 "another Mac cannot be reached, and is asked again within a minute: No route to host"),
                (lane_wait.ALONE, "no reading", "this Mac alone, as another Mac cannot take it: no reading"),
                (lane_wait.OVERFLOW, "another Mac is busy",
                 "whichever Mac admits it first, the farm's other Macs asked again: another Mac is busy")]:
            with self.subTest(kind=kind):
                lane_wait.waiting(kind, reason)
                self.assertEqual(self.said(), expected)
                lane_wait.running()

    def test_a_wait_for_this_mac_says_why_the_farm_mac_did_not_take_it(self):
        lane_wait.waiting(lane_wait.OVERFLOW, "another Mac is busy")
        lane_wait.waiting(lane_wait.ADMISSION, "all 3 are busy")
        self.assertEqual(self.said(), "admission on this Mac: all 3 are busy; whichever Mac admits it first, the farm's "
                                      "other Macs asked again: another Mac is busy")
        lane_wait.running()
        # Admitted, the offer no longer applies to a later wait.
        lane_wait.waiting(lane_wait.ADMISSION, "all 3 are busy")
        self.assertEqual(self.said(), "admission on this Mac: all 3 are busy")

    def test_a_relayed_wait_names_the_farm_mac_and_keeps_no_offer(self):
        lane_wait.waiting(lane_wait.OVERFLOW, "another Mac is busy")
        lane_wait.relayed(json.dumps({"kind": "admission", "reason": "busy", "stranded": True}).encode(), "build-host")
        record = lane_wait.current(self.path)
        self.assertEqual(record["mac"], "build-host")
        self.assertIs(record["stranded"], True)
        self.assertEqual(self.said(), "admission on build-host: busy")
        lane_wait.relayed(b"null", "build-host")
        self.assertIsNone(self.said())
        self.assertEqual(lane_wait.current(self.path)["mac"], "build-host")
        # Garbled frames change nothing.
        lane_wait.relayed(b"{oops", "build-host")
        self.assertEqual(lane_wait.current(self.path)["mac"], "build-host")

    def test_a_send_keeps_a_wait_going_but_says_where_a_lane_with_none_went(self):
        lane_wait.sent("build-host")
        self.assertEqual(lane_wait.current(self.path)["mac"], "build-host")
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
        # Nor its observation record for `qk admissions now` (#342).
        admission._observe, admission._unobserve = lambda: None, lambda: None
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
        offered = remote.Offer("gate", "`qk run --here gate`", lambda host, sent: 0)
        offered.hosts, offered.host, offered.busy = ("fake",), "fake", "fake is busy: fake did not take it"
        return offered

    def test_overflow_says_whichever_mac_admits_it_first(self):
        offered = self.offer()
        with contextlib.redirect_stderr(StringIO()):
            self.assertEqual(remote.overflow(offered, lambda seconds: None, once=True), remote.BUSY)
        self.assertEqual(self.said(), "whichever Mac admits it first, the farm's other Macs asked again: fake is busy: "
                                      "fake did not take it")

    def test_an_offer_keeps_the_place_until_the_farm_mac_starts_the_lane(self):
        """vocaro/queuekeeper#160: while the lane is offered to another Mac, its place in this Mac's queue is kept
        holding (resources.Place.kept) for as long as the lane's record says that another Mac has not started it:
        through the offer and another Mac's own wait, and no longer once its admission there is relayed."""
        self.assertIsNone(lane_wait.taken_there())
        offered, keeping, unplaced = self.offer(), [], []
        class Place:
            @contextlib.contextmanager
            def kept(place, asked):
                keeping.append(asked)
                yield
                keeping.append(None)
        def send(host, sent):
            unplaced.append(keeping[-1]())
            lane_wait.relayed(json.dumps({"kind": lane_wait.ADMISSION, "reason": "all 3 guaranteed slots are busy"}).encode(), host)
            unplaced.append(keeping[-1]())
            # Declined there, the wait's process gone: the lane is still nowhere.
            lane_wait.relayed(b"null", host)
            unplaced.append(keeping[-1]())
            lane_wait.relayed(json.dumps({"kind": None, "admitted": True}).encode(), host)
            unplaced.append(keeping[-1]())
            return 0
        offered.place, offered.send = Place(), send
        with patch.object(remote, "reachable"), contextlib.redirect_stderr(StringIO()):
            self.assertEqual(remote.overflow(offered, lambda seconds: None, timeout=3600), 0)
        self.assertEqual(unplaced, [True, True, True, False])
        self.assertEqual(lane_wait.taken_there(), True)
        # Kept during the offer alone, and ended with it.
        self.assertEqual(len(keeping), 2)
        self.assertIsNone(keeping[-1])
        # Admitted on this Mac, the lane is not on the second; a run that names no record cannot say.
        lane_wait.running()
        self.assertEqual(lane_wait.taken_there(), False)
        with patch.dict(os.environ):
            del os.environ[lane_wait.VARIABLE]
            self.assertIsNone(lane_wait.taken_there())

    def test_overflow_says_a_farm_mac_it_cannot_reach(self):
        offered = self.offer()
        offered.away = "fake cannot be reached: it is asleep"
        with contextlib.redirect_stderr(StringIO()):
            remote.overflow(offered, lambda seconds: None, once=True)
        self.assertEqual(self.said(), "another Mac cannot be reached, and is asked again within a minute: fake cannot "
                                      "be reached: it is asleep")

    def test_one_slow_probe_keeps_the_farm_mac_in_rotation_and_two_say_how_many_and_how_long_ago(self):
        """vocaro/queuekeeper#25: one `ssh build-host true` that did not finish in 30 s, while another Mac answered a
        shell in 0.2 s, made status say it could not be reached for about six minutes, until the next offer."""
        slow = remote.Unreachable("fake cannot be reached: `ssh fake true` did not finish in 30s", "see whether fake is "
                                  "overloaded")
        for outcomes, expected in (
                # One slow answer, then another Mac takes the work: it never counted as unreachable.
                ([slow, None], [None, None]),
                # Two in a row: unreachable, said with how many and how long ago, then back.
                ([slow, slow, None], [None, None, 2])):
            lane_wait.running()
            offered, waits, seen = self.offer(), [], []
            def admit(seconds):
                waits.append(seconds)
                found = lane_wait.current(self.path)
                seen.append((found or {}).get("kind") == lane_wait.UNREACHABLE and found["probes"]["failed"])
                return None
            # Waits between offers already backed off to two minutes, as in a long wait.
            with self.subTest(failures=len(outcomes) - 1), patch.object(remote, "reachable", side_effect=outcomes), \
                    patch.object(remote, "OVERFLOW_WAIT", remote.OVERFLOW_WAIT_MAX), \
                    patch("sys.stderr", new_callable=StringIO) as error:
                self.assertEqual(remote.overflow(offered, admit, timeout=3600), 0)
            # The first wait is FREE_WAIT; each that follows a failed probe is the re-probe's, 30 s then 60 s.
            self.assertEqual(waits[1:], [remote.REPROBE_WAIT, remote.REPROBE_WAIT_MAX][:len(outcomes) - 1])
            self.assertEqual([found or None for found in seen], expected)
            self.assertEqual((offered.busy, offered.away, offered.failed, offered.failed_at), (None, None, 0, None))
            self.assertIn("as one offer that could not reach it does not make it unreachable", error.getvalue())
            self.assertEqual("after 2 offers in a row could not reach it" in error.getvalue(), len(outcomes) == 3)
        # Reached again after two failures: while the offer goes on, the status no longer says it cannot be reached.
        lane_wait.running()
        offered, sending = self.offer(), []
        offered.send = lambda host, sent: (sending.append(self.said()), 0)[1]
        with patch.object(remote, "reachable", side_effect=[slow, slow, None]), \
                patch("sys.stderr", new_callable=StringIO):
            self.assertEqual(remote.overflow(offered, lambda seconds: None, timeout=3600), 0)
        self.assertEqual(sending, ["whichever Mac admits it first, the farm's other Macs asked again: fake is busy: "
                                   "fake did not take it"])
        self.assertIsNone(lane_wait.current(self.path)["probes"])
        # Reached and busy, the lane still queued here: the status says another Mac is busy, and no probe count.
        lane_wait.running()
        offered, said = self.offer(), []
        def admit(seconds):
            said.append(self.said())
            return None
        busy = remote.Busy("every checkout is in use")
        with patch.object(remote, "reachable", side_effect=[slow, slow, busy, None]), \
                patch("sys.stderr", new_callable=StringIO):
            self.assertEqual(remote.overflow(offered, admit, timeout=3600), 0)
        self.assertIn("2 probes failed", said[2])
        self.assertEqual(said[3], "whichever Mac admits it first, the farm's other Macs asked again: fake is busy: "
                                  "every checkout is in use")
        self.assertIsNone(lane_wait.current(self.path)["probes"])
        # The record's age is the last failed probe's, read when the record is read.
        lane_wait.waiting(lane_wait.UNREACHABLE, slow.problem, probes={"failed": 2, "last": 1000.0})
        lane_wait.waiting(lane_wait.ADMISSION, "all 3 are busy")
        record = lane_wait.current(self.path)
        self.assertEqual(lane_wait.words(record, at=1040.0),
                         "admission on this Mac: all 3 are busy; another Mac cannot be reached (2 probes failed, the "
                         f"last 40 s ago), and is asked again within a minute: {slow.problem}")
        self.assertIn("the last 100 s ago", lane_wait.words(record, at=1100.0))

    @staticmethod
    def choosing(free):
        """Offer.room that gives a reading room by its "free" key alone, so that the fakes below need no capacity."""
        return patch.object(remote.Offer, "room", lambda offered, reading, mac: None if reading.get("free") else "full")

    def test_a_first_offer_that_cannot_reach_the_farm_mac_asks_it_again_soon(self):
        """vocaro/queuekeeper#25: one `ssh fake true` that did not finish at the first offer kept another Mac out of
        rotation for the lane's whole wait in this Mac's queue. It is asked again within REPROBE_WAIT: back, it takes
        the work; still down, it counts as unreachable at the second miss, asked again at a bounded backoff."""
        slow = remote.Unreachable("fake cannot be reached: `ssh fake true` did not finish in 30s", "see")
        for outcomes, expected in (([slow, None], 0), ([slow] * 4, "admitted here")):
            lane_wait.running()
            offered, waits, said = remote.Offer("gate", "`qk run --here gate`", lambda host, sent: 0, hosts=("fake",)), [], []
            def admit(seconds):
                waits.append(seconds)
                said.append(self.said())
                return "admitted here" if len(waits) == 4 else None
            with self.subTest(failures=outcomes.count(slow)), self.choosing(True), \
                    patch.object(remote, "reachable", side_effect=outcomes) as reachable, \
                    patch.object(remote, "capacity_there", return_value={"free": True}), \
                    patch.object(remote.offload_host, "capacity", return_value={"free": False}), \
                    patch("sys.stderr", new_callable=StringIO) as error:
                # The first miss runs the lane here, as before, but is no verdict on the Mac it missed.
                self.assertEqual(offered.make(), slow.problem)
                self.assertEqual((offered.busy, offered.away, offered.failed), (slow.problem, None, 1))
                self.assertIn(f"queuekeeper: running gate on this Mac: {slow.problem}; fake is asked again within "
                              f"{remote.REPROBE_WAIT}s while it waits here", error.getvalue())
                self.assertEqual(remote.overflow(offered, admit, timeout=3600), expected)
            # One probe per wait, never a tight loop: each wait at least FREE_WAIT, none past the re-probe's bound.
            self.assertEqual(reachable.call_count, len(outcomes))
            self.assertTrue(all(resources.FREE_WAIT <= wait <= remote.REPROBE_WAIT_MAX for wait in waits), waits)
            self.assertEqual(waits, sorted(waits))
            # The verdict's count and age: after one miss the Mac is still in rotation; after more, unreachable.
            self.assertRegex(said[0], r"^whichever Mac admits it first, the farm's other Macs asked again \(1 probe "
                                      r"failed, the last \d+ s ago\): fake cannot be reached")
            if expected == 0:
                self.assertEqual(waits, [resources.FREE_WAIT])
                self.assertEqual((offered.busy, offered.away, offered.failed, offered.missed), (None, None, 0, {}))
            else:
                self.assertRegex(said[-1], r"^another Mac cannot be reached \(4 probes failed, the last \d+ s ago\)")
                self.assertEqual(offered.reprobe(), remote.REPROBE_WAIT_MAX)
        # The negative control: work that never runs here (--offload-only) is not run, and nothing asks again.
        offered = remote.Offer("gate", "`qk run --here gate`", lambda host, sent: 0, hosts=("fake",))
        with patch.object(remote, "reachable", side_effect=slow), patch("sys.stderr", new_callable=StringIO):
            self.assertEqual(offered.make(fallback=False), remote.BUSY)
        self.assertIsNone(offered.busy)

    def test_a_farm_mac_of_several_missed_once_while_another_is_busy_is_asked_again_soon_and_once(self):
        """A Mac of several that one probe misses while the others decline the work as busy is asked again within
        REPROBE_WAIT, said with its name, its count and the probe's age; missed again, it waits for the next offer to
        the others, which costs them a checkout claim, rather than having them offered the work every half minute."""
        slow = remote.Unreachable("mini cannot be reached: `ssh mini true` did not finish in 30s", "see")
        for back in (True, False):
            lane_wait.running()
            reach = {"mini": iter([None, slow, None if back else slow]), "studio": iter([None] * 3)}
            readings = {"mini": iter([{"free": False}, {"free": True}]), "studio": iter([{"free": False}] * 3)}
            sends = []
            def send(host, sent):
                sends.append(host)
                if host == "studio" or sends.count("mini") == 1: raise remote.Busy("every checkout is in use")
                return 0
            def reachable(host):
                found = next(reach[host])
                if found is not None: raise found
            offered, waits, said = remote.Offer("gate", "`qk run --here gate`", send, hosts=("mini", "studio")), [], []
            def admit(seconds):
                waits.append(seconds)
                said.append(self.said())
                return "admitted here" if len(waits) == 3 else None
            with self.subTest(back=back), self.choosing(True), patch.object(remote, "reachable", reachable), \
                    patch.object(remote, "capacity_there", lambda host, **options: next(readings[host])), \
                    patch.object(remote.offload_host, "capacity", return_value={"free": False}), \
                    patch.object(remote, "OVERFLOW_WAIT", remote.OVERFLOW_WAIT_MAX), \
                    patch("sys.stderr", new_callable=StringIO):
                offered.make()
                self.assertIsNotNone(offered.busy)
                self.assertEqual(remote.overflow(offered, admit, timeout=3600), 0 if back else "admitted here")
            # Waits already backed off to two minutes: the miss brings the next offer within REPROBE_WAIT.
            self.assertEqual(waits[:2], [resources.FREE_WAIT, remote.REPROBE_WAIT])
            self.assertRegex(said[1], r"^whichever Mac admits it first, the farm's other Macs asked again \(mini: 1 "
                                      r"probe failed, the last \d+ s ago\): studio is busy: every checkout is in use$")
            self.assertIsNone(offered.away)
            if back:
                self.assertEqual((sends, offered.missed), (["mini", "studio", "mini"], {}))
            else:
                # Missed twice in a row: no early offer to the busy Mac, and the status says how many.
                self.assertEqual(waits[2], remote.OVERFLOW_WAIT_MAX)
                self.assertIsNone(offered.soon())
                self.assertRegex(said[2], r"\(mini: 2 probes failed, the last \d+ s ago\)")

    def test_a_farm_mac_that_can_no_longer_take_it_leaves_this_mac_alone(self):
        offered = self.offer()
        with patch.object(remote, "reachable", side_effect=[remote.Unavailable("names a device")]), \
                contextlib.redirect_stderr(StringIO()):
            self.assertIsNone(offered.again())
        self.assertEqual(self.said(), "this Mac alone, as another Mac cannot take it: names a device")

    def test_the_farm_mac_taking_it_after_all_clears_the_wait(self):
        """The cleared-wait case for a wait that crossed Macs."""
        lane_wait.waiting(lane_wait.OVERFLOW, "busy")
        lane_wait.relayed(json.dumps({"kind": None, "reason": None}).encode(), "fake")
        self.assertIsNone(self.said())
        self.assertEqual(lane_wait.current(self.path)["mac"], "fake")

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
        with patch.object(foreground, "farm_mac", return_value=True), \
                patch.object(foreground.reservations, "condition", return_value="held"), \
                patch.object(foreground.offload_host, "mac_name", return_value="here"), self.assertRaises(TimeoutError):
            with foreground.slot("gate", timeout=0, record=record, ready=lambda: [], said=lambda text: None):
                self.fail("ran while another run held the foreground")
        self.assertEqual(self.said(), "another Mac's foreground, held by s-2 for voiceover, expected release soon")


class History(Named):
    """Each wait that ends, kept in this Mac's wait history beside its admission log (wait_history, #233)."""

    def setUp(self):
        super().setUp()
        # The history is in this test's own namespace, as the admission log is: host_isolation's is shared by the module.
        patcher = patch.dict(os.environ, {"HEAVY_SLOT_DIR": str(self.folder / "slots")})
        patcher.start()
        self.addCleanup(patcher.stop)
        for owner, name, value in ((wait_history, "_mac", "Here"), (lane_wait, "_closes", None),
                                   (lane_wait, "_owned", None)):
            patcher = patch.object(owner, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def at(self, moment, call, *arguments, **options):
        with patch.object(lane_wait.time, "time", return_value=moment):
            return call(*arguments, **options)

    def kept(self):
        entries, unreadable = wait_history.read()
        self.assertEqual(unreadable, 0)
        return entries

    def test_the_log_is_beside_the_admission_log_in_the_namespace(self):
        self.assertEqual(wait_history.path(), resources.log_path().with_name("waits.jsonl"))
        self.assertEqual(wait_history.path().parent, self.folder / "slots")

    def test_each_kind_of_wait_is_kept_as_it_ends_with_its_lane_and_mac(self):
        os.environ.pop(lane_wait.VARIABLE)
        lane_wait.begin(self.folder, "gate", "EPUBLib", True)
        self.at(1000.0, lane_wait.waiting, lane_wait.SCREEN, "this Mac's screen is locked")
        # New words for the same wait keep its start.
        self.at(1300.0, lane_wait.waiting, lane_wait.SCREEN, "this Mac's screen shows the login window")
        self.at(1600.0, lane_wait.waiting, lane_wait.ADMISSION, "all 3 guaranteed slots are busy")
        self.assertEqual(len(self.kept()), 1)
        self.at(1690.0, lane_wait.running)
        # A nested run's admission reports into the outer lane's record, and is kept as that lane's.
        lane_wait.begin(self.folder, "swift-tests", "EPUBLib", False)
        self.at(1700.0, lane_wait.waiting, lane_wait.ADMISSION, "busy")
        self.at(1710.0, lane_wait.running)
        lane_wait.finish()
        screen, admission, nested = self.kept()
        self.assertEqual({key: screen[key] for key in ("project", "lane", "kind", "mac", "seconds", "wholeHost",
                                                        "reason", "ended")},
                         {"project": "EPUBLib", "lane": "gate", "kind": "screen", "mac": "Here", "seconds": 600.0,
                          "wholeHost": True, "reason": "this Mac's screen shows the login window",
                          "ended": "became a wait for admission"})
        self.assertEqual((screen["start"].timestamp(), screen["end"].timestamp()), (1000.0, 1600.0))
        self.assertEqual((admission["kind"], admission["seconds"], admission["ended"]), ("admission", 90.0, "cleared"))
        self.assertEqual((nested["lane"], nested["seconds"]), ("gate", 10.0))
        self.assertNotIn(lane_wait.LANE, os.environ)

    def test_a_wait_the_run_leaves_ends_with_the_run(self):
        # As a lane that waited out its limit for an unlocked screen does, exiting 75.
        lane_wait.begin(self.folder, "gate", "Pagepour", True)
        self.at(2000.0, lane_wait.waiting, lane_wait.SCREEN, "locked")
        self.at(2500.0, lane_wait.finish)
        [entry] = self.kept()
        self.assertEqual((entry["kind"], entry["seconds"], entry["ended"]), ("screen", 500.0, "the run ended"))
        # The record is not this process's to remove: a dispatch named it (offload_host), and it says the wait.
        self.assertEqual(lane_wait.current(self.path)["kind"], "screen")

    def test_a_wait_on_another_mac_is_left_to_that_macs_history(self):
        self.at(3000.0, lane_wait.waiting, lane_wait.OVERFLOW, "another Mac is busy")
        self.at(3030.0, lane_wait.relayed, json.dumps({"kind": "screen", "reason": "locked"}).encode(), "build-host")
        self.at(3500.0, lane_wait.relayed, b"null", "build-host")
        [entry] = self.kept()
        self.assertEqual((entry["kind"], entry["seconds"], entry["lane"], entry["project"], entry["wholeHost"]),
                         ("overflow", 30.0, "gate", None, None))

    def test_no_wait_keeps_nothing(self):
        lane_wait.running()
        os.environ.pop(lane_wait.VARIABLE)
        lane_wait.waiting(lane_wait.ADMISSION, "busy")
        lane_wait.running()
        self.assertEqual(self.kept(), [])

    def test_the_history_is_bounded_by_one_previous_generation(self):
        with patch.object(wait_history, "LIMIT", 600):
            for index in range(6):
                self.at(4000.0 + 10 * index, lane_wait.waiting, lane_wait.ADMISSION, "busy")
                self.at(4005.0 + 10 * index, lane_wait.running)
        log = wait_history.path()
        self.assertLessEqual(log.stat().st_size, 600)
        self.assertLessEqual(log.with_name("waits.1.jsonl").stat().st_size, 600)
        entries = self.kept()
        self.assertLess(len(entries), 6)
        self.assertEqual(entries[-1]["end"].timestamp(), 4055.0)

    def test_a_history_that_cannot_be_written_stops_nothing(self):
        (self.folder / "slots").write_text("not a folder")
        with contextlib.redirect_stderr(StringIO()) as said:
            lane_wait.waiting(lane_wait.ADMISSION, "busy")
            lane_wait.running()
        self.assertIn("the wait history was not written", said.getvalue())
        self.assertIsNone(lane_wait.current(self.path)["kind"])

    def test_another_mac_sends_the_lines_since_a_time(self):
        self.at(5000.0, lane_wait.waiting, lane_wait.SCREEN, "locked")
        self.at(5100.0, lane_wait.running)
        self.at(6000.0, lane_wait.waiting, lane_wait.SCREEN, "locked")
        self.at(6100.0, lane_wait.running)
        sent = offload_host.wait_log(6050.0)["lines"]
        self.assertEqual([wait_history.read_entry(line)["end"].timestamp() for line in sent], [6100.0])


class WaitReport(unittest.TestCase):
    """qk admissions waits: each Mac's waits by kind, an unlocked screen first, and by project and lane."""
    SINCE = datetime(2026, 11, 18, 8, tzinfo=timezone.utc)
    UNTIL = SINCE + timedelta(hours=8)

    def entry(self, lane, kind, start, minutes, whole=True, project="EPUBLib"):
        begun = self.SINCE + timedelta(minutes=start)
        line = {"time": (begun + timedelta(minutes=minutes)).isoformat(), "event": "wait", "project": project,
                "lane": lane, "kind": kind, "mac": "Here", "start": begun.isoformat(),
                "end": (begun + timedelta(minutes=minutes)).isoformat(), "seconds": minutes * 60.0,
                "wholeHost": whole, "reason": "why", "ended": "cleared"}
        return json.dumps(line)

    def lines(self, sources, kinds=None):
        found = []
        self.assertEqual(wait_history.report(sources, self.SINCE, self.UNTIL, kinds, found.append), 0)
        return found

    def test_screen_waits_come_first_and_each_lane_is_counted_within_the_period(self):
        here = [wait_history.read_entry(text) for text in [
            self.entry("gate", "admission", 10, 20),
            self.entry("gate", "screen", 30, 40),
            # Begun before the period: only its minutes inside it count.
            self.entry("gate", "screen", -30, 40),
            self.entry("mac-tests", "screen", 100, 5, whole=False, project="Pagepour"),
            # Wholly outside the period.
            self.entry("gate", "screen", 600, 10)]]
        self.assertNotIn(None, here)
        found = self.lines([("this Mac (Here)", here), ("build-host", "it did not answer in 30s"), ("other", [])])
        self.assertEqual(found[1:], [
            "  this Mac (Here):",
            "    an unlocked screen (screen): 3 waits, 55m00s in all, longest 40m00s",
            "      EPUBLib gate (whole-host): 2, 50m00s, longest 40m00s",
            "      Pagepour mac-tests: 1, 5m00s, longest 5m00s",
            "    admission (admission): 1 wait, 20m00s in all, longest 20m00s",
            "      EPUBLib gate (whole-host): 1, 20m00s, longest 20m00s",
            "  build-host: history unavailable: it did not answer in 30s",
            "  other: no wait ended in the period",
            "  A wait still going on is not counted until it ends; qk queue status shows it. A wait counts once for "
            "each kind it went through."])
        self.assertEqual(self.lines([("this Mac (Here)", here)], {"admission"})[1:3],
                         ["  this Mac (Here):", "    admission (admission): 1 wait, 20m00s in all, longest 20m00s"])

    def test_the_command_reads_a_copy_of_another_macs_history(self):
        folder = Path(tempfile.mkdtemp(dir=host_isolation.ROOT))
        copy = folder / "copy.jsonl"
        copy.write_text(self.entry("gate", "screen", 30, 40) + "\n{garbled\n")
        found = []
        with patch.dict(os.environ, {"HEAVY_SLOT_DIR": str(folder / "slots")}), \
                patch.object(cli.farm, "active", return_value=[{"name": "build-host", "ssh": "build-host"}]), \
                patch.object(offload_host, "mac_name", return_value="Here"), \
                patch.object(remote, "wait_log_there", side_effect=AssertionError("asked over ssh")):
            self.assertEqual(wait_history.main(["--since", self.SINCE.isoformat(), "--until", self.UNTIL.isoformat(),
                                                "--kind", "screen", "--there", str(copy)], found.append), 0)
        self.assertEqual(found[1:4], ["  this Mac (Here): no wait ended in the period", "  build-host:",
                                      "    an unlocked screen (screen): 1 wait, 40m00s in all, longest 40m00s"])


class Relay(unittest.TestCase):
    """offload_host, which runs alone on another Mac and so cannot import lane_wait, sending the record back."""

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
        """A run on another Mac that declined has exited with its wait recorded: a wait that goes on across
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
