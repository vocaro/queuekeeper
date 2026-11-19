"""The Queue tab: the round's gates and every entry waiting, then the last ones settled; and, as the page's first
view, every repository's at once (everywhere), each repository's own answer combined (combine)."""
import json
from pathlib import Path
import re
import stat
import threading
import time

from queuekeeper import health, lane_wait, merge_queue, remote
from . import each

TITLE, ORDER = "Now", 10  # the Now tab, whose address is still `queue`
ENTRY = ("id", "state", "subject", "note", "branch", "submitted", "settled", "landedAs", "why", "front", "noGate")
GATE = ("lane", "mac", "began", "waiting", "waitingSince", "stuck", "entries")
# An entry that came back is Home's to mention for this long.
NEWS = 3600
# How far back the Queue tab's Needs attention looks for an entry that came back and a flaky test.
OPEN = 86400
# The busiest agents (agent) the flow map gives a node of their own; the others share one.
AGENTS = 4


def entry(found):
    shown = {key: found[key] for key in ENTRY if found.get(key) is not None}
    if found.get("log"): shown["log"] = Path(str(found["log"])).name
    if found.get("commits"): shown["commits"] = len(found["commits"])
    if found.get("flakes"): shown["flakes"] = len(found["flakes"])
    if found.get("ungated"): shown["ungated"] = True
    shown["by"] = agent(found)
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


# Each coding agent's name as the flow map shows it, by its key in resources.CODING_AGENTS, and where its worktrees live.
AGENT_NAMES = {"claude": "Claude", "codex": "Codex"}
AGENT_WORKTREES = {"claude": "/.claude/worktrees/", "codex": "/.codex/worktrees/"}
PERSON = "Person"


def agent(entry):
    """Who submitted entry, as the flow map names them: the coding agent its submission recorded (resources.coding_agent);
    for one submitted before entries recorded it, the agent whose worktree it was submitted from; then its branch's
    prefix, claude/fix-it being Claude's and ann/fix-it ann's; and a Person for a branch with none. An entry as the page
    is given it (entry) carries this name as "by"."""
    entry = entry or {}
    if entry.get("by"): return entry["by"]  # named already, as entry shows it to the page
    if entry.get("agent") in AGENT_NAMES: return AGENT_NAMES[entry["agent"]]
    directory = str(entry.get("directory") or "") + "/"
    found = next((key for key, where in AGENT_WORKTREES.items() if where in directory), None)
    if found: return AGENT_NAMES[found]
    branch = str(entry.get("branch") or "")
    prefix = branch.split("/", 1)[0] if "/" in branch.strip("/") else None
    return AGENT_NAMES.get(prefix, prefix) if prefix else PERSON


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
    """What the Queue tab's flow map draws: who has entries in the queue (agent), busiest first; the Macs,
    each with the roles of the round's gates that run on it; and the last entry to come back within NEWS, if one did."""
    counts, current = {}, set((state.get("round") or {}).get("entries") or [])
    for found in state.get("waiting") or []:
        testing = found.get("id") in current or found.get("state") in ("testing", "passed")
        one = counts.setdefault(agent(found), {"waiting": 0, "testing": 0})
        one["testing" if testing else "waiting"] += 1
    ranked = sorted(counts.items(), key=lambda item: (-sum(item[1].values()), item[0].casefold()))
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
            "back": {"agent": agent(back), "id": back.get("id"), "subject": back.get("subject"),
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
            "ungated": state.get("ungated") or [], "flow": flow(state, gates, time.time()),
            "attention": attention(state, repository.events(), time.time())}


def reason(why):
    """The first clause of why an entry came back, as a sentence: the queue's reasons go on with what to do, the
    load while the lane ran and each failed test, which the page shows only when asked."""
    first = re.split(r"(?<=[a-z0-9)])(?::\s|\.\s)", why.strip(), maxsplit=1)[0].rstrip(".")
    return first[:1].upper() + first[1:] + "." if first else why


def attention(state, journal, now):
    """What is open and needs someone, newest first within each level, danger first: a hold, a closure, entries waiting
    with no worker, a gate that looks stuck, each entry that came back within OPEN and whose branch has not been queued again
    since, and each test found flaky within OPEN with its issue. Each is {level, title, text, at} with the entry, the
    log or the issue it is about when it has one."""
    found = []
    if state.get("held"): found.append({"level": "danger", "title": "Held", "text": f"The queue is held {state['held']}"})
    closure = state.get("closed")
    if isinstance(closure, dict):
        # It reopens by itself at until (vocaro/queuekeeper#286); until then every `qk land` is refused.
        until = closure.get("until")
        found.append({"level": "warning", "title": "Closed", "at": closure.get("at"),
                      "text": f"Closed to new entries by {closure.get('by') or 'someone'}: {closure.get('why') or 'no reason given'}"
                      + (f". It reopens by itself at {time.strftime('%H:%M', time.localtime(until))}, or sooner with "
                         "`qk queue reopen`" if isinstance(until, (int, float)) else "")})
    if state.get("waiting") and not state.get("worker"):
        found.append({"level": "warning", "title": "No worker", "text": f"{len(state['waiting'])} waiting with no worker; "
                      "`qk queue resume` starts one"})
    for role, gate_ in ((state.get("round") or {}).get("gates") or {}).items():
        if gate_.get("stuck"):
            found.append({"level": "warning", "title": "Stuck", "text": f"The {role} {gate_.get('lane') or 'lane'}: {gate_['stuck']}",
                          **({"log": Path(str(gate_["log"])).name} if gate_.get("log") else {})})
    again = {}
    for one in [*(state.get("waiting") or []), *(state.get("recent") or [])]:
        if one.get("branch"): again[one["branch"]] = max(again.get(one["branch"], 0), one.get("submitted") or 0)
    for one in reversed(state.get("recent") or []):
        settled = one.get("settled") or 0
        if one.get("state") not in ("failed", "conflict") or now - settled > OPEN: continue
        if one.get("branch") and again.get(one["branch"], 0) > settled: continue
        found.append({"level": "warning", "title": "Conflict" if one["state"] == "conflict" else "Came back", "at": settled,
                      "text": one.get("subject") or one.get("id") or "an entry",
                      **({"reason": reason(one["why"]), "detail": one["why"]} if one.get("why") else {}),
                      "entry": one.get("id"), **({"log": Path(str(one["log"])).name} if one.get("log") else {})})
    flaky = {}
    for event in journal:
        if event["kind"] == "test.flaky" and now - event.get("time", 0) <= OPEN and isinstance(event.get("test"), str):
            flaky[event["test"]] = event
    for test, event in sorted(flaky.items(), key=lambda item: -item[1].get("time", 0)):
        found.append({"level": "notice", "title": "Flaky", "at": event.get("time"), "text": test,
                      **({"issue": event["issue"]} if isinstance(event.get("issue"), str) else {})})
    order = {"danger": 0, "warning": 1, "notice": 2}
    return sorted(found, key=lambda one: (order[one["level"]], -(one.get("at") or now)))


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


def split(found):
    """(the entries of a repository's Queue answer found that are being tested, those waiting for a round)."""
    current = found.get("round") or {}
    inside = set(current.get("entries") or []) | {one for gate_ in current.get("gates") or [] for one in gate_.get("entries") or []}
    active = [one for one in found.get("waiting") or [] if one.get("id") in inside or one.get("state") in ("testing", "passed")]
    return active, [one for one in found.get("waiting") or [] if one not in active]


def plural(count, noun, nouns=None):
    return f"{count} {noun if count == 1 else nouns or noun + 's'}"


SHARED = ("macs", "devices")  # the signs that are every repository's, since the Macs and devices are


def verdict(parts):
    """The health of every repository's queue at once: a sentence naming each repository that needs a look for a
    reason of its own, held, stopped or its own worst sign, then what the Macs and devices say and what could not
    be checked, said once since they are every repository's; the worst level of all that; each sign the worst of
    that sign's; and the latest forecast."""
    rank = lambda level: health.RANK.get(level, 0)
    read = [(name, found["health"]) for name, found, _ in parts if found and found.get("health")]
    unread = [(name, why) for name, found, why in parts if found is None]
    if not read and not unread: return None
    own = lambda one: max((sign_ for sign_ in one.get("signs") or [] if sign_["name"] not in SHARED),
                          key=lambda sign_: rank(sign_["level"]), default=None)
    def trouble_of(one):
        """(level, label, reason) of a repository's own trouble, or None."""
        if one["verdict"] in ("held", "stopped"): return one["level"], one["label"], one["sentence"].split(". ")[0].rstrip(".")
        worst = own(one)
        if worst and rank(worst["level"]) > rank(health.UNKNOWN):
            return worst["level"], "Stuck" if worst["level"] == health.DANGER else "Slower than usual", worst["short"]
        if one.get("findings"): return health.WARNING, "Needs a look", f"doctor has {plural(len(one['findings']), 'finding')}"
        return None
    trouble = sorted([(name, found) for name, one in read if (found := trouble_of(one))], key=lambda item: -rank(item[1][0]))
    signs_ = [sign_ for _, one in read for sign_ in one.get("signs") or []]
    shared = {}
    for sign_ in signs_:
        if sign_["name"] in SHARED and rank(sign_["level"]) > rank(shared.get(sign_["name"], {}).get("level", health.IDLE)):
            shared[sign_["name"]] = sign_
    shared_bad = [sign_ for sign_ in shared.values() if rank(sign_["level"]) > rank(health.UNKNOWN)]
    unknown = list(dict.fromkeys(sign_["text"] for sign_ in signs_ if sign_["level"] == health.UNKNOWN))
    said, fine = [], len(read) - len(trouble)
    if trouble:
        said.append(f"{len(trouble)} of {len(parts)} {'needs' if len(trouble) == 1 else 'need'} a look: "
                    + "; ".join(f"{name}, {why[:1].lower() + why[1:]}" for name, (_, _, why) in trouble) + ".")
    for name, why in unread: said.append(f"{name} cannot be read: {why}.")
    said += [health.upper(sign_["text"]) + "." for sign_ in shared_bad]
    if unknown: said.append("What could not be checked: " + "; ".join(unknown) + ".")
    if trouble or unread:
        if fine: said.append(f"The other {fine} are fine." if fine > 1 else "The other one is fine.")
    elif any(one["verdict"] != "quiet" for _, one in read):
        said.insert(0, f"Every queue is moving normally{'' if len(read) == 1 else f', all {len(read)} of them'}.")
    else:
        said.insert(0, "Nothing is waiting in " + ("the one repository." if len(read) == 1 else f"any of the {len(read)} repositories."))
    levels = [found[0] for _, found in trouble] + [sign_["level"] for sign_ in shared_bad] \
        + ([health.UNKNOWN] if unknown or unread else [])
    level = max(levels, key=rank, default=health.OK if any(one["verdict"] != "quiet" for _, one in read) else health.IDLE)
    if trouble: label = trouble[0][1][1]
    elif shared_bad: label = "Stuck" if level == health.DANGER else "Needs a look"
    elif unknown or unread: label = "Can't tell"
    else: label = "All good" if level == health.OK else "All quiet"
    sentence = " ".join(said)
    signs = []
    for title in health.SIGNS:
        found = [(name, sign_) for name, one in read for sign_ in one.get("signs") or [] if sign_["name"] == title]
        if not found: continue
        found.sort(key=lambda item: -rank(item[1]["level"]))
        name, worst = found[0]
        shorts = {sign_["short"] for _, sign_ in found}
        bad = [item for item in found if item[1]["level"] not in (health.OK, health.IDLE)]
        if len(shorts) == 1 or len(found) == 1: short, text = worst["short"], worst["text"]
        elif not bad: short = text = f"Fine in all {len(found)}"
        else:
            more = f" (and {len(bad) - 1} more)" if len(bad) > 1 else ""
            short, text = f"{name}: {worst['short']}{more}", "; ".join(f"{one}: {sign_['text']}" for one, sign_ in bad)
        signs.append({**worst, "short": short, "text": text})
    casts = [one["forecast"] for _, one in read if one.get("forecast")]
    forecast = {"waiting": sum(cast["waiting"] for cast in casts), "clearsAt": max(cast["clearsAt"] for cast in casts)} if casts else None
    return {"level": level, "label": label, "sentence": sentence, "signs": signs, "forecast": forecast}


def combine(parts, now=None):
    """Every repository's Queue at once, from parts, [(name, its Queue answer (data), None)] or (name, None, why
    it cannot be read): its health, the day's numbers summed, what needs the owner in any of them, a row for each
    repository, the rounds being tested, the entries in progress and waiting oldest first, and the flow map's
    queues, agents and Macs. An entry or gate carries its repository's name."""
    now = time.time() if now is None else now
    read = [(name, found) for name, found, _ in parts if found]
    total = lambda key: sum(found["overview"]["counts"].get(key) or 0 for _, found in read)
    def summed(key):
        series = [found["overview"].get("trend", {}).get(key) or [] for _, found in read]
        return [sum(values) for values in zip(*series)] if series and all(series) else []
    rows, lines, rounds, active, pending, queues, counts, back = [], [], [], [], [], [], {}, None
    farm_line = None
    for name, found, why in parts:
        if found is None:
            rows.append({"name": name, "level": "warning", "headline": "Cannot be read", "detail": why, "error": why})
            lines.append({"title": "Cannot read", "text": why, "level": "warning", "panel": "queue", "repository": name})
            continue
        overview, current = found["overview"], found.get("round")
        mine, waiting = split(found)
        tag = lambda entries: [{**one, "repository": name} for one in entries]
        active += tag(mine)
        pending += tag(waiting)
        median = (overview.get("trend") or {}).get("medianGateSeconds")
        if current:
            rounds.append({**current, "repository": name, "medianGateSeconds": median,
                           "gates": [{**one, "repository": name} for one in current.get("gates") or []]})
        rows.append({"name": name, "level": overview["level"], "headline": overview["headline"], "detail": overview["detail"],
                     "health": {key: (found.get("health") or {}).get(key) for key in ("level", "label", "verdict")},
                     "worker": found.get("worker"), "held": found.get("held"), "counts": overview["counts"],
                     "landedTrend": (overview.get("trend") or {}).get("landed") or [], "medianGateSeconds": median,
                     "testing": len(mine), "gates": len((current or {}).get("gates") or [])})
        for line in overview.get("lines") or []:
            if line.get("level") not in ("danger", "warning"): continue
            if line.get("panel") == "farm": farm_line = farm_line or line
            else: lines.append({**line, "repository": name})
        queues.append({"name": name, "waiting": len(waiting), "testing": len(mine), "held": bool(found.get("held")),
                       "worker": bool(found.get("worker"))})
        for one, testing in [*((one, True) for one in mine), *((one, False) for one in waiting)]:
            agent_ = counts.setdefault(agent(one), {"waiting": 0, "testing": 0, "queues": []})
            agent_["testing" if testing else "waiting"] += 1
            if name not in agent_["queues"]: agent_["queues"].append(name)
        for one in found.get("recent") or []:
            if one.get("state") in ("failed", "conflict") and now - (one.get("settled") or 0) < NEWS \
                    and (back is None or one["settled"] > back["settled"]):
                back = {"agent": agent(one), "id": one.get("id"), "subject": one.get("subject"),
                        "state": one.get("state"), "settled": one.get("settled"), "repository": name}
    if farm_line: lines.append(farm_line)
    lines.sort(key=lambda line: line.get("level") != "danger")
    ranked = sorted(counts.items(), key=lambda item: (-(item[1]["waiting"] + item[1]["testing"]), item[0].casefold()))
    agents = [{"name": name, **one} for name, one in ranked[:AGENTS]]
    if ranked[AGENTS:]:
        rest = [one for _, one in ranked[AGENTS:]]
        agents.append({"name": f"{len(rest)} more", "waiting": sum(one["waiting"] for one in rest), "more": True,
                       "testing": sum(one["testing"] for one in rest),
                       "queues": list(dict.fromkeys(queue_ for one in rest for queue_ in one["queues"]))})
    macs_ = []
    for _, found in read:
        for mac in (found.get("flow") or {}).get("macs") or []:
            if not any(kept["name"] == mac["name"] for kept in macs_): macs_.append({**mac, "gates": []})
    for name, found in read:
        for mac in (found.get("flow") or {}).get("macs") or []:
            kept = next(kept for kept in macs_ if kept["name"] == mac["name"])
            kept["gates"] += [{"repository": name, "role": role} for role in mac.get("gates") or []]
    submitted = lambda one: one.get("submitted") or 0
    return {"health": verdict(parts),
            "counts": {"waiting": total("waiting"), "landed": total("landed"), "cameBack": total("cameBack"),
                       "testing": len(active)},
            "trend": {key: summed(key) for key in ("waiting", "landed", "cameBack")},
            "worker": any(found.get("worker") for _, found in read),
            "repositories": rows, "lines": lines, "rounds": rounds,
            "attention": sorted(([{**one, "repository": name} for name, found in read for one in found.get("attention") or []]),
                                key=lambda one: ({"danger": 0, "warning": 1}.get(one["level"], 2), -(one.get("at") or now))),
            "active": sorted(active, key=submitted), "pending": sorted(pending, key=submitted),
            "flow": {"queues": queues, "agents": agents, "macs": macs_, "back": back}}


def everywhere(repositories, query):
    return combine(each(repositories, lambda repository: data(repository, {})))
