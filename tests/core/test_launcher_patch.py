"""The launcher patch updates an exact historical copy and leaves custom launchers for review."""
from contextlib import redirect_stderr, redirect_stdout
from io import BytesIO, StringIO, TextIOWrapper
from pathlib import Path
import subprocess
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
