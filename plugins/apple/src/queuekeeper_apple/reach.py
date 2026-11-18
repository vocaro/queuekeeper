"""Which Macs reach each physical device, and how, as each Mac's `devicectl` lists it; and the owner's declaration of
each device: the ways a Mac may reach it, in his order, and what it means when no Mac does (vocaro/queuekeeper#211,
doc/guides/devices.md, Declared devices).

The owner declares; Queuekeeper reads. What a Mac reads never adds a device to the farm or takes one out: a device
that nobody declared is listed as seen and never used, and a declared one that no Mac reaches stays in the record
with its reservation. A declaration is kept beside the record's entries, under the entry's key, as the owner's
notification setup is, so declaring changes no entry and no holder's revision.

Reach is `connectionProperties.transportType`: `wired` is a cable and `localNetwork` the network. A simulator lists
as `sameMachine` and is left out. `tunnelState` is not reach: on 2026-11-17 it read `disconnected` for a device
plainly attached by cable."""
import concurrent.futures
import json
import os
from pathlib import Path
import shlex
import subprocess
import time

from queuekeeper import offload_host

# The ways a declaration may name, in no order of their own, and devicectl's transport for each.
WAYS = ("cable", "network")
TRANSPORTS = {"wired": "cable", "localNetwork": "network"}
# devicectl's transport for a simulator, which runs on the Mac that lists it.
SAME_MACHINE = "sameMachine"
# What a declared device's absence means: an error, for one that should always be reachable, or offline, for one
# that may be away.
ABSENT = ("error", "offline")
# Where the record keeps the owner's declarations, by entry key, beside the entries.
DECLARATIONS = "declarations"
# The keys of a declaration: the owner's three, and who declared it and when.
KEYS = ("reach", "cableOn", "absent", "declaredBy", "declaredAt")
# A file holding a devicectl listing that this Mac's reading takes in place of devicectl's, for tests only: every
# test module sets it to an empty listing (tests/host_isolation.py), so no test reads this Mac's devices.
LISTING = "QUEUEKEEPER_DEVICE_LISTING"
# The listing, read-only. devicectl's own --timeout bounds its wait for devices to answer.
COMMAND = ("xcrun", "devicectl", "list", "devices", "--quiet", "--timeout", "10", "--json-output", "-")
# Seconds one Mac's listing may take, over SSH included. On 2026-11-17 it took 0.17 to 0.42 s here and 0.56 s from
# another Mac over SSH.
TIMEOUT = 30


def listed(answer):
    """Each physical device a devicectl listing names, by hardware UDID: the way it reaches the Mac that listed it
    now (cable, network, or None when it is listed by no way a declaration may name), devicectl's own transport,
    and the device's kind, model and name. Simulators are left out. Current and older devicectl JSON alike."""
    result = answer.get("result") if isinstance(answer, dict) else None
    devices = result.get("devices") if isinstance(result, dict) else None
    if not isinstance(devices, list): raise ValueError("devicectl's listing holds no devices")
    found = {}
    for device in devices:
        if not isinstance(device, dict): continue
        properties = device.get("properties") if isinstance(device.get("properties"), dict) else {}
        hardware = properties.get("hardware") or device.get("hardwareProperties") or {}
        connection = properties.get("connection") or device.get("connectionProperties") or {}
        about = properties.get("device") or device.get("deviceProperties") or {}
        udid = str(hardware.get("udid") or "").upper()
        transport = connection.get("transportType")
        if hardware.get("reality") == "simulated" or transport == SAME_MACHINE or \
                not offload_host.PHYSICAL.fullmatch(udid):
            continue
        text = lambda value: value if isinstance(value, str) else None
        found[udid] = {"way": TRANSPORTS.get(transport), "transport": text(transport),
                       "kind": text(hardware.get("deviceType")), "model": text(hardware.get("marketingName")),
                       "name": text(about.get("name"))}
    return found


def mac_here():
    """The name readings give this Mac: its local host name, as the farm's other Macs know it."""
    return offload_host.mac_name()


def read_here(run=subprocess.run):
    """This Mac's devices, as listed: (listed's answer, seconds the listing took)."""
    started = time.monotonic()
    if os.environ.get(LISTING):
        text = Path(os.environ[LISTING]).read_text()
    else:
        done = run(list(COMMAND), capture_output=True, text=True, timeout=TIMEOUT, stdin=subprocess.DEVNULL)
        if done.returncode:
            raise RuntimeError(f"devicectl exited {done.returncode}: {(done.stderr or '').strip()[-300:]}")
        text = done.stdout
    return listed(json.loads(text)), time.monotonic() - started


def read_there(host):
    """The devices another Mac lists, read over SSH in its login shell: (listed's answer, seconds)."""
    from queuekeeper import remote
    started = time.monotonic()
    done = remote.ssh(host, shlex.join(COMMAND), stdin=subprocess.DEVNULL, capture_output=True, text=True,
                      timeout=TIMEOUT)
    if done.returncode:
        said = (done.stderr or "").strip().splitlines()
        raise RuntimeError(f"its devicectl listing exited {done.returncode}" + (f": {said[-1][-300:]}" if said else ""))
    return listed(json.loads(done.stdout)), time.monotonic() - started


def farm_macs():
    """(name, SSH destination) of each of the farm's Macs, paused ones too, which still reach devices; none when
    the farm cannot be read."""
    from queuekeeper import farm
    try: return [(mac["name"], mac["ssh"]) for mac in farm.read()]
    except ValueError: return []


def readings(here=None, there=None, macs=None):
    """Each Mac's reading, this Mac first and then the farm's in its order, read at once: {"mac", "host" (None for
    this Mac), "readAt", and "devices" and "seconds", or "error"}. A Mac that cannot be read is in the list with
    its error, never left out."""
    macs = farm_macs() if macs is None else macs
    here, there = here or read_here, there or read_there
    targets = [(mac_here(), None), *macs]

    def read(target):
        name, host = target
        at = time.time()
        try:
            found, seconds = here() if host is None else there(host)
            return {"mac": name, "host": host, "readAt": at, "devices": found, "seconds": round(seconds, 2)}
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            return {"mac": name, "host": host, "readAt": at, "error": str(error) or type(error).__name__}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(targets)) as pool:
        return list(pool.map(read, targets))


def problems(declaration, macs=None):
    """What is wrong with one device's declaration; none when it is right. macs, when given, are the names of the
    Macs a cable may name: this Mac's and the farm's."""
    if not isinstance(declaration, dict): return [f"a declaration is an object, not {declaration!r}"]
    found = []
    unknown = sorted(set(declaration) - set(KEYS))
    if unknown: found.append(f"it holds {', '.join(unknown)}, which a declaration does not take")
    reach = declaration.get("reach")
    if not isinstance(reach, list) or not reach or not all(isinstance(way, str) and way in WAYS for way in reach) \
            or len(set(reach)) != len(reach):
        found.append(f"reach is the ways a Mac may reach it, in order, each of {' or '.join(WAYS)} once, not {reach!r}")
        reach = []
    cable_on = declaration.get("cableOn")
    if cable_on is not None:
        if not isinstance(cable_on, str) or not cable_on:
            found.append(f"cableOn names the Mac its cable is expected on, not {cable_on!r}")
        elif "cable" not in reach:
            found.append("cableOn names the Mac its cable is expected on, and reach lists no cable")
        elif macs is not None and cable_on.lower() not in {mac.lower() for mac in macs}:
            found.append(f"cableOn names {cable_on}, which is neither this Mac nor one of the farm's: "
                         f"{', '.join(macs)}")
    if declaration.get("absent") not in ABSENT:
        found.append(f"absent is {' or '.join(ABSENT)}, not {declaration.get('absent')!r}")
    return found


def declared(reach, cable_on=None, absent=None, *, by, at):
    """A declaration as the record keeps it."""
    return {"reach": list(reach), **({"cableOn": cable_on} if cable_on else {}), "absent": absent,
            "declaredBy": by, "declaredAt": at}


def ways_text(declaration):
    """A declaration's ways, as status says them: cable on studio, then network."""
    said = [f"cable on {declaration['cableOn']}" if way == "cable" and declaration.get("cableOn") else way
            for way in declaration.get("reach") or []]
    return ", then ".join(said)


def seen_on(udid, reading, declaration=None):
    """What one Mac's reading says of the device udid: {"mac", "state", "way", "transport", "detail"}, where state
    is reached (by a declared way, or any way for an undeclared device), other (listed, by a way its declaration
    does not list, or by none a declaration may name), unlisted, or unread."""
    base = {"mac": reading["mac"], "way": None, "transport": None}
    if "error" in reading: return {**base, "state": "unread", "detail": reading["error"]}
    device = reading["devices"].get(udid)
    if device is None: return {**base, "state": "unlisted", "detail": "not listed"}
    found = {**base, "way": device["way"], "transport": device["transport"]}
    if device["way"] is None:
        return {**found, "state": "other", "detail": f"listed, by no way a Mac may reach it ({device['transport'] or 'no transport'})"}
    if declaration is not None and device["way"] not in (declaration.get("reach") or []):
        return {**found, "state": "other", "detail": f"by {device['way']}, which its declaration does not list"}
    if declaration is not None and device["way"] == "cable" and declaration.get("cableOn") and \
            declaration["cableOn"].lower() != reading["mac"].lower():
        return {**found, "state": "reached", "detail": f"by cable, which is expected on {declaration['cableOn']}"}
    return {**found, "state": "reached", "detail": f"by {device['way']}"}


def survey(value, found, macs=None):
    """What the record value and the Macs' readings found say of the devices: {"devices": one item for each entry
    of a physical device and each declaration, in the record's order, "unknown": each physical device a Mac lists
    that no entry records, "unread": each Mac not read}. Each item has its key, device, declaration (None when
    undeclared), the declaration's problems and what each Mac saw of it. macs are the names a cable may name."""
    entries = value.get("devices") or {}
    declarations = value.get(DECLARATIONS) if isinstance(value.get(DECLARATIONS), dict) else {}
    keys = [key for key, entry in entries.items()
            if isinstance(entry, dict) and offload_host.PHYSICAL.fullmatch(str(entry.get("device") or "").upper())]
    keys += [key for key in declarations if key not in keys]
    items, recorded = [], set()
    for key in keys:
        udid = str((entries.get(key) or {}).get("device") or "").upper() or None
        declaration = declarations.get(key)
        wrong = problems(declaration, macs) if declaration is not None else []
        if declaration is not None and udid is None:
            wrong.append(f"the record holds no physical device under {key}")
        usable = declaration if declaration is not None and not wrong else None
        recorded.add(udid)
        items.append({"key": key, "device": udid, "declaration": declaration, "problems": wrong,
                      "seen": [seen_on(udid, reading, usable) for reading in found] if udid else []})
    unknown = {}
    for reading in found:
        for udid, device in (reading.get("devices") or {}).items():
            if udid not in recorded: unknown.setdefault(udid, device)
    return {"devices": items,
            "unknown": [{"device": udid, **{name: device[name] for name in ("kind", "model", "name")},
                         "seen": [seen_on(udid, reading) for reading in found]} for udid, device in unknown.items()],
            "unread": [{"mac": reading["mac"], "error": reading["error"]} for reading in found if "error" in reading]}


def seen_text(seen):
    """What each Mac saw, in a line: build-host by network; studio not listed."""
    return "; ".join(f"{item['mac']} {item['detail']}" for item in seen if item["state"] != "unread")


def lines(found, deferred=None):
    """What `devices status` says of reach, from survey's answer found."""
    said = []
    if deferred:
        said.append(f"Devices are declared in {deferred}'s record, which holds physical devices; this record "
                    "declares none")
    for item in found["devices"]:
        declaration = item["declaration"]
        if declaration is None:
            head = f"{item['key']}: not declared"
        elif item["problems"]:
            head = f"{item['key']}: declared wrongly, so taken as undeclared: {'; '.join(item['problems'])}"
        else:
            means = "an error" if declaration["absent"] == "error" else "offline, no error"
            who = f" by {declaration['declaredBy']}" if declaration.get("declaredBy") else ""
            when = f" at {declaration['declaredAt']}" if declaration.get("declaredAt") else ""
            head = f"{item['key']}: declared{who}{when}: reach {ways_text(declaration)}; reached by no Mac means {means}"
        seen = seen_text(item["seen"])
        said.append(f"{head}. Read: {seen}" if seen else head)
    for item in found["unknown"]:
        named = ", ".join(str(part) for part in (item["kind"], item["model"], item["name"] and f"named {item['name']!r}")
                          if part)
        where = f"whether {deferred}'s record declares it is said there" if deferred else \
            "not in the record and not declared"
        said.append(f"Seen, {where}: {item['device']} ({named}). Read: {seen_text(item['seen'])}")
    for item in found["unread"]:
        said.append(f"{item['mac']}'s devices were not read: {item['error']}")
    return said
