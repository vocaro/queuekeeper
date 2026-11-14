"""`qk pre-push`, the hook that runs the configured lanes for what a push changes (vocaro/queuekeeper#6), against a
throwaway repository. No lane runs: the run is a stand-in."""
import contextlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/python"))
import host_isolation  # noqa: F401 -- first, as every test module
from queuekeeper import cli, merge_queue, pre_push
from queuekeeper.lane_results import UNMEASURED

CONFIG = {"name": "Project", "lanes": {"gate": {"command": ["./check.sh"]}, "docs": {"command": ["./docs.sh"]}},
          "queue": {"spelling": False}}


class PrePushTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)
        identity = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "Test",
                    "GIT_AUTHOR_EMAIL": "test@example.com", "GIT_COMMITTER_NAME": "Test",
                    "GIT_COMMITTER_EMAIL": "test@example.com"}
        without = {name: value for name, value in os.environ.items()
                   if not name.startswith("GIT_") and name not in (pre_push.VARIABLE, pre_push.QUEUE_VARIABLE)}
        self.enterContext(patch.dict(os.environ, {**without, **identity}, clear=True))
        self.git("init", "-q", "-b", "main", str(self.root))
        self.base = self.commit({"src/a.py": "a\n", "README.md": "read\n"})
        self.git("update-ref", "refs/remotes/origin/main", self.base)
        self.ran = []
        self.statuses = {}

    def git(self, *arguments):
        return subprocess.run(["git", "-C", str(self.root), *arguments], check=True, capture_output=True,
                              text=True).stdout.strip()

    def commit(self, files):
        for name, text in files.items():
            (self.root / name).parent.mkdir(parents=True, exist_ok=True)
            (self.root / name).write_text(text)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "Change")
        return self.git("rev-parse", "HEAD")

    def push(self, lines, config=CONFIG, environ=None):
        """(status, what the hook said) for a push whose input is lines."""
        def run(root, lane):
            self.ran.append(lane)
            return self.statuses.get(lane, 0)
        said = io.StringIO()
        with contextlib.redirect_stderr(said):
            status = pre_push.main(self.root, config, ["origin", "url"], io.StringIO("".join(lines)),
                                   environ=environ or {}, run=run)
        return status, said.getvalue()

    def line(self, local, there=pre_push.ZERO, ref="refs/heads/work"):
        return f"{ref} {local} {ref} {there}\n"

    def test_a_push_that_changes_code_runs_the_gate_and_one_of_documents_only_runs_the_docs_lane(self):
        code = self.commit({"src/a.py": "b\n"})
        status, said = self.push([self.line(code)])
        self.assertEqual((status, self.ran), (0, ["gate"]))
        self.assertIn("running gate: it changes code paths (1 path(s))", said)
        docs = self.commit({"README.md": "read more\n"})
        self.ran.clear()
        self.assertEqual(self.push([self.line(docs, code)])[0], 0)
        self.assertEqual(self.ran, ["docs"])
        # A new branch is compared with the remote's main, so the code commit below it counts, and the push runs the
        # docs lane as well as the gate, for the document it changes beside code (vocaro/queuekeeper#122).
        self.ran.clear()
        status, said = self.push([self.line(docs)])
        self.assertEqual(self.ran, ["docs", "gate"])
        self.assertIn("running docs, gate: it changes code paths and documentation paths (2 path(s))", said)

    def test_the_project_s_own_docs_paths_and_queue_lanes_choose(self):
        config = {**CONFIG, "lanes": {**CONFIG["lanes"], "fast": {"command": ["x"]}, "notes": {"command": ["y"]}},
                  "queue": {"spelling": False, "lanes": ["fast", "gate"],
                            "docsOnly": {"paths": {"notes/": "prose"}, "lanes": ["notes"]}}}
        self.assertEqual(pre_push.lanes_for(config, ["notes/a.txt"])[0], ["notes"])
        # A push of documents and code runs the docs lanes too, first, as the queue does (vocaro/queuekeeper#122).
        self.assertEqual(pre_push.lanes_for(config, ["notes/a.txt", "README.md"]),
                         (["notes", "fast", "gate"], "it changes code paths and documentation paths"))
        self.assertEqual(pre_push.lanes_for(config, ["src/a.py", "README.md"])[0], ["fast", "gate"])
        self.assertEqual(pre_push.lanes_for(config, ["README.md"])[0], merge_queue.docs_lanes(config, ["README.md"]) or ["fast", "gate"])

    def test_a_lane_that_fails_or_is_unmeasured_refuses_the_push_and_later_lanes_do_not_run(self):
        code = self.commit({"src/a.py": "b\n"})
        config = {**CONFIG, "queue": {"spelling": False, "lanes": ["docs", "gate"]}}
        self.statuses = {"docs": 7}
        status, said = self.push([self.line(code)], config)
        self.assertEqual((status, self.ran), (1, ["docs"]))
        self.assertIn("BLOCKED: docs failed (exit 7)", said)
        self.statuses = {"gate": UNMEASURED}
        status, said = self.push([self.line(code)], config)
        self.assertEqual(status, UNMEASURED)
        self.assertIn("gate was unmeasured, so nothing was verified", said)

    def test_a_deleted_branch_a_skip_and_the_queue_s_own_push_run_nothing(self):
        code = self.commit({"src/a.py": "b\n"})
        self.assertEqual(self.push([self.line(pre_push.ZERO, code)]), (0, ""))
        status, said = self.push([self.line(code)], environ={pre_push.VARIABLE: "skip"})
        self.assertEqual(status, 0)
        self.assertIn("nothing was verified", said)
        status, said = self.push([self.line(code)], environ={pre_push.QUEUE_VARIABLE: "1"})
        self.assertIn("the merge queue ran the lanes", said)
        self.assertEqual((status, self.ran), (0, []))

    def test_a_project_with_no_gate_says_nothing_was_verified_and_a_checkout_elsewhere_is_said(self):
        code = self.commit({"src/a.py": "b\n"})
        status, said = self.push([self.line(code)], {"name": "Project", "lanes": {}, "queue": {"spelling": False}})
        self.assertEqual(status, 0)
        self.assertIn("no gate lane and no queue.lanes", said)
        self.commit({"src/a.py": "c\n"})
        self.assertIn("which is not the commit pushed", self.push([self.line(code)])[1])

    def test_a_branch_with_no_base_changes_every_file_and_both_sides_of_a_rename_are_paths(self):
        self.git("update-ref", "-d", "refs/remotes/origin/main")
        head = self.git("rev-parse", "HEAD")
        self.assertEqual(pre_push.pushed(self.root, [self.line(head)])[1], ["README.md", "src/a.py"])
        (self.root / "doc").mkdir()
        self.git("mv", "src/a.py", "doc/a.md")
        self.git("commit", "-q", "-m", "Move")
        moved = self.git("rev-parse", "HEAD")
        self.assertEqual(pre_push.pushed(self.root, [self.line(moved, head)]), ([moved], ["doc/a.md", "src/a.py"]))

    def test_the_command_reads_the_project_and_the_hook_s_input(self):
        (self.root / "queuekeeper.json").write_text('{"schemaVersion": 1, "name": "Project", "lanes": {}}')
        with patch.object(sys, "stdin", io.StringIO("")):
            self.assertEqual(cli.main(["--project", str(self.root), "pre-push", "origin", "url"]), 0)


if __name__ == "__main__":
    unittest.main()
