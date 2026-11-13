"""What git_state reads of a checkout, against throwaway repositories."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/python"))
import host_isolation  # noqa: F401 -- first, as every test module
from queuekeeper import git_state, offload_host, remote


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


class MainCheckoutTests(Fixture):
    def test_a_linked_worktree_names_its_main_checkout_and_the_main_checkout_none(self):
        linked = self.root / "linked"
        self.git(self.checkout, "worktree", "add", "-q", "--detach", str(linked))
        self.assertEqual(git_state.main_checkout(linked), self.checkout)
        self.assertIsNone(git_state.main_checkout(self.checkout))

    def test_a_worktree_of_a_bare_repository_has_no_main_checkout(self):
        # As a dispatched checkout on the second Mac is: its repository's parent holds no project.
        placed = self.root / "slot/project"
        self.git(self.origin, "worktree", "add", "-q", "--detach", str(placed), "main")
        self.assertIsNone(git_state.main_checkout(placed))


class SiblingRequestTests(Fixture):
    def test_each_extension_folder_is_asked_to_follow_what_this_macs_checkout_of_it_calls_origin_main(self):
        project = self.root / "project"
        project.mkdir()
        (project / "queuekeeper.json").write_text(
            '{"extensions": ["queuekeeper.apple", {"module": "kit.hooks", "path": "../checkout/src/python"},'
            ' {"module": "absent.hooks", "path": "../absent/src"}]}')
        main = self.git(self.checkout, "rev-parse", "refs/remotes/origin/main")
        self.assertEqual(remote.sibling_requests(project),
                         [{"path": "Development/checkout", "root": remote.ROOT, "want": main},
                          {"path": "Development/absent", "root": remote.ROOT}])


class SiblingLinkTests(unittest.TestCase):
    def test_the_second_mac_links_its_own_checkout_of_each_extension_folder_beside_a_dispatched_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            slot, kit = home / "build-host/checkouts/Example/0", home / "Development/kit"
            (slot / "Example").mkdir(parents=True)
            (kit / "src/python").mkdir(parents=True)
            # What an earlier layout left under the name goes: a placed copy that is no longer sent.
            (slot / "kit/old").mkdir(parents=True)
            said = []
            with patch.object(Path, "home", return_value=home), patch.object(offload_host, "say", said.append):
                offload_host.link_siblings(slot, ["kit", "absent"])
                offload_host.link_siblings(slot, ["kit", "absent"])  # again, with the link in place
            self.assertTrue((slot / "kit").is_symlink())
            self.assertEqual((slot / "kit").resolve(), kit)
            self.assertFalse((slot / "absent").exists())
            self.assertEqual(sum("has no" in line and "absent" in line for line in said), 2)
            self.assertEqual(sum("is this Mac's" in line for line in said), 1)
            # What `qk doctor` asks before any dispatch (#117): which it has no checkout of, creating none.
            self.assertEqual(offload_host.missing_siblings(["kit", "absent"], home=home), ["absent"])
            self.assertFalse((home / "Development/absent").exists())


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
