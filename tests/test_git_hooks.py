"""What `qk doctor` checks and repairs of core.hooksPath, against throwaway repositories (vocaro/queuekeeper#6), and
of a fresh clone's, which has none (vocaro/queuekeeper#131)."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/python"))
import host_isolation  # first, as every test module
from queuekeeper import cli, config, git_hooks


class Repository(unittest.TestCase):
    """A repository that tracks its hooks in .githooks, with a linked worktree."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)
        identity = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "Test",
                    "GIT_AUTHOR_EMAIL": "test@example.com", "GIT_COMMITTER_NAME": "Test",
                    "GIT_COMMITTER_EMAIL": "test@example.com"}
        without = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
        self.enterContext(patch.dict(os.environ, {**without, **identity}, clear=True))
        self.main = self.root / "main"
        self.git(self.root, "init", "-q", "-b", "main", str(self.main))
        host_isolation.executable(self.hook(self.main), "#!/bin/sh\nexit 0\n")
        self.git(self.main, "add", "-A")
        self.git(self.main, "commit", "-q", "-m", "First")
        self.linked = self.root / "linked"
        self.git(self.main, "worktree", "add", "-q", "-b", "work", str(self.linked))

    def hook(self, checkout, name="pre-push"):
        (checkout / ".githooks").mkdir(exist_ok=True)
        return checkout / ".githooks" / name

    def git(self, cwd, *arguments):
        return subprocess.run(["git", "-C", str(cwd), *arguments], check=True, capture_output=True, text=True).stdout.strip()

    def value(self, checkout, scope="--get"):
        ran = subprocess.run(["git", "-C", str(checkout), "config", *([scope] if scope != "--get" else []), "--get",
                              git_hooks.KEY], capture_output=True, text=True)
        return ran.stdout.strip() or None


class HooksPathTests(Repository):
    def test_a_project_with_no_hooks_path_or_a_relative_one_is_warned_of_nothing(self):
        self.assertEqual(git_hooks.check(self.linked), [])
        self.git(self.main, "config", git_hooks.KEY, ".githooks")
        self.assertEqual(git_hooks.check(self.main) + git_hooks.check(self.linked), [])
        self.assertEqual(git_hooks.check(self.root), [])  # no repository

    def test_an_absolute_path_into_the_main_checkout_is_made_relative(self):
        self.git(self.main, "config", git_hooks.KEY, str(self.main / ".githooks"))
        warning, = git_hooks.check(self.linked)
        self.assertIn("repaired core.hooksPath: the repository's value was the absolute", warning)
        self.assertEqual(self.value(self.main), ".githooks")
        self.assertEqual(git_hooks.check(self.linked), [])

    def test_a_worktree_s_own_absolute_value_is_cleared_when_it_names_the_same_folder(self):
        # What Claude Code leaves in a worktree it creates: the relative value, resolved in the main checkout.
        self.git(self.main, "config", git_hooks.KEY, ".githooks")
        self.git(self.main, "config", "extensions.worktreeConfig", "true")
        self.git(self.linked, "config", "--worktree", git_hooks.KEY, str(self.main / ".githooks"))
        warning, = git_hooks.check(self.linked)
        self.assertIn("this worktree's own value", warning)
        self.assertIn("is cleared", warning)
        self.assertEqual((self.value(self.linked, "--worktree"), self.value(self.linked)), (None, ".githooks"))
        self.assertEqual(git_hooks.check(self.linked), [])

    def test_a_worktree_s_own_value_is_the_installation_when_the_repository_has_none(self):
        self.git(self.main, "config", "extensions.worktreeConfig", "true")
        self.git(self.linked, "config", "--worktree", git_hooks.KEY, str(self.main / ".githooks"))
        self.assertEqual(len(git_hooks.check(self.linked)), 1)
        self.assertEqual((self.value(self.linked, "--worktree"), self.value(self.main, "--local")), (None, ".githooks"))

    def test_a_worktree_s_value_for_another_folder_is_named_and_left(self):
        self.git(self.main, "config", git_hooks.KEY, ".githooks")
        self.git(self.main, "config", "extensions.worktreeConfig", "true")
        self.git(self.linked, "config", "--worktree", git_hooks.KEY, "/elsewhere/hooks")
        found = git_hooks.check(self.linked)
        self.assertIn("this worktree has its own core.hooksPath, /elsewhere/hooks", found[0])
        self.assertIn("names /elsewhere/hooks, which does not exist here", found[1])
        self.assertEqual(self.value(self.linked, "--worktree"), "/elsewhere/hooks")

    def test_without_worktree_configuration_the_shared_value_is_not_taken_for_an_override(self):
        # There --worktree reads and writes the repository's value, and clearing it would remove the installation.
        self.git(self.main, "config", git_hooks.KEY, ".githooks")
        self.assertEqual(git_hooks.check(self.linked), [])
        self.assertEqual(self.value(self.main), ".githooks")

    def test_a_missing_folder_and_a_hook_that_cannot_run_are_said(self):
        self.git(self.main, "config", git_hooks.KEY, "hooks-that-moved")
        self.assertIn("Git skips a missing hooks folder without a word", git_hooks.check(self.linked)[0])
        self.git(self.main, "config", git_hooks.KEY, ".githooks")
        self.hook(self.linked, "post-checkout").write_text("#!/bin/sh\n")
        self.assertEqual(git_hooks.check(self.linked), [".githooks/post-checkout is not executable, so Git skips it "
                                                        "without a word: chmod +x it"])

    def test_hooks_outside_the_repository_are_left_alone(self):
        own = self.root / "my-hooks"
        self.git(self.main, "config", git_hooks.KEY, str(own))
        self.assertEqual(git_hooks.check(self.linked), [])
        self.assertEqual(self.value(self.main), str(own))

    def test_an_absolute_path_this_checkout_has_no_folder_for_is_said_and_kept(self):
        self.git(self.main, "config", git_hooks.KEY, str(self.main / ".githooks"))
        shutil.rmtree(self.linked / ".githooks")
        warning, = git_hooks.check(self.linked)
        self.assertIn("so every worktree runs that one checkout's hooks", warning)
        self.assertEqual(self.value(self.main), str(self.main / ".githooks"))



class FreshCloneTests(Repository):
    """A checkout with no core.hooksPath at all, as a fresh clone has (vocaro/queuekeeper#131)."""

    def queuekeeper_hook(self):
        host_isolation.executable(self.hook(self.main), '#!/bin/sh\nexec python3 scripts/qk pre-push "$@"\n')
        self.git(self.main, "commit", "-q", "-am", "Hand the push to Queuekeeper")
        self.git(self.linked, "merge", "-q", "main")

    def test_the_declared_folder_is_set_where_the_path_is_unset(self):
        warning, = git_hooks.check(self.linked, ".githooks")
        self.assertIn("repaired core.hooksPath: it was unset", warning)
        self.assertEqual((self.value(self.main, "--local"), self.value(self.linked)), (".githooks", ".githooks"))
        self.assertEqual(git_hooks.check(self.linked, ".githooks") + git_hooks.check(self.main, ".githooks"), [])

    def test_a_declared_folder_this_checkout_lacks_is_said_and_nothing_set(self):
        warning, = git_hooks.check(self.linked, "tools/hooks")
        self.assertIn("gitHooks names tools/hooks, which does not exist here", warning)
        self.assertIsNone(self.value(self.main))

    def test_a_tracked_hook_that_runs_qk_pre_push_is_said_without_the_key_and_nothing_set(self):
        self.assertEqual(git_hooks.check(self.linked), [])  # its hook is its own, not Queuekeeper's
        self.queuekeeper_hook()
        warning, = git_hooks.check(self.linked)
        self.assertIn("core.hooksPath is unset", warning)
        self.assertIn("though .githooks/pre-push runs qk pre-push: run `git config core.hooksPath .githooks`", warning)
        self.assertIsNone(self.value(self.main))
        # The negative control: the same hook in a folder not named for hooks is not taken for the project's.
        self.git(self.main, "mv", ".githooks", "scripts")
        self.git(self.main, "commit", "-q", "-m", "Move")
        self.assertEqual(git_hooks.check(self.main), [])

    def test_a_path_that_names_another_folder_than_the_declared_one_is_said_and_kept(self):
        self.git(self.main, "config", git_hooks.KEY, "hooks-that-moved")
        found = git_hooks.check(self.linked, ".githooks")
        self.assertIn("core.hooksPath is hooks-that-moved, but queuekeeper.json's gitHooks names .githooks", found[0])
        self.assertIn("does not exist here", found[1])
        self.assertEqual(self.value(self.main), "hooks-that-moved")

    def test_a_person_s_own_hooks_hide_the_declared_folder_and_are_said(self):
        own = self.root / "my-hooks"
        settings = self.root / "global-config"
        self.git(self.root, "config", "--file", str(settings), git_hooks.KEY, str(own))
        with patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(settings)}):
            self.assertEqual(git_hooks.check(self.linked), [])
            warning, = git_hooks.check(self.linked, ".githooks")
        self.assertIn(f"core.hooksPath is {own}, but queuekeeper.json's gitHooks names .githooks", warning)
        self.assertIsNone(self.value(self.main, "--local"))

    def test_doctor_hooks_says_each_finding_on_a_line_and_fails_only_on_a_warning(self):
        def ask(declared):
            printed = io.StringIO()
            with contextlib.redirect_stdout(printed):
                status = cli.hooks_path(self.linked, {"gitHooks": declared} if declared else {})
            return status, printed.getvalue().splitlines()
        self.assertEqual(ask(None), (0, ["fine: core.hooksPath is unset, and the project declares no hooks of its own"]))
        self.queuekeeper_hook()
        status, (line,) = ask(None)
        self.assertEqual(status, 1)
        self.assertTrue(line.startswith("warning: core.hooksPath is unset"), line)
        status, (line,) = ask(".githooks")
        self.assertEqual(status, 0)
        self.assertTrue(line.startswith("repaired core.hooksPath: it was unset"), line)
        self.assertEqual(ask(".githooks"), (0, ["fine: core.hooksPath is .githooks"]))

    def test_the_key_is_a_folder_inside_the_checkout(self):
        def parse(folder):
            return config.parse(json.dumps({"schemaVersion": 1, "name": "p", "extensions": [], "gitHooks": folder}))
        self.assertEqual(parse("tools/repository/hooks")["gitHooks"], "tools/repository/hooks")
        for folder in ["", "/abs/hooks", "../hooks", ".", " hooks", 3]:
            with self.subTest(folder=folder), self.assertRaisesRegex(ValueError, "gitHooks must be the folder"):
                parse(folder)
        schema = json.loads((config.TOOLKIT_ROOT / "schemas/queuekeeper.schema.json").read_text())
        self.assertEqual(schema["properties"]["gitHooks"]["type"], "string")


if __name__ == "__main__":
    unittest.main()
