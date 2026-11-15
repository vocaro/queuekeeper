"""The Home tab: what the queue is doing in one line, three numbers for the day with the trend behind each, and a
line from each other panel that has one to give."""
import statistics
import time

from . import available, load

TITLE, ORDER = "Home", 0
HIDDEN = True
NAME = __name__.rsplit(".", 1)[1]


def headline(state, now):
    """(level, what the queue is doing, a sentence of detail)."""
    waiting = len(state.get("waiting") or [])
    held = state.get("held")
    if held:
        return "danger", "Held for you" if held.startswith("for the owner") else "Held for an agent", held
    current = state.get("round") or {}
    if state.get("worker") and current.get("round"):
        testing = len(current.get("entries") or [])
        gates = list((current.get("gates") or {}).values())
        detail = "Its round is starting."
        if gates:
            first = gates[0]
            detail = f"The {first.get('lane')} lane is running on {first.get('mac') or 'a Mac not yet chosen'}."
        if waiting > testing: detail += f" {waiting - testing} more waiting."
        return "active", f"Testing {testing} {'entry' if testing == 1 else 'entries'}", detail
    if state.get("worker"): return "active", "Worker running", f"{waiting} waiting."
    if waiting: return "warning", f"{waiting} waiting with no worker", "`qk queue resume` starts one."
    return "idle", "Idle", "Nothing is waiting."


def today(journal, now):
    """How many entries landed and came back since local midnight, by the journal."""
    start = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
    kinds = [event["kind"] for event in journal if event.get("time", 0) >= start]
    return {"landed": kinds.count("entry.landed"),
            "cameBack": kinds.count("entry.failed") + kinds.count("entry.conflict")}


HOURS = 24
GATES = 20  # the passing gate runs the gate's trend and median are taken over, newest first
JOINED, SETTLED = ("entry.queued",), ("entry.landed", "entry.failed", "entry.conflict", "entry.removed")


def trend(journal, waiting, now):
    """Each of the last HOURS hours, oldest first, by the journal: how many entries waited at its end, counted back
    from the `waiting` now, and how many landed and came back in it; and the seconds of the last GATES passing runs
    of the gate lane, oldest first, with their median, or None when there are none."""
    start = now - HOURS * 3600
    hour = lambda event: min(HOURS - 1, int((event["time"] - start) // 3600))
    landed, came_back, change = [0] * HOURS, [0] * HOURS, [0] * HOURS
    for event in journal:
        at = event.get("time")
        if not isinstance(at, (int, float)) or at < start: continue
        kind = event["kind"]
        if kind == "entry.landed": landed[hour(event)] += 1
        if kind in ("entry.failed", "entry.conflict"): came_back[hour(event)] += 1
        change[hour(event)] += 1 if kind in JOINED else -1 if kind in SETTLED else 0
    depth, after = [0] * HOURS, 0
    for index in reversed(range(HOURS)):
        depth[index] = max(0, waiting - after)
        after += change[index]
    gates = [event["seconds"] for event in journal if event["kind"] == "gate.ended" and event.get("lane") == "gate"
             and event.get("status") == 0 and isinstance(event.get("seconds"), (int, float))][-GATES:]
    return {"waiting": depth, "landed": landed, "cameBack": came_back, "gateSeconds": [round(seconds) for seconds in gates],
            "medianGateSeconds": round(statistics.median(gates)) if gates else None}


def data(repository, query):
    now, state = time.time(), repository.snapshot()
    level, said, detail = headline(state, now)
    began = min((found["began"] for found in ((state.get("round") or {}).get("gates") or {}).values()
                 if isinstance(found.get("began"), (int, float))), default=None)
    lines = []
    for name in available():
        if name == NAME: continue
        order = 1000
        try:
            module = load(name)
            order = module.ORDER
            line = module.summary(repository) if hasattr(module, "summary") else None
        except Exception as error:  # noqa: BLE001
            line = {"title": name.capitalize(), "text": f"This panel has a fault: {error}", "level": "warning"}
        if line: lines.append({"panel": name, "order": order, **line})
    # What needs the owner first, then the panels in tab order.
    lines.sort(key=lambda line: (line.get("level") not in ("danger", "warning"), line["order"]))
    waiting, journal = len(state.get("waiting") or []), repository.events()
    return {"level": level, "headline": said, "detail": detail, "began": began,
            "counts": {"waiting": waiting, **today(journal, now)}, "trend": trend(journal, waiting, now), "lines": lines}
