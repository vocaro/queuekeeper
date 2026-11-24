"""The project launcher and command started by an interpreter older than Queuekeeper runs under."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src/python"))
import host_isolation  # noqa: F401 -- first, as every test module
from host_isolation import stretched

ROOT = Path(__file__).resolve().parents[2]
OLD = "/usr/bin/python3"


def older():
    """Whether the Mac's system Python is one the launcher refuses."""
    if not os.access(OLD, os.X_OK): return False
    told = subprocess.run([OLD, "-c", "import sys; sys.exit(sys.version_info >= (3, 14))"], capture_output=True)
    return told.returncode == 0


@unittest.skipUnless(older(), f"{OLD} is not older than Python 3.14")
class OlderInterpreterTests(unittest.TestCase):
    launcher = "scripts/qk"

    def run_launcher(self, path):
        environment = {name: value for name, value in os.environ.items() if name != "QUEUEKEEPER_LAUNCHER_PYTHON"}
        return subprocess.run([OLD, str(ROOT / self.launcher), "--version"], env={**environment, "PATH": path},
                              capture_output=True, text=True, timeout=stretched(60))

    def test_it_hands_over_to_the_first_python3_on_the_path_that_is_new_enough(self):
        # As a project does that runs its lanes' launcher under a pinned environment's interpreter, whose folder
        # is first on the PATH: the older python3 there is passed over.
        result = self.run_launcher(os.pathsep.join(["/usr/bin", str(Path(sys.executable).parent), "/bin"]))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout.strip(), r"^\d+\.\d+")

    def test_it_refuses_once_when_the_path_has_no_newer_python3(self):
        result = self.run_launcher("/usr/bin:/bin")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(result.stderr.count("needs Python 3.14 or newer"), 1, result.stderr)


class CommandOlderInterpreterTests(OlderInterpreterTests):
    """The command used by bare runners in projects without scripts/qk has the same interpreter contract."""
    launcher = "bin/qk"


class BootstrapTests(unittest.TestCase):
    def test_a_main_checkout_import_failure_names_the_checkout_before_the_traceback(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "toolkit"
            project = Path(folder) / "project"
            core = root / "src/python/queuekeeper"
            core.mkdir(parents=True)
            (core / "__init__.py").touch()
            shutil.copyfile(ROOT / "src/python/queuekeeper/checkout.py", core / "checkout.py")
            (core / "cli.py").write_text("from .config import NOT_PREFLIGHT\n")
            (core / "config.py").write_text("# The working tree is missing the new name.\n")
            environment = host_isolation.reduced(os.environ, QUEUEKEEPER_ROOT=str(root))
            environment.pop("PYTHONPATH", None)
            git = root / ".git"
            for relative, checkout in [("bin/qk", root), ("scripts/qk", project)]:
                launcher = checkout / relative
                launcher.parent.mkdir(parents=True)
                shutil.copyfile(ROOT / relative, launcher)
                for shared in (True, False):
                    with self.subTest(launcher=relative, shared=shared):
                        if shared:
                            git.mkdir()
                            (git / "HEAD").write_text("ref: refs/heads/main\n")
                        else:
                            shutil.rmtree(git)
                            git.write_text("gitdir: /linked-worktree\n")
                        result = subprocess.run([sys.executable, str(launcher), "--version"], env=environment,
                                                capture_output=True, text=True, timeout=stretched(60))
                        self.assertEqual(result.returncode, 1, result.stderr)
                        self.assertEqual(result.stdout, "")
                        self.assertIn("ImportError: cannot import name 'NOT_PREFLIGHT'", result.stderr)
                        if shared:
                            self.assertTrue(result.stderr.startswith(
                                f"queuekeeper: could not load Queuekeeper from {root.resolve()}. "
                                "Inspect that checkout with git status.\n"), result.stderr)
                        else:
                            self.assertTrue(result.stderr.startswith("Traceback"), result.stderr)
                            git.unlink()


class FollowedRootTests(unittest.TestCase):
    """bin/qk, the installed qk among them, hands over to the checkout QUEUEKEEPER_ROOT names when that is another
    checkout, as a merge queue worker's copy is to its lanes, rather than load a second Queuekeeper that a project's
    hooks refuse (vocaro/queuekeeper#340)."""
    SELECTED = ("#!/usr/bin/env python3\nimport json, os, sys\n"
                "print(json.dumps({'argv': sys.argv[1:], 'followed': os.environ.get('QUEUEKEEPER_FOLLOWED_ROOT')}))\n")

    def setUp(self):
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.selected = self.base / "selected"
        (self.selected / "bin").mkdir(parents=True)
        (self.selected / "bin/qk").write_text(self.SELECTED)
        (self.selected / "src/python/queuekeeper").mkdir(parents=True)
        (self.selected / "src/python/queuekeeper/cli.py").write_text("")

    def qk(self, **variables):
        environment = host_isolation.reduced(os.environ, **variables)
        return subprocess.run([sys.executable, str(ROOT / "bin/qk"), "--project", str(self.base), "--version"],
                              env=environment, cwd=self.base, capture_output=True, text=True, timeout=stretched(60))

    def test_it_hands_over_to_the_selected_checkout_once(self):
        result = self.qk(QUEUEKEEPER_ROOT=str(self.selected))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"argv": ["--project", str(self.base), "--version"],
                                                     "followed": str(self.selected)})

    def test_it_runs_itself_when_there_is_nothing_else_to_hand_over_to(self):
        # Negative controls: its own checkout named, a folder that is no Queuekeeper checkout, and a handover
        # already made, which never hands over again.
        (self.base / "bin").mkdir()
        (self.base / "bin/qk").write_text(self.SELECTED)
        for variables in ({"QUEUEKEEPER_ROOT": str(ROOT)}, {"QUEUEKEEPER_ROOT": str(self.base)},
                          {"QUEUEKEEPER_ROOT": str(self.selected), "QUEUEKEEPER_FOLLOWED_ROOT": str(ROOT)}):
            with self.subTest(variables=variables):
                result = self.qk(**variables)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertRegex(result.stdout.strip(), r"^\d+\.\d+")


class EarlierLauncherTests(unittest.TestCase):
    """A project's scripts/qk from before the plugins left the core puts only src/python on the import path and
    calls cli.main. Thirteen projects carry one, and each must run a current Queuekeeper (vocaro-engineering#497)."""
    SHAPE = ("import os, sys; sys.path.insert(0, sys.argv[1]); from queuekeeper.cli import main\n"
             "try: main(['--project', sys.argv[2], '--version'])\n"
             "except SystemExit as stop: status = stop.code\n"
             "import queuekeeper_apple; print(os.environ.get('PYTHONPATH', '')); raise SystemExit(status)")

    def run_shape(self, source):
        with tempfile.TemporaryDirectory() as project:
            (Path(project) / "queuekeeper.json").write_text(json.dumps(
                {"schemaVersion": 1, "name": "earlier", "plugins": ["queuekeeper_apple"], "lanes": {}}))
            # Neither the composition this test process runs under nor a checkout's own launcher reaches it.
            environment = {name: value for name, value in os.environ.items() if name != "PYTHONPATH"}
            return subprocess.run([sys.executable, "-c", self.SHAPE, str(source), project], env=environment,
                                  cwd=project, capture_output=True, text=True, timeout=stretched(60))

    def test_it_loads_the_checkouts_plugins_and_hands_them_to_python_children(self):
        result = self.run_shape(ROOT / "src/python")
        self.assertEqual(result.returncode, 0, result.stderr)
        version, paths = result.stdout.strip().splitlines()
        self.assertRegex(version, r"^\d+\.\d+")
        self.assertEqual(paths.split(os.pathsep)[:2], [str(ROOT / "src/python"), str(ROOT / "plugins/apple/src")])

    def test_a_core_with_no_workspace_manifest_beside_it_composes_nothing(self):
        # As an installed package is: its plugins come from distribution metadata, and here there are none.
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "lib/python/site-packages"
            shutil.copytree(ROOT / "src/python/queuekeeper", source / "queuekeeper",
                            ignore=shutil.ignore_patterns("__pycache__"))
            result = self.run_shape(source)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("No module named 'queuekeeper_apple'", result.stderr)


class MessageCommandTests(unittest.TestCase):
    """What Queuekeeper's messages tell a person or a session to run names `python3 scripts/qk` only in a project that
    holds that copy of the launcher, and the installed `qk` in one that dropped it (vocaro/queuekeeper#339)."""

    def setUp(self):
        from queuekeeper import config
        self.config = config
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        base = Path(folder.name).resolve()
        self.with_copy, self.without = base / "with-copy", base / "without"
        for project in (self.with_copy, self.without):
            (project / ".git").mkdir(parents=True)
            (project / "deep/down").mkdir(parents=True)
        (self.with_copy / "scripts").mkdir()
        shutil.copyfile(ROOT / "scripts/qk", self.with_copy / "scripts/qk")
        self.base = base

    def in_directory(self, folder):
        before = os.getcwd()
        os.chdir(folder)
        self.addCleanup(os.chdir, before)

    def test_a_message_names_the_copy_only_in_a_project_that_holds_it(self):
        said = self.config.qk_said
        self.assertEqual(said("disk --delete", self.with_copy), "python3 scripts/qk disk --delete")
        self.assertEqual(said("disk --delete", self.without), "qk disk --delete")
        self.assertEqual(said(["admissions", "remove", "OWNER", "--why", "TEXT"], self.without),
                         "qk admissions remove OWNER --why TEXT")

    def test_a_command_run_from_anywhere_names_the_project(self):
        said = self.config.qk_said
        self.assertEqual(said(["queue", "wait", "qk1"], self.with_copy, anywhere=True),
                         f"python3 {self.with_copy}/scripts/qk queue wait qk1")
        self.assertEqual(said(["queue", "wait", "qk1"], self.without, anywhere=True),
                         f"qk --project {self.without} queue wait qk1")
        spaced = self.base / "a project"
        (spaced / ".git").mkdir(parents=True)
        self.assertEqual(said("land", spaced, anywhere=True), f"qk --project '{spaced}' land")

    def test_by_default_it_reads_the_project_the_command_runs_in(self):
        for project, expected in ((self.with_copy, "python3 scripts/qk disk"), (self.without, "qk disk")):
            for folder in (project, project / "deep/down"):
                with self.subTest(folder=folder):
                    self.in_directory(folder)
                    self.assertEqual(self.config.qk_said("disk"), expected)
        outside = self.base / "outside"
        outside.mkdir()
        self.in_directory(outside)
        self.assertEqual(self.config.qk_words(), ["qk"])

    def test_the_disk_and_admission_advice_follow_the_project(self):
        from unittest.mock import patch
        from queuekeeper import disk, lane_wait
        with patch.dict(os.environ, {"QUEUEKEEPER_START_FREE_GB": "30"}), \
                patch.object(disk, "volume_usage", return_value=(100 * disk.GIB, 99 * disk.GIB, disk.GIB)):
            for project, launcher in ((self.with_copy, "python3 scripts/qk"), (self.without, "qk")):
                with self.subTest(project=project.name):
                    short = disk.short_of_space(disk.START_FREE, (str(project),), root=project)
                    self.assertIn(f"`{launcher} disk --delete` removes what nobody owns, and `{launcher} disk` lists",
                                  short)
                    if launcher == "qk": self.assertNotIn("scripts/qk", short)
        for project, launcher in ((self.with_copy, "python3 scripts/qk"), (self.without, "qk")):
            with self.subTest(project=project.name):
                waits = project / ".build/queuekeeper" / lane_wait.WAITS
                waits.mkdir(parents=True)
                (waits / "gate.json").write_text(json.dumps({
                    "schemaVersion": 1, "kind": lane_wait.ADMISSION, "reason": "all 3 guaranteed slots are busy",
                    "mac": "this Mac", "offer": None, "stranded": False, "pid": os.getpid(), "admitted": False,
                    "since": 0, "written": 0}))
                [warning] = lane_wait.admission_warnings(project, at=10)
                self.assertIn(f"`{launcher} admissions now` shows who holds", warning)


class SecondProjectTests(unittest.TestCase):
    """A project's scripts/qk names its own project first, so a --project the user gives it comes second. That one
    names the project to run on, for a plugin's command as for a core one: a plugin's command ran on neither and
    failed with an unrelated parse error, "unrecognized arguments" (vocaro/queuekeeper#345)."""
    KIT = textwrap.dedent("""
        import json
        from queuekeeper import plugins
        def where(argv, project):
            print(json.dumps({"kit": NAME, "project": project, "argv": argv}))
            return 0
        def parsed(root, args, config):
            print(json.dumps({"kit": NAME, "project": str(root), "config": config and config["name"],
                              "word": args.word}))
            return 0
        def register():
            plugins.COMMANDS["kit-where"] = ("say which project's kit ran, and on which project", where)
            plugins.PARSED_COMMANDS["kit-parsed"] = ("the same, parsed", lambda parser: parser.add_argument("word"),
                                                     parsed, True)
    """)

    def setUp(self):
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.launching, self.named = self.base / "launching", self.base / "named"
        for project in (self.launching, self.named):
            (project / "src/python/kit").mkdir(parents=True)
            (project / "src/python/kit/__init__.py").write_text("")
            (project / "src/python/kit/hooks.py").write_text(f"NAME = {project.name!r}\n{self.KIT}")
            (project / "queuekeeper.json").write_text(json.dumps(
                {"schemaVersion": 1, "name": project.name, "lanes": {},
                 "plugins": [{"module": "kit.hooks", "path": "src/python"}]}))
        (self.launching / "scripts").mkdir()
        shutil.copy(ROOT / "scripts/qk", self.launching / "scripts/qk")

    def qk(self, *arguments):
        result = subprocess.run([sys.executable, str(self.launching / "scripts/qk"), *arguments],
                                env=host_isolation.reduced(os.environ, QUEUEKEEPER_ROOT=str(ROOT)),
                                cwd=self.launching, capture_output=True, text=True, timeout=stretched(60))
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_a_plugins_own_command_runs_the_named_projects_plugin_on_it(self):
        for given in (["--project", str(self.named)], [f"--project={self.named}"]):
            with self.subTest(given=given):
                self.assertEqual(self.qk(*given, "kit-where", "status"),
                                 {"kit": "named", "project": str(self.named), "argv": ["status"]})

    def test_a_command_the_core_parses_runs_on_the_named_project(self):
        self.assertEqual(self.qk("--project", str(self.named), "kit-parsed", "status"),
                         {"kit": "named", "project": str(self.named), "config": "named", "word": "status"})

    def test_without_a_second_project_the_launchers_own_is_run_on(self):
        # Negative control: the launcher's project, its plugin and its configuration.
        self.assertEqual(self.qk("kit-where", "status"),
                         {"kit": "launching", "project": str(self.launching), "argv": ["status"]})
        self.assertEqual(self.qk("kit-parsed", "status"), {"kit": "launching", "project": str(self.launching),
                                                           "config": "launching", "word": "status"})


if __name__ == "__main__":
    unittest.main()
