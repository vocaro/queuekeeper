"""`qk devices` from the Mac desktop plugin alone: the foreground's actions, in a project with no device plugin
(vocaro/queuekeeper#48)."""
import contextlib
from datetime import datetime, timedelta, timezone
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src/python"))
import host_isolation  # noqa: F401  Keeps every test out of the host's heavy-work admission.
from host_isolation import stretched
from queuekeeper_mac_desktop import reservations


def scratch():
    """A test directory outside /private/tmp, which the record refuses, even when TMPDIR is there."""
    base = Path(tempfile.gettempdir())
    if reservations.restart_emptied(base) and sys.platform == "darwin":
        base = Path(subprocess.check_output(["/usr/bin/getconf", "DARWIN_USER_TEMP_DIR"], text=True).strip())
    return tempfile.TemporaryDirectory(dir=base)


def later(minutes=30):
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


class ForegroundCommandTests(unittest.TestCase):
    def setUp(self):
        temp = scratch()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "device-handoff.json"
        patcher = patch.dict(os.environ, {reservations.OVERRIDE: str(self.path)})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(reservations.OWNER, None)

    def cli(self, *arguments):
        with contextlib.redirect_stdout(StringIO()) as output, contextlib.redirect_stderr(StringIO()) as error:
            code = reservations.main(list(arguments))
        return code, output.getvalue(), error.getvalue()

    def test_a_project_with_only_the_desktop_plugin_has_the_command(self):
        # Unity builds on the Mac desktop plugin and on no device plugin, so `qk devices` is this plugin's.
        program = ("import json, sys\nfrom queuekeeper import plugins\nplugins.load(['queuekeeper_unity'])\n"
                   "code = plugins.COMMANDS['devices'][1](['status'], None)\n"
                   "print(json.dumps({'code': code, 'mobile': 'queuekeeper_mobile' in sys.modules}))\n")
        environment = {**os.environ, "PYTHONPATH": host_isolation.pythonpath()}
        found = subprocess.run([sys.executable, "-c", program], env=environment, capture_output=True, text=True,
                               timeout=stretched(60))
        self.assertEqual(found.returncode, 0, found.stderr)
        *printed, last = found.stdout.splitlines()
        self.assertEqual(json.loads(last), {"code": 0, "mobile": False})
        self.assertIn(f"Device record {self.path}", printed[0])
        self.assertTrue(any(line.startswith(f"{reservations.FOREGROUND}: free") for line in printed), printed)

    def test_status_filters_by_owner(self):
        # vocaro/queuekeeper#372, in a project with no device plugin.
        code, _output, error = self.cli("reserve", reservations.FOREGROUND, "--owner", "session-a", "--revision", "0",
                                        "--purpose", "attended Unity play mode test", "--release-by", later())
        self.assertEqual(code, 0, error)
        self.assertIn(f"{reservations.FOREGROUND}: held; owner session-a",
                      self.cli("status", "--owner", "session-a")[1])
        with patch.dict(os.environ, {reservations.OWNER: "session-a"}):
            self.assertIn(f"{reservations.FOREGROUND}: held; owner session-a", self.cli("status", "--mine")[1])
        output = self.cli("status", "--owner", "session-b")[1]
        self.assertNotIn(f"{reservations.FOREGROUND}:", output)
        self.assertIn("session-b holds no reservation in this record", output)

    def test_the_foreground_is_reserved_checked_and_released(self):
        code, output, error = self.cli("reserve", reservations.FOREGROUND, "--owner", "session-a", "--revision", "0",
                                       "--purpose", "attended Unity play mode test", "--release-by", later())
        self.assertEqual(code, 0, error)
        revision = json.loads(output)["revision"]
        code, output, error = self.cli("check", "--owner", "session-a")
        self.assertEqual(code, 0, error)
        self.assertIn("held by session-a for attended Unity play mode test", output)
        code, output, _error = self.cli("status")
        self.assertIn(f"{reservations.FOREGROUND}: held; owner session-a", output)
        code, output, error = self.cli("release", reservations.FOREGROUND, "--owner", "session-a", "--revision",
                                       str(revision), "--outcome", "passed", "--restoration", "verified",
                                       "--frontmost", "restored")
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)["condition"], "released")
        code, _output, error = self.cli("check", "--owner", "session-a")
        self.assertEqual(code, 2)
        self.assertIn(f"nobody holds {reservations.FOREGROUND}", error)

    def test_a_held_foreground_is_recovered_by_the_owner(self):
        entry = reservations.Record().reserve(reservations.FOREGROUND, owner="session-a", revision=0,
                                              purpose="attended test", release_by=later())
        code, output, error = self.cli("recover", reservations.FOREGROUND, "--by", "owner", "--revision",
                                       str(entry["revision"]), "--verified", "the screen was checked")
        self.assertEqual(code, 0, error)
        recovered = json.loads(output)
        self.assertEqual(recovered["condition"], "released")
        self.assertEqual(recovered["entry"]["recovery"]["verified"], "the screen was checked")

    def test_a_window_is_recorded_only_on_the_creators_foreground_revision(self):
        record = reservations.Record()
        key = reservations.FOREGROUND
        entry = record.reserve(key, owner="creator", revision=0, purpose="window test", release_by=later())
        revision = str(entry["revision"])
        window = ("--pid", "42", "--start", "observed start", "--token", "window-7")
        with patch.object(reservations, "process_start", return_value="other start"):
            code, _output, error = self.cli("record-window", "--owner", "creator", "--revision", revision, *window)
            self.assertEqual(code, 2)
            self.assertIn("no longer has the recorded process start", error)
        self.assertEqual(record.entry(key), entry)
        with patch.object(reservations, "process_start", return_value="observed start"):
            code, _output, error = self.cli("record-window", "--owner", "another-run", "--revision", revision, *window)
            self.assertEqual(code, 2)
            self.assertIn("is held by creator", error)
            self.assertEqual(record.entry(key), entry)
            code, output, error = self.cli("record-window", "--owner", "creator", "--revision", revision, *window)
            self.assertEqual(code, 0, error)
            recorded = json.loads(output)["entry"]
        self.assertEqual(recorded["openedWindows"], [{"pid": 42, "start": "observed start", "token": "window-7"}])
        released = record.release(key, owner="creator", revision=recorded["revision"], outcome="passed",
                                  restoration="verified", frontmost="restored")
        next_lease = record.reserve(key, owner="creator", revision=released["revision"],
                                    purpose="new window test", release_by=later())
        code, _output, error = self.cli("record-window", "--owner", "creator", "--revision", revision, *window)
        self.assertEqual(code, 2)
        self.assertIn("is at revision", error)
        self.assertNotIn("openedWindows", record.entry(key))
        self.assertEqual(record.entry(key), next_lease)

    def test_general_record_changes_cannot_forge_a_window_record(self):
        record = reservations.Record()
        key = reservations.FOREGROUND
        with self.assertRaisesRegex(ValueError, "the record writes openedWindows itself"):
            record.reserve(key, owner="creator", revision=0, purpose="window test", release_by=later(),
                           details={"openedWindows": []})
        entry = record.reserve(key, owner="creator", revision=0, purpose="window test", release_by=later())
        with self.assertRaisesRegex(ValueError, "the record writes openedWindows itself"):
            record.update(key, owner="creator", revision=entry["revision"], openedWindows=[])
        self.assertEqual(record.entry(key), entry)

    def test_device_actions_are_not_offered_without_a_device_plugin(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(StringIO()) as error:
            reservations.main(["take", "iPhone"])
        self.assertIn("invalid choice: 'take'", error.getvalue())


if __name__ == "__main__":
    unittest.main()
