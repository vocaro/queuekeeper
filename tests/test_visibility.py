"""The switch for the visibility features (queuekeeper.visibility), against a feature made for the test."""
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/python"))
import host_isolation  # noqa: F401 -- first, as every test module
from queuekeeper import cli, extensions, visibility

FEATURE = '''"""Says hello, for a test."""
from queuekeeper import extensions
REGISTERED = []


def register():
    REGISTERED.append(1)
    extensions.COMMANDS["hello"] = ("say hello", lambda argv, project: print("hello") or 0)
'''
BROKEN = '"""Cannot be imported."""\nraise RuntimeError("broken on purpose")\n'


class Switch(unittest.TestCase):
    """A folder of two features, one of them broken, stands in for the package's own, and a settings file of the
    test's for the owner's."""

    def setUp(self):
        self.base = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.base, True)
        (self.base / "qk_hello.py").write_text(FEATURE)
        (self.base / "qk_broken.py").write_text(BROKEN)
        sys.path.insert(0, str(self.base))
        self.addCleanup(sys.path.remove, str(self.base))
        for name in ("qk_hello", "qk_broken"): self.addCleanup(sys.modules.pop, name, None)
        for what, value in ((visibility, {"available": lambda: {"hello": "qk_hello", "broken": "qk_broken"},
                                          "settings_path": lambda: self.base / "settings" / visibility.FILE}),):
            for name, replacement in value.items():
                patcher = patch.object(what, name, replacement)
                patcher.start()
                self.addCleanup(patcher.stop)
        for kept in (extensions.COMMANDS, extensions.LOADED):
            before = kept.copy()
            self.addCleanup(lambda kept=kept, before=before: (kept.clear(), kept.update(before) if isinstance(kept, dict)
                                                              else kept.extend(before)))
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop(visibility.SWITCH, None)

    def command(self, *words):
        printed, errors = io.StringIO(), io.StringIO()
        with patch("sys.stdout", printed), patch("sys.stderr", errors):
            status = cli.main(["visibility", *words])
        return status, printed.getvalue(), errors.getvalue()

    def test_every_feature_is_off_until_it_is_turned_on(self):
        self.assertEqual(visibility.enabled(), [])
        status, printed, _ = self.command("status")
        self.assertEqual(status, 0)
        self.assertIn("off  hello  Says hello, for a test.", printed)
        self.assertIn("off  broken  Cannot be imported.", printed)
        self.assertNotIn("qk_hello", sys.modules)  # described from its source, not by importing it

    def test_enable_and_disable_change_one_feature_and_keep_the_rest_of_the_file(self):
        path = visibility.settings_path()
        path.parent.mkdir()
        path.write_text(json.dumps({"notify": {"channels": ["banner"]}, "features": {"other": True}}))
        self.assertEqual(self.command("enable", "hello")[:2], (0, "hello is on\n"))
        self.assertEqual(visibility.enabled(), ["hello"])
        self.assertEqual(json.loads(path.read_text()),
                         {"notify": {"channels": ["banner"]}, "features": {"other": True, "hello": True}})
        self.assertEqual(json.loads(self.command("status", "--json")[1])["features"]["hello"]["enabled"], True)
        self.assertEqual(self.command("disable", "hello")[:2], (0, "hello is off\n"))
        self.assertEqual(visibility.enabled(), [])

    def test_a_feature_that_is_not_installed_is_refused_by_name(self):
        status, _, errors = self.command("enable", "nope")
        self.assertEqual(status, 2)
        self.assertIn("no visibility feature is named nope: broken, hello", errors)
        self.assertFalse(visibility.settings_path().exists())

    def test_a_feature_that_is_on_registers_and_adds_its_command(self):
        visibility.switch("hello", True)
        printed = io.StringIO()
        with patch("sys.stdout", printed): status = cli.main(["hello"])
        self.assertEqual((status, printed.getvalue()), (0, "hello\n"))
        self.assertEqual(sys.modules["qk_hello"].REGISTERED, [1])

    def test_a_feature_that_is_off_is_never_imported(self):
        self.command("status")
        visibility.load()
        self.assertNotIn("qk_hello", sys.modules)
        self.assertNotIn("hello", extensions.COMMANDS)

    def test_the_environment_switch_turns_every_feature_off(self):
        visibility.switch("hello", True)
        os.environ[visibility.SWITCH] = "off"
        self.assertEqual(visibility.enabled(), [])
        visibility.load()
        self.assertNotIn("qk_hello", sys.modules)
        self.assertIn("every feature is off", self.command("status")[1])

    def test_a_broken_feature_is_said_and_left_out_and_the_command_still_runs(self):
        visibility.switch("broken", True)
        visibility.switch("hello", True)
        status, printed, errors = self.command("status")
        self.assertEqual(status, 0)
        self.assertIn("the visibility feature broken did not load, and is left out: broken on purpose", errors)
        self.assertIn("`qk visibility disable broken`", errors)
        self.assertIn("hello", extensions.COMMANDS)

    def test_settings_that_cannot_be_read_turn_nothing_on(self):
        path = visibility.settings_path()
        path.parent.mkdir()
        for text in ("not json", "[]", '{"features": []}', '{"features": {"hello": "yes"}}'):
            with self.subTest(text=text):
                path.write_text(text)
                self.assertEqual(visibility.enabled(), [])
        path.write_text("not json")
        visibility.switch("hello", True)
        self.assertEqual(visibility.enabled(), ["hello"])


if __name__ == "__main__":
    unittest.main()
