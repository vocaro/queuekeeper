"""How the Android platform names, lists, silences and watches a device (queuekeeper_android.android_platform, adb,
device_notifications), read from recorded adb output with no device attached."""
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import host_isolation  # noqa: F401
from queuekeeper import remote
from queuekeeper_android import adb, android_platform, device_notifications, sdk, settings
from queuekeeper_mobile import devices, platforms

PIXEL = "33041FDH2000QK"
LISTING = """List of devices attached
33041FDH2000QK         device usb:1-1 product:shiba model:Pixel_8 device:shiba transport_id:3
emulator-5554          device product:sdk_gphone64_arm64 model:sdk_gphone64_arm64 device:emu64a transport_id:1
192.168.1.20:5555      device product:tangorpro model:Pixel_Tablet device:tangorpro transport_id:4
adb-R58M12ABCDE-x7Yz1Q._adb-tls-connect._tcp device product:a54 model:SM_A546E device:a54x transport_id:5
R3CT40ABCDE            unauthorized usb:1-2 transport_id:6
"""


def later(minutes=30):
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


class Names(unittest.TestCase):
    def test_a_serial_says_whether_it_is_a_cable_s_the_network_s_or_an_emulator_s(self):
        listed = adb.listed(LISTING)
        self.assertEqual({serial: device["way"] for serial, device in listed.items()}, {
            PIXEL: "cable", "emulator-5554": "emulator", "192.168.1.20:5555": "network",
            "adb-R58M12ABCDE-x7Yz1Q._adb-tls-connect._tcp": "network", "R3CT40ABCDE": "cable"})
        self.assertEqual(listed[PIXEL]["model"], "Pixel_8")
        self.assertEqual(adb.emulator_port("emulator-5556"), 5556)
        self.assertEqual(adb.hardware_serial("adb-R58M12ABCDE-x7Yz1Q._adb-tls-connect._tcp"), "R58M12ABCDE")
        self.assertIsNone(adb.hardware_serial("192.168.1.20:5555"))

    def test_a_physical_device_is_never_an_emulator_a_word_or_an_iphone_s_udid(self):
        physical = android_platform.physical
        for serial in (PIXEL, "192.168.1.20:5555", "adb-R58M12ABCDE-x7Yz1Q._adb-tls-connect._tcp", "ce0317131a4c1c1f05"):
            self.assertTrue(physical(serial), serial)
        for word in ("emulator-5554", "iPhone", "AndroidPhone", "a" * 40, "00008130-001A2C3E0C38001C", "", None):
            self.assertFalse(physical(word), word)
        self.assertTrue(android_platform.PLATFORM.virtual("avd:Pool_Pixel_8_API_36_1"))
        self.assertIs(platforms.of(PIXEL), android_platform.PLATFORM)

    def test_a_serial_keeps_its_case_in_the_record(self):
        self.assertEqual(devices.canonical("ce0317131a4c1c1f05"), "ce0317131a4c1c1f05")

    def test_the_listing_leaves_out_emulators_and_says_how_each_device_is_reached(self):
        found = android_platform.reached(LISTING)
        self.assertEqual(set(found), {PIXEL, "192.168.1.20:5555", "adb-R58M12ABCDE-x7Yz1Q._adb-tls-connect._tcp",
                                      "R3CT40ABCDE"})
        self.assertEqual((found[PIXEL]["way"], found[PIXEL]["model"]), ("cable", "Pixel 8"))
        self.assertEqual(found["R3CT40ABCDE"]["state"], "unauthorized")

    def test_this_mac_s_listing_is_read_from_the_tests_file_and_reaches_nothing_by_default(self):
        self.assertEqual(android_platform.PLATFORM.listing_here(), {})
        with tempfile.NamedTemporaryFile("w", suffix=".txt") as listing:
            listing.write(LISTING)
            listing.flush()
            with patch.dict(os.environ, {android_platform.LISTING: listing.name}):
                self.assertIn(PIXEL, android_platform.PLATFORM.listing_here())
                self.assertEqual(android_platform.PLATFORM.connections()["R3CT40ABCDE"]["connection"], "unavailable")

    def test_a_farm_mac_lists_devices_using_its_sdk_root_or_intel_homebrew_sdk(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            tool = root / "platform-tools/adb"
            tool.parent.mkdir()
            (root / "listing.txt").write_text(LISTING)
            host_isolation.executable(tool, '#!/bin/sh\n[ "$*" = "devices -l" ] && exec /bin/cat "${0%/*}/../listing.txt"\n')

            for intel_sdk in (False, True):
                with self.subTest(intel_sdk=intel_sdk):
                    empty = str(root / "absent")
                    defaults = {"$HOME/Library/Android/sdk": empty, sdk.DEFAULTS[1]: empty,
                                sdk.DEFAULTS[2]: folder if intel_sdk else empty}
                    environment = {"PATH": ""}
                    if not intel_sdk:
                        environment["ANDROID_SDK_ROOT"] = folder

                    def ssh(host, command, **options):
                        self.assertEqual(host, "farm-mac")
                        for default, path in defaults.items():
                            command = command.replace(default, path)
                        return subprocess.run(["/bin/sh", "-c", command], env=environment, **options)

                    with patch.object(remote, "ssh", side_effect=ssh):
                        self.assertEqual(android_platform.PLATFORM.listing_there("farm-mac"),
                                         android_platform.reached(LISTING))

    def test_health_names_a_device_adb_does_not_list_or_that_has_not_authorized_the_mac(self):
        health = android_platform.PLATFORM.health
        with patch.object(adb, "devices", return_value=adb.listed(LISTING)):
            self.assertIsNone(health(PIXEL, "AndroidPhone"))
            self.assertIn("not authorized", health("R3CT40ABCDE", "AndroidTablet"))
            self.assertIn("does not list", health("ZY2245ABCD", "AndroidPhone"))
        with patch.object(adb, "devices", side_effect=adb.Failed("no server")):
            self.assertIn("could not list", health(PIXEL, "AndroidPhone"))

    def test_the_owner_s_setup_is_refused_since_android_needs_none(self):
        with self.assertRaisesRegex(ValueError, "needs no setup"):
            android_platform.PLATFORM.setup("AndroidPhone", {})


class DoNotDisturb(unittest.TestCase):
    """Do Not Disturb set and read back over adb, against a device that answers as Android 16's shell does."""

    def setUp(self):
        self.zen, self.commands = "0", []

        def shell(serial, *words, timeout=None, check=True):
            self.commands.append(words)
            if words[:3] == ("settings", "get", "global"): return self.zen
            if words[:3] == ("cmd", "notification", "set_dnd"):
                self.zen = {"off": "0", "priority": "1", "none": "2", "alarms": "3"}[words[3]]
                return ""
            raise AssertionError(words)
        patcher = patch.object(adb, "shell", shell)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.temp = tempfile.TemporaryDirectory(dir=os.environ["QUEUEKEEPER_DEVICE_RECORD"].rsplit("/devices/", 1)[0])
        self.addCleanup(self.temp.cleanup)
        self.record = devices.Record(Path(self.temp.name) / "record.json")

    def test_silencing_reads_the_mode_back_and_release_puts_back_the_one_found(self):
        entry = self.record.reserve("AndroidPhone", owner="a", revision=0, purpose="p", release_by=later(),
                                    device=PIXEL)
        said = []
        entry = devices.silence_notifications(self.record, "AndroidPhone", owner="a", revision=entry["revision"],
                                              said=said.append)
        self.assertEqual(self.zen, "1")
        notifications = entry["notifications"]
        self.assertEqual((notifications["silence"], notifications["confirmed"], notifications["before"]),
                         ("requested", True, "off"))
        self.assertIn("Do Not Disturb on, read back", devices._notification_state("AndroidPhone", entry))
        entry = devices.restore_notifications(self.record, "AndroidPhone", owner="a", revision=entry["revision"],
                                              said=said.append)
        self.assertEqual((self.zen, entry["notifications"]["restore"]), ("0", "done"))

    def test_retry_after_a_lost_silence_reply_keeps_the_original_restore_pending(self):
        entry = self.record.reserve("AndroidPhone", owner="a", revision=0, purpose="p", release_by=later(),
                                    device=PIXEL)
        shell = adb.shell

        def lose_silence_reply(serial, *words, **options):
            result = shell(serial, *words, **options)
            if words == ("cmd", "notification", "set_dnd", "priority"):
                raise adb.Failed("the adb reply was lost")
            return result

        with patch.object(adb, "shell", side_effect=lose_silence_reply):
            entry = devices.silence_notifications(self.record, "AndroidPhone", owner="a",
                                                  revision=entry["revision"], said=lambda text: None)
        self.assertEqual(self.zen, "1")
        self.assertEqual(entry["notifications"]["silence"], "failed")
        entry = devices.silence_notifications(self.record, "AndroidPhone", owner="a",
                                              revision=entry["revision"], said=lambda text: None)
        notifications = entry["notifications"]
        self.assertEqual((notifications["silence"], notifications["before"], notifications["restore"]),
                         ("requested", "off", "pending"))
        self.assertEqual(self.commands.count(("cmd", "notification", "set_dnd", "priority")), 1)
        entry = devices.restore_notifications(self.record, "AndroidPhone", owner="a",
                                              revision=entry["revision"], said=lambda text: None)
        self.assertEqual((self.zen, entry["notifications"]["restore"]), ("0", "done"))

    def test_a_device_already_in_do_not_disturb_is_left_as_the_owner_s(self):
        self.zen = "3"
        entry = self.record.reserve("AndroidPhone", owner="a", revision=0, purpose="p", release_by=later(),
                                    device=PIXEL)
        entry = devices.silence_notifications(self.record, "AndroidPhone", owner="a", revision=entry["revision"],
                                              said=lambda text: None)
        self.assertEqual(entry["notifications"]["silence"], "owner")
        self.assertIsNone(devices.restore_for_recovery("AndroidPhone", entry, said=lambda text: None))
        self.assertEqual(self.zen, "3")

    def test_an_unreadable_mode_changes_nothing_and_leaves_nothing_to_put_back(self):
        with patch.object(device_notifications, "mode", return_value=None):
            outcome = device_notifications.silence(PIXEL)
        self.assertEqual((outcome["silence"], outcome["restore"]), ("failed", "not needed"))


class Watch(unittest.TestCase):
    EVENTS = """1763490000.123  1000  1234 I notification_enqueue: [10123,4321,com.example.reader,1,NULL,0,Notification(),0]
1763490001.456  1000  1234 I notification_enqueue: [10200,4400,com.chat.app,7,NULL,0,Notification(),0]
1763490001.789  1000  1234 I notification_alert: [0|com.chat.app|7|null|10200,1,1,0]
1763490002.000  1000  1234 I am_proc_start: [0,999,10123,com.example.reader,activity,{}]
"""

    def test_the_run_s_own_notifications_are_left_out_and_another_app_s_counts(self):
        found = device_notifications.events(self.EVENTS, own=("com.example.reader",))
        self.assertEqual([(event["tag"], event["package"]) for event in found],
                         [("notification_enqueue", "com.chat.app"), ("notification_alert", "com.chat.app")])

    def test_a_watch_switched_off_says_so_and_reads_nothing(self):
        seen = device_notifications.watch(PIXEL).stop()
        self.assertEqual((seen["watched"], seen["presented"]), (False, 0))
        self.assertIn("off", seen["detail"])

    def test_an_adb_log_watch_uses_the_device_s_serial_and_cleans_up_its_relay(self):
        with tempfile.TemporaryDirectory() as folder:
            fake_adb = Path(folder) / "adb"
            alert = self.EVENTS.splitlines()[2]
            host_isolation.executable(fake_adb, "#!/bin/sh\n"
                                      f'[ "$*" = "-s {PIXEL} logcat -b events -v epoch -T 1" ] || exit 2\n'
                                      f"printf '%s\\n' '{alert}'\n"
                                      "exec /bin/sleep 60\n")
            with patch.dict(os.environ, {device_notifications.WATCH: "on"}), \
                    patch.object(sdk, "tool", return_value=str(fake_adb)):
                watch = android_platform.PLATFORM.watch(PIXEL)
                seen = watch.stop()
            self.assertEqual(seen, {"watched": True, "transport": "adb", "presented": 1,
                                    "first": "notification_alert from com.chat.app"})
            self.assertIsNotNone(watch.process.poll())
            self.assertFalse(Path(watch.path).exists())


class Settings(unittest.TestCase):
    def test_the_android_key_takes_a_gradle_project_inside_the_checkout(self):
        settings.android({"directory": "android", "module": "feature:reader", "variant": "freeDebug",
                          "gradleArguments": ["--offline"]})
        for wrong in ({"directory": "../x"}, {"module": ":app"}, {"variant": "Debug"}, {"gradleArguments": "x"},
                      {"other": 1}, []):
            with self.subTest(wrong=wrong), self.assertRaises(ValueError): settings.android(wrong)
        self.assertEqual(settings.configured({"android": {"module": "x"}})["variant"], "debug")

    def test_the_pool_s_name_is_a_plain_name(self):
        settings.emulator_pool("Pagepour")
        for wrong in ("", " x", "a/b", 3):
            with self.subTest(wrong=wrong), self.assertRaises(ValueError): settings.emulator_pool(wrong)


if __name__ == "__main__":
    unittest.main()
