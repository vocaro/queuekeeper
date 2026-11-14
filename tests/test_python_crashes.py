"""Read-only Python crash notes, never a gate verdict (#130)."""
import host_isolation
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from queuekeeper import offload_host, remote


class CrashNotes(unittest.TestCase):
    def test_recent_reports_include_signal_parent_and_crashed_frames_without_changing_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = {"captureTime": "2026-11-14T10:00:00Z", "exception": {"signal": "SIGABRT"},
                      "parentProc": "python3", "faultingThread": 1,
                      "threads": [{"frames": []}, {"frames": [{"symbol": "abort"}, {"symbol": "fixture"}]}]}
            recent = root / "Python-recent.ips"
            recent.write_text(json.dumps({"timestamp": "metadata"}) + "\n" + json.dumps(report))
            old = root / "Python-old.ips"
            old.write_text(json.dumps(report))
            os.utime(old, (1, 1))
            broken = root / "Python-broken.ips"
            broken.write_text("incomplete report")
            before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in root.iterdir()}
            result = offload_host.python_crash_reports(root)
            self.assertEqual(len(result["reports"]), 1)
            found = result["reports"][0]
            self.assertEqual(found["signal"], "SIGABRT")
            self.assertEqual(found["parentProcess"], "python3")
            self.assertEqual(found["time"], report["captureTime"])
            self.assertEqual(found["frames"], report["threads"][1]["frames"])
            self.assertIn("Python-broken.ips", result["notes"][0])
            self.assertEqual(before, {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in root.iterdir()})

    def test_local_notes_are_available_even_without_a_second_mac_and_are_not_errors(self):
        notes = {"since": "yesterday", "reports": [{"signal": "SIGQUIT"}], "notes": []}
        with patch.object(remote, "configured", return_value=None), \
                patch.object(offload_host, "python_crash_reports", return_value=notes):
            result = remote.compare_hosts()
        self.assertEqual(result["pythonCrashes"]["local"], notes)
        self.assertEqual(result["errors"], [])

    def test_host_survey_carries_crash_notes_for_the_second_mac(self):
        notes = {"since": "yesterday", "reports": [{"signal": "SIGABRT"}], "notes": []}
        with patch.object(offload_host, "identity", return_value={"xcode": None}), \
                patch.object(offload_host, "output", return_value=None), \
                patch.object(offload_host, "xcode", return_value=None), \
                patch.object(offload_host, "device_record", return_value={}), \
                patch.object(offload_host, "python_crash_reports", return_value=notes):
            result = offload_host.toolchain(None)
        self.assertEqual(result["pythonCrashes"], notes)
