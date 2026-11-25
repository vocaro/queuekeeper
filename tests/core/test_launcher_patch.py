"""The launcher patch updates an exact historical copy and leaves custom launchers for review."""
from contextlib import redirect_stderr, redirect_stdout
from io import BytesIO, StringIO, TextIOWrapper
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import host_isolation  # noqa: F401 -- establish a private host environment before using Queuekeeper
from queuekeeper import cli, git_state, launcher_patch


OLD = b'#!/usr/bin/env python3\n"""A project\'s launcher for Queuekeeper: example."""\n\nprint("old")\n'
NEW = OLD.replace(b'"old"', b'"new"')
FILE = launcher_patch.LAUNCHER


class LauncherPatch(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.source, self.project = self.directory / "queuekeeper", self.directory / "project"
        self.environment = git_state.environment()
        for root in (self.source, self.project):
            root.mkdir()
            self.git(root, "init", "--quiet", "--initial-branch=main")
            (root / FILE).parent.mkdir()
            self.commit(root, OLD)
        self.commit(self.source, NEW)

    def git(self, directory, *args, data=None):
        result = subprocess.run(["git", "-C", str(directory), *args], input=data, capture_output=True,
                                env=self.environment, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        return result.stdout

    def commit(self, directory, content):
        (directory / FILE).write_bytes(content)
        self.git(directory, "add", FILE)
        self.git(directory, "-c", "user.name=Example", "-c", "user.email=example@example.invalid",
                 "-c", "commit.gpgSign=false", "commit", "--quiet", "-m", "Launcher")

    def test_command_emits_reviewable_patch_without_mutating_project_or_loading_plugins(self):
        with TextIOWrapper(BytesIO()) as stdout, redirect_stdout(stdout), redirect_stderr(StringIO()) as stderr, \
                patch.object(cli, "TOOLKIT_ROOT", self.source), patch.object(cli, "load_plugins") as load:
            status = cli.main(["--project", str(self.project), "launcher-patch"])
            prepared = stdout.buffer.getvalue()
            self.assertEqual((status, stderr.getvalue()), (0, ""))
            load.assert_not_called()
        self.assertIn(b"diff --git a/scripts/qk b/scripts/qk", prepared)
        self.assertEqual(self.git(self.project, "status", "--porcelain"), b"")
        self.assertEqual((self.project / FILE).read_bytes(), OLD)
        self.git(self.project, "apply", "--check", data=prepared)
        self.git(self.project, "apply", data=prepared)
        self.assertEqual((self.project / FILE).read_bytes(), NEW)
        self.assertEqual(self.git(self.project, "diff", "--cached"), b"")

    def test_current_copy_has_no_patch(self):
        self.commit(self.project, NEW)
        self.assertEqual(launcher_patch.prepare(self.project, self.source), b"")

    def test_bare_command_prepares_a_refresh_the_project_launcher_can_run(self):
        # Give the source its real entry point and core package, keeping the launcher's history
        # in the fixture so the test also works when the suite runs without Git history of its own.
        toolkit = cli.TOOLKIT_ROOT
        shutil.copytree(toolkit / "src/python", self.source / "src/python",
                        ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copyfile(toolkit / "pyproject.toml", self.source / "pyproject.toml")  # where the core's version is
        command = self.source / "bin/qk"
        command.parent.mkdir()
        shutil.copyfile(toolkit / "bin/qk", command)
        self.commit(self.source, (toolkit / FILE).read_bytes())
        environment = host_isolation.reduced(self.environment)
        environment.pop("PYTHONPATH", None)
        prepared = subprocess.run([sys.executable, str(command), "launcher-patch"], cwd=self.project,
                                  env=environment, capture_output=True, timeout=host_isolation.stretched(60))
        self.assertEqual(prepared.returncode, 0, prepared.stderr.decode())
        self.assertEqual(prepared.stderr, b"")
        self.assertEqual(self.git(self.project, "status", "--porcelain"), b"")
        self.git(self.project, "apply", "--check", data=prepared.stdout)
        self.git(self.project, "apply", data=prepared.stdout)

        refreshed = subprocess.run([sys.executable, FILE, "--version"], cwd=self.project,
                                   env={**environment, "QUEUEKEEPER_ROOT": str(self.source)},
                                   capture_output=True, text=True, timeout=host_isolation.stretched(60))
        self.assertEqual(refreshed.returncode, 0, refreshed.stderr)
        self.assertEqual(refreshed.stdout.strip(), cli.__version__)
        self.assertEqual(self.git(self.project, "status", "--porcelain"), b" M scripts/qk\n")

    def publish_fixture_bases(self):
        for root in (self.project, self.source):
            self.git(root, "update-ref", "refs/remotes/origin/main", "HEAD")

    def test_queue_submits_only_the_reviewed_stock_launcher_as_an_ordinary_landing(self):
        self.publish_fixture_bases()
        self.git(self.project, "config", "user.name", "Example")
        self.git(self.project, "config", "user.email", "example@example.invalid")
        (self.project / "unrelated").write_text("leave this staged")
        self.git(self.project, "add", "unrelated")
        refs = self.git(self.project, "show-ref")
        index = self.git(self.project, "ls-files", "--stage")
        reviewed = self.directory / "reviewed.patch"
        reviewed.write_bytes(launcher_patch.prepare(self.project, self.source))

        real_run = subprocess.run
        def capture(command, **options):
            if command[0] == "git":
                return real_run(command, **options)
            launch.append((command, options))
            return subprocess.CompletedProcess(command, 0)

        launch = []
        with patch.object(subprocess, "run", side_effect=capture), \
                patch.object(cli, "TOOLKIT_ROOT", self.source), redirect_stderr(StringIO()) as stderr:
            self.assertEqual(cli.main(["--project", str(self.project), "launcher-patch", "--queue", str(reviewed)]), 0)
        [(command, options)] = launch
        proposed = command[-1]
        self.assertEqual(command[:-1], [sys.executable, str(self.source / "bin/qk"), "--project",
                                        str(self.project), "land"])
        self.assertEqual(options["cwd"], self.project)
        self.assertIn(f"qk land {proposed}", stderr.getvalue())
        self.assertEqual(self.git(self.project, "diff-tree", "--no-commit-id", "--name-only", "-r", proposed),
                         b"scripts/qk\n")
        self.assertEqual(self.git(self.project, "show", f"{proposed}:{FILE}"), NEW)
        self.assertEqual(self.git(self.project, "rev-parse", f"{proposed}^"),
                         self.git(self.project, "rev-parse", "origin/main"))
        self.assertEqual(self.git(self.project, "show-ref"), refs)
        self.assertEqual(self.git(self.project, "ls-files", "--stage"), index)
        self.assertEqual((self.project / FILE).read_bytes(), OLD)

    def test_queue_refuses_an_unreviewed_patch_and_unpublished_commits(self):
        self.publish_fixture_bases()
        reviewed = launcher_patch.prepare(self.project, self.source)
        with self.assertRaisesRegex(ValueError, "reviewed patch must match"):
            launcher_patch.proposal(self.project, self.source, reviewed + b"# changed")
        self.commit(self.source, NEW.replace(b'"new"', b'"local"'))
        with self.assertRaisesRegex(ValueError, "Queuekeeper source checkout at its fetched origin/main"):
            launcher_patch.proposal(self.project, self.source, reviewed)
        self.git(self.source, "reset", "--hard", "origin/main")
        self.commit(self.project, OLD + b"# customized\n")
        with self.assertRaisesRegex(ValueError, "project checkout at its fetched origin/main"):
            launcher_patch.proposal(self.project, self.source, reviewed)

    def test_patch_uses_source_head_even_when_the_source_launcher_has_local_edits(self):
        (self.source / FILE).write_bytes(NEW + b"# local source edit\n")
        self.git(self.project, "apply", data=launcher_patch.prepare(self.project, self.source))
        self.assertEqual((self.project / FILE).read_bytes(), NEW)

    def test_project_path_must_name_the_checkout_root(self):
        with self.assertRaisesRegex(ValueError, "project checkout root"):
            launcher_patch.prepare(self.project / "scripts", self.source)

    def test_custom_copy_with_stock_header_is_rejected(self):
        custom = OLD + b"# project-owned change\n"
        self.commit(self.project, custom)
        with self.assertRaisesRegex(ValueError, "does not match a stock copy"):
            launcher_patch.prepare(self.project, self.source)
        self.assertEqual((self.project / FILE).read_bytes(), custom)

    def test_launcher_changes_are_rejected_even_if_the_bytes_match_a_stock_copy(self):
        (self.project / FILE).write_bytes(NEW)
        with self.assertRaisesRegex(ValueError, "staged or unstaged changes"):
            launcher_patch.prepare(self.project, self.source)
        self.git(self.project, "add", FILE)
        (self.project / FILE).write_bytes(OLD)
        with self.assertRaisesRegex(ValueError, "staged or unstaged changes"):
            launcher_patch.prepare(self.project, self.source)

    def test_staged_executable_mode_change_is_rejected(self):
        self.git(self.project, "update-index", "--chmod=+x", FILE)
        with self.assertRaisesRegex(ValueError, "staged or unstaged changes"):
            launcher_patch.prepare(self.project, self.source)

    def test_shallow_or_missing_source_checkout_is_rejected(self):
        shallow = self.directory / "shallow"
        self.git(self.directory, "clone", "--quiet", "--depth=1", self.source.as_uri(), str(shallow))
        for source in (shallow, self.directory):
            with self.subTest(source=source.name), self.assertRaisesRegex(ValueError, "full Queuekeeper source checkout"):
                launcher_patch.prepare(self.project, source)
