"""The build host's side of `eng remote`, sent over SSH and run there by the host's own Python.

It imports nothing from the toolkit, because it runs before the toolkit is checked out there,
and Python 3.9, a Mac's system Python, runs it. It claims a persistent checkout of the project,
places the dispatched commits, and runs `eng run` or `eng admit` there under the build host's
own heavy-work admission, with its device record deferring physical devices to the sending Mac's
and the run given the reservations it carries. Standard output carries framed lines: `O` for
output, `L` for the lane's log as it grows, then, once the work has ended, an `F` naming each file the plan asks back,
such as a lane group's lanes' logs and receipts, followed by `D` lines of its bytes in base64, and
a closing `R` with the result as JSON. Standard input is the
dispatcher's lifeline: its end, which a stop, the dispatcher's death or a dropped connection
sends, stops the run as a stop signal stops a local one. Given `{"toolchain": DEVELOPER_DIR}`
instead of a plan, it prints the Mac's Xcodes, simulator runtimes and device record for
`eng doctor`; given `{"images": ...}` or `{"receive": ...}`, it lists the Mac's runtime images or
adds one read from standard input, for `eng remote setup --runtimes`; given `{"leftovers": ROOT}`,
it lists the processes left over from runs there, which `eng remote` prints before it sends work;
given `{"device": ...}`, what there may still use a physical device a run was given, for
`devices recover`.
"""
import base64
import ctypes
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import pwd
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time

# Seconds between reports while every checkout is in use, and before waiting for one gives up.
REPORT_SECONDS, CLAIM_TIMEOUT = 300, 5400
# Seconds the output of a finished command may take to drain: a descendant can hold the pipe.
DRAIN_SECONDS = 5
# Bytes of a runtime image read or written at once.
CHUNK = 8 << 20
# Bytes of a file sent back in one D line, before base64.
FILE_CHUNK = 48 << 10
# The platform a runtime image names when simctl gives no runtime bundle to name it by.
PLATFORMS = {"com.apple.platform.iphonesimulator": "iOS", "com.apple.platform.appletvsimulator": "tvOS",
             "com.apple.platform.watchsimulator": "watchOS", "com.apple.platform.xrsimulator": "visionOS"}
# Where heavy-work admission keeps its guard and its log, as resources.py names them, which this
# file cannot import: the guard beside the legacy gate lock, and the log's two generations, older
# first, in the account's durable state unless HEAVY_SLOT_DIR names a namespace of its own.
GATE_LOCK = "/tmp/studywright-gate-suite.lock"
ADMISSION_STATE = "Library/Application Support/vocaro-engineering"
ADMISSION_LOGS = ("admissions.1.jsonl", "admissions.jsonl")
# The simulators' devices, relative to the home: a booted device's processes name its directory.
DEVICES = "Library/Developer/CoreSimulator/Devices"
DEVICE = re.compile(r"[0-9A-F]{8}-(?:[0-9A-F]{4}-){3}[0-9A-F]{12}", re.IGNORECASE)
# The device record, relative to the home, as devices.py finds it, which VOCARO_DEVICE_RECORD
# overrides for a test's; its key for the Mac it defers physical devices to; and a physical
# device's hardware UDID, as devices.PHYSICAL matches it.
DEVICE_RECORD = "Library/Application Support/vocaro-engineering/device-handoff.json"
DEFERRED = "physicalDevicesRecordedOn"
PHYSICAL = re.compile(r"[0-9A-F]{8}-[0-9A-F]{16}|[0-9A-F]{40}")
# The process that holds a simulator pool member's reservation, {"pid", "start"}, as
# simulators/pool.py records it when it reserves the member.
HOLDER = "holderProcess"
# A program inside an Xcode, such as the DTServiceHub that xcodebuild starts for a test run,
# which launchd adopts once xcodebuild has ended.
XCODE = re.compile(r"[^\s'\"]*/Xcode[^/\s'\"]*\.app/Contents/")
# Characters of a leftover's command line that the census reports.
COMMAND_LIMIT = 1000
# How ps prints a process's start (lstart), with its spacing collapsed.
STARTED = "%a %b %d %H:%M:%S %Y"
# What the work's Python processes are given: no bytecode, whose __pycache__ directories a
# project's .gitignore need not name, and which would leave its checkout with changes (claim).
NO_BYTECODE = {"PYTHONDONTWRITEBYTECODE": "1"}

_output = threading.Lock()
_stop = threading.Event()


class Refusal(RuntimeError):
    """The build host will not start the work, and nothing of it ran. A busy build host cannot start
    it now; any other refusal is its configuration's, and fix says what fixing that takes."""

    def __init__(self, reason, fix=None, busy=False):
        super().__init__(reason)
        self.fix, self.busy = fix, busy


def emit(kind, data):
    """Write one framed line. A closed connection is a stop: nobody is left to read the result."""
    if not data.endswith(b"\n"): data += b"\n"
    with _output:
        try:
            sys.stdout.buffer.write(kind + b" " + data)
            sys.stdout.buffer.flush()
        except OSError:
            _stop.set()


def say(text):
    emit(b"O", f"build-host: {text}".encode())


def output(command, environment=None):
    try:
        result = subprocess.run(command, capture_output=True, text=True, env=environment, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def xcode(developer_dir):
    """`xcodebuild -version` of the Xcode at developer_dir, or None."""
    if developer_dir and Path(developer_dir).is_dir():
        return output(["xcrun", "xcodebuild", "-version"], {**os.environ, "DEVELOPER_DIR": developer_dir})
    return None


def mac_name():
    return output(["scutil", "--get", "LocalHostName"]) or os.uname().nodename


def identity(developer_dir):
    """The Mac a result was measured on, its macOS and the Xcode at developer_dir."""
    number = lambda name: int(output(["sysctl", "-n", name]) or 0) or None
    return {"name": mac_name(),
            "model": output(["sysctl", "-n", "hw.model"]), "cpus": number("hw.ncpu"),
            "memoryBytes": number("hw.memsize"), "macOS": output(["sw_vers", "-productVersion"]),
            "build": output(["sw_vers", "-buildVersion"]), "developerDir": developer_dir, "xcode": xcode(developer_dir)}


def record_path():
    return Path(os.environ.get("VOCARO_DEVICE_RECORD") or Path(pwd.getpwuid(os.getuid()).pw_dir) / DEVICE_RECORD)


def device_record():
    """The Mac its device record defers physical devices to, and the entries that hold a physical
    device, which `eng doctor` compares. It reads without the record's lock: every change replaces
    the record whole."""
    path = record_path()
    try:
        record = json.loads(path.read_text())
    except FileNotFoundError:
        return {"path": str(path), "deferredTo": None, "physical": []}
    except (OSError, ValueError) as error:
        return {"path": str(path), "error": f"it cannot be read: {error}"}
    entries = record.get("devices") if isinstance(record, dict) else None
    if not isinstance(entries, dict):
        return {"path": str(path), "error": "it is no device record"}
    held = sorted(key for key, entry in entries.items() if isinstance(entry, dict) and entry.get("state") != "released"
                  and PHYSICAL.fullmatch(str(entry.get("device") or "").upper()))
    return {"path": str(path), "deferredTo": record.get(DEFERRED), "physical": held}


def toolchain(developer_dir):
    """The Mac's identity with the Xcode at developer_dir, its default Xcode (`xcode-select -p`), the
    simulator runtimes the Xcode at developer_dir lists and its device record, which `eng doctor`
    compares."""
    found = identity(developer_dir)
    default = output(["xcode-select", "-p"])
    runtimes = None
    if found["xcode"]:
        listed = output(["xcrun", "simctl", "list", "runtimes", "-j"], {**os.environ, "DEVELOPER_DIR": developer_dir})
        try:
            runtimes = sorted(f"{runtime['name']} ({runtime['buildversion']})"
                              for runtime in json.loads(listed)["runtimes"] if runtime.get("isAvailable"))
        except (TypeError, ValueError, KeyError):
            pass
    return {**found, "defaultDeveloperDir": default, "defaultXcode": xcode(default), "runtimes": runtimes,
            "deviceRecord": device_record()}


def image_file(record):
    """The file of the disk image that a record of `simctl runtime list` names, and None; or None and
    why no such file can be read. simctl names a disk image by its own path, and a cryptex by its
    asset's AssetData folder, whose Restore folder holds the image beside the cryptex's signatures.
    Either file's size is the record's sizeBytes (Xcode 27.2 on macOS 27.0.1 and 27.2, October 2026)."""
    path, size = record.get("path"), record.get("sizeBytes")
    if not path or not isinstance(size, int):
        return None, "simctl names no path and size of its image"
    path = Path(path)
    try:
        candidates = [path] if path.is_file() else sorted((path / "Restore").glob("*.dmg"))
        matching = [candidate for candidate in candidates if candidate.is_file() and candidate.stat().st_size == size]
    except OSError as error:
        return None, f"{path} cannot be read: {error}"
    if len(matching) != 1:
        return None, f"{path} holds no single disk image of its {size} bytes"
    if not os.access(matching[0], os.R_OK):
        return None, f"{matching[0]} cannot be read"
    return str(matching[0]), None


def images(developer_dir):
    """The runtime disk images `simctl runtime list` reports, each with the runtime it holds named as
    toolchain names it, NAME (BUILD), and its image file, or None when simctl lists none."""
    listed = output(["xcrun", "simctl", "runtime", "list", "-j"], {**os.environ, "DEVELOPER_DIR": developer_dir})
    try:
        records = list(json.loads(listed).values())
    except (TypeError, ValueError, AttributeError):
        return None
    found = []
    for record in records:
        if not isinstance(record, dict) or not record.get("build"): continue
        bundle = Path(record.get("runtimeBundlePath") or "")
        name = bundle.stem if bundle.suffix == ".simruntime" else \
            f"{PLATFORMS.get(record.get('platformIdentifier'), record.get('platformIdentifier'))} {record.get('version')}"
        file, unreadable = image_file(record)
        found.append({"runtime": f"{name} ({record['build']})", "name": name, "build": record["build"],
                      "identifier": record.get("identifier"), "state": record.get("state"),
                      "sizeBytes": record.get("sizeBytes"), "architectures": record.get("supportedArchitectures") or [],
                      "file": file, "unreadable": unreadable})
    return sorted(found, key=lambda image: image["runtime"])


def stage(directory, clear):
    """The free bytes where runtime images are staged, and the staged images an earlier copy left
    there, which clear removes. Only clear creates the directory."""
    directory = Path(directory)
    if clear: directory.mkdir(parents=True, exist_ok=True)
    left = sorted(path.name for path in directory.glob("*.dmg*")) if directory.is_dir() else []
    if clear:
        for name in left: (directory / name).unlink(missing_ok=True)
    existing = next(path for path in (directory, *directory.parents) if path.exists())
    return {"free": shutil.disk_usage(existing).free, "left": left}


def receive(stream, directory, build, size, developer_dir, progress=None):
    """Stage a runtime image read from stream in directory, then have simctl add it, moving it into
    simctl's own storage. Only an image of exactly size bytes is added, and whatever fails removes
    the staged file, so a failed copy adds nothing and leaves nothing behind. Return None once the
    image is added, else why it was not. progress, given the bytes received so far, may raise to stop."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    partial, staged = directory / f"{build}.dmg.partial", directory / f"{build}.dmg"
    try:
        received = 0
        with partial.open("wb") as file:
            while not _stop.is_set():
                chunk = stream.read(CHUNK)
                if not chunk: break
                file.write(chunk)
                received += len(chunk)
                if progress is not None: progress(received)
        if _stop.is_set(): return "stopped before it was added"
        if received != size: return f"received {received} of its {size} bytes"
        partial.rename(staged)
        # In a session of its own, so a terminal's interrupt cannot end it halfway through.
        added = subprocess.run(["xcrun", "simctl", "runtime", "add", str(staged), "--move"], stdin=subprocess.DEVNULL,
                               capture_output=True, text=True, env={**os.environ, "DEVELOPER_DIR": developer_dir},
                               start_new_session=True)
        if added.returncode:
            lines = [line.strip() for line in (added.stderr + added.stdout).splitlines() if line.strip()]
            return f"simctl runtime add exited {added.returncode}: {lines[-1] if lines else 'no output'}"
        return None
    except OSError as error:
        return f"staging it in {directory} failed: {error}"
    finally:
        # simctl removes the staged file once it holds the image; a failure leaves it to remove.
        for path in (partial, staged): path.unlink(missing_ok=True)


class Usage(ctypes.Structure):
    """<sys/resource.h>'s struct rusage_info_v4, which proc_pid_rusage fills for RUSAGE_INFO_V4."""
    _fields_ = [("uuid", ctypes.c_uint8 * 16), ("counters", ctypes.c_uint64 * 16), ("bytesRead", ctypes.c_uint64),
                ("bytesWritten", ctypes.c_uint64), ("times", ctypes.c_uint64 * 9), ("logicalWrites", ctypes.c_uint64),
                ("rest", ctypes.c_uint64 * 7)]


_libproc = []


def disk_writes(pid):
    """The bytes a process has written to files since it started, or None where they cannot be read.
    proc_pid_rusage reads the account's own processes without sudo. Its logical writes count a write
    when it is made, flushed or not, and to a file deleted since, but no write to a pipe or /dev/null;
    its disk I/O counted a buffered 4 MiB not at all, only what the process flushed itself (macOS
    27.0, October 2026)."""
    if not _libproc:
        try: _libproc.append(ctypes.CDLL("/usr/lib/libproc.dylib"))
        except OSError: _libproc.append(None)
    usage = Usage()
    if _libproc[0] is None or _libproc[0].proc_pid_rusage(pid, 4, ctypes.byref(usage)) != 0:
        return None
    return usage.logicalWrites


def listing(command):
    """A command's standard output whatever its status, or None when it cannot run: lsof exits 1
    when it finds nothing, or when a process it looked at has ended."""
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return None


def process_table(text):
    """The processes a `ps -axww -o pid=,ppid=,uid=,lstart=,args=` listing names, by PID: each
    one's parent, user ID, start with its spacing collapsed, and command line."""
    table = {}
    for line in (text or "").splitlines():
        parts = line.split(None, 8)
        if len(parts) < 8 or not all(part.isdigit() for part in parts[:3]): continue
        table[int(parts[0])] = {"parent": int(parts[1]), "uid": int(parts[2]), "start": " ".join(parts[3:8]),
                                "args": parts[8] if len(parts) > 8 else ""}
    return table


def working_directories(text):
    """Each process's working directory, from an `lsof -Fpn -d cwd` listing."""
    found, pid = {}, None
    for line in (text or "").splitlines():
        if line[:1] == "p" and line[1:].isdigit(): pid = int(line[1:])
        elif line[:1] == "n" and pid is not None: found[pid] = line[1:]
    return found


def started(start):
    """A start as ps prints it, in seconds since the epoch, or None."""
    try: return time.mktime(time.strptime(start, STARTED))
    except (TypeError, ValueError, OverflowError): return None


def identities(records):
    """(PID, start with its spacing collapsed) of each record that names an integer PID, the start
    None where a record has none."""
    return [(record["pid"], " ".join(record["start"].split()) if isinstance(record.get("start"), str) else None)
            for record in records if isinstance(record, dict) and isinstance(record.get("pid"), int)]


def guard_owners():
    """The owners heavy-work admission's guard records, as resources.guard_owners reads them."""
    try:
        state = json.loads(Path(os.environ.get("SW_GATE_LOCK_FILE", GATE_LOCK) + ".warm-push-guard").read_text())
    except (OSError, ValueError):
        return []
    owners = state.get("owners") if isinstance(state, dict) and state.get("vocaroEngineering") == 1 else None
    if not isinstance(owners, dict): return []
    return identities([owner for owner in owners.values()
                       if isinstance(owner, dict) and isinstance(owner.get("start"), str)])


def unended():
    """(PID, when it was admitted, in seconds since the epoch) of each admission the admission log
    records without an end: one still held, or one SIGKILL ended."""
    directory = Path(os.environ["HEAVY_SLOT_DIR"]) if os.environ.get("HEAVY_SLOT_DIR") else \
        Path(pwd.getpwuid(os.getuid()).pw_dir) / ADMISSION_STATE
    admitted, ended = {}, set()
    for name in ADMISSION_LOGS:
        try: lines = (directory / name).read_bytes().splitlines()
        except OSError: continue
        for line in lines:
            try: entry = json.loads(line)
            except ValueError: continue
            if not isinstance(entry, dict): continue
            if entry.get("event") == "admitted" and isinstance(entry.get("pid"), int):
                admitted[entry.get("identity")] = entry
            elif entry.get("event") == "ended": ended.add(entry.get("identity"))
    found = []
    for identity, entry in admitted.items():
        if identity in ended: continue
        try: moment = datetime.fromisoformat(entry["time"]).timestamp()
        except (TypeError, ValueError, KeyError): moment = None
        found.append((entry["pid"], moment))
    return found


def dispatchers(checkouts):
    """The runs that each project's checkout slot locks record, as claim writes them: (PID, start)."""
    records = []
    for path in sorted(Path(checkouts).glob("*/*.lock")):
        try: records.append(json.loads(path.read_text()))
        except (OSError, ValueError): continue
    return identities(records)


def reservations():
    """(UDID, PID, start with its spacing collapsed) of each process that the device record names as
    holding a simulator it has not released: the process that reserved a pool member, as
    simulators/pool.py records it, and an active run's process. With them, why the record could not
    be read, or None. It reads without the record's lock: every change replaces the record whole."""
    path = record_path()
    try:
        record = json.loads(path.read_text())
    except FileNotFoundError:
        return [], None
    except (OSError, ValueError) as error:
        return [], (f"the device record {path}, which could not be read ({error}), so no reservation accounts "
                    "for a simulator")
    entries = record.get("devices") if isinstance(record, dict) else None
    if not isinstance(entries, dict):
        return [], f"the device record {path}, which is no device record, so no reservation accounts for a simulator"
    found = []
    for entry in entries.values():
        if not isinstance(entry, dict) or entry.get("state") == "released": continue
        udid = str(entry.get("device") or "").upper()
        if not DEVICE.fullmatch(udid): continue
        holders = [entry.get(HOLDER)]
        if entry.get("activeRun"):
            holders.append({"pid": entry.get("hostProcessIdentifier"), "start": entry.get("hostProcessStart")})
        found += [(udid, pid, start) for pid, start in identities(holders) if start is not None]
    return found, None


def live_runs(table, recorded, admitted):
    """The PIDs of live runs: each recorded (PID, start) whose process still has that start (a record
    without one counts while its PID runs), and each admission the log has not ended whose process
    started no later than it was admitted, since a process that took its PID over started later."""
    roots = {pid for pid, start in recorded if pid in table and start in (None, table[pid]["start"])}
    for pid, moment in admitted:
        began = started(table[pid]["start"]) if pid in table else None
        if pid in table and (moment is None or began is None or began <= moment + 1): roots.add(pid)
    return roots


def forms(path):
    """The forms a directory takes in a command line or in lsof's report, each with a closing slash:
    as given, resolved, and with and without the /private that /tmp and /var resolve to."""
    found = set()
    for form in {str(path), os.path.realpath(path)}:
        form = form.rstrip("/")
        if not form: continue
        found.add(form + "/")
        if form.startswith("/private/"): found.add(form[len("/private"):] + "/")
        if form.startswith(("/tmp/", "/var/")): found.add("/private" + form + "/")
    return sorted(found)


def lineage(table, pid):
    """pid and its ancestors in table, nearest first."""
    line = []
    while pid in table and pid not in line:
        line.append(pid)
        pid = table[pid]["parent"]
    return line


def leftovers(root, listed=listing, written=disk_writes):
    """The account's processes that run from a checkout under root, the temporary directory or a
    simulator's device, and that no live run accounts for: what `eng remote` prints before it sends
    work, and `eng doctor` reports. It reads and reports, and stops nothing.

    A process runs from a place when its working directory lies in it or its command line names a
    path in it; a program under /System is macOS's own. It is a live run's when it or an ancestor
    is a live admission owner, as the guard or the admission log records it, or a live run of this
    file, as its checkout's slot lock records it: a light lane and a provision lane take no
    admission. Some of a run's processes launchd starts or adopts, and those a live run holds are
    its own, with everything below them. A simulator's processes descend from launchd_sim, never
    from a run, so a device is a live run's while the device record holds it for one of that run's
    processes, as the pool records the process that reserved a member, or while one of them names
    it. An Xcode program, such as the DTServiceHub that xcodebuild leaves behind, is a live run's
    when it started in a checkout after a live admission there began. Each leftover whose parent is
    no leftover is listed, with its PID, start, command, how much it has written, and each of its
    descendants with what that one has written."""
    home, uid = Path.home(), os.getuid()
    table = process_table(listed(["ps", "-axww", "-o", "pid=,ppid=,uid=,lstart=,args="]))
    if not table: return {"error": "ps listed no processes"}
    unchecked = []
    found = listed(["lsof", "-nPw", "-a", "-u", str(uid), "-d", "cwd", "-Fpn"])
    if not found:
        unchecked.append("the processes' working directories, which lsof did not list, so only their command lines "
                         "were read")
    directories = working_directories(found)
    temporary = os.environ.get("TMPDIR") or (listed(["getconf", "DARWIN_USER_TEMP_DIR"]) or "").strip()
    if not temporary: unchecked.append("the temporary directory, which neither TMPDIR nor getconf named")
    checkouts = home / root / "checkouts"
    places = [(kind, forms(path)) for kind, path in
              [("checkout", checkouts), ("temporary directory", temporary), ("simulator", home / DEVICES)] if path]

    ancestry = lambda pid: lineage(table, pid)
    slots = forms(checkouts)

    def slot(path):
        """The checkout slot a path lies in, as (project, slot), or None."""
        for prefix in slots:
            if path and (path + "/").startswith(prefix):
                parts = path[len(prefix):].split("/")
                if len(parts) > 1 and parts[0] and parts[1]: return parts[0], parts[1]
        return None

    def place(pid):
        """Where the process runs from: (kind, the path that places it there, device or None), or None."""
        cwd = directories.get(pid)
        for kind, prefixes in places:
            for prefix in prefixes:
                if cwd and (cwd + "/").startswith(prefix):
                    path = cwd
                else:
                    mention = re.search(re.escape(prefix) + r"[^\s'\"]*", table[pid]["args"])
                    if mention is None: continue
                    path = mention.group(0)
                device = DEVICE.match(path[len(prefix):]) if kind == "simulator" else None
                return kind, path, device and device.group(0).upper()
        return None

    owners, logged = guard_owners(), unended()
    roots = live_runs(table, owners + dispatchers(checkouts), logged)
    own = set(ancestry(os.getpid()))
    runs = {pid for pid in table if roots.intersection(ancestry(pid))}
    # The devices the device record holds for one of a live run's processes, by UDID, and those a
    # live run names: a run can name a device by its kind alone, as eng simulator-tests does.
    reserved, problem = reservations()
    if problem: unchecked.append(problem)
    held = {udid for udid, pid, start in reserved if pid in runs and table[pid]["start"] == start}
    named = " ".join(table[pid]["args"] for pid in runs).upper()
    # When each live admission began, by the checkout slot its owner works in: as the log records
    # it, or else from the start of the owner the guard records.
    begun = {pid: started(start) for pid, start in owners if pid in table and table[pid]["start"] == start}
    begun.update((pid, moment) for pid, moment in logged
                 if moment is not None and live_runs(table, [], [(pid, moment)]))
    windows = {}
    for pid, moment in begun.items():
        where = slot(directories.get(pid))
        if where is not None and moment is not None: windows[where] = min(moment, windows.get(where, moment))

    def holds(pid, kind, path, device):
        """Whether a live run holds the process: its simulator, or, for an Xcode program, the
        checkout it started in after that run's admission there began."""
        if device: return device in held or device in named
        if kind != "checkout" or not XCODE.match(table[pid]["args"]): return False
        since, began = windows.get(slot(path)), started(table[pid]["start"])
        return since is not None and began is not None and began >= since - 1

    placed = {}
    for pid, process in table.items():
        # macOS's own agents, which launchd starts, can work in the temporary directory, as
        # IMTransferAgent did on both Macs (macOS 27, October 2026).
        if process["uid"] != uid or pid in own or pid in runs or process["args"].startswith("/System/"): continue
        where = place(pid)
        if where is not None: placed[pid] = where
    # What a live run holds is its own with everything below it, as a simulator's processes are.
    holding = {pid for pid, where in placed.items() if holds(pid, *where)}
    if holding: runs |= {pid for pid in table if holding.intersection(ancestry(pid))}
    candidates = {pid: where for pid, where in placed.items() if pid not in runs}
    children = {}
    for pid, process in table.items(): children.setdefault(process["parent"], []).append(pid)
    # This file stays ASCII: it reaches the build host as a command-line argument.
    command = lambda pid: table[pid]["args"][:COMMAND_LIMIT] + ("..." if len(table[pid]["args"]) > COMMAND_LIMIT else "")
    now, rows = time.time(), []
    for pid, (kind, path, device) in candidates.items():
        # A leftover below another is listed among that one's descendants.
        if any(above in candidates for above in ancestry(pid)[1:]): continue
        descendants, pending, seen = [], list(children.get(pid, [])), {pid}
        while pending:
            child = pending.pop()
            if child in seen or child in own or child in runs: continue
            seen.add(child)
            descendants.append({"pid": child, "command": command(child), "written": written(child)})
            pending.extend(children.get(child, []))
        began = started(table[pid]["start"])
        rows.append({"pid": pid, "parent": table[pid]["parent"], "start": table[pid]["start"],
                     "seconds": round(now - began) if began is not None else None, "command": command(pid),
                     "place": kind, "path": path, **({"simulator": device} if device else {}),
                     "cwd": directories.get(pid), "written": written(pid),
                     "descendants": sorted(descendants, key=lambda descendant: -(descendant["written"] or 0))})
    total = lambda row: (row["written"] or 0) + sum(descendant["written"] or 0 for descendant in row["descendants"])
    return {"leftovers": sorted(rows, key=lambda row: (-total(row), row["pid"])), "unchecked": unchecked,
            "processes": sum(1 for process in table.values() if process["uid"] == uid)}


def device_users(request, listed=listing, written=disk_writes):
    """What here may still use the physical device a run was given, for `devices recover`: each of
    the account's processes whose command line names its UDID, live or left over, among them the
    run's own build_host.py, whose plan names it while the run lasts; and each leftover that started
    since the run did. It reads and reports, and stops nothing."""
    udid = request["udid"].upper()
    table = process_table(listed(["ps", "-axww", "-o", "pid=,ppid=,uid=,lstart=,args="]))
    if not table: return {"error": "ps listed no processes"}
    own, uid = set(lineage(table, os.getpid())), os.getuid()
    naming = [{"pid": pid, "start": process["start"], "command": process["args"][:COMMAND_LIMIT]}
              for pid, process in table.items()
              if process["uid"] == uid and pid not in own and udid in process["args"].upper()]
    census = leftovers(request["root"], listed, written)
    if "error" in census: return census
    since = request.get("since")
    later = [row for row in census["leftovers"] if since is None or (started(row["start"]) or since) >= since - 1]
    return {**census, "leftovers": later, "naming": naming}


def answer(request):
    """build_host.py's answer to a request other than a plan, for `eng doctor`, `eng remote`,
    `eng remote setup` and `devices recover`."""
    if "toolchain" in request: return toolchain(request["toolchain"])
    if "leftovers" in request or "device" in request:
        # A census that fails says why: it must never stop the work it is printed before.
        try: return leftovers(request["leftovers"]) if "leftovers" in request else device_users(request["device"])
        except Exception as error: return {"error": f"{type(error).__name__}: {error}"}
    if "images" in request:
        return {"images": images(request["images"]), **stage(Path.home() / request["staging"], request.get("clear"))}
    # A stop or a dropped connection ends the copy, and the staged file is removed.
    for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(number, lambda *_: _stop.set())
    copy = request["receive"]
    return {"failed": receive(sys.stdin.buffer, Path.home() / copy["staging"], copy["build"], copy["size"],
                              copy["developerDir"])}


def git(*arguments, cwd=None):
    result = subprocess.run(["git", *arguments], cwd=cwd, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f"git {' '.join(arguments)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def changes(checkout):
    """Tracked changes and nonignored untracked files, which a run left in a checkout."""
    return git("status", "--porcelain", "--untracked-files=normal", cwd=checkout) if checkout.exists() else ""


def claim(base, count, directory, wait=True):
    """Lock a free checkout slot without changes, waiting while every one is in use, if wait.

    The lock is an flock the run's command inherits, so it holds while that command lives. A
    checkout with changes is left as it is, for inspection, and passed over. The lock's holder
    writes its PID and start into it, so that a census of leftover processes knows a live run's
    processes from those a lost run left (leftovers).
    """
    base.mkdir(parents=True, exist_ok=True)
    started, reported, named = time.monotonic(), None, set()
    record = json.dumps({"pid": os.getpid(), "start": output(["ps", "-p", str(os.getpid()), "-o", "lstart="])})
    while not _stop.is_set():
        changed = 0
        for index in range(count):
            descriptor = os.open(base / f"{index}.lock", os.O_RDWR | os.O_CREAT, 0o644)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(descriptor)
                continue
            # Before git status, whose process is this run's too.
            os.ftruncate(descriptor, 0)
            os.pwrite(descriptor, (record + "\n").encode(), 0)
            checkout = base / str(index) / directory
            left = changes(checkout)
            if not left:
                return index, descriptor
            os.close(descriptor)
            changed += 1
            if index not in named:
                say(f"passing over {checkout}, which a run left with changes:\n{left}")
                named.add(index)
        if changed == count:
            repository = base.parents[1] / "repositories" / f"{directory}.git"
            raise Refusal(f"every checkout under {base} has changes a run left",
                          f"inspect and remove them, each with `git --git-dir {repository} worktree remove --force PATH`")
        if not wait:
            raise Refusal(f"every checkout of {directory} is in use", busy=True)
        waited = int(time.monotonic() - started)
        if waited >= CLAIM_TIMEOUT:
            raise Refusal(f"no checkout of {directory} came free in {CLAIM_TIMEOUT}s", busy=True)
        if reported is None or waited - reported >= REPORT_SECONDS:
            say(f"waiting ({waited}s): every checkout of {directory} is in use")
            reported = waited
        time.sleep(1)
    return None


def place(repository, checkout, commit, ref, origin=None):
    """Check commit out, detached, in a persistent worktree whose ignored inputs and builds stay.

    Only the toolkit names an origin: a heavy lane's policy refresh fetches it."""
    if origin and output(["git", "--git-dir", str(repository), "config", "--get", "remote.origin.url"]) != origin:
        git("--git-dir", str(repository), "config", "remote.origin.url", origin)
    if checkout.exists():
        git("checkout", "--detach", "--quiet", commit, cwd=checkout)
    else:
        git("--git-dir", str(repository), "worktree", "prune")
        git("--git-dir", str(repository), "worktree", "add", "--detach", "--quiet", str(checkout), commit)
    if git("rev-parse", "HEAD", cwd=checkout) != commit:
        raise RuntimeError(f"{checkout} is not at {commit}")
    # The detached worktree keeps the commit reachable; the pushed ref only carried it here.
    subprocess.run(["git", "--git-dir", str(repository), "update-ref", "-d", ref], capture_output=True)


def credentials(environment, path):
    """Give the run's git the build host's credential file and no other helper.

    SwiftPM clones private dependencies with git, and an SSH session cannot unlock the keychain
    that osxkeychain, Xcode's git default, reads. The variables reach only this run, so the
    owner's own git configuration on the build host is unchanged."""
    count = int(environment.get("GIT_CONFIG_COUNT", "0"))
    for offset, value in enumerate(["", f"store --file={shlex.quote(str(path))}"]):
        environment[f"GIT_CONFIG_KEY_{count + offset}"] = "credential.helper"
        environment[f"GIT_CONFIG_VALUE_{count + offset}"] = value
    environment["GIT_CONFIG_COUNT"] = str(count + 2)


def defer_devices(kit, mac):
    """Make this Mac's device record defer physical devices to mac's, the sending Mac's.

    A paired iPhone or iPad is reachable from both Macs, so it is reserved in one record only;
    this one then refuses to reserve it. A record that defers them to another Mac, or holds one,
    is a configuration problem for the owner, so the work does not start."""
    try:
        deferred = subprocess.run([sys.executable, "-m", "vocaro_engineering.devices", "defer", mac], capture_output=True,
                                  text=True, env={**os.environ, "PYTHONPATH": str(kit / "src/python"), **NO_BYTECODE},
                                  timeout=60)
    except subprocess.TimeoutExpired:
        deferred = subprocess.CompletedProcess([], 1, "", "it did not finish in 60s")
    if deferred.returncode:
        problem = deferred.stderr.strip().splitlines()[-1:] or [f"exit {deferred.returncode}"]
        raise Refusal(f"its device record cannot defer physical devices to {mac}: {problem[0]}",
                      "decide which Mac's record holds physical devices and resolve that in the build host's record "
                      "(guides/shared-host.md, Physical devices and the build host)")
    if deferred.stdout.strip(): say(deferred.stdout.strip())


def relay(stream):
    for line in iter(stream.readline, b""):
        emit(b"O", line)


def follow(path, done):
    """Send the lane's log from its start as it grows. The lane creates it afresh."""
    stream, pending = None, b""
    try:
        while True:
            finished = done.is_set()
            if stream is None:
                try: stream = path.open("rb")
                except FileNotFoundError: pass
            if stream is not None:
                *lines, pending = (pending + stream.read()).split(b"\n")
                for line in lines: emit(b"L", line)
            if finished: break
            time.sleep(0.2)
    finally:
        if stream is not None: stream.close()
    if pending: emit(b"L", pending)


def send_back(directory, paths):
    """Send each of these files under directory that exists, whole: an `F` line naming it, then `D`
    lines of its bytes in base64, so that a file's every byte arrives and each line stays one frame."""
    for path in paths:
        try: stream = (directory / path).open("rb")
        except OSError: continue
        with stream:
            emit(b"F", json.dumps({"path": path}).encode())
            for chunk in iter(lambda: stream.read(FILE_CHUNK), b""):
                emit(b"D", base64.b64encode(chunk))


def step(command, checkout, environment, lock, log):
    """Run one eng command; a stop sends it SIGTERM, which it carries out as a supervised stop."""
    if log is not None: log.unlink(missing_ok=True)
    child = subprocess.Popen(command, cwd=checkout, env=environment, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True,
                             pass_fds=(lock,))
    done = threading.Event()
    threads = [threading.Thread(target=relay, args=(child.stdout,), daemon=True)]
    if log is not None: threads.append(threading.Thread(target=follow, args=(log, done), daemon=True))
    for thread in threads: thread.start()
    forwarded = False
    while child.poll() is None:
        if _stop.is_set() and not forwarded:
            say("stop requested; sending SIGTERM to the run, which stops its command and leaves admission")
            try: os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError: pass
            forwarded = True
        time.sleep(0.1)
    done.set()
    for thread in threads: thread.join(DRAIN_SECONDS)
    return child.returncode


def watch():
    """The end of standard input is the dispatcher's stop.

    It reads the descriptor, not sys.stdin: Python 3.9 aborts at exit when a daemon thread is
    blocked holding the buffered reader's lock (observed on macOS 27.2, October 2026)."""
    try:
        while os.read(0, 65536): pass
    except OSError:
        pass
    _stop.set()


def main(plan):
    for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(number, lambda *_: _stop.set())
    threading.Thread(target=watch, daemon=True).start()
    result = {"started": time.time(), "exitStatus": None, "stopped": False,
              "host": identity(plan.get("developerDir"))}
    # Until the first command starts, a failure is a refusal: nothing ran. The dispatcher may run
    # the work on its own Mac instead only when the refusal is busy.
    begun, back = False, None
    try:
        expected, found = plan.get("xcode"), result["host"]["xcode"]
        if expected and found != expected:
            version = lambda text: " ".join(text.split())
            raise Refusal(f"the Xcode at {plan['developerDir']} is {version(found or 'missing')}, not the dispatcher's "
                          f"{version(expected)}", "install this Mac's Xcode at that path on the build host "
                          "(guides/shared-host.md, Setting up a build host), or select with DEVELOPER_DIR an Xcode "
                          "both Macs have at the same path")
        root, project, toolkit = Path.home() / plan["root"], plan["project"], plan.get("toolkit")
        base = root / "checkouts" / project["directory"]
        claimed = claim(base, plan["checkouts"], project["directory"], wait=not plan.get("decline"))
        if claimed is None:
            result["stopped"] = True
            return result
        index, lock = claimed
        checkout = base / str(index) / project["directory"]
        place(root / project["repository"], checkout, project["commit"], project["ref"], project.get("origin"))
        kit = checkout
        if toolkit is not None:
            kit = base / str(index) / toolkit["directory"]
            place(root / toolkit["repository"], kit, toolkit["commit"], toolkit["ref"], toolkit.get("origin"))
        result.update(checkout=str(checkout), slot=index)
        say(f"{checkout} at {project['commit'][:12]}")
        environment = {**os.environ, "VOCARO_ENGINEERING_ROOT": str(kit), **NO_BYTECODE}
        if plan.get("deviceRecord"): defer_devices(kit, plan["deviceRecord"])
        # The reservations, held in the sending Mac's record, that this run may use (devices.LEASE).
        if plan.get("lease"): environment["VOCARO_DEVICE_LEASE"] = json.dumps(plan["lease"])
        if plan.get("developerDir"): environment["DEVELOPER_DIR"] = plan["developerDir"]
        if (root / "git-credentials").is_file(): credentials(environment, root / "git-credentials")
        eng = [sys.executable, str(kit / "bin/eng"), "--project", str(checkout)]
        steps = [("provision", [*eng, "run", "provision"], checkout / ".build/engineering/provision/run.log")] \
            if plan.get("provision") else []
        steps.append(("work", [*eng, *plan["work"]], checkout / plan["log"] if plan.get("log") else None))
        returns = plan.get("returns")
        if returns:
            back = checkout / returns["directory"], returns["paths"]
            # The work writes them afresh, so none that an earlier run left goes back as this run's.
            for path in back[1]: (back[0] / path).unlink(missing_ok=True)
        for stage, command, log in steps:
            if _stop.is_set(): break
            begun = True
            result.update(stage=stage, exitStatus=step(command, checkout, environment, lock, log))
            if result["exitStatus"] != 0: break
        result["stopped"] = _stop.is_set()
        receipt = checkout / plan["receipt"]
        # A command that failed before it wrote a receipt leaves the previous run's in place.
        fresh = receipt.is_file() and receipt.stat().st_mtime >= result["started"]
        if fresh:
            result["receipt"] = json.loads(receipt.read_text())
        # `eng run --if-free` declined the lane before running it, and wrote no receipt.
        if plan.get("decline") and result.get("stage") == "work" and result["exitStatus"] == plan["busy"] \
                and not fresh:
            result.update(refused="its heavy-work queue cannot take the lane now", busy=True)
    except Refusal as refusal:
        result.update(refused=str(refusal), busy=refusal.busy, fix=refusal.fix)
    except (OSError, RuntimeError, ValueError) as error:
        result["error" if begun else "refused"] = str(error)
    finally:
        result["finished"] = time.time()
        # However the work ended, a stop or a failure included, what it wrote that the plan asks back.
        if back is not None and begun:
            try: send_back(*back)
            except OSError as error: say(f"could not send back what the run wrote in {back[0]}: {error}")
        emit(b"R", json.dumps(result).encode())
    return result


if __name__ == "__main__":
    request = json.loads(sys.argv[1])
    if {"toolchain", "images", "receive", "leftovers", "device"} & request.keys(): print(json.dumps(answer(request)))
    else: main(request)
