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
from queuekeeper import config, disk, lane_results, offload_host, pins
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
            with self.subTest(siblings=wrong), self.assertRaisesRegex(ValueError, "siblings is {FOLDER: VARIABLE}"):
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


if __name__ == "__main__":
    unittest.main()
