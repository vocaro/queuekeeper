"""Direct screen-capture approvals: one launch path per capturing runner, and when they lapse (#209)."""
import contextlib
from datetime import datetime, timezone
from io import StringIO
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src/python"))
import host_isolation  # noqa: F401  Keeps every test out of the host's heavy-work admission.
from queuekeeper import offload_host, remote
from queuekeeper_mac_desktop import capture_approvals

RUNNER = "AppForegroundTests-Runner.app"


def make_runner(root, marker):
    runner = Path(root) / "Debug" / RUNNER
    (runner / "Contents/MacOS").mkdir(parents=True)
    (runner / "Contents/Info.plist").write_text("<plist/>")
    (runner / "Contents/MacOS/runner").write_text(marker)
    (runner / "Contents/Frameworks").mkdir()
    os.symlink("../MacOS/runner", runner / "Contents/Frameworks/link")
    return runner


class StableRunnerTests(unittest.TestCase):
    def test_path_is_per_project_outside_checkouts(self):
        self.assertEqual(capture_approvals.stable_runner_path("App", RUNNER, "/Users/someone"),
                         Path("/Users/someone/Library/Application Support/queuekeeper/foreground-runners/App")
                         / RUNNER)

    def test_rebind_moves_the_runner_and_its_contents_only(self):
        built = "/slot/3/derived-abc/Build/Products/Debug/" + RUNNER
        plan = {"TestHostPath": built, "DependentProductPaths": ["/slot/3/Debug/App.app", built, built + "/Contents/PlugIns/T.xctest"],
                "TestBundlePath": "__TESTHOST__/Contents/PlugIns/T.xctest",
                "Env": {"DYLD_FRAMEWORK_PATH": "/slot/3/derived-abc/Build/Products/Debug"}, "Sibling": built + "-other.app"}
        bound = capture_approvals.rebind(plan, built, "/stable/" + RUNNER)
        self.assertEqual(bound["TestHostPath"], "/stable/" + RUNNER)
        self.assertEqual(bound["DependentProductPaths"], ["/slot/3/Debug/App.app", "/stable/" + RUNNER,
                                                          "/stable/" + RUNNER + "/Contents/PlugIns/T.xctest"])
        for key in ["TestBundlePath", "Env", "Sibling"]:  # negative controls
            self.assertEqual(bound[key], plan[key])

    def test_stage_replaces_the_previous_copy(self):
        with tempfile.TemporaryDirectory() as root:
            stable = Path(root) / "stable" / RUNNER
            capture_approvals.stage(make_runner(Path(root) / "a", "first"), stable, [])
            capture_approvals.stage(make_runner(Path(root) / "b", "second"), stable, [])
            self.assertEqual((stable / "Contents/MacOS/runner").read_text(), "second")
            self.assertTrue((stable / "Contents/Frameworks/link").is_symlink())
            self.assertEqual([path.name for path in stable.parent.iterdir()], [RUNNER])

    def test_stage_keeps_a_codesigned_bundle_valid(self):
        with tempfile.TemporaryDirectory() as root:
            built = make_runner(Path(root) / "a", "unused")
            (built / "Contents/MacOS/runner").unlink()
            (built / "Contents/Frameworks/link").unlink()
            subprocess.run(["cp", "/usr/bin/true", str(built / "Contents/MacOS/runner")], check=True)
            (built / "Contents/Info.plist").write_bytes(plistlib.dumps(
                {"CFBundleExecutable": "runner", "CFBundleIdentifier": "test.stable.runner"}))
            subprocess.run(["codesign", "-s", "-", "--force", str(built)], check=True, capture_output=True)
            stable = capture_approvals.stage(built, Path(root) / "stable" / RUNNER, [])
            verified = subprocess.run(["codesign", "--verify", "--strict", str(stable)], capture_output=True)
            self.assertEqual(verified.returncode, 0, verified.stderr)

    def test_stage_refuses_while_a_runner_runs_from_the_stable_path(self):
        with tempfile.TemporaryDirectory() as root:
            stable = Path(root) / "stable" / RUNNER
            capture_approvals.stage(make_runner(Path(root) / "a", "first"), stable, [])
            with self.assertRaisesRegex(RuntimeError, "still running"):
                capture_approvals.stage(make_runner(Path(root) / "b", "second"), stable, [{"pid": 1}])
            self.assertEqual((stable / "Contents/MacOS/runner").read_text(), "first")

    def test_stage_refuses_a_missing_build(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(RuntimeError, "missing"):
                capture_approvals.stage(Path(root) / RUNNER, Path(root) / "stable" / RUNNER, [])
            self.assertFalse((Path(root) / "stable").exists())


def approval(alerted, hint):
    return {"kScreenCaptureAlertableUsageCount": 3, "kScreenCaptureApprovalLastAlerted": alerted,
            "kScreenCaptureApprovalLastUsed": alerted, "kScreenCapturePrivacyHintDate": hint,
            "kScreenCapturePrivacyHintPolicy": 2592000}


class LapseTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.record = self.home / offload_host.CAPTURE_APPROVALS
        self.record.parent.mkdir(parents=True)
        stable = (self.home / offload_host.CAPTURE_RUNNERS / "App" / RUNNER).as_uri() + "/"
        self.entries = {
            stable: approval(datetime(2026, 11, 7, 2, 46, 41), datetime(2026, 12, 7, 2, 46, 41)),
            "/usr/libexec/sshd-keygen-wrapper": approval(datetime(2026, 10, 30, 18, 36), datetime(2026, 11, 29, 18, 36)),
            # Negative controls: a checkout slot's old runner, another app, and a malformed entry.
            "file:///Users/x/checkouts/App/0/.build/Debug/" + RUNNER + "/": approval(datetime(2026, 10, 1), datetime(2026, 10, 31)),
            "com.example.other": approval(datetime(2026, 10, 1), datetime(2026, 10, 31)),
            (self.home / offload_host.CAPTURE_RUNNERS / "Bad" / RUNNER).as_uri() + "/": {"kScreenCapturePrivacyHintDate": "soon"}}

    def write(self, value):
        self.record.write_bytes(plistlib.dumps(value))

    def test_lists_only_the_stable_runners_and_the_ssh_host_soonest_first(self):
        self.write(self.entries)
        found = offload_host.screen_capture_approvals(home=self.home)
        self.assertEqual(found, [
            {"app": "/usr/libexec/sshd-keygen-wrapper", "allowed": "2026-10-30T18:36:00+00:00",
             "lapses": "2026-11-29T18:36:00+00:00"},
            {"app": str(self.home / offload_host.CAPTURE_RUNNERS / "App" / RUNNER), "allowed": "2026-11-07T02:46:41+00:00",
             "lapses": "2026-12-07T02:46:41+00:00"}])
        self.assertIs(capture_approvals.lapses, offload_host.screen_capture_approvals)

    def test_an_unreadable_record_is_none_and_an_unrelated_one_empty(self):
        self.assertIsNone(offload_host.screen_capture_approvals(home=self.home))
        self.record.write_text("not a plist")
        self.assertIsNone(offload_host.screen_capture_approvals(home=self.home))
        self.write({"com.example.other": self.entries["com.example.other"]})
        self.assertEqual(offload_host.screen_capture_approvals(home=self.home), [])

    def test_report_says_lapsed_or_lapses_and_when_it_cannot_read(self):
        found = [{"app": "/usr/libexec/sshd-keygen-wrapper", "allowed": "2026-10-30T18:36:00+00:00",
                  "lapses": "2026-11-29T18:36:00+00:00"}]
        before, after = (capture_approvals.report(found, datetime(2026, 11, day, tzinfo=timezone.utc)) for day in (6, 30))
        self.assertIn("sshd-keygen-wrapper lapses 2026-11-29 18:36 UTC", before[0])
        self.assertIn("sshd-keygen-wrapper lapsed 2026-11-29 18:36 UTC", after[0])
        self.assertIn("cannot be read", capture_approvals.report(None)[0])
        self.assertEqual(capture_approvals.report([]), [])

    def test_eng_remote_foreground_lists_them_without_changing_its_verdict(self):
        found = [{"app": "/usr/libexec/sshd-keygen-wrapper", "allowed": "2026-10-30T18:36:00+00:00",
                  "lapses": "2026-11-29T18:36:00+00:00"}]
        for answer in [{"mac": "Second", "problems": [], "entry": None, "captureApprovals": found},
                       {"mac": "Second", "problems": [], "entry": None}]:  # an older second Mac says nothing
            with self.subTest(keys=sorted(answer)), patch.object(remote, "ask", return_value=answer), \
                 contextlib.redirect_stdout(StringIO()) as output:
                self.assertEqual(remote.foreground_report("build-host"), 0)
            self.assertEqual("direct screen-capture approval for /usr/libexec/sshd-keygen-wrapper" in output.getvalue(),
                             "captureApprovals" in answer)


if __name__ == "__main__":
    unittest.main()
