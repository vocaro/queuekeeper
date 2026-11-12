"""Delivery: the journal's events since the last delivery, each told through every channel that is on.

A delivery is a process of its own (`python3 -m queuekeeper.visibility.notify DIRECTORY`), which the process that
wrote an event starts apart from itself (spawn) and never waits for. It keeps where it stopped beside the journal:

    <common>/queuekeeper-queue/visibility-notify.json   the last event told of, by its ID and time
    <common>/queuekeeper-queue/visibility-notify.lock   one delivery at a time

so a delivery that dies loses at most its own notices, the next one goes on from the same place, and no event is
told twice however many deliveries start. What a channel refuses is written to notify.log, beside the owner's
settings, and holds up neither the other channels nor the next notice."""
import contextlib
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

from ... import events, visibility
from . import channels, rules

CURSOR, LOCK, LOG, LOG_LIMIT = "visibility-notify.json", "visibility-notify.lock", "notify.log", 1 << 20
# A first delivery tells of nothing older than this many seconds: the journal's history is not news.
FIRST = 120
# The most notices one delivery sends as they are; the rest go as one that counts them.
MOST = 5
MODULE = __package__


def settings():
    found = visibility.settings().get("notify")
    return found if isinstance(found, dict) else {}


def log_path():
    return visibility.settings_path().parent / LOG


def log(words):
    """Append a line to the feature's log. Nothing here raises."""
    with contextlib.suppress(Exception):
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            if path.stat().st_size > LOG_LIMIT: path.replace(path.with_name(LOG + ".1"))
        with path.open("a") as stream: stream.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {words}\n")


def repository_of(directory):
    """The name of the repository whose queue is kept in directory, <common>/queuekeeper-queue: the folder that
    holds its .git, or a bare repository's own name."""
    common = Path(directory).parent
    return common.parent.name if common.name == ".git" else common.name.removesuffix(".git")


def spawn(directory, event):
    """Start a delivery for the queue kept in directory, apart from this process, when event is one a rule that is
    on tells of (extensions.AFTER_EVENT). It returns at once."""
    if visibility.switched_off() or rules.rule_of(event, rules.chosen(settings())) is None: return
    start(str(directory))


def start(*arguments):
    """Start this feature's process (`python3 -m queuekeeper.visibility.notify ARGUMENTS`) apart from this one, which
    does not wait for it."""
    package_root = str(Path(__file__).resolve().parents[3])
    environment = {**os.environ, "PYTHONPATH": os.pathsep.join(
        [package_root, *filter(None, [os.environ.get("PYTHONPATH")])])}
    started = subprocess.Popen([sys.executable, "-m", MODULE, *arguments], stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=environment,
                               start_new_session=True, cwd="/")
    # It outlives this process, which must not wait for it; one that ends first is reaped here.
    threading.Thread(target=started.wait, daemon=True).start()


def pending(directory, cursor, now):
    """The journal's events after the one cursor names, or, with no cursor, those of the last FIRST seconds. A
    cursor whose event the journal no longer holds stands for its time."""
    found = events.read(directory)
    if cursor:
        for index, event in enumerate(found):
            if event.get("id") == cursor.get("id"): return found[index + 1:]
    since = cursor.get("time", 0) if cursor else now - FIRST
    return [event for event in found if isinstance(event.get("time"), (int, float)) and event["time"] > since]


def read_cursor(directory):
    try: found = json.loads((Path(directory) / CURSOR).read_text())
    except (OSError, ValueError): return None
    return found if isinstance(found, dict) and isinstance(found.get("time"), (int, float)) else None


def write_cursor(directory, event):
    path = Path(directory) / CURSOR
    staged = path.with_suffix(f".{os.getpid()}.tmp")
    staged.write_text(json.dumps({"id": event.get("id"), "time": event.get("time", 0)}) + "\n")
    os.replace(staged, path)


def gathered(notices):
    """notices as they are sent: the first MOST - 1 of more than MOST, then one that counts the rest."""
    if len(notices) <= MOST: return notices
    rest = notices[MOST - 1:]
    urgent = any(notice["level"] == rules.URGENT for notice in rest)
    return [*notices[:MOST - 1], {
        "rule": "several", "level": rules.URGENT if urgent else rules.NORMAL, "title": f"{len(rest)} more notices",
        "body": rules.cut("; ".join(f"{notice['title']}: {notice['body']}" for notice in rest)),
        "repository": rest[0]["repository"], "event": None}]


def send(notice, found=None):
    """Send notice through every channel that is on, and return (how many took it, [each refusal])."""
    found = settings() if found is None else found
    sent, refused = 0, []
    for name, module, config in channels.on(found):
        try:
            module.send(notice, config)
            sent += 1
        except Exception as error:  # noqa: BLE001
            refused.append(f"{name} did not send \"{notice['title']}\": {error}")
    return sent, refused


def deliver(directory, now=None):
    """Tell of each event written to the journal in directory since the last delivery, and return how many
    notices went to at least one channel. The cursor moves past every event read, told of or not, before anything
    is sent: an event is told at most once, and a channel that hangs cannot make the next delivery repeat it."""
    now = time.time() if now is None else now
    found = settings()
    with (Path(directory) / LOCK).open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        fresh = pending(directory, read_cursor(directory), now)
        if not fresh: return 0
        write_cursor(directory, fresh[-1])
        on, repository = rules.chosen(found), repository_of(directory)
        notices = gathered([notice for notice in (rules.notice(event, on, repository) for event in fresh) if notice])
        told = 0
        for notice in notices:
            sent, refused = send(notice, found)
            told += bool(sent)
            for words in refused: log(words)
        if notices and not channels.on(found): log(f"{len(notices)} notice(s) of {repository} went nowhere: no channel is on")
        return told


def main(argv):
    """The feature's process: a delivery for the queue kept in DIRECTORY, or with --check a look for a queue that
    is stuck in the repository at ROOT (check)."""
    checking = argv[:1] == ["--check"]
    if len(argv) != 1 + checking:
        print(f"usage: python3 -m {MODULE} DIRECTORY | --check ROOT", file=sys.stderr)
        return 2
    if visibility.switched_off() or "notify" not in visibility.enabled(): return 0
    try:
        if checking:
            from . import check
            check.check(argv[1])
        else: deliver(argv[0])
    except Exception as error:  # noqa: BLE001
        log(f"the {'check of' if checking else 'delivery for'} {argv[-1]} stopped: {error}")
        return 1
    return 0
