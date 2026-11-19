"""Each Mac's history of lanes' waits, and `qk admissions waits` over it (vocaro/queuekeeper#233, step 1).

lane_wait keeps only what a lane waits for now, and removes it when the run ends, so how long gates waited for an
unlocked screen could not be read back. As each wait of one kind ends, lane_wait appends one line here: cleared, turned
into a wait of another kind or for another Mac, or left as the run ended. The line gives the project and lane whose run
waited, the kind (admission, screen, foreground, space, or one of the offers: unreachable, alone, overflow), this Mac's
name, when the wait began and ended and how long it lasted, whether the lane is whole-host, the reason last given, and
how it ended. Only the Mac the wait is on writes it: a wait relayed from another Mac is that Mac's to write, in its
own history, which `qk admissions waits` asks it for over ssh as `qk admissions usage` asks for its admission log.

The history is LOG beside the admission log (resources.log_path), in the same directory and so in a test's own
HEAVY_SLOT_DIR namespace, with one previous generation, PREVIOUS: a line that would take it past LIMIT bytes first
makes it the previous generation, so the two stay within twice LIMIT. A wait still going on is in neither until it
ends; `qk queue status` shows it. Writing never fails or holds up a lane."""
import argparse
from collections import Counter
import contextlib
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import sys

LOG, PREVIOUS, LOCK, LIMIT = "waits.jsonl", "waits.1.jsonl", "waits.lock", 4 << 20
# The most characters of a reason a line keeps.
REASON_LIMIT = 300
# The period `qk admissions waits` reads by default: a working day and the night after it.
WINDOW = 24 * 3600
# How a report names each kind, in the order it shows them: an unlocked screen first, the wait #233 measures.
KINDS = {"screen": "an unlocked screen", "admission": "admission", "foreground": "another Mac's foreground",
         "space": "free disk space", "overflow": "whichever Mac admits it first",
         "unreachable": "another Mac that could not be reached", "alone": "this Mac alone, as another Mac could not take it"}
FIELDS = ("time", "event", "project", "lane", "kind", "mac", "start", "end", "seconds", "wholeHost", "reason", "ended")


def path():
    """This Mac's history, beside its admission log."""
    from queuekeeper import resources
    return resources.log_path().with_name(LOG)


_mac = None


def mac_name():
    """This Mac's name, as offload_host.mac_name gives it, read once a process."""
    global _mac
    if _mac is None:
        from queuekeeper import offload_host
        _mac = offload_host.mac_name()
    return _mac


def stamp(moment):
    return datetime.fromtimestamp(moment, timezone.utc).isoformat(timespec="seconds")


def line(*, project, lane, kind, start, end, whole_host, reason, ended, dispatched_from=None):
    """One finished wait's line, start and end being epoch times."""
    return {"time": stamp(end), "event": "wait", "project": project, "lane": lane, "kind": kind, "mac": mac_name(),
            "start": stamp(start), "end": stamp(end), "seconds": round(max(end - start, 0.0), 1),
            "wholeHost": whole_host, "reason": reason[:REASON_LIMIT] if isinstance(reason, str) else None,
            "ended": ended, **({"dispatchedFrom": dispatched_from} if dispatched_from else {})}


def append(entry):
    """Append entry to this Mac's history, under its lock, which lets one writer at a time make the history the
    previous generation. A failure is said on standard error and never raised."""
    try:
        log = path()
        log.parent.mkdir(parents=True, exist_ok=True)
        text = (json.dumps(entry) + "\n").encode()
        with (log.parent / LOCK).open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with contextlib.suppress(FileNotFoundError):
                size = log.stat().st_size
                if size and size + len(text) > LIMIT: log.replace(log.with_name(PREVIOUS))
            fd = os.open(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NONBLOCK, 0o644)
            try: os.write(fd, text)
            finally: os.close(fd)
    except Exception as error:  # noqa: BLE001 - a history that cannot be written never stops a lane
        with contextlib.suppress(Exception):
            print(f"queuekeeper: the wait history was not written: {error}", file=sys.stderr, flush=True)


def read_entry(text):
    """A history line as a dict with its times read as datetimes, or None when it is not one."""
    try:
        entry = json.loads(text)
        if not isinstance(entry, dict) or entry.get("event") != "wait": return None
        entry = {**entry, "time": datetime.fromisoformat(entry["time"]), "start": datetime.fromisoformat(entry["start"]),
                 "end": datetime.fromisoformat(entry["end"])}
    except (ValueError, KeyError, TypeError):
        return None
    if (any(entry[key].tzinfo is None for key in ("time", "start", "end")) or entry["end"] < entry["start"]
            or not all(isinstance(entry.get(key), str) and entry[key] for key in ("kind", "lane", "mac"))):
        return None
    return entry


def read(paths=None):
    """The entries of the histories named, or of this Mac's two generations, oldest first, and how many lines could
    not be read."""
    if paths is None:
        log = path()
        paths = [log.with_name(PREVIOUS), log]
        missing_ok = True
    else: missing_ok = False
    entries, unreadable = [], 0
    for name in paths:
        try: lines = Path(name).read_bytes().splitlines()
        except FileNotFoundError:
            if missing_ok: continue
            raise
        for text in lines:
            entry = read_entry(text)
            if entry is None: unreadable += 1
            else: entries.append(entry)
    return entries, unreadable


def label(entry):
    project = entry.get("project")
    named = f"{project} {entry['lane']}" if isinstance(project, str) and project else entry["lane"]
    return named + (" (whole-host)" if entry.get("wholeHost") is True else "")


def tally(entries, since, until, kinds=None):
    """{kind: {label: [count, seconds within the period, longest]}} for the waits of entries that overlap
    [since, until), the seconds of each clipped to it."""
    found = {}
    for entry in entries:
        if kinds and entry["kind"] not in kinds: continue
        low, high = max(entry["start"], since), min(entry["end"], until)
        if entry["end"] < since or entry["start"] >= until: continue
        seconds = (high - low).total_seconds()
        counted = found.setdefault(entry["kind"], {}).setdefault(label(entry), [0, 0.0, 0.0])
        counted[0] += 1
        counted[1] += seconds
        counted[2] = max(counted[2], seconds)
    return found


def mac_lines(name, entries, since, until, kinds=None):
    """What one Mac's history says of [since, until): per kind, screen first, the waits and their time, in all and
    by each project's lane, longest first."""
    from queuekeeper.resources import duration
    if isinstance(entries, str): return [f"  {name}: history unavailable: {entries}"]
    found = tally(entries, since, until, kinds)
    if not found: return [f"  {name}: no wait ended in the period"]
    out = [f"  {name}:"]
    order = [kind for kind in KINDS if kind in found] + sorted(kind for kind in found if kind not in KINDS)
    for kind in order:
        labels = found[kind]
        count, seconds = sum(each[0] for each in labels.values()), sum(each[1] for each in labels.values())
        longest = max(each[2] for each in labels.values())
        out.append(f"    {KINDS.get(kind, kind)} ({kind}): {count} wait{'' if count == 1 else 's'}, "
                   f"{duration(seconds)} in all, longest {duration(longest)}")
        for named, (number, total, most) in sorted(labels.items(), key=lambda item: (-item[1][1], item[0])):
            out.append(f"      {named}: {number}, {duration(total)}, longest {duration(most)}")
    return out


def report(sources, since, until, kinds=None, out=print):
    """Print the waits of sources, each (a Mac's name, its history's entries or why they are unavailable), and
    return 0."""
    shown = lambda moment: moment.astimezone().strftime("%Y-%m-%d %H:%M %Z")
    out(f"Lanes' waits from {shown(since)} to {shown(until)}, each Mac's own (count, time within the period, longest):")
    for name, entries in sources:
        for text in mac_lines(name, entries, since, until, kinds): out(text)
    out("  A wait still going on is not counted until it ends; qk queue status shows it. A wait counts once for "
        "each kind it went through.")
    return 0


def main(argv=None, out=print):
    """qk admissions waits: how long lanes waited, for what and on which Mac, from each Mac's wait history."""
    from queuekeeper import farm, offload_host, remote, resources
    parser = argparse.ArgumentParser(prog="qk admissions waits", description="Show how many times and how long each "
        "project's lanes waited in the period, by what they waited for (an unlocked screen first, then admission, "
        "another Mac's foreground, free disk space and the offers to another Mac) and on which Mac, from each Mac's "
        f"wait history ({LOG} beside its admission log).")
    parser.add_argument("--since", type=resources.local_time, metavar="WHEN", help="the period's start, an ISO 8601 "
                        "date or time, local unless it names a zone (default 24 hours before --until)")
    parser.add_argument("--until", type=resources.local_time, metavar="WHEN", help="the period's end (default now)")
    parser.add_argument("--kind", action="append", choices=list(KINDS), help="show only waits of this kind; "
                        "repeat for more (--kind screen shows the waits for an unlocked screen)")
    parser.add_argument("--there", type=Path, action="append", metavar="LOG", help="a copy of another Mac's wait "
                        f"history to read instead of asking it over ssh; name the previous generation ({PREVIOUS}) first")
    args = parser.parse_args(argv)
    until = args.until or datetime.now(timezone.utc)
    since = args.since or until - timedelta(seconds=WINDOW)
    if since >= until: parser.error("--since must come before --until")
    try: here = read()[0]
    except OSError as error: here = str(error)
    sources = [(f"this Mac ({offload_host.mac_name()})", here)]
    try: macs = farm.active()
    except ValueError: macs = []
    for index, mac in enumerate(macs):
        # A copy of a history is the first Mac's, as --there of qk admissions usage is.
        if args.there and index == 0:
            try: entries = read(args.there)[0]
            except OSError as error: entries = str(error)
        else:
            try: entries = remote.wait_log_there(mac["ssh"], since)
            except (remote.Unreachable, remote.Unavailable, remote.Misconfigured) as error: entries = str(error)
        sources.append((resources.farm_mac_name(mac, len(macs)), entries))
    if args.there and not macs:
        try: sources.append(("another Mac (a copy of its history)", read(args.there)[0]))
        except OSError as error: sources.append(("another Mac (a copy of its history)", str(error)))
    return report(sources, since, until, set(args.kind or []), out)
