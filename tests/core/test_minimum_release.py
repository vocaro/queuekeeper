"""queuekeeper.json's queuekeeper.minimum, the oldest Queuekeeper release a project needs (vocaro/queuekeeper#351)."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src/python"))
import host_isolation  # noqa: F401 -- first, as every test module
from queuekeeper import cli, config, git_state

SCHEMA = config.TOOLKIT_ROOT / "schemas/queuekeeper.schema.json"


def project(minimum=None):
    return {"schemaVersion": 1, "name": "Example",
            **({"queuekeeper": {"minimum": minimum}} if minimum is not None else {})}


class SettingTests(unittest.TestCase):
    def test_minimum_takes_only_a_release_version_and_the_schema_says_the_same(self):
        self.assertEqual(config.parse(json.dumps(project("1.2.0")))["queuekeeper"], {"minimum": "1.2.0"})
        self.assertNotIn("queuekeeper", config.parse(json.dumps(project())))
        pattern = json.loads(SCHEMA.read_text())["properties"]["queuekeeper"]["properties"]["minimum"]["pattern"]
        for minimum in ["1.2", "v1.2.0", "1.2.0-beta", "01.2.0", 1, ""]:
            with self.subTest(minimum=minimum), self.assertRaisesRegex(ValueError, "queuekeeper.minimum must be"):
                config.parse(json.dumps(project(minimum)))
        for declared in [{"pin": "1.2.0"}, "1.2.0"]:
            with self.subTest(declared=declared), self.assertRaisesRegex(ValueError, "queuekeeper supports only"):
                config.parse(json.dumps({**project(), "queuekeeper": declared}))
        import re
        for version, valid in [("1.2.0", True), ("10.0.12", True), ("1.2", False), ("v1.2.0", False),
                               ("01.2.0", False)]:
            with self.subTest(version=version):
                self.assertEqual(bool(re.fullmatch(pattern, version)), valid)
                self.assertEqual(bool(config.RELEASE_VERSION.fullmatch(version)), valid)


class ShortfallTests(unittest.TestCase):
    def test_an_older_queuekeeper_is_named_with_how_to_update_it(self):
        said = config.release_shortfall(project("1.10.0"), "1.9.3", Path("/Q"))
        self.assertIn("Example's queuekeeper.json asks for Queuekeeper 1.10.0 or newer", said)
        self.assertIn("the Queuekeeper at /Q is 1.9.3", said)
        self.assertIn("`git -C /Q fetch --tags` and then `git -C /Q checkout v1.10.0`", said)

    def test_a_queuekeeper_at_or_past_the_minimum_an_unknown_version_and_no_minimum_say_nothing(self):
        for declared, running in [("1.2.0", "1.2.0"), ("1.2.0", "1.10.0"), ("1.2.0", "2.0.0"),
                                  ("1.2.0", "unknown"), (None, "0.1.0")]:
            with self.subTest(minimum=declared, running=running):
                self.assertIsNone(config.release_shortfall(project(declared), running, Path("/Q")))


class CommandTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)

    def configure(self, minimum):
        (self.root / "queuekeeper.json").write_text(json.dumps(project(minimum)))

    def doctor(self, running, newer=None):
        with patch.object(cli, "__version__", running), \
                patch.object(cli.shutil, "which", return_value="/usr/bin/true"), \
                patch.object(cli, "source_identity", return_value={}), \
                patch.object(git_state, "newer_release", return_value=newer) as asked, \
                contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(cli.main(["--project", str(self.root), "doctor"]), 0)
        asked.assert_called_once_with(cli.TOOLKIT_ROOT)
        return json.loads(output.getvalue()), error.getvalue()

    def test_doctor_warns_of_the_shortfall_only_when_queuekeeper_is_older(self):
        self.configure("1.2.0")
        report, error = self.doctor("1.1.0")
        self.assertEqual([w for w in report["warnings"] if "queuekeeper.minimum" in w],
                         [config.release_shortfall(project("1.2.0"), "1.1.0")])
        self.assertNotIn("queuekeeper.minimum", error)  # given once, in the report
        for running in ["1.2.0", "1.3.0"]:
            with self.subTest(running=running):
                report, _ = self.doctor(running)
                self.assertFalse([w for w in report["warnings"] if "queuekeeper.minimum" in w], report["warnings"])

    def test_another_command_that_reads_the_configuration_says_it_on_standard_error(self):
        for running, said in [("1.1.0", True), ("1.2.0", False)]:
            with self.subTest(running=running):
                self.configure("1.2.0")
                with patch.object(cli, "__version__", running), contextlib.redirect_stdout(io.StringIO()), \
                        contextlib.redirect_stderr(io.StringIO()) as error:
                    self.assertEqual(cli.main(["--project", str(self.root), "docs", "check"]), 0)
                self.assertEqual("asks for Queuekeeper 1.2.0 or newer" in error.getvalue(), said, error.getvalue())

    def test_doctor_gives_a_newer_release_as_a_note_and_not_a_warning(self):
        self.configure(None)
        note = "Queuekeeper at /Q is release v1.2.0, and v1.3.0 is out: update ..."
        report, _ = self.doctor("1.2.0", newer=note)
        self.assertIn(note, report["notes"])
        self.assertNotIn(note, report["warnings"])
        report, _ = self.doctor("1.2.0")
        self.assertFalse([n for n in report["notes"] if "is out" in n], report["notes"])


if __name__ == "__main__":
    unittest.main()
