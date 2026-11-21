"""Apple contributes bounded device observations, which the mobile plugin joins to the record, without a
dashboard dependency."""
import json
import subprocess
import unittest
from unittest.mock import patch
import host_isolation
from queuekeeper_apple import observations as farm
from queuekeeper_mobile import observations as shared

READ_CONNECTIONS = farm.read_connections
READ_DEVICES = shared.read_devices

class DeviceConnections(unittest.TestCase):
    PHONE = "00000000-0000000000000001"
    PAD = "00000000-0000000000000002"

    def test_current_and_legacy_xcode_inventory_use_hardware_ids_and_exclude_simulators(self):
        found = farm.connections_of({"result": {"devices": [
            {"identifier": "core-device-uuid", "properties": {
                "hardware": {"udid": self.PHONE.lower(), "reality": "physical", "deviceType": "iPhone",
                             "marketingName": "iPhone 17 Pro Max"},
                "connection": {"state": "connected", "transportType": "localNetwork"}}},
            {"hardwareProperties": {"udid": self.PAD, "deviceType": "iPad", "marketingName": "iPad Air 13-inch (M3)"},
             "connectionProperties": {"tunnelState": "disconnected", "transportType": "wired"}},
            {"properties": {"hardware": {"udid": self.PHONE, "reality": "simulated"},
                            "connection": {"state": "disconnected"}}},
            {"identifier": self.PHONE, "deviceProperties": {"name": "iPhone"}},
        ]}})
        # Each device's kind and marketing name, which the Farm tab's icon and line show.
        self.assertEqual(found, {
            self.PHONE: {"connection": "connected", "transport": "localNetwork", "kind": "iphone", "model": "iPhone 17 Pro Max"},
            self.PAD: {"connection": "disconnected", "transport": "wired", "kind": "ipad", "model": "iPad Air 13-inch (M3)"}})
        # A kind it does not draw, or none, is other.
        watch = {"result": {"devices": [{"hardwareProperties": {"udid": self.PHONE, "deviceType": "appleWatch"}}]}}
        self.assertEqual((farm.connections_of(watch)[self.PHONE]["kind"], farm.connections_of(watch)[self.PHONE]["model"]),
                         ("other", None))
        with self.assertRaises(ValueError): farm.connections_of({"result": {"devices": None}})
        self.assertEqual(farm.connections_of({"result": {"devices": [{"hardwareProperties": {"udid": self.PHONE}}]}})
                         [self.PHONE]["connection"], "unknown")

    def test_inventory_query_is_bounded_and_reports_command_and_json_failures(self):
        result = subprocess.CompletedProcess([], 0, json.dumps({"result": {"devices": []}}), "")
        with patch.object(farm.subprocess, "run", return_value=result) as run:
            self.assertEqual(READ_CONNECTIONS(), {})
        self.assertEqual(run.call_args.kwargs["timeout"], 15)
        self.assertIn("--timeout", run.call_args.args[0])
        self.assertIn("--json-output", run.call_args.args[0])
        for result in (subprocess.CompletedProcess([], 1, "", "CoreDevice unavailable"),
                       subprocess.CompletedProcess([], 0, "not JSON", "")):
            with self.subTest(result=result), patch.object(farm.subprocess, "run", return_value=result):
                with self.assertRaises((RuntimeError, ValueError)): READ_CONNECTIONS()

    def test_connection_failures_preserve_reservations_and_do_not_claim_disconnection(self):
        record = {"devices": {"iPhone": {"device": self.PHONE, "state": "held", "ownerChatID": "session"}}}
        for error in (FileNotFoundError("xcrun unavailable"), subprocess.TimeoutExpired("devicectl", 15),
                      ValueError("invalid inventory")):
            with self.subTest(error=error), patch.object(farm.offload_host, "device_record_whole", return_value=record), \
                    patch.object(shared, "read_connections", side_effect=error):
                found = READ_DEVICES()
            self.assertIsNone(found["error"])
            self.assertTrue(found["connectionError"])
            self.assertIsNone(found["checkedAt"])
            self.assertEqual(found["devices"][0]["connection"], "unknown")
            self.assertEqual(found["devices"][0]["owner"], "session")

    def test_live_connections_join_reservations_by_udid_and_deferrals_are_not_queried(self):
        record = {"devices": {"iPhone": {"device": self.PHONE, "state": "held"},
                              "iPad": {"device": self.PAD, "state": "released"}}}
        with patch.object(farm.offload_host, "device_record_whole", return_value=record), \
                patch.object(shared, "read_connections", return_value={self.PHONE: {"connection": "connected", "transport": "localNetwork"}}):
            found = READ_DEVICES()
        self.assertEqual([(device["name"], device["connection"]) for device in found["devices"]],
                         [("iPad", "notDetected"), ("iPhone", "connected")])
        self.assertEqual((found["devices"][0]["kind"], found["devices"][0]["model"]), ("other", None))  # not detected
        self.assertIsNotNone(found["checkedAt"])
        self.assertIsNone(found["connectionError"])
        record[farm.offload_host.DEFERRED] = "other-mac"
        with patch.object(farm.offload_host, "device_record_whole", return_value=record), \
                patch.object(shared, "read_connections") as read:
            found = READ_DEVICES()
        read.assert_not_called()
        self.assertEqual(found["devices"][0]["connection"], "unknown")

    def test_physical_devices_are_read_separately_from_mac_slots(self):
        record = {"devices": {
            "iphone": {"device": "00000000-0000000000000001", "state": "reserved", "ownerChatID": "session",
                       "purpose": "release tests", "expectedReleaseBy": "2026-11-14T10:00:00Z"},
            "ipad": {"device": "00000000-0000000000000002", "state": "released"},
            "AndroidPhone": {"device": "33041FDH2000QK", "physicalDevice": True, "state": "reserved"},
            "emulator": {"device": "avd:Pool_1", "state": "reserved"},
            "simulator": {"device": "00000000-0000-0000-0000-000000000003", "state": "reserved"},
            "mac-foreground": {"state": "reserved"}}, "queues": {"iphone": [{"owner": "next"}]}}
        with patch.object(farm.offload_host, "device_record_whole", return_value=record):
            found = READ_DEVICES()
        named = {device["name"]: device for device in found["devices"]}
        self.assertEqual(set(named), {"AndroidPhone", "ipad", "iphone"})
        self.assertEqual(named["iphone"]["waiting"], 1)
        self.assertEqual(named["iphone"]["owner"], "session")
        self.assertIsNone(named["ipad"]["owner"])
        self.assertEqual(named["ipad"]["connection"], "notDetected")
        with patch.object(farm.offload_host, "device_record_whole", return_value={"devices": {}, farm.offload_host.DEFERRED: "studio"}):
            self.assertEqual(READ_DEVICES()["deferredTo"], "studio")
        with patch.object(farm.offload_host, "device_record_whole", side_effect=ValueError("broken record")):
            self.assertEqual(READ_DEVICES()["error"], "broken record")
