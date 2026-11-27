"""`qk pr check`: a pull request's lanes run on its merge with the base, and each result is posted as a commit status
(vocaro/queuekeeper#375). gh is a fake that records what it is asked; origin is a bare repository on disk."""
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
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src/python"))
import host_isolation  # noqa: F401 -- first, as every test module
from queuekeeper import cli, pull_requests

FAKE_GH = """#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_GH_LOG"
case "$1 $2" in
  "pr view") cat "$FAKE_GH_PR" ;;
  "api --method") exit "${FAKE_GH_POST_STATUS:-0}" ;;
  *) echo "fake gh: unexpected $*" >&2; exit 1 ;;
esac
"""


class PullRequestCheck(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)
        bin_ = self.root / "bin"
        bin_.mkdir()
        host_isolation.executable(bin_ / "gh", FAKE_GH)
        self.log, self.request = self.root / "gh.log", self.root / "pr.json"
        identity = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "Test",
                    "GIT_AUTHOR_EMAIL": "test@example.com", "GIT_COMMITTER_NAME": "Test",
                    "GIT_COMMITTER_EMAIL": "test@example.com"}
        without = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
        self.enterContext(patch.dict(os.environ, {**without, **identity, "PATH": f"{bin_}:{os.environ['PATH']}",
                                                  "FAKE_GH_LOG": str(self.log), "FAKE_GH_PR": str(self.request)},
                                     clear=True))
        self.origin, self.project = self.root / "origin.git", self.root / "project"
        self.git(self.root, "init", "-q", "--bare", "-b", "main", str(self.origin))
        self.git(self.root, "init", "-q", "-b", "main", str(self.project))
        self.git(self.project, "remote", "add", "origin", str(self.origin))
        self.write("queuekeeper.json", json.dumps({"schemaVersion": 1, "name": "x", "integration": "pull-request",
                                                    "lanes": {"gate": {"command": ["true"]},
                                                              "build": {"command": ["true"]}}}))
        self.write("shared", "one\n")
        self.commit("First")
        self.git(self.project, "push", "-q", "origin", "main")
        # The pull request's branch changes a file of its own; main moves on after it forked.
        self.git(self.project, "checkout", "-q", "-b", "feature")
        self.write("feature", "new\n")
        self.head = self.commit("Feature")
        self.git(self.project, "push", "-q", "origin", f"{self.head}:refs/pull/7/head")
        self.git(self.project, "checkout", "-q", "main")
        self.write("later", "main moved\n")
        self.commit("Later")
        self.git(self.project, "push", "-q", "origin", "main")
        self.git(self.project, "checkout", "-q", "feature")
        self.pull_request()
        self.ran = []

    def git(self, directory, *words):
        return subprocess.run(["git", "-C", str(directory), *words], check=True, capture_output=True,
                              text=True).stdout.strip()

    def write(self, name, text):
        (self.project / name).write_text(text)

    def commit(self, message):
        self.git(self.project, "add", "-A")
        self.git(self.project, "commit", "-q", "-m", message)
        return self.git(self.project, "rev-parse", "HEAD")

    def pull_request(self, **fields):
        self.request.write_text(json.dumps({"number": 7, "headRefOid": self.head, "baseRefName": "main",
                                            "url": "https://github.com/o/r/pull/7", "state": "OPEN", **fields}))

    def lane(self, *statuses):
        """Patch the lane runner to exit with statuses in turn, recording each lane and what its tree held."""
        answers = iter(statuses)

        def run(checkout, lane):
            self.ran.append((lane, sorted(path.name for path in checkout.iterdir() if path.name != ".git")))
            return next(answers)
        return patch.object(pull_requests, "run_lane", run)

    def qk(self, *words):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            status = cli.main(["--project", str(self.project), "pr", "check", *words])
        return status, output.getvalue()

    def posted(self):
        """(state, context, description) of each status posted, in order."""
        found = []
        for line in self.log.read_text().splitlines() if self.log.exists() else []:
            if not line.startswith("api --method POST"): continue
            words = dict(part.split("=", 1) for part in line.split(" -f ")[1:])
            self.assertIn(f"/statuses/{self.head}", line)
            found.append((words["state"], words["context"], words["description"]))
        return found

    def test_a_passing_gate_is_posted_pending_then_success_on_the_head(self):
        with self.lane(0): status, said = self.qk("7")
        self.assertEqual(status, 0, said)
        # The lane ran on the merge: the pull request's file and main's later one both.
        self.assertEqual(self.ran, [("gate", ["feature", "later", "queuekeeper.json", "shared"])])
        self.assertEqual([(state, context) for state, context, _ in self.posted()],
                         [("pending", "queuekeeper/gate"), ("success", "queuekeeper/gate")])
        self.assertIn("pr view 7 --json", self.log.read_text())

    def test_each_lane_named_gets_its_own_context_and_a_failure_fails_the_check(self):
        with self.lane(0, 1): status, said = self.qk("--lane", "build", "--lane", "gate")
        self.assertEqual(status, 1, said)
        self.assertEqual([(state, context) for state, context, _ in self.posted()],
                         [("pending", "queuekeeper/build"), ("success", "queuekeeper/build"),
                          ("pending", "queuekeeper/gate"), ("failure", "queuekeeper/gate")])
        self.assertIn("pr view --json", self.log.read_text())

    def test_a_run_with_no_verdict_stays_pending_never_success(self):
        for code in (3, 75, 127, -15):
            with self.subTest(code=code):
                self.log.unlink(missing_ok=True)
                with self.lane(code): status, said = self.qk("7")
                self.assertEqual(status, 75, said)
                self.assertEqual([state for state, _, _ in self.posted()], ["pending", "pending"])
                self.assertIn("no verdict", self.posted()[-1][2])

    def test_a_head_that_conflicts_with_its_base_fails_without_running(self):
        self.git(self.project, "checkout", "-q", "main")
        self.write("feature", "main's own\n")
        self.commit("Conflicting")
        self.git(self.project, "push", "-q", "origin", "main")
        with self.lane(): status, said = self.qk("7")
        self.assertEqual(status, 1, said)
        self.assertEqual(self.ran, [])
        self.assertEqual([(state, context) for state, context, _ in self.posted()], [("failure", "queuekeeper/gate")])
        self.assertIn("conflicts with main", self.posted()[0][2])
        # The kept checkout is left with no merge in progress, ready for the next check.
        checkout = Path(self.git(self.project, "rev-parse", "--path-format=absolute", "--git-common-dir")) \
            / pull_requests.FOLDER / "7"
        self.assertEqual(self.git(checkout, "status", "--porcelain"), "")

    def test_the_kept_checkout_is_reused_and_cleaned_between_checks(self):
        with self.lane(0): self.qk("7")
        checkout = Path(self.git(self.project, "rev-parse", "--path-format=absolute", "--git-common-dir")) \
            / pull_requests.FOLDER / "7"
        (checkout / "stray").write_text("left by a build\n")
        with self.lane(0): status, said = self.qk("7")
        self.assertEqual(status, 0, said)
        self.assertEqual(self.ran[-1], ("gate", ["feature", "later", "queuekeeper.json", "shared"]))

    def test_a_check_already_running_is_not_started_twice(self):
        with self.lane(0): self.qk("7")
        lock = Path(self.git(self.project, "rev-parse", "--path-format=absolute", "--git-common-dir")) \
            / pull_requests.FOLDER / "7.lock"
        with open(lock, "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            with self.lane(): status, said = self.qk("7")
        self.assertEqual(status, 75)
        self.assertIn("being checked already", said)

    def test_a_project_without_a_gate_must_name_its_lanes(self):
        self.write("queuekeeper.json", json.dumps({"schemaVersion": 1, "name": "x", "integration": "pull-request",
                                                    "lanes": {"build": {"command": ["true"]}}}))
        self.head = self.commit("No gate")
        self.git(self.project, "push", "-q", "-f", "origin", f"{self.head}:refs/pull/7/head")
        self.pull_request()
        with self.lane(): status, said = self.qk("7")
        self.assertEqual(status, 1)
        self.assertIn("--lane", said)
        with self.lane(): status, said = self.qk("7", "--lane", "nowhere")
        self.assertIn("no lane nowhere", said)

    def test_a_closed_pull_request_is_not_checked(self):
        self.pull_request(state="MERGED")
        with self.lane(): status, said = self.qk("7")
        self.assertEqual(status, 0, said)
        self.assertEqual(self.posted(), [])

    def test_the_queue_takes_an_open_labeled_pull_request_on_main(self):
        self.pull_request(labels=[{"name": "queue"}], isDraft=False, title="Add the feature", headRefName="feature")
        found = pull_requests.for_queue(self.project, 7, "queue")
        self.assertEqual(found, {"number": 7, "head": self.head, "title": "Add the feature", "headRefName": "feature",
                                 "url": "https://github.com/o/r/pull/7", "label": "queue"})
        self.assertEqual(pull_requests.merge_message(found), "Merge pull request #7 from feature\n\nAdd the feature")
        for fields, said in (({"labels": []}, "has no queue label"),
                             ({"labels": [{"name": "queue"}], "isDraft": True}, "is a draft"),
                             ({"labels": [{"name": "queue"}], "baseRefName": "develop"}, "targets develop"),
                             ({"labels": [{"name": "queue"}], "state": "CLOSED"}, "is closed"),
                             ({"labels": [{"name": "queue"}], "headRefOid": "0" * 40}, "land it again")):
            with self.subTest(said=said):
                self.pull_request(**fields)
                with self.assertRaisesRegex(ValueError, said): pull_requests.for_queue(self.project, 7, "queue")
        # Another label name, as pullRequests.label gives.
        self.pull_request(labels=[{"name": "ship-it"}])
        self.assertEqual(pull_requests.for_queue(self.project, 7, "ship-it")["label"], "ship-it")

    def test_the_queue_rechecks_a_pull_request_before_pushing_it(self):
        request = {"number": 7, "label": "queue"}
        self.pull_request(labels=[{"name": "queue"}])
        self.assertEqual(pull_requests.recheck(self.project, request, self.head), (True, None))
        for fields, said in (({"labels": []}, "taken off"), ({"labels": [{"name": "queue"}], "state": "CLOSED"}, "closed"),
                             ({"labels": [{"name": "queue"}], "headRefOid": "1" * 40}, "pushed again")):
            with self.subTest(said=said):
                self.pull_request(**fields)
                approved, why = pull_requests.recheck(self.project, request, self.head)
                self.assertIs(approved, False)
                self.assertIn(said, why)
        self.request.write_text("not json")
        self.assertIsNone(pull_requests.recheck(self.project, request, self.head)[0])

    def test_a_head_origin_does_not_have_says_to_check_again(self):
        self.pull_request(headRefOid="0" * 40)
        with self.lane(): status, said = self.qk("7")
        self.assertEqual(status, 1)
        self.assertIn("check again", said)


if __name__ == "__main__":
    unittest.main()
