"""Heavy-work admission interoperating with the existing PDF slot locks.

Admission records each job it admits in a guard, named by StudyWright's former host gate lock:
QUEUEKEEPER_GATE_LOCK_FILE, `/tmp/queuekeeper-gate-suite.lock` by default, with `.warm-push-guard`
appended. StudyWright's gate checks an inherited admission against that path, and tests isolate
admission by redirecting it; admission takes no lock there (#25). Exclusive work holds every old
PDF slot. A deadline is never a release. An owner whose process has ended, or that admission
cannot confirm still runs, loses its entry and its slots at the next attempt, which stops what is
left of it first (vocaro/queuekeeper#23).

HEAVY_SLOTS jobs are always admitted while no exclusive request waits. More, up to
HEAVY_SLOTS_MAX, are admitted one at a time while the CPU's busy share and memory pressure show
spare capacity: a fixed count left the host idle behind jobs waiting on simulators, while ten
unqueued builds starved watchdogd until the kernel panicked (2026-10-14). The busy share is the
part of the CPU's time, over about the last BUSY_WINDOW seconds, spent outside idle. It replaced
the one-minute load average, which on a Mac running simulators counts their many daemons' brief
wake-ups: the second Mac read a load of 27 to 66 against a limit of 7.5 while its CPU was up to
half idle, and its extra slots opened almost only when it was idle anyway (vocaro-engineering#200). Admission cannot
preempt a job that becomes heavy later. A whole-host lane, a gate that runs a project's suites
side by side, takes every guaranteed slot, so that two such lanes never run on one Mac at once
(two EPUBLib gates at once put a 10-core Mac at a load of 719 on 2026-11-04, #168), and while it
runs no other heavy work is admitted beside it, not even beyond them: a gate's load dips between
its phases, and another project's gate that squeezed in on 2026-11-05 left it unmeasured (#196).
Each limit can be set for a host in its limits file,
admission-limits.json beside the admission log, which every process there reads, as a number or
as a share of the cores; a limit's environment variable overrides it for one process. Without
the file the defaults stand (#49).

All requests queue in arrival order. Exclusive work waits for earlier ordinary requests as well
as work already running; later ordinary work waits behind it. While heavy jobs kept
taking freed slots, exclusive gates waited for hours (October 2026). A whole-host request joins
the same queue: exclusive and whole-host requests behind it wait their turn, and other heavy work
takes no guaranteed slot while it waits, only a slot beyond them, so that the guaranteed slots
drain toward it rather than a steady stream of heavy jobs keeping one busy for good (#174).
Admitted, exclusive work holds every slot and waits, bounded, for the CPU's busy share to read quiet before its
command starts, and tells the command what it read: the work it waited out still shows in every trailing measure
of the Mac (Admission._quiet, vocaro/queuekeeper#121).

A generative-model run takes one heavy slot and the host's model lock, an flock on `model` in
the slot directory, both or neither: two model runs exclude each other while builds and tests
run beside them. One model workload per machine is a limit of the hardware: two processes
driving one Mac's on-device model both stalled. Exclusive work holds the model with the host.

Each admission appends a line to the host's admission log, and its end another, so that an audit
can count admissions, waits and holds instead of guessing (#46); `qk admissions` summarizes it.
A request that waited says why, which an audit could only infer from the holders logged beside it
(#49): how many of its attempts each kind of refusal refused, and the busy share, load and memory pressure
read; the load is kept for audits beside the busy share, though it no longer refuses.
Its waiting message names the limit that refused it and the limit's value, beside what it read.

Beside its guard entry, each admission keeps a process record: what supervision in the owner's
process, the toolkit's or a caller's own, saw of each command, its processes with their starts and
the process groups and sessions they used, appended at each sample that sees something new. Once
the owner has ended, `qk admissions recover` runs the end-of-run census against that record and
removes the entry only when nothing of an unsettled command still runs (#61). An entry without a
record is left for a check by hand, and `qk admissions remove`, which no allow rule matches, removes it once nothing
its owner may have started is found running. Admission writes the record's first line as it adds the entry, so that an
owner killed before its first command leaves a record too, and recovery then checks the owner's process group for
what it started (#315). Admission itself frees each entry it meets whose owner has ended or cannot be confirmed
running, at once: it sends SIGTERM, then SIGKILL, to what is left of the owner, each process checked by PID and start
first, removes the entry, frees its slots and the model, and logs why. Uncertainty about a holder never keeps a slot
held: jobs can be rerun, and the queue moving comes first (the owner's decision, 2026-11-13, vocaro/queuekeeper#23).
An uncertain entry whose owner still runs, its stop being carried out, admission recovers as `qk admissions recover`
would, and one recovery leaves counts as the slots it held, every slot for exclusive work, so that only work beyond
them waits (#89).
"""
import argparse
from collections import Counter
import contextlib
import contextvars
from datetime import datetime, timedelta, timezone
import fcntl
import functools
import json
import math
import os
from pathlib import Path
import pwd
import re
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from . import lane_wait

TOKEN = "QUEUEKEEPER_RESOURCE_TOKEN"
# `qk run --if-free` waits this many seconds for admission, then exits with BUSY (EX_TEMPFAIL)
# without running: how the second Mac says it cannot take a lane now.
FREE_WAIT, BUSY = 5, 75
# The admission log and its previous generation. A line that would take the log past LOG_LIMIT
# bytes first makes the log the previous generation, so the two together stay within twice it.
LOG, PREVIOUS_LOG, LOG_LIMIT = "admissions.jsonl", "admissions.1.jsonl", 8 << 20
# The latest ended runs of a label whose median is its measured time (measured_seconds).
MEASURED_RUNS = 5
# The characters of a label that logs, queues and messages keep; the guard records it whole.
LABEL_LIMIT = 160
# The path that names the guard's namespace, unless QUEUEKEEPER_GATE_LOCK_FILE names another.
GATE_LOCK = "/tmp/queuekeeper-gate-suite.lock"
# The admissions this process holds, by identity: only the owner's own supervision writes an entry's
# process record. Nested work in another process reuses the admission and writes nothing to it.
_held = {}


def process_start(pid):
    """When pid started, as `ps -o lstart=` prints it, or None when no such process runs.

    A process in an app's sandbox, such as an XCTest runner checking the foreground reservation that
    queuekeeper holds on the second Mac, may not start ps; the kernel's record gives it the same text."""
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
    return Path(environment.get("QUEUEKEEPER_GATE_LOCK_FILE", GATE_LOCK) + ".warm-push-guard")


# The fields of a guard entry as admission writes it, each with its type. Every toolkit has written the first
# four; an entry may lack the others, which later toolkits added or which mark it. A field named in neither,
# such as a newer toolkit may add, is read past.
FIELDS, LATER = {"pid": int, "start": str, "label": str, "exclusive": bool}, {"model": bool, "admitted": str, "uncertain": bool,
                                                                                "wholeHost": bool, "origin": dict}
# Where work that asks for admission comes from, as JSON: the second Mac's runs of a dispatch read it from the Mac that
# sent them (remote.dispatch), since their own checkout is a copy that names no session (work_origin).
ORIGIN = "QUEUEKEEPER_ADMISSION_ORIGIN"
# The fields of an origin, each a string, and the longest each keeps.
ORIGIN_FIELDS, ORIGIN_LIMIT = ("repository", "branch", "worktree", "session", "mac"), 300


def clean_origin(found):
    """The origin fields of found that are nonempty strings, each cut to ORIGIN_LIMIT, or None when it has none."""
    if not isinstance(found, dict): return None
    kept = {field: found[field][:ORIGIN_LIMIT] for field in ORIGIN_FIELDS if isinstance(found.get(field), str) and found[field]}
    return kept or None


def work_origin(directory=None):
    """Where the work asking for admission comes from, for `qk admissions now` to name beside its label, which for a
    plain command is only the command (vocaro-engineering#342): the repository (its main checkout's directory name),
    the branch, the worktree's path, the agent session that ran queuekeeper, and the Mac it ran on. Work the second Mac runs for
    another Mac takes the sender's (ORIGIN). Each field it cannot read is left out; it never raises."""
    try:
        sent = clean_origin(json.loads(os.environ[ORIGIN])) if os.environ.get(ORIGIN) else None
    except ValueError: sent = None
    if sent: return sent
    found = {"session": os.environ.get("CLAUDE_CODE_HOST_SESSION_ID") or os.environ.get("CLAUDE_CODE_SESSION_ID"),
             "mac": os.uname().nodename.split(".")[0]}
    try:
        read = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--show-toplevel", "--git-common-dir",
                               "--abbrev-ref", "HEAD"], cwd=directory, capture_output=True, text=True, timeout=5)
        top, common, branch = read.stdout.splitlines()[:3] if read.returncode == 0 else (None, None, None)
    except (OSError, ValueError, subprocess.SubprocessError): top = common = branch = None
    if common:
        common = Path(common)
        found["repository"] = (common.parent if common.name == ".git" else common).name
    found.update(worktree=top, branch=None if branch == "HEAD" else branch)
    return clean_origin(found)


def whence(origin):
    """An origin as a report line's words, or None."""
    origin = clean_origin(origin)
    if not origin: return None
    return ", ".join(f"{field} {origin[field]}" for field in ORIGIN_FIELDS if field in origin)


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
        base = subprocess.check_output(["/usr/bin/getconf", "DARWIN_USER_TEMP_DIR"], text=True, timeout=5)
        base = base.strip() if isinstance(base, str) else base
        # A relative answer would start another budget in whatever folder the process is in, and a test's mock of
        # subprocess.run answers with a mock, which names a folder MagicMock/ there (vocaro/queuekeeper#161).
        if not isinstance(base, str) or not os.path.isabs(base):
            raise RuntimeError("Cannot establish the host resource directory: getconf DARWIN_USER_TEMP_DIR answered "
                               f"{str(base)[:80]!r}, not an absolute path")
        return Path(base) / "queuekeeper-heavy-slots"
    return Path("/tmp/queuekeeper-heavy-slots")


def log_path():
    """The host's admission log. An isolated HEAVY_SLOT_DIR namespace keeps its own beside its slots.
    The default namespace's is in the account's durable state, which a reboot and the temporary-file
    cleaner leave alone, unlike the slot directory; a HOME private to one session must not start another."""
    if os.environ.get("HEAVY_SLOT_DIR"):
        return Path(os.environ["HEAVY_SLOT_DIR"]) / LOG
    state = "Library/Application Support" if sys.platform == "darwin" else ".local/state"
    return Path(pwd.getpwuid(os.getuid()).pw_dir) / state / "queuekeeper" / LOG


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
    from queuekeeper.processes import Interrupted, NotStarted, TimedOut
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


# The CPU's busy share is read from the kernel's cumulative ticks, host_statistics' HOST_CPU_LOAD_INFO (user,
# system, idle and nice), which need no privilege. Every process on the host keeps its readings in CPU_SAMPLES in
# the slot directory, so that one reading spans about BUSY_WINDOW seconds without waiting for them: it is taken
# against the youngest kept sample at least BUSY_WINDOW seconds old, else the oldest at least BUSY_MINIMUM old. A
# one-off reading with none to take it against waits BUSY_QUICK seconds for its own. Samples are kept for
# BUSY_KEEP seconds, one a second at most. offload_host.py reads them the same way, which it cannot import.
CPU_SAMPLES, BUSY_WINDOW, BUSY_MINIMUM, BUSY_QUICK, BUSY_KEEP = "cpu-samples.json", 15.0, 5.0, 2.0, 120.0
HOST_CPU_LOAD_INFO, CPU_STATES = 3, 4


@functools.lru_cache(maxsize=None)
def _mach():
    """libSystem and this task's host port, once: each mach_host_self call adds a send right."""
    import ctypes
    library = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
    library.mach_host_self.restype = ctypes.c_uint
    return library, library.mach_host_self()


def cpu_ticks():
    """The CPU's cumulative ticks, user, system, idle and nice, or None when they cannot be read."""
    if sys.platform != "darwin":
        return None
    import ctypes
    try:
        library, host = _mach()
        info, count = (ctypes.c_uint * CPU_STATES)(), ctypes.c_uint(CPU_STATES)
        if library.host_statistics(host, HOST_CPU_LOAD_INFO, info, ctypes.byref(count)) != 0:
            return None
    except (OSError, AttributeError):
        return None
    return list(info)


def busy_share(earlier, later):
    """The percentage of the CPU's ticks between two readings spent outside idle, or None when none passed. The
    kernel's counters are 32-bit and wrap."""
    passed = [(after - before) % (1 << 32) for before, after in zip(earlier, later)]
    total = sum(passed)
    return None if total == 0 else round(100.0 * (total - passed[2]) / total, 1)


def cpu_busy(directory, wait=False, ticks=cpu_ticks, now=time.time, sleep=time.sleep):
    """The CPU's busy share now, as (percent, seconds it spans), from the samples every process keeps in the slot
    directory, adding this one. (None, None) when the ticks cannot be read; (None, seconds) when no kept sample is
    BUSY_MINIMUM old yet, unless wait, for a one-off reading, which then waits BUSY_QUICK seconds for its own."""
    current, at = ticks(), now()
    if current is None:
        return None, None
    path = Path(directory) / CPU_SAMPLES
    try:
        kept = [sample for sample in json.loads(path.read_text())
                if isinstance(sample, list) and len(sample) == 1 + CPU_STATES
                and all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in sample)
                and at - BUSY_KEEP <= sample[0] <= at]
    except (OSError, ValueError, TypeError):
        kept = []
    if not kept or at - max(sample[0] for sample in kept) >= 1:
        # Written whole, under a name of its own, so that a reader never meets half a file; a lost race loses a sample.
        with contextlib.suppress(OSError):
            Path(directory).mkdir(parents=True, exist_ok=True)
            partial = path.with_name(f"{CPU_SAMPLES}.{os.getpid()}.{threading.get_ident()}")
            partial.write_text(json.dumps(sorted(kept + [[round(at, 3), *current]])))
            os.replace(partial, path)
    old = [sample for sample in kept if at - sample[0] >= BUSY_MINIMUM]
    full = [sample for sample in old if at - sample[0] >= BUSY_WINDOW]
    base = max(full) if full else min(old) if old else None
    if base is None and wait:
        sleep(BUSY_QUICK)
        later, after = ticks(), now()
        if later is None:
            return None, None
        return busy_share(current, later), round(after - at, 1)
    if base is None:
        return None, round(at - min(sample[0] for sample in kept), 1) if kept else 0.0
    return busy_share(base[1:], current), round(at - base[0], 1)


# An exclusive admission settles before its command starts (vocaro/queuekeeper#121). Admitted the moment the work ahead
# of it ends, it holds every slot of a Mac whose trailing readings still count that work: on 2026-11-13 mpm's timed
# gates read a one-minute load of 25.9 against their limit of 13.5 with nothing admitted running, and voided. So it
# waits, holding the slots, while the CPU's busy share is at or above QUIET_LIMIT percent, for at most QUIET_SECONDS,
# and then starts its command however busy the Mac reads. HEAVY_QUIET_LIMIT and HEAVY_QUIET_SECONDS override them
# for one process; HEAVY_QUIET_SECONDS=0 starts at once. They are not keys of the limits file, which an older toolkit
# would refuse as misconfigured.
QUIET_LIMIT, QUIET_SECONDS = 25.0, 300.0
# What an exclusive command is told of the Mac as it starts, so that a project's check judges admission's reading
# rather than taking its own: the CPU's busy share in percent and the seconds that reading spans, the one-minute load,
# the seconds it waited for the Mac to settle, and whether the Mac read quiet (1) or not (0: it ran at the bound, or
# the busy share could not be read). A value that could not be read is left unset.
HOST_BUSY, HOST_BUSY_SECONDS, HOST_LOAD, HOST_SETTLED, HOST_QUIET = (
    "QUEUEKEEPER_HOST_BUSY", "QUEUEKEEPER_HOST_BUSY_SECONDS", "QUEUEKEEPER_HOST_LOAD", "QUEUEKEEPER_HOST_SETTLED_SECONDS",
    "QUEUEKEEPER_HOST_QUIET")
HOST_READING = (HOST_BUSY, HOST_BUSY_SECONDS, HOST_LOAD, HOST_SETTLED, HOST_QUIET)


# The host's admission limits, beside its admission log: the limits its owner chose for this host (#49).
LIMITS = "admission-limits.json"
# Each limit by its key in the limits file: the environment variable that overrides it, whether it counts
# slots, and its default and how the default is reached, given the cores and the guaranteed slots in force.
LIMIT_KEYS = {
    "slots": ("HEAVY_SLOTS", True, lambda cores, slots: (3, "3")),
    "slotsMax": ("HEAVY_SLOTS_MAX", True, lambda cores, slots: (max(slots, cores // 2), "half the cores, at least HEAVY_SLOTS")),
    "busyLimit": ("HEAVY_BUSY_LIMIT", False, lambda cores, slots: (75.0, "75, percent of the CPU's time")),
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


def busy_limit():
    """The CPU's busy share, in percent, at or above which the host has no spare capacity: HEAVY_BUSY_LIMIT, or the
    host's limits file, or 75."""
    return float(limits()["busyLimit"][0])


def limit_source(source, path=None):
    """Where a Mac's admission limit comes from, in words, given its source as limits gives it or as
    offload_host.limits gives the second Mac's: the limit's environment variable, the default (a source that
    begins with "default"), or the path of the Mac's limits file. path is that Mac's limits file, which a default
    names as where to set the limit. None when the source is unknown, as from an older toolkit
    (vocaro/queuekeeper#29)."""
    if not isinstance(source, str) or not source:
        return None
    if source in {variable for variable, _counts, _default in LIMIT_KEYS.values()}:
        return "set in the environment"
    if source.startswith("default"):
        return f"the default, since {path} does not set it" if path else "the default"
    return f"set in {source}"


def limit_words(key, value, source=None, path=None, mac="this Mac"):
    """A limit as a line quotes it: its variable and value, and whose limit it is and where it comes from
    (limit_source), so that a reader can tell it belongs to the Mac and which file or setting to look at without
    reading the guides. On 2026-11-12 "at or above HEAVY_BUSY_LIMIT=70%" was read as a project's limit; it was the
    Mac's, from its limits file, and not the default the guides quote (vocaro/queuekeeper#29)."""
    variable = LIMIT_KEYS[key][0]
    shown = f"{value:g}%" if key == "busyLimit" else f"{value:g}"
    whence = limit_source(source, path)
    return f"{variable}={shown} ({mac}'s limit{', ' + whence if whence else ''})"


def limits_words(found=None, mac="this Mac"):
    """limit_words for each limit in force on this Mac, by key, from limits' answer (found) or a new one, saying it is
    mac's."""
    found = limits() if found is None else found
    path = limits_path()
    return {key: limit_words(key, value, source, path, mac) for key, (value, source) in found.items()}


def limits_main(argv=None, out=print):
    """qk admissions limits: the admission limits in force for a process started like this one, and their sources."""
    parser = argparse.ArgumentParser(prog="qk admissions limits", description="Print the heavy-work admission "
        "limits in force here and where each comes from: its environment variable, the host's limits file or the "
        "default (doc/guides/shared-host.md, Heavy-work admission).")
    parser.parse_args(argv)
    try:
        found = limits()
    except ValueError as error:
        print(f"queuekeeper: {error}", file=sys.stderr)
        return 2
    out(f"{os.cpu_count() or 1} cores; limits file {limits_path()}")
    for key, (value, source) in found.items():
        out(f"{LIMIT_KEYS[key][0]}={value:g}  ({key}; {source})")
    return 0


def pressure(limit=None):
    """Why the host has no spare capacity now, by the CPU's busy share and memory pressure, as the refusal's kind,
    cpu or memory, and its reason, which names the limit and its value beside what was read; None when it has.
    The merge queue waits for it to clear before it runs a gate again that gave no verdict (doc/guides/maintenance.md,
    The merge queue); admission judges its own attempt's reading the same way (Admission._extra_refusal)."""
    said = None
    if limit is None:
        found = limits()
        limit, said = float(found["busyLimit"][0]), limits_words(found)["busyLimit"]
    return judged(cpu_busy(slot_directory(), wait=True)[0], limit, memory_level, said)


def judged(busy, limit, level, said=None):
    """pressure's answer for a reading of a Mac: its CPU's busy share in percent, None when unavailable, the limit
    of that Mac, and level, a function that reads its memory pressure level, called only when the share is below
    the limit. said quotes the limit with whose it is and where it comes from (limit_words); without it the reason
    quotes the bare limit. The merge queue judges the second Mac's reading by it too (remote.pressure_there)."""
    said = f"HEAVY_BUSY_LIMIT={limit:g}%" if said is None else said
    if busy is None:
        return "cpu", f"the CPU's busy share, which {said} bounds, is unavailable"
    if busy >= limit:
        return "cpu", f"the CPU is {busy:.0f}% busy, at or above {said}"
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
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix=".queuekeeper-guard-", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(state, stream)
            stream.write("\n")
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


# Seconds a retained request holds later incompatible work back after its process last asked,
# including the gap between local waits while the second Mac is offered the work (Place).
PLACE_SECONDS = 180
# All waiting requests, in the slot directory, changed only under its coordination lock.
QUEUE = "exclusive-queue.json"
# Seconds a caller waits for its place's keeper to end (Place.kept).
KEEPER_JOIN = 5


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


# Each waiting request's observation record, for `qk admissions now` (vocaro-engineering#342): a file of its own in this
# folder beside the slot files, which only that request writes, from its first refusal until it stops waiting however
# it stops. Nothing in admission reads it: it is no place in the queue, holds nothing back and orders nothing. A request
# rewrites it at most every OBSERVE_SECONDS, and at most every SWEEP_SECONDS one request removes the records of waiters
# proven dead (sweep_waits). offload_host.admissions_now reads them, as WAITS and NOW_SCHEMA there name them.
WAITS, WAIT_SCHEMA, OBSERVE_SECONDS, SWEEP_SECONDS, SWEEP_LIMIT = "admission-waits", 1, 5, 60, 512


def sweep_waits(folder):
    """Remove the observation records of waiters whose process is proven to have ended: by a ps that answered, its PID
    runs no more or runs another process. A record that cannot be read or whose process ps could not check is left.
    One request does it at a time, under a lock it does not wait for, at most every SWEEP_SECONDS and over at most
    SWEEP_LIMIT records; it touches nothing but those records."""
    from . import offload_host
    marker = folder / ".sweep"
    with open(folder / ".sweep.lock", "a+") as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: return
        with contextlib.suppress(FileNotFoundError):
            if time.time() - marker.stat().st_mtime < SWEEP_SECONDS: return
        marker.touch()
        records = {}
        for name in sorted(os.listdir(folder))[:SWEEP_LIMIT]:
            if not name.endswith(".json") or name.startswith("."): continue
            try: record = offload_host.read_json(folder / name, 64 << 10)
            except (OSError, ValueError): continue
            if isinstance(record, dict) and type(record.get("pid")) is int and isinstance(record.get("start"), str):
                records[name] = record
        listed = offload_host.census([record["pid"] for record in records.values()])
        if listed is None: return
        for name, record in records.items():
            if offload_host.process_state(record, listed) == "ended":
                with contextlib.suppress(OSError): (folder / name).unlink()


REQUEST_STARTED = "QUEUEKEEPER_ADMISSION_REQUEST_STARTED"
_request_started = contextvars.ContextVar("admission_request_started", default=None)


def request_started():
    """Arrival on this Mac's clock; dispatch carries elapsed age rather than another Mac's wall clock."""
    value = _request_started.get()
    if value is None:
        try: value = float(os.environ.get(REQUEST_STARTED, "nan"))
        except ValueError: value = float("nan")
    now = time.time()
    return min(value, now) if math.isfinite(value) else now


@contextlib.contextmanager
def request_order(started):
    token = _request_started.set(started)
    try: yield
    finally: _request_started.reset(token)


@contextlib.contextmanager
def child_requests():
    """Keep a dispatch's arrival local to its CLI step, while preserving inherited admission.

    A leaf command can request independent work, including each part of a self lane. Such new
    requests must not reuse their ancestor's arrival. The parent keeps its input until it ends.
    """
    before = os.environ.pop(REQUEST_STARTED, None)
    token = _request_started.set(None)
    try: yield
    finally:
        _request_started.reset(token)
        if before is None: os.environ.pop(REQUEST_STARTED, None)
        else: os.environ[REQUEST_STARTED] = before


class Place:
    """A request's arrival, retained across local attempts between remote offers (remote.overflow).

    Ordinary, exclusive and whole-host requests all keep their arrival order across retries (#360).

    An admission given a place leaves it there when its wait times out; its caller calls leave() once it no longer
    waits for this Mac, admitted or not, and admission removes it when admitted. While the process is away, between
    waits, its place holds nothing back once HEAVY_PLACE_SECONDS have passed since it last asked or last said that
    its work still waits (kept), and a place whose process has ended is dropped, as every queued request is."""

    def __init__(self):
        self.identity = uuid.uuid4().hex
        self.started = request_started()

    def still_waiting(self):
        """Say that the work still waits for this Mac though its process is not asking now: the place goes on holding
        later work back as if it had just asked. `asked` keeps when it last did, for `qk admissions now`; its next
        attempt removes it. A place no longer in the queue, admitted or left, is not put back."""
        directory = slot_directory()
        if not (directory / QUEUE).exists():
            return
        with (directory / "coordination.lock").open("a+") as bridge:
            fcntl.flock(bridge, fcntl.LOCK_EX)
            waiters = queued_requests(directory / QUEUE)
            mine = next((waiter for waiter in waiters if waiter["identity"] == self.identity), None)
            if mine is not None:
                if "seen" in mine: mine.setdefault("asked", mine["seen"])
                mine["seen"] = time.time()
                record_queue(directory / QUEUE, waiters)

    @contextlib.contextmanager
    def kept(self, unplaced, every=None):
        """Keep the place holding later work back while its process is away from this Mac's queue and unplaced()
        says that no Mac has taken the work: from a thread, every third of HEAVY_PLACE_SECONDS, it asks unplaced
        and then says the work still waits (still_waiting), until the block ends. It stops for good the first time
        unplaced does not say True, so that work the second Mac took and runs stops holding this Mac back
        HEAVY_PLACE_SECONDS later, as does work whose placement cannot be told. Without this an offer to the second
        Mac that kept a whole-host gate away for longer than HEAVY_PLACE_SECONDS let a later gate take the slots as
        they were freed (vocaro/queuekeeper#160). Nothing here may stop the work: a failure ends the keeping."""
        if every is None:
            try: every = float(os.environ.get("HEAVY_PLACE_SECONDS", str(PLACE_SECONDS))) / 3
            except ValueError: every = PLACE_SECONDS / 3
        done = threading.Event()

        def keep():
            with contextlib.suppress(Exception):
                while not done.wait(every):
                    if unplaced() is not True: return
                    self.still_waiting()
        keeper = threading.Thread(target=keep, daemon=True)
        if every > 0: keeper.start()
        try: yield
        finally:
            done.set()
            # A keeper still waiting for the coordination lock changes nothing once the place has left the queue.
            if keeper.is_alive(): keeper.join(KEEPER_JOIN)

    def leave(self):
        directory = slot_directory()
        if not (directory / QUEUE).exists():
            return
        with (directory / "coordination.lock").open("a+") as bridge:
            fcntl.flock(bridge, fcntl.LOCK_EX)
            waiters = queued_requests(directory / QUEUE)
            if any(waiter["identity"] == self.identity for waiter in waiters):
                record_queue(directory / QUEUE, [waiter for waiter in waiters if waiter["identity"] != self.identity])


class AdmissionRecordError(RuntimeError):
    """An admission could not record its opening, before any command started."""


class Admission:
    def __init__(self, label, exclusive=False, timeout=5400, model=False, whole_host=False, place=None, placement=None,
                 supervised=False):
        self.label, self.exclusive, self.timeout = label, exclusive, timeout
        # Whether this process starts every command of the admission through processes.supervise, as qk admit, qk run
        # and the resource CLI do, so that supervision records each before it starts it: then an entry whose record
        # names no command ended before its first, and recovery checks its owner's process group instead (#315). A
        # caller that runs its own commands, which may record none, leaves it False.
        self.supervised = supervised
        # Why the work is asking this Mac, for its admitted and withdrawn lines, or None: whether queuekeeper offered it to
        # the second Mac first and why it is here (offered, because), or the Mac that sent it (dispatchedFrom).
        # `qk admissions usage` counts it.
        self.placement = placement
        # Exclusive work holds the host, the model with it.
        self.model = model or exclusive
        # A whole-host lane, a gate that runs a project's suites side by side, takes every guaranteed slot, so
        # that a second such lane never runs beside it on this Mac; a lighter heavy lane still shares, beyond
        # them. Exclusive work holds every slot already (vocaro-engineering#168).
        self.whole_host = bool(whole_host) and not exclusive
        self.kind = "exclusive" if exclusive else "model" if model else "heavy"
        # Where the work comes from, which its guard entry and wait record carry for `qk admissions now`.
        self.origin = work_origin()
        # When the request began to wait, and when it was admitted, by the monotonic clock.
        self.started, self.admitted = time.monotonic(), None
        self.held_seconds = None
        self.directory = slot_directory()
        # Each from its environment variable, the host's limits file or the default (#49).
        found = limits()
        self.slots, self.maximum = found["slots"][0], found["slotsMax"][0]
        self.busy_limit = float(found["busyLimit"][0])
        # Each limit as a refusal quotes it: whose it is and where it comes from (vocaro/queuekeeper#29). The Mac is
        # named, not "this Mac": the second Mac's refusal is relayed to the Mac that offered it the work.
        from . import offload_host
        self.said = limits_words(found, offload_host.mac_name())
        # The CPU's busy share, read over about BUSY_WINDOW seconds, shows a new job only once it has started work.
        self.settle = float(found["settleSeconds"][0])
        self.poll = float(os.environ.get("HEAVY_POLL_SECONDS", "1"))
        # How long a dead or uncertain entry that recovery left waits before it is checked again.
        self.recheck = float(os.environ.get("HEAVY_RECOVER_SECONDS", "60"))
        # How long a queued request holds later work back after it last asked (_holds).
        self.hold = float(os.environ.get("HEAVY_PLACE_SECONDS", str(PLACE_SECONDS)))
        # How quiet the Mac must read before an exclusive command starts, and how long it may wait for that (#121).
        self.quiet_limit = float(os.environ.get("HEAVY_QUIET_LIMIT", str(QUIET_LIMIT)))
        self.quiet_seconds = float(os.environ.get("HEAVY_QUIET_SECONDS", str(QUIET_SECONDS)))
        # What the settle found, for the ended line, and the reading variables it replaced, for the end to restore.
        self.settled, self.told = None, None
        if self.slots < 1 or self.maximum < self.slots or self.settle < 0 or self.poll <= 0 or timeout < 0 \
                or self.recheck < 0 or self.hold < 0 or self.quiet_limit < 0 or self.quiet_seconds < 0:
            raise ValueError("Invalid resource limits")
        # Why the latest attempt was refused: its kind, which the log counts (#49), and the waiting message,
        # which names the limit that refused it and its value beside what the attempt read.
        # guard: the guard is unreadable or not in admission's format and needs inspection; stranded: a dead or
        # uncertain entry recovery left is counted among the busy slots, or as the model's holder (#89);
        # exclusive: exclusive work holds the host or is queued ahead; wholeHost: a whole-host request queued ahead
        # keeps the guaranteed slots, one of them free; model: another run holds the model; slots: every slot the request may take is busy; settle, cpu or memory: the settling time, the
        # CPU's busy share or memory pressure withholds a slot beyond the guaranteed ones; an older toolkit's
        # load, the load average that once did. An older toolkit's log
        # lines also count legacy, a gate holding StudyWright's former host lock, which no longer refuses (#25).
        self.refusal = self.waiting = None
        # The dead or uncertain entries recovery left, by identity: when they were checked, and why they stayed.
        self.unrecovered = {}
        # The identities of the entries this request's attempts freed, their owners ended or unknown (free_holder).
        self.reclaimed = []
        # The latest attempt's stranded entries, those recovery left: by identity, the entry and why it stayed;
        # then, as counted, each one's identity, entry, why, the slots it is counted as and whether the model.
        self.stranded, self.counted = {}, []
        # For the log line: the refused attempts by kind, in the order the kinds first refused it; the holders
        # when it asked; the first, last and highest one-minute load its attempts read, and so of the CPU's busy
        # share; the latest attempt's busy share and the seconds it spans; and the highest memory pressure level
        # read beside its refusals.
        self.refusals, self.holders, self.loads, self.memory = Counter(), None, None, None
        self.busies, self.busy = None, (None, None)
        self.guard = guard_path(os.environ)
        # All requests waiting for admission, first come first.
        self.queue = self.directory / QUEUE
        self.identity = uuid.uuid4().hex
        # The identity of its queued record: its place's, kept across this process's successive admissions, or its own.
        self.place = place
        self.ordered = place.started if place is not None else request_started()
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
        # This request's observation record while it waits (WAITS), when it last wrote it by the monotonic clock, and
        # whether writing it failed, which ends it for this request and changes nothing else.
        self.wait_record = self.directory / WAITS / f"{self.identity}.json"
        self.observed_at, self.unobserved, self.owner_start = None, False, None

    def _observe(self):
        """Publish what this waiting request waits for, for `qk admissions now`: at its first refusal, and then at most
        every OBSERVE_SECONDS. Never a reason to change admission: a failure is said once and ends the record."""
        now = time.monotonic()
        if self.unobserved or (self.observed_at is not None and now - self.observed_at < OBSERVE_SECONDS): return
        first = self.observed_at is None
        self.observed_at = now
        staged = None
        try:
            folder = self.wait_record.parent
            folder.mkdir(mode=0o700, exist_ok=True)
            if self.owner_start is None:
                self.owner_start = process_start(os.getpid())
                if self.owner_start is None: raise OSError("the waiting process's identity cannot be read")
            waited = now - self.started
            record = {"schemaVersion": WAIT_SCHEMA, "identity": self.queued_as, "request": self.identity,
                      "pid": os.getpid(), "start": self.owner_start, "label": self.label[:LABEL_LIMIT],
                      "queueKind": "exclusive" if self.exclusive else "wholeHost" if self.whole_host else "ordinary",
                      "model": self.model, "queuedAt": self.ordered, "startedAt": time.time() - waited,
                      "sampledAt": time.time(), "waited": round(waited, 3), "refusal": self.refusal,
                      "reason": self.waiting, **({"origin": self.origin} if self.origin else {})}
            with tempfile.NamedTemporaryFile("w", dir=folder, prefix=f".{self.identity}.", delete=False) as stream:
                staged = Path(stream.name)
                json.dump(record, stream)
            staged.replace(self.wait_record)
            staged = None
            if first:
                # Another waiter's crash is no failure of this record.
                with contextlib.suppress(Exception): sweep_waits(folder)
        except Exception as error:
            self.unobserved = True
            with contextlib.suppress(Exception):
                print(f"heavy: what this request waits for was not recorded for `qk admissions now`: {error}",
                      file=sys.stderr, flush=True)
        finally:
            if staged is not None:
                with contextlib.suppress(OSError): staged.unlink()

    def _unobserve(self):
        """This request no longer waits, however its wait ended: its observation record goes."""
        if self.observed_at is None: return
        with contextlib.suppress(OSError): self.wait_record.unlink(missing_ok=True)

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

        An entry whose owner admission cannot confirm running, its process ended or unknown (liveness), is freed here
        at once, what is left of it stopped (free_holder): uncertainty about a holder never keeps a slot held (the
        owner's decision, 2026-11-13, vocaro/queuekeeper#23). An uncertain entry whose owner still runs is recovered
        here, as `qk admissions recover` would recover it. One recovery leaves is checked again after
        HEAVY_RECOVER_SECONDS. Until then it stays in self.stranded, and the attempt counts it as the slots it held
        (#89)."""
        self.stranded = {}
        if not self.guard.exists():
            return {"vocaroEngineering": 1, "owners": {}}
        value = self._recorded()
        if value is None:
            self.refusal, self.waiting = "guard", (f"the guard {self.guard} is unreadable or not in admission's format "
                                                   "and requires inspection; `python3 scripts/qk admissions recover` lists it")
            return None
        for identity, owner in list(value["owners"].items()):
            condition, how = liveness(owner)
            if condition != "running":
                line = free_holder(self.guard, self.directory, self.maximum, value, identity, condition, how,
                                   by={"identity": self.identity, "pid": os.getpid(), "label": self.label[:LABEL_LIMIT]})
                self.unrecovered.pop(identity, None)
                self.reclaimed.append(identity)
                print(f"heavy: {reclaimed_words(line)}", file=sys.stderr, flush=True)
                continue
            if not owner.get("uncertain"):
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
        return "; ".join(described) + (f"; `python3 scripts/qk admissions recover` lists "
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
        """Whether an earlier request holds this one back. Ordinary requests hold later exclusive and
        whole-host requests back, while ordinary requests can share. A retained place stops holding
        anything back once it has neither asked nor been said to wait still (Place.kept) within HEAVY_PLACE_SECONDS."""
        if waiter.get("ordinary") and not (self.exclusive or self.whole_host):
            return False
        seen = waiter.get("seen")
        if "seen" not in waiter and not (waiter.get("wholeHost") or waiter.get("ordinary")):
            return True  # Older exclusive records have no freshness field.
        return isinstance(seen, (int, float)) and not isinstance(seen, bool) and time.time() - seen < self.hold

    def _log(self, event, **fields):
        """Append this request's line to the admission log. The caller holds the coordination lock."""
        append_log({"event": event, "identity": self.identity, "pid": os.getpid(), "kind": self.kind,
                    "label": self.label[:LABEL_LIMIT], **fields})

    def _seen(self):
        """What the request saw, for its admitted or withdrawn line (#49): the holders and the one-minute load
        when it asked, and, once an attempt was refused, why it waited."""
        fields = {"holders": self.holders, "load": self.loads[0] if self.loads else None,
                  "busy": self.busies[0] if self.busies else None,
                  **({"placement": self.placement} if self.placement else {})}
        if self.counted:
            # The stranded entries recovery left, which the latest attempt counted as the slots they held (#89).
            fields["stranded"] = [{"identity": identity, "pid": owner["pid"], "label": str(owner.get("label"))[:LABEL_LIMIT],
                                   "uncertain": bool(owner.get("uncertain")), "why": why, "slots": slots, "model": model}
                                  for identity, owner, why, slots, model in self.counted]
        if self.reclaimed:
            # The entries its attempts freed, each with a `reclaimed` line of its own (vocaro/queuekeeper#23).
            fields["reclaimed"] = list(self.reclaimed)
        if self.refusals:
            last, peak = self.loads[1:] if self.loads else (None, None)
            busy_last, busy_peak = self.busies[1:] if self.busies else (None, None)
            fields["wait"] = {"refusals": dict(self.refusals), "first": next(iter(self.refusals)), "last": self.refusal,
                              "load": {"last": last, "max": peak}, "busy": {"last": busy_last, "max": busy_peak},
                              "memory": self.memory}
        return fields

    def _count(self):
        """Count the refusal of the attempt just made by its kind, and read the memory pressure beside it."""
        self.refusals[self.refusal] += 1
        level = memory_level()
        if level is not None: self.memory = max(level, self.memory or 0)

    def _withdraw(self, error):
        """Log that this request stopped waiting without admission, and take it out of the queue if it is there."""
        from queuekeeper.processes import Interrupted
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
            return "slots", f"{self.said['slotsMax']} allows none beyond them"
        settling = time.time() - last_admission(self.directory, self.maximum)
        if settling < self.settle:
            return "settle", (f"the CPU does not yet show the job admitted {int(settling)}s ago, "
                              f"within HEAVY_SETTLE_SECONDS={self.settle:g}")
        busy, seconds = self.busy
        if busy is None and seconds is not None:
            # The samples kept do not span a reading yet: the next attempts' will.
            return "settle", f"the CPU has been read over {seconds:g}s, short of the {BUSY_MINIMUM:g}s a reading spans"
        return judged(busy, self.busy_limit, memory_level, self.said["busyLimit"])

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
            refusal = "slots", (f"so is every slot up to {self.said['slotsMax']}" if kept is None else
                                f"every slot beyond them up to {self.said['slotsMax']} is busy")
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

    def _whole_host_holder(self, state):
        """The whole-host lane running on the host, if any: a live guard owner marked wholeHost."""
        return next((owner for identity, owner in state["owners"].items()
                     if owner.get("wholeHost") and identity not in self.stranded), None)

    def _held_back(self, state):
        """Refuse the attempt at the model or a slot. Exclusive work holds both while its guard entry stands,
        so whatever refused the request there, it waits behind that work, which the message names; so does
        heavy work behind a running whole-host lane, which holds the slots beyond the guaranteed ones too (#196)."""
        holder = next((owner for identity, owner in state["owners"].items()
                       if owner.get("exclusive") and identity not in self.stranded), None)
        lane = None if holder is not None or self.exclusive or self.whole_host else self._whole_host_holder(state)
        if holder is not None:
            self.refusal, self.waiting = "exclusive", ("it waits behind exclusive work holding the host: "
                                                       f"{str(holder.get('label') or 'an unnamed owner')[:LABEL_LIMIT]}")
        elif lane is not None:
            self.refusal, self.waiting = "wholeHost", ("it waits behind a whole-host lane holding the host: "
                                                       f"{str(lane.get('label') or 'an unnamed owner')[:LABEL_LIMIT]}")
        else:
            self._refused_beside_stranded()
        return False

    def _holding(self):
        """What holds what the latest refused attempt wanted, read now: {label of a running admission: its locks}, and
        the locks that are busy though their files name no running admission, such as a caller of the old slot API or
        a process that outlived its entry. Probed and let go at once, under the coordination lock (_extra_slot)."""
        recorded = self._recorded()
        running_now = {identity: owner for identity, owner in (recorded["owners"] if recorded else {}).items()
                       if identity not in self.stranded and not owner.get("uncertain") and running(owner)}
        held, unverified = {}, []
        with (self.directory / "coordination.lock").open("a+") as bridge:
            fcntl.flock(bridge, fcntl.LOCK_EX)
            for lock in ["model"] if self.refusal == "model" else [f"slot{slot}" for slot in range(self.maximum)]:
                try:
                    fd = os.open(self.directory / lock, os.O_RDWR)
                except FileNotFoundError:
                    continue
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    written = os.pread(fd, 4096, 0).decode(errors="replace").split(" ", 2)
                    owner = running_now.get(written[1]) if len(written) > 1 else None
                    if owner is None: unverified.append(lock)
                    else: held.setdefault(str(owner.get("label") or "an unnamed owner")[:LABEL_LIMIT], []).append(lock)
                finally:
                    os.close(fd)
        return held, unverified

    def _gave_up(self, waited):
        """What a request that stops waiting says stopped it: which Mac, what held it, the time waited and that nothing
        ran (#270). Work known to be running only made it wait, so this says the queue was busy and names that work. The
        orphan wording stays for what no running admission vouches for: a guard admission cannot read, a stranded entry
        counted as the slots it held, or a slot or the model busy with no running admission named in its file."""
        from . import offload_host
        mac, held, unverified = offload_host.mac_name(), {}, []
        if self.refusal not in ("guard", "stranded", "exclusive", "wholeHost"):
            try:
                held, unverified = self._holding()
            except OSError as error:
                # A request that stops waiting fails as it would have: `qk run --if-free` tells a busy host by it.
                unverified = [f"the locks, unread: {error}"]
        if self.refusal in ("guard", "stranded") or unverified:
            busy = f"; busy with no running admission named: {', '.join(unverified)}" if unverified else ""
            return (f"Resource queue busy or an orphan requires inspection ({self.waiting}); no lock was reclaimed{busy}; "
                    f"waited {waited}s on {mac}; nothing ran")
        # The model's holder is in the waiting message already.
        named = ("; held by " + ", ".join(f"{label} ({', '.join(locks)})" for label, locks in held.items())
                 if held and self.refusal != "model" else "")
        return f"Resource queue busy on {mac}: {self.waiting}{named}; waited {waited}s; nothing ran"

    def _attempt(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        with (self.directory / "coordination.lock").open("a+") as bridge:
            fcntl.flock(bridge, fcntl.LOCK_EX)
            load = one_minute_load()
            if load is not None:
                self.loads = [load, load, load] if self.loads is None else [self.loads[0], load, max(self.loads[2], load)]
            self.busy = cpu_busy(self.directory)
            if self.busy[0] is not None:
                busy = self.busy[0]
                self.busies = [busy, busy, busy] if self.busies is None else [self.busies[0], busy, max(self.busies[2], busy)]
            waiters = self._waiters()
            mine = next((waiter for waiter in waiters if waiter["identity"] == self.queued_as), None)
            if mine is None:
                born = process_start(os.getpid())
                if born is None:
                    raise RuntimeError("Cannot establish the admission owner's process identity")
                mine = {"identity": self.queued_as, "pid": os.getpid(), "start": born,
                        "label": self.label[:LABEL_LIMIT], "queuedAt": self.ordered,
                        **({"wholeHost": True} if self.whole_host else {}),
                        **({"ordinary": True} if not (self.exclusive or self.whole_host) else {})}
                # Insert before a later dated arrival; undated legacy records retain their existing order.
                index = next((index for index, waiter in enumerate(waiters)
                              if waiter.get("queuedAt", float("-inf")) > self.ordered), len(waiters))
                waiters.insert(index, mine)
            self.queued = True
            # It asks now: what its caller said while it was away (Place.still_waiting) is over.
            mine.pop("asked", None)
            mine["seen"] = time.time()
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
            if ahead is not None and (len(reserved) >= self.maximum if ahead.get("ordinary") else
                                      any(slot < self.slots for slot in reserved) if ahead.get("wholeHost") else reserved):
                ahead = None
            # A whole-host request queued ahead keeps the guaranteed slots for itself, so that they drain toward it
            # while lighter heavy work goes on beyond them; exclusive and whole-host requests wait their turn (#174).
            kept = ahead if ahead is not None and ahead.get("wholeHost") and not (self.exclusive or self.whole_host) else None
            if ahead is not None and kept is None:
                # Work already admitted runs on; nothing new starts before the request queued ahead.
                if ahead.get("ordinary"):
                    self.refusal, self.waiting = "slots", f"it waits behind ordinary work queued ahead of it: {ahead['label']}"
                elif ahead.get("wholeHost"):
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
                # A running whole-host lane holds the slots beyond the guaranteed ones too: a gate's load dips
                # between its phases, which is no spare capacity for another heavy job (#196).
                if taken is None and not self.exclusive and not self.whole_host and self._whole_host_holder(state) is None:
                    taken = self._extra_slot(reserved, kept)
                if taken is None:
                    busy = wanted - len(self.descriptors)
                    if self.exclusive:
                        self.refusal, self.waiting = "slots", (f"exclusive work needs every slot up to "
                                                               f"{self.said['slotsMax']} free, and {busy} "
                                                               f"{'is' if busy == 1 else 'are'} busy")
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
            try:
                # Publish a complete opening record before the guard. A write failure must leave no admission,
                # just as a kill before this publication must leave no reservation to recover (#315, #336).
                self._open_record(born)
                state["owners"][self.identity] = {"pid": os.getpid(), "start": born, "label": self.label,
                                                   "exclusive": self.exclusive, "model": self.model,
                                                   "admitted": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                                   **({"wholeHost": True} if self.whole_host else {}),
                                                   **({"origin": self.origin} if self.origin else {})}
                publish_guard(self.guard, state)
            except BaseException:
                # An interrupt can follow the guard's atomic rename. Keep the opening record if the guard
                # refers to it or cannot be read, so recovery can still prove that no command started.
                with contextlib.suppress(OSError):
                    published = self._recorded()
                    if not self.guard.exists() or (published is not None and self.identity not in published["owners"]):
                        self.process_record.unlink(missing_ok=True)
                self._release()
                raise
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
        from queuekeeper.processes import Interrupted, requested
        started = self.started = time.monotonic()
        reported, said_progress, progress_at, progress_next = None, None, 0, 0
        try:
            while not self._attempt():
                self._count()
                lane_wait.waiting(lane_wait.ADMISSION, self.waiting, stranded=self.refusal == "stranded")
                self._observe()
                # Nothing is published yet but the request's place in the queue,
                # so a stop or a timeout leaves nothing to release once it withdraws.
                if requested() is not None:
                    raise Interrupted(requested(), "while waiting for admission")
                if hasattr(self, "guard") and time.monotonic() >= progress_next:
                    from . import lane_progress
                    progress_next = time.monotonic() + 5
                    recorded = self._recorded()
                    observations = lane_progress.blockers(self.guard, recorded["owners"] if recorded else {})
                    shown = [lane_progress.words(item) for item in observations]
                    signature = [(r.get("run"), r.get("state")) for r in observations]
                    if shown and (signature != said_progress or time.monotonic() - progress_at >= 30):
                        print("heavy: running work: " + "; ".join(shown), file=sys.stderr, flush=True)
                        progress_at = time.monotonic()
                    said_progress = signature
                elapsed = time.monotonic() - started
                waited = int(elapsed)
                if reported is None or waited - reported >= 300:
                    print(f"heavy: waiting ({waited}s): {self.waiting}", file=sys.stderr, flush=True)
                    reported = waited
                if elapsed >= self.timeout:
                    raise TimeoutError(self._gave_up(waited))
                time.sleep(self.poll)
        except BaseException as error:
            try:
                self._withdraw(error)
            except Exception:
                # A queued request that cannot leave the queue says so. The log alone never changes
                # how a request fails: `qk run --if-free` tells a busy host by its TimeoutError.
                if self.queued: raise
            raise
        finally:
            # Admitted, timed out, stopped or failed: it waits no more.
            self._unobserve()
        _held[self.identity] = self
        lane_wait.running()
        print(f"heavy: slot {','.join(str(slot) for slot, _fd in self.descriptors)}"
              f"{' and the model' if self.held_model is not None else ''} after {int(time.monotonic() - started)}s: "
              f"{self.label[:LABEL_LIMIT]}", file=sys.stderr, flush=True)
        if self.counted:
            print(f"heavy: admitted beside a stranded entry: {self._stranded_text()}", file=sys.stderr, flush=True)
        if self.exclusive:
            try:
                self._quiet()
            except BaseException as error:
                # Admitted, it holds every slot, and no command has started: a stop while the Mac settles releases
                # them as a stopped run does, and anything else as a command that could not start.
                from queuekeeper.processes import Interrupted, NotStarted
                self.__exit__(type(error) if isinstance(error, Interrupted) else NotStarted, error, error.__traceback__)
                raise
        return self

    def _quiet(self):
        """Wait, holding every slot, until the CPU's busy share reads under HEAVY_QUIET_LIMIT, for at most
        HEAVY_QUIET_SECONDS, and then tell the command what admission read of the Mac (HOST_READING, #121). A busy
        share that cannot be read starts the command at once. HEAVY_QUIET_SECONDS=0 waits for nothing and says
        nothing, though the command is still told the reading."""
        from queuekeeper.processes import Interrupted, requested
        began, said = time.monotonic(), False
        while True:
            busy, seconds = cpu_busy(self.directory)
            waited = round(time.monotonic() - began, 1)
            quiet = None if busy is None and seconds is None else busy is not None and busy < self.quiet_limit
            if quiet is None or quiet or waited >= self.quiet_seconds:
                break
            if requested() is not None:
                raise Interrupted(requested(), "while the Mac settled for exclusive work")
            if not said:
                shown = "has been read too briefly" if busy is None else f"is {busy:.0f}% busy"
                print(f"heavy: waiting for the Mac to settle: the CPU {shown}, at or above HEAVY_QUIET_LIMIT="
                      f"{self.quiet_limit:g}%; it starts within HEAVY_QUIET_SECONDS={self.quiet_seconds:g} either way",
                      file=sys.stderr, flush=True)
                said = True
            time.sleep(self.poll)
        load = one_minute_load()
        if self.quiet_seconds > 0:
            self.settled = {"seconds": waited, "busy": busy, "quiet": bool(quiet)}
        if self.quiet_seconds > 0 and not quiet:
            why = ("the CPU's busy share cannot be read" if quiet is None else
                   f"the CPU is still {busy:.0f}% busy after {waited:g}s, at or above HEAVY_QUIET_LIMIT={self.quiet_limit:g}%"
                   if busy is not None else f"the CPU has not been read over {BUSY_MINIMUM:g}s after {waited:g}s")
            print(f"heavy: starting without a quiet Mac: {why}", file=sys.stderr, flush=True)
        elif said:
            print(f"heavy: the Mac settled after {waited:g}s: the CPU is {busy:.0f}% busy", file=sys.stderr, flush=True)
        self.told = {name: os.environ.get(name) for name in HOST_READING}
        reading = {HOST_BUSY: busy, HOST_BUSY_SECONDS: seconds if busy is not None else None, HOST_LOAD: load,
                   HOST_SETTLED: waited, HOST_QUIET: int(bool(quiet))}
        for name, value in reading.items():
            if value is None: os.environ.pop(name, None)
            else: os.environ[name] = f"{value:g}"

    def _open_record(self, born):
        """Begin the process record with its admission line, before the guard entry is published: the owner, its
        process group and session, the ancestors it was started under, and whether this process supervises every
        command it starts (supervised). Recovery of an entry whose record names no command checks the owner's group,
        and its session where the group leads it, for anything started since the owner, its ancestors aside (census).
        If it cannot be written, admission fails before publishing the guard. It reads ps itself,
        not processes.snapshot, which supervision's samples share."""
        try:
            listing = subprocess.run(["ps", "-axo", "pid=,ppid=,lstart="], capture_output=True, text=True, check=True)
            listed = {int(parts[0]): (int(parts[1]), " ".join(parts[2:])) for parts in
                      (line.split() for line in listing.stdout.splitlines()) if len(parts) > 2}
            if os.getpid() not in listed:
                raise ValueError("ps did not list the admission owner")
            ancestors, parent = [], os.getppid()
            while parent > 1 and parent in listed and len(ancestors) < 64:
                ancestors.append([parent, listed[parent][1]])
                parent = listed[parent][0]
            line = {"admission": self.identity, "owner": [os.getpid(), born], "group": os.getpgid(0),
                    "session": os.getsid(0), "ancestors": ancestors, "supervised": self.supervised}
            publish_guard(self.process_record, line)
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            raise AdmissionRecordError(f"Cannot write the admission's opening process record; nothing ran: {error}") from error

    def observe(self, line):
        """Append a line to this admission's process record: what supervision in this process saw of a
        command (#61). processes.supervise writes it, and so does a caller that supervises its own
        commands, as StudyWright's push keeper does: a dictionary with the command's own key as
        `command`, and `argv` before it starts, `root` and its `spaces` once started, the `processes`
        ([PID, start]) and `spaces` each sample sees first, `closed` updates ([space, close time or
        None if used again]), or `settled` once its end is confirmed.
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
        for name, value in (self.told or {}).items():
            if value is None: os.environ.pop(name, None)
            else: os.environ[name] = value
        self.told = None
        with (self.directory / "coordination.lock").open("a+") as bridge:
            fcntl.flock(bridge, fcntl.LOCK_EX)
            slots = [slot for slot, _fd in self.descriptors]
            self._release()
            # An exception may leave unobserved children. Retain the guard for inspection, unless
            # nothing is left to inspect: the command could not start (a mistyped name must not
            # stop the host), or a stop or a timeout saw the command and every observed descendant end.
            outcome = ending(kind)
            self._settle(release=outcome != "uncertain")
            self.held_seconds = round(time.monotonic() - self.admitted, 3)
            self._log("ended", outcome=outcome, slots=slots, held=self.held_seconds,
                      **({"settled": self.settled} if self.settled is not None else {}))
        return False

    def device_lane_passed(self, keys):
        """Add the verdict after coverage was judged, independently of releasing the slot."""
        if self.held_seconds is None:
            return  # An inherited admission has no timing of its own.
        try:
            with (self.directory / "coordination.lock").open("a+") as bridge:
                fcntl.flock(bridge, fcntl.LOCK_EX)
                self._log("device-lane-passed", held=self.held_seconds, devices=list(keys))
        except OSError as error:
            print(f"heavy: the device lane's timing was not recorded: {error}", file=sys.stderr, flush=True)


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
        field = {"admitted": "waited", "withdrawn": "waited", "ended": "held", "recovered": None, "reclaimed": None,
                 "device-lane-passed": "held"}[entry["event"]]
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


def measured_seconds(label, paths=None, *, devices=None):
    """The median duration of the latest matching admissions, or None without a sample.

    A device lane uses only judged, whole passes for its device plan. A released slot by itself
    carries no verdict and is usable only for the estimate of an ad hoc command.
    """
    entries, _unreadable = read_log(paths)
    matching = [entry for entry in entries if entry["label"] == label[:LABEL_LIMIT]]
    if devices is None:
        held = [entry["held"] for entry in matching if entry["event"] == "ended" and entry["outcome"] == "released"]
    else:
        keys = list(devices)
        held = [entry["held"] for entry in matching
                if entry["event"] == "device-lane-passed" and entry.get("devices") == keys]
    return statistics.median(held[-MEASURED_RUNS:]) if held else None


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
        if isinstance(first.get("busy"), (int, float)): group["busies"].append(first["busy"])
        wait = first.get("wait")
        if wait is None: return
        refusals = sum(wait["refusals"].values())
        for kind, count in wait["refusals"].items():
            group["behind"][kind] += 1
            group["queued"][kind] += first["waited"] * count / refusals
        if wait["load"]["max"] is not None: group["peaks"].append(wait["load"]["max"])
        busy = (wait.get("busy") or {}).get("max")
        if isinstance(busy, (int, float)): group["busyPeaks"].append(busy)

    for lines in requests.values():
        first, end = lines.get("admitted") or lines.get("withdrawn"), lines.get("ended")
        if first is None or (since and first["time"] < since) or (until and first["time"] >= until):
            continue
        # Keyed by kind and label, and by kind and None for the kind as a whole.
        for key in [(first["kind"], None), (first["kind"], first["label"])]:
            group = groups.setdefault(key, {"admitted": 0, "withdrawn": Counter(), "ends": Counter(),
                                            "waited": [], "held": [], "recorded": 0, "behind": Counter(),
                                            "queued": Counter(), "loads": [], "peaks": [], "busies": [],
                                            "busyPeaks": []})
            group["waited"].append(first["waited"])
            saw(group, first)
            if first["event"] == "withdrawn":
                group["withdrawn"][first["outcome"]] += 1
                continue
            group["admitted"] += 1
            # An admission with no end logged is still held, or its process was killed and its entry
            # either awaits recovery or was recovered or reclaimed since (vocaro/queuekeeper#23).
            group["ends"][end["outcome"] if end else "recovered" if "recovered" in lines
                          else "reclaimed" if "reclaimed" in lines else "unlogged"] += 1
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
        busies, busy_peaks = group["busies"], group["busyPeaks"]
        if busies:
            text += f"; CPU busy when asked mean {sum(busies) / len(busies):.0f}%, max {max(busies):.0f}%"
            if busy_peaks: text += f", while waiting max {max(busy_peaks):.0f}%"
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


# `qk admissions usage`: whether the second Mac is being used, and why work is on this Mac, read from both Macs'
# capacity and admission logs rather than from the sound of a fan (doc/guides/shared-host.md, Which Mac runs a lane).
USAGE_WINDOW = 3 * 3600
# An admission with no end logged that began longer than this before the window's end is taken as one whose process
# died unrecorded, and counts as running no longer: lanes' time limits are 2 h by default (processes.DEFAULT_LIMIT).
UNENDED_SECONDS = 12 * 3600
# Why work that queuekeeper never offered to the second Mac runs here, as cli's placement reasons say it, by the category
# usage counts it under. Until the owner's decision of 2026-11-07, qk admit without --verify ran here, and log lines
# from then still say so; it goes to the second Mac first now, unless one of cli.here_because's reasons keeps it here.
NOT_OFFERED = [("qk admit runs here without --verify", "qk admit without --verify"),
               ("qk admit --here", "qk admit --here"),
               ("a part of the self lane ", "a self lane's part"),
               ("foreground work runs on the Mac", "foreground work"),
               ("--here", "qk run --here"), ("--serial", "qk run --serial"),
               ("the lane says: ", "a lane that stays on this Mac"), ("its lane ", "a lane that stays on this Mac"),
               ("nested in the admission it inherits", "nested in an admission it inherits")]
# Older admissions marked local path exclusions as offered. Recognize the exact diagnostics written by
# remote.first_command and remote.local_paths, retaining the original reason for each admission.
LEGACY_LOCAL_PATH_REASONS = (
    ("it names /", ", a path outside the checkout, which the second Mac does not have"),
    ("it names ~", ", under this Mac's home, which the second Mac's checkout does not hold"),
    ("it names this checkout's path ", ", which the second Mac's checkout is not"),
)
# A lane run's label, a project's name and its lane (cli.lane_label): two words with neither a path nor an option.
LANE_LABEL = re.compile(r"[A-Za-z0-9_.-]+ [A-Za-z0-9_.-]+")
# The labels usage names under each kind of unrecorded admission, those that held the longest.
LABELS_SHOWN = 3


def spans(entries, since, until):
    """The admissions of entries that overlap [since, until), each as (start, end, whole host) clipped to it, and how
    many admissions with no end logged are taken as ended. An admission is held from its admitted line to its ended or,
    without one, its recovered or reclaimed line; one admitted before the log's first line begins its held seconds
    before its end."""
    lines = {}
    for entry in entries:
        lines.setdefault(entry["identity"], {})[entry["event"]] = entry
    found, dead = [], 0
    for each in lines.values():
        admitted, end = each.get("admitted"), each.get("ended") or each.get("recovered") or each.get("reclaimed")
        if admitted is not None: start = admitted["time"]
        elif each.get("ended") is not None: start = end["time"] - timedelta(seconds=end["held"])
        else: continue
        if end is None and (until - start).total_seconds() > UNENDED_SECONDS:
            dead += 1
            continue
        low, high = max(start, since), min(end["time"] if end else until, until)
        if high > low: found.append((low, high, bool((admitted or {}).get("wholeHost"))))
    return found, dead


def occupancy(entries, since, until):
    """The seconds of [since, until) that a Mac spent at each count of admitted heavy jobs, the seconds a whole-host
    lane held it, and how many admissions with no end logged spans took as ended."""
    held, dead = spans(entries, since, until)
    changes = sorted([(start, 1, whole) for start, _end, whole in held] + [(end, -1, whole) for _start, end, whole in held],
                     key=lambda change: (change[0], change[1]))
    seconds, count, wholes, whole_seconds, moment = Counter(), 0, 0, 0.0, since
    for at, step, whole in changes:
        seconds[count] += (at - moment).total_seconds()
        if wholes: whole_seconds += (at - moment).total_seconds()
        count, wholes, moment = count + step, wholes + (step if whole else 0), at
    seconds[count] += (until - moment).total_seconds()
    if wholes: whole_seconds += (until - moment).total_seconds()
    return {count: value for count, value in seconds.items() if value > 0}, whole_seconds, dead


def placed(entry):
    """Where an admitted line's work stands with the second Mac, as (group, reason): never offered to it, offered and
    declined by it, sent from another Mac, or unrecorded, for a line that names no placement, which an older toolkit
    and admission's other callers write. A declined reason's numbers read as N, so that like reasons count together."""
    placement = entry.get("placement")
    if not isinstance(placement, dict):
        return "unrecorded", "a lane" if LANE_LABEL.fullmatch(entry["label"]) else "a command"
    if placement.get("dispatchedFrom"): return "sent", f"sent by {placement['dispatchedFrom']}"
    because = str(placement.get("because") or "no reason given")
    if placement.get("offered") and not any(
            because.startswith(prefix) and because.endswith(suffix) for prefix, suffix in LEGACY_LOCAL_PATH_REASONS):
        return "declined", re.sub(r"\d+(?:\.\d+)?", "N", because)
    return "never", next((name for prefix, name in NOT_OFFERED if because.startswith(prefix)), because)


def shares(seconds, total):
    return ", ".join(f"{count} {100 * value / total:.0f}%" for count, value in sorted(seconds.items()))


def window_lines(mac, entries, since, until):
    """What one Mac's admission log says of [since, until): its share of time at each count of admitted heavy jobs."""
    total = (until - since).total_seconds()
    seconds, whole, dead = occupancy(entries, since, until)
    mean = sum(count * value for count, value in seconds.items()) / total if total else 0
    begun = sum(1 for entry in entries if entry["event"] == "admitted" and since <= entry["time"] < until)
    text = (f"  {mac}: {begun} admitted; time at N heavy jobs: {shares(seconds, total)}; mean {mean:.1f}; "
            f"a whole-host lane held it {100 * whole / total:.0f}%")
    if dead: text += f"; {dead} admission(s) with no end logged, begun over {UNENDED_SECONDS // 3600} h before, not counted"
    return [text]


def placement_lines(entries, since, until):
    """This Mac's admissions made in [since, until) by where they stand with the second Mac (placed), each with the
    admissions and the time they held, ended or not."""
    lines = {}
    for entry in entries:
        lines.setdefault(entry["identity"], {})[entry["event"]] = entry
    groups = {}
    for each in lines.values():
        admitted = each.get("admitted")
        if admitted is None or not since <= admitted["time"] < until: continue
        end = each.get("ended") or each.get("recovered") or each.get("reclaimed")
        held = ((end["time"] if end else min(until, datetime.now(timezone.utc))) - admitted["time"]).total_seconds()
        group, reason = placed(admitted)
        counted = groups.setdefault(group, {}).setdefault(reason, [0, 0.0, Counter()])
        counted[0] += 1
        counted[1] += max(held, 0)
        # An unrecorded line says only its label, so the labels that held the longest stand in for the reason.
        counted[2][admitted["label"]] += max(held, 0)
    heads = {"never": "never offered to the second Mac", "declined": "offered to the second Mac and declined",
             "sent": "sent here by another Mac", "unrecorded": "placement unrecorded (an older toolkit's line, or a "
             "caller's own admission, such as qk admit's before it logged placement)"}
    out = []
    for group in ["never", "declined", "sent", "unrecorded"]:
        if group not in groups: continue
        reasons = groups[group]
        out.append(f"  {heads[group]}: {sum(each[0] for each in reasons.values())}, held "
                   f"{duration(sum(each[1] for each in reasons.values()))}")
        for reason, (count, held, labels) in sorted(reasons.items(), key=lambda item: (-item[1][1], item[0])):
            out.append(f"    {reason}: {count}, held {duration(held)}")
            if group == "unrecorded":
                out += [f"      {label[:80]}: held {duration(seconds)}" for label, seconds in labels.most_common(LABELS_SHOWN)]
    if not out: out.append("  none")
    out.append("  Light lanes take no admission and are not logged: each runs where it starts, never offered.")
    return out


def capacity_lines(readings):
    """Both Macs' capacity side by side, readings being (column name, the name remote.spare's verdict gives the Mac,
    reading or why it is unavailable), with remote.spare's
    verdict for an ordinary heavy lane and for a whole-host lane on each."""
    from queuekeeper import remote
    def cells(name, short, reading):
        if isinstance(reading, str) or "error" in reading:
            why = reading if isinstance(reading, str) else reading["error"]
            return {"": name, "slots busy": f"unavailable: {why}"}
        busy = sorted(reading.get("busy") or [])
        cpu = reading.get("cpuBusy")
        memory = reading.get("memory")
        return {"": name,
                "slots busy": f"{len(busy)} of {reading['slots']} guaranteed, {reading['slotsMax']} at most"
                              + (f" (slots {', '.join(map(str, busy))})" if busy else ""),
                "CPU busy": (f"{cpu:.0f}%" if isinstance(cpu, (int, float)) else "unavailable")
                            + f" (limit {reading['busyLimit']:g}%)",
                "memory": PRESSURE.get(memory, "unread" if memory is None else f"level {memory}"),
                "whole-host": ", ".join(reading.get("wholeHost") or []) or "none",
                "exclusive": ", ".join(reading.get("exclusive") or []) or "none",
                "queued ahead": reading.get("kept") or reading.get("queuedExclusive") or "none",
                "heavy lane": remote.spare(reading, short) or "spare capacity",
                "whole-host lane": remote.spare(reading, short, whole_host=True) or "spare capacity"}
    columns = [cells(name, short, reading) for name, short, reading in readings]
    rows = list(dict.fromkeys(key for column in columns for key in column))
    width = max(len(row) for row in rows)
    return [f"  {row:<{width}}  " + " | ".join(column.get(row, "-") for column in columns) for row in rows]


def usage(here, there, since, until, out=print):
    """Print the usage report: here, and each of there, the farm's other Macs in order (or the one of them), is
    (name, short name, capacity reading or why unavailable, admission log entries or why unavailable)."""
    shown = lambda moment: moment.astimezone().strftime("%Y-%m-%d %H:%M %Z")
    others = there if isinstance(there, list) else [there]
    out("Capacity now:")
    for line in capacity_lines([here[:3], *(other[:3] for other in others)]): out(line)
    out(f"Admitted heavy jobs from {shown(since)} to {shown(until)} (time at N heavy jobs at once):")
    for name, _short, _reading, entries in [here, *others]:
        if isinstance(entries, str): out(f"  {name}: log unavailable: {entries}")
        else:
            for line in window_lines(name, entries, since, until): out(line)
    out("Why this Mac's admissions made in the period ran here:")
    if isinstance(here[3], str): out(f"  log unavailable: {here[3]}")
    else:
        for line in placement_lines(here[3], since, until): out(line)
    return 0


def usage_main(argv=None, out=print):
    """qk admissions usage: whether the second Mac is being used, and why work is on this Mac."""
    from queuekeeper import offload_host, remote
    parser = argparse.ArgumentParser(prog="qk admissions usage", description="Show both Macs' capacity now, side by "
        "side, with whether each has spare capacity for a heavy and for a whole-host lane; the share of the period "
        "each Mac spent at each count of admitted heavy jobs; and why this Mac's admissions ran here: never offered "
        "to the second Mac, and why, or offered and declined, and why (doc/guides/shared-host.md, Which Mac runs a lane).")
    parser.add_argument("--since", type=local_time, metavar="WHEN", help="the period's start, an ISO 8601 date or "
                        "time, local unless it names a zone (default 3 hours before --until)")
    parser.add_argument("--until", type=local_time, metavar="WHEN", help="the period's end (default now)")
    parser.add_argument("--there", type=Path, action="append", metavar="LOG", help="a copy of the second Mac's "
                        "admission log to read instead of asking it over ssh; name the previous generation first")
    args = parser.parse_args(argv)
    until = args.until or datetime.now(timezone.utc)
    since = args.since or until - timedelta(seconds=USAGE_WINDOW)
    if since >= until: parser.error("--since must come before --until")
    here = (f"this Mac ({offload_host.mac_name()})", "this Mac", offload_host.capacity(), read_log()[0])
    from queuekeeper import farm
    try: macs = farm.active()
    except ValueError as error: macs, why = [], str(error)
    else: why = "no second Mac is configured"
    if not macs: return usage(here, ("second Mac", "the second Mac", why, why), since, until, out)
    there = []
    for index, mac in enumerate(macs):
        host = mac["ssh"]
        try: reading = remote.capacity_there(host)
        except (remote.Unreachable, remote.Unavailable, remote.Misconfigured) as error: reading = str(error)
        # A copy of a log is the first Mac's, the one the farm had when it had one.
        if args.there and index == 0: entries = read_log(args.there)[0]
        else:
            try: entries = remote.admission_log_there(host, since - timedelta(seconds=UNENDED_SECONDS))
            except (remote.Unreachable, remote.Unavailable, remote.Misconfigured) as error: entries = str(error)
        there.append((farm_mac_name(mac, len(macs)), host if len(macs) == 1 else mac["name"], reading, entries))
    return usage(here, there, since, until, out)


def farm_mac_name(mac, count):
    """What a report calls one of the farm's Macs: by its name in the farm and its SSH destination once the farm
    has several to tell apart, and as the second Mac while it has one."""
    if count == 1: return f"second Mac ({mac['ssh']})"
    return mac["name"] if mac["name"] == mac["ssh"] else f"{mac['name']} ({mac['ssh']})"


def unknown_host(name, role, why, state="unknown"):
    """A host `qk admissions now` could not read: unknown, never idle; or notConfigured, a farm with no other Mac."""
    return {"name": name, "role": role, "state": state, "reasons": [why], "observedAt": None, "limits": None,
            "slots": None, "model": None, "holders": None, "unattributed": None, "waiters": None, "reclaimed": None}


def farm_role(mac, count):
    """The key a report gives one of the farm's Macs among its hosts: its name in the farm, and `second` while the
    farm has one Mac, as before it could have more. This Mac's is `here`."""
    return "second" if count == 1 else mac["name"]


def admissions_now(here_only=False, read_here=None, read_there=None, macs=None):
    """{"schemaVersion": 1, "hosts": [...]}: this Mac's reading (offload_host.admissions_now) and, unless here_only,
    that of each of the farm's Macs that take work now (macs(), farm.active), in the farm's order, all asked at once
    over ssh, each within remote.ADMISSIONS_BUDGET seconds. Each of them carries its name in the farm (mac), its
    hardware UUID (id) and its SSH destination (ssh), and its role is its key among the hosts (farm_role). A Mac
    that cannot be read is unknown with why, and this Mac's reading stands beside it."""
    from queuekeeper import farm, offload_host, remote
    read_here = read_here or offload_host.admissions_now
    read_there = read_there or remote.admissions_there
    try: here = {"name": f"this Mac ({offload_host.mac_name()})", "role": "here", **read_here()}
    except Exception as error:  # noqa: BLE001 - a reading that fails is unknown, never idle
        here = unknown_host("this Mac", "here", f"its reading failed: {type(error).__name__}: {error}")
    hosts = [here]
    if here_only: return {"schemaVersion": 1, "hosts": hosts}
    try: found = (macs or farm.active)()
    except ValueError as error: return {"schemaVersion": 1, "hosts": [*hosts, unknown_host("second Mac", "second", str(error))]}
    if not found:
        hosts.append(unknown_host("second Mac", "second", "no second Mac is configured", "notConfigured"))
        return {"schemaVersion": 1, "hosts": hosts}
    def ask(mac):
        name, role = farm_mac_name(mac, len(found)), farm_role(mac, len(found))
        try: reading = {"name": name, "role": role, **read_there(mac["ssh"], remote.ADMISSIONS_BUDGET)}
        except remote.Unavailable as error: reading = unknown_host(name, role, str(error))
        except Exception as error:  # noqa: BLE001 - a reading that fails is unknown, never idle
            reading = unknown_host(name, role, f"its reading failed: {type(error).__name__}: {error}")
        return {**reading, "mac": mac["name"], "id": mac.get("id"), "ssh": mac["ssh"]}
    readings = [None] * len(found)
    def one(index): readings[index] = ask(found[index])
    threads = [threading.Thread(target=one, args=(index,), daemon=True) for index in range(1, len(found))]
    for thread in threads: thread.start()
    one(0)
    for thread in threads: thread.join()
    return {"schemaVersion": 1, "hosts": hosts + readings}


def progress_lines(holder):
    """A running holder's progress as its leaves observe it (offload_host.holder_progress), a line each; that
    nothing is observed for one whose leaves publish none; nothing for a holder that does not run, or whose
    reading has no progress, such as an older toolkit's. A quiet interval is evidence, never a verdict
    (vocaro/queuekeeper#70)."""
    if holder.get("process") != "running" or "progress" not in holder: return []
    observations = holder["progress"]
    if observations is None: return ["      progress: unknown"]
    if not observations: return ["      progress: not observed (only a lane that declares `stall` publishes it)"]
    def seconds(value): return f"{value:.0f}s" if type(value) in (int, float) else "an unknown time"
    lines = []
    for record in observations:
        quiet, bound = record.get("quietSeconds"), record.get("thresholdSeconds")
        if record.get("state") == "quiet":
            now = f"quiet {seconds(quiet)}, past its {seconds(bound)} bound ({record.get('action') or 'report'})"
        elif type(quiet) in (int, float): now = f"quiet {seconds(quiet)} of its {seconds(bound)} bound"
        else: now = "unobservable now"
        last = record.get("lastProgressSeconds")
        lines.append(f"      progress: {record.get('lane') or 'a lane'} on {record.get('host') or 'an unnamed host'}: "
                     f"{now}; last progress {'at an unknown time' if last is None else seconds(last) + ' ago'}; "
                     f"longest quiet {seconds(record.get('maximumQuietSeconds'))}; {record.get('samples')} samples, "
                     f"{record.get('unobservableSamples')} unobservable")
    return lines


def reclaimed_lines(reclaimed):
    """The holders admission reclaimed lately (offload_host.reclaimed_lately), a line each, for `qk admissions now`:
    each slot it held is free, and says so, with when, why and what was signaled (vocaro/queuekeeper#23). Nothing
    when there are none, or the reading, an older toolkit's, does not say."""
    if not reclaimed: return []
    lines = ["  reclaimed:"]
    for item in reclaimed:
        kind = ("exclusive" if item.get("kind") == "exclusive" else "whole-host" if item.get("wholeHost") else "heavy") \
            + (" with the model" if item.get("model") and item.get("kind") != "exclusive" else "")
        where = "every slot" if item.get("kind") == "exclusive" else \
            ", ".join(f"slot {slot}" for slot in item.get("slots") or []) or "no slot its files named"
        moment = item.get("reclaimedAt")
        when = (f"{duration(item['reclaimedSeconds'])} ago" if isinstance(item.get("reclaimedSeconds"), (int, float))
                else "at an unknown time") + (f", at {datetime.fromtimestamp(moment).strftime('%H:%M:%S')}"
                                               if isinstance(moment, (int, float)) else "")
        condition = {"ended": "its process had ended", "unknown": "its process could not be checked"}.get(
            item.get("process"), "its process could not be confirmed running")
        signaled = item.get("signaled") or []
        stopped = ("; signaled " + ", ".join(f"PID {each['pid']} ({' then '.join(each.get('signals') or [])})"
                                              for each in signaled) if signaled
                   else f"; {item['unsignaled']}" if item.get("unsignaled") else "; nothing of it still ran to signal")
        survived = f"; still running after SIGKILL: PID {', '.join(map(str, item['survived']))}" if item.get("survived") else ""
        lines.append(f"    {item.get('label')}: {kind}, {where} freed, PID {item.get('pid')}, reclaimed {when}: "
                     f"{condition}" + (f" ({item['why']})" if item.get("why") else "") + f"{stopped}{survived}")
    return lines


def now_lines(reading, at=None):
    """`qk admissions now` in words, host by host."""
    lines = []
    for host in reading["hosts"]:
        state, reasons = host.get("state"), host.get("reasons") or []
        if state in ("unknown", "notConfigured"):
            lines.append(f"{host['name']}: {'unknown' if state == 'unknown' else 'not configured'}: {'; '.join(reasons)}")
            continue
        when = host.get("observedAt")
        read = f", read at {datetime.fromtimestamp(when).strftime('%H:%M:%S')}" if isinstance(when, (int, float)) else ""
        space = host.get("namespace") or {}
        isolated = "" if space.get("shared", True) else \
            f", isolated namespace {space.get('HEAVY_SLOT_DIR') or space.get('slotDirectory')}"
        lines.append(f"{host['name']}{read}{isolated}" + (f": partial: {'; '.join(reasons)}" if state == "partial" else ""))
        found, holders = host.get("limits"), host.get("holders")
        slots = host.get("slots")
        if found and slots is not None:
            held = [slot for slot in slots if slot.get("holder")]
            guaranteed = sum(1 for slot in held if slot["guaranteed"])
            exclusive = any(holder["exclusive"] for holder in holders or [])
            guaranteed = found["slots"] if exclusive else guaranteed
            extra = found["slotsMax"] - found["slots"] if exclusive else len(held) - guaranteed
            model = (host.get("model") or {}).get("holder")
            owner = next((holder["label"] for holder in holders or [] if holder["identity"] == model), None)
            lines.append(f"  slots: {guaranteed} of {found['slots']} guaranteed and {extra} of "
                         f"{found['slotsMax'] - found['slots']} beyond them held; the model "
                         + (f"held by {owner}" if owner else "free"))
        if holders is None: lines.append("  holders: unknown")
        elif not holders: lines.append("  holders: none")
        else:
            lines.append("  holders:")
            for holder in holders:
                kind = ("exclusive" if holder["exclusive"] else "whole-host" if holder["wholeHost"] else "heavy") \
                    + (" with the model" if holder["model"] and not holder["exclusive"] else "")
                if holder["exclusive"]: where = "every slot"
                elif holder["slots"] is None: where = "a slot no file names"
                else:
                    where = ", ".join([f"slot {slot}" for slot in holder["guaranteed"]]
                                      + [f"slot {slot} (beyond the guaranteed)" for slot in holder["extra"]]) or "no slot"
                held = f"held {duration(holder['heldSeconds'])}" if holder.get("heldSeconds") is not None else "held for an unknown time"
                # A holder whose process has ended or cannot be checked is freed at admission's next attempt
                # (vocaro/queuekeeper#23); an uncertain one whose owner still runs is counted until it is recovered.
                status = ("; its process has ended: admission frees its slots at its next attempt, stopping what is "
                          "left of it" if holder["process"] == "ended"
                          else "; its process could not be checked: admission frees its slots at its next attempt"
                          if holder["process"] == "unknown"
                          else "; stranded: uncertain, counted until recovered (`python3 scripts/qk admissions recover`)"
                          if holder.get("stranded") else "")
                lines.append(f"    {holder['label']}: {kind}, {where}, PID {holder['pid']}, {held}{status}")
                if whence(holder.get("origin")): lines.append(f"      from {whence(holder['origin'])}")
                lines += progress_lines(holder)
        lines += reclaimed_lines(host.get("reclaimed"))
        waiters = host.get("waiters")
        if waiters is None: lines.append("  waiting: unknown")
        elif not waiters: lines.append("  waiting: none")
        else:
            lines.append("  waiting:")
            for waiter in waiters:
                kind = {"exclusive": "exclusive", "wholeHost": "whole-host", "ordinary": "heavy"}.get(waiter["kind"], waiter["kind"])
                if waiter.get("model"): kind += " with the model"
                if waiter.get("position") is not None: kind += f", priority place {waiter['position']}"
                waited = f"waited {duration(waiter['waitedSeconds'])}" if waiter.get("waitedSeconds") is not None \
                    else "waited for an unknown time"
                if waiter.get("away"):
                    asked = waiter.get("lastAskedSeconds")
                    why = ("place kept while away, last asked " + (f"{duration(asked)} ago" if asked is not None else "at an unknown time")
                           + ("; it holds later work back" if waiter.get("holdsBack")
                              else "; it no longer holds later work back" if waiter.get("holdsBack") is False else "")
                           + "; no reason recorded")
                elif not waiter.get("reason"):
                    why = "asking, with no reason recorded (a toolkit older than its record, or before its first refusal)"
                else:
                    why = waiter["reason"]
                    if waiter.get("stale"): why += f" (as of {duration(waiter['sampledSeconds'])} ago: stale)"
                status = "; its process could not be checked" if waiter.get("process") == "unknown" else ""
                lines.append(f"    {waiter['label']}: {kind}, PID {waiter['pid']}, {waited}: {why}{status}")
                if whence(waiter.get("origin")): lines.append(f"      from {whence(waiter['origin'])}")
    return lines


def now_main(argv=None, out=print, read=None):
    """qk admissions now: who holds and who waits for heavy-work admission on each Mac now (vocaro-engineering#342). 0
    when every Mac asked about was read whole, 1 when one is partial or unknown, 2 for a mistaken command line."""
    parser = argparse.ArgumentParser(prog="qk admissions now", description="Show who holds heavy-work admission on "
        "this Mac and the second Mac now, and who waits for it, for how long and why, as each waiter says it. It changes "
        "nothing. A Mac that cannot be read is unknown, never idle (doc/guides/shared-host.md, Who holds and who waits).")
    parser.add_argument("--json", action="store_true", help="print the readings as JSON")
    parser.add_argument("--here", action="store_true", help="read this Mac only, in this process's namespace")
    args = parser.parse_args(argv)
    isolated = [name for name in ("HEAVY_SLOT_DIR", "QUEUEKEEPER_GATE_LOCK_FILE") if os.environ.get(name)]
    if isolated and not args.here:
        parser.error(f"{' and '.join(isolated)} name{'s' if len(isolated) == 1 else ''} an isolated namespace on this "
                     "Mac alone: add --here to read it")
    reading = (read or admissions_now)(here_only=args.here)
    if args.json: out(json.dumps(reading, indent=2))
    else:
        for line in now_lines(reading): out(line)
    return 0 if all(host.get("state") in ("observed", "notConfigured") for host in reading["hosts"]) else 1


# What recovery tells a person to do about an entry it leaves.
BY_HAND = ("check by hand that nothing its owner started still runs, then remove its entry with `python3 scripts/qk "
           "admissions remove OWNER --why TEXT` (doc/guides/shared-host.md, Lock reclamation)")
# Why recovery leaves an entry it cannot prove finished, as reclaim says it: the entries `qk admissions remove` may
# remove once a person has checked them, unlike one whose owner or a process it names still runs.
UNRECORDED, UNVOUCHED = "it has no process record", "its process record cannot vouch for every command"


def entry_age(owner):
    """How long ago an entry's job was admitted, or, for an entry an older toolkit wrote, its owner started."""
    try: return f"admitted {duration(max(0, time.time() - datetime.fromisoformat(owner['admitted']).timestamp()))} ago"
    except (KeyError, TypeError, ValueError): pass
    try:
        began = time.mktime(time.strptime(" ".join(owner["start"].split()), "%a %b %d %H:%M:%S %Y"))
        return f"its owner started {duration(max(0, time.time() - began))} ago"
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return "admitted at an unknown time"


# Seconds ended gives ps to read an owner's start before it asks the kernel instead.
ENDED_PS_SECONDS = 5


def liveness(owner):
    """Whether a guard entry's owner still runs as itself, as (state, why): running, why None; ended, its PID gone or
    running another process; or unknown, its PID runs but neither ps, within ENDED_PS_SECONDS, nor the kernel, which
    reads no process's arguments (kernel_start), gives its start (vocaro/queuekeeper#23)."""
    pid = owner["pid"]
    try: os.kill(pid, 0)
    except ProcessLookupError: return "ended", f"its PID {pid} is gone"
    except PermissionError: pass
    try:
        listed = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="], capture_output=True, text=True,
                                timeout=ENDED_PS_SECONDS, stdin=subprocess.DEVNULL)
        start = " ".join(listed.stdout.split()) if listed.returncode == 0 else ""
        failed = f"ps exited {listed.returncode}"
    except subprocess.TimeoutExpired: start, failed = "", f"ps took more than {ENDED_PS_SECONDS}s"
    except (OSError, subprocess.SubprocessError) as error: start, failed = "", f"ps failed: {error}"
    if not start:
        if sys.platform != "darwin":
            return "unknown", f"{failed}, and the kernel's record of a process is read only on macOS"
        try: start = kernel_start(pid)
        except OSError as error: return "unknown", f"{failed}, and the kernel could not say: {error}"
        if start is None: return "ended", f"its PID {pid} is gone"
        start = " ".join(start.split())
    if start != " ".join(str(owner["start"]).split()):
        return "ended", f"its PID {pid} runs another process, started {start}"
    return "running", None


def ended(owner):
    """Whether an entry's owner has certainly ended: its PID is gone, or another start is read there (liveness). When
    neither ps nor the kernel can say, the owner is left running, so that attended recovery, which `qk admissions
    recover` runs, never takes a live owner's entry. Admission frees such an entry itself (free_holder)."""
    return liveness(owner)[0] == "ended"


# Seconds a freed holder's processes have after SIGTERM before SIGKILL, and after SIGKILL before admission names those
# still running and goes on without them (vocaro/queuekeeper#23). Admission waits them out under its coordination
# lock, so they are short. Chosen, not measured.
RECLAIM_TERM_SECONDS, RECLAIM_KILL_SECONDS = 3.0, 2.0


def collapsed(start):
    """A process start with its spacing collapsed, as ps and the kernel give it, or None."""
    return " ".join(start.split()) if isinstance(start, str) else None


def start_now(pid):
    """The start the process at pid has now, spacing collapsed: None when no process runs there, "" when it cannot be
    read. The kernel first on macOS, which starts no process and works when ps fails; ps otherwise."""
    if sys.platform == "darwin":
        try: return collapsed(kernel_start(pid))
        except OSError: pass
    try: return collapsed(process_start(pid))
    except (OSError, subprocess.SubprocessError): return ""


def held_processes(guard, identity, entry):
    """{PID: start} of an entry's owner and of every process its process record names, each as recorded: what is
    left of a holder, if anything, is among them and below them. A line that cannot be read is passed over."""
    found = {entry["pid"]: collapsed(entry["start"])}
    path = record_path(guard, identity) if re.fullmatch(r"[0-9a-f]{32}", identity) else None
    try: lines = path.read_bytes().splitlines() if path is not None else []
    except OSError: lines = []
    for text in lines:
        try: line = json.loads(text)
        except ValueError: continue
        listed = line.get("processes") if isinstance(line, dict) else None
        for item in listed if isinstance(listed, list) else []:
            if isinstance(item, list) and len(item) == 2 and type(item[0]) is int and item[0] > 1 \
                    and isinstance(item[1], str):
                found.setdefault(item[0], collapsed(item[1]))
    return found


def lineage(current):
    """This process and its ancestors, which reclamation never signals, from a snapshot when there is one."""
    mine, pid = {os.getpid(), os.getppid()}, os.getpid()
    while current and pid in current and pid > 1 and len(mine) < 512:
        pid = current[pid][0]
        mine.add(pid)
    return mine


def stop_holder(guard, identity, entry, condition, *, listing=None, start_of=None, kill=None, clock=time.monotonic,
                sleep=time.sleep):
    """Stop what is left of a holder admission frees (free_holder): SIGTERM, then SIGKILL after RECLAIM_TERM_SECONDS,
    to each process held_processes names whose PID still has the start recorded for it, and to every process found
    below one of those by parent; each is checked again by PID and start just before each signal, so that a PID
    another process now has is never signaled. This process and its ancestors never are. A holder whose state is
    unknown, its start unreadable, is signaled nothing, nor anything under it: its PID is not proven its own. Returns
    what was signaled, [{pid, start, signals}], the PIDs still running RECLAIM_KILL_SECONDS after SIGKILL, and why
    nothing was signaled when that is so (vocaro/queuekeeper#23)."""
    import signal
    from queuekeeper.processes import descendants, snapshot
    listing, start_of, kill = listing or snapshot, start_of or start_now, kill or os.kill
    if condition != "ended":
        return {"signaled": [], "survived": [],
                "unsignaled": "its start could not be read, so its PID is not proven its holder's: neither it nor "
                               "anything under it was signaled"}

    def listed():
        try: return listing()
        except (OSError, subprocess.SubprocessError, ValueError): return None

    mine = lineage(listed())
    known = {pid: start for pid, start in held_processes(guard, identity, entry).items()
             if pid > 1 and pid not in mine and start and start_of(pid) == start}
    signals = {}

    def sweep(number):
        """Find what runs below the known processes, and send number to each one still running as itself that has not
        had it (SIGKILL again each time); those still running, by PID."""
        current = listed()
        alive = {pid: start for pid, start in known.items() if start_of(pid) == start}
        if current is not None:
            for pid, (_parent, _group, began) in descendants(current, set(alive)).items():
                start = collapsed(began)
                if pid > 1 and pid not in mine and pid not in known and start:
                    known[pid] = alive[pid] = start
        for pid, start in alive.items():
            sent = signals.setdefault((pid, start), [])
            if number == signal.SIGTERM and sent: continue
            if start_of(pid) != start: continue  # it ended, or its PID went to another process, since it was found
            with contextlib.suppress(ProcessLookupError, PermissionError):
                kill(pid, number)
                sent.append(signal.Signals(number).name)
        return {pid for pid, start in alive.items() if start_of(pid) == start}

    alive = sweep(signal.SIGTERM) if known else set()
    deadline = clock() + RECLAIM_TERM_SECONDS
    while alive and clock() < deadline:
        sleep(0.05)
        alive = sweep(signal.SIGTERM)
    if alive:
        alive = sweep(signal.SIGKILL)
        deadline = clock() + RECLAIM_KILL_SECONDS
        while alive and clock() < deadline:
            sleep(0.05)
            alive = {pid for pid in alive if start_of(pid) == known[pid]}
    return {"signaled": [{"pid": pid, "start": start, "signals": sent} for (pid, start), sent in sorted(signals.items())
                          if sent],
            "survived": sorted(alive)}


def free_locks(directory, maximum, identity):
    """Free the slots and the model whose files name identity as their holder, which the caller has just removed from
    the guard: the lock names of each, and of those whose lock something still held, which are replaced by a fresh
    file, so that the next admission takes it. What still holds the old one, which reclamation could not stop, keeps
    a lock that no longer guards anything (vocaro/queuekeeper#23). The caller holds the coordination lock."""
    named, replaced = [], []
    for name in [f"slot{slot}" for slot in range(maximum)] + ["model"]:
        path = Path(directory) / name
        try: written = path.read_text(errors="replace").split(" ", 2)
        except OSError: continue
        if len(written) < 2 or written[1] != identity: continue
        named.append(name)
        try: fd = os.open(path, os.O_RDWR)
        except OSError: continue
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            with contextlib.suppress(OSError):
                path.unlink()
                replaced.append(name)
        finally:
            os.close(fd)
    return named, replaced


def free_holder(guard, directory, maximum, state, identity, condition, why, by=None):
    """Free a guard entry whose owner admission cannot confirm running, its state ended or unknown (liveness), at
    once: stop what is left of it (stop_holder), remove the entry, and the guard with its last one, and its process
    record, free its slots, every slot for exclusive work, and the model if it held it (free_locks), and log a
    `reclaimed` line with why and what was signaled. Uncertainty about a holder never keeps a slot held: when the
    queue cannot confirm that a holder's process still runs, it takes the process as ended (the owner's decision,
    2026-11-13, vocaro/queuekeeper#23). Returns that line. The caller holds the coordination lock."""
    owners = state["owners"]
    entry = owners[identity]
    try: stopped = stop_holder(guard, identity, entry, condition)
    except Exception as error:  # noqa: BLE001 - a failure to stop it never keeps its slots held
        stopped = {"signaled": [], "survived": [], "unsignaled": f"stopping what is left of it failed: {error}"}
    del owners[identity]
    if owners: publish_guard(guard, state)
    else: guard.unlink(missing_ok=True)
    if re.fullmatch(r"[0-9a-f]{32}", identity):
        with contextlib.suppress(OSError): record_path(guard, identity).unlink(missing_ok=True)
    named, replaced = free_locks(directory, maximum, identity)
    line = {"event": "reclaimed", "identity": identity, "pid": entry["pid"],
            "kind": "exclusive" if entry.get("exclusive") else "model" if entry.get("model") else "heavy",
            "label": str(entry.get("label"))[:LABEL_LIMIT], "uncertain": bool(entry.get("uncertain")),
            **({"wholeHost": True} if entry.get("wholeHost") else {}), "process": condition, "why": why,
            "slots": [int(name[4:]) for name in named if name.startswith("slot")],
            "model": bool(entry.get("model") or entry.get("exclusive")), "replaced": replaced, **stopped,
            **({"by": by} if by else {})}
    append_log(line)
    return line


def reclaimed_words(line):
    """What a `reclaimed` line says, for admission's own message: the holder, why and what was signaled."""
    signaled = line.get("signaled") or []
    stopped = ("; signaled " + ", ".join(f"PID {item['pid']} ({' then '.join(item['signals'])})" for item in signaled)
               if signaled else f"; {line['unsignaled']}" if line.get("unsignaled") else
               "; nothing of it still ran to signal")
    survived = f"; still running after SIGKILL: PID {', '.join(map(str, line['survived']))}" if line.get("survived") else ""
    replaced = f"; replaced, as something still held them: {', '.join(line['replaced'])}" if line.get("replaced") else ""
    held = ", ".join([f"slot {slot}" for slot in line.get("slots") or []] + (["the model"] if line.get("model") else []))
    return (f"reclaimed {' '.join(line['label'].split())} (PID {line['pid']}), {held or 'no slot its files name'}: "
            f"{'its process has ended' if line['process'] == 'ended' else 'its process could not be checked'} "
            f"({line['why']}){stopped}{survived}{replaced}")


class Unrecorded(ValueError):
    """A process record that names no command of a caller that runs its own: it vouches for nothing."""


def opening(line):
    """A process record's admission line (Admission._open_record), checked; a ValueError when it is not one."""
    owner, ancestors = line.get("owner"), line.get("ancestors")
    if not (isinstance(owner, list) and len(owner) == 2 and type(owner[0]) is int and isinstance(owner[1], str)
            and type(line.get("group")) is int and type(line.get("session")) is int
            and isinstance(line.get("supervised"), bool) and isinstance(ancestors, list)
            and all(isinstance(item, list) and len(item) == 2 and type(item[0]) is int and isinstance(item[1], str)
                    for item in ancestors)):
        raise ValueError("an unexpected admission line")
    return line


def owner_survivors(admission):
    """What may still run of an owner that ended before its first recorded command, as {PID: why}, with how many
    process groups and sessions were checked: each live process in the owner's process group, or in its session
    where the group leads the session, that started no earlier than the owner, other than the owner's recorded
    ancestors, a group's or session's leader, which the owner did not start, and this process and its children,
    such as the ps that lists them. Its own supervision would have recorded a command before starting it in a
    session of its own (processes.supervise), so what the owner started before its first is in its group, or its
    session, unless it left both. The owner's own PID is not checked here: recovery has found it ended or reused.

    It errs toward keeping the entry: work started after the owner by a sibling in the same group, such as another
    lane its shell started beside it, keeps it too until that work ends (#315)."""
    from queuekeeper.processes import birth_time, session, snapshot
    pid, start = admission["owner"]
    group, sid = admission["group"], admission["session"]
    spaces = {group} | ({sid} if sid == group else set())
    born = birth_time(start)
    ancestors = {(ancestor, " ".join(began.split())) for ancestor, began in admission["ancestors"]}
    current, left = snapshot(), {}
    for other, (parent, other_group, began) in current.items():
        if other in spaces or other == pid or other == os.getpid() or parent == os.getpid() \
                or (other, " ".join(began.split())) in ancestors:
            continue
        birth = birth_time(began)
        if born is not None and birth is not None and birth < born:
            continue
        if other_group in spaces:
            left[other] = f"in its owner's process group {other_group}, started since its owner"
        elif sid in spaces and session(other) == sid:
            left[other] = f"in its owner's session {sid}, started since its owner"
    # Its group, and its session when the group leads it, are checked: two, though one ID names both.
    return left, 2 if sid == group else 1


def census(path):
    """What still runs of the commands an admission's process record leaves unsettled, as {PID: why}, with
    how many commands, recorded processes and process groups or sessions were checked. A ValueError says
    why the record cannot vouch for every command: a line that cannot be read, or a command whose
    process it never named; Unrecorded, that it names no command of a caller that runs its own.

    A record whose admission line (Admission._open_record) is followed by no command is of an owner that ended
    before its first: its owner's group is checked instead (owner_survivors), and the census reports no command
    and no recorded process beside the groups and sessions checked (#315).

    It is the census a run ends with (processes.survivors): each recorded process has ended, by PID and
    start, and no live process is in a group or session one was seen using, but for an ID that is now a
    live process's PID (#26) or a process born after the last recorded close of the space (#244).
    A command the record settled was confirmed ended by its own supervision.
    """
    from queuekeeper.processes import snapshot, survivors
    commands, admission = {}, None
    with path.open("rb") as stream:
        for number, text in enumerate(stream, 1):
            try:
                line = json.loads(text)
                if isinstance(line, dict) and "admission" in line and "command" not in line:
                    admission = opening(line)
                    continue
                if not isinstance(line["command"], str) or not isinstance(line.get("argv", []), list):
                    raise TypeError("an unexpected value")
                command = commands.setdefault(line["command"], {"argv": [], "root": None, "processes": set(),
                                                                "spaces": set(), "closed": {}, "settled": False})
                processes = {(pid, start) for pid, start in line.get("processes", [])}
                spaces, root = set(line.get("spaces", [])), line.get("root", command["root"])
                if not all(type(pid) is int and isinstance(start, str) for pid, start in processes) or \
                        not all(type(space) is int for space in spaces) or not (root is None or type(root) is int):
                    raise TypeError("an unexpected value")
                for space, when in line.get("closed", []):
                    if type(space) is not int or (when is not None and (
                            type(when) not in (int, float) or not math.isfinite(when))):
                        raise TypeError("an unexpected value")
                    if when is None:
                        command["closed"].pop(space, None)
                    else:
                        command["closed"][space] = when
            except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
                raise ValueError(f"line {number} of {path} cannot be read") from None
            command["argv"], command["root"] = line.get("argv", command["argv"]), root
            command["processes"] |= processes
            command["spaces"] |= spaces
            command["settled"] = command["settled"] or "settled" in line
    if not commands and admission is None:
        raise ValueError("its process record is empty")
    if not commands and admission is not None:
        if not admission["supervised"]:
            raise Unrecorded("its caller runs its own commands without recording them")
        left, spaces = owner_survivors(admission)
        return left, 0, 0, spaces
    unsettled = [command for command in commands.values() if not command["settled"]]
    for command in unsettled:
        if command["root"] is None:
            raise ValueError(f"its command `{' '.join(map(str, command['argv']))[:LABEL_LIMIT]}` may have started before "
                             "the record named its process")
    current, left = snapshot(), {}
    spaces, unclosed, closed = set(), set(), {}
    for command in unsettled:
        for pid, start in command["processes"]:
            if pid in current and " ".join(current[pid][2].split()) == " ".join(start.split()):
                left[pid] = "a recorded process, still running"
        if command["root"] in current and all(pid != command["root"] for pid, _start in command["processes"]):
            left.setdefault(command["root"], "its command's PID, whose start the record never saw, is in use")
        used = command["spaces"] | {command["root"]}
        spaces |= used
        # A process can be a leftover of any unsettled command that used the space.
        unclosed |= used - command["closed"].keys()
        for space in used & command["closed"].keys():
            when = command["closed"][space]
            closed[space] = max(closed.get(space, when), when)
    for pid, group, sid in survivors(spaces, {space: when for space, when in closed.items() if space not in unclosed}):
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
    def shown(entry):
        """An entry's label on one line: an qk admit label is its command line, which can span several."""
        return " ".join(entry["label"].split())[:LABEL_LIMIT]

    with locked_guard(out) as opened:
        if opened is None:
            return 1
        guard, state, owners = opened

        def recorded(identity):
            """The entry's process record, or None: an identity that is not admission's names none."""
            path = record_path(guard, identity) if re.fullmatch(r"[0-9a-f]{32}", identity) else None
            return path if path is not None and path.is_file() else None

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
            out("Recover one with `python3 scripts/qk admissions recover OWNER`, OWNER its PID or identity.")
            return 0
        identity = named(guard, owners, owner, out)
        if identity is None:
            return 1
        removed, message, _why = reclaim(guard, state, identity)
        out(message)
        return 0 if removed else 1


@contextlib.contextmanager
def locked_guard(out):
    """The guard, its state and its entries, read under the coordination lock, which is held until the block ends;
    or None, said with out, when the guard is unreadable or not in admission's format, which recovery and removal
    leave alone (#72)."""
    guard = guard_path(os.environ)
    directory = slot_directory()
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "coordination.lock").open("a+") as bridge:
        fcntl.flock(bridge, fcntl.LOCK_EX)
        try: state = json.loads(guard.read_text())
        except FileNotFoundError: state = {"vocaroEngineering": 1, "owners": {}}
        except (OSError, ValueError): state = None
        owners = entries(state)
        if owners is None:
            out(f"{guard} is unreadable or not in admission's format. Recovery leaves it alone; inspect it by hand "
                "(doc/guides/shared-host.md, Lock reclamation).")
            yield None
        else:
            yield guard, state, owners


def named(guard, owners, owner, out):
    """The identity of the one entry whose owner's PID or identity is owner, or None, said with out."""
    found = [identity for identity, entry in owners.items() if identity == owner or str(entry["pid"]) == owner]
    if len(found) == 1:
        return found[0]
    out(f"No entry of {guard} has the PID or identity {owner}; nothing was changed." if not found else
        f"PID {owner} owns {len(found)} entries of {guard}; name one by its identity: {', '.join(found)}. "
        "Nothing was changed.")
    return None


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
    unrecorded = (f"{name} has no process record, so its entry stays: an older toolkit admitted it, its record could "
                  "not be written, or its caller runs its own commands without recording them, as an older push keeper "
                  "does; a push keeper's owner.json, in its checkout's transaction lock, lists each command it ran. To "
                  f"recover it, {BY_HAND}."), UNRECORDED
    if path is None or not path.is_file():
        return (False, *unrecorded)
    try:
        left, commands, processes, spaces = census(path)
    except Unrecorded:
        return (False, *unrecorded)
    except ValueError as error:
        return False, (f"{name}'s process record cannot vouch for every command, so its entry stays: {error}. To "
                       f"recover it, {BY_HAND}."), UNVOUCHED
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
        "it ended before its first command, and nothing started since it still runs in its process group"
        + (" or session" if spaces > 1 else "") if spaces else
        "its process record shows every command it ran confirmed ended")
        + f". Its entry is removed{', and the guard with it' if not owners else ''}."), None


def remaining(path, entry):
    """What may still run of an entry's owner whose process record is missing or cannot vouch for every command, as
    {PID: why}: what of the record can be read is checked, and nothing more. Each recorded process still running as
    itself; each live process in a process group or session a recorded command used, as the run's census finds it,
    with no birth cutoff; and what the owner started before its first command (owner_survivors), in its recorded
    group or session, or, without an admission line, in a group or session the owner led. Without a record, an
    owner that led neither, as one a shell started in the shell's own group, leaves nothing here to check: that is
    the check by hand `qk admissions remove` asks for (#315)."""
    from queuekeeper.processes import snapshot, survivors
    admission, processes, spaces = None, set(), set()
    if path is not None and path.is_file():
        with path.open("rb") as stream:
            for text in stream:
                try: line = json.loads(text)
                except ValueError: continue
                if not isinstance(line, dict):
                    continue
                if "admission" in line and "command" not in line:
                    with contextlib.suppress(ValueError): admission = opening(line)
                    continue
                listed = line.get("processes")
                for item in listed if isinstance(listed, list) else []:
                    if isinstance(item, list) and len(item) == 2 and type(item[0]) is int and isinstance(item[1], str):
                        processes.add((item[0], " ".join(item[1].split())))
                used = line.get("spaces")
                spaces |= {space for space in (used if isinstance(used, list) else []) if type(space) is int}
                if type(line.get("root")) is int:
                    spaces.add(line["root"])
    if admission is None:
        admission = {"owner": [entry["pid"], entry["start"]], "group": entry["pid"], "session": entry["pid"],
                     "ancestors": []}
    left, _checked = owner_survivors(admission)
    current = snapshot()
    for pid, start in processes:
        if pid in current and " ".join(current[pid][2].split()) == start:
            left[pid] = "a recorded process, still running"
    for pid, group, sid in survivors(spaces):
        left.setdefault(pid, f"group {group}, session {sid}")
    return left


def remove(owner, why, out=print):
    """Remove by hand the guard entry whose owner's PID or identity is named, once a person has checked that nothing
    its owner started still runs, and recovery cannot prove it: its process record is missing or cannot vouch for
    every command. Recovery runs first, so an entry it can prove finished is recovered, and one whose owner or a
    process it names still runs stays. Then the census of what of the record can be read (remaining) runs, and the
    entry stays while it names anything. Otherwise the entry is removed, and the guard with its last one, its record
    deleted, and a `recovered` line logged, as recovery logs one, marked `byHand`, with why recovery left it
    (`unproven`) and the person's reason (`why`). Returns 0 when it removed or recovered the entry, and 1 otherwise.

    It is a command of its own, not an option of `qk admissions recover`, so that this repository's allow rule for
    recovery, which removes only provably finished entries, does not match it: an agent's auto mode asks before it
    removes an entry nothing proves finished (owner's decision, 2026-11-08, #315)."""
    if not why.strip():
        out("Say with --why what you checked that shows nothing its owner started still runs. Nothing was changed.")
        return 1
    with locked_guard(out) as opened:
        if opened is None:
            return 1
        guard, state, owners = opened
        identity = named(guard, owners, owner, out)
        if identity is None:
            return 1
        removed, message, unproven = reclaim(guard, state, identity)
        if removed:
            out(message)
            return 0
        if unproven not in (UNRECORDED, UNVOUCHED):
            out(f"{message}\nNothing was removed.")
            return 1
        entry = owners[identity]
        name = f"{' '.join(entry['label'].split())[:LABEL_LIMIT]} (PID {entry['pid']})"
        path = record_path(guard, identity) if re.fullmatch(r"[0-9a-f]{32}", identity) else None
        left = remaining(path, entry)
        if left:
            listing = subprocess.run(["ps", "-o", "pid,ppid,pgid,lstart,args", "-p", ",".join(map(str, sorted(left)))],
                                     capture_output=True, text=True).stdout.rstrip()
            out(f"{len(left)} process(es) its owner may have started still run, so the entry of {name} stays: "
                + "; ".join(f"PID {pid} ({reason})" for pid, reason in sorted(left.items()))
                + (f"\n{listing}" if listing else "") + "\nStop or wait for them, then run it again. Nothing was removed.")
            return 1
        del owners[identity]
        if owners: publish_guard(guard, state)
        else: guard.unlink(missing_ok=True)
        if path is not None:
            with contextlib.suppress(OSError): path.unlink(missing_ok=True)
        append_log({"event": "recovered", "identity": identity, "pid": entry["pid"],
                    "kind": "exclusive" if entry.get("exclusive") else "model" if entry.get("model") else "heavy",
                    "label": entry["label"][:LABEL_LIMIT], "uncertain": bool(entry.get("uncertain")),
                    "commands": 0, "processes": 0, "spaces": 0, "byHand": True, "unproven": unproven,
                    "why": why.strip()[:LABEL_LIMIT]})
        out(f"Removed {name} by hand: {unproven}, and nothing its owner may have started was found running. Its entry "
            f"is removed{', and the guard with it' if not owners else ''}, and the admission log records why.")
        return 0


def remove_main(argv=None):
    """qk admissions remove OWNER --why TEXT: remove by hand an entry recovery cannot prove finished."""
    parser = argparse.ArgumentParser(prog="qk admissions remove", description="Remove by hand a guard entry that "
        "recovery cannot prove finished, its process record missing or unable to vouch for every command, once you "
        "have checked that nothing its owner started still runs. Recovery runs first: an entry it can prove finished "
        "is recovered, and one whose owner or a process it names still runs stays. Then it checks what of the process "
        "record can be read, and the owner's process group or session, and leaves the entry while anything there "
        "runs. The admission log records the removal and why. It exits 1 when it removes nothing. Unlike `qk "
        "admissions recover`, it is deliberately outside this repository's allow rule for recovery "
        "(doc/guides/shared-host.md, Lock reclamation).")
    parser.add_argument("owner", metavar="OWNER", help="the PID or identity of the entry's owner, as `qk admissions "
                        "recover` lists it")
    parser.add_argument("--why", required=True, metavar="TEXT", help="what you checked that shows nothing its owner "
                        "started still runs; the admission log keeps it")
    args = parser.parse_args(argv)
    try:
        return remove(args.owner, args.why)
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"queuekeeper: {error}", file=sys.stderr)
        return 2


def recover_main(argv=None):
    """qk admissions recover [OWNER]: the guard's dead or uncertain entries, or the recovery of one."""
    parser = argparse.ArgumentParser(prog="qk admissions recover", description="List the guard's dead or uncertain "
        "entries, or recover one: remove that entry alone, and the guard with its last one, once its owner has ended "
        "and a census against its process record finds nothing of its commands still running. It exits 1 when it "
        "removes nothing, and leaves a live owner's entry, an entry without a process record and a guard that is "
        "unreadable or not in admission's format alone (doc/guides/shared-host.md, Lock reclamation).")
    parser.add_argument("owner", nargs="?", metavar="OWNER", help="the PID or identity of the entry's owner, as the "
                        "listing names it; without it, list the guard's dead or uncertain entries")
    parser.add_argument("--host", metavar="HOST", help="list or recover on this SSH host, using its login shell "
                        "and ~/Development/queuekeeper/bin/qk; without it, use this Mac")
    args = parser.parse_args(argv)
    try:
        if args.host:
            from queuekeeper import remote
            return remote.recover_admission(args.host, args.owner)
        return recover(args.owner)
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"queuekeeper: {error}", file=sys.stderr)
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
    from queuekeeper.processes import forwarding_signals, supervise
    try:
        with forwarding_signals(), Admission(" ".join(command), args.exclusive, args.timeout, args.model,
                                             args.whole_host, supervised=True) as admission:
            environment = dict(os.environ)
            return supervise(command, cwd=Path.cwd(), env=environment, on_stop=admission.interrupted)
    except AdmissionRecordError as error:
        print(f"heavy: {error}", file=sys.stderr)
        return BUSY


def json_descriptors():
    token = json.loads(os.environ[TOKEN])
    return [(index, item["fd"]) for index, item in enumerate(token["locks"])]


if __name__ == "__main__":
    # The module as supervision imports it, not this copy run as __main__, so that supervision finds the
    # admission this process holds and writes its process record.
    from queuekeeper import resources
    raise SystemExit(resources.main())
