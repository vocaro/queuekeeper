"""A waiting whole-host lane's place in a Mac's admission queue, held for the Mac that sent it.

A gate that waits takes a place in the queue of each Mac that can run it, so that each Mac's guaranteed slots drain
toward it, and runs on the first to admit it (the owner's decision, 2026-11-17, vocaro/queuekeeper#211). The sending
Mac runs this file there over ssh, by itself, as it runs offload_host.py (remote.Places): `python3 -c SOURCE
'{"identity": ..., "label": ..., "age": SECONDS, "from": NAME}'`, with its standard input the channel that keeps the
place. It is a file of its own so that offload_host.py, which goes with every dispatch, stays within what one ssh
packet carries, and it imports nothing of the toolkit, which that Mac may not have. What it shares with admission
it names as resources.py and offload_host.py name it: the slot directory, the queue file and its lock, and
HEAVY_PLACE_SECONDS."""
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import select
import signal
import subprocess
import sys
import threading
import time

# As resources.QUEUE, resources.PLACE_SECONDS and offload_host.SLOTS have them. Seconds between the holder's looks
# for a word from the Mac that sent it.
QUEUE, PLACE_SECONDS, SLOTS, PLACE_POLL = "exclusive-queue.json", 180, "queuekeeper-heavy-slots", 5


def output(command):
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def slot_directory():
    """Where admission's slot files are, as resources.slot_directory and offload_host.slot_directory find them."""
    if os.environ.get("HEAVY_SLOT_DIR"): return Path(os.environ["HEAVY_SLOT_DIR"])
    if sys.platform != "darwin": return Path("/tmp") / SLOTS
    base = output(["/usr/bin/getconf", "DARWIN_USER_TEMP_DIR"])
    if not base or not os.path.isabs(base):
        raise RuntimeError(f"Cannot establish the host resource directory: getconf DARWIN_USER_TEMP_DIR answered "
                           f"{str(base)[:80]!r}, not an absolute path")
    return Path(base) / SLOTS


def place_held(identity, record=None):
    """Put record, a waiting request's place, into this Mac's admission queue at its arrival, in place of any record
    of that identity, or with no record take that identity out: under the queue's coordination lock and written
    whole, as resources.Admission and resources.Place change it. A queue that cannot be read is begun again, as
    admission begins it: each live request records itself at its next attempt."""
    directory = slot_directory()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / QUEUE
    with open(directory / "coordination.lock", "a+") as bridge:
        fcntl.flock(bridge, fcntl.LOCK_EX)
        try: waiters = json.loads(path.read_text())["waiters"]
        except (OSError, ValueError, KeyError, TypeError): waiters = []
        waiters = [waiter for waiter in waiters if isinstance(waiter, dict) and waiter.get("identity") != identity] \
            if isinstance(waiters, list) else []
        if record is not None:
            dated = lambda waiter: waiter.get("queuedAt") if isinstance(waiter.get("queuedAt"), (int, float)) else float("-inf")
            # Before a later dated arrival, as admission inserts a request.
            at = next((index for index, waiter in enumerate(waiters) if dated(waiter) > record["queuedAt"]), len(waiters))
            waiters.insert(at, {**record, "seen": time.time()})
        if not waiters:
            path.unlink(missing_ok=True)
            return
        staged = path.with_name(f".queuekeeper-place-{os.getpid()}")
        try:
            staged.write_text(json.dumps({"vocaroEngineering": 1, "waiters": waiters}) + "\n")
            os.replace(staged, path)
        finally:
            staged.unlink(missing_ok=True)


def hold_place(request, control=0, say=print):
    """Hold a place in this Mac's admission queue for a whole-host lane that waits on the Mac that sent this, until
    that Mac lets it go: its exit status. A gate that waits takes a place in the queue of each Mac that can run it,
    so that each Mac's guaranteed slots drain toward it, and runs on the first to admit it (the owner's decision,
    2026-11-17, vocaro/queuekeeper#211). The place is a queued whole-host request as admission records one, with
    this process as its owner and the lane's arrival, carried as its age, as its own: admission here, of any
    toolkit, keeps the guaranteed slots from later arrivals for it while it has asked within HEAVY_PLACE_SECONDS,
    and drops it once this process has ended. It holds no checkout and runs nothing.

    The sending Mac writes a line on control, this process's standard input, while the lane still waits, and each
    renews the place. Its end, the line `leave`, a hangup or a stop takes the place out at once. A sender that
    falls silent, asleep or cut off with the connection left open, renews nothing, so the place holds nothing back
    HEAVY_PLACE_SECONDS after its last word, and after twice that this process takes it out and ends. The run
    itself, offered here once this Mac has room, names the place in QUEUEKEEPER_ADMISSION_PLACE, so that its own
    admissions are not held back by it (resources.Admission)."""
    identity = request.get("identity")
    if not (isinstance(identity, str) and re.fullmatch(r"[0-9a-f]{32}", identity)):
        say(json.dumps({"error": f"a place needs an identity of 32 hex digits, not {identity!r}"}), flush=True)
        return 2
    try: lapse = float(os.environ.get("HEAVY_PLACE_SECONDS", str(PLACE_SECONDS)))
    except ValueError: lapse = PLACE_SECONDS
    born = output(["ps", "-p", str(os.getpid()), "-o", "lstart="])
    if not born:
        say(json.dumps({"error": "the place holder's own process start cannot be read"}), flush=True)
        return 1
    age = request.get("age")
    arrived = time.time() - (max(0.0, float(age)) if isinstance(age, (int, float)) and not isinstance(age, bool) else 0.0)
    record = {"identity": identity, "pid": os.getpid(), "start": born, "label": str(request.get("label") or "a lane")[:160],
              "queuedAt": arrived, "wholeHost": True, "heldFor": str(request.get("from") or "another Mac")[:160]}
    stopped = threading.Event()
    for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(number, lambda *_: stopped.set())
    try:
        place_held(identity, record)
        say(json.dumps({"place": "held", "pid": os.getpid(), "queuedAt": arrived}), flush=True)
        heard = time.monotonic()
        while not stopped.is_set():
            try: ready, _, _ = select.select([control], [], [], min(PLACE_POLL, max(lapse, 0.1)))
            except (OSError, ValueError): break
            if ready:
                try: words = os.read(control, 4096)
                except OSError: break
                if not words or b"leave" in words: break
                # The lane still waits: the place goes on holding later work back, and is put back if it was dropped.
                place_held(identity, record)
                heard = time.monotonic()
            elif time.monotonic() - heard >= 2 * lapse:
                break
    finally:
        with contextlib.suppress(OSError): place_held(identity)
    return 0


if __name__ == "__main__":
    sys.exit(hold_place(json.loads(sys.argv[1])))
