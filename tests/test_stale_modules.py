import contextlib
from io import StringIO
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest.mock import patch
import host_isolation
from host_isolation import stretched
from queuekeeper import cli, config, processes, resources
from queuekeeper.apple import stale_modules

SHIM = stale_modules.SHIMS / "xcodebuild"
EXPLICIT = ".build/derived/Build/Intermediates.noindex/SwiftExplicitPrecompiledModules"
# A stand-in build tool. It counts its runs in STANDIN_COUNT and, by STANDIN_MODE: stale fails on the module file
# STANDIN_MODULE while that file exists, as a build on a stale module cache does; always fails on it every time;
# other fails without it; mention passes with the message in its output; sleep writes its pid and waits.
STANDIN = textwrap.dedent("""\
    #!/usr/bin/env python3
    import os, sys, time
    from pathlib import Path
    count = Path(os.environ["STANDIN_COUNT"])
    count.write_text((count.read_text() if count.exists() else "") + " ".join(sys.argv[1:]) + "\\n")
    mode, module = os.environ["STANDIN_MODE"], os.environ.get("STANDIN_MODULE", "")
    error = ("<unknown>:0: error: file '/sources/include/resvg.h' has been modified since the module file "
             f"'{module}' was built: mtime changed")
    if mode == "sleep":
        import signal
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        Path(os.environ["STANDIN_PID"]).write_text(str(os.getpid()))
        time.sleep(120)
    if mode == "mention":
        print("warning: " + error); print("** BUILD SUCCEEDED **"); sys.exit(0)
    if mode == "other":
        print("error: cannot find 'something' in scope", file=sys.stderr); sys.exit(65)
    if mode == "always" or Path(module).exists():
        print(error, file=sys.stderr)
        print("SVGRenderer.swift:167:31: error: cannot find 'resvg_options_load_font_faces' in scope", file=sys.stderr)
        sys.exit(65)
    print("** BUILD SUCCEEDED **")
""")


class Fixture(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="stale-modules-")).resolve()
        self.addCleanup(__import__("shutil").rmtree, self.root, True)
        self.checkout = self.root / "checkout"
        self.tools = self.root / "tools"
        self.tools.mkdir()
        for tool in stale_modules.TOOLS: host_isolation.executable(self.tools / tool, STANDIN)
        self.count = self.root / "count"
        self.modules = self.checkout / EXPLICIT
        self.module = self.modules / "CResvg-1.pcm"
        self.module.parent.mkdir(parents=True)
        self.module.write_text("old")
        self.results = self.root / "results"

    def variables(self, mode, module=None, root=None):
        base = {**os.environ, "PATH": os.pathsep.join([str(self.tools), os.environ.get("PATH", os.defpath)]),
                "STANDIN_COUNT": str(self.count), "STANDIN_MODE": mode, "STANDIN_MODULE": str(module or self.module),
                stale_modules.RESULTS_VARIABLE: str(self.results)}
        return stale_modules.environment(base, self.checkout if root is None else root)

    def runs(self):
        return self.count.read_text().splitlines() if self.count.exists() else []


class Folders(unittest.TestCase):
    def test_the_named_module_cache_inside_the_root_only(self):
        root = Path(tempfile.mkdtemp()).resolve()
        explicit = root / ".build/d/Build/Intermediates.noindex/SwiftExplicitPrecompiledModules"
        swiftpm = root / ".build/epub-viewer/out/Intermediates.noindex/SwiftExplicitPrecompiledModules"
        implicit = root / ".build/d/ModuleCache.noindex"
        text = "\n".join(f"error: file 'h.h' has been modified since the module file '{path}' was built: mtime changed"
                         for path in (explicit / "CResvg-1.pcm", explicit / "CResvg-1.pcm", swiftpm / "CResvg-2.pcm",
                                      implicit / "3X/Foo-4.pcm"))
        self.assertEqual(stale_modules.folders(text, root), [explicit, swiftpm, implicit])
        # A relative module file is read from the build's directory.
        relative = "has been modified since the module file 'Build/Intermediates.noindex/" \
                   "SwiftExplicitPrecompiledModules/C.pcm' was built"
        self.assertEqual(stale_modules.folders(relative, root, root / ".build/d"), [explicit])
        # Controls: outside the root, in no module cache, the root itself, or no module file named.
        outside = Path(tempfile.mkdtemp()).resolve() / "Build/Intermediates.noindex/SwiftExplicitPrecompiledModules"
        for path in (outside / "C.pcm", root / ".build/d/Build/Products/C.pcm", root / "C.pcm"):
            self.assertEqual(stale_modules.folders(
                f"has been modified since the module file '{path}' was built", root), [], path)
        self.assertEqual(stale_modules.folders(
            f"has been modified since the module file '{root.parent / 'ModuleCache'}/C.pcm' was built",
            root.parent / "ModuleCache"), [])
        self.assertEqual(stale_modules.folders("has been modified since the module file", root), [])


class Environment(unittest.TestCase):
    def test_the_wrappers_come_first_once_with_the_root(self):
        once = stale_modules.environment({"PATH": "/usr/bin:/bin", "OTHER": "1"}, "/tmp")
        self.assertEqual(once["PATH"].split(os.pathsep), [str(stale_modules.SHIMS), "/usr/bin", "/bin"])
        self.assertEqual((once["OTHER"], once[stale_modules.ROOT_VARIABLE]), ("1", str(Path("/tmp").resolve())))
        # A nested lane's own root replaces its caller's, and the wrappers stay first, once.
        twice = stale_modules.environment({**once, "PATH": "/opt/bin:" + once["PATH"]}, "/var")
        self.assertEqual(twice["PATH"].split(os.pathsep), [str(stale_modules.SHIMS), "/opt/bin", "/usr/bin", "/bin"])
        self.assertEqual(twice[stale_modules.ROOT_VARIABLE], str(Path("/var").resolve()))

    def test_every_tool_has_a_wrapper_that_finds_the_real_one_past_any_wrapper(self):
        for tool in stale_modules.TOOLS:
            self.assertTrue(os.access(stale_modules.SHIMS / tool, os.X_OK), tool)
            self.assertTrue(stale_modules.shim(stale_modules.SHIMS / tool), tool)
        with tempfile.TemporaryDirectory() as other, tempfile.TemporaryDirectory() as tools:
            # Another toolkit's copy of the wrappers is passed over too.
            host_isolation.executable(Path(other) / "swift", (stale_modules.SHIMS / "swift").read_text())
            host_isolation.executable(Path(tools) / "swift", "#!/bin/sh\nexit 0\n")
            path = os.pathsep.join([str(stale_modules.SHIMS), other, tools])
            self.assertEqual(stale_modules.real("swift", path), str(Path(tools) / "swift"))
            self.assertIsNone(stale_modules.real("swift", os.pathsep.join([str(stale_modules.SHIMS), other])))


class Wrapper(Fixture):
    def wrapped(self, mode, tool="xcodebuild", **options):
        return subprocess.run([str(stale_modules.SHIMS / tool), "build", "-scheme", "S"], cwd=self.checkout,
                              env=self.variables(mode, **options), capture_output=True, text=True,
                              timeout=stretched(60))

    def test_a_build_on_a_stale_module_cache_removes_it_and_builds_once_more(self):
        for tool in stale_modules.TOOLS:
            with self.subTest(tool=tool):
                self.module.parent.mkdir(parents=True, exist_ok=True)
                self.module.write_text("old")
                self.count.unlink(missing_ok=True)
                finished = self.wrapped("stale", tool)
                self.assertEqual(finished.returncode, 0, finished.stderr)
                self.assertEqual(self.runs(), ["build -scheme S"] * 2)
                self.assertFalse(self.modules.exists())
                # The other folders of the build are kept.
                self.assertTrue(self.modules.parent.is_dir())
                # The first run's error and the rerun's success are both in the output, with what was removed.
                self.assertIn("cannot find 'resvg_options_load_font_faces'", finished.stderr)
                self.assertIn("** BUILD SUCCEEDED **", finished.stdout)
                self.assertIn(f"{stale_modules.NOTICE}: {tool} failed", finished.stderr)
                self.assertIn(f"removed {self.modules}", finished.stderr)
                self.assertIn("the rerun of", finished.stderr)
                self.assertIn(f"{tool}: removed {self.modules}; the rebuild ended with status 0",
                              (self.results / stale_modules.RECORD).read_text())

    def test_the_same_error_again_is_not_run_a_third_time(self):
        finished = self.wrapped("always")
        self.assertEqual(finished.returncode, 65)
        self.assertEqual(len(self.runs()), 2)
        self.assertIn("the same error again, so it is not run a third time", finished.stderr)
        self.assertIn("the rebuild ended with status 65", (self.results / stale_modules.RECORD).read_text())

    def test_controls_run_once_and_remove_nothing(self):
        cases = {"other": ({}, 65), "mention": ({}, 0),
                 "outside": ({"module": self.root / "elsewhere" / EXPLICIT / "C.pcm"}, 65),
                 "no root": ({"root": ""}, 65)}
        for name, (options, status) in cases.items():
            with self.subTest(name):
                self.count.unlink(missing_ok=True)
                mode = name if name in ("other", "mention") else "always"
                variables = self.variables(mode, **{key: value for key, value in options.items() if key != "root"})
                if "root" in options: variables.pop(stale_modules.ROOT_VARIABLE)
                finished = subprocess.run([str(SHIM), "build"], cwd=self.checkout, env=variables,
                                          capture_output=True, text=True, timeout=stretched(60))
                self.assertEqual(finished.returncode, status, finished.stderr)
                self.assertEqual(len(self.runs()), 1)
                self.assertTrue(self.module.exists())
                self.assertFalse((self.results / stale_modules.RECORD).exists())
                if name == "outside": self.assertIn("so nothing was removed and it is not run again", finished.stderr)

    def test_a_stop_reaches_the_build_once_and_its_status_says_so(self):
        # SIGTERM to the wrapper alone is passed on; SIGINT to the process group, as a terminal's or a supervised
        # stop's, reaches the build by itself, and the wrapper waits for it.
        for number, group in ((signal.SIGTERM, False), (signal.SIGINT, True)):
            with self.subTest(signal=number.name):
                pid = self.root / "pid"
                pid.unlink(missing_ok=True)
                variables = {**self.variables("sleep"), "STANDIN_PID": str(pid)}
                process = subprocess.Popen([str(SHIM), "build"], cwd=self.checkout, env=variables,
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                           start_new_session=True, preexec_fn=host_isolation.default_signals)
                self.addCleanup(lambda process=process: process.poll() is None and os.killpg(process.pid, 9))
                deadline = time.monotonic() + stretched(30)
                while not pid.exists() or not pid.read_text():
                    self.assertLess(time.monotonic(), deadline, "the stand-in never started")
                    time.sleep(0.05)
                if group: os.killpg(process.pid, number)
                else: process.send_signal(number)
                self.assertEqual(process.wait(timeout=stretched(30)), 128 + number)
                with self.assertRaises(ProcessLookupError):
                    deadline = time.monotonic() + stretched(10)
                    while time.monotonic() < deadline:
                        os.kill(int(pid.read_text()), 0)
                        time.sleep(0.05)


class Lane(Fixture):
    """queuekeeper run's lane, whose script sends the build's output to a log of its own, as BreezEPUB's test-ios.sh does."""

    def setUp(self):
        super().setUp()
        command = ["/bin/sh", "-c", "mkdir -p .build/validation && xcodebuild build-for-testing "
                   "> .build/validation/build.log 2>&1 && echo 'Ran 1 tests'"]
        (self.checkout / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Project", "lanes": {
            "settings-rendering": {"command": command, "resource": "heavy"}}}))
        variables = {"PATH": os.pathsep.join([str(self.tools), os.environ.get("PATH", os.defpath)]),
                     "STANDIN_COUNT": str(self.count), "STANDIN_MODE": "stale", "STANDIN_MODULE": str(self.module),
                     "HEAVY_POLL_SECONDS": "0.01"}
        for patcher in (patch.dict(os.environ, variables), patch.object(cli, "source_identity", return_value={}),
                        patch.object(cli, "refresh_policy", return_value=None)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_lane_in_a_reused_checkout_builds_past_a_stale_module_cache(self):
        with contextlib.redirect_stderr(StringIO()) as error, contextlib.redirect_stdout(StringIO()):
            status = cli.main(["--project", str(self.checkout), "run", "--here", "settings-rendering"])
        self.assertEqual(status, 0, error.getvalue())
        self.assertEqual(len(self.runs()), 2)
        self.assertFalse(self.modules.exists())
        log = (self.checkout / ".build/validation/build.log").read_text()
        self.assertIn(f"{stale_modules.NOTICE}: xcodebuild failed", log)
        self.assertIn("** BUILD SUCCEEDED **", log)
        # The record comes back with the lane's results.
        results = self.checkout / ".build/queuekeeper/settings-rendering/results"
        self.assertIn("the rebuild ended with status 0", (results / stale_modules.RECORD).read_text())


class CompileFirst(Fixture):
    """A lane's "compileFirst" step, with the stand-in xcodebuild: one admission, the compile before the tests."""

    # Each step writes the admission it holds to a file of its own; the tests' step prints its count.
    COMPILE = ["/bin/sh", "-c", f'echo "${resources.TOKEN}" >> compile.tokens; '
               "exec xcodebuild build-for-testing -derivedDataPath .build/native-tests"]
    TESTS = ["/bin/sh", "-c", f'echo "${resources.TOKEN}" >> tests.tokens; echo "Ran 2 tests"']

    def setUp(self):
        super().setUp()
        self.configured({"tests": self.lane()})
        variables = {"PATH": os.pathsep.join([str(self.tools), os.environ.get("PATH", os.defpath)]),
                     "STANDIN_COUNT": str(self.count), "STANDIN_MODE": "clean", "STANDIN_MODULE": str(self.root / "absent"),
                     "HEAVY_POLL_SECONDS": "0.01"}
        for patcher in (patch.dict(os.environ, variables), patch.object(cli, "source_identity", return_value={}),
                        patch.object(cli, "refresh_policy", return_value=None)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def lane(self, **more):
        return {"command": self.TESTS, "compileFirst": self.COMPILE, "resource": "heavy", **more}

    def configured(self, lanes):
        (self.checkout / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Project", "lanes": lanes}))

    def run_lane(self, name="tests", *arguments):
        with contextlib.redirect_stderr(StringIO()) as error, contextlib.redirect_stdout(StringIO()):
            status = cli.main(["--project", str(self.checkout), "run", "--here", name, *arguments])
        return status, error.getvalue()

    def receipt(self, name="tests"):
        return json.loads((self.checkout / ".build/queuekeeper" / name / "source.json").read_text())

    def log(self, file, name="tests"):
        return (self.checkout / ".build/queuekeeper" / name / file).read_text()

    def tokens(self, kind):
        path = self.checkout / f"{kind}.tokens"
        return path.read_text().splitlines() if path.exists() else []

    def test_a_clean_compile_then_the_tests_run_in_one_admission(self):
        with patch.object(cli, "admitted_here", wraps=cli.admitted_here) as admitted:
            status, error = self.run_lane()
        self.assertEqual(status, 0, error)
        self.assertEqual(admitted.call_count, 1)
        compiled, tested = self.tokens("compile"), self.tokens("tests")
        self.assertEqual((len(compiled), len(tested)), (1, 1))
        self.assertTrue(json.loads(compiled[0])["locks"], "the compile step ran outside any admission")
        self.assertEqual(compiled, tested)
        self.assertEqual(self.runs(), ["build-for-testing -derivedDataPath .build/native-tests"])
        receipt = self.receipt()
        self.assertEqual((receipt["outcome"], receipt["exitStatus"]), ("passed", 0))
        self.assertEqual((receipt["compile"]["outcome"], receipt["compile"]["exitStatus"]), ("passed", 0))
        self.assertIn("** BUILD SUCCEEDED **", self.log("compile.log"))
        self.assertEqual(self.log("run.log"), "Ran 2 tests\n")

    def test_a_compile_error_stops_the_run_before_any_test_starts(self):
        # Words like a test total in the compiler's output are no test count.
        self.configured({"tests": self.lane(minimumTests=2, compileFirst=[
            "/bin/sh", "-c", 'echo "Ran 9 tests"; exec xcodebuild build-for-testing'])})
        with patch.dict(os.environ, {"STANDIN_MODE": "other"}):
            status, error = self.run_lane()
        self.assertEqual(status, 65, error)
        self.assertEqual(self.tokens("tests"), [])
        self.assertEqual(len(self.runs()), 1)
        receipt = self.receipt()
        self.assertEqual(receipt["outcome"], "build-failed")
        self.assertEqual((receipt["compile"]["outcome"], receipt["compile"]["exitStatus"]), ("failed", 65))
        self.assertNotIn("unmeasured", receipt)
        self.assertFalse([key for key in {**receipt, **receipt["compile"]} if "tests" in key.lower()], receipt)
        self.assertIn("the build failed: compileFirst exited 65, and no test ran", error)
        self.assertIn("cannot find 'something' in scope", self.log("run.log"))
        self.assertFalse((self.checkout / ".build/queuekeeper/tests/compile.log").exists())

    def test_a_compile_that_exits_the_unmeasured_status_is_a_failed_build(self):
        self.configured({"tests": self.lane(compileFirst=["/bin/sh", "-c", "exit 3"])})
        status, error = self.run_lane()
        self.assertEqual(status, 1, error)
        self.assertEqual((self.receipt()["outcome"], self.receipt()["compile"]["exitStatus"]), ("build-failed", 3))
        self.assertEqual(self.tokens("tests"), [])

    def test_a_compile_that_reaches_the_timeout_is_unmeasured_not_a_failed_build(self):
        self.configured({"tests": self.lane(timeout=1, compileFirst=["/bin/sh", "-c", "exec sleep 120"])})
        status, error = self.run_lane()
        self.assertEqual(status, 3, error)
        self.assertEqual(self.receipt()["outcome"], "unmeasured")
        self.assertEqual(self.tokens("tests"), [])

    def test_the_tests_are_counted_from_their_own_output_alone(self):
        self.configured({"tests": self.lane(minimumTests=5, compileFirst=[
            "/bin/sh", "-c", 'echo "Ran 9 tests"'])})
        status, error = self.run_lane()
        self.assertEqual(status, 2, error)
        self.assertIn("Ran 9 tests", self.log("compile.log"))
        self.assertEqual(self.receipt()["outcome"], "failed")

    def test_a_stale_module_cache_in_the_compile_step_builds_once_more(self):
        with patch.dict(os.environ, {"STANDIN_MODE": "stale", "STANDIN_MODULE": str(self.module)}):
            status, error = self.run_lane()
        self.assertEqual(status, 0, error)
        self.assertEqual(len(self.runs()), 2)
        self.assertFalse(self.modules.exists())
        self.assertEqual(len(self.tokens("tests")), 1)
        self.assertIn(f"{stale_modules.NOTICE}: xcodebuild failed", self.log("compile.log"))
        recorded = self.checkout / ".build/queuekeeper/tests/results" / stale_modules.RECORD
        self.assertIn("the rebuild ended with status 0", recorded.read_text())

    def test_the_compile_step_and_the_tests_share_the_lanes_timeout(self):
        self.configured({"tests": self.lane(timeout=500)})
        with patch.object(processes, "run", wraps=processes.run) as ran:
            status, error = self.run_lane()
        self.assertEqual(status, 0, error)
        limits = [call.kwargs["timeout"] for call in ran.call_args_list]
        self.assertEqual(len(limits), 2)
        self.assertEqual(limits[0], 500)
        self.assertLess(limits[1], 500)
        self.assertGreaterEqual(limits[1], 1)

    def test_a_filter_goes_to_the_tests_alone(self):
        self.configured({"tests": self.lane(command=["/bin/sh", "-c", 'echo "$1" > filter; echo "Ran 1 tests"', "sh"])})
        status, error = self.run_lane("tests", "--", "-only-testing:One")
        self.assertEqual(status, 0, error)
        self.assertEqual((self.checkout / "filter").read_text(), "-only-testing:One\n")
        self.assertEqual(self.runs(), ["build-for-testing -derivedDataPath .build/native-tests"])

    def test_a_lane_without_the_step_runs_only_its_command(self):
        self.configured({"tests": {"command": self.TESTS, "resource": "heavy"}})
        status, error = self.run_lane()
        self.assertEqual(status, 0, error)
        self.assertEqual(self.runs(), [])
        self.assertEqual(len(self.tokens("tests")), 1)
        self.assertNotIn("compile", self.receipt())
        self.assertFalse((self.checkout / ".build/queuekeeper/tests/compile.log").exists())

    def test_a_failing_lane_without_the_step_stays_failed_not_build_failed(self):
        self.configured({"tests": {"command": ["/bin/sh", "-c", "exit 1"], "resource": "heavy"}})
        status, error = self.run_lane()
        self.assertEqual(status, 1, error)
        self.assertEqual(self.receipt()["outcome"], "failed")

    def test_a_group_runs_its_lanes_commands_without_the_step(self):
        self.configured({"suite": self.lane(), "gate": {"parallel": ["suite"]}})
        status, error = self.run_lane("gate")
        self.assertEqual(status, 0, error)
        self.assertEqual(self.runs(), [])
        self.assertEqual(self.tokens("compile"), [])
        self.assertEqual(len(self.tokens("tests")), 1)
        self.assertNotIn("compile", self.receipt("gate"))

    def test_the_step_is_an_argv_in_an_admitted_lane(self):
        document = {"schemaVersion": 1, "name": "Project"}
        for lane, message in (({"command": ["true"], "compileFirst": "xcodebuild build-for-testing"}, "argv"),
                              ({"command": ["true"], "compileFirst": []}, "argv"),
                              ({"command": ["true"], "compileFirst": ["xcodebuild", ""]}, "argv"),
                              ({"command": ["true"], "compileFirst": ["true"], "resource": "self"}, "admitted lane")):
            with self.subTest(lane=lane), self.assertRaisesRegex(ValueError, message):
                config.parse(json.dumps({**document, "lanes": {"tests": lane}}))
        # The positive control.
        config.parse(json.dumps({**document, "lanes": {"tests": {"command": ["true"], "compileFirst": ["true"]}}}))


if __name__ == "__main__":
    unittest.main()
