"""waiting: a wait that a busy host slows but does not fail, and that a hang still ends (#284)."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/python"))
import host_isolation  # noqa: F401 -- isolates this module's host state as every test module's
from vocaro_engineering import waiting

WORKS = "import sys, time\nend = time.monotonic() + float(sys.argv[2])\nwhile time.monotonic() < end: pass\n"


class WaitUntilTests(unittest.TestCase):
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
command = ["eng", "unittest", "--timeout=60"]
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
        self.assertEqual(waiting.fixed_waits(Path(__file__).resolve().parents[1]), [])


if __name__ == "__main__":
    unittest.main()
