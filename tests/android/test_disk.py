"""Android disk inventory against a private home, project and reservation record."""
from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import host_isolation  # noqa: F401
import queuekeeper_android
from queuekeeper import disk
from queuekeeper_android import disk as android_disk
from queuekeeper_android.emulators import avd, pool
from queuekeeper_mobile import devices


class Inventory(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.development = self.home / "Development"
        self.project = self.development / "Example"
        (self.project / ".git").mkdir(parents=True)
        (self.project / "queuekeeper.json").write_text('{"emulatorPool": "Example"}')
        self.folder = self.home / ".android/avd"
        self.enterContext(patch.dict(os.environ, {name: "" for name in
                                                  ("ANDROID_AVD_HOME", "ANDROID_USER_HOME", "GRADLE_USER_HOME")}))
        self.records = {}
        self.enterContext(patch.object(devices.Record, "load", side_effect=lambda: {"devices": self.records}))
        self.now = datetime(2026, 11, 20, 12, tzinfo=timezone.utc).timestamp()
        self.host = disk.Host(self.home, self.development, self.now, 24 * 3600, [])

    def avd(self, name, folder=None):
        path = (folder or self.folder) / f"{name}.avd"
        path.mkdir(parents=True)
        avd.write_ini(path / "config.ini", {"image.sysdir.1": "system-images/android-36/google_apis/arm64-v8a/",
                                            "hw.device.name": "pixel_8"})
        return path

    def collect(self, *categories):
        def measure(command, **options):
            self.assertEqual(command[:2], ["du", "-skx"])
            return subprocess.CompletedProcess(command, 0, f"12\t{command[-1]}\n", "")

        return {Path(item.path): item for item in disk.inventory(self.host, disk.Sizes(run=measure), categories)}

    def test_pool_inventory_uses_the_project_or_a_named_reservation(self):
        queuekeeper_android.register()
        idle = self.avd("Example_Pixel_8_API_36_1")
        held = self.avd("OldPool_Pixel_8_API_36_2")
        template = self.avd("Example_Pixel_8_API_36_template")
        (template / pool.WARMED).touch()
        self.avd("Personal_Pixel_8_API_36_1")
        self.avd("Example_Mismatched_kind_3")
        self.records[idle.stem] = {"device": f"avd:{idle.stem}", "state": "released",
                                   "releasedAt": "2026-11-20T10:00:00Z"}
        self.records[held.stem] = {"device": f"avd:{held.stem}", "state": "reserved"}
        self.host.processes = [disk.Process(42, "emulator", paths=(str(held / "userdata-qemu.img"),))]

        rows = self.collect("android-avds")
        self.assertEqual(set(rows), {idle, held, template})
        self.assertEqual(rows[idle].idle, 7200)
        self.assertIn("reservation released 2 h ago", rows[idle].reason)
        self.assertIn("held reservation", rows[held].reason)
        self.assertIn("PID 42", rows[held].reason)
        self.assertIn("warmed pool template", rows[template].reason)
        self.assertTrue(all(item.status == disk.KEPT and item.command is None and item.size == 12 * 1024
                            for item in rows.values()))

    def test_custom_roots_and_logs_are_measured_without_following_links(self):
        queuekeeper_android.register()
        custom_avds = self.home / "AVDs"
        gradle = self.home / "Gradle cache"
        member = self.avd("Example_Pixel_8_API_36_1", custom_avds)
        (custom_avds / "Example_Pixel_8_API_36_2.avd").symlink_to(member, target_is_directory=True)
        log = self.project / ".build/queuekeeper/emulator-tests"
        folders = (log, gradle / "caches", gradle / "wrapper/dists")
        for folder in folders:
            folder.mkdir(parents=True)
        with patch.dict(os.environ, {"ANDROID_AVD_HOME": str(custom_avds), "GRADLE_USER_HOME": str(gradle)}), \
                patch.object(devices.Record, "load", side_effect=devices.Refused("unreadable")):
            rows = self.collect(*android_disk.CATEGORIES)
        self.assertEqual(set(rows), {member, *folders})
        self.assertIn("device record could not be read", rows[member].reason)
        self.assertEqual(rows[log].category, "android-logs")
        self.assertTrue(all(item.status == disk.KEPT and item.command is None for item in rows.values()))
