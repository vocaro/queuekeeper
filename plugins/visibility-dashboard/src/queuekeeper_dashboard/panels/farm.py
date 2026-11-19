"""The Farm tab: each Mac's admission slots, who holds them and who waits, for every project, and what admission
judges the Mac by: its CPU's busy share, its load, its memory pressure and whether its screen is locked.

Reading another Mac takes a second or more over SSH for each question, so the readings are made in a thread of
their own and kept: a request is answered at once with the last ones, and ones older than FRESH seconds start
others. The last hour's readings are kept too, as samples (see sample), so the tab can draw each Mac's load and
the farm's slots by project over that hour; readings are made only while the tab is asked, so the hour may have
gaps."""
import json
import os
import subprocess
import threading
import time

from queuekeeper import plugins, farm, host_health, offload_host, remote, resources
from .. import admission_ids

TITLE, ORDER = "Farm", 20
FRESH = 10.0
STATE = {"reading": None, "detail": {}, "at": 0.0, "busy": False, "error": None, "samples": []}
KEPT = 3600.0  # the seconds of samples kept
LOCK = threading.Lock()
MEMORY = {1: "normal", 2: "warning", 4: "critical"}


def read():
    # A process in a namespace of its own, such as a test's, reads that namespace, which is this Mac's alone.
    isolated = any(os.environ.get(name) for name in ("HEAVY_SLOT_DIR", "QUEUEKEEPER_GATE_LOCK_FILE"))
    return resources.admissions_now(here_only=isolated)


def load_of(pressure, screen, where="this Mac"):
    """What a Mac's pressure answer (offload_host.pressure) and its screen reading (offload_host.screen) say, as
    the page shows it. What either lacks is None. health is the warning of a Mac short of memory that the pressure
    answer's health reading gives (host_health.warnings), where naming the Mac, or None, and shortage the same
    reading's parts for the page to lay out (host_health.shortage)."""
    pressure, screen = (found if isinstance(found, dict) else {} for found in (pressure, screen))
    number = lambda value: value if isinstance(value, (int, float)) and not isinstance(value, bool) else None
    return {"cpuBusy": number(pressure.get("cpuBusy")), "busyLimit": number(pressure.get("busyLimit")),
            "load": number(pressure.get("load")), "memory": MEMORY.get(pressure.get("memory")),
            "screen": screen.get("state") if screen.get("state") in ("locked", "unlocked") else "unknown",
            "error": pressure.get("error"), "health": (host_health.warnings(pressure.get("health"), where) or [None])[0],
            "shortage": host_health.shortage(pressure.get("health")),
            "hardware": hardware_of(pressure.get("hardware"))}


KINDS = ("laptop", "desktop")


def hardware_of(found):
    """A Mac's model and kind from its pressure answer (offload_host.hardware): its kind `other` when unknown or
    absent, as from another Mac whose toolkit predates the reading."""
    found = found if isinstance(found, dict) else {}
    model = found.get("model") if isinstance(found.get("model"), str) else None
    return {"model": model, "kind": found.get("kind") if found.get("kind") in KINDS else "other"}


def read_detail():
    """{role: load_of(...)} for this Mac, `here`, and each of the farm's Macs that take work now, by the role
    `qk admissions now` gives it (resources.farm_role). A Mac that cannot be asked has its reason as its error."""
    found = {"here": load_of({**offload_host.pressure(), "health": offload_host.host_health()}, offload_host.screen())}
    macs = farm.active()
    for mac in macs:
        host, role = mac["ssh"], resources.farm_role(mac, len(macs))
        try: found[role] = load_of(remote.reading_there(host, health=True)[0], remote.screen_there(host), host)
        except Exception as error:  # noqa: BLE001
            found[role] = {**load_of({}, {}), "error": str(getattr(error, "problem", None) or error)}
    return found






def read_devices():
    """Read the active device provider, or an empty inventory without one."""
    return plugins.DEVICES.observations()

def project_of(found):
    """The project a holder's or waiter's work is for, by its origin, or None when it names none."""
    origin = found.get("origin") if isinstance(found.get("origin"), dict) else {}
    return origin.get("repository") if isinstance(origin.get("repository"), str) and origin.get("repository") else None


def sample(reading, detail, at):
    """What one reading says for the last hour's charts: each read Mac's CPU busy share and load by its role, and
    the slots held across the farm by project, work that names none counted as "other", and how many slots the
    read Macs have."""
    macs, projects = {}, {}
    for found in (reading or {}).get("hosts", []):
        if found.get("state") != "observed": continue
        load = (detail or {}).get(found.get("role")) or {}
        macs[found.get("role")] = {"cpuBusy": load.get("cpuBusy"), "load": load.get("load")}
        for holder in found.get("holders") or []:
            name = project_of(holder) or "other"
            projects[name] = projects.get(name, 0) + len(holder.get("slots") or [])
    most = sum(len(found.get("slots") or []) for found in (reading or {}).get("hosts", []) if found.get("state") == "observed")
    return {"at": round(at, 1), "macs": macs, "projects": projects, "most": most}


def refresh():
    try: reading, error = read(), None
    except Exception as failure:  # noqa: BLE001
        reading, error = None, str(failure)
    try: detail = read_detail()
    except Exception:  # noqa: BLE001
        detail = None
    devices = read_devices()
    with LOCK:
        STATE["devices"] = devices
        if reading is not None: STATE["reading"] = reading
        if detail is not None: STATE["detail"] = detail
        now = time.time()
        if reading is not None:
            STATE["samples"] = [kept for kept in STATE["samples"] if kept["at"] > now - KEPT] + [sample(reading, STATE["detail"], now)]
        STATE.update(at=now, busy=False, error=error)


def current():
    """(the last reading or None, when it was made, why the last try failed or None), starting a new reading in
    the background when the last is old."""
    with LOCK:
        if not STATE["busy"] and time.time() - STATE["at"] > FRESH:
            STATE["busy"] = True
            threading.Thread(target=refresh, daemon=True).start()
        return STATE["reading"], STATE["at"], STATE["error"]


def host(found, names=None):
    names = names or {}
    slots = found.get("slots") or []
    limits = found.get("limits") or {}
    with LOCK: detail = STATE["detail"].get(found.get("role"))
    projects = {holder.get("identity"): project_of(holder) for holder in found.get("holders") or []}
    return {"name": found.get("name"), "role": found.get("role"), "state": found.get("state"),
            "reasons": found.get("reasons") or [], "load": detail,
            # Each slot in order: whether it is held, the project of the work holding it, and whether it is guaranteed.
            "cells": [{"held": bool(slot.get("holder")), "project": projects.get(slot.get("holder")),
                       "guaranteed": bool(slot.get("guaranteed"))} for slot in slots],
            "slots": {"used": sum(1 for slot in slots if slot.get("holder")), "guaranteed": limits.get("slots"),
                      "most": limits.get("slotsMax") or len(slots)},
            "holders": [{"id": names.get(holder.get("identity"), holder.get("identity")), "label": holder.get("label"), "heldSeconds": holder.get("heldSeconds"), "project": project_of(holder),
                         "slots": len(holder.get("slots") or []), "wholeHost": bool(holder.get("wholeHost")),
                         "exclusive": bool(holder.get("exclusive"))} for holder in found.get("holders") or []],
            "waiters": [{"id": names.get(waiter.get("request") or waiter.get("identity"), waiter.get("request") or waiter.get("identity")), "label": waiter.get("label"), "waitedSeconds": waiter.get("waitedSeconds"), "project": project_of(waiter),
                         "reason": waiter.get("reason")} for waiter in found.get("waiters") or []]}


def admission(reading, role, identifier, names=None):
    """Find a request only in the observed farm state. A waiting request keeps its link when it is admitted:
    its request ID becomes the holder's identity, even when its queue place used another identity.
    """
    names = names or {}
    fields = ("identity", "request", "label", "pid", "start", "origin", "process", "kind", "model", "slots",
              "wholeHost", "exclusive", "admittedAt", "heldSeconds", "waitedSeconds", "position", "queued",
              "refusal", "reason", "sampledSeconds", "stale", "away", "lastAskedSeconds", "holdsBack",
              "stranded", "cpuPercent", "progress")
    for host_ in (reading or {}).get("hosts", []):
        if host_.get("role") != role: continue
        for collection, state in (("holders", "running"), ("waiters", "waiting")):
            for found in host_.get(collection) or []:
                identity = found.get("request") or found.get("identity")
                if identifier not in (found.get("identity"), found.get("request"), names.get(identity), names.get(found.get("identity"))): continue
                return {**{field: found[field] for field in fields if field in found},
                        "id": names.get(identity, identifier), "host": host_.get("name"), "state": state}
    return {"id": identifier, "state": "missing"}


def data(repository, query):
    reading, at, error = current()
    names = admission_ids.names_for(reading)
    with LOCK: devices, samples = STATE.get("devices"), list(STATE["samples"])
    return {"deviceRecord": devices, "samples": samples, "readAt": at if reading else None, "error": error,
            **({"admission": admission(reading, query.get("host"), query["admission"], names)} if query.get("admission") else {}),
            "hosts": [host(found, names) for found in (reading or {}).get("hosts", [])] if reading else None}


def everywhere(repositories, query):
    """The farm is every project's already: admission is a Mac's, not a repository's."""
    return data(None, query)


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
    short = [found["name"] for found in hosts if (found["load"] or {}).get("health")]
    if short: text += f"; {', '.join(map(str, short))} {'is' if len(short) == 1 else 'are'} short of memory"
    if unread: text += f"; {', '.join(map(str, unread))} could not be read"
    return {"title": "Farm", "text": text, "level": "warning" if unread or short else "plain"}
