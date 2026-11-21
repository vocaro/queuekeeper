"""Android SDK inventories and the Android plugin's cross-Mac findings, without reaching another Mac."""
import contextlib
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import host_isolation
from queuekeeper import cli, offload_host, plugins, remote
from queuekeeper_android import doctor, sdk
from test_support import farm as farm_fixture


IMAGE = "system-images;android-36;google_apis;arm64-v8a"
INSTALLED = f"""[=======================================] 100% Loading package information...
Installed packages:
  Path | Version | Description | Location
  ------- | ------- | ------- | -------
  emulator | 36.1.2 | Android Emulator | emulator
  platform-tools | 36.0.0 | Android SDK Platform-Tools | platform-tools
  {IMAGE} | 7 | Google APIs ARM 64 v8a System Image | system-images/android-36/google_apis/arm64-v8a
  platforms;android-36 | 2 | Android SDK Platform 36 | platforms/android-36

Available Packages:
  Path | Version | Description
  emulator | 36.2.3 | Android Emulator
"""
PACKAGES = {"emulator": "36.1.2", "platform-tools": "36.0.0", IMAGE: "7"}


class Inventory(unittest.TestCase):
    def test_only_installed_host_care_packages_and_their_revisions_are_read(self):
        self.assertEqual(sdk.package_revisions(INSTALLED), PACKAGES)
        self.assertEqual(sdk.package_revisions("Installed packages:\nPath | Version | Description\n"), {})

    def test_an_invalid_table_is_not_read_as_an_empty_success(self):
        for output in ("Loading package information...", "Installed packages:\n",
                       "Installed packages:\nPath | Version\nplatform-tools |"):
            with self.subTest(output=output), self.assertRaises(ValueError):
                sdk.package_revisions(output)

    def test_remote_probe_uses_the_other_macs_sdk_and_the_same_inventory_code(self):
        with tempfile.TemporaryDirectory() as folder:
            sdk_root = Path(folder) / "SDK with space"
            manager = sdk_root / "cmdline-tools/latest/bin/sdkmanager"
            manager.parent.mkdir(parents=True)
            java = Path(folder) / "jdk/bin/java"
            java.parent.mkdir(parents=True)
            java.touch()
            listing = sdk_root / "installed.txt"
            listing.write_text(INSTALLED)
            host_isolation.executable(manager, """#!/bin/sh
[ "$1" = --list_installed ] || exit 11
[ "$2" = "--sdk_root=$ANDROID_HOME" ] || exit 12
exec /bin/cat "$ANDROID_HOME/installed.txt"
""")
            environment = {**os.environ, "ANDROID_HOME": str(sdk_root), "ANDROID_SDK_ROOT": "",
                           "JAVA_HOME": str(java.parents[1]), "PATH": str(Path(sys.executable).parent)}

            def ssh(host, command, **options):
                self.assertEqual(host, "build")
                return subprocess.run(["/bin/sh", "-c", command], env=environment, **options)

            with patch.object(remote, "ssh", side_effect=ssh) as read:
                self.assertEqual(doctor.inventory_there("build"), {"packages": PACKAGES})
            read.assert_called_once()

    def test_failed_local_and_remote_commands_report_why_the_inventory_is_unknown(self):
        with patch.object(sdk, "tool", side_effect=sdk.Missing("sdkmanager is missing")):
            self.assertEqual(sdk.inventory(), {"error": "sdkmanager is missing"})
        with patch.object(remote, "ssh", return_value=subprocess.CompletedProcess([], 0, "", "")):
            self.assertIn("error", doctor.inventory_there("build"))
        with patch.object(remote, "ssh", return_value=subprocess.CompletedProcess([], 255, "", "Connection reset")):
            self.assertIn("Connection reset", doctor.inventory_there("build")["error"])


def comparison(host, *, remote=True):
    return {"host": host, "errors": [], "unchecked": [] if remote else [f"{host} cannot be reached"],
            **({"remote": {}} if remote else {})}


class Comparison(unittest.TestCase):
    def test_a_differing_version_or_missing_package_fails_doctor_on_the_respective_mac(self):
        reports = {host: comparison(host) for host in ("build", "mini")}
        there = {"packages": {"emulator": "36.0.9", IMAGE: "6"}}
        with tempfile.TemporaryDirectory() as folder, farm_fixture.only(farm_fixture.mac("build", "builder"), "mini"), \
                patch.object(cli, "load_plugins", side_effect=lambda argv: doctor.register()), \
                patch.object(plugins, "HOST_CHECKS", []), patch.object(plugins, "DOCTOR", []), \
                patch.object(cli, "working_configuration", return_value={"name": "Android", "android": {"module": "app"}}), \
                patch.object(cli, "source_identity", return_value={}), \
                patch.object(remote, "compare_hosts", side_effect=reports.get), \
                patch.object(offload_host, "host_health", return_value={}), \
                patch.object(sdk, "tool", return_value="/fake/android-tool"), \
                patch.object(sdk, "java_home", return_value="/fake/jdk"), \
                patch.object(sdk, "inventory", return_value={"packages": PACKAGES}) as here, \
                patch.object(doctor, "inventory_there", side_effect={"build": there, "mini": {"packages": PACKAGES}}.get), \
                contextlib.redirect_stdout(StringIO()) as output:
            self.assertEqual(cli.main(["--project", folder, "doctor"]), 1)
        here.assert_called_once()
        build, mini = json.loads(output.getvalue())["farm"]
        self.assertEqual(build["local"]["androidSdk"], {"packages": PACKAGES})
        self.assertEqual(build["remote"]["androidSdk"], there)
        [error] = build["errors"]
        self.assertIn("emulator is 36.1.2 on this Mac and 36.0.9 on build", error)
        self.assertIn("platform-tools is 36.0.0 on this Mac and missing on build", error)
        self.assertIn(f"{IMAGE} is 7 on this Mac and 6 on build", error)
        self.assertEqual(mini["errors"], [])

    def test_one_unreadable_inventory_is_unchecked_and_unreachable_macs_are_not_probed(self):
        build, away = comparison("build"), comparison("away", remote=False)
        with patch.object(sdk, "inventory", return_value={"packages": PACKAGES}), \
                patch.object(doctor, "inventory_there", return_value={"error": "sdkmanager is missing"}) as read:
            doctor.compare_sdk(None, {"emulatorPool": "Android"}, [build, away])
        read.assert_called_once_with("build")
        self.assertEqual(build["unchecked"], ["Android SDK packages on build: sdkmanager is missing"])
        self.assertEqual(build["errors"], [])
        self.assertEqual(away["unchecked"], ["away cannot be reached"])

    def test_unconfigured_projects_do_not_probe_and_unreadable_local_sdk_is_unchecked(self):
        report = comparison(None, remote=False)
        with patch.object(sdk, "inventory", return_value={"error": "sdkmanager timed out"}) as local, \
                patch.object(doctor, "inventory_there") as other:
            doctor.compare_sdk(None, {}, [report])
            local.assert_not_called()
            doctor.compare_sdk(None, {"android": {}}, [report])
        local.assert_called_once()
        other.assert_not_called()
        self.assertIn("Android SDK packages on this Mac: sdkmanager timed out", report["unchecked"])
