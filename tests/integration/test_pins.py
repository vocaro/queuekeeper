"""A run's pin set against real Git repositories: an origin, this Mac's checkout of a folder beside a project, and
the checkouts runs are pinned to (vocaro-engineering#497)."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src/python"))
from queuekeeper import config, disk, lane_results, offload_host, pins, remote
import host_isolation

# A plugin that says which of its versions loaded: `qk kit-says` prints VALUE, and names the file it came from.
KIT = ("from pathlib import Path\n"
       "from queuekeeper import plugins\n"
       "VALUE = {value!r}\n"
       "def says(arguments, project):\n"
       "    print(f'kit-says {{VALUE}} from {{Path(__file__).resolve()}}')\n"
       "    return 0\n"
       "def register(): plugins.COMMANDS['kit-says'] = ('say which kit loaded', says)\n")
# A lane that asks the plugin by a nested qk command, has a change land in this Mac's checkout of its folder as a
# fast-forward does, and asks again.
LANE = ("import subprocess, sys\n"
        "qk = [sys.executable, sys.argv[1], '--project', '.', 'kit-says']\n"
        "subprocess.run(qk, check=True)\n"
        "subprocess.run(['git', '-C', sys.argv[2], 'merge', '-q', '--ff-only', 'landing'], check=True)\n"
        "subprocess.run(qk, check=True)\n")


def git(directory, *words):
    done = subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                           "-C", str(directory), *words], capture_output=True, text=True)
    if done.returncode: raise AssertionError(f"git {' '.join(words)} in {directory}: {done.stderr}")
    return done.stdout.strip()


class Fixture(unittest.TestCase):
    """A home whose Development holds a project and kit, the folder its plugin loads from, a clone of an origin."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="pins")).resolve()
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.store = self.home / "pins"
        isolated = patch.dict(os.environ, {offload_host.PINS_DIRECTORY: str(self.store)})
        isolated.start()
        self.addCleanup(isolated.stop)
        os.environ.pop(pins.VARIABLE, None)
        self.origin = self.home / "kit.git"
        git(self.home, "init", "-q", "--bare", "-b", "main", str(self.origin))
        self.kit = self.home / "Development/kit"
        self.kit.parent.mkdir()
        git(self.home, "clone", "-q", str(self.origin), str(self.kit))
        self.one = self.publish("one")
        self.project = self.home / "Development/project"
        self.project.mkdir()
        (self.project / "queuekeeper.json").write_text(json.dumps({
            "schemaVersion": 1, "name": "fixture",
            "plugins": [{"module": "fixture_kit", "path": "../kit/src/python"}],
            "lanes": {"nested": {"command": ["python3", "lane.py", str(ROOT / "bin/qk"), str(self.kit)]}}}))
        (self.project / "lane.py").write_text(LANE)
        git(self.project, "init", "-q", "-b", "main")
        git(self.project, "add", ".")
        git(self.project, "commit", "-qm", "start")

    def publish(self, value):
        """A commit of kit that says value, on its origin's main and in this Mac's checkout: its hash."""
        (self.kit / "src/python").mkdir(parents=True, exist_ok=True)
        (self.kit / "src/python/fixture_kit.py").write_text(KIT.format(value=value))
        git(self.kit, "add", ".")
        git(self.kit, "commit", "-qm", value)
        git(self.kit, "push", "-q", "origin", "HEAD:main")
        git(self.kit, "fetch", "-q", "origin")
        return git(self.kit, "rev-parse", "HEAD")

    def pinned(self, name, commit):
        return self.store / name / commit / name

    def qk(self, *words, **variables):
        return subprocess.run([sys.executable, str(ROOT / "bin/qk"), "--project", str(self.project), *words],
                              cwd=self.project, capture_output=True, text=True, env={**os.environ, **variables})


class PinnedCheckoutTests(Fixture):
    def test_it_is_one_read_only_checkout_for_each_commit_that_no_landing_moves(self):
        made = offload_host.pinned_checkout(self.kit, "kit", self.one)
        # Named as the folder is, since SwiftPM calls a local package by its folder's name (vocaro-engineering#497).
        self.assertEqual(made, self.store / "kit" / self.one / "kit")
        self.assertEqual(offload_host.pin_head(made), self.one)
        file = made / "src/python/fixture_kit.py"
        self.assertIn("'one'", file.read_text())
        self.assertFalse(os.access(file, os.W_OK))
        ready = self.store / "kit" / f"{self.one}.ready"
        os.utime(ready, (1, 1))
        two = self.publish("two")
        # Asked for again, it is the same checkout, used now, and what landed since is another beside it.
        self.assertEqual(offload_host.pinned_checkout(self.kit, "kit", self.one), made)
        self.assertGreater(ready.stat().st_mtime, time.time() - 60)
        self.assertIn("'one'", file.read_text())
        other = offload_host.pinned_checkout(self.kit, "kit", two)
        self.assertNotEqual(other, made)
        self.assertIn("'two'", (other / "src/python/fixture_kit.py").read_text())
        self.assertEqual(offload_host.pin_head(made), self.one)

    def test_one_left_half_made_is_made_again(self):
        made = offload_host.pinned_checkout(self.kit, "kit", self.one)
        (self.store / "kit" / f"{self.one}.ready").unlink()
        (made / "src/python/fixture_kit.py").unlink()
        self.assertEqual(offload_host.pinned_checkout(self.kit, "kit", self.one), made)
        self.assertIn("'one'", (made / "src/python/fixture_kit.py").read_text())

    def test_whatever_else_holds_the_commits_folder_is_replaced(self):
        held = self.store / "kit" / self.one
        (held / "src").mkdir(parents=True)
        (held / "src/stray").write_text("not a checkout")
        made = offload_host.pinned_checkout(self.kit, "kit", self.one)
        self.assertEqual(sorted(path.name for path in held.iterdir()), ["kit"])
        self.assertEqual(offload_host.pin_head(made), self.one)

    def test_a_commit_the_checkout_lacks_is_fetched_from_its_origin_and_one_no_origin_holds_is_refused(self):
        # As another Mac's own checkout is when a round pins a commit that landed since its last connection.
        there = self.home / "there/Development/kit"
        there.parent.mkdir(parents=True)
        git(self.home, "clone", "-q", str(self.origin), str(there))
        two = self.publish("two")
        self.assertNotEqual(git(there, "rev-parse", "HEAD"), two)
        made = offload_host.pinned_checkout(there, "kit", two, home=self.home / "there")
        self.assertIn("'two'", (made / "src/python/fixture_kit.py").read_text())
        # Its own ~/Development/kit stays where it was: the pinned checkout is a worktree of it, not a move of it.
        self.assertEqual(git(there, "rev-parse", "HEAD"), self.one)
        with self.assertRaisesRegex(RuntimeError, "fetch"):
            offload_host.pinned_checkout(there, "kit", "1" * 40, home=self.home / "there")
        with self.assertRaisesRegex(RuntimeError, "does not hold"):
            offload_host.pinned_checkout(there, "kit", "1" * 40, home=self.home / "there", fetch=False)

    def test_one_unused_for_a_week_is_listed_and_removed_and_one_in_use_stays(self):
        old = offload_host.pinned_checkout(self.kit, "kit", self.one)
        new = offload_host.pinned_checkout(self.kit, "kit", self.publish("two"))
        os.utime(self.store / "kit" / f"{self.one}.ready", (1, 1))
        stale = offload_host.stale_pins()
        self.assertEqual([found[0] for found in stale], [old])
        for found in stale: offload_host.remove_pin(*found)
        self.assertFalse(old.parent.exists())
        self.assertTrue(new.is_dir())
        self.assertNotIn(str(old), git(self.kit, "worktree", "list"))


    def test_qk_disk_counts_them_and_offers_those_past_the_rule_that_no_process_uses(self):
        old = offload_host.pinned_checkout(self.kit, "kit", self.one)
        new = offload_host.pinned_checkout(self.kit, "kit", self.publish("two"))
        host, sizes = SimpleNamespace(now=time.time()), SimpleNamespace(of=lambda path: 5)
        idle = SimpleNamespace(of=lambda path: None)
        kept, = disk.pinned_checkouts(host, idle, sizes)
        self.assertEqual((kept.category, kept.path, kept.status, kept.size, kept.command),
                         ("pinned-checkouts", str(self.store), disk.KEPT, 10, None))
        self.assertIn("2 checkouts of 1 folder(s); none past the rule: one stays until no run has used it for 7 days",
                      kept.reason)
        os.utime(self.store / "kit" / f"{self.one}.ready", (1, 1))
        offered, = disk.pinned_checkouts(host, idle, sizes)
        self.assertEqual((offered.status, offered.size, offered.command[-2:]), (disk.CANDIDATE, 5, ["disk", "--prune-pins"]))
        # One a process still uses is not offered, however long ago a run last pinned it.
        used = SimpleNamespace(of=lambda path: object() if Path(path) == old else None)
        self.assertEqual(disk.pinned_checkouts(host, used, sizes)[0].status, disk.KEPT)
        said = []
        self.assertEqual(disk.prune_pins(said.append), 0)
        self.assertEqual(said, [f"pruned {old}", f"1 pinned checkout pruned, unused for 7 days, in {self.store}"])
        self.assertFalse(old.exists())
        self.assertTrue(new.is_dir())

    def orphan(self):
        """A pin whose commit's folder was removed some other way, leaving its ready file (vocaro/queuekeeper#360)."""
        made = offload_host.pinned_checkout(self.kit, "kit", self.one)
        git(self.kit, "worktree", "remove", "--force", "--force", str(made))
        made.parent.rmdir()
        return made, self.store / "kit" / f"{self.one}.ready"

    def test_a_ready_file_left_with_no_checkout_is_listed_however_recent_and_removed_alone(self):
        made, ready = self.orphan()
        kept = offload_host.pinned_checkout(self.kit, "kit", self.publish("two"))
        self.assertEqual(offload_host.stale_pins(), [(made, ready, self.kit)])
        self.assertTrue(offload_host.remove_pin(*offload_host.stale_pins()[0]))
        self.assertFalse(ready.exists())
        self.assertEqual(offload_host.stale_pins(), [])
        self.assertTrue(kept.is_dir())
        self.assertTrue((self.store / "kit" / f"{git(self.kit, 'rev-parse', 'HEAD')}.ready").is_file())

    def test_a_ready_file_whose_checkout_a_run_made_after_it_was_listed_stays(self):
        made, ready = self.orphan()
        found, = offload_host.stale_pins()
        # A run pins the commit again between the listing and the removal, as one racing the prune does.
        self.assertEqual(offload_host.pinned_checkout(self.kit, "kit", self.one), made)
        self.assertFalse(offload_host.remove_pin(*found))
        self.assertTrue(ready.is_file())
        self.assertEqual(offload_host.pin_head(made), self.one)

    def test_qk_disk_counts_ready_files_with_no_checkout_and_prunes_them(self):
        made, ready = self.orphan()
        host, sizes = SimpleNamespace(now=time.time()), SimpleNamespace(of=lambda path: 5)
        idle = SimpleNamespace(of=lambda path: None)
        offered, = disk.pinned_checkouts(host, idle, sizes)
        self.assertEqual((offered.status, offered.size, offered.command[-2:]), (disk.CANDIDATE, 5, ["disk", "--prune-pins"]))
        self.assertIn("0 checkouts of 0 folder(s), and 1 ready file with no checkout left; none past the rule",
                      offered.reason)
        said = []
        self.assertEqual(disk.prune_pins(said.append), 0)
        self.assertEqual(said, [f"pruned {ready}, which had no checkout beside it",
                                f"0 pinned checkouts pruned, unused for 7 days, in {self.store}",
                                "1 ready file with no checkout left pruned"])
        self.assertFalse(ready.exists())
        self.assertEqual(disk.pinned_checkouts(host, idle, sizes), [])


class PlaceSiblingsTests(Fixture):
    def test_the_farm_mac_links_each_folder_at_the_commit_sent_in_place_of_its_live_checkout(self):
        slot = self.home / "build-host/checkouts/Example/0"
        (slot / "Example").mkdir(parents=True)
        # What the layout before this left under the name: a link to the live checkout, which every connection moves.
        (slot / "kit").symlink_to(self.kit, target_is_directory=True)
        two = self.publish("two")
        said = []
        with patch.object(offload_host, "say", said.append):
            placed = offload_host.place_siblings(slot, [{"name": "kit", "commit": self.one}], home=self.home)
            again = offload_host.place_siblings(slot, [{"name": "kit", "commit": self.one}], home=self.home)
        root = self.home / offload_host.PINS / "kit" / self.one / "kit"
        self.assertEqual(placed, {"kit": {"commit": self.one, "root": str(root)}})
        self.assertEqual(again, placed)
        self.assertEqual((slot / "kit").resolve(), root.resolve())
        self.assertIn("'one'", (slot / "kit/src/python/fixture_kit.py").read_text())
        self.assertEqual(git(self.kit, "rev-parse", "HEAD"), two)
        self.assertEqual(sum(f"is kit at {self.one[:12]}" in line for line in said), 1)

    def test_a_folder_sent_with_no_commit_is_a_link_to_its_own_checkout_as_it_stands_and_in_no_set(self):
        slot = self.home / "build-host/checkouts/Example/0"
        (slot / "Example").mkdir(parents=True)
        said = []
        with patch.object(offload_host, "say", said.append):
            placed = offload_host.place_siblings(slot, [{"name": "kit"}, {"name": "absent"}], home=self.home)
        self.assertEqual(placed, {})
        self.assertEqual((slot / "kit").resolve(), self.kit.resolve())
        self.assertFalse((slot / "absent").exists())
        self.assertEqual([("as it stands" in line, "has no" in line) for line in said], [(True, False), (False, True)])

    def test_a_folder_it_has_no_checkout_of_and_a_commit_it_cannot_fetch_are_errors_in_the_set_and_no_link(self):
        slot = self.home / "build-host/checkouts/Example/0"
        (slot / "Example").mkdir(parents=True)
        said = []
        with patch.object(offload_host, "say", said.append):
            placed = offload_host.place_siblings(slot, [{"name": "absent", "commit": self.one},
                                                        {"name": "kit", "commit": "2" * 40}], home=self.home)
        self.assertIn("this Mac has no", placed["absent"]["error"])
        self.assertIn("fetch", placed["kit"]["error"])
        self.assertFalse((slot / "absent").exists() or (slot / "kit").exists())
        self.assertEqual(len(said), 2)
        # A qk command under that set ends before any lane, saying which folder and commit and why (cli.could_not_start).
        with self.assertRaisesRegex(pins.Unpinned, f"kit pinned at {'2' * 12} for this run could not be had on this "
                                                   "Mac: .*fetch"):
            pins.verify({"kit": placed["kit"]})


class MakeTests(Fixture):
    def test_a_folder_is_pinned_at_its_origin_main_whatever_its_checkout_holds(self):
        # A session's commit and edit in the shared checkout, neither on origin/main.
        (self.kit / "src/python/fixture_kit.py").write_text(KIT.format(value="committed here"))
        git(self.kit, "commit", "-qam", "a session's commit")
        (self.kit / "src/python/fixture_kit.py").write_text(KIT.format(value="edited here"))
        said = []
        made = pins.make(self.project, ["kit"], say=said.append)
        self.assertEqual(made, {"kit": {"commit": self.one, "root": str(self.pinned("kit", self.one))}})
        self.assertEqual(said, [f"kit at {self.one[:12]}, its origin/main, from {self.pinned('kit', self.one)}"])
        self.assertEqual(pins.local(made), [])
        self.assertEqual(pins.dispatched(self.project, ["kit", "absent"]),
                         [{"name": "kit", "commit": self.one}, {"name": "absent"}])

    def test_a_folder_that_is_no_checkout_or_names_no_origin_main_is_left_out_and_a_missing_one_is_refused(self):
        (self.home / "Development/plain/src").mkdir(parents=True)
        alone = self.home / "Development/alone"
        alone.mkdir()
        git(alone, "init", "-q", "-b", "main")
        said = []
        self.assertEqual(pins.make(self.project, ["plain", "alone"], say=said.append), {})
        self.assertEqual(said, [f"alone as {alone} stands: it names no origin/main to pin it at"])
        with self.assertRaisesRegex(pins.Unpinned, "this Mac has no .*/Development/absent"):
            pins.make(self.project, ["absent"])

    def test_sibling_chooses_a_commit_or_a_checkouts_working_tree_and_one_not_pushed_is_local(self):
        two = self.publish("two")
        chosen = pins.chosen(self.project, ["kit"], [f"kit={self.one}"])
        made = pins.make(self.project, ["kit"], chosen)
        self.assertEqual(made["kit"], {"commit": self.one, "root": str(self.pinned("kit", self.one)),
                                       "chosen": self.one})
        # A session's worktree of the folder, with a change it has not committed: a snapshot, which no origin holds.
        session = self.home / "session"
        git(self.kit, "worktree", "add", "-q", "--detach", str(session), two)
        (session / "src/python/fixture_kit.py").write_text(KIT.format(value="a session's"))
        made = pins.make(self.project, ["kit"], pins.chosen(self.project, ["kit"], [f"kit={session}"]))
        self.assertNotIn(made["kit"]["commit"], (self.one, two))
        self.assertIn("a session's", (Path(made["kit"]["root"]) / "src/python/fixture_kit.py").read_text())
        self.assertEqual((made["kit"]["chosen"], made["kit"]["local"]), (str(session), True))
        self.assertEqual(pins.local(made), ["kit"])
        self.assertEqual(git(session, "status", "--porcelain"), "M src/python/fixture_kit.py")
        for wrong, why in (["kit"], "names no folder and commit"), (["other=main"], "is no folder the project's"), \
                ([f"kit={self.one}", f"kit={two}"], "twice"), (["kit=nowhere"], "no directory and no commit"):
            with self.assertRaisesRegex(ValueError, why): pins.chosen(self.project, ["kit"], wrong)


class FurtherFolderTests(Fixture):
    """A folder the pinned one declares in its own queuekeeper.json, found by an environment variable: the toolkit
    loads apple-engineering and AXVerify so, for a project that lists neither."""

    def setUp(self):
        super().setUp()
        self.more = self.home / "Development/more"
        git(self.home, "init", "-q", "--bare", "-b", "main", str(self.home / "more.git"))
        git(self.home, "clone", "-q", str(self.home / "more.git"), str(self.more))
        self.first = self.land_more("first")
        (self.kit / "queuekeeper.json").write_text(json.dumps({
            "schemaVersion": 1, "name": "kit", "siblings": {"more": "MORE_ROOT", "absent": "ABSENT_ROOT"}}))
        self.declares = self.publish("declares")
        configuration = json.loads((self.project / "queuekeeper.json").read_text())
        configuration["lanes"]["asks"] = {"command": [
            "python3", "-c", "import os; print('more-is', os.environ.get('MORE_ROOT'), os.environ.get('ABSENT_ROOT'))"]}
        (self.project / "queuekeeper.json").write_text(json.dumps(configuration))
        git(self.project, "commit", "-qam", "a lane that asks")

    def land_more(self, value):
        (self.more / "value.txt").write_text(value)
        (self.more / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "more",
                                                               "siblings": {"kit": "KIT_ROOT"}}))
        git(self.more, "add", ".")
        git(self.more, "commit", "-qm", value)
        git(self.more, "push", "-q", "origin", "HEAD:main")
        git(self.more, "fetch", "-q", "origin")
        return git(self.more, "rev-parse", "HEAD")

    def test_what_a_pinned_folder_declares_is_pinned_too_and_its_variable_names_the_checkout(self):
        self.assertEqual(config.sibling_variables(self.project), {"kit": "KIT_ROOT", "more": "MORE_ROOT",
                                                                 "absent": "ABSENT_ROOT"})
        said = []
        made = pins.make(self.project, config.sibling_variables(self.project), say=said.append)
        root = self.pinned("more", self.first)
        # The two name each other, which ends; the one this Mac has nothing of is said and left out.
        self.assertEqual(made, {"kit": {"commit": self.declares, "root": str(self.pinned("kit", self.declares)),
                                        "variable": "KIT_ROOT"},
                                "more": {"commit": self.first, "root": str(root), "variable": "MORE_ROOT"}})
        self.assertIn(f"no absent: this Mac has no {self.home / 'Development/absent'}", said)
        self.assertEqual(pins.exported(made), {"KIT_ROOT": made["kit"]["root"], "MORE_ROOT": str(root)})
        self.assertEqual(pins.exported({"more": {**made["more"], "error": "no such commit"}}), {})
        with patch.dict(os.environ, {pins.VARIABLE: pins.encoded(made)}):
            sent = pins.dispatched(self.project, config.sibling_variables(self.project))
        self.assertEqual(sent, [{"name": "kit", "commit": self.declares, "variable": "KIT_ROOT"},
                                {"name": "more", "commit": self.first, "variable": "MORE_ROOT"},
                                {"name": "absent", "variable": "ABSENT_ROOT"}])
        slot = self.home / "build-host/checkouts/Example/0"
        (slot / "Example").mkdir(parents=True)
        with patch.object(offload_host, "say", lambda text: None):
            placed = offload_host.place_siblings(slot, sent, home=self.home)
        self.assertEqual(placed["more"], {"commit": self.first, "variable": "MORE_ROOT",
                                          "root": str(self.home / offload_host.PINS / "more" / self.first / "more")})
        self.assertNotIn("absent", placed)

    def test_the_list_is_read_at_the_commit_pinned_not_as_the_checkout_stands(self):
        # A session's edit of the shared checkout's list, which no run reads.
        (self.kit / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "kit"}))
        made = pins.make(self.project, ["kit"])
        self.assertEqual(made["more"]["variable"], "MORE_ROOT")
        chosen = pins.chosen(self.project, ["kit"], [f"kit={self.one}"])
        self.assertEqual(list(pins.make(self.project, ["kit"], chosen)), ["kit"])  # which declared none

    def test_a_lane_finds_the_folder_by_its_variable_at_the_commit_the_run_began_with(self):
        ran = self.qk("run", "--here", "asks")
        self.assertEqual(ran.returncode, 0, ran.stderr)
        root = self.pinned("more", self.first)
        self.assertIn(f"queuekeeper: this run loads more at {self.first[:12]}, its origin/main, from {root}\n", ran.stderr)
        log = (self.project / ".build/queuekeeper/asks/run.log").read_text()
        self.assertIn(f"more-is {root} None\n", log)
        receipt = json.loads((self.project / ".build/queuekeeper/asks/source.json").read_text())
        self.assertEqual(receipt["siblings"]["more"], {"commit": self.first, "root": str(root), "variable": "MORE_ROOT"})
        # A command that inherits the set, as a later lane of a checkpoint does, after a landing in the folder.
        self.land_more("second")
        held = {pins.VARIABLE: pins.encoded(pins.make(self.project, ["kit"]) | {"more": receipt["siblings"]["more"]})}
        again = self.qk("run", "--here", "asks", **held)
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn(f"more-is {root} None\n", (self.project / ".build/queuekeeper/asks/run.log").read_text())
        self.assertEqual((root / "value.txt").read_text(), "first")

    def test_siblings_that_names_no_folder_and_variable_is_refused(self):
        for wrong in (["more"], {"more": "more_root"}, {"../more": "MORE_ROOT"}, {"more": None}):
            with self.subTest(siblings=wrong), self.assertRaisesRegex(ValueError, "siblings"):
                config.parse(json.dumps({"schemaVersion": 1, "name": "fixture", "siblings": wrong}))
        self.assertEqual(config.parse(json.dumps({"schemaVersion": 1, "name": "fixture",
                                                  "siblings": {"more": "MORE_ROOT"}}))["siblings"], {"more": "MORE_ROOT"})


class HeldTests(Fixture):
    def test_plugins_load_from_the_pinned_root_and_a_set_that_is_not_what_it_says_is_refused(self):
        live = [("fixture_kit", str(self.kit / "src/python"))]
        self.assertEqual(config.plugins_of(self.project), live)
        made = pins.make(self.project, ["kit"])
        with patch.dict(os.environ, {pins.VARIABLE: pins.encoded(made)}):
            self.assertEqual(config.plugins_of(self.project),
                             [("fixture_kit", str(self.pinned("kit", self.one) / "src/python"))])
        # A folder the set does not name, as one of another project's run, is found beside the project as ever.
        with patch.dict(os.environ, {pins.VARIABLE: pins.encoded({"other": made["kit"]})}):
            self.assertEqual(config.plugins_of(self.project), live)
        gone = {"kit": {"commit": self.one, "root": str(self.home / "gone")}}
        moved = {"kit": {"commit": "3" * 40, "root": made["kit"]["root"]}}
        for held, why in (pins.encoded(gone), "has no checkout at"), (pins.encoded(moved), f"is at {self.one[:12]} in"), \
                ("[]", "holds no pin set"), ("{", "holds no pin set"):
            with patch.dict(os.environ, {pins.VARIABLE: held}), self.assertRaisesRegex(pins.Unpinned, why):
                config.plugins_of(self.project)

    def test_a_command_under_a_set_whose_checkout_is_gone_could_not_start(self):
        gone = pins.encoded({"kit": {"commit": self.one, "root": str(self.home / "gone")}})
        ran = self.qk("--version", **{pins.VARIABLE: gone})
        self.assertEqual(ran.returncode, lane_results.TOOL_NOT_STARTED, ran.stderr)
        self.assertIn(f"{lane_results.TOOL_SAID}Unpinned: kit pinned at {self.one[:12]} for this run has no checkout at "
                      f"{self.home / 'gone'}, which QUEUEKEEPER_SIBLINGS names\n", ran.stderr)


class RunTests(Fixture):
    def test_a_landing_in_the_folder_mid_run_reaches_no_later_command_of_the_lane(self):
        two = self.publish("two")
        git(self.kit, "branch", "landing", two)
        git(self.kit, "reset", "-q", "--hard", self.one)
        git(self.kit, "update-ref", "refs/remotes/origin/main", self.one)
        ran = self.qk("run", "--here", "nested")
        self.assertEqual(ran.returncode, 0, ran.stderr)
        self.assertIn(f"queuekeeper: this run loads kit at {self.one[:12]}, its origin/main, from "
                      f"{self.pinned('kit', self.one)}\n", ran.stderr)
        log = (self.project / ".build/queuekeeper/nested/run.log").read_text()
        pinned = self.pinned("kit", self.one) / "src/python/fixture_kit.py"
        self.assertEqual([line for line in log.splitlines() if line.startswith("kit-says")],
                         [f"kit-says one from {pinned}"] * 2)
        # The landing did reach the shared checkout, which a command of no run still loads as it stands.
        self.assertEqual(git(self.kit, "rev-parse", "HEAD"), two)
        self.assertIn(f"kit-says two from {self.kit / 'src/python/fixture_kit.py'}", self.qk("kit-says").stdout)
        receipt = json.loads((self.project / ".build/queuekeeper/nested/source.json").read_text())
        self.assertEqual(receipt["siblings"], {"kit": {"commit": self.one, "root": str(self.pinned("kit", self.one))}})

    def declare_kit(self, revision):
        path = self.project / "queuekeeper.json"
        settings = json.loads(path.read_text())
        settings["siblings"] = {"kit": {"variable": "KIT_ROOT", "revision": revision}}
        settings["lanes"]["check"] = {
            "command": [sys.executable, "-c", """
import os, subprocess, sys
from pathlib import Path
root = Path(os.environ['KIT_ROOT'])
sys.path.insert(0, str(root / 'src/python'))
import fixture_kit
loaded = subprocess.check_output([sys.executable, sys.argv[1], 'kit-says'], text=True).strip()
assert loaded == f'kit-says {fixture_kit.VALUE} from {Path(fixture_kit.__file__).resolve()}', loaded
print(loaded)
""", str(ROOT / "bin/qk")]}
        path.write_text(json.dumps(settings))

    def test_exact_pin_agrees_with_plugin_and_command_root_on_both_checkout_layouts(self):
        there = self.home / "there/Development/kit"
        there.parent.mkdir(parents=True)
        git(self.home, "clone", "-q", str(self.origin), str(there))
        two = self.publish("two")
        three = self.publish("three")
        self.declare_kit(two.upper())
        ran = self.qk("run", "--here", "check")
        self.assertEqual(ran.returncode, 0, ran.stderr)
        self.assertIn(f"kit at {two[:12]}, declared revision", ran.stderr)
        log = self.project / ".build/queuekeeper/check/run.log"
        self.assertIn(f"kit-says two from {self.pinned('kit', two)}/src/python/fixture_kit.py", log.read_text())
        receipt = json.loads(log.with_name("source.json").read_text())
        self.assertEqual(receipt["siblings"]["kit"],
                         {"commit": two, "root": str(self.pinned("kit", two)), "variable": "KIT_ROOT"})

        # Dispatch the commit, then prepare it on a Mac whose clone predates that commit.
        self.assertEqual(remote.sibling_requests(self.project), [])
        names = config.sibling_folders(self.project)
        made = pins.make(self.project, names)
        self.assertEqual(pins.recorded(made), receipt["siblings"])
        with patch.dict(os.environ, {pins.VARIABLE: pins.encoded(made)}):
            sent = pins.dispatched(self.project, config.sibling_declarations(self.project))
        self.assertEqual(sent, [{"name": "kit", "commit": two, "variable": "KIT_ROOT"}])
        slot = self.home / "slot"
        slot.mkdir()
        with patch.object(offload_host, "say"):
            placed = offload_host.place_siblings(slot, sent, home=self.home / "there")
        ran = self.qk("run", "--here", "check", **{pins.VARIABLE: pins.encoded(placed)})
        self.assertEqual(ran.returncode, 0, ran.stderr)
        self.assertIn(f"kit-says two from {placed['kit']['root']}/src/python/fixture_kit.py", log.read_text())
        self.assertEqual(git(there, "rev-parse", "HEAD"), self.one)
        self.assertEqual(git(self.kit, "rev-parse", "HEAD"), three)

        admitted = self.qk("admit", "--here", "--", sys.executable, "-c", "import os; print(os.environ['KIT_ROOT'])")
        self.assertEqual(admitted.returncode, 0, admitted.stderr)
        self.assertIn(str(self.pinned("kit", two)), admitted.stdout)
        receipt = json.loads((self.project / ".build/queuekeeper/admit/source.json").read_text())
        self.assertEqual(receipt["siblings"]["kit"]["commit"], two)

        ran = self.qk("run", "--here", "--sibling", "kit=origin/main", "check")
        self.assertEqual(ran.returncode, 0, ran.stderr)
        self.assertIn(f"kit-says three from {self.pinned('kit', three)}/src/python/fixture_kit.py", log.read_text())

    def test_unavailable_declared_commit_stops_before_running_the_lane(self):
        self.declare_kit("4" * 40)
        ran = self.qk("run", "--here", "check")
        self.assertEqual(ran.returncode, lane_results.TOOL_NOT_STARTED, ran.stderr)
        self.assertIn("kit could not be pinned at " + "4" * 40, ran.stderr)
        self.assertFalse((self.project / ".build/queuekeeper/check/run.log").exists())

    def test_exact_declaration_rejects_refs_abbreviations_and_unknown_fields(self):
        base = {"schemaVersion": 1, "name": "fixture"}
        for entry in ({"variable": "KIT_ROOT", "revision": "main"},
                      {"variable": "KIT_ROOT", "revision": self.one[:12]},
                      {"variable": "KIT_ROOT", "revision": self.one, "other": True},
                      {"revision": self.one}):
            with self.subTest(entry=entry), self.assertRaisesRegex(ValueError, "siblings.kit"):
                config.parse(json.dumps({**base, "siblings": {"kit": entry}}))

    def test_sibling_is_refused_under_a_set_and_for_a_folder_the_project_does_not_load_from(self):
        made = pins.encoded(pins.make(self.project, ["kit"]))
        ran = self.qk("run", "--here", "--sibling", f"kit={self.one}", "nested", **{pins.VARIABLE: made})
        self.assertEqual(ran.returncode, 2, ran.stderr)
        self.assertIn(f"queuekeeper: --sibling kit={self.one} refused: this command runs under a pin set already",
                      ran.stderr)
        ran = self.qk("run", "--here", "--sibling=other=main", "nested")
        self.assertEqual(ran.returncode, 2, ran.stderr)
        self.assertIn("queuekeeper: --sibling names other, which is no folder the project's plugins load from: "
                      "they load from kit\n", ran.stderr)
        self.assertFalse((self.project / ".build/queuekeeper/nested/run.log").exists())

    def test_a_folder_chosen_at_a_commit_its_origin_lacks_keeps_the_lane_on_this_mac_and_says_so(self):
        session = self.home / "session"
        git(self.kit, "worktree", "add", "-q", "--detach", str(session), self.one)
        (session / "src/python/fixture_kit.py").write_text(KIT.format(value="a session's"))
        git(self.kit, "branch", "landing", self.one)
        ran = self.qk("run", "--sibling", f"kit={session}", "nested")
        self.assertEqual(ran.returncode, 0, ran.stderr)
        self.assertIn("queuekeeper: nested runs on this Mac: kit is pinned for this run at a commit its origin does not "
                      "hold, which another Mac cannot fetch; push it to run nested there\n", ran.stderr)
        log = (self.project / ".build/queuekeeper/nested/run.log").read_text()
        self.assertEqual(sum(line.startswith("kit-says a session's from") for line in log.splitlines()), 2)
        receipt = json.loads((self.project / ".build/queuekeeper/nested/source.json").read_text())
        self.assertEqual((receipt["siblings"]["kit"]["chosen"], receipt["siblings"]["kit"]["local"]), (str(session), True))
        ran = self.qk("run", "--offload-only", "--sibling", f"kit={session}", "nested")
        self.assertEqual(ran.returncode, 2, ran.stderr)
        self.assertIn("nested not run: --offload-only never runs it here, and kit is pinned", ran.stderr)


class LinkTests(Fixture):
    """A sibling a build reads by a relative path, "../FOLDER", linked beside the lane's checkout to the run's pinned
    checkout of it (pins.place_links, vocaro/queuekeeper#355)."""

    def declare(self, entry):
        path = self.project / "queuekeeper.json"
        settings = json.loads(path.read_text())
        settings["siblings"] = {"kit": entry}
        settings["lanes"]["relative"] = {"command": [sys.executable, "-c", (
            "from pathlib import Path\n"
            "kit = Path('../kit-link/src/python/fixture_kit.py')\n"
            "print('relative', kit.read_text().splitlines()[2], 'from', kit.resolve())\n")]}
        path.write_text(json.dumps(settings))

    def ran(self):
        ran = self.qk("run", "--here", "relative")
        self.assertEqual(ran.returncode, 0, ran.stderr)
        log = (self.project / ".build/queuekeeper/relative/run.log").read_text()
        return ran.stderr, [line for line in log.splitlines() if line.startswith("relative")]

    def read_from(self, value, commit):
        return [f"relative VALUE = {value!r} from {self.pinned('kit', commit).resolve()}/src/python/fixture_kit.py"]

    def test_a_lane_finds_its_sibling_by_a_relative_path_at_the_pinned_commit_and_a_pruned_pin_is_linked_again(self):
        self.declare({"variable": "KIT_ROOT", "link": "../kit-link"})
        link = self.home / "Development/kit-link"
        said, read = self.ran()
        self.assertIn(f"queuekeeper: {link} links kit at {self.one[:12]}, {self.pinned('kit', self.one)}\n", said)
        self.assertEqual(read, self.read_from("one", self.one))
        self.assertEqual(Path(os.readlink(link)), self.pinned("kit", self.one))
        # Nothing to do while it already links the run's commit.
        said, _ = self.ran()
        self.assertNotIn(str(link), said)
        # A new origin/main: the link Queuekeeper made points to the new pin.
        two = self.publish("two")
        said, read = self.ran()
        self.assertIn(f"{link} links kit at {two[:12]}", said)
        self.assertEqual(read, self.read_from("two", two))
        # A pin `qk disk --prune-pins` removed leaves the link dangling. The next lane pins the commit again at the
        # same path, which the link names, so it reads again; one left at another commit is linked anew below.
        later = time.time() + 30 * 24 * 60 * 60
        for checkout, ready, clone in offload_host.stale_pins(now=later):
            offload_host.remove_pin(checkout, ready, clone, now=later)
        self.assertTrue(link.is_symlink() and not link.exists())
        self.assertTrue(self.kit.is_dir())
        _, read = self.ran()
        self.assertEqual(read, self.read_from("two", two))
        for checkout, ready, clone in offload_host.stale_pins(now=later):
            offload_host.remove_pin(checkout, ready, clone, now=later)
        link.unlink()
        link.symlink_to(self.pinned("kit", self.one), target_is_directory=True)
        self.assertFalse(link.exists())
        said, read = self.ran()
        self.assertIn(f"{link} links kit at {two[:12]}", said)
        self.assertEqual(read, self.read_from("two", two))

    def place(self, made, folder):
        said = []
        return pins.place_links(self.project, {"kit": folder}, made, self.project, said.append), said

    def test_a_folder_a_persons_link_and_the_macs_own_checkout_stay_as_they_are(self):
        made = pins.make(self.project, {"kit": "KIT_ROOT"})
        folder = self.home / "Development/kit-folder"
        (folder / "inside").mkdir(parents=True)
        placed, said = self.place(made, "kit-folder")
        self.assertEqual(placed, {})
        self.assertTrue(folder.is_dir() and not folder.is_symlink() and (folder / "inside").is_dir())
        self.assertIn(f"{folder} left as it is: it is no link", said[0])
        elsewhere = self.home / "elsewhere"
        elsewhere.mkdir()
        person = self.home / "Development/kit-person"
        person.symlink_to(elsewhere, target_is_directory=True)
        placed, said = self.place(made, "kit-person")
        self.assertEqual((placed, Path(os.readlink(person))), ({}, elsewhere))
        self.assertIn(f"{person} left as it is: a link to {elsewhere} that Queuekeeper did not make", said[0])
        # The main checkout's sibling is the Mac's own checkout of it, which is what that checkout always read.
        self.assertEqual(self.place(made, "kit"), ({}, []))
        self.assertFalse(self.kit.is_symlink())
        # A link whose target is gone is replaced, as nothing could read it.
        dangling = self.home / "Development/kit-dangling"
        dangling.symlink_to(self.home / "gone", target_is_directory=True)
        self.place(made, "kit-dangling")
        self.assertEqual(dangling.resolve(), self.pinned("kit", self.one).resolve())
        # With no pinned checkout of the sibling there is nothing to link.
        placed, said = self.place({}, "kit-none")
        self.assertEqual(placed, {})
        self.assertFalse((self.home / "Development/kit-none").exists())
        self.assertIn("this run has no pinned checkout of kit", said[0])

    def test_qk_disk_and_removing_the_folder_neither_follow_nor_delete_through_the_link(self):
        made = pins.make(self.project, {"kit": "KIT_ROOT"})
        pinned = self.pinned("kit", self.one)
        (pinned / "large").write_bytes(b"x" * (4 * 1024 * 1024))
        workers = self.home / "queuekeeper-workers/project"
        checkout = workers / "project"
        checkout.mkdir(parents=True)
        pins.place_links(checkout, {"kit": "kit"}, made, self.project)
        self.assertEqual((workers / "kit").resolve(), pinned.resolve())
        sizes = disk.Sizes()
        self.assertLess(sizes.of(workers), 1024 * 1024)
        sizes.prime(workers.parent, 2)
        self.assertLess(sizes.of(workers), 1024 * 1024)
        disk.last_activity(workers)
        shutil.rmtree(workers.parent)
        self.assertTrue((pinned / "large").is_file())
        self.assertEqual(git(pinned, "rev-parse", "HEAD"), self.one)

    def test_link_takes_one_folder_beside_the_checkout_as_the_schema_does(self):
        base = {"schemaVersion": 1, "name": "fixture"}
        settings = {"kit": {"variable": "KIT_ROOT", "link": "../kit"},
                    "both": {"variable": "BOTH_ROOT", "revision": self.one.upper(), "link": "../both/"},
                    "plain": "PLAIN_ROOT"}
        config.parse(json.dumps({**base, "siblings": settings}))
        self.assertEqual(config.siblings(settings), {"kit": "KIT_ROOT", "plain": "PLAIN_ROOT",
                                                     "both": {"variable": "BOTH_ROOT", "revision": self.one}})
        self.assertEqual(config.sibling_links({"siblings": settings}), {"kit": "kit", "both": "both"})
        schema = json.loads((ROOT / "schemas/queuekeeper.schema.json").read_text())
        entry = schema["properties"]["siblings"]["additionalProperties"]["oneOf"][1]
        pattern = entry["properties"]["link"]["pattern"]
        self.assertEqual(set(entry["properties"]), {"variable", "revision", "link"})
        for link in ("../kit", "../kit/", "../kit.d", "../_x"):
            self.assertRegex(link, pattern)
            config.parse(json.dumps({**base, "siblings": {"kit": {"variable": "KIT_ROOT", "link": link}}}))
        for link in ("/tmp/kit", "kit", "../../kit", "../kit/more", "..", "../", "../..", "../.", "./kit", 3, ""):
            with self.subTest(link=link):
                if isinstance(link, str): self.assertNotRegex(link, pattern)
                with self.assertRaisesRegex(ValueError, "siblings.kit.link"):
                    config.parse(json.dumps({**base, "siblings": {"kit": {"variable": "KIT_ROOT", "link": link}}}))
        with self.assertRaisesRegex(ValueError, "siblings.kit"):
            config.parse(json.dumps({**base, "siblings": {"kit": {"variable": "KIT_ROOT"}}}))


if __name__ == "__main__":
    unittest.main()
