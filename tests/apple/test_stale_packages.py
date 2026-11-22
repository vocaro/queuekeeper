import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import host_isolation
from host_isolation import stretched
from queuekeeper import offload_host, remote
from queuekeeper_apple import shims, stale_packages


class MovedCheckout(unittest.TestCase):
    def setUp(self):
        home = self.enterContext(tempfile.TemporaryDirectory(prefix="stale-packages-"))
        self.home = Path(home).resolve()
        self.farm = self.home / remote.ROOT
        self.old_farm = self.home / offload_host.EARLIER_ROOT
        self.checkout = self.farm / "checkouts/Example/2/Example"
        self.old = self.old_farm / "checkouts/Example/2/Example"
        self.state = self.checkout / ".build/derived/SourcePackages/workspace-state.json"
        self.tools = self.home / "tools"
        self.tools.mkdir()
        self.runs = self.home / "runs"
        for tool in ("xcodebuild", "swift"):
            host_isolation.executable(self.tools / tool, '#!/bin/sh\n'
                'echo ran >> "$FIXTURE_RUNS"\n'
                'if [ "$FIXTURE_STATUS" -ne 0 ]; then echo "error: missing dependency" >&2; fi\n'
                'exit "$FIXTURE_STATUS"\n')

    def state_at(self, state, path):
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps({"object": {"artifacts": [{"packageRef": {"location": str(path)}}]}}))

    def wrapped(self, tool, *arguments, status=65, scratch_override=""):
        variables = shims.environment({**os.environ, "HOME": str(self.home),
            "PATH": os.pathsep.join([str(self.tools), os.environ.get("PATH", os.defpath)]),
            "FIXTURE_RUNS": str(self.runs), "FIXTURE_STATUS": str(status),
            "SWIFTPM_BUILD_DIR": scratch_override}, self.checkout)
        return subprocess.run([str(shims.SHIMS / tool), *arguments], cwd=self.checkout,
                              env=variables, capture_output=True, text=True, timeout=stretched(60))

    def test_a_failed_xcode_build_names_the_old_checkout_even_through_the_farm_link(self):
        self.state_at(self.state, self.old / "Dependency")
        self.old_farm.symlink_to(self.farm)
        self.assertEqual(self.old.resolve(), self.checkout)
        before = self.state.read_bytes()
        finished = self.wrapped("xcodebuild", "build", "-derivedDataPath", ".build/derived")
        self.assertEqual(finished.returncode, 65, finished.stderr)
        self.assertIn(f"{stale_packages.NOTICE}: {self.state} still names the checkout at {self.old}", finished.stderr)
        self.assertEqual(self.runs.read_text().splitlines(), ["ran"])
        self.assertEqual(self.state.read_bytes(), before)

    def test_a_failed_swift_build_reads_its_explicit_scratch_state(self):
        state = self.checkout / ".build/release/workspace-state.json"
        self.state_at(state, self.old / "Dependency")
        finished = self.wrapped("swift", "test", "--scratch-path=.build/release")
        self.assertEqual(finished.returncode, 65, finished.stderr)
        self.assertIn(f"{stale_packages.NOTICE}: {state}", finished.stderr)
        self.assertTrue(state.is_file())
        overridden = self.wrapped("swift", "test", "--scratch-path=.build/release", scratch_override=".build/other")
        self.assertNotIn(stale_packages.NOTICE, overridden.stderr)

    def test_xcode_prefers_explicit_source_packages_to_derived_data(self):
        self.state_at(self.state, self.old / "Dependency")
        explicit = self.checkout / ".build/packages/workspace-state.json"
        self.state_at(explicit, self.checkout / "Dependency")
        arguments = ("build", "-derivedDataPath", ".build/derived", "-clonedSourcePackagesDirPath", ".build/packages")
        self.assertNotIn(stale_packages.NOTICE, self.wrapped("xcodebuild", *arguments).stderr)
        self.state_at(explicit, self.old / "Dependency")
        self.assertIn(f"{stale_packages.NOTICE}: {explicit}", self.wrapped("xcodebuild", *arguments).stderr)

    def test_success_and_unrelated_or_unreadable_package_state_do_not_warn(self):
        self.state_at(self.state, self.old / "Dependency")
        arguments = ("build", "-derivedDataPath", ".build/derived")
        finished = self.wrapped("xcodebuild", *arguments, status=0)
        self.assertEqual(finished.returncode, 0)
        self.assertNotIn(stale_packages.NOTICE, finished.stderr)
        for path in (self.checkout / "Dependency", self.home / "sibling/Dependency",
                     self.old.parent / "Example-sibling/Dependency", self.old.parent.parent / "1/Example/Dependency"):
            with self.subTest(path=path):
                self.state_at(self.state, path)
                finished = self.wrapped("xcodebuild", *arguments)
                self.assertEqual(finished.returncode, 65, finished.stderr)
                self.assertNotIn(stale_packages.NOTICE, finished.stderr)
        self.state.write_text("{")
        self.assertNotIn(stale_packages.NOTICE, self.wrapped("xcodebuild", *arguments).stderr)

    def test_package_state_outside_the_checkout_is_passed_over(self):
        outside = self.home / "other/workspace-state.json"
        self.state_at(outside, self.old / "Dependency")
        self.state_at(self.state, self.checkout / "Dependency")
        self.state.unlink()
        self.state.symlink_to(outside)
        before = outside.read_bytes()
        finished = self.wrapped("xcodebuild", "build", "-derivedDataPath", ".build/derived")
        self.assertEqual(finished.returncode, 65, finished.stderr)
        self.assertNotIn(stale_packages.NOTICE, finished.stderr)
        self.assertEqual(outside.read_bytes(), before)
