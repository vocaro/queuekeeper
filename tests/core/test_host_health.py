"""A Mac short of memory is named in qk doctor, with what holds it and what the owner can do (host_health)."""
import host_isolation  # noqa: F401 -- first, as every test module
from test_support import farm as farm_fixture
import contextlib
import math
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from queuekeeper import cli, host_health, merge_queue, offload_host, remote

GB = 1024 ** 3
DAY = 86400


def reading(swap=(0, 0), memory=64 * GB, largest=(), level=1):
    return {"memoryBytes": memory, "swapUsedBytes": swap[0], "swapTotalBytes": swap[1], "memoryLevel": level,
            "largest": [{"pid": pid, "name": name, "residentBytes": size, "seconds": seconds}
                        for pid, name, size, seconds in largest]}


# Another Mac on 2026-11-15. Its memory pressure level was not recorded that day: warning is assumed here.
SWAPPING = reading((int(31.7 * GB), 32 * GB), largest=[(312, "fseventsd", 34 * GB, 15 * DAY + 2 * 3600),
                                                        (901, "Xcode", 4 * GB, 3600), (77, "WindowServer", GB, 15 * DAY)],
                   level=2)


class Warnings(unittest.TestCase):
    def test_a_healthy_mac_has_no_warning(self):
        self.assertEqual(host_health.warnings(reading((GB, 32 * GB), largest=[(1, "Xcode", 8 * GB, 60)]), "this Mac"), [])

    def test_swap_near_its_total_is_no_warning_while_memory_pressure_is_normal(self):
        # This Mac on 2026-11-15, with 45% of memory free: macOS sizes swap to what it uses, so used sits near the
        # total on a healthy Mac (vocaro/queuekeeper#168).
        healthy = reading((int(5.7 * GB), 7 * GB), memory=36 * GB, largest=[(5943, "Claude Helper (Renderer)", GB, DAY)])
        self.assertEqual(host_health.warnings(healthy, "this Mac"), [])
        self.assertEqual(host_health.warnings(reading((32 * GB, 32 * GB)), "build"), [])

    def test_swap_says_nothing_where_the_pressure_level_is_unread(self):
        for unread in (None, True, "2", 3):
            self.assertEqual(host_health.warnings(reading((32 * GB, 32 * GB), level=unread), "build"), [])
        older = reading((32 * GB, 32 * GB))
        del older["memoryLevel"]
        self.assertEqual(host_health.warnings(older, "build"), [])

    def test_an_unread_reading_is_no_warning(self):
        for unread in (None, "nonsense", {}, reading((None, None), memory=None, largest=[])):
            self.assertEqual(host_health.warnings(unread, "build"), [])

    def test_full_swap_and_an_outsized_daemon_name_the_largest_processes_and_the_safe_restart(self):
        [found] = host_health.warnings(SWAPPING, "build")
        self.assertIn("build is short of memory", found)
        self.assertIn("swap is 31.7 GB of 32.0 GB used (99%) while macOS reports memory pressure at level 2 (warning)",
                      found)
        self.assertIn("fseventsd holds 34.0 GB of its 64.0 GB of memory (53%)", found)
        self.assertIn("fseventsd (PID 312, up 15d 2h, 34.0 GB), Xcode (PID 901, up 1h, 4.0 GB), "
                      "WindowServer (PID 77, up 15d, 1.0 GB)", found)
        self.assertIn("`sudo killall fseventsd` on build, which is safe", found)
        self.assertIn("Queuekeeper stops nothing itself", found)
        self.assertNotIn("quit", found)

    def test_an_outsized_app_is_to_be_quit_or_the_mac_restarted(self):
        [found] = host_health.warnings(reading(largest=[(4242, "LeakyApp", 20 * GB, 2 * DAY)]), "this Mac")
        self.assertIn("LeakyApp holds 20.0 GB", found)
        self.assertNotIn("swap is", found)
        self.assertIn("quit LeakyApp (PID 4242) on this Mac, or restart this Mac", found)
        self.assertNotIn("killall", found)

    def test_with_swap_alone_full_the_largest_process_is_the_one_to_free(self):
        [found] = host_health.warnings(reading((27 * GB, 32 * GB), largest=[(9, "Simulator", 6 * GB, 600),
                                                                           (8, "mds_stores", 10 * GB, DAY)], level=4),
                                       "build")
        self.assertIn("swap is 27.0 GB of 32.0 GB used (84%) while macOS reports memory pressure at level 4 (critical)",
                      found)
        self.assertLess(found.index("mds_stores (PID 8"), found.index("Simulator (PID 9"))
        self.assertIn("`sudo killall mds_stores` on build", found)
        self.assertNotIn("quit Simulator", found)

    def test_the_thresholds_are_the_ones_it_names(self):
        self.assertEqual(host_health.warnings(reading((int(0.79 * 32 * GB), 32 * GB), level=4), "x"), [])
        nearly_full = (math.ceil(host_health.SWAP_SHARE * 32 * GB), 32 * GB)
        self.assertEqual(host_health.warnings(reading(nearly_full, level=1), "x"), [])
        for level in host_health.PRESSED:
            self.assertTrue(host_health.warnings(reading(nearly_full, level=level), "x"))
        self.assertEqual(sorted(host_health.PRESSED), [2, 4])
        self.assertEqual(host_health.warnings(reading(largest=[(1, "a", 16 * GB, 1)]), "x"), [])
        self.assertTrue(host_health.warnings(reading(largest=[(1, "a", 16 * GB + 1, 1)]), "x"))


class Reading(unittest.TestCase):
    def test_swap_usage_reads_vm_swapusage(self):
        self.assertEqual(offload_host.swap_usage("total = 32768.00M  used = 31744.00M  free = 1024.00M  (encrypted)"),
                         (31744 * 1024 ** 2, 32768 * 1024 ** 2))
        self.assertEqual(offload_host.swap_usage("total = 0.00M  used = 0.00M  free = 0.00M"), (0, 0))
        self.assertIsNone(offload_host.swap_usage(None))
        self.assertIsNone(offload_host.swap_usage("nonsense"))

    def test_host_health_lists_the_largest_processes_without_their_arguments(self):
        answers = {("sysctl", "-n", "hw.memsize"): str(64 * GB),
                   ("/usr/sbin/sysctl", "-n", "kern.memorystatus_vm_pressure_level"): "2",
                   ("sysctl", "-n", "vm.swapusage"): "total = 32768.00M  used = 31744.00M  free = 1024.00M",
                   offload_host.HEALTH_LISTING: "\n".join([
                       "  312 35651584 15-02:00:00 /System/Library/Frameworks/CoreServices.framework/fseventsd",
                       "  901  4194304    01:00:00 /Applications/Xcode.app/Contents/MacOS/Xcode",
                       "   77     1024       00:05 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                       "  bad line"])}
        asked = []
        with patch.object(offload_host, "output", lambda command, *_: asked.append(tuple(command)) or answers[tuple(command)]):
            found = offload_host.host_health()
        self.assertNotIn("args", " ".join(offload_host.HEALTH_LISTING))
        self.assertEqual((found["memoryBytes"], found["swapUsedBytes"]), (64 * GB, 31744 * 1024 ** 2))
        self.assertEqual(found["memoryLevel"], 2)
        self.assertEqual(found["largest"][0], {"pid": 312, "name": "fseventsd", "residentBytes": 34 * GB,
                                               "seconds": 15 * DAY + 2 * 3600})
        self.assertEqual([one["name"] for one in found["largest"]], ["fseventsd", "Xcode", "Google Chrome"])

    def test_a_listing_that_fails_leaves_the_processes_unread(self):
        with patch.object(offload_host, "output", return_value=None):
            self.assertEqual(offload_host.host_health(), {"memoryBytes": None, "swapUsedBytes": None,
                                                          "swapTotalBytes": None, "memoryLevel": None,
                                                          "largest": None})

    def test_the_farm_asks_for_health_beside_pressure_and_the_merge_queue_does_not(self):
        with patch.object(offload_host, "pressure", return_value={"load": 1.0}), \
                patch.object(offload_host, "host_health", return_value=SWAPPING):
            self.assertEqual(offload_host.answer({"pressure": True}), {"load": 1.0})
            self.assertEqual(offload_host.answer({"pressure": True, "health": True}), {"load": 1.0, "health": SWAPPING})


class Doctor(unittest.TestCase):
    def doctor(self, hosts, here):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Example",
                "integration": "direct-main", "lanes": {"gate": {"command": ["true"], "timeout": 60}}}))
            with patch.object(cli.shutil, "which", return_value="/usr/bin/true"), \
                    patch.object(cli, "source_identity", return_value={}), \
                    patch.object(remote, "compare_hosts", return_value=hosts), \
                    patch.object(offload_host, "host_health", return_value=here), \
                    contextlib.redirect_stdout(StringIO()) as output:
                cli.main(["--project", directory, "doctor"])
        return json.loads(output.getvalue())

    def test_doctor_warns_of_each_mac_short_of_memory(self):
        hosts = {"host": "build", "errors": [], "unchecked": [], "pythonCrashes": {},
                 "remote": {"hostHealth": SWAPPING}}
        found = [w for w in self.doctor(hosts, reading(largest=[(4242, "LeakyApp", 20 * GB, DAY)]))["warnings"]
                 if "short of memory" in w]
        self.assertEqual(len(found), 2, found)
        self.assertTrue(found[0].startswith("this Mac is short of memory"))
        self.assertIn("quit LeakyApp", found[0])
        self.assertTrue(found[1].startswith("build is short of memory"))
        self.assertIn("`sudo killall fseventsd` on build", found[1])

    def test_a_farm_mac_whose_toolkit_does_not_read_health_is_unchecked(self):
        hosts = {"host": "build", "errors": [], "unchecked": [], "pythonCrashes": {}, "remote": {}}
        shown = self.doctor(hosts, reading())
        self.assertFalse([w for w in shown["warnings"] if "short of memory" in w])
        self.assertTrue(any("whether build is short of memory" in item for item in shown["farm"][0]["unchecked"]))



class QueueDoctor(unittest.TestCase):
    def test_queue_doctor_finds_each_mac_short_of_memory_and_notes_one_it_cannot_read(self):
        asked = []
        def read(host):
            asked.append(host)
            return SWAPPING if host == "build" else reading()
        with farm_fixture.only("build"):
            findings, notes = merge_queue.memory_findings(read)
        self.assertEqual(asked, [None, "build"])
        self.assertEqual(notes, [])
        [found] = findings
        self.assertEqual((found["signal"], found["mac"]), ("short of memory", "build"))
        self.assertIn("swap is 31.7 GB of 32.0 GB used", found["diagnosis"])
        self.assertIn("`sudo killall fseventsd` on build, which is safe", found["remedy"])
        self.assertEqual([one["pid"] for one in found["largest"]], [312, 901, 77])
        with farm_fixture.only("build"):
            findings, notes = merge_queue.memory_findings(lambda host: None if host else reading())
        self.assertEqual(findings, [])
        self.assertEqual(notes, ["whether build is short of memory is unknown, as its processes "
                                 "could not be read there"])
        # Every Mac of the farm that takes work now is read, in the farm's order.
        asked.clear()
        with farm_fixture.only("build", farm_fixture.mac("loaned", state="paused"), "mini"):
            findings, notes = merge_queue.memory_findings(read)
        self.assertEqual((asked, [found["mac"] for found in findings]), ([None, "build", "mini"], ["build"]))

    def test_doctor_compares_each_of_the_farm_s_macs_and_fails_when_any_differs(self):
        reports = {"build": {"host": "build", "errors": [], "unchecked": [], "pythonCrashes": {},
                             "remote": {"hostHealth": reading()}},
                   "mini": {"host": "mini", "errors": ["the default Xcodes differ"], "unchecked": [],
                            "pythonCrashes": {}, "remote": {"hostHealth": SWAPPING}}}
        with tempfile.TemporaryDirectory() as directory, farm_fixture.only("build", "mini"), \
                patch.object(remote, "compare_hosts", side_effect=lambda host=None: reports[host]) as compared, \
                patch.object(cli.shutil, "which", return_value="/usr/bin/true"), \
                patch.object(cli, "source_identity", return_value={}), \
                patch.object(offload_host, "host_health", return_value=reading()), \
                contextlib.redirect_stdout(StringIO()) as output:
            Path(directory, "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Example",
                "integration": "direct-main", "lanes": {"gate": {"command": ["true"], "timeout": 60}}}))
            status = cli.main(["--project", directory, "doctor"])
        shown = json.loads(output.getvalue())
        self.assertEqual(([call.args for call in compared.call_args_list], status), ([("build",), ("mini",)], 1))
        self.assertEqual([one["host"] for one in shown["farm"]], ["build", "mini"])
        self.assertNotIn("offload", shown)
        self.assertTrue(any(warning.startswith("mini is short of memory") for warning in shown["warnings"]))

    def test_a_plugin_s_doctor_check_fails_doctor_by_its_errors_and_warns_by_its_warnings(self):
        # Such as the Apple plugin's declared devices, one declared always reachable that no Mac reaches
        # (vocaro/queuekeeper#211).
        report = {"host": None, "errors": [], "unchecked": [], "pythonCrashes": {}}
        for found, expected in (((["iPad is reached by no Mac"], ["careful"]), 1), (([], ["careful"]), 0)):
            with self.subTest(found=found), tempfile.TemporaryDirectory() as directory, farm_fixture.only(), \
                    patch.object(remote, "compare_hosts", return_value=report), \
                    patch.object(cli.shutil, "which", return_value="/usr/bin/true"), \
                    patch.object(cli, "source_identity", return_value={}), \
                    patch.object(offload_host, "host_health", return_value=reading()), \
                    patch.object(cli.plugins, "DOCTOR_CHECKS", [lambda root, config, found=found: found]), \
                    contextlib.redirect_stdout(StringIO()) as output:
                Path(directory, "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Example",
                    "integration": "direct-main", "lanes": {"gate": {"command": ["true"], "timeout": 60}}}))
                status = cli.main(["--project", directory, "doctor"])
            shown = json.loads(output.getvalue())
            self.assertEqual((status, shown["errors"]), (expected, found[0]))
            self.assertIn("careful", shown["warnings"])

    def test_a_farm_mac_without_the_reading_is_unknown_and_one_away_is_too(self):
        with patch.object(remote, "reading_there", return_value=({"load": 1.0}, 10.0)) as there:
            self.assertIsNone(merge_queue.health_of("build"))
        there.assert_called_once_with("build", health=True)
        with patch.object(remote, "reading_there", side_effect=remote.Unavailable("build is asleep")):
            self.assertIsNone(merge_queue.health_of("build"))

    def test_queue_doctor_reports_the_finding_and_changes_nothing(self):
        finding = {"signal": "short of memory", "diagnosis": "this Mac is short of memory", "remedy": "quit it",
                   "mac": "this Mac", "largest": []}
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(merge_queue, "diagnose", return_value=([], [], False)), \
                patch.object(merge_queue, "memory_findings", return_value=([finding], [])), \
                contextlib.redirect_stdout(StringIO()) as output:
            self.assertEqual(merge_queue.doctor(Path(directory), as_json=True), 1)
        self.assertEqual(json.loads(output.getvalue())["findings"], [finding])


if __name__ == "__main__":
    unittest.main()
