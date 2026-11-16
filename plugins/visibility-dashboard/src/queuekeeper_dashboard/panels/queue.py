"""The Queue tab: the round's gates and every entry waiting, then the last ones settled."""
import json
from pathlib import Path
import re
import stat
import threading
import time

from queuekeeper import health, lane_wait, merge_queue, remote

TITLE, ORDER = "Queue", 10
ENTRY = ("id", "state", "subject", "note", "branch", "submitted", "settled", "landedAs", "why", "front", "noGate")
GATE = ("lane", "mac", "began", "waiting", "waitingSince", "stuck", "entries")
# An entry that came back is Home's to mention for this long.
NEWS = 3600
# The busiest branch prefixes the flow map gives a node of their own; the others share one.
AGENTS = 4


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


def lookup(repository, identifier, state):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,159}", identifier): return None
    for found in [*(state.get("waiting") or []), *(state.get("recent") or [])]:
        if found.get("id") == identifier: return found
    path = repository.directory / "entries" / f"{identifier}.json"
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024: return None
        found = json.loads(path.read_text())
        return found if isinstance(found, dict) and found.get("id") == identifier else None
    except (OSError, ValueError): return None


def detail(repository, query, state):
    if query.get("entry"):
        identifier = query["entry"]
        found = lookup(repository, identifier, state)
        if found is None: return {"kind": "missing", "id": identifier}
        return {"kind": "entry", "entry": {**entry(found), "tip": found.get("tip"), "base": found.get("base"),
                                           "commitList": found.get("commits") or []}}
    current = state.get("round") or {}
    role = query.get("gate")
    found = (current.get("gates") or {}).get(role)
    if not found or (query.get("round") and query["round"] != current.get("round")):
        return {"kind": "missing", "id": query.get("round") or role}
    entries = []
    for identifier in found.get("entries") or []:
        record = lookup(repository, identifier, state)
        entries.append(entry(record) if record else {"id": identifier, "subject": identifier, "state": "unknown"})
    return {"kind": "gate", "gate": gate(role, found), "round": current.get("round"), "entries": entries}


def agent(branch):
    """Whose entry this is, by its branch's prefix: claude/fix-it is claude's. A branch with no prefix is "other"."""
    branch = str(branch or "")
    return branch.split("/", 1)[0] if "/" in branch.strip("/") else "other"


def macs():
    """The farm's Macs as the flow map draws them, from the Farm panel's last reading: none without that panel or
    before its first reading."""
    from . import available, load
    if "farm" not in available(): return []
    farm = load("farm")
    reading = farm.current()[0]
    return [{"name": found.get("name"), "role": found.get("role"), **({"ssh": found["ssh"]} if found.get("ssh") else {}),
             "state": found.get("state"),
             **{key: shown["slots"][key] for key in ("used", "most")}}
            for found in (reading or {}).get("hosts", []) if found.get("state") != "notConfigured"
            for shown in (farm.host(found),)]


def flow(state, gates, now):
    """What the Queue tab's flow map draws: who has entries in the queue, by branch prefix, busiest first; the Macs,
    each with the roles of the round's gates that run on it; and the last entry to come back within NEWS, if one did."""
    counts, current = {}, set((state.get("round") or {}).get("entries") or [])
    for found in state.get("waiting") or []:
        testing = found.get("id") in current or found.get("state") in ("testing", "passed")
        one = counts.setdefault(agent(found.get("branch")), {"waiting": 0, "testing": 0})
        one["testing" if testing else "waiting"] += 1
    ranked = sorted(counts.items(), key=lambda item: (-sum(item[1].values()), item[0]))
    agents = [{"name": name, **one} for name, one in ranked[:AGENTS]]
    if ranked[AGENTS:]:
        rest = [one for _, one in ranked[AGENTS:]]
        agents.append({"name": f"{len(rest)} more", "waiting": sum(one["waiting"] for one in rest),
                       "testing": sum(one["testing"] for one in rest), "more": True})
    plain = lambda name: re.sub(r"^the ", "", str(name or "")).split(" (", 1)[0]
    # A gate's Mac is this Mac, or the farm's Mac its record names by its SSH destination (lane_wait.farm_mac).
    on = lambda found, mac: (lane_wait.host_of(found["mac"]) == mac["ssh"] if mac.get("ssh") and lane_wait.host_of(found["mac"])
                             else plain(found["mac"]) == plain(mac["name"]))
    farm = [{**mac, "gates": [role for role, found in gates if found.get("mac") and on(found, mac)]} for mac in macs()]
    back = next((found for found in reversed(state.get("recent") or []) if found.get("state") in ("failed", "conflict")
                 and now - (found.get("settled") or 0) < NEWS), None)
    return {"agents": agents, "macs": farm,
            "back": {"agent": agent(back.get("branch")), "id": back.get("id"), "subject": back.get("subject"),
                     "state": back.get("state"), "settled": back.get("settled")} if back else None}


DOCTORED = 60.0  # seconds doctor's findings serve the verdict before they are read again, in a thread of their own
DOCTOR, DOCTOR_LOCK = {}, threading.Lock()


def findings(repository):
    """doctor's findings for repository's queue (merge_queue.diagnose), as last read; None before the first reading.
    Reading them reads git and the gate logs, so it runs in a thread, at most every DOCTORED seconds."""
    def read():
        try: found = merge_queue.diagnose(repository.root)[0]
        except Exception: found = None  # noqa: BLE001 - the verdict then goes without them
        with DOCTOR_LOCK: DOCTOR[repository.root] = {"findings": found, "at": time.time(), "busy": False}
    with DOCTOR_LOCK:
        kept = DOCTOR.setdefault(repository.root, {"findings": None, "at": 0.0, "busy": False})
        if not kept["busy"] and time.time() - kept["at"] > DOCTORED:
            kept["busy"] = True
            threading.Thread(target=read, daemon=True).start()
        return kept["findings"]


def health_of(repository, state):
    """The queue's health (queuekeeper.health), judged from the Farm panel's last readings of the Macs and devices,
    which it keeps fresh, and doctor's last findings: the same verdict as `qk queue health`, without asking a Mac."""
    from . import available, load
    readings = record = None
    if "farm" in available():
        farm = load("farm")
        at = farm.current()[1]
        with farm.LOCK: detail, record = dict(farm.STATE.get("detail") or {}), farm.STATE.get("devices")
        if detail:
            readings = [{"name": lane_wait.HERE, "reading": detail.get("here"), "readAt": at}]
            from queuekeeper import farm as macs_of, resources
            macs = macs_of.active()
            for mac in macs:
                readings.append({"name": lane_wait.farm_mac(mac["ssh"]),
                                 "reading": detail.get(resources.farm_role(mac, len(macs))), "readAt": at})
    return health.assess(state, repository.events(), readings=readings, record=record, findings=findings(repository))


def data(repository, query):
    from . import load
    state = repository.snapshot()
    current = state.get("round") or {}
    order = {"batch": 0, "older half": 1, "newer half": 2, "stacked": 3}
    gates = sorted((current.get("gates") or {}).items(), key=lambda item: (order.get(item[0], 9), item[0]))
    return {**({"detail": detail(repository, query, state)} if query.get("entry") or query.get("gate") else {}),
            "overview": load("home").data(repository, query), "health": health_of(repository, state), "worker": bool(state.get("worker")), "held": state.get("held"),
            "round": {"name": current.get("round"), "base": current.get("base"), "entries": current.get("entries") or [],
                      "bisect": current.get("bisect"), "gates": [gate(role, found) for role, found in gates]}
            if current.get("round") else None,
            "waiting": [entry(found) for found in state.get("waiting") or []],
            "recent": [entry(found) for found in reversed(state.get("recent") or [])],
            "ungated": state.get("ungated") or [], "flow": flow(state, gates, time.time())}


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
