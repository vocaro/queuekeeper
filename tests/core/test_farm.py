"""farm: the Macs beside this one that take work, as their owner adds, renames, pauses and removes them."""
import contextlib
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
import host_isolation  # noqa: F401 -- isolates this module's host state as every test module's
from queuekeeper import cli, farm, offload_host, remote

STUDIO = {"id": "AAAA-1", "name": "Studio"}
MINI = {"id": "BBBB-2", "name": "Mini"}


class FarmCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        environment = patch.dict(os.environ, {"HEAVY_SLOT_DIR": folder.name})
        environment.start()
        self.addCleanup(environment.stop)
        self.macs = {"studio.local": STUDIO, "mini.local": MINI}

    def join(self, ssh):
        if ssh not in self.macs: raise remote.Unreachable(f"{ssh} cannot be reached", f"wake {ssh}")
        return dict(self.macs[ssh])

    def add(self, ssh, name=None, here="HERE-0"):
        return farm.add(ssh, name, join=self.join, here=here)[0]


class RegistryTests(FarmCase):
    def test_no_file_is_a_farm_with_no_mac(self):
        self.assertEqual((farm.read(), farm.destinations(), remote.configured()), ([], (), None))

    def test_a_mac_is_added_under_its_own_name_or_the_one_given_and_tried_in_the_order_added(self):
        first, second = self.add("studio.local"), self.add("mini.local", "render-box")
        self.assertEqual((first["name"], first["id"], first["ssh"], first["state"]),
                         ("Studio", "AAAA-1", "studio.local", farm.ACTIVE))
        self.assertEqual(second["name"], "render-box")
        self.assertEqual(farm.destinations(), ("studio.local", "mini.local"))
        self.assertEqual((remote.configured(), farm.destinations()), ("studio.local", ("studio.local", "mini.local")))
        # A name reaches its Mac whatever its case; an SSH destination the farm does not name is itself.
        self.assertEqual(farm.destination("RENDER-BOX"), "mini.local")
        self.assertEqual(farm.destination("elsewhere.local"), "elsewhere.local")
        self.assertEqual((farm.named("mini.local"), farm.named("elsewhere.local")), ("render-box", "elsewhere.local"))
        self.assertEqual(remote.chosen("Studio"), "studio.local")

    def test_a_mac_is_added_once_and_never_this_mac_or_one_that_cannot_be_reached(self):
        self.add("studio.local")
        self.macs["studio-wifi.local"] = STUDIO
        for ssh, name, here, said in [
                ("studio.local", None, "HERE-0", "the farm has already"),
                # The same Mac by another route, which its hardware UUID tells.
                ("studio-wifi.local", "other", "HERE-0", "reaches Studio, which the farm has already"),
                ("mini.local", "studio", "HERE-0", "already has a Mac named Studio"),
                ("mini.local", None, "BBBB-2", "reaches this Mac"),
                ("mini.local", "none", "HERE-0", "cannot name a Mac"),
                ("mini.local", "-dash", "HERE-0", "cannot name a Mac"),
                ("-oProxyCommand=evil", None, "HERE-0", "is not an SSH destination")]:
            with self.subTest(ssh=ssh, name=name), self.assertRaisesRegex(ValueError, said):
                self.add(ssh, name, here)
        with self.assertRaises(remote.Unreachable): self.add("away.local")
        self.macs["blank.local"] = {"id": None, "name": "Blank"}
        with self.assertRaisesRegex(ValueError, "hardware UUID"): self.add("blank.local")
        self.assertEqual([mac["name"] for mac in farm.read()], ["Studio"])

    def test_a_renamed_mac_keeps_its_identity_and_its_place(self):
        self.add("studio.local"), self.add("mini.local")
        self.assertEqual(farm.rename("studio", "Desk")["id"], "AAAA-1")
        self.assertEqual([(mac["name"], mac["ssh"]) for mac in farm.read()],
                         [("Desk", "studio.local"), ("Mini", "mini.local")])
        # Its own name in another case is no clash; another Mac's is.
        self.assertEqual(farm.rename("desk", "DESK")["name"], "DESK")
        with self.assertRaisesRegex(ValueError, "already has a Mac named Mini"): farm.rename("DESK", "mini")
        with self.assertRaisesRegex(ValueError, r"no Mac named Studio \(its Macs: DESK, Mini\)"): farm.rename("Studio", "x")

    def test_a_paused_mac_takes_no_work_until_it_is_resumed(self):
        self.add("studio.local"), self.add("mini.local")
        self.assertEqual(farm.pause("Studio", "its disk is being replaced")["pausedBecause"], "its disk is being replaced")
        self.assertEqual((remote.configured(), farm.destinations()), ("mini.local", ("mini.local",)))
        self.assertIn("paused (its disk is being replaced)", farm.line(farm.find("studio")))
        # Named, it is still reached, as setup and a check of it need.
        self.assertEqual(remote.chosen("Studio"), "studio.local")
        self.assertNotIn("pausedBecause", farm.resume("Studio"))
        self.assertEqual(farm.destinations(), ("studio.local", "mini.local"))

    def test_a_removed_mac_is_gone_unless_work_this_mac_sent_still_runs_there(self):
        self.add("studio.local"), self.add("mini.local")
        with self.assertRaisesRegex(ValueError, r"Studio still runs work this Mac sent it \(run-7\).*qk farm pause Studio"):
            farm.remove("studio", running=lambda ssh: ["run-7"] if ssh == "studio.local" else [])
        self.assertEqual(len(farm.read()), 2)
        self.assertEqual(farm.remove("studio")["name"], "Studio")
        self.assertEqual([mac["name"] for mac in farm.read()], ["Mini"])
        with self.assertRaisesRegex(ValueError, "no Mac named Studio"): farm.find("Studio")
        # It can be added again, last in the order.
        self.add("studio.local")
        self.assertEqual(farm.destinations(), ("mini.local", "studio.local"))

    def test_a_file_that_is_no_farm_stops_what_reads_it(self):
        farm.path().parent.mkdir(parents=True, exist_ok=True)
        for text in ["", "[]", '{"version": 2, "macs": []}', '{"version": 1, "macs": [{"name": "x"}]}',
                     '{"version": 1, "macs": [{"name": "a", "ssh": "h", "state": "asleep"}]}',
                     json.dumps({"version": 1, "macs": [{"name": "a", "ssh": "h", "state": "active"},
                                                        {"name": "A", "ssh": "i", "state": "active"}]})]:
            with self.subTest(text):
                farm.path().write_text(text)
                with self.assertRaisesRegex(ValueError, "farm.json"): remote.configured()

    def test_no_mac_to_reach_is_refused(self):
        with self.assertRaisesRegex(ValueError, "qk farm add DESTINATION"): remote.chosen()

    def test_only_a_live_dispatcher_s_run_counts_as_running_there(self):
        records = remote.dispatch_records()
        records.mkdir(parents=True)
        start = remote.resources.process_start(os.getpid())
        for journal, host, pid, began in [("live", "studio.local", os.getpid(), start), ("dead", "studio.local", os.getpid(), "long ago"),
                                          ("other", "mini.local", os.getpid(), start)]:
            (records / f"{journal}.json").write_text(json.dumps({"journal": journal, "host": host, "pid": pid, "start": began}))
        (records / "torn.json").write_text("{")
        self.assertEqual(remote.running_on("studio.local"), ["live"])


class CommandTests(FarmCase):
    def qk(self, *arguments):
        with contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()) as error, \
                patch.object(remote, "join", side_effect=self.join), \
                patch.object(offload_host, "hardware_id", return_value="HERE-0"):
            status = cli.main(["farm", *arguments])
        return status, output.getvalue(), error.getvalue()

    def test_the_owner_adds_lists_renames_pauses_resumes_and_removes_a_mac(self):
        self.assertEqual(self.qk(), (0, "The farm has no Mac beside this one: add one with `qk farm add DESTINATION`\n", ""))
        self.macs["studio.local"] = {**STUDIO, "moved": True, "from": "/Users/o/Development/build-host",
                                     "to": "/Users/o/Development/queuekeeper-farm", "linked": "/Users/o/Development/build-host"}
        status, output, error = self.qk("add", "studio.local")
        self.assertEqual((status, error), (0, ""))
        self.assertEqual(output.splitlines(), [
            "Added Studio  active  ssh studio.local",
            "Moved /Users/o/Development/build-host there to /Users/o/Development/queuekeeper-farm, with its checkouts",
            "Linked /Users/o/Development/build-host there to it, for build state that still names the old path",
            "Next, give it the tools the lanes need: `qk remote setup --host Studio`",
            "If lanes may take its screen, say so in its settings: `qk farm foreground Studio`"])
        self.assertEqual(self.qk("add", "mini.local", "--name", "render")[0], 0)
        self.assertEqual(self.qk("rename", "render", "Render-Box"), (0, "render is now Render-Box\n", ""))
        self.assertEqual(self.qk("pause", "render-box", "--reason", "on loan")[1], "Paused Render-Box  paused (on loan)  ssh mini.local\n")
        self.assertEqual(self.qk("list")[1], "Studio  active  ssh studio.local\nRender-Box  paused (on loan)  ssh mini.local\n")
        self.assertEqual([(mac["name"], mac["state"]) for mac in json.loads(self.qk("list", "--json")[1])["macs"]],
                         [("Studio", "active"), ("Render-Box", "paused")])
        self.assertEqual(self.qk("resume", "Render-Box")[1], "Resumed Render-Box  active  ssh mini.local\n")
        status, output, error = self.qk("remove", "studio")
        self.assertEqual((status, error), (0, ""))
        self.assertIn(f"Removed Studio. Its checkouts stay on it, under ~/{remote.ROOT}", output)
        self.assertEqual(self.qk("list")[1], "Render-Box  active  ssh mini.local\n")

    def test_the_owner_marks_a_mac_for_foreground_work_in_that_macs_own_settings(self):
        """The foreground mark alone, on any Mac (the owner's decision, 2026-11-17, vocaro/queuekeeper#211): a Mac
        takes foreground work when its own settings say so, which `qk farm foreground NAME` writes there. Until then
        the mark was in this Mac's farm.json, and while it marked none the farm's first Mac took foreground work."""
        self.macs["studio.local"], self.macs["mini.local"] = STUDIO, MINI
        marks = {}

        def ask(host, request, **options):
            if host == "away.local": raise remote.Unreachable(f"{host} cannot be reached", f"wake {host}")
            marks[host] = request["markForeground"]
            return {"mac": host, "takes": {"foreground": marks[host]}, "file": f"/Users/o/{host}/mac-settings.json"}

        reading = lambda host, **options: {"takes": {"foreground": marks.get(host, False)}}
        self.qk("add", "studio.local")
        self.qk("add", "mini.local")
        with patch.object(remote, "ask", side_effect=ask), patch.object(remote, "reachable"), \
                patch.object(remote, "capacity_there", side_effect=reading):
            # No Mac's settings say so: none takes foreground work, the farm's first Mac no more than another.
            self.assertEqual(remote.foreground_hosts(), ())
            status, output, error = self.qk("foreground", "mini")
            self.assertEqual((status, error), (0, ""))
            self.assertEqual(output.splitlines(), [
                f"{MINI['name']} takes foreground work, by its settings (/Users/o/mini.local/mac-settings.json)",
                f"Foreground lanes now go to: {MINI['name']}"])
            self.assertEqual((marks, remote.foreground_hosts()), ({"mini.local": True}, ("mini.local",)))
            # The mark is the Mac's, and this Mac's farm.json holds none; one an earlier toolkit left is taken out.
            self.assertNotIn("foreground", farm.find("mini"))
            with farm.changing() as macs: farm.find("studio", macs)["foreground"] = True
            self.qk("foreground", "studio")
            self.assertNotIn("foreground", farm.find("studio"))
            self.assertEqual(remote.foreground_hosts(), ("studio.local", "mini.local"))
            self.assertEqual(self.qk("list")[1], "Studio  active  ssh studio.local\nMini  active  ssh mini.local\n")
            # A marked Mac that is paused takes none, and the mark comes off with --off.
            self.qk("pause", "studio")
            self.assertEqual(remote.foreground_hosts(), ("mini.local",))
            self.qk("resume", "studio")
            output = self.qk("foreground", "mini", "--off")[1]
            self.assertIn(f"{MINI['name']} takes no foreground work, by its settings", output)
            self.assertIn("Foreground lanes now go to: Studio\n", output)
            # The Mac this runs on is marked as any other is, in its own settings.
            status, output, error = self.qk("foreground", "here")
            self.assertEqual((status, error), (0, ""))
            here = offload_host.mac_settings()
            self.assertEqual((here["takes"]["foreground"], here["set"]), (True, ["foreground"]))
            self.assertEqual(output.splitlines(), [f"This Mac takes foreground work, by its settings ({here['file']})",
                                                   "Foreground lanes now go to: Studio, this Mac"])
            self.assertIn("Foreground lanes now go to: Studio\n", self.qk("foreground", "this", "--off")[1])
            self.assertFalse(offload_host.mac_settings()["takes"]["foreground"])
        # A Mac that cannot be reached is not marked, and one that is no Mac of the farm is refused.
        self.macs["away.local"] = {"id": "AWAY-1", "name": "Away"}
        self.qk("add", "away.local")
        with patch.object(remote, "ask", side_effect=ask), patch.object(remote, "reachable"), \
                patch.object(remote, "capacity_there", side_effect=reading):
            status, output, error = self.qk("foreground", "away")
            self.assertEqual((status, output), (2, ""))
            self.assertIn("away.local cannot be reached", error)
            status, output, error = self.qk("foreground", "nobody")
            self.assertEqual(status, 2)
            self.assertIn("the farm has no Mac named nobody", error)

    def test_a_refusal_says_why_and_changes_nothing(self):
        status, output, error = self.qk("add", "away.local")
        self.assertEqual((status, output), (2, ""))
        self.assertEqual(error, "qk farm: away.local cannot be reached\nqk farm: to fix it, wake away.local\n")
        self.assertEqual(self.qk("pause", "nobody")[0], 2)
        self.assertFalse(farm.path().exists())

    def test_what_a_mac_left_unmoved_or_unrepaired_is_said(self):
        self.macs["studio.local"] = {**STUDIO, "moved": False, "left": "~/Development/build-host stays while a run holds it"}
        self.assertIn("Not moved there: ~/Development/build-host stays while a run holds it\n", self.qk("add", "studio.local")[1])
        self.macs["mini.local"] = {**MINI, "moved": True, "from": "a", "to": "b", "unrepaired": ["source.git: no"]}
        self.assertIn("A checkout there was not repaired: source.git: no\n", self.qk("add", "mini.local")[1])


class LaneFitTests(FarmCase):
    """`qk doctor` warns of a lane no Mac of the farm takes by its own settings: its labels, or its kind of work
    (remote.unplaceable_lanes, vocaro/queuekeeper#224). A Mac that cannot be read is unknown, never one that lacks."""
    LANES = {"build": {"command": ["true"]},
             "gate": {"command": ["true"], "resource": "exclusive", "wholeHost": True},
             "suite": {"command": ["true"], "wholeHost": True},
             "unity": {"command": ["true"], "labels": ["unity-license"]},
             "story": {"command": ["true"], "model": True},
             "screen": {"command": ["true"], "foreground": True},
             "parts": {"command": ["true"], "resource": "self", "labels": ["unity-license"]}}

    def setUp(self):
        super().setUp()
        self.add("studio.local"), self.add("mini.local")
        self.asked = []

    def settings(self, file, takes=None, labels=()):
        return {"takes": {"wholeHost": True, "exclusive": True, "model": True, "foreground": False, **(takes or {})},
                "takesSet": sorted(takes or {}), "labels": list(labels), "settingsFile": f"/Users/o/{file}/mac-settings.json"}

    def warnings(self, here, there, lanes=None):
        def read(host):
            self.asked.append(host)
            if isinstance(there[host], Exception): raise there[host]
            return there[host]
        return remote.unplaceable_lanes({"lanes": lanes or self.LANES}, read_here=lambda: here, read_there=read)

    def test_a_lane_no_mac_takes_is_named_with_what_it_needs_and_what_each_mac_has(self):
        here = self.settings("here", {"wholeHost": False, "model": False})
        there = {"studio.local": self.settings("studio", {"wholeHost": False}, ["apple-m5-max"]),
                 "mini.local": self.settings("mini", {"wholeHost": False, "exclusive": False})}
        found = {warning.split(":")[0]: warning for warning in self.warnings(here, there)}
        # Each Mac is read once, however many lanes ask, all at once and so in no set order.
        self.assertEqual(sorted(self.asked), ["mini.local", "studio.local"])
        self.assertEqual(sorted(found), ["screen", "suite", "unity"])
        self.assertEqual(found["unity"],
            "unity: the lane needs a Mac of the farm that has the label unity-license, and none does, so the lane "
            "cannot run: this Mac lacks the label unity-license that the work needs (its labels: none, by its settings "
            "(/Users/o/here/mac-settings.json)); Studio lacks the label unity-license that the work needs (its labels: "
            "apple-m5-max, by its settings (/Users/o/studio/mac-settings.json)); Mini lacks the label unity-license "
            "that the work needs (its labels: none, by its settings (/Users/o/mini/mac-settings.json)); give a Mac what "
            "the lane needs in its mac-settings.json, or correct the lane")
        self.assertIn("suite: the lane needs a Mac of the farm that takes whole-host gates, and none does", found["suite"])
        self.assertIn("Studio takes no whole-host gates, by its settings (/Users/o/studio/mac-settings.json)", found["suite"])
        self.assertIn("takes foreground work, and none does", found["screen"])
        self.assertIn("or `qk farm foreground NAME` for foreground work", found["screen"])
        # The positive controls beside them: Studio takes exclusive work and this Mac's limits model runs aside,
        # Studio and Mini take them; an ordinary heavy lane and a self lane are never judged.
        for name in ("gate", "story", "build", "parts"): self.assertNotIn(name, found)
        # A lane, and a group by its lanes', that needs labels and a kind says both, and each Mac's labels.
        lanes = {"group": {"parallel": ["unity", "suite"], "wholeHost": True, "resource": "exclusive"},
                 "unity": self.LANES["unity"], "suite": self.LANES["suite"]}
        there["mini.local"] = self.settings("mini", {"exclusive": False}, ["unity-license"])
        [group] = [warning for warning in self.warnings(here, there, lanes) if warning.startswith("group")]
        self.assertIn("the lane needs a Mac of the farm that takes exclusive work and has the label unity-license", group)
        self.assertIn("Mini takes no exclusive work, by its settings (/Users/o/mini/mac-settings.json) (its labels: "
                      "unity-license)", group)

    def test_a_lane_some_mac_takes_is_not_named_and_nothing_is_read_when_no_lane_needs_it(self):
        here = self.settings("here")
        there = {"studio.local": self.settings("studio", {"foreground": True}),
                 "mini.local": self.settings("mini", labels=["unity-license"])}
        self.assertEqual(self.warnings(here, there), [])
        # A project whose lanes need nothing a Mac may decline asks no Mac at all.
        self.asked.clear()
        self.assertEqual(self.warnings(here, there, {"build": self.LANES["build"], "parts": self.LANES["parts"]}), [])
        self.assertEqual(self.asked, [])

    def test_a_mac_that_cannot_be_read_is_unknown_and_never_said_to_lack(self):
        here = self.settings("here")
        # Mini cannot be reached, and Studio's toolkit reports no settings, as one before them.
        there = {"studio.local": {"slots": 3, "slotsMax": 9, "busy": []},
                 "mini.local": remote.Unreachable("mini.local cannot be reached", "wake it")}
        unity, screen = sorted(self.warnings(here, there), reverse=True)
        self.assertIn("screen: the lane needs a Mac of the farm that takes foreground work, and none that could be "
                      "read does, so it runs only if Studio or Mini does", screen)
        self.assertTrue(unity.startswith("unity: the lane needs a Mac of the farm that has the label unity-license, "
                                         "and none that could be read does, so it runs only if Studio or Mini does: "
                                         "this Mac lacks the label unity-license"), unity)
        self.assertIn("whether Studio takes it is unknown: its toolkit does not report its settings", unity)
        self.assertIn("whether Mini takes it is unknown: mini.local cannot be reached", unity)
        self.assertNotIn("Mini lacks", unity)
        self.assertNotIn("Studio lacks", unity)
        # One Mac that is read and takes it is enough, whatever the others.
        there["studio.local"] = self.settings("studio", {"foreground": True}, labels=["unity-license"])
        self.assertEqual(self.warnings(here, there), [])
        # This Mac's own settings that cannot be read are unknown too.
        there["studio.local"] = self.settings("studio", {"foreground": True})
        [unity] = self.warnings("Invalid Mac settings: /x is not JSON", there)
        self.assertIn("whether this Mac takes it is unknown: Invalid Mac settings: /x is not JSON", unity)

    def test_the_macs_are_read_at_once_so_doctor_waits_about_one_read_not_their_sum(self):
        # vocaro/queuekeeper#224, the owner's decision of 2026-11-19: a Mac that is asleep must not make doctor slow.
        self.macs["air.local"] = {"id": "CCCC-3", "name": "Air"}
        self.add("air.local")
        here, there = self.settings("here"), {host: self.settings(host, {"foreground": True}, ["unity-license"])
                                              for host in ("studio.local", "mini.local", "air.local")}
        def slow(host):
            time.sleep(0.5)
            return there[host]
        began = time.monotonic()
        found = remote.unplaceable_lanes({"lanes": self.LANES}, read_here=lambda: here, read_there=slow, limit=5)
        elapsed = time.monotonic() - began
        self.assertEqual(found, [])
        self.assertLess(elapsed, 1.2, "three reads of 0.5 s each took their sum, not one read")
        self.assertGreaterEqual(elapsed, 0.5)
        self.assertEqual(remote.SETTINGS_TIMEOUT, 10)

    def test_a_mac_that_does_not_answer_in_time_is_unknown_and_never_said_to_lack(self):
        here = self.settings("here")
        released = threading.Event()
        self.addCleanup(released.set)
        def read(host):
            if host == "mini.local":
                released.wait(30)  # asleep: it answers only after doctor stops waiting
                return self.settings("mini", {"foreground": True}, ["unity-license"])
            return self.settings("studio")
        began = time.monotonic()
        unity, screen = sorted(remote.unplaceable_lanes({"lanes": self.LANES}, read_here=lambda: here,
                                                        read_there=read, limit=0.3), reverse=True)
        self.assertLess(time.monotonic() - began, 2)
        self.assertIn("so it runs only if Mini does", unity)
        self.assertIn("whether Mini takes it is unknown: it did not answer in 0.3s", unity)
        self.assertIn("Studio lacks the label unity-license", unity)
        self.assertNotIn("Mini lacks", unity)
        self.assertIn("whether Mini takes it is unknown: it did not answer in 0.3s", screen)
        self.assertNotIn("Mini takes no", screen)

    def test_a_lane_that_stays_here_is_judged_by_this_macs_settings_alone(self):
        lanes = {"local": {"command": ["true"], "here": "it reads this Mac's keychain", "labels": ["unity-license"]},
                 "screen": {"command": ["true"], "here": "it uses this Mac's display", "foreground": True}}
        there = {"studio.local": self.settings("studio", labels=["unity-license"]), "mini.local": self.settings("mini")}
        [local] = self.warnings(self.settings("here"), there, lanes)
        self.assertTrue(local.startswith("local: the lane needs this Mac, where the lane says it runs, that has the "
                                         "label unity-license, and none does"), local)
        self.assertNotIn("Studio", local)
        # Its foreground runs within the owner's window whatever the settings say, so it is not judged by them.
        self.assertFalse(any(warning.startswith("screen") for warning in self.warnings(self.settings("here"), there, lanes)))
        self.assertEqual(self.warnings(self.settings("here", labels=["unity-license"]), there, lanes), [])

    def test_doctor_warns_of_the_lane_by_this_macs_own_settings_file(self):
        (offload_host.admission_directory() / offload_host.MAC_SETTINGS).write_text(json.dumps({"labels": ["other"]}))
        with farm.changing() as macs: macs.clear()
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(remote, "compare_hosts", return_value={"host": None, "errors": [], "unchecked": [],
                                                                    "pythonCrashes": {}}), \
                patch.object(cli.shutil, "which", return_value="/usr/bin/true"), \
                patch.object(cli, "source_identity", return_value={}), \
                patch.object(offload_host, "host_health", return_value={}), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            Path(directory, "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Example",
                "integration": "direct-main", "lanes": {"gate": {"command": ["true"], "timeout": 60},
                                                        "unity": {"command": ["true"], "timeout": 60,
                                                                  "labels": ["unity-license"]}}}))
            cli.main(["--project", directory, "doctor"])
        warnings = json.loads(output.getvalue())["warnings"]
        [unity] = [warning for warning in warnings if warning.startswith("unity:")]
        self.assertIn("this Mac lacks the label unity-license that the work needs (its labels: other, by its "
                      f"settings ({offload_host.admission_directory() / offload_host.MAC_SETTINGS}))", unity)
        self.assertFalse([warning for warning in warnings if warning.startswith("gate:")])


class AdoptRootTests(unittest.TestCase):
    """A Mac joining the farm moves the directory it kept under its earlier name, with its checkouts in use again."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name).resolve()
        self.old, self.new = self.home / offload_host.EARLIER_ROOT, self.home / remote.ROOT

    def git(self, *arguments):
        environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        done = subprocess.run(["git", *arguments], capture_output=True, text=True, env=environment)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout.strip()

    def warm(self):
        """A repository under the earlier root with a commit and one checkout of it in slot 0."""
        source, repository = self.home / "source", self.old / "repositories/source.git"
        self.git("init", "--quiet", str(source))
        (source / "file.txt").write_text("warm\n")
        self.git("-C", str(source), "add", "file.txt")
        self.git("-C", str(source), "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "--quiet", "-m", "one")
        self.git("clone", "--quiet", "--bare", str(source), str(repository))
        checkout = self.old / "checkouts/source/0/source"
        self.git("--git-dir", str(repository), "worktree", "add", "--detach", "--quiet", str(checkout), "HEAD")
        (self.old / "git-credentials").write_text("token\n")
        return self.git("-C", str(source), "rev-parse", "HEAD")

    def test_the_directory_moves_whole_and_its_checkouts_work_where_they_now_are(self):
        commit = self.warm()
        found = offload_host.adopt_root(remote.ROOT, home=self.home)
        self.assertEqual(found, {"moved": True, "from": str(self.old), "to": str(self.new), "linked": str(self.old)})
        # The old path stays as a link, for build state that recorded it, such as Xcode's of a package's artifacts.
        self.assertTrue(self.old.is_symlink())
        self.assertEqual(self.old.resolve(), self.new)
        self.assertEqual((self.old / "checkouts/source/0/source/file.txt").read_text(), "warm\n")
        self.assertEqual((self.new / "git-credentials").read_text(), "token\n")
        checkout = self.new / "checkouts/source/0/source"
        self.assertEqual(self.git("-C", str(checkout), "rev-parse", "HEAD"), commit)
        self.assertEqual(self.git("-C", str(checkout), "status", "--porcelain"), "")
        listed = self.git("--git-dir", str(self.new / "repositories/source.git"), "worktree", "list", "--porcelain")
        self.assertIn(f"worktree {checkout}", listed)
        self.assertNotIn("prunable", listed)
        # Asked again, as a Mac added again is, there is nothing left to move.
        self.assertEqual(offload_host.adopt_root(remote.ROOT, home=self.home), {"moved": False})
        # A Mac moved before the link was made is given it when it is added again, and one with no checkouts is not.
        self.old.unlink()
        self.assertEqual(offload_host.adopt_root(remote.ROOT, home=self.home), {"moved": False, "linked": str(self.old)})
        self.assertEqual(self.old.resolve(), self.new)
        self.old.unlink()
        shutil.rmtree(self.new / "checkouts")
        self.assertEqual(offload_host.adopt_root(remote.ROOT, home=self.home), {"moved": False})
        self.assertFalse(self.old.is_symlink())

    def test_nothing_moves_onto_a_directory_that_exists_or_from_under_a_run(self):
        self.warm()
        self.new.mkdir(parents=True)
        found = offload_host.adopt_root(remote.ROOT, home=self.home)
        self.assertEqual(found["moved"], False)
        self.assertIn("exists already", found["left"])
        self.new.rmdir()
        start = remote.resources.process_start(os.getpid())
        (self.old / "checkouts/source/0.lock").write_text(json.dumps({"pid": os.getpid(), "start": start}))
        found = offload_host.adopt_root(remote.ROOT, home=self.home)
        self.assertEqual(found["moved"], False)
        self.assertIn(f"while a run holds a checkout in it (process {os.getpid()})", found["left"])
        self.assertTrue(self.old.is_dir())
        # A lock whose run has ended, as every used slot keeps, holds nothing back.
        (self.old / "checkouts/source/0.lock").write_text(json.dumps({"pid": os.getpid(), "start": "Thu Jan  1 00:00:00 1970"}))
        self.assertTrue(offload_host.adopt_root(remote.ROOT, home=self.home)["moved"])

    def test_a_mac_says_which_mac_it_is_as_it_joins(self):
        with patch.dict(os.environ, {offload_host.HARDWARE_VARIABLE: "CCCC-3"}), \
                patch.object(offload_host, "mac_name", return_value="Loft"), \
                patch.object(offload_host.Path, "home", return_value=self.home):
            self.assertEqual(offload_host.answer({"join": {"root": remote.ROOT}}), {"id": "CCCC-3", "name": "Loft", "moved": False})


if __name__ == "__main__":
    unittest.main()
