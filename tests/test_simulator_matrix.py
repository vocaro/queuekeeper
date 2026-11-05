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
import host_isolation
from host_isolation import stretched

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
    if tool == "xcresulttool":
        # The record the fake xcodebuild left in the bundle, as xcresulttool prints it; none, a failure.
        what, bundle = rest[1], pathlib.Path(rest[rest.index("--path") + 1])
        record = bundle / f"{what}.json"
        if not record.exists(): sys.exit(f"Error: {bundle} is not a result bundle")
        print(record.read_text())
        sys.exit(0)
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
    with (state / "xcodebuild-env.log").open("a") as log:
        log.write(json.dumps({"action": action, **{name: value for name, value in os.environ.items()
                                                   if "PROJECT_" in name}}) + "\\n")
    if action == "build-for-testing":
        if os.environ.get("FAKE_BUILD_FAIL"): sys.exit("** TEST BUILD FAILED **")
        # FAKE_BUILD_WAIT holds the build until a file of that name appears in FAKE_STATE.
        while os.environ.get("FAKE_BUILD_WAIT") and not (state / os.environ["FAKE_BUILD_WAIT"]).exists():
            time.sleep(0.05)
        # FAKE_STALE_BUILDS builds report a stale module, each after recording the module folder it found,
        # and FAKE_STALE_THEN_FAIL makes the build after them fail on something else.
        modules = pathlib.Path(value("-derivedDataPath")) / "Build/Intermediates.noindex/SwiftExplicitPrecompiledModules"
        builds = state / "builds"
        count = int(builds.read_text()) + 1 if builds.exists() else 1
        builds.write_text(str(count))
        with (state / "modules.log").open("a") as log: log.write(f"{modules.exists()}\\n")
        if count <= int(os.environ.get("FAKE_STALE_BUILDS", "0")):
            print("error: file 'include/resvg.h' has been modified since the module file 'm.pcm' was built")
            sys.exit("** TEST BUILD FAILED **")
        if os.environ.get("FAKE_STALE_THEN_FAIL"):
            print("error: cannot find 'resvg_options_load_font_faces' in scope")
            sys.exit("** TEST BUILD FAILED **")
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
    unmeasured = kind in os.environ.get("FAKE_UNMEASURED", "")
    if os.environ.get("FAKE_BUNDLE"):
        # The bundle's own record: three tests, testA skipped, unmeasured when asked.
        skip = {"name": "Test skipped - Unmeasured: the host was busy" if unmeasured else "Test skipped - planted",
                "nodeType": "Skip Message"}
        case = {"name": "testA()", "nodeIdentifier": "T/testA()", "nodeType": "Test Case", "result": "Skipped",
                "children": [skip]}
        (bundle / "summary.json").write_text(json.dumps({"result": "Passed", "totalTestCount": 3, "passedTests": 2,
                                                         "failedTests": 0, "skippedTests": 1}))
        (bundle / "tests.json").write_text(json.dumps({"testNodes": [{"name": "P", "nodeType": "Test Plan",
            "children": [{"name": "T", "nodeType": "Test Suite", "children": [case]}]}]}))
    if "YES" in arguments[arguments.index("-parallel-testing-enabled") + 1:][:1]:
        # Parallel testing: each test's outcome on a clone, no totals and no skip reason.
        for name, result in (("T.testA()", "skipped"), ("T.testB()", "passed"), ("T.testC()", "passed")):
            print(f"Test case '{name}' {result} on 'Clone 1 of {kind} - xctest (1)' (0.001 seconds)")
        sys.exit(65 if kind in os.environ.get("FAKE_TEST_FAIL", "") else 0)
    if unmeasured:
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
                            "VOCARO_DEVICE_RECORD": str(self.record), "VOCARO_DEVICE_OWNER": "matrix-tests",
                            # Devices that should meet wait for each other this long, stretched by load.
                            "FAKE_MEET_LIMIT": str(stretched(60))}

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
        # Each test has a derived data folder of its own beside its log, gone once it ends, so none is left
        # under ~/Library/Developer/Xcode/DerivedData (vocaro-engineering#158).
        scratches = [Path(call[call.index("-derivedDataPath") + 1]) for call in tests]
        self.assertEqual(sorted(path.name for path in scratches), ["iPad-Air-13-inch-M4.derived", "iPhone-17-Pro.derived"])
        self.assertFalse(any(path.exists() for path in scratches))
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

    def test_keep_state_takes_each_member_without_erasing_it(self):
        iphone = self.add("Project iPhone 17 Pro 1")
        ipad = self.add("Project iPad Air 13-inch (M4) 1", IPAD)
        status, output = self.run_matrix("--keep-state", FAKE_MEET="2")
        self.assertEqual(status, 0, output)
        simctl = (self.state / "simctl.log").read_text().splitlines()
        self.assertFalse([line for line in simctl if line.startswith(("erase ", "create "))], simctl)
        self.assertEqual(sorted(line.split()[1] for line in simctl if line.startswith("boot ")), sorted([iphone, ipad]))
        self.assertIn(f"TOOK Project iPhone 17 Pro 1 ({iphone}), not erased: it keeps its state", output)
        self.assertRegex(output, rf"BOOTED iPhone-17-Pro \({iphone}\) in \d+s")
        for key in ("Project iPhone 17 Pro 1", "Project iPad Air 13-inch (M4) 1"):
            self.assertEqual((self.entries()[key]["keptState"], self.entries()[key]["state"]), (True, "released"))
            self.assertIn("not erased", self.entries()[key]["preservation"])
        self.assert_every_device_returned()
        # The negative control: the same run without it erases both members.
        (self.state / "simctl.log").unlink()
        status, output = self.run_matrix(FAKE_MEET="2")
        self.assertEqual(status, 0, output)
        simctl = (self.state / "simctl.log").read_text().splitlines()
        self.assertEqual(sorted(line.split()[1] for line in simctl if line.startswith("erase ")), sorted([iphone, ipad]))
        self.assertNotIn("keptState", self.entries()["Project iPhone 17 Pro 1"])
        self.assert_every_device_returned()

    def planted_modules(self):
        derived = self.root / ".build/simulator-tests/derived"
        modules = derived / "Build/Intermediates.noindex/SwiftExplicitPrecompiledModules"
        modules.mkdir(parents=True)
        (modules / "resvg.pcm").write_text("old")
        kept = derived / "Build/Intermediates.noindex/Other.build"
        kept.mkdir()
        return modules, kept

    def build_calls(self):
        return [call for call in self.calls() if call[0] == "build-for-testing"]

    def module_folders_seen(self):
        return (self.state / "modules.log").read_text().split()

    def test_a_stale_module_clears_that_cache_and_rebuilds_once_then_passes(self):
        modules, kept = self.planted_modules()
        status, output = self.run_matrix(FAKE_MEET="2", FAKE_STALE_BUILDS="1")
        self.assertEqual(status, 0, output)
        self.assertEqual(len(self.build_calls()), 2, output)
        self.assertEqual(self.build_calls()[0], self.build_calls()[1])
        # The first build found the old modules, the rebuild none, and nothing else in derived data went.
        self.assertEqual(self.module_folders_seen(), ["True", "False"])
        self.assertFalse(modules.exists())
        self.assertTrue(kept.exists())
        # The log records that it happened, and the first build's log is kept beside the rebuild's.
        self.assertIn("STALE MODULES: the build failed", output)
        self.assertIn("removed", output)
        self.assertIn("the rebuild ended with status 0", output)
        logs = self.root / ".build/simulator-tests/logs"
        self.assertIn("has been modified since the module file", (logs / "build-stale-modules.log").read_text())
        self.assertNotIn("has been modified", (logs / "build.log").read_text())
        self.assertEqual(evidence.executed_tests(output), 6, output)
        self.assert_every_device_returned()

    def test_a_stale_module_rebuild_that_fails_otherwise_is_that_failure(self):
        self.planted_modules()
        status, output = self.run_matrix(FAKE_STALE_BUILDS="1", FAKE_STALE_THEN_FAIL="1")
        self.assertEqual(status, 1, output)
        self.assertEqual(len(self.build_calls()), 2, output)
        self.assertIn("the rebuild ended with status 1", output)
        self.assertNotIn("the same error again", output)
        self.assertIn("cannot find 'resvg_options_load_font_faces' in scope", output)
        self.assertIn("not tested: the build failed", output)
        self.assert_every_device_returned()

    def test_a_stale_module_that_comes_back_does_not_loop(self):
        self.planted_modules()
        status, output = self.run_matrix(FAKE_STALE_BUILDS="99")
        self.assertEqual(status, 1, output)
        self.assertEqual(len(self.build_calls()), 2, output)
        self.assertIn("the same error again, so it is not rebuilt again", output)
        self.assert_every_device_returned()

    def test_another_build_failure_does_not_rebuild_or_touch_the_modules(self):
        modules, _ = self.planted_modules()
        status, output = self.run_matrix(FAKE_BUILD_FAIL="1")
        self.assertEqual(status, 1, output)
        self.assertEqual(len(self.build_calls()), 1, output)
        self.assertTrue((modules / "resvg.pcm").exists())
        self.assertNotIn("STALE MODULES", output)

    def test_a_passing_build_that_mentions_a_stale_module_does_not_rebuild(self):
        modules, _ = self.planted_modules()
        (self.root / "bin/xcodebuild").write_text((self.root / "bin/xcodebuild").read_text().replace(
            'print("** TEST BUILD SUCCEEDED **")',
            'print("warning: has been modified since the module file"); print("** TEST BUILD SUCCEEDED **")'))
        status, output = self.run_matrix(FAKE_MEET="2")
        self.assertEqual(status, 0, output)
        self.assertEqual(len(self.build_calls()), 1, output)
        self.assertTrue(modules.exists())

    def test_a_forwarded_variable_reaches_each_test_run_unless_already_set(self):
        status, output = self.run_matrix("--forward-env", "PROJECT_", FAKE_MEET="2", PROJECT_CORPUS="/corpus",
                                         PROJECT_KEPT="from-prefix", TEST_RUNNER_PROJECT_KEPT="already-set",
                                         OTHER_PROJECT_VALUE="left-alone")
        self.assertEqual(status, 0, output)
        seen = [json.loads(line) for line in (self.state / "xcodebuild-env.log").read_text().splitlines()]
        tests = [entry for entry in seen if entry["action"] == "test-without-building"]
        self.assertEqual(len(tests), 2, seen)
        for entry in tests:
            self.assertEqual(entry["TEST_RUNNER_PROJECT_CORPUS"], "/corpus")
            self.assertEqual(entry["TEST_RUNNER_PROJECT_KEPT"], "already-set")
            # Negative control: a variable that merely contains the prefix does not start with it.
            self.assertNotIn("TEST_RUNNER_OTHER_PROJECT_VALUE", entry)
        # The log names what went on, and no value.
        self.assertIn("FORWARD PROJECT_: TEST_RUNNER_PROJECT_CORPUS", output)
        self.assertNotIn("/corpus", output)

    def test_without_forward_env_nothing_is_forwarded(self):
        status, output = self.run_matrix(FAKE_MEET="2", PROJECT_CORPUS="/corpus")
        self.assertEqual(status, 0, output)
        seen = [json.loads(line) for line in (self.state / "xcodebuild-env.log").read_text().splitlines()]
        self.assertFalse([entry for entry in seen if "TEST_RUNNER_PROJECT_CORPUS" in entry], seen)

    def test_an_empty_forward_prefix_is_refused(self):
        status, output = self.run_matrix("--forward-env", "")
        self.assertEqual(status, 2, output)
        self.assertIn("--forward-env needs a prefix", output)

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
        # Fixed: the 1 s boot timeout is the behavior under test, and the slow boot's 60 s outlasts it.
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

    def test_build_only_builds_for_the_first_device_and_boots_erases_and_tests_nothing(self):
        iphone = self.add("Project iPhone 17 Pro 1")
        status, output = self.run_matrix("--build-only", "--", "-only-testing:T/C")
        self.assertEqual(status, 0, output)
        self.assertEqual([call[0] for call in self.calls()], ["build-for-testing"])
        build = self.calls()[0]
        self.assertEqual(build[build.index("-destination") + 1], f"platform=iOS Simulator,id={iphone}")
        self.assertNotIn("-only-testing:T/C", build)
        simctl = (self.state / "simctl.log").read_text().splitlines()
        self.assertFalse([line for line in simctl if line.startswith(("boot ", "erase ", "create "))], simctl)
        self.assertNotIn("Project iPad Air 13-inch (M4) 1", self.devices())
        self.assertRegex(output, r"Simulator build \(wall-clock \d+s\): BUILT, no test run")
        self.assertEqual(self.entries()["Project iPhone 17 Pro 1"]["state"], "released")
        self.assert_every_device_returned()
        # The full run after it builds into the same derived data and tests on every device.
        status, output = self.run_matrix(FAKE_MEET="2")
        self.assertEqual(status, 0, output)
        self.assertEqual(evidence.executed_tests(output), 6, output)

    def test_a_failed_build_only_run_fails(self):
        self.add("Project iPhone 17 Pro 1")
        status, output = self.run_matrix("--build-only", FAKE_BUILD_FAIL="1")
        self.assertEqual(status, 1, output)
        self.assertIn("FAIL build: status 1", output)
        self.assertRegex(output, r"Simulator build \(wall-clock \d+s\): FAIL, no test run")
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
        deadline = time.monotonic() + stretched(60)
        while len(list(self.state.glob("testing-*"))) < 2:
            self.assertLess(time.monotonic(), deadline, "the devices never started testing")
            time.sleep(0.05)
        process.send_signal(signal.SIGTERM)
        output = process.communicate(timeout=stretched(60))[0]
        self.assertEqual(process.returncode, -signal.SIGTERM, output)
        self.assertIn("SIGTERM received; forwarding it", output)
        self.assert_every_device_returned()

    def test_a_run_waits_for_another_that_holds_its_derived_data_and_logs_but_not_for_one_elsewhere(self):
        # A build beside another run's tests copied the resources they read again (vocaro/EPUBLib#872).
        command = [sys.executable, str(ENG), "simulator-tests", "--scheme", "Package-Tests", "--runtime", "iOS-27-0",
                   "--device", "iPhone-17-Pro", "--device", "iPad-Air-13-inch-M4", "--pool", "Project"]
        def start(**environment):
            process = subprocess.Popen(command, cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       text=True, env={**self.environment, **environment})
            self.addCleanup(lambda: process.poll() is None and process.kill())
            return process
        first = start(FAKE_BUILD_WAIT="release")
        deadline = time.monotonic() + stretched(60)
        while not (self.state / "xcodebuild.log").exists():
            self.assertLess(time.monotonic(), deadline, "the first run never started its build")
            time.sleep(0.05)
        second = start()
        # A run with folders of its own goes ahead while the first holds the defaults.
        status, output = self.run_matrix("--derived-data", "elsewhere/derived", "--logs", "elsewhere/logs")
        self.assertEqual(status, 0, output)
        self.assertNotIn("WAIT:", output)
        def where(call):
            path = call[call.index("-derivedDataPath" if call[0] == "build-for-testing" else "-resultBundlePath") + 1]
            return ("elsewhere " if "elsewhere" in path else "") + call[0]
        self.assertEqual([where(call) for call in self.calls()],
                         ["build-for-testing", "elsewhere build-for-testing", *["elsewhere test-without-building"] * 2])
        self.assertIsNone(second.poll(), "the second run waits for the first, which is still building")
        (self.state / "release").touch()
        outputs = [process.communicate(timeout=stretched(60))[0] for process in (first, second)]
        self.assertEqual([first.returncode, second.returncode], [0, 0], outputs)
        self.assertNotIn("WAIT:", outputs[0])
        derived = self.root / ".build/simulator-tests/derived"
        self.assertRegex(outputs[1], rf"WAIT: another eng simulator-tests run holds {derived} \(PID {first.pid} since ")
        self.assertIn(f"HELD {derived} after waiting", outputs[1])
        # The second run built only once the first run's tests had ended.
        self.assertEqual([where(call) for call in self.calls()][4:],
                         [*["test-without-building"] * 2, "build-for-testing", *["test-without-building"] * 2])
        self.assert_every_device_returned()

    def test_a_run_waits_for_its_logs_folder_too(self):
        import fcntl
        logs = self.root / "logs"
        logs.mkdir()
        lock = (logs / ".eng-simulator-tests.lock").open("w")
        self.addCleanup(lock.close)
        fcntl.flock(lock, fcntl.LOCK_EX)
        command = [sys.executable, str(ENG), "simulator-tests", "--scheme", "Package-Tests", "--runtime", "iOS-27-0",
                   "--device", "iPhone-17-Pro", "--device", "iPad-Air-13-inch-M4", "--pool", "Project", "--logs", "logs"]
        process = subprocess.Popen(command, cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                   env=self.environment)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        time.sleep(1)
        self.assertIsNone(process.poll())
        self.assertFalse((self.state / "xcodebuild.log").exists(), "nothing builds while another run holds the logs")
        self.assertFalse((self.state / "simctl.log").exists() and "boot" in (self.state / "simctl.log").read_text(),
                         "and no device is booted for it")
        fcntl.flock(lock, fcntl.LOCK_UN)
        output = process.communicate(timeout=stretched(60))[0]
        self.assertEqual(process.returncode, 0, output)
        self.assertIn(f"WAIT: another eng simulator-tests run holds {logs} (it has not said which)", output)
        self.assertIn(f"HELD {logs} after waiting", output)
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
        self.assertEqual(self.run_matrix("--workers", "2", FAKE_BUNDLE="1")[0], 0)
        tests = [call for call in self.calls() if call[0] == "test-without-building"]
        self.assertTrue(all(call[call.index("-parallel-testing-worker-count") + 1] == "2" for call in tests))

    def test_with_parallel_workers_the_result_bundle_counts_each_skip_as_one(self):
        status, output = self.run_matrix("--workers", "2", FAKE_BUNDLE="1")
        self.assertEqual(status, 0, output)
        self.assertIn("PASS iPhone-17-Pro: Executed 2 tests, 1 skipped", output)
        self.assertEqual(evidence.executed_tests(output), 4, output)

    def test_with_parallel_workers_an_unmeasured_skip_the_console_does_not_name_leaves_the_run_unmeasured(self):
        status, output = self.run_matrix("--workers", "2", FAKE_BUNDLE="1", FAKE_UNMEASURED="iPad-Air-13-inch-M4")
        self.assertEqual(status, evidence.UNMEASURED, output)
        self.assertEqual(evidence.unmeasured_tests(output),
                         ["iPad-Air-13-inch-M4: -[T/testA()] : Test skipped - Unmeasured: the host was busy"])
        self.assertIn("PASS iPhone-17-Pro", output)

    def test_with_parallel_workers_and_no_readable_bundle_no_test_counts(self):
        # The console's per-test lines are not a count: a run whose bundle cannot be read establishes nothing.
        status, output = self.run_matrix("--workers", "2")
        self.assertEqual(status, 1, output)
        self.assertIn("FAIL iPhone-17-Pro: no tests ran (Executed 0 tests)", output)

    def test_a_skip_both_the_console_and_the_bundle_name_is_listed_once(self):
        status, output = self.run_matrix(FAKE_BUNDLE="1", FAKE_UNMEASURED="iPhone-17-Pro")
        self.assertEqual(status, evidence.UNMEASURED, output)
        self.assertEqual(evidence.unmeasured_tests(output),
                         ["iPhone-17-Pro: -[T/testA()] : Test skipped - Unmeasured: the host was busy"])

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
