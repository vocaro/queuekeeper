import hashlib
import json
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/python"))
from vocaro_engineering.simulators.runtime_identity import RuntimeIdentityUnavailable, runtime_for_device
import host_isolation


class RuntimeIdentityTests(unittest.TestCase):
    UDID = "ABCDEF00-0000-0000-0000-000000000005"
    RUNTIME = "com.apple.CoreSimulator.SimRuntime.iOS-99-1"

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "iOS.simruntime/Contents/Resources/RuntimeRoot"
        self.path = self.root / "System/Library/CoreServices/SystemVersion.plist"
        self.path.parent.mkdir(parents=True)
        self.system = {"ProductName": "iPhone OS", "ProductVersion": "99.1", "ProductBuildVersion": "new-build",
                       "BuildID": "synthetic-build", "SystemImageID": "synthetic-image"}
        self.path.write_bytes(plistlib.dumps(self.system))
        self.catalog = [{"identifier": self.RUNTIME, "version": "99.1", "buildversion": build}
                        for build in ("old-build", "new-build")]
        self.device = {"udid": self.UDID, "state": "Booted", "isAvailable": True}
        self.devices = {self.RUNTIME: [self.device]}
        self.calls = []

    def query(self, *arguments):
        self.calls.append(arguments)
        if arguments == ("list", "devices", "-j"): return json.dumps({"devices": self.devices})
        if arguments == ("list", "runtimes", "-j"): return json.dumps({"runtimes": self.catalog})
        if arguments == ("getenv", self.UDID, "SIMULATOR_ROOT"): return str(self.root)
        self.fail(f"unexpected mutation or query: {arguments}")

    def identify(self):
        return runtime_for_device(self.UDID.lower(), self.RUNTIME, run=self.query)

    def test_duplicate_old_first_catalog_preserves_both_and_fingerprints_actual_guest(self):
        identity = self.identify()
        self.assertEqual(identity["buildversion"], "new-build")
        self.assertEqual(identity["catalogCandidates"], self.catalog)
        self.assertEqual(identity["systemVersion"], self.system)
        self.assertEqual(identity["systemVersionSHA256"], hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertEqual(self.calls[-1], ("getenv", self.UDID, "SIMULATOR_ROOT"))

    def test_actual_guest_build_remains_authoritative_when_catalog_builds_are_stale(self):
        self.catalog = self.catalog[:1]
        self.assertEqual(self.identify()["buildversion"], "new-build")

    def test_wrong_runtime_shutdown_missing_or_duplicate_device_is_unavailable_before_getenv(self):
        cases = [{"other-runtime": [self.device]}, {}, {self.RUNTIME: [self.device, self.device]},
                 {self.RUNTIME: [dict(self.device, state="Shutdown")]},
                 {self.RUNTIME: [dict(self.device, isAvailable=False)]}]
        for devices in cases:
            with self.subTest(devices=devices):
                self.devices, self.calls = devices, []
                with self.assertRaises(RuntimeIdentityUnavailable): self.identify()
                self.assertFalse(any(call[0] == "getenv" for call in self.calls))

    def test_host_root_is_rejected_without_reading_host_plist(self):
        self.root = Path("/")
        with self.assertRaisesRegex(RuntimeIdentityUnavailable, "explicit simulator RuntimeRoot"):
            self.identify()

    def test_runtime_root_symlink_cannot_redirect_the_read_to_a_host_root(self):
        actual = self.root
        alias = actual.parents[3] / "alias.simruntime/Contents/Resources/RuntimeRoot"
        alias.parent.mkdir(parents=True)
        alias.symlink_to(actual, target_is_directory=True)
        self.root = alias
        self.assertEqual(self.identify()["runtimeRoot"], str(actual.resolve()))
        host = actual.parents[3] / "host"
        host.mkdir()
        self.root.unlink()
        self.root.symlink_to(host, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeIdentityUnavailable, "resolved SIMULATOR_ROOT"):
            self.identify()

    def test_host_product_name_or_wrong_version_cannot_establish_guest_identity(self):
        for changed in ({"ProductName": "macOS"}, {"ProductVersion": "1.0"}):
            with self.subTest(changed=changed):
                self.path.write_bytes(plistlib.dumps(dict(self.system, **changed)))
                with self.assertRaises(RuntimeIdentityUnavailable): self.identify()

    def test_missing_malformed_or_incomplete_guest_plist_is_unavailable(self):
        for raw in (b"not a plist", plistlib.dumps({"ProductName": "iPhone OS"}), None):
            with self.subTest(raw=raw):
                if raw is None: self.path.unlink()
                else: self.path.write_bytes(raw)
                with self.assertRaises(RuntimeIdentityUnavailable): self.identify()

    def test_simctl_failure_or_missing_catalog_is_unavailable(self):
        with self.assertRaises(RuntimeIdentityUnavailable):
            runtime_for_device(self.UDID, self.RUNTIME, run=lambda *args: (_ for _ in ()).throw(subprocess.CalledProcessError(1, args)))
        self.catalog = []
        with self.assertRaises(RuntimeIdentityUnavailable): self.identify()

    def test_booted_alias_is_rejected_before_any_simctl(self):
        with self.assertRaises(RuntimeIdentityUnavailable):
            runtime_for_device("booted", self.RUNTIME, run=self.query)
        self.assertEqual(self.calls, [])
