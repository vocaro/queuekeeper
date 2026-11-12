import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/python"))
from queuekeeper import offload_host, resources, shims
from queuekeeper.parallel import unisolated
import host_isolation
from host_isolation import stretched

TESTS = Path(__file__).resolve().parent
# A test marking a file executable itself, not through host_isolation.executable.
MARKS_EXECUTABLE = re.compile(r"\.chmod\(\s*0o7|\.chmod\(.*S_IEXEC|os\.chmod\(")

# Admits a heavy job with whatever namespace the environment names, after importing host_isolation
# when the first argument says so, and prints the guard and slot directory the admission used.
PROBE = """
import json, os, sys
sys.path.insert(0, sys.argv[1])
if sys.argv[2] == "isolated": import host_isolation
from queuekeeper import resources
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
            forgot.write_text("import unittest\nfrom queuekeeper import cli\n")
            isolated.write_text("import unittest\nimport host_isolation\n")
            nested.write_text("def setUpModule():\n    import host_isolation\n")
            self.assertEqual(unisolated([forgot, isolated, nested]), ["test_forgot.py", "test_nested.py"])

    def test_no_test_module_marks_a_file_executable_itself(self):
        # A stand-in on PATH goes through host_isolation.executable, which checks that it runs (#214). This
        # module's own negative control below makes one that cannot run, so it is not scanned.
        found = [f"{module.name}:{number}" for module in sorted(TESTS.glob("test*.py")) if module.name != Path(__file__).name
                 for number, line in enumerate(module.read_text().splitlines(), 1) if MARKS_EXECUTABLE.search(line)]
        self.assertEqual(found, [])
        for line in ("tool.chmod(0o755)", "path.chmod(path.stat().st_mode | stat.S_IEXEC)", "os.chmod(tool, 0o755)"):
            self.assertTrue(MARKS_EXECUTABLE.search(line), line)
        # A read-only directory is not a stand-in.
        self.assertIsNone(MARKS_EXECUTABLE.search("self.root.chmod(0o500)"))

    def test_a_stand_in_that_would_not_run_is_refused_before_the_real_tool_runs_in_its_place(self):
        with tempfile.TemporaryDirectory() as directory:
            tools = Path(directory)
            # The negative control first: execvp runs the next match on PATH when a stand-in cannot run.
            indented = tools / "true"
            indented.write_text("    #!/bin/sh\nexit 3\n")
            indented.chmod(0o755)
            ran = subprocess.run(["true"], env={"PATH": f"{tools}:/usr/bin:/bin"})
            self.assertEqual(ran.returncode, 0, "the real true ran in the unrunnable stand-in's place")
            refused = {
                "indented": "    #!/bin/sh\nexit 0\n",
                "no-interpreter": "#!/no/such/interpreter\n",
                "missing-on-path": "#!/usr/bin/env no-such-interpreter-214\n",
                "python": f"#!{sys.executable}\nprint('a\n')\n",
                "env-python": "#!/usr/bin/env python3\nif True:\nprint(1)\n",
                "shell": "#!/bin/sh\nif then\n",
            }
            for name, text in refused.items():
                with self.subTest(name=name), self.assertRaises(AssertionError):
                    host_isolation.executable(tools / name, text)
                self.assertFalse(os.access(tools / name, os.X_OK), name)
            for name, text in {"sh": "#!/bin/sh\nexit 4\n", "python": f"#!{sys.executable}\nraise SystemExit(5)\n"}.items():
                with self.subTest(name=name):
                    made = host_isolation.executable(tools / f"good-{name}", text)
                    self.assertEqual(subprocess.run([str(made)]).returncode, {"sh": 4, "python": 5}[name])
            # Written beforehand, it is checked the same way.
            (tools / "late").write_text("#!/bin/sh\nexit 6\n")
            self.assertEqual(subprocess.run([str(host_isolation.executable(tools / "late"))]).returncode, 6)

    def test_this_process_resolves_admission_to_its_own_namespace(self):
        root = host_isolation.ROOT
        self.assertTrue(resources.guard_path(os.environ).is_relative_to(root))
        self.assertTrue(resources.slot_directory().is_relative_to(root))
        self.assertTrue(resources.log_path().is_relative_to(root))
        # A test that clears the environment still reaches no host guard.
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(resources.guard_path(os.environ).is_relative_to(root))
            self.assertTrue(Path(offload_host.GATE_LOCK).is_relative_to(root))

    def test_a_suite_run_inside_an_admission_takes_its_tests_out_of_it_and_off_the_second_mac(self):
        # The toolkit's gate is admitted (#200), so its suite inherits the gate's token; a test must not reuse it, nor
        # send a lane it runs to the real second Mac, nor report that lane's waits into the gate's own record (#335).
        token = '{"schemaVersion": 1, "locks": []}'
        source = str(Path(resources.__file__).resolve().parents[1])
        record = host_isolation.remote.lane_wait.VARIABLE
        environment = {**os.environ, resources.TOKEN: token, "QUEUEKEEPER_OFFLOAD_HOST": "build-host", record: "gate.json"}
        seen = {}
        for mode, imports in (("isolated", "import host_isolation; "), ("control", "")):
            code = (f"import os, sys; sys.path.insert(0, sys.argv[1]); {imports}"
                    "print(os.environ.get(sys.argv[2]), os.environ.get('QUEUEKEEPER_OFFLOAD_HOST'), os.environ.get(sys.argv[3]))")
            result = subprocess.run([sys.executable, "-c", code, source, resources.TOKEN, record], env=environment, cwd=TESTS,
                                    capture_output=True, text=True, timeout=stretched(60))
            self.assertEqual(result.returncode, 0, result.stderr)
            seen[mode] = result.stdout.strip()
        self.assertEqual(seen, {"isolated": "None none None", "control": f"{token} build-host gate.json"})

    def test_a_suite_run_inside_a_lane_drops_what_that_lane_set_for_its_own_command(self):
        # On 2026-11-09 fixtures wrote into the real gate's results/, and its summary named a fixture's test as failed (#289).
        cli = host_isolation.cli
        names = (cli.parallel.RESULTS_VARIABLE, shims.ROOT_VARIABLE, cli.SELF_LANE, host_isolation.app_registrations.RUN,
                 host_isolation.devices.WINDOW, host_isolation.devices.OWNER, "QUEUEKEEPER_ROOT", "QUEUEKEEPER_LANE_PROGRESS_RUN")
        source = str(Path(resources.__file__).resolve().parents[1])
        for name in names:
            for mode, imports, expected in (("isolated", "import host_isolation; ", "None"), ("control", "", "outer")):
                with self.subTest(name=name, mode=mode):
                    code = f"import os, sys; sys.path.insert(0, sys.argv[1]); {imports}print(os.environ.get(sys.argv[2]))"
                    result = subprocess.run([sys.executable, "-c", code, source, name], env={**os.environ, name: "outer"},
                                            cwd=TESTS, capture_output=True, text=True, timeout=stretched(60))
                    self.assertEqual((result.returncode, result.stdout.strip()), (0, expected), result.stderr)

    def test_an_admission_that_forgets_to_redirect_stays_out_of_the_namespace_it_inherited(self):
        # The inherited namespace stands in for the host's: a test must never admit into the real one.
        with tempfile.TemporaryDirectory() as directory:
            host = Path(directory).resolve()
            environment = {**os.environ, "HEAVY_SLOT_DIR": str(host / "slots"), "QUEUEKEEPER_GATE_LOCK_FILE": str(host / "gate"),
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
                self.assertIn("queuekeeper-test-host-", outcomes["isolated"][key])
                self.assertFalse(Path(outcomes["isolated"][key]).exists())


class SignalDispositionTests(unittest.TestCase):
    """default_signals, from a runner that ignores every stop signal, as a background job's shell or nohup can."""

    # Says it is ready, then waits for the test's go, which only an ignored stop reaches.
    CHILD = ("import os, sys, time\nfrom pathlib import Path\nhere = Path(sys.argv[1])\n(here / 'ready').touch()\n"
             "began = time.monotonic()\n"
             "while not (here / 'go').exists() and time.monotonic() - began < 120: time.sleep(0.01)")

    def stopped(self, number, **options):
        """The child's status after it is sent number and then told to go."""
        with tempfile.TemporaryDirectory() as directory:
            here = Path(directory)
            child = subprocess.Popen([sys.executable, "-I", "-c", self.CHILD, directory], **options)
            self.addCleanup(lambda: child.poll() is None and child.kill())
            deadline = time.monotonic() + stretched(30)
            while not (here / "ready").exists():
                self.assertIsNone(child.poll(), "the child ended before it was ready")
                self.assertLess(time.monotonic(), deadline, "the child was never ready")
                time.sleep(0.01)
            # An ignored signal is discarded when sent, so the go comes after it either way.
            child.send_signal(number)
            (here / "go").touch()
            return child.wait(stretched(30))

    def test_a_child_started_with_default_signals_is_stopped_though_its_runner_ignores_them(self):
        for number in host_isolation.STOP_SIGNALS:
            self.addCleanup(signal.signal, number, signal.signal(number, signal.SIG_IGN))
        for number in host_isolation.STOP_SIGNALS:
            with self.subTest(signal=number.name):
                self.assertEqual(self.stopped(number, preexec_fn=host_isolation.default_signals), -number)
                # The negative control: without it the child inherits the ignore and ends at the go.
                self.assertEqual(self.stopped(number), 0)


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
