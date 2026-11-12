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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src/python"))
from queuekeeper import cli, config as configuration, landing, merge_queue as mq, offload_host
# What eng land runs before it queues, which Fixture leaves out but SpelledBeforeQueued puts back.
SPELLED_BEFORE_QUEUED = mq.spelled_before_queued
import host_isolation
from host_isolation import stretched

# Each class in a process of its own under `eng unittest`'s default module split: whole, with a real Git
# fixture built for every test, this module alone bounded the toolkit's suite (guides/parallel-tests.md).
shard_by = "class"


# Fails when any tracked file is named bad*, passes otherwise; touches a counter so a test can count gates.
# A British spelling, built so this file holds none, that eng spelling fails on.
BRITISH = "a label" + "led point\n"
LANE = """import os, pathlib, shutil, sys, time
# Appended, so that two gates at once, a bisect's halves (mq.Beside), count as two.
with open(sys.argv[1], "a") as counter:
    counter.write("x")
    counter.flush()
    run = counter.tell()  # which gate this is, however many start after it
status = pathlib.Path(sys.argv[2])
# The screen readings eng would log for the run, which a test writes beside the status (#293).
screen = pathlib.Path(sys.argv[2] + ".screen")
if screen.exists(): print(screen.read_text(), flush=True)
# A screen that locks during the run: the reading eng takes once the lane ends (#293).
if os.environ.get("FIXTURE_LOCK_DURING_RUN"):
    shutil.copy(os.environ["FIXTURE_LOCK_DURING_RUN"], os.environ["VOCARO_SCREEN_IOREG"])
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
        for part in ("src/python", "policies", "agents", "integrations", "bin", "schemas"):
            shutil.copytree(ROOT / part, checkout / part, ignore=shutil.ignore_patterns("__pycache__"))
        for name in ("engineering.json", ".gitignore"):
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
        patcher = patch.dict(os.environ, {"VOCARO_OFFLOAD_HOST": "none", **GIT_ALONE,
                                          "VOCARO_ENGINEERING_ROOT": str(local_toolkit()[0])})
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
        # eng land runs no spelling lane before it queues unless a test says so (SpelledBeforeQueued, #283).
        unspelled = patch.object(mq, "spelled_before_queued")
        unspelled.start()
        self.addCleanup(unspelled.stop)
        self.counter, self.status = self.base / "gates", self.base / "status"
        self.origin = self.base / "origin.git"
        run(self.base, "git", "init", "-q", "--bare", "-b", "main", str(self.origin))
        self.repo = self.base / "project"
        run(self.base, "git", "clone", "-q", str(self.origin), str(self.repo))
        (self.repo / "scripts").mkdir()
        shutil.copy(ROOT / "scripts/eng", self.repo / "scripts/eng")
        (self.repo / "lane.py").write_text(LANE)
        # The agent entry a direct-main project keeps, which `eng docs check` reads.
        (self.repo / "AGENTS.md").write_text("# Fixture\n\n" + (ROOT / cli.SHARED_GUIDANCE).read_text() + "\n"
                                             + "".join(f"{line}\n" for line in cli.claude_imports({})))
        (self.repo / "CLAUDE.md").write_text(f"{cli.CLAUDE_ENTRY}\n")
        (self.repo / "engineering.json").write_text(json.dumps({
            "schemaVersion": 1, "name": "fixture", "integration": "direct-main",
            # Generated paths are opaque identities, including when embedded in fixture source.
            "spelling": {"exemptPhrases": {str(self.base): "the generated root is an opaque fixture identity"}},
            "queue": {"carryOver": {"records/": "no lane reads it"}},
            "lanes": {"gate": {"command": ["python3", "lane.py", str(self.counter), str(self.status)]}}}))
        run(self.repo, "git", "add", ".")
        run(self.repo, "git", "commit", "-qm", "start")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")

    def config(self):
        return configuration.load(self.repo)

    def publish(self, config, message="Change the configuration"):
        """Commit this configuration in the clone's checkout and push it to main."""
        (self.repo / "engineering.json").write_text(json.dumps(config))
        run(self.repo, "git", "add", "engineering.json")
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
        """Queue branches with no worker started, so a test holds every order fixed. Unchecked, eng land's trial
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


class Landing(Fixture):
    def test_advisory_queue_read_error_preserves_wait_status(self):
        parser = argparse.ArgumentParser()
        mq.land_arguments(parser)
        for status in (0, 1, mq.resources.BUSY):
            with self.subTest(status=status), patch.object(mq, "add", return_value="entry"), \
                    patch.object(mq, "wait", return_value=status), \
                    patch.object(mq.Queue, "all", side_effect=OSError("cannot read entries")), \
                    contextlib.redirect_stdout(io.StringIO()) as said:
                self.assertEqual(mq.land(self.repo, parser.parse_args(["--wait"])), status)
                self.assertIn("warning: could not read queue entries for landing budget checks: cannot read entries",
                              said.getvalue())

    def test_the_log_says_where_each_lane_ran_as_eng_run_chose(self):
        """The queue's lanes go where eng run sends every lane, by both Macs' capacity (vocaro-engineering#168), and
        its log says where each ran and why: here, by the receipt's reason; on the second Mac, by its dispatch."""
        self.commit("a", "a")
        [identifier] = self.add("a")
        with contextlib.redirect_stdout(io.StringIO()) as said:
            self.assertEqual(self.work(), 0)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertIn("gate exited 0 on this Mac (no second Mac is configured)", said.getvalue())
        # A dispatch record of a run on the second Mac names it; a receipt an earlier run wrote here does not.
        worker = mq.Worker.__new__(mq.Worker)
        worker.checkout = self.base / "checkout"
        receipt = worker.checkout / ".build/engineering/gate/source.json"
        receipt.parent.mkdir(parents=True)
        receipt.write_text(json.dumps({"ranOn": "this Mac", "hereBecause": "the second Mac is loaded (load 719.0, at or "
                                                                            "above its HEAVY_LOAD_LIMIT=7.5)"}))
        worker.ran_on = None
        self.assertEqual(worker.placed("gate", 0),
                         " on this Mac (the second Mac is loaded (load 719.0, at or above its HEAVY_LOAD_LIMIT=7.5))")
        self.assertEqual(worker.placed("gate", time.time() + 60), "")
        worker.ran_on = "build-host"
        self.assertEqual(worker.placed("gate", 0), " on build-host")
        worker.ran_on = None
        receipt.write_text("{")
        self.assertEqual(worker.placed("gate", 0), "")
        receipt.write_text(json.dumps({"ranOn": "second Mac"}))
        self.assertEqual(worker.placed("gate", 0), "")

    def test_a_gate_sees_sigint_at_its_default_from_a_worker_started_as_a_background_job(self):
        """A non-interactive shell starts `eng land &` with SIGINT and SIGQUIT ignored, and its worker passed that to
        every gate lane until the worker put back the defaults (vocaro/BreezEPUB#47). Without that reset, the
        negative control, the gate's command sees them ignored."""
        record = self.base / "signals"
        (self.repo / "signals.py").write_text(
            "import signal, sys\nwith open(sys.argv[1], 'a') as out:\n"
            "    out.write(' '.join('ignored' if signal.getsignal(n) == signal.SIG_IGN else 'default'\n"
            "                       for n in (signal.SIGINT, signal.SIGQUIT)) + '\\n')\n")
        run(self.repo, "git", "add", "signals.py")
        self.publish({"schemaVersion": 1, "name": "fixture", "integration": "direct-main",
                      "lanes": {"gate": {"command": ["python3", "signals.py", str(record)]}}})
        before = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGQUIT)}
        for number, handler in before.items(): self.addCleanup(signal.signal, number, handler)
        for reset, seen in ((contextlib.nullcontext(), "default default"),
                            (patch.object(mq.processes, "default_background_signals"), "ignored ignored")):
            with self.subTest(seen=seen):
                for number in before: signal.signal(number, signal.SIG_IGN)
                record.unlink(missing_ok=True)
                self.commit(f"change-{seen[0]}", f"change-{seen[0]}")
                [identifier] = self.add(f"change-{seen[0]}")
                with reset, contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(self.work(), 0)
                self.assertEqual(self.state(identifier)["state"], "landed")
                self.assertEqual(record.read_text(), seen + "\n")

    def test_one_gate_lands_a_whole_batch(self):
        self.commit("a", "a"); self.commit("b", "b"); self.commit("c", "c")
        ids = self.add("a", "b", "c")
        self.assertEqual(self.work(), 0)
        self.assertEqual([self.state(i)["state"] for i in ids], ["landed"] * 3)
        self.assertEqual(self.gates(), 1)
        self.assertLessEqual({"a", "b", "c"}, self.main_files())
        # Not one merge, and each change its own commit, with its message.
        log = run(self.repo, "git", "log", "--format=%s", "origin/main").splitlines()
        self.assertEqual(log[:3], ["Add c", "Add b", "Add a"])
        self.assertEqual(run(self.repo, "git", "rev-list", "--merges", "origin/main"), "")
        # What was pushed is what was tested: nothing else moved, and the ref is gone.
        self.assertEqual(run(self.repo, "git", "for-each-ref", "refs/eng-queue"), "")

    def test_the_branch_moving_after_add_changes_nothing(self):
        self.commit("a", "a")
        [identifier] = self.add("a")
        self.commit("later", "a", start="a")
        self.work()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertNotIn("later", self.main_files())

    def test_a_bad_commit_comes_back_and_the_rest_land(self):
        self.commit("a", "a"); self.commit("bad", "bad"); self.commit("c", "c"); self.commit("d", "d")
        ids = self.add("a", "bad", "c", "d")
        self.assertEqual(self.work(), 0)
        states = [self.state(i)["state"] for i in ids]
        self.assertEqual(states, ["landed", "failed", "landed", "landed"])
        self.assertNotIn("bad", self.main_files())
        self.assertLessEqual({"a", "c", "d"}, self.main_files())
        failed = self.state(ids[1])
        self.assertIn("bad: ['bad']", Path(failed["log"]).read_text())
        # all four fail, [a bad] fail, [a] passes, [bad] fails, then [c d] pass: not one gate per commit plus.
        self.assertEqual(self.gates(), 5)
        self.assertTrue(run(self.repo, "git", "rev-parse", "--verify", f"refs/eng-queue/{ids[1]}"))

    def test_two_that_pass_alone_and_fail_together(self):
        self.commit("left", "left"); self.commit("right", "right")
        ids = self.add("left", "right")
        self.work()
        self.assertEqual([self.state(i)["state"] for i in ids], ["landed", "failed"])
        self.assertNotIn("right", self.main_files())

    def test_a_conflict_comes_back_without_costing_the_others_a_gate(self):
        self.commit("same", "one", "one"); self.commit("same", "two", "two"); self.commit("c", "c")
        ids = self.add("one", "two", "c", checked=False)
        self.work()
        self.assertEqual([self.state(i)["state"] for i in ids], ["landed", "conflict", "landed"])
        self.assertIn("same", self.state(ids[1])["why"])
        # Its author learns which entry to wait for before rebasing (#130), and that it alone conflicts (#247).
        self.assertIn(f"conflicts with {ids[0]}, ahead of it, not with origin/main, in same: once it lands, rebase",
                      self.state(ids[1])["why"])
        self.assertNotIn(ids[2], self.state(ids[1])["why"])
        self.assertEqual(self.gates(), 1)

    def test_a_conflict_with_main_alone_names_no_entry(self):
        start = run(self.repo, "git", "rev-parse", "origin/main")
        self.commit("same", "one", "one")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        self.commit("same", "two", "two", start)
        identifier, = self.add("two", checked=False)
        self.work()
        why = self.state(identifier)["why"]
        self.assertIn("conflicts with origin/main, in same: rebase onto origin/main", why)
        self.assertNotIn("once those land", why)

    def test_a_conflict_with_main_behind_an_entry_that_does_not_touch_its_file_names_main(self):
        """Negative control of #247: an entry ahead that changes other files is not what it conflicts with."""
        start = run(self.repo, "git", "rev-parse", "origin/main")
        self.commit("same", "one", "one")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        self.commit("c", "c")
        self.commit("same", "two", "two", start)
        ids = self.add("c", "two", checked=False)
        self.work()
        why = self.state(ids[1])["why"]
        self.assertEqual([self.state(i)["state"] for i in ids], ["landed", "conflict"])
        self.assertIn("conflicts with origin/main, in same: rebase onto origin/main", why)
        self.assertNotIn(ids[0], why)

    def test_a_conflict_with_main_and_an_entry_ahead_names_both(self):
        """When origin/main has changed the file since the commit's parent as well as an entry ahead, both may be
        what it conflicts with, as before #247."""
        start = run(self.repo, "git", "rev-parse", "origin/main")
        self.commit("same", "main", "main")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        self.commit("same", "one", "one")
        self.commit("same", "two", "two", start)
        ids = self.add("one", "two", checked=False)
        self.work()
        why = self.state(ids[1])["why"]
        self.assertEqual([self.state(i)["state"] for i in ids], ["landed", "conflict"])
        self.assertIn(f"conflicts with what is ahead of it, origin/main and {ids[0]}, in same: once those land", why)

    def test_a_commit_main_already_holds_needs_no_gate(self):
        tip = self.commit("a", "a")
        run(self.repo, "git", "push", "-q", "origin", f"{tip}:main")
        self.commit("b", "b")
        with patch("builtins.print"):
            self.assertIsNone(mq.add(self.repo, "a"))
        self.assertEqual(mq.Queue(self.repo).all(), [])

    REFUSED = "is a merge, which a queue would have to put on main: rebase the branch's own commits onto origin/main"

    def refused(self, branch):
        """What eng land refuses branch with, having queued nothing."""
        with self.assertRaises(ValueError) as caught, patch("builtins.print"):
            mq.add(self.repo, branch)
        self.assertEqual(mq.Queue(self.repo).all(), [])
        return str(caught.exception)

    def test_a_merge_that_brings_in_another_branchs_commits_is_refused_naming_them(self):
        """A merge of another branch's unlanded work stays refused (#217), and the refusal names what that merge
        brings in, not the branch's own commit or what main holds, and says how to carry it (#262)."""
        other = self.commit("a", "a")
        second = self.commit("a2", "a", start="a")
        own = self.commit("b", "b", start="origin/main")
        run(self.repo, "git", "merge", "-q", "--no-edit", "a")
        merge = run(self.repo, "git", "rev-parse", "HEAD")
        said = self.refused("b")
        self.assertEqual(said, f"{merge[:12]} is a merge that brings in 2 commits origin/main lacks: {other[:12]} Add a; "
                               f"{second[:12]} Add a2. A queue would have to put it on main: take it off the branch and "
                               "cherry-pick those commits onto it, or wait until their own branch lands and merge "
                               "origin/main, and add the branch again")
        self.assertNotIn(own[:12], said)

    def test_a_long_list_of_merged_in_commits_is_bounded_and_counted(self):
        tips = [self.commit("a0", "a")]
        for number in range(1, mq.MERGED_IN_LISTED + 2):
            tips.append(self.commit(f"a{number}", "a", start="a"))
        self.commit("b", "b", start="origin/main")
        run(self.repo, "git", "merge", "-q", "--no-edit", "a")
        said = self.refused("b")
        self.assertIn(f"brings in {mq.MERGED_IN_LISTED + 2} commits origin/main lacks", said)
        self.assertIn("; and 2 more not listed here. A queue", said)
        self.assertEqual([tip[:12] in said for tip in tips], [True] * mq.MERGED_IN_LISTED + [False, False])

    def test_two_merges_name_what_each_brings_in_once(self):
        """The second merge's branch holds the first's commit, which the refusal lists once."""
        first = self.commit("a", "a")
        second = self.commit("c", "c", start="a")
        self.commit("b", "b", start="origin/main")
        run(self.repo, "git", "merge", "-q", "--no-edit", "a")
        one = run(self.repo, "git", "rev-parse", "HEAD")
        run(self.repo, "git", "merge", "-q", "--no-edit", "c")
        two = run(self.repo, "git", "rev-parse", "HEAD")
        self.assertIn(f"{two[:12]}, {one[:12]} are merges that bring in 2 commits origin/main lacks: "
                      f"{first[:12]} Add a; {second[:12]} Add c. A queue would have to put them on main",
                      self.refused("b"))

    def test_a_merge_of_a_branch_main_holds_under_another_sha_keeps_the_rebase_advice(self):
        """A negative control: the queue lands a commit under another SHA, so merging the landed branch brings in no
        patch main lacks, and is still refused. Cherry-picking would add nothing, so the refusal does not say to."""
        start = run(self.repo, "git", "rev-parse", "origin/main")
        self.commit("elsewhere", "main-work")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        self.commit("a", "a", start=start)
        self.commit("elsewhere2", "landed")
        run(self.repo, "git", "cherry-pick", "a")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        self.commit("b", "b", start="origin/main")
        run(self.repo, "git", "merge", "-q", "--no-edit", "a")
        said = self.refused("b")
        self.assertRegex(said, self.REFUSED)
        self.assertNotIn("cherry-pick", said)

    def synced(self, branch, name, content="x"):
        """branch with a commit adding name, refreshed by merging origin/main after main moves, as the app's
        sync does, and then a commit after the merge. Returns the branch's tip and the merge."""
        self.commit(name, branch, content)
        self.commit("elsewhere", "main-work", start="origin/main")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        run(self.repo, "git", "checkout", "-q", branch)
        run(self.repo, "git", "fetch", "-q", "origin")
        run(self.repo, "git", "merge", "-q", "--no-edit", "origin/main")
        merge = run(self.repo, "git", "rev-parse", "HEAD")
        self.commit("after", branch, start=branch)
        return run(self.repo, "git", "rev-parse", "HEAD"), merge

    def test_a_branch_refreshed_by_merging_main_lands_linear(self):
        """The app's sync merges origin/main into a session's branch: eng land replays the branch's own commits
        onto origin/main, lands them with no merge, and leaves the branch alone, whose work the landing check
        then finds on main (#217)."""
        tip, merge = self.synced("b", "b")
        with patch.object(mq.Queue, "start"), patch("builtins.print") as said:
            identifier = mq.add(self.repo, "b")
        lines = [" ".join(map(str, call.args)) for call in said.call_args_list]
        self.assertEqual(len([line for line in lines if f"dropped {merge[:12]}, a merge of origin/main" in line]), 1)
        entry = self.state(identifier)
        self.assertEqual((entry["replays"], entry["branch"], len(entry["commits"])), (tip, "b", 2))
        self.assertEqual(run(self.repo, "git", "rev-parse", "b"), tip)
        self.assertEqual(self.work(), 0)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertLessEqual({"b", "after", "elsewhere"}, self.main_files())
        self.assertEqual(run(self.repo, "git", "rev-list", "--merges", "origin/main"), "")
        self.assertEqual(run(self.repo, "git", "log", "--format=%s", "-3", "origin/main").splitlines(),
                         ["Add after", "Add b", "Add elsewhere"])
        # Closeout and eng disk's proof: the branch, merge and all, is on main as equal patches.
        self.assertEqual(mq.landing.contained(lambda *words: mq.git(self.repo, *words), "origin/main", tip),
                         mq.landing.PATCH_EQUIVALENT)

    def test_a_refreshed_branch_whose_replay_conflicts_is_refused(self):
        """A negative control: main changes the branch's file after the sync, so its commits do not replay."""
        self.synced("b", "same", "branch")
        self.commit("same", "main-again", "main", start="origin/main")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        said = self.refused("b")
        self.assertRegex(said, self.REFUSED)
        self.assertNotIn("cherry-pick", said)
        self.assertEqual(run(self.repo, "git", "worktree", "list", "--porcelain").count("worktree "), 1)

    def test_a_sync_that_settled_a_conflict_is_refused(self):
        """A negative control: a merge of main whose tree no clean merge makes carries a change of its own, which
        neither the replay nor the landing check could see."""
        start = run(self.repo, "git", "rev-parse", "origin/main")
        self.commit("same", "main-work", "main")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        self.commit("same", "b", "branch", start=start)
        subprocess.run(["git", "merge", "-q", "--no-edit", "origin/main"], cwd=self.repo, capture_output=True)
        (self.repo / "same").write_text("both")
        run(self.repo, "git", "commit", "-qam", "Merge origin/main")
        tip = run(self.repo, "git", "rev-parse", "HEAD")
        said = self.refused("b")
        self.assertRegex(said, self.REFUSED)
        self.assertNotIn("cherry-pick", said)
        self.assertIsNone(mq.landing.contained(lambda *words: mq.git(self.repo, *words), "origin/main", tip))

    def test_adding_a_branch_again_replaces_it(self):
        self.commit("a", "a")
        [first] = self.add("a")
        self.commit("a2", "a", start="a")
        with patch.object(mq.Queue, "start"), patch("builtins.print"):
            mq.add(self.repo, "a")
        self.assertEqual(self.state(first)["state"], "removed")
        self.work()
        self.assertLessEqual({"a", "a2"}, self.main_files())
        self.assertEqual(self.gates(), 1)


class CheckedWhenQueued(Fixture):
    """eng land's trial integration finds a conflict with the entries ahead when the entry is queued, not when its
    round starts (vocaro-engineering#178)."""
    def land(self, branch, **options):
        """(the new entry's ID or None, what eng land said, the error it refused with or None), no worker started."""
        said, error = io.StringIO(), None
        with patch.object(mq.Queue, "start"), contextlib.redirect_stdout(said), contextlib.redirect_stderr(said):
            try: mq.add(self.repo, branch, **options)
            except ValueError as refused: error = str(refused)
        identifier = next((e["id"] for e in reversed(mq.Queue(self.repo).all())
                           if e["branch"] == branch and e["state"] in mq.WAITING), None)
        return identifier, said.getvalue(), error

    def worktrees(self):
        return run(self.repo, "git", "worktree", "list", "--porcelain").count("worktree ")

    def test_a_conflict_with_an_entry_ahead_is_refused_and_nothing_is_queued(self):
        self.commit("same", "one", "one"); self.commit("c", "c"); self.commit("same", "two", "two")
        ahead = self.add("one", "c")
        before = self.worktrees()
        identifier, _, error = self.land("two")
        self.assertIsNone(identifier)
        self.assertEqual([e["id"] for e in mq.Queue(self.repo).all()], ahead)
        # for-each-ref sorts by name, and two entries made in one second sort by their random suffix, not their order.
        self.assertEqual(sorted(run(self.repo, "git", "for-each-ref", "--format=%(refname)", "refs/eng-queue").split()),
                         sorted(f"refs/eng-queue/{i}" for i in ahead))
        # The files and the entries: all it was checked against, in order, and the one that changes the file.
        self.assertIn("is not queued", error)
        # Only the entry that changes the file, since origin/main holds it as the commit's parent did (#247).
        self.assertIn(f"conflicts with {ahead[0]}, ahead of it, not with origin/main, in same: once it lands", error)
        self.assertIn(f"and, in order, {ahead[0]}, {ahead[1]}; {ahead[0]} changes the files in conflict", error)
        self.assertNotIn(f"{ahead[1]} change", error)
        # The scratch worktree is gone, and Git knows of no other.
        self.assertEqual(self.worktrees(), before)
        self.assertEqual(run(self.repo, "git", "worktree", "list", "--porcelain").count("locked"), 0)
        # The session's checkout is untouched: still on the branch it was, at its tip.
        self.assertEqual(run(self.repo, "git", "symbolic-ref", "--short", "HEAD"), "two")
        self.assertEqual(run(self.repo, "git", "status", "--porcelain"), "")

    def test_an_entry_that_applies_is_queued_as_now(self):
        self.commit("a", "a"); self.commit("b", "b"); self.commit("c", "c")
        ahead = self.add("a", "b")
        before = self.worktrees()
        identifier, said, error = self.land("c")
        self.assertIsNone(error)
        self.assertEqual(self.state(identifier)["state"], "queued")
        self.assertIn(f"it applies onto origin/main at", said)
        self.assertIn(f"after {ahead[0]}, {ahead[1]}, as a round would integrate it now", said)
        self.assertEqual(self.worktrees(), before)
        self.assertEqual(self.work(), 0)
        self.assertEqual([self.state(i)["state"] for i in [*ahead, identifier]], ["landed"] * 3)
        self.assertEqual(self.gates(), 1)

    def test_an_entry_that_conflicts_with_main_alone_is_refused_naming_no_entry(self):
        start = run(self.repo, "git", "rev-parse", "origin/main")
        self.commit("same", "one", "one")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        self.commit("same", "two", "two", start)
        identifier, _, error = self.land("two")
        self.assertIsNone(identifier)
        self.assertIn("conflicts with origin/main, in same: rebase onto origin/main", error)
        self.assertIn("Checked against origin/main at", error)
        self.assertNotIn("in order", error)

    def test_a_changelog_conflict_the_worker_settles_is_not_refused(self):
        start = "# Release notes\n\n## Unreleased\n\n- old\n"
        self.commit("CHANGELOG.md", "start", start)
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        for name in ("a", "b"): self.commit("CHANGELOG.md", name, start.replace("- old", f"- {name}\n- old"))
        [first] = self.add("a")
        identifier, said, error = self.land("b")
        self.assertIsNone(error)
        self.assertIn("a round would settle", said)
        self.assertIn("kept both sides of CHANGELOG.md, the new lines first", said)
        self.work()
        self.assertEqual([self.state(i)["state"] for i in (first, identifier)], ["landed"] * 2)
        run(self.repo, "git", "fetch", "-q", "origin")
        self.assertIn("- b\n- a\n- old", run(self.repo, "git", "show", "origin/main:CHANGELOG.md"))

    def test_a_changelog_conflict_the_worker_does_not_settle_is_refused(self):
        start = "# Release notes\n\n## Unreleased\n\n- old\n"
        self.commit("CHANGELOG.md", "start", start)
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        for name in ("a", "b"): self.commit("CHANGELOG.md", name, start.replace("- old", f"- {name}"))
        [first] = self.add("a")
        identifier, _, error = self.land("b")
        self.assertIsNone(identifier)
        self.assertIn(f"in CHANGELOG.md:", error)
        self.assertIn(f"{first} changes the files in conflict", error)

    def test_a_front_entry_is_checked_against_main_alone(self):
        """--front tests it alone onto main, so the entries behind it are no part of its check; the one behind it
        that its landing makes conflict comes back from its round, as before."""
        self.commit("same", "one", "one"); self.commit("same", "fix", "fix")
        [behind] = self.add("one")
        identifier, said, error = self.land("fix", front=True, why="a fix every entry needs")
        self.assertIsNone(error)
        self.assertIn(", alone, as --front tests it", said)
        self.assertNotIn(behind, said)
        self.work()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(self.state(behind)["state"], "conflict")
        # A front entry that conflicts with main itself is refused.
        start = run(self.repo, "git", "rev-parse", "origin/main~1")
        self.commit("same", "late", "late", start)
        late, _, error = self.land("late", front=True)
        self.assertIsNone(late)
        self.assertIn("conflicts with origin/main, in same", error)

    def test_the_round_still_sends_back_what_the_check_could_not_see(self):
        start = run(self.repo, "git", "rev-parse", "origin/main")
        self.commit("same", "two", "two"); self.commit("c", "c")
        ids = self.add("two", "c")
        # main moves after the check, by a hand push.
        self.commit("same", "pushed", "pushed", start)
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        self.work()
        self.assertEqual([self.state(i)["state"] for i in ids], ["conflict", "landed"])
        self.assertIn("conflicts with origin/main, in same", self.state(ids[0])["why"])

    def test_an_entry_ahead_that_a_round_would_leave_out_is_named_and_not_held_against_it(self):
        start = run(self.repo, "git", "rev-parse", "origin/main")
        self.commit("same", "ahead", "ahead")
        [ahead] = self.add("ahead")
        # main moves under the entry ahead, which its round will send back.
        self.commit("same", "pushed", "pushed", start)
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        self.commit("same", "behind", "behind")
        identifier, said, error = self.land("behind")
        self.assertIsNone(error)
        self.assertIn(f"left out of the check, as a round would leave it: {ahead}, ", said)
        self.assertIn("conflicts with origin/main, in same", said)
        # And named in a refusal too.
        self.commit("same", "third", "third")
        third, _, error = self.land("third")
        self.assertIsNone(third)
        self.assertIn(f"in order, {identifier}; {identifier} changes the files in conflict; left out, as a round "
                      f"would leave it: {ahead}", error)

    def test_the_entries_ahead_are_taken_in_the_order_rounds_land_them(self):
        entries = [{"id": "q-old", "state": "queued", "submitted": 1}, {"id": "t", "state": "testing", "submitted": 3},
                   {"id": "front", "state": "queued", "submitted": 4, "front": True},
                   {"id": "p", "state": "passed", "submitted": 5}, {"id": "q-new", "state": "queued", "submitted": 2}]
        self.assertEqual([e["id"] for e in mq.round_order(entries)], ["p", "t", "front", "q-old", "q-new"])

    def test_a_branch_added_again_is_not_checked_against_what_it_replaces(self):
        self.commit("same", "a", "one")
        [first] = self.add("a")
        self.commit("same", "a", "rewritten")
        identifier, said, error = self.land("a")
        self.assertIsNone(error)
        self.assertEqual(self.state(first)["state"], "removed")
        self.assertEqual(self.state(identifier)["state"], "queued")

    def test_a_trial_that_cannot_be_made_queues_the_entry(self):
        self.commit("a", "a")
        with patch.object(mq, "trial", side_effect=ValueError("`git worktree add` exited 128: no space")):
            identifier, said, error = self.land("a")
        self.assertIsNone(error)
        self.assertEqual(self.state(identifier)["state"], "queued")
        self.assertIn("could not check it against the entries ahead, so the round finds any conflict: `git worktree "
                      "add` exited 128: no space", said)

    def test_the_scratch_worktree_goes_when_the_trial_fails_midway(self):
        self.commit("a", "a")
        before = self.worktrees()
        with patch.object(mq.Trial, "integrate", side_effect=RuntimeError("stopped")):
            identifier, said, _ = self.land("a")
        self.assertIn("stopped", said)
        self.assertEqual(self.worktrees(), before)
        self.assertEqual(self.state(identifier)["state"], "queued")

    def test_eng_land_and_eng_queue_add_exit_1_for_a_refusal_as_for_an_entry_that_came_back(self):
        import argparse
        self.commit("same", "one", "one"); self.commit("same", "two", "two")
        self.add("one")
        # Without --wait: with it, a refusal for an entry ahead alone waits for that entry (#302, LandWaitsForAhead).
        for adder, words, call in ((mq.land_arguments, ["two"], mq.land),
                                   (mq.add_arguments, ["add", "two"], mq.main)):
            parser = argparse.ArgumentParser()
            adder(parser)
            said = io.StringIO()
            # A queued entry's wait would last as long as no worker runs: here it ends at once, as for a landing.
            with patch.object(mq.Queue, "start"), patch.object(mq, "wait", return_value=0), \
                    contextlib.redirect_stdout(said), contextlib.redirect_stderr(said):
                self.assertEqual(call(self.repo, parser.parse_args(words)), 1)
            self.assertIn("eng: ", said.getvalue())
            self.assertIn("is not queued", said.getvalue())
            self.assertEqual(len(mq.Queue(self.repo).all()), 1)

    def test_a_refusal_names_the_command_that_waits_for_the_entries_and_the_lines_in_conflict(self):
        """#304: the entries that change the files in conflict, in an `eng queue wait` ready to run; #302: the commit's
        own lines that conflict, from the trial. A conflict with origin/main alone, the negative control, names no
        wait, since nothing ahead is worth waiting for."""
        self.commit("same", "one", "a\nb\nc\nd\n"); self.commit("other", "o", "o\n")
        self.commit("same", "two", "a\nB\nC\nd\n")
        ahead = self.add("one", "o")
        identifier, _, error = self.land("two")
        self.assertIsNone(identifier)
        self.assertIn(f"/scripts/eng queue wait {ahead[0]}, which says whether it landed, so rebase, or came back",
                      error)
        self.assertNotIn(f"queue wait {ahead[0]} {ahead[1]}", error)
        self.assertIn("`eng land --wait` waits for it itself and checks again", error)
        # Of the file both add, only the lines that differ: its second and third.
        self.assertIn("its lines in conflict: same:2-3. ", error)
        start = run(self.repo, "git", "rev-parse", "origin/main")
        self.commit("main", "pushed", "pushed")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        self.commit("main", "late", "late", start)
        identifier, _, error = self.land("late")
        self.assertIsNone(identifier)
        self.assertIn("conflicts with origin/main, in main: rebase", error)
        self.assertIn("its lines in conflict: main:1", error)
        self.assertNotIn("queue wait", error)

    def test_conflict_hunks_are_the_commits_own_lines(self):
        checkout = self.base / "hunks"
        checkout.mkdir()
        (checkout / "f").write_text("keep\n<<<<<<< HEAD\nahead\n=======\nmine 1\nmine 2\n>>>>>>> abc (Mine)\nkeep\n"
                                    "<<<<<<< HEAD\nahead\n||||||| parent\nold\n=======\n>>>>>>> abc (Mine)\nlast\n"
                                    "<<<<<<< HEAD\n=======\nmine 3\n>>>>>>> abc (Mine)\n")
        (checkout / "g").write_text("no blocks\n")
        self.assertEqual(mq.conflict_hunks(checkout, ["f", "g", "gone"]), ["f:2-3", "f before line 5", "f:6"])


    def test_eng_land_from_another_repository_refuses_and_from_a_worktree_or_none_lands(self):
        """Each project's launcher lands its own repository: another project's eng land, run here, would
        queue that project's HEAD or report it landed, exit 0 (vocaro-engineering#210)."""
        import argparse
        parser = argparse.ArgumentParser()
        mq.land_arguments(parser)
        other = self.base / "other"
        run(self.base, "git", "init", "-q", str(other))
        worktree = self.base / "worktree"
        run(self.repo, "git", "worktree", "add", "-q", "--detach", str(worktree))
        outside = self.base / "outside"
        outside.mkdir()
        self.commit("a", "a")
        said = io.StringIO()
        with patch.object(mq.Queue, "start"), contextlib.redirect_stdout(said), contextlib.redirect_stderr(said):
            self.assertEqual(mq.land(self.repo, parser.parse_args(["a"]), other / "."), 1)
        self.assertIn(f"eng: this eng lands {self.repo}, but the current directory is in {other.resolve()}",
                      said.getvalue())
        self.assertEqual(mq.Queue(self.repo).all(), [])
        for directory in (worktree, outside, self.repo):
            with patch.object(mq.Queue, "start"), patch("builtins.print"), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(mq.land(self.repo, parser.parse_args(["a"]), directory), 0, directory)
        self.assertEqual(len(mq.Queue(self.repo).all()), 1)


class LandWaitsForAhead(Fixture):
    """`eng land --wait` refused for entries ahead alone waits for them and checks again against the whole queue
    (vocaro-engineering#302), and `eng queue wait` takes several entries (#304). No worker starts by itself: each
    wait runs the round a worker would."""
    def land(self, branch, waits=None):
        """(exit status, what eng land --wait said, the IDs each wait for entries ahead was given). waits, by the call's
        number from 1, is what to do before that wait looks; by default a worker's round."""
        import argparse
        parser = argparse.ArgumentParser()
        mq.land_arguments(parser)
        real_wait, real_wait_for, given = mq.wait, mq.wait_for, []
        def wait_for(root, identifiers, limit=mq.WAIT_LIMIT):
            given.append(list(identifiers))
            (waits or {}).get(len(given), self.work)()
            return real_wait_for(root, identifiers, limit)
        def wait(root, identifier, limit=mq.WAIT_LIMIT):
            self.work()
            return real_wait(root, identifier, limit)
        said = io.StringIO()
        with patch.object(mq.Queue, "start"), patch.object(mq, "wait_for", wait_for), patch.object(mq, "wait", wait), \
                self.polls(lambda: self.fail("a wait polled: nothing would end it")), \
                contextlib.redirect_stdout(said), contextlib.redirect_stderr(said):
            status = mq.land(self.repo, parser.parse_args([branch, "--wait"]))
        return status, said.getvalue(), given

    @staticmethod
    def polls(step):
        """A wait's poll, a sleep of mq.POLL, runs step instead; every other sleep, as subprocess's own, is real."""
        real = time.sleep
        return patch.object(mq.time, "sleep", lambda seconds: step() if seconds == mq.POLL else real(seconds))

    def test_landing_budget_warning_reads_main_after_a_new_lane_key_lands(self):
        config = self.config()
        config["lanes"]["device-voiceover"] = {
            "command": ["true"], "devices": ["iPad"], "here": "the fixture device", "deviceRunMinutes": 90}
        config["release"] = {"lanes": ["device-voiceover"], "maxCommits": 1}
        self.commit("fix", "fix")
        real_wait = mq.wait

        def wait(root, identifier, limit):
            result = real_wait(root, identifier, limit)
            run(self.repo, "git", "checkout", "-q", "--detach", "origin/main")
            self.publish(config, "Add a release lane with a new key")
            with self.assertRaisesRegex(ValueError, "Invalid lane device-voiceover"):
                mq.config_at(root, mq.MAIN)
            return result

        with patch.object(mq, "wait", wait), \
                patch.object(configuration, "LANE_KEYS", configuration.LANE_KEYS - {"deviceRunMinutes"}):
            result, said, _ = self.land("fix")
        self.assertEqual(result, 0, said)
        self.assertIn("landed:", said)
        self.assertIn("(device-voiceover), over release.maxCommits, 1", said)

    def test_behind_an_entry_in_its_gate_that_fails_it_is_queued_and_lands_without_a_second_command(self):
        self.commit("same", "one", "one"); self.commit("bad", "one", "bad", start="one")
        self.commit("same", "two", "two")
        [ahead] = self.add("one")
        mq.Queue(self.repo).mark([self.state(ahead)], mq.TESTING)
        status, said, given = self.land("two")
        self.assertEqual(status, 0, said)
        self.assertEqual(given, [[ahead]])
        self.assertIn(f"conflicts with {ahead}, ahead of it, not with origin/main", said)
        self.assertIn(f"eng land --wait: waits for {ahead}, then checks again against the whole queue", said)
        self.assertIn(f"{ahead} came back", said)
        self.assertEqual(self.state(ahead)["state"], "failed")
        mine = next(e for e in mq.Queue(self.repo).all() if e["branch"] == "two")
        self.assertEqual(mine["state"], "landed")
        self.assertIn(f"landed: {mine['landedAs']}", said)

    def test_behind_an_entry_that_lands_and_still_conflicts_it_is_refused_saying_to_rebase(self):
        """The negative control: once the entry ahead lands, the conflict is with origin/main, which no wait ends."""
        self.commit("same", "one", "one"); self.commit("same", "two", "two")
        [ahead] = self.add("one")
        status, said, given = self.land("two")
        self.assertEqual(status, 1, said)
        self.assertEqual(given, [[ahead]])
        self.assertEqual(self.state(ahead)["state"], "landed")
        self.assertIn("all landed: rebase onto origin/main", said)
        self.assertIn("conflicts with origin/main, in same: rebase onto origin/main and add it again", said)
        self.assertFalse(any(e["branch"] == "two" for e in mq.Queue(self.repo).all()))

    def test_a_conflict_with_origin_main_is_refused_at_once_with_no_wait(self):
        start = run(self.repo, "git", "rev-parse", "origin/main")
        self.commit("same", "one", "one")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        self.commit("same", "two", "two", start)
        status, said, given = self.land("two")
        self.assertEqual((status, given), (1, []), said)
        self.assertNotIn("eng land --wait: waits", said)

    def test_each_wake_checks_against_the_whole_queue_and_waits_for_an_entry_queued_meanwhile(self):
        """#302's second case: while it waited, another entry touching the same lines was queued; the check after the
        wake names it, and the wait goes on for it."""
        self.commit("same", "one", "one"); self.commit("same", "two", "two"); self.commit("same", "three", "three")
        [first] = self.add("one")
        meanwhile = []
        def first_wait():
            mq.Queue(self.repo).settle(self.state(first), mq.FAILED, why="its gate failed")
            meanwhile.extend(self.add("three", checked=False))
        status, said, given = self.land("two", waits={1: first_wait})
        self.assertEqual(given, [[first], meanwhile])
        self.assertEqual(self.state(meanwhile[0])["state"], "landed")
        # And once that one landed, its conflict is with origin/main: rebase.
        self.assertEqual(status, 1, said)
        self.assertIn("conflicts with origin/main, in same: rebase", said)

    def test_eng_queue_wait_takes_several_entries_and_a_return_to_queued_keeps_it_going(self):
        self.commit("a", "a"); self.commit("bad", "b")
        ids = self.add("a", "b")
        queue = mq.Queue(self.repo)
        # Each poll moves the queue one step: a gate starts, a --front entry sends it back to queued, a round settles.
        steps = [lambda: queue.mark([self.state(ids[0])], mq.TESTING),
                 lambda: queue.mark([self.state(ids[0])], mq.QUEUED), self.work]
        slept = []
        def poll():
            slept.append(mq.POLL)
            steps[len(slept) - 1]()
        import argparse
        parser = argparse.ArgumentParser()
        mq.add_arguments(parser)
        said = io.StringIO()
        with patch.object(mq.Queue, "start"), self.polls(poll), contextlib.redirect_stdout(said):
            status = mq.main(self.repo, parser.parse_args(["wait", *ids]))
        self.assertEqual(len(slept), 3)
        self.assertEqual(status, 1, said.getvalue())
        self.assertEqual([self.state(i)["state"] for i in ids], ["landed", "failed"])
        self.assertIn(f"{ids[1]} came back, so a commit that conflicted with it may apply as it is", said.getvalue())
        # Each landed: 0, saying to rebase. An unknown ID is refused before any wait.
        self.commit("c", "c"); self.commit("d", "d")
        more = self.add("c", "d")
        with patch.object(mq.Queue, "start"), self.polls(self.work), \
                contextlib.redirect_stdout(io.StringIO()) as landed:
            self.assertEqual(mq.wait_for(self.repo, more), 0)
        self.assertIn("all landed: rebase onto origin/main", landed.getvalue())
        with self.assertRaisesRegex(ValueError, "The queue has no entry nobody"):
            mq.wait_for(self.repo, [more[0], "nobody"])
        # Held, while one still waits: 75.
        self.commit("e", "e")
        [held] = self.add("e")
        mq.Queue(self.repo).hold("for a test")
        with patch.object(mq.Queue, "start"), contextlib.redirect_stdout(io.StringIO()) as waiting:
            self.assertEqual(mq.wait_for(self.repo, [held, more[0]]), mq.resources.BUSY)
        self.assertIn(f"{held} (queued) waiting, and the queue is held", waiting.getvalue())


ISOLATED ="import unittest\nimport host_isolation\n\nclass T(unittest.TestCase):\n    def test_t(self): pass\n"
UNISOLATED = "import unittest\n\nclass T(unittest.TestCase):\n    def test_t(self): pass\n"
# What the gate of 2026-11-07 was refused with, in the checkout it ran in (vocaro-engineering#280, #290).
REFUSED_LINE = ("eng unittest: test_forgot.py must import host_isolation at its top level, as every test module beside "
                "/queue/checkout/tests/host_isolation.py does; nothing ran")


class UnittestGate(Fixture):
    """A project whose gate is eng unittest over tests/, which keeps its tests off the real hosts in host_isolation.py,
    as the toolkit's does. No test here runs a gate: eng land's checks and `eng queue doctor` read trees and logs."""
    def setUp(self):
        super().setUp()
        config = self.config()
        config["lanes"]["gate"] = {"command": ["python3", "scripts/eng", "unittest", "-s", "tests", "--split", "test",
                                               "-v"]}
        (self.repo / "tests").mkdir()
        (self.repo / "tests/host_isolation.py").write_text("")
        (self.repo / "tests/test_ok.py").write_text(ISOLATED)
        run(self.repo, "git", "add", "tests")
        self.publish(config, "Gate on eng unittest")

    def land(self, branch, **options):
        """(the new entry's ID or None, what eng land said, the error it refused with or None), no worker started."""
        said, error = io.StringIO(), None
        with patch.object(mq.Queue, "start"), contextlib.redirect_stdout(said), contextlib.redirect_stderr(said):
            try: mq.add(self.repo, branch, **options)
            except mq.Refused as refused: error = str(refused)
        identifier = next((e["id"] for e in reversed(mq.Queue(self.repo).all())
                           if e["branch"] == branch and e["state"] in mq.WAITING), None)
        return identifier, said.getvalue(), error


class CheckedBeforeRunning(UnittestGate):
    """eng land and `eng queue add` run eng unittest's checks before a run on the entry's tree, and refuse an entry
    that a gate would refuse in seconds having run nothing (vocaro-engineering#290)."""
    def test_a_module_without_host_isolation_is_refused_with_the_refusal_line_and_nothing_is_queued(self):
        self.commit("tests/test_forgot.py", "forgot", UNISOLATED)
        identifier, _, error = self.land("forgot")
        self.assertIsNone(identifier)
        self.assertEqual(mq.Queue(self.repo).all(), [])
        self.assertEqual(run(self.repo, "git", "for-each-ref", "refs/eng-queue"), "")
        self.assertIn("is not queued: eng unittest would refuse every gate holding it before running anything", error)
        self.assertIn(f"eng unittest: test_forgot.py must import host_isolation at its top level, as every test module "
                      f"beside {self.repo}/tests/host_isolation.py does; nothing ran", error)

    def test_eng_land_says_the_refusal_and_exits_1(self):
        import argparse
        parser = argparse.ArgumentParser()
        mq.land_arguments(parser)
        self.commit("tests/test_forgot.py", "forgot", UNISOLATED)
        said = io.StringIO()
        with patch.object(mq.Queue, "start"), contextlib.redirect_stdout(said), contextlib.redirect_stderr(said):
            self.assertEqual(mq.land(self.repo, parser.parse_args(["forgot"]), self.repo), 1)
        self.assertIn("eng: ", said.getvalue())
        self.assertIn("test_forgot.py must import host_isolation", said.getvalue())
        self.assertEqual(mq.Queue(self.repo).all(), [])

    def test_a_module_that_does_not_parse_is_refused(self):
        self.commit("tests/test_broken.py", "broken", "import host_isolation\ndef (:\n")
        identifier, _, error = self.land("broken")
        self.assertIsNone(identifier)
        self.assertIn("eng unittest: test_broken.py does not parse (invalid syntax, line 2); nothing ran", error)

    def test_an_isolated_module_is_queued(self):
        # The positive control.
        self.commit("tests/test_new.py", "new", ISOLATED)
        identifier, _, error = self.land("new")
        self.assertIsNone(error)
        self.assertEqual(self.state(identifier)["state"], "queued")

    def test_a_refusal_main_already_has_is_not_the_entrys(self):
        # main took a module without the import past the queue: an entry that adds nothing of the kind is queued.
        run(self.repo, "git", "checkout", "-q", "main")
        (self.repo / "tests/test_old.py").write_text(UNISOLATED)
        run(self.repo, "git", "add", "tests/test_old.py")
        run(self.repo, "git", "commit", "-qm", "Past the queue")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        self.commit("other", "other")
        identifier, _, error = self.land("other")
        self.assertIsNone(error)
        self.assertEqual(self.state(identifier)["state"], "queued")
        # An entry that adds a second such module is still refused, naming its own.
        self.commit("tests/test_forgot.py", "forgot", UNISOLATED)
        identifier, _, error = self.land("forgot")
        self.assertIsNone(identifier)
        self.assertIn("test_forgot.py, test_old.py must import host_isolation", error)

    def test_a_gate_that_is_not_eng_unittest_is_not_checked(self):
        config = self.config()
        config["lanes"]["gate"] = {"command": ["python3", "lane.py", str(self.counter), str(self.status)]}
        self.publish(config)
        self.commit("tests/test_forgot.py", "forgot", UNISOLATED)
        identifier, _, error = self.land("forgot")
        self.assertIsNone(error)
        self.assertEqual(self.state(identifier)["state"], "queued")

    def test_a_suite_without_host_isolation_or_a_module_its_pattern_skips_is_not_refused(self):
        config = self.config()
        config["lanes"]["gate"]["command"] += ["-p", "test_ok.py"]
        self.publish(config)
        self.commit("tests/test_forgot.py", "forgot", UNISOLATED)
        self.assertIsNone(self.land("forgot")[2])
        # Every module, in a suite without host_isolation.py.
        run(self.repo, "git", "checkout", "-q", "main")
        run(self.repo, "git", "rm", "-q", "tests/host_isolation.py")
        config["lanes"]["gate"]["command"] = config["lanes"]["gate"]["command"][:-2]
        self.publish(config, "No isolation")
        self.commit("tests/test_bare.py", "bare", UNISOLATED)
        self.assertIsNone(self.land("bare")[2])

    def test_the_suites_of_a_lanes_command(self):
        self.assertEqual(mq.suite_of(["python3", "scripts/eng", "unittest", "-s", "tests", "--split", "test", "-v"]),
                         ("tests", None))
        self.assertEqual(mq.suite_of(["python3", "scripts/eng", "unittest", "--start-directory=t", "-p", "a*.py",
                                      "--pattern=b*.py", "-j", "4"]), ("t", ["a*.py", "b*.py"]))
        self.assertEqual(mq.suite_of(["python3", "scripts/eng", "unittest"]), (".", None))
        self.assertIsNone(mq.suite_of(["python3", "lane.py"]))


class Placement(Fixture):
    def test_the_cli_checks_the_configured_gate_on_every_mac_without_starting_work(self):
        config = self.config()
        config["lanes"]["gate"].update(resource="heavy", wholeHost=True)
        self.publish(config)
        free = {"slots": 3, "slotsMax": 7, "busy": [], "busyLimit": 90}
        blocker = {"state": "queued", "kind": "exclusive", "pid": 12, "identity": "admission-12", "processStart": "start"}
        queued = {**free, "busy": [1], "queuedExclusive": "PID 12", "blockers": [blocker]}
        busy = {**free, "busy": [1]}
        with patch.object(mq.remote, "gate_hosts", return_value=("build-host", "gate-03", "gate-04")), \
                patch.object(mq.remote, "capacity_there", side_effect=[
                    queued, busy, mq.remote.Unreachable("gate-04 is offline", "wake it")
                ]) as probe, patch.object(offload_host, "capacity", return_value=free) as here, \
                patch.object(mq.Queue, "__init__", side_effect=AssertionError("placement touched the queue")), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(["--project", str(self.repo), "queue", "placement", "--json"]), 0)
        report = json.loads(output.getvalue())
        self.assertTrue(report["wholeHost"])
        self.assertEqual(report["remoteHosts"], ["build-host", "gate-03", "gate-04"])
        self.assertEqual([row["status"] for row in report["hosts"]], ["waiting", "waiting", "unavailable", "eligible"])
        self.assertEqual(report["hosts"][0]["blockers"], [blocker])
        self.assertIn("exclusive work is queued ahead", report["hosts"][0]["reason"])
        self.assertIn("needs every guaranteed slot", report["hosts"][1]["reason"])
        self.assertEqual([call.args[0] for call in probe.call_args_list], report["remoteHosts"])
        self.assertTrue(all(call.kwargs == {"diagnostic": True} for call in probe.call_args_list))
        here.assert_called_once_with(diagnostic=True)


class Doctor(UnittestGate):
    """`eng queue doctor` reads the queue for the signs that it is stuck rather than testing, says what each means and
    what heals it, and changes nothing (vocaro-engineering#290)."""
    def doctor(self, working=True):
        said = io.StringIO()
        with patch.object(mq.Queue, "working", return_value=working), contextlib.redirect_stdout(said):
            status_ = mq.doctor(self.repo)
        with patch.object(mq.Queue, "working", return_value=working), contextlib.redirect_stdout(io.StringIO()) as js:
            self.assertEqual(mq.doctor(self.repo, as_json=True), status_)
        return status_, said.getvalue(), json.loads(js.getvalue())

    def gate_log(self, name, text, at):
        path = mq.Queue(self.repo).logs / f"{name}-gate.log"
        path.write_text(text)
        os.utime(path, (at, at))
        return path

    def snapshot(self):
        queue = mq.Queue(self.repo)
        return [(e["id"], e["state"]) for e in queue.all()], queue.held()

    def test_a_healthy_queue_is_healthy(self):
        # The negative control: an entry waiting with a worker, and the newest gate gave a verdict, even though an
        # older one was refused.
        self.commit("tests/test_new.py", "new", ISOLATED)
        self.add("new")
        self.gate_log("20261107-150000-aaaaaaaa", REFUSED_LINE + "\n", 1000)
        self.gate_log("20261107-160000-bbbbbbbb", "eng unittest: 9 tests (0 skipped) in 9 processes, 4 at a time, "
                      "1.0s: FAILED: test_x.T.test_t (FAILED (failures=1), status 1)\n", 2000)
        status_, said, found = self.doctor()
        self.assertEqual(status_, 0, said)
        self.assertIn("eng queue doctor: healthy: 1 entry waiting, worker running", said)
        self.assertEqual(found["findings"], [])
        self.assertTrue(found["healthy"])

    def test_a_gate_waiting_for_admission_points_to_eng_admissions_now_without_a_finding(self):
        # vocaro-engineering#342: a pointer, not a report: doctor reads no admission and stays healthy.
        self.commit("tests/test_new.py", "new", ISOLATED)
        self.add("new")
        queue = mq.Queue(self.repo)
        queue.note(lambda state: state.setdefault("gates", {}).__setitem__("main", {
            "lane": "gate", "waiting": "admission on this Mac: all 3 guaranteed slots are busy",
            "waitingKind": "admission", "waitingSince": time.time() - 120}))
        from queuekeeper import offload_host, remote
        with patch.object(offload_host, "admissions_now", side_effect=AssertionError("read")), \
                patch.object(remote, "admissions_there", side_effect=AssertionError("asked")):
            status_, said, found = self.doctor()
        self.assertEqual((status_, found["healthy"], found["findings"]), (0, True, []))
        self.assertIn("eng queue doctor: healthy", said)
        self.assertIn("note: the main gate's gate has waited 2m", said)
        self.assertIn("`python3 scripts/eng admissions now`", found["hints"][0])
        # The negative control: a gate waiting for something else, or with no worker, has no pointer.
        queue.note(lambda state: state["gates"]["main"].update(waitingKind="screen"))
        self.assertEqual(self.doctor()[2]["hints"], [])
        queue.note(lambda state: state["gates"]["main"].update(waitingKind="admission"))
        self.assertEqual(self.doctor(working=False)[2]["hints"], [])

    def test_gates_refused_before_running_name_the_entry_that_brings_the_refusal_and_change_nothing(self):
        self.commit("tests/test_new.py", "new", ISOLATED)
        self.commit("tests/test_forgot.py", "forgot", UNISOLATED)
        self.commit("c", "c")
        # Queued as before eng land checked: by an older toolkit, or as the entry of 2026-11-07 was.
        with patch.object(mq, "checked_before_running"): ids = self.add("new", "forgot", "c")
        self.gate_log("20261107-165720-19efdd1d", REFUSED_LINE + "\n", 1000)
        self.gate_log("20261107-165854-a956c211", "heavy: slot 1\n" + REFUSED_LINE + "\n", 2000)
        before = self.snapshot()
        status_, said, found = self.doctor()
        self.assertEqual(status_, 1, said)
        signals = {finding["signal"]: finding for finding in found["findings"]}
        self.assertEqual(set(signals), {"refused gates", "refused before running"}, said)
        refused = signals["refused gates"]
        self.assertIn("2 gates since the last verdict ran nothing", refused["diagnosis"])
        self.assertIn(REFUSED_LINE, refused["diagnosis"])
        self.assertIn("20261107-165854-a956c211-gate.log, 20261107-165720-19efdd1d-gate.log", refused["diagnosis"])
        self.assertIn(f"{ids[1]} brings it", refused["remedy"])
        self.assertIn(f"`eng queue remove {ids[1]}`", refused["remedy"])
        self.assertNotIn(ids[0], said)
        self.assertNotIn(ids[2], said)
        self.assertIn(f"eng unittest refuses {ids[1]}'s tree before running anything", said)
        self.assertIn("and it changed nothing", said)
        self.assertEqual(self.snapshot(), before)

    def test_a_refusal_no_waiting_entry_brings_is_not_blamed_on_one(self):
        self.commit("c", "c")
        self.add("c")
        self.gate_log("20261107-165720-19efdd1d", REFUSED_LINE + "\n", 1000)
        status_, said, found = self.doctor()
        self.assertEqual(status_, 1, said)
        self.assertEqual([f["signal"] for f in found["findings"]], ["refused gates"])
        self.assertIn("1 gate since the last verdict ran nothing", said)
        self.assertIn("no waiting entry's tree alone is refused so", said)

    def test_gates_in_a_row_with_no_verdict_are_a_finding_and_one_is_not(self):
        self.gate_log("20261107-100000-aaaaaaaa", "eng: lane gate reached its limit\n", 1000)
        status_, said, _ = self.doctor()
        self.assertEqual(status_, 0, said)
        self.gate_log("20261107-110000-bbbbbbbb", "eng unittest: 9 tests (0 skipped) in 9 processes, 4 at a time, "
                      "1.0s: UNMEASURED: test_x (timed out after 1800s)\n", 2000)
        status_, said, found = self.doctor()
        self.assertEqual(status_, 1, said)
        self.assertEqual([f["signal"] for f in found["findings"]], ["no verdict"])
        self.assertIn("2 gates since the last verdict gave none", said)

    def test_stacked_admission_declines_neither_count_nor_hide_main_gate_no_verdicts(self):
        for number in range(mq.DOCTOR_LOGS + 1):
            self.gate_log(f"stack-{number}", f"nothing ran\n{mq.STACK_ADMISSION_DECLINED}\n", 1000 + number)
        status_, said, _ = self.doctor()
        self.assertEqual(status_, 0, said)
        # Real no-verdict runs still count, even with more than the log limit's worth of newer stack declines.
        self.gate_log("main-busy", "nothing ran\n", 900)
        self.gate_log("main-timed-out", "eng: lane gate reached its limit\n", 901)
        status_, said, found = self.doctor()
        self.assertEqual(status_, 1, said)
        self.assertEqual([f["signal"] for f in found["findings"]], ["no verdict"])
        self.assertIn("2 gates since the last verdict gave none", said)
        self.assertIn("main-timed-out-gate.log, main-busy-gate.log", said)

    def test_a_hold_and_entries_waiting_with_no_worker_are_findings(self):
        self.commit("c", "c")
        self.add("c")
        status_, said, found = self.doctor(working=False)
        self.assertEqual(status_, 1, said)
        self.assertEqual([f["signal"] for f in found["findings"]], ["no worker"])
        self.assertIn("1 entry wait and no worker runs", said)
        mq.Queue(self.repo).hold_file.write_text("for a session: something\n")
        status_, said, found = self.doctor(working=False)
        self.assertEqual([f["signal"] for f in found["findings"]], ["held"])
        self.assertIn("the queue is held for a session: something", said)

    def test_an_entry_queued_again_unchanged_came_back_and_a_changed_one_did_not(self):
        self.commit("c", "c")
        first = self.add("c")[0]
        mq.remove(mq.Queue(self.repo), first, quiet=True)
        with patch("time.time", return_value=time.time() + 5): again = self.add("c")[0]
        status_, said, found = self.doctor()
        self.assertEqual(status_, 1, said)
        self.assertEqual([f["signal"] for f in found["findings"]], ["came back"])
        self.assertIn(f"{again} holds the very change {first} held, which was taken out", said)
        # The control: the branch, changed, comes back as another change.
        mq.remove(mq.Queue(self.repo), again, quiet=True)
        run(self.repo, "git", "checkout", "-q", "c")
        (self.repo / "c").write_text("fixed")
        run(self.repo, "git", "commit", "-qam", "Fix c")
        with patch("time.time", return_value=time.time() + 10): self.add("c")
        status_, said, _ = self.doctor()
        self.assertEqual(status_, 0, said)

    def test_the_command_line_takes_doctor(self):
        import argparse
        parser = argparse.ArgumentParser()
        mq.add_arguments(parser)
        self.assertEqual(vars(parser.parse_args(["doctor", "--json"])), {"operation": "doctor", "json": True})


class RefusedGates(UnittestGate):
    """The worker blames a gate eng unittest refused before running anything on the one entry that brings the refusal,
    with no rerun and no bisect; with none or several it bisects, and when the same refusal comes again with no entry
    to blame it holds for a session with `eng queue doctor`'s diagnosis (owner's decision, 2026-11-08,
    vocaro-engineering#290). These tests run real gates of eng unittest."""
    def queued(self, *branches):
        """Queue branches as an older toolkit did, or the entry of 2026-11-07, before eng land checked them."""
        with patch.object(mq, "checked_before_running"): return self.add(*branches)

    def work_said(self):
        with contextlib.redirect_stdout(io.StringIO()) as said: self.assertEqual(self.work(), 0)
        return said.getvalue()

    def past_the_queue(self, name, content=UNISOLATED):
        """Put a test module on main past the queue, as a hand push might."""
        run(self.repo, "git", "checkout", "-q", "main")
        run(self.repo, "git", "pull", "-q", "origin", "main")
        (self.repo / "tests" / name).write_text(content)
        run(self.repo, "git", "add", f"tests/{name}")
        run(self.repo, "git", "commit", "-qm", "Past the queue")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")

    def test_the_one_entry_a_refusal_names_goes_back_in_one_round_without_a_rerun_or_a_bisect(self):
        self.commit("c", "c")
        self.commit("tests/test_forgot.py", "forgot", UNISOLATED)
        self.commit("tests/test_new.py", "new", ISOLATED)
        ids = self.queued("c", "forgot", "new")
        log = self.work_said()
        self.assertEqual([self.state(i)["state"] for i in ids], ["landed", "failed", "landed"], log)
        self.assertIn("gate ran nothing, as eng unittest refused it before running anything, which no rerun would "
                      "change: eng unittest: test_forgot.py must import host_isolation", log)
        self.assertIn(f"blaming {ids[1]} (Add tests/test_forgot.py): eng unittest refused gate before running "
                      f"anything, which no rerun would change, and only {ids[1]} brings the refusal, as its tree alone "
                      "is refused so and it changes tests/test_forgot.py: eng unittest: test_forgot.py must import "
                      "host_isolation", log)
        self.assertIn("sending it back and gating the other 2 at once instead of bisecting", log)
        self.assertNotIn("fail together", log)
        self.assertNotIn("rerunning", log)
        self.assertNotIn("no failed test to rerun alone", log)
        self.assertIn("test_forgot.py must import host_isolation", self.state(ids[1])["why"])
        # Two gates in all: the batch, then the other two at once.
        self.assertEqual(log.count("testing "), 2, log)
        self.assertIsNone(mq.Queue(self.repo).held())

    def test_a_refusal_two_entries_bring_bisects(self):
        # Each brings a module the one refusal names: no single cause, so the batch bisects, and each half then
        # blames its own.
        self.commit("tests/test_one.py", "one", UNISOLATED)
        self.commit("tests/test_two.py", "two", UNISOLATED)
        self.commit("c", "c")
        ids = self.queued("one", "two", "c")
        log = self.work_said()
        self.assertIn("eng unittest refused gate before running anything, and 2 entries of the batch's 3 bring it "
                      f"({ids[0]}: its tree alone is refused so and it changes tests/test_one.py; {ids[1]}: its tree "
                      "alone is refused so and it changes tests/test_two.py), so no commit is blamed", log)
        self.assertIn("3 commits fail together", log)
        self.assertEqual([self.state(i)["state"] for i in ids], ["failed", "failed", "landed"], log)
        self.assertIsNone(mq.Queue(self.repo).held(), log)

    def test_a_refusal_naming_a_module_two_entries_change_bisects(self):
        self.past_the_queue("test_old.py")
        # At either end of the module, so that neither conflicts with the other.
        self.commit("tests/test_old.py", "one", "# one\n" + UNISOLATED)
        self.commit("tests/test_old.py", "two", UNISOLATED + "# two\n")
        ids = self.queued("one", "two")
        log = self.work_said()
        self.assertIn(f"and 2 entries of the batch's 2 bring it ({ids[0]}: it changes tests/test_old.py; {ids[1]}: "
                      "it changes tests/test_old.py), so no commit is blamed", log)
        self.assertIn("2 commits fail together", log)
        self.assertNotIn("blaming", log.split("fail together")[0])
        # Each half alone then has one entry that changes the module: it goes back, as the rule says.
        self.assertEqual([self.state(i)["state"] for i in ids], ["failed", "failed"], log)

    def refused_by_main(self, beside=True, bisecting=False):
        """Two entries whose every gate main's own module refuses, run until the worker stops: (ids, log, hold).
        bisecting starts the worker mid-bisect, as one that took over from another does (Worker.progress), so that
        the first gate is the bisect's two halves, with no gate before them."""
        # main took a module without the import past the queue: every gate is refused, and no entry is the cause.
        self.past_the_queue("test_old.py")
        self.commit("c", "c")
        self.commit("d", "d")
        ids = self.queued("c", "d")
        with contextlib.ExitStack() as halves:
            # Both halves on this Mac, as BothHalves gates them, since the fixture has no second Mac; or one at a time.
            for name, value in ((("HERE", ()), ("BESIDE", ()), ("offerable", lambda config, lanes: None)) if beside
                                else (("offerable", lambda config, lanes: "a test says so"),)):
                halves.enter_context(patch.object(mq, name, value))
            if bisecting:
                real = mq.Worker.__init__
                def taken_over(worker, queue, progress=None):
                    real(worker, queue, {"limit": 1, "suspects": 2} if type(worker) is mq.Worker else progress)
                halves.enter_context(patch.object(mq.Worker, "__init__", taken_over))
            log = self.work_said()
        if not bisecting:
            self.assertIn("eng unittest refused gate before running anything, and no entry of the batch's 2 brings it, "
                          "so no commit is blamed", log)
            self.assertIn("2 commits fail together", log)
        # Nobody was sent back on a refusal no entry brings.
        self.assertEqual([self.state(i)["state"] for i in ids], ["queued", "queued"], log)
        held = mq.Queue(self.repo).held()
        self.assertIsNotNone(held, log)
        return ids, log, held

    def test_a_refusal_no_entry_brings_in_both_halves_of_a_bisect_holds_for_a_session_with_the_diagnosis(self):
        ids, log, held = self.refused_by_main()
        self.assertIn("eng unittest refused this gate and the bisect's newer half beside it before running anything", held)
        self.assertEqual(log.count("testing "), 3, log)  # the batch, then its two halves at once
        self.check_hold(ids, log, held)

    def test_the_same_refusal_in_both_halves_with_no_gate_before_holds_and_sends_back_no_lone_half(self):
        # Each half is one entry, which a refusal no entry brings would otherwise send back as a lone failure.
        ids, log, held = self.refused_by_main(bisecting=True)
        self.assertIn("eng unittest refused this gate and the bisect's newer half beside it before running anything", held)
        self.assertIn("newer half: eng unittest refused gate before running anything, and no entry of the batch's 1 "
                      "brings it", log)
        self.assertIn("queue: held: for a session, not the owner: eng unittest refused this gate and the bisect's older "
                      "half beside it", log)
        self.assertEqual(log.count("testing "), 2, log)  # the two halves at once, and nothing after
        self.check_hold(ids, log, held)

    def test_a_refusal_no_entry_brings_in_two_gates_in_a_row_holds_for_a_session(self):
        ids, log, held = self.refused_by_main(beside=False)
        self.assertIn("eng unittest refused this gate and the gate before it before running anything", held)
        self.assertEqual(log.count("testing "), 2, log)
        self.check_hold(ids, log, held)

    def check_hold(self, ids, log, held):
        self.assertTrue(held.startswith("for a session, not the owner: eng unittest refused this gate and "), held)
        self.assertIn("the same way, and no one entry of the batch brings the refusal", held)
        self.assertIn("test_old.py must import host_isolation", held)
        self.assertIn("eng queue doctor: refused gates: ", held)
        self.assertIn("no waiting entry's tree alone is refused so", held)
        self.assertIn(f"queue: held: {held}", log)
        self.assertEqual([self.state(i)["state"] for i in ids], ["queued", "queued"], log)
        with contextlib.redirect_stdout(io.StringIO()) as status_:
            mq.status(self.repo)
        self.assertIn("eng queue doctor: refused gates: ", status_.getvalue())

    def test_a_refusal_that_names_no_module_blames_nobody(self):
        worker = mq.Worker(mq.Queue(self.repo))
        self.commit("c", "c")
        ids = self.queued("c")
        with contextlib.redirect_stdout(io.StringIO()) as said:
            self.assertIsNone(worker.blame_refusal([self.state(ids[0])], "gate",
                                                   "eng unittest: something else; nothing ran", self.config()))
        self.assertIn("the refusal names no test module, so no commit is blamed", said.getvalue())
        self.assertEqual(worker.unblamed["line"], "eng unittest: something else; nothing ran")

    def test_the_same_refusal_from_two_checkouts_is_one(self):
        self.assertEqual(mq.refusal_key(REFUSED_LINE), mq.refusal_key(REFUSED_LINE.replace("/queue/checkout",
                                                                                            "/elsewhere/beside")))
        self.assertNotEqual(mq.refusal_key(REFUSED_LINE), mq.refusal_key(REFUSED_LINE.replace("test_forgot",
                                                                                               "test_other")))


class NoVerdict(Fixture):
    @contextlib.contextmanager
    def no_verdict_for(self, runs, status="3"):
        """The gate gives no verdict, exiting status, for its first runs runs, and passes after."""
        self.status.write_text(status)
        real = mq.Worker.gate
        def gate(worker, *arguments):
            result = real(worker, *arguments)
            if self.gates() >= runs: self.status.unlink(missing_ok=True)
            return result
        with patch.object(mq.Worker, "gate", gate): yield

    def test_a_lane_that_merely_exits_as_a_lost_local_network_does_counts_and_ends_no_worker(self):
        """The negative control of vocaro-engineering#300: only the worker's own answer ends it, or a lane exiting 69
        would end worker after worker with nothing to bound them. The lane exits so under a real eng run."""
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.no_verdict_for(1, str(mq.remote.LOST_ACCESS)), \
                patch.object(mq.remote, "local_network_lost", return_value=None), \
                contextlib.redirect_stdout(io.StringIO()) as said:
            self.assertEqual(self.work(), 0)
        self.assertEqual(self.gates(), 2)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertIn(f"no verdict (exit {mq.remote.LOST_ACCESS}), run 1 of at most {mq.NO_VERDICT_RUNS}", said.getvalue())
        self.assertNotIn("exits, holding nothing", said.getvalue())

    def test_unmeasured_holds_after_the_bound_and_blames_nobody(self):
        self.status.write_text("3")
        self.commit("a", "a")
        [identifier] = self.add("a")
        with patch("builtins.print"): self.assertEqual(self.work(), 0)
        self.assertEqual(self.gates(), mq.NO_VERDICT_RUNS)
        self.assertEqual(self.state(identifier)["state"], "queued")
        self.assertNotIn("a", self.main_files())
        held = mq.Queue(self.repo).held()
        # The hold names the Mac the gate ran on, from its receipt (vocaro-engineering#185).
        self.assertIn(f"the lanes gave no verdict in 3 runs in a row, the last with exit 3 on this Mac "
                      f"({offload_host.mac_name()}), unmeasured, with no test its log names, the most "
                      "queue.noVerdictRuns allows (3); see ", held)
        self.assertIn("eng queue resume", held)
        self.status.unlink()
        with patch.object(mq.Queue, "start"), patch("builtins.print"): mq.resume(self.repo)
        self.work()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(self.gates(), mq.NO_VERDICT_RUNS + 1)

    def test_no_verdict_is_retried_up_to_the_bound_and_lands_when_a_retry_passes(self):
        for status in ("3", "75", "78"):
            with self.subTest(status=status):
                start = self.gates()
                self.commit(f"a{status}", f"a{status}")
                [identifier] = self.add(f"a{status}")
                # The bound's last run passes: two runs with no verdict, and the third lands it.
                with self.no_verdict_for(start + mq.NO_VERDICT_RUNS - 1, status), \
                        contextlib.redirect_stdout(io.StringIO()) as said:
                    self.assertEqual(self.work(), 0)
                self.assertEqual(self.gates() - start, mq.NO_VERDICT_RUNS)
                self.assertEqual(self.state(identifier)["state"], "landed")
                self.assertIsNone(mq.Queue(self.repo).held())
                self.assertIn(f"no verdict (exit {status}), run 1 of at most 3", said.getvalue())
                self.assertIn(f"no verdict (exit {status}), run 2 of at most 3", said.getvalue())

    LOCKED = ("eng: screen: locked (console user t, CGSSessionScreenIsLocked yes, IOConsoleLocked no) on this Mac "
              "(Fixture-Mac) at 01:58:02")
    UNLOCKED = ("eng: screen: unlocked (console user t, CGSSessionScreenIsLocked absent, IOConsoleLocked no) on this "
                "Mac (Fixture-Mac) at 01:39:20")

    @contextlib.contextmanager
    def commands(self):
        """The commands the worker runs, each lane's eng run among them."""
        real, ran = mq.processes.run, []
        def recorded(command, *arguments, **options):
            ran.append(list(command))
            return real(command, *arguments, **options)
        with patch.object(mq.processes, "run", recorded): yield ran

    def lane_runs(self, ran):
        """The eng run commands of ran but the spelling lane's, which comes before the lane these tests judge."""
        return [command for command in ran if command[:3] == ["python3", "scripts/eng", "run"] and command[-1] != mq.SPELLING]

    def test_a_run_a_locked_screen_voided_does_not_count_and_the_next_goes_only_to_an_unlocked_mac(self):
        """vocaro-engineering#293, the owner's decision of 2026-11-08: more runs than the bound, each unmeasured behind
        a locked screen, hold nothing; each run after one goes to eng run --unlocked-screen. The first, undeclared,
        is judged by its log's last screen reading; each later one, which eng reads the screen for, by its receipt's
        screenAfter, from a recording that reads unlocked as the run starts and locked once the lane has run."""
        screen = Path(f"{self.status}.screen")
        screen.write_text(f"{self.UNLOCKED}\n{self.LOCKED}\n")
        screens = Path(__file__).resolve().parent / "fixtures/ScreenState"
        recording = self.base / "screen.plist"
        real = mq.Worker.gate
        def unlocked_at_start(worker, *arguments, **options):
            shutil.copy(screens / "this-mac-unlocked.plist", recording)
            return real(worker, *arguments, **options)
        self.commit("a", "a")
        [identifier] = self.add("a")
        with patch.dict(os.environ, {offload_host.SCREEN_RECORDING: str(recording),
                                     "FIXTURE_LOCK_DURING_RUN": str(screens / "this-mac-locked-idle.plist")}), \
                patch.object(mq.Worker, "gate", unlocked_at_start), self.no_verdict_for(mq.NO_VERDICT_RUNS + 1), \
                self.commands() as ran, contextlib.redirect_stdout(io.StringIO()) as said:
            self.assertEqual(self.work(), 0)
        self.assertEqual(self.gates(), mq.NO_VERDICT_RUNS + 2)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertIsNone(mq.Queue(self.repo).held())
        self.assertIn("no verdict (exit 3) on this Mac", said.getvalue())
        self.assertEqual(said.getvalue().count("gate ran behind a locked screen"), mq.NO_VERDICT_RUNS + 1)
        self.assertIn(f"gate ran behind a locked screen, the last reading {self.LOCKED[5:]}, which queue.noVerdictRuns "
                      "does not count; its next run goes only to a Mac whose screen reads unlocked", said.getvalue())
        self.assertRegex(said.getvalue(), r"gate ran behind a locked screen, the last reading screen: locked \(console "
                         r"user trevorharmon, CGSSessionScreenIsLocked yes, IOConsoleLocked no\) on this Mac \(\S+\) at ")
        self.assertNotIn("of at most", said.getvalue())
        runs = self.lane_runs(ran)
        self.assertEqual(runs[0], ["python3", "scripts/eng", "run", "gate"])
        self.assertEqual(runs[1:], [["python3", "scripts/eng", "run", "--unlocked-screen", "gate"]] * (len(runs) - 1))
        self.assertIn("gate: only on a Mac whose screen reads unlocked", said.getvalue())

    def test_a_run_whose_last_reading_is_unlocked_or_that_ran_nothing_still_counts(self):
        """The negative controls: an unmeasured run whose last reading is unlocked, and a run that exits 75 or 78
        with a lock in its log, as one that waited out the overflow for an unlocked Mac does, count as before."""
        screen = Path(f"{self.status}.screen")
        for status, said_screen in (("3", f"{self.LOCKED}\n{self.UNLOCKED}\n"), ("75", f"{self.LOCKED}\n"),
                                    ("78", f"{self.LOCKED}\n")):
            with self.subTest(status=status):
                start = self.gates()
                screen.write_text(said_screen)
                self.status.write_text(status)
                self.commit(f"a{status}", f"a{status}")
                [identifier] = self.add(f"a{status}")
                with self.commands() as ran, contextlib.redirect_stdout(io.StringIO()) as said:
                    self.assertEqual(self.work(), 0)
                self.assertEqual(self.gates() - start, mq.NO_VERDICT_RUNS)
                self.assertEqual(self.state(identifier)["state"], "queued")
                self.assertIn("the most queue.noVerdictRuns allows (3)", mq.Queue(self.repo).held())
                self.assertNotIn("locked screen", said.getvalue())
                self.assertTrue(all("--unlocked-screen" not in command for command in self.lane_runs(ran)))
                mq.Queue(self.repo).hold_file.unlink()
                for entry in mq.Queue(self.repo).all(): mq.remove(mq.Queue(self.repo), entry["id"], quiet=True)
        self.status.unlink()

    def test_the_receipts_screen_after_decides_over_the_log(self):
        """The last reading: a fresh receipt's screenAfter, which eng writes once a lane that depends on the screen
        ends unmeasured; else the log's last `screen:` line. A receipt older than the run is not the run's."""
        receipt = self.base / ".build/engineering/gate/source.json"
        receipt.parent.mkdir(parents=True)
        log = self.base / "gate.log"
        log.write_text(f"{self.UNLOCKED}\n")
        began = time.time() - 60
        worker = type("Runner", (), {"checkout": self.base, "lane_now": ("gate", began, ())})()
        locked = {"state": "locked", "user": "t", "CGSSessionScreenIsLocked": True, "IOConsoleLocked": False,
                  "mac": "Fixture-Mac", "at": "01:58:02"}
        receipt.write_text(json.dumps({"screenAfter": locked}))
        self.assertEqual(mq.Worker.screen_locked(worker, 3, log), ("gate", self.LOCKED[5:]))
        receipt.write_text(json.dumps({"screenAfter": {**locked, "state": "unknown", "reason": "no console"}}))
        self.assertIsNone(mq.Worker.screen_locked(worker, 3, log))
        receipt.write_text(json.dumps({"screenAfter": {**locked, "state": "unlocked"}}))
        log.write_text(f"{self.LOCKED}\n")
        self.assertIsNone(mq.Worker.screen_locked(worker, 3, log))
        # A receipt from before the run: the log decides.
        os.utime(receipt, (began - 10, began - 10))
        self.assertEqual(mq.Worker.screen_locked(worker, 3, log), ("gate", self.LOCKED[5:]))
        self.assertIsNone(mq.Worker.screen_locked(worker, 75, log))
        self.assertIsNone(mq.Worker.screen_locked(worker, 78, log))
        log.write_text("no reading\n")
        self.assertIsNone(mq.Worker.screen_locked(worker, 3, log))

    FAR_LOCKED = {"state": "locked", "user": "t", "CGSSessionScreenIsLocked": True, "IOConsoleLocked": False,
                  "mac": "Far-Mac", "at": "01:58:02"}
    FAR_READING = ("screen: locked (console user t, CGSSessionScreenIsLocked yes, IOConsoleLocked no) on the second "
                   "Mac (Far-Mac) at 01:58:02")

    def dispatch_receipt(self, screen_after=None, **result):
        """What remote.execute writes to a dispatch's source.json: the lane's own receipt, which holds screenAfter, sits
        under result, as offload_host returns it from the second Mac; nothing at the top level says the screen."""
        lane = {"outcome": "unmeasured", **({} if screen_after is None else {"screenAfter": screen_after})}
        return {"ranOn": "second Mac", "mac": "Far-Mac", "host": "far-mac", "lane": "gate",
                "result": {"exitStatus": 3, "outcome": "unmeasured", "receipt": lane, **result}}

    def second_mac_run(self):
        """(a worker whose last gate began a minute ago, the source.json of that gate's dispatch, the log) laid out where
        the real remote.dispatch finds them."""
        began = time.time() - 60
        ran = (self.base / ".build/engineering/remote" / mq.remote.DISPATCHES / "gate"
               / time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))
        ran.mkdir(parents=True)
        worker = type("Runner", (), {"checkout": self.base, "lane_now": ("gate", began, ())})()
        return worker, ran / "source.json", self.base / "gate.log"

    def test_a_second_macs_reading_under_its_dispatchs_result_decides_over_the_log(self):
        """The lane's receipt from the second Mac is under the dispatch's result, so the last reading is there."""
        worker, receipt, log = self.second_mac_run()
        receipt.write_text(json.dumps(self.dispatch_receipt(self.FAR_LOCKED)))
        for said in ("no reading\n", f"{self.UNLOCKED}\n"):
            with self.subTest(log=said):
                log.write_text(said)
                self.assertEqual(mq.Worker.screen_locked(worker, 3, log), ("gate", self.FAR_READING))
        log.write_text(f"{self.LOCKED}\n")
        for state in ("unlocked", "unknown"):
            with self.subTest(state=state):
                receipt.write_text(json.dumps(self.dispatch_receipt({**self.FAR_LOCKED, "state": state})))
                self.assertIsNone(mq.Worker.screen_locked(worker, 3, log))
        # No admission and a host's configuration ran nothing, whatever the receipt reads.
        receipt.write_text(json.dumps(self.dispatch_receipt(self.FAR_LOCKED)))
        self.assertIsNone(mq.Worker.screen_locked(worker, 75, log))
        self.assertIsNone(mq.Worker.screen_locked(worker, 78, log))

    def test_a_dispatch_receipt_without_a_usable_reading_leaves_the_local_receipt_and_the_log(self):
        """A malformed or stale dispatch receipt says nothing: the local receipt, else the log's last line, decides.
        A screenAfter at a dispatch's top level is not where one is written, and is not read."""
        worker, receipt, log = self.second_mac_run()
        base = self.dispatch_receipt()
        malformed = [{**base, "result": value} for value in (None, "text", 7, [])] \
            + [{**base, "result": {"exitStatus": 3, "receipt": value}} for value in (None, "text", 7, [])] \
            + [{**base, "result": {"exitStatus": 3}}, {"host": "far-mac"}, base] \
            + [self.dispatch_receipt(value) for value in ("locked", 7, [], {}, {"state": ""})] \
            + [{**base, "screenAfter": self.FAR_LOCKED}]
        local = self.base / ".build/engineering/gate/source.json"
        local.parent.mkdir(parents=True)
        for shape in malformed:
            with self.subTest(shape=shape):
                receipt.write_text(json.dumps(shape))
                local.unlink(missing_ok=True)
                log.write_text(f"{self.LOCKED}\n")
                self.assertEqual(mq.Worker.screen_locked(worker, 3, log), ("gate", self.LOCKED[5:]))
                log.write_text(f"{self.UNLOCKED}\n")
                self.assertIsNone(mq.Worker.screen_locked(worker, 3, log))
                local.write_text(json.dumps({"screenAfter": {**self.FAR_LOCKED, "mac": "Fixture-Mac"}}))
                self.assertEqual(mq.Worker.screen_locked(worker, 3, log), ("gate", self.LOCKED[5:]))
        # A dispatch receipt from before the run is not the run's.
        local.unlink(missing_ok=True)
        log.write_text(f"{self.UNLOCKED}\n")
        receipt.write_text(json.dumps(self.dispatch_receipt(self.FAR_LOCKED)))
        os.utime(receipt, (worker.lane_now[1] - 10, worker.lane_now[1] - 10))
        self.assertIsNone(mq.Worker.screen_locked(worker, 3, log))

    def test_a_local_receipt_is_read_at_its_top_level_only(self):
        """A run on this Mac has no dispatch; its receipt holds screenAfter at the top level, and a result in it is
        not a dispatch's."""
        receipt = self.base / ".build/engineering/gate/source.json"
        receipt.parent.mkdir(parents=True)
        log = self.base / "gate.log"
        log.write_text(f"{self.UNLOCKED}\n")
        worker = type("Runner", (), {"checkout": self.base, "lane_now": ("gate", time.time() - 60, ())})()
        here = {**self.FAR_LOCKED, "mac": "Fixture-Mac"}
        nested = {"result": {"receipt": {"screenAfter": here}}}
        receipt.write_text(json.dumps(nested))
        self.assertIsNone(mq.Worker.screen_locked(worker, 3, log))
        receipt.write_text(json.dumps({**nested, "screenAfter": here}))
        self.assertEqual(mq.Worker.screen_locked(worker, 3, log), ("gate", self.LOCKED[5:]))
        receipt.write_text(json.dumps({**nested, "screenAfter": {**here, "state": "unlocked"}}))
        self.assertIsNone(mq.Worker.screen_locked(worker, 3, log))

    @contextlib.contextmanager
    def gated_on_second_mac(self, reading):
        """Every gate reports a dispatch to the second Mac whose receipt, written once the lane has run, reads reading,
        and whose run.log holds what the lane printed, while this Mac's own screen reads unlocked for eng run
        --unlocked-screen."""
        screens = Path(__file__).resolve().parent / "fixtures/ScreenState"
        recording = self.base / "screen.plist"
        shutil.copy(screens / "this-mac-unlocked.plist", recording)
        ran = self.base / "dispatches/far"
        ran.mkdir(parents=True)
        def dispatch(root, name, since):
            with contextlib.suppress(FileNotFoundError):
                (Path(root) / ".build/engineering" / name / "run.log").rename(ran / "run.log")
            (ran / "source.json").write_text(json.dumps(self.dispatch_receipt(reading)))
            return ran, "far-mac"
        with patch.dict(os.environ, {offload_host.SCREEN_RECORDING: str(recording)}), \
                patch.object(mq.remote, "dispatch", side_effect=dispatch):
            yield

    def test_a_second_mac_run_its_receipt_reads_locked_does_not_count_and_the_next_needs_an_unlocked_mac(self):
        """With no `screen:` line in the log, only the reading under the dispatch's result can say the screen was
        locked: more runs than the bound hold nothing, and each after the first goes to eng run --unlocked-screen."""
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.gated_on_second_mac(self.FAR_LOCKED), self.no_verdict_for(mq.NO_VERDICT_RUNS + 1), \
                self.commands() as ran, contextlib.redirect_stdout(io.StringIO()) as said:
            self.assertEqual(self.work(), 0)
        self.assertEqual(self.gates(), mq.NO_VERDICT_RUNS + 2)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertIsNone(mq.Queue(self.repo).held())
        self.assertIn(f"gate ran behind a locked screen, the last reading {self.FAR_READING}, which "
                      "queue.noVerdictRuns does not count", said.getvalue())
        self.assertNotIn("of at most", said.getvalue())
        runs = self.lane_runs(ran)
        self.assertEqual(runs[0], ["python3", "scripts/eng", "run", "gate"])
        self.assertEqual(runs[1:], [["python3", "scripts/eng", "run", "--unlocked-screen", "gate"]] * (len(runs) - 1))

    def test_a_second_mac_run_its_receipt_reads_unlocked_counts_though_its_log_says_locked(self):
        """The negative control: the receipt's last reading wins over a log line saying locked, so the run counts."""
        Path(f"{self.status}.screen").write_text(f"{self.LOCKED}\n")
        self.status.write_text("3")
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.gated_on_second_mac({**self.FAR_LOCKED, "state": "unlocked"}), self.commands() as ran, \
                contextlib.redirect_stdout(io.StringIO()) as said:
            self.assertEqual(self.work(), 0)
        self.assertEqual(self.gates(), mq.NO_VERDICT_RUNS)
        self.assertEqual(self.state(identifier)["state"], "queued")
        self.assertIn("the most queue.noVerdictRuns allows (3)", mq.Queue(self.repo).held())
        self.assertNotIn("locked screen", said.getvalue())
        self.assertTrue(all("--unlocked-screen" not in command for command in self.lane_runs(ran)))
        self.status.unlink()

    def test_a_worker_that_takes_over_keeps_the_lanes_that_need_an_unlocked_screen(self):
        queue = mq.Queue(self.repo)
        self.assertNotIn("unlocked", mq.Worker(queue).progress())
        worker = mq.Worker(queue, {"limit": None, "suspects": 0, "no_verdicts": 1, "unlocked": ["gate"]})
        self.assertEqual(worker.unlocked, {"gate"})
        self.assertEqual(worker.progress()["unlocked"], ["gate"])
        self.assertIs(mq.Beside(worker).unlocked, worker.unlocked)

    @contextlib.contextmanager
    def gates_that(self, steps, then=None):
        """Each gate in turn does what steps says, and every gate past them passes: "locked", unmeasured with a screen
        that reads unlocked at the start and locks during the run; "began locked", which reads locked from the first;
        "unmeasured", with a screen unlocked throughout; "waited", exit 75, as a run that waits out the overflow for
        an unlocked Mac does; and "passed", a verdict. then, if given, is called once the gate at its step has run."""
        screens = Path(__file__).resolve().parent / "fixtures/ScreenState"
        recording, during = self.base / "screen.plist", self.base / "during.plist"
        screen = Path(f"{self.status}.screen")
        locked, unlocked = screens / "this-mac-locked-idle.plist", screens / "this-mac-unlocked.plist"
        status = {"locked": "3", "began locked": "3", "unmeasured": "3", "waited": "75"}
        said = {"locked": f"{self.UNLOCKED}\n{self.LOCKED}\n", "began locked": f"{self.LOCKED}\n"}
        real, gated = mq.Worker.gate, []
        def gate(worker, *arguments, **options):
            step = steps[len(gated)] if len(gated) < len(steps) else "passed"
            gated.append(step)
            shutil.copy(unlocked, recording)
            shutil.copy(locked if step in said else unlocked, during)
            screen.write_text(said.get(step, f"{self.UNLOCKED}\n"))
            if step in status: self.status.write_text(status[step])
            else: self.status.unlink(missing_ok=True)
            result = real(worker, *arguments, **options)
            if then and len(gated) - 1 == then[0]: then[1]()
            return result
        with patch.dict(os.environ, {offload_host.SCREEN_RECORDING: str(recording),
                                     "FIXTURE_LOCK_DURING_RUN": str(during)}), patch.object(mq.Worker, "gate", gate):
            yield gated

    def test_the_fifth_locked_run_in_a_row_holds_the_queue_for_a_session_and_the_fourth_does_not(self):
        """vocaro-engineering#293, the owner's decision of 2026-11-09: the cap on the runs queue.noVerdictRuns does
        not count. The hold is a session's, and names the lane, the screen reading and the Mac."""
        self.assertEqual(mq.LOCKED_SCREEN_RUNS, 5)
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.gates_that(["locked"] * mq.LOCKED_SCREEN_RUNS), contextlib.redirect_stdout(io.StringIO()) as said:
            self.assertEqual(self.work(), 0)
        self.assertEqual(self.gates(), 5)
        self.assertEqual(self.state(identifier)["state"], "queued")
        self.assertNotIn("a", self.main_files())
        held = mq.Queue(self.repo).held()
        self.assertTrue(held.startswith("for a session, not the owner: gate ended behind a screen that locked during "
                                        "the run in 5 runs in a row, the last reading screen: locked ("), held)
        self.assertRegex(held, r"the last reading screen: locked \(console user trevorharmon, CGSSessionScreenIsLocked "
                         r"yes, IOConsoleLocked no\) on this Mac \(\S+\) at \S+, the most LOCKED_SCREEN_RUNS allows "
                         r"\(5\); see ")
        self.assertIn("runs `eng queue resume` itself", held)
        self.assertIn("4 such runs in a row, and 5 hold the queue", said.getvalue())
        self.assertNotIn("the most queue.noVerdictRuns allows", held)

    def test_four_locked_runs_in_a_row_hold_nothing(self):
        """The negative control of the fifth: the run after the fourth passes, and the entry lands."""
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.gates_that(["locked"] * (mq.LOCKED_SCREEN_RUNS - 1)), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.work(), 0)
        self.assertEqual(self.gates(), 5)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertIsNone(mq.Queue(self.repo).held())

    def test_a_run_that_began_behind_the_lock_is_not_counted_toward_the_cap(self):
        """The owner's condition: unlocked at its start and locked by its end. The first run, not placed and locked
        from its first reading, is not one of the five; the five placed after it are."""
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.gates_that(["began locked"] + ["locked"] * mq.LOCKED_SCREEN_RUNS), \
                contextlib.redirect_stdout(io.StringIO()) as said:
            self.assertEqual(self.work(), 0)
        self.assertEqual(self.gates(), 6)
        self.assertEqual(self.state(identifier)["state"], "queued")
        self.assertIn("LOCKED_SCREEN_RUNS does not count it either", said.getvalue())
        self.assertIn("in 5 runs in a row", mq.Queue(self.repo).held())

    def test_a_verdict_between_locked_runs_starts_the_cap_again(self):
        """A pass is a verdict: four locked runs on each side of it, eight in all, hold nothing. The second entry
        arrives once the first's passing gate has run, so that one worker goes on, with its count, to gate it."""
        self.commit("a", "a"); self.commit("b", "b")
        [first] = self.add("a")
        arrived = []
        steps = ["locked"] * 4 + ["passed"] + ["locked"] * 4
        with self.gates_that(steps, (4, lambda: arrived.extend(self.add("b")))) as gated, \
                contextlib.redirect_stdout(io.StringIO()) as said:
            self.assertEqual(self.work(), 0)
        self.assertIsNone(mq.Queue(self.repo).held())
        self.assertEqual(gated, steps + ["passed"])
        self.assertEqual(said.getvalue().count("4 such runs in a row"), 2)
        self.assertNotIn("5 such runs", said.getvalue())
        self.assertEqual([self.state(identifier)["state"] for identifier in (first, *arrived)], ["landed", "landed"])

    def test_a_no_verdict_of_another_cause_starts_the_cap_again_and_still_counts_as_before(self):
        self.commit("a", "a")
        [identifier] = self.add("a")
        steps = ["locked"] * 4 + ["unmeasured"] + ["locked"] * 4
        with self.gates_that(steps), contextlib.redirect_stdout(io.StringIO()) as said:
            self.assertEqual(self.work(), 0)
        self.assertEqual(self.gates(), len(steps) + 1)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertIsNone(mq.Queue(self.repo).held())
        self.assertIn("no verdict (exit 3), run 1 of at most 3", said.getvalue())
        self.assertEqual(said.getvalue().count("4 such runs in a row"), 2)
        self.assertNotIn("5 such runs", said.getvalue())

    def test_a_run_that_waited_for_an_unlocked_mac_neither_adds_to_the_cap_nor_ends_it(self):
        """Exit 75 from a lane sent only to an unlocked Mac ran nothing: the row goes on across it, so three locked
        runs, the wait and two more hold at the fifth locked run, the sixth gate. It counts toward noVerdictRuns."""
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.gates_that(["locked"] * 3 + ["waited"] + ["locked"] * 2), \
                contextlib.redirect_stdout(io.StringIO()) as said:
            self.assertEqual(self.work(), 0)
        self.assertEqual(self.gates(), 6)
        self.assertEqual(self.state(identifier)["state"], "queued")
        self.assertIn("in 5 runs in a row", mq.Queue(self.repo).held())
        self.assertIn("no verdict (exit 75), run 1 of at most 3", said.getvalue())

    def test_whether_a_run_began_unlocked_is_read_from_its_placement_or_its_first_reading(self):
        worker, log = mq.Worker(mq.Queue(self.repo)), self.base / "gate.log"
        declared = {"lanes": {"gate": {"unlockedScreen": True}}}
        log.write_text(f"{self.UNLOCKED}\n{self.LOCKED}\n")
        self.assertTrue(worker.began_unlocked("gate", log, {}))
        log.write_text(f"{self.LOCKED}\n{self.UNLOCKED}\n")
        self.assertFalse(worker.began_unlocked("gate", log, {}))
        log.write_text("no reading\n")
        self.assertFalse(worker.began_unlocked("gate", log, {}))
        self.assertFalse(worker.began_unlocked("gate", self.base / "no-such.log", {}))
        self.assertFalse(worker.began_unlocked("gate", None, {}))
        self.assertTrue(worker.began_unlocked("gate", log, declared))
        worker.sent_unlocked = True
        self.assertTrue(worker.began_unlocked("gate", log, {}))

    def test_a_worker_that_takes_over_keeps_its_count_of_locked_runs(self):
        queue = mq.Queue(self.repo)
        self.assertNotIn("locked_screens", mq.Worker(queue).progress())
        worker = mq.Worker(queue, {"limit": None, "suspects": 0, "no_verdicts": 0, "locked_screens": 3})
        self.assertEqual(worker.locked_screens, 3)
        self.assertEqual(worker.progress()["locked_screens"], 3)

    def test_a_failure_is_not_read_as_no_verdict(self):
        self.status.write_text("1")
        self.commit("a", "a")
        [identifier] = self.add("a")
        self.work()
        self.assertEqual(self.state(identifier)["state"], "failed")
        # Never retried: one gate, and no hold.
        self.assertEqual(self.gates(), 1)
        self.assertIsNone(mq.Queue(self.repo).held())

    def test_a_failure_after_no_verdict_comes_back_and_is_not_retried(self):
        self.status.write_text("3")
        real = mq.Worker.gate
        def gate(worker, *arguments):
            result = real(worker, *arguments)
            self.status.write_text("1")
            return result
        self.commit("a", "a")
        [identifier] = self.add("a")
        with patch.object(mq.Worker, "gate", gate), patch("builtins.print"): self.assertEqual(self.work(), 0)
        self.assertEqual(self.gates(), 2)
        self.assertEqual(self.state(identifier)["state"], "failed")
        self.assertIsNone(mq.Queue(self.repo).held())

    def test_a_verdict_starts_the_count_again(self):
        """Two runs with no verdict, a pass, then two more: no hold, though four gave none."""
        self.commit("a", "a"); self.commit("b", "b")
        [first] = self.add("a")
        with self.no_verdict_for(2), patch("builtins.print"): self.work()
        self.assertEqual(self.state(first)["state"], "landed")
        [second] = self.add("b")
        with self.no_verdict_for(5), patch("builtins.print"): self.work()
        self.assertEqual(self.state(second)["state"], "landed")
        self.assertEqual(self.gates(), 6)
        self.assertIsNone(mq.Queue(self.repo).held())

    def test_the_bound_is_mains_queue_no_verdict_runs(self):
        self.assertEqual(mq.NO_VERDICT_RUNS, 3)
        self.assertEqual(mq.no_verdict_runs({}), 3)
        self.assertEqual(mq.no_verdict_runs({"queue": {}}), 3)
        self.assertEqual(mq.no_verdict_runs(self.config()), 3)
        for runs in (1, 5):
            with self.subTest(runs=runs):
                run(self.repo, "git", "fetch", "-q", "origin")
                run(self.repo, "git", "checkout", "-q", "-B", "main", "origin/main")
                config = self.config()
                config["queue"]["noVerdictRuns"] = runs
                self.publish(config, f"Allow {runs} runs with no verdict")
                self.assertEqual(mq.no_verdict_runs(self.config()), runs)
                with contextlib.suppress(OSError): mq.Queue(self.repo).hold_file.unlink()
                start = self.gates()
                self.status.write_text("3")
                self.commit(f"a{runs}", f"a{runs}")
                [identifier] = self.add(f"a{runs}")
                with patch("builtins.print"): self.assertEqual(self.work(), 0)
                self.assertEqual(self.gates() - start, runs)
                self.assertEqual(self.state(identifier)["state"], "queued")
                self.assertIn(f"in {runs} run{'' if runs == 1 else 's'} in a row, the last with exit 3", mq.Queue(self.repo).held())
                self.assertIn(f"the most queue.noVerdictRuns allows ({runs})", mq.Queue(self.repo).held())
                mq.remove(mq.Queue(self.repo), identifier, quiet=True)

    def test_a_branch_cannot_raise_its_own_bound(self):
        """Read from main, as every queue setting is: a batch that sets 5 still holds after main's 3."""
        self.status.write_text("3")
        config = self.config()
        config["queue"]["noVerdictRuns"] = 5
        run(self.repo, "git", "fetch", "-q", "origin")
        run(self.repo, "git", "checkout", "-q", "-B", "raise", "origin/main")
        (self.repo / "engineering.json").write_text(json.dumps(config))
        run(self.repo, "git", "add", "engineering.json")
        run(self.repo, "git", "commit", "-qm", "Raise the bound")
        [identifier] = self.add("raise")
        with patch("builtins.print"): self.work()
        self.assertEqual(self.gates(), 3)
        self.assertEqual(self.state(identifier)["state"], "queued")

    def test_the_setting_must_be_a_positive_integer(self):
        start = {"schemaVersion": 1, "name": "fixture", "integration": "direct-main",
                 "lanes": {"gate": {"command": ["true"]}}}
        for runs in (1, 3, 10):
            with self.subTest(runs=runs):
                configuration.parse(json.dumps({**start, "queue": {"noVerdictRuns": runs}}))
        for runs in (0, -1, 2.5, "3", True, None):
            with self.subTest(runs=runs), self.assertRaisesRegex(ValueError, "queue.noVerdictRuns"):
                configuration.parse(json.dumps({**start, "queue": {"noVerdictRuns": runs}}))

    def test_the_schema_knows_the_setting(self):
        # The schema's queue takes no property it does not list, so a project that set the bound failed it.
        queue = json.loads((ROOT / "schemas/engineering.schema.json").read_text())["properties"]["queue"]
        self.assertFalse(queue["additionalProperties"])
        self.assertEqual(queue["properties"]["noVerdictRuns"]["type"], "integer")
        self.assertEqual(queue["properties"]["noVerdictRuns"]["minimum"], 1)

    def test_the_hold_says_why_the_last_run_gave_no_verdict(self):
        worker = mq.Worker(mq.Queue(self.repo))
        log = self.base / "gate.log"
        log.write_text("test_voices (m.Voices) ... skipped 'Unmeasured: the voices were not ready in 500 s'\n"
                       "test_more (m.Voices) ... skipped 'Unmeasured: the voices were not ready in 500 s'\n"
                       "test_more (m.Voices) ... skipped 'Unmeasured: the voices were not ready in 500 s'\n")
        self.assertEqual(worker.no_verdict_cause(3, log),
                         "unmeasured: 2 tests, the first test_voices (m.Voices) ... skipped 'Unmeasured: the voices "
                         "were not ready in 500 s'")
        log.write_text("test_voices (m.Voices) ... skipped 'Unmeasured: " + "x" * 400 + "'\n")
        self.assertEqual(len(worker.no_verdict_cause(3, log).split("the first ", 1)[1]), mq.UNMEASURED_SHOWN)
        self.assertEqual(worker.no_verdict_cause(3, self.base / "missing.log"), "unmeasured, with no test its log names")
        self.assertEqual(worker.no_verdict_cause(75, log), "no admission: the host stayed at capacity")
        self.assertIn("configuration", worker.no_verdict_cause(78, log))
        worker.timed_out = "gate reached its limit: after 7200 s"
        self.assertEqual(worker.no_verdict_cause(3, log), "unmeasured: gate reached its limit: after 7200 s")

    TIMED_OUT = "Owned command timed out after 2400s: python3; it and its observed descendants ended"
    NOTHING = "unmeasured, with no test its log names"

    def stopped_worker(self):
        """A worker whose gate stopped at the lane gate a minute ago, the log that names no test, and where the
        lane's receipt goes (#288)."""
        checkout = Path(tempfile.mkdtemp(dir=self.base))
        worker = mq.Worker.__new__(mq.Worker)
        worker.checkout, worker.timed_out, worker.lane_now = checkout, None, ("gate", time.time() - 60, ())
        log = checkout / "gate.log"
        log.write_text("test_last (m.Passed) ... ok\n")
        receipt = checkout / ".build/engineering/gate/source.json"
        receipt.parent.mkdir(parents=True)
        return worker, log, receipt

    def dispatch_of(self, worker, said):
        """The second Mac's dispatch of the worker's gate, whose receipt holds said, or which has none for None."""
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        run = worker.checkout / f".build/engineering/remote/.dispatches/gate/{stamp}-1"
        run.mkdir(parents=True)
        if said is not None: (run / "source.json").write_text(said)
        return run

    def test_a_no_verdict_hold_quotes_the_error_its_own_receipt_records(self):
        """A lane that reached its own limit: the real eng run, its real receipt, and a log that just stops (#288)."""
        config = self.config()
        config["lanes"]["gate"] = {"command": ["python3", "-c", "import time; time.sleep(120)"], "timeout": 2}
        config["queue"]["noVerdictRuns"] = 1
        self.publish(config)
        self.commit("a", "a")
        [identifier] = self.add("a")
        with patch("builtins.print"): self.work()
        self.assertEqual(self.state(identifier)["state"], "queued")
        held = mq.Queue(self.repo).held()
        self.assertIn(f"the last with exit 3 on this Mac ({offload_host.mac_name()}), unmeasured: Owned command timed "
                      "out after 2s: python3", held)
        self.assertNotIn(self.NOTHING, held)

    def test_the_cause_is_the_receipts_error_when_the_log_names_no_test(self):
        worker, log, receipt = self.stopped_worker()
        receipt.write_text(json.dumps({"outcome": "unmeasured", "error": self.TIMED_OUT}))
        self.assertEqual(worker.no_verdict_cause(3, log), f"unmeasured: {self.TIMED_OUT}")
        # An unreadable log says no more than one that names no test.
        self.assertEqual(worker.no_verdict_cause(3, self.base / "missing.log"), f"unmeasured: {self.TIMED_OUT}")
        # Shortened as a test's name is, and kept on the hold's one line.
        receipt.write_text(json.dumps({"error": "first line\n  second " + "x" * 400}))
        said = worker.no_verdict_cause(3, log).removeprefix("unmeasured: ")
        self.assertEqual(len(said), mq.UNMEASURED_SHOWN)
        self.assertTrue(said.startswith("first line second x"), said)
        self.assertTrue(said.endswith("..."), said)

    def test_the_cause_of_a_run_on_the_second_mac_is_in_the_receipt_that_came_back(self):
        # As remote files it: the lane's own receipt under result, as offload_host read it on the second Mac.
        worker, log, _ = self.stopped_worker()
        run = self.dispatch_of(worker, json.dumps({"host": "build-host", "result": {
            "exitStatus": 3, "receipt": {"outcome": "unmeasured", "error": self.TIMED_OUT}}}))
        self.assertEqual(worker.no_verdict_cause(3, log), f"unmeasured: {self.TIMED_OUT}")
        # What stopped the run there, where the lane wrote no receipt.
        (run / "source.json").write_text(json.dumps({"host": "build-host", "result": {"error": "the checkout is gone"}}))
        self.assertEqual(worker.no_verdict_cause(3, log), "unmeasured: the checkout is gone")

    def test_with_no_receipt_of_this_run_that_says_why_the_cause_is_the_fixed_text(self):
        worker, log, receipt = self.stopped_worker()
        self.assertEqual(worker.no_verdict_cause(3, log), self.NOTHING)  # no receipt at all
        for said in (b"{not json", b'"a string"', b"[1]", b"null", b"\xff\xfe", b'{"outcome": "unmeasured"}',
                     b'{"error": ""}', b'{"error": "  \\n"}', b'{"error": 7}', b'{"error": null}', b'{"result": "text"}',
                     b'{"result": {"receipt": "text"}}'):
            with self.subTest(said=said):
                receipt.write_bytes(said)
                self.assertEqual(worker.no_verdict_cause(3, log), self.NOTHING)
        # An earlier run's receipt is not this one's.
        receipt.write_text(json.dumps({"error": self.TIMED_OUT}))
        os.utime(receipt, (worker.lane_now[1] - 10, worker.lane_now[1] - 10))
        self.assertEqual(worker.no_verdict_cause(3, log), self.NOTHING)
        # A worker that has run no lane has no receipt to read.
        lane_now, worker.lane_now = worker.lane_now, None
        self.assertEqual(worker.no_verdict_cause(3, log), self.NOTHING)
        # A receipt that is a directory cannot be read either.
        worker.lane_now = lane_now
        receipt.unlink()
        receipt.mkdir()
        self.assertEqual(worker.no_verdict_cause(3, log), self.NOTHING)

    def test_a_run_on_the_second_mac_with_an_absent_or_malformed_receipt_gets_the_fixed_text(self):
        for said in (None, "{not json", "[1]", json.dumps({"host": "build-host", "result": {"exitStatus": 3}})):
            with self.subTest(said=said):
                worker, log, _ = self.stopped_worker()
                self.dispatch_of(worker, said)
                self.assertEqual(worker.no_verdict_cause(3, log), self.NOTHING)

    def test_tests_the_log_names_and_the_other_causes_keep_their_words(self):
        worker, log, receipt = self.stopped_worker()
        receipt.write_text(json.dumps({"error": self.TIMED_OUT}))
        log.write_text("test_voices (m.Voices) ... skipped 'Unmeasured: the voices were not ready in 500 s'\n")
        self.assertEqual(worker.no_verdict_cause(3, log), "unmeasured: 1 test, the first test_voices (m.Voices) ... "
                                                          "skipped 'Unmeasured: the voices were not ready in 500 s'")
        self.assertEqual(worker.no_verdict_cause(75, log), "no admission: the host stayed at capacity")
        self.assertIn("configuration", worker.no_verdict_cause(78, log))
        worker.timed_out = "gate reached its limit: after 7200 s"
        self.assertEqual(worker.no_verdict_cause(3, log), "unmeasured: gate reached its limit: after 7200 s")

    BUSY = ("cpu", "the CPU is 95% busy, at or above HEAVY_BUSY_LIMIT=75%")

    def test_a_retry_waits_while_the_load_is_high_and_starts_once_it_drops(self):
        self.status.write_text("3")
        self.commit("a", "a")
        [identifier] = self.add("a")
        # The second gate passes only if it starts after the host settled: the load is what voided the first.
        with self.host([self.BUSY, self.BUSY, self.BUSY, None], then=self.status.unlink) as (slept, said):
            self.assertEqual(self.work(), 0)
        self.assertEqual(slept, [mq.SETTLE_POLL] * 3)
        self.assertEqual(self.gates(), 2)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertIsNone(mq.Queue(self.repo).held())
        log = said.getvalue()
        self.assertIn("the CPU is 95% busy, at or above HEAVY_BUSY_LIMIT=75%: the lanes run again once the host settles", log)
        self.assertIn(f"the host settled after {3 * mq.SETTLE_POLL}s and 4 looks; one-minute load first 40, highest 40, "
                      "last 3: the lanes run again", log)

    def test_a_retry_whose_host_stays_busy_past_the_bound_holds_and_says_why(self):
        self.status.write_text("3")
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.host([self.BUSY]) as (slept, said):
            self.assertEqual(self.work(), 0)
        self.assertEqual(sum(slept), mq.SETTLE_LIMIT)
        self.assertEqual(len(slept), mq.SETTLE_LIMIT // mq.SETTLE_POLL)
        # No second gate and nothing blamed: the entry waits for the owner, and the hold says why.
        self.assertEqual(self.gates(), 1)
        self.assertEqual(self.state(identifier)["state"], "queued")
        held = mq.Queue(self.repo).held()
        self.assertIn(f"the lanes gave no verdict in 1 run in a row, the last with exit 3 on this Mac "
                      f"({offload_host.mac_name()}), unmeasured, with no test its log names, and the host did not settle in {mq.SETTLE_LIMIT}s (the CPU is 95% busy, at or above", held)
        self.assertIn("eng queue resume", held)
        self.assertIn(f"the host did not settle in {mq.SETTLE_LIMIT}s and {len(slept) + 1} looks", said.getvalue())
        # A line every SETTLE_REPORT while it waited, not one per look.
        self.assertEqual(said.getvalue().count("still waiting"), mq.SETTLE_LIMIT // mq.SETTLE_REPORT)
        self.pressure.side_effect = None
        self.status.unlink()
        with patch.object(mq.Queue, "start"), patch("builtins.print"): mq.resume(self.repo)
        self.work()
        self.assertEqual(self.state(identifier)["state"], "landed")

    def test_every_retry_waits_for_the_host_to_settle(self):
        """Each run after the first starts only once the host settled, never into the load that voided the last."""
        self.status.write_text("3")
        self.commit("a", "a")
        [identifier] = self.add("a")
        started = []
        real = mq.Worker.gate
        def gate(worker, *arguments):
            started.append(self.pressure.call_count)
            return real(worker, *arguments)
        with self.host([self.BUSY, None, self.BUSY, self.BUSY, None]) as (slept, said), \
                patch.object(mq.Worker, "gate", gate):
            self.assertEqual(self.work(), 0)
        # The first gate before any look; the second after the second look settled; the third after the fifth.
        self.assertEqual(started, [0, 2, 5])
        self.assertEqual(slept, [mq.SETTLE_POLL] * 3)
        self.assertEqual(self.gates(), 3)
        self.assertEqual(self.state(identifier)["state"], "queued")
        self.assertIn("in 3 runs in a row", mq.Queue(self.repo).held())
        self.assertEqual(said.getvalue().count("settled after"), 2)

    def test_a_failure_comes_back_at_once_without_a_wait(self):
        """Negative control: only no verdict waits for the host. A lane's failure, alone or bisected, never does."""
        self.commit("a", "a"); self.commit("bad", "bad")
        ids = self.add("a", "bad")
        with self.host([self.BUSY]) as (slept, said):
            self.assertEqual(self.work(), 0)
        self.assertEqual([self.state(i)["state"] for i in ids], ["landed", "failed"])
        self.assertEqual(slept, [])
        self.assertEqual(self.pressure.call_count, 0)
        self.assertNotIn("settle", said.getvalue())
        self.assertIsNone(mq.Queue(self.repo).held())

    def test_a_stop_during_the_wait_ends_it_and_leaves_the_entry_waiting(self):
        self.status.write_text("3")
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.host([self.BUSY]) as (slept, _), \
                patch.object(mq.processes, "requested", side_effect=lambda: 15 if slept else None):
            with self.assertRaisesRegex(mq.processes.Interrupted, "waited for the host to settle"): self.work()
        self.assertEqual(slept, [mq.SETTLE_POLL])
        self.assertEqual(self.gates(), 1)
        self.assertEqual(self.state(identifier)["state"], "queued")

    def test_a_worker_that_died_leaves_its_batch_to_the_next(self):
        self.commit("a", "a")
        [identifier] = self.add("a")
        queue = mq.Queue(self.repo)
        queue.mark([queue.read(identifier)], "testing")
        self.work()
        self.assertEqual(self.state(identifier)["state"], "landed")

    def test_a_held_queue_runs_nothing(self):
        self.commit("a", "a")
        [identifier] = self.add("a")
        mq.Queue(self.repo).hold("owner's decision")
        self.work()
        self.assertEqual(self.gates(), 0)
        self.assertEqual(self.state(identifier)["state"], "queued")


class HoldKinds(Fixture):
    """Whom a hold is for, in its reason's first words, and the tries a cause that clears by itself gets first: no
    hold waits for the owner unless nobody else can fix its cause (owner's decision, 2026-11-04,
    vocaro-engineering#173)."""
    FOR_OWNER = "for the owner, since nobody else can fix it: "
    FOR_SESSION = "for a session, not the owner: "
    SESSION_RESUMES = ("; the session that fixes it, or any session that finds it gone, runs `eng queue resume` itself "
                       "and says so")
    OWNER_FIXES = "; once it is fixed, any session that finds the cause gone runs `eng queue resume` and says so"

    def test_a_remote_out_of_reach_past_its_backoff_is_tried_again_and_lands(self):
        self.commit("a", "a")
        [identifier] = self.add("a")
        # The round's own fetch fails every try of its backoff; the next round's goes through.
        with self.remote_says(UNREACHED["https name"], range(1, len(mq.BACKOFF) + 2)) as (fetches, slept), \
                contextlib.redirect_stdout(io.StringIO()) as said:
            self.assertEqual(self.work(), 0)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertIn("a", self.main_files())
        self.assertIsNone(mq.Queue(self.repo).held())
        self.assertEqual(slept, [*mq.BACKOFF, mq.CLEARING_WAITS[0]])
        self.assertIn(f"exited 128 {len(mq.BACKOFF) + 1} times: {UNREACHED['https name']}: a cause that clears by itself, "
                      f"so the round runs again in {mq.CLEARING_WAITS[0]}s, try 1 of {len(mq.CLEARING_WAITS)}",
                      said.getvalue())
        self.assertEqual(self.gates(), 1)

    def test_a_cause_that_does_not_clear_holds_for_a_session_after_the_last_try(self):
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.remote_says(UNREACHED["https name"], range(1, 999)) as (fetches, slept), patch("builtins.print"):
            self.assertEqual(self.work(), 1)
        expected = []
        for wait in mq.CLEARING_WAITS: expected += [*mq.BACKOFF, wait]
        self.assertEqual(slept, expected + list(mq.BACKOFF))
        self.assertEqual(len(fetches), (len(mq.CLEARING_WAITS) + 1) * (len(mq.BACKOFF) + 1))
        held = mq.Queue(self.repo).held()
        self.assertTrue(held.startswith(f"{self.FOR_SESSION}`git fetch"), held)
        self.assertIn(f"exited 128 {len(mq.BACKOFF) + 1} times", held)
        self.assertTrue(held.endswith(f", after {len(mq.CLEARING_WAITS)} more tries of the round over "
                                      f"{sum(mq.CLEARING_WAITS)}s did not clear it{self.SESSION_RESUMES}"), held)
        self.assertNotIn("owner,", held.split(":", 1)[1])
        self.assertEqual(self.state(identifier)["state"], "queued")
        self.assertEqual(self.gates(), 0)

    def test_a_round_that_went_through_starts_the_tries_again(self):
        """Two outages, each past the backoff but within the tries, with a landing between: no hold."""
        self.commit("a", "a"); self.commit("b", "b")
        ids = self.add("a", "b")
        tries = len(mq.BACKOFF) + 1
        failing = set(range(1, tries * len(mq.CLEARING_WAITS) + 1))
        # Every fetch of the first round's tries fails; then, once a and b have landed and c is added, as many again.
        real = mq.Worker.round
        def round_(worker):
            went = real(worker)
            if went and self.state(ids[1])["state"] == "landed" and not self.state_of("c"):
                self.commit("c", "c")
                self.add("c")
                failing.update(range(len(fetches) + 1, len(fetches) + 1 + tries * len(mq.CLEARING_WAITS)))
            return went
        with self.remote_says(UNREACHED["ssh name"], failing) as (fetches, slept), patch("builtins.print"), \
                patch.object(mq.Worker, "round", round_):
            self.assertEqual(self.work(), 0)
        self.assertEqual([self.state(i)["state"] for i in ids], ["landed"] * 2)
        self.assertEqual(self.state_of("c")["state"], "landed")
        self.assertIsNone(mq.Queue(self.repo).held())
        self.assertEqual(slept.count(mq.CLEARING_WAITS[-1]), 2)

    def state_of(self, branch):
        return next((entry for entry in mq.Queue(self.repo).all() if entry["branch"] == branch), None)

    def test_a_refused_push_holds_for_the_owner_at_once_and_says_so(self):
        self.commit("a", "a")
        [identifier] = self.add("a")
        hook = self.origin / "hooks/pre-receive"
        hook.write_text("#!/bin/sh\necho declined by the remote >&2\nexit 1\n")
        host_isolation.executable(hook)
        with patch.object(mq, "pause") as slept, patch("builtins.print"): self.assertEqual(self.work(), 1)
        slept.assert_not_called()
        held = mq.Queue(self.repo).held()
        self.assertTrue(held.startswith(f"{self.FOR_OWNER}the push of "), held)
        self.assertIn(" was refused: ", held)
        self.assertTrue(held.endswith(f"which the resume pushes without gating again{self.OWNER_FIXES}"), held)
        self.assertNotIn(self.FOR_SESSION, held)
        self.assertEqual(self.state(identifier)["state"], "passed")
        with contextlib.redirect_stdout(io.StringIO()) as shown: mq.status(self.repo)
        self.assertIn(f"worker: none; held {self.FOR_OWNER}the push of ", shown.getvalue())
        with contextlib.redirect_stdout(io.StringIO()) as told:
            self.assertEqual(mq.wait(self.repo, identifier, 1), mq.resources.BUSY)
        self.assertIn(f"{identifier} is waiting, and the queue is held {self.FOR_OWNER}the push of ", told.getvalue())
        self.commit("b", "b")
        with patch.object(mq.Queue, "start"), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()) as said:
            later = mq.add(self.repo, "b")
        self.assertIn(f"queue: {later} waits, since the queue is held {self.FOR_OWNER}the push of ", said.getvalue())

    def test_a_refused_fetch_holds_for_the_owner_and_a_dropped_one_is_a_sessions_past_the_tries(self):
        """The negative control of the retry: a refusal is tried no further and names the owner."""
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.remote_says(REFUSED["https authentication"], range(1, 999)) as (fetches, slept), patch("builtins.print"):
            self.assertEqual(self.work(), 1)
        self.assertEqual((len(fetches), slept), (1, []))
        held = mq.Queue(self.repo).held()
        self.assertTrue(held.startswith(f"{self.FOR_OWNER}`git fetch"), held)
        self.assertTrue(held.endswith(self.OWNER_FIXES), held)
        self.assertNotIn("did not clear it", held)

    def test_what_a_session_can_fix_holds_for_a_session_not_the_owner(self):
        """A path where the worker's checkout goes, and a hook that refuses what the gate passed."""
        checkout = self.base / "vocaro-queue/project/project"
        checkout.mkdir(parents=True)
        (checkout / "kept").write_text("x")
        self.commit("a", "a")
        [identifier] = self.add("a")
        with patch("builtins.print"): self.assertEqual(self.work(), 1)
        held = mq.Queue(self.repo).held()
        self.assertEqual(held, f"{self.FOR_SESSION}{checkout} exists and is no worktree of {self.repo}: move it"
                               f"{self.SESSION_RESUMES}")
        self.assertEqual(self.state(identifier)["state"], "queued")
        shutil.rmtree(checkout)
        mq.Queue(self.repo).hold_file.unlink()
        hook = self.repo / ".git/hooks/pre-push"
        hook.write_text("#!/bin/sh\necho the reason >&2\nexit 1\n")
        host_isolation.executable(hook)
        with patch("builtins.print"): self.assertEqual(self.work(), 1)
        held = mq.Queue(self.repo).held()
        self.assertTrue(held.startswith(f"{self.FOR_SESSION}the pre-push hook refused "), held)
        self.assertTrue(held.endswith(f"which the resume pushes without gating again{self.SESSION_RESUMES}"), held)
        self.assertEqual(self.state(identifier)["state"], "passed")

    def test_no_verdict_from_a_misconfigured_host_is_the_owners_and_any_other_a_sessions(self):
        for status, kind in (("78", self.FOR_OWNER), ("3", self.FOR_SESSION), ("75", self.FOR_SESSION)):
            with self.subTest(status=status):
                with contextlib.suppress(OSError): mq.Queue(self.repo).hold_file.unlink()
                self.status.write_text(status)
                self.commit(f"a{status}", f"a{status}")
                [identifier] = self.add(f"a{status}")
                with patch("builtins.print"): self.assertEqual(self.work(), 0)
                held = mq.Queue(self.repo).held()
                self.assertTrue(held.startswith(f"{kind}the lanes gave no verdict in {mq.NO_VERDICT_RUNS} runs in a row, "
                                                f"the last with exit {status} on this Mac "
                                                f"({offload_host.mac_name()}), "), held)
                self.assertTrue(held.endswith(self.OWNER_FIXES if kind == self.FOR_OWNER else self.SESSION_RESUMES), held)
                self.assertEqual(self.state(identifier)["state"], "queued")
                mq.remove(mq.Queue(self.repo), identifier, quiet=True)

    def test_the_kinds_are_told_apart_in_words(self):
        self.assertTrue(mq.worded(mq.OWNER, "x").startswith(self.FOR_OWNER + "x; "))
        self.assertTrue(mq.worded(mq.SESSION, "x").startswith(self.FOR_SESSION + "x; "))
        self.assertNotIn("for the owner", mq.worded(mq.SESSION, "x"))
        hold = mq.Hold(mq.CLEARS, "why")
        self.assertIsInstance(hold, ValueError)
        self.assertEqual((hold.kind, str(hold)), (mq.CLEARS, "why"))


# Files, as `eng run` does for a lane the second Mac ran, a dispatch record in the checkout's
# .build/engineering/remote/.dispatches/LANE/, named for the second it started unless the receipt names
# one, and holding the receipt that sys.argv[1] holds, if any; then runs lane.py with the rest.
DISPATCHING_LANE = """import json, os, pathlib, runpy, sys, time
receipt = pathlib.Path(sys.argv[1])
if receipt.exists():
    said = json.loads(receipt.read_text())
    name = said.pop("name", None) or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + f"-{os.getpid()}"
    run = pathlib.Path(".build/engineering/remote/.dispatches/gate") / name
    run.mkdir(parents=True, exist_ok=True)
    if said: (run / "source.json").write_text(json.dumps(said))
sys.argv = ["lane.py", *sys.argv[2:]]
runpy.run_path("lane.py", run_name="__main__")
"""


class SecondMacSettle(Fixture):
    """The settle wait reads the Mac the lane ran on (#140): the second Mac, faked at offload_host's answer
    (remote.ask), when the lane's dispatch says it ran there; this Mac otherwise."""
    HOST = "fake-mac"
    LOCAL_BUSY = ("cpu", "the CPU is 95% busy, at or above HEAVY_BUSY_LIMIT=75%")
    THERE_BUSY = {"load": 30.0, "cpuBusy": 90.0, "busyLimit": 75.0, "memory": 1}
    THERE_SETTLED = {"load": 2.0, "cpuBusy": 20.0, "busyLimit": 75.0, "memory": 1}

    def setUp(self):
        super().setUp()
        self.receipt = self.base / "receipt"
        (self.repo / "dispatching.py").write_text(DISPATCHING_LANE)
        run(self.repo, "git", "add", "dispatching.py")
        run(self.repo, "git", "commit", "-qm", "Add the dispatching lane")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        config = self.config()
        config["lanes"]["gate"]["command"] = ["python3", "dispatching.py", str(self.receipt), str(self.counter),
                                              str(self.status)]
        self.publish(config)

    def ran_there(self, **receipt):
        self.receipt.write_text(json.dumps({"host": self.HOST, "ranOn": "second Mac", **receipt}))

    @contextlib.contextmanager
    def second_mac(self, answers):
        """The second Mac's answers to the pressure request, one per look, the last repeating; an exception
        among them is raised, as remote.ask raises for a second Mac it cannot reach. Yields the requests."""
        asked = []
        def ask(host, request, timeout=None):
            asked.append((host, request))
            said = answers[min(len(asked) - 1, len(answers) - 1)]
            if isinstance(said, Exception): raise said
            return said
        with patch.object(mq.remote, "ask", side_effect=ask): yield asked

    @contextlib.contextmanager
    def passing_after_no_verdict(self):
        """The retry passes: the status file that voided the first gate goes once it has."""
        real = mq.Worker.gate
        def gate(worker, *arguments):
            result = real(worker, *arguments)
            if result[0] == 3: self.status.unlink()
            return result
        with patch.object(mq.Worker, "gate", gate): yield

    def test_a_lane_the_second_mac_ran_waits_for_that_mac_not_this_one(self):
        self.status.write_text("3")
        self.ran_there()
        self.commit("a", "a")
        [identifier] = self.add("a")
        # This Mac stays busy throughout: a wait that read it would hold, not land.
        with self.second_mac([self.THERE_BUSY, self.THERE_BUSY, self.THERE_SETTLED]) as asked, \
                self.host([self.LOCAL_BUSY]) as (slept, said), self.passing_after_no_verdict():
            self.assertEqual(self.work(), 0)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(self.gates(), 2)
        self.assertEqual(slept, [mq.SETTLE_POLL] * 2)
        self.assertEqual(asked, [(self.HOST, {"pressure": True})] * 3)
        self.assertEqual(self.pressure.call_count, 0)
        log = said.getvalue()
        self.assertIn(f"no verdict (exit 3) from {self.HOST}", log)
        self.assertIn(f"on {self.HOST}, the CPU is 90% busy, at or above HEAVY_BUSY_LIMIT=75%: the lanes run again once "
                      f"{self.HOST} settles", log)
        self.assertIn(f"{self.HOST} settled after {2 * mq.SETTLE_POLL}s and 3 looks; one-minute load first 30, "
                      "highest 30, last 2: the lanes run again", log)

    def test_a_second_mac_that_stays_busy_holds_naming_it(self):
        self.status.write_text("3")
        self.ran_there()
        self.commit("a", "a")
        [identifier] = self.add("a")
        # This Mac is settled throughout: a wait that read it would run the lanes again at once.
        with self.second_mac([{"load": 1.0, "cpuBusy": 10.0, "busyLimit": 75.0, "memory": 2}]), self.host([None]) as (slept, said):
            self.assertEqual(self.work(), 0)
        self.assertEqual(sum(slept), mq.SETTLE_LIMIT)
        self.assertEqual(self.gates(), 1)
        self.assertEqual(self.state(identifier)["state"], "queued")
        self.assertEqual(self.pressure.call_count, 0)
        self.assertIn(f"{self.HOST} did not settle in {mq.SETTLE_LIMIT}s (on {self.HOST}, memory pressure is level 2 "
                      "(warning), not 1 (normal))", mq.Queue(self.repo).held())

    def test_a_lane_that_ran_here_reads_only_this_mac(self):
        """Negative control: no dispatch record, so the second Mac is never asked."""
        self.status.write_text("3")
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.second_mac([AssertionError("the second Mac was asked")]) as asked, \
                self.host([self.LOCAL_BUSY, None], then=self.status.unlink) as (slept, said):
            self.assertEqual(self.work(), 0)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(asked, [])
        self.assertEqual(self.pressure.call_count, 2)
        self.assertIn("the host settled after", said.getvalue())
        self.assertNotIn(self.HOST, said.getvalue())

    def test_a_dispatch_that_only_prepared_or_started_before_the_lane_reads_this_mac(self):
        """Negative controls: nothing ran on the second Mac during this lane's run."""
        for case, receipt in [("prepared", {"stage": "preparation", "outcome": "failed-or-interrupted"}),
                              ("earlier", {"name": "20200101T000000Z-1"})]:
            with self.subTest(case):
                self.status.write_text("3")
                self.ran_there(**receipt)
                self.commit(f"a-{case}", f"a-{case}")
                [identifier] = self.add(f"a-{case}")
                self.pressure.reset_mock()
                with self.second_mac([AssertionError("the second Mac was asked")]) as asked, \
                        self.host([self.LOCAL_BUSY, None], then=self.status.unlink):
                    self.assertEqual(self.work(), 0)
                self.assertEqual(self.state(identifier)["state"], "landed")
                self.assertEqual(asked, [])
                self.assertEqual(self.pressure.call_count, 2)

    def test_a_second_mac_out_of_reach_falls_back_to_this_macs_reading_saying_so(self):
        self.status.write_text("3")
        self.ran_there()
        self.commit("a", "a")
        [identifier] = self.add("a")
        lost = mq.remote.Misconfigured(f"{self.HOST} cannot be reached: ssh: Could not resolve hostname", "wake it")
        # Out of reach at both looks, while this Mac is busy and then settled; asked again at each.
        with self.second_mac([lost]) as asked, \
                self.host([self.LOCAL_BUSY, None], then=self.status.unlink) as (slept, said):
            self.assertEqual(self.work(), 0)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(len(asked), 2)
        self.assertEqual(self.pressure.call_count, 2)
        log = said.getvalue()
        self.assertEqual(log.count("cannot be read, so the wait reads this Mac's load instead"), 1)
        self.assertIn(f"{self.HOST}, where the lanes ran, cannot be read, so the wait reads this Mac's load instead: "
                      f"{self.HOST} cannot be reached", log)
        self.assertIn("the host settled after", log)

    def test_a_second_mac_that_answers_again_is_read_again(self):
        self.status.write_text("3")
        self.ran_there()
        self.commit("a", "a")
        [identifier] = self.add("a")
        lost = mq.remote.Unavailable(f"{self.HOST} did not report its load: it answered None")
        with self.second_mac([lost, self.THERE_SETTLED]), self.host([self.LOCAL_BUSY]) as (slept, said), \
                self.passing_after_no_verdict():
            self.assertEqual(self.work(), 0)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(self.pressure.call_count, 1)
        self.assertIn(f"{self.HOST}'s load can be read again: reading it", said.getvalue())
        self.assertIn(f"{self.HOST} settled after {mq.SETTLE_POLL}s and 2 looks", said.getvalue())

    # Before a retry the wait reads the Mac the retry could use, not the one that voided the last run (#253): two
    # fake Macs, the second by its capacity and pressure answers and this one by its capacity and resources.pressure.
    FREE = {"slots": 3, "slotsMax": 3, "busy": [], "busyLimit": 75.0}
    FULL = {"slots": 3, "slotsMax": 3, "busy": [0, 1, 2], "busyLimit": 75.0}

    @contextlib.contextmanager
    def macs(self, there, here):
        """A second Mac whose capacity reads there and this Mac's here, each a reading or a function of nothing."""
        read = lambda found: found() if callable(found) else found
        with patch.object(mq.remote, "configured", return_value=self.HOST), \
                patch.object(mq.remote, "capacity_there", side_effect=lambda host: read(there)), \
                patch.object(mq.offload_host, "capacity", side_effect=lambda **_: read(here)):
            yield

    def test_a_loaded_second_mac_the_retry_would_use_is_waited_for_though_this_mac_voided_the_run(self):
        self.status.write_text("3")
        self.commit("a", "a")
        [identifier] = self.add("a")
        # The run was here, and this Mac has settled; the old wait read this Mac and retried at once.
        with self.macs(self.FREE, self.FREE), self.second_mac([self.THERE_BUSY, self.THERE_SETTLED]) as asked, \
                self.host([None]) as (slept, said), self.passing_after_no_verdict():
            self.assertEqual(self.work(), 0)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(slept, [mq.SETTLE_POLL])
        self.assertEqual(asked, [(self.HOST, {"pressure": True})] * 2)
        self.assertEqual(self.pressure.call_count, 0)
        log = said.getvalue()
        self.assertIn(f"the lanes run next on {self.HOST}, which has spare capacity for gate and is asked first", log)
        self.assertIn(f"the lanes run again once {self.HOST} settles", log)

    def test_a_loaded_this_mac_the_retry_would_use_is_waited_for_though_the_second_mac_voided_the_run(self):
        """2026-11-07's shape: the wait settled build-host, and the retry went to this Mac."""
        self.status.write_text("3")
        self.ran_there()
        self.commit("a", "a")
        [identifier] = self.add("a")
        # This Mac has the free slot; the second Mac voided the run, has none, and reads settled throughout.
        with self.macs(self.FULL, self.FREE), self.second_mac([self.THERE_SETTLED]) as asked, \
                self.host([self.LOCAL_BUSY, None], then=self.status.unlink) as (slept, said):
            self.assertEqual(self.work(), 0)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(slept, [mq.SETTLE_POLL])
        self.assertEqual(asked, [])
        self.assertEqual(self.pressure.call_count, 2)
        log = said.getvalue()
        self.assertIn("the lanes run next on this Mac, the one with spare capacity for gate", log)
        self.assertIn("the lanes run again once this Mac settles", log)
        self.assertIn("this Mac settled after", log)

    def test_a_settled_target_does_not_wait_for_the_loaded_mac_it_will_not_use(self):
        """Negative controls, each way round: the Mac that will not take the retry stays loaded."""
        for name, there, here, ran_there, answers, looks, read_here in [
                ("second", self.FREE, self.FREE, False, [self.THERE_SETTLED], [self.LOCAL_BUSY], 0),
                ("this", self.FULL, self.FREE, True, [self.THERE_BUSY], [None], 1)]:
            with self.subTest(name):
                self.status.write_text("3")
                if ran_there: self.ran_there()
                else: self.receipt.unlink(missing_ok=True)
                self.commit(f"a-{name}", f"a-{name}")
                [identifier] = self.add(f"a-{name}")
                self.pressure.reset_mock()
                with self.macs(there, here), self.second_mac(answers) as asked, self.host(looks) as (slept, said), \
                        self.passing_after_no_verdict():
                    self.assertEqual(self.work(), 0)
                self.assertEqual(self.state(identifier)["state"], "landed")
                self.assertEqual(slept, [])
                self.assertEqual(len(asked), 1 - read_here)
                self.assertEqual(self.pressure.call_count, read_here)
                self.assertNotIn("settles", said.getvalue())

    def test_with_neither_mac_free_the_retry_waits_for_both_since_either_may_admit_it(self):
        self.status.write_text("3")
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.macs(self.FULL, self.FULL), self.second_mac([self.THERE_SETTLED]) as asked, \
                self.host([self.LOCAL_BUSY, None], then=self.status.unlink) as (slept, said):
            self.assertEqual(self.work(), 0)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(slept, [mq.SETTLE_POLL])
        self.assertEqual(len(asked), 2)
        self.assertEqual(self.pressure.call_count, 2)
        log = said.getvalue()
        self.assertIn("the lanes run next on whichever Mac admits gate first, since neither has spare capacity", log)
        self.assertIn(f"{self.HOST} and this Mac settled after", log)

    def test_the_target_is_read_at_each_look_as_slots_come_and_go(self):
        self.status.write_text("3")
        self.commit("a", "a")
        [identifier] = self.add("a")
        readings = iter([self.FREE, self.FULL])
        # The second Mac has a slot at the first look, loaded, and none at the second, when this Mac has settled.
        with self.macs(lambda: next(readings, self.FULL), self.FREE), self.second_mac([self.THERE_BUSY]) as asked, \
                self.host([None], then=self.status.unlink) as (slept, said):
            self.assertEqual(self.work(), 0)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(slept, [mq.SETTLE_POLL])
        self.assertEqual(len(asked), 1)
        self.assertEqual(self.pressure.call_count, 1)
        self.assertEqual(said.getvalue().count("the lanes run next on "), 2)

    def test_a_second_mac_whose_capacity_cannot_be_read_leaves_this_mac_the_retry_goes_to(self):
        self.status.write_text("3")
        self.ran_there()
        self.commit("a", "a")
        [identifier] = self.add("a")
        def away(): raise mq.remote.Unreachable(f"{self.HOST} cannot be reached: it is asleep", "wake it")
        with self.macs(away, self.FREE), self.second_mac([AssertionError("the second Mac was read")]) as asked, \
                self.host([None], then=self.status.unlink) as (slept, said):
            self.assertEqual(self.work(), 0)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(asked, [])
        self.assertIn(f"the lanes run next on this Mac, since {self.HOST}'s capacity cannot be read: {self.HOST} "
                      "cannot be reached: it is asleep", said.getvalue())

    def test_a_lane_that_stays_on_this_mac_reads_only_this_mac(self):
        self.status.write_text("3")
        config = self.config()
        config["lanes"]["gate"]["here"] = "a measurement tied to this Mac"
        self.publish(config)
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.macs(AssertionError, AssertionError), self.second_mac([AssertionError("the second Mac was read")]) as asked, \
                self.host([self.LOCAL_BUSY, None], then=self.status.unlink) as (slept, said):
            self.assertEqual(self.work(), 0)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(asked, [])
        self.assertIn("the lanes run next on this Mac, since gate runs only there", said.getvalue())


class SecondMacReading(unittest.TestCase):
    """remote.pressure_there and remote.dispatched alone, and the reading offload_host gives."""

    def test_the_second_macs_reading_is_judged_by_its_own_limit(self):
        # The load is what the queue logs; the busy share is what judges, so a high load alone refuses nothing.
        cases = [({"load": 2.0, "cpuBusy": 75.0, "busyLimit": 75.0, "memory": 1},
                  ("cpu", "the CPU is 75% busy, at or above HEAVY_BUSY_LIMIT=75%")),
                 ({"load": 66.0, "cpuBusy": 74.9, "busyLimit": 75.0, "memory": 1}, None),
                 ({"load": 2.0, "cpuBusy": 40.0, "busyLimit": 75.0, "memory": 4},
                  ("memory", "memory pressure is level 4 (critical), not 1 (normal)")),
                 ({"load": None, "cpuBusy": 40.0, "busyLimit": 75.0, "memory": None},
                  ("memory", "memory pressure, which must be level 1 (normal), cannot be read")),
                 ({"load": 2.0, "cpuBusy": None, "busyLimit": 75.0, "memory": 1},
                  ("cpu", "the CPU's busy share, which HEAVY_BUSY_LIMIT=75% bounds, is unavailable")),
                 ({"load": 2.0, "cpuBusy": True, "busyLimit": 75.0, "memory": 1},
                  ("cpu", "the CPU's busy share, which HEAVY_BUSY_LIMIT=75% bounds, is unavailable"))]
        for answer, expected in cases:
            with self.subTest(answer=answer), patch.object(mq.remote, "ask", return_value=answer) as ask:
                self.assertEqual(mq.remote.pressure_there("fake-mac"), (expected, answer["load"]))
                ask.assert_called_once_with("fake-mac", {"pressure": True}, timeout=mq.remote.CENSUS_TIMEOUT)

    def test_a_macs_capacity_is_judged_by_its_slots_its_cpu_and_what_runs_there(self):
        """remote.spare, which eng run judges both Macs by before it chooses one (vocaro-engineering#168)."""
        idle = {"slots": 3, "slotsMax": 5, "busy": [], "load": 2.0, "cpuBusy": 20.0, "busyLimit": 75.0, "wholeHost": [],
                "exclusive": []}
        cases = [
            (idle, {}, None),
            ({**idle, "busy": [0, 1]}, {}, None),
            ({**idle, "busy": [0, 1, 2]}, {}, "every guaranteed slot of the second Mac is busy (HEAVY_SLOTS=3)"),
            ({**idle, "busy": [0, 1, 2, 3]}, {}, "every guaranteed slot of the second Mac is busy (HEAVY_SLOTS=3)"),
            ({**idle, "busy": [3, 4]}, {}, None),
            # A free guaranteed slot is spare capacity however busy the CPU, as admission admits work into one
            # (owner's decision, 2026-11-07): the second Mac at 93% against its 90% with two of four slots free.
            ({**idle, "cpuBusy": 75.0}, {}, None),
            ({**idle, "cpuBusy": 99.6}, {}, None),
            ({**idle, "cpuBusy": None}, {}, None),
            ({**idle, "slots": 4, "busy": [0, 1], "cpuBusy": 93.0, "busyLimit": 90.0}, {}, None),
            # The negative control: with every guaranteed slot busy, the Mac still refuses, whatever its CPU.
            ({**idle, "busy": [0, 1, 2], "cpuBusy": 10.0}, {}, "every guaranteed slot of the second Mac is busy (HEAVY_SLOTS=3)"),
            # The case of vocaro-engineering#200: a load of 40 against the old limit of 7.5, with the CPU half idle.
            ({**idle, "load": 40.0, "cpuBusy": 52.0}, {}, None),
            ({**idle, "busy": [0, 1, 2], "wholeHost": ["EPUBLib gate"]}, {},
             "the second Mac is running a whole-host lane: EPUBLib gate"),
            ({**idle, "busy": [0, 1, 2, 3, 4], "exclusive": ["Pagepour timed"]}, {},
             "exclusive work holds the second Mac: Pagepour timed"),
            # A whole-host lane needs every guaranteed slot; an exclusive one every slot.
            ({**idle, "busy": [0]}, {"whole_host": True},
             "a whole-host lane needs every guaranteed slot of the second Mac (HEAVY_SLOTS=3) free, and 1 is busy"),
            ({**idle, "busy": [3]}, {"whole_host": True}, None),
            ({**idle, "busy": [3]}, {"exclusive": True},
             "exclusive work needs every slot of the second Mac up to HEAVY_SLOTS_MAX=5 free, and 1 is busy"),
            ({**idle, "busy": [0, 3]}, {"exclusive": True},
             "exclusive work needs every slot of the second Mac up to HEAVY_SLOTS_MAX=5 free, and 2 are busy"),
            ({**idle, "cpuBusy": 80.0}, {"whole_host": True}, None),
            ({**idle, "cpuBusy": 80.0}, {"exclusive": True}, None),
        ]
        for reading, need, expected in cases:
            with self.subTest(reading=reading, need=need):
                self.assertEqual(mq.remote.spare(reading, "the second Mac", **need), expected)
        self.assertIsNone(mq.remote.spare({**idle, "cpuBusy": 90.0}, "this Mac"))
        # Beyond the guaranteed slots, the CPU's busy share still bounds the work (KeptSlotsTests).
        self.assertEqual(mq.remote.spare({**idle, "cpuBusy": 90.0, "kept": "EPUBLib gate"}, "this Mac"),
                         "this Mac is busy (CPU 90% busy, at or above its HEAVY_BUSY_LIMIT=75%)")
        # The second Mac's reading comes from offload_host, which must answer with one; an error there is its
        # owner's to fix, as a limits file admission cannot read refuses every request on that host.
        with patch.object(mq.remote, "ask", return_value=idle) as ask:
            self.assertEqual(mq.remote.capacity_there("fake-mac"), idle)
            ask.assert_called_once_with("fake-mac", {"capacity": True}, timeout=mq.remote.CENSUS_TIMEOUT, unsent=True)
        for answer in [{"load": 1.0}, ["no"], None, {**idle, "busy": "none"}, {**idle, "busyLimit": None}]:
            with self.subTest(answer=answer), patch.object(mq.remote, "ask", return_value=answer):
                with self.assertRaisesRegex(mq.remote.Unavailable, "fake-mac did not report its capacity"):
                    mq.remote.capacity_there("fake-mac")
        with patch.object(mq.remote, "ask", return_value={"error": "Invalid resource limits: x is not JSON"}):
            with self.assertRaisesRegex(mq.remote.Misconfigured, "fake-mac could not read its admission limits: Invalid "
                                                                  "resource limits: x is not JSON"):
                mq.remote.capacity_there("fake-mac")

    def test_the_second_macs_busy_share_and_limit_are_what_a_gate_records(self):
        for answer, expected in (({"load": 2.0, "cpuBusy": 82.5, "busyLimit": 75, "memory": 1}, (82.5, 75.0)),
                                 ({"load": 2.0, "cpuBusy": None, "busyLimit": 60.0}, (None, 60.0)),
                                 ({"load": 2.0, "cpuBusy": True, "busyLimit": 75.0}, (None, 75.0))):
            with self.subTest(answer=answer), patch.object(mq.remote, "ask", return_value=answer):
                self.assertEqual(mq.remote.busy_there("fake-mac"), expected)
        with patch.object(mq.remote, "ask", return_value={"load": 1.0}):
            with self.assertRaisesRegex(mq.remote.Unavailable, "fake-mac did not report its CPU busy share"):
                mq.remote.busy_there("fake-mac")

    def test_a_gates_busy_reading_is_of_the_mac_it_ran_on_and_none_when_unreadable(self):
        with patch.object(mq.remote, "busy_there", return_value=(90.0, 75.0)) as there:
            self.assertEqual(mq.busy_of("fake-mac"), (90.0, 75.0, "fake-mac"))
            there.assert_called_once_with("fake-mac")
        with patch.object(mq.remote, "busy_there", return_value=(None, 75.0)):
            self.assertIsNone(mq.busy_of("fake-mac"))
        with patch.object(mq.remote, "busy_there", side_effect=mq.remote.Unavailable("gone")):
            self.assertIsNone(mq.busy_of("fake-mac"))
        with patch.object(mq.resources, "cpu_busy", return_value=(40.0, 15.0)) as here, \
                patch.object(mq.resources, "busy_limit", return_value=70.0):
            self.assertEqual(mq.busy_of(None), (40.0, 70.0, "this Mac"))
            self.assertTrue(here.call_args.kwargs["wait"])
        with patch.object(mq.resources, "busy_limit", side_effect=ValueError("bad limits")):
            self.assertIsNone(mq.busy_of(None))

    def test_a_second_mac_without_a_reading_is_unavailable(self):
        for answer in [{"error": "HEAVY_BUSY_LIMIT is no number: x"}, {"load": 1.0}, {"load": 1.0, "limit": 7.5}, ["no"], None]:
            with self.subTest(answer=answer), patch.object(mq.remote, "ask", return_value=answer):
                with self.assertRaisesRegex(mq.remote.Unavailable, "fake-mac did not report its CPU busy share"):
                    mq.remote.pressure_there("fake-mac")

    def test_the_reading_there_uses_admissions_limit(self):
        from queuekeeper import offload_host, resources
        with patch.dict(os.environ, {"HEAVY_BUSY_LIMIT": "50"}), patch.object(offload_host, "cpu_busy", return_value=30.0):
            read = offload_host.pressure()
            self.assertEqual((read["busyLimit"], read["cpuBusy"]), (resources.busy_limit(), 30.0))
        with patch.dict(os.environ), patch.object(offload_host, "cpu_busy", return_value=30.0):
            os.environ.pop("HEAVY_BUSY_LIMIT", None)
            self.assertEqual(offload_host.pressure()["busyLimit"], resources.busy_limit())
        self.assertIsInstance(read["load"], float)
        with patch.dict(os.environ, {"HEAVY_BUSY_LIMIT": "x"}):
            self.assertIn("error", offload_host.pressure())
        # Answered as a request, not taken for a plan to run.
        done = subprocess.run([sys.executable, str(ROOT / "src/python/queuekeeper/offload_host.py"),
                               json.dumps({"pressure": True})], capture_output=True, text=True, timeout=stretched(60))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(set(json.loads(done.stdout.splitlines()[-1])), {"load", "cpuBusy", "busyLimit", "memory"})

    def test_only_a_dispatch_that_ran_there_since_the_lane_began_names_its_mac(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        runs = root / ".build/engineering/remote/.dispatches/gate"
        def dispatch(name, receipt):
            (runs / name).mkdir(parents=True)
            if receipt is not None: (runs / name / "source.json").write_text(json.dumps(receipt))
        since = 1_800_000_000.5  # 2027-01-15T08:00:00.5Z
        self.assertIsNone(mq.remote.dispatched(root, "gate", since))
        dispatch("20270115T075959Z-1", {"host": "earlier"})
        self.assertIsNone(mq.remote.dispatched(root, "gate", since))
        # Named for the second it started, which holds since.
        dispatch("20270115T080000Z-2", {"host": "fake-mac"})
        self.assertEqual(mq.remote.dispatched(root, "gate", since), "fake-mac")
        # A later dispatch that only prepared ran nothing there; the one before it did.
        dispatch("20270115T080100Z-3", {"host": "other", "stage": "preparation"})
        self.assertEqual(mq.remote.dispatched(root, "gate", since), "fake-mac")
        # One lost, with no receipt, ran on the configured second Mac.
        dispatch("20270115T080200Z-4", None)
        with patch.dict(os.environ, {"VOCARO_OFFLOAD_HOST": "configured-mac"}):
            self.assertEqual(mq.remote.dispatched(root, "gate", since), "configured-mac")
        with patch.dict(os.environ, {"VOCARO_OFFLOAD_HOST": "none"}):
            self.assertIsNone(mq.remote.dispatched(root, "gate", since))
        self.assertIsNone(mq.remote.dispatched(root, "other-lane", since))


class MainMoves(Fixture):
    def during_gate(self, name, content="x"):
        """Push name to main directly, as a session outside the queue does, while the lanes run."""
        real = mq.Worker.gate
        outside = self.base / "outside"
        run(self.base, "git", "clone", "-q", str(self.origin), str(outside))
        pushed = []
        def gate(worker, lanes, round_name):
            outcome = real(worker, lanes, round_name)
            if not pushed:
                pushed.append(True)
                path = outside / name
                path.parent.mkdir(exist_ok=True)
                path.write_text(content)
                run(outside, "git", "pull", "-q", "origin", "main")
                run(outside, "git", "add", name)
                run(outside, "git", "commit", "-qm", f"Outside {name}")
                run(outside, "git", "push", "-q", "origin", "HEAD:main")
            return outcome
        return patch.object(mq.Worker, "gate", gate)

    def test_a_record_on_main_carries_the_result_over(self):
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.during_gate("records/one.json"): self.work()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(self.gates(), 1)
        self.assertLessEqual({"a", "records"}, self.main_files())

    def test_a_code_read_markdown_input_on_main_gates_again(self):
        config = self.config()
        config["queue"]["docsOnly"] = {"paths": {"README.md": "prose"}, "lanes": ["docs"],
                                        "exclude": {"README.md": "the code gate reads this input"}}
        self.publish(config)
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.during_gate("README.md", "changed input\n"): self.work()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(self.gates(), 2)

    def test_disabling_docs_only_disables_automatic_markdown_carry_over(self):
        config = self.config()
        config["queue"]["docsOnly"] = False
        self.publish(config)
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.during_gate("README.md", "changed input\n"): self.work()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(self.gates(), 2)

    def test_code_on_main_gates_again(self):
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.during_gate("code"): self.work()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(self.gates(), 2)

    def test_the_new_code_is_what_the_second_gate_reads(self):
        self.commit("right", "right")
        [identifier] = self.add("right")
        with self.during_gate("left"): self.work()
        self.assertEqual(self.state(identifier)["state"], "failed")
        self.assertNotIn("right", self.main_files())

    def test_a_document_the_batch_also_changes_gates_again(self):
        self.commit("guide.md", "doc", "ours")
        [identifier] = self.add("doc")
        with self.during_gate("guide.md", "theirs"): self.work()
        self.assertEqual(self.state(identifier)["state"], "conflict")
        self.assertEqual(self.gates(), 1)


class PrePushHooks(Fixture):
    def setUp(self):
        super().setUp()
        self.ran = self.base / "hook-runs"

    def hook(self, body, directory=None):
        """A pre-push hook that records each run's arguments and stdin, then runs body."""
        path = (directory or self.repo / ".git/hooks") / "pre-push"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'#!/bin/sh\nread -r told\necho "$1 $2 $told" >> "{self.ran}"\n{body}\n')
        host_isolation.executable(path)

    def runs(self):
        return self.ran.read_text().splitlines() if self.ran.exists() else []

    def opt_in(self, covering, branch=None):
        """Name covering in engineering.json, directly on main or on a branch of its own."""
        config = json.loads((self.repo / "engineering.json").read_text())
        config["queue"]["hookCoveredBy"] = covering
        run(self.repo, "git", "fetch", "-q", "origin")
        run(self.repo, "git", "checkout", "-q", "-B", branch or "opt-in", "origin/main")
        (self.repo / "engineering.json").write_text(json.dumps(config))
        run(self.repo, "git", "add", "engineering.json")
        run(self.repo, "git", "commit", "-qm", "Opt in")
        if not branch: run(self.repo, "git", "push", "-q", "origin", "HEAD:main")

    def test_a_passing_hook_lands_and_was_told_what_git_tells_it(self):
        tip = self.commit("a", "a")
        before = run(self.repo, "git", "rev-parse", "origin/main")
        [identifier] = self.add("a")
        self.hook("exit 0")
        self.work()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(self.gates(), 1)
        landed = self.state(identifier)["landedAs"]
        self.assertEqual(run(self.repo, "git", "log", "-1", "--format=%T", landed),
                         run(self.repo, "git", "log", "-1", "--format=%T", tip))
        self.assertEqual(self.runs(), [f"origin {self.origin} HEAD {landed} refs/heads/main {before}"])

    def test_no_hook_lands(self):
        self.commit("a", "a")
        [identifier] = self.add("a")
        self.work()
        self.assertEqual(self.state(identifier)["state"], "landed")

    def test_a_hook_tracked_under_a_relative_hooks_path_runs_from_the_tested_commit(self):
        self.hook(f'grep -q marker hooks/pre-push && touch "{self.base}/saw-tree"', self.repo / "hooks")
        with (self.repo / "hooks/pre-push").open("a") as file: file.write("# marker\n")
        run(self.repo, "git", "add", "hooks")
        run(self.repo, "git", "commit", "-qm", "Track the hook")
        run(self.repo, "git", "push", "-q", "--no-verify", "origin", "HEAD:main")
        run(self.repo, "git", "config", "core.hooksPath", "hooks")
        self.commit("a", "a")
        [identifier] = self.add("a")
        self.work()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(len(self.runs()), 1)
        self.assertTrue((self.base / "saw-tree").exists())

    def test_the_hook_runs_before_any_connection(self):
        """The drop on 2026-10-31 came from a connection held open through the hook's wait."""
        order = self.base / "order"
        wrapper = self.base / "receive-pack"
        wrapper.write_text(f'#!/bin/sh\necho connect >> "{order}"\nexec git-receive-pack "$@"\n')
        host_isolation.executable(wrapper)
        run(self.repo, "git", "config", "remote.origin.receivepack", str(wrapper))
        self.hook(f'echo hook >> "{order}"')
        self.commit("a", "a")
        [identifier] = self.add("a")
        self.work()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(order.read_text().split(), ["hook", "connect"])

    def test_a_hook_refusal_holds_after_one_run_and_names_its_log(self):
        self.hook("echo the reason >&2\nexit 1")
        self.commit("a", "a")
        [identifier] = self.add("a")
        self.assertEqual(self.work(), 1)
        self.assertEqual(self.state(identifier)["state"], "passed")
        self.assertNotIn("a", self.main_files())
        self.assertEqual(len(self.runs()), 1)
        held = mq.Queue(self.repo).held()
        self.assertIn("refused", held)
        self.assertTrue(held.startswith("for a session, not the owner: "), held)
        self.assertNotIn("the reason", held)
        self.assertIn("the reason", Path(held.split("see ")[1].split(";")[0]).read_text())

    def test_no_verdict_from_the_hook_tries_it_again_without_another_gate(self):
        for status in (3, 75, 78):
            with self.subTest(status=status):
                self.ran.unlink(missing_ok=True)
                self.hook(f'[ "$(wc -l < "{self.ran}")" -gt 1 ] && exit 0\nexit {status}')
                gates = self.gates()
                self.commit(f"a{status}", f"a{status}")
                [identifier] = self.add(f"a{status}")
                self.assertEqual(self.work(), 0)
                self.assertEqual(self.state(identifier)["state"], "landed")
                self.assertEqual(len(self.runs()), 2)
                self.assertEqual(self.gates(), gates + 1)

    def test_no_verdict_from_the_hook_waits_for_the_host_before_it_runs_again(self):
        self.hook(f'[ "$(wc -l < "{self.ran}")" -gt 1 ] && exit 0\nexit 3')
        self.commit("a", "a")
        [identifier] = self.add("a")
        busy = ("memory", "memory pressure is level 2 (warning), not 1 (normal)")
        with self.host([busy, None]) as (slept, said):
            self.assertEqual(self.work(), 0)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(slept, [mq.SETTLE_POLL])
        self.assertEqual(len(self.runs()), 2)
        self.assertIn("memory pressure is level 2 (warning), not 1 (normal): the pre-push hook runs again once the host "
                      "settles", said.getvalue())

    def test_a_hook_that_gave_no_verdict_holds_when_the_host_stays_busy(self):
        self.hook("exit 3")
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.host([NoVerdict.BUSY]) as (slept, _):
            self.assertEqual(self.work(), 1)
        self.assertEqual(sum(slept), mq.SETTLE_LIMIT)
        self.assertEqual(len(self.runs()), 1)
        self.assertEqual(self.state(identifier)["state"], "passed")
        held = mq.Queue(self.repo).held()
        self.assertIn("the pre-push hook gave no verdict, exit 3, on", held)
        self.assertIn(f"the host did not settle in {mq.SETTLE_LIMIT}s", held)

    def test_a_hook_that_keeps_giving_no_verdict_holds_and_blames_nobody(self):
        self.hook("exit 75")
        self.commit("a", "a")
        [identifier] = self.add("a")
        self.assertEqual(self.work(), 1)
        self.assertEqual(self.state(identifier)["state"], "passed")
        self.assertEqual(len(self.runs()), 2)
        self.assertEqual(self.gates(), 1)
        self.assertIn("no verdict, exit 75", mq.Queue(self.repo).held())

    def test_a_dropped_connection_does_not_run_the_hook_again(self):
        self.hook("exit 0")
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.remote_says("fatal: the remote end hung up unexpectedly", {1}, "push"): self.work()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(len(self.runs()), 1)

    def test_a_carried_over_commit_is_a_new_commit_and_runs_the_hook_again(self):
        outside = self.base / "outside"
        run(self.base, "git", "clone", "-q", str(self.origin), str(outside))
        (outside / "records").mkdir()
        (outside / "records/one.json").write_text("{}")
        run(outside, "git", "add", "records")
        run(outside, "git", "commit", "-qm", "A record")
        self.hook(f'[ "$(wc -l < "{self.ran}")" -gt 1 ] && exit 0\n'
                  f'unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE\ngit -C "{outside}" push -q origin HEAD:main\nexit 0')
        self.commit("a", "a")
        [identifier] = self.add("a")
        self.work()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(self.gates(), 1)
        first, second = [line.split()[3] for line in self.runs()]
        self.assertNotEqual(first, second)
        self.assertEqual(second, self.state(identifier)["landedAs"])
        self.assertLessEqual({"a", "records"}, self.main_files())

    def test_main_opting_in_replaces_the_hook_and_says_so(self):
        self.opt_in({"gate": "all the hook runs"})
        self.hook("exit 1")
        self.commit("a", "a")
        [identifier] = self.add("a")
        said = []
        with patch.object(mq.Worker, "say", lambda worker, words: said.append(words)): self.work()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(self.runs(), [])
        self.assertTrue(any("hookCoveredBy says gate" in words for words in said), said)

    def test_a_covering_lane_that_fails_still_stops_the_commit(self):
        self.opt_in({"gate": "all the hook runs"})
        self.hook("exit 0")
        self.commit("bad", "bad")
        [identifier] = self.add("bad")
        self.work()
        self.assertEqual(self.state(identifier)["state"], "failed")
        self.assertNotIn("bad", self.main_files())

    def test_a_branch_cannot_opt_itself_out_of_the_hook(self):
        self.opt_in({"gate": "all the hook runs"}, branch="sneaks")
        self.hook("exit 1")
        [identifier] = self.add("sneaks")
        self.assertEqual(self.work(), 1)
        self.assertEqual(self.state(identifier)["state"], "passed")
        self.assertEqual(len(self.runs()), 1)

    def test_the_commit_that_first_names_the_lanes_cannot_opt_out_either(self):
        config = json.loads((self.repo / "engineering.json").read_text())
        config["lanes"]["checks"] = config["lanes"].pop("gate")
        (self.repo / "engineering.json").write_text(json.dumps(config))
        run(self.repo, "git", "add", "engineering.json")
        run(self.repo, "git", "commit", "-qm", "No gate lane yet")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        run(self.repo, "git", "checkout", "-q", "-B", "name", "origin/main")
        config["queue"].update(lanes=["checks"], hookCoveredBy={"checks": "all the hook runs"})
        (self.repo / "engineering.json").write_text(json.dumps(config))
        run(self.repo, "git", "add", "engineering.json")
        run(self.repo, "git", "commit", "-qm", "Name the lanes")
        self.hook("exit 1")
        [identifier] = self.add("name")
        self.assertEqual(self.work(), 1)
        self.assertEqual(self.state(identifier)["state"], "passed")
        self.assertEqual(len(self.runs()), 1)

    def test_the_opt_in_must_name_lanes_the_queue_runs_and_what_they_cover(self):
        config = json.loads((self.repo / "engineering.json").read_text())
        config["lanes"]["other"] = {"command": ["true"]}
        for bad in ({"other": "not a queue lane"}, {"absent": "no such lane"}, {"gate": ""}, {"gate": "  "}, {"gate": 1},
                    {}, ["gate"], "gate"):
            with self.subTest(bad=bad):
                (self.repo / "engineering.json").write_text(json.dumps({**config, "queue": {"hookCoveredBy": bad}}))
                with self.assertRaisesRegex(ValueError, "hookCoveredBy"): self.config()
        for queue in ({"hookCoveredBy": {"gate": "the default lane"}},
                      {"lanes": ["other"], "hookCoveredBy": {"other": "a named lane"}}):
            (self.repo / "engineering.json").write_text(json.dumps({**config, "queue": queue}))
            self.assertEqual(self.config()["queue"], queue)
        # A named list replaces the default gate lane.
        (self.repo / "engineering.json").write_text(json.dumps(
            {**config, "queue": {"lanes": ["other"], "hookCoveredBy": {"gate": "not run"}}}))
        with self.assertRaisesRegex(ValueError, "hookCoveredBy"): self.config()

# What each check saw, a line a run, and it fails while the file its first argument names is in the tree.
CHECK = """import pathlib, subprocess, sys
seen = subprocess.run(["git", "diff", "--name-only", sys.argv[3], "HEAD"], capture_output=True, text=True).stdout.split()
with open(sys.argv[2], "a") as log: log.write(" ".join(seen) + "\\n")
sys.exit(1 if pathlib.Path(sys.argv[1]).exists() else 0)
"""


class CarryOverChecks(Fixture):
    during_gate = MainMoves.during_gate

    def setUp(self):
        super().setUp()
        self.seen = self.base / "seen"
        (self.repo / "check.py").write_text(CHECK)
        run(self.repo, "git", "add", "check.py")
        run(self.repo, "git", "commit", "-qm", "Add a check")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")

    def name_checks(self, *checks):
        """Put queue.carryOverChecks on main, as a project does, and return that commit."""
        config = json.loads((self.repo / "engineering.json").read_text())
        config["queue"]["carryOverChecks"] = list(checks)
        (self.repo / "engineering.json").write_text(json.dumps(config))
        run(self.repo, "git", "add", "engineering.json")
        run(self.repo, "git", "commit", "-qm", "Name the carry-over checks")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        return run(self.repo, "git", "rev-parse", "HEAD")

    def check(self, fails_on="absent", base="origin/main", **more):
        return {"command": ["python3", "check.py", fails_on, str(self.seen), base], **more}

    def runs(self):
        return self.seen.read_text().splitlines() if self.seen.exists() else []

    def test_a_passing_check_carries_on_the_rebased_tree(self):
        self.name_checks(self.check(base="{base}"))
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.during_gate("records/one.json"): self.work()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(self.gates(), 1)
        # {base} reached the check, which then saw main's move and the batch together.
        self.assertEqual(self.runs(), ["a records/one.json"])

    def test_a_check_against_origin_main_does_not_see_the_move(self):
        """Why {base} exists: origin/main is already the new main when a check runs."""
        self.name_checks(self.check())
        self.commit("a", "a")
        self.add("a")
        with self.during_gate("records/one.json"): self.work()
        self.assertEqual(self.runs(), ["a"])

    def test_a_failing_check_gates_again_with_no_blame(self):
        self.name_checks(self.check(fails_on="records/one.json"))
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.during_gate("records/one.json"), patch("builtins.print") as said: self.work()
        self.assertEqual(self.gates(), 2)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertNotIn("why", self.state(identifier))
        self.assertIsNone(mq.Queue(self.repo).held())
        self.assertIn("gating again", " ".join(str(call) for call in said.call_args_list))

    def test_checks_stop_at_the_first_failure(self):
        self.name_checks(self.check(fails_on="records/one.json"), self.check())
        self.commit("a", "a")
        self.add("a")
        with self.during_gate("records/one.json"): self.work()
        self.assertEqual(len(self.runs()), 1)

    def test_a_check_is_not_run_without_a_carry_over(self):
        self.name_checks(self.check())
        self.commit("a", "a")
        self.add("a")
        self.work()
        self.assertEqual(self.runs(), [])
        self.commit("b", "b")
        self.add("b")
        with self.during_gate("code"): self.work()
        self.assertEqual(self.runs(), [])
        self.assertEqual(self.gates(), 3)

    def test_paths_limit_a_check_to_moves_of_them(self):
        self.name_checks(self.check(paths=["records/"]))
        self.commit("a", "a")
        self.add("a")
        with self.during_gate("other.md"): self.work()
        self.assertEqual(self.runs(), [])
        self.assertEqual(self.gates(), 1)

    def test_paths_name_a_directory(self):
        self.name_checks(self.check(paths=["records/"]))
        self.commit("a", "a")
        self.add("a")
        with self.during_gate("records/one.json"): self.work()
        self.assertEqual(len(self.runs()), 1)

    def test_paths_name_a_file(self):
        self.name_checks(self.check(paths=["guide.md"]))
        self.commit("a", "a")
        self.add("a")
        with self.during_gate("guide.md"): self.work()
        self.assertEqual(len(self.runs()), 1)

    def test_a_check_that_cannot_start_gates_again(self):
        self.name_checks({"command": ["/nonexistent/check"]})
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.during_gate("records/one.json"): self.work()
        self.assertEqual(self.gates(), 2)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertIsNone(mq.Queue(self.repo).held())

    def test_a_check_past_its_limit_gates_again(self):
        self.name_checks({"command": ["python3", "hangs.py"]})
        self.commit("a", "a")
        [identifier] = self.add("a")
        real = mq.processes.run
        def bounded(command, **options):
            if "hangs.py" in command: raise mq.processes.TimedOut("past its limit")
            return real(command, **options)
        with self.during_gate("records/one.json"), patch.object(mq.processes, "run", bounded): self.work()
        self.assertEqual(self.gates(), 2)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertIsNone(mq.Queue(self.repo).held())

    def test_a_document_that_fails_the_docs_check_gates_again(self):
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.during_gate("README.md", "[gone](missing.md)\n"): self.work()
        self.assertEqual(self.gates(), 2)
        self.assertEqual(self.state(identifier)["state"], "landed")

    def test_a_document_that_passes_the_docs_check_carries(self):
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.during_gate("README.md", "fine\n"): self.work()
        self.assertEqual(self.gates(), 1)
        self.assertEqual(self.state(identifier)["state"], "landed")

    def test_the_configuration_is_checked(self):
        config = json.loads((self.repo / "engineering.json").read_text())
        good = {"command": ["python3", "x.py"]}
        for checks in ([], [good], [{**good, "paths": ["records/", "guide.md"]}]):
            (self.repo / "engineering.json").write_text(json.dumps({**config, "queue": {"carryOverChecks": checks}}))
            self.assertEqual(self.config()["queue"]["carryOverChecks"], checks)
        for bad in ("python3 x.py", {"command": ["x"]}, ["python3 x.py"], [["python3", "x.py"]], [{}], [{"command": []}],
                    [{"command": "python3 x.py"}], [{"command": ["python3", ""]}], [{"command": ["x"], "other": 1}],
                    [{"command": ["x"], "paths": []}], [{"command": ["x"], "paths": "records/"}],
                    [{"command": ["x"], "paths": [""]}]):
            with self.subTest(bad=bad):
                (self.repo / "engineering.json").write_text(json.dumps({**config, "queue": {"carryOverChecks": bad}}))
                with self.assertRaisesRegex(ValueError, "carryOverChecks"): self.config()

    def test_the_schema_knows_the_setting(self):
        schema = json.loads((ROOT / "schemas/engineering.schema.json").read_text())
        check = schema["properties"]["queue"]["properties"]["carryOverChecks"]["items"]
        self.assertEqual(set(check["properties"]), {"command", "paths"})


class DocsOnly(Fixture):
    def setUp(self):
        super().setUp()
        self.docs_counter, self.docs_status = self.base / "docs-runs", self.base / "docs-status"
        self.ran = self.base / "hook-runs"
        config = json.loads((self.repo / "engineering.json").read_text())
        config["lanes"]["docs"] = {"command": ["python3", "lane.py", str(self.docs_counter), str(self.docs_status)]}
        self.declared = {"paths": {"doc/": "no code lane reads it", "README.md": "nor this"}, "lanes": ["docs"]}
        self.configured = config
        self.publish({**config, "queue": {**config["queue"], "docsOnly": self.declared}}, "Declare the docs")

    def docs_runs(self):
        return len(self.docs_counter.read_text()) if self.docs_counter.exists() else 0

    def test_a_docs_only_batch_runs_only_the_docs_lanes_and_lands_as_one(self):
        self.commit("doc/guide.md", "guide"); self.commit("README.md", "readme"); self.commit("doc/papers.tsv", "tsv")
        ids = self.add("guide", "readme", "tsv")
        said = []
        with patch.object(mq.Worker, "say", lambda worker, words: said.append(words)): self.assertEqual(self.work(), 0)
        self.assertEqual([self.state(i)["state"] for i in ids], ["landed"] * 3)
        self.assertEqual((self.gates(), self.docs_runs()), (0, 1))
        self.assertEqual([self.state(i)["docsOnly"] for i in ids], [["spelling", "docs"]] * 3)
        self.assertLessEqual({"doc", "README.md"}, self.main_files())
        self.assertTrue(any("queue.docsOnly: running only spelling, docs" in words for words in said), said)
        self.assertIn("docs lanes only (spelling, docs): no gate of that commit", mq.describe(self.state(ids[0])))

    def landed_order(self, *ids):
        """The entries' landed commits, oldest on main first."""
        run(self.repo, "git", "fetch", "-q", "origin")
        history = run(self.repo, "git", "rev-list", "--reverse", "origin/main").split()
        return sorted(ids, key=lambda identifier: history.index(self.state(identifier)["landedAs"]))

    def test_a_docs_only_entry_queued_with_code_never_rides_the_code_gate(self):
        """vocaro-engineering#394: queued behind and beside code entries, a docs-only entry lands first, on the docs
        lanes alone, and the code entries gate without it. The old worker gated all three together, every lane."""
        self.commit("code", "code"); self.commit("doc/guide.md", "guide"); self.commit("more", "more")
        code, guide, more = self.add("code", "guide", "more")
        said = []
        with patch.object(mq.Worker, "say", lambda worker, words: said.append(words)): self.assertEqual(self.work(), 0)
        self.assertEqual([self.state(i)["state"] for i in (code, guide, more)], ["landed"] * 3)
        self.assertEqual((self.gates(), self.docs_runs()), (1, 1))
        self.assertEqual(self.state(guide)["docsOnly"], ["spelling", "docs"])
        self.assertNotIn("gatedOn", self.state(guide))
        self.assertIn("docs lanes only (spelling, docs): no gate of that commit", mq.describe(self.state(guide)))
        for identifier in (code, more):
            self.assertNotIn("docsOnly", self.state(identifier))
            self.assertIn("gatedOn", self.state(identifier))
        self.assertEqual(self.landed_order(code, guide, more), [guide, code, more])
        self.assertTrue(any("1 docs-only entry lands first, on the docs lanes alone" in words for words in said), said)

    def test_one_entry_that_changes_docs_and_code_runs_every_lane(self):
        """The control: an entry is docs-only by its own paths, and one that changes code beside a document gates."""
        run(self.repo, "git", "checkout", "-q", "-B", "both", "origin/main")
        (self.repo / "doc").mkdir(exist_ok=True)
        (self.repo / "doc/guide.md").write_text("guide")
        (self.repo / "code").write_text("code")
        run(self.repo, "git", "add", "doc/guide.md", "code")
        run(self.repo, "git", "commit", "-qm", "Both")
        self.commit("README.md", "readme", "readme", start="origin/main")
        both, readme = self.add("both", "readme")
        self.work()
        self.assertEqual([self.state(i)["state"] for i in (both, readme)], ["landed"] * 2)
        self.assertEqual((self.gates(), self.docs_runs()), (1, 1))
        self.assertNotIn("docsOnly", self.state(both))
        self.assertEqual(self.state(readme)["docsOnly"], ["spelling", "docs"])

    def test_a_docs_entry_that_changes_a_path_an_older_entry_changes_waits_for_it(self):
        """Order is kept where two entries change a path in common: the docs entry waits for the older code entry,
        and then lands on the docs lanes in a round of its own, not in the code entry's gate."""
        (self.repo / "doc").mkdir(exist_ok=True)
        (self.repo / "doc/guide.md").write_text("one\ntwo\nthree\nfour\nfive\n")
        run(self.repo, "git", "add", "doc/guide.md")
        run(self.repo, "git", "commit", "-qm", "A guide")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        run(self.repo, "git", "checkout", "-q", "-B", "code", "origin/main")
        (self.repo / "doc/guide.md").write_text("ONE\ntwo\nthree\nfour\nfive\n")
        (self.repo / "code").write_text("code")
        run(self.repo, "git", "add", "doc/guide.md", "code")
        run(self.repo, "git", "commit", "-qm", "Code and its guide")
        run(self.repo, "git", "checkout", "-q", "-B", "guide", "origin/main")
        (self.repo / "doc/guide.md").write_text("one\ntwo\nthree\nfour\nFIVE\n")
        run(self.repo, "git", "commit", "-qam", "The guide alone")
        code, guide = self.add("code", "guide")
        said = []
        with patch.object(mq.Worker, "say", lambda worker, words: said.append(words)): self.work()
        self.assertEqual([self.state(i)["state"] for i in (code, guide)], ["landed"] * 2)
        self.assertEqual((self.gates(), self.docs_runs()), (1, 1))
        self.assertEqual(self.landed_order(code, guide), [code, guide])
        self.assertEqual(self.state(guide)["docsOnly"], ["spelling", "docs"])
        self.assertTrue(any("wait for a round of their own on the docs lanes" in words for words in said), said)

    def test_an_undeclared_project_uses_the_default_docs_paths(self):
        self.publish(self.configured, "Declare nothing")
        self.commit("doc/guide.md", "guide")
        [identifier] = self.add("guide")
        self.work()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual((self.gates(), self.docs_runs()), (0, 1))
        self.assertEqual(self.state(identifier)["docsOnly"], ["spelling", "docs"])

    def test_a_path_beside_a_listed_file_or_directory_runs_every_lane(self):
        for name in ("docs/guide.md", "README.md.orig", "doc"):
            with self.subTest(name=name):
                self.counter.unlink(missing_ok=True)
                self.commit(name, name.replace("/", "-"))
                [identifier] = self.add(name.replace("/", "-"))
                self.work()
                self.assertEqual(self.state(identifier)["state"], "landed")
                self.assertEqual((self.gates(), self.docs_runs()), (1, 0))

    def test_a_file_moved_into_docs_names_the_path_it_left(self):
        self.commit("tool.py", "tool", "print('a tool')\n")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        self.counter.unlink(missing_ok=True)
        run(self.repo, "git", "checkout", "-q", "-B", "move", "origin/main")
        (self.repo / "doc").mkdir(exist_ok=True)
        run(self.repo, "git", "mv", "tool.py", "doc/tool.py")
        run(self.repo, "git", "commit", "-qm", "Move the tool")
        [identifier] = self.add("move")
        self.work()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual((self.gates(), self.docs_runs()), (1, 0))

    def test_a_bisects_gates_look_for_docs_entries_to_land_beside_them(self):
        """A bisect's gates stack nothing, but docs entries that arrive during them still land beside them: the old
        worker ran a bisect's gates with no watch at all, so a docs entry waited out every half."""
        self.commit("bad", "bad"); self.commit("code", "code")
        ids = self.add("bad", "code")
        watched, real = [], mq.Worker.watched
        def watch(worker, runner, config, lanes, base, head, applied, gate, stacks=True):
            watched.append((len(applied), stacks))
            return real(worker, runner, config, lanes, base, head, applied, gate, stacks=stacks)
        with patch.object(mq.Worker, "watched", watch), patch.object(mq, "STACK_POLL", 0.05): self.work()
        self.assertEqual([self.state(i)["state"] for i in ids], ["failed", "landed"])
        # The bisect's round, the older half gating with the newer beside it, ran under the watch, stacking nothing.
        self.assertIn((2, False), watched)

    def test_a_failing_docs_lane_sends_the_batch_back(self):
        self.docs_status.write_text("1")
        self.commit("doc/guide.md", "guide")
        [identifier] = self.add("guide")
        self.work()
        self.assertEqual(self.state(identifier)["state"], "failed")
        self.assertEqual((self.gates(), self.docs_runs()), (0, 1))
        self.assertNotIn("doc", self.main_files())

    def test_a_branch_cannot_declare_its_own_docs(self):
        # Main has no gate lane yet, so the round reads the batch's lanes, but never its docsOnly.
        config = json.loads(json.dumps(self.configured))
        config["lanes"]["checks"] = config["lanes"].pop("gate")
        self.publish(config, "No gate lane yet")
        run(self.repo, "git", "checkout", "-q", "-B", "declare", "origin/main")
        config["queue"].update(lanes=["checks"], docsOnly={"paths": {"engineering.json": "it says so"}, "lanes": ["docs"]})
        (self.repo / "engineering.json").write_text(json.dumps(config))
        run(self.repo, "git", "add", "engineering.json")
        run(self.repo, "git", "commit", "-qm", "Declare the docs here")
        [identifier] = self.add("declare")
        self.work()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual((self.gates(), self.docs_runs()), (1, 0))

    def test_a_hook_the_code_lanes_cover_does_not_run(self):
        # The hook is part of lanes that read none of the batch's paths, so it can say nothing new of them.
        config = json.loads((self.repo / "engineering.json").read_text())
        config["queue"]["hookCoveredBy"] = {"gate": "all the hook runs"}
        self.publish(config, "Opt in")
        PrePushHooks.hook(self, "exit 1")
        self.commit("doc/guide.md", "guide")
        [identifier] = self.add("guide")
        self.work()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual((self.gates(), self.docs_runs(), PrePushHooks.runs(self)), (0, 1, []))

    def test_a_docs_only_hook_no_lane_covers_is_skipped(self):
        PrePushHooks.hook(self, "exit 0")
        self.commit("doc/guide.md", "guide")
        [identifier] = self.add("guide")
        self.work()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(len(PrePushHooks.runs(self)), 0)

    def test_a_docs_only_landing_is_no_gate_eng_release_takes(self):
        from vocaro_engineering import release
        # As a real project does, so that the spelling lane's own directory is no uncommitted change under the gate.
        (self.repo / ".gitignore").write_text(".build/\n")
        run(self.repo, "git", "add", ".gitignore")
        run(self.repo, "git", "commit", "-qm", "Ignore .build")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        self.commit("code", "code")
        [code] = self.add("code")
        self.work()
        checkout = mq.Queue(self.repo).checkout
        # The control: a batch that ran every lane leaves a receipt of the gate at the commit it pushed.
        landed = self.state(code)["landedAs"]
        self.assertRegex(release.gate_passed(checkout, landed), rf"in this checkout|on this Mac \(.*\), in {checkout}")
        # And keeps it by commit, where a session's own checkout reads it (vocaro-engineering#198), as the toolkit
        # that ran it, the fixture's, has no working changes, as a run on the second Mac counts.
        self.assertEqual(release.gate_passed(self.repo, landed), f"on this Mac ({offload_host.mac_name()}), in {checkout}")
        self.commit("doc/guide.md", "guide")
        [docs] = self.add("guide")
        self.work()
        self.assertEqual(self.state(docs)["state"], "landed")
        with self.assertRaisesRegex(release.Stop, "no pass of the whole gate lane"):
            release.gate_passed(checkout, self.state(docs)["landedAs"])

    def test_the_configuration_is_checked(self):
        config = self.configured
        good = {"paths": {"doc/": "why"}, "lanes": ["docs"]}
        for declared in (good, {"paths": {"doc/": "why", "README.md": "why"}, "lanes": ["docs"]}):
            (self.repo / "engineering.json").write_text(json.dumps({**config, "queue": {"docsOnly": declared}}))
            self.assertEqual(self.config()["queue"]["docsOnly"], declared)
        for bad in ({}, {"paths": {"doc/": "why"}}, {"lanes": ["docs"]}, {**good, "other": 1}, {**good, "paths": {}},
                    {**good, "paths": ["doc/"]}, {**good, "paths": {"doc/": ""}}, {**good, "paths": {" ": "why"}},
                    {**good, "lanes": []}, {**good, "lanes": "docs"}, {**good, "lanes": ["docs", "docs"]},
                    {**good, "lanes": ["absent"]}, {**good, "lanes": ["gate"]}, {**good, "lanes": ["docs", "gate"]}, {**good, "exclude": []},
                    {**good, "exclude": {"doc/input.md": ""}}, True, []):
            with self.subTest(bad=bad):
                (self.repo / "engineering.json").write_text(json.dumps({**config, "queue": {"docsOnly": bad}}))
                with self.assertRaisesRegex(ValueError, "docsOnly"): self.config()

    def test_the_schema_knows_the_setting(self):
        schema = json.loads((ROOT / "schemas/engineering.schema.json").read_text())
        declared = schema["properties"]["queue"]["properties"]["docsOnly"]
        self.assertEqual(set(declared["properties"]), {"paths", "lanes", "exclude"})
        self.assertEqual(declared["properties"]["lanes"]["items"]["not"], {"const": "gate"})


class DocsFirst(unittest.TestCase):
    """Which waiting entries go ahead on the docs lanes (mq.docs_first), and what a code batch keeps (mq.without_docs),
    by each entry's own paths (vocaro-engineering#394)."""
    PATHS = {"code": {"src/a.py"}, "guide": {"doc/guide.md"}, "readme": {"README.md"}, "both": {"doc/guide.md", "src/b.py"},
             "shared": {"doc/shared.md"}, "pdf": {"doc/paper.pdf"}}

    def entries(self, *names, **fields):
        return [{"id": name, "state": "queued", **fields.get(name, {})} for name in names]

    def first(self, *names, config=None, **options):
        found = mq.docs_first(self.entries(*names, **options.pop("fields", {})), config or {},
                              lambda entry: self.PATHS[entry["id"]], **options)
        return [entry["id"] for entry in found]

    def test_docs_entries_go_ahead_of_code_that_changes_none_of_their_paths(self):
        self.assertEqual(self.first("code", "guide", "readme"), ["guide", "readme"])

    def test_an_older_entry_that_changes_the_same_path_keeps_its_place(self):
        self.PATHS = {**self.PATHS, "edit": {"doc/guide.md", "src/c.py"}}
        self.assertEqual(self.first("edit", "guide", "readme"), ["readme"])
        # A docs entry held back holds back the later one that changes its path too.
        self.assertEqual(self.first("edit", "guide", "readme", "both"), ["readme"])

    def test_an_entry_put_first_or_landing_ungated_or_testing_stays_in_line(self):
        fields = {"guide": {"front": True}, "readme": {"noGate": True}, "shared": {"state": "testing"}}
        self.assertEqual(self.first("guide", "readme", "shared", fields=fields), [])

    def test_an_entry_waits_for_the_entries_its_docs_lanes_failed_ahead_of(self):
        fields = {"guide": {"waitsFor": ["code"]}}
        self.assertEqual(self.first("code", "guide", fields=fields), [])
        self.assertEqual(self.first("guide", fields=fields), ["guide"])
        # Nor does it go beside the gate of the entry it waits for.
        self.assertEqual(self.first("guide", fields=fields, gating={"code"}), [])

    def test_a_gating_batch_blocks_its_paths_and_a_carry_must_take_every_path(self):
        self.assertEqual(self.first("guide", "readme", blocked={"README.md"}), ["guide"])
        config = {"queue": {"docsOnly": {"paths": {"doc/": "prose and papers"}, "lanes": ["docs"]}}}
        self.assertEqual(self.first("pdf", "guide", config=config), ["pdf", "guide"])
        carried = lambda path: mq.carried_over(path, config, set())
        self.assertEqual(self.first("pdf", "guide", config=config, carried=carried), ["guide"])

    def test_a_code_batch_leaves_out_its_docs_entries_up_to_code_that_changes_their_paths(self):
        changed_by = lambda entry: self.PATHS[entry["id"]]
        kept = lambda *names: [entry["id"] for entry in mq.without_docs(self.entries(*names), {}, changed_by)]
        self.assertEqual(kept("code", "guide", "readme"), ["code"])
        self.assertEqual(kept("guide", "code", "both"), ["code"])
        self.assertEqual(kept("code", "pdf"), ["code", "pdf"])


class DocsCarryPolicy(unittest.TestCase):
    def test_known_inputs_override_carry_permissions_and_false_keeps_explicit_permissions(self):
        excluded = {"queue": {"docsOnly": {"paths": {"docs/": "prose unless excluded"}, "lanes": ["docs"],
                                            "exclude": {"docs/": "code reads these inputs"}},
                              "carryOver": {"docs/": "an older declaration"}}}
        self.assertFalse(mq.carried_over("docs/input.md", excluded, []))
        self.assertTrue(mq.carried_over("README.md", excluded, []))
        disabled = {"queue": {"docsOnly": False, "carryOver": {"records/": "not read by code"}}}
        self.assertFalse(mq.carried_over("README.md", disabled, []))
        self.assertTrue(mq.carried_over("records/input.json", disabled, []))
        self.assertFalse(mq.carried_over("records/input.json", disabled, ["records/input.json"]))


class DefaultDocsOnly(Fixture):
    def test_conventional_markdown_uses_implicit_docs_and_skips_code_and_hook(self):
        self.ran = self.base / "hook-runs"
        PrePushHooks.hook(self, "exit 1")
        self.commit("guides/guide.md", "guide", "# Guide\n")
        [identifier] = self.add("guide")
        self.assertEqual(self.work(), 0)
        state = self.state(identifier)
        self.assertEqual(state["state"], "landed")
        self.assertEqual(state["docsOnly"], ["spelling", "docs"])
        self.assertEqual(self.gates(), 0)
        self.assertNotIn("gatedOn", state)
        self.assertEqual(PrePushHooks.runs(self), [])
        logs = list(mq.Queue(self.repo).logs.glob("*-docs.log"))
        self.assertTrue(logs)
        self.assertIn("Documentation checks passed", logs[-1].read_text())

    def test_a_docs_entry_that_fails_ahead_of_older_code_waits_for_it_and_then_lands(self):
        """A document that links to a file an older code entry adds fails its docs lanes ahead of it: it goes back in
        line behind that entry, rather than being sent back, and then lands on the docs lanes on top of it."""
        self.commit("tool.py", "tool", "print('a tool')\n")
        self.commit("docs/guide.md", "guide", "[the tool](../tool.py)\n")
        tool, guide = self.add("tool", "guide")
        self.work()
        self.assertEqual([self.state(i)["state"] for i in (tool, guide)], ["landed"] * 2)
        self.assertEqual(self.state(guide)["waitsFor"], [tool])
        self.assertEqual(self.state(guide)["docsOnly"], ["spelling", "docs"])
        self.assertEqual(self.gates(), 1)

    def test_broken_links_in_the_default_docs_folder_are_rejected(self):
        self.commit("docs/guide.md", "guide", "[missing](missing.md)\n")
        [identifier] = self.add("guide")
        self.work()
        self.assertEqual(self.state(identifier)["state"], "failed")
        self.assertEqual(self.gates(), 0)
        self.assertNotIn("docs", self.main_files())

    def test_spelling_failure_in_default_docs_is_rejected(self):
        self.commit("README.md", "guide", BRITISH)
        [identifier] = self.add("guide")
        self.work()
        self.assertEqual(self.state(identifier)["state"], "failed")
        self.assertEqual(self.gates(), 0)

    def test_false_disables_defaults_and_keeps_code_gate_and_hook(self):
        config = self.config()
        config["queue"]["docsOnly"] = False
        self.publish(config)
        self.ran = self.base / "hook-runs"
        PrePushHooks.hook(self, "exit 0")
        self.commit("README.md", "guide", "# Guide\n")
        [identifier] = self.add("guide")
        self.work()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertNotIn("docsOnly", self.state(identifier))
        self.assertEqual(self.gates(), 1)
        self.assertEqual(len(PrePushHooks.runs(self)), 1)

    def test_an_explicit_excluded_document_keeps_the_code_gate(self):
        config = self.config()
        config["queue"]["docsOnly"] = {"paths": {"guides/": "prose"}, "lanes": ["docs"],
                                        "exclude": {"guides/input.md": "a test reads this fixture"}}
        self.publish(config)
        self.commit("guides/input.md", "input", "# Input\n")
        [identifier] = self.add("input")
        self.work()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertNotIn("docsOnly", self.state(identifier))
        self.assertEqual(self.gates(), 1)

    def test_default_classification_keeps_code_configuration_and_fixtures_gated(self):
        config = self.config()
        for path in ("README.md", "CONTRIBUTING.md", "CHANGELOG.md", "doc/guide.md", "docs/guide.md",
                     "guides/guide.md", "knowledge/apple/topic.md", "guides/topic.markdown"):
            with self.subTest(path=path):
                self.assertEqual(mq.docs_lanes(config, [path]), ["spelling", "docs"])
                self.assertIsNone(mq.docs_lanes(config, [path, "src/code.py"]))
        for path in ("engineering.json", "guides/config.json", "guides/code.py", "tests/input.md", "src/input.md",
                     "AGENTS.md", "agents/core.md", ".github/workflows/check.yml", "README.md.orig"):
            with self.subTest(path=path): self.assertIsNone(mq.docs_lanes(config, [path]))
        self.assertIsNone(mq.docs_lanes(config, []))
        self.assertEqual(configuration.lane_to_run(config, "docs")["command"], configuration.IMPLICIT_DOCS)

    def test_a_default_docs_file_moved_into_source_keeps_code_gate(self):
        self.commit("README.md", "guide", "# Guide\n")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        run(self.repo, "git", "checkout", "-q", "-B", "move", "origin/main")
        (self.repo / "src").mkdir()
        run(self.repo, "git", "mv", "README.md", "src/input.md")
        run(self.repo, "git", "commit", "-qm", "Move documentation to source")
        [identifier] = self.add("move")
        self.work()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertNotIn("docsOnly", self.state(identifier))
        self.assertEqual(self.gates(), 1)


class ImplicitSpelling(Fixture):
    """The toolkit's spelling check, which the queue runs before the gate in a project with no spelling lane, through
    eng run, as any lane, unless main's queue.spelling is false; a project's own spelling lane runs in its place."""
    RUN = ["python3", "scripts/eng", "run", "spelling"]

    def rounds(self):
        said = []
        with patch.object(mq.Worker, "say", lambda _, words: said.append(words)): self.work()
        return said

    @contextlib.contextmanager
    def lanes_run(self, spelling=None):
        """The commands the worker starts, in a list, each run as it would be, or, for the spelling lane, exiting
        spelling when that is given."""
        started, real = [], mq.processes.run
        def recorded(command, **options):
            started.append(list(command))
            return spelling if spelling is not None and list(command) == self.RUN else real(command, **options)
        with patch.object(mq.processes, "run", recorded): yield started

    def test_the_toolkits_check_is_a_lane_eng_run_admits_and_places(self):
        self.commit("fix", "fix", content="a labeled point\n")
        [identifier] = self.add("fix")
        with self.lanes_run() as started: said = self.rounds()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(started[:2], [self.RUN, ["python3", "scripts/eng", "run", "gate"]])
        self.assertNotIn(mq.IMPLICIT_SPELLING, started)
        # The round says where it ran, as it does of a lane, from the receipt eng run wrote once it was admitted.
        self.assertTrue(any(words.startswith("spelling exited 0 on this Mac (") for words in said), said)

    def test_an_ungated_landing_runs_it_as_a_lane_and_not_the_gate(self):
        self.commit("fix", "fix", content="a labeled point\n")
        with patch.object(mq.Queue, "start"), patch("builtins.print"):
            mq.add(self.repo, "fix", no_gate=True, why="an emergency")
        with self.lanes_run() as started: self.rounds()
        self.assertIn("fix", self.main_files())
        self.assertIn(self.RUN, started)
        self.assertEqual(self.gates(), 0)

    def test_a_docs_only_batch_runs_it_as_a_lane_before_its_own(self):
        config = self.config()
        config["lanes"]["docs"] = {"command": ["python3", "-c", "pass"]}
        config["queue"]["docsOnly"] = {"paths": {"doc/": "no code lane reads it"}, "lanes": ["docs"]}
        self.publish(config, "Declare the docs-only lanes")
        self.commit("doc/a.md", "fix", content="a labeled point\n")
        [identifier] = self.add("fix")
        with self.lanes_run() as started: self.rounds()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(started[:2], [self.RUN, ["python3", "scripts/eng", "run", "docs"]])
        self.assertEqual(self.gates(), 0)

    def test_turned_off_the_queue_asks_eng_run_for_no_spelling_lane(self):
        # Negative control of the test above.
        config = self.config()
        config["queue"]["spelling"] = False
        self.publish(config, "Turn the toolkit's spelling off")
        self.commit("fix", "fix")
        [identifier] = self.add("fix")
        with self.lanes_run() as started: self.rounds()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertNotIn(self.RUN, started)

    def test_no_admission_or_no_measurement_is_no_verdict_and_rejects_nothing(self):
        for status in (mq.resources.BUSY, mq.evidence.UNMEASURED, mq.remote.MISCONFIGURED):
            with self.subTest(status=status):
                self.commit(f"fix{status}", f"fix{status}", content=BRITISH)
                [identifier] = self.add(f"fix{status}", checked=False)
                with self.lanes_run(spelling=status) as started, patch("builtins.print"): self.rounds()
                self.assertEqual(self.state(identifier)["state"], "queued")
                self.assertEqual(self.gates(), 0)
                self.assertNotIn(f"fix{status}", self.main_files())
                self.assertEqual(started.count(self.RUN), mq.NO_VERDICT_RUNS)
                self.assertIn(f"the lanes gave no verdict in {mq.NO_VERDICT_RUNS} runs in a row, the last with exit "
                              f"{status}", mq.Queue(self.repo).held())
                mq.remove(mq.Queue(self.repo), identifier, quiet=True)
                with patch.object(mq.Queue, "start"), patch("builtins.print"): mq.resume(self.repo)

    def test_a_clean_batch_passes_the_toolkits_spelling_and_then_the_gate(self):
        self.commit("fix", "fix", content="a labeled point\n")
        [identifier] = self.add("fix")
        said = self.rounds()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(self.gates(), 1)
        self.assertTrue(any("runs the toolkit's: python3 scripts/eng spelling" in words for words in said), said)
        logs = sorted(path.name for path in mq.Queue(self.repo).logs.glob("*.log"))
        self.assertTrue(any(name.endswith("-spelling.log") for name in logs), logs)

    def test_a_british_spelling_fails_before_the_gate(self):
        self.commit("fix", "fix", content=BRITISH)
        [identifier] = self.add("fix", checked=False)
        with self.lanes_run() as started: self.rounds()
        self.assertEqual(self.state(identifier)["state"], "failed")
        self.assertEqual(self.gates(), 0)
        self.assertNotIn("fix", self.main_files())
        self.assertIn(self.RUN, started)
        self.assertNotIn(mq.IMPLICIT_SPELLING, started)

    def test_turned_off_the_gate_alone_decides(self):
        config = json.loads((self.repo / "engineering.json").read_text())
        config["queue"]["spelling"] = False
        self.publish(config, "Turn the toolkit's spelling off")
        self.commit("fix", "fix", content=BRITISH)
        [identifier] = self.add("fix", checked=False)
        said = self.rounds()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(self.gates(), 1)
        self.assertFalse(any("toolkit's" in words for words in said), said)

    def test_a_projects_own_spelling_lane_runs_in_its_place(self):
        own, own_status = self.base / "own-spelling", self.base / "own-status"
        config = json.loads((self.repo / "engineering.json").read_text())
        config["lanes"]["spelling"] = {"command": ["python3", "lane.py", str(own), str(own_status)]}
        self.publish(config, "Add a spelling lane")
        # The project's lane passes it, so the toolkit's never read it.
        self.commit("fix", "fix", content=BRITISH)
        [identifier] = self.add("fix", checked=False)
        said = self.rounds()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual((len(own.read_text()), self.gates()), (1, 1))
        self.assertFalse(any("toolkit's" in words for words in said), said)

    def test_a_branch_that_drops_the_spelling_lane_gets_the_toolkits(self):
        # Main's lane names spelling; the batch's checkout, which eng run reads, no longer has it.
        own, own_status = self.base / "own-spelling", self.base / "own-status"
        config = json.loads((self.repo / "engineering.json").read_text())
        config["lanes"]["spelling"] = {"command": ["python3", "lane.py", str(own), str(own_status)]}
        self.publish(config, "Add a spelling lane")
        run(self.repo, "git", "checkout", "-q", "-B", "drop", "origin/main")
        del config["lanes"]["spelling"]
        (self.repo / "engineering.json").write_text(json.dumps(config))
        run(self.repo, "git", "commit", "-qam", "Drop the spelling lane")
        [identifier] = self.add("drop")
        said = self.rounds()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertFalse(own.exists())
        self.assertTrue(any("runs the toolkit's" in words for words in said), said)

    def test_queue_spelling_can_only_turn_it_off(self):
        config = json.loads((self.repo / "engineering.json").read_text())
        config["queue"]["spelling"] = False
        (self.repo / "engineering.json").write_text(json.dumps(config))
        self.assertIs(self.config()["queue"]["spelling"], False)
        for bad in (True, "no", 0, None):
            with self.subTest(bad=bad):
                (self.repo / "engineering.json").write_text(json.dumps({**config, "queue": {"spelling": bad}}))
                with self.assertRaisesRegex(ValueError, "queue.spelling can only be false"): self.config()
        schema = json.loads((ROOT / "schemas/engineering.schema.json").read_text())
        self.assertEqual(schema["properties"]["queue"]["properties"]["spelling"]["const"], False)


class NegatedClaims(unittest.TestCase):
    """What landing.negated reads as a closing claim its own clause denies (vocaro-engineering#286)."""
    def test_each_form_of_negation_is_refused(self):
        for message in ("This does not fix #43.", "This doesn't fix #43.", "This doesn’t fix #43.",
                        "This cannot close #43.", "This will never resolve #43.", "This no longer fixes #43.",
                        "Landed without the fix #43.", "NOT fixed #43."):
            with self.subTest(message):
                self.assertEqual(landing.negated(message), [(1, message, "#43")])

    def test_a_few_words_may_stand_between_and_more_may_not(self):
        for message in ("It does not yet fix #43.", "It does not by itself fix #43.", "It does not on its own fix #43."):
            with self.subTest(message):
                self.assertEqual(len(landing.negated(message)), 1)
        self.assertEqual(landing.negated("It does not on any of its paths fix #43."), [])

    def test_a_plain_claim_passes(self):
        for message in ("Closes #43.", "Fixes #43", "resolved: #43", "Add a thing\n\nCloses #43.\nCloses #44."):
            with self.subTest(message):
                self.assertEqual(landing.negated(message), [])

    def test_other_repositories_and_urls_are_named_in_full(self):
        for message, issue in (("This does not fix vocaro/BreezEPUB#43.", "vocaro/BreezEPUB#43"),
                               ("This does not close https://github.com/vocaro/BreezEPUB/issues/43.",
                                "vocaro/BreezEPUB#43")):
            with self.subTest(message):
                self.assertEqual(landing.negated(message), [(1, message, issue)])
        self.assertEqual(landing.negated("Closes vocaro/BreezEPUB#43."), [])
        self.assertEqual(landing.negated("Fixes https://github.com/vocaro/BreezEPUB/issues/43"), [])

    def test_what_is_no_claim_passes(self):
        for message in ("Refs #43.", "This does not change #43.", "This leaves #43 open.", "Does not touch fixes in #43.",
                        "Finished without fixing #43.", "Not ready: see #43.", "Refs vocaro/BreezEPUB#43, not fixed."):
            with self.subTest(message):
                self.assertEqual(landing.negated(message), [])

    def test_a_negation_in_an_earlier_clause_passes(self):
        for message in ("The lane did not start, and fixes #43.", "The lane did not start and fixes #43.",
                        "Do not retry; closes #43.", "Do not retry: closes #43.", "Do not retry. Closes #43.",
                        "Do not retry — closes #43.", "Do not retry - closes #43.",
                        "Stop if it cannot run because that fixes #43.", "This is not slow but fixes #43.",
                        "The run (that did not start) fixes #43."):
            with self.subTest(message):
                self.assertEqual(landing.negated(message), [])

    def test_a_claim_on_its_own_line_passes_after_a_line_that_negates(self):
        for message in ("Do not count a run\n\nCloses #43.", "It does not change the parser\nCloses #43.",
                        "Notes:\n- does not retry\n- fixes #43"):
            with self.subTest(message):
                self.assertEqual(landing.negated(message), [])

    def test_a_negation_wrapped_onto_the_line_before_is_refused(self):
        self.assertEqual(landing.negated("Add a thing\n\nThis does not\nfix #43, and adds more."),
                         [(4, "fix #43, and adds more.", "#43")])

    def test_not_only_asserts_the_claim(self):
        for message in ("This not only fixes #43 but adds a test.", "It does not just fix #43."):
            with self.subTest(message):
                self.assertEqual(landing.negated(message), [])

    def test_each_claim_is_judged_alone(self):
        self.assertEqual([claim[2] for claim in landing.negated("Fixes #1 and does not close #2.")], ["#2"])
        self.assertEqual([claim[2] for claim in landing.negated("Does not fix #1 or close #2.")], ["#1", "#2"])
        self.assertEqual(landing.negated("Closes #1, not #2."), [])

    def test_a_version_number_ends_no_clause(self):
        self.assertEqual(len(landing.negated("This does not in v1.2 fix #43.")), 1)


class ClaimsBeforeQueued(Fixture):
    """eng land and `eng queue add` refuse a commit message that denies a closing claim before anything is queued
    (vocaro-engineering#286)."""
    def worded(self, branch, *messages):
        """A branch of one commit for each message, on origin/main."""
        for index, message in enumerate(messages):
            self.commit(f"{branch}-{index}", branch, start="origin/main" if index == 0 else branch)
            run(self.repo, "git", "commit", "-q", "--amend", "-m", message)
        return run(self.repo, "git", "rev-parse", branch)

    def land(self, branch, **options):
        """(the new entry's ID or None, what eng land said, the error it refused with or None), no worker started."""
        said, error, identifier = io.StringIO(), None, None
        with patch.object(mq.Queue, "start"), contextlib.redirect_stdout(said), contextlib.redirect_stderr(said):
            try: identifier = mq.add(self.repo, branch, **options)
            except mq.Refused as refused: error = str(refused)
        return identifier, said.getvalue(), error

    def test_a_negated_claim_is_refused_naming_the_commit_and_line_and_nothing_is_queued(self):
        tip = self.worded("fix", "Add a thing\n\nSome context.\nThis does not fix #43.")
        with patch.object(mq, "check") as trial, patch.object(mq, "checked_before_running") as unittest_checks:
            identifier, _, error = self.land("fix")
        self.assertIsNone(identifier)
        self.assertEqual(mq.Queue(self.repo).all(), [])
        self.assertEqual(run(self.repo, "git", "for-each-ref", "refs/eng-queue"), "")
        trial.assert_not_called()
        unittest_checks.assert_not_called()
        self.assertIn(f"{tip[:12]} is not queued", error)
        self.assertIn(f"{tip[:12]} line 4 closes #43: This does not fix #43.", error)
        self.assertIn('"Refs #43"', error)
        self.assertIn('"leaves #43 open"', error)
        self.assertIn("Checked before it is queued", error)

    def test_a_plain_claim_is_queued(self):
        # The positive control.
        self.worded("fix", "Add a thing\n\nThe lane did not start, and this is the repair.\n\nCloses #43.")
        identifier, _, error = self.land("fix")
        self.assertIsNone(error)
        self.assertEqual(self.state(identifier)["state"], "queued")

    def test_any_commit_of_the_branch_is_read_and_each_finding_named(self):
        self.worded("two", "Add a thing, without fix vocaro/BreezEPUB#7.", "Add another\n\nCloses #8.",
                    "Add a third that won't close #9.")
        _, _, error = self.land("two")
        self.assertIn("line 1 closes vocaro/BreezEPUB#7:", error)
        self.assertIn("line 1 closes #9:", error)
        self.assertNotIn("closes #8", error)

    def test_a_message_main_already_holds_is_not_read(self):
        self.worded("old", "This does not fix #43.")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        self.worded("fix", "Add a thing")
        identifier, _, error = self.land("fix")
        self.assertIsNone(error)
        self.assertEqual(self.state(identifier)["state"], "queued")

    def test_the_emergency_options_do_not_skip_it(self):
        self.worded("fix", "This does not fix #43.")
        for options in ({"front": True}, {"no_gate": True, "why": "urgent"}):
            with self.subTest(options):
                identifier, _, error = self.land("fix", **options)
                self.assertIsNone(identifier)
                self.assertIn("is not queued", error)
        self.assertEqual(mq.Queue(self.repo).all(), [])

    def test_a_branch_refreshed_by_a_merge_is_read_too(self):
        old = run(self.repo, "git", "rev-parse", "origin/main")
        self.commit("other", "other")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        self.commit("mine", "fix", start=old)
        run(self.repo, "git", "commit", "-q", "--amend", "-m", "This does not fix #43.")
        run(self.repo, "git", "merge", "-q", "--no-edit", "origin/main")
        identifier, _, error = self.land("fix")
        self.assertIsNone(identifier)
        self.assertIn("closes #43: This does not fix #43.", error)

    def test_messages_it_cannot_read_are_refused(self):
        self.worded("fix", "Add a thing")
        with patch.object(mq.landing, "negated_claims", return_value=None):
            identifier, _, error = self.land("fix")
        self.assertIsNone(identifier)
        self.assertIn("commit messages could not be read", error)


class ImplicitSpellingLane(Fixture):
    """`eng run spelling` in a project with no spelling lane runs the toolkit's check as a lane: offered to the second
    Mac and admitted like any other (owner's decision, 2026-11-09, vocaro-engineering#305)."""
    OFFERED = SimpleNamespace(status=0)

    def eng_run(self, *words):
        return cli.main(["--project", str(self.repo), "run", *words])

    def test_it_is_offered_to_the_second_mac_as_any_lane_is(self):
        with patch.object(cli.remote, "first", return_value=self.OFFERED) as first:
            self.assertEqual(self.eng_run("spelling"), 0)
        config, lane = first.call_args.args[1:3]
        self.assertEqual(lane, "spelling")
        self.assertEqual(config["lanes"]["spelling"], {"command": mq.IMPLICIT_SPELLING, "timeout": mq.SPELLING_LIMIT})
        self.assertIsNone(configuration.stays(config["lanes"], "spelling"))

    def test_a_projects_own_lane_keeps_its_command(self):
        # Negative control.
        config = self.config()
        config["lanes"]["spelling"] = {"command": ["python3", "lane.py"]}
        self.publish(config, "Add a spelling lane")
        with patch.object(cli.remote, "first", return_value=self.OFFERED) as first:
            self.assertEqual(self.eng_run("spelling"), 0)
        self.assertEqual(first.call_args.args[1]["lanes"]["spelling"], {"command": ["python3", "lane.py"]})

    def test_it_waits_for_admission_and_no_admission_runs_nothing(self):
        with patch.object(cli, "refresh_policy", return_value="policy"), \
                patch.object(cli, "admitted_here", return_value=mq.resources.BUSY) as admitted, \
                patch.object(cli.processes, "run", side_effect=AssertionError("ran with no admission")):
            self.assertEqual(self.eng_run("--here", "spelling"), mq.resources.BUSY)
        self.assertEqual(admitted.call_args.args[1:3], ("spelling", "fixture spelling"))

    def test_queue_spelling_says_only_whether_the_queue_asks(self):
        # A batch's engineering.json that turns it off must not leave the round's `eng run spelling` with no lane.
        config = self.config()
        config["queue"]["spelling"] = False
        self.publish(config, "Turn the toolkit's spelling off")
        with patch.object(cli.remote, "first", return_value=self.OFFERED) as first:
            self.assertEqual(self.eng_run("spelling"), 0)
        self.assertEqual(first.call_args.args[2], "spelling")

    def test_any_other_lane_a_project_lacks_is_still_refused(self):
        with patch.object(cli.remote, "first", side_effect=AssertionError("offered a lane that does not exist")), \
                contextlib.redirect_stderr(io.StringIO()) as said:
            self.assertEqual(self.eng_run("nonesuch"), 2)
        self.assertIn("No configured lane: nonesuch", said.getvalue())


class SpellingStoppedByRemoval(Fixture):
    """Withdraw an entry after its spelling command has actually acquired admission (#301)."""

    def setUp(self):
        super().setUp()
        self.ready, self.release = self.base / "ready", self.base / "release"
        self.stopped = self.base / "stopped"
        self.first_done, self.next_done = self.base / "first-done", self.base / "next-done"
        config = self.config()
        command = [sys.executable, "-c", """
import os, pathlib, signal, sys, time

ready, release, stopped, first_done, next_done = map(pathlib.Path, sys.argv[1:])
assert os.environ["VOCARO_RESOURCE_TOKEN"]

def stop(signum, frame):
    stopped.touch()
    sys.exit(128 + signum)

if pathlib.Path("hold-spelling").exists():
    signal.signal(signal.SIGTERM, stop)
    ready.touch()
    while not release.exists():
        time.sleep(0.02)
    first_done.touch()
else:
    next_done.touch()
""", *map(str, (self.ready, self.release, self.stopped, self.first_done, self.next_done))]
        config["lanes"]["spelling"] = {"command": command, "timeout": int(stretched(45))}
        self.publish(config, "Make spelling wait in a chosen batch")
        self.guard = mq.resources.guard_path(os.environ)

    def remove_during_spelling(self, testing, queued_branch=None):
        look, removed, polls = mq.Removal.look, [], 0

        def after_ready(removal):
            nonlocal polls
            if not self.ready.exists() or testing not in removal.ids:
                return look(removal)
            if not removed:
                self.assertTrue(mq.resources.guard_owners(os.environ), "spelling has no admission")
                self.assertEqual(self.state(testing)["state"], mq.TESTING)
                identifier = self.add(queued_branch)[0] if queued_branch else testing
                if queued_branch:
                    self.assertNotIn(identifier, removal.ids)
                    self.assertEqual(self.state(identifier)["state"], mq.QUEUED)
                mq.remove(mq.Queue(self.repo), identifier)
                removed.append(identifier)
            gone = look(removal)
            if queued_branch and not self.release.exists():
                self.assertFalse(gone)
                self.assertFalse(self.stopped.exists())
                polls += 1
                if polls == 3:
                    self.release.touch()
            return gone

        with patch.object(mq.Removal, "look", after_ready), patch.object(mq, "REMOVAL_POLL", 0.1), \
                patch.object(mq.Worker, "stack"), contextlib.redirect_stdout(io.StringIO()) as said:
            self.assertEqual(self.work(), 0, said.getvalue())
        log = said.getvalue()
        self.assertEqual(len(removed), 1, log)
        self.assertEqual(self.state(removed[0])["state"], mq.REMOVED, log)
        self.assertFalse(self.guard.exists(), "spelling left an admission guard")
        self.assertIsNone(mq.Queue(self.repo).held())
        self.assertEqual(self.gates(), 1, log)
        return removed[0], log

    def test_removing_a_testing_entry_stops_admitted_spelling_and_the_survivor_lands(self):
        self.commit("hold-spelling", "withdrawn")
        self.commit("survives", "survivor")
        withdrawn, survivor = self.add("withdrawn", "survivor")
        _, log = self.remove_during_spelling(withdrawn)
        self.assertTrue(self.stopped.exists(), log)
        self.assertFalse(self.first_done.exists(), log)
        self.assertTrue(self.next_done.exists(), log)
        self.assertRegex(log, f"spelling: stopped[^\\n]*, as {withdrawn} left the queue")
        self.assertIn(f"{withdrawn} left the queue, which stopped the gate: gating what is left", log)
        self.assertNotIn("no verdict", log)
        self.assertEqual(self.state(survivor)["state"], mq.LANDED, log)
        self.assertNotIn("hold-spelling", self.main_files())
        self.assertIn("survives", self.main_files())

    def test_removing_an_entry_outside_the_batch_leaves_admitted_spelling_running(self):
        self.commit("hold-spelling", "testing")
        self.commit("later", "queued")
        [testing] = self.add("testing")
        removed, log = self.remove_during_spelling(testing, queued_branch="queued")
        self.assertTrue(self.first_done.exists(), log)
        self.assertFalse(self.stopped.exists(), log)
        self.assertFalse(self.next_done.exists(), log)
        self.assertIn(f"removed {removed}", log)
        self.assertNotIn("spelling: stopped", log)
        self.assertEqual(self.state(testing)["state"], mq.LANDED, log)


class SpelledBeforeQueued(Fixture):
    """eng land and `eng queue add` run the spelling lane the worker would run before the gate, the project's own or
    the toolkit's, on the entry's commits replayed onto origin/main, and refuse an entry it finds a British spelling in
    (vocaro-engineering#283)."""
    def setUp(self):
        super().setUp()
        real = patch.object(mq, "spelled_before_queued", SPELLED_BEFORE_QUEUED)
        real.start()
        self.addCleanup(real.stop)

    def land(self, branch):
        """(the new entry's ID or None, what eng land said, the error it refused with or None), no worker started."""
        said, error = io.StringIO(), None
        with patch.object(mq.Queue, "start"), contextlib.redirect_stdout(said), contextlib.redirect_stderr(said):
            try: mq.add(self.repo, branch)
            except mq.Refused as refused: error = str(refused)
        identifier = next((e["id"] for e in reversed(mq.Queue(self.repo).all())
                           if e["branch"] == branch and e["state"] in mq.WAITING), None)
        return identifier, said.getvalue(), error

    def own_lane(self, code):
        """Give main a spelling lane of its own that prints a line and exits code on a tree holding a file named fix,
        and 0 on any other."""
        config = self.config()
        config["lanes"]["spelling"] = {"command": [
            "python3", "-c", f"import os, sys; print('own lane said it'); sys.exit({code} if os.path.exists('fix') else 0)"]}
        self.publish(config, "Add a spelling lane")
        return config

    def test_a_british_spelling_is_refused_with_its_findings_and_nothing_is_queued(self):
        self.commit("fix", "fix", content=BRITISH)
        identifier, _, error = self.land("fix")
        self.assertIsNone(identifier)
        self.assertEqual(mq.Queue(self.repo).all(), [])
        self.assertEqual(run(self.repo, "git", "for-each-ref", "refs/eng-queue"), "")
        self.assertIn("is not queued: python3 scripts/eng spelling fails on it replayed onto origin/main", error)
        self.assertIn("fix:1: British spelling", error)
        self.assertIn("Checked before it is queued", error)

    def test_a_clean_entry_is_queued_and_says_it_was_checked(self):
        # The positive control.
        self.commit("fix", "fix", content="a labeled point\n")
        identifier, said, error = self.land("fix")
        self.assertIsNone(error)
        self.assertEqual(self.state(identifier)["state"], "queued")
        self.assertIn("spelling checked in", said)
        self.assertIn("python3 scripts/eng spelling passes on it", said)

    def test_an_entry_behind_main_is_checked_replayed_onto_it(self):
        old = run(self.repo, "git", "rev-parse", "origin/main")
        self.commit("other", "other")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        self.commit("fix", "fix", content=BRITISH, start=old)
        main = run(self.repo, "git", "rev-parse", "origin/main")
        identifier, _, error = self.land("fix")
        self.assertIsNone(identifier)
        self.assertIn(f"replayed onto origin/main at {main[:12]}", error)

    def test_turned_off_nothing_is_checked(self):
        config = self.config()
        config["queue"]["spelling"] = False
        self.publish(config, "Turn the toolkit's spelling off")
        self.commit("fix", "fix", content=BRITISH)
        identifier, said, error = self.land("fix")
        self.assertIsNone(error)
        self.assertEqual(self.state(identifier)["state"], "queued")
        self.assertNotIn("spelling", said)

    def test_a_projects_own_lane_runs_in_place_of_the_toolkits(self):
        # Its lane passes, so the toolkit's never read the British spelling.
        self.own_lane(0)
        self.commit("fix", "fix", content=BRITISH)
        identifier, said, error = self.land("fix")
        self.assertIsNone(error)
        self.assertEqual(self.state(identifier)["state"], "queued")
        self.assertIn("passes on it", said)
        self.assertNotIn("scripts/eng spelling", said)

    def test_a_finding_of_a_projects_own_lane_refuses_the_entry(self):
        self.own_lane(1)
        self.commit("fix", "fix")
        identifier, _, error = self.land("fix")
        self.assertIsNone(identifier)
        self.assertIn("fails on it", error)
        self.assertIn("own lane said it", error)

    def test_a_branch_that_drops_the_spelling_lane_gets_the_toolkits(self):
        # Main's lane passes everything; the entry's commit, which the worker's eng run reads, has no lane.
        config = self.own_lane(0)
        run(self.repo, "git", "checkout", "-q", "-B", "drop", "origin/main")
        del config["lanes"]["spelling"]
        (self.repo / "engineering.json").write_text(json.dumps(config))
        (self.repo / "fix").write_text(BRITISH)
        run(self.repo, "git", "add", "engineering.json", "fix")
        run(self.repo, "git", "commit", "-qm", "Drop the spelling lane")
        identifier, _, error = self.land("drop")
        self.assertIsNone(identifier)
        self.assertIn("python3 scripts/eng spelling fails on it", error)

    def test_a_lane_that_fails_with_no_finding_queues_the_entry(self):
        self.own_lane(2)
        self.commit("fix", "fix")
        identifier, said, error = self.land("fix")
        self.assertIsNone(error)
        self.assertEqual(self.state(identifier)["state"], "queued")
        self.assertIn("exited 2 on it, which is no finding, so the round runs the spelling lane", said)

    def test_a_failure_main_already_has_is_not_the_entrys(self):
        # main took a British spelling past the queue: an entry that adds a clean file is queued, and says why.
        run(self.repo, "git", "checkout", "-q", "main")
        (self.repo / "old").write_text(BRITISH)
        run(self.repo, "git", "add", "old")
        run(self.repo, "git", "commit", "-qm", "Past the queue")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        self.commit("other", "other")
        identifier, said, error = self.land("other")
        self.assertIsNone(error)
        self.assertEqual(self.state(identifier)["state"], "queued")
        self.assertIn("fails on origin/main at", said)

    def test_a_run_past_its_limit_queues_the_entry(self):
        self.commit("fix", "fix", content=BRITISH)
        with patch.object(mq.processes, "run", side_effect=mq.processes.TimedOut("300 s")):
            identifier, said, error = self.land("fix")
        self.assertIsNone(error)
        self.assertEqual(self.state(identifier)["state"], "queued")
        self.assertIn("could not check its spelling, so the round runs the spelling lane", said)


class AheadOfTheQueue(Fixture):
    """eng land's emergency options --front and --no-gate, in every repository with no setting (owner's decision,
    2026-11-04, vocaro-engineering#163); --no-gate needs a written reason."""
    WHY = "a flaky voice test unrelated to the fix fails the gate"

    def setUp(self):
        super().setUp()
        self.light_counter, self.light_status = self.base / "spelling-runs", self.base / "spelling-status"
        config = json.loads((self.repo / "engineering.json").read_text())
        config["lanes"]["spelling"] = {"command": ["python3", "lane.py", str(self.light_counter), str(self.light_status)]}
        self.configured = config
        self.publish(config, "Add a spelling lane")
        self.counter.unlink(missing_ok=True)
        self.light_counter.unlink(missing_ok=True)

    def light(self):
        return len(self.light_counter.read_text()) if self.light_counter.exists() else 0

    def add_with(self, branch, **options):
        with patch.object(mq.Queue, "start"), patch("builtins.print"): return mq.add(self.repo, branch, **options)

    def ungated(self, branch):
        return self.add_with(branch, no_gate=True, why=self.WHY)

    def rounds(self, count=1):
        """count rounds of one worker, and what it said."""
        said, worker = [], mq.Worker(mq.Queue(self.repo))
        with patch.object(mq.Worker, "say", lambda _, words: said.append(words)):
            for _ in range(count): worker.round()
        return worker, said

    def tip_message(self):
        run(self.repo, "git", "fetch", "-q", "origin")
        return run(self.repo, "git", "log", "-1", "--format=%B", "origin/main")

    def queue_status(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out): mq.status(self.repo)
        return out.getvalue()

    def test_no_gate_needs_a_written_reason(self):
        self.commit("fix", "fix")
        for why in (None, "", "  "):
            with self.subTest(why=why), self.assertRaisesRegex(ValueError, "--no-gate needs a written reason"):
                self.add_with("fix", no_gate=True, why=why)
        with self.assertRaisesRegex(ValueError, "--why is the reason for --front or --no-gate"):
            self.add_with("fix", why=self.WHY)
        self.assertEqual(mq.Queue(self.repo).all(), [])
        # The control: with a reason it is queued, and the reason is kept on one line.
        identifier = self.add_with("fix", no_gate=True, why="a flaky test\n  fails the gate")
        self.assertEqual((self.state(identifier)["noGate"], self.state(identifier)["aheadWhy"]),
                         (True, "a flaky test fails the gate"))

    def test_both_options_work_with_no_setting_and_main_cannot_declare_one(self):
        # The fixture's main declares neither, and front needs no reason.
        self.commit("a", "a")
        identifier = self.add_with("a", front=True)
        self.assertEqual(self.state(identifier)["front"], True)
        self.assertNotIn("aheadWhy", self.state(identifier))
        for name in ("front", "noGate"):
            with self.subTest(name=name):
                (self.repo / "engineering.json").write_text(json.dumps(
                    {**self.configured, "queue": {name: {"why": "a setting that no longer exists"}}}))
                with self.assertRaisesRegex(ValueError, "queue supports only"): self.config()
        schema = json.loads((ROOT / "schemas/engineering.schema.json").read_text())["properties"]["queue"]["properties"]
        self.assertFalse({"front", "noGate"} & set(schema))

    def test_a_front_entry_is_tested_next_as_a_batch_of_its_own(self):
        self.commit("a", "a"); self.commit("b", "b"); self.commit("c", "c")
        a, b = self.add("a", "b")
        c = self.add_with("c", front=True, why="an important toolkit fix")
        _, said = self.rounds()
        self.assertEqual([self.state(i)["state"] for i in (a, b, c)], ["queued", "queued", "landed"])
        self.assertEqual(self.gates(), 1)
        self.assertTrue(any("at the front of the queue" in words for words in said), said)
        self.assertNotIn("ungated", self.state(c))
        # Those already queued are tested with the fix, without being queued again.
        self.rounds()
        self.assertEqual([self.state(i)["state"] for i in (a, b)], ["landed"] * 2)
        self.assertEqual(self.gates(), 2)
        log = run(self.repo, "git", "log", "--format=%s", "origin/main").splitlines()
        self.assertEqual(log[:3], ["Add b", "Add a", "Add c"])

    def test_without_front_the_same_entries_share_one_gate_in_order(self):
        self.commit("a", "a"); self.commit("b", "b"); self.commit("c", "c")
        ids = self.add("a", "b", "c")
        self.rounds()
        self.assertEqual([self.state(i)["state"] for i in ids], ["landed"] * 3)
        self.assertEqual(self.gates(), 1)
        self.assertEqual(run(self.repo, "git", "log", "--format=%s", "origin/main").splitlines()[:3],
                         ["Add c", "Add b", "Add a"])

    def test_front_again_moves_an_entry_already_queued(self):
        self.commit("a", "a"); self.commit("b", "b")
        a, b = self.add("a", "b")
        self.assertEqual(self.add_with("b", front=True, why="it unblocks the queue"), b)
        described = mq.describe(self.state(b))
        self.assertIn("is tested next", described)
        self.assertIn("because it unblocks the queue", described)
        self.rounds()
        self.assertEqual([self.state(i)["state"] for i in (a, b)], ["queued", "landed"])

    def test_a_front_entry_leaves_a_bisect_where_it_was(self):
        self.commit("a", "a"); self.commit("bad", "bad")
        self.add("a")
        identifier = self.add_with("bad", front=True)
        worker = mq.Worker(mq.Queue(self.repo))
        worker.limit, worker.suspects = 1, 2
        with patch.object(mq.Worker, "say"): worker.round()
        self.assertEqual(self.state(identifier)["state"], "failed")
        self.assertEqual((worker.limit, worker.suspects), (1, 2))

    def test_no_gate_lands_on_the_light_lanes_and_records_its_reason(self):
        tip = self.commit("fix", "fix")
        identifier = self.ungated("fix")
        _, said = self.rounds()
        entry = self.state(identifier)
        self.assertEqual(entry["state"], "landed")
        self.assertEqual((self.gates(), self.light()), (0, 1))
        self.assertEqual(entry["ungated"], {"why": self.WHY, "lanes": ["spelling"]})
        self.assertNotIn("gatedOn", entry)
        self.assertTrue(any("lands without the gate" in words and self.WHY in words
                            and "the next gate on main covers it" in words for words in said), said)
        # Main's history says so, with the reason, and the commit keeps its message, author and tree.
        message = self.tip_message()
        self.assertTrue(message.startswith("Add fix"), message)
        self.assertIn(f"{mq.UNGATED}: eng land --no-gate ran spelling", message)
        self.assertIn(f"because {self.WHY}", message)
        self.assertEqual(run(self.repo, "git", "log", "-1", "--format=%an %T", "origin/main"),
                         run(self.repo, "git", "log", "-1", "--format=%an %T", tip))
        self.assertIn(f"landed without the gate, on spelling, because {self.WHY}", mq.describe(entry))
        # The UNGATED amend keeps the author, date and subject by which the entry finds its own commit (#184).
        self.assertEqual(entry["landedAs"], run(self.repo, "git", "rev-parse", "origin/main"))
        self.assertEqual(entry["landedCommits"], 1)
        self.assertIn(f"ungated on main: {identifier}", self.queue_status())
        # A later gate on main covers it, and the status says no more of it.
        self.commit("later", "later")
        later = self.add_with("later")
        self.rounds()
        self.assertEqual(self.state(later)["state"], "landed")
        self.assertEqual(self.gates(), 1)
        self.assertNotIn(mq.UNGATED, self.tip_message())
        self.assertNotIn("ungated on main", self.queue_status())

    def test_a_project_without_a_spelling_lane_lands_ungated_on_the_toolkits(self):
        config = json.loads(json.dumps(self.configured))
        del config["lanes"]["spelling"]
        run(self.repo, "git", "checkout", "-q", "--detach", "origin/main")
        self.publish(config, "No spelling lane")
        self.counter.unlink(missing_ok=True)
        self.commit("fix", "fix")
        identifier = self.ungated("fix")
        _, said = self.rounds()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual((self.gates(), self.light()), (0, 0))
        self.assertEqual(self.state(identifier)["ungated"]["lanes"], ["spelling"])
        self.assertTrue(any("runs the toolkit's: python3 scripts/eng spelling" in words for words in said), said)

    def test_a_project_without_a_spelling_lane_that_turned_the_toolkits_off_runs_no_lane(self):
        config = json.loads(json.dumps(self.configured))
        del config["lanes"]["spelling"]
        config["queue"]["spelling"] = False
        run(self.repo, "git", "checkout", "-q", "--detach", "origin/main")
        self.publish(config, "No spelling lane")
        self.counter.unlink(missing_ok=True)
        self.commit("fix", "fix")
        identifier = self.ungated("fix")
        _, said = self.rounds()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual((self.gates(), self.light()), (0, 0))
        self.assertEqual(self.state(identifier)["ungated"]["lanes"], [])
        self.assertFalse(any("toolkit's" in words for words in said), said)

    def test_an_ungated_landing_fails_on_the_toolkits_spelling(self):
        config = json.loads(json.dumps(self.configured))
        del config["lanes"]["spelling"]
        run(self.repo, "git", "checkout", "-q", "--detach", "origin/main")
        self.publish(config, "No spelling lane")
        self.counter.unlink(missing_ok=True)
        self.commit("fix", "fix", content=BRITISH)
        identifier = self.ungated("fix")
        self.rounds()
        self.assertEqual(self.state(identifier)["state"], "failed")
        self.assertEqual(self.gates(), 0)
        self.assertNotIn("fix", self.main_files())

    def test_a_docs_only_landing_does_not_cover_an_ungated_one(self):
        config = json.loads(json.dumps(self.configured))
        config["queue"]["docsOnly"] = {"paths": {"doc/": "no code lane reads it"}, "lanes": ["spelling"]}
        run(self.repo, "git", "checkout", "-q", "--detach", "origin/main")
        self.publish(config, "Declare the docs")
        self.commit("fix", "fix")
        identifier = self.ungated("fix")
        self.rounds()
        self.commit("doc/guide.md", "guide")
        self.add("guide")
        self.rounds()
        self.assertEqual(self.gates(), 0)
        self.assertIn(f"ungated on main: {identifier}", self.queue_status())

    def test_an_ungated_entry_waits_its_turn_and_lands_alone(self):
        self.commit("a", "a"); self.commit("b", "b"); self.commit("c", "c")
        [a] = self.add("a")
        b = self.ungated("b")
        [c] = self.add("c")
        self.rounds()
        self.assertEqual([self.state(i)["state"] for i in (a, b, c)], ["landed", "queued", "queued"])
        self.assertEqual(self.gates(), 1)
        self.rounds()
        self.assertEqual([self.state(i)["state"] for i in (b, c)], ["landed", "queued"])
        self.assertEqual(self.gates(), 1)
        self.assertIn("ungated", self.state(b))
        self.rounds()
        self.assertEqual(self.state(c)["state"], "landed")
        self.assertEqual(self.gates(), 2)

    def test_front_and_no_gate_together_land_at_once(self):
        self.commit("a", "a"); self.commit("fix", "fix")
        [a] = self.add("a")
        fix = self.add_with("fix", front=True, no_gate=True, why=self.WHY)
        self.rounds()
        self.assertEqual([self.state(i)["state"] for i in (a, fix)], ["queued", "landed"])
        self.assertEqual((self.gates(), self.light()), (0, 1))
        self.assertIn(mq.UNGATED, self.tip_message())

    def test_a_light_lane_that_fails_sends_it_back(self):
        self.light_status.write_text("1")
        self.commit("fix", "fix")
        identifier = self.ungated("fix")
        self.rounds()
        self.assertEqual(self.state(identifier)["state"], "failed")
        self.assertNotIn("fix", self.main_files())

    def test_the_pre_push_hook_still_runs_and_its_refusal_holds(self):
        self.ran = self.base / "hook-runs"
        PrePushHooks.hook(self, "exit 1")
        self.commit("fix", "fix")
        identifier = self.ungated("fix")
        with contextlib.redirect_stdout(io.StringIO()): self.work()
        self.assertEqual(len(PrePushHooks.runs(self)), 1)
        self.assertIn("pre-push hook refused", mq.Queue(self.repo).held())
        self.assertNotIn("fix", self.main_files())
        self.assertEqual(self.state(identifier)["state"], "passed")

    def test_the_options_reach_eng_land_and_eng_queue_add(self):
        import argparse
        for adder, words in ((mq.land_arguments, []), (mq.add_arguments, ["add"])):
            parser = argparse.ArgumentParser()
            adder(parser)
            asked = parser.parse_args([*words, "--front", "--no-gate", "--why", "a reason"])
            plain = parser.parse_args(words)
            self.assertEqual((asked.front, asked.no_gate, asked.why, plain.front, plain.no_gate, plain.why),
                             (True, True, "a reason", False, False, None))


class LaggingCheckout(Fixture):
    """The main checkout's engineering.json is behind origin/main, as Pagepour's was (#96)."""
    def session(self, lagging, published=None):
        """Leave the main checkout on a commit with the configuration lagging, and in a session's worktree
        on branch fix a commit on top of origin/main, which holds the configuration published."""
        current = json.loads((self.repo / "engineering.json").read_text())
        self.publish(lagging, "An older configuration")
        worktree = self.base / "session"
        run(self.repo, "git", "worktree", "add", "-q", "-b", "fix", str(worktree))
        (worktree / "engineering.json").write_text(json.dumps(published or current))
        run(worktree, "git", "add", "engineering.json")
        run(worktree, "git", "commit", "-q", "--allow-empty", "-m", "The newer configuration")
        run(worktree, "git", "push", "-q", "origin", "HEAD:main")
        (worktree / "fix").write_text("x")
        run(worktree, "git", "add", "fix")
        run(worktree, "git", "commit", "-qm", "Add fix")
        return worktree

    def land_from_another_project(self):
        other = self.base / "dependent"
        other.mkdir()
        (other / "engineering.json").write_text(json.dumps({"schemaVersion": 1, "name": "dependent"}))
        run(other, "git", "init", "-q")
        (other / "scripts").mkdir()
        shutil.copy(ROOT / "scripts/eng", other / "scripts/eng")
        return subprocess.run(["python3", "scripts/eng", "land", "--repo", "project", "fix", "--wait",
                               "--limit", str(stretched(120))], cwd=other, capture_output=True, text=True)

    def test_a_gate_lane_only_origin_main_has_lets_the_commit_in(self):
        self.session({"schemaVersion": 1, "name": "fixture"})
        done = self.land_from_another_project()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("fix", self.main_files())
        self.assertEqual(self.gates(), 1)

    def test_no_gate_lane_on_origin_main_is_refused_whatever_the_checkout_says(self):
        """The negative control: the checkout has a gate lane, and origin/main and the commit have none."""
        current = json.loads((self.repo / "engineering.json").read_text())
        self.session(current, {"schemaVersion": 1, "name": "fixture"})
        done = self.land_from_another_project()
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("nothing to run", done.stderr)
        self.assertEqual(mq.Queue(self.repo).all(), [])
        self.assertNotIn("fix", self.main_files())

    def test_no_gate_lane_anywhere_is_refused(self):
        lanes = {"schemaVersion": 1, "name": "fixture"}
        self.session(lanes, lanes)
        done = self.land_from_another_project()
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("nothing to run", done.stderr)
        self.assertEqual(mq.Queue(self.repo).all(), [])

    def test_pull_request_on_origin_main_is_refused_whatever_the_checkout_says(self):
        current = json.loads((self.repo / "engineering.json").read_text())
        self.session(current, {**current, "integration": "pull-request"})
        done = self.land_from_another_project()
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("pull request", done.stderr)
        self.assertEqual(mq.Queue(self.repo).all(), [])

    def test_the_worker_runs_no_lane_from_the_checkout(self):
        """The issue's open question: a gate that would fail, only in the main checkout, does not run."""
        current = json.loads((self.repo / "engineering.json").read_text())
        # No implicit spelling subprocess on the published commit. Either stale lane would fail.
        current["queue"]["spelling"] = False
        failing_lanes = {lane: {"command": ["false"]} for lane in ("gate", "spelling")}
        self.session({**current, "lanes": failing_lanes}, current)
        done = self.land_from_another_project()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("fix", self.main_files())
        self.assertEqual(self.gates(), 1)

    def test_a_checkout_too_old_for_this_toolkit_stops_no_worker(self):
        """The worker's --project is the main checkout, whose configuration must not even be checked."""
        worktree = self.session({"schemaVersion": 1, "name": "fixture", "hostedRepositories": []})
        with self.assertRaises(ValueError): configuration.load(self.repo)
        done = subprocess.run(["python3", "scripts/eng", "land", "fix", "--wait", "--limit", str(stretched(120))],
                              cwd=worktree, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("fix", self.main_files())


class Changelogs(Fixture):
    """Branches that each add a line at the top of a section conflict in turn (#101)."""
    START = "# Release notes\n\n## Unreleased\n\n- old\n\n## 1.0\n\n- first\n"

    def setUp(self):
        super().setUp()
        self.commit("CHANGELOG.md", "start", self.START)
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")

    def changelog(self):
        run(self.repo, "git", "fetch", "-q", "origin")
        return run(self.repo, "git", "show", "origin/main:CHANGELOG.md") + "\n"

    def top(self, line):
        return self.START.replace("- old\n", f"{line}\n- old\n", 1)

    def outcome(self, *contents, name="CHANGELOG.md"):
        """Land each content of name, on a branch of its own from the same commit, oldest first. Each is queued
        unchecked, so that the round decides, and eng land's trial of each against those ahead of it must agree
        with the round: refused just where the round sends it back (vocaro-engineering#178)."""
        for number, content in enumerate(contents): self.commit(name, f"b{number}", content)
        ids = self.add(*(f"b{number}" for number in range(len(contents))), checked=False)
        queue = mq.Queue(self.repo)
        entries, base = queue.all(), run(self.repo, "git", "rev-parse", "origin/main")
        refused = [mq.trial(queue, entry, entries[:index], base)["why"] is not None
                   for index, entry in enumerate(entries)]
        self.work()
        states = [self.state(identifier)["state"] for identifier in ids]
        self.assertEqual(refused, [state == "conflict" for state in states], states)
        return states

    def test_each_branch_lands_with_its_line_first_after_one_docs_pass(self):
        self.assertEqual(self.outcome(self.top("- a"), self.top("- b"), self.top("- c")), ["landed"] * 3)
        self.assertEqual(self.changelog(), self.START.replace("- old\n", "- c\n- b\n- a\n- old\n", 1))
        self.assertEqual(self.gates(), 0)
        self.assertEqual(len(list(mq.Queue(self.repo).logs.glob("*-docs.log"))), 1)
        self.assertEqual([entry["docsOnly"] for entry in mq.Queue(self.repo).all()],
                         [["spelling", "docs"]] * 3)
        self.assertIsNone(mq.Queue(self.repo).held())
        # The first went in as it was; each settled one adds its very line beside other neighbors, which
        # Closeout's landed.sh proves resolved, so its branch goes unasked (#164, guides/maintenance.md).
        contained = lambda branch: mq.landing.contained(lambda *words: mq.git(self.repo, *words), "origin/main", branch)
        self.assertNotIn(contained("b0"), (None, mq.landing.RESOLVED))
        self.assertEqual([contained("b1"), contained("b2")], [mq.landing.RESOLVED] * 2)

    def test_a_settled_commit_keeps_its_message_and_author(self):
        for name, start in (("b0", self.top("- a")), ("b1", self.top("- b"))):
            self.commit("CHANGELOG.md", name, start)
        run(self.repo, "git", "commit", "-q", "--amend", "--author", "Other <other@example.com>", "-m",
            "Say b\n\n# Not a comment\n\nCloses #1.")
        self.add("b0", "b1")
        self.work()
        run(self.repo, "git", "fetch", "-q", "origin")
        self.assertEqual(run(self.repo, "git", "log", "-1", "--format=%an|%B", "origin/main"),
                         "Other|Say b\n\n# Not a comment\n\nCloses #1.")

    def test_a_branch_of_two_commits_settles_each(self):
        self.commit("CHANGELOG.md", "b0", self.top("- a"))
        self.commit("CHANGELOG.md", "b1", self.top("- b"))
        (self.repo / "other").write_text("x")
        run(self.repo, "git", "add", "other")
        run(self.repo, "git", "commit", "-qm", "Add other")
        (self.repo / "CHANGELOG.md").write_text(self.top("- c\n- b"))
        run(self.repo, "git", "add", "CHANGELOG.md")
        run(self.repo, "git", "commit", "-qm", "Add c")
        ids = self.add("b0", "b1")
        self.work()
        self.assertEqual([self.state(i)["state"] for i in ids], ["landed"] * 2)
        self.assertEqual(self.changelog(), self.START.replace("- old\n", "- c\n- b\n- a\n- old\n", 1))

    def test_a_branch_that_rewords_its_own_line_lands_once(self):
        """EPUBLib's #938: its second commit changed the line its first added, which commit by commit read as
        a change to a line ahead of it (#130)."""
        self.commit("CHANGELOG.md", "b0", self.top("- a"))
        self.commit("CHANGELOG.md", "b1", self.top("- b"))
        (self.repo / "CHANGELOG.md").write_text(self.top("- b, reworded"))
        run(self.repo, "git", "commit", "-qam", "Reword b")
        ids = self.add("b0", "b1")
        self.work()
        self.assertEqual([self.state(i)["state"] for i in ids], ["landed"] * 2)
        self.assertEqual(self.changelog(), self.START.replace("- old\n", "- b, reworded\n- a\n- old\n", 1))
        self.assertNotIn("kept", self.state(ids[0]))
        kept = self.state(ids[1])["kept"]
        self.assertEqual(len(kept), 2, kept)
        self.assertTrue(all("kept both sides of CHANGELOG.md" in line for line in kept), kept)
        self.assertIn("kept both sides of CHANGELOG.md", mq.describe(self.state(ids[1])))

    def test_a_branch_that_later_rewords_a_line_ahead_of_it_comes_back(self):
        self.commit("CHANGELOG.md", "b0", self.top("- a"))
        self.commit("CHANGELOG.md", "b1", self.top("- b"))
        (self.repo / "CHANGELOG.md").write_text(self.top("- b").replace("- old", "- old, reworded"))
        run(self.repo, "git", "commit", "-qam", "Reword old")
        ids = self.add("b0", "b1", checked=False)
        self.work()
        self.assertEqual([self.state(i)["state"] for i in ids], ["landed", "conflict"])
        self.assertEqual(self.changelog(), self.top("- a"))

    def test_a_section_with_no_entries_yet(self):
        start = "# Release notes\n\n## Unreleased\n\n## 1.0\n\n- first\n"
        self.commit("CHANGELOG.md", "empty", start)
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        self.assertEqual(self.outcome(*(start.replace("## 1.0", f"{line}\n\n## 1.0") for line in ("- a", "- b"))),
                         ["landed"] * 2)
        self.assertEqual(self.changelog(), start.replace("## 1.0", "- b\n- a\n\n## 1.0"))

    def test_a_real_conflict_in_the_changelog_comes_back(self):
        states = self.outcome(self.START.replace("- old", "- one"), self.START.replace("- old", "- two"))
        self.assertEqual(states, ["landed", "conflict"])
        self.assertEqual(self.changelog(), self.START.replace("- old", "- one"))

    def test_a_real_conflict_beside_an_added_line_comes_back_whole(self):
        ahead = self.top("- a").replace("- first", "- one")
        states = self.outcome(ahead, self.top("- b").replace("- first", "- two"))
        self.assertEqual(states, ["landed", "conflict"])
        self.assertEqual(self.changelog(), ahead)

    def test_entries_added_below_an_entry_land_with_the_new_first(self):
        """EPUBLib's #706: the entry ahead had put its line second, and so had the branch (#130)."""
        start = self.START.replace("- old\n", "- old\n- older\n", 1)
        self.commit("CHANGELOG.md", "longer", start)
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        states = self.outcome(*(start.replace("- older", f"{line}\n- older") for line in ("- a", "- b")))
        self.assertEqual(states, ["landed"] * 2)
        self.assertEqual(self.changelog(), start.replace("- older", "- b\n- a\n- older"))

    def test_lines_added_below_what_is_no_entry_come_back(self):
        start = self.START.replace("- old\n", "Entries since 1.0.\n- old\n", 1)
        self.commit("CHANGELOG.md", "intro", start)
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        states = self.outcome(*(start.replace("- old", f"{line}\n- old") for line in ("- a", "- b")))
        self.assertEqual(states, ["landed", "conflict"])

    def test_lines_that_start_no_entry_below_an_entry_come_back(self):
        """Keeping both would join the commit's wrapped line to the entry above it."""
        start = self.START.replace("- old\n", "- old\n- older\n", 1)
        self.commit("CHANGELOG.md", "longer", start)
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        states = self.outcome(start.replace("- older", "- a\n- older"), start.replace("- older", "  more of old\n- older"))
        self.assertEqual(states, ["landed", "conflict"])

    def test_a_new_section_ahead_comes_back(self):
        """Keeping both would put the line in the section just released."""
        states = self.outcome(self.START.replace("- old", "## 1.1\n\n- old"), self.top("- b"))
        self.assertEqual(states, ["landed", "conflict"])

    def test_the_same_shape_in_another_file_comes_back(self):
        # The ordinary conflict is decided by Git. Run the fixture gate on the survivor without an implicit spelling lane.
        config = json.loads((self.repo / "engineering.json").read_text())
        self.publish({**config, "queue": {**config["queue"], "spelling": False}}, "Turn the toolkit's spelling off")
        self.commit("NOTES.md", "notes", self.START)
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        states = self.outcome(self.top("- a"), self.top("- b"), name="NOTES.md")
        self.assertEqual(states, ["landed", "conflict"])
        self.assertEqual(self.gates(), 1)

    def test_a_changelog_conflict_beside_another_files_comes_back_whole(self):
        self.commit("same", "same", "start")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        for number, line in enumerate(("- a", "- b")):
            self.commit("CHANGELOG.md", f"b{number}", self.top(line))
            (self.repo / "same").write_text(line)
            run(self.repo, "git", "add", "same")
            run(self.repo, "git", "commit", "-q", "--amend", "--no-edit")
        ids = self.add("b0", "b1", checked=False)
        self.work()
        self.assertEqual([self.state(i)["state"] for i in ids], ["landed", "conflict"])
        self.assertIn("in CHANGELOG.md, same:", self.state(ids[1])["why"])
        self.assertEqual(self.changelog(), self.top("- a"))

    def test_a_line_on_both_sides_is_not_doubled(self):
        states = self.outcome(self.top("- shared\n- a"), self.top("- shared\n- b"))
        self.assertEqual(states, ["landed", "conflict"])
        self.assertEqual(self.changelog().count("- shared"), 1)

    def test_a_line_main_has_elsewhere_is_not_doubled(self):
        states = self.outcome(self.top("- a"), self.top("- first"))
        self.assertEqual(states, ["landed", "conflict"])
        self.assertEqual(self.changelog().count("- first"), 1)

    def test_a_line_written_twice_in_the_commit_is_not_doubled(self):
        states = self.outcome(self.top("- a"), self.top("- b\n- b"))
        self.assertEqual(states, ["landed", "conflict"])
        self.assertNotIn("- b", self.changelog())

    def test_a_branch_landed_already_is_not_doubled(self):
        self.assertEqual(self.outcome(self.top("- a"), self.top("- b")), ["landed"] * 2)
        run(self.repo, "git", "checkout", "-q", "-B", "again", "origin/main~2")
        (self.repo / "CHANGELOG.md").write_text(self.top("- a"))
        run(self.repo, "git", "add", "CHANGELOG.md")
        run(self.repo, "git", "commit", "-qm", "Add a again")
        self.add("again")
        self.work()
        self.assertEqual(self.changelog().count("- a\n"), 1)

    def test_line_endings_stay_as_they_are(self):
        start, ahead, new = (text.encode().replace(b"\n", b"\r\n") for text in
                             (self.START, self.top("- a"), self.top("- b")))
        self.assertEqual(mq.keep_both(start, ahead, new), self.START.replace("- old\n", "- b\n- a\n- old\n", 1)
                         .encode().replace(b"\n", b"\r\n"))

    def test_keep_both_needs_a_conflict(self):
        start = self.START.encode()
        self.assertIsNone(mq.keep_both(start, start, self.top("- a").encode()))


class LanePolicy(Fixture):
    """A lane in the fixture reads the policy from a local origin: no GitHub, and no write to the real checkout (#298)."""
    def traced(self, *words, **variables):
        """What Git says it ran for words, a command in the fixture's project, with variables in its environment."""
        trace = self.base / "command.trace"
        environment = {key: value for key, value in {**os.environ, **variables}.items() if value is not None}
        subprocess.run(words, cwd=self.repo, capture_output=True, stdin=subprocess.DEVNULL, timeout=stretched(60),
                       env={**environment, "GIT_TRACE": str(trace), "GIT_TERMINAL_PROMPT": "0"})
        return trace.read_text()

    def test_a_lane_fetches_the_policy_from_the_local_origin_and_nothing_from_github(self):
        trace = self.base / "lane.trace"
        with patch.dict(os.environ, {"GIT_TRACE": str(trace)}):
            self.commit("a", "a")
            [identifier] = self.add("a")
            status = self.work()
        self.assertEqual((status, self.state(identifier)["state"], self.gates()), (0, "landed", 1))
        said = trace.read_text()
        self.assertNotIn("remote-http", said)
        self.assertNotIn("github.com", said)
        # The control: the trace holds the policy's fetch, and that fetch went to the origin on this disk.
        self.assertIn("fetch origin refs/heads/main:refs/remotes/origin/main", said)
        self.assertIn(str(local_toolkit()[1]), said)

    def test_the_trace_shows_a_fetch_over_https_and_the_fixture_refuses_one(self):
        """The negative controls, at a port on this Mac that nothing answers, so that neither needs the network: what
        the lane's test looks for is what Git prints for HTTPS, and the fixture's own Git never gets that far."""
        fetch = ("git", "ls-remote", "https://127.0.0.1:1/toolkit.git")
        self.assertIn("remote-http", self.traced(*fetch, GIT_ALLOW_PROTOCOL=None))
        (self.base / "command.trace").unlink()
        self.assertNotIn("remote-http", self.traced(*fetch))

    def test_the_lanes_toolkit_is_a_clean_copy_whose_origin_is_local(self):
        from vocaro_engineering import release
        kit, origin = local_toolkit()
        self.assertEqual(os.environ["VOCARO_ENGINEERING_ROOT"], str(kit))
        self.assertNotEqual(kit.resolve(), ROOT.resolve())
        self.assertEqual(run(kit, "git", "remote", "get-url", "origin"), str(origin))
        self.assertEqual(run(kit, "git", "rev-parse", "origin/main"), run(kit, "git", "rev-parse", "HEAD"))
        self.assertEqual(cli.source_identity(kit)["workingChangesSHA256"], release.remote.CLEAN)


class ToolkitCopy(Fixture):
    """Which toolkit a project's gate runs, with a toolkit repository of its own in the fixture."""
    def setUp(self):
        super().setUp()
        self.seen = self.base / "seen"
        (self.repo / "report.py").write_text(
            "import os, pathlib, subprocess, sys\n"
            "root = os.environ.get('VOCARO_ENGINEERING_ROOT', '')\n"
            "head = subprocess.run(['git', '-C', root, 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip() if root else ''\n"
            "dirty = subprocess.run(['git', '-C', root, 'status', '--porcelain'], capture_output=True, text=True).stdout.strip() if root else ''\n"
            "pathlib.Path(sys.argv[1]).write_text('\\n'.join([root, head, dirty]))\n")
        config = json.loads((self.repo / "engineering.json").read_text())
        config["lanes"]["gate"]["command"] = ["python3", "report.py", str(self.seen)]
        run(self.repo, "git", "checkout", "-q", "-B", "start", "origin/main")
        run(self.repo, "git", "add", "report.py")
        self.publish(config, "Report the toolkit")
        self.kit_origin = self.base / "toolkit.git"
        run(self.base, "git", "init", "-q", "--bare", "-b", "main", str(self.kit_origin))
        self.kit = self.base / "toolkit"
        run(self.base, "git", "clone", "-q", str(self.kit_origin), str(self.kit))
        shutil.copytree(ROOT / "src/python", self.kit / "src/python", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy(ROOT / ".gitignore", self.kit)
        # The policy every admitted lane reads, as the toolkit's origin holds it (cli.refresh_policy).
        shutil.copytree(ROOT / "policies", self.kit / "policies")
        run(self.kit, "git", "add", ".")
        run(self.kit, "git", "commit", "-qm", "Toolkit")
        run(self.kit, "git", "push", "-q", "origin", "HEAD:main")
        # This Mac's queue folder, laid out as the second Mac's checkouts are (vocaro-engineering#104).
        self.checkout = self.base / "vocaro-queue/project/project"
        self.copy = self.base / "vocaro-queue/project/toolkit"
        variables = patch.dict(os.environ)
        variables.start()
        self.addCleanup(variables.stop)
        del os.environ["VOCARO_ENGINEERING_ROOT"]
        # Let subprocesses write bytecode even if the calling shell turns it off. Apple's Python 3.9
        # defaults to a central cache rather than an adjacent __pycache__; keep both interpreters'
        # caches inside this fixture and check the copied checkout's path below (#368).
        os.environ.pop("PYTHONDONTWRITEBYTECODE", None)
        self.bytecode = self.base / "bytecode"
        os.environ["PYTHONPYCACHEPREFIX"] = str(self.bytecode)
        toolkit = patch.object(mq, "TOOLKIT_ROOT", self.kit)
        toolkit.start()
        self.addCleanup(toolkit.stop)
        # The worker's move to the copy, through its seam rather than a real exec, which would replace this process.
        self.moves = []
        def execute(command, env): raise Replaced(command, env)
        for patcher in (patch.object(mq, "execute", execute), patch.object(mq, "load", mq.load)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def work(self):
        """The worker, then the one that replaces it when it moves to the copy: a process of its own in use
        (merge_queue.execute), here the same module started again with the copy as its toolkit, the copy's config
        module judging main, and the environment the move carried."""
        while True:
            try: return mq.run(self.repo)
            except Replaced as moved:
                self.moves.append(moved)
                self.assertEqual(moved.command, mq.Queue(self.repo).command(self.copy))
                self.assertNotIn("VOCARO_ENGINEERING_ROOT", moved.env)
                mq.TOOLKIT_ROOT = self.copy
                mq.load = self.code_of(self.copy).load
                os.environ[mq.PROGRESS_VARIABLE] = moved.env[mq.PROGRESS_VARIABLE]

    def code_of(self, checkout):
        """A toolkit checkout's config module, loaded apart from this process's."""
        source = checkout / "src/python/queuekeeper/config.py"
        spec = importlib.util.spec_from_file_location(f"config_of_{len(self.moves)}", source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def advance_origin(self, name):
        """A landed toolkit commit the shared checkout has not pulled."""
        other = self.base / f"other-{name}"
        run(self.base, "git", "clone", "-q", str(self.kit_origin), str(other))
        (other / name).write_text("x")
        run(other, "git", "add", name)
        run(other, "git", "commit", "-qm", f"Add {name}")
        run(other, "git", "push", "-q", "origin", "HEAD:main")
        return run(other, "git", "rev-parse", "HEAD")

    def gate_once(self, name):
        self.commit(name, name)
        [identifier] = self.add(name)
        status = self.work()
        return identifier, status

    def test_a_gate_runs_the_toolkit_origin_has_not_the_lagging_shared_checkout(self):
        behind = run(self.kit, "git", "rev-parse", "HEAD")
        landed = self.advance_origin("landed")
        with contextlib.redirect_stdout(io.StringIO()) as said: identifier, status = self.gate_once("a")
        self.assertEqual(status, 0)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(self.seen.read_text().split("\n"), [str(self.copy), landed, ""])
        cache = self.bytecode / self.copy.relative_to(self.copy.anchor) / "src/python/queuekeeper"
        self.assertTrue(any(cache.glob("cli.*.pyc")))
        self.assertEqual(run(self.kit, "git", "rev-parse", "HEAD"), behind)
        # And so does the worker itself, once, from the copy (vocaro-engineering#173).
        self.assertEqual(len(self.moves), 1)
        self.assertIn(f"moving to the toolkit at {landed[:12]}, origin/main, from {behind[:12]}, which this worker was "
                      "started from: the next worker is started from the copy", said.getvalue())
        self.assertEqual(json.loads(self.moves[0].env[mq.PROGRESS_VARIABLE]), {"limit": None, "suspects": 0, "no_verdicts": 0})
        self.assertNotIn(mq.PROGRESS_VARIABLE, os.environ)

    def test_a_worker_started_from_the_copy_stays(self):
        """The negative control: the toolkit at origin is the code the worker runs, so it does not move."""
        self.gate_once("a")
        self.assertEqual(self.moves, [])
        mq.TOOLKIT_ROOT = self.copy
        with contextlib.redirect_stdout(io.StringIO()) as said: identifier, status = self.gate_once("b")
        self.assertEqual((status, self.state(identifier)["state"], self.moves), (0, "landed", []))
        self.assertNotIn("moving to the toolkit", said.getvalue())
        self.assertEqual(self.seen.read_text().split("\n")[0], str(self.copy))

    def knowing(self, key):
        """A landed toolkit commit whose config module accepts the lane key that this toolkit's own refuses."""
        other = self.base / f"other-knows-{key}"
        run(self.base, "git", "clone", "-q", str(self.kit_origin), str(other))
        source = other / "src/python/queuekeeper/config.py"
        edited = accepting_lane_key(source.read_text(), key)
        source.write_text(edited)
        run(other, "git", "add", "src/python/queuekeeper/config.py")
        run(other, "git", "commit", "-qm", f"Accept {key}")
        run(other, "git", "push", "-q", "origin", "HEAD:main")
        return run(other, "git", "rev-parse", "HEAD")

    def set_on_main(self, key):
        """main's gate lane with the key set, published after the entry was added, as EPUBLib's 41f46e94 landed it."""
        config = json.loads((self.repo / "engineering.json").read_text())
        config["lanes"]["gate"][key] = True
        run(self.repo, "git", "checkout", "-q", "-B", "start", "origin/main")
        self.publish(config, f"Set {key} on the gate lane")
        return config

    def test_the_worker_judges_main_with_the_toolkit_at_origin_not_its_own_older_code(self):
        """EPUBLib's hold of 2026-11-04: main's gate lane set a key the copy accepted and the worker's own config
        module, from an older checkout, refused (vocaro-engineering#173)."""
        self.commit("a", "a")
        [identifier] = self.add("a")
        config = self.set_on_main("newKey")
        behind = run(self.kit, "git", "rev-parse", "HEAD")
        landed = self.knowing("newKey")
        # The control: the code this worker is started from refuses main.
        with self.assertRaisesRegex(ValueError, "Invalid lane gate"): configuration.parse(json.dumps(config))
        with contextlib.redirect_stdout(io.StringIO()) as said: status = self.work()
        self.assertEqual(status, 0)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertIn("a", self.main_files())
        self.assertIsNone(mq.Queue(self.repo).held())
        self.assertEqual(len(self.moves), 1)
        self.assertIn(f"moving to the toolkit at {landed[:12]}, origin/main, from {behind[:12]}", said.getvalue())
        self.assertEqual(self.seen.read_text().split("\n"), [str(self.copy), landed, ""])
        self.assertEqual(self.code_of(self.copy).parse(json.dumps(config))["lanes"]["gate"]["newKey"], True)
        self.assertEqual(run(self.kit, "git", "rev-parse", "HEAD"), behind)

    def test_main_that_the_newest_toolkit_refuses_holds_for_a_session_naming_the_toolkit(self):
        """The negative control: with nothing newer at origin, the worker stays, and the hold is a session's, since
        a session lands the fix, in the toolkit or on main; it never waits for the owner."""
        self.commit("a", "a")
        [identifier] = self.add("a")
        self.set_on_main("newKey")
        code = run(self.kit, "git", "rev-parse", "HEAD")
        with patch("builtins.print"): status = self.work()
        self.assertEqual(status, 1)
        self.assertEqual(self.moves, [])
        self.assertEqual(self.state(identifier)["state"], "queued")
        self.assertFalse(self.seen.exists())
        held = mq.Queue(self.repo).held()
        base = run(self.repo, "git", "rev-parse", "origin/main")
        self.assertEqual(held, f"for a session, not the owner: Invalid lane gate, judging main at {base[:12]} with the "
                               f"toolkit at {code[:12]}; the session that fixes it, or any session that finds it gone, "
                               "runs `eng queue resume` itself and says so")

    def test_a_bisect_goes_on_from_where_the_worker_that_moved_left_it(self):
        """Two entries that fail together, the toolkit landing between the rounds: the worker that takes over
        tests the oldest alone, as the one it replaces was about to, costing no gate."""
        config = json.loads((self.repo / "engineering.json").read_text())
        config["lanes"]["gate"]["command"] = ["python3", "lane.py", str(self.counter), str(self.status)]
        # Kept on this Mac, so that the bisect gates one half at a time, which the count below follows, rather than its
        # halves at once (Beside): every lane is admitted and offerable since the owner's decision of 2026-11-07.
        config["lanes"]["gate"]["here"] = "the test counts its gates one after another"
        run(self.repo, "git", "checkout", "-q", "-B", "start", "origin/main")
        self.publish(config, "Fail on bad files")
        self.commit("a", "a"); self.commit("bad", "bad")
        ids = self.add("a", "bad")
        real = mq.Worker.gate
        def gate(worker, *arguments, **keywords):
            result = real(worker, *arguments, **keywords)
            if self.gates() == 1: self.advance_origin("between")
            return result
        with patch.object(mq.Worker, "gate", gate), patch("builtins.print"): self.assertEqual(self.work(), 0)
        self.assertEqual([self.state(i)["state"] for i in ids], ["landed", "failed"])
        self.assertEqual(len(self.moves), 1)
        self.assertEqual(json.loads(self.moves[0].env[mq.PROGRESS_VARIABLE]), {"limit": 1, "suspects": 2, "no_verdicts": 0})
        # Both together, then a alone, then bad alone: not both together again.
        self.assertEqual(self.gates(), 3)

    def test_a_dirty_shared_checkout_neither_reaches_a_gate_nor_is_touched(self):
        edited = self.kit / "src/python/queuekeeper/cli.py"
        edited.write_text(edited.read_text() + "\nraise SystemExit(1)  # a session's edit\n")
        (self.kit / "untracked").write_text("x")
        identifier, status = self.gate_once("a")
        self.assertEqual(status, 0)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(self.seen.read_text().split("\n")[2], "")
        self.assertIn("a session's edit", edited.read_text())
        self.assertTrue((self.kit / "untracked").exists())

    def test_the_copy_follows_origin_and_loses_what_was_left_in_it(self):
        self.gate_once("a")
        (self.copy / "left").write_text("x")
        (self.copy / "src/python/queuekeeper/project.py").write_text("raise SystemExit(1)\n")
        landed = self.advance_origin("later")
        self.gate_once("b")
        self.assertEqual(self.seen.read_text().split("\n"), [str(self.copy), landed, ""])

    def test_a_toolkit_that_cannot_be_fetched_holds_and_blames_nobody(self):
        run(self.kit, "git", "remote", "set-url", "origin", str(self.base / "gone.git"))
        identifier, status = self.gate_once("a")
        self.assertEqual(status, 1)
        self.assertEqual(self.state(identifier)["state"], "queued")
        self.assertIn("eng queue resume", mq.Queue(self.repo).held())
        self.assertFalse(self.seen.exists())

    def test_something_at_the_copys_path_holds_and_is_left_alone(self):
        self.copy.mkdir(parents=True)
        (self.copy / "kept").write_text("x")
        identifier, status = self.gate_once("a")
        self.assertEqual(status, 1)
        self.assertEqual(self.state(identifier)["state"], "queued")
        self.assertTrue((self.copy / "kept").exists())

    def test_the_checkouts_relative_path_to_the_toolkit_finds_the_queues_copy(self):
        # Pagepour's and studywright's ../vocaro-engineering, here ../toolkit, as a lane reads it.
        config = json.loads((self.repo / "engineering.json").read_text())
        config["lanes"]["gate"]["command"] = ["python3", "-c", "import os, pathlib, sys; "
                                              "pathlib.Path(sys.argv[1]).write_text(os.path.realpath('../toolkit'))",
                                              str(self.seen)]
        run(self.repo, "git", "checkout", "-q", "-B", "start", "origin/main")
        self.publish(config, "Read the toolkit by its relative path")
        identifier, status = self.gate_once("a")
        self.assertEqual(status, 0)
        self.assertEqual(self.seen.read_text(), str(self.copy))
        listed = run(self.repo, "git", "worktree", "list", "--porcelain")
        self.assertIn(f"worktree {self.checkout}\n", listed)
        self.assertIn(f"worktree {self.copy}\n", run(self.kit, "git", "worktree", "list", "--porcelain"))
        # Nothing beside the main checkout, where the old layout kept them.
        self.assertFalse((self.base / "project-queue").exists())
        self.assertFalse((self.base / "project-queue-toolkit").exists())

    def old_layout(self):
        """The checkouts an earlier worker kept beside the main checkout, each locked as it locked them,
        the project's with an input a lane provisioned."""
        old, old_kit = self.base / "project-queue", self.base / "project-queue-toolkit"
        run(self.repo, "git", "worktree", "add", "-q", "--detach", "--lock", "--reason", "the merge queue's checkout",
            str(old), "origin/main")
        run(self.kit, "git", "worktree", "add", "-q", "--detach", "--lock", "--reason",
            "the merge queue's copy of the toolkit", str(old_kit), "origin/main")
        info = Path(run(old, "git", "rev-parse", "--path-format=absolute", "--git-common-dir")) / "info/exclude"
        info.parent.mkdir(exist_ok=True)
        info.write_text("provisioned/\n")
        (old / "provisioned").mkdir()
        (old / "provisioned/input").write_text("made by a provision lane\n")
        return old, old_kit

    def locks(self, repository):
        found, path = {}, None
        for line in run(repository, "git", "worktree", "list", "--porcelain").splitlines():
            if line.startswith("worktree "): path = line[len("worktree "):]
            elif line.startswith("locked"): found[path] = line[len("locked "):]
        return found

    def test_the_old_layout_moves_once_nothing_uses_it_and_keeps_what_was_provisioned(self):
        old, old_kit = self.old_layout()
        with patch.object(mq.disk, "census", return_value=[]):
            identifier, status = self.gate_once("a")
        self.assertEqual(status, 0)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertFalse(old.exists())
        self.assertFalse(old_kit.exists())
        self.assertEqual((self.checkout / "provisioned/input").read_text(), "made by a provision lane\n")
        self.assertEqual(self.seen.read_text().split("\n")[0], str(self.copy))
        # Still locked, with the reasons they had, and Git knows each only at its new place.
        self.assertEqual(self.locks(self.repo), {str(self.checkout): "the merge queue's checkout"})
        self.assertEqual(self.locks(self.kit), {str(self.copy): "the merge queue's copy of the toolkit"})
        run(self.checkout, "git", "status", "--porcelain")
        run(self.copy, "git", "status", "--porcelain")

    def test_a_moved_checkout_forgets_the_package_state_that_names_its_old_path(self):
        # As studywright's suite-root failed on 2026-11-02: its moved .build named ~/Development/studywright-queue (#114).
        old, _old_kit = self.old_layout()
        info = Path(run(old, "git", "rev-parse", "--path-format=absolute", "--git-common-dir")) / "info/exclude"
        info.write_text(info.read_text() + ".build/\n")
        stale = {".build": "SwiftPM's", "app/.build/check-app/SourcePackages": "Xcode's",
                 ".build/DerivedData/ios-tests/SourcePackages": "Xcode's, in DerivedData"}
        for folder in stale:
            (old / folder).mkdir(parents=True)
            (old / folder / "workspace-state.json").write_text(json.dumps({"path": str(old / folder / "checkouts")}))
        for name in ("artifacts/bundle/info.json", "checkouts/tokenizers/Package.swift", "repositories/tokenizers/HEAD",
                     "debug/product", "DerivedData/ios-tests/Build/product"):
            (old / ".build" / name).parent.mkdir(parents=True, exist_ok=True)
            (old / ".build" / name).write_text("built\n")
        # A package resolved at the new path keeps its state.
        current = self.checkout / "Packages/Reader/.build"
        (old / "Packages/Reader/.build/artifacts").mkdir(parents=True)
        (old / "Packages/Reader/.build/workspace-state.json").write_text(json.dumps({"path": str(current)}))
        # Exercise cleanup after a confirmed move. The cases below cover a live user and an unavailable census.
        with patch.object(mq.disk, "census", return_value=[]), \
                contextlib.redirect_stdout(io.StringIO()) as said:
            identifier, status = self.gate_once("a")
        self.assertEqual(status, 0)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertFalse(old.exists())
        moved = self.checkout / ".build"
        for name in ("workspace-state.json", "artifacts", "checkouts", "repositories"):
            self.assertFalse((moved / name).exists(), name)
        for folder in list(stale)[1:]: self.assertFalse((self.checkout / folder).exists(), folder)
        # Build products stay, and so does the input a lane provisioned.
        self.assertEqual((moved / "debug/product").read_text(), "built\n")
        self.assertEqual((moved / "DerivedData/ios-tests/Build/product").read_text(), "built\n")
        self.assertEqual((self.checkout / "provisioned/input").read_text(), "made by a provision lane\n")
        self.assertTrue((current / "workspace-state.json").exists())
        self.assertTrue((current / "artifacts").exists())
        for folder in stale: self.assertIn(f"cleared {self.checkout / folder}, which named {old}", said.getvalue())
        self.assertNotIn("Packages/Reader", said.getvalue())

    def test_an_old_checkout_a_process_uses_stays_and_the_new_one_is_made_afresh(self):
        old, old_kit = self.old_layout()
        # A lane an earlier worker left running in it, found by the real process census.
        held = subprocess.Popen(["sleep", "300"], cwd=old)
        self.addCleanup(held.wait)
        self.addCleanup(held.kill)
        with contextlib.redirect_stdout(io.StringIO()) as said:
            identifier, status = self.gate_once("a")
        self.assertEqual(status, 0)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual((old / "provisioned/input").read_text(), "made by a provision lane\n")
        self.assertFalse((self.checkout / "provisioned").exists())
        locks = self.locks(self.repo)
        self.assertEqual(locks[str(self.checkout)], "the merge queue's checkout")
        self.assertEqual(locks[str(old)], f"retired by the merge queue: replaced by {self.checkout}")
        # The toolkit's copy, which nothing used, moved.
        self.assertFalse(old_kit.exists())
        self.assertEqual(self.locks(self.kit), {str(self.copy): "the merge queue's copy of the toolkit"})
        self.assertIn(f"{old} stays where it is, since PID {held.pid} (sleep)", said.getvalue())

    def test_an_old_checkout_that_cannot_be_checked_for_processes_stays(self):
        old, _old_kit = self.old_layout()
        with patch.object(mq.disk, "census", side_effect=RuntimeError("could not list processes with lsof and ps")):
            identifier, status = self.gate_once("a")
        self.assertEqual(status, 0)
        self.assertTrue((old / "provisioned/input").exists())
        self.assertTrue(self.locks(self.repo)[str(old)].startswith("retired by the merge queue"))

    def test_the_toolkits_own_queue_tests_its_batch(self):
        with patch.object(mq, "TOOLKIT_ROOT", self.repo):
            worker = mq.Worker(mq.Queue(self.repo))
            worker.toolkit()
        self.assertNotIn("VOCARO_ENGINEERING_ROOT", worker.environment)
        self.assertFalse(self.copy.exists())

    def test_a_toolkit_at_origin_that_does_not_start_is_not_moved_to(self):
        """A toolkit commit pushed by hand whose worker code does not import: the project's worker stays on its own
        code, and its lanes run the toolkit it was started from, rather than a gate that sends every entry back."""
        behind = run(self.kit, "git", "rev-parse", "HEAD")
        other = self.base / "other-broken"
        run(self.base, "git", "clone", "-q", str(self.kit_origin), str(other))
        source = other / "src/python/queuekeeper/merge_queue.py"
        source.write_text(source.read_text() + "\ndef broken(:\n")
        run(other, "git", "commit", "-qam", "Break the worker")
        broken = run(other, "git", "rev-parse", "HEAD")
        run(other, "git", "push", "-q", "origin", "HEAD:main")
        with contextlib.redirect_stdout(io.StringIO()) as said: identifier, status = self.gate_once("a")
        self.assertEqual((status, self.state(identifier)["state"], self.moves), (0, "landed", []))
        self.assertIn(f"staying on the toolkit at {behind[:12]}, which this worker was started from, rather than "
                      f"moving to {broken[:12]}, origin/main: importing it exited 1: SyntaxError", said.getvalue())
        self.assertEqual(self.seen.read_text().split("\n"), [str(self.copy), behind, ""])

    def test_an_explicit_toolkit_stands(self):
        with patch.dict(os.environ, {"VOCARO_ENGINEERING_ROOT": str(self.kit)}):
            identifier, status = self.gate_once("a")
        self.assertEqual(status, 0)
        self.assertEqual(self.seen.read_text().split("\n")[0], str(self.kit))
        self.assertFalse(self.copy.exists())


class ToolkitsOwnWorker(Fixture):
    """The toolkit's own queue judging its main with the toolkit at origin/main, not the code its worker was started
    from (vocaro-engineering#177). The fixture's project is the toolkit: it holds the toolkit's Python, and the
    worker starts from a checkout of it at the first commit, as from the shared checkout before follow moves it."""
    ROOT_PREFIX = "fixture_" + "oei" + "tre_"

    def setUp(self):
        super().setUp()
        shutil.copytree(ROOT / "src/python", self.repo / "src/python", ignore=shutil.ignore_patterns("__pycache__"))
        # The policy every admitted lane reads, as the toolkit's origin holds it (cli.refresh_policy).
        shutil.copytree(ROOT / "policies", self.repo / "policies")
        # Its own toolkit, as the toolkit's engineering.json names it, with the toolkit's spelling exemptions, which
        # its Python needs to pass the spelling check the queue runs before the gate.
        config = json.loads((self.repo / "engineering.json").read_text())
        spelling = configuration.load(ROOT).get("spelling", {})
        spelling = {**spelling, "exemptPhrases": {
            **spelling.get("exemptPhrases", {}), **config["spelling"]["exemptPhrases"]}}
        (self.repo / "engineering.json").write_text(json.dumps(
            {**config, "toolkit": {"path": "."}, "spelling": spelling}))
        run(self.repo, "git", "add", "src", "policies", "engineering.json")
        run(self.repo, "git", "commit", "-qm", "Toolkit")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        self.started = self.base / "started"
        run(self.repo, "git", "worktree", "add", "-q", "--detach", str(self.started), "origin/main")
        self.copy = self.base / "vocaro-queue/project/project-toolkit"
        variables = patch.dict(os.environ)
        variables.start()
        self.addCleanup(variables.stop)
        del os.environ["VOCARO_ENGINEERING_ROOT"]
        for patcher in (patch.object(mq, "TOOLKIT_ROOT", self.started), patch.object(mq, "execute", self.execute),
                        patch.object(mq, "load", mq.load)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.moves, self.follow = [], True

    def execute(self, command, env): raise Replaced(command, env)

    def work(self):
        """The worker, then the one its move starts from the copy, with the copy's config module judging main, as
        ToolkitCopy.work; with follow False, a move ends the worker as a process that died at once would."""
        while True:
            try: return mq.run(self.repo)
            except Replaced as moved:
                self.moves.append(moved)
                self.assertEqual(moved.command, mq.Queue(self.repo).command(self.copy))
                if not self.follow: return None
                mq.TOOLKIT_ROOT = self.copy
                mq.load = self.code_of(self.copy).load
                os.environ[mq.PROGRESS_VARIABLE] = moved.env[mq.PROGRESS_VARIABLE]

    def code_of(self, checkout):
        """A toolkit checkout's config module, loaded apart from this process's."""
        source = checkout / "src/python/queuekeeper/config.py"
        spec = importlib.util.spec_from_file_location(f"own_config_{len(self.moves)}_{checkout.name}", source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def toolkit_branch(self, branch, edit, key=None):
        """A toolkit commit on a new branch from origin/main: edit(text) of merge_queue.py's or, with key, config.py
        accepting key and main's gate lane setting it, in one commit."""
        run(self.repo, "git", "fetch", "-q", "origin")
        run(self.repo, "git", "checkout", "-q", "-B", branch, "origin/main")
        name = "config.py" if key else "merge_queue.py"
        source = self.repo / "src/python/queuekeeper" / name
        original = source.read_text()
        edited = accepting_lane_key(original, key) if key else edit(original)
        self.assertNotEqual(edited, original)
        source.write_text(edited)
        if key:
            config = json.loads((self.repo / "engineering.json").read_text())
            config["lanes"]["gate"][key] = True
            (self.repo / "engineering.json").write_text(json.dumps(config))
        run(self.repo, "git", "commit", "-qam", f"Change the toolkit on {branch}")
        return run(self.repo, "git", "rev-parse", "HEAD")

    def landed(self, name, follow=True, adder=None):
        """Queue the branch name, work, and return (exit, its entry's state, what the worker said). With adder, a
        config module, `eng land` reads main with it, as a session's eng at origin/main does."""
        with patch.object(mq, "parse", adder.parse if adder else mq.parse): [identifier] = self.add(name)
        self.follow = follow
        with contextlib.redirect_stdout(io.StringIO()) as said: status = self.work()
        return status, self.state(identifier)["state"], said.getvalue()

    def test_a_batch_that_adds_a_key_and_sets_it_lands_and_the_next_round_needs_no_hold(self):
        """The case #173 left: the toolkit's own worker, started before the batch, judged the next round's main,
        which sets the key, with its own config module, which refuses it, and held."""
        code = run(self.started, "git", "rev-parse", "HEAD")
        self.toolkit_branch("knows", None, key="newKey")
        self.assertEqual(self.landed("knows")[:2], (0, "landed"))
        self.assertEqual(self.moves, [])
        knows = run(self.repo, "git", "rev-parse", "origin/main")
        # The control: the code this worker is started from refuses main as it now stands.
        main = run(self.repo, "git", "show", "origin/main:engineering.json")
        with self.assertRaisesRegex(ValueError, "Invalid lane gate"): configuration.parse(main)
        self.commit("b", "b")
        status, state, said = self.landed("b", adder=self.code_of(self.repo))
        self.assertEqual((status, state), (0, "landed"))
        self.assertIsNone(mq.Queue(self.repo).held())
        self.assertEqual(len(self.moves), 1)
        self.assertIn(f"moving to the toolkit at {knows[:12]}, origin/main, from {code[:12]}", said)
        self.assertNotIn("VOCARO_ENGINEERING_ROOT", self.moves[0].env)
        self.assertEqual(run(self.copy, "git", "rev-parse", "HEAD"), knows)
        # The worker the move started finished a round, so the move is forgotten.
        self.assertFalse((mq.Queue(self.repo).directory / mq.MOVED).exists())

    def test_a_worker_whose_code_is_origin_main_does_not_move(self):
        """The negative control: no copy, no move."""
        self.commit("a", "a")
        self.assertEqual(self.landed("a")[:2], (0, "landed"))
        self.assertEqual(self.moves, [])
        self.assertFalse(self.copy.exists())

    def test_a_main_whose_worker_does_not_start_lands_its_fix_through_the_old_worker(self):
        """A commit on main, pushed by hand, whose worker code does not import: moving there would leave the queue
        no worker, so the fix could land only by hand. The worker stays on its own code and lands the fix; then,
        main starting again, the next round moves."""
        code = run(self.started, "git", "rev-parse", "HEAD")
        broken = self.toolkit_branch("broken", lambda source: source + "\ndef broken(:\n")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        self.assertIn("SyntaxError", mq.starts(self.repo, broken))
        self.assertIsNone(mq.starts(self.repo, code))
        self.toolkit_branch("fix", lambda source: source.replace("\ndef broken(:\n", ""))
        status, state, said = self.landed("fix")
        self.assertEqual((status, state, self.moves), (0, "landed", []))
        self.assertIn(f"staying on the toolkit at {code[:12]}, which this worker was started from, rather than "
                      f"moving to {broken[:12]}, origin/main: importing it exited 1: SyntaxError", said)
        self.assertIsNone(mq.Queue(self.repo).held())
        self.commit("c", "c")
        status, state, said = self.landed("c")
        self.assertEqual((status, state, len(self.moves)), (0, "landed", 1))
        self.assertNotIn("staying on the toolkit", said)
        self.assertEqual(run(self.copy, "git", "show", "-s", "--format=%s", "HEAD"), "Change the toolkit on fix")

    def test_a_toolkit_a_moved_worker_stopped_in_is_not_moved_to_again(self):
        """A worker that moved and then died, or held, before it finished a round: the next worker, which the next
        `eng land` starts from older code, stays on that code and lands, rather than moving into the same stop.
        Only that commit is refused: the next one on main is moved to."""
        code = run(self.started, "git", "rev-parse", "HEAD")
        later = self.toolkit_branch("later", lambda source: source + "\n# A later toolkit.\n")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        self.commit("a", "a")
        self.assertEqual(self.landed("a", follow=False)[1], "queued")
        moved = json.loads((mq.Queue(self.repo).directory / mq.MOVED).read_text())
        self.assertEqual((moved["from"], moved["to"]), (code, later))
        status, state, said = self.landed_again()
        self.assertEqual((status, state, len(self.moves)), (0, "landed", 1))
        self.assertIn(f"staying on the toolkit at {code[:12]}, which this worker was started from, rather than "
                      f"moving to {later[:12]}, origin/main: a worker moved to it before and stopped before it "
                      "finished a round", said)
        self.assertTrue((mq.Queue(self.repo).directory / mq.MOVED).exists())
        # The positive control: main has moved past that commit, so the next worker moves, and the one it starts
        # finishes a round, which forgets the move.
        landed = run(self.repo, "git", "rev-parse", "origin/main")
        self.commit("c", "c")
        status, state, said = self.landed("c")
        self.assertEqual((status, state, len(self.moves)), (0, "landed", 2))
        self.assertIn(f"moving to the toolkit at {landed[:12]}, origin/main, from {code[:12]}", said)
        self.assertFalse((mq.Queue(self.repo).directory / mq.MOVED).exists())

    def test_a_worker_under_its_launcher_follows_after_moving_to_a_newer_toolkit(self):
        """vocaro-engineering#374, as the production worker ran on 2026-11-10: the signed launcher started the worker
        from the shared checkout, the worker moved to a toolkit copy, and the launcher, still its parent with the
        original arguments, held back the checkout the worker then meant to follow. Another queue's launcher runs
        nothing from it either (#384)."""
        queue = mq.Queue(self.repo)
        own = launcher_process(4242, queue, self.repo)
        another = launcher_process(4343, queue, self.repo)
        load = mq.load
        # One try after the landing, as SharedCheckout makes it: a held-back checkout ends the worker at once.
        limit = patch.object(mq, "FOLLOW_LIMIT", 0)
        limit.start()
        self.addCleanup(limit.stop)
        later = self.toolkit_branch("later", lambda source: source + "\n# A later toolkit.\n")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        for name, processes, verified in (("unsigned launcher", [own], False), ("another launcher", [own, another], True),
                                          ("own launcher", [own], True)):
            with self.subTest(name):
                self.commit(name.replace(" ", "-"), name.replace(" ", "-"))
                [identifier] = self.add(name.replace(" ", "-"))
                # The shared checkout on main, behind the landing to come.
                run(self.repo, "git", "checkout", "-q", "-B", "main", "origin/main")
                before = run(self.repo, "git", "rev-parse", "HEAD")
                moves = len(self.moves)
                with under_launcher(processes, verified), contextlib.redirect_stdout(io.StringIO()) as said:
                    self.assertEqual(self.work(), 0)
                said = said.getvalue()
                self.assertEqual(self.state(identifier)["state"], "landed")
                self.assertEqual(len(self.moves), moves + 1, said)
                # The moved worker runs from the copy, while its launcher's arguments still name the shared checkout.
                self.assertIn(str(self.copy / "src/python"), self.moves[-1].command)
                self.assertNotIn(str(self.repo / "src/python"), self.moves[-1].command)
                after, main = run(self.repo, "git", "rev-parse", "HEAD"), run(self.repo, "git", "rev-parse", "origin/main")
                if verified:
                    self.assertEqual(after, main)
                    self.assertIn(f"fast-forwarded {self.repo} from {before[:12]} to {main[:12]}", said)
                else:
                    # The negative control: a launcher that does not verify as signed still holds it back.
                    self.assertEqual(after, before)
                    self.assertIn(f"{mq.IN_USE}: PID 4242 ({mq.LAUNCHER_PROGRAM}), argument {self.repo}/src/python", said)
                # The next worker the launcher starts begins from the older toolkit again and moves.
                mq.TOOLKIT_ROOT, mq.load = self.started, load
        self.assertEqual(run(self.copy, "git", "log", "-1", "--format=%s", later), "Change the toolkit on later")

    def landed_again(self):
        """Work the entry a worker left queued: (exit, its state, what the worker said)."""
        [entry] = mq.Queue(self.repo).waiting()
        with contextlib.redirect_stdout(io.StringIO()) as said: status = self.work()
        return status, self.state(entry["id"])["state"], said.getvalue()

    def test_a_failed_exec_holds_and_forgets_the_move(self):
        """An exec that returns never ran the toolkit it named, which later workers may still move to."""
        self.toolkit_branch("later", lambda source: source + "\n# A later toolkit.\n")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        self.commit("a", "a")
        mq.execute = lambda command, env: None
        status, state, said = self.landed("a")
        self.assertEqual((status, state), (1, "queued"))
        self.assertIn("could not start itself again", mq.Queue(self.repo).held())
        self.assertFalse((mq.Queue(self.repo).directory / mq.MOVED).exists())


LAUNCHER_APP = "/Applications/Queue Launcher.app"


def launcher_process(pid, queue, toolkit):
    """A signed queue launcher as its LaunchAgent runs it (Queue.launcher_plist): the launcher's executable with the
    worker's command from toolkit as its arguments, at the queue's main checkout, as ps and lsof show it."""
    return mq.disk.Process(pid=pid, command=mq.LAUNCHER_PROGRAM, cwd=str(queue.main_checkout),
                           args=" ".join([f"{LAUNCHER_APP}/Contents/MacOS/{mq.LAUNCHER_PROGRAM}",
                                          *queue.command(toolkit, if_waiting=True)]))


@contextlib.contextmanager
def under_launcher(processes, verified=True):
    """The host's census is processes and a launcher app verifies as signed when verified; yields the apps checked."""
    checked = []
    def signed(app):
        checked.append(app)
        return verified
    with patch.object(mq.disk, "census", return_value=processes), patch.object(mq, "signed_launcher", signed):
        yield checked


class SharedCheckout(Fixture):
    """The toolkit's own queue keeping its main checkout, the shared checkout, at origin/main
    (vocaro-engineering#104). The fixture's clone stands for that checkout."""
    def setUp(self):
        super().setUp()
        for patcher in (patch.object(mq, "TOOLKIT_ROOT", self.repo), patch.object(mq, "FOLLOW_LIMIT", 0)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def held(self, command, cwd):
        process = subprocess.Popen(command, cwd=cwd)
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        return process

    def python(self, cwd):
        """A Python left running in cwd, which may import the toolkit from it as `python3 -c` does."""
        return self.held([sys.executable, "-c", "import time; time.sleep(300)"], cwd)

    def land(self, prepare=lambda: None):
        """Land one commit while the shared checkout is on main at the old origin/main, as prepare leaves it,
        and return (its HEAD before, after, origin/main, what the worker said)."""
        self.landings = getattr(self, "landings", 0) + 1
        name = f"landed-{self.landings}"
        self.commit(name, name)
        self.add(name)
        run(self.repo, "git", "checkout", "-q", "-B", "main", "origin/main")
        prepare()
        before = run(self.repo, "git", "rev-parse", "HEAD")
        with contextlib.redirect_stdout(io.StringIO()) as said:
            self.assertEqual(self.work(), 0)
        return before, run(self.repo, "git", "rev-parse", "HEAD"), run(self.repo, "git", "rev-parse", "origin/main"), \
            said.getvalue()

    def test_a_clean_shared_checkout_on_main_follows_each_landing(self):
        # A session working in a worktree inside it does not hold it back: that is not its tree.
        nested = self.repo / ".claude/worktrees/agent"
        run(self.repo, "git", "worktree", "add", "-q", "--detach", str(nested), "origin/main")
        (self.repo / ".git/info/exclude").write_text(".claude/worktrees/\n")
        held = subprocess.Popen(["sleep", "300"], cwd=nested)
        self.addCleanup(held.wait)
        self.addCleanup(held.kill)
        before, after, main, said = self.land()
        self.assertNotEqual(before, main)
        self.assertEqual(after, main)
        self.assertIn(f"fast-forwarded {self.repo} from {before[:12]} to {main[:12]}", said)
        self.assertTrue((self.repo / "landed-1").is_file())

    def test_a_shared_checkout_that_is_not_plainly_behind_is_left_as_it_is(self):
        def edit(): (self.repo / "AGENTS.md").write_text("a session's edit\n")
        def untracked(): (self.repo / "notes.txt").write_text("a session's file\n")
        def other_branch(): run(self.repo, "git", "checkout", "-q", "-B", "elsewhere", "origin/main")
        def own_commit():
            (self.repo / "own").write_text("x")
            run(self.repo, "git", "add", "own")
            run(self.repo, "git", "commit", "-qm", "A commit of its own")
        cases = {"changed": (edit, "changed or untracked path(s), such as AGENTS.md"),
                 "untracked": (untracked, "changed or untracked path(s), such as notes.txt"),
                 "branch": (other_branch, "it is on elsewhere, not main"),
                 "commit": (own_commit, "has commits origin/main lacks")}
        for name, (prepare, why) in cases.items():
            with self.subTest(name):
                run(self.repo, "git", "checkout", "-q", "-f", "-B", "main", "origin/main")
                run(self.repo, "git", "clean", "-fdq")
                before, after, main, said = self.land(prepare)
                self.assertNotEqual(after, main)
                self.assertEqual(after, before)
                self.assertIn(f"left {self.repo} as it is: ", said)
                self.assertIn(why, said)
                if name == "changed": self.assertEqual((self.repo / "AGENTS.md").read_text(), "a session's edit\n")
                if name == "untracked": self.assertTrue((self.repo / "notes.txt").is_file())

    def test_a_commit_made_in_the_shared_checkout_and_landed_does_not_hold_it_back(self):
        # A session commits in the shared checkout and lands from it; the queue lands the change under
        # another SHA, so the checkout's main is beside origin/main, not behind it (2026-11-06).
        def made_here():
            run(self.repo, "git", "cherry-pick", f"landed-{self.landings}")
            run(self.repo, "git", "commit", "-q", "--amend", "-m", "The same change, as the session made it")
        before, after, main, said = self.land(made_here)
        self.assertNotEqual(before, main)
        self.assertNotEqual(subprocess.run(["git", "merge-base", "--is-ancestor", before, main], cwd=self.repo).returncode, 0,
                            "not plainly behind: a fast-forward alone could not move it")
        self.assertEqual(after, main)
        self.assertIn(f"moved {self.repo} from {before[:12]} to {main[:12]}, past 1 commit(s) of its own "
                      "that origin/main already holds by their changes", said)
        self.assertEqual(run(self.repo, "git", "status", "--porcelain"), "")

    def test_a_landed_commit_beside_one_that_did_not_land_still_holds_it_back(self):
        # The negative control: one commit of its own that origin/main does not hold keeps it as it is.
        def made_here():
            run(self.repo, "git", "cherry-pick", f"landed-{self.landings}")
            run(self.repo, "git", "commit", "-q", "--amend", "-m", "The same change, as the session made it")
            (self.repo / "own").write_text("x")
            run(self.repo, "git", "add", "own")
            run(self.repo, "git", "commit", "-qm", "A commit of its own")
        before, after, main, said = self.land(made_here)
        self.assertEqual(after, before)
        self.assertIn(f"left {self.repo} as it is: its HEAD {before[:12]} has commits origin/main lacks, whose "
                      "changes it does not hold either: 1, such as", said)
        self.assertTrue((self.repo / "own").is_file())

    def test_a_process_started_from_the_shared_checkout_holds_it_back(self):
        def started(): self.runner = self.python(self.repo / "scripts")
        before, after, main, said = self.land(started)
        self.assertEqual(after, before)
        self.assertRegex(said, rf"a process started from it may still run its toolkit: PID {self.runner.pid} "
                               rf"\([\w.]+\), working directory \S*/scripts;")
        # A census that fails says nothing of what runs there, so it holds the checkout back too.
        self.runner.kill()
        self.runner.wait()
        with patch.object(mq.disk, "census", side_effect=RuntimeError("could not list processes")):
            before, after, main, said = self.land()
        self.assertEqual(after, before)
        self.assertIn("whether a process started from it still runs is unknown", said)
        # The positive control: with the process gone, the next landing moves it.
        before, after, main, said = self.land()
        self.assertEqual(after, main)

    def test_a_session_that_only_names_the_checkout_does_not_hold_it_back(self):
        """vocaro-engineering#182: a Claude session in a worktree inside it passes the checkout itself as its project
        root, and its MCP servers start at that root; neither runs the toolkit from its tree."""
        nested = self.repo / ".claude/worktrees/agent"
        run(self.repo, "git", "worktree", "add", "-q", "--detach", str(nested), "origin/main")
        (self.repo / ".git/info/exclude").write_text(".claude/worktrees/\n")
        def started():
            self.held(["sh", "-c", "sleep 300; exit 0", "--project-config-root", str(self.repo)], nested)
            self.held(["sleep", "300"], self.repo)
        before, after, main, said = self.land(started)
        self.assertEqual(after, main)
        self.assertIn(f"fast-forwarded {self.repo} from {before[:12]} to {main[:12]}", said)

    def test_a_program_only_left_in_the_checkout_does_not_hold_it_back(self):
        """vocaro-engineering#384: a session's shell left in src/python, or a tool a run started there, loads nothing
        of the tree by its working directory alone; only Python puts that directory on its import path."""
        def left():
            self.held(["sleep", "300"], self.repo / "scripts")
            self.held(["sh", "-c", "sleep 300; exit 0"], self.repo / "scripts")
        before, after, main, said = self.land(left)
        self.assertEqual(after, main)
        self.assertIn(f"fast-forwarded {self.repo} from {before[:12]} to {main[:12]}", said)
        # The negative controls: such a program that names a file there by a relative argument, and a Python left
        # there, still hold it back, each by what ties it.
        cases = {"relative argument": (lambda: self.held(["sh", "-c", "sleep 300; exit 0", "eng"], self.repo / "scripts"),
                                       r"argument \S*/scripts/eng"),
                 "python": (lambda: self.python(self.repo / "scripts"), r"working directory \S*/scripts")}
        for name, (start, how) in cases.items():
            with self.subTest(name):
                started = []
                before, after, main, said = self.land(lambda: started.append(start()))
                self.assertEqual(after, before)
                self.assertRegex(said, rf"may still run its toolkit: PID {started[0].pid} \([\w.]+\), {how}")
                started[0].kill()
                started[0].wait()

    def test_a_toolkit_run_from_the_checkouts_root_holds_it_back_and_is_named(self):
        def started(): self.runner = self.held(["sh", "-c", "sleep 300; exit 0", "scripts/eng"], self.repo)
        before, after, main, said = self.land(started)
        self.assertEqual(after, before)
        self.assertRegex(said, rf"may still run its toolkit: PID {self.runner.pid} \(\w+\), argument \S*/scripts/eng; "
                               "tried again once nothing waits")

    def test_a_checkout_left_for_a_run_from_it_follows_once_the_run_ends(self):
        tries = []
        def ended(seconds):
            tries.append(seconds)
            if len(tries) == 2: self.runner.kill()
        def started(): self.runner = self.python(self.repo / "scripts")
        with patch.object(mq, "FOLLOW_LIMIT", None), patch.object(mq, "pause", ended):
            before, after, main, said = self.land(started)
        self.assertEqual(after, main)
        self.assertEqual(tries, [mq.FOLLOW_POLL] * 2)
        self.assertIn(f"left {self.repo} as it is: {mq.IN_USE}", said)
        self.assertIn(f"fast-forwarded {self.repo} from {before[:12]} to {main[:12]}", said)
        # The negative controls: a checkout left on another branch is a person's, so nothing tries it again, and a
        # test's bound on the tries leaves it behind, saying so.
        tries.clear()
        def elsewhere(): run(self.repo, "git", "checkout", "-q", "-B", "elsewhere", "origin/main")
        with patch.object(mq, "FOLLOW_LIMIT", None), patch.object(mq, "pause", ended):
            before, after, main, said = self.land(elsewhere)
        self.assertEqual((after, tries), (before, []))
        run(self.repo, "git", "checkout", "-q", "main")
        with patch.object(mq, "FOLLOW_LIMIT", 0.5), patch.object(mq, "pause", lambda seconds: time.sleep(0.3)):
            before, after, main, said = self.land(started)
        self.assertEqual(after, before)
        self.assertIn(f"left {self.repo} behind after 0.5s of tries: {mq.IN_USE}", said)

    def held_back(self, during):
        """Land one commit while a Python runs from the checkout, as the census shows it, with no bound on the
        tries; during(tries, holders) runs at each try's pause. Returns (before, after, origin/main, said, tries)."""
        holders = [mq.disk.Process(pid=42, command="Python", cwd=str(self.repo / "scripts"))]
        tries = []
        def paused(seconds):
            tries.append(seconds)
            during(len(tries), holders)
        with patch.object(mq.disk, "census", return_value=holders), patch.object(mq, "FOLLOW_LIMIT", None), \
                patch.object(mq, "pause", paused):
            before, after, main, said = self.land()
        return before, after, main, said, tries

    def test_a_run_that_outlasts_an_hour_still_lets_it_follow_once_it_ends(self):
        """vocaro-engineering#384, owner's decision of 2026-11-11: the tries go on until the checkout moves, past
        the hour they once stopped at, saying the deferral once while the same run holds it and again when another
        does, and the round's account says what the worker waits for."""
        hour = 3600 // mq.FOLLOW_POLL
        waits = []
        def during(tried, holders):
            if tried == 1: waits.append(mq.Queue(self.repo).round_state().get("waiting"))
            if tried == hour: holders[:] = [mq.disk.Process(pid=43, command="Python", cwd=str(self.repo / "scripts"))]
            if tried == hour + 2: holders.clear()
        before, after, main, said, tries = self.held_back(during)
        self.assertEqual(after, main)
        self.assertEqual(len(tries), hour + 2)
        self.assertGreater(sum(tries), 3600)
        self.assertIn(f"fast-forwarded {self.repo} from {before[:12]} to {main[:12]}", said)
        told = f"left {self.repo} behind for now, trying again every {mq.FOLLOW_POLL}s until it moves: {mq.IN_USE}: "
        self.assertEqual(said.count(told), 2)
        self.assertIn(told + "PID 42 (Python)", said)
        self.assertIn(told + "PID 43 (Python)", said)
        self.assertEqual(waits, ["the shared checkout to follow origin/main once nothing runs from it"])
        self.assertIsNone(mq.Queue(self.repo).round_state().get("waiting"))
        self.assertNotIn("behind after", said)

    def test_a_hold_during_the_tries_ends_them(self):
        def during(tried, holders):
            if tried == 2: mq.Queue(self.repo).hold("for a test")
        before, after, main, said, tries = self.held_back(during)
        self.assertEqual((after, len(tries)), (before, 2))
        self.assertIn(f"stops trying to move {self.repo}: the queue is held", said)

    def test_a_checkout_left_for_another_reason_during_the_tries_ends_them(self):
        """The negative control: a run holds it back, then a session's edit makes it a person's, so the tries stop
        rather than go on forever, and the log says why it was left."""
        def during(tried, holders):
            if tried == 2:
                holders.clear()
                (self.repo / "AGENTS.md").write_text("a session's edit\n")
            self.assertLess(tried, 3, "a dirty checkout should not be tried again")
        before, after, main, said, tries = self.held_back(during)
        self.assertEqual((after, len(tries)), (before, 2))
        self.assertIn(f"left {self.repo} as it is: it has 1 changed or untracked path(s), such as AGENTS.md", said)
        self.assertEqual((self.repo / "AGENTS.md").read_text(), "a session's edit\n")

    def test_a_landing_that_arrives_during_the_tries_ends_them(self):
        # Keep the checkout in use until the test releases it, independent of the host census.
        holders = [mq.disk.Process(pid=42, command="Python", cwd=str(self.repo / "scripts"))]
        tries = []
        def arrived(seconds):
            tries.append(seconds)
            if len(tries) == 1:
                self.commit("later", "later")
                self.add("later")
                run(self.repo, "git", "checkout", "-q", "main")
                return
            self.assertEqual(self.gates(), 2, "the queued entry should gate before the worker retries")
            if len(tries) == 3:
                holders.clear()
        with patch.object(mq.disk, "census", return_value=holders), \
                patch.object(mq, "FOLLOW_LIMIT", None), patch.object(mq, "pause", arrived):
            _, after, main, said = self.land()
        # The arrival ended the first tries, the second round landed it, and the tries after it caught up.
        self.assertIn("later", self.main_files())
        self.assertEqual(tries, [mq.FOLLOW_POLL] * 3)
        self.assertEqual(after, main)
        self.assertEqual(said.count(f"left {self.repo} as it is: {mq.IN_USE}"), 2)
        self.assertIn(f"stops trying to move {self.repo}: an entry waits", said)

    def test_a_signed_launcher_does_not_hold_it_back(self):
        """vocaro-engineering#374, #384: a LaunchAgent's launcher names the shared checkout's src/python in its
        arguments and waits on the worker it spawned; it runs nothing from the checkout, so neither the worker's own
        launcher nor another queue's holds it back, while the worker it started counts by its own arguments."""
        queue = mq.Queue(self.repo)
        own, other = launcher_process(4242, queue, self.repo), launcher_process(4343, queue, self.repo)
        self.assertEqual([how for _, how, _ in mq.disk.runners([own], self.repo)], ["argument"],
                         "the fixture's launcher is one the census counts by its arguments")
        with under_launcher([own, other]) as checked:
            before, after, main, said = self.land()
        self.assertEqual(after, main)
        self.assertIn(f"fast-forwarded {self.repo} from {before[:12]} to {main[:12]}", said)
        self.assertIn(LAUNCHER_APP, checked)
        # The negative controls: an unsigned launcher, a worker still running from the checkout that a launcher
        # started, and a process that runs from the checkout beside the launchers each still hold it back.
        runner = mq.disk.Process(pid=4444, command="Python", cwd=str(self.repo / "scripts"))
        worker = mq.disk.Process(pid=4545, command="python3", cwd=str(self.repo),
                                 args=" ".join(queue.command(self.repo, if_waiting=True)))
        cases = {"unsigned": ([own], False, f"PID 4242 ({mq.LAUNCHER_PROGRAM}), argument {self.repo}/src/python"),
                 "its worker": ([own, worker], True, f"PID 4545 (python3), argument {self.repo}/src/python"),
                 "a run from it": ([own, other, runner], True, "PID 4444 (Python), working directory")}
        for name, (processes, verified, why) in cases.items():
            with self.subTest(name), under_launcher(processes, verified):
                before, after, main, said = self.land()
                self.assertEqual(after, before)
                self.assertIn(f"left {self.repo} as it is: {mq.IN_USE}: ", said)
                self.assertIn(why, said)
                if verified: self.assertNotIn(f"({mq.LAUNCHER_PROGRAM})", said)

    def test_a_projects_queue_leaves_the_toolkits_checkout_alone(self):
        with patch.object(mq, "TOOLKIT_ROOT", ROOT):
            before, after, main, said = self.land()
        self.assertEqual(after, before)
        self.assertNotIn("fast-forwarded", said)
        self.assertNotIn("as it is:", said)


# What git prints, over HTTPS and SSH, when it did not reach GitHub.
UNREACHED = {
    "https name": "fatal: unable to access 'https://github.com/vocaro/vocaro-engineering.git/': Could not resolve host: "
                  "github.com",
    "ssh name": "ssh: Could not resolve hostname github.com: nodename nor servname provided, or not known\n"
                "fatal: Could not read from remote repository.\n\nPlease make sure you have the correct access rights",
    "https refused": "fatal: unable to access 'https://github.com/x/y.git/': Failed to connect to github.com port 443 "
                     "after 2 ms: Couldn't connect to server",
    "ssh refused": "ssh: connect to host github.com port 22: Connection refused\nfatal: Could not read from remote "
                   "repository.",
    "https timeout": "fatal: unable to access 'https://github.com/x/y.git/': Failed to connect to github.com port 443 "
                     "after 75003 ms: Operation timed out",
    "ssh timeout": "ssh: connect to host github.com port 22: Operation timed out\nfatal: Could not read from remote "
                   "repository.",
    "https reset": "error: RPC failed; curl 56 Recv failure: Connection reset by peer\nfatal: early EOF",
    "ssh dropped": "Connection closed by 140.82.112.4 port 22\nfatal: the remote end hung up unexpectedly",
}
# What git prints when GitHub answered and refused, which holds at once.
REFUSED = {
    "https authentication": "remote: Invalid username or token.\nfatal: Authentication failed for "
                            "'https://github.com/x/y.git/'",
    "https forbidden": "fatal: unable to access 'https://github.com/x/y.git/': The requested URL returned error: 403",
    "https no prompt": "fatal: could not read Username for 'https://github.com': terminal prompts disabled",
    "ssh key": "git@github.com: Permission denied (publickey).\nfatal: Could not read from remote repository.",
    "ssh host key": "Host key verification failed.\nfatal: Could not read from remote repository.",
    "missing": "ERROR: Repository not found.\nfatal: Could not read from remote repository.",
    "rejected": " ! [remote rejected] HEAD -> main (protected branch hook declined)\nerror: failed to push some refs",
    "not git": "fatal: 'origin' does not appear to be a git repository",
    # A local path with no repository: git adds the line a dropped SSH connection ends with.
    "no repository": "fatal: '/x/gone.git' does not appear to be a git repository\nfatal: Could not read from remote "
                     "repository.\n\nPlease make sure you have the correct access rights\nand the repository exists.",
}


class Unreachable(Fixture):
    """A fetch or push that did not reach the remote is tried again with a backoff, and holds only when it keeps
    failing; one the remote refused holds at once (vocaro-engineering#113)."""
    def test_what_did_not_reach_the_remote_is_told_from_a_refusal(self):
        for name, said in UNREACHED.items():
            with self.subTest(name): self.assertTrue(mq.unreached(said))
        for name, said in REFUSED.items():
            with self.subTest(name): self.assertFalse(mq.unreached(said))

    def test_a_fetch_that_did_not_reach_the_remote_is_tried_again(self):
        for name, said in UNREACHED.items():
            with self.subTest(name):
                self.commit(name.replace(" ", "-"), name.replace(" ", "-"))
                [identifier] = self.add(name.replace(" ", "-"))
                with patch("builtins.print"), self.remote_says(said, {1, 2}) as (fetches, slept): self.work()
                self.assertEqual(self.state(identifier)["state"], "landed")
                self.assertIsNone(mq.Queue(self.repo).held())
                self.assertEqual(slept, list(mq.BACKOFF[:2]))

    def test_a_passed_gate_survives_an_outage_at_the_fetch_before_the_push(self):
        """The case of 2026-11-02: the round's own fetch worked, the push's did not, for a while."""
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.remote_says(UNREACHED["https name"], {2, 3, 4}) as (fetches, slept): self.work()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertIn("a", self.main_files())
        self.assertEqual(self.gates(), 1)
        self.assertEqual(slept, list(mq.BACKOFF[:3]))

    def test_a_push_that_did_not_reach_the_remote_is_pushed_again(self):
        for name in ("https refused", "ssh timeout"):
            with self.subTest(name):
                self.commit(name.replace(" ", "-"), name.replace(" ", "-"))
                [identifier] = self.add(name.replace(" ", "-"))
                with patch("builtins.print"), self.remote_says(UNREACHED[name], {1}, "push") as (pushes, slept):
                    self.work()
                self.assertEqual(self.state(identifier)["state"], "landed")
                self.assertEqual(len(pushes), 2)

    def test_a_remote_that_stays_out_of_reach_holds_after_the_last_backoff_of_the_last_try(self):
        """Each round's fetch gets its backoff, and the round its tries (HoldKinds); then it holds."""
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.remote_says(UNREACHED["https name"], range(1, 999)) as (fetches, slept), patch("builtins.print"):
            self.assertEqual(self.work(), 1)
        self.assertEqual(self.state(identifier)["state"], "queued")
        rounds = len(mq.CLEARING_WAITS) + 1
        self.assertEqual((len(fetches), slept.count(mq.BACKOFF[-1])), (rounds * (len(mq.BACKOFF) + 1), rounds))
        self.assertIn(f"{len(mq.BACKOFF) + 1} times", mq.Queue(self.repo).held())
        self.assertEqual(self.gates(), 0)

    def test_a_fetch_or_push_the_remote_refused_holds_at_once(self):
        """A push refused for its credentials is tried again (PushRefusedInAnIncident); a fetch so refused is not."""
        for command in ("fetch", "push"):
            for name, said in REFUSED.items():
                if command == "push" and mq.AUTHENTICATION.search(said): continue
                with self.subTest(command=command, refusal=name):
                    with contextlib.suppress(OSError): mq.Queue(self.repo).hold_file.unlink()
                    branch = f"{command}-{name}".replace(" ", "-")
                    self.commit(branch, branch)
                    [identifier] = self.add(branch)
                    with patch("builtins.print"), self.remote_says(said, {1}, command) as (calls, slept):
                        self.assertEqual(self.work(), 1)
                    self.assertEqual(self.state(identifier)["state"], "queued" if command == "fetch" else "passed")
                    self.assertEqual((len(calls), slept), (1, []))
                    self.assertIn(said.splitlines()[-1], mq.Queue(self.repo).held())
                    mq.remove(mq.Queue(self.repo), identifier, quiet=True)

    def test_land_tries_its_fetch_again_and_fails_on_a_refusal(self):
        self.commit("a", "a")
        with patch.object(mq.Queue, "start"), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()) as said:
            with self.remote_says(UNREACHED["ssh dropped"], {1}) as (fetches, slept):
                self.assertIsNotNone(mq.add(self.repo, "a"))
            self.assertIn("did not reach the remote", said.getvalue())
            self.assertEqual(slept, [mq.BACKOFF[0]])
            self.commit("b", "b")
            with self.remote_says(REFUSED["ssh key"], {1}) as (fetches, slept):
                with self.assertRaisesRegex(ValueError, "Permission denied"): mq.add(self.repo, "b")
            self.assertEqual((len(fetches), slept), (1, []))


class SignedLauncher(Fixture):
    def arguments(self, *words):
        parser = argparse.ArgumentParser()
        mq.add_arguments(parser)
        return parser.parse_args(words)

    def registered(self, queue):
        directory = self.base / "Library" / "LaunchAgents"
        directory.mkdir(parents=True)
        (directory / f"{queue.launcher_label()}.plist").touch()

    def test_a_registered_launcher_is_started_instead_of_the_session_worker(self):
        self.commit("a", "a")
        self.add("a")
        queue = mq.Queue(self.repo)
        self.registered(queue)
        with patch.object(mq.Path, "home", return_value=self.base), patch.object(mq.sys, "platform", "darwin"), \
                patch.object(mq.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as request, \
                patch.object(mq.subprocess, "Popen") as detached:
            queue.start()
        request.assert_called_once_with([mq.LAUNCHCTL, "kickstart", f"gui/{os.getuid()}/{queue.launcher_label()}"],
                                        capture_output=True, text=True, timeout=10)
        detached.assert_not_called()

    def test_a_rejected_launcher_start_falls_back_and_records_the_reason(self):
        self.commit("a", "a")
        self.add("a")
        queue = mq.Queue(self.repo)
        self.registered(queue)
        refusal = SimpleNamespace(returncode=1, stderr="service is unavailable")
        with patch.object(mq.Path, "home", return_value=self.base), patch.object(mq.sys, "platform", "darwin"), \
                patch.object(mq.subprocess, "run", return_value=refusal), patch.object(mq, "environment", return_value={}), \
                patch.object(mq.subprocess, "Popen") as detached, patch.object(mq.threading, "Thread"):
            queue.start()
        detached.assert_called_once()
        self.assertTrue(detached.call_args.kwargs["start_new_session"])
        self.assertIn("service is unavailable", (queue.directory / "worker.log").read_text())

    def test_launcher_plist_uses_a_stable_checkout_and_an_idle_safe_command(self):
        queue = mq.Queue(self.repo)
        app = self.base / "Queue Launcher.app"
        executable = app / "Contents" / "MacOS" / mq.LAUNCHER_PROGRAM
        executable.parent.mkdir(parents=True)
        executable.touch()
        args = self.arguments("launcher-plist", str(app))
        stable = self.base / "stable-toolkit"
        output = io.StringIO()
        with patch.object(mq.sys, "platform", "darwin"), patch.object(mq, "Queue", return_value=queue), \
                patch.object(mq, "text", return_value=str(stable / ".git")), \
                patch.object(mq.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as verify, \
                contextlib.redirect_stdout(output):
            self.assertEqual(mq.main(self.repo, args), 0)
        verify.assert_called_once_with(["/usr/bin/codesign", "--verify", "--strict",
                                        f'-R=anchor apple generic and identifier "{mq.LAUNCHER_BUNDLE}"', str(app)],
                                       capture_output=True, text=True, timeout=10)
        agent = plistlib.loads(output.getvalue().encode())
        self.assertEqual(agent["Label"], queue.launcher_label())
        self.assertEqual(agent["AssociatedBundleIdentifiers"], [mq.LAUNCHER_BUNDLE])
        self.assertEqual(agent["ProgramArguments"], [str(executable), *queue.command(stable, if_waiting=True)])
        self.assertTrue(agent["RunAtLoad"])
        self.assertEqual(agent["StartInterval"], mq.LAUNCHER_INTERVAL)

    def test_agent_entry_does_nothing_for_an_empty_held_or_running_queue(self):
        queue = mq.Queue(self.repo)
        args = self.arguments("run", "--if-waiting")
        with patch.object(mq.remote, "stop_dead_dispatches") as recover:
            self.assertEqual(mq.main(self.repo, args), 0)
            self.commit("a", "a")
            self.add("a")
            queue.hold_file.write_text("held for this test")
            self.assertEqual(mq.main(self.repo, args), 0)
            self.assertEqual(queue.hold_file.read_text(), "held for this test")
            queue.hold_file.unlink()
            with queue.worker() as free:
                self.assertTrue(free)
                self.assertEqual(mq.main(self.repo, args), 0)
        recover.assert_not_called()
        self.assertEqual(self.gates(), 0)

    def test_agent_entry_drains_a_waiting_queue(self):
        self.commit("a", "a")
        identifier, = self.add("a")
        args = self.arguments("run", "--if-waiting")
        self.assertEqual(mq.main(self.repo, args), 0)
        self.assertEqual(self.state(identifier)["state"], mq.LANDED)
        self.assertIn("a", self.main_files())


class LostLocalNetwork(Fixture):
    """A worker whose process macOS denies the local network, as once the session that started it ends, exits before a
    round, holding nothing, rather than gate everything on this Mac (vocaro-engineering#282)."""
    LOST = "macOS holds itself responsible for this process (PID 1), a Python with no app of its own"

    def test_a_worker_that_lost_the_local_network_exits_holding_nothing_before_a_gate(self):
        self.commit("a", "a")
        [identifier] = self.add("a")
        with patch.object(mq.remote, "configured", return_value="fake-mac"), \
                patch.object(mq.remote, "local_network_lost", return_value=self.LOST), \
                contextlib.redirect_stdout(io.StringIO()) as said:
            self.assertEqual(self.work(), 0)
        self.assertEqual(self.state(identifier)["state"], "queued")
        self.assertIsNone(mq.Queue(self.repo).held())
        self.assertEqual(self.gates(), 0)
        self.assertIn("exits, holding nothing, since this worker cannot reach the second Mac: " + self.LOST,
                      said.getvalue())
        self.assertFalse(mq.Queue(self.repo).working())
        # The next worker, a live session's, lands it.
        with patch.object(mq.remote, "configured", return_value="fake-mac"), \
                patch.object(mq.remote, "local_network_lost", return_value=None), patch("builtins.print"):
            self.assertEqual(self.work(), 0)
        self.assertEqual(self.state(identifier)["state"], "landed")

    @contextlib.contextmanager
    def losing_access_at_the_gate(self, status):
        """Access is there at the round's check and gone once the gate's lane ran: the lane's eng run, whose
        environment is kept, exits status without running anything, as a real one that found NoLocalNetwork does
        (cli's run; test_remote's RemoteTests). Yields the environments of those runs."""
        real, ran = mq.processes.run, []
        def lane(command, *arguments, **options):
            if command[:3] != ["python3", "scripts/eng", "run"]: return real(command, *arguments, **options)
            ran.append(options["env"])
            return status
        with patch.object(mq.remote, "configured", return_value="fake-mac"), \
                patch.object(mq.remote, "local_network_lost", side_effect=lambda: self.LOST if ran else None), \
                patch.object(mq.processes, "run", lane):
            yield ran

    def test_a_gate_whose_lane_lost_the_local_network_exits_holding_nothing_and_counts_no_run(self):
        """vocaro-engineering#300: as before a round (#282), not a no-verdict run toward queue.noVerdictRuns."""
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.losing_access_at_the_gate(mq.remote.LOST_ACCESS) as ran, \
                contextlib.redirect_stdout(io.StringIO()) as said:
            self.assertEqual(self.work(), 0)
        self.assertEqual(len(ran), 1)
        self.assertEqual(ran[0][mq.remote.QUEUE_GATE], "1")
        self.assertEqual(self.state(identifier)["state"], "queued")
        self.assertIsNone(mq.Queue(self.repo).held())
        self.assertEqual(self.gates(), 0)
        self.assertIn("exits, holding nothing, since this worker cannot reach the second Mac: " + self.LOST, said.getvalue())
        self.assertNotIn("of at most", said.getvalue())
        self.assertFalse(mq.Queue(self.repo).working())
        # The next worker, a live session's, lands it.
        with patch.object(mq.remote, "configured", return_value="fake-mac"), \
                patch.object(mq.remote, "local_network_lost", return_value=None), patch("builtins.print"):
            self.assertEqual(self.work(), 0)
        self.assertEqual(self.state(identifier)["state"], "landed")

    def test_a_worker_with_no_second_mac_never_asks(self):
        self.commit("a", "a")
        [identifier] = self.add("a")
        with patch.object(mq.remote, "local_network_lost", return_value=self.LOST) as asked, patch("builtins.print"):
            self.assertEqual(self.work(), 0)
        self.assertEqual(self.state(identifier)["state"], "landed")
        asked.assert_not_called()


class LandedCommits(Fixture):
    """Each entry records and prints its own commit on main, not the batch's tip (vocaro-engineering#184)."""

    def on_main(self, subject):
        run(self.repo, "git", "fetch", "-q", "origin")
        return run(self.repo, "git", "log", "--format=%H", "-1", f"--grep=^{subject}$", "origin/main")

    def tip(self):
        run(self.repo, "git", "fetch", "-q", "origin")
        return run(self.repo, "git", "rev-parse", "origin/main")

    def waited(self, identifier):
        with contextlib.redirect_stdout(io.StringIO()) as said:
            self.assertEqual(mq.wait(self.repo, identifier), 0)
        return said.getvalue()

    def test_a_single_entry_is_the_tip(self):
        self.commit("a", "a")
        [identifier] = self.add("a")
        self.work()
        entry, tip = self.state(identifier), self.tip()
        self.assertEqual((entry["landedAs"], entry["landedTip"]), (tip, tip))
        self.assertEqual(entry["landedCommits"], 1)
        said = self.waited(identifier)
        self.assertEqual(said.rsplit("landed: ", 1)[1].strip(), tip)
        self.assertIn(f"-> {tip[:12]}\n", said)
        self.assertNotIn("batch tip", said)
        self.assertNotIn("commits)", said)

    def test_an_entry_below_the_tip_names_its_own_commit_and_the_tip(self):
        self.commit("a", "a"); self.commit("b", "b"); self.commit("c", "c")
        first, middle, last = self.add("a", "b", "c")
        self.work()
        tip = self.tip()
        for identifier, subject in ((first, "Add a"), (middle, "Add b"), (last, "Add c")):
            entry = self.state(identifier)
            self.assertEqual(entry["landedAs"], self.on_main(subject))
            self.assertEqual(entry["landedTip"], tip)
        own = self.state(first)["landedAs"]
        self.assertNotEqual(own, tip)  # the bug: every entry named the tip
        said = self.waited(first)
        self.assertEqual(said.rsplit("landed: ", 1)[1].strip(), own)
        self.assertIn(f"-> {own[:12]}, in batch tip {tip[:12]}", said)
        # The control: the entry whose commit is the tip says nothing of a batch tip.
        said = self.waited(last)
        self.assertEqual(said.rsplit("landed: ", 1)[1].strip(), tip)
        self.assertNotIn("batch tip", said)

    def test_a_multi_commit_entry_names_its_last_commit_and_counts_them(self):
        self.commit("m1", "multi")
        for name in ("m2", "m3"):
            (self.repo / name).write_text("x")
            run(self.repo, "git", "add", name)
            run(self.repo, "git", "commit", "-qm", f"Add {name}")
        self.commit("z", "z")
        multi, single = self.add("multi", "z")
        self.work()
        tip, entry = self.tip(), self.state(multi)
        self.assertEqual(entry["landedAs"], self.on_main("Add m3"))
        self.assertEqual(entry["landedCommits"], 3)
        self.assertEqual(run(self.repo, "git", "rev-list", "--count", f"{self.on_main('Add m1')}^..{entry['landedAs']}"),
                         "3")
        said = self.waited(multi)
        self.assertIn(f"-> {entry['landedAs'][:12]} (3 commits), in batch tip {tip[:12]}", said)
        self.assertEqual(said.rsplit("landed: ", 1)[1].strip(), entry["landedAs"])
        # The control: a one-commit entry gives no count.
        self.assertEqual(self.state(single)["landedAs"], tip)
        self.assertNotIn("commits)", self.waited(single))

    def test_commits_main_already_had_leave_the_tip(self):
        worker = mq.Worker.__new__(mq.Worker)
        worker.checkout = self.repo
        base = run(self.repo, "git", "rev-parse", "origin/main")
        tip = self.commit("a", "a")
        other = self.commit("b", "b")
        # An entry none of whose commits base..tip holds is left out, and records the tip.
        own = worker.landed([{"id": "a", "commits": [tip]}, {"id": "b", "commits": [other]}], base, tip)
        self.assertEqual(own, {"a": (tip, 1)})


class PassedThroughAHold(Fixture):
    """A batch whose lanes passed keeps that verdict through a hold its push meets, and the next worker pushes
    it without gating the same tree again (vocaro-engineering#150)."""
    def refused(self, *branches):
        """Queue branches and run a worker whose push the remote refuses, as GitHub's SSH did on 2026-11-04."""
        ids = self.add(*branches)
        # Every try, past the retries a refused key has (vocaro-engineering#151), on a remote that is not GitHub's.
        with self.remote_says(REFUSED["ssh key"], range(1, 99), "push"), patch("builtins.print"):
            self.assertEqual(self.work(), 1)
        return ids

    def resume(self):
        """`eng queue resume` with its worker run here, in the foreground."""
        mq.Queue(self.repo).hold_file.unlink()
        with patch("builtins.print"): return self.work()

    def test_a_refused_push_keeps_the_pass_and_resume_pushes_it_without_a_gate(self):
        self.commit("a", "a"); self.commit("b", "b")
        ids = self.refused("a", "b")
        self.assertEqual([self.state(i)["state"] for i in ids], ["passed"] * 2)
        verdict = self.state(ids[0])["passed"]
        self.assertEqual(verdict["batch"], ids)
        self.assertEqual(verdict["base"], run(self.repo, "git", "rev-parse", "origin/main"))
        held = mq.Queue(self.repo).held()
        self.assertIn("Permission denied (publickey)", held)
        self.assertIn(f"passed {verdict['head'][:12]} on {verdict['base'][:12]} (2 entries)", held)
        self.assertIn("lands it without gating it again", mq.describe(self.state(ids[0])))
        self.assertEqual(self.gates(), 1)
        self.assertEqual(self.resume(), 0)
        self.assertEqual([self.state(i)["state"] for i in ids], ["landed"] * 2)
        self.assertEqual(self.gates(), 1)
        # The batch's tip is the commit that passed; each entry, pushed by a worker that picked nothing, its own (#184).
        self.assertEqual([self.state(i)["landedTip"] for i in ids], [verdict["head"]] * 2)
        self.assertEqual(self.state(ids[1])["landedAs"], verdict["head"])
        self.assertEqual(self.state(ids[0])["landedAs"], run(self.repo, "git", "rev-parse", f"{verdict['head']}^"))
        self.assertLessEqual({"a", "b"}, self.main_files())
        # The landed entries still record the gate that passed them, for its budget.
        self.assertEqual(self.state(ids[0])["gate"], verdict["gate"])

    def test_without_a_pass_the_next_worker_gates_again(self):
        """Negative control: a batch a worker left testing was never judged, so it is gated again."""
        self.commit("a", "a")
        [identifier] = self.add("a")
        queue = mq.Queue(self.repo)
        queue.mark([queue.read(identifier)], "testing")
        with patch("builtins.print"): self.assertEqual(self.work(), 0)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(self.gates(), 1)

    def test_main_moving_by_files_no_lane_reads_carries_the_pass_over(self):
        self.commit("a", "a")
        [identifier] = self.refused("a")
        self.commit("records/note", "other")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        self.assertEqual(self.resume(), 0)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(self.gates(), 1)
        self.assertLessEqual({"a", "records"}, self.main_files())

    def test_main_moving_by_files_a_lane_reads_gates_again(self):
        self.commit("a", "a")
        [identifier] = self.refused("a")
        first = self.state(identifier)["passed"]
        self.commit("other", "other")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        self.assertEqual(self.resume(), 0)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(self.gates(), 2)
        # What landed is the pass of the second gate, on the main that moved.
        self.assertNotEqual(self.state(identifier)["passed"]["base"], first["base"])
        self.assertEqual(self.state(identifier)["landedAs"], self.state(identifier)["passed"]["head"])

    def test_an_entry_removed_during_the_hold_gates_what_is_left(self):
        self.commit("a", "a"); self.commit("b", "b")
        ids = self.refused("a", "b")
        with patch("builtins.print"): mq.remove(mq.Queue(self.repo), ids[0])
        self.assertEqual(self.resume(), 0)
        self.assertEqual(self.state(ids[1])["state"], "landed")
        self.assertEqual(self.gates(), 2)
        self.assertNotIn("a", self.main_files())

    def test_a_branch_added_again_during_the_hold_gates_again(self):
        self.commit("a", "a")
        [first] = self.refused("a")
        self.commit("a2", "a", start="a")
        [second] = self.add("a")
        self.assertEqual(self.state(first)["state"], "removed")
        self.assertEqual(self.resume(), 0)
        self.assertEqual(self.state(second)["state"], "landed")
        self.assertEqual(self.gates(), 2)
        self.assertLessEqual({"a", "a2"}, self.main_files())

    def test_an_entry_added_during_the_hold_waits_for_the_next_round(self):
        self.commit("a", "a")
        [first] = self.refused("a")
        self.commit("b", "b")
        [second] = self.add("b")
        self.assertEqual(self.resume(), 0)
        self.assertEqual([self.state(first)["state"], self.state(second)["state"]], ["landed", "landed"])
        # One gate for a, before the hold, and one for b after it.
        self.assertEqual(self.gates(), 2)
        self.assertEqual(self.state(first)["landedAs"], self.state(first)["passed"]["head"])

    def test_a_push_that_went_through_before_the_worker_died_lands_as_it_is(self):
        self.commit("a", "a")
        [identifier] = self.refused("a")
        head = self.state(identifier)["passed"]["head"]
        run(self.repo, "git", "push", "-q", "--no-verify", "origin", f"{head}:refs/heads/main")
        self.assertEqual(self.resume(), 0)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(self.gates(), 1)
        self.assertEqual(run(self.repo, "git", "rev-parse", "origin/main"), head)

    def test_a_commit_that_is_gone_gates_again(self):
        self.commit("a", "a")
        [identifier] = self.refused("a")
        queue = mq.Queue(self.repo)
        entry = queue.read(identifier)
        entry["passed"]["head"] = "0" * 40
        queue.save(entry)
        self.assertEqual(self.resume(), 0)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(self.gates(), 2)


class PushRefusedInAnIncident(Fixture):
    """A push refused for its credentials, seconds after a fetch from the same URL went through, is tried again:
    after each of BACKOFF, then, on GitHub, while its status says Git Operations is not operational, for up to
    SETTLE_LIMIT. GitHub's SSH refused the owner's key so for over an hour on 2026-11-04 (vocaro-engineering#151)."""
    @contextlib.contextmanager
    def incident(self, pushes, statuses=None, github=True):
        """The numbered pushes refused for the key, GitHub's Git Operations as statuses says at each look (the last
        repeating; None for no reading), and a clock that each pause moves. Yields the pushes, the pauses and what
        the worker said."""
        clock, slept, said = [1000.0], [], io.StringIO()
        def pause(seconds):
            slept.append(seconds)
            clock[0] += seconds
        looks = list(statuses or [("operational", None)])
        def status():
            self.looks += 1
            return looks[min(self.looks, len(looks)) - 1]
        self.looks = 0
        with self.remote_says(REFUSED["ssh key"], pushes, "push") as (calls, _), patch.object(mq, "pause", pause), \
                patch.object(mq, "now", lambda: clock[0]), patch.object(mq, "on_github", return_value=github), \
                patch.object(mq, "git_operations", side_effect=status), contextlib.redirect_stdout(said):
            yield calls, slept, said

    def test_a_refusal_that_clears_within_the_backoff_lands_without_another_gate(self):
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.incident({1, 2}) as (pushes, slept, said): self.assertEqual(self.work(), 0)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual((len(pushes), slept, self.gates(), self.looks), (3, list(mq.BACKOFF[:2]), 1, 0))
        self.assertIn("the push was refused as the fetch before it was not, so trying again in 10s", said.getvalue())

    def test_a_refusal_that_lasts_while_github_is_degraded_is_waited_out(self):
        self.commit("a", "a")
        [identifier] = self.add("a")
        tries = len(mq.BACKOFF) + 4
        with self.incident(range(1, tries), [("partial_outage", None)]) as (pushes, slept, said):
            self.assertEqual(self.work(), 0)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(slept, list(mq.BACKOFF) + [mq.REFUSAL_POLL] * 3)
        self.assertEqual((len(pushes), self.gates()), (tries, 1))
        self.assertIn("while GitHub's Git Operations is partial_outage", said.getvalue())

    def test_a_refusal_while_github_is_operational_holds_after_the_backoff_and_says_so(self):
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.incident(range(1, 99)) as (pushes, slept, _): self.assertEqual(self.work(), 1)
        self.assertEqual((len(pushes), slept, self.looks), (len(mq.BACKOFF) + 1, list(mq.BACKOFF), 1))
        held = mq.Queue(self.repo).held()
        self.assertIn("was refused: git@github.com: Permission denied (publickey).", held)
        self.assertIn("GitHub's Git Operations is operational", held)
        # The batch's pass stays for resume (vocaro-engineering#150).
        self.assertEqual(self.state(identifier)["state"], "passed")

    def test_a_refusal_that_outlasts_the_limit_holds_naming_githubs_status(self):
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.incident(range(1, 999), [("major_outage", None)]) as (pushes, slept, _):
            self.assertEqual(self.work(), 1)
        self.assertEqual(sum(slept), mq.SETTLE_LIMIT)
        self.assertIn(f"GitHub's Git Operations was still major_outage after {mq.SETTLE_LIMIT}s of tries",
                      mq.Queue(self.repo).held())
        self.assertEqual(self.state(identifier)["state"], "passed")
        self.assertEqual(self.gates(), 1)

    def test_an_unreadable_status_holds_after_the_backoff_and_says_why(self):
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.incident(range(1, 99), [(None, "GitHub's status could not be read: timed out")]) as (pushes, *_):
            self.assertEqual(self.work(), 1)
        self.assertEqual(len(pushes), len(mq.BACKOFF) + 1)
        self.assertIn("GitHub's status could not be read: timed out", mq.Queue(self.repo).held())

    def test_a_remote_that_is_not_githubs_holds_after_the_backoff_without_asking_github(self):
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.incident(range(1, 99), github=False) as (pushes, slept, _): self.assertEqual(self.work(), 1)
        self.assertEqual((len(pushes), slept, self.looks), (len(mq.BACKOFF) + 1, list(mq.BACKOFF), 0))
        self.assertNotIn("Git Operations", mq.Queue(self.repo).held())

    def test_a_push_url_other_than_the_fetchs_holds_at_once(self):
        """The fetch that went through says nothing of credentials it did not use."""
        run(self.repo, "git", "config", "remote.origin.pushurl", f"file://{self.origin}")
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.incident(range(1, 99)) as (pushes, slept, _): self.assertEqual(self.work(), 1)
        self.assertEqual((len(pushes), slept, self.looks), (1, [], 0))

    def test_a_refusal_of_another_kind_holds_at_once(self):
        """Negative control: a missing repository or a rejected push is no incident's."""
        for name in ("missing", "rejected", "ssh host key"):
            with self.subTest(name):
                with contextlib.suppress(OSError): mq.Queue(self.repo).hold_file.unlink()
                self.commit(name.replace(" ", "-"), name.replace(" ", "-"))
                [identifier] = self.add(name.replace(" ", "-"))
                with patch("builtins.print"), self.remote_says(REFUSED[name], range(1, 99), "push") as (pushes, slept), \
                        patch.object(mq, "git_operations") as status:
                    self.assertEqual(self.work(), 1)
                self.assertEqual((len(pushes), slept), (1, []))
                status.assert_not_called()
                mq.remove(mq.Queue(self.repo), identifier, quiet=True)

    def test_main_moving_during_the_wait_is_carried_over_as_any_rejection(self):
        self.commit("a", "a")
        [identifier] = self.add("a")
        moved = []
        real = mq.git
        def git(directory, *arguments, **options):
            # The retry, the first push to get past the refusal: main moves first, by a path no lane reads, so
            # the push is rejected, not refused.
            if arguments[:1] == ("push",) and not moved and self.pushes == 0:
                moved.append(self.commit("records/note", "other"))
                run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
            if arguments[:1] == ("push",): self.pushes += 1
            return real(directory, *arguments, **options)
        self.pushes = 0
        with patch.object(mq, "git", git), self.incident({1}) as (pushes, slept, _): self.assertEqual(self.work(), 0)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertLessEqual({"a", "records"}, self.main_files())
        self.assertEqual((self.gates(), len(moved)), (1, 1))

    def test_githubs_status_is_read_from_its_components(self):
        def answer(body):
            response = io.BytesIO(json.dumps(body).encode() if not isinstance(body, bytes) else body)
            return patch.object(mq.urllib.request, "urlopen", return_value=response)
        with answer({"components": [{"name": "API Requests", "status": "operational"},
                                    {"name": "Git Operations", "status": "partial_outage"}]}):
            self.assertEqual(mq.git_operations(), ("partial_outage", None))
        with answer({"components": [{"name": "API Requests", "status": "operational"}]}):
            self.assertEqual(mq.git_operations(), (None, "GitHub's status names no Git Operations"))
        with answer(b"<html>"):
            self.assertIn("could not be read", mq.git_operations()[1])
        with patch.object(mq.urllib.request, "urlopen", side_effect=OSError("timed out")):
            self.assertEqual(mq.git_operations(), (None, "GitHub's status could not be read: timed out"))

    def test_githubs_urls_are_told_from_others(self):
        for url in ("git@github.com:vocaro/x.git", "ssh://git@github.com/vocaro/x.git", "https://github.com/vocaro/x.git",
                    "https://token@github.com/vocaro/x.git"):
            with self.subTest(url): self.assertTrue(mq.on_github(url))
        for url in (str(self.origin), "https://github.com.example/x.git", "git@gitlab.com:x/y.git"):
            with self.subTest(url): self.assertFalse(mq.on_github(url))


class Refusals(Fixture):
    def test_pull_request_projects_are_refused(self):
        self.publish({"schemaVersion": 1, "name": "x", "integration": "pull-request",
                      "lanes": {"gate": {"command": ["true"]}}})
        with self.assertRaisesRegex(ValueError, "pull request"): mq.add(self.repo, "HEAD")

    def test_a_project_with_nothing_to_run_is_refused(self):
        self.publish({"schemaVersion": 1, "name": "x", "lanes": {"docs": {"command": ["true"]}}})
        with self.assertRaisesRegex(ValueError, "nothing to run"): mq.add(self.repo, "HEAD")

    def test_named_lanes_come_first_and_gate_is_the_default(self):
        self.assertEqual(mq.lanes_of({"queue": {"lanes": ["a"]}, "lanes": {"a": {}, "gate": {}}}), ["spelling", "a"])
        self.assertEqual(mq.lanes_of({"lanes": {"gate": {}}}), ["spelling", "gate"])
        self.assertEqual(mq.lanes_of({"lanes": {"gate": {}, "provision": {}, "spelling": {}, "docs": {}}}),
                         ["spelling", "provision", "gate"])

    def test_a_project_without_a_spelling_lane_gets_the_toolkits_unless_it_turned_it_off(self):
        off = {"spelling": False}
        self.assertEqual(mq.lanes_of({"lanes": {"gate": {}}}), ["spelling", "gate"])
        self.assertEqual(mq.lanes_of({"lanes": {"gate": {}, "provision": {}}}), ["spelling", "provision", "gate"])
        self.assertEqual(mq.lanes_of({"lanes": {"gate": {}}, "queue": off}), ["gate"])
        # A project's own lane is the one that runs, turned off or not, and only once.
        self.assertEqual(mq.lanes_of({"lanes": {"gate": {}, "spelling": {}}, "queue": off}), ["spelling", "gate"])
        self.assertEqual(mq.ungated_lanes({"lanes": {"gate": {}}}), ["spelling"])
        self.assertEqual(mq.ungated_lanes({"lanes": {"gate": {}}, "queue": off}), [])
        self.assertEqual(mq.ungated_lanes({"lanes": {"gate": {}, "spelling": {}}, "queue": off}), ["spelling"])
        declared = {"paths": {"doc/": "no code lane reads it"}, "lanes": ["docs"]}
        lanes = {"gate": {}, "docs": {}}
        self.assertEqual(mq.docs_lanes({"lanes": lanes, "queue": {"docsOnly": declared}}, ["doc/a.md"]), ["spelling", "docs"])
        self.assertEqual(mq.docs_lanes({"lanes": lanes, "queue": {"docsOnly": declared, **off}}, ["doc/a.md"]), ["docs"])
        self.assertIsNone(mq.docs_lanes({"lanes": lanes, "queue": {"docsOnly": declared}}, ["doc/a.md", "code"]))
        named = {**declared, "lanes": ["docs", "spelling"]}
        self.assertEqual(mq.docs_lanes({"lanes": {**lanes, "spelling": {}}, "queue": {"docsOnly": named}}, ["doc/a.md"]),
                         ["docs", "spelling"])
        # eng run places the toolkit's check as any lane, so it holds no bisect half on this Mac; an unknown lane does.
        self.assertIsNone(mq.offerable({"lanes": {"gate": {}}}, ["spelling", "gate"]))
        self.assertIn("no configured lane", mq.offerable({"lanes": {"gate": {}}, "queue": off}, ["spelling", "gate"]))

    def test_configuration_is_checked(self):
        config = json.loads((self.repo / "engineering.json").read_text())
        for bad in ({"lanes": ["none"]}, {"other": 1}, {"carryOver": {"x/": ""}}):
            (self.repo / "engineering.json").write_text(json.dumps({**config, "queue": bad}))
            with self.assertRaises(ValueError): self.config()

    def test_a_push_refused_for_any_other_reason_holds(self):
        self.commit("a", "a")
        [identifier] = self.add("a")
        hook = self.origin / "hooks/pre-receive"
        hook.write_text("#!/bin/sh\necho no >&2\nexit 1\n")
        host_isolation.executable(hook)
        self.assertEqual(self.work(), 1)
        self.assertEqual(self.state(identifier)["state"], "passed")
        self.assertIn("refused", mq.Queue(self.repo).held())

    def test_a_push_whose_connection_dropped_is_pushed_again(self):
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.remote_says("fatal: the remote end hung up unexpectedly", {1}, "push") as (pushes, slept):
            self.work()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertIn("a", self.main_files())
        self.assertEqual(self.gates(), 1)
        self.assertEqual((len(pushes), slept), (2, [mq.BACKOFF[0]]))

    def test_a_push_that_keeps_dropping_holds_after_the_rounds_tries(self):
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.remote_says("fatal: the remote end hung up unexpectedly", range(1, 999), "push") as (pushes, slept), \
                patch("builtins.print"):
            self.assertEqual(self.work(), 1)
        self.assertEqual(self.state(identifier)["state"], "passed")
        held = mq.Queue(self.repo).held()
        self.assertIn("did not reach the remote", held)
        self.assertTrue(held.startswith("for a session, not the owner: "), held)
        rounds = len(mq.CLEARING_WAITS) + 1
        self.assertEqual((len(pushes), slept.count(mq.BACKOFF[-1])), (rounds * (len(mq.BACKOFF) + 1), rounds))
        # The pass stays through the tries: one gate.
        self.assertEqual(self.gates(), 1)

    def test_a_remote_that_refuses_a_push_holds_at_once_whatever_else_it_says(self):
        """The remote answered: its hook's message is no dropped connection, which Git would print the same."""
        self.commit("a", "a")
        [identifier] = self.add("a")
        hook = self.origin / "hooks/pre-receive"
        hook.write_text('#!/bin/sh\necho "fatal: the remote end hung up unexpectedly" >&2\nexit 1\n')
        host_isolation.executable(hook)
        with patch.object(mq, "pause") as slept: self.assertEqual(self.work(), 1)
        self.assertEqual(self.state(identifier)["state"], "passed")
        self.assertIn("refused", mq.Queue(self.repo).held())
        slept.assert_not_called()

    def test_a_removed_entry_does_not_land(self):
        self.commit("a", "a"); self.commit("b", "b")
        ids = self.add("a", "b")
        queue = mq.Queue(self.repo)
        real = mq.Worker.gate
        def gate(worker, lanes, round_name):
            outcome = real(worker, lanes, round_name)
            if not queue.read(ids[0])["state"] == "removed":
                with patch("builtins.print"): mq.remove(queue, ids[0])
            return outcome
        with patch.object(mq.Worker, "gate", gate): self.work()
        self.assertEqual(self.state(ids[0])["state"], "removed")
        self.assertEqual(self.state(ids[1])["state"], "landed")
        self.assertNotIn("a", self.main_files())
        self.assertEqual(self.gates(), 2)

    def test_the_commit_that_first_names_the_lanes_lands_and_main_decides_after(self):
        """Negative control first: once main names its lanes, a branch cannot choose another."""
        config = json.loads((self.repo / "engineering.json").read_text())
        lane = config["lanes"].pop("gate")
        config["lanes"]["checks"] = lane
        (self.repo / "engineering.json").write_text(json.dumps(config))
        run(self.repo, "git", "add", "engineering.json")
        run(self.repo, "git", "commit", "-qm", "No gate lane yet")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        run(self.repo, "git", "checkout", "-q", "-B", "name", "origin/main")
        config["queue"]["lanes"] = ["checks"]
        (self.repo / "engineering.json").write_text(json.dumps(config))
        run(self.repo, "git", "add", "engineering.json")
        run(self.repo, "git", "commit", "-qm", "Name the lanes")
        [named] = self.add("name")
        self.work()
        self.assertEqual(self.state(named)["state"], "landed")
        self.assertEqual(self.gates(), 1)
        self.assertIsNone(mq.Queue(self.repo).held())
        run(self.repo, "git", "fetch", "-q", "origin")
        run(self.repo, "git", "checkout", "-q", "-B", "skip", "origin/main")
        (self.repo / "bad").write_text("x")
        config["lanes"]["nothing"] = {"command": ["true"]}
        config["queue"]["lanes"] = ["nothing"]
        (self.repo / "engineering.json").write_text(json.dumps(config))
        run(self.repo, "git", "add", "bad", "engineering.json")
        run(self.repo, "git", "commit", "-qm", "Choose a gate that passes")
        [skip] = self.add("skip")
        self.work()
        self.assertEqual(self.state(skip)["state"], "failed")
        self.assertNotIn("bad", self.main_files())

    def test_a_session_in_another_repository_lands_here(self):
        """Through the real command line, from another project's checkout, as a session fixing a
        dependency does: this repository's queue takes it, and its own session's entry waits beside it."""
        other = self.base / "dependent"
        other.mkdir()
        (other / "engineering.json").write_text(json.dumps({"schemaVersion": 1, "name": "dependent"}))
        run(other, "git", "init", "-q")
        (other / "scripts").mkdir()
        shutil.copy(ROOT / "scripts/eng", other / "scripts/eng")
        self.commit("fix", "fix")
        self.commit("own", "own")
        [own] = self.add("own")
        done = subprocess.run(["python3", "scripts/eng", "land", "--repo", "project", "fix", "--wait",
                               "--limit", str(stretched(120))], cwd=other, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertLessEqual({"fix", "own"}, self.main_files())
        self.assertEqual(self.state(own)["state"], "landed")
        self.assertEqual(self.gates(), 1)
        landed = done.stdout.rsplit("landed: ", 1)[1].strip()
        self.assertEqual(run(self.repo, "git", "rev-parse", "origin/main"), landed)
        self.assertEqual(mq.Queue(other).all(), [])

    def test_another_repository_needs_a_named_commit_and_a_real_project(self):
        other = self.base / "dependent"
        other.mkdir()
        (other / "scripts").mkdir()
        shutil.copy(ROOT / "scripts/eng", other / "scripts/eng")
        (other / "engineering.json").write_text(json.dumps({"schemaVersion": 1, "name": "dependent"}))
        run(other, "git", "init", "-q")
        for words, said in ((["land", "--repo", "project"], "you name"), (["land", "--repo", "absent", "x"], "names no project")):
            done = subprocess.run(["python3", "scripts/eng", *words], cwd=other, capture_output=True, text=True)
            self.assertNotEqual(done.returncode, 0)
            self.assertIn(said, done.stderr)
        self.assertEqual(mq.Queue(self.repo).all(), [])

    def test_a_session_started_worker_lands_in_the_background(self):
        self.commit("a", "a")
        with patch("builtins.print"): mq.add(self.repo, "a")
        identifier = mq.Queue(self.repo).all()[0]["id"]
        with patch("builtins.print"): self.assertEqual(mq.wait(self.repo, identifier, stretched(120)), 0)
        self.assertIn("a", self.main_files())

    def test_a_worker_that_ended_leaves_no_child_of_this_process(self):
        # Unreaped, it stayed a zombie, with its own process group and this process as its parent,
        # for test_core to take for the command it supervises when one process ran both (#99).
        def children():
            listing = subprocess.Popen(["ps", "-axo", "pid=,ppid=,stat="], stdout=subprocess.PIPE, text=True)
            listed = listing.communicate()[0]
            # Without ps itself, a child of this process while it lists.
            return {pid: stat for pid, parent, stat in map(str.split, listed.splitlines())
                    if int(parent) == os.getpid() and int(pid) != listing.pid}
        before = set(children())
        self.commit("a", "a")
        with patch("builtins.print"): mq.add(self.repo, "a")
        identifier = mq.Queue(self.repo).all()[0]["id"]
        with patch("builtins.print"): self.assertEqual(mq.wait(self.repo, identifier, stretched(120)), 0)
        self.assertIn("a", self.main_files())
        deadline = time.monotonic() + stretched(30)
        while (left := {pid: stat for pid, stat in children().items() if pid not in before}) and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertEqual(left, {})


# A fixture suite for an eng unittest gate: test_sometimes fails on each run whose letter in PLAN is F, counting its
# runs in RUNS, and imports fixturelib.helper, which imports fixturelib.deeper.
FLAKY_MODULE = """import pathlib, sys, unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src/python"))
from fixturelib import helper

RUNS, PLAN = pathlib.Path({runs!r}), pathlib.Path({plan!r})


class Flaky(unittest.TestCase):
    def test_sometimes(self):
        done = len(RUNS.read_text()) if RUNS.exists() else 0
        RUNS.write_text("x" * (done + 1))
        plan = PLAN.read_text() if PLAN.exists() else ""
        self.assertNotEqual(plan[done:done + 1], "F", f"run {{done + 1}} fails, as planned")

    def test_steady(self):
        self.assertEqual(helper.VALUE, 1)
"""

# A word check of docs/, whose failure names each PATH:LINE, as eng spelling's does.
WORDS_MODULE = """import pathlib, unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


class Words(unittest.TestCase):
    def test_spelling(self):
        found = [f"{path.relative_to(ROOT)}:{number}: misspelling wrongword"
                 for path in sorted((ROOT / "docs").rglob("*.md"))
                 for number, line in enumerate(path.read_text().splitlines(), 1) if "wrongword" in line]
        self.assertEqual(found, [], "\\n".join(found))
"""


class RejectionFacts(Fixture):
    """What a rejection adds about the failing lane's load and tests, and what it leaves alone (#273)."""
    def test_a_rejection_of_a_lane_that_names_no_tests_says_it_cannot_tell(self):
        self.commit("a", "a")
        [identifier] = self.add("a")
        self.status.write_text("1")
        # The positive control of sampled_while_running: the rejected lane's end is read once, sampling or not.
        with self.sampled_while_running() as finish, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.work(), 0)
        finish.assert_called_once()
        entry = self.state(identifier)
        self.assertEqual(entry["state"], "failed")
        self.assertIn("the one-minute load was highest 3", entry["why"])
        self.assertIn("Which tests failed could not be determined (its log has no eng unittest summary), so whether this "
                      "entry changes their files is unknown", entry["why"])
        self.assertNotIn("changes no file", entry["why"])
        self.assertIn("could not be determined", mq.describe(entry))

    @contextlib.contextmanager
    def sampled_while_running(self):
        """The lane's load sampled while it runs, as a lane longer than BUSY_SAMPLE is (or one a sleep or a clock step
        stretches past it), and the end-of-lane reading a rejection adds watched apart from it: such a sampling is no
        reading of the lane's end (#358). Yields the watched LoadSampler.finish."""
        with patch.object(mq, "BUSY_SAMPLE", 0.01), \
                patch.object(mq.LoadSampler, "finish", autospec=True, side_effect=mq.LoadSampler.finish) as finish:
            yield finish

    def test_a_gate_that_passes_reads_no_end_of_lane_load_and_names_none(self):
        # The negative control: at a passing lane's end nothing is read, and the entry has no reason at all.
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.sampled_while_running() as finish, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.work(), 0)
        finish.assert_not_called()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertNotIn("why", self.state(identifier))

    def test_a_lane_with_no_verdict_reads_no_load_and_rejects_nothing(self):
        # Load is reported on a rejection only: a run left unmeasured stays as it was, and is queued again.
        self.commit("a", "a")
        [identifier] = self.add("a")
        self.status.write_text(str(mq.evidence.UNMEASURED))
        with self.sampled_while_running() as finish, contextlib.redirect_stdout(io.StringIO()): self.work()
        finish.assert_not_called()
        entry = self.state(identifier)
        self.assertNotEqual(entry["state"], "failed")
        self.assertNotIn("During gate", entry.get("why", ""))


class LoadReadings(unittest.TestCase):
    """load_of, load_words and LoadSampler alone (#273)."""
    def test_load_of_reads_the_second_mac_there_and_this_mac_here(self):
        there = {"load": 12.5, "busyLimit": 75.0}
        with patch.object(mq.remote, "reading_there", return_value=(there, 40.0)) as asked:
            self.assertEqual(mq.load_of("fake-mac"), (12.5, 40.0, 75.0))
        asked.assert_called_once_with("fake-mac")
        with patch.object(mq.remote, "reading_there", return_value=({"busyLimit": 75.0, "load": "x"}, None)):
            self.assertEqual(mq.load_of("fake-mac"), (None, None, 75.0))
        with patch.object(mq.remote, "reading_there", side_effect=mq.remote.Unavailable("gone")), \
                self.assertRaises(mq.remote.Unavailable):
            mq.load_of("fake-mac")
        with patch.object(mq, "busy_of", return_value=(50.0, 70.0, "this Mac")), \
                patch.object(mq.resources, "one_minute_load", return_value=9.5):
            self.assertEqual(mq.load_of(None), (9.5, 50.0, 70.0))
        with patch.object(mq, "busy_of", return_value=None), patch.object(mq.resources, "one_minute_load", return_value=None):
            self.assertEqual(mq.load_of(None), (None, None, None))

    def test_load_words_keep_a_failed_reading_apart_from_a_low_one(self):
        cases = [([(70.0, 90.0, 75.0), (40.0, 99.0, 75.0)], "the one-minute load was highest 70, last 40 over 2 readings, "
                  "and the CPU was at most 99% busy, against its busy limit of 75%"),
                 ([(5.0, 10.0, 75.0), None, (None, None, 75.0)], "the one-minute load was highest 5, last 5 over 1 of 3 "
                  "readings, and the CPU was at most 10% busy, against its busy limit of 75%"),
                 ([], "the one-minute load could not be read, and the CPU busy share could not be read"),
                 ([None], "the one-minute load could not be read, and the CPU busy share could not be read")]
        for readings, expected in cases:
            with self.subTest(readings=readings):
                self.assertEqual(mq.load_words("gate", None, readings), f"During gate on this Mac, {expected}")
        self.assertIn("on the second Mac (fake-mac)", mq.load_words("gate", "fake-mac", []))

    def test_the_sampler_keeps_only_the_readings_of_the_mac_the_lane_ran_on(self):
        # Before the dispatch names the second Mac, a reading is this Mac's: not the lane's.
        readings = {None: (70.0, 99.0, 75.0), "fake-mac": (12.0, 40.0, 75.0)}
        with patch.object(mq, "load_of", side_effect=readings.get), \
                patch.object(mq.remote, "dispatched", side_effect=[None, "fake-mac"]):
            sampler = mq.LoadSampler("/nowhere", "gate", 0).start()
            sampler.read()
            sampler.read()
            said = sampler.finish("fake-mac")
        self.assertIn("on the second Mac (fake-mac), the one-minute load was highest 12, last 12 over 2 readings", said)
        self.assertNotIn("70", said)

    def test_a_reading_that_raises_reaches_no_gate(self):
        with patch.object(mq, "load_of", side_effect=ValueError("no")), \
                patch.object(mq.remote, "dispatched", side_effect=OSError("no")):
            sampler = mq.LoadSampler("/nowhere", "gate", 0).start()
            sampler.read()
            self.assertEqual(sampler.finish(None), "During gate on this Mac, the one-minute load could not be read, and "
                                                   "the CPU busy share could not be read")


class FlakyReruns(Fixture):
    """A failed eng unittest gate's failed tests rerun alone before a bisect, and a batch whose failed tests all pass
    alone and that reaches none of their files lands, with the flake recorded (owner's decision of 2026-11-07, #231)."""
    # Every rerun scenario exercises an opaque root that would otherwise fail spelling first.
    ROOT_PREFIX = "fixture_" + "oei" + "tre_"
    BUSY = ("cpu", "the CPU is 95% busy, at or above HEAVY_BUSY_LIMIT=75%")
    TEST = "test_flaky.Flaky.test_sometimes"

    def setUp(self):
        super().setUp()
        self.runs, self.plan = self.base / "runs", self.base / "plan"
        (self.repo / "tests").mkdir()
        (self.repo / "tests/test_flaky.py").write_text(FLAKY_MODULE.format(runs=str(self.runs), plan=str(self.plan)))
        (self.repo / "tests/test_words.py").write_text(WORDS_MODULE)
        library = self.repo / "src/python/fixturelib"
        library.mkdir(parents=True)
        (library / "__init__.py").write_text("")
        (library / "helper.py").write_text("from . import deeper\nVALUE = 1\n")
        (library / "deeper.py").write_text("DEPTH = 1\n")
        config = self.config()
        # This gate intentionally consumes docs/*.md as test inputs, so its documents keep the code gate.
        config["queue"]["docsOnly"] = False
        config["lanes"]["gate"] = {"command": ["python3", "scripts/eng", "unittest", "-s", "tests", "-v"], "timeout": 600}
        (self.repo / "engineering.json").write_text(json.dumps(config))
        run(self.repo, "git", "add", ".")
        run(self.repo, "git", "commit", "-qm", "Gate on an eng unittest suite")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")

    def assert_gate_failure(self, path):
        """The original failure and assertion survive collection on both supported unittest formats."""
        log = Path(path).read_text()
        # Python 3.9 names the class in parentheses; later versions include the method there too.
        self.assertRegex(log, r"(?m)^FAIL: test_sometimes \(test_flaky\.Flaky(?:\.test_sometimes)?\)$")
        self.assertIn("AssertionError: 'F' == 'F' : run 1 fails, as planned", log)

    def runs_of_the_test(self):
        return len(self.runs.read_text()) if self.runs.exists() else 0

    def tip_message(self):
        run(self.repo, "git", "fetch", "-q", "origin")
        return run(self.repo, "git", "log", "-1", "--format=%B", "origin/main")

    def main_messages(self):
        return run(self.repo, "git", "log", "--format=%B", "origin/main")

    def test_a_test_that_fails_once_and_passes_alone_lands_the_batch_after_a_settle_and_is_recorded(self):
        self.plan.write_text("F")
        self.commit("a", "a"); self.commit("b", "b")
        ids = self.add("a", "b")
        with self.host([self.BUSY, None]) as (slept, said):
            self.assertEqual(self.work(), 0)
        log = said.getvalue()
        entries = [self.state(identifier) for identifier in ids]
        self.assertEqual([entry["state"] for entry in entries], ["landed", "landed"])
        # One gate and its reruns, each alone, after the host settled, and no bisect.
        self.assertEqual(self.runs_of_the_test(), 1 + mq.RERUNS)
        self.assertEqual(slept, [mq.SETTLE_POLL])
        self.assertNotIn("fail together", log)
        self.assertIn(f"gate failed 1 test at ", log)
        self.assertIn(f": {self.TEST}; rerunning each alone up to {mq.RERUNS} times on that commit before a bisect", log)
        self.assertIn("the failed tests run alone again once the host settles", log)
        self.assertIn("no commit of the batch changes its module or a file it imports: landing the batch", log)
        # Recorded in each entry, with both logs, on the tracker and in the landed tip's trailer.
        for entry in entries:
            [flake] = entry["flakes"]
            self.assertEqual(flake["test"], self.TEST)
            self.assertTrue(Path(flake["gateLog"]).is_file())
            self.assert_gate_failure(flake["gateLog"])
            self.assertEqual(len(flake["rerunLogs"]), mq.RERUNS)
            self.assertTrue(all("OK" in Path(path).read_text() for path in flake["rerunLogs"]))
            self.assertEqual(flake["issue"], "https://example.invalid/fixture/issues/1")
        self.assertEqual(entries[0]["landedTip"], entries[1]["landedTip"])
        [tracked] = self.tracked
        self.assertEqual(tracked["test"], self.TEST)
        self.assertIn("the batch landed", tracked["outcome"])
        self.assertIn(f"flaky: {self.TEST} failed the gate at", log)
        tip = self.tip_message()
        self.assertIn(f"{mq.FLAKY}: {self.TEST} failed the gate at ", tip)
        self.assertIn(f"passed alone {mq.RERUNS} times there; gate log {entries[0]['flakes'][0]['gateLog']}", tip)
        self.assertIn("https://example.invalid/fixture/issues/1", tip)
        self.assertEqual(self.main_messages().count(f"{mq.FLAKY}:"), 1)
        self.assertIn("flaky: test_flaky.Flaky.test_sometimes failed the gate at", mq.describe(entries[0]))

    @contextlib.contextmanager
    def second_mac(self):
        """On collection, move the lane's unittest log into a fake second-Mac dispatch. The live observers
        see no dispatch, so they cannot move the log before the worker collects it (#331)."""
        dispatches = self.base / "dispatches"
        def dispatch(root, name, since):
            own = Path(root) / ".build/engineering" / name / "run.log"
            if not own.exists() or "eng unittest: " not in own.read_text(errors="replace"): return None, None
            ran = dispatches / f"{len(list(dispatches.glob('*'))) if dispatches.exists() else 0}"
            ran.mkdir(parents=True)
            own.rename(ran / "run.log")
            return ran, "fake-mac"
        with patch.object(mq.remote, "dispatch", side_effect=dispatch), \
                patch.object(mq.remote, "dispatched", return_value=None):
            yield

    def test_a_failed_test_of_a_gate_the_second_mac_ran_is_rerun_alone_from_its_dispatchs_log(self):
        # The queue's log held only what eng run printed here, with no eng unittest summary, so a second Mac's
        # failed test was never rerun and every batch bisected.
        self.plan.write_text("F")
        self.commit("a", "a"); self.commit("b", "b")
        ids = self.add("a", "b")
        finish = mq.GateWatch.finish

        def final_poll(watch):
            # Exercise an observation after the lane wrote its log and before the worker collects the dispatch.
            watch.look()
            finish(watch)

        with self.second_mac(), patch.object(mq.GateWatch, "finish", final_poll), \
                contextlib.redirect_stdout(io.StringIO()) as said:
            self.assertEqual(self.work(), 0)
        log = said.getvalue()
        self.assertIn("gate exited 1 on fake-mac", log)
        self.assertIn(f": {self.TEST}; rerunning each alone up to {mq.RERUNS} times on that commit before a bisect", log)
        self.assertNotIn("no eng unittest summary", log)
        self.assertNotIn("fail together", log)
        entries = [self.state(identifier) for identifier in ids]
        self.assertEqual([entry["state"] for entry in entries], ["landed", "landed"])
        self.assert_gate_failure(entries[0]["flakes"][0]["gateLog"])

    def make_gate_a_group(self, other_status):
        """Make the gate a group of the eng unittest suite and a lane that exits other_status, as the toolkit's is (#289)."""
        declared = json.loads((self.repo / "engineering.json").read_text())
        declared["lanes"]["suite"] = declared["lanes"]["gate"]
        declared["lanes"]["other"] = {"command": ["python3", "-c", f"raise SystemExit({other_status})"], "timeout": 600}
        declared["lanes"]["gate"] = {"parallel": ["suite", "other"], "resource": "heavy"}
        (self.repo / "engineering.json").write_text(json.dumps(declared))
        run(self.repo, "git", "add", "engineering.json")
        run(self.repo, "git", "commit", "-qm", "Gate on a group of lanes")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")

    def test_a_flake_in_a_groups_eng_unittest_lane_reruns_alone_and_the_batch_lands(self):
        self.make_gate_a_group(0)
        self.plan.write_text("F")
        self.commit("a", "a"); self.commit("b", "b")
        ids = self.add("a", "b")
        with contextlib.redirect_stdout(io.StringIO()) as said: self.assertEqual(self.work(), 0)
        log = said.getvalue()
        self.assertIn(f": {self.TEST}; rerunning each alone up to {mq.RERUNS} times on that commit before a bisect", log)
        self.assertNotIn("no eng unittest summary", log)
        self.assertEqual(self.runs_of_the_test(), 1 + mq.RERUNS)
        entries = [self.state(identifier) for identifier in ids]
        self.assertEqual([entry["state"] for entry in entries], ["landed", "landed"])
        self.assert_gate_failure(entries[0]["flakes"][0]["gateLog"])

    def group_on_the_second_mac(self, keep_lanes=True):
        """remote.dispatch as for a group gate the second Mac ran: its lanes' logs and receipts are only in the
        dispatch's lanes/, as eng files them, and none is left in the worker's checkout."""
        dispatches = self.base / "dispatches"
        def dispatch(root, name, since):
            own = Path(root) / ".build/engineering" / name / "lanes"
            if not own.is_dir(): return None, None
            ran = dispatches / f"{len(list(dispatches.glob('*'))) if dispatches.exists() else 0}"
            ran.mkdir(parents=True)
            (ran / "run.log").write_text("")
            if keep_lanes: own.rename(ran / "lanes")
            else: shutil.rmtree(own)
            return ran, "fake-mac"
        return patch.object(mq.remote, "dispatch", side_effect=dispatch)

    def test_a_flake_in_a_group_gate_the_second_mac_ran_reruns_from_its_dispatchs_lane_log(self):
        # A dispatched group's run.log is empty and its output holds eng unittest's summary only indented, in a
        # failed lane's tail; the lane's own log came back to the dispatch, not to the worker's checkout.
        self.make_gate_a_group(0)
        self.plan.write_text("F")
        self.commit("a", "a"); self.commit("b", "b")
        ids = self.add("a", "b")
        with self.group_on_the_second_mac(), contextlib.redirect_stdout(io.StringIO()) as said:
            self.assertEqual(self.work(), 0)
        log = said.getvalue()
        self.assertIn("gate exited 1 on fake-mac", log)
        self.assertIn(f": {self.TEST}; rerunning each alone up to {mq.RERUNS} times on that commit before a bisect", log)
        self.assertNotIn("no eng unittest summary", log)
        entries = [self.state(identifier) for identifier in ids]
        self.assertEqual([entry["state"] for entry in entries], ["landed", "landed"])
        self.assert_gate_failure(entries[0]["flakes"][0]["gateLog"])

    def test_a_group_gate_whose_dispatch_brought_no_lane_logs_bisects_and_says_why(self):
        # The negative control: with no receipt, no lane is known to have passed, so nothing is rerun.
        self.make_gate_a_group(0)
        self.plan.write_text("F")
        self.commit("a", "a"); self.commit("b", "b")
        self.add("a", "b")
        with self.group_on_the_second_mac(keep_lanes=False), contextlib.redirect_stdout(io.StringIO()) as said:
            self.assertEqual(self.work(), 0)
        self.assertIn("gate's lane other did not pass, which no rerun of a Python test can clear: bisecting", said.getvalue())
        self.assertNotIn("rerunning each alone", said.getvalue())

    def test_a_failed_remote_group_gate_reports_the_unittest_members_summary_and_assertion_in_the_wait(self):
        self.make_gate_a_group(1)
        self.plan.write_text("F")
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.group_on_the_second_mac(), contextlib.redirect_stdout(io.StringIO()) as worker:
            self.assertEqual(self.work(), 0, worker.getvalue())
        entry = self.state(identifier)
        self.assertEqual(entry["state"], mq.FAILED)
        gate_log = Path(entry["log"]).read_text()
        [summary] = [line for line in gate_log.splitlines() if line.startswith("eng unittest: ") and
                     "FAILED: test_flaky" in line]
        with contextlib.redirect_stdout(io.StringIO()) as waited:
            self.assertEqual(mq.wait(self.repo, identifier, 1), 1)
        self.assertIn(summary, waited.getvalue())
        self.assertIn("AssertionError: 'F' == 'F' : run 1 fails, as planned", waited.getvalue())
        self.assertIn("remote dispatch run.log:", waited.getvalue())

    def test_a_group_whose_other_lane_failed_too_bisects_without_a_rerun(self):
        # The negative control: passing reruns of the Python tests must not land a batch another lane failed.
        self.make_gate_a_group(1)
        self.plan.write_text("F")
        self.commit("a", "a")
        self.add("a")
        with contextlib.redirect_stdout(io.StringIO()) as said: self.assertEqual(self.work(), 0)
        log = said.getvalue()
        self.assertIn("gate's lane other did not pass, which no rerun of a Python test can clear: bisecting", log)
        self.assertNotIn("rerunning each alone", log)
        self.assertEqual(self.runs_of_the_test(), 1)

    def test_a_second_mac_gate_whose_dispatch_kept_no_log_bisects_and_says_why(self):
        # The negative control: a dispatch with no run.log gives the rerun nothing to read.
        self.plan.write_text("F")
        self.commit("a", "a"); self.commit("b", "b")
        ids = self.add("a", "b")
        def lost(root, name, since):
            own = Path(root) / ".build/engineering" / name / "run.log"
            if not own.exists() or "eng unittest: " not in own.read_text(errors="replace"): return None, None
            own.unlink()
            return self.base, "fake-mac"
        with patch.object(mq.remote, "dispatch", side_effect=lost), \
                contextlib.redirect_stdout(io.StringIO()) as said:
            self.assertEqual(self.work(), 0)
        log = said.getvalue()
        self.assertIn("gate failed with no failed test to rerun alone (its log has no eng unittest summary): bisecting",
                      log)
        self.assertEqual([self.state(identifier)["state"] for identifier in ids], ["landed", "landed"])

    def test_a_test_that_fails_again_alone_is_real_and_the_batch_bisects_with_nothing_recorded(self):
        # The gate fails, the first rerun passes and the second fails: real. Every run after passes.
        self.plan.write_text("F.F")
        self.commit("a", "a"); self.commit("b", "b")
        ids = self.add("a", "b")
        with contextlib.redirect_stdout(io.StringIO()) as said: self.assertEqual(self.work(), 0)
        log = said.getvalue()
        self.assertIn(f"{self.TEST} failed again alone, on rerun 2 (exit 1), so the failure is real", log)
        # Neither commit changes the test's module, a file it imports or a path its failure names: no blame.
        self.assertIn(f"{self.TEST} reaches no entry of the batch's 2, so no commit is blamed: bisecting", log)
        self.assertIn("2 commits fail together: next the oldest 1", log)
        self.assertEqual([self.state(identifier)["state"] for identifier in ids], ["landed", "landed"])
        self.assertEqual(self.runs_of_the_test(), 3 + 2)
        self.assertEqual(self.tracked, [])
        self.assertTrue(all("flakes" not in self.state(identifier) for identifier in ids))
        self.assertNotIn(f"{mq.FLAKY}:", self.main_messages())

    WORDS = "test_words.Words.test_spelling"

    def test_a_real_failure_whose_output_names_a_path_one_commit_changes_sends_that_commit_back_without_a_bisect(self):
        self.commit("c", "c")
        self.commit("docs/a.md", "a", "The wrongword\n")
        self.commit("docs/b.md", "b", "The word\n")
        ids = self.add("c", "a", "b")
        with contextlib.redirect_stdout(io.StringIO()) as said: self.assertEqual(self.work(), 0)
        log = said.getvalue()
        self.assertIn(f"{self.WORDS} failed again alone, on rerun 1 (exit 1), so the failure is real", log)
        self.assertIn(f"blaming {ids[1]} (Add docs/a.md): {self.WORDS}, which failed alone, as its failure names "
                      f"docs/a.md, which only {ids[1]} changes; sending it back and gating the other 2 at once instead "
                      "of bisecting", log)
        self.assertNotIn("fail together", log)
        entries = [self.state(identifier) for identifier in ids]
        self.assertEqual([entry["state"] for entry in entries], ["landed", "failed", "landed"])
        self.assertIn("its failure names docs/a.md", entries[1]["why"])
        # One gate for the batch and one for the other two, with no half gated.
        self.assertEqual(self.runs_of_the_test(), 2)
        self.assertEqual(self.tracked, [])

    def test_a_real_failure_of_a_test_whose_import_one_commit_changes_sends_that_commit_back(self):
        self.plan.write_text("FF")
        self.commit("b", "b")
        self.commit("src/python/fixturelib/helper.py", "a", "from . import deeper\nVALUE = 1  # changed\n")
        ids = self.add("b", "a")
        with contextlib.redirect_stdout(io.StringIO()) as said: self.assertEqual(self.work(), 0)
        log = said.getvalue()
        self.assertIn(f"blaming {ids[1]} (Add src/python/fixturelib/helper.py): {self.TEST}, which failed alone, as it "
                      f"imports src/python/fixturelib/helper.py, which only {ids[1]} changes", log)
        self.assertNotIn("fail together", log)
        self.assertEqual([self.state(identifier)["state"] for identifier in ids], ["landed", "failed"])
        self.assertEqual(self.runs_of_the_test(), 3)

    def test_a_failure_that_names_paths_of_two_commits_bisects(self):
        self.commit("docs/a.md", "a", "The wrongword\n")
        self.commit("docs/b.md", "b", "The wrongword\n")
        ids = self.add("a", "b")
        with contextlib.redirect_stdout(io.StringIO()) as said: self.assertEqual(self.work(), 0)
        log = said.getvalue()
        self.assertIn(f"{self.WORDS} reaches 2 entries of the batch's 2 ({ids[0]}: its failure names docs/a.md; "
                      f"{ids[1]}: its failure names docs/b.md), so no commit is blamed: bisecting", log)
        self.assertIn("2 commits fail together: next the oldest 1", log)
        self.assertNotIn("blaming", log)
        self.assertEqual([self.state(identifier)["state"] for identifier in ids], ["failed", "failed"])

    def test_failed_tests_that_reach_different_commits_bisect(self):
        self.plan.write_text("FF")
        self.commit("docs/a.md", "a", "The wrongword\n")
        self.commit("src/python/fixturelib/helper.py", "b", "from . import deeper\nVALUE = 1  # changed\n")
        ids = self.add("a", "b")
        with contextlib.redirect_stdout(io.StringIO()) as said: self.assertEqual(self.work(), 0)
        log = said.getvalue()
        self.assertIn("the failed tests reach different entries (", log)
        self.assertIn("2 commits fail together: next the oldest 1", log)
        self.assertNotIn("blaming", log)
        self.assertEqual([self.state(identifier)["state"] for identifier in ids], ["failed", "landed"])

    def test_a_test_whose_module_cannot_be_found_bisects(self):
        self.commit("docs/a.md", "a", "The wrongword\n")
        self.commit("b", "b")
        ids = self.add("a", "b")
        with patch.object(mq, "imported_files", return_value=None), \
                contextlib.redirect_stdout(io.StringIO()) as said:
            self.assertEqual(self.work(), 0)
        log = said.getvalue()
        self.assertIn(f"the worker cannot find the module of {self.WORDS}, so no commit is blamed: bisecting", log)
        self.assertIn("2 commits fail together", log)
        self.assertEqual([self.state(identifier)["state"] for identifier in ids], ["failed", "landed"])

    def test_a_flake_beside_a_real_failure_blames_no_commit(self):
        # The flaky test fails in the gate only; the spelling failure is real and names a.
        self.plan.write_text("F")
        self.commit("docs/a.md", "a", "The wrongword\n")
        self.commit("b", "b")
        ids = self.add("a", "b")
        with contextlib.redirect_stdout(io.StringIO()) as said: self.assertEqual(self.work(), 0)
        log = said.getvalue()
        self.assertIn(f"{self.TEST} passed alone beside the real failure, so no commit is blamed", log)
        self.assertIn("2 commits fail together", log)
        self.assertNotIn("blaming", log)
        self.assertEqual([self.state(identifier)["state"] for identifier in ids], ["failed", "landed"])

    def test_a_lone_entry_whose_failure_is_real_stops_at_the_first_real_rerun(self):
        self.plan.write_text("FF")
        self.commit("docs/a.md", "a", "The wrongword\n")
        [identifier] = self.add("a")
        with contextlib.redirect_stdout(io.StringIO()) as said: self.assertEqual(self.work(), 0)
        log = said.getvalue()
        self.assertEqual(log.count("so the failure is real"), 1)
        self.assertNotIn("blaming", log)
        self.assertEqual(self.state(identifier)["state"], "failed")

    LOADED = (70.0, 99.0, 75.0)

    def rejected(self, *commits):
        """The why of the one entry added, a lone real failure of TEST, committed as commits say."""
        self.plan.write_text("FF")
        for number, (path, *content) in enumerate(commits):
            self.commit(path, f"branch-{number}", *content)
        [identifier] = self.add(*[f"branch-{number}" for number in range(len(commits))])
        with contextlib.redirect_stdout(io.StringIO()): self.assertEqual(self.work(), 0)
        entry = self.state(identifier)
        self.assertEqual(entry["state"], "failed")
        # The gate and the one rerun that confirmed it, as without the rejection's added words.
        self.assertEqual(self.runs_of_the_test(), 2)
        return entry["why"]

    def test_a_rejection_on_a_loaded_host_in_a_file_the_entry_does_not_touch_says_so(self):
        self.load_of.return_value = self.LOADED
        why = self.rejected(("docs/a.md", "The word\n"))
        self.assertIn("the queue's lanes fail on this commit on top of origin/main: fix it and add it again. During gate "
                      "on this Mac, the one-minute load was highest 70, last 70 over 1 reading, and the CPU was at most "
                      "99% busy, against its busy limit of 75%.", why)
        self.assertIn(f"This entry changes no file of the failed tests {self.TEST}: neither their modules nor what those "
                      "import", why)

    def test_a_rejection_that_changes_the_failing_tests_file_names_it(self):
        changed = (self.repo / "tests/test_flaky.py").read_text() + "# changed\n"
        why = self.rejected(("tests/test_flaky.py", changed))
        self.assertIn(f"This entry changes tests/test_flaky.py, a file of the failed tests {self.TEST}", why)
        self.assertNotIn("changes no file", why)

    def test_a_rejection_that_changes_a_file_the_failing_test_imports_names_it(self):
        why = self.rejected(("src/python/fixturelib/helper.py", "from . import deeper\nVALUE = 1  # changed\n"))
        self.assertIn(f"This entry changes src/python/fixturelib/helper.py, a file of the failed tests {self.TEST}", why)
        self.assertNotIn("changes no file", why)

    def test_a_rejection_says_plainly_that_the_load_could_not_be_read(self):
        for name, setting in (("raises", {"side_effect": mq.remote.Unavailable("gone")}),
                              ("empty", {"return_value": (None, None, None), "side_effect": None})):
            with self.subTest(name):
                self.runs.unlink(missing_ok=True)
                self.load_of.configure_mock(**setting)
                why = self.rejected(("docs/a.md", f"The word {name}\n"))
                self.assertIn("During gate on this Mac, the one-minute load could not be read, and the CPU busy share "
                              "could not be read.", why)
                self.assertNotIn("highest", why)

    def test_a_rejection_of_a_gate_the_second_mac_ran_says_that_macs_load_and_never_this_macs(self):
        readable = lambda host: (12.0, 40.0, 75.0) if host == "fake-mac" else self.LOADED
        self.load_of.side_effect = readable
        with self.second_mac(): why = self.rejected(("docs/a.md", "The word\n"))
        self.assertIn("During gate on the second Mac (fake-mac), the one-minute load was highest 12, last 12 over 1 "
                      "reading, and the CPU was at most 40% busy, against its busy limit of 75%.", why)
        self.assertNotIn("highest 70", why)

    def test_a_rejection_from_a_second_mac_that_cannot_be_read_does_not_say_this_macs_load(self):
        def reading(host):
            if host == "fake-mac": raise mq.remote.Unavailable("gone")
            return self.LOADED
        self.load_of.side_effect = reading
        with self.second_mac(): why = self.rejected(("docs/a.md", "The word\n"))
        self.assertIn("During gate on the second Mac (fake-mac), the one-minute load could not be read", why)
        self.assertNotIn("highest", why)
        self.assertNotIn("this Mac", why)

    def test_a_rejection_says_plainly_when_the_tests_module_cannot_be_found(self):
        with patch.object(mq, "imported_files", return_value=None):
            why = self.rejected(("docs/a.md", "The word\n"))
        self.assertIn(f"{self.TEST} failed. The module of {self.TEST} cannot be found, so whether this entry changes their "
                      "files is unknown", why)
        self.assertNotIn("changes no file", why)

    def test_a_rejection_whose_commits_cannot_be_read_says_so_and_is_still_a_rejection(self):
        with patch.object(mq.Worker, "changed_by", side_effect=RuntimeError("git log failed")):
            why = self.rejected(("docs/a.md", "The word\n"))
        self.assertIn("Whether this entry changes a file of the failed tests could not be determined: git log failed", why)
        self.assertNotIn("changes no file", why)

    def test_the_entry_blame_sends_back_says_the_load_and_its_own_files_and_the_rest_land(self):
        self.load_of.return_value = self.LOADED
        self.plan.write_text("FF")
        self.commit("b", "b")
        self.commit("src/python/fixturelib/helper.py", "a", "from . import deeper\nVALUE = 1  # changed\n")
        ids = self.add("b", "a")
        with contextlib.redirect_stdout(io.StringIO()): self.assertEqual(self.work(), 0)
        entries = [self.state(identifier) for identifier in ids]
        self.assertEqual([entry["state"] for entry in entries], ["landed", "failed"])
        self.assertIn("Fix it and add it again. During gate on this Mac, the one-minute load was highest 70", entries[1]["why"])
        self.assertIn(f"This entry changes src/python/fixturelib/helper.py, a file of the failed tests {self.TEST}",
                      entries[1]["why"])
        self.assertNotIn("why", entries[0])

    def test_a_flake_in_a_batch_that_changes_a_file_its_module_imports_bisects_and_is_still_recorded(self):
        self.plan.write_text("F")
        self.commit("src/python/fixturelib/helper.py", "a", "from . import deeper\nVALUE = 1  # changed\n")
        self.commit("b", "b")
        ids = self.add("a", "b")
        with contextlib.redirect_stdout(io.StringIO()) as said: self.assertEqual(self.work(), 0)
        log = said.getvalue()
        self.assertIn(f"every failed test passed alone, but the batch reaches {self.TEST}, which changes "
                      "src/python/fixturelib/helper.py, which it imports: bisecting", log)
        self.assertIn("2 commits fail together: next the oldest 1", log)
        self.assertEqual([self.state(identifier)["state"] for identifier in ids], ["landed", "landed"])
        self.assertEqual(self.runs_of_the_test(), 1 + mq.RERUNS + 2)
        [tracked] = self.tracked
        self.assertIn("bisected", tracked["outcome"])
        self.assertTrue(all(self.state(identifier)["flakes"][0]["test"] == self.TEST for identifier in ids))
        self.assertNotIn(f"{mq.FLAKY}:", self.main_messages())

    def test_a_file_the_tests_module_does_not_import_itself_does_not_stop_the_landing(self):
        """Only the module's own imports count: deeper.py, which helper.py imports, is not one of them."""
        self.plan.write_text("F")
        self.commit("src/python/fixturelib/deeper.py", "a", "DEPTH = 2\n")
        [identifier] = self.add("a")
        with contextlib.redirect_stdout(io.StringIO()) as said: self.assertEqual(self.work(), 0)
        self.assertIn("landing the batch", said.getvalue())
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertIn(f"{mq.FLAKY}: {self.TEST}", self.tip_message())

    def test_a_host_that_does_not_settle_bisects_without_rerunning(self):
        self.plan.write_text("F")
        self.commit("a", "a"); self.commit("b", "b")
        ids = self.add("a", "b")
        with self.host([self.BUSY]) as (slept, said):
            self.assertEqual(self.work(), 0)
        self.assertIn(f"the failed tests are not rerun, as the host did not settle in {mq.SETTLE_LIMIT}s", said.getvalue())
        self.assertIn("2 commits fail together", said.getvalue())
        self.assertEqual(sum(slept), mq.SETTLE_LIMIT)
        self.assertEqual([self.state(identifier)["state"] for identifier in ids], ["landed", "landed"])
        self.assertEqual(self.runs_of_the_test(), 3)
        self.assertEqual(self.tracked, [])

    def test_more_failed_tests_than_a_flake_would_bisect_at_once(self):
        self.plan.write_text("F")
        self.commit("a", "a"); self.commit("b", "b")
        self.add("a", "b")
        with patch.object(mq, "RERUN_MOST", 0), contextlib.redirect_stdout(io.StringIO()) as said:
            self.assertEqual(self.work(), 0)
        self.assertIn("gate failed 1 tests, more than the 0 a flake would: bisecting", said.getvalue())
        self.assertEqual(self.runs_of_the_test(), 3)
        self.assertEqual(self.pressure.call_count, 0)

    def test_a_gate_that_is_not_eng_unittest_bisects_at_once_as_before(self):
        config = self.config()
        config["lanes"]["gate"] = {"command": ["python3", "lane.py", str(self.counter), str(self.status)]}
        self.publish(config)
        self.commit("a", "a"); self.commit("bad", "bad")
        ids = self.add("a", "bad")
        with contextlib.redirect_stdout(io.StringIO()) as said: self.assertEqual(self.work(), 0)
        self.assertEqual([self.state(identifier)["state"] for identifier in ids], ["landed", "failed"])
        self.assertNotIn("rerun", said.getvalue())
        self.assertEqual(self.pressure.call_count, 0)
        self.assertEqual(self.tracked, [])


UNITTEST_LOG = """queue: 12:00:00 gate on abc
== test_a.A.test_fine: OK (1 tests, 0.4s)
test_fine (test_a.A.test_fine) ... ok
== test_b.B: FAILED (failures=1, errors=1) (3 tests, 2.0s)
test_one (test_b.B.test_one) ... FAIL
test_two (test_b.B.test_two) ... ERROR
======================================================================
FAIL: test_one (test_b.B.test_one)
----------------------------------------------------------------------
AssertionError
======================================================================
ERROR: test_two (test_b.B.test_two) (case=1)
----------------------------------------------------------------------
RuntimeError
== test_c: FAILED (failures=1) (1 tests, 1.0s)
FAIL: test_old (test_c.C)
----------------------------------------------------------------------
eng unittest: 5 tests (0 skipped) in 3 processes, 4 at a time, 3.4s: FAILED: test_b.B (FAILED (failures=1, errors=1), status 1), test_c (FAILED (failures=1), status 1)
"""


class FlakyParts(unittest.TestCase):
    """The parts of #231's reruns alone: the failed tests a log names, the rerun's command, a test module's imports and
    the tracker, which a fake gh stands in for."""

    def test_the_failed_tests_are_read_from_each_failed_process_output(self):
        self.assertEqual(mq.failed_tests(UNITTEST_LOG), (["test_b.B.test_one", "test_b.B.test_two", "test_c.C.test_old"], None))

    def test_a_log_whose_failure_cannot_be_rerun_alone_names_why(self):
        summary = UNITTEST_LOG.splitlines()[-1]
        cases = {
            "its log has no eng unittest summary": UNITTEST_LOG.replace(summary, "FAILED"),
            "eng unittest's verdict is OK": UNITTEST_LOG.replace(summary.split("s: ", 1)[1], "OK"),
            "eng unittest also left tests unmeasured": UNITTEST_LOG.replace(summary, summary + "; UNMEASURED: test_d (timed out after 1800s)"),
            "test_c ran 0 of 1 tests": UNITTEST_LOG.replace("test_c (FAILED (failures=1), status 1)", "test_c (ran 0 of 1 tests)"),
            "the output of test_c names no failed test": UNITTEST_LOG.replace("FAIL: test_old (test_c.C)", "Segmentation fault"),
            "test_c did not load": UNITTEST_LOG.replace("FAIL: test_old (test_c.C)", "ERROR: test_c (unittest.loader._FailedTest.test_c)"),
        }
        for why, log in cases.items():
            with self.subTest(why=why):
                self.assertEqual(mq.failed_tests(log), (None, why))

    def test_a_path_is_named_only_whole_and_with_a_directory_or_an_extension(self):
        for output in ("docs/a.md:3: misspelling wrongword", "see docs/a.md.", "'docs/a.md'", "(docs/a.md)"):
            with self.subTest(output=output):
                self.assertTrue(mq.named_in("docs/a.md", output))
        # Negative controls: a longer path or name around it, and a bare word.
        for path, output in (("docs/a.md", "docs/a.mdx:1:"), ("docs/a.md", "old/docs/a.md:1:"),
                             ("docs/a.md", "docs/a.md.orig:1:"), ("a", "a failure"), ("README", "README:1:")):
            with self.subTest(path=path, output=output):
                self.assertFalse(mq.named_in(path, output))

    def test_the_rerun_keeps_the_suites_options_and_names_its_own_test(self):
        own = ["python3", "scripts/eng", "unittest", "-s", "tests", "--split", "test", "-v"]
        self.assertEqual(mq.rerun_command(own), ["python3", "scripts/eng", "unittest", "-s", "tests", "--split", "test", "-j", "1", "-v"])
        self.assertEqual(mq.rerun_command(["eng", "unittest", "-p", "test_x*.py", "-j", "8", "-k", "slow", "--timeout=60",
                                           "-t", ".", "test_x.Selected"]),
                         ["eng", "unittest", "-p", "test_x*.py", "--timeout=60", "-t", ".", "-j", "1", "-v"])
        batched = ["eng", "unittest", "--split", "class", "--batch-size", "4"]
        self.assertEqual(mq.rerun_command(batched + ["-j", "8"]), batched + ["-j", "1", "-v"])
        # Negative controls: a lane that is not eng unittest, an option the rerun cannot carry, a value left out.
        for command in (["python3", "lane.py"], ["python3", "scripts/eng", "xctest"], ["python3", "-m", "unittest"],
                        ["python3", "scripts/eng", "unittest", "--failfast"], ["python3", "scripts/eng", "unittest", "-s"],
                        None, "python3 scripts/eng unittest"):
            with self.subTest(command=command):
                self.assertIsNone(mq.rerun_command(command))

    def test_a_group_counts_as_its_one_eng_unittest_lane(self):
        suite = {"command": ["python3", "scripts/eng", "unittest", "-s", "tests", "-p", "test_a*.py"]}
        other = {"command": ["swift", "test"]}
        lanes = {"suite": suite, "suite2": suite, "other": other, "plain": suite,
                 "gate": {"parallel": ["suite", "other"]}, "two": {"parallel": ["suite", "suite2"]},
                 "none": {"parallel": ["other"]}}
        self.assertEqual(mq.unittest_member(lanes, "gate"), "suite")
        self.assertEqual(mq.unittest_member(lanes, "plain"), "plain")
        self.assertEqual(mq.unittest_lanes({"lanes": lanes, "queue": {"lanes": ["gate"]}}), {"gate": ("tests", ["test_a*.py"])})
        # Negative controls: two such lanes, none, and no lane at all give no one lane to rerun.
        for lane in ("two", "none", "gone"):
            with self.subTest(lane=lane):
                self.assertIsNone(mq.unittest_member(lanes, lane))

    def test_a_test_modules_files_are_itself_and_the_projects_files_it_imports_directly(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = {"tests/test_x.py": "import os\nfrom pkg import a\nimport pkg.b as bb\n"
                                        "def later():\n    from pkg.c import thing\n",
                     "tests/suite/__init__.py": "", "tests/suite/test_y.py": "from . import helper\nfrom .more import x\n",
                     "tests/suite/helper.py": "", "tests/suite/more.py": "x = 1\n",
                     "src/python/pkg/__init__.py": "", "src/python/pkg/a.py": "from pkg import d\n",
                     "src/python/pkg/b.py": "", "src/python/pkg/c.py": "thing = 1\n", "src/python/pkg/d.py": ""}
            for name, body in files.items():
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_text(body)
            self.assertEqual(mq.imported_files(root, root / "tests", "test_x.X.test_one"),
                             ["src/python/pkg/__init__.py", "src/python/pkg/a.py", "src/python/pkg/b.py",
                              "src/python/pkg/c.py", "tests/test_x.py"])
            self.assertEqual(mq.imported_files(root, root / "tests", "suite.test_y.Y.test_two"),
                             ["tests/suite/__init__.py", "tests/suite/helper.py", "tests/suite/more.py",
                              "tests/suite/test_y.py"])
            self.assertIsNone(mq.imported_files(root, root / "tests", "test_missing.X.test_one"))

    FLAKE = {"test": "test_b.B.test_one", "commit": "c" * 40, "gateLog": "/logs/gate.log",
             "rerunLogs": ["/logs/rerun-1.log", "/logs/rerun-2.log"], "outcome": "The batch landed.", "mac": "fixture-mac"}

    def gh(self, answers):
        """A fake gh: each call's answer by its first two words, recorded in calls."""
        calls = []
        def gh(*arguments):
            calls.append(arguments)
            code, out, err = answers[" ".join(arguments[:2])]
            return subprocess.CompletedProcess(["gh", *arguments], code, out, err)
        return gh, calls

    def test_the_tracker_comments_on_the_open_issue_that_names_the_test(self):
        gh, calls = self.gh({"issue list": (0, json.dumps([{"number": 230, "url": "https://x/issues/230", "title": "t"}]), ""),
                             "issue comment": (0, "https://x/issues/230#c\n", "")})
        self.assertEqual(mq.hardening(Path("."), self.FLAKE, gh), ("https://x/issues/230", None))
        self.assertEqual([call[:2] for call in calls], [("issue", "list"), ("issue", "comment")])
        self.assertIn('"test_one" in:title,body,comments', calls[0])
        self.assertEqual(calls[1][2], "230")
        body = calls[1][calls[1].index("--body") + 1]
        for said in ("`test_b.B.test_one`", "passed alone 2 of 2 times", "The batch landed.", "`/logs/gate.log`",
                     "`/logs/rerun-2.log`", "fixture-mac", "vocaro/vocaro-engineering#231"):
            self.assertIn(said, body)

    def test_the_tracker_files_a_hardening_issue_when_none_names_the_test(self):
        gh, calls = self.gh({"issue list": (0, "[]", ""), "issue create": (0, "https://x/issues/240\n", "")})
        self.assertEqual(mq.hardening(Path("."), self.FLAKE, gh), ("https://x/issues/240", None))
        self.assertEqual([call[:2] for call in calls], [("issue", "list"), ("issue", "create")])
        self.assertEqual(calls[1][calls[1].index("--title") + 1], "Harden the flaky test test_one (test_b.B)")

    def test_a_tracker_gh_cannot_reach_is_said_and_stops_nothing(self):
        for answers, why in (({"issue list": (1, "", "boom\n")}, "gh issue list failed: boom"),
                             ({"issue list": (0, "not json", "")}, "gh issue list printed no issue list"),
                             ({"issue list": (0, json.dumps([{"number": 1, "url": "u"}]), ""), "issue comment": (1, "", "no\n")},
                              "gh issue comment on u failed: no"),
                             ({"issue list": (0, "[]", ""), "issue create": (1, "", "denied\n")}, "gh issue create failed: denied")):
            with self.subTest(why=why):
                gh, _ = self.gh(answers)
                url, said = mq.hardening(Path("."), self.FLAKE, gh)
                self.assertIsNone(url)
                self.assertTrue(said.startswith(why), said)


class BothHalves(Fixture):
    """A bisect's two halves gate at once, the newer beside the older, one on each Mac (vocaro-engineering#241).
    Here both run on this Mac, as the fixture has no second Mac: HERE and BESIDE are no options, and every lane
    may go to the second Mac (offerable)."""
    def setUp(self):
        super().setUp()
        for name, value in (("HERE", ()), ("BESIDE", ()), ("offerable", lambda config, lanes: None)):
            patcher = patch.object(mq, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def worked(self):
        with contextlib.redirect_stdout(io.StringIO()) as said:
            self.assertEqual(self.work(), 0)
        return said.getvalue()

    @staticmethod
    def rounds(log):
        """The rounds the worker ran, each one gate of the batch or of a bisect's older half."""
        return sum(1 for line in log.splitlines() if " testing " in line and "newer half" not in line
                   and "of the bisect" not in line)

    def test_the_good_older_half_lands_and_the_newer_bisects_at_once(self):
        """[a b bad d] fail; [a b] and [bad d] gate at once, and a b land; [bad] and [d] at once: bad comes back and d
        lands. The same 5 gates as one half at a time, in 3 rounds rather than 5."""
        for name in ("a", "b", "bad", "d"): self.commit(name, name)
        ids = self.add("a", "b", "bad", "d")
        log = self.worked()
        self.assertEqual([self.state(i)["state"] for i in ids], ["landed", "landed", "failed", "landed"])
        self.assertEqual(self.gates(), 5)
        self.assertEqual(self.rounds(log), 3, log)
        self.assertIn("testing the newer 2 entries of the bisect", log)
        self.assertIn("newer half: 2 commits fail together here too: they bisect once the older half has", log)
        self.assertIn("testing the newer 1 entry of the bisect", log)
        self.assertNotIn("bad", self.main_files())

    def test_the_good_newer_half_lands_while_the_older_bisects(self):
        """[a bad c d] fail; [a bad] and [c d] at once: c d land first, ahead of a, and [a] and [bad] gate at once."""
        for name in ("a", "bad", "c", "d"): self.commit(name, name)
        ids = self.add("a", "bad", "c", "d")
        log = self.worked()
        self.assertEqual([self.state(i)["state"] for i in ids], ["landed", "failed", "landed", "landed"])
        self.assertEqual(self.gates(), 5)
        self.assertEqual(self.rounds(log), 3, log)
        subjects = run(self.repo, "git", "log", "--format=%s", "origin/main").splitlines()
        self.assertLess(subjects.index("Add a"), subjects.index("Add d"))  # a, newest first, landed after c and d

    def test_the_newer_halfs_rejection_says_the_load_and_that_its_tests_are_unknown(self):
        """The rejection Beside settles itself, from the half that ran beside the older one, adds what a lone one does."""
        for name in ("a", "bad", "c", "d"): self.commit(name, name)
        ids = self.add("a", "bad", "c", "d")
        self.worked()
        why = self.state(ids[1])["why"]
        self.assertIn("fix it and add it again. During gate on this Mac, the one-minute load was highest 3", why)
        self.assertIn("Which tests failed could not be determined (its log has no eng unittest summary), so whether this "
                      "entry changes their files is unknown", why)

    def test_two_that_pass_alone_land_the_older_and_the_newer_gates_again(self):
        """Each passes alone, and together they fail: the older lands, and the newer, whose result was of the main
        before it, gates again on top of it and comes back, as one half at a time does."""
        self.commit("left", "left"); self.commit("right", "right")
        ids = self.add("left", "right")
        log = self.worked()
        self.assertEqual([self.state(i)["state"] for i in ids], ["landed", "failed"])
        self.assertIn("passed beside the older half, which landed first", log)
        self.assertNotIn("right", self.main_files())

    def test_a_second_mac_that_declines_leaves_the_bisect_as_before(self):
        """Negative control: when the second Mac cannot start the newer half now, it waits behind the older half,
        and the bisect costs what it did, one half at a time: 5 gates in 5 rounds."""
        for name in ("a", "bad", "c", "d"): self.commit(name, name)
        ids = self.add("a", "bad", "c", "d")
        real = mq.Beside.gate
        def declined(worker, lanes, round_name, where=()):
            worker.ran_where = ""
            return mq.resources.BUSY, None
        with patch.object(mq.Beside, "gate", declined):
            log = self.worked()
        self.assertIs(mq.Beside.gate, real)
        self.assertEqual([self.state(i)["state"] for i in ids], ["landed", "failed", "landed", "landed"])
        self.assertEqual(self.gates(), 5)
        self.assertEqual(self.rounds(log), 5, log)
        self.assertIn(f"newer half: no verdict (exit {mq.resources.BUSY}): queued again, behind the older half", log)

    def test_a_lane_that_runs_only_here_keeps_one_half_at_a_time(self):
        """Negative control: a lane that runs only on this Mac (offerable, whose own cases the test below covers)
        sends no newer half beside the older, and the bisect is as before: 5 gates in 5 rounds."""
        self.commit("a", "a"); self.commit("bad", "bad"); self.commit("c", "c"); self.commit("d", "d")
        ids = self.add("a", "bad", "c", "d")
        why = "gate runs only on this Mac: the lane says: the fixture's reason"
        with patch.object(mq, "offerable", lambda config, lanes: why):
            log = self.worked()
        self.assertEqual([self.state(i)["state"] for i in ids], ["landed", "failed", "landed", "landed"])
        self.assertEqual(self.gates(), 5)
        self.assertEqual(self.rounds(log), 5, log)
        self.assertIn(f"the newer 2 of the bisect wait for the older half, since {why}", log)
        self.assertIn(f"the newer 1 of the bisect wait for the older half, since {why}", log)
        self.assertNotIn("newer half", log)

    def test_the_lanes_each_half_runs_say_where(self):
        """The older half runs with eng run's --here, the newer with --offload-only, which never runs it here."""
        self.assertEqual(OFFERED, (("--here",), ("--offload-only",)))
        self.assertEqual(OFFERABLE({"lanes": {"gate": {"command": ["x"], "resource": "heavy"}}}, ["gate"]), None)
        # A light gate, such as the toolkit's was, or one with no resource, is admitted and offered as every lane is
        # (the owner's decision, 2026-11-07), so its halves run one on each Mac; a self lane stays here.
        for resource in ({"resource": "light"}, {}):
            self.assertEqual(OFFERABLE({"lanes": {"gate": {"command": ["x"], **resource}}}, ["gate"]), None)
        self.assertEqual(OFFERABLE({"lanes": {"gate": {"command": ["x"], "resource": "self"}}}, ["gate"]),
                         "gate runs only on this Mac: a self lane runs where it starts, and its script admits each of "
                         "its parts")
        self.assertEqual(OFFERABLE({"lanes": {"gate": {"command": ["x"], "resource": "heavy", "here": "it builds the "
                                                      "app"}}}, ["gate"]), "gate runs only on this Mac: the lane says: "
                                                                              "it builds the app")
        self.assertEqual(OFFERABLE({"lanes": {}}, ["gate"]), "gate runs only on this Mac: it is no configured lane")

    def test_a_newer_entry_that_needs_an_older_one_waits_rather_than_come_back(self):
        """A newer entry that conflicts with origin/main on its own may need an older entry of the bisect: it is
        not sent back, as a conflict in a round would be, but waits for the older half."""
        worker = mq.Worker(mq.Queue(self.repo))
        beside = mq.Beside(worker)
        entry = {"id": "x"}
        with patch.object(mq.Queue, "settle") as settle:
            beside.left_out(entry, mq.CONFLICT, why="conflicts")
            settle.assert_not_called()
            beside.left_out(entry, mq.LANDED, why="held")
            settle.assert_called_once()
        self.assertEqual(beside.unreachable, [entry])
        self.assertEqual(beside.checkout.name, f"{self.repo.name}{mq.BESIDE_SUFFIX}")


class PooledStack(Fixture):
    def test_a_stack_tries_build_macs_other_than_the_one_running_the_batch(self):
        worker = mq.Worker(mq.Queue(self.repo))
        worker.lane_now = ("gate", time.time(), ())
        worker.environment = {}
        waiting = [{"id": "new", "state": mq.QUEUED}]
        config = {"lanes": {"gate": {"resource": "heavy"}}}
        for mac, where, excluded in [
            (mq.lane_wait.HERE, mq.BESIDE, None),
            (mq.lane_wait.second_mac("gate-03"), ("--if-free",), "gate-03"),
        ]:
            record = {"mac": mac, "kind": None, "pid": os.getpid(), "admitted": False}
            with self.subTest(mac=mac), patch.dict(os.environ, {
                mq.remote.HOST_VARIABLE: "build-host", mq.remote.GATE_HOSTS: "build-host,gate-03,gate-04"
            }), patch.object(worker.queue, "waiting", return_value=waiting), \
                    patch.object(mq.lane_wait, "current", return_value=record), \
                    patch.object(mq, "Stack") as stack:
                worker.stack(worker, config, ["gate"], "head", [], threading.Event(), set())
                stack.assert_not_called()
                record["admitted"] = True
                worker.stack(worker, config, ["gate"], "head", [], threading.Event(), set())
                runner = stack.call_args.args[1]
                self.assertEqual(stack.call_args.args[5], where)
                self.assertEqual(runner.environment.get(mq.remote.QUEUE_GATE_EXCLUDE), excluded)
            worker.pending = None


class Stacked(Fixture):
    """Entries that arrive while a batch gates gate stacked on its tip, on the other Mac, and land once it has,
    without a gate of their own; a batch that does not land throws that gate away (vocaro-engineering#241). Both
    run on this Mac here, as in BothHalves."""
    PATIENCE = 600  # seconds a step may take before the test gives up on it, which only a regression uses
    EVERY_RUN = range(1, 6)  # more gates than any of these rounds starts

    def setUp(self):
        super().setUp()
        for name, value in (("HERE", ()), ("BESIDE", ()), ("offerable", lambda config, lanes: None),
                            ("STACK_POLL", 0.1)):
            patcher = patch.object(mq, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        starting = patch.object(mq.Queue, "start")
        starting.start()
        self.addCleanup(starting.stop)

    def release(self, runs):
        """The lanes of these gates, counted from 1 in the order they started, may end (hold, in LANE)."""
        for number in runs: Path(f"{self.counter}.release.{number}").touch()

    def worked(self, branch, then, *more, checked=True, ends=EVERY_RUN):
        """The log of a worker whose first gate's lane holds until branch has arrived, as a session's eng land adds
        it, unchecked with no trial integration, as when the batch ahead changes after it (Fixture.add), and then(log)
        is true: only then may the gates numbered in ends finish. No wall-clock wait decides what the round sees, so
        a stalled host only makes it slower (vocaro-engineering#250); PATIENCE ends what a regression would hold."""
        failures, finished = [], threading.Event()
        def until(happened, what):
            deadline = time.monotonic() + stretched(self.PATIENCE)
            while not happened():
                if finished.is_set(): raise AssertionError(f"the round ended before {what}")
                if time.monotonic() > deadline: raise AssertionError(f"no {what} within {self.PATIENCE}s, stretched")
                time.sleep(0.02)
        def drive():
            try:
                until(lambda: self.gates() >= 1, "first gate's lane started")
                with patch.object(mq, "check") if not checked else contextlib.nullcontext():
                    mq.add(self.repo, branch)
                until(lambda: then(said.getvalue()), f"sign that the worker had looked at {branch}")
                self.release(ends)
                until(finished.is_set, "end of the round")
            except Exception as failure: failures.append(failure)
            finally: self.release(self.EVERY_RUN)  # so that a failing round ends too, with its log
        with contextlib.redirect_stdout(io.StringIO()) as said, contextlib.ExitStack() as stack:
            for context in more: stack.enter_context(context)
            driver = threading.Thread(target=drive, name="driver", daemon=True)
            driver.start()
            try: status = self.work()
            finally:
                finished.set()
                driver.join()
        if failures: raise failures[0]
        self.assertEqual(status, 0, said.getvalue())
        return said.getvalue()

    def stacked_started(self, log):
        """The stacked gate's lane runs: the batch's and its own have started."""
        return self.gates() >= 2

    def added(self, branch):
        return next(entry for entry in mq.Queue(self.repo).all() if entry["branch"] == branch)

    def test_stack_waits_for_the_final_lanes_admission_before_choosing_the_other_mac(self):
        self.commit("a", "a"); self.commit("b", "b")
        first, second = self.add("a", "b")
        worker = mq.Worker(mq.Queue(self.repo))
        lanes = ["spelling", "gate"]
        args = (worker, self.config(), lanes, self.state(first)["tip"], [self.state(first)], threading.Event(), set())
        waits = mq.lane_wait
        spelling = waits.path_of(worker.checkout, "spelling")
        gate = waits.path_of(worker.checkout, "gate")
        with (patch.multiple(mq, HERE=("--here",), BESIDE=("--offload-only",)),
              patch.object(mq, "Stack") as stack,
              patch.dict(os.environ, {waits.VARIABLE: str(spelling)})):
            worker.stack(*args)
            stack.assert_not_called()
            worker.lane_now = ("spelling", time.time(), ())
            waits.running()
            worker.stack(*args)
            stack.assert_not_called()

            worker.lane_now = ("gate", time.time(), ())
            os.environ[waits.VARIABLE] = str(gate)
            waits.sent("build-host")
            worker.stack(*args)
            stack.assert_not_called()
            waits.relayed(b'{"kind":"admission","reason":"busy"}', "build-host")
            worker.stack(*args)
            stack.assert_not_called()
            waits.relayed(b"null", "build-host")
            worker.stack(*args)
            stack.assert_not_called()
            waits.relayed(b'{"kind":null,"admitted":true}', "build-host")
            worker.stack(*args)
            stack.assert_called_once()
            self.assertEqual(stack.call_args.args[-1], mq.HERE)
            self.assertEqual([entry["id"] for entry in stack.call_args.args[2]], [second])

            worker.pending = None
            stack.reset_mock()
            waits.running()
            worker.stack(*args)
            stack.assert_called_once()
            self.assertEqual(stack.call_args.args[-1], mq.BESIDE)

    def test_an_entry_that_arrives_during_a_gate_lands_on_its_gate_stacked_on_the_batch(self):
        self.commit("hold", "a"); self.commit("b", "b")
        [first] = self.add("a")
        log = self.worked("b", self.stacked_started)
        b = self.added("b")
        self.assertEqual((self.state(first)["state"], b["state"]), ("landed", "landed"), log)
        self.assertEqual(self.gates(), 2, log)
        self.assertIn("that arrived during the gate", log)
        self.assertIn("goes on as the next round, as that is origin/main now", log)
        self.assertIn("whose gate began on top of the batch that just landed", log)
        # b went on top of a, and its gate was of exactly that commit.
        self.assertEqual(run(self.repo, "git", "rev-parse", f"{b['landedAs']}^"), self.state(first)["landedAs"])
        self.assertEqual(b["gatedOn"], self.state(first)["landedAs"])
        self.assertLessEqual({"hold", "b"}, self.main_files())

    def test_a_docs_entry_that_arrives_during_a_code_gate_lands_beside_it_on_the_docs_lanes(self):
        """vocaro-engineering#394: a docs-only entry that arrives while a code batch gates lands beside the gate, on the
        docs lanes, and the batch's pass carries over the document main gained. The old worker stacked it on the
        batch's tip and gated it with every lane."""
        self.commit("hold", "a"); self.commit("doc/guide.md", "guide", "# Guide\n")
        [first] = self.add("a")
        # The old worker's sign, a stacked gate starting, ends the wait too, so that it fails rather than waits.
        log = self.worked("guide", lambda log: "docs beside the gate: pushed" in log or self.stacked_started(log))
        guide = self.added("guide")
        self.assertEqual((self.state(first)["state"], guide["state"]), ("landed", "landed"), log)
        self.assertEqual(self.gates(), 1, log)
        self.assertEqual(guide["docsOnly"], ["spelling", "docs"], log)
        self.assertNotIn("gatedOn", guide)
        self.assertNotIn("that arrived during the gate", log)
        self.assertIn("carried the result over 1 file(s) on main: doc/guide.md", log)
        # The document landed first, and the batch on top of it.
        self.assertEqual(run(self.repo, "git", "rev-parse", f"{self.state(first)['landedAs']}^"), guide["landedAs"])
        self.assertLessEqual({"hold", "doc"}, self.main_files())

    def test_a_stacked_gate_goes_on_over_a_document_landed_beside_the_gate_below_it(self):
        """A code entry stacked on the batch, and then a docs entry landed beside the gate: the batch carries its
        result over the document, and so does the stacked gate, which lands with no gate of its own."""
        self.commit("hold", "a"); self.commit("b", "b"); self.commit("doc/guide.md", "guide", "# Guide\n")
        [first] = self.add("a")
        added = []
        def then(log):
            if not self.stacked_started(log): return False
            if not added:
                with patch.object(mq, "check"): mq.add(self.repo, "guide")
                added.append(True)
            return "docs beside the gate: pushed" in log
        log = self.worked("b", then)
        b, guide = self.added("b"), self.added("guide")
        self.assertEqual([self.state(first)["state"], b["state"], guide["state"]], ["landed"] * 3, log)
        self.assertEqual(self.gates(), 2, log)
        self.assertEqual(guide["docsOnly"], ["spelling", "docs"], log)
        self.assertIn("as origin/main is that tip with documents its result carries over", log)
        self.assertEqual(log.count("carried the result over 1 file(s) on main: doc/guide.md"), 2, log)
        self.assertLessEqual({"hold", "b", "doc"}, self.main_files())

    def test_a_batch_that_fails_throws_away_the_gate_stacked_on_it(self):
        """The batch below fails: its stacked gate is canceled, and the entry gates again on main and lands."""
        self.commit("bad", "a", "x"); self.commit("hold", "a", start="a"); self.commit("b", "b")
        [first] = self.add("a")
        log = self.worked("b", self.stacked_started, ends=(1,))  # the stacked gate's lane ends only by its cancel
        self.assertEqual((self.state(first)["state"], self.added("b")["state"]), ("failed", "landed"), log)
        self.assertIn("is thrown away, since the batch below it did not land at the tip it was stacked on: queued "
                      "again", log)
        # Canceled, rather than run out on top of a's: its lane counts once it has started.
        self.assertIn("stacked: gate: unmeasured: Owned command was canceled", log)
        self.assertEqual(self.gates(), 3, log)
        self.assertNotIn("hold", self.main_files())

    def test_a_lane_that_runs_only_here_stacks_nothing(self):
        """Negative control: with a lane that runs only on this Mac, the entry waits for the batch and gates on its
        own, as before."""
        self.commit("hold", "a"); self.commit("b", "b")
        [first] = self.add("a")
        why = "gate runs only on this Mac: the lane says: the fixture's reason"
        log = self.worked("b", lambda log: f"since {why}" in log,
                          patch.object(mq, "offerable", lambda config, lanes: why))
        self.assertEqual((self.state(first)["state"], self.added("b")["state"]), ("landed", "landed"), log)
        self.assertEqual(self.gates(), 2, log)
        self.assertEqual(log.count(f"the entries behind the batch wait for it, since {why}"), 1, log)
        self.assertNotIn("that arrived during the gate", log)

    def test_a_stacked_gate_the_second_mac_declines_is_thrown_away_and_the_entry_gates_after(self):
        """Negative control: a second Mac that cannot start it gives no verdict, which is thrown away, so the entry
        gates after the batch, as before."""
        self.commit("hold", "a"); self.commit("b", "b")
        [first] = self.add("a")
        declined_log = self.base / "declined-gate.log"
        def declined(worker, lanes, round_name, where=(), cancel=None):
            declined_log.write_text("nothing ran\n")
            return mq.resources.BUSY, declined_log
        log = self.worked("b", lambda log: "since it gave no verdict" in log,
                          patch.object(mq.Beside, "gate", declined),
                          patch.object(mq, "STACK_ADMISSION_BACKOFF", stretched(self.PATIENCE)))
        self.assertEqual((self.state(first)["state"], self.added("b")["state"]), ("landed", "landed"), log)
        self.assertEqual(self.gates(), 2, log)
        self.assertIn(f"is thrown away, since it gave no verdict (exit {mq.resources.BUSY}): queued again", log)
        self.assertNotIn("goes on as the next round", log)
        self.assertEqual(log.count("that arrived during the gate"), 1, log)
        self.assertIn(mq.STACK_ADMISSION_DECLINED, declined_log.read_text())

    def test_a_stacked_gate_is_offered_again_only_after_its_admission_backoff(self):
        self.commit("a", "a"); self.commit("b", "b")
        first, second = self.add("a", "b")
        worker = mq.Worker(mq.Queue(self.repo))
        dropped = []
        worker.pending = SimpleNamespace(done=lambda: True, void=lambda: "busy", not_admitted=lambda: True,
                                         drop=dropped.append)
        config, lanes, head = self.config(), ["gate"], self.state(first)["tip"]
        worker.lane_now = ("gate", time.time(), ())
        path = mq.lane_wait.path_of(worker.checkout, "gate")
        with patch.dict(os.environ, {mq.lane_wait.VARIABLE: str(path)}):
            mq.lane_wait.running()
        arguments = (worker, config, lanes, head, [self.state(first)], threading.Event(), set())
        with patch.object(mq.time, "monotonic", return_value=100) as clock, patch.object(mq, "Stack") as stack:
            worker.stack(*arguments)
            self.assertEqual(dropped, ["busy"])
            stack.assert_not_called()
            clock.return_value += mq.STACK_ADMISSION_BACKOFF - 1
            worker.stack(*arguments)
            stack.assert_not_called()
            clock.return_value += 1
            worker.stack(*arguments)
        stack.assert_called_once()
        self.assertEqual([entry["id"] for entry in stack.call_args.args[2]], [second])
        stack.return_value.begin.assert_called_once()

    def test_a_stacked_gate_admitted_on_retry_gives_the_verdict(self):
        self.commit("hold", "a"); self.commit("b", "b")
        [first] = self.add("a")
        attempts, gate = [], mq.Beside.gate
        def offer(worker, lanes, round_name, where=(), cancel=None):
            attempts.append(round_name)
            if len(attempts) == 1: return mq.resources.BUSY, None
            return gate(worker, lanes, round_name, where=where, cancel=cancel)
        # The preceding fixture checks the wait itself with a monotonic clock.
        log = self.worked("b", self.stacked_started, patch.object(mq, "STACK_ADMISSION_BACKOFF", 0),
                          patch.object(mq.Beside, "gate", offer))
        b = self.added("b")
        self.assertEqual((self.state(first)["state"], b["state"]), ("landed", "landed"), log)
        self.assertEqual(len(attempts), 2, log)
        self.assertEqual(self.gates(), 2, log)
        self.assertIn("whose gate began on top of the batch that just landed", log)
        self.assertEqual(b["gatedOn"], self.state(first)["landedAs"])

    def test_an_adopted_stack_refused_admission_is_gated_normally_without_holding(self):
        config = self.config()
        config["queue"]["noVerdictRuns"] = 1
        self.publish(config)
        self.commit("hold", "a"); self.commit("b", "b")
        [first] = self.add("a")
        offered, adopted = threading.Event(), threading.Event()
        begun = mq.Worker.begun
        def began(worker, *args, **kwargs):
            begun(worker, *args, **kwargs)
            if kwargs.get("adopted"): adopted.set()
        def declined(worker, lanes, round_name, where=(), cancel=None):
            offered.set()
            self.assertTrue(adopted.wait(stretched(self.PATIENCE)), "the batch never adopted the stacked gate")
            return mq.resources.BUSY, None
        log = self.worked("b", lambda _: offered.is_set(), patch.object(mq.Worker, "begun", began),
                          patch.object(mq.Beside, "gate", declined))
        self.assertEqual((self.state(first)["state"], self.added("b")["state"]), ("landed", "landed"), log)
        self.assertIn("whose gate began on top of the batch that just landed", log)
        self.assertEqual(self.gates(), 2, log)
        self.assertIsNone(mq.Queue(self.repo).held(), log)

    def test_an_entry_that_conflicts_with_the_batch_is_not_stacked_or_sent_back_by_it(self):
        """A stacked gate settles nothing: an entry that conflicts with the batch below waits, and its own round
        sends it back."""
        self.commit("same", "a", "a"); self.commit("hold", "a", start="a"); self.commit("same", "c", "c")
        [first] = self.add("a")
        log = self.worked("c", lambda log: "ahead of it, not with origin/main" in log, checked=False)
        c = self.added("c")
        self.assertEqual((self.state(first)["state"], c["state"]), ("landed", "conflict"), log)
        self.assertNotIn("that arrived during the gate", log)
        self.assertEqual(self.gates(), 1, log)
        # Its stacked look names the entry below it, which it conflicts with, not origin/main (#247).
        self.assertIn(f"stacked: {c['id']}: ", log)
        self.assertIn(f"conflicts with {first}, ahead of it, not with origin/main, in same: once it lands", log)
        self.assertNotIn(f"stacked: {c['id']}: {c['tip'][:12]} conflicts with origin/main", log)


class RemovedWhileTesting(Fixture):
    """`eng queue remove` of an entry whose gate runs stops that gate, by the cancel a thrown-away stacked gate's lanes
    end with, and what is left gates again; nothing stacks on a batch an entry of which has left (#247)."""
    SLOW = "60"  # seconds the gate of the removed entry would take, far past what the test waits

    def setUp(self):
        super().setUp()
        for name, value in (("HERE", ()), ("BESIDE", ()), ("offerable", lambda config, lanes: None),
                            ("STACK_POLL", 0.1), ("REMOVAL_POLL", 0.1)):
            patcher = patch.object(mq, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        starting = patch.object(mq.Queue, "start")
        starting.start()
        self.addCleanup(starting.stop)
        # The removal lands on the gate lane these tests time, not on the toolkit's spelling check ahead of it, which a
        # loaded Mac can stretch past the moment a test removes the entry.
        config = json.loads((self.repo / "engineering.json").read_text())
        self.publish({**config, "queue": {**config["queue"], "spelling": False}}, "Turn the toolkit's spelling off")

    def remove(self, identifier):
        """Remove identifier as `eng queue remove` does from another process while the worker runs, whose lines
        go to the worker's log here, as stdout is one for both."""
        mq.remove(mq.Queue(self.repo), identifier)

    def arrive(self, branch):
        """Add branch as a session's eng land does, without its trial, as when the batch ahead changes after it."""
        with patch.object(mq, "check"): mq.add(self.repo, branch)
        return self.added(branch)["id"]

    def during(self, action, after=0.5):
        """action runs on its own thread after seconds of the first gate, as a session's commands do."""
        real, first = mq.Worker.gate, []
        def gate(worker, lanes, round_name, where=(), cancel=None):
            if not first:
                first.append(True)
                timer = threading.Timer(after, action)
                timer.start()
                self.addCleanup(timer.join)
            return real(worker, lanes, round_name, where, cancel)
        return patch.object(mq.Worker, "gate", gate)

    def added(self, branch):
        return next(entry for entry in mq.Queue(self.repo).all() if entry["branch"] == branch)

    def worked(self, *more):
        with contextlib.redirect_stdout(io.StringIO()) as said, contextlib.ExitStack() as stack:
            for context in more: stack.enter_context(context)
            began = time.monotonic()
            self.assertEqual(self.work(), 0)
        return said.getvalue(), time.monotonic() - began

    def test_removing_a_testing_entry_stops_its_gate_and_the_rest_lands(self):
        self.commit("slow", "a", self.SLOW); self.commit("b", "b")
        a, b = self.add("a", "b")
        run = mq.processes.run
        stop_times = []

        def observe_stop(command, *, cancel=None, **options):
            if not isinstance(cancel, mq.Removal) or a not in cancel.ids:
                return run(command, cancel=cancel, **options)
            is_set, removed = cancel.is_set, None

            def remove_once_running():
                nonlocal removed
                # The fixture writes the counter after the lane has started, before it sleeps.
                if removed is None and self.counter.exists():
                    self.remove(a)
                    removed = time.monotonic()
                return is_set()

            with patch.object(cancel, "is_set", remove_once_running):
                try:
                    return run(command, cancel=cancel, **options)
                except mq.processes.Canceled:
                    self.assertIsNotNone(removed)
                    stop_times.append(time.monotonic() - removed)
                    raise

        log, _ = self.worked(patch.object(mq.processes, "run", observe_stop))
        self.assertEqual(len(stop_times), 1, log)
        self.assertLess(stop_times[0], float(self.SLOW) / 2, log)
        self.assertEqual((self.state(a)["state"], self.state(b)["state"]), ("removed", "landed"), log)
        self.assertRegex(log, f"gate: stopped[^\\n]*, as {a} left the queue")
        self.assertIn(f"{a} left the queue, which stopped the gate: gating what is left", log)
        self.assertNotIn("unmeasured", log)
        self.assertNotIn("no verdict", log)
        self.assertIsNone(mq.Queue(self.repo).held())
        self.assertNotIn("slow", self.main_files())
        self.assertIn("b", self.main_files())
        # What `eng queue remove` said: the worker stops the gate, rather than letting it hold the queue.
        self.assertIn(f"removed {a}\n", log)
        self.assertIn(f"{a} was testing: the worker stops its gate within about 0.1s", log)

    def test_a_replacement_that_conflicts_only_with_the_removed_entry_is_not_stacked_on_it(self):
        """The case of #247: the replacement, based on origin/main, changes the removed entry's file. It stacks on
        nothing, is told it conflicts with the removed entry rather than origin/main, and lands once that entry's gate
        has stopped."""
        # a is removed once the stacker has looked at c and said it conflicts with a, while a's gate still runs: a
        # condition observed, not a time guessed, so that a loaded Mac's slow look cannot fall after the removal
        # (vocaro-engineering#381). The stacker looks only once a's final lane runs admitted (Worker.admitted_mac),
        # which on a Mac loaded far past its cores came more than 15 s after c arrived; the wait ends at the look, and
        # its bound, a third of a's gate, only ends a failing run, with a removed either way. a's gate is twice the
        # class's, which a stopped gate never costs.
        slow = 2 * float(self.SLOW)
        self.commit("slow", "a", str(int(slow))); self.commit("same", "a", "a", start="a"); self.commit("same", "c", "c")
        [a] = self.add("a")
        conflicted, looked, real_say = threading.Event(), [], mq.Worker.say
        def say(worker, words):
            real_say(worker, words)
            if words.startswith("stacked: ") and f" conflicts with {a}, ahead of it" in words: conflicted.set()
        def replace():
            self.arrive("c")
            seen = conflicted.wait(slow / 3)
            looked.append((seen, self.state(a)["state"]))
            self.remove(a)
        log, took = self.worked(self.during(replace), patch.object(mq.Worker, "say", say))
        c = self.added("c")
        self.assertEqual(looked, [(True, mq.TESTING)], log)
        self.assertLess(took, slow / 2, log)
        self.assertEqual((self.state(a)["state"], c["state"]), ("removed", "landed"), log)
        self.assertIn(f"stacked: {c['id']}: {c['tip'][:12]} conflicts with {a}, ahead of it, not with "
                      "origin/main, in same: once it lands, rebase onto origin/main and add it again", log)
        self.assertNotIn("conflicts with origin/main", log)
        self.assertNotIn("that arrived during the gate", log)
        self.assertRegex(log, f"gate: stopped[^\\n]*, as {a} left the queue")

    def test_removing_a_queued_entry_leaves_the_gate_running(self):
        """Negative control: removing an entry that waits behind the batch, not stacked on it, stops nothing; the
        batch lands, and remove says nothing of a gate."""
        self.commit("slow", "a", "2"); self.commit("b", "b")
        [a] = self.add("a")
        with patch.object(mq, "STACK_POLL", 30):  # b stays queued, not stacked
            log, _ = self.worked(self.during(lambda: self.remove(self.arrive("b")), after=0.3))
        b = self.added("b")
        self.assertEqual((self.state(a)["state"], b["state"]), ("landed", "removed"), log)
        self.assertIn(f"removed {b['id']}\n", log)
        self.assertNotIn("left the queue", log)
        self.assertNotIn("was testing", log)
        self.assertEqual(self.gates(), 1, log)

    def test_remove_says_what_becomes_of_a_testing_entrys_gate(self):
        """Its line after `removed ID`: only for an entry that was testing."""
        self.commit("a", "a"); self.commit("b", "b")
        a, b = self.add("a", "b")
        queue = mq.Queue(self.repo)
        queue.mark([{"id": a}], mq.TESTING)
        queue.record({"id": a}, stopsWhenRemoved=5)
        with contextlib.redirect_stdout(io.StringIO()) as said, patch.object(mq.Queue, "working", return_value=True):
            mq.remove(queue, a)
            mq.remove(queue, b)
        self.assertEqual(said.getvalue(), f"removed {a}\n{a} was testing: the worker stops its gate within about 5s, "
                         "on this Mac or the second, which frees its admission there, and gates the rest of its batch "
                         f"again\nremoved {b}\n")

    def test_what_remove_says_of_a_testing_entry(self):
        """A worker that records stopsWhenRemoved stops the gate; an older one lets it run on, which remove says,
        with how long it may hold the queue; and with no worker, no gate is left."""
        self.commit("a", "a")
        [a] = self.add("a")
        queue = mq.Queue(self.repo)
        entry = dict(queue.read(a), state=mq.TESTING)
        with patch.object(mq.Queue, "working", return_value=True):
            self.assertIn("stops its gate within about 5s", mq.stopping(queue, dict(entry, stopsWhenRemoved=5)))
            older = mq.stopping(queue, entry)
        self.assertIn("does not stop a gate for a removal: its gate runs on until its lanes end, at most 6 h a lane, "
                      "and holds the queue until then", older)
        with patch.object(mq.Queue, "working", return_value=False):
            self.assertIn("no worker runs, so no gate of it is left to stop", mq.stopping(queue, entry))


class RoundAccount(Fixture):
    """The worker's account of its round in eng-queue (ROUND_FILE), which `eng queue status` shows so that nobody has
    to read worker.log to learn why entries wait: a bisect's halves, each gate's lane, Mac and time, and what it waits
    for."""
    UNREACHABLE = ("eng: gate waits for this Mac, and asks the second Mac again at the next offer: build-host cannot "
                   "be reached: ssh: connect to host build-host port 22: No route to host")
    WHOLE_HOST = ("heavy: waiting (0s): it waits behind a whole-host lane holding the host: StudyWright gate")

    def setUp(self):
        super().setUp()
        for name, value in (("HERE", ()), ("BESIDE", ()), ("offerable", lambda config, lanes: None)):
            patcher = patch.object(mq, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def shown(self, as_json=False, working=True):
        with patch.object(mq.Queue, "working", return_value=working), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            mq.status(self.repo, as_json=as_json)
        return json.loads(out.getvalue()) if as_json else out.getvalue()

    def watch(self, began=None):
        """A GateWatch of a gate lane, and the path of the lane's wait record."""
        queue = mq.Queue(self.repo)
        worker = mq.Worker(queue)
        worker.gating = ["e1"]
        log = queue.logs / "round-gate.log"
        log.write_text("")
        with patch.object(mq.Worker, "head", return_value="a" * 40):
            watch = mq.GateWatch(worker, "gate", time.time() - 90 if began is None else began, log)
        self.addCleanup(watch.stopped.set)
        record = mq.lane_wait.path_of(worker.checkout, "gate")
        patcher = patch.dict(os.environ, {mq.lane_wait.VARIABLE: str(record)})
        patcher.start()
        self.addCleanup(patcher.stop)
        return queue, watch

    def gate_after_look(self, queue, watch):
        with patch.object(mq.remote, "dispatched", return_value=None): watch.look()
        return queue.round_state()["gates"]["batch"]

    def test_a_gate_record_follows_the_lanes_wait_record(self):
        queue, watch = self.watch()
        mq.lane_wait.waiting(mq.lane_wait.UNREACHABLE, "ssh: connect to host build-host port 22: No route to host")
        gate = self.gate_after_look(queue, watch)
        self.assertEqual((gate["lane"], gate["entries"], gate["mac"]), ("gate", ["e1"], None))
        self.assertEqual(gate["waiting"], "the second Mac cannot be reached, and is asked again at the next offer: "
                                          "ssh: connect to host build-host port 22: No route to host")
        self.assertIsInstance(gate["waitingSince"], float)
        # What it waits for by kind, which eng queue doctor's pointer to `eng admissions now` reads (#342).
        self.assertEqual(gate["waitingKind"], mq.lane_wait.UNREACHABLE)
        mq.lane_wait.waiting(mq.lane_wait.ADMISSION, "all 3 guaranteed slots are busy")
        self.assertEqual(self.gate_after_look(queue, watch)["waitingKind"], mq.lane_wait.ADMISSION)
        mq.lane_wait.running()
        gate = self.gate_after_look(queue, watch)
        self.assertEqual((gate["mac"], gate["waiting"], gate["waitingSince"], gate["waitingKind"]),
                         ("this Mac", None, None, None))
        # A lane that a dispatch sent to the second Mac runs there.
        with patch.object(mq.remote, "dispatched", return_value="build-host"): watch.look()
        self.assertEqual(queue.round_state()["gates"]["batch"]["mac"], "the second Mac (build-host)")
        # While its run goes on, its dispatch's folder has no name yet (remote.file_results): the record says where.
        mq.lane_wait.sent("build-host")
        self.assertEqual(self.gate_after_look(queue, watch)["mac"], "the second Mac (build-host)")
        watch.finish()
        self.assertEqual(queue.round_state()["gates"], {})

    def test_a_wait_on_the_second_mac_is_shown_and_names_the_mac(self):
        """The issue's first acceptance: its wait comes back as a record, and no log line says it."""
        queue, watch = self.watch()
        mq.lane_wait.sent("build-host")
        mq.lane_wait.relayed(json.dumps({"kind": mq.lane_wait.ADMISSION, "reason": "all 3 guaranteed slots are busy"}),
                             "build-host")
        gate = self.gate_after_look(queue, watch)
        self.assertEqual((gate["mac"], gate["waiting"]),
                         ("the second Mac (build-host)", "admission on the second Mac (build-host): all 3 guaranteed "
                                                         "slots are busy"))
        said = self.shown()
        self.assertIn("gate lane on the second Mac (build-host), running", said)
        self.assertRegex(said, r"waiting: admission on the second Mac \(build-host\): all 3 guaranteed slots are busy, "
                               r"for \d+s")
        mq.lane_wait.relayed(b"null", "build-host")
        gate = self.gate_after_look(queue, watch)
        self.assertEqual((gate["mac"], gate["waiting"]), ("the second Mac (build-host)", None))
        self.assertNotIn("waiting:", self.shown())

    def test_status_reads_the_record_and_no_printed_line(self):
        """The issue's second acceptance: what eng run prints may be reworded, or not printed at all."""
        queue, watch = self.watch()
        watch.log.write_text("\n".join((self.WHOLE_HOST, self.UNREACHABLE, "eng: trying gate on this Mac: busy",
                                        "remote: sending 0123456789ab to build-host")) + "\n")
        gate = self.gate_after_look(queue, watch)
        self.assertEqual((gate["mac"], gate["waiting"]), (None, None))
        mq.lane_wait.waiting(mq.lane_wait.ADMISSION, "it waits behind a whole-host lane holding the host: X")
        watch.log.write_text("heavy: slot 1 after 3s: fixture gate\n")  # says it was admitted, and is not read
        self.assertEqual(self.gate_after_look(queue, watch)["waiting"],
                         "admission on this Mac: it waits behind a whole-host lane holding the host: X")

    def test_a_lane_admitted_at_once_shows_no_wait(self):
        """Negative control: never having waited, nothing says it did."""
        queue, watch = self.watch()
        mq.lane_wait.running()
        gate = self.gate_after_look(queue, watch)
        self.assertEqual((gate["mac"], gate["waiting"], gate["waitingSince"], gate["stranded"]),
                         ("this Mac", None, None, False))
        self.assertNotIn("waiting:", self.shown())

    def test_a_wait_an_earlier_run_or_a_dead_process_left_is_not_shown(self):
        """A run that has moved on shows no stale wait."""
        queue, watch = self.watch(began=time.time() + 3600)  # a record written before this run's start
        mq.lane_wait.waiting(mq.lane_wait.ADMISSION, "all 3 guaranteed slots are busy")
        self.assertIsNone(self.gate_after_look(queue, watch)["waiting"])
        queue, watch = self.watch()
        path = Path(os.environ[mq.lane_wait.VARIABLE])
        # Negative control: the same record, from a live process, is shown.
        mq.lane_wait.waiting(mq.lane_wait.ADMISSION, "all 3 guaranteed slots are busy")
        self.assertIsNotNone(self.gate_after_look(queue, watch)["waiting"])
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        path.write_text(json.dumps({**json.loads(path.read_text()), "pid": dead.pid}))
        self.assertIsNone(self.gate_after_look(queue, watch)["waiting"])

    def test_a_lanes_record_is_no_verdict(self):
        """It holds nothing of an outcome, and a run ends by its exit whatever the record last said."""
        queue, watch = self.watch()
        mq.lane_wait.waiting(mq.lane_wait.ADMISSION, "all 3 guaranteed slots are busy")
        path = Path(os.environ[mq.lane_wait.VARIABLE])
        record = json.loads(path.read_text())
        self.assertEqual(set(record), {"schemaVersion", "kind", "reason", "mac", "offer", "stranded", "pid", "since",
                                       "written", "admitted"})
        self.assertIs(record["admitted"], False)
        gate = self.gate_after_look(queue, watch)
        self.assertNotIn("status", gate)
        self.assertNotIn("outcome", gate)
        self.assertIn("gate lane on no Mac yet, running", self.shown())
        watch.finish()
        self.assertEqual(queue.round_state()["gates"], {})

    DEAD = ("admission on this Mac: a whole-host lane needs every guaranteed slot (HEAVY_SLOTS=3) free, and 3 are busy; "
            "the guard entry of BreezEPUB ways-in, PID 8645, is dead and requires inspection (it has no process record), "
            "and is counted as slot 1; `python3 scripts/eng admissions recover` lists it")

    def test_a_gate_held_up_by_dead_entries_past_the_threshold_is_stuck(self):
        # BreezEPUB's gate waited 86 min behind three dead entries while status said only "waiting" (#315).
        at = time.time()
        late, early = at - mq.STUCK_SECONDS - 60, at - mq.STUCK_SECONDS + 60
        said = mq.stuck({"waitingSince": late, "stranded": True}, at)
        self.assertEqual(said, "stuck: the gate has waited 16m00s for admission held up by dead or uncertain guard "
                               "entries that admission has not recovered; `python3 scripts/eng admissions recover` lists "
                               "them (guides/shared-host.md, Lock reclamation)")
        # Negative controls: before the threshold; a wait on no stranded entry; a gate with no wait recorded.
        self.assertIsNone(mq.stuck({"waitingSince": early, "stranded": True}, at))
        self.assertIsNone(mq.stuck({"waitingSince": late, "stranded": False}, at))
        self.assertIsNone(mq.stuck({"stranded": True}, at))
        self.assertIsNone(mq.stuck({"waitingSince": None, "stranded": True}, at))
        queue = mq.Queue(self.repo)
        queue.note(lambda state: state.update(round="r", entries=["e1", "e2"], gates={
            "batch": {"lane": "gate", "began": late, "entries": ["e1"], "mac": None, "waiting": self.DEAD,
                      "waitingSince": late, "stranded": True, "log": "/l/b.log"},
            "stacked": {"lane": "gate", "began": late, "entries": ["e2"], "mac": None, "waiting": self.DEAD,
                        "waitingSince": early, "stranded": True, "log": "/l/s.log"}}))
        said = self.shown()
        self.assertEqual(said.count("    stuck: the gate has waited 16m"), 1, said)
        self.assertLess(said.index("batch (e1)"), said.index("stuck:"))
        self.assertLess(said.index("stuck:"), said.index("stacked gate (e2)"))
        shown = self.shown(as_json=True)["round"]["gates"]
        self.assertTrue(shown["batch"]["stuck"].startswith("stuck: the gate has waited 16m"))
        self.assertNotIn("stuck", shown["stacked"])

    def test_the_worker_log_says_once_that_a_gate_is_stuck(self):
        queue, watch = self.watch(began=time.time() - 1)
        mq.lane_wait.waiting(mq.lane_wait.ADMISSION, self.DEAD.removeprefix("admission on this Mac: "), stranded=True)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            watch.look()
            began = queue.round_state()["gates"]["batch"]["waitingSince"]
            self.assertEqual(out.getvalue(), "")
            later = began + mq.STUCK_SECONDS + 1
            with patch.object(mq.time, "time", return_value=later):
                watch.look()
                watch.look()
            self.assertEqual(queue.round_state()["gates"]["batch"]["waitingSince"], began)
            self.assertEqual(out.getvalue().count("gate: stuck: the gate has waited 15m01s"), 1, out.getvalue())
            # Once admitted, the wait and its start clear.
            mq.lane_wait.running()
            watch.look()
        gate = queue.round_state()["gates"]["batch"]
        self.assertEqual((gate["waiting"], gate["waitingSince"]), (None, None))
        watch.finish()

    def test_status_shows_the_round_and_why_its_gates_wait(self):
        queue = mq.Queue(self.repo)
        began = time.time() - 27 * 60
        queue.note(lambda state: state.update(
            round="20261107-120000-abcdef12", entries=["e1"], bisect={"suspects": 6, "older": 1, "newer": 2},
            gates={"older half": {"lane": "gate", "began": began, "entries": ["e1"], "mac": None, "log": "/l/o.log",
                                  "waiting": "admission on this Mac: it waits behind a whole-host lane holding the "
                                             "host: StudyWright gate"},
                   "newer half": {"lane": "gate", "began": began, "entries": ["e2", "e3"],
                                  "mac": "the second Mac (build-host)", "waiting": None, "log": "/l/n.log"},
                   "stacked": {"lane": "gate", "began": time.time() - 5, "entries": ["e4"], "mac": None,
                               "waiting": "waits for this Mac: No route to host", "log": "/l/s.log"}}))
        said = self.shown()
        self.assertIn("round 20261107-120000-abcdef12: 1 entry; bisecting 6 entries: older half 1 entry, newer half 2",
                      said)
        self.assertIn("  older half (e1): gate lane on no Mac yet, running 27m", said)
        self.assertIn("    waiting: admission on this Mac: it waits behind a whole-host lane holding the host: "
                      "StudyWright gate", said)
        self.assertIn("  newer half (e2, e3): gate lane on the second Mac (build-host), running 27m", said)
        self.assertIn("  stacked gate (e4): gate lane on no Mac yet, running ", said)
        self.assertIn("    waiting: waits for this Mac: No route to host", said)
        self.assertLess(said.index("older half (e1)"), said.index("newer half (e2"))
        self.assertLess(said.index("newer half (e2"), said.index("stacked gate"))
        shown = self.shown(as_json=True)
        self.assertEqual(shown["round"]["bisect"], {"suspects": 6, "older": 1, "newer": 2})
        self.assertGreaterEqual(shown["round"]["gates"]["older half"]["runningSeconds"], 27 * 60)
        self.assertEqual(shown["round"]["gates"]["stacked"]["entries"], ["e4"])
        # Negative control: what a worker that stopped wrote is no round of the queue's.
        self.assertNotIn("round ", self.shown(working=False))
        self.assertIsNone(self.shown(as_json=True, working=False)["round"])

    def test_a_bisect_records_both_halves_while_they_gate_and_nothing_once_the_worker_stops(self):
        for name in ("a", "b", "bad", "d"): self.commit(name, name)
        self.add("a", "b", "bad", "d")
        queue, seen = mq.Queue(self.repo), []
        real = mq.processes.run
        def recorded(command, **options):
            seen.append(queue.round_state())
            return real(command, **options)
        with patch.object(mq.processes, "run", recorded), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.work(), 0)
        first = seen[0]
        self.assertIsNone(first["bisect"])
        self.assertEqual(list(first["gates"]), ["batch"])
        self.assertEqual(len(first["gates"]["batch"]["entries"]), 4)
        halves = [state for state in seen if state["bisect"]]
        self.assertTrue(halves, seen)
        self.assertEqual(halves[0]["bisect"], {"suspects": 4, "older": 2, "newer": 2})
        roles = set().union(*(state["gates"] for state in halves))
        self.assertLessEqual({"older half", "newer half"}, roles)
        self.assertFalse(queue.round_file.exists())

    def test_a_wait_for_the_host_to_settle_is_recorded_and_cleared(self):
        worker = mq.Worker(mq.Queue(self.repo))
        states = []
        with self.host([("cpu", "busy: the fixture's load"), None],
                       then=lambda: states.append(worker.queue.round_state())) as (slept, said):
            self.assertIsNone(worker.settle("the lanes run"))
        self.assertIn("the host to settle before the lanes run again, since busy: the fixture's load",
                      states[0]["waiting"])
        self.assertIsNone(worker.queue.round_state()["waiting"])


class GateResultsNamed(Fixture):
    """A passing gate says where its per-test results are, in its log and `eng land --wait`'s output, and copies
    none of them there (#266): a session once searched the queue's checkout to confirm its new tests had run."""
    SUITE = "suites/ios-tests/simulator-tests"
    DETAIL = "test_new_case passed on the iPhone"

    @contextlib.contextmanager
    def second_mac(self, *results):
        """The gate ran on fake-mac, by a dispatch's folder as eng run files it once the lane has ended, read by the
        real remote.dispatch, with the files results names sent back under its results/."""
        real = mq.remote.dispatch
        def dispatch(root, name, since):
            ran = Path(root) / ".build/engineering/remote" / mq.remote.DISPATCHES / name \
                / (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-1")
            ran.mkdir(parents=True, exist_ok=True)
            (ran / "source.json").write_text(json.dumps({"host": "fake-mac"}))
            (ran / "run.log").write_text("what the lane printed there\n")
            for result in results:
                (ran / "results" / result).parent.mkdir(parents=True, exist_ok=True)
                (ran / "results" / result).write_text(self.DETAIL + "\n")
            return real(root, name, since)
        # While the lane runs there is no dispatch: only the read after it ends finds one, as for a real run.
        with patch.object(mq.remote, "dispatch", side_effect=dispatch), \
                patch.object(mq.remote, "dispatched", return_value=None):
            yield

    def landed(self, context=None):
        """(the landed entry, its gate log's text, what `eng land --wait` prints for it, what the worker said)."""
        self.commit("a", "a")
        [identifier] = self.add("a")
        with context or contextlib.nullcontext(), contextlib.redirect_stdout(io.StringIO()) as worker:
            self.assertEqual(self.work(), 0)
        entry = self.state(identifier)
        self.assertEqual(entry["state"], "landed")
        self.assertEqual(self.gates(), 1)
        logs = sorted(mq.Queue(self.repo).logs.glob("*-gate.log"))
        self.assertEqual(len(logs), 1)
        with contextlib.redirect_stdout(io.StringIO()) as waited:
            self.assertEqual(mq.wait(self.repo, identifier, 1), 0)
        return entry, logs[0].read_text(), waited.getvalue(), worker.getvalue()

    def assert_waited_ends(self, waited, entry):
        """`eng land --wait` ends with the line naming the results, the gate's log, and the landed commit, last."""
        self.assertEqual(waited.splitlines()[-3:], [entry["gate"]["results"], f"gate log: {entry['gate']['log']}",
                                                    f"landed: {entry['landedAs']}"])

    def test_a_passing_second_mac_gate_names_its_results_and_says_they_are_on_this_mac(self):
        entry, log, waited, worker = self.landed(self.second_mac(f"{self.SUITE}/iPhone.log", f"{self.SUITE}/iPad.log",
                                                                 "suites/mac-tests/unit.log"))
        line = entry["gate"]["results"]
        self.assertIn("gate per-test results: 3 files", line)
        self.assertIn(f"{self.SUITE}: 2", line)
        self.assertIn("suites/mac-tests: 1", line)
        self.assertIn("on this Mac, sent back by the second Mac (fake-mac)", line)
        # The named folder is the dispatch's own, and it holds the per-simulator logs.
        folder = Path(line.split(") in ", 1)[1].split(",", 1)[0])
        self.assertEqual(folder.parent.parent.name, "gate")
        self.assertTrue((folder / self.SUITE / "iPhone.log").is_file())
        self.assertIn(f"{line}\n", log)
        self.assertIn(line, worker)
        self.assertNotIn(self.DETAIL, log)  # named, not copied
        self.assertIn("what the lane printed there", log)  # the dispatch's run.log, which the worker does copy
        self.assert_waited_ends(waited, entry)
        self.assertTrue(Path(entry["gate"]["log"]).is_file())
        self.assertNotIn(self.DETAIL, waited)

    def test_a_passing_second_mac_gate_with_no_results_says_it_kept_none(self):
        # The negative control: no files came back, so the line claims none, and the entry still lands.
        entry, log, waited, _ = self.landed(self.second_mac())
        line = entry["gate"]["results"]
        self.assertIn("gate per-test results: none: the second Mac (fake-mac) sent none back to ", line)
        self.assertNotIn("sent back by", line)
        self.assertIn(f"{line}\n", log)
        self.assert_waited_ends(waited, entry)

    def test_a_gate_run_here_that_wrote_results_names_them_as_a_dispatched_one_does(self):
        # The same gap: nothing named them here either, and the next run here replaces them.
        config = self.config()
        config["lanes"]["gate"]["command"] = [
            "sh", "-c", f'mkdir -p "$ENG_RESULTS_DIR/{self.SUITE}" && echo "{self.DETAIL}" > '
                        f'"$ENG_RESULTS_DIR/{self.SUITE}/mac.log" && printf x >> "{self.counter}"']
        self.publish(config)
        entry, log, waited, _ = self.landed()
        line = entry["gate"]["results"]
        self.assertIn(f"gate per-test results: 1 file ({self.SUITE}: 1) in ", line)
        self.assertIn(".build/engineering/gate/results, on this Mac; its next run here replaces them", line)
        self.assertNotIn("second Mac", line)
        self.assertTrue((Path(line.split(") in ", 1)[1].split(",", 1)[0]) / self.SUITE / "mac.log").is_file())
        self.assertIn(f"{line}\n", log)
        self.assertNotIn(self.DETAIL, log)
        self.assert_waited_ends(waited, entry)

    def test_a_gate_run_here_that_wrote_no_results_prints_what_it_did(self):
        entry, log, waited, worker = self.landed()
        self.assertGreater(entry["gate"]["seconds"], 0)
        self.assertNotIn("results", entry["gate"])
        for said in (log, waited, worker): self.assertNotIn("per-test results", said)
        self.assertEqual(waited, f"{mq.describe(entry)}\nlanded: {entry['landedAs']}\n")

    def test_results_an_earlier_run_left_here_are_not_named(self):
        # A lane that ended here without writing its results leaves the last run's folder: not this gate's.
        root = self.base / "checkout"
        results = root / ".build/engineering/gate/results"
        (results / "suites").mkdir(parents=True)
        (results / "suites/old.log").write_text("old\n")
        old = time.time() - 60
        os.utime(results, (old, old))
        runner = types.SimpleNamespace(checkout=root, ran_on=None)
        self.assertIsNone(mq.Worker.results_named(runner, "gate", None, old + 30))
        self.assertIn("1 file (suites: 1)", mq.Worker.results_named(runner, "gate", None, old - 30))

    def test_a_results_folder_with_many_folders_names_only_the_fullest(self):
        results = self.base / "results"
        for number in range(mq.RESULT_FOLDERS_NAMED + 3):
            (results / f"suite-{number:02d}").mkdir(parents=True)
            for index in range(number + 1): (results / f"suite-{number:02d}" / f"{index}.log").write_text("x")
        said = mq.result_files(results)
        self.assertIn("66 files (suite-10: 11, ", said)
        self.assertIn(", and 3 more folders)", said)
        self.assertNotIn("suite-00", said)
        self.assertEqual(mq.result_files(self.base / "absent"), "")


class FailedGateEvidence(Fixture):
    ROOT_PREFIX = "fixture_" + "oei" + "tre_"

    FIRST = "test_sample.Sample.test_first"
    SECOND = "test_sample.Sample.test_second"
    SUMMARY = (f"eng unittest: 2 tests (0 skipped) in 2 processes, 1 at a time, 0.1s: FAILED: "
               f"{FIRST} (FAILED (failures=1), status 1), {SECOND} (FAILED (failures=1), status 1)")

    def failed(self, context):
        self.status.write_text("1")
        self.commit("a", "a")
        [identifier] = self.add("a")
        with context, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.work(), 0)
        entry = self.state(identifier)
        self.assertEqual(entry["state"], mq.FAILED)
        return entry

    def wait_output(self, entry):
        with contextlib.redirect_stdout(io.StringIO()) as waited:
            self.assertEqual(mq.wait(self.repo, entry["id"], 1), 1)
        return waited.getvalue()

    def test_root_exemption_keeps_spelling_checks_for_contents_and_child_filenames(self):
        self.assertTrue(cli.spelling.Rules().british_in(str(self.base)))
        with contextlib.redirect_stdout(io.StringIO()) as said:
            self.assertEqual(cli.main(["--project", str(self.repo), "spelling"]), 0)
        for index, (name, content, word) in enumerate([
                ("payload.txt", BRITISH, "label" + "led"),
                ("co" + "lour.txt", "American words\n", "co" + "lour")]):
            with self.subTest(name=name):
                self.commit(name, f"spelling-{index}", content)
                with contextlib.redirect_stdout(io.StringIO()) as said:
                    status = cli.main(["--project", str(self.repo), "spelling"])
                self.assertEqual(status, 1, said.getvalue())
                self.assertIn("British spelling", said.getvalue())
                self.assertIn(word, said.getvalue())

    def test_a_failed_remote_gate_keeps_its_assertions_and_dispatch_paths_in_the_wait(self):
        dispatch = self.base / ".dispatches/gate/retained"
        dispatch.mkdir(parents=True)
        log, output = dispatch / "run.log", dispatch / "output.log"
        log.write_text("== nested.Sample.test_turn: OK (1 tests, 0.1s)\n"
                       "FAIL: test_turn (nested.Sample.test_turn)\nAssertionError: nested mismatch\n" + "=" * 70 + "\n"
                       f"== {self.FIRST}: FAILED (1 tests, 0.1s)\n"
                       f"FAIL: test_first ({self.FIRST})\nAssertionError: first mismatch\n" + "=" * 70 + "\n"
                       f"== {self.SECOND}: FAILED (1 tests, 0.1s)\n"
                       f"FAIL: test_second ({self.SECOND})\nAssertionError: second mismatch\n" + "-" * 70 + "\n"
                       f"Ran 2 tests in 0.1s\n{self.SUMMARY}\n")
        output.write_text("the dispatch's transport output\n")

        def remote_gate(root, lane, began):
            return (dispatch, "fake-mac") if lane == "gate" else (None, None)

        entry = self.failed(patch.object(mq.remote, "dispatch", side_effect=remote_gate))
        waited = self.wait_output(entry)
        gate = Path(entry["log"]).read_text()
        for text in (gate, waited):
            self.assertIn(self.SUMMARY, text)
            self.assertIn("AssertionError: first mismatch", text)
            self.assertIn("AssertionError: second mismatch", text)
            self.assertIn(f"remote dispatch run.log: {log}", text)
            self.assertIn(f"remote dispatch output.log: {output}", text)
        self.assertIn("AssertionError: nested mismatch", gate)
        self.assertNotIn("AssertionError: nested mismatch", waited)
        log.unlink()
        waited = self.wait_output(entry)
        self.assertIn("remote dispatch run.log: unavailable (not returned or pruned)", waited)
        self.assertNotIn(f"remote dispatch run.log: {log}", waited)
        self.assertIn(f"remote dispatch output.log: {output}", waited)
        self.assertIn(self.SUMMARY, waited)
        self.assertIn(entry["log"], waited)

    def assert_swift_failure_evidence(self, *, grouped):
        dispatch = self.base / ".dispatches/gate/swift"
        dispatch.mkdir(parents=True)
        run_log, output = dispatch / "run.log", dispatch / "output.log"
        report = "eng run gate: failed (exit 1), on fake-mac\n  tests: 1 passed, 2 failed, 0 skipped, 0 unmeasured\n"
        first = '✘ Test "Keeps the heading" recorded an issue at LayoutTests.swift:42:9: Expectation failed: width > 0'
        second = "✘ Test wraps() recorded an issue at LayoutTests.swift:64:5: Expectation failed: height > 0"
        failure = ("✘ Test fixture() recorded an issue at LayoutTests.swift:10:5: Expectation failed: fixture only\n"
                   "✔ Test fixture() passed after 0.1 seconds.\n"
                   f"{first}\n{first}\n{second}\n"
                   '✘ Test "Keeps the heading" failed after 0.1 seconds with 1 issue.\n'
                   "✘ Test wraps() failed after 0.1 seconds with 1 issue.\n"
                   "✘ Test run with 3 tests in 1 suite failed after 0.2 seconds with 2 issues.\n")
        output.write_text(report)
        if grouped:
            config = self.config()
            lanes = config["lanes"]
            lanes["swift"] = lanes["other"] = lanes["gate"]
            lanes["gate"] = {"parallel": ["swift", "other"], "resource": "heavy"}
            self.publish(config)
            members = dispatch / "lanes"
            members.mkdir()
            (members / "swift.log").write_text(failure)
            (members / "swift.json").write_text('{"status": 1}')
            (members / "other.log").write_text(failure.replace("width > 0", "passing member fixture"))
            (members / "other.json").write_text('{"status": 0}')
        run_log.write_text("" if grouped else failure)

        run_command = mq.processes.run

        def run_gate(command, **options):
            if command[:3] == ["python3", "scripts/eng", "run"] and command[-1] == "gate":
                options["log_path"].write_text(report)
                return 1
            return run_command(command, **options)

        def remote_gate(root, lane, began):
            return (dispatch, "fake-mac") if lane == "gate" else (None, None)

        with patch.object(mq.processes, "run", run_gate):
            entry = self.failed(patch.object(mq.remote, "dispatch", side_effect=remote_gate))
        waited = self.wait_output(entry)
        for text in (waited, Path(entry["log"]).read_text()):
            self.assertIn(report.strip(), text)
            self.assertIn(first, text)
            self.assertIn(second, text)
            self.assertIn(f"remote dispatch run.log: {run_log}", text)
            self.assertIn(f"remote dispatch output.log: {output}", text)
        self.assertEqual(waited.count(first), 1)
        self.assertNotIn("fixture only", waited)
        self.assertNotIn("passing member fixture", waited)

    def test_a_failed_remote_gate_reports_swift_testing_assertions_in_the_wait(self):
        self.assert_swift_failure_evidence(grouped=False)

    def test_a_failed_remote_group_gate_reports_its_swift_testing_members_assertions(self):
        self.assert_swift_failure_evidence(grouped=True)

    def test_an_outer_report_does_not_repeat_a_nested_fixture_failure(self):
        lane_log = "    FAIL: test_turn (nested.Sample.test_turn)\n    AssertionError: nested mismatch\n"
        report = "eng run gate: failed (exit 1), on fake-mac\n  tests: 2 passed\n  failed: Pages/testTurn\n"
        self.assertEqual(mq.failed_lane_excerpt(lane_log, report, "gate"), report.splitlines()[:2])
        self.assertEqual(mq.failed_lane_excerpt(lane_log, "", "gate"), [])

    def test_the_persisted_excerpt_has_both_line_and_total_bounds(self):
        assertions, statuses = [], []
        for index in range(15):
            method = f"test_{index}"
            name = f"test_sample.Sample.{method}"
            assertions.extend([f"== {name}: FAILED (1 tests, 0.1s)", f"FAIL: {method} ({name})",
                               "AssertionError: " + "x" * 1000, "=" * 70])
            statuses.append(f"{name} (FAILED (failures=1), status 1)")
        assertions.append("eng unittest: 15 tests (0 skipped) in 15 processes, 1 at a time, 0.1s: FAILED: "
                          + ", ".join(statuses))
        excerpt = mq.failed_lane_excerpt("\n".join(assertions), "", "gate")
        self.assertTrue(excerpt[0].startswith("eng unittest:"))
        self.assertTrue(excerpt[1].startswith("FAIL: test_0"))
        self.assertTrue(all(len(line) <= mq.FAILURE_LINE_LIMIT for line in excerpt))
        self.assertLessEqual(len("\n".join(excerpt)), mq.FAILURE_EXCERPT_LIMIT)
        self.assertLess(len(excerpt), 16)

    def test_a_local_gate_failure_keeps_the_existing_queue_log_guidance(self):
        entry = self.failed(patch.object(mq.remote, "dispatch", return_value=(None, None)))
        self.assertNotIn("failedDispatch", entry)
        waited = self.wait_output(entry)
        self.assertNotIn("remote dispatch", waited)
        self.assertIn(entry["log"], waited)


# What the module says before a test's patches: each half's placement option, and which lanes may go to the second Mac.
OFFERED = (mq.HERE, mq.BESIDE)
OFFERABLE = mq.offerable


if __name__ == "__main__":
    unittest.main()
