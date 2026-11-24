"""The notify feature (queuekeeper_notify): its rules, its channels, its delivery and its command.

No test here posts a real banner: every one that turns the feature on turns the banner channel off, or replaces
what the channel runs."""
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
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src/python"))
import host_isolation  # noqa: F401 -- first, as every test module
from queuekeeper import cli, events, plugins, merge_queue as mq, visibility
import queuekeeper_notify as notify
from queuekeeper_notify import channels, check, commands, deliver, rules
from queuekeeper_notify.channels import banner, command, imessage, webhook
from test_support import queue as test_merge_queue

DEFAULT = rules.chosen({})
NOTICE = {"rule": "came-back", "level": rules.URGENT, "title": "Entry came back", "body": "Add a: its gate failed",
          "repository": "project", "event": {"kind": "entry.failed", "entry": "a"}}


class Settings:
    """The owner's settings for a test: the process's own file, which host_isolation keeps private, so that a
    delivery the test starts as a process of its own reads the same one. The banner channel is off in it."""

    def configure(self, rules_=None, **configured):
        path = visibility.settings_path()
        self.addCleanup(lambda: [found.unlink() for found in (path, deliver.log_path()) if found.exists()])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"features": {"notify": True}, "notify": {
            "rules": rules_ or {}, "channels": {"banner": {"enabled": False}, **configured}}}))
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop(visibility.SWITCH, None)

    def keep_plugins(self):
        for kept in (plugins.COMMANDS, plugins.LOADED, plugins.AFTER_EVENT, plugins.HOST_COMMAND):
            before = kept.copy()
            self.addCleanup(lambda kept=kept, before=before: (kept.clear(), kept.update(before) if isinstance(kept, dict)
                                                              else kept.extend(before)))

    def recorder(self, base):
        """A command channel's argv that appends each notice it is given to a file, and that file."""
        script, kept = base / "record.py", base / "notices.jsonl"
        script.write_text("import sys\nopen(sys.argv[1], 'a').write(sys.stdin.read() + '\\n')\n")
        return [sys.executable, str(script), str(kept)], kept


class Rules(unittest.TestCase):
    def test_the_defaults_tell_of_what_needs_the_owner_and_not_of_every_landing(self):
        self.assertEqual(DEFAULT, {"held-owner", "came-back", "stuck", "flaky", "ungated"})

    def test_the_settings_turn_one_rule_on_or_off_and_ignore_what_is_no_switch(self):
        self.assertEqual(rules.chosen({"rules": {"landed": True, "flaky": False, "ungated": "no", "unknown": True}}),
                         {"held-owner", "came-back", "stuck", "ungated", "landed"})
        self.assertEqual(rules.chosen({"rules": []}), DEFAULT)

    def test_each_kind_goes_to_its_rule_and_the_rest_to_none(self):
        for event, expected in (
                ({"kind": "queue.held", "for": "owner"}, ["held-owner"]),
                ({"kind": "queue.held", "for": "session"}, ["held-session"]),
                ({"kind": "queue.held"}, ["held-session"]),
                ({"kind": "entry.failed"}, ["came-back"]), ({"kind": "entry.conflict"}, ["came-back"]),
                ({"kind": "test.flaky"}, ["flaky"]), ({"kind": "gate.stuck"}, ["stuck"]),
                ({"kind": "entry.landed", "ungated": {"why": "w"}}, ["ungated", "landed"]),
                ({"kind": "entry.landed"}, ["landed"]), ({"kind": "queue.resumed"}, ["resumed"]),
                ({"kind": "gate.began"}, []), ({"kind": "later.kind"}, []), ({}, [])):
            with self.subTest(event=event):
                self.assertEqual(rules.candidates(event), expected)

    def test_an_ungated_landing_is_told_as_a_landing_when_only_that_rule_is_on(self):
        event = {"kind": "entry.landed", "subject": "Fix", "landedAs": "0123456789abcdef", "ungated": {"why": "urgent"}}
        self.assertEqual(rules.notice(event, DEFAULT, "p")["title"], "Landed without the gate")
        self.assertEqual(rules.notice(event, DEFAULT, "p")["body"], "Fix: urgent")
        landed = rules.notice(event, {"landed"}, "p")
        self.assertEqual((landed["title"], landed["body"]), ("Landed", "Fix as 0123456789ab"))
        self.assertIsNone(rules.notice(event, set(), "p"))

    def test_a_notice_says_what_happened_and_carries_its_event(self):
        on = set(rules.RULES)
        for event, title, body, level in (
                ({"kind": "queue.held", "for": "owner", "why": "for the owner: a refused key"}, "Queue held for you",
                 "for the owner: a refused key", rules.URGENT),
                ({"kind": "queue.held", "for": "session", "why": "w"}, "Queue held for an agent", "w", rules.NORMAL),
                ({"kind": "entry.failed", "subject": "Add a", "why": "its gate failed"}, "Entry came back",
                 "Add a: its gate failed", rules.URGENT),
                ({"kind": "entry.conflict", "entry": "e1", "why": "conflicts in a.txt"}, "Entry has a conflict",
                 "e1: conflicts in a.txt", rules.URGENT),
                ({"kind": "test.flaky", "test": "test_x", "landed": True}, "Flaky test",
                 "test_x failed its gate and passed alone, so its batch landed", rules.NORMAL),
                ({"kind": "test.flaky", "test": "test_x", "landed": False}, "Flaky test",
                 "test_x failed its gate and passed alone, and its batch was bisected", rules.NORMAL),
                ({"kind": "gate.stuck", "lane": "gate", "why": "stuck: the gate has waited 15m01s"}, "Gate is stuck",
                 "gate is stuck: the gate has waited 15m01s", rules.URGENT),
                ({"kind": "queue.resumed"}, "Queue resumed", "Its hold was cleared", rules.NORMAL)):
            with self.subTest(title=title):
                notice_ = rules.notice(event, on, "project")
                self.assertEqual({key: notice_[key] for key in ("title", "body", "level", "repository", "event")},
                                 {"title": title, "body": body, "level": level, "repository": "project", "event": event})

    def test_a_long_body_is_cut_and_put_on_one_line(self):
        body = rules.notice({"kind": "queue.held", "for": "owner", "why": "a cause\n" * 200}, DEFAULT, "p")["body"]
        self.assertEqual(len(body), rules.BODY_LIMIT)
        self.assertNotIn("\n", body)
        self.assertTrue(body.endswith("…"))


class Channels(Settings, unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.base, True)

    def test_the_four_channels_are_found_by_name(self):
        self.assertEqual(channels.available(), ["banner", "command", "imessage", "webhook"])

    def test_imessage_is_off_until_an_address_is_set_and_switches_apart_from_the_banner(self):
        for config in ({}, {"to": ""}, {"to": "  "}, {"to": 3}):
            with self.subTest(config=config): self.assertIn("no address is set", imessage.ready(config))
        with patch.object(imessage.sys, "platform", "linux"):
            self.assertEqual(imessage.ready({"to": "me@example.invalid"}), "iMessage is for macOS")
        both = {"channels": {"imessage": {"to": "me@example.invalid"}}}
        with patch.object(sys, "platform", "darwin"), patch.object(imessage.shutil, "which", return_value="/x"), \
                patch.object(banner.shutil, "which", return_value="/x"):
            self.assertEqual([name for name, _, _ in channels.on(both)], ["banner", "imessage"])
            both["channels"]["banner"] = {"enabled": False}
            self.assertEqual([name for name, _, _ in channels.on(both)], ["imessage"])
            both["channels"] = {"banner": {}, "imessage": {"to": "me@example.invalid", "enabled": False}}
            self.assertEqual([name for name, _, _ in channels.on(both)], ["banner"])

    def test_imessage_passes_the_address_and_text_as_arguments_and_never_as_script(self):
        ran = []
        def run(command_, **how):
            ran.append((command_, how))
            return subprocess.CompletedProcess(command_, 0, "", "")
        hostile = {**NOTICE, "title": 'a" & (do shell script "x") & "'}
        with patch.object(imessage.subprocess, "run", run):
            imessage.send(hostile, {"to": " me@example.invalid "})
        argv, how = ran[0]
        self.assertEqual(argv[0], imessage.OSASCRIPT)
        self.assertEqual(argv[-2:], ["me@example.invalid", f"project: {hostile['title']}. {NOTICE['body']}"])
        self.assertNotIn("do shell script", " ".join(argv[:-2]))
        self.assertIn("service type = iMessage", " ".join(argv[:-2]))
        self.assertEqual((how["timeout"], how["stdin"]), (imessage.LIMIT, subprocess.DEVNULL))

    def test_imessage_refused_control_of_messages_is_definite_and_other_errors_are_uncertain(self):
        config = {"to": "me@example.invalid"}
        refused = subprocess.CompletedProcess([], 1, "", "execution error: Not authorized to send Apple events "
                                              "to Messages. (-1743)\n")
        with patch.object(imessage.subprocess, "run", return_value=refused), \
                self.assertRaisesRegex(channels.NotDelivered, "Automation"):
            imessage.send(NOTICE, config)
        failed = subprocess.CompletedProcess([], 1, "", "execution error: Messages got an error (-1708)\n")
        with patch.object(imessage.subprocess, "run", return_value=failed), self.assertRaisesRegex(RuntimeError, "-1708"):
            imessage.send(NOTICE, config)
        with patch.object(imessage.subprocess, "run", side_effect=FileNotFoundError("absent")), \
                self.assertRaises(channels.NotDelivered):
            imessage.send(NOTICE, config)

    def test_a_channel_is_on_when_it_is_ready_and_not_turned_off(self):
        argv, _ = self.recorder(self.base)
        found = {"channels": {"banner": {"enabled": False}, "command": {"argv": argv}, "webhook": {"url": "ftp://x"}}}
        self.assertEqual([name for name, _, _ in channels.on(found)], ["command"])
        self.assertEqual(channels.state(found, "banner")[2], "turned off")
        self.assertIn("no http or https url", channels.state(found, "webhook")[2])
        found["channels"]["command"]["enabled"] = False
        self.assertEqual(channels.on(found), [])
        for broken in ({}, {"channels": []}, {"channels": {"command": "x"}}):
            with self.subTest(broken=broken):
                self.assertNotIn("command", [name for name, _, _ in channels.on(broken)])

    def test_a_channel_that_cannot_be_imported_is_off_and_says_why(self):
        with patch.object(channels.importlib, "import_module", side_effect=ImportError("no such module")):
            self.assertEqual(channels.state({}, "banner")[2], "it cannot be imported: no such module")
            self.assertEqual(channels.on({}), [])

    def test_the_banner_passes_the_notice_as_arguments_and_never_as_script(self):
        ran = []
        def run(command_, **how):
            ran.append((command_, how))
            return subprocess.CompletedProcess(command_, 0, "", "")
        hostile = {**NOTICE, "title": 'a" & (do shell script "x") & "', "body": "b\nc"}
        with patch.object(banner.subprocess, "run", run):
            banner.send(hostile, {})
            banner.send(NOTICE, {"sound": "Basso"})
        argv, how = ran[0]
        self.assertEqual(argv[0], banner.OSASCRIPT)
        self.assertEqual(argv[-4:], [hostile["title"], "b\nc", "project", ""])
        self.assertNotIn("do shell script", " ".join(argv[:-4]))
        self.assertEqual(ran[1][0][-1], "Basso")
        self.assertEqual(how["timeout"], 15)

    def test_a_banner_that_osascript_refuses_raises_with_what_it_said(self):
        refused = subprocess.CompletedProcess([], 1, "", "execution error: not allowed\n")
        with patch.object(banner.subprocess, "run", return_value=refused), \
                self.assertRaisesRegex(RuntimeError, "execution error: not allowed"):
            banner.send(NOTICE, {})

    def test_the_banner_is_for_a_mac(self):
        with patch.object(banner.sys, "platform", "linux"): self.assertEqual(banner.ready({}), "banners are for macOS")

    def test_the_command_gets_the_notice_on_its_input_and_its_title_and_body_as_arguments(self):
        script, kept = self.base / "hook.py", self.base / "got.json"
        script.write_text("import json, sys\njson.dump({'argv': sys.argv[2:], 'notice': json.load(sys.stdin)}, "
                          "open(sys.argv[1], 'w'))\n")
        config = {"argv": [sys.executable, str(script), str(kept)]}
        self.assertIsNone(command.ready(config))
        command.send(NOTICE, config)
        self.assertEqual(json.loads(kept.read_text()), {"argv": [NOTICE["title"], NOTICE["body"]], "notice": NOTICE})

    def test_a_command_that_fails_raises_with_its_output_and_status(self):
        config = {"argv": [sys.executable, "-c", "import sys; print('no route', file=sys.stderr); sys.exit(3)"]}
        with self.assertRaisesRegex(RuntimeError, r"no route \(exit 3\)"): command.send(NOTICE, config)

    def test_a_missing_executable_is_definite_but_a_running_command_timeout_is_uncertain(self):
        for sender, config in ((banner, {}), (command, {"argv": ["notifier"]})):
            with self.subTest(channel=sender.__name__):
                with patch.object(subprocess, "run", side_effect=FileNotFoundError("executable absent")):
                    with self.assertRaises(channels.NotDelivered):
                        sender.send(NOTICE, config)
                with patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired(["notifier"], 30)):
                    with self.assertRaises(subprocess.TimeoutExpired):
                        sender.send(NOTICE, config)

    def test_a_command_needs_an_argument_list(self):
        for config in ({}, {"argv": []}, {"argv": "echo hi"}, {"argv": ["echo", 1]}):
            with self.subTest(config=config): self.assertIn("no argv is set", command.ready(config))

    def serve(self, status=200):
        got = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                got.append((self.path, dict(self.headers), json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                self.send_response(status)
                self.end_headers()
            def log_message(self, *arguments): pass
        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}/hook", got

    def test_the_webhook_posts_the_notice_as_json_with_a_line_of_text_and_its_headers(self):
        url, got = self.serve()
        config = {"url": url, "headers": {"Authorization": "Bearer t"}}
        self.assertIsNone(webhook.ready(config))
        webhook.send(NOTICE, config)
        (path, headers, body), = got
        self.assertEqual(path, "/hook")
        self.assertEqual((headers["Content-Type"], headers["Authorization"]), ("application/json", "Bearer t"))
        self.assertEqual(body, {"text": "project: Entry came back. Add a: its gate failed", **NOTICE})

    def test_a_webhook_that_answers_with_an_error_raises(self):
        url, _ = self.serve(status=500)
        with self.assertRaises(OSError) as raised: webhook.send(NOTICE, {"url": url})
        raised.exception.close()

    def test_a_webhook_needs_an_http_address_and_text_headers(self):
        for config in ({}, {"url": "file:///etc/passwd"}, {"url": "https://"}, {"url": 3}):
            with self.subTest(config=config): self.assertIn("no http or https url", webhook.ready(config))
        self.assertIn("headers", webhook.ready({"url": "https://example.invalid/h", "headers": {"X": 1}}))


class Delivery(Settings, unittest.TestCase):
    """Deliveries against a journal of the test's, with a channel that keeps what it is sent."""

    def setUp(self):
        self.base = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.base, True)
        self.directory = self.base / "project" / ".git" / "queuekeeper-queue"
        self.directory.mkdir(parents=True)
        self.configure()
        self.sent = []
        self.kept = types.SimpleNamespace(send=lambda notice_, config: self.sent.append(notice_))
        on = patch.object(channels, "on", lambda found: [("kept", self.kept, {})])
        on.start()
        self.addCleanup(on.stop)

    def failed(self, entry, at=None):
        with patch.object(events.time, "time", return_value=at) if at else patch.object(events, "VERSION", 1):
            return events.emit(self.directory, "entry.failed", entry=entry, subject=f"Add {entry}", why="its gate failed")

    def titles(self):
        return [(notice_["title"], notice_["event"] and notice_["event"]["entry"]) for notice_ in self.sent]

    def test_each_event_a_rule_tells_of_is_told_once_however_many_deliveries_run(self):
        self.failed("a")
        events.emit(self.directory, "gate.began", lane="gate")
        self.assertEqual(deliver.deliver(self.directory), 1)
        self.assertEqual(deliver.deliver(self.directory), 0)
        self.failed("b")
        self.failed("c")
        self.assertEqual(deliver.deliver(self.directory), 2)
        self.assertEqual(self.titles(), [("Entry came back", "a"), ("Entry came back", "b"), ("Entry came back", "c")])
        self.assertEqual(self.sent[0]["repository"], "project")

    def test_a_first_delivery_tells_of_nothing_older_than_two_minutes(self):
        now = time.time()
        self.failed("old", at=now - deliver.FIRST - 60)
        self.failed("new", at=now - 5)
        self.assertEqual(deliver.deliver(self.directory, now=now), 1)
        self.assertEqual(self.titles(), [("Entry came back", "new")])

    def test_a_cursor_whose_event_the_journal_dropped_stands_for_its_time(self):
        first = self.failed("a", at=1000.0)
        deliver.deliver(self.directory, now=1001.0)
        (self.directory / events.FILE).unlink()
        self.failed("before", at=999.0)
        self.failed("after", at=1002.0)
        self.assertEqual(deliver.read_cursor(self.directory), {"id": first["id"], "time": 1000.0})
        self.assertEqual(deliver.deliver(self.directory), 1)
        self.assertEqual(self.titles()[-1], ("Entry came back", "after"))

    def test_many_notices_at_once_go_as_a_few_and_one_that_counts_the_rest(self):
        for number in range(9): self.failed(str(number))
        self.assertEqual(deliver.deliver(self.directory), deliver.MOST)
        self.assertEqual([title for title, _ in self.titles()], ["Entry came back"] * 4 + ["5 more notices"])
        last = self.sent[-1]
        self.assertEqual((last["level"], last["rule"], last["event"]), (rules.URGENT, "several", None))
        self.assertIn("Add 4: its gate failed; Entry came back: Add 5", last["body"])

    def test_a_new_owner_hold_is_distinct_even_when_recent_event_notices_are_summarized(self):
        for number in range(deliver.MOST + 1): self.failed(str(number))
        hold = events.emit(self.directory, "queue.held", **{"for": "owner", "why": "for the owner: inspect"})

        def refuse_hold(notice_, config):
            if notice_["event"] == hold:
                raise channels.NotDelivered("no route")

        both = [*channels.on({}), ("broken", types.SimpleNamespace(send=refuse_hold), {})]
        with patch.object(channels, "on", return_value=both):
            self.assertEqual(deliver.deliver(self.directory), deliver.MOST)
        self.assertEqual(self.sent[-2]["title"], "Queue held for you")
        self.assertEqual(self.sent[-1]["rule"], "several")
        self.assertEqual(deliver.read_cursor(self.directory)[deliver.HOLD_RETRY], {"event": hold, "channels": ["broken"]})

    def test_a_channel_that_refuses_is_logged_and_the_others_still_send(self):
        def refuse(notice_, config): raise channels.NotDelivered("no route")
        both = [("broken", types.SimpleNamespace(send=refuse), {}), ("kept", self.kept, {})]
        self.failed("a")
        with patch.object(channels, "on", lambda found: both):
            self.assertEqual(deliver.deliver(self.directory), 1)
        self.assertEqual(self.titles(), [("Entry came back", "a")])
        self.assertIn('broken did not send "Entry came back": no route', deliver.log_path().read_text())

    def test_a_notice_is_not_sent_again_after_a_channel_refused_it(self):
        self.kept.send = lambda notice_, config: (_ for _ in ()).throw(RuntimeError("down"))
        self.failed("a")
        self.assertEqual(deliver.deliver(self.directory), 0)
        self.kept.send = lambda notice_, config: self.sent.append(notice_)
        self.assertEqual(deliver.deliver(self.directory), 0)
        self.assertEqual(self.sent, [])

    def test_notices_with_no_channel_on_are_logged_as_going_nowhere(self):
        self.failed("a")
        with patch.object(channels, "on", lambda found: []):
            self.assertEqual(deliver.deliver(self.directory), 0)
        self.assertIn("1 notice(s) of project went nowhere: no channel is on", deliver.log_path().read_text())

    def test_a_rule_that_is_off_tells_of_nothing_and_still_moves_the_cursor(self):
        self.configure(rules_={"came-back": False})
        self.failed("a")
        self.assertEqual(deliver.deliver(self.directory), 0)
        self.configure()
        self.assertEqual(deliver.deliver(self.directory), 0)
        self.assertEqual(self.sent, [])

    def test_the_repository_is_named_by_the_folder_that_holds_its_git_directory(self):
        self.assertEqual(deliver.repository_of("/a/project/.git/queuekeeper-queue"), "project")
        self.assertEqual(deliver.repository_of("/a/origin.git/queuekeeper-queue"), "origin")

    def test_the_process_delivers_only_while_the_feature_is_on(self):
        self.failed("a")
        with patch.dict(os.environ, {visibility.SWITCH: "off"}): self.assertEqual(deliver.main([str(self.directory)]), 0)
        with patch.object(visibility, "enabled", lambda: []): self.assertEqual(deliver.main([str(self.directory)]), 0)
        self.assertEqual(self.sent, [])
        self.assertEqual(deliver.main([str(self.directory)]), 0)
        self.assertEqual(len(self.sent), 1)
        with patch("sys.stderr", io.StringIO()): self.assertEqual(deliver.main([]), 2)

    def test_a_delivery_that_stops_is_logged_and_exits_1(self):
        with patch.object(deliver, "deliver", side_effect=RuntimeError("no journal")):
            self.assertEqual(deliver.main([str(self.directory)]), 1)
        self.assertIn(f"the delivery for {self.directory} stopped: no journal", deliver.log_path().read_text())


class Spawning(Settings, unittest.TestCase):
    def setUp(self):
        self.configure()
        started = patch.object(deliver.subprocess, "Popen")
        self.popen = started.start()
        self.addCleanup(started.stop)

    def test_an_event_a_rule_tells_of_starts_a_delivery_apart_from_this_process(self):
        deliver.spawn("/a/queue", {"kind": "entry.failed"})
        (argv,), how = self.popen.call_args
        self.assertEqual(argv, [sys.executable, "-m", "queuekeeper_notify", "/a/queue"])
        self.assertTrue(how["start_new_session"])
        self.assertEqual((how["stdin"], how["stdout"], how["stderr"]), (subprocess.DEVNULL,) * 3)
        self.assertEqual(how["env"]["PYTHONPATH"].split(os.pathsep)[0],
                         str(Path(notify.__file__).resolve().parents[1]))

    def test_an_event_no_rule_that_is_on_tells_of_starts_nothing(self):
        for event in ({"kind": "gate.began"}, {"kind": "entry.landed"}, {"kind": "queue.held", "for": "session"}):
            deliver.spawn("/a/queue", event)
        with patch.dict(os.environ, {visibility.SWITCH: "off"}): deliver.spawn("/a/queue", {"kind": "entry.failed"})
        self.popen.assert_not_called()


class SpawningForATest(Settings, test_merge_queue.Fixture):
    def test_a_requested_test_starts_a_look_even_with_every_rule_that_looks_turned_off(self):
        self.configure(rules_={"stuck": False, "held-owner": False})
        mq.Queue(self.repo)
        started = patch.object(deliver, "start")
        popen = started.start()
        self.addCleanup(started.stop)
        here = os.getcwd()
        self.addCleanup(os.chdir, here)
        os.chdir(self.repo)
        check.spawn()
        popen.assert_not_called()
        check.request_test(self.repo)
        check.spawn()
        self.assertEqual(popen.call_args.args[0], "--check")


class Checking(Settings, test_merge_queue.Fixture):
    """The look for a queue that is stuck without an event, with findings of the test's and a channel that keeps
    what it is sent."""

    def setUp(self):
        super().setUp()
        self.configure()
        self.directory = mq.Queue(self.repo).directory
        self.sent, self.found = [], []
        kept = types.SimpleNamespace(send=lambda notice_, config: self.sent.append(notice_))
        on = patch.object(channels, "on", lambda found: [("kept", kept, {})])
        on.start()
        self.addCleanup(on.stop)

    def look(self, at):
        return check.check(self.repo, now=at, diagnose=lambda root: (list(self.found), [], False))

    def finding(self, signal="no worker", diagnosis="2 entries wait and no worker runs"):
        return {"signal": signal, "diagnosis": diagnosis, "remedy": "run `qk queue resume`"}

    def test_a_finding_is_told_once_it_has_lasted_and_only_once(self):
        self.found = [self.finding()]
        self.assertEqual(self.look(1000.0), (self.found, 0))
        self.assertEqual(self.look(1000.0 + check.PERSIST - 1)[1], 0)
        self.assertEqual(self.look(1000.0 + check.PERSIST)[1], 1)
        self.assertEqual(self.look(1000.0 + check.PERSIST + 600)[1], 0)
        notice_, = self.sent
        self.assertEqual((notice_["title"], notice_["level"], notice_["rule"], notice_["repository"]),
                         ("Queue looks stuck", rules.URGENT, "stuck", "project"))
        self.assertEqual(notice_["body"], "2 entries wait and no worker runs. Next: run `qk queue resume`")

    def test_a_finding_that_goes_before_it_has_lasted_is_never_told(self):
        self.found = [self.finding()]
        self.look(1000.0)
        self.found = []
        self.look(1100.0)
        self.found = [self.finding()]
        self.assertEqual(self.look(1000.0 + check.PERSIST)[1], 0)
        self.assertEqual(self.sent, [])

    def test_a_finding_that_went_and_came_back_is_told_again_once_it_has_lasted_again(self):
        self.found = [self.finding()]
        self.look(0.0)
        self.look(check.PERSIST)
        self.found = []
        self.look(check.PERSIST + 10)
        self.found = [self.finding()]
        self.assertEqual(self.look(check.PERSIST + 20)[1], 0)
        self.assertEqual(self.look(2 * check.PERSIST + 20)[1], 1)
        self.assertEqual(len(self.sent), 2)

    def test_each_finding_lasts_by_its_own_signal_whatever_its_words_become(self):
        self.found = [self.finding(diagnosis="1 entry waits and no worker runs")]
        self.look(0.0)
        self.found = [self.finding(diagnosis="3 entries wait and no worker runs"), self.finding("no verdict", "3 gates gave none")]
        self.assertEqual(self.look(check.PERSIST)[1], 1)
        self.assertEqual(self.look(2 * check.PERSIST)[1], 1)
        self.assertEqual([notice_["finding"]["signal"] for notice_ in self.sent], ["no worker", "no verdict"])

    def test_a_hold_and_an_entry_that_came_back_unchanged_are_not_this_looks_to_tell(self):
        self.found = [self.finding("held", "the queue is held for a test"), self.finding("came back", "e2 holds e1's change")]
        self.look(0.0)
        self.assertEqual(self.look(check.PERSIST), ([], 0))
        self.assertEqual(self.sent, [])

    def test_the_rule_turned_off_tells_of_nothing(self):
        self.configure(rules_={"stuck": False})
        self.found = [self.finding()]
        self.look(0.0)
        self.assertEqual(self.look(check.PERSIST)[1], 0)
        self.assertEqual(self.sent, [])

    def test_a_requested_test_is_sent_once_by_the_next_look_and_logged(self):
        check.request_test(self.repo)
        self.assertEqual(self.look(1000.0), ([], 0))
        notice_, = self.sent
        self.assertEqual((notice_["rule"], notice_["title"], notice_["repository"]),
                         ("test", "Queuekeeper test notice", "project"))
        self.assertIn("scheduled queue check", notice_["body"])
        self.assertFalse((self.directory / check.TEST).exists())
        self.assertIn("scheduled test notice of project", deliver.log_path().read_text())
        self.assertIn("sent through kept", deliver.log_path().read_text())
        self.look(1100.0)
        self.assertEqual(len(self.sent), 1)

    def test_a_requested_test_for_one_channel_goes_through_that_one_alone(self):
        other = []
        both = [("kept", types.SimpleNamespace(send=lambda notice_, config: self.sent.append(notice_)), {}),
                ("other", types.SimpleNamespace(send=lambda notice_, config: other.append(notice_)), {})]
        with patch.object(channels, "on", lambda found: both):
            check.request_test(self.repo, "other")
            self.look(1000.0)
        self.assertEqual((len(self.sent), len(other)), (0, 1))

    def test_a_requested_test_a_channel_refuses_is_logged_and_not_repeated(self):
        def refuse(notice_, config): raise channels.NotDelivered("Messages refused")
        with patch.object(channels, "on", lambda found: [("broken", types.SimpleNamespace(send=refuse), {})]):
            check.request_test(self.repo)
            self.look(1000.0)
            self.look(1100.0)
        logged = deliver.log_path().read_text()
        self.assertIn("sent through no channel", logged)
        self.assertEqual(logged.count("Messages refused"), 1)

    def test_an_unreadable_test_request_is_dropped_and_logged(self):
        (self.directory / check.TEST).write_text("{not json")
        self.look(1000.0)
        self.assertEqual(self.sent, [])
        self.assertFalse((self.directory / check.TEST).exists())
        self.assertIn("could not be read and was dropped", deliver.log_path().read_text())

    def test_a_finding_retries_after_every_channel_refuses(self):
        def refuse(notice_, config): raise channels.NotDelivered("no route")
        self.found = [self.finding()]
        self.look(0.0)
        with patch.object(channels, "on", lambda found: [("broken", types.SimpleNamespace(send=refuse), {})]):
            self.assertEqual(self.look(check.PERSIST)[1], 0)
        self.assertIn('broken did not send "Queue looks stuck": no route', deliver.log_path().read_text())
        self.assertEqual(self.look(check.PERSIST + 60)[1], 1)
        self.assertEqual(self.look(check.PERSIST + 120)[1], 0)
        self.assertEqual(len(self.sent), 1)

    def test_uncertain_delivery_is_skipped_while_a_definite_refusal_can_retry(self):
        self.found = [self.finding()]
        ambiguous, accepted = [], []
        refusing = True

        def timed_out(notice_, config):
            ambiguous.append(notice_)
            raise subprocess.TimeoutExpired(["notifier"], 30)

        def recoverable(notice_, config):
            if refusing: raise channels.NotDelivered("not started")
            accepted.append(notice_)

        self.look(0.0)
        both = [("timed-out", types.SimpleNamespace(send=timed_out), {}),
                ("recoverable", types.SimpleNamespace(send=recoverable), {})]
        with patch.object(channels, "on", return_value=both):
            self.assertEqual(self.look(check.PERSIST)[1], 0)
            self.assertEqual(check.read(self.directory)["findings"]["no worker"]["uncertain"], ["timed-out"])
            refusing = False
            self.assertEqual(self.look(check.PERSIST + check.EVERY)[1], 1)
            self.assertEqual(self.look(check.PERSIST + 2 * check.EVERY)[1], 0)
        self.assertEqual(ambiguous, accepted)
        self.assertEqual(len(ambiguous), 1)
        self.assertIn("could not confirm delivery", deliver.log_path().read_text())

    def test_a_finding_waits_for_a_channel_to_be_enabled(self):
        self.found = [self.finding()]
        self.look(0.0)
        with patch.object(channels, "on", return_value=[]):
            self.assertEqual(self.look(check.PERSIST)[1], 0)
        self.assertEqual(self.look(check.PERSIST + 60)[1], 1)

    def test_a_finding_delivered_to_one_channel_does_not_retry_other_channels(self):
        def refuse(notice_, config): raise channels.NotDelivered("no route")
        self.found = [self.finding()]
        self.look(0.0)
        available = [*channels.on({}), ("broken", types.SimpleNamespace(send=refuse), {})]
        with patch.object(channels, "on", return_value=available):
            self.assertEqual(self.look(check.PERSIST)[1], 1)
            self.assertEqual(self.look(check.PERSIST + 60)[1], 0)
        self.assertEqual(len(self.sent), 1)

    def test_the_queues_own_doctor_is_what_it_asks(self):
        self.commit("a.txt", "one")
        self.add("one")
        findings, told = check.check(self.repo, now=0.0)
        self.assertEqual(([finding["signal"] for finding in findings], told), (["no worker"], 0))
        self.assertEqual(check.check(self.repo, now=check.PERSIST)[1], 1)
        self.assertIn("1 entry wait and no worker runs", self.sent[0]["body"])

    def test_a_broken_doctor_is_reported_without_clearing_an_earlier_finding(self):
        self.found = [self.finding()]
        self.look(0.0)
        self.assertEqual(self.look(check.PERSIST)[1], 1)

        def broken(root):
            raise OSError("private detector detail")

        first_failure = check.PERSIST + 10
        for at, told in ((first_failure, 0), (first_failure + check.PERSIST, 1),
                         (first_failure + check.PERSIST + 60, 0)):
            findings, count = check.check(self.repo, now=at, diagnose=broken)
            self.assertEqual(([finding["signal"] for finding in findings], count), ([rules.CHECK_FAILED], told))
        self.assertEqual([notice_["title"] for notice_ in self.sent], ["Queue looks stuck", "Queue check failed"])
        self.assertNotIn("private detector detail", json.dumps(self.sent))
        self.assertIn("private detector detail", deliver.log_path().read_text())
        self.assertEqual(self.look(first_failure + check.PERSIST + 120), (self.found, 0))
        self.assertNotIn(rules.CHECK_FAILED, check.read(self.directory)["findings"])

    def test_a_background_check_returns_failure_and_keeps_trying_to_tell_of_it(self):
        now = 1000.0
        with (patch.object(mq, "diagnose", side_effect=RuntimeError("no doctor")),
              patch.object(check.time, "time", side_effect=lambda: now)):
            self.assertEqual(deliver.main(["--check", str(self.repo)]), 1)
            self.assertEqual(self.sent, [])
            now += check.PERSIST
            self.assertEqual(deliver.main(["--check", str(self.repo)]), 1)
            self.assertEqual(deliver.main(["--check", str(self.repo)]), 1)
        self.assertEqual([notice_["title"] for notice_ in self.sent], ["Queue check failed"])

    def test_real_diagnosis_tells_of_no_verdict_and_preserves_queue_records(self):
        queue = mq.Queue(self.repo)
        config = self.config()
        config["lanes"]["gate"]["command"] = ["python3", "scripts/qk", "unittest", "-s", "tests"]
        (self.repo / "tests").mkdir()
        (self.repo / "tests/host_isolation.py").touch()
        (self.repo / "tests/test_gate.py").write_text(
            "import unittest\nimport host_isolation\n\nclass Gate(unittest.TestCase):\n    def test_gate(self): pass\n")
        test_merge_queue.run(self.repo, "git", "add", "tests")
        self.publish(config, "Gate on qk unittest")
        self.commit("a.txt", "one")
        identifier, = self.add("one")
        queue.mark([queue.read(identifier)], mq.TESTING)
        for index in range(2):
            (queue.logs / f"20261107-10000{index}-aaaaaaaa-gate.log").write_text(
                "queuekeeper: lane gate reached its limit\n")
        recorded = [self.repo / ".git/HEAD", *queue.logs.glob("*"), *queue.entries.glob("*.json")]
        before = {path: path.read_bytes() for path in recorded}
        with queue.worker() as free:
            self.assertTrue(free)
            findings, _ = check.check(self.repo, now=0.0)
            self.assertEqual(check.check(self.repo, now=check.PERSIST)[1], 1)
            self.assertEqual(check.check(self.repo, now=2 * check.PERSIST)[1], 0)
        self.assertEqual([finding["signal"] for finding in findings], ["no verdict"])
        notice_, = self.sent
        self.assertEqual(notice_["finding"], findings[0])
        self.assertEqual({path: path.read_bytes() for path in recorded}, before)
        self.assertIsNone(queue.held())
        self.assertEqual(queue.read(identifier)["state"], mq.TESTING)

    def test_an_owner_hold_alerts_via_its_event_without_a_duplicate_from_the_check(self):
        queue = mq.Queue(self.repo)
        why = "for the owner: inspect the queue"
        with patch.object(plugins, "AFTER_EVENT", []):
            queue.hold(why)

        self.assertEqual(deliver.deliver(self.directory), 1)
        self.assertEqual(deliver.deliver(self.directory), 0)
        self.assertEqual(check.check(self.repo, now=0.0), ([], 0))
        self.assertEqual(check.check(self.repo, now=check.PERSIST), ([], 0))
        notice_, = self.sent
        self.assertEqual((notice_["title"], notice_["body"]), ("Queue held for you", why))
        self.assertEqual(queue.held(), why)

    def test_the_checker_retries_only_the_refused_owner_hold_channel(self):
        queue = mq.Queue(self.repo)
        retried, ambiguous = [], []
        refusing = True

        def flaky(notice_, config):
            if refusing: raise channels.NotDelivered("no route")
            retried.append(notice_)

        def timed_out(notice_, config):
            ambiguous.append(notice_)
            raise TimeoutError("no response from the webhook")

        hold = "for the owner: inspect the queue"
        # Load a cursor written by a previous Queuekeeper to verify its shape migrates as events advance.
        prior = queue.emit("gate.began", lane="gate")
        (self.directory / deliver.CURSOR).write_text(json.dumps({"id": prior["id"], "time": prior["time"]}))
        both = [*channels.on({}), ("flaky", types.SimpleNamespace(send=flaky), {}),
                ("timed-out", types.SimpleNamespace(send=timed_out), {})]
        with patch.object(plugins, "AFTER_EVENT", []), patch.object(channels, "on", return_value=both):
            queue.hold(hold)
            self.assertEqual(deliver.deliver(self.directory), 1)
            refused = deliver.read_cursor(self.directory)[deliver.HOLD_RETRY]
            self.assertEqual(refused["channels"], ["flaky"])
            newest = queue.emit("gate.began", lane="gate")
            self.assertEqual(deliver.deliver(self.directory), 0)
            self.assertEqual(deliver.read_cursor(self.directory)[deliver.HOLD_RETRY], refused)
            self.assertEqual(retried, [])
            self.configure(rules_={"stuck": False})
            self.spawned().assert_called_once_with("--check", str(self.repo))
            refusing = False
            self.assertEqual(deliver.main(["--check", str(self.repo)]), 0)
            self.assertEqual(deliver.main(["--check", str(self.repo)]), 0)

        notice_, = self.sent
        self.assertEqual(retried, [notice_])
        self.assertEqual(ambiguous, [notice_])
        self.assertIn("could not confirm delivery", deliver.log_path().read_text())
        self.assertEqual(notice_["body"], hold)
        self.assertEqual(deliver.read_cursor(self.directory), {"id": newest["id"], "time": newest["time"]})
        self.assertEqual(queue.held(), hold)

    def test_the_checker_discards_a_retry_for_a_superseded_hold_even_with_the_same_reason(self):
        queue = mq.Queue(self.repo)
        hold = "for the owner: inspect the queue"

        def refuse(notice_, config):
            raise channels.NotDelivered("no route")

        with patch.object(plugins, "AFTER_EVENT", []):
            queue.hold(hold)
            with patch.object(channels, "on", return_value=[("kept", types.SimpleNamespace(send=refuse), {})]):
                self.assertEqual(deliver.deliver(self.directory), 0)
            queue.hold_file.unlink()
            queue.emit("queue.resumed", was=hold)
            queue.hold(hold)
            self.assertEqual(deliver.main(["--check", str(self.repo)]), 0)
            self.assertEqual(self.sent, [])
            self.assertNotIn(deliver.HOLD_RETRY, deliver.read_cursor(self.directory))
            self.assertEqual(deliver.deliver(self.directory), 1)
        notice_, = self.sent
        self.assertEqual(notice_["body"], hold)

    def spawned(self, folder=None):
        with patch.object(deliver, "start") as start, patch.object(check.Path, "cwd", return_value=folder or self.repo):
            check.spawn()
        return start

    def test_a_command_starts_a_look_at_its_repository_at_most_once_in_two_minutes(self):
        self.spawned().assert_called_once_with("--check", str(self.repo))
        self.spawned().assert_not_called()
        old = time.time() - check.EVERY - 1
        os.utime(self.directory / check.STATE, (old, old))
        self.spawned().assert_called_once_with("--check", str(self.repo))

    def test_a_command_in_a_linked_worktree_starts_the_look_at_its_main_checkout(self):
        linked = self.base / "linked"
        test_merge_queue.run(self.repo, "git", "worktree", "add", "-q", str(linked), "-b", "linked")
        self.spawned(linked).assert_called_once_with("--check", str(self.repo))

    def test_no_look_starts_where_there_is_no_queue_or_the_rule_or_the_feature_is_off(self):
        self.spawned(self.base).assert_not_called()
        self.configure(rules_={"stuck": False})
        self.spawned().assert_not_called()
        self.configure()
        with patch.dict(os.environ, {visibility.SWITCH: "off"}): self.spawned().assert_not_called()

    def test_the_process_looks_when_asked_to_check(self):
        with patch.object(check, "check", return_value=([], 0)) as looked:
            self.assertEqual(deliver.main(["--check", str(self.repo)]), 0)
        looked.assert_called_once_with(str(self.repo))
        with patch.object(check, "check", side_effect=RuntimeError("no queue")):
            self.assertEqual(deliver.main(["--check", str(self.repo)]), 1)
        self.assertIn(f"the check of {self.repo} stopped: no queue", deliver.log_path().read_text())

    def test_the_command_lists_the_findings_and_exits_1_when_there_are_any(self):
        self.keep_plugins()
        printed = io.StringIO()
        with patch("sys.stdout", printed): status = cli.main(["--project", str(self.repo), "notify", "check"])
        self.assertEqual((status, printed.getvalue()), (0, "0 finding(s), 0 told of now\n"))
        self.commit("a.txt", "one")
        self.add("one")
        printed = io.StringIO()
        with patch("sys.stdout", printed): status = cli.main(["--project", str(self.repo), "notify", "check"])
        self.assertEqual(status, 1)
        self.assertIn("no worker: 1 entry wait and no worker runs", printed.getvalue())
        self.assertTrue(printed.getvalue().endswith("1 finding(s), 0 told of now\n"))

    def test_the_command_distinguishes_a_broken_check_from_a_queue_finding(self):
        self.keep_plugins()
        printed = io.StringIO()
        with (patch("sys.stdout", printed), patch.object(mq, "diagnose", side_effect=OSError("no doctor"))):
            status = cli.main(["--project", str(self.repo), "notify", "check"])
        self.assertEqual(status, 2)
        self.assertIn("doctor unavailable: Queue doctor could not complete its check", printed.getvalue())

    def test_the_feature_registers_the_look_with_the_commands_that_reach_the_hosts(self):
        self.keep_plugins()
        visibility.load()
        self.assertIn(check.spawn, plugins.HOST_COMMAND)


class EndToEnd(Settings, test_merge_queue.Fixture):
    """The queue, with the feature on, tells its owner of an entry that came back, through a delivery of its own."""

    def test_an_entry_that_fails_its_gate_reaches_the_owners_command(self):
        argv, kept = self.recorder(self.base)
        self.configure(command={"argv": argv})
        self.keep_plugins()
        visibility.load()
        self.assertIn(deliver.spawn, plugins.AFTER_EVENT)
        self.status.write_text("1")
        self.commit("a.txt", "one")
        identifier, = self.add("one")
        with patch("builtins.print"): self.work()
        self.assertEqual(self.state(identifier)["state"], mq.FAILED)
        deadline = time.monotonic() + host_isolation.stretched(30)
        while not kept.exists() and time.monotonic() < deadline: time.sleep(0.1)
        self.assertTrue(kept.exists(), deliver.log_path().read_text() if deliver.log_path().exists() else "no log")
        time.sleep(0.5)
        got = [json.loads(line) for line in kept.read_text().splitlines() if line]
        self.assertEqual([(notice_["title"], notice_["event"]["entry"]) for notice_ in got],
                         [("Entry came back", identifier)])
        self.assertEqual(got[0]["repository"], "project")
        self.assertIn("Add a.txt", got[0]["body"])


class Command(Settings, unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.base, True)
        self.configure()
        self.keep_plugins()

    def command(self, *words):
        printed, errors, status = io.StringIO(), io.StringIO(), None
        with patch("sys.stdout", printed), patch("sys.stderr", errors):
            try: status = cli.main(["--project", str(self.base), "notify", *words])
            except SystemExit: pass
        return status, printed.getvalue(), errors.getvalue()

    def stated(self):
        return json.loads(visibility.settings_path().read_text())["notify"]

    def test_status_lists_each_rule_and_channel_and_hides_what_a_channel_is_set_to(self):
        self.configure(webhook={"url": "https://example.invalid/secret-token"})
        status, printed, _ = self.command("status")
        self.assertEqual(status, 0)
        self.assertIn("  on   came-back  an entry failed its gate, or conflicts with main", printed)
        self.assertIn("  off  landed  an entry landed", printed)
        self.assertIn("  off  banner  A macOS notification banner, posted with osascript\n       off: turned off", printed)
        self.assertIn("  on   webhook  ", printed)
        self.assertIn("       url: (set)", printed)
        self.assertNotIn("secret-token", printed)
        told = json.loads(self.command("status", "--json")[1])
        self.assertEqual((told["rules"]["landed"]["enabled"], told["channels"]["webhook"]["enabled"]), (False, True))
        self.assertNotIn("secret-token", json.dumps(told))

    def test_enable_and_disable_switch_one_rule_or_one_channel(self):
        self.assertEqual(self.command("enable", "landed")[:2], (0, "landed is on\n"))
        self.assertEqual(self.command("disable", "flaky")[0], 0)
        self.assertEqual(self.command("enable", "banner")[0], 0)
        self.assertEqual(self.stated()["rules"], {"landed": True, "flaky": False})
        self.assertEqual(self.stated()["channels"]["banner"], {"enabled": True})
        self.assertEqual(rules.chosen(deliver.settings()), {"held-owner", "came-back", "stuck", "ungated", "landed"})
        status, _, errors = self.command("enable", "nope")
        self.assertEqual(status, 2)
        self.assertIn("notify has no rule or channel named nope", errors)

    def test_set_takes_json_or_text_and_unset_removes_it(self):
        self.assertEqual(self.command("set", "command", "argv", '["echo", "hi"]')[0], 0)
        self.assertEqual(self.command("set", "webhook", "url", "https://example.invalid/h")[0], 0)
        self.assertEqual(self.stated()["channels"]["command"], {"argv": ["echo", "hi"]})
        self.assertEqual(self.stated()["channels"]["webhook"], {"url": "https://example.invalid/h"})
        self.assertEqual(self.command("unset", "webhook", "url")[0], 0)
        self.assertEqual(self.stated()["channels"]["webhook"], {})
        self.assertEqual(self.command("set", "nope", "url", "x")[0], 2)
        self.assertEqual(self.command("set", "banner", "enabled", "true")[0], 2)

    def test_test_sends_through_each_channel_that_is_on_and_says_how_each_went(self):
        argv, kept = self.recorder(self.base)
        self.configure(command={"argv": argv})
        status, printed, _ = self.command("test")
        self.assertEqual(status, 0)
        self.assertEqual(printed.splitlines()[:2], ["banner: off: turned off", "command: sent"])
        got = json.loads(kept.read_text())
        self.assertEqual((got["title"], got["repository"], got["rule"]),
                         ("Queuekeeper test notice", self.base.name, "test"))

    def test_a_test_from_a_linked_worktree_names_the_repository_and_not_the_worktree(self):
        argv, kept = self.recorder(self.base)
        self.configure(command={"argv": argv})
        main, linked = self.base / "project", self.base / "elsewhere" / "a-worktree"
        (main / ".git" / "worktrees" / "a-worktree").mkdir(parents=True)
        linked.mkdir(parents=True)
        (linked / ".git").write_text(f"gitdir: {main / '.git' / 'worktrees' / 'a-worktree'}\n")
        (main / ".git" / "worktrees" / "a-worktree" / "commondir").write_text("../..\n")
        printed = io.StringIO()
        with patch("sys.stdout", printed): status = cli.main(["--project", str(linked), "notify", "test"])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(kept.read_text())["repository"], "project")

    def test_test_exits_1_when_a_channel_refuses_or_none_is_on(self):
        self.configure(command={"argv": [sys.executable, "-c", "raise SystemExit(4)"]})
        status, printed, _ = self.command("test")
        self.assertEqual(status, 1)
        self.assertIn("command: did not send:", printed)
        self.configure()
        status, printed, _ = self.command("test")
        self.assertEqual(status, 1)
        self.assertIn("no channel is on, so the test went nowhere", printed)
        self.assertEqual(self.command("test", "--channel", "banner")[0], 1)

    def test_a_scheduled_test_leaves_a_request_for_the_next_look_and_sends_nothing_now(self):
        argv, kept = self.recorder(self.base)
        self.configure(command={"argv": argv})
        (self.base / ".git" / "queuekeeper-queue").mkdir(parents=True)
        status, printed, _ = self.command("test", "--scheduled", "--channel", "command")
        self.assertEqual(status, 0)
        self.assertIn("the next scheduled check sends the test", printed)
        self.assertFalse(kept.exists())
        request = json.loads((self.base / ".git" / "queuekeeper-queue" / check.TEST).read_text())
        self.assertEqual(request["channel"], "command")
        self.assertEqual(self.command("test", "--scheduled", "--channel", "nope")[0], 2)

    def test_a_scheduled_test_needs_a_queue(self):
        status, _, errors = self.command("test", "--scheduled")
        self.assertEqual(status, 2)
        self.assertIn("has no queue for a scheduled check", errors)

    def test_it_is_no_command_while_the_feature_is_off(self):
        visibility.switch("notify", False)
        plugins.COMMANDS.pop("notify", None)
        status, _, errors = self.command("status")
        self.assertIsNone(status)
        self.assertIn("invalid choice: 'notify'", errors)


if __name__ == "__main__":
    unittest.main()
