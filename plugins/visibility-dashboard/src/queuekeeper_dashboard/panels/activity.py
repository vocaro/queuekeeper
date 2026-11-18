"""The Activity tab: what the queue did, newest first, one row for each round it tested and one line for each time
it was held, resumed, closed or reopened; and every repository's at once (everywhere), merged by time.

A round, by the journal, is what round.began names: the entries it tested, each with its subject and branch as
entry.queued gave them, and how each ended (landed, came back, conflicted or was removed) where the entry settled
before it was tested again; each lane it ran, from gate.ended, with how it ended, its log and how long it took, and
each lane begun and not yet ended as running; and the tests found flaky at its commit."""
import os
import time

from . import each

TITLE, ORDER = "Activity", 90
ROUNDS = 60  # the rounds a repository's feed shows, newest first; ?more asks for that many again
NOTICES = ("queue.held", "queue.resumed", "queue.closed", "queue.reopened")
STALE = 6 * 3600  # seconds after which a lane that began and never ended is no longer shown as running
SETTLED = {"entry.landed": "landed", "entry.failed": "back", "entry.conflict": "conflict", "entry.removed": "removed"}


def notice(event):
    kind = event["kind"]
    if kind == "queue.held": return f"The queue was held {event.get('why') or ''}".strip()
    if kind == "queue.resumed": return "The queue resumed" + (f" after it was held {event['was']}" if event.get("was") else "")
    if kind == "queue.closed": return f"The queue was closed to new entries by {event.get('by') or 'someone'}" + (f": {event['why']}" if event.get("why") else "")
    return "The queue reopened to new entries"


def reason(why):
    """The first clause of why, as the Now tab's Needs attention gives it (queue.reason)."""
    from . import load
    return load("queue").reason(why)


def rounds(journal, now, most=ROUNDS):
    """The last most rounds and the notices among them, newest first (see the module's docstring)."""
    queued, settled, began, lanes, running, flaky, notices = {}, {}, [], {}, {}, {}, []
    for order, event in enumerate(journal):
        kind, at = event["kind"], event.get("time")
        if not isinstance(at, (int, float)): continue
        if kind == "entry.queued" and isinstance(event.get("entry"), str):
            queued[event["entry"]] = {"subject": event.get("subject"), "branch": event.get("branch")}
        elif kind in SETTLED and isinstance(event.get("entry"), str):
            settled.setdefault(event["entry"], []).append({"at": at, "state": SETTLED[kind], "why": event.get("why"),
                                                          "landedAs": event.get("landedAs"), "subject": event.get("subject")})
        elif kind == "round.began" and isinstance(event.get("round"), str):
            began.append({**event, "order": order})
        elif kind == "gate.began" and isinstance(event.get("round"), str):
            running.setdefault(event["round"], {})[(event.get("lane"), event.get("role"))] = at
        elif kind == "gate.ended" and isinstance(event.get("round"), str):
            running.get(event["round"], {}).pop((event.get("lane"), event.get("role")), None)
            lanes.setdefault(event["round"], []).append({
                "lane": event.get("lane"), "role": event.get("role"), "status": event.get("status"),
                "seconds": event.get("seconds"), "log": os.path.basename(event["log"]) if isinstance(event.get("log"), str) else None})
        elif kind == "test.flaky" and isinstance(event.get("commit"), str):
            flaky.setdefault(event["commit"], []).append({"test": event.get("test"), "issue": event.get("issue")})
        elif kind in NOTICES:
            notices.append({"kind": "notice", "time": at, "order": order, "text": notice(event), "level": "warning" if kind == "queue.held" else "plain"})
    # Each entry's rounds in order, so that an outcome belongs to the last round that began before it.
    tested = {}
    for event in began:
        for entry in event.get("entries") or []: tested.setdefault(entry, []).append(event["time"])
    shown = []
    for event in began[-most:]:
        name, at = event["round"], event["time"]
        entries = []
        for entry in event.get("entries") or []:
            later = [start for start in tested.get(entry, []) if start > at]
            until = min(later) if later else now + 1
            outcome = next((one for one in settled.get(entry, []) if at <= one["at"] < until), None)
            known = queued.get(entry, {})
            entries.append({"id": entry, "subject": known.get("subject") or (outcome or {}).get("subject"), "branch": known.get("branch"),
                            "state": outcome["state"] if outcome else "retested" if later else "testing",
                            **({"why": outcome["why"], "reason": reason(outcome["why"])} if outcome and outcome.get("why") else {}),
                            **({"landedAs": outcome["landedAs"]} if outcome and outcome.get("landedAs") else {})})
        ran = lanes.get(name, [])
        # A lane begun and not ended is running, unless it began so long ago that its end went unrecorded.
        still = [{"lane": lane, "role": role, "status": None, "seconds": now - start, "log": f"{name}-{lane}.log"}
                 for (lane, role), start in running.get(name, {}).items() if now - start < STALE]
        shown.append({"kind": "round", "time": at, "order": event["order"], "round": name, "head": event.get("head"), "entries": entries,
                      "lanes": ran + still, "flaky": flaky.get(event.get("head") or "", [])})
    # Newest first, and of two in the same moment the later in the journal; the order is the journal's alone.
    first = shown[0]["time"] if shown else now
    found = sorted(shown + [one for one in notices if one["time"] >= first], key=lambda one: (-one["time"], -one["order"]))
    for one in found: del one["order"]
    return found


def data(repository, query):
    try: most = max(ROUNDS, min(2000, int(query.get("rounds") or ROUNDS)))
    except ValueError: raise LookupError("rounds is a number") from None
    journal = repository.events()
    return {"feed": rounds(journal, time.time(), most), "total": sum(1 for event in journal if event["kind"] == "round.began")}


def combine(parts, most=ROUNDS):
    """Every repository's feed at once, from parts, [(name, its Activity answer (data), None)] or (name, None, why
    it cannot be read), merged newest first and cut at most rounds, each row naming its repository."""
    rows = sorted(({**one, "repository": name} for name, found, _ in parts if found for one in found["feed"]), key=lambda one: -one["time"])
    kept, count = [], 0
    for one in rows:
        if one["kind"] == "round":
            if count == most: break
            count += 1
        kept.append(one)
    return {"feed": kept, "total": sum(found["total"] for _, found, _ in parts if found),
            "unread": [{"name": name, "error": why} for name, found, why in parts if found is None]}


def everywhere(repositories, query):
    try: most = max(ROUNDS, min(2000, int(query.get("rounds") or ROUNDS)))
    except ValueError: raise LookupError("rounds is a number") from None
    return combine(each(repositories, lambda repository: data(repository, query)), most)
