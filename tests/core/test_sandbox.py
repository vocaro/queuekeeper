"""A lane's "sandbox": its settings, its Seatbelt profile, and qk run starting its command under sandbox-exec."""
import contextlib
from io import StringIO
import json
import os
from pathlib import Path
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch

import host_isolation  # noqa: F401 (keeps the tests off the real hosts)
from queuekeeper import cli, config, plugins, lane_results, sandbox

SCHEMA = Path(__file__).resolve().parents[2] / "schemas/queuekeeper.schema.json"


def parsed(**lane):
    return config.parse(json.dumps({"schemaVersion": 1, "name": "Example", "lanes": {"unit": lane}}))


class Settings(unittest.TestCase):
    def test_true_takes_the_defaults_and_false_or_absent_none(self):
        self.assertEqual(sandbox.settings("unit", {"sandbox": True}), {"write": [], "network": []})
        self.assertIsNone(sandbox.settings("unit", {"sandbox": False}))
        self.assertIsNone(sandbox.settings("unit", {}))

    def test_endpoints_seatbelt_can_enforce_are_taken(self):
        said = {"write": ["~/.gradle", "out"], "network": ["localhost", "localhost:8080", "*:443", "/var/run/x.sock"]}
        self.assertEqual(sandbox.settings("unit", {"sandbox": said}), said)

    def test_a_host_name_or_a_bad_value_is_refused_naming_the_lane(self):
        for value, said in (({"network": ["example.com:443"]}, "tells only localhost"),
                            ({"network": ["*:70000"]}, "unit.sandbox.network"),
                            ({"write": [""]}, "unit.sandbox.write"), ({"reads": []}, "unit.sandbox must be"),
                            ("yes", "unit.sandbox must be")):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, said):
                sandbox.settings("unit", {"sandbox": value})

    def test_parse_takes_it_on_an_admitted_lane_and_refuses_it_on_a_self_lane(self):
        self.assertTrue(parsed(command=["true"], sandbox=True)["lanes"]["unit"]["sandbox"])
        with self.assertRaisesRegex(ValueError, "unit.sandbox needs an admitted lane"):
            parsed(command=["true"], sandbox=True, resource="self")

    def test_a_plugin_refuses_it_for_the_lanes_it_knows_cannot_run_in_one(self):
        with patch.object(plugins, "SANDBOX_REFUSALS", [lambda lane: "it runs xcodebuild" if "xcodebuild" in lane["command"] else None]):
            parsed(command=["make"], sandbox=True)
            with self.assertRaisesRegex(ValueError, "unit.sandbox cannot be: it runs xcodebuild"):
                parsed(command=["xcodebuild", "test"], sandbox=True)

    def test_the_schema_lists_the_key_and_the_endpoints_it_takes(self):
        schema = json.loads(SCHEMA.read_text())["properties"]["lanes"]["additionalProperties"]["properties"]["sandbox"]
        self.assertEqual(set(schema["oneOf"][1]["properties"]), sandbox.KEYS)


class Profile(unittest.TestCase):
    def test_writes_are_denied_but_where_allowed_and_the_network_is_off_by_default(self):
        text = sandbox.profile(["/w"], [], ["/w"])
        self.assertTrue(text.startswith("(version 1)\n(allow default)\n(deny file-write*)\n(allow file-write*\n  (subpath \"/w\")"))
        self.assertIn("(deny network*)", text)
        self.assertIn('(allow network-outbound (remote unix-socket (subpath "/w")))', text)
        self.assertNotIn("remote tcp", text)

    def test_endpoints_become_rules_and_a_port_on_any_host_may_resolve_names(self):
        text = sandbox.profile([], ["localhost:5432", "*:443"], [])
        self.assertIn('(allow network-outbound (remote ip "localhost:5432"))', text)
        self.assertIn('(allow network-outbound (remote tcp "*:443"))', text)
        self.assertIn("mDNSResponder", text)
        self.assertNotIn("network*", sandbox.profile([], ["*"], []))

    def test_quotes_survive_in_a_path(self):
        self.assertIn('(subpath "/a \\"b\\"")', sandbox.profile(['/a "b"'], ["*"], []))

    def test_writable_folders_are_resolved_once_each_with_a_relative_one_from_the_tree(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(sandbox, "darwin_folder", return_value=None):
            tree = Path(temp)
            found = sandbox.writable(tree, tree / "run", ["out", "/tmp"], {"TMPDIR": temp})
        self.assertEqual(found[0], os.path.realpath(temp))
        self.assertIn(os.path.realpath(Path(temp) / "out"), found)
        self.assertEqual(found.count("/private/tmp"), 1)


@unittest.skipUnless(Path(sandbox.SANDBOX_EXEC).exists(), "sandbox-exec is macOS's")
class Runs(unittest.TestCase):
    """qk run, in this process, of lanes in a checkout inside a temporary folder that the sandbox does not allow, beside
    a folder outside it: the temporary folders the profile allows are made /private/var/tmp's alone."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve() / "project"
        self.root.mkdir()
        self.outside = self.root.parent / "outside"
        self.outside.mkdir()
        for patcher in (patch.object(sandbox, "darwin_folder", return_value=None),
                        patch.dict(os.environ, {"TMPDIR": "/private/var/tmp"})):
            patcher.start()
            self.addCleanup(patcher.stop)
        os.environ.pop(sandbox.INSIDE, None)

    def configure(self, **lanes):
        (self.root / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Example", "lanes": lanes}))

    def lane(self, body, **keys):
        return {"command": [sys.executable, "-c", textwrap.dedent(body)], **keys}

    def qk(self, *arguments):
        stream = StringIO()
        with patch.object(cli, "refresh_policy", return_value="policy"), patch.object(cli, "source_identity", return_value={}), \
                contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
            status = cli.main(["--project", str(self.root), "run", "--here", *arguments])
        return status, stream.getvalue()

    def receipt(self, lane="lane"):
        return json.loads((self.root / ".build/queuekeeper" / lane / "source.json").read_text())

    def log(self, lane="lane"):
        return (self.root / ".build/queuekeeper" / lane / "run.log").read_text()

    WRITES = f"""\
        import os, pathlib, sys
        pathlib.Path('inside.txt').write_text('ok')
        print('inside', os.environ.get('{sandbox.INSIDE}'))
        try: pathlib.Path(sys.argv[1]).write_text('no')
        except PermissionError: print('outside refused')
        else: print('outside written')
        """

    def test_a_sandboxed_lane_writes_in_its_checkout_and_is_refused_outside(self):
        lane = self.lane(self.WRITES, sandbox=True)
        lane["command"].append(str(self.outside / "x"))
        self.configure(lane=lane)
        status, said = self.qk("lane")
        self.assertEqual(status, 0, said)
        self.assertIn("inside lane\noutside refused", self.log())
        self.assertFalse((self.outside / "x").exists())
        recorded = self.receipt()["sandbox"]
        self.assertEqual(recorded["network"], "off")
        self.assertIn(str(self.root), recorded["write"])
        self.assertTrue(Path(recorded["profile"]).is_file())

    def test_a_lane_without_one_writes_where_it_likes(self):
        lane = self.lane(self.WRITES)
        lane["command"].append(str(self.outside / "x"))
        self.configure(lane=lane)
        self.assertEqual(self.qk("lane")[0], 0)
        self.assertIn("outside written", self.log())
        self.assertNotIn("sandbox", self.receipt())

    def test_a_declared_folder_is_writable(self):
        lane = self.lane(self.WRITES, sandbox={"write": [str(self.outside)]})
        lane["command"].append(str(self.outside / "x"))
        self.configure(lane=lane)
        self.assertEqual(self.qk("lane")[0], 0)
        self.assertIn("outside written", self.log())

    CONNECTS = """\
        import socket
        try:
            server = socket.socket(); server.bind(('127.0.0.1', 0)); server.listen()
            socket.create_connection(server.getsockname(), timeout=5)
        except PermissionError: print('network refused')
        else: print('network reached')
        """

    def test_the_network_is_off_unless_the_lane_names_it(self):
        for network, said in (([], "network refused"), (["localhost"], "network reached")):
            with self.subTest(network=network):
                self.configure(lane=self.lane(self.CONNECTS, sandbox={"network": network}))
                self.assertEqual(self.qk("lane")[0], 0)
                self.assertIn(said, self.log())

    def test_compile_first_runs_in_the_sandbox_too(self):
        lane = self.lane("print('tests')", sandbox=True)
        lane["compileFirst"] = [sys.executable, "-c", f"open({str(self.outside / 'built')!r}, 'w')"]
        self.configure(lane=lane)
        status, said = self.qk("lane")
        self.assertNotEqual(status, 0, said)
        self.assertFalse((self.outside / "built").exists())

    def test_a_lane_run_inside_a_sandbox_runs_under_that_one(self):
        lane = self.lane(self.WRITES, sandbox=True)
        lane["command"].append(str(self.outside / "x"))
        self.configure(lane=lane)
        with patch.dict(os.environ, {sandbox.INSIDE: "outer"}):
            self.assertEqual(self.qk("lane")[0], 0)
        self.assertEqual(self.receipt()["sandbox"], {"inside": "outer"})
        self.assertIn("outside written", self.log())

    def test_a_profile_that_does_not_apply_ends_the_lane_not_started(self):
        self.configure(lane=self.lane("print('ran')", sandbox=True))
        with patch.object(sandbox, "profile", return_value="(version 1)\n(nonsense\n"):
            status, said = self.qk("lane")
        self.assertEqual(status, lane_results.NOT_STARTED)
        self.assertIn("does not apply here", said)
        self.assertNotIn("ran\n", self.log().replace("could not start", ""))

    def test_each_lane_of_a_group_runs_under_its_own(self):
        boxed = self.lane(self.WRITES, sandbox=True)
        boxed["command"].append(str(self.outside / "boxed"))
        free = self.lane(self.WRITES)
        free["command"].append(str(self.outside / "free"))
        self.configure(boxed=boxed, free=free, group={"parallel": ["boxed", "free"]})
        status, said = self.qk("group")
        self.assertEqual(status, 0, said)
        self.assertFalse((self.outside / "boxed").exists())
        self.assertTrue((self.outside / "free").exists())
        self.assertEqual(set(self.receipt("group")["sandbox"]), {"boxed"})


if __name__ == "__main__":
    unittest.main()
