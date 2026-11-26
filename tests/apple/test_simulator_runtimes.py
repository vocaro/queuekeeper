"""The Apple plugin's plan for giving two Macs the same simulator runtimes (queuekeeper_apple.simulators.runtimes).
tests/integration/test_remote.py's RemoteRuntimeTests run it end to end through `qk remote setup --runtimes`."""
import ast
import base64
import contextlib
from io import StringIO
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zlib

import host_isolation  # first, as every test module
from queuekeeper import offload_host, plugins, remote
from queuekeeper_apple import doctor
from queuekeeper_apple.simulators import runtime_host, runtimes


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


class HostProgramTests(unittest.TestCase):
    """The runtime images' own host program (runtime_host.py), which core remote.ask sends whole to another Mac."""

    def test_another_macs_images_are_asked_of_the_plugins_own_program(self):
        sent = []
        def ssh(host, script, **options):
            sent.append(script)
            return subprocess.CompletedProcess(script, 0, stdout='{"images": [], "free": 1, "left": []}\n', stderr="")
        with patch.object(remote, "ssh", side_effect=ssh), patch.object(remote, "runtime_pins", return_value=["P"]):
            self.assertEqual(runtimes.runtime_images("fake", "fake", "/X.app"), {"images": [], "free": 1, "left": []})
        program, request = shlex.split(sent[0])[2:]
        packed = re.search(r"b64decode\('([^']+)'\)", program).group(1)
        self.assertEqual(zlib.decompress(base64.b64decode(packed)).decode(), runtimes.PROGRAM.read_text())
        self.assertEqual(json.loads(request), {"images": "/X.app", "staging": remote.RUNTIME_STAGING, "clear": False,
                                               "pinned": ["P"]})

    def test_a_delete_there_is_asked_of_the_plugins_program_and_here_runs_it_here(self):
        with patch.object(remote, "ask", return_value={"problem": None}) as ask, \
                patch.object(remote, "runtime_pins", return_value=["P"]):
            self.assertIsNone(runtimes.delete_runtime("fake", "fake", "/X.app", "B"))
        ask.assert_called_once_with("fake", {"deleteRuntime": {"developer": "/X.app", "build": "B", "pinned": ["P"]}},
                                    program=runtimes.PROGRAM)
        with patch.object(runtime_host, "delete_runtime", return_value="in use") as delete, \
                patch.object(remote, "runtime_pins", return_value=["P"]):
            self.assertEqual(runtimes.delete_runtime("fake", runtimes.HERE, "/X.app", "B"), "in use")
        delete.assert_called_once_with("/X.app", "B", ["P"])

    def test_the_host_program_imports_only_the_standard_library_and_runs_on_python_3_9(self):
        source = runtimes.PROGRAM.read_text()
        tree = ast.parse(source, feature_version=(3, 9))
        found = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import)
                 for alias in node.names}
        found |= {node.module.split(".")[0] for node in ast.walk(tree)
                  if isinstance(node, ast.ImportFrom) and node.level == 0}
        self.assertTrue(found <= sys.stdlib_module_names, found - sys.stdlib_module_names)
        self.assertFalse(any(isinstance(node, ast.ImportFrom) and node.level for node in ast.walk(tree)))


class MixedVersionTests(unittest.TestCase):
    """A core and an Apple plugin from either side of the move of the runtime images into the plugin."""

    def test_a_newer_core_refuses_an_older_plugins_reads_by_name(self):
        # An older plugin read the images through core remote and offload_host, which no longer hold them.
        for name in ("runtime_images", "send_runtime", "fetch_runtime"):
            with self.subTest(name):
                self.assertFalse(hasattr(remote, name))
                with self.assertRaisesRegex(AttributeError, "moved to the Apple plugin's simulators.runtimes; use the "
                                            "core and its plugins from the same Queuekeeper revision"):
                    getattr(remote, name)
        with self.assertRaisesRegex(AttributeError, "has no attribute 'no_such_name'"):
            remote.no_such_name

    def test_a_receiver_refuses_an_older_plugins_request_instead_of_running_it_as_a_plan(self):
        # An older plugin asks offload_host.py itself, which now answers none of these requests.
        for request, read in [({"deleteRuntime": {"developer": "/X.app", "build": "B"}}, "problem"),
                              ({"images": "/X.app", "staging": "s", "clear": False}, "error"),
                              ({"receive": {"staging": "s", "build": "B", "size": 1, "developerDir": "/X.app"}},
                               "failed")]:
            with self.subTest(next(iter(request))), tempfile.TemporaryDirectory() as home:
                done = subprocess.run([sys.executable, "-I", offload_host.__file__, json.dumps(request)],
                                      input="", capture_output=True, text=True, cwd=home,
                                      env={**os.environ, "HOME": home}, timeout=host_isolation.stretched(30))
                self.assertEqual(done.returncode, 0, done.stderr)
                answer = json.loads(done.stdout.strip().splitlines()[-1])
                self.assertIn("no longer answers", answer[read])
                self.assertIn("the Apple plugin answers it", answer[read])
                self.assertEqual(os.listdir(home), [])

    def test_the_plugin_asks_an_older_core_for_the_images_it_still_holds(self):
        # A core from before send_file lists, deletes and copies the images itself.
        calls = []
        record = lambda name, value=None: (lambda *arguments: calls.append((name, arguments)) or value)
        with patch.object(remote, "send_file"), \
                patch.object(remote, "runtime_images", create=True, side_effect=record("images", {"images": []})), \
                patch.object(remote, "send_runtime", create=True, side_effect=record("send")), \
                patch.object(remote, "fetch_runtime", create=True, side_effect=record("fetch")), \
                patch.object(offload_host, "delete_runtime", create=True, side_effect=record("delete here")), \
                patch.object(remote, "ask", side_effect=lambda *arguments, **options: calls.append(
                    ("ask", arguments, options)) or {"problem": None}), \
                patch.object(remote, "runtime_pins", return_value=[]):
            del remote.send_file
            self.assertTrue(runtimes.older_core())
            self.assertEqual(runtimes.runtime_images("fake", "fake", "/X.app"), {"images": []})
            runtimes.send_runtime("fake", image("B"), "/X.app", None)
            runtimes.fetch_runtime("fake", image("B"), "/X.app", None)
            runtimes.delete_runtime("fake", runtimes.HERE, "/X.app", "B")
            runtimes.delete_runtime("fake", "fake", "/X.app", "B")
        self.assertEqual([call[0] for call in calls], ["images", "send", "fetch", "delete here", "ask"])
        # The older core's own offload_host.py answers the delete there: no program of the plugin's is sent.
        self.assertEqual(calls[-1][2], {})
        self.assertFalse(runtimes.older_core())


if __name__ == "__main__":
    unittest.main()
