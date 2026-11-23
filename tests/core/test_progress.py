import contextlib
from io import StringIO
import json
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import host_isolation
from host_isolation import stretched
from queuekeeper import cli, config, lane_progress, lane_results, parallel, processes, progress, resources


class QuietTests(unittest.TestCase):
    def test_only_consecutive_observed_quiet_time_counts(self):
        quiet = progress.Quiet()
        for now in range(5): self.assertEqual(quiet.sample(now, now, "same"), now)
        self.assertIsNone(quiet.sample(5, 5, None))
        self.assertEqual(quiet.sample(6, 6, "same"), 0)
        self.assertEqual(quiet.sample(7, 7, "same"), 1)
        self.assertEqual(quiet.sample(8, 8, "new output"), 0)

    def test_host_sleep_delayed_observation_and_clock_changes_reset_the_interval(self):
        for now, wall in ((2, 50), (50, 50), (2, -1), (-1, 2)):
            with self.subTest(now=now, wall=wall):
                quiet = progress.Quiet()
                quiet.sample(0, 0, "same")
                quiet.sample(1, 1, "same")
                self.assertIsNone(quiet.sample(now, wall, "same"))
                self.assertEqual(quiet.sample(now + 1, wall + 1, "same"), 0)

    def test_cpu_only_output_only_process_exit_and_pid_reuse_are_progress(self):
        quiet = progress.Quiet()
        states = [({1: ("born", 0)}, (2, 3, 0)), ({1: ("born", 1)}, (2, 3, 0)),
                  ({1: ("born", 1)}, (2, 3, 8)), ({1: ("reused", 0)}, (2, 3, 8)),
                  ({2: ("new", 0)}, (2, 3, 8))]
        for now, state in enumerate(states): self.assertEqual(quiet.sample(now, now, state), 0)


class CpuTests(unittest.TestCase):
    def test_cpu_reads_exact_pid_and_start_and_supported_clock_forms(self):
        birth = "Tue Nov 10 12:30:00 2026"
        for clock, seconds in (("00:01.25", 1.25), ("01:02:03", 3723), ("2-01:02:03", 176523)):
            result = subprocess.CompletedProcess([], 0, f" 123 {birth} {clock}\n", "")
            with self.subTest(clock=clock), patch.object(progress.subprocess, "run", return_value=result):
                self.assertEqual(progress.cpu({123: birth}), {123: (birth, seconds)})
                self.assertIsNone(progress.cpu({123: "another birth"}))
                self.assertIsNone(progress.cpu({123: birth, 124: birth}))

    def test_missing_malformed_or_timed_out_cpu_is_unobservable(self):
        for value in (subprocess.CompletedProcess([], 1, "", "denied"),
                      subprocess.CompletedProcess([], 0, "malformed", ""),
                      subprocess.TimeoutExpired("ps", 1), OSError("missing")):
            options = {"side_effect": value} if isinstance(value, Exception) else {"return_value": value}
            with self.subTest(value=value), patch.object(progress.subprocess, "run", **options):
                self.assertIsNone(progress.cpu({123: "birth"}))
        self.assertIsNone(progress.cpu({}))

    def test_unavailable_cpu_distinguishes_missing_processes_from_bad_listings(self):
        birth = "Tue Nov 10 12:30:00 2026"
        row = f"123 {birth} 00:01.25\n"
        cases = (
            ({}, None, "no-identities"),
            ({123: birth}, subprocess.CompletedProcess([], 1, "", ""), "missing-identity"),
            ({123: birth, 124: birth}, subprocess.CompletedProcess([], 0, row, ""), "missing-identity"),
            ({123: "old birth"}, subprocess.CompletedProcess([], 0, row, ""), "changed-identity"),
            ({123: birth}, subprocess.CompletedProcess([], 0, row.replace("00:01.25", "bad"), ""), "malformed-output"),
            ({123: birth}, subprocess.CompletedProcess([], 1, "", "denied"), "process-list-failed"),
            ({123: birth}, OSError("ps unavailable"), "process-list-failed"),
            ({123: birth}, subprocess.TimeoutExpired("ps", 1), "process-list-timeout"),
        )
        for identities, result, expected in cases:
            options = {"side_effect": result} if isinstance(result, Exception) else {"return_value": result}
            reasons = []
            with self.subTest(result=result), patch.object(progress.subprocess, "run", **options) as run:
                self.assertIsNone(progress.cpu(identities, unavailable=lambda reason, _evidence: reasons.append(reason)))
                self.assertEqual(reasons, [expected])
                self.assertEqual(run.call_count, bool(identities))

    def test_real_ps_has_an_independent_positive_control(self):
        birth = processes.snapshot()[os.getpid()][2]
        found = progress.cpu({os.getpid(): " ".join(birth.split())})
        self.assertIsNotNone(found, "the host's real ps CPU observation must work")
        self.assertGreaterEqual(found[os.getpid()][1], 0)


class MonitorTests(unittest.TestCase):
    def test_the_1200_second_boundary_and_final_work_recheck_are_independent_of_real_time(self):
        # Exact value verification, not a claim that a physical 20-minute run occurred or
        # that this fixture value is calibrated for a production lane.
        for channel in ("cpu", "output"):
            with self.subTest(channel=channel), tempfile.TemporaryDirectory() as directory:
                log = Path(directory) / "run.log"
                log.touch()
                monitor = progress.Monitor({"seconds": 1200, "action": "stop", "primaryControl": True},
                                           log, Path(directory) / "progress.json")
                used = [0]
                def readings(identities, *, unavailable=None):
                    self.assertEqual(identities, {2: "worker-start"})
                    return {2: ("worker-start", used[0])}
                with patch.object(progress, "cpu", side_effect=readings), \
                        patch.object(monitor, "publish", return_value=True), \
                        contextlib.redirect_stderr(StringIO()):
                    for moment in range(1201):
                        with patch.object(progress.time, "monotonic", return_value=moment), \
                                patch.object(progress.time, "time", return_value=moment):
                            detail = monitor.observe({1: "controller-start", 2: "worker-start"}, excluded={1})
                        self.assertEqual(bool(detail), moment == 1200)
                    self.assertEqual(monitor.last["samples"], 1201)
                    self.assertEqual(monitor.last["unobservableSamples"], 0)
                    self.assertEqual(monitor.last["quietSeconds"], 1200)
                    if channel == "cpu": used[0] = 1
                    else: log.write_text("new work output")
                    with patch.object(progress.time, "monotonic", return_value=1200.5), \
                            patch.object(progress.time, "time", return_value=1200.5):
                        self.assertIsNone(monitor.observe({1: "controller-start", 2: "worker-start"},
                                                          excluded={1}, force=True))
                    self.assertEqual(monitor.last["quietSeconds"], 0)

    def test_report_default_and_explicit_stop_use_this_lanes_own_output_and_processes(self):
        with tempfile.TemporaryDirectory() as directory:
            log, status, sibling = [Path(directory) / name for name in ("run.log", "progress.json", "sibling.log")]
            log.touch()
            readings = lambda ids, **_: {pid: (start, 0) for pid, start in ids.items()} if ids else None
            for action in (None, "stop"):
                settings = {"seconds": 2, **({"action": action} if action else {})}
                monitor = progress.Monitor(settings, log, status)
                with patch.object(progress, "cpu", side_effect=readings), contextlib.redirect_stderr(StringIO()) as error:
                    for moment in range(4):
                        sibling.write_text(str(moment))
                        with patch.object(progress.time, "monotonic", return_value=moment), \
                                patch.object(progress.time, "time", return_value=moment):
                            result = monitor.observe({1: "work", 2: "poller"}, excluded={2})
                        self.assertEqual(bool(result), action == "stop" and moment >= 2)
                self.assertEqual(error.getvalue().count("quiet lane:"), 1)
                recorded = json.loads(status.read_text())
                self.assertEqual(recorded["processes"], [{"pid": 1, "start": "work"}])
                self.assertEqual(recorded["wait"], "unknown")
                monitor.finish("exit-0")
                self.assertEqual(json.loads(status.read_text())["phase"], "ended")
            self.assertFalse(list(Path(directory).glob("*.tmp-*")))

    def test_a_missing_log_cannot_trigger_a_stop_and_is_recorded_privately(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "missing.log"
            monitor = progress.Monitor({"seconds": 1, "action": "stop"}, log, None)
            with patch.object(progress, "cpu", return_value={1: ("birth", 0)}):
                for moment in range(4):
                    with patch.object(progress.time, "monotonic", return_value=moment), \
                            patch.object(progress.time, "time", return_value=moment):
                        self.assertIsNone(monitor.observe({1: "birth"}))
            self.assertEqual(monitor.last["state"], "unobservable")
            retained = json.loads(monitor.status.read_text())
            self.assertEqual(retained["logUnavailableSamples"], {"missing-log": 4})
            self.assertEqual(retained["lastLogUnavailable"]["reason"], "missing-log")
            self.assertIn(str(log), retained["lastLogUnavailable"]["error"])
            self.assertNotIn("lastLogUnavailable", monitor.last)

    def test_cpu_failures_reset_coverage_and_retain_bounded_evidence_only_in_the_receipt(self):
        birth = "Tue Nov 10 12:30:00 2026"
        read = subprocess.CompletedProcess([], 0, f"123 {birth} 00:01.25\n", "")
        denied = subprocess.CompletedProcess([], 1, "", "permission denied")
        timeout = subprocess.TimeoutExpired("ps", 1, stderr=b"x" * 2048 + b"private tail")
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "run.log"
            log.touch()
            live = Path(directory) / "live.json"
            monitor = progress.Monitor({"seconds": 1, "action": "stop"}, log, None)
            monitor.live_paths = [live]
            with patch.object(progress.subprocess, "run", side_effect=[read, denied, timeout, read]), \
                    patch.object(lane_progress, "publish") as publish:
                for moment in range(4):
                    with patch.object(progress.time, "monotonic", return_value=moment), \
                            patch.object(progress.time, "time", return_value=moment):
                        identities = {123: birth, 124: birth} if moment == 2 else {123: birth}
                        self.assertIsNone(monitor.observe(identities))
                monitor.finish("exit-0")
                for args in publish.call_args_list:
                    self.assertNotIn("cpuUnavailableSamples", args.args[0])
                    self.assertNotIn("lastCpuUnavailable", args.args[0])
            retained = json.loads(monitor.status.read_text())
            self.assertEqual(retained["cpuUnavailableSamples"], {"process-list-failed": 1, "process-list-timeout": 1})
            self.assertEqual(retained["unobservableSamples"], 2)
            self.assertEqual(retained["observedSeconds"], 0)
            evidence = retained["lastCpuUnavailable"]
            self.assertEqual(evidence["reason"], "process-list-timeout")
            self.assertEqual(evidence["requested"], [{"pid": 123, "start": birth}, {"pid": 124, "start": birth}])
            self.assertEqual(evidence["stderr"], "x" * progress.DIAGNOSTIC_LIMIT + " [truncated]")

    def test_the_final_recheck_can_observe_progress_and_withdraw_a_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "run.log"
            log.touch()
            monitor = progress.Monitor({"seconds": 1, "action": "stop"}, log, None)
            with patch.object(progress, "cpu", side_effect=[{1: ("birth", 0)}, {1: ("birth", 0)},
                                                            {1: ("birth", 1)}]), \
                    contextlib.redirect_stderr(StringIO()):
                for moment in (0, 1):
                    with patch.object(progress.time, "monotonic", return_value=moment), \
                            patch.object(progress.time, "time", return_value=moment):
                        detail = monitor.observe({1: "birth"})
                self.assertIsNotNone(detail)
                with patch.object(progress.time, "monotonic", return_value=1), \
                        patch.object(progress.time, "time", return_value=1):
                    self.assertIsNone(monitor.observe({1: "birth"}, force=True))
            self.assertEqual(monitor.last["state"], "observing")

    def test_a_failed_status_write_resets_the_quiet_interval_before_monitoring_resumes(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "run.log"
            log.touch()
            monitor = progress.Monitor({"seconds": 2, "action": "stop"}, log, None)
            monitor.live_paths = [Path(directory) / "live.json"]
            write = monitor.write

            def sometimes_unwritable():
                if progress.time.monotonic() == 1:
                    raise OSError("x" * 2048 + "private tail")
                write()

            with patch.object(progress, "cpu", return_value={1: ("birth", 0)}), \
                    patch.object(monitor, "write", side_effect=sometimes_unwritable), \
                    patch.object(lane_progress, "publish") as publish, \
                    contextlib.redirect_stderr(StringIO()) as error:
                for moment in range(5):
                    with patch.object(progress.time, "monotonic", return_value=moment), \
                            patch.object(progress.time, "time", return_value=moment):
                        detail = monitor.observe({1: "birth"})
                    self.assertEqual(bool(detail), moment == 4)
                self.assertEqual(monitor.last["quietSeconds"], 2)
                self.assertEqual(monitor.last["unobservableSamples"], 1)
                self.assertEqual(error.getvalue().count("progress observations unavailable"), 1)
                self.assertNotIn("private tail", error.getvalue())
                for call in publish.call_args_list:
                    self.assertNotIn("lastPublicationFailure", call.args[0])
                    self.assertNotIn("observationError", call.args[0])
            retained = json.loads(monitor.status.read_text())
            self.assertEqual(retained["publicationFailures"], 1)
            self.assertEqual(retained["lastPublicationFailure"], {
                "observedAt": 1, "error": "x" * progress.DIAGNOSTIC_LIMIT + " [truncated]"})


class LaneTests(unittest.TestCase):
    def test_an_unwritable_observation_does_not_stop_the_command_or_change_its_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release = root / "release"
            script = ("from pathlib import Path\nimport sys,time\n"
                      "print('ready',flush=True)\n"
                      "while not Path(sys.argv[1]).exists(): time.sleep(.01)\nprint('done',flush=True)")
            def unavailable():
                release.touch()
                raise OSError("status unavailable")
            for action in ("report", "stop"):
                release.unlink(missing_ok=True)
                with self.subTest(action=action), patch.object(progress.Monitor, "write", side_effect=unavailable), \
                        contextlib.redirect_stderr(StringIO()) as error:
                    status = processes.run([sys.executable, "-c", script, str(release)], cwd=root,
                        log_path=root / "run.log", timeout=stretched(30), stall={"seconds": 1, "action": action})
                    self.assertEqual(status, 0)
                    self.assertIn("done", (root / "run.log").read_text())
                    self.assertIn("progress observations unavailable", error.getvalue())

    def test_stall_settings_need_a_leaf_and_explicit_measured_stop_reason(self):
        def parsed(settings, **options):
            return config.parse(json.dumps({"schemaVersion": 1, "name": "test", "lanes": {
                "test": {"command": ["true"], "stall": settings, **options}}}))
        self.assertEqual(parsed({"seconds": 10})["lanes"]["test"]["stall"], {"seconds": 10})
        parsed({"seconds": 10, "action": "stop", "reason": "measured healthy quiet intervals"})
        for declared in (True, False):
            self.assertIs(parsed({"seconds": 10, "primaryControl": declared})["lanes"]["test"]["stall"]["primaryControl"], declared)
        for settings in ({}, {"seconds": 0}, {"seconds": True}, {"seconds": 1, "extra": 0},
                         {"seconds": 1, "action": "ignore"}, {"seconds": 1, "action": "stop"},
                         {"seconds": 1, "action": "stop", "reason": " "}):
            with self.subTest(settings=settings), self.assertRaises(ValueError): parsed(settings)
        for invalid in (None, 0, 1, "true", [], {}):
            with self.subTest(primaryControl=invalid), self.assertRaisesRegex(ValueError, "primaryControl must be a boolean"):
                parsed({"seconds": 10, "primaryControl": invalid})
        with self.assertRaises(ValueError): parsed({"seconds": 1}, resource="self")

    def test_direct_and_group_stalls_are_unmeasured_and_a_sibling_keeps_its_verdict(self):
        # Fixed: the one-second quiet interval is the behavior under test. CPU readings are
        # deterministic so scheduler load cannot turn a quiet helper into an unobservable sample.
        settings = {"seconds": 1, "action": "stop", "reason": "fixture's deliberate idle sleep"}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hung = {"command": [sys.executable, "-c", "import time; time.sleep(60)"],
                    "timeout": round(stretched(30)), "stall": settings}
            quick = {"command": [sys.executable, "-c", "print('Ran 1 test')"]}
            (root / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "test", "lanes": {
                "hung": hung, "quick": quick, "both": {"parallel": ["hung", "quick"]}}}))
            readings = lambda ids, **_: {pid: (start, 0) for pid, start in ids.items()} if ids else None
            with patch.object(progress, "cpu", side_effect=readings), patch.object(cli, "source_identity", return_value={}), \
                    contextlib.redirect_stdout(StringIO()), contextlib.redirect_stderr(StringIO()) as error:
                for lane in ("hung", "both"):
                    self.assertEqual(cli.main(["--project", directory, "run", "--here", lane]), lane_results.UNMEASURED,
                                     error.getvalue())
            direct = json.loads((root / ".build/queuekeeper/hung/source.json").read_text())
            self.assertEqual(direct["outcome"], "unmeasured")
            self.assertIn("stalled", direct["error"])
            members = json.loads((root / ".build/queuekeeper/both/source.json").read_text())["lanes"]
            self.assertEqual(members["hung"]["outcome"], "unmeasured")
            self.assertEqual(members["quick"]["outcome"], "passed")
            self.assertEqual(resources.ending(processes.Stalled), "timed-out")
            self.assertEqual(resources.guard_owners(os.environ), set(), "confirmed cleanup must release the admission")
            for path in ("hung/results/progress.json", "both/results/hung/progress.json"):
                self.assertEqual(json.loads((root / ".build/queuekeeper" / path).read_text())["state"], "Stalled")

    def test_report_only_quiet_work_and_work_with_output_complete_normally(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = {"seconds": 1}
            # Both run past the one-second behavior under test. The second emits output repeatedly;
            # explicit stop would expose an output probe that accidentally watched another file.
            scripts = [("import time; time.sleep(2); print('done')", settings),
                       ("import time\nfor i in range(5): print(i, flush=True); time.sleep(.4)",
                        {**settings, "action": "stop"})]
            readings = lambda ids, **_: {pid: (start, 0) for pid, start in ids.items()} if ids else None
            with patch.object(progress, "cpu", side_effect=readings), contextlib.redirect_stderr(StringIO()):
                for script, settings in scripts:
                    self.assertEqual(processes.run([sys.executable, "-c", script], cwd=root, log_path=root / "run.log",
                                                   timeout=stretched(30), stall=settings), 0)
                    self.assertEqual(json.loads((root / "run.progress.json").read_text())["state"], "exit-0")
