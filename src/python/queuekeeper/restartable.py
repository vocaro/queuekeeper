"""Long-running processes started from a Queuekeeper checkout that restart themselves when it moves.

The queue's worker fast-forwards the shared checkout after each landing only while no process started from it still
runs, since a process that keeps running could mix the toolkit it loaded with the one that replaced it. A
process meant to stay up, such as the dashboard's server, would hold the checkout behind for as long as it runs
(vocaro/queuekeeper#159). Such a
process may instead register here and watch the checkout: once its HEAD moves, the process replaces itself, by
exec, with the same command from the new code. The follow then passes over each process registered here (passed),
as the owner allowed: "qk scripts and code should be free to restart as needed" (2026-11-17).

A process that loads nothing more from the checkout for a while may register for that while too, pinned to what it
loaded: the Apple plugin's build-tool wrapper does so while a build runs (queuekeeper_apple.stale_modules.pinned), in
the same form, since other projects' builds through the shared checkout's wrapper kept it behind on their own.

A registration is a file under the checkout's Git directory, FOLDER/PID.json, naming the process and when it
started, as ps says, so that a PID used again by another process is never passed over."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

FOLDER = "queuekeeper-restartable"
POLL = 5.0  # seconds between the watcher's readings of the checkout's HEAD
SETTLE = 1.0  # seconds a moved HEAD is left before the process replaces itself, so a checkout in motion settles
RESTARTED = "QUEUEKEEPER_RESTARTED"  # set in the environment of a process that replaced itself


def code_checkout():
    """The checkout this Queuekeeper runs from, <checkout>/src/python/queuekeeper, or None when that is no Git
    checkout, as an installed copy."""
    root = Path(__file__).resolve().parents[3]
    return root if (root / ".git").exists() else None


def git(checkout, *args, seconds=10):
    try:
        done = subprocess.run(["git", "-C", str(checkout), *args], capture_output=True, text=True, timeout=seconds,
                              env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
    except (OSError, subprocess.SubprocessError): return None
    return done.stdout.strip() if done.returncode == 0 else None


def folder(checkout):
    """Where checkout's registrations are kept: under its common Git directory, which a worktree shares."""
    common = git(checkout, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return Path(common) / FOLDER if common else None


def started(pid):
    """When the process pid started, as ps says, or None when there is no such process."""
    try:
        done = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError): return None
    return done.stdout.strip() or None


def register(checkout, what, pid=None):
    """Record the process pid (this one by default), described as what, as one that restarts itself when checkout
    moves; return the registration's path, or None when it could not be written."""
    pid = os.getpid() if pid is None else pid
    place, began = folder(checkout), started(pid)
    if place is None or began is None: return None
    try:
        place.mkdir(exist_ok=True)
        path = place / f"{pid}.json"
        path.write_text(json.dumps({"pid": pid, "started": began, "what": what, "head": head(checkout)}) + "\n")
        return path
    except OSError: return None


def unregister(checkout, pid=None):
    place = folder(checkout)
    if place is None: return
    try: (place / f"{os.getpid() if pid is None else pid}.json").unlink()
    except OSError: pass


def passed(checkout):
    """{pid: what} for each process registered for checkout that still runs, by its start time; a registration
    whose process has ended, or whose PID another process now has, is removed."""
    place, found = folder(checkout), {}
    if place is None or not place.is_dir(): return found
    for path in place.glob("*.json"):
        try: record = json.loads(path.read_text())
        except (OSError, ValueError): record = None
        pid = record.get("pid") if isinstance(record, dict) else None
        if isinstance(pid, int) and record.get("started") and started(pid) == record["started"]:
            found[pid] = str(record.get("what") or "a restartable process")
            continue
        try: path.unlink()
        except OSError: pass
    return found


def head(checkout):
    return git(checkout, "rev-parse", "HEAD")


def restart(say=print):
    """Replace this process with the same command, from wherever its code now is, with RESTARTED set."""
    say(f"queuekeeper: the toolkit moved; restarting from it (PID {os.getpid()})")
    sys.stdout.flush()
    sys.stderr.flush()
    os.execve(sys.executable, [sys.executable, *sys.orig_argv[1:]], {**os.environ, RESTARTED: "1"})


def watch(checkout, then, poll=POLL, settle=SETTLE, stop=None, *, initial=None):
    """Call then(old, new) once HEAD differs from its initial reading and stays put for settle seconds.

    A caller that read HEAD before registering can pass that reading as initial. Return the daemon thread.
    stop, an Event, ends it.
    """
    stop = stop or threading.Event()
    first = head(checkout) if initial is None else initial

    def run():
        while not stop.wait(poll):
            now = head(checkout)
            if not now or not first or now == first: continue
            time.sleep(settle)
            if head(checkout) == now: return then(first, now)

    thread = threading.Thread(target=run, name="restartable-watch", daemon=True)
    thread.start()
    return thread
