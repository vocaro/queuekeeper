#!/usr/bin/env python3
"""Retake the README's screenshots of the dashboard: assets/dashboard-TAB.png for each tab shown there.

    python3 tools/dashboard-screenshots.py                    Home (Queue) and Farm, in the dark theme
    python3 tools/dashboard-screenshots.py --tabs logs history
    python3 tools/dashboard-screenshots.py --theme light --out /tmp/shots

What is captured is the dashboard itself: this checkout's page, scripts and styles, served by the dashboard's own
server. Only the data is made up, the same every run: a project named lumen-ios with a round being tested, entries
waiting and settled, and two Macs. Times are counted back from now, since the page shows each as how long ago it
was. Three things differ from what `qk serve` sends: the data, the theme, which the page takes from the system and
this script fixes, and one more script on the page, which reports the page's height so that each picture is as
tall as its tab.

The pictures are taken by Google Chrome, headless, at twice the page's 1000 points. Name another Chromium browser
with --chrome. When a panel's data changes shape, change PANELS here to match: a tab that then draws a fault is
refused, not saved."""
import argparse
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time

CHECKOUT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CHECKOUT / "src/python"))
from queuekeeper.visibility.dashboard import panels, server  # noqa: E402

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
REPOSITORY, WIDTH, SCALE = "lumen-ios", 1000, 2
TABS = ("queue", "farm", "history", "logs")
# The page's own script loads a panel's and asks for its data; this long is given it, in the page's own time.
SETTLE = 6000
MEASURE = """setInterval(() => { document.documentElement.dataset.height = document.body.scrollHeight;
  document.documentElement.dataset.fault = document.querySelector("main p.danger") ? "yes" : "no"; }, 100);"""

NOW = time.time()
ago = lambda seconds: NOW - seconds
SUBMITTED = {"0406": 4300, "0407": 3960, "0408": 3900, "0409": 2700, "0410": 1560, "0411": 1500, "0412": 410, "0413": 365,
             "0414": 290, "0415": 120, "0416": 45}
TIPS = {"0406": "c41d09aa", "0407": "5be2f7c1", "0408": "e09a3d47", "0409": "18f6b2d0", "0410": "a7c03e59", "0411": "63d9e1f4",
        "0412": "2fb8a6c3", "0413": "90e4d17b", "0414": "b35c7a08", "0415": "7d1f92e6", "0416": "4a68c0d5"}
SLUGS = dict(zip(TIPS, ("settings-layout", "export-errors", "search-index", "export-queue", "tag-picker", "scroll-position",
                        "thumbnail-cache", "issue-42-share-sheet-ipad", "sync-retries", "toolbar-labels", "import-progress")))
E = {key: f"qk{key}-{SLUGS[key]}" for key in TIPS}
ROUND = time.strftime("%Y%m%d-%H%M%S", time.localtime(ago(252))) + "-4c9a1e7b"
BEFORE = time.strftime("%Y%m%d-%H%M%S", time.localtime(ago(1900))) + "-d02f6a88"

REPOSITORIES = [
    {"name": "lumen-ios", "level": "active", "headline": "Testing 1 entry",
     "detail": "The gate lane is running on studio.local. 1 more waiting.", "waiting": 2},
    {"name": "orbit-game", "level": "active", "headline": "Testing 1 entry",
     "detail": "The gate lane is running on this Mac.", "waiting": 1},
    {"name": "fieldnotes", "level": "idle", "headline": "Idle", "detail": "Nothing is waiting.", "waiting": 0},
]

WAITING = [
    {"id": E["0412"], "state": "testing", "subject": "Cache decoded thumbnails between launches",
     "branch": "agent/thumbnail-cache", "commits": 2, "submitted": ago(410)},
    {"id": E["0415"], "state": "queued", "subject": "Add VoiceOver labels to the editor toolbar",
     "branch": "agent/toolbar-labels", "submitted": ago(120)},
]

RECENT = [
    {"id": E["0411"], "state": "landed", "subject": "Keep the scroll position when a note is edited",
     "branch": "agent/scroll-position", "landedAs": "4c9a1e7b02d3f8", "submitted": ago(1500), "settled": ago(540)},
]

PANELS = {
    "home": {
        "level": "active", "headline": "Testing 1 entry",
        "detail": "The gate lane is running on studio.local. 1 more waiting.", "began": ago(252),
        "counts": {"waiting": 2, "landed": 12, "cameBack": 0},
        "lines": [],
    },
    "queue": {
        "worker": True, "held": None,
        "round": {"name": ROUND, "base": "4c9a1e7b02d3f8", "entries": [E["0412"]], "bisect": None,
                  "gates": [
                      {"role": "batch", "lane": "gate", "mac": "studio.local", "began": ago(252),
                       "phase": "simulator tests, 212 of 340", "entries": [E["0412"]],
                       "log": ROUND + "-gate.log"},
                  ]},
        "waiting": WAITING, "recent": RECENT, "ungated": [],
    },
    "farm": {
        "readAt": ago(4), "error": None,
        "hosts": [
            {"name": "This Mac", "role": "here", "state": "observed", "reasons": [],
             "load": {"cpuBusy": 38, "busyLimit": 85, "load": 6.4, "memory": "normal", "screen": "unlocked", "error": None},
             "slots": {"used": 1, "guaranteed": 1, "most": 4},
             "holders": [
                 {"id": "qk4156-orbit-game-gate", "label": "orbit-game · gate", "heldSeconds": 431,
                  "slots": 1, "wholeHost": False, "exclusive": False}],
             "waiters": []},
            {"name": "studio.local", "role": "second", "state": "observed", "reasons": [],
             "load": {"cpuBusy": 90, "busyLimit": 85, "load": 14.2, "memory": "normal", "screen": "unlocked", "error": None},
             "slots": {"used": 2, "guaranteed": 2, "most": 4},
             "holders": [
                 {"id": "qk8237-lumen-ios-gate", "label": "lumen-ios · gate lane", "heldSeconds": 252, "slots": 2, "wholeHost": True, "exclusive": False}],
             "waiters": [
                 {"id": "qk1948-fieldnotes-sync-tests", "label": "fieldnotes · simulator tests", "waitedSeconds": 37,
                  "reason": "The Mac's CPU is near its busy limit."}]},
        ],
    },
    "history": {
        "today": {"landed": 12, "cameBack": 0, "flaky": 0, "rounds": 6, "medianGateSeconds": 252},
        "total": 2,
        "events": [
            {"time": ago(252), "kind": "round.began", "level": "plain", "text": "Round " + ROUND + " began with 1 entry."},
            {"time": ago(540), "kind": "entry.landed", "level": "success",
             "text": E["0411"] + " landed as 4c9a1e7b02d3: Keep the scroll position when a note is edited."},
        ],
    },
}

LOG = f"""queue: round {ROUND}: 1 entry on 4c9a1e7b02d3
queue: gate lane admitted on studio.local, 2 slots
gate: building LumenTests for iOS Simulator
gate: build succeeded in 1m 48s
gate: simulator tests, 4 simulators from the pool
gate: 212 of 340 tests passed, 0 failed
"""
PANELS["queue"]["overview"] = PANELS.pop("home")
PANELS["history"]["recent"] = PANELS["queue"]["recent"]
PANELS["farm"]["deviceRecord"] = {"devices": [
    {"name": "iPhone", "state": "held", "owner": "release-tests", "purpose": "Release checks",
     "releaseBy": None, "waiting": 0, "connection": "connected", "transport": "localNetwork"}],
    "deferredTo": None, "error": None, "connectionError": None, "checkedAt": ago(4)}

LOGS = [{"name": "worker.log", "size": 48213, "modified": ago(3)},
        {"name": ROUND + "-gate.log", "size": len(LOG), "modified": ago(3)},
        {"name": ROUND + "-device.log", "size": 1904, "modified": ago(64)},
        {"name": BEFORE + "-gate.log", "size": 291377, "modified": ago(540)}]


def logs(query):
    """What the Logs panel is answered: the logs, and the round's gate log once the page asks for one."""
    if not query.get("name"): return {"logs": LOGS}
    offset = min(int(query.get("offset", 0)), len(LOG))
    return {"name": query["name"], "size": len(LOG), "offset": offset, "next": len(LOG), "text": LOG[offset:], "logs": LOGS}


class App(server.App):
    """The dashboard's own, answering with the data above in place of a queue's."""

    def __init__(self):
        pass

    def answer(self, path, query):
        parts = [part for part in path.split("/") if part]
        if parts == ["panels"]: return panels.listed()
        if parts == ["repositories"]: return REPOSITORIES
        if len(parts) == 4 and parts[:3] == ["r", REPOSITORY, "panel"] and parts[3] in TABS:
            if parts[3] == "queue" and (query.get("entry") or query.get("gate")):
                raw = [{**entry, "commits": ["a" * 40] * entry.get("commits", 1)} for entry in [*WAITING, *RECENT]]
                state = {"waiting": raw, "round": {"round": ROUND, "gates": {
                    gate["role"]: {**gate, "progress": [{"phase": gate.get("phase")}]} for gate in PANELS["queue"]["round"]["gates"]}}}
                repository = type("Repository", (), {"directory": Path("/nonexistent-dashboard-fixture")})()
                return {**PANELS["queue"], "detail": panels.load("queue").detail(repository, query, state)}
            if parts[3] == "farm" and query.get("admission"):
                detail = {"id": query["admission"], "state": "missing"}
                for host in PANELS["farm"]["hosts"]:
                    if host["role"] != query.get("host"): continue
                    for collection, state in (("holders", "running"), ("waiters", "waiting")):
                        for entry in host[collection]:
                            if entry["id"] == query["admission"]:
                                detail = {**entry, "state": state, "host": host["name"], "kind": "ordinary", "pid": 4021,
                                          "process": "running", "origin": {"repository": "fieldnotes", "branch": "codex/sync-tests"}}
                return {**PANELS["farm"], "admission": detail}
            return logs(query) if parts[3] == "logs" else PANELS[parts[3]]
        raise server.Refused(404, "no such address")


class Handler(server.Handler):
    def do_GET(self):
        if self.path == "/static/measure.js": return self.send(200, MEASURE.encode(), server.TYPES[".js"])
        return super().do_GET()

    def file(self, path):
        if path.name == "index.html":
            body = path.read_text().replace("</body>", '<script src="/static/measure.js"></script>\n</body>')
            return self.send(200, body.encode(), server.TYPES[".html"])
        if path.name == "app.css":
            # The page is dark where the system is; the picture is of the theme asked for, whatever this Mac's is.
            dark = "(prefers-color-scheme: dark)"
            body = path.read_text()
            if dark not in body: raise server.Refused(500, "app.css no longer chooses its theme by " + dark)
            return self.send(200, body.replace(dark, "all" if self.server.theme == "dark" else "not all").encode(),
                             server.TYPES[".css"])
        return super().file(path)


def serve(theme):
    made = server.http.server.ThreadingHTTPServer((server.BIND, 0), Handler)
    made.daemon_threads = True
    made.app, made.bound, made.theme = App(), server.BIND, theme
    threading.Thread(target=made.serve_forever, daemon=True).start()
    return made


def chrome(binary, profile, address, *arguments, height=800):
    return subprocess.run([binary, "--headless=new", "--disable-gpu", "--hide-scrollbars", f"--user-data-dir={profile}",
                           f"--force-device-scale-factor={SCALE}", f"--window-size={WIDTH},{height}",
                           f"--virtual-time-budget={SETTLE}", *arguments, address],
                          capture_output=True, text=True, timeout=120)


def capture(binary, profile, address, target):
    """Save a picture of the page at address to target, as tall as the page. Why it could not be, or None."""
    drawn = chrome(binary, profile, address, "--dump-dom").stdout
    height, fault = re.search(r'data-height="(\d+)"', drawn), re.search(r'data-fault="(\w+)"', drawn)
    if not height or not fault or "Loading…" in drawn: return "the page did not finish drawing"
    if fault.group(1) == "yes": return "the page drew a fault, so its data here no longer fits the panel"
    chrome(binary, profile, address, f"--screenshot={target}", height=int(height.group(1)))
    return None if target.is_file() else "Chrome saved no picture"


def main(argv):
    parser = argparse.ArgumentParser(prog="dashboard-screenshots.py", description="Retake the README's screenshots "
                                     "of the dashboard, from made-up data.")
    parser.add_argument("--tabs", nargs="+", default=["queue", "farm"], choices=TABS, metavar="TAB",
                        help="the tabs to take (default: queue farm; also history and logs)")
    parser.add_argument("--theme", choices=("dark", "light"), default="dark", help="the page's theme (default dark)")
    parser.add_argument("--out", type=Path, default=CHECKOUT / "assets", metavar="FOLDER",
                        help="where the pictures go (default: this checkout's assets)")
    parser.add_argument("--chrome", default=CHROME, metavar="PATH", help="the Chromium browser to take them with")
    args = parser.parse_args(argv)
    if not Path(args.chrome).is_file():
        print(f"dashboard-screenshots: no browser at {args.chrome}; name one with --chrome", file=sys.stderr)
        return 2
    args.out.mkdir(parents=True, exist_ok=True)
    made, failed = serve(args.theme), 0
    try:
        with tempfile.TemporaryDirectory() as profile:
            for tab in args.tabs:
                target = args.out / f"dashboard-{'home' if tab == 'queue' else tab}.png"
                address = f"http://{server.BIND}:{made.server_address[1]}/r/{REPOSITORY}/{tab}"
                target.unlink(missing_ok=True)
                why = capture(args.chrome, profile, address, target)
                if why: failed += 1
                print(f"dashboard-screenshots: {tab}: {why or target}", file=sys.stderr if why else sys.stdout)
    finally:
        made.shutdown()
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
