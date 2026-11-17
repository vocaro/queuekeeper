"""An entry's machine-readable result survives the terminal and the recent status window."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src/python"))
import host_isolation  # noqa: F401 -- first, as every test module
from queuekeeper import agent_results, merge_queue
from queuekeeper import agent_logs
from test_support.queue import Fixture


class AgentResults(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.entry = {"id": "old-entry", "tip": "a" * 40, "state": "landed",
                      "subject": "A change", "landedAs": "b" * 40}
        self.queue = mock.Mock(root=self.root, hold_file=self.root / "hold")
        self.queue.read.return_value = self.entry
        self.queue.held.return_value = None
        self.queue.working.return_value = False

    def test_direct_lookup_returns_old_landing_without_listing_recent_entries(self):
        self.queue.all.side_effect = AssertionError("do not list recent entries")
        found = agent_results.lookup(self.queue, "old-entry")
        self.assertEqual(found["landedAs"], "b" * 40)
        self.assertEqual(found["commit"], "a" * 40)
        self.assertEqual(found["project"], str(self.root))
        self.assertTrue(found["terminal"])
        self.queue.read.assert_called_once_with("old-entry")
        self.queue.held.assert_not_called()

    def test_failed_dispatch_retains_host_paths_and_test_evidence(self):
        dispatch = self.root / "dispatch"
        dispatch.mkdir()
        (dispatch / "source.json").write_text(json.dumps({"host": "build-host"}))
        (dispatch / "run.log").write_text(
            'FAIL: test_a (test_example.Example.test_a)\n'
            'Traceback (most recent call last):\n'
            '  File "/project/tests/test_example.py", line 9, in test_a\n'
            '    self.fail()\nAssertionError\n\n'
            'Ran 1 test in 0.001s\n\nFAILED (failures=1)\n')
        self.entry.update(state="failed", why="The gate failed", log=str(self.root / "pruned.log"),
                          failedDispatch={"directory": str(dispatch), "excerpt": ["AssertionError"]})
        found = agent_results.entry_result(self.queue, self.entry)
        self.assertEqual(found["tests"]["failed"], 1)
        self.assertEqual(found["failure"]["tests"][0]["location"], "/project/tests/test_example.py:9")
        run = next(item for item in found["artifacts"] if item["kind"] == "run-log")
        self.assertEqual(run["producedOn"], "build-host")
        self.assertEqual(run["location"], "local")
        self.assertTrue(run["available"])
        missing = next(item for item in found["artifacts"] if item["kind"] == "queue-log")
        self.assertFalse(missing["available"])

    def test_old_landing_retains_flakes_without_inventing_missing_gate_evidence(self):
        flake = {"test": "test_example.Example.test_a", "commit": "c" * 40,
                 "gateLog": "/pruned/gate.log", "rerunLogs": ["/pruned/rerun-1.log", "/pruned/rerun-2.log"],
                 "mac": "build-host", "issue": "https://github.com/vocaro/queuekeeper/issues/142",
                 "outcome": "The batch landed."}
        self.entry["flakes"] = [flake]
        output = io.StringIO()
        with mock.patch.object(merge_queue, "Queue", return_value=self.queue), contextlib.redirect_stdout(output):
            self.assertEqual(agent_results.result(self.root, "old-entry", True), 0)
        found = json.loads(output.getvalue())
        self.assertEqual(found["flakes"], [{**flake, "reruns": {"status": "passed", "count": 2}}])
        self.assertEqual(self.entry["flakes"], [flake])
        self.assertEqual(found["state"], "landed")
        self.assertIsNone(found["failure"])
        self.assertIsNone(found["tests"])
        self.assertIsNone(found["resultsSummary"])
        self.assertEqual(found["artifacts"], [])

    def test_no_recorded_flakes_are_an_empty_list(self):
        self.assertEqual(agent_results.lookup(self.queue, "old-entry")["flakes"], [])

    def test_pending_entry_exposes_only_its_current_gate_and_hold(self):
        self.entry.update(state="testing")
        self.queue.working.return_value = True
        self.queue.held.return_value = "for the owner: SSH refused"
        self.queue.round_state.return_value = {"gates": {
            "own": {"entries": ["old-entry"], "lane": "gate", "mac": "build-host",
                    "log": str(self.root / "gate.log")},
            "other": {"entries": ["someone-else"], "lane": "unrelated", "log": "/unrelated"}}}
        found = agent_results.lookup(self.queue, "old-entry")
        self.assertFalse(found["terminal"])
        self.assertEqual(found["recovery"]["responsibleParty"], "owner")
        self.assertEqual([gate["lane"] for gate in found["gates"]], ["gate"])
        self.assertEqual(len(found["artifacts"]), 1)

    def test_log_reads_are_bounded_and_report_truncation(self):
        log = self.root / "large.log"
        self.enterContext(mock.patch.object(agent_results, "LOG_BYTES", 4096))
        log.write_bytes(b"x" * (agent_results.LOG_BYTES + 1))
        self.entry.update(state="failed", log=str(log))
        found = agent_results.lookup(self.queue, "old-entry")
        self.assertTrue(found["logTruncated"])
        self.assertEqual(len(agent_results.diagnostic_excerpt(log)[0]), agent_results.LOG_BYTES)

    def test_tests_counted_before_the_last_mebibyte_of_a_log_are_in_the_count(self):
        # vocaro/queuekeeper#163: a 4.4 MB gate log's record counted only the tests in its last 1 MiB.
        log = self.root / "gate.log"
        log.write_text("Ran 1010 tests in 1s\n\nOK (skipped=3)\n" + ("y" * 79 + "\n") * (1 << 14)
                       + "Ran 4723 tests in 1s\n\nOK\n")
        self.assertGreater(log.stat().st_size, 1 << 20)
        self.entry["log"] = str(log)
        found = agent_results.lookup(self.queue, "old-entry")
        self.assertEqual(found["tests"], {"passed": 5730, "failed": 0, "skipped": 3, "unmeasured": 0, "ran": 5730})
        self.assertFalse(found["logTruncated"])

    def test_a_landed_group_gate_is_counted_by_its_summary_not_by_its_qk_unittest_lane(self):
        # vocaro/queuekeeper#177: AXVerify's gate, a group of three lanes, passed with 212 tests, and its log printed
        # the group's summary before its qk unittest lane's own output of 68 tests. The record counted 67 and 1 skip.
        log = self.root / "20261116-060746-9f16caf9-gate.log"
        log.write_text("PASS gate-python (1s, status 0): /w/gate-python.log\n"
                       "PASS gate-swift-mac (66s, status 0): /w/gate-swift-mac.log\n"
                       "PASS gate-swift-ios (321s, status 0): /w/gate-swift-ios.log\n"
                       "qk run gate: passed, on this Mac\n"
                       "  tests: 212 passed, 0 failed, 1 skipped, 0 unmeasured\n"
                       "  lanes: 3 passed, 0 failed, 0 unmeasured\n\n"
                       "Ran 39 tests in 0.1s\n\nOK\nRan 29 tests in 0.1s\n\nOK (skipped=1)\n"
                       "queuekeeper unittest: 68 tests (1 skipped) in 5 processes, 4 at a time, 0.8s: OK\n")
        self.entry["log"] = str(log)
        found = agent_results.lookup(self.queue, "old-entry")
        self.assertEqual(found["tests"], {"passed": 212, "failed": 0, "skipped": 1, "unmeasured": 0, "ran": 212})

    def test_a_log_read_only_at_its_end_gives_no_count_and_says_its_failing_tests_are_partial(self):
        early = "FAIL: test_early (test_example.Example.test_early)\n\nRan 7 tests in 1s\n\nFAILED (failures=1)\n"
        late = "FAIL: test_late (test_example.Example.test_late)\n\nRan 2 tests in 1s\n\nFAILED (failures=1)\n"
        log = self.root / "gate.log"
        log.write_text(early + "x" * 4096 + "\n" + late)
        self.entry.update(state="failed", log=str(log))
        whole = agent_results.lookup(self.queue, "old-entry")
        self.assertEqual(whole["tests"]["ran"], 9)
        self.assertFalse(whole["failure"]["testsTruncated"])
        self.assertEqual(len(whole["failure"]["tests"]), 2)
        self.enterContext(mock.patch.object(agent_results, "LOG_BYTES", 4096))
        found = agent_results.lookup(self.queue, "old-entry")
        self.assertTrue(found["logTruncated"])
        self.assertIsNone(found["tests"])
        self.assertTrue(found["failure"]["testsTruncated"])
        self.assertEqual([test["name"] for test in found["failure"]["tests"]], ["test_example.Example.test_late"])

    def test_final_failure_diagnostics_exclude_previous_failed_rounds(self):
        old = self.root / "old.log"
        final = self.root / "final.log"
        old.write_text("FAIL: test_old (test_previous.Example.test_old)\n\nRan 1 test in 1s\n\nFAILED (failures=1)\n")
        final.write_text("FAIL: test_final (test_current.Example.test_final)\n\nRan 2 tests in 1s\n\nFAILED (failures=1)\n")
        self.entry.update(state="failed", log=str(final), logs=[
            {"lane": "gate", "path": str(old), "status": 1},
            {"lane": "gate", "path": str(final), "status": 1}])
        found = agent_results.lookup(self.queue, "old-entry")
        self.assertEqual(found["tests"]["ran"], 2)
        self.assertEqual([test["name"] for test in found["failure"]["tests"]], ["test_current.Example.test_final"])
        self.assertEqual(len(found["artifacts"]), 2)
        selected = agent_logs.selected(found["artifacts"])
        self.assertEqual(selected["path"], str(final))
        self.assertTrue(selected["primary"])

    def test_custom_success_uses_latest_completed_log_without_historical_failures(self):
        old = self.root / "old.log"
        final = self.root / "final.log"
        old.write_text("FAIL: test_old (test_previous.Example.test_old)\n\nRan 1 test in 1s\n\nFAILED (failures=1)\n")
        final.write_text("Ran 3 tests in 1s\n\nOK\n")
        self.entry["logs"] = [{"lane": "checks", "path": str(old), "status": 1},
                              {"lane": "checks", "path": str(final), "status": 0}]
        found = agent_results.lookup(self.queue, "old-entry")
        self.assertEqual(found["tests"]["ran"], 3)
        self.assertEqual(found["tests"]["failed"], 0)
        self.assertIsNone(found["failure"])
        self.assertEqual(len(found["artifacts"]), 2)
        self.assertEqual(agent_logs.selected(found["artifacts"])["path"], str(final))

    def test_unknown_and_path_ids_are_structured_errors(self):
        self.queue.read.return_value = None
        for identifier in ("missing", "../outside", "/absolute"):
            with self.subTest(identifier=identifier), mock.patch.object(merge_queue, "Queue", return_value=self.queue):
                output = io.StringIO()
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
                    status = agent_results.result(self.root, identifier, True)
                self.assertEqual(status, 2)
                self.assertEqual(json.loads(output.getvalue())["exitStatus"], 2)

    def test_result_query_returns_json_for_failure_with_successful_lookup(self):
        self.entry.update(state="conflict", why="Conflicts in src/a.py")
        output = io.StringIO()
        with mock.patch.object(merge_queue, "Queue", return_value=self.queue), contextlib.redirect_stdout(output):
            status = agent_results.result(self.root, "old-entry", True)
        found = json.loads(output.getvalue())
        self.assertEqual(status, 0)
        self.assertEqual(found["failure"]["category"], "conflict")
        self.assertEqual(found["exitStatus"], 0)


class DurableGateLogs(Fixture):
    def test_successful_custom_gate_keeps_the_pending_cursor_log_after_landing(self):
        config = self.config()
        config["lanes"]["checks"] = config["lanes"].pop("gate")
        config["queue"]["lanes"] = ["checks"]
        self.publish(config)
        self.commit("a", "a")
        [identifier] = self.add("a")
        queue = merge_queue.Queue(self.repo)
        run = merge_queue.processes.run
        pending = []

        def observed(command, **kwargs):
            status = run(command, **kwargs)
            if "run" in command and command[-1] == "checks":
                entry = queue.read(identifier)
                self.assertEqual(entry["state"], "testing")
                artifacts, _ = agent_results.entry_artifacts(queue, entry)
                artifact = next(item for item in artifacts if item["path"].endswith("-checks.log"))
                chunk = agent_logs.chunk(artifact, limit=4)
                pending.append((artifact, chunk))
            return status

        with mock.patch.object(merge_queue.processes, "run", observed), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.work(), 0)
        self.assertEqual(len(pending), 1)
        artifact, first = pending[0]
        settled = queue.read(identifier)
        self.assertEqual(settled["state"], "landed")
        self.assertNotIn("gate", settled)  # A custom successful gate has no standard gate measurement.
        artifacts, _ = agent_results.entry_artifacts(queue, settled)
        resumed = agent_logs.selected(artifacts, cursor=first["nextCursor"])
        self.assertEqual(resumed["path"], artifact["path"])
        rest = agent_logs.chunk(resumed, cursor=first["nextCursor"], complete=True)
        self.assertEqual(first["content"] + rest["content"], Path(artifact["path"]).read_text())
        reference = next(item for item in settled["logs"] if item["lane"] == "checks")
        self.assertEqual(reference["path"], artifact["path"])

    def test_documentation_landing_records_its_successful_lane_logs(self):
        self.commit("docs/change.md", "docs", "# A document\n")
        [identifier] = self.add("docs")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.work(), 0)
        entry = self.state(identifier)
        self.assertEqual(entry["state"], "landed")
        self.assertTrue(entry["docsOnly"])
        self.assertTrue(entry["logs"])
        queue = merge_queue.Queue(self.repo)
        artifacts, _ = agent_results.entry_artifacts(queue, entry)
        for reference in entry["logs"]:
            artifact = next(item for item in artifacts if item["path"] == reference["path"])
            self.assertTrue(artifact["available"])
            self.assertEqual(artifact["lane"], reference["lane"])


if __name__ == "__main__":
    unittest.main()
