import contextlib
from io import BytesIO, StringIO, TextIOWrapper
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

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src/python"))
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


SCRATCH = "Test scratch folders"


def scratch_items(root):
    """An extension's items for these tests: each folder under root/tmp, a candidate once nothing uses it."""
    def items(host, usage, sizes, wanted):
        if "scratch" not in wanted: return []
        return [disk.cache_item(host, usage, sizes, "scratch", path, "a test's scratch folder")
                for path in sorted((root / "tmp").iterdir())]
    return items


class Extended(Fixture):
    """The inventory with one extension's category, and none of the Apple extension's, which the command would add."""

    def setUp(self):
        super().setUp()
        self.scratch, self.other = self.root / "tmp/scratch", self.root / "tmp/other"
        for folder in (self.scratch, self.other): folder.mkdir(parents=True)
        self.ledger = self.root / "ledger.jsonl"
        listed = scratch_items(self.root)
        for patched in (patch.object(cli, "extensions_of", lambda *folders: []), patch.object(disk, "EXTENSION_HOSTS", []),
                        patch.object(disk, "EXTENSION_PRIMES", []),
                        patch.object(disk, "EXTENSION_ITEMS", [listed]),
                        patch.dict(disk.EXTENSION_CATEGORIES, {listed: ["scratch"]}),
                        patch.dict(disk.CATEGORIES, {"scratch": SCRATCH, "simulator": "Simulators",
                                                     "runtime": "Simulator runtimes"})):
            patched.start()
            self.addCleanup(patched.stop)

    def collect(self, processes=(), categories=None):
        return disk.inventory(self.host(processes), disk.Sizes(measure=False), categories)


class DeletionTests(Extended):
    def test_every_candidate_category_is_removed_without_asking_and_no_owner_item(self):
        # The owner's decision of 2026-11-05: a candidate is proven unowned, so --delete asks nobody, on a
        # terminal or not; an owner item's command never runs.
        stale = disk.Item("simulator", "U Example", disk.CANDIDATE, "test", command=["xcrun", "simctl", "delete", "U"])
        runtime = disk.Item("runtime", "R", disk.CANDIDATE, "test", command=["xcrun", "simctl", "runtime", "delete", "R"])
        owner = disk.Item("scratch", "/nowhere/kept", disk.OWNER, "test", command=["rm", "-rf", "--", "/nowhere/kept"])
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
        self.assertFalse(self.scratch.exists() or self.other.exists())
        self.assertIn(stale.command, calls)
        self.assertIn(runtime.command, calls)
        self.assertNotIn(owner.command, calls)  # an owner item's command never runs
        self.assertIn("Removing 2 test scratch folders candidates, ", output.getvalue())
        records = {record["path"]: record for record in map(json.loads, self.ledger.read_text().splitlines())}
        self.assertEqual(set(records), {str(self.scratch), str(self.other), stale.path, runtime.path})
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
        for path in (str(self.scratch), str(self.other), stale.path):
            self.assertEqual(records[path]["approval"], "--yes: owner, 2026-11-05 chat")

    def test_yes_still_checks_each_candidate_again_and_without_a_name_says_only_yes(self):
        items = self.collect()
        process = disk.Process(78, "swift-build", cwd=str(self.scratch))
        with contextlib.redirect_stdout(StringIO()):
            disk.delete(items, lambda category: self.collect([process], [category]), self.ledger, approval="")
        self.assertTrue(self.scratch.exists())
        self.assertFalse(self.other.exists())
        records = {record["path"]: record for record in map(json.loads, self.ledger.read_text().splitlines())}
        self.assertTrue(records[str(self.scratch)]["outcome"].startswith("skipped: in use: PID 78"))
        self.assertEqual(records[str(self.other)]["approval"], "--yes")

    def test_each_decision_is_recorded_with_its_outcome(self):
        with contextlib.redirect_stdout(StringIO()):
            removed = disk.delete(self.collect(), lambda category: self.collect(categories=[category]), self.ledger,
                                  out=print)
        self.assertEqual(removed, 2)
        self.assertFalse(self.scratch.exists() or self.other.exists())
        records = [json.loads(line) for line in self.ledger.read_text().splitlines()]
        self.assertEqual(sorted((record["path"], record["outcome"]) for record in records),
                         sorted([(str(self.scratch), "removed"), (str(self.other), "removed")]))

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


class KeptResultsTests(Fixture):
    """Lane results kept by commit beside the receipts in a repository's Git directory (#218)."""
    def setUp(self):
        super().setUp()
        from queuekeeper import remote
        self.remote, self.store, self.now = remote, self.main / ".git/queuekeeper/remote", time.time()

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
        self.assertEqual(item.command, ["python3", str(disk.TOOLKIT_ROOT / "bin/qk"), "remote", "prune-results",
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


class ProgressTests(Extended):
    """vocaro/queuekeeper#212: a run says on stderr which pass it is in and how far it has got, at most one line
    each interval besides the forced ones, and its stdout reaches a file line by line."""

    def test_progress_is_throttled_apart_from_forced_lines(self):
        clock, lines = [0.0], []
        progress = disk.Progress(lines.append, interval=10, clock=lambda: clock[0])
        for second in range(31):
            clock[0] = float(second)
            progress(f"tick {second}")
            if second == 15: progress("pass", force=True)
        # The first line, one 10 s after each line written, and the forced one, which restarts the interval.
        self.assertEqual(lines, ["tick 0", "tick 10", "pass", "tick 25"])

    def test_each_pass_and_item_checked_is_reported(self):
        lines = []
        sizes = disk.Sizes(measure=False, progress=lambda line, force=False: lines.append((line, force)))
        disk.inventory(self.host(), sizes)
        self.assertIn(("Inventorying scratch.", True), lines)
        self.assertIn((f"Inventorying scratch: 2 items checked, now {self.scratch}.", False), lines)

    def test_delete_says_which_removal_it_starts_before_it_runs(self):
        said = []
        disk.delete(self.collect(), lambda category: self.collect(categories=[category]), self.ledger,
                    out=said.append, progress=lambda line, force=False: said.append(line))
        self.assertEqual(said[1:6], ["Checking the scratch candidates again.",
                                     f"Removing 1 of 2 scratch candidates: {self.other}",
                                     f"removed: {self.other}",
                                     f"Removing 2 of 2 scratch candidates: {self.scratch}",
                                     f"removed: {self.scratch}"])

    def test_json_carries_no_progress_and_stdout_is_line_buffered(self):
        stdout, stderr = TextIOWrapper(BytesIO(), encoding="utf-8"), StringIO()
        with patch.object(disk.Host, "current", return_value=self.host()), \
                patch.object(disk, "EXTENSION_PRIMES", [lambda host: [(self.root / "tmp", 1)]]), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.assertEqual(cli.main(["--project", str(self.root), "disk", "--json"]), 0)
        self.assertTrue(stdout.line_buffering)
        stdout.seek(0)
        found = json.loads(stdout.read())
        self.assertEqual({item["path"] for item in found["items"]}, {str(self.scratch), str(self.other)})
        progress = stderr.getvalue().splitlines()
        self.assertEqual(progress[:3], ["Listing the account's processes with lsof and ps.",
                                        f"Measuring {self.root / 'tmp'} in one du pass, 1 of 1.",
                                        "Inventorying kept-results."])
        self.assertIn("Inventorying scratch.", progress)


class CommandTests(Extended):
    def run_disk(self, *arguments, processes=()):
        host = self.host(processes)
        output = StringIO()
        with patch.object(disk.Host, "current", return_value=host), \
                patch.object(disk.Sizes, "prime", autospec=True) as prime, \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(StringIO()):
            self.assertEqual(cli.main(["--project", str(self.root), "disk", *arguments]), 0)
        return {(str(call.args[1]), call.args[2]) for call in prime.call_args_list}, output.getvalue()

    def test_disk_reports_and_changes_nothing(self):
        _, text = self.run_disk("--min-size-mb", "0")
        self.assertIn("This inventory changed nothing", text)
        self.assertIn(f"$ rm -rf -- {self.scratch}", text)
        self.assertTrue(self.scratch.exists())

    def test_disk_measures_only_the_folders_an_extension_names(self):
        scanned, text = self.run_disk()
        self.assertEqual(scanned, set())
        with patch.object(disk, "EXTENSION_PRIMES", [lambda host: [(self.root / "tmp", 1), (None, 2)]]):
            scanned, text = self.run_disk()
        self.assertEqual(scanned, {(str(self.root / "tmp"), 1)})
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(StringIO()):
            self.run_disk("--map")

    def test_a_dry_run_lists_only_what_delete_would_remove(self):
        busy = [disk.Process(79, "swift-build", cwd=str(self.other))]
        _, text = self.run_disk("--dry-run", processes=busy)
        self.assertIn("Dry run: nothing was removed", text)
        self.assertIn(f"$ rm -rf -- {self.scratch}", text)
        self.assertNotIn(str(self.other), text)  # kept: a process works in it
        self.assertIn("In all: 1 items", text)
        self.assertTrue(self.scratch.exists())
        _, text = self.run_disk("--dry-run", "--json", processes=busy)
        self.assertEqual([item["path"] for item in json.loads(text)["items"]], [str(self.scratch)])
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
