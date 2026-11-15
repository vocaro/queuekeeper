"""`qk results keep`: a passing run's per-test outcomes and seconds, for a lane that runs xcodebuild itself (#260)."""
import contextlib
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src/python"))
from queuekeeper import cli, lane_results, parallel
from queuekeeper_apple import run_results
from queuekeeper_apple.simulators import matrix
import host_isolation

ENG = Path(__file__).resolve().parents[2] / "scripts/qk"
# xcresulttool, printing the summary.json and tests.json a fixture bundle holds; without them, a failure.
FAKE_XCRUN = r'''
import json, pathlib, sys
args = sys.argv[1:]
assert args[:3] == ["xcresulttool", "get", "test-results"], args
bundle = pathlib.Path(args[args.index("--path") + 1])
if args[3] == "activities":
    records = json.loads((bundle / "activities.json").read_text())
    test = args[args.index("--test-id") + 1]
    if test not in records: sys.exit("Error: no activities for the requested test")
    print(json.dumps(records[test]))
    sys.exit(0)
record = bundle / f"{args[3]}.json"
if not record.exists(): sys.exit(f"Error: {record.parent} is not a result bundle")
print(record.read_text())
'''
LOG = """Test Case '-[UI.Smoke testOpens]' started.
Test Case '-[UI.Smoke testOpens]' passed (1.250 seconds).
2026-11-07 16:16:45.147 xctest[1:2] noise that names no outcome
\t Executed 5 tests, with 2 tests skipped and 0 failures (0 unexpected) in 2.0 (2.0) seconds
"""


def case(name, result, seconds=None, *children):
    node = {"name": f"{name}()", "nodeIdentifier": f"Smoke/{name}()", "nodeType": "Test Case", "result": result,
            "children": list(children)}
    if seconds is not None: node["durationInSeconds"] = seconds
    return node


def skip(text):
    return {"name": text, "nodeType": "Skip Message"}


CASES = [case("testOpens", "Passed", 1.25), case("testBroken", "Failed", 0.5), case("testOptional", "Skipped", 0, skip("Test skipped - planted")),
         case("testKnown", "Expected Failure", 0.1), case("testBusy", "Skipped", 0, skip("/p/T.swift:5: Test skipped - Unmeasured: the host was busy")),
         case("testLost", "Passed"), case("testOdd", "Mystery", 0.2)]


class KeepTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        tools = self.root / "bin"
        tools.mkdir()
        (tools / "xcrun").write_text("#!" + sys.executable + "\n" + FAKE_XCRUN)
        host_isolation.executable(tools / "xcrun")
        self.log = self.root / "test.log"
        self.log.write_text(LOG)
        self.bundle = self.root / "smoke.xcresult"
        self.results = self.root / "lane-results"
        self.kept = self.results / matrix.KEPT
        environment = {key: value for key, value in os.environ.items() if key != parallel.RESULTS_VARIABLE}
        environment["PATH"] = f"{tools}{os.pathsep}{os.environ['PATH']}"
        patcher = patch.dict(os.environ, environment, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def fixture(self, cases=CASES, result="Passed"):
        self.bundle.mkdir()
        (self.bundle / "summary.json").write_text(json.dumps({"result": result, "totalTestCount": len(cases)}))
        (self.bundle / "tests.json").write_text(json.dumps({"testNodes": [{"name": "Plan", "nodeType": "Test Plan", "children": [
            {"name": "App", "nodeType": "UI test bundle", "children": [{"name": "Smoke", "nodeType": "Test Suite", "children": cases}]}]}]}))

    def keep(self, *extra, name="iPhone-17-Pro", status="0", bundle=None):
        out, error = StringIO(), StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(error):
            result = cli.main(["results", "keep", "--log", str(self.log), "--bundle", str(bundle or self.bundle),
                               "--name", name, "--status", status, *extra])
        return result, out.getvalue() + error.getvalue()

    def record(self, name="iPhone-17-Pro"):
        return json.loads((self.kept / f"{name}.json").read_text())

    def test_a_passing_run_keeps_and_prints_each_test_with_every_outcome_distinct(self):
        self.fixture()
        os.environ[parallel.RESULTS_VARIABLE] = str(self.results)
        status, output = self.keep()
        self.assertEqual(status, 0, output)
        self.assertEqual(sorted(path.name for path in self.kept.iterdir()), ["iPhone-17-Pro.json", "iPhone-17-Pro.log"])
        record = self.record()
        self.assertEqual([(test["test"], test["outcome"], test["seconds"]) for test in record["tests"]], [
            ("Smoke/testOpens()", "passed", 1.25), ("Smoke/testBroken()", "failed", 0.5),
            ("Smoke/testOptional()", "skipped", 0.0), ("Smoke/testKnown()", "expected failure", 0.1),
            ("Smoke/testBusy()", "unmeasured", 0.0), ("Smoke/testLost()", "passed", None),
            ("Smoke/testOdd()", "unmeasured", 0.2)])
        self.assertEqual(record["tests"][4]["reason"], "the host was busy")
        self.assertIn("'Mystery'", record["tests"][6]["reason"])
        self.assertEqual((record["run"], record["status"], record["bundleUnread"]), ("iPhone-17-Pro", 0, None))
        self.assertEqual(record["summary"]["totalTestCount"], len(CASES))
        # Only the log's lines that give an outcome.
        self.assertEqual((self.kept / "iPhone-17-Pro.log").read_text().splitlines(),
                         ["Test Case '-[UI.Smoke testOpens]' passed (1.250 seconds).",
                          "\t Executed 5 tests, with 2 tests skipped and 0 failures (0 unexpected) in 2.0 (2.0) seconds"])
        for line in ("iPhone-17-Pro: passed 1.250s Smoke/testOpens()", "iPhone-17-Pro: failed 0.500s Smoke/testBroken()",
                     "iPhone-17-Pro: skipped 0.000s Smoke/testOptional()", "iPhone-17-Pro: expected failure 0.100s Smoke/testKnown()",
                     "iPhone-17-Pro: unmeasured 0.000s Smoke/testBusy() (the host was busy)",
                     "iPhone-17-Pro: passed no time Smoke/testLost()"):
            self.assertIn(line + "\n", output)
        self.assertIn("iPhone-17-Pro: 2 passed, 1 failed, 1 skipped, 1 expected failure, 2 unmeasured (xcodebuild status 0)\n", output)
        self.assertIn(f"iPhone-17-Pro: kept in {self.kept}\n", output)
        # The bundle stays where it is: only the small files are kept.
        self.assertFalse(list(self.results.rglob("*.xcresult")))
        self.assertEqual(len(list(self.results.rglob("*"))), 3)

    def test_a_failed_run_keeps_the_same_record_with_its_status(self):
        self.fixture([case("testBroken", "Failed", 0.5)], "Failed")
        status, output = self.keep("--results", str(self.results), status="65")
        self.assertEqual(status, 0, output)
        self.assertEqual((self.record()["status"], self.record()["tests"][0]["outcome"]), (65, "failed"))

    def test_keeps_selected_timed_activities_under_their_test(self):
        identifier_url = "test://com.apple.xcode/Example/UI/Smoke/testOpens"
        opens = {**case("testOpens", "Passed", 10), "nodeIdentifierURL": identifier_url}
        self.fixture([opens, case("testBroken", "Failed", 5), case("testLost", "Passed", 3)])
        (self.bundle / "activities.json").write_text(json.dumps({
            identifier_url: {"testRuns": [
                {"activities": [{"title": "Setup", "childActivities": [
                    {"title": "Files Recents still loading after 30 s"},
                    {"title": "Files Recents loaded after 5 s"},
                    {"title": "Files Recents loaded after retry"},
                ]}]},
                {"activities": [{"title": "Files Recents loaded after 0 s"}]},
            ]},
            "Smoke/testBroken()": {"testRuns": [{"activities": [{"title": "Setup"}]}]},
        }))
        status, output = self.keep("--results", str(self.results), "--activity-timing-prefix",
                                   "Files Recents loaded after", status="65")
        self.assertEqual(status, 0, output)
        self.assertEqual(self.record()["status"], 65)
        opened, broken, lost = self.record()["tests"]
        self.assertEqual(opened["seconds"], 10)
        self.assertEqual(opened["activities"], [
            {"title": "Files Recents loaded after 5 s", "seconds": 5},
            {"title": "Files Recents loaded after 0 s", "seconds": 0},
        ])
        self.assertEqual(broken["activities"], [])
        self.assertIsNone(lost["activities"])
        self.assertIn("Smoke/testOpens(): Files Recents loaded after 5 s", output)
        self.assertIn("Smoke/testLost(): activity timings could not be read", output)
        self.assertNotIn("still loading", output)
        self.assertNotIn("after retry", output)

    def test_a_run_with_no_readable_results_says_so_and_is_not_success(self):
        unreadable = self.root / "unreadable.xcresult"
        unreadable.mkdir()
        empty = self.root / "empty.xcresult"
        empty.mkdir()
        (empty / "summary.json").write_text(json.dumps({"result": "Passed", "totalTestCount": 0}))
        (empty / "tests.json").write_text(json.dumps({"testNodes": []}))
        for bundle, reason, tests in ((self.root / "absent.xcresult", "no result bundle at", None),
                                      (unreadable, "could not read the result bundle's test results", None),
                                      (empty, "records no test case", [])):
            with self.subTest(bundle.name):
                status, output = self.keep("--results", str(self.results), name=bundle.stem, bundle=bundle)
                self.assertEqual(status, lane_results.UNMEASURED, output)
                self.assertRegex(output, rf"UNMEASURED {bundle.stem}: no test's outcome is known, whatever xcodebuild's status 0 says: .*{reason}")
                self.assertNotIn(" passed,", output)
                record = self.record(bundle.stem)
                self.assertEqual(record["tests"], tests)
                self.assertIn(reason, record["bundleUnread"])
                self.assertEqual(record["status"], 0)

    def test_outside_a_lane_it_prints_and_says_nothing_was_kept(self):
        self.fixture()
        status, output = self.keep()
        self.assertEqual(status, 0, output)
        self.assertIn("iPhone-17-Pro: passed 1.250s Smoke/testOpens()\n", output)
        self.assertIn("not kept: no lane gave it a results directory", output)
        self.assertFalse(self.results.exists())

    def test_nothing_written_is_a_failure(self):
        self.fixture()
        self.results.write_text("a file where the directory goes")
        status, output = self.keep("--results", str(self.results))
        self.assertEqual(status, 1, output)
        self.assertIn("were not kept", output)

    def test_a_record_already_kept_is_left_alone(self):
        # The negative control: a run `qk simulator-tests` kept writes this name itself, in this format.
        self.fixture()
        record = matrix.bundle_record(self.bundle)
        self.assertEqual(matrix.kept("iPhone-17-Pro-1", self.log, record, 0, 12, self.results), self.kept)
        before = {path.name: path.read_bytes() for path in self.kept.iterdir()}
        status, output = self.keep("--results", str(self.results), name="iPhone-17-Pro-1", status="65")
        self.assertEqual(status, 1, output)
        self.assertIn("iPhone-17-Pro-1 is already kept", output)
        self.assertEqual({path.name: path.read_bytes() for path in self.kept.iterdir()}, before)
        # Another name is not refused, and its record has the same fields: one format, not two.
        status, output = self.keep("--results", str(self.results), name="iPad")
        self.assertEqual(status, 0, output)
        self.assertEqual(set(self.record("iPad")), set(self.record("iPhone-17-Pro-1")))

    def test_a_name_that_is_not_plain_is_refused(self):
        self.fixture()
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(StringIO()):
            self.keep("--results", str(self.results), name="../elsewhere")
        self.assertFalse(self.results.exists())

    def lane(self, *command):
        project = self.root / "project"
        project.mkdir()
        (project / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "P", "lanes": {"smoke": {"command": list(command)}}}))
        finished = subprocess.run([sys.executable, str(ENG), "--project", str(project), "run", "smoke"], cwd=project,
                                  capture_output=True, text=True, env=os.environ)
        return finished, project / ".build/queuekeeper/smoke/results"

    def test_a_lane_that_calls_it_shows_each_test_in_eng_runs_output_and_keeps_them_in_its_results(self):
        self.fixture([case("testOpens", "Passed", 1.25)])
        finished, results = self.lane(sys.executable, str(ENG), "results", "keep", "--log", str(self.log), "--bundle", str(self.bundle),
                                      "--name", "iPhone-17-Pro", "--status", "0")
        self.assertEqual(finished.returncode, 0, finished.stdout + finished.stderr)
        self.assertIn("iPhone-17-Pro: passed 1.250s Smoke/testOpens()", finished.stdout + finished.stderr)
        record = json.loads((results / matrix.KEPT / "iPhone-17-Pro.json").read_text())
        self.assertEqual(record["tests"], [{"test": "Smoke/testOpens()", "outcome": "passed", "seconds": 1.25}])

    def test_a_lane_that_does_not_call_it_keeps_nothing(self):
        finished, results = self.lane(sys.executable, "-c", "print('Executed 1 test, with 0 failures')")
        self.assertEqual(finished.returncode, 0, finished.stdout + finished.stderr)
        self.assertFalse((results / matrix.KEPT).exists())
        self.assertNotIn("per-test results", finished.stdout + finished.stderr)

    def test_eng_run_does_not_print_a_lanes_own_simulator_tests_records(self):
        # The negative control: a lane through qk simulator-tests keeps its lines and JSON, and its gate's output is unchanged.
        self.fixture([case("testOpens", "Passed", 1.25)])
        matrix.kept("iPhone-17-Pro-1", self.log, matrix.bundle_record(self.bundle), 0, 12, self.results)
        out = StringIO()
        with contextlib.redirect_stdout(out): run_results.report(self.results)
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(self.record("iPhone-17-Pro-1")["keptBy"], "qk simulator-tests")

    def test_report_reads_a_groups_lanes_says_when_results_are_unread_and_survives_a_damaged_record(self):
        lanes = {"left": {"run": "iPhone", "keptBy": run_results.KEPT_BY, "status": 0, "bundleUnread": None,
                          "tests": [{"test": "T/a()", "outcome": "passed", "seconds": 0.5}]},
                 "right": {"run": "iPad", "keptBy": run_results.KEPT_BY, "status": 0, "tests": None, "bundleUnread": "no result bundle at x"}}
        for lane, record in lanes.items():
            (self.results / lane / matrix.KEPT).mkdir(parents=True)
            (self.results / lane / matrix.KEPT / f"{record['run']}.json").write_text(json.dumps(record))
        (self.results / "left" / matrix.KEPT / "damaged.json").write_text("{")
        out = StringIO()
        with contextlib.redirect_stdout(out): run_results.report(self.results)
        self.assertIn("iPhone: passed 0.500s T/a()\n", out.getvalue())
        self.assertIn("iPhone: 1 passed, 0 failed, 0 skipped, 0 expected failure, 0 unmeasured (xcodebuild status 0)\n", out.getvalue())
        self.assertIn("UNMEASURED iPad: no test's outcome is known, whatever xcodebuild's status 0 says: no result bundle at x\n", out.getvalue())
        self.assertRegex(out.getvalue(), r"damaged\.json could not be read")


if __name__ == "__main__":
    unittest.main()
