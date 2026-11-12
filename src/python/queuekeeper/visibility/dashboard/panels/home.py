"""The Home tab: what the queue is doing in one line, three numbers for the day, and a line from each other panel
that has one to give."""
import time

from . import available, load

TITLE, ORDER = "Home", 0
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
    if waiting: return "warning", f"{waiting} waiting with no worker", "`queuekeeper queue resume` starts one."
    return "idle", "Idle", "Nothing is waiting."


def today(journal, now):
    """How many entries landed and came back since local midnight, by the journal."""
    start = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
    kinds = [event["kind"] for event in journal if event.get("time", 0) >= start]
    return {"landed": kinds.count("entry.landed"),
            "cameBack": kinds.count("entry.failed") + kinds.count("entry.conflict")}


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
    return {"level": level, "headline": said, "detail": detail, "began": began,
            "counts": {"waiting": len(state.get("waiting") or []), **today(repository.events(), now)}, "lines": lines}
