"""The dashboard feature (queuekeeper_dashboard): its panels' data, its server and its command.

The page's scripts are not run here: test_the_page_and_every_panel_script_are_served checks that they are served,
and the page itself is checked in a browser."""
import http.client
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src/python"))
import host_isolation  # noqa: F401 -- first, as every test module
from queuekeeper import cli, events, extensions, merge_queue as mq, visibility
from queuekeeper_dashboard import admission_ids, panels, registry, repository, server
from queuekeeper_dashboard.panels import farm, history, home, logs, queue

READ_DEVICES = farm.read_devices
READ_DETAIL = farm.read_detail  # the real one, which Fixture replaces so that no test asks a Mac
from test_support import queue as test_merge_queue

HOSTS = {"schemaVersion": 1, "hosts": [
    {"name": "this Mac (desk)", "role": "here", "state": "observed", "reasons": [], "limits": {"slots": 3, "slotsMax": 7},
     "slots": [{"slot": 0, "holder": "a"}, {"slot": 1, "holder": "b"}, {"slot": 2, "holder": None}],
     "holders": [{"identity": "holder-1", "label": "project gate", "heldSeconds": 90.0, "slots": [0], "wholeHost": True, "exclusive": False}],
     "waiters": [{"identity": "place-1", "request": "request-1", "label": "other unit", "waitedSeconds": 12.5, "reason": "every slot is busy"}]},
    {"name": "second Mac (build)", "role": "second", "state": "unknown", "reasons": ["ssh could not reach build"]}]}


LOADS = {"here": {"cpuBusy": 68.0, "busyLimit": 75.0, "load": 3.2, "memory": "normal", "screen": "locked", "error": None}}


from test_support.dashboard import Fixture




class PublicAdmissionIDs(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "ids.json"
        patcher = patch.object(admission_ids, "path", return_value=self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_readable_names_are_persistent_collision_checked_and_follow_requests_into_running(self):
        reading = {"hosts": [{"role": "here", "waiters": [
            {"identity": "place", "label": "Fix share sheet (#42)", "origin": {"branch": "codex/fix-share-sheet"}}]}]}
        with patch.object(admission_ids.secrets, "randbelow", return_value=7318):
            first = admission_ids.names_for(reading)
        self.assertEqual(first["place"], "qk7318-issue-42-fix-share-sheet")
        reading["hosts"][0]["waiters"][0]["request"] = "request"
        names = admission_ids.names_for(reading)
        self.assertEqual(names["request"], first["place"])
        reading["hosts"][0] = {"role": "here", "holders": [{"identity": "request", "label": "changed metadata"}]}
        self.assertEqual(admission_ids.names_for(reading)["request"], first["place"])
        reading["hosts"][0]["holders"].append({"identity": "different", "label": "Fix share sheet (#42)",
                                              "origin": {"branch": "codex/fix-share-sheet"}})
        with patch.object(admission_ids.secrets, "randbelow", side_effect=[7318, 7319]):
            names = admission_ids.names_for(reading)
        self.assertEqual(names["different"], "qk7319-issue-42-fix-share-sheet")
        self.assertEqual(json.loads(self.path.read_text())["request"], first["place"])

    def test_command_names_use_repository_and_action_when_the_branch_is_generic(self):
        self.assertEqual(admission_ids.slug({"label": "/Applications/Xcode.app/bin/xcodebuild build -project App.xcodeproj",
            "origin": {"repository": "Pagepour", "branch": "main"}}), "pagepour-xcodebuild-build")
        self.assertEqual(admission_ids.slug({"label": "Fix layout (vocaro/queuekeeper#91)",
            "origin": {"branch": "worktree-agent-abc123"}}), "issue-91-fix-layout-vocaro-queuekeeper-91")

    def test_friendly_and_legacy_links_resolve_to_the_same_live_admission(self):
        names = {"request-1": "qk7318-project-tests", "place-1": "qk7318-project-tests"}
        for identifier in ("request-1", "place-1", "qk7318-project-tests"):
            detail = farm.admission(HOSTS, "here", identifier, names)
            self.assertEqual((detail["id"], detail["state"]), ("qk7318-project-tests", "waiting"))
        self.assertEqual(farm.host(HOSTS["hosts"][0], names)["waiters"][0]["id"], "qk7318-project-tests")


class AdmissionDetails(unittest.TestCase):
    def setUp(self):
        names = patch.object(admission_ids, "names_for", return_value={})
        names.start()
        self.addCleanup(names.stop)

    def test_detail_preserves_recorded_wait_reason_and_origin_while_list_shows_ids(self):
        reading = json.loads(json.dumps(HOSTS))
        waiter = reading["hosts"][0]["waiters"][0]
        waiter.update(pid=42, origin={"repository": "project", "branch": "codex/fix"},
                      reason="it waits behind a whole-host lane holding the host: another gate", secret="not a detail field")
        with patch.object(farm, "current", return_value=(reading, 123.0, None)):
            found = farm.data(None, {"host": "here", "admission": "request-1"})
            listing = farm.data(None, {})
        self.assertEqual(listing["hosts"][0]["waiters"][0]["id"], "request-1")
        self.assertNotIn("admission", listing)
        detail = found["admission"]
        self.assertEqual((detail["id"], detail["identity"], detail["state"]), ("request-1", "place-1", "waiting"))
        self.assertEqual(detail["reason"], waiter["reason"])
        self.assertEqual(detail["origin"]["branch"], "codex/fix")
        self.assertEqual(detail["pid"], 42)
        self.assertNotIn("secret", detail)

    def test_a_request_link_keeps_working_when_it_moves_from_waiting_to_running(self):
        reading = json.loads(json.dumps(HOSTS))
        host = reading["hosts"][0]
        host["waiters"] = []
        host["holders"].append({"identity": "request-1", "label": "other unit", "heldSeconds": 5, "slots": [0]})
        found = farm.admission(reading, "here", "request-1")
        self.assertEqual((found["state"], found["label"], found["heldSeconds"]), ("running", "other unit", 5))
        self.assertEqual(farm.host(host)["holders"][-1]["id"], "request-1")

    def test_a_missing_request_or_wrong_host_never_resolves_to_another_request(self):
        for role, identifier in (("second", "request-1"), ("here", "finished"), ("here", "../hold")):
            with self.subTest(role=role, identifier=identifier):
                self.assertEqual(farm.admission(HOSTS, role, identifier), {"id": identifier, "state": "missing"})
        self.assertEqual(farm.admission(None, "here", "request-1")["state"], "missing")


class QueueDetails(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.repository = type("Repository", (), {"directory": Path(self.directory.name)})()
        (self.repository.directory / "entries").mkdir()

    def test_entry_details_retain_notes_commits_and_metadata(self):
        found = {"id": "qk7318-fix-layout", "state": "testing", "subject": "Fix layout", "note": "Long explanation",
                 "branch": "codex/fix-layout", "tip": "a" * 40, "base": "b" * 40, "commits": ["a" * 40], "submitted": 1}
        detail = queue.detail(self.repository, {"entry": found["id"]}, {"waiting": [found]})
        self.assertEqual(detail["kind"], "entry")
        self.assertEqual((detail["entry"]["note"], detail["entry"]["commitList"], detail["entry"]["tip"]),
                         (found["note"], found["commits"], found["tip"]))

    def test_old_entry_details_are_read_safely_outside_the_recent_window(self):
        found = {"id": "20261113-100000-aaaaaaaa", "subject": "Old entry", "state": "landed"}
        path = self.repository.directory / "entries" / (found["id"] + ".json")
        path.write_text(json.dumps(found))
        self.assertEqual(queue.lookup(self.repository, found["id"], {}), found)
        (path.parent / "link.json").symlink_to(path)
        for identifier in ("../hold", "link", "missing", "/etc/passwd"):
            with self.subTest(identifier=identifier): self.assertIsNone(queue.lookup(self.repository, identifier, {}))

    def test_gate_details_keep_wait_reasons_and_do_not_switch_silently_to_a_new_round(self):
        gate = {"lane": "spelling", "waiting": "long admission explanation", "entries": ["qk7318-fix"], "began": 1}
        state = {"round": {"round": "round-1", "gates": {"batch": gate}},
                 "waiting": [{"id": "qk7318-fix", "state": "testing", "subject": "Fix"}]}
        detail = queue.detail(self.repository, {"gate": "batch", "round": "round-1"}, state)
        self.assertEqual(detail["gate"]["waiting"], gate["waiting"])
        self.assertEqual(detail["entries"][0]["subject"], "Fix")
        self.assertEqual(queue.detail(self.repository, {"gate": "batch", "round": "old"}, state)["kind"], "missing")


class Panels(Fixture):
    def test_the_panels_are_found_by_name_in_tab_order_with_a_script_each(self):
        self.assertEqual([(panel["name"], panel["title"]) for panel in panels.listed()],
                         [("queue", "Queue"), ("farm", "Farm"), ("history", "History"), ("logs", "Logs")])
        for name in panels.available():
            self.assertTrue((panels.FOLDER / f"{name}.js").is_file(), name)
            self.assertIn(f'QK.panel("{name}"', (panels.FOLDER / f"{name}.js").read_text())

    def test_a_panel_that_does_not_load_is_listed_with_its_fault(self):
        real = panels.load
        def load(name):
            if name == "farm": raise RuntimeError("a fault of its own")
            return real(name)
        with patch.object(panels, "load", load):
            found = {panel["name"]: panel for panel in panels.listed()}
        self.assertEqual(found["farm"]["error"], "a fault of its own")
        self.assertNotIn("error", found["queue"])

    def test_home_of_an_idle_queue_says_so_and_counts_nothing(self):
        found = home.data(self.one, {})
        self.assertEqual((found["level"], found["headline"], found["detail"]), ("idle", "Idle", "Nothing is waiting."))
        self.assertEqual(found["counts"], {"waiting": 0, "landed": 0, "cameBack": 0})

    def test_home_says_what_the_queue_is_doing(self):
        waiting = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
        gates = {"batch": {"lane": "gate", "mac": "this Mac", "began": 1.0}}
        for state, expected in (
                ({"held": "for the owner, since nobody else can fix it: a key"},
                 ("danger", "Held for you", "for the owner, since nobody else can fix it: a key")),
                ({"held": "for a session, not the owner: a cause"},
                 ("danger", "Held for an agent", "for a session, not the owner: a cause")),
                ({"worker": True, "round": {"round": "r", "entries": ["a"], "gates": gates}, "waiting": waiting},
                 ("active", "Testing 1 entry", "The gate lane is running on this Mac. 2 more waiting.")),
                ({"worker": True, "round": {"round": "r", "entries": ["a", "b"]}, "waiting": waiting[:2]},
                 ("active", "Testing 2 entries", "Its round is starting.")),
                ({"worker": True, "waiting": waiting}, ("active", "Worker running", "3 waiting.")),
                ({"waiting": waiting}, ("warning", "3 waiting with no worker", "`qk queue resume` starts one."))):
            with self.subTest(expected=expected[1]):
                self.assertEqual(home.headline(state, None), expected)

    def test_home_counts_the_day_and_gives_each_panels_line_what_needs_the_owner_first(self):
        self.settle("one", why="its gate failed")
        self.settle("two", mq.LANDED, landedAs="0" * 40)
        self.commit("three.txt", "three")
        self.add("three")
        self.one.state = None
        self.read_farm()
        found = home.data(self.one, {})
        self.assertEqual(found["counts"], {"waiting": 1, "landed": 1, "cameBack": 1})
        self.assertEqual([(line["panel"], line["title"]) for line in found["lines"]],
                         [("queue", "Needs you"), ("farm", "Farm"), ("history", "Last landed")])
        self.assertEqual(found["lines"][0]["text"], "1 waiting with no worker")
        self.assertEqual(found["lines"][2]["text"], "Add two.txt")

    def test_home_gives_the_last_days_trend_hour_by_hour_and_the_gates_last_passing_runs(self):
        now = 1_800_000_000.0
        at = lambda hours, kind, **fields: {"time": now - hours * 3600, "kind": kind, **fields}
        journal = [at(30, "entry.landed"), at(30, "entry.queued"),
                   at(5.5, "entry.queued"), at(5.4, "entry.queued"), at(5.2, "entry.queued"),
                   at(3.5, "entry.landed"), at(3.4, "entry.failed"), at(1.5, "entry.conflict"), at(0.5, "entry.queued"),
                   *(at(4, "gate.ended", lane="gate", status=0, seconds=seconds) for seconds in (600.4, 900, 1200)),
                   at(4, "gate.ended", lane="gate", status=1, seconds=5), at(4, "gate.ended", lane="spelling", status=0, seconds=3)]
        found = home.trend(journal, 2, now)
        self.assertEqual([len(found[name]) for name in ("waiting", "landed", "cameBack")], [home.HOURS] * 3)
        self.assertEqual(found["waiting"][-7:], [1, 4, 4, 2, 2, 1, 2])
        self.assertEqual(found["waiting"][:18], [1] * 18)
        self.assertEqual((sum(found["landed"]), found["landed"][20]), (1, 1))
        self.assertEqual((found["cameBack"][20], found["cameBack"][22]), (1, 1))
        self.assertEqual((found["gateSeconds"], found["medianGateSeconds"]), ([600, 900, 1200], 900))
        idle = home.trend([], 0, now)
        self.assertEqual((idle["waiting"], idle["gateSeconds"], idle["medianGateSeconds"]), ([0] * home.HOURS, [], None))
        self.assertIn("trend", home.data(self.one, {}))

    def test_home_shows_a_panel_whose_line_fails_as_a_fault_and_keeps_the_others(self):
        with patch.object(history, "summary", side_effect=RuntimeError("no journal")):
            lines = {line["panel"]: line for line in home.data(self.one, {})["lines"]}
        self.assertEqual(lines["history"]["text"], "This panel has a fault: no journal")
        self.assertIn("farm", lines)

    def test_the_queue_panel_shows_entries_gates_and_log_names_without_paths(self):
        self.commit("a.txt", "one")
        identifier, = self.add("one")
        log = mq.Queue(self.repo).logs / "round-gate.log"
        failed = self.settle("two", why="its gate failed", log=str(log))
        state = {**mq.snapshot(self.repo), "worker": True, "round": {
            "round": "r1", "base": "b" * 40, "entries": [identifier], "bisect": None, "gates": {
                "stacked": {"lane": "gate", "mac": None, "began": 5.0},
                "batch": {"lane": "gate", "mac": "this Mac", "began": 1.0, "log": str(log), "entries": [identifier],
                          "progress": [{"phase": "building"}, {"phase": "testing"}], "waiting": "admission",
                          "waitingSince": 2.0, "stuck": "no output"}}}}
        with patch.object(self.one, "snapshot", lambda: state): found = queue.data(self.one, {})
        self.assertEqual([gate["role"] for gate in found["round"]["gates"]], ["batch", "stacked"])
        self.assertEqual(found["round"]["gates"][0], {
            "role": "batch", "lane": "gate", "mac": "this Mac", "began": 1.0, "waiting": "admission", "waitingSince": 2.0,
            "stuck": "no output", "entries": [identifier], "log": "round-gate.log", "phase": "testing"})
        self.assertEqual((found["round"]["name"], found["worker"], found["held"]), ("r1", True, None))
        waiting, = found["waiting"]
        self.assertEqual((waiting["id"], waiting["state"], waiting["subject"], waiting["commits"]),
                         (identifier, "queued", "Add a.txt", 1))
        recent, = found["recent"]
        self.assertEqual((recent["id"], recent["state"], recent["why"], recent["log"]),
                         (failed, "failed", "its gate failed", "round-gate.log"))
        self.assertNotIn(str(self.base), json.dumps(found))

    def test_queue_includes_the_overview_and_history_includes_settled_changes(self):
        identifier = self.settle("human-readable", why="gate failed")
        found = queue.data(self.one, {})
        self.assertEqual(found["overview"]["counts"]["cameBack"], 1)
        self.assertEqual(history.data(self.one, {})["recent"][0]["id"], identifier)


    def test_the_queue_panels_line_on_home_is_what_needs_the_owner(self):
        self.assertIsNone(queue.summary(self.one))
        self.settle("one", why="its gate failed")
        self.assertEqual(queue.summary(self.one)["text"], "1 entry came back within the hour")
        self.commit("b.txt", "two")
        self.add("two")
        self.one.state = None
        self.assertEqual(queue.summary(self.one)["text"], "1 waiting with no worker")
        mq.Queue(self.repo).hold("for a test")
        self.one.state = None
        self.assertEqual(queue.summary(self.one), {"title": "Needs you", "text": "The queue is held for a test",
                                                   "level": "danger"})

    def test_an_entry_that_came_back_long_ago_is_no_longer_news(self):
        self.settle("one", why="its gate failed")
        with patch.object(queue.time, "time", return_value=time.time() + queue.NEWS + 1):
            self.assertIsNone(queue.summary(self.one))

    def test_the_farm_answers_at_once_and_reads_in_the_background(self):
        first = farm.data(self.one, {})
        self.assertEqual((first["hosts"], first["readAt"]), (None, None))
        self.read_farm()
        found = farm.data(self.one, {})
        here, second = found["hosts"]
        self.assertEqual(here["slots"], {"used": 2, "guaranteed": 3, "most": 7})
        self.assertEqual(here["holders"], [{"id": "holder-1", "label": "project gate", "heldSeconds": 90.0, "slots": 1, "wholeHost": True,
                                           "exclusive": False}])
        self.assertEqual(here["waiters"], [{"id": "request-1", "label": "other unit", "waitedSeconds": 12.5, "reason": "every slot is busy"}])
        self.assertEqual((second["state"], second["reasons"]), ("unknown", ["ssh could not reach build"]))
        self.assertEqual((here["load"], second["load"]), (LOADS["here"], None))
        self.assertEqual(farm.summary(self.one), {
            "title": "Farm", "level": "warning",
            "text": "1 Mac read, 2 of 7 slots in use, 1 waiting; the screen of this Mac (desk) is locked; "
                    "second Mac (build) could not be read"})

    def test_a_macs_load_is_what_its_pressure_and_screen_readings_say(self):
        self.assertEqual(farm.load_of({"cpuBusy": 91.4, "busyLimit": 75, "load": 12.08, "memory": 4}, {"state": "unlocked"}),
                         {"cpuBusy": 91.4, "busyLimit": 75, "load": 12.08, "memory": "critical", "screen": "unlocked",
                          "error": None})
        self.assertEqual(farm.load_of({"cpuBusy": None, "memory": None, "busyLimit": True}, {"state": "asleep"}),
                         {"cpuBusy": None, "busyLimit": None, "load": None, "memory": None, "screen": "unknown",
                          "error": None})
        self.assertEqual(farm.load_of({"error": "no slot directory"}, None)["error"], "no slot directory")
        self.assertEqual(farm.load_of("nonsense", [])["screen"], "unknown")

    def test_both_macs_are_asked_and_one_that_cannot_be_is_given_its_reason(self):
        pressure = {"cpuBusy": 10.0, "busyLimit": 75.0, "load": 1.0, "memory": 1}
        with patch.object(farm.offload_host, "pressure", return_value=pressure), \
                patch.object(farm.offload_host, "screen", return_value={"state": "unlocked"}), \
                patch.object(farm.remote, "configured", return_value="build"), \
                patch.object(farm.remote, "reading_there", return_value=({**pressure, "cpuBusy": 80.0}, 80.0)), \
                patch.object(farm.remote, "screen_there", return_value={"state": "locked"}):
            found = READ_DETAIL()
            self.assertEqual((found["here"]["cpuBusy"], found["here"]["screen"]), (10.0, "unlocked"))
            self.assertEqual((found["second"]["cpuBusy"], found["second"]["screen"]), (80.0, "locked"))
            with patch.object(farm.remote, "screen_there", side_effect=farm.remote.Unavailable("build is asleep")):
                self.assertEqual(READ_DETAIL()["second"]["error"], "build is asleep")
            with patch.object(farm.remote, "configured", return_value=None):
                self.assertEqual(list(READ_DETAIL()), ["here"])

    def test_a_load_that_cannot_be_read_leaves_the_slots_and_the_last_load_shown(self):
        self.read_farm()
        farm.STATE["at"] = 0.0
        with patch.object(farm, "read_detail", side_effect=RuntimeError("no ioreg")):
            self.read_farm()
        found = farm.data(self.one, {})
        self.assertIsNone(found["error"])
        self.assertEqual(found["hosts"][0]["load"], LOADS["here"])

    def test_a_farm_that_cannot_be_read_says_why_and_keeps_its_last_reading(self):
        self.read_farm()
        farm.STATE["at"] = 0.0
        with patch.object(farm, "read", side_effect=RuntimeError("no slot directory")):
            self.read_farm()
        found = farm.data(self.one, {})
        self.assertEqual(found["error"], "no slot directory")
        self.assertEqual(len(found["hosts"]), 2)

    def test_the_farm_is_read_at_most_once_while_a_reading_is_fresh(self):
        count = []
        with patch.object(farm, "read", lambda: count.append(1) or HOSTS):
            for _ in range(5): self.read_farm()
        self.assertEqual(len(count), 1)

    def test_logs_lists_the_queues_logs_newest_first_with_the_workers_first(self):
        queue_ = mq.Queue(self.repo)
        for number, name in enumerate(("old.log", "new.log")):
            (queue_.logs / name).write_text("x" * (number + 1))
            os.utime(queue_.logs / name, (1000 + number, 1000 + number))
        (queue_.logs / ".hidden").write_text("x")
        (queue_.logs / "folder").mkdir()
        os.symlink("/etc/hosts", queue_.logs / "link.log")
        (queue_.directory / "worker.log").write_text("worker")
        found = logs.data(self.one, {})["logs"]
        self.assertEqual([(log["name"], log["size"]) for log in found], [("worker.log", 6), ("new.log", 2), ("old.log", 1)])

    def test_a_log_is_read_from_its_tail_and_then_from_where_the_last_chunk_ended(self):
        path = mq.Queue(self.repo).logs / "gate.log"
        path.write_text("a" * (logs.TAIL + 10) + "END\n")
        first = logs.data(self.one, {"name": "gate.log"})
        self.assertEqual((first["offset"], first["next"], first["size"]), (14, path.stat().st_size, path.stat().st_size))
        self.assertTrue(first["text"].endswith("END\n"))
        with path.open("a") as log: log.write("more\n")
        second = logs.data(self.one, {"name": "gate.log", "offset": str(first["next"])})
        self.assertEqual((second["text"], second["offset"]), ("more\n", first["next"]))
        self.assertEqual(logs.data(self.one, {"name": "gate.log", "offset": "999999999"})["text"], "")
        self.assertEqual(len(logs.data(self.one, {"name": "gate.log", "offset": "0"})["text"]), min(logs.CHUNK, logs.TAIL + 19))

    def test_only_a_log_of_the_queues_is_read_by_a_name_with_no_folder_in_it(self):
        queue_ = mq.Queue(self.repo)
        (queue_.logs / "gate.log").write_text("x")
        (queue_.directory / "hold").write_text("secret")
        os.symlink(queue_.directory / "hold", queue_.logs / "link.log")
        (queue_.logs / "folder").mkdir()
        for name in ("../hold", "/etc/hosts", "..", ".", "", ".hidden", "link.log", "folder", "missing.log",
                     "logs/gate.log", "..\\hold", "entries"):
            with self.subTest(name=name), self.assertRaises(LookupError):
                logs.path_of(self.one, name)
        with self.assertRaises(LookupError): logs.data(self.one, {"name": "gate.log", "offset": "x"})
        self.assertEqual(logs.path_of(self.one, "gate.log")[0], queue_.logs / "gate.log")

    def test_history_gives_the_days_totals_and_the_events_newest_first(self):
        directory = mq.Queue(self.repo).directory
        events.emit(directory, "round.began", round="r")
        for seconds in (600, 900, 1200): events.emit(directory, "gate.ended", lane="gate", status=0, seconds=seconds)
        events.emit(directory, "gate.ended", lane="gate", status=1, seconds=5)
        events.emit(directory, "gate.ended", lane="spelling", status=0, seconds=3)
        events.emit(directory, "test.flaky", test="test_x", commit="c" * 40)
        events.emit(directory, "entry.failed", entry="a", why="its gate failed")
        events.emit(directory, "entry.landed", entry="b", subject="Add b", landedAs="0123456789abcdef")
        with patch.object(events.time, "time", return_value=time.time() - 3 * 86400):
            events.emit(directory, "entry.landed", entry="old", landedAs="f" * 40)
        found = history.data(self.one, {})
        self.assertEqual(found["today"], {"landed": 1, "cameBack": 1, "flaky": 1, "rounds": 1, "medianGateSeconds": 900})
        self.assertEqual(found["total"], 10)
        self.assertEqual(found["events"][1], {"time": found["events"][1]["time"], "kind": "entry.landed",
                                             "text": "b landed as 0123456789ab", "level": "success"})
        self.assertEqual([event["level"] for event in found["events"][2:4]], ["danger", "warning"])
        self.assertEqual([event["kind"] for event in history.data(self.one, {"kind": "entry."})["events"]],
                         ["entry.landed", "entry.landed", "entry.failed"])
        self.assertEqual(history.summary(self.one)["text"], "old")

    def test_history_shows_only_the_last_events_of_a_long_journal(self):
        directory = mq.Queue(self.repo).directory
        for number in range(history.MOST + 5): events.emit(directory, "queue.resumed", was=str(number))
        found = history.data(self.one, {})
        self.assertEqual((len(found["events"]), found["total"]), (history.MOST, history.MOST + 5))
        self.assertIsNone(history.summary(self.one))


class Repositories(Fixture):
    def test_a_snapshot_serves_every_panel_for_a_moment_and_the_journal_until_it_changes(self):
        with patch.object(repository.merge_queue, "snapshot", wraps=mq.snapshot) as read:
            for _ in range(5): self.one.snapshot()
            self.assertEqual(read.call_count, 1)
            self.one.read_at -= repository.FRESH + 1
            self.one.snapshot()
            self.assertEqual(read.call_count, 2)
        directory = mq.Queue(self.repo).directory
        events.emit(directory, "queue.resumed")
        with patch.object(repository.events, "read", wraps=events.read) as read:
            self.assertEqual(len(self.one.events()), 1)
            self.one.events()
            self.assertEqual(read.call_count, 1)
            events.emit(directory, "queue.resumed")
            self.assertEqual(len(self.one.events()), 2)

    def test_repositories_of_one_folder_name_get_names_of_their_own(self):
        found = repository.named(["/a/project", "/b/project", "/c/other", "/d/project"])
        self.assertEqual({name: str(one.root) for name, one in found.items()},
                         {"project": "/a/project", "project-2": "/b/project", "other": "/c/other", "project-3": "/d/project"})

    def test_the_registry_lists_each_repository_once_and_drops_one_whose_queue_is_gone(self):
        gone = self.base / "gone"
        (gone / ".git" / registry.QUEUE).mkdir(parents=True)
        mq.Queue(self.repo)  # which makes the fixture's queue
        for root in (self.repo, gone, self.repo): registry.add(root)
        self.assertEqual(registry.known(), [self.repo, gone])
        shutil.rmtree(gone)
        self.assertEqual(registry.known(), [self.repo])
        registry.path().write_text("not json")
        self.assertEqual(registry.known(), [])

    def test_a_queue_that_journals_an_event_is_added_to_the_registry_once_a_process(self):
        directory = str(mq.Queue(self.repo).directory)
        self.addCleanup(registry.NOTED.discard, directory)
        with patch.object(registry, "add", wraps=registry.add) as added:
            registry.note(directory, {"kind": "entry.queued"})
            registry.note(directory, {"kind": "entry.landed"})
        self.assertEqual(added.call_count, 1)
        self.assertEqual(registry.known(), [self.repo])
        self.assertIsNone(registry.checkout_of("/a/origin.git/queuekeeper-queue"))


class Served(Fixture):
    """Requests to a server on a port of its own, on this Mac alone."""

    def setUp(self):
        super().setUp()
        self.server = server.make([self.repo], port=0)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_address[1]

    def get(self, path, method="GET", host=None):
        """(status, headers, body) of a request, whose Host is this server's by name unless host is given."""
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=host_isolation.stretched(20))
        self.addCleanup(connection.close)
        connection.request(method, path, headers={"Host": f"localhost:{self.port}" if host is None else host})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()

    def api(self, path):
        status, _, body = self.get("/api" + path)
        return status, json.loads(body)

    def test_the_page_and_every_panel_script_are_served(self):
        for path in ("/", "/r", "/r/project/queue", "/r/project/logs?name=worker.log"):
            status, headers, body = self.get(path)
            self.assertEqual((status, headers["Content-Type"]), (200, "text/html; charset=utf-8"), path)
            self.assertIn(b'<script src="/static/app.js">', body)
        for path, kind in (("/static/app.js", "text/javascript; charset=utf-8"), ("/static/app.css", "text/css; charset=utf-8"),
                           *((f"/panels/{name}.js", "text/javascript; charset=utf-8") for name in panels.available())):
            status, headers, body = self.get(path)
            self.assertEqual((status, headers["Content-Type"]), (200, kind), path)
            self.assertTrue(body)

    def test_every_answer_forbids_what_the_page_does_not_need(self):
        for path in ("/", "/api/panels", "/static/app.js", "/nope"):
            headers = self.get(path)[1]
            self.assertEqual(headers["Content-Security-Policy"], server.POLICY, path)
            self.assertEqual((headers["Cache-Control"], headers["X-Content-Type-Options"]), ("no-store", "nosniff"))
            self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_the_page_has_no_inline_script_or_style_which_its_policy_would_refuse(self):
        page = (server.STATIC / "index.html").read_text()
        self.assertNotIn("<style", page)
        self.assertNotIn(" style=", page)
        self.assertEqual(page.count("<script"), 1)
        for path in (server.STATIC / "app.js", *panels.FOLDER.glob("*.js")):
            self.assertNotIn("innerHTML", path.read_text(), path.name)

    def test_the_api_lists_the_panels_and_the_repositories(self):
        self.assertEqual([panel["name"] for panel in self.api("/panels")[1]], ["queue", "farm", "history", "logs"])
        status, found = self.api("/repositories")
        self.assertEqual((status, found), (200, [{"name": "project", "level": "idle", "headline": "Idle",
                                                  "detail": "Nothing is waiting.", "waiting": 0}]))

    def test_each_panels_data_is_answered_for_a_repository_with_the_query_passed_on(self):
        (mq.Queue(self.repo).logs / "gate.log").write_text("hello\n")
        for name in panels.available():
            status, found = self.api(f"/r/project/panel/{name}")
            self.assertEqual(status, 200, (name, found))
            self.assertIsInstance(found, dict)
        status, found = self.api("/r/project/panel/logs?name=gate.log&offset=2")
        self.assertEqual((status, found["text"], found["next"]), (200, "llo\n", 6))

    def test_what_is_not_there_is_answered_404_as_json(self):
        for path in ("/r/nope/panel/home", "/r/project/panel/nope", "/r/project/panel/logs?name=../hold",
                     "/r/project/panel/logs?name=missing.log", "/nope", "/r/project", "/r/project/panel/home/more"):
            status, found = self.api(path)
            self.assertEqual(status, 404, path)
            self.assertIn("error", found)
        for path in ("/static/nope.js", "/static/../server.py", "/panels/nope.js", "/panels/farm.py", "/panels/__init__.py",
                     "/static/", "/favicon.ico"):
            self.assertEqual(self.get(path)[0], 404, path)

    def test_a_panel_that_raises_is_answered_as_that_panels_fault_and_the_others_still_answer(self):
        with patch.object(history, "data", side_effect=RuntimeError("no journal")):
            status, found = self.api("/r/project/panel/history")
            self.assertEqual((status, found), (500, {"error": "the history panel has a fault: no journal"}))
            self.assertEqual(self.api("/r/project/panel/queue")[0], 200)
        with patch.object(panels, "load", side_effect=ImportError("no module")):
            self.assertEqual(self.api("/r/project/panel/queue"), (500, {"error": "the queue panel did not load: no module"}))

    def test_only_a_request_that_names_this_mac_is_answered(self):
        for host in ("evil.example", "evil.example:80", f"localhost.evil.example:{self.port}", "", "192.168.1.5"):
            status, _, body = self.get("/api/repositories", host=host)
            self.assertEqual(status, 403, host)
            self.assertNotIn(b"project", body)
        for host in ("localhost", f"localhost:{self.port}", f"127.0.0.1:{self.port}", f"[::1]:{self.port}"):
            self.assertEqual(self.get("/api/repositories", host=host)[0], 200, host)

    def test_nothing_but_reading_is_answered(self):
        for method in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS"):
            status, headers, _ = self.get("/api/repositories", method=method)
            self.assertEqual((status, headers["Allow"]), (405, "GET, HEAD"), method)
        status, headers, body = self.get("/api/panels", method="HEAD")
        self.assertEqual((status, body), (200, b""))

    def test_a_repository_added_to_the_registry_after_the_start_is_served(self):
        other = self.base / "other"
        (other / ".git" / registry.QUEUE).mkdir(parents=True)
        registry.add(other)
        self.assertEqual([one["name"] for one in self.api("/repositories")[1]], ["project", "other"])
        self.assertEqual(self.api("/repositories")[1][1]["headline"], "Cannot be read")
        self.assertEqual(self.api("/r/project/panel/home")[0], 200)

    def test_a_connection_the_browser_drops_between_requests_says_nothing(self):
        stderr = io.StringIO()
        with patch.object(sys, "stderr", stderr):
            try: raise ConnectionResetError(54, "Connection reset by peer")
            except ConnectionResetError: self.server.handle_error(None, ("127.0.0.1", 1))
        self.assertEqual(stderr.getvalue(), "")
        with patch.object(sys, "stderr", stderr):
            try: raise RuntimeError("a fault")
            except RuntimeError: self.server.handle_error(None, ("127.0.0.1", 1))
        self.assertIn("RuntimeError: a fault", stderr.getvalue())

    def test_a_repository_keeps_what_it_has_read_across_listings(self):
        before = self.server.app.repository("project")
        self.api("/repositories")
        self.assertIs(self.server.app.repository("project"), before)


class Command(Fixture):
    def setUp(self):
        super().setUp()
        settings = patch.object(visibility, "settings_path", lambda: self.base / "settings" / visibility.FILE)
        settings.start()
        self.addCleanup(settings.stop)
        for kept in (extensions.COMMANDS, extensions.LOADED, extensions.AFTER_EVENT):
            before = kept.copy()
            self.addCleanup(lambda kept=kept, before=before: (kept.clear(), kept.update(before) if isinstance(kept, dict)
                                                              else kept.extend(before)))
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop(visibility.SWITCH, None)
        extensions.COMMANDS.pop("serve", None)

    def command(self, *words, project=None):
        printed, errors, status = io.StringIO(), io.StringIO(), None
        with patch("sys.stdout", printed), patch("sys.stderr", errors):
            try: status = cli.main(["--project", str(project or self.repo), "serve", *words])
            except SystemExit: pass
        return status, printed.getvalue(), errors.getvalue()

    def test_it_is_no_command_until_the_feature_is_on(self):
        status, _, errors = self.command()
        self.assertIsNone(status)
        self.assertIn("invalid choice: 'serve'", errors)

    def test_once_on_it_serves_its_project_on_this_mac_alone_and_lists_it_in_the_registry(self):
        visibility.switch("dashboard", True)
        made = []
        def serving(served): made.append(served)
        with patch.object(server.http.server.ThreadingHTTPServer, "serve_forever", serving):
            status, printed, errors = self.command("--port", "0")
        self.assertEqual((status, errors), (0, ""))
        self.assertEqual(made[0].server_address[0], "127.0.0.1")
        self.assertRegex(printed, r"serving 1 repository at http://localhost:\d+/; Ctrl-C stops it")
        self.assertEqual(list(made[0].app.repositories), ["project"])
        self.assertEqual(registry.stored(), [str(self.repo)])

    def test_another_address_is_served_only_with_a_warning(self):
        visibility.switch("dashboard", True)
        with patch.object(server.http.server.ThreadingHTTPServer, "serve_forever", lambda served: None):
            status, _, errors = self.command("--port", "0", "--bind", "0.0.0.0")
        self.assertEqual(status, 0)
        self.assertIn("0.0.0.0 is not this Mac alone: every machine that can reach it can read the queue and its logs",
                      errors)

    def test_a_port_in_use_and_a_folder_that_is_no_checkout_are_refused(self):
        visibility.switch("dashboard", True)
        taken = server.make([self.repo], port=0)
        self.addCleanup(taken.server_close)
        status, _, errors = self.command("--port", str(taken.server_address[1]))
        self.assertEqual(status, 2)
        self.assertIn("cannot serve on 127.0.0.1", errors)
        status, _, errors = self.command("--port", "0", str(self.base / "nowhere"))
        self.assertEqual(status, 2)
        self.assertIn("is no checkout of a repository", errors)


if __name__ == "__main__":
    unittest.main()
