"""Check that scripts/sim-key's keys reach an app on a pool simulator: sim-key-smoke [OPTIONS].

    scripts/sim-key-smoke [--device TYPE] [--runtime RUNTIME] [--pool NAME] [--results DIR]

Run it under admission (`qk admit --here -- scripts/sim-key-smoke`, or `qk run sim-key-smoke`) on
each new Xcode or simulator runtime, since any of them can break the private API sim-key drives.
It takes one member of the project's simulator pool with `qk simulator-pool take`, serves a page
with an autofocused text field from 127.0.0.1 on a free port, opens it in the member's Safari with
`simctl openurl`, and sends KEYS with scripts/sim-key one at a time. The page reports each keydown
and keyup back to the server, and every key must arrive, both down and up, within --deadline
seconds. The member is released however the run ends, Ctrl-C and SIGTERM included. No Simulator
window opens.

It prints a PASS or MISS line per key and a verdict line, and with --results (default
$QUEUEKEEPER_RESULTS_DIR) writes sim-key-smoke.json there.

Exit status: 0 when every key arrived; 1 when a key was sent and did not arrive, sim-key failed,
or the member could not be released; 3 when nothing was measured, because no member could be
taken, the page never reported back, or sim-key could not reach the simulator's HID service (its
own exit 3); 64 for arguments it does not take; and 128 plus the signal when stopped.
"""
import argparse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import plistlib
import re
import signal
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit

from ..sources import ROOT

PASSED, FAILED, UNMEASURED, EXIT_USAGE = 0, 1, 3, 64
# Each key sent, and the KeyboardEvent.code values whose keydown and keyup it must bring.
KEYS = (
    ("q", ("KeyQ",)), ("k", ("KeyK",)),
    ("pageup", ("PageUp",)), ("pagedown", ("PageDown",)), ("home", ("Home",)), ("end", ("End",)),
    ("right", ("ArrowRight",)), ("left", ("ArrowLeft",)), ("up", ("ArrowUp",)), ("down", ("ArrowDown",)),
    ("0x4b", ("PageUp",)), ("shift+right", ("ShiftLeft", "ArrowRight")),
)
DEVICE = "iPhone-17-Pro"
PAGE = """<!doctype html>
<meta name="viewport" content="width=device-width">
<title>sim-key smoke</title>
<input id="field" autofocus autocapitalize="off" autocorrect="off" spellcheck="false">
<script>
const field = document.getElementById("field");
function report(kind, event) {
  const query = new URLSearchParams({kind, key: event ? event.key : "", code: event ? event.code : "",
                                     focused: document.activeElement === field ? "1" : "0", value: field.value});
  fetch("/log?" + query, {cache: "no-store"}).catch(() => {});
}
document.addEventListener("keydown", event => report("keydown", event), true);
document.addEventListener("keyup", event => report("keyup", event), true);
window.addEventListener("load", () => { field.focus(); report("ready"); });
</script>
"""
REPORT_KINDS = {"ready", "keydown", "keyup"}


class Stopped(BaseException):
    """SIGTERM or SIGHUP, raised so that the member is still released."""
    def __init__(self, number):
        super().__init__(number)
        self.number = number


def stopped(number, _frame):
    raise Stopped(number)


def parse_report(target):
    """The event a /log?... request reports, as a dict, or None for any other request."""
    parts = urlsplit(target)
    if parts.path != "/log":
        return None
    query = {name: values[-1] for name, values in parse_qs(parts.query, keep_blank_values=True).items()}
    if query.get("kind") not in REPORT_KINDS:
        return None
    return {"kind": query["kind"], "key": query.get("key", ""), "code": query.get("code", ""),
            "focused": query.get("focused") == "1", "value": query.get("value", "")}


class Reports:
    """The events the page has reported, in the order they arrived, for waiting threads."""

    def __init__(self):
        self.events = []
        self.changed = threading.Condition()

    def add(self, event):
        with self.changed:
            self.events.append(event)
            self.changed.notify_all()

    def mark(self):
        with self.changed:
            return len(self.events)

    def since(self, start):
        with self.changed:
            return list(self.events[start:])

    def wait_for(self, done, start, deadline):
        """Waits until done(events since start) holds or deadline seconds pass; returns those events."""
        end = time.monotonic() + deadline
        with self.changed:
            while not done(self.events[start:]):
                left = end - time.monotonic()
                if left <= 0:
                    break
                self.changed.wait(left)
            return list(self.events[start:])


def serve(reports):
    """A server on 127.0.0.1 at a free port, serving PAGE at / and taking reports at /log, in its own thread."""
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if urlsplit(self.path).path == "/":
                body = PAGE.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            event = parse_report(self.path)
            if event is not None:
                reports.add(event)
            self.send_response(204 if event is not None else 404)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def missing(codes, events):
    """The "keydown CODE" and "keyup CODE" of codes that events lack."""
    seen = {(event["kind"], event["code"]) for event in events}
    return [f"{kind} {code}" for code in codes for kind in ("keydown", "keyup") if (kind, code) not in seen]


@dataclass
class Outcome:
    verdict: int = PASSED
    reason: str = ""
    keys: list = field(default_factory=list)


class Tools:
    """What the check runs: Queuekeeper's pool command, simctl and scripts/sim-key."""

    def __init__(self, pool=None, root=ROOT):
        self.root, self.pool = Path(root), pool

    def qk(self, *arguments):
        return [sys.executable, str(self.root / "bin/qk"), "simulator-pool",
                *(["--pool", self.pool] if self.pool else []), *arguments]

    def latest_runtime(self):
        """The newest available iOS runtime, as take names it (iOS-27-0), or None."""
        try:
            listed = subprocess.run(["xcrun", "simctl", "list", "runtimes", "available", "-j"],
                                    capture_output=True, text=True, timeout=60)
            runtimes = json.loads(listed.stdout).get("runtimes", []) if listed.returncode == 0 else []
        except (OSError, subprocess.SubprocessError, ValueError):
            return None
        # Only the general runtimes, such as iOS-27-2, not a device's own, such as iOS-27-2-iPhone19-4.
        ios = [runtime for runtime in runtimes if runtime.get("platform") == "iOS" and runtime.get("isAvailable", True)
               and re.fullmatch(r"iOS-\d+-\d+", str(runtime.get("identifier", "")).rsplit(".", 1)[-1])]
        def version(runtime):
            return tuple(int(part) for part in str(runtime.get("version", "0")).split(".") if part.isdigit())
        return max(ios, key=version)["identifier"].rsplit(".", 1)[-1] if ios else None

    def take(self, device, runtime):
        """The UDID of a member taken and booted, or (None, why). Its own session, so that a Ctrl-C reaches
        only this process, which stops the take with SIGTERM and lets the take release what it holds."""
        process = subprocess.Popen(self.qk("take", "--device", device, "--runtime", runtime,
                                           "--purpose", "sim-key smoke", "--hold", "30"),
                                   stdout=subprocess.PIPE, text=True, start_new_session=True)
        try:
            out, _ = process.communicate()
        except BaseException:
            process.terminate()
            try:
                process.wait(timeout=180)
            except subprocess.TimeoutExpired:
                process.kill()
            raise
        udid = out.strip().splitlines()[-1].strip() if out.strip() else ""
        if process.returncode != 0 or not udid:
            return None, f"qk simulator-pool take exited {process.returncode}"
        return udid, ""

    def release(self, udid, outcome):
        try:
            done = subprocess.run(self.qk("release", udid, "--outcome", outcome), timeout=300)
        except (OSError, subprocess.SubprocessError) as error:
            print(f"sim-key-smoke: release of {udid} did not run: {error}", file=sys.stderr)
            return False
        return done.returncode == 0

    def describe(self, udid):
        """The member's name and runtime, and the selected Xcode's version and build."""
        found = {"udid": udid}
        try:
            listed = subprocess.run(["xcrun", "simctl", "list", "devices", "-j"], capture_output=True, text=True,
                                    timeout=60)
            for runtime, devices in json.loads(listed.stdout).get("devices", {}).items():
                for device in devices:
                    if device.get("udid", "").upper() == udid.upper():
                        found.update(name=device.get("name"), runtime=runtime.rsplit(".", 1)[-1])
            developer = subprocess.run(["xcode-select", "--print-path"], capture_output=True, text=True,
                                       timeout=60).stdout.strip()
            contents = Path(developer).parent
            info = plistlib.loads((contents / "Info.plist").read_bytes())
            version = plistlib.loads((contents / "version.plist").read_bytes())
            found.update(xcode=info.get("CFBundleShortVersionString"), xcodeBuild=version.get("ProductBuildVersion"))
        except (OSError, subprocess.SubprocessError, ValueError, plistlib.InvalidFileException):
            pass
        return found

    def open_url(self, udid, url):
        """simctl openurl's status and message. A timeout (NSPOSIXErrorDomain 60) is routine: Safari may
        load the page anyway, so the caller waits for the page rather than trusting the status."""
        try:
            done = subprocess.run(["xcrun", "simctl", "openurl", udid, url], capture_output=True, text=True,
                                  timeout=120)
        except subprocess.TimeoutExpired:
            return 124, "simctl openurl took more than 120 seconds"
        except OSError as error:
            return 1, str(error)
        return done.returncode, (done.stderr or done.stdout).strip()

    def send(self, udid, key):
        try:
            done = subprocess.run([sys.executable, str(self.root / "scripts/sim-key"), udid, key], timeout=600)
        except subprocess.TimeoutExpired:
            return 1
        return done.returncode


def check(tools, udid, reports, url, *, deadline, page_deadline, keys=KEYS):
    """Opens the page on udid and sends keys, printing a line for each; the Outcome."""
    outcome = Outcome()
    for attempt in (1, 2):
        start = reports.mark()
        status, message = tools.open_url(udid, url)
        if status != 0:
            print(f"sim-key-smoke: simctl openurl exited {status} ({message or 'no message'}); "
                  f"waiting for the page anyway")
        ready = [event for event in reports.wait_for(lambda events: any(e["kind"] == "ready" for e in events),
                                                     start, page_deadline) if event["kind"] == "ready"]
        if ready:
            print(f"sim-key-smoke: the page loaded, {'with' if ready[-1]['focused'] else 'WITHOUT'} the field focused")
            break
        print(f"sim-key-smoke: the page did not report within {page_deadline} seconds (attempt {attempt} of 2)")
    else:
        return Outcome(UNMEASURED, f"the page at {url} never reported back from the simulator's Safari")
    for key, codes in keys:
        start = reports.mark()
        status = tools.send(udid, key)
        if status == UNMEASURED:
            outcome.keys.append({"key": key, "result": "unmeasured", "sim-key": status})
            print(f"UNMEASURED {key}: sim-key exited 3, the simulator's HID service cannot be reached")
            return Outcome(UNMEASURED, "sim-key could not reach the simulator's HID service", outcome.keys)
        if status != 0:
            outcome.keys.append({"key": key, "result": "failed", "sim-key": status})
            print(f"MISS {key}: sim-key exited {status}")
            outcome.verdict = FAILED
            continue
        events = reports.wait_for(lambda events: not missing(codes, events), start, deadline)
        lacking = missing(codes, events)
        unfocused = any(event["kind"] in {"keydown", "keyup"} and not event["focused"] for event in events)
        outcome.keys.append({"key": key, "result": "missed" if lacking else "arrived", "missing": lacking,
                             "events": events})
        if lacking:
            outcome.verdict = FAILED
            print(f"MISS {key}: no {', '.join(lacking)} within {deadline} seconds")
        else:
            print(f"PASS {key}: keydown and keyup of {' '.join(codes)}"
                  + (" (the field had lost focus)" if unfocused else ""))
    if outcome.verdict == FAILED:
        missed = [entry["key"] for entry in outcome.keys if entry["result"] != "arrived"]
        outcome.reason = f"{len(missed)} of {len(keys)} keys did not arrive: {' '.join(missed)}"
    else:
        values = [event["value"] for event in reports.since(0) if event["kind"] == "keyup"]
        outcome.reason = f"all {len(keys)} keys arrived" + (f"; the field reads {values[-1]!r}" if values else "")
    return outcome


def arguments(argv):
    parser = argparse.ArgumentParser(prog="sim-key-smoke", description=__doc__.split("\n\n")[0])
    parser.add_argument("--device", default=DEVICE, metavar="TYPE", help=f"the device type to take (default {DEVICE})")
    parser.add_argument("--runtime", help="the runtime to take, such as iOS-27-0 (default: the newest available iOS)")
    parser.add_argument("--pool", help="the pool (default: the project's simulatorPool)")
    parser.add_argument("--deadline", type=float, default=15, metavar="SECONDS",
                        help="seconds each key's keydown and keyup may take to arrive (default 15)")
    parser.add_argument("--page-deadline", type=float, default=90, metavar="SECONDS",
                        help="seconds the page may take to load and report (default 90), tried twice")
    parser.add_argument("--results", default=os.environ.get("QUEUEKEEPER_RESULTS_DIR"), metavar="DIR",
                        help="where to write sim-key-smoke.json (default $QUEUEKEEPER_RESULTS_DIR)")
    return parser.parse_args(argv)


def main(argv=None, tools=None):
    try:
        args = arguments(sys.argv[1:] if argv is None else list(argv))
    except SystemExit as exit:
        return PASSED if exit.code == 0 else EXIT_USAGE
    tools = tools or Tools(pool=args.pool)
    # Its lines stay in order with those of the commands it runs, when written to a file.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    previous = {number: signal.signal(number, stopped) for number in (signal.SIGTERM, signal.SIGHUP)}
    record = {"device": args.device, "runtime": args.runtime}
    udid, outcome, server = None, None, None
    try:
        record["runtime"] = args.runtime or tools.latest_runtime()
        if not record["runtime"]:
            outcome = Outcome(UNMEASURED, "no available iOS runtime was found")
        else:
            udid, why = tools.take(args.device, record["runtime"])
            if udid is None:
                outcome = Outcome(UNMEASURED, f"no {args.device} on {record['runtime']} could be taken: {why}")
            else:
                record.update(tools.describe(udid))
                print(f"sim-key-smoke: took {record.get('name') or udid} ({udid}), {record.get('runtime')}, "
                      f"Xcode {record.get('xcode')} ({record.get('xcodeBuild')})")
                reports = Reports()
                server = serve(reports)
                url = f"http://127.0.0.1:{server.server_address[1]}/"
                outcome = check(tools, udid, reports, url, deadline=args.deadline, page_deadline=args.page_deadline)
    except (KeyboardInterrupt, Stopped) as stop:
        number = stop.number if isinstance(stop, Stopped) else signal.SIGINT
        outcome = Outcome(128 + number, f"stopped by signal {number}", outcome.keys if outcome else [])
    except Exception as error:
        outcome = Outcome(FAILED, f"the check itself failed: {error!r}", outcome.keys if outcome else [])
    finally:
        # Nothing interrupts the release itself.
        for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(number, signal.SIG_IGN)
        try:
            if server is not None:
                server.shutdown()
                server.server_close()
            if udid is not None:
                label = {PASSED: "passed", FAILED: "failed", UNMEASURED: "unmeasured"}.get(outcome.verdict, "stopped")
                record["released"] = tools.release(udid, f"sim-key smoke {label}")
                print(f"sim-key-smoke: {'released' if record['released'] else 'COULD NOT RELEASE'} {udid}")
                if not record["released"] and outcome.verdict == PASSED:
                    outcome = Outcome(FAILED, f"{outcome.reason}, but the member was not released", outcome.keys)
        finally:
            signal.signal(signal.SIGINT, signal.default_int_handler)
            for number, handler in previous.items():
                signal.signal(number, handler)
    word = {PASSED: "PASSED", FAILED: "FAILED", UNMEASURED: "UNMEASURED"}.get(outcome.verdict, "STOPPED")
    print(f"sim-key-smoke: {word}: {outcome.reason}")
    if args.results:
        record.update(verdict=word.lower(), exit=outcome.verdict, reason=outcome.reason, keys=outcome.keys)
        try:
            Path(args.results).mkdir(parents=True, exist_ok=True)
            (Path(args.results) / "sim-key-smoke.json").write_text(json.dumps(record, indent=2) + "\n")
        except OSError as error:
            print(f"sim-key-smoke: the results were not written: {error}", file=sys.stderr)
    return outcome.verdict
