"""The mobile plugin refuses a sandbox to a lane that uses physical devices, which it reaches outside any sandbox."""
import json
import unittest

import host_isolation  # noqa: F401 (keeps the tests off the real hosts)
from queuekeeper import config, plugins
from queuekeeper_mobile import settings


class Refusal(unittest.TestCase):
    def test_a_lane_with_devices_or_a_device_pass_is_refused(self):
        for lane in ({"command": ["scripts/release-pass"], "devices": ["iPhone"]},
                     {"command": ["scripts/qk", "device-pass", "--device", "iPad", "--", "make", "check"]}):
            with self.subTest(lane=lane):
                self.assertIn("physical devices", settings.sandbox_refusal(lane))

    def test_other_lanes_are_not(self):
        self.assertIsNone(settings.sandbox_refusal({"command": ["make", "check"]}))

    def test_a_project_with_the_plugin_has_its_device_lane_refused_when_its_configuration_is_read(self):
        source = json.dumps({"schemaVersion": 1, "name": "App", "plugins": ["queuekeeper_mobile"],
                             "lanes": {"release": {"command": ["scripts/release-pass"], "devices": ["iPhone"],
                                                   "sandbox": True}}})
        with self.assertRaisesRegex(ValueError, "release.sandbox cannot be: it uses physical devices"):
            config.parse(source)
        self.assertIn(settings.sandbox_refusal, plugins.SANDBOX_REFUSALS)


if __name__ == "__main__":
    unittest.main()
