"""Scratch worktrees with a record of their maker, and their recovery once it is gone (worktree_ownership, #138)."""
import contextlib
import fcntl
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src/python"))
import host_isolation  # noqa: F401
from queuekeeper import disk, merge_queue as mq, offload_host, worktree_ownership as wo

HERE = "TEST-MAC-UUID"
GIT = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com", "GIT_ALLOW_PROTOCOL": "file"}
REASON = mq.TRIAL_REASON
# A maker that is killed inside its scratch block, as an interrupted `qk land` is: it prints the worktree's path, runs
# what it was asked to there, and is killed before its finally can run.
MAKER = """
import os, signal, sys
sys.path.insert(0, sys.argv[1])
from queuekeeper import worktree_ownership as wo
with wo.scratch(sys.argv[2], "refs/remotes/origin/main", "trial", sys.argv[3], "project") as path:
    exec(sys.argv[4])
    print(path, flush=True)
    os.kill(os.getpid(), signal.SIGKILL)
"""


def git(directory, *arguments):
    return subprocess.run(["git", "-C", str(directory), *arguments], check=True, capture_output=True,
                          text=True).stdout.strip()


def nobody():
    return []


class Fixture(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        for patcher in (patch.dict(os.environ, GIT), patch.object(wo, "this_mac", lambda: HERE)):
            patcher.start()
            self.addCleanup(patcher.stop)
        origin = self.base / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
        self.repo = self.base / "project"
        subprocess.run(["git", "clone", "-q", str(origin), str(self.repo)], check=True, capture_output=True)
        (self.repo / ".gitignore").write_text("build/\n")
        (self.repo / "a").write_text("a\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "start")
        git(self.repo, "push", "-q", "origin", "HEAD:main")
        self.common = Path(git(self.repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        self.folders = []
        self.addCleanup(self.clean)

    def clean(self):
        for folder in self.folders: shutil.rmtree(folder, ignore_errors=True)

    def abandon(self, then="pass", reason=REASON):
        """The path of a scratch worktree whose maker was killed inside its block, after running then there."""
        done = subprocess.run([sys.executable, "-c", MAKER, str(ROOT / "src/python"), str(self.repo), reason, then],
                              capture_output=True, text=True, env={**os.environ, offload_host.HARDWARE_VARIABLE: HERE})
        self.assertEqual(done.returncode, -9, done.stderr)
        path = Path(done.stdout.strip())
        self.folders.append(path.parent)
        return path

    def reclaim(self, users=nobody, **options):
        return wo.reclaim(self.repo, census=wo.Census(take=users), **options)

    def records(self):
        return sorted(path.name for path in wo.records(self.common).glob("*.json"))

    def registered(self):
        return [each["path"] for each in wo.registrations(self.common)]

    def record(self):
        [path] = wo.records(self.common).glob("*.json")
        return path, json.loads(path.read_text())


class Interrupted(Fixture):
    def test_a_killed_makers_worktree_is_removed_once_and_a_second_recovery_finds_nothing(self):
        path = self.abandon()
        _, record = self.record()
        # What the record proves: this repository, this Mac, the maker and what the worktree was for.
        self.assertEqual((record["repository"], record["host"], record["purpose"], record["path"]),
                         (os.path.realpath(self.common), HERE, "trial", str(path)))
        self.assertIsInstance(record["pid"], int)
        self.assertTrue(record["start"])
        [registration] = wo.registrations(self.common)
        self.assertEqual(registration["reason"], f"{REASON} [{wo.MARK} {record['id']}]")
        [result] = self.reclaim()
        self.assertEqual(result["outcome"], wo.RECLAIMED, result)
        self.assertFalse(path.parent.exists())
        self.assertEqual((self.records(), self.registered()), ([], []))
        self.assertEqual(self.reclaim(), [])

    def test_a_dry_run_changes_nothing(self):
        path = self.abandon()
        [result] = self.reclaim(dry_run=True)
        self.assertEqual(result["outcome"], wo.RECLAIMED)
        self.assertTrue(path.is_dir())
        self.assertEqual(len(self.records()), 1)

    def test_a_record_whose_worktree_was_never_added_is_dropped_with_its_empty_folder(self):
        path = self.abandon()
        git(self.repo, "worktree", "unlock", str(path))
        git(self.repo, "worktree", "remove", str(path))
        [result] = self.reclaim()
        self.assertEqual(result["outcome"], wo.FORGOTTEN)
        self.assertFalse(path.parent.exists())
        self.assertEqual(self.records(), [])

    def test_a_folder_holding_more_than_the_worktree_stays_as_evidence(self):
        path = self.abandon("open(os.path.join(os.path.dirname(path), 'preflight.log'), 'w').write('ran')")
        [result] = self.reclaim()
        self.assertEqual(result["outcome"], wo.RECLAIMED)
        self.assertFalse(path.exists())
        self.assertEqual((path.parent / "preflight.log").read_text(), "ran")
        self.assertIn("preflight.log", result["why"])


class Protected(Fixture):
    def assertKept(self, path, words, **options):
        [result] = self.reclaim(**options)
        self.assertEqual(result["outcome"], wo.KEPT, result)
        self.assertIn(words, result["why"])
        self.assertIn(str(path), self.registered())
        self.assertEqual(len(self.records()), 1)
        return result

    def test_a_maker_that_still_runs_keeps_it_and_the_worker_says_nothing_of_it(self):
        with wo.scratch(self.repo, "refs/remotes/origin/main", "trial", REASON, "project") as path:
            self.assertKept(path, "still runs")
            self.assertEqual(self.reclaim(quiet_running=True), [])
        self.assertEqual((self.records(), self.registered()), ([], []))

    def test_a_reused_pid_is_no_live_maker_but_an_unreadable_one_is_kept(self):
        path = self.abandon()
        record_path, record = self.record()
        # The PID now runs another process, this one, started at another time.
        record_path.write_text(json.dumps({**record, "pid": os.getpid(), "start": "Thu Jan 1 00:00:00 1970"}))
        with patch.object(wo.resources, "process_start", side_effect=OSError("no ps")):
            self.assertKept(path, "is unknown")
        [result] = self.reclaim()
        self.assertEqual(result["outcome"], wo.RECLAIMED)

    def test_changes_untracked_and_ignored_files_and_an_operation_under_way_keep_it(self):
        for then, words in (("open(os.path.join(path, 'a'), 'w').write('b')", "holds changes"),
                            ("open(os.path.join(path, 'new'), 'w').write('b')", "holds changes"),
                            ("os.makedirs(os.path.join(path, 'build')); open(os.path.join(path, 'build', 'r'), 'w')",
                             "build/"),
                            ("import subprocess; read = lambda *words: subprocess.run(['git', '-C', path, *words], "
                             "capture_output=True, text=True).stdout.strip(); "
                             "open(read('rev-parse', '--path-format=absolute', '--git-path', 'MERGE_HEAD'), 'w')"
                             ".write(read('rev-parse', 'HEAD'))", "under way (MERGE_HEAD)")):
            with self.subTest(words=words):
                path = self.abandon(then)
                self.assertKept(path, words)
                if (path / "build").exists(): self.assertTrue((path / "build/r").exists())
                self.reset(path)

    def reset(self, path):
        git(self.repo, "worktree", "unlock", str(path))
        git(self.repo, "worktree", "remove", "--force", "--force", str(path))
        for record in wo.records(self.common).glob("*.json"): record.unlink()

    def test_a_commit_main_lacks_keeps_it_and_one_main_holds_as_an_equal_patch_does_not(self):
        commit = ("import subprocess; open(os.path.join(path, 'c'), 'w').write('c'); "
                  "subprocess.run(['git', '-C', path, 'add', 'c'], check=True); "
                  "subprocess.run(['git', '-C', path, 'commit', '-qm', 'c'], check=True)")
        path = self.abandon(commit)
        self.assertKept(path, "no equal patch on origin/main")
        # The same change lands on main as another commit: the scratch's own is then no work of its own.
        (self.repo / "c").write_text("c")
        git(self.repo, "add", "c")
        git(self.repo, "commit", "-qm", "c landed")
        git(self.repo, "push", "-q", "origin", "HEAD:main")
        [result] = self.reclaim()
        self.assertEqual(result["outcome"], wo.RECLAIMED, result)

    def test_a_process_using_it_keeps_it_and_so_does_a_census_that_fails(self):
        path = self.abandon()
        self.assertKept(path, "has its working directory, an open file or an argument inside",
                        users=lambda: [disk.Process(4242, "xcodebuild", str(path))])
        def failing(): raise RuntimeError("could not list processes with lsof and ps")
        self.assertKept(path, "whether a process uses it is unknown", users=failing)

    def test_another_recovery_under_way_leaves_it_to_that_one(self):
        path = self.abandon()
        with open(wo.records(self.common) / ".lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.assertEqual(self.reclaim(), [])
        self.assertTrue(path.is_dir())

    def test_another_mac_a_lock_that_names_no_record_and_a_broken_record_keep_it(self):
        path = self.abandon()
        record_path, record = self.record()
        for change, words in (({"host": "OTHER-MAC", "hostName": "studio"}, "another Mac (studio)"),
                              ({"repository": "/elsewhere/.git"}, "another repository"),
                              ({"start": None}, "not whole")):
            with self.subTest(words=words):
                record_path.write_text(json.dumps({**record, **change}))
                self.assertKept(path, words)
        record_path.write_text(json.dumps(record))
        git(self.repo, "worktree", "unlock", str(path))
        git(self.repo, "worktree", "lock", "--reason", REASON, str(path))
        self.assertKept(path, "does not name this record")

    def test_a_path_that_is_no_worktree_is_never_removed(self):
        path = self.abandon()
        git(self.repo, "worktree", "unlock", str(path))
        git(self.repo, "worktree", "remove", str(path))
        path.mkdir()
        (path / "work").write_text("someone's")
        [result] = self.reclaim()
        self.assertEqual(result["outcome"], wo.KEPT)
        self.assertIn("no worktree of this repository", result["why"])
        self.assertEqual((path / "work").read_text(), "someone's")


class MissingFolder(Fixture):
    def test_a_registration_whose_folder_went_is_forgotten_alone_once_its_head_is_landed(self):
        path = self.abandon()
        other = self.base / "other"
        git(self.repo, "worktree", "add", "-q", "--detach", str(other), "HEAD")
        shutil.rmtree(other)
        shutil.rmtree(path)
        [result] = self.reclaim()
        self.assertEqual(result["outcome"], wo.RECLAIMED, result)
        # Only its own registration went: the other missing one, which no record names, is still listed.
        self.assertEqual([os.path.realpath(each) for each in self.registered()], [os.path.realpath(other)])
        self.assertEqual(self.records(), [])

    def test_a_missing_folder_whose_head_main_lacks_is_kept(self):
        path = self.abandon("import subprocess; open(os.path.join(path, 'c'), 'w').write('c'); "
                            "subprocess.run(['git', '-C', path, 'add', 'c'], check=True); "
                            "subprocess.run(['git', '-C', path, 'commit', '-qm', 'c'], check=True)")
        shutil.rmtree(path)
        [result] = self.reclaim()
        self.assertEqual(result["outcome"], wo.KEPT)
        self.assertIn("no equal patch", result["why"])
        self.assertEqual(len(self.registered()), 1)


class GoneQueueCheckouts(Fixture):
    """A merge queue's long-lived checkouts whose folder went with a temporary queue folder (#356)."""
    def gone(self, name, reason=mq.TOOLKIT_REASON, at="HEAD", age=2 * wo.SETTLED):
        path = self.base / "tmp-queue" / "queuekeeper-workers" / name / "toolkit"
        git(self.repo, "worktree", "add", "-q", "--detach", "--lock", "--reason", reason, str(path), at)
        shutil.rmtree(path)
        then = time.time() - age
        admin = wo.registration(self.common, path)["admin"]
        for entry in (admin, *Path(admin).iterdir()): os.utime(entry, (then, then))
        return path

    def test_a_gone_landed_copy_is_forgotten_and_nothing_else_is(self):
        copy = self.gone("a")
        checkout = self.gone("b", mq.CHECKOUT_REASON)
        other = self.gone("c", "someone else's")
        live = self.base / "live"
        git(self.repo, "worktree", "add", "-q", "--detach", "--lock", "--reason", mq.TOOLKIT_REASON, str(live), "HEAD")
        results = wo.forget_gone(self.repo, mq.QUEUE_REASONS)
        self.assertEqual(sorted((os.path.realpath(each["path"]), each["outcome"]) for each in results),
                         sorted([(os.path.realpath(copy), wo.FORGOTTEN), (os.path.realpath(checkout), wo.FORGOTTEN)]))
        self.assertEqual(sorted(os.path.realpath(each) for each in self.registered()),
                         sorted([os.path.realpath(other), os.path.realpath(live)]))
        self.assertTrue(live.is_dir())

    def test_a_dry_run_forgets_nothing(self):
        copy = self.gone("a")
        [result] = wo.forget_gone(self.repo, mq.QUEUE_REASONS, dry_run=True)
        self.assertEqual(result["outcome"], wo.FORGOTTEN)
        self.assertEqual([os.path.realpath(each) for each in self.registered()], [os.path.realpath(copy)])

    def test_one_gone_lately_waits_unless_it_is_the_callers_own(self):
        copy = self.gone("a", age=0)
        self.assertEqual(wo.forget_gone(self.repo, mq.QUEUE_REASONS), [])
        self.assertEqual(len(self.registered()), 1)
        [result] = wo.forget_gone(self.repo, mq.QUEUE_REASONS, mine=copy)
        self.assertEqual(result["outcome"], wo.FORGOTTEN)
        self.assertEqual(self.registered(), [])

    def test_a_head_main_lacks_is_kept(self):
        scratch = self.base / "scratch"
        git(self.repo, "worktree", "add", "-q", "--detach", str(scratch), "HEAD")
        (scratch / "c").write_text("c\n")
        git(scratch, "add", "c")
        git(scratch, "commit", "-qm", "c")
        unlanded = git(scratch, "rev-parse", "HEAD")
        git(self.repo, "worktree", "remove", "--force", str(scratch))
        self.gone("a", at=unlanded)
        [result] = wo.forget_gone(self.repo, mq.QUEUE_REASONS)
        self.assertEqual(result["outcome"], wo.KEPT)
        self.assertIn("no equal patch", result["why"])
        self.assertEqual(len(self.registered()), 1)

    def test_reclaim_forgets_them_and_reports_them(self):
        copy = self.gone("a")
        said = io.StringIO()
        with patch.object(wo, "Census", lambda real=wo.Census: real(take=nobody)), contextlib.redirect_stdout(said):
            self.assertEqual(mq.reclaim(self.repo, dry_run=True, as_json=True), 0)
        [listed] = json.loads(said.getvalue())["gone"]
        self.assertEqual((os.path.realpath(listed["path"]), listed["outcome"]), (os.path.realpath(copy), wo.FORGOTTEN))
        self.assertEqual(len(self.registered()), 1)
        said = io.StringIO()
        with patch.object(wo, "Census", lambda real=wo.Census: real(take=nobody)), contextlib.redirect_stdout(said):
            self.assertEqual(mq.reclaim(self.repo), 0)
        self.assertIn(f"forgotten {listed['path']} ({mq.TOOLKIT_REASON})", said.getvalue())
        self.assertEqual(self.registered(), [])


class Legacy(Fixture):
    def test_a_scratch_worktree_from_before_records_is_listed_and_never_removed(self):
        old = self.base / "eng-land-spelling-x" / "project"
        git(self.repo, "worktree", "add", "-q", "--detach", "--lock", "--reason", wo.LEGACY_REASONS[1], str(old), "HEAD")
        self.assertEqual(self.reclaim(), [])
        [listed] = wo.legacy(self.repo)
        self.assertEqual(os.path.realpath(listed["path"]), os.path.realpath(old))
        self.assertIn("maker is unknown", listed["why"])
        said = io.StringIO()
        with contextlib.redirect_stdout(said):
            self.assertEqual(mq.reclaim(self.repo), 1)
        self.assertIn(f"legacy {listed['path']}", said.getvalue())
        self.assertTrue(old.is_dir())

    def test_the_command_exits_0_with_nothing_left_and_reports_json(self):
        path = self.abandon()
        said = io.StringIO()
        with patch.object(wo, "Census", lambda real=wo.Census: real(take=nobody)), contextlib.redirect_stdout(said):
            self.assertEqual(mq.reclaim(self.repo, dry_run=True, as_json=True), 0)
        report = json.loads(said.getvalue())
        self.assertEqual((report["dryRun"], report["records"][0]["outcome"], report["legacy"]), (True, wo.RECLAIMED, []))
        self.assertTrue(path.is_dir())
        with patch.object(wo, "Census", lambda real=wo.Census: real(take=nobody)), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mq.reclaim(self.repo), 0)
        self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
