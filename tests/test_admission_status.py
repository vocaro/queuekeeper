"""`queuekeeper admissions now`: who holds and who waits for admission on each Mac, read without changing anything (#342)."""
import contextlib
from datetime import datetime, timezone
import fcntl
from io import StringIO
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import host_isolation
from host_isolation import stretched

from queuekeeper import config, offload_host, processes, remote, resources


def tree(root):
    """Every file under root with its bytes, to show a reading changed nothing."""
    return {str(path.relative_to(root)): path.read_bytes() for path in sorted(Path(root).rglob("*")) if path.is_file()}


class Namespace(unittest.TestCase):
    """A private admission namespace: two guaranteed slots, three in all."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.directory, self.guard = root / "slots", root / "gate.warm-push-guard"
        environment = patch.dict(os.environ, {"HEAVY_SLOT_DIR": str(self.directory), "HEAVY_SLOTS": "2",
                                              "HEAVY_SLOTS_MAX": "3", "HEAVY_POLL_SECONDS": "0.01",
                                              "HEAVY_SETTLE_SECONDS": "0", "QUEUEKEEPER_GATE_LOCK_FILE": str(root / "gate")})
        environment.start()
        self.addCleanup(environment.stop)
        saved = os.environ.pop(resources.TOKEN, None)
        self.addCleanup(lambda: os.environ.__setitem__(resources.TOKEN, saved) if saved else os.environ.pop(resources.TOKEN, None))
        for patcher in [patch.object(resources, "cpu_busy", return_value=(10.0, 15.0)),
                        patch.object(resources, "memory_level", return_value=1),
                        patch.object(offload_host, "cpu_busy", return_value=10.0)]:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.start = resources.process_start(os.getpid())

    def owner(self, label, **fields):
        return {"pid": os.getpid(), "start": self.start, "label": label, "exclusive": False, "model": False,
                "admitted": datetime.now(timezone.utc).isoformat(timespec="seconds"), **fields}

    def holders(self, owners, slots=(), model=None):
        """A guard of owners by identity, slot files naming slots' holders, and the model file naming model's."""
        self.directory.mkdir(parents=True, exist_ok=True)
        self.guard.write_text(json.dumps({"vocaroEngineering": 1, "owners": owners}))
        for slot, identity in slots:
            (self.directory / f"slot{slot}").write_text(f"{os.getpid()} {identity} {owners.get(identity, {}).get('label')}\n")
        if model: (self.directory / "model").write_text(f"{os.getpid()} {model} x\n")

    def read(self):
        return offload_host.admissions_now()


class HoldersTests(Namespace):
    def test_a_free_host_is_observed_with_no_holder_and_no_waiter(self):
        reading = self.read()
        self.assertEqual((reading["state"], reading["reasons"], reading["holders"], reading["waiters"]),
                         ("observed", [], [], []))
        self.assertEqual(reading["limits"], {"slots": 2, "slotsMax": 3})
        self.assertFalse(reading["namespace"]["shared"])

    def test_each_kind_of_holder_with_its_slots_guaranteed_and_beyond(self):
        self.holders({"a": self.owner("Alpha unittest"), "b": self.owner("Beta gate", wholeHost=True),
                      "c": self.owner("Gamma model", model=True)},
                     slots=[(0, "a"), (1, "b"), (2, "c")], model="c")
        reading = self.read()
        self.assertEqual(reading["state"], "observed")
        found = {holder["label"]: holder for holder in reading["holders"]}
        self.assertEqual((found["Alpha unittest"]["guaranteed"], found["Alpha unittest"]["extra"]), ([0], []))
        self.assertTrue(found["Beta gate"]["wholeHost"])
        self.assertEqual((found["Gamma model"]["extra"], found["Gamma model"]["model"]), ([2], True))
        self.assertEqual(reading["model"], {"holder": "c"})
        self.assertTrue(all(holder["process"] == "running" and not holder["stranded"] for holder in reading["holders"]))
        lines = resources.now_lines({"hosts": [{"name": "this Mac", **reading}]})
        self.assertIn("  slots: 2 of 2 guaranteed and 1 of 1 beyond them held; the model held by Gamma model", lines)
        self.assertTrue(any(line.startswith("    Gamma model: heavy with the model, slot 2 (beyond the guaranteed)")
                            for line in lines))

    def test_exclusive_work_holds_every_slot_and_the_model(self):
        self.holders({"x": self.owner("Bench", exclusive=True, model=True)}, slots=[(0, "x")])
        reading = self.read()
        (holder,) = reading["holders"]
        self.assertEqual((holder["slots"], holder["model"]), ([0, 1, 2], True))
        self.assertEqual((reading["model"], reading["unattributed"]), ({"holder": "x"}, []))

    def test_dead_uncertain_and_unnamed_entries_are_stranded_not_free(self):
        self.holders({"dead": self.owner("Gone", start="Thu Jan  1 00:00:00 1970"),
                      "unsure": self.owner("Unsure", uncertain=True), "old": self.owner("Old")},
                     slots=[(0, "dead"), (1, "unsure"), (2, "released")])
        reading = self.read()
        found = {holder["label"]: holder for holder in reading["holders"]}
        self.assertEqual((found["Gone"]["process"], found["Gone"]["stranded"]), ("ended", True))
        self.assertTrue(found["Unsure"]["stranded"])
        # An entry no slot file names, as an older toolkit's may be, holds a slot no reading can place.
        self.assertIsNone(found["Old"]["slots"])
        self.assertEqual(reading["unattributed"], [2])
        self.assertIn("qk admissions recover", "\n".join(resources.now_lines({"hosts": [{"name": "this Mac", **reading}]})))

    def test_an_unreadable_guard_or_queue_or_process_check_is_partial_never_idle(self):
        self.directory.mkdir(parents=True)
        self.guard.write_text("{not json")
        (self.directory / resources.QUEUE).write_text("[]")
        reading = self.read()
        self.assertEqual((reading["state"], reading["holders"], reading["waiters"]), ("partial", None, None))
        self.assertEqual(len(reading["reasons"]), 2)
        self.holders({"a": self.owner("Alpha")}, slots=[(0, "a")])
        (self.directory / resources.QUEUE).unlink()
        with patch.object(offload_host, "census", return_value=None):
            reading = self.read()
        self.assertEqual((reading["state"], reading["holders"][0]["process"], reading["holders"][0]["stranded"]),
                         ("partial", "unknown", False))
        self.assertIn("ps could not say", reading["reasons"][0])

    def test_reading_changes_nothing(self):
        self.holders({"a": self.owner("Alpha")}, slots=[(0, "a"), (2, "released")], model="a")
        (self.directory / resources.QUEUE).write_text(json.dumps({"vocaroEngineering": 1, "waiters": [
            {"identity": "w", "pid": os.getpid(), "start": self.start, "label": "Waiter", "queuedAt": time.time() - 9,
             "seen": time.time() - 500, "ordinary": True}]}))
        (self.directory / resources.WAITS).mkdir()
        (self.directory / resources.WAITS / "r.json").write_text(json.dumps({
            "schemaVersion": 1, "identity": "v", "request": "r", "pid": os.getpid(), "start": self.start, "label": "Asking",
            "queueKind": "ordinary", "queuedAt": time.time() - 5, "sampledAt": time.time(), "waited": 5.0,
            "refusal": "slots", "reason": "all 2 guaranteed slots are busy"}))
        before = tree(self.temp.name)
        reading = self.read()
        self.assertEqual(tree(self.temp.name), before)
        found = {waiter["label"]: waiter for waiter in reading["waiters"]}
        # A place kept while its process is away, and a waiter whose own record says why, though the queue lacks it.
        self.assertEqual((found["Waiter"]["away"], found["Waiter"]["holdsBack"]), (True, False))
        self.assertEqual((found["Asking"]["reason"], found["Asking"]["queued"]), ("all 2 guaranteed slots are busy", False))

    def test_a_reading_too_large_is_cut_and_partial(self):
        reading = {"state": "observed", "reasons": [], "waiters": [{"label": "x" * 1000}] * 2000, "holders": [], "slots": []}
        cut = offload_host.bounded(reading)
        self.assertLessEqual(len(json.dumps(cut)), offload_host.NOW_BYTES)
        self.assertEqual(cut["state"], "partial")


class WaiterTests(Namespace):
    def hold_slots(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        held = [resources.take_slot(self.directory, slot + 1, start=slot)[1] for slot in range(3)]
        for fd in held: os.write(fd, b"1 other holder\n")
        return held

    def records(self):
        folder = self.directory / resources.WAITS
        return sorted(path.name for path in folder.glob("*.json")) if folder.exists() else []

    def waiting(self, admission, seconds=stretched(20)):
        """Run admission in a thread until its observation record appears; return the thread and what it raised."""
        outcome = {}
        def run():
            try:
                with admission: outcome["admitted"] = True
            except BaseException as error:  # noqa: BLE001
                outcome["error"] = error
        thread = threading.Thread(target=run)
        thread.start()
        deadline = time.monotonic() + seconds
        while not self.records():
            self.assertLess(time.monotonic(), deadline, "the waiter wrote no record")
            time.sleep(0.01)
        return thread, outcome

    def test_a_waiter_says_how_long_and_exactly_why_and_its_record_goes_when_it_is_admitted(self):
        held = self.hold_slots()
        admission = resources.Admission("Delta unittest", timeout=stretched(30))
        said = StringIO()
        with contextlib.redirect_stderr(said):
            thread, outcome = self.waiting(admission)
            (waiter,) = self.read()["waiters"]
            for fd in held: os.close(fd)
            thread.join(stretched(30))
        self.assertTrue(outcome.get("admitted"), outcome)
        self.assertEqual((waiter["label"], waiter["kind"], waiter["refusal"], waiter["process"], waiter["position"]),
                         ("Delta unittest", "ordinary", "slots", "running", None))
        # The very words the waiter printed.
        self.assertIn(f"heavy: waiting (0s): {waiter['reason']}\n", said.getvalue())
        self.assertIsNotNone(waiter["waitedSeconds"])
        self.assertEqual(self.records(), [])

    def test_its_record_goes_when_it_times_out_or_is_stopped(self):
        held = self.hold_slots()
        self.addCleanup(lambda: [os.close(fd) for fd in held])
        with contextlib.redirect_stderr(StringIO()):
            with self.assertRaises(TimeoutError):
                with resources.Admission("Timed", timeout=0): self.fail("admitted")
            self.assertEqual(self.records(), [])
            self.addCleanup(processes._stops.clear)
            thread, outcome = self.waiting(resources.Admission("Stopped", timeout=stretched(30)))
            processes.request_stop(signal.SIGTERM)
            thread.join(stretched(30))
        self.assertIsInstance(outcome.get("error"), processes.Interrupted)
        self.assertEqual(self.records(), [])

    def test_a_priority_waiter_has_its_place_and_a_model_waiter_names_the_holder(self):
        held = self.hold_slots()
        self.addCleanup(lambda: [os.close(fd) for fd in held])
        model = os.open(self.directory / "model", os.O_RDWR | os.O_CREAT)
        self.addCleanup(os.close, model)
        fcntl.flock(model, fcntl.LOCK_EX)
        os.write(model, b"1 someone Epsilon model\n")
        with contextlib.redirect_stderr(StringIO()):
            gate = resources.Admission("Zeta gate", whole_host=True, timeout=stretched(30))
            thread, _outcome = self.waiting(gate)
            runs = resources.Admission("Eta model", model=True, timeout=stretched(30))
            outcome = {}
            def run():
                try:
                    with runs: pass
                except BaseException as error:  # noqa: BLE001
                    outcome["error"] = error
            other = threading.Thread(target=run)
            other.start()
            deadline = time.monotonic() + stretched(20)
            while len(self.records()) < 2:
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.01)
            reading = self.read()
            self.addCleanup(processes._stops.clear)
            processes.request_stop(signal.SIGTERM)
            thread.join(stretched(30))
            other.join(stretched(30))
        found = {waiter["label"]: waiter for waiter in reading["waiters"]}
        self.assertEqual((found["Zeta gate"]["kind"], found["Zeta gate"]["position"]), ("wholeHost", 1))
        self.assertEqual(found["Eta model"]["model"], True)
        self.assertIn(found["Eta model"]["refusal"], ("model", "wholeHost", "slots"))
        self.assertEqual(self.records(), [])

    def test_a_crashed_waiter_is_not_waiting_and_a_later_waiter_sweeps_its_record(self):
        held = self.hold_slots()
        self.addCleanup(lambda: [os.close(fd) for fd in held])
        script = ("from queuekeeper import resources\n"
                  f"with resources.Admission('Crashed', timeout={stretched(60)}): pass\n")
        environment = {**os.environ, "PYTHONPATH": str(config.TOOLKIT_ROOT / "src/python")}
        crashed = subprocess.Popen([sys.executable, "-c", script], env=environment, stderr=subprocess.PIPE)
        self.assertIn(b"heavy: waiting", crashed.stderr.readline())
        deadline = time.monotonic() + stretched(20)
        while not self.records():
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.01)
        self.assertEqual([waiter["label"] for waiter in self.read()["waiters"]], ["Crashed"])
        crashed.kill()
        crashed.wait()
        crashed.stderr.close()
        self.assertEqual(len(self.records()), 1)
        # Its queue record and its own stay behind, and neither reads as waiting.
        self.assertEqual(self.read()["waiters"], [])
        # The crashed waiter swept when it first wrote; the next sweep is a minute on, or now without its marker.
        (self.directory / resources.WAITS / ".sweep").unlink()
        with contextlib.redirect_stderr(StringIO()), self.assertRaises(TimeoutError):
            with resources.Admission("Later", timeout=0): pass
        self.assertEqual(self.records(), [])

    def test_a_record_that_cannot_be_written_changes_no_admission(self):
        held = self.hold_slots()
        self.directory.joinpath(resources.WAITS).write_text("a file where the folder goes")
        said = StringIO()
        with contextlib.redirect_stderr(said):
            with self.assertRaisesRegex(TimeoutError, "so is every slot up to HEAVY_SLOTS_MAX=3"):
                with resources.Admission("Theta", timeout=0): pass
            for fd in held: os.close(fd)
            with resources.Admission("Theta", timeout=0): pass
        self.assertEqual(said.getvalue().count("was not recorded for `queuekeeper admissions now`"), 1)


class OriginTests(Namespace):
    """Where each holder's and waiter's work comes from: repository, branch, worktree, session and Mac (#342)."""
    hold_slots, records, waiting = WaiterTests.hold_slots, WaiterTests.records, WaiterTests.waiting
    SENT = {"repository": "BreezEPUB", "branch": "codex/issue-6", "worktree": "/Users/x/.codex/worktrees/ab/BreezEPUB",
            "session": "local_1234", "mac": "Trevors-MacBook-Pro-715"}

    def test_a_holder_and_a_waiter_say_where_their_work_comes_from(self):
        with patch.dict(os.environ, {"CLAUDE_CODE_HOST_SESSION_ID": "local_here"}):
            # A run dispatched from another Mac, as the queue's gate is, carries its sender's origin, which work_origin
            # reads first (#401).
            os.environ.pop(resources.ORIGIN, None)
            mine = resources.work_origin()
            held = self.hold_slots()
            with contextlib.redirect_stderr(StringIO()):
                thread, outcome = self.waiting(resources.Admission("python3 scripts/x.py", timeout=stretched(30)))
                (waiter,) = self.read()["waiters"]
                for fd in held: os.close(fd)
                thread.join(stretched(30))
            self.assertTrue(outcome.get("admitted"), outcome)
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True).stdout.strip()
        self.assertEqual((mine["worktree"], mine["session"], mine["mac"]), (top, "local_here", os.uname().nodename.split(".")[0]))
        self.assertTrue(mine["repository"])
        self.assertEqual(waiter["origin"], mine)
        with resources.Admission("python3 scripts/x.py", timeout=0) as admission:
            (holder,) = self.read()["holders"]
            self.assertEqual(holder["origin"], admission.origin)
            self.assertEqual(json.loads(self.guard.read_text())["owners"][admission.identity]["origin"], admission.origin)

    def test_work_sent_from_another_mac_names_the_senders_origin_and_a_bad_one_reads_past(self):
        with patch.dict(os.environ, {resources.ORIGIN: json.dumps({**self.SENT, "extra": 1, "branch": 7})}):
            self.assertEqual(resources.work_origin(), {k: v for k, v in self.SENT.items() if k != "branch"})
        with patch.dict(os.environ, {resources.ORIGIN: "not json"}):
            self.assertIn("mac", resources.work_origin())
        self.assertIsNone(offload_host.origin_of("x"))
        self.assertEqual(offload_host.origin_of({"repository": "r" * 400})["repository"], "r" * 300)
        # An entry with an origin is still in admission's format, and one with a malformed origin is refused, never read.
        self.assertIsNotNone(resources.entries({"vocaroEngineering": 1, "owners": {"a": self.owner("x", origin=self.SENT)}}))
        self.assertIsNone(resources.entries({"vocaroEngineering": 1, "owners": {"a": self.owner("x", origin="here")}}))

    def test_the_report_names_the_origin_and_no_longer_lists_unattributed_slots(self):
        reading = CommandTests.reading(None, holders=[{"identity": "a", "label": "python3 scripts/x.py", "pid": 1,
                                                       "exclusive": False, "wholeHost": False, "model": False,
                                                       "guaranteed": [0], "extra": [], "slots": [0], "heldSeconds": 5,
                                                       "process": "running", "origin": self.SENT}],
                                       unattributed=[1, 2], slots=[{"slot": 0, "guaranteed": True, "holder": "a"}])
        lines = []
        with patch.dict(os.environ, {"HEAVY_SLOT_DIR": "", "QUEUEKEEPER_GATE_LOCK_FILE": ""}):
            resources.now_main([], out=lines.append, read=lambda here_only: {"hosts": [{**reading, "name": "this Mac"}]})
        self.assertIn("      from repository BreezEPUB, branch codex/issue-6, worktree /Users/x/.codex/worktrees/ab/BreezEPUB, "
                      "session local_1234, mac Trevors-MacBook-Pro-715", lines)
        self.assertFalse([line for line in lines if "other slots" in line], lines)


class OrderingControl(Namespace):
    """An ordinary waiter's record changes neither who is admitted nor in what order, whether written, not written
    or failing (#342; the order itself is #21, #196, #360)."""

    def scenario(self):
        os.environ.update(HEAVY_SLOTS="1", HEAVY_SLOTS_MAX="1")
        self.directory.mkdir(parents=True, exist_ok=True)
        said, events = StringIO(), []
        queue = self.directory / resources.QUEUE
        order = lambda: [waiter["label"] for waiter in json.loads(queue.read_text())["waiters"]] if queue.exists() else []
        with contextlib.redirect_stderr(said):
            held = resources.take_slot(self.directory, 1)[1]
            ordinary, later = resources.Place(), resources.Place()
            for label, place, exclusive in [("ordinary", ordinary, False), ("bench", later, True)]:
                try:
                    with resources.Admission(label, exclusive=exclusive, timeout=0, place=place): events.append(label)
                except TimeoutError as error: events.append(f"{label} waits: {str(error).split(':', 1)[1]}")
                events.append(order())
            os.close(held)
            # The free slot goes to the ordinary request queued first, then to the exclusive one.
            for label, place, exclusive in [("bench", later, True), ("ordinary", ordinary, False), ("bench", later, True)]:
                try:
                    with resources.Admission(label, exclusive=exclusive, timeout=0, place=place): events.append(label)
                except TimeoutError as error: events.append(f"{label} waits: {str(error).split(':', 1)[1]}")
                events.append(order())
        ordinary.leave()
        later.leave()
        return [str(event).replace(str(self.directory), "DIR") for event in events]

    def test_written_unwritten_and_failing_records_admit_alike(self):
        written = self.scenario()
        with patch.object(resources.Admission, "_observe", lambda self: None):
            unwritten = self.scenario()
        (self.directory / resources.WAITS).rename(self.directory / "gone")
        (self.directory / resources.WAITS).write_text("a file")
        failing = self.scenario()
        self.assertEqual(written, unwritten)
        self.assertEqual(written, failing)
        self.assertEqual((written[1], written[3]), (str(["ordinary"]), str(["ordinary", "bench"])))
        self.assertIn("ordinary work queued ahead of it: ordinary", written[4])
        self.assertEqual(written[6:], ["ordinary", str(["bench"]), "bench", "[]"])

    def test_a_model_waiter_leaves_a_free_slot_to_later_plain_work(self):
        for mode in ["written", "unwritten"]:
            with self.subTest(mode=mode), contextlib.ExitStack() as stack:
                if mode == "unwritten": stack.enter_context(patch.object(resources.Admission, "_observe", lambda self: None))
                stack.enter_context(contextlib.redirect_stderr(StringIO()))
                self.directory.mkdir(parents=True, exist_ok=True)
                model = os.open(self.directory / "model", os.O_RDWR | os.O_CREAT)
                fcntl.flock(model, fcntl.LOCK_EX)
                place = resources.Place()
                with self.assertRaisesRegex(TimeoutError, "the on-device model"):
                    with resources.Admission("model run", model=True, timeout=0, place=place): pass
                with resources.Admission("plain", timeout=0) as admission:
                    self.assertEqual([slot for slot, _ in admission.descriptors], [0])
                place.leave()
                os.close(model)


class CommandTests(unittest.TestCase):
    def reading(self, state="observed", **fields):
        return {"schemaVersion": 1, "mac": "m", "observedAt": time.time(), "state": state, "reasons": [],
                "namespace": {"shared": True}, "limits": {"slots": 3, "slotsMax": 4}, "slots": [], "model": {"holder": None},
                "holders": [], "unattributed": [], "waiters": [], **fields}

    def test_an_unreachable_second_mac_is_unknown_beside_this_macs_reading(self):
        def unreachable(host, budget):
            self.assertLessEqual(budget, remote.ADMISSIONS_BUDGET)
            raise remote.Unreachable(f"{host} did not answer in {budget:g}s", "wake it")
        found = resources.admissions_now(read_here=self.reading, read_there=unreachable, configured=lambda: "build-host")
        here, there = found["hosts"]
        self.assertEqual((here["state"], here["holders"]), ("observed", []))
        self.assertEqual((there["state"], there["holders"], there["waiters"]), ("unknown", None, None))
        self.assertIn("build-host did not answer", there["reasons"][0])
        lines = []
        with patch.dict(os.environ, {"HEAVY_SLOT_DIR": "", "QUEUEKEEPER_GATE_LOCK_FILE": ""}):
            status = resources.now_main([], out=lines.append, read=lambda here_only: found)
        self.assertEqual(status, 1)
        self.assertTrue(any(line.startswith("second Mac (build-host): unknown: build-host did not answer") for line in lines))
        self.assertIn("  holders: none", lines)

    def test_no_second_mac_configured_is_no_failure_and_here_reads_this_mac_alone(self):
        asked = []
        found = resources.admissions_now(read_here=self.reading, read_there=lambda *_: asked.append(1),
                                         configured=lambda: None)
        self.assertEqual(found["hosts"][1]["state"], "notConfigured")
        with patch.dict(os.environ, {"HEAVY_SLOT_DIR": "", "QUEUEKEEPER_GATE_LOCK_FILE": ""}):
            self.assertEqual(resources.now_main(["--json"], out=lambda text: None, read=lambda here_only: found), 0)
        only = resources.admissions_now(here_only=True, read_here=self.reading, read_there=lambda *_: asked.append(1))
        self.assertEqual((len(only["hosts"]), asked), (1, []))
        # A reading this Mac cannot take is unknown, never idle.
        def failing(): raise OSError("no directory")
        broken = resources.admissions_now(here_only=True, read_here=failing)
        self.assertEqual((broken["hosts"][0]["state"], broken["hosts"][0]["holders"]), ("unknown", None))

    def test_eng_doctor_points_a_lane_waiting_for_admission_to_the_report(self):
        from queuekeeper import lane_wait
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root) / ".build/queuekeeper" / lane_wait.WAITS
            folder.mkdir(parents=True)
            record = {"schemaVersion": 1, "kind": "admission", "reason": "all 3 guaranteed slots are busy", "mac": "this Mac",
                      "offer": None, "stranded": False, "pid": os.getpid(), "admitted": False, "since": time.time() - 90,
                      "written": time.time()}
            (folder / "gate.json").write_text(json.dumps(record))
            (folder / "ui.json").write_text(json.dumps({**record, "kind": "screen"}))
            (folder / "gone.json").write_text(json.dumps({**record, "pid": 2 ** 30}))
            (warning,) = lane_wait.admission_warnings(root)
        self.assertRegex(warning, r"^lane gate has waited 9\ds for admission on this Mac: all 3 guaranteed slots are busy; "
                         r"`python3 scripts/qk admissions now`")

    def test_an_isolated_namespace_needs_here(self):
        # host_isolation names an isolated namespace for every test.
        with contextlib.redirect_stderr(StringIO()) as said, self.assertRaises(SystemExit) as stopped:
            resources.now_main([], read=lambda here_only: self.fail("read"))
        self.assertEqual(stopped.exception.code, 2)
        self.assertIn("add --here", said.getvalue())
        out = []
        self.assertEqual(resources.now_main(["--here", "--json"], out=out.append), 0)
        reading = json.loads(out[0])
        self.assertEqual([host["role"] for host in reading["hosts"]], ["here"])
        self.assertFalse(reading["hosts"][0]["namespace"]["shared"])


if __name__ == "__main__":
    unittest.main()
