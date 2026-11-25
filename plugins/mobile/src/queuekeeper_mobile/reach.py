"""Which Macs reach each physical device, and how, as each Mac's platform tools list it (Platform.listing_here: an
iPhone's by `devicectl`, an Android phone's by `adb devices`); and the owner's declaration of each device: the ways
a Mac may reach it, in his order, and what it means when no Mac does (vocaro/queuekeeper#211).

The owner declares; Queuekeeper reads. What a Mac reads never adds a device to the farm or takes one out: a device
that nobody declared is listed as seen and never used, and a declared one that no Mac reaches stays in the record
with its reservation. A declaration is kept beside the record's entries, under the entry's key, as the owner's
notification setup is, so declaring changes no entry and no holder's revision. A run on declared devices starts
only on a Mac that reaches each of them by a declared way (place, placement).

Reach is how the platform's tool says the device is attached: a cable or the network. A simulator or an emulator is
left out. Each platform's listing says how it reads that (the Apple plugin's device_listing, the Android plugin's android_platform)."""
import concurrent.futures
import contextlib
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from queuekeeper import offload_host
from . import platforms

# The ways a declaration may name, in no order of their own.
WAYS = ("cable", "network")
# What a declared device's absence means: an error, for one that should always be reachable, or offline, for one
# that may be away.
ABSENT = ("error", "offline")
# Where the record keeps the owner's declarations, by entry key, beside the entries.
DECLARATIONS = "declarations"
# The keys of a declaration: the owner's three, and who declared it and when.
KEYS = ("reach", "cableOn", "absent", "declaredBy", "declaredAt")
# Seconds a device may go unreached before it counts as unreachable: by one way on one Mac, or by every declared way on
# every Mac, after which its absence counts, as an error or as offline. One drop is not a decision: a momentary loss
# of Wi-Fi, a device's sleep or a cable moved from one port to another neither raises the error nor, once placement
# reads it, causes a fallback.
SETTLE = 300
# The file beside the record where each Mac's readings of the declared devices are remembered, so that a device's
# absence is measured from the first reading that missed it.
HISTORY = "device-reach.json"
# Seconds one Mac's listing may take, over SSH included. On 2026-11-17 devicectl's took 0.17 to 0.42 s here and
# 0.56 s from another Mac over SSH.
TIMEOUT = 30


def mac_here():
    """The name readings give this Mac: its local host name, as the farm's other Macs know it."""
    return offload_host.mac_name()


def merged(read):
    """The devices every platform switched on lists, by read(platform), one listing. A platform whose listing
    fails fails the whole reading: what it would have listed is unknown, not absent."""
    found = {}
    for platform in platforms.PLATFORMS:
        found.update(read(platform))
    return found


def read_here():
    """This Mac's devices, as its platforms list them: (one listing, seconds the listing took)."""
    started = time.monotonic()
    return merged(lambda platform: platform.listing_here()), time.monotonic() - started


def read_there(host):
    """The devices another Mac lists, read over SSH in its login shell: (one listing, seconds)."""
    started = time.monotonic()
    return merged(lambda platform: platform.listing_there(host)), time.monotonic() - started


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
            if isinstance(entry, dict) and platforms.physical(str(entry.get("device") or ""))]
    keys += [key for key in declarations if key not in keys]
    items, recorded = [], set()
    for key in keys:
        udid = platforms.canonical(str((entries.get(key) or {}).get("device") or "")) or None
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
        line = f"{head}. Read: {seen}" if seen else head
        if item.get("absence") and item["absence"]["state"] != "reached": line += f". {item['absence']['said']}"
        said.append(line)
    for item in found["unknown"]:
        named = ", ".join(str(part) for part in (item["kind"], item["model"], item["name"] and f"named {item['name']!r}")
                          if part)
        where = f"whether {deferred}'s record declares it is said there" if deferred else \
            "not in the record and not declared"
        said.append(f"Seen, {where}: {item['device']} ({named}). Read: {seen_text(item['seen'])}")
    for item in found["unread"]:
        said.append(f"{item['mac']}'s devices were not read: {item['error']}")
    return said


def history_path(record):
    """Where the readings of the record at path record are remembered: beside it."""
    return Path(record).with_name(HISTORY)


@contextlib.contextmanager
def _locked(path):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path.with_name(path.name + ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def _read_history(path):
    try: value = json.loads(path.read_text())
    except (OSError, ValueError): return {"version": 1, "devices": {}}
    if not isinstance(value, dict) or not isinstance(value.get("devices"), dict): return {"version": 1, "devices": {}}
    return value


def remember(path, found, now=None):
    """Fold survey's answer found into the history at path, under its lock, and return each declared device's
    history by UDID: when a Mac last reached it by a declared way (reachedAt) and since when none has
    (missingSince), and the same for each way on each Mac, keyed "MAC/WAY". A Mac that was not read changes nothing
    of its own ways, and leaves a device no read Mac reaches as it was: whether it is missing is not known."""
    now = time.time() if now is None else now
    with _locked(path):
        value = _read_history(path)
        for item in found["devices"]:
            declaration = item["declaration"]
            if declaration is None or item["problems"] or not item["device"]: continue
            kept = value["devices"].setdefault(item["device"], {"reachedAt": None, "missingSince": None, "ways": {}})
            for seen in item["seen"]:
                if seen["state"] == "unread": continue
                for way in declaration["reach"]:
                    mark = kept["ways"].setdefault(f"{seen['mac']}/{way}", {"seenAt": None, "missingSince": None})
                    if seen["state"] == "reached" and seen["way"] == way:
                        mark.update(seenAt=now, missingSince=None)
                    elif mark["missingSince"] is None:
                        mark["missingSince"] = now
            if any(seen["state"] == "reached" for seen in item["seen"]):
                kept.update(reachedAt=now, missingSince=None)
            elif not any(seen["state"] == "unread" for seen in item["seen"]) and kept["missingSince"] is None:
                kept["missingSince"] = now
        staged = path.with_name(f".{path.name}.{os.getpid()}")
        staged.write_text(json.dumps(value, indent=2) + "\n")
        os.replace(staged, path)
        return value["devices"]


def stamp(moment):
    """A remembered time as ISO 8601, to the second, or None."""
    return None if moment is None else datetime.fromtimestamp(moment, timezone.utc).isoformat(timespec="seconds")


def absence(item, kept, now=None, settle=SETTLE):
    """What the absence rule says of one declared device, item of survey's answer, given its history kept:
    {"key", "absent", "state", "since", "reachedAt", "text"}. state is reached; settling, unreached by every Mac
    for less than settle seconds; unreachable, for settle seconds or more, which its declaration's absent makes an
    error or offline; or unknown, unreached by every Mac read while another was not read. Either way the device
    stays in the record and its reservation stands."""
    now = time.time() if now is None else now
    declaration, kept = item["declaration"], kept or {}
    found = {"key": item["key"], "absent": declaration["absent"], "since": stamp(kept.get("missingSince")),
             "reachedAt": stamp(kept.get("reachedAt"))}
    seen = "; ".join(f"{each['mac']} {each['detail']}" for each in item["seen"])
    tried = f"tried {ways_text(declaration)}; read: {seen}"
    if any(each["state"] == "reached" for each in item["seen"]):
        return {**found, "state": "reached", "since": None, "text": f"{item['key']} is reached", "said": "Reached"}
    unread = [each["mac"] for each in item["seen"] if each["state"] == "unread"]
    if unread:
        return {**found, "state": "unknown",
                "text": f"whether any Mac reaches {item['key']} is not known: no Mac read reaches it, and "
                        f"{', '.join(unread)} {'was' if len(unread) == 1 else 'were'} not read; {tried}",
                "said": f"Whether a Mac reaches it is not known: {', '.join(unread)} not read"}
    missing = now - kept["missingSince"] if kept.get("missingSince") is not None else 0
    last = f", last reached {found['reachedAt']}" if found["reachedAt"] else ""
    if missing < settle:
        return {**found, "state": "settling",
                "text": f"{item['key']} has been reached by no Mac for {missing:.0f}s (since {found['since']}{last}); "
                        f"it counts as unreachable after {settle}s; {tried}",
                "said": f"Reached by no Mac for {missing:.0f}s, since {found['since']}{last}; unreachable after {settle}s"}
    if declaration["absent"] == "offline":
        return {**found, "state": "unreachable",
                "text": f"{item['key']} is offline since {found['since']}{last}, which its declaration allows: "
                        f"no error; {tried}",
                "said": f"Offline since {found['since']}{last}, which is no error"}
    return {**found, "state": "unreachable",
            "text": f"{item['key']}, declared always reachable, is reached by no Mac since {found['since']}{last}: "
                    f"{tried}",
            "said": f"Error: reached by no Mac since {found['since']}{last}, and declared always reachable"}


def judged(value, record, found=None, macs=None, now=None, settle=SETTLE):
    """survey's answer for the record value, kept at path record, with each usable declaration's absence (absence)
    under "absence", after remembering this reading beside the record. found are the Macs' readings, read now
    when not given; macs are the names a cable may name."""
    found = readings() if found is None else found
    if macs is None: macs = [reading["mac"] for reading in found]
    answer = survey(value, found, macs)
    if any(item["declaration"] is not None and not item["problems"] for item in answer["devices"]):
        kept = remember(history_path(record), answer, now)
        for item in answer["devices"]:
            if item["declaration"] is not None and not item["problems"] and item["device"]:
                item["absence"] = absence(item, kept.get(item["device"]), now, settle)
    return answer


def declared_value(record):
    """The record at path record, when it holds physical devices and declares any; else None, and nothing is read."""
    try: value = json.loads(Path(record).read_text())
    except (OSError, ValueError): return None
    if not isinstance(value, dict) or value.get(offload_host.DEFERRED) or not value.get(DECLARATIONS): return None
    return value


def findings(record):
    """What `doctor` says of the declared devices in the record at path record: (errors, warnings). A device
    declared always reachable that no Mac has reached for the settle time is an error; one whose reach is not known
    because a Mac was not read, and a declaration that is wrong, are warnings. An offline device is neither."""
    value = declared_value(record)
    if value is None: return [], []
    answer = judged(value, record)
    errors, warnings = [], []
    for item in answer["devices"]:
        if item["declaration"] is not None and item["problems"]:
            warnings.append(f"{item['key']}'s device declaration is wrong, so it is taken as undeclared: "
                            f"{'; '.join(item['problems'])}")
        found = item.get("absence")
        if not found or found["absent"] != "error": continue
        if found["state"] == "unreachable": errors.append(found["text"])
        elif found["state"] == "unknown": warnings.append(found["text"])
    warnings += cable_findings(answer, _read_history(history_path(record))["devices"])
    return errors, warnings


def offline_gates(record, config):
    """doctor's warnings of each lane a landing runs (config.landing_lanes: the gate, queue.lanes and the lanes run
    beside them) that needs a device the record at path record declares rightly with absent offline. Whenever no
    Mac reaches that device the lane gets no verdict, and after three such runs the queue sends the batch back. A
    device that is undeclared, declared wrongly or declared error gives none (vocaro/queuekeeper#211)."""
    from queuekeeper.config import landing_lanes
    from .settings import lane_devices
    value = declared_value(record)
    if value is None: return []
    declarations = value[DECLARATIONS]
    if not isinstance(declarations, dict): return []
    lanes = config.get("lanes", {})
    warnings = []
    for name, why in landing_lanes(lanes, config.get("queue", {})).items():
        lane = lanes.get(name)
        if not isinstance(lane, dict): continue
        for key in lane_devices(lane):
            declaration = declarations.get(key)
            if declaration is None or problems(declaration) or declaration["absent"] != "offline": continue
            warnings.append(f"{name} needs {key}, declared --absent offline, and {why}: whenever no Mac reaches "
                            f"{key}, {name} is not run and gives no verdict, and after three such runs the queue "
                            f"sends the batch back. Move that check to a lane the queue does not gate on, such as "
                            f"one of release.lanes, or declare {key} --absent error if it should always be there")
    return warnings


def health(record):
    """What the health sign reads of the declared devices in the record at path record: each one's name, state,
    absent and text (absence), or none when the record declares none."""
    value = declared_value(record)
    if value is None: return []
    return [{"name": item["key"], **{name: item["absence"][name] for name in ("state", "absent", "since", "text")}}
            for item in judged(value, record)["devices"] if "absence" in item]


# Seconds between readings while a device run waits for a device, or for a way it prefers, to settle.
POLL = 15


def _way_settling(item, kept, best, now, settle):
    """Why a way that item's declaration lists before best, the index of the earliest way any Mac reaches its device
    by now, is settling: seen on a Mac and missing there for less than settle seconds, so that one drop causes no
    fallback; or None."""
    for way in item["declaration"]["reach"][:best]:
        for mark, seen in (kept.get("ways") or {}).items():
            mac, _slash, marked = mark.rpartition("/")
            if marked != way or seen.get("seenAt") is None or seen.get("missingSince") is None: continue
            missing = now - seen["missingSince"]
            if missing < settle:
                return (f"{item['key']}'s {way} on {mac} has not answered for {missing:.0f}s, and a run waits "
                        f"{settle}s before it uses a way declared after it")
    return None


def place(items, kept, here, now=None, settle=SETTLE, *, away=False, on=None, among=None):
    """Where a run on the declared devices items may start (vocaro/queuekeeper#211). items are judged's items, each
    with its absence; kept is the remembered history by UDID (remember); here is this Mac's name. A Mac qualifies
    when it reaches every one of the devices by a way its declaration lists. The one whose ways come earliest in the
    declarations ranks first, and between equals the readings' order decides: this Mac, then the farm's in order.
    Without away the run starts on this Mac, which holds the reservations, when it qualifies; away asks for another
    Mac, the first that qualifies, or on, by name; among, when given, names the Macs it may choose from.

    The answer's state is run, with the Mac in mac, the way to each device in ways and each fallback to a later way
    in fallback; elsewhere, when no Mac the run may use qualifies, with those that do in macs; wait, while a device
    or a way it prefers is settling or a Mac was not read; or hold or offline, when a device is unreachable and its
    declaration makes that an error or allows it. said says why, as phrases."""
    now = time.time() if now is None else now
    lost = [item for item in items if item["absence"]["state"] == "unreachable"]
    if lost:
        error = any(item["absence"]["absent"] == "error" for item in lost)
        return {"state": "hold" if error else "offline", "said": [item["absence"]["text"] for item in lost]}
    reached = {}
    for item in items:
        ways = item["declaration"]["reach"]
        reached[item["key"]] = {seen["mac"]: ways.index(seen["way"]) for seen in item["seen"]
                                if seen["state"] == "reached"}
    waiting = [item["absence"]["text"] for item in items if item["absence"]["state"] in ("settling", "unknown")]
    for item in items:
        best = min(reached[item["key"]].values(), default=len(item["declaration"]["reach"]))
        settling = _way_settling(item, kept.get(item["device"]) or {}, best, now, settle)
        if settling: waiting.append(settling)
    if waiting: return {"state": "wait", "said": waiting}
    order = [seen["mac"] for seen in items[0]["seen"]]
    every = sorted((mac for mac in order if all(mac in by for by in reached.values())),
                   key=lambda mac: (sum(by[mac] for by in reached.values()), order.index(mac)))
    usable = [mac for mac in every if not (away and mac == here)]
    if on is not None: usable = [mac for mac in usable if mac.lower() == on.lower()]
    if among is not None: usable = [mac for mac in usable if mac.lower() in {name.lower() for name in among}]
    if not away and here not in every: usable = []
    if not usable:
        keys = " and ".join(reached)
        if every:
            said = f"{keys} {'is' if len(reached) == 1 else 'are'} reached by a declared way only from {', '.join(every)}"
        else:
            said = f"no one Mac reaches {keys} by a declared way: " + "; ".join(
                f"{key} from {', '.join(by) or 'none'}" for key, by in reached.items())
        return {"state": "elsewhere", "macs": every, "said": [said]}
    mac = here if not away else usable[0]
    ways = {item["key"]: item["declaration"]["reach"][reached[item["key"]][mac]] for item in items}
    fallback = []
    for item in items:
        index = reached[item["key"]][mac]
        if index == 0: continue
        better = [other for other, at in reached[item["key"]].items() if at < index]
        fallback.append(f"{item['key']} by {ways[item['key']]}, not by {' or '.join(item['declaration']['reach'][:index])}"
                        + (f", which only {', '.join(better)} reaches it by" if better else
                           ", by which no Mac reaches it now"))
    said = [f"on {mac}, which reaches " + ", ".join(f"{key} by {way}" for key, way in ways.items())]
    return {"state": "run", "mac": mac, "ways": ways, "fallback": fallback, "said": said}


def placement(record, keys, name, *, away=False, on=None, among=None, read=None, settle=SETTLE, poll=POLL):
    """Where a run of name on the devices of keys may start, as their declarations in the record at path record
    allow (place); None when the record declares none of them or defers physical devices, which leaves the run as
    it was. read, when given, returns the Macs' readings in place of readings(). While a device or a way it prefers
    is settling, this reads again every poll seconds, for at most settle seconds and one poll more; past that the
    run is not run.

    The answer: {"status": None to run, "hold" when a device declared always reachable is reached by no Mac, or
    "not run"; "mac", the Mac the run starts on; "lines", what to say; "receipt", what its receipt keeps}."""
    started = time.monotonic()
    said_waiting = False
    while True:
        value = declared_value(record)
        if value is None or not any(key in value[DECLARATIONS] for key in keys): return None
        answer = judged(value, record, read() if read is not None else None, settle=settle)
        items = [item for item in answer["devices"] if item["key"] in keys and "absence" in item]
        if not items: return None
        found = place(items, _read_history(history_path(record))["devices"], mac_here(), settle=settle, away=away,
                      on=on, among=among)
        if found["state"] != "wait" or time.monotonic() - started >= settle + poll: break
        if not said_waiting:
            print(f"queuekeeper: {name} waits for its devices: {'; '.join(found['said'])}", file=sys.stderr, flush=True)
            said_waiting = True
        time.sleep(poll)
    why = "; ".join(found["said"])
    if found["state"] == "run":
        lines = [f"queuekeeper: {name} runs {why}"]
        lines += [f"queuekeeper: {name} falls back: {text}" for text in found["fallback"]]
        return {"status": None, "mac": found["mac"], "lines": lines,
                "receipt": {"mac": found["mac"], "ways": found["ways"], "fallback": found["fallback"]}}
    if found["state"] == "hold":
        said = (f"{name} not run: {why}. It runs once a Mac reaches the device by a declared way; `qk devices status` "
                "says what each Mac reads")
        return {"status": "hold", "mac": None, "receipt": None, "lines": [f"queuekeeper: {said}"]}
    if found["state"] == "offline":
        said = f"{name} not run: {why}. No verdict counts against the commits"
    elif found["state"] == "wait":
        said = f"{name} not run: its devices did not settle within {settle + poll}s: {why}"
    else:
        said = f"{name} not run: {why}"
        if found["macs"] and not away:
            flags = " ".join(f"--device {key}" for key in keys)
            said += (f". Its devices are reserved in this Mac's record: hold them here and send the run with "
                     f"`qk remote run {flags} --host {found['macs'][0]} {name}`")
    return {"status": "not run", "mac": None, "receipt": None, "lines": [f"queuekeeper: {said}"]}


def cable_findings(answer, kept, now=None, settle=SETTLE):
    """The warnings doctor gives of a declared device whose cable is expected on a Mac (cableOn) and has not
    answered there for settle seconds while the device is reached some other way: the owner expected the cable."""
    now = time.time() if now is None else now
    found = []
    for item in answer["devices"]:
        declaration, absence_ = item["declaration"], item.get("absence")
        if not absence_ or absence_["state"] != "reached" or not declaration.get("cableOn"): continue
        marks = (kept.get(item["device"]) or {}).get("ways") or {}
        mark = next((seen for name, seen in marks.items()
                     if name.lower() == f"{declaration['cableOn']}/cable".lower()), None)
        if mark is None or mark.get("missingSince") is None or now - mark["missingSince"] < settle: continue
        last = f", last {stamp(mark['seenAt'])}" if mark.get("seenAt") else ""
        found.append(f"{item['key']}'s cable on {declaration['cableOn']} has not answered since "
                     f"{stamp(mark['missingSince'])}{last}; it is reached another way: {seen_text(item['seen'])}")
    return found
