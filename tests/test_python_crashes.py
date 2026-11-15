"""Read-only Python crash notes, never a gate verdict (#130)."""
import host_isolation
import contextlib
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from queuekeeper import cli, offload_host, remote


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

    def test_malformed_json_shapes_become_notes_not_exceptions(self):
        cases = [[], {"threads": {"0": {}}}, {"threads": [False]},
                 {"threads": [{"triggered": True, "frames": {"0": {}}}]},
                 {"threads": [{"triggered": True, "frames": [None]}]}, {"exception": []}]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for n, case in enumerate(cases):
                (root / f"Python-{n}.ips").write_text(json.dumps(case))
            result = offload_host.python_crash_reports(root)
            self.assertEqual(result["reports"], [])
            self.assertEqual(len(result["notes"]), len(cases))

    def test_remote_survey_notes_reach_doctors_readable_output_without_failing(self):
        census = {"since": "yesterday", "reports": [{"path": "/fake/Python-crash.ips",
                  "time": "2026-11-14", "signal": "SIGABRT", "parentProcess": "fixture",
                  "frames": [{"symbol": "abort"}]}], "notes": []}
        with patch.object(remote, "configured", return_value="fake"), \
                patch.object(remote, "connection", return_value={}), \
                patch.object(remote, "slow_connection", return_value=None), \
                patch.object(remote, "developer_directory", return_value=None), \
                patch.object(remote, "reachable"), \
                patch.object(remote, "survey", return_value={"pythonCrashes": census}), \
                patch.object(remote, "leftover_census", return_value=({"leftovers": []}, None)), \
                patch.object(remote, "compare_device_records"), \
                patch.object(offload_host, "toolchain", return_value={}), \
                patch.object(offload_host, "python_crash_reports", return_value={"reports": [], "notes": []}):
            hosts = remote.compare_hosts()
        self.assertEqual(hosts["errors"], [])
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Example"}))
            with patch.object(remote, "compare_hosts", return_value=hosts), \
                    patch.object(cli, "fix_hosts", side_effect=lambda root, report: report), \
                    patch.object(cli.shutil, "which", return_value="/usr/bin/true"), \
                    patch.object(cli, "source_identity", return_value={}), \
                    contextlib.redirect_stdout(StringIO()) as output:
                status = cli.main(["--project", directory, "doctor"])
        self.assertEqual(status, 0)
        note = json.loads(output.getvalue())["notes"][0]
        for expected in ["fake", "Python-crash.ips", "2026-11-14", "SIGABRT", "fixture", "abort"]:
            self.assertIn(expected, note)
