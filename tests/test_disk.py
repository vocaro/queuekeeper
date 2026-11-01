import contextlib
from io import StringIO
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/python"))
from vocaro_engineering import cli, devices, disk, landing
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


class WorktreeTests(Fixture):
    def test_a_landed_worktree_is_a_candidate_and_an_unlanded_one_is_kept(self):
        landed = self.worktree("landed")
        unlanded = self.worktree("unlanded")
        commit(unlanded, "work", "not on main\n")
        items = self.items(self.host(), ["worktree"])
        self.assertEqual(items[str(landed)].status, disk.CANDIDATE, items[str(landed)].reason)
        self.assertEqual(items[str(landed)].command, ["git", "-C", str(self.main), "worktree", "remove", str(landed)])
        self.assertEqual(items[str(unlanded)].status, disk.KEPT)
        self.assertIn("unlanded: 1 commits", items[str(unlanded)].reason)
        self.assertIsNone(items[str(unlanded)].command)

    def test_work_landed_by_an_equal_patch_counts_and_a_different_patch_does_not(self):
        picked = self.worktree("picked", branch="picked")  # its branch keeps the commits
        work = commit(picked, "feature", "the feature\n")
        changed = self.worktree("changed")
        commit(changed, "feature", "another version of the feature\n")
        commit(self.main, "other", "other work first, so the picked commit differs\n")
        git(self.main, "cherry-pick", work)
        self.publish()
        items = self.items(self.host(), ["worktree"])
        self.assertEqual(items[str(picked)].status, disk.CANDIDATE, items[str(picked)].reason)
        self.assertIn("equal patch", items[str(picked)].reason)
        self.assertEqual(items[str(changed)].status, disk.KEPT)

    def test_a_squash_counts_as_closeout_counts_it_and_one_main_edited_over_does_not(self):
        squashed = self.worktree("squashed", branch="squashed")
        commit(squashed, "feature", "the feature\n")
        commit(squashed, "notes", "notes\n")
        edited = self.worktree("edited")
        commit(edited, "shared", "edited version\n")
        commit(edited, "more", "more\n")
        for path in (squashed, edited):
            git(self.main, "merge", "-q", "--squash", git(path, "rev-parse", "HEAD"))
            git(self.main, "commit", "-q", "-m", f"Squash {path.name}")
        commit(self.main, "shared", "main edited over the squash\n")
        self.publish()
        items = self.items(self.host(), ["worktree"])
        self.assertEqual(items[str(squashed)].status, disk.CANDIDATE, items[str(squashed)].reason)
        self.assertIn("would change nothing", items[str(squashed)].reason)
        self.assertEqual(items[str(edited)].status, disk.KEPT)
        self.assertIn("unlanded: 2 commits", items[str(edited)].reason)

    def test_a_detached_worktree_main_reaches_only_by_equal_patch_is_kept_and_one_on_a_branch_is_not(self):
        # Removing the detached one would leave its commits to git gc, and only the network can say
        # whether an open issue cites them; the branch keeps the other's.
        detached = self.worktree("detached")
        on_branch = self.worktree("on-branch", branch="on-branch")
        picks = [commit(path, f"feature-{path.name}", "the feature\n") for path in (detached, on_branch)]
        commit(self.main, "other", "other work first, so each picked commit differs\n")
        git(self.main, "cherry-pick", *picks)
        self.publish()
        items = self.items(self.host(), ["worktree"])
        self.assertEqual(items[str(detached)].status, disk.KEPT, items[str(detached)].reason)
        self.assertIn("its detached HEAD is the last ref to commits origin/main does not reach",
                      items[str(detached)].reason)
        self.assertIsNone(items[str(detached)].command)
        self.assertEqual(items[str(on_branch)].status, disk.CANDIDATE, items[str(on_branch)].reason)

    def test_eng_results_under_build_keep_a_worktree_and_swiftpm_output_does_not(self):
        commit(self.main, ".gitignore", ".build/\n")
        self.publish()
        swiftpm = self.worktree("swiftpm")
        for name in ("debug/Module.build/output.o", "checkouts/Dependency/Package.swift", "workspace-state.json"):
            (swiftpm / ".build" / name).parent.mkdir(parents=True, exist_ok=True)
            (swiftpm / ".build" / name).write_text("rebuilt by SwiftPM\n")
        results = self.worktree("results")
        (results / ".build/debug").mkdir(parents=True)
        (results / ".build/engineering/suites/lanes").mkdir(parents=True)
        (results / ".build/engineering/suites/lanes/run.log").write_text("Ran 12 tests\n")
        emptied = self.worktree("emptied")
        (emptied / ".build/engineering").mkdir(parents=True)  # no result in it
        items = self.items(self.host(), ["worktree"])
        self.assertEqual(items[str(swiftpm)].status, disk.CANDIDATE, items[str(swiftpm)].reason)
        self.assertEqual(items[str(emptied)].status, disk.CANDIDATE, items[str(emptied)].reason)
        self.assertEqual(items[str(results)].status, disk.KEPT)
        self.assertEqual(items[str(results)].reason, "ignored files that may be evidence: .build/engineering/")

    def test_the_inventory_and_landed_sh_share_one_containment_proof(self):
        self.assertIs(disk.landing, landing)
        script = (Path(__file__).resolve().parents[1] / "skills/closeout/scripts/landed.sh").read_text()
        # landed.sh's own proof is landing.py: no shell copy of the patch or merge comparison is left.
        self.assertIn('contained() {\n    python3 -I -B "$landing" contained "$1" "$2"\n}', script)
        self.assertNotIn('git cherry "$1"', script)
        self.assertNotIn('merge-tree --write-tree "$1"', script)

    def test_a_live_process_keeps_a_landed_worktree(self):
        paths = {how: self.worktree(how) for how in ("cwd", "file", "argument", "idle")}
        processes = [disk.Process(101, "swift-build", cwd=str(paths["cwd"] / "Sources")),
                     disk.Process(102, "sourcekit-lsp", paths=(str(paths["file"] / ".build/index.db"),)),
                     disk.Process(103, "xcodebuild", args=f"xcodebuild -derivedDataPath={paths['argument']}/dd")]
        items = self.items(self.host(processes), ["worktree"])
        for how, pid in (("cwd", 101), ("file", 102), ("argument", 103)):
            self.assertEqual(items[str(paths[how])].status, disk.KEPT, how)
            self.assertIn(f"PID {pid}", items[str(paths[how])].reason)
        self.assertEqual(items[str(paths["idle"])].status, disk.CANDIDATE)

    def test_dirty_locked_recent_and_possible_evidence_keep_a_landed_worktree(self):
        dirty = self.worktree("dirty")
        (dirty / "notes.txt").write_text("untracked work\n")
        locked = self.worktree("locked")
        git(self.main, "worktree", "lock", "--reason", "claude agent", str(locked))
        commit(self.main, ".gitignore", "measurements/raw/\n__pycache__/\n")
        self.publish()
        evidence = self.worktree("evidence")
        (evidence / "measurements/raw").mkdir(parents=True)
        (evidence / "measurements/raw/run.json").write_text("{}\n")
        cache = self.worktree("cache")
        (cache / "measurements/__pycache__").mkdir(parents=True)
        (cache / "measurements/__pycache__/x.pyc").write_text("")
        items = self.items(self.host(), ["worktree"])
        self.assertIn("dirty: 1", items[str(dirty)].reason)
        self.assertIn("locked: claude agent", items[str(locked)].reason)
        self.assertIn("may be evidence: measurements/", items[str(evidence)].reason)
        self.assertEqual(items[str(cache)].status, disk.CANDIDATE, items[str(cache)].reason)
        for path in (dirty, locked, evidence): self.assertEqual(items[str(path)].status, disk.KEPT)
        recent = self.items(self.host(now=time.time()), ["worktree"])
        self.assertEqual(recent[str(cache)].status, disk.KEPT)
        self.assertIn("changed", recent[str(cache)].reason)

    def test_a_worktree_outside_the_session_directories_is_only_an_owner_decision(self):
        shared = self.worktree("Project", parent=self.root / "share")
        session = self.worktree("session", parent=self.home / ".codex/worktrees/abcd")
        items = self.items(self.host(), ["worktree"])
        self.assertEqual(items[str(shared)].status, disk.OWNER)
        self.assertIn("persistent or shared checkout", items[str(shared)].reason)
        self.assertEqual(items[str(session)].status, disk.CANDIDATE)

    def test_an_unregistered_directory_is_never_a_candidate(self):
        orphan = self.main / ".claude/worktrees/orphan"
        orphan.mkdir(parents=True)
        (orphan / ".git").write_text(f"gitdir: {self.main}/.git/worktrees/orphan\n")
        used = self.main / ".claude/worktrees/used"
        used.mkdir()
        items = self.items(self.host([disk.Process(7, "zsh", cwd=str(used))]), ["worktree"])
        self.assertEqual(items[str(orphan)].status, disk.OWNER)
        self.assertEqual(items[str(orphan)].command, ["rm", "-rf", "--", str(orphan)])
        self.assertEqual(items[str(used)].status, disk.KEPT)
        self.assertNotIn(disk.CANDIDATE, {item.status for item in items.values()})


def device(name, state="Shutdown", used="2026-01-01T00:00:00Z", **extra):
    return {"udid": str(uuid.uuid4()).upper(), "name": name, "state": state, "isAvailable": True,
            "lastUsedAt": used, **extra}


class SimulatorTests(Fixture):
    def simulators(self, devices, **values):
        listing = {"devices": {"com.apple.CoreSimulator.SimRuntime.iOS-27-0": devices}}
        items = disk.inventory(self.host(simulators=listing, **values), disk.Sizes(measure=False), ["simulator"])
        return {item.path.split(" ")[0]: item for item in items}

    def test_an_abandoned_disposable_device_is_a_candidate_and_a_pool_device_never_is(self):
        disposable = device(f"Project-iPhone-{uuid.uuid4()}")
        pool = device("Project Duo 1")
        unavailable_pool = device("Project Duo 2", isAvailable=False, availabilityError="runtime missing")
        default = device("iPhone 17 Pro")
        items = self.simulators([disposable, pool, unavailable_pool, default],
                                default_devices=frozenset({default["udid"]}))
        self.assertEqual(items[disposable["udid"]].status, disk.CANDIDATE, items[disposable["udid"]].reason)
        self.assertEqual(items[disposable["udid"]].command, ["xcrun", "simctl", "delete", disposable["udid"]])
        self.assertEqual(items[pool["udid"]].status, disk.KEPT)
        self.assertIsNone(items[pool["udid"]].command)
        self.assertEqual(items[unavailable_pool["udid"]].status, disk.OWNER)
        self.assertEqual(items[default["udid"]].status, disk.OWNER)

    def test_a_pool_member_is_a_candidate_only_under_the_pruning_rule(self):
        stamp = lambda days: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(LATER - days * 86400))
        lowest, stale, recent, released, booted = (device("Project Duo 1", used=stamp(40)),
            device("Project Duo 2", used=stamp(20)), device("Project Duo 3", used=stamp(3)),
            device("Project Duo 4", used=stamp(20)), device("Project Duo 5", state="Booted", used=stamp(20)))
        other = device("Other Duo 2", used=stamp(40))
        record = json.dumps({"devices": {"Project Duo 4": {"device": released["udid"], "state": "released",
                                                           "releasedAt": stamp(2)}}})
        items = self.simulators([lowest, stale, recent, released, booted, other], pools=frozenset({"Project"}),
                                records={"/record.json": record})
        self.assertEqual(items[stale["udid"]].status, disk.CANDIDATE, items[stale["udid"]].reason)
        self.assertEqual(items[stale["udid"]].command[-6:],
                         ["simulator-pool", "--pool", "Project", "--owner", "eng disk", "prune", stale["udid"]][-6:])
        self.assertIn("eligible for pruning", items[stale["udid"]].reason)
        for member, reason in ((lowest, "lowest-numbered"), (recent, "within 14 days"), (released, "was released"),
                               (booted, "Booted")):
            self.assertEqual(items[member["udid"]].status, disk.KEPT, reason)
            self.assertIn(reason, items[member["udid"]].reason)
        # A device in no configured pool is the owner's, however old.
        self.assertEqual(items[other["udid"]].status, disk.KEPT)
        self.assertIn("in no project's pool", items[other["udid"]].reason)

    def test_the_pools_are_those_the_main_checkouts_name(self):
        (self.main / "engineering.json").write_text(json.dumps({"simulatorPool": "Project"}))
        (self.development / "Unconfigured").mkdir()
        self.assertEqual(disk.pools(self.development), frozenset({"Project"}))
        # A main checkout that lags origin/main still names the pool origin/main names.
        lagging = self.development / "Lagging"
        lagging.mkdir()
        git(lagging, "init", "-q", "-b", "main")
        commit(lagging, "engineering.json", json.dumps({"simulatorPool": "Lagging"}))
        git(lagging, "update-ref", "refs/remotes/origin/main", "HEAD")
        git(lagging, "rm", "-q", "engineering.json")
        git(lagging, "commit", "-q", "-m", "older")
        self.assertEqual(disk.pools(self.development), frozenset({"Project", "Lagging"}))

    def test_booted_recent_reserved_or_referenced_disposable_devices_are_kept(self):
        booted = device(f"P-{uuid.uuid4()}", state="Booted")
        recent = device(f"P-{uuid.uuid4()}", used=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(LATER - 600)))
        by_udid = device(f"P-{uuid.uuid4()}")
        by_name = device(f"P-{uuid.uuid4()}")
        argument = device(f"P-{uuid.uuid4()}")
        record = json.dumps({"devices": {"a": {"device": by_udid["udid"].lower()}, "b": {"device": by_name["name"]}}})
        process = disk.Process(9, "xcodebuild", args=f"xcodebuild test -destination platform=iOS Simulator,id={argument['udid']}")
        items = self.simulators([booted, recent, by_udid, by_name, argument], records={"/record.json": record},
                                processes=[process])
        expected = {booted["udid"]: "Booted", recent["udid"]: "last used 10 min ago",
                    by_udid["udid"]: "device record /record.json", by_name["udid"]: "device record /record.json",
                    argument["udid"]: "arguments of PID 9"}
        for udid, reason in expected.items():
            self.assertEqual(items[udid].status, disk.KEPT, reason)
            self.assertIn(reason, items[udid].reason)

    def test_a_disposable_device_named_only_in_the_record_at_the_override_is_kept(self):
        reserved, unnamed = device(f"P-{uuid.uuid4()}"), device(f"P-{uuid.uuid4()}")
        record = self.root / "isolated-record.json"
        record.write_text(json.dumps({"devices": {"a": {"device": reserved["udid"]}}}))
        with patch.dict(os.environ, {"VOCARO_DEVICE_RECORD": str(record)}):
            items = self.simulators([reserved, unnamed], records=disk.device_records())
        self.assertEqual(items[reserved["udid"]].status, disk.KEPT)
        self.assertIn(f"named in the device record {record}", items[reserved["udid"]].reason)
        self.assertEqual(items[unnamed["udid"]].status, disk.CANDIDATE, items[unnamed["udid"]].reason)

    def test_device_records_follow_the_devices_module_and_keep_the_legacy_records(self):
        # A record moved by the devices module, as on a host without ~/Library, and a legacy one.
        moved, legacy = self.root / "moved/device-handoff.json", self.root / "old-device-handoff.json"
        moved.parent.mkdir()
        for path in [moved, legacy]: path.write_text("{}")
        environment = {key: value for key, value in os.environ.items() if key != "VOCARO_DEVICE_RECORD"}
        with patch.dict(os.environ, environment, clear=True), patch.object(devices, "default_path", return_value=moved), \
             patch.object(devices, "LEGACY", str(self.root / "*device-handoff*.json")):
            records = disk.device_records()
        self.assertEqual(set(records) & {str(moved), str(legacy)}, {str(moved), str(legacy)})

    def test_an_unreadable_device_record_keeps_every_disposable_device(self):
        disposable = device(f"P-{uuid.uuid4()}")
        items = self.simulators([disposable], records={"/record.json": None})
        self.assertEqual(items[disposable["udid"]].status, disk.KEPT)
        self.assertIn("could not be read", items[disposable["udid"]].reason)

    def test_a_runtime_without_devices_is_an_owner_decision(self):
        listing = {"devices": {"com.apple.CoreSimulator.SimRuntime.iOS-27-0": [device("iPhone 17")]}}
        images = {"A": {"identifier": "A", "runtimeIdentifier": "com.apple.CoreSimulator.SimRuntime.iOS-27-0", "sizeBytes": 8},
                  "B": {"identifier": "B", "runtimeIdentifier": "com.apple.CoreSimulator.SimRuntime.iOS-26-5", "sizeBytes": 9}}
        items = {item.path[0]: item for item in disk.inventory(self.host(simulators=listing, runtimes=images),
                                                                disk.Sizes(measure=False), ["runtime"])}
        self.assertEqual(items["A"].status, disk.KEPT)
        self.assertEqual(items["B"].status, disk.OWNER)
        self.assertEqual(items["B"].command, ["xcrun", "simctl", "runtime", "delete", "B"])


class CacheTests(Fixture):
    def derived(self, name, workspace=None):
        directory = self.home / "Library/Developer/Xcode/DerivedData" / name
        (directory / "Build").mkdir(parents=True)
        if workspace:
            with open(directory / "info.plist", "wb") as handle: plistlib.dump({"WorkspacePath": str(workspace)}, handle)
        return directory

    def test_derived_data_is_a_candidate_only_when_its_workspace_is_gone_and_nothing_uses_it(self):
        present = self.derived("Present-" + "a" * 28, self.main)
        gone = self.derived("Gone-" + "b" * 28, self.main / ".claude/worktrees/removed/Project.xcodeproj")
        used = self.derived("Used-" + "c" * 28, self.main / ".claude/worktrees/removed/Other.xcodeproj")
        unknown = self.derived("Unknown-" + "d" * 28)
        shared = self.derived("ModuleCache.noindex")
        explicit = self.derived("SDKExplicitPrecompiledModules")
        session = self.derived("Session-" + "e" * 28, self.root / "tmp/claude-501/session/App.xcodeproj")
        process = disk.Process(5, "XCBBuildService", paths=(str(used / "Build/Products/x.o"),))
        items = self.items(self.host([process], temp_roots=[(self.root / "tmp", 6)]), ["derived-data"])
        self.assertEqual(items[str(session)].status, disk.CANDIDATE)
        self.assertEqual(items[str(gone)].status, disk.CANDIDATE)
        self.assertEqual(items[str(gone)].command, ["rm", "-rf", "--", str(gone)])
        self.assertEqual(items[str(present)].status, disk.KEPT)
        self.assertEqual(items[str(used)].status, disk.KEPT)
        self.assertEqual(items[str(unknown)].status, disk.OWNER)
        self.assertIn("holds Build", items[str(unknown)].reason)
        self.assertEqual(items[str(shared)].status, disk.KEPT)
        self.assertEqual(items[str(explicit)].status, disk.KEPT)

    def test_derived_data_of_a_workspace_outside_the_projects_is_not_listed(self):
        mine = self.derived("Mine-" + "a" * 28, self.root / "Desktop/Personal.xcodeproj")
        gone = self.derived("Gone-" + "b" * 28, self.root / "removed/Personal.xcodeproj")
        project = self.derived("Project-" + "c" * 28, self.development / "Removed/Project.xcodeproj")
        items = self.items(self.host(temp_roots=[(self.root / "tmp", 6)]), ["derived-data"])
        self.assertNotIn(str(mine), items)
        self.assertNotIn(str(gone), items)
        self.assertEqual(items[str(project)].status, disk.CANDIDATE)

    def test_scratch_outside_checkouts_is_a_candidate_and_results_are_owner_decisions(self):
        temp = self.root / "tmp"
        scratch = temp / "session/scratchpad/build"
        scratch.mkdir(parents=True)
        (scratch / "workspace-state.json").write_text("{}")
        checkout = temp / "share/Project"
        (checkout / ".build").mkdir(parents=True)
        (checkout / ".git").write_text("gitdir: elsewhere\n")
        (checkout / ".build/workspace-state.json").write_text("{}")
        used = temp / "used"
        (used / "Build").mkdir(parents=True)
        (used / "Logs").mkdir()
        bundle = temp / "session/scratchpad/run.xcresult"
        bundle.mkdir()
        live, finished = temp / "checks.live", temp / "checks.done"
        started = subprocess.run(["ps", "-o", "lstart=", "-p", str(os.getpid())], capture_output=True, text=True).stdout
        for directory, pid in ((live, os.getpid()), (finished, 999999)):
            directory.mkdir()
            (directory / "owner").write_text(f"{pid} {started.strip()}\n")
        sessions = temp / "claude-501"
        open_checkout = self.worktree("live")
        kept = sessions / disk.encoded(open_checkout) / "session/scratchpad/dd"
        left = sessions / disk.encoded(self.main / ".claude/worktrees/removed") / "session/scratchpad/dd"
        removed = self.main / ".claude/worktrees/still-running"
        running = sessions / disk.encoded(removed) / "session/scratchpad/dd"
        for directory in (kept, left, running):
            directory.mkdir(parents=True)
            (directory / "workspace-state.json").write_text("{}")
        processes = [disk.Process(3, "swift-build", cwd=str(used)), disk.Process(4, "claude", cwd=str(removed / "Output"))]
        host = self.host(processes, temp_roots=[(temp, 6)], session_scratch=sessions)
        items = self.items(host, ["scratch", "results"])
        self.assertEqual(items[str(kept)].status, disk.KEPT)
        self.assertIn(f"whose checkout {open_checkout} still exists", items[str(kept)].reason)
        self.assertEqual(items[str(left)].status, disk.CANDIDATE)
        self.assertEqual(items[str(running)].status, disk.KEPT)
        self.assertIn("PID 4 (claude) still works in its checkout", items[str(running)].reason)
        self.assertEqual(items[str(scratch)].status, disk.CANDIDATE, items[str(scratch)].reason)
        self.assertNotIn(str(checkout / ".build"), items)
        self.assertEqual(items[str(used)].status, disk.KEPT)
        self.assertEqual(items[str(bundle)].status, disk.OWNER)
        self.assertEqual(items[str(live)].status, disk.KEPT)
        self.assertIn("still in progress", items[str(live)].reason)
        self.assertEqual(items[str(finished)].status, disk.OWNER)


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

    def test_deletion_needs_a_terminal(self):
        with self.assertRaises(RuntimeError):
            disk.delete(self.collect(), lambda category: self.collect(categories=[category]), self.ledger,
                        ask=lambda prompt: "y", terminal=False)
        self.assertTrue(self.landed.exists() and self.scratch.exists())

    def test_only_confirmed_candidates_are_removed_and_each_decision_is_recorded(self):
        answers = {"linked worktrees": "y", "scratch builds outside checkouts": "n"}
        ask = lambda prompt: next(answer for title, answer in answers.items() if title in prompt)
        with contextlib.redirect_stdout(StringIO()):
            removed = disk.delete(self.collect(), lambda category: self.collect(categories=[category]), self.ledger,
                                  ask=ask, out=print, terminal=True)
        self.assertEqual(removed, 1)
        self.assertFalse(self.landed.exists())
        self.assertTrue(self.unlanded.exists())
        self.assertTrue(self.scratch.exists())
        records = [json.loads(line) for line in self.ledger.read_text().splitlines()]
        self.assertEqual([(record["path"], record["outcome"]) for record in records], [(str(self.landed), "removed")])

    def test_a_candidate_is_checked_again_before_removal(self):
        items = self.collect()
        process = disk.Process(77, "swift-build", cwd=str(self.scratch))
        with contextlib.redirect_stdout(StringIO()):
            disk.delete(items, lambda category: self.collect([process], [category]), self.ledger,
                        ask=lambda prompt: "y" if "scratch" in prompt else "n", terminal=True)
        self.assertTrue(self.scratch.exists())
        self.assertIn("skipped: in use: PID 77", self.ledger.read_text())

    def test_a_removal_inside_a_working_tree_is_refused(self):
        inside = self.main / "build"
        inside.mkdir()
        item = disk.Item("scratch", str(inside), disk.CANDIDATE, "test", command=["rm", "-rf", "--", str(inside)])
        with contextlib.redirect_stdout(StringIO()):
            disk.delete([item], lambda category: [item], self.ledger, ask=lambda prompt: "y", terminal=True)
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
            disk.delete(collect(["simulator"]), lambda category: collect([category]), self.ledger,
                        ask=lambda prompt: "y", run=run, terminal=True)
        self.assertEqual(calls, [["xcrun", "simctl", "delete", disposable["udid"]]])


class EvidenceArchiveTests(Fixture):
    def test_durable_evidence_is_listed_for_offload_and_never_a_candidate(self):
        measurements = self.development / "project-measurements"
        session = self.development / "session-evidence"
        kept = self.main / ".build/session-evidence"
        for folder in (measurements, session, kept):
            folder.mkdir(parents=True)
        (self.development / "notes").mkdir()
        private = self.development / "apple-engineering-evidence"
        private.mkdir()
        git(private, "init", "-q")
        items = self.items(self.host(offload_host="build-host"), ["evidence"])
        self.assertEqual(set(items), {str(measurements), str(session), str(kept)})
        for item in items.values():
            self.assertEqual(item.status, disk.OWNER)
            self.assertIn("build-host", item.reason)
        self.assertEqual(items[str(measurements)].command,
                         ["python3", "scripts/eng", "evidence", "offload", str(measurements)])
        self.assertEqual(items[str(kept)].command[-2:], ["--to", "Development/session-evidence/Project"])

    def test_a_folder_waiting_for_the_second_mac_says_so_wherever_it_is(self):
        from vocaro_engineering import evidence_offload
        measurements = self.development / "project-measurements"
        elsewhere = self.home / "results/run-7"
        failed = self.development / "other-evidence"
        for folder in (measurements, elsewhere, failed): folder.mkdir(parents=True)
        queue = evidence_offload.Queue(self.home)
        queue.add(measurements, None, False, "second cannot be reached")
        queue.add(elsewhere, "Development/kept", True, "second cannot be reached")
        queue.add(failed, None, False, "second cannot be reached")
        queue.fail(failed, "FAILED: 1 files did not verify")
        queue.add(self.home / "gone", None, False, "second cannot be reached")
        items = self.items(self.host(offload_host="build-host"), ["evidence"])
        self.assertEqual(set(items), {str(measurements), str(elsewhere), str(failed)})
        self.assertIn("found the second Mac unreachable", items[str(measurements)].reason)
        self.assertIn("eng retries it", items[str(elsewhere)].reason)
        self.assertEqual(items[str(elsewhere)].command,
                         ["python3", "scripts/eng", "evidence", "offload", str(elsewhere), "--to", "Development/kept",
                          "--keep"])
        self.assertIn("waits for a person", items[str(failed)].reason)
        self.assertIn("FAILED", items[str(failed)].reason)
        for item in items.values(): self.assertEqual(item.status, disk.OWNER)

    def test_nothing_is_listed_without_a_second_mac(self):
        (self.development / "project-measurements").mkdir()
        self.assertEqual(self.items(self.host(), ["evidence"]), {})


class CommandTests(Fixture):
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


if __name__ == "__main__":
    unittest.main()
