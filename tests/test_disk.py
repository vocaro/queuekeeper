import contextlib
from io import StringIO
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/python"))
from queuekeeper import cli, disk
import host_isolation

LATER = time.time() + 10 * 86400  # every fixture looks idle from here

def git(cwd, *arguments):
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="Test",
                       GIT_AUTHOR_EMAIL="test@example.com", GIT_COMMITTER_NAME="Test",
                       GIT_COMMITTER_EMAIL="test@example.com")
    return subprocess.run(["git", "-C", str(cwd), *arguments], check=True, capture_output=True, text=True,
                          env=environment).stdout.strip()


def commit(cwd, name, text):
    (Path(cwd) / name).write_text(text)
    git(cwd, "add", name)
    git(cwd, "commit", "-q", "-m", name)
    return git(cwd, "rev-parse", "HEAD")


class Fixture(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.root)])
        self.home = self.root / "home"
        self.development = self.home / "Development"
        self.main = self.development / "Project"
        self.main.mkdir(parents=True)
        git(self.main, "init", "-q", "-b", "main")
        commit(self.main, "README", "first\n")
        self.publish()

    def publish(self):
        """Point origin/main at the main checkout's HEAD, as a fetch would."""
        git(self.main, "update-ref", "refs/remotes/origin/main", "HEAD")

    def worktree(self, name, parent=None, branch=None):
        path = (parent or self.main / ".claude/worktrees") / name
        git(self.main, "worktree", "add", "-q", *(["-b", branch] if branch else ["--detach"]), str(path), "origin/main")
        return path

    def host(self, processes=(), now=LATER, **values):
        return disk.Host(home=self.home, development=self.development, now=now, min_idle=86400,
                         processes=list(processes), **values)

    def items(self, host, categories=None):
        return {item.path: item for item in disk.inventory(host, disk.Sizes(measure=False), categories)}


class DeletionTests(Fixture):
    def setUp(self):
        super().setUp()
        self.landed = self.worktree("landed")
        self.unlanded = self.worktree("unlanded")
        commit(self.unlanded, "work", "unlanded\n")
        self.scratch = self.root / "tmp/scratch"
        self.scratch.mkdir(parents=True)
        (self.scratch / "workspace-state.json").write_text("{}")
        self.ledger = self.root / "ledger.jsonl"

    def collect(self, processes=(), categories=None):
        host = self.host(processes, temp_roots=[(self.root / "tmp", 3)])
        return disk.inventory(host, disk.Sizes(measure=False), categories)

    def test_every_candidate_category_is_removed_without_asking_and_no_owner_item(self):
        # The owner's decision of 2026-11-05: a candidate is proven unowned, so --delete asks nobody, on a
        # terminal or not; an owner item's command never runs.
        stale = disk.Item("simulator", "U Example", disk.CANDIDATE, "test", command=["xcrun", "simctl", "delete", "U"])
        runtime = disk.Item("runtime", "R", disk.CANDIDATE, "test", command=["xcrun", "simctl", "runtime", "delete", "R"])
        owner = disk.Item("evidence", "/nowhere/x-evidence", disk.OWNER, "test", command=["eng", "evidence", "offload"])
        items = [*self.collect(), stale, runtime, owner]
        calls = []
        def run(command, **options):
            calls.append(command)
            if command[0] == "xcrun": return subprocess.CompletedProcess(command, 0, "", "")
            return subprocess.run(command, **options)
        def fresh(category):
            return [item for item in items if item.category == category] if category in ("simulator", "runtime") \
                else self.collect(categories=[category])
        with patch("builtins.input", side_effect=AssertionError("asked")), contextlib.redirect_stdout(StringIO()) as output:
            removed = disk.delete(items, fresh, self.ledger, out=print, run=run)
        self.assertEqual(removed, 4)
        self.assertFalse(self.landed.exists() or self.scratch.exists())
        self.assertTrue(self.unlanded.exists())
        self.assertIn(stale.command, calls)
        self.assertIn(runtime.command, calls)
        self.assertNotIn(owner.command, calls)  # an owner item's command never runs
        self.assertIn("Removing 1 linked worktrees candidates, ", output.getvalue())
        self.assertNotIn("Kept every", output.getvalue())
        records = {record["path"]: record for record in map(json.loads, self.ledger.read_text().splitlines())}
        self.assertEqual(set(records), {str(self.landed), str(self.scratch), stale.path, runtime.path})
        self.assertTrue(all("approval" not in record for record in records.values()))  # nobody asked: none to name

    def test_yes_names_who_asked_on_every_entry(self):
        stale = disk.Item("simulator", "U Example", disk.CANDIDATE, "test", command=["xcrun", "simctl", "delete", "U"])
        items = [*self.collect(), stale]
        def run(command, **options):
            if command[0] == "xcrun": return subprocess.CompletedProcess(command, 0, "", "")
            return subprocess.run(command, **options)
        def fresh(category):
            return [item for item in items if item.category == category] if category == "simulator" \
                else self.collect(categories=[category])
        with contextlib.redirect_stdout(StringIO()) as output:
            removed = disk.delete(items, fresh, self.ledger, out=print, run=run, approval="owner, 2026-11-05 chat")
        self.assertEqual(removed, 3)
        self.assertIn("on the approval --yes: owner, 2026-11-05 chat.", output.getvalue())
        records = {record["path"]: record for record in map(json.loads, self.ledger.read_text().splitlines())}
        for path in (str(self.landed), str(self.scratch), stale.path):
            self.assertEqual(records[path]["approval"], "--yes: owner, 2026-11-05 chat")

    def test_yes_still_checks_each_candidate_again_and_without_a_name_says_only_yes(self):
        items = self.collect()
        process = disk.Process(78, "swift-build", cwd=str(self.scratch))
        with contextlib.redirect_stdout(StringIO()):
            disk.delete(items, lambda category: self.collect([process], [category]), self.ledger, approval="")
        self.assertTrue(self.scratch.exists())
        self.assertFalse(self.landed.exists())
        records = {record["path"]: record for record in map(json.loads, self.ledger.read_text().splitlines())}
        self.assertTrue(records[str(self.scratch)]["outcome"].startswith("skipped: in use: PID 78"))
        self.assertEqual(records[str(self.landed)]["approval"], "--yes")

    def test_each_decision_is_recorded_with_its_outcome(self):
        with contextlib.redirect_stdout(StringIO()):
            removed = disk.delete(self.collect(), lambda category: self.collect(categories=[category]), self.ledger,
                                  out=print)
        self.assertEqual(removed, 2)
        self.assertFalse(self.landed.exists() or self.scratch.exists())
        self.assertTrue(self.unlanded.exists())
        records = [json.loads(line) for line in self.ledger.read_text().splitlines()]
        self.assertEqual(sorted((record["path"], record["outcome"]) for record in records),
                         sorted([(str(self.landed), "removed"), (str(self.scratch), "removed")]))

    def test_a_candidate_is_checked_again_before_removal(self):
        items = self.collect()
        process = disk.Process(77, "swift-build", cwd=str(self.scratch))
        with contextlib.redirect_stdout(StringIO()):
            disk.delete(items, lambda category: self.collect([process], [category]), self.ledger)
        self.assertTrue(self.scratch.exists())
        self.assertIn("skipped: in use: PID 77", self.ledger.read_text())

    def test_a_removal_inside_a_working_tree_is_refused(self):
        inside = self.main / "build"
        inside.mkdir()
        item = disk.Item("scratch", str(inside), disk.CANDIDATE, "test", command=["rm", "-rf", "--", str(inside)])
        with contextlib.redirect_stdout(StringIO()):
            disk.delete([item], lambda category: [item], self.ledger)
        self.assertTrue(inside.exists())
        self.assertIn("inside a repository's working tree", self.ledger.read_text())

    def test_simulator_deletion_names_the_exact_device(self):
        disposable, pool = device(f"P-{uuid.uuid4()}"), device("Project Duo 1")
        listing = {"devices": {"com.apple.CoreSimulator.SimRuntime.iOS-27-0": [disposable, pool]}}
        collect = lambda categories=None: disk.inventory(self.host(simulators=listing), disk.Sizes(measure=False), categories)
        calls = []
        def run(command, **options):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")
        with contextlib.redirect_stdout(StringIO()):
            disk.delete(collect(["simulator"]), lambda category: collect([category]), self.ledger, run=run)
        self.assertEqual(calls, [["xcrun", "simctl", "delete", disposable["udid"]]])


class KeptResultsTests(Fixture):
    """Lane results kept by commit beside the receipts in a repository's Git directory (#218)."""
    def setUp(self):
        super().setUp()
        from queuekeeper import remote
        self.remote, self.store, self.now = remote, self.main / ".git/vocaro-engineering/remote", time.time()

    def kept(self, commit, days, lane="gate"):
        folder = self.store / commit / f"{lane}.results"
        (folder / "suites").mkdir(parents=True)
        (folder / "suites/shard.log").write_text("passed after 0.7 seconds\n")
        receipt = self.store / commit / f"{lane}.json"
        receipt.write_text(json.dumps({"lane": lane, "commit": commit, "keptResults": folder.name}))
        os.utime(receipt, (self.now - days * 86400,) * 2)
        return folder

    def fill(self):
        newest = self.remote.KEPT_RESULTS_PER_LANE
        folders = [self.kept(f"c{index:02}", index * 3) for index in range(newest + 2)]
        return folders[:newest], folders[newest:]

    def test_results_past_the_rule_are_a_candidate_and_within_it_are_kept(self):
        young, old = self.fill()
        items = self.items(self.host(now=self.now), ["kept-results"])
        item = items[str(self.store)]
        self.assertEqual(item.status, disk.CANDIDATE, item.reason)
        self.assertIn(f"gate at {len(young) + len(old)} commits; {len(old)} past the rule", item.reason)
        self.assertIn(self.remote.KEPT_RESULTS_RULE, item.reason)
        self.assertEqual(item.command, ["python3", str(disk.TOOLKIT_ROOT / "bin/eng"), "remote", "prune-results",
                                        str(self.main)])
        # In use, it is kept.
        processes = [disk.Process(301, "grep", paths=(str(old[0] / "suites/shard.log"),))]
        self.assertEqual(self.items(self.host(processes, now=self.now), ["kept-results"])[str(self.store)].status,
                         disk.KEPT)
        # The negative control: within the rule, they are counted and kept, and the inventory removed nothing.
        for folder in old: shutil.rmtree(folder)
        item = self.items(self.host(now=self.now), ["kept-results"])[str(self.store)]
        self.assertEqual(item.status, disk.KEPT)
        self.assertIn("none past the rule", item.reason)
        self.assertTrue(all(folder.exists() for folder in young))
        # A repository with no kept results is not listed.
        shutil.rmtree(self.store)
        self.assertEqual(self.items(self.host(now=self.now), ["kept-results"]), {})

    def test_delete_prunes_by_the_rule_through_eng_remote_and_leaves_every_receipt(self):
        young, old = self.fill()
        host = self.host(now=self.now)
        [item] = disk.inventory(host, disk.Sizes(measure=False), ["kept-results"])
        # The dry run of the command it runs removes nothing.
        with contextlib.redirect_stdout(StringIO()) as said:
            self.assertEqual(cli.main(item.command[2:] + ["--dry-run"]), 0)
        self.assertIn(f"would prune {old[0]}", said.getvalue())
        self.assertTrue(all(folder.exists() for folder in young + old))
        def run(command, **_):
            with contextlib.redirect_stdout(StringIO()): status = cli.main(command[2:])
            return subprocess.CompletedProcess(command, status, "", "")
        with contextlib.redirect_stdout(StringIO()):
            removed = disk.delete([item], lambda category: disk.inventory(host, disk.Sizes(measure=False), [category]),
                                  self.root / "ledger.jsonl", out=lambda *_: None, run=run)
        self.assertEqual(removed, 1)
        self.assertEqual([folder.exists() for folder in young + old], [True] * len(young) + [False] * len(old))
        self.assertEqual(len(list(self.store.glob("*/gate.json"))), len(young) + len(old))
        entry = json.loads((self.root / "ledger.jsonl").read_text())
        self.assertEqual((entry["category"], entry["outcome"]), ("kept-results", "removed"))
        # Once within the rule, a further --delete finds nothing to do.
        self.assertEqual(self.items(self.host(now=self.now), ["kept-results"])[str(self.store)].status, disk.KEPT)


class CommandTests(Fixture):
    def test_archive_worktrees_dry_run_does_not_transfer_or_print_a_bypass_command(self):
        commit(self.main, ".gitignore", ".build/\n")
        self.publish()
        path = self.worktree("archive")
        (path / ".build").mkdir()
        (path / ".build/probe.log").write_text("evidence")
        output = StringIO()
        host = self.host(temp_roots=[], offload_host="second")
        with patch.object(disk.Host, "current", return_value=host), patch.object(disk.Sizes, "prime"), \
                patch.object(disk, "archive_worktrees") as archive, contextlib.redirect_stdout(output), \
                contextlib.redirect_stderr(StringIO()):
            self.assertEqual(cli.main(["--project", str(self.root), "disk", "--dry-run", "--archive-worktrees"]), 0)
        archive.assert_not_called()
        self.assertIn(f"{path} (verify archive before removal)", output.getvalue())
        self.assertNotIn(f"worktree remove {path}", output.getvalue())
        self.assertTrue(path.exists())

    def test_eng_disk_reports_and_changes_nothing(self):
        landed = self.worktree("landed")
        host = self.host(temp_roots=[])
        output = StringIO()
        with patch.object(disk.Host, "current", return_value=host), patch.object(disk.Sizes, "prime"), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(StringIO()):
            self.assertEqual(cli.main(["--project", str(self.root), "disk", "--min-size-mb", "0"]), 0)
        text = output.getvalue()
        self.assertIn("This inventory changed nothing", text)
        self.assertIn(f"$ git -C {self.main} worktree remove {landed}", text)
        self.assertTrue(landed.exists())

    def run_disk(self, *arguments):
        host = self.host(temp_roots=[])
        output = StringIO()
        with patch.object(disk.Host, "current", return_value=host), \
                patch.object(disk.Sizes, "prime", autospec=True) as prime, \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(StringIO()):
            self.assertEqual(cli.main(["--project", str(self.root), "disk", *arguments]), 0)
        return {(str(call.args[1]), call.args[2]) for call in prime.call_args_list}, output.getvalue()

    def test_eng_disk_reads_only_the_projects_locations(self):
        scanned, text = self.run_disk()
        self.assertEqual(scanned, {(str(self.home / "Library/Developer/Xcode/DerivedData"), 1),
                                   (str(self.home / "Library/Caches/org.swift.swiftpm"), 2)})
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(StringIO()):
            self.run_disk("--map")

    def test_a_dry_run_lists_only_what_delete_would_remove(self):
        landed, unlanded = self.worktree("landed"), self.worktree("unlanded")
        commit(unlanded, "work", "unlanded\n")
        outside = self.worktree("outside", parent=self.root / "elsewhere")
        _, text = self.run_disk("--dry-run")
        self.assertIn("Dry run: nothing was removed", text)
        self.assertIn(f"$ git -C {self.main} worktree remove {landed}", text)
        self.assertNotIn(str(unlanded), text)
        self.assertNotIn(str(outside), text)  # an owner decision --delete never runs
        self.assertIn("In all: 1 items", text)
        self.assertTrue(landed.exists())
        _, text = self.run_disk("--dry-run", "--json")
        self.assertEqual([item["path"] for item in json.loads(text)["items"]], [str(landed)])
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(StringIO()):
            self.run_disk("--dry-run", "--delete")

    def test_yes_reaches_delete_only_with_delete(self):
        def approval(*arguments):
            with patch.object(disk, "delete") as delete:
                self.run_disk(*arguments)
            return delete.call_args.kwargs["approval"] if delete.called else "not called"
        self.assertIsNone(approval("--delete"))
        self.assertEqual(approval("--delete", "--yes"), "")
        self.assertEqual(approval("--delete", "--yes", "owner, 2026-11-04 chat"), "owner, 2026-11-04 chat")
        error = StringIO()
        with patch.object(disk, "delete") as delete, contextlib.redirect_stderr(error):
            self.assertEqual(cli.main(["--project", str(self.root), "disk", "--yes"]), 2)
        self.assertFalse(delete.called)
        self.assertIn("--yes names who asked for --delete's removals", error.getvalue())


if __name__ == "__main__":
    unittest.main()
