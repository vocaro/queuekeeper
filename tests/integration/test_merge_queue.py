"""The merge queue against real Git repositories: a bare origin, a clone, and lanes that read the tree."""
import argparse
import atexit
import contextlib
import functools
import importlib.util
import io
import json
import os
from pathlib import Path
import plistlib
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import types
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src/python"))
from queuekeeper import agent_logs, agent_results, cli, config as configuration, landing_proofs, lane_coverage, \
    merge_queue as mq, offload_host
# What qk land runs before it queues, which Fixture leaves out but PreflightBeforeQueued puts back.
PREFLIGHT_BEFORE_QUEUED = mq.preflight_before_queued
import host_isolation
from host_isolation import stretched

from test_support import farm as farm_fixture
from test_support.queue import (FLAGGED, LANE, GIT_ALONE, Fixture, Replaced, accepting_lane_key,
                                local_toolkit, never_exec, run)

shard_by = "class"

# Capacity readings (offload_host.capacity) of a Mac with room for a lane, and of one that admission declines: its CPU over
# its limit while admitted work runs there (remote.spare).
ROOMY = {"slots": 2, "slotsMax": 4, "busy": [], "cpuBusy": 10, "busyLimit": 90, "memory": 1}
CROWDED = {**ROOMY, "busy": [0], "cpuBusy": 98}


def BOTH_HERE(mac, hosts, waits):
    """mq.apart for a fixture with no other Mac: the second gate of a pair runs on this Mac beside the first, with no
    placement option, so that a test sees both run."""
    return (), None, "on this Mac, as the fixture has no other"


class EntryIdentifiers(Fixture):
    def test_branch_subject_and_explicit_issue_names_are_ref_safe(self):
        self.assertEqual(mq.entry_slug("codex/fix-share-sheet", "Fix #42"), "issue-42-fix-share-sheet")
        self.assertEqual(mq.entry_slug("agent/layout", "Fix layout", "Closes #29"), "issue-29-layout")
        self.assertEqual(mq.entry_slug("codex/layout#29", "Adjust layout"), "issue-29-layout-29")
        self.assertEqual(mq.entry_slug("HEAD", "Fix the share sheet"), "fix-the-share-sheet")
        self.assertEqual(mq.entry_slug("main", "Fix café / layout.. @{x}"), "fix-cafe-layout-x")
        self.assertEqual(mq.entry_slug(None, "🚀"), "change")
        self.assertLessEqual(len(mq.entry_slug("a" * 200, "subject")), 56)

    def test_queue_entry_prefers_a_local_closing_reference_over_other_mentions(self):
        branch = "codex/layout#91"
        self.commit("a.txt", branch)
        run(self.repo, "git", "commit", "--amend", "-qm", "Adjust layout (Refs #42)",
            "-m", "See vocaro/queuekeeper#312 for context.\n\nCloses #1083")
        identifier, = self.add(branch)
        self.assertRegex(identifier, r"^qk\d{4}-issue-1083-layout-91$")

    def test_queue_entry_only_takes_qualified_references_to_its_origin_repository(self):
        run(self.repo, "git", "remote", "set-url", "--push", "origin", "git@github.com:vocaro/EPUBLib.git")
        self.commit("a.txt", "codex/layout")
        run(self.repo, "git", "commit", "--amend", "-qm", "Adjust layout (vocaro/queuekeeper#312)",
            "-m", "Fixes: https://github.com/vocaro/queuekeeper/issues/312\n\n"
                  "RESOLVES: VOCARO/epublib#1083")
        local, = self.add("codex/layout")
        self.assertRegex(local, r"^qk\d{4}-issue-1083-layout$")

        self.commit("b.txt", "codex/measurement")
        run(self.repo, "git", "commit", "--amend", "-qm", "Measure layout (vocaro/queuekeeper#312)",
            "-m", "See vocaro/queuekeeper#311.\n\nCloses vocaro/queuekeeper#313")
        external, = self.add("codex/measurement")
        self.assertRegex(external, r"^qk\d{4}-measurement$")

    def test_new_ids_are_descriptive_and_existing_ids_are_never_overwritten(self):
        self.commit("a.txt", "codex/fix-layout")
        queue = mq.Queue(self.repo)
        queue.save({"id": "qk0007-fix-layout", "submitted": 0, "state": "landed"})
        with patch.object(mq.secrets, "randbelow", side_effect=[7, 8]), queue.locked():
            identifier = mq.entry_identifier(queue, "codex/fix-layout", "Fix layout")
        self.assertEqual(identifier, "qk0008-fix-layout")
        first, = self.add("codex/fix-layout")
        self.assertRegex(first, r"^qk\d{4}-fix-layout$")
        self.assertEqual(self.state(first)["branch"], "codex/fix-layout")

    def test_an_entry_records_the_coding_agent_whose_session_queued_it(self):
        sessions = ("CLAUDE_CODE_HOST_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID")
        self.commit("a.txt", "fix-layout")
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-7"}):
            for name in sessions[:2]: os.environ.pop(name, None)
            first, = self.add("fix-layout")
        self.assertEqual((self.state(first)["agent"], self.state(first)["agentSession"]), ("codex", "thread-7"))
        self.commit("b.txt", "by-hand")
        with patch.dict(os.environ, {}):
            for name in sessions: os.environ.pop(name, None)
            second, = self.add("by-hand")
        self.assertNotIn("agent", self.state(second))


class GateReceiptReuse(Fixture):
    def setUp(self):
        super().setUp()
        (self.repo / ".git/info/exclude").write_text(".build/\n")
        self.inputs = self.base / "gate-inputs"
        self.inputs.write_text("toolchain 1")
        self.landing_config = self.config()
        self.landing_config["queue"]["reuseGate"] = {"gate": ["/bin/cat", str(self.inputs)]}
        self.publish(self.landing_config)

    def local_pass(self, *arguments):
        return run(self.repo, sys.executable, "scripts/qk", "run", "--here", "gate", *arguments)

    def receipt_path(self, commit):
        return mq.remote.store(self.repo) / commit / "gate.json"

    def land(self, branch):
        identifier, = self.add(branch)
        with contextlib.redirect_stdout(io.StringIO()) as said:
            self.work()
        state = self.state(identifier)
        self.assertEqual(state["state"], "landed", said.getvalue())
        return state, said.getvalue()

    def test_a_verified_lane_is_found_by_tip_and_only_the_other_lane_runs(self):
        other = self.base / "other-gates"
        self.landing_config["lanes"]["integration"] = {
            "command": ["python3", "lane.py", str(other), str(self.base / "other-status")]}
        self.landing_config["queue"]["lanes"] = ["gate", "integration"]
        self.publish(self.landing_config)
        commit = self.commit("feature", "feature")
        self.local_pass()
        with patch.dict(os.environ, {"GIT_COMMITTER_DATE": "2025-01-02T00:00:00+0000"}):
            state, said = self.land("feature")
        self.assertNotEqual(state["landedAs"], commit)
        self.assertEqual(state["gateCandidates"]["gate"]["commit"], commit)
        self.assertEqual(state["reusedGates"], state["gateCandidates"])
        self.assertEqual(agent_results.entry_result(mq.Queue(self.repo), state)["reusedGates"], state["reusedGates"])
        self.assertIn("gate: reused the passing local run", said)
        self.assertEqual((self.gates(), other.read_text()), (1, "x"))

    def test_changed_execution_inputs_run_the_gate_normally(self):
        self.commit("feature", "feature")
        self.local_pass()
        self.inputs.write_text("toolchain 2")
        state, _ = self.land("feature")
        self.assertIn("gateCandidates", state)
        self.assertNotIn("reusedGates", state)
        self.assertEqual(self.gates(), 2)

    def test_missing_probe_does_not_stop_normal_gate(self):
        self.commit("feature", "feature")
        self.local_pass()
        self.inputs.unlink()
        state, _ = self.land("feature")
        self.assertNotIn("reusedGates", state)
        self.assertEqual(self.gates(), 2)

    def test_receipt_replaced_after_submission_runs_the_gate_normally(self):
        commit = self.commit("feature", "feature")
        self.local_pass()
        identifier, = self.add("feature")
        path = self.receipt_path(commit)
        saved = json.loads(path.read_text())
        saved["checkout"] = "a different run"
        path.write_text(json.dumps(saved))
        self.work()
        state = self.state(identifier)
        self.assertEqual(state["state"], "landed")
        self.assertNotIn("reusedGates", state)
        self.assertEqual(self.gates(), 2)

    def test_a_different_batch_tree_runs_the_gate_normally(self):
        self.commit("feature", "feature")
        self.local_pass()
        self.commit("another", "another")
        run(self.repo, "git", "push", "origin", "HEAD:main")
        state, _ = self.land("feature")
        self.assertIn("gateCandidates", state)
        self.assertNotIn("reusedGates", state)
        self.assertEqual(self.gates(), 2)

    def test_a_whole_group_can_reuse_its_member_passes(self):
        self.landing_config["lanes"]["member"] = self.landing_config["lanes"]["gate"]
        self.landing_config["lanes"]["gate"] = {"parallel": ["member"]}
        self.publish(self.landing_config)
        self.commit("feature", "feature")
        self.local_pass()
        state, _ = self.land("feature")
        self.assertIn("gate", state["reusedGates"])
        self.assertEqual(self.gates(), 1)

    def test_a_reused_pass_at_a_new_commit_supplies_counts_to_future_runs(self):
        self.landing_config["lanes"]["gate"]["command"] = [
            "python3", "-c", 'print("Ran 3 tests in 0.001s\\n\\nOK")']
        self.publish(self.landing_config)
        commit = self.commit("feature", "feature")
        self.local_pass()
        with patch.dict(os.environ, {"GIT_COMMITTER_DATE": "2025-01-02T00:00:00+0000"}):
            state, _ = self.land("feature")
        landed = state["landedAs"]
        self.assertNotEqual(landed, commit)
        self.assertIn("gate", state["reusedGates"])
        baseline = cli.count_history.baseline(self.repo, mq.remote.store(self.repo), "gate",
                                              offload_host.mac_name(), landed, mq.remote.HERE)
        self.assertEqual((baseline.commit, baseline.executed), (landed, 3))
        self.assertFalse(self.receipt_path(landed).exists())

    def test_the_current_count_floor_can_reject_a_passing_receipt(self):
        command = self.landing_config["lanes"]["gate"]["command"]
        command[:] = ["python3", "-c", 'import os; print("Ran %s tests in 0.001s\\n\\nOK" % os.environ["FIXTURE_COUNT"])']
        self.publish(self.landing_config)
        self.commit("feature", "feature")
        with patch.dict(os.environ, {"FIXTURE_COUNT": "1"}):
            self.local_pass()
        run(self.repo, "git", "checkout", "-q", "main")
        with patch.dict(os.environ, {"FIXTURE_COUNT": "2"}):
            self.local_pass()
            state, _ = self.land("feature")
        self.assertIn("gateCandidates", state)
        self.assertNotIn("reusedGates", state)

    def test_opt_in_must_come_from_main(self):
        self.landing_config["queue"].pop("reuseGate")
        self.publish(self.landing_config)
        self.landing_config["queue"]["reuseGate"] = {"gate": ["/bin/cat", str(self.inputs)]}
        self.commit("queuekeeper.json", "feature", json.dumps(self.landing_config))
        self.local_pass()
        state, _ = self.land("feature")
        self.assertNotIn("gateCandidates", state)
        self.assertEqual(self.gates(), 2)

    def test_a_partial_run_is_not_a_candidate(self):
        self.commit("feature", "feature")
        self.local_pass("--", str(self.base / "partial"), str(self.status))
        state, _ = self.land("feature")
        self.assertNotIn("gateCandidates", state)
        self.assertNotIn("reusedGates", state)
        self.assertEqual(self.gates(), 1)


class Landing(Fixture):
    def test_advisory_queue_read_error_preserves_wait_status(self):
        parser = argparse.ArgumentParser()
        mq.land_arguments(parser)
        for status in (0, 1, mq.resources.BUSY):
            with self.subTest(status=status), patch.object(mq, "add", return_value="entry"), \
                    patch.object(mq, "wait", return_value=status), \
                    patch.object(mq.Queue, "all", side_effect=OSError("cannot read entries")), \
                    contextlib.redirect_stdout(io.StringIO()) as said:
                self.assertEqual(mq.land(self.repo, parser.parse_args(["--wait"])), status)
                self.assertIn("warning: could not read queue entries for landing budget checks: cannot read entries",
                              said.getvalue())

    def test_the_log_says_where_each_lane_ran_as_eng_run_chose(self):
        """The queue's lanes go where qk run sends every lane, by each Mac's capacity (vocaro-engineering#168), and
        its log says where each ran and why: here, by the receipt's reason; on another Mac, by its dispatch."""
        self.commit("a", "a")
        [identifier] = self.add("a")
        with contextlib.redirect_stdout(io.StringIO()) as said:
            self.assertEqual(self.work(), 0)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertIn("gate exited 0 on this Mac (the farm has no other Mac that takes work now)", said.getvalue())
        # A dispatch record of a run on another Mac names it; a receipt an earlier run wrote here does not.
        worker = mq.Worker.__new__(mq.Worker)
        worker.checkout = self.base / "checkout"
        receipt = worker.checkout / ".build/queuekeeper/gate/source.json"
        receipt.parent.mkdir(parents=True)
        receipt.write_text(json.dumps({"ranOn": "this Mac", "hereBecause": "another Mac is loaded (load 719.0, at or "
                                                                            "above its HEAVY_LOAD_LIMIT=7.5)"}))
        worker.ran_on = None
        self.assertEqual(worker.placed("gate", 0),
                         " on this Mac (another Mac is loaded (load 719.0, at or above its HEAVY_LOAD_LIMIT=7.5))")
        self.assertEqual(worker.placed("gate", time.time() + 60), "")
        worker.ran_on = "build-host"
        self.assertEqual(worker.placed("gate", 0), " on build-host")
        worker.ran_on = None
        receipt.write_text("{")
        self.assertEqual(worker.placed("gate", 0), "")
        receipt.write_text(json.dumps({"ranOn": "second Mac"}))
        self.assertEqual(worker.placed("gate", 0), "")

    def test_a_gate_sees_sigint_at_its_default_from_a_worker_started_as_a_background_job(self):
        """A non-interactive shell starts `qk land &` with SIGINT and SIGQUIT ignored, and its worker passed that to
        every gate lane until the worker put back the defaults (vocaro/BreezEPUB#47). Without that reset, the
        negative control, the gate's command sees them ignored."""
        record = self.base / "signals"
        (self.repo / "signals.py").write_text(
            "import signal, sys\nwith open(sys.argv[1], 'a') as out:\n"
            "    out.write(' '.join('ignored' if signal.getsignal(n) == signal.SIG_IGN else 'default'\n"
            "                       for n in (signal.SIGINT, signal.SIGQUIT)) + '\\n')\n")
        run(self.repo, "git", "add", "signals.py")
        self.publish({"schemaVersion": 1, "name": "fixture", "integration": "direct-main",
                      "lanes": {"gate": {"command": ["python3", "signals.py", str(record)]}}})
        before = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGQUIT)}
        for number, handler in before.items(): self.addCleanup(signal.signal, number, handler)
        for reset, seen in ((contextlib.nullcontext(), "default default"),
                            (patch.object(mq.processes, "default_background_signals"), "ignored ignored")):
            with self.subTest(seen=seen):
                for number in before: signal.signal(number, signal.SIG_IGN)
                record.unlink(missing_ok=True)
                self.commit(f"change-{seen[0]}", f"change-{seen[0]}")
                [identifier] = self.add(f"change-{seen[0]}")
                with reset, contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(self.work(), 0)
                self.assertEqual(self.state(identifier)["state"], "landed")
                self.assertEqual(record.read_text(), seen + "\n")

    def test_one_gate_lands_a_whole_batch(self):
        self.commit("a", "a"); self.commit("b", "b"); self.commit("c", "c")
        ids = self.add("a", "b", "c")
        self.assertEqual(self.work(), 0)
        self.assertEqual([self.state(i)["state"] for i in ids], ["landed"] * 3)
        self.assertEqual(self.gates(), 1)
        self.assertLessEqual({"a", "b", "c"}, self.main_files())
        # Not one merge, and each change its own commit, with its message.
        log = run(self.repo, "git", "log", "--format=%s", "origin/main").splitlines()
        self.assertEqual(log[:3], ["Add c", "Add b", "Add a"])
        self.assertEqual(run(self.repo, "git", "rev-list", "--merges", "origin/main"), "")
        # What was pushed is what was tested: nothing else moved, and the ref is gone.
        self.assertEqual(run(self.repo, "git", "for-each-ref", "refs/queuekeeper/queue"), "")

    def test_the_branch_moving_after_add_changes_nothing(self):
        self.commit("a", "a")
        [identifier] = self.add("a")
        self.commit("later", "a", start="a")
        self.work()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertNotIn("later", self.main_files())

    def test_a_bad_commit_comes_back_and_the_rest_land(self):
        self.commit("a", "a"); self.commit("bad", "bad"); self.commit("c", "c"); self.commit("d", "d")
        ids = self.add("a", "bad", "c", "d")
        self.assertEqual(self.work(), 0)
        states = [self.state(i)["state"] for i in ids]
        self.assertEqual(states, ["landed", "failed", "landed", "landed"])
        self.assertNotIn("bad", self.main_files())
        self.assertLessEqual({"a", "c", "d"}, self.main_files())
        failed = self.state(ids[1])
        self.assertIn("bad: ['bad']", Path(failed["log"]).read_text())
        # all four fail, [a bad] fail, [a] passes, [bad] fails, [bad] fails on the main before a landed too, so it
        # comes back alone (vocaro/queuekeeper#263), then [c d] pass: not one gate per commit plus.
        self.assertEqual(self.gates(), 6)
        self.assertNotIn("conflictWith", failed)
        self.assertTrue(run(self.repo, "git", "rev-parse", "--verify", f"refs/queuekeeper/queue/{ids[1]}"))

    def test_two_that_pass_alone_and_fail_together(self):
        """One half at a time, left passes and lands before right gates, which then fails alone on a main that holds
        left; it passes on the main before left landed, so it comes back naming left, landed (vocaro/queuekeeper#263):
        the pair, left, right, and right on the main before left, 4 gates."""
        self.commit("left", "left"); self.commit("right", "right")
        ids = self.add("left", "right")
        with contextlib.redirect_stdout(io.StringIO()) as said:
            self.work()
        left, right = (self.state(i) for i in ids)
        self.assertEqual([left["state"], right["state"]], ["landed", "failed"])
        self.assertNotIn("right", self.main_files())
        self.assertEqual(right["conflictWith"], [{"id": ids[0], "sha": left["tip"], "state": "landed"}])
        self.assertIn(f"which now holds {ids[0]} at {left['tip']}, landed in the same bisect, though this commit passes "
                      "on ", right["why"])
        self.assertIn("Rebase onto origin/main", right["why"])
        self.assertIn(f"{ids[1]} sent back naming 1 landed entry after 1 probe", said.getvalue())
        self.assertEqual(self.gates(), 4)
        result = agent_results.lookup(mq.Queue(self.repo), ids[1])
        self.assertEqual(result["failure"]["category"], "fails-together")
        self.assertEqual(result["recovery"]["conflictWith"], right["conflictWith"])

    def test_of_several_landed_the_one_that_breaks_a_later_entry_is_named(self):
        """[a left right] fail; one half at a time, a and then left land, and right fails alone. It passes on the main
        before both, so the search of the landed drops a, and right names left alone (vocaro/queuekeeper#263)."""
        for name in ("a", "left", "right"): self.commit(name, name)
        ids = self.add("a", "left", "right")
        with contextlib.redirect_stdout(io.StringIO()) as said:
            self.work()
        a, left, right = (self.state(i) for i in ids)
        self.assertEqual([a["state"], left["state"], right["state"]], ["landed", "landed", "failed"], said.getvalue())
        self.assertEqual(right.get("conflictWith"), [{"id": ids[1], "sha": left["tip"], "state": "landed"}],
                         said.getvalue())
        # right on the main before both, then [left right] there: a dropped with one probe.
        self.assertIn(f"{ids[2]} sent back naming 1 landed entry after 2 probes", said.getvalue())

    def test_a_conflict_comes_back_without_costing_the_others_a_gate(self):
        self.commit("same", "one", "one"); self.commit("same", "two", "two"); self.commit("c", "c")
        ids = self.add("one", "two", "c", checked=False)
        self.work()
        self.assertEqual([self.state(i)["state"] for i in ids], ["landed", "conflict", "landed"])
        self.assertIn("same", self.state(ids[1])["why"])
        # Its author learns which entry to wait for before rebasing (#130), and that it alone conflicts (#247).
        self.assertIn(f"conflicts with {ids[0]}, ahead of it, not with origin/main, in same: once it lands, rebase",
                      self.state(ids[1])["why"])
        self.assertNotIn(ids[2], self.state(ids[1])["why"])
        self.assertEqual(self.gates(), 1)

    def test_a_conflict_with_main_alone_names_no_entry(self):
        start = run(self.repo, "git", "rev-parse", "origin/main")
        self.commit("same", "one", "one")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        self.commit("same", "two", "two", start)
        identifier, = self.add("two", checked=False)
        self.work()
        why = self.state(identifier)["why"]
        self.assertIn("conflicts with origin/main, in same: rebase onto origin/main", why)
        self.assertNotIn("once those land", why)

    def test_a_conflict_with_main_behind_an_entry_that_does_not_touch_its_file_names_main(self):
        """Negative control of #247: an entry ahead that changes other files is not what it conflicts with."""
        start = run(self.repo, "git", "rev-parse", "origin/main")
        self.commit("same", "one", "one")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        self.commit("c", "c")
        self.commit("same", "two", "two", start)
        ids = self.add("c", "two", checked=False)
        self.work()
        why = self.state(ids[1])["why"]
        self.assertEqual([self.state(i)["state"] for i in ids], ["landed", "conflict"])
        self.assertIn("conflicts with origin/main, in same: rebase onto origin/main", why)
        self.assertNotIn(ids[0], why)

    def test_a_conflict_with_main_and_an_entry_ahead_names_both(self):
        """When origin/main has changed the file since the commit's parent as well as an entry ahead, both may be
        what it conflicts with, as before #247."""
        start = run(self.repo, "git", "rev-parse", "origin/main")
        self.commit("same", "main", "main")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        self.commit("same", "one", "one")
        self.commit("same", "two", "two", start)
        ids = self.add("one", "two", checked=False)
        self.work()
        why = self.state(ids[1])["why"]
        self.assertEqual([self.state(i)["state"] for i in ids], ["landed", "conflict"])
        self.assertIn(f"conflicts with what is ahead of it, origin/main and {ids[0]}, in same: once those land", why)

    def test_a_commit_main_already_holds_needs_no_gate(self):
        tip = self.commit("a", "a")
        run(self.repo, "git", "push", "-q", "origin", f"{tip}:main")
        self.commit("b", "b")
        with patch("builtins.print"):
            self.assertIsNone(mq.add(self.repo, "a"))
        self.assertEqual(mq.Queue(self.repo).all(), [])

    REFUSED = "is a merge, which a queue would have to put on main: rebase the branch's own commits onto origin/main"

    def refused(self, branch):
        """What qk land refuses branch with, having queued nothing."""
        with self.assertRaises(ValueError) as caught, patch("builtins.print"):
            mq.add(self.repo, branch)
        self.assertEqual(mq.Queue(self.repo).all(), [])
        return str(caught.exception)

    def test_a_merge_that_brings_in_another_branchs_commits_is_refused_naming_them(self):
        """A merge of another branch's unlanded work stays refused (#217), and the refusal names what that merge
        brings in, not the branch's own commit or what main holds, and says how to carry it (#262)."""
        other = self.commit("a", "a")
        second = self.commit("a2", "a", start="a")
        own = self.commit("b", "b", start="origin/main")
        run(self.repo, "git", "merge", "-q", "--no-edit", "a")
        merge = run(self.repo, "git", "rev-parse", "HEAD")
        said = self.refused("b")
        self.assertEqual(said, f"{merge[:12]} is a merge that brings in 2 commits origin/main lacks: {other[:12]} Add a; "
                               f"{second[:12]} Add a2. A queue would have to put it on main: take it off the branch and "
                               "cherry-pick those commits onto it, or wait until their own branch lands and merge "
                               "origin/main, and add the branch again")
        self.assertNotIn(own[:12], said)

    def test_a_long_list_of_merged_in_commits_is_bounded_and_counted(self):
        tips = [self.commit("a0", "a")]
        for number in range(1, mq.MERGED_IN_LISTED + 2):
            tips.append(self.commit(f"a{number}", "a", start="a"))
        self.commit("b", "b", start="origin/main")
        run(self.repo, "git", "merge", "-q", "--no-edit", "a")
        said = self.refused("b")
        self.assertIn(f"brings in {mq.MERGED_IN_LISTED + 2} commits origin/main lacks", said)
        self.assertIn("; and 2 more not listed here. A queue", said)
        self.assertEqual([tip[:12] in said for tip in tips], [True] * mq.MERGED_IN_LISTED + [False, False])

    def test_two_merges_name_what_each_brings_in_once(self):
        """The second merge's branch holds the first's commit, which the refusal lists once."""
        first = self.commit("a", "a")
        second = self.commit("c", "c", start="a")
        self.commit("b", "b", start="origin/main")
        run(self.repo, "git", "merge", "-q", "--no-edit", "a")
        one = run(self.repo, "git", "rev-parse", "HEAD")
        run(self.repo, "git", "merge", "-q", "--no-edit", "c")
        two = run(self.repo, "git", "rev-parse", "HEAD")
        self.assertIn(f"{two[:12]}, {one[:12]} are merges that bring in 2 commits origin/main lacks: "
                      f"{first[:12]} Add a; {second[:12]} Add c. A queue would have to put them on main",
                      self.refused("b"))

    def test_a_merge_of_a_branch_main_holds_under_another_sha_keeps_the_rebase_advice(self):
        """A negative control: the queue lands a commit under another SHA, so merging the landed branch brings in no
        patch main lacks, and is still refused. Cherry-picking would add nothing, so the refusal does not say to."""
        start = run(self.repo, "git", "rev-parse", "origin/main")
        self.commit("elsewhere", "main-work")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        self.commit("a", "a", start=start)
        self.commit("elsewhere2", "landed")
        run(self.repo, "git", "cherry-pick", "a")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        self.commit("b", "b", start="origin/main")
        run(self.repo, "git", "merge", "-q", "--no-edit", "a")
        said = self.refused("b")
        self.assertRegex(said, self.REFUSED)
        self.assertNotIn("cherry-pick", said)

    def synced(self, branch, name, content="x"):
        """branch with a commit adding name, refreshed by merging origin/main after main moves, as the app's
        sync does, and then a commit after the merge. Returns the branch's tip and the merge."""
        self.commit(name, branch, content)
        self.commit("elsewhere", "main-work", start="origin/main")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        run(self.repo, "git", "checkout", "-q", branch)
        run(self.repo, "git", "fetch", "-q", "origin")
        run(self.repo, "git", "merge", "-q", "--no-edit", "origin/main")
        merge = run(self.repo, "git", "rev-parse", "HEAD")
        self.commit("after", branch, start=branch)
        return run(self.repo, "git", "rev-parse", "HEAD"), merge

    def test_a_branch_refreshed_by_merging_main_lands_linear(self):
        """The app's sync merges origin/main into a session's branch: qk land replays the branch's own commits
        onto origin/main, lands them with no merge, and leaves the branch alone, whose work the landing check
        then finds on main (#217)."""
        tip, merge = self.synced("b", "b")
        with patch.object(mq.Queue, "start"), patch("builtins.print") as said:
            identifier = mq.add(self.repo, "b")
        lines = [" ".join(map(str, call.args)) for call in said.call_args_list]
        self.assertEqual(len([line for line in lines if f"dropped {merge[:12]}, a merge of origin/main" in line]), 1)
        entry = self.state(identifier)
        self.assertEqual((entry["replays"], entry["branch"], len(entry["commits"])), (tip, "b", 2))
        self.assertEqual(run(self.repo, "git", "rev-parse", "b"), tip)
        self.assertEqual(self.work(), 0)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertLessEqual({"b", "after", "elsewhere"}, self.main_files())
        self.assertEqual(run(self.repo, "git", "rev-list", "--merges", "origin/main"), "")
        self.assertEqual(run(self.repo, "git", "log", "--format=%s", "-3", "origin/main").splitlines(),
                         ["Add after", "Add b", "Add elsewhere"])
        # Closeout and qk disk's proof: the branch, merge and all, is on main as equal patches.
        self.assertEqual(mq.landing_proofs.contained(lambda *words: mq.git(self.repo, *words), "origin/main", tip),
                         mq.landing_proofs.PATCH_EQUIVALENT)

    def test_a_refreshed_branch_whose_replay_conflicts_is_refused(self):
        """A negative control: main changes the branch's file after the sync, so its commits do not replay."""
        self.synced("b", "same", "branch")
        self.commit("same", "main-again", "main", start="origin/main")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        said = self.refused("b")
        self.assertRegex(said, self.REFUSED)
        self.assertNotIn("cherry-pick", said)
        self.assertEqual(run(self.repo, "git", "worktree", "list", "--porcelain").count("worktree "), 1)

    def test_a_sync_that_settled_a_conflict_is_refused(self):
        """A negative control: a merge of main whose tree no clean merge makes carries a change of its own, which
        neither the replay nor the landing check could see."""
        start = run(self.repo, "git", "rev-parse", "origin/main")
        self.commit("same", "main-work", "main")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        self.commit("same", "b", "branch", start=start)
        subprocess.run(["git", "merge", "-q", "--no-edit", "origin/main"], cwd=self.repo, capture_output=True)
        (self.repo / "same").write_text("both")
        run(self.repo, "git", "commit", "-qam", "Merge origin/main")
        tip = run(self.repo, "git", "rev-parse", "HEAD")
        said = self.refused("b")
        self.assertRegex(said, self.REFUSED)
        self.assertNotIn("cherry-pick", said)
        self.assertIsNone(mq.landing_proofs.contained(lambda *words: mq.git(self.repo, *words), "origin/main", tip))

    def test_adding_a_branch_again_replaces_it(self):
        self.commit("a", "a")
        [first] = self.add("a")
        self.commit("a2", "a", start="a")
        with patch.object(mq.Queue, "start"), patch("builtins.print"):
            mq.add(self.repo, "a")
        self.assertEqual(self.state(first)["state"], "removed")
        self.work()
        self.assertLessEqual({"a", "a2"}, self.main_files())
        self.assertEqual(self.gates(), 1)


# A gate lane that runs LANE's gate, then, when the tree it tests has a file named rewrite, rewrites the tracked
# AGENTS.md, as a formatter would, and leaves an untracked build product either way, which is no change.
REWRITING = """import pathlib, subprocess, sys
subprocess.run([sys.executable, "lane.py", *sys.argv[1:]], check=True)
pathlib.Path("product.o").write_text("built")
if pathlib.Path("rewrite").exists(): pathlib.Path("AGENTS.md").write_text("# Formatted\\n")
"""


class LaneChangesItsTree(Fixture):
    """A lane that passes but changes the tracked tree it tested sends the batch back, as the queue pushes the
    commit it built, not the tree the lane leaves; files Git does not track are no change."""
    def setUp(self):
        super().setUp()
        (self.repo / "rewriting.py").write_text(REWRITING)
        (self.repo / ".gitignore").write_text("product.o\n")
        run(self.repo, "git", "add", "rewriting.py", ".gitignore")
        config = self.config()
        config["lanes"]["gate"]["command"] = ["python3", "rewriting.py", str(self.counter), str(self.status)]
        self.publish(config, "Gate through rewriting.py")

    def test_a_lane_that_rewrites_a_tracked_file_sends_its_entry_back_and_the_rest_land(self):
        self.commit("a", "a"); self.commit("rewrite", "rewrite"); self.commit("c", "c")
        ids = self.add("a", "rewrite", "c")
        with contextlib.redirect_stdout(io.StringIO()) as said:
            self.work()
        self.assertEqual([self.state(i)["state"] for i in ids], ["landed", "failed", "landed"])
        why = self.state(ids[1])["why"]
        self.assertIn("gate passed but changed the tracked files it tested: AGENTS.md.", why)
        self.assertNotIn("product.o", why)
        self.assertNotIn("rerunning", said.getvalue())
        self.assertIn("gate changed the tree it tested, which the same tree does again: no rerun", said.getvalue())
        # origin/main, without the batch, leaves its tree as it was, so the batch's commits are why (#264).
        self.assertIn("on origin/main at", said.getvalue())
        self.assertIn("without changing its tree: the batch's commits change it", said.getvalue())
        self.assertIsNone(mq.Queue(self.repo).held())
        self.assertNotIn("rewrite", self.main_files())
        run(self.repo, "git", "fetch", "-q", "origin")
        self.assertEqual(run(self.repo, "git", "show", "origin/main:AGENTS.md"), "# Fixture")

    def test_a_lane_that_changes_main_itself_holds_and_sends_no_entry_back(self):
        """vocaro/queuekeeper#264: when the lane changes origin/main's tree too, without the batch, the change is
        main's, so the queue holds for a session with every entry waiting, and a change that fixes main lands them."""
        (self.repo / "rewrite").write_text("x")
        run(self.repo, "git", "add", "rewrite")
        self.publish(json.loads((self.repo / "queuekeeper.json").read_text()), "Rewrite AGENTS.md in the gate")
        base = run(self.repo, "git", "rev-parse", "HEAD")
        self.commit("a", "a"); self.commit("c", "c")
        ids = self.add("a", "c")
        with contextlib.redirect_stdout(io.StringIO()) as said:
            self.work()
        self.assertEqual([self.state(i)["state"] for i in ids], ["queued", "queued"])
        self.assertFalse(any(self.state(i).get("why") for i in ids))
        self.assertIn("gate changes origin/main's tree too, without the batch: no entry is sent back", said.getvalue())
        held = mq.Queue(self.repo).held()
        self.assertIn(f"for a session, not the owner: gate changes the tracked files it tests on origin/main at "
                      f"{base[:12]} itself, without any entry: AGENTS.md. ", held)
        self.assertIn("so none is sent back", held)
        self.assertIn("-origin-main-gate.log", held)
        self.assertNotIn("bisect", said.getvalue())
        # A change that leaves main as the lane leaves it, queued and resumed, lands with the rest.
        self.commit("AGENTS.md", "fix", content="# Formatted\n")
        ids += self.add("fix")
        mq.Queue(self.repo).hold_file.unlink()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.work(), 0)
        self.assertEqual([self.state(i)["state"] for i in ids], ["landed", "landed", "landed"])

    def test_untracked_products_are_no_change(self):
        self.commit("a", "a")
        [identifier] = self.add("a")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.work(), 0)
        self.assertEqual(self.state(identifier)["state"], "landed")

    def test_what_altered_finds_here_and_on_another_mac(self):
        """altered names staged and unstaged edits and deletions, and a moved HEAD, as offload_host.altered does in
        another Mac's checkout, whose finding a dispatch's receipt carries back (altered_remotely)."""
        head = run(self.repo, "git", "rev-parse", "HEAD")
        for find in (mq.altered, lambda checkout, commit: offload_host.altered(checkout, commit)):
            with self.subTest(find=find):
                self.assertEqual(find(self.repo, head), [])
                (self.repo / "product.o").write_text("built")
                (self.repo / "untracked").write_text("new")
                self.assertEqual(find(self.repo, head), [])
                (self.repo / "AGENTS.md").write_text("changed\n")
                (self.repo / "lane.py").unlink()
                (self.repo / "rewriting.py").write_text("staged\n")
                run(self.repo, "git", "add", "rewriting.py")
                self.assertEqual(find(self.repo, head), ["AGENTS.md", "lane.py", "rewriting.py"])
                run(self.repo, "git", "commit", "-qam", "moved")
                self.assertEqual(find(self.repo, head), ["HEAD", "AGENTS.md", "lane.py", "rewriting.py"])
                run(self.repo, "git", "reset", "-q", "--hard", head)
                (self.repo / "untracked").unlink()
        dispatch = self.base / "dispatch"
        dispatch.mkdir()
        self.assertIn("could not be read", mq.altered_remotely(dispatch))
        for receipt, found in (({"result": {"exitStatus": 0}}, []),
                               ({"result": {"altered": ["HEAD", "a.py"]}}, ["HEAD", "a.py"]),
                               ({"result": {"altered": {"error": "git diff failed"}}}, "git diff failed")):
            with self.subTest(receipt=receipt):
                (dispatch / "source.json").write_text(json.dumps(receipt))
                self.assertEqual(mq.altered_remotely(dispatch), found)
        self.assertEqual(mq.altered_words("gate", [f"f{n}" for n in range(12)], " on build-host"),
                         "gate passed but changed the tracked files it tested on build-host: f0, f1, f2, f3, f4, f5, "
                         "f6, f7, f8, f9 and 2 more. The queue lands the commit it built, not the tree a lane leaves, "
                         "so a lane must leave tracked files as it found them")

    def test_a_lane_on_another_mac_that_changed_its_checkout_fails_the_gate(self):
        """The receipt of the lane's dispatch says what it changed there, which fails the gate as a change here does."""
        worker = mq.Worker.__new__(mq.Worker)
        worker.checkout, worker.ran_on, worker.altered_files = self.repo, "build-host", None
        worker.say = lambda line: None
        head = run(self.repo, "git", "rev-parse", "HEAD")
        dispatch, log = self.base / "dispatch", self.base / "gate.log"
        dispatch.mkdir()
        (dispatch / "source.json").write_text(json.dumps({"result": {"altered": []}}))
        self.assertEqual(worker.unaltered("gate", head, log, dispatch), 0)
        self.assertIsNone(worker.altered_files)
        (dispatch / "source.json").write_text(json.dumps({"result": {"altered": ["Sources/App.swift"]}}))
        self.assertEqual(worker.unaltered("gate", head, log, dispatch), mq.ALTERED)
        self.assertIn("changed the tracked files it tested on build-host: Sources/App.swift.", worker.altered_files)
        self.assertIn(worker.altered_files, log.read_text())

    def test_queue_remove_says_it_stops_a_gate_in_progress(self):
        parser = argparse.ArgumentParser()
        mq.add_arguments(parser)
        operations = next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction))
        said = next(choice.help for choice in operations._choices_actions if choice.dest == "remove")
        self.assertIn("stops within seconds", said)
        self.assertNotIn("finishes", said)


class CheckedWhenQueued(Fixture):
    """qk land's trial integration finds a conflict with the entries ahead when the entry is queued, not when its
    round starts (vocaro-engineering#178)."""
    def land(self, branch, **options):
        """(the new entry's ID or None, what qk land said, the error it refused with or None), no worker started."""
        said, error = io.StringIO(), None
        with patch.object(mq.Queue, "start"), contextlib.redirect_stdout(said), contextlib.redirect_stderr(said):
            try: mq.add(self.repo, branch, **options)
            except ValueError as refused: error = str(refused)
        identifier = next((e["id"] for e in reversed(mq.Queue(self.repo).all())
                           if e["branch"] == branch and e["state"] in mq.WAITING), None)
        return identifier, said.getvalue(), error

    def worktrees(self):
        return run(self.repo, "git", "worktree", "list", "--porcelain").count("worktree ")

    def test_a_conflict_with_an_entry_ahead_is_refused_and_nothing_is_queued(self):
        self.commit("same", "one", "one"); self.commit("c", "c"); self.commit("same", "two", "two")
        ahead = self.add("one", "c")
        before = self.worktrees()
        identifier, _, error = self.land("two")
        self.assertIsNone(identifier)
        self.assertEqual([e["id"] for e in mq.Queue(self.repo).all()], ahead)
        # for-each-ref sorts by name, and two entries made in one second sort by their random suffix, not their order.
        self.assertEqual(sorted(run(self.repo, "git", "for-each-ref", "--format=%(refname)", "refs/queuekeeper/queue").split()),
                         sorted(f"refs/queuekeeper/queue/{i}" for i in ahead))
        # The files and the entries: all it was checked against, in order, and the one that changes the file.
        self.assertIn("is not queued", error)
        # Only the entry that changes the file, since origin/main holds it as the commit's parent did (#247).
        self.assertIn(f"conflicts with {ahead[0]}, ahead of it, not with origin/main, in same: once it lands", error)
        self.assertIn(f"and, in order, {ahead[0]}, {ahead[1]}; {ahead[0]} changes the files in conflict", error)
        self.assertNotIn(f"{ahead[1]} change", error)
        # The scratch worktree is gone, and Git knows of no other.
        self.assertEqual(self.worktrees(), before)
        self.assertEqual(run(self.repo, "git", "worktree", "list", "--porcelain").count("locked"), 0)
        # The session's checkout is untouched: still on the branch it was, at its tip.
        self.assertEqual(run(self.repo, "git", "symbolic-ref", "--short", "HEAD"), "two")
        self.assertEqual(run(self.repo, "git", "status", "--porcelain"), "")

    def test_an_entry_that_applies_is_queued_as_now(self):
        self.commit("a", "a"); self.commit("b", "b"); self.commit("c", "c")
        ahead = self.add("a", "b")
        before = self.worktrees()
        identifier, said, error = self.land("c")
        self.assertIsNone(error)
        self.assertEqual(self.state(identifier)["state"], "queued")
        self.assertIn(f"it applies onto origin/main at", said)
        self.assertIn(f"after {ahead[0]}, {ahead[1]}, as a round would integrate it now", said)
        self.assertEqual(self.worktrees(), before)
        self.assertEqual(self.work(), 0)
        self.assertEqual([self.state(i)["state"] for i in [*ahead, identifier]], ["landed"] * 3)
        self.assertEqual(self.gates(), 1)

    def test_an_entry_that_conflicts_with_main_alone_is_refused_naming_no_entry(self):
        start = run(self.repo, "git", "rev-parse", "origin/main")
        self.commit("same", "one", "one")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        self.commit("same", "two", "two", start)
        identifier, _, error = self.land("two")
        self.assertIsNone(identifier)
        self.assertIn("conflicts with origin/main, in same: rebase onto origin/main", error)
        self.assertIn("Checked against origin/main at", error)
        self.assertNotIn("in order", error)

    def test_a_changelog_conflict_the_worker_settles_is_not_refused(self):
        start = "# Release notes\n\n## Unreleased\n\n- old\n"
        self.commit("CHANGELOG.md", "start", start)
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        for name in ("a", "b"): self.commit("CHANGELOG.md", name, start.replace("- old", f"- {name}\n- old"))
        [first] = self.add("a")
        identifier, said, error = self.land("b")
        self.assertIsNone(error)
        self.assertIn("a round would settle", said)
        self.assertIn("kept both sides of CHANGELOG.md, the new lines first", said)
        self.work()
        self.assertEqual([self.state(i)["state"] for i in (first, identifier)], ["landed"] * 2)
        run(self.repo, "git", "fetch", "-q", "origin")
        self.assertIn("- b\n- a\n- old", run(self.repo, "git", "show", "origin/main:CHANGELOG.md"))

    def test_a_changelog_conflict_the_worker_does_not_settle_is_refused(self):
        start = "# Release notes\n\n## Unreleased\n\n- old\n"
        self.commit("CHANGELOG.md", "start", start)
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        for name in ("a", "b"): self.commit("CHANGELOG.md", name, start.replace("- old", f"- {name}"))
        [first] = self.add("a")
        identifier, _, error = self.land("b")
        self.assertIsNone(identifier)
        self.assertIn(f"in CHANGELOG.md:", error)
        self.assertIn(f"{first} changes the files in conflict", error)

    def test_a_front_entry_is_checked_against_main_alone(self):
        """--front tests it alone onto main, so the entries behind it are no part of its check; the one behind it
        that its landing makes conflict comes back from its round, as before."""
        self.commit("same", "one", "one"); self.commit("same", "fix", "fix")
        [behind] = self.add("one")
        identifier, said, error = self.land("fix", front=True, why="a fix every entry needs")
        self.assertIsNone(error)
        self.assertIn(", alone, as --front tests it", said)
        self.assertNotIn(behind, said)
        self.work()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(self.state(behind)["state"], "conflict")
        # A front entry that conflicts with main itself is refused.
        start = run(self.repo, "git", "rev-parse", "origin/main~1")
        self.commit("same", "late", "late", start)
        late, _, error = self.land("late", front=True)
        self.assertIsNone(late)
        self.assertIn("conflicts with origin/main, in same", error)

    def test_the_round_still_sends_back_what_the_check_could_not_see(self):
        start = run(self.repo, "git", "rev-parse", "origin/main")
        self.commit("same", "two", "two"); self.commit("c", "c")
        ids = self.add("two", "c")
        # main moves after the check, by a hand push.
        self.commit("same", "pushed", "pushed", start)
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        self.work()
        self.assertEqual([self.state(i)["state"] for i in ids], ["conflict", "landed"])
        self.assertIn("conflicts with origin/main, in same", self.state(ids[0])["why"])

    def test_an_entry_ahead_that_a_round_would_leave_out_is_named_and_not_held_against_it(self):
        start = run(self.repo, "git", "rev-parse", "origin/main")
        self.commit("same", "ahead", "ahead")
        [ahead] = self.add("ahead")
        # main moves under the entry ahead, which its round will send back.
        self.commit("same", "pushed", "pushed", start)
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        self.commit("same", "behind", "behind")
        identifier, said, error = self.land("behind")
        self.assertIsNone(error)
        self.assertIn(f"left out of the check, as a round would leave it: {ahead}, ", said)
        self.assertIn("conflicts with origin/main, in same", said)
        # And named in a refusal too.
        self.commit("same", "third", "third")
        third, _, error = self.land("third")
        self.assertIsNone(third)
        self.assertIn(f"in order, {identifier}; {identifier} changes the files in conflict; left out, as a round "
                      f"would leave it: {ahead}", error)

    def test_the_entries_ahead_are_taken_in_the_order_rounds_land_them(self):
        entries = [{"id": "q-old", "state": "queued", "submitted": 1}, {"id": "t", "state": "testing", "submitted": 3},
                   {"id": "front", "state": "queued", "submitted": 4, "front": True},
                   {"id": "p", "state": "passed", "submitted": 5}, {"id": "q-new", "state": "queued", "submitted": 2}]
        self.assertEqual([e["id"] for e in mq.round_order(entries)], ["p", "t", "front", "q-old", "q-new"])

    def test_a_branch_added_again_is_not_checked_against_what_it_replaces(self):
        self.commit("same", "a", "one")
        [first] = self.add("a")
        self.commit("same", "a", "rewritten")
        identifier, said, error = self.land("a")
        self.assertIsNone(error)
        self.assertEqual(self.state(first)["state"], "removed")
        self.assertEqual(self.state(identifier)["state"], "queued")

    def test_a_trial_that_cannot_be_made_queues_the_entry(self):
        self.commit("a", "a")
        with patch.object(mq, "trial", side_effect=ValueError("`git worktree add` exited 128: no space")):
            identifier, said, error = self.land("a")
        self.assertIsNone(error)
        self.assertEqual(self.state(identifier)["state"], "queued")
        self.assertIn("could not check it against the entries ahead, so the round finds any conflict: `git worktree "
                      "add` exited 128: no space", said)

    def test_the_scratch_worktree_goes_when_the_trial_fails_midway(self):
        self.commit("a", "a")
        before = self.worktrees()
        with patch.object(mq.Trial, "integrate", side_effect=RuntimeError("stopped")):
            identifier, said, _ = self.land("a")
        self.assertIn("stopped", said)
        self.assertEqual(self.worktrees(), before)
        self.assertEqual(self.state(identifier)["state"], "queued")
        # Nor is its record of its maker left, once the worktree has gone (worktree_ownership, #138).
        common = Path(run(self.repo, "git", "rev-parse", "--path-format=absolute", "--git-common-dir"))
        self.assertEqual(list(mq.worktree_ownership.records(common).glob("*.json")), [])

    def test_a_worker_removes_the_scratch_worktree_a_killed_qk_land_left(self):
        common = Path(run(self.repo, "git", "rev-parse", "--path-format=absolute", "--git-common-dir"))
        maker = subprocess.Popen(["true"])
        maker.wait()
        # What a `qk land` killed inside its trial leaves: a locked worktree, and its record naming a maker now gone.
        with patch.object(mq.worktree_ownership, "maker", return_value={"pid": maker.pid, "start": "then"}):
            blocked = mq.worktree_ownership.scratch(self.repo, "refs/remotes/origin/main", "trial", mq.TRIAL_REASON,
                                                    "project")
            path = blocked.__enter__()
        records = list(mq.worktree_ownership.records(common).glob("*.json"))
        self.assertEqual(len(records), 1)
        said = []
        with patch.object(mq.worktree_ownership, "Census",
                          lambda real=mq.worktree_ownership.Census: real(take=lambda: [])):
            [result] = mq.reclaim_scratch(mq.Queue(self.repo), said.append)
        self.assertEqual(result["outcome"], "reclaimed", result)
        self.assertFalse(path.exists())
        self.assertEqual(list(mq.worktree_ownership.records(common).glob("*.json")), [])
        self.assertIn(f"scratch worktree reclaimed {path}", said[0])
        # The block's own end, had it come, finds nothing left to remove.
        blocked.__exit__(None, None, None)

    def test_eng_land_and_eng_queue_add_exit_1_for_a_refusal_as_for_an_entry_that_came_back(self):
        import argparse
        self.commit("same", "one", "one"); self.commit("same", "two", "two")
        self.add("one")
        # Without --wait: with it, a refusal for an entry ahead alone waits for that entry (#302, LandWaitsForAhead).
        for adder, words, call in ((mq.land_arguments, ["two"], mq.land),
                                   (mq.add_arguments, ["add", "two"], mq.main)):
            parser = argparse.ArgumentParser()
            adder(parser)
            said = io.StringIO()
            # A queued entry's wait would last as long as no worker runs: here it ends at once, as for a landing.
            with patch.object(mq.Queue, "start"), patch.object(mq, "wait", return_value=0), \
                    contextlib.redirect_stdout(said), contextlib.redirect_stderr(said):
                self.assertEqual(call(self.repo, parser.parse_args(words)), 1)
            self.assertIn("queuekeeper: ", said.getvalue())
            self.assertIn("is not queued", said.getvalue())
            self.assertEqual(len(mq.Queue(self.repo).all()), 1)

    def test_a_refusal_names_the_command_that_waits_for_the_entries_and_the_lines_in_conflict(self):
        """#304: the entries that change the files in conflict, in an `qk queue wait` ready to run; #302: the commit's
        own lines that conflict, from the trial. A conflict with origin/main alone, the negative control, names no
        wait, since nothing ahead is worth waiting for."""
        self.commit("same", "one", "a\nb\nc\nd\n"); self.commit("other", "o", "o\n")
        self.commit("same", "two", "a\nB\nC\nd\n")
        ahead = self.add("one", "o")
        identifier, _, error = self.land("two")
        self.assertIsNone(identifier)
        self.assertIn(f"/scripts/qk queue wait {ahead[0]}, which says whether it landed, so rebase, or came back",
                      error)
        self.assertNotIn(f"queue wait {ahead[0]} {ahead[1]}", error)
        self.assertIn("`qk land --wait` waits for it itself and checks again", error)
        # Of the file both add, only the lines that differ: its second and third.
        self.assertIn("its lines in conflict: same:2-3. ", error)
        start = run(self.repo, "git", "rev-parse", "origin/main")
        self.commit("main", "pushed", "pushed")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        self.commit("main", "late", "late", start)
        identifier, _, error = self.land("late")
        self.assertIsNone(identifier)
        self.assertIn("conflicts with origin/main, in main: rebase", error)
        self.assertIn("its lines in conflict: main:1", error)
        self.assertNotIn("queue wait", error)

    def test_conflict_hunks_are_the_commits_own_lines(self):
        checkout = self.base / "hunks"
        checkout.mkdir()
        (checkout / "f").write_text("keep\n<<<<<<< HEAD\nahead\n=======\nmine 1\nmine 2\n>>>>>>> abc (Mine)\nkeep\n"
                                    "<<<<<<< HEAD\nahead\n||||||| parent\nold\n=======\n>>>>>>> abc (Mine)\nlast\n"
                                    "<<<<<<< HEAD\n=======\nmine 3\n>>>>>>> abc (Mine)\n")
        (checkout / "g").write_text("no blocks\n")
        self.assertEqual(mq.conflict_hunks(checkout, ["f", "g", "gone"]), ["f:2-3", "f before line 5", "f:6"])


    def test_eng_land_from_another_repository_refuses_and_from_a_worktree_or_none_lands(self):
        """Each project's launcher lands its own repository: another project's qk land, run here, would
        queue that project's HEAD or report it landed, exit 0 (vocaro-engineering#210)."""
        import argparse
        parser = argparse.ArgumentParser()
        mq.land_arguments(parser)
        other = self.base / "other"
        run(self.base, "git", "init", "-q", str(other))
        worktree = self.base / "worktree"
        run(self.repo, "git", "worktree", "add", "-q", "--detach", str(worktree))
        outside = self.base / "outside"
        outside.mkdir()
        self.commit("a", "a")
        said = io.StringIO()
        with patch.object(mq.Queue, "start"), contextlib.redirect_stdout(said), contextlib.redirect_stderr(said):
            self.assertEqual(mq.land(self.repo, parser.parse_args(["a"]), other / "."), 1)
        self.assertIn(f"queuekeeper: this queuekeeper lands {self.repo}, but the current directory is in {other.resolve()}",
                      said.getvalue())
        self.assertEqual(mq.Queue(self.repo).all(), [])
        for directory in (worktree, outside, self.repo):
            with patch.object(mq.Queue, "start"), patch("builtins.print"), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(mq.land(self.repo, parser.parse_args(["a"]), directory), 0, directory)
        self.assertEqual(len(mq.Queue(self.repo).all()), 1)


class LandWaitsForAhead(Fixture):
    """`qk land --wait` refused for entries ahead alone waits for them and checks again against the whole queue
    (vocaro-engineering#302), and `qk queue wait` takes several entries (#304). No worker starts by itself: each
    wait runs the round a worker would."""
    def land(self, branch, waits=None):
        """(exit status, what qk land --wait said, the IDs each wait for entries ahead was given). waits, by the call's
        number from 1, is what to do before that wait looks; by default a worker's round."""
        import argparse
        parser = argparse.ArgumentParser()
        mq.land_arguments(parser)
        real_wait, real_wait_for, given = mq.wait, mq.wait_for, []
        def wait_for(root, identifiers, limit=mq.WAIT_LIMIT):
            given.append(list(identifiers))
            (waits or {}).get(len(given), self.work)()
            return real_wait_for(root, identifiers, limit)
        def wait(root, identifier, limit=mq.WAIT_LIMIT):
            self.work()
            return real_wait(root, identifier, limit)
        said = io.StringIO()
        with patch.object(mq.Queue, "start"), patch.object(mq, "wait_for", wait_for), patch.object(mq, "wait", wait), \
                self.polls(lambda: self.fail("a wait polled: nothing would end it")), \
                contextlib.redirect_stdout(said), contextlib.redirect_stderr(said):
            status = mq.land(self.repo, parser.parse_args([branch, "--wait"]))
        return status, said.getvalue(), given

    @staticmethod
    def polls(step):
        """A wait's poll, a sleep of mq.POLL, runs step instead; every other sleep, as subprocess's own, is real."""
        real = time.sleep
        return patch.object(mq.time, "sleep", lambda seconds: step() if seconds == mq.POLL else real(seconds))

    def test_landing_budget_warning_reads_main_after_a_new_lane_key_lands(self):
        config = self.config()
        config["lanes"]["device-voiceover"] = {
            "command": ["true"], "devices": ["iPad"], "here": "the fixture device", "compileFirst": ["true"]}
        config["release"] = {"lanes": ["device-voiceover"], "maxCommits": 1}
        self.commit("fix", "fix")
        real_wait = mq.wait

        def wait(root, identifier, limit):
            result = real_wait(root, identifier, limit)
            run(self.repo, "git", "checkout", "-q", "--detach", "origin/main")
            self.publish(config, "Add a release lane with a new key")
            with self.assertRaisesRegex(ValueError, "Invalid lane device-voiceover"):
                mq.config_at(root, mq.MAIN)
            return result

        # What the project's tooling warns of after a landing reads the configuration main holds.
        warned = lambda root, read: [f"release lanes on main: {', '.join(read['release']['lanes'])}"]
        with patch.object(mq, "wait", wait), patch.object(mq.budgets.plugins, "LANDING_WARNINGS", [warned]), \
                patch.object(configuration, "LANE_KEYS", configuration.LANE_KEYS - {"compileFirst"}):
            result, said, _ = self.land("fix")
        self.assertEqual(result, 0, said)
        self.assertIn("landed:", said)
        self.assertIn("release lanes on main: device-voiceover", said)

    def test_behind_an_entry_in_its_gate_that_fails_it_is_queued_and_lands_without_a_second_command(self):
        self.commit("same", "one", "one"); self.commit("bad", "one", "bad", start="one")
        self.commit("same", "two", "two")
        [ahead] = self.add("one")
        mq.Queue(self.repo).mark([self.state(ahead)], mq.TESTING)
        status, said, given = self.land("two")
        self.assertEqual(status, 0, said)
        self.assertEqual(given, [[ahead]])
        self.assertIn(f"conflicts with {ahead}, ahead of it, not with origin/main", said)
        self.assertIn(f"qk land --wait: waits for {ahead}, then checks again against the whole queue", said)
        self.assertIn(f"{ahead} came back", said)
        self.assertEqual(self.state(ahead)["state"], "failed")
        mine = next(e for e in mq.Queue(self.repo).all() if e["branch"] == "two")
        self.assertEqual(mine["state"], "landed")
        self.assertIn(f"landed: {mine['landedAs']}", said)

    def test_behind_an_entry_that_lands_and_still_conflicts_it_is_refused_saying_to_rebase(self):
        """The negative control: once the entry ahead lands, the conflict is with origin/main, which no wait ends."""
        self.commit("same", "one", "one"); self.commit("same", "two", "two")
        [ahead] = self.add("one")
        status, said, given = self.land("two")
        self.assertEqual(status, 1, said)
        self.assertEqual(given, [[ahead]])
        self.assertEqual(self.state(ahead)["state"], "landed")
        self.assertIn("all landed: rebase onto origin/main", said)
        self.assertIn("conflicts with origin/main, in same: rebase onto origin/main and add it again", said)
        self.assertFalse(any(e["branch"] == "two" for e in mq.Queue(self.repo).all()))

    def test_a_conflict_with_origin_main_is_refused_at_once_with_no_wait(self):
        start = run(self.repo, "git", "rev-parse", "origin/main")
        self.commit("same", "one", "one")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        self.commit("same", "two", "two", start)
        status, said, given = self.land("two")
        self.assertEqual((status, given), (1, []), said)
        self.assertNotIn("qk land --wait: waits", said)

    def test_each_wake_checks_against_the_whole_queue_and_waits_for_an_entry_queued_meanwhile(self):
        """#302's second case: while it waited, another entry touching the same lines was queued; the check after the
        wake names it, and the wait goes on for it."""
        self.commit("same", "one", "one"); self.commit("same", "two", "two"); self.commit("same", "three", "three")
        [first] = self.add("one")
        meanwhile = []
        def first_wait():
            mq.Queue(self.repo).settle(self.state(first), mq.FAILED, why="its gate failed")
            meanwhile.extend(self.add("three", checked=False))
        status, said, given = self.land("two", waits={1: first_wait})
        self.assertEqual(given, [[first], meanwhile])
        self.assertEqual(self.state(meanwhile[0])["state"], "landed")
        # And once that one landed, its conflict is with origin/main: rebase.
        self.assertEqual(status, 1, said)
        self.assertIn("conflicts with origin/main, in same: rebase", said)

    def test_eng_queue_wait_takes_several_entries_and_a_return_to_queued_keeps_it_going(self):
        self.commit("a", "a"); self.commit("bad", "b")
        ids = self.add("a", "b")
        queue = mq.Queue(self.repo)
        # Each poll moves the queue one step: a gate starts, a --front entry sends it back to queued, a round settles.
        steps = [lambda: queue.mark([self.state(ids[0])], mq.TESTING),
                 lambda: queue.mark([self.state(ids[0])], mq.QUEUED), self.work]
        slept = []
        def poll():
            slept.append(mq.POLL)
            steps[len(slept) - 1]()
        import argparse
        parser = argparse.ArgumentParser()
        mq.add_arguments(parser)
        said = io.StringIO()
        with patch.object(mq.Queue, "start"), self.polls(poll), contextlib.redirect_stdout(said):
            status = mq.main(self.repo, parser.parse_args(["wait", *ids]))
        self.assertEqual(len(slept), 3)
        self.assertEqual(status, 1, said.getvalue())
        self.assertEqual([self.state(i)["state"] for i in ids], ["landed", "failed"])
        self.assertIn(f"{ids[1]} came back, so a commit that conflicted with it may apply as it is", said.getvalue())
        # Each landed: 0, saying to rebase. An unknown ID is refused before any wait.
        self.commit("c", "c"); self.commit("d", "d")
        more = self.add("c", "d")
        with patch.object(mq.Queue, "start"), self.polls(self.work), \
                contextlib.redirect_stdout(io.StringIO()) as landed:
            self.assertEqual(mq.wait_for(self.repo, more), 0)
        self.assertIn("all landed: rebase onto origin/main", landed.getvalue())
        with self.assertRaisesRegex(ValueError, "The queue has no entry nobody"):
            mq.wait_for(self.repo, [more[0], "nobody"])
        # Held, while one still waits: 75.
        self.commit("e", "e")
        [held] = self.add("e")
        mq.Queue(self.repo).hold("for a test")
        with patch.object(mq.Queue, "start"), contextlib.redirect_stdout(io.StringIO()) as waiting:
            self.assertEqual(mq.wait_for(self.repo, [held, more[0]]), mq.resources.BUSY)
        self.assertIn(f"{held} (queued) waiting, and the queue is held", waiting.getvalue())


ISOLATED ="import unittest\nimport host_isolation\n\nclass T(unittest.TestCase):\n    def test_t(self): pass\n"
UNISOLATED = "import unittest\n\nclass T(unittest.TestCase):\n    def test_t(self): pass\n"
# What the gate of 2026-11-07 was refused with, in the checkout it ran in (vocaro-engineering#280, #290).
REFUSED_LINE = ("queuekeeper unittest: test_forgot.py must import host_isolation at its top level, as every test module beside "
                "/queue/checkout/tests/host_isolation.py does; nothing ran")


class UnittestGate(Fixture):
    """A project whose gate is qk unittest over tests/, which keeps its tests off the real hosts in host_isolation.py,
    as the toolkit's does. No test here runs a gate: qk land's checks and `qk queue doctor` read trees and logs."""
    def setUp(self):
        super().setUp()
        config = self.config()
        config["lanes"]["gate"] = {"command": ["python3", "scripts/qk", "unittest", "-s", "tests", "--split", "test",
                                               "-v"]}
        (self.repo / "tests").mkdir()
        (self.repo / "tests/host_isolation.py").write_text("")
        (self.repo / "tests/test_ok.py").write_text(ISOLATED)
        run(self.repo, "git", "add", "tests")
        self.publish(config, "Gate on qk unittest")

    def land(self, branch, **options):
        """(the new entry's ID or None, what qk land said, the error it refused with or None), no worker started."""
        said, error = io.StringIO(), None
        with patch.object(mq.Queue, "start"), contextlib.redirect_stdout(said), contextlib.redirect_stderr(said):
            try: mq.add(self.repo, branch, **options)
            except mq.Refused as refused: error = str(refused)
        identifier = next((e["id"] for e in reversed(mq.Queue(self.repo).all())
                           if e["branch"] == branch and e["state"] in mq.WAITING), None)
        return identifier, said.getvalue(), error


class CheckedBeforeRunning(UnittestGate):
    """qk land and `qk queue add` run qk unittest's checks before a run on the entry's tree, and refuse an entry
    that a gate would refuse in seconds having run nothing (vocaro-engineering#290)."""
    def test_a_module_without_host_isolation_is_refused_with_the_refusal_line_and_nothing_is_queued(self):
        self.commit("tests/test_forgot.py", "forgot", UNISOLATED)
        identifier, _, error = self.land("forgot")
        self.assertIsNone(identifier)
        self.assertEqual(mq.Queue(self.repo).all(), [])
        self.assertEqual(run(self.repo, "git", "for-each-ref", "refs/queuekeeper/queue"), "")
        self.assertIn("is not queued: qk unittest would refuse every gate holding it before running anything", error)
        self.assertIn(f"queuekeeper unittest: test_forgot.py must import host_isolation at its top level, as every test module "
                      f"beside {self.repo}/tests/host_isolation.py does; nothing ran", error)

    def test_eng_land_says_the_refusal_and_exits_1(self):
        import argparse
        parser = argparse.ArgumentParser()
        mq.land_arguments(parser)
        self.commit("tests/test_forgot.py", "forgot", UNISOLATED)
        said = io.StringIO()
        with patch.object(mq.Queue, "start"), contextlib.redirect_stdout(said), contextlib.redirect_stderr(said):
            self.assertEqual(mq.land(self.repo, parser.parse_args(["forgot"]), self.repo), 1)
        self.assertIn("queuekeeper: ", said.getvalue())
        self.assertIn("test_forgot.py must import host_isolation", said.getvalue())
        self.assertEqual(mq.Queue(self.repo).all(), [])

    def test_a_module_that_does_not_parse_is_refused(self):
        self.commit("tests/test_broken.py", "broken", "import host_isolation\ndef (:\n")
        identifier, _, error = self.land("broken")
        self.assertIsNone(identifier)
        self.assertIn("queuekeeper unittest: test_broken.py does not parse (invalid syntax, line 2); nothing ran", error)

    def test_nested_modules_are_refused_in_the_submitted_tree(self):
        self.commit("tests/apple/test_forgot.py", "nested", UNISOLATED)
        identifier, _, error = self.land("nested")
        self.assertIsNone(identifier)
        self.assertIn("apple/test_forgot.py must import host_isolation", error)
        self.assertEqual(mq.Queue(self.repo).all(), [])

    def test_an_isolated_module_is_queued(self):
        # The positive control.
        self.commit("tests/test_new.py", "new", ISOLATED)
        identifier, _, error = self.land("new")
        self.assertIsNone(error)
        self.assertEqual(self.state(identifier)["state"], "queued")

    def test_a_refusal_main_already_has_is_not_the_entrys(self):
        # main took a module without the import past the queue: an entry that adds nothing of the kind is queued.
        run(self.repo, "git", "checkout", "-q", "main")
        (self.repo / "tests/test_old.py").write_text(UNISOLATED)
        run(self.repo, "git", "add", "tests/test_old.py")
        run(self.repo, "git", "commit", "-qm", "Past the queue")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        self.commit("other", "other")
        identifier, _, error = self.land("other")
        self.assertIsNone(error)
        self.assertEqual(self.state(identifier)["state"], "queued")
        # An entry that adds a second such module is still refused, naming its own.
        self.commit("tests/test_forgot.py", "forgot", UNISOLATED)
        identifier, _, error = self.land("forgot")
        self.assertIsNone(identifier)
        self.assertIn("test_forgot.py, test_old.py must import host_isolation", error)

    def test_a_gate_that_is_not_eng_unittest_is_not_checked(self):
        config = self.config()
        config["lanes"]["gate"] = {"command": ["python3", "lane.py", str(self.counter), str(self.status)]}
        self.publish(config)
        self.commit("tests/test_forgot.py", "forgot", UNISOLATED)
        identifier, _, error = self.land("forgot")
        self.assertIsNone(error)
        self.assertEqual(self.state(identifier)["state"], "queued")

    def test_a_suite_without_host_isolation_or_a_module_its_pattern_skips_is_not_refused(self):
        config = self.config()
        config["lanes"]["gate"]["command"] += ["-p", "test_ok.py"]
        self.publish(config)
        self.commit("tests/test_forgot.py", "forgot", UNISOLATED)
        self.assertIsNone(self.land("forgot")[2])
        # Every module, in a suite without host_isolation.py.
        run(self.repo, "git", "checkout", "-q", "main")
        run(self.repo, "git", "rm", "-q", "tests/host_isolation.py")
        config["lanes"]["gate"]["command"] = config["lanes"]["gate"]["command"][:-2]
        self.publish(config, "No isolation")
        self.commit("tests/test_bare.py", "bare", UNISOLATED)
        self.assertIsNone(self.land("bare")[2])

    def test_the_suites_of_a_lanes_command(self):
        self.assertEqual(mq.suite_of(["python3", "scripts/qk", "unittest", "-s", "tests", "--split", "test", "-v"]),
                         ("tests", None))
        self.assertEqual(mq.suite_of(["python3", "scripts/qk", "unittest", "--start-directory=t", "-p", "a*.py",
                                      "--pattern=b*.py", "-j", "4"]), ("t", ["a*.py", "b*.py"]))
        self.assertEqual(mq.suite_of(["python3", "scripts/qk", "unittest"]), (".", None))
        self.assertIsNone(mq.suite_of(["python3", "lane.py"]))


class Placement(Fixture):
    def test_the_cli_checks_the_configured_gate_on_every_mac_without_starting_work(self):
        config = self.config()
        config["lanes"]["gate"].update(resource="heavy", wholeHost=True)
        self.publish(config)
        free = {"slots": 3, "slotsMax": 7, "busy": [], "busyLimit": 90}
        blocker = {"state": "queued", "kind": "exclusive", "pid": 12, "identity": "admission-12", "processStart": "start"}
        queued = {**free, "busy": [1], "queuedExclusive": "PID 12", "blockers": [blocker]}
        busy = {**free, "busy": [1]}
        with farm_fixture.only("build-host", "gate-03", "gate-04"), \
                patch.object(mq.remote, "capacity_there", side_effect=[
                    queued, busy, mq.remote.Unreachable("gate-04 is offline", "wake it")
                ]) as probe, patch.object(offload_host, "capacity", return_value=free) as here, \
                patch.object(mq.Queue, "__init__", side_effect=AssertionError("placement touched the queue")), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(["--project", str(self.repo), "queue", "placement", "--json"]), 0)
        report = json.loads(output.getvalue())
        self.assertTrue(report["wholeHost"])
        self.assertEqual(report["remoteHosts"], ["build-host", "gate-03", "gate-04"])
        self.assertEqual([row["status"] for row in report["hosts"]], ["waiting", "waiting", "unavailable", "eligible"])
        self.assertEqual(report["hosts"][0]["blockers"], [blocker])
        self.assertIn("exclusive work is queued ahead", report["hosts"][0]["reason"])
        self.assertIn("needs every guaranteed slot", report["hosts"][1]["reason"])
        self.assertEqual([call.args[0] for call in probe.call_args_list], report["remoteHosts"])
        self.assertTrue(all(call.kwargs == {"diagnostic": True} for call in probe.call_args_list))
        here.assert_called_once_with(diagnostic=True)

    def probe(self, *words):
        """(exit status, output) of `qk queue placement` with words, on a farm of one other Mac, studio, which has the label
        apple-m5-max, and this Mac, which has none."""
        free = {"slots": 3, "slotsMax": 7, "busy": [], "busyLimit": 90}
        with farm_fixture.only("studio"), \
                patch.object(mq.remote, "capacity_there", return_value={**free, "labels": ["apple-m5-max"]}), \
                patch.object(offload_host, "capacity", return_value={**free, "labels": []}), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            status_ = cli.main(["--project", str(self.repo), "queue", "placement", *words])
        return status_, output.getvalue()

    def test_a_working_trees_label_change_is_checked_before_it_lands(self):
        # vocaro/queuekeeper#278: origin/main's gate needs no label; the working tree's, not yet committed, needs one.
        self.publish(self.config())
        main = mq.main_commit(self.repo)
        config = self.config()
        config["lanes"]["gate"]["labels"] = ["apple-m5-max"]
        (self.repo / "queuekeeper.json").write_text(json.dumps(config))
        status_, said = self.probe("gate", "--json")
        self.assertEqual(status_, 0)
        report = json.loads(said)
        self.assertEqual((report["source"], report["commit"], report["labels"]), ("main", main, []))
        self.assertEqual(report["config"], f"origin/main ({main[:12]})")
        self.assertIn("working tree changes gate's labels", report["workingTreeDiffers"])
        self.assertIn("--worktree", report["workingTreeDiffers"])
        self.assertEqual([row["status"] for row in report["hosts"]], ["eligible", "eligible"])
        # Read from the working tree, the new label is the lane's, and this Mac, which lacks it, declines.
        status_, said = self.probe("gate", "--worktree", "--json")
        self.assertEqual(status_, 0)
        report = json.loads(said)
        self.assertEqual((report["source"], report["commit"], report["labels"]), ("worktree", None, ["apple-m5-max"]))
        self.assertIn(str(self.repo / "queuekeeper.json"), report["config"])
        self.assertNotIn("workingTreeDiffers", report)
        self.assertEqual([row["status"] for row in report["hosts"]], ["eligible", "waiting"])
        self.assertIn("lacks the label apple-m5-max that the work needs", report["hosts"][1]["reason"])
        # The text names the file it read and the lane's labels.
        status_, said = self.probe("gate", "--worktree")
        self.assertEqual(status_, 0)
        self.assertIn("gate from this checkout's working tree (", said)
        self.assertIn("labels: apple-m5-max)", said)
        status_, said = self.probe("gate")
        self.assertIn(f"gate from origin/main ({main[:12]})", said)
        self.assertIn("labels: none)", said)
        self.assertIn("note: this checkout's working tree changes gate's labels", said)

    def test_a_ref_is_read_as_committed(self):
        self.publish(self.config())
        config = self.config()
        config["lanes"]["gate"]["labels"] = ["apple-m5-max"]
        self.commit("queuekeeper.json", "labelled", json.dumps(config))
        tip = run(self.repo, "git", "rev-parse", "HEAD")
        # The working tree is put back to origin/main's, so that only the ref holds the label.
        run(self.repo, "git", "checkout", "-q", "--detach", "origin/main")
        status_, said = self.probe("gate", "--ref", "labelled", "--json")
        self.assertEqual(status_, 0)
        report = json.loads(said)
        self.assertEqual((report["source"], report["commit"], report["labels"]), ("ref", tip, ["apple-m5-max"]))
        self.assertEqual(report["config"], f"labelled ({tip[:12]})")
        self.assertEqual([row["status"] for row in report["hosts"]], ["eligible", "waiting"])
        # A working tree that agrees with origin/main is not noted.
        status_, said = self.probe("gate", "--json")
        self.assertNotIn("workingTreeDiffers", json.loads(said))
        # A ref that names nothing is refused, and --ref and --worktree are not both taken.
        with contextlib.redirect_stderr(io.StringIO()) as error:
            status_, said = self.probe("gate", "--ref", "no-such-branch")
        self.assertNotEqual(status_, 0)
        self.assertIn("no-such-branch names no commit", error.getvalue() + said)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.probe("gate", "--ref", "labelled", "--worktree")


class Doctor(UnittestGate):
    """`qk queue doctor` reads the queue for the signs that it is stuck rather than testing, says what each means and
    what heals it, and changes nothing (vocaro-engineering#290)."""
    def doctor(self, working=True):
        said = io.StringIO()
        with patch.object(mq.Queue, "working", return_value=working), contextlib.redirect_stdout(said):
            status_ = mq.doctor(self.repo)
        with patch.object(mq.Queue, "working", return_value=working), contextlib.redirect_stdout(io.StringIO()) as js:
            self.assertEqual(mq.doctor(self.repo, as_json=True), status_)
        return status_, said.getvalue(), json.loads(js.getvalue())

    def gate_log(self, name, text, at):
        path = mq.Queue(self.repo).logs / f"{name}-gate.log"
        path.write_text(text)
        os.utime(path, (at, at))
        return path

    def snapshot(self):
        queue = mq.Queue(self.repo)
        return [(e["id"], e["state"]) for e in queue.all()], queue.held()

    def test_a_healthy_queue_is_healthy(self):
        # The negative control: an entry waiting with a worker, and the newest gate gave a verdict, even though an
        # older one was refused.
        self.commit("tests/test_new.py", "new", ISOLATED)
        self.add("new")
        self.gate_log("20261107-150000-aaaaaaaa", REFUSED_LINE + "\n", 1000)
        self.gate_log("20261107-160000-bbbbbbbb", "queuekeeper unittest: 9 tests (0 skipped) in 9 processes, 4 at a time, "
                      "1.0s: FAILED: test_x.T.test_t (FAILED (failures=1), status 1)\n", 2000)
        status_, said, found = self.doctor()
        self.assertEqual(status_, 0, said)
        self.assertIn("qk queue doctor: healthy: 1 entry waiting, worker running", said)
        self.assertEqual(found["findings"], [])
        self.assertTrue(found["healthy"])

    def test_a_gate_waiting_for_admission_points_to_eng_admissions_now_without_a_finding(self):
        # vocaro-engineering#342: a pointer, not a report: doctor reads no admission and stays healthy.
        self.commit("tests/test_new.py", "new", ISOLATED)
        self.add("new")
        queue = mq.Queue(self.repo)
        queue.note(lambda state: state.setdefault("gates", {}).__setitem__("main", {
            "lane": "gate", "waiting": "admission on this Mac: all 3 guaranteed slots are busy",
            "waitingKind": "admission", "waitingSince": time.time() - 120}))
        from queuekeeper import offload_host, remote
        with patch.object(offload_host, "admissions_now", side_effect=AssertionError("read")), \
                patch.object(remote, "admissions_there", side_effect=AssertionError("asked")):
            status_, said, found = self.doctor()
        self.assertEqual((status_, found["healthy"], found["findings"]), (0, True, []))
        self.assertIn("qk queue doctor: healthy", said)
        self.assertIn("note: the main gate's gate has waited 2m", said)
        self.assertIn("`python3 scripts/qk admissions now`", found["hints"][0])
        # The negative control: a gate waiting for something else, or with no worker, has no pointer.
        queue.note(lambda state: state["gates"]["main"].update(waitingKind="screen"))
        self.assertEqual(self.doctor()[2]["hints"], [])
        queue.note(lambda state: state["gates"]["main"].update(waitingKind="admission"))
        self.assertEqual(self.doctor(working=False)[2]["hints"], [])

    # vocaro/queuekeeper#89: on 2026-11-11 BreezEPUB's gate waited 45 minutes on this Mac, 83% idle, behind one slot
    # held by another session's iPad Quick Look run at about 3% CPU, while doctor said healthy.
    REFUSAL = "all 3 guaranteed slots are busy"
    QUICK_LOOK = {"identity": "q", "label": "python3 scripts/qk run quicklook-ipad", "pid": 4242, "start": "x",
                  "heldSeconds": 5 * 3600, "process": "running", "cpuPercent": 3.0, "exclusive": False,
                  "wholeHost": False, "model": False, "guaranteed": [0], "extra": [], "slots": [0], "stranded": False,
                  "origin": {"repository": "BreezEPUB", "branch": "quicklook",
                             "worktree": "/Users/x/.claude/worktrees/quicklook-ipad", "session": "local_1"}}

    def waiting_gate(self, minutes, mac="this Mac"):
        """A worker's account of a gate waiting minutes for admission on mac, as GateWatch records it."""
        self.commit("tests/test_new.py", "new", ISOLATED)
        self.add("new")
        queue = mq.Queue(self.repo)
        queue.note(lambda state: state.setdefault("gates", {}).__setitem__("main", {
            "lane": "gate", "waiting": f"admission on {mac}: {self.REFUSAL}", "waitingKind": "admission",
            "waitingOn": mac, "waitingSince": time.time() - minutes * 60}))
        return queue

    def reading(self, *holders):
        """offload_host.admissions_now's reading of a Mac with holders, as `qk admissions now` takes it."""
        return {"schemaVersion": 1, "mac": "m", "observedAt": time.time(), "state": "observed", "reasons": [],
                "namespace": {"shared": True}, "limits": {"slots": 3, "slotsMax": 4}, "slots": [], "model": {"holder": None},
                "holders": list(holders), "unattributed": [], "waiters": []}

    def test_a_gate_held_back_long_by_a_light_holder_on_an_idle_mac_names_the_holder_and_changes_nothing(self):
        queue = self.waiting_gate(45)
        unsure = {**self.QUICK_LOOK, "identity": "u", "label": "python3 scripts/qk admit -- xcodebuild test", "pid": 77,
                  "heldSeconds": 600, "process": "unknown", "cpuPercent": None, "origin": None}
        before, account = self.snapshot(), queue.round_state()["gates"]
        from queuekeeper import remote
        # A holder admission reclaimed lately holds nothing, and is named apart from the holders (vocaro/queuekeeper#23).
        reclaimed = {"identity": "g", "label": "EPUBLib swift-tests", "pid": 14501, "kind": "heavy", "slots": [2],
                     "process": "ended", "why": "its PID 14501 is gone", "reclaimedSeconds": 65.0}
        reading = {**self.reading(self.QUICK_LOOK, unsure), "reclaimed": [reclaimed]}
        with patch.object(offload_host, "admissions_now", return_value=reading), \
                patch.object(remote, "admissions_there", side_effect=AssertionError("asked another Mac")):
            status_, said, found = self.doctor()
        self.assertEqual((status_, found["healthy"]), (1, False), said)
        (finding,) = found["findings"]
        self.assertEqual(finding["signal"], "held back on a Mac with spare CPU")
        diagnosis = finding["diagnosis"]
        # What it waits behind, in the waiter's own words, and how idle the Mac is. The wait is read against the clock
        # at the doctor's look, so its seconds are whatever the run took to get there: only its minutes are fixed
        # (vocaro/queuekeeper#124).
        self.assertIn("the main gate's gate has waited 45m", diagnosis)
        self.assertIn(f"s for admission on this Mac: {self.REFUSAL}, while this Mac is 25% busy, under its 75% limit",
                      diagnosis)
        # Each holder: command, PID, hold time, CPU use and where its work comes from.
        self.assertIn("python3 scripts/qk run quicklook-ipad (PID 4242, held 5h00m, CPU 3% of one core, with what it "
                      "started, from repository BreezEPUB, branch quicklook, worktree "
                      "/Users/x/.claude/worktrees/quicklook-ipad, session local_1", diagnosis)
        # A holder whose process could not be checked (vocaro/queuekeeper#23) is said so, never guessed light, and
        # that admission frees it at its next attempt.
        self.assertIn("python3 scripts/qk admit -- xcodebuild test (PID 77, held 10m00s, its process could not be "
                      "checked, so its CPU use is unknown, and admission frees its slots at its next attempt, where its "
                      "work comes from was not recorded)", diagnosis)
        self.assertIn(". Reclaimed lately, holding nothing now: EPUBLib swift-tests (PID 14501, slot 2, reclaimed 1m05s "
                      "ago: its process had ended, its PID 14501 is gone) (vocaro/queuekeeper#89)", diagnosis)
        self.assertEqual(finding["reclaimed"], [reclaimed])
        self.assertIn("qk admissions now", finding["remedy"])
        self.assertEqual([holder["pid"] for holder in finding["holders"]], [4242, 77])
        self.assertIn("held back on a Mac with spare CPU: the main gate's gate has waited", said)
        self.assertEqual((self.snapshot(), queue.round_state()["gates"]), (before, account))
        self.busy_of.assert_called_with(None)

    def test_a_gate_waiting_long_on_a_busy_mac_or_briefly_on_an_idle_one_is_no_finding(self):
        # The negative control: a Mac at or above its busy limit is busy, and its holders are not read.
        self.waiting_gate(45)
        from queuekeeper import remote
        never = (patch.object(offload_host, "admissions_now", side_effect=AssertionError("read")),
                 patch.object(remote, "admissions_there", side_effect=AssertionError("asked")))
        self.busy_of.return_value = (90.0, 75.0, "this Mac")
        with never[0], never[1]: status_, said, found = self.doctor()
        self.assertEqual((status_, found["findings"]), (0, []), said)
        self.assertIn("note: the main gate's gate has waited 45m", said)
        # A busy share that cannot be read is unknown, never idle: a note, no finding.
        self.busy_of.return_value = None
        with never[0], never[1]: status_, said, found = self.doctor()
        self.assertEqual((status_, found["findings"]), (0, []), said)
        self.assertIn("whether this Mac has spare CPU is unknown, as its busy share could not be read", said)
        # Under the bound, an idle Mac's wait is read no further.
        self.busy_of.return_value = (25.0, 75.0, "this Mac")
        mq.Queue(self.repo).note(lambda state: state["gates"]["main"].update(
            waitingSince=time.time() - mq.IDLE_WAIT_SECONDS + 60))
        with never[0], never[1]: self.assertEqual(self.doctor()[2]["findings"], [])

    def test_a_gate_waiting_for_the_farm_mac_reads_its_holders_there_or_says_unknown(self):
        self.waiting_gate(30, mac="build-host")
        from queuekeeper import remote
        self.busy_of.return_value = (10.0, 75.0, "build-host")
        with patch.object(offload_host, "admissions_now", side_effect=AssertionError("read this Mac")), \
                patch.object(remote, "admissions_there", return_value=self.reading(self.QUICK_LOOK)) as there:
            found = self.doctor()[2]
        self.busy_of.assert_called_with("build-host")
        self.assertEqual(there.call_args.args[0], "build-host")
        self.assertIn("while build-host is 10% busy", found["findings"][0]["diagnosis"])
        self.assertIn("PID 4242, held 5h00m, CPU 3% of one core", found["findings"][0]["diagnosis"])
        with patch.object(remote, "admissions_there", side_effect=remote.Unreachable("build-host did not answer in 10s", "wake it")):
            found = self.doctor()[2]
        self.assertIn("Holders: unknown: build-host did not answer in 10s", found["findings"][0]["diagnosis"])
        self.assertIsNone(found["findings"][0]["holders"])

    def test_gates_refused_before_running_name_the_entry_that_brings_the_refusal_and_change_nothing(self):
        self.commit("tests/test_new.py", "new", ISOLATED)
        self.commit("tests/test_forgot.py", "forgot", UNISOLATED)
        self.commit("c", "c")
        # Queued as before qk land checked: by an older toolkit, or as the entry of 2026-11-07 was.
        with patch.object(mq, "checked_before_running"): ids = self.add("new", "forgot", "c")
        self.gate_log("20261107-165720-19efdd1d", REFUSED_LINE + "\n", 1000)
        self.gate_log("20261107-165854-a956c211", "heavy: slot 1\n" + REFUSED_LINE + "\n", 2000)
        before = self.snapshot()
        status_, said, found = self.doctor()
        self.assertEqual(status_, 1, said)
        signals = {finding["signal"]: finding for finding in found["findings"]}
        self.assertEqual(set(signals), {"refused gates", "refused before running"}, said)
        refused = signals["refused gates"]
        self.assertIn("2 gates since the last verdict ran nothing", refused["diagnosis"])
        self.assertIn(REFUSED_LINE, refused["diagnosis"])
        self.assertIn("20261107-165854-a956c211-gate.log, 20261107-165720-19efdd1d-gate.log", refused["diagnosis"])
        self.assertIn(f"{ids[1]} brings it", refused["remedy"])
        self.assertIn(f"`qk queue remove {ids[1]}`", refused["remedy"])
        self.assertNotIn(ids[0], said)
        self.assertNotIn(ids[2], said)
        self.assertIn(f"qk unittest refuses {ids[1]}'s tree before running anything", said)
        self.assertIn("and it changed nothing", said)
        self.assertEqual(self.snapshot(), before)

    def test_a_refusal_no_waiting_entry_brings_is_not_blamed_on_one(self):
        self.commit("c", "c")
        self.add("c")
        self.gate_log("20261107-165720-19efdd1d", REFUSED_LINE + "\n", 1000)
        status_, said, found = self.doctor()
        self.assertEqual(status_, 1, said)
        self.assertEqual([f["signal"] for f in found["findings"]], ["refused gates"])
        self.assertIn("1 gate since the last verdict ran nothing", said)
        self.assertIn("no waiting entry's tree alone is refused so", said)

    def test_gates_in_a_row_with_no_verdict_are_a_finding_and_one_is_not(self):
        self.gate_log("20261107-100000-aaaaaaaa", "queuekeeper: lane gate reached its limit\n", 1000)
        status_, said, _ = self.doctor()
        self.assertEqual(status_, 0, said)
        self.gate_log("20261107-110000-bbbbbbbb", "queuekeeper unittest: 9 tests (0 skipped) in 9 processes, 4 at a time, "
                      "1.0s: UNMEASURED: test_x (timed out after 1800s)\n", 2000)
        status_, said, found = self.doctor()
        self.assertEqual(status_, 1, said)
        self.assertEqual([f["signal"] for f in found["findings"]], ["no verdict"])
        self.assertIn("2 gates since the last verdict gave none", said)

    def test_stacked_admission_declines_neither_count_nor_hide_main_gate_no_verdicts(self):
        for number in range(mq.DOCTOR_LOGS + 1):
            self.gate_log(f"stack-{number}", f"nothing ran\n{mq.STACK_ADMISSION_DECLINED}\n", 1000 + number)
        status_, said, _ = self.doctor()
        self.assertEqual(status_, 0, said)
        # Real no-verdict runs still count, even with more than the log limit's worth of newer stack declines.
        self.gate_log("main-busy", "nothing ran\n", 900)
        self.gate_log("main-timed-out", "queuekeeper: lane gate reached its limit\n", 901)
        status_, said, found = self.doctor()
        self.assertEqual(status_, 1, said)
        self.assertEqual([f["signal"] for f in found["findings"]], ["no verdict"])
        self.assertIn("2 gates since the last verdict gave none", said)
        self.assertIn("main-timed-out-gate.log, main-busy-gate.log", said)

    def test_a_hold_and_entries_waiting_with_no_worker_are_findings(self):
        self.commit("c", "c")
        self.add("c")
        status_, said, found = self.doctor(working=False)
        self.assertEqual(status_, 1, said)
        self.assertEqual([f["signal"] for f in found["findings"]], ["no worker"])
        self.assertIn("1 entry wait and no worker runs", said)
        mq.Queue(self.repo).hold_file.write_text("for a session: something\n")
        status_, said, found = self.doctor(working=False)
        self.assertEqual([f["signal"] for f in found["findings"]], ["held"])
        self.assertIn("the queue is held for a session: something", said)

    def test_an_entry_queued_again_unchanged_came_back_and_a_changed_one_did_not(self):
        self.commit("c", "c")
        first = self.add("c")[0]
        mq.remove(mq.Queue(self.repo), first, quiet=True)
        with patch("time.time", return_value=time.time() + 5): again = self.add("c")[0]
        status_, said, found = self.doctor()
        self.assertEqual(status_, 1, said)
        self.assertEqual([f["signal"] for f in found["findings"]], ["came back"])
        self.assertIn(f"{again} holds the very change {first} held, which was taken out", said)
        # The control: the branch, changed, comes back as another change.
        mq.remove(mq.Queue(self.repo), again, quiet=True)
        run(self.repo, "git", "checkout", "-q", "c")
        (self.repo / "c").write_text("fixed")
        run(self.repo, "git", "commit", "-qam", "Fix c")
        with patch("time.time", return_value=time.time() + 10): self.add("c")
        status_, said, _ = self.doctor()
        self.assertEqual(status_, 0, said)

    def test_the_command_line_takes_doctor(self):
        import argparse
        parser = argparse.ArgumentParser()
        mq.add_arguments(parser)
        self.assertEqual(vars(parser.parse_args(["doctor", "--json"])), {"operation": "doctor", "json": True})


class RefusedGates(UnittestGate):
    """The worker blames a gate qk unittest refused before running anything on the one entry that brings the refusal,
    with no rerun and no bisect; with none or several it bisects, and when the same refusal comes again with no entry
    to blame it holds for a session with `qk queue doctor`'s diagnosis (owner's decision, 2026-11-08,
    vocaro-engineering#290). These tests run real gates of qk unittest."""
    def queued(self, *branches):
        """Queue branches as an older toolkit did, or the entry of 2026-11-07, before qk land checked them."""
        with patch.object(mq, "checked_before_running"): return self.add(*branches)

    def work_said(self):
        with contextlib.redirect_stdout(io.StringIO()) as said: self.assertEqual(self.work(), 0)
        return said.getvalue()

    def past_the_queue(self, name, content=UNISOLATED):
        """Put a test module on main past the queue, as a hand push might."""
        run(self.repo, "git", "checkout", "-q", "main")
        run(self.repo, "git", "pull", "-q", "origin", "main")
        (self.repo / "tests" / name).write_text(content)
        run(self.repo, "git", "add", f"tests/{name}")
        run(self.repo, "git", "commit", "-qm", "Past the queue")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")

    def test_the_one_entry_a_refusal_names_goes_back_in_one_round_without_a_rerun_or_a_bisect(self):
        self.commit("c", "c")
        self.commit("tests/test_forgot.py", "forgot", UNISOLATED)
        self.commit("tests/test_new.py", "new", ISOLATED)
        ids = self.queued("c", "forgot", "new")
        log = self.work_said()
        self.assertEqual([self.state(i)["state"] for i in ids], ["landed", "failed", "landed"], log)
        self.assertIn("gate ran nothing, as qk unittest refused it before running anything, which no rerun would "
                      "change: queuekeeper unittest: test_forgot.py must import host_isolation", log)
        self.assertIn(f"blaming {ids[1]} (Add tests/test_forgot.py): qk unittest refused gate before running "
                      f"anything, which no rerun would change, and only {ids[1]} brings the refusal, as its tree alone "
                      "is refused so and it changes tests/test_forgot.py: queuekeeper unittest: test_forgot.py must import "
                      "host_isolation", log)
        self.assertIn("sending it back and gating the other 2 at once instead of bisecting", log)
        self.assertNotIn("fail together", log)
        self.assertNotIn("rerunning", log)
        self.assertNotIn("no failed test to rerun alone", log)
        self.assertIn("test_forgot.py must import host_isolation", self.state(ids[1])["why"])
        # Two gates in all: the batch, then the other two at once.
        self.assertEqual(log.count("testing "), 2, log)
        self.assertIsNone(mq.Queue(self.repo).held())

    def test_a_refusal_two_entries_bring_bisects(self):
        # Each brings a module the one refusal names: no single cause, so the batch bisects, and each half then
        # blames its own.
        self.commit("tests/test_one.py", "one", UNISOLATED)
        self.commit("tests/test_two.py", "two", UNISOLATED)
        self.commit("c", "c")
        ids = self.queued("one", "two", "c")
        log = self.work_said()
        self.assertIn("qk unittest refused gate before running anything, and 2 entries of the batch's 3 bring it "
                      f"({ids[0]}: its tree alone is refused so and it changes tests/test_one.py; {ids[1]}: its tree "
                      "alone is refused so and it changes tests/test_two.py), so no commit is blamed", log)
        self.assertIn("3 commits fail together", log)
        self.assertEqual([self.state(i)["state"] for i in ids], ["failed", "failed", "landed"], log)
        self.assertIsNone(mq.Queue(self.repo).held(), log)

    def test_a_refusal_naming_a_module_two_entries_change_bisects(self):
        self.past_the_queue("test_old.py")
        # At either end of the module, so that neither conflicts with the other.
        self.commit("tests/test_old.py", "one", "# one\n" + UNISOLATED)
        self.commit("tests/test_old.py", "two", UNISOLATED + "# two\n")
        ids = self.queued("one", "two")
        log = self.work_said()
        self.assertIn(f"and 2 entries of the batch's 2 bring it ({ids[0]}: it changes tests/test_old.py; {ids[1]}: "
                      "it changes tests/test_old.py), so no commit is blamed", log)
        self.assertIn("2 commits fail together", log)
        self.assertNotIn("blaming", log.split("fail together")[0])
        # Each half alone then has one entry that changes the module: it goes back, as the rule says.
        self.assertEqual([self.state(i)["state"] for i in ids], ["failed", "failed"], log)

    def refused_by_main(self, beside=True, bisecting=False):
        """Two entries whose every gate main's own module refuses, run until the worker stops: (ids, log, hold).
        bisecting starts the worker mid-bisect, as one that took over from another does (Worker.progress), so that
        the first gate is the bisect's two halves, with no gate before them."""
        # main took a module without the import past the queue: every gate is refused, and no entry is the cause.
        self.past_the_queue("test_old.py")
        self.commit("c", "c")
        self.commit("d", "d")
        ids = self.queued("c", "d")
        with contextlib.ExitStack() as halves:
            # Both halves on this Mac, as BothHalves gates them, since the fixture has no other Mac; or one at a time.
            for name, value in ((("apart", BOTH_HERE), ("BESIDE_POLL", 0.02), ("offerable", lambda config, lanes: None)) if beside
                                else (("offerable", lambda config, lanes: "a test says so"),)):
                halves.enter_context(patch.object(mq, name, value))
            if bisecting:
                real = mq.Worker.__init__
                def taken_over(worker, queue, progress=None):
                    real(worker, queue, {"limit": 1, "suspects": 2} if type(worker) is mq.Worker else progress)
                halves.enter_context(patch.object(mq.Worker, "__init__", taken_over))
            log = self.work_said()
        if not bisecting:
            self.assertIn("qk unittest refused gate before running anything, and no entry of the batch's 2 brings it, "
                          "so no commit is blamed", log)
            self.assertIn("2 commits fail together", log)
        # Nobody was sent back on a refusal no entry brings.
        self.assertEqual([self.state(i)["state"] for i in ids], ["queued", "queued"], log)
        held = mq.Queue(self.repo).held()
        self.assertIsNotNone(held, log)
        return ids, log, held

    def test_a_refusal_no_entry_brings_in_both_halves_of_a_bisect_holds_for_a_session_with_the_diagnosis(self):
        ids, log, held = self.refused_by_main()
        self.assertIn("qk unittest refused this gate and the bisect's newer half beside it before running anything", held)
        self.assertEqual(log.count("testing "), 3, log)  # the batch, then its two halves at once
        self.check_hold(ids, log, held)

    def test_the_same_refusal_in_both_halves_with_no_gate_before_holds_and_sends_back_no_lone_half(self):
        # Each half is one entry, which a refusal no entry brings would otherwise send back as a lone failure.
        ids, log, held = self.refused_by_main(bisecting=True)
        self.assertIn("qk unittest refused this gate and the bisect's newer half beside it before running anything", held)
        self.assertIn("newer half: qk unittest refused gate before running anything, and no entry of the batch's 1 "
                      "brings it", log)
        self.assertIn("queue: held: for a session, not the owner: qk unittest refused this gate and the bisect's older "
                      "half beside it", log)
        self.assertEqual(log.count("testing "), 2, log)  # the two halves at once, and nothing after
        self.check_hold(ids, log, held)

    def test_a_refusal_no_entry_brings_in_two_gates_in_a_row_holds_for_a_session(self):
        ids, log, held = self.refused_by_main(beside=False)
        self.assertIn("qk unittest refused this gate and the gate before it before running anything", held)
        self.assertEqual(log.count("testing "), 2, log)
        self.check_hold(ids, log, held)

    def check_hold(self, ids, log, held):
        self.assertTrue(held.startswith("for a session, not the owner: qk unittest refused this gate and "), held)
        self.assertIn("the same way, and no one entry of the batch brings the refusal", held)
        self.assertIn("test_old.py must import host_isolation", held)
        self.assertIn("qk queue doctor: refused gates: ", held)
        self.assertIn("no waiting entry's tree alone is refused so", held)
        self.assertIn(f"queue: held: {held}", log)
        self.assertEqual([self.state(i)["state"] for i in ids], ["queued", "queued"], log)
        with contextlib.redirect_stdout(io.StringIO()) as status_:
            mq.status(self.repo)
        self.assertIn("qk queue doctor: refused gates: ", status_.getvalue())

    def test_a_refusal_that_names_no_module_blames_nobody(self):
        worker = mq.Worker(mq.Queue(self.repo))
        self.commit("c", "c")
        ids = self.queued("c")
        with contextlib.redirect_stdout(io.StringIO()) as said:
            self.assertIsNone(worker.blame_refusal([self.state(ids[0])], "gate",
                                                   "queuekeeper unittest: something else; nothing ran", self.config()))
        self.assertIn("the refusal names no test module, so no commit is blamed", said.getvalue())
        self.assertEqual(worker.unblamed["line"], "queuekeeper unittest: something else; nothing ran")

    def test_the_same_refusal_from_two_checkouts_is_one(self):
        self.assertEqual(mq.refusal_key(REFUSED_LINE), mq.refusal_key(REFUSED_LINE.replace("/queue/checkout",
                                                                                            "/elsewhere/beside")))
        self.assertNotEqual(mq.refusal_key(REFUSED_LINE), mq.refusal_key(REFUSED_LINE.replace("test_forgot",
                                                                                               "test_other")))


# A gate whose every test passes, as qk unittest says it, though the lane fails after them (vocaro/queuekeeper#290):
# with a file named teardown in its tree it exits 1, and when its tests' definitions number fewer than baseline says,
# it says the count as qk run's count history does and exits 2, as the gate of 2026-11-19 on build-host did. Each run
# adds a line to the counter whose path follows the command.
PASSING_UNITTEST = """import pathlib, sys
with open(sys.argv[-1], "a") as counter: counter.write("x")
count = sum(path.read_text().count("def test") for path in pathlib.Path("tests").glob("test*.py"))
if pathlib.Path("skipall").exists(): count -= 1
print(f"queuekeeper unittest: {count} tests (0 skipped) in 1 processes, 1 at a time, 0.1s: OK")
baseline = int(pathlib.Path("baseline").read_text())
if count < baseline:
    print(f"queuekeeper: gate executed {count} tests, {baseline - count} fewer than the {baseline} its last passing "
          "whole run on Fixture-Mac executed at 0123456789ab (gate.json). If tests were removed on purpose, add the "
          "trailer `Tests-Removed: gate` to a commit after 0123456789ab", file=sys.stderr)
    sys.exit(2)
sys.exit(1 if pathlib.Path("teardown").exists() else 0)
"""
TWO_TESTS = "import unittest\nimport host_isolation\n\nclass T(unittest.TestCase):\n    def test_a(self): pass\n    def test_b(self): pass\n"


class PassingTestsFailedGate(Fixture):
    """A gate whose lane fails though qk unittest's verdict is OK is never bisected, since it names no failed test to
    find (vocaro/queuekeeper#290): a count too low goes back to the one entry that removes tests, or holds at once with
    none, and any other cause is no verdict, run again within queue.noVerdictRuns."""
    def setUp(self):
        super().setUp()
        config = self.config()
        config["lanes"]["gate"] = {"command": ["python3", "tools/qk", "unittest", "-s", "tests", "-v",
                                               str(self.counter)]}
        for name, content in (("tools/qk", PASSING_UNITTEST), ("tests/host_isolation.py", ""),
                              ("tests/test_one.py", ISOLATED), ("tests/test_two.py", TWO_TESTS), ("baseline", "3")):
            (self.repo / name).parent.mkdir(exist_ok=True)
            (self.repo / name).write_text(content)
        run(self.repo, "git", "add", ".")
        self.publish(config, "Gate on a qk unittest whose tests pass")

    def removal(self, path, branch):
        """A commit of its own on a new branch that deletes path."""
        run(self.repo, "git", "fetch", "-q", "origin")
        run(self.repo, "git", "checkout", "-q", "-B", branch, "origin/main")
        run(self.repo, "git", "rm", "-q", path)
        run(self.repo, "git", "commit", "-qm", f"Remove {path}")

    def queued(self, *branches):
        with patch.object(mq, "checked_before_running"): return self.add(*branches)

    def work_said(self):
        with contextlib.redirect_stdout(io.StringIO()) as said: self.assertEqual(self.work(), 0)
        return said.getvalue()

    def test_a_count_too_low_goes_back_to_the_one_entry_that_removes_tests_without_a_bisect(self):
        self.commit("c", "c")
        self.removal("tests/test_two.py", "drop")
        self.commit("tests/test_three.py", "more", ISOLATED)
        ids = self.queued("c", "drop", "more")
        log = self.work_said()
        self.assertEqual([self.state(i)["state"] for i in ids], ["landed", "failed", "landed"], log)
        self.assertIn("gate failed with every test passing, as it ran too few: gate executed 2 tests, 1 fewer than the "
                      "3 its last passing whole run", log)
        self.assertIn(f"blaming {ids[1]} (Remove tests/test_two.py): gate ran too few tests, though every test it ran "
                      f"passed, and only {ids[1]} removes tests from its suite (2 test definitions, from "
                      "tests/test_two.py), which no rerun would change", log)
        self.assertIn("Tests-Removed: gate", self.state(ids[1])["why"])
        self.assertNotIn(": bisecting", log)
        self.assertNotIn("fail together", log)
        # The batch, then the other two at once.
        self.assertEqual(self.gates(), 2, log)
        self.assertIsNone(mq.Queue(self.repo).held())

    def test_a_count_too_low_that_no_entry_removes_holds_at_once_and_sends_back_nobody(self):
        self.commit("c", "c")
        self.commit("skipall", "skip")
        ids = self.queued("c", "skip")
        log = self.work_said()
        self.assertEqual([self.state(i)["state"] for i in ids], ["queued", "queued"], log)
        self.assertEqual(self.gates(), 1, log)
        held = mq.Queue(self.repo).held()
        self.assertTrue(held.startswith("for a session, not the owner: gate ran too few tests, though every test it "
                                        "ran passed, and no entry of the batch's 2 removes tests"), held)
        self.assertIn("gate executed 2 tests, 1 fewer than the 3", held)
        self.assertIn("No entry is sent back", held)
        self.assertNotIn(": bisecting", log)

    def test_any_other_failure_after_passing_tests_is_no_verdict_and_sends_back_at_the_bound(self):
        self.commit("c", "c")
        self.commit("teardown", "teardown")
        ids = self.queued("c", "teardown")
        log = self.work_said()
        self.assertIn("gate failed though qk unittest's verdict was OK, so its tests gave no verdict", log)
        self.assertIn(f"run 1 of at most {mq.NO_VERDICT_RUNS}", log)
        self.assertNotIn(": bisecting", log)
        self.assertNotIn("fail together", log)
        self.assertEqual([self.state(i)["state"] for i in ids], ["failed", "failed"], log)
        self.assertEqual(self.gates(), mq.NO_VERDICT_RUNS, log)
        self.assertIsNone(mq.Queue(self.repo).held())
        why = self.state(ids[0])["why"]
        self.assertIn(f"the lanes gave no verdict in {mq.NO_VERDICT_RUNS} runs in a row", why)
        self.assertIn("gate failed though qk unittest's verdict was OK", why)


class NoVerdict(Fixture):
    @contextlib.contextmanager
    def no_verdict_for(self, runs, status="3"):
        """The gate gives no verdict, exiting status, for its first runs runs, and passes after."""
        self.status.write_text(status)
        real = mq.Worker.gate
        def gate(worker, *arguments):
            result = real(worker, *arguments)
            if self.gates() >= runs: self.status.unlink(missing_ok=True)
            return result
        with patch.object(mq.Worker, "gate", gate): yield

    def test_a_lane_that_merely_exits_as_a_lost_local_network_does_counts_and_ends_no_worker(self):
        """The negative control of vocaro-engineering#300: only the worker's own answer ends it, or a lane exiting 69
        would end worker after worker with nothing to bound them. The lane exits so under a real qk run."""
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.no_verdict_for(1, str(mq.remote.LOST_ACCESS)), \
                patch.object(mq.remote, "local_network_lost", return_value=None), \
                contextlib.redirect_stdout(io.StringIO()) as said:
            self.assertEqual(self.work(), 0)
        self.assertEqual(self.gates(), 2)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertIn(f"no verdict (exit {mq.remote.LOST_ACCESS}), run 1 of at most {mq.NO_VERDICT_RUNS}", said.getvalue())
        self.assertNotIn("exits, holding nothing", said.getvalue())

    def test_a_retry_names_the_admission_refusal_before_the_last_attempt(self):
        self.commit("a", "a")
        [identifier] = self.add("a")
        refusal = ("gate not run on this Mac (fixture): Resource queue busy on fixture: "
                   "it waits behind a whole-host lane holding the host: another gate; waited 5400s; nothing ran")
        Path(f"{self.status}.screen").write_text(f"queuekeeper: {refusal}\n")
        with self.no_verdict_for(1, str(mq.resources.BUSY)), contextlib.redirect_stdout(io.StringIO()) as said:
            self.assertEqual(self.work(), 0)
        self.assertEqual(self.gates(), 2)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertRegex(said.getvalue(), rf"no verdict \(exit 75\).*run 1 of at most {mq.NO_VERDICT_RUNS}: not run: ")
        self.assertIn(f"not run: {refusal}", said.getvalue())

    def test_unmeasured_batch_comes_back_and_later_entries_still_land(self):
        self.commit("a", "a")
        self.commit("b", "b")
        [first] = self.add("a")
        real = mq.Worker.gate
        attempted = []

        def gate(worker, *arguments, **options):
            attempted.append(worker.gating)
            result = real(worker, *arguments, **options)
            if self.gates() == 1:
                later.extend(self.add("b"))
            if self.gates() == mq.NO_VERDICT_RUNS:
                self.status.unlink()
            return result

        later = []
        self.status.write_text("3")
        with patch.object(mq.Worker, "gate", gate), patch.object(mq.Worker, "stack"), patch("builtins.print"):
            self.assertEqual(self.work(), 0)
        self.assertEqual(self.gates(), mq.NO_VERDICT_RUNS + 1)
        self.assertIsNone(mq.Queue(self.repo).held())
        entry = self.state(first)
        self.assertEqual(entry["state"], "failed")
        self.assertEqual(self.state(later[0])["state"], "landed")
        self.assertNotIn("a", self.main_files())
        self.assertIn("b", self.main_files())
        self.assertEqual(attempted, [(first,)] * mq.NO_VERDICT_RUNS + [(later[0],)])
        why = entry["why"]
        self.assertIn(f"the lanes gave no verdict in 3 runs in a row, the last with exit 3 on this Mac "
                      f"({offload_host.mac_name()}), unmeasured, with no test its log names, the most "
                      "queue.noVerdictRuns allows (3)", why)
        self.assertIn("gate takes one shared slot of heavy-work admission", why)
        out = agent_results.entry_result(mq.Queue(self.repo), entry)
        self.assertEqual(out["failure"]["category"], "gate-no-verdict")
        self.assertEqual(out["recovery"]["reasonCode"], "entry-no-verdict")
        log = agent_logs.selected(out["artifacts"], "queue-doctor-log")
        self.assertIn("qk queue doctor:", Path(log["path"]).read_text())

    def test_a_lane_that_ran_out_of_disk_space_holds_at_once_and_blames_nobody(self):
        """vocaro/queuekeeper#162: a run again would fill the same disk, so the first such run holds, for a session to
        free space; the entry waits and is not sent back. Under a real qk run, the lane says what qk run says."""
        self.status.write_text("3")
        Path(f"{self.status}.screen").write_text(f"{mq.lane_results.OUT_OF_SPACE}gate on this Mac (Fixture-Mac): the "
                                                 "volume of /System/Volumes/Data has 1.0 GiB free")
        self.commit("a", "a")
        [identifier] = self.add("a")
        with contextlib.redirect_stdout(io.StringIO()): self.assertEqual(self.work(), 0)
        self.assertEqual(self.gates(), 1)
        self.assertEqual(self.state(identifier)["state"], "queued")
        held = mq.Queue(self.repo).held()
        self.assertTrue(held.startswith("for a session, not the owner: a lane ran out of disk space"), held)
        self.assertIn("gate on this Mac (Fixture-Mac): the volume of /System/Volumes/Data has 1.0 GiB free", held)
        self.assertIn("no entry is sent back", held)
        self.assertIn("qk disk --delete", held)
        # Space freed and the queue resumed, the entry lands.
        self.status.unlink()
        Path(f"{self.status}.screen").unlink()
        with patch.object(mq.Queue, "start"), patch("builtins.print"): mq.resume(self.repo)
        self.work()
        self.assertEqual(self.state(identifier)["state"], "landed")

    def test_a_lane_that_waited_for_disk_space_says_so_in_its_no_verdict(self):
        worker = mq.Worker(mq.Queue(self.repo))
        log = self.base / "gate.log"
        log.write_text("queuekeeper: gate waits: this Mac (studio) is short of disk space: the volume of / has 7.0 GiB "
                       "free\nqueuekeeper: gate not run on this Mac (studio): this Mac (studio) is short of disk space: "
                       "the volume of / has 7.0 GiB free\n")
        self.assertEqual(worker.no_verdict_cause(75, log),
                         "not run: gate not run on this Mac (studio): this Mac (studio) is short of disk space: the volume "
                         "of / has 7.0 GiB free")
        # A wait for disk space is distinct from running out during execution.
        self.assertIsNone(worker.out_of_space(3, log))
        self.assertIsNone(worker.out_of_space(75, log))

    def test_only_an_unmeasured_heavy_lane_that_shares_its_mac_is_pointed_at_exclusive_admission(self):
        """vocaro/queuekeeper#28: admission looks only at a run's start, so heavy work admitted beside a heavy lane
        afterwards can void a timing in it. A lane that holds its Mac, a self lane, which admits its own parts, a run
        that ran nothing or reached its limit, and no lane at all get no such hint."""
        worker = mq.Worker(mq.Queue(self.repo))
        worker.lane_now = ("gate", time.time(), ())
        lanes = lambda gate: {"lanes": {"gate": gate}}
        self.assertIn("`qk admit --here --exclusive`", worker.shared_timing(3, lanes({"command": ["true"]})))
        self.assertIn("gate takes one shared slot", worker.shared_timing(3, lanes({"resource": "heavy"})))
        self.assertIn("gate takes one shared slot", worker.shared_timing(3, lanes({"resource": "light"})))
        for gate in ({"resource": "exclusive"}, {"resource": "self"}, {"wholeHost": True}, {"resource": "nonsense"}):
            with self.subTest(gate=gate): self.assertEqual(worker.shared_timing(3, lanes(gate)), "")
        self.assertEqual(worker.shared_timing(3, {"lanes": {}}), "")
        for status in (mq.resources.BUSY, mq.remote.MISCONFIGURED, mq.remote.LOST_ACCESS):
            with self.subTest(status=status): self.assertEqual(worker.shared_timing(status, lanes({})), "")
        worker.timed_out = "gate reached its limit: 7200s"
        self.assertEqual(worker.shared_timing(3, lanes({})), "")
        worker.timed_out, worker.lane_now = None, None
        self.assertEqual(worker.shared_timing(3, lanes({})), "")

    def test_no_verdict_is_retried_up_to_the_bound_and_lands_when_a_retry_passes(self):
        # Keep the default bound for an unmeasured lane. The other exit statuses need one
        # no-verdict run each to prove they also retry and land on the last allowed run.
        for status, bound in (("3", mq.NO_VERDICT_RUNS), ("75", 2), ("78", 2)):
            with self.subTest(status=status):
                start = self.gates()
                self.commit(f"a{status}", f"a{status}")
                [identifier] = self.add(f"a{status}")
                with patch.object(mq, "NO_VERDICT_RUNS", bound), self.no_verdict_for(start + bound - 1, status), \
                        contextlib.redirect_stdout(io.StringIO()) as said:
                    self.assertEqual(self.work(), 0)
                self.assertEqual(self.gates() - start, bound)
                self.assertEqual(self.state(identifier)["state"], "landed")
                self.assertIsNone(mq.Queue(self.repo).held())
                for attempt in range(1, bound):
                    self.assertIn(f"no verdict (exit {status}), run {attempt} of at most {bound}", said.getvalue())

    LOCKED = ("queuekeeper: screen: locked (console user t, CGSSessionScreenIsLocked yes, IOConsoleLocked no) on this Mac "
              "(Fixture-Mac) at 01:58:02")
    UNLOCKED = ("queuekeeper: screen: unlocked (console user t, CGSSessionScreenIsLocked absent, IOConsoleLocked no) on this "
                "Mac (Fixture-Mac) at 01:39:20")

    @contextlib.contextmanager
    def commands(self):
        """The commands the worker runs, each lane's qk run among them."""
        real, ran = mq.processes.run, []
        def recorded(command, *arguments, **options):
            ran.append(list(command))
            return real(command, *arguments, **options)
        with patch.object(mq.processes, "run", recorded): yield ran

    def lane_runs(self, ran):
        """The qk run commands of ran: the fixture's project has no preflight lane, so these are the lanes judged."""
        return [command for command in ran if command[:3] == ["python3", "scripts/qk", "run"]]

    def test_a_run_a_locked_screen_voided_does_not_count_and_the_next_goes_only_to_an_unlocked_mac(self):
        """vocaro-engineering#293, the owner's decision of 2026-11-08: more runs than the bound, each unmeasured behind
        a locked screen, hold nothing; each run after one goes to qk run --unlocked-screen. The first, undeclared,
        is judged by its log's last screen reading; each later one, which queuekeeper reads the screen for, by its receipt's
        screenAfter, from a recording that reads unlocked as the run starts and locked once the lane has run."""
        screen = Path(f"{self.status}.screen")
        screen.write_text(f"{self.UNLOCKED}\n{self.LOCKED}\n")
        screens = Path(__file__).resolve().parents[1] / "mac_desktop/fixtures/ScreenState"
        recording = self.base / "screen.plist"
        real = mq.Worker.gate
        def unlocked_at_start(worker, *arguments, **options):
            shutil.copy(screens / "this-mac-unlocked.plist", recording)
            return real(worker, *arguments, **options)
        self.commit("a", "a")
        [identifier] = self.add("a")
        with patch.dict(os.environ, {offload_host.SCREEN_RECORDING: str(recording),
                                     "FIXTURE_LOCK_DURING_RUN": str(screens / "this-mac-locked-idle.plist")}), \
                patch.object(mq.Worker, "gate", unlocked_at_start), self.no_verdict_for(mq.NO_VERDICT_RUNS + 1), \
                self.commands() as ran, contextlib.redirect_stdout(io.StringIO()) as said:
            self.assertEqual(self.work(), 0)
        self.assertEqual(self.gates(), mq.NO_VERDICT_RUNS + 2)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertIsNone(mq.Queue(self.repo).held())
        self.assertIn("no verdict (exit 3) on this Mac", said.getvalue())
        self.assertEqual(said.getvalue().count("gate ran behind a locked screen"), mq.NO_VERDICT_RUNS + 1)
        self.assertIn(f"gate ran behind a locked screen, the last reading {self.LOCKED[len("queuekeeper: "):]}, which queue.noVerdictRuns "
                      "does not count; its next run goes only to a Mac whose screen reads unlocked", said.getvalue())
        self.assertRegex(said.getvalue(), r"gate ran behind a locked screen, the last reading screen: locked \(console "
                         r"user owner, CGSSessionScreenIsLocked yes, IOConsoleLocked no\) on this Mac \(\S+\) at ")
        self.assertNotIn("of at most", said.getvalue())
        runs = self.lane_runs(ran)
        self.assertEqual(runs[0], ["python3", "scripts/qk", "run", "gate"])
        self.assertEqual(runs[1:], [["python3", "scripts/qk", "run", "--unlocked-screen", "gate"]] * (len(runs) - 1))
        self.assertIn("gate: only on a Mac whose screen reads unlocked", said.getvalue())

    def test_a_run_whose_last_reading_is_unlocked_or_that_ran_nothing_still_counts(self):
        """The negative controls: an unmeasured run whose last reading is unlocked, and a run that exits 75 or 78
        with a lock in its log, as one that waited out the overflow for an unlocked Mac does, count as before."""
        # A bound of one distinguishes a counted run from a screen-related exemption immediately.
        screen = Path(f"{self.status}.screen")
        for status, said_screen in (("3", f"{self.LOCKED}\n{self.UNLOCKED}\n"), ("75", f"{self.LOCKED}\n"),
                                    ("78", f"{self.LOCKED}\n")):
            with self.subTest(status=status):
                start = self.gates()
                screen.write_text(said_screen)
                self.commit(f"a{status}", f"a{status}")
                [identifier] = self.add(f"a{status}")
                with patch.object(mq, "NO_VERDICT_RUNS", 1), self.no_verdict_for(start + 1, status), \
                        self.commands() as ran, contextlib.redirect_stdout(io.StringIO()) as said:
                    self.assertEqual(self.work(), 0)
                self.assertEqual(self.gates() - start, 1)
                self.assertEqual(self.state(identifier)["state"], "failed")
                self.assertIn("the most queue.noVerdictRuns allows (1)", self.state(identifier)["why"])
                self.assertNotIn("locked screen", said.getvalue())
                self.assertTrue(all("--unlocked-screen" not in command for command in self.lane_runs(ran)))

    def test_the_receipts_screen_after_decides_over_the_log(self):
        """The last reading: a fresh receipt's screenAfter, which queuekeeper writes once a lane that depends on the screen
        ends unmeasured; else the log's last `screen:` line. A receipt older than the run is not the run's."""
        receipt = self.base / ".build/queuekeeper/gate/source.json"
        receipt.parent.mkdir(parents=True)
        log = self.base / "gate.log"
        log.write_text(f"{self.UNLOCKED}\n")
        began = time.time() - 60
        worker = type("Runner", (), {"checkout": self.base, "lane_now": ("gate", began, ())})()
        locked = {"state": "locked", "user": "t", "CGSSessionScreenIsLocked": True, "IOConsoleLocked": False,
                  "mac": "Fixture-Mac", "at": "01:58:02"}
        receipt.write_text(json.dumps({"screenAfter": locked}))
        self.assertEqual(mq.Worker.screen_locked(worker, 3, log), ("gate", self.LOCKED[len("queuekeeper: "):]))
        receipt.write_text(json.dumps({"screenAfter": {**locked, "state": "unknown", "reason": "no console"}}))
        self.assertIsNone(mq.Worker.screen_locked(worker, 3, log))
        receipt.write_text(json.dumps({"screenAfter": {**locked, "state": "unlocked"}}))
        log.write_text(f"{self.LOCKED}\n")
        self.assertIsNone(mq.Worker.screen_locked(worker, 3, log))
        # A receipt from before the run: the log decides.
        os.utime(receipt, (began - 10, began - 10))
        self.assertEqual(mq.Worker.screen_locked(worker, 3, log), ("gate", self.LOCKED[len("queuekeeper: "):]))
        self.assertIsNone(mq.Worker.screen_locked(worker, 75, log))
        self.assertIsNone(mq.Worker.screen_locked(worker, 78, log))
        log.write_text("no reading\n")
        self.assertIsNone(mq.Worker.screen_locked(worker, 3, log))

    FAR_LOCKED = {"state": "locked", "user": "t", "CGSSessionScreenIsLocked": True, "IOConsoleLocked": False,
                  "mac": "Far-Mac", "at": "01:58:02"}
    FAR_READING = ("screen: locked (console user t, CGSSessionScreenIsLocked yes, IOConsoleLocked no) on "
                   "Far-Mac at 01:58:02")

    def dispatch_receipt(self, screen_after=None, **result):
        """What remote.execute writes to a dispatch's source.json: the lane's own receipt, which holds screenAfter, sits
        under result, as offload_host returns it from another Mac; nothing at the top level says the screen."""
        lane = {"outcome": "unmeasured", **({} if screen_after is None else {"screenAfter": screen_after})}
        return {"ranOn": "second Mac", "mac": "Far-Mac", "host": "far-mac", "lane": "gate",
                "result": {"exitStatus": 3, "outcome": "unmeasured", "receipt": lane, **result}}

    def farm_mac_run(self):
        """(a worker whose last gate began a minute ago, the source.json of that gate's dispatch, the log) laid out where
        the real remote.dispatch finds them."""
        began = time.time() - 60
        ran = (self.base / ".build/queuekeeper/remote" / mq.remote.DISPATCHES / "gate"
               / time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))
        ran.mkdir(parents=True)
        worker = type("Runner", (), {"checkout": self.base, "lane_now": ("gate", began, ())})()
        return worker, ran / "source.json", self.base / "gate.log"

    def test_a_farm_macs_reading_under_its_dispatchs_result_decides_over_the_log(self):
        """The lane's receipt from another Mac is under the dispatch's result, so the last reading is there."""
        worker, receipt, log = self.farm_mac_run()
        receipt.write_text(json.dumps(self.dispatch_receipt(self.FAR_LOCKED)))
        for said in ("no reading\n", f"{self.UNLOCKED}\n"):
            with self.subTest(log=said):
                log.write_text(said)
                self.assertEqual(mq.Worker.screen_locked(worker, 3, log), ("gate", self.FAR_READING))
        log.write_text(f"{self.LOCKED}\n")
        for state in ("unlocked", "unknown"):
            with self.subTest(state=state):
                receipt.write_text(json.dumps(self.dispatch_receipt({**self.FAR_LOCKED, "state": state})))
                self.assertIsNone(mq.Worker.screen_locked(worker, 3, log))
        # No admission and a host's configuration ran nothing, whatever the receipt reads.
        receipt.write_text(json.dumps(self.dispatch_receipt(self.FAR_LOCKED)))
        self.assertIsNone(mq.Worker.screen_locked(worker, 75, log))
        self.assertIsNone(mq.Worker.screen_locked(worker, 78, log))

    def test_a_dispatch_receipt_without_a_usable_reading_leaves_the_local_receipt_and_the_log(self):
        """A malformed or stale dispatch receipt says nothing: the local receipt, else the log's last line, decides.
        A screenAfter at a dispatch's top level is not where one is written, and is not read."""
        worker, receipt, log = self.farm_mac_run()
        base = self.dispatch_receipt()
        malformed = [{**base, "result": value} for value in (None, "text", 7, [])] \
            + [{**base, "result": {"exitStatus": 3, "receipt": value}} for value in (None, "text", 7, [])] \
            + [{**base, "result": {"exitStatus": 3}}, {"host": "far-mac"}, base] \
            + [self.dispatch_receipt(value) for value in ("locked", 7, [], {}, {"state": ""})] \
            + [{**base, "screenAfter": self.FAR_LOCKED}]
        local = self.base / ".build/queuekeeper/gate/source.json"
        local.parent.mkdir(parents=True)
        for shape in malformed:
            with self.subTest(shape=shape):
                receipt.write_text(json.dumps(shape))
                local.unlink(missing_ok=True)
                log.write_text(f"{self.LOCKED}\n")
                self.assertEqual(mq.Worker.screen_locked(worker, 3, log), ("gate", self.LOCKED[len("queuekeeper: "):]))
                log.write_text(f"{self.UNLOCKED}\n")
                self.assertIsNone(mq.Worker.screen_locked(worker, 3, log))
                local.write_text(json.dumps({"screenAfter": {**self.FAR_LOCKED, "mac": "Fixture-Mac"}}))
                self.assertEqual(mq.Worker.screen_locked(worker, 3, log), ("gate", self.LOCKED[len("queuekeeper: "):]))
        # A dispatch receipt from before the run is not the run's.
        local.unlink(missing_ok=True)
        log.write_text(f"{self.UNLOCKED}\n")
        receipt.write_text(json.dumps(self.dispatch_receipt(self.FAR_LOCKED)))
        os.utime(receipt, (worker.lane_now[1] - 10, worker.lane_now[1] - 10))
        self.assertIsNone(mq.Worker.screen_locked(worker, 3, log))

    def test_a_local_receipt_is_read_at_its_top_level_only(self):
        """A run on this Mac has no dispatch; its receipt holds screenAfter at the top level, and a result in it is
        not a dispatch's."""
        receipt = self.base / ".build/queuekeeper/gate/source.json"
        receipt.parent.mkdir(parents=True)
        log = self.base / "gate.log"
        log.write_text(f"{self.UNLOCKED}\n")
        worker = type("Runner", (), {"checkout": self.base, "lane_now": ("gate", time.time() - 60, ())})()
        here = {**self.FAR_LOCKED, "mac": "Fixture-Mac"}
        nested = {"result": {"receipt": {"screenAfter": here}}}
        receipt.write_text(json.dumps(nested))
        self.assertIsNone(mq.Worker.screen_locked(worker, 3, log))
        receipt.write_text(json.dumps({**nested, "screenAfter": here}))
        self.assertEqual(mq.Worker.screen_locked(worker, 3, log), ("gate", self.LOCKED[len("queuekeeper: "):]))
        receipt.write_text(json.dumps({**nested, "screenAfter": {**here, "state": "unlocked"}}))
        self.assertIsNone(mq.Worker.screen_locked(worker, 3, log))

    @contextlib.contextmanager
    def gated_on_farm_mac(self, reading):
        """Every gate reports a dispatch to another Mac whose receipt, written once the lane has run, reads reading,
        and whose run.log holds what the lane printed, while this Mac's own screen reads unlocked for qk run
        --unlocked-screen. The dispatch's results appear once each lane's qk run has ended, as remote.execute
        returns them; remote.dispatch only looks them up, as the real one does. The gate's watch and samplers look
        it up while the lane runs, which a busy host makes them do once a lane outlasts WATCH_POLL: here they look
        all through every lane, so the verdict cannot depend on when they do (vocaro/queuekeeper#17)."""
        screens = Path(__file__).resolve().parents[1] / "mac_desktop/fixtures/ScreenState"
        recording = self.base / "screen.plist"
        shutil.copy(screens / "this-mac-unlocked.plist", recording)
        ran = self.base / "dispatches/far"
        ran.mkdir(parents=True)
        real = mq.processes.run
        def returned(command, *arguments, cwd=None, **options):
            try: return real(command, *arguments, cwd=cwd, **options)
            finally:
                if list(command[:3]) == ["python3", "scripts/qk", "run"]:
                    with contextlib.suppress(FileNotFoundError):
                        (Path(cwd) / ".build/queuekeeper" / command[-1] / "run.log").rename(ran / "run.log")
                    (ran / "source.json").write_text(json.dumps(self.dispatch_receipt(reading)))
        with patch.dict(os.environ, {offload_host.SCREEN_RECORDING: str(recording)}), \
                patch.object(mq.processes, "run", returned), \
                patch.object(mq.remote, "dispatch", return_value=(ran, "far-mac")), \
                patch.object(mq, "WATCH_POLL", 0.01), patch.object(mq, "BUSY_SAMPLE", 0.01):
            yield

    def test_a_farm_mac_run_its_receipt_reads_locked_does_not_count_and_the_next_needs_an_unlocked_mac(self):
        """With no `screen:` line in the log, only the reading under the dispatch's result can say the screen was
        locked: more runs than the bound hold nothing, and each after the first goes to qk run --unlocked-screen."""
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.gated_on_farm_mac(self.FAR_LOCKED), self.no_verdict_for(mq.NO_VERDICT_RUNS + 1), \
                self.commands() as ran, contextlib.redirect_stdout(io.StringIO()) as said:
            self.assertEqual(self.work(), 0)
        # The worker's log says which run went otherwise.
        self.assertEqual(self.gates(), mq.NO_VERDICT_RUNS + 2, said.getvalue())
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertIsNone(mq.Queue(self.repo).held())
        self.assertIn(f"gate ran behind a locked screen, the last reading {self.FAR_READING}, which "
                      "queue.noVerdictRuns does not count", said.getvalue())
        self.assertNotIn("of at most", said.getvalue())
        runs = self.lane_runs(ran)
        self.assertEqual(runs[0], ["python3", "scripts/qk", "run", "gate"])
        self.assertEqual(runs[1:], [["python3", "scripts/qk", "run", "--unlocked-screen", "gate"]] * (len(runs) - 1))

    def test_a_farm_mac_run_its_receipt_reads_unlocked_counts_though_its_log_says_locked(self):
        """The negative control: the receipt's last reading wins over a log line saying locked, so the run counts."""
        Path(f"{self.status}.screen").write_text(f"{self.LOCKED}\n")
        self.status.write_text("3")
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.gated_on_farm_mac({**self.FAR_LOCKED, "state": "unlocked"}), self.commands() as ran, \
                contextlib.redirect_stdout(io.StringIO()) as said:
            self.assertEqual(self.work(), 0)
        self.assertEqual(self.gates(), mq.NO_VERDICT_RUNS)
        self.assertEqual(self.state(identifier)["state"], "failed")
        self.assertIn("the most queue.noVerdictRuns allows (3)", self.state(identifier)["why"])
        self.assertNotIn("locked screen", said.getvalue())
        self.assertTrue(all("--unlocked-screen" not in command for command in self.lane_runs(ran)))
        self.status.unlink()

    def test_a_worker_that_takes_over_keeps_the_lanes_that_need_an_unlocked_screen(self):
        queue = mq.Queue(self.repo)
        self.assertNotIn("unlocked", mq.Worker(queue).progress())
        worker = mq.Worker(queue, {"limit": None, "suspects": 0, "no_verdicts": 1, "unlocked": ["gate"]})
        self.assertEqual(worker.unlocked, {"gate"})
        self.assertEqual(worker.progress()["unlocked"], ["gate"])
        self.assertIs(mq.Beside(worker).unlocked, worker.unlocked)

    @contextlib.contextmanager
    def gates_that(self, steps, then=None):
        """Each gate in turn does what steps says, and every gate past them passes: "locked", unmeasured with a screen
        that reads unlocked at the start and locks during the run; "began locked", which reads locked from the first;
        "unmeasured", with a screen unlocked throughout; "waited", exit 75, as a run that waits out the overflow for
        an unlocked Mac does; and "passed", a verdict. then, if given, is called once the gate at its step has run."""
        screens = Path(__file__).resolve().parents[1] / "mac_desktop/fixtures/ScreenState"
        recording, during = self.base / "screen.plist", self.base / "during.plist"
        screen = Path(f"{self.status}.screen")
        locked, unlocked = screens / "this-mac-locked-idle.plist", screens / "this-mac-unlocked.plist"
        status = {"locked": "3", "began locked": "3", "unmeasured": "3", "waited": "75"}
        said = {"locked": f"{self.UNLOCKED}\n{self.LOCKED}\n", "began locked": f"{self.LOCKED}\n"}
        real, gated = mq.Worker.gate, []
        def gate(worker, *arguments, **options):
            step = steps[len(gated)] if len(gated) < len(steps) else "passed"
            gated.append(step)
            shutil.copy(unlocked, recording)
            shutil.copy(locked if step in said else unlocked, during)
            screen.write_text(said.get(step, f"{self.UNLOCKED}\n"))
            if step in status: self.status.write_text(status[step])
            else: self.status.unlink(missing_ok=True)
            result = real(worker, *arguments, **options)
            if then and len(gated) - 1 == then[0]: then[1]()
            return result
        with patch.dict(os.environ, {offload_host.SCREEN_RECORDING: str(recording),
                                     "FIXTURE_LOCK_DURING_RUN": str(during)}), patch.object(mq.Worker, "gate", gate):
            yield gated

    def test_the_fifth_locked_run_in_a_row_sends_back_the_batch(self):
        """vocaro-engineering#293, the owner's decision of 2026-11-09: the cap on the runs queue.noVerdictRuns does
        not count. The send-back names the lane, the screen reading and the Mac."""
        self.assertEqual(mq.LOCKED_SCREEN_RUNS, 5)
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.gates_that(["locked"] * mq.LOCKED_SCREEN_RUNS), contextlib.redirect_stdout(io.StringIO()) as said:
            self.assertEqual(self.work(), 0)
        self.assertEqual(self.gates(), 5)
        self.assertEqual(self.state(identifier)["state"], "failed")
        self.assertNotIn("a", self.main_files())
        held = self.state(identifier)["why"]
        self.assertTrue(held.startswith("gate ended behind a screen that locked during "
                                        "the run in 5 runs in a row, the last reading screen: locked ("), held)
        self.assertRegex(held, r"the last reading screen: locked \(console user owner, CGSSessionScreenIsLocked "
                         r"yes, IOConsoleLocked no\) on this Mac \(\S+\) at \S+, the most LOCKED_SCREEN_RUNS allows "
                         r"\(5\); see ")
        self.assertIsNone(mq.Queue(self.repo).held())
        self.assertIn("4 such runs in a row, and 5 send the batch back", said.getvalue())
        self.assertNotIn("the most queue.noVerdictRuns allows", held)

    def test_four_locked_runs_in_a_row_hold_nothing(self):
        """The negative control of the fifth: the run after the fourth passes, and the entry lands."""
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.gates_that(["locked"] * (mq.LOCKED_SCREEN_RUNS - 1)), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.work(), 0)
        self.assertEqual(self.gates(), 5)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertIsNone(mq.Queue(self.repo).held())

    def test_a_run_that_began_behind_the_lock_is_not_counted_toward_the_cap(self):
        """The owner's condition: unlocked at its start and locked by its end. The first run, not placed and locked
        from its first reading, is not one of the five; the five placed after it are."""
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.gates_that(["began locked"] + ["locked"] * mq.LOCKED_SCREEN_RUNS), \
                contextlib.redirect_stdout(io.StringIO()) as said:
            self.assertEqual(self.work(), 0)
        self.assertEqual(self.gates(), 6)
        self.assertEqual(self.state(identifier)["state"], "failed")
        self.assertIn("LOCKED_SCREEN_RUNS does not count it either", said.getvalue())
        self.assertIn("in 5 runs in a row", self.state(identifier)["why"])

    def test_a_verdict_between_locked_runs_starts_the_cap_again(self):
        """A pass is a verdict: four locked runs on each side of it, eight in all, hold nothing. The second entry
        arrives once the first's passing gate has run, so that one worker goes on, with its count, to gate it."""
        self.commit("a", "a"); self.commit("b", "b")
        [first] = self.add("a")
        arrived = []
        steps = ["locked"] * 4 + ["passed"] + ["locked"] * 4
        with self.gates_that(steps, (4, lambda: arrived.extend(self.add("b")))) as gated, \
                contextlib.redirect_stdout(io.StringIO()) as said:
            self.assertEqual(self.work(), 0)
        self.assertIsNone(mq.Queue(self.repo).held())
        self.assertEqual(gated, steps + ["passed"])
        self.assertEqual(said.getvalue().count("4 such runs in a row"), 2)
        self.assertNotIn("5 such runs", said.getvalue())
        self.assertEqual([self.state(identifier)["state"] for identifier in (first, *arrived)], ["landed", "landed"])

    def test_a_no_verdict_of_another_cause_starts_the_cap_again_and_still_counts_as_before(self):
        self.commit("a", "a")
        [identifier] = self.add("a")
        steps = ["locked"] * 4 + ["unmeasured"] + ["locked"] * 4
        with self.gates_that(steps), contextlib.redirect_stdout(io.StringIO()) as said:
            self.assertEqual(self.work(), 0)
        self.assertEqual(self.gates(), len(steps) + 1)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertIsNone(mq.Queue(self.repo).held())
        self.assertIn("no verdict (exit 3), run 1 of at most 3", said.getvalue())
        self.assertEqual(said.getvalue().count("4 such runs in a row"), 2)
        self.assertNotIn("5 such runs", said.getvalue())

    def test_a_run_that_waited_for_an_unlocked_mac_neither_adds_to_the_cap_nor_ends_it(self):
        """Exit 75 from a lane sent only to an unlocked Mac ran nothing: the row goes on across it, so three locked
        runs, the wait and two more send back at the fifth locked run, the sixth gate. The wait counts toward noVerdictRuns."""
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.gates_that(["locked"] * 3 + ["waited"] + ["locked"] * 2), \
                contextlib.redirect_stdout(io.StringIO()) as said:
            self.assertEqual(self.work(), 0)
        self.assertEqual(self.gates(), 6)
        self.assertEqual(self.state(identifier)["state"], "failed")
        self.assertIn("in 5 runs in a row", self.state(identifier)["why"])
        self.assertIn("no verdict (exit 75), run 1 of at most 3", said.getvalue())

    def test_whether_a_run_began_unlocked_is_read_from_its_placement_or_its_first_reading(self):
        worker, log = mq.Worker(mq.Queue(self.repo)), self.base / "gate.log"
        declared = {"lanes": {"gate": {"unlockedScreen": True}}}
        log.write_text(f"{self.UNLOCKED}\n{self.LOCKED}\n")
        self.assertTrue(worker.began_unlocked("gate", log, {}))
        log.write_text(f"{self.LOCKED}\n{self.UNLOCKED}\n")
        self.assertFalse(worker.began_unlocked("gate", log, {}))
        log.write_text("no reading\n")
        self.assertFalse(worker.began_unlocked("gate", log, {}))
        self.assertFalse(worker.began_unlocked("gate", self.base / "no-such.log", {}))
        self.assertFalse(worker.began_unlocked("gate", None, {}))
        self.assertTrue(worker.began_unlocked("gate", log, declared))
        worker.sent_unlocked = True
        self.assertTrue(worker.began_unlocked("gate", log, {}))

    def test_a_bisects_older_half_is_placed_as_any_gate_is_and_so_leaves_a_mac_whose_locked_screen_keeps_its_lane(self):
        """vocaro/queuekeeper#205 and #211: the older half runs with no placement option, so qk run offers it to every
        Mac as it does an ordinary gate, and a lane that needs an unlocked screen goes to a Mac whose screen reads
        unlocked. Until #211 it ran with --here, and left this Mac only by a rule of its own while this Mac's screen
        was locked (Worker.screen_placement)."""
        self.assertFalse(hasattr(mq.Worker, "screen_placement"))
        for name in ("a", "bad", "c", "d"): self.commit(name, name)
        self.add("a", "bad", "c", "d")
        with patch.object(mq, "offerable", lambda config, lanes: None), self.commands() as ran, \
                contextlib.redirect_stdout(io.StringIO()) as said:
            self.work()
        runs = self.lane_runs(ran)
        # The batch, then each older half, with no option; each newer half only where the older half's Mac is not.
        self.assertIn(["python3", "scripts/qk", "run", "gate"], runs)
        self.assertNotIn(["python3", "scripts/qk", "run", "--here", "gate"], runs)
        self.assertIn(["python3", "scripts/qk", "run", "--offload-wait", "gate"], runs)
        self.assertIn("newer half: its lanes wait for admission on a Mac other than the older half's", said.getvalue())

    def test_a_worker_that_takes_over_keeps_its_count_of_locked_runs(self):
        queue = mq.Queue(self.repo)
        self.assertNotIn("locked_screens", mq.Worker(queue).progress())
        worker = mq.Worker(queue, {"limit": None, "suspects": 0, "no_verdicts": 0, "locked_screens": 3})
        self.assertEqual(worker.locked_screens, 3)
        self.assertEqual(worker.progress()["locked_screens"], 3)

    def test_a_failure_is_not_read_as_no_verdict(self):
        self.status.write_text("1")
        self.commit("a", "a")
        [identifier] = self.add("a")
        self.work()
        self.assertEqual(self.state(identifier)["state"], "failed")
        # Never retried: one gate, and no hold.
        self.assertEqual(self.gates(), 1)
        self.assertIsNone(mq.Queue(self.repo).held())

    def test_a_failure_after_no_verdict_comes_back_and_is_not_retried(self):
        self.status.write_text("3")
        real = mq.Worker.gate
        def gate(worker, *arguments):
            result = real(worker, *arguments)
            self.status.write_text("1")
            return result
        self.commit("a", "a")
        [identifier] = self.add("a")
        with patch.object(mq.Worker, "gate", gate), patch("builtins.print"): self.assertEqual(self.work(), 0)
        self.assertEqual(self.gates(), 2)
        self.assertEqual(self.state(identifier)["state"], "failed")
        self.assertIsNone(mq.Queue(self.repo).held())

    def test_a_verdict_starts_the_count_again(self):
        """Two runs with no verdict, a pass, then two more: no hold, though four gave none."""
        self.commit("a", "a"); self.commit("b", "b")
        [first] = self.add("a")
        with self.no_verdict_for(2), patch("builtins.print"): self.work()
        self.assertEqual(self.state(first)["state"], "landed")
        [second] = self.add("b")
        with self.no_verdict_for(5), patch("builtins.print"): self.work()
        self.assertEqual(self.state(second)["state"], "landed")
        self.assertEqual(self.gates(), 6)
        self.assertIsNone(mq.Queue(self.repo).held())

    def test_the_bound_is_mains_queue_no_verdict_runs(self):
        self.assertEqual(mq.NO_VERDICT_RUNS, 3)
        self.assertEqual(mq.no_verdict_runs({}), 3)
        self.assertEqual(mq.no_verdict_runs({"queue": {}}), 3)
        self.assertEqual(mq.no_verdict_runs(self.config()), 3)
        for runs in (1, 5):
            with self.subTest(runs=runs):
                run(self.repo, "git", "fetch", "-q", "origin")
                run(self.repo, "git", "checkout", "-q", "-B", "main", "origin/main")
                config = self.config()
                config["queue"]["noVerdictRuns"] = runs
                self.publish(config, f"Allow {runs} runs with no verdict")
                self.assertEqual(mq.no_verdict_runs(self.config()), runs)
                start = self.gates()
                self.status.write_text("3")
                self.commit(f"a{runs}", f"a{runs}")
                [identifier] = self.add(f"a{runs}")
                with patch("builtins.print"): self.assertEqual(self.work(), 0)
                self.assertEqual(self.gates() - start, runs)
                self.assertEqual(self.state(identifier)["state"], "failed")
                self.assertIn(f"in {runs} run{'' if runs == 1 else 's'} in a row, the last with exit 3", self.state(identifier)["why"])
                self.assertIn(f"the most queue.noVerdictRuns allows ({runs})", self.state(identifier)["why"])
                mq.remove(mq.Queue(self.repo), identifier, quiet=True)

    def test_a_branch_cannot_raise_its_own_bound(self):
        """Read from main, as every queue setting is: a batch that sets 5 still comes back after main's 3."""
        self.status.write_text("3")
        config = self.config()
        config["queue"]["noVerdictRuns"] = 5
        run(self.repo, "git", "fetch", "-q", "origin")
        run(self.repo, "git", "checkout", "-q", "-B", "raise", "origin/main")
        (self.repo / "queuekeeper.json").write_text(json.dumps(config))
        run(self.repo, "git", "add", "queuekeeper.json")
        run(self.repo, "git", "commit", "-qm", "Raise the bound")
        [identifier] = self.add("raise")
        with patch("builtins.print"): self.work()
        self.assertEqual(self.gates(), 3)
        self.assertEqual(self.state(identifier)["state"], "failed")

    def test_the_setting_must_be_a_positive_integer(self):
        start = {"schemaVersion": 1, "name": "fixture", "integration": "direct-main",
                 "lanes": {"gate": {"command": ["true"]}}}
        for runs in (1, 3, 10):
            with self.subTest(runs=runs):
                configuration.parse(json.dumps({**start, "queue": {"noVerdictRuns": runs}}))
        for runs in (0, -1, 2.5, "3", True, None):
            with self.subTest(runs=runs), self.assertRaisesRegex(ValueError, "queue.noVerdictRuns"):
                configuration.parse(json.dumps({**start, "queue": {"noVerdictRuns": runs}}))

    def test_the_schema_knows_the_setting(self):
        # The schema's queue takes no property it does not list, so a project that set the bound failed it.
        queue = json.loads((ROOT / "schemas/queuekeeper.schema.json").read_text())["properties"]["queue"]
        self.assertFalse(queue["additionalProperties"])
        self.assertEqual(queue["properties"]["noVerdictRuns"]["type"], "integer")
        self.assertEqual(queue["properties"]["noVerdictRuns"]["minimum"], 1)

    def test_the_reason_explains_why_the_last_run_gave_no_verdict(self):
        worker = mq.Worker(mq.Queue(self.repo))
        log = self.base / "gate.log"
        log.write_text("test_voices (m.Voices) ... skipped 'Unmeasured: the voices were not ready in 500 s'\n"
                       "test_more (m.Voices) ... skipped 'Unmeasured: the voices were not ready in 500 s'\n"
                       "test_more (m.Voices) ... skipped 'Unmeasured: the voices were not ready in 500 s'\n")
        self.assertEqual(worker.no_verdict_cause(3, log),
                         "unmeasured: 2 tests, the first test_voices (m.Voices) ... skipped 'Unmeasured: the voices "
                         "were not ready in 500 s'")
        log.write_text("test_voices (m.Voices) ... skipped 'Unmeasured: " + "x" * 400 + "'\n")
        self.assertEqual(len(worker.no_verdict_cause(3, log).split("the first ", 1)[1]), mq.UNMEASURED_SHOWN)
        self.assertEqual(worker.no_verdict_cause(3, self.base / "missing.log"), "unmeasured, with no test its log names")
        self.assertEqual(worker.no_verdict_cause(75, log), "no admission: the host stayed at capacity")
        self.assertIn("configuration", worker.no_verdict_cause(78, log))
        # A Mac that could not provision its checkout is named, with its provision lane's log (vocaro/queuekeeper#19).
        unprovisioned = ("this Mac (studio) could not provision its checkout: the provision lane exited 4 in /work, so "
                         "gate did not run here; its log is /work/.build/queuekeeper/provision/run.log")
        log.write_text(f"heavy: slot 0\nqueuekeeper: gate not run: {unprovisioned}\nqueuekeeper: to fix it, give this Mac "
                       "what provisioning needs\n")
        self.assertEqual(worker.no_verdict_cause(78, log), unprovisioned)
        self.assertEqual(worker.no_verdict_cause(75, log), "no admission: the host stayed at capacity")
        # A group's member not run for a locked screen gives its reason, quoted in the group's log (#200).
        refusal = ("mac-test-suites not run on this Mac (studio): the lane needs an unlocked screen, and this Mac's is "
                   "locked; release the outer admission before waiting for an unlocked screen and retry")
        log.write_text(f"RUN mac-suites\nNOT RUN mac-suites (2s, status 75): /x/mac-suites.log\n    queuekeeper: "
                       f"screen: locked on this Mac\n    queuekeeper: {refusal}\nqk run gate: not run (exit 75), on this Mac\n")
        self.assertEqual(worker.no_verdict_cause(75, log), f"not run: {refusal}")
        worker.timed_out = "gate reached its limit: after 7200 s"
        self.assertEqual(worker.no_verdict_cause(3, log), "unmeasured: gate reached its limit: after 7200 s")

    TIMED_OUT = "Owned command timed out after 2400s: python3; it and its observed descendants ended"
    NOTHING = "unmeasured, with no test its log names"

    def stopped_worker(self):
        """A worker whose gate stopped at the lane gate a minute ago, the log that names no test, and where the
        lane's receipt goes (#288)."""
        checkout = Path(tempfile.mkdtemp(dir=self.base))
        worker = mq.Worker.__new__(mq.Worker)
        worker.checkout, worker.timed_out, worker.lane_now = checkout, None, ("gate", time.time() - 60, ())
        log = checkout / "gate.log"
        log.write_text("test_last (m.Passed) ... ok\n")
        receipt = checkout / ".build/queuekeeper/gate/source.json"
        receipt.parent.mkdir(parents=True)
        return worker, log, receipt

    def dispatch_of(self, worker, said):
        """Another Mac's dispatch of the worker's gate, whose receipt holds said, or which has none for None."""
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        run = worker.checkout / f".build/queuekeeper/remote/.dispatches/gate/{stamp}-1"
        run.mkdir(parents=True)
        if said is not None: (run / "source.json").write_text(said)
        return run

    def test_a_no_verdict_send_back_quotes_the_error_its_own_receipt_records(self):
        """A lane that reached its own limit: the real qk run, its real receipt, and a log that just stops (#288)."""
        config = self.config()
        config["lanes"]["gate"] = {"command": ["python3", "-c", "import time; time.sleep(120)"], "timeout": 2}
        config["queue"]["noVerdictRuns"] = 1
        self.publish(config)
        self.commit("a", "a")
        [identifier] = self.add("a")
        with patch("builtins.print"): self.work()
        self.assertEqual(self.state(identifier)["state"], "failed")
        held = self.state(identifier)["why"]
        self.assertIn(f"the last with exit 3 on this Mac ({offload_host.mac_name()}), unmeasured: Owned command timed "
                      "out after 2s: python3", held)
        self.assertNotIn(self.NOTHING, held)

    def test_the_cause_is_the_receipts_error_when_the_log_names_no_test(self):
        worker, log, receipt = self.stopped_worker()
        receipt.write_text(json.dumps({"outcome": "unmeasured", "error": self.TIMED_OUT}))
        self.assertEqual(worker.no_verdict_cause(3, log), f"unmeasured: {self.TIMED_OUT}")
        # An unreadable log says no more than one that names no test.
        self.assertEqual(worker.no_verdict_cause(3, self.base / "missing.log"), f"unmeasured: {self.TIMED_OUT}")
        # Shortened as a test's name is, and kept on the hold's one line.
        receipt.write_text(json.dumps({"error": "first line\n  second " + "x" * 400}))
        said = worker.no_verdict_cause(3, log).removeprefix("unmeasured: ")
        self.assertEqual(len(said), mq.UNMEASURED_SHOWN)
        self.assertTrue(said.startswith("first line second x"), said)
        self.assertTrue(said.endswith("..."), said)

    def test_the_cause_of_a_run_on_the_farm_mac_is_in_the_receipt_that_came_back(self):
        # As remote files it: the lane's own receipt under result, as offload_host read it on another Mac.
        worker, log, _ = self.stopped_worker()
        run = self.dispatch_of(worker, json.dumps({"host": "build-host", "result": {
            "exitStatus": 3, "receipt": {"outcome": "unmeasured", "error": self.TIMED_OUT}}}))
        self.assertEqual(worker.no_verdict_cause(3, log), f"unmeasured: {self.TIMED_OUT}")
        # What stopped the run there, where the lane wrote no receipt.
        (run / "source.json").write_text(json.dumps({"host": "build-host", "result": {"error": "the checkout is gone"}}))
        self.assertEqual(worker.no_verdict_cause(3, log), "unmeasured: the checkout is gone")

    def test_with_no_receipt_of_this_run_that_says_why_the_cause_is_the_fixed_text(self):
        worker, log, receipt = self.stopped_worker()
        self.assertEqual(worker.no_verdict_cause(3, log), self.NOTHING)  # no receipt at all
        for said in (b"{not json", b'"a string"', b"[1]", b"null", b"\xff\xfe", b'{"outcome": "unmeasured"}',
                     b'{"error": ""}', b'{"error": "  \\n"}', b'{"error": 7}', b'{"error": null}', b'{"result": "text"}',
                     b'{"result": {"receipt": "text"}}'):
            with self.subTest(said=said):
                receipt.write_bytes(said)
                self.assertEqual(worker.no_verdict_cause(3, log), self.NOTHING)
        # An earlier run's receipt is not this one's.
        receipt.write_text(json.dumps({"error": self.TIMED_OUT}))
        os.utime(receipt, (worker.lane_now[1] - 10, worker.lane_now[1] - 10))
        self.assertEqual(worker.no_verdict_cause(3, log), self.NOTHING)
        # A worker that has run no lane has no receipt to read.
        lane_now, worker.lane_now = worker.lane_now, None
        self.assertEqual(worker.no_verdict_cause(3, log), self.NOTHING)
        # A receipt that is a directory cannot be read either.
        worker.lane_now = lane_now
        receipt.unlink()
        receipt.mkdir()
        self.assertEqual(worker.no_verdict_cause(3, log), self.NOTHING)

    def test_a_run_on_the_farm_mac_with_an_absent_or_malformed_receipt_gets_the_fixed_text(self):
        for said in (None, "{not json", "[1]", json.dumps({"host": "build-host", "result": {"exitStatus": 3}})):
            with self.subTest(said=said):
                worker, log, _ = self.stopped_worker()
                self.dispatch_of(worker, said)
                self.assertEqual(worker.no_verdict_cause(3, log), self.NOTHING)

    def test_tests_the_log_names_and_the_other_causes_keep_their_words(self):
        worker, log, receipt = self.stopped_worker()
        receipt.write_text(json.dumps({"error": self.TIMED_OUT}))
        log.write_text("test_voices (m.Voices) ... skipped 'Unmeasured: the voices were not ready in 500 s'\n")
        self.assertEqual(worker.no_verdict_cause(3, log), "unmeasured: 1 test, the first test_voices (m.Voices) ... "
                                                          "skipped 'Unmeasured: the voices were not ready in 500 s'")
        self.assertEqual(worker.no_verdict_cause(75, log), "no admission: the host stayed at capacity")
        self.assertIn("configuration", worker.no_verdict_cause(78, log))
        worker.timed_out = "gate reached its limit: after 7200 s"
        self.assertEqual(worker.no_verdict_cause(3, log), "unmeasured: gate reached its limit: after 7200 s")

    VOIDED = ("Unmeasured: RunGroupAwareFractureComponentUnionChecks voided its own measurement (gate exit 3) - 15 "
              "row(s) pass, none fail, but a timing it depends on could not be trusted. Rerun on an idle machine.")

    def test_a_log_that_names_no_test_gives_the_projects_own_reason(self):
        """mpm's gate of 2026-11-12, which said in a line of its own what it could not measure (vocaro/queuekeeper#30)."""
        worker, log, receipt = self.stopped_worker()
        log.write_text("test_last (m.Passed) ... ok\n" + self.VOIDED + "\n" + "done\n")
        self.assertEqual(worker.no_verdict_cause(3, log), f"unmeasured, and its log says: {self.VOIDED}")
        # The last of several, bounded as a test's line is.
        log.write_text(f"Unmeasured: the first\n{self.VOIDED}\nUnmeasured: {'x' * 400}\n")
        said = worker.no_verdict_cause(3, log)
        self.assertTrue(said.startswith("unmeasured, and its log says (3 such lines, the last): Unmeasured: xxx"), said)
        self.assertEqual(len(said.split("the last): ", 1)[1]), mq.UNMEASURED_SHOWN)
        # A test the log reports unmeasured, and an error the receipt records, still come first.
        log.write_text(self.VOIDED + "\ntest_voices (m.Voices) ... skipped 'Unmeasured: the voices were not ready'\n")
        self.assertEqual(worker.no_verdict_cause(3, log), "unmeasured: 1 test, the first test_voices (m.Voices) ... "
                                                          "skipped 'Unmeasured: the voices were not ready'")
        log.write_text(self.VOIDED + "\n")
        receipt.write_text(json.dumps({"error": self.TIMED_OUT}))
        self.assertEqual(worker.no_verdict_cause(3, log), f"unmeasured: {self.TIMED_OUT}")
        # A line of another prefix is not guessed at.
        receipt.unlink()
        log.write_text(self.VOIDED.replace("Unmeasured:", "NON-RUN:") + "\n")
        self.assertEqual(worker.no_verdict_cause(3, log), self.NOTHING)

    def test_the_send_back_and_the_worker_log_quote_the_projects_reason(self):
        """A real qk run of a gate that says why in its own line and exits 3: the worker's no-verdict line, the recorded
        reason and qk queue status quote it (vocaro/queuekeeper#30)."""
        config = self.config()
        config["lanes"]["gate"] = {"command": ["python3", "-c", f"print({self.VOIDED!r}); raise SystemExit(3)"]}
        config["queue"]["noVerdictRuns"] = 2
        self.publish(config)
        self.commit("a", "a")
        [identifier] = self.add("a")
        with patch.object(mq.Worker, "settle", return_value=None), \
                contextlib.redirect_stdout(io.StringIO()) as said:
            self.work()
        self.assertEqual(self.state(identifier)["state"], "failed")
        self.assertIn(f"no verdict (exit 3), run 1 of at most 2: unmeasured, and its log says: {self.VOIDED}",
                      said.getvalue())
        held = self.state(identifier)["why"]
        self.assertIn(f"the last with exit 3 on this Mac ({offload_host.mac_name()}), unmeasured, and its log says: "
                      f"{self.VOIDED}, the most queue.noVerdictRuns allows (2)", held)
        snapshot, _ = mq.reading(self.repo)
        self.assertIn(self.VOIDED, json.dumps(snapshot))

    BUSY = ("cpu", "the CPU is 95% busy, at or above HEAVY_BUSY_LIMIT=75%")

    def test_a_retry_waits_while_the_load_is_high_and_starts_once_it_drops(self):
        self.status.write_text("3")
        self.commit("a", "a")
        [identifier] = self.add("a")
        # The second gate passes only if it starts after the host settled: the load is what voided the first.
        with self.host([self.BUSY, self.BUSY, self.BUSY, None], then=self.status.unlink) as (slept, said):
            self.assertEqual(self.work(), 0)
        self.assertEqual(slept, [mq.SETTLE_POLL] * 3)
        self.assertEqual(self.gates(), 2)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertIsNone(mq.Queue(self.repo).held())
        log = said.getvalue()
        self.assertIn("the CPU is 95% busy, at or above HEAVY_BUSY_LIMIT=75%: the lanes run again once the host settles", log)
        self.assertIn(f"the host settled after {3 * mq.SETTLE_POLL}s and 4 looks; one-minute load first 40, highest 40, "
                      "last 3: the lanes run again", log)

    def test_a_retry_whose_host_stays_busy_past_the_bound_sends_back_and_says_why(self):
        self.status.write_text("3")
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.host([self.BUSY]) as (slept, said):
            self.assertEqual(self.work(), 0)
        self.assertEqual(sum(slept), mq.SETTLE_LIMIT)
        self.assertEqual(len(slept), mq.SETTLE_LIMIT // mq.SETTLE_POLL)
        # No second gate: the entry comes back with the reason the Mac could not run it.
        self.assertEqual(self.gates(), 1)
        self.assertEqual(self.state(identifier)["state"], "failed")
        held = self.state(identifier)["why"]
        self.assertIn(f"the lanes gave no verdict in 1 run in a row, the last with exit 3 on this Mac "
                      f"({offload_host.mac_name()}), unmeasured, with no test its log names, and the host did not settle in {mq.SETTLE_LIMIT}s (the CPU is 95% busy, at or above", held)
        self.assertIsNone(mq.Queue(self.repo).held())
        self.assertIn(f"the host did not settle in {mq.SETTLE_LIMIT}s and {len(slept) + 1} looks", said.getvalue())
        # A line every SETTLE_REPORT while it waited, not one per look.
        self.assertEqual(said.getvalue().count("still waiting"), mq.SETTLE_LIMIT // mq.SETTLE_REPORT)
        self.pressure.side_effect = None
        self.status.unlink()
        [retry] = self.add("a")
        self.work()
        self.assertEqual(self.state(retry)["state"], "landed")

    def test_every_retry_waits_for_the_host_to_settle(self):
        """Each run after the first starts only once the host settled, never into the load that voided the last."""
        self.status.write_text("3")
        self.commit("a", "a")
        [identifier] = self.add("a")
        started = []
        real = mq.Worker.gate
        def gate(worker, *arguments):
            started.append(self.pressure.call_count)
            return real(worker, *arguments)
        with self.host([self.BUSY, None, self.BUSY, self.BUSY, None]) as (slept, said), \
                patch.object(mq.Worker, "gate", gate):
            self.assertEqual(self.work(), 0)
        # The first gate before any look; the second after the second look settled; the third after the fifth.
        self.assertEqual(started, [0, 2, 5])
        self.assertEqual(slept, [mq.SETTLE_POLL] * 3)
        self.assertEqual(self.gates(), 3)
        self.assertEqual(self.state(identifier)["state"], "failed")
        self.assertIn("in 3 runs in a row", self.state(identifier)["why"])
        self.assertIsNone(mq.Queue(self.repo).held())
        self.assertEqual(said.getvalue().count("settled after"), 2)

    def test_a_failure_comes_back_at_once_without_a_wait(self):
        """Negative control: only no verdict waits for the host. A lane's failure, alone or bisected, never does."""
        self.commit("a", "a"); self.commit("bad", "bad")
        ids = self.add("a", "bad")
        with self.host([self.BUSY]) as (slept, said):
            self.assertEqual(self.work(), 0)
        self.assertEqual([self.state(i)["state"] for i in ids], ["landed", "failed"])
        self.assertEqual(slept, [])
        self.assertEqual(self.pressure.call_count, 0)
        self.assertNotIn("settle", said.getvalue())
        self.assertIsNone(mq.Queue(self.repo).held())

    def test_a_stop_during_the_wait_ends_it_and_leaves_the_entry_waiting(self):
        self.status.write_text("3")
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.host([self.BUSY]) as (slept, _), \
                patch.object(mq.processes, "requested", side_effect=lambda: 15 if slept else None):
            with self.assertRaisesRegex(mq.processes.Interrupted, "waited for the host to settle"): self.work()
        self.assertEqual(slept, [mq.SETTLE_POLL])
        self.assertEqual(self.gates(), 1)
        self.assertEqual(self.state(identifier)["state"], "queued")

    def test_a_worker_that_died_leaves_its_batch_to_the_next(self):
        self.commit("a", "a")
        [identifier] = self.add("a")
        queue = mq.Queue(self.repo)
        queue.mark([queue.read(identifier)], "testing")
        self.work()
        self.assertEqual(self.state(identifier)["state"], "landed")

    def test_a_held_queue_runs_nothing(self):
        self.commit("a", "a")
        [identifier] = self.add("a")
        mq.Queue(self.repo).hold("owner's decision")
        self.work()
        self.assertEqual(self.gates(), 0)
        self.assertEqual(self.state(identifier)["state"], "queued")


class HoldKinds(Fixture):
    """Whom a hold is for, in its reason's first words, and the tries a cause that clears by itself gets first: no
    hold waits for the owner unless nobody else can fix its cause (owner's decision, 2026-11-04,
    vocaro-engineering#173)."""
    FOR_OWNER = "for the owner, since nobody else can fix it: "
    FOR_SESSION = "for a session, not the owner: "
    SESSION_RESUMES = ("; the session that fixes it, or any session that finds it gone, runs `qk queue resume` itself "
                       "and says so")
    OWNER_FIXES = "; once it is fixed, any session that finds the cause gone runs `qk queue resume` and says so"

    def test_a_remote_out_of_reach_past_its_backoff_is_tried_again_and_lands(self):
        self.commit("a", "a")
        [identifier] = self.add("a")
        # The round's own fetch fails every try of its backoff; the next round's goes through.
        with self.remote_says(UNREACHED["https name"], range(1, len(mq.BACKOFF) + 2)) as (fetches, slept), \
                contextlib.redirect_stdout(io.StringIO()) as said:
            self.assertEqual(self.work(), 0)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertIn("a", self.main_files())
        self.assertIsNone(mq.Queue(self.repo).held())
        self.assertEqual(slept, [*mq.BACKOFF, mq.CLEARING_WAITS[0]])
        self.assertIn(f"exited 128 {len(mq.BACKOFF) + 1} times: {UNREACHED['https name']}: a cause that clears by itself, "
                      f"so the round runs again in {mq.CLEARING_WAITS[0]}s, try 1 of {len(mq.CLEARING_WAITS)}",
                      said.getvalue())
        self.assertEqual(self.gates(), 1)

    def test_a_cause_that_does_not_clear_holds_for_a_session_after_the_last_try(self):
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.remote_says(UNREACHED["https name"], range(1, 999)) as (fetches, slept), patch("builtins.print"):
            self.assertEqual(self.work(), 1)
        expected = []
        for wait in mq.CLEARING_WAITS: expected += [*mq.BACKOFF, wait]
        self.assertEqual(slept, expected + list(mq.BACKOFF))
        self.assertEqual(len(fetches), (len(mq.CLEARING_WAITS) + 1) * (len(mq.BACKOFF) + 1))
        held = mq.Queue(self.repo).held()
        self.assertTrue(held.startswith(f"{self.FOR_SESSION}`git fetch"), held)
        self.assertIn(f"exited 128 {len(mq.BACKOFF) + 1} times", held)
        self.assertTrue(held.endswith(f", after {len(mq.CLEARING_WAITS)} more tries of the round over "
                                      f"{sum(mq.CLEARING_WAITS)}s did not clear it{self.SESSION_RESUMES}"), held)
        self.assertNotIn("owner,", held.split(":", 1)[1])
        self.assertEqual(self.state(identifier)["state"], "queued")
        self.assertEqual(self.gates(), 0)

    def test_a_round_that_went_through_starts_the_tries_again(self):
        """Two outages, each past the backoff but within the tries, with a landing between: no hold."""
        self.commit("a", "a"); self.commit("b", "b")
        ids = self.add("a", "b")
        tries = len(mq.BACKOFF) + 1
        failing = set(range(1, tries * len(mq.CLEARING_WAITS) + 1))
        # Every fetch of the first round's tries fails; then, once a and b have landed and c is added, as many again.
        real = mq.Worker.round
        def round_(worker):
            went = real(worker)
            if went and self.state(ids[1])["state"] == "landed" and not self.state_of("c"):
                self.commit("c", "c")
                self.add("c")
                failing.update(range(len(fetches) + 1, len(fetches) + 1 + tries * len(mq.CLEARING_WAITS)))
            return went
        with self.remote_says(UNREACHED["ssh name"], failing) as (fetches, slept), patch("builtins.print"), \
                patch.object(mq.Worker, "round", round_):
            self.assertEqual(self.work(), 0)
        self.assertEqual([self.state(i)["state"] for i in ids], ["landed"] * 2)
        self.assertEqual(self.state_of("c")["state"], "landed")
        self.assertIsNone(mq.Queue(self.repo).held())
        self.assertEqual(slept.count(mq.CLEARING_WAITS[-1]), 2)

    def state_of(self, branch):
        return next((entry for entry in mq.Queue(self.repo).all() if entry["branch"] == branch), None)

    def test_a_remote_push_rejection_sends_back_the_batch_and_the_next_entry_lands(self):
        self.commit("a", "a")
        self.commit("b", "b")
        [first] = self.add("a")
        later = []
        hook = self.origin / "hooks/pre-receive"
        hook.write_text('#!/bin/sh\nread old new ref\n'
                        'if git show "$new:a" >/dev/null 2>&1; then echo declined by the remote >&2; exit 1; fi\n')
        host_isolation.executable(hook)
        real = mq.Worker.gate

        def gate(worker, *arguments, **options):
            outcome = real(worker, *arguments, **options)
            if self.gates() == 1: later.extend(self.add("b"))
            return outcome

        with patch.object(mq, "pause") as slept, patch.object(mq.Worker, "gate", gate), \
                patch.object(mq.Worker, "stack"), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.work(), 0)
        slept.assert_not_called()
        entry = self.state(first)
        self.assertEqual(entry["state"], "failed")
        self.assertIn("pre-receive hook declined", entry["why"])
        self.assertIn("declined by the remote", Path(entry["log"]).read_text())
        self.assertNotIn("publishing", entry)
        self.assertTrue(Path(entry["setupDoctorLog"]).is_file())
        self.assertEqual(self.state(later[0])["state"], "landed")
        self.assertIn("b", self.main_files())
        self.assertNotIn("a", self.main_files())
        self.assertIsNone(mq.Queue(self.repo).held())

    def test_a_refused_fetch_holds_for_the_owner_and_a_dropped_one_is_a_sessions_past_the_tries(self):
        """The negative control of the retry: a refusal is tried no further and names the owner."""
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.remote_says(REFUSED["https authentication"], range(1, 999)) as (fetches, slept), patch("builtins.print"):
            self.assertEqual(self.work(), 1)
        self.assertEqual((len(fetches), slept), (1, []))
        held = mq.Queue(self.repo).held()
        self.assertTrue(held.startswith(f"{self.FOR_OWNER}`git fetch"), held)
        self.assertTrue(held.endswith(self.OWNER_FIXES), held)
        self.assertNotIn("did not clear it", held)

    def test_what_a_session_can_fix_holds_for_a_session_not_the_owner(self):
        """A path where the worker's checkout goes is a repository-wide failure."""
        checkout = self.base / "queuekeeper-workers/project/project"
        checkout.mkdir(parents=True)
        (checkout / "kept").write_text("x")
        self.commit("a", "a")
        [identifier] = self.add("a")
        with patch("builtins.print"): self.assertEqual(self.work(), 1)
        held = mq.Queue(self.repo).held()
        self.assertEqual(held, f"{self.FOR_SESSION}{checkout} exists and is no worktree of {self.repo}: move it"
                               f"{self.SESSION_RESUMES}")
        self.assertEqual(self.state(identifier)["state"], "queued")

    def test_a_hook_core_hookspath_places_outside_the_queues_checkout_is_named_as_that_folders_copy(self):
        """vocaro/queuekeeper#186: Pagepour's core.hooksPath named its main checkout's .githooks, whose old hook refused
        a batch that had moved what the hook reads."""
        hooks = self.base / "main-hooks"
        hooks.mkdir()
        (hooks / "pre-push").write_text("#!/bin/sh\necho the reason >&2\nexit 1\n")
        host_isolation.executable(hooks / "pre-push")
        run(self.repo, "git", "config", "core.hooksPath", str(hooks))
        self.commit("a", "a")
        [identifier] = self.add("a")
        with patch("builtins.print"): self.assertEqual(self.work(), 0)
        why = self.state(identifier)["why"]
        self.assertIn(f"The hook ran from {hooks.resolve()}, which core.hooksPath names outside the "
                      "queue's checkout, so it was that folder's copy and not the batch's: a change to the hook, or to "
                      "what it reads, that the old copy refuses lands in two steps", why)
        self.assertEqual(mq.hooks_elsewhere(self.repo), f". The hook ran from {hooks.resolve()}, which core.hooksPath "
                         "names outside the queue's checkout, so it was that folder's copy and not the batch's: a change "
                         "to the hook, or to what it reads, that the old copy refuses lands in two steps, keeping what "
                         "the old copy needs for one landing and dropping it in a second")
        # A hook folder inside the checkout is the batch's own, and is not named.
        run(self.repo, "git", "config", "core.hooksPath", ".githooks")
        self.assertEqual(mq.hooks_elsewhere(self.repo), "")
        run(self.repo, "git", "config", "core.hooksPath", str(self.repo / ".githooks"))
        self.assertEqual(mq.hooks_elsewhere(self.repo), "")

    def test_a_failed_doctor_check_still_records_a_send_back_event(self):
        self.commit("a", "a")
        [identifier] = self.add("a")
        queue = mq.Queue(self.repo)
        entry = queue.read(identifier)
        worker = mq.Worker(queue)
        gate_log = queue.logs / "round-gate.log"
        earlier_log = queue.logs / "round-gate-doctor.log"
        earlier_log.write_text("Earlier lane passed.\n")
        with patch.object(mq.processes, "run", side_effect=OSError("doctor unavailable")) as doctor, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(worker.send_back_no_verdict([entry], gate_log,
                                                         "the Mac did not admit the gate", worker), mq.FAILED)
        self.assertEqual(earlier_log.read_text(), "Earlier lane passed.\n")
        self.assertEqual(doctor.call_args.args[0][-2:], ["queue", "doctor"])
        self.assertIsNone(queue.held())
        settled = queue.read(identifier)
        self.assertEqual(settled["state"], "failed")
        self.assertIn("doctor unavailable", Path(settled["doctorLog"]).read_text())
        [event] = mq.events.read(queue.directory, kinds={"entry.failed"})
        self.assertEqual(event["entry"], identifier)
        self.assertIn("the Mac did not admit the gate", event["why"])

    def test_the_kinds_are_told_apart_in_words(self):
        self.assertTrue(mq.worded(mq.OWNER, "x").startswith(self.FOR_OWNER + "x; "))
        self.assertTrue(mq.worded(mq.SESSION, "x").startswith(self.FOR_SESSION + "x; "))
        self.assertNotIn("for the owner", mq.worded(mq.SESSION, "x"))
        hold = mq.Hold(mq.CLEARS, "why")
        self.assertIsInstance(hold, ValueError)
        self.assertEqual((hold.kind, str(hold)), (mq.CLEARS, "why"))


# Files, as `qk run` does for a lane another Mac ran, a dispatch record in the checkout's
# .build/queuekeeper/remote/.dispatches/LANE/, named for the second it started unless the receipt names
# one, and holding the receipt that sys.argv[1] holds, if any; then runs lane.py with the rest.
DISPATCHING_LANE = """import json, os, pathlib, runpy, sys, time
receipt = pathlib.Path(sys.argv[1])
if receipt.exists():
    said = json.loads(receipt.read_text())
    name = said.pop("name", None) or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + f"-{os.getpid()}"
    run = pathlib.Path(".build/queuekeeper/remote/.dispatches/gate") / name
    run.mkdir(parents=True, exist_ok=True)
    if said: (run / "source.json").write_text(json.dumps(said))
sys.argv = ["lane.py", *sys.argv[2:]]
runpy.run_path("lane.py", run_name="__main__")
"""


class FarmMacSettle(Fixture):
    """The settle wait reads the Mac the lane ran on (#140): another Mac, faked at offload_host's answer
    (remote.ask), when the lane's dispatch says it ran there; this Mac otherwise."""
    HOST = "fake-mac"
    LOCAL_BUSY = ("cpu", "the CPU is 95% busy, at or above HEAVY_BUSY_LIMIT=75%")
    THERE_BUSY = {"load": 30.0, "cpuBusy": 90.0, "busyLimit": 75.0, "memory": 1}
    THERE_SETTLED = {"load": 2.0, "cpuBusy": 20.0, "busyLimit": 75.0, "memory": 1}

    def setUp(self):
        super().setUp()
        self.receipt = self.base / "receipt"
        (self.repo / "dispatching.py").write_text(DISPATCHING_LANE)
        run(self.repo, "git", "add", "dispatching.py")
        run(self.repo, "git", "commit", "-qm", "Add the dispatching lane")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        config = self.config()
        config["lanes"]["gate"]["command"] = ["python3", "dispatching.py", str(self.receipt), str(self.counter),
                                              str(self.status)]
        self.publish(config)

    def ran_there(self, **receipt):
        self.receipt.write_text(json.dumps({"host": self.HOST, "ranOn": "second Mac", **receipt}))

    @contextlib.contextmanager
    def farm_mac(self, answers):
        """Another Mac's answers to the pressure request, one per look, the last repeating; an exception
        among them is raised, as remote.ask raises for another Mac it cannot reach. Yields the requests."""
        asked = []
        def ask(host, request, timeout=None):
            asked.append((host, request))
            said = answers[min(len(asked) - 1, len(answers) - 1)]
            if isinstance(said, Exception): raise said
            return said
        with patch.object(mq.remote, "ask", side_effect=ask): yield asked

    @contextlib.contextmanager
    def passing_after_no_verdict(self):
        """The retry passes: the status file that voided the first gate goes once it has."""
        real = mq.Worker.gate
        def gate(worker, *arguments):
            result = real(worker, *arguments)
            if result[0] == 3: self.status.unlink()
            return result
        with patch.object(mq.Worker, "gate", gate): yield

    def test_a_lane_the_farm_mac_ran_waits_for_that_mac_not_this_one(self):
        self.status.write_text("3")
        self.ran_there()
        self.commit("a", "a")
        [identifier] = self.add("a")
        # This Mac stays busy throughout: a wait that read it would hold, not land.
        with self.farm_mac([self.THERE_BUSY, self.THERE_BUSY, self.THERE_SETTLED]) as asked, \
                self.host([self.LOCAL_BUSY]) as (slept, said), self.passing_after_no_verdict():
            self.assertEqual(self.work(), 0)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(self.gates(), 2)
        self.assertEqual(slept, [mq.SETTLE_POLL] * 2)
        self.assertEqual(asked, [(self.HOST, {"pressure": True})] * 3)
        self.assertEqual(self.pressure.call_count, 0)
        log = said.getvalue()
        self.assertIn(f"no verdict (exit 3) from {self.HOST}", log)
        self.assertIn(f"on {self.HOST}, the CPU is 90% busy, at or above HEAVY_BUSY_LIMIT=75% ({self.HOST}'s limit): "
                      f"the lanes run again once {self.HOST} settles", log)
        self.assertIn(f"{self.HOST} settled after {2 * mq.SETTLE_POLL}s and 3 looks; one-minute load first 30, "
                      "highest 30, last 2: the lanes run again", log)

    def test_a_farm_mac_that_stays_busy_is_named_in_the_send_back(self):
        self.status.write_text("3")
        self.ran_there()
        self.commit("a", "a")
        [identifier] = self.add("a")
        # This Mac is settled throughout: a wait that read it would run the lanes again at once.
        with self.farm_mac([{"load": 1.0, "cpuBusy": 10.0, "busyLimit": 75.0, "memory": 2}]), self.host([None]) as (slept, said):
            self.assertEqual(self.work(), 0)
        self.assertEqual(sum(slept), mq.SETTLE_LIMIT)
        self.assertEqual(self.gates(), 1)
        self.assertEqual(self.state(identifier)["state"], "failed")
        self.assertEqual(self.pressure.call_count, 0)
        self.assertIn(f"{self.HOST} did not settle in {mq.SETTLE_LIMIT}s (on {self.HOST}, memory pressure is level 2 "
                      "(warning), not 1 (normal))", self.state(identifier)["why"])

    def test_a_lane_that_ran_here_reads_only_this_mac(self):
        """Negative control: no dispatch record, so another Mac is never asked."""
        self.status.write_text("3")
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.farm_mac([AssertionError("another Mac was asked")]) as asked, \
                self.host([self.LOCAL_BUSY, None], then=self.status.unlink) as (slept, said):
            self.assertEqual(self.work(), 0)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(asked, [])
        self.assertEqual(self.pressure.call_count, 2)
        self.assertIn("the host settled after", said.getvalue())
        self.assertNotIn(self.HOST, said.getvalue())

    def test_a_dispatch_that_only_prepared_or_started_before_the_lane_reads_this_mac(self):
        """Negative controls: nothing ran on another Mac during this lane's run."""
        for case, receipt in [("prepared", {"stage": "preparation", "outcome": "failed-or-interrupted"}),
                              ("earlier", {"name": "20200101T000000Z-1"})]:
            with self.subTest(case):
                self.status.write_text("3")
                self.ran_there(**receipt)
                self.commit(f"a-{case}", f"a-{case}")
                [identifier] = self.add(f"a-{case}")
                self.pressure.reset_mock()
                with self.farm_mac([AssertionError("another Mac was asked")]) as asked, \
                        self.host([self.LOCAL_BUSY, None], then=self.status.unlink):
                    self.assertEqual(self.work(), 0)
                self.assertEqual(self.state(identifier)["state"], "landed")
                self.assertEqual(asked, [])
                self.assertEqual(self.pressure.call_count, 2)

    def test_a_farm_mac_out_of_reach_falls_back_to_this_macs_reading_saying_so(self):
        self.status.write_text("3")
        self.ran_there()
        self.commit("a", "a")
        [identifier] = self.add("a")
        lost = mq.remote.Misconfigured(f"{self.HOST} cannot be reached: ssh: Could not resolve hostname", "wake it")
        # Out of reach at both looks, while this Mac is busy and then settled; asked again at each.
        with self.farm_mac([lost]) as asked, \
                self.host([self.LOCAL_BUSY, None], then=self.status.unlink) as (slept, said):
            self.assertEqual(self.work(), 0)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(len(asked), 2)
        self.assertEqual(self.pressure.call_count, 2)
        log = said.getvalue()
        self.assertEqual(log.count("cannot be read, so the wait reads this Mac's load instead"), 1)
        self.assertIn(f"{self.HOST}, where the lanes ran, cannot be read, so the wait reads this Mac's load instead: "
                      f"{self.HOST} cannot be reached", log)
        self.assertIn("the host settled after", log)

    def test_a_farm_mac_that_answers_again_is_read_again(self):
        self.status.write_text("3")
        self.ran_there()
        self.commit("a", "a")
        [identifier] = self.add("a")
        lost = mq.remote.Unavailable(f"{self.HOST} did not report its load: it answered None")
        with self.farm_mac([lost, self.THERE_SETTLED]), self.host([self.LOCAL_BUSY]) as (slept, said), \
                self.passing_after_no_verdict():
            self.assertEqual(self.work(), 0)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(self.pressure.call_count, 1)
        self.assertIn(f"{self.HOST}'s load can be read again: reading it", said.getvalue())
        self.assertIn(f"{self.HOST} settled after {mq.SETTLE_POLL}s and 2 looks", said.getvalue())

    # Before a retry the wait reads the Mac the retry could use, not the one that voided the last run (#253): two
    # fake Macs, the second by its capacity and pressure answers and this one by its capacity and resources.pressure.
    FREE = {"slots": 3, "slotsMax": 3, "busy": [], "busyLimit": 75.0}
    FULL = {"slots": 3, "slotsMax": 3, "busy": [0, 1, 2], "busyLimit": 75.0}

    @contextlib.contextmanager
    def macs(self, there, here):
        """Another Mac whose capacity reads there and this Mac's here, each a reading or a function of nothing."""
        read = lambda found: found() if callable(found) else found
        with farm_fixture.only(self.HOST), \
                patch.object(mq.remote, "capacity_there", side_effect=lambda host: read(there)), \
                patch.object(mq.offload_host, "capacity", side_effect=lambda **_: read(here)):
            yield

    def test_a_loaded_farm_mac_the_retry_would_use_is_waited_for_though_this_mac_voided_the_run(self):
        self.status.write_text("3")
        self.commit("a", "a")
        [identifier] = self.add("a")
        # The run was here, and this Mac has settled; the old wait read this Mac and retried at once.
        with self.macs(self.FREE, self.FREE), self.farm_mac([self.THERE_BUSY, self.THERE_SETTLED]) as asked, \
                self.host([None]) as (slept, said), self.passing_after_no_verdict():
            self.assertEqual(self.work(), 0)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(slept, [mq.SETTLE_POLL])
        self.assertEqual(asked, [(self.HOST, {"pressure": True})] * 2)
        self.assertEqual(self.pressure.call_count, 0)
        log = said.getvalue()
        # Both Macs have room and neither reading gives a busy share, so the farm's Mac comes before this Mac.
        self.assertIn(f"the lanes run next on {self.HOST}, where gate would go: {self.HOST} has the most headroom of "
                      "the Macs with spare capacity, its CPU unread: this Mac its CPU unread", log)
        self.assertIn(f"the lanes run again once {self.HOST} settles", log)

    def test_a_loaded_this_mac_the_retry_would_use_is_waited_for_though_the_farm_mac_voided_the_run(self):
        """2026-11-07's shape: the wait settled build-host, and the retry went to this Mac."""
        self.status.write_text("3")
        self.ran_there()
        self.commit("a", "a")
        [identifier] = self.add("a")
        # This Mac has the free slot; another Mac voided the run, has none, and reads settled throughout.
        with self.macs(self.FULL, self.FREE), self.farm_mac([self.THERE_SETTLED]) as asked, \
                self.host([self.LOCAL_BUSY, None], then=self.status.unlink) as (slept, said):
            self.assertEqual(self.work(), 0)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(slept, [mq.SETTLE_POLL])
        self.assertEqual(asked, [])
        self.assertEqual(self.pressure.call_count, 2)
        log = said.getvalue()
        self.assertIn("the lanes run next on this Mac, the one with spare capacity for gate", log)
        self.assertIn("the lanes run again once this Mac settles", log)
        self.assertIn("this Mac settled after", log)

    def test_a_settled_target_does_not_wait_for_the_loaded_mac_it_will_not_use(self):
        """Negative controls, each way round: the Mac that will not take the retry stays loaded."""
        for name, there, here, ran_there, answers, looks, read_here in [
                ("second", self.FREE, self.FREE, False, [self.THERE_SETTLED], [self.LOCAL_BUSY], 0),
                ("this", self.FULL, self.FREE, True, [self.THERE_BUSY], [None], 1)]:
            with self.subTest(name):
                self.status.write_text("3")
                if ran_there: self.ran_there()
                else: self.receipt.unlink(missing_ok=True)
                self.commit(f"a-{name}", f"a-{name}")
                [identifier] = self.add(f"a-{name}")
                self.pressure.reset_mock()
                with self.macs(there, here), self.farm_mac(answers) as asked, self.host(looks) as (slept, said), \
                        self.passing_after_no_verdict():
                    self.assertEqual(self.work(), 0)
                self.assertEqual(self.state(identifier)["state"], "landed")
                self.assertEqual(slept, [])
                self.assertEqual(len(asked), 1 - read_here)
                self.assertEqual(self.pressure.call_count, read_here)
                self.assertNotIn("settles", said.getvalue())

    def test_with_neither_mac_free_the_retry_waits_for_both_since_either_may_admit_it(self):
        self.status.write_text("3")
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.macs(self.FULL, self.FULL), self.farm_mac([self.THERE_SETTLED]) as asked, \
                self.host([self.LOCAL_BUSY, None], then=self.status.unlink) as (slept, said):
            self.assertEqual(self.work(), 0)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(slept, [mq.SETTLE_POLL])
        self.assertEqual(len(asked), 2)
        self.assertEqual(self.pressure.call_count, 2)
        log = said.getvalue()
        self.assertIn("the lanes run next on whichever Mac admits gate first, since none has spare capacity", log)
        self.assertIn(f"{self.HOST} and this Mac settled after", log)

    def test_the_target_is_read_at_each_look_as_slots_come_and_go(self):
        self.status.write_text("3")
        self.commit("a", "a")
        [identifier] = self.add("a")
        readings = iter([self.FREE, self.FULL])
        # Another Mac has a slot at the first look, loaded, and none at the second, when this Mac has settled.
        with self.macs(lambda: next(readings, self.FULL), self.FREE), self.farm_mac([self.THERE_BUSY]) as asked, \
                self.host([None], then=self.status.unlink) as (slept, said):
            self.assertEqual(self.work(), 0)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(slept, [mq.SETTLE_POLL])
        self.assertEqual(len(asked), 1)
        self.assertEqual(self.pressure.call_count, 1)
        self.assertEqual(said.getvalue().count("the lanes run next on "), 2)

    def test_a_farm_mac_whose_capacity_cannot_be_read_leaves_this_mac_the_retry_goes_to(self):
        self.status.write_text("3")
        self.ran_there()
        self.commit("a", "a")
        [identifier] = self.add("a")
        def away(): raise mq.remote.Unreachable(f"{self.HOST} cannot be reached: it is asleep", "wake it")
        with self.macs(away, self.FREE), self.farm_mac([AssertionError("another Mac was read")]) as asked, \
                self.host([None], then=self.status.unlink) as (slept, said):
            self.assertEqual(self.work(), 0)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(asked, [])
        self.assertIn(f"the lanes run next on this Mac, since {self.HOST}'s capacity cannot be read: {self.HOST} "
                      "cannot be reached: it is asleep", said.getvalue())

    def test_a_lane_that_stays_on_this_mac_reads_only_this_mac(self):
        self.status.write_text("3")
        config = self.config()
        config["lanes"]["gate"]["here"] = "a measurement tied to this Mac"
        self.publish(config)
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.macs(AssertionError, AssertionError), self.farm_mac([AssertionError("another Mac was read")]) as asked, \
                self.host([self.LOCAL_BUSY, None], then=self.status.unlink) as (slept, said):
            self.assertEqual(self.work(), 0)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(asked, [])
        self.assertIn("the lanes run next on this Mac, since gate runs only there", said.getvalue())


class FarmMacReading(unittest.TestCase):
    """remote.pressure_there and remote.dispatched alone, and the reading offload_host gives."""

    def test_the_farm_macs_reading_is_judged_by_its_own_limit(self):
        # The load is what the queue logs; the busy share is what judges, so a high load alone refuses nothing.
        # The limit is quoted as that Mac's, and where it is set there when its reading says so, which an older
        # toolkit's does not (vocaro/queuekeeper#29).
        cases = [({"load": 2.0, "cpuBusy": 75.0, "busyLimit": 75.0, "memory": 1},
                  ("cpu", "the CPU is 75% busy, at or above HEAVY_BUSY_LIMIT=75% (fake-mac's limit)")),
                 ({"load": 2.0, "cpuBusy": 91.0, "busyLimit": 90.0, "memory": 1, "limitsFile": "/Users/x/limits.json",
                   "limitsFrom": {"busyLimit": "/Users/x/limits.json"}},
                  ("cpu", "the CPU is 91% busy, at or above HEAVY_BUSY_LIMIT=90% (fake-mac's limit, set in "
                          "/Users/x/limits.json)")),
                 ({"load": 2.0, "cpuBusy": 80.0, "busyLimit": 75.0, "memory": 1, "limitsFile": "/Users/x/limits.json",
                   "limitsFrom": {"busyLimit": "default"}},
                  ("cpu", "the CPU is 80% busy, at or above HEAVY_BUSY_LIMIT=75% (fake-mac's limit, the default, since "
                          "/Users/x/limits.json does not set it)")),
                 ({"load": 66.0, "cpuBusy": 74.9, "busyLimit": 75.0, "memory": 1}, None),
                 ({"load": 2.0, "cpuBusy": 40.0, "busyLimit": 75.0, "memory": 4},
                  ("memory", "memory pressure is level 4 (critical), not 1 (normal)")),
                 ({"load": None, "cpuBusy": 40.0, "busyLimit": 75.0, "memory": None},
                  ("memory", "memory pressure, which must be level 1 (normal), cannot be read")),
                 ({"load": 2.0, "cpuBusy": None, "busyLimit": 75.0, "memory": 1},
                  ("cpu", "the CPU's busy share, which HEAVY_BUSY_LIMIT=75% (fake-mac's limit) bounds, is unavailable")),
                 ({"load": 2.0, "cpuBusy": True, "busyLimit": 75.0, "memory": 1},
                  ("cpu", "the CPU's busy share, which HEAVY_BUSY_LIMIT=75% (fake-mac's limit) bounds, is unavailable"))]
        for answer, expected in cases:
            with self.subTest(answer=answer), patch.object(mq.remote, "ask", return_value=answer) as ask:
                self.assertEqual(mq.remote.pressure_there("fake-mac"), (expected, answer["load"]))
                ask.assert_called_once_with("fake-mac", {"pressure": True}, timeout=mq.remote.CENSUS_TIMEOUT)

    NO_MORE = " and admitted work runs there, so it takes no more"

    def test_a_macs_capacity_is_judged_by_its_slots_its_cpu_and_what_runs_there(self):
        """remote.spare, which qk run judges the Macs by before it chooses one (vocaro-engineering#168)."""
        idle = {"slots": 3, "slotsMax": 5, "busy": [], "load": 2.0, "cpuBusy": 20.0, "busyLimit": 75.0, "wholeHost": [],
                "exclusive": []}
        cases = [
            (idle, {}, None),
            ({**idle, "busy": [0, 1]}, {}, None),
            # With every guaranteed slot busy, a slot beyond them is room where admission would open one
            # (vocaro/queuekeeper#211): under the Mac's CPU limit, and none once every slot is busy.
            ({**idle, "busy": [0, 1, 2]}, {}, None),
            ({**idle, "busy": [0, 1, 2, 3]}, {}, None),
            ({**idle, "busy": [0, 1, 2, 3, 4]}, {}, "every guaranteed slot of another Mac is busy (HEAVY_SLOTS=3)"),
            ({**idle, "busy": [0, 1, 2], "cpuBusy": 75.0}, {},
             "another Mac is busy (CPU 75% busy, at or above its HEAVY_BUSY_LIMIT=75%)" + self.NO_MORE),
            ({**idle, "busy": [0, 1, 2], "cpuBusy": None}, {},
             "every guaranteed slot of another Mac is busy (HEAVY_SLOTS=3), and the CPU busy share of another Mac, which "
             "HEAVY_BUSY_LIMIT=75% (another Mac's limit) bounds, is unavailable"),
            ({**idle, "busy": [3, 4]}, {}, None),
            # A Mac with nothing admitted has room for one job however busy its CPU, as its admission admits the
            # first (the owner's decision, 2026-11-17, vocaro/queuekeeper#211).
            ({**idle, "cpuBusy": 75.0}, {}, None),
            ({**idle, "cpuBusy": 99.6}, {}, None),
            ({**idle, "cpuBusy": None}, {}, None),
            # With admitted work running there it takes no more at or over its limit, with guaranteed slots free or
            # not: the case of 2026-11-07, another Mac at 93% against its 90% with two of four slots free, which then
            # counted as room. Under the limit, or with a share that cannot be read, a free guaranteed slot is room.
            ({**idle, "slots": 4, "busy": [0, 1], "cpuBusy": 93.0, "busyLimit": 90.0}, {},
             "another Mac is busy (CPU 93% busy, at or above its HEAVY_BUSY_LIMIT=90%)" + self.NO_MORE),
            ({**idle, "slots": 4, "busy": [0, 1], "cpuBusy": 89.9, "busyLimit": 90.0}, {}, None),
            ({**idle, "slots": 4, "busy": [0, 1], "cpuBusy": None, "busyLimit": 90.0}, {}, None),
            ({**idle, "busy": [3], "cpuBusy": 80.0}, {"whole_host": True},
             "another Mac is busy (CPU 80% busy, at or above its HEAVY_BUSY_LIMIT=75%)" + self.NO_MORE),
            ({**idle, "busy": [0, 1, 2], "cpuBusy": 99.6}, {},
             "another Mac is busy (CPU 100% busy, at or above its HEAVY_BUSY_LIMIT=75%)" + self.NO_MORE),
            # The case of vocaro-engineering#200: a load of 40 against the old limit of 7.5, with the CPU half idle.
            ({**idle, "load": 40.0, "cpuBusy": 52.0}, {}, None),
            ({**idle, "busy": [0, 1, 2], "wholeHost": ["EPUBLib gate"]}, {},
             "another Mac is running a whole-host lane: EPUBLib gate"),
            ({**idle, "busy": [0, 1, 2, 3, 4], "exclusive": ["Pagepour timed"]}, {},
             "exclusive work holds another Mac: Pagepour timed"),
            # A whole-host lane needs every guaranteed slot; an exclusive one every slot.
            ({**idle, "busy": [0]}, {"whole_host": True},
             "a whole-host lane needs every guaranteed slot of another Mac (HEAVY_SLOTS=3) free, and 1 is busy"),
            ({**idle, "busy": [3]}, {"whole_host": True}, None),
            ({**idle, "busy": [3]}, {"exclusive": True},
             "exclusive work needs every slot of another Mac up to HEAVY_SLOTS_MAX=5 (another Mac's limit) free, "
             "and 1 is busy"),
            ({**idle, "busy": [0, 3]}, {"exclusive": True},
             "exclusive work needs every slot of another Mac up to HEAVY_SLOTS_MAX=5 (another Mac's limit) free, "
             "and 2 are busy"),
            ({**idle, "cpuBusy": 80.0}, {"whole_host": True}, None),
            ({**idle, "cpuBusy": 80.0}, {"exclusive": True}, None),
        ]
        for reading, need, expected in cases:
            with self.subTest(reading=reading, need=need):
                self.assertEqual(mq.remote.spare(reading, "another Mac", **need), expected)
        self.assertIsNone(mq.remote.spare({**idle, "cpuBusy": 90.0}, "this Mac"))
        # Beyond the guaranteed slots, the CPU's busy share still bounds the work (KeptSlotsTests).
        self.assertEqual(mq.remote.spare({**idle, "cpuBusy": 90.0, "kept": "EPUBLib gate"}, "this Mac"),
                         "this Mac is busy (CPU 90% busy, at or above its HEAVY_BUSY_LIMIT=75%)")
        # A reading that says where the Mac sets its limits has the reason say it too (vocaro/queuekeeper#29).
        sourced = {**idle, "limitsFile": "/Users/x/limits.json",
                   "limitsFrom": {"slotsMax": "/Users/x/limits.json", "busyLimit": "HEAVY_BUSY_LIMIT"}}
        self.assertEqual(mq.remote.spare({**sourced, "cpuBusy": 90.0, "kept": "EPUBLib gate"}, "build-host"),
                         "build-host is busy (CPU 90% busy, at or above its HEAVY_BUSY_LIMIT=75%, set in the "
                         "environment)")
        self.assertEqual(mq.remote.spare({**sourced, "busy": [3]}, "build-host", exclusive=True),
                         "exclusive work needs every slot of build-host up to HEAVY_SLOTS_MAX=5 (build-host's limit, set "
                         "in /Users/x/limits.json) free, and 1 is busy")
        # Another Mac's reading comes from offload_host, which must answer with one; an error there is its
        # owner's to fix, as a limits file admission cannot read refuses every request on that host.
        with patch.object(mq.remote, "ask", return_value=idle) as ask:
            self.assertEqual(mq.remote.capacity_there("fake-mac"), idle)
            ask.assert_called_once_with("fake-mac", {"capacity": True}, timeout=mq.remote.CENSUS_TIMEOUT, unsent=True)
        for answer in [{"load": 1.0}, ["no"], None, {**idle, "busy": "none"}, {**idle, "busyLimit": None}]:
            with self.subTest(answer=answer), patch.object(mq.remote, "ask", return_value=answer):
                with self.assertRaisesRegex(mq.remote.Unavailable, "fake-mac did not report its capacity"):
                    mq.remote.capacity_there("fake-mac")
        with patch.object(mq.remote, "ask", return_value={"error": "Invalid resource limits: x is not JSON"}):
            with self.assertRaisesRegex(mq.remote.Misconfigured, "fake-mac could not read its admission limits: Invalid "
                                                                  "resource limits: x is not JSON"):
                mq.remote.capacity_there("fake-mac")

    def test_the_farm_macs_busy_share_and_limit_are_what_a_gate_records(self):
        for answer, expected in (({"load": 2.0, "cpuBusy": 82.5, "busyLimit": 75, "memory": 1}, (82.5, 75.0)),
                                 ({"load": 2.0, "cpuBusy": None, "busyLimit": 60.0}, (None, 60.0)),
                                 ({"load": 2.0, "cpuBusy": True, "busyLimit": 75.0}, (None, 75.0))):
            with self.subTest(answer=answer), patch.object(mq.remote, "ask", return_value=answer):
                self.assertEqual(mq.remote.busy_there("fake-mac"), expected)
        with patch.object(mq.remote, "ask", return_value={"load": 1.0}):
            with self.assertRaisesRegex(mq.remote.Unavailable, "fake-mac did not report its CPU busy share"):
                mq.remote.busy_there("fake-mac")

    def test_a_gates_busy_reading_is_of_the_mac_it_ran_on_and_none_when_unreadable(self):
        with patch.object(mq.remote, "busy_there", return_value=(90.0, 75.0)) as there:
            self.assertEqual(mq.busy_of("fake-mac"), (90.0, 75.0, "fake-mac"))
            there.assert_called_once_with("fake-mac")
        with patch.object(mq.remote, "busy_there", return_value=(None, 75.0)):
            self.assertIsNone(mq.busy_of("fake-mac"))
        with patch.object(mq.remote, "busy_there", side_effect=mq.remote.Unavailable("gone")):
            self.assertIsNone(mq.busy_of("fake-mac"))
        with patch.object(mq.resources, "cpu_busy", return_value=(40.0, 15.0)) as here, \
                patch.object(mq.resources, "busy_limit", return_value=70.0):
            self.assertEqual(mq.busy_of(None), (40.0, 70.0, "this Mac"))
            self.assertTrue(here.call_args.kwargs["wait"])
        with patch.object(mq.resources, "busy_limit", side_effect=ValueError("bad limits")):
            self.assertIsNone(mq.busy_of(None))

    def test_a_farm_mac_without_a_reading_is_unavailable(self):
        for answer in [{"error": "HEAVY_BUSY_LIMIT is no number: x"}, {"load": 1.0}, {"load": 1.0, "limit": 7.5}, ["no"], None]:
            with self.subTest(answer=answer), patch.object(mq.remote, "ask", return_value=answer):
                with self.assertRaisesRegex(mq.remote.Unavailable, "fake-mac did not report its CPU busy share"):
                    mq.remote.pressure_there("fake-mac")

    def test_the_reading_there_uses_admissions_limit(self):
        from queuekeeper import offload_host, resources
        with patch.dict(os.environ, {"HEAVY_BUSY_LIMIT": "50"}), patch.object(offload_host, "cpu_busy", return_value=30.0):
            read = offload_host.pressure()
            self.assertEqual((read["busyLimit"], read["cpuBusy"]), (resources.busy_limit(), 30.0))
        with patch.dict(os.environ), patch.object(offload_host, "cpu_busy", return_value=30.0):
            os.environ.pop("HEAVY_BUSY_LIMIT", None)
            self.assertEqual(offload_host.pressure()["busyLimit"], resources.busy_limit())
        self.assertIsInstance(read["load"], float)
        with patch.dict(os.environ, {"HEAVY_BUSY_LIMIT": "x"}):
            self.assertIn("error", offload_host.pressure())
        # Answered as a request, not taken for a plan to run.
        done = subprocess.run([sys.executable, str(ROOT / "src/python/queuekeeper/offload_host.py"),
                               json.dumps({"pressure": True})], capture_output=True, text=True, timeout=stretched(60))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(set(json.loads(done.stdout.splitlines()[-1])),
                         {"load", "cpuBusy", "busyLimit", "memory", "hardware", "limitsFrom", "limitsFile"})

    def test_only_a_dispatch_that_ran_there_since_the_lane_began_names_its_mac(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        runs = root / ".build/queuekeeper/remote/.dispatches/gate"
        def dispatch(name, receipt):
            (runs / name).mkdir(parents=True)
            if receipt is not None: (runs / name / "source.json").write_text(json.dumps(receipt))
        since = 1_800_000_000.5  # 2027-01-15T08:00:00.5Z
        self.assertIsNone(mq.remote.dispatched(root, "gate", since))
        dispatch("20270115T075959Z-1", {"host": "earlier"})
        self.assertIsNone(mq.remote.dispatched(root, "gate", since))
        # Named for the second it started, which holds since.
        dispatch("20270115T080000Z-2", {"host": "fake-mac"})
        self.assertEqual(mq.remote.dispatched(root, "gate", since), "fake-mac")
        # A later dispatch that only prepared ran nothing there; the one before it did.
        dispatch("20270115T080100Z-3", {"host": "other", "stage": "preparation"})
        self.assertEqual(mq.remote.dispatched(root, "gate", since), "fake-mac")
        # One lost, with no receipt, ran on the configured second Mac.
        dispatch("20270115T080200Z-4", None)
        with farm_fixture.only("configured-mac"):
            self.assertEqual(mq.remote.dispatched(root, "gate", since), "configured-mac")
        with farm_fixture.only():
            self.assertIsNone(mq.remote.dispatched(root, "gate", since))
        self.assertIsNone(mq.remote.dispatched(root, "other-lane", since))


class MainMoves(Fixture):
    def during_gate(self, name, content="x"):
        """Push name to main directly, as a session outside the queue does, while the lanes run."""
        real = mq.Worker.gate
        outside = self.base / "outside"
        run(self.base, "git", "clone", "-q", str(self.origin), str(outside))
        pushed = []
        def gate(worker, lanes, round_name, **options):
            outcome = real(worker, lanes, round_name, **options)
            if not pushed:
                pushed.append(True)
                path = outside / name
                path.parent.mkdir(exist_ok=True)
                path.write_text(content)
                run(outside, "git", "pull", "-q", "origin", "main")
                run(outside, "git", "add", name)
                run(outside, "git", "commit", "-qm", f"Outside {name}")
                run(outside, "git", "push", "-q", "origin", "HEAD:main")
            return outcome
        return patch.object(mq.Worker, "gate", gate)

    def test_a_record_on_main_carries_the_result_over(self):
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.during_gate("records/one.json"): self.work()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(self.gates(), 1)
        self.assertLessEqual({"a", "records"}, self.main_files())

    def test_a_code_read_markdown_input_on_main_gates_again(self):
        config = self.config()
        config["queue"]["unread"]["exclude"] = {"README.md": "the code gate reads this input"}
        self.publish(config)
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.during_gate("README.md", "changed input\n"): self.work()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(self.gates(), 2)

    def test_unread_false_carries_nothing_over(self):
        config = self.config()
        config["queue"]["unread"] = False
        self.publish(config)
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.during_gate("README.md", "changed input\n"): self.work()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(self.gates(), 2)

    def test_code_on_main_gates_again(self):
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.during_gate("code"): self.work()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(self.gates(), 2)

    def test_the_new_code_is_what_the_second_gate_reads(self):
        self.commit("right", "right")
        [identifier] = self.add("right")
        with self.during_gate("left"): self.work()
        self.assertEqual(self.state(identifier)["state"], "failed")
        self.assertNotIn("right", self.main_files())

    def test_a_document_the_batch_also_changes_gates_again(self):
        self.commit("guide.md", "doc", "ours")
        [identifier] = self.add("doc")
        with self.during_gate("guide.md", "theirs"): self.work()
        self.assertEqual(self.state(identifier)["state"], "conflict")
        self.assertEqual(self.gates(), 1)


class PathLanes:
    """What a queue whose gate lanes are chosen by a batch's paths does whatever chooses them: the tests Inputs and
    MemberInputs share, each of which publishes its own lanes in setUp."""

    during_gate = MainMoves.during_gate

    def hook(self, body):
        PrePushHooks.hook(self, body)

    def runs(self):
        return PrePushHooks.runs(self)

    def app_gates(self):
        return len(self.app.read_text()) if self.app.exists() else 0

    def test_a_batch_runs_only_the_lanes_its_paths_select(self):
        self.commit("src/a.py", "a")
        [identifier] = self.add("a")
        self.work()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual((self.gates(), self.app_gates()), (1, 0))
        self.commit("shared/b.py", "b")
        self.add("b")
        self.work()
        self.assertEqual((self.gates(), self.app_gates()), (2, 1))

    def test_main_moving_by_paths_of_other_lanes_carries_the_pass_over(self):
        self.commit("src/a.py", "a")
        [identifier] = self.add("a")
        with self.during_gate("app/view.py"): self.work()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual((self.gates(), self.app_gates()), (1, 0))
        self.assertLessEqual({"src", "app"}, self.main_files())

    def gates_again_after(self, moved):
        self.commit("src/a.py", "a")
        [identifier] = self.add("a")
        with self.during_gate(moved): self.work()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(self.gates(), 2)

    def test_main_moving_by_a_path_of_a_lane_the_batch_ran_gates_again(self):
        self.gates_again_after("src/b.py")

    def test_main_moving_by_a_path_of_both_lanes_gates_again(self):
        self.gates_again_after("shared/c.py")

    def test_a_lane_covers_the_pre_push_hook_only_when_its_paths_selected_it(self):
        config = self.config()
        config["queue"]["hookCoveredBy"] = {"app": "the hook's checks"}
        self.publish(config, "The app lane covers the hook")
        self.hook("exit 0")
        self.commit("app/view.py", "app")
        self.add("app")
        self.work()
        self.assertEqual(self.runs(), [])
        self.commit("src/a.py", "src")
        [identifier] = self.add("src")
        self.work()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(len(self.runs()), 1)

    def test_a_carry_by_path_reruns_the_preflight_lanes(self):
        self.preflight_check()
        self.commit("src/a.py", "a")
        [identifier] = self.add("a")
        with patch.object(mq.Worker, "carry_passes", return_value=False) as checked, \
                self.during_gate("app/view.py"):
            self.work()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(self.gates(), 2)
        self.assertEqual(checked.call_args_list[0].args[0][-1][-1], "preflight")


class Inputs(PathLanes, Fixture):
    """Lane inputs (vocaro/queuekeeper#289): a batch runs only the gate lanes whose inputs one of its paths matches,
    skips the gate when it matches none, and a pass carries over main moving by paths of other lanes or of none."""

    def setUp(self):
        Fixture.setUp(self)
        self.app = self.base / "app-gates"
        config = self.config()
        config["lanes"]["gate"]["inputs"] = ["src/**", "shared/*.py"]
        config["lanes"]["app"] = {"command": ["python3", "lane.py", str(self.app), str(self.base / "app-status")],
                                  "inputs": ["app/**", "shared/*.py"]}
        config["queue"]["lanes"] = ["gate", "app"]
        self.publish(config, "Select lanes by their inputs")
        self.ran = self.base / "hook-runs"

    def test_a_path_no_rule_covers_runs_every_lane(self):
        """With inputs, such a path runs no gate lane: the batch lands on the docs check, which is no gate."""
        self.commit("tools/make.txt", "x")
        [identifier] = self.add("x")
        self.work()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual((self.gates(), self.app_gates()), (0, 0))
        self.assertIn("tools", self.main_files())

    def test_main_moving_by_a_path_no_rule_covers_gates_again(self):
        """With inputs, no lane reads such a path, so the pass stands."""
        self.commit("src/a.py", "a")
        [identifier] = self.add("a")
        with self.during_gate("tools/make.txt"): self.work()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual((self.gates(), self.app_gates()), (1, 0))

    def test_a_change_to_the_lanes_own_settings_runs_every_lane(self):
        config = self.config()
        config["lanes"]["gate"]["inputs"] = ["src/**", "shared/*.py", "extra/**"]
        self.commit("queuekeeper.json", "settings", json.dumps(config))
        [identifier] = self.add("settings")
        self.work()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual((self.gates(), self.app_gates()), (1, 1))

    def test_a_landing_records_the_gate_lanes_that_ran_as_a_partial_gate(self):
        self.commit("src/a.py", "a")
        [identifier] = self.add("a")
        self.work()
        entry = self.state(identifier)
        self.assertEqual(entry["partialGate"], {"lanes": ["gate"], "skipped": ["app"]})
        self.commit("shared/b.py", "b")
        [whole] = self.add("b")
        self.work()
        self.assertNotIn("partialGate", self.state(whole))

    def full_run_after(self, landings):
        config = self.config()
        config["queue"]["fullRun"] = {"landings": landings}
        self.publish(config, f"A full run on main after {landings}")

    def test_a_full_run_gates_main_whole_once_enough_partial_landings_are_owed_one(self):
        self.full_run_after(2)
        self.commit("src/a.py", "a")
        [first] = self.add("a")
        self.work()
        self.assertEqual((self.gates(), self.app_gates()), (1, 0))
        self.assertNotIn("fullRun", self.state(first))
        self.commit("app/b.py", "b")
        [second] = self.add("b")
        with contextlib.redirect_stdout(io.StringIO()) as said: self.work()
        # The second landing's own gate, then every lane on main.
        self.assertEqual((self.gates(), self.app_gates()), (2, 2), said.getvalue())
        self.assertIn("full run on main at", said.getvalue())
        head = run(self.repo, "git", "rev-parse", "origin/main")
        self.assertEqual([self.state(identifier)["fullRun"] for identifier in (first, second)], [head, head])
        self.assertEqual(mq.owed_full_run(self.repo, mq.Queue(self.repo).all()), [])
        self.assertIsNone(mq.Queue(self.repo).held())

    def test_a_landing_gated_whole_since_covers_the_partial_ones_before_it(self):
        self.full_run_after(2)
        for name, path in (("a", "src/a.py"), ("b", "shared/b.py"), ("c", "app/c.py")):
            self.commit(path, name)
            self.add(name)
            self.work()
        # a is covered by b's whole gate, which ran on a main that holds it, and c alone is owed a full run.
        self.assertEqual([entry["subject"] for entry in mq.owed_full_run(self.repo, mq.Queue(self.repo).all())],
                         ["Add app/c.py"])
        self.assertEqual((self.gates(), self.app_gates()), (2, 2))

    def test_a_full_run_that_fails_holds_the_queue_and_names_the_first_landing_that_breaks_the_lane(self):
        """A path no lane's inputs name, which the lanes read all the same: the landing skips the gate, and only the
        full run finds it."""
        self.full_run_after(2)
        self.commit("tools/fine.txt", "fine")
        self.add("fine")
        self.work()
        self.commit("bad-input", "bad")
        [bad] = self.add("bad")
        with contextlib.redirect_stdout(io.StringIO()) as said: self.work()
        self.assertEqual(self.state(bad)["state"], "landed")
        held = mq.Queue(self.repo).held()
        self.assertIn("the full run on main at", held, said.getvalue())
        self.assertIn(f"it first fails where {bad} landed", held)
        self.assertIn("no gate lane ran", held)
        # The whole gate, the lane before the oldest of them, at the first and at the second.
        self.assertEqual(self.gates(), 4, said.getvalue())
        head = run(self.repo, "git", "rev-parse", "origin/main")
        self.assertEqual(self.state(bad)["fullRunFailed"], head)

    def test_a_lane_without_inputs_runs_for_every_change(self):
        config = self.config()
        del config["lanes"]["gate"]["inputs"]
        self.publish(config, "Only the app lane gives inputs")
        gates, apps = self.gates(), self.app_gates()
        self.commit("tools/make.txt", "x")
        [identifier] = self.add("x")
        self.work()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual((self.gates() - gates, self.app_gates() - apps), (1, 0))


class MemberInputs(PathLanes, Fixture):
    """Inputs on the lanes of a gate lane group (vocaro/queuekeeper#289): a batch runs the group with only the lanes
    its paths select, skips the gate when it selects none, and runs them all for a path every lane reads."""

    def setUp(self):
        Fixture.setUp(self)
        self.app = self.base / "app-gates"
        config = self.config()
        config["lanes"]["unit"] = {**config["lanes"]["gate"], "inputs": ["src/**", "shared/*.py"]}
        config["lanes"]["app"] = {"command": ["python3", "lane.py", str(self.app), str(self.base / "app-status")],
                                  "inputs": ["app/**", "shared/*.py"]}
        config["lanes"]["gate"] = {"parallel": ["unit", "app"]}
        self.publish(config, "Gate on a group whose lanes give inputs")
        self.ran = self.base / "hook-runs"

    def landed(self, path, expected):
        self.commit(path, "a")
        [identifier] = self.add("a")
        with contextlib.redirect_stdout(io.StringIO()) as said: self.work()
        self.assertEqual(self.state(identifier)["state"], "landed", said.getvalue())
        self.assertEqual((self.gates(), self.app_gates()), expected, said.getvalue())
        return said.getvalue()

    def test_a_batch_runs_only_the_lanes_its_paths_select(self):
        said = self.landed("src/a.py", (1, 0))
        self.assertIn("gate: only unit of its lanes, which main's lane inputs select for the batch's paths", said)

    def test_disjoint_entries_already_waiting_gate_together_and_land_the_tested_heads_in_order(self):
        self.commit("src/a.py", "a")
        self.commit("app/b.py", "b")
        first, second = self.add("a", "b")
        original = mq.Worker.gate
        stacked_started = threading.Event()
        tested_heads = {}
        def gate(worker, lanes, name, **options):
            if worker.role == "batch":
                tested_heads[first] = worker.head()
                if not stacked_started.wait(stretched(20)):
                    raise AssertionError("the second gate did not start beside the first")
            elif worker.role == "stacked":
                tested_heads[second] = worker.head()
                stacked_started.set()
            return original(worker, lanes, name, **options)
        with (contextlib.redirect_stdout(io.StringIO()) as said,
              patch.object(mq.Worker, "gate", gate), patch.object(mq, "apart", BOTH_HERE),
              patch.object(mq, "STACK_POLL", 0.05),
              patch.object(mq.Worker, "stack_room", return_value=None),
              patch.object(mq.Worker, "admitted_mac", return_value=mq.HERE_MAC),
              patch.object(mq.farm, "destinations", return_value=("other-mac",))):
            self.assertEqual(self.work(), 0, said.getvalue())
        for identifier in [first, second]:
            entry = self.state(identifier)
            self.assertEqual((entry["state"], entry["landedAs"]), ("landed", tested_heads[identifier]))
        self.assertEqual(self.state(second)["gatedOn"], tested_heads[first])
        self.assertEqual((self.gates(), self.app_gates()), (1, 1), said.getvalue())
        self.assertIn("their paths and gate lanes are disjoint", said.getvalue())
        self.assertIn("gate: only app of its lanes", said.getvalue())

    def test_entries_with_different_paths_that_select_the_same_member_stay_in_one_batch(self):
        self.commit("src/a.py", "a")
        self.commit("src/b.py", "b")
        first, second = self.add("a", "b")
        queue = mq.Queue(self.repo)
        waiting = queue.waiting()
        with (patch.object(mq.farm, "destinations", return_value=("other-mac",)),
              patch.object(mq.Worker, "stack_room", return_value=None)):
            self.assertEqual(mq.Worker(queue).disjoint_batch(waiting, self.config()), (waiting, ()))
        self.assertEqual([entry["id"] for entry in waiting], [first, second])

    def test_a_disjoint_prefix_is_not_split_again_until_it_settles(self):
        self.commit("src/a.py", "a")
        self.commit("app/b.py", "b")
        first, second = self.add("a", "b")
        queue = mq.Queue(self.repo)
        waiting = queue.waiting()
        worker = mq.Worker(queue)
        with patch.object(mq.farm, "destinations", return_value=("other-mac",)):
            prefix, suffix = worker.disjoint_batch(waiting, self.config())
            self.assertEqual(([entry["id"] for entry in prefix], suffix), ([first], (second,)))
            self.assertEqual(worker.disjoint_batch(waiting, self.config()), (waiting, ()))

    def test_a_rename_counts_both_its_old_and_new_gate_lanes(self):
        self.commit("src/old.py", "source")
        self.commit("src/stays.py", "source-anchor", content="stays", start="source")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        self.commit("src/a.py", "a")
        run(self.repo, "git", "checkout", "-q", "-B", "move", "origin/main")
        (self.repo / "app").mkdir()
        run(self.repo, "git", "mv", "src/old.py", "app/new.py")
        run(self.repo, "git", "commit", "-qm", "Move an input from the unit lane to the app lane")
        self.add("a", "move")
        queue = mq.Queue(self.repo)
        waiting = queue.waiting()
        with patch.object(mq.farm, "destinations", return_value=("other-mac",)):
            self.assertEqual(mq.Worker(queue).disjoint_batch(waiting, self.config()), (waiting, ()))

    def test_a_disjoint_pair_stays_in_one_batch_without_room_for_two_gates(self):
        self.commit("src/a.py", "a")
        self.commit("app/b.py", "b")
        self.add("a", "b")
        queue = mq.Queue(self.repo)
        waiting = queue.waiting()
        with (patch.object(mq.farm, "destinations", return_value=("other-mac",)),
              patch.object(mq.Worker, "stack_room", return_value="no capacity")):
            self.assertEqual(mq.Worker(queue).disjoint_batch(waiting, self.config()), (waiting, ()))

    def test_a_disjoint_stack_rechecks_inputs_against_the_integrated_tree(self):
        self.commit("app/temporary.py", "app-change")
        run(self.repo, "git", "mv", "app/temporary.py", "app/permanent.py")
        run(self.repo, "git", "commit", "-qm", "Rename the new input before it lands")
        [identifier] = self.add("app-change")
        queue = mq.Queue(self.repo)
        [entry] = queue.waiting()
        worker = mq.Worker(queue)
        runner = mq.Beside(worker, label="stacked", settles=False)
        base = run(self.repo, "git", "rev-parse", mq.MAIN)
        paths = worker.own_paths(entry)
        self.assertEqual(paths, {"app/temporary.py", "app/permanent.py"})

        stack = mq.Stack(worker, runner, [entry], base, ["gate"], (), paths=paths,
                         parallel_config=self.config())
        with patch.object(runner, "gate") as gate:
            self.assertFalse(stack.begin())
            self.assertIsNone(stack.thread)
            gate.assert_not_called()
        self.assertEqual(self.state(identifier)["state"], mq.QUEUED)

        self.assertEqual(self.work(), 0)
        self.assertEqual(self.state(identifier)["state"], mq.LANDED)
        self.assertEqual((self.gates(), self.app_gates()), (0, 1))

    def test_a_path_of_both_lanes_runs_the_whole_group(self):
        self.assertNotIn("of its lanes", self.landed("shared/b.py", (1, 1)))

    def test_a_path_no_rule_covers_runs_every_lane(self):
        """With inputs on every lane of the group, such a path skips the gate."""
        self.landed("tools/make.txt", (0, 0))

    def test_a_lane_of_the_group_without_inputs_runs_for_every_change(self):
        config = self.config()
        del config["lanes"]["unit"]["inputs"]
        self.publish(config, "Only the app lane gives inputs")
        self.landed("tools/make.txt", (1, 0))

    def test_a_change_to_the_lanes_own_settings_runs_every_lane(self):
        config = self.config()
        config["lanes"]["unit"]["inputs"] = ["src/**", "shared/*.py", "extra/**"]
        self.commit("queuekeeper.json", "settings", json.dumps(config))
        [identifier] = self.add("settings")
        self.work()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual((self.gates(), self.app_gates()), (1, 1))

    def test_main_moving_by_paths_of_other_lanes_carries_the_pass_over(self):
        """A move by a path of a lane of the group that the batch's paths did not choose carries the pass over."""
        self.commit("src/a.py", "a")
        [identifier] = self.add("a")
        with self.during_gate("app/view.py"): self.work()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual((self.gates(), self.app_gates()), (1, 0))

    def test_a_landing_records_the_lanes_of_the_group_that_ran_as_a_partial_gate(self):
        self.commit("src/a.py", "a")
        [identifier] = self.add("a")
        self.work()
        entry = self.state(identifier)
        self.assertEqual(entry["partialGate"], {"lanes": ["gate"], "skipped": [],
                                                "members": {"gate": {"ran": ["unit"], "skipped": ["app"]}}})
        self.assertIn("partial gate: of gate's lanes, unit ran and app did not: no path it changes is among the "
                      "inputs of what did not run", mq.describe(entry))

    def test_main_moving_by_a_path_no_rule_covers_gates_again(self):
        """With inputs, no lane reads such a path, so the pass stands."""
        self.commit("src/a.py", "a")
        [identifier] = self.add("a")
        with self.during_gate("tools/make.txt"): self.work()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual((self.gates(), self.app_gates()), (1, 0))

    def test_a_lane_covers_the_pre_push_hook_only_when_its_paths_selected_it(self):
        """A group covers the hook only when all of its lanes ran."""
        config = self.config()
        config["queue"]["hookCoveredBy"] = {"gate": "the hook's checks"}
        self.publish(config, "The gate covers the hook")
        self.hook("exit 0")
        self.landed("shared/b.py", (1, 1))
        self.assertEqual(self.runs(), [])
        self.commit("src/a.py", "src")
        [identifier] = self.add("src")
        self.work()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(len(self.runs()), 1)

    # A group is one gate lane, so no move by a path of its lanes carries over by lane.
    test_a_carry_by_path_reruns_the_preflight_lanes = None


class InputRules(unittest.TestCase):
    """Lane inputs read without a queue: the lanes each path selects, the parser's checks and qk doctor's report."""
    LANES = {"gate": {"command": ["true"], "inputs": ["src/**", "tests/**"]},
             "app": {"command": ["true"], "inputs": ["app/**", "*.xcconfig"]}, "provision": {"command": ["true"]}}

    def config(self, lanes=None, **queue):
        return {"lanes": lanes or self.LANES, "queue": {"lanes": ["gate", "app"], **queue}}

    def parsed(self, lanes, named=("gate", "app"), **queue):
        return configuration.parse(json.dumps({"schemaVersion": 1, "name": "demo", "lanes": lanes,
                                               "queue": {"lanes": list(named), **queue}}))

    def test_each_path_selects_the_lanes_whose_inputs_match_it(self):
        config = self.config()
        cases = [("src/a/b.py", {"gate"}), ("app/x.swift", {"app"}), ("Base.xcconfig", {"app"}),
                 ("Makefile", set()), ("sub/Base.xcconfig", set()), ("doc/a.md", set()),
                 ("queuekeeper.json", None), ("queuekeeper-workspace.json", None)]
        for path, expected in cases:
            with self.subTest(path=path):
                self.assertEqual(configuration.affected_by(config, path), expected)
        self.assertEqual(configuration.affected_lanes(config, ["src/a.py", "Makefile"]), ["gate"])
        self.assertEqual(mq.lanes_of(config, ["app/x.swift"]), ["app"])

    def test_a_batch_of_paths_no_lane_names_skips_the_gate_as_unread_paths_do(self):
        config = self.config()
        self.assertEqual(mq.docs_lanes(config, ["Makefile", "assets/a.png"]), ["docs"])
        self.assertIsNone(mq.docs_lanes(config, ["Makefile", "src/a.py"]))
        self.assertIsNone(mq.docs_lanes(config, ["Makefile", "queuekeeper.json"]))
        self.assertTrue(mq.carried_over("Makefile", config, set()))
        self.assertFalse(mq.carried_over("src/a.py", config, set()))
        self.assertFalse(mq.carried_by_rules("app/x.swift", config, set(), ["gate"]))
        without_provision = self.config({name: lane for name, lane in self.LANES.items() if name != "provision"})
        self.assertTrue(mq.carried_by_rules("app/x.swift", without_provision, set(), ["gate"]))
        self.assertFalse(mq.carried_by_rules("queuekeeper.json", config, set(), ["gate"]))

    def test_a_lane_without_inputs_runs_for_every_change_and_nothing_skips_the_gate(self):
        config = self.config({**self.LANES, "app": {"command": ["true"]}})
        self.assertEqual(configuration.affected_by(config, "Makefile"), {"app"})
        self.assertEqual(configuration.affected_by(config, "src/a.py"), {"gate", "app"})
        self.assertIsNone(mq.docs_lanes(config, ["Makefile"]))
        self.assertEqual(mq.docs_lanes(config, ["README.md"]), ["docs"])

    def test_what_queue_unread_says_code_reads_runs_every_lane(self):
        """A path queue.unread excludes, or any path where it is false, is one code reads: with no lane's inputs
        naming it, every gate lane runs, since nothing says which."""
        excluded = self.config(unread={"exclude": {"assets/": "a test reads them"}})
        self.assertIsNone(configuration.affected_by(excluded, "assets/a.png"))
        self.assertIsNone(mq.docs_lanes(excluded, ["assets/a.png"]))
        self.assertEqual(configuration.affected_by(excluded, "Makefile"), set())
        never = self.config(unread=False)
        self.assertIsNone(configuration.affected_by(never, "Makefile"))
        self.assertIsNone(mq.docs_lanes(never, ["Makefile"]))
        self.assertEqual(configuration.affected_by(never, "src/a.py"), {"gate"})

    def test_the_parser_takes_inputs_of_gate_lanes_only_and_not_beside_queue_affected(self):
        self.parsed(self.LANES)
        self.parsed({"gate": {"parallel": ["app"], "inputs": ["src/**"]}, "app": {"command": ["true"]}},
                    named=["gate"])
        for inputs in ([], "src/**", ["src/**", "src/**"], [""], ["/src/**"], [1]):
            with self.subTest(inputs=inputs), self.assertRaisesRegex(ValueError, "gate.inputs"):
                self.parsed({**self.LANES, "gate": {"command": ["true"], "inputs": inputs}})
        with self.assertRaisesRegex(ValueError, "provision.inputs is given for a lane that is not one of the gate"):
            self.parsed({**self.LANES, "provision": {"command": ["true"], "inputs": ["src/**"]}})

    def test_queue_affected_is_refused_with_the_inputs_that_read_the_same(self):
        """vocaro/queuekeeper#289 retired it: no project's main gave it, and lane inputs say the same from the lanes."""
        plain = {"gate": {"command": ["true"]}, "app": {"command": ["true"]}}
        rules = [{"paths": ["src/**"], "lanes": ["gate"]}, {"paths": ["app/**", "shared/*.py"], "lanes": ["app"]},
                 {"paths": ["shared/*.py"], "lanes": ["gate", "app"]}]
        with self.assertRaises(ValueError) as refused: self.parsed(plain, affected=rules)
        said = str(refused.exception)
        self.assertIn("queue.affected, which each gate lane's inputs replaced (vocaro/queuekeeper#289)", said)
        self.assertIn('"gate": ["src/**", "shared/*.py"]', said)
        self.assertIn('"app": ["app/**", "shared/*.py"]', said)
        self.assertIn("runs no gate lane, where under queue.affected it ran every one", said)
        with self.assertRaisesRegex(ValueError, "queue.affected, which each gate lane's inputs replaced"):
            self.parsed(plain, affected="x")
        self.assertNotIn("affected", mq.MAIN_ONLY)

    GROUP = {"gate": {"parallel": ["unit", "app"]}, "unit": {"command": ["true"], "inputs": ["src/**", "shared/*.py"]},
             "app": {"command": ["true"], "inputs": ["app/**", "shared/*.py"]}}

    def group(self, lanes=None, **queue):
        return {"lanes": lanes or self.GROUP, "queue": {"lanes": ["gate"], **queue}}

    def test_a_groups_lanes_give_inputs_and_a_batch_selects_among_them(self):
        config = self.group()
        self.assertEqual(configuration.lane_inputs(config), {"gate": ["src/**", "shared/*.py", "app/**"]})
        cases = [(["src/a.py"], ["unit"]), (["app/x.swift", "Makefile"], ["app"]), (["shared/a.py"], None),
                 (["src/a.py", "app/b.swift"], None), (["src/a.py", "queuekeeper.json"], None), ([], None),
                 (["src/a.py", "README.md"], ["unit"])]
        for changed, expected in cases:
            with self.subTest(changed=changed):
                self.assertEqual(configuration.selected_members(config, "gate", changed), expected)
        self.assertEqual(mq.group_members(config, ["gate"], ["src/a.py"]), {"gate": ["unit"]})
        self.assertEqual(mq.group_members(config, ["gate"], ["shared/a.py"]), {})
        self.assertEqual(mq.docs_lanes(config, ["Makefile"]), ["docs"])
        # A path queue.unread says code reads, which no lane names, runs them all.
        excluded = self.group(unread={"exclude": {"assets/": "a test reads them"}})
        self.assertIsNone(configuration.selected_members(excluded, "gate", ["src/a.py", "assets/a.png"]))

    def test_a_lane_of_a_group_without_inputs_runs_whenever_the_group_does_and_for_every_change(self):
        config = self.group({**self.GROUP, "unit": {"command": ["true"]}})
        self.assertEqual(configuration.lane_inputs(config), {})
        self.assertIsNone(mq.docs_lanes(config, ["Makefile"]))
        self.assertEqual(configuration.selected_members(config, "gate", ["Makefile"]), ["unit"])
        self.assertIsNone(configuration.selected_members(config, "gate", ["app/a.swift"]))
        # A group that gives its own runs whole or not at all.
        whole = self.group({"gate": {"parallel": ["unit"], "inputs": ["src/**"]}, "unit": {"command": ["true"]}})
        self.assertIsNone(configuration.selected_members(whole, "gate", ["src/a.py"]))

    def test_the_parser_takes_inputs_of_a_gate_groups_lanes_and_not_of_both(self):
        self.parsed(self.GROUP, named=["gate"])
        with self.assertRaisesRegex(ValueError, "gate.inputs and unit.inputs are both given"):
            self.parsed({**self.GROUP, "gate": {"parallel": ["unit", "app"], "inputs": ["src/**"]}}, named=["gate"])
        with self.assertRaisesRegex(ValueError, "unit.inputs is given for a lane that is not one of the gate lanes"):
            self.parsed({**self.GROUP, "other": {"command": ["true"]}}, named=["other"])

    def test_a_partial_gate_names_what_ran_and_carry_over_reads_a_groups_chosen_lanes(self):
        config = self.group()
        self.assertIsNone(mq.partial_gate(config, ["shared/a.py"]))
        partial = mq.partial_gate(config, ["src/a.py"])
        self.assertEqual(partial, {"lanes": ["gate"], "skipped": [],
                                   "members": {"gate": {"ran": ["unit"], "skipped": ["app"]}}})
        self.assertEqual(mq.partial_gate(self.config(), ["app/x.swift"]), {"lanes": ["app"], "skipped": ["gate"]})
        self.assertEqual(mq.partial_said(mq.partial_gate(self.config(), ["app/x.swift"])),
                         "of the gate lanes, the app lane ran and the gate lane did not: no path it changes is among "
                         "the inputs of what did not run")
        members = mq.group_members(config, ["gate"], ["src/a.py"])
        # main moving by a path of the lane left out carries; by one of the lane that ran, or of both, does not.
        self.assertTrue(mq.carried_by_rules("app/x.swift", config, set(), ["gate"], members))
        self.assertFalse(mq.carried_by_rules("src/b.py", config, set(), ["gate"], members))
        self.assertFalse(mq.carried_by_rules("shared/b.py", config, set(), ["gate"], members))
        self.assertFalse(mq.carried_by_rules("app/x.swift", config, set(), ["gate"]))
        self.assertFalse(mq.carried_by_rules("queuekeeper.json", config, set(), ["gate"], members))
        # A lane of the group that gives no inputs reads every path.
        loose = self.group({**self.GROUP, "unit": {"command": ["true"]}})
        self.assertFalse(mq.carried_by_rules("app/x.swift", loose, set(), ["gate"], {"gate": ["unit"]}))

    def test_queue_full_run_is_false_or_positive_counts_and_only_mains(self):
        self.assertEqual(configuration.full_run(self.config()), {"landings": 5, "minutes": 240})
        self.assertEqual(configuration.full_run(self.config(fullRun={"minutes": 30})), {"landings": 5, "minutes": 30})
        self.assertIsNone(configuration.full_run(self.config(fullRun=False)))
        self.parsed(self.LANES, fullRun=False)
        self.parsed(self.LANES, fullRun={"landings": 1, "minutes": 1})
        for refused in (True, {}, {"landings": 0}, {"minutes": 1.5}, {"hours": 1}, 5):
            with self.subTest(refused=refused), self.assertRaisesRegex(ValueError, "queue.fullRun"):
                self.parsed(self.LANES, fullRun=refused)
        self.assertIn("fullRun", mq.MAIN_ONLY)

    def test_only_main_may_give_inputs(self):
        own = mq.as_batch(json.loads(json.dumps(self.config())))
        self.assertEqual(configuration.lane_inputs(own), {})
        self.assertIsNone(mq.docs_lanes(own, ["Makefile"]))

    def test_rides_says_what_skips_the_gate_and_which_lanes_run(self):
        self.assertEqual(mq.rides(self.config(), {"app/x.swift"}),
                         "rides the code gate: app/x.swift is a path a gate lane reads; of the gate lanes, only the "
                         "app lane, which main's lane inputs select for its paths")
        self.assertEqual(mq.rides(self.config(), {"Makefile"}),
                         "skips the code gate: no gate lane's inputs name Makefile nor any other path it changes; it "
                         "lands on the docs lane")
        self.assertIn("is documentation only", mq.rides(self.config(), {"README.md"}))

    def test_doctor_lists_the_files_that_skip_the_gate_and_globs_that_match_nothing(self):
        tracked = ["src/a.py", "app/b.swift", "README.md", "Makefile", "tools/x.py", "queuekeeper.json"]
        found = lane_coverage.affected_paths(Path("."), self.config(), tracked)
        self.assertEqual(found, {"skipping": ["Makefile", "tools/x.py"],
                                 "unmatched": ["gate: tests/**", "app: *.xcconfig"], "problems": []})
        [note] = lane_coverage.skipping_notes(found)
        self.assertIn("Makefile, tools/x.py", note)
        [warning] = lane_coverage.affected_warnings(found)
        self.assertIn("gate: tests/**, app: *.xcconfig", warning)
        self.assertIsNone(lane_coverage.affected_paths(Path("."), {"lanes": {}, "queue": {}}, tracked))
        grouped = lane_coverage.affected_paths(Path("."), self.group(), tracked)
        self.assertEqual((grouped["skipping"], grouped["unmatched"]),
                         (["Makefile", "tools/x.py"], ["gate: shared/*.py"]))


class PrePushHooks(Fixture):
    def setUp(self):
        super().setUp()
        self.ran = self.base / "hook-runs"

    def hook(self, body, directory=None):
        """A pre-push hook that records each run's arguments and stdin, then runs body."""
        path = (directory or self.repo / ".git/hooks") / "pre-push"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'#!/bin/sh\nread -r told\necho "$1 $2 $told" >> "{self.ran}"\n{body}\n')
        host_isolation.executable(path)

    def runs(self):
        return self.ran.read_text().splitlines() if self.ran.exists() else []

    def opt_in(self, covering, branch=None):
        """Name covering in queuekeeper.json, directly on main or on a branch of its own."""
        config = json.loads((self.repo / "queuekeeper.json").read_text())
        config["queue"]["hookCoveredBy"] = covering
        run(self.repo, "git", "fetch", "-q", "origin")
        run(self.repo, "git", "checkout", "-q", "-B", branch or "opt-in", "origin/main")
        (self.repo / "queuekeeper.json").write_text(json.dumps(config))
        run(self.repo, "git", "add", "queuekeeper.json")
        run(self.repo, "git", "commit", "-qm", "Opt in")
        if not branch: run(self.repo, "git", "push", "-q", "origin", "HEAD:main")

    def test_a_passing_hook_lands_and_was_told_what_git_tells_it(self):
        tip = self.commit("a", "a")
        before = run(self.repo, "git", "rev-parse", "origin/main")
        [identifier] = self.add("a")
        self.hook("exit 0")
        self.work()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(self.gates(), 1)
        landed = self.state(identifier)["landedAs"]
        self.assertEqual(run(self.repo, "git", "log", "-1", "--format=%T", landed),
                         run(self.repo, "git", "log", "-1", "--format=%T", tip))
        self.assertEqual(self.runs(), [f"origin {self.origin} HEAD {landed} refs/heads/main {before}"])

    def test_no_hook_lands(self):
        self.commit("a", "a")
        [identifier] = self.add("a")
        self.work()
        self.assertEqual(self.state(identifier)["state"], "landed")

    def test_a_hook_tracked_under_a_relative_hooks_path_runs_from_the_tested_commit(self):
        self.hook(f'grep -q marker hooks/pre-push && touch "{self.base}/saw-tree"', self.repo / "hooks")
        with (self.repo / "hooks/pre-push").open("a") as file: file.write("# marker\n")
        run(self.repo, "git", "add", "hooks")
        run(self.repo, "git", "commit", "-qm", "Track the hook")
        run(self.repo, "git", "push", "-q", "--no-verify", "origin", "HEAD:main")
        run(self.repo, "git", "config", "core.hooksPath", "hooks")
        self.commit("a", "a")
        [identifier] = self.add("a")
        self.work()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(len(self.runs()), 1)
        self.assertTrue((self.base / "saw-tree").exists())

    def test_the_hook_runs_before_any_connection(self):
        """The drop on 2026-10-31 came from a connection held open through the hook's wait."""
        order = self.base / "order"
        wrapper = self.base / "receive-pack"
        wrapper.write_text(f'#!/bin/sh\necho connect >> "{order}"\nexec git-receive-pack "$@"\n')
        host_isolation.executable(wrapper)
        run(self.repo, "git", "config", "remote.origin.receivepack", str(wrapper))
        self.hook(f'echo hook >> "{order}"')
        self.commit("a", "a")
        [identifier] = self.add("a")
        self.work()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(order.read_text().split(), ["hook", "connect"])

    def test_a_hook_refusal_sends_back_after_one_run_and_names_its_log(self):
        self.hook("echo the reason >&2\nexit 1")
        self.commit("a", "a")
        [identifier] = self.add("a")
        self.assertEqual(self.work(), 0)
        self.assertEqual(self.state(identifier)["state"], "failed")
        self.assertNotIn("a", self.main_files())
        self.assertEqual(len(self.runs()), 1)
        entry = self.state(identifier)
        self.assertIsNone(mq.Queue(self.repo).held())
        self.assertIn("refused", entry["why"])
        self.assertIn("the reason", Path(entry["log"]).read_text())
        result = agent_results.entry_result(mq.Queue(self.repo), entry)
        self.assertEqual(result["failure"]["category"], "publication-refused")
        self.assertEqual(result["recovery"]["reasonCode"], "publication-refused")

    def test_no_verdict_from_the_hook_tries_it_again_without_another_gate(self):
        for status in (3, 75, 78):
            with self.subTest(status=status):
                self.ran.unlink(missing_ok=True)
                self.hook(f'[ "$(wc -l < "{self.ran}")" -gt 1 ] && exit 0\nexit {status}')
                gates = self.gates()
                self.commit(f"a{status}", f"a{status}")
                [identifier] = self.add(f"a{status}")
                self.assertEqual(self.work(), 0)
                self.assertEqual(self.state(identifier)["state"], "landed")
                self.assertEqual(len(self.runs()), 2)
                self.assertEqual(self.gates(), gates + 1)

    def test_no_verdict_from_the_hook_waits_for_the_host_before_it_runs_again(self):
        self.hook(f'[ "$(wc -l < "{self.ran}")" -gt 1 ] && exit 0\nexit 3')
        self.commit("a", "a")
        [identifier] = self.add("a")
        busy = ("memory", "memory pressure is level 2 (warning), not 1 (normal)")
        with self.host([busy, None]) as (slept, said):
            self.assertEqual(self.work(), 0)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(slept, [mq.SETTLE_POLL])
        self.assertEqual(len(self.runs()), 2)
        self.assertIn("memory pressure is level 2 (warning), not 1 (normal): the pre-push hook runs again once the host "
                      "settles", said.getvalue())

    def test_a_hook_that_gave_no_verdict_comes_back_when_the_host_stays_busy(self):
        self.hook("exit 3")
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.host([NoVerdict.BUSY]) as (slept, _):
            self.assertEqual(self.work(), 0)
        self.assertEqual(sum(slept), mq.SETTLE_LIMIT)
        self.assertEqual(len(self.runs()), 1)
        self.assertEqual(self.state(identifier)["state"], "failed")
        self.assertIsNone(mq.Queue(self.repo).held())
        why = self.state(identifier)["why"]
        self.assertIn("the pre-push hook gave no verdict, exit 3, on", why)
        self.assertIn(f"the host did not settle in {mq.SETTLE_LIMIT}s", why)

    def test_a_hook_that_keeps_giving_no_verdict_comes_back_without_a_test_failure(self):
        self.hook("exit 75")
        self.commit("a", "a")
        [identifier] = self.add("a")
        self.assertEqual(self.work(), 0)
        self.assertEqual(self.state(identifier)["state"], "failed")
        self.assertEqual(len(self.runs()), 2)
        self.assertEqual(self.gates(), 1)
        self.assertIn("no verdict, exit 75", self.state(identifier)["why"])
        self.assertIsNone(mq.Queue(self.repo).held())

    def test_a_dropped_connection_does_not_run_the_hook_again(self):
        self.hook("exit 0")
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.remote_says("fatal: the remote end hung up unexpectedly", {1}, "push"): self.work()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(len(self.runs()), 1)

    def test_a_carried_over_commit_is_a_new_commit_and_runs_the_hook_again(self):
        outside = self.base / "outside"
        run(self.base, "git", "clone", "-q", str(self.origin), str(outside))
        (outside / "records").mkdir()
        (outside / "records/one.json").write_text("{}")
        run(outside, "git", "add", "records")
        run(outside, "git", "commit", "-qm", "A record")
        self.hook(f'[ "$(wc -l < "{self.ran}")" -gt 1 ] && exit 0\n'
                  f'unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE\ngit -C "{outside}" push -q origin HEAD:main\nexit 0')
        self.commit("a", "a")
        [identifier] = self.add("a")
        self.work()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(self.gates(), 1)
        first, second = [line.split()[3] for line in self.runs()]
        self.assertNotEqual(first, second)
        self.assertEqual(second, self.state(identifier)["landedAs"])
        self.assertLessEqual({"a", "records"}, self.main_files())

    def test_main_opting_in_replaces_the_hook_and_says_so(self):
        self.opt_in({"gate": "all the hook runs"})
        self.hook("exit 1")
        self.commit("a", "a")
        [identifier] = self.add("a")
        said = []
        with patch.object(mq.Worker, "say", lambda worker, words: said.append(words)): self.work()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(self.runs(), [])
        self.assertTrue(any("hookCoveredBy says gate" in words for words in said), said)

    def test_a_covering_lane_that_fails_still_stops_the_commit(self):
        self.opt_in({"gate": "all the hook runs"})
        self.hook("exit 0")
        self.commit("bad", "bad")
        [identifier] = self.add("bad")
        self.work()
        self.assertEqual(self.state(identifier)["state"], "failed")
        self.assertNotIn("bad", self.main_files())

    def test_a_branch_cannot_opt_itself_out_of_the_hook(self):
        self.opt_in({"gate": "all the hook runs"}, branch="sneaks")
        self.hook("exit 1")
        [identifier] = self.add("sneaks")
        self.assertEqual(self.work(), 0)
        self.assertEqual(self.state(identifier)["state"], "failed")
        self.assertEqual(len(self.runs()), 1)

    def test_the_commit_that_first_names_the_lanes_cannot_opt_out_either(self):
        config = json.loads((self.repo / "queuekeeper.json").read_text())
        config["lanes"]["checks"] = config["lanes"].pop("gate")
        (self.repo / "queuekeeper.json").write_text(json.dumps(config))
        run(self.repo, "git", "add", "queuekeeper.json")
        run(self.repo, "git", "commit", "-qm", "No gate lane yet")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        run(self.repo, "git", "checkout", "-q", "-B", "name", "origin/main")
        config["queue"].update(lanes=["checks"], hookCoveredBy={"checks": "all the hook runs"})
        (self.repo / "queuekeeper.json").write_text(json.dumps(config))
        run(self.repo, "git", "add", "queuekeeper.json")
        run(self.repo, "git", "commit", "-qm", "Name the lanes")
        self.hook("exit 1")
        [identifier] = self.add("name")
        self.assertEqual(self.work(), 0)
        self.assertEqual(self.state(identifier)["state"], "failed")
        self.assertEqual(len(self.runs()), 1)

    def test_the_opt_in_must_name_lanes_the_queue_runs_and_what_they_cover(self):
        config = json.loads((self.repo / "queuekeeper.json").read_text())
        config["lanes"]["other"] = {"command": ["true"]}
        for bad in ({"other": "not a queue lane"}, {"absent": "no such lane"}, {"gate": ""}, {"gate": "  "}, {"gate": 1},
                    {}, ["gate"], "gate"):
            with self.subTest(bad=bad):
                (self.repo / "queuekeeper.json").write_text(json.dumps({**config, "queue": {"hookCoveredBy": bad}}))
                with self.assertRaisesRegex(ValueError, "hookCoveredBy"): self.config()
        for queue in ({"hookCoveredBy": {"gate": "the default lane"}},
                      {"lanes": ["other"], "hookCoveredBy": {"other": "a named lane"}}):
            (self.repo / "queuekeeper.json").write_text(json.dumps({**config, "queue": queue}))
            self.assertEqual(self.config()["queue"], queue)
        # A named list replaces the default gate lane.
        (self.repo / "queuekeeper.json").write_text(json.dumps(
            {**config, "queue": {"lanes": ["other"], "hookCoveredBy": {"gate": "not run"}}}))
        with self.assertRaisesRegex(ValueError, "hookCoveredBy"): self.config()

# What each check saw, a line a run, and it fails while the file its first argument names is in the tree.
CHECK = """import pathlib, subprocess, sys
seen = subprocess.run(["git", "diff", "--name-only", sys.argv[3], "HEAD"], capture_output=True, text=True).stdout.split()
with open(sys.argv[2], "a") as log: log.write(" ".join(seen) + "\\n")
sys.exit(1 if pathlib.Path(sys.argv[1]).exists() else 0)
"""


class CarryOverChecks(Fixture):
    during_gate = MainMoves.during_gate

    def setUp(self):
        super().setUp()
        self.seen = self.base / "seen"
        (self.repo / "check.py").write_text(CHECK)
        run(self.repo, "git", "add", "check.py")
        run(self.repo, "git", "commit", "-qm", "Add a check")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")

    def name_checks(self, *checks):
        """Put queue.carryOverChecks on main, as a project does, and return that commit."""
        config = json.loads((self.repo / "queuekeeper.json").read_text())
        config["queue"]["carryOverChecks"] = list(checks)
        (self.repo / "queuekeeper.json").write_text(json.dumps(config))
        run(self.repo, "git", "add", "queuekeeper.json")
        run(self.repo, "git", "commit", "-qm", "Name the carry-over checks")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        return run(self.repo, "git", "rev-parse", "HEAD")

    def check(self, fails_on="absent", base="origin/main", **more):
        return {"command": ["python3", "check.py", fails_on, str(self.seen), base], **more}

    def runs(self):
        return self.seen.read_text().splitlines() if self.seen.exists() else []

    def test_a_passing_check_carries_on_the_rebased_tree(self):
        self.name_checks(self.check(base="{base}"))
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.during_gate("records/one.json"): self.work()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(self.gates(), 1)
        # {base} reached the check, which then saw main's move and the batch together.
        self.assertEqual(self.runs(), ["a records/one.json"])

    def test_a_check_against_origin_main_does_not_see_the_move(self):
        """Why {base} exists: origin/main is already the new main when a check runs."""
        self.name_checks(self.check())
        self.commit("a", "a")
        self.add("a")
        with self.during_gate("records/one.json"): self.work()
        self.assertEqual(self.runs(), ["a"])

    def test_a_failing_check_gates_again_with_no_blame(self):
        self.name_checks(self.check(fails_on="records/one.json"))
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.during_gate("records/one.json"), patch("builtins.print") as said: self.work()
        self.assertEqual(self.gates(), 2)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertNotIn("why", self.state(identifier))
        self.assertIsNone(mq.Queue(self.repo).held())
        self.assertIn("gating again", " ".join(str(call) for call in said.call_args_list))

    def test_checks_stop_at_the_first_failure(self):
        self.name_checks(self.check(fails_on="records/one.json"), self.check())
        self.commit("a", "a")
        self.add("a")
        with self.during_gate("records/one.json"): self.work()
        self.assertEqual(len(self.runs()), 1)

    def test_a_check_is_not_run_without_a_carry_over(self):
        self.name_checks(self.check())
        self.commit("a", "a")
        self.add("a")
        self.work()
        self.assertEqual(self.runs(), [])
        self.commit("b", "b")
        self.add("b")
        with self.during_gate("code"): self.work()
        self.assertEqual(self.runs(), [])
        self.assertEqual(self.gates(), 3)

    def test_paths_limit_a_check_to_moves_of_them(self):
        self.name_checks(self.check(paths=["records/"]))
        self.commit("a", "a")
        self.add("a")
        with self.during_gate("doc/other.md"): self.work()
        self.assertEqual(self.runs(), [])
        self.assertEqual(self.gates(), 1)

    def test_paths_name_a_directory(self):
        self.name_checks(self.check(paths=["records/"]))
        self.commit("a", "a")
        self.add("a")
        with self.during_gate("records/one.json"): self.work()
        self.assertEqual(len(self.runs()), 1)

    def test_paths_name_a_file(self):
        self.name_checks(self.check(paths=["doc/guide.md"]))
        self.commit("a", "a")
        self.add("a")
        with self.during_gate("doc/guide.md"): self.work()
        self.assertEqual(len(self.runs()), 1)

    def test_a_check_that_cannot_start_gates_again(self):
        self.name_checks({"command": ["/nonexistent/check"]})
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.during_gate("records/one.json"): self.work()
        self.assertEqual(self.gates(), 2)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertIsNone(mq.Queue(self.repo).held())

    def test_a_check_past_its_limit_gates_again(self):
        self.name_checks({"command": ["python3", "hangs.py"]})
        self.commit("a", "a")
        [identifier] = self.add("a")
        real = mq.processes.run
        def bounded(command, **options):
            if "hangs.py" in command: raise mq.processes.TimedOut("past its limit")
            return real(command, **options)
        with self.during_gate("records/one.json"), patch.object(mq.processes, "run", bounded): self.work()
        self.assertEqual(self.gates(), 2)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertIsNone(mq.Queue(self.repo).held())

    def test_a_document_that_fails_the_docs_check_gates_again(self):
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.during_gate("README.md", "[gone](missing.md)\n"): self.work()
        self.assertEqual(self.gates(), 2)
        self.assertEqual(self.state(identifier)["state"], "landed")

    def test_a_document_that_passes_the_docs_check_carries(self):
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.during_gate("README.md", "fine\n"): self.work()
        self.assertEqual(self.gates(), 1)
        self.assertEqual(self.state(identifier)["state"], "landed")

    def test_the_configuration_is_checked(self):
        config = json.loads((self.repo / "queuekeeper.json").read_text())
        good = {"command": ["python3", "x.py"]}
        for checks in ([], [good], [{**good, "paths": ["records/", "guide.md"]}]):
            (self.repo / "queuekeeper.json").write_text(json.dumps({**config, "queue": {"carryOverChecks": checks}}))
            self.assertEqual(self.config()["queue"]["carryOverChecks"], checks)
        for bad in ("python3 x.py", {"command": ["x"]}, ["python3 x.py"], [["python3", "x.py"]], [{}], [{"command": []}],
                    [{"command": "python3 x.py"}], [{"command": ["python3", ""]}], [{"command": ["x"], "other": 1}],
                    [{"command": ["x"], "paths": []}], [{"command": ["x"], "paths": "records/"}],
                    [{"command": ["x"], "paths": [""]}]):
            with self.subTest(bad=bad):
                (self.repo / "queuekeeper.json").write_text(json.dumps({**config, "queue": {"carryOverChecks": bad}}))
                with self.assertRaisesRegex(ValueError, "carryOverChecks"): self.config()

    def test_the_schema_knows_the_setting(self):
        schema = json.loads((ROOT / "schemas/queuekeeper.schema.json").read_text())
        check = schema["properties"]["queue"]["properties"]["carryOverChecks"]["items"]
        self.assertEqual(set(check["properties"]), {"command", "paths"})


class DocsOnly(Fixture):
    def setUp(self):
        super().setUp()
        self.docs_counter, self.docs_status = self.base / "docs-runs", self.base / "docs-status"
        self.ran = self.base / "hook-runs"
        config = json.loads((self.repo / "queuekeeper.json").read_text())
        config["lanes"]["docs"] = {"command": ["python3", "lane.py", str(self.docs_counter), str(self.docs_status)]}
        paths = {**config["queue"]["unread"]["paths"], "doc/": "no code lane reads it", "README.md": "nor this"}
        self.configured = config
        self.publish({**config, "queue": {**config["queue"], "unread": {"paths": paths, "defaults": False}}},
                     "Declare the docs")

    def docs_runs(self):
        return len(self.docs_counter.read_text()) if self.docs_counter.exists() else 0

    def test_a_docs_only_batch_runs_only_the_docs_lanes_and_lands_as_one(self):
        self.commit("doc/guide.md", "guide"); self.commit("README.md", "readme"); self.commit("doc/papers.tsv", "tsv")
        ids = self.add("guide", "readme", "tsv")
        said = []
        with patch.object(mq.Worker, "say", lambda worker, words: said.append(words)): self.assertEqual(self.work(), 0)
        self.assertEqual([self.state(i)["state"] for i in ids], ["landed"] * 3)
        self.assertEqual((self.gates(), self.docs_runs()), (0, 1))
        self.assertEqual([self.state(i)["docsOnly"] for i in ids], [["docs"]] * 3)
        self.assertLessEqual({"doc", "README.md"}, self.main_files())
        self.assertTrue(any("by main's queue.unread: running only docs" in words for words in said), said)
        self.assertIn("docs lanes only (docs): no gate of that commit", mq.describe(self.state(ids[0])))

    def test_a_docs_only_batch_runs_the_lanes_its_paths_name_and_records_why(self):
        """vocaro/queuekeeper#27: a path that one small check reads names that check's lane, and a docs-only batch
        that changes it runs that lane beside the docs lanes, never the gate, and its landing says so."""
        privacy = self.base / "privacy-runs"
        config = json.loads(json.dumps(self.configured))
        config["lanes"]["privacy"] = {"command": ["python3", "lane.py", str(privacy), str(self.base / "privacy-status")]}
        why = "only the privacy check reads it"
        unread = {"paths": {**config["queue"]["unread"]["paths"], "doc/release/": {"why": why, "lanes": ["privacy"]}}}
        self.publish({**config, "queue": {**config["queue"], "unread": unread}}, "Name the privacy lane")
        (self.repo / "doc/release").mkdir(parents=True, exist_ok=True)
        self.commit("doc/release/notes.md", "notes")
        [notes] = self.add("notes")
        said = []
        with patch.object(mq.Worker, "say", lambda worker, words: said.append(words)): self.assertEqual(self.work(), 0)
        state = self.state(notes)
        self.assertEqual((state["state"], self.gates(), len(privacy.read_text())), ("landed", 0, 1))
        self.assertEqual(state["docsOnly"], ["docs", "privacy"])
        self.assertEqual(state["docsNamed"], {"privacy": {"doc/release/": why}})
        named = f"privacy for doc/release/ ({why})"
        self.assertIn(f"queue.unread.paths named {named}", mq.describe(state))
        self.assertTrue(any(f"queue.unread.paths names {named}" in words for words in said), said)

    def landed_order(self, *ids):
        """The entries' landed commits, oldest on main first."""
        run(self.repo, "git", "fetch", "-q", "origin")
        history = run(self.repo, "git", "rev-list", "--reverse", "origin/main").split()
        return sorted(ids, key=lambda identifier: history.index(self.state(identifier)["landedAs"]))

    def test_a_docs_only_entry_queued_with_code_never_rides_the_code_gate(self):
        """vocaro-engineering#394: queued behind and beside code entries, a docs-only entry lands first, on the docs
        lanes alone, and the code entries gate without it. The old worker gated all three together, every lane."""
        self.commit("code", "code"); self.commit("doc/guide.md", "guide"); self.commit("more", "more")
        code, guide, more = self.add("code", "guide", "more")
        said = []
        with patch.object(mq.Worker, "say", lambda worker, words: said.append(words)): self.assertEqual(self.work(), 0)
        self.assertEqual([self.state(i)["state"] for i in (code, guide, more)], ["landed"] * 3)
        self.assertEqual((self.gates(), self.docs_runs()), (1, 1))
        self.assertEqual(self.state(guide)["docsOnly"], ["docs"])
        self.assertNotIn("gatedOn", self.state(guide))
        self.assertIn("docs lanes only (docs): no gate of that commit", mq.describe(self.state(guide)))
        for identifier in (code, more):
            self.assertNotIn("docsOnly", self.state(identifier))
            self.assertIn("gatedOn", self.state(identifier))
        self.assertEqual(self.landed_order(code, guide, more), [guide, code, more])
        self.assertTrue(any("1 docs-only entry lands first, on the docs lanes alone" in words for words in said), said)

    def both(self):
        """A branch, both, whose one commit changes doc/guide.md and code."""
        run(self.repo, "git", "checkout", "-q", "-B", "both", "origin/main")
        (self.repo / "doc").mkdir(exist_ok=True)
        (self.repo / "doc/guide.md").write_text("guide")
        (self.repo / "code").write_text("code")
        run(self.repo, "git", "add", "doc/guide.md", "code")
        run(self.repo, "git", "commit", "-qm", "Both")

    def test_one_entry_that_changes_docs_and_code_runs_every_lane(self):
        """The control: an entry is docs-only by its own paths, and one that changes code beside a document gates,
        and runs the docs lanes as well (vocaro/queuekeeper#122)."""
        self.both()
        self.commit("README.md", "readme", "readme", start="origin/main")
        both, readme = self.add("both", "readme")
        said = []
        with patch.object(mq.Worker, "say", lambda worker, words: said.append(words)): self.work()
        self.assertEqual([self.state(i)["state"] for i in (both, readme)], ["landed"] * 2)
        self.assertEqual((self.gates(), self.docs_runs()), (1, 2))
        self.assertNotIn("docsOnly", self.state(both))
        self.assertIn("gatedOn", self.state(both))
        self.assertEqual(self.state(readme)["docsOnly"], ["docs"])
        self.assertTrue(any("the batch changes documents beside code: running docs as well as the gate" in words
                            for words in said), said)

    def landed_saying(self, branch, **options):
        """What qk land prints to standard output as it queues branch, with no worker started."""
        said = io.StringIO()
        with patch.object(mq.Queue, "start"), contextlib.redirect_stdout(said):
            mq.add(self.repo, branch, **options)
        return said.getvalue()

    def test_qk_land_says_which_gate_each_entry_rides(self):
        """vocaro/queuekeeper#118: an entry that changed README.md with a stylesheet and screenshots was expected to
        land as documentation and rode the full gate, which its author learned only by waiting."""
        self.both()
        self.assertRegex(self.landed_saying("both"), r"queue: \S+ rides the code gate: code is not documentation; "
                                                     r"the docs lane runs too, ahead of it, for its documents\n")
        self.commit("README.md", "readme", "readme")
        self.assertRegex(self.landed_saying("readme"), r"queue: \S+ is documentation only: lands on the docs lane, "
                                                       r"not the code gate\n")
        self.commit("app.css", "style")
        self.assertRegex(self.landed_saying("style"), r"queue: \S+ rides the code gate: app.css is not documentation\n")
        # An ungated landing says what it does instead.
        self.commit("other.css", "ungated")
        said = self.landed_saying("ungated", no_gate=True, why="an incident")
        self.assertNotIn("gate:", said)
        self.assertNotIn("documentation only", said)
        self.assertIn("lands alone on the preflight lanes, without the gate", said)

    def test_an_entry_that_changes_code_and_a_document_its_docs_lane_fails_is_sent_back(self):
        """vocaro/queuekeeper#122: on 2026-11-12 vocaro/mpm's gate passed a commit that changed tooling and took
        AGENTS.md, a queue.docsOnly path, past what its docs lane allows, three times, as no gate lane held that check. The
        docs lanes run ahead of the gate, so such an entry is sent back before the gate starts."""
        self.docs_status.write_text("1")
        self.both()
        [both] = self.add("both")
        self.work()
        self.assertEqual(self.state(both)["state"], "failed")
        self.assertEqual((self.gates(), self.docs_runs()), (0, 1))
        self.assertNotIn("code", self.main_files())

    def test_the_docs_lanes_a_batch_of_documents_and_code_runs_too(self):
        lanes = {"gate": {}, "docs": {}, "notes": {}, "both": {"parallel": ["gate", "notes"]}}
        declared = {"paths": {"doc/": "no code lane reads it"}, "defaults": False}
        config = {"lanes": lanes, "queue": {"unread": declared}}
        self.assertEqual(mq.lanes_of(config, ["doc/a.md", "src/a.py"]), ["docs", "gate"])
        self.assertEqual(mq.lanes_of(config, ["src/a.py"]), ["gate"])
        # A batch of documents alone takes the docs lanes instead (docs_lanes), never these.
        self.assertEqual(mq.lanes_of(config, ["doc/a.md"]), ["gate"])
        self.assertEqual(mq.lanes_of(config), ["gate"])
        # A lane the gate runs, itself or as a group's member, runs once; queue.unread false gates as before.
        self.assertEqual(mq.lanes_of({"lanes": lanes, "queue": {"lanes": ["docs", "gate"], "unread": declared}},
                                     ["doc/a.md", "src/a.py"]), ["docs", "gate"])
        grouped = {"lanes": lanes, "queue": {"lanes": ["both"], "unread": {**declared, "lanes": ["notes"]}}}
        self.assertEqual(mq.lanes_of(grouped, ["doc/a.md", "src/a.py"]), ["both"])
        self.assertEqual(mq.lanes_of({"lanes": lanes, "queue": {"unread": False}}, ["doc/a.md", "src/a.py"]),
                         ["gate"])
        # The defaults: conventional Markdown beside code runs the docs lane, the toolkit's where there is none.
        self.assertEqual(mq.lanes_of({"lanes": {"gate": {}}}, ["README.md", "AGENTS.md"]), ["docs", "gate"])
        # A preflight lane runs first, and once, though the docs lanes would run it too.
        listed = {"lanes": {**lanes, "lint": {}}, "queue": {"unread": {**declared, "lanes": ["docs", "lint"]},
                                                          "preflight": ["lint"]}}
        self.assertEqual(mq.lanes_of(listed, ["doc/a.md", "src/a.py"]), ["lint", "docs", "gate"])

    def test_a_docs_entry_that_changes_a_path_an_older_entry_changes_waits_for_it(self):
        """Order is kept where two entries change a path in common: the docs entry waits for the older code entry,
        and then lands on the docs lanes in a round of its own, not in the code entry's gate."""
        (self.repo / "doc").mkdir(exist_ok=True)
        (self.repo / "doc/guide.md").write_text("one\ntwo\nthree\nfour\nfive\n")
        run(self.repo, "git", "add", "doc/guide.md")
        run(self.repo, "git", "commit", "-qm", "A guide")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        run(self.repo, "git", "checkout", "-q", "-B", "code", "origin/main")
        (self.repo / "doc/guide.md").write_text("ONE\ntwo\nthree\nfour\nfive\n")
        (self.repo / "code").write_text("code")
        run(self.repo, "git", "add", "doc/guide.md", "code")
        run(self.repo, "git", "commit", "-qm", "Code and its guide")
        run(self.repo, "git", "checkout", "-q", "-B", "guide", "origin/main")
        (self.repo / "doc/guide.md").write_text("one\ntwo\nthree\nfour\nFIVE\n")
        run(self.repo, "git", "commit", "-qam", "The guide alone")
        code, guide = self.add("code", "guide")
        said = []
        with patch.object(mq.Worker, "say", lambda worker, words: said.append(words)): self.work()
        self.assertEqual([self.state(i)["state"] for i in (code, guide)], ["landed"] * 2)
        # The code entry's gate runs the docs lanes too, for the guide it changes (vocaro/queuekeeper#122).
        self.assertEqual((self.gates(), self.docs_runs()), (1, 2))
        self.assertEqual(self.landed_order(code, guide), [code, guide])
        self.assertEqual(self.state(guide)["docsOnly"], ["docs"])
        self.assertTrue(any("wait for a round of their own on the docs lanes" in words for words in said), said)

    def test_an_undeclared_project_uses_the_default_docs_paths(self):
        self.publish(self.configured, "Declare nothing")
        self.commit("doc/guide.md", "guide")
        [identifier] = self.add("guide")
        self.work()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual((self.gates(), self.docs_runs()), (0, 1))
        self.assertEqual(self.state(identifier)["docsOnly"], ["docs"])

    def test_a_path_beside_a_listed_file_or_directory_runs_every_lane(self):
        for name in ("docs/guide.md", "README.md.orig", "doc"):
            with self.subTest(name=name):
                self.counter.unlink(missing_ok=True)
                self.commit(name, name.replace("/", "-"))
                [identifier] = self.add(name.replace("/", "-"))
                self.work()
                self.assertEqual(self.state(identifier)["state"], "landed")
                self.assertEqual((self.gates(), self.docs_runs()), (1, 0))

    def test_a_file_moved_into_docs_names_the_path_it_left(self):
        self.commit("tool.py", "tool", "print('a tool')\n")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        self.counter.unlink(missing_ok=True)
        run(self.repo, "git", "checkout", "-q", "-B", "move", "origin/main")
        (self.repo / "doc").mkdir(exist_ok=True)
        run(self.repo, "git", "mv", "tool.py", "doc/tool.py")
        run(self.repo, "git", "commit", "-qm", "Move the tool")
        [identifier] = self.add("move")
        self.work()
        self.assertEqual(self.state(identifier)["state"], "landed")
        # The gate, for the path it left, and the docs lanes beside it, for the path it now has (#122).
        self.assertEqual((self.gates(), self.docs_runs()), (1, 1))

    def test_a_bisects_gates_look_for_docs_entries_to_land_beside_them(self):
        """A bisect's gates stack nothing, but docs entries that arrive during them still land beside them: the old
        worker ran a bisect's gates with no watch at all, so a docs entry waited out every half."""
        self.commit("bad", "bad"); self.commit("code", "code")
        ids = self.add("bad", "code")
        watched, real = [], mq.Worker.watched
        def watch(worker, runner, config, lanes, base, head, applied, gate, stacks=True):
            watched.append((len(applied), stacks))
            return real(worker, runner, config, lanes, base, head, applied, gate, stacks=stacks)
        with patch.object(mq.Worker, "watched", watch), patch.object(mq, "STACK_POLL", 0.05): self.work()
        self.assertEqual([self.state(i)["state"] for i in ids], ["failed", "landed"])
        # The bisect's round, the older half gating with the newer beside it, ran under the watch, stacking nothing.
        self.assertIn((2, False), watched)

    def test_a_failing_docs_lane_sends_the_batch_back(self):
        self.docs_status.write_text("1")
        self.commit("doc/guide.md", "guide")
        [identifier] = self.add("guide")
        self.work()
        self.assertEqual(self.state(identifier)["state"], "failed")
        self.assertEqual((self.gates(), self.docs_runs()), (0, 1))
        self.assertNotIn("doc", self.main_files())

    def test_a_branch_cannot_declare_its_own_docs(self):
        # Main has no gate lane yet, so the round reads the batch's lanes, but never its queue.unread.
        config = json.loads(json.dumps(self.configured))
        config["lanes"]["checks"] = config["lanes"].pop("gate")
        self.publish(config, "No gate lane yet")
        run(self.repo, "git", "checkout", "-q", "-B", "declare", "origin/main")
        config["queue"].update(lanes=["checks"], unread={"paths": {"queuekeeper.json": "it says so"}})
        (self.repo / "queuekeeper.json").write_text(json.dumps(config))
        run(self.repo, "git", "add", "queuekeeper.json")
        run(self.repo, "git", "commit", "-qm", "Declare the docs here")
        [identifier] = self.add("declare")
        self.work()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual((self.gates(), self.docs_runs()), (1, 0))

    def test_a_hook_the_code_lanes_cover_does_not_run(self):
        # The hook is part of lanes that read none of the batch's paths, so it can say nothing new of them.
        config = json.loads((self.repo / "queuekeeper.json").read_text())
        config["queue"]["hookCoveredBy"] = {"gate": "all the hook runs"}
        self.publish(config, "Opt in")
        PrePushHooks.hook(self, "exit 1")
        self.commit("doc/guide.md", "guide")
        [identifier] = self.add("guide")
        self.work()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual((self.gates(), self.docs_runs(), PrePushHooks.runs(self)), (0, 1, []))

    def test_a_docs_only_hook_no_lane_covers_is_skipped(self):
        PrePushHooks.hook(self, "exit 0")
        self.commit("doc/guide.md", "guide")
        [identifier] = self.add("guide")
        self.work()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(len(PrePushHooks.runs(self)), 0)

class DocsFirst(unittest.TestCase):
    """Which waiting entries go ahead on the docs lanes (mq.docs_first), and what a code batch keeps (mq.without_docs),
    by each entry's own paths (vocaro-engineering#394)."""
    PATHS = {"code": {"src/a.py"}, "guide": {"doc/guide.md"}, "readme": {"README.md"}, "both": {"doc/guide.md", "src/b.py"},
             "shared": {"doc/shared.md"}, "pdf": {"doc/paper.pdf"}}

    def entries(self, *names, **fields):
        return [{"id": name, "state": "queued", **fields.get(name, {})} for name in names]

    def first(self, *names, config=None, **options):
        found = mq.docs_first(self.entries(*names, **options.pop("fields", {})), config or {},
                              lambda entry: self.PATHS[entry["id"]], **options)
        return [entry["id"] for entry in found]

    def test_docs_entries_go_ahead_of_code_that_changes_none_of_their_paths(self):
        self.assertEqual(self.first("code", "guide", "readme"), ["guide", "readme"])

    def test_an_older_entry_that_changes_the_same_path_keeps_its_place(self):
        self.PATHS = {**self.PATHS, "edit": {"doc/guide.md", "src/c.py"}}
        self.assertEqual(self.first("edit", "guide", "readme"), ["readme"])
        # A docs entry held back holds back the later one that changes its path too.
        self.assertEqual(self.first("edit", "guide", "readme", "both"), ["readme"])

    def test_an_entry_put_first_or_landing_ungated_or_testing_stays_in_line(self):
        fields = {"guide": {"front": True}, "readme": {"noGate": True}, "shared": {"state": "testing"}}
        self.assertEqual(self.first("guide", "readme", "shared", fields=fields), [])

    def test_an_entry_waits_for_the_entries_its_docs_lanes_failed_ahead_of(self):
        fields = {"guide": {"waitsFor": ["code"]}}
        self.assertEqual(self.first("code", "guide", fields=fields), [])
        self.assertEqual(self.first("guide", fields=fields), ["guide"])
        # Nor does it go beside the gate of the entry it waits for.
        self.assertEqual(self.first("guide", fields=fields, gating={"code"}), [])

    def test_a_gating_batch_blocks_its_paths_and_an_unread_path_of_any_kind_goes_beside_it(self):
        self.assertEqual(self.first("guide", "readme", blocked={"README.md"}), ["guide"])
        # A paper under a declared unread folder both skips the gate and carries over, so it goes beside a gate too.
        config = {"queue": {"unread": {"paths": {"doc/": "prose and papers"}}}}
        self.assertEqual(self.first("pdf", "guide", config=config, blocked={"src/a.py"}), ["pdf", "guide"])
        self.assertTrue(mq.carried_over("doc/paper.pdf", config, {"src/a.py"}))

    def test_a_code_batch_leaves_out_its_docs_entries_up_to_code_that_changes_their_paths(self):
        changed_by = lambda entry: self.PATHS[entry["id"]]
        kept = lambda *names: [entry["id"] for entry in mq.without_docs(self.entries(*names), {}, changed_by)]
        self.assertEqual(kept("code", "guide", "readme"), ["code"])
        self.assertEqual(kept("guide", "code", "both"), ["code"])
        self.assertEqual(kept("code", "pdf"), ["code", "pdf"])


class Riding(unittest.TestCase):
    """rides, the line qk land prints of the gate an entry will ride (vocaro/queuekeeper#118)."""

    def test_the_first_path_that_is_not_documentation_is_named(self):
        config = {"lanes": {"gate": {}}}
        self.assertEqual(mq.rides(config, {"README.md", "doc/a.md"}),
                         "is documentation only: lands on the docs lane, not the code gate")
        self.assertEqual(mq.rides(config, {"README.md", "static/app.css", "shot.png"}),
                         "rides the code gate: shot.png is not documentation; the docs lane runs too, ahead of it, "
                         "for its documents")
        listed = {"lanes": {"gate": {}, "lint": {}}, "queue": {"preflight": ["lint"]}}
        self.assertEqual(mq.rides(listed, {"doc/a.md"}),
                         "is documentation only: lands on the lint and docs lanes, not the code gate")
        self.assertEqual(mq.rides(config, set()), "rides the code gate: its commits change no file")
        self.assertEqual(mq.rides({"lanes": {"gate": {}}, "queue": {"unread": False}}, {"README.md"}),
                         "rides the code gate: main's queue.unread is false, so every entry does")

    def test_a_move_out_of_documentation_names_the_path_it_left_or_took(self):
        """Both sides of a move arrive, as docs_lanes reads them, so a document moved into source names its new path."""
        self.assertEqual(mq.rides({"lanes": {"gate": {}}}, {"doc/a.md", "src/a.md"}),
                         "rides the code gate: src/a.md is not documentation; the docs lane runs too, ahead of it, "
                         "for its documents")


class DocsCarryPolicy(unittest.TestCase):
    def test_known_inputs_override_carry_permissions_and_false_carries_nothing(self):
        excluded = {"queue": {"unread": {"paths": {"docs/": "prose unless excluded"},
                                         "exclude": {"docs/": "code reads these inputs"}, "defaults": False}}}
        self.assertFalse(mq.carried_over("docs/input.md", excluded, []))
        self.assertFalse(mq.carried_over("README.md", excluded, []))
        self.assertFalse(mq.carried_over("README.md", {"queue": {"unread": False}}, []))
        self.assertFalse(mq.carried_over("records/input.json", {"queue": {"unread": False}}, []))
        listed = {"queue": {"unread": {"paths": {"records/": "not read by code"}}}}
        self.assertTrue(mq.carried_over("README.md", listed, []))
        self.assertTrue(mq.carried_over("records/input.json", listed, []))
        self.assertFalse(mq.carried_over("records/input.json", listed, ["records/input.json"]))


class Unread(unittest.TestCase):
    """queue.unread, the one list of paths no code lane reads, which both a docs-only batch and a carry-over read, and
    the refusal of the older queue.docsOnly and queue.carryOver it replaced (vocaro/queuekeeper#189)."""
    LANES = {"gate": {"command": ["true"]}, "notes": {"command": ["true"]}}

    def parsed(self, queue):
        return configuration.parse(json.dumps({"schemaVersion": 1, "name": "demo", "lanes": self.LANES, "queue": queue}))

    def test_the_defaults_paths_exclude_and_false(self):
        cases = [({}, "README.md", True), ({}, "doc/a.md", True), ({}, "doc/a.markdown", True),
                 ({}, "AGENTS.md", False), ({}, "src/NOTES.md", False), ({}, "doc/a.png", False),
                 ({"unread": {"paths": {"records/": "why"}}}, "records/a.json", True),
                 ({"unread": {"paths": {"records/": "why"}}}, "README.md", True),
                 ({"unread": {"paths": {"records/": "why"}, "defaults": False}}, "README.md", False),
                 ({"unread": {"exclude": {"doc/input.md": "a test reads it"}}}, "doc/input.md", False),
                 ({"unread": {"paths": {"doc/": "why"}, "exclude": {"doc/x/": "why"}}}, "doc/x/a.png", False),
                 ({"unread": False}, "README.md", False)]
        for queue, path, expected in cases:
            with self.subTest(queue=queue, path=path):
                self.assertIs(configuration.unread({"queue": queue}, path), expected)

    def test_one_reading_decides_both_a_gate_skip_and_a_carry_over(self):
        for queue in ({}, {"unread": {"paths": {"records/": "why"}}}, {"unread": False},
                      {"unread": {"paths": {"doc/": "why", "records/": "why"}, "defaults": False}}):
            config = {"queue": queue}
            for path in ("README.md", "AGENTS.md", "doc/a.md", "doc/a.png", "records/a.json", "src/a.py"):
                with self.subTest(queue=queue, path=path):
                    self.assertEqual(mq.docs_lanes(config, [path]) is not None, mq.carried_over(path, config, set()))

    def test_the_older_settings_are_refused_with_the_unread_to_give(self):
        """vocaro/vocaro-engineering#504 moved every project to queue.unread, so queue.docsOnly and queue.carryOver are
        refused, as retired() refuses buildHost, each saying the queue.unread that reads the same, or the choice to
        make where none does (vocaro/queuekeeper#189)."""
        self.assertEqual(configuration.unread_declared({}), {})
        cases = [({"carryOver": {"records/": "why"}},
                  'queuekeeper.json gives queue.carryOver, which queue.unread replaced (vocaro/queuekeeper#189): give '
                  '"queue": {"unread": {"paths": {"records/": "why"}}} in its place'),
                 ({"docsOnly": {"paths": {"doc/": "a"}, "lanes": ["notes"], "exclude": {"doc/x.md": "b"}},
                   "carryOver": {"records/": "c"}},
                  'gives queue.docsOnly and queue.carryOver, which queue.unread replaced (vocaro/queuekeeper#189): give '
                  '"queue": {"unread": {"defaults": false, "exclude": {"doc/x.md": "b"}, "lanes": ["notes"], '
                  '"paths": {"doc/": "a", "records/": "c"}}} in its place'),
                 ({"docsOnly": {"paths": {"doc/": "a"}, "lanes": ["docs"]}},
                  '"queue": {"unread": {"defaults": false, "paths": {"doc/": "a"}}}'),
                 ({"docsOnly": False}, '"queue": {"unread": false}'),
                 ({"docsOnly": False, "carryOver": {"records/": "why"}},
                  "cannot say both that every batch gates and that records/ carries over: give "
                  '"queue": {"unread": false} to carry nothing over, or {"unread": {"paths": {"records/": "why"}, '
                  '"defaults": false}} to let those paths also skip the gate'),
                 ({"docsOnly": True}, "move each path into queue.unread.paths"),
                 ({"carryOver": ["records/"]}, "move each path into queue.unread.paths"),
                 ({"unread": {}, "carryOver": {"records/": "why"}}, "gives queue.carryOver, which queue.unread replaced")]
        for queue, said in cases:
            with self.subTest(queue=queue):
                with self.assertRaises(ValueError) as refused: self.parsed(queue)
                self.assertIn(said, str(refused.exception))

    def test_the_lanes_a_batch_of_unread_paths_runs(self):
        config = {"lanes": self.LANES, "queue": {"unread": {"lanes": ["notes"]}}}
        self.assertEqual(mq.docs_lanes(config, ["doc/a.md"]), ["notes"])
        self.assertIsNone(mq.docs_lanes(config, ["doc/a.md", "src/a.py"]))
        self.assertIsNone(mq.docs_lanes({"queue": {"unread": False}}, ["doc/a.md"]))

    def test_a_path_names_the_few_lanes_that_read_it(self):
        """vocaro/queuekeeper#27: a batch of plain unread paths runs the docs lanes, one that changes a path naming
        lanes runs those too, and one beside a path a code lane reads gates. A path naming lanes never carries over,
        since those lanes read it."""
        laned = {"why": "the privacy check reads it", "lanes": ["notes"]}
        config = {"lanes": self.LANES, "queue": {"unread": {"paths": {"doc/release/": laned, "records/": "why"},
                                                            "exclude": {"doc/release/input.md": "a test reads it"}}}}
        self.assertEqual(mq.docs_lanes(config, ["doc/a.md", "records/a.json"]), ["docs"])
        self.assertEqual(mq.docs_lanes(config, ["doc/a.md", "doc/release/b.md"]), ["docs", "notes"])
        self.assertIsNone(mq.docs_lanes(config, ["doc/release/b.md", "src/a.py"]))
        self.assertIsNone(mq.docs_lanes(config, ["doc/release/input.md"]))
        self.assertEqual(configuration.unread_named(config, ["doc/a.md", "doc/release/b.md"]),
                         {"notes": {"doc/release/": "the privacy check reads it"}})
        self.assertTrue(mq.carried_over("records/a.json", config, set()))
        self.assertFalse(mq.carried_over("doc/release/b.md", config, set()))
        self.assertIn("rides the code gate", mq.rides(config, ["doc/release/b.md", "src/a.py"]))
        self.assertEqual(mq.rides(config, ["doc/release/b.md"]),
                         "is documentation only: lands on the docs and notes lanes, not the code gate, "
                         "main's queue.unread.paths naming notes for doc/release/ (the privacy check reads it)")
        self.assertEqual(configuration.landing_lanes(self.LANES, config["queue"])["notes"],
                         "a docs-only batch that changes doc/release/ runs it, by queue.unread.paths")
        self.assertEqual(self.parsed(config["queue"])["queue"]["unread"], config["queue"]["unread"])
        for bad in ({"why": "w"}, {"why": "", "lanes": ["notes"]}, {"why": "w", "lanes": ["gate"]},
                    {"why": "w", "lanes": ["absent"]}, {"why": "w", "lanes": []}, {"why": "w", "lanes": ["notes"], "x": 1}):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "queue.unread"):
                self.parsed({"unread": {"paths": {"doc/release/": bad}}})
        with self.assertRaisesRegex(ValueError, "queue.unread"):
            self.parsed({"unread": {"exclude": {"doc/x.md": laned}}})

    def test_the_configuration_is_checked(self):
        for declared in (False, {}, {"paths": {"records/": "why"}}, {"lanes": ["notes"]}, {"defaults": True},
                         {"paths": {"doc/": "why"}, "exclude": {"doc/x.md": "why"}, "defaults": False}):
            with self.subTest(declared=declared):
                self.assertEqual(self.parsed({"unread": declared})["queue"]["unread"], declared)
        for bad in (True, [], {"other": 1}, {"paths": {"doc/": ""}}, {"paths": ["doc/"]}, {"exclude": {" ": "why"}},
                    {"lanes": []}, {"lanes": ["gate"]}, {"lanes": ["absent"]}, {"lanes": ["notes", "notes"]},
                    {"defaults": "no"}, {"defaults": False}):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "queue.unread"):
                self.parsed({"unread": bad})

    def test_only_main_gives_it(self):
        self.assertIn("unread", mq.MAIN_ONLY)
        batch = mq.as_batch({"queue": {"unread": {"paths": {"src/": "a branch's claim"}}}})
        self.assertEqual(batch["queue"], {})

    def test_doctor_warns_of_a_carry_over_check_that_can_never_run(self):
        """A queue.carryOverChecks entry limited to paths queue.unread.paths names lanes for never runs: such a path
        never carries over, so main gaining it gates again, and a batch of it runs those lanes. PDFReflowLib dropped
        two such entries by hand (vocaro/queuekeeper#189); qk doctor now names them."""
        laned = {"why": "the measurement check reads it", "lanes": ["notes"]}
        check = {"command": ["python3", "check.py", "--base", "{base}"], "paths": ["measurements/"]}
        config = {"lanes": self.LANES, "queue": {"unread": {"paths": {"measurements/": laned, "records/": "why"}},
                                                 "carryOverChecks": [check]}}
        [warned] = configuration.unrun_carry_checks(config)
        self.assertIn("queue.carryOverChecks runs python3 check.py --base '{base}' only on a move of measurements/",
                      warned)
        self.assertEqual(mq.carry_checks(config, [path for path in ("measurements/a.json", "records/a.json")
                                                  if mq.carried_over(path, config, set())], "base"), [])
        # A check that also runs on other moves, on a plain unread path, on none or under a path an exclude keeps for
        # the code gate, can run.
        for paths in (["measurements/", "records/"], ["records/"], None, ["measurements/x/"]):
            with self.subTest(paths=paths):
                other = {"command": ["true"], **({"paths": paths} if paths else {})}
                queue = {**config["queue"], "carryOverChecks": [other]}
                if paths == ["measurements/x/"]:
                    queue["unread"] = {**queue["unread"], "exclude": {"measurements/x/input.json": "a test reads it"}}
                self.assertEqual(configuration.unrun_carry_checks({**config, "queue": queue}), [])
        self.assertEqual(configuration.unrun_carry_checks({"queue": {"unread": False, "carryOverChecks": [check]}}), [])
        self.assertEqual(self.parsed(config["queue"])["queue"]["carryOverChecks"], [check])

    def test_the_schema_knows_the_setting(self):
        schema = json.loads((ROOT / "schemas/queuekeeper.schema.json").read_text())
        queue = schema["properties"]["queue"]["properties"]
        self.assertEqual(set(queue["unread"]["properties"]), {"paths", "exclude", "lanes", "defaults"})
        self.assertEqual(queue["unread"]["properties"]["lanes"]["items"]["not"], {"const": "gate"})
        laned = queue["unread"]["properties"]["paths"]["additionalProperties"]["oneOf"][1]
        self.assertEqual((set(laned["properties"]), laned["properties"]["lanes"]["items"]["not"]),
                         ({"why", "lanes"}, {"const": "gate"}))
        self.assertFalse({"docsOnly", "carryOver"} & set(queue))


class DefaultDocsOnly(Fixture):
    def test_conventional_markdown_uses_implicit_docs_and_skips_code_and_hook(self):
        self.ran = self.base / "hook-runs"
        PrePushHooks.hook(self, "exit 1")
        self.commit("doc/guide.md", "guide", "# Guide\n")
        [identifier] = self.add("guide")
        self.assertEqual(self.work(), 0)
        state = self.state(identifier)
        self.assertEqual(state["state"], "landed")
        self.assertEqual(state["docsOnly"], ["docs"])
        self.assertEqual(self.gates(), 0)
        self.assertNotIn("gatedOn", state)
        self.assertEqual(PrePushHooks.runs(self), [])
        logs = list(mq.Queue(self.repo).logs.glob("*-docs.log"))
        self.assertTrue(logs)
        self.assertIn("Documentation checks passed", logs[-1].read_text())

    def test_a_docs_entry_that_fails_ahead_of_older_code_waits_for_it_and_then_lands(self):
        """A document that links to a file an older code entry adds fails its docs lanes ahead of it: it goes back in
        line behind that entry, rather than being sent back, and then lands on the docs lanes on top of it."""
        self.commit("tool.py", "tool", "print('a tool')\n")
        self.commit("docs/guide.md", "guide", "[the tool](../tool.py)\n")
        tool, guide = self.add("tool", "guide")
        self.work()
        self.assertEqual([self.state(i)["state"] for i in (tool, guide)], ["landed"] * 2)
        self.assertEqual(self.state(guide)["waitsFor"], [tool])
        self.assertEqual(self.state(guide)["docsOnly"], ["docs"])
        self.assertEqual(self.gates(), 1)

    def test_a_docs_only_batch_does_not_wait_behind_a_whole_host_lane(self):
        """vocaro-engineering#394: a docs-only batch's preflight lane and the toolkit's docs check run unadmitted under
        DOCS_CHECK_LIMIT, so a whole-host lane holding this Mac does not hold them back. The old worker ran them through
        qk run's admission, which waited until the holder let go: here, after PATIENCE, which fails the test."""
        from queuekeeper import resources
        slots = self.base / "slots"
        environment = {"HEAVY_SLOT_DIR": str(slots), "HEAVY_SLOTS": "2", "HEAVY_SLOTS_MAX": "3",
                       "HEAVY_SETTLE_SECONDS": "0"}
        held, release, released_early = threading.Event(), threading.Event(), []
        def hold():
            with contextlib.redirect_stderr(io.StringIO()), resources.Admission("gate", whole_host=True, timeout=0):
                held.set()
                if not release.wait(stretched(60)): released_early.append(True)
        self.preflight_check()
        self.commit("doc/guide.md", "guide", "# Guide\n")
        [identifier] = self.add("guide")
        with patch.dict(os.environ, environment):
            holder = threading.Thread(target=hold, name="whole-host lane", daemon=True)
            holder.start()
            self.assertTrue(held.wait(stretched(30)))
            os.environ.pop(resources.TOKEN, None)  # the worker is no work nested in the holder's admission
            said = io.StringIO()
            try:
                with contextlib.redirect_stdout(said): self.assertEqual(self.work(), 0)
            finally:
                release.set()
                holder.join()
        self.assertEqual(released_early, [], said.getvalue())
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(self.state(identifier)["docsOnly"], ["check", "docs"])
        for lane in ("check", "docs"):
            self.assertIn(f"{lane} exited 0 in the queue's checkout, unadmitted, as a check that takes seconds",
                          said.getvalue())

    def test_a_docs_check_that_reaches_its_limit_runs_admitted(self):
        """The control: a check that does not end within DOCS_CHECK_LIMIT runs again through qk run, admitted."""
        self.commit("doc/guide.md", "guide", "# Guide\n")
        [identifier] = self.add("guide")
        real = mq.processes.run
        def slow(command, **options):
            if command == mq.IMPLICIT_DOCS: raise mq.processes.TimedOut("the fixture's slow check")
            return real(command, **options)
        said = io.StringIO()
        with patch.object(mq.processes, "run", slow), contextlib.redirect_stdout(said): self.work()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertIn(f"docs: did not end unadmitted within {mq.DOCS_CHECK_LIMIT}s", said.getvalue())
        self.assertIn("docs exited 0 on this Mac", said.getvalue())

    def test_a_code_batch_checks_its_preflight_lane_and_docs_while_every_slot_is_held(self):
        """vocaro/queuekeeper#169: a batch that changes code runs its preflight lane and docs check unadmitted too, so they
        end while a whole-host lane holds this Mac, and only its gate waits. The old worker ran them through qk run's
        admission: on 2026-11-15 a round waited 34 min 33 s to check spelling. Here the holder lets go only once both
        have ended, or after PATIENCE, which fails the test."""
        from queuekeeper import resources
        environment = {"HEAVY_SLOT_DIR": str(self.base / "slots"), "HEAVY_SLOTS": "2", "HEAVY_SLOTS_MAX": "3",
                       "HEAVY_SETTLE_SECONDS": "0"}
        ended = "{} exited 0 in the queue's checkout, unadmitted, as a check that takes seconds"
        held, release, said, lock = threading.Event(), threading.Event(), [], threading.Lock()
        checked = threading.Event()
        def hold():
            with contextlib.redirect_stderr(io.StringIO()), resources.Admission("gate", whole_host=True, timeout=0):
                held.set()
                release.wait(stretched(120))
        def say(_, words):
            with lock: said.append(words)
            # Before the gate asks for admission, so the holder is still there when both checks have ended.
            if words == ended.format("docs"):
                checked.set()
                release.set()
        self.preflight_check()
        self.commit("tool.py", "tool", "print('a tool')\n")
        run(self.repo, "git", "checkout", "-q", "tool")
        (self.repo / "doc").mkdir(exist_ok=True)
        (self.repo / "doc/guide.md").write_text("# Guide\n")
        run(self.repo, "git", "add", "doc/guide.md")
        run(self.repo, "git", "commit", "-qm", "Add a guide beside the tool")
        [identifier] = self.add("tool")
        with patch.dict(os.environ, environment):
            holder = threading.Thread(target=hold, name="whole-host lane", daemon=True)
            holder.start()
            self.assertTrue(held.wait(stretched(30)))
            os.environ.pop(resources.TOKEN, None)  # the worker is no work nested in the holder's admission
            watchdog = threading.Timer(stretched(60), release.set)  # the old worker: let its preflight lane go
            watchdog.start()
            try:
                with patch.object(mq.Worker, "say", say), patch.object(mq.Worker, "stack"), \
                        patch.object(mq.Worker, "docs_beside"):
                    self.assertEqual(self.work(), 0)
            finally:
                watchdog.cancel()
                release.set()
                holder.join()
        self.assertTrue(checked.is_set(), said)
        self.assertEqual(said.index(ended.format("check")) + 2, said.index(ended.format("docs")), said)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertNotIn("docsOnly", self.state(identifier))
        self.assertEqual(self.gates(), 1)
        # Only the gate went through qk run, which says where it ran.
        self.assertEqual([words.split()[0] for words in said if " exited 0 on this Mac" in words], ["gate"], said)

    def test_a_code_batchs_check_that_reaches_its_limit_runs_admitted(self):
        """The control: a code batch's check that does not end within DOCS_CHECK_LIMIT runs again through qk run."""
        self.preflight_check()
        self.commit("tool.py", "tool", "print('a tool')\n")
        [identifier] = self.add("tool")
        real, started = mq.processes.run, []
        def slow(command, **options):
            started.append(list(command))
            if command == ["python3", "check.py"]:
                self.assertEqual(options["timeout"], mq.DOCS_CHECK_LIMIT)
                raise mq.processes.TimedOut("the fixture's slow check")
            return real(command, **options)
        said = io.StringIO()
        with patch.object(mq.processes, "run", slow), contextlib.redirect_stdout(said): self.work()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(started[:3], [["python3", "check.py"], ["python3", "scripts/qk", "run", "check"],
                                       ["python3", "scripts/qk", "run", "gate"]])
        self.assertIn(f"check: did not end unadmitted within {mq.DOCS_CHECK_LIMIT}s", said.getvalue())
        self.assertIn("check exited 0 on this Mac", said.getvalue())
        self.assertEqual(self.gates(), 1)

    def test_the_gate_lane_is_never_a_code_batchs_unadmitted_check(self):
        """A lane main names as its gate takes admission, though it is a preflight lane or the docs check too."""
        config = self.config()
        self.assertEqual(mq.quick_lanes(config, code=True), {"docs"})
        config["lanes"]["lint"], config["queue"]["preflight"] = {"command": ["true"]}, ["lint"]
        self.assertEqual(mq.quick_lanes(config, code=True), {"lint", "docs"})
        config["queue"]["lanes"] = ["docs", "gate"]
        self.assertEqual(mq.quick_lanes(config, code=True), {"lint"})
        self.assertEqual(mq.quick_lanes(config), {"lint", "docs"})
        config["queue"]["preflight"], config["queue"]["lanes"] = ["gate"], None
        self.assertEqual(mq.quick_lanes(config, code=True), {"docs"})

    def test_a_removed_entry_stops_an_unadmitted_check_which_says_nothing_of_its_limit(self):
        worker = mq.Worker(mq.Queue(self.repo))
        worker.checkout = self.repo
        cancel, said = threading.Event(), []
        cancel.set()
        with patch.object(mq.Worker, "say", lambda _, words: said.append(words)), \
                patch.object(mq.Worker, "implicit", return_value=[sys.executable, "-c", "import time; time.sleep(600)"]):
            self.assertIsNone(worker.quick("docs", self.base / "docs.log", cancel))
        self.assertEqual(said, [])

    def test_a_project_docs_lane_of_its_own_is_admitted(self):
        """A docs lane whose command is not the toolkit's has a cost queuekeeper cannot know, so it runs admitted."""
        config = self.config()
        config["lanes"]["docs"] = {"command": ["python3", "scripts/qk", "docs", "check", "--strict"]}
        worker = mq.Worker(mq.Queue(self.repo))
        worker.checkout = self.base / "checkout"
        worker.checkout.mkdir()
        (worker.checkout / "queuekeeper.json").write_text(json.dumps(config))
        with patch.object(mq.processes, "run") as run_:
            self.assertIsNone(worker.quick("docs", self.base / "docs.log"))
            self.assertIsNone(worker.quick("gate", self.base / "gate.log"))
        run_.assert_not_called()

    def test_broken_links_in_the_default_docs_folder_are_rejected(self):
        self.commit("docs/guide.md", "guide", "[missing](missing.md)\n")
        [identifier] = self.add("guide")
        self.work()
        self.assertEqual(self.state(identifier)["state"], "failed")
        self.assertEqual(self.gates(), 0)
        self.assertNotIn("docs", self.main_files())

    def test_a_preflight_finding_in_default_docs_is_rejected(self):
        # A docs-only batch runs the project's preflight lane before its docs lanes, never the gate.
        self.preflight_check()
        self.commit("README.md", "guide", FLAGGED)
        [identifier] = self.add("guide")
        self.work()
        self.assertEqual(self.state(identifier)["state"], "failed")
        self.assertEqual(self.gates(), 0)
        self.assertNotIn("README.md", self.main_files())

    def test_false_disables_defaults_and_keeps_code_gate_and_hook(self):
        config = self.config()
        config["queue"]["unread"] = False
        self.publish(config)
        self.ran = self.base / "hook-runs"
        PrePushHooks.hook(self, "exit 0")
        self.commit("README.md", "guide", "# Guide\n")
        [identifier] = self.add("guide")
        self.work()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertNotIn("docsOnly", self.state(identifier))
        self.assertEqual(self.gates(), 1)
        self.assertEqual(len(PrePushHooks.runs(self)), 1)

    def test_an_explicit_excluded_document_keeps_the_code_gate(self):
        config = self.config()
        config["queue"]["unread"] = {"paths": {"guides/": "prose"}, "defaults": False,
                                      "exclude": {"guides/input.md": "a test reads this fixture"}}
        self.publish(config)
        self.commit("guides/input.md", "input", "# Input\n")
        [identifier] = self.add("input")
        self.work()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertNotIn("docsOnly", self.state(identifier))
        self.assertEqual(self.gates(), 1)

    def test_default_classification_keeps_code_configuration_and_fixtures_gated(self):
        config = self.config()
        for path in ("README.md", "CONTRIBUTING.md", "CHANGELOG.md", "doc/guide.md", "docs/guide.md",
                     "doc/apple/topic.md", "docs/topic.markdown"):
            with self.subTest(path=path):
                self.assertEqual(mq.docs_lanes(config, [path]), ["docs"])
                self.assertIsNone(mq.docs_lanes(config, [path, "src/code.py"]))
        # guides/ and knowledge/ are no default: a project that keeps documents there names them in
        # queue.unread.paths, which adds to the defaults (vocaro/queuekeeper#106).
        for path in ("queuekeeper.json", "doc/config.json", "docs/code.py", "tests/input.md", "src/input.md",
                     "guides/guide.md", "guides/topic.markdown", "knowledge/apple/topic.md",
                     "AGENTS.md", "agents/core.md", ".github/workflows/check.yml", "README.md.orig"):
            with self.subTest(path=path): self.assertIsNone(mq.docs_lanes(config, [path]))
        # One reading answers both questions, so main changing such a guide gates a passed batch again, where a
        # default document carries over; a project that names the folder gets both back, beside the defaults.
        self.assertTrue(mq.carried_over("doc/guide.md", config, set()))
        named = {**config, "queue": {**config.get("queue", {}), "unread": {"paths": {"guides/": "prose"}}}}
        for path in ("guides/guide.md", "knowledge/apple/topic.md"):
            with self.subTest(carried=path): self.assertFalse(mq.carried_over(path, config, set()))
        self.assertEqual(mq.docs_lanes(named, ["guides/guide.md", "doc/guide.md"]), ["docs"])
        self.assertTrue(mq.carried_over("guides/guide.md", named, set()))
        self.assertIsNone(mq.docs_lanes(named, ["knowledge/apple/topic.md"]))
        self.assertIsNone(mq.docs_lanes(config, []))
        self.assertEqual(configuration.lane_to_run(config, "docs", self.repo)["command"], configuration.IMPLICIT_DOCS)

    def test_queuekeepers_own_guides_are_unread_by_the_default(self):
        """Queuekeeper keeps its guides under doc/, so the default takes them, and only their Markdown, with no
        older queue.docsOnly or queue.carryOver; nothing is left at guides/, which is no default folder (vocaro/queuekeeper#106).
        Its queue.unread keeps the defaults and adds the README assets and screenshot script (vocaro/queuekeeper#127).
        Paths a test reads name the lanes of those tests, which a docs-only batch changing them runs in place of the
        code gate and never carries over."""
        own = configuration.load(ROOT)
        self.assertEqual(configuration.DOCS_FOLDERS, ("doc/", "docs/"))
        self.assertFalse({"docsOnly", "carryOver"} & set(own.get("queue", {})))
        self.assertEqual(set(own["queue"]["unread"]), {"paths"})
        checked = {
            "doc/user_guide.md": ["setup-examples", "plugin-lists"],
            "doc/developer_guide.md": ["plugin-lists"],
            "README.md": ["setup-examples", "plugin-lists"],
            "assets/overview.svg": ["plugin-lists"],
            "tools/dashboard-screenshots.py": ["dashboard-screenshots"],
        }
        self.assertEqual(set(own["queue"]["unread"]["paths"]), {"assets/", *checked})
        for path, lanes in checked.items():
            with self.subTest(path=path):
                self.assertEqual(mq.docs_lanes(own, [path]), ["docs", *lanes])
                self.assertFalse(mq.carried_over(path, own, set()))
        self.assertEqual(run(ROOT, "git", "ls-files", "--", "guides"), "")
        for path in ("doc/related_tools.md", "assets/dashboard-farm.png"):
            with self.subTest(path=path):
                self.assertTrue((ROOT / path).is_file())
                self.assertEqual(mq.docs_lanes(own, [path]), ["docs"])
                self.assertIsNone(mq.docs_lanes(own, [path, "src/python/queuekeeper/config.py"]))
                self.assertTrue(mq.carried_over(path, own, set()))
        old = "guides" + "/maintenance.md"   # the guide's path before the move, which no default takes
        for path in ("AGENTS.md", "queuekeeper.json", "tests/core/test_docs.py", old, "doc/diagram.png"):
            with self.subTest(path=path):
                self.assertIsNone(mq.docs_lanes(own, [path]))
                self.assertFalse(mq.carried_over(path, own, set()))

    def test_a_default_docs_file_moved_into_source_keeps_code_gate(self):
        self.commit("README.md", "guide", "# Guide\n")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        run(self.repo, "git", "checkout", "-q", "-B", "move", "origin/main")
        (self.repo / "src").mkdir()
        run(self.repo, "git", "mv", "README.md", "src/input.md")
        run(self.repo, "git", "commit", "-qm", "Move documentation to source")
        [identifier] = self.add("move")
        self.work()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertNotIn("docsOnly", self.state(identifier))
        self.assertEqual(self.gates(), 1)


class PreflightLaneInRounds(Fixture):
    """A project's own preflight lane, listed in queue.preflight, which the queue runs first in a round: unadmitted and,
    when it does not end in time, through qk run, as any lane (vocaro/queuekeeper#169). A project that lists none has
    none, a lane named spelling among them (vocaro/queuekeeper#69)."""
    CHECK = ["python3", "check.py"]
    RUN = ["python3", "scripts/qk", "run", "check"]

    def setUp(self):
        super().setUp()
        self.preflight_check()

    def rounds(self):
        said = []
        with patch.object(mq.Worker, "say", lambda _, words: said.append(words)): self.work()
        return said

    @contextlib.contextmanager
    def lanes_run(self, check=None):
        """The commands the worker starts, in a list, each run as it would be, or, for the preflight lane through
        qk run, exiting check when that is given."""
        started, real = [], mq.processes.run
        def recorded(command, **options):
            started.append(list(command))
            return check if check is not None and list(command) == self.RUN else real(command, **options)
        with patch.object(mq.processes, "run", recorded): yield started

    def test_a_code_batch_runs_its_preflight_lane_unadmitted_and_the_gate_through_qk_run(self):
        self.commit("fix", "fix", content="a clean point\n")
        [identifier] = self.add("fix")
        with self.lanes_run() as started: said = self.rounds()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(started[:2], [self.CHECK, ["python3", "scripts/qk", "run", "gate"]])
        self.assertNotIn(self.RUN, started)
        self.assertIn("check exited 0 in the queue's checkout, unadmitted, as a check that takes seconds", said)

    def test_a_check_that_does_not_end_in_time_is_a_lane_qk_run_admits_and_places(self):
        self.commit("fix", "fix", content="a clean point\n")
        [identifier] = self.add("fix")
        with patch.object(mq.Worker, "quick", return_value=None), self.lanes_run() as started: said = self.rounds()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(started[:2], [self.RUN, ["python3", "scripts/qk", "run", "gate"]])
        self.assertNotIn(self.CHECK, started)
        # The project's own lane, so the round names no toolkit check in its place.
        self.assertFalse(any("runs the toolkit's" in words for words in said), said)
        # The round says where it ran, as it does of a lane, from the receipt qk run wrote once it was admitted.
        self.assertTrue(any(words.startswith("check exited 0 on this Mac (") for words in said), said)

    def test_an_ungated_landing_runs_it_and_not_the_gate(self):
        self.commit("fix", "fix", content="a clean point\n")
        with patch.object(mq.Queue, "start"), patch("builtins.print"):
            mq.add(self.repo, "fix", no_gate=True, why="an emergency")
        with self.lanes_run() as started: self.rounds()
        self.assertIn("fix", self.main_files())
        self.assertIn(self.CHECK, started)
        self.assertNotIn(self.RUN, started)
        self.assertEqual(self.gates(), 0)

    def test_a_docs_only_batch_runs_it_before_its_own_lanes(self):
        config = self.config()
        config["lanes"]["docs"] = {"command": ["python3", "-c", "pass"]}
        config["queue"]["unread"] = {"paths": {"doc/": "no code lane reads it"}, "defaults": False}
        self.publish(config, "Declare the docs-only lanes")
        self.commit("doc/a.md", "fix", content="a clean point\n")
        [identifier] = self.add("fix")
        with self.lanes_run() as started: self.rounds()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(self.state(identifier)["docsOnly"], ["check", "docs"])
        # The preflight lane runs unadmitted in a docs-only batch, and the project's own docs lane through qk run
        # (vocaro-engineering#394).
        self.assertEqual(started[:2], [self.CHECK, ["python3", "scripts/qk", "run", "docs"]])
        self.assertEqual(self.gates(), 0)

    def test_a_lane_named_spelling_that_queue_preflight_does_not_list_runs_only_where_the_gate_names_it(self):
        """vocaro/queuekeeper#69: a lane's name says nothing of when it runs. A project with no queue.preflight and a
        lane named spelling, which the queue once ran first by its name, runs only its gate."""
        own, own_status = self.base / "own-spelling", self.base / "own-status"
        config = self.config()
        config["lanes"]["spelling"] = {"command": ["python3", "lane.py", str(own), str(own_status)]}
        del config["queue"]["preflight"]
        self.publish(config, "A spelling lane the queue does not list")
        self.assertEqual(mq.lanes_of(self.config()), ["gate"])
        self.assertEqual(mq.ungated_lanes(self.config()), [])
        self.commit("fix", "fix", content=FLAGGED)
        [identifier] = self.add("fix", checked=False)
        with self.lanes_run() as started: said = self.rounds()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertIn(["python3", "scripts/qk", "run", "gate"], started)
        self.assertFalse([command for command in started if command[-1] in ("spelling", "check") or command == self.CHECK],
                         started)
        self.assertFalse(own.exists())
        self.assertEqual(self.gates(), 1)
        self.assertFalse(any(words.startswith(("spelling", "check")) for words in said), said)

    def test_no_admission_or_no_measurement_is_no_verdict_and_sends_back_at_the_limit(self):
        for status in (mq.resources.BUSY, mq.lane_results.UNMEASURED, mq.remote.MISCONFIGURED):
            with self.subTest(status=status):
                self.commit(f"fix{status}", f"fix{status}", content=FLAGGED)
                [identifier] = self.add(f"fix{status}", checked=False)
                # The admitted run, which alone can say so: the check did not end unadmitted in time.
                with patch.object(mq.Worker, "quick", return_value=None), self.lanes_run(check=status) as started, \
                        patch("builtins.print"): self.rounds()
                self.assertEqual(self.state(identifier)["state"], "failed")
                self.assertEqual(self.gates(), 0)
                self.assertNotIn(f"fix{status}", self.main_files())
                self.assertEqual(started.count(self.RUN), mq.NO_VERDICT_RUNS)
                self.assertIn(f"the lanes gave no verdict in {mq.NO_VERDICT_RUNS} runs in a row, the last with exit "
                              f"{status}", self.state(identifier)["why"])
                mq.remove(mq.Queue(self.repo), identifier, quiet=True)


    def test_a_clean_batch_passes_the_preflight_lane_and_then_the_gate(self):
        self.commit("fix", "fix", content="a clean point\n")
        [identifier] = self.add("fix")
        said = self.rounds()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(self.gates(), 1)
        self.assertIn("check exited 0 in the queue's checkout, unadmitted, as a check that takes seconds", said)
        logs = sorted(path.name for path in mq.Queue(self.repo).logs.glob("*.log"))
        self.assertTrue(any(name.endswith("-check.log") for name in logs), logs)

    def test_a_finding_fails_before_the_gate(self):
        self.commit("fix", "fix", content=FLAGGED)
        [identifier] = self.add("fix", checked=False)
        with self.lanes_run() as started: self.rounds()
        self.assertEqual(self.state(identifier)["state"], "failed")
        self.assertEqual(self.gates(), 0)
        self.assertNotIn("fix", self.main_files())
        self.assertNotIn(self.RUN, started)
        # The check ran once more, on origin/main alone, which passes it: the entry is what fails (#180).
        self.assertEqual(started.count(self.CHECK), 2)
        self.assertRegex(self.state(identifier)["why"], r"\. origin/main at \w{12} passes check without this entry\.$")

    def test_a_failure_origin_main_has_too_is_named_in_the_entrys(self):
        """vocaro/queuekeeper#180: main took a finding past the queue, so a clean entry fails the preflight lane on top
        of it, and can never pass alone. Its failure says that origin/main fails the lane too, with the same finding,
        so that its session looks for main's fix instead of adding it again."""
        run(self.repo, "git", "checkout", "-q", "main")
        run(self.repo, "git", "pull", "-q", "--ff-only", "origin", "main")
        (self.repo / "old").write_text(FLAGGED)
        run(self.repo, "git", "add", "old")
        run(self.repo, "git", "commit", "-qm", "Past the queue")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        self.commit("other", "other", content="a clean point\n")
        [identifier] = self.add("other", checked=False)
        said = self.rounds()
        entry = self.state(identifier)
        self.assertEqual(entry["state"], "failed")
        self.assertEqual(self.gates(), 0)
        self.assertRegex(entry["why"], r"is unknown\. origin/main at \w{12} fails check too, without this entry, "
                                       r"with the same 1 finding: main may need a fix first")
        main_log = entry["why"].rsplit("; see ", 1)[1].rstrip(".")
        self.assertTrue(main_log.endswith("-check-origin-main.log"), main_log)
        self.assertIn("old:1:", Path(main_log).read_text())
        self.assertTrue(any(words.startswith("check exited 1 on origin/main at") for words in said), said)

    def unlaunched(self):
        """Take the launcher out of main, as a project that followed getting started has none, and return how the
        queue's toolkit is run in its place (vocaro/queuekeeper#108)."""
        run(self.repo, "git", "rm", "-q", "scripts/qk")
        run(self.repo, "git", "commit", "-qm", "Drop the launcher")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        return [sys.executable, str(Path(os.environ["QUEUEKEEPER_ROOT"]).resolve() / "bin/qk")]

    def test_a_project_with_no_launcher_runs_its_gate_with_the_queues_toolkit(self):
        qk = self.unlaunched()
        self.commit("fix", "fix", content="a clean point\n")
        [identifier] = self.add("fix")
        with self.lanes_run() as started: said = self.rounds()
        self.assertEqual(self.state(identifier)["state"], "landed")
        # The preflight lane runs unadmitted as its own command, and the gate still goes through qk run, admitted and
        # placed, by the toolkit's own qk.
        self.assertEqual(started[0], self.CHECK)
        self.assertEqual((started[1][:2], started[1][2], started[1][-2:]), (qk, "--project", ["run", "gate"]))
        self.assertIn("check exited 0 in the queue's checkout, unadmitted, as a check that takes seconds", said)

    def test_with_no_launcher_a_docs_only_batch_runs_its_checks(self):
        self.unlaunched()
        self.commit("doc/guide.md", "guide", "# Guide\n")
        [identifier] = self.add("guide")
        said = self.rounds()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(self.state(identifier)["docsOnly"], ["check", "docs"])
        for lane in ("check", "docs"):
            self.assertIn(f"{lane} exited 0 in the queue's checkout, unadmitted, as a check that takes seconds", said)


class NegatedClaims(unittest.TestCase):
    """What landing_proofs.negated reads as a closing claim its own clause denies (vocaro-engineering#286)."""
    def test_each_form_of_negation_is_refused(self):
        for message in ("This does not fix #43.", "This doesn't fix #43.", "This doesn’t fix #43.",
                        "This cannot close #43.", "This will never resolve #43.", "This no longer fixes #43.",
                        "Landed without the fix #43.", "NOT fixed #43."):
            with self.subTest(message):
                self.assertEqual(landing_proofs.negated(message), [(1, message, "#43")])

    def test_a_few_words_may_stand_between_and_more_may_not(self):
        for message in ("It does not yet fix #43.", "It does not by itself fix #43.", "It does not on its own fix #43."):
            with self.subTest(message):
                self.assertEqual(len(landing_proofs.negated(message)), 1)
        self.assertEqual(landing_proofs.negated("It does not on any of its paths fix #43."), [])

    def test_a_plain_claim_passes(self):
        for message in ("Closes #43.", "Fixes #43", "resolved: #43", "Add a thing\n\nCloses #43.\nCloses #44."):
            with self.subTest(message):
                self.assertEqual(landing_proofs.negated(message), [])

    def test_other_repositories_and_urls_are_named_in_full(self):
        for message, issue in (("This does not fix vocaro/BreezEPUB#43.", "vocaro/BreezEPUB#43"),
                               ("This does not close https://github.com/vocaro/BreezEPUB/issues/43.",
                                "vocaro/BreezEPUB#43")):
            with self.subTest(message):
                self.assertEqual(landing_proofs.negated(message), [(1, message, issue)])
        self.assertEqual(landing_proofs.negated("Closes vocaro/BreezEPUB#43."), [])
        self.assertEqual(landing_proofs.negated("Fixes https://github.com/vocaro/BreezEPUB/issues/43"), [])

    def test_what_is_no_claim_passes(self):
        for message in ("Refs #43.", "This does not change #43.", "This leaves #43 open.", "Does not touch fixes in #43.",
                        "Finished without fixing #43.", "Not ready: see #43.", "Refs vocaro/BreezEPUB#43, not fixed."):
            with self.subTest(message):
                self.assertEqual(landing_proofs.negated(message), [])

    def test_a_negation_in_an_earlier_clause_passes(self):
        for message in ("The lane did not start, and fixes #43.", "The lane did not start and fixes #43.",
                        "Do not retry; closes #43.", "Do not retry: closes #43.", "Do not retry. Closes #43.",
                        "Do not retry — closes #43.", "Do not retry - closes #43.",
                        "Stop if it cannot run because that fixes #43.", "This is not slow but fixes #43.",
                        "The run (that did not start) fixes #43."):
            with self.subTest(message):
                self.assertEqual(landing_proofs.negated(message), [])

    def test_a_claim_on_its_own_line_passes_after_a_line_that_negates(self):
        for message in ("Do not count a run\n\nCloses #43.", "It does not change the parser\nCloses #43.",
                        "Notes:\n- does not retry\n- fixes #43"):
            with self.subTest(message):
                self.assertEqual(landing_proofs.negated(message), [])

    def test_a_negation_wrapped_onto_the_line_before_is_refused(self):
        self.assertEqual(landing_proofs.negated("Add a thing\n\nThis does not\nfix #43, and adds more."),
                         [(4, "fix #43, and adds more.", "#43")])

    def test_not_only_asserts_the_claim(self):
        for message in ("This not only fixes #43 but adds a test.", "It does not just fix #43."):
            with self.subTest(message):
                self.assertEqual(landing_proofs.negated(message), [])

    def test_each_claim_is_judged_alone(self):
        self.assertEqual([claim[2] for claim in landing_proofs.negated("Fixes #1 and does not close #2.")], ["#2"])
        self.assertEqual([claim[2] for claim in landing_proofs.negated("Does not fix #1 or close #2.")], ["#1", "#2"])
        self.assertEqual(landing_proofs.negated("Closes #1, not #2."), [])

    def test_a_version_number_ends_no_clause(self):
        self.assertEqual(len(landing_proofs.negated("This does not in v1.2 fix #43.")), 1)


class ClaimsBeforeQueued(Fixture):
    """qk land and `qk queue add` refuse a commit message that denies a closing claim before anything is queued
    (vocaro-engineering#286)."""
    def worded(self, branch, *messages):
        """A branch of one commit for each message, on origin/main."""
        for index, message in enumerate(messages):
            self.commit(f"{branch}-{index}", branch, start="origin/main" if index == 0 else branch)
            run(self.repo, "git", "commit", "-q", "--amend", "-m", message)
        return run(self.repo, "git", "rev-parse", branch)

    def land(self, branch, **options):
        """(the new entry's ID or None, what qk land said, the error it refused with or None), no worker started."""
        said, error, identifier = io.StringIO(), None, None
        with patch.object(mq.Queue, "start"), contextlib.redirect_stdout(said), contextlib.redirect_stderr(said):
            try: identifier = mq.add(self.repo, branch, **options)
            except mq.Refused as refused: error = str(refused)
        return identifier, said.getvalue(), error

    def test_a_negated_claim_is_refused_naming_the_commit_and_line_and_nothing_is_queued(self):
        tip = self.worded("fix", "Add a thing\n\nSome context.\nThis does not fix #43.")
        with patch.object(mq, "check") as trial, patch.object(mq, "checked_before_running") as unittest_checks:
            identifier, _, error = self.land("fix")
        self.assertIsNone(identifier)
        self.assertEqual(mq.Queue(self.repo).all(), [])
        self.assertEqual(run(self.repo, "git", "for-each-ref", "refs/queuekeeper/queue"), "")
        trial.assert_not_called()
        unittest_checks.assert_not_called()
        self.assertIn(f"{tip[:12]} is not queued", error)
        self.assertIn(f"{tip[:12]} line 4 closes #43: This does not fix #43.", error)
        self.assertIn('"Refs #43"', error)
        self.assertIn('"leaves #43 open"', error)
        self.assertIn("Reword the commit and land it again", error)

    def test_a_plain_claim_is_queued(self):
        # The positive control.
        self.worded("fix", "Add a thing\n\nThe lane did not start, and this is the repair.\n\nCloses #43.")
        identifier, _, error = self.land("fix")
        self.assertIsNone(error)
        self.assertEqual(self.state(identifier)["state"], "queued")

    def test_any_commit_of_the_branch_is_read_and_each_finding_named(self):
        self.worded("two", "Add a thing, without fix vocaro/BreezEPUB#7.", "Add another\n\nCloses #8.",
                    "Add a third that won't close #9.")
        _, _, error = self.land("two")
        self.assertIn("line 1 closes vocaro/BreezEPUB#7:", error)
        self.assertIn("line 1 closes #9:", error)
        self.assertNotIn("closes #8", error)

    def test_a_message_main_already_holds_is_not_read(self):
        self.worded("old", "This does not fix #43.")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        self.worded("fix", "Add a thing")
        identifier, _, error = self.land("fix")
        self.assertIsNone(error)
        self.assertEqual(self.state(identifier)["state"], "queued")

    def test_the_emergency_options_do_not_skip_it(self):
        self.worded("fix", "This does not fix #43.")
        for options in ({"front": True}, {"no_gate": True, "why": "urgent"}):
            with self.subTest(options):
                identifier, _, error = self.land("fix", **options)
                self.assertIsNone(identifier)
                self.assertIn("is not queued", error)
        self.assertEqual(mq.Queue(self.repo).all(), [])

    def test_a_branch_refreshed_by_a_merge_is_read_too(self):
        old = run(self.repo, "git", "rev-parse", "origin/main")
        self.commit("other", "other")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        self.commit("mine", "fix", start=old)
        run(self.repo, "git", "commit", "-q", "--amend", "-m", "This does not fix #43.")
        run(self.repo, "git", "merge", "-q", "--no-edit", "origin/main")
        identifier, _, error = self.land("fix")
        self.assertIsNone(identifier)
        self.assertIn("closes #43: This does not fix #43.", error)

    def test_messages_it_cannot_read_are_refused(self):
        self.worded("fix", "Add a thing")
        with patch.object(mq.landing_proofs, "negated_claims", return_value=None):
            identifier, _, error = self.land("fix")
        self.assertIsNone(identifier)
        self.assertIn("commit messages could not be read", error)


class RewrittenMain(Fixture):
    """qk land and `qk queue add` refuse a branch built on origin/main's history as it was before a rewrite, as git
    filter-repo makes, which would carry that whole history and put back what the rewrite removed
    (vocaro/queuekeeper#287)."""
    def grow_main(self, *names):
        """One commit on origin/main adding each file named, in order."""
        for name in names:
            self.commit(name, "grow")
            run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        run(self.repo, "git", "fetch", "-q", "origin")

    def rewrite_main(self, since=0, drop=".mailmap"):
        """Rewrite origin/main as git filter-repo does: every commit from the since-th, oldest first, made again with
        its dates and message, the first with another author address, and drop left out of every tree, a commit that
        only added it pruned. Returns the old main."""
        run(self.repo, "git", "fetch", "-q", "origin")
        old = run(self.repo, "git", "rev-parse", "origin/main")
        commits = run(self.repo, "git", "rev-list", "--reverse", "origin/main").split()
        index = self.base / "rewrite-index"
        parent = run(self.repo, "git", "rev-parse", f"{commits[since]}^") if since else None
        for number, commit in enumerate(commits[since:]):
            fields = run(self.repo, "git", "log", "-1", "--date=raw",
                         "--format=%an%x1f%ae%x1f%ad%x1f%cn%x1f%ce%x1f%cd", commit).split("\x1f")
            message = subprocess.run(["git", "log", "-1", "--format=%B", commit], cwd=self.repo, check=True,
                                     capture_output=True, text=True).stdout
            alone = {**os.environ, "GIT_INDEX_FILE": str(index)}
            subprocess.run(["git", "read-tree", commit], cwd=self.repo, check=True, env=alone)
            subprocess.run(["git", "rm", "-q", "--cached", "--ignore-unmatch", drop], cwd=self.repo, check=True,
                           env=alone)
            tree = subprocess.run(["git", "write-tree"], cwd=self.repo, check=True, env=alone, capture_output=True,
                                  text=True).stdout.strip()
            if parent and tree == run(self.repo, "git", "rev-parse", f"{parent}^{{tree}}"): continue
            dated = {**os.environ, "GIT_AUTHOR_NAME": fields[0],
                     "GIT_AUTHOR_EMAIL": "rewritten@example.com" if number == 0 else fields[1],
                     "GIT_AUTHOR_DATE": fields[2], "GIT_COMMITTER_NAME": fields[3], "GIT_COMMITTER_EMAIL": fields[4],
                     "GIT_COMMITTER_DATE": fields[5]}
            parent = subprocess.run(["git", "commit-tree", tree, *(["-p", parent] if parent else [])],
                                    cwd=self.repo, check=True, env=dated, input=message, capture_output=True,
                                    text=True).stdout.strip()
        run(self.repo, "git", "push", "-q", "--force", "origin", f"{parent}:refs/heads/main")
        run(self.repo, "git", "fetch", "-q", "origin")
        return old

    def land(self, branch):
        """(the new entry's ID or None, the Refused it raised or None), no worker started."""
        identifier, error = None, None
        with patch.object(mq.Queue, "start"), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            try: identifier = mq.add(self.repo, branch)
            except mq.Refused as refused: error = refused
        return identifier, error

    def test_a_branch_on_the_old_history_is_refused_with_the_rebase_that_moves_its_own_commits(self):
        self.grow_main("one", ".mailmap", "two", "three")
        self.commit("mine-1", "stale")
        own = self.commit("mine-2", "stale", start="stale")
        old = self.rewrite_main()
        newest = run(self.repo, "git", "rev-parse", old)
        mailmap = run(self.repo, "git", "log", "--format=%H", "-1", old, "--", ".mailmap")
        with patch.object(mq, "check") as trial, patch.object(mq, "linear") as replay:
            identifier, error = self.land("stale")
        trial.assert_not_called()
        replay.assert_not_called()
        self.assertIsNone(identifier)
        self.assertEqual(mq.Queue(self.repo).all(), [])
        words = str(error)
        self.assertIn(f"{own[:12]} is not queued: it is built on origin/main's history as it was before a rewrite", words)
        # start, one, two and three are copies; the .mailmap commit, pruned by the rewrite, is on main in no form.
        self.assertIn("4 of its 7 commits that origin/main lacks are old copies", words)
        self.assertIn(f"Its own work is the 2 commits after {newest[:12]}, the newest copy", words)
        command = ["git", "rebase", "--onto", "origin/main", newest, "stale"]
        self.assertIn(f"`{shlex.join(command)}`", words)
        self.assertIn(f"1 commit among the old history is on origin/main in no form", words)
        self.assertIn(f"{mailmap[:12]} Add .mailmap", words)
        advice = error.recovery
        self.assertEqual((advice["reasonCode"], advice["responsibleParty"], advice["action"], advice["retryable"]),
                         ("rewritten-main", "session", "rebase", True))
        self.assertEqual(advice["suggestedCommands"], [command])
        # The suggested rebase moves the branch's own commits alone, which then queue, without .mailmap.
        run(self.repo, *command)
        self.assertNotIn(".mailmap", run(self.repo, "git", "ls-tree", "--name-only", "stale").split())
        self.assertEqual(run(self.repo, "git", "rev-list", "--count", "origin/main..stale"), "2")
        identifier, error = self.land("stale")
        self.assertIsNone(error)
        self.assertEqual(self.state(identifier)["state"], mq.QUEUED)

    def test_a_rewrite_partway_down_main_is_found_from_the_fork(self):
        self.grow_main("one", "two", "three", "four", "five")
        self.commit("mine", "stale")
        self.rewrite_main(since=2)
        _, error = self.land("stale")
        self.assertIsInstance(error, mq.Refused)
        self.assertIn("4 of its 5 commits that origin/main lacks are old copies", str(error))
        self.assertNotIn("in no form", str(error))

    def test_a_branch_of_old_history_alone_is_told_it_has_nothing_to_land(self):
        self.grow_main("one", "two", "three")
        run(self.repo, "git", "checkout", "-q", "-B", "old", "origin/main")
        newest = run(self.repo, "git", "rev-parse", "old")
        self.rewrite_main()
        _, error = self.land("old")
        self.assertIn(f"It holds nothing after {newest[:12]}, the newest copy, so it has nothing of its own", str(error))
        self.assertEqual(mq.Queue(self.repo).all(), [])

    def test_an_ordinary_branch_is_queued(self):
        # Negative control.
        self.grow_main("one", "two")
        self.commit("mine", "fresh")
        identifier, error = self.land("fresh")
        self.assertIsNone(error)
        self.assertEqual(self.state(identifier)["state"], mq.QUEUED)

    def test_a_long_lived_branch_far_behind_main_is_queued(self):
        # Negative control: many commits of its own, and a fork far down main.
        self.commit("mine-0", "long")
        for number in range(1, 12): self.commit(f"mine-{number}", "long", start="long")
        self.grow_main(*(f"main-{number}" for number in range(8)))
        identifier, error = self.land("long")
        self.assertIsNone(error)
        self.assertEqual(self.state(identifier)["state"], mq.QUEUED)

    def test_a_branch_whose_commits_main_already_has_replayed_is_queued(self):
        # Negative control: commits main has as cherry-picks, or as the queue's own replay of an earlier landing of
        # this branch, carry new committer dates, so they are no rewrite's copies.
        self.grow_main("one")
        picked = [self.commit("mine-0", "picked")]
        for number in range(1, 5): picked.append(self.commit(f"mine-{number}", "picked", start="picked"))
        run(self.repo, "git", "checkout", "-q", "-B", "landing", "origin/main")
        self.commit("other", "landing", start="landing")
        later = {**os.environ, "GIT_COMMITTER_DATE": "2000000000 +0000"}
        subprocess.run(["git", "cherry-pick", *picked[:4]], cwd=self.repo, check=True, env=later, capture_output=True)
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        identifier, error = self.land("picked")
        self.assertIsNone(error)
        self.assertEqual(self.state(identifier)["state"], mq.QUEUED)

    def test_fewer_copies_than_the_floor_are_queued(self):
        # Negative control: a couple of commits picked onto main within the second keep their dates, but are too few.
        self.grow_main("one")
        picked = [self.commit("mine-0", "few")]
        for number in range(1, 4): picked.append(self.commit(f"mine-{number}", "few", start="few"))
        # On another commit of main's, so that a pick with every date kept is a new commit, not the same one.
        run(self.repo, "git", "checkout", "-q", "-B", "landing", "origin/main")
        self.commit("other", "landing", start="landing")
        for commit in picked[:mq.REWRITTEN_COPIES - 1]:
            subprocess.run(["git", "cherry-pick", commit], cwd=self.repo, check=True, env={
                **os.environ, "GIT_COMMITTER_DATE": run(self.repo, "git", "log", "-1", "--format=%cd", "--date=raw",
                                                        commit)}, capture_output=True)
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        _, copies = mq.old_copies(self.repo, run(self.repo, "git", "rev-parse", "few"))
        self.assertEqual(len(copies), mq.REWRITTEN_COPIES - 1)
        identifier, error = self.land("few")
        self.assertIsNone(error)
        self.assertEqual(self.state(identifier)["state"], mq.QUEUED)


class ImplicitDocsLane(Fixture):
    """`qk run docs` in a project with no docs lane runs the toolkit's docs check as a lane: offered to the second Mac
    and admitted like any other (owner's decision, 2026-11-09, vocaro-engineering#305). Queuekeeper has no spelling
    check, so spelling is a lane like any other, which a project has only when it configures one (vocaro/queuekeeper#69)."""
    OFFERED = SimpleNamespace(status=0)

    def eng_run(self, *words):
        return cli.main(["--project", str(self.repo), "run", *words])

    def test_it_is_offered_to_the_farm_mac_as_any_lane_is(self):
        with patch.object(cli.remote, "first", return_value=self.OFFERED) as first:
            self.assertEqual(self.eng_run("docs"), 0)
        config, lane = first.call_args.args[1:3]
        self.assertEqual(lane, "docs")
        self.assertEqual(config["lanes"]["docs"], {"command": configuration.IMPLICIT_DOCS,
                                                   "timeout": configuration.PREFLIGHT_LIMIT})
        self.assertIsNone(configuration.stays(config["lanes"], "docs"))

    def test_a_projects_own_spelling_lane_keeps_its_command(self):
        config = self.config()
        config["lanes"]["spelling"] = {"command": ["python3", "lane.py"]}
        self.publish(config, "Add a spelling lane")
        with patch.object(cli.remote, "first", return_value=self.OFFERED) as first:
            self.assertEqual(self.eng_run("spelling"), 0)
        self.assertEqual(first.call_args.args[1]["lanes"]["spelling"], {"command": ["python3", "lane.py"]})

    def test_it_waits_for_admission_and_no_admission_runs_nothing(self):
        with patch.object(cli, "refresh_policy", return_value="policy"), \
                patch.object(cli, "admitted_here", return_value=mq.resources.BUSY) as admitted, \
                patch.object(cli.processes, "run", side_effect=AssertionError("ran with no admission")):
            self.assertEqual(self.eng_run("--here", "docs"), mq.resources.BUSY)
        self.assertEqual(admitted.call_args.args[1:3], ("docs", "fixture docs"))

    def test_spelling_and_any_other_lane_a_project_lacks_are_refused(self):
        for name in ("spelling", "nonesuch"):
            with self.subTest(name=name), \
                    patch.object(cli.remote, "first", side_effect=AssertionError("offered a lane that does not exist")), \
                    contextlib.redirect_stderr(io.StringIO()) as said:
                self.assertEqual(self.eng_run(name), 2)
                self.assertIn(f"No configured lane: {name}", said.getvalue())

    def test_with_no_launcher_it_runs_with_the_toolkit_on_the_mac_that_runs_it(self):
        # On another Mac QUEUEKEEPER_ROOT names the toolkit sent there, and qk run there makes the command
        # (offload_host); a project needs no launcher for it (vocaro/queuekeeper#108).
        (self.repo / "scripts/qk").unlink()
        sent = self.base / "sent-toolkit"
        with patch.dict(os.environ, {"QUEUEKEEPER_ROOT": str(sent)}), \
                patch.object(cli.remote, "first", return_value=self.OFFERED) as first:
            self.assertEqual(self.eng_run("docs"), 0)
        config = first.call_args.args[1]
        self.assertEqual(config["lanes"]["docs"], {
            "command": [sys.executable, str(sent / "bin/qk"), "--project", str(self.repo), "docs", "check"],
            "timeout": configuration.PREFLIGHT_LIMIT})
        self.assertIsNone(configuration.stays(config["lanes"], "docs"))


class LaneThatCannotStart(Fixture):
    """A lane whose command cannot start is reported as that, not as the commit failing: qk run exits NOT_STARTED
    with a not-started receipt, and the queue holds for a session with every entry waiting (vocaro/queuekeeper#108)."""

    def eng_run(self, *words):
        with patch.object(cli, "refresh_policy", return_value="policy"), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as said:
            status = cli.main(["--project", str(self.repo), "run", "--here", *words])
        return status, said.getvalue()

    def lane(self, command):
        config = self.config()
        config["lanes"]["gate"] = {"command": command}
        self.publish(config, "Change the gate")

    def receipt(self):
        return json.loads((self.repo / ".build/queuekeeper/gate/source.json").read_text())

    def test_an_interpreter_with_no_file_to_run_could_not_start(self):
        # python3 exits 2 with "can't open file", as a failure would; qk run reads the file system instead.
        self.lane(["python3", "scripts/missing.py"])
        status, said = self.eng_run("gate")
        self.assertEqual(status, mq.lane_results.NOT_STARTED)
        self.assertEqual(self.receipt()["outcome"], "not-started")
        self.assertIn("python3 has no scripts/missing.py to run in ", self.receipt()["notStarted"])
        self.assertIn("qk run gate: could not start (exit 127), on this Mac", said)
        self.assertTrue((self.repo / ".build/queuekeeper/gate/run.log").read_text().startswith(
            "queuekeeper: gate could not start: "))

    def test_a_command_the_system_cannot_start_could_not_start(self):
        self.lane(["./no-such-tool"])
        status, _said = self.eng_run("gate")
        self.assertEqual(status, mq.lane_results.NOT_STARTED)
        self.assertEqual(self.receipt()["outcome"], "not-started")
        self.assertIn("Command did not start", self.receipt()["notStarted"])

    def test_a_command_that_starts_and_exits_127_or_2_is_a_failure(self):
        # Negative control: what the command itself exits is its own, whatever the status.
        for exit_status in (127, 2):
            with self.subTest(exit_status=exit_status):
                self.lane(["python3", "-c", f"import sys; sys.exit({exit_status})"])
                status, said = self.eng_run("gate")
                self.assertEqual(status, exit_status)
                self.assertEqual(self.receipt()["outcome"], "failed")
                self.assertNotIn("notStarted", self.receipt())
                self.assertNotIn("could not start", said)

    def test_the_queue_holds_and_blames_no_commit(self):
        self.lane(["python3", "scripts/missing.py"])
        self.commit("fix", "fix", content="a labeled point\n")
        [identifier] = self.add("fix")
        with patch("builtins.print"): self.work()
        self.assertEqual(self.state(identifier)["state"], "queued")
        held = mq.Queue(self.repo).held()
        self.assertIn("for a session, not the owner: gate could not start on this Mac (", held)
        self.assertIn("python3 has no scripts/missing.py to run in ", held)
        self.assertIn("says nothing of the batch's commits and none is sent back", held)
        self.assertNotIn("fail on this commit", held)

    def test_a_lane_whose_own_command_exits_127_still_sends_the_commit_back(self):
        # Negative control: a command that started and exited 127 leaves no not-started receipt.
        self.lane(["python3", "-c", "import sys; sys.exit(127)"])
        self.commit("fix", "fix", content="a labeled point\n")
        [identifier] = self.add("fix")
        with patch("builtins.print"): self.work()
        self.assertEqual(self.state(identifier)["state"], "failed")
        self.assertIn("the queue's lanes fail on this commit", self.state(identifier)["why"])
        self.assertIsNone(mq.Queue(self.repo).held())


class UnprovisionedWhereTheGateRuns(Fixture):
    """A queue gate provisions its own checkout, and a provision failure there is the Mac's environment (#19)."""

    def test_a_code_batch_provisions_the_gate_checkout_only_once(self):
        config = self.config()
        (self.repo / ".git/info/exclude").write_text(".build/\n")
        steps = self.base / "steps"
        # The gate checks the input in its own checkout, and the shared trace counts provision runs.
        config["lanes"]["provision"] = {"command": ["python3", "-c", (
            "from pathlib import Path; Path('.build/provisioned').touch(); "
            f"Path({str(steps)!r}).open('a').write('provision\\n')")]}
        config["lanes"]["gate"] = {"command": ["python3", "-c", (
            "from pathlib import Path; assert Path('.build/provisioned').exists(); "
            f"Path({str(steps)!r}).open('a').write('gate\\n')")], "here": "test runs in the worker checkout"}
        self.publish(config, "Add a provision lane")
        self.commit("fix", "fix", content="a labeled point\n")
        [identifier] = self.add("fix")
        with contextlib.redirect_stdout(io.StringIO()) as said: self.work()
        self.assertEqual(self.state(identifier)["state"], "landed", said.getvalue())
        self.assertEqual(steps.read_text(), "provision\ngate\n")

    def test_a_gate_whose_checkout_cannot_be_provisioned_comes_back_identifying_the_missing_environment(self):
        config = self.config()
        config["lanes"]["provision"] = {"command": ["python3", "-c", "print('3 required inputs missing'); exit(4)"]}
        config["queue"]["noVerdictRuns"] = 1
        self.publish(config, "Add a provision lane")
        self.commit("fix", "fix", content="a labeled point\n")
        [identifier] = self.add("fix")
        with contextlib.redirect_stdout(io.StringIO()) as said: self.work()
        # The gate's own command never ran, and the result names the missing environment.
        self.assertEqual(self.gates(), 0, said.getvalue())
        self.assertEqual(self.state(identifier)["state"], "failed", said.getvalue())
        self.assertNotIn("fix", self.main_files())
        held = self.state(identifier)["why"]
        checkout = mq.Queue(self.repo).checkout
        self.assertIn(f"the lanes gave no verdict in 1 run in a row, the last with exit {mq.remote.MISCONFIGURED}, this Mac "
                      f"({offload_host.mac_name()}) could not provision its checkout: the provision lane exited 4 in ", held)
        self.assertIn(f"so gate did not run here; its log is {checkout / '.build/queuekeeper/provision/run.log'}", held)
        self.assertIsNone(mq.Queue(self.repo).held())
        self.assertIn("3 required inputs missing", (checkout / ".build/queuekeeper/provision/run.log").read_text())
        self.assertNotIn("fail on this commit", held)
        self.assertNotIn("a shared host's configuration", held)


class PinnedFolders(Fixture):
    """vocaro-engineering#497: a round pins each folder beside the project that its plugins load from at its
    origin/main, so that every lane of the round, and each qk command nested in one, loads one commit of it, whatever
    lands in the shared checkout or a session edits there meanwhile."""
    KIT = ("import os\n"
           "VALUE = {value!r}\n"
           "if VALUE == 'unloadable': raise RuntimeError('this kit needs a newer Queuekeeper')\n"
           "def register():\n"
           "    if os.environ.get('KIT_SEEN'): open(os.environ['KIT_SEEN'], 'a').write(VALUE + '\\n')\n")

    def setUp(self):
        super().setUp()
        self.kit_origin, self.kit, self.seen = self.base / "kit.git", self.base / "kit", self.base / "seen"
        run(self.base, "git", "init", "-q", "--bare", "-b", "main", str(self.kit_origin))
        run(self.base, "git", "clone", "-q", str(self.kit_origin), str(self.kit))
        self.one = self.kit_lands("one")
        seeing = patch.dict(os.environ, {"KIT_SEEN": str(self.seen)})
        seeing.start()
        self.addCleanup(seeing.stop)
        config = json.loads((self.repo / "queuekeeper.json").read_text())
        config["plugins"] = [{"module": "fixture_kit", "path": "../kit/src/python"}]
        self.publish(config, "Load a plugin from a folder beside the project")

    def kit_lands(self, value, here=True):
        """A commit of the kit that says value, on its origin's main, and with here in the shared checkout too."""
        (self.kit / "src/python").mkdir(parents=True, exist_ok=True)
        (self.kit / "src/python/fixture_kit.py").write_text(self.KIT.format(value=value))
        run(self.kit, "git", "add", ".")
        run(self.kit, "git", "commit", "-qm", value)
        commit = run(self.kit, "git", "rev-parse", "HEAD")
        run(self.kit, "git", "push", "-q", "origin", "HEAD:main")
        if not here: run(self.kit, "git", "reset", "-q", "--hard", "HEAD~1")
        return commit

    def loaded(self):
        """What each qk command since the last look loaded of the kit."""
        found = set(self.seen.read_text().split()) if self.seen.exists() else set()
        self.seen.unlink(missing_ok=True)
        return found

    def round(self, name):
        self.commit(name, name, content="a labeled point\n")
        identifier, = self.add(name)
        said = io.StringIO()
        with contextlib.redirect_stdout(said): self.work()
        return identifier, said.getvalue()

    def test_every_lane_loads_origin_main_not_what_the_shared_checkout_holds(self):
        # A session's edit in the shared checkout, and then a landing that the checkout has not followed.
        (self.kit / "src/python/fixture_kit.py").write_text(self.KIT.format(value="a session's edit"))
        first, said = self.round("first")
        self.assertEqual(self.state(first)["state"], "landed")
        self.assertEqual(self.loaded(), {"one"})
        self.assertIn(f"folders beside the project: kit at {self.one[:12]}", said)
        run(self.kit, "git", "checkout", "-q", "--", ".")
        two = self.kit_lands("two", here=False)
        second, said = self.round("second")
        self.assertEqual(self.state(second)["state"], "landed")
        self.assertEqual(self.loaded(), {"two"})
        self.assertIn(f"folders beside the project: kit at {two[:12]}", said)
        self.assertEqual(json.loads((mq.Queue(self.repo).directory / mq.PINS_STARTED).read_text())["siblings"],
                         {"kit": two})

    def test_gates_the_batch_with_its_own_pin_then_keeps_it_on_main(self):
        two = self.kit_lands("two", here=False)
        # After the second release the shared checkout is still at one, and the floating ref moves again.
        run(self.kit, "git", "reset", "-q", "--hard", two)
        three = self.kit_lands("three", here=False)
        config = json.loads((self.repo / "queuekeeper.json").read_text())
        config["siblings"] = {"kit": {"variable": "KIT_ROOT", "revision": self.one}}
        config["lanes"]["gate"]["command"] = ["python3", "-c", """
import json, os, subprocess
from pathlib import Path
settings = json.loads(Path('queuekeeper.json').read_text())
root = Path(os.environ['KIT_ROOT'])
declared = settings['siblings']['kit']
expected = declared['revision'] if isinstance(declared, dict) else os.environ['KIT_FLOATING']
assert subprocess.check_output(['git', '-C', root, 'rev-parse', 'HEAD'], text=True).strip() == expected
"""]
        self.publish(config, "Declare the initial kit revision")
        for branch, selected, expected in (("bump-pin", {"variable": "KIT_ROOT", "revision": two}, two),
                                           ("return-to-floating", "KIT_ROOT", three)):
            with self.subTest(branch=branch), patch.dict(os.environ, {"KIT_FLOATING": three}):
                config["siblings"]["kit"] = selected
                tip = self.commit("queuekeeper.json", branch, content=json.dumps(config))
                if isinstance(selected, dict):
                    status, printed, _ = mq.preflight_run(mq.Queue(self.repo), self.repo, tip, "gate")
                    self.assertEqual(status, 0, printed)
                [entry] = self.add(branch)
                self.loaded()
                self.assertEqual(self.work(), 0)
                self.assertEqual(self.state(entry)["state"], "landed")
                checkout = mq.Queue(self.repo).checkout
                receipt = json.loads((checkout / ".build/queuekeeper/gate/source.json").read_text())
                self.assertEqual(receipt["siblings"]["kit"]["commit"], expected)
        self.assertEqual(run(self.kit, "git", "rev-parse", "HEAD"), two)

    def test_a_candidate_pin_that_cannot_be_fetched_does_not_gate_with_the_main_pin(self):
        config = json.loads((self.repo / "queuekeeper.json").read_text())
        config["siblings"] = {"kit": {"variable": "KIT_ROOT", "revision": self.one}}
        self.publish(config, "Declare the initial kit revision")
        config["siblings"]["kit"]["revision"] = "4" * 40
        self.commit("queuekeeper.json", "unavailable-pin", content=json.dumps(config))
        [entry] = self.add("unavailable-pin")
        self.assertEqual(self.work(), 0)
        self.assertEqual(self.state(entry)["state"], "failed")
        self.assertEqual(self.gates(), 0)
        self.assertIn("kit could not be pinned at " + "4" * 40, Path(self.state(entry)["log"]).read_text())

    def test_a_set_that_does_not_start_is_not_adopted_and_the_round_runs_with_the_last_that_did(self):
        first, _ = self.round("first")
        self.assertEqual(self.state(first)["state"], "landed")
        self.loaded()
        broken = self.kit_lands("unloadable")
        second, said = self.round("second")
        self.assertEqual(self.state(second)["state"], "landed")
        self.assertIn(f"Queuekeeper does not start in the project at origin/main with kit at {broken[:12]}, each at "
                      "its origin/main", said)
        self.assertIn(f"this round runs with kit at {self.one[:12]}, which the last set that started held, and the "
                      "next round tries origin/main again", said)
        # Only the trial loaded the one that does not load, and it says nothing: every lane ran with the earlier.
        self.assertEqual(self.loaded(), {"one"})
        fixed = self.kit_lands("three")
        third, said = self.round("third")
        self.assertEqual(self.state(third)["state"], "landed")
        self.assertIn(f"folders beside the project: kit at {fixed[:12]}", said)
        self.assertEqual(self.loaded(), {"three"})

    def test_with_no_earlier_set_one_that_does_not_start_holds_for_a_session_and_sends_nothing_back(self):
        broken = self.kit_lands("unloadable")
        first, _ = self.round("first")
        self.assertEqual(self.state(first)["state"], "queued")
        self.assertEqual(self.gates(), 0)
        held = mq.Queue(self.repo).held()
        self.assertIn(f"for a session, not the owner: Queuekeeper does not start in the project at origin/main with "
                      f"kit at {broken[:12]}, each at its origin/main", held)
        self.assertIn("and no earlier set started: bring Queuekeeper and those folders to commits that work together, "
                      "which no entry of this queue can do", held)

    def test_a_worker_started_inside_a_pinned_run_makes_its_own_set(self):
        stale = json.dumps({"kit": {"commit": "4" * 40, "root": str(self.base / "gone")}})
        with patch.dict(os.environ, {mq.pins.VARIABLE: stale}):
            self.assertNotIn(mq.pins.VARIABLE, mq.environment())
            first, _ = self.round("first")
        self.assertEqual(self.state(first)["state"], "landed")
        self.assertEqual(self.loaded(), {"one"})


class QueuekeeperThatCannotStart(Fixture):
    """vocaro/queuekeeper#158: a lane in which Queuekeeper itself cannot start, as when a folder beside the project
    that one of its plugins loads from moves to a commit this Queuekeeper cannot load, tested no commit: the queue
    holds for a session with every entry waiting. On 2026-11-15 each such lane was a traceback with exit 1, and the
    queue bisected and sent back at least nine entries."""

    def setUp(self):
        super().setUp()
        # A plugin in a folder beside the project, which every lane's qk loads as it is now, whatever the commit.
        self.adapter = self.base / "adapter"
        self.adapter.mkdir()
        (self.adapter / "fixture_adapter.py").write_text(
            "from pathlib import Path\n"
            "if (Path(__file__).parent / 'moved').exists():\n"
            "    raise RuntimeError('Queuekeeper at /worker   predates standalone plugin composition')\n"
            "def register(): pass\n")
        config = json.loads((self.repo / "queuekeeper.json").read_text())
        config["plugins"] = [{"module": "fixture_adapter", "path": str(self.adapter)}]
        self.publish(config, "Load a plugin from a folder beside the project")

    def qk(self, *words):
        return subprocess.run([sys.executable, "scripts/qk", *words], cwd=self.repo, capture_output=True, text=True)

    def test_a_command_whose_plugin_does_not_load_exits_70_and_says_so(self):
        (self.adapter / "moved").touch()
        ran = self.qk("run", "--here", "gate")
        self.assertEqual(ran.returncode, mq.lane_results.TOOL_NOT_STARTED, ran.stderr)
        self.assertIn("Traceback (most recent call last):", ran.stderr)
        self.assertIn("queuekeeper: could not start: RuntimeError: Queuekeeper at /worker predates standalone plugin "
                      "composition\n", ran.stderr)
        self.assertIn("says nothing of the project's commits (exit 70)", ran.stderr)
        self.assertEqual(self.gates(), 0)
        self.assertFalse((self.repo / ".build/queuekeeper/gate/source.json").exists())

    def test_the_queue_holds_and_blames_no_commit_and_the_entries_land_after_resume(self):
        (self.adapter / "moved").touch()
        self.commit("first", "first", content="a labeled point\n")
        self.commit("second", "second", content="a labeled line\n")
        ids = self.add("first", "second")
        with patch("builtins.print"): self.work()
        self.assertEqual(self.gates(), 0)
        self.assertEqual([self.state(identifier)["state"] for identifier in ids], ["queued", "queued"])
        held = mq.Queue(self.repo).held()
        self.assertIn("for a session, not the owner: gate could not start: Queuekeeper itself did not start on "
                      "this Mac, before the lane's command, with exit 70: RuntimeError: Queuekeeper at /worker "
                      "predates standalone plugin composition. Its command never ran", held)
        self.assertIn("says nothing of the batch's commits and none is sent back", held)
        self.assertIn("bring Queuekeeper and the folders the project's plugins load from to commits that work "
                      "together", held)
        self.assertIn("qk queue resume", held)
        # Asked without the batch, it does not start either, so no entry is why (batch_stops_start).
        self.assertIn("none is sent back: Queuekeeper does not start on this Mac without the batch's commits either, "
                      "so no entry is why: bring Queuekeeper", held)
        self.assertNotIn("fail on this commit", held)
        # The cause gone, a resume lands them: nothing was sent back to be added again.
        (self.adapter / "moved").unlink()
        mq.Queue(self.repo).hold_file.unlink()
        with patch("builtins.print"): self.assertEqual(self.work(), 0)
        self.assertEqual([self.state(identifier)["state"] for identifier in ids], ["landed", "landed"])
        self.assertLessEqual({"first", "second"}, self.main_files())
        self.assertIsNone(mq.Queue(self.repo).held())

    def breaking(self, branch):
        """A commit on branch that has the project load a plugin no folder holds, so that Queuekeeper starts on
        main and not on the commit."""
        config = json.loads((self.repo / "queuekeeper.json").read_text())
        config["plugins"].append({"module": "absent_adapter", "path": str(self.adapter)})
        self.commit("queuekeeper.json", branch, content=json.dumps(config))

    def test_an_entry_whose_own_commit_stops_queuekeeper_is_sent_back_and_the_queue_is_not_held(self):
        # Queuekeeper starts on main alone, so the batch is why: bisected as a failed lane, with the good entry landed.
        self.breaking("breaks")
        self.commit("good", "good", content="a labeled point\n")
        breaks, good = self.add("breaks", "good")
        with contextlib.redirect_stdout(io.StringIO()) as said: self.work()
        self.assertIsNone(mq.Queue(self.repo).held())
        self.assertEqual(self.state(breaks)["state"], "failed")
        self.assertIn("the queue's lanes fail on this commit", self.state(breaks)["why"])
        self.assertEqual(self.state(good)["state"], "landed")
        self.assertIn("good", self.main_files())
        self.assertNotIn("absent_adapter", run(self.repo, "git", "show", "origin/main:queuekeeper.json"))
        self.assertIn("without the batch, so the batch's commits stop it: a failure of theirs", said.getvalue())
        # No lane's command ran on the breaking commit: only the good entry's gate ran.
        self.assertEqual(self.gates(), 1)

    def test_only_a_start_without_the_batch_and_none_with_it_blames_the_batch(self):
        worker = mq.Worker(mq.Queue(self.repo))
        log = self.base / "round-gate.log"
        resets = []
        def asked(*answers):
            """batch_stops_start with each start answering in turn; the commits the checkout was reset to after."""
            del resets[:]
            with patch.object(mq.Worker, "starts", side_effect=list(answers)), \
                    patch.object(mq.Worker, "reset", lambda _, commit: resets.append(commit)):
                return worker.batch_stops_start("base", "head", log)
        # Here the lane itself showed that it does not start with the batch: only main is asked, and head restored.
        self.assertTrue(asked(True))
        self.assertEqual((resets, worker.probed), (["base", "head"], ""))
        self.assertFalse(asked(False))
        self.assertEqual(resets, ["base", "head"])
        self.assertIn("does not start on this Mac without the batch's commits either", worker.probed)
        # A start that says neither, as one that reached START_LIMIT, blames no entry and claims nothing.
        self.assertFalse(asked(None))
        self.assertEqual(worker.probed, "")
        # The lane ran on another Mac: a batch that starts here is not why, and main is not asked.
        worker.ran_on = "build-host"
        self.assertFalse(asked(True))
        self.assertEqual(resets, [])
        self.assertIn("so the fault is in what that Mac holds", worker.probed)
        # One that starts here at neither is the folders' fault here too; at main alone, the batch's.
        self.assertFalse(asked(False, False))
        self.assertTrue(asked(False, True))
        self.assertEqual(resets, ["base", "head"])

    def test_a_start_is_read_by_its_exit(self):
        worker = mq.Worker(mq.Queue(self.repo))
        worker.checkout = self.repo
        log = self.base / "round-gate.log"
        self.assertIs(worker.starts(log), True)
        self.assertEqual(self.gates(), 0)
        (self.adapter / "moved").touch()
        self.assertIs(worker.starts(log), False)
        self.assertIn("queuekeeper: could not start: RuntimeError", (self.base / "round-gate-start.log").read_text())
        with patch.object(mq.processes, "run", side_effect=mq.processes.TimedOut("no end")):
            self.assertIsNone(worker.starts(log))
        with patch.object(mq.processes, "run", return_value=1): self.assertIsNone(worker.starts(log))

    def test_a_lane_whose_own_command_exits_70_still_sends_the_commit_back(self):
        # Negative control: the command ran, and qk run wrote the lane's receipt, so the status is the command's own.
        config = self.config()
        config["lanes"]["gate"] = {"command": ["python3", "-c", "import sys; sys.exit(70)"]}
        self.publish(config, "Change the gate")
        self.commit("fix", "fix", content="a labeled point\n")
        [identifier] = self.add("fix")
        with patch("builtins.print"): self.work()
        self.assertEqual(self.state(identifier)["state"], "failed")
        self.assertIn("the queue's lanes fail on this commit", self.state(identifier)["why"])
        self.assertIsNone(mq.Queue(self.repo).held())

    def test_a_failing_lane_with_the_plugin_loaded_still_sends_the_commit_back(self):
        self.commit("bad", "bad")
        [identifier] = self.add("bad")
        with patch("builtins.print"): self.work()
        self.assertEqual(self.gates(), 1)
        self.assertEqual(self.state(identifier)["state"], "failed")
        self.assertIn("the queue's lanes fail on this commit", self.state(identifier)["why"])
        self.assertIsNone(mq.Queue(self.repo).held())

    def test_only_a_run_that_left_no_receipt_of_the_lane_is_queuekeepers_own(self):
        worker = mq.Worker(mq.Queue(self.repo))
        worker.checkout = self.base / "checkout"
        log = self.base / "round-gate.log"
        log.write_text("Traceback (most recent call last):\nqueuekeeper: could not start: ModuleNotFoundError: No "
                       "module named 'queuekeeper.apple'\n")
        said = "before the lane's command, with exit 70: ModuleNotFoundError: No module named 'queuekeeper.apple'"
        began = time.time()
        receipt = worker.checkout / ".build/queuekeeper/gate/source.json"
        receipt.parent.mkdir(parents=True)
        # Here: no receipt, or one an earlier run left, and the lane's command never ran.
        self.assertEqual(worker.tool_unstarted("gate", began, 70, None, log),
                         f"Queuekeeper itself did not start on this Mac, {said}")
        receipt.write_text("{}")
        os.utime(receipt, (began - 60, began - 60))
        self.assertIn("on this Mac", worker.tool_unstarted("gate", began, 70, None, log))
        # A receipt written since the lane began is of a command that ran and exited 70 itself.
        os.utime(receipt, (began + 1, began + 1))
        self.assertIsNone(worker.tool_unstarted("gate", began, 70, None, log))
        # Any other status is not Queuekeeper's.
        os.utime(receipt, (began - 60, began - 60))
        self.assertIsNone(worker.tool_unstarted("gate", began, 1, None, log))
        # On another Mac: the work stage's own exit, with no receipt of the lane come back.
        ran = self.base / "dispatch"
        ran.mkdir()
        def there(**result): (ran / "source.json").write_text(json.dumps({"host": "build-host", "result": result}))
        there(stage="work", exitStatus=70, stopped=False)
        self.assertEqual(worker.tool_unstarted("gate", began, 70, ran, log),
                         f"Queuekeeper itself did not start there, {said}")
        there(stage="work", exitStatus=70, stopped=False, receipt={"outcome": "failed"})
        self.assertIsNone(worker.tool_unstarted("gate", began, 70, ran, log))
        there(stage="provision", exitStatus=70, stopped=False)
        self.assertIsNone(worker.tool_unstarted("gate", began, 70, ran, log))
        (ran / "source.json").unlink()
        self.assertIsNone(worker.tool_unstarted("gate", began, 70, ran, log))
        # The hold reads it only for that status.
        worker.tooling = "why"
        self.assertEqual(worker.unstarted(70), "why")
        self.assertIsNone(worker.unstarted(1))


class PreflightStoppedByRemoval(Fixture):
    """Withdraw an entry after its preflight lane's command has actually acquired admission (#301)."""

    def setUp(self):
        super().setUp()
        self.ready, self.release = self.base / "ready", self.base / "release"
        self.stopped = self.base / "stopped"
        self.first_done, self.next_done = self.base / "first-done", self.base / "next-done"
        config = self.config()
        command = [sys.executable, "-c", """
import os, pathlib, signal, sys, time

ready, release, stopped, first_done, next_done = map(pathlib.Path, sys.argv[1:])
assert os.environ["QUEUEKEEPER_RESOURCE_TOKEN"]

def stop(signum, frame):
    stopped.touch()
    sys.exit(128 + signum)

if pathlib.Path("hold-check").exists():
    signal.signal(signal.SIGTERM, stop)
    ready.touch()
    while not release.exists():
        time.sleep(0.02)
    first_done.touch()
else:
    next_done.touch()
""", *map(str, (self.ready, self.release, self.stopped, self.first_done, self.next_done))]
        config["lanes"]["check"] = {"command": command, "timeout": int(stretched(45))}
        config["queue"]["preflight"] = ["check"]
        self.publish(config, "Make the preflight lane wait in a chosen batch")
        self.guard = mq.resources.guard_path(os.environ)
        # The admitted run, as of a check that did not end unadmitted in time (Worker.quick, vocaro/queuekeeper#169).
        unadmitted = patch.object(mq.Worker, "quick", return_value=None)
        unadmitted.start()
        self.addCleanup(unadmitted.stop)

    def remove_during_check(self, testing, queued_branch=None):
        look, removed, polls = mq.Removal.look, [], 0

        def after_ready(removal):
            nonlocal polls
            if not self.ready.exists() or testing not in removal.ids:
                return look(removal)
            if not removed:
                self.assertTrue(mq.resources.guard_owners(os.environ), "check has no admission")
                self.assertEqual(self.state(testing)["state"], mq.TESTING)
                identifier = self.add(queued_branch)[0] if queued_branch else testing
                if queued_branch:
                    self.assertNotIn(identifier, removal.ids)
                    self.assertEqual(self.state(identifier)["state"], mq.QUEUED)
                mq.remove(mq.Queue(self.repo), identifier)
                removed.append(identifier)
            gone = look(removal)
            if queued_branch and not self.release.exists():
                self.assertFalse(gone)
                self.assertFalse(self.stopped.exists())
                polls += 1
                if polls == 3:
                    self.release.touch()
            return gone

        with patch.object(mq.Removal, "look", after_ready), patch.object(mq, "REMOVAL_POLL", 0.1), \
                patch.object(mq.Worker, "stack"), contextlib.redirect_stdout(io.StringIO()) as said:
            self.assertEqual(self.work(), 0, said.getvalue())
        log = said.getvalue()
        self.assertEqual(len(removed), 1, log)
        self.assertEqual(self.state(removed[0])["state"], mq.REMOVED, log)
        self.assertFalse(self.guard.exists(), "check left an admission guard")
        self.assertIsNone(mq.Queue(self.repo).held())
        self.assertEqual(self.gates(), 1, log)
        return removed[0], log

    def test_removing_a_testing_entry_stops_an_admitted_preflight_lane_and_the_survivor_lands(self):
        self.commit("hold-check", "withdrawn")
        self.commit("survives", "survivor")
        withdrawn, survivor = self.add("withdrawn", "survivor")
        _, log = self.remove_during_check(withdrawn)
        self.assertTrue(self.stopped.exists(), log)
        self.assertFalse(self.first_done.exists(), log)
        self.assertTrue(self.next_done.exists(), log)
        self.assertRegex(log, f"check: stopped[^\\n]*, as {withdrawn} left the queue")
        self.assertIn(f"{withdrawn} left the queue, which stopped the gate: gating what is left", log)
        self.assertNotIn("no verdict", log)
        self.assertEqual(self.state(survivor)["state"], mq.LANDED, log)
        self.assertNotIn("hold-check", self.main_files())
        self.assertIn("survives", self.main_files())

    def test_removing_an_entry_outside_the_batch_leaves_an_admitted_preflight_lane_running(self):
        self.commit("hold-check", "testing")
        self.commit("later", "queued")
        [testing] = self.add("testing")
        removed, log = self.remove_during_check(testing, queued_branch="queued")
        self.assertTrue(self.first_done.exists(), log)
        self.assertFalse(self.stopped.exists(), log)
        self.assertFalse(self.next_done.exists(), log)
        self.assertIn(f"removed {removed}", log)
        self.assertNotIn("check: stopped", log)
        self.assertEqual(self.state(testing)["state"], mq.LANDED, log)


class PreflightBeforeQueued(Fixture):
    """qk land and `qk queue add` run main's queue.preflight lanes, in order, on the entry's commits replayed onto
    origin/main, and refuse it on the first finding (vocaro-engineering#283); a project that lists none has none, a
    lane named spelling among them (vocaro/queuekeeper#69)."""
    def setUp(self):
        super().setUp()
        real = patch.object(mq, "preflight_before_queued", PREFLIGHT_BEFORE_QUEUED)
        real.start()
        self.addCleanup(real.stop)

    def land(self, branch):
        """(the new entry's ID or None, what qk land said, the error it refused with or None), no worker started."""
        said, error = io.StringIO(), None
        with patch.object(mq.Queue, "start"), contextlib.redirect_stdout(said), contextlib.redirect_stderr(said):
            try: mq.add(self.repo, branch)
            except mq.Refused as refused: error = str(refused)
        identifier = next((e["id"] for e in reversed(mq.Queue(self.repo).all())
                           if e["branch"] == branch and e["state"] in mq.WAITING), None)
        return identifier, said.getvalue(), error

    def preflight(self, *named, **lanes):
        """Give main the lanes, each a command, and queue.preflight naming named."""
        config = self.config()
        config["lanes"].update({name: {"command": command, "timeout": 60} for name, command in lanes.items()})
        config["queue"]["preflight"] = list(named)
        self.publish(config, "Name the preflight lanes")

    @staticmethod
    def finding(word, code=1):
        """A check that prints a finding naming word and exits code on a tree holding a file named fix, else 0."""
        return ["python3", "-c", f"import os, sys; found = os.path.exists('fix'); print('{word} ran'); "
                                 f"found and print('fix:1: {word} found it'); sys.exit({code} if found else 0)"]

    def test_a_finding_is_refused_with_its_findings_and_nothing_is_queued(self):
        self.preflight_check()
        self.commit("fix", "fix", content=FLAGGED)
        identifier, _, error = self.land("fix")
        self.assertIsNone(identifier)
        self.assertEqual(mq.Queue(self.repo).all(), [])
        self.assertEqual(run(self.repo, "git", "for-each-ref", "refs/queuekeeper/queue"), "")
        self.assertIn("is not queued: python3 check.py fails on it replayed onto origin/main", error)
        self.assertIn("fix:1: flag" + "ged", error)
        self.assertIn("Fix that and land it again", error)

    def test_an_entry_that_drops_a_lane_main_lists_is_told_to_land_that_first(self):
        """vocaro/queuekeeper#186: main's queue.preflight counts, so one commit cannot both drop a lane and what it
        reads. The refusal says so, and names the two landings."""
        config = self.preflight_check()
        self.commit("fix", "fix", content=FLAGGED)
        config["queue"]["preflight"] = []
        (self.repo / "queuekeeper.json").write_text(json.dumps(config))
        run(self.repo, "git", "commit", "-qam", "Drop the check")
        identifier, _, error = self.land("fix")
        self.assertIsNone(identifier)
        self.assertIn("fix:1: flag" + "ged", error)
        self.assertIn("origin/main's queue.preflight lists check, and only main's list counts, so check ran as main "
                      "has it although this entry's queuekeeper.json leaves check out of its queue.preflight. Land the "
                      "change to queue.preflight and the check lane on its own first, and what check reads in a second "
                      "landing once that is on main", error)
        self.assertNotIn("Fix that and land it again", error)

    def test_an_entry_that_changes_the_lane_is_told_the_same(self):
        config = self.preflight_check()
        self.commit("fix", "fix", content=FLAGGED)
        config["lanes"]["check"]["timeout"] = 30
        (self.repo / "queuekeeper.json").write_text(json.dumps(config))
        run(self.repo, "git", "commit", "-qam", "Change the check")
        _, _, error = self.land("fix")
        self.assertIn("although this entry's queuekeeper.json changes the check lane.", error)

    def test_a_clean_entry_is_queued_and_says_it_was_checked(self):
        # The positive control.
        self.preflight_check()
        self.commit("fix", "fix", content="a clean point\n")
        identifier, said, error = self.land("fix")
        self.assertIsNone(error)
        self.assertEqual(self.state(identifier)["state"], "queued")
        self.assertIn("check checked in", said)
        self.assertIn("python3 check.py passes on it", said)

    def test_an_entry_behind_main_is_checked_replayed_onto_it(self):
        self.preflight_check()
        old = run(self.repo, "git", "rev-parse", "origin/main")
        self.commit("other", "other")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        self.commit("fix", "fix", content=FLAGGED, start=old)
        main = run(self.repo, "git", "rev-parse", "origin/main")
        identifier, _, error = self.land("fix")
        self.assertIsNone(identifier)
        self.assertIn(f"replayed onto origin/main at {main[:12]}", error)

    def test_the_lanes_run_in_order_and_the_first_finding_refuses(self):
        self.preflight("first", "second", first=["true"], second=self.finding("second"))
        self.commit("fix", "fix")
        identifier, said, error = self.land("fix")
        self.assertIsNone(identifier)
        self.assertIn("first checked in", said)
        self.assertIn("fix:1: second found it", error)

    def test_none_listed_checks_nothing(self):
        self.preflight()
        self.commit("fix", "fix")
        identifier, said, error = self.land("fix")
        self.assertIsNone(error)
        self.assertEqual(self.state(identifier)["state"], "queued")
        self.assertNotIn("passes on it replayed onto origin/main", said)
        # qk land's trial integration says "queue: checked in"; a preflight lane names itself first.
        self.assertNotRegex(said, r"queue: \S+ checked in")

    def test_a_lane_named_spelling_that_queue_preflight_does_not_list_is_not_run(self):
        """vocaro/queuekeeper#69: the queue once ran a lane named spelling before it queued an entry, by its name alone.
        With no queue.preflight, the lane's finding refuses nothing, and nothing warns of a spelling check."""
        config = self.config()
        config["lanes"]["spelling"] = {"command": self.finding("spelling")}
        self.publish(config, "A spelling lane the queue does not list")
        self.assertNotIn("preflight", self.config()["queue"])
        self.commit("fix", "fix")
        identifier, said, error = self.land("fix")
        self.assertIsNone(error)
        self.assertEqual(self.state(identifier)["state"], "queued")
        self.assertNotIn("spelling", said)
        # qk land's trial integration says "queue: checked in"; a preflight lane names itself first.
        self.assertNotRegex(said, r"queue: \S+ checked in")

    def test_a_lane_finds_the_projects_main_checkout(self):
        self.preflight("where", where=["python3", "-c", f"import os; print('at', os.environ['{mq.PROJECT_CHECKOUT_VARIABLE}']); "
                                                        "raise SystemExit(1 if os.path.exists('fix') else 0)"])
        self.commit("fix", "fix")
        _, _, error = self.land("fix")
        self.assertIn(f"at {self.repo.resolve()}", error)

    def test_a_lane_that_fails_with_no_finding_queues_the_entry(self):
        self.preflight("lint", lint=self.finding("lint", code=2))
        self.commit("fix", "fix")
        identifier, said, error = self.land("fix")
        self.assertIsNone(error)
        self.assertEqual(self.state(identifier)["state"], "queued")
        self.assertIn("exited 2 on it, which is no finding, so the round runs the lint lane", said)

    def test_a_failure_main_already_has_is_not_the_entrys(self):
        # main took a finding past the queue: an entry that adds a clean file is queued, and says why.
        self.preflight_check()
        run(self.repo, "git", "checkout", "-q", "main")
        (self.repo / "old").write_text(FLAGGED)
        run(self.repo, "git", "add", "old")
        run(self.repo, "git", "commit", "-qm", "Past the queue")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        self.commit("other", "other")
        identifier, said, error = self.land("other")
        self.assertIsNone(error)
        self.assertEqual(self.state(identifier)["state"], "queued")
        self.assertIn("fails on origin/main at", said)

    def test_a_run_past_its_limit_queues_the_entry(self):
        self.preflight_check()
        self.commit("fix", "fix", content=FLAGGED)
        with patch.object(mq.processes, "run", side_effect=mq.processes.TimedOut("300 s")):
            identifier, said, error = self.land("fix")
        self.assertIsNone(error)
        self.assertEqual(self.state(identifier)["state"], "queued")
        self.assertIn("could not check its check, so the round runs the check lane", said)


class AheadOfTheQueue(Fixture):
    """qk land's emergency options --front and --no-gate, in every repository with no setting (owner's decision,
    2026-11-04, vocaro-engineering#163); --no-gate needs a written reason."""
    WHY = "a flaky voice test unrelated to the fix fails the gate"

    def setUp(self):
        super().setUp()
        self.light_counter, self.light_status = self.base / "light-runs", self.base / "light-status"
        config = json.loads((self.repo / "queuekeeper.json").read_text())
        config["lanes"]["light"] = {"command": ["python3", "lane.py", str(self.light_counter), str(self.light_status)]}
        config["queue"]["preflight"] = ["light"]
        self.configured = config
        self.publish(config, "Add a preflight lane")
        self.counter.unlink(missing_ok=True)
        self.light_counter.unlink(missing_ok=True)

    def light(self):
        return len(self.light_counter.read_text()) if self.light_counter.exists() else 0

    def add_with(self, branch, **options):
        with patch.object(mq.Queue, "start"), patch("builtins.print"): return mq.add(self.repo, branch, **options)

    def ungated(self, branch):
        return self.add_with(branch, no_gate=True, why=self.WHY)

    def rounds(self, count=1):
        """count rounds of one worker, and what it said."""
        said, worker = [], mq.Worker(mq.Queue(self.repo))
        with patch.object(mq.Worker, "say", lambda _, words: said.append(words)):
            for _ in range(count): worker.round()
        return worker, said

    def tip_message(self):
        run(self.repo, "git", "fetch", "-q", "origin")
        return run(self.repo, "git", "log", "-1", "--format=%B", "origin/main")

    def queue_status(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out): mq.status(self.repo)
        return out.getvalue()

    def test_no_gate_needs_a_written_reason(self):
        self.commit("fix", "fix")
        for why in (None, "", "  "):
            with self.subTest(why=why), self.assertRaisesRegex(ValueError, "--no-gate needs a written reason"):
                self.add_with("fix", no_gate=True, why=why)
        with self.assertRaisesRegex(ValueError, "--why is the reason for --front or --no-gate"):
            self.add_with("fix", why=self.WHY)
        self.assertEqual(mq.Queue(self.repo).all(), [])
        # The control: with a reason it is queued, and the reason is kept on one line.
        identifier = self.add_with("fix", no_gate=True, why="a flaky test\n  fails the gate")
        self.assertEqual((self.state(identifier)["noGate"], self.state(identifier)["aheadWhy"]),
                         (True, "a flaky test fails the gate"))

    def test_both_options_work_with_no_setting_and_main_cannot_declare_one(self):
        # The fixture's main declares neither, and front needs no reason.
        self.commit("a", "a")
        identifier = self.add_with("a", front=True)
        self.assertEqual(self.state(identifier)["front"], True)
        self.assertNotIn("aheadWhy", self.state(identifier))
        for name in ("front", "noGate"):
            with self.subTest(name=name):
                (self.repo / "queuekeeper.json").write_text(json.dumps(
                    {**self.configured, "queue": {name: {"why": "a setting that no longer exists"}}}))
                with self.assertRaisesRegex(ValueError, "queue supports only"): self.config()
        schema = json.loads((ROOT / "schemas/queuekeeper.schema.json").read_text())["properties"]["queue"]["properties"]
        self.assertFalse({"front", "noGate"} & set(schema))

    def test_a_front_entry_is_tested_next_as_a_batch_of_its_own(self):
        self.commit("a", "a"); self.commit("b", "b"); self.commit("c", "c")
        a, b = self.add("a", "b")
        c = self.add_with("c", front=True, why="an important toolkit fix")
        _, said = self.rounds()
        self.assertEqual([self.state(i)["state"] for i in (a, b, c)], ["queued", "queued", "landed"])
        self.assertEqual(self.gates(), 1)
        self.assertTrue(any("at the front of the queue" in words for words in said), said)
        self.assertNotIn("ungated", self.state(c))
        # Those already queued are tested with the fix, without being queued again.
        self.rounds()
        self.assertEqual([self.state(i)["state"] for i in (a, b)], ["landed"] * 2)
        self.assertEqual(self.gates(), 2)
        log = run(self.repo, "git", "log", "--format=%s", "origin/main").splitlines()
        self.assertEqual(log[:3], ["Add b", "Add a", "Add c"])

    def test_without_front_the_same_entries_share_one_gate_in_order(self):
        self.commit("a", "a"); self.commit("b", "b"); self.commit("c", "c")
        ids = self.add("a", "b", "c")
        self.rounds()
        self.assertEqual([self.state(i)["state"] for i in ids], ["landed"] * 3)
        self.assertEqual(self.gates(), 1)
        self.assertEqual(run(self.repo, "git", "log", "--format=%s", "origin/main").splitlines()[:3],
                         ["Add c", "Add b", "Add a"])

    def test_front_again_moves_an_entry_already_queued(self):
        self.commit("a", "a"); self.commit("b", "b")
        a, b = self.add("a", "b")
        self.assertEqual(self.add_with("b", front=True, why="it unblocks the queue"), b)
        described = mq.describe(self.state(b))
        self.assertIn("is tested next", described)
        self.assertIn("because it unblocks the queue", described)
        self.rounds()
        self.assertEqual([self.state(i)["state"] for i in (a, b)], ["queued", "landed"])

    def test_a_front_entry_leaves_a_bisect_where_it_was(self):
        self.commit("a", "a"); self.commit("bad", "bad")
        self.add("a")
        identifier = self.add_with("bad", front=True)
        worker = mq.Worker(mq.Queue(self.repo))
        worker.limit, worker.suspects = 1, 2
        with patch.object(mq.Worker, "say"): worker.round()
        self.assertEqual(self.state(identifier)["state"], "failed")
        self.assertEqual((worker.limit, worker.suspects), (1, 2))

    def test_no_gate_lands_on_the_light_lanes_and_records_its_reason(self):
        tip = self.commit("fix", "fix")
        identifier = self.ungated("fix")
        _, said = self.rounds()
        entry = self.state(identifier)
        self.assertEqual(entry["state"], "landed")
        self.assertEqual((self.gates(), self.light()), (0, 1))
        self.assertEqual(entry["ungated"], {"why": self.WHY, "lanes": ["light"]})
        self.assertNotIn("gatedOn", entry)
        self.assertTrue(any("lands without the gate" in words and self.WHY in words
                            and "the next gate on main covers it" in words for words in said), said)
        # Main's history says so, with the reason, and the commit keeps its message, author and tree.
        message = self.tip_message()
        self.assertTrue(message.startswith("Add fix"), message)
        self.assertIn(f"{mq.UNGATED}: qk land --no-gate ran light", message)
        self.assertIn(f"because {self.WHY}", message)
        self.assertEqual(run(self.repo, "git", "log", "-1", "--format=%an %T", "origin/main"),
                         run(self.repo, "git", "log", "-1", "--format=%an %T", tip))
        self.assertIn(f"landed without the gate, on light, because {self.WHY}", mq.describe(entry))
        # The UNGATED amend keeps the author, date and subject by which the entry finds its own commit (#184).
        self.assertEqual(entry["landedAs"], run(self.repo, "git", "rev-parse", "origin/main"))
        self.assertEqual(entry["landedCommits"], 1)
        self.assertIn(f"ungated on main: {identifier}", self.queue_status())
        # A later gate on main covers it, and the status says no more of it.
        self.commit("later", "later")
        later = self.add_with("later")
        self.rounds()
        self.assertEqual(self.state(later)["state"], "landed")
        self.assertEqual(self.gates(), 1)
        self.assertNotIn(mq.UNGATED, self.tip_message())
        self.assertNotIn("ungated on main", self.queue_status())

    def test_a_project_with_no_preflight_lanes_lands_ungated_on_none(self):
        # A lane named spelling is no preflight lane unless queue.preflight lists it (vocaro/queuekeeper#69).
        config = json.loads(json.dumps(self.configured))
        config["lanes"]["spelling"] = config["lanes"].pop("light")
        del config["queue"]["preflight"]
        run(self.repo, "git", "checkout", "-q", "--detach", "origin/main")
        self.publish(config, "No preflight lane")
        self.counter.unlink(missing_ok=True)
        self.commit("fix", "fix")
        identifier = self.ungated("fix")
        _, said = self.rounds()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual((self.gates(), self.light()), (0, 0))
        self.assertEqual(self.state(identifier)["ungated"]["lanes"], [])
        self.assertTrue(any("running only no lane; the next gate on main covers it" in words for words in said), said)

    def test_an_ungated_landing_fails_on_its_preflight_lane(self):
        # The fixture's lane fails on a tree holding a file named bad.
        self.commit("bad", "bad")
        identifier = self.ungated("bad")
        self.rounds()
        self.assertEqual(self.state(identifier)["state"], "failed")
        # Once on the entry, and once more on origin/main alone, which passes it (#180).
        self.assertEqual((self.gates(), self.light()), (0, 2))
        self.assertNotIn("bad", self.main_files())

    def test_a_docs_only_landing_does_not_cover_an_ungated_one(self):
        config = json.loads(json.dumps(self.configured))
        config["queue"]["unread"] = {"paths": {"doc/": "no code lane reads it"}, "lanes": ["light"], "defaults": False}
        run(self.repo, "git", "checkout", "-q", "--detach", "origin/main")
        self.publish(config, "Declare the docs")
        self.commit("fix", "fix")
        identifier = self.ungated("fix")
        self.rounds()
        self.commit("doc/guide.md", "guide")
        self.add("guide")
        self.rounds()
        self.assertEqual(self.gates(), 0)
        self.assertIn(f"ungated on main: {identifier}", self.queue_status())

    def test_an_ungated_entry_waits_its_turn_and_lands_alone(self):
        self.commit("a", "a"); self.commit("b", "b"); self.commit("c", "c")
        [a] = self.add("a")
        b = self.ungated("b")
        [c] = self.add("c")
        self.rounds()
        self.assertEqual([self.state(i)["state"] for i in (a, b, c)], ["landed", "queued", "queued"])
        self.assertEqual(self.gates(), 1)
        self.rounds()
        self.assertEqual([self.state(i)["state"] for i in (b, c)], ["landed", "queued"])
        self.assertEqual(self.gates(), 1)
        self.assertIn("ungated", self.state(b))
        self.rounds()
        self.assertEqual(self.state(c)["state"], "landed")
        self.assertEqual(self.gates(), 2)

    def test_front_and_no_gate_together_land_at_once(self):
        self.commit("a", "a"); self.commit("fix", "fix")
        [a] = self.add("a")
        fix = self.add_with("fix", front=True, no_gate=True, why=self.WHY)
        self.rounds()
        self.assertEqual([self.state(i)["state"] for i in (a, fix)], ["queued", "landed"])
        self.assertEqual((self.gates(), self.light()), (0, 1))
        self.assertIn(mq.UNGATED, self.tip_message())

    def test_front_and_no_gate_together_go_ahead_of_an_older_front_entry(self):
        """The ungated entry needs no gate, so it does not wait for one an older --front entry runs
        (vocaro/queuekeeper#26)."""
        self.commit("first", "first"); self.commit("fix", "fix")
        first = self.add_with("first", front=True)
        fix = self.add_with("fix", front=True, no_gate=True, why=self.WHY)
        self.rounds()
        self.assertEqual([self.state(i)["state"] for i in (first, fix)], ["queued", "landed"])
        self.assertEqual(self.gates(), 0)
        self.rounds()
        self.assertEqual(self.state(first)["state"], "landed")

    def test_a_light_lane_that_fails_sends_it_back(self):
        self.light_status.write_text("1")
        self.commit("fix", "fix")
        identifier = self.ungated("fix")
        self.rounds()
        self.assertEqual(self.state(identifier)["state"], "failed")
        self.assertNotIn("fix", self.main_files())

    def test_the_pre_push_hook_still_runs_and_its_refusal_sends_back(self):
        self.ran = self.base / "hook-runs"
        PrePushHooks.hook(self, "exit 1")
        self.commit("fix", "fix")
        identifier = self.ungated("fix")
        with contextlib.redirect_stdout(io.StringIO()): self.work()
        self.assertEqual(len(PrePushHooks.runs(self)), 1)
        self.assertIn("pre-push hook refused", self.state(identifier)["why"])
        self.assertNotIn("fix", self.main_files())
        self.assertEqual(self.state(identifier)["state"], "failed")

    def test_the_options_reach_eng_land_and_eng_queue_add(self):
        import argparse
        for adder, words in ((mq.land_arguments, []), (mq.add_arguments, ["add"])):
            parser = argparse.ArgumentParser()
            adder(parser)
            asked = parser.parse_args([*words, "--front", "--no-gate", "--why", "a reason"])
            plain = parser.parse_args(words)
            self.assertEqual((asked.front, asked.no_gate, asked.why, plain.front, plain.no_gate, plain.why),
                             (True, True, "a reason", False, False, None))


class LaggingCheckout(Fixture):
    """The main checkout's queuekeeper.json is behind origin/main, as Pagepour's was (#96)."""
    def session(self, lagging, published=None):
        """Leave the main checkout on a commit with the configuration lagging, and in a session's worktree
        on branch fix a commit on top of origin/main, which holds the configuration published."""
        current = json.loads((self.repo / "queuekeeper.json").read_text())
        self.publish(lagging, "An older configuration")
        worktree = self.base / "session"
        run(self.repo, "git", "worktree", "add", "-q", "-b", "fix", str(worktree))
        (worktree / "queuekeeper.json").write_text(json.dumps(published or current))
        run(worktree, "git", "add", "queuekeeper.json")
        run(worktree, "git", "commit", "-q", "--allow-empty", "-m", "The newer configuration")
        run(worktree, "git", "push", "-q", "origin", "HEAD:main")
        (worktree / "fix").write_text("x")
        run(worktree, "git", "add", "fix")
        run(worktree, "git", "commit", "-qm", "Add fix")
        return worktree

    def land_from_another_project(self):
        other = self.base / "dependent"
        other.mkdir()
        (other / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "dependent"}))
        run(other, "git", "init", "-q")
        (other / "scripts").mkdir()
        shutil.copy(ROOT / "scripts/qk", other / "scripts/qk")
        return subprocess.run(["python3", "scripts/qk", "land", "--repo", "project", "fix", "--wait",
                               "--limit", str(stretched(120))], cwd=other, capture_output=True, text=True)

    def test_a_gate_lane_only_origin_main_has_lets_the_commit_in(self):
        self.session({"schemaVersion": 1, "name": "fixture"})
        done = self.land_from_another_project()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("fix", self.main_files())
        self.assertEqual(self.gates(), 1)

    def test_no_gate_lane_on_origin_main_is_refused_whatever_the_checkout_says(self):
        """The negative control: the checkout has a gate lane, and origin/main and the commit have none."""
        current = json.loads((self.repo / "queuekeeper.json").read_text())
        self.session(current, {"schemaVersion": 1, "name": "fixture"})
        done = self.land_from_another_project()
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("nothing to run", done.stderr)
        self.assertEqual(mq.Queue(self.repo).all(), [])
        self.assertNotIn("fix", self.main_files())

    def test_no_gate_lane_anywhere_is_refused(self):
        lanes = {"schemaVersion": 1, "name": "fixture"}
        self.session(lanes, lanes)
        done = self.land_from_another_project()
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("nothing to run", done.stderr)
        self.assertEqual(mq.Queue(self.repo).all(), [])

    def test_pull_request_on_origin_main_is_refused_whatever_the_checkout_says(self):
        current = json.loads((self.repo / "queuekeeper.json").read_text())
        self.session(current, {**current, "integration": "pull-request"})
        done = self.land_from_another_project()
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("pull request", done.stderr)
        self.assertEqual(mq.Queue(self.repo).all(), [])

    def test_the_worker_runs_no_lane_from_the_checkout(self):
        """The issue's open question: a gate that would fail, only in the main checkout, does not run."""
        current = json.loads((self.repo / "queuekeeper.json").read_text())
        # Either stale lane would fail, the checkout's preflight lane among them.
        failing_lanes = {lane: {"command": ["false"]} for lane in ("gate", "check")}
        self.session({**current, "lanes": failing_lanes, "queue": {**current["queue"], "preflight": ["check"]}}, current)
        done = self.land_from_another_project()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("fix", self.main_files())
        self.assertEqual(self.gates(), 1)

    def test_a_checkout_behind_the_commit_that_added_its_configuration_is_still_the_project(self):
        """--repo names the project when only origin/main holds queuekeeper.json, the main checkout being on a
        commit from before it, as StudyWright's was (#188)."""
        worktree = self.base / "session"
        run(self.repo, "git", "worktree", "add", "-q", "-b", "fix", str(worktree), "origin/main")
        (worktree / "fix").write_text("x")
        run(worktree, "git", "add", "fix")
        run(worktree, "git", "commit", "-qm", "Add fix")
        run(self.repo, "git", "checkout", "-q", "--orphan", "before")
        run(self.repo, "git", "rm", "-rqf", ".")
        run(self.repo, "git", "commit", "-q", "--allow-empty", "-m", "Before Queuekeeper")
        self.assertFalse((self.repo / "queuekeeper.json").exists())
        self.assertEqual(mq.repository(self.repo, str(self.repo)), self.repo.resolve())
        done = self.land_from_another_project()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("fix", self.main_files())

    def test_project_names_a_checkout_behind_the_commit_that_added_its_configuration(self):
        """--project takes such a checkout for the queue's commands too, which read origin/main (#204)."""
        worktree = self.base / "session"
        run(self.repo, "git", "worktree", "add", "-q", "-b", "fix", str(worktree), "origin/main")
        (worktree / "fix").write_text("x")
        run(worktree, "git", "add", "fix")
        run(worktree, "git", "commit", "-qm", "Add fix")
        run(self.repo, "git", "checkout", "-q", "--orphan", "before")
        run(self.repo, "git", "rm", "-rqf", ".")
        run(self.repo, "git", "commit", "-q", "--allow-empty", "-m", "Before Queuekeeper")
        self.assertFalse((self.repo / "queuekeeper.json").exists())
        project = ["python3", str(ROOT / "scripts/qk"), "--project", str(self.repo)]
        # qk queue doctor exits 1 on any finding. It reads this Mac's memory from host_isolation's recording, not from
        # the Mac, whose swap and memory pressure a gate's own load raises (vocaro/queuekeeper#215), so it is healthy.
        for words in (["queue", "status", "--json"], ["queue", "placement", "gate", "--json"],
                      ["queue", "events", "--json"], ["queue", "doctor", "--json"]):
            done = subprocess.run([*project, *words], cwd=self.base, capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, f"{words}: {done.stdout}{done.stderr}")
        self.assertTrue(json.loads(done.stdout)["healthy"], done.stdout)
        # qk queue health answers by its exit status whether the Macs are healthy, and it reads the Mac this suite
        # runs on: at a memory pressure of warning, which a gate's own load brings, it says `slow` and exits 1
        # (vocaro/queuekeeper#221). What is checked here is that it read the project and answered, not that Mac.
        done = subprocess.run([*project, "queue", "health", "--json"], cwd=self.base, capture_output=True, text=True)
        self.assertIn(done.returncode, (0, 1), f"queue health: {done.stdout}{done.stderr}")
        self.assertEqual(json.loads(done.stdout)["exitStatus"], done.returncode, done.stdout)
        self.assertIn("verdict", json.loads(done.stdout))
        # A command that runs what the working tree holds says why it cannot, not only that the file is missing.
        for words in (["run", "gate"], ["doctor"]):
            done = subprocess.run([*project, *words], cwd=self.base, capture_output=True, text=True)
            self.assertEqual(done.returncode, 2, f"{words}: {done.stdout}{done.stderr}")
            self.assertIn("the checkout is behind the commit that added it", done.stderr)
            self.assertNotIn("Traceback", done.stderr)
        done = subprocess.run([*project, "land", "fix", "--wait", "--json", "--limit", str(stretched(120))],
                              cwd=self.repo, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("fix", self.main_files())
        identifier = json.loads(done.stdout)["id"]
        for words in (["queue", "result", identifier, "--json"], ["queue", "wait", identifier, "--json"],
                      ["queue", "logs", identifier, "--json"]):
            done = subprocess.run([*project, *words], cwd=self.base, capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, f"{words}: {done.stdout}{done.stderr}")

    def test_a_checkout_too_old_for_this_toolkit_stops_no_worker(self):
        """The worker's --project is the main checkout, whose configuration must not even be checked."""
        worktree = self.session({"schemaVersion": 1, "name": "fixture", "hostedRepositories": []})
        with self.assertRaises(ValueError): configuration.load(self.repo)
        done = subprocess.run(["python3", "scripts/qk", "land", "fix", "--wait", "--limit", str(stretched(120))],
                              cwd=worktree, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("fix", self.main_files())


class AddOnlyJson(Fixture):
    land = CheckedWhenQueued.land
    PATH = "scripts/test-classes.json"
    START = '{"classes": {"old": "model"}, "version": 1}\n'

    def configure(self, path=None, keys=("classes",)):
        config = self.config()
        config["queue"]["addOnlyJson"] = {path or self.PATH: list(keys)}
        self.publish(config)
        return config

    def main_json(self):
        run(self.repo, "git", "fetch", "-q", "origin")
        return run(self.repo, "git", "show", f"origin/main:{self.PATH}")

    def variants(self, one, two, *, configured=True):
        if configured:
            self.configure()
        self.commit(self.PATH, "start", self.START)
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        for name, contents in (("one", one), ("two", two)):
            self.commit(self.PATH, name, contents)
        return self.add("one")[0]

    def test_trial_and_round_settle_distinct_new_keys_in_one_gate(self):
        first = self.variants(self.START.replace('"old"', '"new-one": "ui", "old"'),
                              self.START.replace('"old"', '"new-two": "model", "old"'))
        second, said, error = self.land("two")
        self.assertIsNone(error)
        self.assertIn(f"kept JSON key additions from both sides of {self.PATH}", said)
        self.assertEqual(self.work(), 0)
        self.assertEqual([self.state(entry)["state"] for entry in (first, second)], ["landed"] * 2)
        self.assertEqual(self.gates(), 1)
        self.assertEqual(json.loads(self.main_json()),
                         {"classes": {"old": "model", "new-one": "ui", "new-two": "model"}, "version": 1})
        self.assertIn(self.PATH, self.state(second)["kept"][0])

    def test_different_values_for_one_new_key_name_it_in_trial_and_send_back(self):
        first = self.variants(self.START.replace('"old"', '"new": "ui", "old"'),
                              self.START.replace('"old"', '"new": "model", "old"'))
        second, _, error = self.land("two")
        self.assertIsNone(second)
        self.assertIn(f'{self.PATH} (both sides add JSON key "new" with different values)', error)
        second, = self.add("two", checked=False)
        self.assertEqual(self.work(), 0)
        self.assertEqual(self.state(first)["state"], "landed")
        self.assertEqual(self.state(second)["state"], "conflict")
        self.assertIn('JSON key "new" with different values', self.state(second)["why"])

    def test_the_same_new_key_on_two_unrelated_lines_is_checked_after_git_merges(self):
        self.configure()
        start = """{
  "classes": {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4
  }
}
"""
        self.commit(self.PATH, "start", start)
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        self.commit(self.PATH, "one", start.replace('    "one"', '    "new": 5,\n    "one"'))
        self.commit(self.PATH, "dispute", start.replace('    "four"', '    "new": 6,\n    "four"'))
        self.commit(self.PATH, "same", start.replace('    "four"', '    "new": 5,\n    "four"'))
        first, = self.add("one")
        refused, _, error = self.land("dispute")
        self.assertIsNone(refused)
        self.assertIn(f"conflicts with {first}", error)
        self.assertIn('JSON key "new" with different values', error)
        second, _, error = self.land("same")
        self.assertIsNone(error)
        self.assertEqual(self.work(), 0)
        self.assertEqual([self.state(entry)["state"] for entry in (first, second)], ["landed"] * 2)
        self.assertEqual(self.main_json().count('"new": 5'), 1)

    def test_a_branch_cannot_declare_a_path_to_settle_its_own_conflict(self):
        first = self.variants(self.START.replace('"old"', '"new-one": "ui", "old"'),
                              self.START.replace('"old"', '"new-two": "model", "old"'), configured=False)
        config = self.config()
        config["queue"]["addOnlyJson"] = {self.PATH: ["classes"]}
        (self.repo / "queuekeeper.json").write_text(json.dumps(config))
        run(self.repo, "git", "add", "queuekeeper.json")
        run(self.repo, "git", "commit", "--amend", "--no-edit")
        refused, _, error = self.land("two")
        self.assertIsNone(refused)
        self.assertIn(f"conflicts with {first}", error)
        self.assertNotIn("JSON key", error)

    def test_additions_do_not_settle_edits_removals_or_malformed_objects(self):
        self.configure()
        self.commit(self.PATH, "start", self.START)
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        self.commit(self.PATH, "one", self.START.replace('"old"', '"new": "ui", "old"'))
        first, = self.add("one")
        for index, contents in enumerate((
                self.START.replace('"old": "model"', '"old": "edited"'),
                self.START.replace('"old": "model"', ''),
                self.START.replace('"old"', '"duplicate": 1, "duplicate": 2, "old"'),
                self.START.replace('"old"', '"invalid": Infinity, "old"'),
                self.START.replace('"version": 1', '"version": 2').replace('"old"', '"other": 0, "old"'),
                self.START.removesuffix('}\n'))):
            with self.subTest(contents=contents):
                name = f"branch-{index}"
                self.commit(self.PATH, name, contents)
                refused, _, error = self.land(name)
                self.assertIsNone(refused)
                self.assertIn(f"conflicts with {first}", error)

    def test_root_objects_keep_numbers_and_the_ahead_files_format(self):
        base = b'{"old":-1.0000000000000000001e-1000}\r\n'
        ahead = b'{"one":null,"old":-1.0000000000000000001e-1000}\r\n'
        edited = b'{"old":-1.0000000000000000002e-1000,"two":true}\r\n'
        incoming = b'{"old":-1.0000000000000000001e-1000,"two":0.10000000000000001}\r\n'
        self.assertEqual(mq.add_only_json.merge(base, ahead, edited, []), (None, None))
        self.assertEqual(mq.add_only_json.merge(base, ahead, incoming, []),
                         (b'{"one":null,"old":-1.0000000000000000001e-1000,"two":0.10000000000000001}\r\n', None))

    def test_setting_paths_and_object_keys_agree_with_the_schema(self):
        schema = json.loads((ROOT / "schemas/queuekeeper.schema.json").read_text())
        setting = schema["properties"]["queue"]["properties"]["addOnlyJson"]
        self.assertEqual(setting["propertyNames"]["pattern"], "^" + configuration.ADD_ONLY_JSON_PATH.pattern + "$")
        self.assertEqual(setting["additionalProperties"]["items"]["type"], "string")
        config = self.configure(keys=())
        self.assertEqual(configuration.parse(json.dumps(config))["queue"]["addOnlyJson"], {self.PATH: []})
        for added in ([], {"../test.json": []}, {"folder//test.json": []}, {"/tmp/test.json": []},
                      {"folder/./test.json": []}, {"test": []}, {"test.json": "classes"}, {"test.json": [1]}):
            config["queue"]["addOnlyJson"] = added
            with self.subTest(added=added), self.assertRaisesRegex(ValueError, "queue.addOnlyJson"):
                configuration.parse(json.dumps(config))


class Changelogs(Fixture):
    """Branches that each add a line at the top of a section conflict in turn (#101)."""
    START = "# Release notes\n\n## Unreleased\n\n- old\n\n## 1.0\n\n- first\n"

    def setUp(self):
        super().setUp()
        self.commit("CHANGELOG.md", "start", self.START)
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")

    def changelog(self):
        run(self.repo, "git", "fetch", "-q", "origin")
        return run(self.repo, "git", "show", "origin/main:CHANGELOG.md") + "\n"

    def top(self, line):
        return self.START.replace("- old\n", f"{line}\n- old\n", 1)

    def outcome(self, *contents, name="CHANGELOG.md"):
        """Land each content of name, on a branch of its own from the same commit, oldest first. Each is queued
        unchecked, so that the round decides, and qk land's trial of each against those ahead of it must agree
        with the round: refused just where the round sends it back (vocaro-engineering#178)."""
        for number, content in enumerate(contents): self.commit(name, f"b{number}", content)
        ids = self.add(*(f"b{number}" for number in range(len(contents))), checked=False)
        queue = mq.Queue(self.repo)
        entries, base = queue.all(), run(self.repo, "git", "rev-parse", "origin/main")
        refused = [mq.trial(queue, entry, entries[:index], base)["why"] is not None
                   for index, entry in enumerate(entries)]
        self.work()
        states = [self.state(identifier)["state"] for identifier in ids]
        self.assertEqual(refused, [state == "conflict" for state in states], states)
        return states

    def test_each_branch_lands_with_its_line_first_after_one_docs_pass(self):
        self.assertEqual(self.outcome(self.top("- a"), self.top("- b"), self.top("- c")), ["landed"] * 3)
        self.assertEqual(self.changelog(), self.START.replace("- old\n", "- c\n- b\n- a\n- old\n", 1))
        self.assertEqual(self.gates(), 0)
        self.assertEqual(len(list(mq.Queue(self.repo).logs.glob("*-docs.log"))), 1)
        self.assertEqual([entry["docsOnly"] for entry in mq.Queue(self.repo).all()],
                         [["docs"]] * 3)
        self.assertIsNone(mq.Queue(self.repo).held())
        # The first went in as it was; each settled one adds its very line beside other neighbors, which
        # Closeout's landed.sh proves resolved, so its branch goes unasked (#164).
        contained = lambda branch: mq.landing_proofs.contained(lambda *words: mq.git(self.repo, *words), "origin/main", branch)
        self.assertNotIn(contained("b0"), (None, mq.landing_proofs.RESOLVED))
        self.assertEqual([contained("b1"), contained("b2")], [mq.landing_proofs.RESOLVED] * 2)

    def test_a_settled_commit_keeps_its_message_and_author(self):
        for name, start in (("b0", self.top("- a")), ("b1", self.top("- b"))):
            self.commit("CHANGELOG.md", name, start)
        run(self.repo, "git", "commit", "-q", "--amend", "--author", "Other <other@example.com>", "-m",
            "Say b\n\n# Not a comment\n\nCloses #1.")
        self.add("b0", "b1")
        self.work()
        run(self.repo, "git", "fetch", "-q", "origin")
        self.assertEqual(run(self.repo, "git", "log", "-1", "--format=%an|%B", "origin/main"),
                         "Other|Say b\n\n# Not a comment\n\nCloses #1.")

    def test_a_branch_of_two_commits_settles_each(self):
        self.commit("CHANGELOG.md", "b0", self.top("- a"))
        self.commit("CHANGELOG.md", "b1", self.top("- b"))
        (self.repo / "other").write_text("x")
        run(self.repo, "git", "add", "other")
        run(self.repo, "git", "commit", "-qm", "Add other")
        (self.repo / "CHANGELOG.md").write_text(self.top("- c\n- b"))
        run(self.repo, "git", "add", "CHANGELOG.md")
        run(self.repo, "git", "commit", "-qm", "Add c")
        ids = self.add("b0", "b1")
        self.work()
        self.assertEqual([self.state(i)["state"] for i in ids], ["landed"] * 2)
        self.assertEqual(self.changelog(), self.START.replace("- old\n", "- c\n- b\n- a\n- old\n", 1))

    def test_a_branch_that_rewords_its_own_line_lands_once(self):
        """EPUBLib's #938: its second commit changed the line its first added, which commit by commit read as
        a change to a line ahead of it (#130)."""
        self.commit("CHANGELOG.md", "b0", self.top("- a"))
        self.commit("CHANGELOG.md", "b1", self.top("- b"))
        (self.repo / "CHANGELOG.md").write_text(self.top("- b, reworded"))
        run(self.repo, "git", "commit", "-qam", "Reword b")
        ids = self.add("b0", "b1")
        self.work()
        self.assertEqual([self.state(i)["state"] for i in ids], ["landed"] * 2)
        self.assertEqual(self.changelog(), self.START.replace("- old\n", "- b, reworded\n- a\n- old\n", 1))
        self.assertNotIn("kept", self.state(ids[0]))
        kept = self.state(ids[1])["kept"]
        self.assertEqual(len(kept), 2, kept)
        self.assertTrue(all("kept both sides of CHANGELOG.md" in line for line in kept), kept)
        self.assertIn("kept both sides of CHANGELOG.md", mq.describe(self.state(ids[1])))

    def test_a_branch_that_later_rewords_a_line_ahead_of_it_comes_back(self):
        self.commit("CHANGELOG.md", "b0", self.top("- a"))
        self.commit("CHANGELOG.md", "b1", self.top("- b"))
        (self.repo / "CHANGELOG.md").write_text(self.top("- b").replace("- old", "- old, reworded"))
        run(self.repo, "git", "commit", "-qam", "Reword old")
        ids = self.add("b0", "b1", checked=False)
        self.work()
        self.assertEqual([self.state(i)["state"] for i in ids], ["landed", "conflict"])
        self.assertEqual(self.changelog(), self.top("- a"))

    def test_a_section_with_no_entries_yet(self):
        start = "# Release notes\n\n## Unreleased\n\n## 1.0\n\n- first\n"
        self.commit("CHANGELOG.md", "empty", start)
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        self.assertEqual(self.outcome(*(start.replace("## 1.0", f"{line}\n\n## 1.0") for line in ("- a", "- b"))),
                         ["landed"] * 2)
        self.assertEqual(self.changelog(), start.replace("## 1.0", "- b\n- a\n\n## 1.0"))

    def test_a_real_conflict_in_the_changelog_comes_back(self):
        states = self.outcome(self.START.replace("- old", "- one"), self.START.replace("- old", "- two"))
        self.assertEqual(states, ["landed", "conflict"])
        self.assertEqual(self.changelog(), self.START.replace("- old", "- one"))

    def test_a_real_conflict_beside_an_added_line_comes_back_whole(self):
        ahead = self.top("- a").replace("- first", "- one")
        states = self.outcome(ahead, self.top("- b").replace("- first", "- two"))
        self.assertEqual(states, ["landed", "conflict"])
        self.assertEqual(self.changelog(), ahead)

    def test_entries_added_below_an_entry_land_with_the_new_first(self):
        """EPUBLib's #706: the entry ahead had put its line second, and so had the branch (#130)."""
        start = self.START.replace("- old\n", "- old\n- older\n", 1)
        self.commit("CHANGELOG.md", "longer", start)
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        states = self.outcome(*(start.replace("- older", f"{line}\n- older") for line in ("- a", "- b")))
        self.assertEqual(states, ["landed"] * 2)
        self.assertEqual(self.changelog(), start.replace("- older", "- b\n- a\n- older"))

    def test_lines_added_below_what_is_no_entry_come_back(self):
        start = self.START.replace("- old\n", "Entries since 1.0.\n- old\n", 1)
        self.commit("CHANGELOG.md", "intro", start)
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        states = self.outcome(*(start.replace("- old", f"{line}\n- old") for line in ("- a", "- b")))
        self.assertEqual(states, ["landed", "conflict"])

    def test_lines_that_start_no_entry_below_an_entry_come_back(self):
        """Keeping both would join the commit's wrapped line to the entry above it."""
        start = self.START.replace("- old\n", "- old\n- older\n", 1)
        self.commit("CHANGELOG.md", "longer", start)
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        states = self.outcome(start.replace("- older", "- a\n- older"), start.replace("- older", "  more of old\n- older"))
        self.assertEqual(states, ["landed", "conflict"])

    def test_a_new_section_ahead_comes_back(self):
        """Keeping both would put the line in the section just released."""
        states = self.outcome(self.START.replace("- old", "## 1.1\n\n- old"), self.top("- b"))
        self.assertEqual(states, ["landed", "conflict"])

    def test_the_same_shape_in_another_file_comes_back(self):
        # The ordinary conflict is decided by Git, and the fixture gate runs on the survivor.
        self.commit("NOTES.md", "notes", self.START)
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        states = self.outcome(self.top("- a"), self.top("- b"), name="NOTES.md")
        self.assertEqual(states, ["landed", "conflict"])
        self.assertEqual(self.gates(), 1)

    def test_a_changelog_conflict_beside_another_files_comes_back_whole(self):
        self.commit("same", "same", "start")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        for number, line in enumerate(("- a", "- b")):
            self.commit("CHANGELOG.md", f"b{number}", self.top(line))
            (self.repo / "same").write_text(line)
            run(self.repo, "git", "add", "same")
            run(self.repo, "git", "commit", "-q", "--amend", "--no-edit")
        ids = self.add("b0", "b1", checked=False)
        self.work()
        self.assertEqual([self.state(i)["state"] for i in ids], ["landed", "conflict"])
        self.assertIn("in CHANGELOG.md, same:", self.state(ids[1])["why"])
        self.assertEqual(self.changelog(), self.top("- a"))

    def test_a_line_on_both_sides_is_not_doubled(self):
        states = self.outcome(self.top("- shared\n- a"), self.top("- shared\n- b"))
        self.assertEqual(states, ["landed", "conflict"])
        self.assertEqual(self.changelog().count("- shared"), 1)

    def test_a_line_main_has_elsewhere_is_not_doubled(self):
        states = self.outcome(self.top("- a"), self.top("- first"))
        self.assertEqual(states, ["landed", "conflict"])
        self.assertEqual(self.changelog().count("- first"), 1)

    def test_a_line_written_twice_in_the_commit_is_not_doubled(self):
        states = self.outcome(self.top("- a"), self.top("- b\n- b"))
        self.assertEqual(states, ["landed", "conflict"])
        self.assertNotIn("- b", self.changelog())

    def test_a_branch_landed_already_is_not_doubled(self):
        self.assertEqual(self.outcome(self.top("- a"), self.top("- b")), ["landed"] * 2)
        run(self.repo, "git", "checkout", "-q", "-B", "again", "origin/main~2")
        (self.repo / "CHANGELOG.md").write_text(self.top("- a"))
        run(self.repo, "git", "add", "CHANGELOG.md")
        run(self.repo, "git", "commit", "-qm", "Add a again")
        self.add("again")
        self.work()
        self.assertEqual(self.changelog().count("- a\n"), 1)

    def test_line_endings_stay_as_they_are(self):
        start, ahead, new = (text.encode().replace(b"\n", b"\r\n") for text in
                             (self.START, self.top("- a"), self.top("- b")))
        self.assertEqual(mq.keep_both(start, ahead, new), self.START.replace("- old\n", "- b\n- a\n- old\n", 1)
                         .encode().replace(b"\n", b"\r\n"))

    def test_keep_both_needs_a_conflict(self):
        start = self.START.encode()
        self.assertIsNone(mq.keep_both(start, start, self.top("- a").encode()))


class LanePolicy(Fixture):
    """A lane in the fixture reads the policy from a local origin: no GitHub, and no write to the real checkout (#298)."""
    def traced(self, *words, **variables):
        """What Git says it ran for words, a command in the fixture's project, with variables in its environment."""
        trace = self.base / "command.trace"
        environment = {key: value for key, value in {**os.environ, **variables}.items() if value is not None}
        subprocess.run(words, cwd=self.repo, capture_output=True, stdin=subprocess.DEVNULL, timeout=stretched(60),
                       env={**environment, "GIT_TRACE": str(trace), "GIT_TERMINAL_PROMPT": "0"})
        return trace.read_text()

    def test_the_trace_shows_a_fetch_over_https_and_the_fixture_refuses_one(self):
        """The negative controls, at a port on this Mac that nothing answers, so that neither needs the network: what
        the lane's test looks for is what Git prints for HTTPS, and the fixture's own Git never gets that far."""
        fetch = ("git", "ls-remote", "https://127.0.0.1:1/toolkit.git")
        self.assertIn("remote-http", self.traced(*fetch, GIT_ALLOW_PROTOCOL=None))
        (self.base / "command.trace").unlink()
        self.assertNotIn("remote-http", self.traced(*fetch))

    def test_the_lanes_toolkit_is_a_clean_copy_whose_origin_is_local(self):
        kit, origin = local_toolkit()
        self.assertEqual(os.environ["QUEUEKEEPER_ROOT"], str(kit))
        self.assertNotEqual(kit.resolve(), ROOT.resolve())
        self.assertEqual(run(kit, "git", "remote", "get-url", "origin"), str(origin))
        self.assertEqual(run(kit, "git", "rev-parse", "origin/main"), run(kit, "git", "rev-parse", "HEAD"))
        self.assertEqual(cli.source_identity(kit)["workingChangesSHA256"], mq.remote.CLEAN)


class ToolkitCopy(Fixture):
    """Which toolkit a project's gate runs, with a toolkit repository of its own in the fixture."""
    def setUp(self):
        super().setUp()
        self.seen = self.base / "seen"
        (self.repo / "report.py").write_text(
            "import os, pathlib, subprocess, sys\n"
            "root = os.environ.get('QUEUEKEEPER_ROOT', '')\n"
            "head = subprocess.run(['git', '-C', root, 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip() if root else ''\n"
            "dirty = subprocess.run(['git', '-C', root, 'status', '--porcelain'], capture_output=True, text=True).stdout.strip() if root else ''\n"
            "pathlib.Path(sys.argv[1]).write_text('\\n'.join([root, head, dirty]))\n")
        config = json.loads((self.repo / "queuekeeper.json").read_text())
        config["lanes"]["gate"]["command"] = ["python3", "report.py", str(self.seen)]
        run(self.repo, "git", "checkout", "-q", "-B", "start", "origin/main")
        run(self.repo, "git", "add", "report.py")
        self.publish(config, "Report the toolkit")
        self.kit_origin = self.base / "toolkit.git"
        run(self.base, "git", "init", "-q", "--bare", "-b", "main", str(self.kit_origin))
        self.kit = self.base / "toolkit"
        run(self.base, "git", "clone", "-q", str(self.kit_origin), str(self.kit))
        shutil.copytree(ROOT / "src/python", self.kit / "src/python", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy(ROOT / ".gitignore", self.kit)
        run(self.kit, "git", "add", ".")
        run(self.kit, "git", "commit", "-qm", "Toolkit")
        run(self.kit, "git", "push", "-q", "origin", "HEAD:main")
        # This Mac's queue folder, laid out as another Mac's checkouts are (vocaro-engineering#104).
        self.checkout = self.base / "queuekeeper-workers/project/project"
        self.copy = self.base / "queuekeeper-workers/project/toolkit"
        variables = patch.dict(os.environ)
        variables.start()
        self.addCleanup(variables.stop)
        del os.environ["QUEUEKEEPER_ROOT"]
        # Let subprocesses write bytecode even if the calling shell turns it off. Apple's Python 3.9
        # defaults to a central cache rather than an adjacent __pycache__; keep both interpreters'
        # caches inside this fixture and check the copied checkout's path below (#368).
        os.environ.pop("PYTHONDONTWRITEBYTECODE", None)
        self.bytecode = self.base / "bytecode"
        os.environ["PYTHONPYCACHEPREFIX"] = str(self.bytecode)
        toolkit = patch.object(mq, "TOOLKIT_ROOT", self.kit)
        toolkit.start()
        self.addCleanup(toolkit.stop)
        # The worker's move to the copy, through its seam rather than a real exec, which would replace this process.
        self.moves = []
        def execute(command, env): raise Replaced(command, env)
        for patcher in (patch.object(mq, "execute", execute), patch.object(mq, "load", mq.load)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def work(self):
        """The worker, then the one that replaces it when it moves to the copy: a process of its own in use
        (merge_queue.execute), here the same module started again with the copy as its toolkit, the copy's config
        module judging main, and the environment the move carried."""
        while True:
            try: return mq.run(self.repo)
            except Replaced as moved:
                self.moves.append(moved)
                self.assertEqual(moved.command, mq.Queue(self.repo).command(self.copy))
                self.assertNotIn("QUEUEKEEPER_ROOT", moved.env)
                mq.TOOLKIT_ROOT = self.copy
                mq.load = self.code_of(self.copy).load
                os.environ[mq.PROGRESS_VARIABLE] = moved.env[mq.PROGRESS_VARIABLE]
                os.environ[mq.LOCK_VARIABLE] = moved.env[mq.LOCK_VARIABLE]

    def code_of(self, checkout):
        """A toolkit checkout's config module, loaded apart from this process's, as one of the queuekeeper package,
        whose hook points it reads (plugins.LANE_KEYS)."""
        source = checkout / "src/python/queuekeeper/config.py"
        spec = importlib.util.spec_from_file_location(f"queuekeeper.config_of_{len(self.moves)}", source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def advance_origin(self, name):
        """A landed toolkit commit the shared checkout has not pulled."""
        other = self.base / f"other-{name}"
        run(self.base, "git", "clone", "-q", str(self.kit_origin), str(other))
        (other / name).write_text("x")
        run(other, "git", "add", name)
        run(other, "git", "commit", "-qm", f"Add {name}")
        run(other, "git", "push", "-q", "origin", "HEAD:main")
        return run(other, "git", "rev-parse", "HEAD")

    def gate_once(self, name):
        self.commit(name, name)
        [identifier] = self.add(name)
        status = self.work()
        return identifier, status

    def test_a_gate_runs_the_toolkit_origin_has_not_the_lagging_shared_checkout(self):
        behind = run(self.kit, "git", "rev-parse", "HEAD")
        landed = self.advance_origin("landed")
        with contextlib.redirect_stdout(io.StringIO()) as said: identifier, status = self.gate_once("a")
        self.assertEqual(status, 0)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(self.seen.read_text().split("\n"), [str(self.copy), landed, ""])
        cache = self.bytecode / self.copy.relative_to(self.copy.anchor) / "src/python/queuekeeper"
        self.assertTrue(any(cache.glob("cli.*.pyc")))
        self.assertEqual(run(self.kit, "git", "rev-parse", "HEAD"), behind)
        # And so does the worker itself, once, from the copy (vocaro-engineering#173).
        self.assertEqual(len(self.moves), 1)
        self.assertIn(f"moving to the toolkit at {landed[:12]}, origin/main, from {behind[:12]}, which this worker was "
                      "started from: the next worker is started from the copy", said.getvalue())
        self.assertEqual(json.loads(self.moves[0].env[mq.PROGRESS_VARIABLE]), {"limit": None, "suspects": 0, "no_verdicts": 0})
        self.assertNotIn(mq.PROGRESS_VARIABLE, os.environ)

    def test_the_lock_is_never_free_between_the_worker_and_the_one_that_replaces_it(self):
        """A session that looked for a worker while the moved one's Python started found none, started its own from
        the shared checkout, and that one took the lock: the moved worker exited without a word, and the other read
        the move as one that had stopped (EPUBLib's and Pagepour's, 2026-11-15, vocaro/queuekeeper#158)."""
        self.advance_origin("landed")
        self.commit("a", "a")
        [identifier] = self.add("a")
        queue = mq.Queue(self.repo)
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(Replaced) as moved: mq.run(self.repo)
            kept = int(moved.exception.env[mq.LOCK_VARIABLE])
            self.assertTrue(os.get_inheritable(kept))
            # Between the two: another session finds a worker, and one it starts all the same is no worker.
            self.assertTrue(queue.working())
            self.assertEqual(mq.run(self.repo), 0)
            self.assertEqual(self.state(identifier)["state"], "queued")
            # The one that replaces it is the worker, by the lock it inherits, and lands the entry.
            mq.TOOLKIT_ROOT = self.copy
            mq.load = self.code_of(self.copy).load
            os.environ.update({name: moved.exception.env[name] for name in (mq.PROGRESS_VARIABLE, mq.LOCK_VARIABLE)})
            self.assertEqual(mq.run(self.repo), 0)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertNotIn(mq.LOCK_VARIABLE, os.environ)
        self.assertFalse(queue.working())
        with self.assertRaises(OSError): os.fstat(kept)

    def test_a_worker_started_from_the_copy_stays(self):
        """The negative control: the toolkit at origin is the code the worker runs, so it does not move."""
        self.gate_once("a")
        self.assertEqual(self.moves, [])
        mq.TOOLKIT_ROOT = self.copy
        with contextlib.redirect_stdout(io.StringIO()) as said: identifier, status = self.gate_once("b")
        self.assertEqual((status, self.state(identifier)["state"], self.moves), (0, "landed", []))
        self.assertNotIn("moving to the toolkit", said.getvalue())
        self.assertEqual(self.seen.read_text().split("\n")[0], str(self.copy))

    def knowing(self, key):
        """A landed toolkit commit whose config module accepts the lane key that this toolkit's own refuses."""
        other = self.base / f"other-knows-{key}"
        run(self.base, "git", "clone", "-q", str(self.kit_origin), str(other))
        source = other / "src/python/queuekeeper/config.py"
        edited = accepting_lane_key(source.read_text(), key)
        source.write_text(edited)
        run(other, "git", "add", "src/python/queuekeeper/config.py")
        run(other, "git", "commit", "-qm", f"Accept {key}")
        run(other, "git", "push", "-q", "origin", "HEAD:main")
        return run(other, "git", "rev-parse", "HEAD")

    def set_on_main(self, key):
        """main's gate lane with the key set, published after the entry was added, as EPUBLib's 41f46e94 landed it."""
        config = json.loads((self.repo / "queuekeeper.json").read_text())
        config["lanes"]["gate"][key] = True
        run(self.repo, "git", "checkout", "-q", "-B", "start", "origin/main")
        self.publish(config, f"Set {key} on the gate lane")
        return config

    def test_the_worker_judges_main_with_the_toolkit_at_origin_not_its_own_older_code(self):
        """EPUBLib's hold of 2026-11-04: main's gate lane set a key the copy accepted and the worker's own config
        module, from an older checkout, refused (vocaro-engineering#173)."""
        self.commit("a", "a")
        [identifier] = self.add("a")
        config = self.set_on_main("newKey")
        behind = run(self.kit, "git", "rev-parse", "HEAD")
        landed = self.knowing("newKey")
        # The control: the code this worker is started from refuses main.
        with self.assertRaisesRegex(ValueError, "Invalid lane gate"): configuration.parse(json.dumps(config))
        with contextlib.redirect_stdout(io.StringIO()) as said: status = self.work()
        self.assertEqual(status, 0)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertIn("a", self.main_files())
        self.assertIsNone(mq.Queue(self.repo).held())
        self.assertEqual(len(self.moves), 1)
        self.assertIn(f"moving to the toolkit at {landed[:12]}, origin/main, from {behind[:12]}", said.getvalue())
        self.assertEqual(self.seen.read_text().split("\n"), [str(self.copy), landed, ""])
        self.assertEqual(self.code_of(self.copy).parse(json.dumps(config))["lanes"]["gate"]["newKey"], True)
        self.assertEqual(run(self.kit, "git", "rev-parse", "HEAD"), behind)

    def test_main_that_the_newest_toolkit_refuses_holds_for_a_session_naming_the_toolkit(self):
        """The negative control: with nothing newer at origin, the worker stays, and the hold is a session's, since
        a session lands the fix, in the toolkit or on main; it never waits for the owner."""
        self.commit("a", "a")
        [identifier] = self.add("a")
        self.set_on_main("newKey")
        code = run(self.kit, "git", "rev-parse", "HEAD")
        with patch("builtins.print"): status = self.work()
        self.assertEqual(status, 1)
        self.assertEqual(self.moves, [])
        self.assertEqual(self.state(identifier)["state"], "queued")
        self.assertFalse(self.seen.exists())
        held = mq.Queue(self.repo).held()
        base = run(self.repo, "git", "rev-parse", "origin/main")
        self.assertEqual(held, f"for a session, not the owner: Invalid lane gate, judging main at {base[:12]} with the "
                               f"toolkit at {code[:12]}; the session that fixes it, or any session that finds it gone, "
                               "runs `qk queue resume` itself and says so")

    def test_a_bisect_goes_on_from_where_the_worker_that_moved_left_it(self):
        """Two entries that fail together, the toolkit landing between the rounds: the worker that takes over
        tests the oldest alone, as the one it replaces was about to, costing no gate."""
        config = json.loads((self.repo / "queuekeeper.json").read_text())
        config["lanes"]["gate"]["command"] = ["python3", "lane.py", str(self.counter), str(self.status)]
        # Kept on this Mac, so that the bisect gates one half at a time, which the count below follows, rather than its
        # halves at once (Beside): every lane is admitted and offerable since the owner's decision of 2026-11-07.
        config["lanes"]["gate"]["here"] = "the test counts its gates one after another"
        run(self.repo, "git", "checkout", "-q", "-B", "start", "origin/main")
        self.publish(config, "Fail on bad files")
        self.commit("a", "a"); self.commit("bad", "bad")
        ids = self.add("a", "bad")
        real = mq.Worker.gate
        def gate(worker, *arguments, **keywords):
            result = real(worker, *arguments, **keywords)
            if self.gates() == 1: self.advance_origin("between")
            return result
        with patch.object(mq.Worker, "gate", gate), patch("builtins.print"): self.assertEqual(self.work(), 0)
        self.assertEqual([self.state(i)["state"] for i in ids], ["landed", "failed"])
        self.assertEqual(len(self.moves), 1)
        self.assertEqual(json.loads(self.moves[0].env[mq.PROGRESS_VARIABLE]), {"limit": 1, "suspects": 2, "no_verdicts": 0})
        # Both together, then a alone, then bad alone, and bad on the main before a landed, where it fails too
        # (vocaro/queuekeeper#263): not both together again.
        self.assertEqual(self.gates(), 4)

    def test_a_dirty_shared_checkout_neither_reaches_a_gate_nor_is_touched(self):
        edited = self.kit / "src/python/queuekeeper/cli.py"
        edited.write_text(edited.read_text() + "\nraise SystemExit(1)  # a session's edit\n")
        (self.kit / "untracked").write_text("x")
        identifier, status = self.gate_once("a")
        self.assertEqual(status, 0)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(self.seen.read_text().split("\n")[2], "")
        self.assertIn("a session's edit", edited.read_text())
        self.assertTrue((self.kit / "untracked").exists())

    def test_the_copy_follows_origin_and_loses_what_was_left_in_it(self):
        self.gate_once("a")
        (self.copy / "left").write_text("x")
        (self.copy / "src/python/queuekeeper/project.py").write_text("raise SystemExit(1)\n")
        landed = self.advance_origin("later")
        self.gate_once("b")
        self.assertEqual(self.seen.read_text().split("\n"), [str(self.copy), landed, ""])

    def test_a_toolkit_that_cannot_be_fetched_holds_and_blames_nobody(self):
        run(self.kit, "git", "remote", "set-url", "origin", str(self.base / "gone.git"))
        identifier, status = self.gate_once("a")
        self.assertEqual(status, 1)
        self.assertEqual(self.state(identifier)["state"], "queued")
        self.assertIn("qk queue resume", mq.Queue(self.repo).held())
        self.assertFalse(self.seen.exists())

    def test_something_at_the_copys_path_holds_and_is_left_alone(self):
        self.copy.mkdir(parents=True)
        (self.copy / "kept").write_text("x")
        identifier, status = self.gate_once("a")
        self.assertEqual(status, 1)
        self.assertEqual(self.state(identifier)["state"], "queued")
        self.assertTrue((self.copy / "kept").exists())

    def test_the_checkouts_relative_path_to_the_toolkit_finds_the_queues_copy(self):
        # Pagepour's and studywright's ../vocaro-engineering, here ../toolkit, as a lane reads it.
        config = json.loads((self.repo / "queuekeeper.json").read_text())
        config["lanes"]["gate"]["command"] = ["python3", "-c", "import os, pathlib, sys; "
                                              "pathlib.Path(sys.argv[1]).write_text(os.path.realpath('../toolkit'))",
                                              str(self.seen)]
        run(self.repo, "git", "checkout", "-q", "-B", "start", "origin/main")
        self.publish(config, "Read the toolkit by its relative path")
        identifier, status = self.gate_once("a")
        self.assertEqual(status, 0)
        self.assertEqual(self.seen.read_text(), str(self.copy))
        listed = run(self.repo, "git", "worktree", "list", "--porcelain")
        self.assertIn(f"worktree {self.checkout}\n", listed)
        self.assertIn(f"worktree {self.copy}\n", run(self.kit, "git", "worktree", "list", "--porcelain"))
        # Nothing beside the main checkout, where the old layout kept them.
        self.assertFalse((self.base / "project-queue").exists())
        self.assertFalse((self.base / "project-queue-toolkit").exists())

    def old_layout(self):
        """The checkouts an earlier worker kept beside the main checkout, each locked as it locked them,
        the project's with an input a lane provisioned."""
        old, old_kit = self.base / "project-queue", self.base / "project-queue-toolkit"
        run(self.repo, "git", "worktree", "add", "-q", "--detach", "--lock", "--reason", "the merge queue's checkout",
            str(old), "origin/main")
        run(self.kit, "git", "worktree", "add", "-q", "--detach", "--lock", "--reason",
            "the merge queue's copy of the toolkit", str(old_kit), "origin/main")
        info = Path(run(old, "git", "rev-parse", "--path-format=absolute", "--git-common-dir")) / "info/exclude"
        info.parent.mkdir(exist_ok=True)
        info.write_text("provisioned/\n")
        (old / "provisioned").mkdir()
        (old / "provisioned/input").write_text("made by a provision lane\n")
        return old, old_kit

    def locks(self, repository):
        found, path = {}, None
        for line in run(repository, "git", "worktree", "list", "--porcelain").splitlines():
            if line.startswith("worktree "): path = line[len("worktree "):]
            elif line.startswith("locked"): found[path] = line[len("locked "):]
        return found

    def test_the_old_layout_moves_once_nothing_uses_it_and_keeps_what_was_provisioned(self):
        old, old_kit = self.old_layout()
        with patch.object(mq.disk, "census", return_value=[]):
            identifier, status = self.gate_once("a")
        self.assertEqual(status, 0)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertFalse(old.exists())
        self.assertFalse(old_kit.exists())
        self.assertEqual((self.checkout / "provisioned/input").read_text(), "made by a provision lane\n")
        self.assertEqual(self.seen.read_text().split("\n")[0], str(self.copy))
        # Still locked, with the reasons they had, and Git knows each only at its new place.
        self.assertEqual(self.locks(self.repo), {str(self.checkout): "the merge queue's checkout"})
        self.assertEqual(self.locks(self.kit), {str(self.copy): "the merge queue's copy of the toolkit"})
        run(self.checkout, "git", "status", "--porcelain")
        run(self.copy, "git", "status", "--porcelain")

    def test_a_moved_checkout_forgets_the_package_state_that_names_its_old_path(self):
        # As studywright's suite-root failed on 2026-11-02: its moved .build named ~/Development/studywright-queue (#114).
        old, _old_kit = self.old_layout()
        info = Path(run(old, "git", "rev-parse", "--path-format=absolute", "--git-common-dir")) / "info/exclude"
        info.write_text(info.read_text() + ".build/\n")
        stale = {".build": "SwiftPM's", "app/.build/check-app/SourcePackages": "Xcode's",
                 ".build/DerivedData/ios-tests/SourcePackages": "Xcode's, in DerivedData"}
        for folder in stale:
            (old / folder).mkdir(parents=True)
            (old / folder / "workspace-state.json").write_text(json.dumps({"path": str(old / folder / "checkouts")}))
        for name in ("artifacts/bundle/info.json", "checkouts/tokenizers/Package.swift", "repositories/tokenizers/HEAD",
                     "debug/product", "DerivedData/ios-tests/Build/product"):
            (old / ".build" / name).parent.mkdir(parents=True, exist_ok=True)
            (old / ".build" / name).write_text("built\n")
        # A package resolved at the new path keeps its state.
        current = self.checkout / "Packages/Reader/.build"
        (old / "Packages/Reader/.build/artifacts").mkdir(parents=True)
        (old / "Packages/Reader/.build/workspace-state.json").write_text(json.dumps({"path": str(current)}))
        # Exercise cleanup after a confirmed move. The cases below cover a live user and an unavailable census.
        with patch.object(mq.disk, "census", return_value=[]), \
                contextlib.redirect_stdout(io.StringIO()) as said:
            identifier, status = self.gate_once("a")
        self.assertEqual(status, 0)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertFalse(old.exists())
        moved = self.checkout / ".build"
        for name in ("workspace-state.json", "artifacts", "checkouts", "repositories"):
            self.assertFalse((moved / name).exists(), name)
        for folder in list(stale)[1:]: self.assertFalse((self.checkout / folder).exists(), folder)
        # Build products stay, and so does the input a lane provisioned.
        self.assertEqual((moved / "debug/product").read_text(), "built\n")
        self.assertEqual((moved / "DerivedData/ios-tests/Build/product").read_text(), "built\n")
        self.assertEqual((self.checkout / "provisioned/input").read_text(), "made by a provision lane\n")
        self.assertTrue((current / "workspace-state.json").exists())
        self.assertTrue((current / "artifacts").exists())
        for folder in stale: self.assertIn(f"cleared {self.checkout / folder}, which named {old}", said.getvalue())
        self.assertNotIn("Packages/Reader", said.getvalue())

    def test_an_old_checkout_a_process_uses_stays_and_the_new_one_is_made_afresh(self):
        old, old_kit = self.old_layout()
        # A lane an earlier worker left running in it, found by the real process census.
        held = subprocess.Popen(["sleep", "300"], cwd=old)
        self.addCleanup(held.wait)
        self.addCleanup(held.kill)
        with contextlib.redirect_stdout(io.StringIO()) as said:
            identifier, status = self.gate_once("a")
        self.assertEqual(status, 0)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual((old / "provisioned/input").read_text(), "made by a provision lane\n")
        self.assertFalse((self.checkout / "provisioned").exists())
        locks = self.locks(self.repo)
        self.assertEqual(locks[str(self.checkout)], "the merge queue's checkout")
        self.assertEqual(locks[str(old)], f"retired by the merge queue: replaced by {self.checkout}")
        # The toolkit's copy, which nothing used, moved.
        self.assertFalse(old_kit.exists())
        self.assertEqual(self.locks(self.kit), {str(self.copy): "the merge queue's copy of the toolkit"})
        self.assertIn(f"{old} stays where it is, since PID {held.pid} (sleep)", said.getvalue())

    def test_an_old_checkout_that_cannot_be_checked_for_processes_stays(self):
        old, _old_kit = self.old_layout()
        with patch.object(mq.disk, "census", side_effect=RuntimeError("could not list processes with lsof and ps")):
            identifier, status = self.gate_once("a")
        self.assertEqual(status, 0)
        self.assertTrue((old / "provisioned/input").exists())
        self.assertTrue(self.locks(self.repo)[str(old)].startswith("retired by the merge queue"))

    def test_the_toolkits_own_queue_tests_its_batch(self):
        with patch.object(mq, "TOOLKIT_ROOT", self.repo):
            worker = mq.Worker(mq.Queue(self.repo))
            worker.toolkit()
        self.assertNotIn("QUEUEKEEPER_ROOT", worker.environment)
        self.assertFalse(self.copy.exists())

    def test_the_toolkits_own_queue_puts_off_its_move_for_the_round_that_adopts_a_kept_stack(self):
        """vocaro/queuekeeper#276: the landing that made a stack adoptable moved the toolkit's own copy, and the move,
        a new worker process, threw the stack away before the next round adopted it, 30 times of 30. No lane of the
        toolkit's own reads the copy, so the worker settles the stack on the code that gated it, stacks nothing on it,
        and moves the round after."""
        head = run(self.repo, "git", "rev-parse", "origin/main")
        stacked = SimpleNamespace(base=head, drop=Mock())
        with patch.object(mq, "TOOLKIT_ROOT", self.repo), contextlib.redirect_stdout(io.StringIO()) as said:
            worker = mq.Worker(mq.Queue(self.repo))
            worker.code, worker.stacked = "0" * 40, stacked
            with patch.object(worker, "refuses", return_value=None), patch.object(worker, "move_to") as moved:
                worker.toolkit()
                moved.assert_not_called()
                stacked.drop.assert_not_called()
                self.assertIs(worker.stacked, stacked)
                self.assertEqual(worker.lagging, head)
                self.assertIn(f"moving to the toolkit at {head[:12]}, origin/main, after this round, which settles the "
                              f"gate stacked on {head[:12]} with the toolkit at {'0' * 12} that gated it", said.getvalue())
                # The adopted round gates the stack with nothing stacked on it.
                worker.stacked = SimpleNamespace(base=head, tip=head, entries=[], paths=set(), lanes=["gate"],
                                                 name="n", runner=worker, wait=None, drop=Mock(), isolating=False, members=None,
                                                 not_admitted=lambda: True, void=lambda: "no verdict")
                with patch.object(mq, "config_at", return_value={"lanes": {"gate": {}}}), \
                        patch.object(mq, "lanes_of", return_value=["gate"]), patch.object(worker, "begun"), \
                        patch.object(worker, "prune"), patch.object(worker, "after_stack"), \
                        patch.object(worker, "watched", return_value=(75, None)) as watched:
                    worker.adopted()
                self.assertIs(watched.call_args.kwargs["stacks"], False)
                # The positive control: the next round, with no stack kept, moves, and its rounds stack again.
                worker.toolkit()
                self.assertIsNone(worker.lagging)
                moved.assert_called_once()

    def test_a_toolkit_at_origin_that_does_not_start_is_not_moved_to(self):
        """A toolkit commit pushed by hand whose worker code does not import: the project's worker stays on its own
        code, and its lanes run the toolkit it was started from, rather than a gate that sends every entry back."""
        behind = run(self.kit, "git", "rev-parse", "HEAD")
        other = self.base / "other-broken"
        run(self.base, "git", "clone", "-q", str(self.kit_origin), str(other))
        source = other / "src/python/queuekeeper/merge_queue.py"
        source.write_text(source.read_text() + "\ndef broken(:\n")
        run(other, "git", "commit", "-qam", "Break the worker")
        broken = run(other, "git", "rev-parse", "HEAD")
        run(other, "git", "push", "-q", "origin", "HEAD:main")
        with contextlib.redirect_stdout(io.StringIO()) as said: identifier, status = self.gate_once("a")
        self.assertEqual((status, self.state(identifier)["state"], self.moves), (0, "landed", []))
        self.assertIn(f"staying on the toolkit at {behind[:12]}, which this worker was started from, rather than "
                      f"moving to {broken[:12]}, origin/main: importing it exited 1: SyntaxError", said.getvalue())
        self.assertEqual(self.seen.read_text().split("\n"), [str(self.copy), behind, ""])

    def test_an_explicit_toolkit_stands(self):
        with patch.dict(os.environ, {"QUEUEKEEPER_ROOT": str(self.kit)}):
            identifier, status = self.gate_once("a")
        self.assertEqual(status, 0)
        self.assertEqual(self.seen.read_text().split("\n")[0], str(self.kit))
        self.assertFalse(self.copy.exists())


class ToolkitsOwnWorker(Fixture):
    """The toolkit's own queue judging its main with the toolkit at origin/main, not the code its worker was started
    from (vocaro-engineering#177). The fixture's project is the toolkit: it holds the toolkit's Python, and the
    worker starts from a checkout of it at the first commit, as from the shared checkout before follow moves it."""

    def setUp(self):
        super().setUp()
        # Its own toolkit, as the toolkit's queuekeeper.json names it. The fixture's project lists no preflight lane,
        # and the queue checks no spelling of its own, so its Python needs no exemptions (vocaro/queuekeeper#69).
        shutil.copytree(ROOT / "src/python", self.repo / "src/python", ignore=shutil.ignore_patterns("__pycache__"))
        run(self.repo, "git", "add", "src")
        run(self.repo, "git", "commit", "-qm", "Toolkit")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        self.started = self.base / "started"
        run(self.repo, "git", "worktree", "add", "-q", "--detach", str(self.started), "origin/main")
        self.copy = self.base / "queuekeeper-workers/project/project-toolkit"
        variables = patch.dict(os.environ)
        variables.start()
        self.addCleanup(variables.stop)
        del os.environ["QUEUEKEEPER_ROOT"]
        for patcher in (patch.object(mq, "TOOLKIT_ROOT", self.started), patch.object(mq, "execute", self.execute),
                        patch.object(mq, "load", mq.load)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.moves, self.follow = [], True

    def execute(self, command, env): raise Replaced(command, env)

    def work(self):
        """The worker, then the one its move starts from the copy, with the copy's config module judging main, as
        ToolkitCopy.work; with follow False, a move ends the worker as a process that died at once would."""
        while True:
            try: return mq.run(self.repo)
            except Replaced as moved:
                self.moves.append(moved)
                self.assertEqual(moved.command, mq.Queue(self.repo).command(self.copy))
                if not self.follow:
                    os.close(int(moved.env[mq.LOCK_VARIABLE]))  # as the process's end closes the lock it inherited
                    return None
                mq.TOOLKIT_ROOT = self.copy
                mq.load = self.code_of(self.copy).load
                os.environ[mq.PROGRESS_VARIABLE] = moved.env[mq.PROGRESS_VARIABLE]
                os.environ[mq.LOCK_VARIABLE] = moved.env[mq.LOCK_VARIABLE]

    def code_of(self, checkout):
        """A toolkit checkout's config module, loaded apart from this process's, as one of the queuekeeper package,
        whose hook points it reads (plugins.LANE_KEYS)."""
        source = checkout / "src/python/queuekeeper/config.py"
        spec = importlib.util.spec_from_file_location(f"queuekeeper.own_config_{len(self.moves)}_{checkout.name}", source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def toolkit_branch(self, branch, edit, key=None):
        """A toolkit commit on a new branch from origin/main: edit(text) of merge_queue.py's or, with key, config.py
        accepting key and main's gate lane setting it, in one commit."""
        run(self.repo, "git", "fetch", "-q", "origin")
        run(self.repo, "git", "checkout", "-q", "-B", branch, "origin/main")
        name = "config.py" if key else "merge_queue.py"
        source = self.repo / "src/python/queuekeeper" / name
        original = source.read_text()
        edited = accepting_lane_key(original, key) if key else edit(original)
        self.assertNotEqual(edited, original)
        source.write_text(edited)
        if key:
            config = json.loads((self.repo / "queuekeeper.json").read_text())
            config["lanes"]["gate"][key] = True
            (self.repo / "queuekeeper.json").write_text(json.dumps(config))
        run(self.repo, "git", "commit", "-qam", f"Change the toolkit on {branch}")
        return run(self.repo, "git", "rev-parse", "HEAD")

    def landed(self, name, follow=True, adder=None):
        """Queue the branch name, work, and return (exit, its entry's state, what the worker said). With adder, a
        config module, `qk land` reads main with it, as a session's queuekeeper at origin/main does."""
        with patch.object(mq, "parse", adder.parse if adder else mq.parse): [identifier] = self.add(name)
        self.follow = follow
        with contextlib.redirect_stdout(io.StringIO()) as said: status = self.work()
        return status, self.state(identifier)["state"], said.getvalue()

    def test_a_batch_that_adds_a_key_and_sets_it_lands_and_the_next_round_needs_no_hold(self):
        """The case #173 left: the toolkit's own worker, started before the batch, judged the next round's main,
        which sets the key, with its own config module, which refuses it, and held."""
        code = run(self.started, "git", "rev-parse", "HEAD")
        self.toolkit_branch("knows", None, key="newKey")
        self.assertEqual(self.landed("knows")[:2], (0, "landed"))
        self.assertEqual(self.moves, [])
        knows = run(self.repo, "git", "rev-parse", "origin/main")
        # The control: the code this worker is started from refuses main as it now stands.
        main = run(self.repo, "git", "show", "origin/main:queuekeeper.json")
        with self.assertRaisesRegex(ValueError, "Invalid lane gate"): configuration.parse(main)
        self.commit("b", "b")
        status, state, said = self.landed("b", adder=self.code_of(self.repo))
        self.assertEqual((status, state), (0, "landed"))
        self.assertIsNone(mq.Queue(self.repo).held())
        self.assertEqual(len(self.moves), 1)
        self.assertIn(f"moving to the toolkit at {knows[:12]}, origin/main, from {code[:12]}", said)
        self.assertNotIn("QUEUEKEEPER_ROOT", self.moves[0].env)
        self.assertEqual(run(self.copy, "git", "rev-parse", "HEAD"), knows)
        # The worker the move started finished a round, so the move is forgotten.
        self.assertFalse((mq.Queue(self.repo).directory / mq.MOVED).exists())

    def test_a_worker_whose_code_is_origin_main_does_not_move(self):
        """The negative control: no copy, no move."""
        self.commit("a", "a")
        self.assertEqual(self.landed("a")[:2], (0, "landed"))
        self.assertEqual(self.moves, [])
        self.assertFalse(self.copy.exists())

    def test_a_main_whose_worker_does_not_start_lands_its_fix_through_the_old_worker(self):
        """A commit on main, pushed by hand, whose worker code does not import: moving there would leave the queue
        no worker, so the fix could land only by hand. The worker stays on its own code and lands the fix; then,
        main starting again, the next round moves."""
        code = run(self.started, "git", "rev-parse", "HEAD")
        broken = self.toolkit_branch("broken", lambda source: source + "\ndef broken(:\n")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        self.assertIn("SyntaxError", mq.starts(self.repo, broken))
        self.assertIsNone(mq.starts(self.repo, code))
        self.toolkit_branch("fix", lambda source: source.replace("\ndef broken(:\n", ""))
        status, state, said = self.landed("fix")
        self.assertEqual((status, state, self.moves), (0, "landed", []))
        self.assertIn(f"staying on the toolkit at {code[:12]}, which this worker was started from, rather than "
                      f"moving to {broken[:12]}, origin/main: importing it exited 1: SyntaxError", said)
        self.assertIsNone(mq.Queue(self.repo).held())
        self.commit("c", "c")
        status, state, said = self.landed("c")
        self.assertEqual((status, state, len(self.moves)), (0, "landed", 1))
        self.assertNotIn("staying on the toolkit", said)
        self.assertEqual(run(self.copy, "git", "show", "-s", "--format=%s", "HEAD"), "Change the toolkit on fix")

    def test_a_toolkit_a_moved_worker_stopped_in_is_not_moved_to_again(self):
        """A worker that moved and then died, or held, before it finished a round: the next worker, which the next
        `qk land` starts from older code, stays on that code and lands, rather than moving into the same stop.
        Only that commit is refused: the next one on main is moved to."""
        code = run(self.started, "git", "rev-parse", "HEAD")
        later = self.toolkit_branch("later", lambda source: source + "\n# A later toolkit.\n")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        self.commit("a", "a")
        self.assertEqual(self.landed("a", follow=False)[1], "queued")
        moved = json.loads((mq.Queue(self.repo).directory / mq.MOVED).read_text())
        self.assertEqual((moved["from"], moved["to"]), (code, later))
        status, state, said = self.landed_again()
        self.assertEqual((status, state, len(self.moves)), (0, "landed", 1))
        self.assertIn(f"staying on the toolkit at {code[:12]}, which this worker was started from, rather than "
                      f"moving to {later[:12]}, origin/main: a worker moved to it before and stopped before it "
                      "finished a round", said)
        self.assertTrue((mq.Queue(self.repo).directory / mq.MOVED).exists())
        # The positive control: main has moved past that commit, so the next worker moves, and the one it starts
        # finishes a round, which forgets the move.
        landed = run(self.repo, "git", "rev-parse", "origin/main")
        self.commit("c", "c")
        status, state, said = self.landed("c")
        self.assertEqual((status, state, len(self.moves)), (0, "landed", 2))
        self.assertIn(f"moving to the toolkit at {landed[:12]}, origin/main, from {code[:12]}", said)
        self.assertFalse((mq.Queue(self.repo).directory / mq.MOVED).exists())

    def test_a_worker_under_its_launcher_follows_after_moving_to_a_newer_toolkit(self):
        """vocaro-engineering#374, as the production worker ran on 2026-11-10: the signed launcher started the worker
        from the shared checkout, the worker moved to a toolkit copy, and the launcher, still its parent with the
        original arguments, held back the checkout the worker then meant to follow. Another queue's launcher runs
        nothing from it either (#384)."""
        queue = mq.Queue(self.repo)
        own = launcher_process(4242, queue, self.repo)
        another = launcher_process(4343, queue, self.repo)
        load = mq.load
        # One try after the landing, as SharedCheckout makes it: a held-back checkout ends the worker at once.
        limit = patch.object(mq, "FOLLOW_LIMIT", 0)
        limit.start()
        self.addCleanup(limit.stop)
        later = self.toolkit_branch("later", lambda source: source + "\n# A later toolkit.\n")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        for name, processes, verified in (("unsigned launcher", [own], False), ("another launcher", [own, another], True),
                                          ("own launcher", [own], True)):
            with self.subTest(name):
                self.commit(name.replace(" ", "-"), name.replace(" ", "-"))
                [identifier] = self.add(name.replace(" ", "-"))
                # The shared checkout on main, behind the landing to come.
                run(self.repo, "git", "checkout", "-q", "-B", "main", "origin/main")
                before = run(self.repo, "git", "rev-parse", "HEAD")
                moves = len(self.moves)
                with under_launcher(processes, verified), contextlib.redirect_stdout(io.StringIO()) as said:
                    self.assertEqual(self.work(), 0)
                said = said.getvalue()
                self.assertEqual(self.state(identifier)["state"], "landed")
                self.assertEqual(len(self.moves), moves + 1, said)
                # The moved worker runs from the copy, while its launcher's arguments still name the shared checkout.
                self.assertIn(str(self.copy / "src/python"), self.moves[-1].command)
                self.assertNotIn(str(self.repo / "src/python"), self.moves[-1].command)
                after, main = run(self.repo, "git", "rev-parse", "HEAD"), run(self.repo, "git", "rev-parse", "origin/main")
                if verified:
                    self.assertEqual(after, main)
                    self.assertIn(f"fast-forwarded {self.repo} from {before[:12]} to {main[:12]}", said)
                else:
                    # The negative control: a launcher that does not verify as signed still holds it back.
                    self.assertEqual(after, before)
                    self.assertIn(f"{mq.IN_USE}: PID 4242 ({mq.LAUNCHER_PROGRAM}), argument {self.repo}/src/python", said)
                # The next worker the launcher starts begins from the older toolkit again and moves.
                mq.TOOLKIT_ROOT, mq.load = self.started, load
        self.assertEqual(run(self.copy, "git", "log", "-1", "--format=%s", later), "Change the toolkit on later")

    def landed_again(self):
        """Work the entry a worker left queued: (exit, its state, what the worker said)."""
        [entry] = mq.Queue(self.repo).waiting()
        with contextlib.redirect_stdout(io.StringIO()) as said: status = self.work()
        return status, self.state(entry["id"])["state"], said.getvalue()

    def test_a_failed_exec_holds_and_forgets_the_move(self):
        """An exec that returns never ran the toolkit it named, which later workers may still move to."""
        self.toolkit_branch("later", lambda source: source + "\n# A later toolkit.\n")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        self.commit("a", "a")
        mq.execute = lambda command, env: None
        status, state, said = self.landed("a")
        self.assertEqual((status, state), (1, "queued"))
        self.assertIn("could not start itself again", mq.Queue(self.repo).held())
        self.assertFalse((mq.Queue(self.repo).directory / mq.MOVED).exists())


LAUNCHER_APP = "/Applications/Queue Launcher.app"


def launcher_process(pid, queue, toolkit):
    """A signed queue launcher as its LaunchAgent runs it (Queue.launcher_plist): the launcher's executable with the
    worker's command from toolkit as its arguments, at the queue's main checkout, as ps and lsof show it."""
    return mq.disk.Process(pid=pid, command=mq.LAUNCHER_PROGRAM, cwd=str(queue.main_checkout),
                           args=" ".join([f"{LAUNCHER_APP}/Contents/MacOS/{mq.LAUNCHER_PROGRAM}",
                                          *queue.command(toolkit, if_waiting=True)]))


@contextlib.contextmanager
def under_launcher(processes, verified=True):
    """The host's census is processes and a launcher app verifies as signed when verified; yields the apps checked."""
    checked = []
    def signed(app):
        checked.append(app)
        return verified
    with patch.object(mq.disk, "census", return_value=processes), patch.object(mq, "signed_launcher", signed):
        yield checked


class SharedCheckout(Fixture):
    """The toolkit's own queue keeping its main checkout, the shared checkout, at origin/main
    (vocaro-engineering#104). The fixture's clone stands for that checkout."""
    def setUp(self):
        super().setUp()
        for patcher in (patch.object(mq, "TOOLKIT_ROOT", self.repo), patch.object(mq, "FOLLOW_LIMIT", 0)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def held(self, command, cwd):
        process = subprocess.Popen(command, cwd=cwd)
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        return process

    def python(self, cwd):
        """A Python left running in cwd, which may import the toolkit from it as `python3 -c` does."""
        return self.held([sys.executable, "-c", "import time; time.sleep(300)"], cwd)

    @contextlib.contextmanager
    def follow_clock(self):
        """Move the worker's monotonic clock past a follow deadline without making the host wait."""
        offset = 0

        def advance(seconds):
            nonlocal offset
            offset += seconds

        with patch.object(mq, "time", wraps=time) as clock:
            clock.monotonic.side_effect = lambda: time.monotonic() + offset
            yield advance

    def land(self, prepare=lambda: None):
        """Land one commit while the shared checkout is on main at the old origin/main, as prepare leaves it,
        and return (its HEAD before, after, origin/main, what the worker said)."""
        self.landings = getattr(self, "landings", 0) + 1
        name = f"landed-{self.landings}"
        self.commit(name, name)
        self.add(name)
        run(self.repo, "git", "checkout", "-q", "-B", "main", "origin/main")
        prepare()
        before = run(self.repo, "git", "rev-parse", "HEAD")
        with contextlib.redirect_stdout(io.StringIO()) as said:
            self.assertEqual(self.work(), 0)
        return before, run(self.repo, "git", "rev-parse", "HEAD"), run(self.repo, "git", "rev-parse", "origin/main"), \
            said.getvalue()

    def test_a_clean_shared_checkout_on_main_follows_each_landing(self):
        # A session working in a worktree inside it does not hold it back: that is not its tree.
        nested = self.repo / ".claude/worktrees/agent"
        run(self.repo, "git", "worktree", "add", "-q", "--detach", str(nested), "origin/main")
        (self.repo / ".git/info/exclude").write_text(".claude/worktrees/\n")
        held = subprocess.Popen(["sleep", "300"], cwd=nested)
        self.addCleanup(held.wait)
        self.addCleanup(held.kill)
        before, after, main, said = self.land()
        self.assertNotEqual(before, main)
        self.assertEqual(after, main)
        self.assertIn(f"fast-forwarded {self.repo} from {before[:12]} to {main[:12]}", said)
        self.assertTrue((self.repo / "landed-1").is_file())

    def test_a_shared_checkout_that_is_not_plainly_behind_is_left_as_it_is(self):
        def edit(): (self.repo / "AGENTS.md").write_text("a session's edit\n")
        def untracked(): (self.repo / "notes.txt").write_text("a session's file\n")
        def other_branch(): run(self.repo, "git", "checkout", "-q", "-B", "elsewhere", "origin/main")
        def own_commit():
            (self.repo / "own").write_text("x")
            run(self.repo, "git", "add", "own")
            run(self.repo, "git", "commit", "-qm", "A commit of its own")
        cases = {"changed": (edit, "changed or untracked path(s), such as AGENTS.md"),
                 "untracked": (untracked, "changed or untracked path(s), such as notes.txt"),
                 "branch": (other_branch, "it is on elsewhere, not main"),
                 "commit": (own_commit, "has commits origin/main lacks")}
        for name, (prepare, why) in cases.items():
            with self.subTest(name):
                run(self.repo, "git", "checkout", "-q", "-f", "-B", "main", "origin/main")
                run(self.repo, "git", "clean", "-fdq")
                before, after, main, said = self.land(prepare)
                self.assertNotEqual(after, main)
                self.assertEqual(after, before)
                self.assertIn(f"left {self.repo} as it is: ", said)
                self.assertIn(why, said)
                if name == "changed": self.assertEqual((self.repo / "AGENTS.md").read_text(), "a session's edit\n")
                if name == "untracked": self.assertTrue((self.repo / "notes.txt").is_file())

    def test_a_commit_made_in_the_shared_checkout_and_landed_does_not_hold_it_back(self):
        # A session commits in the shared checkout and lands from it; the queue lands the change under
        # another SHA, so the checkout's main is beside origin/main, not behind it (2026-11-06).
        def made_here():
            run(self.repo, "git", "cherry-pick", f"landed-{self.landings}")
            run(self.repo, "git", "commit", "-q", "--amend", "-m", "The same change, as the session made it")
        before, after, main, said = self.land(made_here)
        self.assertNotEqual(before, main)
        self.assertNotEqual(subprocess.run(["git", "merge-base", "--is-ancestor", before, main], cwd=self.repo).returncode, 0,
                            "not plainly behind: a fast-forward alone could not move it")
        self.assertEqual(after, main)
        self.assertIn(f"moved {self.repo} from {before[:12]} to {main[:12]}, past 1 commit(s) of its own "
                      "that origin/main already holds by their changes", said)
        self.assertEqual(run(self.repo, "git", "status", "--porcelain"), "")

    def test_a_landed_commit_beside_one_that_did_not_land_still_holds_it_back(self):
        # The negative control: one commit of its own that origin/main does not hold keeps it as it is.
        def made_here():
            run(self.repo, "git", "cherry-pick", f"landed-{self.landings}")
            run(self.repo, "git", "commit", "-q", "--amend", "-m", "The same change, as the session made it")
            (self.repo / "own").write_text("x")
            run(self.repo, "git", "add", "own")
            run(self.repo, "git", "commit", "-qm", "A commit of its own")
        before, after, main, said = self.land(made_here)
        self.assertEqual(after, before)
        self.assertIn(f"left {self.repo} as it is: its HEAD {before[:12]} has commits origin/main lacks, whose "
                      "changes it does not hold either: 1, such as", said)
        self.assertTrue((self.repo / "own").is_file())

    def test_a_process_started_from_the_shared_checkout_holds_it_back(self):
        def started(): self.runner = self.python(self.repo / "scripts")
        before, after, main, said = self.land(started)
        self.assertEqual(after, before)
        self.assertRegex(said, rf"a process started from it may still run its toolkit: PID {self.runner.pid} "
                               rf"\([\w.]+\), working directory \S*/scripts;")
        # A census that fails says nothing of what runs there, so it holds the checkout back too.
        self.runner.kill()
        self.runner.wait()
        with patch.object(mq.disk, "census", side_effect=RuntimeError("could not list processes")):
            before, after, main, said = self.land()
        self.assertEqual(after, before)
        self.assertIn("whether a process started from it still runs is unknown", said)
        # The positive control: with the process gone, the next landing moves it.
        before, after, main, said = self.land()
        self.assertEqual(after, main)

    def test_a_session_that_only_names_the_checkout_does_not_hold_it_back(self):
        """vocaro-engineering#182: a Claude session in a worktree inside it passes the checkout itself as its project
        root, and its MCP servers start at that root; neither runs the toolkit from its tree."""
        nested = self.repo / ".claude/worktrees/agent"
        run(self.repo, "git", "worktree", "add", "-q", "--detach", str(nested), "origin/main")
        (self.repo / ".git/info/exclude").write_text(".claude/worktrees/\n")
        def started():
            self.held(["sh", "-c", "sleep 300; exit 0", "--project-config-root", str(self.repo)], nested)
            self.held(["sleep", "300"], self.repo)
        before, after, main, said = self.land(started)
        self.assertEqual(after, main)
        self.assertIn(f"fast-forwarded {self.repo} from {before[:12]} to {main[:12]}", said)

    def test_a_program_only_left_in_the_checkout_does_not_hold_it_back(self):
        """vocaro-engineering#384: a session's shell left in src/python, or a tool a run started there, loads nothing
        of the tree by its working directory alone; only Python puts that directory on its import path."""
        def left():
            self.held(["sleep", "300"], self.repo / "scripts")
            self.held(["sh", "-c", "sleep 300; exit 0"], self.repo / "scripts")
        before, after, main, said = self.land(left)
        self.assertEqual(after, main)
        self.assertIn(f"fast-forwarded {self.repo} from {before[:12]} to {main[:12]}", said)
        # The negative controls: such a program that names a file there by a relative argument, and a Python left
        # there, still hold it back, each by what ties it.
        cases = {"relative argument": (lambda: self.held(["sh", "-c", "sleep 300; exit 0", "qk"], self.repo / "scripts"),
                                       r"argument \S*/scripts/qk"),
                 "python": (lambda: self.python(self.repo / "scripts"), r"working directory \S*/scripts")}
        for name, (start, how) in cases.items():
            with self.subTest(name):
                started = []
                before, after, main, said = self.land(lambda: started.append(start()))
                self.assertEqual(after, before)
                self.assertRegex(said, rf"may still run its toolkit: PID {started[0].pid} \([\w.]+\), {how}")
                started[0].kill()
                started[0].wait()

    def test_a_process_that_restarts_itself_and_a_shell_naming_a_removed_worktree_do_not_hold_it_back(self):
        """vocaro/queuekeeper#159: a dashboard left open registers as restarting itself once the checkout moves
        (restartable), and a session's shell can outlive the worktree it names; neither holds the checkout back."""
        def started():
            self.server = self.python(self.repo / "scripts")
            self.assertIsNotNone(mq.restartable.register(self.repo, "the dashboard", pid=self.server.pid))
            self.held(["sh", "-c", "sleep 300; exit 0", str(self.repo / ".claude/worktrees/removed/notes.md")], self.repo)
        before, after, main, said = self.land(started)
        self.assertEqual(after, main)
        self.assertIn(f"fast-forwarded {self.repo} from {before[:12]} to {main[:12]}", said)
        # The negative control: a registration whose PID another process now has, by its start time, passes over
        # nothing, and is removed.
        record = mq.restartable.folder(self.repo) / f"{self.server.pid}.json"
        record.write_text(json.dumps({"pid": self.server.pid, "started": "Thu Jan  1 00:00:00 1970", "what": "x"}))
        before, after, main, said = self.land()
        self.assertEqual(after, before)
        self.assertRegex(said, rf"may still run its toolkit: PID {self.server.pid} \([\w.]+\), working directory")
        self.assertFalse(record.exists())

    def test_a_toolkit_run_from_the_checkouts_root_holds_it_back_and_is_named(self):
        def started(): self.runner = self.held(["sh", "-c", "sleep 300; exit 0", "scripts/qk"], self.repo)
        before, after, main, said = self.land(started)
        self.assertEqual(after, before)
        self.assertRegex(said, rf"may still run its toolkit: PID {self.runner.pid} \(\w+\), argument \S*/scripts/qk; "
                               "tried again once nothing waits")

    def test_a_checkout_left_for_a_run_from_it_follows_once_the_run_ends(self):
        tries = []
        def ended(seconds):
            tries.append(seconds)
            if len(tries) == 2: self.runner.kill()
        def started(): self.runner = self.python(self.repo / "scripts")
        with patch.object(mq, "FOLLOW_LIMIT", None), patch.object(mq, "pause", ended):
            before, after, main, said = self.land(started)
        self.assertEqual(after, main)
        self.assertEqual(tries, [mq.FOLLOW_POLL] * 2)
        self.assertIn(f"left {self.repo} as it is: {mq.IN_USE}", said)
        self.assertIn(f"fast-forwarded {self.repo} from {before[:12]} to {main[:12]}", said)
        # The negative controls: a checkout left on another branch is a person's, so nothing tries it again, and a
        # test's bound on the tries leaves it behind, saying so.
        tries.clear()
        def elsewhere(): run(self.repo, "git", "checkout", "-q", "-B", "elsewhere", "origin/main")
        with patch.object(mq, "FOLLOW_LIMIT", None), patch.object(mq, "pause", ended):
            before, after, main, said = self.land(elsewhere)
        self.assertEqual((after, tries), (before, []))
        run(self.repo, "git", "checkout", "-q", "main")
        limit = mq.FOLLOW_POLL // 2
        with self.follow_clock() as advance, patch.object(mq, "FOLLOW_LIMIT", limit), \
                patch.object(mq, "pause", side_effect=advance) as paused:
            before, after, main, said = self.land(started)
        paused.assert_called_once_with(mq.FOLLOW_POLL)
        self.assertEqual(after, before)
        self.assertIn(f"left {self.repo} behind after {limit}s of tries: {mq.IN_USE}", said)

    def held_back(self, during):
        """Land one commit while a Python runs from the checkout, as the census shows it, with no bound on the
        tries; during(tries, holders) runs at each try's pause. Returns (before, after, origin/main, said, tries)."""
        holders = [mq.disk.Process(pid=42, command="Python", cwd=str(self.repo / "scripts"))]
        tries = []
        def paused(seconds):
            tries.append(seconds)
            during(len(tries), holders)
        with patch.object(mq.disk, "census", return_value=holders), patch.object(mq, "FOLLOW_LIMIT", None), \
                patch.object(mq, "pause", paused):
            before, after, main, said = self.land()
        return before, after, main, said, tries

    def test_a_run_that_outlasts_an_hour_still_lets_it_follow_once_it_ends(self):
        """vocaro-engineering#384, owner's decision of 2026-11-11: the tries go on until the checkout moves, past
        the hour they once stopped at, saying the deferral once while the same run holds it and again when another
        does, and the round's account says what the worker waits for."""
        waits = []

        def during(tried, holders):
            if tried == 1:
                state = mq.Queue(self.repo).round_state()
                waits.append((state.get("waiting"), state.get("catchingUp"), mq.snapshot(self.repo)["idle"]))
                advance(3600 + mq.FOLLOW_POLL)
            if tried == 3:
                holders[:] = [mq.disk.Process(pid=43, command="Python", cwd=str(self.repo / "scripts"))]
            if tried == 5:
                holders.clear()

        with self.follow_clock() as advance:
            before, after, main, said, tries = self.held_back(during)
        self.assertEqual(after, main)
        self.assertEqual(tries, [mq.FOLLOW_POLL] * 5)
        self.assertIn(f"fast-forwarded {self.repo} from {before[:12]} to {main[:12]}", said)
        told = f"left {self.repo} behind for now, trying again every {mq.FOLLOW_POLL}s until it moves: {mq.IN_USE}: "
        self.assertEqual(said.count(told), 2)
        self.assertIn(told + "PID 42 (Python)", said)
        self.assertIn(told + "PID 43 (Python)", said)
        # A worker only waiting to move the checkout leaves the queue idle (vocaro/queuekeeper#285).
        self.assertEqual(waits, [("the shared checkout to follow origin/main once nothing runs from it", True, True)])
        self.assertIsNone(mq.Queue(self.repo).round_state().get("waiting"))
        self.assertIsNone(mq.Queue(self.repo).round_state().get("catchingUp"))
        self.assertNotIn("behind after", said)

    def test_what_holds_it_is_named_in_words_by_status_and_doctor(self):
        """vocaro/queuekeeper#159: while the worker waits for the checkout to follow, `qk queue status` and `qk queue
        doctor` say what holds it, a project's queue worker, a lane's build through the Apple wrapper or a `qk`
        command, with its PID and how long it has run, where the worker's log named only a PID and an argument."""
        worker = self.held([sys.executable, "-c", "import time; time.sleep(300)  # 'queue', 'run'",
                            str(self.repo / "src/python"), "/Users/someone/Development/mpm"], self.repo)
        shim = self.held(["sh", "-c", "sleep 300; exit 0",
                          str(self.repo / "plugins/apple/src/queuekeeper_apple/_shims/xcodebuild")], self.repo)
        command = self.held(["sh", "-c", "sleep 300; exit 0", str(self.repo / "bin/qk"), "unittest"], self.repo)
        seen = []
        def paused(seconds):
            state = mq.Queue(self.repo).round_state()
            seen.append((state, mq.round_lines(state), mq.checkout_hints(self.repo)))
            for process in (worker, shim, command):
                process.kill()
                process.wait()
        with patch.object(mq, "FOLLOW_LIMIT", None), patch.object(mq, "pause", paused):
            before, after, main, said = self.land()
        self.assertEqual(after, main)
        state, lines, hints = seen[0]
        self.assertEqual({holder["pid"]: holder["what"] for holder in state["holders"]},
                         {worker.pid: "the queue worker for mpm",
                          shim.pid: "a lane's xcodebuild through the Apple plugin's wrapper",
                          command.pid: "a `qk unittest` command"})
        self.assertTrue(all(isinstance(holder["since"], float) for holder in state["holders"]))
        self.assertIn(f"    held by the queue worker for mpm, PID {worker.pid}, running ", "\n".join(lines))
        self.assertEqual(len(hints), 1)
        self.assertRegex(hints[0], rf"^the worker has waited \d+s for the shared checkout to follow origin/main once "
                                   rf"nothing runs from it; held by .*a `qk unittest` command, PID {command.pid}, running")
        # Once it has followed, neither says anything of it.
        self.assertIsNone(mq.Queue(self.repo).round_state().get("holders"))
        self.assertEqual(mq.checkout_hints(self.repo), [])
        # A process that is gone has no start.
        self.assertEqual(mq.started_at([worker.pid]), {})

    def test_a_hold_during_the_tries_ends_them(self):
        def during(tried, holders):
            if tried == 2: mq.Queue(self.repo).hold("for a test")
        before, after, main, said, tries = self.held_back(during)
        self.assertEqual((after, len(tries)), (before, 2))
        self.assertIn(f"stops trying to move {self.repo}: the queue is held", said)

    def test_a_checkout_left_for_another_reason_during_the_tries_ends_them(self):
        """The negative control: a run holds it back, then a session's edit makes it a person's, so the tries stop
        rather than go on forever, and the log says why it was left."""
        def during(tried, holders):
            if tried == 2:
                holders.clear()
                (self.repo / "AGENTS.md").write_text("a session's edit\n")
            self.assertLess(tried, 3, "a dirty checkout should not be tried again")
        before, after, main, said, tries = self.held_back(during)
        self.assertEqual((after, len(tries)), (before, 2))
        self.assertIn(f"left {self.repo} as it is: it has 1 changed or untracked path(s), such as AGENTS.md", said)
        self.assertEqual((self.repo / "AGENTS.md").read_text(), "a session's edit\n")

    def test_a_landing_that_arrives_during_the_tries_ends_them(self):
        # Keep the checkout in use until the test releases it, independent of the host census.
        holders = [mq.disk.Process(pid=42, command="Python", cwd=str(self.repo / "scripts"))]
        tries = []
        def arrived(seconds):
            tries.append(seconds)
            if len(tries) == 1:
                self.commit("later", "later")
                self.add("later")
                run(self.repo, "git", "checkout", "-q", "main")
                return
            self.assertEqual(self.gates(), 2, "the queued entry should gate before the worker retries")
            if len(tries) == 3:
                holders.clear()
        with patch.object(mq.disk, "census", return_value=holders), \
                patch.object(mq, "FOLLOW_LIMIT", None), patch.object(mq, "pause", arrived):
            _, after, main, said = self.land()
        # The arrival ended the first tries, the second round landed it, and the tries after it caught up.
        self.assertIn("later", self.main_files())
        self.assertEqual(tries, [mq.FOLLOW_POLL] * 3)
        self.assertEqual(after, main)
        self.assertEqual(said.count(f"left {self.repo} as it is: {mq.IN_USE}"), 2)
        self.assertIn(f"stops trying to move {self.repo}: an entry waits", said)

    def test_a_signed_launcher_does_not_hold_it_back(self):
        """vocaro-engineering#374, #384: a LaunchAgent's launcher names the shared checkout's src/python in its
        arguments and waits on the worker it spawned; it runs nothing from the checkout, so neither the worker's own
        launcher nor another queue's holds it back, while the worker it started counts by its own arguments."""
        queue = mq.Queue(self.repo)
        own, other = launcher_process(4242, queue, self.repo), launcher_process(4343, queue, self.repo)
        self.assertEqual([how for _, how, _ in mq.disk.runners([own], self.repo)], ["argument"],
                         "the fixture's launcher is one the census counts by its arguments")
        with under_launcher([own, other]) as checked:
            before, after, main, said = self.land()
        self.assertEqual(after, main)
        self.assertIn(f"fast-forwarded {self.repo} from {before[:12]} to {main[:12]}", said)
        self.assertIn(LAUNCHER_APP, checked)
        # The negative controls: an unsigned launcher, a worker still running from the checkout that a launcher
        # started, and a process that runs from the checkout beside the launchers each still hold it back.
        runner = mq.disk.Process(pid=4444, command="Python", cwd=str(self.repo / "scripts"))
        worker = mq.disk.Process(pid=4545, command="python3", cwd=str(self.repo),
                                 args=" ".join(queue.command(self.repo, if_waiting=True)))
        cases = {"unsigned": ([own], False, f"PID 4242 ({mq.LAUNCHER_PROGRAM}), argument {self.repo}/src/python"),
                 "its worker": ([own, worker], True, f"PID 4545 (python3), argument {self.repo}/src/python"),
                 "a run from it": ([own, other, runner], True, "PID 4444 (Python), working directory")}
        for name, (processes, verified, why) in cases.items():
            with self.subTest(name), under_launcher(processes, verified):
                before, after, main, said = self.land()
                self.assertEqual(after, before)
                self.assertIn(f"left {self.repo} as it is: {mq.IN_USE}: ", said)
                self.assertIn(why, said)
                if verified: self.assertNotIn(f"({mq.LAUNCHER_PROGRAM})", said)

    def test_a_projects_queue_leaves_the_toolkits_checkout_alone(self):
        with patch.object(mq, "TOOLKIT_ROOT", ROOT):
            before, after, main, said = self.land()
        self.assertEqual(after, before)
        self.assertNotIn("fast-forwarded", said)
        self.assertNotIn("as it is:", said)


# What git prints, over HTTPS and SSH, when it did not reach GitHub.
UNREACHED = {
    "https name": "fatal: unable to access 'https://github.com/vocaro/queuekeeper.git/': Could not resolve host: "
                  "github.com",
    "ssh name": "ssh: Could not resolve hostname github.com: nodename nor servname provided, or not known\n"
                "fatal: Could not read from remote repository.\n\nPlease make sure you have the correct access rights",
    "https refused": "fatal: unable to access 'https://github.com/x/y.git/': Failed to connect to github.com port 443 "
                     "after 2 ms: Couldn't connect to server",
    "ssh refused": "ssh: connect to host github.com port 22: Connection refused\nfatal: Could not read from remote "
                   "repository.",
    "https timeout": "fatal: unable to access 'https://github.com/x/y.git/': Failed to connect to github.com port 443 "
                     "after 75003 ms: Operation timed out",
    "ssh timeout": "ssh: connect to host github.com port 22: Operation timed out\nfatal: Could not read from remote "
                   "repository.",
    "https reset": "error: RPC failed; curl 56 Recv failure: Connection reset by peer\nfatal: early EOF",
    "ssh dropped": "Connection closed by 140.82.112.4 port 22\nfatal: the remote end hung up unexpectedly",
}
# What git prints when GitHub answered and refused, which holds at once.
REFUSED = {
    "https authentication": "remote: Invalid username or token.\nfatal: Authentication failed for "
                            "'https://github.com/x/y.git/'",
    "https forbidden": "fatal: unable to access 'https://github.com/x/y.git/': The requested URL returned error: 403",
    "https no prompt": "fatal: could not read Username for 'https://github.com': terminal prompts disabled",
    "ssh key": "git@github.com: Permission denied (publickey).\nfatal: Could not read from remote repository.",
    "ssh host key": "Host key verification failed.\nfatal: Could not read from remote repository.",
    "missing": "ERROR: Repository not found.\nfatal: Could not read from remote repository.",
    "rejected": " ! [remote rejected] HEAD -> main (protected branch hook declined)\nerror: failed to push some refs",
    "not git": "fatal: 'origin' does not appear to be a git repository",
    # A local path with no repository: git adds the line a dropped SSH connection ends with.
    "no repository": "fatal: '/x/gone.git' does not appear to be a git repository\nfatal: Could not read from remote "
                     "repository.\n\nPlease make sure you have the correct access rights\nand the repository exists.",
}


class Unreachable(Fixture):
    """A fetch or push that did not reach the remote is tried again with a backoff, and holds only when it keeps
    failing; one the remote refused holds at once (vocaro-engineering#113)."""
    def test_what_did_not_reach_the_remote_is_told_from_a_refusal(self):
        for name, said in UNREACHED.items():
            with self.subTest(name): self.assertTrue(mq.unreached(said))
        for name, said in REFUSED.items():
            with self.subTest(name): self.assertFalse(mq.unreached(said))

    def test_a_fetch_that_did_not_reach_the_remote_is_tried_again(self):
        # Exercise every transport error through the same fetch the worker uses. One round also
        # checks that a recovered fetch lets the worker gate and land the entry.
        for name, said in UNREACHED.items():
            with self.subTest(name), self.remote_says(said, {1, 2}) as (fetches, slept):
                self.assertTrue(mq.fetch_main(self.repo, say=lambda _: None))
                self.assertEqual(len(fetches), 3)
                self.assertEqual(slept, list(mq.BACKOFF[:2]))

        self.commit("a", "a")
        [identifier] = self.add("a")
        with patch("builtins.print"), self.remote_says(UNREACHED["ssh name"], {1, 2}) as (_, slept):
            self.assertEqual(self.work(), 0)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertIsNone(mq.Queue(self.repo).held())
        self.assertEqual(self.gates(), 1)
        self.assertEqual(slept, list(mq.BACKOFF[:2]))

    def test_a_passed_gate_survives_an_outage_at_the_fetch_before_the_push(self):
        """The case of 2026-11-02: the round's own fetch worked, the push's did not, for a while."""
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.remote_says(UNREACHED["https name"], {2, 3, 4}) as (fetches, slept): self.work()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertIn("a", self.main_files())
        self.assertEqual(self.gates(), 1)
        self.assertEqual(slept, list(mq.BACKOFF[:3]))

    def test_a_push_that_did_not_reach_the_remote_is_pushed_again(self):
        for name in ("https refused", "ssh timeout"):
            with self.subTest(name):
                self.commit(name.replace(" ", "-"), name.replace(" ", "-"))
                [identifier] = self.add(name.replace(" ", "-"))
                with patch("builtins.print"), self.remote_says(UNREACHED[name], {1}, "push") as (pushes, slept):
                    self.work()
                self.assertEqual(self.state(identifier)["state"], "landed")
                self.assertEqual(len(pushes), 2)

    def test_a_remote_that_stays_out_of_reach_holds_after_the_last_backoff_of_the_last_try(self):
        """Each round's fetch gets its backoff, and the round its tries (HoldKinds); then it holds."""
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.remote_says(UNREACHED["https name"], range(1, 999)) as (fetches, slept), patch("builtins.print"):
            self.assertEqual(self.work(), 1)
        self.assertEqual(self.state(identifier)["state"], "queued")
        rounds = len(mq.CLEARING_WAITS) + 1
        self.assertEqual((len(fetches), slept.count(mq.BACKOFF[-1])), (rounds * (len(mq.BACKOFF) + 1), rounds))
        self.assertIn(f"{len(mq.BACKOFF) + 1} times", mq.Queue(self.repo).held())
        self.assertEqual(self.gates(), 0)

    def test_a_fetch_or_push_the_remote_refused_holds_at_once(self):
        """Repository access errors hold. Credential backoff and ref rejection have their own tests."""
        for command in ("fetch", "push"):
            for name, said in REFUSED.items():
                if command == "push" and (mq.AUTHENTICATION.search(said) or mq.REJECTED_MAIN.search(said)): continue
                with self.subTest(command=command, refusal=name):
                    with contextlib.suppress(OSError): mq.Queue(self.repo).hold_file.unlink()
                    branch = f"{command}-{name}".replace(" ", "-")
                    self.commit(branch, branch)
                    [identifier] = self.add(branch)
                    with patch("builtins.print"), self.remote_says(said, {1}, command) as (calls, slept):
                        self.assertEqual(self.work(), 1)
                    self.assertEqual(self.state(identifier)["state"], "queued" if command == "fetch" else "passed")
                    self.assertEqual((len(calls), slept), (1, []))
                    self.assertIn(said.splitlines()[-1], mq.Queue(self.repo).held())
                    mq.remove(mq.Queue(self.repo), identifier, quiet=True)

    def test_land_tries_its_fetch_again_and_fails_on_a_refusal(self):
        self.commit("a", "a")
        with patch.object(mq.Queue, "start"), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()) as said:
            with self.remote_says(UNREACHED["ssh dropped"], {1}) as (fetches, slept):
                self.assertIsNotNone(mq.add(self.repo, "a"))
            self.assertIn("did not reach the remote", said.getvalue())
            self.assertEqual(slept, [mq.BACKOFF[0]])
            self.commit("b", "b")
            with self.remote_says(REFUSED["ssh key"], {1}) as (fetches, slept):
                with self.assertRaisesRegex(ValueError, "Permission denied"): mq.add(self.repo, "b")
            self.assertEqual((len(fetches), slept), (1, []))


class SignedLauncher(Fixture):
    def arguments(self, *words):
        parser = argparse.ArgumentParser()
        mq.add_arguments(parser)
        return parser.parse_args(words)

    def registered(self, queue):
        directory = self.base / "Library" / "LaunchAgents"
        directory.mkdir(parents=True)
        (directory / f"{queue.launcher_label()}.plist").touch()

    def test_a_registered_launcher_is_started_instead_of_the_session_worker(self):
        self.commit("a", "a")
        self.add("a")
        queue = mq.Queue(self.repo)
        self.registered(queue)
        with patch.object(mq.Path, "home", return_value=self.base), patch.object(mq.sys, "platform", "darwin"), \
                patch.object(mq.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as request, \
                patch.object(mq.subprocess, "Popen") as detached:
            queue.start()
        request.assert_called_once_with([mq.LAUNCHCTL, "kickstart", f"gui/{os.getuid()}/{queue.launcher_label()}"],
                                        capture_output=True, text=True, timeout=10)
        detached.assert_not_called()

    def test_a_rejected_launcher_start_falls_back_and_records_the_reason(self):
        self.commit("a", "a")
        self.add("a")
        queue = mq.Queue(self.repo)
        self.registered(queue)
        refusal = SimpleNamespace(returncode=1, stderr="service is unavailable")
        with patch.object(mq.Path, "home", return_value=self.base), patch.object(mq.sys, "platform", "darwin"), \
                patch.object(mq.subprocess, "run", return_value=refusal), patch.object(mq, "environment", return_value={}), \
                patch.object(mq.subprocess, "Popen") as detached, patch.object(mq.threading, "Thread"):
            queue.start()
        detached.assert_called_once()
        self.assertTrue(detached.call_args.kwargs["start_new_session"])
        self.assertIn("service is unavailable", (queue.directory / "worker.log").read_text())

    def test_launcher_plist_uses_a_stable_checkout_and_an_idle_safe_command(self):
        queue = mq.Queue(self.repo)
        app = self.base / "Queue Launcher.app"
        executable = app / "Contents" / "MacOS" / mq.LAUNCHER_PROGRAM
        executable.parent.mkdir(parents=True)
        executable.touch()
        args = self.arguments("launcher-plist", str(app))
        stable = self.base / "stable-toolkit"
        output = io.StringIO()
        with patch.object(mq.sys, "platform", "darwin"), patch.object(mq, "Queue", return_value=queue), \
                patch.object(mq, "text", return_value=str(stable / ".git")), \
                patch.object(mq.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as verify, \
                contextlib.redirect_stdout(output):
            self.assertEqual(mq.main(self.repo, args), 0)
        verify.assert_called_once_with(["/usr/bin/codesign", "--verify", "--strict",
                                        f'-R=anchor apple generic and identifier "{mq.LAUNCHER_BUNDLE}"', str(app)],
                                       capture_output=True, text=True, timeout=10)
        agent = plistlib.loads(output.getvalue().encode())
        self.assertEqual(agent["Label"], queue.launcher_label())
        self.assertEqual(agent["AssociatedBundleIdentifiers"], [mq.LAUNCHER_BUNDLE])
        self.assertEqual(agent["ProgramArguments"], [str(executable), *queue.command(stable, if_waiting=True)])
        self.assertTrue(agent["RunAtLoad"])
        self.assertEqual(agent["StartInterval"], mq.LAUNCHER_INTERVAL)

    def test_agent_entry_does_nothing_for_an_empty_held_or_running_queue(self):
        queue = mq.Queue(self.repo)
        args = self.arguments("run", "--if-waiting")
        with patch.object(mq.remote, "stop_dead_dispatches") as recover:
            self.assertEqual(mq.main(self.repo, args), 0)
            self.commit("a", "a")
            identifier, = self.add("a")
            queue.mark([queue.read(identifier)], mq.TESTING)
            queue.hold_file.write_text("held for this test")
            self.assertEqual(mq.main(self.repo, args), 0)
            self.assertEqual(queue.hold_file.read_text(), "held for this test")
            self.assertEqual(self.state(identifier)["state"], mq.TESTING)
            queue.hold_file.unlink()
            with queue.worker() as free:
                self.assertTrue(free)
                self.assertEqual(mq.main(self.repo, args), 0)
        recover.assert_not_called()
        self.assertEqual(self.gates(), 0)

    def test_agent_entry_drains_queued_and_interrupted_entries_after_restart(self):
        self.commit("a", "a")
        self.commit("b", "b")
        interrupted, waiting = self.add("a", "b")
        queue = mq.Queue(self.repo)
        # The earlier worker left the first entry testing. No process holds the worker lock.
        queue.mark([queue.read(interrupted)], mq.TESTING)
        args = self.arguments("run", "--if-waiting")
        self.assertEqual(mq.main(self.repo, args), 0)
        self.assertEqual([self.state(identifier)["state"] for identifier in (interrupted, waiting)],
                         [mq.LANDED, mq.LANDED])
        self.assertLessEqual({"a", "b"}, self.main_files())
        self.assertEqual(self.gates(), 1)


class LostLocalNetwork(Fixture):
    """A worker whose process macOS denies the local network, as once the session that started it ends, exits before a
    round, holding nothing, rather than gate everything on this Mac (vocaro-engineering#282)."""
    LOST = "macOS holds itself responsible for this process (PID 1), a Python with no app of its own"

    def test_a_worker_that_lost_the_local_network_exits_holding_nothing_before_a_gate(self):
        self.commit("a", "a")
        [identifier] = self.add("a")
        with farm_fixture.only("fake-mac"), \
                patch.object(mq.remote, "local_network_lost", return_value=self.LOST), \
                contextlib.redirect_stdout(io.StringIO()) as said:
            self.assertEqual(self.work(), 0)
        self.assertEqual(self.state(identifier)["state"], "queued")
        self.assertIsNone(mq.Queue(self.repo).held())
        self.assertEqual(self.gates(), 0)
        self.assertIn("exits, holding nothing, since this worker cannot reach another Mac: " + self.LOST,
                      said.getvalue())
        self.assertFalse(mq.Queue(self.repo).working())
        # The next worker, a live session's, lands it.
        with farm_fixture.only("fake-mac"), \
                patch.object(mq.remote, "local_network_lost", return_value=None), patch("builtins.print"):
            self.assertEqual(self.work(), 0)
        self.assertEqual(self.state(identifier)["state"], "landed")

    @contextlib.contextmanager
    def losing_access_at_the_gate(self, status):
        """Access is there at the round's check and gone once the gate's lane ran: the lane's qk run, whose
        environment is kept, exits status without running anything, as a real one that found NoLocalNetwork does
        (cli's run; test_remote's RemoteTests). Yields the environments of those runs."""
        real, ran = mq.processes.run, []
        def lane(command, *arguments, **options):
            if command[:3] != ["python3", "scripts/qk", "run"]: return real(command, *arguments, **options)
            ran.append(options["env"])
            return status
        with farm_fixture.only("fake-mac"), \
                patch.object(mq.remote, "local_network_lost", side_effect=lambda: self.LOST if ran else None), \
                patch.object(mq.processes, "run", lane):
            yield ran

    def test_a_gate_whose_lane_lost_the_local_network_exits_holding_nothing_and_counts_no_run(self):
        """vocaro-engineering#300: as before a round (#282), not a no-verdict run toward queue.noVerdictRuns."""
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.losing_access_at_the_gate(mq.remote.LOST_ACCESS) as ran, \
                contextlib.redirect_stdout(io.StringIO()) as said:
            self.assertEqual(self.work(), 0)
        self.assertEqual(len(ran), 1)
        self.assertEqual(ran[0][mq.remote.QUEUE_GATE], "1")
        self.assertEqual(self.state(identifier)["state"], "queued")
        self.assertIsNone(mq.Queue(self.repo).held())
        self.assertEqual(self.gates(), 0)
        self.assertIn("exits, holding nothing, since this worker cannot reach another Mac: " + self.LOST, said.getvalue())
        self.assertNotIn("of at most", said.getvalue())
        self.assertFalse(mq.Queue(self.repo).working())
        # The next worker, a live session's, lands it.
        with farm_fixture.only("fake-mac"), \
                patch.object(mq.remote, "local_network_lost", return_value=None), patch("builtins.print"):
            self.assertEqual(self.work(), 0)
        self.assertEqual(self.state(identifier)["state"], "landed")

    def test_a_worker_with_no_farm_mac_never_asks(self):
        self.commit("a", "a")
        [identifier] = self.add("a")
        with patch.object(mq.remote, "local_network_lost", return_value=self.LOST) as asked, patch("builtins.print"):
            self.assertEqual(self.work(), 0)
        self.assertEqual(self.state(identifier)["state"], "landed")
        asked.assert_not_called()


class LandedCommits(Fixture):
    """Each entry records and prints its own commit on main, not the batch's tip (vocaro-engineering#184)."""

    def on_main(self, subject):
        run(self.repo, "git", "fetch", "-q", "origin")
        return run(self.repo, "git", "log", "--format=%H", "-1", f"--grep=^{subject}$", "origin/main")

    def tip(self):
        run(self.repo, "git", "fetch", "-q", "origin")
        return run(self.repo, "git", "rev-parse", "origin/main")

    def waited(self, identifier):
        with contextlib.redirect_stdout(io.StringIO()) as said:
            self.assertEqual(mq.wait(self.repo, identifier), 0)
        return said.getvalue()

    def test_a_single_entry_is_the_tip(self):
        self.commit("a", "a")
        [identifier] = self.add("a")
        self.work()
        entry, tip = self.state(identifier), self.tip()
        self.assertEqual((entry["landedAs"], entry["landedTip"]), (tip, tip))
        self.assertEqual(entry["landedCommits"], 1)
        said = self.waited(identifier)
        self.assertEqual(said.rsplit("landed: ", 1)[1].strip(), tip)
        self.assertIn(f"-> {tip[:12]}\n", said)
        self.assertNotIn("batch tip", said)
        self.assertNotIn("commits)", said)

    def test_an_entry_below_the_tip_names_its_own_commit_and_the_tip(self):
        self.commit("a", "a"); self.commit("b", "b"); self.commit("c", "c")
        first, middle, last = self.add("a", "b", "c")
        self.work()
        tip = self.tip()
        for identifier, subject in ((first, "Add a"), (middle, "Add b"), (last, "Add c")):
            entry = self.state(identifier)
            self.assertEqual(entry["landedAs"], self.on_main(subject))
            self.assertEqual(entry["landedTip"], tip)
        own = self.state(first)["landedAs"]
        self.assertNotEqual(own, tip)  # the bug: every entry named the tip
        said = self.waited(first)
        self.assertEqual(said.rsplit("landed: ", 1)[1].strip(), own)
        self.assertIn(f"-> {own[:12]}, in batch tip {tip[:12]}", said)
        # The control: the entry whose commit is the tip says nothing of a batch tip.
        said = self.waited(last)
        self.assertEqual(said.rsplit("landed: ", 1)[1].strip(), tip)
        self.assertNotIn("batch tip", said)

    def test_a_multi_commit_entry_names_its_last_commit_and_counts_them(self):
        self.commit("m1", "multi")
        for name in ("m2", "m3"):
            (self.repo / name).write_text("x")
            run(self.repo, "git", "add", name)
            run(self.repo, "git", "commit", "-qm", f"Add {name}")
        self.commit("z", "z")
        multi, single = self.add("multi", "z")
        self.work()
        tip, entry = self.tip(), self.state(multi)
        self.assertEqual(entry["landedAs"], self.on_main("Add m3"))
        self.assertEqual(entry["landedCommits"], 3)
        self.assertEqual(run(self.repo, "git", "rev-list", "--count", f"{self.on_main('Add m1')}^..{entry['landedAs']}"),
                         "3")
        said = self.waited(multi)
        self.assertIn(f"-> {entry['landedAs'][:12]} (3 commits), in batch tip {tip[:12]}", said)
        self.assertEqual(said.rsplit("landed: ", 1)[1].strip(), entry["landedAs"])
        # The control: a one-commit entry gives no count.
        self.assertEqual(self.state(single)["landedAs"], tip)
        self.assertNotIn("commits)", self.waited(single))

    def test_commits_main_already_had_leave_the_tip(self):
        worker = mq.Worker.__new__(mq.Worker)
        worker.checkout = self.repo
        base = run(self.repo, "git", "rev-parse", "origin/main")
        tip = self.commit("a", "a")
        other = self.commit("b", "b")
        # An entry none of whose commits base..tip holds is left out, and records the tip.
        own = worker.landed([{"id": "a", "commits": [tip]}, {"id": "b", "commits": [other]}], base, tip)
        self.assertEqual(own, {"a": (tip, 1)})


class PassedThroughAHold(Fixture):
    """A batch whose lanes passed keeps that verdict through a hold its push meets, and the next worker pushes
    it without gating the same tree again (vocaro-engineering#150)."""
    def refused(self, *branches):
        """Queue branches and run a worker whose push the remote refuses, as GitHub's SSH did on 2026-11-04."""
        ids = self.add(*branches)
        # Every try, past the retries a refused key has (vocaro-engineering#151), on a remote that is not GitHub's.
        with self.remote_says(REFUSED["ssh key"], range(1, 99), "push"), patch("builtins.print"):
            self.assertEqual(self.work(), 1)
        return ids

    def resume(self):
        """`qk queue resume` with its worker run here, in the foreground."""
        mq.Queue(self.repo).hold_file.unlink()
        with patch("builtins.print"): return self.work()

    def test_a_refused_push_keeps_the_pass_and_resume_pushes_it_without_a_gate(self):
        self.commit("a", "a"); self.commit("b", "b")
        ids = self.refused("a", "b")
        self.assertEqual([self.state(i)["state"] for i in ids], ["passed"] * 2)
        verdict = self.state(ids[0])["passed"]
        self.assertEqual(verdict["batch"], ids)
        self.assertEqual(verdict["base"], run(self.repo, "git", "rev-parse", "origin/main"))
        held = mq.Queue(self.repo).held()
        self.assertIn("Permission denied (publickey)", held)
        self.assertIn(f"passed {verdict['head'][:12]} on {verdict['base'][:12]} (2 entries)", held)
        self.assertIn("lands it without gating it again", mq.describe(self.state(ids[0])))
        self.assertEqual(self.gates(), 1)
        self.assertEqual(self.resume(), 0)
        self.assertEqual([self.state(i)["state"] for i in ids], ["landed"] * 2)
        self.assertEqual(self.gates(), 1)
        # The batch's tip is the commit that passed; each entry, pushed by a worker that picked nothing, its own (#184).
        self.assertEqual([self.state(i)["landedTip"] for i in ids], [verdict["head"]] * 2)
        self.assertEqual(self.state(ids[1])["landedAs"], verdict["head"])
        self.assertEqual(self.state(ids[0])["landedAs"], run(self.repo, "git", "rev-parse", f"{verdict['head']}^"))
        self.assertLessEqual({"a", "b"}, self.main_files())
        # The landed entries still record the gate that passed them, for its budget.
        self.assertEqual(self.state(ids[0])["gate"], verdict["gate"])

    def test_without_a_pass_the_next_worker_gates_again(self):
        """Negative control: a batch a worker left testing was never judged, so it is gated again."""
        self.commit("a", "a")
        [identifier] = self.add("a")
        queue = mq.Queue(self.repo)
        queue.mark([queue.read(identifier)], "testing")
        with patch("builtins.print"): self.assertEqual(self.work(), 0)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(self.gates(), 1)

    def test_main_moving_by_files_no_lane_reads_carries_the_pass_over(self):
        self.commit("a", "a")
        [identifier] = self.refused("a")
        self.commit("records/note", "other")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        self.assertEqual(self.resume(), 0)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(self.gates(), 1)
        self.assertLessEqual({"a", "records"}, self.main_files())

    def test_main_moving_by_files_a_lane_reads_gates_again(self):
        self.commit("a", "a")
        [identifier] = self.refused("a")
        first = self.state(identifier)["passed"]
        self.commit("other", "other")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        self.assertEqual(self.resume(), 0)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(self.gates(), 2)
        # What landed is the pass of the second gate, on the main that moved.
        self.assertNotEqual(self.state(identifier)["passed"]["base"], first["base"])
        self.assertEqual(self.state(identifier)["landedAs"], self.state(identifier)["passed"]["head"])

    def test_an_entry_removed_during_the_hold_gates_what_is_left(self):
        self.commit("a", "a"); self.commit("b", "b")
        ids = self.refused("a", "b")
        with patch("builtins.print"): mq.remove(mq.Queue(self.repo), ids[0])
        self.assertEqual(self.resume(), 0)
        self.assertEqual(self.state(ids[1])["state"], "landed")
        self.assertEqual(self.gates(), 2)
        self.assertNotIn("a", self.main_files())

    def test_a_branch_added_again_during_the_hold_gates_again(self):
        self.commit("a", "a")
        [first] = self.refused("a")
        self.commit("a2", "a", start="a")
        [second] = self.add("a")
        self.assertEqual(self.state(first)["state"], "removed")
        self.assertEqual(self.resume(), 0)
        self.assertEqual(self.state(second)["state"], "landed")
        self.assertEqual(self.gates(), 2)
        self.assertLessEqual({"a", "a2"}, self.main_files())

    def test_an_entry_added_during_the_hold_waits_for_the_next_round(self):
        self.commit("a", "a")
        [first] = self.refused("a")
        self.commit("b", "b")
        [second] = self.add("b")
        self.assertEqual(self.resume(), 0)
        self.assertEqual([self.state(first)["state"], self.state(second)["state"]], ["landed", "landed"])
        # One gate for a, before the hold, and one for b after it.
        self.assertEqual(self.gates(), 2)
        self.assertEqual(self.state(first)["landedAs"], self.state(first)["passed"]["head"])

    def test_a_push_that_went_through_before_the_worker_died_lands_as_it_is(self):
        self.commit("a", "a")
        [identifier] = self.refused("a")
        head = self.state(identifier)["passed"]["head"]
        run(self.repo, "git", "push", "-q", "--no-verify", "origin", f"{head}:refs/heads/main")
        self.assertEqual(self.resume(), 0)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(self.gates(), 1)
        self.assertEqual(run(self.repo, "git", "rev-parse", "origin/main"), head)

    def test_a_commit_that_is_gone_gates_again(self):
        self.commit("a", "a")
        [identifier] = self.refused("a")
        queue = mq.Queue(self.repo)
        entry = queue.read(identifier)
        entry["passed"]["head"] = "0" * 40
        queue.save(entry)
        self.assertEqual(self.resume(), 0)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(self.gates(), 2)


class Killed(BaseException):
    """The worker's death at a point, which nothing in the worker catches, as SIGKILL leaves nothing to."""


class PublicationUnverified(Fixture):
    """A push cut off before its answer came, by the worker's death, a timeout or a dropped connection, may have
    landed: its entries keep the push (publishing), publication-unverified, until a fetch of origin/main says, and
    the next round lands what main holds without pushing or gating it again, and pushes the rest."""
    def pushes(self, then=None, calls=None):
        """git with each push run as then says: "before" dies before it runs, "after" dies once it went through,
        "timeout" times out once it went through, "timeout-unsent" times out without it; None runs it. calls, the
        pushes made, numbered from 1, that then applies to (every push when None); seen gets each push made."""
        real, seen = mq.git, []
        def git(directory, *arguments, **options):
            if not arguments or arguments[0] != "push": return real(directory, *arguments, **options)
            seen.append(arguments)
            if then is None or (calls is not None and len(seen) not in calls):
                return real(directory, *arguments, **options)
            if then in ("after", "timeout"): self.assertEqual(real(directory, *arguments, **options).returncode, 0)
            if then.startswith("timeout"): raise subprocess.TimeoutExpired(["git", *arguments], mq.GIT_TIMEOUT)
            raise Killed(then)
        return patch.object(mq, "git", git), seen

    def die(self, then, *branches):
        """Queue branches and run a worker that dies at then of its push (pushes)."""
        ids = self.add(*branches)
        dying, _ = self.pushes(then)
        with dying, patch("builtins.print"), self.assertRaises(Killed): self.work()
        return ids

    def restart(self):
        """The next worker, with the pushes it makes."""
        counting, seen = self.pushes()
        with counting, patch("builtins.print"): self.assertEqual(self.work(), 0)
        return seen

    def origin_main(self):
        run(self.repo, "git", "fetch", "-q", "origin")
        return run(self.repo, "git", "rev-parse", "origin/main")

    def move_main(self, name="later"):
        """Another landing on top of origin/main, by a file the gate's lane reads."""
        self.commit(name, name)
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")

    def reconciled(self):
        return [event for event in mq.events.read(mq.Queue(self.repo).directory, None, ())
                if event.get("kind") == "batch.pushed" and event.get("reconciled")]

    def test_a_worker_killed_before_its_push_leaves_it_unverified_and_the_next_pushes_it(self):
        self.commit("a", "a")
        [identifier] = self.die("before", "a")
        entry = self.state(identifier)
        self.assertEqual(entry["state"], "passed")
        head = entry["passed"]["head"]
        self.assertEqual([attempt["head"] for attempt in entry["publishing"]["attempts"]], [head])
        self.assertNotEqual(self.origin_main(), head)
        result = agent_results.lookup(mq.Queue(self.repo), identifier)
        self.assertEqual(result["publication"], {"status": "unverified", "heads": [head]})
        self.assertEqual(result["recovery"]["reasonCode"], "publication-unverified")
        self.assertIn("publication-unverified", mq.describe(entry))
        self.assertEqual(len(self.restart()), 1)
        entry = self.state(identifier)
        self.assertEqual((entry["state"], entry["landedAs"]), ("landed", head))
        self.assertNotIn("publishing", entry)
        self.assertEqual(self.origin_main(), head)
        self.assertEqual(self.gates(), 1)
        self.assertEqual(self.reconciled(), [])

    def test_a_rejected_last_attempt_does_not_send_back_a_push_origin_already_took(self):
        self.commit("a", "a")
        [identifier] = self.add("a")
        real = mq.git
        pushed = []

        def git(directory, *arguments, **options):
            if not arguments or arguments[0] != "push": return real(directory, *arguments, **options)
            pushed.append(arguments)
            if len(pushed) == 1:
                self.assertEqual(real(directory, *arguments, **options).returncode, 0)
                return subprocess.CompletedProcess(arguments, 1, "", UNREACHED["ssh dropped"])
            return subprocess.CompletedProcess(arguments, 1, "", REFUSED["rejected"])

        with patch.object(mq, "git", git), patch.object(mq, "pause"), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.work(), 0)
        self.assertEqual(len(pushed), 2)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertIsNone(mq.Queue(self.repo).held())

    def test_a_rejected_retry_keeps_an_earlier_unanswered_push_until_it_reconciles(self):
        self.commit("a", "a")
        [identifier] = self.add("a")
        real = mq.git
        attempted = []

        def git(directory, *arguments, **options):
            if not arguments or arguments[0] != "push": return real(directory, *arguments, **options)
            attempted.append(arguments)
            said = UNREACHED["ssh dropped"] if len(attempted) == 1 else REFUSED["rejected"]
            return subprocess.CompletedProcess(arguments, 1, "", said)

        queue = mq.Queue(self.repo)
        with patch.object(mq, "git", git), patch.object(mq, "pause"), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.work(), 1)
        entry = self.state(identifier)
        head = entry["passed"]["head"]
        self.assertTrue(any(attempt.get("unanswered") for marked in entry["pushedBefore"]
                            for attempt in marked["attempts"]))
        self.assertEqual(entry["state"], "passed")
        result = agent_results.lookup(queue, identifier)
        self.assertEqual(result["recovery"]["reasonCode"], "publication-unverified")
        self.assertEqual(result["publication"], {"status": "unverified", "heads": [head]})
        self.assertIn("an earlier push may still land", queue.held())
        run(self.repo, "git", "push", "-q", "origin", f"{head}:refs/heads/main")
        queue.hold_file.unlink()
        self.assertEqual(self.restart(), [])
        self.assertEqual(self.state(identifier)["landedAs"], head)

    def test_a_hook_refusal_preserves_unanswered_history_from_another_worker(self):
        self.commit("a", "a")
        [identifier] = self.die("before", "a")
        queue = mq.Queue(self.repo)
        hook = self.repo / ".git/hooks/pre-push"
        host_isolation.executable(hook, "#!/bin/sh\nexit 1\n")
        with patch.object(mq, "pause"), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.work(), 1)
        entry = self.state(identifier)
        self.assertEqual(entry["state"], "passed")
        [kept] = entry["pushedBefore"]
        head = entry["passed"]["head"]
        self.assertEqual(kept["attempts"][0]["head"], head)
        self.assertNotIn("publishing", entry)
        self.assertEqual(agent_results.lookup(queue, identifier)["publication"],
                         {"status": "unverified", "heads": [head]})

    def test_a_worker_killed_after_its_push_went_through_lands_it_without_pushing_again(self):
        self.commit("a", "a")
        [identifier] = self.die("after", "a")
        head = self.state(identifier)["passed"]["head"]
        self.assertEqual(self.origin_main(), head)
        self.assertEqual(self.restart(), [])
        entry = self.state(identifier)
        self.assertEqual((entry["state"], entry["landedAs"], entry["landedTip"]), ("landed", head, head))
        self.assertEqual(entry["gatedOn"], entry["passed"]["base"])
        self.assertEqual(self.gates(), 1)
        [event] = self.reconciled()
        self.assertEqual((event["head"], event["entries"]), (head, [identifier]))

    def test_a_push_that_landed_before_main_moved_on_lands_without_a_gate_or_a_second_copy(self):
        """Main moved past the push by a file the lane reads, and by a change to the batch's own file, which a carry
        or a second gate would cherry-pick onto main again: the fetch finds the push in main's history instead."""
        self.commit("a", "a")
        [identifier] = self.die("after", "a")
        head = self.state(identifier)["passed"]["head"]
        self.commit("a", "edit", content="edited", start="origin/main")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        self.move_main()
        moved = self.origin_main()
        self.assertEqual(self.restart(), [])
        entry = self.state(identifier)
        self.assertEqual((entry["state"], entry["landedAs"]), ("landed", head))
        self.assertEqual(self.gates(), 1)
        self.assertEqual(self.origin_main(), moved)
        self.assertEqual(run(self.repo, "git", "show", "origin/main:a"), "edited")

    def test_a_worker_killed_while_settling_the_batch_lands_the_rest(self):
        """The rest of the batch lands as the push that carried it, without a second gate, even once main moved on
        past that push: the batch that passed is no longer whole, and what main holds of the rest is no commit of its
        own there, so only the push's record (publishing) knows it."""
        self.commit("a", "a"); self.commit("b", "b")
        ids = self.add("a", "b")
        real, settled = mq.Queue.settle, []
        def settle(queue, entry, state, **fields):
            if settled: raise Killed("settling")
            settled.append(entry["id"])
            return real(queue, entry, state, **fields)
        with patch.object(mq.Queue, "settle", settle), patch("builtins.print"), self.assertRaises(Killed):
            self.work()
        self.assertEqual([self.state(i)["state"] for i in ids], ["landed", "passed"])
        head = self.state(ids[1])["passed"]["head"]
        self.move_main()
        moved = self.origin_main()
        self.assertEqual(self.restart(), [])
        self.assertEqual([self.state(i)["state"] for i in ids], ["landed", "landed"])
        self.assertEqual(self.state(ids[1])["landedAs"], head)
        self.assertEqual([self.state(i)["landedTip"] for i in ids], [head, head])
        self.assertEqual(self.gates(), 1)
        self.assertEqual(self.origin_main(), moved)

    def test_a_worker_killed_mid_carry_pushes_the_whole_batch_that_passed(self):
        """A carry resets the checkout and picks the batch again; a worker that died between picks left part of it
        there, which the next worker must not push as what passed. Worker.settings leaves the checkout at the head
        that passed as well, and did so before push_passed reset it there itself, so the code before 3b82cbe624e9
        passes this test too: it guards both (vocaro/queuekeeper#265)."""
        self.commit("a", "a"); self.commit("b", "b")
        ids = self.add("a", "b")
        refusing, _ = self.pushes("before")
        with refusing, patch("builtins.print"), self.assertRaises(Killed): self.work()
        self.commit("records/note", "note")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        real, picked = mq.Worker.pick, []
        def pick(worker, entry, ahead=()):
            if picked: raise Killed("carrying")
            picked.append(entry["id"])
            return real(worker, entry, ahead)
        with patch.object(mq.Worker, "pick", pick), patch("builtins.print"), self.assertRaises(Killed): self.work()
        self.assertEqual(picked, [ids[0]])
        self.restart()
        self.assertEqual([self.state(i)["state"] for i in ids], ["landed", "landed"])
        self.assertLessEqual({"a", "b", "records"}, self.main_files())
        self.assertEqual(self.gates(), 1)

    def test_a_push_that_timed_out_after_landing_settles_on_the_rounds_next_try(self):
        self.commit("a", "a")
        [identifier] = self.add("a")
        timing_out, seen = self.pushes("timeout", calls={1})
        said = io.StringIO()
        with timing_out, patch.object(mq, "pause"), contextlib.redirect_stdout(said):
            self.assertEqual(self.work(), 0)
        self.assertEqual(len(seen), 1)
        entry = self.state(identifier)
        self.assertEqual((entry["state"], entry["landedAs"]), ("landed", self.origin_main()))
        self.assertIn("publication-unverified: the push of", said.getvalue())
        self.assertIn("pushed before its answer came", said.getvalue())
        self.assertEqual(len(self.reconciled()), 1)

    def test_a_push_that_timed_out_unsent_is_pushed_again_on_the_rounds_next_try(self):
        self.commit("a", "a")
        [identifier] = self.add("a")
        timing_out, seen = self.pushes("timeout-unsent", calls={1})
        with timing_out, patch.object(mq, "pause"), patch("builtins.print"): self.assertEqual(self.work(), 0)
        self.assertEqual(len(seen), 2)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(self.gates(), 1)
        self.assertEqual(self.reconciled(), [])

    def test_pushes_that_never_answer_hold_as_unverified_until_a_fetch_settles_them(self):
        self.commit("a", "a")
        [identifier] = self.add("a")
        timing_out, _ = self.pushes("timeout-unsent")
        with timing_out, patch.object(mq, "pause"), patch("builtins.print"): self.assertEqual(self.work(), 1)
        held = mq.Queue(self.repo).held()
        self.assertIn("publication-unverified", held)
        self.assertTrue(held.endswith(f"which the resume pushes without gating again{HoldKinds.SESSION_RESUMES}"), held)
        entry = self.state(identifier)
        self.assertEqual(entry["state"], "passed")
        self.assertTrue(entry["publishing"]["unanswered"])
        result = agent_results.lookup(mq.Queue(self.repo), identifier)
        self.assertEqual(result["publication"]["status"], "unverified")
        self.assertEqual(result["recovery"]["reasonCode"], "publication-unverified")
        self.assertEqual(result["recovery"]["action"], "inspect-hold")
        mq.Queue(self.repo).hold_file.unlink()
        self.assertEqual(len(self.restart()), 1)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(self.gates(), 1)

    def test_a_dropped_connection_whose_push_landed_before_main_moved_lands_as_it_is(self):
        """The push went through and its answer was lost; by the retry main had moved on, so the retry is rejected,
        and the fetch after it finds the first push in main's history rather than carrying the batch over."""
        self.commit("a", "a")
        [identifier] = self.add("a")
        real, seen = mq.git, []
        def git(directory, *arguments, **options):
            if arguments and arguments[0] == "push":
                seen.append(arguments)
                done = real(directory, *arguments, **options)
                if len(seen) == 1:
                    self.move_main()
                    hung_up = "fatal: the remote end hung up unexpectedly\n"
                    return subprocess.CompletedProcess(done.args, 128, "", hung_up)
                return done
            return real(directory, *arguments, **options)
        with patch.object(mq, "git", git), patch.object(mq, "pause"), patch("builtins.print"):
            self.assertEqual(self.work(), 0)
        self.assertEqual(len(seen), 2)
        entry = self.state(identifier)
        self.assertEqual((entry["state"], entry["landedAs"]), ("landed", entry["passed"]["head"]))
        self.assertEqual(self.gates(), 1)

    def orphaned(self, script, *branches):
        """Queue branches and run a worker killed alone mid-push, its git push left running as an orphan, as SIGKILL
        to the worker's Python process leaves it, which launchd reaps once it ends: script, a shell command, runs
        before the push, which it skips by exec-ing something else. The ids and the orphan's record, its pid and
        start, which the test ends if it outlives the test."""
        ids = self.add(*branches)
        real, orphans = mq.git, []
        def git(directory, *arguments, **options):
            if not arguments or arguments[0] != "push": return real(directory, *arguments, **options)
            launched = subprocess.run(
                ["/bin/sh", "-c", f'({script}; exec git -C "$0" "$@") </dev/null >/dev/null 2>&1 & echo $!',
                 str(directory), *arguments], capture_output=True, text=True, check=True, env=mq.environment())
            pid = int(launched.stdout)
            orphans.append({"pid": pid, "start": mq.pushing_start(pid)})
            self.addCleanup(lambda orphan=orphans[-1]: mq.push_runs(orphan) and os.kill(orphan["pid"], signal.SIGKILL))
            options["started"](pid)
            raise Killed("mid-push")
        with patch.object(mq, "git", git), patch("builtins.print"), self.assertRaises(Killed): self.work()
        [orphan] = orphans
        self.assertTrue(mq.push_runs(orphan))
        return ids, orphan

    def test_a_push_that_outlives_its_killed_worker_is_waited_out_and_lands_without_a_second_push(self):
        """vocaro/queuekeeper#265: the dead worker's push lands seconds after the next worker starts. Its fetch
        must not say it did not land before it ends, or the next worker pushes the batch again over it."""
        self.commit("a", "a")
        [identifier], orphan = self.orphaned("sleep 3", "a")
        [attempt] = self.state(identifier)["publishing"]["attempts"]
        self.assertEqual((attempt["pid"], attempt["start"]), (orphan["pid"], orphan["start"]))
        self.assertEqual(self.restart(), [])
        self.assertFalse(mq.push_runs(orphan))
        entry = self.state(identifier)
        self.assertEqual((entry["state"], entry["landedAs"]), ("landed", attempt["head"]))
        self.assertEqual(self.origin_main(), attempt["head"])
        self.assertEqual(self.gates(), 1)
        [event] = self.reconciled()
        self.assertEqual(event["head"], attempt["head"])

    def test_a_dead_workers_push_that_runs_past_its_timeout_is_ended_and_pushed_again(self):
        self.commit("a", "a")
        [identifier], orphan = self.orphaned("exec sleep 600", "a")
        said = io.StringIO()
        counting, seen = self.pushes()
        with patch.object(mq, "GIT_TIMEOUT", 1), patch.object(mq, "PUSH_SETTLE", 1), counting, \
                contextlib.redirect_stdout(said):
            self.assertEqual(self.work(), 0)
        self.assertFalse(mq.push_runs(orphan))
        self.assertIn("ending the push of", said.getvalue())
        self.assertEqual(len(seen), 1)
        entry = self.state(identifier)
        self.assertEqual((entry["state"], entry["landedAs"]), ("landed", self.origin_main()))
        self.assertEqual(self.gates(), 1)
        self.assertEqual(self.reconciled(), [])

    def test_a_recorded_push_pid_that_now_runs_another_process_is_not_waited_for(self):
        self.commit("a", "a")
        [identifier] = self.die("before", "a")
        entry = self.state(identifier)
        stranger = subprocess.Popen(["/bin/sleep", "600"])
        self.addCleanup(lambda: (stranger.kill(), stranger.wait()))
        attempts = [{**attempt, "pid": stranger.pid, "start": "Thu Jan  1 00:00:00 1970"}
                    for attempt in entry["publishing"]["attempts"]]
        mq.Queue(self.repo).record(entry, publishing={**entry["publishing"], "attempts": attempts})
        self.assertEqual(len(self.restart()), 1)
        self.assertIsNone(stranger.poll())
        self.assertEqual(self.state(identifier)["state"], "landed")

    def test_a_push_that_lands_after_reconcile_found_it_missing_still_lands_once(self):
        """vocaro/queuekeeper#265: a push ended mid-flight can land once the remote applies it, after the next
        worker's fetch said it had not, and main can move on past it. The entry keeps that push (pushedBefore), so
        the next worker's push finds it in main's history rather than carrying commits main already holds."""
        self.commit("a", "a")
        [identifier] = self.die("before", "a")
        head = self.state(identifier)["passed"]["head"]
        real, late = mq.Worker.reconcile, []
        def reconcile(worker):
            done = real(worker)
            if not late:
                late.append(self.state(identifier))
                run(self.repo, "git", "push", "-q", "origin", f"{head}:refs/heads/main")
                self.move_main()
            return done
        counting, seen = self.pushes()
        said = io.StringIO()
        with patch.object(mq.Worker, "reconcile", reconcile), counting, contextlib.redirect_stdout(said):
            self.assertEqual(self.work(), 0)
        self.assertEqual(seen, [])
        entry = self.state(identifier)
        self.assertEqual((entry["state"], entry["landedAs"]), ("landed", head))
        self.assertEqual(self.gates(), 1)
        self.assertEqual(run(self.repo, "git", "rev-list", "--count", f"{head}..origin/main"), "1")
        [event] = self.reconciled()
        self.assertEqual(event["head"], head)
        # The push's own fetch found it, before any carry over commits main then held.
        self.assertIn("an earlier push of part of the batch landed", said.getvalue())
        self.assertNotIn("carried the result over", said.getvalue())
        [kept] = late[0]["pushedBefore"]
        self.assertEqual([attempt["head"] for attempt in kept["attempts"]], [head])
        self.assertNotIn("publishing", late[0])
        self.assertNotIn("pushedBefore", entry)

class PushRefusedInAnIncident(Fixture):
    """A push refused for its credentials, seconds after a fetch from the same URL went through, is tried again:
    after each of BACKOFF, then, on GitHub, while its status says Git Operations is not operational, for up to
    SETTLE_LIMIT. GitHub's SSH refused the owner's key so for over an hour on 2026-11-04 (vocaro-engineering#151)."""
    @contextlib.contextmanager
    def incident(self, pushes, statuses=None, github=True):
        """The numbered pushes refused for the key, GitHub's Git Operations as statuses says at each look (the last
        repeating; None for no reading), and a clock that each pause moves. Yields the pushes, the pauses and what
        the worker said."""
        clock, slept, said = [1000.0], [], io.StringIO()
        def pause(seconds):
            slept.append(seconds)
            clock[0] += seconds
        looks = list(statuses or [("operational", None)])
        def status():
            self.looks += 1
            return looks[min(self.looks, len(looks)) - 1]
        self.looks = 0
        with self.remote_says(REFUSED["ssh key"], pushes, "push") as (calls, _), patch.object(mq, "pause", pause), \
                patch.object(mq, "now", lambda: clock[0]), patch.object(mq, "on_github", return_value=github), \
                patch.object(mq, "git_operations", side_effect=status), contextlib.redirect_stdout(said):
            yield calls, slept, said

    def test_a_refusal_that_clears_within_the_backoff_lands_without_another_gate(self):
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.incident({1, 2}) as (pushes, slept, said): self.assertEqual(self.work(), 0)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual((len(pushes), slept, self.gates(), self.looks), (3, list(mq.BACKOFF[:2]), 1, 0))
        self.assertIn("the push was refused as the fetch before it was not, so trying again in 10s", said.getvalue())

    def test_a_refusal_that_lasts_while_github_is_degraded_is_waited_out(self):
        self.commit("a", "a")
        [identifier] = self.add("a")
        tries = len(mq.BACKOFF) + 4
        with self.incident(range(1, tries), [("partial_outage", None)]) as (pushes, slept, said):
            self.assertEqual(self.work(), 0)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertEqual(slept, list(mq.BACKOFF) + [mq.REFUSAL_POLL] * 3)
        self.assertEqual((len(pushes), self.gates()), (tries, 1))
        self.assertIn("while GitHub's Git Operations is partial_outage", said.getvalue())

    def test_a_refusal_while_github_is_operational_holds_after_the_backoff_and_says_so(self):
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.incident(range(1, 99)) as (pushes, slept, _): self.assertEqual(self.work(), 1)
        self.assertEqual((len(pushes), slept, self.looks), (len(mq.BACKOFF) + 1, list(mq.BACKOFF), 1))
        held = mq.Queue(self.repo).held()
        self.assertIn("was refused: git@github.com: Permission denied (publickey).", held)
        self.assertIn("GitHub's Git Operations is operational", held)
        # The batch's pass stays for resume (vocaro-engineering#150).
        self.assertEqual(self.state(identifier)["state"], "passed")

    def test_a_refusal_that_outlasts_the_limit_holds_naming_githubs_status(self):
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.incident(range(1, 999), [("major_outage", None)]) as (pushes, slept, _):
            self.assertEqual(self.work(), 1)
        self.assertEqual(sum(slept), mq.SETTLE_LIMIT)
        self.assertIn(f"GitHub's Git Operations was still major_outage after {mq.SETTLE_LIMIT}s of tries",
                      mq.Queue(self.repo).held())
        self.assertEqual(self.state(identifier)["state"], "passed")
        self.assertEqual(self.gates(), 1)

    def test_an_unreadable_status_holds_after_the_backoff_and_says_why(self):
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.incident(range(1, 99), [(None, "GitHub's status could not be read: timed out")]) as (pushes, *_):
            self.assertEqual(self.work(), 1)
        self.assertEqual(len(pushes), len(mq.BACKOFF) + 1)
        self.assertIn("GitHub's status could not be read: timed out", mq.Queue(self.repo).held())

    def test_a_remote_that_is_not_githubs_holds_after_the_backoff_without_asking_github(self):
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.incident(range(1, 99), github=False) as (pushes, slept, _): self.assertEqual(self.work(), 1)
        self.assertEqual((len(pushes), slept, self.looks), (len(mq.BACKOFF) + 1, list(mq.BACKOFF), 0))
        self.assertNotIn("Git Operations", mq.Queue(self.repo).held())

    def test_a_push_url_other_than_the_fetchs_holds_at_once(self):
        """The fetch that went through says nothing of credentials it did not use."""
        run(self.repo, "git", "config", "remote.origin.pushurl", f"file://{self.origin}")
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.incident(range(1, 99)) as (pushes, slept, _): self.assertEqual(self.work(), 1)
        self.assertEqual((len(pushes), slept, self.looks), (1, [], 0))

    def test_a_refusal_of_another_kind_holds_at_once(self):
        """Negative control: a missing repository or failed host-key verification requires repository access."""
        for name in ("missing", "ssh host key"):
            with self.subTest(name):
                with contextlib.suppress(OSError): mq.Queue(self.repo).hold_file.unlink()
                self.commit(name.replace(" ", "-"), name.replace(" ", "-"))
                [identifier] = self.add(name.replace(" ", "-"))
                with patch("builtins.print"), self.remote_says(REFUSED[name], range(1, 99), "push") as (pushes, slept), \
                        patch.object(mq, "git_operations") as status:
                    self.assertEqual(self.work(), 1)
                self.assertEqual((len(pushes), slept), (1, []))
                status.assert_not_called()
                mq.remove(mq.Queue(self.repo), identifier, quiet=True)

    def test_main_moving_during_the_wait_is_carried_over_as_any_rejection(self):
        self.commit("a", "a")
        [identifier] = self.add("a")
        moved = []
        real = mq.git
        def git(directory, *arguments, **options):
            # The retry, the first push to get past the refusal: main moves first, by a path no lane reads, so
            # the push is rejected, not refused.
            if arguments[:1] == ("push",) and not moved and self.pushes == 0:
                moved.append(self.commit("records/note", "other"))
                run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
            if arguments[:1] == ("push",): self.pushes += 1
            return real(directory, *arguments, **options)
        self.pushes = 0
        with patch.object(mq, "git", git), self.incident({1}) as (pushes, slept, _): self.assertEqual(self.work(), 0)
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertLessEqual({"a", "records"}, self.main_files())
        self.assertEqual((self.gates(), len(moved)), (1, 1))

    def test_githubs_status_is_read_from_its_components(self):
        def answer(body):
            response = io.BytesIO(json.dumps(body).encode() if not isinstance(body, bytes) else body)
            return patch.object(mq.urllib.request, "urlopen", return_value=response)
        with answer({"components": [{"name": "API Requests", "status": "operational"},
                                    {"name": "Git Operations", "status": "partial_outage"}]}):
            self.assertEqual(mq.git_operations(), ("partial_outage", None))
        with answer({"components": [{"name": "API Requests", "status": "operational"}]}):
            self.assertEqual(mq.git_operations(), (None, "GitHub's status names no Git Operations"))
        with answer(b"<html>"):
            self.assertIn("could not be read", mq.git_operations()[1])
        with patch.object(mq.urllib.request, "urlopen", side_effect=OSError("timed out")):
            self.assertEqual(mq.git_operations(), (None, "GitHub's status could not be read: timed out"))

    def test_githubs_urls_are_told_from_others(self):
        for url in ("git@github.com:vocaro/x.git", "ssh://git@github.com/vocaro/x.git", "https://github.com/vocaro/x.git",
                    "https://token@github.com/vocaro/x.git"):
            with self.subTest(url): self.assertTrue(mq.on_github(url))
        for url in (str(self.origin), "https://github.com.example/x.git", "git@gitlab.com:x/y.git"):
            with self.subTest(url): self.assertFalse(mq.on_github(url))


class Refusals(Fixture):
    def test_pull_request_projects_are_refused(self):
        self.publish({"schemaVersion": 1, "name": "x", "integration": "pull-request",
                      "lanes": {"gate": {"command": ["true"]}}})
        with self.assertRaisesRegex(ValueError, "pull request"): mq.add(self.repo, "HEAD")

    def test_a_project_with_nothing_to_run_is_refused(self):
        self.publish({"schemaVersion": 1, "name": "x", "lanes": {"docs": {"command": ["true"]}}})
        with self.assertRaisesRegex(ValueError, "nothing to run"): mq.add(self.repo, "HEAD")

    def test_named_lanes_come_first_and_gate_is_the_default(self):
        self.assertEqual(mq.lanes_of({"queue": {"lanes": ["a"]}, "lanes": {"a": {}, "gate": {}}}), ["a"])
        self.assertEqual(mq.lanes_of({"lanes": {"gate": {}}}), ["gate"])
        self.assertEqual(mq.lanes_of({"lanes": {"gate": {}, "provision": {}, "lint": {}, "docs": {}},
                                      "queue": {"preflight": ["lint"]}}), ["lint", "gate"])

    def test_separate_provision_stays_when_another_queue_step_can_need_it(self):
        lanes = {"gate": {}, "provision": {}, "lint": {}, "docs": {}}
        config = {"lanes": lanes, "queue": {"preflight": ["lint"]}}
        self.assertEqual(mq.lanes_of(config, ["a.py", "README.md"]), ["lint", "provision", "docs", "gate"])
        self.assertEqual(mq.lanes_of(config, ["queuekeeper.json"]), ["lint", "provision", "gate"])
        self.assertIsNone(mq.offerable(config, mq.lanes_of(config, ["a.py"])))
        for policy in ({"hookCoveredBy": {"provision": "checks the hook's inputs"}},
                       {"reuseGate": {"gate": ["true"]}}, {"lanes": ["provision", "gate"]}):
            with self.subTest(policy):
                self.assertEqual(mq.lanes_of({**config, "queue": {**config["queue"], **policy}}, ["a.py"]),
                                 ["lint", "provision", "gate"])

    def test_a_lane_named_spelling_is_no_preflight_lane_unless_listed(self):
        """vocaro/queuekeeper#69: Queuekeeper has no spelling check of its own, and a lane's name says nothing of when
        the queue runs it: only queue.preflight makes a lane run first, in a round, an ungated landing and a docs-only
        batch."""
        own = {"gate": {}, "provision": {}, "spelling": {}}
        self.assertEqual(mq.lanes_of({"lanes": {"gate": {}}}), ["gate"])
        self.assertEqual(mq.lanes_of({"lanes": own}), ["gate"])
        self.assertEqual(mq.lanes_of({"lanes": own, "queue": {"preflight": ["spelling"]}}), ["spelling", "gate"])
        self.assertEqual(mq.ungated_lanes({"lanes": own}), [])
        self.assertEqual(mq.ungated_lanes({"lanes": own, "queue": {"preflight": ["spelling"]}}), ["spelling"])
        declared = {"paths": {"doc/": "no code lane reads it"}, "defaults": False}
        lanes = {"gate": {}, "docs": {}, "spelling": {}}
        self.assertEqual(mq.docs_lanes({"lanes": lanes, "queue": {"unread": declared}}, ["doc/a.md"]), ["docs"])
        self.assertEqual(mq.docs_lanes({"lanes": lanes, "queue": {"unread": declared, "preflight": ["spelling"]}},
                                       ["doc/a.md"]), ["spelling", "docs"])
        self.assertIsNone(mq.docs_lanes({"lanes": lanes, "queue": {"unread": declared}}, ["doc/a.md", "code"]))
        # A lane queue.unread.lanes names runs where it names it, once.
        named = {**declared, "lanes": ["docs", "spelling"]}
        self.assertEqual(mq.docs_lanes({"lanes": lanes, "queue": {"unread": named, "preflight": ["spelling"]}},
                                       ["doc/a.md"]), ["docs", "spelling"])
        # A lane the project lacks holds a bisect half on this Mac, spelling as any other; the toolkit's docs check
        # does not.
        self.assertIn("spelling runs only on this Mac: it is no configured lane",
                      mq.offerable({"lanes": {"gate": {}}}, ["spelling", "gate"]))
        self.assertIsNone(mq.offerable({"lanes": {"gate": {}}}, ["docs", "gate"]))

    def test_configuration_is_checked(self):
        config = json.loads((self.repo / "queuekeeper.json").read_text())
        for bad in ({"lanes": ["none"]}, {"other": 1}, {"unread": {"paths": {"x/": ""}}}):
            (self.repo / "queuekeeper.json").write_text(json.dumps({**config, "queue": bad}))
            with self.assertRaises(ValueError): self.config()

    def test_an_explicit_remote_rejection_sends_back_the_entry(self):
        self.commit("a", "a")
        [identifier] = self.add("a")
        hook = self.origin / "hooks/pre-receive"
        hook.write_text("#!/bin/sh\necho no >&2\nexit 1\n")
        host_isolation.executable(hook)
        self.assertEqual(self.work(), 0)
        entry = self.state(identifier)
        self.assertEqual(entry["state"], "failed")
        self.assertIn("pre-receive hook declined", entry["why"])
        self.assertNotIn("publishing", entry)
        self.assertIsNone(mq.Queue(self.repo).held())

    def test_a_push_whose_connection_dropped_is_pushed_again(self):
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.remote_says("fatal: the remote end hung up unexpectedly", {1}, "push") as (pushes, slept):
            self.work()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertIn("a", self.main_files())
        self.assertEqual(self.gates(), 1)
        self.assertEqual((len(pushes), slept), (2, [mq.BACKOFF[0]]))

    def test_a_push_that_keeps_dropping_holds_after_the_rounds_tries(self):
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.remote_says("fatal: the remote end hung up unexpectedly", range(1, 999), "push") as (pushes, slept), \
                patch("builtins.print"):
            self.assertEqual(self.work(), 1)
        self.assertEqual(self.state(identifier)["state"], "passed")
        held = mq.Queue(self.repo).held()
        self.assertIn("did not reach the remote", held)
        self.assertTrue(held.startswith("for a session, not the owner: "), held)
        rounds = len(mq.CLEARING_WAITS) + 1
        self.assertEqual((len(pushes), slept.count(mq.BACKOFF[-1])), (rounds * (len(mq.BACKOFF) + 1), rounds))
        # The pass stays through the tries: one gate.
        self.assertEqual(self.gates(), 1)

    def test_a_rejection_does_not_retry_even_if_the_hook_mentions_a_disconnect(self):
        """The remote answered: its hook's message is no dropped connection, which Git would print the same."""
        self.commit("a", "a")
        [identifier] = self.add("a")
        hook = self.origin / "hooks/pre-receive"
        hook.write_text('#!/bin/sh\necho "fatal: the remote end hung up unexpectedly" >&2\nexit 1\n')
        host_isolation.executable(hook)
        with patch.object(mq, "pause") as slept: self.assertEqual(self.work(), 0)
        entry = self.state(identifier)
        self.assertEqual(entry["state"], "failed")
        self.assertIn("pre-receive hook declined", entry["why"])
        self.assertIn("the remote end hung up unexpectedly", Path(entry["log"]).read_text())
        self.assertIsNone(mq.Queue(self.repo).held())
        slept.assert_not_called()

    def test_a_removed_entry_does_not_land(self):
        self.commit("a", "a"); self.commit("b", "b")
        ids = self.add("a", "b")
        queue = mq.Queue(self.repo)
        real = mq.Worker.gate
        def gate(worker, lanes, round_name):
            outcome = real(worker, lanes, round_name)
            if not queue.read(ids[0])["state"] == "removed":
                with patch("builtins.print"): mq.remove(queue, ids[0])
            return outcome
        with patch.object(mq.Worker, "gate", gate): self.work()
        self.assertEqual(self.state(ids[0])["state"], "removed")
        self.assertEqual(self.state(ids[1])["state"], "landed")
        self.assertNotIn("a", self.main_files())
        self.assertEqual(self.gates(), 2)

    def test_the_commit_that_first_names_the_lanes_lands_and_main_decides_after(self):
        """Negative control first: once main names its lanes, a branch cannot choose another."""
        config = json.loads((self.repo / "queuekeeper.json").read_text())
        lane = config["lanes"].pop("gate")
        config["lanes"]["checks"] = lane
        (self.repo / "queuekeeper.json").write_text(json.dumps(config))
        run(self.repo, "git", "add", "queuekeeper.json")
        run(self.repo, "git", "commit", "-qm", "No gate lane yet")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        run(self.repo, "git", "checkout", "-q", "-B", "name", "origin/main")
        config["queue"]["lanes"] = ["checks"]
        (self.repo / "queuekeeper.json").write_text(json.dumps(config))
        run(self.repo, "git", "add", "queuekeeper.json")
        run(self.repo, "git", "commit", "-qm", "Name the lanes")
        [named] = self.add("name")
        self.work()
        self.assertEqual(self.state(named)["state"], "landed")
        self.assertEqual(self.gates(), 1)
        self.assertIsNone(mq.Queue(self.repo).held())
        run(self.repo, "git", "fetch", "-q", "origin")
        run(self.repo, "git", "checkout", "-q", "-B", "skip", "origin/main")
        (self.repo / "bad").write_text("x")
        config["lanes"]["nothing"] = {"command": ["true"]}
        config["queue"]["lanes"] = ["nothing"]
        (self.repo / "queuekeeper.json").write_text(json.dumps(config))
        run(self.repo, "git", "add", "bad", "queuekeeper.json")
        run(self.repo, "git", "commit", "-qm", "Choose a gate that passes")
        [skip] = self.add("skip")
        self.work()
        self.assertEqual(self.state(skip)["state"], "failed")
        self.assertNotIn("bad", self.main_files())

    def without_configuration(self):
        """Main as a project's is before it adopts Queuekeeper, with no queuekeeper.json, and a branch adding one."""
        run(self.repo, "git", "rm", "-q", "queuekeeper.json")
        run(self.repo, "git", "commit", "-qm", "Before Queuekeeper")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        run(self.repo, "git", "checkout", "-q", "-B", "adopt", "origin/main")
        run(self.repo, "git", "checkout", "-q", "HEAD~1", "--", "queuekeeper.json")
        run(self.repo, "git", "commit", "-qm", "Add Queuekeeper")

    def test_commands_explain_how_to_adopt_when_the_project_has_no_configuration(self):
        self.without_configuration()
        run(self.repo, "git", "checkout", "-q", "--detach", "origin/main")
        project = [sys.executable, str(ROOT / "scripts/qk"), "--project", str(self.repo)]
        for words in (["run", "gate"], ["admit", "--", "true"], ["doctor", "--check-only"]):
            with self.subTest(words=words):
                done = subprocess.run([*project, *words], cwd=self.repo, capture_output=True, text=True)
                self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
                self.assertIn("has no queuekeeper.json in its working tree", done.stderr)
                self.assertIn("'Configure a project' in the user guide", done.stderr)
                self.assertNotIn("No such file or directory", done.stderr)

        done = subprocess.run([*project, "python-path", "queuekeeper"], cwd=self.repo, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

        (self.repo / "queuekeeper.json").write_text("{")
        done = subprocess.run([*project, "run", "gate"], cwd=self.repo, capture_output=True, text=True)
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertIn("Expecting property name", done.stderr)
        self.assertNotIn("has no queuekeeper.json", done.stderr)

    def test_a_deleted_working_configuration_is_not_reported_as_behind_adoption(self):
        configuration = self.repo / "queuekeeper.json"
        configuration.unlink()
        project = [sys.executable, str(ROOT / "bin/qk")]
        for staged in (False, True):
            with self.subTest(staged=staged):
                if staged: run(self.repo, "git", "add", "-u", "queuekeeper.json")
                done = subprocess.run([*project, "run", "gate"], cwd=self.repo, capture_output=True, text=True)
                self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
                self.assertIn("restore queuekeeper.json from HEAD", done.stderr)
                self.assertNotIn("behind the commit that added it", done.stderr)

    def test_the_commit_that_adds_the_configuration_lands_and_main_decides_after(self):
        """#10: the queue read main's queuekeeper.json alone, so it refused a project's first landing."""
        self.without_configuration()
        said = io.StringIO()
        with patch.object(mq.Queue, "start"), contextlib.redirect_stderr(said), contextlib.redirect_stdout(io.StringIO()):
            adopted = mq.add(self.repo, "adopt", quiet_wait=True)
        self.assertIn("origin/main has no queuekeeper.json yet", said.getvalue())
        said = io.StringIO()
        with contextlib.redirect_stdout(said): self.work()
        self.assertEqual(self.state(adopted)["state"], "landed")
        self.assertEqual(self.gates(), 1)
        self.assertIsNone(mq.Queue(self.repo).held())
        self.assertIn("has no queuekeeper.json yet: this round reads the batch's", said.getvalue())
        self.assertIn("queuekeeper.json", self.main_files())
        # Once main has one, a branch cannot choose a gate that passes.
        self.commit("bad", "skip")
        config = json.loads((self.repo / "queuekeeper.json").read_text())
        config["lanes"]["nothing"] = {"command": ["true"]}
        config["queue"]["lanes"] = ["nothing"]
        (self.repo / "queuekeeper.json").write_text(json.dumps(config))
        run(self.repo, "git", "commit", "-qam", "Choose a gate that passes")
        [skip] = self.add("skip")
        self.work()
        self.assertEqual(self.state(skip)["state"], "failed")
        self.assertNotIn("bad", self.main_files())

    def test_a_first_landing_runs_the_gate_it_brings(self):
        self.without_configuration()
        (self.repo / "bad").write_text("x")
        run(self.repo, "git", "add", "bad")
        run(self.repo, "git", "commit", "-qm", "Fail the gate")
        [adopted] = self.add("adopt")
        self.work()
        self.assertEqual(self.state(adopted)["state"], "failed")
        self.assertEqual(self.gates(), 1)
        self.assertNotIn("queuekeeper.json", self.main_files())

    def test_a_first_landing_cannot_skip_the_gate_by_its_docs_paths(self):
        """What only main may say, a branch does not say for itself while main has no configuration."""
        self.without_configuration()
        config = json.loads((self.repo / "queuekeeper.json").read_text())
        config["lanes"]["nothing"] = {"command": ["true"]}
        config["queue"]["unread"] = {"paths": {"queuekeeper.json": "it says so", "bad": "it says so"},
                                     "lanes": ["nothing"], "defaults": False}
        (self.repo / "queuekeeper.json").write_text(json.dumps(config))
        (self.repo / "bad").write_text("x")
        run(self.repo, "git", "add", "bad", "queuekeeper.json")
        run(self.repo, "git", "commit", "-qm", "Call everything docs")
        [adopted] = self.add("adopt")
        self.work()
        self.assertEqual(self.state(adopted)["state"], "failed")
        self.assertNotIn("bad", self.main_files())

    def test_a_commit_is_refused_when_neither_it_nor_main_has_a_configuration(self):
        self.without_configuration()
        self.commit("other", "other")
        with self.assertRaisesRegex(ValueError, "neither origin/main nor .* has a queuekeeper.json"): self.add("other")
        self.assertEqual(mq.Queue(self.repo).all(), [])

    def without_main(self):
        """Origin as a repository created empty is, with no main at all, and the clone's commit, which holds a
        queuekeeper.json, on a branch of its own: the landing that creates main (#10). Returns the commit."""
        shutil.rmtree(self.origin)
        run(self.base, "git", "init", "-q", "--bare", "-b", "main", str(self.origin))
        run(self.repo, "git", "update-ref", "-d", "refs/remotes/origin/main")
        run(self.repo, "git", "checkout", "-q", "-B", "first")
        return run(self.repo, "git", "rev-parse", "HEAD")

    def origin_main(self):
        """origin's main, or "" while it has none."""
        return run(self.origin, "git", "for-each-ref", "--format=%(objectname)", "refs/heads/main")

    def test_the_landing_into_a_repository_with_no_main_creates_main_at_its_tip(self):
        """#10: the queue fetched origin's main to land anything, so an empty repository's first commit went in by
        hand."""
        tip = self.without_main()
        said = io.StringIO()
        with patch.object(mq.Queue, "start"), contextlib.redirect_stderr(said), contextlib.redirect_stdout(io.StringIO()):
            first = mq.add(self.repo, "first", quiet_wait=True)
        self.assertIn("origin has no main yet, so this landing creates it", said.getvalue())
        self.assertIsNone(self.state(first)["base"])
        said = io.StringIO()
        with contextlib.redirect_stdout(said): self.work()
        self.assertEqual(self.state(first)["state"], "landed", said.getvalue())
        self.assertEqual(self.state(first)["landedAs"], tip)
        self.assertEqual(self.origin_main(), tip)
        self.assertEqual(self.gates(), 1)
        self.assertIsNone(mq.Queue(self.repo).held())
        self.assertIn("creating main", said.getvalue())
        # From then on main exists, and the next entry lands on it as any does.
        self.commit("a", "next")
        [after] = self.add("next")
        self.work()
        self.assertEqual(self.state(after)["state"], "landed")
        self.assertIn("a", self.main_files())

    def test_the_next_entry_can_create_main_after_a_definitive_rejection_of_the_first(self):
        base = self.without_main()
        (self.repo / "refused").write_text("x")
        run(self.repo, "git", "add", "refused")
        run(self.repo, "git", "commit", "-qm", "First candidate")
        run(self.repo, "git", "branch", "second", base)
        [first] = self.add("first")
        hook = self.origin / "hooks/pre-receive"
        hook.write_text('#!/bin/sh\nread old new ref\n'
                        'if git show "$new:refused" >/dev/null 2>&1; then exit 1; fi\n')
        host_isolation.executable(hook)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.work(), 0)
        self.assertEqual(self.state(first)["state"], "failed")
        self.assertEqual(self.origin_main(), "")
        self.assertIsNone(mq.Queue(self.repo).held())
        [second] = self.add("second")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.work(), 0)
        self.assertEqual(self.state(second)["state"], "landed")
        self.assertEqual(self.origin_main(), base)

    def test_a_rejected_first_main_retry_keeps_the_unanswered_push_for_reconciliation(self):
        tip = self.without_main()
        [identifier] = self.add("first")
        real = mq.git
        attempted = []

        def git(directory, *arguments, **options):
            if not arguments or arguments[0] != "push": return real(directory, *arguments, **options)
            attempted.append(arguments)
            said = UNREACHED["ssh dropped"] if len(attempted) == 1 else REFUSED["rejected"]
            return subprocess.CompletedProcess(arguments, 1, "", said)

        queue = mq.Queue(self.repo)
        with patch.object(mq, "git", git), patch.object(mq, "pause"), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.work(), 1)
        self.assertIn("pushedBefore", self.state(identifier))
        self.assertEqual(agent_results.lookup(queue, identifier)["publication"],
                         {"status": "unverified", "heads": [tip]})
        run(self.repo, "git", "push", "-q", "origin", f"{tip}:refs/heads/main")
        queue.hold_file.unlink()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.work(), 0)
        self.assertEqual(self.state(identifier)["landedAs"], tip)

    def test_a_landing_that_would_create_main_and_fails_its_gate_leaves_origin_without_main(self):
        self.without_main()
        (self.repo / "bad").write_text("x")
        run(self.repo, "git", "add", "bad")
        run(self.repo, "git", "commit", "-qm", "Fail the gate")
        [first] = self.add("first")
        self.work()
        self.assertEqual(self.state(first)["state"], "failed")
        self.assertIn("gated alone to create main", self.state(first)["why"])
        self.assertEqual(self.gates(), 1)
        self.assertEqual(self.origin_main(), "")
        self.assertIsNone(mq.Queue(self.repo).held())

    def test_a_commit_without_a_configuration_is_refused_when_origin_has_no_main(self):
        self.without_main()
        run(self.repo, "git", "rm", "-q", "queuekeeper.json")
        run(self.repo, "git", "commit", "-qm", "No Queuekeeper")
        with self.assertRaisesRegex(mq.Refused, "origin has no main yet, and .* has no queuekeeper.json"):
            self.add("first")
        with self.assertRaisesRegex(mq.Refused, "without --no-gate"), patch.object(mq.Queue, "start"), \
                patch("builtins.print"):
            mq.add(self.repo, "HEAD~1", no_gate=True, why="an empty repository")
        self.assertEqual(mq.Queue(self.repo).all(), [])

    def test_only_one_entry_waits_to_create_main(self):
        self.without_main()
        [first] = self.add("first")
        self.commit("a", "second", start="first")
        with self.assertRaises(mq.Refused) as refused, patch("builtins.print"): mq.add(self.repo, "second")
        self.assertIn("creates main, gated alone", str(refused.exception))
        self.assertEqual(refused.exception.waits_for, [first])
        self.assertEqual([entry["id"] for entry in mq.Queue(self.repo).all()], [first])
        # The same branch added again replaces its own entry rather than waiting for it.
        run(self.repo, "git", "checkout", "-q", "first")
        self.commit("b", "first", start="first")
        [again] = self.add("first")
        self.assertEqual(self.state(first)["state"], "removed")
        self.assertEqual(self.state(again)["state"], "queued")

    def test_main_created_during_the_gate_sends_the_landing_back_to_the_queue(self):
        """The push creates main only while there is none (--force-with-lease), so that nothing is rewritten."""
        start = self.without_main()
        self.commit("a", "first", start="first")
        real = mq.Worker.gate
        def gate(worker, lanes, round_name):
            outcome = real(worker, lanes, round_name)
            if not self.origin_main(): run(self.repo, "git", "push", "-q", "origin", f"{start}:refs/heads/main")
            return outcome
        [first] = self.add("first")
        said = io.StringIO()
        with patch.object(mq.Worker, "gate", gate), contextlib.redirect_stdout(said): self.work()
        self.assertIn("main was created while", said.getvalue())
        self.assertEqual(self.state(first)["state"], "landed", said.getvalue())
        self.assertEqual(self.gates(), 2)
        # Landed on top of the main made by hand, which it did not rewrite.
        run(self.repo, "git", "fetch", "-q", "origin")
        self.assertEqual(run(self.repo, "git", "rev-parse", "origin/main~1"), start)
        self.assertIn("a", self.main_files())

    def test_qk_land_wait_exits_by_whether_the_landing_that_creates_main_landed(self):
        """Through the real command line: git's "couldn't find remote ref" made qk land fail with exit 2, and with 0
        before that."""
        tip = self.without_main()
        # A commit without the configuration, which the working tree keeps, is refused, exit 1.
        run(self.repo, "git", "rm", "-q", "--cached", "queuekeeper.json")
        run(self.repo, "git", "commit", "-qm", "No Queuekeeper")
        land = ["python3", "scripts/qk", "land", "--wait", "--limit", str(stretched(120))]
        done = subprocess.run(land, cwd=self.repo, capture_output=True, text=True)
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("has no queuekeeper.json", done.stderr)
        self.assertNotIn("couldn't find remote ref", done.stderr)
        # A commit whose gate fails comes back, exit 1.
        run(self.repo, "git", "reset", "-q", "--hard", tip)
        (self.repo / "bad").write_text("x")
        run(self.repo, "git", "add", "bad")
        run(self.repo, "git", "commit", "-qm", "Fail the gate")
        done = subprocess.run(land, cwd=self.repo, capture_output=True, text=True)
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertEqual(self.origin_main(), "")
        # And one that passes creates main, exit 0.
        run(self.repo, "git", "reset", "-q", "--hard", tip)
        done = subprocess.run(land, cwd=self.repo, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.origin_main(), tip)

    def test_a_session_in_another_repository_lands_here(self):
        """Through the real command line, from another project's checkout, as a session fixing a
        dependency does: this repository's queue takes it, and its own session's entry waits beside it."""
        other = self.base / "dependent"
        other.mkdir()
        (other / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "dependent"}))
        run(other, "git", "init", "-q")
        (other / "scripts").mkdir()
        shutil.copy(ROOT / "scripts/qk", other / "scripts/qk")
        self.commit("fix", "fix")
        self.commit("own", "own")
        [own] = self.add("own")
        done = subprocess.run(["python3", "scripts/qk", "land", "--repo", "project", "fix", "--wait",
                               "--limit", str(stretched(120))], cwd=other, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertLessEqual({"fix", "own"}, self.main_files())
        self.assertEqual(self.state(own)["state"], "landed")
        self.assertEqual(self.gates(), 1)
        landed = done.stdout.rsplit("landed: ", 1)[1].strip()
        self.assertEqual(run(self.repo, "git", "rev-parse", "origin/main"), landed)
        self.assertEqual(mq.Queue(other).all(), [])

    def test_another_repository_needs_a_named_commit_and_a_real_project(self):
        other = self.base / "dependent"
        other.mkdir()
        (other / "scripts").mkdir()
        shutil.copy(ROOT / "scripts/qk", other / "scripts/qk")
        (other / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "dependent"}))
        run(other, "git", "init", "-q")
        # A checkout neither of whose working tree and origin/main holds a configuration is said to be one (#188).
        plain = self.base / "plain"
        plain.mkdir()
        run(plain, "git", "init", "-q")
        (plain / "inside").mkdir()
        for words, said in ((["land", "--repo", "project"], "you name"), (["land", "--repo", "absent", "x"], "names no project"),
                            (["land", "--repo", "plain", "x"], "plain is a Git checkout, but neither its working tree nor its origin/main"),
                            (["land", "--repo", str(plain / "inside"), "x"], "names no project")):
            done = subprocess.run(["python3", "scripts/qk", *words], cwd=other, capture_output=True, text=True)
            self.assertNotEqual(done.returncode, 0)
            self.assertIn(said, done.stderr)
        self.assertEqual(mq.Queue(self.repo).all(), [])

    def test_a_session_started_worker_lands_in_the_background(self):
        self.commit("a", "a")
        with patch("builtins.print"): mq.add(self.repo, "a")
        identifier = mq.Queue(self.repo).all()[0]["id"]
        with patch("builtins.print"): self.assertEqual(mq.wait(self.repo, identifier, stretched(120)), 0)
        self.assertIn("a", self.main_files())

    def test_a_worker_that_ended_leaves_no_child_of_this_process(self):
        # Unreaped, it stayed a zombie, with its own process group and this process as its parent,
        # for test_core to take for the command it supervises when one process ran both (#99).
        def children():
            listing = subprocess.Popen(["ps", "-axo", "pid=,ppid=,stat="], stdout=subprocess.PIPE, text=True)
            listed = listing.communicate()[0]
            # Without ps itself, a child of this process while it lists.
            return {pid: stat for pid, parent, stat in map(str.split, listed.splitlines())
                    if int(parent) == os.getpid() and int(pid) != listing.pid}
        before = set(children())
        self.commit("a", "a")
        with patch("builtins.print"): mq.add(self.repo, "a")
        identifier = mq.Queue(self.repo).all()[0]["id"]
        with patch("builtins.print"): self.assertEqual(mq.wait(self.repo, identifier, stretched(120)), 0)
        self.assertIn("a", self.main_files())
        deadline = time.monotonic() + stretched(30)
        while (left := {pid: stat for pid, stat in children().items() if pid not in before}) and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertEqual(left, {})


class MainConfigurationUnreadable(Fixture):
    """origin/main's queuekeeper.json, which the queue judges every entry by, holds what this Queuekeeper refuses: a
    setting a release turned from warned into refused (vocaro/queuekeeper#295), or a key added after a branch left main,
    which the branch's Queuekeeper predates (vocaro/queuekeeper#279)."""

    def push_main(self, config, message):
        run(self.repo, "git", "fetch", "-q", "origin")
        run(self.repo, "git", "checkout", "-q", "-B", "main-change", "origin/main")
        (self.repo / "queuekeeper.json").write_text(json.dumps(config))
        run(self.repo, "git", "commit", "-qam", message)
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")

    def good(self):
        return json.loads(run(self.repo, "git", "show", "origin/main:queuekeeper.json"))

    def test_an_entry_that_fixes_mains_configuration_lands_gated_by_its_own(self):
        """#295: every entry was refused, the fix included, with a message that read as if about the entry's file."""
        good = self.good()
        self.push_main({**good, "spelling": {"words": []}}, "A setting since refused")
        run(self.repo, "git", "checkout", "-q", "-B", "fix", "origin/main")
        (self.repo / "queuekeeper.json").write_text(json.dumps(good))
        run(self.repo, "git", "commit", "-qam", "Drop the refused setting")
        said = io.StringIO()
        with patch.object(mq.Queue, "start"), contextlib.redirect_stderr(said), contextlib.redirect_stdout(io.StringIO()):
            fixed = mq.add(self.repo, "fix", quiet_wait=True)
        self.assertIn("origin/main's queuekeeper.json at", said.getvalue())
        self.assertIn("spelling is no longer a Queuekeeper setting", said.getvalue())
        self.assertIn("That is origin/main's file, not", said.getvalue())
        self.assertIn("'s own reads, so it gates this landing, all but its queue.hookCoveredBy", said.getvalue())
        said = io.StringIO()
        with contextlib.redirect_stdout(said): self.work()
        self.assertEqual(self.state(fixed)["state"], "landed")
        self.assertEqual(self.gates(), 1)
        self.assertIsNone(mq.Queue(self.repo).held())
        self.assertIn("cannot be read", said.getvalue())
        self.assertIn("so this round reads it, all but its queue.hookCoveredBy", said.getvalue())
        self.assertEqual(self.good(), good)

    def test_an_entry_that_leaves_mains_configuration_as_it_is_is_refused_naming_mains_file(self):
        """Negative control: an entry whose own file is main's, unchanged, cannot gate itself by it."""
        self.push_main({**self.good(), "spelling": {"words": []}}, "A setting since refused")
        self.commit("a", "a")
        with self.assertRaises(ValueError) as raised, patch.object(mq.Queue, "start"), patch("builtins.print"):
            mq.add(self.repo, "a")
        message = str(raised.exception)
        self.assertNotIsInstance(raised.exception, mq.Refused)
        self.assertIn("origin/main's queuekeeper.json at", message)
        self.assertIn("That is origin/main's file, not", message)
        self.assertIn("own cannot be read either", message)
        self.assertEqual(mq.Queue(self.repo).all(), [])

    def test_a_branch_behind_a_key_main_added_is_told_to_rebase(self):
        """#279: a branch cut before main's file gained a key was told only "Unknown configuration keys"."""
        self.commit("a", "behind")
        self.push_main({**self.good(), "novel": True}, "A key this Queuekeeper does not know")
        with self.assertRaises(mq.Refused) as raised, patch.object(mq.Queue, "start"), patch("builtins.print"):
            mq.add(self.repo, "behind")
        error = raised.exception
        self.assertIn("Unknown configuration keys: ['novel']", str(error))
        self.assertIn("the branch is behind origin/main", str(error))
        self.assertIn("git rebase origin/main", str(error))
        advice = error.recovery
        self.assertEqual((advice["reasonCode"], advice["responsibleParty"], advice["action"], advice["retryable"]),
                         ("behind-main", "session", "rebase", True))
        self.assertEqual(advice["suggestedCommands"], [["git", "rebase", "origin/main"]])
        self.assertEqual(mq.Queue(self.repo).all(), [])
        # Negative control: rebased, it holds main's file, which a rebase does not make this code read.
        run(self.repo, "git", "checkout", "-q", "behind")
        run(self.repo, "git", "rebase", "-q", "origin/main")
        with self.assertRaises(ValueError) as raised, patch.object(mq.Queue, "start"), patch("builtins.print"):
            mq.add(self.repo, "behind")
        self.assertNotIsInstance(raised.exception, mq.Refused)
        self.assertIn("own cannot be read either", str(raised.exception))


# A fixture suite for an qk unittest gate: test_sometimes fails on each run whose letter in PLAN is F, counting its
# runs in RUNS, and imports fixturelib.helper, which imports fixturelib.deeper.
FLAKY_MODULE = """import pathlib, sys, unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src/python"))
from fixturelib import helper

RUNS, PLAN = pathlib.Path({runs!r}), pathlib.Path({plan!r})


class Flaky(unittest.TestCase):
    def test_sometimes(self):
        done = len(RUNS.read_text()) if RUNS.exists() else 0
        RUNS.write_text("x" * (done + 1))
        plan = PLAN.read_text() if PLAN.exists() else ""
        self.assertNotEqual(plan[done:done + 1], "F", f"run {{done + 1}} fails, as planned")

    def test_steady(self):
        self.assertEqual(helper.VALUE, 1)
"""

# A word check of docs/, whose failure names each PATH:LINE, as a project's own preflight check's does.
WORDS_MODULE = """import pathlib, unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


class Words(unittest.TestCase):
    def test_spelling(self):
        found = [f"{path.relative_to(ROOT)}:{number}: misspelling wrongword"
                 for path in sorted((ROOT / "docs").rglob("*.md"))
                 for number, line in enumerate(path.read_text().splitlines(), 1) if "wrongword" in line]
        self.assertEqual(found, [], "\\n".join(found))
"""


class RejectionFacts(Fixture):
    """What a rejection adds about the failing lane's load and tests, and what it leaves alone (#273)."""
    def test_a_rejection_of_a_lane_that_names_no_tests_says_it_cannot_tell(self):
        self.commit("a", "a")
        [identifier] = self.add("a")
        self.status.write_text("1")
        # The positive control of sampled_while_running: the rejected lane's end is read once, sampling or not.
        with self.sampled_while_running() as finish, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.work(), 0)
        finish.assert_called_once()
        entry = self.state(identifier)
        self.assertEqual(entry["state"], "failed")
        self.assertIn("the one-minute load was highest 3", entry["why"])
        # The gate's own summary is read when there is no qk unittest summary, and it names no test (#83).
        self.assertIn("Which tests failed could not be determined (gate's summary names no failed test; its tests: no "
                      "test counts in its log), so whether this entry changes their files is unknown", entry["why"])
        self.assertNotIn("changes no file", entry["why"])
        self.assertIn("could not be determined", mq.describe(entry))

    @contextlib.contextmanager
    def sampled_while_running(self):
        """The lane's load sampled while it runs, as a lane longer than BUSY_SAMPLE is (or one a sleep or a clock step
        stretches past it), and the end-of-lane reading a rejection adds watched apart from it: such a sampling is no
        reading of the lane's end (#358). Yields the watched LoadSampler.finish."""
        with patch.object(mq, "BUSY_SAMPLE", 0.01), \
                patch.object(mq.LoadSampler, "finish", autospec=True, side_effect=mq.LoadSampler.finish) as finish:
            yield finish

    def test_a_gate_that_passes_reads_no_end_of_lane_load_and_names_none(self):
        # The negative control: at a passing lane's end nothing is read, and the entry has no reason at all.
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.sampled_while_running() as finish, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.work(), 0)
        finish.assert_not_called()
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertNotIn("why", self.state(identifier))

    def test_a_lane_with_no_verdict_reads_no_load_and_blames_no_commit(self):
        # A run left unmeasured comes back at the limit without load sampled for attributing a test failure.
        self.commit("a", "a")
        [identifier] = self.add("a")
        self.status.write_text(str(mq.lane_results.UNMEASURED))
        with self.sampled_while_running() as finish, contextlib.redirect_stdout(io.StringIO()): self.work()
        finish.assert_not_called()
        entry = self.state(identifier)
        self.assertEqual(entry["state"], "failed")
        self.assertTrue(entry["noVerdict"])
        self.assertNotIn("While gate ran", entry.get("why", ""))


class MainCheckoutCopies(Fixture):
    """A rejection of an entry that changes a folder inside the project that its plugins load from says that the
    round's lanes loaded main's copy of it, not the entry's, and names the staged landing (vocaro/queuekeeper#186)."""

    def setUp(self):
        super().setUp()
        (self.repo / "kit").mkdir()
        (self.repo / "kit/fixture_inside.py").write_text("def register(): pass\n")
        run(self.repo, "git", "add", "kit")
        config = self.config()
        config["plugins"] = [{"module": "fixture_inside", "path": "kit"}]
        self.publish(config, "Load a plugin from a folder inside the project")

    def rejected(self, path, content):
        """The why of one entry that commits content to path, whose gate fails."""
        self.status.write_text("1")
        self.commit(path, "branch", content)
        [identifier] = self.add("branch")
        with contextlib.redirect_stdout(io.StringIO()): self.assertEqual(self.work(), 0)
        entry = self.state(identifier)
        self.assertEqual(entry["state"], "failed")
        return entry["why"]

    def test_an_entry_that_changes_the_folder_is_told_main_judged_it(self):
        why = self.rejected("kit/fixture_inside.py", "def register(): pass\n# changed\n")
        self.assertIn("This entry changes kit/fixture_inside.py in kit, which fixture_inside loads from: the round's "
                      "lanes load the project's plugins from its main checkout, not from the batch, so main's copy "
                      "judged this entry. If the failure comes from that copy, land the change to the folder on its "
                      "own first, and what relies on it once that is on main.", why)

    def test_an_entry_that_changes_no_file_there_is_not(self):
        why = self.rejected("elsewhere", "x\n")
        self.assertIn("fail on this commit", why)
        self.assertNotIn("main's copy", why)

    def test_a_folder_beside_the_project_or_a_module_with_no_folder_is_not_named(self):
        changed = {"kit/a.py", "../kit/a.py", "src/a.py"}
        self.assertEqual(mq.main_copies({"plugins": ["fixture_inside", {"module": "beside", "path": "../kit"},
                                                        {"module": "absolute", "path": "/kit"}]}, changed), "")
        self.assertIn("src/a.py in src, which inside loads from",
                      mq.main_copies({"plugins": [{"module": "inside", "path": "./src/"}]}, changed))


class SummaryFacts(Fixture):
    """A rejection of a lane whose log has no qk unittest summary names the failed tests from the summary qk run ends
    with, and compares the Swift or Python files they failed in with the entry's (vocaro/queuekeeper#83)."""
    CELLS = '"testCellsTheReaderGoesToHaveTheirHighlightAndSelection"'
    RANGES = '"testHighlightsOnCellRangesReachOnlyTheirCells"'
    # Swift Testing's lines for two failed tests, as EPUBLib's gate printed them on 2026-11-11.
    SWIFT = (f"✘ Test {CELLS} recorded an issue at TableScrolledCellTests.swift:298:9: Expectation failed: cells\n"
             f"✘ Test {CELLS} failed after 0.200 seconds with 1 issue.\n"
             f"✘ Test {RANGES} recorded an issue at TableCoverageInteractionTests.swift:749:9: Expectation failed\n"
             f"✘ Test {RANGES} failed after 0.100 seconds with 1 issue.\n"
             "✘ Test run with 30 tests in 4 suites failed after 1.000 seconds with 2 issues.\n")
    CELLS_FILE = "Tests/EPUBLibTests/TableScrolledCellTests.swift"
    NAMED = (f"{CELLS} at TableScrolledCellTests.swift:298, {RANGES} at TableCoverageInteractionTests.swift:749")
    PYTEST_FILE = "pipeline/tests/test_warm_push.py"
    PYTEST_NAME = "test_a_push_is_one_heavy_job_beside_other_admitted_work"
    PYTEST = ("  ✗ pytest                                         569s\n"
              f"      ____________ {PYTEST_NAME} ____________\n"
              "      E           AssertionError: Previous HEAD position was not preserved\n"
              "      tests/test_warm_push.py:606: AssertionError\n"
              "      ========= 1 failed, 24 passed in 569s =========\n")

    def setUp(self):
        super().setUp()
        for path in (self.CELLS_FILE, "Tests/EPUBLibTests/TableCoverageInteractionTests.swift", self.PYTEST_FILE):
            (self.repo / path).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / path).write_text("import Testing\n" if path.endswith(".swift") else "# pytest fixture\n")
        run(self.repo, "git", "add", ".")
        run(self.repo, "git", "commit", "-qm", "Add the tests")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")

    def rejected(self, printed, path, content="changed\n", status=1):
        """The why of one entry that commits content to path, whose gate prints printed and exits status."""
        Path(f"{self.status}.screen").write_text(printed)
        self.status.write_text(str(status))
        self.commit(path, "branch", content)
        [identifier] = self.add("branch")
        with contextlib.redirect_stdout(io.StringIO()): self.assertEqual(self.work(), 0)
        entry = self.state(identifier)
        self.assertEqual(entry["state"], "failed")
        return entry["why"]

    def test_an_entry_that_changes_the_file_a_swift_test_failed_in_is_told_so(self):
        why = self.rejected(self.SWIFT, self.CELLS_FILE, "import Testing\n// changed\n")
        self.assertIn(f"This entry changes {self.CELLS_FILE}, a file of the failed tests {self.NAMED}", why)
        self.assertNotIn("could not be determined", why)
        self.assertNotIn("cannot be placed", why)

    def test_an_entry_that_changes_no_such_file_is_told_that_and_what_was_compared(self):
        why = self.rejected(self.SWIFT, "Sources/Reader.swift", "let reader = 1\n")
        self.assertIn(f"This entry changes no file of the failed tests {self.NAMED}: only the files they failed in are "
                      "compared, not the code they test", why)

    def test_a_file_named_twice_in_the_repository_is_not_guessed(self):
        # A second file of the same name leaves the bare name the summary gives two places.
        why = self.rejected(self.SWIFT, "Tests/Other/TableScrolledCellTests.swift", "import Testing\n")
        self.assertIn(f"{self.NAMED} failed. The file of {self.CELLS} at TableScrolledCellTests.swift:298 cannot be "
                      "placed in the repository, so whether this entry changes their files is unknown", why)
        self.assertNotIn("changes no file", why)

    def test_a_test_named_with_no_file_is_said_to_have_none_to_compare(self):
        printed = self.SWIFT + '✘ Test "testAlone" failed after 0.100 seconds with 1 issue.\n'
        why = self.rejected(printed, self.CELLS_FILE, "import Testing\n// changed\n")
        self.assertIn(f"This entry changes {self.CELLS_FILE}, a file of the failed tests {self.NAMED}", why)
        self.assertIn('No Swift or Python file is named for "testAlone", so whether this entry changes '
                      "their files is unknown", why)

    def test_a_nested_pytest_report_without_a_short_summary_names_the_test_and_its_file(self):
        why = self.rejected(self.PYTEST, "elsewhere")
        self.assertIn(f"This entry changes no file of the failed tests {self.PYTEST_NAME} at "
                      "tests/test_warm_push.py:606", why)
        self.assertNotIn("could not be determined", why)

    def test_a_pytest_short_summary_can_name_the_changed_file_without_a_line_number(self):
        node = f"{self.PYTEST_FILE}::{self.PYTEST_NAME}"
        why = self.rejected(f"FAILED {node} - AssertionError: no\n========= 1 failed in 1s =========\n",
                            self.PYTEST_FILE)
        self.assertIn(f"This entry changes {self.PYTEST_FILE}, a file of the failed tests {node}", why)

    def test_a_pytest_node_id_names_the_test_file_even_if_the_assertion_is_in_a_helper(self):
        node = f"{self.PYTEST_FILE}::{self.PYTEST_NAME}"
        printed = (f"____________ {self.PYTEST_NAME} ____________\n"
                   "tests/helpers.py:90: AssertionError\n"
                   "=========== short test summary info ===========\n"
                   f"FAILED {node} - AssertionError: no\n"
                   "========= 1 failed in 1s =========\n")
        why = self.rejected(printed, self.PYTEST_FILE)
        self.assertIn(f"This entry changes {self.PYTEST_FILE}, a file of the failed tests {node} at "
                      "tests/helpers.py:90", why)

    def test_a_failed_lane_whose_summary_names_no_test_says_so(self):
        # As in vocaro/vocaro-engineering#406: devices failed, yet the counts the gate read say none did.
        printed = "Executed 9831 tests, with 0 failures (0 unexpected) in 100.0 (100.1) seconds\n"
        why = self.rejected(printed, "Sources/Reader.swift", "let reader = 1\n", status=65)
        self.assertIn("Which tests failed could not be determined (gate's summary names no failed test; its tests: "
                      "9831 passed, 0 failed", why)
        self.assertNotIn("qk unittest summary", why)

    def test_placed_file_takes_a_bare_name_a_path_or_an_absolute_path_and_never_guesses(self):
        files = {"Tests/A/Cells.swift", "Tests/B/Ranges.swift", "Tests/B/Twice.swift", "Tests/C/Twice.swift"}
        for path, expected in (("Cells.swift", "Tests/A/Cells.swift"), ("A/Cells.swift", "Tests/A/Cells.swift"),
                               ("./Tests/A/Cells.swift", "Tests/A/Cells.swift"),
                               ("/Users/someone/EPUBLib/Tests/B/Ranges.swift", "Tests/B/Ranges.swift"),
                               ("Twice.swift", None), ("Missing.swift", None), ("ells.swift", None)):
            with self.subTest(path=path):
                self.assertEqual(mq.placed_file(path, files), expected)


class LoadReadings(unittest.TestCase):
    """load_of, load_words and LoadSampler alone (#273)."""
    def test_load_of_reads_the_farm_mac_there_and_this_mac_here(self):
        there = {"load": 12.5, "busyLimit": 75.0}
        with patch.object(mq.remote, "reading_there", return_value=(there, 40.0)) as asked:
            self.assertEqual(mq.load_of("fake-mac"), (12.5, 40.0, 75.0))
        asked.assert_called_once_with("fake-mac")
        with patch.object(mq.remote, "reading_there", return_value=({"busyLimit": 75.0, "load": "x"}, None)):
            self.assertEqual(mq.load_of("fake-mac"), (None, None, 75.0))
        with patch.object(mq.remote, "reading_there", side_effect=mq.remote.Unavailable("gone")), \
                self.assertRaises(mq.remote.Unavailable):
            mq.load_of("fake-mac")
        with patch.object(mq, "busy_of", return_value=(50.0, 70.0, "this Mac")), \
                patch.object(mq.resources, "one_minute_load", return_value=9.5):
            self.assertEqual(mq.load_of(None), (9.5, 50.0, 70.0))
        with patch.object(mq, "busy_of", return_value=None), patch.object(mq.resources, "one_minute_load", return_value=None):
            self.assertEqual(mq.load_of(None), (None, None, None))

    def test_load_words_keep_a_failed_reading_apart_from_a_low_one(self):
        own = ". Both count the lane's own work, so neither says what else that Mac was doing"
        cases = [([(70.0, 90.0, 75.0), (40.0, 99.0, 75.0)], "the one-minute load was highest 70, last 40 over 2 readings, "
                  "and the CPU was at most 99% busy" + own),
                 ([(5.0, 10.0, 75.0), None, (None, None, 75.0)], "the one-minute load was highest 5, last 5 over 1 of 3 "
                  "readings, and the CPU was at most 10% busy" + own),
                 ([], "the one-minute load could not be read, and the CPU busy share could not be read"),
                 ([None], "the one-minute load could not be read, and the CPU busy share could not be read")]
        for readings, expected in cases:
            with self.subTest(readings=readings):
                self.assertEqual(mq.load_words("gate", None, readings), f"While gate ran on this Mac, {expected}")
        self.assertIn("on fake-mac", mq.load_words("gate", "fake-mac", []))

    def test_load_words_say_the_wait_to_start_apart_from_the_run(self):
        """A gate's own work is most of the load while it runs: only its wait's readings are of other work alone
        (vocaro/queuekeeper#12)."""
        ran, waited = [(782.0, 99.0, 70.0)], [(178.0, 85.0, 70.0), None, (86.0, 60.0, 70.0)]
        self.assertEqual(mq.load_words("gate", None, ran, waited),
                         "While gate ran on this Mac, the one-minute load was highest 782, last 782 over 1 reading, and "
                         "the CPU was at most 99% busy. Both count the lane's own work, so neither says what else that "
                         "Mac was doing. While it waited to start, before any of its own work, the load was highest 178 "
                         "over 2 of 3 readings, and the CPU was at most 85% busy, against that Mac's busy limit of 70%")
        # The negative control: a wait with nothing read adds no sentence, and no limit is set against the run.
        for nothing in ([], [None]):
            self.assertNotIn("waited", mq.load_words("gate", None, ran, nothing))
        self.assertNotIn("busy limit", mq.load_words("gate", None, ran))

    def test_the_sampler_keeps_a_reading_of_the_lanes_wait_apart_from_its_run(self):
        waits = iter([{"kind": "admission"}, {"kind": None}, None])
        loads = iter([(178.0, 85.0, 70.0), (782.0, 99.0, 70.0), (300.0, 95.0, 70.0), (20.0, 30.0, 70.0)])
        with patch.object(mq, "load_of", side_effect=lambda host: next(loads)), \
                patch.object(mq.remote, "dispatched", return_value=None), \
                patch.object(mq.lane_wait, "current", side_effect=lambda path, since: next(waits)):
            sampler = mq.LoadSampler("/nowhere", "gate", 0).start()
            for _ in range(3): sampler.read()
            said = sampler.finish(None)
        self.assertIn("the one-minute load was highest 782, last 20 over 3 readings", said)
        self.assertIn("While it waited to start, before any of its own work, the load was highest 178 over 1 reading", said)

    def test_the_sampler_keeps_only_the_readings_of_the_mac_the_lane_ran_on(self):
        # Before the dispatch names another Mac, a reading is this Mac's: not the lane's.
        readings = {None: (70.0, 99.0, 75.0), "fake-mac": (12.0, 40.0, 75.0)}
        with patch.object(mq, "load_of", side_effect=readings.get), \
                patch.object(mq.remote, "dispatched", side_effect=[None, "fake-mac"]):
            sampler = mq.LoadSampler("/nowhere", "gate", 0).start()
            sampler.read()
            sampler.read()
            said = sampler.finish("fake-mac")
        self.assertIn("on fake-mac, the one-minute load was highest 12, last 12 over 2 readings", said)
        self.assertNotIn("70", said)

    def test_the_sampler_reads_the_farm_mac_its_wait_record_names_before_the_dispatch_is_filed(self):
        """vocaro/queuekeeper#132: a dispatch's record is filed only when it ends, so the lane's wait record names the
        Mac while the lane runs there, and a failed gate there keeps more than its one reading at the end."""
        readings = {None: (70.0, 99.0, 70.0), "build-host": (12.0, 40.0, 90.0)}
        records = iter([{"kind": "admission", "mac": "build-host"},
                        {"kind": None, "mac": "build-host"},
                        {"kind": None, "mac": "build-host"}])
        with patch.object(mq, "load_of", side_effect=readings.get), \
                patch.object(mq.remote, "dispatched", return_value=None), \
                patch.object(mq.lane_wait, "current", side_effect=lambda path, since: next(records)):
            sampler = mq.LoadSampler("/nowhere", "gate", 0).start()
            for _ in range(3): sampler.read()
            said = sampler.finish("build-host")
        self.assertIn("on build-host, the one-minute load was highest 12, last 12 over 3 readings", said)
        self.assertIn("While it waited to start, before any of its own work, the load was highest 12 over 1 reading", said)
        self.assertNotIn("70", said)
        # The negative control: a record that names this Mac is read here.
        with patch.object(mq, "load_of", side_effect=readings.get) as load_of, \
                patch.object(mq.remote, "dispatched", return_value="build-host"), \
                patch.object(mq.lane_wait, "current", return_value={"kind": None, "mac": mq.lane_wait.HERE}):
            mq.LoadSampler("/nowhere", "gate", 0).read()
        load_of.assert_called_once_with(None)

    def test_a_reading_that_raises_reaches_no_gate(self):
        with patch.object(mq, "load_of", side_effect=ValueError("no")), \
                patch.object(mq.remote, "dispatched", side_effect=OSError("no")):
            sampler = mq.LoadSampler("/nowhere", "gate", 0).start()
            sampler.read()
            self.assertEqual(sampler.finish(None), "While gate ran on this Mac, the one-minute load could not be read, and "
                                                   "the CPU busy share could not be read")


class FlakyReruns(Fixture):
    """A failed qk unittest gate's failed tests rerun alone before a bisect, and a batch whose failed tests all pass
    alone and that reaches none of their files lands, with the flake recorded (owner's decision of 2026-11-07, #231)."""
    BUSY = ("cpu", "the CPU is 95% busy, at or above HEAVY_BUSY_LIMIT=75%")
    TEST = "test_flaky.Flaky.test_sometimes"
    PRODUCTION_RERUNS = mq.RERUNS

    def setUp(self):
        super().setUp()
        # Most cases cover what the worker does with the rerun's verdict. Tests that exercise
        # successive attempts set their own limit.
        self.enterContext(patch.object(mq, "RERUNS", 1))
        self.runs, self.plan = self.base / "runs", self.base / "plan"
        (self.repo / "tests").mkdir()
        (self.repo / "tests/test_flaky.py").write_text(FLAKY_MODULE.format(runs=str(self.runs), plan=str(self.plan)))
        (self.repo / "tests/test_words.py").write_text(WORDS_MODULE)
        library = self.repo / "src/python/fixturelib"
        library.mkdir(parents=True)
        (library / "__init__.py").write_text("")
        (library / "helper.py").write_text("from . import deeper\nVALUE = 1\n")
        (library / "deeper.py").write_text("DEPTH = 1\n")
        config = self.config()
        # This gate intentionally consumes docs/*.md as test inputs, so its documents keep the code gate.
        config["queue"]["unread"] = False
        config["lanes"]["gate"] = {"command": ["python3", "scripts/qk", "unittest", "-s", "tests", "-v"], "timeout": 600}
        (self.repo / "queuekeeper.json").write_text(json.dumps(config))
        run(self.repo, "git", "add", ".")
        run(self.repo, "git", "commit", "-qm", "Gate on an qk unittest suite")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")

    def assert_gate_failure(self, path):
        """The original failure and assertion survive collection on both supported unittest formats."""
        log = Path(path).read_text()
        # Python 3.9 names the class in parentheses; later versions include the method there too.
        self.assertRegex(log, r"(?m)^FAIL: test_sometimes \(test_flaky\.Flaky(?:\.test_sometimes)?\)$")
        self.assertIn("AssertionError: 'F' == 'F' : run 1 fails, as planned", log)

    def runs_of_the_test(self):
        return len(self.runs.read_text()) if self.runs.exists() else 0

    def tip_message(self):
        run(self.repo, "git", "fetch", "-q", "origin")
        return run(self.repo, "git", "log", "-1", "--format=%B", "origin/main")

    def main_messages(self):
        return run(self.repo, "git", "log", "--format=%B", "origin/main")

    def test_a_test_that_fails_once_and_passes_alone_lands_the_batch_after_a_settle_and_is_recorded(self):
        self.plan.write_text("F")
        self.commit("a", "a"); self.commit("b", "b")
        ids = self.add("a", "b")
        self.enterContext(patch.object(mq, "RERUNS", self.PRODUCTION_RERUNS))
        with self.host([self.BUSY, None]) as (slept, said):
            self.assertEqual(self.work(), 0)
        log = said.getvalue()
        entries = [self.state(identifier) for identifier in ids]
        self.assertEqual([entry["state"] for entry in entries], ["landed", "landed"])
        # One gate and its reruns, each alone, after the host settled, and no bisect.
        self.assertEqual(self.runs_of_the_test(), 1 + mq.RERUNS)
        self.assertEqual(slept, [mq.SETTLE_POLL])
        self.assertNotIn("fail together", log)
        self.assertIn(f"gate failed 1 test at ", log)
        self.assertIn(f": {self.TEST}; rerunning each alone up to {mq.RERUNS} times on that commit before a bisect", log)
        self.assertIn("the failed tests run alone again once the host settles", log)
        self.assertIn("no commit of the batch changes its module or a file it imports: landing the batch", log)
        # Recorded in each entry, with both logs, on the tracker and in the landed tip's trailer.
        for entry in entries:
            [flake] = entry["flakes"]
            self.assertEqual(flake["test"], self.TEST)
            self.assertTrue(Path(flake["gateLog"]).is_file())
            self.assert_gate_failure(flake["gateLog"])
            self.assertEqual(len(flake["rerunLogs"]), mq.RERUNS)
            self.assertTrue(all("OK" in Path(path).read_text() for path in flake["rerunLogs"]))
            self.assertEqual(flake["issue"], "https://example.invalid/fixture/issues/1")
        self.assertEqual(entries[0]["landedTip"], entries[1]["landedTip"])
        [tracked] = self.tracked
        self.assertEqual(tracked["test"], self.TEST)
        self.assertIn("the batch landed", tracked["outcome"])
        self.assertIn(f"flaky: {self.TEST} failed the gate at", log)
        tip = self.tip_message()
        self.assertIn(f"{mq.FLAKY}: {self.TEST} failed the gate at ", tip)
        self.assertIn(f"passed alone {mq.RERUNS} times there; gate log {entries[0]['flakes'][0]['gateLog']}", tip)
        self.assertIn("https://example.invalid/fixture/issues/1", tip)
        self.assertEqual(self.main_messages().count(f"{mq.FLAKY}:"), 1)
        self.assertIn("flaky: test_flaky.Flaky.test_sometimes failed the gate at", mq.describe(entries[0]))

    def test_a_gate_fail_fast_stopped_at_a_failure_that_then_passes_alone_gates_again_rather_than_land(self):
        """--fail-fast stops the gate once a failed test fails again alone there, before the rest of the suite runs.
        When the worker's reruns then pass, nothing ran the rest, so the batch gates again rather than land as flaky."""
        config = json.loads((self.repo / "queuekeeper.json").read_text())
        config["lanes"]["gate"]["command"] += ["-j", "1", "--fail-fast"]
        (self.repo / "queuekeeper.json").write_text(json.dumps(config))
        run(self.repo, "git", "commit", "-qam", "Stop the gate at a failure that fails again alone")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        # The gate's run and the runner's own run alone fail; every run after them passes.
        self.plan.write_text("FF")
        self.commit("a", "a")
        [identifier] = self.add("a")
        with contextlib.redirect_stdout(io.StringIO()) as said:
            self.assertEqual(self.work(), 0)
        log = said.getvalue()
        self.assertEqual(self.state(identifier)["state"], "landed", log)
        # The gate and its run alone, the worker's reruns, and the second gate, which passes whole.
        self.assertEqual(self.runs_of_the_test(), 2 + mq.RERUNS + 1, log)
        self.assertIn(f"rerunning each alone up to {mq.RERUNS} times", log)
        self.assertIn("before the rest of the suite ran: gating the batch again", log)
        self.assertNotIn("it imports: landing the batch", log)
        [tracked] = self.tracked
        self.assertIn("so the batch gated again", tracked["outcome"])
        # Landed by a gate that ran whole, so with no flaky trailer.
        self.assertNotIn(f"{mq.FLAKY}:", self.tip_message())

    def test_the_rounds_account_names_the_failure_and_each_rerun_until_the_verdict(self):
        # A wait read the round's empty gates during reruns as a gate that had not started (vocaro/queuekeeper#41).
        self.enterContext(patch.object(mq, "RERUNS", 2))
        self.plan.write_text("F")
        self.commit("a", "a"); self.commit("b", "b")
        self.add("a", "b")
        queue, seen, settled = mq.Queue(self.repo), [], []
        real = mq.processes.run
        def recorded(command, **options):
            failure = (queue.round_state() or {}).get("failure")
            if failure: seen.append((dict(failure), (options.get("env") or {}).get(mq.lane_wait.VARIABLE)))
            return real(command, **options)
        with patch.object(mq.processes, "run", recorded), \
                self.host([self.BUSY, None], then=lambda: settled.append(queue.round_state())):
            self.assertEqual(self.work(), 0)
        # While the host settles: the failed gate, its log and its failed test, and what the round waits for.
        failure = settled[0]["failure"]
        self.assertEqual((failure["lane"], len(failure["entries"]), failure["tests"], failure["reruns"]),
                         ("gate", 2, [self.TEST], mq.RERUNS))
        self.assertTrue(failure["log"].endswith("-gate.log"), failure)
        self.assertNotIn("test", failure)
        self.assertIn("the failed tests run alone", settled[0]["waiting"])
        # Each rerun, with its log and the wait record its admission writes to.
        self.assertEqual([(each["test"], each["attempt"]) for each, _ in seen],
                         [(self.TEST, attempt) for attempt in range(1, mq.RERUNS + 1)])
        for each, variable in seen:
            self.assertEqual(variable, each["waitRecord"])
            self.assertTrue(each["rerunLog"].endswith(f"-rerun-1-{each['attempt']}.log"), each)
        self.assertFalse(queue.round_file.exists())

    @contextlib.contextmanager
    def farm_mac(self):
        """On collection, move the lane's unittest log into a fake second-Mac dispatch. The live observers
        see no dispatch, so they cannot move the log before the worker collects it (#331)."""
        dispatches = self.base / "dispatches"
        def dispatch(root, name, since):
            own = Path(root) / ".build/queuekeeper" / name / "run.log"
            if not own.exists() or "queuekeeper unittest: " not in own.read_text(errors="replace"): return None, None
            ran = dispatches / f"{len(list(dispatches.glob('*'))) if dispatches.exists() else 0}"
            ran.mkdir(parents=True)
            own.rename(ran / "run.log")
            return ran, "fake-mac"
        with patch.object(mq.remote, "dispatch", side_effect=dispatch), \
                patch.object(mq.remote, "dispatched", return_value=None):
            yield

    def test_a_failed_test_of_a_gate_the_farm_mac_ran_is_rerun_alone_from_its_dispatchs_log(self):
        # The queue's log held only what qk run printed here, with no qk unittest summary, so another Mac's
        # failed test was never rerun and every batch bisected.
        self.plan.write_text("F")
        self.commit("a", "a"); self.commit("b", "b")
        ids = self.add("a", "b")
        finish = mq.GateWatch.finish

        def final_poll(watch):
            # Exercise an observation after the lane wrote its log and before the worker collects the dispatch.
            watch.look()
            finish(watch)

        with self.unittest_on_the_farm_mac(), patch.object(mq.GateWatch, "finish", final_poll), \
                contextlib.redirect_stdout(io.StringIO()) as said:
            self.assertEqual(self.work(), 0)
        log = said.getvalue()
        self.assertIn("gate exited 1 on fake-mac", log)
        self.assertIn(f": {self.TEST}; rerunning each alone up to {mq.RERUNS} times on that commit before a bisect", log)
        self.assertNotIn("no qk unittest summary", log)
        self.assertNotIn("fail together", log)
        entries = [self.state(identifier) for identifier in ids]
        self.assertEqual([entry["state"] for entry in entries], ["landed", "landed"])
        self.assert_gate_failure(entries[0]["flakes"][0]["gateLog"])

    def make_gate_a_group(self, other_status):
        """Make the gate a group of the qk unittest suite and a lane that exits other_status, as the toolkit's is (#289)."""
        declared = json.loads((self.repo / "queuekeeper.json").read_text())
        declared["lanes"]["suite"] = declared["lanes"]["gate"]
        declared["lanes"]["other"] = {"command": ["python3", "-c", f"raise SystemExit({other_status})"], "timeout": 600}
        declared["lanes"]["gate"] = {"parallel": ["suite", "other"], "resource": "heavy"}
        (self.repo / "queuekeeper.json").write_text(json.dumps(declared))
        run(self.repo, "git", "add", "queuekeeper.json")
        run(self.repo, "git", "commit", "-qm", "Gate on a group of lanes")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")

    def test_a_flake_in_a_groups_eng_unittest_lane_reruns_alone_and_the_batch_lands(self):
        self.make_gate_a_group(0)
        self.plan.write_text("F")
        self.commit("a", "a"); self.commit("b", "b")
        ids = self.add("a", "b")
        with contextlib.redirect_stdout(io.StringIO()) as said: self.assertEqual(self.work(), 0)
        log = said.getvalue()
        self.assertIn(f": {self.TEST}; rerunning each alone up to {mq.RERUNS} times on that commit before a bisect", log)
        self.assertNotIn("no qk unittest summary", log)
        self.assertEqual(self.runs_of_the_test(), 1 + mq.RERUNS)
        entries = [self.state(identifier) for identifier in ids]
        self.assertEqual([entry["state"] for entry in entries], ["landed", "landed"])
        self.assert_gate_failure(entries[0]["flakes"][0]["gateLog"])

    def test_a_fail_fast_group_cut_short_by_a_failure_that_then_passes_alone_gates_again_rather_than_land(self):
        """A group with failFast ends at its first failed lane, so its other lanes never finish. When the worker's
        reruns of that lane's failed test then pass, nothing ran the rest, so the batch gates again rather than land as
        flaky (vocaro/queuekeeper#256)."""
        self.make_gate_a_group(0)
        declared = json.loads((self.repo / "queuekeeper.json").read_text())
        # The other lane hangs in the first gate, until the group stops it, and passes at once in any later one.
        seen = self.base / "other-ran"
        declared["lanes"]["other"]["command"] = [
            "python3", "-c", f"import pathlib, time\nseen = pathlib.Path({str(seen)!r})\nfirst = not seen.exists()\n"
            "seen.touch()\nif first: time.sleep(600)"]
        declared["lanes"]["gate"]["failFast"] = True
        (self.repo / "queuekeeper.json").write_text(json.dumps(declared))
        run(self.repo, "git", "commit", "-qam", "End the gate at its first failed lane")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        self.plan.write_text("F")
        self.commit("a", "a")
        [identifier] = self.add("a")
        started = time.monotonic()
        with contextlib.redirect_stdout(io.StringIO()) as said:
            self.assertEqual(self.work(), 0)
        log = said.getvalue()
        self.assertLess(time.monotonic() - started, 300, log)
        self.assertEqual(self.state(identifier)["state"], "landed", log)
        # The gate, the worker's reruns, and the second gate, which runs whole and passes.
        self.assertEqual(self.runs_of_the_test(), 1 + mq.RERUNS + 1, log)
        self.assertIn(f"rerunning each alone up to {mq.RERUNS} times", log)
        self.assertIn("gate stopped at its first failed lane, suite, before other finished: gating the batch again", log)
        self.assertNotIn("it imports: landing the batch", log)
        [tracked] = self.tracked
        self.assertIn("before the rest of its group, other, ran, so the batch gated again", tracked["outcome"])
        self.assertNotIn(f"{mq.FLAKY}:", self.tip_message())

    @contextlib.contextmanager
    def dispatched_once_each_run_ends(self, file):
        """A gate another Mac ran: once each qk run has ended, as remote.execute returns it, file(own, dispatches) moves
        what the lane left in its directory own of the worker's checkout into a new fake dispatch it makes under
        dispatches and returns, or returns None for a run that sent none. remote.dispatch only looks dispatches up, as
        the real one does. The gate's watch and samplers look them up while the lane runs, which a busy host makes them
        do once a lane outlasts WATCH_POLL: here they look all through every lane, so the verdict cannot depend on when
        they do. A lookup that moved a lane's output mid-run made qk run fail on this Mac (vocaro/queuekeeper#17,
        vocaro/queuekeeper#110, vocaro/queuekeeper#111, vocaro/queuekeeper#144)."""
        dispatches, made = self.base / "dispatches", {}
        real = mq.processes.run
        def returned(command, *arguments, cwd=None, **options):
            try: return real(command, *arguments, cwd=cwd, **options)
            finally:
                if list(command[:3]) == ["python3", "scripts/qk", "run"] and cwd is not None:
                    ran = file(Path(cwd) / ".build/queuekeeper" / str(command[-1]), dispatches)
                    if ran is not None: made.setdefault((Path(cwd).resolve(), command[-1]), []).append((time.time(), ran))
        def dispatch(root, name, since):
            found = [ran for started, ran in made.get((Path(root).resolve(), name), []) if started >= since]
            return (found[-1], "fake-mac") if found else (None, None)
        with patch.object(mq.processes, "run", returned), patch.object(mq.remote, "dispatch", side_effect=dispatch), \
                patch.object(mq, "WATCH_POLL", 0.01), patch.object(mq, "BUSY_SAMPLE", 0.01):
            yield

    @staticmethod
    def new_dispatch(dispatches):
        ran = dispatches / f"{len(list(dispatches.glob('*'))) if dispatches.exists() else 0}"
        ran.mkdir(parents=True)
        return ran

    def group_on_the_farm_mac(self, keep_lanes=True):
        """A group gate another Mac ran: its lanes' logs and receipts are only in a dispatch's lanes/, as queuekeeper
        files them, and none is left in the worker's checkout; its run.log is empty. Without keep_lanes the dispatch
        brought none back."""
        def file(own, dispatches):
            if not (own / "lanes").is_dir(): return None
            ran = self.new_dispatch(dispatches)
            (ran / "run.log").write_text("")
            if keep_lanes: (own / "lanes").rename(ran / "lanes")
            else: shutil.rmtree(own / "lanes")
            return ran
        return self.dispatched_once_each_run_ends(file)

    def unittest_on_the_farm_mac(self, keep_log=True):
        """A qk unittest gate another Mac ran: what it printed, with qk unittest's summary, is only in its dispatch's
        run.log, and none is left in the worker's checkout. Without keep_log the dispatch kept no run.log."""
        def file(own, dispatches):
            log = own / "run.log"
            try:
                if "queuekeeper unittest: " not in log.read_text(errors="replace"): return None
            except OSError: return None
            ran = self.new_dispatch(dispatches)
            if keep_log: log.rename(ran / "run.log")
            else: log.unlink()
            return ran
        return self.dispatched_once_each_run_ends(file)

    def test_a_flake_in_a_group_gate_the_farm_mac_ran_reruns_from_its_dispatchs_lane_log(self):
        # A dispatched group's run.log is empty and its output holds qk unittest's summary only indented, in a
        # failed lane's tail; the lane's own log came back to the dispatch, not to the worker's checkout.
        self.make_gate_a_group(0)
        self.plan.write_text("F")
        self.commit("a", "a"); self.commit("b", "b")
        ids = self.add("a", "b")
        with self.group_on_the_farm_mac(), contextlib.redirect_stdout(io.StringIO()) as said:
            self.assertEqual(self.work(), 0)
        log = said.getvalue()
        self.assertIn("gate exited 1 on fake-mac", log)
        self.assertIn(f": {self.TEST}; rerunning each alone up to {mq.RERUNS} times on that commit before a bisect", log)
        self.assertNotIn("no qk unittest summary", log)
        entries = [self.state(identifier) for identifier in ids]
        self.assertEqual([entry["state"] for entry in entries], ["landed", "landed"])
        self.assert_gate_failure(entries[0]["flakes"][0]["gateLog"])

    def test_a_group_gate_whose_dispatch_brought_no_lane_logs_bisects_and_says_why(self):
        # The negative control: with no receipt, no lane is known to have passed, so nothing is rerun.
        self.make_gate_a_group(0)
        self.plan.write_text("F")
        self.commit("a", "a"); self.commit("b", "b")
        self.add("a", "b")
        with self.group_on_the_farm_mac(keep_lanes=False), contextlib.redirect_stdout(io.StringIO()) as said:
            self.assertEqual(self.work(), 0)
        self.assertIn("gate's lane other did not pass, which no rerun of a Python test can clear: bisecting", said.getvalue())
        self.assertNotIn("rerunning each alone", said.getvalue())

    def test_a_failed_remote_group_gate_reports_the_unittest_members_summary_and_assertion_in_the_wait(self):
        self.make_gate_a_group(1)
        self.plan.write_text("F")
        self.commit("a", "a")
        [identifier] = self.add("a")
        with self.group_on_the_farm_mac(), contextlib.redirect_stdout(io.StringIO()) as worker:
            self.assertEqual(self.work(), 0, worker.getvalue())
        entry = self.state(identifier)
        self.assertEqual(entry["state"], mq.FAILED)
        gate_log = Path(entry["log"]).read_text()
        [summary] = [line for line in gate_log.splitlines() if line.startswith("queuekeeper unittest: ") and
                     "FAILED: test_flaky" in line]
        with contextlib.redirect_stdout(io.StringIO()) as waited:
            self.assertEqual(mq.wait(self.repo, identifier, 1), 1)
        self.assertIn(summary, waited.getvalue())
        self.assertIn("AssertionError: 'F' == 'F' : run 1 fails, as planned", waited.getvalue())
        self.assertIn("remote dispatch run.log:", waited.getvalue())

    def test_a_group_whose_other_lane_failed_too_bisects_without_a_rerun(self):
        # The negative control: passing reruns of the Python tests must not land a batch another lane failed.
        self.make_gate_a_group(1)
        self.plan.write_text("F")
        self.commit("a", "a")
        self.add("a")
        with contextlib.redirect_stdout(io.StringIO()) as said: self.assertEqual(self.work(), 0)
        log = said.getvalue()
        self.assertIn("gate's lane other did not pass, which no rerun of a Python test can clear: bisecting", log)
        self.assertNotIn("rerunning each alone", log)
        self.assertEqual(self.runs_of_the_test(), 1)

    def test_a_farm_mac_gate_whose_dispatch_kept_no_log_bisects_and_says_why(self):
        # The negative control: a dispatch with no run.log gives the rerun nothing to read.
        self.plan.write_text("F")
        self.commit("a", "a"); self.commit("b", "b")
        ids = self.add("a", "b")
        # Its stand-in once removed the lane's run.log whenever it was looked up, and the gate's watch looks while the
        # lane runs: a passing gate of the bisect that outlasted WATCH_POLL lost its log and failed (#144).
        with self.unittest_on_the_farm_mac(keep_log=False), contextlib.redirect_stdout(io.StringIO()) as said:
            self.assertEqual(self.work(), 0)
        log = said.getvalue()
        self.assertIn("gate exited 1 on fake-mac", log)
        self.assertIn("gate failed with no failed test to rerun alone (its log has no qk unittest summary): bisecting",
                      log)
        self.assertNotIn("rerunning each alone", log)
        self.assertEqual([self.state(identifier)["state"] for identifier in ids], ["landed", "landed"], log)

    def test_a_test_that_fails_again_alone_is_real_and_the_batch_bisects_with_nothing_recorded(self):
        # The gate fails, the first rerun passes and the second fails: real. Every run after passes.
        self.enterContext(patch.object(mq, "RERUNS", 2))
        self.plan.write_text("F.F")
        self.commit("a", "a"); self.commit("b", "b")
        ids = self.add("a", "b")
        with contextlib.redirect_stdout(io.StringIO()) as said: self.assertEqual(self.work(), 0)
        log = said.getvalue()
        self.assertIn(f"{self.TEST} failed again alone, on rerun 2 (exit 1), so the failure is real", log)
        # Neither commit changes the test's module, a file it imports or a path its failure names: no blame.
        self.assertIn(f"{self.TEST} reaches no entry of the batch's 2, so no commit is blamed: bisecting", log)
        self.assertIn("2 commits fail together: next the oldest 1", log)
        self.assertEqual([self.state(identifier)["state"] for identifier in ids], ["landed", "landed"])
        self.assertEqual(self.runs_of_the_test(), 3 + 2)
        self.assertEqual(self.tracked, [])
        self.assertTrue(all("flakes" not in self.state(identifier) for identifier in ids))
        self.assertNotIn(f"{mq.FLAKY}:", self.main_messages())

    WORDS = "test_words.Words.test_spelling"

    def test_a_real_failure_whose_output_names_a_path_one_commit_changes_sends_that_commit_back_without_a_bisect(self):
        self.commit("c", "c")
        self.commit("docs/a.md", "a", "The wrongword\n")
        self.commit("docs/b.md", "b", "The word\n")
        ids = self.add("c", "a", "b")
        with contextlib.redirect_stdout(io.StringIO()) as said: self.assertEqual(self.work(), 0)
        log = said.getvalue()
        self.assertIn(f"{self.WORDS} failed again alone, on rerun 1 (exit 1), so the failure is real", log)
        self.assertIn(f"blaming {ids[1]} (Add docs/a.md): {self.WORDS}, which failed alone, as its failure names "
                      f"docs/a.md, which only {ids[1]} changes; it passed alone without this entry, on the batch's "
                      f"other entries, {ids[0]}, {ids[2]}, which gate again at once; sending it back and gating the "
                      "other 2 at once instead of bisecting", log)
        self.assertNotIn("fail together", log)
        entries = [self.state(identifier) for identifier in ids]
        self.assertEqual([entry["state"] for entry in entries], ["landed", "failed", "landed"])
        self.assertIn("its failure names docs/a.md", entries[1]["why"])
        self.assertIn(f"on the batch's other entries, {ids[0]}, {ids[2]}, which gate again at once", entries[1]["why"])
        # One gate for the batch and one for the other two, with no half gated.
        self.assertEqual(self.runs_of_the_test(), 2)
        self.assertEqual(self.tracked, [])

    def test_a_real_failure_of_a_test_whose_import_one_commit_changes_sends_that_commit_back(self):
        self.plan.write_text("FF")
        self.commit("b", "b")
        self.commit("src/python/fixturelib/helper.py", "a", "from . import deeper\nVALUE = 1  # changed\n")
        ids = self.add("b", "a")
        with contextlib.redirect_stdout(io.StringIO()) as said: self.assertEqual(self.work(), 0)
        log = said.getvalue()
        self.assertIn(f"blaming {ids[1]} (Add src/python/fixturelib/helper.py): {self.TEST}, which failed alone, as it "
                      f"imports src/python/fixturelib/helper.py, which only {ids[1]} changes; it passed alone without "
                      f"this entry, on the batch's other entry, {ids[0]}, which gate again at once", log)
        self.assertNotIn("fail together", log)
        self.assertEqual([self.state(identifier)["state"] for identifier in ids], ["landed", "failed"])
        # The gate, the rerun that confirmed the failure, the run without the blamed entry, and the other's gate.
        self.assertEqual(self.runs_of_the_test(), 4)

    def test_a_failure_another_entry_brings_through_a_file_the_test_reads_bisects_instead_of_blaming(self):
        # On 2026-11-16 a test read queuekeeper.json, which one entry changed, while the other entry changed a module
        # it imports; blame named the second alone and sent it back for the first's failure (vocaro/queuekeeper#183).
        module = (self.repo / "tests/test_flaky.py").read_text().replace(
            "    def test_steady(self):",
            "    def test_reads(self):\n"
            "        found = (pathlib.Path(__file__).resolve().parents[1] / 'data/setting.txt').exists()\n"
            "        self.assertFalse(found, 'a setting was found')\n\n"
            "    def test_steady(self):")
        self.commit("tests/test_flaky.py", "reads", module)
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        self.commit("data/setting.txt", "a", "on\n")
        self.commit("src/python/fixturelib/helper.py", "b", "from . import deeper\nVALUE = 1  # changed\n")
        ids = self.add("a", "b")
        with contextlib.redirect_stdout(io.StringIO()) as said: self.assertEqual(self.work(), 0)
        log = said.getvalue()
        reads = "test_flaky.Flaky.test_reads"
        self.assertIn(f"{reads} failed again alone, on rerun 1 (exit 1), so the failure is real", log)
        self.assertIn(f"{ids[1]} is blamed, but {reads} failed without it too, on the batch's other entries "
                      f"({ids[0]}), so the blame is not confirmed: bisecting", log)
        self.assertNotIn(f"blaming {ids[1]}", log)
        self.assertIn("2 commits fail together: next the oldest 1", log)
        # The bisect sends back the entry that brings the file, and the one blame named lands.
        self.assertEqual([self.state(identifier)["state"] for identifier in ids], ["failed", "landed"])

    def test_a_failure_that_names_paths_of_two_commits_bisects(self):
        self.commit("docs/a.md", "a", "The wrongword\n")
        self.commit("docs/b.md", "b", "The wrongword\n")
        ids = self.add("a", "b")
        with contextlib.redirect_stdout(io.StringIO()) as said: self.assertEqual(self.work(), 0)
        log = said.getvalue()
        self.assertIn(f"{self.WORDS} reaches 2 entries of the batch's 2 ({ids[0]}: its failure names docs/a.md; "
                      f"{ids[1]}: its failure names docs/b.md), so no commit is blamed: bisecting", log)
        self.assertIn("2 commits fail together: next the oldest 1", log)
        self.assertNotIn("blaming", log)
        self.assertEqual([self.state(identifier)["state"] for identifier in ids], ["failed", "failed"])

    def test_failed_tests_that_reach_different_commits_bisect(self):
        self.plan.write_text("FF")
        self.commit("docs/a.md", "a", "The wrongword\n")
        self.commit("src/python/fixturelib/helper.py", "b", "from . import deeper\nVALUE = 1  # changed\n")
        ids = self.add("a", "b")
        with contextlib.redirect_stdout(io.StringIO()) as said: self.assertEqual(self.work(), 0)
        log = said.getvalue()
        self.assertIn("the failed tests reach different entries (", log)
        self.assertIn("2 commits fail together: next the oldest 1", log)
        self.assertNotIn("blaming", log)
        self.assertEqual([self.state(identifier)["state"] for identifier in ids], ["failed", "landed"])

    def test_a_test_whose_module_cannot_be_found_bisects(self):
        self.commit("docs/a.md", "a", "The wrongword\n")
        self.commit("b", "b")
        ids = self.add("a", "b")
        with patch.object(mq, "imported_files", return_value=None), \
                contextlib.redirect_stdout(io.StringIO()) as said:
            self.assertEqual(self.work(), 0)
        log = said.getvalue()
        self.assertIn(f"the worker cannot find the module of {self.WORDS}, so no commit is blamed: bisecting", log)
        self.assertIn("2 commits fail together", log)
        self.assertEqual([self.state(identifier)["state"] for identifier in ids], ["failed", "landed"])

    @contextlib.contextmanager
    def unmeasured_rerun(self):
        run_test = mq.processes.run

        def rerun(command, **options):
            if command[-1] == self.TEST and mq.lane_wait.VARIABLE in (options.get("env") or {}):
                options["log_path"].write_text("queuekeeper unittest: UNMEASURED: test timed out\n")
                return mq.lane_results.UNMEASURED
            return run_test(command, **options)

        with patch.object(mq.processes, "run", rerun):
            yield

    def test_a_rerun_with_no_verdict_beside_one_confirmed_failure_sends_its_entry_back(self):
        self.plan.write_text("F")
        self.commit("docs/a.md", "a", "The wrongword\n")
        self.commit("b", "b")
        ids = self.add("a", "b")
        with self.unmeasured_rerun(), contextlib.redirect_stdout(io.StringIO()) as said:
            self.assertEqual(self.work(), 0)
        log = said.getvalue()
        self.assertIn(f"blaming {ids[0]} (Add docs/a.md): {self.WORDS}, which failed alone", log)
        self.assertIn(f"{self.TEST} gave no verdict alone, on rerun 1", log)
        self.assertNotIn("fail together", log)
        self.assertEqual([self.state(identifier)["state"] for identifier in ids], ["failed", "landed"])
        # The batch gate and b's gate both run the unresolved test. It is left out of the blame probe.
        self.assertEqual(self.runs_of_the_test(), 2)
        self.assertEqual(self.tracked, [])

    def test_a_rerun_with_no_verdict_and_no_confirmed_failure_still_bisects(self):
        self.plan.write_text("F")
        self.commit("src/python/fixturelib/helper.py", "a", "from . import deeper\nVALUE = 1  # changed\n")
        self.commit("b", "b")
        ids = self.add("a", "b")
        with self.unmeasured_rerun(), contextlib.redirect_stdout(io.StringIO()) as said:
            self.assertEqual(self.work(), 0)
        log = said.getvalue()
        self.assertIn(f"{self.TEST} gave no verdict alone, on rerun 1", log)
        self.assertIn("no rerun confirmed a failure, so no commit is blamed: bisecting", log)
        self.assertIn("2 commits fail together", log)
        self.assertNotIn("blaming", log)
        self.assertEqual([self.state(identifier)["state"] for identifier in ids], ["landed", "landed"])
        self.assertEqual(self.tracked, [])

    def test_a_flake_beside_a_real_failure_that_points_at_one_entry_sends_it_back_and_files_the_flake(self):
        # The flaky test fails in the gate only; the spelling failure is real and names a. Until 2026-11-19 the flake
        # stopped the blame and the batch bisected, which cost about an hour in vocaro/queuekeeper#88; now a is sent
        # back at once, the flake is filed, and b gates again.
        self.plan.write_text("F")
        self.commit("docs/a.md", "a", "The wrongword\n")
        self.commit("b", "b")
        ids = self.add("a", "b")
        with contextlib.redirect_stdout(io.StringIO()) as said: self.assertEqual(self.work(), 0)
        log = said.getvalue()
        self.assertIn(f"{self.TEST} passed alone beside the real failure, so blame weighs the confirmed failures "
                      "alone, and the flakes are filed only if one entry is blamed", log)
        self.assertIn(f"blaming {ids[0]} (Add docs/a.md): {self.WORDS}, which failed alone, as its failure names "
                      f"docs/a.md, which only {ids[0]} changes; it passed alone without this entry", log)
        self.assertNotIn("fail together", log)
        entries = [self.state(identifier) for identifier in ids]
        self.assertEqual([entry["state"] for entry in entries], ["failed", "landed"])
        self.assertIn(f"{self.TEST} failed the gate too but passed alone, a flake filed apart from this entry",
                      entries[0]["why"])
        # The flake is filed as any, once, and recorded in both entries of the batch it failed.
        self.assertIn(f"flaky: {self.TEST} failed the gate at", log)
        [tracked] = self.tracked
        self.assertEqual(tracked["test"], self.TEST)
        self.assertIn(f"blamed on {ids[0]}", tracked["outcome"])
        for entry in entries: self.assertEqual([flake["test"] for flake in entry["flakes"]], [self.TEST])
        # The gate, its reruns, and b's gate: no half was gated.
        self.assertEqual(self.runs_of_the_test(), 1 + mq.RERUNS + 1)

    def test_a_flake_beside_a_real_failure_that_points_at_no_entry_still_bisects_and_files_nothing(self):
        # The spelling failure is real, but it names docs/old.md, which origin/main already has and no entry changes.
        self.plan.write_text("F")
        self.commit("docs/old.md", "old", "The wrongword\n")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        self.commit("a", "a"); self.commit("b", "b")
        ids = self.add("a", "b")
        with contextlib.redirect_stdout(io.StringIO()) as said: self.assertEqual(self.work(), 0)
        log = said.getvalue()
        self.assertIn(f"{self.TEST} passed alone beside the real failure", log)
        self.assertIn(f"{self.WORDS} reaches no entry of the batch's 2, so no commit is blamed: bisecting", log)
        self.assertIn("2 commits fail together", log)
        self.assertNotIn("blaming", log)
        self.assertNotIn("flaky:", log)
        self.assertEqual(self.tracked, [])
        self.assertTrue(all("flakes" not in self.state(identifier) for identifier in ids))

    def test_a_flake_beside_real_failures_that_point_at_two_entries_still_bisects(self):
        self.plan.write_text("F")
        self.commit("docs/a.md", "a", "The wrongword\n")
        self.commit("docs/b.md", "b", "The wrongword\n")
        ids = self.add("a", "b")
        with contextlib.redirect_stdout(io.StringIO()) as said: self.assertEqual(self.work(), 0)
        log = said.getvalue()
        self.assertIn(f"{self.TEST} passed alone beside the real failure", log)
        self.assertIn(f"{self.WORDS} reaches 2 entries of the batch's 2", log)
        self.assertIn("2 commits fail together", log)
        self.assertNotIn("blaming", log)
        self.assertEqual(self.tracked, [])
        self.assertEqual([self.state(identifier)["state"] for identifier in ids], ["failed", "failed"])

    def changing(self, note, module=None):
        """tests/test_flaky.py, or module, with test_sometimes's own body given one more statement, naming note."""
        module = module if module is not None else self.main_module()
        line = "    def test_sometimes(self):\n"
        return module.replace(line, f"{line}        note = {note!r}\n", 1)

    def test_a_test_whose_body_one_entry_changes_that_fails_once_sends_that_entry_back_and_files_nothing(self):
        # Owner's decision of 2026-11-19 on vocaro/queuekeeper#129: a test origin/main has, whose method exactly one
        # entry changes, fails the gate and passes alone: that entry answers for it, as for a test it adds.
        self.plan.write_text("F")
        self.commit("b", "b")
        self.commit("tests/test_flaky.py", "a", self.changing("a"))
        ids = self.add("b", "a")
        with contextlib.redirect_stdout(io.StringIO()) as said: self.assertEqual(self.work(), 0)
        log = said.getvalue()
        self.assertIn(f"{self.TEST} passed alone, but of the batch's 2 only {ids[1]} changes its body, so it is no "
                      "flake and no issue is filed", log)
        self.assertIn(f"blaming {ids[1]} (Add tests/test_flaky.py), which changes {self.TEST}; sending it back and "
                      "gating the other 1 at once instead of bisecting", log)
        self.assertNotIn("fail together", log)
        self.assertNotIn("flaky:", log)
        entries = [self.state(identifier) for identifier in ids]
        self.assertEqual([entry["state"] for entry in entries], ["landed", "failed"])
        self.assertIn(f"{self.TEST}, a test whose body this entry alone of the batch changes, failed the gate and then "
                      f"passed alone {mq.RERUNS} times on that commit", entries[1]["why"])
        self.assertEqual(self.tracked, [])
        self.assertTrue(all("flakes" not in entry for entry in entries))
        # The gate, its reruns and b's gate alone.
        self.assertEqual(self.runs_of_the_test(), 1 + mq.RERUNS + 1)

    def test_a_test_whose_module_but_not_body_one_entry_changes_is_still_a_flake_and_bisects(self):
        # a changes test_steady beside it, not test_sometimes: changing only the module is not enough to be blamed.
        self.plan.write_text("F")
        module = self.main_module().replace("        self.assertEqual(helper.VALUE, 1)\n",
                                            "        self.assertEqual(helper.VALUE, 1)\n        self.assertTrue(True)\n")
        self.commit("b", "b")
        self.commit("tests/test_flaky.py", "a", module)
        ids = self.add("b", "a")
        with contextlib.redirect_stdout(io.StringIO()) as said: self.assertEqual(self.work(), 0)
        log = said.getvalue()
        self.assertNotIn("changes its body", log)
        self.assertNotIn("blaming", log)
        self.assertIn(f"flaky: {self.TEST} failed the gate at", log)
        self.assertIn("2 commits fail together", log)
        [tracked] = self.tracked
        self.assertEqual(tracked["test"], self.TEST)

    def test_a_test_whose_body_two_entries_change_is_still_a_flake_and_bisects(self):
        self.plan.write_text("F")
        self.commit("tests/test_flaky.py", "a", self.changing("a"))
        self.commit("tests/test_flaky.py", "b", self.changing("b", (self.repo / "tests/test_flaky.py").read_text()),
                    start="a")
        ids = self.add("a", "b")
        with contextlib.redirect_stdout(io.StringIO()) as said: self.assertEqual(self.work(), 0)
        log = said.getvalue()
        self.assertNotIn("changes its body", log)
        self.assertNotIn("blaming", log)
        self.assertIn(f"flaky: {self.TEST} failed the gate at", log)
        self.assertIn("2 commits fail together", log)
        self.assertEqual([self.state(identifier)["state"] for identifier in ids], ["landed", "landed"])

    def test_a_test_module_the_batch_adds_that_fails_once_and_passes_alone_sends_its_entry_back_and_files_nothing(self):
        # On 2026-11-13 a test a batch added failed its first gate and passed alone, and the queue filed it as a
        # flake to harden (vocaro/queuekeeper#129); a test origin/main lacks has no history to be a flake by.
        added_runs, added_plan = self.base / "added-runs", self.base / "added-plan"
        added_plan.write_text("F")
        self.commit("b", "b")
        self.commit("tests/test_added.py", "a", FLAKY_MODULE.format(runs=str(added_runs), plan=str(added_plan)))
        ids = self.add("b", "a")
        with contextlib.redirect_stdout(io.StringIO()) as said: self.assertEqual(self.work(), 0)
        log = said.getvalue()
        test = "test_added.Flaky.test_sometimes"
        self.assertIn(f"{test} passed alone, but origin/main has no such test, as the batch adds it, so it is no flake "
                      "and no issue is filed", log)
        self.assertIn(f"blaming {ids[1]} (Add tests/test_added.py), which adds {test}; sending it back and gating the "
                      "other 1 at once instead of bisecting", log)
        self.assertNotIn("fail together", log)
        self.assertNotIn("flaky:", log)
        entries = [self.state(identifier) for identifier in ids]
        self.assertEqual([entry["state"] for entry in entries], ["landed", "failed"])
        self.assertIn(f"{test}, a test this entry adds, failed the gate and then passed alone {mq.RERUNS} times on that "
                      "commit. origin/main has no such test, so it has no history to call it flaky by, and no "
                      "flaky-test issue is filed: fix it before this entry lands. Fix it and add it again.",
                      entries[1]["why"])
        self.assertEqual(len(added_runs.read_text()), 1 + mq.RERUNS)
        self.assertEqual(self.tracked, [])
        self.assertTrue(all("flakes" not in entry for entry in entries))
        self.assertNotIn(f"{mq.FLAKY}:", self.main_messages())

    def test_a_test_method_a_lone_entry_adds_to_an_existing_class_that_fails_once_sends_it_back(self):
        added_runs = self.base / "added-runs"
        method = (f"    def test_added(self):\n        runs = pathlib.Path({str(added_runs)!r})\n"
                  "        done = len(runs.read_text()) if runs.exists() else 0\n"
                  "        runs.write_text(\"x\" * (done + 1))\n"
                  "        self.assertTrue(done, \"the first run fails\")\n\n")
        module = (self.repo / "tests/test_flaky.py").read_text().replace("    def test_steady(self):",
                                                                         method + "    def test_steady(self):")
        self.commit("tests/test_flaky.py", "a", module)
        [identifier] = self.add("a")
        with contextlib.redirect_stdout(io.StringIO()) as said: self.assertEqual(self.work(), 0)
        log = said.getvalue()
        test = "test_flaky.Flaky.test_added"
        self.assertIn(f"{test} passed alone, but origin/main has no such test", log)
        self.assertIn(f"blaming {identifier} (Add tests/test_flaky.py), which adds {test}; sending it back\n", log)
        entry = self.state(identifier)
        self.assertEqual(entry["state"], "failed")
        self.assertIn(f"{test}, a test this entry adds, failed the gate", entry["why"])
        # The test the entry changes but origin/main already has passed the gate, and nothing was filed.
        self.assertEqual(self.runs_of_the_test(), 1)
        self.assertEqual(self.tracked, [])
        self.assertNotIn("flakes", entry)

    ADDED_METHOD = ("    def test_added(self):\n        runs = pathlib.Path({runs!r})\n"
                    "        done = len(runs.read_text()) if runs.exists() else 0\n"
                    "        runs.write_text(\"x\" * (done + 1))\n"
                    "        self.assertTrue(done, \"the first run fails\")\n\n")

    def main_module(self):
        """tests/test_flaky.py as origin/main has it, whatever branch the clone has checked out."""
        return run(self.repo, "git", "show", "origin/main:tests/test_flaky.py") + "\n"

    def adding(self, runs):
        """tests/test_flaky.py with a test_added method, which fails its first run only, before test_steady."""
        return self.main_module().replace(
            "    def test_steady(self):", self.ADDED_METHOD.format(runs=str(runs)) + "    def test_steady(self):")

    def test_an_added_test_is_blamed_on_the_entry_that_defines_it_not_another_that_changes_its_module(self):
        # Both entries change the test's module, but only a's tip defines the new test: b, which only adds a comment
        # there, is not the test's author and lands (vocaro/queuekeeper#129).
        added_runs = self.base / "added-runs"
        self.commit("tests/test_flaky.py", "b", "# A note b adds.\n" + self.main_module())
        self.commit("tests/test_flaky.py", "a", self.adding(added_runs))
        ids = self.add("b", "a")
        with contextlib.redirect_stdout(io.StringIO()) as said: self.assertEqual(self.work(), 0)
        log = said.getvalue()
        test = "test_flaky.Flaky.test_added"
        self.assertIn(f"{test} passed alone, but origin/main has no such test", log)
        self.assertIn(f"blaming {ids[1]} (Add tests/test_flaky.py), which adds {test}; sending it back and gating the "
                      "other 1 at once instead of bisecting", log)
        self.assertNotIn("fail together", log)
        entries = [self.state(identifier) for identifier in ids]
        self.assertEqual([entry["state"] for entry in entries], ["landed", "failed"])
        self.assertIn(f"{test}, a test this entry adds, failed the gate", entries[1]["why"])
        self.assertEqual(self.tracked, [])
        self.assertTrue(all("flakes" not in entry for entry in entries))

    def test_an_added_test_that_two_entries_define_blames_neither_and_bisects(self):
        # b builds on a and changes the module again, so both tips define the new test: the worker cannot tell which
        # entry adds it, and it bisects rather than send back either.
        added_runs = self.base / "added-runs"
        self.commit("tests/test_flaky.py", "a", self.adding(added_runs))
        self.commit("tests/test_flaky.py", "b", (self.repo / "tests/test_flaky.py").read_text() + "# b's note\n",
                    start="a")
        ids = self.add("a", "b")
        with contextlib.redirect_stdout(io.StringIO()) as said: self.assertEqual(self.work(), 0)
        log = said.getvalue()
        test = "test_flaky.Flaky.test_added"
        self.assertIn(f"{test} passed alone, but origin/main has no such test", log)
        self.assertIn(f"{test}, which the batch adds, is added by 2 entries of the batch's 2 that the worker can tell, so "
                      "no commit is blamed: bisecting", log)
        self.assertNotIn("blaming", log)
        self.assertNotIn("flaky:", log)
        self.assertEqual(self.tracked, [])
        self.assertTrue(all("flakes" not in self.state(identifier) for identifier in ids))

    def test_an_old_flake_beside_a_test_the_batch_adds_is_filed_and_the_batch_bisects_blaming_no_commit(self):
        # Both tests fail the gate and pass alone. The old one is a flake, filed as ever; the added one is none. No
        # one entry explains both, so none is blamed and the batch bisects (owner's decision of 2026-11-07,
        # vocaro/queuekeeper#88).
        added_runs, added_plan = self.base / "added-runs", self.base / "added-plan"
        self.plan.write_text("F")
        added_plan.write_text("F")
        self.commit("b", "b")
        self.commit("tests/test_added.py", "a", FLAKY_MODULE.format(runs=str(added_runs), plan=str(added_plan)))
        ids = self.add("b", "a")
        with contextlib.redirect_stdout(io.StringIO()) as said: self.assertEqual(self.work(), 0)
        log = said.getvalue()
        test = "test_added.Flaky.test_sometimes"
        self.assertIn(f"{test} passed alone, but origin/main has no such test, as the batch adds it, so it is no flake "
                      "and no issue is filed", log)
        self.assertIn(f"flaky: {self.TEST} failed the gate at", log)
        self.assertNotIn(f"flaky: {test}", log)
        self.assertIn(f"every failed test passed alone, but the batch reaches {test}, which the batch adds, so "
                      "origin/main has no such test: bisecting", log)
        self.assertIn("2 commits fail together: next the oldest 1", log)
        self.assertNotIn("blaming", log)
        [tracked] = self.tracked
        self.assertEqual(tracked["test"], self.TEST)
        self.assertIn("it also adds a failed test that passed alone, which is no flake", tracked["outcome"])
        for identifier in ids:
            self.assertEqual([flake["test"] for flake in self.state(identifier)["flakes"]], [self.TEST])

    def test_a_flake_of_a_test_origin_main_has_is_still_filed_when_the_batch_adds_other_tests(self):
        # The existing-test side of vocaro/queuekeeper#129: a batch that adds a test module of its own, which passes,
        # still files the old test's flake and lands.
        self.plan.write_text("F")
        self.commit("tests/test_more.py", "a", "import unittest\n\n\nclass More(unittest.TestCase):\n"
                                              "    def test_more(self):\n        pass\n")
        [identifier] = self.add("a")
        with contextlib.redirect_stdout(io.StringIO()) as said: self.assertEqual(self.work(), 0)
        log = said.getvalue()
        self.assertNotIn("origin/main has no such test", log)
        self.assertIn(f"flaky: {self.TEST} failed the gate at", log)
        entry = self.state(identifier)
        self.assertEqual(entry["state"], "landed")
        self.assertEqual([flake["test"] for flake in entry["flakes"]], [self.TEST])
        [tracked] = self.tracked
        self.assertEqual(tracked["test"], self.TEST)

    def test_a_lone_entry_whose_failure_is_real_stops_at_the_first_real_rerun(self):
        self.enterContext(patch.object(mq, "RERUNS", 2))
        self.plan.write_text("FF")
        self.commit("docs/a.md", "a", "The wrongword\n")
        [identifier] = self.add("a")
        with contextlib.redirect_stdout(io.StringIO()) as said: self.assertEqual(self.work(), 0)
        log = said.getvalue()
        self.assertEqual(log.count("so the failure is real"), 1)
        self.assertNotIn("blaming", log)
        self.assertEqual(self.state(identifier)["state"], "failed")

    LOADED = (70.0, 99.0, 75.0)

    def rejected(self, *commits):
        """The why of the one entry added, a lone real failure of TEST, committed as commits say."""
        self.plan.write_text("FF")
        for number, (path, *content) in enumerate(commits):
            self.commit(path, f"branch-{number}", *content)
        [identifier] = self.add(*[f"branch-{number}" for number in range(len(commits))])
        with contextlib.redirect_stdout(io.StringIO()): self.assertEqual(self.work(), 0)
        entry = self.state(identifier)
        self.assertEqual(entry["state"], "failed")
        # The gate and the one rerun that confirmed it, as without the rejection's added words.
        self.assertEqual(self.runs_of_the_test(), 2)
        return entry["why"]

    def test_a_rejection_on_a_loaded_host_in_a_file_the_entry_does_not_touch_says_so(self):
        self.load_of.return_value = self.LOADED
        why = self.rejected(("docs/a.md", "The word\n"))
        self.assertIn("the queue's lanes fail on this commit on top of origin/main: fix it and add it again. While gate "
                      "ran on this Mac, the one-minute load was highest 70, last 70 over 1 reading, and the CPU was at most "
                      "99% busy. Both count the lane's own work", why)
        self.assertNotIn("busy limit", why)
        self.assertIn(f"This entry changes no file of the failed tests {self.TEST}: neither their modules nor what those "
                      "import", why)

    def test_a_rejection_that_changes_the_failing_tests_file_names_it(self):
        changed = (self.repo / "tests/test_flaky.py").read_text() + "# changed\n"
        why = self.rejected(("tests/test_flaky.py", changed))
        self.assertIn(f"This entry changes tests/test_flaky.py, a file of the failed tests {self.TEST}", why)
        self.assertNotIn("changes no file", why)

    def test_a_rejection_that_changes_a_file_the_failing_test_imports_names_it(self):
        why = self.rejected(("src/python/fixturelib/helper.py", "from . import deeper\nVALUE = 1  # changed\n"))
        self.assertIn(f"This entry changes src/python/fixturelib/helper.py, a file of the failed tests {self.TEST}", why)
        self.assertNotIn("changes no file", why)

    def test_a_rejection_says_plainly_that_the_load_could_not_be_read(self):
        for name, setting in (("raises", {"side_effect": mq.remote.Unavailable("gone")}),
                              ("empty", {"return_value": (None, None, None), "side_effect": None})):
            with self.subTest(name):
                self.runs.unlink(missing_ok=True)
                self.load_of.configure_mock(**setting)
                why = self.rejected(("docs/a.md", f"The word {name}\n"))
                self.assertIn("While gate ran on this Mac, the one-minute load could not be read, and the CPU busy share "
                              "could not be read.", why)
                self.assertNotIn("highest", why)

    def test_a_rejection_of_a_gate_the_farm_mac_ran_says_that_macs_load_and_never_this_macs(self):
        readable = lambda host: (12.0, 40.0, 75.0) if host == "fake-mac" else self.LOADED
        self.load_of.side_effect = readable
        with self.farm_mac(): why = self.rejected(("docs/a.md", "The word\n"))
        self.assertIn("While gate ran on fake-mac, the one-minute load was highest 12, last 12 over 1 "
                      "reading, and the CPU was at most 40% busy. Both count the lane's own work", why)
        self.assertNotIn("highest 70", why)

    def test_a_rejection_from_a_farm_mac_that_cannot_be_read_does_not_say_this_macs_load(self):
        def reading(host):
            if host == "fake-mac": raise mq.remote.Unavailable("gone")
            return self.LOADED
        self.load_of.side_effect = reading
        with self.farm_mac(): why = self.rejected(("docs/a.md", "The word\n"))
        self.assertIn("While gate ran on fake-mac, the one-minute load could not be read", why)
        self.assertNotIn("highest", why)
        self.assertNotIn("this Mac", why)

    def test_a_rejection_says_plainly_when_the_tests_module_cannot_be_found(self):
        with patch.object(mq, "imported_files", return_value=None):
            why = self.rejected(("docs/a.md", "The word\n"))
        self.assertIn(f"{self.TEST} failed. The module of {self.TEST} cannot be found, so whether this entry changes their "
                      "files is unknown", why)
        self.assertNotIn("changes no file", why)

    def test_a_rejection_whose_commits_cannot_be_read_says_so_and_is_still_a_rejection(self):
        with patch.object(mq.Worker, "changed_by", side_effect=RuntimeError("git log failed")):
            why = self.rejected(("docs/a.md", "The word\n"))
        self.assertIn("Whether this entry changes a file of the failed tests could not be determined: git log failed", why)
        self.assertNotIn("changes no file", why)

    def test_the_entry_blame_sends_back_says_the_load_and_its_own_files_and_the_rest_land(self):
        self.load_of.return_value = self.LOADED
        self.plan.write_text("FF")
        self.commit("b", "b")
        self.commit("src/python/fixturelib/helper.py", "a", "from . import deeper\nVALUE = 1  # changed\n")
        ids = self.add("b", "a")
        with contextlib.redirect_stdout(io.StringIO()): self.assertEqual(self.work(), 0)
        entries = [self.state(identifier) for identifier in ids]
        self.assertEqual([entry["state"] for entry in entries], ["landed", "failed"])
        self.assertIn("Fix it and add it again. While gate ran on this Mac, the one-minute load was highest 70", entries[1]["why"])
        self.assertIn(f"This entry changes src/python/fixturelib/helper.py, a file of the failed tests {self.TEST}",
                      entries[1]["why"])
        self.assertNotIn("why", entries[0])

    def test_a_flake_in_a_batch_that_changes_a_file_its_module_imports_bisects_and_is_still_recorded(self):
        self.plan.write_text("F")
        self.commit("src/python/fixturelib/helper.py", "a", "from . import deeper\nVALUE = 1  # changed\n")
        self.commit("b", "b")
        ids = self.add("a", "b")
        with contextlib.redirect_stdout(io.StringIO()) as said: self.assertEqual(self.work(), 0)
        log = said.getvalue()
        self.assertIn(f"every failed test passed alone, but the batch reaches {self.TEST}, which changes "
                      "src/python/fixturelib/helper.py, which it imports: bisecting", log)
        self.assertIn("2 commits fail together: next the oldest 1", log)
        self.assertEqual([self.state(identifier)["state"] for identifier in ids], ["landed", "landed"])
        self.assertEqual(self.runs_of_the_test(), 1 + mq.RERUNS + 2, log)
        [tracked] = self.tracked
        self.assertIn("bisected", tracked["outcome"])
        self.assertTrue(all(self.state(identifier)["flakes"][0]["test"] == self.TEST for identifier in ids))
        self.assertNotIn(f"{mq.FLAKY}:", self.main_messages())

    def test_a_file_the_tests_module_does_not_import_itself_does_not_stop_the_landing(self):
        """Only the module's own imports count: deeper.py, which helper.py imports, is not one of them."""
        self.plan.write_text("F")
        self.commit("src/python/fixturelib/deeper.py", "a", "DEPTH = 2\n")
        [identifier] = self.add("a")
        with contextlib.redirect_stdout(io.StringIO()) as said: self.assertEqual(self.work(), 0)
        self.assertIn("landing the batch", said.getvalue())
        self.assertEqual(self.state(identifier)["state"], "landed")
        self.assertIn(f"{mq.FLAKY}: {self.TEST}", self.tip_message())

    def test_a_host_that_does_not_settle_bisects_without_rerunning(self):
        self.plan.write_text("F")
        self.commit("a", "a"); self.commit("b", "b")
        ids = self.add("a", "b")
        with self.host([self.BUSY]) as (slept, said):
            self.assertEqual(self.work(), 0)
        self.assertIn(f"the failed tests are not rerun, as the host did not settle in {mq.SETTLE_LIMIT}s", said.getvalue())
        self.assertIn("2 commits fail together", said.getvalue())
        self.assertEqual(sum(slept), mq.SETTLE_LIMIT)
        self.assertEqual([self.state(identifier)["state"] for identifier in ids], ["landed", "landed"])
        self.assertEqual(self.runs_of_the_test(), 3, said.getvalue())
        self.assertEqual(self.tracked, [])

    def test_more_failed_tests_than_a_flake_would_bisect_at_once(self):
        self.plan.write_text("F")
        self.commit("a", "a"); self.commit("b", "b")
        self.add("a", "b")
        with patch.object(mq, "RERUN_MOST", 0), contextlib.redirect_stdout(io.StringIO()) as said:
            self.assertEqual(self.work(), 0)
        self.assertIn("gate failed 1 tests, more than the 0 a flake would: bisecting", said.getvalue())
        self.assertEqual(self.runs_of_the_test(), 3)
        self.assertEqual(self.pressure.call_count, 0)

    def test_a_gate_with_an_option_a_rerun_does_not_know_bisects_and_says_why(self):
        """A qk unittest option with no role keeps its gate from flake reruns, but never silently (#259)."""
        self.plan.write_text("F")
        self.commit("a", "a"); self.commit("b", "b")
        ids = self.add("a", "b")
        with patch.dict(mq.RERUN_OPTIONS), contextlib.redirect_stdout(io.StringIO()) as said:
            del mq.RERUN_OPTIONS["-v"]
            self.assertEqual(self.work(), 0)
        log = said.getvalue()
        self.assertIn("gate's qk unittest command cannot be rerun one test at a time, as it has -v, an option of qk "
                      "unittest that a rerun of one test does not know: its failed tests are not rerun alone", log)
        self.assertIn("2 commits fail together", log)
        self.assertEqual([self.state(identifier)["state"] for identifier in ids], ["landed", "landed"])
        self.assertEqual(self.tracked, [])

    def test_a_gate_that_is_not_eng_unittest_bisects_at_once_as_before(self):
        config = self.config()
        config["lanes"]["gate"] = {"command": ["python3", "lane.py", str(self.counter), str(self.status)]}
        self.publish(config)
        self.commit("a", "a"); self.commit("bad", "bad")
        ids = self.add("a", "bad")
        with contextlib.redirect_stdout(io.StringIO()) as said: self.assertEqual(self.work(), 0)
        self.assertEqual([self.state(identifier)["state"] for identifier in ids], ["landed", "failed"])
        self.assertNotIn("rerun", said.getvalue())
        self.assertEqual(self.pressure.call_count, 0)
        self.assertEqual(self.tracked, [])


UNITTEST_LOG = """queue: 12:00:00 gate on abc
== test_a.A.test_fine: OK (1 tests, 0.4s)
test_fine (test_a.A.test_fine) ... ok
== test_b.B: FAILED (failures=1, errors=1) (3 tests, 2.0s)
test_one (test_b.B.test_one) ... FAIL
test_two (test_b.B.test_two) ... ERROR
======================================================================
FAIL: test_one (test_b.B.test_one)
----------------------------------------------------------------------
AssertionError
======================================================================
ERROR: test_two (test_b.B.test_two) (case=1)
----------------------------------------------------------------------
RuntimeError
== test_c: FAILED (failures=1) (1 tests, 1.0s)
FAIL: test_old (test_c.C)
----------------------------------------------------------------------
queuekeeper unittest: 5 tests (0 skipped) in 3 processes, 4 at a time, 3.4s: FAILED: test_b.B (FAILED (failures=1, errors=1), status 1), test_c (FAILED (failures=1), status 1)
"""


class FlakyParts(unittest.TestCase):
    """The parts of #231's reruns alone: the failed tests a log names, the rerun's command, a test module's imports and
    the tracker, which a fake gh stands in for."""

    def test_the_failed_tests_are_read_from_each_failed_process_output(self):
        self.assertEqual(mq.failed_tests(UNITTEST_LOG), (["test_b.B.test_one", "test_b.B.test_two", "test_c.C.test_old"], None))

    def test_a_log_whose_failure_cannot_be_rerun_alone_names_why(self):
        summary = UNITTEST_LOG.splitlines()[-1]
        cases = {
            "its log has no qk unittest summary": UNITTEST_LOG.replace(summary, "FAILED"),
            "qk unittest's verdict is OK": UNITTEST_LOG.replace(summary.split("s: ", 1)[1], "OK"),
            "qk unittest also left tests unmeasured": UNITTEST_LOG.replace(summary, summary + "; UNMEASURED: test_d (timed out after 1800s)"),
            "test_c ran 0 of 1 tests": UNITTEST_LOG.replace("test_c (FAILED (failures=1), status 1)", "test_c (ran 0 of 1 tests)"),
            "the output of test_c names no failed test": UNITTEST_LOG.replace("FAIL: test_old (test_c.C)", "Segmentation fault"),
            "test_c did not load": UNITTEST_LOG.replace("FAIL: test_old (test_c.C)", "ERROR: test_c (unittest.loader._FailedTest.test_c)"),
        }
        for why, log in cases.items():
            with self.subTest(why=why):
                self.assertEqual(mq.failed_tests(log), (None, why))

    def test_a_path_is_named_only_whole_and_with_a_directory_or_a_plugin(self):
        for output in ("docs/a.md:3: misspelling wrongword", "see docs/a.md.", "'docs/a.md'", "(docs/a.md)"):
            with self.subTest(output=output):
                self.assertTrue(mq.named_in("docs/a.md", output))
        # Negative controls: a longer path or name around it, and a bare word.
        for path, output in (("docs/a.md", "docs/a.mdx:1:"), ("docs/a.md", "old/docs/a.md:1:"),
                             ("docs/a.md", "docs/a.md.orig:1:"), ("a", "a failure"), ("README", "README:1:")):
            with self.subTest(path=path, output=output):
                self.assertFalse(mq.named_in(path, output))

    def test_the_rerun_keeps_the_suites_options_and_names_its_own_test(self):
        own = ["python3", "scripts/qk", "unittest", "-s", "tests", "--split", "test", "-v"]
        self.assertEqual(mq.rerun_command(own), ["python3", "scripts/qk", "unittest", "-s", "tests", "--split", "test", "-j", "1", "-v"])
        self.assertEqual(mq.rerun_command(["qk", "unittest", "-p", "test_x*.py", "-j", "8", "-k", "slow", "--timeout=60",
                                           "-t", ".", "test_x.Selected"]),
                         ["qk", "unittest", "-p", "test_x*.py", "--timeout=60", "-t", ".", "-j", "1", "-v"])
        batched = ["qk", "unittest", "--split", "class", "--batch-size", "4"]
        self.assertEqual(mq.rerun_command(batched + ["-j", "8"]), batched + ["-j", "1", "-v"])
        # One test alone has nothing to stop early: a gate's --fail-fast stays with the gate.
        self.assertEqual(mq.rerun_command(batched + ["-j", "8", "--fail-fast"]), batched + ["-j", "1", "-v"])
        # Negative controls: a lane that is not qk unittest, an option the rerun cannot carry, a value left out.
        for command in (["python3", "lane.py"], ["python3", "scripts/qk", "xctest"], ["python3", "-m", "unittest"],
                        ["python3", "scripts/qk", "unittest", "--failfast"], ["python3", "scripts/qk", "unittest", "-s"],
                        None, "python3 scripts/qk unittest"):
            with self.subTest(command=command):
                self.assertIsNone(mq.rerun_command(command))
        self.assertEqual([mq.rerun_unread(command) for command in (["python3", "lane.py"], None, ["qk", "unittest", "-v"])],
                         [None, None, None])
        self.assertEqual(mq.rerun_unread(["qk", "unittest", "--failfast"]),
                         "it has --failfast, an option of qk unittest that a rerun of one test does not know")
        self.assertEqual(mq.rerun_unread(["qk", "unittest", "-s"]), "its -s has no value")
        self.assertEqual(mq.rerun_unread(["qk", "unittest", "--verbose=1"]),
                         "it gives --verbose, which takes no value, a value")

    def test_the_rerun_reads_every_option_of_qk_unittest_by_its_role(self):
        """Each option the runner's own parser defines, a new one too, is kept or dropped by its role, so none can
        turn flake reruns off unseen (#259)."""
        base = ["qk", "unittest"]
        for action in mq.parallel.unittest_options():
            if not action.option_strings: continue
            role = mq.parallel.UNITTEST_ROLES[action.dest]
            for option in action.option_strings:
                given = [option] if action.nargs == 0 else [option, str((action.choices or ["4"])[0])]
                with self.subTest(option=option, role=role):
                    kept = given if role in ("suite", "shape") else []
                    self.assertEqual(mq.rerun_command(base + given + ["test_a.A"]), base + kept + ["-j", "1", "-v"])

    def test_a_group_counts_as_its_one_eng_unittest_lane(self):
        suite = {"command": ["python3", "scripts/qk", "unittest", "-s", "tests", "-p", "test_a*.py"]}
        other = {"command": ["swift", "test"]}
        lanes = {"suite": suite, "suite2": suite, "other": other, "plain": suite,
                 "gate": {"parallel": ["suite", "other"]}, "two": {"parallel": ["suite", "suite2"]},
                 "none": {"parallel": ["other"]}}
        self.assertEqual(mq.unittest_member(lanes, "gate"), "suite")
        self.assertEqual(mq.unittest_member(lanes, "plain"), "plain")
        self.assertEqual(mq.unittest_lanes({"lanes": lanes, "queue": {"lanes": ["gate"]}}), {"gate": ("tests", ["test_a*.py"])})
        # Negative controls: two such lanes, none, and no lane at all give no one lane to rerun.
        for lane in ("two", "none", "gone"):
            with self.subTest(lane=lane):
                self.assertIsNone(mq.unittest_member(lanes, lane))

    def test_a_test_modules_files_are_itself_and_the_projects_files_it_imports_directly(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = {"tests/test_x.py": "import os\nfrom pkg import a\nimport pkg.b as bb\n"
                                        "def later():\n    from pkg.c import thing\n",
                     "tests/suite/__init__.py": "", "tests/suite/test_y.py": "from . import helper\nfrom .more import x\n",
                     "tests/suite/helper.py": "", "tests/suite/more.py": "x = 1\n",
                     "src/python/pkg/__init__.py": "", "src/python/pkg/a.py": "from pkg import d\n",
                     "src/python/pkg/b.py": "", "src/python/pkg/c.py": "thing = 1\n", "src/python/pkg/d.py": ""}
            for name, body in files.items():
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_text(body)
            self.assertEqual(mq.imported_files(root, root / "tests", "test_x.X.test_one"),
                             ["src/python/pkg/__init__.py", "src/python/pkg/a.py", "src/python/pkg/b.py",
                              "src/python/pkg/c.py", "tests/test_x.py"])
            self.assertEqual(mq.imported_files(root, root / "tests", "suite.test_y.Y.test_two"),
                             ["tests/suite/__init__.py", "tests/suite/helper.py", "tests/suite/more.py",
                              "tests/suite/test_y.py"])
            self.assertIsNone(mq.imported_files(root, root / "tests", "test_missing.X.test_one"))

    def test_a_test_is_added_when_origin_main_lacks_its_module_or_its_method_and_never_when_it_cannot_tell(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "tests").mkdir()
            old = "import unittest\n\n\nclass X(unittest.TestCase):\n    def test_old(self):\n        pass\n"
            (root / "tests/test_x.py").write_text(old)
            run(root, "git", "init", "-q")
            run(root, "git", "add", ".")
            run(root, "git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "base")
            base = run(root, "git", "rev-parse", "HEAD")
            (root / "tests/test_x.py").write_text(old + "\n    def test_new(self):\n        pass\n\n\n"
                                                  "class Y(X):\n    pass\n")
            (root / "tests/test_y.py").write_text(old)
            added = lambda test: mq.added_test(root, root / "tests", test, base)
            self.assertIsNone(added("test_x.X.test_old"))
            self.assertEqual(added("test_x.X.test_new"), ("tests/test_x.py", ["X", "test_new"]))
            self.assertEqual(added("test_y.X.test_old"), ("tests/test_y.py", ["X", "test_old"]))
            # A test a base class gives is not in its class's own body, so whether main has it is not told.
            self.assertIsNone(added("test_x.Y.test_old"))
            self.assertIsNone(added("test_missing.X.test_old"))
            self.assertIsNone(mq.added_test(root, root / "tests", "test_x.X.test_new", "0" * 40))

    FLAKE = {"test": "test_b.B.test_one", "commit": "c" * 40, "gateLog": "/logs/gate.log",
             "rerunLogs": ["/logs/rerun-1.log", "/logs/rerun-2.log"], "outcome": "The batch landed.", "mac": "fixture-mac"}

    def gh(self, answers):
        """A fake gh: each call's answer by its first two words, recorded in calls."""
        calls = []
        def gh(*arguments):
            calls.append(arguments)
            code, out, err = answers[" ".join(arguments[:2])]
            return subprocess.CompletedProcess(["gh", *arguments], code, out, err)
        return gh, calls

    def test_the_tracker_comments_on_the_open_issue_that_names_the_test(self):
        gh, calls = self.gh({"issue list": (0, json.dumps([{"number": 230, "url": "https://x/issues/230", "title": "t"}]), ""),
                             "issue comment": (0, "https://x/issues/230#c\n", "")})
        self.assertEqual(mq.hardening(Path("."), self.FLAKE, gh), ("https://x/issues/230", None))
        self.assertEqual([call[:2] for call in calls], [("issue", "list"), ("issue", "comment")])
        self.assertIn('"test_one" in:title,body,comments', calls[0])
        self.assertEqual(calls[1][2], "230")
        body = calls[1][calls[1].index("--body") + 1]
        for said in ("`test_b.B.test_one`", "passed alone 2 of 2 times", "The batch landed.", "`/logs/gate.log`",
                     "`/logs/rerun-2.log`", "fixture-mac", "vocaro/vocaro-engineering#231"):
            self.assertIn(said, body)

    def test_the_tracker_files_a_hardening_issue_when_none_names_the_test(self):
        gh, calls = self.gh({"issue list": (0, "[]", ""), "issue create": (0, "https://x/issues/240\n", "")})
        self.assertEqual(mq.hardening(Path("."), self.FLAKE, gh), ("https://x/issues/240", None))
        self.assertEqual([call[:2] for call in calls], [("issue", "list"), ("issue", "create")])
        self.assertEqual(calls[1][calls[1].index("--title") + 1], "Harden the flaky test test_one (test_b.B)")

    @staticmethod
    def block(test, where, said, frames=1):
        return ("=" * 70 + f"\nFAIL: {test} ({where})\n" + "-" * 70 + "\nTraceback (most recent call last):\n"
                + "".join(f'  File "/x/tests/test_b.py", line {line}, in {test}\n    step()\n' for line in range(frames))
                + f"AssertionError: {said}\n\n")

    def gate_log(self, text):
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder)
        (folder / "gate.log").write_text(text)
        return {**self.FLAKE, "gateLog": str(folder / "gate.log")}

    def test_the_issue_quotes_the_tests_first_failure_from_the_gate_log(self):
        # vocaro/queuekeeper#128: the logs the queue names are later pruned, so the issue quotes the failure itself.
        flake = self.gate_log("== test_b: FAILED (3 tests, 1.0s)\n" + self.block("test_two", "test_b.B.test_two", "other")
                              + self.block("test_one", "test_b.B.test_one", "first ```fenced``` 1 != 2")
                              + self.block("test_one", "test_b.B.test_one", "second") + "-" * 70 + "\nRan 3 tests\n")
        for answers in ({"issue list": (0, "[]", ""), "issue create": (0, "https://x/issues/240\n", "")},
                        {"issue list": (0, json.dumps([{"number": 230, "url": "u", "title": "t"}]), ""),
                         "issue comment": (0, "u#c\n", "")}):
            with self.subTest(answers=list(answers)[1]):
                gh, calls = self.gh(answers)
                mq.hardening(Path("."), flake, gh)
                body = calls[1][calls[1].index("--body") + 1]
                self.assertIn("````\nFAIL: test_one (test_b.B.test_one)\n", body)
                self.assertIn("AssertionError: first ```fenced``` 1 != 2\n````\n", body)
                self.assertIn(f"- Gate log: `{flake['gateLog']}`", body)
                for absent in ("AssertionError: second", "AssertionError: other", "Ran 3 tests"):
                    self.assertNotIn(absent, body)

    def test_a_long_failure_keeps_its_heading_and_its_end_within_the_limit(self):
        log = self.block("test_one", "test_b.B.test_one", "the end " + "x" * 900, frames=200)
        quoted = mq.flake_failure(log, "test_b.B.test_one")
        self.assertLessEqual(sum(len(line) + 1 for line in quoted), mq.FAILURE_EXCERPT_LIMIT)
        self.assertEqual(quoted[0], "FAIL: test_one (test_b.B.test_one)")
        self.assertRegex(quoted[1], r"^\[\.\.\. \d+ lines left out \.\.\.\]$")
        self.assertTrue(quoted[-1].startswith("AssertionError: the end x") and quoted[-1].endswith("..."), quoted[-1])
        self.assertEqual(len(quoted[-1]), mq.FAILURE_LINE_LIMIT)
        self.assertEqual(mq.flake_failure(log, "test_b.B.test_two"), [])

    def test_an_issue_with_no_failure_to_quote_says_so(self):
        for flake in (self.FLAKE, self.gate_log("no failure here\n")):
            with self.subTest(log=flake["gateLog"]):
                gh, calls = self.gh({"issue list": (0, "[]", ""), "issue create": (0, "https://x/issues/240\n", "")})
                mq.hardening(Path("."), flake, gh)
                self.assertIn("named no `FAIL:` or `ERROR:` block", calls[1][calls[1].index("--body") + 1])

    def test_a_tracker_gh_cannot_reach_is_said_and_stops_nothing(self):
        for answers, why in (({"issue list": (1, "", "boom\n")}, "gh issue list failed: boom"),
                             ({"issue list": (0, "not json", "")}, "gh issue list printed no issue list"),
                             ({"issue list": (0, json.dumps([{"number": 1, "url": "u"}]), ""), "issue comment": (1, "", "no\n")},
                              "gh issue comment on u failed: no"),
                             ({"issue list": (0, "[]", ""), "issue create": (1, "", "denied\n")}, "gh issue create failed: denied")):
            with self.subTest(why=why):
                gh, _ = self.gh(answers)
                url, said = mq.hardening(Path("."), self.FLAKE, gh)
                self.assertIsNone(url)
                self.assertTrue(said.startswith(why), said)



# A lane that is no qk unittest: its own runner, which writes JUnit XML into the lane's results folder and reruns the
# one test it is given (vocaro/queuekeeper#109).
JUNIT_LANE = """import os, pathlib, sys
from xml.sax.saxutils import quoteattr

RUNS, PLAN = pathlib.Path({runs!r}), pathlib.Path({plan!r})


def sometimes():
    done = len(RUNS.read_text()) if RUNS.exists() else 0
    RUNS.write_text("x" * (done + 1))
    plan = PLAN.read_text() if PLAN.exists() else ""
    return plan[done:done + 1] != "F"


TESTS = {{"suite.Flaky.sometimes": sometimes, "suite.Flaky.steady": lambda: True}}
chosen = sys.argv[1:] or list(TESTS)
passed = {{name: TESTS[name]() for name in chosen}}
for name, ok in passed.items(): print(("ok " if ok else "FAILED ") + name)
folder = os.environ.get("QUEUEKEEPER_RESULTS_DIR")
if folder and not sys.argv[1:]:
    cases = "".join("<testcase classname=" + quoteattr(name.rpartition(".")[0]) + " name="
                    + quoteattr(name.rpartition(".")[2]) + ">" + ("" if ok else "<failure message='planned'/>")
                    + "</testcase>" for name, ok in passed.items())
    pathlib.Path(folder).mkdir(parents=True, exist_ok=True)
    (pathlib.Path(folder) / "junit.xml").write_text("<testsuites><testsuite>" + cases + "</testsuite></testsuites>")
sys.exit(0 if all(passed.values()) else 1)
"""


class DeclaredFlakyReruns(Fixture):
    """A lane that is not qk unittest but whose failedTests name its failed tests in JUnit XML and how to rerun one
    reruns them alone before a bisect, as a qk unittest lane does (vocaro/queuekeeper#109)."""
    TEST = "suite.Flaky.sometimes"

    def setUp(self):
        super().setUp()
        self.runs, self.plan = self.base / "runs", self.base / "plan"
        (self.repo / "tests").mkdir()
        (self.repo / "tests/lane.py").write_text(JUNIT_LANE.format(runs=str(self.runs), plan=str(self.plan)))
        self.gate({"paths": ["tests/"]})

    def gate(self, failed):
        config = self.config()
        config["lanes"]["gate"] = {"command": ["python3", "tests/lane.py"], "timeout": 600, "failedTests": {
            "results": "junit.xml", "rerun": ["python3", "tests/lane.py", "{test}"], **failed}}
        if not failed: del config["lanes"]["gate"]["failedTests"]
        (self.repo / "queuekeeper.json").write_text(json.dumps(config))
        run(self.repo, "git", "add", ".")
        run(self.repo, "git", "commit", "-qm", "Gate on a lane of its own runner")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")

    def runs_of_the_test(self):
        return len(self.runs.read_text()) if self.runs.exists() else 0

    def worked(self):
        with contextlib.redirect_stdout(io.StringIO()) as said: self.assertEqual(self.work(), 0)
        return said.getvalue()

    def test_a_failed_test_that_passes_alone_lands_the_batch_and_is_recorded(self):
        self.plan.write_text("F")
        self.commit("a", "a"); self.commit("b", "b")
        ids = self.add("a", "b")
        log = self.worked()
        entries = [self.state(identifier) for identifier in ids]
        self.assertEqual([entry["state"] for entry in entries], ["landed", "landed"])
        self.assertEqual(self.runs_of_the_test(), 1 + mq.RERUNS)
        self.assertIn(f"gate failed 1 test at ", log)
        self.assertIn(f": {self.TEST}; rerunning each alone up to {mq.RERUNS} times on that commit before a bisect", log)
        self.assertIn("no commit of the batch changes a path gate's tests depend on: landing the batch", log)
        self.assertNotIn("fail together", log)
        for entry in entries:
            [flake] = entry["flakes"]
            self.assertEqual(flake["test"], self.TEST)
            self.assertEqual(len(flake["rerunLogs"]), mq.RERUNS)
            self.assertTrue(all("ok suite.Flaky.sometimes" in Path(path).read_text() for path in flake["rerunLogs"]))
        [tracked] = self.tracked
        self.assertIn("the batch landed", tracked["outcome"])

    def test_a_batch_that_changes_a_path_its_tests_depend_on_bisects_with_the_flake_recorded(self):
        self.gate({"paths": ["tests/", "a"]})
        self.plan.write_text("F")
        self.commit("a", "a"); self.commit("b", "b")
        ids = self.add("a", "b")
        log = self.worked()
        self.assertIn("every failed test passed alone, but the batch changes a, which gate's tests depend on: bisecting",
                      log)
        self.assertEqual([self.state(identifier)["state"] for identifier in ids], ["landed", "landed"])
        [tracked] = self.tracked
        self.assertIn("The batch was bisected, as the batch changes a", tracked["outcome"])

    def test_a_lane_that_lists_no_paths_bisects_once_its_failed_tests_pass_alone(self):
        self.gate({"paths": []})
        self.plan.write_text("F")
        self.commit("a", "a"); self.commit("b", "b")
        self.add("a", "b")
        log = self.worked()
        self.assertIn("gate's failedTests list no paths its tests depend on, so any change may reach them: bisecting", log)

    def test_a_failure_a_rerun_confirms_sends_the_lone_entry_back(self):
        self.plan.write_text("FF")
        self.commit("a", "a")
        [identifier] = self.add("a")
        log = self.worked()
        self.assertIn(f"{self.TEST} failed again alone, on rerun 1 (exit 1), so the failure is real", log)
        self.assertEqual(self.runs_of_the_test(), 2)
        entry = self.state(identifier)
        self.assertNotEqual(entry["state"], "landed")
        self.assertNotIn("flakes", entry)
        self.assertEqual(self.tracked, [])

    def test_a_lane_without_failed_tests_bisects_as_before(self):
        # The negative control: the same lane, failing the same way, with no failedTests, reruns nothing.
        self.gate({})
        self.plan.write_text("F")
        self.commit("a", "a"); self.commit("b", "b")
        self.add("a", "b")
        log = self.worked()
        self.assertNotIn("rerunning each alone", log)
        self.assertEqual(self.tracked, [])


class DeclaredFailures(unittest.TestCase):
    """The parts of a failedTests lane's reruns: the JUnit XML it names its failures in and the setting itself
    (vocaro/queuekeeper#109)."""

    def test_the_failed_and_erred_cases_of_a_junit_file_are_named_classname_dot_name(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "junit.xml"
            path.write_text("<testsuites><testsuite><testcase classname='a.B' name='one'><failure/></testcase>"
                            "<testcase classname='a.B' name='two'/><testcase name='three'><error/></testcase>"
                            "<testcase classname='a.B' name='one'><failure/></testcase></testsuite></testsuites>")
            self.assertEqual(mq.junit_failures(path), (["a.B.one", "three"], None))
            path.write_text("<testsuite><testcase classname='a.B' name='two'/></testsuite>")
            self.assertEqual(mq.junit_failures(path), (None, f"{path} names no failed test"))
            path.write_text("<testsuite>")
            self.assertTrue(mq.junit_failures(path)[1].startswith(f"{path} cannot be read: "))
            self.assertEqual(mq.junit_failures(Path(folder) / "none.xml"),
                             (None, "the lane wrote no none.xml in its results folder"))

    def test_a_group_reruns_its_one_failed_lane_that_declares_failed_tests(self):
        declared = {"results": "j.xml", "rerun": ["t", "{test}"]}
        lanes = {"one": {"command": ["x"], "failedTests": declared}, "two": {"command": ["y"]},
                 "group": {"parallel": ["one", "two"]}}
        self.assertEqual(mq.declared_failures(lanes, "one", []), ("one", declared))
        self.assertEqual(mq.declared_failures(lanes, "group", ["one"]), ("one", declared))
        self.assertIsNone(mq.declared_failures(lanes, "group", ["one", "two"]))
        self.assertIsNone(mq.declared_failures(lanes, "group", ["two"]))
        self.assertIsNone(mq.declared_failures(lanes, "two", []))

    def test_the_setting_is_checked_when_the_configuration_loads(self):
        good = {"results": "junit.xml", "rerun": ["scripts/test.sh", "--only", "{test}"], "paths": ["src/"]}
        for changed, message in (({}, None),
                                 ({"paths": "src/"}, "paths lists the files"),
                                 ({"results": "/abs.xml"}, "relative and inside it"),
                                 ({"results": "../out.xml"}, "relative and inside it"),
                                 ({"rerun": ["scripts/test.sh"]}, "names {test}"),
                                 ({"rerun": "scripts/test.sh {test}"}, "names {test}"),
                                 ({"other": 1}, "needs results and rerun")):
            settings = {key: value for key, value in {**good, **changed}.items() if value is not None}
            with self.subTest(changed), tempfile.TemporaryDirectory() as root:
                (Path(root) / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Lanes", "lanes": {
                    "gate": {"command": ["true"], "failedTests": settings}}}))
                if message is None: configuration.load(root)
                else:
                    with self.assertRaisesRegex(ValueError, f"gate.failedTests.*{message}"): configuration.load(root)

class BothHalves(Fixture):
    """A bisect's two halves gate at once, the newer beside the older, one on each Mac (vocaro-engineering#241).
    Here both run on this Mac, as the fixture has no other Mac: the newer half is not kept off the older half's Mac
    (BOTH_HERE), and every lane may go to another Mac (offerable)."""
    def setUp(self):
        super().setUp()
        for name, value in (("apart", BOTH_HERE), ("BESIDE_POLL", 0.02), ("offerable", lambda config, lanes: None)):
            patcher = patch.object(mq, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def worked(self):
        with contextlib.redirect_stdout(io.StringIO()) as said:
            self.assertEqual(self.work(), 0)
        return said.getvalue()

    @staticmethod
    def rounds(log):
        """The rounds the worker ran, each one gate of the batch or of a bisect's older half."""
        return sum(1 for line in log.splitlines() if " testing " in line and "newer half" not in line
                   and "of the bisect" not in line)

    def test_the_good_older_half_lands_and_the_newer_bisects_at_once(self):
        """[a b bad d] fail; [a b] and [bad d] gate at once, and a b land; [bad] and [d] at once: bad comes back and d
        lands. The same 5 gates as one half at a time, in 3 rounds rather than 5."""
        for name in ("a", "b", "bad", "d"): self.commit(name, name)
        ids = self.add("a", "b", "bad", "d")
        log = self.worked()
        self.assertEqual([self.state(i)["state"] for i in ids], ["landed", "landed", "failed", "landed"])
        # And bad on the main before a b landed, where it fails too, so it names neither (vocaro/queuekeeper#263).
        self.assertEqual(self.gates(), 6)
        self.assertEqual(self.rounds(log), 3, log)
        self.assertIn("comes back alone", log)
        self.assertIn("testing the newer 2 entries of the bisect", log)
        self.assertIn("newer half: 2 commits fail together here too: they bisect once the older half has", log)
        self.assertIn("testing the newer 1 entry of the bisect", log)
        self.assertNotIn("bad", self.main_files())

    def test_the_good_newer_half_lands_while_the_older_bisects(self):
        """[a bad c d] fail; [a bad] and [c d] at once: c d land first, ahead of a, and [a] and [bad] gate at once."""
        for name in ("a", "bad", "c", "d"): self.commit(name, name)
        ids = self.add("a", "bad", "c", "d")
        log = self.worked()
        self.assertEqual([self.state(i)["state"] for i in ids], ["landed", "failed", "landed", "landed"])
        self.assertEqual(self.gates(), 5)
        self.assertEqual(self.rounds(log), 3, log)
        subjects = run(self.repo, "git", "log", "--format=%s", "origin/main").splitlines()
        self.assertLess(subjects.index("Add a"), subjects.index("Add d"))  # a, newest first, landed after c and d

    def test_the_newer_halfs_rejection_says_the_load_and_that_its_tests_are_unknown(self):
        """The rejection Beside settles itself, from the half that ran beside the older one, adds what a lone one does."""
        for name in ("a", "bad", "c", "d"): self.commit(name, name)
        ids = self.add("a", "bad", "c", "d")
        self.worked()
        why = self.state(ids[1])["why"]
        self.assertIn("fix it and add it again. While gate ran on this Mac, the one-minute load was highest 3", why)
        self.assertIn("Which tests failed could not be determined (gate's summary names no failed test; its tests: no "
                      "test counts in its log), so whether this entry changes their files is unknown", why)

    def test_two_that_pass_alone_come_back_naming_each_other(self):
        """Each passes alone, and together they fail: both come back, each naming the other by ID and commit
        (conflictWith), rather than the older landing and the newer coming back alone with no word of why. The pair,
        its halves at once, and the pair again on the main the search runs on: 3 rounds' gates, 4 in all."""
        self.commit("left", "left"); self.commit("right", "right")
        ids = self.add("left", "right")
        log = self.worked()
        left, right = (self.state(i) for i in ids)
        self.assertEqual([left["state"], right["state"]], ["failed", "failed"])
        self.assertEqual(left["conflictWith"], [{"id": ids[1], "sha": right["tip"], "state": "failed"}])
        self.assertEqual(right["conflictWith"], [{"id": ids[0], "sha": left["tip"], "state": "failed"}])
        self.assertIn(f"the queue's lanes fail on this commit together with {ids[1]} at {right['tip']}, though each "
                      "passes alone on top of origin/main", left["why"])
        self.assertIn(f"together with {ids[0]} at {left['tip']}", right["why"])
        self.assertIn("each half passed, though the 2 failed together", log)
        self.assertIn("2 of the 2 sent back after 1 probe", log)
        self.assertEqual(self.gates(), 4)
        self.assertNotIn("left", self.main_files())
        self.assertNotIn("right", self.main_files())
        for entry, other in ((left, right), (right, left)):
            result = agent_results.lookup(mq.Queue(self.repo), entry["id"])
            self.assertEqual(result["failure"]["category"], "fails-together")
            self.assertEqual(result["conflictWith"], [{"id": other["id"], "sha": other["tip"], "state": "failed"}])
            self.assertEqual(result["recovery"]["reasonCode"], "entry-fails-together")
            self.assertEqual(result["recovery"]["conflictWith"], result["conflictWith"])
            self.assertIn(["qk", "--project", str(self.repo.resolve()), "queue", "result", other["id"], "--json"],
                          result["recovery"]["suggestedCommands"])

    def test_the_fewest_that_fail_together_come_back_and_the_rest_land(self):
        """[a left right d] fail, and both halves, [a left] and [right d], pass: the search drops a and d, keeping
        left and right, which come back naming each other, and a and d gate again and land."""
        for name in ("a", "left", "right", "d"): self.commit(name, name)
        ids = self.add("a", "left", "right", "d")
        log = self.worked()
        states = [self.state(i) for i in ids]
        self.assertEqual([state["state"] for state in states], ["landed", "failed", "failed", "landed"])
        self.assertEqual(states[1]["conflictWith"], [{"id": ids[2], "sha": states[2]["tip"], "state": "failed"}])
        self.assertEqual(states[2]["conflictWith"], [{"id": ids[1], "sha": states[1]["tip"], "state": "failed"}])
        # The whole, [left right d], [left d], [left right], then left and right alone, two at once
        # (vocaro/queuekeeper#262), and the search's cost in the round's events.
        self.assertIn("2 of the 4 sent back after 6 probes, 1 pair of them at once, in ", log)
        self.assertIn("the other 2 gate again", log)
        self.assertLessEqual({"a", "d"}, self.main_files())
        searches = mq.events.read(mq.Queue(self.repo).directory, kinds=["round.together"])
        self.assertEqual([(event["probes"], event["atOnce"]) for event in searches], [(6, 1)])

    def test_a_probe_that_fails_only_by_tests_that_pass_alone_drops_no_entry(self):
        """[a left right d]: the probe of [left d] fails, but its failed test passes alone on rerun, so it is no proof
        and right, which left needs, stays; until vocaro/queuekeeper#260 it was dropped, and left and d came back
        naming each other."""
        for name in ("a", "left", "right", "d"): self.commit(name, name)
        ids = self.add("a", "left", "right", "d")
        flaky = frozenset((ids[1], ids[3]))
        gate, rerun, flaked = mq.Probe.gate, mq.Probe.rerun, []
        def flaky_gate(worker, lanes, round_name, where=(), cancel=None, quick=None):
            if frozenset(worker.gating) == flaky:
                flaked.append(round_name)
                log = self.base / f"{round_name}.log"
                log.write_text("a flaky failure\n")
                worker.failed_lane = "gate"
                return 1, log
            return gate(worker, lanes, round_name, where, cancel, quick)
        def flaky_rerun(worker, config, applied, base, head, log, round_name):
            if frozenset(entry["id"] for entry in applied) == flaky:
                worker.repeated, worker.passed_alone = False, True
                return None
            return rerun(worker, config, applied, base, head, log, round_name)
        with patch.object(mq.Probe, "gate", flaky_gate), patch.object(mq.Probe, "rerun", flaky_rerun):
            log = self.worked()
        self.assertEqual(len(flaked), 1)
        states = [self.state(i) for i in ids]
        self.assertEqual([state["state"] for state in states], ["landed", "failed", "failed", "landed"], log)
        self.assertEqual(states[1]["conflictWith"], [{"id": ids[2], "sha": states[2]["tip"], "state": "failed"}])
        self.assertIn(f"{ids[1]}, {ids[3]} fail only by tests that pass alone, which is no proof, so none of them is "
                      "dropped", log)

    def test_a_half_that_passes_by_its_flakes_still_starts_the_search(self):
        """The newer half [right] fails a test that passes alone on rerun, and the older [left] passes: both count as
        passed, so the two come back naming each other, the flake named apart, rather than left landing and right
        coming back alone (vocaro/queuekeeper#261)."""
        self.commit("left", "left"); self.commit("right", "right")
        ids = self.add("left", "right")
        gate, rerun, flaked = mq.Beside.gate, mq.Beside.rerun, []
        test = "tests.test_fixture.Fixture.test_flaky"
        def flaky_gate(worker, lanes, round_name, where=(), cancel=None, quick=None):
            if worker.label == "newer half" and not flaked:
                flaked.append(round_name)
                log = self.base / f"{round_name}.log"
                log.write_text("a flaky failure\n")
                return 1, log
            return gate(worker, lanes, round_name, where, cancel, quick)
        def flaky_rerun(worker, config, applied, base, head, log, round_name):
            if worker.label == "newer half":
                return [{"test": test, "commit": head, "gateLog": str(log), "rerunLogs": [], "mac": "fixture"}]
            return rerun(worker, config, applied, base, head, log, round_name)
        with patch.object(mq.Beside, "gate", flaky_gate), patch.object(mq.Beside, "rerun", flaky_rerun):
            log = self.worked()
        self.assertEqual(len(flaked), 1)
        left, right = (self.state(i) for i in ids)
        self.assertEqual([left["state"], right["state"]], ["failed", "failed"], log)
        self.assertEqual(right["conflictWith"], [{"id": ids[0], "sha": left["tip"], "state": "failed"}])
        self.assertIn(f"each half passed, though the 2 failed together (once {test} passed alone on rerun)", log)
        self.assertIn(f"The bisect's halves passed only once {test} passed alone on rerun, flakes this failure is not.",
                      right["why"])
        self.assertNotIn("left", self.main_files())

    def test_a_halfs_flakes_record_the_search_rather_than_a_landing(self):
        """As above, the newer half [right] passes by a flake, whose rerun chose the outcome of a batch that lands
        (Worker.hold_flakes, as weigh_failure's last step calls it). The search runs instead, so the flake's record,
        on the entry, the tracker and the test.flaky event, says what the search did, not that the batch landed
        (vocaro/queuekeeper#302)."""
        self.commit("left", "left"); self.commit("right", "right")
        ids = self.add("left", "right")
        gate, rerun, flaked = mq.Beside.gate, mq.Beside.rerun, []
        test = "tests.test_fixture.Fixture.test_flaky"
        landed = "No commit of the batch's 1 changes its module or a file it imports, so the batch landed."
        def flaky_gate(worker, lanes, round_name, where=(), cancel=None, quick=None):
            if worker.label == "newer half" and not flaked:
                flaked.append(round_name)
                log = self.base / f"{round_name}.log"
                log.write_text("a flaky failure\n")
                return 1, log
            return gate(worker, lanes, round_name, where, cancel, quick)
        def flaky_rerun(worker, config, applied, base, head, log, round_name):
            if worker.label == "newer half":
                flakes = [{"test": test, "commit": head, "gateLog": str(log), "rerunLogs": [], "mac": "fixture"}]
                worker.hold_flakes(flakes, applied, {}, landed)
                return flakes
            return rerun(worker, config, applied, base, head, log, round_name)
        with patch.object(mq.Beside, "gate", flaky_gate), patch.object(mq.Beside, "rerun", flaky_rerun):
            log = self.worked()
        left, right = (self.state(i) for i in ids)
        self.assertEqual([left["state"], right["state"]], ["failed", "failed"], log)
        said = (f"Both halves of the bisect passed, this one only once its failed tests passed alone, so it did not "
                f"land: the search for the entries that fail together ran instead, and sent back {ids[0]}, {ids[1]}.")
        [flake] = right["flakes"]
        self.assertEqual(flake["outcome"], said)
        self.assertNotIn("flakes", left)
        self.assertEqual([tracked["outcome"] for tracked in self.tracked], [said])
        [event] = mq.events.read(mq.Queue(self.repo).directory, kinds=["test.flaky"])
        self.assertEqual((event["test"], event["landed"], event["entries"]), (test, False, [ids[1]]))

    def test_a_halfs_flakes_still_record_a_landing_when_no_search_runs(self):
        """Negative control: the newer half [d] passes by a flake while the older [bad] fails, so no search runs, d
        lands as before, and its flake's record says the batch landed."""
        for name in ("a", "b", "bad", "d"): self.commit(name, name)
        ids = self.add("a", "b", "bad", "d")
        gate, rerun, flaked = mq.Beside.gate, mq.Beside.rerun, []
        test = "tests.test_fixture.Fixture.test_flaky"
        landed = "No commit of the batch's 1 changes its module or a file it imports, so the batch landed."
        def flaky_gate(worker, lanes, round_name, where=(), cancel=None, quick=None):
            if worker.label == "newer half" and [entry["id"] for entry in worker.newer] == [ids[3]] and not flaked:
                flaked.append(round_name)
                log = self.base / f"{round_name}.log"
                log.write_text("a flaky failure\n")
                return 1, log
            return gate(worker, lanes, round_name, where, cancel, quick)
        def flaky_rerun(worker, config, applied, base, head, log, round_name):
            if worker.label == "newer half" and flaked:
                flakes = [{"test": test, "commit": head, "gateLog": str(log), "rerunLogs": [], "mac": "fixture"}]
                worker.hold_flakes(flakes, applied, {}, landed)
                return flakes
            return rerun(worker, config, applied, base, head, log, round_name)
        with patch.object(mq.Beside, "gate", flaky_gate), patch.object(mq.Beside, "rerun", flaky_rerun):
            log = self.worked()
        self.assertEqual(len(flaked), 1, log)
        self.assertEqual([self.state(i)["state"] for i in ids], ["landed", "landed", "failed", "landed"], log)
        self.assertNotIn("each half passed", log)
        self.assertEqual([flake["outcome"] for flake in self.state(ids[3])["flakes"]], [landed])
        [event] = mq.events.read(mq.Queue(self.repo).directory, kinds=["test.flaky"])
        self.assertTrue(event["landed"])

    def test_a_probe_with_no_verdict_starts_the_bisect_again_from_its_halves(self):
        """A probe that gives no verdict blames nobody: every entry waits again, and the next round gates the halves
        again, whose search then ends as any does."""
        self.commit("left", "left"); self.commit("right", "right")
        ids = self.add("left", "right")
        real, declined = mq.Beside.gate, []
        def gate(worker, lanes, round_name, where=(), cancel=None, quick=None):
            if worker.label == mq.TOGETHER and not declined:
                declined.append(round_name)
                worker.ran_where = ""
                return mq.resources.BUSY, None
            return real(worker, lanes, round_name, where, cancel, quick)
        with patch.object(mq.Beside, "gate", gate):
            log = self.worked()
        self.assertEqual(len(declined), 1)
        self.assertIn(f"the search for the entries that fail together ended without a verdict on {ids[0]}, {ids[1]}: "
                      "the bisect starts again from its halves", log)
        self.assertEqual([self.state(i)["state"] for i in ids], ["failed", "failed"])
        self.assertEqual(log.count("each half passed, though the 2 failed together"), 2, log)

    def one_half_at_a_time(self):
        """The bisect gates one half at a time, as on one Mac, while a second gate of a pair can still run beside the
        first (BOTH_HERE): no newer half goes beside the older."""
        return patch.object(mq.Worker, "beside", lambda worker, *arguments, **keywords: None)

    def test_a_lone_entry_is_gated_on_the_main_before_the_landings_beside_its_own_gate(self):
        """One half at a time, left lands and right then fails alone; its probe on the main before left landed ran
        beside its own gate, not after it, so naming left costs no wall time: 4 gates, one of them at once
        (vocaro/queuekeeper#263)."""
        self.commit("left", "left"); self.commit("right", "right")
        ids = self.add("left", "right")
        with self.one_half_at_a_time():
            log = self.worked()
        left, right = (self.state(i) for i in ids)
        self.assertEqual([left["state"], right["state"]], ["landed", "failed"], log)
        self.assertEqual(right["conflictWith"], [{"id": ids[0], "sha": left["tip"], "state": "landed"}])
        self.assertIn(f"gating {ids[1]} at ", log)
        self.assertIn("the main before this bisect's landings, beside its own gate", log)
        self.assertIn(f"it gated on ", log)
        self.assertIn(f"{ids[1]} sent back naming 1 landed entry after 1 probe, 1 pair of them at once", log)
        self.assertEqual(self.gates(), 4)
        searches = mq.events.read(mq.Queue(self.repo).directory, kinds=["round.together"])
        self.assertEqual([(event["probes"], event["atOnce"]) for event in searches], [(1, 1)])

    def test_a_lone_entry_that_fails_before_the_landings_too_comes_back_alone(self):
        """[a bad]: a lands, and bad fails alone, beside its probe on the main before a, where it fails too: it
        comes back alone, naming no one, after the same 4 gates."""
        self.commit("a", "a"); self.commit("bad", "bad")
        ids = self.add("a", "bad")
        with self.one_half_at_a_time():
            log = self.worked()
        a, bad = (self.state(i) for i in ids)
        self.assertEqual([a["state"], bad["state"]], ["landed", "failed"], log)
        self.assertNotIn("conflictWith", bad)
        self.assertIn("comes back alone", log)
        self.assertEqual(self.gates(), 4)

    def test_a_lone_entry_that_passes_cancels_its_probe_and_lands(self):
        """[a b bad]: a lands, then [b bad] fail, and b, gated alone beside its probe, passes: the probe is
        canceled, b lands, and bad comes back alone."""
        for name in ("a", "b", "bad"): self.commit(name, name)
        ids = self.add("a", "b", "bad")
        with self.one_half_at_a_time():
            log = self.worked()
        self.assertEqual([self.state(i)["state"] for i in ids], ["landed", "landed", "failed"], log)
        self.assertNotIn("conflictWith", self.state(ids[2]))
        self.assertNotIn(f"{ids[1]} fails alone", log)

    def test_a_newer_half_whose_admission_wait_expires_goes_back_behind_the_older_half(self):
        """If no other Mac admits the newer half within its wait, the bisect still falls back to serial gates."""
        for name in ("a", "bad", "c", "d"): self.commit(name, name)
        ids = self.add("a", "bad", "c", "d")
        real = mq.Beside.gate
        def declined(worker, lanes, round_name, where=(), cancel=None):
            worker.ran_where = ""
            return mq.resources.BUSY, None
        with patch.object(mq.Beside, "gate", declined):
            log = self.worked()
        self.assertIs(mq.Beside.gate, real)
        self.assertEqual([self.state(i)["state"] for i in ids], ["landed", "failed", "landed", "landed"])
        self.assertEqual(self.gates(), 5)
        self.assertEqual(self.rounds(log), 5, log)
        self.assertIn(f"newer half: no verdict (exit {mq.resources.BUSY}): queued again, behind the older half", log)

    def test_the_newer_half_waits_for_admission_off_the_older_halfs_mac(self):
        queue = mq.Queue(self.repo)
        worker = mq.Worker(queue)
        for mac, where, excluded, result in [
            (mq.HERE_MAC, ("--offload-wait",), mq.remote.HERE, 1),
            (mq.lane_wait.farm_mac("build-host"), (), "build-host", 0),
            (mq.lane_wait.farm_mac("build-host"), (), "build-host", mq.lane_results.UNMEASURED),
        ]:
            with self.subTest(mac=mac, result=result), farm_fixture.only("build-host", "gate-03"), \
                    patch.object(mq, "apart", APART):
                newer = mq.Beside(worker)
                newer.tip = "newer-tip"
                waiting, older_returning = threading.Event(), threading.Event()
                marks = []

                def older_gate(lanes, round_name):
                    self.assertTrue(waiting.wait(stretched(10)))
                    older_returning.set()
                    return result, None

                def newer_gate(lanes, round_name, *, where):
                    self.assertEqual(newer.environment[mq.remote.QUEUE_GATE_EXCLUDE], excluded)
                    mark = Path(newer.environment[mq.remote.QUEUE_GATE_RELEASE])
                    self.assertFalse(mark.exists())
                    marks.append(mark)
                    waiting.set()
                    self.assertTrue(older_returning.wait(stretched(10)))
                    if result not in mq.NO_VERDICT:
                        until = time.monotonic() + stretched(10)
                        while not mark.exists() and time.monotonic() < until:
                            time.sleep(0.02)
                        self.assertTrue(mark.exists())
                    else:
                        self.assertFalse(mark.exists())
                    return 0, None

                with patch.object(worker, "admitted_mac", return_value=mac), \
                        patch.object(worker, "gate", side_effect=older_gate), \
                        patch.object(newer, "gate", side_effect=newer_gate) as gate:
                    self.assertEqual(newer.both(worker, ["gate"], "older"), (result, None))
                gate.assert_called_once_with(["gate"], newer.round_name, where=where)
                self.assertEqual(newer.result, (0, None))
                self.assertEqual(len(marks), 1)
                self.assertFalse(marks[0].parent.exists())
                self.assertNotIn(mq.remote.QUEUE_GATE_RELEASE, newer.environment)

    def test_a_lane_that_runs_only_here_keeps_one_half_at_a_time(self):
        """Negative control: a lane that runs only on this Mac (offerable, whose own cases the test below covers)
        sends no newer half beside the older, and the bisect is as before: 5 gates in 5 rounds, and one more of bad on
        the main before a landed (vocaro/queuekeeper#263)."""
        self.commit("a", "a"); self.commit("bad", "bad"); self.commit("c", "c"); self.commit("d", "d")
        ids = self.add("a", "bad", "c", "d")
        why = "gate runs only on this Mac: the lane says: the fixture's reason"
        with patch.object(mq, "offerable", lambda config, lanes: why):
            log = self.worked()
        self.assertEqual([self.state(i)["state"] for i in ids], ["landed", "failed", "landed", "landed"])
        self.assertEqual(self.gates(), 6)
        self.assertEqual(self.rounds(log), 5, log)
        self.assertIn(f"the newer 2 of the bisect wait for the older half, since {why}", log)
        self.assertIn(f"the newer 1 of the bisect wait for the older half, since {why}", log)
        self.assertNotIn("newer half", log)

    def test_the_second_gate_of_a_pair_goes_to_any_mac_but_the_first_gates(self):
        """vocaro/queuekeeper#211: the first gate of a pair, a bisect's older half or a batch, is offered as any gate
        is, and the second is kept off whichever Mac admitted it, this Mac a candidate like every other. Until then
        the older half ran with --here and the newer ran remotely, and a stacked gate ran here whenever the
        farm had one other Mac and the batch gated there."""
        self.assertFalse(hasattr(mq, "HERE") or hasattr(mq, "BESIDE"))
        near, far = mq.lane_wait.farm_mac("build-host"), mq.lane_wait.farm_mac("gate-03")
        for hosts in ((), ("build-host",), ("build-host", "gate-03")):
            # The first gate is on this Mac. Its companion waits elsewhere or makes an immediate offer.
            self.assertEqual(APART(mq.HERE_MAC, hosts, True),
                             (("--offload-wait",), None, "on another Mac, waiting there for one to admit them"))
            self.assertEqual(APART(mq.HERE_MAC, hosts, False),
                             (("--offload-only",), None, "on another Mac if it can start them now"))
        for hosts in (("build-host",), ("build-host", "gate-03")):
            # The first gate is on another Mac. Its companion can use any Mac but that one.
            self.assertEqual(APART(near, hosts, True),
                             ((), "build-host", f"on a Mac other than {near}, which gates the batch below them"))
            self.assertEqual(APART(near, hosts, False),
                             (("--if-free",), "build-host", f"on a Mac other than {near} if one can start them now"))
        self.assertEqual(APART(far, ("build-host", "gate-03"), False)[:2], (("--if-free",), "gate-03"))
        # A Mac the farm does not list is none to keep away from, so nothing is placed by it.
        self.assertIsNone(APART(far, ("build-host",), True))
        self.assertIsNone(APART("somewhere", (), False))
        self.assertEqual(OFFERABLE({"lanes": {"gate": {"command": ["x"], "resource": "heavy"}}}, ["gate"]), None)
        # A light gate, such as the toolkit's was, or one with no resource, is admitted and offered as every lane is
        # (the owner's decision, 2026-11-07), so its halves run one on each Mac; a self lane stays here.
        for resource in ({"resource": "light"}, {}):
            self.assertEqual(OFFERABLE({"lanes": {"gate": {"command": ["x"], **resource}}}, ["gate"]), None)
        self.assertEqual(OFFERABLE({"lanes": {"gate": {"command": ["x"], "resource": "self"}}}, ["gate"]),
                         "gate runs only on this Mac: a self lane runs where it starts, and its script admits each of "
                         "its parts")
        self.assertEqual(OFFERABLE({"lanes": {"gate": {"command": ["x"], "resource": "heavy", "here": "it builds the "
                                                      "app"}}}, ["gate"]), "gate runs only on this Mac: the lane says: "
                                                                              "it builds the app")
        self.assertEqual(OFFERABLE({"lanes": {}}, ["gate"]), "gate runs only on this Mac: it is no configured lane")

    def test_a_newer_entry_that_needs_an_older_one_waits_rather_than_come_back(self):
        """A newer entry that conflicts with origin/main on its own may need an older entry of the bisect: it is
        not sent back, as a conflict in a round would be, but waits for the older half."""
        worker = mq.Worker(mq.Queue(self.repo))
        beside = mq.Beside(worker)
        entry = {"id": "x"}
        with patch.object(mq.Queue, "settle") as settle:
            beside.left_out(entry, mq.CONFLICT, why="conflicts")
            settle.assert_not_called()
            beside.left_out(entry, mq.LANDED, why="held")
            settle.assert_called_once()
        self.assertEqual(beside.unreachable, [entry])
        self.assertEqual(beside.checkout.name, f"{self.repo.name}{mq.BESIDE_SUFFIX}")


class Fewest(unittest.TestCase):
    """mq.fewest, the greedy search for the fewest entries that still fail together (Worker.together)."""
    @staticmethod
    def search(names, failing, unknown=()):
        """fewest over names, a subset failing when it holds every name of one of failing, a set of sets; a subset
        named in unknown has no verdict (None). Returns the result and the subsets probed, each once."""
        probed = []
        def fails(subset):
            key = frozenset(entry["id"] for entry in subset)
            probed.append(key)
            if key in unknown: return False
            return any(group <= key for group in failing)
        entries = [{"id": name} for name in names]
        return [entry["id"] for entry in mq.fewest(entries, fails)], probed

    def test_a_pair_needs_only_the_halves_known(self):
        found, probed = self.search("ab", [{"a", "b"}])
        self.assertEqual(found, ["a", "b"])
        self.assertEqual(probed, [frozenset("b"), frozenset("a")])

    def test_the_pair_inside_a_larger_set_is_found_and_each_left_is_needed(self):
        for failing in ({"b", "g"}, {"a", "h"}, {"d", "e"}, {"c", "e", "f"}):
            with self.subTest(failing=failing):
                found, _ = self.search("abcdefgh", [failing])
                self.assertEqual(set(found), failing)
                self.assertEqual(found, sorted(found))

    def test_of_two_groups_that_fail_one_is_kept_whole(self):
        found, _ = self.search("abcdef", [{"a", "d"}, {"c", "f"}])
        self.assertIn(set(found), ({"a", "d"}, {"c", "f"}))

    def test_an_entry_whose_removal_cannot_be_probed_is_kept(self):
        found, _ = self.search("abc", [{"a", "c"}], unknown={frozenset("ac")})
        self.assertEqual(found, ["a", "b", "c"])


class FailsTogetherWords(unittest.TestCase):
    def test_one_partner_and_several(self):
        one = mq.together_words([{"id": "p", "tip": "1" * 40}])
        self.assertIn(f"together with p at {'1' * 40}, though each passes alone", one)
        self.assertIn("that entry came back too. Rebase onto its commit", one)
        several = mq.together_words([{"id": "p", "tip": "1" * 40}, {"id": "q", "tip": "2" * 40}])
        self.assertIn(f"together with p at {'1' * 40}, q at {'2' * 40}", several)
        self.assertIn("those entries came back too. Rebase onto their commits", several)


class PooledStack(Fixture):
    def setUp(self):
        super().setUp()
        self.roomy.stop()  # these read room as a stack does, from the readings each test gives

    def test_a_stack_tries_build_macs_other_than_the_one_running_the_batch(self):
        worker = mq.Worker(mq.Queue(self.repo))
        worker.lane_now = ("gate", time.time(), ())
        worker.environment = {}
        waiting = [{"id": "new", "state": mq.QUEUED}]
        config = {"lanes": {"gate": {"resource": "heavy"}}}
        for mac, where, excluded in [
            (mq.lane_wait.HERE, ("--offload-wait",), None),
            # Every Mac but the batch's, this Mac among them, waiting for one as any gate does.
            (mq.lane_wait.farm_mac("gate-03"), (), "gate-03"),
        ]:
            record = {"mac": mac, "kind": None, "pid": os.getpid(), "admitted": False}
            with self.subTest(mac=mac), farm_fixture.only("build-host", "gate-03", "gate-04"), patch.object(worker.queue, "waiting", return_value=waiting), \
                    patch.object(mq.lane_wait, "current", return_value=record), \
                    patch.object(mq.remote, "capacity_there", return_value=ROOMY), \
                    patch.object(mq, "Stack") as stack:
                worker.stack(worker, config, ["gate"], "head", [], threading.Event(), set())
                stack.assert_not_called()
                record["admitted"] = True
                worker.stack(worker, config, ["gate"], "head", [], threading.Event(), set())
                runner = stack.call_args.args[1]
                self.assertEqual(stack.call_args.args[5], where)
                self.assertEqual(runner.environment.get(mq.remote.QUEUE_GATE_EXCLUDE), excluded)
            worker.pending = None

    def test_an_older_entry_blocks_adoption_of_a_disjoint_stack(self):
        older = {"id": "older", "state": mq.QUEUED}
        newer = {"id": "newer", "state": mq.TESTING}
        worker = mq.Worker(mq.Queue(self.repo))
        stack = mq.Stack(worker, Mock(), [newer], "base", ["gate"], (), parallel_config={"lanes": {}})
        self.assertEqual(stack.stale([older, newer]), "an entry ahead of the disjoint stack still waits")
        self.assertIsNone(stack.stale([newer, older]))

    def test_an_isolation_stack_uses_main_and_leaves_every_suspect_to_the_bisect(self):
        worker = mq.Worker(mq.Queue(self.repo))
        worker.suspects = 2
        worker.lane_now = ("gate", time.time(), ())
        waiting = [{"id": "older", "state": mq.TESTING}, {"id": "newer", "state": mq.QUEUED},
                   {"id": "fresh", "state": mq.QUEUED}]
        config = {"lanes": {"gate": {"resource": "heavy"}}}
        for mac, where, excluded in [
            (mq.lane_wait.HERE, ("--offload-only",), None),
            (mq.lane_wait.farm_mac("build-host"), ("--if-free",), "build-host"),
        ]:
            record = {"mac": mac, "kind": None, "pid": os.getpid(), "admitted": True}
            with self.subTest(mac=mac), farm_fixture.only("build-host"), \
                    patch.object(worker.queue, "waiting", return_value=waiting), \
                    patch.object(mq.lane_wait, "current", return_value=record), \
                    patch.object(mq.remote, "capacity_there", return_value=ROOMY), \
                    patch.object(mq.offload_host, "capacity", return_value=ROOMY), patch.object(mq, "Stack") as stack:
                worker.stack(worker, config, ["gate"], "base", waiting[:1], threading.Event(), set(), isolating=True)
                stack.assert_called_once()
                _, runner, entries, base, _lanes, options = stack.call_args.args
                self.assertEqual((entries, base, options), (waiting[2:], "base", where))
                self.assertEqual(runner.checkout, worker.queue.home / f"{self.repo.name}{mq.ISOLATION_STACK_SUFFIX}")
                self.assertEqual(stack.call_args.kwargs["below"], ())
                self.assertTrue(stack.call_args.kwargs["isolating"])
                self.assertEqual(runner.environment.get(mq.remote.QUEUE_GATE_EXCLUDE), excluded)
            worker.pending = None

    def test_a_finished_isolation_stack_can_only_be_adopted_on_the_same_base_with_nothing_older(self):
        worker = mq.Worker(mq.Queue(self.repo))
        base = mq.text(self.repo, "rev-parse", mq.MAIN)
        fresh = {"id": "new", "state": mq.QUEUED}
        for limit, waiting, stacked_base, kept in [
            (1, [fresh], base, False),
            (None, [{"id": "older", "state": mq.QUEUED}, fresh], base, False),
            (None, [fresh], "old-base", False),
            (None, [{**fresh, "state": mq.TESTING}], base, True),
        ]:
            with self.subTest(limit=limit, waiting=waiting, stacked_base=stacked_base), \
                    patch.object(worker.queue, "waiting", return_value=waiting):
                stack = Mock(entries=[fresh], base=stacked_base)
                stack.void.return_value = stack.stale.return_value = None
                worker.limit, worker.pending, worker.stacked = limit, stack, None
                worker.after_isolation_stack()
                self.assertIsNone(worker.pending)
                self.assertIs(worker.stacked, stack if kept else None)
                self.assertEqual(stack.drop.call_count, 0 if kept else 1)

    def test_a_kept_isolation_stack_is_discarded_if_main_moves_before_the_next_round(self):
        worker = mq.Worker(mq.Queue(self.repo))
        base = mq.text(self.repo, "rev-parse", mq.MAIN)
        fresh = {"id": "new", "state": mq.TESTING}
        stack = Mock(entries=[fresh], paths={"code"}, base=base, isolating=True)
        stack.stale.return_value = None
        worker.stacked = stack
        moved = self.commit("README.md", "move-main", "# Updated\n")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        with patch.object(worker.queue, "waiting", return_value=[fresh]):
            self.assertTrue(worker.round())
        self.assertIsNone(worker.stacked)
        stack.drop.assert_called_once_with(f"origin/main is {moved[:12]}, not the tip {base[:12]} it was gated on")
        stack.wait.assert_not_called()

    def test_the_next_isolation_check_does_not_wait_for_the_stacks_verdict(self):
        worker = mq.Worker(mq.Queue(self.repo))
        stack, started = Mock(), threading.Event()
        stack.done.return_value = False

        def start(*args, **kwargs):
            worker.pending = stack
            started.set()

        def gate():
            self.assertTrue(started.wait(stretched(5)))
            return 1, None

        with patch.object(worker, "stack", side_effect=start), patch.object(worker, "docs_beside"), \
                patch.object(mq, "STACK_POLL", 0.01):
            self.assertEqual(worker.watched(worker, {}, ["gate"], "main", "bisect-tip", [], gate, isolating=True),
                             (1, None))
        self.assertIsNone(worker.pending)
        stack.drop.assert_called_once_with("the bisect's gate ended and its next checks take precedence")

    def stacked(self, mac, busy, said=None, clock=100):
        """(Stack's mock, the worker's log, its stack_retry_after) of one look at a stack, the batch admitted on mac,
        the farm build-host, gate-03 and gate-04, and busy the farm Macs and this Mac (None) that have no room."""
        worker = mq.Worker(mq.Queue(self.repo))
        worker.lane_now = ("gate", time.time(), ())
        worker.environment = {}
        record = {"mac": mac, "kind": None, "pid": os.getpid(), "admitted": True}
        read = []
        def there(host):
            read.append(host)
            if host == "gate-04" and "gate-04" in busy: raise mq.remote.Unavailable("gate-04 did not answer")
            return CROWDED if host in busy else ROOMY
        with farm_fixture.only("build-host", "gate-03", "gate-04"), \
                patch.object(worker.queue, "waiting", return_value=[{"id": "new", "state": mq.QUEUED}]), \
                patch.object(mq.lane_wait, "current", return_value=record), \
                patch.object(mq.remote, "capacity_there", side_effect=there), \
                patch.object(mq.offload_host, "capacity", return_value=CROWDED if None in busy else ROOMY), \
                patch.object(mq.time, "monotonic", return_value=clock), \
                patch.object(mq, "Stack") as stack, contextlib.redirect_stdout(io.StringIO()) as log:
            worker.stack(worker, {"lanes": {"gate": {"resource": "heavy"}}}, ["gate"], "head", [], threading.Event(),
                         set() if said is None else said)
        return stack, log.getvalue(), worker.stack_retry_after, read

    def test_a_stack_waits_without_integrating_while_every_mac_it_may_go_to_is_busy(self):
        """Most stacked gates used to integrate, wait and be declined (exit 75) while the farm was busy: none starts
        now unless a Mac it may go to has room, and the next look waits STACK_ADMISSION_BACKOFF, as after a decline."""
        said = set()
        stack, log, after, read = self.stacked(mq.lane_wait.farm_mac("gate-03"), {"build-host", "gate-04", None}, said)
        stack.assert_not_called()
        self.assertEqual(after, 100 + mq.STACK_ADMISSION_BACKOFF)
        # The batch's Mac is never read; this Mac is, as the stack may go there.
        self.assertEqual(read, ["build-host", "gate-04"])
        self.assertIn("the entries behind the batch wait, since no Mac that may take them has spare capacity now", log)
        # Admitted work runs on each, over its CPU limit, which the old test of the CPU alone could miss.
        self.assertIn("build-host is busy (CPU 98% busy, at or above its HEAVY_BUSY_LIMIT=90%) and admitted work runs "
                      "there, so it takes no more", log)
        self.assertIn("gate-04's capacity cannot be read: gate-04 did not answer", log)
        self.assertIn("this Mac is busy (CPU 98% busy", log)
        # Said once while it lasts.
        _, again, _, _ = self.stacked(mq.lane_wait.farm_mac("gate-03"), {"build-host", "gate-04", None}, said)
        self.assertEqual(again, "")

    def test_a_stack_kept_off_this_mac_ignores_this_macs_room(self):
        stack, _, after, read = self.stacked(mq.lane_wait.HERE, {"build-host", "gate-03", "gate-04"})
        stack.assert_not_called()
        self.assertEqual(read, ["build-host", "gate-03", "gate-04"])
        self.assertEqual(after, 100 + mq.STACK_ADMISSION_BACKOFF)

    def test_a_stack_starts_when_one_mac_it_may_go_to_has_room(self):
        """Negative control: one Mac with room, here this Mac or gate-04, is enough."""
        for busy in ({"build-host", "gate-04"}, {"build-host", None}):
            with self.subTest(busy=busy):
                said = {mq.STACK_ROOMLESS}
                stack, _, after, _ = self.stacked(mq.lane_wait.farm_mac("gate-03"), busy, said)
                stack.assert_called_once()
                stack.return_value.begin.assert_called_once()
                self.assertEqual(after, 0)
                self.assertNotIn(mq.STACK_ROOMLESS, said)


class Stacked(Fixture):
    """Entries that arrive while a batch gates gate stacked on its tip, on another Mac, and land once it has,
    without a gate of their own; a batch that does not land throws that gate away (vocaro-engineering#241). Both
    run on this Mac here, as in BothHalves."""
    PATIENCE = 600  # seconds a step may take before the test gives up on it, which only a regression uses
    EVERY_RUN = range(1, 6)  # more gates than any of these rounds starts

    def setUp(self):
        super().setUp()
        for name, value in (("apart", BOTH_HERE), ("BESIDE_POLL", 0.02), ("offerable", lambda config, lanes: None),
                            ("STACK_POLL", 0.1)):
            patcher = patch.object(mq, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        starting = patch.object(mq.Queue, "start")
        starting.start()
        self.addCleanup(starting.stop)

    def release(self, runs):
        """The lanes of these gates, counted from 1 in the order they started, may end (hold, in LANE)."""
        for number in runs: Path(f"{self.counter}.release.{number}").touch()

    def worked(self, branch, then, *more, checked=True, ends=EVERY_RUN, after=1):
        """The log of a worker whose gate numbered after holds until branch has arrived, as a session's qk land adds
        it, unchecked with no trial integration, as when the batch ahead changes after it (Fixture.add), and then(log)
        is true: only then may the gates numbered in ends finish. No wall-clock wait decides what the round sees, so
        a stalled host only makes it slower (vocaro-engineering#250); PATIENCE ends what a regression would hold."""
        failures, finished = [], threading.Event()
        def until(happened, what):
            deadline = time.monotonic() + stretched(self.PATIENCE)
            while not happened():
                if finished.is_set(): raise AssertionError(f"the round ended before {what}")
                if time.monotonic() > deadline: raise AssertionError(f"no {what} within {self.PATIENCE}s, stretched")
                time.sleep(0.02)
        def drive():
            try:
                until(lambda: self.gates() >= after, f"gate {after}'s lane started")
                with patch.object(mq, "check") if not checked else contextlib.nullcontext():
                    mq.add(self.repo, branch)
                until(lambda: then(said.getvalue()), f"sign that the worker had looked at {branch}")
                self.release(ends)
                until(finished.is_set, "end of the round")
            except Exception as failure: failures.append(failure)
            finally: self.release(self.EVERY_RUN)  # so that a failing round ends too, with its log
        with contextlib.redirect_stdout(io.StringIO()) as said, contextlib.ExitStack() as stack:
            for context in more: stack.enter_context(context)
            driver = threading.Thread(target=drive, name="driver", daemon=True)
            driver.start()
            try: status = self.work()
            finally:
                finished.set()
                driver.join()
        if failures: raise failures[0]
        self.assertEqual(status, 0, said.getvalue())
        return said.getvalue()

    def stacked_started(self, log):
        """The stacked gate's lane runs: the batch's and its own have started."""
        return self.gates() >= 2

    def added(self, branch):
        return next(entry for entry in mq.Queue(self.repo).all() if entry["branch"] == branch)

    def test_stack_waits_for_the_final_lanes_admission_before_choosing_the_other_mac(self):
        self.commit("a", "a"); self.commit("b", "b")
        first, second = self.add("a", "b")
        worker = mq.Worker(mq.Queue(self.repo))
        lanes = ["check", "gate"]
        args = (worker, self.config(), lanes, self.state(first)["tip"], [self.state(first)], threading.Event(), set())
        waits = mq.lane_wait
        check = waits.path_of(worker.checkout, "check")
        gate = waits.path_of(worker.checkout, "gate")
        with (patch.object(mq, "apart", APART), farm_fixture.only("build-host"),
              patch.object(mq, "Stack") as stack,
              patch.dict(os.environ, {waits.VARIABLE: str(check)})):
            worker.stack(*args)
            stack.assert_not_called()
            worker.lane_now = ("check", time.time(), ())
            waits.running()
            worker.stack(*args)
            stack.assert_not_called()

            worker.lane_now = ("gate", time.time(), ())
            os.environ[waits.VARIABLE] = str(gate)
            waits.sent("build-host")
            worker.stack(*args)
            stack.assert_not_called()
            waits.relayed(b'{"kind":"admission","reason":"busy"}', "build-host")
            worker.stack(*args)
            stack.assert_not_called()
            waits.relayed(b"null", "build-host")
            worker.stack(*args)
            stack.assert_not_called()
            waits.relayed(b'{"kind":null,"admitted":true}', "build-host")
            worker.stack(*args)
            stack.assert_called_once()
            # The batch gates on the farm's one other Mac: the stacked gate is offered to every Mac but that one,
            # which leaves this Mac, where it waits as any gate does. Until vocaro/queuekeeper#211 it ran with --here.
            self.assertEqual(stack.call_args.args[-1], ())
            self.assertEqual(stack.call_args.args[1].environment.get(mq.remote.QUEUE_GATE_EXCLUDE), "build-host")
            self.assertEqual(stack.call_args.kwargs["said_where"],
                             f"on a Mac other than {waits.farm_mac('build-host')}, which gates the batch below them")
            self.assertEqual([entry["id"] for entry in stack.call_args.args[2]], [second])

            worker.pending = None
            stack.reset_mock()
            waits.running()
            worker.stack(*args)
            stack.assert_called_once()
            self.assertEqual(stack.call_args.args[-1], ("--offload-wait",))
            self.assertIsNone(stack.call_args.args[1].environment.get(mq.remote.QUEUE_GATE_EXCLUDE))

    def test_an_entry_that_arrives_during_a_gate_lands_on_its_gate_stacked_on_the_batch(self):
        self.commit("hold", "a"); self.commit("b", "b")
        [first] = self.add("a")
        log = self.worked("b", self.stacked_started)
        b = self.added("b")
        self.assertEqual((self.state(first)["state"], b["state"]), ("landed", "landed"), log)
        self.assertEqual(self.gates(), 2, log)
        self.assertIn("that arrived during the gate", log)
        self.assertIn("goes on as the next round, as that is origin/main now", log)
        self.assertIn("whose gate began on top of the batch that just landed", log)
        # b went on top of a, and its gate was of exactly that commit.
        self.assertEqual(run(self.repo, "git", "rev-parse", f"{b['landedAs']}^"), self.state(first)["landedAs"])
        self.assertEqual(b["gatedOn"], self.state(first)["landedAs"])
        self.assertLessEqual({"hold", "b"}, self.main_files())

    def test_a_stack_beside_a_serial_bisect_can_land_after_the_last_suspect_is_sent_back(self):
        self.commit("bad-a", "a")
        (self.repo / "hold").touch()
        run(self.repo, "git", "add", "hold")
        run(self.repo, "git", "commit", "-qm", "Hold the suspect gate until the stack finishes")
        self.commit("b", "b")
        [first] = self.add("a")
        base = mq.text(self.repo, "rev-parse", mq.MAIN)
        worker = mq.Worker(mq.Queue(self.repo))
        worker.limit = worker.suspects = 1

        def work():
            self.assertTrue(worker.round())
            self.assertIsNotNone(worker.stacked, "the stack should be ready after the last suspect fails")
            self.assertTrue(worker.round())
            return 0

        with patch.object(self, "work", side_effect=work):
            log = self.worked("b", lambda _log: worker.pending is not None and worker.pending.done())
        b = self.added("b")
        self.assertEqual((self.state(first)["state"], b["state"]), (mq.FAILED, mq.LANDED), log)
        self.assertEqual((self.gates(), b["gatedOn"]), (2, base), log)
        self.assertIn("whose gate began while the prior batch was being isolated", log)
        self.assertIn("b", self.main_files())
        self.assertNotIn("hold", self.main_files())

    def test_a_stack_beside_a_joint_failure_probe_can_land_after_both_suspects_are_sent_back(self):
        self.commit("left", "left")
        (self.repo / "hold").touch()
        run(self.repo, "git", "add", "hold")
        run(self.repo, "git", "commit", "-qm", "Hold the joint probe until the stack finishes")
        self.commit("right", "right")
        self.commit("later", "later")
        suspects = self.add("left", "right")
        base = mq.text(self.repo, "rev-parse", mq.MAIN)
        worker = mq.Worker(mq.Queue(self.repo))
        worker.limit, worker.suspects = 1, 2
        self.release((1, 2))  # only the probe, after both halves pass, waits for the stack

        def work():
            self.assertTrue(worker.round())
            self.assertIsNotNone(worker.stacked, "the stack should be ready after the joint search")
            self.assertTrue(worker.round())
            return 0

        with patch.object(self, "work", side_effect=work):
            log = self.worked("later", lambda _log: worker.pending is not None and worker.pending.done(), after=3)
        later = self.added("later")
        self.assertEqual([self.state(entry)["state"] for entry in suspects], [mq.FAILED, mq.FAILED], log)
        self.assertEqual((later["state"], later["gatedOn"], self.gates()), (mq.LANDED, base, 4), log)
        self.assertIn("whose gate began while the prior batch was being isolated", log)
        self.assertIn("later", self.main_files())
        self.assertTrue({"left", "right", "hold"}.isdisjoint(self.main_files()))

    def test_a_stacked_gate_lands_what_arrived_before_the_queue_closed_and_a_later_entry_is_refused(self):
        """vocaro/queuekeeper#32: closing the queue while a stacked gate runs leaves that gate and its entry as they
        were, and refuses what arrives after."""
        self.commit("hold", "a"); self.commit("b", "b"); self.commit("c", "c")
        [first] = self.add("a")
        queue, refused = mq.Queue(self.repo), []
        def closed_once_stacked(log):
            if not self.stacked_started(log): return False
            if queue.close("a host goes away", "the maintenance session") is None:
                with self.assertRaises(mq.Refused) as caught, contextlib.redirect_stderr(io.StringIO()):
                    mq.add(self.repo, "c")
                refused.append(str(caught.exception))
            return True
        log = self.worked("b", closed_once_stacked)
        b = self.added("b")
        self.assertEqual((self.state(first)["state"], b["state"]), ("landed", "landed"), log)
        self.assertEqual(self.gates(), 2, log)
        self.assertEqual(b["gatedOn"], self.state(first)["landedAs"])
        self.assertEqual(len(refused), 1, refused)
        self.assertIn("closed to new entries by the maintenance session", refused[0])
        self.assertEqual([entry["branch"] for entry in queue.all()], ["a", "b"])
        self.assertTrue(queue.closed())

    def test_a_docs_entry_that_arrives_during_a_code_gate_lands_beside_it_on_the_docs_lanes(self):
        """vocaro-engineering#394: a docs-only entry that arrives while a code batch gates lands beside the gate, on the
        docs lanes, and the batch's pass carries over the document main gained. The old worker stacked it on the
        batch's tip and gated it with every lane."""
        self.commit("hold", "a"); self.commit("doc/guide.md", "guide", "# Guide\n")
        [first] = self.add("a")
        # The old worker's sign, a stacked gate starting, ends the wait too, so that it fails rather than waits.
        log = self.worked("guide", lambda log: "docs beside the gate: pushed" in log or self.stacked_started(log))
        guide = self.added("guide")
        self.assertEqual((self.state(first)["state"], guide["state"]), ("landed", "landed"), log)
        self.assertEqual(self.gates(), 1, log)
        self.assertEqual(guide["docsOnly"], ["docs"], log)
        self.assertNotIn("gatedOn", guide)
        self.assertNotIn("that arrived during the gate", log)
        self.assertIn("carried the result over 1 file(s) on main: doc/guide.md", log)
        # The document landed first, and the batch on top of it.
        self.assertEqual(run(self.repo, "git", "rev-parse", f"{self.state(first)['landedAs']}^"), guide["landedAs"])
        self.assertLessEqual({"hold", "doc"}, self.main_files())

    def test_a_docs_entry_lands_beside_the_gate_of_an_entry_put_first(self):
        """An entry qk land --front put on its own stacks nothing behind it, but a docs entry that arrives during its
        gate still lands beside it: the old worker gated such an entry with no watch, so the docs entry waited it out."""
        self.commit("hold", "a"); self.commit("doc/guide.md", "guide", "# Guide\n")
        with patch.object(mq.Queue, "start"), patch("builtins.print"): mq.add(self.repo, "a", front=True)
        first = self.added("a")["id"]
        # The old worker gives no sign while the gate holds: a bound, far beyond what the landing beside it takes, ends
        # the wait, so that it fails rather than waits.
        deadline = []
        def then(log):
            deadline[:] = deadline or [time.monotonic() + stretched(120)]
            return "docs beside the gate: pushed" in log or time.monotonic() > deadline[0]
        log = self.worked("guide", then)
        guide = self.added("guide")
        self.assertEqual((self.state(first)["state"], guide["state"]), ("landed", "landed"), log)
        self.assertEqual(guide["docsOnly"], ["docs"], log)
        self.assertEqual(self.gates(), 1, log)
        self.assertNotIn("that arrived during the gate", log)
        self.assertIn("carried the result over 1 file(s) on main: doc/guide.md", log)

    def test_a_stacked_gate_goes_on_over_a_document_landed_beside_the_gate_below_it(self):
        """A code entry stacked on the batch, and then a docs entry landed beside the gate: the batch carries its
        result over the document, and so does the stacked gate, which lands with no gate of its own."""
        self.commit("hold", "a"); self.commit("b", "b"); self.commit("doc/guide.md", "guide", "# Guide\n")
        [first] = self.add("a")
        added = []
        def then(log):
            if not self.stacked_started(log): return False
            if not added:
                with patch.object(mq, "check"): mq.add(self.repo, "guide")
                added.append(True)
            return "docs beside the gate: pushed" in log
        log = self.worked("b", then)
        b, guide = self.added("b"), self.added("guide")
        self.assertEqual([self.state(first)["state"], b["state"], guide["state"]], ["landed"] * 3, log)
        self.assertEqual(self.gates(), 2, log)
        self.assertEqual(guide["docsOnly"], ["docs"], log)
        self.assertIn("as origin/main is that tip with documents its result carries over", log)
        self.assertEqual(log.count("carried the result over 1 file(s) on main: doc/guide.md"), 2, log)
        self.assertLessEqual({"hold", "b", "doc"}, self.main_files())

    def test_a_batch_that_fails_throws_away_the_gate_stacked_on_it(self):
        """The batch below fails: its stacked gate is canceled, and the entry gates again on main and lands."""
        self.commit("bad", "a", "x"); self.commit("hold", "a", start="a"); self.commit("b", "b")
        [first] = self.add("a")
        log = self.worked("b", self.stacked_started, ends=(1,))  # the stacked gate's lane ends only by its cancel
        self.assertEqual((self.state(first)["state"], self.added("b")["state"]), ("failed", "landed"), log)
        self.assertIn("is thrown away, since the batch below it did not land at the tip it was stacked on: queued "
                      "again", log)
        # Canceled, rather than run out on top of a's: its lane counts once it has started. It is recorded as
        # canceled, in b's logs and the journal, not as an unmeasured gate of an entry that then landed
        # (vocaro/queuekeeper#302).
        self.assertRegex(log, r"stacked: gate: canceled[^\n]*: Owned command was canceled")
        self.assertNotIn("unmeasured", log)
        self.assertEqual([item["status"] for item in self.added("b")["logs"] if item["lane"] == "gate"],
                         [mq.CANCELED, 0], log)
        ended = [event["status"] for event in mq.events.read(mq.Queue(self.repo).directory, kinds=["gate.ended"])]
        self.assertIn(mq.CANCELED, ended)
        self.assertNotIn(mq.lane_results.UNMEASURED, ended)
        self.assertEqual(self.gates(), 3, log)
        self.assertNotIn("hold", self.main_files())

    def test_a_lane_that_runs_only_here_stacks_nothing(self):
        """Negative control: with a lane that runs only on this Mac, the entry waits for the batch and gates on its
        own, as before."""
        self.commit("hold", "a"); self.commit("b", "b")
        [first] = self.add("a")
        why = "gate runs only on this Mac: the lane says: the fixture's reason"
        log = self.worked("b", lambda log: f"since {why}" in log,
                          patch.object(mq, "offerable", lambda config, lanes: why))
        self.assertEqual((self.state(first)["state"], self.added("b")["state"]), ("landed", "landed"), log)
        self.assertEqual(self.gates(), 2, log)
        self.assertEqual(log.count(f"the entries behind the batch wait for it, since {why}"), 1, log)
        self.assertNotIn("that arrived during the gate", log)

    def test_a_stacked_gate_the_farm_mac_declines_is_thrown_away_and_the_entry_gates_after(self):
        """Negative control: another Mac that cannot start it gives no verdict, which is thrown away, so the entry
        gates after the batch, as before."""
        self.commit("hold", "a"); self.commit("b", "b")
        [first] = self.add("a")
        declined_log = self.base / "declined-gate.log"
        def declined(worker, lanes, round_name, where=(), cancel=None):
            declined_log.write_text("nothing ran\n")
            return mq.resources.BUSY, declined_log
        log = self.worked("b", lambda log: "since it gave no verdict" in log,
                          patch.object(mq.Beside, "gate", declined),
                          patch.object(mq, "STACK_ADMISSION_BACKOFF", stretched(self.PATIENCE)))
        self.assertEqual((self.state(first)["state"], self.added("b")["state"]), ("landed", "landed"), log)
        self.assertEqual(self.gates(), 2, log)
        self.assertIn(f"is thrown away, since it gave no verdict (exit {mq.resources.BUSY}): queued again", log)
        self.assertNotIn("goes on as the next round", log)
        self.assertEqual(log.count("that arrived during the gate"), 1, log)
        self.assertIn(mq.STACK_ADMISSION_DECLINED, declined_log.read_text())

    def test_a_stacked_gate_is_offered_again_only_after_its_admission_backoff(self):
        self.commit("a", "a"); self.commit("b", "b")
        first, second = self.add("a", "b")
        worker = mq.Worker(mq.Queue(self.repo))
        dropped = []
        worker.pending = SimpleNamespace(done=lambda: True, void=lambda: "busy", not_admitted=lambda: True,
                                         drop=dropped.append)
        config, lanes, head = self.config(), ["gate"], self.state(first)["tip"]
        worker.lane_now = ("gate", time.time(), ())
        path = mq.lane_wait.path_of(worker.checkout, "gate")
        with patch.dict(os.environ, {mq.lane_wait.VARIABLE: str(path)}):
            mq.lane_wait.running()
        arguments = (worker, config, lanes, head, [self.state(first)], threading.Event(), set())
        with patch.object(mq.time, "monotonic", return_value=100) as clock, patch.object(mq, "Stack") as stack:
            worker.stack(*arguments)
            self.assertEqual(dropped, ["busy"])
            stack.assert_not_called()
            clock.return_value += mq.STACK_ADMISSION_BACKOFF - 1
            worker.stack(*arguments)
            stack.assert_not_called()
            clock.return_value += 1
            worker.stack(*arguments)
        stack.assert_called_once()
        self.assertEqual([entry["id"] for entry in stack.call_args.args[2]], [second])
        stack.return_value.begin.assert_called_once()

    def test_a_stacked_gate_admitted_on_retry_gives_the_verdict(self):
        self.commit("hold", "a"); self.commit("b", "b")
        [first] = self.add("a")
        attempts, gate = [], mq.Beside.gate
        def offer(worker, lanes, round_name, where=(), cancel=None):
            attempts.append(round_name)
            if len(attempts) == 1: return mq.resources.BUSY, None
            return gate(worker, lanes, round_name, where=where, cancel=cancel)
        # The preceding fixture checks the wait itself with a monotonic clock.
        log = self.worked("b", self.stacked_started, patch.object(mq, "STACK_ADMISSION_BACKOFF", 0),
                          patch.object(mq.Beside, "gate", offer))
        b = self.added("b")
        self.assertEqual((self.state(first)["state"], b["state"]), ("landed", "landed"), log)
        self.assertEqual(len(attempts), 2, log)
        self.assertEqual(self.gates(), 2, log)
        self.assertIn("whose gate began on top of the batch that just landed", log)
        self.assertEqual(b["gatedOn"], self.state(first)["landedAs"])

    def test_an_adopted_stack_refused_admission_is_gated_normally_without_holding(self):
        config = self.config()
        config["queue"]["noVerdictRuns"] = 1
        self.publish(config)
        self.commit("hold", "a"); self.commit("b", "b")
        [first] = self.add("a")
        offered, adopted = threading.Event(), threading.Event()
        begun = mq.Worker.begun
        def began(worker, *args, **kwargs):
            begun(worker, *args, **kwargs)
            if kwargs.get("adopted"): adopted.set()
        def declined(worker, lanes, round_name, where=(), cancel=None):
            offered.set()
            self.assertTrue(adopted.wait(stretched(self.PATIENCE)), "the batch never adopted the stacked gate")
            return mq.resources.BUSY, None
        log = self.worked("b", lambda _: offered.is_set(), patch.object(mq.Worker, "begun", began),
                          patch.object(mq.Beside, "gate", declined))
        self.assertEqual((self.state(first)["state"], self.added("b")["state"]), ("landed", "landed"), log)
        self.assertIn("whose gate began on top of the batch that just landed", log)
        self.assertEqual(self.gates(), 2, log)
        self.assertIsNone(mq.Queue(self.repo).held(), log)

    def test_an_entry_that_conflicts_with_the_batch_is_not_stacked_or_sent_back_by_it(self):
        """A stacked gate settles nothing: an entry that conflicts with the batch below waits, and its own round
        sends it back."""
        self.commit("same", "a", "a"); self.commit("hold", "a", start="a"); self.commit("same", "c", "c")
        [first] = self.add("a")
        log = self.worked("c", lambda log: "ahead of it, not with origin/main" in log, checked=False)
        c = self.added("c")
        self.assertEqual((self.state(first)["state"], c["state"]), ("landed", "conflict"), log)
        self.assertNotIn("that arrived during the gate", log)
        self.assertEqual(self.gates(), 1, log)
        # Its stacked look names the entry below it, which it conflicts with, not origin/main (#247).
        self.assertIn(f"stacked: {c['id']}: ", log)
        self.assertIn(f"conflicts with {first}, ahead of it, not with origin/main, in same: once it lands", log)
        self.assertNotIn(f"stacked: {c['id']}: {c['tip'][:12]} conflicts with origin/main", log)


class RemovedWhileTesting(Fixture):
    """`qk queue remove` of an entry whose gate runs stops that gate, by the cancel a thrown-away stacked gate's lanes
    end with, and what is left gates again; nothing stacks on a batch an entry of which has left (#247)."""
    SLOW = "60"  # seconds the gate of the removed entry would take, far past what the test waits

    def setUp(self):
        super().setUp()
        for name, value in (("apart", BOTH_HERE), ("BESIDE_POLL", 0.02), ("offerable", lambda config, lanes: None),
                            ("STACK_POLL", 0.1), ("REMOVAL_POLL", 0.1)):
            patcher = patch.object(mq, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        starting = patch.object(mq.Queue, "start")
        starting.start()
        self.addCleanup(starting.stop)
        # The removal lands on the gate lane these tests time: the fixture's project has no preflight lane ahead of it,
        # which a loaded Mac could stretch past the moment a test removes the entry.

    def remove(self, identifier):
        """Remove identifier as `qk queue remove` does from another process while the worker runs, whose lines
        go to the worker's log here, as stdout is one for both."""
        mq.remove(mq.Queue(self.repo), identifier)

    def arrive(self, branch):
        """Add branch as a session's qk land does, without its trial, as when the batch ahead changes after it."""
        with patch.object(mq, "check"): mq.add(self.repo, branch)
        return self.added(branch)["id"]

    def during(self, action, after=0.5):
        """action runs on its own thread after seconds of the first gate, as a session's commands do."""
        real, first = mq.Worker.gate, []
        def gate(worker, lanes, round_name, where=(), cancel=None):
            if not first:
                first.append(True)
                timer = threading.Timer(after, action)
                timer.start()
                self.addCleanup(timer.join)
            return real(worker, lanes, round_name, where, cancel)
        return patch.object(mq.Worker, "gate", gate)

    def added(self, branch):
        return next(entry for entry in mq.Queue(self.repo).all() if entry["branch"] == branch)

    def worked(self, *more):
        with contextlib.redirect_stdout(io.StringIO()) as said, contextlib.ExitStack() as stack:
            for context in more: stack.enter_context(context)
            began = time.monotonic()
            self.assertEqual(self.work(), 0)
        return said.getvalue(), time.monotonic() - began

    def test_removing_a_testing_entry_stops_its_gate_and_the_rest_lands(self):
        self.commit("slow", "a", self.SLOW); self.commit("b", "b")
        a, b = self.add("a", "b")
        run = mq.processes.run
        stop_times = []

        def observe_stop(command, *, cancel=None, **options):
            if not isinstance(cancel, mq.Removal) or a not in cancel.ids:
                return run(command, cancel=cancel, **options)
            is_set, removed = cancel.is_set, None

            def remove_once_running():
                nonlocal removed
                # The fixture writes the counter after the lane has started, before it sleeps.
                if removed is None and self.counter.exists():
                    self.remove(a)
                    removed = time.monotonic()
                return is_set()

            with patch.object(cancel, "is_set", remove_once_running):
                try:
                    return run(command, cancel=cancel, **options)
                except mq.processes.Canceled:
                    self.assertIsNotNone(removed)
                    stop_times.append(time.monotonic() - removed)
                    raise

        log, _ = self.worked(patch.object(mq.processes, "run", observe_stop))
        self.assertEqual(len(stop_times), 1, log)
        self.assertLess(stop_times[0], float(self.SLOW) / 2, log)
        self.assertEqual((self.state(a)["state"], self.state(b)["state"]), ("removed", "landed"), log)
        self.assertRegex(log, f"gate: stopped[^\\n]*, as {a} left the queue")
        self.assertIn(f"{a} left the queue, which stopped the gate: gating what is left", log)
        self.assertNotIn("unmeasured", log)
        self.assertNotIn("no verdict", log)
        self.assertIsNone(mq.Queue(self.repo).held())
        self.assertNotIn("slow", self.main_files())
        self.assertIn("b", self.main_files())
        # What `qk queue remove` said: the worker stops the gate, rather than letting it hold the queue.
        self.assertIn(f"removed {a}\n", log)
        self.assertIn(f"{a} was testing: the worker stops its gate within about 0.1s", log)

    def test_a_replacement_that_conflicts_only_with_the_removed_entry_is_not_stacked_on_it(self):
        """The case of #247: the replacement, based on origin/main, changes the removed entry's file. It stacks on
        nothing, is told it conflicts with the removed entry rather than origin/main, and lands once that entry's gate
        has stopped."""
        # a is removed once the stacker has looked at c and said it conflicts with a, while a's gate still runs: a
        # condition observed, not a time guessed (vocaro-engineering#381). The stacker looks only once a's final lane
        # runs admitted (Worker.admitted_mac), and then makes its checkout and integrates c there, so the look comes
        # as late as the Mac's load makes it: on another Mac on 2026-11-12, 94 s after c arrived. a's gate
        # therefore holds (hold, in LANE) until the removal stops it, rather than sleeping a fixed time the look could
        # outlast, and the wait for the look has a bound stretched by the load, which only ends a failing run, with a
        # removed either way (vocaro/queuekeeper#31). What the removal is checked for is fixed: a's gate stops within
        # STOP of it. A run in which it does not is released after STOP, so that it ends, and fails.
        STOP = float(self.SLOW)
        self.commit("hold", "a"); self.commit("same", "a", "a", start="a"); self.commit("same", "c", "c")
        [a] = self.add("a")
        conflicted, stopped, looked, stops, real_say = threading.Event(), threading.Event(), [], [], mq.Worker.say
        def say(worker, words):
            real_say(worker, words)
            if words.startswith("stacked: ") and f" conflicts with {a}, ahead of it" in words: conflicted.set()
            if words.startswith("gate: stopped") and words.endswith(f", as {a} left the queue"): stopped.set()
        def replace():
            self.arrive("c")
            seen = conflicted.wait(stretched(2 * STOP))
            looked.append((seen, self.state(a)["state"]))
            removed = time.monotonic()
            self.remove(a)
            if stopped.wait(STOP): stops.append(time.monotonic() - removed)
            else: Path(f"{self.counter}.release.1").touch()  # a's gate, the first, ends, and the test fails
        log, took = self.worked(self.during(replace), patch.object(mq.Worker, "say", say))
        c = self.added("c")
        self.assertEqual(looked, [(True, mq.TESTING)], log)
        self.assertEqual(len(stops), 1, log)
        self.assertLess(stops[0], STOP, log)
        # The whole run, through its admissions and c's gate, only by a bound a loaded Mac stretches.
        self.assertLess(took, stretched(STOP), log)
        self.assertEqual((self.state(a)["state"], c["state"]), ("removed", "landed"), log)
        self.assertIn(f"stacked: {c['id']}: {c['tip'][:12]} conflicts with {a}, ahead of it, not with "
                      "origin/main, in same: once it lands, rebase onto origin/main and add it again", log)
        self.assertNotIn("conflicts with origin/main", log)
        self.assertNotIn("that arrived during the gate", log)
        self.assertRegex(log, f"gate: stopped[^\\n]*, as {a} left the queue")

    def test_removing_a_queued_entry_leaves_the_gate_running(self):
        """Negative control: removing an entry that waits behind the batch, not stacked on it, stops nothing; the
        batch lands, and remove says nothing of a gate."""
        self.commit("slow", "a", "2"); self.commit("b", "b")
        [a] = self.add("a")
        with patch.object(mq, "STACK_POLL", 30):  # b stays queued, not stacked
            log, _ = self.worked(self.during(lambda: self.remove(self.arrive("b")), after=0.3))
        b = self.added("b")
        self.assertEqual((self.state(a)["state"], b["state"]), ("landed", "removed"), log)
        self.assertIn(f"removed {b['id']}\n", log)
        self.assertNotIn("left the queue", log)
        self.assertNotIn("was testing", log)
        self.assertEqual(self.gates(), 1, log)

    def test_remove_says_what_becomes_of_a_testing_entrys_gate(self):
        """Its line after `removed ID`: only for an entry that was testing."""
        self.commit("a", "a"); self.commit("b", "b")
        a, b = self.add("a", "b")
        queue = mq.Queue(self.repo)
        queue.mark([{"id": a}], mq.TESTING)
        queue.record({"id": a}, stopsWhenRemoved=5)
        with contextlib.redirect_stdout(io.StringIO()) as said, patch.object(mq.Queue, "working", return_value=True):
            mq.remove(queue, a)
            mq.remove(queue, b)
        self.assertEqual(said.getvalue(), f"removed {a}\n{a} was testing: the worker stops its gate within about 5s, "
                         "on this Mac or the second, which frees its admission there, and gates the rest of its batch "
                         f"again\nremoved {b}\n")

    def test_what_remove_says_of_a_testing_entry(self):
        """A worker that records stopsWhenRemoved stops the gate; an older one lets it run on, which remove says,
        with how long it may hold the queue; and with no worker, no gate is left."""
        self.commit("a", "a")
        [a] = self.add("a")
        queue = mq.Queue(self.repo)
        entry = dict(queue.read(a), state=mq.TESTING)
        with patch.object(mq.Queue, "working", return_value=True):
            self.assertIn("stops its gate within about 5s", mq.stopping(queue, dict(entry, stopsWhenRemoved=5)))
            older = mq.stopping(queue, entry)
        self.assertIn("does not stop a gate for a removal: its gate runs on until its lanes end, at most 6 h a lane, "
                      "and holds the queue until then", older)
        with patch.object(mq.Queue, "working", return_value=False):
            self.assertIn("no worker runs, so no gate of it is left to stop", mq.stopping(queue, entry))


class UngatedAheadOfARound(Fixture):
    """An entry qk land --front --no-gate put first lands ahead of a round whose gate waits to start, which then gates
    again on top of it, and waits only for a gate that runs, which `qk queue status` says (vocaro/queuekeeper#26)."""
    WHY = "a docs fix every session needs now"
    LIMIT = 60  # seconds a test waits for what it watches for, far past what it takes

    def setUp(self):
        super().setUp()
        for name, value in (("apart", BOTH_HERE), ("BESIDE_POLL", 0.02), ("offerable", lambda config, lanes: None),
                            ("STACK_POLL", 0.1), ("REMOVAL_POLL", 0.1)):
            patcher = patch.object(mq, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        starting = patch.object(mq.Queue, "start")
        starting.start()
        self.addCleanup(starting.stop)
        # Only the gate lane runs, as the fixture's project has no preflight lane, so that the ungated landing runs no
        # lane and the gate's run is the one timed.

    def until(self, happened, what):
        ends = time.monotonic() + self.LIMIT
        while not happened():
            if time.monotonic() > ends: raise AssertionError(f"no {what} within {self.LIMIT}s")
            time.sleep(0.02)

    def arrive(self, branch):
        """Add branch as a session's qk land --front --no-gate does while the worker runs."""
        with patch.object(mq, "check"), patch("builtins.print"):
            return mq.add(self.repo, branch, front=True, no_gate=True, why=self.WHY)

    def waits_for_admission(self):
        """The gate lane's record as qk run writes it while the lane waits for admission (lane_wait.waiting)."""
        path = mq.lane_wait.path_of(mq.Queue(self.repo).checkout, "gate")
        now = time.time()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schemaVersion": mq.lane_wait.SCHEMA, "kind": mq.lane_wait.ADMISSION,
                                    "reason": "another project's whole-host gate holds this Mac",
                                    "mac": mq.lane_wait.HERE, "offer": None, "stranded": False, "pid": os.getpid(),
                                    "admitted": False, "since": now, "written": now}))

    def worked(self, action):
        """Run the worker while action runs on its own thread, as a session's commands do; what the worker said."""
        failed = []
        def act():
            try: action()
            except BaseException as error: failed.append(error)
        thread = threading.Thread(target=act)
        with contextlib.redirect_stdout(io.StringIO()) as said:
            thread.start()
            try: self.assertEqual(self.work(), 0)
            finally: thread.join(self.LIMIT)
        if failed: raise failed[0]
        return said.getvalue()

    def subjects(self):
        run(self.repo, "git", "fetch", "-q", "origin")
        return run(self.repo, "git", "log", "-3", "--format=%s", "origin/main").splitlines()

    def test_it_lands_ahead_of_a_round_waiting_for_admission(self):
        self.commit("hold", "a"); self.commit("fix", "fix")
        [a] = self.add("a")
        (self.base / "gates.release.2").write_text("")  # a's gate again, on top of the fix, runs through
        arrived = []
        def arrive():
            self.until(self.counter.exists, "gate of a")
            self.waits_for_admission()
            arrived.append(self.arrive("fix"))
            # The fix lands while a's first gate still holds; released then either way, so that a worker that waits
            # for it fails this test rather than hang it.
            try: self.until(lambda: self.state(arrived[0])["state"] == mq.LANDED, "landing of the fix")
            finally: (self.base / "gates.release.1").write_text("")
        log = self.worked(arrive)
        [fix] = arrived
        self.assertEqual((self.state(fix)["state"], self.state(a)["state"]), ("landed", "landed"), log)
        # The fix landed first, and a gated again on top of it.
        self.assertEqual(self.subjects()[:2], ["Add hold", "Add fix"], log)
        self.assertEqual(self.gates(), 2, log)
        self.assertIn(mq.UNGATED, run(self.repo, "git", "log", "-1", "--format=%B", "origin/main~1"))
        self.assertIn(f"gate: stopped before it started, as {fix} lands first without the gate, as qk land --front "
                      "--no-gate asked, while gate waited for admission on this Mac", log)
        self.assertIn("which stopped the gate before it started: the batch gates again on top of it", log)
        self.assertNotIn("no verdict", log)
        self.assertNotIn("unmeasured", log)
        self.assertIsNone(mq.Queue(self.repo).held())

    def test_an_entry_of_the_round_given_both_again_leaves_it(self):
        """qk land --front --no-gate again for an entry whose round's gate waits for admission takes it out of that
        round, which gates again on top of it, as on 2026-11-15, when only `qk queue remove` did."""
        self.commit("hold", "a"); self.commit("fix", "fix")
        a, fix = self.add("a", "fix")
        (self.base / "gates.release.2").write_text("")  # a's gate again, on top of the fix, runs through
        said = []
        def again():
            self.until(self.counter.exists, "gate of a and the fix")
            self.waits_for_admission()
            # Its own lines, read from print, since the worker on the other thread writes to the same stdout.
            with patch("builtins.print", side_effect=lambda *words, **_: said.append(" ".join(map(str, words)))):
                mq.add(self.repo, "fix", front=True, no_gate=True, why=self.WHY)
            try: self.until(lambda: self.state(fix)["state"] == mq.LANDED, "landing of the fix")
            finally: (self.base / "gates.release.1").write_text("")
        log = self.worked(again)
        self.assertIn(f"queue: {fix} is testing: the worker takes it out of its round", "\n".join(said))
        self.assertNotIn("changes nothing now", "\n".join(said))
        self.assertEqual((self.state(fix)["state"], self.state(a)["state"]), ("landed", "landed"), log)
        self.assertEqual(self.subjects()[:2], ["Add hold", "Add fix"], log)
        self.assertEqual(self.gates(), 2, log)
        self.assertIn(mq.UNGATED, run(self.repo, "git", "log", "-1", "--format=%B", "origin/main~1"))
        self.assertIn(f"gate: stopped before it started, as {fix} lands first without the gate", log)

    def test_another_option_of_a_testing_entry_says_how_to_land_it_ungated(self):
        """--no-gate alone, given again to an entry in its round, changes nothing, and says what would."""
        self.commit("fix", "fix")
        [fix] = self.add("fix")
        queue = mq.Queue(self.repo)
        queue.mark([{"id": fix}], mq.TESTING)
        out = io.StringIO()
        with contextlib.redirect_stderr(out), contextlib.redirect_stdout(io.StringIO()):
            mq.promote(queue, {"id": fix}, {"noGate": True, "aheadWhy": self.WHY})
        self.assertIn(f"{fix} is testing, so --no-gate changes nothing now", out.getvalue())
        self.assertIn(f"give --front and --no-gate together, which take it out of a round whose gate waits to start, "
                      f"or run `qk queue remove {fix}` and land it again with --no-gate", out.getvalue())
        self.assertFalse(self.state(fix).get("noGate"))

    def test_it_waits_for_a_gate_that_runs_and_status_says_so(self):
        self.commit("hold", "a"); self.commit("fix", "fix")
        [a] = self.add("a")
        seen = []
        def arrive():
            self.until(self.counter.exists, "gate of a")
            fix = self.arrive("fix")
            time.sleep(1)  # ten looks for an entry ahead of the gate, which a running gate gives none
            out = io.StringIO()
            with contextlib.redirect_stdout(out): mq.status(self.repo)
            seen.append((fix, self.state(fix)["state"], out.getvalue()))
            (self.base / "gates.release.1").write_text("")
        log = self.worked(arrive)
        [(fix, state, status)] = seen
        self.assertEqual(state, "queued", log)
        self.assertRegex(status, f"{fix} .*\n(.*\n)*? +waits for the running gate of {a}, which has run \\d+s")
        self.assertEqual((self.state(fix)["state"], self.state(a)["state"]), ("landed", "landed"), log)
        self.assertEqual(self.subjects()[:2], ["Add fix", "Add hold"], log)
        self.assertEqual(self.gates(), 1, log)
        self.assertNotIn("stopped before it started", log)

    def test_what_status_says_it_waits_for(self):
        """Between rounds nothing; a gate that runs, but no stacked one, and its time; else the round it goes ahead
        of."""
        self.assertIsNone(mq.ahead_of_round({}))
        running = {"lane": "gate", "began": 1000, "entries": ["a"], "waiting": None}
        waiting = {**running, "waiting": "admission on this Mac: busy"}
        self.assertEqual(mq.ahead_of_round({"entries": ["a"], "gates": {"batch": running}}, at=1185),
                         "waits for the running gate of a, which has run 3m05s")
        self.assertEqual(mq.ahead_of_round({"entries": ["a"], "gates": {"batch": waiting, "stacked": running}},
                                           at=1185),
                         "lands ahead of the round of a, whose gate waits to start: the worker stops it within about "
                         "0.1s")
        self.assertEqual(mq.ahead_of_round({"entries": ["a"], "gates": {"older half": waiting,
                                                                         "newer half": running}}, at=1060),
                         "waits for the running gate of a, which has run 1m00s")


class IdleStatus(Fixture):
    """`qk queue status --idle` exits 0 only when no worker runs and no entry is queued, testing or passed, and BUSY
    otherwise, with the lines status prints anyway, so that a script can ask whether ending the worker would end a
    gate (vocaro/queuekeeper#21)."""

    def said(self, *words):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = cli.main(["--project", str(self.repo), "queue", "status", *words])
        return code, out.getvalue()

    def asked(self):
        """--idle's exit status, after checking that its lines are status's own and that --json says the same. The
        three reads see one clock, since a second that passes between them, as on a loaded Mac, changes the times
        status prints, such as how long the worker has waited (vocaro/queuekeeper#305)."""
        with patch.object(mq.time, "time", return_value=time.time()):
            code, said = self.said("--idle")
            plain, shown = self.said()
            self.assertEqual((plain, said), (0, shown))
            answer, printed = self.said("--idle", "--json")
        self.assertEqual(answer, code)
        self.assertEqual(json.loads(printed)["idle"], code == 0)
        return code

    @contextlib.contextmanager
    def live_worker(self):
        """A live process that holds the worker's lock, as a worker between rounds does, until the block ends and it
        is killed, which leaves the lock file behind."""
        lock = mq.Queue(self.repo).directory / "worker.lock"
        script = ("import fcntl, sys, time; lock = open(sys.argv[1], 'w'); fcntl.flock(lock, fcntl.LOCK_EX); "
                  "print('locked', flush=True); time.sleep(600)")
        holder = subprocess.Popen([sys.executable, "-c", script, str(lock)], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "locked")
            yield
        finally:
            holder.kill()
            holder.wait()
            holder.stdout.close()
        self.assertTrue(lock.exists())

    def test_an_empty_queue_with_no_worker_is_idle(self):
        self.assertEqual(self.asked(), 0)
        self.assertEqual(self.said("--idle")[1].splitlines()[0], "worker: none")

    def test_an_entry_queued_testing_or_passed_is_not_idle(self):
        self.commit("a.txt", "one")
        identifier, = self.add("one")
        queue = mq.Queue(self.repo)
        self.assertEqual(self.asked(), mq.resources.BUSY)
        queue.mark([self.state(identifier)], mq.TESTING)
        self.assertEqual(self.state(identifier)["state"], mq.TESTING)
        self.assertEqual(self.asked(), mq.resources.BUSY)
        queue.save({**self.state(identifier), "state": mq.PASSED, "passed": {"head": "a" * 40, "base": "b" * 40}})
        self.assertEqual(self.asked(), mq.resources.BUSY)
        queue.save({**self.state(identifier), "state": mq.LANDED, "settled": time.time()})
        self.assertEqual(self.asked(), 0)

    def test_a_live_worker_with_nothing_waiting_is_not_idle_and_a_dead_ones_lock_is_none(self):
        with self.live_worker():
            self.assertEqual(self.asked(), mq.resources.BUSY)
            self.assertEqual(self.said("--idle")[1].splitlines()[0], "worker: running")
        self.assertEqual(self.asked(), 0)

    def test_a_held_queue_is_idle_only_with_nothing_waiting(self):
        with patch("builtins.print"): mq.Queue(self.repo).hold("for a session: a test holds it")
        self.assertEqual(self.asked(), 0)
        self.commit("a.txt", "one")
        self.add("one")
        self.assertEqual(self.asked(), mq.resources.BUSY)
        self.assertIn("held for a session: a test holds it", self.said("--idle")[1])

    def test_a_worker_that_only_waits_to_move_the_shared_checkout_is_idle_and_status_says_so(self):
        """vocaro/queuekeeper#285: a worker that has settled every entry and only waits for the shared checkout to
        follow origin/main (Worker.catch_up) blocks no maintenance, though another project's run can hold it for
        hours, so --idle exits 0 and status says that this waiting does not block maintenance."""
        queue, now = mq.Queue(self.repo), time.time()
        with self.live_worker():
            queue.note(lambda state: state.update(
                waiting="the shared checkout to follow origin/main once nothing runs from it",
                waitingSince=now - 60, catchingUp=True,
                holders=[{"pid": 26815, "what": "the queue worker for mpm", "since": now - 120}]))
            self.assertEqual(self.asked(), 0)
            # Read at the moment the account says it was written, so that the holder has run exactly 2m00s however
            # slowly the reads before came (vocaro/queuekeeper#305).
            with patch.object(mq.time, "time", return_value=now): lines = self.said("--idle")[1].splitlines()
            self.assertEqual(lines[0], "worker: running")
            self.assertIn("    held by the queue worker for mpm, PID 26815, running 2m00s", lines)
            self.assertIn(f"    {mq.CATCHING_UP_WORDS}", lines)
            # The negative controls: an entry that arrives meanwhile is not idle, whatever the account says...
            self.commit("a.txt", "one")
            identifier, = self.add("one")
            self.assertEqual(self.asked(), mq.resources.BUSY)
            queue.save({**self.state(identifier), "state": mq.LANDED, "settled": time.time()})
            self.assertEqual(self.asked(), 0)
            # ...and a live worker whose account says no such wait, as when catch_up has ended, is not idle either.
            queue.note(lambda state: state.update(waiting=None, waitingSince=None, holders=None, catchingUp=None))
            self.assertEqual(self.asked(), mq.resources.BUSY)
            self.assertNotIn(mq.CATCHING_UP_WORDS, self.said()[1])


class ClosedQueue(Fixture):
    """`qk queue close --why TEXT` closes the queue to new entries: `qk land` and `qk queue add` refuse each with the
    reason and who closed it, the worker finishes what the queue has, status shows the closure, and `qk queue reopen`
    takes entries again, so that maintenance that needs an empty queue can close it, wait for `--idle` and reopen it
    with no entry arriving in between (vocaro/queuekeeper#32)."""
    WHY, BY = "the launcher is being retired", "the maintenance session"

    def command(self, *words):
        """(exit status, standard output, standard error) of `qk queue WORDS` or `qk land WORDS`, starting no worker."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), patch.object(mq.Queue, "start"), \
                patch.object(cli.plugins, "recover"), contextlib.chdir(self.repo):
            code = cli.main(["--project", str(self.repo), *words])
        return code, out.getvalue(), err.getvalue()

    def close(self):
        code, said, _ = self.command("queue", "close", "--why", self.WHY, "--by", self.BY)
        self.assertEqual(code, 0, said)
        return said

    def kept(self):
        """The queue's entries and the refs that keep their commits."""
        return mq.Queue(self.repo).all(), run(self.repo, "git", "for-each-ref", "refs/queuekeeper/queue")

    def refused(self, branch, **options):
        """The refusal of branch by mq.add, which must queue nothing and replace nothing."""
        before = self.kept()
        with self.assertRaises(mq.Refused) as caught, patch.object(mq.Queue, "start"), patch("builtins.print"):
            mq.add(self.repo, branch, **options)
        self.assertEqual(self.kept(), before)
        return caught.exception

    def amended(self, branch):
        """One more commit on branch, which `qk land` of it would replace its queued entry with."""
        run(self.repo, "git", "checkout", "-q", branch)
        (self.repo / "more.txt").write_text("x")
        run(self.repo, "git", "add", "more.txt")
        run(self.repo, "git", "commit", "-qm", "Add more.txt")

    def test_a_closed_queue_refuses_a_new_entry_with_the_reason_and_who_closed_it(self):
        tip = self.commit("a.txt", "one")
        said = self.close()
        self.assertIn(f"closed to new entries by {self.BY} since ", said)
        self.assertIn(self.WHY, said)
        self.assertIn("0 waiting", said)
        error = self.refused("one")
        self.assertRegex(str(error), rf"^{tip[:12]} is not queued: the queue is closed to new entries by {self.BY} "
                         rf"since \d{{4}}-\d\d-\d\d \d\d:\d\d:\d\d \(\d+s ago\): {self.WHY}\. Entries already in it "
                         r"finish, and it reopens by itself at \S+ \S+, in (59m\d\ds|1h00m), unless its closer renews it with "
                         r"`qk queue close --extend`\.")
        self.assertIn("`qk queue reopen` once done; if that session is gone, any session may", str(error))
        self.assertIn("once `qk queue closed` exits 1", str(error))
        self.assertEqual(error.waits_for, [])
        self.assertEqual((error.closed["why"], error.closed["by"]), (self.WHY, self.BY))
        for words in (("land", "one"), ("land", "one", "--wait", "--limit", "0"), ("queue", "add", "one")):
            with self.subTest(words=words):
                code, out, err = self.command(*words)
                self.assertEqual(code, 1)
                self.assertEqual(out, "")
                self.assertIn(f"queuekeeper: {tip[:12]} is not queued: the queue is closed to new entries by {self.BY}", err)
                self.assertIn(self.WHY, err)
        self.assertEqual(mq.Queue(self.repo).all(), [])

    def test_front_no_gate_and_a_replacement_are_refused_and_the_earlier_entry_stays(self):
        self.commit("a.txt", "one")
        identifier, = self.add("one")
        self.close()
        self.commit("fix.txt", "fix")
        for options in ({"front": True}, {"no_gate": True, "why": "an emergency"},
                        {"front": True, "no_gate": True, "why": "an emergency"}):
            with self.subTest(options=options):
                self.assertIn("closed to new entries", str(self.refused("fix", **options)))
        # The branch's newer commits would replace its entry, which stays queued with the commits it had.
        earlier = self.state(identifier)["tip"]
        self.amended("one")
        self.assertIn("closed to new entries", str(self.refused("one")))
        self.assertEqual((self.state(identifier)["state"], self.state(identifier)["tip"]), (mq.QUEUED, earlier))

    def test_a_commit_the_queue_already_holds_is_followed_and_its_options_still_move_it(self):
        self.commit("a.txt", "one")
        identifier, = self.add("one")
        self.close()
        with patch.object(mq.Queue, "start"), contextlib.redirect_stdout(io.StringIO()) as said:
            self.assertEqual(mq.add(self.repo, "one"), identifier)
            self.assertEqual(mq.add(self.repo, "one", front=True, why="it unblocks the queue"), identifier)
        self.assertIn(f"{identifier} already holds", said.getvalue())
        self.assertTrue(self.state(identifier)["front"])
        self.assertEqual(len(mq.Queue(self.repo).all()), 1)

    def test_the_worker_finishes_every_entry_a_closed_queue_has_whatever_its_round(self):
        """A batch that fails and bisects, its halves, an entry alone and a --front entry all reach a verdict."""
        for name in ("a", "b", "bad", "d", "first"): self.commit(name, name)
        ids = self.add("a", "b", "bad", "d")
        with patch.object(mq.Queue, "start"), patch("builtins.print"):
            front = mq.add(self.repo, "first", front=True, why="it goes first")
        self.close()
        self.assertEqual(self.command("queue", "status", "--idle")[0], mq.resources.BUSY)
        with patch("builtins.print"): self.assertEqual(self.work(), 0)
        self.assertEqual([self.state(i)["state"] for i in (*ids, front)],
                         ["landed", "landed", "failed", "landed", "landed"])
        self.assertEqual(self.main_files() & {"a", "b", "bad", "d", "first"}, {"a", "b", "d", "first"})
        # Still closed once drained: idle now, and nothing can arrive before the maintenance reopens it.
        self.assertTrue(mq.Queue(self.repo).closed())
        self.assertEqual(self.command("queue", "status", "--idle")[0], 0)
        self.commit("late", "late")
        self.assertIn("closed to new entries", str(self.refused("late")))

    def test_status_shows_the_closure_in_words_and_as_json_apart_from_a_hold(self):
        self.assertNotIn("closed", self.command("queue", "status")[1])
        self.assertIsNone(json.loads(self.command("queue", "status", "--json")[1])["closed"])
        self.close()
        code, said, _ = self.command("queue", "status")
        lines = said.splitlines()
        self.assertEqual((code, lines[0]), (0, "worker: none"))
        self.assertRegex(lines[1], rf"^closed to new entries by {self.BY} since \S+ \S+ \(\d+s ago\): {self.WHY}; entries "
                         r"already in it finish, it reopens by itself at \S+ \S+, in (59m\d\ds|1h00m), unless its closer renews "
                         r"it with `qk queue close --extend`, and `qk queue reopen` takes new ones sooner$")
        state = json.loads(self.command("queue", "status", "--json")[1])
        self.assertEqual((state["closed"]["why"], state["closed"]["by"]), (self.WHY, self.BY))
        self.assertAlmostEqual(state["closed"]["at"], time.time(), delta=600)
        self.assertEqual((state["held"], state["recovery"], state["idle"]), (None, None, True))
        self.assertEqual(state["closed"], mq.snapshot(self.repo)["closed"])
        # A hold beside it is the worker line's, as before, and resuming it leaves the queue closed.
        with patch("builtins.print"): mq.Queue(self.repo).hold("for a session: a test holds it")
        lines = self.command("queue", "status")[1].splitlines()
        self.assertEqual(lines[0], "worker: none; held for a session: a test holds it")
        self.assertTrue(lines[1].startswith("closed to new entries by "))
        self.assertEqual(self.command("queue", "resume")[0], 0)
        self.assertIsNone(mq.Queue(self.repo).held())
        self.assertTrue(mq.Queue(self.repo).closed())

    def test_reopening_is_one_command_that_takes_entries_again_and_clears_no_hold(self):
        self.commit("a.txt", "one")
        self.close()
        with patch("builtins.print"): mq.Queue(self.repo).hold("for a session: a test holds it")
        self.refused("one")
        code, said, _ = self.command("queue", "reopen")
        self.assertEqual(code, 0)
        self.assertIn(f"reopened: the queue was closed to new entries by {self.BY}", said)
        self.assertIsNone(mq.Queue(self.repo).closed())
        self.assertFalse(mq.Queue(self.repo).closed_file.exists())
        self.assertEqual(mq.Queue(self.repo).held(), "for a session: a test holds it")
        self.assertNotIn("closed to new entries", self.command("queue", "status")[1])
        identifier, = self.add("one")
        self.assertEqual(self.state(identifier)["state"], mq.QUEUED)
        self.assertEqual(self.command("queue", "reopen")[:2], (0, "the queue was not closed\n"))

    def test_idle_reads_a_closed_queue_as_any_other(self):
        self.close()
        self.assertEqual(self.command("queue", "status", "--idle")[0], 0)
        self.command("queue", "reopen")
        self.commit("a.txt", "one")
        identifier, = self.add("one")
        self.close()
        code, said, _ = self.command("queue", "status", "--idle")
        self.assertEqual(code, mq.resources.BUSY)
        self.assertIn("closed to new entries", said)
        state = json.loads(self.command("queue", "status", "--idle", "--json")[1])
        self.assertEqual((state["idle"], state["closed"]["by"]), (False, self.BY))
        mq.Queue(self.repo).save({**self.state(identifier), "state": mq.LANDED, "settled": time.time()})
        self.assertEqual(self.command("queue", "status", "--idle")[0], 0)

    def test_a_second_close_changes_nothing_and_names_the_first(self):
        self.close()
        first = mq.Queue(self.repo).closed()
        code, out, err = self.command("queue", "close", "--why", "another reason", "--by", "another session")
        self.assertEqual((code, out), (1, ""))
        self.assertIn(f"already closed to new entries by {self.BY}", err)
        self.assertEqual(mq.Queue(self.repo).closed(), first)

    def test_close_needs_a_reason_and_names_the_session_that_ran_it(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as usage:
            cli.main(["--project", str(self.repo), "queue", "close"])
        self.assertEqual(usage.exception.code, 2)
        self.assertEqual(self.command("queue", "close", "--why", "  ")[0], 2)
        self.assertIsNone(mq.Queue(self.repo).closed())
        origin = {"repository": "fixture", "branch": "work", "session": "abc", "mac": "desk"}
        with patch.object(mq.resources, "work_origin", return_value=origin):
            self.assertEqual(self.command("queue", "close", "--why", "a move\nof state")[0], 0)
        closure = mq.Queue(self.repo).closed()
        self.assertEqual((closure["by"], closure["why"]),
                         ("repository fixture, branch work, session abc, mac desk", "a move of state"))

    def test_a_queue_closed_while_an_entry_is_checked_saves_nothing(self):
        """The closure is read again under the lock that saves the entry, which close takes, so that an entry is either
        saved before close returns or refused."""
        self.commit("a.txt", "one")
        queue = mq.Queue(self.repo)
        with patch.object(mq, "check", side_effect=lambda *_: queue.close(self.WHY, self.BY)):
            error = self.refused("one")
        self.assertEqual(error.closed["by"], self.BY)
        queue.reopen()
        # Past the point where a replaced entry is taken out, the refusal says that it was.
        earlier, = self.add("one")
        self.amended("one")
        real = mq.remove
        def removed_then_closed(*arguments, **options):
            real(*arguments, **options)
            queue.close(self.WHY, self.BY)
        with patch.object(mq, "remove", side_effect=removed_then_closed), patch.object(mq.Queue, "start"), \
                patch("builtins.print"), self.assertRaises(mq.Refused) as caught:
            mq.add(self.repo, "one")
        self.assertIn(f"{earlier}, the branch's earlier commits, was taken out before it closed", str(caught.exception))
        self.assertEqual([entry["state"] for entry in queue.all()], [mq.REMOVED])

    def test_a_closure_that_cannot_be_read_still_closes_the_queue(self):
        self.commit("a.txt", "one")
        queue = mq.Queue(self.repo)
        queue.closed_file.write_text("{")
        self.assertIn("cannot be read", str(self.refused("one")))
        self.assertIn("closed to new entries by an unknown session", self.command("queue", "status")[1])
        self.assertEqual(self.command("queue", "reopen")[0], 0)
        self.assertIsNone(queue.closed())

    def test_closing_and_reopening_are_journaled(self):
        self.close()
        self.command("queue", "close", "--why", "again", "--by", "another")
        self.command("queue", "reopen")
        self.command("queue", "reopen")
        told = mq.events.read(mq.Queue(self.repo).directory, kinds=["queue."])
        self.assertEqual([(event["kind"], event["why"], event["by"]) for event in told],
                         [("queue.closed", self.WHY, self.BY), ("queue.reopened", self.WHY, self.BY)])
        self.assertTrue(mq.events.words(told[0]).endswith(f"closed to new entries by {self.BY}: {self.WHY}"))

    def lapsed(self, before=1):
        """Move the closure's deadline before seconds ago, as if that much time had passed."""
        queue = mq.Queue(self.repo)
        queue.write_closure({**queue.closure(), "until": time.time() - before})

    def test_a_closure_lapses_by_itself_at_its_deadline_and_the_queue_takes_entries_again(self):
        """vocaro/queuekeeper#286: a closer that is gone cannot refuse every landing forever."""
        self.commit("a.txt", "one")
        self.close()
        closure = mq.Queue(self.repo).closed()
        self.assertAlmostEqual(closure["until"] - closure["at"], mq.CLOSE_SECONDS, delta=1)
        # The negative control: before its deadline the closure refuses, whatever time has passed since it was made.
        mq.Queue(self.repo).write_closure({**closure, "at": time.time() - 3 * mq.CLOSE_SECONDS})
        self.assertIn("closed to new entries", str(self.refused("one")))
        self.assertEqual(self.command("queue", "closed")[0], 0)
        self.lapsed()
        self.assertIsNone(mq.Queue(self.repo).closed())
        self.assertNotIn("closed to new entries", self.command("queue", "status")[1])
        identifier, = self.add("one")
        self.assertEqual(self.state(identifier)["state"], mq.QUEUED)
        # Tidied away and journaled once, as having lapsed rather than been reopened.
        self.assertFalse(mq.Queue(self.repo).closed_file.exists())
        told = mq.events.read(mq.Queue(self.repo).directory, kinds=["queue."])
        self.assertEqual([(event["kind"], event["by"]) for event in told], [("queue.closed", self.BY), ("queue.lapsed", self.BY)])
        self.assertIn(f"reopened by itself: the closure by {self.BY} lapsed: {self.WHY}", mq.events.words(told[1]))
        # A lapsed closure is no closure: a new close takes its place, and reopen says it had lapsed.
        self.close()
        self.lapsed()
        self.assertEqual(self.command("queue", "close", "--why", "again", "--by", "another")[0], 0)
        self.assertEqual(mq.Queue(self.repo).closed()["by"], "another")
        self.lapsed()
        code, said, _ = self.command("queue", "reopen")
        self.assertEqual(code, 0)
        self.assertIn("the queue was not closed: its closure by another lapsed at ", said)

    def test_for_sets_the_deadline_within_a_day_and_refuses_anything_else(self):
        cases = {"90": 90, "45s": 45, "30m": 1800, "2h": 7200, "1h30m": 5400, "24h": 86400}
        for words, seconds in cases.items():
            with self.subTest(words=words):
                self.assertEqual(self.command("queue", "close", "--why", self.WHY, "--for", words)[0], 0)
                closure = mq.Queue(self.repo).closed()
                self.assertAlmostEqual(closure["until"] - closure["at"], seconds, delta=1)
                self.command("queue", "reopen")
        for words in ("0", "0m", "25h", "86401", "soon", "1d", ""):
            with self.subTest(words=words):
                code, _, err = self.command("queue", "close", "--why", self.WHY, "--for", words)
                self.assertEqual(code, 2)
                self.assertIn("--for takes", err)
                self.assertIsNone(mq.Queue(self.repo).closed())

    def test_its_closer_renews_it_with_extend_and_nothing_else_does(self):
        self.assertEqual(self.command("queue", "close", "--why", self.WHY, "--by", self.BY, "--for", "10m")[0], 0)
        first = mq.Queue(self.repo).closed()
        code, said, _ = self.command("queue", "close", "--extend", "--for", "2h")
        self.assertEqual(code, 0)
        self.assertIn("It reopens by itself at ", said)
        renewed = mq.Queue(self.repo).closed()
        self.assertEqual({key: renewed[key] for key in ("why", "by", "at")}, {key: first[key] for key in ("why", "by", "at")})
        self.assertAlmostEqual(renewed["until"], time.time() + 7200, delta=60)
        # Without --for, an hour from now.
        self.assertEqual(self.command("queue", "close", "--extend")[0], 0)
        self.assertAlmostEqual(mq.Queue(self.repo).closed()["until"], time.time() + mq.CLOSE_SECONDS, delta=60)
        told = mq.events.read(mq.Queue(self.repo).directory, kinds=["queue.renewed"])
        self.assertEqual(len(told), 2)
        # A second close is not a renewal, and leaves the deadline as it was.
        before = mq.Queue(self.repo).closed()["until"]
        self.assertEqual(self.command("queue", "close", "--why", "x", "--for", "24h")[0], 1)
        self.assertEqual(mq.Queue(self.repo).closed()["until"], before)
        # There is nothing to renew once the queue is open, by reopen or by lapsing.
        self.lapsed()
        code, _, err = self.command("queue", "close", "--extend")
        self.assertEqual(code, 1)
        self.assertIn("the queue is not closed", err)
        self.assertIsNone(mq.Queue(self.repo).closed())
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as usage:
            cli.main(["--project", str(self.repo), "queue", "close", "--extend", "--why", self.WHY])
        self.assertEqual(usage.exception.code, 2)

    def test_closed_answers_whether_the_queue_is_closed_and_for_how_long(self):
        code, said, _ = self.command("queue", "closed")
        self.assertEqual((code, said), (1, "open: the queue takes new entries\n"))
        state = json.loads(self.command("queue", "closed", "--json")[1])
        self.assertEqual(state, {"schemaVersion": 1, "closed": False, "closure": None, "closedSeconds": None,
                                 "reopensInSeconds": None})
        self.close()
        queue = mq.Queue(self.repo)
        queue.write_closure({**queue.closed(), "at": time.time() - 600})
        code, said, _ = self.command("queue", "closed")
        self.assertEqual(code, 0)
        self.assertRegex(said, rf"^closed to new entries by {self.BY} since \S+ \S+ \(10m0\ds ago\): {self.WHY}; it "
                         r"reopens by itself at \S+ \S+, in (59m\d\ds|1h00m), unless its closer renews it with "
                         r"`qk queue close --extend`\n$")
        code, out, _ = self.command("queue", "closed", "--json")
        state = json.loads(out)
        self.assertEqual((code, state["closed"], state["closure"]["by"]), (0, True, self.BY))
        self.assertAlmostEqual(state["closedSeconds"], 600, delta=60)
        self.assertAlmostEqual(state["reopensInSeconds"], mq.CLOSE_SECONDS, delta=60)
        self.lapsed()
        self.assertEqual(self.command("queue", "closed")[0], 1)

    def test_a_closure_from_before_deadlines_or_unreadable_lapses_an_hour_after_it_was_made(self):
        queue = mq.Queue(self.repo)
        now = time.time()
        for made, closed in ((now - 60, True), (now - mq.CLOSE_SECONDS - 60, False)):
            with self.subTest(made=made):
                queue.closed_file.write_text(json.dumps({"schemaVersion": 1, "why": self.WHY, "by": self.BY, "at": made}))
                self.assertEqual(bool(queue.closed()), closed)
                self.assertEqual(queue.closure()["until"], made + mq.CLOSE_SECONDS)
        queue.closed_file.write_text("{")
        self.assertIn("cannot be read", queue.closed()["why"])
        os.utime(queue.closed_file, (now - mq.CLOSE_SECONDS - 60,) * 2)
        self.assertIsNone(queue.closed())
        self.assertEqual(self.command("queue", "closed")[0], 1)
        self.assertFalse(queue.closed_file.exists())

    def test_doctor_names_a_closure_and_a_closer_whose_worktree_is_gone(self):
        self.assertNotIn("closed", [finding["signal"] for finding in mq.diagnose(self.repo)[0]])
        gone = self.repo.parent / "a-worktree-since-removed"
        with patch.object(mq.resources, "work_origin", return_value={"worktree": str(gone), "branch": "work"}):
            self.assertEqual(self.command("queue", "close", "--why", self.WHY)[0], 0)
        self.assertEqual(mq.Queue(self.repo).closed()["worktree"], str(gone))
        finding, = [finding for finding in mq.diagnose(self.repo)[0] if finding["signal"] == "closed"]
        self.assertIn(f"{self.WHY}, and refuses every new entry; it reopens by itself at ", finding["diagnosis"])
        self.assertIn(f"the worktree it was closed from, {gone}, is gone", finding["diagnosis"])
        self.assertIn("any session runs `qk queue reopen`", finding["remedy"])
        # A closer whose worktree is still there is not said to be gone.
        self.command("queue", "reopen")
        with patch.object(mq.resources, "work_origin", return_value={"worktree": str(self.repo)}):
            self.command("queue", "close", "--why", self.WHY)
        finding, = [finding for finding in mq.diagnose(self.repo)[0] if finding["signal"] == "closed"]
        self.assertNotIn("is gone", finding["diagnosis"])


class RoundAccount(Fixture):
    """The worker's account of its round in queuekeeper-queue (ROUND_FILE), which `qk queue status` shows so that nobody has
    to read worker.log to learn why entries wait: a bisect's halves, each gate's lane, Mac and time, and what it waits
    for."""
    UNREACHABLE = ("queuekeeper: gate waits for this Mac, and asks another Mac again within 60s, after 2 offers in a row "
                   "could not reach it: build-host cannot be reached: ssh: connect to host build-host port 22: No route to host")
    WHOLE_HOST = ("heavy: waiting (0s): it waits behind a whole-host lane holding the host: StudyWright gate")

    def setUp(self):
        super().setUp()
        for name, value in (("apart", BOTH_HERE), ("BESIDE_POLL", 0.02), ("offerable", lambda config, lanes: None)):
            patcher = patch.object(mq, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def shown(self, as_json=False, working=True):
        with patch.object(mq.Queue, "working", return_value=working), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            mq.status(self.repo, as_json=as_json)
        return json.loads(out.getvalue()) if as_json else out.getvalue()

    def watch(self, began=None):
        """A GateWatch of a gate lane, and the path of the lane's wait record."""
        queue = mq.Queue(self.repo)
        worker = mq.Worker(queue)
        worker.gating = ["e1"]
        log = queue.logs / "round-gate.log"
        log.write_text("")
        with patch.object(mq.Worker, "head", return_value="a" * 40):
            watch = mq.GateWatch(worker, "gate", time.time() - 90 if began is None else began, log)
        self.addCleanup(watch.stopped.set)
        record = mq.lane_wait.path_of(worker.checkout, "gate")
        patcher = patch.dict(os.environ, {mq.lane_wait.VARIABLE: str(record)})
        patcher.start()
        self.addCleanup(patcher.stop)
        return queue, watch

    def gate_after_look(self, queue, watch):
        with patch.object(mq.remote, "dispatched", return_value=None): watch.look()
        return queue.round_state()["gates"]["batch"]

    def test_a_gate_record_says_when_the_lanes_run_in_its_checkout_last_moved(self):
        # `qk unittest` keeps its progress in its own folder, and qk queue health reads the gate's moved from here.
        queue, watch = self.watch()
        self.assertIsNone(self.gate_after_look(queue, watch)["moved"])
        from queuekeeper import run_progress
        folder = run_progress.new_folder(watch.checkout)
        run_progress.Tracker(folder, ["qk", "unittest", "-s", "tests"]).started("test_a")
        self.assertAlmostEqual(self.gate_after_look(queue, watch)["moved"], time.time(), delta=30)

    def test_a_gate_record_says_when_its_lanes_log_last_changed(self):
        # A lane's command, such as `swift test` or a script, writes to its lane's log, not to the gate's (#185).
        queue, watch = self.watch(began=time.time() - 1800)
        from queuekeeper import run_progress
        log = watch.checkout / run_progress.LANES / "gate/lanes/swift-tests.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("Test Case passed\n")
        written = time.time() - 60
        os.utime(log, (written, written))
        self.assertAlmostEqual(self.gate_after_look(queue, watch)["moved"], written, delta=1)

    def test_a_gate_record_follows_the_lanes_wait_record(self):
        queue, watch = self.watch()
        mq.lane_wait.waiting(mq.lane_wait.UNREACHABLE, "ssh: connect to host build-host port 22: No route to host",
                             probes={"failed": 2, "last": time.time() - 40})
        gate = self.gate_after_look(queue, watch)
        self.assertEqual((gate["lane"], gate["entries"], gate["mac"]), ("gate", ["e1"], None))
        # The verdict's count and age, read as the gate is looked at (vocaro/queuekeeper#25).
        self.assertRegex(gate["waiting"], r"^another Mac cannot be reached \(2 probes failed, the last 4\d s ago\), "
                                          r"and is asked again within a minute: ssh: connect to host build-host port 22: "
                                          r"No route to host$")
        self.assertIsInstance(gate["waitingSince"], float)
        # What it waits for by kind, which qk queue doctor's pointer to `qk admissions now` reads (#342).
        self.assertEqual(gate["waitingKind"], mq.lane_wait.UNREACHABLE)
        mq.lane_wait.waiting(mq.lane_wait.ADMISSION, "all 3 guaranteed slots are busy")
        self.assertEqual(self.gate_after_look(queue, watch)["waitingKind"], mq.lane_wait.ADMISSION)
        # The Mac it waits for, which qk queue doctor reads that Mac's busy share and holders by (#89).
        self.assertEqual(queue.round_state()["gates"]["batch"]["waitingOn"], "this Mac")
        mq.lane_wait.relayed(json.dumps({"kind": mq.lane_wait.ADMISSION, "reason": "all slots are busy"}), "build-host")
        gate = self.gate_after_look(queue, watch)
        self.assertEqual(gate["waitingOn"], "build-host")
        self.assertEqual(mq.lane_wait.host_of(gate["waitingOn"]), "build-host")
        self.assertIsNone(mq.lane_wait.host_of("this Mac"))
        mq.lane_wait.running()
        gate = self.gate_after_look(queue, watch)
        self.assertEqual((gate["mac"], gate["waiting"], gate["waitingSince"], gate["waitingKind"]),
                         ("this Mac", None, None, None))
        # A lane that a dispatch sent to another Mac runs there.
        with patch.object(mq.remote, "dispatched", return_value="build-host"): watch.look()
        self.assertEqual(queue.round_state()["gates"]["batch"]["mac"], "build-host")
        # While its run goes on, its dispatch's folder has no name yet (remote.file_results): the record says where.
        mq.lane_wait.sent("build-host")
        self.assertEqual(self.gate_after_look(queue, watch)["mac"], "build-host")
        watch.finish()
        self.assertEqual(queue.round_state()["gates"], {})

    def test_a_wait_on_the_farm_mac_is_shown_and_names_the_mac(self):
        """The issue's first acceptance: its wait comes back as a record, and no log line says it."""
        queue, watch = self.watch()
        mq.lane_wait.sent("build-host")
        mq.lane_wait.relayed(json.dumps({"kind": mq.lane_wait.ADMISSION, "reason": "all 3 guaranteed slots are busy"}),
                             "build-host")
        gate = self.gate_after_look(queue, watch)
        self.assertEqual((gate["mac"], gate["waiting"]),
                         ("build-host", "admission on build-host: all 3 guaranteed "
                                                         "slots are busy"))
        said = self.shown()
        self.assertIn("gate lane on build-host, running", said)
        self.assertRegex(said, r"waiting: admission on build-host: all 3 guaranteed slots are busy, "
                               r"for \d+s")
        mq.lane_wait.relayed(b"null", "build-host")
        gate = self.gate_after_look(queue, watch)
        self.assertEqual((gate["mac"], gate["waiting"]), ("build-host", None))
        self.assertNotIn("waiting:", self.shown())

    def test_status_reads_the_record_and_no_printed_line(self):
        """The issue's second acceptance: what qk run prints may be reworded, or not printed at all."""
        queue, watch = self.watch()
        watch.log.write_text("\n".join((self.WHOLE_HOST, self.UNREACHABLE, "queuekeeper: trying gate on this Mac: busy",
                                        "remote: sending 0123456789ab to build-host")) + "\n")
        gate = self.gate_after_look(queue, watch)
        self.assertEqual((gate["mac"], gate["waiting"]), (None, None))
        mq.lane_wait.waiting(mq.lane_wait.ADMISSION, "it waits behind a whole-host lane holding the host: X")
        watch.log.write_text("heavy: slot 1 after 3s: fixture gate\n")  # says it was admitted, and is not read
        self.assertEqual(self.gate_after_look(queue, watch)["waiting"],
                         "admission on this Mac: it waits behind a whole-host lane holding the host: X")

    def test_a_lane_admitted_at_once_shows_no_wait(self):
        """Negative control: never having waited, nothing says it did."""
        queue, watch = self.watch()
        mq.lane_wait.running()
        gate = self.gate_after_look(queue, watch)
        self.assertEqual((gate["mac"], gate["waiting"], gate["waitingSince"], gate["stranded"]),
                         ("this Mac", None, None, False))
        self.assertNotIn("waiting:", self.shown())

    def test_a_wait_an_earlier_run_or_a_dead_process_left_is_not_shown(self):
        """A run that has moved on shows no stale wait."""
        queue, watch = self.watch(began=time.time() + 3600)  # a record written before this run's start
        mq.lane_wait.waiting(mq.lane_wait.ADMISSION, "all 3 guaranteed slots are busy")
        self.assertIsNone(self.gate_after_look(queue, watch)["waiting"])
        queue, watch = self.watch()
        path = Path(os.environ[mq.lane_wait.VARIABLE])
        # Negative control: the same record, from a live process, is shown.
        mq.lane_wait.waiting(mq.lane_wait.ADMISSION, "all 3 guaranteed slots are busy")
        self.assertIsNotNone(self.gate_after_look(queue, watch)["waiting"])
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        path.write_text(json.dumps({**json.loads(path.read_text()), "pid": dead.pid}))
        self.assertIsNone(self.gate_after_look(queue, watch)["waiting"])

    def test_a_lanes_record_is_no_verdict(self):
        """It holds nothing of an outcome, and a run ends by its exit whatever the record last said."""
        queue, watch = self.watch()
        mq.lane_wait.waiting(mq.lane_wait.ADMISSION, "all 3 guaranteed slots are busy")
        path = Path(os.environ[mq.lane_wait.VARIABLE])
        record = json.loads(path.read_text())
        # kindSince, when a wait of its kind began, is a time for the wait history and no outcome (#233).
        self.assertEqual(set(record), {"schemaVersion", "kind", "reason", "mac", "offer", "stranded", "pid", "since",
                                       "kindSince", "written", "admitted", "probes"})
        self.assertIs(record["admitted"], False)
        gate = self.gate_after_look(queue, watch)
        self.assertNotIn("status", gate)
        self.assertNotIn("outcome", gate)
        self.assertIn("gate lane on no Mac yet, running", self.shown())
        watch.finish()
        self.assertEqual(queue.round_state()["gates"], {})

    DEAD = ("admission on this Mac: a whole-host lane needs every guaranteed slot (HEAVY_SLOTS=3) free, and 3 are busy; "
            "the guard entry of BreezEPUB ways-in, PID 8645, is dead and requires inspection (it has no process record), "
            "and is counted as slot 1; `python3 scripts/qk admissions recover` lists it")

    def test_a_gate_held_up_by_dead_entries_past_the_threshold_is_stuck(self):
        # BreezEPUB's gate waited 86 min behind three dead entries while status said only "waiting" (#315).
        at = time.time()
        late, early = at - mq.STUCK_SECONDS - 60, at - mq.STUCK_SECONDS + 60
        said = mq.stuck({"waitingSince": late, "stranded": True}, at)
        self.assertEqual(said, "stuck: the gate has waited 16m00s for admission held up by dead or uncertain guard "
                               "entries that admission has not recovered; `python3 scripts/qk admissions recover` lists "
                               "them")
        # Negative controls: before the threshold; a wait on no stranded entry; a gate with no wait recorded.
        self.assertIsNone(mq.stuck({"waitingSince": early, "stranded": True}, at))
        self.assertIsNone(mq.stuck({"waitingSince": late, "stranded": False}, at))
        self.assertIsNone(mq.stuck({"stranded": True}, at))
        self.assertIsNone(mq.stuck({"waitingSince": None, "stranded": True}, at))
        queue = mq.Queue(self.repo)
        queue.note(lambda state: state.update(round="r", entries=["e1", "e2"], gates={
            "batch": {"lane": "gate", "began": late, "entries": ["e1"], "mac": None, "waiting": self.DEAD,
                      "waitingSince": late, "stranded": True, "log": "/l/b.log"},
            "stacked": {"lane": "gate", "began": late, "entries": ["e2"], "mac": None, "waiting": self.DEAD,
                        "waitingSince": early, "stranded": True, "log": "/l/s.log"}}))
        said = self.shown()
        self.assertEqual(said.count("    stuck: the gate has waited 16m"), 1, said)
        self.assertLess(said.index("batch (e1)"), said.index("stuck:"))
        self.assertLess(said.index("stuck:"), said.index("stacked gate (e2)"))
        shown = self.shown(as_json=True)["round"]["gates"]
        self.assertTrue(shown["batch"]["stuck"].startswith("stuck: the gate has waited 16m"))
        self.assertNotIn("stuck", shown["stacked"])

    def test_the_worker_log_says_once_that_a_gate_is_stuck(self):
        queue, watch = self.watch(began=time.time() - 1)
        mq.lane_wait.waiting(mq.lane_wait.ADMISSION, self.DEAD.removeprefix("admission on this Mac: "), stranded=True)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            watch.look()
            began = queue.round_state()["gates"]["batch"]["waitingSince"]
            self.assertEqual(out.getvalue(), "")
            later = began + mq.STUCK_SECONDS + 1
            with patch.object(mq.time, "time", return_value=later):
                watch.look()
                watch.look()
            self.assertEqual(queue.round_state()["gates"]["batch"]["waitingSince"], began)
            self.assertEqual(out.getvalue().count("gate: stuck: the gate has waited 15m01s"), 1, out.getvalue())
            # And the journal once, for whatever tells the owner (doc/visibility.md).
            told, = mq.events.read(queue.directory, kinds=["gate.stuck"])
            self.assertEqual((told["lane"], told["role"], told["waitingSince"]), ("gate", "batch", began))
            self.assertTrue(told["why"].startswith("stuck: the gate has waited 15m01s"), told["why"])
            # Once admitted, the wait and its start clear.
            mq.lane_wait.running()
            watch.look()
        gate = queue.round_state()["gates"]["batch"]
        self.assertEqual((gate["waiting"], gate["waitingSince"]), (None, None))
        watch.finish()

    def test_status_shows_the_round_and_why_its_gates_wait(self):
        queue = mq.Queue(self.repo)
        began = time.time() - 27 * 60
        queue.note(lambda state: state.update(
            round="20261107-120000-abcdef12", entries=["e1"], bisect={"suspects": 6, "older": 1, "newer": 2},
            gates={"older half": {"lane": "gate", "began": began, "entries": ["e1"], "mac": None, "log": "/l/o.log",
                                  "waiting": "admission on this Mac: it waits behind a whole-host lane holding the "
                                             "host: StudyWright gate"},
                   "newer half": {"lane": "gate", "began": began, "entries": ["e2", "e3"],
                                  "mac": "build-host", "waiting": None, "log": "/l/n.log"},
                   "stacked": {"lane": "gate", "began": time.time() - 5, "entries": ["e4"], "mac": None,
                               "waiting": "waits for this Mac: No route to host", "log": "/l/s.log"}}))
        said = self.shown()
        self.assertIn("round 20261107-120000-abcdef12: 1 entry; bisecting 6 entries: older half 1 entry, newer half 2",
                      said)
        self.assertIn("  older half (e1): gate lane on no Mac yet, running 27m", said)
        self.assertIn("    waiting: admission on this Mac: it waits behind a whole-host lane holding the host: "
                      "StudyWright gate", said)
        self.assertIn("  newer half (e2, e3): gate lane on build-host, running 27m", said)
        self.assertIn("  stacked gate (e4): gate lane on no Mac yet, running ", said)
        self.assertIn("    waiting: waits for this Mac: No route to host", said)
        self.assertLess(said.index("older half (e1)"), said.index("newer half (e2"))
        self.assertLess(said.index("newer half (e2"), said.index("stacked gate"))
        shown = self.shown(as_json=True)
        self.assertEqual(shown["round"]["bisect"], {"suspects": 6, "older": 1, "newer": 2})
        self.assertGreaterEqual(shown["round"]["gates"]["older half"]["runningSeconds"], 27 * 60)
        self.assertEqual(shown["round"]["gates"]["stacked"]["entries"], ["e4"])
        # Negative control: what a worker that stopped wrote is no round of the queue's.
        self.assertNotIn("round ", self.shown(working=False))
        self.assertIsNone(self.shown(as_json=True, working=False)["round"])

    def test_a_bisect_records_both_halves_while_they_gate_and_nothing_once_the_worker_stops(self):
        for name in ("a", "b", "bad", "d"): self.commit(name, name)
        self.add("a", "b", "bad", "d")
        queue, seen = mq.Queue(self.repo), []
        real = mq.processes.run
        def recorded(command, **options):
            seen.append(queue.round_state())
            return real(command, **options)
        with patch.object(mq.processes, "run", recorded), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.work(), 0)
        first = seen[0]
        self.assertIsNone(first["bisect"])
        self.assertEqual(list(first["gates"]), ["batch"])
        self.assertEqual(len(first["gates"]["batch"]["entries"]), 4)
        halves = [state for state in seen if state["bisect"]]
        self.assertTrue(halves, seen)
        self.assertEqual(halves[0]["bisect"], {"suspects": 4, "older": 2, "newer": 2})
        roles = set().union(*(state["gates"] for state in halves))
        self.assertLessEqual({"older half", "newer half"}, roles)
        self.assertFalse(queue.round_file.exists())

    def test_a_wait_for_the_host_to_settle_is_recorded_and_cleared(self):
        worker = mq.Worker(mq.Queue(self.repo))
        states = []
        with self.host([("cpu", "busy: the fixture's load"), None],
                       then=lambda: states.append(worker.queue.round_state())) as (slept, said):
            self.assertIsNone(worker.settle("the lanes run"))
        self.assertIn("the host to settle before the lanes run again, since busy: the fixture's load",
                      states[0]["waiting"])
        self.assertIsNone(worker.queue.round_state()["waiting"])


class WaitFollows(Fixture):
    """`qk land --wait` and `qk queue wait` say each change of their entry's situation once, by the round's account,
    and a heartbeat while nothing changes; their result lines and exit statuses stay as they were
    (vocaro/queuekeeper#41)."""
    MAC = "build-host"
    # When the rounds of these tests are looked at: each began 125 s before it, and its gate 65 s.
    NOW = 1_800_000_000.0

    @staticmethod
    def round(queue, name, entries, gates, bisect=None, started=None):
        def change(state):
            state.clear()
            state.update(round=name, started=started or WaitFollows.NOW - 125, entries=entries, gates=gates,
                         bisect=bisect)
        queue.note(change)

    @staticmethod
    def gate(entries, mac=None, waiting=None, kind=None):
        return {"lane": "gate", "began": WaitFollows.NOW - 65, "entries": entries, "mac": mac, "waiting": waiting,
                "waitingKind": kind, "log": "/logs/round-gate.log"}

    def test_each_change_is_one_line_and_the_result_lines_stay(self):
        self.commit("a", "a"); self.commit("b", "b")
        a, b = self.add("a", "b")
        queue = mq.Queue(self.repo)
        self.round(queue, "R1", [a], {"batch": self.gate([a], self.MAC)})
        # Each poll moves the round one step; one step changes nothing, and a wait says nothing of it.
        steps = [lambda: None,
                 lambda: self.round(queue, "R2", [b], {"batch": self.gate([b])}),
                 lambda: self.round(queue, "R2", [b], {"batch": self.gate([b], self.MAC)}),
                 lambda: self.round(queue, "R2", [b], {"older half": self.gate([b], self.MAC)},
                                    bisect={"suspects": 2, "older": 1, "newer": 1}),
                 lambda: queue.settle(self.state(b), mq.FAILED, why="its gate failed")]
        slept = []
        def poll():
            slept.append(mq.POLL)
            steps[len(slept) - 1]()
        with patch.object(mq.Queue, "start"), patch.object(mq.Queue, "working", return_value=True), \
                patch.object(mq.time, "time", return_value=self.NOW), \
                LandWaitsForAhead.polls(poll), contextlib.redirect_stdout(io.StringIO()) as said:
            self.assertEqual(mq.wait(self.repo, b), 1)
        lines = said.getvalue().splitlines()
        self.assertEqual(lines[:4], [
            f"{b} waits behind round R1 ({a}), which has run 2m05s",
            f"{b} is in round R2, 1 entry, whose gate runs: gate lane on no Mac yet",
            f"{b} is in round R2, 1 entry, whose gate runs: gate lane on {self.MAC}",
            f"{b} had its batch halved after a failure: the older half of round R2, 1 entry of 2: gate lane on "
            f"{self.MAC}"])
        # The result, exactly as before.
        self.assertEqual(lines[4:], mq.describe(self.state(b)).splitlines())

    def test_queue_wait_follows_each_entry_it_waits_for(self):
        self.commit("a", "a"); self.commit("b", "b")
        a, b = self.add("a", "b")
        queue = mq.Queue(self.repo)
        self.round(queue, "R1", [a], {"batch": self.gate([a], self.MAC)})
        def poll():
            for identifier in (a, b): queue.settle(self.state(identifier), mq.FAILED, why="its gate failed")
        with patch.object(mq.Queue, "start"), patch.object(mq.Queue, "working", return_value=True), \
                patch.object(mq.time, "time", return_value=self.NOW), \
                LandWaitsForAhead.polls(poll), contextlib.redirect_stdout(io.StringIO()) as said:
            self.assertEqual(mq.wait_for(self.repo, [a, b]), 1)
        self.assertEqual(said.getvalue().splitlines()[:2], [
            f"{a} is in round R1, 1 entry, whose gate runs: gate lane on {self.MAC}",
            f"{b} waits behind round R1 ({a}), which has run 2m05s"])

    def test_a_heartbeat_while_nothing_changes_says_what_status_shows(self):
        self.commit("a", "a"); self.commit("b", "b")
        a, b = self.add("a", "b")
        queue = mq.Queue(self.repo)
        waiting = "admission on build-host: its one-minute load 239 is past the busy limit"
        self.round(queue, "R1", [a], {"batch": self.gate([a], self.MAC, waiting, mq.lane_wait.ADMISSION)})
        now = [1000.0]
        follow = mq.Follow(b, clock=lambda: now[0])
        def look(seconds):
            now[0] += seconds
            return follow.look(queue, self.state(b), queue.round_state(), self.NOW)
        first = look(0)
        self.assertEqual(first, f"{b} waits behind round R1 ({a}), which has run 2m05s")
        self.assertIsNone(look(mq.HEARTBEAT - 1))
        beat = look(1)
        self.assertTrue(beat.startswith(f"heartbeat: {b} waits behind round R1 ({a}), which has run 2m05s; "
                                        f"round R1: 1 entry; batch ({a}): gate lane on {self.MAC}, running 1m05s; "
                                        f"waiting: {waiting}"), beat)
        self.assertNotIn("log:", beat)
        self.assertIsNone(look(60))
        # A change is said at once, and a situation already said is not said again, but in a heartbeat.
        self.round(queue, "R2", [b], {"batch": self.gate([b], self.MAC)})
        self.assertEqual(look(1), f"{b} is in round R2, 1 entry, whose gate runs: gate lane on {self.MAC}")
        self.round(queue, "R1", [a], {"batch": self.gate([a], self.MAC, waiting, mq.lane_wait.ADMISSION)})
        self.assertIsNone(look(1))
        self.assertTrue(look(mq.HEARTBEAT).startswith(f"heartbeat: {b} waits behind round R1"))
        # With no worker, and once passed.
        self.assertEqual(follow.look(queue, self.state(b), None), f"{b} is queued, and no worker runs yet: the wait "
                                                                    "starts one")
        self.assertEqual(follow.look(queue, {**self.state(b), "state": mq.PASSED}, queue.round_state()),
                         f"{b} passed its gate and waits for the push")
        # Its own round, halved, before its gate starts.
        self.round(queue, "R3", [b], {}, bisect={"suspects": 4, "older": 1, "newer": 3})
        self.assertEqual(look(1), f"{b} had its batch halved after a failure: the older half of round R3, 1 entry of "
                                  "4, whose gate has not started")

    def test_a_gate_that_failed_is_said_as_its_failed_tests_rerun_and_never_as_not_started(self):
        # On 2026-11-13 a round's flaky test was rerun alone for minutes, rerun 2 waiting for admission, while the
        # wait said its gate had not started (vocaro/queuekeeper#41).
        self.commit("a", "a")
        [a] = self.add("a")
        queue = mq.Queue(self.repo)
        test, record = "test_x.X.test_y", self.base / "rerun-wait.json"
        # A wait record counts from when the rerun began, by the clock it was written by.
        at = time.time()
        failure = {"lane": "gate", "entries": [a], "log": "/logs/R1-gate.log", "since": at - 30}
        self.round(queue, "R1", [a], {})
        queue.note(lambda state: state.update(failure=dict(failure)))
        now = [1000.0]
        follow = mq.Follow(a, clock=lambda: now[0])
        def look(seconds=1):
            now[0] += seconds
            return follow.look(queue, self.state(a), mq.rerun_waits(queue.round_state()), at)
        def failed(**fields):
            queue.note(lambda state: state["failure"].update(fields))
        self.assertEqual(look(), f"{a} is in round R1, 1 entry, whose gate failed in its gate lane: the worker reads its failure to "
                                 "tell a flake from a real one; see /logs/R1-gate.log")
        failed(tests=[test, "test_x.X.test_z"], reruns=mq.RERUNS)
        self.assertEqual(look(), f"{a} is in round R1, 1 entry, whose gate failed in its gate lane: 2 failed tests, {test}, "
                                 f"test_x.X.test_z, to rerun alone up to {mq.RERUNS} times, which wait to start; see "
                                 "/logs/R1-gate.log")
        self.assertIsNone(look())
        failed(test=test, number=1, attempt=2, rerunLog="/logs/R1-rerun-1-2.log", began=at - 50,
               waitRecord=str(record))
        self.assertEqual(look(), f"{a} is in round R1, 1 entry, whose gate failed in its gate lane: rerunning {test} alone, rerun "
                                 f"2 of {mq.RERUNS}, test 1 of 2; see /logs/R1-rerun-1-2.log")
        # The rerun's admission writes what it waits for to the record the failure names: said once, as a change.
        with patch.dict(os.environ, {mq.lane_wait.VARIABLE: str(record)}):
            mq.lane_wait.waiting(mq.lane_wait.ADMISSION, "a whole-host gate of another project holds this Mac")
        self.assertEqual(look(), f"{a} is in round R1, 1 entry, whose gate failed in its gate lane: rerunning {test} alone, rerun "
                                 f"2 of {mq.RERUNS}, test 1 of 2; waiting: admission on this Mac: a whole-host gate of "
                                 "another project holds this Mac; see /logs/R1-rerun-1-2.log")
        self.assertIsNone(look())
        beat = look(mq.HEARTBEAT)
        self.assertTrue(beat.startswith(f"heartbeat: {a} is in round R1"), beat)
        self.assertIn(f"failed gate ({a}): gate lane failed; rerunning {test} alone, rerun 2 of {mq.RERUNS}, test 1 of "
                      "2, running 50s; waiting: admission on this Mac", beat)
        self.assertNotIn("has not started", beat)
        # Status shows it too, with both logs, and --json has the record, with what the rerun waits for.
        lines = mq.round_lines(mq.rerun_waits(queue.round_state()), at)
        self.assertIn("    log: /logs/R1-gate.log\n    rerun log: /logs/R1-rerun-1-2.log", lines[-1])
        with patch.object(mq.Queue, "working", return_value=True):
            state = mq.snapshot(self.repo)
        self.assertEqual(state["round"]["failure"]["test"], test)
        self.assertIn("a whole-host gate of another project", state["round"]["failure"]["waiting"])


class GateResultsNamed(Fixture):
    """A passing gate says where its per-test results are, in its log and `qk land --wait`'s output, and copies
    none of them there (#266): a session once searched the queue's checkout to confirm its new tests had run."""
    SUITE = "suites/ios-tests/simulator-tests"
    DETAIL = "test_new_case passed on the iPhone"

    @contextlib.contextmanager
    def farm_mac(self, *results):
        """The gate ran on fake-mac, by a dispatch's folder as qk run files it once the lane has ended, read by the
        real remote.dispatch, with the files results names sent back under its results/."""
        real = mq.remote.dispatch
        def dispatch(root, name, since):
            ran = Path(root) / ".build/queuekeeper/remote" / mq.remote.DISPATCHES / name \
                / (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-1")
            ran.mkdir(parents=True, exist_ok=True)
            (ran / "source.json").write_text(json.dumps({"host": "fake-mac"}))
            (ran / "run.log").write_text("what the lane printed there\n")
            for result in results:
                (ran / "results" / result).parent.mkdir(parents=True, exist_ok=True)
                (ran / "results" / result).write_text(self.DETAIL + "\n")
            return real(root, name, since)
        # While the lane runs there is no dispatch: only the read after it ends finds one, as for a real run.
        with patch.object(mq.remote, "dispatch", side_effect=dispatch), \
                patch.object(mq.remote, "dispatched", return_value=None):
            yield

    def landed(self, context=None):
        """(the landed entry, its gate log's text, what `qk land --wait` prints for it, what the worker said)."""
        self.commit("a", "a")
        [identifier] = self.add("a")
        with context or contextlib.nullcontext(), contextlib.redirect_stdout(io.StringIO()) as worker:
            self.assertEqual(self.work(), 0)
        entry = self.state(identifier)
        self.assertEqual(entry["state"], "landed")
        self.assertEqual(self.gates(), 1)
        logs = sorted(mq.Queue(self.repo).logs.glob("*-gate.log"))
        self.assertEqual(len(logs), 1)
        with contextlib.redirect_stdout(io.StringIO()) as waited:
            self.assertEqual(mq.wait(self.repo, identifier, 1), 0)
        return entry, logs[0].read_text(), waited.getvalue(), worker.getvalue()

    def assert_waited_ends(self, waited, entry):
        """`qk land --wait` ends with the line naming the results, the gate's log, and the landed commit, last."""
        self.assertEqual(waited.splitlines()[-3:], [entry["gate"]["results"], f"gate log: {entry['gate']['log']}",
                                                    f"landed: {entry['landedAs']}"])

    def test_a_passing_farm_mac_gate_names_its_results_and_says_they_are_on_this_mac(self):
        entry, log, waited, worker = self.landed(self.farm_mac(f"{self.SUITE}/iPhone.log", f"{self.SUITE}/iPad.log",
                                                                 "suites/mac-tests/unit.log"))
        line = entry["gate"]["results"]
        self.assertIn("gate per-test results: 3 files", line)
        self.assertIn(f"{self.SUITE}: 2", line)
        self.assertIn("suites/mac-tests: 1", line)
        self.assertIn("on this Mac, sent back by fake-mac", line)
        # The named folder is the dispatch's own, and it holds the per-simulator logs.
        folder = Path(line.split(") in ", 1)[1].split(",", 1)[0])
        self.assertEqual(folder.parent.parent.name, "gate")
        self.assertTrue((folder / self.SUITE / "iPhone.log").is_file())
        self.assertIn(f"{line}\n", log)
        self.assertIn(line, worker)
        self.assertNotIn(self.DETAIL, log)  # named, not copied
        self.assertIn("what the lane printed there", log)  # the dispatch's run.log, which the worker does copy
        self.assert_waited_ends(waited, entry)
        self.assertTrue(Path(entry["gate"]["log"]).is_file())
        self.assertNotIn(self.DETAIL, waited)

    def test_a_passing_farm_mac_gate_with_no_results_says_its_command_wrote_none(self):
        # The negative control: no files came back, so the line claims none, and the entry still lands. A lane whose
        # command writes none, as Queuekeeper's own `qk unittest` gate, gets no results/ back at all, and the line
        # says the command wrote none rather than that another Mac failed to send them (#38), naming no folder
        # that is not there.
        entry, log, waited, _ = self.landed(self.farm_mac())
        line = entry["gate"]["results"]
        self.assertEqual(line, "gate per-test results: none: its command wrote none in its results/ on "
                               "fake-mac, which sends back all it writes there")
        self.assertNotIn("sent none back", line)
        self.assertIn(f"{line}\n", log)
        self.assert_waited_ends(waited, entry)

    def test_a_gate_run_here_that_wrote_results_names_them_as_a_dispatched_one_does(self):
        # The same gap: nothing named them here either, and the next run here replaces them.
        config = self.config()
        config["lanes"]["gate"]["command"] = [
            "sh", "-c", f'mkdir -p "$QUEUEKEEPER_RESULTS_DIR/{self.SUITE}" && echo "{self.DETAIL}" > '
                        f'"$QUEUEKEEPER_RESULTS_DIR/{self.SUITE}/mac.log" && printf x >> "{self.counter}"']
        self.publish(config)
        entry, log, waited, _ = self.landed()
        line = entry["gate"]["results"]
        self.assertIn(f"gate per-test results: 1 file ({self.SUITE}: 1) in ", line)
        self.assertIn(".build/queuekeeper/gate/results, on this Mac; its next run here replaces them", line)
        self.assertNotIn("second Mac", line)
        self.assertTrue((Path(line.split(") in ", 1)[1].split(",", 1)[0]) / self.SUITE / "mac.log").is_file())
        self.assertIn(f"{line}\n", log)
        self.assertNotIn(self.DETAIL, log)
        self.assert_waited_ends(waited, entry)

    def test_a_gate_run_here_that_wrote_no_results_prints_what_it_did(self):
        entry, log, waited, worker = self.landed()
        self.assertGreater(entry["gate"]["seconds"], 0)
        self.assertNotIn("results", entry["gate"])
        for said in (log, waited, worker): self.assertNotIn("per-test results", said)
        self.assertEqual(waited, f"{mq.describe(entry)}\nlanded: {entry['landedAs']}\n")

    def test_results_an_earlier_run_left_here_are_not_named(self):
        # A lane that ended here without writing its results leaves the last run's folder: not this gate's.
        root = self.base / "checkout"
        results = root / ".build/queuekeeper/gate/results"
        (results / "suites").mkdir(parents=True)
        (results / "suites/old.log").write_text("old\n")
        old = time.time() - 60
        os.utime(results, (old, old))
        runner = types.SimpleNamespace(checkout=root, ran_on=None)
        self.assertIsNone(mq.Worker.results_named(runner, "gate", None, old + 30))
        self.assertIn("1 file (suites: 1)", mq.Worker.results_named(runner, "gate", None, old - 30))

    def test_a_results_folder_with_many_folders_names_only_the_fullest(self):
        results = self.base / "results"
        for number in range(mq.RESULT_FOLDERS_NAMED + 3):
            (results / f"suite-{number:02d}").mkdir(parents=True)
            for index in range(number + 1): (results / f"suite-{number:02d}" / f"{index}.log").write_text("x")
        said = mq.result_files(results)
        self.assertIn("66 files (suite-10: 11, ", said)
        self.assertIn(", and 3 more folders)", said)
        self.assertNotIn("suite-00", said)
        self.assertEqual(mq.result_files(self.base / "absent"), "")


class FailedGateEvidence(Fixture):
    FIRST = "test_sample.Sample.test_first"
    SECOND = "test_sample.Sample.test_second"
    SUMMARY = (f"queuekeeper unittest: 2 tests (0 skipped) in 2 processes, 1 at a time, 0.1s: FAILED: "
               f"{FIRST} (FAILED (failures=1), status 1), {SECOND} (FAILED (failures=1), status 1)")

    def failed(self, context):
        self.status.write_text("1")
        self.commit("a", "a")
        [identifier] = self.add("a")
        with context, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.work(), 0)
        entry = self.state(identifier)
        self.assertEqual(entry["state"], mq.FAILED)
        return entry

    def wait_output(self, entry):
        with contextlib.redirect_stdout(io.StringIO()) as waited:
            self.assertEqual(mq.wait(self.repo, entry["id"], 1), 1)
        return waited.getvalue()

    def test_a_failed_remote_gate_keeps_its_assertions_and_dispatch_paths_in_the_wait(self):
        dispatch = self.base / ".dispatches/gate/retained"
        dispatch.mkdir(parents=True)
        log, output = dispatch / "run.log", dispatch / "output.log"
        log.write_text("== nested.Sample.test_turn: OK (1 tests, 0.1s)\n"
                       "FAIL: test_turn (nested.Sample.test_turn)\nAssertionError: nested mismatch\n" + "=" * 70 + "\n"
                       f"== {self.FIRST}: FAILED (1 tests, 0.1s)\n"
                       f"FAIL: test_first ({self.FIRST})\nAssertionError: first mismatch\n" + "=" * 70 + "\n"
                       f"== {self.SECOND}: FAILED (1 tests, 0.1s)\n"
                       f"FAIL: test_second ({self.SECOND})\nAssertionError: second mismatch\n" + "-" * 70 + "\n"
                       f"Ran 2 tests in 0.1s\n{self.SUMMARY}\n")
        output.write_text("the dispatch's transport output\n")

        def remote_gate(root, lane, began):
            return (dispatch, "fake-mac") if lane == "gate" else (None, None)

        entry = self.failed(patch.object(mq.remote, "dispatch", side_effect=remote_gate))
        waited = self.wait_output(entry)
        gate = Path(entry["log"]).read_text()
        for text in (gate, waited):
            self.assertIn(self.SUMMARY, text)
            self.assertIn("AssertionError: first mismatch", text)
            self.assertIn("AssertionError: second mismatch", text)
            self.assertIn(f"remote dispatch run.log: {log}", text)
            self.assertIn(f"remote dispatch output.log: {output}", text)
        self.assertIn("AssertionError: nested mismatch", gate)
        self.assertNotIn("AssertionError: nested mismatch", waited)
        log.unlink()
        waited = self.wait_output(entry)
        self.assertIn("remote dispatch run.log: unavailable (not returned or pruned)", waited)
        self.assertNotIn(f"remote dispatch run.log: {log}", waited)
        self.assertIn(f"remote dispatch output.log: {output}", waited)
        self.assertIn(self.SUMMARY, waited)
        self.assertIn(entry["log"], waited)

    def assert_swift_failure_evidence(self, *, grouped):
        dispatch = self.base / ".dispatches/gate/swift"
        dispatch.mkdir(parents=True)
        run_log, output = dispatch / "run.log", dispatch / "output.log"
        report = "qk run gate: failed (exit 1), on fake-mac\n  tests: 1 passed, 2 failed, 0 skipped, 0 unmeasured\n"
        first = '✘ Test "Keeps the heading" recorded an issue at LayoutTests.swift:42:9: Expectation failed: width > 0'
        second = "✘ Test wraps() recorded an issue at LayoutTests.swift:64:5: Expectation failed: height > 0"
        failure = ("✘ Test fixture() recorded an issue at LayoutTests.swift:10:5: Expectation failed: fixture only\n"
                   "✔ Test fixture() passed after 0.1 seconds.\n"
                   f"{first}\n{first}\n{second}\n"
                   '✘ Test "Keeps the heading" failed after 0.1 seconds with 1 issue.\n'
                   "✘ Test wraps() failed after 0.1 seconds with 1 issue.\n"
                   "✘ Test run with 3 tests in 1 suite failed after 0.2 seconds with 2 issues.\n")
        output.write_text(report)
        if grouped:
            config = self.config()
            lanes = config["lanes"]
            lanes["swift"] = lanes["other"] = lanes["gate"]
            lanes["gate"] = {"parallel": ["swift", "other"], "resource": "heavy"}
            self.publish(config)
            members = dispatch / "lanes"
            members.mkdir()
            (members / "swift.log").write_text(failure)
            (members / "swift.json").write_text('{"status": 1}')
            (members / "other.log").write_text(failure.replace("width > 0", "passing member fixture"))
            (members / "other.json").write_text('{"status": 0}')
        run_log.write_text("" if grouped else failure)

        run_command = mq.processes.run

        def run_gate(command, **options):
            if command[:3] == ["python3", "scripts/qk", "run"] and command[-1] == "gate":
                options["log_path"].write_text(report)
                return 1
            return run_command(command, **options)

        def remote_gate(root, lane, began):
            return (dispatch, "fake-mac") if lane == "gate" else (None, None)

        with patch.object(mq.processes, "run", run_gate):
            entry = self.failed(patch.object(mq.remote, "dispatch", side_effect=remote_gate))
        waited = self.wait_output(entry)
        for text in (waited, Path(entry["log"]).read_text()):
            self.assertIn(report.strip(), text)
            self.assertIn(first, text)
            self.assertIn(second, text)
            self.assertIn(f"remote dispatch run.log: {run_log}", text)
            self.assertIn(f"remote dispatch output.log: {output}", text)
        self.assertEqual(waited.count(first), 1)
        self.assertNotIn("fixture only", waited)
        self.assertNotIn("passing member fixture", waited)

    def test_a_group_gate_that_fails_only_in_a_simulator_lane_is_named_and_counted_by_its_summary(self):
        # vocaro/queuekeeper#193: BreezEPUB's gate, a group, failed only in smoke-ipad, while its qk unittest lanes
        # passed. The record counted the qk unittest lanes' 250 tests alone, named no failed test, and its message
        # said qk unittest's verdict was OK.
        dispatch = self.base / ".dispatches/gate/group"
        members = dispatch / "lanes"
        members.mkdir(parents=True)
        test = "ReadingPositionAcceptanceTests/testContinuousScrollRestoresAfterClosingAndRelaunching()"
        place = "/p/BreezEPUBUITests/ReadingPositionAcceptanceTests.swift:127"
        unittests = ("queuekeeper unittest: 250 tests (0 skipped) in 4 processes, 2 at a time, 1.0s: OK\n"
                     "Ran 250 tests in 1.000s\n\nOK\n")
        output = (f"{unittests}FAIL smoke-ipad (262s, status 1): {members / 'smoke-ipad.log'}\n"
                  f"iPad-Pro-11-inch-M5-12GB-smoke: failed 60.195s {test}\n"
                  "iPad-Pro-11-inch-M5-12GB-smoke: 1 passed, 1 failed, 0 skipped, 0 expected failure, 0 unmeasured "
                  "(xcodebuild status 65)\n")
        report = (f"{output}qk run gate: failed (exit 1), on fake-mac\n"
                  "  tests: 300 passed, 1 failed, 0 skipped, 0 unmeasured; no test counts from docs\n"
                  "  lanes: 2 passed, 1 failed (smoke-ipad), 0 unmeasured\n"
                  f"  failed: {test} at {place}\n  receipt: {dispatch / 'source.json'}\n")
        config = self.config()
        lanes = config["lanes"]
        lanes["docs"] = lanes["tests"] = lanes["smoke-ipad"] = lanes["gate"]
        lanes["gate"] = {"parallel": ["docs", "tests", "smoke-ipad"], "resource": "heavy"}
        self.publish(config)
        for name, status in (("docs", 0), ("tests", 0), ("smoke-ipad", 1)):
            (members / f"{name}.log").write_text(unittests if name == "tests" else "")
            (members / f"{name}.json").write_text(json.dumps({"status": status}))
        (dispatch / "run.log").write_text(output)
        (dispatch / "output.log").write_text(report)
        records = dispatch / "results/smoke-ipad/failures/simulator-tests/iPad"
        records.mkdir(parents=True)
        file, line = place.rsplit(":", 1)
        assertion = "Expectation failed: restoredOffset == expected"
        (records / "failures.json").write_text(json.dumps({"failedTests": [
            {"test": test, "failures": [{"file": file, "line": int(line),
                                         "message": assertion + "\nrestoredOffset → 0"}]}]}))
        run_command = mq.processes.run

        def run_gate(command, **options):
            if command[:3] == ["python3", "scripts/qk", "run"] and command[-1] == "gate":
                options["log_path"].write_text(report)
                return 1
            return run_command(command, **options)

        def remote_gate(root, lane, began):
            return (dispatch, "fake-mac") if lane == "gate" else (None, None)

        with patch.object(mq.processes, "run", run_gate):
            entry = self.failed(patch.object(mq.remote, "dispatch", side_effect=remote_gate))
        self.assertIn(f"gate's lane smoke-ipad did not pass. {test} at {place} failed.", entry["why"])
        self.assertNotIn("qk unittest's verdict", entry["why"])
        found = agent_results.lookup(mq.Queue(self.repo), entry["id"])
        self.assertEqual(found["tests"], {"passed": 300, "failed": 1, "skipped": 0, "unmeasured": 0, "ran": 301})
        self.assertEqual(found["failure"]["tests"], [{"name": test, "location": place}])
        self.assertEqual(found["failure"]["moreTests"], 0)
        self.assertIn("smoke-ipad", found["failure"]["message"])
        waited = self.wait_output(entry)
        self.assertIn(f"{test} at {place}: {assertion} restoredOffset → 0", waited)
        self.assertIn(f"remote dispatch run.log: {dispatch / 'run.log'}", waited)
        self.assertIn(f"remote dispatch output.log: {dispatch / 'output.log'}", waited)

    def test_a_failed_remote_gate_reports_swift_testing_assertions_in_the_wait(self):
        self.assert_swift_failure_evidence(grouped=False)

    def test_a_failed_remote_group_gate_reports_its_swift_testing_members_assertions(self):
        self.assert_swift_failure_evidence(grouped=True)

    def test_an_outer_report_does_not_repeat_a_nested_fixture_failure(self):
        lane_log = "    FAIL: test_turn (nested.Sample.test_turn)\n    AssertionError: nested mismatch\n"
        report = "qk run gate: failed (exit 1), on fake-mac\n  tests: 2 passed\n  failed: Pages/testTurn\n"
        self.assertEqual(mq.failed_lane_excerpt(lane_log, report, "gate"), report.splitlines()[:2])
        self.assertEqual(mq.failed_lane_excerpt(lane_log, "", "gate"), [])

    def test_the_persisted_excerpt_has_both_line_and_total_bounds(self):
        assertions, statuses = [], []
        for index in range(15):
            method = f"test_{index}"
            name = f"test_sample.Sample.{method}"
            assertions.extend([f"== {name}: FAILED (1 tests, 0.1s)", f"FAIL: {method} ({name})",
                               "AssertionError: " + "x" * 1000, "=" * 70])
            statuses.append(f"{name} (FAILED (failures=1), status 1)")
        assertions.append("queuekeeper unittest: 15 tests (0 skipped) in 15 processes, 1 at a time, 0.1s: FAILED: "
                          + ", ".join(statuses))
        excerpt = mq.failed_lane_excerpt("\n".join(assertions), "", "gate")
        self.assertTrue(excerpt[0].startswith("queuekeeper unittest:"))
        self.assertTrue(excerpt[1].startswith("FAIL: test_0"))
        self.assertTrue(all(len(line) <= mq.FAILURE_LINE_LIMIT for line in excerpt))
        self.assertLessEqual(len("\n".join(excerpt)), mq.FAILURE_EXCERPT_LIMIT)
        self.assertLess(len(excerpt), 16)

    def test_a_local_gate_failure_keeps_the_existing_queue_log_guidance(self):
        entry = self.failed(patch.object(mq.remote, "dispatch", return_value=(None, None)))
        self.assertNotIn("failedDispatch", entry)
        waited = self.wait_output(entry)
        self.assertNotIn("remote dispatch", waited)
        self.assertIn(entry["log"], waited)


# What the module says before a test's patches: how the second gate of a pair is kept off the first gate's Mac, and
# which lanes may go to another Mac.
APART = mq.apart
OFFERABLE = mq.offerable


class FollowsTests(unittest.TestCase):
    def test_the_toolkits_own_queue_and_a_project_whose_main_says_so_follow_their_landings(self):
        worker = object.__new__(mq.Worker)
        worker.queue = SimpleNamespace(main_checkout="checkout")
        with patch.object(mq.Worker, "toolkits_own", return_value=True), \
                patch.object(mq, "config_at", side_effect=AssertionError("its own queue reads no configuration")):
            self.assertTrue(worker.follows())
        with patch.object(mq.Worker, "toolkits_own", return_value=False):
            for config, follows in [({"queue": {"followLandings": True}}, True), ({"queue": {}}, False), ({}, False)]:
                with self.subTest(config), patch.object(mq, "config_at", return_value=config):
                    self.assertIs(worker.follows(), follows)
            with patch.object(mq, "config_at", side_effect=ValueError("no configuration on main")):
                self.assertFalse(worker.follows())


if __name__ == "__main__":
    unittest.main()
