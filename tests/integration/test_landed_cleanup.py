"""What a landing leaves behind: the local branch deleted once the queue proves it landed, and the worktrees that held
landed work reported, never removed (landed_cleanup)."""
import contextlib
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src/python"))
from queuekeeper import events, landed_cleanup as lc, merge_queue as mq, worktree_ownership as wo
import host_isolation  # noqa: F401

from test_support.queue import Fixture, run

shard_by = "class"


def nobody():
    return []


class Landed(Fixture):
    def land(self, branch, name=None):
        """Commit name on a new branch, land it with a worker, and leave the checkout detached, so that no worktree
        has the branch checked out. The entry."""
        tip = self.commit(name or branch, branch)
        [identifier] = self.add(branch)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.work(), 0)
        run(self.repo, "git", "fetch", "-q", "origin")
        run(self.repo, "git", "checkout", "-q", "--detach", "origin/main")
        entry = self.state(identifier)
        self.assertEqual((entry["state"], entry["tip"]), ("landed", tip))
        return entry

    def entries(self):
        return mq.Queue(self.repo).all()

    def branch(self, name):
        found = run(self.repo, "git", "for-each-ref", "--format=%(objectname)", f"refs/heads/{name}")
        return found or None

    def tidy(self, **options):
        return lc.branches(self.repo, self.entries(), **options)


class Branches(Landed):
    def test_a_landed_branch_at_its_queued_tip_is_deleted_logged_and_restorable_and_a_dry_run_keeps_it(self):
        entry = self.land("a")
        [would] = self.tidy(dry_run=True)
        self.assertEqual((would["outcome"], would["how"]), (lc.DELETED, "exact"))
        self.assertEqual(self.branch("a"), entry["tip"])
        queue = mq.Queue(self.repo)
        [done] = self.tidy(emit=queue.emit)
        self.assertEqual(done["outcome"], lc.DELETED)
        self.assertIsNone(self.branch("a"))
        self.assertEqual(done["restore"], ["git", "branch", "a", entry["tip"]])
        self.assertIn("restore with `git branch a ", lc.branch_words(done))
        [event] = [each for each in map(json.loads, events.path_of(queue.directory).read_text().splitlines())
                   if each["kind"] == "branch.deleted"]
        self.assertEqual((event["branch"], event["tip"], event["entry"]), ("a", entry["tip"], entry["id"]))
        # Gone already: nothing to say of it again.
        self.assertEqual(self.tidy(), [])

    def test_a_branch_with_a_commit_main_lacks_is_kept(self):
        self.land("a")
        run(self.repo, "git", "checkout", "-q", "a")
        (self.repo / "more").write_text("more\n")
        run(self.repo, "git", "add", "more")
        run(self.repo, "git", "commit", "-qm", "More")
        run(self.repo, "git", "checkout", "-q", "--detach")
        [kept] = self.tidy()
        self.assertEqual(kept["outcome"], lc.KEPT)
        self.assertIn("have no equal patch on origin/main", kept["why"])
        self.assertIsNotNone(self.branch("a"))

    def test_a_branch_rewritten_to_an_equal_patch_is_deleted(self):
        entry = self.land("a")
        run(self.repo, "git", "checkout", "-q", "a")
        run(self.repo, "git", "commit", "-q", "--amend", "--no-edit", "--date=2001-01-01T00:00:00")
        run(self.repo, "git", "checkout", "-q", "--detach")
        self.assertNotEqual(self.branch("a"), entry["tip"])
        [done] = self.tidy()
        self.assertEqual((done["outcome"], done["how"]), (lc.DELETED, "patch-equivalent"))

    def test_a_checked_out_branch_main_and_a_branch_with_a_newer_waiting_entry_are_kept(self):
        self.land("a")
        self.land("b")
        run(self.repo, "git", "checkout", "-q", "a")
        # b queued again with a commit of its own, not yet landed.
        self.commit("c", "b", start="b")
        self.add("b")
        run(self.repo, "git", "checkout", "-q", "a")
        main = run(self.repo, "git", "rev-parse", "main")
        pretend = {"id": "qk0001-main", "branch": "main", "state": "landed", "tip": main, "landedAs": main,
                   "submitted": 1}
        found = {result["branch"]: result for result in lc.branches(self.repo, [*self.entries(), pretend])}
        self.assertEqual({name: result["outcome"] for name, result in found.items()},
                         {"a": lc.KEPT, "b": lc.KEPT, "main": lc.KEPT})
        self.assertIn(f"checked out at {self.repo}", found["a"]["why"])
        self.assertIn("is queued", found["b"]["why"])
        self.assertIn("never deleted", found["main"]["why"])
        self.assertIsNotNone(self.branch("main"))

    def test_a_branch_being_rebased_counts_as_checked_out(self):
        self.land("a")
        git_dir = Path(run(self.repo, "git", "rev-parse", "--absolute-git-dir"))
        (git_dir / "rebase-merge").mkdir()
        (git_dir / "rebase-merge/head-name").write_text("refs/heads/a\n")
        try:
            [kept] = self.tidy()
        finally:
            (git_dir / "rebase-merge/head-name").unlink()
            (git_dir / "rebase-merge").rmdir()
        self.assertEqual(kept["outcome"], lc.KEPT)
        self.assertIn("checked out", kept["why"])

    def test_qk_land_waits_then_tidies_its_own_branch_unless_it_is_checked_out(self):
        entry = self.land("a")
        said = []
        result = mq.tidy_branch(self.repo, entry["id"], say=said.append)
        self.assertEqual(result["outcome"], lc.DELETED)
        self.assertIn("deleted branch a", said[-1])
        other = self.land("b")
        run(self.repo, "git", "checkout", "-q", "b")
        result = mq.tidy_branch(self.repo, other["id"], say=said.append)
        self.assertEqual(result["outcome"], lc.KEPT)
        self.assertIn("kept branch b: it is checked out", said[-1])


class Worktrees(Landed):
    def setUp(self):
        super().setUp()
        self.land("a")
        self.tree = self.base / "tree"
        run(self.repo, "git", "worktree", "add", "-q", str(self.tree), "a")
        common = Path(run(self.repo, "git", "rev-parse", "--path-format=absolute", "--git-common-dir"))
        (common / "info").mkdir(exist_ok=True)
        (common / "info/exclude").write_text("build/\n__pycache__/\n")

    def report(self, take=nobody):
        return lc.worktrees(self.repo, self.entries(), census=wo.Census(take=take))

    def test_a_clean_unused_worktree_of_a_landed_branch_is_disposable_and_stays(self):
        (self.tree / "__pycache__").mkdir()
        (self.tree / "__pycache__/x.pyc").write_text("")
        [row] = self.report()
        self.assertEqual((row["path"], row["branch"], row["disposable"], row["why"]), (str(self.tree), "a", True, []))
        self.assertTrue(self.tree.is_dir())
        self.assertIn("is disposable", lc.worktree_words(row))
        # Its branch is checked out there, so it stays too.
        [kept] = lc.branches(self.repo, self.entries())
        self.assertIn(f"checked out at {self.tree}", kept["why"])
        # A locked worktree, such as the queue worker's own checkout, is a tool's and never listed.
        run(self.repo, "git", "worktree", "lock", "--reason", "a tool's", str(self.tree))
        self.assertEqual(self.report(), [])

    def test_evidence_changes_and_a_process_inside_keep_it(self):
        (self.tree / "build").mkdir()
        (self.tree / "build/receipt.json").write_text("{}")
        [row] = self.report()
        self.assertFalse(row["disposable"])
        self.assertEqual((row["evidence"], row["evidenceCount"]), (["build/"], 1))
        (self.tree / "build/receipt.json").unlink()
        (self.tree / "build").rmdir()
        (self.tree / "a").write_text("changed\n")
        [row] = self.report()
        self.assertIn("change(s) or untracked", row["why"][0])
        run(self.tree, "git", "checkout", "--", "a")
        shell = SimpleNamespace(pid=7, command="zsh", cwd=str(self.tree), paths=set(), args="zsh")
        [row] = self.report(take=lambda: [shell])
        self.assertIn("PID 7 (zsh)", row["why"][0])
        def fails(): raise OSError("lsof failed")
        [row] = self.report(take=fails)
        self.assertIn("unknown", row["why"][0])

    def test_a_detached_worktree_main_holds_is_listed_and_one_of_unlanded_work_is_not(self):
        run(self.tree, "git", "checkout", "-q", "--detach", "origin/main")
        [row] = self.report()
        self.assertEqual((row["branch"], row["disposable"]), (None, True))
        (self.tree / "new").write_text("new\n")
        run(self.tree, "git", "add", "new")
        run(self.tree, "git", "commit", "-qm", "New")
        self.assertEqual(self.report(), [])

    def test_qk_queue_reclaim_reports_both_in_json(self):
        said = []
        with patch_census():
            self.assertEqual(mq.reclaim(self.repo, as_json=True, out=said.append), 0)
        [payload] = [json.loads(line) for line in said]
        self.assertEqual([each["branch"] for each in payload["branches"]], ["a"])
        self.assertEqual([each["path"] for each in payload["worktrees"]], [str(self.tree)])
        self.assertTrue(self.tree.is_dir())


def patch_census():
    """No process uses anything: a census of none, for what makes its own."""
    real = wo.Census
    return patch.object(wo, "Census", lambda take=None: real(take=nobody))
