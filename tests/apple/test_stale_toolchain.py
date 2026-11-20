"""A build plan that names a Metal Toolchain mount macOS has since replaced is cleared and the build runs once more;
a build that still fails so is the Mac's fault, and its lane ends unmeasured (vocaro/queuekeeper#281)."""
import contextlib
from io import StringIO
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from unittest.mock import patch
import host_isolation
from host_isolation import stretched
from queuekeeper import cli, lane_results, plugins
from queuekeeper_apple import stale_modules, stale_toolchain

SHIM = stale_modules.SHIMS / "xcodebuild"
# A mount that is not this Mac's: the stand-in's error names it, as a build plan made before a remount does.
MOUNT = "/var/run/com.apple.security.cryptexd/mnt/com.apple.MobileAsset.MetalToolchain-v27.2.47.0.KvKoUX"
METAL = f"{MOUNT}/Metal.xctoolchain/usr/bin/metal"
PLAN = ".build/ios/Build/Intermediates.noindex/XCBuildData"
# A stand-in build tool. It counts its runs in STANDIN_COUNT and, by STANDIN_MODE: stale fails to spawn METAL while a
# manifest under STANDIN_PLANS names its mount, as a build on a stale plan does; always fails so every time; other fails
# without it.
STANDIN = textwrap.dedent(f"""\
    #!/usr/bin/env python3
    import os, sys
    from pathlib import Path
    count = Path(os.environ["STANDIN_COUNT"])
    count.write_text((count.read_text() if count.exists() else "") + " ".join(sys.argv[1:]) + "\\n")
    mode = os.environ["STANDIN_MODE"]
    error = "error: unable to spawn process '{METAL}' (No such file or directory)"
    if mode == "other":
        print("error: cannot find 'something' in scope", file=sys.stderr); sys.exit(65)
    named = any("KvKoUX" in path.read_text(errors="replace") for plans in os.environ["STANDIN_PLANS"].split(":")
                for path in Path(plans).rglob("manifest.json"))
    if mode == "always" or named:
        print(error, file=sys.stderr)
        print("error: CompileMetalFile Sources/Lib/FoilSticker.metal failed with a nonzero exit code", file=sys.stderr)
        sys.exit(65)
    print("** BUILD SUCCEEDED **")
""")


def plan(folder, mount=MOUNT):
    """A build plan in folder whose manifest names mount."""
    manifest = Path(folder) / "abc123.xcbuilddata" / "manifest.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"commands": {"metal": {"args": [f"{mount}/Metal.xctoolchain/usr/bin/metal"]}}}))
    return Path(folder)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="stale-toolchain-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.checkout = self.root / "checkout"
        self.tools = self.root / "tools"
        self.tools.mkdir()
        for tool in stale_modules.TOOLS: host_isolation.executable(self.tools / tool, STANDIN)
        self.count = self.root / "count"
        self.plan = plan(self.checkout / PLAN)
        # Another build folder's plan, made after the remount, names no old mount and is kept.
        self.fresh = plan(self.checkout / ".build/out/Intermediates.noindex/XCBuildData", MOUNT[:-6] + "vIQWJo")
        self.results = self.root / "results"
        self.outside = self.root / "DerivedData/App/Build/Intermediates.noindex/XCBuildData"

    def variables(self, mode):
        return stale_modules.environment(
            {**os.environ, "PATH": os.pathsep.join([str(self.tools), os.environ.get("PATH", os.defpath)]),
             "STANDIN_COUNT": str(self.count), "STANDIN_MODE": mode,
             "STANDIN_PLANS": os.pathsep.join([str(self.checkout), str(self.outside)]),
             stale_modules.RESULTS_VARIABLE: str(self.results)}, self.checkout)

    def runs(self):
        return self.count.read_text().splitlines() if self.count.exists() else []


class Reading(unittest.TestCase):
    def test_the_mounts_of_tools_that_could_not_spawn_for_want_of_the_file(self):
        text = "\n".join([f"error: unable to spawn process '{METAL}' (No such file or directory)",
                          f"error: unable to spawn process '{MOUNT}/Metal.xctoolchain/usr/bin/metallib' (No such file "
                          "or directory)",
                          f"error: unable to spawn process '/private{METAL}' (No such file or directory)"])
        self.assertEqual(stale_toolchain.mounts(text), [MOUNT, "/private" + MOUNT])
        # Controls: a tool outside cryptexd's mounts, another cause, or no spawn at all.
        for other in ("error: unable to spawn process '/usr/bin/false' (No such file or directory)",
                      f"error: unable to spawn process '{METAL}' (Permission denied)",
                      "error: cannot find 'something' in scope"):
            self.assertEqual(stale_toolchain.mounts(other), [], other)

    def test_only_the_plans_inside_the_root_that_name_the_mount(self):
        root = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, root, True)
        stale = [plan(root / PLAN), plan(root / ".build/out/Intermediates.noindex/XCBuildData")]
        # By its folder's name, so a /private spelling of the mount is the same mount.
        stale.append(plan(root / "derived/Build/Intermediates.noindex/XCBuildData", "/private" + MOUNT))
        plan(root / "fresh/Build/Intermediates.noindex/XCBuildData", MOUNT[:-6] + "vIQWJo")
        # A plan anywhere else in a build folder, or under a folder passed over, is not one.
        plan(root / ".build/ios/Build/Intermediates.noindex/Lib.build/XCBuildData")
        plan(root / ".git/Intermediates.noindex/XCBuildData")
        found = stale_toolchain.plans(root, [MOUNT])
        self.assertEqual(sorted(found), sorted(stale))
        self.assertEqual(stale_toolchain.plans(root, ["/var/run/com.apple.security.cryptexd/mnt/other"]), [])

    def test_the_fault_recorded_in_a_lanes_results_is_its_hosts(self):
        with tempfile.TemporaryDirectory() as results:
            self.assertIsNone(stale_toolchain.host_fault(results))
            stale_toolchain.record_fault("first\nline", results)
            stale_toolchain.record_fault("second", results)
            self.assertEqual(stale_toolchain.host_fault(results), "second")
            self.assertEqual(plugins.host_fault(results), "second")
        self.assertIn(stale_toolchain.host_fault, plugins.HOST_FAULTS)
        self.assertIsNone(plugins.host_fault("/nonexistent/results"))


class Wrapper(Fixture):
    def wrapped(self, mode, tool="xcodebuild"):
        return subprocess.run([str(stale_modules.SHIMS / tool), "build"], cwd=self.checkout,
                              env=self.variables(mode), capture_output=True, text=True, timeout=stretched(60))

    def test_a_stale_plan_is_removed_and_the_build_runs_once_more(self):
        for tool in stale_modules.TOOLS:
            with self.subTest(tool=tool):
                plan(self.plan)
                self.count.unlink(missing_ok=True)
                shutil.rmtree(self.results, ignore_errors=True)
                finished = self.wrapped("stale", tool)
                self.assertEqual(finished.returncode, 0, finished.stderr)
                self.assertEqual(len(self.runs()), 2)
                self.assertFalse(self.plan.exists())
                # The build folder's other parts, and a plan naming the mount in use, are kept.
                self.assertTrue(self.plan.parent.is_dir())
                self.assertTrue(self.fresh.is_dir())
                self.assertIn(f"{stale_toolchain.NOTICE}: {tool} could not start", finished.stderr)
                self.assertIn(f"removed the build plans that name it, {self.plan}", finished.stderr)
                self.assertIn("** BUILD SUCCEEDED **", finished.stdout)
                self.assertIn(f"{tool}: removed {self.plan}; the rebuild ended with status 0",
                              (self.results / stale_toolchain.RECORD).read_text())
                self.assertIsNone(stale_toolchain.host_fault(self.results))

    def test_the_same_failure_again_is_the_hosts_fault_and_not_run_a_third_time(self):
        finished = self.wrapped("always")
        self.assertEqual(finished.returncode, 65)
        self.assertEqual(len(self.runs()), 2)
        self.assertIn("the same error again, so it is not run a third time", finished.stderr)
        fault = stale_toolchain.host_fault(self.results)
        self.assertIn("this Mac's fault, not the commits'", fault)
        self.assertIn("ran it once more, which failed the same way", fault)

    def test_a_plan_outside_the_checkout_is_left_and_the_fault_says_so(self):
        shutil.rmtree(self.plan)
        plan(self.outside)
        finished = self.wrapped("stale")
        self.assertEqual(finished.returncode, 65)
        self.assertEqual(len(self.runs()), 1)
        self.assertTrue(self.outside.is_dir())
        self.assertIn("No build plan inside", stale_toolchain.host_fault(self.results))
        self.assertFalse((self.results / stale_toolchain.RECORD).exists())

    def test_another_failure_runs_once_and_records_nothing(self):
        finished = self.wrapped("other")
        self.assertEqual(finished.returncode, 65)
        self.assertEqual(len(self.runs()), 1)
        self.assertTrue(self.plan.is_dir())
        self.assertNotIn("STALE", finished.stderr)
        self.assertFalse(self.results.exists())


class Lane(Fixture):
    """qk run's lane, whose script sends the build's output to a log of its own, and its compileFirst step."""

    def setUp(self):
        super().setUp()
        build = ["/bin/sh", "-c", "mkdir -p .build/validation && xcodebuild build-for-testing "
                 "> .build/validation/build.log 2>&1 && echo 'Ran 1 tests'"]
        (self.checkout / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Project", "lanes": {
            "tests": {"command": build, "resource": "heavy"},
            "compiled": {"command": ["/bin/sh", "-c", "echo 'Ran 1 tests'"], "resource": "heavy",
                         "compileFirst": ["xcodebuild", "build-for-testing"]}}}))
        variables = {"PATH": os.pathsep.join([str(self.tools), os.environ.get("PATH", os.defpath)]),
                     "STANDIN_COUNT": str(self.count), "STANDIN_MODE": "stale",
                     "STANDIN_PLANS": os.pathsep.join([str(self.checkout), str(self.outside)]),
                     "HEAVY_POLL_SECONDS": "0.01"}
        for patcher in (patch.dict(os.environ, variables), patch.object(cli, "source_identity", return_value={}),
                        patch.object(cli, "refresh_policy", return_value=None)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_lane(self, name):
        with contextlib.redirect_stderr(StringIO()) as error, contextlib.redirect_stdout(StringIO()):
            status = cli.main(["--project", str(self.checkout), "run", "--here", name])
        receipt = json.loads((self.checkout / ".build/queuekeeper" / name / "source.json").read_text())
        return status, error.getvalue(), receipt

    def test_a_lane_in_a_reused_checkout_builds_past_a_stale_plan(self):
        status, error, receipt = self.run_lane("tests")
        self.assertEqual(status, 0, error)
        self.assertEqual(len(self.runs()), 2)
        self.assertFalse(self.plan.exists())
        self.assertEqual(receipt["outcome"], "passed")
        self.assertIn(f"{stale_toolchain.NOTICE}: xcodebuild could not start",
                      (self.checkout / ".build/validation/build.log").read_text())

    def test_a_stale_mount_that_stays_leaves_the_lane_unmeasured_not_failed(self):
        for name in ("tests", "compiled"):
            with self.subTest(lane=name), patch.dict(os.environ, {"STANDIN_MODE": "always"}):
                plan(self.plan)
                self.count.unlink(missing_ok=True)
                status, error, receipt = self.run_lane(name)
                self.assertEqual(status, lane_results.UNMEASURED, error)
                self.assertEqual(len(self.runs()), 2)
                self.assertEqual(receipt["outcome"], "unmeasured")
                self.assertIn("this Mac's fault, not the commits'", receipt["error"])
                self.assertEqual(receipt["unmeasured"], [receipt["error"]])
                self.assertIn("for the Mac's fault", error)

    def test_a_plain_build_failure_stays_failed(self):
        with patch.dict(os.environ, {"STANDIN_MODE": "other"}):
            status, error, receipt = self.run_lane("compiled")
        self.assertEqual(status, 65, error)
        self.assertEqual(receipt["outcome"], "build-failed")
        self.assertNotIn("unmeasured", receipt)


if __name__ == "__main__":
    unittest.main()
