"""Claude Code's hooks: what a starting session hears of its entries, and the guard against landing on main by hand."""
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

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src/python"))
import host_isolation  # noqa: F401 -- first, as every test module
from queuekeeper import agent_hooks, cli


class Repository(unittest.TestCase):
    """A repository on main with a linked worktree on the branch work, and a queue store of hand-written entries."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)
        identity = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "Test",
                    "GIT_AUTHOR_EMAIL": "test@example.com", "GIT_COMMITTER_NAME": "Test",
                    "GIT_COMMITTER_EMAIL": "test@example.com"}
        without = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
        self.enterContext(patch.dict(os.environ, {**without, **identity}, clear=True))
        os.environ.pop(agent_hooks.GUARD_SWITCH, None)
        self.main = self.root / "main"
        self.git(self.root, "init", "-q", "-b", "main", str(self.main))
        (self.main / "file").write_text("one\n")
        self.git(self.main, "add", "-A")
        self.git(self.main, "commit", "-q", "-m", "First")
        self.linked = self.root / "linked"
        self.git(self.main, "worktree", "add", "-q", "-b", "work", str(self.linked))
        self.store = self.main / ".git" / "queuekeeper-queue"

    def git(self, directory, *words):
        return subprocess.run(["git", "-C", str(directory), *words], check=True, capture_output=True,
                              text=True).stdout.strip()

    def hook(self, name, payload):
        """What the hook prints, parsed, or None when it prints nothing; it always exits 0."""
        output = io.StringIO()
        with patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(["hook", name]), 0)
        return json.loads(output.getvalue()) if output.getvalue().strip() else None


class SessionStart(Repository):
    def entry(self, identifier, state, ref=True, **fields):
        (self.store / "entries").mkdir(parents=True, exist_ok=True)
        entry = {"id": identifier, "state": state, "branch": "work", "directory": str(self.linked),
                 "subject": f"Subject of {identifier}", "submitted": time.time(), **fields}
        if state not in agent_hooks.WAITING: entry.setdefault("settled", time.time())
        (self.store / "entries" / f"{identifier}.json").write_text(json.dumps(entry))
        if ref: self.git(self.main, "update-ref", f"refs/queuekeeper/queue/{identifier}", "HEAD")
        return entry

    def context(self, directory=None):
        found = self.hook("session-start", {"hook_event_name": "SessionStart", "cwd": str(directory or self.linked)})
        if found is None: return None
        self.assertEqual(found["hookSpecificOutput"]["hookEventName"], "SessionStart")
        return found["hookSpecificOutput"]["additionalContext"]

    def test_a_sent_back_entry_of_this_worktree_is_reported_with_its_next_command(self):
        self.entry("work-1", "failed", why="The gate failed: test_a")
        said = self.context()
        self.assertIn("work-1", said)
        self.assertIn("The gate failed: test_a", said)
        self.assertIn(f"`qk --project {self.linked} queue result work-1 --json`", said)
        self.assertIn("qk land --wait", said)

    def test_a_held_queue_is_reported_for_a_waiting_entry_with_its_diagnosis(self):
        self.entry("work-1", "testing")
        self.store.joinpath("hold").write_text("for the owner: the signing key is refused\n")
        said = self.context()
        self.assertIn("work-1", said)
        self.assertIn("held", said)
        self.assertIn("the signing key is refused", said)
        self.assertIn("queue doctor --json", said)

    def test_a_waiting_entry_without_a_hold_is_followed_rather_than_queued_again(self):
        self.entry("work-1", "queued")
        self.assertIn("queue wait work-1 --json", self.context())

    def test_only_the_newest_entry_of_a_branch_counts(self):
        self.entry("work-1", "failed", submitted=time.time() - 60, why="old failure")
        self.entry("work-2", "landed")
        self.assertIsNone(self.context())

    def test_removed_old_and_landed_entries_say_nothing(self):
        for state, ref, settled in (("failed", False, time.time()), ("conflict", True, time.time() - 30 * 86400),
                                    ("landed", True, time.time()), ("removed", True, time.time())):
            with self.subTest(state=state, ref=ref):
                shutil.rmtree(self.store, ignore_errors=True)
                self.entry("work-1", state, ref=ref, settled=settled)
                self.assertIsNone(self.context())

    def test_another_branchs_entries_are_not_this_sessions(self):
        self.entry("other-1", "failed", branch="other", directory=str(self.root / "elsewhere"))
        self.assertIsNone(self.context())
        # The same branch checked out in another worktree is the same work.
        self.entry("work-1", "conflict", directory=str(self.root / "elsewhere"), why="file conflicts")
        self.assertIn("work-1", self.context())

    def test_an_earlier_sessions_entry_from_this_folder_on_another_branch_is_not_this_sessions(self):
        # Claude Code gives a finished session's worktree folder to a new session, on a branch of its own.
        self.entry("earlier-1", "failed", branch="earlier", why="its gate failed")
        self.assertIsNone(self.context())
        # Mid-rebase the checkout is on no branch, and the folder's entries are still its own.
        self.git(self.linked, "checkout", "-q", "--detach")
        self.assertIn("earlier-1", self.context())

    def test_a_repository_without_a_queue_is_not_given_one(self):
        self.assertIsNone(self.context())
        self.assertFalse(self.store.exists())
        self.assertIsNone(self.context(self.root))

    def test_unreadable_input_and_entries_say_nothing(self):
        (self.store / "entries").mkdir(parents=True)
        (self.store / "entries" / "broken.json").write_text("{")
        self.assertIsNone(self.context())
        output = io.StringIO()
        # Unreadable input leaves the hook the process's directory, which must not be a real worktree with real entries.
        with patch.object(sys, "stdin", io.StringIO("not json")), contextlib.redirect_stdout(output), \
                contextlib.chdir(self.root):
            self.assertEqual(cli.main(["hook", "session-start"]), 0)
        self.assertEqual(output.getvalue(), "")


class Guard(Repository):
    def decision(self, command, directory=None):
        found = self.hook("pre-tool-use", {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                                           "tool_input": {"command": command}, "cwd": str(directory or self.linked)})
        if found is None: return None
        self.assertEqual(found["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("qk land --wait", found["hookSpecificOutput"]["permissionDecisionReason"])
        return found["hookSpecificOutput"]["permissionDecisionReason"]

    def test_landing_on_main_by_hand_is_refused(self):
        for command in ("git push origin main", "git push origin HEAD:main", "git push -f origin work:refs/heads/main",
                        "git push origin +main", "git push origin :main", "git push --all origin",
                        "git -C . push origin main", "FOO=1 sudo git push origin main",
                        "make && git push origin main > push.log 2>&1", "bash -c 'git push origin main'",
                        "git commit -m 'one\ntwo'\ngit push origin main", "git update-ref refs/heads/main HEAD",
                        "git branch -f main HEAD", "gh pr merge 12 --squash", "cd ../main && git merge work",
                        f"git -C {self.main} merge work"):
            with self.subTest(command=command):
                self.assertIsNotNone(self.decision(command))

    def test_ordinary_work_passes(self):
        for command in ("git push origin work", "git push -u origin HEAD", "git merge origin/main", "git status",
                        "echo 'git push origin main'", "git commit -m 'never git push origin main'",
                        "qk land --wait --json", "gh pr view 12", "git log main", "git branch -d other",
                        "cd ../main && git merge --abort", "git push origin main-notes", "echo \"unclosed"):
            with self.subTest(command=command):
                self.assertIsNone(self.decision(command))

    def test_a_push_or_merge_after_a_switch_in_the_same_command_is_judged_by_the_branch_switched_to(self):
        """With main checked out, an agent's chained switch then push or merge touches the branch it switched to, not
        main (vocaro/queuekeeper#390)."""
        self.git(self.main, "branch", "-q", "feature")
        for command in ("git switch -q feature; echo f >> f.txt; git commit -qam f; git push -q",
                        "git switch -qc fresh HEAD; git merge -q --no-ff --no-edit work",
                        "git checkout -q feature && git push", "git checkout -b other && git merge work",
                        "git switch --detach HEAD && git merge work", "bash -c 'git switch -q feature; git push'",
                        "git switch -q feature; git push origin HEAD"):
            with self.subTest(command=command):
                self.assertIsNone(self.decision(command, self.main))
        # Back on main, or never off it, is still refused; so is a branch switched to in another checkout.
        for command in ("git switch -q feature; git switch -q main; git push",
                        "git checkout -- file && git push", "git switch -q feature; git push origin main",
                        f"git -C {self.linked} switch -q feature; git merge work"):
            with self.subTest(command=command):
                self.assertIsNotNone(self.decision(command, self.main))
        # A switch whose branch the guard cannot read is judged by the branch checked out now, which it says.
        why = self.decision("git switch -q not-a-branch; git push", self.main)
        self.assertIn("judged by the branch checked out now, main", why)

    def test_a_bare_push_is_refused_where_it_would_reach_main(self):
        self.assertIsNotNone(self.decision("git push", self.main))
        self.assertIsNone(self.decision("git push"))
        # A branch made from origin/main that tracks it, under push.default upstream, pushes to main.
        origin = self.root / "origin.git"
        self.git(self.root, "init", "-q", "--bare", str(origin))
        self.git(self.main, "remote", "add", "origin", str(origin))
        self.git(self.main, "push", "-q", "origin", "main")
        self.git(self.linked, "fetch", "-q", "origin")
        self.git(self.linked, "branch", "-q", "--set-upstream-to", "origin/main")
        self.git(self.linked, "config", "push.default", "upstream")
        self.assertIsNotNone(self.decision("git push"))

    def remote_ahead(self):
        """Give main an origin it tracks whose main is one commit ahead, fetched but not merged, and give work a
        commit the origin lacks."""
        origin = self.root / "origin.git"
        self.git(self.root, "init", "-q", "--bare", str(origin))
        self.git(self.main, "remote", "add", "origin", str(origin))
        self.git(self.main, "push", "-q", "-u", "origin", "main")
        other = self.root / "other"
        self.git(self.root, "clone", "-q", "-b", "main", str(origin), str(other))
        (other / "file").write_text("two\n")
        self.git(other, "commit", "-q", "-am", "Second")
        self.git(other, "push", "-q", "origin", "main")
        self.git(self.main, "fetch", "-q", "origin")
        (self.linked / "file").write_text("local\n")
        self.git(self.linked, "commit", "-q", "-am", "Local")

    def test_main_may_fast_forward_to_the_remotes_main(self):
        self.remote_ahead()
        for command in ("git merge --ff-only origin/main", "git merge --ff-only refs/remotes/origin/main",
                        "git merge -q --ff-only origin/main~1", "git merge --ff-only", "git pull --ff-only",
                        "git pull --ff-only origin", "git pull --ff-only -q origin main",
                        f"git -C {self.main} merge --ff-only origin/main"):
            with self.subTest(command=command):
                self.assertIsNone(self.decision(command, self.main))
        self.git(self.main, "config", "pull.ff", "only")
        self.assertIsNone(self.decision("git pull", self.main))

    def test_main_may_not_merge_or_pull_anything_else(self):
        self.remote_ahead()
        for command in ("git merge origin/main", "git merge --ff-only work", "git merge --ff-only origin/main work",
                        "git merge --ff-only --squash origin/main", "git merge --ff-only -m x origin/main",
                        "git merge --ff-only nowhere", "git pull", "git pull origin main", "git pull --rebase",
                        "git pull --ff-only --rebase", "git pull --ff-only origin work",
                        "git pull --ff-only origin work:main", "git pull --ff-only elsewhere main",
                        "git pull --ff-only --ff"):
            with self.subTest(command=command):
                self.assertIn("fast-forwarding", self.decision(command, self.main))
        self.assertIsNone(self.decision("git pull origin main"))

    def test_without_a_remote_main_no_merge_into_main_passes(self):
        self.assertIsNotNone(self.decision("git merge --ff-only work", self.main))
        self.assertIsNotNone(self.decision("git pull --ff-only", self.main))

    def test_a_pull_request_project_is_pointed_to_a_pull_request(self):
        (self.linked / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "x",
                                                                  "integration": "pull-request"}))
        for command in ("git push origin main", "gh pr merge 12 --squash"):
            with self.subTest(command=command):
                found = self.hook("pre-tool-use", {"tool_name": "Bash", "tool_input": {"command": command},
                                                   "cwd": str(self.linked)})
                why = found["hookSpecificOutput"]["permissionDecisionReason"]
                self.assertEqual(found["hookSpecificOutput"]["permissionDecision"], "deny")
                self.assertIn("open a pull request", why)
                self.assertNotIn("qk land", why)
        self.assertIsNone(self.decision("git push origin work"))
        # The main checkout, whose queuekeeper.json does not say so, still lands through the queue.
        self.assertIsNotNone(self.decision("git push origin main", self.main))

    def test_a_pull_request_queue_project_is_pointed_to_a_labeled_pull_request(self):
        (self.linked / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "x",
                                                                  "integration": "pull-request-queue"}))
        found = self.hook("pre-tool-use", {"tool_name": "Bash", "tool_input": {"command": "git push origin main"},
                                           "cwd": str(self.linked)})
        why = found["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("labels for the merge queue", why)
        self.assertNotIn("qk land --wait", why)

    def test_an_agent_may_not_add_the_queue_label_in_a_pull_request_queue_project(self):
        """The label is a person's approval, and the queue merges what carries it (vocaro/queuekeeper#391)."""
        def decided(command, config):
            (self.linked / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "x", **config}))
            found = self.hook("pre-tool-use", {"tool_name": "Bash", "tool_input": {"command": command},
                                               "cwd": str(self.linked)})
            return found and found["hookSpecificOutput"]["permissionDecisionReason"]
        queue_mode = {"integration": "pull-request-queue"}
        for command in ("gh pr edit 5 --add-label queue", "gh pr edit 5 --add-label=bug,queue",
                        "gh issue edit 5 --add-label queue", "gh pr create --title x --label queue",
                        "gh pr create -l queue", "bash -c 'gh pr edit 5 --add-label queue'",
                        "gh api repos/o/r/issues/5/labels -f 'labels[]=queue'",
                        "gh api --method POST repos/o/r/issues/5/labels --input labels.json",
                        "gh api graphql -f query='mutation { addLabelsToLabelable(input: {}) { clientMutationId } }'"):
            with self.subTest(command=command):
                why = decided(command, queue_mode)
                self.assertIn("leave the label to a person", why)
        for command in ("gh pr edit 5 --remove-label queue", "gh api -X DELETE repos/o/r/issues/5/labels/queue",
                        "gh api repos/o/r/issues/5/labels", "gh pr edit 5 --add-label queued",
                        "gh pr edit 5 --add-label bug", "qk land --pr 5", "gh pr view 5 --json labels"):
            with self.subTest(command=command):
                self.assertIsNone(decided(command, queue_mode))
        # Another label, as pullRequests.label names it; and in other modes a label is no approval.
        self.assertIsNotNone(decided("gh pr edit 5 --add-label ship", {**queue_mode, "pullRequests": {"label": "ship"}}))
        self.assertIsNone(decided("gh pr edit 5 --add-label queue", {**queue_mode, "pullRequests": {"label": "ship"}}))
        self.assertIsNone(decided("gh pr edit 5 --add-label queue", {"integration": "pull-request"}))
        self.assertIsNone(decided("gh pr edit 5 --add-label queue", {}))

    def test_other_tools_and_the_owners_switch_pass(self):
        found = self.hook("pre-tool-use", {"tool_name": "Write", "tool_input": {"command": "git push origin main"}})
        self.assertIsNone(found)
        with patch.dict(os.environ, {agent_hooks.GUARD_SWITCH: "off"}):
            self.assertIsNone(self.decision("git push origin main"))

    def test_an_unknown_hook_is_a_usage_error(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["hook", "stop"]), 2)


if __name__ == "__main__":
    unittest.main()
