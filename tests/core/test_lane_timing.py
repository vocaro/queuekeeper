import contextlib
from io import StringIO
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import host_isolation
from host_isolation import stretched
from queuekeeper import agent_results, budgets, cli, config, lane_timing, lane_wait, merge_queue, offload_host, processes


def history(tmp):
    """A history of this test's own, beside an admission log of its own."""
    return patch.dict(os.environ, {"HEAVY_SLOT_DIR": tmp, lane_timing.STALE_VARIABLE: ""})


class Clock:
    def __init__(self): self.now = 0.0
    def __call__(self): return self.now


class BaselineTests(unittest.TestCase):
    def test_the_baseline_is_the_p90_of_recent_passing_runs_since_the_last_stale_mark(self):
        with tempfile.TemporaryDirectory() as tmp, history(tmp):
            self.assertIsNone(lane_timing.baseline("P", "gate"))
            for index, seconds in enumerate([60, 62, 58, 61]): lane_timing.record("P", "gate", seconds, at=100 + index)
            self.assertIsNone(lane_timing.baseline("P", "gate"), "fewer than FEWEST runs make no baseline")
            lane_timing.record("P", "gate", 70, at=110)
            lane_timing.record("Other", "gate", 999, at=111)
            lane_timing.record("P", "lint", 999, at=112)
            found = lane_timing.baseline("P", "gate")
            self.assertEqual((found["p90"], found["median"], found["samples"]), (70, 61, 5))
            for index in range(lane_timing.RECENT): lane_timing.record("P", "gate", 10, at=200 + index)
            self.assertEqual(lane_timing.baseline("P", "gate")["p90"], 10, "only the newest RECENT runs count")
            lane_timing.mark_stale("P", ["gate"], "split the suite", at=300)
            self.assertIsNone(lane_timing.baseline("P", "gate"))
            for index in range(lane_timing.FEWEST): lane_timing.record("P", "gate", 30, at=301 + index)
            self.assertEqual(lane_timing.baseline("P", "gate")["p90"], 30)
            lane_timing.mark_stale("P", [lane_timing.EVERY_LANE], "new Mac", at=400)
            self.assertIsNone(lane_timing.baseline("P", "gate"))
            self.assertEqual(lane_timing.stale_marks("P"), {"gate": 300, "*": 400})

    def test_marks_a_dispatch_carried_count_as_this_macs_own(self):
        with tempfile.TemporaryDirectory() as tmp, history(tmp):
            for index in range(5): lane_timing.record("P", "gate", 30, at=100 + index)
            with patch.dict(os.environ, {lane_timing.STALE_VARIABLE: json.dumps({"gate": 200, "x": "bad"})}):
                self.assertEqual(lane_timing.carried(), {"gate": 200})
                self.assertIsNone(lane_timing.baseline("P", "gate"))
            self.assertIsNotNone(lane_timing.baseline("P", "gate"))

    def test_thresholds_are_relative_and_apply_only_after_min_samples(self):
        self.assertEqual(lane_timing.threshold("+25%"), ("percent", 25.0))
        self.assertEqual(lane_timing.threshold("+2m"), ("seconds", 120.0))
        self.assertEqual(lane_timing.threshold("+1.5h"), ("seconds", 5400.0))
        for bad in ("25%", "+0%", "+2d", "-5m", 5, None):
            with self.subTest(bad=bad), self.assertRaises(ValueError): lane_timing.threshold(bad)
        found = {"p90": 100, "median": 90, "samples": 10}
        self.assertEqual(lane_timing.fail_seconds({"fail": "+25%"}, found), 125)
        self.assertEqual(lane_timing.fail_seconds({"fail": "+2m"}, found), 220)
        self.assertIsNone(lane_timing.fail_seconds({"fail": "+2m", "minSamples": 11}, found))
        self.assertIsNone(lane_timing.fail_seconds({"fail": "+2m"}, {**found, "samples": 9}))
        self.assertIsNone(lane_timing.fail_seconds({}, found))
        self.assertIsNone(lane_timing.fail_seconds({"fail": "+2m"}, None))


class SettingTests(unittest.TestCase):
    def parsed(self, **lane):
        return config.parse(json.dumps({"schemaVersion": 1, "name": "test", "lanes": {
            "test": {"command": ["true"], **lane}}}))["lanes"]["test"]

    def test_timing_notice_and_a_stalls_notice_are_checked(self):
        self.parsed(timing={"warn": False, "fail": "+25%", "minSamples": 5}, notice="ask the owner first")
        self.parsed(stall={"seconds": 5, "notice": "a quiet build is a hang"})
        for lane in ({"timing": {"fail": "25%"}}, {"timing": {"warn": 1}}, {"timing": {"minSamples": 4}},
                     {"timing": {"other": 1}}, {"timing": "+25%"}, {"notice": " "}, {"notice": 5},
                     {"stall": {"seconds": 5, "notice": ""}}):
            with self.subTest(lane=lane), self.assertRaises(ValueError): self.parsed(**lane)

    def test_the_schema_lists_the_new_keys(self):
        schema = json.loads((Path(cli.TOOLKIT_ROOT) / "schemas/queuekeeper.schema.json").read_text())
        text = json.dumps(schema)
        for key in ('"timing"', '"notice"', '"minSamples"'): self.assertIn(key, text)

    def test_the_notice_is_the_stalls_then_the_lanes_then_the_default(self):
        self.assertEqual(lane_timing.notice({}), lane_timing.DEFAULT_NOTICE)
        self.assertIn("do not raise the limit just to make the gate pass", lane_timing.DEFAULT_NOTICE)
        lane = {"notice": "lane's", "stall": {"seconds": 1, "notice": "stall's"}}
        self.assertEqual(lane_timing.notice(lane), "lane's")
        self.assertEqual(lane_timing.notice(lane, stall=True), "stall's")
        self.assertEqual(lane_timing.notice({"notice": "lane's", "stall": {"seconds": 1}}, stall=True), "lane's")


class PaceTests(unittest.TestCase):
    def test_it_warns_once_past_the_p90_and_fails_past_the_threshold(self):
        with tempfile.TemporaryDirectory() as tmp, history(tmp):
            for index in range(10): lane_timing.record("P", "gate", 100, at=index)
            clock, live = Clock(), Path(tmp) / "gate.json.timing"
            pace = lane_timing.Pace("gate", {"timing": {"fail": "+50%"}}, "P", live=live, clock=clock)
            self.assertTrue(pace.active())
            clock.now = 99
            self.assertEqual(pace.observe(), (None, None))
            found = lane_timing.live(Path(tmp) / "gate.json")
            self.assertEqual((found["state"], found["medianSeconds"], found["p90Seconds"], found["samples"]),
                             ("baseline", 100, 100, 10))
            self.assertNotIn("words", found, "the baseline is not a warning, including to an older reader")
            clock.now = 101
            said, failed = pace.observe()
            self.assertTrue(said.startswith(lane_timing.PREFIX + "warning: gate has run 1m41s, past its p90 of 1m40s"))
            self.assertIsNone(failed)
            self.assertEqual(lane_timing.live(Path(tmp) / "gate.json")["state"], "slow")
            clock.now = 120
            self.assertEqual(pace.observe(), (None, None), "the warning is said once")
            clock.now = 151
            said, failed = pace.observe()
            self.assertIn("failed: ", said)
            self.assertIn("+50%", failed)
            self.assertEqual(lane_timing.live(Path(tmp) / "gate.json")["state"], "failed")
            self.assertEqual(pace.summary()["failSeconds"], 150)

    def test_a_first_look_past_the_threshold_says_the_warning_with_the_failure(self):
        with tempfile.TemporaryDirectory() as tmp, history(tmp):
            for index in range(10): lane_timing.record("P", "gate", 100, at=index)
            clock = Clock()
            pace = lane_timing.Pace("gate", {"timing": {"fail": "+50%"}}, "P", live=None, clock=clock)
            clock.now = 151
            said, failed = pace.observe()
            self.assertEqual([line.split(": ")[1] for line in said.splitlines()], ["warning", "failed"])
            self.assertEqual(lane_timing.found_in(said)[0], lane_timing.PREFIX + "warning: " + pace.warned)
            self.assertIn("+50%", failed)

    def test_without_a_baseline_or_with_warnings_off_there_is_nothing_to_watch(self):
        with tempfile.TemporaryDirectory() as tmp, history(tmp):
            self.assertFalse(lane_timing.Pace("gate", {}, "P", live=None).active())
            for index in range(10): lane_timing.record("P", "gate", 100, at=index)
            self.assertFalse(lane_timing.Pace("gate", {"timing": {"warn": False}}, "P", live=None).active())
            self.assertTrue(lane_timing.Pace("gate", {"timing": {"warn": False, "fail": "+1m"}}, "P", live=None).active())

    def test_supervise_stops_a_run_past_its_threshold_as_too_slow(self):
        with tempfile.TemporaryDirectory() as tmp, history(tmp):
            for index in range(10): lane_timing.record("P", "slow", 0.1, at=index)
            pace = lane_timing.Pace("slow", {"timing": {"fail": "+1s"}}, "P", live=None)
            log = Path(tmp) / "run.log"
            with contextlib.redirect_stderr(StringIO()) as error, self.assertRaises(processes.TooSlow):
                processes.run([sys.executable, "-c", "import time; time.sleep(60)"], cwd=tmp, log_path=log,
                              timeout=stretched(30), pace=pace)
            self.assertIn(lane_timing.PREFIX + "warning: slow has run", log.read_text())
            self.assertIn(lane_timing.PREFIX + "failed: slow ran past", log.read_text())
            self.assertIn(lane_timing.PREFIX + "warning:", error.getvalue())

    def test_a_sample_stalled_past_the_timeout_still_stops_the_run_as_too_slow(self):
        # vocaro/queuekeeper#283: under a load of about 100, a run past its 1.1 s threshold reached its 30 s timeout
        # instead. A ps that a loaded host stalls past both bounds, as here, once judged the run by its timeout first.
        # The 2 s timeout and the 3 s stall are the behavior under test, and stay fixed.
        real, calls = processes.snapshot, []
        def stalled():
            calls.append(None)
            if len(calls) == 2: time.sleep(3)
            return real()
        with tempfile.TemporaryDirectory() as tmp, history(tmp), patch.object(processes, "snapshot", stalled):
            for index in range(10): lane_timing.record("P", "slow", 0.1, at=index)
            pace = lane_timing.Pace("slow", {"timing": {"fail": "+1s"}}, "P", live=None)
            with contextlib.redirect_stderr(StringIO()), self.assertRaises(processes.TooSlow):
                processes.run([sys.executable, "-c", "import time; time.sleep(60)"], cwd=tmp,
                              log_path=Path(tmp) / "run.log", timeout=2, pace=pace)


class LaneRunTests(unittest.TestCase):
    def project(self, directory, lanes):
        (Path(directory) / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "timed",
                                                                       "lanes": lanes}))

    def run_lane(self, directory, lane):
        with patch.object(cli, "source_identity", return_value={"revision": "abc"}), \
                contextlib.redirect_stdout(StringIO()), contextlib.redirect_stderr(StringIO()) as error:
            status = cli.main(["--project", directory, "run", "--here", lane])
        return status, error.getvalue()

    def test_a_passing_run_joins_the_history_and_a_slow_one_fails_with_its_notice(self):
        with tempfile.TemporaryDirectory() as tmp, history(tmp), tempfile.TemporaryDirectory() as directory:
            self.project(directory, {
                "quick": {"command": [sys.executable, "-c", "print('Ran 1 test')"]},
                "slow": {"command": [sys.executable, "-c", "import time; time.sleep(60)"],
                         "timeout": round(stretched(60)), "timing": {"fail": "+1s", "minSamples": 5},
                         "notice": "split the lane instead"}})
            for _ in range(lane_timing.FEWEST):
                status, said = self.run_lane(directory, "quick")
                self.assertEqual(status, 0, said)
            found = lane_timing.baseline("timed", "quick")
            self.assertEqual(found["samples"], lane_timing.FEWEST)
            for index in range(lane_timing.FEWEST): lane_timing.record("timed", "slow", 0.1, at=index)
            status, said = self.run_lane(directory, "slow")
            self.assertEqual(status, 1, said)
            self.assertIn(lane_timing.PREFIX + "warning: slow has run", said)
            self.assertIn(lane_timing.NOTICE_PREFIX + "slow reached its timing fail threshold: split the lane instead",
                          said)
            receipt = json.loads((Path(directory) / ".build/queuekeeper/slow/source.json").read_text())
            self.assertEqual(receipt["outcome"], "failed")
            self.assertEqual(receipt["timing"]["samples"], lane_timing.FEWEST)
            log = (Path(directory) / ".build/queuekeeper/slow/run.log").read_text()
            self.assertEqual(lane_timing.found_in(log)[-1],
                             lane_timing.NOTICE_PREFIX + "slow reached its timing fail threshold: split the lane instead")
            self.assertEqual(lane_timing.baseline("timed", "slow")["samples"], lane_timing.FEWEST,
                             "a failed run joins no history")

    def test_a_timeout_shows_the_default_notice(self):
        with tempfile.TemporaryDirectory() as tmp, history(tmp), tempfile.TemporaryDirectory() as directory:
            self.project(directory, {"hang": {"command": [sys.executable, "-c", "import time; time.sleep(60)"],
                                              "timeout": 1}})
            status, said = self.run_lane(directory, "hang")
            self.assertNotEqual(status, 0, said)
            self.assertIn(lane_timing.NOTICE_PREFIX + "hang reached its timeout: do not raise the limit", said)

    def test_a_groups_members_are_each_timed_against_their_own_baselines(self):
        # vocaro/queuekeeper#284: a group's members ran with no warning, threshold or history.
        with tempfile.TemporaryDirectory() as tmp, history(tmp), tempfile.TemporaryDirectory() as directory:
            wait = Path(tmp) / "both.json"
            self.project(directory, {
                "quick": {"command": [sys.executable, "-c", "print('Ran 1 test')"]},
                "slow": {"command": [sys.executable, "-c", "import time; time.sleep(60)"],
                         "timeout": round(stretched(60)), "timing": {"fail": "+1s", "minSamples": 5},
                         "notice": "split the lane instead"},
                "both": {"parallel": ["quick", "slow"]}})
            for index in range(lane_timing.FEWEST): lane_timing.record("timed", "slow", 0.1, at=index)
            with patch.object(cli, "source_identity", return_value={"revision": "abc"}), \
                    patch.dict(os.environ, {"QUEUEKEEPER_LANE_WAIT": str(wait)}), \
                    contextlib.redirect_stdout(StringIO()) as out, contextlib.redirect_stderr(StringIO()) as error:
                status = cli.main(["--project", directory, "run", "--here", "both"])
            said = out.getvalue() + error.getvalue()
            self.assertEqual(status, 1, said)
            self.assertIn(lane_timing.PREFIX + "warning: slow has run", said)
            notice = lane_timing.NOTICE_PREFIX + "slow reached its timing fail threshold: split the lane instead"
            self.assertIn(notice, said, "a member's limit shows the member's own notice")
            lanes = Path(directory) / ".build/queuekeeper/both/lanes"
            self.assertEqual(lane_timing.found_in((lanes / "slow.log").read_text())[-1], notice)
            member = json.loads((lanes / "slow.json").read_text())
            self.assertEqual((member["status"], member["timing"]["samples"]), (1, lane_timing.FEWEST))
            runs = [entry for entry in lane_timing.read() if entry.get("event") == "run" and entry["lane"] == "quick"]
            self.assertEqual([(entry["project"], entry["commit"]) for entry in runs], [("timed", "abc")],
                             "a passing member joins its own history")
            self.assertEqual(lane_timing.baseline("timed", "slow")["samples"], lane_timing.FEWEST)
            live = lane_timing.live(wait)
            self.assertEqual((live["lane"], live["state"], set(live["lanes"])), ("both", "failed", {"slow"}))

    def test_a_groups_live_record_holds_each_members_own(self):
        with tempfile.TemporaryDirectory() as tmp, history(tmp):
            for name in ("a", "b"):
                for index in range(10): lane_timing.record("P", name, 100, at=index)
            live, clocks = Path(tmp) / "group.json.timing", {"a": Clock(), "b": Clock()}
            live.write_text(json.dumps({"words": "an earlier run's", "written": 0}))
            lane_timing.clear_live(live)
            paces = {name: lane_timing.Pace(name, {"timing": {"fail": "+50%"}}, "P", live=live, clock=clocks[name],
                                            group="group") for name in ("a", "b")}
            found = lane_timing.live(Path(tmp) / "group.json")
            self.assertEqual((found["state"], set(found["lanes"])), ("baseline", {"a", "b"}))
            self.assertNotIn("medianSeconds", found, "the group has no baseline of its own")
            self.assertNotIn("words", found)
            clocks["a"].now = 101
            paces["a"].observe()
            clocks["b"].now = 151
            paces["b"].observe()
            found = lane_timing.live(Path(tmp) / "group.json")
            self.assertEqual((found["lane"], found["state"]), ("group", "failed"))
            self.assertEqual(set(found["lanes"]), {"a", "b"})
            self.assertTrue(found["words"].startswith("b ran past"), "a failure's words come first")
            self.assertIn("a has run 1m41s", found["words"])


class StaleCommandTests(unittest.TestCase):
    def test_qk_timing_stale_marks_lanes_and_show_reports_them(self):
        with tempfile.TemporaryDirectory() as tmp, history(tmp), tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "timed", "lanes": {
                "gate": {"command": ["true"], "timing": {"fail": "+25%"}}, "lint": {"command": ["true"]}}}))
            for index in range(5): lane_timing.record("timed", "gate", 60, at=index)
            with contextlib.redirect_stdout(StringIO()) as out:
                self.assertEqual(cli.main(["--project", directory, "timing", "show", "--json"]), 0)
            shown = {row["lane"]: row for row in json.loads(out.getvalue())["lanes"]}
            self.assertEqual(shown["gate"]["baseline"]["p90"], 60)
            self.assertIsNone(shown["gate"]["failSeconds"], "the threshold waits for minSamples")
            with contextlib.redirect_stdout(StringIO()), contextlib.redirect_stderr(StringIO()), \
                    self.assertRaises(SystemExit):
                cli.main(["--project", directory, "timing", "stale", "gate"])
            with contextlib.redirect_stdout(StringIO()), contextlib.redirect_stderr(StringIO()), \
                    self.assertRaises(SystemExit):
                cli.main(["--project", directory, "timing", "stale", "nope", "--reason", "x"])
            with contextlib.redirect_stdout(StringIO()) as out:
                self.assertEqual(cli.main(["--project", directory, "timing", "stale", "gate", "--reason",
                                           "moved the UI tests out"]), 0)
            self.assertIn("marked gate of timed stale", out.getvalue())
            self.assertIsNone(lane_timing.baseline("timed", "gate"))
            with contextlib.redirect_stdout(StringIO()) as out:
                self.assertEqual(cli.main(["--project", directory, "timing", "show"]), 0)
            self.assertIn("gate: no baseline yet", out.getvalue())
            self.assertIn("marked stale", out.getvalue())

    def test_the_budget_leaves_out_gate_runs_settled_before_the_stale_mark(self):
        config_ = {"lanes": {"gate": {"command": ["true"], "budgetMinutes": 1}}}
        entries = [{"state": "landed", "settled": 100 + index, "landedAs": f"c{index}",
                    "gate": {"seconds": 600, "workSeconds": 600}} for index in range(5)]
        self.assertTrue(budgets.gate_warnings(config_, entries))
        self.assertEqual(budgets.gate_warnings(config_, entries, stale=200), [])
        self.assertTrue(budgets.gate_warnings(config_, entries, stale=101))


class ShownTests(unittest.TestCase):
    def test_status_and_wait_show_the_executing_macs_baseline_before_a_warning(self):
        with tempfile.TemporaryDirectory() as tmp, history(tmp):
            root = Path(tmp)
            there, here = root / "there.json", root / "here.json"
            lane_timing.Pace("gate", {}, "P", live=Path(str(there) + lane_timing.LIVE))
            self.assertIsNone(lane_timing.live(there), "a lane with no baseline publishes no timing estimate")
            for seconds in [55, 60, 65, 70, 90]: lane_timing.record("P", "gate", seconds)
            lane_timing.Pace("gate", {}, "P", live=Path(str(there) + lane_timing.LIVE))
            with patch.dict(os.environ, {lane_wait.VARIABLE: str(here)}):
                lane_wait.relayed(json.dumps({"timing": offload_host.timing_state(there)}), "build-host")
            found = lane_timing.live(here, lane="gate")
            self.assertNotIn("startedMonotonic", found, "another Mac's monotonic clock has no meaning here")
            self.assertIsNone(lane_timing.live(here, lane="another-lane"), "provisioning can share the wait path")
            self.assertEqual((found["state"], found["medianSeconds"], found["p90Seconds"], found["samples"]),
                             ("baseline", 65, 90, 5))
            gate = {"lane": "gate", "began": 1, "entries": ["e1"], "mac": "build-host", "timing": found}
            state = {"round": "r1", "entries": ["e1"], "gates": {"batch": gate}}
            expected = "timing: gate: median 1m05s, p90 1m30s over 5 passing runs on that Mac"
            self.assertIn(expected, "\n".join(merge_queue.round_lines(state, at=2)))
            self.assertIn(expected, merge_queue.situation(None, {"id": "e1", "state": "testing"}, state, at=2)[1])
            self.assertIsNone(merge_queue.remaining_work(gate))

    def test_remaining_work_uses_the_local_lanes_work_start_and_stops_at_its_p90(self):
        with tempfile.TemporaryDirectory() as tmp, history(tmp):
            wait = Path(tmp) / "gate.json"
            clock = Clock()
            clock.now = 100
            for seconds in [55, 60, 65, 70, 90]: lane_timing.record("P", "gate", seconds)
            lane_timing.Pace("gate", {"timing": {"warn": False}}, "P", clock=clock,
                             live=Path(str(wait) + lane_timing.LIVE))
            gate = {"lane": "gate", "began": 1, "entries": ["e1"], "mac": "this Mac",
                    "timing": lane_timing.live(wait)}
            state = {"round": "r1", "entries": ["e1"], "gates": {"batch": gate}}
            entry = {"id": "e1", "state": "testing"}
            with patch.object(lane_timing.time, "monotonic", clock):
                clock.now = 120
                self.assertEqual(merge_queue.remaining_work(gate), {"low": 45, "high": 70})
                self.assertIn("roughly 45s to 1m10s of lane work left", "\n".join(merge_queue.round_lines(state, at=1000)))
                key, said = merge_queue.situation(None, entry, state, at=1000)
                self.assertIn("roughly 45s to 1m10s of lane work left", said)
                for unavailable in ({"mac": None}, {"mac": "build-host"}, {"waiting": "admission"}, {"lane": "other"},
                                    {"timing": None}, {"timing": {**gate["timing"], "lanes": {}}}):
                    self.assertIsNone(merge_queue.remaining_work({**gate, **unavailable}))
                clock.now = 170
                after_median, said = merge_queue.situation(None, entry, state, at=1000)
                self.assertEqual(after_median, key, "countdown changes do not print new wait lines")
                self.assertIn("roughly up to 20s of lane work left", said)
                clock.now = 190
                self.assertIsNone(merge_queue.remaining_work(gate))
                self.assertNotIn("lane work left", merge_queue.situation(None, entry, state, at=1000)[1])

    def test_status_and_wait_show_a_running_gates_timing(self):
        gate = {"lane": "gate", "began": 0, "entries": ["e1"], "mac": "this Mac",
                "timing": {"state": "slow", "words": "gate has run 12m00s, past its p90 of 10m00s"}}
        state = {"round": "r1", "entries": ["e1"], "gates": {"batch": gate}}
        self.assertIn("timing: warning: gate has run 12m00s", "\n".join(merge_queue.round_lines(state, at=1)))
        key, words = merge_queue.situation(None, {"id": "e1", "state": "testing"}, state, at=1)
        self.assertIn("timing: warning: gate has run 12m00s", words)
        quiet = {**state, "gates": {"batch": {**gate, "timing": None}}}
        self.assertNotEqual(key, merge_queue.situation(None, {"id": "e1", "state": "testing"}, quiet, at=1)[0],
                            "a new warning is a new situation, which queue wait prints")

    def test_results_name_the_timing_lines_each_once(self):
        text = "\n".join(["x", lane_timing.PREFIX + "warning: gate has run 2m", "y",
                          lane_timing.PREFIX + "warning: gate has run 2m",
                          lane_timing.NOTICE_PREFIX + "gate reached its timeout: do not raise it"])
        self.assertEqual(lane_timing.found_in(text), [lane_timing.PREFIX + "warning: gate has run 2m",
                                                      lane_timing.NOTICE_PREFIX + "gate reached its timeout: do not raise it"])
        self.assertTrue(hasattr(agent_results, "lane_timing"))

    def test_a_relayed_record_is_written_beside_this_macs_wait_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            wait = Path(tmp) / "gate.json"
            lane_timing.relayed({"lane": "gate", "state": "slow", "words": "past"}, str(wait))
            self.assertEqual(lane_timing.live(wait)["words"], "past")
            self.assertIsNone(lane_timing.live(wait, since=lane_timing.time.time() + 60))


if __name__ == "__main__":
    unittest.main()
