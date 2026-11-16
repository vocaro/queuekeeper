"""waiting: a wait that a busy host slows but does not fail, and that a hang still ends (#284)."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src/python"))
import host_isolation  # noqa: F401 -- isolates this module's host state as every test module's
from queuekeeper import offload_host, resources, waiting

WORKS = "import sys, time\nend = time.monotonic() + float(sys.argv[2])\nwhile time.monotonic() < end: pass\n"


def quiet(test):
    """No busy share to doubt the load by, so that a test's patched load alone stretches its bounds."""
    patcher = patch.object(waiting, "busy_now", return_value=None)
    patcher.start()
    test.addCleanup(patcher.stop)


class WaitUntilTests(unittest.TestCase):
    def setUp(self):
        quiet(self)

    def test_a_condition_already_true_returns_at_once(self):
        started = time.monotonic()
        self.assertTrue(waiting.wait_until(lambda: True, stall=60))
        self.assertLess(time.monotonic() - started, 1)

    def test_progress_keeps_a_wait_going_past_its_stall_bound(self):
        """The positive control: work that keeps moving is waited for however long it takes."""
        ticks, end = iter(range(10 ** 6)), time.monotonic() + 1.5  # wall-clock: the work's length, no bound
        with patch.object(waiting.os, "getloadavg", return_value=(0.0,) * 3):
            self.assertTrue(waiting.wait_until(lambda: time.monotonic() >= end, progress=lambda: next(ticks),
                                               stall=0.3, interval=0.02))

    def test_a_stall_ends_the_wait_at_its_bound(self):
        """The negative control: work that stands still fails at the bound instead of holding the suite."""
        started = time.monotonic()
        with patch.object(waiting.os, "getloadavg", return_value=(0.0,) * 3):
            self.assertFalse(waiting.wait_until(lambda: False, progress=lambda: "still", stall=0.3, interval=0.02))
        self.assertLess(time.monotonic() - started, 5)

    def test_without_progress_the_bound_is_a_stretched_deadline(self):
        cores = os.cpu_count() or 1
        for load, least in ((0.0, 0.2), (4 * cores, 0.8)):
            with self.subTest(load=load), patch.object(waiting.os, "getloadavg", return_value=(load,) * 3):
                started = time.monotonic()
                self.assertFalse(waiting.wait_until(lambda: False, stall=0.2, interval=0.02))
                self.assertGreaterEqual(time.monotonic() - started, least)

    def test_work_that_ends_on_the_last_look_counts(self):
        answers = iter([False, False, True])
        with patch.object(waiting.os, "getloadavg", return_value=(0.0,) * 3):
            self.assertTrue(waiting.wait_until(lambda: next(answers, True), progress=lambda: 0, stall=0, interval=0))


    def test_setting_stall_shortens_a_default_wait(self):
        """stall is read when the call is made, so a test that sets waiting.STALL shortens every default wait (#388)."""
        started = time.monotonic()
        with patch.object(waiting, "STALL", 0.2), patch.object(waiting.os, "getloadavg", return_value=(0.0,) * 3):
            self.assertFalse(waiting.wait_until(lambda: False, interval=0.02))
        self.assertLess(time.monotonic() - started, 5)


def until_exit(process, done):
    """A pause that returns when process exits, and once it has exited sleeps out the interval while done() fails."""
    def pause(interval):
        started = time.monotonic()
        try:
            process.wait(timeout=interval)
        except subprocess.TimeoutExpired:
            return
        if not done():
            time.sleep(max(0, interval - (time.monotonic() - started)))
    return pause


class PauseTests(unittest.TestCase):
    """A caller's pause wakes the wait early, and never turns it into a busy loop (#395)."""
    def setUp(self):
        quiet(self)

    def child(self, seconds):
        process = subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({seconds})"],
                                   stdin=subprocess.DEVNULL)
        self.addCleanup(lambda: (process.kill(), process.wait()))
        return process

    def test_a_pause_that_wakes_on_exit_ends_the_wait_before_its_interval(self):
        process = self.child(0.3)
        done = lambda: process.poll() is not None
        started = time.monotonic()
        self.assertTrue(waiting.wait_until(done, interval=60, pause=until_exit(process, done)))
        self.assertLess(time.monotonic() - started, 10, "The wait slept out its 60 s interval after the exit.")

    def test_a_pause_lasts_the_interval_while_done_still_fails(self):
        process = self.child(0)
        samples = []
        def progress():
            samples.append(time.monotonic())
            return "still"
        never = lambda: False
        with patch.object(waiting.os, "getloadavg", return_value=(0.0,) * 3):
            self.assertFalse(waiting.wait_until(never, progress=progress, stall=1, interval=0.2,
                                                pause=until_exit(process, never)))
        gaps = [later - earlier for earlier, later in zip(samples, samples[1:])]
        self.assertTrue(gaps)
        self.assertGreaterEqual(min(gaps), 0.2, f"Progress was sampled {len(samples)} times, {min(gaps):.3f} s apart.")


class StretchReadingTests(unittest.TestCase):
    """A bound stretches by the load per core, and by DOUBTED_STRETCH when a low load sits beside a saturated CPU
    (vocaro/queuekeeper#43)."""

    def reading(self, load, busy, cores=10):
        loaded = patch.object(waiting.os, "getloadavg", **({"side_effect": OSError("no load")} if load is None
                                                             else {"return_value": (load,) * 3}))
        with loaded, patch.object(waiting.os, "cpu_count", return_value=cores), \
                patch.object(waiting, "busy_now", return_value=busy) as read:
            return waiting.stretch_reading(), waiting.stretched(5), read.called

    def test_build_hosts_low_reading_beside_a_saturated_cpu_is_doubted(self):
        # 2026-11-12: 7.45 on 10 cores with the CPU 100% busy, 12 s before the load read 250. Before #43 this
        # stretched a 5 s bound not at all.
        for busy in (100.0, 99.0, waiting.SATURATED):
            with self.subTest(busy=busy):
                reading, bound, _ = self.reading(7.45, busy)
                self.assertEqual(reading, {"load": 7.45, "cores": 10, "busy": busy, "factor": waiting.DOUBTED_STRETCH})
                self.assertEqual(bound, 5 * waiting.DOUBTED_STRETCH)
        self.assertEqual(waiting.stretch_words(reading),
                         "stretched 24 times, by a one-minute load of 7.45 on 10 cores with the CPU 95% busy")

    def test_a_low_load_beside_a_cpu_with_idle_time_or_no_busy_reading_stretches_by_the_load(self):
        """The negative control: a quiet Mac's bounds are not stretched for nothing."""
        for busy in (94.9, 10.0, None):
            with self.subTest(busy=busy):
                reading, bound, _ = self.reading(7.45, busy)
                self.assertEqual((reading["factor"], bound), (1, 5))

    def test_a_load_at_or_over_the_cores_stretches_by_itself_and_reads_no_busy_share(self):
        for load, factor in ((10.0, 1), (250.0, 25), (951.0, 95.1)):
            with self.subTest(load=load):
                reading, bound, read = self.reading(load, 100.0)
                self.assertEqual((reading["busy"], read), (None, False))
                self.assertAlmostEqual(reading["factor"], factor)
                self.assertAlmostEqual(bound, 5 * factor)
        self.assertEqual(waiting.stretch_words(reading), "stretched 95.1 times, by a one-minute load of 951 on 10 cores")

    def test_a_load_that_cannot_be_read_is_doubted(self):
        reading, bound, read = self.reading(None, None)
        self.assertEqual((reading["load"], reading["factor"], bound, read), (None, waiting.DOUBTED_STRETCH, 120, False))
        self.assertEqual(waiting.stretch_words(reading), "stretched 24 times, by a one-minute load that could not be read")

    def test_the_busy_share_is_admissions_reading_of_its_slot_directory(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, HEAVY_SLOT_DIR=directory), \
                patch.object(waiting, "cpu_busy", return_value=(97.5, 15.0)) as read:
            self.assertEqual(waiting.busy_now(), 97.5)
        self.assertEqual(read.call_args.args, (Path(directory),))
        with patch.object(waiting, "cpu_busy", side_effect=OSError("unreadable")):
            self.assertIsNone(waiting.busy_now())

    @unittest.skipUnless(sys.platform == "darwin", "macOS names its default slot directory by getconf")
    def test_a_mocked_getconf_answer_is_refused_and_writes_nothing_where_the_process_is(self):
        """A project's test that patches subprocess.run with a bare mock and reaches a stretched bound got a mock
        for the slot directory, and cpu-samples.json under MagicMock/ in its checkout (vocaro/queuekeeper#161)."""
        environment = {name: value for name, value in os.environ.items() if name != "HEAVY_SLOT_DIR"}
        before = Path.cwd()
        with tempfile.TemporaryDirectory() as directory:
            os.chdir(directory)
            try:
                waiting._slots.cache_clear()
                with patch.dict(os.environ, environment, clear=True), patch("subprocess.run", MagicMock()), \
                        patch.object(waiting.os, "getloadavg", return_value=(0.0,) * 3):
                    with self.assertRaisesRegex(RuntimeError, "not an absolute path"):
                        resources.slot_directory()
                    self.assertIsNone(waiting.busy_now())
                    self.assertIsNone(waiting.stretch_reading()["busy"])
                for answer in ("relative/folder\n", "\n"):
                    with self.subTest(answer=answer), patch.dict(os.environ, environment, clear=True), \
                            patch.object(resources.subprocess, "check_output", return_value=answer), \
                            self.assertRaisesRegex(RuntimeError, "Cannot establish the host resource directory"):
                        resources.slot_directory()
                for answer in ("relative/folder", None):  # another Mac's reading of the same directory
                    with self.subTest(there=answer), patch.dict(os.environ, environment, clear=True), \
                            patch.object(offload_host, "output", return_value=answer), \
                            self.assertRaisesRegex(RuntimeError, "Cannot establish the host resource directory"):
                        offload_host.slot_directory()
                with patch.dict(os.environ, environment, clear=True), \
                        patch.object(resources.subprocess, "check_output", return_value="/private/answer/\n"):
                    self.assertEqual(resources.slot_directory(), Path("/private/answer/queuekeeper-heavy-slots"))
                self.assertEqual(list(Path(directory).iterdir()), [])
            finally:
                os.chdir(before)
                waiting._slots.cache_clear()


class ProbeTests(unittest.TestCase):
    def test_files_change_with_a_new_file_a_size_or_a_removal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            probe = waiting.files(root / "work", root / "missing")
            self.assertEqual(probe(), [])
            (root / "work/deeper").mkdir(parents=True)
            self.assertEqual(probe(), [], "An empty directory is no progress.")
            (root / "work/deeper/log").write_text("one")
            first = probe()
            self.assertEqual(len(first), 1)
            self.assertEqual(probe(), first)
            (root / "work/deeper/log").write_text("one two")
            self.assertNotEqual(probe(), first)
            (root / "work/deeper/log").unlink()
            self.assertEqual(probe(), [])

    def test_cpu_counts_a_working_process_and_not_a_sleeping_or_excluded_one(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "waiting-probe"
            script = Path(directory) / "work.py"
            script.write_text(WORKS)
            sleeper = subprocess.Popen(["sleep", "30"], cwd=directory)
            worker = subprocess.Popen([sys.executable, str(script), str(marker), "30"])
            try:
                busy, idle, excluded = waiting.cpu(marker), waiting.cpu("sleep 30"), waiting.cpu(marker, exclude=["work.py"])
                first = busy()
                self.assertEqual([pid for pid, _ in first], [str(worker.pid)])
                self.assertTrue(waiting.wait_until(lambda: busy() != first, stall=10, interval=0.05),
                                "A process at work uses CPU between looks.")
                self.assertEqual(excluded(), [])
                asleep = [entry for entry in idle() if entry[0] == str(sleeper.pid)]
                time.sleep(0.5)
                self.assertEqual([entry for entry in idle() if entry[0] == str(sleeper.pid)], asleep,
                                 "A sleeping process uses no CPU.")
            finally:
                for process in (sleeper, worker):
                    process.kill()
                    process.wait()

    def test_combined_changes_when_any_probe_does(self):
        left, right = ["a"], ["b"]
        probe = waiting.combined(lambda: list(left), lambda: list(right))
        first = probe()
        self.assertEqual(probe(), first)
        right.append("c")
        self.assertNotEqual(probe(), first)


class FixedWaitTests(unittest.TestCase):
    SOURCE = """import subprocess, threading, time
process.communicate(timeout=30)
process.wait(timeout=10)
deadline = time.monotonic() + 20
deadline = time.monotonic() + stretched(60)
process.communicate(timeout=stretched(30))
process.wait(timeout=45)  # wall-clock: the limit under test
subprocess.run(["true"],
               timeout=120.5)
def simctl(*arguments, timeout=600): pass
fake = lambda *arguments, timeout=30: None
command = ["qk", "unittest", "--timeout=60"]
Admission("gate", timeout=60)
threading.Event().wait(timeout=LIMIT)
subprocess.check_output(["true"],  # wall-clock: says why on its first line
                        timeout=90)
"""

    def project(self, directory, files):
        root = Path(directory)
        for name, text in files.items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text(text)
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        subprocess.run(["git", "-C", str(root), "add", *files], check=True)
        return root

    def test_literal_bounds_of_twenty_seconds_or_more_in_tests_are_found(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.project(directory, {"scripts/tests/test_fixture.py": self.SOURCE,
                                            "tests/helpers.py": "deadline = time.monotonic() + 30\n",
                                            "tests/test_broken.py": "process.wait(timeout=30\n",
                                            "evidence/run/originals/tests/test_fixture.py": self.SOURCE,
                                            "scripts/run.py": self.SOURCE})
            # A call that spans lines is found at its first; defaults, option strings, other calls and names are not,
            # nor a file that does not parse, recorded evidence or a file outside the tests.
            self.assertEqual(waiting.fixed_waits(root), [("scripts/tests/test_fixture.py", 2),
                ("scripts/tests/test_fixture.py", 4), ("scripts/tests/test_fixture.py", 8), ("tests/helpers.py", 1)])
            [warning] = waiting.doctor_warnings(root)
            self.assertIn("4 test line(s)", warning)
            self.assertIn("scripts/tests/test_fixture.py:2", warning)
            self.assertIn("Waits bound stalls", warning)
            self.assertIn("`monotonic() + N` deadline", warning)

    def found(self, text):
        """The line numbers fixed_waits lists for a test file holding this text."""
        with tempfile.TemporaryDirectory() as directory:
            root = self.project(directory, {"tests/test_shapes.py": text})
            return [number for name, number in waiting.fixed_waits(root)]

    def test_a_deadline_of_any_length_is_found_and_a_short_timeout_is_not(self):
        self.assertEqual(self.found("""deadline = time.monotonic() + 10
deadline = monotonic() + 0.5
deadline = time.monotonic() + 5  # wall-clock: a grace before SIGKILL
deadline = time.monotonic() + LIMIT
deadline = time.monotonic() + stretched(5)
deadline = time.monotonic() - 5
deadline = time.time() + 5
later = started + 5
process.wait(timeout=5)
process.communicate(timeout=19.9)
"""), [1, 2])

    def test_a_fixed_wait_in_a_fixtures_source_is_found_on_its_line_in_the_file(self):
        self.assertEqual(self.found("""import time
FAKE = r'''
import os, time
deadline = time.monotonic() + 10
grace = time.monotonic() + 5  # wall-clock: a cleanup's grace
process.communicate(timeout=30)
process.wait(timeout=5)
limit = time.monotonic() + stretched(5)
'''
CHILD: str = "import time\\ndeadline = time.monotonic() + 3\\n"
RUNNER = '''#!/usr/bin/env python3
import time
end = time.monotonic() + 7
'''
"""), [4, 6, 10, 13])

    def test_a_shebang_script_that_is_not_python_is_searched_for_a_literal_deadline(self):
        self.assertEqual(self.found("""WRAPPER = '''#!/bin/sh
python3 -c 'import time; end = time.monotonic() + 7'
python3 -c 'import time; end = time.monotonic() + 8'  # wall-clock: the limit under test
python3 -c 'import time; end = time.monotonic() + LIMIT'
python3 -c 'import time; end = time.monotonic() + 2 * LIMIT'
python3 -c 'import time; end = time.monotonic() + 1.5x'
timeout=30 python3 run.py
'''
"""), [2])

    def test_strings_that_are_not_a_fixtures_source_are_left_alone(self):
        """The negative controls nearest a fixture: one-line data, text that does not parse and has no shebang, a
        script that is not at module level, and a fixture with nothing fixed in it."""
        self.assertEqual(self.found("""EXPECTED = "deadline = time.monotonic() + 30"
CALL = "process.wait(timeout=30)"
LOG = '''gate timed out at monotonic() + 30
second line, and not Python at all
'''
class Fixtures:
    SOURCE = '''import time
deadline = time.monotonic() + 10
'''
def helper():
    local = '''import time
deadline = time.monotonic() + 10
'''
    return local
CLEAN = '''import time
deadline = time.monotonic() + limit
process.wait(timeout=5)
'''
"""), [])

    def test_a_project_with_none_or_no_repository_warns_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.project(directory, {"tests/test_ok.py": "process.wait(timeout=stretched(30))\n"})
            self.assertEqual(waiting.doctor_warnings(root), [])
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(waiting.doctor_warnings(directory), [])

    def test_the_toolkit_holds_none(self):
        self.assertEqual(waiting.fixed_waits(Path(__file__).resolve().parents[2]), [])


if __name__ == "__main__":
    unittest.main()
