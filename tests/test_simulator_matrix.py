import datetime
import json
import os
import signal
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock
import uuid
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/python"))
from vocaro_engineering import devices, evidence

ENG = Path(__file__).resolve().parents[1] / "scripts/eng"

# Stand-ins for xcrun simctl and xcodebuild that record what they were asked to do in FAKE_STATE,
# where each simulator is a file of its name, type, runtime, state and last use. A device type
# named in FAKE_SLOW_BOOT, FAKE_TEST_FAIL, FAKE_UNMEASURED or FAKE_NO_TESTS boots slowly, fails its
# tests, reports an unmeasured test or runs none. Each test run waits until FAKE_MEET devices have
# started testing, or fails after FAKE_MEET_LIMIT seconds.
XCRUN = textwrap.dedent("""\
    #!/usr/bin/env python3
    import datetime, json, os, pathlib, sys, time, uuid
    state = pathlib.Path(os.environ["FAKE_STATE"])
    devices = state / "devices"
    devices.mkdir(parents=True, exist_ok=True)
    tool, action, *rest = sys.argv[1:]
    with (state / "simctl.log").open("a") as log: log.write(" ".join([action, *rest]) + "\\n")
    def device(udid): return json.loads((devices / udid).read_text())
    def change(udid, **values): (devices / udid).write_text(json.dumps({**device(udid), **values}))
    def kind(udid): return device(udid)["type"].rsplit(".", 1)[-1]
    if action == "create":
        udid = str(uuid.uuid4()).upper()
        (devices / udid).write_text(json.dumps({"name": rest[0], "type": rest[1], "runtime": rest[2],
                                                "state": "Shutdown"}))
        print(udid)
    elif action == "boot":
        if device(rest[0])["state"] != "Shutdown": sys.exit("Unable to boot device in current state: Booted")
        change(rest[0], state="Booted", lastUsedAt=datetime.datetime.now(datetime.timezone.utc).isoformat())
    elif action == "bootstatus" and kind(rest[0]) in os.environ.get("FAKE_SLOW_BOOT", ""):
        time.sleep(60)
    elif action == "shutdown":
        change(rest[0], state="Shutdown")
    elif action == "erase":
        if device(rest[0])["state"] != "Shutdown": sys.exit("Unable to erase contents and settings in current state")
    elif action == "delete":
        (devices / rest[0]).unlink()
    elif action == "list" and rest[0] == "devicetypes":
        print(json.dumps({"devicetypes": [{"identifier": "com.apple.CoreSimulator.SimDeviceType.iPhone-17-Pro",
                                           "name": "iPhone 17 Pro"},
                                          {"identifier": "com.apple.CoreSimulator.SimDeviceType.iPad-Air-13-inch-M4",
                                           "name": "iPad Air 13-inch (M4)"}]}))
    elif action == "list":
        listing = {}
        for path in devices.iterdir():
            value = json.loads(path.read_text())
            listing.setdefault(value["runtime"], []).append({"udid": path.name, "deviceTypeIdentifier": value["type"],
                                                             "isAvailable": True, **value})
        print(json.dumps({"devices": listing}))
""")
IPHONE, IPAD = "com.apple.CoreSimulator.SimDeviceType.iPhone-17-Pro", "com.apple.CoreSimulator.SimDeviceType.iPad-Air-13-inch-M4"
RUNTIME = "com.apple.CoreSimulator.SimRuntime.iOS-27-0"
XCODEBUILD = textwrap.dedent("""\
    #!/usr/bin/env python3
    import json, os, pathlib, sys, time
    state = pathlib.Path(os.environ["FAKE_STATE"])
    action, arguments = sys.argv[1], sys.argv[2:]
    value = lambda flag: arguments[arguments.index(flag) + 1]
    with (state / "xcodebuild.log").open("a") as log: log.write(json.dumps(sys.argv[1:]) + "\\n")
    if action == "build-for-testing":
        if os.environ.get("FAKE_BUILD_FAIL"): sys.exit("** TEST BUILD FAILED **")
        products = pathlib.Path(value("-derivedDataPath")) / "Build/Products"
        products.mkdir(parents=True, exist_ok=True)
        (products / f"{value('-scheme')}_iphonesimulator27.0-arm64.xctestrun").write_text("{}")
        print("** TEST BUILD SUCCEEDED **")
        sys.exit(0)
    udid = value("-destination").split("id=")[1]
    kind = json.loads((state / "devices" / udid).read_text())["type"].rsplit(".", 1)[-1]
    bundle = pathlib.Path(value("-resultBundlePath"))
    if bundle.exists(): sys.exit(f"Existing file at -resultBundlePath {bundle}")
    bundle.mkdir(parents=True)
    (state / f"testing-{udid}").touch()
    deadline = time.monotonic() + float(os.environ.get("FAKE_MEET_LIMIT", "60"))
    while len(list(state.glob("testing-*"))) < int(os.environ.get("FAKE_MEET", "1")):
        if time.monotonic() > deadline: sys.exit(f"{kind} never met the other devices")
        time.sleep(0.05)
    if kind in os.environ.get("FAKE_NO_TESTS", ""): sys.exit(0)
    skipped = os.environ.get("FAKE_SKIPPED", "0") if kind in os.environ.get("FAKE_SKIPPED_ON", "") else "0"
    if kind in os.environ.get("FAKE_UNMEASURED", ""):
        print("/p/T.swift:5: -[P.T testA] : Test skipped - Unmeasured: the host was busy")
    print("Test Suite 'All tests' passed at 2026-10-24 01:00:00.000.")
    print(f"\\t Executed 3 tests, with {skipped} tests skipped and 0 failures (0 unexpected) in 0.1 (0.1) seconds")
    sys.exit(65 if kind in os.environ.get("FAKE_TEST_FAIL", "") else 0)
""")


class SimulatorMatrixTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.state = self.root / "state"
        self.state.mkdir()
        tools = self.root / "bin"
        tools.mkdir()
        for name, source in (("xcrun", XCRUN), ("xcodebuild", XCODEBUILD)):
            (tools / name).write_text(source)
            (tools / name).chmod(0o755)
        self.record = self.root / "record/device-handoff.json"
        self.others = set()  # devices another session holds booted
        self.environment = {"PATH": f"{tools}:{Path(sys.executable).parent}:/usr/bin:/bin", "FAKE_STATE": str(self.state),
                            "VOCARO_DEVICE_RECORD": str(self.record), "VOCARO_DEVICE_OWNER": "matrix-tests"}

    def run_matrix(self, *arguments, **environment):
        command = [sys.executable, str(ENG), "simulator-tests", "--scheme", "Package-Tests", "--runtime", "iOS-27-0",
                   "--device", "iPhone-17-Pro", "--device", "iPad-Air-13-inch-M4", "--pool", "Project", *arguments]
        finished = subprocess.run(command, cwd=self.root, capture_output=True, text=True,
                                  env={**self.environment, **environment})
        return finished.returncode, finished.stdout + finished.stderr

    def calls(self):
        return [json.loads(line) for line in (self.state / "xcodebuild.log").read_text().splitlines()]

    def devices(self):
        """Each simulator by name: its UDID and the rest of its file."""
        return {value["name"]: {"udid": path.name, **value} for path in (self.state / "devices").iterdir()
                for value in [json.loads(path.read_text())]}

    def add(self, name, device_type=IPHONE, state="Shutdown", days=0):
        udid = str(uuid.uuid4()).upper()
        used = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)).isoformat()
        (self.state / "devices").mkdir(exist_ok=True)
        (self.state / "devices" / udid).write_text(json.dumps({"name": name, "type": device_type, "runtime": RUNTIME,
                                                               "state": state, "lastUsedAt": used}))
        return udid

    def entries(self):
        return json.loads(self.record.read_text())["devices"] if self.record.exists() else {}

    def assert_every_device_returned(self):
        """Every simulator is shut down and kept, and every reservation of this run released."""
        simctl = (self.state / "simctl.log").read_text().splitlines()
        self.assertFalse([line for line in simctl if line.startswith(("delete ", "rename "))], simctl)
        self.assertFalse(any("booted" in line.split() for line in simctl))
        self.assertTrue(all(device["state"] == "Shutdown" for name, device in self.devices().items()
                            if name not in self.others), self.devices())
        held = {key: entry for key, entry in self.entries().items() if entry.get("ownerChatID") == "matrix-tests"}
        self.assertEqual(held, {})

    def test_one_build_serves_every_device_and_the_devices_test_side_by_side(self):
        status, output = self.run_matrix("--", "-only-testing:T/C", FAKE_MEET="2")
        self.assertEqual(status, 0, output)
        self.assertEqual(evidence.executed_tests(output), 6, output)
        self.assertIn("PASS iPhone-17-Pro: Executed 3 tests", output)
        self.assertIn("PASS iPad-Air-13-inch-M4: Executed 3 tests", output)
        builds = [call for call in self.calls() if call[0] == "build-for-testing"]
        tests = [call for call in self.calls() if call[0] == "test-without-building"]
        self.assertEqual((len(builds), len(tests)), (1, 2))
        self.assertIn("-jobs", builds[0])
        self.assertNotIn("-only-testing:T/C", builds[0])
        self.assertTrue(all(call[-1] == "-only-testing:T/C" for call in tests))
        self.assertTrue(all(call[call.index("-parallel-testing-enabled") + 1] == "NO" for call in tests))
        # The build's destination is one of the devices, so it builds only that device's architecture.
        udids = {call[call.index("-destination") + 1] for call in tests}
        self.assertIn(builds[0][builds[0].index("-destination") + 1], udids)
        # An empty pool gets the first member of each kind, named after its device type, kept after the run.
        members = self.devices()
        self.assertEqual(sorted(members), ["Project iPad Air 13-inch (M4) 1", "Project iPhone 17 Pro 1"])
        self.assertIn("CREATED Project iPhone 17 Pro 1", output)
        self.assert_every_device_returned()
        self.assertEqual({key: entry["state"] for key, entry in self.entries().items()},
                         {name: "released" for name in members})
        # A second run reuses and erases both members, and replaces the first run's result bundles.
        for path in [*self.state.glob("testing-*"), self.state / "simctl.log"]: path.unlink()
        status, output = self.run_matrix(FAKE_MEET="2")
        self.assertEqual(status, 0, output)
        self.assertEqual(self.devices(), {name: {**member, "lastUsedAt": self.devices()[name]["lastUsedAt"]}
                                          for name, member in members.items()})
        simctl = (self.state / "simctl.log").read_text().splitlines()
        self.assertFalse([line for line in simctl if line.startswith("create ")], simctl)
        self.assertEqual(sorted(line.split()[1] for line in simctl if line.startswith("erase ")),
                         sorted(member["udid"] for member in members.values()))
        self.assertIn("TOOK Project iPhone 17 Pro 1", output)
        self.assert_every_device_returned()

    def test_a_booted_or_held_member_is_another_sessions_so_the_next_is_created(self):
        booted = self.add("Project iPhone 17 Pro 1", state="Booted")
        held = self.add("Project iPad Air 13-inch (M4) 1", IPAD)
        self.others = {"Project iPhone 17 Pro 1"}
        self.add_holder("Project iPad Air 13-inch (M4) 1", held, 0)
        status, output = self.run_matrix(FAKE_MEET="2")
        self.assertEqual(status, 0, output)
        self.assertEqual(sorted(self.devices()), ["Project iPad Air 13-inch (M4) 1", "Project iPad Air 13-inch (M4) 2",
                                                  "Project iPhone 17 Pro 1", "Project iPhone 17 Pro 2"])
        simctl = (self.state / "simctl.log").read_text()
        for udid in (booted, held): self.assertNotIn(udid, simctl)
        self.assertEqual(self.devices()["Project iPhone 17 Pro 1"]["state"], "Booted")
        self.assertEqual(self.entries()["Project iPad Air 13-inch (M4) 1"]["ownerChatID"], "another session")
        self.assert_every_device_returned()

    def test_members_unused_for_fourteen_days_are_pruned_but_never_the_lowest_of_a_kind(self):
        lowest = self.add("Project iPhone 17 Pro 1", days=30)
        busy = self.add("Project iPhone 17 Pro 2", state="Booted", days=30)
        recent = self.add("Project iPhone 17 Pro 3", days=3)
        stale = self.add("Project iPhone 17 Pro 4", days=15)
        older = self.add("Project iPhone 17 Pro 5", days=20)
        ipad = self.add("Project iPad Air 13-inch (M4) 7", IPAD, days=20)
        self.others = {"Project iPhone 17 Pro 2"}
        status, output = self.run_matrix(FAKE_MEET="2")
        self.assertEqual(status, 0, output)
        simctl = (self.state / "simctl.log").read_text().splitlines()
        deleted = [line.split()[1] for line in simctl if line.startswith("delete ")]
        # The highest-numbered eligible member goes first; the only iPad is the lowest of its kind.
        self.assertEqual(deleted, [older, stale])
        self.assertEqual(sorted(self.devices()), ["Project iPad Air 13-inch (M4) 7", "Project iPhone 17 Pro 1",
                                                  "Project iPhone 17 Pro 2", "Project iPhone 17 Pro 3"])
        self.assertIn(f"PRUNED Project iPhone 17 Pro 5 ({older})", output)
        pruned = self.entries()["Project iPhone 17 Pro 4"]
        self.assertEqual((pruned["state"], pruned["pruned"]["udid"]), ("released", stale))
        self.assertIn("Pruned: deleted", pruned["run"])
        # The run took the free members left: the lowest iPhone and the iPad.
        self.assertIn("TOOK Project iPhone 17 Pro 1", output)
        self.assertIn("TOOK Project iPad Air 13-inch (M4) 7", output)
        self.assertNotIn(busy, " ".join(simctl))
        self.assertEqual(self.devices()["Project iPhone 17 Pro 3"]["udid"], recent)
        self.assertEqual(self.devices()["Project iPhone 17 Pro 1"]["udid"], lowest)
        # With no iPad free, a new member takes the lowest free number.
        for path in [*self.state.glob("testing-*"), self.state / "simctl.log"]: path.unlink()
        self.add("Project iPad Air 13-inch (M4) 3", IPAD, state="Booted")
        self.others.add("Project iPad Air 13-inch (M4) 3")
        held = self.entries()["Project iPad Air 13-inch (M4) 7"]
        self.assertEqual(held["state"], "released")
        self.add_holder("Project iPad Air 13-inch (M4) 7", ipad, held["revision"])
        status, output = self.run_matrix(FAKE_MEET="2")
        self.assertEqual(status, 0, output)
        self.assertIn("CREATED Project iPad Air 13-inch (M4) 1", output)
        self.assert_every_device_returned()

    def add_holder(self, key, udid, revision):
        """Another session's reservation of a member."""
        with mock.patch.dict(os.environ, {"VOCARO_DEVICE_RECORD": str(self.record)}):
            devices.Record().reserve(key, owner="another session", revision=revision, device=udid,
                                     purpose="Its own tests", release_by=(datetime.datetime.now(datetime.timezone.utc)
                                                                          + datetime.timedelta(hours=1)).isoformat())

    def test_one_device_at_a_time_cannot_meet_the_other(self):
        status, output = self.run_matrix("--jobs", "1", FAKE_MEET="2", FAKE_MEET_LIMIT="1")
        self.assertEqual(status, 1, output)
        self.assertIn("never met the other devices", output)
        self.assert_every_device_returned()

    def test_a_boot_past_its_limit_leaves_only_that_device_unmeasured(self):
        status, output = self.run_matrix("--boot-timeout", "1", FAKE_SLOW_BOOT="iPad-Air-13-inch-M4")
        self.assertEqual(status, 3, output)
        self.assertIn("UNMEASURED iPad-Air-13-inch-M4: the boot exceeded its limit", output)
        self.assertIn("PASS iPhone-17-Pro: Executed 3 tests", output)
        self.assertEqual(len([call for call in self.calls() if call[0] == "test-without-building"]), 1)
        self.assert_every_device_returned()

    def test_a_failed_build_tests_no_device(self):
        status, output = self.run_matrix(FAKE_BUILD_FAIL="1")
        self.assertEqual(status, 1, output)
        self.assertIn("FAIL build: status 1", output)
        self.assertIn("FAIL iPhone-17-Pro: not tested: the build failed", output)
        self.assertEqual([call[0] for call in self.calls()], ["build-for-testing"])
        self.assert_every_device_returned()

    def test_any_device_failure_fails_the_run_and_otherwise_an_unmeasured_test_leaves_it_unmeasured(self):
        cases = [({"FAKE_UNMEASURED": "iPhone-17-Pro"}, 3), ({"FAKE_UNMEASURED": "iPhone-17-Pro",
                  "FAKE_TEST_FAIL": "iPad-Air-13-inch-M4"}, 1), ({"FAKE_NO_TESTS": "iPad-Air-13-inch-M4"}, 1)]
        for environment, expected in cases:
            with self.subTest(environment):
                for path in self.state.glob("testing-*"): path.unlink()
                (self.state / "simctl.log").unlink(missing_ok=True)
                status, output = self.run_matrix(**environment)
                self.assertEqual(status, expected, output)
                self.assert_every_device_returned()
        self.assertIn("FAIL iPad-Air-13-inch-M4: no tests ran", output)
        status, output = self.run_matrix(FAKE_UNMEASURED="iPhone-17-Pro")
        self.assertEqual(len(evidence.unmeasured_tests(output)), 1, output)

    def test_a_stop_ends_every_device_run_and_returns_every_device_to_the_pool(self):
        command = [sys.executable, str(ENG), "simulator-tests", "--scheme", "Package-Tests", "--runtime", "iOS-27-0",
                   "--device", "iPhone-17-Pro", "--device", "iPad-Air-13-inch-M4", "--pool", "Project"]
        # Three devices never meet, so both runs wait until the stop.
        process = subprocess.Popen(command, cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                   env={**self.environment, "FAKE_MEET": "3"})
        self.addCleanup(lambda: process.poll() is None and process.kill())
        deadline = time.monotonic() + 60
        while len(list(self.state.glob("testing-*"))) < 2:
            self.assertLess(time.monotonic(), deadline, "the devices never started testing")
            time.sleep(0.05)
        process.send_signal(signal.SIGTERM)
        output = process.communicate(timeout=60)[0]
        self.assertEqual(process.returncode, -signal.SIGTERM, output)
        self.assertIn("SIGTERM received; forwarding it", output)
        self.assert_every_device_returned()

    def test_skipped_tests_ran_nothing_so_a_device_whose_tests_all_skipped_fails(self):
        status, output = self.run_matrix(FAKE_SKIPPED="2", FAKE_SKIPPED_ON="iPad-Air-13-inch-M4")
        self.assertEqual(status, 0, output)
        self.assertIn("PASS iPad-Air-13-inch-M4: Executed 1 tests, 2 skipped", output)
        self.assertEqual(evidence.executed_tests(output), 4, output)
        for path in [*self.state.glob("testing-*"), self.state / "simctl.log"]: path.unlink()
        status, output = self.run_matrix(FAKE_SKIPPED="3", FAKE_SKIPPED_ON="iPad-Air-13-inch-M4")
        self.assertEqual(status, 1, output)
        self.assertIn("FAIL iPad-Air-13-inch-M4: no tests ran (Executed 0 tests, 3 skipped)", output)
        self.assert_every_device_returned()

    def test_each_test_has_xctests_time_allowance(self):
        self.assertEqual(self.run_matrix("--test-allowance", "120")[0], 0)
        tests = [call for call in self.calls() if call[0] == "test-without-building"]
        self.assertEqual(len(tests), 2)
        for call in tests:
            self.assertEqual(call[call.index("-test-timeouts-enabled") + 1], "YES")
            self.assertEqual(call[call.index("-default-test-execution-time-allowance") + 1], "120")

    def test_test_arguments_that_set_their_own_time_allowance_replace_engs(self):
        self.assertEqual(self.run_matrix("--", "-test-timeouts-enabled", "NO")[0], 0)
        tests = [call for call in self.calls() if call[0] == "test-without-building"]
        self.assertTrue(all(call.count("-test-timeouts-enabled") == 1 and call[-1] == "NO" for call in tests))
        self.assertFalse(any("-default-test-execution-time-allowance" in call for call in tests))

    def test_parallel_workers_on_each_device_are_opt_in(self):
        self.assertEqual(self.run_matrix("--workers", "2")[0], 0)
        tests = [call for call in self.calls() if call[0] == "test-without-building"]
        self.assertTrue(all(call[call.index("-parallel-testing-worker-count") + 1] == "2" for call in tests))

    def test_a_lane_that_ends_with_its_separator_takes_a_filter_from_eng_run(self):
        matrix = [sys.executable, str(ENG), "simulator-tests", "--scheme", "Package-Tests", "--runtime", "iOS-27-0",
                  "--device", "iPhone-17-Pro", "--device", "iPad-Air-13-inch-M4"]
        # The pool's name comes from the project's configuration.
        (self.root / "engineering.json").write_text(json.dumps({"schemaVersion": 1, "name": "Project",
            "capabilities": ["simulator-pool"], "simulatorPool": "Project", "lanes": {
            "ios-tests": {"command": [*matrix, "--"], "minimumTests": 6}, "unseparated": {"command": matrix}}}))
        run = lambda lane: subprocess.run([sys.executable, str(ENG), "--project", str(self.root), "run", lane, "--",
                                           "-only-testing:T/C"], cwd=self.root, capture_output=True, text=True,
                                          env=self.environment)
        finished = run("ios-tests")
        self.assertEqual(finished.returncode, 0, finished.stdout + finished.stderr)
        tests = [call for call in self.calls() if call[0] == "test-without-building"]
        self.assertEqual([call[-1] for call in tests], ["-only-testing:T/C"] * 2)
        self.assertEqual(sorted(self.devices()), ["Project iPad Air 13-inch (M4) 1", "Project iPhone 17 Pro 1"])
        self.assert_every_device_returned()
        # Without the separator the filter would reach simulator-tests' own options, which refuse it.
        (self.state / "simctl.log").unlink()
        finished = run("unseparated")
        self.assertEqual(finished.returncode, 2, finished.stdout + finished.stderr)
        self.assertIn("a lane whose command runs simulator-tests ends with \"--\"",
                      (self.root / ".build/engineering/unseparated/run.log").read_text())
        self.assertFalse((self.state / "simctl.log").exists())

    def test_a_run_without_a_pool_name_refuses_before_taking_a_device(self):
        command = [sys.executable, str(ENG), "simulator-tests", "--scheme", "Package-Tests", "--runtime", "iOS-27-0",
                   "--device", "iPhone-17-Pro"]
        finished = subprocess.run(command, cwd=self.root, capture_output=True, text=True, env=self.environment)
        self.assertEqual(finished.returncode, 2, finished.stdout + finished.stderr)
        self.assertIn("name the simulator pool", finished.stderr)
        self.assertFalse((self.state / "simctl.log").exists())

    def test_invalid_arguments_create_no_device(self):
        for arguments in (["--jobs", "0"], ["--device", "iPhone-17-Pro"], ["--build-jobs", "0"]):
            with self.subTest(arguments):
                self.assertEqual(self.run_matrix(*arguments)[0], 2)
        self.assertFalse((self.state / "simctl.log").exists())


if __name__ == "__main__":
    unittest.main()
