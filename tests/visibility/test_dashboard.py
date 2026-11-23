"""The dashboard feature (queuekeeper_dashboard): its panels' data, its server and its command.

The page's scripts are not run here: test_the_page_and_every_panel_script_are_served checks that they are served,
and the page itself is checked in a browser."""
import http.client
import http.server
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
from test_support import farm as farm_fixture
from queuekeeper import cli, events, lane_timing, plugins, merge_queue as mq, visibility
from queuekeeper import __version__ as queuekeeper_version
from queuekeeper_dashboard import admission_ids, panels, registry, repository, server
from queuekeeper_dashboard.panels import activity, farm, history, home, logs, queue

READ_DEVICES = farm.read_devices
READ_DETAIL = farm.read_detail  # the real one, which Fixture replaces so that no test asks a Mac
from test_support import queue as test_merge_queue

HOSTS = {"schemaVersion": 1, "hosts": [
    {"name": "this Mac (desk)", "role": "here", "state": "observed", "reasons": [], "limits": {"slots": 3, "slotsMax": 7},
     "slots": [{"slot": 0, "holder": "a"}, {"slot": 1, "holder": "b"}, {"slot": 2, "holder": None}],
     "holders": [{"identity": "holder-1", "label": "project gate", "heldSeconds": 90.0, "slots": [0], "wholeHost": True, "exclusive": False}],
     "waiters": [{"identity": "place-1", "request": "request-1", "label": "other unit", "waitedSeconds": 12.5, "reason": "every slot is busy"}]},
    {"name": "build", "role": "second", "state": "unknown", "reasons": ["ssh could not reach build"]}]}


GB = 1024 ** 3
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
                         [("queue", "Now"), ("farm", "Farm"), ("history", "Trends"), ("activity", "Activity"), ("logs", "Logs"), ("about", "About")])
        # Logs and About are pages reached from the others' links, not tabs of their own.
        self.assertEqual([panel["name"] for panel in panels.listed() if not panel["tab"]], ["logs", "about"])
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

    def test_the_flow_map_groups_entries_by_branch_prefix_and_puts_each_gate_on_its_mac(self):
        now = 1_800_000_000.0
        waiting = [{"id": "a", "branch": "claude/one", "state": "testing"}, {"id": "b", "branch": "claude/two", "state": "queued"},
                   {"id": "c", "branch": "codex/x", "state": "queued"}, {"id": "d", "branch": "plain", "state": "queued"},
                   {"id": "e", "branch": "ann/x"}, {"id": "f", "branch": "bob/x"}, {"id": "g", "branch": "cy/x"}]
        recent = [{"id": "old", "branch": "codex/y", "state": "failed", "settled": now - queue.NEWS - 1},
                  {"id": "new", "branch": "zed/y", "state": "conflict", "subject": "Z", "settled": now - 60},
                  {"id": "landed", "branch": "claude/z", "state": "landed", "settled": now - 10}]
        state = {"waiting": waiting, "recent": recent, "round": {"entries": ["c"]}}
        gates = [("batch", {"mac": "this Mac"}), ("stacked", {"mac": "build"}), ("older half", {})]
        self.assertEqual(queue.flow(state, gates, now)["macs"], [])  # before the farm's first reading
        self.read_farm()
        found = queue.flow(state, gates, now)
        self.assertEqual(found["agents"], [
            {"name": "Claude", "waiting": 1, "testing": 1}, {"name": "ann", "waiting": 1, "testing": 0},
            {"name": "bob", "waiting": 1, "testing": 0}, {"name": "Codex", "waiting": 0, "testing": 1},
            {"name": "2 more", "waiting": 2, "testing": 0, "more": True}])
        self.assertEqual(found["macs"], [
            {"name": "this Mac (desk)", "role": "here", "state": "observed", "used": 2, "most": 7, "gates": ["batch"]},
            {"name": "build", "role": "second", "state": "unknown", "used": 0, "most": 0, "gates": ["stacked"]}])
        self.assertEqual(found["back"], {"agent": "zed", "id": "new", "subject": "Z", "state": "conflict", "settled": now - 60})
        self.assertEqual([queue.agent({"branch": branch}) for branch in ("claude/a/b", "codex/x", "ann/x", "main", "/lead", None)],
                         ["Claude", "Codex", "ann", "Person", "Person", "Person"])
        # The agent a submission recorded comes first, then the agent whose worktree it came from, then the prefix.
        self.assertEqual(queue.agent({"agent": "codex", "directory": "/r/.claude/worktrees/x", "branch": "ann/x"}), "Codex")
        self.assertEqual(queue.agent({"directory": "/r/.claude/worktrees/agent-a5", "branch": "worktree-agent-a5"}), "Claude")
        self.assertEqual(queue.agent({"directory": "/u/.codex/worktrees/de6d/r", "branch": None}), "Codex")
        self.assertEqual(queue.agent({"agent": "unknown", "directory": "/r", "branch": "ann/x"}), "ann")
        self.assertEqual(queue.agent(None), "Person")
        # The page is given an entry without where it came from, so it carries whose it is, which every repository's
        # flow map reads.
        shown = queue.entry({"id": "x", "agent": "claude", "directory": "/r", "branch": "worktree-agent-a5"})
        self.assertEqual((shown["by"], "directory" in shown, queue.agent(shown)), ("Claude", False, "Claude"))
        self.assertEqual(queue.data(self.one, {})["flow"]["agents"], [])

    def test_each_mac_is_drawn_by_its_kind_which_a_battery_tells_and_a_model_alone_does_not(self):
        from queuekeeper import offload_host
        battery = '+-o AppleSmartBattery\n    {\n      "BatteryInstalled" = Yes\n'
        cases = [("MacBookPro18,2", None, "laptop"), ("Mac17,6", battery, "laptop"), ("Mac14,13", "", "desktop"),
                 ("Mac14,13", None, "unknown"), ("", battery, "unknown")]
        for model, found, kind in cases:
            # A reading not given is read, and here cannot be.
            with self.subTest(model=model, battery=found), patch.object(offload_host, "output", return_value=None):
                self.assertEqual(offload_host.hardware(model, found)["kind"], kind)
        # What the Farm tab draws: laptop or desktop, and other for unknown or for a Mac that did not say.
        self.assertEqual([farm.hardware_of(found)["kind"] for found in ({"kind": "laptop", "model": "Mac17,6"},
                          {"kind": "unknown"}, None)], ["laptop", "other", "other"])
        self.assertEqual(farm.load_of({"hardware": {"kind": "desktop", "model": "Mac14,13"}}, {})["hardware"],
                         {"kind": "desktop", "model": "Mac14,13"})
        # Each kind's icon is served, and carries its CC BY 4.0 credit.
        for kind in ("laptop", "desktop", "iphone", "ipad", "other"):
            icon = server.STATIC / f"device-{kind}.svg"
            self.assertTrue(icon.read_text().startswith("<!--") and "CC BY 4.0" in icon.read_text().splitlines()[0], kind)

    def test_queue_includes_the_overview_and_the_queues_health(self):
        self.settle("human-readable", why="gate failed")
        found = queue.data(self.one, {})
        self.assertEqual(found["overview"]["counts"]["cameBack"], 1)
        # The queue's health is `qk queue health`'s verdict, and before the farm's first reading the Macs are unknown,
        # never well (queuekeeper.health).
        with patch.dict(farm.STATE, {"detail": {}, "devices": None}):
            verdict = queue.data(self.one, {})["health"]
        self.assertEqual([sign["name"] for sign in verdict["signs"]], ["progress", "pace", "macs", "devices", "cameBack"])
        self.assertEqual(verdict["signs"][2]["level"], "unknown")
        self.assertNotIn(verdict["verdict"], ("good", "quiet"))


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
        self.assertEqual(here["holders"], [{"id": "holder-1", "label": "project gate", "heldSeconds": 90.0, "project": None, "slots": 1,
                                           "wholeHost": True, "exclusive": False}])
        self.assertEqual(here["waiters"], [{"id": "request-1", "label": "other unit", "waitedSeconds": 12.5, "project": None,
                                           "reason": "every slot is busy"}])
        self.assertEqual((second["state"], second["reasons"]), ("unknown", ["ssh could not reach build"]))
        self.assertEqual((here["load"], second["load"]), (LOADS["here"], None))
        self.assertEqual(farm.summary(self.one), {
            "title": "Farm", "level": "warning",
            "text": "1 Mac read, 2 of 7 slots in use, 1 waiting; the screen of this Mac (desk) is locked; "
                    "build could not be read"})

    def test_each_slot_names_the_project_holding_it_and_the_last_hour_is_kept_as_samples(self):
        reading = {"hosts": [
            {"name": "this Mac", "role": "here", "state": "observed", "limits": {"slots": 2, "slotsMax": 3},
             "slots": [{"slot": 0, "guaranteed": True, "holder": "a"}, {"slot": 1, "guaranteed": True, "holder": "b"},
                       {"slot": 2, "guaranteed": False, "holder": None}],
             "holders": [{"identity": "a", "slots": [0], "origin": {"repository": "EPUBLib"}},
                         {"identity": "b", "slots": [1], "origin": None}],
             "waiters": [{"identity": "w", "origin": {"repository": "queuekeeper"}}]},
            {"name": "build", "role": "build", "state": "unknown", "slots": [{"slot": 0, "holder": "z"}]}]}
        here = farm.host(reading["hosts"][0])
        self.assertEqual(here["cells"], [{"held": True, "project": "EPUBLib", "guaranteed": True},
                                         {"held": True, "project": None, "guaranteed": True},
                                         {"held": False, "project": None, "guaranteed": False}])
        self.assertEqual(([one["project"] for one in here["holders"]], here["waiters"][0]["project"]), (["EPUBLib", None], "queuekeeper"))
        # A Mac not read counts neither its slots nor its load; work naming no project is "other".
        self.assertEqual(farm.sample(reading, {"here": {"cpuBusy": 40.0, "load": 3.5}}, 100.04),
                         {"at": 100.0, "macs": {"here": {"cpuBusy": 40.0, "load": 3.5}}, "projects": {"EPUBLib": 1, "other": 1}, "most": 3})
        # A reading adds a sample, and one older than the hour kept goes.
        with farm.LOCK: farm.STATE["samples"] = [{"at": time.time() - farm.KEPT - 1}, {"at": time.time() - 60}]
        self.read_farm()
        samples = farm.data(self.one, {})["samples"]
        self.assertEqual(len(samples), 2)
        self.assertEqual((samples[-1]["macs"], samples[-1]["most"]), ({"here": {"cpuBusy": 68.0, "load": 3.2}}, 3))
        # A reading that fails adds none.
        farm.STATE["at"] = 0.0
        with patch.object(farm, "read", side_effect=RuntimeError("no slot directory")):
            self.read_farm()
        self.assertEqual(len(farm.data(self.one, {})["samples"]), 2)

    def test_history_compares_with_yesterday_and_charts_lane_runs_landings_and_gate_times(self):
        now = time.mktime((2026, 11, 16, 15, 0, 0, 0, 0, -1))
        today, yesterday = history.midnight(now), history.midnight(now) - 86400 + 3600
        journal = [{"kind": "entry.queued", "time": today - 3 * 86400},
                   {"kind": "entry.landed", "time": yesterday + 3600},  # 02:00 yesterday
                   {"kind": "entry.landed", "time": yesterday + 20 * 3600},  # 21:00 yesterday, after this time of day
                   {"kind": "test.flaky", "time": yesterday + 3600},
                   {"kind": "gate.ended", "lane": "gate", "status": 0, "seconds": 600, "time": yesterday + 7200},
                   {"kind": "entry.landed", "time": today + 9 * 3600 + 60},
                   {"kind": "gate.ended", "lane": "gate", "status": 0, "seconds": 900, "time": today + 3600, "round": "r1",
                    "mac": "the second Mac (build-host)", "log": "/x/logs/r1-gate.log"},
                   {"kind": "gate.ended", "lane": "gate", "status": 3, "seconds": 300, "time": today + 7200, "mac": "this Mac (desk)"},
                   {"kind": "gate.ended", "lane": "spelling", "status": 0, "time": today + 7300}]
        self.assertEqual((history.totals(journal, now)["landed"], history.totals(journal, now - 86400)["landed"],
                          history.totals(journal, now - 86400)["flaky"]), (1, 1, 1))
        with patch.object(history.farm, "read", return_value=[{"ssh": "build-host", "name": "studio"}]):
            runs = history.gate_runs(journal, now)
        self.assertEqual(runs, [
            {"began": today + 2700, "ended": today + 3600, "lane": "gate", "status": 0, "mac": "studio", "round": "r1", "log": "r1-gate.log"},
            {"began": today + 6900, "ended": today + 7200, "lane": "gate", "status": 3, "mac": None, "round": None, "log": None}])
        days = history.landings(journal, now)
        # The journal began three days ago, so the days before it are left out.
        self.assertEqual([day["day"] for day in days], [history.midnight(today - back * 86400 + 3600) for back in (3, 2, 1, 0)])
        self.assertEqual((days[-2]["hours"][2], days[-2]["hours"][21], days[-1]["hours"][9], sum(days[0]["hours"])), (1, 1, 1, 0))
        self.assertEqual(history.gate_seconds(journal), [600, 900])
        self.assertEqual([history.mac_name(said, {"build-host": "studio"}) for said in
                          ("this Mac (desk)", "the second Mac (build-host)", "studio", "", None)], [None, "studio", "studio", None, None])
        self.assertEqual(set(history.data(self.one, {})) >= {"today", "yesterday", "gates", "landings", "gateSeconds"}, True)

    def test_the_queue_says_what_is_open_and_needs_someone_most_pressing_first(self):
        now = 10_000_000.0
        state = {"held": "for the owner: the gate timed out twice", "worker": False,
                 "waiting": [{"id": "qk3-b", "branch": "b", "submitted": now - 60}],
                 "round": {"gates": {"batch": {"lane": "gate", "stuck": "its log has not grown for 20m", "log": "/q/logs/r-gate.log"}}},
                 "recent": [{"id": "qk1-a", "branch": "a", "state": "failed", "settled": now - 600, "subject": "Add a", "why": "its gate failed",
                             "log": "/q/logs/r1-gate.log"},
                            {"id": "qk2-b", "branch": "b", "state": "conflict", "settled": now - 300},  # queued again as qk3-b
                            {"id": "qk0-c", "branch": "c", "state": "failed", "settled": now - 2 * queue.OPEN},  # too long ago
                            {"id": "qk4-d", "branch": "d", "state": "landed", "settled": now - 100}]}
        journal = [{"kind": "test.flaky", "time": now - 50, "test": "t.T.test_x", "issue": "https://x/issues/7"},
                   {"kind": "test.flaky", "time": now - 2 * queue.OPEN, "test": "t.T.test_old"}]
        found = queue.attention(state, journal, now)
        self.assertEqual([(one["level"], one["title"]) for one in found],
                         [("danger", "Held"), ("warning", "No worker"), ("warning", "Stuck"), ("warning", "Came back"), ("notice", "Flaky")])
        back = found[3]
        self.assertEqual((back["entry"], back["log"], back["text"], back["reason"], back["detail"]),
                         ("qk1-a", "r1-gate.log", "Add a", "Its gate failed.", "its gate failed"))
        # A long reason is cut to its first clause; the whole of it stays as the detail.
        self.assertEqual(queue.reason("the queue's lanes fail on this commit on top of origin/main: fix it and add it again. While gate ran"),
                         "The queue's lanes fail on this commit on top of origin/main.")
        self.assertEqual((found[2]["log"], found[4]["issue"], found[4]["text"]), ("r-gate.log", "https://x/issues/7", "t.T.test_x"))
        self.assertEqual(queue.attention({}, [], now), [])
        # A closure, which refuses every landing until it lapses (vocaro/queuekeeper#286), says when that is.
        closed = queue.attention({"closed": {"by": "a session", "why": "a move", "at": now - 60, "until": now + 3540}}, [], now)
        self.assertEqual([(one["level"], one["title"]) for one in closed], [("warning", "Closed")])
        self.assertIn("Closed to new entries by a session: a move. It reopens by itself at ", closed[0]["text"])

    def test_a_mac_short_of_memory_makes_the_farms_line_a_warning(self):
        self.read_farm()
        with farm.LOCK: farm.STATE["detail"] = {"here": {**LOADS["here"], "health": "this Mac is short of memory: ..."}}
        found = farm.summary(self.one)
        self.assertEqual(found["level"], "warning")
        self.assertIn("this Mac (desk) is short of memory", found["text"])

    def test_a_macs_load_is_what_its_pressure_and_screen_readings_say(self):
        self.assertEqual(farm.load_of({"cpuBusy": 91.4, "busyLimit": 75, "load": 12.08, "memory": 4}, {"state": "unlocked"}),
                         {"cpuBusy": 91.4, "busyLimit": 75, "load": 12.08, "memory": "critical", "screen": "unlocked",
                          "error": None, "health": None, "shortage": None, "hardware": {"model": None, "kind": "other"}})
        self.assertEqual(farm.load_of({"cpuBusy": None, "memory": None, "busyLimit": True}, {"state": "asleep"}),
                         {"cpuBusy": None, "busyLimit": None, "load": None, "memory": None, "screen": "unknown",
                          "error": None, "health": None, "shortage": None, "hardware": {"model": None, "kind": "other"}})
        self.assertEqual(farm.load_of({"error": "no slot directory"}, None)["error"], "no slot directory")
        self.assertEqual(farm.load_of("nonsense", [])["screen"], "unknown")

    def test_both_macs_are_asked_and_one_that_cannot_be_is_given_its_reason(self):
        pressure = {"cpuBusy": 10.0, "busyLimit": 75.0, "load": 1.0, "memory": 1}
        healthy = {"memoryBytes": 64 * GB, "swapUsedBytes": 0, "swapTotalBytes": 0, "largest": []}
        swapping = {**healthy, "swapUsedBytes": 31 * GB, "swapTotalBytes": 32 * GB,
                    "largest": [{"pid": 312, "name": "fseventsd", "residentBytes": 34 * GB, "seconds": 15 * 86400}]}
        with patch.object(farm.offload_host, "pressure", return_value=pressure), \
                patch.object(farm.offload_host, "host_health", return_value=healthy), \
                patch.object(farm.offload_host, "screen", return_value={"state": "unlocked"}), \
                farm_fixture.only("build"), \
                patch.object(farm.remote, "reading_there",
                             return_value=({**pressure, "cpuBusy": 80.0, "health": swapping}, 80.0)) as there, \
                patch.object(farm.remote, "screen_there", return_value={"state": "locked"}):
            found = READ_DETAIL()
            self.assertEqual((found["here"]["cpuBusy"], found["here"]["screen"]), (10.0, "unlocked"))
            self.assertEqual((found["build"]["cpuBusy"], found["build"]["screen"]), (80.0, "locked"))
            there.assert_called_with("build", health=True)
            self.assertIsNone(found["here"]["health"])
            self.assertIn("build is short of memory", found["build"]["health"])
            self.assertIn("`sudo killall fseventsd` on build", found["build"]["health"])
            shortage = found["build"]["shortage"]
            self.assertIsNone(shortage["swap"])  # its reading names no pressure level, as from an older toolkit
            self.assertEqual(shortage["largest"], shortage["free"])
            self.assertEqual(shortage["free"], [{"name": "fseventsd", "pid": 312, "residentBytes": 34 * GB, "seconds": 15 * 86400,
                                                 "share": 34 / 64, "outsized": True, "restarted": True}])
            self.assertIsNone(found["here"]["shortage"])
            with patch.object(farm.remote, "screen_there", side_effect=farm.remote.Unavailable("build is asleep")):
                self.assertEqual(READ_DETAIL()["build"]["error"], "build is asleep")
            with farm_fixture.only():
                self.assertEqual(list(READ_DETAIL()), ["here"])
            # Several Macs are each read, by their names in the farm, and a paused one is not.
            with farm_fixture.only(farm_fixture.mac("studio.local", "studio"), "mini",
                                   farm_fixture.mac("loaned", state="paused")):
                self.assertEqual(list(READ_DETAIL()), ["here", "studio", "mini"])
                there.assert_called_with("mini", health=True)

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
        path.write_text("a" * (logs.TAIL + logs.BACK + 10) + "END\n")  # a line too long to start the tail at its start
        first = logs.data(self.one, {"name": "gate.log"})
        self.assertEqual((first["offset"], first["next"], first["size"]), (logs.BACK + 14, path.stat().st_size, path.stat().st_size))
        self.assertTrue(first["text"].endswith("END\n"))
        with path.open("a") as log: log.write("more\n")
        second = logs.data(self.one, {"name": "gate.log", "offset": str(first["next"])})
        self.assertEqual((second["text"], second["offset"]), ("more\n", first["next"]))
        self.assertEqual(logs.data(self.one, {"name": "gate.log", "offset": "999999999"})["text"], "")
        self.assertEqual(len(logs.data(self.one, {"name": "gate.log", "offset": "0"})["text"]), min(logs.CHUNK, logs.TAIL + logs.BACK + 19))

    def test_a_logs_name_and_the_journal_say_its_round_commit_lane_and_how_it_ended(self):
        queue_ = mq.Queue(self.repo)
        round_ = "20261116-212941-9f43197f"
        passed = "  queuekeeper unittest: 1 tests (0 skipped) in 1 processes, 1 at a time, 0.9s: OK (command exit 0)\n"
        failed = "queuekeeper unittest: 1 tests (0 skipped) in 1 processes, 1 at a time, 0.9s: FAILED: core.test_x\n"
        for lane, text in (("gate", "x\n"), ("docs", "x\n"), ("rerun-1-2", passed), ("rerun-2-1", failed)):
            (queue_.logs / f"{round_}-{lane}.log").write_text(text)
        (queue_.logs / "other.log").write_text("x")
        events.emit(queue_.directory, "gate.began", lane="gate", round=round_)
        events.emit(queue_.directory, "gate.ended", lane="gate", round=round_, status=1, seconds=1488,
                    log=str(queue_.logs / f"{round_}-gate.log"), mac="this Mac (Trevors-MacBook-Pro)")
        events.emit(queue_.directory, "gate.began", lane="docs", round=round_)
        found = {log["name"]: log for log in logs.data(self.one, {})["logs"]}
        gate = found[f"{round_}-gate.log"]
        self.assertEqual({key: gate[key] for key in ("round", "commit", "lane", "status", "seconds", "mac")},
                         {"round": "20261116-212941", "commit": "9f43197f", "lane": "gate", "status": 1, "seconds": 1488,
                          "mac": None})
        self.assertNotIn("running", gate)
        self.assertTrue(found[f"{round_}-docs.log"]["running"])
        self.assertEqual([tuple(found[f"{round_}-{lane}.log"][key] for key in ("lane", "test", "attempt", "status"))
                          for lane in ("rerun-1-2", "rerun-2-1")], [("rerun", 1, 2, 0), ("rerun", 2, 1, 1)])
        self.assertNotIn("round", found["other.log"])
        events.emit(queue_.directory, "test.flaky", test="core.test_x.T.test_y", commit="9f43197f" + "0" * 32,
                    issue="https://x/issues/9")
        events.emit(queue_.directory, "test.flaky", test="core.test_x.T.test_z", commit="1" * 40)
        chosen = logs.data(self.one, {"name": f"{round_}-gate.log"})
        self.assertEqual(chosen["log"]["status"], 1)
        self.assertEqual(sorted(log["name"] for log in chosen["reruns"]), [f"{round_}-rerun-1-2.log", f"{round_}-rerun-2-1.log"])
        self.assertEqual(chosen["flaky"], {"core.test_x.T.test_y": "https://x/issues/9"})
        self.assertEqual(chosen["path"], str(queue_.logs / f"{round_}-gate.log"))

    def test_a_logs_index_says_where_its_failures_suites_heartbeats_and_summary_are(self):
        lines = ["progress: here", "== core.test_a.A[1]: OK (2 tests, 0.1s)", "test_one (core.test_a.A.test_one) ... ok",
                 "remote: heartbeat 2026-11-17T06:57:51Z: gate on build-host, 5m 00s in",
                 "== core.test_b.B[1]: FAILED (failures=1) (3 tests, 31.1s)",
                 "FAIL: test_two (core.test_b.B.test_two)", "-" * 70, "Traceback (most recent call last):",
                 '  File "x.py", line 3, in test_two', "AssertionError: Lists differ: [] != [1]", "",
                 "ERROR: test_three (core.test_b.B.test_three)", "Traceback (most recent call last):", "KeyError: 'x'",
                 "queuekeeper unittest: 5 tests (0 skipped) in 2 processes, 8 at a time, 1.5s: FAILED: core.test_b.B[1]"]
        start = lambda number: len("\n".join(lines[:number - 1])) + 1  # where line number starts
        path = mq.Queue(self.repo).logs / "gate.log"
        path.write_text("\n".join(lines) + "\n")
        found = logs.data(self.one, {"name": "gate.log", "find": "TEST_TWO"})
        index = found["index"]
        self.assertEqual((index["lines"], index["size"], found["line"], found["offset"]), (len(lines), path.stat().st_size, 1, 0))
        self.assertEqual(index["failures"], [
            {"test": "core.test_b.B.test_two", "line": 6, "offset": start(6), "reason": "AssertionError: Lists differ: [] != [1]"},
            {"test": "core.test_b.B.test_three", "line": 12, "offset": start(12), "reason": "KeyError: 'x'"}])
        self.assertEqual(index["marks"], [[2, "pass"], [4, "beat"], [5, "failed"], [6, "fail"], [12, "fail"], [15, "summary"]])
        self.assertEqual(index["summary"], lines[-1])
        self.assertEqual(found["found"], {"text": "TEST_TWO", "lines": [[6, start(6)], [9, start(9)]], "count": 2})
        self.assertNotIn("index", logs.data(self.one, {"name": "gate.log", "known": str(index["size"])}))
        self.assertEqual(logs.data(self.one, {"name": "gate.log", "line": "12"})["line"], 1)
        before = logs.data(self.one, {"name": "gate.log", "before": str(start(12))})
        self.assertEqual((before["offset"], before["next"], before["text"]), (0, start(12), "\n".join(lines[:11]) + "\n"))
        with self.assertRaises(LookupError): logs.data(self.one, {"name": "gate.log", "line": "x"})

    def test_a_long_log_is_read_by_whole_lines_and_from_near_a_line_asked_for(self):
        path = mq.Queue(self.repo).logs / "gate.log"
        path.write_text("".join(f"line {number:06}\n" for number in range(1, 60001)))  # 12 bytes a line
        tail = logs.data(self.one, {"name": "gate.log"})
        self.assertTrue(tail["text"].startswith("line ") and tail["text"].endswith("line 060000\n"))
        self.assertEqual(tail["line"], int(tail["text"][5:11]))
        near = logs.data(self.one, {"name": "gate.log", "line": "30000"})
        self.assertEqual(near["text"][:12], f"line {near['line']:06}\n")
        self.assertTrue(30000 - logs.BACK // 12 - 1 <= near["line"] < 30000)
        self.assertTrue(near["text"].endswith("\n") and len(near["text"]) < logs.CHUNK)
        after = logs.data(self.one, {"name": "gate.log", "offset": str(near["next"])})
        self.assertEqual(after["text"][:12], f"line {near['line'] + len(near['text']) // 12:06}\n")
        at = logs.data(self.one, {"name": "gate.log", "at": str(12 * 999 + 5)})
        self.assertEqual(at["text"][:12], f"line {at['line']:06}\n")
        self.assertTrue(1000 - logs.BACK // 12 - 1 <= at["line"] < 1000)

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

    def test_trends_give_the_days_totals_and_each_days_measures(self):
        # Every event, and the moment the panel reads them, at a time of the test's own: today's noon and the seconds
        # before it, so that neither the wall clock's steps nor midnight can move one out of its day (#222).
        directory, now = mq.Queue(self.repo).directory, history.midnight(time.time()) + 12 * 3600
        def at(before, kind, **fields):
            with patch.object(events.time, "time", return_value=now - before):
                # One the journal did not take fails here, and not as a total that is short.
                self.assertIsNotNone(events.emit(directory, kind, **fields), kind)
        at(60, "round.began", round="r")
        for seconds in (600, 900, 1200): at(50, "gate.ended", lane="gate", status=0, seconds=seconds)
        at(40, "gate.ended", lane="gate", status=1, seconds=5)
        at(40, "gate.ended", lane="spelling", status=0, seconds=3)
        at(30, "test.flaky", test="test_x", commit="c" * 40)
        at(20, "entry.failed", entry="a", why="its gate failed")
        at(10, "entry.landed", entry="b", subject="Add b", landedAs="0123456789abcdef")
        at(3 * 86400, "entry.landed", entry="old", landedAs="f" * 40)
        at(3 * 86400, "gate.ended", lane="gate", status=0, seconds=1800)
        with patch.object(history.time, "time", return_value=now): found = history.data(self.one, {})
        self.assertEqual(found["today"], {"landed": 1, "cameBack": 1, "failed": 1, "conflict": 0, "removed": 0, "flaky": 1,
                                          "rounds": 1, "medianGateSeconds": 900})
        now = time.time()
        settled = history.totals([{"time": now, "kind": kind} for kind in
                                  ("entry.landed", "entry.conflict", "entry.conflict", "entry.removed", "entry.failed")], now)
        self.assertEqual([settled[key] for key in ("landed", "failed", "conflict", "removed", "cameBack")], [1, 1, 2, 1, 3])
        # Each day from the journal's first, today last: what landed and came back, flaky tests, and each passing
        # gate as a share of the median of every passing gate over the period (1050 s of 600, 900, 1200 and 1800).
        days = found["daily"]
        self.assertEqual(len(days), 4)
        self.assertEqual([(one["landed"], one["cameBack"], one["flaky"]) for one in days], [(1, 0, 0), (0, 0, 0), (0, 0, 0), (1, 1, 1)])
        self.assertEqual((days[0]["medianGateSeconds"], days[0]["gateShares"]), (1800, [round(1800 / 1050, 2)]))
        self.assertEqual((days[-1]["medianGateSeconds"], days[-1]["gateShares"]), (900, [round(seconds / 1050, 2) for seconds in (600, 900, 1200)]))
        self.assertEqual(history.summary(self.one)["text"], "old")

    def test_trends_give_each_lanes_timing_baseline_beside_its_recent_passing_runs(self):
        config = json.loads((self.repo / "queuekeeper.json").read_text())
        config["lanes"]["lint"] = {"command": ["true"], "timing": {"fail": "+50%", "minSamples": 5}}
        config["lanes"]["docs"] = {"command": ["true"], "timing": {"fail": "+2m"}}
        (self.repo / "queuekeeper.json").write_text(json.dumps(config))
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"HEAVY_SLOT_DIR": tmp, lane_timing.STALE_VARIABLE: ""}):
            self.assertEqual([one["baseline"] for one in history.baselines(self.repo)], [None, None, None])
            for index, seconds in enumerate((100, 110, 120, 130, 900)): lane_timing.record("fixture", "gate", seconds, at=10 + index)
            lane_timing.mark_stale("fixture", ["gate"], "split the suite", at=20)
            for index, seconds in enumerate((60, 62, 58, 61, 70)): lane_timing.record("fixture", "gate", seconds, at=30 + index)
            # More runs than the baseline counts: the oldest are drawn but left out of it for age (#280).
            for index in range(lane_timing.RECENT + 3): lane_timing.record("fixture", "lint", 10, at=40 + index)
            for index in range(5): lane_timing.record("fixture", "docs", 30, at=40 + index)
            lane_timing.record("other", "gate", 999, at=50)
            found = {one["lane"]: one for one in history.data(self.one, {})["baselines"]}
        self.assertEqual(list(found), ["docs", "gate", "lint"])
        gate, lint, docs = found["gate"], found["lint"], found["docs"]
        # Every recent run is drawn, those before the stale mark among them; the baseline is only those after it.
        self.assertEqual([seconds for _, seconds in gate["runs"]], [100, 110, 120, 130, 900, 60, 62, 58, 61, 70])
        self.assertEqual((gate["baseline"]["p90"], gate["baseline"]["samples"], gate["staleSince"]), (70, 5, 20))
        self.assertEqual((gate["fail"], gate["failSeconds"]), (None, None))
        # The page fades each run the baseline leaves out: before the stale mark, or older than the newest RECENT.
        self.assertEqual(gate["countedFrom"], 30)
        self.assertEqual((len(lint["runs"]), lint["baseline"]["samples"], lint["countedFrom"]),
                         (lane_timing.RECENT + 3, lane_timing.RECENT, 43))
        # A fail threshold is drawn once the baseline has the lane's minSamples runs, and said as waiting before then.
        self.assertEqual((lint["fail"], lint["failSeconds"], lint["minSamples"]), ("+50%", 15.0, 5))
        self.assertEqual((docs["fail"], docs["failSeconds"], docs["minSamples"]), ("+2m", None, lane_timing.MIN_SAMPLES))
        combined = history.combine([("alpha", {**history.data(self.one, {}), "baselines": [gate]}, None)])
        self.assertEqual([(one["repository"], one["lane"]) for one in combined["baselines"]], [("alpha", "gate")])
        self.assertEqual(combined["fewest"], lane_timing.FEWEST)
        (self.repo / "queuekeeper.json").write_text("{")
        self.assertEqual(history.baselines(self.repo), [], "a configuration that cannot be read leaves no baselines")

    def test_activity_lists_each_round_with_how_its_entries_ended_and_its_lanes(self):
        directory, start = mq.Queue(self.repo).directory, time.time() - 600
        def at(offset, kind, **fields):
            with patch.object(events.time, "time", return_value=start + offset): events.emit(directory, kind, **fields)
        at(0, "entry.queued", entry="e1", subject="Add one", branch="agent/one")
        at(1, "entry.queued", entry="e2", subject="Add two", branch="agent/two")
        at(10, "round.began", round="r1", entries=["e1", "e2"], head="h" * 40)
        at(11, "gate.began", lane="spelling", round="r1", role="batch")
        at(14, "gate.ended", lane="spelling", round="r1", role="batch", status=0, seconds=3, log="/q/logs/r1-spelling.log")
        at(15, "gate.began", lane="gate", round="r1", role="batch")
        at(300, "gate.ended", lane="gate", round="r1", role="batch", status=1, seconds=285, log="/q/logs/r1-gate.log")
        at(301, "test.flaky", test="t.T.test_x", commit="h" * 40, issue="https://x/issues/7")
        at(302, "entry.landed", entry="e1", landedAs="a" * 40)
        at(303, "queue.held", why="for a session: a test")
        at(304, "queue.resumed", was="for a session: a test")
        at(310, "round.began", round="r2", entries=["e2"], head="k" * 40)
        at(311, "gate.began", lane="gate", round="r2", role="batch")
        feed = activity.data(self.one, {})["feed"]
        self.assertEqual([one.get("round") or one["text"] for one in feed],
                         ["r2", "The queue resumed after it was held for a session: a test", "The queue was held for a session: a test", "r1"])
        r2, r1 = feed[0], feed[3]
        self.assertEqual([(entry["id"], entry["state"]) for entry in r1["entries"]], [("e1", "landed"), ("e2", "retested")])
        self.assertEqual((r1["entries"][0]["subject"], r1["entries"][0]["branch"], r1["entries"][0]["landedAs"]), ("Add one", "agent/one", "a" * 40))
        self.assertEqual([(lane["lane"], lane["status"], lane["log"]) for lane in r1["lanes"]], [("spelling", 0, "r1-spelling.log"), ("gate", 1, "r1-gate.log")])
        self.assertEqual(r1["flaky"], [{"test": "t.T.test_x", "issue": "https://x/issues/7"}])
        self.assertEqual([(entry["id"], entry["state"]) for entry in r2["entries"]], [("e2", "testing")])
        self.assertEqual([(lane["lane"], lane["status"], lane["log"]) for lane in r2["lanes"]], [("gate", None, "r2-gate.log")])
        # A settled entry that came back says why, in the round it was tested in last.
        at(400, "entry.failed", entry="e2", why="its gate failed")
        feed = activity.data(self.one, {})["feed"]
        self.assertEqual([feed[0]["entries"][0][key] for key in ("state", "why", "reason")], ["back", "its gate failed", "Its gate failed."])

    def test_about_names_the_version_and_license_and_each_third_party_notice(self):
        found = panels.load("about").data(self.one, {})
        self.assertEqual((found["version"], found["license"]["name"]), (queuekeeper_version, "Apache License 2.0"))
        # The page's notices are the works THIRD_PARTY_NOTICES.md describes, one for each of its sections.
        notices = Path(panels.__file__).resolve().parents[3] / "THIRD_PARTY_NOTICES.md"
        sections = [line[3:].strip() for line in notices.read_text().splitlines() if line.startswith("## ")]
        self.assertEqual([one["name"] for one in found["notices"]], sections)
        for one in found["notices"]:
            self.assertTrue(one["url"].startswith("https://") and one["licenseUrl"].startswith("https://"), one["name"])

    def test_activity_shows_the_last_rounds_and_more_when_asked(self):
        directory = mq.Queue(self.repo).directory
        for number in range(activity.ROUNDS + 5): events.emit(directory, "round.began", round=f"r{number}", entries=[])
        found = activity.data(self.one, {})
        self.assertEqual((len(found["feed"]), found["total"], found["feed"][0]["round"]), (activity.ROUNDS, activity.ROUNDS + 5, f"r{activity.ROUNDS + 4}"))
        self.assertEqual(len(activity.data(self.one, {"rounds": str(activity.ROUNDS + 5)})["feed"]), activity.ROUNDS + 5)
        with self.assertRaises(LookupError): activity.data(self.one, {"rounds": "x"})
        self.assertIsNone(history.summary(self.one))

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


def queue_part(name, waiting=(), gates=None, recent=(), health_=None, counts=None, macs=()):
    """What queue.data answers for one repository, as queue.combine reads it."""
    round_ = {"name": f"{name}-round", "entries": [one["id"] for one in waiting if one.get("state") == "testing"],
              "bisect": None, "gates": gates} if gates else None
    return {"overview": {"level": "idle", "headline": "Idle", "detail": "Nothing is waiting.", "lines": [],
                         "counts": counts or {"waiting": len(waiting), "landed": 0, "cameBack": 0},
                         "trend": {"waiting": [len(waiting)] * 24, "landed": [0] * 24, "cameBack": [0] * 24, "medianGateSeconds": 300}},
            "health": health_ or {"verdict": "quiet", "level": "idle", "label": "All quiet", "sentence": "Nothing is waiting.",
                                  "signs": [], "forecast": None, "findings": []},
            "worker": True, "held": None, "round": round_, "waiting": list(waiting), "recent": list(recent),
            "ungated": [], "flow": {"agents": [], "macs": [{"name": "this Mac", "role": "here", "state": "observed",
                                                            "used": 1, "most": 4, "gates": list(macs)}], "back": None}}


def verdict_of(verdict, level, label, *signs, sentence=""):
    return {"verdict": verdict, "level": level, "label": label, "sentence": sentence, "signs": list(signs),
            "forecast": None, "findings": []}


class EveryRepository(Fixture):
    """The page's first view, every repository at once: each panel's `everywhere`, combined from each repository's
    own answer."""

    def test_every_queue_is_combined_and_each_entry_and_gate_names_its_repository(self):
        now = 1_800_000_000.0
        alpha = queue_part("alpha", waiting=[
            {"id": "a1", "state": "testing", "branch": "claude/one", "submitted": 10.0},
            {"id": "a2", "state": "queued", "branch": "claude/two", "submitted": 30.0}],
            gates=[{"role": "batch", "lane": "gate", "began": 5.0, "entries": ["a1"]}], macs=["batch"],
            recent=[{"id": "a0", "state": "failed", "branch": "zed/x", "subject": "Z", "settled": now - 60}],
            counts={"waiting": 2, "landed": 3, "cameBack": 1})
        beta = queue_part("beta", waiting=[{"id": "b1", "state": "queued", "branch": "codex/x", "submitted": 20.0}],
                          counts={"waiting": 1, "landed": 4, "cameBack": 0})
        found = queue.combine([("alpha", alpha, None), ("beta", beta, None), ("gamma", None, "no queue")], now)
        self.assertEqual(found["counts"], {"waiting": 3, "landed": 7, "cameBack": 1, "testing": 1})
        self.assertEqual(found["trend"]["waiting"], [3] * 24)
        self.assertEqual([(one["id"], one["repository"]) for one in found["active"]], [("a1", "alpha")])
        # Up next is every repository's, oldest first.
        self.assertEqual([(one["id"], one["repository"]) for one in found["pending"]], [("b1", "beta"), ("a2", "alpha")])
        round_, = found["rounds"]
        self.assertEqual((round_["repository"], round_["gates"][0]["repository"], round_["medianGateSeconds"]), ("alpha", "alpha", 300))
        self.assertEqual(found["flow"]["queues"], [
            {"name": "alpha", "waiting": 1, "testing": 1, "held": False, "worker": True},
            {"name": "beta", "waiting": 1, "testing": 0, "held": False, "worker": True}])
        self.assertEqual(found["flow"]["agents"], [{"name": "Claude", "waiting": 1, "testing": 1, "queues": ["alpha"]},
                                                   {"name": "Codex", "waiting": 1, "testing": 0, "queues": ["beta"]}])
        self.assertEqual(found["flow"]["macs"][0]["gates"], [{"repository": "alpha", "role": "batch"}])
        self.assertEqual((found["flow"]["back"]["id"], found["flow"]["back"]["repository"]), ("a0", "alpha"))
        rows = {row["name"]: row for row in found["repositories"]}
        self.assertEqual((rows["alpha"]["testing"], rows["beta"]["counts"]["landed"], rows["gamma"]["error"]), (1, 4, "no queue"))
        self.assertEqual(found["lines"], [{"title": "Cannot read", "text": "no queue", "level": "warning", "panel": "queue",
                                           "repository": "gamma"}])

    def test_the_verdict_names_each_repository_in_trouble_and_says_the_macs_once(self):
        from queuekeeper import health
        macs = health.sign("macs", health.WARNING, "this Mac's memory pressure is at warning")
        alpha = queue_part("alpha", health_=verdict_of("slow", health.WARNING, "Slower than usual",
            health.sign("progress", health.WARNING, "the gate has waited 20m for admission", "Waiting 20m for a Mac"), macs))
        beta = queue_part("beta", health_=verdict_of("slow", health.WARNING, "Slower than usual",
            health.sign("progress", health.OK, "testing 1 entry", "Testing 1"), macs))
        gamma = queue_part("gamma")
        found = queue.verdict([("alpha", alpha, None), ("beta", beta, None), ("gamma", gamma, None)])
        self.assertEqual((found["level"], found["label"]), ("warning", "Slower than usual"))
        self.assertEqual(found["sentence"], "1 of 3 needs a look: alpha, waiting 20m for a Mac. "
                                            "This Mac's memory pressure is at warning. The other 2 are fine.")
        progress, shared = found["signs"]
        self.assertEqual((progress["short"], shared["short"]), ("alpha: Waiting 20m for a Mac", macs["short"]))
        quiet = queue.verdict([("alpha", gamma, None), ("beta", gamma, None)])
        self.assertEqual((quiet["label"], quiet["sentence"]), ("All quiet", "Nothing is waiting in any of the 2 repositories."))
        unread = queue.verdict([("alpha", gamma, None), ("beta", None, "no queue")])
        self.assertEqual((unread["label"], unread["sentence"]), ("Can't tell", "beta cannot be read: no queue. The other one is fine."))

    def test_every_history_is_summed_and_merged_newest_first(self):
        def day(landed): return {"landed": landed, "cameBack": 1, "failed": 1, "conflict": 0, "removed": 0, "flaky": 0,
                                 "rounds": 2, "medianGateSeconds": 300}
        def part(at, landed): return {"today": day(landed), "yesterday": day(1), "gateSeconds": [300, 310],
                                      "gates": [{"began": at, "ended": at + 5, "lane": "gate", "status": 0, "mac": None, "round": None, "log": None}],
                                      "landings": [{"day": 100.0, "hours": [landed] + [0] * 23}],
                                      "daily": [{"day": 100.0, "landed": landed, "cameBack": 1, "flaky": 0, "medianGateSeconds": 300,
                                                 "gateShares": [1.0, at / 10]}]}
        found = history.combine([("alpha", part(10.0, 2), None), ("beta", part(20.0, 3), None), ("gamma", None, "no queue")])
        self.assertEqual((found["today"]["landed"], found["today"]["rounds"], found["today"]["medianGateSeconds"]), (5, 4, None))
        self.assertEqual(found["landings"], [{"day": 100.0, "hours": [5] + [0] * 23}])
        # Each day's counts are summed and every repository's gate shares kept; seconds are no repository's but its own.
        self.assertEqual(found["daily"], [{"day": 100.0, "landed": 5, "cameBack": 2, "flaky": 0, "medianGateSeconds": None,
                                           "gateShares": [1.0, 1.0, 1.0, 2.0]}])
        self.assertEqual([one["repository"] for one in found["gates"]], ["alpha", "beta"])
        self.assertEqual([row.get("error") for row in found["repositories"]], [None, None, "no queue"])
        feeds = activity.combine([("alpha", {"feed": [{"kind": "round", "time": 10.0}, {"kind": "notice", "time": 15.0}], "total": 3}, None),
                                  ("beta", {"feed": [{"kind": "round", "time": 20.0}], "total": 1}, None), ("gamma", None, "no queue")], most=2)
        self.assertEqual([(one["repository"], one["time"]) for one in feeds["feed"]], [("beta", 20.0), ("alpha", 15.0), ("alpha", 10.0)])
        self.assertEqual((feeds["total"], feeds["unread"]), (4, [{"name": "gamma", "error": "no queue"}]))

    def test_the_real_panels_answer_for_every_repository(self):
        (mq.Queue(self.repo).logs / "r-gate.log").write_text("hello\n")
        found = {name: panels.load(name).everywhere({"project": self.one}, {}) for name in ("queue", "farm", "history", "activity", "logs", "about")}
        self.assertEqual([row["name"] for row in found["queue"]["repositories"]], ["project"])
        self.assertEqual([row["name"] for row in found["history"]["repositories"]], ["project"])
        self.assertEqual({log["repository"] for log in found["logs"]["logs"]}, {"project"})
        chunk = panels.load("logs").everywhere({"project": self.one}, {"name": "r-gate.log", "repository": "project"})
        self.assertEqual((chunk["text"], chunk["repository"], chunk["log"]["repository"]), ("hello\n", "project", "project"))
        with self.assertRaises(LookupError): panels.load("logs").everywhere({"project": self.one}, {"name": "r-gate.log"})
        self.assertEqual({panel["name"]: panel["everywhere"] for panel in panels.listed()},
                         {"queue": True, "farm": True, "history": True, "activity": True, "logs": True, "about": True})


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
        for path in ("/", "/r", "/all", "/all/history", "/r/project/queue", "/r/project/logs?name=worker.log"):
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
        self.assertEqual([panel["name"] for panel in self.api("/panels")[1]], ["queue", "farm", "history", "activity", "logs", "about"])
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

    def test_each_tab_is_answered_for_every_repository_at_once(self):
        (mq.Queue(self.repo).logs / "gate.log").write_text("hello\n")
        for name in ("queue", "farm", "history", "logs"):
            status, found = self.api(f"/all/panel/{name}")
            self.assertEqual(status, 200, (name, found))
        status, found = self.api("/all/panel/logs?name=gate.log&repository=project&offset=2")
        self.assertEqual((status, found["text"], found["repository"]), (200, "llo\n", "project"))
        for path in ("/all/panel/home", "/all/panel/nope", "/all/panel/logs?name=gate.log", "/all/panel/logs?name=gate.log&repository=nope"):
            self.assertEqual(self.api(path)[0], 404, path)

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
        for kept in (plugins.COMMANDS, plugins.LOADED, plugins.AFTER_EVENT):
            before = kept.copy()
            self.addCleanup(lambda kept=kept, before=before: (kept.clear(), kept.update(before) if isinstance(kept, dict)
                                                              else kept.extend(before)))
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop(visibility.SWITCH, None)
        plugins.COMMANDS.pop("serve", None)

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

    def test_dashboard_restarts_when_head_moves_before_its_watcher_starts(self):
        before, after = "a" * 40, "b" * 40
        revision = before

        def registered(*_args):
            nonlocal revision
            revision = after

        with patch.object(server, "make") as make, patch.object(server.signal, "signal"), \
                patch.object(server.threading, "Thread") as thread, patch("sys.stdout", io.StringIO()) as out, \
                patch.object(server.restartable, "code_checkout", return_value=self.repo), \
                patch.object(server.restartable, "head", side_effect=lambda _checkout: revision), \
                patch.object(server.restartable, "register", side_effect=registered), \
                patch.object(server.restartable, "unregister"), patch.object(server.restartable, "restart") as restart:
            make.return_value.server_address = ("127.0.0.1", 8750)
            self.assertEqual(server.main([], project=self.repo), 0)
            self.assertEqual(make.return_value.commit, before)
            watch, = (call.kwargs["target"] for call in thread.call_args_list
                      if call.kwargs.get("name") == "restartable-watch")
            with patch.object(threading.Event, "wait", side_effect=[False, True]), \
                    patch.object(server.restartable.time, "sleep"):
                watch()
            restart.assert_called_once_with()
            self.assertIn(f"moved from {before[:12]} to {after[:12]}", out.getvalue())

    def serving(self, made):
        """made, serving on a thread of its own until the test ends or it is shut down."""
        threading.Thread(target=made.serve_forever, daemon=True).start()
        def ended():
            made.shutdown()
            made.server_close()
        self.addCleanup(ended)
        return made

    def test_a_port_another_dashboard_serves_is_taken_over_unless_told_not_to(self):
        visibility.switch("dashboard", True)
        taken = server.make([self.repo], port=0)
        port = taken.server_address[1]
        threading.Thread(target=taken.serve_forever, daemon=True).start()
        self.addCleanup(taken.server_close)
        stopped = []
        def kill(pid, number):
            stopped.append((pid, number))
            taken.shutdown()
            taken.server_close()
        with patch.object(server, "listeners", lambda port: [4242]), patch.object(server.os, "kill", kill):
            status, _, errors = self.command("--port", str(port), "--no-replace")
            self.assertEqual((status, stopped), (2, []))
            self.assertIn(f"a dashboard already serves http://localhost:{port}/; without --no-replace", errors)
            made = []
            with patch.object(server.http.server.ThreadingHTTPServer, "serve_forever", lambda served: made.append(served)):
                status, printed, errors = self.command("--port", str(port))
        self.assertEqual((status, errors), (0, ""))
        self.assertEqual(stopped, [(4242, server.signal.SIGTERM)])
        self.assertIn(f"stopping the dashboard already at http://localhost:{port}/ (PID 4242)", printed)
        self.assertEqual(made[0].server_address[1], port)
        made[0].server_close()

    def test_a_port_in_use_and_a_folder_that_is_no_checkout_are_refused(self):
        visibility.switch("dashboard", True)
        taken = self.serving(server.http.server.ThreadingHTTPServer(("127.0.0.1", 0), http.server.BaseHTTPRequestHandler))
        with patch.object(server.os, "kill") as kill:
            status, _, errors = self.command("--port", str(taken.server_address[1]))
        self.assertEqual(status, 2)
        kill.assert_not_called()
        self.assertIn("cannot serve on 127.0.0.1", errors)
        self.assertIn("--port 0 serves on any free port", errors)
        status, _, errors = self.command("--port", "0", str(self.base / "nowhere"))
        self.assertEqual(status, 2)
        self.assertIn("is no checkout of a repository", errors)


if __name__ == "__main__":
    unittest.main()
