#!/usr/bin/env python3
"""Retake the README's screenshots of the dashboard: assets/dashboard-TAB.png for each tab shown there, but for the
Now tab (`queue`) assets/dashboard-home.svg, its picture with the Flow map's dots moving in it as they do on the page.

    python3 tools/dashboard-screenshots.py                    Now (queue), Farm, Trends (history) and Activity, in the dark theme
    python3 tools/dashboard-screenshots.py --tabs logs activity
    python3 tools/dashboard-screenshots.py --theme light --out /tmp/shots

What is captured is the dashboard itself: this checkout's page, scripts and styles, served by the dashboard's own
server. Each tab is taken where the page opens, showing every repository at once. Only the data is made up, the same
every run: four projects and two Macs, busy enough that the Flow map shows each of its parts. lumen-ios tests a
batch on studio.local with a stacked round ahead of it waiting for a Mac, orbit-game tests on This Mac, fieldnotes'
gate waits for a Mac and harbor-mac is idle. Claude, Codex and a person have entries in them, and an entry of Codex's
came back within the hour. Each project's answers are made up as one repository's, and the panels' own
`combine` makes every repository's from them, as the server does. Times are counted back from now, since the page shows each as how long ago it
was. Three things differ from what `qk serve` sends: the data, the theme, which the page takes from the system and
this script fixes, and one more script on the page, which reports the page's height so that each picture is as
tall as its tab.

The Flow map is drawn by the page as an SVG whose dots move by SVG's own animation (animateMotion), with no script.
So Home is saved as an SVG holding its picture, with the map as the page drew it laid over the picture's own, where the
page says it is, and the stylesheet's Flow rules and the theme's colors written in. A README shows it as an image,
moving.

The pictures are taken by Google Chrome, headless, at twice the page's 1000 points. Name another Chromium browser
with --chrome. When a panel's data changes shape, change PANELS here to match: a tab that then draws a fault is
refused, not saved.

Run it directly on this Mac, as above, not through `qk admit`. It needs this Mac's Chrome, it writes into this
checkout, and it is light: one headless Chrome for a few seconds, which admission is not for. `qk admit` may send it
to another Mac, which may have no Chrome and would keep the pictures in its own checkout; there it refuses
(vocaro/queuekeeper#172).

    python3 tools/dashboard-screenshots.py --check-motion FOLDER

checks that Home's dots move. Headless Chrome's --virtual-time-budget does not advance SVG's animation, so two pictures
taken that way show the dots in one place however fine the image is. This instead opens assets/dashboard-home.svg (the
one in --out) in headless Chrome over its DevTools pipe, pauses the image's clock and sets it to each of MOTION_TIMES,
saves the Flow map then as FOLDER/flow-at-Ts.png, and says where each dot was: exit 0 when a dot is somewhere else at
each time, 1 when none moved (vocaro/queuekeeper#237)."""
import argparse
import base64
import copy
import html
import json
import math
import os
from pathlib import Path
import random
import re
import select
import subprocess
import sys
import tempfile
import threading
import time

CHECKOUT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CHECKOUT / "src/python"))
from queuekeeper.checkout import activate
activate(Path(__file__).resolve().parents[1])
from queuekeeper import health, host_health, lane_timing  # noqa: E402
from queuekeeper.remote import DISPATCHER  # noqa: E402
from queuekeeper_dashboard import panels, server  # noqa: E402

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
REPOSITORY, WIDTH, SCALE = "lumen-ios", 1000, 2
TABS = ("queue", "farm", "history", "activity", "logs", "about")
# The page's own script loads a panel's and asks for its data; this long is given it, in the page's own time.
SETTLE = 6000
# It also says where the Flow map is drawn and on what color, for the moving map laid over the picture of it.
MEASURE = """setInterval(() => { const page = document.documentElement.dataset;
  page.height = document.body.scrollHeight;
  page.fault = document.querySelector("main p.danger") ? "yes" : "no";
  const map = document.querySelector(".flow-map svg");
  if (!map) return;
  const box = map.getBoundingClientRect();
  let under = map;
  while (under && getComputedStyle(under).backgroundColor === "rgba(0, 0, 0, 0)") under = under.parentElement;
  page.flow = [box.left + scrollX, box.top + scrollY, box.width, box.height].join(" ");
  page.flowUnder = under ? getComputedStyle(under).backgroundColor : ""; }, 100);"""

NOW = time.time()
# The second Mac's memory: swap nearly full under pressure, and its largest processes (host_health.shortage).
SHORT = {"memoryBytes": 32 << 30, "swapUsedBytes": 14 << 30, "swapTotalBytes": 15 << 30, "memoryLevel": 2,
         "largest": [{"pid": 41661, "name": "Xcode", "residentBytes": 6 << 30, "seconds": 41580},
                     {"pid": 58763, "name": "swift-frontend", "residentBytes": 640 << 20, "seconds": 40},
                     {"pid": 312, "name": "mds_stores", "residentBytes": 410 << 20, "seconds": 9 * 86400}]}
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
    {"name": "fieldnotes", "level": "active", "headline": "Testing 1 entry",
     "detail": "The gate lane is waiting for a Mac.", "waiting": 0},
    {"name": "harbor-mac", "level": "idle", "headline": "Idle", "detail": "Nothing is waiting.", "waiting": 0},
]

# Each entry as the panel gives it, with whose it is (queue.agent) as "by".
WAITING = [
    {"id": E["0412"], "state": "testing", "subject": "Cache decoded thumbnails between launches",
     "branch": "claude/thumbnail-cache", "commits": 2, "submitted": ago(410), "by": "Claude"},
    {"id": E["0413"], "state": "testing", "subject": "Fix the share sheet on iPad",
     "branch": "claude/issue-42-share-sheet-ipad", "submitted": ago(365), "by": "Claude"},
    {"id": E["0415"], "state": "queued", "subject": "Add VoiceOver labels to the editor toolbar",
     "branch": "toolbar-labels", "submitted": ago(120), "by": "Person"},
    {"id": E["0416"], "state": "queued", "subject": "Show progress while a notebook imports",
     "branch": "claude/import-progress", "submitted": ago(45), "by": "Claude"},
]

RECENT = [
    {"id": "qk0398-offline-merge", "state": "failed", "subject": "Merge a note edited offline",
     "branch": "codex/offline-merge", "submitted": ago(2300), "settled": ago(1900), "by": "Codex",
     "why": "The gate failed lumen.test_sync.Tests.test_a_note_edited_offline_merges on this commit on top of origin/main."},
    {"id": E["0411"], "state": "landed", "subject": "Keep the scroll position when a note is edited",
     "branch": "claude/scroll-position", "landedAs": "4c9a1e7b02d3f8", "submitted": ago(1500), "settled": ago(540), "by": "Claude"},
]

PANELS = {
    "home": {
        "level": "active", "headline": "Testing 2 entries",
        "detail": "The gate lane is running on studio.local. 2 more waiting.", "began": ago(252),
        "counts": {"waiting": 2, "landed": 12, "cameBack": 1},
        "trend": {"waiting": [0, 0, 0, 0, 0, 0, 0, 0, 1, 2, 3, 2, 1, 1, 2, 4, 3, 2, 1, 0, 1, 2, 3, 2],
                  "landed": [0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 2, 1, 0, 1, 2, 1, 1, 0, 1, 1, 0, 1, 0, 0],
                  "cameBack": [0] * 23 + [1], "gateSeconds": [262, 248, 301, 255, 240, 236, 270, 252, 244, 238, 251, 249],
                  "medianGateSeconds": 250},
        "lines": [],
    },
    "queue": {
        "worker": True, "held": None,
        "round": {"name": ROUND, "base": "4c9a1e7b02d3f8", "entries": [E["0412"], E["0413"]], "bisect": None,
                  "gates": [
                      {"role": "batch", "lane": "gate", "mac": "studio.local", "began": ago(252),
                       "phase": "simulator tests, 212 of 340", "entries": [E["0412"]],
                       "log": ROUND + "-gate.log"},
                      # Testing ahead: the batch with the next entry stacked on it, waiting for a Mac with room.
                      {"role": "stacked", "lane": "gate", "began": ago(240), "waiting": "4 slots, which no Mac has free",
                       "waitingSince": ago(240), "entries": [E["0412"], E["0413"]]},
                  ]},
        "waiting": WAITING, "recent": RECENT, "ungated": [],
        # What is open: one entry that came back and was not queued again, and a test found flaky (queue.attention).
        "attention": [
            {"level": "warning", "title": "Came back", "at": ago(1900), "entry": "qk0398-offline-merge",
             "text": "Merge a note edited offline", "reason": "Its gate failed lumen.test_sync.Tests.test_a_note_edited_offline_merges.",
             "detail": "its gate failed lumen.test_sync.Tests.test_a_note_edited_offline_merges: fix it and add it again. While gate ran "
                       "on studio.local, the one-minute load was highest 14.2.",
             "log": BEFORE + "-gate.log"},
            {"level": "notice", "title": "Flaky", "at": ago(2900), "text": "lumen.test_sync.Tests.test_a_note_edited_offline_merges",
             "issue": "https://github.com/example/lumen/issues/41"}],
        "health": {"verdict": "good", "level": health.OK, "label": "All good",
                   "sentence": "Testing 2 entries and 2 wait behind them, the gate about as long as its usual 4m; "
                               "the last entry landed 9m ago.",
                   "signs": [health.sign("progress", health.OK, "testing 2 entries, 2 waiting", "Testing 2, 2 waiting"),
                             health.sign("pace", health.OK, "the last entry landed 9m ago", "Last landed 9m ago"),
                             health.sign("macs", health.OK, "1 slot free on This Mac"),
                             health.sign("devices", health.OK, "1 of 1 connected"),
                             health.sign("cameBack", health.OK, "1 of the last 8 came back")],
                   "forecast": {"waiting": 2, "clearsAt": ago(-540)}, "usualGateSeconds": 250, "findings": [],
                   "exitStatus": health.HEALTHY},
        "flow": {"agents": [{"name": "Claude", "waiting": 1, "testing": 2}, {"name": "Person", "waiting": 1, "testing": 0}],
                 "back": {"agent": "Codex", "id": "qk0398-offline-merge", "subject": "Merge a note edited offline",
                          "state": "failed", "settled": ago(1900)},
                 "macs": [{"name": "This Mac", "role": "here", "state": "observed", "used": 3, "most": 4, "gates": []},
                          {"name": "studio.local", "role": "second", "state": "observed", "used": 4, "most": 4, "gates": ["batch"]}]},
    },
    "farm": {
        "readAt": ago(4), "error": None,
        "hosts": [
            {"name": "This Mac", "role": "here", "state": "observed", "reasons": [],
             "load": {"cpuBusy": 38, "busyLimit": 85, "load": 6.4, "memory": "normal", "screen": "unlocked", "error": None,
                      "health": None, "shortage": None,
                      "hardware": {"model": "Mac15,9", "kind": "laptop"}},
             "slots": {"used": 3, "guaranteed": 1, "most": 4},
             "cells": [{"held": True, "project": "orbit-game", "guaranteed": True}, {"held": True, "project": "orbit-game", "guaranteed": False},
                       {"held": True, "project": "orbit-game", "guaranteed": False}, {"held": False, "project": None, "guaranteed": False}],
             "holders": [
                 {"id": "qk4156-orbit-game-gate", "label": "orbit-game · gate", "heldSeconds": 431, "project": "orbit-game",
                  "slots": 3, "wholeHost": False, "exclusive": False}],
             "waiters": []},
            {"name": "studio.local", "role": "second", "state": "observed", "reasons": [],
             "load": {"cpuBusy": 90, "busyLimit": 85, "load": 14.2, "memory": "warning", "screen": "unlocked", "error": None,
                      "health": host_health.warnings(SHORT, "studio.local")[0], "shortage": host_health.shortage(SHORT),
                      "hardware": {"model": "Mac14,13", "kind": "desktop"}},
             "slots": {"used": 4, "guaranteed": 2, "most": 4},
             "cells": [{"held": True, "project": "lumen-ios", "guaranteed": True}, {"held": True, "project": "lumen-ios", "guaranteed": True},
                       {"held": True, "project": "lumen-ios", "guaranteed": False}, {"held": True, "project": "lumen-ios", "guaranteed": False}],
             "holders": [
                 {"id": "qk8237-lumen-ios-gate", "label": "lumen-ios · gate lane", "heldSeconds": 252, "project": "lumen-ios",
                  "slots": 4, "wholeHost": True, "exclusive": False}],
             "waiters": [
                 {"id": "qk8240-lumen-ios-stacked", "label": "lumen-ios · gate lane, testing ahead", "waitedSeconds": 240,
                  "project": "lumen-ios", "reason": "No Mac has 4 slots free."},
                 {"id": "qk1948-fieldnotes-gate", "label": "fieldnotes · gate lane", "waitedSeconds": 95, "project": "fieldnotes",
                  "reason": "No Mac has 2 slots free."}]},
        ],
    },
    "history": {
        "today": {"landed": 12, "cameBack": 0, "failed": 0, "conflict": 0, "removed": 1, "flaky": 0, "rounds": 6,
                  "medianGateSeconds": 252},
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
PANELS["about"] = panels.load("about").answer()
# What lumen-ios's queue did: the round being tested, a hold and its resume, a round whose entry came back, its gate
# failing a test it broke while another test's failure was a flake a rerun passed, and earlier a batch of two that landed.
PANELS["activity"] = {"total": 11, "feed": [
    {"kind": "round", "time": ago(252), "round": ROUND, "head": None, "flaky": [],
     "entries": [{"id": E["0412"], "subject": "Show the sync status in the toolbar", "branch": "claude/sync-status", "state": "testing"}],
     "lanes": [{"lane": "lint", "role": "batch", "status": 0, "seconds": 38, "log": ROUND + "-lint.log"},
               {"lane": "gate", "role": "batch", "status": None, "seconds": 252, "log": ROUND + "-gate.log"}]},
    {"kind": "notice", "time": ago(900), "text": "The queue resumed after it was held for a session: the gate gave no verdict twice", "level": "plain"},
    {"kind": "notice", "time": ago(1500), "text": "The queue was held for a session: the gate gave no verdict twice", "level": "warning"},
    {"kind": "round", "time": ago(1900), "round": BEFORE, "head": None,
     "flaky": [{"test": "lumen.test_search.Tests.test_results_update_while_typing", "issue": "https://github.com/example/lumen/issues/41"}],
     "entries": [{"id": "qk0398-offline-merge", "subject": "Merge a note edited offline", "branch": "codex/offline-merge", "state": "back",
                  "why": "The gate failed lumen.test_sync.Tests.test_a_note_edited_offline_merges on this commit on top of origin/main."}],
     "lanes": [{"lane": "lint", "role": "batch", "status": 0, "seconds": 41, "log": BEFORE + "-lint.log"},
               {"lane": "gate", "role": "batch", "status": 1, "seconds": 288, "log": BEFORE + "-gate.log"}]},
    {"kind": "round", "time": ago(2600), "round": "20261116-090000-4c9a1e7b", "head": None, "flaky": [],
     "entries": [{"id": E["0411"], "subject": "Keep the scroll position when a note is edited", "branch": "claude/scroll-position",
                  "state": "landed", "landedAs": "4c9a1e7b02d3f8"}],
     "lanes": [{"lane": "lint", "role": "batch", "status": 0, "seconds": 36, "log": None},
               {"lane": "gate", "role": "batch", "status": 0, "seconds": 251, "log": None}]},
    {"kind": "round", "time": ago(3400), "round": "20261116-084500-18f6b2d0", "head": None, "flaky": [],
     "entries": [{"id": E["0409"], "subject": "Queue exports while another is running", "branch": "claude/export-queue",
                  "state": "landed", "landedAs": "18f6b2d07c4e19"},
                 {"id": E["0410"], "subject": "Pick tags from a list as you type", "branch": "codex/tag-picker",
                  "state": "landed", "landedAs": "a7c03e59b81d26"}],
     "lanes": [{"lane": "lint", "role": "batch", "status": 0, "seconds": 40, "log": None},
               {"lane": "gate", "role": "batch", "status": 0, "seconds": 274, "log": None}]}]}
# Each of the last two weeks, oldest first, so that the last 7 days have 7 before them to be set against.
PANELS["history"]["daily"] = [
    {"day": time.mktime(time.localtime(NOW - 86400 * back)[:3] + (0, 0, 0, 0, 0, -1)), "landed": 9 + (back * 5) % 7 + (0 if back > 6 else 3),
     "cameBack": (back * 3) % 4 // 2 + (1 if back > 6 else 0), "flaky": back % 3 // 2, "medianGateSeconds": 250 + (back * 13) % 40,
     "gateShares": [round(0.85 + ((back * 7 + step) % 9) / 20, 2) for step in range(4)]} for back in range(13, -1, -1)]
# The farm's last hour, a reading every 10 seconds: each Mac's load drifting, and the slots by project.
PANELS["farm"]["samples"] = [
    {"at": ago(3600 - 10 * step), "most": 8,
     "macs": {"here": {"cpuBusy": 38, "load": round(5 + 2 * ((step * 7) % 11) / 11, 1)},
              "second": {"cpuBusy": 90, "load": round(11 + 4 * ((step * 5) % 13) / 13, 1)}},
     "projects": {"orbit-game": 3 if step > 320 else 1 + (step // 90) % 2, "lumen-ios": 4 if step > 335 else 2 if step > 120 else (step // 40) % 3,
                  "fieldnotes": 1 if 150 < step < 260 else 0}}
    for step in range(361)]
# The Trends tab's samples are drawn at random, as real work is uneven, from a seed, so that every run draws the same.
RNG = random.Random(2026)


def midnight(back):
    """The local midnight that began the day `back` days before today."""
    return time.mktime(time.localtime(NOW - 86400 * back)[:3] + (0, 0, 0, 0, 0, -1))


def counted(mean):
    """A count of landings in an hour whose mean is `mean`, as a Poisson draw."""
    count, left = 0, RNG.random()
    while left > math.exp(-mean):
        count, left = count + 1, left * RNG.random()
    return count


def landed_by_hour(per_day):
    """One repository's landings in each hour of the last 7 days. Agents land work at any hour, a little more while
    their people are awake, and in bursts, so most hours have one or none and a few have several; today stops now."""
    hour_now = time.localtime(NOW).tm_hour
    return [{"day": midnight(back), "hours": [0 if back == 0 and hour > hour_now else
                                              counted(per_day / 24 * (1.4 if 8 <= hour <= 23 else 0.5) * RNG.choice((0.3, 0.8, 1, 1, 1.5, 2.8)))
                                              for hour in range(24)]} for back in range(6, -1, -1)]


def gate_times(median, count=40):
    """The seconds of a repository's last passing gates: spread around its median, with now and then a slow one."""
    return [round(RNG.lognormvariate(math.log(median), 0.3) * (RNG.uniform(1.8, 3) if RNG.random() < 0.08 else 1)) for _ in range(count)]


def lane_runs():
    """Today's lane runs on each Mac, at most the last 7 hours: bursts of rounds back to back, quiet spells between,
    gates of uneven length, now and then one that fails or goes unmeasured, and a short docs lane ahead of some.
    studio.local runs lumen-ios's; This Mac runs lumen-ios's and orbit-game's. Returns each repository's runs."""
    start, runs = max(midnight(0) + 300, NOW - 7 * 3600), {"lumen-ios": [], "orbit-game": []}
    for mac in ("studio.local", None):
        at = start + RNG.uniform(0, 1800)
        while True:
            name = "lumen-ios" if mac or RNG.random() < 0.55 else "orbit-game"
            if RNG.random() < 0.3:
                runs[name].append({"began": at, "ended": at + RNG.uniform(25, 60), "lane": "docs", "status": 0, "mac": mac,
                                   "round": None, "log": None})
                at = runs[name][-1]["ended"] + RNG.uniform(5, 20)
            seconds = RNG.lognormvariate(math.log(270 if name == "lumen-ios" else 440), 0.3)
            ended = at + seconds
            if ended > NOW - 400: break
            status = RNG.choices((0, 1, 3), (86, 10, 4))[0]
            runs[name].append({"began": at, "ended": ended, "lane": "gate", "status": status, "mac": mac, "round": None, "log": None})
            at = ended + (RNG.uniform(15, 90) if RNG.random() < 0.65 else RNG.uniform(900, 3600))
    return {name: sorted(found, key=lambda run: run["ended"]) for name, found in runs.items()}


def lane_timing_rows(lanes):
    """Each lane's timing baseline as the Trends tab's baselines give it, from (lane, median seconds, runs, the runs
    after a stale mark or None, fail threshold or None): its runs spread around the median a day apart, those before
    a stale mark faster, as before work was added to the lane on purpose."""
    rows = []
    for lane, median, count, after, fail in lanes:
        times = [ago(86400 * (count - index) / 2) for index in range(count)]
        stale = None if after is None else times[count - after] - 600
        runs = [[at, round(RNG.lognormvariate(math.log(median * (0.7 if stale and at < stale else 1)), 0.12))] for at in times]
        counted = [run for run in runs if stale is None or run[0] > stale][-lane_timing.RECENT:]
        recent = [seconds for _, seconds in counted]
        found = None if len(recent) < lane_timing.FEWEST else {
            "p90": lane_timing.percentile(recent), "median": sorted(recent)[len(recent) // 2], "samples": len(recent), "staleSince": stale}
        limit = lane_timing.fail_seconds({"fail": fail}, found) if fail else None
        rows.append({"lane": lane, "runs": runs, "baseline": found, "fail": fail, "failSeconds": round(limit, 1) if limit else None,
                     "minSamples": lane_timing.MIN_SAMPLES if fail else None, "staleSince": stale,
                     "countedFrom": counted[0][0] if counted else None})
    return rows


RUNS = lane_runs()
PANELS["history"].update(
    yesterday={"landed": 9, "cameBack": 1, "failed": 1, "conflict": 0, "removed": 0, "flaky": 1, "rounds": 7, "medianGateSeconds": 281},
    gates=RUNS["lumen-ios"], landings=landed_by_hour(14), gateSeconds=gate_times(260),
    baselines=lane_timing_rows([("docs", 40, 30, None, None), ("gate", 265, 30, None, "+25%")]), fewest=lane_timing.FEWEST)
PANELS["farm"]["deviceRecord"] = {"devices": [
    {"name": "iPhone", "kind": "iphone", "model": "iPhone 17 Pro", "state": "held", "owner": "release-tests",
     "purpose": "Release checks",
     "releaseBy": None, "waiting": 0, "connection": "connected", "transport": "localNetwork"}],
    "deferredTo": None, "error": None, "connectionError": None, "checkedAt": ago(4)}

# The round's gate log: a gate still running whose suites have passed but one, which failed a test.
LOG += "".join(f"== lumen.test_{name}.Tests[1]: OK ({count} tests, 0.{count}s)\ntest_{name} (lumen.test_{name}.Tests.test_{name}) ... ok\n"
               for count, name in enumerate(("layout", "fonts", "colors", "pages", "links", "search", "export", "import"), 2))
LOG += """== lumen.test_sync.Tests[1]: FAILED (failures=1) (3 tests, 4.2s)
FAIL: test_a_note_edited_offline_merges (lumen.test_sync.Tests.test_a_note_edited_offline_merges)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "tests/test_sync.py", line 88, in test_a_note_edited_offline_merges
AssertionError: 'draft' != 'merged'
remote: heartbeat: gate on studio.local, 4m 00s in: its log grew by 212 lines, the latest 3s ago
"""
LOGS = [{"name": "worker.log", "size": 48213, "modified": ago(3)},
        {"name": ROUND + "-gate.log", "size": len(LOG), "modified": ago(3), "round": ROUND[:15], "commit": ROUND[16:],
         "lane": "gate", "running": True},
        {"name": ROUND + "-docs.log", "size": 1904, "modified": ago(240), "round": ROUND[:15], "commit": ROUND[16:],
         "lane": "docs", "status": 0, "seconds": 41, "mac": None},
        {"name": BEFORE + "-gate.log", "size": 291377, "modified": ago(1610), "round": BEFORE[:15], "commit": BEFORE[16:],
         "lane": "gate", "status": 1, "seconds": 288, "mac": "studio.local"},
        {"name": BEFORE + "-rerun-1-1.log", "size": 6973, "modified": ago(1580), "round": BEFORE[:15], "commit": BEFORE[16:],
         "lane": "rerun", "test": 1, "attempt": 1, "status": 0},
        {"name": BEFORE + "-rerun-1-2.log", "size": 6973, "modified": ago(1570), "round": BEFORE[:15], "commit": BEFORE[16:],
         "lane": "rerun", "test": 1, "attempt": 2, "status": 0}]


ORBIT = time.strftime("%Y%m%d-%H%M%S", time.localtime(ago(431))) + "-9e21b4c0"
NOTES = time.strftime("%Y%m%d-%H%M%S", time.localtime(ago(95))) + "-51f0c3e8"
LEVEL = {"id": "qk0420-level-select", "state": "testing", "subject": "Remember the last level played",
         "branch": "codex/level-select", "submitted": ago(980), "by": "Codex"}
HINTS = {"id": "qk0423-level-hints", "state": "queued", "subject": "Offer a hint after three failed tries",
         "branch": "codex/level-hints", "submitted": ago(210), "by": "Codex"}
LEVELS = {"id": "qk0381-level-timer", "state": "landed", "subject": "Pause the level timer while the game is in the background",
          "branch": "codex/level-timer", "landedAs": "3b8e51c0d7a2f9", "submitted": ago(3900), "settled": ago(2900), "by": "Codex"}
SYNC = {"id": "qk0402-sync-conflicts", "state": "testing", "subject": "Show both versions when a sync conflicts",
        "branch": "worktree-agent-a5181cd05a8167cb", "submitted": ago(160), "by": "Claude"}
JOURNAL = {"id": "qk0398-journal-export", "state": "landed", "subject": "Export a journal as Markdown",
           "branch": "claude/journal-export", "landedAs": "77c1e0d4a9b2f3", "submitted": ago(5200), "settled": ago(4100), "by": "Claude"}
DOCK = {"id": "qk0217-dock-badge", "state": "landed", "subject": "Badge the Dock icon with the unread count",
        "branch": "dock-badge", "landedAs": "e3a90b7c51d2f4", "submitted": ago(7900), "settled": ago(6800), "by": "Person"}


def others():
    """orbit-game's, fieldnotes' and harbor-mac's own answers, from lumen-ios's: orbit-game testing an entry on This
    Mac with another waiting, fieldnotes' gate waiting for a Mac, and harbor-mac idle, having landed earlier in the day."""
    orbit, notes, harbor = copy.deepcopy(PANELS), copy.deepcopy(PANELS), copy.deepcopy(PANELS)
    for found, landed in ((orbit, 5), (notes, 7), (harbor, 4)):
        found["queue"]["overview"].update(counts={"waiting": 0, "landed": landed, "cameBack": 0}, lines=[])
        found["queue"]["overview"]["trend"].update(
            landed=[(hour * 7 + landed * 3) % 9 // 6 if 8 <= hour <= 20 else 0 for hour in range(24)],
            waiting=[(hour * 3 + landed) % 5 // 4 for hour in range(24)])
        own = {id(notes): JOURNAL, id(harbor): DOCK}.get(id(found), LEVELS)
        found["queue"].update(recent=[own], waiting=[], round=None, attention=[])
        found["queue"]["flow"].update(agents=[], back=None)
        for mac in found["queue"]["flow"]["macs"]: mac["gates"] = []
        found["history"].update(gates=[], daily=[{**day, "landed": day["landed"] // 2, "cameBack": 0} for day in found["history"]["daily"]])
        found["activity"] = {"total": 1, "feed": [{"kind": "round", "time": own["settled"] - 300, "round": ROUND, "head": None, "flaky": [],
            "entries": [{"id": own["id"], "subject": own["subject"], "branch": own["branch"], "state": "landed", "landedAs": own["landedAs"]}],
            "lanes": [{"lane": "gate", "role": "batch", "status": 0, "seconds": 280, "log": None}]}]}
        found["history"].update(gateSeconds=gate_times({id(orbit): 440, id(notes): 92, id(harbor): 150}[id(found)]),
                                landings=landed_by_hour({id(orbit): 6, id(notes): 9, id(harbor): 4}[id(found)]),
                                baselines=lane_timing_rows({id(orbit): [("gate", 440, 30, 12, "+2m")], id(notes): [("gate", 92, 20, 3, None)],
                                                            id(harbor): [("gate", 150, 24, None, None)]}[id(found)]))
        found["queue"]["health"]["signs"][4] = health.sign("cameBack", health.OK, "none of the last 8 came back")
        found["history"]["today"].update(landed=landed, rounds=landed, removed=0,
                                         medianGateSeconds=sorted(found["history"]["gateSeconds"])[len(found["history"]["gateSeconds"]) // 2])
    orbit["queue"]["overview"].update(level="active", headline="Testing 1 entry", detail="The gate lane is running on This Mac.",
                                      began=ago(431), counts={"waiting": 1, "landed": 5, "cameBack": 0})
    orbit["queue"].update(waiting=[LEVEL, HINTS], round={"name": ORBIT, "base": "9e21b4c0f2a7d1", "entries": [LEVEL["id"]], "bisect": None,
        "gates": [{"role": "batch", "lane": "gate", "mac": "This Mac", "began": ago(431), "phase": "unit tests, 88 of 140",
                   "entries": [LEVEL["id"]], "log": ORBIT + "-gate.log"}]})
    orbit["queue"]["flow"]["agents"] = [{"name": "codex", "waiting": 0, "testing": 1}]
    orbit["queue"]["flow"]["macs"][0]["gates"] = ["batch"]
    orbit["queue"]["overview"]["trend"]["medianGateSeconds"] = 445
    orbit["queue"]["health"].update(sentence="Testing 1 entry, the gate about as long as its usual 7m.",
                                    forecast={"waiting": 1, "clearsAt": ago(-60)})
    orbit["queue"]["health"]["signs"][0] = health.sign("progress", health.OK, "testing 1 entry, 1 waiting", "Testing 1, 1 waiting")
    notes["queue"]["overview"].update(level="active", headline="Testing 1 entry", detail="The gate lane is waiting for a Mac.",
                                      began=ago(95), counts={"waiting": 0, "landed": 7, "cameBack": 0})
    notes["queue"].update(waiting=[SYNC], round={"name": NOTES, "base": "51f0c3e8a92d7b", "entries": [SYNC["id"]], "bisect": None,
        "gates": [{"role": "batch", "lane": "gate", "began": ago(95), "waiting": "2 slots, which no Mac has free",
                   "waitingSince": ago(95), "entries": [SYNC["id"]]}]})
    notes["queue"]["health"].update(sentence="Testing 1 entry, its gate waiting 1m 35s for a Mac with room.", forecast={"waiting": 1, "clearsAt": ago(-420)})
    notes["queue"]["health"]["signs"][0] = health.sign("progress", health.OK, "testing 1 entry", "Testing 1")
    notes["activity"]["feed"].insert(0, {"kind": "round", "time": ago(95), "round": NOTES, "head": None, "flaky": [],
        "entries": [{"id": SYNC["id"], "subject": SYNC["subject"], "branch": SYNC["branch"], "state": "testing"}],
        "lanes": [{"lane": "gate", "role": "batch", "status": None, "seconds": 95, "log": None}]})
    harbor["queue"]["health"]["signs"][0] = health.sign("progress", health.IDLE, "nothing is waiting", "Nothing waiting")
    harbor["queue"]["health"]["signs"][1] = health.sign("pace", health.OK, "the last entry landed 1h 53m ago", "Last landed 1h 53m ago")
    orbit["activity"]["feed"].insert(0, {"kind": "round", "time": ago(431), "round": ORBIT, "head": None, "flaky": [],
        "entries": [{"id": LEVEL["id"], "subject": LEVEL["subject"], "branch": LEVEL["branch"], "state": "testing"}],
        "lanes": [{"lane": "gate", "role": "batch", "status": None, "seconds": 431, "log": ORBIT + "-gate.log"}]})
    orbit["history"]["gates"] = RUNS["orbit-game"]
    harbor["queue"]["overview"].update(level="idle", headline="Idle", detail="Nothing is waiting.", began=None)
    harbor["queue"]["health"].update(verdict="quiet", level=health.IDLE, label="All quiet", forecast=None,
                                     sentence="Nothing is waiting. The last entry landed 1h 53m ago.")
    return {"orbit-game": orbit, "fieldnotes": notes, "harbor-mac": harbor}


MADE_UP = {REPOSITORY: PANELS, **others()}
OTHER_LOGS = {"orbit-game": [{"name": "worker.log", "size": 20117, "modified": ago(5)},
                             {"name": ORBIT + "-gate.log", "size": 88213, "modified": ago(5), "round": ORBIT[:15], "commit": ORBIT[16:],
                              "lane": "gate", "running": True}],
              "fieldnotes": [{"name": "worker.log", "size": 9120, "modified": ago(95)}],
              "harbor-mac": [{"name": "worker.log", "size": 6342, "modified": ago(6800)}]}


def everywhere(tab, query):
    """What a tab is answered for every repository at once, from each one's made-up answer."""
    if tab == "farm": return PANELS["farm"]
    if tab == "about": return panels.load("about").answer()
    if tab == "logs":
        listed = sorted(({**log, "repository": name} for name, found in ((REPOSITORY, LOGS), *OTHER_LOGS.items()) for log in found),
                        key=lambda log: (log["name"] != "worker.log", -log["modified"]))
        if not query.get("name"): return {"logs": listed, "unread": []}
        found = logs(query)
        return {**found, "repository": query.get("repository"), "log": {**found["log"], "repository": query.get("repository")},
                "logs": listed, "unread": []}
    return panels.load(tab).combine([(name, found[tab], None) for name, found in MADE_UP.items()])


def logs(query):
    """What the Logs panel is answered: the logs, and the round's gate log, indexed, once the page asks for one."""
    if not query.get("name"): return {"logs": LOGS}
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "gate.log"
        path.write_text(LOG)
        found = panels.load("logs").index(path, len(LOG))
    offset = min(int(query.get("offset", 0)), len(LOG))
    return {"name": query["name"], "size": len(LOG), "offset": offset, "next": len(LOG), "text": LOG[offset:], "line": 1 if not offset else None,
            "path": "/Users/you/lumen/.git/queuekeeper-queue/logs/" + query["name"], "ask": query.get("ask"), "logs": LOGS,
            "log": next((log for log in LOGS if log["name"] == query["name"]), {"name": query["name"]}), "reruns": [], "flaky": {},
            "index": {key: found[key] for key in ("lines", "marks", "failures", "summary", "size")}}


class App(server.App):
    """The dashboard's own, answering with the data above in place of a queue's."""

    def __init__(self):
        pass

    def answer(self, path, query):
        parts = [part for part in path.split("/") if part]
        if parts == ["panels"]: return panels.listed()
        if parts == ["repositories"]: return REPOSITORIES
        if len(parts) == 3 and parts[:2] == ["all", "panel"] and parts[2] in TABS: return everywhere(parts[2], query)
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
    # The commit a page is served from is none here: the page reloads only when a server says it moved.
    made.app, made.bound, made.theme, made.commit = App(), server.BIND, theme, None
    threading.Thread(target=made.serve_forever, daemon=True).start()
    return made


def chrome(binary, profile, address, *arguments, height=800):
    return subprocess.run([binary, "--headless=new", "--disable-gpu", "--hide-scrollbars", f"--user-data-dir={profile}",
                           f"--force-device-scale-factor={SCALE}", f"--window-size={WIDTH},{height}",
                           f"--virtual-time-budget={SETTLE}", *arguments, address],
                          capture_output=True, text=True, timeout=120)


def capture(binary, profile, address, target):
    """Save a picture of the page at address to target, as tall as the page. (Why it could not be, or None; the page
    as drawn.)"""
    drawn = chrome(binary, profile, address, "--dump-dom").stdout
    height, fault = re.search(r'data-height="(\d+)"', drawn), re.search(r'data-fault="(\w+)"', drawn)
    if not height or not fault or "Loading…" in drawn: return "the page did not finish drawing", drawn
    if fault.group(1) == "yes": return "the page drew a fault, so its data here no longer fits the panel", drawn
    chrome(binary, profile, address, f"--screenshot={target}", height=int(height.group(1)))
    # The page as drawn at the picture's height, so that where it says its Flow map is, is where the picture has it.
    drawn = chrome(binary, profile, address, "--dump-dom", height=int(height.group(1))).stdout
    return (None if target.is_file() else "Chrome saved no picture"), drawn


def flow(drawn, theme):
    """The page's Flow map as SVG of its own: (its label, its style, what it draws, its width and height in its own
    units), the map as drawn with its links and their tips taken out, and the stylesheet's Flow rules and the theme's
    colors. None when the page drew no map."""
    found = re.search(r'<div class="flow-map">(<svg\b.*?</svg>)</div>', drawn, re.S)
    if not found: return None
    drawing = re.sub(r"</?a\b[^>]*>|<title>.*?</title>", "", found.group(1), flags=re.S)
    width, height = map(float, re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', drawing).groups())
    css = (CHECKOUT / "plugins/visibility-dashboard/src/queuekeeper_dashboard/static/app.css").read_text()
    light, dark = re.findall(r":root \{([^}]*--p1:[^}]*)\}", css)[:2]
    # Each rule for the map's insides, which the map's own svg then holds; the box around it is the page's. A rule
    # under an @media stays under it, as the one that stills the dots for reduced motion; the theme's are above.
    inside = lambda text: [chosen + " {" + body + "}" for selector, body in re.findall(r"(\.flow-map [^{}]*)\{([^}]*)\}", text)
                           if (chosen := selector.replace(".flow-map ", "").strip()) and not chosen.endswith("svg")]
    media = re.compile(r"@media([^{]*)\{((?:[^{}]*\{[^}]*\})*[^{}]*)\}")
    rules = inside(media.sub("", css)) + [f"@media{condition}{{ {' '.join(inside(body))} }}" for condition, body in media.findall(css)
                                          if inside(body) and "prefers-color-scheme" not in condition]
    style = "\n".join([f"svg {{{light}{dark if theme == 'dark' else ''}}}",
                       "svg { font-family: -apple-system, system-ui, sans-serif; font-variant-numeric: tabular-nums; }", *rules])
    opened = re.match(r"<svg\b[^>]*>", drawing).group(0)
    label = html.unescape(re.search(r'aria-label="([^"]*)"', opened).group(1))
    return label, style, drawing[len(opened):-len("</svg>")], width, height


def moving(picture, drawn, theme):
    """The picture of the page as an SVG, with the Flow map laid over the picture's own map where the page drew it,
    on the color it was drawn on, so that its dots move as they do on the page (by SVG's own animation, which an
    image plays) and stand still where the viewer asks for reduced motion. None when the page drew no map."""
    found, where = flow(drawn, theme), re.search(r'data-flow="([\d. ]+)"', drawn)
    under, height = re.search(r'data-flow-under="([^"]*)"', drawn), re.search(r'data-height="(\d+)"', drawn)
    if not found or not where or not height: return None
    label, style, drawing, width, tall = found
    x, y, w, h = map(float, where.group(1).split())
    page = int(height.group(1))
    encoded = base64.b64encode(picture).decode()
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {page}" width="{WIDTH}" height="{page}" role="img">\n'
            f"<title>The dashboard, its Flow map moving: {html.escape(label)}</title>\n<style>\n{style}\n</style>\n"
            f'<image width="{WIDTH}" height="{page}" href="data:image/png;base64,{encoded}"/>\n'
            f'<svg x="{x:g}" y="{y:g}" width="{w:g}" height="{h:g}" viewBox="0 0 {width:g} {tall:g}">\n'
            f'<rect width="{width:g}" height="{tall:g}" fill="{html.escape(under.group(1) if under else "var(--card)")}"/>\n'
            + drawing + "\n</svg>\n</svg>\n")


# The image's clock, in seconds, set for each still: apart by less than every dot's shortest round, 1.6 seconds.
MOTION_TIMES = (0, 0.5)
# Where each dot is, and the Flow map's box, after the clock is set and the image drawn again at that time.
DOTS = """(async () => { const image = document.documentElement; image.pauseAnimations(); image.setCurrentTime(%s);
  await new Promise(done => setTimeout(done, 200));
  const map = image.querySelector("svg").getBoundingClientRect();
  return {map: [map.x, map.y, map.width, map.height], dots: [...image.querySelectorAll("circle.moving")].map(dot => {
    const box = dot.getBoundingClientRect(); return [+(box.x + box.width / 2).toFixed(1), +(box.y + box.height / 2).toFixed(1)]; })}; })()"""


class DevTools:
    """A headless Chrome driven over the DevTools protocol on a pipe (--remote-debugging-pipe: it reads from its file
    3 and writes to its file 4, each message ended by a NUL), with one tab open at address."""

    def __init__(self, binary, profile, address, timeout=60):
        theirs_in, self.out = os.pipe()
        self.into, theirs_out = os.pipe()

        def pipes():
            reads, writes = os.dup(theirs_in), os.dup(theirs_out)
            os.dup2(reads, 3); os.dup2(writes, 4)
        self.process = subprocess.Popen([binary, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--remote-debugging-pipe",
                                         f"--user-data-dir={profile}", f"--window-size={WIDTH},800", "about:blank"],
                                        pass_fds=(3, 4, theirs_in, theirs_out), preexec_fn=pipes,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.close(theirs_in); os.close(theirs_out)
        self.timeout, self.sent, self.unread, self.session = timeout, 0, b"", None
        try:
            target = self.call("Target.createTarget", url=address)["targetId"]
            self.session = self.call("Target.attachToTarget", targetId=target, flatten=True)["sessionId"]
            deadline = time.monotonic() + timeout
            while self.evaluate("document.readyState") != "complete":
                if time.monotonic() > deadline: raise TimeoutError(f"Chrome did not finish loading {address}")
                time.sleep(0.1)
        except BaseException:
            self.close()
            raise

    def call(self, method, **params):
        self.sent += 1
        message = {"id": self.sent, "method": method, "params": params, **({"sessionId": self.session} if self.session else {})}
        os.write(self.out, json.dumps(message).encode() + b"\0")
        while True:
            while b"\0" not in self.unread:
                if not select.select([self.into], [], [], self.timeout)[0]: raise TimeoutError(f"Chrome did not answer {method}")
                read = os.read(self.into, 1 << 16)
                if not read: raise ConnectionError(f"Chrome closed its pipe before answering {method}")
                self.unread += read
            line, self.unread = self.unread.split(b"\0", 1)
            answer = json.loads(line)
            if answer.get("id") != self.sent: continue
            if "error" in answer: raise RuntimeError(f"{method}: {answer['error'].get('message')}")
            return answer["result"]

    def evaluate(self, expression):
        return self.call("Runtime.evaluate", expression=expression, awaitPromise=True, returnByValue=True)["result"].get("value")

    def close(self):
        self.process.kill(); self.process.wait()
        os.close(self.out); os.close(self.into)


def dots(binary, image, folder):
    """Where each of the Flow map's dots is in the SVG image, at each of MOTION_TIMES on its clock, saving the map at
    each time to folder as flow-at-Ts.png: [(the time, the still, [[x, y] of each dot])]."""
    found = []
    with tempfile.TemporaryDirectory() as profile:
        browser = DevTools(binary, profile, image.resolve().as_uri())
        try:
            for at in MOTION_TIMES:
                drawn = browser.evaluate(DOTS % at)
                x, y, width, height = drawn["map"]
                still = folder / f"flow-at-{at:g}s.png"
                shot = browser.call("Page.captureScreenshot", captureBeyondViewport=True,
                                    clip={"x": x, "y": y, "width": width, "height": height, "scale": SCALE})
                still.write_bytes(base64.b64decode(shot["data"]))
                found.append((at, still, drawn["dots"]))
        finally:
            browser.close()
    return found


def moved(found):
    """Why the dots found by dots() do not show the map moving, or None when they do: there are some, and some dot is
    somewhere else at each time than at the time before."""
    if not found or not found[0][2]: return "the image has no moving dots"
    for (before, _, was), (at, _, now) in zip(found, found[1:]):
        if was == now: return f"every dot is where it was at {before:g}s again at {at:g}s"
    return None


def check_motion(binary, image, folder):
    if not image.is_file():
        print(f"dashboard-screenshots: no image at {image}; take it first, or name its folder with --out", file=sys.stderr)
        return 2
    folder.mkdir(parents=True, exist_ok=True)
    found = dots(binary, image, folder)
    for at, still, where in found:
        print(f"dashboard-screenshots: at {at:g}s: {still}: dots at " + " ".join(f"{x:g},{y:g}" for x, y in where))
    why = moved(found)
    print(f"dashboard-screenshots: {image}: " + (why or "the dots move"), file=sys.stderr if why else sys.stdout)
    return 1 if why else 0


def main(argv):
    parser = argparse.ArgumentParser(prog="dashboard-screenshots.py", description="Retake the README's screenshots "
                                     "of the dashboard, from made-up data.")
    parser.add_argument("--tabs", nargs="+", default=["queue", "farm", "history", "activity"], choices=TABS, metavar="TAB",
                        help="the tabs to take (default: queue farm history; also activity, logs and about)")
    parser.add_argument("--theme", choices=("dark", "light"), default="dark", help="the page's theme (default dark)")
    parser.add_argument("--out", type=Path, default=CHECKOUT / "assets", metavar="FOLDER",
                        help="where the pictures go (default: this checkout's assets)")
    parser.add_argument("--chrome", default=CHROME, metavar="PATH", help="the Chromium browser to take them with")
    parser.add_argument("--check-motion", type=Path, metavar="FOLDER", help="take no pictures; check that the Flow map's "
                        "dots in --out's dashboard-home.svg move, saving a still of the map at each time to FOLDER")
    args = parser.parse_args(argv)
    if os.environ.get(DISPATCHER):
        print(f"dashboard-screenshots: sent to this Mac by {os.environ[DISPATCHER]}, whose checkout the pictures "
              "belong in; run python3 tools/dashboard-screenshots.py directly there, not through qk admit",
              file=sys.stderr)
        return 2
    if not Path(args.chrome).is_file():
        print(f"dashboard-screenshots: no browser at {args.chrome}; name one with --chrome", file=sys.stderr)
        return 2
    if args.check_motion: return check_motion(args.chrome, args.out / "dashboard-home.svg", args.check_motion)
    args.out.mkdir(parents=True, exist_ok=True)
    made, failed = serve(args.theme), 0
    try:
        with tempfile.TemporaryDirectory() as profile:
            for tab in args.tabs:
                # Home is an SVG that holds its picture, under a Flow map that moves; it is taken as a picture first.
                target = Path(profile) / "home.png" if tab == "queue" else args.out / f"dashboard-{tab}.png"
                address = f"http://{server.BIND}:{made.server_address[1]}/" + ("" if tab == "queue" else f"all/{tab}")
                target.unlink(missing_ok=True)
                why, drawn = capture(args.chrome, profile, address, target)
                if why: failed += 1
                if why or tab != "queue": print(f"dashboard-screenshots: {tab}: {why or target}", file=sys.stderr if why else sys.stdout)
                if tab != "queue" or why: continue
                drawing, home = moving(target.read_bytes(), drawn, args.theme), args.out / "dashboard-home.svg"
                if drawing is None:
                    failed += 1
                    print("dashboard-screenshots: queue: the page drew no Flow map", file=sys.stderr)
                    continue
                home.write_text(drawing)
                print(f"dashboard-screenshots: queue: {home}")
    finally:
        made.shutdown()
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
