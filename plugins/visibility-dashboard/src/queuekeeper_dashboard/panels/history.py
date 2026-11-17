"""The History tab: the day's totals beside yesterday's to the same hour, today's gate runs on each Mac, when work
has landed by day and hour, how long passing gates take, the last events that need attention, and the journal's
last events, newest first; and every repository's at once (everywhere), each repository's own answer combined
(combine)."""
import os
import re
import statistics
import time

from queuekeeper import events, farm
from . import each

TITLE, ORDER = "History", 40
MOST = 200
LEVELS = {"entry.failed": "danger", "entry.conflict": "danger", "queue.held": "warning", "test.flaky": "warning",
          "gate.stuck": "warning",
          "entry.landed": "success", "batch.pushed": "success"}
STAMP = len("2026-11-12 09:30:19 ")


# The kinds that need someone's attention, which the tab lists apart from the rest, and how many of the last.
ATTENTION, NOTED = {kind for kind, level in LEVELS.items() if level in ("danger", "warning")}, 10


def shown(event):
    """An event as the page lists it, with the entry it is of and the name of the log it wrote, when it has them."""
    found = {"time": event.get("time"), "kind": event["kind"], "text": events.words(event)[STAMP:],
             "level": LEVELS.get(event["kind"], "plain")}
    if isinstance(event.get("entry"), str): found["entry"] = event["entry"]
    if isinstance(event.get("log"), str): found["log"] = os.path.basename(event["log"])
    return found


def midnight(now):
    """Local midnight before now, in seconds."""
    return time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))


def totals(journal, now):
    """The totals from local midnight to now, by the journal."""
    start = midnight(now)
    day = [event for event in journal if start <= event.get("time", 0) <= now]
    kinds = [event["kind"] for event in day]
    gates = [event["seconds"] for event in day if event["kind"] == "gate.ended" and event.get("lane") == "gate"
             and event.get("status") == 0 and isinstance(event.get("seconds"), (int, float))]
    return {"landed": kinds.count("entry.landed"), "cameBack": kinds.count("entry.failed") + kinds.count("entry.conflict"),
            "failed": kinds.count("entry.failed"), "conflict": kinds.count("entry.conflict"), "removed": kinds.count("entry.removed"),
            "flaky": kinds.count("test.flaky"), "rounds": kinds.count("round.began"),
            "medianGateSeconds": round(statistics.median(gates)) if gates else None}


DAYS = 7  # the days the landings' heatmap covers, today last
RUNS = 60  # the passing gate runs the durations' chart covers, newest


def mac_name(said, names):
    """A Mac as the journal names it, by its farm name where one is known, or None for this Mac. Earlier toolkits
    wrote "this Mac (HOST)" and "the second Mac (SSH DESTINATION)"; names maps a farm Mac's SSH destination to its
    name."""
    if not isinstance(said, str) or not said: return None
    found = re.fullmatch(r"(this Mac|the second Mac) \((.+)\)", said)
    if found and found[1] == "this Mac": return None
    said = found[2] if found else said
    return names.get(said, said)


def gate_runs(journal, now):
    """Each lane run that ended today with its seconds, oldest first: when it began and ended, its lane, whether it
    passed (0), failed or went unmeasured (3), the Mac that ran it, None for this Mac, and its log's name."""
    start = midnight(now)
    try: names = {mac["ssh"]: mac["name"] for mac in farm.read()}
    except Exception:  # noqa: BLE001 - a farm list that cannot be read leaves each Mac as the journal names it
        names = {}
    return [{"began": event["time"] - event["seconds"], "ended": event["time"], "lane": event.get("lane"),
             "status": event.get("status"), "mac": mac_name(event.get("mac"), names), "round": event.get("round"),
             "log": os.path.basename(event["log"]) if isinstance(event.get("log"), str) else None}
            for event in journal if event["kind"] == "gate.ended" and event.get("time", 0) >= start
            and isinstance(event.get("seconds"), (int, float)) and event["seconds"] > 0]


def landings(journal, now):
    """How many entries landed in each hour of the last DAYS days, oldest day first, each day as its local midnight
    and 24 counts; days before the journal's first event are left out, since it cannot say."""
    today, first = midnight(now), min((event.get("time", now) for event in journal), default=now)
    days = [midnight(today - 86400 * back + 3600) for back in reversed(range(DAYS))]
    days = [day for day in days if day + 86400 > first]
    counts = {day: [0] * 24 for day in days}
    for event in journal:
        at = event.get("time")
        if event["kind"] != "entry.landed" or not isinstance(at, (int, float)): continue
        day = midnight(at)
        if day in counts: counts[day][time.localtime(at).tm_hour] += 1
    return [{"day": day, "hours": counts[day]} for day in days]


def gate_seconds(journal):
    """The seconds of the last RUNS passing runs of the gate lane, oldest first."""
    return [round(event["seconds"]) for event in journal if event["kind"] == "gate.ended" and event.get("lane") == "gate"
            and event.get("status") == 0 and isinstance(event.get("seconds"), (int, float))][-RUNS:]


def data(repository, query):
    from . import load
    journal, now = repository.events(), time.time()
    wanted = query.get("kind")
    found = [event for event in journal if not wanted or event["kind"].startswith(wanted)]
    return {"recent": [load("queue").entry(found) for found in reversed(repository.snapshot().get("recent") or [])], "today": totals(journal, now),
            # Yesterday from its midnight to this time of day, for the change beside each of today's totals.
            "yesterday": totals(journal, now - 86400), "gates": gate_runs(journal, now), "landings": landings(journal, now),
            "gateSeconds": gate_seconds(journal), "events": [shown(event) for event in reversed(found[-MOST:])],
            # The last events that need attention, from the whole journal, newest first.
            "attention": [shown(event) for event in reversed([event for event in journal if event["kind"] in ATTENTION][-NOTED:])],
            "total": len(found)}


RECENT = 30  # the settled entries every repository's history lists, newest first
SUMMED = ("landed", "cameBack", "failed", "conflict", "removed", "flaky", "rounds")


def combine(parts):
    """Every repository's History at once, from parts, [(name, its History answer (data), None)] or (name, None,
    why it cannot be read): the day's totals summed beside yesterday's, a row for each repository with its own day
    and gate times, every lane run on the Macs they share, landings summed by day and hour, and the settled entries,
    events and events that need attention of all of them, newest first. A gate time is a repository's alone, so no median is taken across
    them. A run, entry or event carries its repository's name."""
    read = [(name, found) for name, found, _ in parts if found]
    def day(key):
        return {**{name: sum(found[key].get(name) or 0 for _, found in read) for name in SUMMED}, "medianGateSeconds": None}
    landings = {}
    for _, found in read:
        for one in found["landings"]:
            hours = landings.setdefault(one["day"], [0] * 24)
            for hour, count in enumerate(one["hours"]): hours[hour] += count
    rows = [{"name": name, "error": why} if found is None else
            {"name": name, "today": found["today"], "yesterday": found["yesterday"], "gateSeconds": found["gateSeconds"]}
            for name, found, why in parts]
    tagged = lambda key: [{**one, "repository": name} for name, found in read for one in found[key]]
    return {"today": day("today"), "yesterday": day("yesterday"), "repositories": rows,
            "gates": sorted(tagged("gates"), key=lambda run: run["began"]),
            "landings": [{"day": at, "hours": landings[at]} for at in sorted(landings)],
            "recent": sorted(tagged("recent"), key=lambda one: -(one.get("settled") or 0))[:RECENT],
            "events": sorted(tagged("events"), key=lambda event: -(event.get("time") or 0))[:MOST],
            "attention": sorted(tagged("attention"), key=lambda event: -(event.get("time") or 0))[:NOTED],
            "total": sum(found["total"] for _, found in read)}


def everywhere(repositories, query):
    return combine(each(repositories, lambda repository: data(repository, query)))


def summary(repository):
    landed = [event for event in repository.events() if event["kind"] == "entry.landed"]
    if not landed: return None
    last = landed[-1]
    return {"title": "Last landed", "text": last.get("subject") or last.get("entry") or "an entry", "at": last.get("time"),
            "level": "plain"}
