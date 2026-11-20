"""A lane that fills the disk waits for free space before it starts, and a run that the disk runs short under ends
promptly as unmeasured, the host's fault, rather than hanging or failing the commits (vocaro/queuekeeper#162). No test
fills a disk: each fakes the volume's reading (disk.volume_usage) or the error a full disk gives (errno ENOSPC)."""
import contextlib
import errno
from io import StringIO
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src/python"))
from queuekeeper import cli, config, disk, lane_results, lane_wait, processes, resources
import host_isolation
from host_isolation import stretched

GIB = disk.GIB


def reading(free_gib):
    """A volume's (total, used, free) bytes with free_gib GiB free."""
    return 1000 * GIB, 1000 * GIB - int(free_gib * GIB), int(free_gib * GIB)


class FloorTests(unittest.TestCase):
    def test_a_volume_below_the_floor_is_short_and_says_how_much_it_has_and_the_floor(self):
        with patch.dict(os.environ, {"QUEUEKEEPER_START_FREE_GB": "30"}), \
                patch.object(disk, "volume_usage", return_value=reading(7)):
            short = disk.short_of_space(disk.START_FREE, (tempfile.gettempdir(),))
        self.assertIn("has 7.0 GiB free, below QUEUEKEEPER_START_FREE_GB=30 GiB (set in the environment)", short)
        self.assertIn("qk disk --delete", short)
        # Negative controls: enough space, a floor of 0, and a reading that cannot be taken stop nothing.
        with patch.dict(os.environ, {"QUEUEKEEPER_START_FREE_GB": "30"}), \
                patch.object(disk, "volume_usage", return_value=reading(31)):
            self.assertIsNone(disk.short_of_space(disk.START_FREE, (tempfile.gettempdir(),)))
        with patch.dict(os.environ, {"QUEUEKEEPER_START_FREE_GB": "0"}), \
                patch.object(disk, "volume_usage", return_value=reading(0)):
            self.assertIsNone(disk.short_of_space(disk.START_FREE, (tempfile.gettempdir(),)))
        with patch.dict(os.environ, {"QUEUEKEEPER_START_FREE_GB": "30"}), \
                patch.object(disk, "volume_usage", return_value=None):
            self.assertIsNone(disk.short_of_space(disk.START_FREE, (tempfile.gettempdir(),)))

    def test_each_floor_has_a_default_and_refuses_a_value_that_is_no_number(self):
        with patch.dict(os.environ):
            for variable, _default in (disk.START_FREE, disk.RUN_FREE): os.environ.pop(variable, None)
            self.assertEqual(disk.free_floor(disk.START_FREE), (10.0, "QUEUEKEEPER_START_FREE_GB", "the default"))
            self.assertEqual(disk.free_floor(disk.RUN_FREE), (2.0, "QUEUEKEEPER_RUN_FREE_GB", "the default"))
            for value in ("x", "-1", "nan"):
                with self.subTest(value=value):
                    os.environ["QUEUEKEEPER_RUN_FREE_GB"] = value
                    with self.assertRaisesRegex(ValueError, f"QUEUEKEEPER_RUN_FREE_GB={value} is not a number"):
                        disk.free_floor(disk.RUN_FREE)


    def test_the_environment_overrides_a_configured_floor_which_overrides_the_default(self):
        """vocaro/queuekeeper#288: one host-wide floor of 30 GiB kept a Unity lane that writes little waiting over an
        hour; a project's settings now give its own, which the variable still overrides."""
        configured = (12, "lanes.timed.disk.startFreeGB in queuekeeper.json")
        with patch.dict(os.environ):
            os.environ.pop("QUEUEKEEPER_START_FREE_GB", None)
            self.assertEqual(disk.free_floor(disk.START_FREE, configured),
                             (12.0, "QUEUEKEEPER_START_FREE_GB", configured[1]))
            with patch.object(disk, "volume_usage", return_value=reading(11)):
                short = disk.short_of_space(disk.START_FREE, (tempfile.gettempdir(),), configured)
            self.assertIn("has 11.0 GiB free, below 12 GiB (lanes.timed.disk.startFreeGB in queuekeeper.json; "
                          "QUEUEKEEPER_START_FREE_GB overrides it)", short)
            # Negative controls: the default alone lets 11 GiB through, and so does a configured floor of 0.
            with patch.object(disk, "volume_usage", return_value=reading(11)):
                self.assertIsNone(disk.short_of_space(disk.START_FREE, (tempfile.gettempdir(),)))
                self.assertIsNone(disk.short_of_space(disk.START_FREE, (tempfile.gettempdir(),), (0, "zero")))
            os.environ["QUEUEKEEPER_START_FREE_GB"] = "5"
            self.assertEqual(disk.free_floor(disk.START_FREE, configured),
                             (5.0, "QUEUEKEEPER_START_FREE_GB", "set in the environment"))


class ConfiguredFloorTests(unittest.TestCase):
    """The project's and each lane's "disk": {"startFreeGB": N} in queuekeeper.json (vocaro/queuekeeper#288)."""

    def load(self, **settings):
        source = {"schemaVersion": 1, "name": "Space", "lanes": {"gate": {"command": ["true"]}}, **settings}
        return config.parse(json.dumps(source))

    def test_a_lane_floor_comes_before_the_project_floor_which_comes_before_none(self):
        lanes = {"gate": {"command": ["true"], "wholeHost": True},
                 "timed": {"command": ["true"], "resource": "exclusive", "disk": {"startFreeGB": 12.5}},
                 "both": {"parallel": ["gate", "timed"], "resource": "exclusive",
                          "disk": {"startFreeGB": 0}}}
        loaded = self.load(disk={"startFreeGB": 30}, lanes=lanes)
        self.assertEqual(config.start_free(loaded, "timed"), (12.5, "lanes.timed.disk.startFreeGB in queuekeeper.json"))
        self.assertEqual(config.start_free(loaded, "both"), (0, "lanes.both.disk.startFreeGB in queuekeeper.json"))
        self.assertEqual(config.start_free(loaded, "gate"), (30, "disk.startFreeGB in queuekeeper.json"))
        self.assertIsNone(config.start_free(self.load(lanes=lanes), "gate"))
        self.assertIsNone(config.start_free(self.load(disk={}), "gate"))

    def test_a_floor_that_is_no_number_at_or_above_0_or_an_unknown_key_is_refused(self):
        for value in (-1, "12", True, None):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, r"^disk\.startFreeGB must be a number"):
                    self.load(disk={"startFreeGB": value})
                with self.assertRaisesRegex(ValueError, r"^gate\.disk\.startFreeGB must be a number"):
                    self.load(lanes={"gate": {"command": ["true"], "disk": {"startFreeGB": value}}})
        with self.assertRaisesRegex(ValueError, "disk takes only startFreeGB"): self.load(disk={"runFreeGB": 2})
        with self.assertRaisesRegex(ValueError, "disk takes only startFreeGB"): self.load(disk=12)
        with self.assertRaisesRegex(ValueError, r"^group\.disk takes only startFreeGB"):
            self.load(lanes={"gate": {"command": ["true"]}, "group": {"parallel": ["gate"], "disk": {"free": 1}}})

    def test_doctor_reports_the_floor_in_force_and_where_it_comes_from(self):
        loaded = self.load(disk={"startFreeGB": 30}, lanes={
            "gate": {"command": ["true"], "wholeHost": True},
            "timed": {"command": ["true"], "resource": "exclusive", "disk": {"startFreeGB": 12}},
            "unit": {"command": ["true"]}, "own": {"command": ["true"], "resource": "self"}})
        with patch.dict(os.environ):
            os.environ.pop("QUEUEKEEPER_START_FREE_GB", None)
            self.assertEqual(cli.start_floors(loaded), {
                "gib": 30.0, "from": "disk.startFreeGB in queuekeeper.json",
                "lanes": {"gate": {"gib": 30.0, "from": "disk.startFreeGB in queuekeeper.json"},
                          "timed": {"gib": 12.0, "from": "lanes.timed.disk.startFreeGB in queuekeeper.json"}}})
            self.assertEqual(cli.start_floors(self.load())["gib"], 10.0)
            self.assertEqual(cli.start_floors(self.load())["from"], "the default")
            os.environ["QUEUEKEEPER_START_FREE_GB"] = "4"
            self.assertEqual(cli.start_floors(loaded)["lanes"]["timed"], {"gib": 4.0, "from": "QUEUEKEEPER_START_FREE_GB"})
            os.environ["QUEUEKEEPER_START_FREE_GB"] = "x"
            self.assertIn("is not a number", cli.start_floors(loaded)["error"])


class SupervisionTests(unittest.TestCase):
    def test_a_disk_that_runs_short_stops_the_command_and_the_run_is_out_of_space(self):
        """On 2026-11-14 a gate that ran out of space hung for about 90 minutes instead of ending."""
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stderr(StringIO()) as said, \
                patch.object(processes, "SPACE_POLL", 0):
            began = time.monotonic()
            with self.assertRaises(processes.OutOfSpace) as raised:
                # Fixed: the 60 s sleep outlasts the first reading under any load.
                processes.run([sys.executable, "-c", "import time; time.sleep(60)"], cwd=directory,
                              log_path=Path(directory) / "run.log", kill_after=5, space=lambda: "the volume is full")
            self.assertLess(time.monotonic() - began, 45)
            # A timeout's kind: unmeasured, not failed.
            self.assertIsInstance(raised.exception, processes.TimedOut)
            self.assertIn("the disk ran short of space", str(raised.exception))
            self.assertIn("supervise: out of disk space: the volume is full; sending SIGTERM", said.getvalue())
            self.assertIn("supervise: out of disk space", (Path(directory) / "run.log").read_text())
            # Negative control: a disk with room changes nothing.
            self.assertEqual(processes.run([sys.executable, "-c", "raise SystemExit(4)"], cwd=directory,
                                           log_path=Path(directory) / "other.log", space=lambda: None), 4)

    def test_a_group_found_again_leaves_the_reading_of_space_as_it_was(self):
        """supervise names each process group or session `space` in its loop: a group closed and then found again
        once replaced the reading with a number, and the queue's own gate failed on `'int' object is not callable`."""
        real, calls = processes.snapshot, []
        def flickers():
            calls.append(None)
            found = real()
            # The second sample misses the root, so its group closes; the third finds it again.
            if len(calls) == 2: found = {pid: row for pid, row in found.items() if row[0] != os.getpid()}
            return found
        readings = []
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stderr(StringIO()), \
                patch.object(processes, "SPACE_POLL", 0), patch.object(processes, "snapshot", flickers):
            # Fixed: the root stays long enough for several samples under any load, then exits 4.
            status = processes.supervise([sys.executable, "-c", "import time; time.sleep(1); raise SystemExit(4)"],
                                         cwd=directory, space=lambda: readings.append(None))
        self.assertEqual(status, 4)
        self.assertGreater(len(calls), 3)
        self.assertGreater(len(readings), 2)

    def test_an_admission_whose_record_finds_the_disk_full_stops_its_command(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stderr(StringIO()) as said, \
                patch.object(processes, "SPACE_POLL", 0):
            with resources.Admission("full", timeout=0) as admission:
                self.assertIsNone(admission.short_of_space(directory))
                real = os.open
                def full(path, *arguments, **keywords):
                    if Path(path) == admission.process_record: raise OSError(errno.ENOSPC, "No space left on device")
                    return real(path, *arguments, **keywords)
                with patch.object(resources.os, "open", full), self.assertRaises(processes.OutOfSpace):
                    processes.supervise([sys.executable, "-c", "import time; time.sleep(60)"], cwd=directory, kill_after=5)
            self.assertIn("No space left on device", admission.short_of_space())
            self.assertIn("the process record was not written", said.getvalue())

    def test_supervision_in_an_admission_reads_the_run_floor(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stderr(StringIO()), \
                patch.dict(os.environ, {"QUEUEKEEPER_RUN_FREE_GB": "2"}), patch.object(processes, "SPACE_POLL", 0), \
                patch.object(disk, "volume_usage", return_value=reading(1)):
            with resources.Admission("short", timeout=0), self.assertRaises(processes.OutOfSpace) as raised:
                processes.supervise([sys.executable, "-c", "import time; time.sleep(60)"], cwd=directory, kill_after=5)
            self.assertIn("below QUEUEKEEPER_RUN_FREE_GB=2 GiB", str(raised.exception))


class LaneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def project(self, **lanes):
        (self.root / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Space", "lanes": lanes}))

    def run_lane(self, name):
        with patch.object(cli, "source_identity", return_value={}), contextlib.redirect_stderr(StringIO()) as said:
            status = cli.main(["--project", str(self.root), "run", name])
        return status, said.getvalue()

    def receipt(self, name):
        return json.loads((self.root / ".build/queuekeeper" / name / "source.json").read_text())

    def test_a_lane_that_fills_the_disk_waits_for_free_space_before_admission_and_says_so(self):
        os.environ[lane_wait.VARIABLE] = str(self.root / "wait.json")
        self.addCleanup(os.environ.pop, lane_wait.VARIABLE, None)
        with patch.dict(os.environ, {"QUEUEKEEPER_START_FREE_GB": "30"}), \
                patch.object(disk, "volume_usage", return_value=reading(7)), contextlib.ExitStack() as stack, \
                patch("sys.stderr", new_callable=StringIO) as said:
            status = cli.admitted_here(stack, "gate", "Space gate", 0, whole_host=True,
                                       space=(disk.DATA_VOLUME, str(self.root)))
        self.assertEqual(status, resources.BUSY)
        self.assertIn(f"queuekeeper: gate waits: this Mac ({cli.offload_host.mac_name()}) "
                      f"{lane_results.SHORT_OF_SPACE}: ", said.getvalue())
        self.assertIn("below QUEUEKEEPER_START_FREE_GB=30 GiB", said.getvalue())
        self.assertIn("queuekeeper: gate not run on this Mac", said.getvalue())
        record = lane_wait.read(self.root / "wait.json")
        self.assertEqual(record["kind"], lane_wait.SPACE)
        self.assertTrue(lane_wait.words(record).startswith("free disk space: "))
        # Space that comes within the wait admits the lane.
        readings = iter([reading(7), reading(40)])
        with patch.dict(os.environ, {"QUEUEKEEPER_START_FREE_GB": "30"}), patch.object(cli, "SPACE_POLL", 0.01), \
                patch.object(disk, "volume_usage", side_effect=lambda path: next(readings, reading(40))), \
                contextlib.ExitStack() as stack, patch("sys.stderr", new_callable=StringIO) as said:
            admission = cli.admitted_here(stack, "gate", "Space gate", stretched(30), whole_host=True,
                                          space=(str(self.root),))
            self.assertIsInstance(admission, resources.Admission)
        self.assertIn("gate has the disk space it waited for", said.getvalue())

    def test_a_lane_waits_for_its_configured_floor_and_its_wait_names_it(self):
        os.environ[lane_wait.VARIABLE] = str(self.root / "wait.json")
        self.addCleanup(os.environ.pop, lane_wait.VARIABLE, None)
        floor = (12, "lanes.gate.disk.startFreeGB in queuekeeper.json")
        with patch.dict(os.environ), patch.object(disk, "volume_usage", return_value=reading(11)), \
                contextlib.ExitStack() as stack, patch("sys.stderr", new_callable=StringIO) as said:
            os.environ.pop("QUEUEKEEPER_START_FREE_GB", None)
            status = cli.admitted_here(stack, "gate", "Space gate", 0, whole_host=True, space=(str(self.root),),
                                       floor=floor)
        self.assertEqual(status, resources.BUSY)
        self.assertIn("below 12 GiB (lanes.gate.disk.startFreeGB in queuekeeper.json; QUEUEKEEPER_START_FREE_GB "
                      "overrides it)", said.getvalue())
        self.assertIn("lanes.gate.disk.startFreeGB", lane_wait.words(lane_wait.read(self.root / "wait.json")))
        # Negative control: the same reading under the default floor is admitted.
        with patch.dict(os.environ), patch.object(disk, "volume_usage", return_value=reading(11)), \
                contextlib.ExitStack() as stack, patch("sys.stderr", new_callable=StringIO):
            os.environ.pop("QUEUEKEEPER_START_FREE_GB", None)
            self.assertIsInstance(cli.admitted_here(stack, "gate", "Space gate", stretched(30), whole_host=True,
                                                    space=(str(self.root),)), resources.Admission)

    def test_a_lane_run_reads_its_floor_from_the_project(self):
        marker = self.root / "ran"
        (self.root / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Space",
            "disk": {"startFreeGB": 30}, "lanes": {"gate": {"command": [sys.executable, "-c",
            f"open({str(marker)!r}, 'w')"], "wholeHost": True}}}))
        with patch.dict(os.environ), patch.object(disk, "volume_usage", return_value=reading(20)), \
                patch.object(cli.remote, "OVERFLOW_TIMEOUT", 0):
            os.environ.pop("QUEUEKEEPER_START_FREE_GB", None)
            status, said = self.run_lane("gate")
        self.assertEqual(status, resources.BUSY, said)
        self.assertIn("below 30 GiB (disk.startFreeGB in queuekeeper.json", said)
        self.assertFalse(marker.exists())

    def test_which_lanes_fill_the_disk(self):
        self.assertTrue(cli.fills_disk({"wholeHost": True}, "heavy"))
        self.assertTrue(cli.fills_disk({}, "exclusive"))
        self.assertTrue(cli.fills_disk({"compileFirst": ["make"]}, "heavy"))
        self.assertFalse(cli.fills_disk({}, "heavy"))
        self.assertFalse(cli.fills_disk({}, "light"))

    def test_a_whole_host_lane_run_below_the_start_floor_runs_nothing(self):
        marker = self.root / "ran"
        self.project(gate={"command": [sys.executable, "-c", f"open({str(marker)!r}, 'w')"], "wholeHost": True})
        with patch.dict(os.environ, {"QUEUEKEEPER_START_FREE_GB": "30"}), \
                patch.object(disk, "volume_usage", return_value=reading(7)), \
                patch.object(cli.remote, "OVERFLOW_TIMEOUT", 0):
            status, said = self.run_lane("gate")
        self.assertEqual(status, resources.BUSY, said)
        self.assertIn(lane_results.SHORT_OF_SPACE, said)
        self.assertFalse(marker.exists())

    def test_a_lane_whose_log_cannot_be_written_is_unmeasured_not_failed(self):
        """On 2026-11-15 a spelling lane whose log could not be written was sent back to its session as a failure."""
        self.project(check={"command": [sys.executable, "-c", "print('Ran 1 test')"]})
        with patch.object(cli.processes, "run", side_effect=OSError(errno.ENOSPC, "No space left on device")):
            status, said = self.run_lane("check")
        self.assertEqual(status, lane_results.UNMEASURED, said)
        self.assertIn(f"{lane_results.OUT_OF_SPACE}check on this Mac", said)
        self.assertIn("No space left on device", said)
        recorded = self.receipt("check")
        self.assertEqual(recorded["outcome"], "unmeasured")
        self.assertIn("No space left on device", recorded["outOfSpace"])
        # Negative control: another error a lane's run raises is still the lane's failure.
        with patch.object(cli.processes, "run", side_effect=OSError(errno.EACCES, "Permission denied")):
            status, said = self.run_lane("check")
        self.assertEqual(status, 2, said)
        self.assertNotIn(lane_results.OUT_OF_SPACE, said)

    def test_a_lane_that_fails_with_the_disk_short_is_unmeasured(self):
        self.project(tests={"command": [sys.executable, "-c", "raise SystemExit(1)"]})
        with patch.dict(os.environ, {"QUEUEKEEPER_RUN_FREE_GB": "2"}), \
                patch.object(disk, "volume_usage", return_value=reading(1)):
            status, said = self.run_lane("tests")
        self.assertEqual(status, lane_results.UNMEASURED, said)
        self.assertIn(lane_results.OUT_OF_SPACE, said)
        self.assertEqual(self.receipt("tests")["outcome"], "unmeasured")
        self.assertIn("below QUEUEKEEPER_RUN_FREE_GB=2 GiB", self.receipt("tests")["outOfSpace"])
        # Negative control: with room on the disk, the failure is the lane's.
        status, said = self.run_lane("tests")
        self.assertNotIn(status, (0, lane_results.UNMEASURED), said)
        self.assertEqual(self.receipt("tests")["outcome"], "failed")
        self.assertNotIn(lane_results.OUT_OF_SPACE, said)

    def test_a_lane_the_disk_runs_short_under_ends_promptly_as_unmeasured(self):
        # Fixed: the 60 s sleep outlasts the first reading under any load.
        self.project(slow={"command": [sys.executable, "-c", "import time; time.sleep(60)"]})
        with patch.dict(os.environ, {"QUEUEKEEPER_RUN_FREE_GB": "2"}), patch.object(processes, "SPACE_POLL", 0), \
                patch.object(disk, "volume_usage", return_value=reading(1)):
            began = time.monotonic()
            status, said = self.run_lane("slow")
        self.assertLess(time.monotonic() - began, 45)
        self.assertEqual(status, lane_results.UNMEASURED, said)
        self.assertIn(f"{lane_results.OUT_OF_SPACE}slow on this Mac", said)
        recorded = self.receipt("slow")
        self.assertEqual(recorded["outcome"], "unmeasured")
        self.assertIn("the disk ran short of space", recorded["outOfSpace"])


if __name__ == "__main__":
    unittest.main()
