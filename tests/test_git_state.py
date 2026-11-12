"""What git_state reads of a checkout, against throwaway repositories.

The toolkit tests these functions only through `scripts/eng`'s choice of a toolkit copy and `eng doctor`, which
stay there, so these cases are new: they hold what `budgets` and `remote` use.

This module does not import `host_isolation` yet, as every test module does: that helper does not
import until `cli` and `disk` are split (PORTING.md). It runs git in a temporary folder only."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/python"))
from queuekeeper import git_state


class Fixture(unittest.TestCase):
    """A bare repository that stands in for the remote, and a clone of it with one commit on main."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)
        identity = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "Test",
                    "GIT_AUTHOR_EMAIL": "test@example.com", "GIT_COMMITTER_NAME": "Test",
                    "GIT_COMMITTER_EMAIL": "test@example.com"}
        # git_state runs git with this process's environment, less what a hook sets.
        without = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
        environment = patch.dict(os.environ, {**without, **identity}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.origin = self.root / "origin.git"
        self.git(self.root, "init", "-q", "--bare", "-b", "main", str(self.origin))
        self.checkout = self.root / "checkout"
        self.git(self.root, "clone", "-q", str(self.origin), str(self.checkout))
        self.git(self.checkout, "symbolic-ref", "HEAD", "refs/heads/main")
        self.commit("First")
        self.git(self.checkout, "push", "-q", "-u", "origin", "main")

    def git(self, cwd, *arguments):
        return subprocess.run(["git", "-C", str(cwd), *arguments], check=True, capture_output=True,
                              text=True).stdout.strip()

    def commit(self, subject):
        self.git(self.checkout, "commit", "-q", "--allow-empty", "-m", subject)
        return self.git(self.checkout, "rev-parse", "HEAD")


class GitTests(Fixture):
    def test_git_gives_its_output_stripped_or_none_when_it_fails(self):
        self.assertEqual(git_state.git(self.checkout, "symbolic-ref", "--short", "HEAD"), "main")
        self.assertIsNone(git_state.git(self.checkout, "rev-parse", "--verify", "--quiet", "no-such-ref"))
        self.assertIsNone(git_state.git(self.root / "nowhere", "rev-parse", "HEAD"))

    def test_git_reads_the_checkout_it_names_whatever_a_hook_set(self):
        other = self.root / "other"
        self.git(self.root, "init", "-q", "-b", "elsewhere", str(other))
        with patch.dict(os.environ, {"GIT_DIR": str(other / ".git"), "GIT_WORK_TREE": str(other)}):
            self.assertNotIn("GIT_DIR", git_state.environment())
            self.assertEqual(git_state.git(self.checkout, "symbolic-ref", "--short", "HEAD"), "main")


class BehindTests(Fixture):
    def test_behind_counts_what_origin_main_has_that_head_lacks(self):
        self.assertEqual(git_state.behind(self.checkout), 0)
        self.commit("Second")
        self.commit("Third")
        self.git(self.checkout, "push", "-q", "origin", "main")
        self.git(self.checkout, "reset", "-q", "--hard", "HEAD~2")
        self.assertEqual(git_state.behind(self.checkout), 2)
        self.assertEqual(git_state.last_landed(self.checkout), "Third")
        # Commits of its own do not count: only what it lacks.
        self.commit("Mine")
        self.assertEqual(git_state.behind(self.checkout), 2)

    def test_a_repository_without_origin_main_has_no_count_and_no_last_landing(self):
        alone = self.root / "alone"
        self.git(self.root, "init", "-q", "-b", "main", str(alone))
        self.git(alone, "commit", "-q", "--allow-empty", "-m", "Only")
        self.assertIsNone(git_state.behind(alone))
        self.assertEqual(git_state.last_landed(alone), "unknown")


class WorktreeTests(Fixture):
    def test_worktrees_lists_the_main_checkout_first_with_each_ones_fields(self):
        linked = self.root / "linked"
        self.git(self.checkout, "worktree", "add", "-q", "-b", "work", str(linked))
        detached = self.root / "detached"
        self.git(self.checkout, "worktree", "add", "-q", "--detach", str(detached))
        gone = self.root / "gone"
        self.git(self.checkout, "worktree", "add", "-q", "-b", "gone", str(gone))
        shutil.rmtree(gone)
        head = self.git(self.checkout, "rev-parse", "HEAD")
        listed = git_state.worktrees(linked)
        self.assertEqual(listed[0]["worktree"], str(self.checkout))
        self.assertEqual((listed[0]["HEAD"], listed[0]["branch"]), (head, "refs/heads/main"))
        found = {entry["worktree"]: entry for entry in listed[1:]}
        self.assertEqual(set(found), {str(linked), str(detached), str(gone)})
        self.assertEqual(found[str(linked)]["branch"], "refs/heads/work")
        self.assertIn("detached", found[str(detached)])
        self.assertNotIn("branch", found[str(detached)])
        self.assertEqual([path for path, entry in found.items() if "prunable" in entry], [str(gone)])

    def test_a_folder_that_is_no_repository_lists_none(self):
        self.assertEqual(git_state.worktrees(self.root / "nowhere"), [])


if __name__ == "__main__":
    unittest.main()
