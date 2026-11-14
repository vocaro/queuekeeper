"""The Farm tab: each Mac's admission slots, who holds them and who waits, for every project, and what admission
judges the Mac by: its CPU's busy share, its load, its memory pressure and whether its screen is locked.

Reading the second Mac takes a second or more over SSH for each question, so the readings are made in a thread of
their own and kept: a request is answered at once with the last ones, and ones older than FRESH seconds start
others."""
import json
import os
import subprocess
import threading
import time

from .... import offload_host, remote, resources
from .. import admission_ids

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


def connections_of(answer):
    """Physical devices by hardware UDID, from current or older Xcode devicectl JSON.
    CoreDevice identifiers and simulator UUIDs must never be matched to a reservation by name.
    """
    result = answer.get("result") if isinstance(answer, dict) else None
    devices = result.get("devices") if isinstance(result, dict) else None
    if not isinstance(devices, list): raise ValueError("Xcode returned no device inventory")
    found = {}
    for device in devices:
        if not isinstance(device, dict): continue
        properties = device.get("properties") or {}
        hardware = properties.get("hardware") or device.get("hardwareProperties") or {}
        udid = str(hardware.get("udid") or "").upper()
        if hardware.get("reality") == "simulated" or not offload_host.PHYSICAL.fullmatch(udid): continue
        connection = properties.get("connection") or device.get("connectionProperties") or {}
        state = connection.get("state") or connection.get("tunnelState")
        found[udid] = {"connection": state if state in ("connected", "disconnected", "connecting", "unavailable") else "unknown",
                       "transport": connection.get("transportType")}
    return found


def read_connections():
    """A bounded, read-only Xcode query; runs in the farm's background refresh, never on a page request."""
    result = subprocess.run(["xcrun", "devicectl", "list", "devices", "--timeout", "10", "--quiet", "--json-output", "-"],
                            capture_output=True, text=True, timeout=15)
    if result.returncode:
        raise RuntimeError(f"Xcode device query exited {result.returncode}: {result.stderr.strip()[-400:]}")
    return connections_of(json.loads(result.stdout))


def read_devices():
    """Physical-device reservations alongside Xcode's observed connection status, without changing either."""
    try:
        record = offload_host.device_record_whole()
        connections, connection_error, checked = {}, None, None
        # A deferring Mac's inventory says nothing about connections on the Mac managing its devices.
        if not record.get(offload_host.DEFERRED):
            try:
                connections = read_connections()
                checked = time.time()
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                connection_error = str(error)
        found = []
        for key, entry in record["devices"].items():
            if not isinstance(entry, dict) or not offload_host.PHYSICAL.fullmatch(str(entry.get("device") or "").upper()):
                continue
            state = entry.get("state") or "held"
            if entry.get("uncertain") or state == "restoration-unverified": state = "needs recovery"
            elif entry.get("activeRun"): state = "run recorded"
            found.append({"name": key, "state": state, "owner": entry.get("ownerChatID") if state != "released" else None,
                          "purpose": entry.get("purpose") if state != "released" else None,
                          "releaseBy": entry.get("expectedReleaseBy") if state != "released" else None,
                          "waiting": len(record.get("queues", {}).get(key, [])),
                          **connections.get(str(entry["device"]).upper(), {
                              "connection": "notDetected" if checked is not None else "unknown", "transport": None})})
        return {"devices": sorted(found, key=lambda item: item["name"]),
                "deferredTo": record.get(offload_host.DEFERRED), "error": None,
                "connectionError": connection_error, "checkedAt": checked}
    except Exception as error:  # noqa: BLE001
        return {"devices": [], "deferredTo": None, "error": str(error), "connectionError": None, "checkedAt": None}


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
        STATE.update(at=time.time(), busy=False, error=error)


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
    return {"name": found.get("name"), "role": found.get("role"), "state": found.get("state"),
            "reasons": found.get("reasons") or [], "load": detail,
            "slots": {"used": sum(1 for slot in slots if slot.get("holder")), "guaranteed": limits.get("slots"),
                      "most": limits.get("slotsMax") or len(slots)},
            "holders": [{"id": names.get(holder.get("identity"), holder.get("identity")), "label": holder.get("label"), "heldSeconds": holder.get("heldSeconds"),
                         "slots": len(holder.get("slots") or []), "wholeHost": bool(holder.get("wholeHost")),
                         "exclusive": bool(holder.get("exclusive"))} for holder in found.get("holders") or []],
            "waiters": [{"id": names.get(waiter.get("request") or waiter.get("identity"), waiter.get("request") or waiter.get("identity")), "label": waiter.get("label"), "waitedSeconds": waiter.get("waitedSeconds"),
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
    with LOCK: devices = STATE.get("devices")
    return {"deviceRecord": devices, "readAt": at if reading else None, "error": error,
            **({"admission": admission(reading, query.get("host"), query["admission"], names)} if query.get("admission") else {}),
            "hosts": [host(found, names) for found in (reading or {}).get("hosts", [])] if reading else None}


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
