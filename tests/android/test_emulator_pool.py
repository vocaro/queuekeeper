"""The emulator pool's AVDs, copied by APFS clone and renamed, and its members taken and released under reservations
(queuekeeper_android.emulators), against an AVD folder of the test's own and an emulator that is faked."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
import os
from pathlib import Path
import socket
from subprocess import CompletedProcess
import tempfile
from threading import Thread
import time
from types import SimpleNamespace
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


class Console(unittest.TestCase):
    def test_a_loopback_connection_must_belong_to_the_recorded_pid(self):
        home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (home / ".emulator_console_auth_token").write_text("stand-in-token\n")
        self.enterContext(patch.object(avd.Path, "home", return_value=home))
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            listener.settimeout(5)
            commands = []

            def serve():
                for _ in range(2):
                    peer, _ = listener.accept()
                    with peer, peer.makefile("rb") as stream:
                        peer.settimeout(5)
                        peer.sendall(b"Android Console: Authentication required\r\nOK\r\n")
                        commands.append(stream.readline())
                        if commands[-1]:
                            peer.sendall(b"OK\r\n")
                            commands.append(stream.readline())
                            peer.sendall(b"Demo_Pixel_8_API_36_1\r\nOK\r\n")

            server = Thread(target=serve)
            server.start()
            try:
                port = listener.getsockname()[1]
                pid = os.getpid()
                with avd.owned_console(port, {"pid": pid, "start": emulator_pool.process_start(pid)}, 3) as (peer, stream):
                    peer.sendall(b"avd name\n")
                    self.assertEqual(avd.console_reply(stream), [b"Demo_Pixel_8_API_36_1"])
                parent = os.getppid()
                with self.assertRaisesRegex(adb.Failed, "could not be tied to the recorded emulator process"):
                    with avd.owned_console(port, {"pid": parent, "start": emulator_pool.process_start(parent)}, 3):
                        self.fail("a console connected to another process was accepted")
            finally:
                server.join(12)
        self.assertEqual(commands, [b"auth stand-in-token\n", b"avd name\n", b""])


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

    def test_a_number_reserved_between_selection_and_claim_is_skipped(self):
        template = self.template()
        occupied, contested, released = (f"Demo_Pixel_8_API_36_{number}" for number in (1, 2, 3))
        make_avd(self.folder, occupied)
        self.running["emulator-5554"] = occupied
        deadline = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        old = self.record.reserve(released, owner="earlier", revision=0, device=f"avd:{released}",
                                  purpose="Earlier tests", release_by=deadline)
        self.record.release(released, owner="earlier", revision=old["revision"], outcome="done", restoration="checked")
        reserve = self.record.reserve
        winning = []

        def claim(key, **options):
            if key == contested:
                # The other claimant commits its reservation after this caller reads revision zero.
                winning.append(reserve(key, **{**options, "owner": "session-b"}))
            return reserve(key, **options)

        with patch.object(self.record, "reserve", side_effect=claim) as tried, \
                patch.object(avd, "clone", wraps=avd.clone) as clone, patch.object(avd, "delete") as delete:
            lease = self.pool.take(IMAGE, "pixel_8", purpose="Tests")

        self.assertEqual([call.args[0] for call in tried.call_args_list], [contested, released])
        self.assertEqual(self.record.entry(contested), winning[0])
        self.assertEqual(lease.key, released)
        self.assertEqual(self.record.entry(released)["ownerChatID"], "session-a")
        clone.assert_called_once_with(template, released, folder=self.folder)
        delete.assert_not_called()

    def test_a_member_whose_emulator_does_not_stop_stays_reserved_for_recovery(self):
        lease = self.pool.take(IMAGE, "pixel_8", purpose="first")
        with patch.object(avd, "stop", return_value=False):
            self.assertFalse(self.pool.release(lease, outcome="done"))
        self.assertEqual(devices.condition(self.record.entry(lease.key)), "held")

    def test_a_release_of_a_changed_reservation_keeps_it_and_leaves_the_emulator_running(self):
        lease = self.pool.take(IMAGE, "pixel_8", purpose="first")
        other = emulator_pool.Pool("Demo", "session-b", record=self.record, folder=self.folder,
                                   said=lambda text: None)
        with patch.object(avd, "stop") as stop:
            self.assertFalse(other.release(lease, outcome="done"))
        stop.assert_not_called()
        self.assertEqual(self.running.get(lease.serial), lease.key)
        self.assertEqual(devices.condition(self.record.entry(lease.key)), "held")
        self.assertTrue(self.pool.release(lease, outcome="done"))
        self.assertNotIn(lease.serial, self.running)
        with patch.object(avd, "stop") as stop:
            self.assertFalse(self.pool.release(lease, outcome="again"))
        stop.assert_not_called()

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


class Reaping(unittest.TestCase):
    KEY, SERIAL = "Demo_Pixel_8_API_36_1", "emulator-5554"
    OWNER, HOLDER, EMULATOR = "old-session", 402, 403

    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory(
            dir=os.environ["QUEUEKEEPER_DEVICE_RECORD"].rsplit("/devices/", 1)[0])))
        self.folder = root / "avd"
        make_avd(self.folder, self.KEY)
        self.record = devices.Record(root / "record.json")
        self.pool = emulator_pool.Pool("Demo", None, record=self.record, folder=self.folder)
        self.start = " ".join(time.ctime(time.time() - 3 * 3600).split())
        self.running, self.answer, self.killed, self.ports_closed = True, self.KEY, [], False
        self.other = []
        self.entry = self.record.reserve(self.KEY, owner=self.OWNER, revision=0, purpose="instrumentation",
                                         device=f"avd:{self.KEY}", release_by=self.due(2),
                                         details={devices.HOLDER: {"pid": self.HOLDER, "start": self.start}})
        self.update(expectedReleaseBy=self.due(-1), serial=self.SERIAL, port=5554,
                    emulatorProcess={"pid": self.EMULATOR, "start": self.start})
        self.enterContext(patch.object(emulator_pool.offload_host, "listing", side_effect=self.listing))
        self.enterContext(patch.object(emulator_pool.offload_host, "census", side_effect=self.census))
        self.enterContext(patch.object(avd, "ports_stopped", side_effect=lambda _port: self.ports_closed))
        self.enterContext(patch.object(avd, "owned_console", side_effect=self.owned_console))
        self.enterContext(patch.object(adb, "run", side_effect=self.adb_run))

    def due(self, hours):
        return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()

    def update(self, **changes):
        self.entry = self.record.update(self.KEY, owner=self.OWNER, revision=self.entry["revision"], **changes)

    def listing(self, command, timeout):
        self.assertEqual(command, list(emulator_pool.offload_host.ENVIRONMENTS))
        rows = [(os.getpid(), os.getppid(), "python qk emulator-pool reap PATH=/bin"), *self.other]
        if self.running: rows.append((self.EMULATOR, 1, f"qemu -avd {self.KEY} -port 5554"))
        return "\n".join(f"{pid} {parent} {os.getuid()} {self.start} {command}" for pid, parent, command in rows)

    def census(self, pids):
        self.assertEqual(pids, [self.EMULATOR])
        return {self.EMULATOR: self.start} if self.running else {}

    @contextmanager
    def owned_console(self, port, emulator, timeout):
        self.assertEqual((port, emulator["pid"]), (5554, self.EMULATOR))
        sent = []

        def send(command):
            sent.append(command)
            if command == b"kill\n":
                self.assertEqual(sent, [b"avd name\n", b"kill\n"])
                self.killed.append(self.SERIAL)
                self.running, self.ports_closed = False, True

        yield SimpleNamespace(sendall=send), BytesIO(f"{self.answer}\nOK\n".encode())

    def adb_run(self, arguments, *, timeout):
        self.assertEqual(arguments, ["devices", "-l"])
        return CompletedProcess(arguments, 0, stdout="List of devices attached\n"
                                + (f"{self.SERIAL}\tdevice\n" if self.running else ""))

    def test_a_dry_run_only_reports_then_reap_stops_and_recovers_the_expired_member(self):
        self.assertEqual(self.pool.reap(dry_run=True), [(f"{self.KEY}: would stop and release", False)])
        self.assertEqual(self.killed, [])
        self.assertEqual(self.record.entry(self.KEY), self.entry)
        self.assertEqual(self.pool.reap(), [(f"{self.KEY}: released after confirming its emulator stopped", False)])
        self.assertEqual(self.killed, [self.SERIAL])
        recovered = self.record.entry(self.KEY)
        self.assertEqual((recovered["state"], recovered["recovery"]["by"], recovered["lastOwnerChatID"]),
                         ("released", "emulator-pool reap", self.OWNER))
        self.assertEqual(self.pool.reap(), [])

    def test_an_unexpired_reservation_live_process_or_reused_serial_keeps_the_emulator(self):
        self.update(expectedReleaseBy=self.due(1))
        self.assertIn("has not passed", self.pool.reap()[0][0])
        self.update(expectedReleaseBy=self.due(-1))
        self.other = [(self.HOLDER, 1, "a live holder")]
        self.assertIn("holder processes is still running", self.pool.reap()[0][0])
        self.other = [(900, 1, f"adb -s {self.SERIAL} logcat")]
        self.assertIn("PID 900 still names", self.pool.reap()[0][0])
        self.other, self.answer = [], "Someone_elses_emulator"
        self.assertIn("does not answer for", self.pool.reap()[0][0])
        self.assertEqual(self.killed, [])
        self.assertEqual(self.record.entry(self.KEY), self.entry)

    def test_an_uncertain_census_or_failed_stop_leaves_the_reservation_held(self):
        with patch.object(emulator_pool.offload_host, "listing", return_value=None):
            self.assertIn("process census could not verify", self.pool.reap()[0][0])
        with patch.object(self.pool, "_reap_stopped", return_value=False), \
                patch.object(emulator_pool, "REAP_STOP_SECONDS", 0):
            line, failed = self.pool.reap()[0]
        self.assertTrue(failed)
        self.assertIn("did not confirm its stop", line)
        self.assertEqual(self.record.entry(self.KEY), self.entry)

    def test_a_port_claimed_in_another_record_is_left_alone(self):
        self.record.reserve("Another_1", owner="other-session", revision=0, purpose="instrumentation",
                            device="avd:Another_1", release_by=self.due(1), details={"port": 5554})
        self.assertIn("reservation Another_1 also holds its AVD or port", self.pool.reap()[0][0])
        self.assertEqual(self.killed, [])
        self.assertEqual(self.record.entry(self.KEY), self.entry)

    def test_a_retry_recovers_a_member_that_already_stopped_without_sending_another_stop(self):
        self.running, self.ports_closed = False, True
        self.assertIn("released after confirming", self.pool.reap()[0][0])
        self.assertEqual(self.killed, [])

    def test_a_renewed_reservation_moves_the_revision_before_any_stop(self):
        stop = self.pool._reap_stop

        def renew(key, old_entry):
            self.update(expectedReleaseBy=self.due(1))
            return stop(key, old_entry)

        with patch.object(self.pool, "_reap_stop", side_effect=renew):
            self.assertIn("reservation changed", self.pool.reap()[0][0])
        self.assertEqual(self.killed, [])
        self.assertEqual(self.record.entry(self.KEY), self.entry)


if __name__ == "__main__":
    unittest.main()
