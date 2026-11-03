import contextlib
from io import StringIO
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/python"))
import host_isolation
from host_isolation import stretched
from vocaro_engineering import cli, crash_alerts, devices, evidence, foreground, offload_host, remote
from vocaro_engineering.voiceover import state as voiceover_state

SECOND = "Trevors-MacBook-Pro-715"
CRASH = {"app": "UserNotificationCenter", "frame": [734, 201, 260, 250],
         "texts": ["ColorPickerUIService quit unexpectedly.", "Click Reopen to open the application again."],
         "buttons": ["Ignore", "Report…", "Reopen"]}
REOPEN_ONLY = {**CRASH, "buttons": ["Report…", "Reopen"]}
PASSWORD = {"app": "SecurityAgent", "frame": [600, 300, 400, 220],
            "texts": ["Xcode wants to use the login keychain."], "buttons": ["Deny", "Allow"]}
ACCESS = {"app": "UserNotificationCenter", "frame": [600, 300, 400, 220],
          "texts": ["“Terminal” would like to access files in your Documents folder."], "buttons": ["Don't Allow", "OK"]}


class Host:
    """A Mac's DialogType and system alerts, behind the commands crash_alerts runs: defaults and osascript."""

    def __init__(self, value=None, alerts=(), pressable=True):
        self.value, self.alerts, self.pressable = value, list(alerts), pressable
        self.changes, self.pressed = [], []
        self.lock = threading.Lock()

    def run(self, command, environment=None):
        with self.lock:
            if command[:2] == ["defaults", "read"]: return self.value
            script = command[-1]
            if "click button" in script:
                button = re.search(r'click button "([^"]+)"', script).group(1)
                place = re.search(r"is \{(-?\d+), (-?\d+)\} and .* is \{(\d+), (\d+)\}", script).groups()
                for alert in self.alerts:
                    if [str(n) for n in alert["frame"]] == list(place) and self.pressable:
                        self.alerts.remove(alert)
                        self.pressed.append((alert["app"], button))
                        return "pressed"
                return ""
            return "".join("\t".join([alert["app"], *map(str, alert["frame"]), "\x1f".join(alert["texts"]) + "\x1f",
                                      "\x1f".join(alert["buttons"])]) + "\n" for alert in self.alerts)

    def change(self, value):
        self.changes.append(value)
        self.value = value
        return True


class HostTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.host = Host("basic")
        self.said = []
        for patcher in (patch.dict(os.environ, {"VOCARO_CRASH_DIALOG_RECORD": str(self.root / "crash-dialog.json")}),
                        patch.object(crash_alerts, "run", side_effect=lambda *a, **k: self.host.run(*a, **k)),
                        patch.object(crash_alerts, "change", side_effect=lambda value: self.host.change(value))):
            patcher.start()
            self.addCleanup(patcher.stop)

    @property
    def record(self):
        return self.root / "crash-dialog.json"

    def guarded(self, **options):
        return crash_alerts.guarded("Project ui-mac", said=self.said.append, watch=options.pop("watch", 60), **options)


class AlertTests(HostTest):
    def test_system_alerts_are_read_with_their_frames_texts_and_buttons(self):
        self.host.alerts = [CRASH, {**PASSWORD, "frame": [0, 0, 0, 0]}]
        self.assertEqual(offload_host.system_alerts(self.host.run), [CRASH])
        self.assertEqual(offload_host.system_alerts(lambda command: None), None)
        missing = "UserNotificationCenter\t1\t2\t3\t4\tmissing value\x1fGone\x1f\tOK\n"
        self.assertEqual(offload_host.system_alerts(lambda command: missing)[0]["texts"], ["Gone"])
        for name in offload_host.ALERT_PROCESSES: self.assertIn(f'"{name}"', offload_host.ALERTS_SCRIPT)

    def test_only_a_crash_report_is_dismissed_and_only_with_ignore_or_ok(self):
        self.assertEqual(crash_alerts.dismissal(CRASH), "Ignore")
        self.assertEqual(crash_alerts.dismissal({**CRASH, "buttons": ["OK"]}), "OK")
        # Reopen starts the app again and Report… opens a window: neither is pressed.
        self.assertIsNone(crash_alerts.dismissal(REOPEN_ONLY))
        # A password, permission or keychain prompt is the owner's, whatever process raised it and whatever it offers.
        self.assertIsNone(crash_alerts.dismissal(PASSWORD))
        self.assertIsNone(crash_alerts.dismissal(ACCESS))
        self.assertIsNone(crash_alerts.dismissal({**PASSWORD, "app": "UserNotificationCenter",
                                                  "texts": ["Xcode quit unexpectedly. Enter your password."]}))
        self.assertIsNone(crash_alerts.dismissal({**CRASH, "texts": ["The disk was not ejected properly."]}))
        self.assertTrue(crash_alerts.owners(PASSWORD) and crash_alerts.owners(ACCESS))
        self.assertIn("x 734, y 201, 260×250", crash_alerts.describe(CRASH))

    def test_clearing_dismisses_each_crash_report_and_leaves_every_other_alert(self):
        self.host.alerts = [CRASH, {**CRASH, "frame": [10, 20, 260, 250]}, ACCESS]
        cleared, left = crash_alerts.clear(self.said.append)
        self.assertEqual(len(cleared), 2)
        self.assertEqual(left, [ACCESS])
        self.assertEqual(self.host.pressed, [("UserNotificationCenter", "Ignore")] * 2)
        self.assertEqual(sum("dismissed a crash-report alert with Ignore" in line for line in self.said), 2)
        # One another press took first is gone, not left: a run's watcher and a driver's check both press it.
        stale = [[CRASH], []]
        self.host = Host()
        self.assertEqual(crash_alerts.clear(self.said.append, windows=lambda: stale.pop(0)), ([], []))
        # One System Events cannot press stays.
        self.host = Host(alerts=[CRASH], pressable=False)
        self.assertEqual(crash_alerts.clear(self.said.append), ([], [CRASH]))


class GuardTests(HostTest):
    def test_dialogs_are_off_only_for_the_run_and_put_back_exactly_however_it_ends(self):
        for before in ("basic", "developer", None):
            for ending in ("passed", "raised"):
                with self.subTest(before=before, ending=ending):
                    self.host = Host(before)
                    receipt = {}
                    with contextlib.suppress(RuntimeError), self.guarded(receipt=receipt):
                        self.assertEqual(self.host.value, crash_alerts.OFF)
                        self.assertEqual(json.loads(self.record.read_text())["before"], before)
                        if ending == "raised": raise RuntimeError("interrupted")
                    self.assertEqual(self.host.value, before)
                    self.assertEqual(self.host.changes, [crash_alerts.OFF, before])
                    self.assertFalse(self.record.exists())
                    self.assertEqual(receipt["crashAlerts"], {"dialogTypeBefore": before, "restored": True,
                                                              "cleared": [], "left": []})
        self.assertTrue(any("crash-report dialogs off for the run" in line for line in self.said))
        self.assertTrue(any("put back crash-report dialogs" in line for line in self.said))
        # Dialogs the owner had off already are neither changed nor put back.
        self.host = Host(crash_alerts.OFF)
        with self.guarded(): pass
        self.assertEqual(self.host.changes, [])
        self.assertFalse(self.record.exists())

    def test_a_run_whose_eng_died_leaves_its_record_and_the_next_eng_puts_the_value_back(self):
        self.host = Host(crash_alerts.OFF)
        self.record.write_text(json.dumps({"before": "basic", "owner": "eng 1 for Project ui-mac", "pid": 999999,
                                           "start": "Mon Nov  2 09:00:00 2026"}))
        # A run that still runs keeps its own.
        with patch.object(crash_alerts.resources, "process_start", return_value="Mon Nov  2 09:00:00 2026"):
            crash_alerts.recover(self.said.append)
        self.assertEqual(self.host.value, crash_alerts.OFF)
        self.assertTrue(self.record.exists())
        with patch.object(crash_alerts.resources, "process_start", return_value=None):
            crash_alerts.recover(self.said.append)
        self.assertEqual(self.host.value, "basic")
        self.assertFalse(self.record.exists())
        self.assertIn("eng: eng 1 for Project ui-mac, which no longer runs, left crash-report dialogs off", self.said)
        # eng's start does it, whatever the command.
        self.record.write_text(json.dumps({"before": None, "pid": 999999, "start": "earlier"}))
        self.host.value = crash_alerts.OFF
        with patch.object(crash_alerts.resources, "process_start", return_value=None), \
                contextlib.redirect_stderr(StringIO()) as error, contextlib.redirect_stdout(StringIO()):
            cli.main(["--project", str(self.root), "admissions"])
        self.assertIsNone(self.host.value)
        self.assertIn("put back crash-report dialogs: com.apple.CrashReporter DialogType is unset again",
                      error.getvalue())

    def test_a_key_already_gone_is_put_back(self):
        # defaults delete fails on a key that is not there, and the value read back is what counts.
        self.host = Host(None)
        with patch.object(crash_alerts, "change", side_effect=lambda value: self.host.change(value) and value is not None):
            with self.guarded(): self.host.value = None
        self.assertFalse(self.record.exists())

    def test_a_value_that_does_not_read_back_keeps_its_record_for_the_next_eng(self):
        with patch.object(crash_alerts, "change", side_effect=lambda value: value == crash_alerts.OFF and
                          self.host.change(value)):
            with self.guarded(): pass
        self.assertEqual(json.loads(self.record.read_text())["before"], "basic")
        self.assertTrue(any("could not put back" in line for line in self.said))
        with patch.object(crash_alerts.resources, "process_start", return_value=None):
            crash_alerts.recover(self.said.append)
        self.assertEqual(self.host.value, "basic")

    def test_a_crash_report_before_the_run_is_dismissed_and_the_run_goes_ahead(self):
        self.host.alerts = [CRASH]
        receipt = {}
        with self.guarded(receipt=receipt) as run: self.assertEqual(run.judged(0), 0)
        self.assertEqual(self.host.pressed, [("UserNotificationCenter", "Ignore")])
        self.assertEqual(len(receipt["crashAlerts"]["cleared"]), 1)

    def test_an_alert_eng_may_not_clear_stops_the_run_unmeasured_before_it_starts(self):
        for alert, words in ((PASSWORD, "owner's to answer and eng never touches"), (ACCESS, "owner's to answer"),
                             (REOPEN_ONLY, "a system alert eng cannot dismiss")):
            with self.subTest(alert["texts"][0]):
                self.host = Host("basic", [alert])
                ran = []
                with self.assertRaisesRegex(crash_alerts.Blocked, words):
                    with self.guarded(): ran.append(True)
                self.assertEqual((ran, self.host.pressed, self.host.value), ([], [], "basic"))
                self.assertFalse(self.record.exists())

    def test_an_alert_raised_during_the_run_is_cleared_or_makes_a_failure_unmeasured(self):
        with self.guarded(watch=0.02) as run:
            self.host.alerts.append(CRASH)
            deadline = time.monotonic() + stretched(5)
            while not self.host.pressed and time.monotonic() < deadline: threading.Event().wait(0.01)
        self.assertEqual(self.host.pressed, [("UserNotificationCenter", "Ignore")])
        self.assertEqual(run.judged(5), 5)
        with self.guarded(watch=0.02) as run:
            self.host.alerts.append(PASSWORD)
            deadline = time.monotonic() + stretched(5)
            while not run.left and time.monotonic() < deadline: threading.Event().wait(0.01)
        self.assertEqual(self.host.alerts, [PASSWORD])
        with contextlib.redirect_stderr(StringIO()) as error:
            self.assertEqual(run.judged(5), evidence.UNMEASURED)
            self.assertEqual(run.judged(0), 0)
        self.assertIn("unmeasured: the run ended with status 5 while a password", error.getvalue())

    def test_a_driver_checks_between_steps(self):
        self.host.alerts = [CRASH]
        with contextlib.redirect_stderr(StringIO()):
            self.assertEqual(crash_alerts.main(["check"]), 0)
            self.host.alerts = [PASSWORD]
            self.assertEqual(crash_alerts.main(["check"]), evidence.UNMEASURED)
            with patch.object(crash_alerts, "run", return_value=None):
                self.assertEqual(crash_alerts.main(["check"]), 1)
            self.assertEqual(crash_alerts.main([]), 2)


class LaneTests(HostTest):
    def setUp(self):
        super().setUp()
        self.project = self.root / "project"
        self.project.mkdir()
        self.device_record = devices.Record(self.root / "device-handoff.json")
        self.device_record.defer(SECOND)
        self.ran = self.root / "ran"
        lane = lambda status: {"command": [sys.executable, "-c", f"open({str(self.ran)!r}, 'a').write('ran\\n'); "
                                           f"print('Ran 1 tests'); raise SystemExit({status})"],
                               "resource": "heavy", "foreground": True}
        (self.project / "engineering.json").write_text(json.dumps({"schemaVersion": 1, "name": "Project", "lanes": {
            "ui-mac": lane(0), "broken-mac": lane(1)}}))
        for patcher in (patch.dict(os.environ, {devices.OVERRIDE: str(self.device_record.path),
                                                "HEAVY_SLOT_DIR": str(self.root / "slots"), "HEAVY_POLL_SECONDS": "0.01",
                                                "SW_GATE_LOCK_FILE": str(self.root / "gate")}),
                        patch.object(cli, "source_identity", return_value={}),
                        patch.object(cli, "refresh_policy", return_value=None),
                        patch.object(voiceover_state, "_read", return_value=False),
                        patch.object(foreground, "front", return_value=None),
                        patch.object(foreground, "problems", return_value=[])):
            patcher.start()
            self.addCleanup(patcher.stop)
        os.environ.pop(devices.OWNER, None)

    def eng(self, *arguments):
        with contextlib.redirect_stderr(StringIO()) as error, contextlib.redirect_stdout(StringIO()):
            return cli.main(["--project", str(self.project), *arguments]), error.getvalue()

    def test_a_foreground_lane_clears_a_crash_report_and_passes_with_dialogs_put_back(self):
        self.host.alerts = [CRASH]
        status, error = self.eng("run", "--here", "ui-mac")
        self.assertEqual(status, 0, error)
        self.assertIn("dismissed a crash-report alert with Ignore", error)
        self.assertEqual((self.host.value, self.host.changes), ("basic", [crash_alerts.OFF, "basic"]))
        receipt = json.loads((self.project / ".build/engineering/ui-mac/source.json").read_text())
        self.assertEqual(receipt["crashAlerts"]["dialogTypeBefore"], "basic")
        self.assertTrue(receipt["crashAlerts"]["restored"])
        # A failing lane fails, and dialogs are back.
        self.assertEqual(self.eng("run", "--here", "broken-mac")[0], 1)
        self.assertEqual(self.host.value, "basic")

    def test_an_owner_prompt_leaves_a_lane_and_admit_unmeasured_with_nothing_run(self):
        self.host.alerts = [PASSWORD]
        for arguments in (("run", "--here", "ui-mac"),
                          ("admit", "--foreground", "--", sys.executable, "-c", f"open({str(self.ran)!r}, 'a')")):
            with self.subTest(arguments[0]):
                status, error = self.eng(*arguments)
                self.assertEqual(status, evidence.UNMEASURED, error)
                self.assertIn("password, permission or keychain prompt", error)
                self.assertIn("SecurityAgent's window at x 600, y 300, 400×220", error)
                self.assertFalse(self.ran.exists())
                self.assertEqual(self.host.value, "basic")
                # The foreground is released for the next run.
                self.assertEqual(self.device_record.entry(devices.FOREGROUND)["state"], "released")
        self.assertEqual(self.host.pressed, [])

    def test_a_lane_that_fails_under_an_alert_raised_during_it_is_unmeasured(self):
        real = crash_alerts.Run.look
        def look(run):
            # The alert appears once the run has started: after the look before it.
            left = real(run)
            self.host.alerts = [REOPEN_ONLY]
            return left
        with patch.object(crash_alerts.Run, "look", look):
            status, error = self.eng("run", "--here", "broken-mac")
            self.assertEqual(status, evidence.UNMEASURED, error)
            self.assertIn("ended with status 1 while a system alert eng cannot dismiss", error)
            receipt = json.loads((self.project / ".build/engineering/broken-mac/source.json").read_text())
            self.assertEqual(receipt["outcome"], "unmeasured")
            # A lane that passes under it passed.
            self.host.alerts = []
            self.assertEqual(self.eng("run", "--here", "ui-mac")[0], 0)
        self.assertTrue(self.ran.exists())


class ReportTests(unittest.TestCase):
    def report(self, crash, entry=None):
        found = {"mac": "Medias-MacBook-Pro-4", "problems": [], "entry": entry, "crashDialogs": crash}
        with patch.object(remote, "ask", return_value=found), contextlib.redirect_stdout(StringIO()) as output:
            self.assertEqual(remote.foreground_report("build-host"), 0)
        return output.getvalue()

    def test_eng_remote_foreground_reports_leftover_alerts_and_dialogs_left_off(self):
        said = self.report({"value": "basic", "record": None, "alerts": [CRASH]})
        self.assertIn("a system alert is over the screen: UserNotificationCenter's window at x 734, y 201, 260×250: "
                      "'ColorPickerUIService quit unexpectedly.'", said)
        said = self.report({"value": "none", "record": None, "alerts": []})
        self.assertIn("crash-report dialogs are off with no run going", said)
        said = self.report({"value": "none", "record": {"before": "basic"}, "alerts": []})
        self.assertIn("puts back com.apple.CrashReporter DialogType basic", said)
        # While a run holds the foreground, dialogs off are that run's.
        held = {"state": "held", "ownerChatID": "eng 5 on Medias-MacBook-Pro-4", "purpose": "ui-mac"}
        self.assertNotIn("crash-report", self.report({"value": "none", "record": {"before": "basic"}, "alerts": []},
                                                     held))
        self.assertIn("cannot be read", self.report({"value": "basic", "record": None, "alerts": None}))
        # A second Mac whose toolkit predates them says nothing of them.
        self.assertNotIn("alert", self.report(None))


if __name__ == "__main__":
    unittest.main()
