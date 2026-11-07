import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/python"))
from vocaro_engineering import offload_host, resources
from vocaro_engineering.parallel import unisolated
import host_isolation
from host_isolation import stretched

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


# Tests that bound a hang with stretched bounds, at a load of at least four per core, as on a loaded Mac: one
# command that never ends, one wait for what never happens, and a positive control that ends at once.
HANGS = textwrap.dedent("""
    import os, subprocess, sys, time, unittest
    from unittest.mock import patch
    import host_isolation
    from host_isolation import stretched

    class Hangs(unittest.TestCase):
        def setUp(self):
            load = max(4 * (os.cpu_count() or 1), os.getloadavg()[0])
            loaded = patch.object(host_isolation.os, "getloadavg", return_value=(load,) * 3)
            loaded.start()
            self.addCleanup(loaded.stop)

        def test_a_command_that_hangs(self):
            print(f"bound {stretched(0.25)}", file=sys.stderr, flush=True)
            subprocess.run(["sleep", "3600"], timeout=stretched(0.25))

        def test_a_wait_that_never_ends(self):
            deadline = time.monotonic() + stretched(0.25)
            while time.monotonic() < deadline: time.sleep(0.01)
            self.fail("what it waited for never happened")

        def test_a_command_that_ends(self):
            subprocess.run(["true"], timeout=stretched(30), check=True)
""")


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
                                        capture_output=True, text=True, timeout=stretched(60))
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


class StretchedTests(unittest.TestCase):
    def test_a_bound_stretches_by_the_load_per_core_and_never_shrinks(self):
        cores = os.cpu_count() or 1
        for load, expected in ((0.0, 2), (cores / 2, 2), (cores, 2), (3 * cores, 6)):
            with self.subTest(load=load), patch.object(host_isolation.os, "getloadavg", return_value=(load,) * 3):
                self.assertAlmostEqual(stretched(2), expected)

    def test_a_stretched_bound_still_fails_a_test_that_really_hangs(self):
        """The negative control: the stretch is finite, so a test that really hangs fails at its bound instead of
        holding the suite, and a test that ends passes under the same load."""
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "test_hangs.py").write_text(HANGS)
            environment = {**os.environ, "PYTHONPATH": os.pathsep.join([str(TESTS), str(TESTS.parent / "src/python")])}
            result = subprocess.run([sys.executable, "-m", "unittest", "-v", "test_hangs"], cwd=directory,
                                    env=environment, capture_output=True, text=True, timeout=stretched(120))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("Ran 3 tests", result.stderr)
        self.assertIn("FAILED (failures=1, errors=1)", result.stderr)
        self.assertRegex(result.stderr, r"test_a_command_that_hangs \(test_hangs\.Hangs\.test_a_command_that_hangs\) "
                                         r"\.\.\. (bound \S+\n)?ERROR")
        self.assertIn("TimeoutExpired: Command '['sleep', '3600']' timed out after", result.stderr)
        self.assertRegex(result.stderr, r"test_a_wait_that_never_ends .* FAIL")
        self.assertIn("what it waited for never happened", result.stderr)
        self.assertRegex(result.stderr, r"test_a_command_that_ends .* ok")
        # The bound it failed at was stretched: four times 0.25 s at least.
        self.assertGreaterEqual(float(re.search(r"bound (\S+)\n", result.stderr).group(1)), 1)


if __name__ == "__main__":
    unittest.main()
