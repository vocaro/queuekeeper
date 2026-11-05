"""Heavy-work admission interoperating with the existing PDF slot locks.

Admission records each job it admits in a guard, named by StudyWright's former host gate lock:
SW_GATE_LOCK_FILE, `/tmp/studywright-gate-suite.lock` by default, with `.warm-push-guard`
appended. StudyWright's gate checks an inherited admission against that path, and tests isolate
admission by redirecting it; admission takes no lock there (#25). Exclusive work holds every old
PDF slot. Crashed owners retain their guard for explicit inspection rather than treating a
deadline as release.

HEAVY_SLOTS jobs are always admitted while no exclusive request waits. More, up to
HEAVY_SLOTS_MAX, are admitted one at a time while the load average and memory pressure show
spare capacity: a fixed count left the host idle behind jobs waiting on simulators, while ten
unqueued builds starved watchdogd until the kernel panicked (2026-10-14). Admission cannot
preempt a job that becomes heavy later. A whole-host lane, a gate that runs a project's suites
side by side, takes every guaranteed slot, so that two such lanes never run on one Mac at once
(two EPUBLib gates at once put a 10-core Mac at a load of 719 on 2026-11-04, #168); a lighter
heavy lane still shares, beyond them, while the host shows spare capacity. Each limit can be set for a host in its limits file,
admission-limits.json beside the admission log, which every process there reads, as a number or
as a share of the cores; a limit's environment variable overrides it for one process. Without
the file the defaults stand (#49).

Exclusive requests queue in arrival order, and every other request waits behind a live one, so
an exclusive request waits only for the work running when it asked. While heavy jobs kept
taking freed slots, exclusive gates waited for hours (October 2026). A whole-host request joins
the same queue: exclusive and whole-host requests behind it wait their turn, and other heavy work
takes no guaranteed slot while it waits, only a slot beyond them, so that the guaranteed slots
drain toward it rather than a steady stream of heavy jobs keeping one busy for good (#174).

A generative-model run takes one heavy slot and the host's model lock, an flock on `model` in
the slot directory, both or neither: two model runs exclude each other while builds and tests
run beside them. One model workload per machine is a limit of the hardware: two processes
driving one Mac's on-device model both stalled. Exclusive work holds the model with the host.

Each admission appends a line to the host's admission log, and its end another, so that an audit
can count admissions, waits and holds instead of guessing (#46); `eng admissions` summarizes it.
A request that waited says why, which an audit could only infer from the holders logged beside it
(#49): how many of its attempts each kind of refusal refused, and the load and memory pressure read.
Its waiting message names the limit that refused it and the limit's value, beside what it read.

Beside its guard entry, each admission keeps a process record: what supervision in the owner's
process, the toolkit's or a caller's own, saw of each command, its processes with their starts and
the process groups and sessions they used, appended at each sample that sees something new. Once
the owner has ended, `eng admissions recover` runs the end-of-run census against that record and
removes the entry only when nothing of an unsettled command still runs (#61). An entry without a
record is left for a check by hand. Admission runs the same recovery itself on each dead or uncertain
entry it meets, so that an entry a task stop's SIGKILL stranded, whose commands have all ended, no
longer holds every session's heavy work until a person removes it. An entry recovery leaves counts as
the slots it held, every slot for exclusive work, so that only work beyond them waits (#89).
"""
import argparse
from collections import Counter
import contextlib
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import pwd
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid

TOKEN = "VOCARO_RESOURCE_TOKEN"
# `eng run --if-free` waits this many seconds for admission, then exits with BUSY (EX_TEMPFAIL)
# without running: how the second Mac says it cannot take a lane now.
FREE_WAIT, BUSY = 5, 75
# The admission log and its previous generation. A line that would take the log past LOG_LIMIT
# bytes first makes the log the previous generation, so the two together stay within twice it.
LOG, PREVIOUS_LOG, LOG_LIMIT = "admissions.jsonl", "admissions.1.jsonl", 8 << 20
# The characters of a label that logs, queues and messages keep; the guard records it whole.
LABEL_LIMIT = 160
# The path that names the guard's namespace, unless SW_GATE_LOCK_FILE names another.
GATE_LOCK = "/tmp/studywright-gate-suite.lock"
# The admissions this process holds, by identity: only the owner's own supervision writes an entry's
# process record. Nested work in another process reuses the admission and writes nothing to it.
_held = {}


def process_start(pid):
    """When pid started, as `ps -o lstart=` prints it, or None when no such process runs.

    A process in an app's sandbox, such as an XCTest runner checking the foreground reservation that
    eng holds on the second Mac, may not start ps; the kernel's record gives it the same text."""
    try:
        result = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="], capture_output=True, text=True)
    except OSError:
        if sys.platform != "darwin":
            raise
        return kernel_start(pid)
    return result.stdout.strip() if result.returncode == 0 else None


def kernel_start(pid):
    """process_start from sysctl's KERN_PROC_PID, with no process started: None when no such process
    runs, and OSError when the kernel refuses the question."""
    import ctypes
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    libc = ctypes.CDLL(None, use_errno=True)
    mib = (ctypes.c_int * 4)(1, 14, 1, pid)  # CTL_KERN, KERN_PROC, KERN_PROC_PID
    found = ctypes.create_string_buffer(1024)
    size = ctypes.c_size_t(len(found))
    if libc.sysctl(mib, 4, found, ctypes.byref(size), None, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, f"sysctl KERN_PROC_PID {pid}: {os.strerror(error)}")
    if size.value == 0:
        return None
    # A kinfo_proc begins with its extern_proc, whose first field is p_starttime (a timeval, seconds
    # first) and whose p_pid is at byte 40.
    if int.from_bytes(found.raw[40:44], sys.byteorder, signed=True) != pid:
        raise OSError(f"sysctl KERN_PROC_PID {pid} described another process")
    started = int.from_bytes(found.raw[0:8], sys.byteorder, signed=True)
    return time.strftime("%a %b %e %H:%M:%S %Y", time.localtime(started))


def running(record):
    """Whether the process a guard owner or queued request recorded still runs as itself."""
    if process_start(record["pid"]) != record.get("start"):
        return False
    try:
        os.kill(record["pid"], 0)
    except ProcessLookupError:
        return False
    return True


def guard_path(environment):
    """Admission's guard in this environment's namespace."""
    return Path(environment.get("SW_GATE_LOCK_FILE", GATE_LOCK) + ".warm-push-guard")


# The fields of a guard entry as admission writes it, each with its type. Every toolkit has written the first
# four; an entry may lack the others, which later toolkits added or which mark it. A field named in neither,
# such as a newer toolkit may add, is read past.
FIELDS, LATER = {"pid": int, "start": str, "label": str, "exclusive": bool}, {"model": bool, "admitted": str, "uncertain": bool,
                                                                                "wholeHost": bool}


def entries(state):
    """A guard's entries by identity, or None when the guard, parsed, is not in admission's format: the marker
    {"vocaroEngineering": 1}, and owners an object whose every entry has a positive PID, its start, its label
    and whether it is exclusive, and each field FIELDS and LATER name of its type. Admission refuses a guard
    not in that format and never overwrites it, as with one it cannot parse (#72)."""
    owners = state.get("owners") if isinstance(state, dict) and state.get("vocaroEngineering") == 1 else None
    if not isinstance(owners, dict):
        return None
    for entry in owners.values():
        if not (isinstance(entry, dict) and all(field in entry for field in FIELDS)
                and all(type(entry[field]) is kind for field, kind in {**FIELDS, **LATER}.items() if field in entry)
                and entry["pid"] > 0):
            return None
    return owners


def guard_owners(environment):
    """Each owner the guard in this environment records, as (PID, process start with its spacing
    collapsed), read as other readers read it, without the coordination lock. A missing or unreadable
    guard, or one without admission's marker and owners object, records none. An entry that names no
    PID and start records no owner, and those beside it still do, even where admission refuses the
    guard (#72): supervision that missed a live owner would signal its children, its samples among them."""
    try:
        state = json.loads(guard_path(environment).read_text())
    except (OSError, ValueError):
        return set()
    owners = state.get("owners") if isinstance(state, dict) and state.get("vocaroEngineering") == 1 else None
    if not isinstance(owners, dict):
        return set()
    return {(owner["pid"], " ".join(owner["start"].split())) for owner in owners.values()
            if isinstance(owner, dict) and isinstance(owner.get("pid"), int) and isinstance(owner.get("start"), str)}


def record_path(guard, identity):
    """An admission's process record: beside its guard, named by the identity that keys its entry."""
    return Path(f"{guard}.{identity}.jsonl")


def holding(environment):
    """The admission this process holds that the environment's token names, or None."""
    try:
        nonces = {item["nonce"] for item in json.loads(environment.get(TOKEN) or "{}").get("locks", [])}
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    return next((_held[nonce] for nonce in nonces if nonce in _held), None)


def slot_directory():
    if os.environ.get("HEAVY_SLOT_DIR"):
        return Path(os.environ["HEAVY_SLOT_DIR"])
    # Preserve the legacy macOS user-temp namespace even when a command uses a private TMPDIR.
    # A process-local temporary directory must not create another host-wide build budget.
    if sys.platform == "darwin":
        base = subprocess.check_output(["/usr/bin/getconf", "DARWIN_USER_TEMP_DIR"], text=True, timeout=5).strip()
        if not base:
            raise RuntimeError("Cannot establish the host resource directory")
        return Path(base) / "pdfreflow-heavy-slots"
    return Path("/tmp/pdfreflow-heavy-slots")


def log_path():
    """The host's admission log. An isolated HEAVY_SLOT_DIR namespace keeps its own beside its slots.
    The default namespace's is in the account's durable state, which a reboot and the temporary-file
    cleaner leave alone, unlike the slot directory; a HOME private to one session must not start another."""
    if os.environ.get("HEAVY_SLOT_DIR"):
        return Path(os.environ["HEAVY_SLOT_DIR"]) / LOG
    state = "Library/Application Support" if sys.platform == "darwin" else ".local/state"
    return Path(pwd.getpwuid(os.getuid()).pw_dir) / state / "vocaro-engineering" / LOG


def append_log(entry):
    """Append an entry to the admission log. The caller holds the coordination lock, which orders the
    lines as admission happened and lets one writer at a time make the log the previous generation.

    A failure is reported and never fails or holds up admission or release: O_NONBLOCK makes opening
    a FIFO that no process reads fail at once rather than wait for a reader.
    """
    try:
        path = log_path()
        line = (json.dumps({"time": datetime.now(timezone.utc).isoformat(timespec="seconds"), **entry}) + "\n").encode()
        path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            size = path.stat().st_size
            if size and size + len(line) > LOG_LIMIT:
                path.replace(path.with_name(PREVIOUS_LOG))
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NONBLOCK, 0o644)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    except Exception as error:
        with contextlib.suppress(Exception):
            print(f"heavy: the admission log was not written: {error}", file=sys.stderr, flush=True)


def ending(kind):
    """How an admission ended, from the type of the exception that ended it, if any. Every end but an
    uncertain one releases the owner's guard entry."""
    from vocaro_engineering.processes import Interrupted, NotStarted, TimedOut
    if kind is None:
        return "released"
    for failure, outcome in [(NotStarted, "not-started"), (Interrupted, "stopped"), (TimedOut, "timed-out")]:
        if issubclass(kind, failure):
            return outcome
    return "uncertain"


def take_slot(directory, slots, start=0, skip=()):
    """Compatibility API: one old-style slot descriptor, or None. Admission skips the slots it counts for a
    stranded guard entry, whose lock its ended owner no longer holds."""
    for slot in range(start, slots):
        if slot in skip: continue
        fd = os.open(Path(directory) / f"slot{slot}", os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            continue
        return slot, fd
    return None


def last_admission(directory, slots):
    """When a holder last wrote its identity into a slot. Probing leaves new slot files empty."""
    latest = 0
    for slot in range(slots):
        try:
            status = (Path(directory) / f"slot{slot}").stat()
        except FileNotFoundError:
            continue
        if status.st_size:
            latest = max(latest, status.st_mtime)
    return latest


# The memory pressure levels macOS reports, by name. Only normal pressure lets an adaptive slot open.
PRESSURE = {1: "normal", 2: "warning", 4: "critical"}


def memory_level():
    """The memory pressure level macOS reports: 1 normal, 2 warning, 4 critical, or None when it cannot be read."""
    if sys.platform != "darwin":
        return None
    result = subprocess.run(["/usr/sbin/sysctl", "-n", "kern.memorystatus_vm_pressure_level"],
        capture_output=True, text=True)
    return int(result.stdout) if result.returncode == 0 and result.stdout.strip().isdigit() else None


def one_minute_load():
    """The one-minute load average as the log records it, or None when it is unavailable."""
    try:
        return round(os.getloadavg()[0], 2)
    except OSError:
        return None


# The host's admission limits, beside its admission log: the limits its owner chose for this host (#49).
LIMITS = "admission-limits.json"
# Each limit by its key in the limits file: the environment variable that overrides it, whether it counts
# slots, and its default and how the default is reached, given the cores and the guaranteed slots in force.
LIMIT_KEYS = {
    "slots": ("HEAVY_SLOTS", True, lambda cores, slots: (3, "3")),
    "slotsMax": ("HEAVY_SLOTS_MAX", True, lambda cores, slots: (max(slots, cores // 2), "half the cores, at least HEAVY_SLOTS")),
    "loadLimit": ("HEAVY_LOAD_LIMIT", False, lambda cores, slots: (0.75 * cores, "three quarters of the cores")),
    "settleSeconds": ("HEAVY_SETTLE_SECONDS", False, lambda cores, slots: (30.0, "30")),
}


def limits_path():
    """The host's limits file. An isolated HEAVY_SLOT_DIR namespace keeps its own beside its slots, as it keeps its
    log, so that a test never reads the host's."""
    return log_path().with_name(LIMITS)


def host_limits(path=None, cores=None):
    """The limits the host's file sets, by key, each resolved against the cores: a number, or {"perCore": F}, F
    times the cores, which for a slot count is rounded down and at least 1. {} when there is no file. Anything else
    in it, an unknown key among them, raises ValueError naming the file: a host whose limits cannot be read is a
    configuration problem for its owner, never a reason to guess them."""
    path = limits_path() if path is None else Path(path)
    cores = (os.cpu_count() or 1) if cores is None else cores
    try:
        text = path.read_text()
    except FileNotFoundError:
        return {}
    except OSError as error:
        raise ValueError(f"Invalid resource limits: {path} cannot be read: {error}") from None
    try:
        data = json.loads(text)
    except ValueError as error:
        raise ValueError(f"Invalid resource limits: {path} is not JSON: {error}") from None
    if not isinstance(data, dict):
        raise ValueError(f"Invalid resource limits: {path} holds no JSON object")
    unknown = sorted(set(data) - set(LIMIT_KEYS))
    if unknown:
        raise ValueError(f"Invalid resource limits: {path} names {', '.join(unknown)}, not one of "
                         f"{', '.join(LIMIT_KEYS)}")

    def number(value):
        return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0

    resolved = {}
    for key, value in data.items():
        counts = LIMIT_KEYS[key][1]
        if isinstance(value, dict) and set(value) == {"perCore"} and number(value["perCore"]) and value["perCore"] > 0:
            share = value["perCore"] * cores
            resolved[key] = max(1, int(share)) if counts else float(share)
        elif number(value) and (not counts or float(value).is_integer()):
            resolved[key] = int(value) if counts else float(value)
        else:
            raise ValueError(f"Invalid resource limits: {path} sets {key} to {json.dumps(value)}, not a "
                             f"{'whole ' if counts else ''}number at or above 0 or {{\"perCore\": F}} with F above 0")
    return resolved


def limits():
    """The admission limits in force, by key: each its value and where it came from. A limit's environment variable,
    set for one process, overrides the host's limits file, which every process on the host reads; without either,
    the default stands, which with no file keeps admission as it was before the file existed (#49)."""
    cores = os.cpu_count() or 1
    path = limits_path()
    configured = host_limits(path, cores)
    found, slots = {}, None
    for key, (variable, counts, default) in LIMIT_KEYS.items():
        if variable in os.environ:
            try:
                value = int(os.environ[variable]) if counts else float(os.environ[variable])
            except ValueError:
                raise ValueError(f"Invalid resource limits: {variable}={os.environ[variable]} is not a "
                                 f"{'whole ' if counts else ''}number") from None
            found[key] = value, variable
        elif key in configured:
            found[key] = configured[key], str(path)
        else:
            value, how = default(cores, slots)
            found[key] = value, f"default: {how}"
        if key == "slots":
            slots = found[key][0]
    (slots, slots_from), (maximum, maximum_from) = found["slots"], found["slotsMax"]
    if slots < 1 or maximum < slots or found["settleSeconds"][0] < 0:
        raise ValueError(f"Invalid resource limits: HEAVY_SLOTS={slots} ({slots_from}) must be at least 1, "
                         f"HEAVY_SLOTS_MAX={maximum} ({maximum_from}) at least HEAVY_SLOTS, and HEAVY_SETTLE_SECONDS "
                         f"at least 0")
    return found


def load_limit():
    """The one-minute load average at or above which the host has no spare capacity: HEAVY_LOAD_LIMIT, or the host's
    limits file, or three quarters of the cores."""
    return float(limits()["loadLimit"][0])


def limits_main(argv=None, out=print):
    """eng admissions limits: the admission limits in force for a process started like this one, and their sources."""
    parser = argparse.ArgumentParser(prog="eng admissions limits", description="Print the heavy-work admission "
        "limits in force here and where each comes from: its environment variable, the host's limits file or the "
        "default (guides/shared-host.md, Heavy-work admission).")
    parser.parse_args(argv)
    try:
        found = limits()
    except ValueError as error:
        print(f"eng: {error}", file=sys.stderr)
        return 2
    out(f"{os.cpu_count() or 1} cores; limits file {limits_path()}")
    for key, (value, source) in found.items():
        out(f"{LIMIT_KEYS[key][0]}={value:g}  ({key}; {source})")
    return 0


def pressure(limit=None):
    """Why the host has no spare capacity now, by the load average and memory pressure, as the refusal's kind,
    load or memory, and its reason, which names the limit and its value beside what was read; None when it has.
    Admission takes a slot beyond the guaranteed ones only without it, and the merge queue waits for it to clear
    before it runs a gate again that gave no verdict (guides/maintenance.md, The merge queue)."""
    limit = load_limit() if limit is None else limit
    try:
        load = os.getloadavg()[0]
    except OSError:
        load = None
    return judged(load, limit, memory_level)


def judged(load, limit, level):
    """pressure's answer for a reading of a Mac: its one-minute load, None when unavailable, the limit of that
    Mac, and level, a function that reads its memory pressure level, called only when the load is below the
    limit. The merge queue judges the second Mac's reading by it too (remote.pressure_there)."""
    if load is None:
        return "load", f"the load average, which HEAVY_LOAD_LIMIT={limit:g} bounds, is unavailable"
    if load >= limit:
        return "load", f"load {load:.1f} is at or above HEAVY_LOAD_LIMIT={limit:g}"
    level = level()
    if level is None:
        return "memory", "memory pressure, which must be level 1 (normal), cannot be read"
    if level != 1:
        named = f" ({PRESSURE[level]})" if level in PRESSURE else ""
        return "memory", f"memory pressure is level {level}{named}, not 1 (normal)"
    return None


def inherited(exclusive=False, model=False):
    raw = os.environ.get(TOKEN)
    if not raw:
        return False
    try:
        token = json.loads(raw)
        if token.get("schemaVersion") != 1 or not token.get("locks"):
            raise ValueError("Invalid admission token")
        for item in token["locks"]:
            held, path = os.fstat(item["fd"]), os.stat(item["path"])
            if (held.st_dev, held.st_ino) != (path.st_dev, path.st_ino):
                raise ValueError("Admission descriptor does not match its lock")
            if item["nonce"] not in os.pread(item["fd"], 4096, 0).decode():
                raise ValueError("Admission owner changed")
        nonces = {item["nonce"] for item in token["locks"]}
        if len(nonces) != 1:
            raise ValueError("Admission descriptors have different owners")
        guard = Path(token.get("guard", guard_path(os.environ)))
        owners = entries(json.loads(guard.read_text()))
        if owners is None:
            raise ValueError(f"the guard {guard} is unreadable or not in admission's format")
        owner = owners.get(next(iter(nonces)))
        if not owner or owner.get("uncertain"):
            raise ValueError("Admission owner has no active guard")
        if process_start(owner["pid"]) != owner.get("start"):
            raise ValueError("Admission owner identity changed")
        if any(bool(token.get(kind)) != bool(owner.get(kind)) for kind in ["exclusive", "model"]):
            raise ValueError("Admission capability differs from its owner")
        if exclusive and not owner.get("exclusive"):
            raise ValueError("Cannot acquire exclusive admission inside shared admission")
        # An exclusive owner holds the host, and with it the model.
        if model and not (owner.get("model") or owner.get("exclusive")):
            raise ValueError("Cannot acquire the on-device model inside admission that does not hold it")
        return True
    except (KeyError, TypeError, OSError, ValueError) as error:
        raise RuntimeError(f"Invalid inherited resource admission: {error}") from error


def publish_guard(path, state):
    """Readers authenticate without the coordination lock, so publication is atomic."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix=".vocaro-guard-", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(state, stream)
            stream.write("\n")
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


# Seconds a queued whole-host request holds later heavy work back after its process last asked: longer than an
# offer of its lane to the second Mac takes, between two of this Mac's waits (Place).
PLACE_SECONDS = 180
# The queue of exclusive and whole-host requests, in the slot directory, changed only under its coordination lock.
QUEUE = "exclusive-queue.json"


def queued_requests(queue):
    """The live queued requests, first come first. The caller holds the coordination lock.

    A queued request holds no slot and has started nothing, so the record of one whose
    process ended or whose identity changed is dropped, where a guard owner's is kept.
    """
    try:
        recorded = json.loads(Path(queue).read_text())["waiters"]
        live = [waiter for waiter in recorded if running(waiter)]
    except FileNotFoundError:
        return []
    except (OSError, ValueError, KeyError, TypeError):
        # Only admission writes the queue, atomically. Losing an unreadable queue's order
        # stops nobody: each live request records itself again at its next attempt.
        recorded, live = None, []
    if live != recorded:
        record_queue(queue, live)
    return live


def record_queue(queue, waiters):
    if waiters: publish_guard(Path(queue), {"vocaroEngineering": 1, "waiters": waiters})
    else: Path(queue).unlink(missing_ok=True)


class Place:
    """A whole-host request's place in the queue, kept across the successive admissions one process asks for, so
    that work which alternates between this Mac's queue and offers to the second Mac (remote.overflow) keeps its
    place in arrival order instead of rejoining behind the requests that came meanwhile (vocaro-engineering#174).

    An admission given a place leaves it there when its wait times out; its caller calls leave() once it no longer
    waits for this Mac, admitted or not, and admission removes it when admitted. While the process is away, between
    waits, its place holds nothing back once HEAVY_PLACE_SECONDS have passed since it last asked, and a place whose
    process has ended is dropped, as every queued request is."""

    def __init__(self):
        self.identity = uuid.uuid4().hex

    def leave(self):
        directory = slot_directory()
        if not (directory / QUEUE).exists():
            return
        with (directory / "coordination.lock").open("a+") as bridge:
            fcntl.flock(bridge, fcntl.LOCK_EX)
            waiters = queued_requests(directory / QUEUE)
            if any(waiter["identity"] == self.identity for waiter in waiters):
                record_queue(directory / QUEUE, [waiter for waiter in waiters if waiter["identity"] != self.identity])


class Admission:
    def __init__(self, label, exclusive=False, timeout=5400, model=False, whole_host=False, place=None):
        self.label, self.exclusive, self.timeout = label, exclusive, timeout
        # Exclusive work holds the host, the model with it.
        self.model = model or exclusive
        # A whole-host lane, a gate that runs a project's suites side by side, takes every guaranteed slot, so
        # that a second such lane never runs beside it on this Mac; a lighter heavy lane still shares, beyond
        # them. Exclusive work holds every slot already (vocaro-engineering#168).
        self.whole_host = bool(whole_host) and not exclusive
        self.kind = "exclusive" if exclusive else "model" if model else "heavy"
        # When the request began to wait, and when it was admitted, by the monotonic clock.
        self.started, self.admitted = time.monotonic(), None
        self.directory = slot_directory()
        # Each from its environment variable, the host's limits file or the default (#49).
        found = limits()
        self.slots, self.maximum = found["slots"][0], found["slotsMax"][0]
        self.load_limit = float(found["loadLimit"][0])
        # The one-minute load average shows a new job's load only gradually.
        self.settle = float(found["settleSeconds"][0])
        self.poll = float(os.environ.get("HEAVY_POLL_SECONDS", "1"))
        # How long a dead or uncertain entry that recovery left waits before it is checked again.
        self.recheck = float(os.environ.get("HEAVY_RECOVER_SECONDS", "60"))
        # How long a queued whole-host request holds later heavy work back after it last asked (_holds).
        self.hold = float(os.environ.get("HEAVY_PLACE_SECONDS", str(PLACE_SECONDS)))
        if self.slots < 1 or self.maximum < self.slots or self.settle < 0 or self.poll <= 0 or timeout < 0 \
                or self.recheck < 0 or self.hold < 0:
            raise ValueError("Invalid resource limits")
        if place is not None and not self.whole_host:
            raise ValueError("Only a whole-host request keeps a place in the queue")
        # Why the latest attempt was refused: its kind, which the log counts (#49), and the waiting message,
        # which names the limit that refused it and its value beside what the attempt read.
        # guard: the guard is unreadable or not in admission's format and needs inspection; stranded: a dead or
        # uncertain entry recovery left is counted among the busy slots, or as the model's holder (#89);
        # exclusive: exclusive work holds the host or is queued ahead; wholeHost: a whole-host request queued ahead
        # keeps the guaranteed slots, one of them free; model: another run holds the model; slots: every slot the request may take is busy; settle, load or memory: the settling time, the
        # load average or memory pressure withholds a slot beyond the guaranteed ones. An older toolkit's log
        # lines also count legacy, a gate holding StudyWright's former host lock, which no longer refuses (#25).
        self.refusal = self.waiting = None
        # The dead or uncertain entries recovery left, by identity: when they were checked, and why they stayed.
        self.unrecovered = {}
        # The latest attempt's stranded entries, those recovery left: by identity, the entry and why it stayed;
        # then, as counted, each one's identity, entry, why, the slots it is counted as and whether the model.
        self.stranded, self.counted = {}, []
        # For the log line: the refused attempts by kind, in the order the kinds first refused it; the holders
        # when it asked; the first, last and highest one-minute load its attempts read; and the highest memory
        # pressure level read beside its refusals.
        self.refusals, self.holders, self.loads, self.memory = Counter(), None, None, None
        self.guard = guard_path(os.environ)
        # Exclusive and whole-host requests waiting for admission, first come first.
        self.queue = self.directory / QUEUE
        self.identity = uuid.uuid4().hex
        # The identity of its queued record: its place's, kept across this process's successive admissions, or its own.
        self.place = place
        self.queued_as = place.identity if place is not None else self.identity
        # What supervision in this process sees of each command, for the entry's recovery, and
        # whether a line failed to reach it, which ends recording. Lane groups supervise in threads.
        self.process_record = record_path(self.guard, self.identity)
        self.unrecorded, self.recording = False, threading.Lock()
        self.descriptors = []
        # The model lock's descriptor while admission holds it.
        self.held_model = None
        self.previous = None
        self.reused = False
        self.queued = False

    def _recorded(self):
        """The published guard, whether or not its owners run, or None if it is unreadable or not in admission's
        format (entries)."""
        try:
            value = json.loads(self.guard.read_text())
        except (OSError, ValueError):
            return None
        return value if entries(value) is not None else None

    def _guard(self):
        """The published guard, or None, with the refusal naming why, when it is unreadable or not in admission's
        format. A guard admission cannot read is never overwritten: whatever wrote it may still run what it records.

        Each dead or uncertain entry is recovered here, as `eng admissions recover` would recover it, so that a
        stranded entry whose commands have all ended no longer holds every session's heavy work until a person
        removes it. An entry recovery leaves is checked again after HEAVY_RECOVER_SECONDS. Until then it stays in
        self.stranded, and the attempt counts it as the slots it held (#89)."""
        self.stranded = {}
        if not self.guard.exists():
            return {"vocaroEngineering": 1, "owners": {}}
        value = self._recorded()
        if value is None:
            self.refusal, self.waiting = "guard", (f"the guard {self.guard} is unreadable or not in admission's format "
                                                   "and requires inspection; `python3 scripts/eng admissions recover` lists it")
            return None
        # Never reclaim a disappeared owner without a census: its children may still use a different session.
        for identity, owner in list(value["owners"].items()):
            if not (owner.get("uncertain") or not running(owner)):
                continue
            why = self._reclaim(value, identity)
            if why is not None:
                self.stranded[identity] = (owner, why)
        return value

    def _count_stranded(self):
        """Count each stranded entry as the slots it held: every slot up to HEAVY_SLOTS_MAX for exclusive work, and
        otherwise the slots whose files name its identity, as every admission writes them. An entry whose slot
        files no longer name it is counted as the lowest slot not already counted. An entry that held the model
        is counted as its holder. Returns the slots counted and whether the model is."""
        self.counted, reserved, unnamed = [], set(), []
        for identity, (owner, why) in self.stranded.items():
            if owner.get("exclusive"):
                slots = list(range(self.maximum))
            else:
                slots = [slot for slot in range(self.maximum) if self._names(f"slot{slot}", identity)]
                if not slots: unnamed.append(len(self.counted))
            reserved.update(slots)
            self.counted.append([identity, owner, why, slots, bool(owner.get("model") or owner.get("exclusive"))])
        for index in unnamed:
            free = [slot for slot in range(self.maximum) if slot not in reserved]
            if free:
                self.counted[index][3] = free[:1]
                reserved.add(free[0])
        return reserved, any(model for *_rest, model in self.counted)

    def _names(self, name, identity):
        """Whether the lock file of that name in the slot directory names this identity as its last holder."""
        try:
            written = (self.directory / name).read_text(errors="replace").split(" ", 2)
        except OSError:
            return False
        return len(written) > 1 and written[1] == identity

    def _stranded_text(self):
        """The stranded entries counted, for a waiting message: each with why recovery left it and what it is
        counted as, and the command that lists them."""
        def held(slots, model):
            named = f"slot{'s' if len(slots) > 1 else ''} {', '.join(map(str, slots))}" if slots else ""
            return " and ".join(part for part in [named, "the model" if model else ""] if part) or "nothing"
        described = [f"the guard entry of {str(owner.get('label') or 'an unnamed owner')[:LABEL_LIMIT]}, PID {owner['pid']}, is "
                     f"{'uncertain' if owner.get('uncertain') else 'dead'} and requires inspection ({why}), and is "
                     f"counted as {held(slots, model)}" for _identity, owner, why, slots, model in self.counted]
        return "; ".join(described) + (f"; `python3 scripts/eng admissions recover` lists "
                                       f"{'it' if len(described) == 1 else 'them'}")

    def _refused_beside_stranded(self):
        """Name the stranded entries counted in the refusal the attempt just made, which they then count as."""
        if self.counted:
            self.refusal, self.waiting = "stranded", f"{self.waiting}; {self._stranded_text()}"

    def _reclaim(self, state, identity):
        """Recover a dead or uncertain entry: None once it is removed, or why it stays. An entry that stays is
        checked again only after HEAVY_RECOVER_SECONDS, since a census reads every process on the host."""
        now = time.monotonic()
        checked = self.unrecovered.get(identity)
        if checked is not None and now - checked[0] < self.recheck:
            return checked[1]
        removed, message, why = reclaim(self.guard, state, identity, automatic=True)
        if removed:
            self.unrecovered.pop(identity, None)
            print(f"heavy: {message}", file=sys.stderr, flush=True)
            return None
        self.unrecovered[identity] = (now, why)
        return why

    def _waiters(self):
        return queued_requests(self.queue)

    def _record(self, waiters):
        record_queue(self.queue, waiters)

    def _holds(self, waiter):
        """Whether a queued request ahead of this one holds it back now. An exclusive request does for as long
        as its process runs. A whole-host request does while its process last asked within HEAVY_PLACE_SECONDS:
        one that keeps its place between successive waits (Place) holds nothing back while it is away, such as
        while the second Mac is offered its lane and perhaps runs it (vocaro-engineering#174)."""
        if not waiter.get("wholeHost"):
            return True
        seen = waiter.get("seen")
        return isinstance(seen, (int, float)) and time.time() - seen < self.hold

    def _log(self, event, **fields):
        """Append this request's line to the admission log. The caller holds the coordination lock."""
        append_log({"event": event, "identity": self.identity, "pid": os.getpid(), "kind": self.kind,
                    "label": self.label[:LABEL_LIMIT], **fields})

    def _seen(self):
        """What the request saw, for its admitted or withdrawn line (#49): the holders and the one-minute load
        when it asked, and, once an attempt was refused, why it waited."""
        fields = {"holders": self.holders, "load": self.loads[0] if self.loads else None}
        if self.counted:
            # The stranded entries recovery left, which the latest attempt counted as the slots they held (#89).
            fields["stranded"] = [{"identity": identity, "pid": owner["pid"], "label": str(owner.get("label"))[:LABEL_LIMIT],
                                   "uncertain": bool(owner.get("uncertain")), "why": why, "slots": slots, "model": model}
                                  for identity, owner, why, slots, model in self.counted]
        if self.refusals:
            last, peak = self.loads[1:] if self.loads else (None, None)
            fields["wait"] = {"refusals": dict(self.refusals), "first": next(iter(self.refusals)), "last": self.refusal,
                              "load": {"last": last, "max": peak}, "memory": self.memory}
        return fields

    def _count(self):
        """Count the refusal of the attempt just made by its kind, and read the memory pressure beside it."""
        self.refusals[self.refusal] += 1
        level = memory_level()
        if level is not None: self.memory = max(level, self.memory or 0)

    def _withdraw(self, error):
        """Log that this request stopped waiting without admission, and take it out of the queue if it is there."""
        from vocaro_engineering.processes import Interrupted
        outcome = ("stopped" if isinstance(error, (Interrupted, KeyboardInterrupt))
                   else "timed-out" if isinstance(error, TimeoutError) else "failed")
        with (self.directory / "coordination.lock").open("a+") as bridge:
            fcntl.flock(bridge, fcntl.LOCK_EX)
            # A request whose wait timed out keeps its place, if it has one, for its next wait; its caller leaves it.
            if self.queued and not (self.place is not None and outcome == "timed-out"):
                self._record([waiter for waiter in self._waiters() if waiter["identity"] != self.queued_as])
            self._log("withdrawn", outcome=outcome, waited=round(time.monotonic() - self.started, 3), **self._seen())
        self.queued = False

    def _extra_refusal(self):
        """Why no slot beyond the guaranteed ones may be taken now, as the refusal's kind and its reason, or None.
        The reason names the limit that refuses and its value beside what was read (#49)."""
        if self.maximum == self.slots:
            return "slots", f"HEAVY_SLOTS_MAX={self.maximum} allows none beyond them"
        settling = time.time() - last_admission(self.directory, self.maximum)
        if settling < self.settle:
            return "settle", (f"the load does not yet show the job admitted {int(settling)}s ago, "
                              f"within HEAVY_SETTLE_SECONDS={self.settle:g}")
        return pressure(self.load_limit)

    def _take_model(self):
        """The model lock's descriptor, or None with its holder named. The caller holds the coordination lock,
        under which every holder writes its identity, so the lock's holder is the one its file names."""
        fd = os.open(self.directory / "model", os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            holder = os.pread(fd, 4096, 0).decode(errors="replace").strip().split(" ", 2)
            os.close(fd)
            self.refusal, self.waiting = "model", ("the on-device model, one run at a time, is held by "
                                                   f"{holder[2] if len(holder) == 3 else 'an unnamed holder'}")
            return None
        return fd

    def _release(self):
        for _slot, fd in self.descriptors:
            os.close(fd)
        if self.held_model is not None:
            os.close(self.held_model)
        self.descriptors, self.held_model = [], None

    def _extra_slot(self, reserved=(), kept=None):
        """A slot beyond the guaranteed ones while the host shows spare capacity, or None. kept is the whole-host
        request queued ahead, for which the guaranteed slots are kept: a refusal then says so when one of them is
        free, as the kind wholeHost, since the request would otherwise have taken it (vocaro-engineering#174)."""
        refusal = self._extra_refusal()
        if refusal is None:
            taken = take_slot(self.directory, self.maximum, start=self.slots, skip=reserved)
            if taken is not None:
                return taken
            refusal = "slots", (f"so is every slot up to HEAVY_SLOTS_MAX={self.maximum}" if kept is None else
                                f"every slot beyond them up to HEAVY_SLOTS_MAX={self.maximum} is busy")
        kind, reason = refusal
        free = None
        if kept is not None:
            # Whether a guaranteed slot is free: probed and let go at once, under the coordination lock.
            free = take_slot(self.directory, self.slots, skip=reserved)
            if free is not None: os.close(free[1])
        if free is not None:
            self.refusal, self.waiting = "wholeHost", (f"a guaranteed slot is free but kept for a whole-host lane queued "
                                                       f"ahead of it: {kept['label']}; and {reason}")
        else:
            self.refusal, self.waiting = kind, f"all {self.slots} guaranteed slots are busy and {reason}"
        return None

    def _held_back(self, state):
        """Refuse the attempt at the model or a slot. Exclusive work holds both while its guard entry stands,
        so whatever refused the request there, it waits behind that work, which the message names."""
        holder = next((owner for identity, owner in state["owners"].items()
                       if owner.get("exclusive") and identity not in self.stranded), None)
        if holder is not None:
            self.refusal, self.waiting = "exclusive", ("it waits behind exclusive work holding the host: "
                                                       f"{str(holder.get('label') or 'an unnamed owner')[:LABEL_LIMIT]}")
        else:
            self._refused_beside_stranded()
        return False

    def _attempt(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        with (self.directory / "coordination.lock").open("a+") as bridge:
            fcntl.flock(bridge, fcntl.LOCK_EX)
            load = one_minute_load()
            if load is not None:
                self.loads = [load, load, load] if self.loads is None else [self.loads[0], load, max(self.loads[2], load)]
            waiters = self._waiters()
            if self.exclusive or self.whole_host:
                mine = next((waiter for waiter in waiters if waiter["identity"] == self.queued_as), None)
                if mine is None:
                    # Its first attempt sets its place, whatever refuses it now.
                    born = process_start(os.getpid())
                    if born is None:
                        raise RuntimeError("Cannot establish the admission owner's process identity")
                    mine = {"identity": self.queued_as, "pid": os.getpid(), "start": born, "label": self.label[:LABEL_LIMIT],
                            **({"wholeHost": True} if self.whole_host else {})}
                    waiters.append(mine)
                # Set first, so that leaving the wait by any exception withdraws the record.
                self.queued = True
                if self.whole_host:
                    # A whole-host request holds later heavy work back only while it keeps asking (_holds).
                    mine["seen"] = round(time.time(), 3)
                self._record(waiters)
            state = self._guard()
            if self.holders is None:
                # The admissions the guard records when the request asks, a dead or uncertain one among them.
                recorded = state or self._recorded()
                self.holders = len(recorded["owners"]) if recorded else None
            if state is None:
                return False
            reserved, model_counted = self._count_stranded()
            # The first request queued ahead of this one that holds it back now, if any.
            ahead = next((waiter for waiter in waiters if waiter["identity"] == self.queued_as or self._holds(waiter)), None)
            if ahead is not None and ahead["identity"] == self.queued_as:
                ahead = None
            # An exclusive request cannot be admitted while a stranded entry is counted as any slot, nor a whole-host
            # one while it is counted as a guaranteed slot, so one queued ahead then holds nothing back: work within
            # the free slots goes on beside the stranded entry (#89).
            if ahead is not None and (any(slot < self.slots for slot in reserved) if ahead.get("wholeHost") else reserved):
                ahead = None
            # A whole-host request queued ahead keeps the guaranteed slots for itself, so that they drain toward it
            # while lighter heavy work goes on beyond them; exclusive and whole-host requests wait their turn (#174).
            kept = ahead if ahead is not None and ahead.get("wholeHost") and not (self.exclusive or self.whole_host) else None
            if ahead is not None and kept is None:
                # Work already admitted runs on; nothing new starts before the request queued ahead.
                if ahead.get("wholeHost"):
                    self.refusal, self.waiting = "wholeHost", f"it waits behind a whole-host lane queued ahead of it: {ahead['label']}"
                else:
                    self.refusal, self.waiting = "exclusive", f"it waits behind exclusive work queued ahead of it: {ahead['label']}"
                return False
            # The model first, so that a request it refuses names the model's holder.
            if self.model:
                if model_counted:
                    self.refusal, self.waiting = "stranded", "the on-device model, one run at a time, is counted as held"
                    return self._held_back(state)
                self.held_model = self._take_model()
                if self.held_model is None:
                    return self._held_back(state)
            wanted = self.maximum if self.exclusive else self.slots if self.whole_host else 1
            for _ in range(wanted):
                taken = None if kept is not None else take_slot(self.directory, self.maximum if self.exclusive else self.slots,
                                                                skip=reserved)
                if taken is None and not self.exclusive and not self.whole_host:
                    taken = self._extra_slot(reserved, kept)
                if taken is None:
                    busy = wanted - len(self.descriptors)
                    if self.exclusive:
                        self.refusal, self.waiting = "slots", (f"exclusive work needs every slot up to HEAVY_SLOTS_MAX="
                                                               f"{self.maximum} free, and {busy} {'is' if busy == 1 else 'are'} busy")
                    elif self.whole_host:
                        self.refusal, self.waiting = "slots", (f"a whole-host lane needs every guaranteed slot "
                                                               f"(HEAVY_SLOTS={self.slots}) free, and {busy} "
                                                               f"{'is' if busy == 1 else 'are'} busy")
                    self._release()
                    return self._held_back(state)
                self.descriptors.append(taken)
            born = process_start(os.getpid())
            if born is None:
                self._release()
                raise RuntimeError("Cannot establish the admission owner's process identity")
            state["owners"][self.identity] = {"pid": os.getpid(), "start": born, "label": self.label,
                                                "exclusive": self.exclusive, "model": self.model,
                                                "admitted": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                                **({"wholeHost": True} if self.whole_host else {})}
            publish_guard(self.guard, state)
            if self.queued:
                # The next queued request waits for these slots, and every other behind it.
                self._record([waiter for waiter in waiters if waiter["identity"] != self.queued_as])
                self.queued = False
            held = [(f"slot{slot}", fd) for slot, fd in self.descriptors]
            if self.held_model is not None:
                held.append(("model", self.held_model))
            locks = []
            for name, fd in held:
                os.ftruncate(fd, 0)
                os.write(fd, f"{os.getpid()} {self.identity} {self.label[:LABEL_LIMIT]}\n".encode())
                os.set_inheritable(fd, True)
                locks.append({"fd": fd, "path": str(self.directory / name), "nonce": self.identity})
            self.previous = os.environ.get(TOKEN)
            os.environ[TOKEN] = json.dumps({"schemaVersion": 1, "exclusive": self.exclusive, "model": self.model,
                                            "wholeHost": self.whole_host, "guard": str(self.guard), "locks": locks})
            self.admitted = time.monotonic()
            self._log("admitted", slots=[slot for slot, _fd in self.descriptors],
                      waited=round(self.admitted - self.started, 3), **({"wholeHost": True} if self.whole_host else {}),
                      **self._seen())
            return True

    def __enter__(self):
        if inherited(self.exclusive, self.model):
            self.reused = True
            return self
        from vocaro_engineering.processes import Interrupted, requested
        started = self.started = time.monotonic()
        reported = None
        try:
            while not self._attempt():
                self._count()
                # Nothing is published yet but an exclusive or whole-host request's place in the queue,
                # so a stop or a timeout leaves nothing to release once it withdraws.
                if requested() is not None:
                    raise Interrupted(requested(), "while waiting for admission")
                waited = int(time.monotonic() - started)
                if reported is None or waited - reported >= 300:
                    print(f"heavy: waiting ({waited}s): {self.waiting}", file=sys.stderr, flush=True)
                    reported = waited
                if time.monotonic() - started >= self.timeout:
                    raise TimeoutError(f"Resource queue busy or an orphan requires inspection ({self.waiting}); no lock was reclaimed")
                time.sleep(self.poll)
        except BaseException as error:
            try:
                self._withdraw(error)
            except Exception:
                # A queued request that cannot leave the queue says so. The log alone never changes
                # how a request fails: `eng run --if-free` tells a busy host by its TimeoutError.
                if self.queued: raise
            raise
        _held[self.identity] = self
        print(f"heavy: slot {','.join(str(slot) for slot, _fd in self.descriptors)}"
              f"{' and the model' if self.held_model is not None else ''} after {int(time.monotonic() - started)}s: "
              f"{self.label[:LABEL_LIMIT]}", file=sys.stderr, flush=True)
        if self.counted:
            print(f"heavy: admitted beside a stranded entry: {self._stranded_text()}", file=sys.stderr, flush=True)
        return self

    def observe(self, line):
        """Append a line to this admission's process record: what supervision in this process saw of a
        command (#61). processes.supervise writes it, and so does a caller that supervises its own
        commands, as StudyWright's push keeper does: a dictionary with the command's own key as
        `command`, and `argv` before it starts, `root` and its `spaces` once started, the `processes`
        ([PID, start]) and `spaces` each sample sees first, or `settled` once its end is confirmed.
        Recovery trusts a record only whole, so a line that cannot be written removes the record and
        ends recording, and recovery then leaves the entry for a check by hand. As with the log, a
        failure never fails or holds up the run."""
        with self.recording:
            if self.unrecorded: return
            try:
                data = (json.dumps(line) + "\n").encode()
                fd = os.open(self.process_record, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
                try:
                    if os.write(fd, data) != len(data): raise OSError("the line was cut short")
                finally:
                    os.close(fd)
            except Exception as error:
                self.unrecorded = True
                with contextlib.suppress(OSError): self.process_record.unlink(missing_ok=True)
                with contextlib.suppress(Exception):
                    print(f"heavy: the process record was not written, so recovering this entry needs a check by "
                          f"hand: {error}", file=sys.stderr, flush=True)

    def _settle(self, release):
        """Remove this job's entry, or mark it uncertain. The caller holds the coordination lock.

        Only this job's entry changes, whatever the other owners' state: a release that waited
        for a dead owner's recovery would leave this entry dead once this process ends. A guard
        that is unreadable or not in admission's format is never overwritten, so this entry stays
        in it, for inspection with the rest.
        """
        state = self._recorded()
        owners = state["owners"] if state is not None else None
        if owners is None or self.identity not in owners:
            return
        if release:
            del owners[self.identity]
            if owners: publish_guard(self.guard, state)
            else: self.guard.unlink(missing_ok=True)
            # After the entry, so that a release cut short never leaves an entry whose record is gone.
            with contextlib.suppress(OSError): self.process_record.unlink(missing_ok=True)
        else:
            owners[self.identity]["uncertain"] = True
            publish_guard(self.guard, state)

    def interrupted(self):
        """Mark this job's entry uncertain while a stop drains its command, in case SIGKILL follows."""
        if self.reused:
            return
        with (self.directory / "coordination.lock").open("a+") as bridge:
            fcntl.flock(bridge, fcntl.LOCK_EX)
            self._settle(release=False)

    def __exit__(self, kind, _value, _traceback):
        if self.reused:
            return False
        _held.pop(self.identity, None)
        if self.previous is None: os.environ.pop(TOKEN, None)
        else: os.environ[TOKEN] = self.previous
        with (self.directory / "coordination.lock").open("a+") as bridge:
            fcntl.flock(bridge, fcntl.LOCK_EX)
            slots = [slot for slot, _fd in self.descriptors]
            self._release()
            # An exception may leave unobserved children. Retain the guard for inspection, unless
            # nothing is left to inspect: the command could not start (a mistyped name must not
            # stop the host), or a stop or a timeout saw the command and every observed descendant end.
            outcome = ending(kind)
            self._settle(release=outcome != "uncertain")
            self._log("ended", outcome=outcome, slots=slots, held=round(time.monotonic() - self.admitted, 3))
        return False


@contextlib.contextmanager
def heavy_slot(label, *, exclusive=False, timeout=5400, model=False, whole_host=False):
    with Admission(label, exclusive, timeout, model, whole_host):
        yield


def read_entry(line):
    """A log line with its time parsed, or None when it is not a whole entry. A line an older toolkit wrote
    lacks what the request saw, its holders, load and wait (#49), and reads as it is."""
    number = lambda value: value is None or isinstance(value, (int, float))
    try:
        entry = json.loads(line)
        entry["time"] = datetime.fromisoformat(entry["time"])
        # A recovery's line counts no seconds of its own.
        field = {"admitted": "waited", "withdrawn": "waited", "ended": "held", "recovered": None}[entry["event"]]
        seconds = entry[field] if field else 0
        # What the request saw, as the summary reads it: the load, and the refusals of each kind and the
        # highest load of a request that waited.
        wait = entry.get("wait")
        seen = number(entry.get("load")) and (wait is None or (bool(wait["refusals"]) and number(wait["load"]["max"])
            and all(type(count) is int and count > 0 for count in wait["refusals"].values())))
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    if (entry["time"].tzinfo is None or not isinstance(seconds, (int, float)) or not seen
            or not all(isinstance(entry.get(key), str) for key in ["identity", "kind", "label"])
            or (entry["event"] in ("withdrawn", "ended") and not isinstance(entry.get("outcome"), str))):
        return None
    return entry


def read_log(paths=None):
    """The entries of the logs named, or of this host's, oldest first, and how many lines could not be
    read. This host's two generations are opened under the coordination lock, so that the log cannot
    become the previous generation between the two."""
    entries, unreadable = [], 0
    with contextlib.ExitStack() as stack:
        if paths is None:
            directory, path, streams = slot_directory(), log_path(), []
            directory.mkdir(parents=True, exist_ok=True)
            with (directory / "coordination.lock").open("a+") as bridge:
                fcntl.flock(bridge, fcntl.LOCK_EX)
                for name in [PREVIOUS_LOG, LOG]:
                    with contextlib.suppress(FileNotFoundError):
                        streams.append(stack.enter_context(path.with_name(name).open("rb")))
        else:
            streams = [stack.enter_context(Path(name).open("rb")) for name in paths]
        for stream in streams:
            for line in stream:
                entry = read_entry(line)
                if entry is None: unreadable += 1
                else: entries.append(entry)
    return entries, unreadable


def duration(seconds):
    seconds = round(seconds)
    if seconds < 60: return f"{seconds}s"
    if seconds < 3600: return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m"


def summarize(entries, since=None, until=None):
    """Lines that summarize the requests made in the period, for each kind and each label within it.

    A request counts in the period when its admitted or withdrawn line falls in it; its end may come
    later. One admitted before the log's first line, whose end alone the log keeps, counts in none.

    A request that waited counts behind each kind that refused it, and its wait is divided among those
    kinds in proportion to their refusals (#49). Where the log also has requests that say what they saw,
    those an older toolkit logged count as unrecorded.
    """
    requests = {}
    for entry in entries:
        requests.setdefault(entry["identity"], {})[entry["event"]] = entry
    groups = {}

    def saw(group, first):
        """Add what a request saw to its group: the kinds that refused it, with its wait, and its loads."""
        if "holders" not in first:
            group["behind"]["unrecorded"] += 1
            group["queued"]["unrecorded"] += first["waited"]
            return
        group["recorded"] += 1
        if first.get("load") is not None: group["loads"].append(first["load"])
        wait = first.get("wait")
        if wait is None: return
        refusals = sum(wait["refusals"].values())
        for kind, count in wait["refusals"].items():
            group["behind"][kind] += 1
            group["queued"][kind] += first["waited"] * count / refusals
        if wait["load"]["max"] is not None: group["peaks"].append(wait["load"]["max"])

    for lines in requests.values():
        first, end = lines.get("admitted") or lines.get("withdrawn"), lines.get("ended")
        if first is None or (since and first["time"] < since) or (until and first["time"] >= until):
            continue
        # Keyed by kind and label, and by kind and None for the kind as a whole.
        for key in [(first["kind"], None), (first["kind"], first["label"])]:
            group = groups.setdefault(key, {"admitted": 0, "withdrawn": Counter(), "ends": Counter(),
                                            "waited": [], "held": [], "recorded": 0, "behind": Counter(),
                                            "queued": Counter(), "loads": [], "peaks": []})
            group["waited"].append(first["waited"])
            saw(group, first)
            if first["event"] == "withdrawn":
                group["withdrawn"][first["outcome"]] += 1
                continue
            group["admitted"] += 1
            # An admission with no end logged is still held, or its process was killed and its entry
            # either awaits recovery or was recovered since.
            group["ends"][end["outcome"] if end else "recovered" if "recovered" in lines else "unlogged"] += 1
            if end: group["held"].append(end["held"])

    def counts(counter):
        return ", ".join(f"{name} {number}" for name, number in counter.most_common())

    def describe(group):
        withdrawn, waited, held = sum(group["withdrawn"].values()), group["waited"], group["held"]
        text = f"{group['admitted']} admitted"
        if withdrawn: text += f", {withdrawn} withdrawn ({counts(group['withdrawn'])})"
        text += f"; waited mean {duration(sum(waited) / len(waited))}, max {duration(max(waited))}"
        behind, loads, peaks = group["behind"], group["loads"], group["peaks"]
        if group["recorded"] and behind:
            # The kind that refused the most requests first, and the requests an older toolkit logged last.
            kinds = sorted(behind, key=lambda kind: (kind == "unrecorded", -behind[kind], kind))
            text += "; waited behind " + ", ".join(f"{kind} {behind[kind]} ({duration(group['queued'][kind])})" for kind in kinds)
        if loads:
            text += f"; load when asked mean {sum(loads) / len(loads):.1f}, max {max(loads):.1f}"
            if peaks: text += f", while waiting max {max(peaks):.1f}"
        if held: text += f"; held mean {duration(sum(held) / len(held))}, max {duration(max(held))}, total {duration(sum(held))}"
        if group["ends"]: text += f"; ends {counts(group['ends'])}"
        return text

    order = {"exclusive": 0, "model": 1, "heavy": 2}
    summary = []
    for kind in sorted({kind for kind, _label in groups}, key=lambda kind: (order.get(kind, 3), kind)):
        summary.append(f"{kind}: {describe(groups[kind, None])}")
        labels = sorted((label for each, label in groups if each == kind and label is not None),
                        key=lambda label: (-len(groups[kind, label]["waited"]), label))
        summary += [f"  {label}: {describe(groups[kind, label])}" for label in labels]
    return summary


def local_time(text):
    """An ISO 8601 date or time, taken as local unless it names a zone."""
    moment = datetime.fromisoformat(text)
    return moment if moment.tzinfo else moment.astimezone()


def report(paths=None, since=None, until=None, out=print):
    """Print a summary of the admission log, this host's or the copies named, and return 0."""
    entries, unreadable = read_log(paths)
    source = ", ".join(map(str, paths)) if paths else str(log_path())
    shown = lambda moment: moment.astimezone().strftime("%Y-%m-%d %H:%M %Z")
    if not entries:
        out(f"No admissions are logged in {source}.")
    else:
        oldest = min(entry["time"] for entry in entries)
        out(f"Admission log {source}, from {shown(oldest)} to {shown(max(entry['time'] for entry in entries))}.")
        if since and since < oldest:
            out(f"The period begins before the log does: requests made before {shown(oldest)} are no longer in it.")
        period = " ".join(([f"from {shown(since)}"] if since else []) + ([f"until {shown(until)}"] if until else []))
        summary = summarize(entries, since, until)
        out(f"Requests made {period or 'at any time the log covers'}:" if summary else
            f"No requests were made {period or 'at any time the log covers'}.")
        for line in summary: out(line)
    if unreadable: out(f"{unreadable} line(s) could not be read.")
    return 0


# What recovery tells a person to do about an entry it leaves.
BY_HAND = ("check by hand that nothing its owner started still runs, then remove its entry from the guard "
           "(guides/shared-host.md, Lock reclamation)")


def entry_age(owner):
    """How long ago an entry's job was admitted, or, for an entry an older toolkit wrote, its owner started."""
    try: return f"admitted {duration(max(0, time.time() - datetime.fromisoformat(owner['admitted']).timestamp()))} ago"
    except (KeyError, TypeError, ValueError): pass
    try:
        began = time.mktime(time.strptime(" ".join(owner["start"].split()), "%a %b %d %H:%M:%S %Y"))
        return f"its owner started {duration(max(0, time.time() - began))} ago"
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return "admitted at an unknown time"


def ended(owner):
    """Whether an entry's owner has certainly ended: its PID is gone, or ps reads another start there. A ps
    that cannot say leaves the owner running, so that recovery never takes a live owner's entry."""
    try: os.kill(owner["pid"], 0)
    except ProcessLookupError: return True
    except PermissionError: pass
    listed = subprocess.run(["ps", "-p", str(owner["pid"]), "-o", "lstart="], capture_output=True, text=True)
    start = " ".join(listed.stdout.split())
    return listed.returncode == 0 and bool(start) and start != " ".join(owner["start"].split())


def census(path):
    """What still runs of the commands an admission's process record leaves unsettled, as {PID: why}, with
    how many commands, recorded processes and process groups or sessions were checked. A ValueError says
    why the record cannot vouch for every command: a line that cannot be read, or a command whose
    process it never named.

    It is the census a run ends with (processes.survivors): each recorded process has ended, by PID and
    start, and no live process is in a group or session one was seen using, but for an ID that is now a
    live process's PID (#26). A command the record settled was confirmed ended by its own supervision.
    """
    from vocaro_engineering.processes import snapshot, survivors
    commands = {}
    with path.open("rb") as stream:
        for number, text in enumerate(stream, 1):
            try:
                line = json.loads(text)
                if not isinstance(line["command"], str) or not isinstance(line.get("argv", []), list):
                    raise TypeError("an unexpected value")
                command = commands.setdefault(line["command"], {"argv": [], "root": None, "processes": set(),
                                                                "spaces": set(), "settled": False})
                processes = {(pid, start) for pid, start in line.get("processes", [])}
                spaces, root = set(line.get("spaces", [])), line.get("root", command["root"])
                if not all(type(pid) is int and isinstance(start, str) for pid, start in processes) or \
                        not all(type(space) is int for space in spaces) or not (root is None or type(root) is int):
                    raise TypeError("an unexpected value")
            except (ValueError, KeyError, TypeError, AttributeError):
                raise ValueError(f"line {number} of {path} cannot be read") from None
            command["argv"], command["root"] = line.get("argv", command["argv"]), root
            command["processes"] |= processes
            command["spaces"] |= spaces
            command["settled"] = command["settled"] or "settled" in line
    unsettled = [command for command in commands.values() if not command["settled"]]
    for command in unsettled:
        if command["root"] is None:
            raise ValueError(f"its command `{' '.join(map(str, command['argv']))[:LABEL_LIMIT]}` may have started before "
                             "the record named its process")
    current, left = snapshot(), {}
    for command in unsettled:
        for pid, start in command["processes"]:
            if pid in current and " ".join(current[pid][2].split()) == " ".join(start.split()):
                left[pid] = "a recorded process, still running"
        if command["root"] in current and all(pid != command["root"] for pid, _start in command["processes"]):
            left.setdefault(command["root"], "its command's PID, whose start the record never saw, is in use")
    spaces = set().union(*(command["spaces"] | {command["root"]} for command in unsettled))
    for pid, group, sid in survivors(spaces):
        left.setdefault(pid, f"group {group}, session {sid}")
    return left, len(unsettled), sum(len(command["processes"]) for command in unsettled), len(spaces)


def recover(owner=None, out=print):
    """List the guard's dead or uncertain entries, or recover the one whose owner's PID or identity is
    named: remove that entry alone, and the guard with its last one, once its owner has ended and a census
    against its process record finds nothing of its commands still running. Returns 0 when it listed the
    entries or removed one, and 1 when it removed nothing.

    A live owner's entry is never touched: the owner removes it, or leaves it, itself. An entry without a
    process record, written by an older toolkit or by a caller that runs its own commands without
    recording them, is left for a check by hand, as is a guard that is unreadable or not in admission's format,
    as admission refuses it: one whose owners are not all entries in admission's format among them (#72).
    """
    guard = guard_path(os.environ)
    directory = slot_directory()
    directory.mkdir(parents=True, exist_ok=True)

    def shown(entry):
        """An entry's label on one line: an eng admit label is its command line, which can span several."""
        return " ".join(entry["label"].split())[:LABEL_LIMIT]

    def recorded(identity):
        """The entry's process record, or None: an identity that is not admission's names none."""
        path = record_path(guard, identity) if re.fullmatch(r"[0-9a-f]{32}", identity) else None
        return path if path is not None and path.is_file() else None

    with (directory / "coordination.lock").open("a+") as bridge:
        fcntl.flock(bridge, fcntl.LOCK_EX)
        try: state = json.loads(guard.read_text())
        except FileNotFoundError: state = {"vocaroEngineering": 1, "owners": {}}
        except (OSError, ValueError): state = None
        owners = entries(state)
        if owners is None:
            out(f"{guard} is unreadable or not in admission's format. Recovery leaves it alone; inspect it by hand "
                "(guides/shared-host.md, Lock reclamation).")
            return 1
        if owner is None:
            # The entries that refuse admission, as admission reads them.
            stranded = [(identity, entry) for identity, entry in owners.items()
                        if entry.get("uncertain") or not running(entry)]
            if not stranded:
                out(f"No entry of {guard} is dead or uncertain.")
                return 0
            out(f"{len(stranded)} entr{'y' if len(stranded) == 1 else 'ies'} of {guard} "
                f"{'is' if len(stranded) == 1 else 'are'} dead or uncertain:")
            for identity, entry in stranded:
                condition = ("uncertain, and its owner still runs" if running(entry) else
                             "uncertain, and its owner has ended" if entry.get("uncertain") else "its owner has ended")
                out(f"  {identity} {shown(entry)}: PID {entry['pid']}, {entry_age(entry)}, {condition}; "
                    f"{'a' if recorded(identity) else 'no'} process record")
            out("Recover one with `python3 scripts/eng admissions recover OWNER`, OWNER its PID or identity.")
            return 0
        named = [identity for identity, entry in owners.items()
                 if identity == owner or str(entry["pid"]) == owner]
        if len(named) != 1:
            out(f"No entry of {guard} has the PID or identity {owner}; nothing was changed." if not named else
                f"PID {owner} owns {len(named)} entries of {guard}; name one by its identity: {', '.join(named)}. "
                "Nothing was changed.")
            return 1
        removed, message, _why = reclaim(guard, state, named[0])
        out(message)
        return 0 if removed else 1


def reclaim(guard, state, identity, automatic=False):
    """Recover one entry of a guard in admission's format: remove it, and the guard with its last one, once its
    owner has ended and a census against its process record finds nothing of its commands still running.
    Returns whether it removed the entry, what recovery prints of it, and, when it stays, why in a few words.
    The caller holds the coordination lock. Admission calls it automatically for each dead or uncertain entry,
    and its log line says so; the conditions are the ones a person's recovery checks, so nothing an attended
    recovery would leave is removed."""
    owners = state["owners"]
    entry = owners[identity]
    name = f"{' '.join(entry['label'].split())[:LABEL_LIMIT]} (PID {entry['pid']})"
    if not ended(entry):
        return False, (f"{name} still runs, so its entry stays: its owner removes the entry, or leaves it for recovery, "
                       "when it ends."), "its owner still runs"
    path = record_path(guard, identity) if re.fullmatch(r"[0-9a-f]{32}", identity) else None
    if path is None or not path.is_file():
        return False, (f"{name} has no process record, so its entry stays: an older toolkit admitted it, it ended before "
            "it began a recorded command, its record could not be written, or its caller runs its own commands "
            "without recording them, as an older push keeper does; a push keeper's owner.json, in its checkout's "
            f"transaction lock, lists each command it ran. To recover it, {BY_HAND}."), "it has no process record"
    try:
        left, commands, processes, spaces = census(path)
    except ValueError as error:
        return False, (f"{name}'s process record cannot vouch for every command, so its entry stays: {error}. To "
                       f"recover it, {BY_HAND}."), "its process record cannot vouch for every command"
    if left:
        listing = subprocess.run(["ps", "-o", "pid,ppid,pgid,lstart,args", "-p", ",".join(map(str, sorted(left)))],
                                 capture_output=True, text=True).stdout.rstrip()
        return False, (f"{len(left)} process(es) of {name} still run, so its entry stays: "
            + "; ".join(f"PID {pid} ({why})" for pid, why in sorted(left.items())) + (f"\n{listing}" if listing else "")), \
            f"{len(left)} of its processes still run: PID {', '.join(map(str, sorted(left)))}"[:200]
    del owners[identity]
    if owners: publish_guard(guard, state)
    else: guard.unlink(missing_ok=True)
    with contextlib.suppress(OSError): path.unlink(missing_ok=True)
    append_log({"event": "recovered", "identity": identity, "pid": entry["pid"],
                "kind": "exclusive" if entry.get("exclusive") else "model" if entry.get("model") else "heavy",
                "label": entry["label"][:LABEL_LIMIT], "uncertain": bool(entry.get("uncertain")),
                "commands": commands, "processes": processes, "spaces": spaces,
                **({"automatic": True} if automatic else {})})
    return True, (f"Recovered {name}: " + (f"nothing of its {commands} unsettled command(s) still runs, among {processes} "
        f"recorded processes and {spaces} process groups and sessions" if commands else
        "its process record shows every command it ran confirmed ended")
        + f". Its entry is removed{', and the guard with it' if not owners else ''}."), None


def recover_main(argv=None):
    """eng admissions recover [OWNER]: the guard's dead or uncertain entries, or the recovery of one."""
    parser = argparse.ArgumentParser(prog="eng admissions recover", description="List the guard's dead or uncertain "
        "entries, or recover one: remove that entry alone, and the guard with its last one, once its owner has ended "
        "and a census against its process record finds nothing of its commands still running. It exits 1 when it "
        "removes nothing, and leaves a live owner's entry, an entry without a process record and a guard that is "
        "unreadable or not in admission's format alone (guides/shared-host.md, Lock reclamation).")
    parser.add_argument("owner", nargs="?", metavar="OWNER", help="the PID or identity of the entry's owner, as the "
                        "listing names it; without it, list the guard's dead or uncertain entries")
    args = parser.parse_args(argv)
    try:
        return recover(args.owner)
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"eng: {error}", file=sys.stderr)
        return 2


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    kind = parser.add_mutually_exclusive_group()
    kind.add_argument("--exclusive", action="store_true")
    kind.add_argument("--model", action="store_true")
    parser.add_argument("--whole-host", action="store_true", help="take every guaranteed slot, as a whole-host lane "
                        "does, so that no other such lane runs beside the command on this host")
    parser.add_argument("--timeout", type=float, default=5400)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command: parser.error("a command is required")
    from vocaro_engineering.processes import forwarding_signals, supervise
    with forwarding_signals(), Admission(" ".join(command), args.exclusive, args.timeout, args.model,
                                         args.whole_host) as admission:
        environment = dict(os.environ)
        return supervise(command, cwd=Path.cwd(), env=environment, on_stop=admission.interrupted)


def json_descriptors():
    token = json.loads(os.environ[TOKEN])
    return [(index, item["fd"]) for index, item in enumerate(token["locks"])]


if __name__ == "__main__":
    # The module as supervision imports it, not this copy run as __main__, so that supervision finds the
    # admission this process holds and writes its process record.
    from vocaro_engineering import resources
    raise SystemExit(resources.main())
