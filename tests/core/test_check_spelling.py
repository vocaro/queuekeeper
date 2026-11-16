"""scripts/check_spelling.py: Queuekeeper's own spelling check, the preflight lane its queuekeeper.json lists in place of
the built-in check a later release removes (vocaro/queuekeeper#69)."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src/python"))
import host_isolation
from queuekeeper import config

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/check_spelling.py"
_spec = importlib.util.spec_from_file_location("check_spelling", SCRIPT)
check_spelling = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_spelling)


def git(cwd, *arguments):
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    subprocess.run(["git", "-C", str(cwd), *arguments], check=True, capture_output=True, env=environment)


class Checking(unittest.TestCase):
    """The copy finds what the built-in check finds, and reports it as a preflight lane does."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.root)])
        git(self.root, "init", "-q")

    def check(self, files, project=None, commands=("run",)):
        for name, text in files.items():
            (self.root / name).write_text(text)
        git(self.root, "add", "-A")
        lines = []
        return check_spelling.check(self.root, project or {}, out=lines.append, commands=commands), lines

    def test_a_clean_tree_passes(self):
        status, lines = self.check({"a.md": "The color of the center.\n"})
        self.assertEqual(status, 0, lines)
        self.assertIn("check_spelling: PASS", lines[-1])

    def test_a_finding_is_a_path_and_a_line_and_exit_1(self):
        status, lines = self.check({"a.md": "fine\nThe colour.\n"})
        self.assertEqual(status, 1)
        self.assertIn("a.md:2: British spelling 'colour' -> color", lines)

    def test_the_command_s_old_name_is_a_finding(self):
        status, lines = self.check({"a.md": "Type queuekeeper run gate.\n"})
        self.assertEqual(status, 1)
        self.assertIn("a.md:1: the command's old name 'queuekeeper run' -> qk run", lines)

    def test_the_project_s_exemptions_hold(self):
        status, lines = self.check({"a.md": "The colour.\n"}, {"exemptPhrases": {"The colour": "a cited line"}})
        self.assertEqual(status, 0, lines)


class Repository(unittest.TestCase):
    def test_this_repository_passes_its_preflight_lane(self):
        # As the queue runs it: the lane's own command, from the checkout, with the subcommands qk's parser lists.
        lane = config.load(ROOT)["lanes"]["spelling"]
        ran = subprocess.run(lane["command"], capture_output=True, text=True, cwd=ROOT,
                             timeout=host_isolation.stretched(120))
        self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
        self.assertIn("the command's old name precedes none of", ran.stdout)

    def test_the_spelling_lane_is_this_project_s_preflight_lane(self):
        value = json.loads((ROOT / "queuekeeper.json").read_text())
        self.assertEqual(value["queue"]["preflight"], ["spelling"])
        self.assertEqual(value["lanes"]["spelling"]["command"], ["python3", "scripts/check_spelling.py"])
        self.assertNotIn("spelling", value, "the built-in check's exemptions now live in the script's PROJECT")
        self.assertIsNone(config.legacy_spelling_warning(config.load(ROOT)))

    def test_qk_s_subcommands_are_read_from_its_parser(self):
        names = check_spelling.subcommands(ROOT)
        self.assertIn("spelling", names)
        self.assertIn("preflight", names)


if __name__ == "__main__":
    unittest.main()
