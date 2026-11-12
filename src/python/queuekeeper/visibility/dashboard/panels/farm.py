"""The Farm tab: each Mac's admission slots, who holds them and who waits, for every project, and what admission
judges the Mac by: its CPU's busy share, its load, its memory pressure and whether its screen is locked.

Reading the second Mac takes a second or more over SSH for each question, so the readings are made in a thread of
their own and kept: a request is answered at once with the last ones, and ones older than FRESH seconds start
others."""
import os
import threading
import time

from .... import offload_host, remote, resources

TITLE, ORDER = "Farm", 20
FRESH = 10.0
STATE = {"reading": None, "detail": {}, "at": 0.0, "busy": False, "error": None}
LOCK = threading.Lock()
MEMORY = {1: "normal", 2: "warning", 4: "critical"}


def read():
    # A process in a namespace of its own, such as a test's, reads that namespace, which is this Mac's alone.
    isolated = any(os.environ.get(name) for name in ("HEAVY_SLOT_DIR", "QUEUEKEEPER_GATE_LOCK_FILE"))
    return resources.admissions_now(here_only=isolated)


def load_of(pressure, screen):
    """What a Mac's pressure answer (offload_host.pressure) and its screen reading (offload_host.screen) say, as
    the page shows it. What either lacks is None."""
    pressure, screen = (found if isinstance(found, dict) else {} for found in (pressure, screen))
    number = lambda value: value if isinstance(value, (int, float)) and not isinstance(value, bool) else None
    return {"cpuBusy": number(pressure.get("cpuBusy")), "busyLimit": number(pressure.get("busyLimit")),
            "load": number(pressure.get("load")), "memory": MEMORY.get(pressure.get("memory")),
            "screen": screen.get("state") if screen.get("state") in ("locked", "unlocked") else "unknown",
            "error": pressure.get("error")}


def read_detail():
    """{role: load_of(...)} for this Mac, `here`, and the second Mac when one is configured, `second`. A Mac that
    cannot be asked has its reason as its error."""
    found = {"here": load_of(offload_host.pressure(), offload_host.screen())}
    host = remote.configured()
    if host:
        try: found["second"] = load_of(remote.reading_there(host)[0], remote.screen_there(host))
        except Exception as error:  # noqa: BLE001
            found["second"] = {**load_of({}, {}), "error": str(getattr(error, "problem", None) or error)}
    return found


def refresh():
    try: reading, error = read(), None
    except Exception as failure:  # noqa: BLE001
        reading, error = None, str(failure)
    try: detail = read_detail()
    except Exception:  # noqa: BLE001
        detail = None
    with LOCK:
        if reading is not None: STATE["reading"] = reading
        if detail is not None: STATE["detail"] = detail
        STATE.update(at=time.time(), busy=False, error=error)


def current():
    """(the last reading or None, when it was made, why the last try failed or None), starting a new reading in
    the background when the last is old."""
    with LOCK:
        if not STATE["busy"] and time.time() - STATE["at"] > FRESH:
            STATE["busy"] = True
            threading.Thread(target=refresh, daemon=True).start()
        return STATE["reading"], STATE["at"], STATE["error"]


def host(found):
    slots = found.get("slots") or []
    limits = found.get("limits") or {}
    with LOCK: detail = STATE["detail"].get(found.get("role"))
    return {"name": found.get("name"), "role": found.get("role"), "state": found.get("state"),
            "reasons": found.get("reasons") or [], "load": detail,
            "slots": {"used": sum(1 for slot in slots if slot.get("holder")), "guaranteed": limits.get("slots"),
                      "most": limits.get("slotsMax") or len(slots)},
            "holders": [{"label": holder.get("label"), "heldSeconds": holder.get("heldSeconds"),
                         "slots": len(holder.get("slots") or []), "wholeHost": bool(holder.get("wholeHost")),
                         "exclusive": bool(holder.get("exclusive"))} for holder in found.get("holders") or []],
            "waiters": [{"label": waiter.get("label"), "waitedSeconds": waiter.get("waitedSeconds"),
                         "reason": waiter.get("reason")} for waiter in found.get("waiters") or []]}


def data(repository, query):
    reading, at, error = current()
    return {"readAt": at if reading else None, "error": error,
            "hosts": [host(found) for found in (reading or {}).get("hosts", [])] if reading else None}


def summary(repository):
    reading, _, error = current()
    if not reading: return {"title": "Farm", "text": error or "Reading the Macs…", "level": "warning" if error else "plain"}
    hosts = [host(found) for found in reading.get("hosts", [])]
    read_ = [found for found in hosts if found["state"] == "observed"]
    unread = [found["name"] for found in hosts if found["state"] not in ("observed", "notConfigured")]
    waiting = sum(len(found["waiters"]) for found in read_)
    text = (f"{len(read_)} {'Mac' if len(read_) == 1 else 'Macs'} read, {sum(found['slots']['used'] for found in read_)} of "
            f"{sum(found['slots']['most'] for found in read_)} slots in use" + (f", {waiting} waiting" if waiting else ""))
    locked = [found["name"] for found in hosts if (found["load"] or {}).get("screen") == "locked"]
    if locked: text += f"; the screen of {', '.join(map(str, locked))} is locked"
    if unread: text += f"; {', '.join(map(str, unread))} could not be read"
    return {"title": "Farm", "text": text, "level": "warning" if unread else "plain"}
