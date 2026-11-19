"""The merge queue against real Git repositories: a bare origin, a clone, and lanes that read the tree."""
import argparse
import atexit
import contextlib
import functools
import importlib.util
import io
import json
import os
from pathlib import Path
import plistlib
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import types
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src/python"))
from queuekeeper import cli, config as configuration, landing_proofs, merge_queue as mq, offload_host
# What qk land runs before it queues, which Fixture leaves out but PreflightBeforeQueued puts back.
PREFLIGHT_BEFORE_QUEUED = mq.preflight_before_queued
import host_isolation
from host_isolation import stretched

# Each class in a process of its own under `qk unittest`'s default module split: whole, with a real Git
# fixture built for every test, this module alone bounded the toolkit's suite.
shard_by = "class"


# What CHECK finds, built so that no file holds it whole, as a project's own spelling check or linter finds a slip.
FLAGGED = "a flag" + "ged point\n"
# A project's own preflight lane (Fixture.preflight_check): prints PATH:LINE: for each line of a tracked file that holds
# FLAGGED's word and exits 1, else exits 0, as a check whose findings the queue quotes does (vocaro/queuekeeper#69).
CHECK = """import subprocess, sys
word = "flag" + "ged"
tracked = subprocess.run(["git", "ls-files", "-z"], capture_output=True, text=True, check=True).stdout.split("\\0")
found = [f"{path}:{number}: {word}" for path in tracked if path
         for number, line in enumerate(open(path, errors="replace").read().splitlines(), 1) if word in line]
print("\\n".join(found) or "nothing found")
sys.exit(1 if found else 0)
"""
# Fails when any tracked file is named bad*, passes otherwise; touches a counter so a test can count gates.
LANE = """import os, pathlib, shutil, sys, time
# Appended, so that two gates at once, a bisect's halves (mq.Beside), count as two.
with open(sys.argv[1], "a") as counter:
    counter.write("x")
    counter.flush()
    run = counter.tell()  # which gate this is, however many start after it
status = pathlib.Path(sys.argv[2])
# The screen readings queuekeeper would log for the run, which a test writes beside the status (#293).
screen = pathlib.Path(sys.argv[2] + ".screen")
if screen.exists(): print(screen.read_text(), flush=True)
# A screen that locks during the run: the reading queuekeeper takes once the lane ends (#293).
if os.environ.get("FIXTURE_LOCK_DURING_RUN"):
    shutil.copy(os.environ["FIXTURE_LOCK_DURING_RUN"], os.environ["QUEUEKEEPER_SCREEN_IOREG"])
if status.exists(): sys.exit(int(status.read_text()))
# A tree with a file named hold gates until the test releases this run, or has gone (Stacked).
released = pathlib.Path(f"{sys.argv[1]}.release.{run}")
while pathlib.Path("hold").exists() and not released.exists() and pathlib.Path(sys.argv[1]).exists(): time.sleep(0.02)
# A tree with a file named slow gates for that many seconds, so that an entry can arrive meanwhile (mq.Stack).
if pathlib.Path("slow").exists(): time.sleep(float(pathlib.Path("slow").read_text()))
bad = sorted(path.name for path in pathlib.Path(".").glob("bad*"))
print("bad:", bad)
both = pathlib.Path("left").exists() and pathlib.Path("right").exists()
sys.exit(1 if bad or both else 0)
"""


def run(directory, *words):
    return subprocess.run(words, cwd=directory, check=True, capture_output=True, text=True).stdout.strip()


def accepting_lane_key(source, key):
    """Give a fixture's config parser one more allowed key for command lanes, in config.LANE_KEYS (#248)."""
    lane_keys = "LANE_KEYS = frozenset({"
    if source.count(lane_keys) != 1:
        raise AssertionError("expected exactly one LANE_KEYS definition in config.py")
    return source.replace(lane_keys, f"{lane_keys}{json.dumps(key)}, ")


# A Git that reads no account's configuration, signs as nobody's, and reaches only what is on this disk.
GIT_ALONE = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_ALLOW_PROTOCOL": "file",
             "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}


@functools.cache
def local_toolkit():
    """A copy of this toolkit, once per process, whose origin is a bare repository beside it, for a Fixture's lanes to
    run: each lane's cli.refresh_policy fetches the toolkit's origin/main, which from the real checkout needed GitHub
    and wrote that checkout's refs (vocaro-engineering#298). Lanes only read it, so that the tests can share it, and
    git ignores the bytecode they leave, so that it stays clean. Returns the checkout and the origin."""
    base = Path(tempfile.mkdtemp()).resolve()
    atexit.register(shutil.rmtree, base, ignore_errors=True)
    checkout, origin = base / "toolkit", base / "toolkit-origin.git"
    with patch.dict(os.environ, GIT_ALONE):
        run(base, "git", "init", "-q", "--bare", "-b", "main", str(origin))
        run(base, "git", "init", "-q", "-b", "main", str(checkout))
        for part in ("src/python", "plugins", "bin", "scripts"):
            shutil.copytree(ROOT / part, checkout / part, ignore=shutil.ignore_patterns("__pycache__", "build", "*.egg-info"))
        for name in ("queuekeeper.json", "queuekeeper-workspace.json", ".gitignore"):
            shutil.copy(ROOT / name, checkout / name)
        run(checkout, "git", "add", ".")
        run(checkout, "git", "commit", "-qm", "Toolkit")
        run(checkout, "git", "remote", "add", "origin", str(origin))
        run(checkout, "git", "push", "-q", "origin", "HEAD:main")
    return checkout, origin


def never_exec(command, env):
    """What a test's worker must never do: replace the test process (merge_queue.execute)."""
    raise AssertionError(f"the worker would exec {shlex.join(command)}")


class Replaced(Exception):
    """A worker's move to a newer toolkit, as a test's fake exec sees it: the command and environment it asked for."""
    def __init__(self, command, env):
        super().__init__(shlex.join(command))
        self.command, self.env = command, env


class Fixture(unittest.TestCase):
    ROOT_PREFIX = "tmp"

    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix=self.ROOT_PREFIX)).resolve()
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        guard = patch.object(mq, "execute", never_exec)
        guard.start()
        self.addCleanup(guard.stop)
        patcher = patch.dict(os.environ, {**GIT_ALONE,
                                          "QUEUEKEEPER_ROOT": str(local_toolkit()[0])})
        patcher.start()
        self.addCleanup(patcher.stop)
        # A settled host unless a test says otherwise, so that no retry waits on this Mac's real load.
        settled = patch.object(mq.resources, "pressure", return_value=None)
        self.pressure = settled.start()
        self.addCleanup(settled.stop)
        # No test reaches GitHub's tracker: a flake the queue records goes here (mq.hardening, #231).
        self.tracked = []
        tracker = patch.object(mq, "hardening", side_effect=lambda checkout, flake: (
            self.tracked.append(dict(flake)), ("https://example.invalid/fixture/issues/1", None))[1])
        tracker.start()
        self.addCleanup(tracker.stop)
        # A gate's busy share, read from no real Mac unless a test says otherwise (mq.busy_of, #235).
        reading = patch.object(mq, "busy_of", return_value=(25.0, 75.0, "this Mac"))
        self.busy_of = reading.start()
        self.addCleanup(reading.stop)
        # A failing lane's load, read from no real Mac unless a test says otherwise (mq.load_of, #273).
        load = patch.object(mq, "load_of", return_value=(3.0, 25.0, 75.0))
        self.load_of = load.start()
        self.addCleanup(load.stop)
        # Each Mac's memory, read from no real Mac unless a test says otherwise (mq.memory_findings).
        memory = patch.object(mq, "memory_findings", return_value=([], []))
        self.memory_findings = memory.start()
        self.addCleanup(memory.stop)
        # qk land runs no preflight lane before it queues unless a test says so (PreflightBeforeQueued, #283).
        unchecked = patch.object(mq, "preflight_before_queued")
        unchecked.start()
        self.addCleanup(unchecked.stop)
        self.counter, self.status = self.base / "gates", self.base / "status"
        self.origin = self.base / "origin.git"
        run(self.base, "git", "init", "-q", "--bare", "-b", "main", str(self.origin))
        self.repo = self.base / "project"
        run(self.base, "git", "clone", "-q", str(self.origin), str(self.repo))
        (self.repo / "scripts").mkdir()
        shutil.copy(ROOT / "scripts/qk", self.repo / "scripts/qk")
        (self.repo / "lane.py").write_text(LANE)
        # The agent entry a direct-main project keeps, which `qk docs check` reads.
        (self.repo / "AGENTS.md").write_text("# Fixture\n")
        (self.repo / "CLAUDE.md").write_text("@AGENTS.md\n")
        (self.repo / "queuekeeper.json").write_text(json.dumps({
            "schemaVersion": 1, "name": "fixture", "integration": "direct-main",
            "queue": {"carryOver": {"records/": "no lane reads it"}},
            "lanes": {"gate": {"command": ["python3", "lane.py", str(self.counter), str(self.status)]}}}))
        run(self.repo, "git", "add", ".")
        run(self.repo, "git", "commit", "-qm", "start")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")

    def config(self):
        return configuration.load(self.repo)

    def preflight_check(self, name="check"):
        """Give main CHECK, as check.py, and a lane name that runs it, listed in queue.preflight: a project's own
        preflight lane, which the queue runs first. Return main's configuration."""
        (self.repo / "check.py").write_text(CHECK)
        run(self.repo, "git", "add", "check.py")
        config = self.config()
        config["lanes"][name] = {"command": ["python3", "check.py"]}
        config.setdefault("queue", {})["preflight"] = [name]
        self.publish(config, f"Add the {name} preflight lane")
        return config

    def publish(self, config, message="Change the configuration"):
        """Commit this configuration in the clone's checkout and push it to main."""
        (self.repo / "queuekeeper.json").write_text(json.dumps(config))
        run(self.repo, "git", "add", "queuekeeper.json")
        run(self.repo, "git", "commit", "-q", "--allow-empty", "-m", message)
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")

    def commit(self, name, branch, content="x", start="origin/main"):
        """A commit of its own on a new branch, in the shared clone's object store."""
        run(self.repo, "git", "fetch", "-q", "origin")
        run(self.repo, "git", "checkout", "-q", "-B", branch, start)
        path = self.repo / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(content)
        run(self.repo, "git", "add", name)
        run(self.repo, "git", "commit", "-qm", f"Add {name}")
        return run(self.repo, "git", "rev-parse", "HEAD")

    def add(self, *branches, checked=True):
        """Queue branches with no worker started, so a test holds every order fixed. Unchecked, qk land's trial
        integration (mq.check) is skipped, as when main or the entries ahead change after it, so that a conflict
        reaches the round, whose send-back such a test exercises."""
        ids = []
        unchecked = patch.object(mq, "check") if not checked else contextlib.nullcontext()
        with patch.object(mq.Queue, "start"), patch("builtins.print"), unchecked:
            for branch in branches:
                mq.add(self.repo, branch)
                ids.append(next(e["id"] for e in reversed(mq.Queue(self.repo).all()) if e["branch"] == branch))
        return ids

    def work(self):
        return mq.run(self.repo)

    def state(self, identifier):
        return mq.Queue(self.repo).read(identifier)

    def main_files(self):
        run(self.repo, "git", "fetch", "-q", "origin")
        return set(run(self.repo, "git", "ls-tree", "--name-only", "origin/main").split())

    def gates(self):
        return len(self.counter.read_text()) if self.counter.exists() else 0

    @contextlib.contextmanager
    def remote_says(self, said, calls, command="fetch"):
        """Each of the numbered calls, from 1, of git's command fails with said, as git does when the network or
        the remote stops it; the rest run. Yields the calls made and the backoffs slept, which take no time."""
        real, seen, slept = mq.git, [], []
        def git(directory, *arguments, **options):
            if arguments and arguments[0] == command:
                seen.append(arguments)
                if len(seen) in calls:
                    done = subprocess.CompletedProcess(["git", *arguments], 128, "", said + "\n")
                    return mq.checked(done, arguments) if options.get("check") else done
            return real(directory, *arguments, **options)
        with patch.object(mq, "git", git), patch.object(mq, "pause", slept.append):
            yield seen, slept

    @contextlib.contextmanager
    def host(self, looks, then=None):
        """The host's pressure at each look as looks says, a reason while it is busy and None once it has
        settled, the last repeating, and a one-minute load of 40 while busy and 3 once settled; then runs at
        the first settled look. Each pause moves a fake clock rather than sleeping. Yields the pauses taken
        and what the worker said."""
        clock, slept, said, last = [1000.0], [], io.StringIO(), [None]
        def pressure(limit=None):
            last[0] = looks[min(self.pressure.call_count - 1, len(looks) - 1)]
            if last[0] is None and then: then()
            return last[0]
        def pause(seconds):
            slept.append(seconds)
            clock[0] += seconds
        self.pressure.side_effect = pressure
        with patch.object(mq, "now", lambda: clock[0]), patch.object(mq, "pause", pause), \
                patch.object(mq.resources, "one_minute_load", side_effect=lambda: 3.0 if last[0] is None else 40.0), \
                contextlib.redirect_stdout(said):
            yield slept, said
