"""Which simulators an admitted command names, which keep it on this Mac (queuekeeper_apple.simulators.commands)."""
import sys
import unittest
from unittest.mock import patch

import host_isolation  # noqa: F401 -- first, as every test module
from queuekeeper import plugins
from queuekeeper_apple import lane
from queuekeeper_apple.simulators import commands
from test_support.devices import DUO


class SimulatorPositionTests(unittest.TestCase):
    """admit --verify keeps a command here for a UUID only where a simulator goes (#142)."""

    def test_a_uuid_in_a_simulator_position_names_a_simulator(self):
        for command in [["xcodebuild", "-destination", f"id={DUO.lower()}"],
                        ["xcodebuild", "-destination", f"platform=iOS Simulator,id={DUO}"],
                        ["xcodebuild", "-destination", f"'platform=iOS Simulator,id={DUO},arch=arm64'"],
                        [f"-destination=id={DUO}"],
                        ["xcrun", "simctl", "boot", DUO.lower()],
                        ["xcrun simctl boot", DUO],
                        ["/usr/bin/xcrun", "simctl", "spawn", f'"{DUO}"', "launchctl", "list"],
                        ["/bin/sh", "-c", f"xcrun simctl shutdown {DUO}; true"],
                        ["tool", "--device", DUO.lower()],
                        ["tool", f"--device={DUO}"]]:
            with self.subTest(command):
                self.assertEqual(commands.simulators(command), {DUO})

    def test_a_uuid_elsewhere_names_no_simulator(self):
        scratchpad = f"/private/tmp/claude-501/-Users-owner-project/{DUO.lower()}/scratchpad/venv/bin/python"
        for command in [[scratchpad, "-m", "unittest"],
                        [sys.executable, f"/tmp/{DUO}/out.json"],
                        ["tool", f"{DUO}.xcresult"],
                        [f"--run={DUO}-1"],
                        [f"--build-id={DUO}"],
                        [f"session-{DUO}"],
                        [f"/tmp/id={DUO}"],
                        ["tool", DUO],
                        ["--devices", DUO],
                        ["--device", f"/tmp/{DUO}"],
                        # A word after simctl's command ends is another command's.
                        ["/bin/sh", "-c", f"xcrun simctl list && tool {DUO}"],
                        [scratchpad, "-m", "pytest", "-k", "simctl"]]:
            with self.subTest(command):
                self.assertEqual(commands.simulators(command), set())


class RegistrationTests(unittest.TestCase):
    def test_the_apple_plugin_keeps_a_command_naming_a_simulator_here_once(self):
        with patch.object(plugins, "COMMAND_KEPT_HERE", []):
            lane.register()
            lane.register()
            self.assertEqual(plugins.COMMAND_KEPT_HERE, [commands.kept_here])
            self.assertEqual(plugins.kept_here(["xcrun", "simctl", "boot", DUO]),
                             "it names a simulator by its identifier, a member of this Mac's pool")
            self.assertIsNone(plugins.kept_here(["tool", DUO]))

    def test_the_apple_plugin_registers_with_a_core_that_has_no_hook(self):
        # An older core recognizes simulators itself and has no COMMAND_KEPT_HERE.
        with patch.object(plugins, "COMMAND_KEPT_HERE", []):
            del plugins.COMMAND_KEPT_HERE
            lane.register()
            self.assertFalse(hasattr(plugins, "COMMAND_KEPT_HERE"))
            plugins.COMMAND_KEPT_HERE = []


if __name__ == "__main__":
    unittest.main()
