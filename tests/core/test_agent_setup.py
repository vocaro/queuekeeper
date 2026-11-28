"""`qk agents`: the rule in a project's agent instructions and Claude Code's hooks, written, rewritten and undone without
touching anyone else's."""
import contextlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src/python"))
import host_isolation  # noqa: F401 -- first, as every test module
from queuekeeper import agent_setup, cli

ROOT = Path(__file__).resolve().parents[2]


class Agents(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)
        without = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
        self.enterContext(patch.dict(os.environ, {**without, "GIT_CONFIG_GLOBAL": os.devnull,
                                                  "GIT_CONFIG_NOSYSTEM": "1"}, clear=True))
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.root)], check=True)
        (self.root / "nested").mkdir()
        self.settings = self.root / ".claude" / "settings.json"

    def qk(self, *words):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            status = cli.main(["--project", str(self.root / "nested"), "agents", *words])
        return status, output.getvalue()

    def test_setup_and_remove_round_trip_leaves_the_project_as_it_was(self):
        status, said = self.qk("setup", "--guard")
        self.assertEqual(status, 0, said)
        text = (self.root / "AGENTS.md").read_text()
        self.assertIn(agent_setup.RULE, text)
        settings = json.loads(self.settings.read_text())
        self.assertEqual(settings["hooks"]["SessionStart"][0]["hooks"][0]["command"], "qk hook session-start")
        self.assertEqual(settings["hooks"]["PreToolUse"][0]["matcher"], "Bash")
        self.assertEqual(self.qk("check")[0], 0)
        status, said = self.qk("remove")
        self.assertEqual(status, 0, said)
        self.assertFalse((self.root / "AGENTS.md").exists())
        self.assertFalse(self.settings.exists())
        self.assertEqual(self.qk("check")[0], 1)

    def test_others_text_and_hooks_survive_setup_rerun_and_remove(self):
        (self.root / "CLAUDE.md").write_text("# Project\n\nOur own rules.\n")
        self.settings.parent.mkdir()
        theirs = {"model": "x", "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "make hello"}]}],
                                          "Stop": [{"hooks": [{"type": "command", "command": "say done"}]}]}}
        self.settings.write_text(json.dumps(theirs))
        self.assertEqual(self.qk("setup", "--launcher", "python3 scripts/qk")[0], 0)
        self.assertEqual(self.qk("setup", "--launcher", "python3 scripts/qk")[0], 0)
        text = (self.root / "CLAUDE.md").read_text()
        self.assertTrue(text.startswith("# Project\n\nOur own rules.\n\n<!-- queuekeeper:agents begin"))
        self.assertEqual(text.count("queuekeeper:agents begin"), 1)
        settings = json.loads(self.settings.read_text())
        commands = [hook["command"] for group in settings["hooks"]["SessionStart"] for hook in group["hooks"]]
        self.assertEqual(commands, ["make hello", 'python3 "$CLAUDE_PROJECT_DIR"/scripts/qk hook session-start'])
        self.assertNotIn("PreToolUse", settings["hooks"])
        self.assertEqual(self.qk("remove")[0], 0)
        self.assertEqual((self.root / "CLAUDE.md").read_text(), "# Project\n\nOur own rules.\n")
        self.assertEqual(json.loads(self.settings.read_text()), theirs)

    def test_an_edited_block_is_neither_rewritten_nor_removed_without_force(self):
        self.qk("setup")
        path = self.root / "AGENTS.md"
        path.write_text(path.read_text().replace("Never push to main.", "Never, ever push to main."))
        status, said = self.qk("remove")
        self.assertEqual(status, 1)
        self.assertIn("edited", said)
        self.assertIn("Never, ever", path.read_text())
        self.assertEqual(self.qk("setup")[0], 1)
        self.assertIn("Never, ever", path.read_text())
        self.assertEqual(self.qk("setup", "--force")[0], 0)
        self.assertNotIn("Never, ever", path.read_text())
        self.assertEqual(self.qk("remove")[0], 0)

    def test_a_block_of_an_earlier_rule_is_brought_up_to_date(self):
        path = self.root / "AGENTS.md"
        older = "Never push to main.\n"
        path.write_text("Intro.\n\n" + agent_setup.BEGIN.format(agent_setup.digest(older)) + "\n" + older
                        + agent_setup.END + "\n\nOutro.\n")
        self.assertIn("another version", self.qk("check")[1])
        self.assertEqual(self.qk("setup")[0], 0)
        self.assertEqual(path.read_text(), "Intro.\n\n" + agent_setup.block() + "\nOutro.\n")

    def test_a_relative_launcher_is_anchored_on_the_project(self):
        (self.root / "scripts").mkdir()
        (self.root / "scripts/qk").write_text("")
        (self.root / "qk.py").write_text("")
        self.assertEqual(agent_setup.anchored("qk", self.root), "qk")
        self.assertEqual(agent_setup.anchored("/opt/bin/qk", self.root), "/opt/bin/qk")
        self.assertEqual(agent_setup.anchored("python3 ./scripts/qk", self.root),
                         'python3 "$CLAUDE_PROJECT_DIR"/scripts/qk')
        self.assertEqual(agent_setup.anchored("python3 qk.py", self.root), 'python3 "$CLAUDE_PROJECT_DIR"/qk.py')
        self.assertEqual(agent_setup.anchored("tools/my\\ qk", self.root), "\"$CLAUDE_PROJECT_DIR\"/'tools/my qk'")
        self.assertEqual(self.qk("setup", "--guard", "--launcher", "python3 scripts/qk")[0], 0)
        settings = json.loads(self.settings.read_text())
        self.assertEqual(settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"],
                         'python3 "$CLAUDE_PROJECT_DIR"/scripts/qk hook pre-tool-use')
        self.assertEqual(self.qk("check")[0], 0)

    def test_a_hook_written_with_a_relative_launcher_is_caught_anchored_and_removable(self):
        self.qk("setup")
        older = {"hooks": {event: [{**({"matcher": matcher} if matcher else {}), "hooks": [
            {"type": "command", "command": f"python3 scripts/qk hook {name}", "timeout": 15}]}]
            for name, (event, matcher) in agent_setup.EVENTS.items()}}
        self.settings.write_text(json.dumps(older))
        status, said = self.qk("check")
        self.assertEqual(status, 1)
        self.assertIn("relative to the session's directory", said)
        self.assertEqual(self.qk("remove")[0], 0)
        self.assertFalse(self.settings.exists())
        self.qk("setup")
        self.settings.write_text(json.dumps(older))
        status, said = self.qk("setup", "--launcher", "python3 scripts/qk")
        self.assertEqual(status, 0, said)
        self.assertIn("anchored", said)
        commands = [hook["command"] for groups in json.loads(self.settings.read_text())["hooks"].values()
                    for group in groups for hook in group["hooks"]]
        self.assertEqual(commands, ['python3 "$CLAUDE_PROJECT_DIR"/scripts/qk hook session-start',
                                    'python3 "$CLAUDE_PROJECT_DIR"/scripts/qk hook pre-tool-use'])
        self.assertEqual(self.qk("check")[0], 0)
        self.assertEqual(self.qk("remove")[0], 0)
        self.assertFalse(self.settings.exists())

    def test_settings_that_are_not_json_are_left_as_they_are(self):
        self.settings.parent.mkdir()
        self.settings.write_text("{not json")
        status, said = self.qk("setup")
        self.assertEqual(status, 2)
        self.assertEqual(self.settings.read_text(), "{not json")

    def integrate(self, mode):
        (self.root / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "x", "integration": mode}))

    def test_a_pull_request_project_gets_the_pull_request_rule(self):
        self.integrate("pull-request")
        status, said = self.qk("setup", "--guard")
        self.assertEqual(status, 0, said)
        self.assertIn("integrating through pull requests", said)
        self.assertIn("merge them through a pull request", said)
        self.assertNotIn("qk land", said)
        text = (self.root / "AGENTS.md").read_text()
        self.assertIn(agent_setup.PULL_REQUEST_RULE, text)
        self.assertNotIn("qk land", text)
        self.assertEqual(self.qk("check")[0], 0)
        self.assertEqual(self.qk("remove")[0], 0)
        self.assertFalse((self.root / "AGENTS.md").exists())

    def test_changing_the_integration_is_caught_and_the_rule_rewritten(self):
        self.qk("setup")
        self.integrate("pull-request")
        status, said = self.qk("check")
        self.assertEqual(status, 1)
        self.assertIn("rule for landing through the merge queue, but the project is integrating through pull", said)
        self.assertEqual(len(agent_setup.doctor_warnings(self.root, {"integration": "pull-request"})), 1)
        self.assertEqual(agent_setup.doctor_warnings(self.root, {}), [])
        status, said = self.qk("setup")
        self.assertEqual(status, 0, said)
        self.assertIn("which was the rule for landing through the merge queue", said)
        self.assertIn(agent_setup.PULL_REQUEST_RULE, (self.root / "AGENTS.md").read_text())
        self.assertEqual(self.qk("check")[0], 0)
        self.assertEqual(agent_setup.doctor_warnings(self.root, {"integration": "pull-request"}), [])
        # And back: a project whose queuekeeper.json no longer says pull-request lands through the queue.
        self.integrate("direct-main")
        self.assertEqual(len(agent_setup.doctor_warnings(self.root, {})), 1)
        self.assertEqual(self.qk("setup")[0], 0)
        self.assertIn(agent_setup.RULE, (self.root / "AGENTS.md").read_text())

    def test_a_pull_request_queue_project_gets_its_own_rule(self):
        self.integrate("pull-request-queue")
        status, said = self.qk("setup")
        self.assertEqual(status, 0, said)
        self.assertIn("landing labeled pull requests through the merge queue", said)
        # While origin/main is not in that mode yet, as before its first landing, the change that puts it there is
        # merged by hand (vocaro/queuekeeper#403); once it is, later pull requests are labeled.
        self.assertIn("a person merges by hand", said)
        identity = {**os.environ, "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.com",
                    "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.com"}
        subprocess.run(["git", "-C", str(self.root), "add", "queuekeeper.json"], check=True, env=identity)
        subprocess.run(["git", "-C", str(self.root), "commit", "-q", "-m", "Queue mode"], check=True, env=identity)
        subprocess.run(["git", "-C", str(self.root), "update-ref", "refs/remotes/origin/main", "HEAD"], check=True)
        status, said = self.qk("setup")
        self.assertIn("have a person label it for the merge queue", said)
        self.assertIn(agent_setup.PULL_REQUEST_QUEUE_RULE, (self.root / "AGENTS.md").read_text())
        self.assertEqual(self.qk("check")[0], 0)
        self.integrate("pull-request")
        self.assertIn("rule for landing labeled pull requests", self.qk("check")[1])

    def test_pull_request_queue_settings_are_checked(self):
        from queuekeeper import config
        base = {"schemaVersion": 1, "name": "x", "integration": "pull-request-queue"}
        self.assertEqual(config.queue_label(config.parse(json.dumps(base))), "queue")
        self.assertEqual(config.queue_label(config.parse(json.dumps({**base, "pullRequests": {"label": "ship"}}))),
                         "ship")
        for settings, said in (({"label": ""}, "nonempty"), ({"labels": "x"}, "Unknown pullRequests"),
                               ("queue", "must be an object")):
            with self.subTest(settings=settings), self.assertRaisesRegex(ValueError, said):
                config.parse(json.dumps({**base, "pullRequests": settings}))
        with self.assertRaisesRegex(ValueError, "Unknown integration"):
            config.parse(json.dumps({**base, "integration": "merge-queue"}))

    def test_an_unreadable_queuekeeper_json_gets_the_queues_rule(self):
        (self.root / "queuekeeper.json").write_text("{not json")
        self.assertEqual(self.qk("setup")[0], 0)
        self.assertIn(agent_setup.RULE, (self.root / "AGENTS.md").read_text())

    def test_the_rules_are_the_user_guides(self):
        guide = (ROOT / "doc/user_guide.md").read_text()
        section = guide[guide.index("### Tell your agents"):]
        section = section[:section.index("\n### ")]
        found = re.findall(r"```markdown\n(.*?)```", section, re.DOTALL)
        self.assertEqual(found, [agent_setup.RULE, agent_setup.PULL_REQUEST_RULE, agent_setup.PULL_REQUEST_QUEUE_RULE])


if __name__ == "__main__":
    unittest.main()
