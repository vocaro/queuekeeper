"""What git_state reads of a checkout, against throwaway repositories."""
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src/python"))
import host_isolation  # noqa: F401 -- first, as every test module
from queuekeeper import cli, git_state, offload_host, remote


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

    def test_git_is_asked_once_for_the_variables_that_aim_it_at_a_repository(self):
        # Asked before every Git command, it cost the merge queue a process start each (vocaro/queuekeeper#257).
        real, asked = subprocess.run, []
        def counted(command, *arguments, **options):
            if command[1:] == ["rev-parse", "--local-env-vars"]: asked.append(command)
            return real(command, *arguments, **options)
        with patch.dict(git_state.LOCAL_VARIABLES, clear=True), patch.object(git_state.subprocess, "run", counted):
            first = git_state.local_variables()
            self.assertIn("GIT_DIR", first)
            self.assertIn("GIT_WORK_TREE", first)
            with patch.dict(os.environ, {"GIT_DIR": str(self.root / "elsewhere")}):
                self.assertNotIn("GIT_DIR", git_state.environment())
            self.assertEqual(git_state.local_variables(), first)
        self.assertEqual(len(asked), 1)


class MainCheckoutTests(Fixture):
    def test_a_linked_worktree_names_its_main_checkout_and_the_main_checkout_none(self):
        linked = self.root / "linked"
        self.git(self.checkout, "worktree", "add", "-q", "--detach", str(linked))
        self.assertEqual(git_state.main_checkout(linked), self.checkout)
        self.assertIsNone(git_state.main_checkout(self.checkout))

    def test_a_worktree_of_a_bare_repository_has_no_main_checkout(self):
        # As a dispatched checkout on another Mac is: its repository's parent holds no project.
        placed = self.root / "slot/project"
        self.git(self.origin, "worktree", "add", "-q", "--detach", str(placed), "main")
        self.assertIsNone(git_state.main_checkout(placed))


class SiblingRequestTests(Fixture):
    def test_each_plugin_folder_is_asked_to_follow_what_this_macs_checkout_of_it_calls_origin_main(self):
        project = self.root / "project"
        project.mkdir()
        (project / "queuekeeper.json").write_text(
            '{"plugins": ["queuekeeper_apple", {"module": "kit.hooks", "path": "../checkout/src/python"},'
            ' {"module": "absent.hooks", "path": "../absent/src"}]}')
        main = self.git(self.checkout, "rev-parse", "refs/remotes/origin/main")
        self.assertEqual(remote.sibling_requests(project),
                         [{"path": "Development/checkout", "root": remote.ROOT, "want": main},
                          {"path": "Development/absent", "root": remote.ROOT}])


class MissingSiblingTests(unittest.TestCase):
    def test_doctor_asks_which_plugin_folder_the_farm_mac_has_no_checkout_of_and_creates_none(self):
        # What `qk doctor` asks before any dispatch (#117). A dispatch takes each folder's pinned commit from that
        # checkout (offload_host.place_siblings, tests/integration/test_pins.py).
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            (home / "Development/kit/src/python").mkdir(parents=True)
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


class OffMainTests(Fixture):
    """What qk says when the Queuekeeper it loaded is a main checkout off main (vocaro/queuekeeper#184)."""

    def test_a_checkout_on_main_says_nothing_even_with_commits_of_its_own(self):
        self.assertIsNone(git_state.off_main(self.checkout))
        self.commit("Mine")
        self.assertIsNone(git_state.off_main(self.checkout))

    def test_a_checkout_on_a_work_branch_names_the_branch_and_its_commit(self):
        # As the shared checkout was on 2026-11-15, for 45 minutes.
        self.git(self.checkout, "switch", "-q", "-c", "unread-paths")
        work = self.commit("Work in progress")
        said = git_state.off_main(self.checkout)
        self.assertIn(f"Queuekeeper loads from {self.checkout}, on branch unread-paths at {work[:12]}, not main", said)
        self.assertIn("linked worktree", said)

    def test_a_detached_head_says_so_only_at_a_commit_main_lacks(self):
        first = self.git(self.checkout, "rev-parse", "HEAD")
        self.git(self.checkout, "switch", "-q", "--detach", first)
        self.assertIsNone(git_state.off_main(self.checkout))
        lacking = self.commit("Not on main")
        self.assertIn(f"detached at {lacking[:12]}, a commit main lacks", git_state.off_main(self.checkout))
        # One that origin/main holds, though this checkout's main is behind it, is landed code.
        self.git(self.checkout, "push", "-q", "origin", "HEAD:main")
        self.assertIsNone(git_state.off_main(self.checkout))

    def test_a_linked_worktree_on_a_branch_and_a_folder_without_git_say_nothing(self):
        # A session's worktree and each queue worker's copy are linked worktrees, never the shared checkout.
        linked = self.root / "linked"
        self.git(self.checkout, "worktree", "add", "-q", "-b", "work", str(linked))
        self.assertIsNone(git_state.off_main(linked))
        self.assertIsNone(git_state.off_main(self.root / "nowhere"))

    def test_every_qk_command_says_it_first_on_standard_error(self):
        self.git(self.checkout, "switch", "-q", "-c", "work")
        with patch.object(cli, "TOOLKIT_ROOT", self.checkout), \
                patch.object(sys, "stderr", io.StringIO()) as error, patch.object(sys, "stdout", io.StringIO()), \
                self.assertRaises(SystemExit):
            cli.main(["--project", str(self.checkout), "--version"])
        self.assertTrue(error.getvalue().startswith(f"queuekeeper: Queuekeeper loads from {self.checkout}, on branch "
                                                    "work at "), error.getvalue())


class ReleaseTests(Fixture):
    """Which release a main checkout installed from a release tag is at (vocaro/queuekeeper#351)."""

    def test_a_checkout_on_a_branch_is_no_release_even_at_a_tagged_commit(self):
        self.git(self.checkout, "tag", "v1.0.0")
        self.assertIsNone(git_state.release(self.checkout))

    def test_a_checkout_detached_at_a_release_tag_names_the_highest(self):
        self.git(self.checkout, "tag", "v1.9.0")
        self.git(self.checkout, "tag", "v1.10.0")
        self.git(self.checkout, "tag", "nightly")
        self.git(self.checkout, "switch", "-q", "--detach", "v1.9.0")
        self.assertEqual(git_state.release(self.checkout), "v1.10.0")

    def test_a_commit_no_release_tag_names_is_no_release(self):
        self.git(self.checkout, "tag", "nightly")
        self.git(self.checkout, "tag", "v1.0")
        self.git(self.checkout, "switch", "-q", "--detach", "HEAD")
        self.assertIsNone(git_state.release(self.checkout))

    def test_a_linked_worktree_and_a_folder_without_git_are_no_release(self):
        self.git(self.checkout, "tag", "v1.0.0")
        linked = self.root / "linked"
        self.git(self.checkout, "worktree", "add", "-q", "--detach", str(linked), "v1.0.0")
        self.assertIsNone(git_state.release(linked))
        self.assertIsNone(git_state.release(self.root / "nowhere"))


class SharedChangesTests(Fixture):
    def test_only_changes_in_the_loaded_main_checkout_warn(self):
        (self.checkout / ".git/info/exclude").write_text("ignored\n")
        (self.checkout / "ignored").touch()
        self.assertIsNone(git_state.shared_changes(self.checkout))

        module = self.checkout / "module.py"
        module.write_text("# module\n")
        for staged in (False, True):
            with self.subTest(staged=staged):
                if staged: self.git(self.checkout, "add", module.name)
                warning = git_state.shared_changes(self.checkout)
                self.assertIn(f"{self.checkout}, whose main checkout has uncommitted changes", warning)
                self.assertIn("git status", warning)
                self.assertIn("linked worktree", warning)
        self.git(self.checkout, "commit", "-qm", "Add a module")
        module.write_text("# local edit\n")
        self.assertIn("uncommitted changes", git_state.shared_changes(self.checkout))

        linked = self.root / "linked"
        self.git(self.checkout, "worktree", "add", "-q", "-b", "work", str(linked))
        (linked / module.name).write_text("# worktree edit\n")
        self.assertIsNone(git_state.shared_changes(linked))
        self.assertIsNone(git_state.shared_changes(self.root / "nowhere"))


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
