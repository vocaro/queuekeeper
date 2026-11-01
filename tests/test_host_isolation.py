import ast
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/python"))
from vocaro_engineering import offload_host, resources
import host_isolation

TESTS = Path(__file__).resolve().parent

# Admits a heavy job with whatever namespace the environment names, after importing host_isolation
# when the first argument says so, and prints the guard and slot directory the admission used.
PROBE = """
import json, os, sys
sys.path.insert(0, sys.argv[1])
if sys.argv[2] == "isolated": import host_isolation
from vocaro_engineering import resources
with resources.Admission("forgot to redirect", timeout=0) as admission:
    print(json.dumps({"guard": str(admission.guard), "slots": str(admission.directory),
                      "held": admission.guard.exists()}))
"""


def unisolated(paths):
    """The test modules among paths that do not import host_isolation at their top level."""
    found = []
    for path in paths:
        tree = ast.parse(Path(path).read_text(), str(path))
        if not any(isinstance(node, ast.Import) and any(alias.name == "host_isolation" for alias in node.names)
                   for node in tree.body):
            found.append(Path(path).name)
    return found


class HostIsolationTests(unittest.TestCase):
    def test_every_test_module_imports_host_isolation(self):
        modules = sorted(TESTS.glob("test*.py"))
        self.assertGreater(len(modules), 20)
        self.assertEqual(unisolated(modules), [])

    def test_the_check_finds_a_module_that_does_not_import_host_isolation(self):
        with tempfile.TemporaryDirectory() as directory:
            forgot, isolated, nested = (Path(directory) / name for name in ("test_forgot.py", "test_isolated.py",
                                                                            "test_nested.py"))
            forgot.write_text("import unittest\nfrom vocaro_engineering import cli\n")
            isolated.write_text("import unittest\nimport host_isolation\n")
            nested.write_text("def setUpModule():\n    import host_isolation\n")
            self.assertEqual(unisolated([forgot, isolated, nested]), ["test_forgot.py", "test_nested.py"])

    def test_this_process_resolves_admission_to_its_own_namespace(self):
        root = host_isolation.ROOT
        self.assertTrue(resources.guard_path(os.environ).is_relative_to(root))
        self.assertTrue(resources.slot_directory().is_relative_to(root))
        self.assertTrue(resources.log_path().is_relative_to(root))
        # A test that clears the environment still reaches no host guard.
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(resources.guard_path(os.environ).is_relative_to(root))
            self.assertTrue(Path(offload_host.GATE_LOCK).is_relative_to(root))

    def test_an_admission_that_forgets_to_redirect_stays_out_of_the_namespace_it_inherited(self):
        # The inherited namespace stands in for the host's: a test must never admit into the real one.
        with tempfile.TemporaryDirectory() as directory:
            host = Path(directory).resolve()
            environment = {**os.environ, "HEAVY_SLOT_DIR": str(host / "slots"), "SW_GATE_LOCK_FILE": str(host / "gate"),
                           "HEAVY_SLOTS": "1", "HEAVY_POLL_SECONDS": "0.01"}
            environment.pop(resources.TOKEN, None)
            source = str(Path(resources.__file__).resolve().parents[1])
            outcomes = {}
            for mode in ("isolated", "control"):
                result = subprocess.run([sys.executable, "-c", PROBE, source, mode], env=environment, cwd=TESTS,
                                        capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stderr)
                outcomes[mode] = json.loads(result.stdout.splitlines()[-1])
                self.assertTrue(outcomes[mode]["held"])
                if mode == "isolated":
                    # It wrote nothing at all to the namespace it inherited.
                    self.assertEqual(list(host.iterdir()), [])
            # The control, without host_isolation, admits into the inherited namespace: the probe sees a leak.
            self.assertEqual(outcomes["control"]["guard"], str(host / "gate.warm-push-guard"))
            self.assertEqual(outcomes["control"]["slots"], str(host / "slots"))
            # With it, the admission went elsewhere, and its namespace went with the process.
            for key in ("guard", "slots"):
                self.assertFalse(Path(outcomes["isolated"][key]).is_relative_to(host))
                self.assertIn("vocaro-test-host-", outcomes["isolated"][key])
                self.assertFalse(Path(outcomes["isolated"][key]).exists())


if __name__ == "__main__":
    unittest.main()
