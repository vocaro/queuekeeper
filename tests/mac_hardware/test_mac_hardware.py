"""The Mac hardware extension: a lane's "chips", the chips its results are valid on, and where they put the lane.
No test reads this Mac's chip or reaches the second Mac: both readings are stand-ins."""
import contextlib
import io
import json
import subprocess
import unittest
from unittest.mock import patch

import host_isolation  # first, as every test module

from queuekeeper import cli; from queuekeeper import config; from queuekeeper import extensions; import queuekeeper_mac_hardware as mac_hardware; from queuekeeper import remote
from queuekeeper_mac_hardware import chip, lane
from test_support import farm as farm_fixture

mac_hardware.register()

M5, M1 = "apple-m5-max", "apple-m1-max"


class Macs(unittest.TestCase):
    """This Mac has an M5 Max and the second Mac, studio, an M1 Max, unless a test says otherwise."""

    def setUp(self):
        self.macs(M5, "studio", M1)

    def macs(self, mine, host, theirs, **more):
        """This Mac's chip, and the chip of the farm's Mac at host, and of each of more, a Mac's destination."""
        found = {**({host: theirs} if host else {}), **more}
        self.enterContext(patch.object(lane, "reading", lambda: (mine, found)))

    def lanes(self, chips, **more):
        return {"gate": {"command": ["./check.sh"], "resource": "exclusive", "chips": chips, **more}}

    def placed(self, chips):
        return extensions.placement(self.lanes(chips), "gate")


class ChipTests(unittest.TestCase):
    def test_a_chip_is_named_in_lowercase_with_hyphens(self):
        self.assertEqual(chip.slug("Apple M5 Max\n"), M5)
        self.assertEqual(chip.slug("Apple M1"), "apple-m1")
        self.assertEqual(chip.slug(""), "")

    def test_this_mac_s_chip_is_read_from_the_system_and_none_when_it_cannot_be(self):
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "Apple M5 Max\n", "")):
            self.assertEqual(chip.here(), M5)
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "unknown oid")):
            self.assertIsNone(chip.here())
        with patch.object(subprocess, "run", side_effect=OSError("no sysctl")):
            self.assertIsNone(chip.here())

    def test_the_second_mac_s_chip_is_asked_once_and_unread_when_it_cannot_be_reached(self):
        asked = []
        def ssh(host, script, **options):
            asked.append((host, script))
            return subprocess.CompletedProcess([], 0, "Apple M1 Max\n", "")
        with patch.dict(chip.THERE, {}, clear=True), patch.object(remote, "ssh", ssh):
            self.assertEqual((chip.there("studio"), chip.there("studio")), (M1, M1))
        self.assertEqual(asked, [("studio", "sysctl -n machdep.cpu.brand_string")])
        with patch.dict(chip.THERE, {}, clear=True), \
                patch.object(remote, "ssh", return_value=subprocess.CompletedProcess([], 255, "", "timed out")):
            self.assertIsNone(chip.there("studio"))
            self.assertEqual(chip.THERE, {})


class PlacementTests(Macs):
    def test_a_lane_that_lists_no_chips_or_both_macs_chips_is_placed_as_any_lane(self):
        self.assertIsNone(self.placed([M5, M1]))
        plain = {"gate": {"command": ["./check.sh"], "resource": "heavy"}}
        self.assertIsNone(extensions.placement(plain, "gate"))
        self.assertIsNone(config.stays(plain, "gate"))
        self.assertIsNone(config.stays(self.lanes([M5, M1]), "gate"))

    def test_a_lane_only_this_mac_s_chip_suits_stays_here_as_one_that_says_here(self):
        where, why = self.placed([M5])
        self.assertEqual(where, {extensions.HERE})
        self.assertIn("this Mac has apple-m5-max, and studio has apple-m1-max", why)
        self.assertIn("gate.chips: its chips are apple-m5-max", config.stays(self.lanes([M5]), "gate"))

    def test_a_lane_only_the_second_mac_s_chip_suits_runs_only_there(self):
        self.assertEqual(self.placed([M1])[0], {"studio"})
        self.assertIsNone(config.stays(self.lanes([M1]), "gate"))

    def test_a_lane_some_of_the_farm_s_macs_suit_is_placed_among_those_alone(self):
        self.macs(M5, "studio", M1, mini=M5, rented=M1, away=None)
        # Every Mac that has a listed chip, and none whose chip is another's or unread.
        self.assertEqual(self.placed([M1])[0], {"studio", "rented"})
        where, why = self.placed([M5])
        self.assertEqual(where, {extensions.HERE, "mini"})
        self.assertIn("this Mac has apple-m5-max, and studio has apple-m1-max, and mini has apple-m5-max, and rented "
                      "has apple-m1-max, and away has a chip that could not be read", why)
        # This Mac may run it, so it does not stay here alone, and is offered to mini and no other.
        self.assertIsNone(config.stays(self.lanes([M5]), "gate"))
        with farm_fixture.only("studio", "mini", "rented", "away"):
            self.assertEqual(remote.eligible(where), ("mini",))
            self.assertEqual(remote.eligible(self.placed([M1])[0]), ("studio", "rented"))
        # Chips that every Mac but the unread one has leave that one out.
        self.assertEqual(self.placed([M5, M1])[0], {extensions.HERE, "studio", "mini", "rented"})

    def test_a_lane_stays_here_when_the_second_mac_s_chip_cannot_be_read_or_there_is_none(self):
        self.macs(M5, "studio", None)
        self.assertEqual(self.placed([M5])[0], {extensions.HERE})
        self.assertIn("studio has a chip that could not be read", self.placed([M5])[1])
        self.macs(M5, None, None)
        self.assertIsNone(self.placed([M5]))
        with self.assertRaisesRegex(ValueError, "the farm has no other Mac"): self.placed([M1])

    def test_a_lane_no_mac_s_chip_suits_is_refused_naming_what_each_has(self):
        with self.assertRaisesRegex(ValueError, "gate.chips: no Mac has a chip the lane lists: its chips are "
                                    "apple-m4; this Mac has apple-m5-max, and studio has apple-m1-max"):
            self.placed(["apple-m4"])

    def test_a_group_goes_where_its_lane_must_and_lanes_that_must_run_apart_are_refused(self):
        lanes = {"all": {"parallel": ["fast", "gpu"]}, "fast": {"command": ["true"], "resource": "heavy"},
                 "gpu": {"command": ["true"], "resource": "heavy", "chips": [M1]}}
        where, why = extensions.placement(lanes, "all")
        self.assertEqual(where, {"studio"})
        self.assertIn("gpu.chips", why)
        lanes["fast"]["chips"] = [M5]
        self.assertIn("fast.chips", extensions.placement(lanes, "fast")[1])
        with self.assertRaisesRegex(ValueError, "all runs its lanes on one Mac, and they must run apart"):
            extensions.placement(lanes, "all")


class ConfigTests(Macs):
    BASE = {"schemaVersion": 1, "name": "Project"}

    def parse(self, chips, listed=("queuekeeper_mac_hardware",), **more):
        return config.parse(json.dumps({**self.BASE, "extensions": list(listed), "lanes": self.lanes(chips, **more)}))

    def test_a_lane_lists_chips_by_name_and_reading_the_configuration_asks_no_mac(self):
        with patch.object(lane, "reading", side_effect=AssertionError("a Mac was asked")):
            self.assertEqual(self.parse([M5, M1])["lanes"]["gate"]["chips"], [M5, M1])

    def test_what_is_not_a_list_of_chip_names_is_refused(self):
        for chips in ([], M5, ["Apple M5 Max"], [5], [""]):
            with self.subTest(chips=chips), self.assertRaisesRegex(ValueError, "gate.chips must be a non-empty list"):
                self.parse(chips)
        with self.assertRaisesRegex(ValueError, "gate.chips needs an admitted lane"): self.parse([M5], resource="self")

    def test_the_key_without_the_extension_is_refused_naming_it(self):
        with patch.dict(extensions.LANE_KEYS, {}, clear=True), \
                patch.object(extensions, "LOADED", ["queuekeeper_mac_hardware"]), \
                self.assertRaisesRegex(ValueError, "gate.chips is the queuekeeper_mac_hardware extension's key"):
            self.parse([M5], listed=())

    def test_doctor_names_a_lane_no_mac_can_run_and_no_other(self):
        self.assertEqual(lane.warnings(None, {"lanes": self.lanes([M5])}), [])
        found = lane.warnings(None, {"lanes": {**self.lanes(["apple-m4"]), "docs": {"command": ["true"]}}})
        self.assertEqual(len(found), 1)
        self.assertIn("gate.chips: no Mac has a chip the lane lists", found[0])
        self.assertIn("so the lane cannot run", found[0])
        self.assertIn(lane.warnings, extensions.DOCTOR)

    def test_host_chip_prints_each_mac_s_chip_as_a_lane_names_it(self):
        lines = []
        self.assertEqual(lane.read(None, out=lines.append), 0)
        self.assertEqual(lines, ["this Mac: apple-m5-max", "studio: apple-m1-max"])
        self.macs(None, None, None)
        lines.clear()
        self.assertEqual(lane.read(None, out=lines.append), 2)
        self.assertEqual(lines, ["this Mac: could not be read"])
        self.assertIs(extensions.HOST_READS["chip"][2], lane.read)


class RunTests(Macs):
    """`qk run` of a lane that runs only on the second Mac."""

    def setUp(self):
        super().setUp()
        import tempfile
        from pathlib import Path
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        (self.root / "queuekeeper.json").write_text(json.dumps({
            "schemaVersion": 1, "name": "Project", "extensions": ["queuekeeper_mac_hardware"],
            "lanes": self.lanes([M1])}))
        self.sent = []
        def execute(root, found, host, work, **options):
            self.sent.append((host, work))
            return 0
        self.enterContext(patch.object(remote, "execute", execute))
        farm_fixture.use(self, "studio")

    def run_lane(self, *arguments):
        error = io.StringIO()
        with contextlib.redirect_stderr(error), contextlib.redirect_stdout(io.StringIO()):
            status = cli.main(["--project", str(self.root), "run", *arguments, "gate"])
        return status, error.getvalue()

    def test_it_is_sent_to_the_second_mac_to_wait_there_and_never_offered_as_free(self):
        status, error = self.run_lane()
        self.assertEqual((status, self.sent), (0, [("studio", ["run", "--here", "gate"])]))
        self.assertIn("gate runs only on studio (gate.chips: its chips are apple-m1-max", error)

    def test_it_cannot_be_run_here(self):
        status, error = self.run_lane("--here")
        self.assertEqual(self.sent, [])
        self.assertNotEqual(status, 0)
        self.assertIn("so it cannot run here with --here", error)

    def test_with_several_macs_that_suit_it_waits_on_the_first_with_spare_capacity_and_names_them(self):
        farm_fixture.use(self, "studio", "mini", "rented")
        self.macs(M5, "studio", M1, mini=M5, rented=M1)
        free = {"slots": 3, "slotsMax": 6, "busy": [], "busyLimit": 75}
        readings = {"studio": {**free, "busy": [0, 1, 2, 3, 4, 5]}, "rented": free}
        with patch.object(remote, "reachable"), \
                patch.object(remote, "capacity_there", side_effect=lambda host, **options: readings[host]):
            status, error = self.run_lane()
        self.assertEqual((status, self.sent), (0, [("rented", ["run", "--here", "gate"])]))
        self.assertIn("gate runs only on studio or rented (gate.chips: its chips are apple-m1-max", error)


if __name__ == "__main__":
    unittest.main()
