"""The merge queue against real Git repositories: a bare origin, a clone, and lanes that read the tree."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src/python"))
from vocaro_engineering import cli, config as configuration, merge_queue as mq
import host_isolation
from host_isolation import stretched


# Fails when any tracked file is named bad*, passes otherwise; touches a counter so a test can count gates.
LANE = """import pathlib, sys
counter = pathlib.Path(sys.argv[1]); counter.write_text(counter.read_text() + "x" if counter.exists() else "x")
status = pathlib.Path(sys.argv[2])
if status.exists(): sys.exit(int(status.read_text()))
bad = sorted(path.name for path in pathlib.Path(".").glob("bad*"))
print("bad:", bad)
both = pathlib.Path("left").exists() and pathlib.Path("right").exists()
sys.exit(1 if bad or both else 0)
"""


def run(directory, *words):
    return subprocess.run(words, cwd=directory, check=True, capture_output=True, text=True).stdout.strip()


class Fixture(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        patcher = patch.dict(os.environ, {"VOCARO_OFFLOAD_HOST": "none", "GIT_CONFIG_GLOBAL": "/dev/null",
                                          "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                                          "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
                                          "VOCARO_ENGINEERING_ROOT": str(ROOT)})
        patcher.start()
        self.addCleanup(patcher.stop)
        # A settled host unless a test says otherwise, so that no retry waits on this Mac's real load.
        settled = patch.object(mq.resources, "pressure", return_value=None)
        self.pressure = settled.start()
        self.addCleanup(settled.stop)
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

    def add(self, *branches):
        """Queue branches with no worker started, so a test holds every order fixed."""
        ids = []
        with patch.object(mq.Queue, "start"), patch("builtins.print"):
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
        ids = self.add("one", "two", "c")
        self.work()
        self.assertEqual([self.state(i)["state"] for i in ids], ["landed", "conflict", "landed"])
        self.assertIn("same", self.state(ids[1])["why"])
        self.assertEqual(self.gates(), 1)

    def test_a_commit_main_already_holds_needs_no_gate(self):
        tip = self.commit("a", "a")
        run(self.repo, "git", "push", "-q", "origin", f"{tip}:main")
        self.commit("b", "b")
        with patch("builtins.print"):
            self.assertIsNone(mq.add(self.repo, "a"))
        self.assertEqual(mq.Queue(self.repo).all(), [])

    def test_a_merge_is_refused(self):
        self.commit("a", "a"); self.commit("b", "b", start="origin/main")
        run(self.repo, "git", "merge", "-q", "--no-edit", "a")
        with self.assertRaisesRegex(ValueError, "is a merge"), patch("builtins.print"):
            mq.add(self.repo, "b")

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


class NoVerdict(Fixture):
    def test_unmeasured_holds_after_a_retry_and_blames_nobody(self):
        self.status.write_text("3")
        self.commit("a", "a")
        [identifier] = self.add("a")
        self.assertEqual(self.work(), 0)
        self.assertEqual(self.gates(), 2)
        self.assertEqual(self.state(identifier)["state"], "queued")
        self.assertNotIn("a", self.main_files())
        queue = mq.Queue(self.repo)
        self.assertIn("exit 3", queue.held())
        self.status.unlink()
        with patch.object(mq.Queue, "start"), patch("builtins.print"): mq.resume(self.repo)
        self.work()
        self.assertEqual(self.state(identifier)["state"], "landed")

    def test_a_failure_is_not_read_as_no_verdict(self):
        self.status.write_text("1")
        self.commit("a", "a")
        [identifier] = self.add("a")
        self.work()
        self.assertEqual(self.state(identifier)["state"], "failed")
        self.assertIsNone(mq.Queue(self.repo).held())

    BUSY = ("load", "load 40.0 is at or above HEAVY_LOAD_LIMIT=13.5")

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
        self.assertIn("load 40.0 is at or above HEAVY_LOAD_LIMIT=13.5: the lanes run again once the host settles", log)
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
        self.assertIn(f"exit 3, and the host did not settle in {mq.SETTLE_LIMIT}s (load 40.0 is at or above", held)
        self.assertIn("eng queue resume", held)
        self.assertIn(f"the host did not settle in {mq.SETTLE_LIMIT}s and {len(slept) + 1} looks", said.getvalue())
        # A line every SETTLE_REPORT while it waited, not one per look.
        self.assertEqual(said.getvalue().count("still waiting"), mq.SETTLE_LIMIT // mq.SETTLE_REPORT)
        self.pressure.side_effect = None
        self.status.unlink()
        with patch.object(mq.Queue, "start"), patch("builtins.print"): mq.resume(self.repo)
        self.work()
        self.assertEqual(self.state(identifier)["state"], "landed")

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
        path.chmod(0o755)

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
        wrapper.chmod(0o755)
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
        self.assertEqual(self.state(identifier)["state"], "queued")
        self.assertNotIn("a", self.main_files())
        self.assertEqual(len(self.runs()), 1)
        held = mq.Queue(self.repo).held()
        self.assertIn("refused", held)
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
        self.assertEqual(self.state(identifier)["state"], "queued")
        held = mq.Queue(self.repo).held()
        self.assertIn("the pre-push hook gave no verdict, exit 3, on", held)
        self.assertIn(f"the host did not settle in {mq.SETTLE_LIMIT}s", held)

    def test_a_hook_that_keeps_giving_no_verdict_holds_and_blames_nobody(self):
        self.hook("exit 75")
        self.commit("a", "a")
        [identifier] = self.add("a")
        self.assertEqual(self.work(), 1)
        self.assertEqual(self.state(identifier)["state"], "queued")
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
        self.assertEqual(self.state(identifier)["state"], "queued")
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
        self.assertEqual(self.state(identifier)["state"], "queued")
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
        self.assertEqual([self.state(i)["docsOnly"] for i in ids], [["docs"]] * 3)
        self.assertLessEqual({"doc", "README.md"}, self.main_files())
        self.assertTrue(any("queue.docsOnly: running only docs" in words for words in said), said)
        self.assertIn("docs lanes only (docs): no gate of that commit", mq.describe(self.state(ids[0])))

    def test_a_mixed_batch_runs_every_lane(self):
        self.commit("doc/guide.md", "guide"); self.commit("code", "code")
        ids = self.add("guide", "code")
        self.work()
        self.assertEqual([self.state(i)["state"] for i in ids], ["landed"] * 2)
        self.assertEqual((self.gates(), self.docs_runs()), (1, 0))
        self.assertNotIn("docsOnly", self.state(ids[0]))
        self.assertNotIn("docs lanes only", mq.describe(self.state(ids[0])))

    def test_an_undeclared_project_runs_every_lane(self):
        self.publish(self.configured, "Declare nothing")
        self.commit("doc/guide.md", "guide")
        [identifier] = self.add("guide")
        self.work()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual((self.gates(), self.docs_runs()), (1, 0))
        self.assertNotIn("docsOnly", self.state(identifier))

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

    def test_a_hook_no_lane_covers_runs(self):
        PrePushHooks.hook(self, "exit 0")
        self.commit("doc/guide.md", "guide")
        [identifier] = self.add("guide")
        self.work()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(len(PrePushHooks.runs(self)), 1)

    def test_a_docs_only_landing_is_no_gate_eng_release_takes(self):
        from vocaro_engineering import release
        self.commit("code", "code")
        [code] = self.add("code")
        self.work()
        checkout = mq.Queue(self.repo).checkout
        # The control: a batch that ran every lane leaves a receipt of the gate at the commit it pushed.
        self.assertIn("in this checkout", release.gate_passed(checkout, self.state(code)["landedAs"]))
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
                    {**good, "lanes": ["absent"]}, {**good, "lanes": ["gate"]}, {**good, "lanes": ["docs", "gate"]}, []):
            with self.subTest(bad=bad):
                (self.repo / "engineering.json").write_text(json.dumps({**config, "queue": {"docsOnly": bad}}))
                with self.assertRaisesRegex(ValueError, "docsOnly"): self.config()

    def test_the_schema_knows_the_setting(self):
        schema = json.loads((ROOT / "schemas/engineering.schema.json").read_text())
        declared = schema["properties"]["queue"]["properties"]["docsOnly"]
        self.assertEqual(set(declared["properties"]), {"paths", "lanes"})
        self.assertEqual(declared["properties"]["lanes"]["items"]["not"], {"const": "gate"})


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
        self.session({**current, "lanes": {"gate": {"command": ["false"]}}})
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
        """Land each content of name, on a branch of its own from the same commit, oldest first."""
        for number, content in enumerate(contents): self.commit(name, f"b{number}", content)
        ids = self.add(*(f"b{number}" for number in range(len(contents))))
        self.work()
        return [self.state(identifier)["state"] for identifier in ids]

    def test_each_branch_lands_with_its_line_first_after_one_gate(self):
        self.assertEqual(self.outcome(self.top("- a"), self.top("- b"), self.top("- c")), ["landed"] * 3)
        self.assertEqual(self.changelog(), self.START.replace("- old\n", "- c\n- b\n- a\n- old\n", 1))
        self.assertEqual(self.gates(), 1)
        self.assertIsNone(mq.Queue(self.repo).held())
        # What guides/maintenance.md warns of: the first went in as it was, and a settled one proves nothing.
        contained = lambda branch: mq.landing.contained(lambda *words: mq.git(self.repo, *words), "origin/main", branch)
        self.assertIsNotNone(contained("b0"))
        self.assertIsNone(contained("b1"))

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

    def test_lines_added_below_the_top_of_a_section_come_back(self):
        start = self.START.replace("- old\n", "- old\n- older\n", 1)
        self.commit("CHANGELOG.md", "longer", start)
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        states = self.outcome(*(start.replace("- older", f"{line}\n- older") for line in ("- a", "- b")))
        self.assertEqual(states, ["landed", "conflict"])

    def test_a_new_section_ahead_comes_back(self):
        """Keeping both would put the line in the section just released."""
        states = self.outcome(self.START.replace("- old", "## 1.1\n\n- old"), self.top("- b"))
        self.assertEqual(states, ["landed", "conflict"])

    def test_the_same_shape_in_another_file_comes_back(self):
        self.commit("NOTES.md", "notes", self.START)
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        states = self.outcome(self.top("- a"), self.top("- b"), name="NOTES.md")
        self.assertEqual(states, ["landed", "conflict"])

    def test_a_changelog_conflict_beside_another_files_comes_back_whole(self):
        self.commit("same", "same", "start")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        for number, line in enumerate(("- a", "- b")):
            self.commit("CHANGELOG.md", f"b{number}", self.top(line))
            (self.repo / "same").write_text(line)
            run(self.repo, "git", "add", "same")
            run(self.repo, "git", "commit", "-q", "--amend", "--no-edit")
        ids = self.add("b0", "b1")
        self.work()
        self.assertEqual([self.state(i)["state"] for i in ids], ["landed", "conflict"])
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
        toolkit = patch.object(mq, "TOOLKIT_ROOT", self.kit)
        toolkit.start()
        self.addCleanup(toolkit.stop)

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
        identifier, status = self.gate_once("a")
        self.assertEqual(status, 0)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(self.seen.read_text().split("\n"), [str(self.copy), landed, ""])
        self.assertEqual(run(self.kit, "git", "rev-parse", "HEAD"), behind)

    def test_a_dirty_shared_checkout_neither_reaches_a_gate_nor_is_touched(self):
        edited = self.kit / "src/python/vocaro_engineering/cli.py"
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
        (self.copy / "src/python/vocaro_engineering/project.py").write_text("raise SystemExit(1)\n")
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
        with contextlib.redirect_stdout(io.StringIO()) as said:
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
        worker = mq.Worker(mq.Queue(self.repo))
        with patch.object(mq, "TOOLKIT_ROOT", self.repo): worker.toolkit()
        self.assertNotIn("VOCARO_ENGINEERING_ROOT", worker.environment)
        self.assertFalse(self.copy.exists())

    def test_an_explicit_toolkit_stands(self):
        with patch.dict(os.environ, {"VOCARO_ENGINEERING_ROOT": str(ROOT)}):
            identifier, status = self.gate_once("a")
        self.assertEqual(status, 0)
        self.assertEqual(self.seen.read_text().split("\n")[0], str(ROOT))
        self.assertFalse(self.copy.exists())


class SharedCheckout(Fixture):
    """The toolkit's own queue keeping its main checkout, the shared checkout, at origin/main
    (vocaro-engineering#104). The fixture's clone stands for that checkout."""
    def setUp(self):
        super().setUp()
        toolkit = patch.object(mq, "TOOLKIT_ROOT", self.repo)
        toolkit.start()
        self.addCleanup(toolkit.stop)

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

    def test_a_process_started_from_the_shared_checkout_holds_it_back(self):
        def started(): self.held = subprocess.Popen(["sleep", "300"], cwd=self.repo / "scripts")
        before, after, main, said = self.land(started)
        self.addCleanup(self.held.wait)
        self.addCleanup(self.held.kill)
        self.assertEqual(after, before)
        self.assertIn(f"a process started from it may still run its toolkit: PID {self.held.pid} (sleep)", said)
        # A census that fails says nothing of what runs there, so it holds the checkout back too.
        self.held.kill()
        with patch.object(mq.disk, "census", side_effect=RuntimeError("could not list processes")):
            before, after, main, said = self.land()
        self.assertEqual(after, before)
        self.assertIn("whether a process started from it still runs is unknown", said)
        # The positive control: with the process gone, the next landing moves it.
        before, after, main, said = self.land()
        self.assertEqual(after, main)

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

    def test_a_remote_that_stays_out_of_reach_holds_after_the_last_backoff(self):
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.remote_says(UNREACHED["https name"], range(1, 99)) as (fetches, slept):
            self.assertEqual(self.work(), 1)
        self.assertEqual(self.state(identifier)["state"], "queued")
        self.assertEqual((len(fetches), slept), (len(mq.BACKOFF) + 1, list(mq.BACKOFF)))
        self.assertIn(f"{len(mq.BACKOFF) + 1} times", mq.Queue(self.repo).held())
        self.assertEqual(self.gates(), 0)

    def test_a_fetch_or_push_the_remote_refused_holds_at_once(self):
        for command in ("fetch", "push"):
            for name, said in REFUSED.items():
                with self.subTest(command=command, refusal=name):
                    with contextlib.suppress(OSError): mq.Queue(self.repo).hold_file.unlink()
                    branch = f"{command}-{name}".replace(" ", "-")
                    self.commit(branch, branch)
                    [identifier] = self.add(branch)
                    with patch("builtins.print"), self.remote_says(said, {1}, command) as (calls, slept):
                        self.assertEqual(self.work(), 1)
                    self.assertEqual(self.state(identifier)["state"], "queued")
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


class Refusals(Fixture):
    def test_pull_request_projects_are_refused(self):
        self.publish({"schemaVersion": 1, "name": "x", "integration": "pull-request",
                      "lanes": {"gate": {"command": ["true"]}}})
        with self.assertRaisesRegex(ValueError, "pull request"): mq.add(self.repo, "HEAD")

    def test_a_project_with_nothing_to_run_is_refused(self):
        self.publish({"schemaVersion": 1, "name": "x", "lanes": {"docs": {"command": ["true"]}}})
        with self.assertRaisesRegex(ValueError, "nothing to run"): mq.add(self.repo, "HEAD")

    def test_named_lanes_come_first_and_gate_is_the_default(self):
        self.assertEqual(mq.lanes_of({"queue": {"lanes": ["a"]}, "lanes": {"a": {}, "gate": {}}}), ["a"])
        self.assertEqual(mq.lanes_of({"lanes": {"gate": {}}}), ["gate"])
        self.assertEqual(mq.lanes_of({"lanes": {"gate": {}, "provision": {}, "spelling": {}, "docs": {}}}),
                         ["spelling", "provision", "gate"])

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
        hook.chmod(0o755)
        self.assertEqual(self.work(), 1)
        self.assertEqual(self.state(identifier)["state"], "queued")
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

    def test_a_push_that_keeps_dropping_holds(self):
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.remote_says("fatal: the remote end hung up unexpectedly", range(1, 99), "push") as (pushes, slept):
            self.assertEqual(self.work(), 1)
        self.assertEqual(self.state(identifier)["state"], "queued")
        self.assertIn("did not reach the remote", mq.Queue(self.repo).held())
        self.assertEqual((len(pushes), slept), (len(mq.BACKOFF) + 1, list(mq.BACKOFF)))

    def test_a_remote_that_refuses_a_push_holds_at_once_whatever_else_it_says(self):
        """The remote answered: its hook's message is no dropped connection, which Git would print the same."""
        self.commit("a", "a")
        [identifier] = self.add("a")
        hook = self.origin / "hooks/pre-receive"
        hook.write_text('#!/bin/sh\necho "fatal: the remote end hung up unexpectedly" >&2\nexit 1\n')
        hook.chmod(0o755)
        with patch.object(mq, "pause") as slept: self.assertEqual(self.work(), 1)
        self.assertEqual(self.state(identifier)["state"], "queued")
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


if __name__ == "__main__":
    unittest.main()
