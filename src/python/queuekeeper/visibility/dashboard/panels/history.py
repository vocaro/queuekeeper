"""The History tab: the day's totals and the journal's last events, newest first."""
import statistics
import time

from .... import events

TITLE, ORDER = "History", 40
MOST = 200
LEVELS = {"entry.failed": "danger", "entry.conflict": "danger", "queue.held": "warning", "test.flaky": "warning",
          "gate.stuck": "warning",
          "entry.landed": "success", "batch.pushed": "success"}
STAMP = len("2026-11-12 09:30:19 ")


def shown(event):
    return {"time": event.get("time"), "kind": event["kind"], "text": events.words(event)[STAMP:],
            "level": LEVELS.get(event["kind"], "plain")}


def totals(journal, now):
    start = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
    day = [event for event in journal if event.get("time", 0) >= start]
    kinds = [event["kind"] for event in day]
    gates = [event["seconds"] for event in day if event["kind"] == "gate.ended" and event.get("lane") == "gate"
             and event.get("status") == 0 and isinstance(event.get("seconds"), (int, float))]
    return {"landed": kinds.count("entry.landed"), "cameBack": kinds.count("entry.failed") + kinds.count("entry.conflict"),
            "flaky": kinds.count("test.flaky"), "rounds": kinds.count("round.began"),
            "medianGateSeconds": round(statistics.median(gates)) if gates else None}


def data(repository, query):
    from . import load
    journal = repository.events()
    wanted = query.get("kind")
    found = [event for event in journal if not wanted or event["kind"].startswith(wanted)]
    return {"recent": [load("queue").entry(found) for found in reversed(repository.snapshot().get("recent") or [])], "today": totals(journal, time.time()), "events": [shown(event) for event in reversed(found[-MOST:])],
            "total": len(found)}


def summary(repository):
    landed = [event for event in repository.events() if event["kind"] == "entry.landed"]
    if not landed: return None
    last = landed[-1]
    return {"title": "Last landed", "text": last.get("subject") or last.get("entry") or "an entry", "at": last.get("time"),
            "level": "plain"}
