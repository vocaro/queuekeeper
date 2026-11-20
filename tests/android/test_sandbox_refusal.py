"""The Android plugin refuses a sandbox to a lane that runs adb or the emulator, and not to a Gradle build, which
runs in one given ~/.gradle and localhost."""
import json
import unittest

import host_isolation  # noqa: F401 (keeps the tests off the real hosts)
from queuekeeper import config, plugins
from queuekeeper_android import settings


class Refusal(unittest.TestCase):
    def test_adb_emulator_and_the_pools_commands_are_refused(self):
        for lane in ({"command": ["adb", "shell", "am", "instrument"]},
                     {"command": ["/opt/android/emulator/emulator", "-avd", "pixel"]},
                     {"command": ["scripts/qk", "emulator-tests", "--device", "pixel_8"]},
                     {"command": ["make"], "compileFirst": ["scripts/qk", "emulator-pool", "warm"]}):
            with self.subTest(lane=lane):
                self.assertIn("adb's server", settings.sandbox_refusal(lane))

    def test_a_gradle_build_is_not(self):
        self.assertIsNone(settings.sandbox_refusal({"command": ["./gradlew", "test", "--no-daemon"]}))

    def test_a_project_with_the_plugin_has_its_emulator_lane_refused_when_its_configuration_is_read(self):
        source = json.dumps({"schemaVersion": 1, "name": "App", "plugins": ["queuekeeper_android"],
                             "lanes": {"ui": {"command": ["scripts/qk", "emulator-tests"], "sandbox": True}}})
        with self.assertRaisesRegex(ValueError, "ui.sandbox cannot be: its command runs emulator-tests"):
            config.parse(source)
        self.assertIn(settings.sandbox_refusal, plugins.SANDBOX_REFUSALS)


if __name__ == "__main__":
    unittest.main()
