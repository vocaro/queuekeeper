"""The project configurations agents copy from the setup instructions must be usable."""
from pathlib import Path
import re
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/python"))
import host_isolation  # noqa: F401 -- first, as every test module
from queuekeeper import config


class SetupExamples(unittest.TestCase):
    def test_setup_configurations_are_accepted_without_implicit_platform_plugins(self):
        root = Path(__file__).resolve().parents[1]
        for name in ("README.md", "doc/getting_started.md"):
            with self.subTest(document=name):
                examples = re.findall(r"^```json\s*\n(.*?)^```", (root / name).read_text(), re.M | re.S)
                self.assertTrue(examples, f"{name} has no JSON setup example")
                for example in examples:
                    project = config.parse(example)
                    self.assertIn("gate", project["lanes"])
                    self.assertEqual(project["extensions"], [])


if __name__ == "__main__":
    unittest.main()
