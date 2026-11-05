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

    def test_eng_results_and_swiftpm_output_go_with_a_landed_worktree(self):
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
        self.assertEqual(items[str(results)].status, disk.CANDIDATE, items[str(results)].reason)
        self.assertIn("its lane results (.build/engineering/) go with it", items[str(results)].reason)
        self.assertEqual(items[str(results)].command, ["git", "-C", str(self.main), "worktree", "remove", str(results)])

    def test_a_landed_subagent_worktree_holding_swiftpm_6_state_and_lane_results_is_a_candidate(self):
        # vocaro-engineering#187: 44 such worktrees, 90 GiB, were kept for these files.
        commit(self.main, ".gitignore", ".build/\n")
        self.publish()
        agent = self.worktree("agent-a1b2c3")
        for name in (".buildSystem_debug/state.db", ".buildSystem_release", "CACHEDIR.TAG",
                     "engineering/suites/RESULTS", "engineering/suites/lanes/run.log"):
            (agent / ".build" / name).parent.mkdir(parents=True, exist_ok=True)
            (agent / ".build" / name).write_text("x\n")
        unknown = self.worktree("unknown")
        (unknown / ".build/notes.txt").parent.mkdir(parents=True)
        (unknown / ".build/notes.txt").write_text("not SwiftPM's\n")
        unlanded = self.worktree("unlanded")
        (unlanded / ".build/engineering").mkdir(parents=True)
        (unlanded / ".build/engineering/RESULTS").write_text("x\n")
        commit(unlanded, "work", "not on main\n")
        dirty = self.worktree("dirty")
        (dirty / ".build/engineering").mkdir(parents=True)
        (dirty / ".build/engineering/RESULTS").write_text("x\n")
        (dirty / "notes.txt").write_text("untracked work\n")
        used = self.worktree("used")
        (used / ".build/engineering").mkdir(parents=True)
        (used / ".build/engineering/RESULTS").write_text("x\n")
        items = self.items(self.host([disk.Process(105, "swift-build", cwd=str(used))]), ["worktree"])
        self.assertEqual(items[str(agent)].status, disk.CANDIDATE, items[str(agent)].reason)
        self.assertIn(".build/engineering/", items[str(agent)].reason)
        self.assertNotIn(".buildSystem", items[str(agent)].reason)
        self.assertEqual(items[str(unknown)].reason, "ignored files that may be evidence: .build/notes.txt")
        self.assertIn("unlanded: 1 commits", items[str(unlanded)].reason)
        self.assertIn("its lane results .build/engineering/ stay with it", items[str(unlanded)].reason)
        self.assertIn("dirty: 1", items[str(dirty)].reason)
        self.assertIn("PID 105", items[str(used)].reason)
        for path in (unknown, unlanded, dirty, used):
            self.assertEqual(items[str(path)].status, disk.KEPT, path.name)
            self.assertIsNone(items[str(path)].command)

    def test_a_live_desktop_session_keeps_a_landed_worktree_and_an_archived_one_does_not(self):
        live, archived, recent = self.worktree("live"), self.worktree("archived"), self.worktree("recent")
        records = self.home / disk.SESSION_RECORDS / "account/org"
        records.mkdir(parents=True)
        for name, path, archived_flag, when in (("local_1", live, False, 0), ("local_2", archived, True, 0),
                                                 ("local_3", recent, True, (time.time() + 2 * 86400 - 3600) * 1000)):
            (records / f"{name}.json").write_text(json.dumps({"sessionId": name, "cwd": str(path / "Sources"),
                                                               "title": name, "isArchived": archived_flag,
                                                               "lastActivityAt": when}))
        items = self.items(self.host(sessions=disk.desktop_sessions(self.home), now=time.time() + 2 * 86400),
                           ["worktree"])
        self.assertEqual(items[str(live)].status, disk.KEPT)
        self.assertIn("the folder of the desktop session local_1 (local_1), not archived", items[str(live)].reason)
        self.assertEqual(items[str(archived)].status, disk.CANDIDATE, items[str(archived)].reason)
        self.assertEqual(items[str(recent)].status, disk.KEPT)  # its session wrote within the idle window
        self.assertIn("changed", items[str(recent)].reason)
        (records / "local_4.json").write_text("{not json")
        items = self.items(self.host(sessions=disk.desktop_sessions(self.home), now=time.time() + 2 * 86400),
                           ["worktree"])
        self.assertEqual(items[str(archived)].status, disk.KEPT)
        self.assertIn("a desktop session record could not be read", items[str(archived)].reason)

    def test_a_cli_sessions_transcript_counts_as_activity_in_its_worktree(self):
        worktree = self.worktree("cli")
        transcripts = self.home / ".claude/projects" / disk.encoded(worktree)
        transcripts.mkdir(parents=True)
        (transcripts / "session.jsonl").write_text("{}\n")
        later = time.time() + 2 * 86400
        os.utime(transcripts / "session.jsonl", (later - 3600, later - 3600))
        item = self.items(self.host(now=later), ["worktree"])[str(worktree)]
        self.assertEqual(item.status, disk.KEPT)
        self.assertIn("changed 60 min ago", item.reason)

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

    def test_a_queue_checkout_the_queue_retired_is_an_owner_decision_while_nothing_uses_it(self):
        # vocaro-engineering#104: the worker made its checkout afresh in vocaro-queue/ and left the old one.
        retired = self.worktree("Project-queue", parent=self.development)
        live = self.worktree("Project", parent=self.development / "vocaro-queue/Project")
        used = self.worktree("Used-queue", parent=self.development)
        git(self.main, "worktree", "lock", "--reason", f"{disk.RETIRED}: replaced by {live}", str(retired))
        git(self.main, "worktree", "lock", "--reason", f"{disk.RETIRED}: replaced by elsewhere", str(used))
        git(self.main, "worktree", "lock", "--reason", "the merge queue's checkout", str(live))
        items = self.items(self.host([disk.Process(104, "xcodebuild", cwd=str(used))]), ["worktree"])
        self.assertEqual(items[str(retired)].status, disk.OWNER, items[str(retired)].reason)
        self.assertIn(f"replaced by {live}", items[str(retired)].reason)
        self.assertEqual(items[str(retired)].command,
                         ["git", "-C", str(self.main), "worktree", "remove", "--force", "--force", str(retired)])
        # The negative controls: the queue's current checkout, and a retired one a process still uses.
        self.assertEqual(items[str(live)].status, disk.KEPT)
        self.assertIn("locked: the merge queue's checkout", items[str(live)].reason)
        self.assertEqual(items[str(used)].status, disk.KEPT)
        self.assertIn("still in use: PID 104", items[str(used)].reason)

    def test_users_finds_what_a_process_holds_in_a_checkout_apart_from_nested_ones(self):
        checkout = self.root / "kit"
        nested = checkout / ".claude/worktrees/agent"
        found = disk.users([disk.Process(1, cwd=str(checkout / "src")), disk.Process(2, cwd=str(nested)),
                            disk.Process(3, paths=(str(checkout / ".build/log"),)),
                            disk.Process(4, args=f"python3 -c x {checkout}/src/python"),
                            disk.Process(5, args=f"python3 {checkout}-queue/scripts/eng"),
                            disk.Process(6, cwd=str(self.root))], checkout, apart=[nested])
        self.assertEqual([process.pid for process in found], [1, 3, 4])

    def test_runners_count_only_what_runs_from_below_a_checkout(self):
        """vocaro-engineering#182: a session in a worktree inside the checkout names the checkout itself as its
        project root, and its helpers start at that root; neither runs code from the tree, while a run from the
        root names a file below it, relative to its working directory."""
        checkout = self.root / "kit"
        nested = checkout / ".claude/worktrees/agent"
        (checkout / "scripts").mkdir(parents=True)
        (checkout / "scripts/eng").write_text("")
        (checkout / "skills/closeout").mkdir(parents=True)
        processes = [
            # The negative controls.
            disk.Process(1, cwd=str(nested), args=f"claude --project-config-root {checkout}"),
            disk.Process(2, cwd=str(checkout), args="node mcp-server --stdio"),
            disk.Process(3, paths=(str(checkout),)),
            disk.Process(4, cwd=str(self.root), args="python3 scripts/eng run lane"),
            disk.Process(5, cwd=str(nested), args="python3 scripts/eng run lane"),
            disk.Process(10, cwd=str(checkout), args="claude --setting-sources user,project --add-dir skills"),
            # The positive controls.
            disk.Process(6, cwd=str(checkout), args="python3 scripts/eng run lane"),
            disk.Process(7, cwd=str(checkout / "scripts"), args="sleep 300"),
            disk.Process(8, paths=(str(checkout / "skills/closeout/landed.sh"),)),
            disk.Process(9, cwd=str(self.root), args=f"python3 {checkout}/scripts/eng land"),
            disk.Process(11, cwd=str(checkout), args="sh -c ./run skills/closeout")]
        found = disk.runners(processes, checkout, apart=[nested])
        self.assertEqual([process.pid for process, _, _ in found], [6, 7, 8, 9, 11])
        self.assertEqual(found[0][1:], ("argument", str(checkout / "scripts/eng")))
        self.assertEqual(found[1][1], "working directory")
        self.assertEqual(found[2][1], "open file")
        # users, which keeps a checkout from being moved, still counts each use at its root.
        self.assertEqual([process.pid for process in disk.users(processes, checkout, apart=[nested])],
                         [1, 2, 3, 10, 6, 7, 8, 9, 11])

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
        # Xcode makes its defaults again, and nothing runs on a device whose runtime is gone (the owner's
        # decision, 2026-11-03).
        self.assertEqual(items[unavailable_pool["udid"]].status, disk.CANDIDATE)
        self.assertEqual(items[default["udid"]].status, disk.CANDIDATE)
        self.assertEqual(items[default["udid"]].command, ["xcrun", "simctl", "delete", default["udid"]])

    def test_a_pool_template_is_kept_until_its_runtime_is_gone(self):
        template = device("Project Duo template (iOS-27-0)")
        gone = device("Project iPad template (iOS-27-0)", isAvailable=False, availabilityError="runtime missing")
        # The same name in another pool, or on another runtime than its name says, is no template of this pool.
        stranger = device("Other Duo template (iOS-27-0)")
        mislabeled = device("Project Duo template (iOS-26-0)")
        record = json.dumps({"devices": {"Project Duo template (iOS-27-0)": {"device": template["udid"],
                                                                             "state": "released"}}})
        items = self.simulators([template, gone, stranger, mislabeled], pools=frozenset({"Project"}),
                                records={"/record.json": record})
        self.assertEqual(items[template["udid"]].status, disk.KEPT)
        self.assertIn("the Project pool's warmed template", items[template["udid"]].reason)
        self.assertIsNone(items[template["udid"]].command)
        self.assertEqual(items[gone["udid"]].status, disk.CANDIDATE)
        self.assertIn("its runtime gone; the next new member of the kind warms another", items[gone["udid"]].reason)
        # Its deletion goes through the pool's prune, which reserves it, checks it again and records it.
        self.assertEqual(items[gone["udid"]].command[-6:],
                         ["simulator-pool", "--pool", "Project", "--owner", "eng disk", "prune", gone["udid"]][-6:])
        for other in (stranger, mislabeled):
            self.assertNotIn("warmed template", items[other["udid"]].reason)

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

    def test_a_runtime_no_device_uses_is_a_candidate_once_unused_for_14_days(self):
        stamp = lambda days: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(LATER - days * 86400))
        listing = {"devices": {"com.apple.CoreSimulator.SimRuntime.iOS-27-0": [device("iPhone 17")]}}
        runtime = lambda key, version, **values: {"identifier": key, "sizeBytes": 8,
            "runtimeIdentifier": f"com.apple.CoreSimulator.SimRuntime.iOS-{version}", **values}
        images = {"A": runtime("A", "27-0", lastUsedAt=stamp(40)), "B": runtime("B", "26-5"),
                  "C": runtime("C", "27-1", lastUsedAt=stamp(20)), "D": runtime("D", "27-2", lastUsedAt=stamp(3)),
                  "E": runtime("E", "27-3", deletable=False)}
        items = {item.path[0]: item for item in disk.inventory(self.host(simulators=listing, runtimes=images, now=LATER),
                                                                disk.Sizes(measure=False), ["runtime"])}
        self.assertEqual(items["A"].status, disk.KEPT)  # a device uses it, however long ago
        for key in "BC":
            self.assertEqual(items[key].status, disk.CANDIDATE, items[key].reason)
            self.assertEqual(items[key].command, ["xcrun", "simctl", "runtime", "delete", key])
        self.assertEqual(items["D"].status, disk.KEPT)
        self.assertIn("within 14 days", items["D"].reason)
        self.assertEqual(items["E"].status, disk.KEPT)

    def test_a_superseded_runtime_build_is_a_candidate_unless_chosen_pinned_reserved_or_recent(self):
        # vocaro-engineering#188: devices of an identifier run on one build, so another build of it is idle.
        stamp = lambda days: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(LATER - days * 86400))
        ios = lambda version: f"com.apple.CoreSimulator.SimRuntime.iOS-{version}"
        listing = {"devices": {ios(version): [device("iPhone 17")] for version in ("27-0", "27-2", "27-3", "27-4")}}
        runtime = lambda key, version, build, **values: {"identifier": key, "sizeBytes": 8, "build": build,
                                                          "runtimeIdentifier": ios(version), **values}
        images = {"old": runtime("old", "27-2", "24B5000", lastUsedAt=stamp(30)),
                  "chosen": runtime("chosen", "27-2", "24B8117", lastUsedAt=stamp(40)),
                  "pinned": runtime("pinned", "27-2", "24B5110a", lastUsedAt=stamp(30)),
                  "beta": runtime("beta", "27-0", "24A5423a"),
                  "release": runtime("release", "27-0", "24A434", lastUsedAt=stamp(1)),
                  "recent": runtime("recent", "27-3", "24C10", lastUsedAt=stamp(6)),
                  "newer": runtime("newer", "27-3", "24C20", lastUsedAt=stamp(1)),
                  "reserved": runtime("reserved", "27-4", "24D10", lastUsedAt=stamp(30)),
                  "current": runtime("current", "27-4", "24D20", lastUsedAt=stamp(1)),
                  "only": runtime("only", "27-5", "24E10", lastUsedAt=stamp(30))}
        matches = {"iphoneos27.2": {"chosenRuntimeBuild": "24B8117", "sdkVersion": "27.2"}}
        pinned = {"24B5110a": "qualified for guarded VoiceOver activation", "24E10": "named by a test pin"}
        records = {"/record.json": json.dumps({"devices": {"x": {"runtimeBuild": "24D10"},
                   # A warmed template's census of installed builds names a build it does not run.
                   "y": {"template": {"runtimeBuild": "24A434", "installedBuilds": ["24A434", "24A5423a"]}}}})}
        def items(**values):
            host = self.host(simulators=listing, runtimes=images, matches=matches, pinned=pinned, records=records)
            for name, value in values.items(): setattr(host, name, value)
            return {item.path.split(" ")[0]: item for item in disk.inventory(host, disk.Sizes(measure=False), ["runtime"])}
        found = items()
        for key in ("old", "beta"):
            self.assertEqual(found[key].status, disk.CANDIDATE, found[key].reason)
            self.assertEqual(found[key].command, ["xcrun", "simctl", "runtime", "delete", key])
        self.assertIn("superseded by 24B8117", found["old"].reason)
        self.assertIn("iphoneos27.2 SDK", found["old"].reason)
        self.assertIn("superseded by 24A434", found["beta"].reason)
        self.assertIn("CoreSimulator used last", found["beta"].reason)
        expected = {"chosen": "devices use it", "release": "devices use it", "newer": "devices use it",
                    "current": "devices use it", "pinned": "qualified for guarded VoiceOver activation",
                    "recent": "within 14 days", "reserved": "the device record /record.json names it",
                    "only": "named by a test pin"}
        for key, why in expected.items():
            self.assertEqual(found[key].status, disk.KEPT, key)
            self.assertIn(why, found[key].reason, key)
            self.assertIsNone(found[key].command, key)
        # Without the SDK's choice, two builds CoreSimulator used last at once leave the choice unknown.
        self.assertEqual(items(matches=None)["old"].status, disk.KEPT)
        self.assertIn("devices use it", items(matches=None)["old"].reason)
        # A record that cannot be read keeps every superseded build.
        self.assertIn("could not be read", items(records={"/record.json": None})["old"].reason)
        # When neither the SDK nor any use shows the choice, no build of the identifier is superseded.
        unused = {key: {**image, "lastUsedAt": None} for key, image in images.items()}
        self.assertEqual(items(runtimes=unused, matches=None)["beta"].status, disk.KEPT)


    def test_a_runtime_candidate_names_the_second_macs_copy_and_stays_while_that_copy_would_come_back(self):
        # vocaro-engineering#188 and #123: `eng remote setup --runtimes` keeps both Macs' runtimes alike.
        stamp = lambda days: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(LATER - days * 86400))
        ios = lambda version: f"com.apple.CoreSimulator.SimRuntime.iOS-{version}"
        listing = {"devices": {ios("27-0"): [device("iPhone 17")]}}
        images = {"beta": {"identifier": "beta", "sizeBytes": 8, "build": "24A5423a", "runtimeIdentifier": ios("27-0")},
                  "release": {"identifier": "release", "sizeBytes": 8, "build": "24A434", "runtimeIdentifier": ios("27-0"),
                              "lastUsedAt": stamp(1)},
                  "unused": {"identifier": "unused", "sizeBytes": 8, "build": "24A100", "runtimeIdentifier": ios("26-0")}}
        def found(there, second="build-host"):
            host = self.host(simulators=listing, runtimes=images, offload_host=second, remote_runtimes=there)
            return {item.path.split(" ")[0]: item for item in disk.inventory(host, disk.Sizes(measure=False), ["runtime"])}
        copy = lambda build, users: {"build": build, "users": users}
        items = found({"images": [copy("24A434", ["A"])]})
        for key in ("beta", "unused"):
            self.assertEqual(items[key].status, disk.CANDIDATE, items[key].reason)
            self.assertIn("build-host holds no copy, so removing it here also leaves the two Macs alike", items[key].reason)
        items = found({"images": [copy("24A5423a", []), copy("24A100", [])]})
        for key in ("beta", "unused"):
            self.assertEqual(items[key].status, disk.CANDIDATE, items[key].reason)
            self.assertIn("build-host holds a copy no simulator uses there", items[key].reason)
        items = found({"images": [copy("24A5423a", ["A", "B"]), copy("24A100", None)]})
        self.assertEqual(items["beta"].status, disk.KEPT)
        self.assertIn("superseded by 24A434", items["beta"].reason)
        self.assertIn("the 2 simulators on its identifier there count as used, so `eng remote setup --runtimes` "
                      "would copy it back here", items["beta"].reason)
        self.assertEqual(items["unused"].status, disk.KEPT)
        self.assertIn("could not be listed", items["unused"].reason)
        items = found({"error": "it did not answer in 180s"})
        for key in ("beta", "unused"):
            self.assertEqual(items[key].status, disk.KEPT)
            self.assertIsNone(items[key].command)
            self.assertIn("build-host's runtimes could not be read (it did not answer in 180s)", items[key].reason)
        self.assertEqual(items["release"].status, disk.KEPT)  # the chosen build is untouched
        # Without a second Mac the reason names none.
        items = found(None, second=None)
        self.assertEqual(items["beta"].status, disk.CANDIDATE)
        self.assertNotIn("build-host", items["beta"].reason)

    def test_the_second_macs_runtimes_are_read_as_eng_remote_reads_them(self):
        from vocaro_engineering import remote
        self.assertIsNone(disk.runtimes_there(None))
        with patch.object(remote, "developer_directory", return_value="/X.app"), \
                patch.object(remote, "runtime_images", return_value={"images": [{"build": "B", "users": []}]}) as read:
            self.assertEqual(disk.runtimes_there("build-host"), {"images": [{"build": "B", "users": []}]})
        read.assert_called_once_with("build-host", "build-host", "/X.app")
        with patch.object(remote, "developer_directory", return_value="/X.app"), \
                patch.object(remote, "runtime_images", side_effect=remote.Misconfigured("unreachable", "fix")):
            self.assertIn("error", disk.runtimes_there("build-host"))
        with patch.object(remote, "developer_directory", return_value="/X.app"), \
                patch.object(remote, "runtime_images", return_value={"images": None}):
            self.assertEqual(disk.runtimes_there("build-host"), {"error": "simctl did not list its runtime images"})


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

    def test_derived_data_recording_no_workspace_is_a_candidate_only_without_build_products(self):
        root = self.home / "Library/Developer/Xcode/DerivedData"
        def tree(name, *folders):
            directory = root / (name + "-" + "f" * 28)
            for folder in folders: (directory / folder).mkdir(parents=True)
            return directory
        resolved = tree("Resolved", "Logs", "SourcePackages", "TestResults")
        logs = tree("Logs", "Logs")
        built = tree("Built", "Logs", "SourcePackages", "Build")
        # What a test-without-building with no -derivedDataPath leaves: a test run's coverage data, no products.
        coverage = tree("Coverage", "Logs", "TestResults", "Build/ProfileData/F49BFB5B")
        (coverage / "Build/ProfileData/F49BFB5B/Coverage.profdata").write_bytes(b"profile")
        products = tree("Products", "Logs", "TestResults", "Build/ProfileData", "Build/Products")
        indexed = tree("Indexed", "Logs", "Index.noindex")
        used = tree("Used", "Logs", "SourcePackages")
        recent = tree("Recent", "Logs", "SourcePackages")
        os.utime(recent / "Logs", (LATER - 3600, LATER - 3600))
        process = disk.Process(6, "xcodebuild", cwd=str(used / "SourcePackages/checkouts/Package"))
        items = self.items(self.host([process]), ["derived-data"])
        for candidate in (resolved, logs, coverage):
            self.assertEqual(items[str(candidate)].status, disk.CANDIDATE, items[str(candidate)].reason)
            self.assertEqual(items[str(candidate)].command, ["rm", "-rf", "--", str(candidate)])
        self.assertIn("records no workspace; holds only Logs, SourcePackages, TestResults, no build products",
                      items[str(resolved)].reason)
        # A folder with build products, or anything else this rule does not know, stays the owner's decision.
        self.assertIn("records no workspace; holds only Build, Logs, TestResults, Build holding only coverage data",
                      items[str(coverage)].reason)
        for owner in (built, indexed, products):
            self.assertEqual(items[str(owner)].status, disk.OWNER, items[str(owner)].reason)
            self.assertIn("records no workspace; holds", items[str(owner)].reason)
        self.assertEqual(items[str(used)].status, disk.KEPT)
        self.assertIn("in use: PID 6 (xcodebuild)", items[str(used)].reason)
        self.assertEqual(items[str(recent)].status, disk.KEPT)
        self.assertIn("but changed 60 min ago", items[str(recent)].reason)

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
        self.assertIn("eng offloads it by itself once nothing in it has changed for 24 h", items[str(session)].reason)
        # The retry offers the same folders, each with the destination the listing names.
        self.assertEqual(disk.archive_folders(self.development),
                         [(measurements, None), (session, None), (kept, "Development/session-evidence/Project")])

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
        away = self.development / "away-evidence"
        away.mkdir()
        queue.add(away, None, False, "second cannot be reached")
        queue.fail(away, "FAILED: 1 files did not verify", away=True)
        items = self.items(self.host(offload_host="build-host"), ["evidence"])
        self.assertEqual(set(items), {str(measurements), str(elsewhere), str(failed), str(away)})
        self.assertIn("found the second Mac away, and eng offers it again once it answers", items[str(away)].reason)
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


class FailureRecordTests(Fixture):
    def record(self, checkout, name, days, label="iPad"):
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(LATER - days * 86400))
        path = checkout / ".build/failures" / name / f"{stamp}-{label}-0000"
        path.mkdir(parents=True)
        (path / "failures.json").write_text("{}")
        return path

    def test_records_are_counted_in_each_checkout_and_those_past_the_rule_are_a_candidate(self):
        from vocaro_engineering import failure_records
        old = self.record(self.main, "ways-in", failure_records.MAX_AGE_DAYS + 1)
        young = self.record(self.main, "ways-in", 1)
        other = self.record(self.main, "recents", 2)
        worktree = self.worktree("diagnosing")
        recent = self.record(worktree, "ways-in", 0)
        processes = [disk.Process(201, "xcresulttool", paths=(str(recent / "failures.json"),))]
        items = self.items(self.host(processes), ["failures"])
        main, there = items[str(self.main / ".build/failures")], items[str(worktree / ".build/failures")]
        self.assertEqual(main.status, disk.CANDIDATE, main.reason)
        self.assertIn("3 records of recents (1), ways-in (2); 1 past the rule", main.reason)
        self.assertIn(failure_records.RULE, main.reason)
        self.assertEqual(main.command, ["python3", str(disk.TOOLKIT_ROOT / "bin/eng"), "failures", "prune",
                                        str(self.main)])
        self.assertEqual(there.status, disk.KEPT)
        self.assertIn("1 record of ways-in (1); in use: PID 201", there.reason)
        self.assertEqual(set(items), {str(self.main / ".build/failures"), str(worktree / ".build/failures")})
        # The negative control: within the rule, the records are counted and kept.
        shutil.rmtree(old)
        items = self.items(self.host(), ["failures"])
        self.assertEqual(items[str(self.main / ".build/failures")].status, disk.KEPT)
        self.assertIn("2 records of recents (1), ways-in (1); none past the rule",
                      items[str(self.main / ".build/failures")].reason)
        # Nothing was removed by the inventory.
        for path in (young, other, recent): self.assertTrue(path.exists())

    def test_delete_prunes_by_the_rule_inside_a_checkout(self):
        from vocaro_engineering import failure_records
        old = self.record(self.main, "ways-in", failure_records.MAX_AGE_DAYS + 1)
        young = self.record(self.main, "ways-in", 1)
        host = self.host()
        [item] = disk.inventory(host, disk.Sizes(measure=False), ["failures"])
        ran = []
        def run(command, **_):
            ran.append(command)
            # As eng failures prune would, by the rule at the inventory's time.
            failure_records.prune(Path(command[-1]), now=LATER)
            return subprocess.CompletedProcess(command, 0, "", "")
        disk.delete([item], lambda category: disk.inventory(host, disk.Sizes(measure=False), [category]),
                    self.root / "ledger.jsonl", out=lambda *_: None, run=run)
        self.assertEqual(ran, [item.command])
        self.assertEqual((old.exists(), young.exists()), (False, True))

    def test_records_and_a_lanes_results_alone_do_not_keep_a_landed_worktree(self):
        commit(self.main, ".gitignore", ".build/\n")
        self.publish()
        records = self.worktree("records")
        self.record(records, "ways-in", 1)
        results = self.worktree("results")
        (results / ".build/engineering/ways-in/results/failures/R").mkdir(parents=True)
        (results / ".build/engineering/ways-in/results/failures/R/failures.json").write_text("{}")
        items = self.items(self.host(), ["worktree"])
        self.assertEqual(items[str(records)].status, disk.CANDIDATE, items[str(records)].reason)
        self.assertEqual(items[str(results)].status, disk.CANDIDATE, items[str(results)].reason)


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
