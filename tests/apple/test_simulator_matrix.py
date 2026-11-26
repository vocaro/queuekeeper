import datetime
import json
import os
import signal
from pathlib import Path
from types import SimpleNamespace
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from unittest import mock
import uuid
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src/python"))
from queuekeeper import lane_report, lane_results, waiting
from queuekeeper_mobile import devices
from queuekeeper_apple.simulators import matrix
import host_isolation
from host_isolation import stretched

ENG = Path(__file__).resolve().parents[2] / "scripts/qk"


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
# named in FAKE_TEST_FAIL, FAKE_UNMEASURED or FAKE_NO_TESTS fails its tests, reports an unmeasured
# test or runs none. Each test run waits until FAKE_MEET devices have started testing,
# or fails after FAKE_MEET_LIMIT seconds.
XCRUN = textwrap.dedent("""\
    #!/usr/bin/env python3
    import datetime, json, os, pathlib, shutil, sys, uuid
    state = pathlib.Path(os.environ["FAKE_STATE"])
    devices = state / "devices"
    devices.mkdir(parents=True, exist_ok=True)
    tool, action, *rest = sys.argv[1:]
    if tool == "xcresulttool":
        if action == "export":
            bundle = pathlib.Path(rest[rest.index("--path") + 1])
            output = pathlib.Path(rest[rest.index("--output-path") + 1])
            if not (bundle / "diagnostics").is_dir(): sys.exit(1)
            shutil.copytree(bundle / "diagnostics", output)
            sys.exit(0)
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
    elif action == "shutdown":
        change(rest[0], state="Shutdown")
    elif action == "erase":
        if device(rest[0])["state"] != "Shutdown": sys.exit("Unable to erase contents and settings in current state")
        if "defaults" in device(rest[0]): change(rest[0], defaults={})
    elif action == "spawn" and rest[1] == "log":
        # VoiceOver's log stream: a speech request among other entries, then nothing until it is stopped, when it
        # says so in simctl.log.
        import signal, time
        (state / f"stream-{rest[0]}.pid").write_text(str(os.getpid()))
        def ended(number, frame):
            with (state / "simctl.log").open("a") as log: log.write(f"stream ended {rest[0]} {signal.Signals(number).name}\\n")
            sys.exit(0)
        signal.signal(signal.SIGTERM, ended)
        signal.signal(signal.SIGINT, ended)
        print('Filtering the log data using "process == \\"vot\\" OR process == \\"MauiAUSP\\""')
        print("Timestamp               Ty Process[PID:TID]")
        print("2026-11-26 10:00:00.000 Db vot[41:1a2] [com.apple.VoiceOverTouch:Speech] SEND req:0x1 OutputRequest: <0x1>")
        print("    Action: 'One Button'")
        print("2026-11-26 10:00:00.001 Db MauiAUSP[42:1b] rendered 512 frames")
        print("2026-11-26 10:00:00.002 Db vot[41:1a2] [com.apple.VoiceOverTouch:Speech] Action: 'Button'")
        sys.stdout.flush()
        while os.getppid() != 1: time.sleep(0.05)
        sys.exit("the stream was left behind by its run")
    elif action == "spawn" and rest[1] == "notifyutil":
        # SpringBoard's published state: its ringer on, as a member's is.
        print(f"{rest[3]} 1")
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
        if os.getppid() == 1: sys.exit(f"{kind} was left behind by its run")
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
    if scenario := json.loads(os.environ.get("FAKE_XCTEST_INTERRUPTION", "{}")).get(kind):
        disconnect = "Lost connection to testmanagerd"
        failure = "XCTAssertEqual failed: 0 is not equal to 1"
        failures = [failure] if scenario == "assertion" else [disconnect]
        if scenario == "assertion-and-disconnect": failures.append(failure)
        cases = [{"nodeType": "Test Case", "nodeIdentifier": f"T/test{number}()", "result": "Failed",
                  "children": [{"nodeType": "Failure Message", "name": message}]}
                 for number, message in enumerate(failures)]
        if scenario == "assertion-in-tree":
            cases[0]["children"].append({"nodeType": "Failure Message", "name": failure})
        # The summary may record a failure that the console could not count.
        finish = time.mktime((2026, 1, 1, 12, 2, 8, 0, 0, -1))
        (bundle / "summary.json").write_text(json.dumps({
            "result": "Failed", "totalTestCount": len(cases), "failedTests": len(cases), "skippedTests": 0,
            "finishTime": finish, "devicesAndConfigurations": [{"device": {"deviceId": udid}}],
            "testFailures": [{"failureText": message} for message in failures]}))
        (bundle / "tests.json").write_text(json.dumps({"testNodes": cases}))
        if scenario == "assertion-in-console":
            print(f"/p/T.swift:5: error: -[T test0] : {failure}")
        if scenario == "crash":
            print("Test Case '-[T test0]' failed (0.1 seconds).")
        if scenario != "bare":
            action = bundle / "diagnostics/0_Test_Simulator_Diagnostics"
            source = "another-device" if scenario == "other-device" else udid
            control = action / f"Simulator_{source}"
            control.mkdir(parents=True)
            minute = "00" if scenario == "stale" else "02"
            (control / "testmanagerd.log").write_text(
                f"12:{minute}:01.260 xcodebuild[42:11] <XCTDaemonControlSession: 0x1> lost connection to daemon\\n")
            session = action / "Probe-Configuration/Session-123"
            session.mkdir(parents=True)
            process = "Probe-Runner" if scenario == "other-process" else "testmanagerd"
            pid = 66 if scenario == "other-xcodebuild" else 42
            lines = [
                f"12:{minute}:01.251 Probe-Runner[99:12] Connection to testmanagerd interrupted, will retry.",
                f"12:{minute}:03.802 xcodebuild[{pid}:13] Handling Crash: {process} at -[XCTDDaemon run]",
                f'12:{minute}:03.802 xcodebuild[{pid}:13] Test operation failure: Error Domain=XCTH Code=5 "{disconnect}"',
            ]
            if scenario == "later-disconnect":
                lines.append(f'12:02:06.000 xcodebuild[42:13] Test operation failure: Error Domain=XCTH Code=5 "{disconnect}"')
            (session / "Session-Probe.log").write_text("\\n".join(lines) + "\\n")
        print("Executed 0 tests, with 0 failures (0 unexpected) in 0.1 (0.1) seconds")
        sys.exit(65)
    skipped = os.environ.get("FAKE_SKIPPED", "0") if kind in os.environ.get("FAKE_SKIPPED_ON", "") else "0"
    unmeasured = kind in os.environ.get("FAKE_UNMEASURED", "") or at_run("FAKE_UNMEASURED_RUN")
    captured = os.environ.get("FAKE_BUNDLE_RECORD")
    record_on = os.environ.get("FAKE_BUNDLE_RECORD_ON")
    if captured and (not record_on or kind == record_on):
        for view, record in zip(("summary", "tests"), json.loads(captured)):
            (bundle / f"{view}.json").write_text(json.dumps(record))
        if record_on:
            if console := os.environ.get("FAKE_BUNDLE_CONSOLE"):
                print(console)
            sys.exit(65)
    elif identity := os.environ.get("FAKE_ISOLATED_BUNDLE"):
        failed = at_run("FAKE_FAIL_RUN")
        if run_number != int(os.environ.get("FAKE_MISSING_CASE_RUN", "0")):
            result = "Failed" if failed else "Passed"
            (bundle / "summary.json").write_text(json.dumps({"result": result, "totalTestCount": 1,
                                                             "failedTests": int(failed), "skippedTests": 0}))
            (bundle / "tests.json").write_text(json.dumps({"testNodes": [{"nodeType": "Test Case", "result": result,
                "nodeIdentifier": "Smoke/testOpens()", "nodeIdentifierURL": identity}]}))
        print(f"Executed 1 tests, with {int(failed)} failures (0 unexpected) in 0.1 (0.1) seconds")
        sys.exit(65 if failed else 0)
    elif os.environ.get("FAKE_BUNDLE"):
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
    if kind in os.environ.get("FAKE_SWIFT_TESTING_FAIL", ""):
        # One Swift Testing test that failed, beside its bundle's empty XCTest run (vocaro/queuekeeper#81).
        print("Test Suite 'T.xctest' started at 2026-11-11 20:14:18.000.")
        print("Test Suite 'T.xctest' passed at 2026-11-11 20:14:18.001.")
        print("\\t Executed 0 tests, with 0 failures (0 unexpected) in 0.000 (0.001) seconds")
        print('\\u25c7 Test "testA()" started.')
        print('\\u2718 Test "testA()" recorded an issue at T.swift:5:3: Expectation failed: weight == 700')
        print('\\u2718 Test "testA()" failed after 65.382 seconds with 1 issue.')
        print("\\u2718 Test run with 1 test in 1 suite failed after 65.400 seconds with 1 issue.")
        sys.exit(65)
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
        # The stand-ins and device record are private to this fixture. Keep host_isolation's dispatch and
        # reaping switches in the child, and give each case a fresh admission guard and slot directory (#336).
        self.environment = {**os.environ,
                            "PATH": f"{tools}:{Path(sys.executable).parent}:/usr/bin:/bin", "FAKE_STATE": str(self.state),
                            "HEAVY_SLOT_DIR": str(self.root / "slots"), "QUEUEKEEPER_GATE_LOCK_FILE": str(self.root / "gate"),
                            "QUEUEKEEPER_DEVICE_RECORD": str(self.record), "QUEUEKEEPER_DEVICE_OWNER": "matrix-tests",
                            # Devices that should meet wait for each other this long, stretched by load.
                            "FAKE_MEET_LIMIT": str(stretched(60))}

    def run_matrix(self, *arguments, wait_for_fake_boot=False, timed_out_boot=None, **environment):
        command = [sys.executable, str(ENG), "simulator-tests", "--unadmitted", "--scheme", "Package-Tests", "--runtime", "iOS-27-0",
                   "--device", "iPhone-17-Pro", "--device", "iPad-Air-13-inch-M4", "--pool", "Project", *arguments]
        if wait_for_fake_boot or timed_out_boot is not None:
            # Wait for the stand-in itself, bounded by the enclosing unittest run. The timeout
            # outcome is injected for one bootstatus so its sibling cannot time out under load.
            driver = textwrap.dedent("""\
                from pathlib import Path
                import runpy
                import sys
                from unittest.mock import patch

                fake_xcrun, timed_out_boot = sys.argv[1:3]
                sys.argv = sys.argv[3:]
                queuekeeper = Path(sys.argv[0])
                sys.path.insert(0, str(queuekeeper.parents[1] / "src/python"))
                from queuekeeper import processes
                supervise = processes.supervise

                def wait_for_boot(command, **options):
                    if command[:4] == ["xcrun", "simctl", "bootstatus", timed_out_boot]:
                        raise processes.TimedOut("simulated bootstatus timeout")
                    if command[:3] in (["xcrun", "simctl", "boot"], ["xcrun", "simctl", "bootstatus"]):
                        command = [fake_xcrun, *command[1:]]
                        options["timeout"] = None
                    return supervise(command, **options)

                with patch.object(processes, "supervise", wait_for_boot):
                    runpy.run_path(str(queuekeeper), run_name="__main__")
            """)
            command[1:1] = ["-c", driver, str(self.root / "bin/xcrun"), timed_out_boot or ""]
        finished = subprocess.run(command, cwd=self.root, capture_output=True, text=True,
                                  env={**self.environment, **environment})
        return finished.returncode, finished.stdout + finished.stderr

    def calls(self):
        return [json.loads(line) for line in (self.state / "xcodebuild.log").read_text().splitlines()]

    def run_series(self, *arguments, **environment):
        """A run of one device, the iPhone, so that its runs come in a known order."""
        command = [sys.executable, str(ENG), "simulator-tests", "--unadmitted", "--scheme", "Package-Tests", "--runtime", "iOS-27-0",
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
        self.assertEqual(lane_results.series_reported(output), 3)

    def test_a_series_counts_iphone_and_ipad_tests_once_per_repeat(self):
        status, output = self.run_matrix("--repeat", "2")
        self.assertEqual(status, 0, output)
        self.assertEqual(self.series_record()["tests"]["passed"], 12)
        self.assertEqual(lane_results.require_tests(output, expected=12), 12)

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

    def test_an_isolated_series_keeps_measured_outcomes_without_changing_its_verdict(self):
        identity = "test://com.apple.xcode/App/AppTests/Smoke/testOpens()"
        selector = "AppTests/Smoke/testOpens()"
        args = ("--repeat", "2", "--erase", "--only", selector)
        env = {"FAKE_ISOLATED_BUNDLE": identity, "FAKE_FAIL_RUN": "iPhone-17-Pro:1"}
        status, output = self.run_series(*args, **env)
        self.assertEqual(status, 1, output)
        self.assertEqual(self.series_record()["isolatedTest"], {
            "selector": selector, "runtime": "iOS-27-0", "observations": [
                {"run": 1, "identifierURL": identity, "outcome": "failed"},
                {"run": 2, "identifierURL": identity, "outcome": "passed"}]})
        self.assertEqual([call[-1] for call in self.ran_tests()], [f"-only-testing:{selector}"] * 2)

    def test_an_isolated_series_needs_a_fresh_matching_bundle_on_each_run(self):
        identity = "test://com.apple.xcode/App/AppTests/Smoke/testOpens"
        selector = "AppTests/Smoke/testOpens()"
        args = ("--repeat", "2", "--erase", "--only", selector)
        env = {"FAKE_ISOLATED_BUNDLE": identity, "FAKE_FAIL_RUN": "iPhone-17-Pro:1"}
        status, output = self.run_series(*args, **env)
        self.assertEqual(status, 1, output)
        self.assertEqual(len(self.series_record()["isolatedTest"]["observations"]), 2)
        (self.state / "runs-iPhone-17-Pro").unlink()
        status, output = self.run_series(*args, **env, FAKE_MISSING_CASE_RUN="2")
        self.assertEqual(status, 1, output)
        self.assertEqual(self.series_record()["isolatedTest"]["observations"], [
            {"run": 1, "identifierURL": identity, "outcome": "failed"}])

    def test_an_exact_series_counts_only_runs_whose_bundle_shows_the_test_alone(self):
        # The merge queue's rerun of a failed Xcode test (queuekeeper_apple.flakes, vocaro/queuekeeper#2).
        identity = "test://com.apple.xcode/App/AppTests/Smoke/testOpens()"
        selector = "AppTests/Smoke/testOpens()"
        # A run number that never comes has the stand-in count its runs, which FAKE_MISSING_CASE_RUN reads.
        env = {"FAKE_ISOLATED_BUNDLE": identity, "FAKE_FAIL_RUN": "iPhone-17-Pro:99"}
        status, output = self.run_series("--", "--repeat", "1", "--erase", "--exact", "--only", selector, **env)
        self.assertEqual(status, 0, output)
        self.assertEqual(self.series_record()["isolatedTest"]["observations"], [
            {"run": 1, "identifierURL": identity, "outcome": "passed"}])
        (self.state / "runs-iPhone-17-Pro").unlink()
        # A run that exits 0 with no such case in its bundle passed nothing: unmeasured, which gives no verdict.
        status, output = self.run_series("--repeat", "2", "--erase", "--exact", "--only", selector, **env,
                                         FAKE_MISSING_CASE_RUN="2")
        self.assertEqual(status, lane_results.UNMEASURED, output)
        self.assertEqual([run["outcome"] for run in self.series_record()["eachRun"]], ["passed", "unmeasured"])
        self.assertIn(f"its result bundle does not show {selector} alone", output)
        (self.state / "runs-iPhone-17-Pro").unlink()
        status, output = self.run_series("--repeat", "1", "--erase", "--exact", "--only", selector,
                                         **{**env, "FAKE_FAIL_RUN": "iPhone-17-Pro:1"})
        self.assertEqual(status, 1, output)

    def test_an_exact_series_that_cannot_isolate_its_test_runs_nothing_and_is_unmeasured(self):
        selector = "AppTests/Smoke/testOpens()"
        for arguments in (("--repeat", "1", "--exact", "--only", selector),
                          ("--repeat", "1", "--erase", "--exact", "--only", "AppTests/testOpens()"),
                          ("--repeat", "1", "--erase", "--exact", "--only", selector, "--", "-test-iterations", "2")):
            with self.subTest(arguments=arguments):
                status, output = self.run_series(*arguments, FAKE_ISOLATED_BUNDLE="test://com.apple.xcode/App/" + selector)
                self.assertEqual(status, lane_results.UNMEASURED, output)
                self.assertIn("--exact needs one --only TARGET/SUITE/METHOD", output)
        self.assertFalse((self.state / "xcodebuild.log").exists() and self.ran_tests())

    def test_exact_needs_a_series(self):
        status, output = self.run_series("--exact")
        self.assertEqual(status, 2, output)
        self.assertIn("--template, --only and --exact say how a series runs", output)

    def test_an_isolated_outcome_rejects_other_cases_even_when_the_display_name_matches(self):
        selector = "AppTests/Smoke/testOpens"
        identity = "test://com.apple.xcode/App/AppTests/Smoke/testOpens"
        summary = {"result": "Passed", "totalTestCount": 1, "skippedTests": 0}
        case = {"nodeType": "Test Case", "nodeIdentifier": "Smoke/testOpens", "result": "Passed",
                "nodeIdentifierURL": identity}
        self.assertEqual(matrix.isolated_outcome((summary, {"testNodes": [case]}), selector, 0),
                         {"identifierURL": identity, "outcome": "passed"})
        for records in ([{**case, "nodeIdentifierURL": identity.replace("AppTests", "OtherTests")}],
                        [{**case, "nodeIdentifierURL": ""}], [{**case, "result": "Expected Failure"}],
                        [{**case, "result": "Skipped"}], [case, case],
                        [{**case, "children": [{"nodeType": "Arguments"}]}]):
            with self.subTest(records=records):
                self.assertIsNone(matrix.isolated_outcome((summary, {"testNodes": records}), selector, 0))

    def test_an_unmeasured_run_leaves_a_series_unmeasured_and_never_passes_it(self):
        self.add("Project iPhone 17 Pro 1")
        status, output = self.run_series("--repeat", "3", "--erase", FAKE_UNMEASURED_RUN="iPhone-17-Pro:2")
        self.assertEqual(status, lane_results.UNMEASURED, output)
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
        self.assertEqual(status, lane_results.UNMEASURED, output)
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
        self.assertIsNone(lane_results.series_reported(output))

    def test_a_run_that_could_not_take_a_member_is_unmeasured_and_the_series_goes_on(self):
        from queuekeeper_apple.simulators.pool import Lease
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
        self.assertEqual(status, lane_results.UNMEASURED)
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
        self.assertEqual(lane_results.executed_tests(output), 6, output)
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
        self.assertEqual(lane_results.executed_tests(output), 6, output)
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
        # Under qk run, xcodebuild's wrapper has rebuilt once and says so in the build's output (#267).
        modules, _ = self.planted_modules()
        (self.root / "bin/xcodebuild").write_text((self.root / "bin/xcodebuild").read_text().replace(
            "print(\"error: file 'include/resvg.h'",
            f"print({matrix.STALE_NOTICE + ': the rerun of xcodebuild ended with status 65'!r}); "
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
        with mock.patch.dict(os.environ, {"QUEUEKEEPER_DEVICE_RECORD": str(self.record)}):
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
            self.assertRegex(output, rf"MUTED {kind}: VoiceOver's sound cues off; SpringBoard published the volume "
                             r"after \d+s; the volume read 0 20 times in a row "
                             r"in \d+s; hold ended at \d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z")
            self.assertLess(output.index(f"MUTED {kind}"), output.index(f"TEST {kind}"))
            # The probe's phrases reach the run's own output.
            self.assertIn(f"{kind} VOICEOVER_EVENT 2026-11-06T00:00:00.000Z touch: One Button", output)
        self.assertIn("PASS iPhone-17-Pro: Executed 3 tests", output)
        self.assertIn("VOICEOVER iPhone-17-Pro: 1 lines; VoiceOver off and the volume 0 after the tests", output)
        # The negative control: the iPad's volume came back while its tests ran and they left VoiceOver on.
        self.assertRegex(output, r"FAIL iPad-Air-13-inch-M4: the simulator was heard or left speaking: \S+ UNMUTED "
                                 r"the volume was 60; muted again; VoiceOver was left on; it has been switched off")
        self.assertRegex(output, r"VOICEOVER iPad-Air-13-inch-M4: [^\n]*; first volume watch check at "
                                 r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z")
        self.assertFalse(self.devices()["Project iPad Air 13-inch (M4) 1"]["voiceover"])
        self.assert_every_device_returned()

    def test_volume_watch_keeps_the_time_of_its_first_attempt_even_without_a_reading(self):
        run = matrix.Run(SimpleNamespace(logs=self.root))
        device = SimpleNamespace(udid="simulator")
        held = "2026-11-06T00:00:20.000Z"
        first = "2026-11-06T00:01:30.000Z"
        unread = "2026-11-06T00:01:35.000Z"
        event = mock.Mock()
        event.wait.side_effect = [False, False, True]
        thread = mock.Mock()
        watch = SimpleNamespace(Event=lambda: event, Thread=thread)
        with mock.patch.object(matrix, "threading", watch), \
             mock.patch.object(matrix.speech, "silence_cues", return_value=True), \
             mock.patch.object(matrix.speech, "hold_mute", return_value=(20, [], 0)), \
             mock.patch.object(matrix.speech, "state", side_effect=[RuntimeError("no volume reading"),
                              {"volume": 0}, {"enabled": False}, {"volume": 0}]), \
             mock.patch.object(matrix.speech, "now", side_effect=[held, first, unread]) as clock, \
             mock.patch.object(run, "say") as say:
            self.assertTrue(run.silence("phone", device, self.root / "phone.log"))
            with run.heard("phone", device):
                thread.call_args.kwargs["target"]()
        self.assertEqual(clock.call_count, 3)
        self.assertTrue(say.call_args_list[0].args[0].endswith(f"hold ended at {held}"))
        say.assert_called_with("VOICEOVER phone: 0 lines; VoiceOver off and the volume 0 after the tests; "
                               f"1 volume checks got no reading; first volume watch check at {first}")

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

    def assert_streams_ended(self, *udids):
        """Each member's VoiceOver log stream started after its boot and ended on the run's SIGINT before the member
        was shut down, and no stream process is left."""
        simctl = (self.state / "simctl.log").read_text().splitlines()
        for udid in udids:
            started = simctl.index(f"spawn {udid} log stream --level debug --style compact --predicate "
                                   'process == "vot" OR process == "MauiAUSP"')
            self.assertLess(simctl.index(f"bootstatus {udid} -b"), started)
            self.assertLess(started, simctl.index(f"stream ended {udid} SIGINT"))
            self.assertLess(simctl.index(f"stream ended {udid} SIGINT"), simctl.index(f"shutdown {udid}"))
            pid = int((self.state / f"stream-{udid}.pid").read_text())
            with self.assertRaises(ProcessLookupError, msg=f"the stream {pid} outlived its run"): os.kill(pid, 0)

    def test_voiceover_log_keeps_each_members_stream_beside_its_log_and_ends_it(self):
        iphone = self.add("Project iPhone 17 Pro 1")
        ipad = self.add("Project iPad Air 13-inch (M4) 1", IPAD)
        logs = self.root / ".build/simulator-tests/logs"
        # One an earlier run left is not passed off as this run's.
        logs.mkdir(parents=True)
        (logs / "iPad-Air-13-inch-M4.voiceover.log").write_text("an earlier run's\n")
        status, output = self.run_matrix("--voiceover", "--voiceover-log", FAKE_VOICEOVER="1",
                                         FAKE_TEST_FAIL="iPad-Air-13-inch-M4")
        self.assertEqual(status, 1, output)
        self.assert_streams_ended(iphone, ipad)
        for kind in ("iPhone-17-Pro", "iPad-Air-13-inch-M4"):
            kept = logs / f"{kind}.voiceover.log"
            text = kept.read_text()
            self.assertIn("SEND req:0x1 OutputRequest:", text)
            self.assertIn("MauiAUSP[42:1b] rendered 512 frames", text)
            self.assertNotIn("an earlier run's", text)
            self.assertIn(f"VOICEOVER LOG {kind}: streaming into {kept}", output)
            self.assertIn(f"VOICEOVER LOG {kind}: 6 lines in {kept}", output)
            self.assertLess(output.index(f"MUTED {kind}"), output.index(f"TEST {kind}"))
        # A failed member's stream ends as a passed one's does.
        self.assertIn("FAIL iPad-Air-13-inch-M4: the tests failed", output)
        self.assertIn("PASS iPhone-17-Pro: Executed 3 tests", output)
        self.assert_every_device_returned()

    def test_voiceover_log_speech_keeps_only_the_speech_requests(self):
        iphone = self.add("Project iPhone 17 Pro 1")
        status, output = self.run_series("--voiceover", "--voiceover-log", "speech")
        self.assertEqual(status, 0, output)
        self.assert_streams_ended(iphone)
        kept = self.root / ".build/simulator-tests/logs/iPhone-17-Pro.voiceover.log"
        self.assertEqual(kept.read_text().splitlines(), [
            'Filtering the log data using "process == "vot" OR process == "MauiAUSP""',
            "Timestamp               Ty Process[PID:TID]",
            "2026-11-26 10:00:00.000 Db vot[41:1a2] [com.apple.VoiceOverTouch:Speech] SEND req:0x1 OutputRequest: <0x1>",
            "    Action: 'One Button'",
            "2026-11-26 10:00:00.002 Db vot[41:1a2] [com.apple.VoiceOverTouch:Speech] Action: 'Button'"])
        self.assertIn(f"VOICEOVER LOG iPhone-17-Pro: 5 of 6 lines, those of its speech requests, in {kept}", output)

    def test_voiceover_log_is_kept_for_each_run_of_a_series(self):
        iphone = self.add("Project iPhone 17 Pro 1")
        status, output = self.run_series("--voiceover", "--voiceover-log", "--repeat", "2")
        self.assertEqual(status, 0, output)
        for run in (1, 2):
            self.assertIn("SEND req:", (self.root / f".build/simulator-tests/logs/iPhone-17-Pro-run{run}.voiceover.log")
                          .read_text())
        simctl = (self.state / "simctl.log").read_text().splitlines()
        self.assertEqual(simctl.count(f"stream ended {iphone} SIGINT"), 2, simctl)

    def test_voiceover_log_needs_voiceover(self):
        status, output = self.run_matrix("--voiceover-log")
        self.assertEqual(status, 2, output)
        self.assertIn("--voiceover-log keeps VoiceOver's log of a --voiceover run: give --voiceover too", output)
        self.assertFalse((self.state / "simctl.log").exists(), "nothing was taken or streamed")

    def test_a_stop_ends_each_members_voiceover_log_stream(self):
        iphone = self.add("Project iPhone 17 Pro 1")
        ipad = self.add("Project iPad Air 13-inch (M4) 1", IPAD)
        command = [sys.executable, str(ENG), "simulator-tests", "--unadmitted", "--scheme", "Package-Tests", "--runtime", "iOS-27-0",
                   "--device", "iPhone-17-Pro", "--device", "iPad-Air-13-inch-M4", "--pool", "Project",
                   "--voiceover", "--voiceover-log"]
        # Three devices never meet, so both runs test until the stop.
        process = subprocess.Popen(command, cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                   env={**self.environment, "FAKE_MEET": "3", "FAKE_MEET_LIMIT": str(stretched(600))},
                                   preexec_fn=host_isolation.default_signals)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        followed = Followed(process)
        testing = lambda: len(list(self.state.glob("testing-*")))
        reported = lambda: any(line is not None and line.startswith(("PASS ", "FAIL ", "UNMEASURED ", "NOT RUN "))
                               for line in list(followed.lines))
        waiting.wait_until(lambda: testing() >= 2 or process.poll() is not None or reported(),
                           progress=waiting.combined(waiting.files(self.state), lambda: len(followed.lines),
                                                     waiting.cpu(self.root / "bin", exclude=["test-without-building"])))
        self.assertEqual(testing(), 2, f"the devices never started testing: {followed.text()}")
        process.send_signal(signal.SIGTERM)
        output = followed.communicate(timeout=stretched(60))
        self.assertEqual(process.returncode, -signal.SIGTERM, output)
        self.assert_streams_ended(iphone, ipad)
        self.assertIn("VOICEOVER LOG iPhone-17-Pro: 6 lines in", output)
        self.assert_every_device_returned()

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

    def test_a_simctl_boot_error_names_the_command_and_keeps_its_error_text(self):
        error = "NSPOSIXErrorDomain code=60: Unable to boot the Simulator"
        for step, command in (("boot", "simctl boot"), ("bootstatus", "simctl bootstatus -b")):
            with self.subTest(step=step):
                run = matrix.Run(SimpleNamespace(logs=self.root, boot_timeout=180))

                def boot(arguments, *, stdout, **_):
                    if arguments[2] != step: return 0
                    print(error, file=stdout)
                    return 60

                with mock.patch.object(matrix.processes, "supervise", side_effect=boot), \
                     mock.patch.object(run, "say") as say:
                    self.assertFalse(run.boot("phone", SimpleNamespace(udid="simulator")))
                self.assertEqual(run.results["phone"], 1)
                self.assertEqual((self.root / "phone-boot.log").read_text(), error + "\n")
                say.assert_any_call(f"FAIL phone: the boot failed with status 60 in `{command}`; "
                                    f"log: {self.root / 'phone-boot.log'}", f"    {error}")

    def test_a_boot_past_its_limit_leaves_only_that_device_unmeasured(self):
        iphone = self.add("Project iPhone 17 Pro 1")
        ipad = self.add("Project iPad Air 13-inch (M4) 1", IPAD)
        status, output = self.run_matrix("--boot-timeout", "180", timed_out_boot=ipad)
        self.assertEqual(status, lane_results.UNMEASURED, output)
        self.assertIn("boot limit 120s, bootstatus limit 180s", output)
        self.assertIn("UNMEASURED iPad-Air-13-inch-M4: the boot exceeded its limit of 180s in "
                      "`simctl bootstatus -b`, so no test ran", output)
        self.assertIn("PASS iPhone-17-Pro: Executed 3 tests", output)
        [call] = self.ran_tests()
        self.assertEqual(call[call.index("-destination") + 1], f"platform=iOS Simulator,id={iphone}")
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
        self.assertEqual(lane_results.executed_tests(output), 6, output)

    def test_a_failed_build_only_run_fails(self):
        self.add("Project iPhone 17 Pro 1")
        status, output = self.run_matrix("--build-only", FAKE_BUILD_FAIL="1")
        self.assertEqual(status, 1, output)
        self.assertIn("FAIL build: status 1", output)
        self.assertRegex(output, r"Simulator build \(wall-clock \d+s\): FAIL, no test run")
        self.assert_every_device_returned()

    def test_any_device_failure_fails_the_run_and_otherwise_an_unmeasured_test_leaves_it_unmeasured(self):
        cases = [
            ({"FAKE_UNMEASURED": "iPhone-17-Pro"}, lane_results.UNMEASURED,
             "UNMEASURED iPhone-17-Pro: Executed 3 tests", "PASS iPad-Air-13-inch-M4: Executed 3 tests"),
            ({"FAKE_UNMEASURED": "iPhone-17-Pro", "FAKE_TEST_FAIL": "iPad-Air-13-inch-M4"}, 1,
             "UNMEASURED iPhone-17-Pro: Executed 3 tests", "FAIL iPad-Air-13-inch-M4: the tests failed with status 65"),
            ({"FAKE_NO_TESTS": "iPad-Air-13-inch-M4"}, 1,
             "PASS iPhone-17-Pro: Executed 3 tests", "FAIL iPad-Air-13-inch-M4: no tests ran"),
        ]
        for environment, expected, iphone_outcome, ipad_outcome in cases:
            with self.subTest(environment):
                for path in self.state.glob("testing-*"): path.unlink()
                (self.state / "simctl.log").unlink(missing_ok=True)
                status, output = self.run_matrix(wait_for_fake_boot=True, **environment)
                self.assertEqual(status, expected, output)
                self.assertIn(iphone_outcome, output)
                self.assertIn(ipad_outcome, output)
                self.assertEqual(len(lane_results.unmeasured_tests(output)), int("FAKE_UNMEASURED" in environment), output)
                self.assert_every_device_returned()

    def test_a_corroborated_daemon_crash_leaves_measurement_missing_after_voiceover_cleanup(self):
        ipad = self.add("Project iPad Air 13-inch (M4) 1", IPAD)
        results = self.root / "lane-results"
        status, output = self.run_matrix(
            "--voiceover", wait_for_fake_boot=True, FAKE_VOICEOVER="1", FAKE_VOICEOVER_LEFT_ON="iPad-Air-13-inch-M4",
            FAKE_XCTEST_INTERRUPTION=json.dumps({"iPad-Air-13-inch-M4": "crash"}), QUEUEKEEPER_RESULTS_DIR=str(results))
        self.assertEqual(status, lane_results.UNMEASURED, output)
        self.assertIn("UNMEASURED iPad-Air-13-inch-M4: the simulator's testmanagerd crashed during testing", output)
        self.assertIn("PASS iPhone-17-Pro: Executed 3 tests", output)
        counts = lane_report.counts(output)
        self.assertEqual((counts["passed"], counts["failed"], counts["unmeasured"]), (3, 0, 1))
        kept = json.loads((results / "simulator-tests/iPad-Air-13-inch-M4.json").read_text())
        self.assertEqual((kept["status"], kept["tests"][0]["outcome"]), (lane_results.UNMEASURED, "unmeasured"))
        self.assertNotIn("failures", [entry.name for entry in results.iterdir()])
        self.assertIn("VoiceOver was left on; it has been switched off", output)
        self.assertIn(f"settings voiceover --device {ipad} --disable", self.devicectl(ipad))
        self.assertFalse(self.devices()["Project iPad Air 13-inch (M4) 1"]["voiceover"])
        self.assert_every_device_returned()

    def test_an_uncorroborated_disconnect_or_assertion_still_fails_its_device(self):
        for scenario in ("bare", "other-device", "other-process", "other-xcodebuild", "stale", "later-disconnect",
                         "assertion-and-disconnect", "assertion-in-tree", "assertion-in-console"):
            with self.subTest(scenario=scenario):
                status, output = self.run_matrix(wait_for_fake_boot=True,
                    FAKE_XCTEST_INTERRUPTION=json.dumps({"iPad-Air-13-inch-M4": scenario}))
                self.assertEqual(status, 1, output)
                self.assertIn("FAIL iPad-Air-13-inch-M4: the tests failed with status 65", output)
                self.assertIn("PASS iPhone-17-Pro", output)
                self.assertFalse(lane_results.unmeasured_tests(output), output)

    def test_another_devices_assertion_failure_takes_precedence_over_a_daemon_crash(self):
        results = self.root / "lane-results"
        status, output = self.run_matrix(wait_for_fake_boot=True, QUEUEKEEPER_RESULTS_DIR=str(results),
            FAKE_XCTEST_INTERRUPTION=json.dumps({"iPad-Air-13-inch-M4": "crash", "iPhone-17-Pro": "assertion"}))
        self.assertEqual(status, 1, output)
        self.assertIn("UNMEASURED iPad-Air-13-inch-M4: the simulator's testmanagerd crashed during testing", output)
        self.assertIn("FAIL iPhone-17-Pro: the tests failed with status 65", output)
        kept = list(results.rglob("failures.json"))
        self.assertEqual([path.parent.name for path in kept], ["iPhone-17-Pro"])
        self.assertIn("XCTAssertEqual failed", kept[0].read_text())
        self.assert_every_device_returned()

    def test_a_stop_ends_every_device_run_and_returns_every_device_to_the_pool(self):
        command = [sys.executable, str(ENG), "simulator-tests", "--unadmitted", "--scheme", "Package-Tests", "--runtime", "iOS-27-0",
                   "--device", "iPhone-17-Pro", "--device", "iPad-Air-13-inch-M4", "--pool", "Project"]
        # Three devices never meet, so both runs wait until the stop, however long the other device takes to start.
        process = subprocess.Popen(command, cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                   env={**self.environment, "FAKE_MEET": "3", "FAKE_MEET_LIMIT": str(stretched(600))},
                                   preexec_fn=host_isolation.default_signals)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        followed = Followed(process)
        # Both devices start testing while the run still moves, not within a fixed time: a minute stretched by the
        # load read when the test began ran out at a load of up to 858 while the devices were still booting
        # (vocaro/queuekeeper#304). A run that ends, or reports a device's outcome, before both start stops the wait.
        testing = lambda: len(list(self.state.glob("testing-*")))
        reported = lambda: any(line is not None and line.startswith(("PASS ", "FAIL ", "UNMEASURED ", "NOT RUN "))
                               for line in list(followed.lines))
        waiting.wait_until(lambda: testing() >= 2 or process.poll() is not None or reported(),
                           progress=waiting.combined(waiting.files(self.state), lambda: len(followed.lines),
                                                     waiting.cpu(self.root / "bin", exclude=["test-without-building"])))
        self.assertEqual(testing(), 2, f"the devices never started testing: {followed.text()}")
        process.send_signal(signal.SIGTERM)
        output = followed.communicate(timeout=stretched(60))
        self.assertEqual(process.returncode, -signal.SIGTERM, output)
        self.assertIn("SIGTERM received; forwarding it", output)
        self.assert_every_device_returned()

    def test_a_run_waits_for_another_that_holds_its_derived_data_and_logs_but_not_for_one_elsewhere(self):
        # A build beside another run's tests copied the resources they read again (vocaro/EPUBLib#872).
        command = [sys.executable, str(ENG), "simulator-tests", "--unadmitted", "--scheme", "Package-Tests", "--runtime", "iOS-27-0",
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
        followed.wait_for("WAIT: another qk simulator-tests run holds", self)
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
        self.assertRegex(outputs[1], rf"WAIT: another qk simulator-tests run holds {derived} \(PID {first.pid} since ")
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
        command = [sys.executable, str(ENG), "simulator-tests", "--unadmitted", "--scheme", "Package-Tests", "--runtime", "iOS-27-0",
                   "--device", "iPhone-17-Pro", "--device", "iPad-Air-13-inch-M4", "--pool", "Project", "--logs", "logs"]
        process = subprocess.Popen(command, cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                   env=self.environment)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        followed = Followed(process)
        followed.wait_for(f"WAIT: another qk simulator-tests run holds {logs} (it has not said which)", self)
        self.assertIsNone(process.poll())
        self.assertFalse((self.state / "xcodebuild.log").exists(), "nothing builds while another run holds the logs")
        self.assertFalse((self.state / "simctl.log").exists() and "boot" in (self.state / "simctl.log").read_text(),
                         "and no device is booted for it")
        fcntl.flock(lock, fcntl.LOCK_UN)
        output = followed.communicate(timeout=stretched(60))
        self.assertEqual(process.returncode, 0, output)
        self.assertIn(f"WAIT: another qk simulator-tests run holds {logs} (it has not said which)", output)
        self.assertIn(f"HELD {logs} after waiting", output)
        self.assert_every_device_returned()

    def test_skipped_tests_ran_nothing_so_a_device_whose_tests_all_skipped_fails(self):
        status, output = self.run_matrix(FAKE_SKIPPED="2", FAKE_SKIPPED_ON="iPad-Air-13-inch-M4")
        self.assertEqual(status, 0, output)
        self.assertIn("PASS iPad-Air-13-inch-M4: Executed 1 tests, 2 skipped", output)
        self.assertEqual(lane_results.executed_tests(output), 4, output)
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
        self.assertEqual(lane_results.executed_tests(output), 4, output)

    def test_with_parallel_workers_an_unmeasured_skip_the_console_does_not_name_leaves_the_run_unmeasured(self):
        status, output = self.run_matrix("--workers", "2", FAKE_BUNDLE="1", FAKE_UNMEASURED="iPad-Air-13-inch-M4")
        self.assertEqual(status, lane_results.UNMEASURED, output)
        self.assertEqual(lane_results.unmeasured_tests(output),
                         ["iPad-Air-13-inch-M4: -[T/testA()] : Test skipped - Unmeasured: the host was busy"])
        self.assertIn("PASS iPhone-17-Pro", output)

    def test_parameterized_cancellations_count_once_per_test_even_when_no_tests_ran(self):
        # Xcode 27.2 keeps each cancellation as a Skip Message under an Arguments node. Its
        # top-level total omits partial skips even though those cases have a Skipped result.
        def case(name, result, *messages):
            children = [{"nodeType": "Arguments", "result": "Skipped",
                         "children": [{"nodeType": "Skip Message", "name": message}]} for message in messages]
            return {"nodeType": "Test Case", "nodeIdentifier": name, "result": result, "children": children}

        busy = case("partlyBusy(n:)", "Passed", "Test case cancelled: Unmeasured: first case",
                    "Test case cancelled: Unmeasured: second case")
        optional = case("partlyOptional(n:)", "Passed", "Test case cancelled")
        all_canceled = case("allCanceled(n:)", "Skipped", "Test case cancelled: Unmeasured: all cases")
        failure = case("failed(n:)", "Failed", "Test case cancelled: Unmeasured: a case in a failed test")
        summary = {"totalTestCount": 4, "skippedTests": 1, "failedTests": 1}
        record = (summary, {"testNodes": [busy, optional, all_canceled, failure]})
        outcomes = matrix.test_cases(record)
        self.assertEqual([test["outcome"] for test in outcomes], ["unmeasured", "skipped", "unmeasured", "failed"])
        self.assertEqual(matrix.bundle_results(None, "device", record), (1, 3, [
            "device: -[partlyBusy(n:)] : Test skipped - Unmeasured: first case",
            "device: -[allCanceled(n:)] : Test skipped - Unmeasured: all cases",
        ]))

        results = self.root / "lane-results"
        status, output = self.run_series("--workers", "2", FAKE_BUNDLE_RECORD=json.dumps((
            {"totalTestCount": 3, "skippedTests": 1, "failedTests": 0},
            {"testNodes": [busy, optional, all_canceled]})), QUEUEKEEPER_RESULTS_DIR=str(results))
        self.assertEqual(status, lane_results.UNMEASURED, output)
        self.assertIn("UNMEASURED iPhone-17-Pro: Executed 0 tests, 3 skipped", output)
        self.assertEqual(lane_results.executed_tests(output), 0, output)
        self.assertEqual(lane_results.skipped_tests(output), 3, output)
        self.assertEqual(lane_results.unmeasured_tests(output), [
            "iPhone-17-Pro: -[partlyBusy(n:)] : Test skipped - Unmeasured: first case",
            "iPhone-17-Pro: -[allCanceled(n:)] : Test skipped - Unmeasured: all cases",
        ])
        kept = json.loads((results / matrix.KEPT / "iPhone-17-Pro.json").read_text())
        self.assertEqual([test["outcome"] for test in kept["tests"]], ["unmeasured", "skipped", "unmeasured"])

    def test_with_parallel_workers_and_no_readable_bundle_no_test_counts(self):
        # The console's per-test lines are not a count: a run whose bundle cannot be read establishes nothing.
        status, output = self.run_matrix("--workers", "2")
        self.assertEqual(status, 1, output)
        self.assertIn("FAIL iPhone-17-Pro: no tests ran (Executed 0 tests)", output)

    def test_a_skip_both_the_console_and_the_bundle_name_is_listed_once(self):
        status, output = self.run_matrix(FAKE_BUNDLE="1", FAKE_UNMEASURED="iPhone-17-Pro")
        self.assertEqual(status, lane_results.UNMEASURED, output)
        self.assertEqual(lane_results.unmeasured_tests(output),
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
        self.assertEqual(lane_results.executed_tests(output), 10, output)
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

    def test_zero_match_lists_disabled_methods_and_suggests_the_exact_selector_once(self):
        status, output = self.run_matrix("--", "-only-testing:T/A/testA",
                                         FAKE_TESTS=json.dumps(self.SHARDED))
        self.assertEqual(status, 1, output)
        self.assertEqual(output.count("try the Swift Testing selector '-only-testing:T/A/testA()'"), 1, output)
        self.assertEqual(len(self.shard_runs()), 2)
        self.assertEqual(len([call for call in self.calls() if "-enumerate-tests" in call]), 1)
        self.assert_every_device_returned()

    def test_sharded_zero_match_uses_the_existing_listing_before_any_test_runs(self):
        status, output = self.run_matrix("--shards", "2", "--", "-only-testing:T/A/testA",
                                         FAKE_TESTS=json.dumps(self.SHARDED))
        self.assertEqual(status, 1, output)
        self.assertIn("try the Swift Testing selector '-only-testing:T/A/testA()'", output)
        self.assertIn("no tests were listed", output)
        self.assertFalse(self.shard_runs())
        self.assertEqual(len([call for call in self.calls() if "-enumerate-tests" in call]), 1)
        self.assert_every_device_returned()

    def test_without_shards_valid_xctest_suite_and_parameterized_selectors_need_no_listing(self):
        status, output = self.run_matrix("--", "-only-testing:T/A/testLegacy", "-only-testing:T/B",
                                         "-only-testing:T/C/testValue(label:)",
                                         FAKE_TESTS='["T/A/testLegacy", "T/B/testSuite()", "T/C/testValue(label:)"]')
        self.assertEqual(status, 0, output)
        self.assertIn("PASS iPhone-17-Pro: Executed 3 tests", output)
        self.assertFalse([call for call in self.calls() if "-enumerate-tests" in call])

    def test_a_stale_listing_does_not_supply_a_hint_when_current_enumeration_fails(self):
        logs = self.root / ".build/simulator-tests/logs"
        logs.mkdir(parents=True)
        (logs / "enumerated.json").write_text(json.dumps({"values": [{
            "enabledTests": [], "disabledTests": [{"identifier": "T/A/testA()"}]}]}))
        status, output = self.run_matrix("--", "-only-testing:T/A/testA",
                                         FAKE_TESTS=json.dumps(self.SHARDED), FAKE_ENUMERATE_FAIL="1")
        self.assertEqual(status, 1, output)
        self.assertNotIn("try the Swift Testing selector", output)
        self.assertIn("no tests ran", output)

    def test_selector_corrections_leave_exact_suite_skipped_and_ambiguous_methods_alone(self):
        for selector, methods, extra in (
            ("T/A/test", {"T/A/test", "T/A/test()"}, []),
            ("T/A", {"T/A/test()"}, []),
            ("T/A/Nested", {"T/A/Nested()", "T/A/Nested/test()"}, []),
            ("T/A/test(label:)", {"T/A/test()"}, []),
            ("T/A/test", {"T/A/test(label:)"}, []),
            ("T/A/test", {"T/A/test()", "T/A/test(label:)"}, []),
            ("T/A/test", {"T/A/test()"}, ["-skip-testing:T/A"]),
        ):
            with self.subTest(selector=selector, methods=methods, extra=extra):
                self.assertEqual(list(matrix.selector_corrections([f"-only-testing:{selector}", *extra], methods)), [])

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

    def runner_timeout_record(self):
        # Project the retained EPUBLib dispatch 20261119T062213Z-19786 shard JSON back onto the two
        # xcresulttool views. The summary has the diagnostic; the synthetic test case has no issue child.
        fixture = Path(__file__).with_name("fixtures") / "XCTestRunnerPreparationTimeout.json"
        return json.loads(fixture.read_text())

    def run_ipad_bundle(self, record, *, console=""):
        return self.run_matrix("--shards", "1", wait_for_fake_boot=True, FAKE_TESTS='["T/A/testA()"]',
                               FAKE_BUNDLE_RECORD_ON="iPad-Air-13-inch-M4", FAKE_BUNDLE_RECORD=json.dumps(record),
                               FAKE_BUNDLE_CONSOLE=console, QUEUEKEEPER_RESULTS_DIR=str(self.root / "lane-results"))

    def test_a_runner_preparation_timeout_is_an_unmeasured_shard_with_zero_tests(self):
        record = self.runner_timeout_record()
        status, output = self.run_ipad_bundle(record)
        message = "The test runner timed out while preparing to run tests."
        self.assertEqual(status, 3, output)
        self.assertIn("UNMEASURED iPad-Air-13-inch-M4: Executed 0 tests in 1 shards", output)
        self.assertIn("PASS iPhone-17-Pro: Executed 1 tests in 1 shards", output)
        self.assertIn(message, output)
        counts = lane_report.counts(output)
        self.assertEqual((counts["passed"], counts["failed"], counts["skipped"], counts["unmeasured"]), (1, 0, 0, 0))
        self.assertEqual(len(self.shard_runs()), 2)
        results = self.root / "lane-results"
        kept = results / "simulator-tests/iPad-Air-13-inch-M4-1"
        artifact = json.loads(kept.with_suffix(".json").read_text())
        self.assertEqual((artifact["status"], artifact["tests"], artifact["summary"]), (3, [], record[0]))
        self.assertEqual((artifact["suiteSeconds"], artifact["bundleUnread"]), ({}, message))
        self.assertEqual(kept.with_suffix(".log").read_text(), "")
        self.assertTrue((self.root / ".build/simulator-tests/logs/iPad-Air-13-inch-M4-1.xcresult").is_dir())
        self.assertFalse(lane_report.records(results))
        self.assert_every_device_returned()

    def test_a_runner_timeout_with_test_activity_or_a_different_failure_still_fails(self):
        named_case = {"nodeType": "Test Case", "name": "testRendering()", "result": "Failed"}
        for scenario, counted in (("named failure", "1 tests, 1 failed"), ("partial run", "2 tests, 1 failed"),
                                  ("test in tree only", "1 tests, 1 failed"), ("skipped test", "1 tests, 1 skipped, 1 failed"),
                                  ("console start", "1 tests, 1 failed"), ("different system failure", "1 tests, 1 failed")):
            with self.subTest(scenario):
                summary, tree = self.runner_timeout_record()
                suites = tree["testNodes"][0]["children"][0]["children"]
                console = ""
                if scenario == "named failure":
                    failure = summary["testFailures"][0]
                    failure.update(testName="testRendering()", testIdentifierString="testRendering()")
                    suites[:] = [{"nodeType": "Test Suite", "name": "RenderingTests", "children": [named_case]}]
                elif scenario in ("partial run", "test in tree only", "skipped test"):
                    case_result = "Skipped" if scenario == "skipped test" else "Passed"
                    suites.append({"nodeType": "Test Suite", "name": "RenderingTests",
                                   "children": [{**named_case, "result": case_result}]})
                    if scenario == "partial run":
                        summary.update(totalTestCount=2, passedTests=1)
                    elif scenario == "skipped test":
                        summary.update(totalTestCount=2, skippedTests=1)
                elif scenario == "console start":
                    console = "Test Case '-[RenderingTests testRendering]' started."
                else:
                    summary["testFailures"][0]["failureText"] = "The test runner exited unexpectedly."
                status, output = self.run_ipad_bundle((summary, tree), console=console)
                self.assertEqual(status, 1, output)
                self.assertIn(f"FAIL iPad-Air-13-inch-M4: Executed {counted}", output)
                self.assertIn("PASS iPhone-17-Pro", output)

    def test_a_failed_swift_testing_test_is_counted_failed_in_its_devices_line(self):
        # Its bundle's XCTest run is empty and passes; the lane reads only the devices' lines (vocaro/queuekeeper#81).
        status, output = self.run_matrix("--shards", "2", FAKE_TESTS=json.dumps(self.SHARDED),
                                         FAKE_SWIFT_TESTING_FAIL="iPad-Air-13-inch-M4")
        self.assertEqual(status, 1, output)
        self.assertIn("FAIL iPad-Air-13-inch-M4: Executed 2 tests, 2 failed in 2 shards", output)
        self.assertIn("PASS iPhone-17-Pro: Executed 5 tests in 2 shards", output)
        found = lane_report.counts(output)
        self.assertEqual((found["passed"], found["failed"]), (5, 2), output)
        for path in self.state.glob("testing-*"): path.unlink()
        status, output = self.run_matrix(FAKE_SWIFT_TESTING_FAIL="iPad-Air-13-inch-M4")
        self.assertEqual(status, 1, output)
        self.assertIn("FAIL iPad-Air-13-inch-M4: the tests failed with status 65, Executed 1 tests, 1 failed", output)

    def test_a_shard_with_unconfirmed_cleanup_keeps_the_failures_its_console_names(self):
        args = SimpleNamespace(logs=self.root, shards=2, workers=0, passed=[], test_allowance=30, test_timeout=60,
                               forward_env=[], app_accessibility=False)
        run = matrix.Run(args)
        device = SimpleNamespace(udid="fake")
        failures = (
            "✘ Test testWeight() recorded an issue at InkTests.swift:12:5: Expectation failed\n"
            "✘ Test testWeight() failed after 0.1 seconds with 1 issue.\n"
            "✘ Test testHeight() failed after 0.1 seconds with 1 issue.\n"
        )
        results = self.root / "results"
        lines = []
        kind = "iPad-Air-13-inch-M4"
        shard = f"{kind}-2"

        def unconfirmed(command, *, log_path, **options):
            log_path.write_text(failures)
            raise matrix.processes.Unconfirmed("xcodebuild exited with status 65; reservation retained")

        run.shards[f"{kind}-1"] = (0, 8, 0, [], None)
        with mock.patch.object(run, "say", side_effect=lambda *parts: lines.extend(parts)), \
                mock.patch.object(matrix.processes, "run", side_effect=unconfirmed), \
                mock.patch.object(matrix, "bundle_record", return_value=None), \
                mock.patch.dict(os.environ, {matrix.RESULTS_VARIABLE: str(results)}):
            with self.assertRaisesRegex(matrix.processes.Unconfirmed, "reservation retained"):
                run.test(shard, device, "fixture.xctestrun", ["selection"])
            run.shards_finished(kind)
        self.assertEqual(run.results[kind], 1)
        output = "\n".join(lines)
        found = lane_report.counts(output)
        self.assertEqual((found["passed"], found["failed"]), (8, 2), output)
        self.assertIn(f"{shard}'s cleanup was not confirmed", output)
        kept = lane_report.records(results)
        self.assertEqual(lane_report.recorded_failing(kept),
                         [("testWeight()", "InkTests.swift:12"), ("testHeight()", None)])

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

    def test_a_bad_shard_timing_does_not_discard_valid_history(self):
        logs = self.root / ".build/simulator-tests/logs"
        logs.mkdir(parents=True)
        timings = logs / "shard-timings.json"
        timings.write_text(json.dumps({"T/A": "bad", "T/B": 1, "T/C": 1, "T/D": 50, "T/Old": 25, "T/Corrupt": []}))
        case = {"nodeType": "Test Case", "nodeIdentifier": "T/A/testA()", "result": "Passed",
                "durationInSeconds": 2.5}
        suite = {"nodeType": "Test Suite", "name": "A", "children": [case]}
        bundle = {"nodeType": "Unit test bundle", "name": "T", "children": [suite]}
        record = ({"totalTestCount": 1, "skippedTests": 0, "failedTests": 0}, {"testNodes": [bundle]})

        status, output = self.run_matrix("--shards", "2", FAKE_TESTS=json.dumps(self.SHARDED),
                                         FAKE_BUNDLE_RECORD=json.dumps(record))

        self.assertEqual(status, 0, output)
        shares = [[arg for arg in call if arg.startswith("-only-testing:")] for call in self.shard_runs()]
        self.assertIn(["-only-testing:T/D"], shares, output)
        self.assertEqual(json.loads(timings.read_text()), {"T/A": 2.5, "T/B": 1, "T/C": 1, "T/D": 50, "T/Old": 25})

    def test_each_shard_keeps_its_per_test_lines_and_bundle_summary_in_its_lanes_results(self):
        # They come back with the lane's dispatch and are kept by commit, where the logs folder's are overwritten (#218).
        results = self.root / "lane-results"
        status, output = self.run_matrix("--shards", "2", FAKE_TESTS=json.dumps(self.SHARDED), FAKE_BUNDLE="1",
                                         FAKE_RESULT_LINES="1", FAKE_TEST_FAIL="iPad-Air-13-inch-M4",
                                         QUEUEKEEPER_RESULTS_DIR=str(results))
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
                # Each test case's outcome, as `qk results keep` records it (#260).
                self.assertEqual(record["tests"], [{"test": "T/testA()", "outcome": "skipped", "seconds": None}])
                self.assertIsNone(record["bundleUnread"])
                self.assertIsInstance(record["seconds"], int)

    def test_outside_a_lane_nothing_is_kept_and_a_missing_log_or_bundle_keeps_no_lines(self):
        status, output = self.run_matrix("--shards", "2", FAKE_TESTS=json.dumps(self.SHARDED), FAKE_BUNDLE="1")
        self.assertEqual(status, 0, output)
        self.assertFalse(list(self.root.rglob("iPhone-17-Pro-1.json")), output)
        from queuekeeper_apple.simulators import matrix
        with mock.patch.dict(os.environ, clear=True):
            self.assertIsNone(matrix.kept("iPhone-17-Pro-1", self.root / "absent.log", None, 0, 1))
        results = self.root / "lane-results"
        self.assertEqual(matrix.kept("iPad-Air-13-inch-M4-2", self.root / "absent.log", None, None, None, results),
                         results / "simulator-tests")
        self.assertEqual(sorted(p.name for p in (results / "simulator-tests").iterdir()), ["iPad-Air-13-inch-M4-2.json"])
        record = json.loads((results / "simulator-tests/iPad-Air-13-inch-M4-2.json").read_text())
        self.assertEqual(record, {"run": "iPad-Air-13-inch-M4-2", "keptBy": "qk simulator-tests", "status": None, "seconds": None, "log": None,
                                  "resultLines": None, "summary": None, "suiteSeconds": None, "tests": None,
                                  "bundleUnread": matrix.UNREAD})

    def test_shards_and_workers_cannot_be_asked_for_together(self):
        status, output = self.run_matrix("--shards", "2", "--workers", "2")
        self.assertEqual(status, 2, output)

    def test_a_lane_that_ends_with_its_separator_takes_a_filter_from_eng_run(self):
        matrix = [sys.executable, str(ENG), "simulator-tests", "--unadmitted", "--scheme", "Package-Tests", "--runtime", "iOS-27-0",
                  "--device", "iPhone-17-Pro", "--device", "iPad-Air-13-inch-M4"]
        # The pool's name comes from the project's configuration, which switches on the plugin that reads it: the
        # --project given after the launcher's names the project whose plugins load (vocaro/queuekeeper#345).
        (self.root / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Project",
            "plugins": ["queuekeeper_apple"], "simulatorPool": "Project", "lanes": {
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
                      (self.root / ".build/queuekeeper/unseparated/run.log").read_text())
        self.assertFalse((self.state / "simctl.log").exists())
        records = (self.root / "slots/admissions.jsonl").read_text().splitlines()
        self.assertIn("Project unseparated", {json.loads(line)["label"] for line in records})

    def test_a_run_without_a_pool_name_refuses_before_taking_a_device(self):
        command = [sys.executable, str(ENG), "simulator-tests", "--unadmitted", "--scheme", "Package-Tests", "--runtime", "iOS-27-0",
                   "--device", "iPhone-17-Pro"]
        # A project that names no pool, run by a launcher whose own checkout (the toolkit) names one: the
        # run's project decides, so it refuses rather than taking the toolkit's members.
        (self.root / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Project"}))
        finished = subprocess.run(command, cwd=self.root, capture_output=True, text=True, env=self.environment)
        self.assertEqual(finished.returncode, 2, finished.stdout + finished.stderr)
        self.assertIn("name the simulator pool", finished.stderr)
        self.assertFalse((self.state / "simctl.log").exists())

    def test_a_run_outside_a_project_root_takes_the_launchers_pool(self):
        # A directory with no queuekeeper.json, such as a project's subdirectory, has the launcher's project
        # name the pool: here Queuekeeper's own, which names Queuekeeper.
        command = [sys.executable, str(ENG), "simulator-tests", "--unadmitted", "--scheme", "Package-Tests", "--runtime", "iOS-27-0",
                   "--device", "iPhone-17-Pro"]
        finished = subprocess.run(command, cwd=self.root, capture_output=True, text=True, env=self.environment)
        self.assertEqual(finished.returncode, 0, finished.stdout + finished.stderr)
        self.assertEqual(sorted(self.devices()), ["Queuekeeper iPhone 17 Pro 1"])

    def test_invalid_arguments_create_no_device(self):
        for arguments in (["--jobs", "0"], ["--device", "iPhone-17-Pro"], ["--build-jobs", "0"]):
            with self.subTest(arguments):
                self.assertEqual(self.run_matrix(*arguments)[0], 2)
        self.assertFalse((self.state / "simctl.log").exists())


if __name__ == "__main__":
    unittest.main()
