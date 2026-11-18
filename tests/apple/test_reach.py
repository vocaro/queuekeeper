"""The owner's declaration of each physical device, and which Macs reach it, read from recorded devicectl listings
(vocaro/queuekeeper#211; doc/guides/devices.md, Declared devices)."""
import contextlib
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

import host_isolation  # noqa: F401
from queuekeeper_apple import devices, reach
from test_support.devices import PHONE, TABLET, DUO, later, scratch

LISTINGS = Path(__file__).resolve().parent / "fixtures/Devicectl"
CABLE, NETWORK = (json.loads((LISTINGS / name).read_text()) for name in ("cable.json", "network.json"))
# A Mac of the farm beside this one, by its name in the farm and its SSH destination.
OTHER = ("studio", "studio.local")


def without(listing, udid):
    """listing with the device udid left out, as a Mac that does not reach it lists."""
    return {"result": {"devices": [device for device in listing["result"]["devices"]
                                   if device["hardwareProperties"]["udid"] != udid]}}


class Listings(unittest.TestCase):
    def test_a_cable_and_the_network_are_read_from_the_transport_and_simulators_are_left_out(self):
        here = reach.listed(CABLE)
        self.assertEqual(set(here), {TABLET, PHONE})
        self.assertEqual({udid: device["way"] for udid, device in here.items()}, {TABLET: "cable", PHONE: "cable"})
        self.assertEqual(here[PHONE]["transport"], "wired")
        self.assertEqual((here[TABLET]["kind"], here[TABLET]["model"]), ("iPad", "iPad Air 13-inch (M3)"))
        # The iPhone's tunnel reads disconnected though it is attached: reach is the transport alone.
        self.assertEqual(next(device["connectionProperties"]["tunnelState"] for device in CABLE["result"]["devices"]
                              if device["hardwareProperties"]["udid"] == PHONE), "disconnected")
        there = reach.listed(NETWORK)
        self.assertEqual({udid: device["way"] for udid, device in there.items()}, {TABLET: "network", PHONE: "network"})

    def test_older_json_a_transport_no_declaration_names_and_a_listing_without_devices(self):
        found = reach.listed({"result": {"devices": [
            {"properties": {"hardware": {"udid": PHONE.lower(), "reality": "physical", "deviceType": "iPhone"},
                            "connection": {"transportType": "localNetwork"}}},
            {"hardwareProperties": {"udid": TABLET}, "connectionProperties": {"pairingState": "paired"}},
            {"hardwareProperties": {"udid": DUO, "reality": "simulated"}, "connectionProperties": {}},
            "not a device"]}})
        self.assertEqual(found[PHONE]["way"], "network")
        self.assertEqual((found[TABLET]["way"], found[TABLET]["transport"]), (None, None))
        self.assertNotIn(DUO, found)
        for answer in ({}, {"result": {"devices": None}}, []):
            with self.subTest(answer=answer), self.assertRaises(ValueError): reach.listed(answer)


class Readings(unittest.TestCase):
    def test_this_mac_reads_the_listing_it_is_given_or_devicectl_read_only_and_bounded(self):
        temp = scratch()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "listing.json"
        path.write_text(json.dumps(CABLE))
        with patch.dict(os.environ, {reach.LISTING: str(path)}):
            self.assertEqual(set(reach.read_here()[0]), {TABLET, PHONE})
        calls = []

        def run(command, **options):
            calls.append((command, options))
            return subprocess.CompletedProcess(command, 0, json.dumps(NETWORK), "")
        with patch.dict(os.environ, {reach.LISTING: ""}):
            found, _seconds = reach.read_here(run)
            self.assertEqual(found[PHONE]["way"], "network")
            self.assertEqual(calls[0][0][:4], ["xcrun", "devicectl", "list", "devices"])
            self.assertEqual(calls[0][1]["timeout"], reach.TIMEOUT)
            with self.assertRaisesRegex(RuntimeError, "exited 1: CoreDevice"):
                reach.read_here(lambda command, **options: subprocess.CompletedProcess(command, 1, "", "CoreDevice gone"))

    def test_another_mac_is_read_over_ssh_and_one_not_read_is_kept_with_its_error(self):
        answered = subprocess.CompletedProcess([], 0, json.dumps(NETWORK), "")
        with patch("queuekeeper.remote.ssh", return_value=answered) as ssh:
            self.assertEqual(set(reach.read_there("studio.local")[0]), {TABLET, PHONE})
        self.assertEqual(ssh.call_args.args[0], "studio.local")
        self.assertIn("devicectl list devices", ssh.call_args.args[1])
        with patch("queuekeeper.remote.ssh", return_value=subprocess.CompletedProcess([], 255, "", "no route\n")), \
                self.assertRaisesRegex(RuntimeError, "exited 255: no route"):
            reach.read_there("studio.local")

        def there(host):
            if host == "away.local": raise subprocess.TimeoutExpired("ssh", reach.TIMEOUT)
            return reach.listed(NETWORK), 0.5
        found = reach.readings(here=lambda: (reach.listed(CABLE), 0.2), there=there,
                               macs=[OTHER, ("away", "away.local")])
        self.assertEqual([item["mac"] for item in found], [reach.mac_here(), "studio", "away"])
        self.assertEqual([item["host"] for item in found], [None, "studio.local", "away.local"])
        self.assertEqual(found[1]["devices"][PHONE]["way"], "network")
        self.assertIn("timed out", found[2]["error"])
        self.assertNotIn("devices", found[2])


class Declarations(unittest.TestCase):
    MACS = ["owner-mac", "studio"]

    def test_the_owners_three_cases_are_declarations_and_a_wrong_one_says_why(self):
        for case in ({"reach": ["network"], "absent": "error"},
                     {"reach": ["cable", "network"], "cableOn": "studio", "absent": "error"},
                     {"reach": ["cable", "network"], "absent": "offline"},
                     {"reach": ["cable"], "absent": "error", "declaredBy": "owner", "declaredAt": later()}):
            with self.subTest(case=case): self.assertEqual(reach.problems(case, self.MACS), [])
        for case, said in (({"reach": [], "absent": "error"}, "reach is the ways"),
                           ({"reach": ["wifi"], "absent": "error"}, "each of cable or network once"),
                           ({"reach": ["cable", "cable"], "absent": "error"}, "once"),
                           ({"reach": "cable", "absent": "error"}, "reach is the ways"),
                           ({"reach": ["network"], "absent": "gone"}, "absent is error or offline"),
                           ({"reach": ["network"]}, "absent is error or offline"),
                           ({"reach": ["network"], "cableOn": "studio", "absent": "error"}, "reach lists no cable"),
                           ({"reach": ["cable"], "cableOn": "attic", "absent": "error"}, "attic, which is neither"),
                           ({"reach": ["cable"], "absent": "error", "usbOn": "studio"}, "usbOn, which a declaration"),
                           ("cable", "is an object")):
            with self.subTest(case=case): self.assertIn(said, "; ".join(reach.problems(case, self.MACS)))
        # A cable's Mac is matched whatever its case, and not checked when no Macs are given.
        self.assertEqual(reach.problems({"reach": ["cable"], "cableOn": "STUDIO", "absent": "offline"}, self.MACS), [])
        self.assertEqual(reach.problems({"reach": ["cable"], "cableOn": "attic", "absent": "offline"}), [])


class Recorded(unittest.TestCase):
    """The declaration and status, in an isolated record."""

    def setUp(self):
        temp = scratch()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = self.root / "device-handoff.json"
        self.record = devices.Record(self.path)
        for patcher in (patch.dict(os.environ, {devices.OVERRIDE: str(self.path)}),
                        patch.object(devices, "LEGACY", str(self.root / "legacy-*device-handoff*.json")),
                        patch.object(reach, "mac_here", return_value="owner-mac"),
                        patch.object(reach, "farm_macs", return_value=[OTHER])):
            patcher.start()
            self.addCleanup(patcher.stop)

    def write(self, entries, **fields):
        self.path.write_text(json.dumps({"vocaroDeviceHandoff": 1, "revision": 4, "devices": entries, **fields}))

    def cli(self, *arguments, here=CABLE, there=NETWORK):
        listing = self.root / "listing.json"
        listing.write_text(json.dumps(here))

        def read_there(host):
            if isinstance(there, Exception): raise there
            return reach.listed(there), 0.5
        with contextlib.redirect_stdout(StringIO()) as output, contextlib.redirect_stderr(StringIO()) as error, \
                patch.dict(os.environ, {reach.LISTING: str(listing)}), patch.object(reach, "read_there", read_there):
            code = devices.main(list(arguments))
        return code, output.getvalue(), error.getvalue()

    def entries(self):
        held = {"device": TABLET, "state": "reserved", "ownerChatID": "session-a", "revision": 3,
                "expectedReleaseBy": later(), "purpose": "VoiceOver session"}
        # An orphan: its run's process is recorded and gone. An uncertain entry needs attended recovery.
        orphan = {"device": PHONE, "state": "testing", "ownerChatID": "session-b", "revision": 7, "activeRun": True,
                  "hostProcessIdentifier": 999999, "hostProcessStart": "gone", "expectedReleaseBy": later()}
        return {"iPad": held, "iPhone": orphan,
                "spare": {"device": "00008110-000A1B2C3D4E5F60", "state": "reserved", "ownerChatID": "session-c",
                          "revision": 2, "uncertain": True, "uncertainReason": "restoration unverified"}}

    def test_declaring_changes_no_entry_held_orphaned_or_uncertain(self):
        self.write(self.entries())
        self.assertEqual({key: devices.condition(entry) for key, entry in self.record.load()["devices"].items()},
                         {"iPad": "held", "iPhone": "orphan", "spare": "uncertain"})
        before = self.record.load()["devices"]
        for key, arguments in (("iPad", ["--reach", "network", "--absent", "error"]),
                               ("iPhone", ["--reach", "cable,network", "--absent", "offline"]),
                               ("spare", ["--reach", "cable,network", "--cable-on", "studio", "--absent", "error"])):
            code, output, error = self.cli("declare", key, "--by", "owner", *arguments)
            self.assertEqual(code, 0, error)
            self.assertEqual(json.loads(output)["key"], key)
        value = self.record.load()
        # Every entry, its revision and reservation fields included, is as it was.
        self.assertEqual(value["devices"], before)
        self.assertEqual(value[reach.DECLARATIONS]["iPhone"]["reach"], ["cable", "network"])
        self.assertEqual(value[reach.DECLARATIONS]["spare"]["cableOn"], "studio")
        self.assertEqual(value[reach.DECLARATIONS]["iPad"]["declaredBy"], "owner")
        # The holder's next change still names the revision it read.
        self.record.update("iPad", owner="session-a", revision=3, run={"note": "still mine"})
        # Declared again, it is replaced; cleared, it is gone, and the entries are still as they were.
        self.cli("declare", "iPad", "--by", "owner", "--reach", "cable", "--absent", "offline")
        self.assertEqual(self.record.load()[reach.DECLARATIONS]["iPad"]["reach"], ["cable"])
        self.assertEqual(self.cli("declare", "iPhone", "--by", "owner", "--clear")[0], 0)
        self.assertNotIn("iPhone", self.record.load()[reach.DECLARATIONS])
        self.assertEqual(self.record.load()["devices"]["iPhone"], before["iPhone"])

    def test_a_wrong_declaration_or_one_with_no_device_to_declare_changes_nothing(self):
        self.write(self.entries())
        written = self.path.read_bytes()
        for arguments, said in ((["iPad", "--reach", "wifi", "--absent", "error"], "each of cable or network"),
                                (["iPad", "--reach", "cable", "--cable-on", "attic", "--absent", "error"], "attic"),
                                (["iPad", "--reach", "network", "--cable-on", "studio", "--absent", "error"],
                                 "lists no cable"),
                                (["iPad", "--reach", "network"], "names --reach and --absent"),
                                (["iPad", "--clear", "--absent", "error"], "takes no --reach"),
                                (["iPad", "--clear"], "iPad is not declared"),
                                (["iPod", "--reach", "network", "--absent", "error"], "records no physical device"),
                                ([devices.FOREGROUND, "--reach", "network", "--absent", "error"],
                                 "records no physical device")):
            with self.subTest(arguments=arguments):
                code, _output, error = self.cli("declare", *arguments, "--by", "owner")
                self.assertEqual(code, 2)
                self.assertIn(said, error)
                self.assertEqual(self.path.read_bytes(), written)
        # Another Mac's record, which defers physical devices, declares none.
        self.write({}, physicalDevicesRecordedOn="owner-mac")
        code, _output, error = self.cli("declare", "iPad", "--by", "owner", "--reach", "network", "--absent", "error")
        self.assertEqual(code, 2)
        self.assertIn("declared in owner-mac's device record", error)

    def test_status_lists_each_device_with_each_macs_reading_and_what_nobody_declared(self):
        entries = self.entries()
        self.write(entries, declarations={"iPhone": {"reach": ["cable", "network"], "cableOn": "studio",
                                                     "absent": "offline", "declaredBy": "owner",
                                                     "declaredAt": "2026-11-17T20:00:00+00:00"}})
        unknown = json.loads(json.dumps(CABLE))
        unknown["result"]["devices"].append({"hardwareProperties": {"udid": "00008120-0011223344556677",
                                                                    "deviceType": "iPhone", "reality": "physical"},
                                             "deviceProperties": {"name": "Guest iPhone"},
                                             "connectionProperties": {"transportType": "localNetwork"}})
        code, output, error = self.cli("status", here=unknown, there=without(NETWORK, TABLET))
        self.assertEqual(code, 0, error)
        self.assertIn("iPad: not declared. Read: owner-mac by cable; studio not listed", output)
        self.assertIn("iPhone: declared by owner at 2026-11-17T20:00:00+00:00: reach cable on studio, then network; "
                      "reached by no Mac means offline, no error. Read: owner-mac by cable, which is expected on "
                      "studio; studio by network", output)
        self.assertIn("spare: not declared. Read: owner-mac not listed; studio not listed", output)
        self.assertIn("Seen, not in the record and not declared: 00008120-0011223344556677 (iPhone, named "
                      "'Guest iPhone'). Read: owner-mac by network; studio not listed", output)
        # The entries are listed as before, above.
        self.assertIn("iPad: held; owner session-a", output)
        # A Mac that cannot be read is said, and its devices are not taken as unlisted.
        code, output, _error = self.cli("status", there=RuntimeError("its devicectl listing exited 255: no route"))
        self.assertIn("studio's devices were not read: its devicectl listing exited 255: no route", output)
        self.assertIn("iPad: not declared. Read: owner-mac by cable\n", output)

    def test_status_says_a_wrong_declaration_and_a_way_it_does_not_list(self):
        self.write(self.entries(), declarations={"iPad": {"reach": ["network"], "absent": "error"},
                                                 "spare": {"reach": ["cable"], "cableOn": "attic", "absent": "error"}})
        _code, output, _error = self.cli("status")
        self.assertIn("iPad: declared: reach network; reached by no Mac means an error. Read: "
                      "owner-mac by cable, which its declaration does not list; studio by network", output)
        self.assertIn("spare: declared wrongly, so taken as undeclared: cableOn names attic", output)

    def test_status_of_a_record_that_defers_physical_devices_reads_this_mac_alone_as_seen(self):
        self.write({}, physicalDevicesRecordedOn="owner-mac")
        with patch.object(reach, "farm_macs", return_value=[]), patch.object(reach, "mac_here", return_value="studio"):
            _code, output, _error = self.cli("status", here=NETWORK)
        self.assertIn("Devices are declared in owner-mac's record", output)
        self.assertIn(f"Seen, whether owner-mac's record declares it is said there: {PHONE} (iPhone, iPhone 17 Pro "
                      "Max, named 'iPhone'). Read: studio by network", output)


if __name__ == "__main__":
    unittest.main()
