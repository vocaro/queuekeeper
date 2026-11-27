"""A pull-request-queue project's merge queue: a labeled pull request lands on main as a merge commit whose second
parent is its head, so GitHub marks it merged (vocaro/queuekeeper#376). gh is not asked here: qk land --pr's lookup
(pull_requests.for_queue) is test_pull_requests's, and these tests give add what it returns."""
import argparse
import contextlib
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src/python"))
import host_isolation  # noqa: F401 -- first, as every test module
from queuekeeper import merge_queue as mq, pull_requests
from test_support.queue import Fixture, run


class PullRequestProject(Fixture):
    """A pull-request-queue project, and pull requests pushed as GitHub keeps them."""

    def setUp(self):
        super().setUp()
        config = self.config()
        config["integration"] = "pull-request-queue"
        self.publish(config, "Land labeled pull requests through the queue")
        # GitHub still approves each pull request when its batch passes, unless a test says otherwise; gh is never
        # asked for real here.
        approved = patch.object(pull_requests, "recheck", return_value=(True, None))
        approved.start()
        self.addCleanup(approved.stop)
        # Each queuekeeper/gate status the queue would post on a pull request's head: (head, state, description).
        self.statuses = []
        posted = patch.object(pull_requests, "post", lambda root, sha, lane, state, description:
                              self.statuses.append((sha, f"queuekeeper/{lane}", state, description)))
        posted.start()
        self.addCleanup(posted.stop)

    def pull(self, number, name, start="origin/main", content="x"):
        """A pull request's head, pushed as GitHub keeps it, and what qk land --pr would queue it with."""
        head = self.commit(name, f"feature-{number}", content=content, start=start)
        run(self.repo, "git", "push", "-q", "-f", "origin", f"{head}:refs/pull/{number}/head")
        return {"number": number, "head": head, "title": f"Add {name}", "headRefName": f"feature-{number}",
                "url": f"https://github.com/o/r/pull/{number}", "label": "queue"}

    def queue(self, request, checked=True):
        unchecked = patch.object(mq, "check") if not checked else contextlib.nullcontext()
        with patch.object(mq.Queue, "start"), patch("builtins.print"), unchecked:
            mq.add(self.repo, request["head"], pull_request=request)
        return next(entry["id"] for entry in reversed(mq.Queue(self.repo).all())
                    if entry["branch"] == f"pull/{request['number']}")

    def parents(self, commit):
        return run(self.repo, "git", "rev-list", "--parents", "-n", "1", commit).split()[1:]


class PullRequestQueue(PullRequestProject):
    def test_a_labeled_pull_request_lands_as_a_merge_of_its_head(self):
        request = self.pull(7, "feature.txt")
        before = run(self.repo, "git", "rev-parse", "origin/main")
        identifier = self.queue(request)
        with contextlib.redirect_stdout(io.StringIO()): self.assertEqual(self.work(), 0)
        entry = self.state(identifier)
        self.assertEqual(entry["state"], "landed")
        self.assertEqual(entry["pullRequest"]["number"], 7)
        main = run(self.repo, "git", "rev-parse", "origin/main")
        # main is the merge itself: first parent the main it was tested on, second the pull request's head.
        self.assertEqual(self.parents(main), [before, request["head"]])
        self.assertEqual(entry["landedAs"], main)
        self.assertIn("feature.txt", self.main_files())
        self.assertTrue(run(self.repo, "git", "log", "-1", "--format=%s", main).startswith(
            "Merge pull request #7 from feature-7"))

    def test_the_pull_request_shows_where_its_entry_stands(self):
        """queued, gated, then landed, as its head's queuekeeper/gate status (vocaro/queuekeeper#394)."""
        request = self.pull(7, "feature.txt")
        identifier = self.queue(request)
        with contextlib.redirect_stdout(io.StringIO()): self.work()
        landed = self.state(identifier)["landedAs"]
        self.assertEqual([(sha, context, state) for sha, context, state, _ in self.statuses],
                         [(request["head"], "queuekeeper/gate", "pending")] * 2
                         + [(request["head"], "queuekeeper/gate", "success")])
        self.assertEqual(self.statuses[-1][3], f"landed on main as {landed[:12]}")

    def test_a_pull_request_taken_out_or_sent_back_says_so_on_itself(self):
        request = self.pull(7, "feature.txt")
        identifier = self.queue(request)
        why = "pull request 7's queue label was taken off while it waited in the queue"
        with patch.object(pull_requests, "recheck", return_value=(False, why)), \
                contextlib.redirect_stdout(io.StringIO()):
            self.work()
        self.assertEqual(self.statuses[-1][2:], ("error", f"taken out of the merge queue: {why}"))
        conflicting = self.pull(8, "shared.txt", content="theirs\n")
        self.commit("shared.txt", "main-change", content="ours\n")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        self.queue(conflicting, checked=False)
        with contextlib.redirect_stdout(io.StringIO()): self.work()
        self.assertEqual(self.statuses[-1][0], conflicting["head"])
        self.assertEqual(self.statuses[-1][2], "failure")
        self.assertIn("sent it back", self.statuses[-1][3])

    def test_entries_of_branches_post_nothing(self):
        self.statuses.clear()
        config = self.config()
        config["integration"] = "direct-main"
        self.publish(config, "Land branches again")
        self.commit("a", "a")
        with patch.object(mq.Queue, "start"), patch("builtins.print"): mq.add(self.repo, "a")
        with contextlib.redirect_stdout(io.StringIO()): self.work()
        self.assertEqual(self.statuses, [])

    def test_pull_requests_batched_together_each_land_as_their_own_merge(self):
        first, second = self.pull(7, "one.txt"), self.pull(8, "two.txt")
        ids = [self.queue(first), self.queue(second)]
        with contextlib.redirect_stdout(io.StringIO()): self.assertEqual(self.work(), 0)
        self.assertEqual(self.gates(), 1)
        landed = [self.state(identifier)["landedAs"] for identifier in ids]
        self.assertEqual([self.parents(commit)[1] for commit in landed], [first["head"], second["head"]])
        self.assertEqual(run(self.repo, "git", "rev-parse", "origin/main"), landed[1])
        self.assertEqual(self.parents(landed[1])[0], landed[0])

    def test_a_pull_request_unlabeled_while_gated_is_taken_out_and_the_rest_lands(self):
        first, second = self.pull(7, "one.txt"), self.pull(8, "two.txt")
        ids = [self.queue(first), self.queue(second)]
        why = "pull request 7's queue label was taken off while it waited in the queue"

        def recheck(root, pull_request, head):
            return (False, why) if pull_request["number"] == 7 else (True, None)
        with patch.object(pull_requests, "recheck", recheck), contextlib.redirect_stdout(io.StringIO()):
            self.work()
        self.assertEqual((self.state(ids[0])["state"], self.state(ids[0])["removedWhy"]), ("removed", why))
        # The other was gated again without it, and landed alone.
        self.assertEqual(self.state(ids[1])["state"], "landed")
        self.assertEqual(self.gates(), 2)
        self.assertEqual({"one.txt", "two.txt"} & self.main_files(), {"two.txt"})

    def test_nothing_is_pushed_while_gh_cannot_say_whether_a_pull_request_is_still_labeled(self):
        identifier = self.queue(self.pull(7, "feature.txt"))
        before = run(self.repo, "git", "rev-parse", "origin/main")
        slept = []
        with patch.object(pull_requests, "recheck", return_value=(None, "gh could not say")), \
                patch.object(mq, "pause", slept.append), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.work(), 1)
        # It tried the round again as a cause that clears by itself does, then held, having pushed nothing.
        self.assertEqual(slept, list(mq.CLEARING_WAITS))
        run(self.repo, "git", "fetch", "-q", "origin")
        self.assertEqual(run(self.repo, "git", "rev-parse", "origin/main"), before)
        self.assertEqual(self.state(identifier)["state"], "queued")
        self.assertIn("gh could not say", mq.Queue(self.repo).held())

    def test_a_pull_request_that_conflicts_is_sent_back_and_main_is_untouched(self):
        request = self.pull(7, "shared.txt", content="theirs\n")
        self.commit("shared.txt", "main-change", content="ours\n")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        before = run(self.repo, "git", "rev-parse", "origin/main")
        identifier = self.queue(request, checked=False)
        with contextlib.redirect_stdout(io.StringIO()): self.work()
        entry = self.state(identifier)
        self.assertEqual(entry["state"], "conflict")
        self.assertIn("shared.txt", entry.get("failure", {}).get("message", "") or str(entry))
        run(self.repo, "git", "fetch", "-q", "origin")
        self.assertEqual(run(self.repo, "git", "rev-parse", "origin/main"), before)

    def test_a_branch_is_refused_and_pointed_to_a_labeled_pull_request(self):
        self.commit("a", "a")
        with patch("builtins.print"), self.assertRaisesRegex(ValueError, r"qk land --pr NUMBER"):
            mq.add(self.repo, "a")

    def test_a_pull_request_with_a_merge_in_it_is_refused(self):
        request = self.pull(7, "feature.txt")
        self.commit("later.txt", "main-later")
        run(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        run(self.repo, "git", "checkout", "-q", "feature-7")
        run(self.repo, "git", "merge", "-q", "--no-edit", "origin/main")
        request["head"] = run(self.repo, "git", "rev-parse", "HEAD")
        run(self.repo, "git", "push", "-q", "-f", "origin", f"{request['head']}:refs/pull/7/head")
        with patch("builtins.print"), self.assertRaisesRegex(ValueError, "rebase its branch onto origin/main"):
            mq.add(self.repo, request["head"], pull_request=request)
        self.assertEqual(mq.Queue(self.repo).all(), [])

    def test_landing_the_same_pull_request_again_replaces_its_entry(self):
        first = self.queue(self.pull(7, "feature.txt"))
        again = self.queue(self.pull(7, "feature.txt", content="pushed again"))
        self.assertNotEqual(first, again)
        self.assertEqual(self.state(first)["state"], "removed")

    def test_qk_land_pr_asks_for_the_label_from_main_and_queues_the_head(self):
        request = self.pull(7, "feature.txt")
        parser = argparse.ArgumentParser()
        mq.land_arguments(parser)
        asked = []

        def for_queue(root, number, label):
            asked.append((number, label))
            return request
        with patch.object(pull_requests, "for_queue", for_queue), patch.object(mq.Queue, "start"), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(mq.land(self.repo, parser.parse_args(["--pr", "7"])), 0)
        self.assertEqual(asked, [(7, "queue")])
        [entry] = mq.Queue(self.repo).all()
        self.assertEqual((entry["tip"], entry["branch"]), (request["head"], "pull/7"))
        with contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaisesRegex(ValueError, "takes no commit"):
            mq.land(self.repo, parser.parse_args(["--pr", "7", "HEAD"]))


class LabeledPullRequestsQueued(PullRequestProject):
    """The queue's launcher, and its worker after each round, queue what a person labeled (queue_labeled)."""

    def scan(self, *found, refused=()):
        """queue_labeled with gh listing found, each as for_queue gives it unless its number is in refused. Returns
        (status, the numbers for_queue was asked for, what it said)."""
        asked, said = [], []

        def for_queue(root, number, label):
            asked.append(number)
            if number in refused: raise ValueError("it holds a merge")
            return next(request for request in found if request["number"] == number)
        with patch.object(pull_requests, "labeled", return_value=list(found)), \
                patch.object(pull_requests, "for_queue", for_queue), patch.object(mq.Queue, "start"), \
                contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            status = mq.queue_labeled(self.repo, say=said.append)
        return status, asked, said

    def test_a_labeled_pull_request_is_queued_once_at_its_head(self):
        request = self.pull(7, "feature.txt")
        status, asked, said = self.scan(request)
        self.assertEqual((status, asked), (0, [7]))
        [entry] = mq.Queue(self.repo).all()
        self.assertEqual((entry["branch"], entry["tip"]), ("pull/7", request["head"]))
        self.assertIn("queued pull request 7", said[0])
        # Already queued at that head: not asked again. Pushed again: queued again, replacing it.
        self.assertEqual(self.scan(request)[1], [])
        moved = self.pull(7, "feature.txt", content="pushed again")
        self.assertEqual(self.scan(moved)[1], [7])
        self.assertEqual([entry["state"] for entry in mq.Queue(self.repo).all()], ["removed", "queued"])

    def test_a_refused_pull_request_is_not_tried_again_until_it_moves(self):
        request = self.pull(7, "feature.txt")
        status, asked, said = self.scan(request, refused={7})
        self.assertEqual((status, asked), (0, [7]))
        self.assertIn("was not queued", said[0])
        status, asked, said = self.scan(request, refused={7})
        self.assertEqual(asked, [])
        # Said as skipped, with why, rather than as nothing waiting (vocaro/queuekeeper#392).
        with patch.object(pull_requests, "labeled", return_value=[request]), patch.object(mq.Queue, "start"):
            told = []
            mq.queue_labeled(self.repo, say=told.append, quiet=False)
        self.assertEqual(told, ["no pull request labeled queue is waiting to be queued; skipped until pushed again: "
                                "#7 (it holds a merge)"])
        # And the pull request says why on itself, once.
        self.assertEqual([state for _, _, state, _ in self.statuses], ["error"])
        self.assertIn("not queued: it holds a merge", self.statuses[0][3])
        self.assertEqual(self.scan(self.pull(7, "feature.txt", content="rebased"), refused={7})[1], [7])

    def test_an_entry_removed_at_its_head_is_queued_again_while_labeled(self):
        request = self.pull(7, "feature.txt")
        self.scan(request)
        [entry] = mq.Queue(self.repo).all()
        with contextlib.redirect_stdout(io.StringIO()): mq.remove(mq.Queue(self.repo), entry["id"])
        self.assertEqual(self.scan(request)[1], [7])

    def test_the_launchers_look_queues_and_lands_a_labeled_pull_request(self):
        request = self.pull(7, "feature.txt")
        with patch.object(pull_requests, "labeled", return_value=[request]), \
                patch.object(pull_requests, "for_queue", return_value=request), patch.object(mq.Queue, "start"), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(mq.run(self.repo, if_waiting=True), 0)
        [entry] = mq.Queue(self.repo).all()
        self.assertEqual(entry["state"], "landed")
        self.assertEqual(self.parents(entry["landedAs"])[1], request["head"])

    def test_waiting_on_landed_pull_requests_says_github_marks_them_merged(self):
        request = self.pull(7, "feature.txt")
        identifier = self.queue(request)
        with contextlib.redirect_stdout(io.StringIO()): self.work()
        with contextlib.redirect_stdout(io.StringIO()) as said:
            self.assertEqual(mq.wait_for(self.repo, [identifier], 5), 0)
        self.assertIn("all landed: GitHub marks each pull request merged", said.getvalue())
        self.assertNotIn("`qk land` again", said.getvalue())

    def test_the_after_round_look_runs_while_the_worker_holds_its_lock(self):
        held = []
        request = self.pull(7, "feature.txt")
        self.queue(request)
        # What `qk queue status` reads as a running worker (Queue.working), asked from another Queue as status does.
        with patch.object(mq, "scan_labeled", lambda root: held.append(mq.Queue(self.repo).working())), \
                contextlib.redirect_stdout(io.StringIO()):
            self.work()
        self.assertTrue(held and all(held), held)

    def test_gh_that_cannot_list_is_said_and_queues_nothing(self):
        said = []
        with patch.object(pull_requests, "labeled", side_effect=ValueError("gh is not signed in")):
            self.assertEqual(mq.queue_labeled(self.repo, say=said.append), 1)
        self.assertIn("gh is not signed in", said[0])
        self.assertEqual(mq.Queue(self.repo).all(), [])


class DirectMainProject(Fixture):
    def test_another_projects_look_asks_github_nothing(self):
        with patch.object(pull_requests, "labeled") as listed:
            self.assertEqual(mq.queue_labeled(self.repo, say=lambda words: None), 0)
            with contextlib.redirect_stdout(io.StringIO()): mq.run(self.repo, if_waiting=True)
        listed.assert_not_called()


    def test_qk_land_pr_is_refused_where_the_project_does_not_queue_pull_requests(self):
        parser = argparse.ArgumentParser()
        mq.land_arguments(parser)
        with patch.object(pull_requests, "for_queue") as asked, contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaisesRegex(ValueError, "pull-request-queue"):
            mq.land(self.repo, parser.parse_args(["--pr", "7"]))
        asked.assert_not_called()


if __name__ == "__main__":
    unittest.main()
