"""The MCP server (queuekeeper_mcp): its protocol over stdio, the qk commands each tool runs, and progress."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src/python"))
import host_isolation  # noqa: F401 -- first, as every test module
from host_isolation import stretched
from queuekeeper import checkout
from queuekeeper_mcp import server
from test_support import queue as test_merge_queue

SERVER = Path(server.__file__)

# A stand-in launcher: records its arguments and directory, writes two lines of progress to standard error and
# prints the JSON that FAKE_RESULT names, then exits with FAKE_STATUS.
FAKE = textwrap.dedent("""\
    import json, os, sys, pathlib
    pathlib.Path(os.environ["FAKE_RECORD"]).write_text(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd()}))
    print("waiting behind a round", file=sys.stderr, flush=True)
    print("heartbeat: still waiting", file=sys.stderr, flush=True)
    sys.stdout.write(os.environ.get("FAKE_RESULT", '{"schemaVersion": 1, "state": "landed"}'))
    sys.exit(int(os.environ.get("FAKE_STATUS", "0")))
""")


class Commands(unittest.TestCase):
    def test_each_tool_runs_its_json_command(self):
        for name, arguments, expected in (
                ("land", {}, ["land", "--json"]),
                ("land", {"reference": "topic", "note": "a fix"}, ["land", "topic", "--json", "--note=a fix"]),
                ("queue_wait", {"ids": ["a", "b"]}, ["queue", "wait", "a", "b", "--limit=600", "--json"]),
                ("queue_wait", {"ids": ["a"], "limit": 5}, ["queue", "wait", "a", "--limit=5", "--json"]),
                ("queue_result", {"id": "a"}, ["queue", "result", "a", "--json"]),
                ("queue_status", {}, ["queue", "status", "--json"]),
                ("queue_logs", {"id": "a"}, ["queue", "logs", "a", "--json"]),
                ("queue_logs", {"id": "a", "cursor": "-c1", "limit": 64},
                 ["queue", "logs", "a", "--json", "--cursor=-c1", "--limit=64"])):
            with self.subTest(name=name, arguments=arguments):
                self.assertEqual(server.command(name, arguments), expected)

    def test_arguments_the_schema_does_not_allow_are_refused(self):
        for name, arguments in (("nothing", {}), ("queue_result", {}), ("queue_result", {"id": 3}),
                                ("queue_status", {"verbose": True}), ("queue_wait", {"ids": []}),
                                ("queue_wait", {"ids": ["a"], "limit": 0}), ("land", {"reference": "--front"}),
                                ("queue_wait", {"ids": ["a", "-b"]}), ("queue_logs", {"id": "-a"})):
            with self.subTest(name=name, arguments=arguments), self.assertRaises(server.Invalid):
                server.command(name, arguments)

    def test_every_listed_tool_has_a_command(self):
        self.assertEqual([tool["name"] for tool in server.TOOLS],
                         ["land", "queue_wait", "queue_result", "queue_status", "queue_logs"])

    def test_the_launcher_is_the_nearest_scripts_qk(self):
        base = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        (base / "scripts").mkdir()
        (base / "scripts/qk").write_text("")
        (base / "deep/er").mkdir(parents=True)
        self.assertEqual(server.launcher_for(base / "deep/er"), base / "scripts/qk")
        self.assertEqual(server.launcher_for(base, "/elsewhere/qk"), Path("/elsewhere/qk"))
        with self.assertRaises(server.Invalid): server.launcher_for(Path("/"))


class Stdio(unittest.TestCase):
    """The server as a client starts it: a process speaking one JSON-RPC message a line."""

    def setUp(self):
        self.base = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        (self.base / "scripts").mkdir()
        (self.base / "scripts/qk").write_text(FAKE)
        (self.base / "worktree").mkdir()
        self.record = self.base / "record.json"

    def converse(self, *messages, result=None, status=0, project=None):
        environment = {**os.environ, "FAKE_RECORD": str(self.record), "FAKE_STATUS": str(status),
                       "PYTHONPATH": os.pathsep.join(map(str, checkout.source_paths()))}
        if result is not None: environment["FAKE_RESULT"] = result
        finished = subprocess.run([sys.executable, str(SERVER), "--project", str(project or self.base)],
                                  input="".join(json.dumps(m) + "\n" for m in messages), capture_output=True,
                                  text=True, env=environment, timeout=stretched(60))
        self.assertEqual(finished.returncode, 0, finished.stderr)
        return [json.loads(line) for line in finished.stdout.splitlines()]

    def call(self, identifier, name, arguments, token=None):
        params = {"name": name, "arguments": arguments}
        if token is not None: params["_meta"] = {"progressToken": token}
        return {"jsonrpc": "2.0", "id": identifier, "method": "tools/call", "params": params}

    def test_it_initializes_and_lists_its_tools(self):
        replies = self.converse(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "ping"},
            {"jsonrpc": "2.0", "id": 4, "method": "resources/list"})
        by_id = {reply["id"]: reply for reply in replies}
        self.assertEqual(by_id[1]["result"]["protocolVersion"], "2025-03-26")
        self.assertEqual(by_id[1]["result"]["serverInfo"]["name"], "queuekeeper")
        self.assertEqual({tool["name"] for tool in by_id[2]["result"]["tools"]},
                         {"land", "queue_wait", "queue_result", "queue_status", "queue_logs"})
        self.assertEqual(by_id[3]["result"], {})
        self.assertEqual(by_id[4]["error"]["code"], -32601)
        self.assertEqual(len(replies), 4, "a notification gets no reply")

    def test_an_unknown_protocol_version_gets_the_latest(self):
        reply, = self.converse({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                "params": {"protocolVersion": "1999-01-01"}})
        self.assertEqual(reply["result"]["protocolVersion"], server.PROTOCOL_VERSIONS[0])

    def test_a_wait_returns_the_json_result_with_progress_from_its_standard_error(self):
        replies = self.converse(self.call(7, "queue_wait", {"ids": ["e1"], "limit": 30}, token="t"),
                                result='{"schemaVersion": 1, "id": "e1", "state": "held"}', status=75)
        progress = [reply["params"] for reply in replies if reply.get("method") == "notifications/progress"]
        self.assertEqual(progress, [{"progressToken": "t", "progress": 1, "message": "waiting behind a round"},
                                    {"progressToken": "t", "progress": 2, "message": "heartbeat: still waiting"}])
        result = replies[-1]["result"]
        self.assertEqual(replies[-1]["id"], 7)
        self.assertEqual(result["structuredContent"], {"schemaVersion": 1, "id": "e1", "state": "held",
                                                       "exitStatus": 75})
        self.assertFalse(result["isError"], "a held entry is a result, not a failure of the tool")
        self.assertIn("exited 75", result["content"][0]["text"])
        recorded = json.loads(self.record.read_text())
        self.assertEqual(recorded["argv"], ["queue", "wait", "e1", "--limit=30", "--json"])

    def test_without_a_progress_token_no_progress_is_sent(self):
        replies = self.converse(self.call(1, "queue_status", {}))
        self.assertEqual([reply["id"] for reply in replies], [1])

    def test_land_runs_in_the_directory_it_names(self):
        replies = self.converse(self.call(1, "land", {"directory": "worktree", "note": "n"}))
        self.assertEqual(replies[-1]["result"]["structuredContent"]["state"], "landed")
        recorded = json.loads(self.record.read_text())
        self.assertEqual(recorded["argv"], ["land", "--json", "--note=n"])
        self.assertEqual(Path(recorded["cwd"]).resolve(), self.base / "worktree")

    def test_output_that_is_not_json_is_an_error_with_its_standard_error(self):
        result = self.converse(self.call(1, "queue_result", {"id": "e1"}), result="Traceback", status=2)[-1]["result"]
        self.assertTrue(result["isError"])
        self.assertIn("heartbeat: still waiting", result["content"][0]["text"])
        self.assertNotIn("structuredContent", result)

    def test_bad_arguments_are_a_tool_error_and_an_unknown_tool_a_protocol_error(self):
        replies = self.converse(self.call(1, "queue_result", {}), self.call(2, "deploy", {}),
                                self.call(3, "queue_status", {"directory": "missing"}))
        by_id = {reply["id"]: reply for reply in replies}
        self.assertTrue(by_id[1]["result"]["isError"])
        self.assertEqual(by_id[2]["error"]["code"], -32602)
        self.assertIn("no directory", by_id[3]["result"]["content"][0]["text"])
        self.assertFalse(self.record.exists(), "no command ran")

    def test_a_line_that_is_not_json_is_a_parse_error(self):
        environment = {**os.environ, "PYTHONPATH": os.pathsep.join(map(str, checkout.source_paths()))}
        finished = subprocess.run([sys.executable, str(SERVER)], input="{not json\n", capture_output=True, text=True,
                                  env=environment, timeout=stretched(60))
        self.assertEqual(json.loads(finished.stdout)["error"]["code"], -32700)


class AgainstAQueue(test_merge_queue.Fixture):
    """The server over a real project's launcher and queue."""

    def converse(self, *calls):
        messages = [{"jsonrpc": "2.0", "id": number, "method": "tools/call",
                     "params": {"name": name, "arguments": arguments}}
                    for number, (name, arguments) in enumerate(calls, 1)]
        finished = subprocess.run([sys.executable, str(SERVER), "--project", str(self.repo)],
                                  input="".join(json.dumps(m) + "\n" for m in messages), capture_output=True,
                                  text=True, timeout=stretched(300))
        self.assertEqual(finished.returncode, 0, finished.stderr)
        return {reply["id"]: reply["result"] for reply in map(json.loads, finished.stdout.splitlines())}

    def test_status_and_result_read_the_queue(self):
        self.commit("one.txt", "one")
        identifier, = self.add("one")
        replies = self.converse(("queue_status", {}), ("queue_result", {"id": identifier}),
                                ("queue_result", {"id": "no-such-entry"}))
        self.assertEqual(replies[1]["structuredContent"]["exitStatus"], 0, replies[1]["content"][0]["text"])
        self.assertIn(identifier, replies[1]["content"][0]["text"])
        result = replies[2]["structuredContent"]
        self.assertEqual((result["id"], result["state"], result["exitStatus"]), (identifier, "queued", 0))
        self.assertTrue(replies[3]["isError"], "an unknown ID exits 2")


if __name__ == "__main__":
    unittest.main()
