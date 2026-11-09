import datetime
import json
import os
import signal
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from unittest import mock
import uuid
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/python"))
from vocaro_engineering import devices, evidence, stale_modules
from vocaro_engineering.simulators import matrix
import host_isolation
from host_isolation import stretched

ENG = Path(__file__).resolve().parents[1] / "scripts/eng"


class Followed:
    """A started process's output, read as it comes, so that a test can wait for a line.

    A run that is to wait for a lock its test holds says so before it does anything else, so the test
    waits for that line before it lets go, not a fixed time: on a loaded host the interpreter alone can
    take longer than a second to start, and a lock let go before the run asks for it is never waited for
    (#180)."""

    def __init__(self, process):
        self.process, self.lines, self.changed = process, [], threading.Condition()
        self.reader = threading.Thread(target=self.read, daemon=True)
        self.reader.start()

    def read(self):
        for line in self.process.stdout:
            with self.changed:
                self.lines.append(line)
                self.changed.notify_all()
        with self.changed:
            self.lines.append(None)
            self.changed.notify_all()

    def wait_for(self, text, test):
        """Until a line holds text; fails the test if the output ends or a stretched minute passes first."""
        deadline = time.monotonic() + stretched(60)
        with self.changed:
            while not any(line is not None and text in line for line in self.lines):
                test.assertNotIn(None, self.lines, f"the run ended without saying {text!r}: {self.text()}")
                remaining = deadline - time.monotonic()
                test.assertGreater(remaining, 0, f"the run never said {text!r}: {self.text()}")
                self.changed.wait(remaining)

    def text(self):
        return "".join(line for line in self.lines if line is not None)

    def communicate(self, timeout):
        """All of the output once the process ends, as Popen.communicate's first value."""
        self.process.wait(timeout=timeout)
        self.reader.join(timeout=timeout)
        with self.changed: return self.text()

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
    if tool == "devicectl":
        # devicectl's audio and VoiceOver state of a simulator: volume 60 and VoiceOver off until set.
        verb, what, udid = rest[0], rest[1], rest[rest.index("--device") + 1]
        with (state / "devicectl.log").open("a") as log: log.write(" ".join(rest) + "\\n")
        path = devices / udid
        value = json.loads(path.read_text())
        if verb == "settings":
            if what == "audio": value["volume"] = int(rest[rest.index("--volume") + 1])
            else: value["voiceover"] = "--enable" in rest
            path.write_text(json.dumps(value))
        print(json.dumps({"result": {"volume": value.get("volume", 60)} if what == "audio" else
                                    {"enabled": value.get("voiceover", False)}}))
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
        if "defaults" in device(rest[0]): change(rest[0], defaults={})
    elif action == "spawn" and rest[1] == "defaults":
        # A setting in the simulator's preferences, which an erase clears; a kind in FAKE_DEFAULTS_IGNORED
        # takes the write and still reads unset.
        settings, key = device(rest[0]).get("defaults", {}), rest[3] + " " + rest[4]
        if rest[2] == "write":
            change(rest[0], defaults={**settings, key: {"true": "1", "false": "0"}.get(rest[-1], rest[-1])})
        elif key not in settings or kind(rest[0]) in os.environ.get("FAKE_DEFAULTS_IGNORED", ""):
            sys.exit(f"The domain/default pair of ({rest[3]}, {rest[4]}) does not exist")
        elif rest[2] == "delete":
            change(rest[0], defaults={name: value for name, value in settings.items() if name != key})
        else:
            print(settings[key])
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
# A stand-in for xcodegen that records its working directory and arguments, and fails with FAKE_XCODEGEN_FAIL.
XCODEGEN = textwrap.dedent("""\
    #!/usr/bin/env python3
    import json, os, pathlib, sys
    state = pathlib.Path(os.environ["FAKE_STATE"])
    with (state / "xcodegen.log").open("a") as log: log.write(json.dumps([os.getcwd(), *sys.argv[1:]]) + "\\n")
    if os.environ.get("FAKE_XCODEGEN_FAIL"): sys.exit("Spec validation error: a target has a missing source")
""")
IPHONE, IPAD = "com.apple.CoreSimulator.SimDeviceType.iPhone-17-Pro","com.apple.CoreSimulator.SimDeviceType.iPad-Air-13-inch-M4"
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
                                                   if "PROJECT_" in name or "SYSTEM_VIEWS" in name}}) + "\\n")
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
    # FAKE_TESTS: the bundle's tests, which -enumerate-tests lists and a test run with -only-testing counts.
    every = json.loads(os.environ.get("FAKE_TESTS", "[]"))
    only = [a.split(":", 1)[1] for a in arguments if a.startswith("-only-testing:")]
    skip = [a.split(":", 1)[1] for a in arguments if a.startswith("-skip-testing:")]
    chosen = lambda test, names: any(test == name or test.startswith(name + "/") for name in names)
    selected = [test for test in every if (not only or chosen(test, only)) and not chosen(test, skip)]
    if "-enumerate-tests" in arguments:
        if os.environ.get("FAKE_ENUMERATE_FAIL"): sys.exit("xcodebuild: error: enumeration failed")
        pathlib.Path(value("-test-enumeration-output-path")).write_text(json.dumps({"errors": [], "values": [{
            "enabledTests": [{"identifier": test} for test in selected],
            "disabledTests": [{"identifier": test} for test in every if test not in selected]}]}))
        (state / "listed").write_text(str(time.monotonic()))
        time.sleep(float(os.environ.get("FAKE_ENUMERATE_LINGER", "0")))
        sys.exit(0)
    if os.environ.get("FAKE_SHARD_STARTS"):
        with open(os.environ["FAKE_SHARD_STARTS"], "a") as starts: starts.write(f"{time.monotonic()}\\n")
    bundle = pathlib.Path(value("-resultBundlePath"))
    if bundle.exists(): sys.exit(f"Existing file at -resultBundlePath {bundle}")
    bundle.mkdir(parents=True)
    (state / f"testing-{udid}").touch()
    deadline = time.monotonic() + float(os.environ.get("FAKE_MEET_LIMIT", "60"))
    while len(list(state.glob("testing-*"))) < int(os.environ.get("FAKE_MEET", "1")):
        if time.monotonic() > deadline: sys.exit(f"{kind} never met the other devices")
        time.sleep(0.05)
    if kind in os.environ.get("FAKE_NO_TESTS", ""): sys.exit(0)
    # FAKE_FAIL_RUN and FAKE_UNMEASURED_RUN name one test run of a kind, KIND:N, the Nth to reach here.
    # Counted only when asked for: a series runs one test of a kind at a time, and shards of a kind do not.
    run_number = 0
    if os.environ.get("FAKE_FAIL_RUN") or os.environ.get("FAKE_UNMEASURED_RUN"):
        runs_file = state / f"runs-{kind}"
        run_number = int(runs_file.read_text()) + 1 if runs_file.exists() else 1
        runs_file.write_text(str(run_number))
    at_run = lambda name: f"{kind}:{run_number}" in os.environ.get(name, "").split(",")
    if os.environ.get("FAKE_VOICEOVER"):
        # A speech probe's phrase; on a kind in FAKE_VOICEOVER_LEFT_ON it leaves VoiceOver on, and on one in
        # FAKE_UNMUTED the simulator's volume comes back while it runs.
        print("VOICEOVER_EVENT 2026-11-06T00:00:00.000Z touch: One Button")
        record = state / "devices" / udid
        value = json.loads(record.read_text())
        if kind in os.environ.get("FAKE_VOICEOVER_LEFT_ON", ""): value["voiceover"] = True
        if kind in os.environ.get("FAKE_UNMUTED", ""): value["volume"] = 60
        record.write_text(json.dumps(value))
        if kind in os.environ.get("FAKE_UNMUTED", ""): time.sleep(3)
    skipped = os.environ.get("FAKE_SKIPPED", "0") if kind in os.environ.get("FAKE_SKIPPED_ON", "") else "0"
    unmeasured = kind in os.environ.get("FAKE_UNMEASURED", "") or at_run("FAKE_UNMEASURED_RUN")
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
    if os.environ.get("FAKE_RESULT_LINES"):
        # Each test's outcome and seconds, as Swift Testing and XCTest print them, among lines that give none.
        print('\\u25c7 Test "testB()" started.')
        print('\\u2714 Test "testB()" passed after 0.010 seconds.')
        print("Test Case '-[T.C testC]' started.")
        print("Test Case '-[T.C testC]' passed (0.020 seconds).")
        print("2026-11-06 16:16:45.147 xctest[1:2] noise that names no outcome")
    if unmeasured:
        print("/p/T.swift:5: -[P.T testA] : Test skipped - Unmeasured: the host was busy")
    print("Test Suite 'All tests' passed at 2026-10-24 01:00:00.000.")
    ran = len(selected) if every else 3
    failures = 1 if at_run("FAKE_FAIL_RUN") else 0
    print(f"\\t Executed {ran} tests, with {skipped} tests skipped and {failures} failures (0 unexpected) in 0.1 (0.1) seconds")
    sys.exit(65 if kind in os.environ.get("FAKE_TEST_FAIL", "") or failures else 0)
""")


def selecting_argument(argument):
    return argument.startswith(("-only-testing", "-skip-testing"))


class SimulatorMatrixTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.state = self.root / "state"
        self.state.mkdir()
        tools = self.root / "bin"
        tools.mkdir()
        for name, source in (("xcrun", XCRUN), ("xcodebuild", XCODEBUILD), ("xcodegen", XCODEGEN)):
            (tools / name).write_text(source)
            host_isolation.executable(tools / name)
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

    def run_series(self, *arguments, **environment):
        """A run of one device, the iPhone, so that its runs come in a known order."""
        command = [sys.executable, str(ENG), "simulator-tests", "--scheme", "Package-Tests", "--runtime", "iOS-27-0",
                   "--device", "iPhone-17-Pro", "--pool", "Project", *arguments]
        finished = subprocess.run(command, cwd=self.root, capture_output=True, text=True,
                                  env={**self.environment, **environment})
        return finished.returncode, finished.stdout + finished.stderr

    def series_record(self):
        return json.loads((self.root / ".build/simulator-tests/logs/series.json").read_text())

    def ran_tests(self):
        return [call for call in self.calls() if call[0] == "test-without-building"]

    def simctl_lines(self, verb):
        return [line for line in (self.state / "simctl.log").read_text().splitlines() if line.startswith(f"{verb} ")]

    def test_a_series_runs_every_repeat_on_a_freshly_erased_member_of_one_build(self):
        iphone = self.add("Project iPhone 17 Pro 1")
        status, output = self.run_series("--repeat", "3", "--erase")
        self.assertEqual(status, 0, output)
        self.assertEqual(len([call for call in self.calls() if call[0] == "build-for-testing"]), 1)
        self.assertEqual(len(self.ran_tests()), 3)
        self.assertEqual(self.simctl_lines("erase"), [f"erase {iphone}"] * 3)
        self.assertEqual(len(self.simctl_lines("shutdown")), 3, "each run's member goes back before the next take")
        self.assertEqual(self.entries()["Project iPhone 17 Pro 1"]["state"], "released")
        record = self.series_record()
        self.assertEqual([run["outcome"] for run in record["eachRun"]], ["passed"] * 3)
        self.assertEqual([run["reset"] for run in record["eachRun"]], ["erased"] * 3)
        self.assertEqual(record["runs"], {"passed": 3, "failed": 0, "skipped": 0, "unmeasured": 0})
        self.assertEqual(record["tests"], {"passed": 9, "failed": 0, "skipped": 0, "unmeasured": 0})
        for run in record["eachRun"]:
            self.assertIsInstance(run["load"]["start"], float)
            self.assertIsInstance(run["load"]["end"], float)
        self.assertIn("Repeat series: asked 3 repeats of iPhone-17-Pro (3 runs); runs: 3 passed, 0 failed, 0 skipped, "
                      "0 unmeasured; tests: 9 passed, 0 failed, 0 skipped, 0 unmeasured", output)
        self.assertIn("SERIES iPhone-17-Pro run 2 of 3: PASSED on Project iPhone 17 Pro 1", output)
        self.assertEqual(evidence.series_reported(output), 3)

    def test_each_of_a_series_runs_keeps_its_own_log_and_load_words(self):
        status, output = self.run_series("--repeat", "2")
        self.assertEqual(status, 0, output)
        self.assertTrue((self.root / ".build/simulator-tests/logs/iPhone-17-Pro-run1.log").exists())
        self.assertTrue((self.root / ".build/simulator-tests/logs/iPhone-17-Pro-run2.log").exists())
        self.assertRegex(output, r"run 1 of 2: PASSED .* load [\d.]+ at start, [\d.]+ at end")

    def test_one_failing_run_among_a_series_fails_it_and_keeps_the_others_apart(self):
        self.add("Project iPhone 17 Pro 1")
        status, output = self.run_series("--repeat", "3", "--erase", FAKE_FAIL_RUN="iPhone-17-Pro:2")
        self.assertEqual(status, 1, output)
        record = self.series_record()
        self.assertEqual([run["outcome"] for run in record["eachRun"]], ["passed", "failed", "passed"])
        self.assertEqual(record["eachRun"][1]["tests"], {"passed": 2, "failed": 1, "skipped": 0, "unmeasured": 0})
        self.assertEqual(record["runs"], {"passed": 2, "failed": 1, "skipped": 0, "unmeasured": 0})
        self.assertIn("runs: 2 passed, 1 failed, 0 skipped, 0 unmeasured", output)
        self.assertEqual(len(self.ran_tests()), 3, "a failure does not stop the series")

    def test_an_unmeasured_run_leaves_a_series_unmeasured_and_never_passes_it(self):
        self.add("Project iPhone 17 Pro 1")
        status, output = self.run_series("--repeat", "3", "--erase", FAKE_UNMEASURED_RUN="iPhone-17-Pro:2")
        self.assertEqual(status, evidence.UNMEASURED, output)
        record = self.series_record()
        self.assertEqual([run["outcome"] for run in record["eachRun"]], ["passed", "unmeasured", "passed"])
        self.assertEqual(record["runs"]["unmeasured"], 1)
        self.assertEqual(record["tests"]["unmeasured"], 1)
        self.assertEqual(record["tests"]["failed"], 0)

    def test_a_failed_build_ends_a_series_with_the_rest_unmeasured_not_passed(self):
        status, output = self.run_series("--repeat", "3", FAKE_BUILD_FAIL="1")
        self.assertEqual(status, 1, output)
        record = self.series_record()
        self.assertEqual([run["outcome"] for run in record["eachRun"]], ["failed", "unmeasured", "unmeasured"])
        self.assertEqual(record["eachRun"][2]["detail"], "this run did not happen")
        self.assertIsNone(record["eachRun"][1]["member"])
        self.assertIsNone(record["eachRun"][1]["load"])
        self.assertIn("load unread at start", output)

    def test_a_series_of_no_runs_is_refused_before_anything_is_built(self):
        for count in ("0", "-1"):
            with self.subTest(count):
                status, output = self.run_series("--repeat", count)
                self.assertEqual(status, 2, output)
                self.assertIn("--repeat needs at least 1", output)
                self.assertFalse((self.state / "xcodebuild.log").exists())

    def test_a_filter_that_selects_no_test_fails_each_of_a_series_runs(self):
        status, output = self.run_series("--repeat", "2", "--only", "T/none", FAKE_TESTS='["T/a", "T/b"]')
        self.assertEqual(status, 1, output)
        record = self.series_record()
        self.assertEqual([run["outcome"] for run in record["eachRun"]], ["failed", "failed"])
        self.assertIn("no tests ran", record["eachRun"][0]["detail"])
        self.assertEqual(record["tests"]["passed"], 0)

    def test_a_series_whose_tests_all_skip_reports_skipped_and_does_not_pass(self):
        status, output = self.run_series("--repeat", "2", FAKE_SKIPPED="3", FAKE_SKIPPED_ON="iPhone-17-Pro")
        self.assertEqual(status, 1, output)
        record = self.series_record()
        self.assertEqual([run["outcome"] for run in record["eachRun"]], ["skipped", "skipped"])
        self.assertEqual(record["runs"], {"passed": 0, "failed": 0, "skipped": 2, "unmeasured": 0})

    def test_a_series_that_asks_for_a_template_copy_and_gets_an_erase_runs_no_test(self):
        status, output = self.run_series("--repeat", "1", "--template")
        self.assertEqual(status, evidence.UNMEASURED, output)
        record = self.series_record()
        self.assertEqual(record["eachRun"][0]["outcome"], "unmeasured")
        self.assertIn("was asked for and the member was", record["eachRun"][0]["detail"])
        self.assertEqual(self.ran_tests(), [])

    def test_series_options_after_the_separator_are_the_lanes_and_its_only_replaces_the_selection(self):
        self.add("Project iPhone 17 Pro 1")
        status, output = self.run_series("--keep-state", "--", "-only-testing:T/lanes", "--repeat", "2", "--erase",
                                         "--only", "T/mine", "-test-iterations", "1")
        self.assertEqual(status, 0, output)
        record = self.series_record()
        self.assertEqual([run["reset"] for run in record["eachRun"]], ["erased", "erased"])
        for call in self.ran_tests():
            self.assertNotIn("--repeat", call)
            self.assertNotIn("--erase", call)
            self.assertIn("-only-testing:T/mine", call)
            self.assertNotIn("-only-testing:T/lanes", call)
            self.assertIn("-test-iterations", call)

    def test_a_series_sets_a_lanes_shards_aside_and_says_so(self):
        status, output = self.run_series("--shards", "2", "--repeat", "1")
        self.assertEqual(status, 0, output)
        self.assertIn("--shards 2 set aside", output)
        self.assertNotIn("LISTED", output)

    def test_no_repeat_asked_for_runs_once_and_reports_no_series(self):
        status, output = self.run_series()
        self.assertEqual(status, 0, output)
        self.assertEqual(len(self.ran_tests()), 1)
        self.assertNotIn("Repeat series", output)
        self.assertNotIn("SERIES", output)
        self.assertFalse((self.root / ".build/simulator-tests/logs/series.json").exists())
        self.assertIsNone(evidence.series_reported(output))

    def test_a_run_that_could_not_take_a_member_is_unmeasured_and_the_series_goes_on(self):
        from vocaro_engineering.simulators.pool import Lease
        leases = [Lease("Project iPhone 1", "U1", 1), RuntimeError("no member could be taken"), Lease("Project iPhone 1", "U1", 2)]
        taken = []
        class Pool:
            def take(self, *_, **__):
                next_lease = leases.pop(0)
                if isinstance(next_lease, Exception): raise next_lease
                taken.append(next_lease)
                return next_lease
            def release(self, *_, **__): pass
        args = matrix.parse(["--scheme", "S", "--runtime", "iOS-27-0", "--device", "iPhone-17-Pro",
                             "--logs", str(self.root / "logs"), "--repeat", "3", "--erase"])
        run = matrix.Run(args)
        run.say = lambda *lines: None
        run.logs.mkdir(parents=True)
        run.build = lambda destination: Path("built.xctestrun")
        run.device = lambda kind, device, built: run.finish(kind, 0, "Executed 1 tests")
        with mock.patch.object(matrix, "taken", wraps=matrix.taken) as taking:
            status = matrix.series(run, Pool(), args, ["iPhone-17-Pro"])
        self.assertEqual(status, evidence.UNMEASURED)
        self.assertEqual(taking.call_count, 3, "the failed take does not stop the series")
        self.assertEqual(len(taken), 2)
        record = json.loads((run.logs / "series.json").read_text())
        self.assertEqual([each["outcome"] for each in record["eachRun"]], ["passed", "unmeasured", "passed"])
        self.assertIn("no member could be taken", record["eachRun"][1]["detail"])
        self.assertEqual(record["runs"]["unmeasured"], 1)

    def test_a_run_with_no_test_counts_stays_apart_from_the_counts_of_a_series(self):
        self.assertIsNone(matrix.tally(65, 3, 0, 0, 0), "a crash's counts say nothing")
        self.assertEqual(matrix.tally(65, 3, 0, 0, 1), {"passed": 2, "failed": 1, "skipped": 0, "unmeasured": 0})
        # An unmeasured skip is counted once, as unmeasured, whether or not the skip count held it.
        self.assertEqual(matrix.tally(0, 2, 1, 1, 0), {"passed": 2, "failed": 0, "skipped": 0, "unmeasured": 1})
        self.assertEqual(matrix.tally(0, 3, 0, 1, 0), {"passed": 2, "failed": 0, "skipped": 0, "unmeasured": 1})
        self.assertEqual(matrix.run_outcome(None, None), "unmeasured")
        self.assertEqual(matrix.run_outcome(1, None), "failed")
        self.assertEqual(matrix.load_words(None), "load unread at start, unread at end")
        self.assertEqual(matrix.load_words({"start": 12.5, "end": None}), "load 12.5 at start, unread at end")

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
        # And --erase erases both on request, which their entries record.
        (self.state / "simctl.log").unlink()
        status, output = self.run_matrix("--erase", FAKE_MEET="2")
        self.assertEqual(status, 0, output)
        simctl = (self.state / "simctl.log").read_text().splitlines()
        self.assertEqual(sorted(line.split()[1] for line in simctl if line.startswith("erase ")), sorted([iphone, ipad]))
        self.assertIn(f"TOOK Project iPhone 17 Pro 1 ({iphone}), erased: asked for with --erase", output)
        self.assertEqual(self.entries()["Project iPhone 17 Pro 1"]["erasedBecause"], "asked for with --erase")
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

    def test_a_stale_module_the_lane_wrapper_already_rebuilt_is_not_rebuilt_again(self):
        # Under eng run, xcodebuild's wrapper has rebuilt once and says so in the build's output (#267).
        modules, _ = self.planted_modules()
        (self.root / "bin/xcodebuild").write_text((self.root / "bin/xcodebuild").read_text().replace(
            "print(\"error: file 'include/resvg.h'",
            f"print({stale_modules.NOTICE + ': the rerun of xcodebuild ended with status 65'!r}); "
            "print(\"error: file 'include/resvg.h'"))
        status, output = self.run_matrix(FAKE_STALE_BUILDS="99")
        self.assertEqual(status, 1, output)
        self.assertEqual(len(self.build_calls()), 1, output)
        self.assertTrue(modules.exists())
        self.assertNotIn("STALE MODULES: the build failed", output)

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

    def test_members_unused_for_an_hour_are_pruned_but_never_the_lowest_of_a_kind(self):
        lowest = self.add("Project iPhone 17 Pro 1", days=30)
        busy = self.add("Project iPhone 17 Pro 2", state="Booted", days=30)
        recent = self.add("Project iPhone 17 Pro 3", days=0.5 / 24)
        stale = self.add("Project iPhone 17 Pro 4", days=2 / 24)
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

    APP_ACCESSIBILITY = "com.apple.Accessibility ApplicationAccessibilityEnabled"

    def app_accessibility_lines(self, udid):
        """The simctl calls for the app accessibility setting on a device, and where each stands among its calls."""
        simctl = [line for line in (self.state / "simctl.log").read_text().splitlines() if udid in line.split()]
        return simctl, [line for line in simctl if line.startswith("spawn ") and self.APP_ACCESSIBILITY in line]

    def test_app_accessibility_is_switched_on_after_each_boot_and_before_the_tests(self):
        iphone = self.add("Project iPhone 17 Pro 1")
        ipad = self.add("Project iPad Air 13-inch (M4) 1", IPAD)
        status, output = self.run_matrix("--app-accessibility", FAKE_MEET="2")
        self.assertEqual(status, 0, output)
        for kind, udid in (("iPhone-17-Pro", iphone), ("iPad-Air-13-inch-M4", ipad)):
            simctl, setting = self.app_accessibility_lines(udid)
            self.assertEqual(setting, [f"spawn {udid} defaults write {self.APP_ACCESSIBILITY} -bool true",
                                       f"spawn {udid} defaults read {self.APP_ACCESSIBILITY}"])
            self.assertLess(simctl.index(f"bootstatus {udid} -b"), simctl.index(setting[0]))
            self.assertIn(f"APP ACCESSIBILITY {kind}: on ({self.APP_ACCESSIBILITY} reads 1)", output)
            # Before its tests start.
            self.assertLess(output.index(f"APP ACCESSIBILITY {kind}"), output.index(f"TEST {kind}"))
            self.assertIn(f"PASS {kind}: Executed 3 tests", output)
        # Each test process is told that the run promised system views, so that a golden read without them fails.
        seen = [json.loads(line) for line in (self.state / "xcodebuild-env.log").read_text().splitlines()]
        tests = [entry for entry in seen if entry["action"] == "test-without-building"]
        self.assertEqual([entry.get("TEST_RUNNER_ACCESSIBILITY_SNAPSHOT_SYSTEM_VIEWS") for entry in tests], ["1", "1"])
        self.assert_every_device_returned()
        # Without it nothing is written, and a member that kept an earlier holder's setting has it removed.
        (self.state / "simctl.log").unlink()
        (self.state / "xcodebuild-env.log").unlink()
        for path in self.state.glob("testing-*"): path.unlink()
        status, output = self.run_matrix("--keep-state", FAKE_MEET="2")
        self.assertEqual(status, 0, output)
        for udid in (iphone, ipad):
            _, setting = self.app_accessibility_lines(udid)
            self.assertEqual(setting, [f"spawn {udid} defaults delete {self.APP_ACCESSIBILITY}",
                                       f"spawn {udid} defaults read {self.APP_ACCESSIBILITY}"])
        self.assertNotIn("APP ACCESSIBILITY", output)
        (self.state / "simctl.log").unlink()
        for path in self.state.glob("testing-*"): path.unlink()
        status, output = self.run_matrix(FAKE_MEET="2")
        self.assertEqual(status, 0, output)
        self.assertEqual([self.app_accessibility_lines(udid)[1] for udid in (iphone, ipad)], [[], []])
        seen = [json.loads(line) for line in (self.state / "xcodebuild-env.log").read_text().splitlines()]
        self.assertFalse([entry for entry in seen if "TEST_RUNNER_ACCESSIBILITY_SNAPSHOT_SYSTEM_VIEWS" in entry], seen)
        self.assert_every_device_returned()

    def test_app_accessibility_that_does_not_read_on_fails_that_device_before_its_tests(self):
        # The negative control: the iPad's write is taken and the key still reads unset.
        status, output = self.run_matrix("--app-accessibility", FAKE_DEFAULTS_IGNORED="iPad-Air-13-inch-M4")
        self.assertEqual(status, 1, output)
        self.assertIn("FAIL iPad-Air-13-inch-M4: its app accessibility could not be switched on, so no test ran: "
                      f"{self.APP_ACCESSIBILITY} reads None after it was written true", output)
        self.assertIn("PASS iPhone-17-Pro: Executed 3 tests", output)
        tests = [call for call in self.calls() if call[0] == "test-without-building"]
        self.assertEqual(len(tests), 1)
        self.assert_every_device_returned()

    def devicectl(self, udid):
        log = self.state / "devicectl.log"
        return [line for line in (log.read_text().splitlines() if log.exists() else []) if f"--device {udid}" in line]

    def test_voiceover_silences_each_member_before_its_tests_and_fails_one_that_was_heard(self):
        iphone = self.add("Project iPhone 17 Pro 1")
        ipad = self.add("Project iPad Air 13-inch (M4) 1", IPAD)
        status, output = self.run_matrix("--voiceover", FAKE_MEET="2", FAKE_VOICEOVER="1",
                                         FAKE_UNMUTED="iPad-Air-13-inch-M4", FAKE_VOICEOVER_LEFT_ON="iPad-Air-13-inch-M4")
        self.assertEqual(status, 1, output)
        simctl = (self.state / "simctl.log").read_text().splitlines()
        for kind, udid in (("iPhone-17-Pro", iphone), ("iPad-Air-13-inch-M4", ipad)):
            # Cues off after the boot, then muted until twenty readings of 0, all before the tests.
            cues = f"spawn {udid} defaults write com.apple.VoiceOverTouch VoiceOverUseSoundEffects -bool false"
            self.assertLess(simctl.index(f"bootstatus {udid} -b"), simctl.index(cues))
            calls = self.devicectl(udid)
            self.assertEqual(calls[0], f"settings audio --device {udid} --volume 0")
            self.assertGreaterEqual(len([call for call in calls if call.startswith("info audio")]), 20)
            self.assertIn(f"MUTED {kind}: VoiceOver's sound cues off; the volume read 0 20 times in a row", output)
            self.assertLess(output.index(f"MUTED {kind}"), output.index(f"TEST {kind}"))
            # The probe's phrases reach the run's own output.
            self.assertIn(f"{kind} VOICEOVER_EVENT 2026-11-06T00:00:00.000Z touch: One Button", output)
        self.assertIn("PASS iPhone-17-Pro: Executed 3 tests", output)
        self.assertIn("VOICEOVER iPhone-17-Pro: 1 lines; VoiceOver off and the volume 0 after the tests", output)
        # The negative control: the iPad's volume came back while its tests ran and they left VoiceOver on.
        self.assertRegex(output, r"FAIL iPad-Air-13-inch-M4: the simulator was heard or left speaking: \S+ UNMUTED "
                                 r"the volume was 60; muted again; VoiceOver was left on; it has been switched off")
        self.assertFalse(self.devices()["Project iPad Air 13-inch (M4) 1"]["voiceover"])
        self.assert_every_device_returned()

    def test_voiceover_whose_cues_cannot_be_switched_off_runs_no_test_on_that_member(self):
        # The iPad's write is taken and the switch still reads unset, so its VoiceOver would play cues.
        status, output = self.run_matrix("--voiceover", FAKE_DEFAULTS_IGNORED="iPad-Air-13-inch-M4")
        self.assertEqual(status, 1, output)
        self.assertIn("FAIL iPad-Air-13-inch-M4: VoiceOver's sound cues could not be switched off", output)
        self.assertIn("PASS iPhone-17-Pro: Executed 3 tests", output)
        self.assertEqual(len([call for call in self.calls() if call[0] == "test-without-building"]), 1)
        self.assert_every_device_returned()

    def test_voiceover_runs_one_test_process_on_each_member(self):
        for option in (["--workers", "2"], ["--shards", "2"], ["--build-only"]):
            with self.subTest(option=option):
                status, output = self.run_matrix("--voiceover", *option)
                self.assertEqual(status, 2, output)
                self.assertIn("--voiceover runs one test process on each member", output)

    def test_xcodegen_generates_the_project_before_the_build_and_its_failure_fails_the_build(self):
        status, output = self.run_matrix("--xcode-project", "Hosted/Host.xcodeproj", "--xcodegen", "Hosted/project.yml",
                                         FAKE_MEET="2")
        self.assertEqual(status, 0, output)
        generated = [json.loads(line) for line in (self.state / "xcodegen.log").read_text().splitlines()]
        self.assertEqual(generated, [[str(self.root), "generate", "--quiet", "--spec", "Hosted/project.yml"]])
        self.assertLess(output.index("GENERATE Hosted/Host.xcodeproj from Hosted/project.yml"), output.index("BUILD "))
        build = [call for call in self.calls() if call[0] == "build-for-testing"][0]
        self.assertEqual(build[build.index("-project") + 1], "Hosted/Host.xcodeproj")
        self.assert_every_device_returned()
        # The negative control: a spec xcodegen refuses builds and tests nothing.
        (self.state / "xcodebuild.log").unlink()
        status, output = self.run_matrix("--xcode-project", "Hosted/Host.xcodeproj", "--xcodegen", "Hosted/project.yml",
                                         FAKE_XCODEGEN_FAIL="1")
        self.assertEqual(status, 1, output)
        self.assertIn("FAIL build: xcodegen ended with status 1", output)
        self.assertIn("a target has a missing source", output)
        self.assertFalse((self.state / "xcodebuild.log").exists())
        self.assert_every_device_returned()
        # It needs the project it generates.
        status, output = self.run_matrix("--xcodegen", "Hosted/project.yml")
        self.assertEqual(status, 2, output)
        self.assertIn("--xcodegen generates an Xcode project: name it with --xcode-project", output)

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
        # The second run asks for the folders before the first lets go of them, however slowly it starts.
        followed = Followed(second)
        followed.wait_for("WAIT: another eng simulator-tests run holds", self)
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
        outputs = [first.communicate(timeout=stretched(60))[0], followed.communicate(timeout=stretched(60))]
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
        followed = Followed(process)
        followed.wait_for(f"WAIT: another eng simulator-tests run holds {logs} (it has not said which)", self)
        self.assertIsNone(process.poll())
        self.assertFalse((self.state / "xcodebuild.log").exists(), "nothing builds while another run holds the logs")
        self.assertFalse((self.state / "simctl.log").exists() and "boot" in (self.state / "simctl.log").read_text(),
                         "and no device is booted for it")
        fcntl.flock(lock, fcntl.LOCK_UN)
        output = followed.communicate(timeout=stretched(60))
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

    SHARDED = ["T/A/testA()", "T/A/testB()", "T/B/testC()", "T/C/testD()", "T/D/testE()"]

    def shard_runs(self):
        return [call for call in self.calls() if call[0] == "test-without-building" and "-enumerate-tests" not in call]

    def test_shards_share_each_devices_tests_among_members_each_one_test_at_a_time(self):
        status, output = self.run_matrix("--shards", "2", FAKE_TESTS=json.dumps(self.SHARDED))
        self.assertEqual(status, 0, output)
        # Two members of each kind, each its own simulator, and each test once on each kind.
        self.assertEqual(sorted(self.devices()), ["Project iPad Air 13-inch (M4) 1", "Project iPad Air 13-inch (M4) 2",
                                                  "Project iPhone 17 Pro 1", "Project iPhone 17 Pro 2"])
        runs = self.shard_runs()
        self.assertEqual(len(runs), 4)
        for call in runs:
            self.assertEqual(call[call.index("-parallel-testing-enabled") + 1], "NO")
        by_kind = {}
        for call in runs:
            udid = call[call.index("-destination") + 1].split("id=")[1]
            kind = self.devices_by_udid()[udid]
            by_kind.setdefault(kind, []).append(sorted(a for a in call if a.startswith("-only-testing:")))
        for kind, shares in by_kind.items():
            self.assertTrue(all(shares), shares)
            self.assertEqual(sorted(a for share in shares for a in share),
                             ["-only-testing:T/A", "-only-testing:T/B", "-only-testing:T/C", "-only-testing:T/D"])
        self.assertIn("PASS iPhone-17-Pro: Executed 5 tests in 2 shards", output)
        # A lane counts each device once, not its shards.
        self.assertEqual(evidence.executed_tests(output), 10, output)
        listings = [call for call in self.calls() if "-enumerate-tests" in call]
        self.assertEqual(len(listings), 1)
        # The listing has its own derived data and result bundle, both gone once the run ends.
        scratch = Path(listings[0][listings[0].index("-derivedDataPath") + 1])
        self.assertEqual(scratch.name, "enumerate.derived")
        self.assertFalse(scratch.exists())
        self.assertFalse(Path(listings[0][listings[0].index("-resultBundlePath") + 1]).exists())
        self.assert_every_device_returned()

    def test_the_shards_start_once_the_listing_is_written_without_waiting_for_its_xcodebuild_to_end(self):
        # The fake listing's xcodebuild lingers 20 s after it writes the list; the shards must not wait for it.
        started = time.monotonic()
        status, output = self.run_matrix("--shards", "2", FAKE_TESTS=json.dumps(self.SHARDED), FAKE_ENUMERATE_LINGER="20",
                                         FAKE_SHARD_STARTS=str(self.state / "starts"))
        self.assertEqual(status, 0, output)
        starts = [float(line) for line in (self.state / "starts").read_text().split()]
        listed = float((self.state / "listed").read_text())
        self.assertLess(max(starts) - listed, 15, output)
        self.assertGreaterEqual(time.monotonic() - started, 20, "the run still ends after the listing's xcodebuild")

    def devices_by_udid(self):
        return {device["udid"]: device["type"].rsplit(".", 1)[-1] for device in self.devices().values()}

    def test_without_shards_each_device_runs_one_process_of_every_test(self):
        status, output = self.run_matrix(FAKE_TESTS=json.dumps(self.SHARDED))
        self.assertEqual(status, 0, output)
        self.assertEqual(len(self.devices()), 2)
        self.assertFalse([call for call in self.calls() if "-enumerate-tests" in call])
        self.assertFalse([a for call in self.shard_runs() for a in call if a.startswith("-only-testing")])

    def test_a_runs_own_selection_is_listed_and_a_partly_chosen_unit_is_shared_test_by_test(self):
        status, output = self.run_matrix("--shards", "2", "--", "-skip-testing:T/A/testB()", "-skip-testing:T/D",
                                         FAKE_TESTS=json.dumps(self.SHARDED))
        self.assertEqual(status, 0, output)
        listed = [call for call in self.calls() if "-enumerate-tests" in call][0]
        self.assertIn("-skip-testing:T/D", listed)
        shares = [a for call in self.shard_runs() for a in call if selecting_argument(a)]
        self.assertFalse([a for a in shares if a.startswith("-skip-testing")])
        iphone = sorted(a for call in self.shard_runs() for a in call if a.startswith("-only-testing")
                        and self.devices_by_udid()[call[call.index("-destination") + 1].split("id=")[1]] == "iPhone-17-Pro")
        self.assertEqual(iphone, ["-only-testing:T/A/testA()", "-only-testing:T/B", "-only-testing:T/C"])
        self.assertIn("PASS iPhone-17-Pro: Executed 3 tests in 2 shards", output)

    def test_a_shard_that_fails_fails_its_device(self):
        status, output = self.run_matrix("--shards", "2", FAKE_TESTS=json.dumps(self.SHARDED), FAKE_TEST_FAIL="iPad-Air-13-inch-M4")
        self.assertEqual(status, 1, output)
        self.assertIn("FAIL iPad-Air-13-inch-M4: Executed 5 tests in 2 shards", output)
        self.assertIn("PASS iPhone-17-Pro", output)

    def test_a_listing_that_fails_runs_nothing_and_fails(self):
        status, output = self.run_matrix("--shards", "2", FAKE_TESTS=json.dumps(self.SHARDED), FAKE_ENUMERATE_FAIL="1")
        self.assertEqual(status, 1, output)
        self.assertFalse(self.shard_runs())
        self.assertIn("no tests were listed", output)
        self.assert_every_device_returned()

    def test_the_last_runs_seconds_pack_the_shards(self):
        logs = self.root / ".build/simulator-tests/logs"
        logs.mkdir(parents=True)
        (logs / "shard-timings.json").write_text(json.dumps({"T/A": 1, "T/B": 1, "T/C": 1, "T/D": 50}))
        status, output = self.run_matrix("--shards", "2", FAKE_TESTS=json.dumps(self.SHARDED))
        self.assertEqual(status, 0, output)
        shares = sorted(sorted(a for a in call if a.startswith("-only-testing")) for call in self.shard_runs()
                        if self.devices_by_udid()[call[call.index("-destination") + 1].split("id=")[1]] == "iPhone-17-Pro")
        self.assertIn(["-only-testing:T/D"], shares, output)

    def test_each_shard_keeps_its_per_test_lines_and_bundle_summary_in_its_lanes_results(self):
        # They come back with the lane's dispatch and are kept by commit, where the logs folder's are overwritten (#218).
        results = self.root / "lane-results"
        status, output = self.run_matrix("--shards", "2", FAKE_TESTS=json.dumps(self.SHARDED), FAKE_BUNDLE="1",
                                         FAKE_RESULT_LINES="1", FAKE_TEST_FAIL="iPad-Air-13-inch-M4",
                                         ENG_RESULTS_DIR=str(results))
        self.assertEqual(status, 1, output)
        kept = results / "simulator-tests"
        runs = ["iPad-Air-13-inch-M4-1", "iPad-Air-13-inch-M4-2", "iPhone-17-Pro-1", "iPhone-17-Pro-2"]
        self.assertEqual(sorted(p.name for p in kept.iterdir()),
                         sorted(f"{run}.{suffix}" for run in runs for suffix in ("json", "log")))
        for run in runs:
            with self.subTest(run):
                lines = (kept / f"{run}.log").read_text().splitlines()
                self.assertEqual(lines, ['✔ Test "testB()" passed after 0.010 seconds.',
                                         "Test Case '-[T.C testC]' passed (0.020 seconds).",
                                         "Test Suite 'All tests' passed at 2026-10-24 01:00:00.000.",
                                         lines[-1]])
                self.assertRegex(lines[-1], r"^\s*Executed \d+ tests")
                record = json.loads((kept / f"{run}.json").read_text())
                # A failed shard keeps its lines and summary too, with its status.
                self.assertEqual(record["status"], 65 if run.startswith("iPad") else 0)
                self.assertEqual((record["run"], record["log"], record["resultLines"]), (run, f"{run}.log", 4))
                self.assertEqual(record["summary"]["totalTestCount"], 3)
                self.assertIsInstance(record["suiteSeconds"], dict)
                # Each test case's outcome, as `eng results keep` records it (#260).
                self.assertEqual(record["tests"], [{"test": "T/testA()", "outcome": "skipped", "seconds": None}])
                self.assertIsNone(record["bundleUnread"])
                self.assertIsInstance(record["seconds"], int)

    def test_outside_a_lane_nothing_is_kept_and_a_missing_log_or_bundle_keeps_no_lines(self):
        status, output = self.run_matrix("--shards", "2", FAKE_TESTS=json.dumps(self.SHARDED), FAKE_BUNDLE="1")
        self.assertEqual(status, 0, output)
        self.assertFalse(list(self.root.rglob("iPhone-17-Pro-1.json")), output)
        from vocaro_engineering.simulators import matrix
        with mock.patch.dict(os.environ, clear=True):
            self.assertIsNone(matrix.kept("iPhone-17-Pro-1", self.root / "absent.log", None, 0, 1))
        results = self.root / "lane-results"
        self.assertEqual(matrix.kept("iPad-Air-13-inch-M4-2", self.root / "absent.log", None, None, None, results),
                         results / "simulator-tests")
        self.assertEqual(sorted(p.name for p in (results / "simulator-tests").iterdir()), ["iPad-Air-13-inch-M4-2.json"])
        record = json.loads((results / "simulator-tests/iPad-Air-13-inch-M4-2.json").read_text())
        self.assertEqual(record, {"run": "iPad-Air-13-inch-M4-2", "keptBy": "eng simulator-tests", "status": None, "seconds": None, "log": None,
                                  "resultLines": None, "summary": None, "suiteSeconds": None, "tests": None,
                                  "bundleUnread": matrix.UNREAD})

    def test_shards_and_workers_cannot_be_asked_for_together(self):
        status, output = self.run_matrix("--shards", "2", "--workers", "2")
        self.assertEqual(status, 2, output)

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
        # A project that names no pool, run by a launcher whose own checkout (the toolkit) names one: the
        # run's project decides, so it refuses rather than taking the toolkit's members.
        (self.root / "engineering.json").write_text(json.dumps({"schemaVersion": 1, "name": "Project"}))
        finished = subprocess.run(command, cwd=self.root, capture_output=True, text=True, env=self.environment)
        self.assertEqual(finished.returncode, 2, finished.stdout + finished.stderr)
        self.assertIn("name the simulator pool", finished.stderr)
        self.assertFalse((self.state / "simctl.log").exists())

    def test_a_run_outside_a_project_root_takes_the_launchers_pool(self):
        # A directory with no engineering.json, such as a project's subdirectory, has the launcher's project
        # name the pool: here the toolkit's own, which names VocaroEngineering.
        command = [sys.executable, str(ENG), "simulator-tests", "--scheme", "Package-Tests", "--runtime", "iOS-27-0",
                   "--device", "iPhone-17-Pro"]
        finished = subprocess.run(command, cwd=self.root, capture_output=True, text=True, env=self.environment)
        self.assertEqual(finished.returncode, 0, finished.stdout + finished.stderr)
        self.assertEqual(sorted(self.devices()), ["VocaroEngineering iPhone 17 Pro 1"])

    def test_invalid_arguments_create_no_device(self):
        for arguments in (["--jobs", "0"], ["--device", "iPhone-17-Pro"], ["--build-jobs", "0"]):
            with self.subTest(arguments):
                self.assertEqual(self.run_matrix(*arguments)[0], 2)
        self.assertFalse((self.state / "simctl.log").exists())


if __name__ == "__main__":
    unittest.main()
