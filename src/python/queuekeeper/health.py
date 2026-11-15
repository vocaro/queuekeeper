"""`qk queue health`: whether the queue is healthy, in one sentence, with the five signs it is judged by and a
forecast of when the entries waiting land. The dashboard's Queue tab shows the same verdict, from the same function.

It never assumes health. A sign is ok only when a fresh reading confirms it; one whose reading is missing, failed or
stale is unknown, never ok. A definite problem outranks an unknown one, and the verdict is good only when no sign is
unknown. When it cannot tell, it says so and why, and exits UNKNOWN, the agent interface's code for unmeasured, so that
a script never reads an unknown as healthy.

assess is pure: it judges the readings it is given. gather makes them, reading the second Mac over SSH and the devices
through Xcode, for the command line; the dashboard passes the readings its Farm tab already keeps."""
import json
import math
import statistics
import sys
import time
from pathlib import Path

OK, WARNING, DANGER, UNKNOWN, IDLE = "ok", "warning", "danger", "unknown", "idle"
RANK = {DANGER: 4, WARNING: 3, UNKNOWN: 2, OK: 1, IDLE: 0}
HEALTHY, ATTENTION, UNKNOWN_STATUS = 0, 1, 3  # exit statuses: 0 healthy or quiet, 1 needs attention, 3 cannot tell

GATES = 20         # the passing gate runs whose median is the usual gate time, newest first
MEDIAN_RUNS = 5    # fewer passing runs than this give no usual time
SLOW = 1.5         # a gate past this many times its usual time is slow
WAIT_LONG = 600    # seconds a gate or round may wait for a Mac, an unlocked screen or admission before it is slow
SILENT = 900       # seconds a running gate's log may stay unchanged before the gate is stuck; a remote gate's log
                   # gets a heartbeat every 300 seconds
STARTING = 300     # seconds the worker may take between rounds while entries wait
STALE = 90         # seconds after which a Mac's reading no longer says how it is now
CAME_BACK = 6      # the settled entries the came-back sign counts over
SIGNS = ("progress", "pace", "macs", "devices", "cameBack")
TITLES = {"progress": "Progress", "pace": "Pace", "macs": "Macs", "devices": "Devices", "cameBack": "Came back"}
PANELS = {"progress": "queue", "pace": "history", "macs": "farm", "devices": "farm", "cameBack": "history"}
MEMORY = {1: "normal", 2: "warning", 4: "critical"}


def span(seconds):
    """A duration as people say it: 45s, 12m, 1h 05m."""
    seconds = max(0, int(seconds))
    if seconds < 60: return f"{seconds}s"
    if seconds < 3600: return f"{seconds // 60}m"
    return f"{seconds // 3600}h {seconds % 3600 // 60:02d}m"


def clock(at):
    """An epoch time as a local clock time, such as 1:10 PM."""
    return time.strftime("%I:%M %p", time.localtime(at)).lstrip("0")


def upper(text):
    return text[:1].upper() + text[1:]


def entries(count):
    return f"{count} entr{'y' if count == 1 else 'ies'}"


def usual_gate(journal):
    """The median seconds of the last GATES passing runs of the gate lane, or None with fewer than MEDIAN_RUNS."""
    runs = [event["seconds"] for event in journal if event.get("kind") == "gate.ended" and event.get("lane") == "gate"
            and event.get("status") == 0 and isinstance(event.get("seconds"), (int, float))][-GATES:]
    return statistics.median(runs) if len(runs) >= MEDIAN_RUNS else None


def sign(name, level, text, short=None):
    """A sign: its level, the clause the sentence uses, and a short form for a tile, the clause itself by default."""
    return {"name": name, "title": TITLES[name], "level": level, "text": text, "short": short or upper(text),
            "panel": PANELS[name]}


def worst(found):
    return max(found, key=lambda item: RANK[item[0]]) if found else (IDLE, "")


def progress(state, journal, usual, now, modified):
    """Whether the round's work is moving: each running gate against the usual gate time and by its log's growth, each
    wait by how long it has lasted, and, between rounds, how long the worker has taken to start the next."""
    if state.get("held"): return sign("progress", DANGER, "the queue is held")
    waiting, current = state.get("waiting") or [], state.get("round") or {}
    if waiting and not state.get("worker"): return sign("progress", DANGER, "no worker is running")
    gates = [(role, gate) for role, gate in sorted((current.get("gates") or {}).items()) if isinstance(gate, dict)]
    if not current.get("round"):
        if not waiting: return sign("progress", IDLE, "nothing to run")
        last = max((event.get("time", 0) for event in journal), default=0)
        if now - last < STARTING: return sign("progress", OK, "starting the next round")
        return sign("progress", WARNING, f"{entries(len(waiting))} wait, but no round has started for {span(now - last)}")
    found = []
    since = current.get("waitingSince")
    if current.get("waiting") and isinstance(since, (int, float)) and now - since >= WAIT_LONG:
        found.append((WARNING, f"the round has waited {span(now - since)} for {current['waiting']}"))
    if not gates and not found:
        began = current.get("started")
        if isinstance(began, (int, float)) and now - began >= WAIT_LONG:
            found.append((WARNING, f"the round began {span(now - began)} ago and no gate has started"))
        else: found.append((OK, "the round is starting"))
    for role, gate in gates:
        lane, mac = gate.get("lane") or "lane", gate.get("mac") or "a Mac not yet chosen"
        if gate.get("stuck"):
            found.append((DANGER, f"the {lane} gate is {gate['stuck']}"))
            continue
        since = gate.get("waitingSince")
        if gate.get("waiting") and isinstance(since, (int, float)):
            level = WARNING if now - since >= WAIT_LONG else OK
            found.append((level, f"the {lane} gate has waited {span(now - since)} for {gate['waiting']}",
                          f"Waiting {span(now - since)} for a Mac"))
            continue
        began = gate.get("began")
        if not isinstance(began, (int, float)):
            found.append((UNKNOWN, f"when the {lane} gate began was not recorded"))
            continue
        elapsed = now - began
        changed = modified(gate.get("log"))
        if changed is None:
            found.append((UNKNOWN, f"the {lane} gate's log cannot be read"))
            continue
        if now - changed >= SILENT:
            found.append((DANGER, f"the {lane} gate on {mac} has written nothing for {span(now - changed)}",
                          f"Silent for {span(now - changed)}"))
            continue
        if lane != "gate":
            found.append((OK, f"the {lane} lane on {mac} is {span(elapsed)} in"))
        elif usual is None:
            found.append((UNKNOWN, f"the gate on {mac} is {span(elapsed)} in, and there are fewer than {MEDIAN_RUNS} "
                                   "passing gate runs to say how long it usually takes"))
        elif elapsed > SLOW * usual:
            found.append((WARNING, f"the gate on {mac} is {span(elapsed)} in, {elapsed / usual:.1f}× its usual "
                                   f"{span(usual)}", f"{span(elapsed)} in, usually {span(usual)}"))
        else: found.append((OK, f"the gate on {mac} is {span(elapsed)} in, usually {span(usual)}",
                            f"{span(elapsed)} in, usually {span(usual)}"))
    level = worst(found)[0]
    chosen = [item for item in found if item[0] == level]
    return sign("progress", level, "; ".join(item[1] for item in chosen), "; ".join(item[2] for item in chosen if len(item) > 2) or None)


def pace(state, journal, usual, now):
    """Whether entries are landing: how long since the last landed, or since the oldest waiting entry was queued when
    that is later, against the usual gate time."""
    waiting = state.get("waiting") or []
    landed = [event["time"] for event in journal if event.get("kind") == "entry.landed"
              and isinstance(event.get("time"), (int, float))]
    start = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
    if not waiting: return sign("pace", IDLE, f"{sum(1 for at in landed if at >= start)} landed today")
    queued = [entry.get("queued") or entry.get("submitted") for entry in waiting]
    queued = [at for at in queued if isinstance(at, (int, float))]
    since = max([max(landed, default=0), min(queued, default=0)])
    if not since: return sign("pace", UNKNOWN, "when the waiting entries were queued was not recorded")
    if usual is None:
        return sign("pace", UNKNOWN, f"there are fewer than {MEDIAN_RUNS} passing gate runs to judge the pace by")
    gap, said = now - since, "the last landed" if since in landed else "the oldest waiting entry was queued"
    if gap > 3 * usual + STARTING:
        return sign("pace", WARNING, f"nothing has landed for {span(gap)}, while a gate usually takes {span(usual)}",
                    f"None landed for {span(gap)}")
    return sign("pace", OK, f"{said} {span(gap)} ago", f"Last landed {span(gap)} ago" if since in landed else None)


def macs(readings, findings, now):
    """Whether each Mac can be read now and has the memory to work: readings is [{"name", "reading", "readAt"}], each
    reading offload_host.pressure's answer, or None when no Mac could be asked."""
    if not readings: return sign("macs", UNKNOWN, "no Mac's reading was made")
    found = []
    for mac in readings:
        name, reading, at = mac["name"], mac.get("reading"), mac.get("readAt")
        if not isinstance(reading, dict) or reading.get("error"):
            why = reading.get("error") if isinstance(reading, dict) else mac.get("error") or "it did not answer"
            found.append((UNKNOWN, f"{name} could not be read: {why}"))
            continue
        if not isinstance(at, (int, float)) or now - at > STALE:
            found.append((UNKNOWN, f"{name} was last read {span(now - at) + ' ago' if isinstance(at, (int, float)) else 'never'}"))
            continue
        memory = reading.get("memory")
        memory = MEMORY.get(memory, memory)
        short = [finding for finding in findings or [] if finding.get("mac") == name and finding.get("signal") == "short of memory"]
        if memory == "critical": found.append((DANGER, f"{name}'s memory pressure is critical"))
        elif short: found.append((WARNING, short[0].get("diagnosis") or f"{name} is short of memory"))
        elif memory == "warning": found.append((WARNING, f"{name}'s memory pressure is at warning"))
        elif memory != "normal": found.append((UNKNOWN, f"{name}'s memory pressure could not be read"))
        else:
            load = reading.get("load")
            found.append((OK, f"{name} is up" + (f", load {load:.1f}" if isinstance(load, (int, float)) else "")))
    for finding in findings or []:
        if finding.get("signal") == "held back on a Mac with spare CPU":
            found.append((WARNING, f"a gate is held back on {finding.get('mac')}, which has spare CPU"))
    level = worst(found)[0]
    texts = [text for found_level, text in found if found_level == level]
    if level == OK: texts = [f"{len(readings)} {'Mac' if len(readings) == 1 else 'Macs'} up, memory normal"]
    short = None if level == OK or len(texts) == 1 else f"{len(texts)} Macs: see Farm"
    return sign("macs", level, "; ".join(texts), short)


def devices(record, state):
    """Whether the physical devices the round may need can be used: a device a running gate waits for that is not
    connected stops it; one that needs recovery is a warning. With no device recorded the sign is idle."""
    if record is None: return sign("devices", UNKNOWN, "the devices were not read")
    if record.get("error"): return sign("devices", UNKNOWN, f"the devices could not be read: {record['error']}")
    if record.get("deferredTo"): return sign("devices", IDLE, f"managed on {record['deferredTo']}")
    found = record.get("devices") or []
    if not found: return sign("devices", IDLE, "none recorded")
    waits = " ".join(str(gate.get("waiting") or "") for gate in ((state.get("round") or {}).get("gates") or {}).values()
                     if isinstance(gate, dict))
    needed = [device for device in found if device.get("name") and device["name"] in waits]
    if record.get("connectionError"):
        level = UNKNOWN if needed else IDLE
        return sign("devices", level, f"their connections could not be checked: {record['connectionError']}")
    missing = [device for device in needed if device.get("connection") != "connected"]
    if missing:
        return sign("devices", DANGER, "; ".join(f"a gate waits for {device['name']}, which is "
                                                 f"{'not detected' if device.get('connection') == 'notDetected' else device.get('connection') or 'not connected'}"
                                                 for device in missing))
    recovering = [device["name"] for device in found if device.get("state") == "needs recovery"]
    if recovering: return sign("devices", WARNING, f"{', '.join(recovering)} needs recovery: `qk devices recover`")
    connected = sum(1 for device in found if device.get("connection") == "connected")
    return sign("devices", OK, f"{connected} of {len(found)} connected")


def came_back(journal):
    """How many of the last CAME_BACK settled entries came back, failed or with a conflict."""
    settled = [event["kind"] for event in journal if event.get("kind") in ("entry.landed", "entry.failed", "entry.conflict")][-CAME_BACK:]
    if not settled: return sign("cameBack", IDLE, "nothing settled yet")
    back = sum(1 for kind in settled if kind != "entry.landed")
    text = f"{back} of the last {len(settled)}"
    if back >= 3: return sign("cameBack", WARNING, f"{text} came back: main or a test may be broken")
    return sign("cameBack", OK, text)


def forecast(state, usual, now, progressing):
    """When the entries waiting should have landed: what is left of the running gate's usual time, then one more round
    for those not in it, which the worker tests together. None when the usual time is unknown or the round is not
    moving. It assumes nothing comes back."""
    waiting = state.get("waiting") or []
    if not waiting or usual is None or progressing not in (OK, WARNING): return None
    current = state.get("round") or {}
    inside = set(current.get("entries") or [])
    pending = [entry for entry in waiting if entry.get("id") not in inside]
    began = [gate.get("began") for gate in (current.get("gates") or {}).values()
             if isinstance(gate, dict) and gate.get("lane") == "gate" and isinstance(gate.get("began"), (int, float))]
    left = max(usual - (now - min(began)), usual / 4) if began else (usual if inside else 0)
    clears = now + left + (usual if pending else 0)
    return {"waiting": len(waiting), "clearsAt": round(clears)}


def assess(state, journal, now=None, readings=None, record=None, findings=None, modified=None):
    """The verdict on the queue from what was read: state is merge_queue.snapshot's, journal the events oldest first,
    readings each Mac's (macs), record the devices' (devices), findings doctor's (merge_queue.diagnose), modified a
    function from a log's path to when it last changed, or None when it cannot be read. Returns {verdict, level,
    label, sentence, signs, forecast, findings, exitStatus}."""
    now = time.time() if now is None else now
    modified = modified or changed_at
    usual = usual_gate(journal)
    found = [progress(state, journal, usual, now, modified), pace(state, journal, usual, now),
             macs(readings, findings, now), devices(record, state), came_back(journal)]
    findings = [finding for finding in findings or [] if finding.get("signal") not in
                ("held", "no worker", "short of memory", "held back on a Mac with spare CPU")]
    by = lambda level: [item for item in found if item["level"] == level]
    waiting, held = state.get("waiting") or [], state.get("held")
    expected = forecast(state, usual, now, found[0]["level"])
    unknown = "; ".join(item["text"] for item in by(UNKNOWN))
    also = f" What could not be checked: {unknown}." if unknown else ""
    if held:
        owner = str(held).startswith("for the owner")
        verdict, level, label = "held", DANGER, "Waiting on you" if owner else "Held"
        sentence = f"The queue is held {held}. Nothing lands until it is resumed."
    elif waiting and not state.get("worker"):
        verdict, level, label = "stopped", DANGER, "Stopped"
        sentence = f"{entries(len(waiting)).capitalize()} wait, but no worker is running. `qk queue resume` starts one."
    elif by(DANGER):
        verdict, level, label = "stuck", DANGER, "Stuck"
        sentence = upper("; ".join(item["text"] for item in by(DANGER))) + "." + also
    elif by(WARNING) or findings:
        verdict, level, label = "slow", WARNING, "Slower than usual" if by(WARNING) else "Needs a look"
        said = [item["text"] for item in by(WARNING)]
        if findings:
            signals = sorted({finding['signal'] for finding in findings})
            said.append(f"doctor has {len(findings)} finding{'' if len(findings) == 1 else 's'} ({', '.join(signals)})")
        sentence = upper("; ".join(said)) + "."
        if expected: sentence += f" The {entries(expected['waiting'])} waiting should land around {clock(expected['clearsAt'])}."
        sentence += also
    elif unknown:
        verdict, level, label = "unknown", UNKNOWN, "Can't tell"
        sentence = f"Health can't be determined: {unknown}. Nothing else shows a problem, so the queue may be fine."
    elif waiting or (state.get("round") or {}).get("round"):
        verdict, level, label = "good", OK, "All good"
        sentence = "Everything is moving normally."
        if expected: sentence += f" At this pace the {entries(expected['waiting'])} waiting land by about {clock(expected['clearsAt'])}."
    else:
        verdict, level, label = "quiet", IDLE, "All quiet"
        landed = [event["time"] for event in journal if event.get("kind") == "entry.landed"]
        sentence = "Nothing is waiting." + (f" The last entry landed {span(now - max(landed))} ago." if landed else "") \
            + " The Macs are ready."
    status = HEALTHY if verdict in ("good", "quiet") else UNKNOWN_STATUS if verdict == "unknown" else ATTENTION
    return {"verdict": verdict, "level": level, "label": label, "sentence": sentence, "signs": found,
            "forecast": expected, "usualGateSeconds": round(usual) if usual else None, "findings": findings,
            "exitStatus": status}


def changed_at(path):
    """When the file at path last changed, or None when it cannot be read."""
    try: return Path(path).stat().st_mtime if path else None
    except OSError: return None


def gather(root):
    """Read what assess judges, now: the queue, its journal, doctor's findings, each Mac's pressure (the second Mac's
    over SSH) and the devices."""
    from . import events, extensions, lane_wait, merge_queue, offload_host, remote
    state = merge_queue.snapshot(root)
    journal = events.read(Path(root) / ".git" / "queuekeeper-queue")
    try: findings = merge_queue.diagnose(root)[0]
    except Exception: findings = []  # noqa: BLE001 - doctor's own checks never fail the verdict
    readings = [{"name": lane_wait.HERE, "reading": offload_host.pressure(), "readAt": time.time()}]
    host = remote.configured()
    if host:
        try: answered = remote.reading_there(host)[0]
        except Exception as error:  # noqa: BLE001 - unknown, never well
            answered = {"error": str(getattr(error, "problem", None) or error)}
        readings.append({"name": lane_wait.second_mac(host), "reading": answered, "readAt": time.time()})
    try: record = extensions.DEVICES.observations()
    except Exception as error:  # noqa: BLE001
        record = {"error": str(error)}
    return {"state": state, "journal": journal, "readings": readings, "record": record, "findings": findings}


GLYPHS = {OK: "✓", WARNING: "!", DANGER: "✕", UNKNOWN: "?", IDLE: "–"}


def main(root, as_json=False):
    """`qk queue health`: print the verdict, or with as_json the whole of assess's answer; exit 0 when the queue is
    healthy or quiet, 1 when it needs attention and 3 when its health cannot be determined."""
    found = assess(**gather(root))
    if as_json:
        print(json.dumps(found, indent=2))
        return found["exitStatus"]
    print(f"{found['label']}. {found['sentence']}")
    for item in found["signs"]:
        print(f"  {GLYPHS[item['level']]} {item['title']:<10} {item['text']}")
    if found["findings"]: print("`qk queue doctor` says what each finding means and what heals it")
    return found["exitStatus"]
