"""The project configurations agents copy from the setup instructions must be usable."""
from pathlib import Path
import re
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src/python"))
import host_isolation  # noqa: F401 -- first, as every test module
from queuekeeper import config


class SetupExamples(unittest.TestCase):
    def test_setup_configurations_are_accepted_without_implicit_platform_plugins(self):
        # The user guide holds the configuration agents copy; the README sends them there and has none of its own.
        # Its whole configurations are the blocks fenced ```json queuekeeper.json; the fragments that show one
        # setting, and the --json results that give schemaVersion too, are not configurations of their own
        # (vocaro/queuekeeper#272).
        name = "doc/user_guide.md"
        root = Path(__file__).resolve().parents[2]
        examples = re.findall(r"^```json queuekeeper\.json[ \t]*\n(.*?)^```", (root / name).read_text(), re.M | re.S)
        self.assertTrue(examples, f"{name} has no JSON setup example")
        for example in examples:
            project = config.parse(example)
            self.assertIn("gate", project["lanes"])
            self.assertEqual(project["plugins"], [])

    def test_a_docs_only_change_to_the_setup_guide_runs_this_module(self):
        # A docs-only landing skips the gate, which runs this module; a change to the guide it reads must run it
        # anyway, or the next code entry's gate fails on a document it did not touch (vocaro/queuekeeper#147).
        project = config.load(Path(__file__).resolve().parents[2])
        named = config.unread_named(project, ["doc/user_guide.md"])
        self.assertTrue(named, "queue.unread names no lane for doc/user_guide.md")
        self.assertTrue(any(__name__.rpartition(".")[2] in " ".join(project["lanes"][lane]["command"]) for lane in named),
                        f"none of {sorted(named)} runs {__name__}")


if __name__ == "__main__":
    unittest.main()
