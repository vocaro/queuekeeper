"""The merge queue against real Git repositories: a bare origin, a clone, and lanes that read the tree."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src/python"))
from vocaro_engineering import config as configuration, merge_queue as mq

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
        self.counter, self.status = self.base / "gates", self.base / "status"
        self.origin = self.base / "origin.git"
        run(self.base, "git", "init", "-q", "--bare", "-b", "main", str(self.origin))
        self.repo = self.base / "project"
        run(self.base, "git", "clone", "-q", str(self.origin), str(self.repo))
        (self.repo / "scripts").mkdir()
        shutil.copy(ROOT / "scripts/eng", self.repo / "scripts/eng")
        (self.repo / "lane.py").write_text(LANE)
        (self.repo / "engineering.json").write_text(json.dumps({
            "schemaVersion": 1, "name": "fixture", "integration": "direct-main",
            "queue": {"carryOver": {"records/": "no lane reads it"}},
            "lanes": {"gate": {"command": ["python3", "lane.py", str(self.counter), str(self.status)]}}}))
        run(self.repo, "git", "add", ".")
        run(self.repo, "git", "commit", "-qm", "start")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")

    def config(self):
        return configuration.load(self.repo)

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
                mq.add(self.repo, self.config(), branch)
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
            self.assertIsNone(mq.add(self.repo, self.config(), "a"))
        self.assertEqual(mq.Queue(self.repo).all(), [])

    def test_a_merge_is_refused(self):
        self.commit("a", "a"); self.commit("b", "b", start="origin/main")
        run(self.repo, "git", "merge", "-q", "--no-edit", "a")
        with self.assertRaisesRegex(ValueError, "is a merge"), patch("builtins.print"):
            mq.add(self.repo, self.config(), "b")

    def test_adding_a_branch_again_replaces_it(self):
        self.commit("a", "a")
        [first] = self.add("a")
        self.commit("a2", "a", start="a")
        with patch.object(mq.Queue, "start"), patch("builtins.print"):
            mq.add(self.repo, self.config(), "a")
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
        def gate(worker, config, round_name):
            outcome = real(worker, config, round_name)
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
        count = self.origin / "attempts"
        receive = self.origin / "hooks/pre-receive"
        receive.write_text(f'#!/bin/sh\n[ -e "{count}" ] && exit 0\ntouch "{count}"\n'
                           'echo "fatal: the remote end hung up unexpectedly" >&2\nexit 1\n')
        receive.chmod(0o755)
        self.commit("a", "a")
        [identifier] = self.add("a")
        self.work()
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


class Refusals(Fixture):
    def test_pull_request_projects_are_refused(self):
        with self.assertRaisesRegex(ValueError, "pull request"):
            mq.add(self.repo, {"name": "x", "integration": "pull-request", "lanes": {"gate": {}}}, "HEAD")

    def test_a_project_with_nothing_to_run_is_refused(self):
        with self.assertRaisesRegex(ValueError, "nothing to run"):
            mq.add(self.repo, {"name": "x", "lanes": {"docs": {}}}, "HEAD")

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

    def test_a_push_whose_connection_dropped_is_pushed_again_once(self):
        self.commit("a", "a")
        [identifier] = self.add("a")
        count = self.origin / "attempts"
        hook = self.origin / "hooks/pre-receive"
        hook.write_text(f'#!/bin/sh\n[ -e "{count}" ] && exit 0\ntouch "{count}"\n'
                        'echo "fatal: the remote end hung up unexpectedly" >&2\nexit 1\n')
        hook.chmod(0o755)
        self.work()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertIn("a", self.main_files())
        self.assertEqual(self.gates(), 1)

    def test_a_push_that_keeps_dropping_holds(self):
        self.commit("a", "a")
        [identifier] = self.add("a")
        hook = self.origin / "hooks/pre-receive"
        hook.write_text('#!/bin/sh\necho "fatal: the remote end hung up unexpectedly" >&2\nexit 1\n')
        hook.chmod(0o755)
        self.assertEqual(self.work(), 1)
        self.assertEqual(self.state(identifier)["state"], "queued")
        self.assertIn("refused", mq.Queue(self.repo).held())

    def test_a_removed_entry_does_not_land(self):
        self.commit("a", "a"); self.commit("b", "b")
        ids = self.add("a", "b")
        queue = mq.Queue(self.repo)
        real = mq.Worker.gate
        def gate(worker, config, round_name):
            outcome = real(worker, config, round_name)
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
        done = subprocess.run(["python3", "scripts/eng", "land", "--repo", "project", "fix", "--wait", "--limit", "120"],
                              cwd=other, capture_output=True, text=True)
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
        with patch("builtins.print"): mq.add(self.repo, self.config(), "a")
        identifier = mq.Queue(self.repo).all()[0]["id"]
        with patch("builtins.print"): self.assertEqual(mq.wait(self.repo, identifier, 120), 0)
        self.assertIn("a", self.main_files())


if __name__ == "__main__":
    unittest.main()
