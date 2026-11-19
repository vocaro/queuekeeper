"""A whole run's executed-test count held against the lane's last passing whole run on main (count_history)."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src/python"))
import host_isolation  # noqa: F401 -- first, as every test module
from queuekeeper import count_history

HERE = "this Mac"


def git(directory, *arguments):
    return subprocess.run(["git", "-C", str(directory), "-c", "user.name=Test", "-c", "user.email=test@example.com",
                           *arguments], check=True, capture_output=True, text=True).stdout.strip()


class CountHistoryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "project"
        self.store = Path(temp.name) / "store"
        git(Path(temp.name), "init", "-q", "-b", "main", str(self.root))

    def commit(self, message="state"):
        git(self.root, "commit", "-q", "--allow-empty", "-m", message)
        return git(self.root, "rev-parse", "HEAD")

    def keep(self, commit, executed, mac="mac-a", lane="gate", status=0, outcome="passed", ran_on=HERE, **keys):
        """A run kept by commit as remote.keep_here keeps one; executed None records no count, as before counts were."""
        inner = {"mac": mac, "outcome": outcome, "project": {"revision": commit}}
        if executed is not None: inner["tests"] = {"executed": executed, "skipped": 0}
        kept = {"ranOn": ran_on, "mac": mac, "commit": commit, "head": commit, "lane": lane, "arguments": [],
                "result": {"exitStatus": status, "outcome": outcome, "stopped": False, "receipt": inner}, **keys}
        (self.store / commit).mkdir(parents=True, exist_ok=True)
        (self.store / commit / f"{lane}.json").write_text(json.dumps(kept))

    def run_at(self, commit, executed, mac="mac-a"):
        receipt = {"mac": mac, "project": {"revision": commit}}
        if executed is not None: receipt["tests"] = {"executed": executed, "skipped": 0}
        said = []
        return count_history.dropped(self.root, self.store, "gate", receipt, HERE, say=said.append), said

    def test_a_run_that_executed_fewer_than_its_last_pass_on_main_fails_naming_that_run(self):
        first = self.commit()
        self.keep(first, 120)
        head = self.commit()
        why, _ = self.run_at(head, 100)
        self.assertIn(f"gate executed 100 tests, 20 fewer than the 120 its last passing whole run on mac-a executed at "
                      f"{first[:12]} ({self.store / first / 'gate.json'})", why)
        self.assertIn(f"add the trailer `Tests-Removed: gate` to a commit after {first[:12]}", why)
        # Additions and an equal count need nothing.
        self.assertEqual(self.run_at(head, 120), (None, []))
        self.assertEqual(self.run_at(head, 500), (None, []))

    def test_a_log_that_reports_no_count_after_one_that_did_executed_none(self):
        first = self.commit()
        self.keep(first, 3)
        why, _ = self.run_at(self.commit(), None)
        self.assertIn("gate executed 0 tests, 3 fewer than the 3", why)

    def test_only_a_pass_of_the_whole_lane_on_the_same_mac_with_a_count_is_a_baseline(self):
        oldest = self.commit()
        self.keep(oldest, 50)
        cases = [{"executed": 90, "mac": "mac-b"},  # another Mac's tests may skip apart
                 {"executed": 90, "status": 2, "outcome": "failed"},
                 {"executed": 90, "outcome": "failed"},  # a zero exit judged short here
                 {"executed": 90, "arguments": ["-k", "x"]},
                 {"executed": 90, "snapshot": True},
                 {"executed": None}]  # kept before counts were recorded
        for keys in cases:
            with self.subTest(keys):
                commit = self.commit()
                self.keep(commit, **keys)
        # Every newer run is passed over for the oldest, whose 50 this run reaches.
        self.assertEqual(self.run_at(self.commit(), 50), (None, []))
        self.assertIsNotNone(self.run_at("HEAD", 49)[0])
        # Another Mac's pass, kept by its dispatch, is judged by its exit status, and so by the Mac it ran on.
        commit = self.commit()
        self.keep(commit, 70, mac="mac-a", ran_on="fake", outcome=None)
        self.assertIn(f"the 70 its last passing whole run on mac-a executed at {commit[:12]}",
                      self.run_at(self.commit(), 60)[0])
        # A Mac with no pass of its own is held to none.
        self.assertEqual(self.run_at("HEAD", 1, mac="mac-c"), (None, []))

    def test_a_branch_not_yet_on_main_sets_no_baseline(self):
        landed = self.commit()
        self.keep(landed, 10)
        git(self.root, "checkout", "-q", "-b", "topic")
        unlanded = self.commit()
        # A pass on the branch that fell, kept as a pass, must not lower what the next run is held to.
        self.keep(unlanded, 4)
        self.assertIn(f"than the 10 its last passing whole run on mac-a executed at {landed[:12]}",
                      self.run_at(self.commit(), 4)[0])
        # Once origin's main holds the branch, its runs are main's.
        git(self.root, "update-ref", "refs/remotes/origin/main", unlanded)
        self.assertEqual(self.run_at("HEAD", 4), (None, []))

    def test_a_trailer_after_the_baseline_lets_the_count_fall(self):
        first = self.commit()
        self.keep(first, 30)
        for message, lane, falls in [("Remove the old parser's tests\n\nTests-Removed: unit, gate", "gate", True),
                                     ("Remove tests\n\nTests-Removed: *", "gate", True),
                                     ("Remove tests\n\nTests-Removed: unit", "gate", False),
                                     ("Mention Tests-Removed: gate in prose", "gate", False)]:
            with self.subTest(message):
                git(self.root, "reset", "-q", "--hard", first)
                head = self.commit(message)
                why, said = self.run_at(head, 25)
                self.assertEqual(why is None, falls, why)
                if falls: self.assertIn("which a commit since says with Tests-Removed: gate", said[0])
        # A trailer before the baseline's run says nothing of a fall since it.
        git(self.root, "reset", "-q", "--hard", first)
        acknowledged = self.commit("Remove tests\n\nTests-Removed: gate")
        self.keep(acknowledged, 25)
        self.assertIsNotNone(self.run_at(self.commit(), 20)[0])

    def test_no_history_holds_a_run_to_nothing(self):
        self.assertEqual(self.run_at(self.commit(), 0), (None, []))
        # Nor does a checkout with no main to read history on.
        git(self.root, "checkout", "-q", "-b", "other")
        git(self.root, "branch", "-q", "-D", "main")
        self.keep(git(self.root, "rev-parse", "HEAD"), 9)
        self.assertEqual(self.run_at(self.commit(), 1), (None, []))

    def test_counts_record_the_executed_apart_from_the_skipped(self):
        self.assertEqual(count_history.counts((10, 3)), {"executed": 7, "skipped": 3})
        self.assertIsNone(count_history.counts(None))


if __name__ == "__main__":
    unittest.main()
