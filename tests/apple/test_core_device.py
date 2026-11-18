"""The bounded check that this Mac's CoreDeviceService answers for a physical device before devicectl or XCTest work
on it (vocaro/queuekeeper#227; doc/guides/devices.md, A stuck CoreDeviceService)."""
import os
import sys
import unittest
from unittest.mock import patch

import host_isolation  # noqa: F401
from queuekeeper_apple import core_device, device_notifications
from test_support.devices import Devicectl, PHONE, TABLET

LISTED = {PHONE: {"way": "cable", "transport": "wired", "kind": "iPhone", "model": None, "name": None}}


class Answers:
    """devicectl as the probe reaches it: each command it is given, answered by answers in turn."""

    def __init__(self, *answers):
        self.answers, self.commands = list(answers), []

    def __call__(self, command):
        self.commands.append(command)
        return self.answers.pop(0)


class Check(unittest.TestCase):
    def setUp(self):
        patcher = patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(core_device.PROBE)

    def check(self, *answers, listed=LISTED):
        self.devicectl = Answers(*answers)
        with patch.object(core_device, "run", self.devicectl), patch.object(core_device, "listing", lambda: listed):
            return core_device.check(PHONE, "iPhone")

    def test_a_service_that_answers_lets_the_work_start_after_one_bounded_read_only_query(self):
        self.assertIsNone(self.check((0, "")))
        [command] = self.devicectl.commands
        self.assertEqual(command[:4], ["xcrun", "devicectl", "--timeout", "30"])
        self.assertEqual(command[4:9], ["device", "info", "details", "--device", PHONE])

    def test_no_answer_for_a_device_devicectl_lists_is_a_stuck_service_named_with_its_restart(self):
        why = self.check((None, "no answer within 30s: Command timeout of 30.0 seconds exceeded"))
        self.assertIn("this Mac's CoreDeviceService looks stuck: devicectl lists iPhone", why)
        self.assertIn("no answer within 30s", why)
        self.assertIn("pkill -x CoreDeviceService", why)
        self.assertIn("once no other device run is in progress here", why)
        self.assertIn(core_device.GUIDE, why)
        self.assertIn("Queuekeeper does not restart it itself", why)
        # A listing that could not be read still names the service, as possibly listing the device.
        def unreadable(): raise RuntimeError("devicectl exited 1")
        with patch.object(core_device, "run", Answers((None, "no answer within 45s"))), \
                patch.object(core_device, "listing", unreadable):
            self.assertIn("CoreDeviceService looks stuck: devicectl may list iPhone", core_device.check(PHONE, "iPhone"))

    def test_no_answer_for_a_device_devicectl_does_not_list_is_the_device_not_the_service(self):
        why = self.check((None, "no answer within 30s"), listed={TABLET: {}})
        self.assertIn("devicectl does not list iPhone", why)
        self.assertNotIn("CoreDeviceService", why)

    def test_a_refusal_is_said_with_devicectl_s_words_and_not_as_a_stuck_service(self):
        why = self.check((1, "ERROR: The device has not been unlocked recently."))
        self.assertIn("devicectl could not reach iPhone (`devicectl device info details` exit 1: ERROR: The device "
                      "has not been unlocked recently.)", why)
        self.assertNotIn("CoreDeviceService", why)

    def test_tests_switch_it_off_and_nothing_runs(self):
        os.environ[core_device.PROBE] = "off"
        self.devicectl = Answers()
        with patch.object(core_device, "run", self.devicectl):
            self.assertIsNone(core_device.check(PHONE))
        self.assertEqual(self.devicectl.commands, [])


class Execute(unittest.TestCase):
    def test_devicectl_that_outlives_its_bound_is_no_answer(self):
        with patch.object(core_device, "TIMEOUT", 0.2), patch.object(core_device, "GRACE", 0.2):
            status, said = core_device.execute([sys.executable, "-c", "import time; time.sleep(30)"])
        self.assertEqual((status, said), (None, "no answer within 0.4s"))

    def test_devicectl_s_own_timeout_is_no_answer_and_a_quick_refusal_is_its_status(self):
        times = iter([0, 31])
        status, said = core_device.execute(
            [sys.executable, "-c", "import sys; sys.exit('Command timeout of 30.0 seconds exceeded')"],
            clock=lambda: next(times))
        self.assertEqual((status, said), (None, "no answer within 30s: Command timeout of 30.0 seconds exceeded"))
        times = iter([0, 0.14])
        status, said = core_device.execute([sys.executable, "-c", "import sys; sys.exit('ERROR: not found')"],
                                           clock=lambda: next(times))
        self.assertEqual((status, said), (1, "ERROR: not found"))


class Shortcuts(unittest.TestCase):
    def test_a_stuck_service_fails_the_owner_s_shortcut_without_waiting_out_its_launch(self):
        launches = Devicectl()
        with patch.dict(os.environ), patch.object(device_notifications, "run", launches), \
                patch.object(core_device, "run", Answers((None, "no answer within 30s"))), \
                patch.object(core_device, "listing", lambda: LISTED):
            os.environ.pop(core_device.PROBE)
            opened, detail = device_notifications.trigger(PHONE, device_notifications.RESTORE)
        self.assertFalse(opened)
        self.assertIn("devicectl did not try to open Shortcuts with shortcuts://run-shortcut?name=Vocaro%20Restore", detail)
        self.assertIn("CoreDeviceService looks stuck", detail)
        self.assertEqual(launches.commands, [])
        # The positive control: a service that answers has the shortcut launched.
        with patch.dict(os.environ), patch.object(device_notifications, "run", launches), \
                patch.object(core_device, "run", Answers((0, ""))):
            os.environ.pop(core_device.PROBE)
            self.assertEqual(device_notifications.trigger(PHONE, device_notifications.RESTORE)[0], True)
        self.assertEqual(len(launches.commands), 1)


if __name__ == "__main__":
    unittest.main()
