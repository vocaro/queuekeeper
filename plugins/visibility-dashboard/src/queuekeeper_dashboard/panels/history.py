"""The Trends tab, whose address is still `history`: each of the last PERIOD days, what landed, came back and was
found flaky and how long passing gates took against their usual time, for the page to set the last 7 or 30 days
against the ones before; today's lane runs on each Mac, when work has landed by day and hour, how long passing
gates take, and each lane's timing baseline on this Mac beside its recent passing runs (baselines); and every repository's at once (everywhere), each repository's own answer combined (combine). What is
happening now is the Now tab's (queue), and what happened, round by round, the Activity tab's (activity)."""
import os
from pathlib import Path
import re
import statistics
import time

from queuekeeper import config as configuration, farm, lane_timing
from . import each

TITLE, ORDER = "Trends", 40
PERIOD = 60  # the days daily covers, today last: twice the longest period the page sets against the one before


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


def daily(journal, now):
    """Each of the last PERIOD days, oldest first, as its local midnight: how many entries landed and came back,
    how many tests were found flaky, the median of its passing gate runs' seconds, and each of those runs as a share
    of the median of every passing gate run over the PERIOD days, which a repository's runs are measured by alone,
    so that days, and repositories, whose gates differ in length can be set side by side. Days before the journal's
    first event are left out, since it cannot say what happened on them."""
    today, first = midnight(now), min((event.get("time", now) for event in journal), default=now)
    days = [day for day in (midnight(today - 86400 * back + 3600) for back in reversed(range(PERIOD))) if day + 86400 > first]
    found = {day: {"day": day, "landed": 0, "cameBack": 0, "flaky": 0, "gates": []} for day in days}
    for event in journal:
        at = event.get("time")
        if not isinstance(at, (int, float)): continue
        one = found.get(midnight(at))
        if one is None: continue
        kind = event["kind"]
        if kind == "entry.landed": one["landed"] += 1
        elif kind in ("entry.failed", "entry.conflict"): one["cameBack"] += 1
        elif kind == "test.flaky": one["flaky"] += 1
        elif kind == "gate.ended" and event.get("lane") == "gate" and event.get("status") == 0 \
                and isinstance(event.get("seconds"), (int, float)) and event["seconds"] > 0:
            one["gates"].append(event["seconds"])
    usual = statistics.median([seconds for one in found.values() for seconds in one["gates"]] or [0]) or None
    for one in found.values():
        gates = one.pop("gates")
        one["medianGateSeconds"] = round(statistics.median(gates)) if gates else None
        one["gateShares"] = [round(seconds / usual, 2) for seconds in gates] if usual else []
    return [found[day] for day in days]


def gate_seconds(journal):
    """The seconds of the last RUNS passing runs of the gate lane, oldest first."""
    return [round(event["seconds"]) for event in journal if event["kind"] == "gate.ended" and event.get("lane") == "gate"
            and event.get("status") == 0 and isinstance(event.get("seconds"), (int, float))][-RUNS:]


LANE_RUNS = 40  # the passing runs of each lane the baselines' chart draws, newest, before a stale mark among them


def baselines(root, entries=None):
    """Each lane's timing baseline on this Mac (lane_timing), as `qk timing show` gives it, beside the seconds of
    its newest LANE_RUNS passing runs, oldest first, as [epoch time, seconds], those before its last stale mark among
    them, so that the page can draw the baseline's p90 and the lane's fail threshold against them and mark where the
    history was marked stale. A lane with fewer than lane_timing.FEWEST passing runs since then has no baseline.
    countedFrom is the time of the oldest run the baseline counts, its newest lane_timing.RECENT since the mark, or
    None with none since it, so that the page fades every run the baseline leaves out, for age as well as for the
    mark (vocaro/queuekeeper#280)."""
    try:
        config = configuration.load(root)
        from queuekeeper import cli
        project = cli.project_name(Path(root), config)
    except Exception:  # noqa: BLE001 - a configuration that cannot be read leaves the tab without baselines
        return []
    entries = lane_timing.read() if entries is None else entries
    marks = lane_timing.stale_marks(project, entries)
    rows = []
    for name in lane_timing.lanes_of(config):
        settings = config["lanes"][name].get("timing")
        settings = settings if isinstance(settings, dict) else {}
        found = lane_timing.baseline(project, name, entries)
        try: limit = lane_timing.fail_seconds(settings, found)
        except ValueError: limit = None
        every = [[entry["time"], entry["seconds"]] for entry in entries if entry.get("event") == "run"
                 and entry.get("project") == project and entry.get("lane") == name
                 and lane_timing.number(entry.get("seconds"))]
        since = lane_timing.stale_since(marks, name)
        counted = [run for run in every if since is None or run[0] > since][-lane_timing.RECENT:]
        runs = every[-LANE_RUNS:]
        rows.append({"lane": name, "runs": runs, "baseline": found, "fail": settings.get("fail"),
                     "failSeconds": round(limit, 1) if limit else None,
                     "minSamples": settings.get("minSamples", lane_timing.MIN_SAMPLES) if "fail" in settings else None,
                     "staleSince": since, "countedFrom": counted[0][0] if counted else None})
    return rows


def data(repository, query):
    journal, now = repository.events(), time.time()
    return {"today": totals(journal, now),
            # Yesterday from its midnight to this time of day, for the change beside each of today's totals.
            "yesterday": totals(journal, now - 86400), "gates": gate_runs(journal, now), "landings": landings(journal, now),
            "gateSeconds": gate_seconds(journal), "daily": daily(journal, now), "baselines": baselines(repository.root),
            "fewest": lane_timing.FEWEST}


SUMMED = ("landed", "cameBack", "failed", "conflict", "removed", "flaky", "rounds")


def combine(parts):
    """Every repository's History at once, from parts, [(name, its History answer (data), None)] or (name, None,
    why it cannot be read): the day's totals summed beside yesterday's, a row for each repository with its own day
    and gate times, every lane run on the Macs they share, landings summed by day and hour, and each day's counts
    summed with every repository's gate runs as shares of its own usual time. A gate time is a repository's alone, so
    no median of seconds is taken across them. A run carries its repository's name."""
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
    tagged = lambda key: [{**one, "repository": name} for name, found in read for one in found.get(key) or []]
    days = {}
    for _, found in read:
        for one in found["daily"]:
            kept = days.setdefault(one["day"], {"day": one["day"], "landed": 0, "cameBack": 0, "flaky": 0,
                                                "medianGateSeconds": None, "gateShares": []})
            for key in ("landed", "cameBack", "flaky"): kept[key] += one[key]
            kept["gateShares"] += one["gateShares"]
    return {"today": day("today"), "yesterday": day("yesterday"), "repositories": rows,
            "gates": sorted(tagged("gates"), key=lambda run: run["began"]),
            "landings": [{"day": at, "hours": landings[at]} for at in sorted(landings)],
            "daily": [days[at] for at in sorted(days)], "baselines": tagged("baselines"), "fewest": lane_timing.FEWEST}


def everywhere(repositories, query):
    return combine(each(repositories, lambda repository: data(repository, query)))


def summary(repository):
    landed = [event for event in repository.events() if event["kind"] == "entry.landed"]
    if not landed: return None
    last = landed[-1]
    return {"title": "Last landed", "text": last.get("subject") or last.get("entry") or "an entry", "at": last.get("time"),
            "level": "plain"}
