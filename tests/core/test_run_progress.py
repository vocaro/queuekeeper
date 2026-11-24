"""A long run keeps its progress in files whatever its standard output is, and qk progress reads them from another
shell (vocaro/queuekeeper#22)."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest.mock import patch

import host_isolation  # first, as every test module

from queuekeeper import cli, run_progress

QK = Path(__file__).resolve().parents[2] / "scripts/qk"


def result(name, status=0, ran=2, outcome="OK"):
    return {"name": name, "tests": [name], "status": status, "output": f"..\nRan {ran} tests in 0.1s\n\n{outcome}\n",
            "ran": ran, "result": outcome, "seconds": 0.5}


class Folder(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        self.enterContext(patch.dict(os.environ))
        os.environ.pop(run_progress.VARIABLE, None)


class Tracking(Folder):
    def test_each_process_is_counted_as_it_ends_and_those_running_are_named(self):
        folder = run_progress.new_folder()
        tracker = run_progress.Tracker(folder, ["qk", "unittest", "-s", "tests"])
        tracker.planned(3, 6, 2)
        tracker.started("test_a")
        tracker.started("test_b")
        tracker.finished("test_b", result("test_b", status=1, outcome="FAILED (failures=1)"))
        state = json.loads((folder / run_progress.STATE).read_text())
        self.assertEqual((state["finished"], state["processes"], state["failed"], list(state["running"])),
                         (1, 3, ["test_b"], ["test_a"]))
        log = (folder / run_progress.LOG).read_text()
        self.assertIn("1 of 3 processes, 2 tests, 1 failed, 0 unmeasured, 1 running: test_b: FAILED (failures=1)", log)
        self.assertIn("Ran 2 tests in 0.1s", log)
        said = "\n".join(run_progress.describe(folder))
        self.assertIn("running for", said)
        self.assertIn("1 of 3 processes, 2 of 6 tests, 1 failed, 0 unmeasured, 2 at a time", said)
        self.assertIn("failed: test_b", said)
        self.assertIn("running: test_a, for", said)
        self.assertIn("last finished: test_b: FAILED (failures=1)", said)
        tracker.finished("test_a", result("test_a"))
        tracker.end(1, "queuekeeper unittest: 4 tests ...")
        said = "\n".join(run_progress.describe(folder))
        self.assertIn("ended ", said)
        self.assertIn("with status 1", said)
        self.assertNotIn("running: ", said)

    def test_the_runs_still_running_say_when_they_last_moved(self):
        self.assertIsNone(run_progress.moved(self.root))
        before = time.time()
        folder = run_progress.new_folder()
        tracker = run_progress.Tracker(folder, ["qk", "unittest", "-s", "tests"])
        tracker.planned(2, 4, 2)
        tracker.started("test_a")
        self.assertGreaterEqual(run_progress.moved(self.root, before), before)
        # A process that finished moved it, and so did what an admitted runner printed.
        state = json.loads((folder / run_progress.STATE).read_text())
        state.update(updated=before - 500, running={"test_a": before - 600})
        run_progress.write(folder / run_progress.STATE, state)
        self.assertEqual(run_progress.moved(self.root), before - 500)
        (folder / run_progress.OUTPUT).write_text("a line\n")
        os.utime(folder / run_progress.OUTPUT, (before - 100, before - 100))
        self.assertEqual(run_progress.moved(self.root), before - 100)
        # A run that started before the gate did is another's, and one that ended moves no more.
        self.assertIsNone(run_progress.moved(self.root, time.time() + 60))
        tracker.end(0)
        self.assertIsNone(run_progress.moved(self.root, before))

    def test_a_lanes_logs_say_when_its_command_last_wrote(self):
        # A lane's command writes to its lane's log, not to the gate's, which says only that it began and ended (#185).
        self.assertIsNone(run_progress.logs_moved(self.root))
        before = time.time()
        lanes = self.root / run_progress.LANES
        logs = {"own": lanes / "gate/run.log", "member": lanes / "gate/lanes/swift-tests.log",
                "beside": lanes / ".runs/gate/20261116T000000Z-42/run.log",
                "beside's member": lanes / ".runs/gate/20261116T000000Z-42/lanes/docs.log"}
        for offset, (name, log) in enumerate(logs.items()):
            with self.subTest(name):
                log.parent.mkdir(parents=True, exist_ok=True)
                log.write_text("a line\n")
                os.utime(log, (before - 300 + offset, before - 300 + offset))
                self.assertEqual(run_progress.logs_moved(self.root, before - 600), before - 300 + offset)
        # A log last written before the gate began is an earlier run's.
        self.assertIsNone(run_progress.logs_moved(self.root, before))

    def test_a_process_a_stop_kept_from_starting_is_not_counted(self):
        tracker = run_progress.Tracker(run_progress.new_folder(), ["qk", "unittest"])
        tracker.planned(1, 1, 1)
        tracker.started("test_a")
        tracker.finished("test_a", None)
        state = json.loads((tracker.folder / run_progress.STATE).read_text())
        self.assertEqual((state["finished"], state["running"]), (0, {}))

    def test_ended_runs_beyond_the_newest_kept_are_removed_and_a_running_one_is_not(self):
        runs = self.root / run_progress.RUNS
        runs.mkdir(parents=True)
        for index in range(run_progress.KEPT + 3):
            folder = runs / f"2026010{index:02d}-1"
            folder.mkdir()
            ended = None if index == 0 else 1.0
            run_progress.write(folder / run_progress.RECORD, {"pid": os.getpid(), "ended": ended})
        run_progress.new_folder()
        left = sorted(path.name for path in runs.iterdir())
        # The oldest runs, which have ended, are gone; the oldest of all still runs, in this process, and stays.
        self.assertIn("2026010" + "00-1", left)
        self.assertNotIn("2026010" + "01-1", left)
        self.assertEqual(len(left), run_progress.KEPT + 2)

    def test_an_admitted_runner_names_its_folder_to_the_run_inside_it_which_announces_none(self):
        said = io.StringIO()
        with contextlib.redirect_stdout(said), run_progress.admitted(["qk", "unittest"]) as outcome:
            named = os.environ[run_progress.VARIABLE]
            tracker = run_progress.tracker(["qk", "unittest"])
            self.assertNotIn(run_progress.VARIABLE, os.environ)
            outcome["status"] = 3
        self.assertEqual(str(tracker.folder), named)
        self.assertEqual(said.getvalue().count("keeps this run's progress"), 1)
        self.assertNotIn(run_progress.VARIABLE, os.environ)
        self.assertEqual(json.loads((Path(named) / run_progress.RECORD).read_text())["status"], 3)


class Pipes(Folder):
    def test_output_into_a_pipe_names_the_folder_on_standard_error_too(self):
        reader, writer = os.pipe()
        self.addCleanup(os.close, reader)
        self.addCleanup(os.close, writer)
        self.assertTrue(run_progress.piped(writer))
        with tempfile.TemporaryFile() as file: self.assertFalse(run_progress.piped(file.fileno()))
        for piped in (True, False):
            out, error = io.StringIO(), io.StringIO()
            with patch.object(run_progress, "piped", return_value=piped), contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(error):
                run_progress.announce(self.root)
            self.assertIn(f"progress: {self.root} keeps this run's progress", out.getvalue())
            self.assertEqual(f"progress: {self.root} keeps" in error.getvalue(), piped)

    def test_a_tee_copies_what_this_process_and_its_children_write_and_outlasts_a_reader_that_left(self):
        copy = self.root / "copy.log"
        with tempfile.TemporaryFile() as shown:
            sys.stdout.flush()
            saved = os.dup(1)
            os.dup2(shown.fileno(), 1)
            try:
                with run_progress.teed(copy):
                    subprocess.run([sys.executable, "-c", "print('from a child')"], check=True)
                    os.write(1, b"from this process\n")
            finally:
                os.dup2(saved, 1)
                os.close(saved)
            shown.seek(0)
            self.assertEqual(shown.read(), b"from a child\nfrom this process\n")
        self.assertEqual(copy.read_text(), "from a child\nfrom this process\n")
        # A reader that went away, as `head` does: the copy is still whole.
        reader, writer = os.pipe()
        os.close(reader)
        sys.stdout.flush()
        saved = os.dup(1)
        os.dup2(writer, 1)
        os.close(writer)
        try:
            with run_progress.teed(copy):
                subprocess.run([sys.executable, "-c", "print('after the reader left')"], check=True)
        finally:
            os.dup2(saved, 1)
            os.close(saved)
        self.assertTrue(copy.read_text().endswith("after the reader left\n"))


class Command(Folder):
    def progress(self, *arguments):
        out, error = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(error):
            status = cli.main(["progress", *arguments])
        return status, out.getvalue() + error.getvalue()

    def test_with_no_run_it_says_where_it_looked(self):
        status, said = self.progress()
        self.assertEqual(status, 1)
        self.assertIn(f"no run kept under {self.root / run_progress.RUNS}", said)

    def test_a_run_on_the_farm_mac_is_read_from_its_output_here(self):
        folder = run_progress.new_folder()
        run_progress.write(folder / run_progress.RECORD, run_progress.started_record(["qk", "unittest", "-s", "tests"]))
        (folder / run_progress.OUTPUT).write_text("progress: 9 tests in 3 processes, 2 at a time\n"
                                                  "== test_a: OK (3 tests, 1.0s)\nRan 3 tests\n\nOK\n")
        status, said = self.progress()
        self.assertEqual(status, 0, said)
        self.assertIn("  9 tests in 3 processes, 2 at a time", said)
        self.assertIn("1 processes finished", said)
        self.assertIn("last line: OK", said)
        self.assertIn("last moved", said)

    def test_with_none_running_it_reads_the_newest_and_all_reads_every_one(self):
        for name in ("20260101T000000Z-1", "20260102T000000Z-1"):
            (self.root / run_progress.RUNS / name).mkdir(parents=True)
            run_progress.write(self.root / run_progress.RUNS / name / run_progress.RECORD,
                               {"pid": 1, "command": ["qk", name], "started": 1.0, "ended": 2.0, "status": 0})
        status, said = self.progress()
        self.assertEqual(status, 0, said)
        self.assertIn("qk 20260102T000000Z-1", said)
        self.assertNotIn("qk 20260101T000000Z-1", said)
        status, said = self.progress("--all")
        self.assertIn("qk 20260101T000000Z-1", said)


class ThisMac(Folder):
    """A run on this Mac is checked here whichever of the Mac's names it recorded, and sleep is told apart from a
    stall (vocaro/queuekeeper#347)."""

    def setUp(self):
        super().setUp()
        # This Mac as the issue saw it: its LocalHostName, and a kernel host name that changed across a sleep.
        self.enterContext(patch.object(run_progress, "this_mac", return_value="Trevors-MacBook-Pro-715"))
        self.enterContext(patch.object(run_progress.socket, "gethostname", return_value="Trevors-MacBook-Pro-715"))

    def run_recorded(self, **record):
        # A base of its own for each, since one process's folders made in one second share a name.
        folder = run_progress.new_folder(Path(tempfile.mkdtemp(dir=self.root)))
        run_progress.write(folder / run_progress.RECORD, {"pid": os.getpid(), "command": ["qk", "unittest"],
                                                          "started": 1000.0, "ended": None, "status": None, **record})
        return folder

    def test_a_live_run_here_is_running_whichever_of_this_macs_names_it_recorded(self):
        for host in ("Trevors-MacBook-Pro-715.local", "Trevors-MacBook-Pro-715", "trevors-macbook-pro-715.local.", None):
            with self.subTest(host=host):
                folder = self.run_recorded(host=host)
                self.assertTrue(run_progress.running(folder))
                said = "\n".join(run_progress.describe(folder, now=1060.0, awake_now=None))
                self.assertIn(f"running for 1m00s, process {os.getpid()}", said)
                self.assertNotIn("not running", said)
        # The kernel's name now, when the LocalHostName is another, is this Mac's too.
        with patch.object(run_progress.socket, "gethostname", return_value="Mac.lan"):
            self.assertTrue(run_progress.running(self.run_recorded(host="Mac.lan")))

    def test_a_run_here_that_ended_without_a_status_is_still_said_to_have(self):
        ended = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True)
        pid = int(ended.stdout)
        for host in ("Trevors-MacBook-Pro-715.local", "Trevors-MacBook-Pro-715"):
            with self.subTest(host=host):
                folder = self.run_recorded(host=host, pid=pid)
                self.assertFalse(run_progress.running(folder))
                self.assertIn(f"  not running: process {pid} on {host} ended without a status",
                              run_progress.describe(folder))

    def test_a_run_on_another_mac_is_not_checked_here(self):
        folder = self.run_recorded(host="build-host.local")
        self.assertFalse(run_progress.running(folder))
        self.assertIn(f"  not running here: process {os.getpid()} is on build-host.local, another Mac, which this one "
                      "cannot check", run_progress.describe(folder))

    def test_a_record_names_this_mac_by_its_local_host_name_and_reads_both_clocks(self):
        with patch.object(run_progress, "awake", return_value=42.0):
            record = run_progress.started_record(["qk"])
        self.assertEqual((record["host"], record["startedAwake"]), ("Trevors-MacBook-Pro-715", 42.0))

    def test_a_running_run_says_how_long_this_mac_slept_during_it_and_since_it_last_moved(self):
        # Started at wall 1000 with the awake clock at 500; an hour later on the wall, this Mac has slept 12 minutes.
        folder = self.run_recorded(host="Trevors-MacBook-Pro-715.local", startedAwake=500.0)
        run_progress.write(folder / run_progress.STATE, {"started": 1000.0, "updated": 3000.0, "updatedAwake": 2500.0})
        said = run_progress.describe(folder, now=4600.0, awake_now=500.0 + 3600 - 720)
        self.assertIn(f"  running for 1h00m (48m00s awake: this Mac slept 12m00s of it), process {os.getpid()}", said)
        # Of the 26m40s since STATE last moved, the Mac slept 12m00s.
        self.assertTrue(any(line.startswith("  last moved 26m40s ago (14m40s awake: this Mac slept 12m00s of it), at ")
                            for line in said), said)

    def test_an_ended_run_says_how_long_this_mac_slept_during_it(self):
        folder = self.run_recorded(host="Trevors-MacBook-Pro-715", startedAwake=500.0, ended=4600.0,
                                   endedAwake=500.0 + 3600 - 720, status=0)
        self.assertTrue(any(line.endswith("with status 0, after 1h00m (48m00s awake: this Mac slept 12m00s of it)")
                            for line in run_progress.describe(folder)))

    def test_no_sleep_is_named_without_both_clocks_across_a_restart_or_for_a_short_drift(self):
        cases = {"no awake reading": ({}, 4600.0, 4100.0), "restart": ({"startedAwake": 9000.0}, 4600.0, 100.0),
                 "drift": ({"startedAwake": 500.0}, 4600.0, 500.0 + 3600 - 30)}
        for case, (record, now, awake_now) in cases.items():
            with self.subTest(case):
                said = "\n".join(run_progress.describe(self.run_recorded(**record), now=now, awake_now=awake_now))
                self.assertIn(f"  running for 1h00m, process {os.getpid()}", said)
                self.assertNotIn("slept", said)

    def test_a_run_on_another_mac_is_not_said_to_have_slept_by_this_macs_clocks(self):
        folder = self.run_recorded(host="build-host", startedAwake=500.0, ended=4600.0, endedAwake=500.0, status=0)
        self.assertNotIn("slept", "\n".join(run_progress.describe(folder)))


class Unittest(Folder):
    def test_qk_unittest_names_its_folder_first_and_keeps_its_progress_there_though_piped(self):
        (self.root / "test_one.py").write_text(textwrap.dedent("""
            import unittest
            class One(unittest.TestCase):
                def test_one(self): pass
                def test_two(self): pass
        """))
        # capture_output gives the run a pipe for its standard output, as `| tail` does.
        finished = subprocess.run([sys.executable, str(QK), "unittest", "--unadmitted", "-s", str(self.root)],
                                  capture_output=True, text=True, cwd=self.root, env={"PATH": "/usr/bin:/bin"})
        self.assertEqual(finished.returncode, 0, finished.stderr + finished.stdout)
        first = finished.stdout.splitlines()[0]
        self.assertTrue(first.startswith("progress: ") and first.endswith("; qk progress reads it from another shell"),
                        finished.stdout)
        folder = Path(first.removeprefix("progress: ").split(" keeps this run")[0])
        self.assertIn(first, finished.stderr)
        self.assertIn("\nprogress: 2 tests in 1 processes, 4 at a time\n", finished.stdout)
        # The only line that begins `queuekeeper unittest: ` is still the summary.
        self.assertEqual([line for line in finished.stdout.splitlines() if line.startswith("queuekeeper unittest: ")],
                         [finished.stdout.splitlines()[-1]])
        state = json.loads((folder / run_progress.STATE).read_text())
        self.assertEqual((state["finished"], state["processes"], state["ran"], state["status"]), (1, 1, 2, 0))
        self.assertIsNotNone(state["ended"])
        self.assertIn("1 of 1 processes, 2 tests, 0 failed", (folder / run_progress.LOG).read_text())


if __name__ == "__main__":
    unittest.main()
