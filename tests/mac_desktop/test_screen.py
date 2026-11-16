"""Reading a Mac's console screen, and keeping a lane that needs it unlocked off a locked Mac (vocaro-engineering#185).

Every reading comes from a recorded `ioreg -n Root -d1 -a` (fixtures/ScreenState): no test locks a real screen."""
from argparse import Namespace
import contextlib
from io import StringIO
import json
import os
from pathlib import Path
import plistlib
import sys
import tempfile
import unittest
from test_support import farm as farm_fixture
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src/python"))
import host_isolation  # noqa: F401  Keeps every test out of the host's heavy-work admission.
from queuekeeper import cli, config, merge_queue as mq, offload_host, remote, resources

SCREENS = Path(__file__).resolve().parents[1] / "mac_desktop/fixtures/ScreenState"
# offload_host.screen itself, which some tests replace with readings it gives.
READ = offload_host.screen


def recorded(name, mac="Medias-MacBook-Pro-5"):
    return READ((SCREENS / f"{name}.plist").read_bytes(), mac=mac, at="03:18:00")


def reading(state, mac="Trevors-MacBook-Pro-715"):
    return {"unlocked": recorded("this-mac-unlocked", mac), "locked": recorded("this-mac-locked-idle", mac),
            "unknown": recorded("second-mac-no-console-session", mac)}[state]


class ScreenReadingTests(unittest.TestCase):
    def test_both_macs_recorded_unlocked_read_unlocked(self):
        here = recorded("this-mac-unlocked", "Trevors-MacBook-Pro-715")
        self.assertEqual((here["state"], here["user"], here["CGSSessionScreenIsLocked"], here["IOConsoleLocked"]),
                         ("unlocked", "trevorharmon", None, False))
        there = recorded("second-mac-unlocked")
        self.assertEqual((there["state"], there["user"]), ("unlocked", "trevorharmon"))
        self.assertEqual(offload_host.screen_line(there),
                         "screen: unlocked (console user trevorharmon, CGSSessionScreenIsLocked absent, IOConsoleLocked "
                         "no) on Medias-MacBook-Pro-5 at 03:18:00")

    def test_an_idle_time_lock_reads_locked_though_ioconsolelocked_says_no(self):
        # The lock of 2026-10-28: CGSSessionScreenIsLocked yes, IOConsoleLocked no. IOConsoleLocked alone would
        # call it unlocked.
        for name in ("second-mac-locked-idle", "this-mac-locked-idle"):
            with self.subTest(name=name):
                found = recorded(name)
                self.assertEqual((found["state"], found["CGSSessionScreenIsLocked"], found["IOConsoleLocked"]),
                                 ("locked", True, False))
                self.assertIn("CGSSessionScreenIsLocked yes, IOConsoleLocked no", offload_host.screen_line(found))

    def test_a_lock_by_hand_reads_locked_with_or_without_ioconsolelocked(self):
        self.assertEqual(recorded("second-mac-locked-by-hand")["state"], "locked")
        absent = recorded("second-mac-locked-without-ioconsolelocked")
        self.assertEqual((absent["state"], absent["IOConsoleLocked"]), ("locked", None))
        self.assertIn("IOConsoleLocked absent", offload_host.screen_line(absent))

    def test_only_the_console_session_counts(self):
        # The negative control: mediaserver's session, not on the console, locked, leaves the screen unlocked.
        found = recorded("second-mac-other-session-locked")
        self.assertEqual((found["state"], found["user"], found["CGSSessionScreenIsLocked"]),
                         ("unlocked", "trevorharmon", None))

    def test_no_console_session_or_a_failed_read_is_unknown_never_unlocked(self):
        found = recorded("second-mac-no-console-session")
        self.assertEqual((found["state"], found["reason"]), ("unknown", "no login session is on the console"))
        self.assertEqual(offload_host.screen_line(found), "screen: unknown (no login session is on the console) on "
                                                          "Medias-MacBook-Pro-5 at 03:18:00")
        self.assertEqual(offload_host.screen(b"not a property list", mac="m")["state"], "unknown")
        failed = Namespace(returncode=1, stdout=b"", stderr=b"ioreg: no such entry\n")
        with patch.dict(os.environ, {offload_host.SCREEN_RECORDING: ""}), \
                patch.object(offload_host.subprocess, "run", return_value=failed):
            found = offload_host.screen(mac="m")
        self.assertEqual((found["state"], found["reason"]), ("unknown", "ioreg exited 1: ioreg: no such entry"))
        with patch.dict(os.environ, {offload_host.SCREEN_RECORDING: ""}), \
                patch.object(offload_host.subprocess, "run", side_effect=OSError("no ioreg")):
            self.assertEqual(offload_host.screen(mac="m")["reason"], "ioreg could not run: no ioreg")

    def test_a_recording_named_in_the_environment_is_read_instead_of_ioreg(self):
        with patch.dict(os.environ, {offload_host.SCREEN_RECORDING: str(SCREENS / "second-mac-locked-idle.plist")}), \
                patch.object(offload_host.subprocess, "run", side_effect=AssertionError("ioreg ran")):
            self.assertEqual(offload_host.screen(mac="m")["state"], "locked")

    def test_the_real_screen_reads_as_some_state(self):
        # Reading this Mac's own IORegistry changes nothing; its state is whatever the owner left.
        with patch.dict(os.environ, {offload_host.SCREEN_RECORDING: ""}):
            self.assertIn(offload_host.screen()["state"], ("unlocked", "locked", "unknown"))


UNSET = "Error: Could not find key 'AppleShowScrollBars' in domain 'kCFPreferencesAnyApplication'.\n"
BUILT_IN = {"Product": "Apple Internal Keyboard / Trackpad", "Built-In": True, "PrimaryUsagePage": 1, "PrimaryUsage": 2,
            "DeviceUsagePairs": [{"DeviceUsagePage": 1, "DeviceUsage": 2}, {"DeviceUsagePage": 1, "DeviceUsage": 1}]}
KEYBOARD = {"Product": "Magic Keyboard", "PrimaryUsagePage": 1, "PrimaryUsage": 6,
            "DeviceUsagePairs": [{"DeviceUsagePage": 1, "DeviceUsage": 6}]}
MOUSE = {"Product": "USB Mouse", "Built-In": False, "PrimaryUsagePage": 1, "PrimaryUsage": 2}
COMBO = {"Product": "Receiver", "PrimaryUsagePage": 1, "PrimaryUsage": 6,
         "DeviceUsagePairs": [{"DeviceUsagePage": 1, "DeviceUsage": 6}, {"DeviceUsagePage": 1, "DeviceUsage": 2}]}


class ScrollBarTests(unittest.TestCase):
    """offload_host.scroll_bars, with a stand-in for defaults and ioreg: no test reads or changes a real preference."""

    def read(self, setting=(0, "Automatic\n", ""), devices=(BUILT_IN, KEYBOARD), ioreg=None):
        self.commands = []

        def ran(command, **options):
            self.commands.append(command)
            status, standard, said = setting if command[0] == "defaults" else ioreg or (0, plistlib.dumps(list(devices)).decode(), "")
            return Namespace(returncode=status, stdout=standard.encode(), stderr=said.encode())
        with patch.dict(os.environ, {offload_host.SCROLL_BARS_RECORDING: ""}), \
                patch.object(offload_host.subprocess, "run", side_effect=ran):
            return offload_host.scroll_bars(mac="m", at="03:18:00")

    def test_automatic_or_unset_is_legacy_with_a_mouse_and_overlay_without(self):
        for setting, name in (((0, "Automatic\n", ""), "Automatic"), ((1, "", UNSET), "unset"),
                              ((1, "", "The domain/default pair of (kCFPreferencesAnyApplication, "
                                       "AppleShowScrollBars) does not exist\n"), "unset")):
            with self.subTest(name=name, said=setting[2]):
                docked = self.read(setting, (BUILT_IN, KEYBOARD, MOUSE))
                self.assertEqual((docked["kind"], docked["setting"], docked["pointingDevices"]), ("legacy", name, ["USB Mouse"]))
                self.assertNotIn("reasons", docked)
                alone = self.read(setting)
                self.assertEqual((alone["kind"], alone["setting"], alone["pointingDevices"]), ("overlay", name, []))
        self.assertEqual(offload_host.scroll_bars_line(self.read(devices=(BUILT_IN, MOUSE))),
                         "scroll bars: legacy (AppleShowScrollBars Automatic, pointing devices: USB Mouse) on m at 03:18:00")
        self.assertEqual(offload_host.scroll_bars_line(self.read(), "this Mac (m)"),
                         "scroll bars: overlay (AppleShowScrollBars Automatic, no pointing device but a built-in one) on "
                         "this Mac (m) at 03:18:00")

    def test_a_fixed_setting_gives_its_kind_whatever_is_attached(self):
        self.assertEqual(self.read((0, "Always\n", ""))["kind"], "legacy")
        self.assertEqual(self.read((0, "WhenScrolling\n", ""), (BUILT_IN, MOUSE))["kind"], "overlay")
        # A mouse's reading failing leaves the kind Always settles, and says what was not read.
        unread = self.read((0, "Always\n", ""), ioreg=(1, "", "ioreg: failed\n"))
        self.assertEqual((unread["kind"], unread["pointingDevices"]), ("legacy", None))
        self.assertEqual(unread["reasons"], ["the pointing devices could not be read: ioreg exited 1: ioreg: failed"])

    def test_a_device_counts_by_its_usages_and_only_a_built_in_one_does_not(self):
        self.assertEqual(self.read(devices=(COMBO,))["pointingDevices"], ["Receiver"])
        # The negative control: a keyboard, and the built-in trackpad, are no such device.
        self.assertEqual(self.read(devices=(KEYBOARD, BUILT_IN))["pointingDevices"], [])
        self.assertEqual(self.read(devices=(MOUSE, MOUSE))["pointingDevices"], ["USB Mouse"])
        # ioreg's output with nothing to list, on a Mac with no HID device, is no pointing device.
        self.assertEqual(self.read(ioreg=(0, "", ""))["pointingDevices"], [])

    def test_a_reading_that_cannot_be_taken_says_so_and_is_never_a_value(self):
        for failed, why in (((2, "", "defaults: broken\n"), "defaults exited 2: defaults: broken"),
                            ((1, "", "something else went wrong\n"), "defaults exited 1: something else went wrong")):
            with self.subTest(why=why):
                found = self.read(failed, (BUILT_IN, MOUSE))
                self.assertEqual((found["kind"], found["setting"], found["pointingDevices"]), ("unknown", None, ["USB Mouse"]))
                self.assertEqual(found["reasons"], [f"AppleShowScrollBars could not be read: {why}"])
        damaged = self.read(ioreg=(0, "not a property list", ""))
        self.assertEqual((damaged["kind"], damaged["setting"], damaged["pointingDevices"]), ("unknown", "Automatic", None))
        self.assertIn("ioreg's output is not a property list", damaged["reasons"][0])
        self.assertEqual(self.read(ioreg=(1, "", "no\n"))["pointingDevices"], None)
        unsure = self.read((0, "Sometimes\n", ""))
        self.assertEqual((unsure["kind"], unsure["setting"]), ("unknown", "Sometimes"))
        self.assertEqual(unsure["reasons"], ["AppleShowScrollBars is 'Sometimes', which gives no known kind"])
        with patch.dict(os.environ, {offload_host.SCROLL_BARS_RECORDING: ""}), \
                patch.object(offload_host.subprocess, "run", side_effect=OSError("no such tool")):
            found = offload_host.scroll_bars(mac="m", at="03:18:00")
        self.assertEqual((found["kind"], found["setting"], found["pointingDevices"]), ("unknown", None, None))
        self.assertEqual(offload_host.scroll_bars_line(found),
                         "scroll bars: unknown (AppleShowScrollBars could not be read: defaults could not run: no such tool, the "
                         "pointing devices could not be read: ioreg could not run: no such tool) on m at 03:18:00")

    def test_it_only_reads_and_launches_no_probe(self):
        # The issue leaves the optional probe process out: the reading is these two read-only commands and no more.
        self.read((0, "Automatic\n", ""), (BUILT_IN, MOUSE))
        self.assertEqual(self.commands, [["defaults", "read", "-g", "AppleShowScrollBars"],
                                         ["ioreg", "-a", "-r", "-c", "IOHIDDevice", "-d", "1"]])

    def test_a_lane_says_it_once_and_nested_work_records_it_silently(self):
        recording = {"mac": "m", "at": "03:18:00", "kind": "legacy", "setting": "unset", "pointingDevices": ["USB Mouse"]}
        said = lambda receipt, lane, inherited: cli.scroll_bars_recorded(receipt, lane, inherited)
        with patch.object(offload_host, "scroll_bars", return_value=recording), patch.dict(os.environ), \
                patch.object(offload_host, "mac_name", return_value="m"), patch("sys.stderr", new_callable=StringIO) as error:
            for name in (cli.SELF_LANE, remote.DISPATCHER): os.environ.pop(name, None)
            receipt = {}
            said(receipt, "gate", False)
            self.assertEqual(receipt["scrollBars"], recording)
            self.assertEqual(error.getvalue(), "queuekeeper: scroll bars: legacy (AppleShowScrollBars unset, pointing devices: USB "
                                               "Mouse) on this Mac (m) at 03:18:00\n")
            # The negative controls: work nested in an admission or a self lane, and a dispatch's provision lane.
            error.truncate(0), error.seek(0)
            receipt = {}
            said(receipt, "gate", True)
            self.assertEqual((receipt["scrollBars"], error.getvalue()), (recording, ""))
            with patch.dict(os.environ, {cli.SELF_LANE: "own"}): said({}, "part", False)
            with patch.dict(os.environ, {remote.DISPATCHER: "sender"}): said({}, "provision", False)
            self.assertEqual(error.getvalue(), "")
            # A provision lane run by itself on this Mac is a lane like any other.
            said({}, "provision", False)
            self.assertIn("scroll bars: legacy", error.getvalue())

    def test_the_host_identity_carries_the_reading(self):
        with patch.object(offload_host, "scroll_bars", return_value={"kind": "overlay"}):
            self.assertEqual(offload_host.identity(None)["scrollBars"], {"kind": "overlay"})


class PlacementTests(unittest.TestCase):
    """remote.spare, which Offer.attempt reads for both Macs, with the screen in each reading."""

    def capacity(self, state):
        return {"slots": 3, "slotsMax": 6, "busy": [], "load": 1.0, "cpuBusy": 10.0, "busyLimit": 75.0, "screen": reading(state)}

    def test_a_locked_mac_is_refused_and_an_unlocked_one_taken_for_a_lane_that_needs_it(self):
        refused = remote.spare(self.capacity("locked"), "another Mac", screen=True)
        self.assertTrue(refused.startswith("the lane needs an unlocked screen, and another Mac's is locked (screen: "
                                           "locked (console user trevorharmon, CGSSessionScreenIsLocked yes, "
                                           "IOConsoleLocked no) on Trevors-MacBook-Pro-715"), refused)
        self.assertIsNone(remote.spare(self.capacity("unlocked"), "another Mac", screen=True))
        unknown = remote.spare(self.capacity("unknown"), "this Mac", screen=True)
        self.assertIn("this Mac's is unknown (screen: unknown (no login session is on the console)", unknown)
        missing = {key: value for key, value in self.capacity("unlocked").items() if key != "screen"}
        self.assertEqual(remote.spare(missing, "another Mac", screen=True),
                         "the lane needs an unlocked screen, and another Mac did not report its screen")

    def test_a_lane_that_does_not_declare_it_ignores_the_screen(self):
        for state in ("locked", "unknown"):
            with self.subTest(state=state):
                self.assertIsNone(remote.spare(self.capacity(state), "another Mac"))
                self.assertIsNone(remote.spare(self.capacity(state), "another Mac", whole_host=True))

    def offer(self, screen):
        offered = remote.Offer("gate", "`qk run --here gate`", lambda host, sent: 0, whole_host=True, screen=screen)
        offered.host = "fake"
        return offered

    def test_the_choice_sends_the_lane_from_a_locked_second_mac_to_an_unlocked_this_mac(self):
        with patch.object(remote, "capacity_there", return_value=self.capacity("locked")), \
                patch.object(offload_host, "capacity", return_value=self.capacity("unlocked")):
            self.assertIn("fake's is locked", farm_fixture.choice(self.offer(True)))
            # The negative control: an undeclared lane is offered to the locked second Mac, as before.
            self.assertIsNone(farm_fixture.choice(self.offer(False)))
        # Neither Mac unlocked: the lane is offered, and the second Mac declines it while its screen is locked.
        with patch.object(remote, "capacity_there", return_value=self.capacity("locked")), \
                patch.object(offload_host, "capacity", return_value=self.capacity("locked")), \
                patch("sys.stderr", new_callable=StringIO) as error:
            self.assertIsNone(farm_fixture.choice(self.offer(True)))
        self.assertIn("no Mac has spare capacity for gate now (the lane needs an unlocked screen, and "
                      "fake's is locked", error.getvalue())

    def test_the_second_mac_declines_a_lane_that_needs_its_screen_while_it_is_locked(self):
        plan = {"unlockedScreen": True, "decline": True}
        with self.assertRaises(offload_host.Refusal) as refused:
            offload_host.screen_declined(plan, lambda: recorded("second-mac-locked-idle"))
        self.assertTrue(refused.exception.busy)
        self.assertTrue(str(refused.exception).startswith(
            "its screen is locked, and the lane needs it unlocked (screen: locked (console user trevorharmon, "
            "CGSSessionScreenIsLocked yes, IOConsoleLocked no) on Medias-MacBook-Pro-5 at 03:18:00)"))
        with self.assertRaises(offload_host.Refusal):
            offload_host.screen_declined(plan, lambda: recorded("second-mac-no-console-session"))
        # The negative controls: unlocked, an undeclared lane, and a run that may not be declined go ahead.
        offload_host.screen_declined(plan, lambda: recorded("second-mac-unlocked"))
        never = lambda: self.fail("the screen was read")
        offload_host.screen_declined({"decline": True}, never)
        offload_host.screen_declined({"unlockedScreen": True}, never)


class AdmissionTests(unittest.TestCase):
    """cli.admitted_here, this Mac's admission, with the screen read from a recording."""

    def setUp(self):
        self.read = patch.object(offload_host, "screen")
        self.screen = self.read.start()
        self.addCleanup(self.read.stop)
        self.admitted = []

        @contextlib.contextmanager
        def admission(label, **options):
            self.admitted.append(label)
            yield "admission"
        patch_admission = patch.object(resources, "Admission", side_effect=admission)
        patch_admission.start()
        self.addCleanup(patch_admission.stop)
        names = patch.object(offload_host, "mac_name", return_value="Trevors-MacBook-Pro-715")
        names.start()
        self.addCleanup(names.stop)
        dispatcher = patch.dict(os.environ)
        dispatcher.start()
        self.addCleanup(dispatcher.stop)
        os.environ.pop(remote.DISPATCHER, None)

    def admit(self, screen, once=True, timeout=0):
        with contextlib.ExitStack() as stack, patch("sys.stderr", new_callable=StringIO) as error, \
                patch.object(cli, "SCREEN_POLL", 0.01), patch.object(cli, "FREE_WAIT", 0):
            return cli.admitted_here(stack, "gate", "Example gate", timeout, once=once, screen=screen), error.getvalue()

    def test_a_locked_mac_does_not_admit_a_lane_that_needs_it_and_says_which_mac(self):
        self.screen.return_value = reading("locked")
        status, error = self.admit(True)
        self.assertEqual(status, resources.BUSY)
        self.assertEqual(self.admitted, [])
        self.assertIn("queuekeeper: gate waits: the lane needs an unlocked screen, and this Mac (Trevors-MacBook-Pro-715)'s is "
                      "locked", error)
        self.assertIn("queuekeeper: gate not run on this Mac (Trevors-MacBook-Pro-715): the lane needs an unlocked screen", error)
        # Without --if-free it waits out its time for an unlocked screen, then declines, still running nothing.
        status, error = self.admit(True, once=False, timeout=0.05)
        self.assertEqual((status, self.admitted), (resources.BUSY, []))
        self.assertEqual(error.count("waits: the lane needs an unlocked screen"), 1, error)

    def test_an_unlocked_mac_admits_it_and_a_lock_that_ends_lets_the_waiting_lane_in(self):
        self.screen.return_value = reading("unlocked")
        self.assertEqual(self.admit(True)[0], "admission")
        self.screen.side_effect = [reading("locked"), reading("locked"), reading("unlocked")]
        status, error = self.admit(True, once=False, timeout=5)
        self.assertEqual(status, "admission")
        self.assertEqual(self.admitted, ["Example gate", "Example gate"])
        self.assertIn("waits: the lane needs an unlocked screen", error)

    def test_an_undeclared_lane_is_admitted_on_a_locked_mac_without_a_reading(self):
        self.screen.side_effect = AssertionError("the screen was read")
        self.assertEqual(self.admit(False)[0], "admission")
        self.assertEqual(self.admit(False, once=False)[0], "admission")

    def test_the_second_mac_names_itself(self):
        self.screen.return_value = reading("locked", "Medias-MacBook-Pro-5")
        with patch.dict(os.environ, {remote.DISPATCHER: "Trevors-MacBook-Pro-715"}):
            status, error = self.admit(True)
        self.assertEqual(status, resources.BUSY)
        self.assertIn("queuekeeper: gate not run on Trevors-MacBook-Pro-715", error)


class InheritedAdmissionTests(unittest.TestCase):
    """A generic outer admission cannot promise the nested lane an unlocked console (#366)."""

    def run_lane(self, state, declared=True, explicit=False, group=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "ran"
            lane = {"command": [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"],
                    "resource": "heavy", "timeout": 30}
            if declared: lane["unlockedScreen"] = True
            lanes = {"check": lane}
            name = "check"
            if group:
                name = "group"
                lanes[name] = {"parallel": ["check"], "resource": "heavy",
                               "unlockedScreen": declared}
            (root / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Nested", "lanes": lanes}))
            with resources.Admission("outer", exclusive=True, timeout=0), \
                    patch.object(cli, "refresh_policy", return_value="policy"), \
                    patch.object(cli, "source_identity", return_value={}), \
                    patch.object(offload_host, "screen", return_value=reading(state)) as screen, \
                    patch.object(cli, "screen_wait", side_effect=AssertionError("nested work waited")), \
                    patch.object(remote, "first", side_effect=AssertionError("nested work was dispatched")), \
                    contextlib.redirect_stdout(StringIO()), contextlib.redirect_stderr(StringIO()) as error:
                status = cli.main(["--project", str(root), "run", *(["--unlocked-screen"] if explicit else []), name])
                return status, marker.exists(), error.getvalue(), screen.call_count

    def test_a_declared_nested_lane_runs_only_with_an_unlocked_reading(self):
        for state in ("unlocked", "locked", "unknown"):
            with self.subTest(state=state):
                status, ran, error, reads = self.run_lane(state)
                self.assertEqual((status, ran), (0, True) if state == "unlocked" else (resources.BUSY, False), error)
                self.assertEqual(reads, 1)
                if state != "unlocked":
                    self.assertIn("release the outer admission before waiting", error)
                    self.assertIn(f"screen: {state}", error)

    def test_the_explicit_option_rechecks_an_undeclared_nested_lane(self):
        for state in ("unlocked", "locked", "unknown"):
            with self.subTest(state=state):
                status, ran, error, reads = self.run_lane(state, declared=False, explicit=True)
                self.assertEqual((status, ran), (0, True) if state == "unlocked" else (resources.BUSY, False), error)
                self.assertEqual(reads, 1)

    def test_a_nested_group_checks_before_starting_any_member(self):
        for state in ("unlocked", "locked", "unknown"):
            with self.subTest(state=state):
                status, ran, error, reads = self.run_lane(state, group=True)
                self.assertEqual((status, ran), (0, True) if state == "unlocked" else (resources.BUSY, False), error)
                self.assertEqual(reads, 1)

    def test_an_undeclared_nested_lane_still_needs_no_screen_reading(self):
        for state in ("locked", "unknown"):
            with self.subTest(state=state):
                status, ran, error, reads = self.run_lane(state, declared=False)
                self.assertEqual((status, ran, reads), (0, True, 0), error)


class MessageTests(unittest.TestCase):
    def setUp(self):
        names = patch.object(offload_host, "mac_name", return_value="Trevors-MacBook-Pro-715")
        names.start()
        self.addCleanup(names.stop)

    def test_a_fallback_says_in_one_line_that_the_run_is_on_this_mac_and_why(self):
        offered = remote.Offer("gate", "`qk run --here gate`", lambda host, sent: 0)
        offered.status = "build-host is busy: build-host did not take it: its heavy-work queue cannot take the lane now"
        offered.sent = {"commit": "0b1d0b3b9329"}
        with patch.dict(os.environ), patch("sys.stderr", new_callable=StringIO) as error:
            os.environ.pop(remote.DISPATCHER, None)
            cli.fell_back("gate", offered)
        self.assertEqual(error.getvalue(), "queuekeeper: gate runs on this Mac (Trevors-MacBook-Pro-715), not another Mac: "
                         "build-host is busy: build-host did not take it: its heavy-work queue cannot take the lane now; "
                         "another Mac's lines above are from the offer it did not take\n")

    def test_an_unmeasured_run_reads_the_screen_again_and_names_the_mac(self):
        receipt = {}
        with patch.dict(os.environ), patch.object(offload_host, "screen", return_value=reading("locked")), \
                patch("sys.stderr", new_callable=StringIO) as error:
            os.environ.pop(remote.DISPATCHER, None)
            cli.unmeasured_screen("gate", receipt)
        self.assertEqual(error.getvalue().splitlines(), [
            "queuekeeper: gate unmeasured on this Mac (Trevors-MacBook-Pro-715)",
            "queuekeeper: screen: locked (console user trevorharmon, CGSSessionScreenIsLocked yes, IOConsoleLocked no) on this "
            "Mac (Trevors-MacBook-Pro-715) at 03:18:00"])
        self.assertEqual(receipt["screenAfter"]["state"], "locked")

    def test_a_hold_for_an_unmeasured_run_quotes_the_last_screen_reading(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "gate.log"
            log.write_text("queuekeeper: screen: unlocked (console user trevorharmon, CGSSessionScreenIsLocked absent, "
                           "IOConsoleLocked no) on this Mac (Trevors-MacBook-Pro-715) at 01:39:20\n"
                           "Test case 'ReaderTests/scroll()' Unmeasured: the screen is locked; the accessibility "
                           "service hides the test window\n"
                           "queuekeeper: screen: locked (console user trevorharmon, CGSSessionScreenIsLocked yes, "
                           "IOConsoleLocked no) on this Mac (Trevors-MacBook-Pro-715) at 01:58:02\n")
            worker = Namespace(timed_out=None)
            with patch.object(mq.lane_results, "unmeasured_tests", return_value=["Test case 'ReaderTests/scroll()' "
                              "Unmeasured: the screen is locked"]):
                said = mq.Worker.no_verdict_cause(worker, 3, log)
        self.assertTrue(said.endswith("; the last reading screen: locked (console user trevorharmon, "
                                      "CGSSessionScreenIsLocked yes, IOConsoleLocked no) on this Mac "
                                      "(Trevors-MacBook-Pro-715) at 01:58:02"), said)


class CommandTests(unittest.TestCase):
    """`qk host screen [--remote]`."""

    def run_command(self, *arguments):
        with patch("sys.stdout", new_callable=StringIO) as out, patch.object(offload_host, "mac_name",
                                                                             return_value="Trevors-MacBook-Pro-715"), \
                patch.dict(os.environ):
            os.environ.pop(remote.DISPATCHER, None)
            return cli.main(["host", "screen", *arguments]), out.getvalue()

    def test_the_status_says_unlocked_locked_or_unknown(self):
        for state, status in (("unlocked", 0), ("locked", 1), ("unknown", 2)):
            with self.subTest(state=state), patch.object(offload_host, "screen", return_value=reading(state)):
                said = self.run_command()
                self.assertEqual(said[0], status)
                self.assertTrue(said[1].startswith(f"screen: {state} ("), said[1])
                self.assertIn("on this Mac (Trevors-MacBook-Pro-715)", said[1])

    def test_remote_reads_the_second_mac(self):
        with farm_fixture.only("build-host"), \
                patch.object(remote, "screen_there", return_value=recorded("second-mac-locked-idle")) as there:
            status, said = self.run_command("--remote")
        there.assert_called_once_with("build-host")
        self.assertEqual(status, 1)
        self.assertIn("on build-host (Medias-MacBook-Pro-5)", said)
        with farm_fixture.only("build-host"), \
                patch.object(remote, "screen_there", side_effect=remote.Unavailable("it answered None")):
            status, said = self.run_command("--remote", "--json")
        self.assertEqual(status, 2)
        self.assertEqual(json.loads(said)["reason"], "build-host could not be read: it answered None")


class ConfigurationTests(unittest.TestCase):
    def check(self, lanes):
        return config.parse(json.dumps({"schemaVersion": 1, "name": "Example", "lanes": lanes}))

    def test_a_heavy_lane_or_group_may_need_an_unlocked_screen(self):
        command = ["true"]
        lanes = self.check({"mac-tests": {"command": command, "resource": "heavy", "unlockedScreen": True},
                            "docs": {"command": command},
                            "gate": {"parallel": ["mac-tests", "docs"], "resource": "heavy", "unlockedScreen": True}})
        self.assertTrue(config.unlocked_screen(lanes["lanes"], "gate"))
        self.assertTrue(config.unlocked_screen(lanes["lanes"], "mac-tests"))
        self.assertFalse(config.unlocked_screen(lanes["lanes"], "docs"))

    def test_a_self_lane_a_non_boolean_or_a_group_that_hides_it_is_refused(self):
        command = ["true"]
        for lanes, said in [
                ({"docs": {"command": command, "resource": "self", "unlockedScreen": True}},
                 "docs.unlockedScreen needs an admitted lane"),
                ({"mac": {"command": command, "resource": "heavy", "unlockedScreen": "yes"}}, "must be true or false"),
                ({"mac": {"command": command, "resource": "heavy", "unlockedScreen": True},
                  "gate": {"parallel": ["mac"], "resource": "heavy"}}, "gate runs mac, which needs an unlocked screen")]:
            with self.subTest(said=said), self.assertRaises(ValueError) as refused:
                self.check(lanes)
            self.assertIn(said, str(refused.exception))


if __name__ == "__main__":
    unittest.main()
