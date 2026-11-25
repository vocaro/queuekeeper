"""When each entry in the merge queue may land: a range from the queue's own passing rounds of the same workload on
the same Mac, for `qk queue status` and its JSON (vocaro/queuekeeper#49).

A round that passes as an ordinary batch, one no bisect, stack, split, rerun or reused lane changed, writes how long
it took from its start to its pass into its `batch.passed` event in the queue's journal (events.py), with its
workload and the Mac that ran its last lane, its destination. The workload (workload) names the lanes the round ran,
the members of a lane group its paths selected, and a digest of those lanes' definitions in the configuration it ran
by, so that a round is only ever compared with rounds that ran the same lanes defined the same way. The journal is the
repository's own, so the history is the repository's too.

A round's history on a Mac is the median and nearest-rank p90 of its newest RECENT such rounds after the latest
`qk timing stale` mark of any of its lanes on this Mac, given FEWEST of them (spread). A round that has not reached
its last lane's Mac yet is given the widest range of the Macs that have such a history, from the lowest median to the
highest p90, rather than one Mac's guessed.

Each waiting entry's estimate (estimates) is the running round's remaining range, the round's median and p90 less
what it has run, followed by each round ahead of the entry, its own included, by the batching rules Worker.choose and
Worker.docs_ahead use: a passed batch is pushed first, an entry `qk land --front` put first gates alone, and entries
go in one batch up to the first that `--alone` or `--no-gate` puts on its own; docs-only entries go ahead of code in a
round of their own on the docs lanes. Ranges add: the lows give the earliest landing, the highs the latest. Each round
ahead is assumed to pass. An estimate that would need what the history cannot give, such as a round past its p90, a
bisect, a failed gate being weighed, a stacked gate or too few comparable rounds, is unknown with its reason, never a
guess. Host load and the wait for admission are not modeled apart: a past round's time includes the waits it had.
Nothing here may change or stop the queue: an estimate that cannot be made says why."""
import hashlib
import json
import statistics
import time

from . import lane_timing

# The newest passing rounds a range is taken over, and the fewest that make one.
RECENT, FEWEST = lane_timing.RECENT, lane_timing.FEWEST
KIND = "batch.passed"
HERE = "this Mac"


def workload(config, lanes, members=None):
    """The workload of a round that runs lanes, with members, {lane group: its lanes run}, by config's definitions:
    {"id", "lanes"} and "members" when some are named. id is a digest of all three."""
    lanes = [str(lane) for lane in lanes]
    members = {str(group): sorted(map(str, chosen)) for group, chosen in (members or {}).items() if chosen}
    named = sorted({*lanes, *(member for chosen in members.values() for member in chosen)})
    definitions = {name: (config.get("lanes") or {}).get(name) for name in named}
    text = json.dumps({"lanes": lanes, "members": members, "definitions": definitions}, sort_keys=True, default=str)
    return {"id": hashlib.sha256(text.encode()).hexdigest()[:16], "lanes": lanes, **({"members": members} if members else {})}


def destination(mac):
    """The Mac a record names, as the queue's records name it: HERE for this Mac in any of its spellings."""
    if not isinstance(mac, str) or not mac.strip(): return None
    mac = mac.strip().removeprefix("on ").strip()
    return HERE if mac.startswith(HERE) else mac


def samples(events):
    """The timed passing rounds the journal's events hold, oldest first: [{"time", "workload", "mac", "seconds"}]."""
    found = []
    for event in events:
        if event.get("kind") != KIND: continue
        seconds, load, mac = event.get("roundSeconds"), event.get("workload"), destination(event.get("mac"))
        if lane_timing.number(seconds) and seconds > 0 and isinstance(load, dict) and isinstance(load.get("id"), str) \
                and mac and lane_timing.number(event.get("time")):
            found.append({"time": event["time"], "workload": load["id"], "lanes": load.get("lanes") or [],
                          "mac": mac, "seconds": seconds})
    return found


def spread(values):
    """{"median", "p90", "samples"} of the newest RECENT values, or None with fewer than FEWEST."""
    values = values[-RECENT:]
    if len(values) < FEWEST: return None
    return {"median": statistics.median(values), "p90": lane_timing.percentile(values), "samples": len(values)}


def history(found, load, marks=None):
    """{Mac: spread} of the rounds of workload load (an id) in found (samples), each Mac with enough of them, leaving out
    rounds before the newest stale mark, by marks ({LANE or "*": epoch time}), of any of their lanes."""
    by_mac = {}
    for sample in found:
        if sample["workload"] != load: continue
        since = [lane_timing.stale_since(marks or {}, lane) for lane in sample["lanes"]] or [None]
        since = max((when for when in since if when is not None), default=None)
        if since is not None and sample["time"] <= since: continue
        by_mac.setdefault(sample["mac"], []).append(sample["seconds"])
    return {mac: found for mac, values in by_mac.items() if (found := spread(values))}


def round_range(found, load, mac=None):
    """(low, high, basis) for one round of workload load (an id) on mac, or on whichever Mac takes it when mac is None,
    by found (history); or (None, None, why not)."""
    if mac is not None:
        known = found.get(mac)
        if known is None:
            return None, None, f"fewer than {FEWEST} comparable passing rounds on {mac}"
        return known["median"], known["p90"], f"{known['samples']} passing rounds on {mac}"
    if not found: return None, None, f"fewer than {FEWEST} comparable passing rounds on any Mac"
    macs = sorted(found)
    return (min(found[mac]["median"] for mac in macs), max(found[mac]["p90"] for mac in macs),
            "whichever Mac takes it: " + " and ".join(f"{found[mac]['samples']} passing rounds on {mac}" for mac in macs))


def running_mac(state):
    """The Mac running the round's last lane, by its gate's record, or None while the round has not reached it."""
    lanes = (state.get("workload") or {}).get("lanes") or []
    gate = (state.get("gates") or {}).get("batch") or {}
    if lanes and gate.get("lane") == lanes[-1] and not gate.get("waiting"): return destination(gate.get("mac"))
    return None


def unknown(why):
    return {"unknown": why}


def plan(waiting, kind_of):
    """The rounds the worker makes of waiting, its entries outside the running round in order, by Worker.choose and
    Worker.docs_ahead: [(kind, [entry, ...])], kind "push" for a passed batch, else kind_of(batch, alone)'s, one of
    "code", "docs" and "ungated", or None for a batch it cannot tell."""
    rounds, left = [], list(waiting)
    while left:
        passed = [entry for entry in left if entry["state"] == "passed"]
        if passed:
            rounds.append(("push", passed))
            left = [entry for entry in left if entry not in passed]
            continue
        first = next((entry for entry in left if entry.get("front") and entry.get("noGate")), None) \
            or next((entry for entry in left if entry.get("front")), None)
        if first is None and (left[0].get("noGate") or left[0].get("alone")): first = left[0]
        if first is not None: batch, alone = [first], True
        else:
            end = next((index for index, entry in enumerate(left) if entry.get("noGate") or entry.get("alone")), len(left))
            batch, alone = left[:end], False
        kind, batch = kind_of(batch, alone, left)
        rounds.append((kind, batch))
        left = [entry for entry in left if entry not in batch]
    return rounds


def span_words(low, high):
    low, high = map(lane_timing.words, (low, high))
    return high if low == high else f"{low} to {high}"


def words(estimate, at=None):
    """What `qk queue status` says of an entry's estimate."""
    if "unknown" in estimate: return f"landing estimate unknown: {estimate['unknown']}"
    at = time.time() if at is None else at
    clock = lambda seconds: time.strftime("%H:%M", time.localtime(at + seconds))  # noqa: E731
    rounds = estimate["rounds"]
    return (f"lands in roughly {span_words(estimate['low'], estimate['high'])} (about {clock(estimate['low'])} to "
            f"{clock(estimate['high'])}), after {rounds} round{'' if rounds == 1 else 's'}: "
            + "; ".join(estimate["basis"]))


def estimates(state, waiting, found, workload_of, kind_of=None, at=None, marks=None):
    """{entry ID: estimate} for each of waiting, the queue's waiting entries in order: {"low", "high", "rounds",
    "basis"} in seconds from at, or {"unknown": why}. state is the round's account, or None with no worker; found the
    journal's timed rounds (samples); workload_of(kind, batch) the workload id a round ahead of that kind would run,
    or None; kind_of(batch, alone, left) the kind and batch a round ahead makes of batch (plan)."""
    at = time.time() if at is None else at
    kind_of = kind_of or (lambda batch, alone, left: ("code", batch))
    if state is None: return {entry["id"]: unknown("no worker runs") for entry in waiting}
    if state.get("bisect"):
        return {entry["id"]: unknown("a bisect is under way, whose rounds depend on which entries fail")
                for entry in waiting}
    if isinstance(state.get("failure"), dict):
        return {entry["id"]: unknown("the round's gate failed, and the worker weighs the failure") for entry in waiting}
    stacked = set(((state.get("gates") or {}).get("stacked") or {}).get("entries") or [])
    current = set(state.get("entries") or []) if state.get("round") else set()
    found_for = lambda load: history(found, load, marks)  # noqa: E731
    low = high = 0
    basis, rounds, why = [], 0, None
    if current:
        load = (state.get("workload") or {}).get("id")
        if not load: why = "the running round recorded no workload to compare"
        else:
            mac = running_mac(state)
            median, p90, said = round_range(found_for(load), load, mac)
            elapsed = max(0.0, at - state["started"]) if lane_timing.number(state.get("started")) else None
            if median is None: why = f"the running round has {said}"
            elif elapsed is None: why = "the running round recorded no start"
            elif elapsed >= p90:
                why = f"the running round has run {lane_timing.words(elapsed)}, past the p90 of its {said}"
            else:
                low, high, rounds = max(0.0, median - elapsed), p90 - elapsed, 1
                basis.append(f"the running round has run {lane_timing.words(elapsed)}, against {said}")
    found_estimates = {}
    for entry in waiting:
        if entry["id"] in current:
            found_estimates[entry["id"]] = unknown(why) if why else \
                {"low": round(low), "high": round(high), "rounds": rounds, "basis": list(basis)}
    others = [entry for entry in waiting if entry["id"] not in current and entry["id"] not in stacked
              and entry["state"] != "testing"]
    for entry in waiting:
        if entry["id"] in stacked:
            found_estimates[entry["id"]] = unknown("it gates stacked on the running round, which no history times")
        elif entry["id"] not in current and entry["state"] == "testing":
            found_estimates[entry["id"]] = unknown("it gates beside the running round, which no history times")
    if why:
        for entry in others: found_estimates[entry["id"]] = unknown(why)
        return found_estimates
    for kind, batch in plan(others, kind_of):
        if why is None:
            if kind == "push":
                rounds += 1
                basis.append(f"a push of {len(batch)} passed entr{'y' if len(batch) == 1 else 'ies'}")
            elif kind is None: why = "a round ahead could not be planned"
            else:
                load = workload_of(kind, batch)
                if load is None: why = f"the workload of a {kind} round ahead could not be read"
                else:
                    median, p90, said = round_range(found_for(load), load)
                    if median is None: why = f"a {kind} round ahead has {said}"
                    else:
                        low, high, rounds = low + median, high + p90, rounds + 1
                        basis.append(f"a {kind} round of {len(batch)} entr{'y' if len(batch) == 1 else 'ies'}, "
                                     f"by {said}")
        for entry in batch:
            found_estimates[entry["id"]] = unknown(why) if why else \
                {"low": round(low), "high": round(high), "rounds": rounds, "basis": list(basis)}
    return found_estimates
