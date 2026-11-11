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
from vocaro_engineering import cli, config, evidence, parallel, processes, progress, resources


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

    def test_real_ps_has_an_independent_positive_control(self):
        birth = processes.snapshot()[os.getpid()][2]
        found = progress.cpu({os.getpid(): " ".join(birth.split())})
        self.assertIsNotNone(found, "the host's real ps CPU observation must work")
        self.assertGreaterEqual(found[os.getpid()][1], 0)


class MonitorTests(unittest.TestCase):
    def test_report_default_and_explicit_stop_use_this_lanes_own_output_and_processes(self):
        with tempfile.TemporaryDirectory() as directory:
            log, status, sibling = [Path(directory) / name for name in ("run.log", "progress.json", "sibling.log")]
            log.touch()
            readings = lambda ids: {pid: (start, 0) for pid, start in ids.items()} if ids else None
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

    def test_unreadable_cpu_or_log_cannot_trigger_a_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "missing.log"
            monitor = progress.Monitor({"seconds": 1, "action": "stop"}, log, None)
            with patch.object(progress, "cpu", return_value=None):
                for moment in range(4):
                    with patch.object(progress.time, "monotonic", return_value=moment), \
                            patch.object(progress.time, "time", return_value=moment):
                        self.assertIsNone(monitor.observe({1: "birth"}))
            self.assertEqual(monitor.last["state"], "unobservable")

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


class LaneTests(unittest.TestCase):
    def test_stall_settings_need_a_leaf_and_explicit_measured_stop_reason(self):
        def parsed(settings, **options):
            return config.parse(json.dumps({"schemaVersion": 1, "name": "test", "lanes": {
                "test": {"command": ["true"], "stall": settings, **options}}}))
        self.assertEqual(parsed({"seconds": 10})["lanes"]["test"]["stall"], {"seconds": 10})
        parsed({"seconds": 10, "action": "stop", "reason": "measured healthy quiet intervals"})
        for settings in ({}, {"seconds": 0}, {"seconds": True}, {"seconds": 1, "extra": 0},
                         {"seconds": 1, "action": "ignore"}, {"seconds": 1, "action": "stop"},
                         {"seconds": 1, "action": "stop", "reason": " "}):
            with self.subTest(settings=settings), self.assertRaises(ValueError): parsed(settings)
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
            (root / "engineering.json").write_text(json.dumps({"schemaVersion": 1, "name": "test", "lanes": {
                "hung": hung, "quick": quick, "both": {"parallel": ["hung", "quick"]}}}))
            readings = lambda ids: {pid: (start, 0) for pid, start in ids.items()} if ids else None
            with patch.object(progress, "cpu", side_effect=readings), patch.object(cli, "source_identity", return_value={}), \
                    contextlib.redirect_stdout(StringIO()), contextlib.redirect_stderr(StringIO()) as error:
                for lane in ("hung", "both"):
                    self.assertEqual(cli.main(["--project", directory, "run", "--here", lane]), evidence.UNMEASURED,
                                     error.getvalue())
            direct = json.loads((root / ".build/engineering/hung/source.json").read_text())
            self.assertEqual(direct["outcome"], "unmeasured")
            self.assertIn("stalled", direct["error"])
            members = json.loads((root / ".build/engineering/both/source.json").read_text())["lanes"]
            self.assertEqual(members["hung"]["outcome"], "unmeasured")
            self.assertEqual(members["quick"]["outcome"], "passed")
            self.assertEqual(resources.ending(processes.Stalled), "timed-out")
            self.assertEqual(resources.guard_owners(os.environ), set(), "confirmed cleanup must release the admission")
            for path in ("hung/results/progress.json", "both/results/hung/progress.json"):
                self.assertEqual(json.loads((root / ".build/engineering" / path).read_text())["state"], "Stalled")

    def test_report_only_quiet_work_and_work_with_output_complete_normally(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = {"seconds": 1}
            # Both run past the one-second behavior under test. The second emits output repeatedly;
            # explicit stop would expose an output probe that accidentally watched another file.
            scripts = [("import time; time.sleep(2); print('done')", settings),
                       ("import time\nfor i in range(5): print(i, flush=True); time.sleep(.4)",
                        {**settings, "action": "stop"})]
            readings = lambda ids: {pid: (start, 0) for pid, start in ids.items()} if ids else None
            with patch.object(progress, "cpu", side_effect=readings), contextlib.redirect_stderr(StringIO()):
                for script, settings in scripts:
                    self.assertEqual(processes.run([sys.executable, "-c", script], cwd=root, log_path=root / "run.log",
                                                   timeout=stretched(30), stall=settings), 0)
                    self.assertEqual(json.loads((root / "run.progress.json").read_text())["state"], "exit-0")
