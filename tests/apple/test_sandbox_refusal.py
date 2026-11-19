"""The Apple extension refuses a sandbox to a lane that runs Xcode or SwiftPM, which apply sandboxes of their own."""
import json
import unittest

import host_isolation  # noqa: F401 (keeps the tests off the real hosts)
from queuekeeper import config, extensions
from queuekeeper_apple import settings


class Refusal(unittest.TestCase):
    def test_xcode_and_swiftpm_commands_and_idioms_lanes_are_refused(self):
        for lane in ({"command": ["/usr/bin/xcodebuild", "test"]}, {"command": ["swift", "test"]},
                     {"command": ["scripts/qk", "simulator-tests", "--"]}, {"command": ["make"], "compileFirst": ["xcrun", "swift", "build"]},
                     {"command": ["make"], "idioms": "layout differs"}):
            with self.subTest(lane=lane):
                self.assertTrue(settings.sandbox_refusal(lane))

    def test_other_commands_are_not(self):
        self.assertIsNone(settings.sandbox_refusal({"command": ["python3", "-m", "pytest"]}))

    def test_a_project_with_the_extension_has_its_xcode_lane_refused_when_its_configuration_is_read(self):
        source = json.dumps({"schemaVersion": 1, "name": "App", "extensions": ["queuekeeper_apple"],
                             "lanes": {"ui": {"command": ["xcodebuild", "test"], "sandbox": True}}})
        with self.assertRaisesRegex(ValueError, "ui.sandbox cannot be: its command runs xcodebuild"):
            config.parse(source)
        self.assertIn(settings.sandbox_refusal, extensions.SANDBOX_REFUSALS)


if __name__ == "__main__":
    unittest.main()
