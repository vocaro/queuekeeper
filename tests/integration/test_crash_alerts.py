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

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src/python"))
import host_isolation
from host_isolation import stretched
from queuekeeper import cli, lane_results, offload_host, remote
from queuekeeper_apple import devices
from queuekeeper_mac_desktop import crash_alerts, foreground
from queuekeeper_apple.voiceover import state as voiceover_state

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

    def __init__(self, value=None, alerts=(), pressable=True, readable=True, listable=True):
        self.value, self.alerts, self.pressable = value, list(alerts), pressable
        # Whether System Events answers a listing, which can be a count of listings it fails first, and whether
        # the window list can be read.
        self.readable, self.listable = readable, listable
        self.changes, self.pressed = [], []
        self.listings = self.glances = 0
        self.lock = threading.Lock()

    def run(self, command, environment=None):
        with self.lock:
            if command[:2] == ["defaults", "read"]: return self.value
            if command[:3] == ["osascript", "-l", "JavaScript"]:
                self.glances += 1
                self.assertGlance(command[-1])
                if not self.listable: return None
                return json.dumps([{"app": alert["app"], "layer": 8, "frame": alert["frame"]} for alert in self.alerts]
                                  + [{"app": "Finder", "layer": 0, "frame": [0, 0, 800, 600]}])
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
            self.listings += 1
            if self.readable is not True:
                if not self.readable: return None
                self.readable -= 1
                if self.readable == 0: self.readable = True
                return None
            return "".join("\t".join([alert["app"], *map(str, alert["frame"]), "\x1f".join(alert["texts"]) + "\x1f",
                                      "\x1f".join(alert["buttons"])]) + "\n" for alert in self.alerts)

    @staticmethod
    def assertGlance(script):
        # The window list alone, of the alert processes, and never a window's name.
        assert "CGWindowListCopyWindowInfo" in script and "kCGWindowName" not in script, script
        assert all(f'"{name}"' in script for name in offload_host.ALERT_PROCESSES), script

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
        for patcher in (patch.dict(os.environ, {"QUEUEKEEPER_CRASH_DIALOG_RECORD": str(self.root / "crash-dialog.json")}),
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

    def test_a_look_clears_more_than_ten_distinct_or_identical_crash_reports(self):
        for alerts in ([{**CRASH, "frame": [n, 20, 260, 250]} for n in range(25)], [CRASH] * 25):
            with self.subTest(identical=alerts[0] == alerts[-1]):
                self.host = Host(alerts=alerts)
                cleared, left = crash_alerts.clear(self.said.append)
                self.assertEqual(cleared, alerts)
                self.assertEqual(left, [])
                self.assertEqual(len(self.host.pressed), 25)

    def test_a_successful_press_that_leaves_or_returns_the_same_alert_is_bounded(self):
        with patch.object(crash_alerts, "press", return_value=True) as pressed:
            cleared, left = crash_alerts.clear(self.said.append, windows=lambda: [CRASH])
        self.assertEqual(left, [CRASH])
        self.assertEqual(len(cleared), crash_alerts.ATTEMPTS + 1)
        self.assertEqual(pressed.call_count, crash_alerts.ATTEMPTS + 1)

    def test_many_failed_presses_are_bounded_even_when_alerts_change(self):
        reads = []
        def windows():
            reads.append(True)
            return [{**CRASH, "frame": [len(reads), 20, 260, 250]}]
        with patch.object(crash_alerts, "press", return_value=False) as pressed:
            cleared, left = crash_alerts.clear(self.said.append, windows=windows)
        self.assertEqual(cleared, [])
        self.assertTrue(left)
        self.assertEqual(pressed.call_count, crash_alerts.ATTEMPTS)

    def test_a_mixed_stop_tells_the_owner_prompt_apart_from_the_crash_report(self):
        message = crash_alerts.stopping([CRASH, ACCESS])
        prompt, remaining = message.split("; also, ")
        self.assertIn("owner's to answer and queuekeeper never touches", prompt)
        self.assertIn(crash_alerts.describe(ACCESS), prompt)
        self.assertNotIn(crash_alerts.describe(CRASH), prompt)
        self.assertIn("a system alert queuekeeper cannot dismiss", remaining)
        self.assertIn(crash_alerts.describe(CRASH), remaining)


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
                                                              "appsUnderTest": [], "cleared": [], "left": [],
                                                              "looks": {"read": 1, "failed": 0, "glanced": 0}})
        self.assertTrue(any("crash-report dialogs off for the run" in line for line in self.said))
        self.assertTrue(any("put back crash-report dialogs" in line for line in self.said))
        # Dialogs the owner had off already are neither changed nor put back.
        self.host = Host(crash_alerts.OFF)
        with self.guarded(): pass
        self.assertEqual(self.host.changes, [])
        self.assertFalse(self.record.exists())

    def test_a_run_whose_eng_died_leaves_its_record_and_the_next_eng_puts_the_value_back(self):
        self.host = Host(crash_alerts.OFF)
        self.record.write_text(json.dumps({"before": "basic", "owner": "queuekeeper 1 for Project ui-mac", "pid": 999999,
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
        self.assertIn("queuekeeper: queuekeeper 1 for Project ui-mac, which no longer runs, left crash-report dialogs off", self.said)
        # queuekeeper's start does it, whatever the command.
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
        for alert, words in ((PASSWORD, "owner's to answer and queuekeeper never touches"), (ACCESS, "owner's to answer"),
                             (REOPEN_ONLY, "a system alert queuekeeper cannot dismiss")):
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
            self.assertEqual(run.judged(5), lane_results.UNMEASURED)
            self.assertEqual(run.judged(0), 0)
        self.assertIn("unmeasured: the run ended with status 5 while a password", error.getvalue())

    def test_a_driver_checks_between_steps(self):
        self.host.alerts = [CRASH]
        with contextlib.redirect_stderr(StringIO()):
            self.assertEqual(crash_alerts.main(["check"]), 0)
            self.host.alerts = [PASSWORD]
            self.assertEqual(crash_alerts.main(["check"]), lane_results.UNMEASURED)
            with patch.object(crash_alerts, "run", return_value=None):
                self.assertEqual(crash_alerts.main(["check"]), 1)
            self.assertEqual(crash_alerts.main([]), 2)


class WatchTests(HostTest):
    """Looks after a failed one, and the window list's glance before System Events (#225)."""

    def test_a_failed_first_look_still_starts_the_watcher_which_clears_a_later_alert(self):
        # The first listing fails, as a slow System Events call did on the second Mac on 2026-11-07.
        self.host = Host("basic", readable=1)
        with self.guarded(watch=0.02) as run:
            self.host.alerts.append(CRASH)
            deadline = time.monotonic() + stretched(5)
            while not self.host.pressed and time.monotonic() < deadline: threading.Event().wait(0.01)
        self.assertEqual(self.host.pressed, [("UserNotificationCenter", "Ignore")])
        self.assertEqual(sum("system alerts cannot be read" in line for line in self.said), 1)
        self.assertIn("queuekeeper looks again every 0.02 s", next(line for line in self.said if "cannot be read" in line))
        self.assertEqual(sum("system alerts can be read again" in line for line in self.said), 1)
        self.assertEqual(run.summary()["looks"]["failed"], 1)
        self.assertGreaterEqual(run.summary()["looks"]["read"], 1)

    def test_looks_that_keep_failing_are_said_once_and_a_failure_stands_saying_none_could_read(self):
        self.host = Host("basic", alerts=[CRASH], readable=False)
        run = crash_alerts.Run(self.said.append, None, ("EPUBLib",))
        self.assertIsNone(run.look(full=True))
        for _ in range(3): self.assertIsNone(run.look())
        self.assertEqual(sum("cannot be read" in line for line in self.said), 1)
        self.assertEqual(run.looks, {"read": 0, "failed": 4, "glanced": 0})
        with contextlib.redirect_stderr(StringIO()) as error:
            self.assertEqual(run.judged(5), 5)
        self.assertIn("queuekeeper: the run's status 5 stands, though no look during it could read the screen's system alerts",
                      error.getvalue())
        # A run whose looks read the screen says nothing of it.
        self.host = Host("basic")
        run = crash_alerts.Run(self.said.append, None, ("EPUBLib",))
        run.look(full=True)
        with contextlib.redirect_stderr(StringIO()) as error: self.assertEqual(run.judged(5), 5)
        self.assertEqual(error.getvalue(), "")

    def test_the_window_list_spares_a_system_events_look_while_it_shows_no_alert_window(self):
        now = [START]
        with patch.object(crash_alerts, "clock", lambda: now[0]):
            run = crash_alerts.Run(self.said.append, None)
            self.assertEqual(run.look(full=True), [])
            self.assertEqual(self.host.listings, 1)
            # Finder's window is in the list, and no alert process's is: System Events is not asked.
            for _ in range(3): self.assertEqual(run.look(), [])
            self.assertEqual((self.host.listings, self.host.glances), (1, 3))
            # A crash report's window in the list sends the look to System Events, which clears it.
            self.host.alerts = [CRASH]
            self.assertEqual(run.look(), [])
            # Listed once to find it and once to see it gone.
            self.assertEqual((self.host.listings, self.host.pressed), (3, [("UserNotificationCenter", "Ignore")]))
            # A window list that cannot be read does too.
            self.host.listable = False
            run.look()
            self.assertEqual(self.host.listings, 4)
            self.host.listable = True
            # And RECHECK s after System Events was last asked, it is asked anyway.
            now[0] += crash_alerts.RECHECK - 1
            run.look()
            self.assertEqual(self.host.listings, 4)
            now[0] += 2
            run.look()
            self.assertEqual(self.host.listings, 5)
        self.assertEqual(run.looks, {"read": 4, "failed": 0, "glanced": 4})
        # Windows given, as a test gives them, are always read.
        given = []
        run = crash_alerts.Run(self.said.append, lambda: given.append(True) or [])
        run.look(full=True)
        run.look()
        self.assertEqual(len(given), 2)

    def test_the_glance_reads_only_alert_processes_windows_of_some_extent(self):
        listed = json.dumps([{"app": "UserNotificationCenter", "layer": 8, "frame": [734, 202, 260, 318]},
                             {"app": "SecurityAgent", "layer": 1000, "frame": [600, 300, 400, 220]},
                             {"app": "UserNotificationCenter", "layer": 8, "frame": [0, 0, 0, 0]},
                             {"app": "Finder", "layer": 0, "frame": [0, 0, 800, 600]},
                             {"app": "UserNotificationCenter", "frame": [1, 2, 3, 4]}, "not a window"])
        with patch.object(crash_alerts, "run", return_value=listed):
            self.assertEqual(crash_alerts.glance(), [
                {"app": "UserNotificationCenter", "layer": 8, "frame": [734, 202, 260, 318]},
                {"app": "SecurityAgent", "layer": 1000, "frame": [600, 300, 400, 220]}])
        for answer in (None, "", "not json", "{}"):
            with self.subTest(answer), patch.object(crash_alerts, "run", return_value=answer):
                self.assertIsNone(crash_alerts.glance())
        Host.assertGlance(crash_alerts.GLANCE_SCRIPT)


OTHER = {**CRASH, "texts": ["Crasher944 quit unexpectedly.", "Click Reopen to open the application again."]}
UNDER_TEST = {**CRASH, "texts": ["EPUBLib quit unexpectedly.", "Click Reopen to open the application again."]}
NAMELESS = {**CRASH, "texts": ["An application crashed."]}
# 2026-11-07 04:46:44 UTC, when vocaro/EPUBLib#944's alert came (#226).
START = 1794026804.0


class ClearedAlertTests(HostTest):
    """A failed run during which queuekeeper cleared a crash-report alert (owner's decision, 2026-11-07, #226)."""

    def judged(self, alert, stood, apps=("EPUBLib",), status=65, during=True):
        """The status a run judges after one look cleared alert, whose press took stood seconds; the error
        printed and the run's summary."""
        now = [START]
        real = crash_alerts.press
        def press(target, button):
            now[0] += stood
            return real(target, button)
        with patch.object(crash_alerts, "clock", lambda: now[0]), patch.object(crash_alerts, "press", press):
            run = crash_alerts.Run(self.said.append, None, apps)
            run.running = during
            self.host.alerts = [alert]
            run.look()
            with contextlib.redirect_stderr(StringIO()) as error: result = run.judged(status)
        return result, error.getvalue(), run.summary()

    def test_the_process_an_alert_names_is_read_from_its_text(self):
        self.assertEqual(crash_alerts.crashed(OTHER), "Crasher944")
        self.assertEqual(crash_alerts.crashed(CRASH), "ColorPickerUIService")
        self.assertEqual(crash_alerts.crashed({**CRASH, "texts": ["The application Finder quit unexpectedly."]}),
                         "Finder")
        self.assertEqual(crash_alerts.crashed({**CRASH, "texts": ["“Mail” unexpectedly quit."]}), "Mail")
        self.assertIsNone(crash_alerts.crashed(NAMELESS))
        self.assertIsNone(crash_alerts.crashed({**CRASH, "texts": ["The application quit unexpectedly."]}))
        self.assertIsNone(crash_alerts.crashed({**CRASH, "texts": ["Click Reopen to open the application again."]}))
        self.assertIsNone(crash_alerts.crashed({**CRASH, "texts": []}))

    def test_another_process_s_alert_that_stood_longer_than_one_look_makes_a_failure_unmeasured(self):
        status, error, summary = self.judged(OTHER, 17)
        self.assertEqual(status, lane_results.UNMEASURED)
        self.assertIn("queuekeeper: unmeasured: the run ended with status 65 after queuekeeper cleared a crash-report alert during it "
                      "that was for Crasher944, not the app under test (EPUBLib), and stood 17.0 s from "
                      "2026-11-07T04:46:44+00:00, longer than one look (2 s)", error)
        self.assertIn("UserNotificationCenter's window at x 734, y 201, 260×250", error)
        self.assertEqual(summary["cleared"], [{"alert": crash_alerts.describe(OTHER), "process": "Crasher944",
                                               "start": "2026-11-07T04:46:44+00:00", "stoodSeconds": 17.0,
                                               "duringRun": True}])
        self.assertEqual(summary["appsUnderTest"], ["EPUBLib"])
        # The app under test is matched as the alert names it, in any case.
        self.assertEqual(self.judged(OTHER, 17, apps=("epublib",))[0], lane_results.UNMEASURED)

    def test_a_failure_stands_under_a_short_alert_one_for_the_app_under_test_or_one_eng_cannot_name(self):
        for alert, stood, apps, why in (
                (OTHER, 0.5, ("EPUBLib",), "was for Crasher944 and stood 0.5 s from 2026-11-07T04:46:44+00:00, "
                                           "within one look (2 s)"),
                (OTHER, crash_alerts.ONE_LOOK, ("EPUBLib",), "within one look"),
                (UNDER_TEST, 30, ("EPUBLib",), "was for EPUBLib, the app under test, whose crash is the run's failure"),
                (UNDER_TEST, 30, ("epublib",), "the app under test"),
                (NAMELESS, 30, ("EPUBLib",), "names no process queuekeeper can read, so queuekeeper counts it as the app under test's"),
                (OTHER, 30, (), "the run names no app under test")):
            with self.subTest(alert=alert["texts"][0], stood=stood, apps=apps):
                status, error, summary = self.judged(alert, stood, apps)
                self.assertEqual(status, 65, error)
                self.assertIn("queuekeeper: the run's status 65 stands: the crash-report alert queuekeeper cleared during it ", error)
                self.assertIn(why, error)
                self.assertNotIn("unmeasured", error)
                self.assertEqual(summary["cleared"][0]["stoodSeconds"], round(stood, 1))
                self.assertEqual(summary["cleared"][0]["process"], crash_alerts.crashed(alert))

    def test_an_app_under_test_crash_keeps_the_failure_when_another_alert_also_stood(self):
        for alerts in ((UNDER_TEST, OTHER), (OTHER, UNDER_TEST)):
            with self.subTest(alerts=alerts), contextlib.redirect_stderr(StringIO()) as error:
                run = crash_alerts.Run(self.said.append, lambda: [], ("epublib",))
                run.cleared = [{"alert": alert, "since": START, "at": START + 17, "during": True}
                               for alert in alerts]
                self.assertEqual(run.judged(65), 65)
                self.assertIn("was for EPUBLib, the app under test", error.getvalue())

    def test_a_passing_run_passed_and_an_alert_cleared_before_the_run_changes_nothing(self):
        status, error, summary = self.judged(OTHER, 30, status=0)
        self.assertEqual((status, error), (0, ""))
        self.assertEqual(summary["cleared"][0]["process"], "Crasher944")
        status, error, summary = self.judged(OTHER, 30, during=False)
        self.assertEqual((status, error), (65, ""))
        self.assertFalse(summary["cleared"][0]["duringRun"])
        # An unmeasured run stays unmeasured.
        self.assertEqual(self.judged(OTHER, 30, status=lane_results.UNMEASURED)[0], lane_results.UNMEASURED)

    def test_an_alert_stands_from_the_first_look_that_listed_it_and_afresh_once_it_has_gone(self):
        now = [START]
        with patch.object(crash_alerts, "clock", lambda: now[0]):
            run = crash_alerts.Run(self.said.append, None, ("EPUBLib",))
            run.running = True
            # One System Events could not press at the first look stands from it.
            self.host = Host(alerts=[OTHER], pressable=False)
            run.look()
            now[0] += 20
            self.host.pressable = True
            run.look()
            self.assertEqual(run.summary()["cleared"][0]["stoodSeconds"], 20)
            # The same alert again, later, stands from the look that met it again.
            now[0] += 100
            self.host.alerts = [OTHER]
            run.look()
            self.assertEqual(run.summary()["cleared"][1]["stoodSeconds"], 0)
            self.assertEqual(run.summary()["cleared"][1]["start"], "2026-11-07T04:48:44+00:00")


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
        (self.project / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Project", "lanes": {
            "ui-mac": lane(0), "broken-mac": lane(1), "tested-mac": {**lane(1), "apps": ["EPUBLib"]}}}))
        for patcher in (patch.dict(os.environ, {devices.OVERRIDE: str(self.device_record.path),
                                                "HEAVY_SLOT_DIR": str(self.root / "slots"), "HEAVY_POLL_SECONDS": "0.01",
                                                "QUEUEKEEPER_GATE_LOCK_FILE": str(self.root / "gate")}),
                        patch.object(cli, "source_identity", return_value={}),
                        patch.object(cli, "refresh_policy", return_value=None),
                        patch.object(voiceover_state, "_read", return_value=False),
                        # queuekeeper's guard around a foreground run always settles, for a stretched SETTLE. VoiceOver is
                        # a fake here that never changes, so no settle can show a late key: the real 5 s, stretched
                        # by load, held each run 40 to 120 s on a loaded host, and 0.3 s still 7 s at the stretch cap.
                        patch.object(voiceover_state, "SETTLE", 0.05),
                        patch.object(foreground, "front", return_value=None),
                        patch.object(foreground, "problems", return_value=[])):
            patcher.start()
            self.addCleanup(patcher.stop)
        os.environ.pop(devices.OWNER, None)

    def qk(self, *arguments):
        with contextlib.redirect_stderr(StringIO()) as error, contextlib.redirect_stdout(StringIO()):
            return cli.main(["--project", str(self.project), *arguments]), error.getvalue()

    def test_a_foreground_lane_clears_a_crash_report_and_passes_with_dialogs_put_back(self):
        self.host.alerts = [CRASH]
        status, error = self.qk("run", "--here", "ui-mac")
        self.assertEqual(status, 0, error)
        self.assertIn("dismissed a crash-report alert with Ignore", error)
        self.assertEqual((self.host.value, self.host.changes), ("basic", [crash_alerts.OFF, "basic"]))
        receipt = json.loads((self.project / ".build/queuekeeper/ui-mac/source.json").read_text())
        self.assertEqual(receipt["crashAlerts"]["dialogTypeBefore"], "basic")
        self.assertTrue(receipt["crashAlerts"]["restored"])
        # A failing lane fails, and dialogs are back.
        self.assertEqual(self.qk("run", "--here", "broken-mac")[0], 1)
        self.assertEqual(self.host.value, "basic")

    def test_an_owner_prompt_leaves_a_lane_and_admit_unmeasured_with_nothing_run(self):
        self.host.alerts = [PASSWORD]
        for arguments in (("run", "--here", "ui-mac"),
                          ("admit", "--foreground", "--", sys.executable, "-c", f"open({str(self.ran)!r}, 'a')")):
            with self.subTest(arguments[0]):
                status, error = self.qk(*arguments)
                self.assertEqual(status, lane_results.UNMEASURED, error)
                self.assertIn("password, permission or keychain prompt", error)
                self.assertIn("SecurityAgent's window at x 600, y 300, 400×220", error)
                self.assertFalse(self.ran.exists())
                self.assertEqual(self.host.value, "basic")
                # The foreground is released for the next run.
                self.assertEqual(self.device_record.entry(devices.FOREGROUND)["state"], "released")
        self.assertEqual(self.host.pressed, [])

    def test_a_lane_that_fails_under_an_alert_raised_during_it_is_unmeasured(self):
        real = crash_alerts.Run.look
        def look(run, **options):
            # The alert appears once the run has started: after the look before it.
            left = real(run, **options)
            self.host.alerts = [REOPEN_ONLY]
            return left
        with patch.object(crash_alerts.Run, "look", look):
            status, error = self.qk("run", "--here", "broken-mac")
            self.assertEqual(status, lane_results.UNMEASURED, error)
            self.assertIn("ended with status 1 while a system alert queuekeeper cannot dismiss", error)
            receipt = json.loads((self.project / ".build/queuekeeper/broken-mac/source.json").read_text())
            self.assertEqual(receipt["outcome"], "unmeasured")
            # A lane that passes under it passed.
            self.host.alerts = []
            self.assertEqual(self.qk("run", "--here", "ui-mac")[0], 0)
        self.assertTrue(self.ran.exists())

    def test_a_lane_that_fails_after_eng_cleared_another_process_s_long_alert_is_unmeasured(self):
        real, now = crash_alerts.Run.look, [START]
        def look(run, alert, **options):
            # The alert appears once the run has started, and its press takes 17 s.
            if run.running and not run.cleared: self.host.alerts = [alert]
            return real(run, **options)
        def press(target, button):
            now[0] += 17
            self.host.alerts = []
            return True
        for alert, lane, outcome in ((OTHER, "tested-mac", lane_results.UNMEASURED), (UNDER_TEST, "tested-mac", 1),
                                     (OTHER, "broken-mac", 1)):
            with self.subTest(alert=alert["texts"][0], lane=lane), \
                    patch.object(crash_alerts, "clock", lambda: now[0]), patch.object(crash_alerts, "press", press), \
                    patch.object(crash_alerts.Run, "look", lambda run, alert=alert, **options: look(run, alert,
                                                                                                     **options)):
                self.host.alerts = []
                status, error = self.qk("run", "--here", lane)
                self.assertEqual(status, outcome, error)
                receipt = json.loads((self.project / f".build/queuekeeper/{lane}/source.json").read_text())
                cleared = receipt["crashAlerts"]["cleared"]
                self.assertEqual([(entry["process"], entry["stoodSeconds"], entry["duringRun"]) for entry in cleared],
                                 [(crash_alerts.crashed(alert), 17.0, True)])
                self.assertEqual(receipt["crashAlerts"]["appsUnderTest"], ["EPUBLib"] if lane == "tested-mac" else [])
                if outcome == lane_results.UNMEASURED:
                    self.assertIn("after queuekeeper cleared a crash-report alert during it that was for Crasher944", error)
                    self.assertEqual(receipt["outcome"], "unmeasured")
                else:
                    self.assertIn("queuekeeper: the run's status 1 stands", error)

    def test_a_lanes_apps_are_names_of_a_foreground_lane(self):
        from queuekeeper import config
        command = {"command": ["true"], "resource": "heavy", "foreground": True}
        for lane, words in (({**command, "apps": []}, "apps names the app under test"),
                            ({**command, "apps": ["EPUBLib", ""]}, "apps names the app under test"),
                            ({**command, "apps": "EPUBLib"}, "apps names the app under test"),
                            ({**command, "apps": [" EPUBLib"]}, "apps names the app under test"),
                            ({**command, "foreground": False, "apps": ["EPUBLib"]}, "apps needs a foreground lane")):
            with self.subTest(lane):
                (self.project / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Project",
                                                                           "lanes": {"ui": lane}}))
                with self.assertRaisesRegex(ValueError, words): config.load(self.project)
        (self.project / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Project",
                                                                   "lanes": {"ui": {**command, "apps": ["EPUBLib"]}}}))
        self.assertEqual(config.load(self.project)["lanes"]["ui"]["apps"], ["EPUBLib"])
        schema = json.loads((config.TOOLKIT_ROOT / "schemas/queuekeeper.schema.json").read_text())
        self.assertEqual(schema["properties"]["lanes"]["additionalProperties"]["properties"]["apps"]["type"], "array")

    def test_admit_takes_the_app_under_test_and_remote_admit_sends_it(self):
        self.host.alerts = []
        now = [START]
        real = crash_alerts.Run.look
        def look(run, **options):
            if run.running and not run.cleared: self.host.alerts = [OTHER]
            return real(run, **options)
        def press(target, button):
            now[0] += 17
            self.host.alerts = []
            return True
        failing = [sys.executable, "-c", "raise SystemExit(4)"]
        with patch.object(crash_alerts, "clock", lambda: now[0]), patch.object(crash_alerts, "press", press), \
                patch.object(crash_alerts.Run, "look", look):
            status, error = self.qk("admit", "--foreground", "--app", "EPUBLib", "--", *failing)
            self.assertEqual(status, lane_results.UNMEASURED, error)
            self.assertIn("not the app under test (EPUBLib)", error)
            receipt = json.loads((self.project / ".build/queuekeeper/admit/source.json").read_text())
            self.assertEqual(receipt["crashAlerts"]["cleared"], [{
                "alert": crash_alerts.describe(OTHER), "process": "Crasher944",
                "start": "2026-11-07T04:46:44+00:00", "stoodSeconds": 17.0, "duringRun": True
            }])
            self.assertTrue(receipt["crashAlerts"]["restored"])
            status, error = self.qk("admit", "--foreground", "--app", "Crasher944", "--", *failing)
            self.assertEqual(status, 4, error)
        self.assertEqual(self.qk("admit", "--app", "EPUBLib", "--", "true")[0], 2)
        sent = []
        with patch.object(remote, "execute", side_effect=lambda root, config, host, work, **options:
                          sent.append(work) or 0), \
                patch.object(remote, "offering_here", side_effect=lambda here, send: send()):
            self.assertEqual(self.qk("remote", "admit", "--foreground", "--app", "EPUBLib", "--app", "EPUBLibUITests",
                                      "--", "true")[0], 0)
        self.assertEqual(sent, [["admit", "--foreground", "--app", "EPUBLib", "--app", "EPUBLibUITests", "--", "true"]])


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
        held = {"state": "held", "ownerChatID": "queuekeeper 5 on Medias-MacBook-Pro-4", "purpose": "ui-mac"}
        self.assertNotIn("crash-report", self.report({"value": "none", "record": {"before": "basic"}, "alerts": []},
                                                     held))
        self.assertIn("cannot be read", self.report({"value": "basic", "record": None, "alerts": None}))
        # A second Mac whose toolkit predates them says nothing of them.
        self.assertNotIn("alert", self.report(None))


if __name__ == "__main__":
    unittest.main()
