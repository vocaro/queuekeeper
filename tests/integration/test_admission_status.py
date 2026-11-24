"""`qk admissions now`: who holds and who waits for admission on each Mac, read without changing anything (#342)."""
import contextlib
from datetime import datetime, timezone
import fcntl
from io import StringIO
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import host_isolation
from test_support import farm as farm_fixture
from host_isolation import stretched

from queuekeeper import config, offload_host, place_holder, processes, remote, resources


# cpu_use itself, which Namespace replaces for every reading so that the host's load cannot make one partial.
CPU_USE = offload_host.cpu_use


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
                        patch.object(offload_host, "cpu_busy", return_value=10.0),
                        # What each holder costs the CPU is read by one ps of every process on the Mac, which a loaded
                        # host can time out or fail: a reading here would turn partial on the host's load, not on
                        # what a test sets up (vocaro/queuekeeper#210). The tests of cpu_use call it directly.
                        patch.object(offload_host, "cpu_use", side_effect=lambda pids: {pid: 1.5 for pid in pids})]:
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

    def test_held_slots_show_a_live_checkpoint_by_its_dedicated_worktree(self):
        tree = str(self.directory.parent / "eng-checkpoint-fixture" / "Alpha")
        origin = {"repository": "Alpha", "worktree": tree}
        self.holders({"a": self.owner("Alpha build", origin=origin),
                      "b": self.owner("Alpha elsewhere", origin={**origin, "worktree": "/another/Alpha"}),
                      "dead": self.owner("Previous lane", origin=origin, start="Thu Jan  1 00:00:00 1970")},
                     slots=[(0, "a"), (1, "b"), (2, "dead")])
        directory = self.directory / "checkpoints"
        directory.mkdir()
        record = {"project": "Alpha", "commit": "0123456789abcdef", "runsIn": tree,
                  "lane": "build", "index": 2, "count": 4}
        (directory / "Alpha.json").write_text(json.dumps({**record, "origin": "private details left out"}))
        with (directory / "Alpha.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            reading = resources.admissions_now(here_only=True)
            host, = reading["hosts"]
            self.assertEqual(host["checkpoints"], [record])
            found = {holder["identity"]: holder for holder in host["holders"]}
            self.assertEqual(found["a"]["checkpoint"], {**record, "host": host["name"]})
            self.assertNotIn("checkpoint", found["b"])
            self.assertNotIn("checkpoint", found["dead"])
            self.assertIn(f"      checkpoint: Alpha at 0123456789ab, build (2 of 4) from {host['name']}",
                          resources.now_lines(reading))
        reading = resources.admissions_now(here_only=True)["hosts"][0]
        self.assertEqual(reading["checkpoints"], [])
        self.assertTrue(all("checkpoint" not in holder for holder in reading["holders"]))

    def test_checkpoint_reading_distinguishes_startup_unreadable_and_stale_records(self):
        directory = self.directory / "checkpoints"
        directory.mkdir(parents=True)
        file = directory / "Alpha.json"
        record = {"project": "Alpha", "commit": "0123456789abcdef", "lane": None, "index": 0, "count": 4}
        file.write_text(json.dumps(record))
        self.assertEqual(self.read()["checkpoints"], [])  # no lock file
        with (directory / "Alpha.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            reading = self.read()
            self.assertEqual((reading["state"], reading["checkpoints"]), ("observed", [{**record, "runsIn": None}]))
            file.write_text(json.dumps({**record, "lane": "build", "index": 1}))
            self.assertEqual(self.read()["checkpoints"][0]["lane"], "build")  # older writer without a worktree
            file.write_text("{")
            reading = self.read()
            self.assertEqual((reading["state"], reading["checkpoints"]), ("partial", None))
            self.assertTrue(any("checkpoint record" in reason for reason in reading["reasons"]))
        self.assertEqual(self.read()["checkpoints"], [])  # stale unlocked record

    def test_each_running_holder_says_how_much_cpu_it_and_what_it_started_use(self):
        # What `qk queue doctor` names beside a gate the holder holds back on an idle Mac (vocaro/queuekeeper#89).
        self.holders({"a": self.owner("Alpha"), "dead": self.owner("Gone", start="Thu Jan  1 00:00:00 1970")},
                     slots=[(0, "a"), (1, "dead")])
        found = {holder["label"]: holder for holder in self.read()["holders"]}
        self.assertIsInstance(found["Alpha"]["cpuPercent"], float)
        self.assertIsNone(found["Gone"]["cpuPercent"])
        # Children count: a holder mostly waits on what it started.
        listing = subprocess.CompletedProcess([], 0, f"{os.getpid()} 1 0.5\n 900001 {os.getpid()} 40,0\n900002 900001 2.5\n"
                                                     "900003 1 99.0\n", "")
        with patch.object(offload_host.subprocess, "run", return_value=listing):
            self.assertEqual(CPU_USE([os.getpid(), 123]), {os.getpid(): 43.0})
        # A ps that cannot say leaves it unknown, and the reading partial, never idle.
        with patch.object(offload_host, "cpu_use", return_value=None):
            reading = self.read()
        self.assertEqual(reading["state"], "partial")
        self.assertIn("ps could not say how much CPU the holders use", reading["reasons"])
        self.assertIsNone({holder["label"]: holder for holder in reading["holders"]}["Alpha"]["cpuPercent"])

    def test_exclusive_work_holds_every_slot_and_the_model(self):
        self.holders({"x": self.owner("Bench", exclusive=True, model=True)}, slots=[(0, "x")])
        reading = self.read()
        (holder,) = reading["holders"]
        self.assertEqual((holder["slots"], holder["model"]), ([0, 1, 2], True))
        self.assertEqual((reading["model"], reading["unattributed"]), ({"holder": "x"}, []))

    def test_dead_entries_are_reclaimable_and_uncertain_ones_whose_owner_runs_stranded_not_free(self):
        self.holders({"dead": self.owner("Gone", start="Thu Jan  1 00:00:00 1970"),
                      "unsure": self.owner("Unsure", uncertain=True), "old": self.owner("Old")},
                     slots=[(0, "dead"), (1, "unsure"), (2, "released")])
        reading = self.read()
        found = {holder["label"]: holder for holder in reading["holders"]}
        # Admission frees a holder whose process has ended at its next attempt (vocaro/queuekeeper#23).
        self.assertEqual((found["Gone"]["process"], found["Gone"]["stranded"], found["Gone"]["reclaimable"]),
                         ("ended", False, True))
        self.assertEqual((found["Unsure"]["stranded"], found["Unsure"]["reclaimable"]), (True, False))
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
        self.assertEqual((reading["state"], reading["holders"][0]["process"], reading["holders"][0]["stranded"],
                          reading["holders"][0]["reclaimable"]), ("partial", "unknown", False, True))
        self.assertIn("neither ps nor the kernel could say", reading["reasons"][0])

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
        self.assertNotIn("lane", found["Asking"])

    def test_waiter_lane_requires_a_fresh_verified_sample_with_a_whole_name(self):
        at = 200.0
        for name, sampled, process, expected in [("ios-tests", at, "running", "ios-tests"),
                                                  ("ios-tests", at - 31, "running", None),
                                                  ("ios-tests", at, "unknown", None),
                                                  ("x" * 161, at, "running", None),
                                                  ("other/path", at, "running", None)]:
            with self.subTest(name=name, sampled=sampled, process=process):
                record = {"lane": name, "sampledAt": sampled}
                result = offload_host.waiter_reading({}, record, "ordinary", None, process, at, 15)
                self.assertEqual(result.get("lane"), expected)

    def test_a_place_said_to_wait_still_reads_as_away_and_holding_since_it_last_asked(self):
        """vocaro/queuekeeper#160: a place its caller keeps holding during an offer (resources.Place.still_waiting) is
        still a place kept while away: `seen` is when the caller last said so, `asked` when the request last asked."""
        self.directory.mkdir(parents=True, exist_ok=True)
        (self.directory / resources.QUEUE).write_text(json.dumps({"vocaroEngineering": 1, "waiters": [
            {"identity": "w", "pid": os.getpid(), "start": self.start, "label": "Kept gate", "queuedAt": time.time() - 900,
             "seen": time.time() - 5, "asked": time.time() - 250, "wholeHost": True}]}))
        waiter, = self.read()["waiters"]
        self.assertEqual((waiter["away"], waiter["holdsBack"]), (True, True))
        self.assertAlmostEqual(waiter["lastAskedSeconds"], 250, delta=5)

    def test_a_farm_queue_place_names_its_sender_beside_its_observation_once(self):
        identity = "f" * 32
        place_holder.place_held(identity, {"identity": identity, "pid": os.getpid(), "start": self.start,
                                           "label": "Farm gate", "queuedAt": time.time() - 15,
                                           "wholeHost": True, "heldFor": "studio"})
        place, = self.read()["waiters"]
        self.assertEqual((place["heldFor"], place["reason"], place["holdsBack"]), ("studio", None, True))

        folder = self.directory / resources.WAITS
        folder.mkdir()
        (folder / "observation.json").write_text(json.dumps({
            "schemaVersion": 1, "identity": identity, "pid": os.getpid(), "start": self.start,
            "request": "observed", "reason": "all guaranteed slots are busy", "sampledAt": time.time(),
        }))
        files = tree(self.temp.name)
        words = []
        self.assertEqual(resources.now_main(["--here"], out=words.append), 0)
        self.assertEqual(sum("Farm gate:" in line for line in words), 1)
        self.assertIn("place held for work sent by studio", "\n".join(words))
        output = []
        self.assertEqual(resources.now_main(["--here", "--json"], out=output.append), 0)
        host, = json.loads(output[0])["hosts"]
        waiter, = host["waiters"]
        self.assertEqual((waiter["heldFor"], waiter["kind"], waiter["position"]), ("studio", "wholeHost", 1))
        self.assertEqual((waiter["process"], waiter["away"], waiter["holdsBack"]), ("running", True, True))
        self.assertEqual(waiter["reason"], "all guaranteed slots are busy")
        self.assertEqual(host["holders"], [])
        self.assertEqual(tree(self.temp.name), files)

    def test_a_farm_place_requires_a_live_keeper_and_a_recent_renewal(self):
        self.directory.mkdir()
        queue = self.directory / resources.QUEUE
        now = time.time()
        for reason, changes, listed in [
            ("expired", {"seen": now - 500}, {os.getpid(): self.start}),
            ("no renewal", {"seen": None}, {os.getpid(): self.start}),
            ("ended", {}, {}),
            ("PID reused", {}, {os.getpid(): "another start"}),
            ("unknown", {}, None),
        ]:
            with self.subTest(reason=reason):
                queue.write_text(json.dumps({"waiters": [{
                    "identity": "f" * 32, "pid": os.getpid(), "start": self.start, "label": "Farm gate",
                    "queuedAt": now - 500, "seen": now - 5, "wholeHost": True, "heldFor": "studio", **changes,
                }]}))
                with patch.object(offload_host, "census", return_value=listed), \
                        patch.dict(os.environ, {"HEAVY_PLACE_SECONDS": "180"}):
                    reading = offload_host.admissions_now(at=now)
                self.assertEqual(reading["waiters"], [])
                self.assertEqual(reading["state"], "partial" if listed is None else "observed")

    def observation(self, identity, name, **changes):
        """A live progress observation a holder's leaf publishes under its admission (lane_progress.destinations)."""
        folder = Path(str(self.guard) + ".progress") / identity
        folder.mkdir(parents=True, exist_ok=True)
        now = time.time()
        (folder / f"{name}.json").write_text(json.dumps({
            "run": name, "phase": "running", "observedAt": now, "startedAt": now - 900, "reporter": os.getpid(),
            "reporterStart": self.start, "lane": name, "host": "build-host", "state": "observing", "action": "report",
            "quietSeconds": 120.0, "thresholdSeconds": 600, "maximumQuietSeconds": 150.0, "lastProgressAt": now - 120,
            "samples": 900, "unobservableSamples": 3, "processes": [{"pid": 1, "start": "x" * 200}] * 50, **changes}))

    def test_each_holder_says_what_its_leaves_observe_of_its_progress(self):
        """vocaro/queuekeeper#70: a holder that has stopped making progress is reported by `qk admissions now`, and one
        whose leaves observe nothing says so rather than looking idle or busy."""
        self.holders({"a": self.owner("Alpha gate"), "b": self.owner("Beta unittest"),
                      "c": self.owner("Gone", start="Thu Jan  1 00:00:00 1970")},
                     slots=[(0, "a"), (1, "b"), (2, "c")])
        self.observation("a", "pytest", state="quiet", quietSeconds=700.0, thresholdSeconds=600, lastProgressAt=time.time() - 700)
        self.observation("a", "swift")
        # Neither an ended observation, nor a stale one, nor one whose reporter's PID now runs another process counts.
        self.observation("a", "ended", phase="ended")
        self.observation("a", "stale", observedAt=time.time() - 60)
        self.observation("a", "reused", reporterStart="Thu Jan  1 00:00:00 1970")
        self.observation("c", "orphan")
        before = tree(self.temp.name)
        reading = self.read()
        self.assertEqual(tree(self.temp.name), before)
        found = {holder["label"]: holder for holder in reading["holders"]}
        self.assertEqual([record["lane"] for record in found["Alpha gate"]["progress"]], ["pytest", "swift"])
        quiet = found["Alpha gate"]["progress"][0]
        self.assertEqual((quiet["state"], quiet["quietSeconds"], quiet["thresholdSeconds"]), ("quiet", 700.0, 600))
        self.assertAlmostEqual(quiet["lastProgressSeconds"], 700, delta=5)
        # Only the observation's summary travels: not the processes it names, nor its reporter.
        self.assertFalse({"processes", "reporter", "reporterStart"} & set(quiet))
        self.assertEqual(found["Beta unittest"]["progress"], [])
        self.assertIsNone(found["Gone"]["progress"])
        lines = resources.now_lines({"hosts": [{"name": "this Mac", **reading}]})
        self.assertIn("      progress: pytest on build-host: quiet 700s, past its 600s bound (report); last progress 700s "
                      "ago; longest quiet 150s; 900 samples, 3 unobservable", lines)
        self.assertIn("      progress: swift on build-host: quiet 120s of its 600s bound; last progress 120s ago; "
                      "longest quiet 150s; 900 samples, 3 unobservable", lines)
        self.assertIn("      progress: not observed (only a lane that declares `stall` publishes it)", lines)
        self.assertEqual(sum("progress:" in line for line in lines), 3)
        # A ps that cannot say leaves every holder's progress unknown, never quiet.
        with patch.object(offload_host, "census", return_value=None):
            unknown = self.read()
        self.assertTrue(all(holder["progress"] is None for holder in unknown["holders"]))

    def test_an_unverifiable_leaf_leaves_a_live_holders_progress_unknown(self):
        self.holders({"a": self.owner("Alpha gate")}, slots=[(0, "a")])
        reporter = os.getpid() + 1
        self.observation("a", "unverified", reporter=reporter, reporterStart="recorded start",
                         state="quiet", quietSeconds=700.0)
        owner = {os.getpid(): " ".join(self.start.split())}
        unknown = {**owner, reporter: None}
        empty = "      progress: not observed (only a lane that declares `stall` publishes it)"
        for census, state, progress, line in (
                (unknown, "partial", None, "      progress: unknown"),
                (owner, "observed", [], empty),
                ({**owner, reporter: "different start"}, "observed", [], empty)):
            with self.subTest(census=census), patch.object(offload_host, "census", return_value=census):
                reading = self.read()
            holder, = reading["holders"]
            self.assertEqual((reading["state"], holder["process"], holder["progress"]), (state, "running", progress))
            self.assertEqual(resources.progress_lines(holder), [line])

        self.observation("a", "verified")
        with patch.object(offload_host, "census", return_value=unknown):
            reading = self.read()
        self.assertEqual(reading["state"], "partial")
        holder, = reading["holders"]
        self.assertEqual([record["lane"] for record in holder["progress"]], ["verified"])

    def test_a_reading_too_large_is_cut_and_partial(self):
        reading = {"state": "observed", "reasons": [], "waiters": [{"label": "x" * 1000}] * 2000, "holders": [], "slots": []}
        cut = offload_host.bounded(reading)
        self.assertLessEqual(len(json.dumps(cut)), offload_host.NOW_BYTES)
        self.assertEqual(cut["state"], "partial")


class FailedCensusTests(Namespace):
    """A ps that fails or times out is retried PID by PID with the kernel, so that it hides no holder whose state the
    kernel can give, and a holder whose start no one can read is unknown; admission frees both an ended and an unknown
    holder at its next attempt (vocaro/queuekeeper#23)."""

    def setUp(self):
        super().setUp()
        if sys.platform != "darwin": self.skipTest("the kernel's record of a process is read on macOS")
        gone = subprocess.Popen(["true"])
        gone.wait()
        self.gone = gone.pid
        self.holders({"live": self.owner("Live"), "gone": self.owner("Gone", pid=self.gone),
                      "reused": self.owner("Reused", start="Thu Jan  1 00:00:00 1970")},
                     slots=[(0, "live"), (1, "gone"), (2, "reused")])

    def failing(self, error):
        """subprocess.run, but the census's `ps -p` raises error."""
        real = subprocess.run
        def run(command, *args, **kwargs):
            if command[:2] == ["ps", "-p"]: raise error
            return real(command, *args, **kwargs)
        return patch.object(offload_host.subprocess, "run", side_effect=run)

    def test_a_ps_that_fails_or_times_out_hides_no_holder_the_kernel_can_place(self):
        for error in [subprocess.TimeoutExpired(["ps"], 5), OSError("ps could not start")]:
            with self.subTest(type(error).__name__), self.failing(error):
                reading = self.read()
            found = {holder["label"]: holder for holder in reading["holders"]}
            self.assertEqual((reading["state"], reading["reasons"]), ("observed", []))
            self.assertEqual((found["Live"]["process"], found["Live"]["reclaimable"]), ("running", False))
            # A PID gone and a PID that now runs another process are both ended, which admission frees.
            self.assertEqual((found["Gone"]["process"], found["Gone"]["reclaimable"]), ("ended", True))
            self.assertEqual((found["Reused"]["process"], found["Reused"]["reclaimable"]), ("ended", True))

    def test_a_live_pid_whose_start_cannot_be_read_is_unknown_alone(self):
        real = offload_host.kernel_start
        def kernel(pid):
            if pid == os.getpid(): raise OSError("sysctl KERN_PROC_PID: the kernel would not say")
            return real(pid)
        with self.failing(subprocess.TimeoutExpired(["ps"], 5)), \
                patch.object(offload_host, "kernel_start", side_effect=kernel):
            reading = self.read()
        found = {holder["label"]: holder for holder in reading["holders"]}
        # Its PID runs, so the holder may be alive and slow to confirm: it is unknown, not ended, and admission frees
        # it all the same, since uncertainty about a holder never keeps a slot held.
        self.assertEqual((found["Live"]["process"], found["Live"]["reclaimable"]), ("unknown", True))
        self.assertEqual((found["Reused"]["process"], found["Reused"]["reclaimable"]), ("unknown", True))
        self.assertEqual((found["Gone"]["process"], found["Gone"]["reclaimable"]), ("ended", True))
        lines = resources.now_lines({"hosts": [{"name": "this Mac", **reading}]})
        self.assertTrue(any(line.startswith("    Live: ") and line.endswith(
            "its process could not be checked: admission frees its slots at its next attempt") for line in lines), lines)
        self.assertEqual(reading["state"], "partial")
        self.assertIn(f"the kernel could not say whether PID {os.getpid()} still runs", reading["reasons"][0])
        # When no PID can be placed at all, the whole census is unknown, as before.
        with self.failing(OSError("no ps")), patch.object(offload_host, "kernel_start", side_effect=OSError("no")):
            self.assertIsNone(offload_host.census([os.getpid()]))

    def test_the_kernels_start_reads_as_ps_reads_it(self):
        self.assertEqual(offload_host.kernel_start(os.getpid()), " ".join(self.start.split()))
        with self.assertRaises(LookupError): offload_host.kernel_start(self.gone)

    def test_recovery_reads_a_reused_pid_as_ended_when_ps_cannot_say(self):
        real = subprocess.run
        def run(command, *args, **kwargs):
            if command[:2] == ["ps", "-p"]: raise subprocess.TimeoutExpired(command, resources.ENDED_PS_SECONDS)
            return real(command, *args, **kwargs)
        live, reused = self.owner("Live"), self.owner("Reused", start="Thu Jan  1 00:00:00 1970")
        with patch.object(resources.subprocess, "run", side_effect=run):
            gone = self.owner("Gone", pid=self.gone)
            self.assertEqual((resources.ended(live), resources.ended(reused), resources.ended(gone)), (False, True, True))
            # Neither ps nor the kernel can say: attended recovery leaves the owner running; admission frees it
            # (liveness reads it unknown, ReclamationTests in test_core).
            with patch.object(resources, "kernel_start", side_effect=OSError("no")):
                self.assertFalse(resources.ended(reused))
                self.assertEqual(resources.liveness(reused)[0], "unknown")


class ReclaimedTests(Namespace):
    """`qk admissions now` and `qk queue doctor` show a holder admission reclaimed as reclaimed, with when and why, and
    its slot as free, never as held (vocaro/queuekeeper#23)."""

    def reclaimed(self, minutes=2, **changes):
        """A `reclaimed` line in the namespace's admission log, as resources.free_holder writes it, minutes ago."""
        when = datetime.fromtimestamp(time.time() - minutes * 60, timezone.utc).isoformat(timespec="seconds")
        line = {"time": when, "event": "reclaimed", "identity": "gone", "pid": 18597, "kind": "heavy",
                "label": "EPUBLib bash -c TEST_FILE=Tests/EPUBViewingTests", "uncertain": False, "process": "ended",
                "why": "its PID 18597 is gone", "slots": [1], "model": False, "replaced": [],
                "signaled": [{"pid": 18610, "start": "Thu Nov 12 15:46:00 2026", "signals": ["SIGTERM", "SIGKILL"]}],
                "survived": [], **changes}
        self.directory.mkdir(parents=True, exist_ok=True)
        with (self.directory / resources.LOG).open("a") as stream: stream.write(json.dumps(line) + "\n")

    def test_a_reclaimed_holder_is_shown_reclaimed_with_when_and_why_and_its_slot_free(self):
        self.holders({"a": self.owner("Alpha")}, slots=[(0, "a"), (1, "gone")])
        # Too old to show, and a line of another kind, before and after it in the log: neither is listed.
        self.reclaimed(minutes=120, identity="older")
        self.reclaimed()
        with (self.directory / resources.LOG).open("a") as stream:
            stream.write(json.dumps({"time": datetime.now(timezone.utc).isoformat(), "event": "admitted",
                                     "identity": "a"}) + "\n")
        before = tree(self.temp.name)
        reading = self.read()
        self.assertEqual(tree(self.temp.name), before)
        self.assertEqual(reading["state"], "observed")
        (item,) = reading["reclaimed"]
        self.assertEqual((item["identity"], item["pid"], item["slots"], item["process"], item["why"]),
                         ("gone", 18597, [1], "ended", "its PID 18597 is gone"))
        self.assertAlmostEqual(item["reclaimedSeconds"], 120, delta=5)
        self.assertEqual(item["signaled"], [{"pid": 18610, "signals": ["SIGTERM", "SIGKILL"]}])
        # Its slot is free and says so: neither held nor unattributed.
        slot = {each["slot"]: each for each in reading["slots"]}[1]
        self.assertEqual((slot["holder"], slot["reclaimed"]), (None, "gone"))
        self.assertEqual(reading["unattributed"], [])
        self.assertEqual([holder["label"] for holder in reading["holders"]], ["Alpha"])
        lines = resources.now_lines({"hosts": [{"name": "this Mac", **reading}]})
        self.assertIn("  slots: 1 of 2 guaranteed and 0 of 1 beyond them held; the model free", lines)
        self.assertIn("  reclaimed:", lines)
        (said,) = [line for line in lines if line.startswith("    EPUBLib bash")]
        self.assertRegex(said, r"^    EPUBLib bash -c TEST_FILE=Tests/EPUBViewingTests: heavy, slot 1 freed, PID 18597, "
                               r"reclaimed 2m0\ds ago, at \d\d:\d\d:\d\d: its process had ended \(its PID 18597 is gone\); "
                               r"signaled PID 18610 \(SIGTERM then SIGKILL\)$")
        # The JSON says the same.
        output = StringIO()
        with patch.object(resources, "admissions_now", return_value={"schemaVersion": 1, "hosts": [
                {"name": "this Mac", "role": "here", **reading}]}):
            resources.now_main(["--json", "--here"], out=output.write)
        self.assertEqual(json.loads(output.getvalue())["hosts"][0]["reclaimed"][0]["why"], "its PID 18597 is gone")

    def test_an_unknown_holder_reclaimed_says_nothing_was_signaled_and_why(self):
        self.reclaimed(identity="unsure", process="unknown", why="ps took more than 5s, and the kernel could not say: no",
                       signaled=[], replaced=["slot1"],
                       unsignaled="its start could not be read, so its PID is not proven its holder's: neither it nor "
                                  "anything under it was signaled")
        lines = resources.now_lines({"hosts": [{"name": "this Mac", **self.read()}]})
        (said,) = [line for line in lines if line.startswith("    EPUBLib bash")]
        self.assertIn("its process could not be checked (ps took more than 5s, and the kernel could not say: no); its "
                      "start could not be read, so its PID is not proven its holder's", said)

    def test_queue_doctor_names_a_reclaimed_holder_apart_from_the_holders(self):
        from queuekeeper import merge_queue
        self.holders({"a": self.owner("Alpha")}, slots=[(0, "a"), (1, "gone")])
        self.reclaimed()
        holders, why, reclaimed = merge_queue.holders_on(None)
        self.assertEqual(([holder["label"] for holder in holders], why), (["Alpha"], None))
        self.assertRegex(merge_queue.reclaimed_words(reclaimed[0]),
                         r"^EPUBLib bash -c TEST_FILE=Tests/EPUBViewingTests \(PID 18597, slot 1, reclaimed 2m0\ds ago: "
                         r"its process had ended, its PID 18597 is gone\)$")


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
        began = time.monotonic()
        with contextlib.redirect_stderr(said):
            thread, outcome = self.waiting(admission)
            (waiter,) = self.read()["waiters"]
            for fd in held: os.close(fd)
            thread.join(stretched(30))
        took = time.monotonic() - began
        self.assertTrue(outcome.get("admitted"), outcome)
        self.assertEqual((waiter["label"], waiter["kind"], waiter["refusal"], waiter["process"], waiter["position"]),
                         ("Delta unittest", "ordinary", "slots", "running", None))
        # The very words the waiter printed, and how long it had waited when it printed them: no longer than this
        # test watched it. On an idle Mac that is 0s; on a loaded one its first refusal takes seconds (#85).
        printed = re.findall(r"^heavy: waiting \((\d+)s\): (.*)$", said.getvalue(), re.MULTILINE)
        self.assertEqual([reason for _seconds, reason in printed], [waiter["reason"]], said.getvalue())
        self.assertLessEqual(int(printed[0][0]), took)
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
        environment = {**os.environ, "PYTHONPATH": host_isolation.pythonpath()}
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
        (record,) = self.records()
        # Its queue record and its own stay behind, and neither reads as waiting.
        self.assertEqual(self.read()["waiters"], [])
        # The crashed waiter swept when it first wrote; the next sweep is a minute on, or now without its marker.
        # A sweep that nothing can answer, neither ps nor the kernel, leaves the record, since nothing proves its
        # process ended; the waiter that swept removes its own record either way.
        marker = self.directory / resources.WAITS / ".sweep"
        marker.unlink()
        with patch.object(offload_host, "census", return_value=None), contextlib.redirect_stderr(StringIO()), \
                self.assertRaises(TimeoutError):
            with resources.Admission("Unanswered", timeout=0): pass
        self.assertEqual(self.records(), [record])
        # On a loaded Mac the sweep's ps can outlast census's 5 s, which left the record until the census asked the
        # kernel instead (vocaro/queuekeeper#16, #23). Its ps times out here every time, so the sweep is the loaded
        # one whatever this Mac's load; the kernel proves the crashed waiter's PID gone.
        marker.unlink()
        real = subprocess.run
        def slow(command, *args, **kwargs):
            # Only the census's ps: the waiter reads its own start with another, which must answer for it to record.
            if command[:2] == ["ps", "-p"] and "pid=,lstart=" in command:
                raise subprocess.TimeoutExpired(command, 5)
            return real(command, *args, **kwargs)
        with patch.object(offload_host.subprocess, "run", side_effect=slow), contextlib.redirect_stderr(StringIO()), \
                self.assertRaises(TimeoutError):
            with resources.Admission("Later", timeout=0): pass
        self.assertEqual(self.records(), [], f"the crashed waiter's record is {record}")

    def test_a_record_that_cannot_be_written_changes_no_admission(self):
        held = self.hold_slots()
        self.directory.joinpath(resources.WAITS).write_text("a file where the folder goes")
        said = StringIO()
        with contextlib.redirect_stderr(said):
            with self.assertRaisesRegex(TimeoutError, "so is every slot up to HEAVY_SLOTS_MAX=3"):
                with resources.Admission("Theta", timeout=0): pass
            for fd in held: os.close(fd)
            with resources.Admission("Theta", timeout=0): pass
        self.assertEqual(said.getvalue().count("was not recorded for `qk admissions now`"), 1)


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

    def test_the_coding_agent_is_the_session_its_environment_names(self):
        self.assertEqual(resources.coding_agent({"CLAUDE_CODE_SESSION_ID": "s", "CLAUDE_CODE_HOST_SESSION_ID": "local_h"}),
                         {"agent": "claude", "agentSession": "local_h"})
        self.assertEqual(resources.coding_agent({"CODEX_THREAD_ID": "t", "CLAUDE_CODE_SESSION_ID": ""}),
                         {"agent": "codex", "agentSession": "t"})
        self.assertEqual(resources.coding_agent({"PATH": "/usr/bin"}), {})

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

    def test_a_lane_sent_to_a_farm_mac_names_its_checkpoint_on_the_sending_mac(self):
        checkpoint = {"project": "Alpha", "commit": "0123456789abcdef", "runsIn": "/tmp/eng-checkpoint-a/Alpha",
                      "lane": "build", "index": 2, "count": 4}
        holder = {"origin": {"worktree": checkpoint["runsIn"]}, "process": "running"}
        entries = {"mini": self.reading(holders=[holder]), "studio": self.reading(checkpoints=[checkpoint])}
        macs = [farm_fixture.mac("mini"), farm_fixture.mac("studio")]
        found = resources.admissions_now(read_here=self.reading, read_there=lambda host, _: entries[host],
                                         macs=lambda: macs)
        self.assertEqual(found["hosts"][1]["holders"][0]["checkpoint"], {**checkpoint, "host": "studio"})

    def test_a_starting_checkpoint_makes_a_worktree_path_on_two_macs_ambiguous(self):
        active = {"project": "Alpha", "commit": "0123456789abcdef", "runsIn": "/tmp/eng-checkpoint/Alpha",
                  "lane": "build", "index": 2, "count": 4}
        starting = {**active, "commit": "fedcba9876543210", "lane": None, "index": 0}
        holder = {"origin": {"worktree": active["runsIn"]}, "process": "running"}
        entries = {"mini": self.reading(holders=[holder], checkpoints=[starting]),
                   "studio": self.reading(checkpoints=[active])}
        found = resources.admissions_now(read_here=self.reading, read_there=lambda host, _: entries[host],
                                         macs=lambda: [farm_fixture.mac(name) for name in entries])
        _, mini, studio = found["hosts"]
        self.assertEqual((mini["checkpoints"], studio["checkpoints"]), ([starting], [active]))
        self.assertNotIn("checkpoint", mini["holders"][0])

    def test_a_farm_checkpoint_without_a_slot_is_visible_beside_unknown_and_empty_readings(self):
        checkpoint = {"project": "Alpha", "commit": "0123456789abcdef", "runsIn": "/private/Alpha",
                      "lane": None, "index": 0, "count": 4}
        entries = {"mini": self.reading(checkpoints=[]), "studio": self.reading(checkpoints=[checkpoint])}

        def read(host, budget):
            if host == "away": raise remote.Unreachable("away did not answer", "wake it")
            return entries[host]

        found = resources.admissions_now(read_here=self.reading, read_there=read,
                                         macs=lambda: [farm_fixture.mac(name) for name in ("mini", "studio", "away")])
        _here, mini, studio, away = found["hosts"]
        self.assertEqual((mini["checkpoints"], studio["checkpoints"], away["checkpoints"]), ([], [checkpoint], None))
        lines = resources.now_lines(found)
        self.assertIn("  checkpoints: unknown", lines)  # this Mac's reading lacks the field
        self.assertIn("  checkpoints: none observed", lines)
        self.assertIn("    Alpha at 0123456789ab, before its first lane", lines)
        self.assertTrue(any(line.startswith("away: unknown: away did not answer") for line in lines))
        self.assertNotIn(checkpoint["runsIn"], "\n".join(lines))

    def test_an_unreachable_farm_mac_is_unknown_beside_this_macs_reading(self):
        def unreachable(host, budget):
            self.assertLessEqual(budget, remote.ADMISSIONS_BUDGET)
            raise remote.Unreachable(f"{host} did not answer in {budget:g}s", "wake it")
        found = resources.admissions_now(read_here=self.reading, read_there=unreachable, macs=lambda: [farm_fixture.mac("build-host")])
        here, there = found["hosts"]
        self.assertEqual((here["state"], here["holders"]), ("observed", []))
        self.assertEqual((there["state"], there["holders"], there["waiters"]), ("unknown", None, None))
        self.assertIn("build-host did not answer", there["reasons"][0])
        lines = []
        with patch.dict(os.environ, {"HEAVY_SLOT_DIR": "", "QUEUEKEEPER_GATE_LOCK_FILE": ""}):
            status = resources.now_main([], out=lines.append, read=lambda here_only: found)
        self.assertEqual(status, 1)
        self.assertTrue(any(line.startswith("build-host: unknown: build-host did not answer") for line in lines))
        self.assertIn("  holders: none", lines)

    def test_a_timed_out_farm_mac_is_read_concurrently_with_two_macs_of_the_same_model(self):
        macs = [farm_fixture.mac("slow.local", "slow"), farm_fixture.mac("west.local", "west"),
                farm_fixture.mac("east.local", "east")]
        started = threading.Barrier(len(macs), timeout=stretched(5))
        calls = []

        def read(host, budget):
            calls.append((host, budget))
            # A serial implementation breaks the barrier. The slowest fake needs no real timeout.
            started.wait()
            if host == "slow.local":
                raise remote.Unreachable("slow.local did not answer", "wake it")
            return self.reading(mac="Mac17,6")

        report = resources.admissions_now(read_here=self.reading, read_there=read, macs=lambda: macs)
        here, slow, west, east = report["hosts"]
        self.assertCountEqual(calls, [(mac["ssh"], remote.ADMISSIONS_BUDGET) for mac in macs])
        self.assertEqual((here["role"], here["state"]), ("here", "observed"))
        self.assertEqual((slow["role"], slow["state"], slow["holders"], slow["waiters"]),
                         ("slow", "unknown", None, None))
        self.assertIn("slow.local did not answer", slow["reasons"][0])
        self.assertEqual([(reading["role"], reading["mac"], reading["id"], reading["state"])
                          for reading in (west, east)],
                         [(mac["name"], mac["name"], mac["id"], "observed") for mac in macs[1:]])

    def test_no_farm_mac_configured_is_no_failure_and_here_reads_this_mac_alone(self):
        asked = []
        found = resources.admissions_now(read_here=self.reading, read_there=lambda *_: asked.append(1),
                                         macs=lambda: [])
        self.assertEqual(found["hosts"][1]["state"], "notConfigured")
        with patch.dict(os.environ, {"HEAVY_SLOT_DIR": "", "QUEUEKEEPER_GATE_LOCK_FILE": ""}):
            self.assertEqual(resources.now_main(["--json"], out=lambda text: None, read=lambda here_only: found), 0)
        # Several Macs are each read, in the farm's order, by their names in the farm; one away is unknown beside them.
        def read(host, budget):
            if host == "mini": raise remote.Unreachable("mini did not answer", "wake it")
            return self.reading()
        macs = [farm_fixture.mac("studio.local", "studio"), farm_fixture.mac("mini"), farm_fixture.mac("rented")]
        several = resources.admissions_now(read_here=self.reading, read_there=read, macs=lambda: macs)["hosts"]
        self.assertEqual([(one["role"], one["name"], one["state"]) for one in several],
                         [("here", several[0]["name"], "observed"), ("studio", "studio (studio.local)", "observed"),
                          ("mini", "mini", "unknown"), ("rented", "rented", "observed")])
        self.assertEqual([(one["mac"], one["ssh"], one["id"]) for one in several[1:]],
                         [(mac["name"], mac["ssh"], mac["id"]) for mac in macs])
        # One Mac is named as each of several is.
        one = resources.admissions_now(read_here=self.reading, read_there=read, macs=lambda: macs[:1])["hosts"][1]
        self.assertEqual((one["role"], one["name"], one["mac"]), ("studio", "studio (studio.local)", "studio"))
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
