from contextlib import redirect_stdout
from datetime import datetime, timedelta
from io import StringIO
import json
import os
from pathlib import Path
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch
SOURCE = Path(__file__).resolve().parents[1] / "src/python"
sys.path.insert(0, str(SOURCE))
from queuekeeper import cli, lane_results
from queuekeeper.apple import xctest_shards
import host_isolation

# Stand-ins for swift and xcrun that answer as the toolchain does (Swift 6.4, Xcode 27.2), so these
# tests build nothing. tests/fixtures/XCTestShards is the real toolchain's positive control.
TESTS = ["Alpha.DoublingTests/testTwo", "Alpha.DoublingTests/testThree", "Alpha.SkippingTests/testSkips",
         "Beta.FailingTests/testFailsOnRequest", "Beta.ZeroTests/testZero"]
SWIFT_TESTING = ["Alpha.doublingInSwiftTesting()"]
SWIFT = textwrap.dedent("""
    import json, os, pathlib, sys
    here = pathlib.Path(os.environ["FAKE_ROOT"])
    arguments = sys.argv[1:]
    with open(here / "calls.jsonl", "a") as calls: calls.write(json.dumps(["swift", *arguments, pathlib.Path.cwd().name]) + "\\n")
    if arguments[:2] == ["build", "--show-bin-path"]: print(here / "bin")
    elif arguments[:1] == ["build"]: sys.exit(int(os.environ.get("FAKE_BUILD_STATUS", "0")))
    elif arguments[:2] == ["test", "list"]:
        tests = json.loads(os.environ["FAKE_TESTS"])
        if isinstance(tests, dict): tests = tests[pathlib.Path.cwd().name]
        if "--disable-swift-testing" not in arguments: tests += json.loads(os.environ["FAKE_SWIFT_TESTING"])
        print("\\n".join(tests))
    elif arguments[:1] == ["test"]:
        import re
        filters = [argument.split("=", 1)[1] for argument in arguments if argument.startswith("--filter=")]
        # Swift Testing matches a filter against an id that goes on past the listed one, as its source location.
        chosen = [test for test in json.loads(os.environ["FAKE_SWIFT_TESTING"])
                  if not filters or any(re.search(pattern, test + "/x.swift:1:2") for pattern in filters)]
        chosen = chosen[:len(chosen) - int(os.environ.get("FAKE_SWIFT_TESTING_DROP", "0"))]
        suites = sorted({test.split("/")[0].split(".", 1)[1] for test in chosen if "/" in test})
        if "--event-stream-output-path" in arguments:
            # Swift Testing's event stream, version 0: the test records, then each event as it happens.
            import time
            stream = open(arguments[arguments.index("--event-stream-output-path") + 1], "w")
            def record(what, **payload):
                stream.write(json.dumps({"version": 0, "kind": what, "payload": payload}) + "\\n")
                stream.flush()
            def event(kind, test, **more):
                record("event", kind=kind, testID=test + "/x.swift:1:2", messages=[],
                       instant={"absolute": time.monotonic(), "since1970": time.time()}, **more)
            for suite in sorted({test.split("/")[0] for test in chosen if "/" in test}):
                record("test", kind="suite", id=suite + "/x.swift:1:2", name=suite)
            for test in chosen: record("test", kind="function", id=test + "/x.swift:1:2", name=test)
            delayed = os.environ.get("FAKE_EVENTS_DELAY")
            if delayed and any(re.search(delayed, test) for test in chosen): time.sleep(1.5)
            for test in chosen:
                if "/" in test: event("testStarted", test.split("/")[0])
                event("testStarted", test)
                if os.environ.get("FAKE_EVENTS_HANG") and re.search(os.environ["FAKE_EVENTS_HANG"], test): time.sleep(60)
                time.sleep(0.05)
                if os.environ.get("FAKE_EVENTS_FAIL") and re.search(os.environ["FAKE_EVENTS_FAIL"], test):
                    event("issueRecorded", test, issue={"isKnown": False})
                event("testEnded", test)
                if "/" in test: event("testEnded", test.split("/")[0])
        for test in chosen: print(f"✔ Test {test.split('/')[-1].split('.')[-1]} passed after 0.001 seconds.")
        for suite in suites: print(f"✔ Suite {suite} passed after 2.500 seconds.")
        print(f"✔ Test run with {len(chosen)} test{'s' if len(chosen) != 1 else ''} in {len(suites)} suites passed "
              "after 0.001 seconds.")
""")
XCRUN = textwrap.dedent("""
    import json, os, pathlib, sys
    here = pathlib.Path(os.environ["FAKE_ROOT"])
    _, _, _, selectors, bundle = sys.argv
    with open(here / "calls.jsonl", "a") as calls: calls.write(json.dumps(["xcrun", selectors, bundle]) + "\\n")
    module = pathlib.Path(bundle).stem
    every = json.loads(os.environ["FAKE_TESTS"])
    every = [test for tests in every.values() for test in tests] if isinstance(every, dict) else every
    tests = [test for test in every if test.startswith(module + ".")
             and any(test == s or test.split("/")[0] == s for s in selectors.split(","))]
    tests = tests[:len(tests) - int(os.environ.get("FAKE_DROP", "0"))]
    if os.environ.get("FAKE_HANG") == module: __import__("time").sleep(60)
    failed = skipped = 0
    print("Test Suite 'Selected tests' started at 2026-10-31 09:20:16.490.")
    for test in tests:
        name = "-[" + test.replace("/", " ") + "]"
        if "Skip" in test:
            skipped += 1
            reason = "Unmeasured: the 80 s backstop passed" if os.environ.get("FAKE_UNMEASURED") else "planted"
            print(f"x.swift:1: {name} : Test skipped - {reason}")
            print(f"Test Case '{name}' skipped (0.001 seconds).")
        elif "Fail" in test and os.environ.get("FAKE_FAIL"):
            failed += 1
            print(f"x.swift:1: error: {name} : planted failure")
            print(f"Test Case '{name}' failed (0.250 seconds).")
        else: print(f"Test Case '{name}' passed (1.500 seconds).")
    skips = f"{skipped} test{'s' if skipped != 1 else ''} skipped and " if skipped else ""
    print(f"Test Suite 'Selected tests' {'failed' if failed else 'passed'} at 2026-10-31 09:20:16.493.")
    print(f"\\t Executed {len(tests)} test{'s' if len(tests) != 1 else ''}, with {skips}{failed} failures (0 unexpected) in 0.1 (0.1) seconds")
    sys.exit(1 if failed else 0)
""")


class ShardPackingTests(unittest.TestCase):
    def test_longest_classes_go_first_into_the_least_loaded_shard(self):
        selected = [f"M.C{n}/test" for n in range(5)]
        seconds = {"M.C0": 10, "M.C1": 8, "M.C2": 5, "M.C3": 4, "M.C4": 1}
        packed, _ = xctest_shards.shards(selected, seconds, 2)
        # 10 and 8 apart; 5 beside the 8, 4 beside the 10, 1 beside the 13.
        self.assertEqual(packed, [(14, ["M.C0", "M.C3"]), (14, ["M.C1", "M.C2", "M.C4"])])

    def test_a_class_with_no_time_counts_the_median(self):
        selected = ["M.Known1/a", "M.Known2/a", "M.Known3/a", "M.New/a"]
        packed, _ = xctest_shards.shards(selected, {"M.Known1": 1, "M.Known2": 3, "M.Known3": 9}, 4)
        self.assertEqual([names for _, names in packed], [["M.Known3"], ["M.Known2"], ["M.New"], ["M.Known1"]])

    def test_no_more_shards_than_classes_and_every_test_kept(self):
        selected = ["M.A/one", "M.A/two", "M.B/one"]
        packed, classes = xctest_shards.shards(selected, {}, 12)
        self.assertEqual(len(packed), 2)
        self.assertEqual(classes, {"M.A": ["M.A/one", "M.A/two"], "M.B": ["M.B/one"]})

    def test_class_seconds_sum_every_outcome(self):
        log = ("Test Case '-[M.A one]' passed (1.250 seconds).\n"
               "Test Case '-[M.A two]' failed (0.750 seconds).\n"
               "Test Case '-[M.B one]' skipped (0.001 seconds).\n"
               "Test Case '-[M.B one]' started.\n")
        self.assertEqual(xctest_shards.class_seconds(log), {"M.A": 2.0, "M.B": 0.001})


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.root))
        tools = self.root / "tools"
        tools.mkdir()
        for name, body in (("swift", SWIFT), ("xcrun", XCRUN)):
            path = tools / name
            path.write_text(f"#!{sys.executable}\n{body}")
            host_isolation.executable(path)
        for module in ("Alpha", "Beta", "Gamma"): (self.root / "bin" / f"{module}.xctest").mkdir(parents=True)
        self.package = self.root / "package"
        self.package.mkdir()
        # Not the results/ of a lane these tests run in, which would keep the stand-ins' test times.
        patcher = patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(xctest_shards.parallel.RESULTS_VARIABLE, None)
        self.environment = {"PATH": f"{tools}{os.pathsep}{os.environ['PATH']}", "FAKE_ROOT": str(self.root),
                            "FAKE_TESTS": json.dumps(TESTS), "FAKE_SWIFT_TESTING": json.dumps(SWIFT_TESTING)}

    def run_eng(self, *arguments, **environment):
        output = StringIO()
        with patch.dict(os.environ, {**self.environment, **environment}), redirect_stdout(output):
            status = cli.main(["xctest", "--unadmitted", "-C", str(self.package), *arguments])
        return status, output.getvalue()

    def calls(self):
        return [json.loads(line) for line in (self.root / "calls.jsonl").read_text().splitlines()]

    def test_every_class_runs_once_and_each_skip_counts_as_one(self):
        status, output = self.run_eng("-j", "2")
        self.assertEqual(status, 0, output)
        # As a lane counts the log: each process's totals once, the Swift Testing run's too.
        self.assertEqual(lane_results.counted(output), (6, 1))
        self.assertIn("queuekeeper xctest: 6 tests (1 skipped) in 5 processes, 2 at a time", output)
        xctest = [call for call in self.calls() if call[0] == "xcrun"]
        selectors = sorted(s for call in xctest for s in call[1].split(","))
        self.assertEqual(selectors, ["Alpha.DoublingTests", "Alpha.SkippingTests", "Beta.FailingTests", "Beta.ZeroTests"])
        # Each process loads only the bundle of its own classes' module.
        for _, chosen, bundle in xctest:
            self.assertTrue(all(s.startswith(Path(bundle).stem + ".") for s in chosen.split(",")), (chosen, bundle))
        self.assertEqual(self.calls()[0][:3], ["swift", "build", "--build-tests"])

    def test_options_reach_every_swift_command(self):
        status, output = self.run_eng("-c", "release", "--scratch-path", ".build/optimized",
                                      "--swift-option=-Xswiftc", "--swift-option=-warnings-as-errors", "--build-jobs", "2")
        self.assertEqual(status, 0, output)
        swift = [call for call in self.calls() if call[0] == "swift"]
        self.assertEqual({tuple(call[:2]) for call in swift},
                         {("swift", "build"), ("swift", "test")})
        for call in swift:
            # A test run's event stream, for its tests' start and finish times, goes after them.
            if "--event-stream-output-path" in call: call = call[:-5] + call[-1:]
            self.assertEqual(call[-7:-1], ["-c", "release", "--scratch-path", ".build/optimized", "-Xswiftc",
                                           "-warnings-as-errors"], call)
        self.assertIn("--jobs", self.calls()[0])
        self.assertTrue((self.package / xctest_shards.TIMINGS.format(configuration="release")).exists())

    def test_a_test_the_host_decided_leaves_the_run_unmeasured_not_passed(self):
        status, output = self.run_eng("--skip-build", FAKE_UNMEASURED="1")
        self.assertEqual(status, lane_results.UNMEASURED, output)
        self.assertIn("UNMEASURED: 1 tests the host decided", output)
        self.assertTrue(output.rstrip().endswith("Test skipped - Unmeasured: the 80 s backstop passed"), output)

    def test_a_failure_outranks_an_unmeasured_test(self):
        status, output = self.run_eng("--skip-build", FAKE_UNMEASURED="1", FAKE_FAIL="1")
        self.assertEqual(status, 1, output)

    def test_several_packages_build_in_turn_and_share_the_processes(self):
        second = self.root / "second"
        second.mkdir()
        tests = {"package": TESTS, "second": ["Gamma.LongTests/testLong", "Gamma.LongTests/testLonger"]}
        (second / xctest_shards.TIMINGS.format(configuration="debug")).parent.mkdir(parents=True)
        (second / xctest_shards.TIMINGS.format(configuration="debug")).write_text(json.dumps({"Gamma.LongTests": 500}))
        status, output = self.run_eng("-C", str(second), "-j", "1", FAKE_TESTS=json.dumps(tests))
        self.assertEqual(status, 0, output)
        # Five XCTest tests and two, and each package's one Swift Testing test.
        self.assertEqual(lane_results.counted(output), (9, 1))
        builds = [call[-1] for call in self.calls() if call[:3] == ["swift", "build", "--build-tests"]]
        self.assertEqual(builds, ["package", "second"])
        xctest = [call[1] for call in self.calls() if call[0] == "xcrun"]
        # The longest shard of either package runs first.
        self.assertEqual(xctest[0], "Gamma.LongTests")
        self.assertIn("== second: shard 1 of 1, Gamma", output)

    def test_a_failed_test_fails_the_run(self):
        status, output = self.run_eng("--skip-build", FAKE_FAIL="1")
        self.assertEqual(status, 1)
        self.assertIn("planted failure", output)
        self.assertRegex(output, r"queuekeeper xctest: .*FAILED: shard \d+ of \d+, Beta")

    def test_a_process_past_its_timeout_is_stopped_and_leaves_the_run_unmeasured(self):
        # Fixed: the 5 s limit is the behavior under test. A sibling shard that load held past it would be left
        # unmeasured too, which none of these assertions rules out, so it cannot fail the test.
        status, output = self.run_eng("--skip-build", "--timeout", "5", FAKE_HANG="Beta")
        self.assertEqual(status, lane_results.UNMEASURED, output)
        self.assertRegex(output, r"== shard \d+ of \d+, Beta .*: timed out after 5s")
        self.assertRegex(output, r"queuekeeper xctest: .*UNMEASURED: shard \d+ of \d+, Beta .*\(timed out after 5s\)")
        self.assertNotIn("FAILED", output)

    def test_a_process_that_runs_fewer_tests_than_it_was_given_fails(self):
        status, output = self.run_eng("--skip-build", "--filter", "Doubling", FAKE_DROP="1")
        self.assertEqual(status, 1)
        self.assertIn("(ran 1 of 2 tests)", output)

    def test_a_filter_names_single_tests_and_skips_swift_testing_it_does_not_match(self):
        status, output = self.run_eng("--skip-build", "--filter", "testTwo$")
        self.assertEqual(status, 0, output)
        self.assertEqual([call[1] for call in self.calls() if call[0] == "xcrun"], ["Alpha.DoublingTests/testTwo"])
        self.assertFalse([call for call in self.calls() if call[:2] == ["swift", "test"] and "list" not in call])
        self.assertEqual(lane_results.counted(output), (1, 0))

    def test_a_filter_that_matches_nothing_runs_nothing(self):
        status, output = self.run_eng("--skip-build", "--filter", "NoSuchTest")
        self.assertEqual(status, xctest_shards.parallel.NO_TESTS)
        self.assertFalse([call for call in self.calls() if call[0] == "xcrun"])

    def test_a_failed_build_runs_nothing(self):
        status, _ = self.run_eng(FAKE_BUILD_STATUS="65")
        self.assertEqual(status, 65)
        self.assertEqual([call[:2] for call in self.calls()], [["swift", "build"]])

    def test_the_last_run_orders_the_next(self):
        self.run_eng("--skip-build")
        timings = json.loads((self.package / xctest_shards.TIMINGS.format(configuration="debug")).read_text())
        self.assertEqual(timings["Alpha.DoublingTests"], 3.0)
        (self.package / xctest_shards.TIMINGS.format(configuration="debug")).write_text(json.dumps(
            {"Alpha.DoublingTests": 1, "Alpha.SkippingTests": 1, "Beta.FailingTests": 1, "Beta.ZeroTests": 99}))
        (self.root / "calls.jsonl").unlink()
        self.run_eng("--skip-build", "-j", "1")
        self.assertEqual([call[1] for call in self.calls() if call[0] == "xcrun"][0], "Beta.ZeroTests")


class SplitSwiftTestingTests(unittest.TestCase):
    """--split-swift-testing: Swift Testing suites packed into the shards beside the XCTest classes, each
    shard's suites in a swift test process of their own that runs one test at a time."""
    SUITES = ["Alpha.SuiteA/one()", "Alpha.SuiteA/two()", "Alpha.SuiteB/three()", "Alpha.doublingInSwiftTesting()"]

    # The same stand-in toolchain as RunnerTests', without running its tests again.
    run_eng, calls = RunnerTests.run_eng, RunnerTests.calls

    def setUp(self):
        RunnerTests.setUp(self)
        self.environment["FAKE_SWIFT_TESTING"] = json.dumps(self.SUITES)

    def swift_testing_runs(self):
        return [call for call in self.calls() if call[:2] == ["swift", "test"] and call[2] != "list"]

    def selected(self):
        import re
        runs = self.swift_testing_runs()
        filters = [[argument.split("=", 1)[1] for argument in call if argument.startswith("--filter=")] for call in runs]
        return sorted(test for patterns in filters for test in self.SUITES
                      if any(re.search(pattern, test) for pattern in patterns))

    def test_each_shard_runs_its_suites_one_test_at_a_time_and_every_test_once(self):
        status, output = self.run_eng("--skip-build", "-j", "2", "--split-swift-testing")
        self.assertEqual(status, 0, output)
        runs = self.swift_testing_runs()
        self.assertGreater(len(runs), 1, "the suites are split over several processes")
        for call in runs:
            self.assertIn("--no-parallel", call)
            self.assertIn("--disable-xctest", call)
            # Shards of one package run at once only past SwiftPM's lock on its .build.
            self.assertIn("--ignore-lock", call)
        # A suite goes whole into one process, and every Swift Testing test runs exactly once.
        self.assertEqual(self.selected(), sorted(self.SUITES))
        self.assertIn("--filter=^Alpha\\.SuiteA/", [argument for call in runs for argument in call])
        self.assertEqual(lane_results.counted(output), (9, 1))
        self.assertRegex(output, r"== shard \d+ of \d+, Swift Testing \(about \d+s\): PASS")

    def test_without_the_option_swift_testing_runs_once_concurrently(self):
        status, output = self.run_eng("--skip-build", "-j", "2")
        self.assertEqual(status, 0, output)
        runs = self.swift_testing_runs()
        self.assertEqual(len(runs), 1)
        self.assertNotIn("--no-parallel", runs[0])
        self.assertNotIn("--ignore-lock", runs[0])
        self.assertFalse([argument for argument in runs[0] if argument.startswith("--filter")])
        self.assertEqual(lane_results.counted(output), (9, 1))

    def test_a_filter_selects_single_tests_of_a_suite(self):
        status, output = self.run_eng("--skip-build", "--split-swift-testing", "--filter", "SuiteA/one")
        self.assertEqual(status, 0, output)
        self.assertEqual(self.selected(), ["Alpha.SuiteA/one()"])
        self.assertFalse([call for call in self.calls() if call[0] == "xcrun"])
        self.assertEqual(lane_results.counted(output), (1, 0))

    def test_a_process_that_runs_fewer_swift_testing_tests_than_it_was_given_fails(self):
        status, output = self.run_eng("--skip-build", "--split-swift-testing", "--filter", "SuiteA",
                                      FAKE_SWIFT_TESTING_DROP="1")
        self.assertEqual(status, 1, output)
        self.assertIn("(ran 1 of 2 tests)", output)

    def test_a_suites_last_time_orders_the_next_run(self):
        timings = self.package / xctest_shards.TIMINGS.format(configuration="debug")
        self.run_eng("--skip-build", "--split-swift-testing")
        self.assertEqual(json.loads(timings.read_text())["Alpha.SuiteA"], 2.5)
        timings.write_text(json.dumps({"Alpha.SuiteB": 99}))
        (self.root / "calls.jsonl").unlink()
        self.run_eng("--skip-build", "-j", "1", "--split-swift-testing")
        first = self.swift_testing_runs()[0]
        self.assertIn("--filter=^Alpha\\.SuiteB/", first)
        self.assertNotIn("--filter=^Alpha\\.SuiteA/", first)

    def test_suite_seconds_reads_only_the_units_a_process_ran(self):
        log = ("✔ Suite SuiteA passed after 2.500 seconds.\n✘ Suite SuiteB failed after 1.250 seconds with 1 issue.\n"
               "✔ Suite Inner passed after 9.000 seconds.\n✔ Suite \"Shown name\" passed after 3.000 seconds.\n")
        self.assertEqual(xctest_shards.suite_seconds(log, ["M.SuiteA", "M.SuiteB"]), {"M.SuiteA": 2.5, "M.SuiteB": 1.25})


class SwiftTestingTimesTests(unittest.TestCase):
    """Each Swift Testing test's wall-clock start and finish, from its process's event stream, kept with its
    id, shard, verdict and seconds (vocaro-engineering#367)."""
    run_eng, calls = RunnerTests.run_eng, RunnerTests.calls
    setUp = SplitSwiftTestingTests.setUp
    SUITES = SplitSwiftTestingTests.SUITES

    def kept(self, results=None):
        path = (Path(results) / xctest_shards.TEST_TIMES if results
                else self.package / xctest_shards.LOCAL_TEST_TIMES.format(configuration="debug"))
        return json.loads(path.read_text())["tests"]

    def test_shards_run_in_another_order_than_printed_keep_each_tests_own_times(self):
        # SuiteA's shard is printed before the others' and starts its tests 1.5 s after them.
        status, output = self.run_eng("--skip-build", "-j", "3", "--split-swift-testing", FAKE_EVENTS_DELAY="SuiteA")
        self.assertEqual(status, 0, output)
        printed = [line for line in output.splitlines() if line.startswith("== ") and "Swift Testing" in line]
        self.assertIn("about", printed[0])
        tests = self.kept()
        self.assertEqual(sorted(entry["test"] for entry in tests), sorted(self.SUITES))
        shard_of = {entry["test"]: entry["shard"] for entry in tests}
        first_printed = printed[0][3:printed[0].index(": ")]
        self.assertEqual({shard_of["Alpha.SuiteA/one()"], shard_of["Alpha.SuiteA/two()"]}, {first_printed})
        self.assertNotEqual(shard_of["Alpha.SuiteB/three()"], first_printed)
        # Kept in the order they started, not the order their output was printed.
        self.assertEqual([entry["test"] for entry in tests][-2:], ["Alpha.SuiteA/one()", "Alpha.SuiteA/two()"])
        started = {entry["test"]: datetime.fromisoformat(entry["started"]) for entry in tests}
        self.assertGreater(started["Alpha.SuiteA/one()"] - started["Alpha.SuiteB/three()"], timedelta(seconds=1))
        for entry in tests:
            self.assertEqual(entry["verdict"], "passed")
            finished = datetime.fromisoformat(entry["finished"])
            self.assertIsNotNone(finished.utcoffset())
            self.assertGreaterEqual(entry["seconds"], 0.05)
            self.assertAlmostEqual((finished - started[entry["test"]]).total_seconds(), entry["seconds"], delta=0.01)
        self.assertRegex(output, r"queuekeeper xctest: 4 Swift Testing tests' start and finish times in .*swift-testing-times")

    def test_a_test_whose_process_times_out_keeps_its_start(self):
        status, output = self.run_eng("--skip-build", "--split-swift-testing", "--filter", "SuiteB", "--timeout", "5",
                                      FAKE_EVENTS_HANG="three")
        self.assertEqual(status, lane_results.UNMEASURED, output)
        [entry] = self.kept()
        self.assertEqual(entry["test"], "Alpha.SuiteB/three()")
        self.assertEqual(entry["verdict"], "unmeasured")
        self.assertEqual(entry["reason"], "timed out")
        self.assertIsNotNone(entry["started"])
        self.assertIsNone(entry["finished"])
        self.assertIsNone(entry["seconds"])

    def test_a_lanes_results_keep_them_and_a_second_run_writes_beside_the_first(self):
        results = self.root / "results"
        for _ in range(2):
            status, output = self.run_eng("--skip-build", "--filter", "SuiteA", FAKE_EVENTS_FAIL="two",
                                          QUEUEKEEPER_RESULTS_DIR=str(results))
        self.assertEqual({entry["test"]: entry["verdict"] for entry in self.kept(results)},
                         {"Alpha.SuiteA/one()": "passed", "Alpha.SuiteA/two()": "failed"})
        self.assertTrue((results / "xctest/swift-testing-times-2.json").is_file())
        self.assertEqual({entry["shard"] for entry in self.kept(results)}, {"Swift Testing"})
        self.assertFalse((self.package / xctest_shards.LOCAL_TEST_TIMES.format(configuration="debug")).exists())

    def test_xctest_alone_keeps_no_times(self):
        self.environment["FAKE_SWIFT_TESTING"] = "[]"
        status, output = self.run_eng("--skip-build")
        self.assertEqual(status, 0, output)
        self.assertNotIn("start and finish times", output)
        self.assertFalse((self.package / xctest_shards.LOCAL_TEST_TIMES.format(configuration="debug")).exists())


class EventStreamTests(unittest.TestCase):
    """test_times over event streams written as Swift Testing writes them."""

    @staticmethod
    def stream(*records):
        return "\n".join(json.dumps({"version": 0, "kind": kind, "payload": payload}) for kind, payload in records)

    @staticmethod
    def event(kind, test, at, **more):
        return "event", {"kind": kind, "testID": test, "instant": {"absolute": at - 1000, "since1970": at}, **more}

    def test_verdicts_and_times_of_functions_not_suites(self):
        at = 1_794_000_000.0
        text = self.stream(
            ("test", {"kind": "suite", "id": "M.S/S.swift:3:2"}),
            ("test", {"kind": "function", "id": "M.S/ok()/S.swift:4:3"}),
            ("test", {"kind": "function", "id": "M.S/bad()/S.swift:5:3"}),
            ("test", {"kind": "function", "id": "M.S/known()/S.swift:6:3"}),
            ("test", {"kind": "function", "id": "M.S/off()/S.swift:7:3"}),
            self.event("runStarted", None, at),
            self.event("testStarted", "M.S/S.swift:3:2", at),
            self.event("testStarted", "M.S/ok()/S.swift:4:3", at + 0.5),
            self.event("testStarted", "M.S/bad()/S.swift:5:3", at + 0.25),
            self.event("issueRecorded", "M.S/bad()/S.swift:5:3", at + 0.3, issue={"isKnown": False}),
            self.event("testStarted", "M.S/known()/S.swift:6:3", at + 0.3),
            self.event("issueRecorded", "M.S/known()/S.swift:6:3", at + 0.35, issue={"isKnown": True}),
            self.event("issueRecorded", "M.S/ok()/S.swift:4:3", at + 0.6, issue={"isKnown": False, "severity": "warning"}),
            self.event("testEnded", "M.S/known()/S.swift:6:3", at + 0.4),
            self.event("testEnded", "M.S/bad()/S.swift:5:3", at + 1.0),
            self.event("testSkipped", "M.S/off()/S.swift:7:3", at + 1.0),
            self.event("testEnded", "M.S/ok()/S.swift:4:3", at + 2.0),
            self.event("testEnded", "M.S/S.swift:3:2", at + 2.0))
        found = {entry["test"]: entry for entry in xctest_shards.test_times(text, "shard 1 of 2, Swift Testing")}
        self.assertEqual(list(found), ["M.S/ok()", "M.S/bad()", "M.S/known()", "M.S/off()"])
        self.assertEqual({test: entry["verdict"] for test, entry in found.items()},
                         {"M.S/ok()": "passed", "M.S/bad()": "failed", "M.S/known()": "passed", "M.S/off()": "skipped"})
        self.assertEqual(found["M.S/bad()"]["seconds"], 0.75)
        self.assertEqual(datetime.fromisoformat(found["M.S/ok()"]["started"]).timestamp(), at + 0.5)
        self.assertEqual(datetime.fromisoformat(found["M.S/ok()"]["finished"]).timestamp(), at + 2.0)
        self.assertIsNone(found["M.S/off()"]["started"])
        self.assertEqual({entry["shard"] for entry in found.values()}, {"shard 1 of 2, Swift Testing"})

    def test_a_start_with_no_finish_and_a_line_cut_short_are_kept_as_far_as_they_go(self):
        at = 1_794_000_000.0
        text = self.stream(self.event("testStarted", "M.f()/F.swift:1:2", at),
                           self.event("testStarted", "M.S/S.swift:3:2", at)) + '\n{"version":0,"kind":"ev'
        # Without test records, a function is told from a suite by its parentheses.
        [entry] = xctest_shards.test_times(text, "Swift Testing", timed_out=True)
        self.assertEqual((entry["test"], entry["verdict"], entry["reason"], entry["finished"], entry["seconds"]),
                         ("M.f()", "unmeasured", "timed out", None, None))
        self.assertEqual(datetime.fromisoformat(entry["started"]).timestamp(), at)
        [entry] = xctest_shards.test_times(text, "Swift Testing")
        self.assertEqual(entry["reason"], "its process ended first")


if __name__ == "__main__":
    unittest.main()
