import contextlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from urllib.parse import urlencode, urlsplit
from urllib.error import HTTPError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src/python"))
from queuekeeper import lane_results
from queuekeeper_apple.simulators import sim_key, sim_key_smoke
import host_isolation
from host_isolation import stretched

UDID = "0C1F2E3D-4B5A-6978-8796-A5B4C3D2E1F0"


class FakeSafari:
    """Stands in for the pool, simctl and sim-key: the page is fetched and reports over HTTP, as Safari's does."""

    def __init__(self, udid=UDID, take_why="", open_status=0, load=True, focused=True, drop=(), statuses=None,
                 interrupt_at=None, release_ok=True):
        self.udid, self.take_why, self.open_status, self.load, self.focused = udid, take_why, open_status, load, focused
        self.drop, self.statuses, self.interrupt_at, self.release_ok = set(drop), statuses or {}, interrupt_at, release_ok
        self.calls, self.url, self.value = [], None, ""

    def latest_runtime(self):
        return "iOS-27-0"

    def take(self, device, runtime):
        self.calls.append(("take", device, runtime))
        return (None, self.take_why) if self.take_why else (self.udid, "")

    def describe(self, udid):
        return {"name": "Queuekeeper iPhone 17 Pro 1", "runtime": "iOS-27-0", "xcode": "27.2", "xcodeBuild": "27C1"}

    def release(self, udid, outcome):
        self.calls.append(("release", udid, outcome))
        return self.release_ok

    def report(self, kind, key="", code=""):
        query = urlencode({"kind": kind, "key": key, "code": code, "focused": "1" if self.focused else "0",
                           "value": self.value})
        with urlopen(f"{self.url}log?{query}", timeout=stretched(10)) as response:
            assert response.status == 204

    def open_url(self, udid, url):
        self.calls.append(("openurl", udid, url))
        self.url = url
        if self.load:
            with urlopen(url, timeout=stretched(10)) as response:
                page = response.read().decode()
            assert "<input id=\"field\" autofocus" in page
            self.report("ready")
        return self.open_status, "NSPOSIXErrorDomain code 60" if self.open_status else ""

    def send(self, udid, key):
        self.calls.append(("send", key))
        if key == self.interrupt_at:
            raise KeyboardInterrupt
        if key in self.statuses:
            return self.statuses[key]
        codes = dict(sim_key_smoke.KEYS)[key]
        if len(key) == 1:
            self.value += key
        for code in codes:
            if (key, code) not in self.drop:
                self.report("keydown", key, code)
        for code in reversed(codes):
            if (key, code) not in self.drop:
                self.report("keyup", key, code)
        return 0


class SmokeTests(unittest.TestCase):
    def smoke(self, safari, *arguments):
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as results, contextlib.redirect_stdout(out):
            status = sim_key_smoke.main(["--results", results, "--deadline", "0.5", "--page-deadline", "1",
                                         *arguments], tools=safari)
            record = json.loads((Path(results) / "sim-key-smoke.json").read_text())
        return status, out.getvalue(), record

    def releases(self, safari):
        return [call for call in safari.calls if call[0] == "release"]

    def test_every_key_arriving_passes_and_the_member_is_released(self):
        safari = FakeSafari(open_status=60)
        status, out, record = self.smoke(safari)
        self.assertEqual(status, 0, out)
        self.assertEqual(safari.calls[0], ("take", "iPhone-17-Pro", "iOS-27-0"))
        self.assertEqual([call[1] for call in safari.calls if call[0] == "send"], [key for key, _ in sim_key_smoke.KEYS])
        # The page is served from 127.0.0.1 on a port of its own.
        url = urlsplit(safari.url)
        self.assertEqual((url.scheme, url.hostname, url.path), ("http", "127.0.0.1", "/"))
        self.assertGreater(url.port, 0)
        # openurl's timeout does not end the check while the page reports.
        self.assertIn("simctl openurl exited 60", out)
        for key, _ in sim_key_smoke.KEYS:
            self.assertIn(f"PASS {key}: ", out)
        self.assertIn("PASS shift+right: keydown and keyup of ShiftLeft ArrowRight", out)
        self.assertIn("sim-key-smoke: PASSED: all 12 keys arrived; the field reads 'qk'", out)
        self.assertEqual(self.releases(safari), [("release", UDID, "sim-key smoke passed")])
        self.assertEqual((record["verdict"], record["exit"], record["released"], record["xcode"]),
                         ("passed", 0, True, "27.2"))
        self.assertEqual(len(record["keys"]), 12)

    def test_a_key_that_does_not_arrive_fails_and_still_releases(self):
        # Negative controls: a keyup lost, a modifier lost from a chord, and sim-key failing.
        safari = FakeSafari(drop={("end", "End"), ("shift+right", "ShiftLeft")}, statuses={"home": 1})
        status, out, record = self.smoke(safari, "--device", "iPad-Air-13-inch-M4", "--runtime", "iOS-27-2")
        self.assertEqual(status, 1, out)
        self.assertEqual(safari.calls[0], ("take", "iPad-Air-13-inch-M4", "iOS-27-2"))
        self.assertIn("MISS end: no keydown End, keyup End within 0.5 seconds", out)
        self.assertIn("MISS shift+right: no keydown ShiftLeft, keyup ShiftLeft", out)
        self.assertIn("MISS home: sim-key exited 1", out)
        self.assertIn("PASS pageup: ", out)
        self.assertIn("sim-key-smoke: FAILED: 3 of 12 keys did not arrive: home end shift+right", out)
        self.assertEqual(self.releases(safari), [("release", UDID, "sim-key smoke failed")])
        self.assertEqual(record["verdict"], "failed")

    def test_a_key_a_later_send_brings_does_not_count_for_an_earlier_one(self):
        safari = FakeSafari(drop={("q", "KeyQ")})
        status, out, _ = self.smoke(safari)
        self.assertEqual(status, 1, out)
        self.assertIn("MISS q: ", out)
        self.assertIn("PASS k: ", out)

    def test_nothing_measured_is_unmeasured_not_failed(self):
        cases = [
            # No member could be taken: nothing to release.
            (FakeSafari(take_why="qk simulator-pool take exited 3"), "no iPhone-17-Pro on iOS-27-0 could be taken", 0),
            # The page never reported: openurl is tried twice.
            (FakeSafari(load=False, open_status=60), "never reported back", 1),
            # sim-key could not reach the HID service.
            (FakeSafari(statuses={"pagedown": sim_key.EXIT_UNREACHABLE}), "could not reach the simulator's HID", 1),
        ]
        for safari, reason, released in cases:
            with self.subTest(reason=reason):
                status, out, record = self.smoke(safari)
                self.assertEqual(status, 3, out)
                self.assertIn(f"sim-key-smoke: UNMEASURED: ", out)
                self.assertIn(reason, out)
                self.assertEqual(len(self.releases(safari)), released)
                self.assertEqual(record["verdict"], "unmeasured")
        self.assertEqual(len([call for call in cases[1][0].calls if call[0] == "openurl"]), 2)
        self.assertNotIn(("send", "end"), cases[2][0].calls)

    def test_a_stop_or_an_error_still_releases_the_member(self):
        safari = FakeSafari(interrupt_at="home")
        status, out, _ = self.smoke(safari)
        self.assertEqual(status, 128 + signal.SIGINT, out)
        self.assertEqual(self.releases(safari), [("release", UDID, "sim-key smoke stopped")])
        self.assertIn("sim-key-smoke: STOPPED", out)
        # SIGINT's handler is Python's own again.
        self.assertIs(signal.getsignal(signal.SIGINT), signal.default_int_handler)

        class Broken(FakeSafari):
            def open_url(self, udid, url):
                raise RuntimeError("simctl vanished")
        broken = Broken()
        status, out, _ = self.smoke(broken)
        self.assertEqual(status, 1, out)
        self.assertIn("the check itself failed", out)
        self.assertEqual(len(self.releases(broken)), 1)

    def test_a_pass_whose_member_was_not_released_fails(self):
        status, out, record = self.smoke(FakeSafari(release_ok=False))
        self.assertEqual(status, 1, out)
        self.assertIn("COULD NOT RELEASE", out)
        self.assertIn("but the member was not released", out)
        self.assertFalse(record["released"])

    def test_its_exit_codes_are_queuekeepers_and_sim_keys(self):
        self.assertEqual(sim_key_smoke.UNMEASURED, lane_results.UNMEASURED)
        self.assertEqual(sim_key_smoke.UNMEASURED, sim_key.EXIT_UNREACHABLE)
        self.assertEqual(sim_key_smoke.EXIT_USAGE, sim_key.EXIT_USAGE)
        # Every key it sends is one sim-key takes.
        for key, _ in sim_key_smoke.KEYS:
            with self.subTest(key=key):
                sim_key.events(key)


class ReportTests(unittest.TestCase):
    def test_a_report_is_read_from_its_query(self):
        self.assertEqual(sim_key_smoke.parse_report("/log?kind=keydown&key=PageUp&code=PageUp&focused=1&value=qk"),
                         {"kind": "keydown", "key": "PageUp", "code": "PageUp", "focused": True, "value": "qk"})
        self.assertEqual(sim_key_smoke.parse_report("/log?kind=ready&focused=0"),
                         {"kind": "ready", "key": "", "code": "", "focused": False, "value": ""})
        # Negative controls: other paths, kinds and a missing kind are not reports.
        for target in ("/", "/favicon.ico", "/log", "/log?kind=click&code=KeyQ", "/logs?kind=keyup", "/x/log?kind=keyup"):
            with self.subTest(target=target):
                self.assertIsNone(sim_key_smoke.parse_report(target))

    def test_missing_names_each_absent_keydown_and_keyup(self):
        events = [{"kind": "keydown", "code": "ShiftLeft"}, {"kind": "keydown", "code": "ArrowRight"},
                  {"kind": "keyup", "code": "ArrowRight"}]
        self.assertEqual(sim_key_smoke.missing(("ShiftLeft", "ArrowRight"), events), ["keyup ShiftLeft"])
        self.assertEqual(sim_key_smoke.missing(("ArrowRight",), events), [])
        self.assertEqual(sim_key_smoke.missing(("KeyQ",), []), ["keydown KeyQ", "keyup KeyQ"])

    def test_the_server_serves_the_page_and_takes_only_reports(self):
        reports = sim_key_smoke.Reports()
        server = sim_key_smoke.serve(reports)
        try:
            host, port = server.server_address
            self.assertEqual(host, "127.0.0.1")
            base = f"http://127.0.0.1:{port}/"
            with urlopen(base, timeout=stretched(10)) as response:
                self.assertEqual(response.headers["Content-Type"], "text/html; charset=utf-8")
                page = response.read().decode()
            for part in ('autofocus', 'addEventListener("keydown"', 'addEventListener("keyup"', 'fetch("/log?"',
                         'report("ready")'):
                self.assertIn(part, page)
            with urlopen(base + "log?kind=keyup&code=End&focused=1", timeout=stretched(10)) as response:
                self.assertEqual(response.status, 204)
            with self.assertRaises(HTTPError) as refused:
                urlopen(base + "elsewhere", timeout=stretched(10))
            refused.exception.close()
            self.assertEqual(refused.exception.code, 404)
            self.assertEqual([event["code"] for event in reports.since(0)], ["End"])
            # A wait ends at its deadline when nothing comes.
            self.assertEqual(reports.wait_for(lambda events: len(events) > 1, 0, 0.2), reports.since(0))
        finally:
            server.shutdown()
            server.server_close()


class ScriptTests(unittest.TestCase):
    def test_the_script_refuses_arguments_it_does_not_take_before_taking_anything(self):
        for arguments in (["--bogus"], ["--deadline", "soon"], ["extra"]):
            with self.subTest(arguments=arguments):
                result = subprocess.run([str(ROOT / "scripts/sim-key-smoke"), *arguments], capture_output=True,
                                        text=True, timeout=stretched(60))
                self.assertEqual(result.returncode, 64, result.stderr)
                self.assertIn("usage: sim-key-smoke", result.stderr)
                self.assertEqual(result.stdout, "")

    def test_the_lane_is_opt_in_and_never_in_the_gate(self):
        config = json.loads((ROOT / "queuekeeper.json").read_text())
        self.assertEqual(config["lanes"]["sim-key-smoke"]["command"], ["python3", "scripts/sim-key-smoke"])
        self.assertNotIn("sim-key-smoke", config.get("queue", {}).get("lanes", ["gate"]))
        self.assertNotIn("sim-key-smoke", json.dumps(config["lanes"]["gate"]))
        self.assertEqual(config["testLanes"]["selects"]["sim-key-smoke"], [])


if __name__ == "__main__":
    unittest.main()
