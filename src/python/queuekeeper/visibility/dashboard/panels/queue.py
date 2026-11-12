"""The Queue tab: the round's gates and every entry waiting, then the last ones settled."""
from pathlib import Path
import time

TITLE, ORDER = "Queue", 10
ENTRY = ("id", "state", "subject", "note", "branch", "submitted", "settled", "landedAs", "why", "front", "noGate")
GATE = ("lane", "mac", "began", "waiting", "waitingSince", "stuck", "entries")
# An entry that came back is Home's to mention for this long.
NEWS = 3600


def entry(found):
    shown = {key: found[key] for key in ENTRY if found.get(key) is not None}
    if found.get("log"): shown["log"] = Path(str(found["log"])).name
    if found.get("commits"): shown["commits"] = len(found["commits"])
    if found.get("flakes"): shown["flakes"] = len(found["flakes"])
    if found.get("ungated"): shown["ungated"] = True
    return shown


def gate(role, found):
    shown = {"role": role, **{key: found[key] for key in GATE if found.get(key) is not None}}
    if found.get("log"): shown["log"] = Path(str(found["log"])).name
    phases = [record["phase"] for record in found.get("progress") or [] if isinstance(record, dict) and record.get("phase")]
    if phases: shown["phase"] = phases[-1]
    return shown


def data(repository, query):
    state = repository.snapshot()
    current = state.get("round") or {}
    order = {"batch": 0, "older half": 1, "newer half": 2, "stacked": 3}
    gates = sorted((current.get("gates") or {}).items(), key=lambda item: (order.get(item[0], 9), item[0]))
    return {"worker": bool(state.get("worker")), "held": state.get("held"),
            "round": {"name": current.get("round"), "base": current.get("base"), "entries": current.get("entries") or [],
                      "bisect": current.get("bisect"), "gates": [gate(role, found) for role, found in gates]}
            if current.get("round") else None,
            "waiting": [entry(found) for found in state.get("waiting") or []],
            "recent": [entry(found) for found in reversed(state.get("recent") or [])],
            "ungated": state.get("ungated") or []}


def summary(repository):
    """What needs the owner, when something does: a hold, entries waiting with no worker, a gate that looks stuck,
    or an entry that came back within the hour."""
    state, now = repository.snapshot(), time.time()
    if state.get("held"): return {"title": "Needs you", "text": f"The queue is held {state['held']}", "level": "danger"}
    if state.get("waiting") and not state.get("worker"):
        return {"title": "Needs you", "text": f"{len(state['waiting'])} waiting with no worker", "level": "warning"}
    stuck = [found["stuck"] for found in ((state.get("round") or {}).get("gates") or {}).values() if found.get("stuck")]
    if stuck: return {"title": "Needs you", "text": stuck[0], "level": "warning"}
    back = [found for found in state.get("recent") or [] if found.get("state") in ("failed", "conflict")
            and now - found.get("settled", 0) < NEWS]
    if back:
        last = max(found["settled"] for found in back)
        return {"title": "Came back", "level": "warning", "at": last,
                "text": f"{len(back)} {'entry' if len(back) == 1 else 'entries'} came back within the hour"}
    return None
