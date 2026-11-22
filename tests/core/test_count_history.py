"""A whole run's executed-test count held against the lane's last passing whole run on main (count_history)."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src/python"))
import host_isolation  # noqa: F401 -- first, as every test module
from queuekeeper import count_history, remote

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

    def keep(self, commit, executed, mac="mac-a", lane="gate", status=0, outcome="passed", ran_on=HERE, skipped=0,
             **keys):
        """A run kept by commit as remote.keep_here keeps one; executed None records no count, as before counts were."""
        inner = {"mac": mac, "outcome": outcome, "project": {"revision": commit}}
        if executed is not None: inner["tests"] = {"executed": executed, "skipped": skipped}
        kept = {"ranOn": ran_on, "mac": mac, "commit": commit, "head": commit, "lane": lane, "arguments": [],
                "result": {"exitStatus": status, "outcome": outcome, "stopped": False, "receipt": inner}, **keys}
        (self.store / commit).mkdir(parents=True, exist_ok=True)
        (self.store / commit / f"{lane}.json").write_text(json.dumps(kept))

    def run_at(self, commit, executed, mac="mac-a", skipped=0):
        receipt = {"mac": mac, "project": {"revision": commit}}
        if executed is not None: receipt["tests"] = {"executed": executed, "skipped": skipped}
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
        # A Mac with no pass of its own is held to the newest on any Mac (vocaro/queuekeeper#300).
        self.assertIn(f"the 70 its last passing whole run on main on any Mac selected, less the 0 this run skipped: "
                      f"mac-c has no passing whole run on main with a count, so it is held to mac-a's at {commit[:12]}",
                      self.run_at("HEAD", 1, mac="mac-c")[0])

    def test_a_mac_with_no_pass_of_its_own_is_held_to_the_tests_another_macs_pass_selected(self):
        """On 2026-11-19 a bisect's half that deleted 15 tests passed on the workstation, which had no count of its
        own, while build-host's last pass on main had executed 3200 and skipped 1 (vocaro/queuekeeper#300)."""
        base = self.commit()
        self.keep(base, 3200, mac="build-host", skipped=1)
        head = self.commit()
        why, _ = self.run_at(head, 3186, mac="workstation")
        self.assertIn(f"gate executed 3186 tests, 15 fewer than the 3201 its last passing whole run on main on any Mac "
                      f"selected, less the 0 this run skipped: workstation has no passing whole run on main with a "
                      f"count, so it is held to build-host's at {base[:12]}, which executed 3200 and skipped 1 "
                      f"({self.store / base / 'gate.json'})", why)
        self.assertIn(f"add the trailer `Tests-Removed: gate` to a commit after {base[:12]}", why)
        # The merge queue reads the fall as a gate that ran too few (Worker.weigh_passed).
        from queuekeeper import merge_queue
        self.assertTrue(merge_queue.COUNT_SHORT.search(why))
        # The negative control: a test that skips on build-host and runs here, or skips here and runs there, is no fall.
        self.assertEqual(self.run_at(head, 3201, mac="workstation"), (None, []))
        self.assertEqual(self.run_at(head, 3199, mac="workstation", skipped=2), (None, []))
        self.assertIsNotNone(self.run_at(head, 3198, mac="workstation", skipped=2)[0])
        # A trailer after the other Mac's pass lets the count fall there too.
        trailer = self.commit("Remove the hardware tests\n\nTests-Removed: gate")
        why, said = self.run_at(trailer, 3186, mac="workstation")
        self.assertIsNone(why)
        self.assertIn("held to build-host's", said[0])
        self.assertIn("which a commit since says with Tests-Removed: gate", said[0])

    def test_a_macs_own_pass_is_its_baseline_however_old_or_short_beside_another_macs(self):
        own = self.commit()
        self.keep(own, 100, mac="mac-a", skipped=5)
        newer = self.commit()
        # A newer pass on a Mac whose tests skip apart, by more than this Mac's would allow.
        self.keep(newer, 150, mac="mac-b", skipped=0)
        why, _ = self.run_at(self.commit(), 100, mac="mac-a", skipped=5)
        self.assertIsNone(why)
        why, _ = self.run_at("HEAD", 99, mac="mac-a", skipped=5)
        self.assertIn(f"1 fewer than the 100 its last passing whole run on mac-a executed at {own[:12]}", why)
        self.assertNotIn("any Mac", why)
        # A Mac with none of its own is held to the newest pass on any Mac, passing over one that kept no skipped count.
        self.assertIn("than the 150 its last passing whole run on main on any Mac selected",
                      self.run_at("HEAD", 1, mac="mac-c")[0])
        self.keep(newer, 150, mac="mac-b", skipped=None)
        self.assertIn(f"than the 105 its last passing whole run on main on any Mac selected, less the 0 this run "
                      f"skipped: mac-c has no passing whole run on main with a count, so it is held to mac-a's at "
                      f"{own[:12]}", self.run_at("HEAD", 1, mac="mac-c")[0])

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

    def correction_receipts(self, revision, ran_on="mac-a", mac="mac-a", executed=4232, skipped=92):
        for name in ("gate", "unit"):
            self.keep(revision, None, mac=mac, lane=name, ran_on=ran_on)
            path = self.store / revision / f"{name}.json"
            data = json.loads(path.read_text())
            receipt = count_history.lane_receipt(data)
            receipt["project"]["workingChangesSHA256"] = remote.CLEAN
            receipt.update(toolkit={"workingChangesSHA256": remote.CLEAN}, exitStatus=0)
            if name == "gate":
                member = {"status": 0, "outcome": "passed", "executedTests": executed, "skippedTests": skipped}
                receipt.update(parallel=["unit", "other"], lanes={"unit": member, "other": member})
            else:
                receipt.update(command=["test"], arguments=[], tests={"executed": 4226, "skipped": 98})
            path.write_text(json.dumps(data))

    def plan_correction(self, revision, mac="mac-a"):
        return count_history.correction_plan(self.root, self.store, "gate", "unit", revision, mac, HERE,
                                             "Reviewed evidence", lambda *_: (["unit", "other"], ["test"]),
                                             remote.CLEAN)

    def test_correction_applies_to_the_exact_member_and_mac_via_the_normal_group_judge(self):
        first = self.commit()
        git(self.root, "update-ref", "refs/remotes/origin/main", first)
        self.correction_receipts(first)
        historical = self.store / first / "gate.json"
        original = historical.read_bytes()
        path, record = self.plan_correction(first)
        self.assertFalse(path.parent.exists())
        count_history.record_correction(path, record)
        with self.assertRaisesRegex(ValueError, "a correction already exists"):
            count_history.record_correction(path, record)
        self.assertEqual(historical.read_bytes(), original)
        self.assertEqual(json.loads(path.read_bytes())["baseline"]["receipt"], json.loads(original))
        self.assertEqual(record["evidence"]["receipt"]["result"]["receipt"]["tests"], record["after"])
        commit = self.commit()
        config = {"lanes": {"gate": {"parallel": ["unit", "other"]}, "unit": {}, "other": {}}}

        def judge(executed, skipped):
            inner = {"mac": "mac-a", "lanes": {
                "unit": {"status": 0, "outcome": "passed", "executedTests": executed, "skippedTests": skipped},
                "other": {"status": 0, "outcome": "passed", "executedTests": 4232, "skippedTests": 92}}}
            failed = remote.group_held_to_history(self.root, config, self.store, "gate", inner, commit)
            return failed, inner

        self.assertEqual(judge(4226, 98)[0], [])
        failed, inner = judge(4225, 99)
        self.assertEqual(failed, ["unit"])
        self.assertIn("1 fewer than the 4226", inner["error"])
        self.assertIn(str(path), inner["error"])
        self.assertEqual(count_history.baseline(self.root, self.store, "other", "mac-a", commit, HERE,
                                                group="gate").executed, 4232)
        self.assertEqual(count_history.baseline(self.root, self.store, "unit", "mac-b", commit, HERE,
                                                group="gate").executed, 4232)
        self.assertEqual(count_history.baseline(self.root, self.store, "unit", "mac-a", commit, HERE).executed, 4226)
        another = json.loads(original)
        another["lane"] = "another-group"
        historical.with_name("another-group.json").write_text(json.dumps(another))
        self.assertEqual(count_history.baseline(self.root, self.store, "unit", "mac-a", commit, HERE,
                                                group="another-group").executed, 4232)
        # Replacing the receipt at the same commit invalidates the correction's binding.
        historical.write_bytes(original + b"\n")
        self.assertEqual(judge(4226, 98)[0], ["unit"])
        self.assertTrue(path.exists())
        historical.write_bytes(original)
        self.correction_receipts(commit, executed=4235)
        git(self.root, "update-ref", "refs/remotes/origin/main", commit)
        self.assertEqual(count_history.baseline(self.root, self.store, "unit", "mac-a", commit, HERE,
                                                group="gate").executed, 4235)

    def test_correction_refuses_incompatible_or_stale_receipts(self):
        first = self.commit()
        git(self.root, "update-ref", "refs/remotes/origin/main", first)
        for changes, refused in (
            ({"mac": "mac-b"}, "passing whole run on mac-a"),
            ({"macId": "other-mac"}, "different execution Mac identities"),
            ({"arguments": ["filter"]}, "passing whole run"),
            ({"snapshot": True}, "passing whole run"),
            ({"expect": 4226}, "passing whole run"),
            ({"command": ["different"]}, "command must match"),
            ({"tests": {"executed": 4225, "skipped": 98}}, "select the same total"),
            ({"tests": {"executed": 0, "skipped": 4324}}, "select the same total"),
        ):
            with self.subTest(changes):
                self.correction_receipts(first)
                base = self.store / first
                group = json.loads((base / "gate.json").read_bytes())
                group["macId"] = "original-mac"
                (base / "gate.json").write_text(json.dumps(group))
                evidence = json.loads((base / "unit.json").read_bytes())
                updated = count_history.lane_receipt(evidence) if changes.keys() & {"command", "tests"} else evidence
                updated.update(changes)
                (base / "unit.json").write_text(json.dumps(evidence))
                with self.assertRaisesRegex(ValueError, refused):
                    self.plan_correction(first)
        second = self.commit()
        self.correction_receipts(second)
        with self.assertRaisesRegex(ValueError, "first-parent history of origin/main"):
            self.plan_correction(second)
        git(self.root, "update-ref", "refs/remotes/origin/main", second)
        with self.assertRaisesRegex(ValueError, "is not the current"):
            self.plan_correction(first)
        self.assertFalse((self.store / "count-corrections").exists())


if __name__ == "__main__":
    unittest.main()
