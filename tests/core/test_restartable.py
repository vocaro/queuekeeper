"""A process that restarts itself when the checkout it runs from moves (queuekeeper.restartable, vocaro/queuekeeper#159)."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

import host_isolation  # noqa: F401 -- first, as every test module

from queuekeeper import restartable

SOURCE = Path(__file__).resolve().parents[2] / "src/python"


def git(where, *args):
    return subprocess.run(["git", "-C", str(where), *args], capture_output=True, text=True, check=True).stdout.strip()


class Restartable(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.repo = Path(folder.name) / "checkout"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "first")

    def commit(self, message):
        git(self.repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", message)

    def test_a_registered_process_is_passed_over_while_it_runs_and_forgotten_once_it_ends(self):
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        path = restartable.register(self.repo, "the dashboard", pid=process.pid)
        self.assertEqual(path, Path(git(self.repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))
                         / restartable.FOLDER / f"{process.pid}.json")
        self.assertEqual(json.loads(path.read_text())["head"], git(self.repo, "rev-parse", "HEAD"))
        self.assertEqual(restartable.passed(self.repo), {process.pid: "the dashboard"})
        process.kill()
        process.wait()
        self.assertEqual(restartable.passed(self.repo), {})
        self.assertFalse(path.exists())
        # One unregistered is not passed over, and a registration that cannot be read is removed.
        restartable.register(self.repo, "x")
        restartable.unregister(self.repo)
        (restartable.folder(self.repo) / "1.json").write_text("not json")
        self.assertEqual(restartable.passed(self.repo), {})
        self.assertEqual(list(restartable.folder(self.repo).iterdir()), [])

    def test_the_watcher_calls_once_the_head_has_moved_and_not_before(self):
        moved, done = [], threading.Event()
        restartable.watch(self.repo, lambda old, new: (moved.append((old, new)), done.set()), poll=0.05, settle=0.05)
        self.assertFalse(done.wait(0.3))
        first = git(self.repo, "rev-parse", "HEAD")
        self.commit("second")
        self.assertTrue(done.wait(10))
        self.assertEqual(moved, [(first, git(self.repo, "rev-parse", "HEAD"))])

    def test_a_process_whose_checkout_moves_replaces_itself_with_the_same_command(self):
        # The process watches the checkout, says its PID, and on the move restarts; the process it becomes says
        # it restarted, with the same PID, and ends.
        code = (f"import os, sys, time\nsys.path.insert(0, {str(SOURCE)!r})\nfrom queuekeeper import restartable\n"
                f"if os.environ.get(restartable.RESTARTED): print('restarted', os.getpid(), flush=True); sys.exit(0)\n"
                f"print('started', os.getpid(), flush=True)\n"
                f"restartable.watch({str(self.repo)!r}, lambda old, new: restartable.restart(), poll=0.05, settle=0.05)\n"
                "time.sleep(30)\n")
        process = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True,
                                   env={key: value for key, value in os.environ.items() if key != restartable.RESTARTED})
        self.addCleanup(process.kill)
        first = process.stdout.readline().split()
        self.commit("second")
        out, _ = process.communicate(timeout=30)
        self.assertEqual(first[0], "started")
        self.assertIn(f"restarted {first[1]}", out)
        self.assertIn("the toolkit moved; restarting from it", out)
        self.assertEqual(process.returncode, 0)


if __name__ == "__main__":
    unittest.main()
