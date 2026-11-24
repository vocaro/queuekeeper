"""Flaky Xcode tests (vocaro/queuekeeper#2): a failed qk simulator-tests lane's tests named from its kept records and
rerun alone with an exact series, the merge queue landing on the same rule as a qk unity test lane's, and the flake
history `qk results history` reports. No xcodebuild or simulator runs: the lane's command is a stand-in."""
import contextlib
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import host_isolation  # first, as every test module

from queuekeeper import config, events, lane_results, plugins
from queuekeeper import merge_queue as mq
from queuekeeper_apple import flakes, run_results
from queuekeeper_apple.simulators import matrix
from test_support.queue import Fixture, run

URL = "test://com.apple.xcode/App/AppTests/Smoke/{}"
LANE = {"command": ["python3", "scripts/qk", "simulator-tests", "--scheme", "App", "--runtime", "iOS-27-0",
                    "--device", "iPhone-17-Pro", "--device", "iPad-Air-13-inch-M4", "--"]}


def case(method, outcome, identity=True):
    found = {"test": f"Smoke/{method}", "outcome": outcome, "seconds": 1.0}
    if identity: found["identifierURL"] = URL.format(method)
    return found


class Naming(unittest.TestCase):
    def setUp(self):
        self.results = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        (self.results / matrix.KEPT).mkdir()

    def keep(self, name, cases, status=65, **fields):
        record = {"run": name, "keptBy": flakes.KEPT_BY, "status": status, "tests": cases, "bundleUnread": None,
                  "summary": {"failedTests": sum(found["outcome"] == "failed" for found in cases)}, **fields}
        (self.results / matrix.KEPT / f"{name}.json").write_text(json.dumps(record))

    def test_a_selector_comes_only_from_a_plain_method_of_an_xcode_identifier_url(self):
        self.assertEqual(flakes.selector(URL.format("testOpens()")), "AppTests/Smoke/testOpens()")
        self.assertEqual(flakes.selector(URL.format("testOpens")), "AppTests/Smoke/testOpens")
        for identity in (None, "", URL.format("testOpens(url:)"), "test://com.apple.xcode/App/Smoke/testOpens()",
                         "test://example.com/App/AppTests/Smoke/testOpens()", URL.format("testOpens()") + "?x=1",
                         "test://com.apple.xcode/App/AppTests/Outer/Inner/testOpens()"):
            with self.subTest(identity=identity): self.assertIsNone(flakes.selector(identity))

    def test_every_devices_failed_tests_are_named_once_each_in_order(self):
        self.keep("iPhone-17-Pro", [case("testA()", "failed"), case("testB()", "passed"), case("testC", "failed")])
        self.keep("iPad-Air-13-inch-M4", [case("testC", "failed"), case("testB()", "skipped")])
        self.assertEqual(flakes.failed(self.results, ["iPhone-17-Pro", "iPad-Air-13-inch-M4"]),
                         (["AppTests/Smoke/testA()", "AppTests/Smoke/testC"], None))

    def test_a_run_the_records_cannot_account_for_names_no_test(self):
        expected = ["iPhone-17-Pro"]
        refusals = {
            "kept no record": lambda: None,
            "not a record qk simulator-tests kept": lambda: self.keep("iPhone-17-Pro", [], keptBy="qk results keep"),
            "was not read": lambda: self.keep("iPhone-17-Pro", [], bundleUnread=matrix.UNREAD),
            "was unmeasured": lambda: self.keep("iPhone-17-Pro", [case("testA()", "failed")],
                                                status=lane_results.UNMEASURED),
            "with no failed test case": lambda: self.keep("iPhone-17-Pro", [case("testA()", "passed")]),
            "left Smoke/testB() unmeasured": lambda: self.keep("iPhone-17-Pro", [case("testA()", "failed"),
                                                                                case("testB()", "unmeasured")]),
            "counts 2 failed tests but names 1": lambda: self.keep("iPhone-17-Pro", [case("testA()", "failed")],
                                                                   summary={"failedTests": 2}),
            "cannot be rerun alone": lambda: self.keep("iPhone-17-Pro", [case("testA()", "failed", identity=False)]),
            "cannot be read": lambda: (self.results / matrix.KEPT / "iPhone-17-Pro.json").write_text("{"),
        }
        for why, kept in refusals.items():
            with self.subTest(why=why):
                shutil.rmtree(self.results / matrix.KEPT)
                (self.results / matrix.KEPT).mkdir()
                kept()
                tests, said = flakes.failed(self.results, expected)
                self.assertIsNone(tests)
                self.assertIn(why, said)
        # One device's record is not every device's: the other's tests may never have run.
        shutil.rmtree(self.results / matrix.KEPT)
        (self.results / matrix.KEPT).mkdir()
        self.keep("iPhone-17-Pro", [case("testA()", "failed")])
        self.assertIn("iPad-Air-13-inch-M4 kept no record",
                      flakes.failed(self.results, ["iPhone-17-Pro", "iPad-Air-13-inch-M4"])[1])

    def test_a_qk_simulator_tests_lane_reruns_one_test_as_an_exact_series_on_each_device(self):
        found = flakes.reruns(LANE)
        self.assertIsInstance(found, plugins.Reruns)
        self.assertTrue(found.no_verdict)
        self.assertEqual(found.rerun, [*LANE["command"], "--repeat", "1", "--erase", "--exact", "--only", "{test}"])
        self.keep("iPhone-17-Pro", [case("testA()", "failed")])
        self.keep("iPad-Air-13-inch-M4", [case("testA()", "passed")], status=0)
        self.assertEqual(found.failed(self.results), (["AppTests/Smoke/testA()"], None))
        # Without a --, one is added, so that the series' options reach it; a sharded lane's records are its shards'.
        sharded = flakes.reruns({"command": ["qk", "simulator-tests", "--scheme", "App", "--runtime", "iOS-27-0",
                                             "--device", "iPhone-17-Pro", "--shards", "2"]})
        self.assertEqual(sharded.rerun[-7:], ["--", "--repeat", "1", "--erase", "--exact", "--only", "{test}"])
        self.assertIn("iPhone-17-Pro-1 kept no record", sharded.failed(self.results)[1])
        self.assertIsNotNone(flakes.reruns({"command": [*LANE["command"], "-skip-testing:AppTests/Slow"]}))

    def test_a_lane_whose_tests_cannot_run_one_alone_gives_none(self):
        for command in (["qk", "unittest", "-s", "tests"], ["./scripts/test.sh"], "qk simulator-tests",
                        ["qk", "simulator-tests", "--scheme", "App"],
                        [*LANE["command"], "-test-iterations", "3"],
                        [*LANE["command"], "--repeat", "2"],
                        ["qk", "simulator-tests", "--scheme", "App", "--runtime", "R", "--device", "D", "--workers", "2"],
                        ["qk", "simulator-tests", "--scheme", "App", "--runtime", "R", "--device", "D", "--build-only"]):
            with self.subTest(command=command): self.assertIsNone(flakes.reruns({"command": command}))

    def test_its_failed_tests_paths_are_accepted_alone_as_a_qk_unity_test_lanes_are(self):
        plugins.RERUNS.append(flakes.reruns)
        self.addCleanup(plugins.RERUNS.remove, flakes.reruns)
        lanes = {"gate": {**LANE, "failedTests": {"paths": ["App/", "AppTests/", "App.xcodeproj/"]}}}
        lane_name, settings = mq.declared_failures(lanes, "gate", [])
        self.assertEqual((lane_name, settings["paths"]), ("gate", ["App/", "AppTests/", "App.xcodeproj/"]))
        self.assertIn("--exact", settings["plugin"].rerun)
        config.failed_tests_setting("gate", lanes["gate"])


class History(unittest.TestCase):
    """`qk results history`'s flake history: rates from the kept attempts and the flakes the queue proved."""

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.store = self.root / "store"

    def kept(self, commit, attempt, name, outcome):
        folder = self.store / commit / ("ios.results" if attempt is None else f"ios.attempts/{attempt}/results")
        (folder / matrix.KEPT).mkdir(parents=True, exist_ok=True)
        path = folder / matrix.KEPT / f"{name}.json"
        record = json.loads(path.read_text()) if path.exists() else {"run": name, "keptBy": flakes.KEPT_BY, "tests": []}
        record["tests"].append(case("testOpens()", outcome))
        path.write_text(json.dumps(record))

    def history(self, test):
        lines = []
        with patch.object(run_results.remote, "store", return_value=self.store):
            self.assertEqual(run_results.history(self.root, "ios", test, lines.append), 0)
        return lines

    def test_rates_count_failures_of_measured_runs_and_commits_where_one_device_both_failed_and_passed(self):
        first, second, third = "a" * 40, "b" * 40, "c" * 40
        self.kept(first, 1, "iPhone", "failed")
        self.kept(first, None, "iPhone", "passed")
        # A device that fails where another passes is no flake by itself.
        self.kept(second, None, "iPhone", "passed")
        self.kept(second, None, "iPad", "failed")
        self.kept(third, None, "iPhone", "skipped")
        selector = "AppTests/Smoke/testOpens()"
        for test in (selector, URL.format("testOpens()"), "Smoke/testOpens()"):
            with self.subTest(test=test):
                lines = self.history(test)
                self.assertIn("ios: 2 passed, 2 failed, 1 skipped, 0 expected failure, 0 unmeasured", lines)
                self.assertIn("ios: failed 2 of 4 measured runs (50%); failed and passed in one result file at 1 of 3 "
                              "commits (33%)", lines)
                self.assertIn(f"{test}: 0 flakes the merge queue proved, failing a gate and passing alone on every "
                              "rerun, in any lane", lines)

    def test_no_observation_gives_no_rate(self):
        lines = self.history("AppTests/Smoke/testOpens()")
        self.assertIn("ios: no retained result names AppTests/Smoke/testOpens()", lines)
        self.assertIn("AppTests/Smoke/testOpens(): 0 flakes the merge queue proved, failing a gate and passing alone "
                      "on every rerun, in any lane", lines)

    def test_proven_flakes_come_from_main_s_trailers_and_the_queue_s_journal_each_once(self):
        project, origin = self.root / "project", self.root / "origin.git"
        run(self.root, "git", "init", "-q", "--bare", str(origin))
        run(self.root, "git", "init", "-q", "-b", "main", str(project))
        run(project, "git", "remote", "add", "origin", str(origin))
        selector = "AppTests/Smoke/testOpens()"
        landed = "1" * 40
        words = mq.flake_words({"test": selector, "commit": landed, "gateLog": "/logs/gate.log", "rerunLogs": ["/r1"]})
        run(project, "git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-q",
            "--allow-empty", "-m", "Land a batch", "--trailer", f"{mq.FLAKY}: {words}",
            "--trailer", f"{mq.FLAKY}: tests.test_other.Other.test_x failed the gate at {'2' * 12} and passed alone")
        run(project, "git", "push", "-q", "origin", "HEAD:main")
        run(project, "git", "fetch", "-q", "origin")
        journal = project / ".git" / "queuekeeper-queue"
        journal.mkdir()
        # The same flake as the trailer's, and one whose batch was bisected, which left no trailer.
        events.emit(journal, "test.flaky", test=selector, commit=landed)
        events.emit(journal, "test.flaky", test=selector, commit="3" * 40)
        self.assertEqual([found for found in flakes.proven(project) if found[0] == selector],
                         [(selector, "1" * 12), (selector, "3" * 12)])
        self.kept("a" * 40, None, "iPhone", "failed")
        lines = []
        with patch.object(run_results.remote, "store", return_value=self.store):
            run_results.history(project, "ios", "Smoke/testOpens()", lines.append)
        self.assertIn(f"Smoke/testOpens(): 2 flakes the merge queue proved, failing a gate and passing alone on every "
                      f"rerun, in any lane: at {'1' * 12}, {'3' * 12}", lines)


# A stand-in for `qk simulator-tests` in a project's lane: two tests, one that fails when the plan says so at its turn,
# kept as qk simulator-tests keeps them; with --only, an exact series of that test alone, whose plan letter F fails it
# and U leaves it unmeasured.
SIMULATOR_LANE = """import json, os, pathlib, sys

RUNS, PLAN = pathlib.Path({runs!r}), pathlib.Path({plan!r})
assert sys.argv[1] == "simulator-tests", sys.argv


def step():
    done = len(RUNS.read_text()) if RUNS.exists() else 0
    RUNS.write_text("x" * (done + 1))
    return (PLAN.read_text() if PLAN.exists() else "")[done:done + 1]


if "--only" in sys.argv:
    assert sys.argv[sys.argv.index("--only") + 1] == "AppTests/Smoke/testSometimes()", sys.argv
    assert {{"--repeat", "--erase", "--exact"}} <= set(sys.argv), sys.argv
    said = step()
    print("exact rerun:", said or "P")
    sys.exit({{"F": 1, "U": 3}}.get(said, 0))
failed = step() == "F"
url = "test://com.apple.xcode/App/AppTests/Smoke/{{}}"
cases = [{{"test": "Smoke/testSometimes()", "identifierURL": url.format("testSometimes()"),
          "outcome": "failed" if failed else "passed", "seconds": 1}},
         {{"test": "Smoke/testSteady()", "identifierURL": url.format("testSteady()"), "outcome": "passed", "seconds": 1}}]
folder = pathlib.Path(os.environ["QUEUEKEEPER_RESULTS_DIR"]) / "simulator-tests"
folder.mkdir(parents=True, exist_ok=True)
(folder / "iPhone-17-Pro.json").write_text(json.dumps({{
    "run": "iPhone-17-Pro", "keptBy": "qk simulator-tests", "status": 65 if failed else 0, "tests": cases,
    "summary": {{"failedTests": int(failed)}}, "bundleUnread": None}}))
sys.exit(1 if failed else 0)
"""


class SimulatorFlakyReruns(Fixture):
    """A failed qk simulator-tests lane's failed test is rerun alone before a bisect, by the selector the Apple plugin
    reads from its kept record and with an exact series, and one that passes alone is a flake, as a qk unittest lane's
    is (vocaro/queuekeeper#2)."""
    TEST = "AppTests/Smoke/testSometimes()"

    def setUp(self):
        super().setUp()
        self.runs, self.plan = self.base / "runs", self.base / "plan"
        (self.repo / "tools").mkdir()
        (self.repo / "tools/qk").write_text(SIMULATOR_LANE.format(runs=str(self.runs), plan=str(self.plan)))
        settings = self.config()
        settings["plugins"] = ["queuekeeper_apple"]
        settings["lanes"]["gate"] = {"command": ["python3", "tools/qk", "simulator-tests", "--scheme", "App",
                                                 "--runtime", "iOS-27-0", "--device", "iPhone-17-Pro", "--"],
                                     "timeout": 600, "failedTests": {"paths": ["tools/"]}}
        (self.repo / "queuekeeper.json").write_text(json.dumps(settings))
        run(self.repo, "git", "add", ".")
        run(self.repo, "git", "commit", "-qm", "Gate on qk simulator-tests")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")

    def worked(self):
        with contextlib.redirect_stdout(io.StringIO()) as said: self.assertEqual(self.work(), 0)
        return said.getvalue()

    def test_a_failed_test_that_passes_alone_is_a_flake_and_the_batch_lands(self):
        self.plan.write_text("F")
        self.commit("a", "a"); self.commit("b", "b")
        ids = self.add("a", "b")
        log = self.worked()
        self.assertIn(f": {self.TEST}; rerunning each alone up to {mq.RERUNS} times on that commit before a bisect", log)
        self.assertIn("no commit of the batch changes a path gate's tests depend on: landing the batch", log)
        self.assertEqual(len(self.runs.read_text()), 1 + mq.RERUNS)
        for identifier in ids:
            entry = self.state(identifier)
            self.assertEqual(entry["state"], "landed")
            [flake] = entry["flakes"]
            self.assertEqual(flake["test"], self.TEST)
            self.assertTrue(all("exact rerun: P" in Path(path).read_text() for path in flake["rerunLogs"]))
        [tracked] = self.tracked
        self.assertEqual(tracked["test"], self.TEST)
        run(self.repo, "git", "fetch", "-q", "origin")
        trailers = run(self.repo, "git", "log", "-1", "--format=%(trailers:key=Flaky-test,valueonly)", "origin/main")
        self.assertTrue(trailers.startswith(f"{self.TEST} failed the gate at "), trailers)

    def test_a_failed_test_that_fails_again_alone_is_no_flake(self):
        self.plan.write_text("FF")
        self.commit("a", "a")
        [identifier] = self.add("a")
        log = self.worked()
        self.assertIn(f"{self.TEST} failed again alone, on rerun 1 (exit 1), so the failure is real", log)
        entry = self.state(identifier)
        self.assertNotEqual(entry["state"], "landed")
        self.assertNotIn("flakes", entry)
        self.assertEqual(self.tracked, [])

    def test_an_unmeasured_rerun_gives_no_verdict_and_proves_nothing(self):
        self.plan.write_text("FU")
        self.commit("a", "a")
        self.add("a")
        log = self.worked()
        self.assertIn(f"{self.TEST} gave no verdict alone, on rerun 1 (exit {lane_results.UNMEASURED})", log)
        self.assertNotIn("so the failure is real", log)
        self.assertEqual(self.tracked, [])

    def test_a_batch_that_changes_a_path_its_tests_depend_on_records_the_flake_and_bisects(self):
        self.plan.write_text("F")
        self.commit("tools/helper.py", "a"); self.commit("b", "b")
        self.add("a", "b")
        log = self.worked()
        self.assertIn("the batch changes tools/helper.py, which gate's tests depend on: bisecting", log)
        [tracked] = self.tracked
        self.assertEqual(tracked["test"], self.TEST)


if __name__ == "__main__":
    unittest.main()
