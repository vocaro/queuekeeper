"""The project configurations agents copy from the setup instructions must be usable."""
from pathlib import Path
import re
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src/python"))
import host_isolation  # noqa: F401 -- first, as every test module
from queuekeeper import config

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_PLUGINS = {"doc/user_guide.md": [], "README.md": ["queuekeeper_apple"]}


class SetupExamples(unittest.TestCase):
    def test_setup_configurations_are_accepted_with_the_documented_plugins(self):
        # The user guide demonstrates the core; the README demonstrates iOS with the Apple plugin.
        # The json queuekeeper.json tag identifies whole configurations among fragments and --json results
        # that may also give schemaVersion (vocaro/queuekeeper#272).
        for name, plugins in EXAMPLE_PLUGINS.items():
            with self.subTest(name=name):
                examples = re.findall(r"^```json queuekeeper\.json[ \t]*\n(.*?)^```",
                                      (ROOT / name).read_text(), re.M | re.S)
                self.assertTrue(examples, f"{name} has no JSON setup example")
                for index, example in enumerate(examples, start=1):
                    with self.subTest(example=index):
                        project = config.parse(example)
                        # A project that integrates through pull requests runs no merge queue, so needs no gate.
                        if project.get("integration", "direct-main") == "direct-main":
                            self.assertIn("gate", project["lanes"])
                        self.assertEqual(project["plugins"], plugins)

    def test_a_docs_only_change_to_a_setup_document_runs_this_module(self):
        # A docs-only landing skips the gate, so each document this module reads must name its lane
        # (vocaro/queuekeeper#147).
        project = config.load(ROOT)
        for name in EXAMPLE_PLUGINS:
            with self.subTest(name=name):
                named = config.unread_named(project, [name])
                self.assertIn("setup-examples", named)
                self.assertIn(__name__.rpartition(".")[2], " ".join(project["lanes"]["setup-examples"]["command"]))


if __name__ == "__main__":
    unittest.main()
