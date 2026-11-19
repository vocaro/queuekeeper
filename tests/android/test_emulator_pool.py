"""The emulator pool's AVDs, copied by APFS clone and renamed, and its members taken and released under reservations
(queuekeeper_android.emulators), against an AVD folder of the test's own and an emulator that is faked."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import host_isolation  # noqa: F401
from queuekeeper_android import adb
from queuekeeper_android.emulators import avd, pool as emulator_pool
from queuekeeper_mobile import devices

IMAGE = "system-images;android-36;google_apis;arm64-v8a"


def make_avd(folder, name, image="system-images/android-36/google_apis/arm64-v8a/", device="pixel_8"):
    path = Path(folder) / f"{name}.avd"
    path.mkdir(parents=True)
    avd.write_ini(path / "config.ini", {"AvdId": name, "avd.ini.displayname": name, "image.sysdir.1": image,
                                        "hw.device.name": device})
    (path / "userdata-qemu.img").write_bytes(b"data")
    avd.write_ini(Path(folder) / f"{name}.ini", {"avd.ini.encoding": "UTF-8", "path": str(path),
                                                  "path.rel": f"avd/{name}.avd", "target": "android-36"})
    return path


class Files(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)

    def test_a_clone_is_named_anew_inside_and_out_and_leaves_the_original_as_it_was(self):
        make_avd(self.folder, "T_template")
        (self.folder / "T_template.avd" / "hardware-qemu.ini.lock").mkdir()
        avd.clone("T_template", "T_1", folder=self.folder)
        listed = avd.listed(self.folder)
        self.assertEqual(set(listed), {"T_template", "T_1"})
        self.assertEqual(listed["T_1"]["config"]["AvdId"], "T_1")
        self.assertEqual(listed["T_1"]["config"]["avd.ini.displayname"], "T_1")
        self.assertEqual(avd.ini(self.folder / "T_1.ini")["path"], str(self.folder / "T_1.avd"))
        self.assertEqual(listed["T_template"]["config"]["AvdId"], "T_template")
        self.assertFalse(list((self.folder / "T_1.avd").glob("*.lock")))
        self.assertEqual((self.folder / "T_1.avd" / "userdata-qemu.img").read_bytes(), b"data")
        with self.assertRaises(FileExistsError): avd.clone("T_template", "T_1", folder=self.folder)

    def test_wiping_deletes_what_runs_wrote_and_deleting_takes_both_files(self):
        path = make_avd(self.folder, "T_1")
        (path / "snapshots").mkdir()
        avd.wipe("T_1", folder=self.folder)
        self.assertFalse((path / "userdata-qemu.img").exists())
        self.assertFalse((path / "snapshots").exists())
        avd.delete("T_1", folder=self.folder)
        self.assertEqual(avd.listed(self.folder), {})

    def test_the_avd_folder_follows_the_sdk_s_variables(self):
        self.assertEqual(avd.home({"ANDROID_AVD_HOME": "/a"}), Path("/a"))
        self.assertEqual(avd.home({"ANDROID_USER_HOME": "/u"}), Path("/u/avd"))
        self.assertEqual(avd.image_package("system-images/android-36/google_apis/arm64-v8a/"), IMAGE)


class Names(unittest.TestCase):
    def test_a_kind_s_label_names_its_profile_and_api_and_any_tag_but_google_apis(self):
        self.assertEqual(emulator_pool.label(IMAGE, "pixel_8"), "Pixel_8_API_36")
        self.assertEqual(emulator_pool.label("system-images;android-35;google_apis_playstore;arm64-v8a",
                                             "pixel_tablet"), "Pixel_Tablet_API_35_playstore")
        self.assertEqual(emulator_pool.word("Page pour/1"), "Page_pour_1")


class Taking(unittest.TestCase):
    """take and release, with the emulator's start, boot and stop faked and the AVD folder the test's own."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ["QUEUEKEEPER_DEVICE_RECORD"].rsplit("/devices/", 1)[0])
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.folder = root / "avd"
        self.record = devices.Record(root / "record.json")
        self.pool = emulator_pool.Pool("Demo", "session-a", record=self.record, folder=self.folder, logs=root / "logs",
                                       said=lambda text: None)
        self.running, self.boots, self.port = {}, [], 5554

        class Process:
            pid = os.getpid()
            def poll(self): return None

        def start(name, port, **_options):
            self.running[f"emulator-{port}"] = name
            return Process()
        def stop(serial, process=None, timeout=60):
            self.running.pop(serial, None)
            return True
        def created(name, image, device, folder=None):
            make_avd(folder, name)
            return avd.listed(folder)[name]
        def booted(serial, timeout):
            self.boots.append(serial)
            return True
        def free_port(used=()):
            port = next(port for port in avd.PORTS if f"emulator-{port}" not in self.running)
            return port
        for target, name, value in ((avd, "start", start), (avd, "stop", stop), (avd, "create", created),
                                    (avd, "running", lambda: dict(self.running)), (avd, "free_port", free_port),
                                    (avd, "listening", lambda port, process=None, timeout=60: True),
                                    (avd, "name_of", lambda serial: self.running.get(serial)),
                                    (adb, "wait_booted", booted), (adb, "shell", lambda *words, **options: "")):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_take_warms_the_template_once_then_clones_members_and_each_runs_on_its_own_port(self):
        first = self.pool.take(IMAGE, "pixel_8", purpose="test one")
        second = self.pool.take(IMAGE, "pixel_8", purpose="test two")
        self.assertEqual((first.key, second.key), ("Demo_Pixel_8_API_36_1", "Demo_Pixel_8_API_36_2"))
        self.assertNotEqual(first.serial, second.serial)
        template = avd.listed(self.folder)["Demo_Pixel_8_API_36_template"]
        self.assertTrue((Path(template["path"]) / emulator_pool.WARMED).exists())
        entry = self.record.entry(first.key)
        self.assertEqual((entry["device"], entry["serial"], entry["ownerChatID"]),
                         ("avd:Demo_Pixel_8_API_36_1", first.serial, "session-a"))
        self.assertEqual(devices.condition(self.record.entry("Demo_Pixel_8_API_36_template")), "released")

    def test_a_released_member_is_taken_again_and_reset_from_the_template(self):
        lease = self.pool.take(IMAGE, "pixel_8", purpose="first")
        member = Path(avd.listed(self.folder)[lease.key]["path"])
        (member / "left-by-the-run").write_text("x")
        self.assertTrue(self.pool.release(lease, outcome="done"))
        self.assertEqual(devices.condition(self.record.entry(lease.key)), "released")
        again = self.pool.take(IMAGE, "pixel_8", purpose="second")
        self.assertEqual(again.key, lease.key)
        self.assertFalse((member / "left-by-the-run").exists())

    def test_a_member_whose_emulator_does_not_stop_stays_reserved_for_recovery(self):
        lease = self.pool.take(IMAGE, "pixel_8", purpose="first")
        with patch.object(avd, "stop", return_value=False):
            self.assertFalse(self.pool.release(lease, outcome="done"))
        self.assertEqual(devices.condition(self.record.entry(lease.key)), "held")

    def test_a_member_that_does_not_boot_is_released_and_another_tried_once(self):
        # The first member's emulator never finishes booting, however long it is waited for.
        with patch.object(adb, "wait_booted", lambda serial, timeout: not self.running[serial].endswith("_1")), \
                patch.object(self.pool, "template", return_value=self.template()):
            lease = self.pool.take(IMAGE, "pixel_8", purpose="flaky", boot_timeout=1)
        self.assertEqual(lease.key, "Demo_Pixel_8_API_36_2")
        self.assertEqual(devices.condition(self.record.entry("Demo_Pixel_8_API_36_1")), "released")

    def test_an_emulator_that_answers_as_another_member_is_not_taken_for_this_one(self):
        with patch.object(avd, "name_of", lambda serial: "Someone_else_1" if self.running[serial].endswith("_1")
                          else self.running[serial]), patch.object(self.pool, "template", return_value=self.template()):
            lease = self.pool.take(IMAGE, "pixel_8", purpose="crowded")
        self.assertEqual(lease.key, "Demo_Pixel_8_API_36_2")

    def test_a_port_another_held_member_records_is_not_chosen(self):
        chosen = []
        def free_port(used=()):
            chosen.append(set(used))
            return 5560
        first = self.pool.take(IMAGE, "pixel_8", purpose="a")
        with patch.object(avd, "free_port", free_port):
            self.pool.take(IMAGE, "pixel_8", purpose="b")
        self.assertIn(first.port, chosen[-1])

    def test_release_mine_releases_every_member_the_owner_holds(self):
        self.pool.take(IMAGE, "pixel_8", purpose="a")
        self.pool.take(IMAGE, "pixel_8", purpose="b")
        self.assertEqual(self.pool.release_mine("closeout"), [("Demo_Pixel_8_API_36_1", True),
                                                              ("Demo_Pixel_8_API_36_2", True)])
        self.assertEqual(self.running, {})

    def template(self):
        name = "Demo_Pixel_8_API_36_template"
        path = make_avd(self.folder, name)
        (path / emulator_pool.WARMED).write_text(json.dumps({}))
        return name


if __name__ == "__main__":
    unittest.main()
