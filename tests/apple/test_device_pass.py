"""The release's device pass as a lane, `qk device-pass` (vocaro-engineering#148)."""
import contextlib
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import host_isolation  # noqa: F401  (first: keeps every test out of the host's admission)
from queuekeeper import budgets, cli
from queuekeeper_apple import device_notifications, device_pass, devices
from test_support.devices import BANNER, Devicectl, PHONE, TABLET, Watches, approval, later, scratch

SOURCE = Path(__file__).resolve().parents[2] / "src/python"
# A command that records the device it was given, by its words and its environment, then exits with STATUS,
# printing an executed test as xcodebuild does.
PROBE = ("import json, os, sys; open(sys.argv[1], 'a').write(json.dumps({'words': sys.argv[2:], "
         "'key': os.environ.get('QUEUEKEEPER_DEVICE_KEY'), 'udid': os.environ.get('QUEUEKEEPER_DEVICE_UDID'), "
         "'results': os.environ.get('QUEUEKEEPER_RESULTS_DIR')}) + '\\n'); print('Executed 1 test, with 0 failures'); "
         "raise SystemExit(int(os.environ.get('STATUS_' + os.environ['QUEUEKEEPER_DEVICE_KEY'], '0')))")


class Fixture(unittest.TestCase):
    def setUp(self):
        temp = scratch()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = self.root / "device-handoff.json"
        self.record = devices.Record(self.path)
        self.calls = self.root / "calls"
        self.watches = Watches()
        for patcher in (patch.dict(os.environ, {devices.OVERRIDE: str(self.path)}),
                        patch.object(device_notifications, "run", Devicectl()),
                        patch.object(device_notifications, "watch", self.watches)):
            patcher.start()
            self.addCleanup(patcher.stop)
        for name in (devices.OWNER, devices.LEASE, "CODEX_THREAD_ID", "CLAUDE_CODE_SESSION_ID", "QUEUEKEEPER_RESULTS_DIR"):
            os.environ.pop(name, None)
        # Each paired device's first reservation records its device.
        for key, device in (("iPad", TABLET), ("iPhone", PHONE)):
            entry = self.record.reserve(key, owner="first", revision=0, purpose="p", release_by=later(), device=device)
            self.record.release(key, owner="first", revision=entry["revision"], outcome="o", restoration="r")

    def command(self, *words):
        return [sys.executable, "-c", PROBE, str(self.calls), *words]

    def ran(self):
        return [json.loads(line) for line in self.calls.read_text().splitlines()] if self.calls.exists() else []

    def hold_iphone(self, owner="session-a"):
        entry = self.record.entry("iPhone")
        return self.record.reserve("iPhone", owner=owner, revision=entry["revision"], purpose="its own run",
                                   release_by=later(60))

    def run_pass(self, keys, command, **options):
        options.setdefault("owner", "session-a")
        options.setdefault("wait", 0)
        said = []
        parts, code = device_pass.run(keys, command, record=self.record, said=said.append, **options)
        return {part.key: part for part in parts}, code, "\n".join(said)


class DevicePass(Fixture):
    def test_the_ipad_is_taken_in_turn_run_on_and_released_and_a_held_iphone_is_used_and_kept(self):
        self.hold_iphone()
        results = self.root / "results"
        parts, code, said = self.run_pass(["iPad", "iPhone"], self.command("id={udid}", "{key}"), results=results)
        self.assertEqual(code, 0, said)
        self.assertEqual({key: part.outcome for key, part in parts.items()}, {"iPad": "passed", "iPhone": "passed"})
        # Each device's command names that device, in its words and its environment, with a results directory
        # of its own.
        [ipad, iphone] = self.ran()
        self.assertEqual((ipad["words"], ipad["key"], ipad["udid"]), ([f"id={TABLET}", "iPad"], "iPad", TABLET))
        self.assertEqual((iphone["words"], iphone["udid"]), ([f"id={PHONE}", "iPhone"], PHONE))
        self.assertEqual(ipad["results"], str(results / "iPad"))
        # The iPad, taken in turn, is released; the iPhone, the session's own reservation, stays held.
        ipad_entry, iphone_entry = self.record.entry("iPad"), self.record.entry("iPhone")
        self.assertEqual(devices.condition(ipad_entry), "released")
        self.assertEqual(ipad_entry["lastOwnerChatID"], "session-a")
        self.assertNotIn("approval", ipad_entry)
        self.assertIn("device pass: passed", ipad_entry["run"])
        self.assertEqual((devices.condition(iphone_entry), iphone_entry["ownerChatID"]), ("held", "session-a"))
        self.assertEqual(iphone_entry["run"]["outcome"], "exit 0")
        self.assertEqual((parts["iPad"].took, parts["iPad"].released, parts["iPhone"].took), (True, True, False))
        self.assertIn("iPhone: session-a holds it already", said)
        # Each run was watched for a banner on its own device.
        self.assertEqual(self.watches.devices, [TABLET, PHONE])
        # The summary records each device's outcome by key, and no device's identity.
        summary = (results / device_pass.SUMMARY).read_text()
        self.assertEqual(json.loads(summary)["status"], 0)
        self.assertEqual([part["key"] for part in json.loads(summary)["devices"]], ["iPad", "iPhone"])
        self.assertNotIn(TABLET, summary)
        self.assertNotIn(PHONE, summary)

    def test_the_iphone_is_taken_in_turn_run_on_and_released_as_the_ipad_is(self):
        # The owner's decision of 2026-11-06: the pass checks no approval, which is between the session and the
        # owner; it only takes the device in turn.
        parts, code, said = self.run_pass(["iPhone"], self.command("{key}"), minutes=20)
        self.assertEqual(code, 0, said)
        self.assertEqual((parts["iPhone"].outcome, parts["iPhone"].took, parts["iPhone"].released), ("passed", True, True))
        self.assertEqual([call["udid"] for call in self.ran()], [PHONE])
        entry = self.record.entry("iPhone")
        self.assertEqual((devices.condition(entry), entry["lastOwnerChatID"]), ("released", "session-a"))
        self.assertNotIn("approval", entry)
        # The negative control: an iPhone another session holds past the wait is not run, and the pass is
        # unmeasured, never passed.
        self.hold_iphone(owner="session-b")
        parts, code, _said = self.run_pass(["iPhone"], self.command())
        self.assertEqual((parts["iPhone"].outcome, code), ("not run", device_pass.UNMEASURED))
        self.assertIn("stayed held or claimed by session-b", parts["iPhone"].detail)
        self.assertEqual(len(self.ran()), 1)

    def test_a_failure_on_one_device_fails_the_pass_and_the_other_still_runs(self):
        self.hold_iphone()
        with patch.dict(os.environ, {"STATUS_iPad": "4"}):
            parts, code, _said = self.run_pass(["iPad", "iPhone"], self.command())
        self.assertEqual(code, 1)
        self.assertEqual((parts["iPad"].outcome, parts["iPad"].status), ("failed", 4))
        self.assertEqual(parts["iPhone"].outcome, "passed")
        # A failure beside a device not run is still a failure.
        with patch.dict(os.environ, {"STATUS_iPad": "4"}):
            self.assertEqual(self.run_pass(["iPad", "iPhone"], self.command(), owner="session-b")[1], 1)
        # The iPad went back after its failure too.
        self.assertEqual(devices.condition(self.record.entry("iPad")), "released")
        # A command's own unmeasured status leaves the device unmeasured.
        with patch.dict(os.environ, {"STATUS_iPad": str(device_pass.UNMEASURED)}):
            parts, code, _said = self.run_pass(["iPad"], self.command())
        self.assertEqual((code, parts["iPad"].outcome), (device_pass.UNMEASURED, "unmeasured"))

    def test_a_failed_run_on_a_held_iphone_whose_entry_changed_meanwhile_ends_its_run_for_the_next_pass(self):
        # vocaro/queuekeeper#230: the session that holds the iPhone changes its entry while the pass runs on it, as
        # an extension of its window does, and the run fails. The run's end is still recorded, so the entry is
        # held with no run active, and the next pass uses it rather than calling it an orphan.
        self.hold_iphone()
        change = ("import sys; from queuekeeper_apple import devices; record = devices.Record(); "
                  "entry = record.entry('iPhone'); record.update('iPhone', owner='session-a', "
                  "revision=entry['revision'], purpose='its own run, extended'); raise SystemExit(1)")
        with patch.dict(os.environ, {"PYTHONPATH": host_isolation.pythonpath()}):
            parts, code, said = self.run_pass(["iPhone"], [sys.executable, "-c", change])
        self.assertEqual((code, parts["iPhone"].outcome, parts["iPhone"].took), (1, "failed", False), said)
        entry = self.record.entry("iPhone")
        self.assertEqual((devices.condition(entry), entry["activeRun"], entry["run"]["outcome"]),
                         ("held", False, "exit 1"))
        self.assertEqual(entry["purpose"], "its own run, extended")
        parts, code, said = self.run_pass(["iPhone"], self.command())
        self.assertEqual((code, parts["iPhone"].outcome), (0, "passed"), said)
        self.assertIn("iPhone: session-a holds it already", said)

    def test_a_run_a_notification_reached_twice_is_unmeasured_and_the_device_goes_back(self):
        self.watches.seen = [BANNER, BANNER]
        parts, code, said = self.run_pass(["iPad"], self.command())
        self.assertEqual((code, parts["iPad"].outcome), (device_pass.UNMEASURED, "unmeasured"))
        self.assertEqual(len(self.ran()), 2)
        self.assertIn("Not run a third time", said)
        self.assertEqual(devices.condition(self.record.entry("iPad")), "released")

    def test_an_ipad_another_session_holds_past_the_wait_is_not_run(self):
        entry = self.record.entry("iPad")
        self.record.reserve("iPad", owner="session-b", revision=entry["revision"], purpose="its own run",
                            release_by=later())
        parts, code, _said = self.run_pass(["iPad"], self.command())
        self.assertEqual((code, parts["iPad"].outcome), (device_pass.UNMEASURED, "not run"))
        self.assertIn("stayed held or claimed by session-b", parts["iPad"].detail)
        self.assertEqual(self.ran(), [])
        self.assertEqual(self.record.entry("iPad")["ownerChatID"], "session-b")

    def test_a_command_that_names_another_device_runs_nowhere(self):
        parts, code, _said = self.run_pass(["iPad"], self.command(f"id={PHONE}"))
        self.assertEqual((code, parts["iPad"].outcome), (device_pass.UNMEASURED, "not run"))
        self.assertIn("names", parts["iPad"].detail)
        self.assertEqual(self.ran(), [])
        self.assertEqual(devices.condition(self.record.entry("iPad")), "released")

    def test_a_device_it_cannot_release_stays_held_and_the_pass_exits_2(self):
        def refuse(*_arguments, **_options): raise devices.Refused("iPad records launched apps")
        with patch.object(self.record, "release", refuse):
            parts, code, said = self.run_pass(["iPad"], self.command())
        self.assertEqual(code, 2)
        self.assertEqual((parts["iPad"].outcome, parts["iPad"].released), ("passed", False))
        self.assertIn("iPad stays held by session-a: iPad records launched apps", said)
        self.assertEqual(devices.condition(self.record.entry("iPad")), "held")

    def test_a_function_gives_each_device_its_own_command(self):
        self.hold_iphone()
        given = []
        def per_device(key, udid):
            given.append((key, udid))
            return self.command("landscape" if key == "iPhone" else "portrait")
        _parts, code, _said = self.run_pass(["iPad", "iPhone"], per_device)
        self.assertEqual(code, 0)
        self.assertEqual(given, [("iPad", TABLET), ("iPhone", PHONE)])
        self.assertEqual([call["words"] for call in self.ran()], [["portrait"], ["landscape"]])

    def test_the_apps_the_command_launches_are_recorded_and_their_quitting_released_with_the_device(self):
        self.hold_iphone()
        app = "org.example.probe"
        _parts, code, said = self.run_pass(["iPad", "iPhone"], self.command(), launched=[app],
                                           apps_quit="the command terminated them")
        self.assertEqual(code, 0, said)
        ipad = self.record.entry("iPad")
        self.assertEqual((devices.condition(ipad), ipad["launchedApps"], ipad["appsQuit"]),
                         ("released", [app], "the command terminated them"))
        # A device the session held keeps its record of them, for its own release.
        self.assertEqual(self.record.entry("iPhone")["launchedApps"], [app])
        # The negative control: launching apps without saying how they are quit runs nothing.
        with self.assertRaisesRegex(ValueError, "says how it quits them"):
            self.run_pass(["iPad"], self.command(), launched=[app])
        self.assertEqual(len(self.ran()), 2)

    def test_on_a_mac_whose_record_defers_devices_the_pass_refuses_naming_here(self):
        self.record.defer("Trevors-MacBook-Pro")
        with self.assertRaisesRegex(device_pass.Misplaced, r'give the lane "here"'):
            self.run_pass(["iPad"], self.command())
        self.assertEqual(self.ran(), [])

    def test_the_command_line(self):
        with contextlib.redirect_stderr(StringIO()) as error:
            code = cli.main(["device-pass", "--device", "iPad", "--owner", "session-a", "--wait", "0", "--",
                             *self.command()])
        self.assertEqual(code, 0, error.getvalue())
        self.assertIn("device-pass: iPad: passed: exit 0", error.getvalue())
        # An iPhone another session holds is not run.
        self.hold_iphone(owner="session-b")
        with contextlib.redirect_stderr(StringIO()) as error:
            code = cli.main(["device-pass", "--device", "iPhone", "--owner", "session-a", "--wait", "0", "--",
                             *self.command()])
        self.assertEqual(code, device_pass.UNMEASURED)
        # No command, no session, or a device that is no paired one: refused.
        with contextlib.redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            cli.main(["device-pass", "--device", "iPad"])
        with contextlib.redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            cli.main(["device-pass", "--device", "Duo", "--", "true"])
        with contextlib.redirect_stderr(StringIO()) as error:
            self.assertEqual(cli.main(["device-pass", "--device", "iPad", "--", "true"]), 2)
        self.assertIn("name the session", error.getvalue())
        self.assertEqual(len(self.ran()), 1)


# The device pass as a lane's command in a subprocess, with the device's log watch replaced, as qk run runs it.
LANE = ("import sys; sys.path.insert(0, {source!r}); from queuekeeper_apple import device_notifications, device_pass; "
        "device_notifications.run = lambda command: (0, ''); "
        "device_notifications.watch = lambda udid: type('W', (), {{'stop': lambda self: "
        "{{'watched': True, 'presented': 0}}}})(); raise SystemExit(device_pass.main(sys.argv[1:]))")


class ReleaseLane(Fixture):
    """A lane whose command is a device pass records a pass at the commit that eng release counts."""

    def project(self, *keys):
        root = self.root / "project"
        root.mkdir()
        lane = {"command": [sys.executable, "-c", LANE.format(source=str(SOURCE)), "--owner", "session-a",
                            "--wait", "0", *[word for key in keys for word in ("--device", key)], "--",
                            *self.command()], "minimumTests": len(keys)}
        (root / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Devices", "lanes": {
            "gate": {"command": [sys.executable, "-c", "pass"]}, "device-voiceover": lane},
            "release": {"lanes": ["device-voiceover"]}}))
        (root / ".gitignore").write_text(".build/\n")
        for words in (["init", "-q"], ["add", "queuekeeper.json", ".gitignore"],
                      ["-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "project"]):
            subprocess.run(["git", "-C", str(root), *words], check=True)
        commit = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
        return root, commit

    def run_lane(self, root):
        with patch.object(cli.remote, "passed", return_value=None), \
                contextlib.redirect_stdout(StringIO()), contextlib.redirect_stderr(StringIO()) as error:
            code = cli.main(["--project", str(root), "run", "device-voiceover"])
        return code, error.getvalue()

    def test_a_passing_device_lane_is_a_release_lanes_pass_at_the_commit(self):
        self.hold_iphone()
        root, commit = self.project("iPad", "iPhone")
        code, error = self.run_lane(root)
        self.assertEqual(code, 0, error)
        receipt = json.loads((root / ".build/queuekeeper/device-voiceover/source.json").read_text())
        self.assertEqual(receipt["outcome"], "passed")
        # The pass's summary is in the lane's results.
        summary = json.loads((root / ".build/queuekeeper/device-voiceover/results" / device_pass.SUMMARY).read_text())
        self.assertEqual([part["outcome"] for part in summary["devices"]], ["passed", "passed"])

    def test_a_device_lane_with_the_iphone_not_run_is_no_pass(self):
        self.hold_iphone(owner="session-b")
        root, commit = self.project("iPad", "iPhone")
        code, error = self.run_lane(root)
        self.assertEqual(code, device_pass.UNMEASURED, error)
        receipt = json.loads((root / ".build/queuekeeper/device-voiceover/source.json").read_text())
        self.assertEqual(receipt["outcome"], "unmeasured")


class Placement(unittest.TestCase):
    def test_a_device_pass_lane_needs_here_and_a_gate_that_runs_one_warns(self):
        command = ["python3", "scripts/qk", "device-pass", "--device", "iPad", "--", "run"]
        budgeted = {"budgetMinutes": 30, "timeout": 60}
        gate = {"command": ["true"], **budgeted}
        heavy = {"command": command, "resource": "heavy", "timeout": 60}
        [warning] = budgets.doctor_warnings({"lanes": {"gate": gate, "device": heavy}})
        self.assertIn("device runs a device pass with no \"here\"", warning)
        # A lane that gives no resource, or light, is admitted and goes to another Mac first too (the owner,
        # 2026-11-07).
        for lane in ({"command": command}, {"command": command, "resource": "light"}):
            [warning] = budgets.doctor_warnings({"lanes": {"gate": gate, "device": lane}})
            self.assertIn("device runs a device pass with no \"here\"", warning)
        # The negative controls: a lane that says here, and a self lane, which runs where it starts.
        self.assertEqual(budgets.doctor_warnings({"lanes": {"gate": gate, "device": {**heavy, "here": "a device"}}}), [])
        self.assertEqual(budgets.doctor_warnings({"lanes": {"gate": gate, "device": {"command": command,
                                                                                     "resource": "self"}}}), [])
        # A gate that runs a device pass takes a physical device, which makes it a release check.
        warned = budgets.doctor_warnings({"lanes": {"gate": {**gate, "command": command}}})
        self.assertTrue(any("gate takes a physical device" in warning for warning in warned), warned)


if __name__ == "__main__":
    unittest.main()
