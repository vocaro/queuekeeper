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
        for value, said in (({"network": ["example.com"]}, "none of localhost"),
                            ({"network": ["93.184.216.34:443"]}, "none of localhost"),
                            ({"network": ["example.com:443", "*:443"]}, "names hosts and also any host"),
                            ({"network": ["*:70000"]}, "unit.sandbox.network"),
                            ({"write": [""]}, "unit.sandbox.write"), ({"reads": []}, "unit.sandbox must be"),
                            ("yes", "unit.sandbox must be")):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, said):
                sandbox.settings("unit", {"sandbox": value})

    def test_hosts_are_named_with_a_port_and_a_domain_may_take_any_name_under_it(self):
        network = ["github.com:443", "*.GitHubUserContent.com:443", "localhost:8080"]
        self.assertEqual(sandbox.settings("unit", {"sandbox": {"network": network}})["network"], network)
        self.assertEqual(sandbox.hosts(network), [("github.com", 443), ("*.githubusercontent.com", 443)])

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

    def test_a_mark_is_the_message_of_each_deny_rule(self):
        text = sandbox.profile(["/w"], ["localhost"], [], "queuekeeper-sandbox-1/unit")
        self.assertIn('(deny file-write* (with message "queuekeeper-sandbox-1/unit"))', text)
        self.assertIn('(deny network* (with message "queuekeeper-sandbox-1/unit"))', text)
        self.assertIn("(deny file-write*)", sandbox.profile(["/w"], [], []))

    def test_named_hosts_are_reached_only_through_the_proxys_port(self):
        text = sandbox.profile([], ["github.com:443", "localhost:5432"], [], proxy=40123)
        self.assertIn('(allow network-outbound (remote ip "localhost:40123"))', text)
        self.assertIn('(allow network-outbound (remote ip "localhost:5432"))', text)
        self.assertNotIn("remote tcp", text)
        self.assertNotIn("mDNSResponder", text)

    def test_quotes_survive_in_a_path(self):
        self.assertIn('(subpath "/a \\"b\\"")', sandbox.profile(['/a "b"'], ["*"], []))

    def test_writable_folders_are_resolved_once_each_with_a_relative_one_from_the_tree(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(sandbox, "darwin_folder", return_value=None):
            tree = Path(temp)
            found = sandbox.writable(tree, tree / "run", ["out", "/tmp"], {"TMPDIR": temp})
        self.assertEqual(found[0], os.path.realpath(temp))
        self.assertIn(os.path.realpath(Path(temp) / "out"), found)
        self.assertEqual(found.count("/private/tmp"), 1)


class Refused(unittest.TestCase):
    """What the kernel's log says a lane's sandbox refused, read from a stream's lines (Refusals.refused), what a run
    records of it (observed), and what its summary says (summary_lines)."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.refusals = sandbox.Refusals(self.directory, 60)

    def stream(self, *messages):
        lines = ['Filtering the log data using "..."']
        lines += [json.dumps({"eventMessage": message, "processImagePath": "/kernel"}) for message in messages]
        self.refusals.path.write_text("\n".join(lines) + "\n")

    def test_each_refusal_of_the_lane_is_read_once_with_its_process(self):
        mark = self.refusals.mark("unit")
        self.stream(f"Sandbox: node(12) deny(1) file-write-create /Users/x/.npmrc\n{mark}",
                    f"2 duplicate reports for Sandbox: node(12) deny(1) file-write-create /Users/x/.npmrc\n{mark}",
                    f"Sandbox: curl(13) deny(1) network-outbound remote:*:80\n{mark}",
                    f"Sandbox: touch(14) deny(1) file-write-create /x\n{self.refusals.mark('other')}",
                    f"Sandbox: touch(15) deny(1) file-write-create /probe\n{self.refusals.token}:probe-start",
                    "not json")
        self.assertEqual(self.refusals.refused("unit"), [
            {"operation": "file-write-create", "target": "/Users/x/.npmrc", "process": "node"},
            {"operation": "network-outbound", "target": "remote:*:80", "process": "curl"}])
        self.assertEqual(len(self.refusals.refused("other")), 1)

    def test_a_run_records_what_was_refused_and_a_failure_names_a_nested_sandbox(self):
        mark = self.refusals.mark("unit")
        self.stream(f"Sandbox: node(12) deny(1) file-write-create /Users/x/.npmrc\n{mark}")
        log = self.directory / "run.log"
        log.write_text("error: 'pkg': Invalid manifest\nsandbox-exec: sandbox_apply: Operation not permitted\n")
        recorded = {"profile": "p"}
        with patch.object(plugins, "SANDBOX_NESTED", [lambda text: "SwiftPM" if "manifest" in text else None]):
            sandbox.observed(recorded, self.refusals, "unit", log, 1)
        self.assertEqual(recorded["nested"], "SwiftPM")
        self.assertEqual(recorded["refused"][0]["target"], "/Users/x/.npmrc")
        lines = sandbox.summary_lines(recorded)
        self.assertIn("SwiftPM applies a sandbox of its own", lines[0])
        self.assertEqual(lines[1], "  sandbox refused: file-write-create /Users/x/.npmrc (node)")

    def test_a_failure_without_a_reported_refusal_says_one_may_have_gone_unreported(self):
        self.stream()
        log = self.directory / "run.log"
        log.write_text("PermissionError: [Errno 1] Operation not permitted\n")
        recorded = {"profile": "p"}
        sandbox.observed(recorded, self.refusals, "unit", log, 1)
        self.assertTrue(recorded["unreported"])
        self.assertIn("the kernel reported no refusal", sandbox.summary_lines(recorded)[0])
        passed = {"profile": "p"}
        sandbox.observed(passed, self.refusals, "unit", log, 0)
        self.assertEqual(passed, {"profile": "p", "refusalsRead": False})

    def test_a_groups_lanes_are_each_named_and_a_long_list_is_cut(self):
        many = [{"operation": "file-write-create", "target": f"/x{n}", "process": "sh"} for n in range(sandbox.SHOWN + 2)]
        lines = sandbox.summary_lines({"boxed": {"profile": "p", "refused": many}, "free": {"inside": "outer"}})
        self.assertEqual(lines[0], "  sandbox refused (boxed): file-write-create /x0 (sh)")
        self.assertEqual(lines[-1], "  ... and 2 more the sandbox refused, listed in the receipt")
        self.assertEqual(sandbox.summary_lines(None), [])


class ProxyAdmits(unittest.TestCase):
    def test_a_named_host_on_its_port_and_names_under_a_domain(self):
        proxy = sandbox.Proxy([("github.com", 443), ("*.example.org", 80)])
        for host, port, admitted in (("github.com", 443, True), ("GitHub.com.", 443, True), ("github.com", 80, False),
                                     ("api.github.com", 443, False), ("a.example.org", 80, True),
                                     ("a.b.example.org", 80, True), ("example.org", 80, False),
                                     ("badexample.org", 80, False), ("140.82.112.3", 443, False)):
            with self.subTest(host=host, port=port):
                self.assertEqual(proxy.admits(host, port), admitted)

    def test_a_connect_to_a_named_host_is_tunnelled_and_another_refused(self):
        import socket as sockets
        import threading
        upstream = sockets.socket()
        upstream.bind(("127.0.0.1", 0))
        upstream.listen()
        self.addCleanup(upstream.close)
        port = upstream.getsockname()[1]

        def echo():
            connection, _ = upstream.accept()
            with connection: connection.sendall(b"echo:" + connection.recv(100))
        threading.Thread(target=echo, daemon=True).start()
        proxy = sandbox.Proxy([("upstream.test", port)])
        real = sockets.create_connection
        with patch.object(sandbox.socket, "create_connection",
                          lambda address, *rest, **options: real(("127.0.0.1", address[1]), *rest, **options)):
            proxy.start()
            self.addCleanup(proxy.close)
            with sockets.create_connection(("127.0.0.1", proxy.port), timeout=10) as client:
                client.sendall(f"CONNECT upstream.test:{port} HTTP/1.1\r\nHost: upstream.test\r\n\r\n".encode())
                self.assertIn(b" 200 ", client.recv(100))
                client.sendall(b"hi")
                self.assertEqual(client.recv(100), b"echo:hi")
            with sockets.create_connection(("127.0.0.1", proxy.port), timeout=10) as client:
                client.sendall(b"CONNECT evil.test:443 HTTP/1.1\r\n\r\n")
                self.assertIn(b" 403 ", client.recv(100))
        self.assertEqual(proxy.refused, ["evil.test:443"])


class Doctor(unittest.TestCase):
    """qk doctor's check that each Mac can apply a sandbox, made only when a lane has one."""

    def warnings(self, lanes, here=None, there=None):
        with patch.object(sandbox, "check_here", return_value=here), \
                patch("queuekeeper.farm.destinations", return_value=["build-host"]):
            return sandbox.doctor_warnings({"lanes": lanes}, read_there=lambda host: there or {"problem": None}, limit=5)

    def test_a_mac_that_cannot_apply_one_is_warned_of(self):
        self.assertEqual(self.warnings({"unit": {"sandbox": True}}), [])
        found = self.warnings({"unit": {"sandbox": True}}, here="it has no /usr/bin/sandbox-exec",
                              there={"problem": "/usr/bin/sandbox-exec applies no profile: exit 71"})
        self.assertEqual(len(found), 2)
        self.assertIn("this Mac cannot run a sandboxed lane", found[0])
        self.assertIn("build-host cannot run a sandboxed lane", found[1])

    def test_no_mac_is_read_without_a_sandboxed_lane_or_inside_one(self):
        with patch.object(sandbox, "check_here", side_effect=AssertionError("read")):
            self.assertEqual(sandbox.doctor_warnings({"lanes": {"unit": {"command": ["true"]}}}), [])
            with patch.dict(os.environ, {sandbox.INSIDE: "outer"}):
                self.assertEqual(sandbox.doctor_warnings({"lanes": {"unit": {"sandbox": True}}}), [])

    @unittest.skipUnless(Path(sandbox.SANDBOX_EXEC).exists(), "sandbox-exec is macOS's")
    def test_this_mac_applies_one(self):
        self.assertIsNone(sandbox.check_here())


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

    # The kernel reports refusals as it can, not certainly each (sandbox.Refusals): on 2026-11-19 it reported none of
    # five writes one such command made over two seconds on a loaded Mac, between two refusals it did report. So the
    # command tries five, each with a target of its own, and then fails as a refused write does, and its summary names
    # one it reported, or says that one may have gone unreported.
    REFUSED = """\
        import pathlib, sys, time
        for n in range(5):
            time.sleep(0.4)
            try: pathlib.Path(f'{sys.argv[1]}-{n}').write_text('no')
            except PermissionError as error: refused = error
        raise refused
        """

    def said_refused(self, recorded, target, said, lane=None):
        """That the run proved the kernel's log was read, and its summary names a refused write to one of the five
        targets, as its receipt does, or says one may have gone unreported."""
        self.assertTrue(recorded["refusalsRead"], said)
        found = [each["target"] for each in recorded.get("refused", [])
                 if each["operation"] == "file-write-create" and each["target"].startswith(f"{target}-")]
        self.assertEqual(len(found), len(recorded.get("refused", [])))
        whose = f" ({lane})" if lane else ""
        if found:
            self.assertIn(f"  sandbox refused{whose}: file-write-create {found[0]} (", said)
        else:
            self.assertTrue(recorded["unreported"])
            self.assertIn("Operation not permitted, and the kernel reported no refusal of it", said)

    def test_a_failed_run_says_what_the_sandbox_refused(self):
        lane = self.lane(self.REFUSED, sandbox=True)
        lane["command"].append(str(self.outside / "x"))
        self.configure(lane=lane)
        status, said = self.qk("lane")
        self.assertNotEqual(status, 0, said)
        self.said_refused(self.receipt()["sandbox"], str(self.outside / "x"), said)
        self.assertFalse(list(self.outside.iterdir()))

    def test_a_program_that_applies_a_sandbox_of_its_own_is_named(self):
        nested = f"import subprocess, sys; sys.exit(subprocess.run([{sandbox.SANDBOX_EXEC!r}, '-p', '(version 1)(allow default)', '/usr/bin/true']).returncode)"
        self.configure(lane=self.lane(nested, sandbox=True))
        status, said = self.qk("lane")
        self.assertNotEqual(status, 0, said)
        self.assertEqual(self.receipt()["sandbox"]["nested"], "a program the command started")
        self.assertIn("applies a sandbox of its own, which macOS refuses inside the lane's", said)

    FETCHES = """\
        import os, socket, sys, urllib.request
        port = sys.argv[1]
        print(urllib.request.urlopen(f'http://upstream.test:{port}/hello', timeout=10).read().decode())
        try: urllib.request.urlopen(f'http://other.test:{port}/', timeout=10)
        except urllib.error.HTTPError as error: print('other', error.code)
        try: socket.create_connection(('127.0.0.1', int(port)), timeout=5)
        except PermissionError: print('direct refused')
        else: print('direct reached')
        """

    def test_a_named_host_is_reached_through_the_proxy_and_no_other(self):
        import http.server
        import socket as sockets
        import threading

        class Hello(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = f"upstream says {self.path}".encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Hello)
        self.addCleanup(server.server_close)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        port = server.server_address[1]
        real = sockets.create_connection
        # upstream.test is this test's server, which the proxy, in this process, reaches by that name.
        def connect(address, *arguments, **options):
            return real(("127.0.0.1", address[1]) if address[0] == "upstream.test" else address, *arguments, **options)
        lane = self.lane(self.FETCHES, sandbox={"network": [f"upstream.test:{port}"]})
        lane["command"].append(str(port))
        self.configure(lane=lane)
        with patch.object(sandbox.socket, "create_connection", connect):
            status, said = self.qk("lane")
        self.assertEqual(status, 0, said)
        self.assertIn("upstream says /hello\nother 403\ndirect refused", self.log())
        recorded = self.receipt()["sandbox"]
        self.assertTrue(recorded["proxy"].startswith("127.0.0.1:"))
        self.assertIn({"operation": "proxy-connect", "target": f"other.test:{port}", "process": None},
                      recorded.get("refused", []))

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

    def test_a_groups_refusals_are_each_its_lanes(self):
        boxed = self.lane(self.REFUSED, sandbox=True)
        boxed["command"].append(str(self.outside / "boxed"))
        other = self.lane("print('passes')", sandbox=True)
        self.configure(boxed=boxed, other=other, group={"parallel": ["boxed", "other"]})
        status, said = self.qk("group")
        self.assertNotEqual(status, 0, said)
        recorded = self.receipt("group")["sandbox"]
        self.said_refused(recorded["boxed"], str(self.outside / "boxed"), said, "boxed")
        self.assertNotIn("refused", recorded["other"])
        self.assertTrue(recorded["other"]["refusalsRead"])


if __name__ == "__main__":
    unittest.main()
