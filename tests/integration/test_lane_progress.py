"""Live observation routing must not change a lane's outcome or revive old work."""
import contextlib
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import host_isolation

from queuekeeper import direct_status, lane_progress, lane_wait, merge_queue, offload_host, progress, remote, resources


class LiveProgress(unittest.TestCase):
    def wait(self, folder):
        path = Path(folder)/"gate.json"
        path.write_text(json.dumps({"progressRun": "a" * 32}))
        return path

    def record(self, **changes):
        return {"run": "run-1", "phase": "running", "observedAt": time.time(), "reporter": os.getpid(),
                "reporterStart": "born", "startedAt": time.time(), "lane": "pytest", "host": "build-host", "quietSeconds": 7.0,
                "thresholdSeconds": 30, "state": "observing", "samples": 12, "unobservableSamples": 2,
                "maximumQuietSeconds": 9, "wait": "unknown", **changes}

    def test_only_fresh_running_exact_reporter_identity_is_visible(self):
        with tempfile.TemporaryDirectory() as folder:
            wait = self.wait(folder)
            live = lane_progress.directory(wait) / "run.json"
            with patch.object(lane_progress, "starts", return_value={os.getpid(): "born"}):
                for changes, expected in (({}, 1), ({"phase": "ended"}, 0), ({"phase": "uncertain"}, 0),
                        ({"observedAt": time.time() - 20}, 0), ({"reporterStart": "reused"}, 0), ({"lane": ["invalid"]}, 0)):
                    lane_progress.write(live, self.record(**changes))
                    self.assertEqual(len(lane_progress.current(wait)), expected, changes)
                lane_progress.write(live, self.record())
                self.assertEqual(lane_progress.current(wait, time.time() + 1), [])
                # An old live run writing fresh bytes cannot become the new gate's observation.
                lane_progress.write(live, self.record(startedAt=time.time() - 10))
                self.assertEqual(lane_progress.current(wait, time.time() - 1), [])
                live.write_text("x" * (lane_progress.MAX_BYTES + 1))
                self.assertEqual(lane_progress.current(wait), [])
                live.unlink()
                os.mkfifo(live)
                self.assertEqual(lane_progress.current(wait), [])
                self.assertEqual(offload_host.progress_state(wait), [])

    def test_old_namespaces_and_flat_leftovers_cannot_starve_the_current_run(self):
        with tempfile.TemporaryDirectory() as folder:
            wait = self.wait(folder)
            base = Path(str(wait) + ".progress")
            old = base/("b" * 32)
            old.mkdir(parents=True)
            for index in range(lane_progress.LIMIT + 20):
                (base/f"old-{index}.json").write_text("{}")
                (old/f"old-{index}.json").write_text(json.dumps(self.record()))
            born = "Tue Nov 10 12:00:00 2026"
            lane_progress.write(lane_progress.directory(wait)/"current.json", self.record(reporterStart=born))
            with patch.object(lane_progress, "starts", return_value={os.getpid(): born}), \
                    patch.object(offload_host.subprocess, "run", return_value=Mock(stdout=f"{os.getpid()} {born}\n", returncode=0)):
                self.assertEqual(len(lane_progress.current(wait)), 1)
                self.assertEqual(len(offload_host.progress_state(wait)), 1)
                # New wait nonce before any publication: nothing from the prior namespace appears.
                wait.write_text(json.dumps({"progressRun": "c" * 32}))
                self.assertEqual(lane_progress.current(wait), [])
                self.assertEqual(offload_host.progress_state(wait), [])
            self.assertTrue((old/"old-0.json").exists())
            for bad in ("../outside", "/tmp", "a" * 33, None):
                wait.write_text(json.dumps({"progressRun": bad}))
                self.assertEqual(lane_progress.current(wait), [])
                self.assertEqual(offload_host.progress_state(wait), [])

    def test_outer_namespace_changes_while_nested_groups_inherit_it(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ), patch.object(lane_wait, "_owned", None):
            os.environ.pop(lane_wait.VARIABLE, None)
            os.environ.pop(lane_progress.VARIABLE, None)
            lane_wait.begin(folder, "gate")
            first = os.environ[lane_progress.VARIABLE]
            lane_wait.begin(folder, "nested")
            self.assertEqual(os.environ[lane_progress.VARIABLE], first)
            lane_wait.running()
            self.assertEqual(lane_progress.directory(os.environ[lane_wait.VARIABLE]).name, first)
            lane_wait.finish()
            lane_wait.begin(folder, "gate")
            self.assertNotEqual(os.environ[lane_progress.VARIABLE], first)
            self.assertEqual(lane_progress.current(os.environ[lane_wait.VARIABLE]), [])
            lane_wait.finish()

    def test_stable_leaf_slots_do_not_accumulate_or_delete_a_newer_run(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
                lane_wait.VARIABLE: str(self.wait(folder)), lane_progress.VARIABLE: "a" * 32}):
            first = lane_progress.destinations("old", "same-evidence-path")
            newer = lane_progress.destinations("new", "same-evidence-path")
            self.assertEqual(first, newer)
            with patch.dict(os.environ, {resources.TOKEN: json.dumps({"guard": str(Path(folder)/"held-guard"),
                    "locks": [{"nonce": "owned"}]}), "QUEUEKEEPER_GATE_LOCK_FILE": str(Path(folder)/"different") } ):
                inherited = lane_progress.destinations("new", "same-evidence-path")
                self.assertTrue(any(str(path).startswith(str(Path(folder)/"held-guard.progress")) for path in inherited))
                self.assertFalse(any("different" in str(path) for path in inherited))
            lane_progress.publish(self.record(run="old", startedAt=1), first)
            lane_progress.publish(self.record(run="new", startedAt=2), newer)
            lane_progress.publish(self.record(run="old", startedAt=1, phase="ended"), first)
            self.assertEqual(lane_progress.read(newer[0])["run"], "new")
            for index in range(lane_progress.LIMIT + 20):
                lane_progress.publish(self.record(run=str(index), startedAt=3+index), newer)
            self.assertEqual(len(list(newer[0].parent.glob("*.json"))), 1)
            lane_progress.publish(self.record(run=str(index), startedAt=3+index, phase="ended"), newer)
            self.assertFalse(newer[0].exists())

    def test_relay_snapshot_keeps_members_distinct_and_removes_completed_members(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {lane_wait.VARIABLE: str(self.wait(folder)), lane_progress.VARIABLE: "a" * 32}), \
                patch.object(lane_progress, "starts", return_value={os.getpid(): "born"}):
            lane_progress.relayed([self.record(), self.record(run="run-2", lane="slow")], "second")
            current = lane_progress.current(os.environ[lane_wait.VARIABLE])
            self.assertEqual({r["run"] for r in current}, {"run-1", "run-2"})
            self.assertTrue(all(r["host"] == "second" for r in current))
            lane_progress.relayed([self.record(run="run-2", lane="slow")], "second")
            self.assertEqual([r["run"] for r in lane_progress.current(os.environ[lane_wait.VARIABLE])], ["run-2"])
            folder = lane_progress.directory(os.environ[lane_wait.VARIABLE])
            lane_progress.write(folder/"other-dispatcher.json", self.record(run="other"))
            lane_progress.relayed([], "second")
            self.assertEqual([r["run"] for r in lane_progress.current(os.environ[lane_wait.VARIABLE])], ["other"])

    def test_restarted_remote_dispatchers_share_one_leaf_slot_and_cannot_clear_its_new_owner(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
                lane_wait.VARIABLE: str(self.wait(folder)), lane_progress.VARIABLE: "a" * 32}):
            began = time.time() - 1000
            for index in range(lane_progress.LIMIT + 20):
                pid = 10000 + index
                with patch.object(lane_progress.os, "getpid", return_value=pid), \
                        patch.object(lane_progress, "start", return_value="born"):
                    lane_progress.relayed([self.record(run=str(index), startedAt=began + index)], "second")
            iterate = Path.iterdir
            new_pid = pid + 1
            with patch.object(lane_progress, "starts", return_value={pid: "born", new_pid: "born"}), \
                    patch.object(Path, "iterdir", lambda folder: iter(sorted(iterate(folder), key=lambda p: p.name))):
                records = lane_progress.current(os.environ[lane_wait.VARIABLE])
                self.assertEqual([record["run"] for record in records], [str(index)])
                self.assertEqual(len(list(lane_progress.directory(os.environ[lane_wait.VARIABLE]).glob("*.json"))), 1)
                # Reconnection can retain the SAME remote work run while changing its collector.
                with patch.object(lane_progress.os, "getpid", return_value=new_pid), \
                        patch.object(lane_progress, "start", return_value="born"):
                    lane_progress.relayed([self.record(run=str(index), startedAt=began + index)], "second")
                with patch.object(lane_progress.os, "getpid", return_value=pid), \
                        patch.object(lane_progress, "start", return_value="born"):
                    lane_progress.relayed([], "second")
                self.assertEqual([r["run"] for r in lane_progress.current(os.environ[lane_wait.VARIABLE])], [str(index)])
                with patch.object(lane_progress.os, "getpid", return_value=new_pid), \
                        patch.object(lane_progress, "start", return_value="born"):
                    lane_progress.relayed([], "second")
                self.assertEqual(lane_progress.current(os.environ[lane_wait.VARIABLE]), [])

    def test_remote_host_relay_filters_pid_reuse_and_ends_with_empty_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            wait = self.wait(folder)
            lane_progress.write(lane_progress.directory(wait)/"run.json", self.record())
            result = Mock(stdout=f"{os.getpid()} born\n", returncode=0)
            # ps lstart has five fields; use its actual format as independent identity input.
            born = "Tue Nov 10 12:00:00 2026"
            lane_progress.write(lane_progress.directory(wait)/"run.json", self.record(reporterStart=born))
            result.stdout = f"{os.getpid()} {born}\n"
            with patch.object(offload_host.subprocess, "run", return_value=result):
                self.assertEqual(len(offload_host.progress_state(wait)), 1)
                result.stdout = f"{os.getpid()} Tue Nov 10 12:00:01 2026\n"
                self.assertEqual(offload_host.progress_state(wait), [])
            done, frames = threading.Event(), []
            done.set()
            with patch.object(offload_host, "emit", side_effect=lambda kind, data: frames.append((kind, json.loads(data)))):
                offload_host.relay_wait(wait, done, True)
            self.assertTrue(all("progress" not in payload for kind, payload in frames))
            self.assertEqual(frames[-1][1]["admitted"], False)

    def test_wire_frames_forward_progress_without_overwriting_the_admission_wait(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {lane_wait.VARIABLE: str(self.wait(folder)), lane_progress.VARIABLE: "a" * 32}), \
                patch.object(lane_progress, "starts", return_value={os.getpid(): "born"}):
            lane_wait.waiting(lane_wait.ADMISSION, "all slots busy")
            frames = remote.Frames(Path(folder), BytesIO(), BytesIO(), [], "build-host")
            frames.take(b"W " + json.dumps({"progress": [self.record()]}).encode() + b"\n")
            self.assertEqual(len(lane_progress.current(os.environ[lane_wait.VARIABLE])), 1)
            self.assertIn("all slots busy", lane_wait.words(lane_wait.current(os.environ[lane_wait.VARIABLE])))
            frames.take(b"W " + json.dumps({"progress": []}).encode() + b"\n")
            self.assertEqual(lane_progress.current(os.environ[lane_wait.VARIABLE]), [])
            self.assertIn("all slots busy", lane_wait.words(lane_wait.current(os.environ[lane_wait.VARIABLE])))

    def test_queue_words_keep_wait_separate_and_expire_completed_or_stale_reports(self):
        now = time.time()
        state = {"gates": {"batch": {"lane": "gate", "began": now - 2, "waiting": "admission", "progress": [self.record(observedAt=now, startedAt=now)]}}}
        said = "\n".join(merge_queue.round_lines(state, now))
        self.assertIn("waiting: admission", said)
        self.assertIn("progress: pytest on build-host", said)
        self.assertIn("wait unknown", said)
        self.assertIn("2 unobservable", said)
        self.assertIn("last progress unknown", said)
        with patch.object(lane_progress.time, "time", return_value=now):
            self.assertIn("last progress 5s ago", lane_progress.words(self.record(lastProgressAt=now-5)))
        self.assertNotIn("progress:", "\n".join(merge_queue.round_lines(state, now + 20)))

    def test_json_queue_status_expires_stale_progress_too(self):
        queue = Mock()
        queue.all.return_value = []
        queue.working.return_value = True
        queue.held.return_value = None
        queue.closed.return_value = None
        queue.left_behind.return_value = None
        now = time.time()
        queue.round_state.return_value = {"gates": {"batch": {"began": now-2,
            "progress": [self.record(startedAt=now), self.record(run="stale", observedAt=now-20)]}}}
        with patch.object(merge_queue, "Queue", return_value=queue), contextlib.redirect_stdout(StringIO()) as output:
            merge_queue.status("unused", as_json=True)
        records = json.loads(output.getvalue())["round"]["gates"]["batch"]["progress"]
        self.assertEqual([record["run"] for record in records], ["run-1"])

    def test_admission_registry_requires_current_owner_and_does_not_read_other_admissions(self):
        with tempfile.TemporaryDirectory() as folder:
            guard = Path(folder)/"guard"
            live = Path(str(guard)+".progress")/"mine"/"run.json"
            lane_progress.write(live, self.record())
            with patch.object(resources, "running", return_value=True), \
                    patch.object(lane_progress, "starts", return_value={os.getpid(): "born"}):
                self.assertEqual(len(lane_progress.blockers(guard, {"mine": {"label": "gate", "pid": os.getpid(), "start": "born"}})), 1)
                self.assertEqual(lane_progress.blockers(guard, {"other": {"label": "gate", "pid": os.getpid(), "start": "born"}}), [])
                self.assertEqual(lane_progress.blockers(guard, {"mine": {"uncertain": True}}), [])
            with patch.object(lane_progress, "starts", return_value={}):
                self.assertEqual(lane_progress.blockers(guard, {"mine": {}}), [])

    def test_monitor_publication_is_shown_to_a_blocked_admission_and_cleared_on_end(self):
        with tempfile.TemporaryDirectory() as folder:
            guard = Path(folder)/"gate.warm-push-guard"
            born = lane_progress.start(os.getpid())
            owner = {"pid": os.getpid(), "start": born, "label": "project gate", "exclusive": False}
            guard.write_text(json.dumps({"vocaroEngineering": 1, "owners": {"owned": owner}}))
            environment = {"QUEUEKEEPER_GATE_LOCK_FILE": str(Path(folder)/"gate"), "HEAVY_SLOT_DIR": str(Path(folder)/"slots"),
                           resources.TOKEN: json.dumps({"locks": [{"nonce": "owned"}]})}
            log = Path(folder)/"pytest.log"
            log.touch()
            with patch.dict(os.environ, environment), patch.object(progress, "cpu", return_value={123: ("born", 0)}):
                monitor = progress.Monitor({"seconds": 30}, log, Path(folder)/"retained.json")
                monitor.observe({123: "born"})
                self.assertEqual(len(lane_progress.blockers(guard, {"owned": owner})), 1)
                os.environ.pop(resources.TOKEN)
                admission = resources.Admission(label="later test", timeout=30)
                admission.waiting, admission.refusal = "all slots busy", "slots"
                with patch.object(admission, "_attempt", side_effect=[False, True]), \
                        patch.object(admission, "_count"), patch.object(resources.time, "sleep"), \
                        contextlib.redirect_stderr(StringIO()) as output:
                    admission.__enter__()
                self.assertIn("heavy: running work: project gate: pytest", output.getvalue())
                self.assertIn("wait unknown", output.getvalue())
                with patch.object(monitor, "write", side_effect=OSError("retained status unavailable")), \
                        contextlib.redirect_stderr(StringIO()): monitor.finish("exit-0")
                self.assertEqual(lane_progress.blockers(guard, {"owned": owner}), [])
                self.assertTrue((Path(folder)/"retained.json").exists())

    def test_direct_status_retries_an_observation_after_an_optional_write_fails(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(lane_progress, "starts", return_value={os.getpid(): "born"}), \
                direct_status.tracking(folder, "check"):
            with patch.object(lane_progress, "write", side_effect=OSError("optional mirror unavailable")):
                direct_status.observe(state="admitted")
            self.assertEqual(direct_status.records(folder, "check")[0]["state"], "starting")
            direct_status.observe(state="admitted")
            self.assertEqual(direct_status.records(folder, "check")[0]["state"], "admitted")

    def remote_status(self, folder, members, frames_in_order):
        """The direct status after one dispatch attempt's frames arrive here in the given order, through the
        writers a dispatch uses: its `offered`, Frames.take, Progress and lane_wait's record."""
        with patch.dict(os.environ, {lane_wait.VARIABLE: str(self.wait(folder))}), \
                patch.object(lane_progress, "starts", return_value={os.getpid(): "born"}), \
                direct_status.tracking(folder, "pair"), contextlib.redirect_stderr(StringIO()):
            progress = remote.Progress("pair", "build-host", members, "its clock", clock=lambda: 0.0)
            frames = remote.Frames(Path(folder), BytesIO(), BytesIO(), (), "build-host", progress)
            direct_status.observe(host="build-host", state="offered", stage=None)
            states = []
            for frame in frames_in_order:
                frames.take(frame)
                found, = direct_status.records(folder, "pair")
                states.append((found["host"], found["state"], found.get("stage")))
            return states

    def test_a_late_admission_frame_keeps_the_verified_progress_it_arrived_after(self):
        admitted = b'W {"kind":null,"admitted":true}\n'
        progress_state = ("build-host", "receiver reported progress")
        for members, reports, stage in (
                (("narrated",), (b"O RUN narrated\n", b'M {"member":"narrated","stage":"Testing","device":"iPad-Pro-11"}\n'),
                 "narrated: Testing: iPad-Pro-11"),
                ((), (b"L Testing: iPad-Pro-11\n",), "Testing")):
            with self.subTest(members=members), tempfile.TemporaryDirectory() as folder:
                # Progress first: the generic admission arriving after it changes neither the state nor the stage.
                late = self.remote_status(folder, members, (*reports, admitted, admitted))
                self.assertEqual(late[-3:], [(*progress_state, stage)] * 3)
                self.assertNotIn("(DEF)", json.dumps(late))
            with self.subTest(members=members, order="admission first"), tempfile.TemporaryDirectory() as folder:
                prompt = self.remote_status(folder, members, (admitted, *reports))
                self.assertEqual(prompt[0], ("build-host", "receiver reported admission", None))
                self.assertEqual(prompt[-1], (*progress_state, stage))

    def test_explicit_waits_and_fresh_dispatches_still_replace_verified_progress(self):
        stage = b"L Testing: iPad-Pro-11\n"
        with tempfile.TemporaryDirectory() as folder:
            states = self.remote_status(folder, (), (stage, b'W {"kind":"screen","reason":"locked"}\n',
                                                     b'W {"kind":null,"admitted":true}\n'))
            self.assertEqual([state for _, state, _ in states], ["receiver reported progress",
                                                                 "receiver reported a wait for an unlocked screen",
                                                                 "receiver reported admission"])
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(lane_progress, "starts", return_value={os.getpid(): "born"}), \
                direct_status.tracking(folder, "pair"):
            direct_status.observe(host="build-host", state=direct_status.PROGRESS, stage="Testing: iPad-Pro-11")
            # A fresh dispatch attempt reports its own state, and its admission is no longer masked.
            direct_status.observe(host="build-host", state="offered", stage=None)
            direct_status.observe(host="build-host", state="admitted")
            self.assertEqual(direct_status.records(folder, "pair")[0]["state"], "admitted")

    def test_optional_publication_failure_and_ended_cleanup_preserve_retained_status(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"live.json"
            lane_progress.publish(self.record(), [path])
            self.assertTrue(path.exists())
            path.write_text('["malformed", "prior record"]')
            lane_progress.publish(self.record(), [path])
            self.assertEqual(lane_progress.read(path)["run"], "run-1")
            with patch.object(lane_progress, "read", return_value=None):
                lane_progress.publish(self.record(phase="ended"), [path])
            self.assertTrue(path.exists(), "an unreadable slot cannot prove the ending publisher owns it")
            lane_progress.publish(self.record(phase="ended"), [path])
            self.assertFalse(path.exists())
            with patch.object(Path, "mkdir", side_effect=OSError("denied")):
                lane_progress.publish(self.record(), [path])


class Coverage(unittest.TestCase):
    def test_unobserved_gaps_never_add_to_healthy_coverage(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=True):
            log = Path(folder)/"run.log"
            log.touch()
            monitor = progress.Monitor({"seconds": 100}, log, Path(folder)/"status.json")
            readings = {123: ("born", 0)}
            for at, available in ((0, True), (1, True), (6, True), (7, True), (8, True),
                                  (9, False), (10, True), (11, True)):
                with patch.object(progress.time, "monotonic", return_value=at), \
                        patch.object(progress.time, "time", return_value=at), \
                        patch.object(progress, "cpu", return_value=readings if available else None):
                    monitor.observe({123: "born"})
            self.assertEqual(monitor.last["samples"], 8)
            self.assertEqual(monitor.last["unobservableSamples"], 2)
            self.assertEqual(monitor.last["observedSeconds"], 3)
            self.assertEqual(monitor.last["maximumObservationGapSeconds"], 5)
            self.assertEqual(json.loads(monitor.status.read_text())["samplingGapSamples"], 1)
            self.assertNotIn("samplingGapSamples", monitor.last)
            monitor.finish("exit-0")
            self.assertEqual(json.loads(monitor.status.read_text())["samplingGapSamples"], 1)

    def test_failed_publication_is_not_healthy_observed_coverage(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=True):
            log = Path(folder)/"run.log"
            log.touch()
            monitor = progress.Monitor({"seconds": 100}, log, Path(folder)/"status.json")
            for at in range(3):
                with patch.object(progress.time, "monotonic", return_value=at), \
                        patch.object(progress.time, "time", return_value=at), \
                        patch.object(progress, "cpu", return_value={123: ("born", 0)}):
                    if at == 1:
                        with patch.object(monitor, "write", side_effect=OSError("denied")), \
                                contextlib.redirect_stderr(StringIO()): monitor.observe({123: "born"})
                    else: monitor.observe({123: "born"})
            self.assertEqual(monitor.last["observedSeconds"], 0)
            self.assertEqual(monitor.last["unobservableSamples"], 1)
            self.assertEqual(json.loads(monitor.status.read_text())["samplingGapSamples"], 0)

    def test_wall_clock_gap_is_visible_when_monotonic_time_excludes_sleep(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=True):
            log = Path(folder)/"run.log"
            log.touch()
            monitor = progress.Monitor({"seconds": 100}, log, Path(folder)/"status.json")
            for now, wall in ((0, 0), (1, 1), (2, 50)):
                with patch.object(progress.time, "monotonic", return_value=now), \
                        patch.object(progress.time, "time", return_value=wall), \
                        patch.object(progress, "cpu", return_value={123: ("born", 0)}): monitor.observe({123: "born"})
            self.assertEqual(monitor.last["maximumObservationGapSeconds"], 49)
            self.assertEqual(monitor.last["observedSeconds"], 1)
            self.assertEqual(monitor.last["unobservableSamples"], 1)
            self.assertEqual(json.loads(monitor.status.read_text())["samplingGapSamples"], 1)
