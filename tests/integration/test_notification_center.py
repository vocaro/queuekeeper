import contextlib
from io import StringIO
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src/python"))
import host_isolation
from test_support import farm as farm_fixture
from queuekeeper import cli, lane_results, offload_host
from queuekeeper_mobile import devices
from queuekeeper_mac_desktop import reservations
from queuekeeper_mac_desktop import foreground, notification_center
from queuekeeper_apple.voiceover import state as voiceover_state

SECOND = "Trevors-MacBook-Pro-715"
CENTER, OTHER = 754, 12136
# The window macOS 27.2's Notification Center shows a notification in: the whole screen, at layer 23.
WINDOW = {"number": 29, "pid": CENTER, "layer": 23, "alpha": 1, "frame": [0, 0, 1728, 1117]}
BANNER = {"kind": "AXNotificationCenterBanner", "frame": [1380, 40, 340, 72],
          "actions": ["AXPress", "Name:Close\nTarget:0x0\nSelector:(null)", "Name:Show Details\nTarget:0x0"],
          "texts": ["Calendar", "Standup in 5 minutes"]}
ALERT = {**BANNER, "kind": "AXNotificationCenterAlert", "frame": [1380, 120, 340, 96]}
STACK = {"kind": "AXNotificationCenterBannerStack", "frame": [1380, 40, 340, 90],
         "actions": ["AXPress", "Name:Clear All\nTarget:0x0"], "texts": ["Mail", "3 notifications"]}
PERMISSION = {**BANNER, "texts": ["“Helper” would like to access your contacts."]}


class Host:
    """A Mac's on-screen windows, their processes and Notification Center's notifications, behind the commands
    notification_center runs: osascript (JavaScript for the window list, AppleScript for the notifications) and
    ps. A window leaves the screen once its process presents no notification, unless it sticks."""

    EXECUTABLES = {CENTER: notification_center.EXECUTABLE,
                   OTHER: "/System/Library/CoreServices/UserNotificationCenter.app/Contents/MacOS/UserNotificationCenter"}

    def __init__(self, windows=(), notifications=(), sticks=False, closes=True):
        self.windows, self.notifications = [dict(w) for w in windows], [dict(n) for n in notifications]
        self.sticks, self.closes = sticks, closes
        self.commands, self.closed = [], []

    def run(self, command, environment=None):
        self.commands.append(command)
        if command[:3] == ["osascript", "-l", "JavaScript"]:
            if not self.sticks and not self.notifications:
                self.windows = [w for w in self.windows if (w["pid"], w["layer"]) != (CENTER, 23)]
            return json.dumps(self.windows)
        if command[:2] == ["ps", "-p"]: return self.EXECUTABLES.get(int(command[2]))
        script = command[-1]
        assert int(re.search(r"unix id is (\d+)", script).group(1)) == CENTER
        if "perform action_" in script:
            kind = re.search(r'kind_ is "([^"]+)"', script).group(1)
            place = [int(n) for n in re.search(r"is \{(\d+), (\d+)\} and .* is \{(\d+), (\d+)\}", script).groups()]
            for notification in self.notifications:
                if notification["kind"] == kind and notification["frame"] == place:
                    if "Name:Close" not in "".join(notification["actions"]) or not self.closes: return "none"
                    self.notifications.remove(notification)
                    self.closed.append(kind)
                    return "closed"
            return "gone"
        return "".join("\t".join([n["kind"], *map(str, n["frame"]), "\x1f".join(a.split("\n")[0] for a in n["actions"]) + "\x1f",
                                  "\x1f".join(n["texts"]) + "\x1f"]) + "\n" for n in self.notifications)

    @property
    def pressed(self):
        return [command for command in self.commands if "perform action_" in command[-1]]


class HostTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.host = Host()
        self.said = []
        self.record = devices.Record(self.root / "device-handoff.json")
        self.record.defer(SECOND)
        farm_fixture.takes_foreground(self)
        for patcher in (patch.object(notification_center, "run", side_effect=lambda *a, **k: self.host.run(*a, **k)),
                        patch.object(notification_center, "trusted", return_value=True),
                        patch.object(notification_center, "STEP", 0.01),
                        patch.object(voiceover_state, "_read", return_value=False),
                        patch.object(voiceover_state, "SETTLE", 0.05),
                        patch.object(foreground, "front", return_value=None)):
            patcher.start()
            self.addCleanup(patcher.stop)
        os.environ.pop(devices.OWNER, None)

    @contextlib.contextmanager
    def held(self):
        """Another Mac's foreground, held as queuekeeper holds it for a run."""
        with foreground.slot("Project ui-mac", timeout=1, record=self.record, ready=lambda: [],
                             said=lambda text: None) as owner:
            yield owner

    def dismiss(self, **options):
        return notification_center.dismiss(self.said.append, record=self.record, wait=options.pop("wait", 0.05),
                                           **options)

    def assert_no_content(self, *texts):
        for text in texts:
            for notification in (BANNER, PERMISSION, STACK):
                for said in notification["texts"]: self.assertNotIn(said, text)


class WindowTests(HostTest):
    def test_only_notification_centers_own_window_at_layer_23_on_the_screen_counts(self):
        self.host.windows = [WINDOW,
                             {**WINDOW, "number": 30, "layer": 0},                    # an ordinary window of its own
                             {**WINDOW, "number": 31, "alpha": 0},                    # invisible
                             {**WINDOW, "number": 32, "frame": [0, 0, 0, 0]},         # of no extent
                             {**WINDOW, "number": 33, "pid": OTHER},                  # UserNotificationCenter's
                             {**WINDOW, "number": 34, "pid": 4242}]                   # a process ps cannot name
        self.host.notifications = [BANNER]
        self.assertEqual(notification_center.windows(),
                         [{"number": 29, "pid": CENTER, "layer": 23, "frame": [0, 0, 1728, 1117]}])
        self.assertIn("osascript", self.host.commands[0])

    def test_an_unreadable_window_list_is_none(self):
        with patch.object(notification_center, "run", return_value=None):
            self.assertIsNone(notification_center.windows())
        with patch.object(notification_center, "run", return_value="not json"):
            self.assertIsNone(notification_center.windows())

    def test_notifications_are_read_with_their_kinds_frames_and_close_actions(self):
        self.host.notifications = [BANNER, STACK]
        found = notification_center.notifications(CENTER)
        self.assertEqual([(n["kind"], n["frame"]) for n in found],
                         [(BANNER["kind"], BANNER["frame"]), (STACK["kind"], STACK["frame"])])
        self.assertEqual(found[0]["actions"], ["AXPress", "Close", "Show Details"])
        self.assertEqual([notification_center.closable(n) for n in found], [True, False])

    def test_the_scripts_set_no_variable_that_a_system_events_term_takes(self):
        # Inside `tell application "System Events"`, `kind` is a property of its own, so `set kind to ""` failed
        # with -10006 on another Mac on 2026-11-07 and no notification could be read (#204), and `item 1 of
        # (position of b)` fails to be a specifier. Every variable the scripts set ends in `_`, and each position
        # is kept in a variable before an item of it is read.
        for name, script in (("ELEMENTS_SCRIPT", notification_center.ELEMENTS_SCRIPT),
                             ("CLOSE_SCRIPT", notification_center.CLOSE_SCRIPT)):
            names = set(re.findall(r"\bset (\w+) to\b", script)) | set(re.findall(r"\brepeat with (\w+) in\b", script))
            self.assertEqual({n for n in names if not n.endswith("_") and n not in ("out", "proc")}, set(), name)
            self.assertNotIn("of (position of", script, name)
            # `entire contents` of the window listed no banner there either; the scripts walk UI elements.
            self.assertNotIn("entire contents", script, name)


class DismissTests(HostTest):
    def test_a_notification_window_is_closed_and_confirmed_gone_under_the_lease(self):
        self.host.windows, self.host.notifications = [WINDOW], [BANNER, ALERT]
        with self.held():
            report = self.dismiss()
        self.assertEqual(self.host.closed, [BANNER["kind"], ALERT["kind"]])
        self.assertEqual(notification_center.windows(), [])
        self.assertEqual(report["found"], ["Notification Center's window 29 (pid 754, layer 23) at x 0, y 0, 1728×1117"])
        self.assertEqual(report["closed"], ["banner at x 1380, y 40, 340×72 in window 29",
                                            "alert at x 1380, y 120, 340×96 in window 29"])
        self.assertIn("queuekeeper: closed a Notification Center banner at x 1380, y 40, 340×72", self.said[0])
        # Only Close: never AXPress, which opens a notification, nor Clear All.
        for command in self.host.pressed:
            self.assertIn('label_ is "Close"', command[-1])
            self.assertNotIn("Clear All", command[-1])
        self.assert_no_content(json.dumps(report), *self.said)

    def test_no_window_does_nothing_and_needs_no_lease(self):
        # An unrelated window is left alone: another app's, at another layer.
        self.host.windows = [{**WINDOW, "number": 40, "layer": 0}, {**WINDOW, "number": 41, "pid": OTHER}]
        report = self.dismiss()
        self.assertEqual(report, {"found": [], "closed": []})
        self.assertEqual(self.host.pressed, [])
        self.assertFalse(any(c[:2] == ["osascript", "-e"] for c in self.host.commands))
        self.assertEqual(len(self.host.windows), 2)

    def test_a_window_with_no_lease_held_is_refused_and_left_alone(self):
        self.host.windows, self.host.notifications = [WINDOW], [BANNER]
        with self.assertRaisesRegex(devices.Refused, "name the session whose mac-foreground"):
            self.dismiss()
        with self.assertRaisesRegex(devices.Refused, "nobody holds mac-foreground"):
            self.dismiss(owner="queuekeeper 1 on another Mac")
        # Another run's lease is not this one's.
        with self.held():
            with self.assertRaisesRegex(devices.Refused, "held by queuekeeper"):
                self.dismiss(owner="another session")
        # Nor on a Mac whose own settings do not say it takes foreground work, a workstation, though its record
        # defers physical devices as this one's does (vocaro/queuekeeper#211).
        offload_host.mark_foreground(False)
        with self.assertRaisesRegex(devices.Refused, "only on a Mac whose own settings say it takes foreground work"):
            notification_center.dismiss(self.said.append, record=self.record, owner="anyone")
        self.assertEqual(self.host.pressed, [])
        self.assertEqual(self.host.notifications, [BANNER])

    def test_a_window_that_stays_fails_loudly_naming_it(self):
        self.host.windows, self.host.notifications, self.host.sticks = [WINDOW], [BANNER], True
        with self.held():
            with self.assertRaises(notification_center.Blocked) as caught:
                self.dismiss()
        self.assertIn("Notification Center's window 29 (pid 754, layer 23) at x 0, y 0, 1728×1117", str(caught.exception))
        # The banner went; the window did not, and nothing else is there to close.
        self.assertIn("something queuekeeper may not close (no notification)", str(caught.exception))
        # A window whose notification will not close is named as well.
        self.host.notifications, self.host.closes = [BANNER], False
        with self.held():
            with self.assertRaisesRegex(notification_center.Blocked, "window 29 .*did not close its banner"):
                self.dismiss()

    def test_a_stack_a_permission_request_or_an_unreadable_window_is_left_for_the_owner(self):
        for notifications, expected in (([STACK], "never Clear All"),
                                        ([BANNER, STACK], "presents nothing else"),
                                        ([PERMISSION], "password, permission or access"),
                                        ([BANNER, PERMISSION], "password, permission or access")):
            with self.subTest(expected=expected, count=len(notifications)):
                self.host = Host([WINDOW], notifications)
                with self.held():
                    with self.assertRaises(notification_center.Blocked) as caught:
                        self.dismiss()
                self.assertIn(expected, str(caught.exception))
                self.assertIn("window 29", str(caught.exception))
                self.assertEqual(self.host.pressed, [])
                self.assert_no_content(str(caught.exception))
        self.host = Host([WINDOW], [BANNER])
        with self.held():
            with patch.object(notification_center, "trusted", return_value=False):
                with self.assertRaisesRegex(notification_center.Blocked, "not trusted for accessibility"):
                    self.dismiss()
            with patch.object(notification_center, "notifications", return_value=None):
                with self.assertRaisesRegex(notification_center.Blocked, "cannot read its notifications"):
                    self.dismiss()
        self.assertEqual(self.host.pressed, [])

    def test_an_unreadable_window_list_is_never_taken_for_a_clear_screen(self):
        with patch.object(notification_center, "run", return_value=None):
            with self.assertRaisesRegex(notification_center.Blocked, "window list cannot be read"):
                self.dismiss()

    def test_the_module_lists_and_checks(self):
        self.host.windows, self.host.notifications = [WINDOW], [BANNER]
        with contextlib.redirect_stdout(StringIO()) as out, contextlib.redirect_stderr(StringIO()) as error:
            self.assertEqual(notification_center.main(["list"]), lane_results.UNMEASURED)
            self.assertIn("window 29", out.getvalue())
            # check with no lease refuses and closes nothing.
            with patch.object(reservations, "Record", return_value=self.record):
                self.assertEqual(notification_center.main(["check"]), 1)
                self.assertEqual(self.host.pressed, [])
                with self.held():
                    self.assertEqual(notification_center.main(["check"]), 0)
            self.assertEqual(notification_center.main(["list"]), 0)
            self.assertEqual(notification_center.main(["other"]), 2)
        self.assertIn("name the session whose mac-foreground reservation", error.getvalue())


class LaneTests(HostTest):
    def setUp(self):
        super().setUp()
        self.project = self.root / "project"
        self.project.mkdir()
        self.ran = self.root / "ran"
        lane = {"command": [sys.executable, "-c", f"open({str(self.ran)!r}, 'a').write('ran\\n'); print('Ran 1 tests')"],
                "resource": "heavy", "foreground": True}
        (self.project / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Project",
                                                                   "lanes": {"ui-mac": lane}}))
        for patcher in (patch.dict(os.environ, {devices.OVERRIDE: str(self.record.path),
                                                "HEAVY_SLOT_DIR": str(self.root / "slots"), "HEAVY_POLL_SECONDS": "0.01",
                                                "QUEUEKEEPER_GATE_LOCK_FILE": str(self.root / "gate")}),
                        patch.object(notification_center, "GONE", 0.05),
                        patch.object(cli, "source_identity", return_value={}),
                        patch.object(cli, "refresh_policy", return_value=None),
                        patch.object(foreground, "problems", return_value=[])):
            patcher.start()
            self.addCleanup(patcher.stop)
        # In the namespace the lanes run in: this Mac takes foreground work, by its own settings.
        farm_fixture.takes_foreground(self)

    def qk(self, *arguments):
        with contextlib.redirect_stderr(StringIO()) as error, contextlib.redirect_stdout(StringIO()):
            return cli.main(["--project", str(self.project), *arguments]), error.getvalue()

    def test_a_foreground_lane_closes_a_notification_and_then_runs(self):
        self.host.windows, self.host.notifications = [WINDOW, {**WINDOW, "number": 50, "layer": 0}], [BANNER]
        status, error = self.qk("run", "--here", "ui-mac")
        self.assertEqual(status, 0, error)
        self.assertTrue(self.ran.exists())
        self.assertIn("closed a Notification Center banner", error)
        receipt = json.loads((self.project / ".build/queuekeeper/ui-mac/source.json").read_text())
        self.assertEqual(len(receipt["notificationCenter"]["closed"]), 1)
        # The unrelated window stays.
        self.assertEqual([w["number"] for w in self.host.windows], [50])
        self.assert_no_content(error, json.dumps(receipt))

    def test_a_window_that_stays_leaves_a_lane_and_admit_unmeasured_with_nothing_run(self):
        self.host.windows, self.host.notifications, self.host.sticks = [WINDOW], [STACK], True
        for arguments in (("run", "--here", "ui-mac"),
                          ("admit", "--foreground", "--", sys.executable, "-c", f"open({str(self.ran)!r}, 'a')")):
            with self.subTest(arguments[0]):
                status, error = self.qk(*arguments)
                self.assertEqual(status, lane_results.UNMEASURED, error)
                self.assertIn("Notification Center's window 29 (pid 754, layer 23)", error)
                self.assertIn("nothing ran", error)
                self.assertFalse(self.ran.exists())
                # The foreground is released for the next run.
                self.assertEqual(self.record.entry(devices.FOREGROUND)["state"], "released")
        self.assertEqual(self.host.pressed, [])

    def test_no_window_runs_as_before(self):
        status, error = self.qk("run", "--here", "ui-mac")
        self.assertEqual(status, 0, error)
        self.assertEqual(self.host.pressed, [])
        receipt = json.loads((self.project / ".build/queuekeeper/ui-mac/source.json").read_text())
        self.assertEqual(receipt["notificationCenter"], {"found": [], "closed": []})

    def test_eng_host_notifications_lists_and_dismisses_only_under_the_lease(self):
        self.host.windows, self.host.notifications = [WINDOW], [BANNER]
        self.assertEqual(self.qk("host", "notifications")[0], lane_results.UNMEASURED)
        status, error = self.qk("host", "notifications", "--dismiss")
        self.assertEqual(status, 1, error)
        self.assertEqual(self.host.pressed, [])
        with self.held():
            self.assertEqual(self.qk("host", "notifications", "--dismiss")[0], 0)
        self.assertEqual(self.host.closed, [BANNER["kind"]])


if __name__ == "__main__":
    unittest.main()
