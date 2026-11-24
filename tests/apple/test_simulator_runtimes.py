"""The Apple plugin's plan for giving two Macs the same simulator runtimes (queuekeeper_apple.simulators.runtimes).
tests/integration/test_remote.py's RemoteRuntimeTests run it end to end through `qk remote setup --runtimes`."""
import contextlib
from io import StringIO
import unittest
from unittest.mock import patch

import host_isolation  # noqa: F401 -- first, as every test module
from queuekeeper import plugins, remote
from queuekeeper_apple import doctor
from queuekeeper_apple.simulators import runtimes


def image(build, **record):
    return {"build": build, "file": f"/images/{build}.dmg", "sizeBytes": 1 << 30, "architectures": ["arm64"],
            "unreadable": None, "users": ["Example iPhone 1"], "state": "Ready", **record}


class PlanTests(unittest.TestCase):
    def plan(self, here, there, here_images, there_images):
        found = {runtimes.HERE: {"runtimes": here}, "fake": {"runtimes": there}}
        stores = {runtimes.HERE: {"images": here_images}, "fake": {"images": there_images}}
        return runtimes.runtime_plan(found, stores, "fake")

    def test_each_runtime_one_mac_lacks_is_copied_deleted_downloaded_or_named(self):
        plan = self.plan(["iOS 27.0 (A)", "iOS 27.1 (B)", "iOS 27.2 (C)", "xrOS 3.0 (D)", "Oddly named"], [],
                         [image("A"), image("B", users=[]),
                          image("C", file=None, unreadable="its image is not where simctl says"),
                          image("D", file=None, unreadable="no image")], [])
        how = {step["runtime"]: step["how"] for step in plan}
        self.assertEqual(how, {"iOS 27.0 (A)": "copy", "iOS 27.1 (B)": "delete", "iOS 27.2 (C)": "download",
                               "xrOS 3.0 (D)": None, "Oddly named": None})
        download = next(step for step in plan if step["how"] == "download")
        # Core remote.download_runtime runs the command the plan names.
        self.assertEqual(download["command"], ["xcodebuild", "-downloadPlatform", "iOS", "-buildVersion", "C",
                                               "-architectureVariant", "arm64"])
        self.assertEqual(download["command"], runtimes.download_command(download))
        self.assertIn("xcodebuild downloads no xrOS runtime", next(s for s in plan if s["build"] == "D")["why"])

    def test_the_download_command_follows_the_images_architectures(self):
        step = {"platform": "iOS", "build": "C"}
        for architectures, variant in [(["arm64"], ["-architectureVariant", "arm64"]),
                                       (["arm64", "x86_64"], ["-architectureVariant", "universal"]),
                                       ([], [])]:
            with self.subTest(architectures):
                self.assertEqual(runtimes.download_command({**step, "architectures": architectures}),
                                 ["xcodebuild", "-downloadPlatform", "iOS", "-buildVersion", "C", *variant])

    def test_differences_name_what_only_each_mac_lists(self):
        self.assertEqual(runtimes.runtime_differences(["a", "b"], ["b", "c"], "fake"),
                         ["only this Mac has a", "only fake has c"])
        self.assertEqual(runtimes.runtime_differences(["a"], ["a"], "fake"), [])


class RegistrationTests(unittest.TestCase):
    def test_the_apple_plugin_answers_remote_setup_runtimes_once(self):
        calls = []
        with patch.object(plugins, "REMOTE_SETUP_RUNTIMES", []), \
                patch.object(runtimes, "match_runtimes", side_effect=lambda host, check: calls.append((host, check)) or 0):
            doctor.register()
            doctor.register()
            self.assertEqual(plugins.REMOTE_SETUP_RUNTIMES, [runtimes.match_runtimes])
            self.assertEqual(remote.setup("fake", [], [], True, runtimes=True), 0)
        self.assertEqual(calls, [("fake", True)])

    def test_the_apple_plugin_registers_with_a_core_that_has_no_hook(self):
        # An older core evens out the runtimes itself and has no REMOTE_SETUP_RUNTIMES.
        with patch.object(plugins, "REMOTE_SETUP_RUNTIMES", []):
            del plugins.REMOTE_SETUP_RUNTIMES
            doctor.register()
            self.assertFalse(hasattr(plugins, "REMOTE_SETUP_RUNTIMES"))
            plugins.REMOTE_SETUP_RUNTIMES = []

    def test_without_a_plugin_that_provides_it_core_refuses_runtimes(self):
        with patch.object(plugins, "REMOTE_SETUP_RUNTIMES", []), contextlib.redirect_stdout(StringIO()) as output:
            with self.assertRaisesRegex(ValueError, "no plugin loaded here provides it"):
                remote.setup("fake", [], [], False, runtimes=True)
        self.assertEqual(output.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
