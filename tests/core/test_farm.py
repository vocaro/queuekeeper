"""farm: the Macs beside this one that take work, as their owner adds, renames, pauses and removes them."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
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
        self.assertEqual((farm.read(), farm.destinations(), remote.configured(), remote.gate_hosts()), ([], (), None, None))

    def test_a_mac_is_added_under_its_own_name_or_the_one_given_and_tried_in_the_order_added(self):
        first, second = self.add("studio.local"), self.add("mini.local", "render-box")
        self.assertEqual((first["name"], first["id"], first["ssh"], first["state"]),
                         ("Studio", "AAAA-1", "studio.local", farm.ACTIVE))
        self.assertEqual(second["name"], "render-box")
        self.assertEqual(farm.destinations(), ("studio.local", "mini.local"))
        self.assertEqual((remote.configured(), remote.gate_hosts()), ("studio.local", ("studio.local", "mini.local")))
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
        self.assertEqual((remote.configured(), remote.gate_hosts()), ("mini.local", None))
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
                                     "to": "/Users/o/Development/queuekeeper-farm"}
        status, output, error = self.qk("add", "studio.local")
        self.assertEqual((status, error), (0, ""))
        self.assertEqual(output.splitlines(), [
            "Added Studio  active  ssh studio.local",
            "Moved /Users/o/Development/build-host there to /Users/o/Development/queuekeeper-farm, with its checkouts",
            "Next, give it the tools the lanes need: `qk remote setup --host Studio`"])
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
        self.assertEqual(found, {"moved": True, "from": str(self.old), "to": str(self.new)})
        self.assertFalse(self.old.exists())
        self.assertEqual((self.new / "git-credentials").read_text(), "token\n")
        checkout = self.new / "checkouts/source/0/source"
        self.assertEqual(self.git("-C", str(checkout), "rev-parse", "HEAD"), commit)
        self.assertEqual(self.git("-C", str(checkout), "status", "--porcelain"), "")
        listed = self.git("--git-dir", str(self.new / "repositories/source.git"), "worktree", "list", "--porcelain")
        self.assertIn(f"worktree {checkout}", listed)
        self.assertNotIn("prunable", listed)
        # Asked again, as a Mac added again is, there is nothing left to move.
        self.assertEqual(offload_host.adopt_root(remote.ROOT, home=self.home), {"moved": False})

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
