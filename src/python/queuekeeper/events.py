"""The merge queue's event journal: one JSON line for each change of the queue's state.

The queue writes it and reads nothing back. Everything that shows the queue to its owner, `queuekeeper queue
events` and the visibility features (doc/visibility.md), reads it, so that none of them is called by the worker
and none can slow or stop a round. The journal is beside the queue's other records:

    <common>/queuekeeper-queue/events.jsonl     the journal
    <common>/queuekeeper-queue/events.1.jsonl   the generation before it, once the journal reached LIMIT

Each line is an object with these keys, and its kind's own beside them (KINDS):

    v      the schema's version, VERSION
    id     unique in the journal, and ordered as written by one process
    time   seconds since the epoch
    kind   what happened, one of KINDS

The schema only grows: a later version adds kinds and keys, and never renames or removes one, so a reader
ignores a kind or key it does not know.
"""
import contextlib
import fcntl
import json
import os
from pathlib import Path
import time

VERSION = 1
FILE, PREVIOUS, LOCK, LIMIT = "events.jsonl", "events.1.jsonl", "events.lock", 4 << 20
# Each kind, and what its line says in words: the keys in braces are the kind's own, which a line may lack.
KINDS = {
    "entry.queued": "{entry} queued: {subject}",
    "entry.landed": "{entry} landed as {landedAs}",
    "entry.failed": "{entry} came back: {why}",
    "entry.conflict": "{entry} came back with a conflict: {why}",
    "entry.removed": "{entry} removed",
    "entries.testing": "testing {entries}",
    "entries.requeued": "back in the queue: {entries}",
    "round.began": "round {round} began: {entries} at {head} on {base}",
    "gate.began": "{lane} began at {head}",
    "gate.ended": "{lane} exited {status}{where}",
    "batch.passed": "passed at {head}: {entries}",
    "batch.pushed": "pushed {head}: {entries}",
    "test.flaky": "flaky: {test} at {commit}",
    "queue.held": "held {why}",
    "queue.resumed": "resumed",
}
SHORT = ("head", "base", "landedAs", "commit")  # commits, which a line in words shows by their first 12 characters


def path_of(directory):
    return Path(directory) / FILE


def emit(directory, kind, **fields):
    """Append an event of kind to the journal of the queue kept in directory, and return it, or None when it was
    not written. A key whose value is None is left out. Nothing here raises: the journal explains the queue, and
    never stops it."""
    try:
        event = {"v": VERSION, "id": f"{time.time_ns():x}-{os.getpid():x}", "time": round(time.time(), 3), "kind": kind,
                 **{key: value for key, value in fields.items() if value is not None}}
        line = (json.dumps(event, default=str) + "\n").encode()
        path = path_of(directory)
        with (path.parent / LOCK).open("w") as lock:
            # One writer at a time, which orders the lines as they happened and lets one of them start a generation.
            fcntl.flock(lock, fcntl.LOCK_EX)
            with contextlib.suppress(FileNotFoundError):
                size = path.stat().st_size
                if size and size + len(line) > LIMIT: path.replace(path.with_name(PREVIOUS))
            # O_NONBLOCK, so that a FIFO put there with no reader fails at once instead of holding the queue up.
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NONBLOCK, 0o644)
            try: os.write(fd, line)
            finally: os.close(fd)
        return event
    except Exception:  # noqa: BLE001
        return None


def parsed(line):
    """The event a journal line holds, or None for a line that is not one, such as one cut short."""
    try: event = json.loads(line)
    except ValueError: return None
    return event if isinstance(event, dict) and isinstance(event.get("kind"), str) else None


def read(directory, since=None, kinds=None):
    """The journal's events, oldest first, the generation before it included: those at or after since, in seconds
    since the epoch, and of kinds, when given. A kind ending in a dot names every kind that begins with it."""
    found = []
    for name in (PREVIOUS, FILE):
        try: text = (Path(directory) / name).read_text(errors="replace")
        except OSError: continue
        for line in text.splitlines():
            event = parsed(line)
            if event is not None and wanted(event, since, kinds): found.append(event)
    return found


def wanted(event, since=None, kinds=None):
    if since is not None and not (isinstance(event.get("time"), (int, float)) and event["time"] >= since): return False
    return not kinds or any(event["kind"] == kind or (kind.endswith(".") and event["kind"].startswith(kind))
                            for kind in kinds)


def follow(directory, kinds=None, poll=0.5, stop=lambda: False):
    """Yield each event written to the journal from now on, until stop() is true. A journal that starts a new
    generation is followed into it: whatever the old one held unread is yielded first."""
    path, stream, rest = path_of(directory), None, b""
    skip = path.exists()  # what the journal holds already is not new; one that starts later is, from its first line
    try:
        while not stop():
            if stream is None:
                try: stream = path.open("rb")
                except OSError:
                    time.sleep(poll)
                    continue
                if skip: stream.seek(0, os.SEEK_END)
                skip, rest = False, b""
            chunk = stream.read()
            if chunk:
                *lines, rest = (rest + chunk).split(b"\n")
                for line in lines:
                    event = parsed(line.decode(errors="replace"))
                    if event is not None and wanted(event, None, kinds): yield event
                continue
            try: moved = os.fstat(stream.fileno()).st_ino != path.stat().st_ino
            except OSError: moved = True
            if moved:
                stream.close()
                stream = None
                continue
            time.sleep(poll)
    finally:
        if stream is not None: stream.close()


def words(event):
    """One line for an event, as `queuekeeper queue events` prints it: its local time, then what happened."""
    shown = {}
    for key, value in event.items():
        if key in SHORT and isinstance(value, str): value = value[:12]
        elif isinstance(value, list): value = ", ".join(map(str, value)) or "nothing"
        shown[key] = value
    if event["kind"] == "gate.ended": shown["where"] = f" on {event['mac']}" if event.get("mac") else ""
    said = KINDS.get(event["kind"], event["kind"]).format_map(Missing(shown))
    moment = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(event.get("time") or 0))
    return f"{moment} {said}"


class Missing(dict):
    """An event's keys for its line in words, with a key it lacks shown as a question mark."""
    def __missing__(self, key):
        return "?"
