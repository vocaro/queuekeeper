"""The second Mac's side of `eng remote`, sent over SSH and run there by that Mac's own Python.

It imports nothing from the toolkit, because it runs before the toolkit is checked out there,
and Python 3.9, a Mac's system Python, runs it. It claims a persistent checkout of the project,
places the dispatched commits, and runs `eng run` or `eng admit` there under the second Mac's
own heavy-work admission, with its device record deferring physical devices to the sending Mac's
and the run given the reservations it carries. Standard output carries framed lines: `O` for
output, `L` for the lane's log as it grows, then, once the work has ended, an `F` naming each file the plan asks back,
such as a lane group's lanes' logs and receipts, followed by `D` lines of its bytes in base64, and
a closing `R` with the result as JSON. Standard input carries the dispatcher's words, a line each: `stop` stops
the run as a stop signal stops a local one, `attached` says the dispatcher heard the run start, and `received`
that it has the result. A plan with `detach` keeps every frame in a journal here, which an opening `J` announces
(Journal), and once the dispatcher has said `attached`, the end of standard input is a lost connection, not a stop:
the run goes on, for at most `detach`'s seconds more, and `{"reattach": ...}`, on a later connection, sends the
journal from its start and follows it; with `"stop"`, it stops that one run instead, for a dispatcher proven dead
(remote.stop_dead_dispatches). Without a journal, or before `attached`, that end stops the run, whether a stop,
the dispatcher's death or a dropped connection sent it (vocaro-engineering#321), and so does the plan's `silence`
seconds with no heartbeat, which is all that a Mac that crashed sends (vocaro-engineering#287). A journaled run the
dispatcher has heard start is never stopped by silence: a sleeping Mac is silent too. Given `{"toolchain": DEVELOPER_DIR}`
instead of a plan, it prints the Mac's Xcodes, simulator runtimes and device record for
`eng doctor`; given `{"images": ...}`, `{"receive": ...}` or `{"deleteRuntime": ...}`, it lists the Mac's
runtime images, adds one read from standard input or deletes one that no simulator but Xcode's
defaults uses, for `eng doctor` and `eng remote setup --runtimes`; given `{"leftovers": ROOT}`,
it lists the processes left over from runs there, which `eng remote` prints before it sends work,
and with `"advertise"` names as refs the commits the named repositories' checkouts hold, so that
the push that follows sends only what they lack, and with `"reap"` first shuts down the stale pool
simulators that census finds (reap), and with `"toolkitCheckout"`, alone too, fast-forwards this Mac's
own toolkit checkout to origin/main when it can, and says why not when it cannot (update_toolkit);
given `{"device": ...}`, what there may still use a physical device a run was given, for
`devices recover`; given `{"keychain": PATH}`, the Mac's name and whether that keychain file is
there, for `eng keychain recover`; given `{"keepUnlocked": ...}`, at the owner's terminal, it asks for
the login keychain's password and keeps it for runs to unlock that keychain with, for `eng keychain
keep-unlocked`; given `{"foreground": true}`, what the Mac lacks for foreground work started over
SSH and who holds its mac-foreground, for `eng remote foreground`; given `{"pressure": true}`, its
CPU's busy share, the limit admission sets on it and its memory pressure, for the merge queue's settle wait;
given `{"capacity": true}`, its spare capacity for heavy work, which `eng run` reads on both Macs
before it chooses one (remote.spare); given `{"admissionLog": SINCE}`, its admission log's lines from
SINCE, an epoch time, for `eng admissions usage`; given `{"screen": true}`, whether its console screen is locked, for
`eng host screen --remote` (screen).
"""
import base64
import contextlib
import ctypes
from datetime import datetime, timezone
import fcntl
import getpass
import glob
import json
import os
from pathlib import Path
import plistlib
import pwd
import re
import select
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import unquote, urlparse

# Seconds between reports while every checkout is in use, and before waiting for one gives up.
REPORT_SECONDS, CLAIM_TIMEOUT = 300, 5400
# Seconds the output of a finished command may take to drain: a descendant can hold the pipe.
DRAIN_SECONDS = 5
# Seconds between readings of a lane's wait record (relay_wait); admission rewords it at most once a poll.
WAIT_POLL = 0.5
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
# The host's admission limits file, beside its log, and its slot files' directory: the legacy user-temp
# namespace unless HEAVY_SLOT_DIR names one, as resources.py names them (#49).
ADMISSION_LIMITS = "admission-limits.json"
SLOTS = "pdfreflow-heavy-slots"
# The queue of exclusive and whole-host requests in the slot directory, and HEAVY_PLACE_SECONDS's default, how
# long a whole-host request that last asked then keeps the guaranteed slots, as resources.QUEUE and
# resources.PLACE_SECONDS have them (vocaro-engineering#174, #186).
QUEUE, PLACE_SECONDS = "exclusive-queue.json", 180
# Each admission limit the capacity reading needs, by its key in the limits file: its environment variable,
# whether it counts slots, and its default given the cores and the guaranteed slots, as resources.LIMIT_KEYS
# has them. The two must agree: eng chooses a Mac by what each Mac's admission would admit.
LIMIT_KEYS = {
    "slots": ("HEAVY_SLOTS", True, lambda cores, slots: 3),
    "slotsMax": ("HEAVY_SLOTS_MAX", True, lambda cores, slots: max(slots, cores // 2)),
    "busyLimit": ("HEAVY_BUSY_LIMIT", False, lambda cores, slots: 75.0),
    "settleSeconds": ("HEAVY_SETTLE_SECONDS", False, lambda cores, slots: 30.0),
}
# The simulators' devices, relative to the home: a booted device's processes name its directory.
DEVICES = "Library/Developer/CoreSimulator/Devices"
DEVICE = re.compile(r"[0-9A-F]{8}-(?:[0-9A-F]{4}-){3}[0-9A-F]{12}", re.IGNORECASE)
# The device record, relative to the home, as devices.py finds it, which VOCARO_DEVICE_RECORD
# overrides for a test's; its key for the Mac it defers physical devices to; and a physical
# device's hardware UDID, as devices.PHYSICAL matches it.
DEVICE_RECORD = "Library/Application Support/vocaro-engineering/device-handoff.json"
DEFERRED = "physicalDevicesRecordedOn"
# Device records older toolkits left, which devices.LEGACY names and disk still reads.
LEGACY_RECORDS = "/private/tmp/*device-handoff*.json"
# The setting that shows an app's "quit unexpectedly" alert, and where a foreground run that has it off
# keeps its value from before the run, relative to the home (crash_alerts.py).
CRASH_DIALOG = ("com.apple.CrashReporter", "DialogType")
CRASH_DIALOG_RECORD = "Library/Application Support/vocaro-engineering/crash-dialog.json"
# The processes whose windows are system alerts over a foreground run's windows: macOS's crash-report and
# other user notification alerts, and the password, keychain and permission prompts that stay the owner's.
ALERT_PROCESSES = ("UserNotificationCenter", "Problem Reporter", "CrashReporter", "SecurityAgent", "coreautha",
                   "universalAccessAuthWarn", "CoreServicesUIAgent")
PHYSICAL = re.compile(r"[0-9A-F]{8}-[0-9A-F]{16}|[0-9A-F]{40}")
# Where a Mac keeps its users' keychain files, relative to the home, and its login keychain's file there.
KEYCHAINS = "Library/Keychains"
LOGIN_KEYCHAIN = "login.keychain-db"
# The file in eng remote's root that holds the login keychain's password, once the owner has run
# `eng keychain keep-unlocked`, from which each run unlocks that keychain in its own session.
KEYCHAIN_PASSWORD = "keychain-password"
# What SecKeychainGetStatus sets for an unlocked keychain (kSecUnlockStateStatus), and the OSStatus of
# a wrong password (errSecAuthFailed) and of a missing keychain file (errSecNoSuchKeychain).
UNLOCK_STATE, AUTH_FAILED, NO_SUCH_KEYCHAIN = 1, -25293, -25294
# How many passwords keep_unlocked asks for before it gives up, as sudo does.
PASSWORD_TRIES = 3
# The process that holds a simulator pool member's reservation, {"pid", "start"}, as
# simulators/pool.py records it when it reserves the member.
HOLDER = "holderProcess"
# Set on a reservation that holds its member only while its holderProcess runs, as a run's own pool does
# (simulators/pool.py, Pool(alone=True)), and never on `eng simulator-pool take`'s, whose session holds the
# member by its word once that command has ended. Only such a reservation may be released before its expected
# release (release_dead; vocaro-engineering#156).
ALONE = "heldOnlyWhileHolderRuns"
# What ps lists for an early release's census: each process's environment after its command line, which
# carries the session a command runs for (CLAUDE_CODE_SESSION_ID, CODEX_THREAD_ID, VOCARO_DEVICE_OWNER).
ENVIRONMENTS = ("ps", "-axwwE", "-o", "pid=,ppid=,uid=,lstart=,args=")
# A program inside an Xcode, such as the DTServiceHub that xcodebuild starts for a test run,
# which launchd adopts once xcodebuild has ended.
XCODE = re.compile(r"[^\s'\"]*/Xcode[^/\s'\"]*\.app/Contents/")
# The executable of an app, an XPC service or an app extension, which launchd starts when a UI test launches
# the app a run built in its checkout, as a Pagepour Mac test run's app and its converter service were on
# 2026-11-03, which the census listed as left over (vocaro-engineering#129).
BUNDLE = re.compile(r"[^\s'\"]*\.(?:app|xpc|appex)/Contents/MacOS/[^/\s'\"]+(?=\s|$)")
# Characters of a leftover's command line that the census reports.
COMMAND_LIMIT = 1000
# How ps prints a process's start (lstart), with its spacing collapsed.
STARTED = "%a %b %d %H:%M:%S %Y"
# What the work's Python processes are given: no bytecode, whose __pycache__ directories a
# project's .gitignore need not name, and which would leave its checkout with changes (claim).
NO_BYTECODE = {"PYTHONDONTWRITEBYTECODE": "1"}
# The refs under which a repository here names each commit a checkout of it holds. A push learns what
# the second Mac has from its refs alone, and a detached checkout's HEAD is none of them, so without
# these a push sends the commit's whole history (vocaro-engineering#67).
CACHE = "refs/vocaro/checkouts/"
COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")

# Seconds a pool simulator's launchd_sim must have run before reap may shut it down.
REAP_AFTER = 2 * 60 * 60
# Seconds one reap may take, which bounds what it adds to a dispatch; seconds it must have left to start
# another stop; seconds the check before a stop may hold the device record's lock, under the 30 another
# session's change waits for it (devices.Record); and seconds it waits for that lock.
REAP_SECONDS, REAP_STOP_SECONDS, REAP_CHECK_SECONDS, REAP_LOCK_SECONDS = 60, 20, 20, 5
# The durable log of its stops, beside the admission log, whose audit reads only admissions.
REAP_LOG = "simulator-reaps.jsonl"
# The log of how long sessions hold the members `eng simulator-pool take` hands them, beside the reap log: a
# line for each take, extend, release and reap release of one, so that TAKE_HOLD (simulators/pool.py) is sized
# from measurement (vocaro-engineering#179). A line that would take it past HOLD_LOG_LIMIT bytes first makes it
# the previous generation, as the admission log does, so the two stay within twice it.
HOLD_LOG, PREVIOUS_HOLD_LOG, HOLD_LOG_LIMIT = "simulator-holds.jsonl", "simulator-holds.1.jsonl", 4 << 20
# Set on the reservation `eng simulator-pool take` makes, never a run's own (ALONE), naming the pool, so that
# reap logs its release in HOLD_LOG.
TAKEN = "takenFromPool"
# A simulator's own launchd_sim, as ps prints it.
LAUNCHD_SIM = re.compile(r"(?:\S*/)?launchd_sim\s")
# What follows a pool's name in a member's: LABEL N, as simulators/pool.py names one; and in its kind's
# warmed template's, LABEL template (RUNTIME), with pool.py's own pattern, which this file cannot import
# because it runs on the second Mac alone.
MEMBER = re.compile(r".+ [1-9][0-9]*")
TEMPLATE = re.compile(r"(?P<label>.+) template \((?P<runtime>[^()]+)\)")
SIMULATORS = ("xcrun", "simctl", "list", "devices", "-j")
# The toolkit's own checkout on each Mac, relative to its home, which update_toolkit keeps at origin/main
# there (vocaro-engineering#203); and its src/python as a shell path from the home, where a recovery that the
# second Mac's record needs runs.
TOOLKIT_CHECKOUT = "Development/vocaro-engineering"
TOOLKIT = f"~/{TOOLKIT_CHECKOUT}/src/python"
# Seconds one update_toolkit may take in all, the bound it adds to the connection that prepares a dispatch,
# and at most for its fetch; and its lock beside the second Mac's repositories, under ROOT.
UPDATE_SECONDS, UPDATE_FETCH_SECONDS = 40, 25
# Seconds its ps and its lsof each have at least, whatever is left of that bound: under load, the git steps
# before them used nearly all of it, and a ps given the last second listed nothing (vocaro-engineering#221).
UPDATE_LISTING_SECONDS = 10
UPDATE_LOCK = "toolkit-update.lock"

# The journals of dispatches a connection's end does not stop, under eng remote's root, each in a directory named
# for its dispatch; the frames in one, and the file a later connection's stop leaves there for the run.
JOURNALS, FRAMES, STOP_FILE = "dispatches", "frames", "stop"
# Seconds an uncollected journal stays: past any run's bound, for a dispatcher that never came back.
JOURNAL_KEPT = 7 * 24 * 60 * 60
# Seconds the end of a run waits on a connection that takes no bytes, and then for the dispatcher's `received`.
# The journal holds what they did not carry, for a later connection, so neither wait need cover a slow Mac.
JOURNAL_STALL_SECONDS, RECEIVED_SECONDS = 30, 10
# Seconds a run stopped at its unattended bound has between SIGTERM and SIGKILL: eng's own stop of a foreground
# run takes up to about 190 s of LINGER and FOREGROUND_KILL_AFTER (processes.py), and nobody is left to close it.
OVERRUN_KILL_SECONDS = 300
# The longest watch goes without looking at the clock for the dispatcher's silence.
WATCH_SECONDS = 5

_output = threading.Lock()
_stop = threading.Event()
# The dispatcher's `attached` and `received`, and that the run outlasted its unattended bound.
_attached, _received, _overran = threading.Event(), threading.Event(), threading.Event()
# Set once the dispatcher has gone silent past the plan's bound (watch): nobody is left to read this run's output.
_silent = threading.Event()
# The run's Journal, when its plan keeps one, and when its connection ended, by time.monotonic.
_journal, _detached_at = None, None


class Refusal(RuntimeError):
    """The second Mac will not start the work, and nothing of it ran. A busy second Mac cannot start
    it now; any other refusal is its configuration's, and fix says what fixing that takes."""

    def __init__(self, reason, fix=None, busy=False):
        super().__init__(reason)
        self.fix, self.busy = fix, busy


def emit(kind, data):
    """Write one framed line, unless the dispatcher has gone silent. A connection nobody reads can take a write
    and never finish it, and a result saying the run stopped would read, to a dispatcher that wakes, as the
    work's own failure; with none, that dispatcher calls the run unmeasured, as it does any lost run."""
    if not _silent.is_set(): write_frame(kind, data)


def write_frame(kind, data):
    """Write one framed line: to the run's journal when it keeps one, which never waits on the network, and otherwise
    to standard output, where a closed connection is a stop, since nobody is left to read the result. A journal
    that cannot be written is one too."""
    if not data.endswith(b"\n"): data += b"\n"
    with _output:
        try:
            if _journal is not None:
                _journal.write(kind + b" " + data)
            else:
                sys.stdout.buffer.write(kind + b" " + data)
                sys.stdout.buffer.flush()
        except OSError:
            _stop.set()


def connection_ended():
    """The connection to the dispatcher ended. Once the dispatcher has heard the run start and while the run keeps a
    journal, the run goes on, and later connections can collect it. Otherwise this is a stop: a dispatcher that
    never heard the run start takes it for one that never ran. Called from a signal handler, so it writes no frame."""
    global _detached_at
    if _received.is_set(): return
    if _journal is None or not _attached.is_set(): _stop.set()
    elif _detached_at is None: _detached_at = time.monotonic()


def write_all(data):
    """Write data to standard output's descriptor, not sys.stdout: a write blocked on a connection that has gone
    must not hold the buffered writer's lock while Python exits (see words)."""
    while data:
        data = data[os.write(1, data):]


class Journal:
    """Every frame of a run, kept on this Mac, so that a run outlives its connection and a later one can collect it.

    The run writes frames here alone, so its output never waits on the network: a connection whose far end has
    gone to sleep takes bytes until its buffers fill and then blocks, which would stop the lane at its next print
    (vocaro-engineering#321). A thread sends the journal on to standard output. An exclusive flock on it, held
    while this process lives and released however it ends, tells a later connection that the run is over."""

    def __init__(self, directory, limit):
        directory.mkdir(parents=True, exist_ok=True)
        self.directory, self.limit, self.path = directory, limit, directory / FRAMES
        self.stream = self.path.open("ab", buffering=0)
        fcntl.flock(self.stream, fcntl.LOCK_EX)
        self.more, self.closed, self.drained = threading.Event(), threading.Event(), threading.Event()
        self.progress = time.monotonic()
        self.pumping = threading.Thread(target=self.pump, daemon=True)
        self.pumping.start()
        threading.Thread(target=self.warden, daemon=True).start()

    def write(self, frame):
        self.stream.write(frame)
        self.more.set()

    def pump(self):
        with self.path.open("rb", buffering=0) as stream:
            while True:
                closed = self.closed.is_set()
                chunk = stream.read(1 << 16)
                if chunk:
                    try: write_all(chunk)
                    except OSError:
                        connection_ended()
                        return
                    self.progress = time.monotonic()
                elif closed:
                    self.drained.set()
                    return
                else:
                    self.more.wait(0.2)
                    self.more.clear()

    def warden(self):
        """A stop that a later connection left, and the bound on a run nobody is attending. eng run and eng admit
        each stop their own work at its timeout; this is for an eng that does not."""
        announced = False
        while not self.closed.wait(0.5):
            if (self.directory / STOP_FILE).exists(): _stop.set()
            if _detached_at is None: continue
            if not announced:
                announced = True
                say(f"the connection to the dispatching Mac ended; the run goes on, for at most {self.limit:g} s more")
            if not _overran.is_set() and time.monotonic() - _detached_at > self.limit:
                _overran.set()
                _stop.set()
                say(f"the run outlasted the {self.limit:g} s it may run with nobody attending; stopping it")

    def finish(self):
        """After the last frame: wait for it to reach the connection, while bytes are moving, and for the dispatcher
        to say it has them, and then remove the journal. A journal not known to be delivered stays to be collected."""
        self.closed.set()
        self.more.set()
        self.progress = time.monotonic()
        while self.pumping.is_alive() and time.monotonic() - self.progress < JOURNAL_STALL_SECONDS:
            time.sleep(0.05)
        if self.drained.is_set(): _received.wait(RECEIVED_SECONDS)
        if _received.is_set(): shutil.rmtree(self.directory, ignore_errors=True)


def prune_journals(base):
    """Remove journals no dispatcher came back for."""
    for directory in base.glob("*"):
        try:
            if time.time() - (directory / FRAMES).stat().st_mtime > JOURNAL_KEPT:
                shutil.rmtree(directory, ignore_errors=True)
        except OSError:
            pass


def stop_journal(directory):
    """Stop the run that journals in directory, what `reattach` says of it, and only that run."""
    try:
        stream = (directory / FRAMES).open("rb", buffering=0)
    except FileNotFoundError:
        return "gone"
    with stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            (directory / STOP_FILE).touch()
            return "stopping"
    shutil.rmtree(directory, ignore_errors=True)
    return "ended"


def reattach(request):
    """Send a dispatch's journal from its start, then what the run adds to it until its process has ended, and close
    with an `E` frame, for a dispatcher whose connection ended. Standard input carries its words as a run's does:
    `stop` leaves the stop file the run's warden looks for, and `received` has the journal removed once it is whole.
    The end of standard input changes nothing: the run goes on. A journal that is not here gives only the `E`.

    With `stop` in the request it follows nothing: it stops this one run, and says on one line what it found.
    `stopping` is a run told to stop, `ended` one that had already ended, and `gone` a journal not here."""
    directory = Path.home() / request["root"] / JOURNALS / request["id"]
    if request.get("stop"):
        print(json.dumps({"stop": stop_journal(directory)}))
        return
    received = threading.Event()

    def listen():
        for word in words():
            if word == b"received": received.set()
            elif word == b"stop":
                try: (directory / STOP_FILE).touch()
                except OSError: pass

    threading.Thread(target=listen, daemon=True).start()
    try:
        stream = (directory / FRAMES).open("rb", buffering=0)
    except OSError:
        try: write_all(b"E {}\n")
        except OSError: pass
        return
    try:
        with stream:
            while True:
                # Read after the check: what the run wrote before it ended is then all there.
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(stream, fcntl.LOCK_UN)
                    ended = True
                except OSError:
                    ended = False
                chunk = stream.read(1 << 16)
                if chunk: write_all(chunk)
                elif ended: break
                else: time.sleep(0.2)
        write_all(b"E {}\n")
    except OSError:
        return
    received.wait(RECEIVED_SECONDS)
    if received.is_set(): shutil.rmtree(directory, ignore_errors=True)


def say(text):
    emit(b"O", f"second Mac: {text}".encode())


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


SCREEN_GUIDE = "guides/background-mac-testing.md, Checking a Mac's screen"
SCREEN_KEYS = ("CGSSessionScreenIsLocked", "IOConsoleLocked")
# A recorded `ioreg -n Root -d1 -a` to read in place of the IORegistry, for tests, which never lock a real screen.
SCREEN_RECORDING = "VOCARO_SCREEN_IOREG"


def screen(found=None, mac=None, at=None):
    """Whether this Mac's console screen is locked now, read from the IORegistry root where the tests' own
    check reads it (vocaro-engineering#185): state is `unlocked`, `locked` or `unknown`. found is the bytes of
    `ioreg -n Root -d1 -a`, read now when None. Only the login session on the console counts: its
    CGSSessionScreenIsLocked, which CGSessionCopyCurrentDictionary() returns, and the root's IOConsoleLocked.
    Either one set is locked, since a lock from idle time sets only the first and a hand lock was once seen
    without the second. No session on the console, or a reading that fails, is unknown with its reason,
    never unlocked. Each key reads True, False or None when absent. SCREEN_RECORDING names a recording to read
    instead of ioreg."""
    reading = {"mac": mac or mac_name(), "at": at or time.strftime("%H:%M:%S")}
    unknown = lambda reason: {**reading, "state": "unknown", "reason": reason}
    if found is None and os.environ.get(SCREEN_RECORDING):
        try: found = Path(os.environ[SCREEN_RECORDING]).read_bytes()
        except OSError as error: return unknown(f"the recording {SCREEN_RECORDING} names cannot be read: {error}")
    if found is None:
        try:
            ran = subprocess.run(["ioreg", "-n", "Root", "-d1", "-a"], capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as error:
            return unknown(f"ioreg could not run: {error}")
        if ran.returncode:
            said = ran.stderr.decode(errors="replace").strip().splitlines()
            return unknown(f"ioreg exited {ran.returncode}" + (f": {said[-1]}" if said else ""))
        found = ran.stdout
    try:
        root = plistlib.loads(found)
    except Exception as error:  # plistlib raises several kinds for a damaged property list.
        return unknown(f"ioreg's output is not a property list: {error}")
    if not isinstance(root, dict) or not isinstance(root.get("IOConsoleUsers", []), list):
        return unknown("ioreg's root entry is not in the expected form")
    console = [user for user in root.get("IOConsoleUsers") or []
               if isinstance(user, dict) and user.get("kCGSSessionOnConsoleKey") is True]
    if not console: return unknown("no login session is on the console")
    user = console[0]
    keys = {"CGSSessionScreenIsLocked": user.get("CGSSessionScreenIsLocked"),
            "IOConsoleLocked": root.get("IOConsoleLocked")}
    keys = {key: value if isinstance(value, bool) else None for key, value in keys.items()}
    locked = any(value is True for value in keys.values())
    return {**reading, "state": "locked" if locked else "unlocked", "user": user.get("kCGSSessionUserNameKey"), **keys}


def screen_line(reading, where=None):
    """One line saying what screen read: `screen: unlocked (console user NAME, CGSSessionScreenIsLocked
    absent, IOConsoleLocked no) on MAC at HH:MM:SS`, or `screen: unknown (REASON) on MAC at ...`. where
    names the Mac in place of its own name, such as `this Mac (NAME)`."""
    said = lambda value: "absent" if value is None else "yes" if value else "no"
    if reading["state"] == "unknown":
        detail = reading.get("reason") or "no reason given"
    else:
        detail = ", ".join([f"console user {reading.get('user') or 'unnamed'}",
                            *(f"{key} {said(reading.get(key))}" for key in SCREEN_KEYS)])
    return f"screen: {reading['state']} ({detail}) on {where or reading.get('mac')} at {reading.get('at')}"


def screen_declined(plan, read=None):
    """Decline, as busy, a lane that needs an unlocked screen while this Mac's does not read unlocked, when the
    plan may be declined, before it takes a checkout: it then waits for a Mac that can measure it instead of
    running unmeasured (vocaro-engineering#185). read is screen, which a test replaces."""
    if not (plan.get("unlockedScreen") and plan.get("decline")): return
    found = (read or screen)()
    if found["state"] != "unlocked":
        raise Refusal(f"its screen is {found['state']}, and the lane needs it unlocked ({screen_line(found)})", busy=True)


SCROLL_BARS_KEY = "AppleShowScrollBars"
# A JSON file to read in place of the two commands, for tests: {"defaults": [exit status, standard output, standard
# error], "ioreg": [the same]}. The readings are parsed as the commands' own output is.
SCROLL_BARS_RECORDING = "VOCARO_SCROLL_BARS_RECORDING"
SCROLL_BARS_COMMANDS = {"defaults": ["defaults", "read", "-g", SCROLL_BARS_KEY],
                        "ioreg": ["ioreg", "-a", "-r", "-c", "IOHIDDevice", "-d", "1"]}
# What `defaults read` says of a key no one set: the first on macOS 27.2, the second on earlier macOS.
SCROLL_BARS_UNSET = re.compile(r"Could not find key|does not exist")
SCROLL_BARS_FIXED = {"Always": "legacy", "WhenScrolling": "overlay"}
# The HID Generic Desktop page's Pointer and Mouse usages.
POINTING_PAGE, POINTING_USAGES = 1, (1, 2)


def scroll_bars_read(tool):
    """(exit status, standard output as bytes, standard error) of tool's read-only command, or None for the status
    with why it could not run in the third place."""
    recording = os.environ.get(SCROLL_BARS_RECORDING)
    if recording:
        try:
            status, standard, said = json.loads(Path(recording).read_text())[tool]
        except (OSError, ValueError, KeyError, TypeError) as error:
            return None, b"", f"the recording {SCROLL_BARS_RECORDING} names cannot be read: {error}"
        return status, standard.encode(), said
    try:
        ran = subprocess.run(SCROLL_BARS_COMMANDS[tool], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as error:
        return None, b"", f"{tool} could not run: {error}"
    return ran.returncode, ran.stdout, ran.stderr.decode(errors="replace")


def scroll_bars_failed(tool, status, said):
    """Why tool's reading failed, in the words of its last line."""
    if status is None: return said
    lines = said.strip().splitlines()
    return f"{tool} exited {status}" + (f": {lines[-1]}" if lines else "")


def pointing_devices(listed):
    """The names, sorted and each once, of the pointing devices in `ioreg -a -r -c IOHIDDevice -d 1`'s output other
    than built-in ones, by each device's usages and its Built-In property; a device that does not say it is built in
    counts. No output is no HID device. ValueError when the output is not the expected list."""
    if not listed.strip(): return []
    try:
        entries = plistlib.loads(listed)
    except Exception as error:  # plistlib raises several kinds for a damaged property list.
        raise ValueError(f"ioreg's output is not a property list: {error}") from error
    if not isinstance(entries, list): raise ValueError("ioreg's output is not a list of devices")
    names = set()
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("Built-In"): continue
        pairs = entry.get("DeviceUsagePairs")
        usages = [(pair.get("DeviceUsagePage"), pair.get("DeviceUsage")) for pair in pairs if isinstance(pair, dict)] \
            if isinstance(pairs, list) else []
        usages.append((entry.get("PrimaryUsagePage"), entry.get("PrimaryUsage")))
        if any(page == POINTING_PAGE and usage in POINTING_USAGES for page, usage in usages):
            names.add(str(entry.get("Product") or "unnamed"))
    return sorted(names)


def scroll_bars(mac=None, at=None):
    """Which scroll-bar kind this Mac gives its processes now, read and never changed (vocaro-engineering#229): kind
    is `legacy`, `overlay` or `unknown`. setting is the global AppleShowScrollBars, or `unset`, and pointingDevices
    the names of the pointing devices attached other than the built-in trackpad: under the automatic setting, which
    an unset key is, any of them means legacy. Always and WhenScrolling settle the kind by themselves. A value that
    could not be read is None, never `unset` or an empty list, with why in reasons; a kind that depended on it, or a
    setting with no known kind, is unknown. It says what the host gives: a process that pins its own kind differs."""
    reading = {"mac": mac or mac_name(), "at": at or time.strftime("%H:%M:%S")}
    reasons = []
    setting, devices = None, None
    status, found, said = scroll_bars_read("defaults")
    if status == 0:
        setting = found.decode(errors="replace").strip()
        if setting not in ("Automatic", *SCROLL_BARS_FIXED):
            reasons.append(f"{SCROLL_BARS_KEY} is {setting!r}, which gives no known kind")
    elif status == 1 and SCROLL_BARS_UNSET.search(said):
        setting = "unset"
    else:
        reasons.append(f"{SCROLL_BARS_KEY} could not be read: {scroll_bars_failed('defaults', status, said)}")
    status, found, said = scroll_bars_read("ioreg")
    if status == 0:
        try:
            devices = pointing_devices(found)
        except ValueError as error:
            reasons.append(f"the pointing devices could not be read: {error}")
    else:
        reasons.append(f"the pointing devices could not be read: {scroll_bars_failed('ioreg', status, said)}")
    kind = SCROLL_BARS_FIXED.get(setting, "unknown")
    if setting in ("Automatic", "unset") and devices is not None: kind = "legacy" if devices else "overlay"
    return {**reading, "kind": kind, "setting": setting, "pointingDevices": devices, **({"reasons": reasons} if reasons else {})}


def scroll_bars_line(reading, where=None):
    """One line saying what scroll_bars read: `scroll bars: legacy (AppleShowScrollBars unset, pointing devices: NAME)
    on MAC at HH:MM:SS`, with why for anything unread. where names the Mac in place of its own name."""
    setting, devices = reading.get("setting"), reading.get("pointingDevices")
    detail = [*([f"{SCROLL_BARS_KEY} {setting}"] if setting is not None else []),
              *([] if devices is None else [f"pointing devices: {', '.join(devices)}" if devices
                                            else "no pointing device but a built-in one"]),
              *reading.get("reasons", [])]
    return f"scroll bars: {reading['kind']} ({', '.join(detail)}) on {where or reading.get('mac')} at {reading.get('at')}"


def identity(developer_dir):
    """The Mac a result was measured on, its macOS, the Xcode at developer_dir and the scroll-bar kind it gives."""
    number = lambda name: int(output(["sysctl", "-n", name]) or 0) or None
    return {"name": mac_name(),
            "model": output(["sysctl", "-n", "hw.model"]), "cpus": number("hw.ncpu"),
            "memoryBytes": number("hw.memsize"), "macOS": output(["sw_vers", "-productVersion"]),
            "build": output(["sw_vers", "-buildVersion"]), "developerDir": developer_dir, "xcode": xcode(developer_dir),
            "scrollBars": scroll_bars()}


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


def keychain(path):
    """The Mac's name, its home, the keychain file path names and whether it is there, and the keychain
    files in ~/Library/Keychains, for `eng keychain recover`. path is absolute, starts with ~, or is a
    file name in ~/Library/Keychains; it is never completed, so a misspelled name is reported missing.
    It reads names and file types only: nothing opens a keychain."""
    home = Path.home()
    folder = home / KEYCHAINS
    given = Path(path).expanduser() if path.startswith(("/", "~")) else home / path if "/" in path else folder / path
    try:
        names = sorted(entry.name for entry in folder.iterdir()
                       if entry.name.endswith((".keychain-db", ".keychain")) and entry.is_file())
    except OSError:
        names = []
    return {"mac": mac_name(), "home": str(home), "path": str(given), "exists": given.is_file(),
            "keychains": [str(folder / name) for name in names]}


def security_unlock(path, password):
    """Unlock the keychain file at path with password, bytes, in this process's security session,
    which the processes it starts share: SecKeychainUnlock's OSStatus, and whether SecKeychainGetStatus
    then reads the keychain unlocked. The password goes to Security alone, never into a command's
    arguments or environment, and with a password given Security shows no dialog."""
    security = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
    release = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation").CFRelease
    security.SecKeychainOpen.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p)]
    security.SecKeychainUnlock.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p, ctypes.c_ubyte]
    security.SecKeychainGetStatus.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    for function in (security.SecKeychainOpen, security.SecKeychainUnlock, security.SecKeychainGetStatus):
        function.restype = ctypes.c_int32
    release.argtypes = [ctypes.c_void_p]
    reference = ctypes.c_void_p()
    status = security.SecKeychainOpen(str(path).encode(), ctypes.byref(reference))
    if status: return status, False
    try:
        status = security.SecKeychainUnlock(reference, len(password), password, 1)
        if status: return status, False
        state = ctypes.c_uint32()
        status = security.SecKeychainGetStatus(reference, ctypes.byref(state))
        return status, not status and bool(state.value & UNLOCK_STATE)
    finally:
        release(reference)


def unlock_for_run(root, unlock=security_unlock):
    """Unlock the login keychain in this run's own SSH session, when the owner has kept its password
    in root (keep_unlocked): an unlock in another session, the owner's desktop or an earlier SSH login,
    does not reach this one (guides/shared-host.md, Keychains on the right Mac). The path it unlocked,
    or None when no password is kept. A kept password that does not unlock it is the owner's to fix,
    and the run does not start: work that signs would fail later, and less plainly."""
    kept = root / KEYCHAIN_PASSWORD
    if not kept.is_file(): return None
    path = Path.home() / KEYCHAINS / LOGIN_KEYCHAIN
    fix = (f"on the Mac that sends work here, run `python3 scripts/eng keychain keep-unlocked` and type this Mac's "
           f"login password at its prompt, or delete {kept} to send work without the unlock "
           "(guides/shared-host.md, Keychains on the right Mac)")
    if not path.is_file(): raise Refusal(f"{kept} holds the password of {path}, which is not there", fix)
    try:
        password = kept.read_bytes()
    except OSError as error:
        raise Refusal(f"{kept} cannot be read: {error}", fix) from None
    # A file the owner wrote by hand may end in a newline, which no typed password does.
    if password.endswith(b"\n"): password = password[:-1]
    try:
        status, unlocked = unlock(path, password)
    except (OSError, AttributeError) as error:
        raise Refusal(f"Security could not be called to unlock {path}: {error}", fix) from None
    if status == AUTH_FAILED:
        raise Refusal(f"the password in {kept} does not unlock {path} (errSecAuthFailed): this Mac's login password "
                      "may have changed since it was kept", fix)
    if status or not unlocked:
        raise Refusal(f"{path} is not unlocked with the password in {kept}: OSStatus {status}", fix)
    return str(path)


def keep(path, password):
    """Write password to path whole, readable and writable only by this account, replacing any file there."""
    temporary = path.with_name(f".{path.name}.{os.getpid()}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(password)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def keep_unlocked(request, ask=None, unlock=security_unlock, run=subprocess.run, out=print, terminal=None):
    """`eng keychain keep-unlocked` on the second Mac, in the owner's Terminal there or through the
    `ssh -t` the sending Mac runs: ask for this Mac's login password, and once it has unlocked the login
    keychain, keep it in request["root"], under the home, readable only by this account, for each eng
    remote run to unlock that keychain in its own session (unlock_for_run). Then set the keychain to
    lock neither after a time nor on sleep, so that the owner's own session keeps it unlocked too. The
    password goes only to Security and that file. Exit 0 when both are done, 1 when no password given
    unlocked the keychain, which changes nothing, or the settings were not changed, and 2 when it
    refused, having asked for nothing."""
    ask = ask or getpass.getpass
    terminal = sys.stdin.isatty() if terminal is None else terminal
    here, expected = mac_name(), request.get("mac")
    root, path = Path.home() / request["root"], Path.home() / KEYCHAINS / LOGIN_KEYCHAIN
    refusal = None
    if expected and here != expected:
        refusal = f"this is {here}, not {expected}, the Mac whose login keychain the request was for"
    elif not path.is_file():
        refusal = f"{path} is not on {here}"
    elif not root.is_dir():
        refusal = (f"{root} is not on {here}: set up the second Mac first, and run eng remote there once "
                   "(guides/shared-host.md, Setting up the second Mac)")
    elif not terminal:
        refusal = "it asks for the password at a terminal, and this has none: run it in your own Terminal"
    if refusal:
        out(f"Refused: {refusal}. Nothing was asked for or changed.")
        return 2
    for _ in range(PASSWORD_TRIES):
        password = ask(f"Login password of {here}, for its login keychain (not shown): ").encode()
        status, unlocked = unlock(path, password)
        if not status and unlocked: break
        out("That password did not unlock it." if status == AUTH_FAILED else
            f"{path} is not unlocked with that password: OSStatus {status}.")
    else:
        out(f"No password unlocked {path}; nothing was kept or changed.")
        return 1
    kept = root / KEYCHAIN_PASSWORD
    try:
        keep(kept, password)
    except OSError as error:
        out(f"It unlocked the keychain, but {kept} could not be written: {error}. Nothing was kept or changed.")
        return 1
    out(f"Kept it in {kept}, readable only by {pwd.getpwuid(os.getuid()).pw_name}: each eng remote run here now "
        "unlocks the login keychain in its own session.")
    # The keychain is unlocked in this session, so neither command needs a dialog; without -l, -u and -t,
    # set-keychain-settings sets no timeout and no lock on sleep.
    for command in (["security", "set-keychain-settings", str(path)], ["security", "show-keychain-info", str(path)]):
        try:
            done = run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as error:
            done = subprocess.CompletedProcess(command, 1, "", str(error))
        said = " ".join(((done.stderr or "") + " " + (done.stdout or "")).split())
        if done.returncode:
            out(f"`{shlex.join(command)}` failed, exit {done.returncode}: {said}. The password is kept; the keychain's "
                "lock settings may still lock it in the owner's own session.")
            return 1
    out(f"Its settings now: {said}")
    return 0


def console_user():
    """The account logged in on the Mac's own screen, or None; root while the login window shows."""
    try: return pwd.getpwuid(os.stat("/dev/console").st_uid).pw_name
    except (OSError, KeyError): return None


def accessibility_trusted():
    """Whether this process, and with it the command that started it, is trusted for accessibility."""
    try:
        services = ctypes.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        services.AXIsProcessTrusted.restype = ctypes.c_bool
        return bool(services.AXIsProcessTrusted())
    except (OSError, AttributeError):
        return False


def screen_capture_allowed():
    """Whether this process, and with it the command that started it, may capture the screen, asked without a
    prompt (CGPreflightScreenCaptureAccess)."""
    try:
        graphics = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        graphics.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
        return bool(graphics.CGPreflightScreenCaptureAccess())
    except (OSError, AttributeError):
        return False


def foreground_problems():
    """What this Mac lacks for foreground work, a Mac VoiceOver session among it, run by a command started
    over SSH, as [problem, the owner's fix] pairs; none when it is ready. Each is a setting of the owner's
    Mac, which the owner changes, never a run (guides/second-mac-foreground.md).
    It reads and changes nothing else: nothing here prompts, activates or sends a key."""
    found = []
    user, console = pwd.getpwuid(os.getuid()).pw_name, console_user()
    if console != user:
        found.append([f"{console or 'nobody'} is logged in on the screen, not {user}",
                      f"log in as {user} on the Mac's own screen; fast user switching to another account hides it"])
    front = output(["lsappinfo", "front"])
    name = output(["lsappinfo", "info", "-only", "name", front]) if front else None
    if name is not None and '"loginwindow"' in name:
        found.append(["the screen is locked, or the login window is in front",
                      "unlock it, and keep it from locking again: in System Settings, Lock Screen, set the screen "
                      "saver and turning the display off to Never and requiring a password after either to Never"])
    if not accessibility_trusted():
        found.append(["a command started over SSH is not trusted for accessibility (AXIsProcessTrusted is false), so "
                      "it can neither send VoiceOver keys through System Events nor read speech's observer",
                      "in System Settings, Privacy & Security, Device Control and Data Access (macOS 27.2's name for "
                      "Accessibility), add /usr/libexec/sshd-keygen-wrapper, which runs Remote Login's commands, and "
                      "turn it on"])
    if not screen_capture_allowed():
        found.append(["a command started over SSH may not capture the screen (CGPreflightScreenCaptureAccess is "
                      "false), so a run's screenshots can fail or raise a permission prompt that takes the focus",
                      "in System Settings, Privacy & Security, Screen & System Audio Recording, add "
                      "/usr/libexec/sshd-keygen-wrapper and turn it on"])
    mode = output(["automationmodetool"]) or ""
    if "DOES NOT REQUIRE" not in mode:
        found.append(["XCTest's automation mode asks for authentication, which nobody at the screen gives",
                      "run `sudo automationmodetool enable-automationmode-without-authentication` there"])
    if output(["defaults", "read", "com.apple.VoiceOverTraining", "doNotShowSplashScreen"]) != "1":
        found.append(["VoiceOver opens its welcome dialog when it starts, which takes the VoiceOver cursor",
                      "run `defaults write com.apple.VoiceOverTraining doNotShowSplashScreen -bool true` there"])
    if output(["defaults", "read", "com.apple.HIToolbox", "AppleDictationAutoEnable"]) != "0":
        found.append(["after a few VoiceOver commands, each of which presses Control, macOS offers to enable "
                      "Dictation, and the prompt takes the VoiceOver cursor",
                      "run `defaults write com.apple.HIToolbox AppleDictationAutoEnable -int 0` there"])
    return found


CAPTURE_APPROVALS = "Library/Group Containers/group.com.apple.replayd/ScreenCaptureApprovals.plist"
CAPTURE_RUNNERS = "Library/Application Support/VocaroEngineering/foreground-runners"
# The programs a command started over SSH runs under: Remote Login's commands' own captures prompt for them.
CAPTURE_SSH_HOSTS = ("/usr/libexec/sshd-keygen-wrapper", "/usr/libexec/sshd-session", "com.apple.sshd-session")


def capture_approval_name(key):
    """An approval's key as a path where it is a file URL, else as recorded."""
    if key.startswith("file://"):
        return unquote(urlparse(key).path).rstrip("/")
    return key


def screen_capture_approvals(home=None, path=None):
    """macOS's direct screen-capture approvals that eng's runs depend on (capture_approvals.py): the stable
    runner paths' and the SSH command host's, each as {"app", "allowed", "lapses"} with ISO 8601 UTC times,
    soonest first; None when the record cannot be read, [] when it holds none of them. It reads the record and
    nothing else, and never writes it."""
    home = Path(home if home is not None else Path.home())
    try:
        entries = plistlib.loads(Path(path or home / CAPTURE_APPROVALS).read_bytes())
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None
    runners = str(home / CAPTURE_RUNNERS) + "/"
    found = []
    for key, entry in entries.items() if isinstance(entries, dict) else ():
        name = capture_approval_name(key)
        if not isinstance(entry, dict) or not (name.startswith(runners) or name in CAPTURE_SSH_HOSTS):
            continue
        times = [entry.get(field) for field in ("kScreenCaptureApprovalLastAlerted", "kScreenCapturePrivacyHintDate")]
        if not all(isinstance(value, datetime) for value in times):
            continue
        allowed, lapse = (value.replace(tzinfo=value.tzinfo or timezone.utc).astimezone(timezone.utc)
                          .isoformat(timespec="seconds") for value in times)
        found.append({"app": name, "allowed": allowed, "lapses": lapse})
    return sorted(found, key=lambda item: item["lapses"])


def crash_dialog(run=output):
    """com.apple.CrashReporter's DialogType, or None when it is unset, macOS's default, which shows an app's
    "quit unexpectedly" alert."""
    return run(["defaults", "read", *CRASH_DIALOG])


def crash_dialog_record():
    """The file a foreground run keeps DialogType's value from before it in while the run has it off, as
    crash_alerts.py writes it, which VOCARO_CRASH_DIALOG_RECORD overrides for a test's."""
    return Path(os.environ.get("VOCARO_CRASH_DIALOG_RECORD")
                or Path(pwd.getpwuid(os.getuid()).pw_dir) / CRASH_DIALOG_RECORD)


# Lists each window of the processes that raise system alerts as one line: the process, the window's
# x, y, width and height, its texts and its buttons, the last two separated by the unit separator.
ALERTS_SCRIPT = """on joined(found)
    set out to ""
    repeat with item_ in found
        try
            set out to out & (item_ as text) & (ASCII character 31)
        end try
    end repeat
    return out
end joined
set out to ""
tell application "System Events"
    repeat with name_ in {%s}
        if exists process (name_ as text) then
            tell process (name_ as text)
                repeat with window_ in windows
                    set place to position of window_
                    set extent to size of window_
                    set texts to ""
                    try
                        set texts to my joined(value of static texts of window_)
                    end try
                    try
                        set texts to texts & (name of window_ as text) & (ASCII character 31)
                    end try
                    set labels to ""
                    try
                        set labels to my joined(name of buttons of window_)
                    end try
                    set out to out & (name_ as text) & tab & (item 1 of place) & tab & (item 2 of place) & tab & (item 1 of extent) & tab & (item 2 of extent) & tab & texts & tab & labels & linefeed
                end repeat
            end tell
        end if
    end repeat
end tell
return out""" % ", ".join(f'"{name}"' for name in ALERT_PROCESSES)


def system_alerts(run=output):
    """The windows of the processes that raise system alerts, each as {"app", "frame": [x, y, width, height],
    "texts", "buttons"}, read through System Events, which needs accessibility; None when they cannot be read.
    It reads and changes nothing else: nothing here presses, activates or sends a key."""
    found = run(["osascript", "-e", ALERTS_SCRIPT])
    if found is None: return None
    alerts = []
    for line in found.splitlines():
        fields = line.split("\t")
        if len(fields) != 7: continue
        try: frame = [int(float(value)) for value in fields[1:5]]
        except ValueError: continue
        # A window with no extent covers nothing.
        if not frame[2] or not frame[3]: continue
        texts, labels = ([part for part in field.split("\x1f") if part and part != "missing value"]
                         for field in fields[5:7])
        alerts.append({"app": fields[0], "frame": frame, "texts": texts, "buttons": labels})
    return alerts


def crash_dialogs():
    """DialogType, the record a run that has it off keeps, and the system alerts on the screen, for
    `eng remote foreground`."""
    path = crash_dialog_record()
    try: kept = json.loads(path.read_text())
    except FileNotFoundError: kept = None
    except (OSError, ValueError) as error: kept = {"error": f"{path} cannot be read: {error}"}
    return {"value": crash_dialog(), "record": kept, "alerts": system_alerts()}


def foreground():
    """The Mac's name, foreground readiness and mac-foreground entry, for `eng remote foreground`.
    VoiceOver is read under the lease only."""
    path = record_path()
    try: entry = json.loads(path.read_text()).get("devices", {}).get("mac-foreground")
    except FileNotFoundError: entry = None
    except (OSError, ValueError, AttributeError) as error: entry = {"error": f"the device record cannot be read: {error}"}
    # VoiceOver may belong to the current holder. Even its baseline is read only after a
    # run acquires the lease; on is a valid baseline, preserved by the foreground state guard.
    return {"mac": mac_name(), "problems": foreground_problems(), "voiceoverManagedUnderLease": True, "entry": entry,
            "crashDialogs": crash_dialogs(), "captureApprovals": screen_capture_approvals()}


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


def xcode_defaults():
    """The UDIDs CoreSimulator's device set names as Xcode's default device for each runtime, as
    simulators.pool.default_devices reads them; this module imports nothing from the toolkit."""
    try:
        with open(Path.home() / "Library/Developer/CoreSimulator/Devices/device_set.plist", "rb") as handle:
            defaults = plistlib.load(handle).get("DefaultDevices", {})
    except (OSError, plistlib.InvalidFileException, ValueError, AttributeError):
        return frozenset()
    return frozenset(udid.upper() for types in defaults.values() if isinstance(types, dict)
                     for udid in types.values() if isinstance(udid, str))


def runtime_users(developer_dir):
    """The names of the simulators on each runtime identifier, but Xcode's defaults, which Xcode makes
    again: what deleting a runtime would take from someone. None when simctl lists no devices."""
    listed = output(["xcrun", "simctl", "list", "devices", "-j"], {**os.environ, "DEVELOPER_DIR": developer_dir})
    try:
        found = json.loads(listed)["devices"]
        defaults = xcode_defaults()
        return {runtime: sorted(device["name"] for device in devices if str(device["udid"]).upper() not in defaults)
                for runtime, devices in found.items()}
    except (TypeError, ValueError, KeyError, AttributeError):
        return None


def used_at(stamp):
    """The time an ISO 8601 stamp such as simctl's lastUsedAt names, or None. Python 3.9's fromisoformat
    reads neither a Z nor a fraction of other than 3 or 6 digits, so both are rewritten first."""
    matched = re.fullmatch(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:?\d\d)?", str(stamp or ""))
    if not matched: return None
    whole, fraction, zone = matched.groups()
    zone = "+00:00" if zone in (None, "Z") else zone if ":" in zone else f"{zone[:3]}:{zone[3:]}"
    try: return datetime.fromisoformat(f"{whole}.{(fraction or '0')[:6].ljust(6, '0')}{zone}").timestamp()
    except ValueError: return None


def chosen_builds(records, matches):
    """For each runtime identifier with several images in records (`simctl runtime list -j`), the build
    CoreSimulator resolves its devices to: the build `simctl runtime match list -j` (matches) chooses
    for an installed SDK, else the build whose image CoreSimulator last used. An identifier whose choice
    neither shows is left out. disk.chosen_builds applies the same rule on this Mac; this module imports
    nothing from the toolkit, since it runs on the second Mac's own Python (vocaro-engineering#190)."""
    groups = {}
    for record in records:
        if isinstance(record, dict): groups.setdefault(record.get("runtimeIdentifier", ""), []).append(record)
    sdks = {match["chosenRuntimeBuild"] for match in (matches or {}).values()
            if isinstance(match, dict) and match.get("chosenRuntimeBuild")}
    chosen = {}
    for identifier, group in groups.items():
        builds = {record.get("build") for record in group}
        if len(group) < 2 or None in builds or len(builds) < len(group): continue
        matched = [record["build"] for record in group if record["build"] in sdks]
        if len(matched) == 1:
            chosen[identifier] = matched[0]
            continue
        if matched: continue
        stamps = sorted(((used_at(record.get("lastUsedAt")), record["build"]) for record in group
                         if used_at(record.get("lastUsedAt")) is not None), reverse=True)
        if stamps and (len(stamps) == 1 or stamps[0][0] != stamps[1][0]): chosen[identifier] = stamps[0][1]
    return chosen


def recorded_builds():
    """Every build word this Mac's device records hold but under installedBuilds, the census of builds
    installed when a template was warmed, as disk.recorded_builds reads them; None when a record exists
    but cannot be read. A record that is not JSON is taken whole, word by word."""
    paths = list(dict.fromkeys([record_path(), Path(pwd.getpwuid(os.getuid()).pw_dir) / DEVICE_RECORD]))
    paths += sorted(Path(path) for path in glob.glob(LEGACY_RECORDS))
    found = set()
    for path in paths:
        if not path.exists(): continue
        try: text = path.read_text(errors="replace")
        except OSError: return None
        try: stack = [json.loads(text)]
        except ValueError:
            found.update(re.findall(r"[A-Za-z0-9]+", text))
            continue
        while stack:
            value = stack.pop()
            if isinstance(value, dict): stack += [item for key, item in value.items() if key != "installedBuilds"]
            elif isinstance(value, list): stack += value
            elif isinstance(value, str): found.update(re.findall(r"[A-Za-z0-9]+", value))
    return found


def images(developer_dir, pinned=()):
    """The runtime disk images `simctl runtime list` reports, each with the runtime it holds named as
    toolchain names it, NAME (BUILD), its image file, and in users the simulators on its runtime
    identifier but Xcode's defaults (runtime_users), or None when those cannot be listed. None when
    simctl lists no images.

    A superseded build, one whose identifier CoreSimulator resolves to another build (chosen_builds), runs
    none of those simulators, so it has no users and supersededBy names that build (vocaro-engineering#190).
    A pinned one keeps its identifier's users: a build in pinned, which the caller names because this
    module cannot read the toolkit (disk.pinned_builds), or one this Mac's device records name; and every
    superseded build keeps them while a record cannot be read."""
    environment = {**os.environ, "DEVELOPER_DIR": developer_dir}
    listed = output(["xcrun", "simctl", "runtime", "list", "-j"], environment)
    try:
        records = list(json.loads(listed).values())
    except (TypeError, ValueError, AttributeError):
        return None
    users = runtime_users(developer_dir)
    try: matches = json.loads(output(["xcrun", "simctl", "runtime", "match", "list", "-j"], environment))
    except (TypeError, ValueError): matches = None
    chosen = chosen_builds(records, matches if isinstance(matches, dict) else None)
    recorded = recorded_builds() if chosen else set()
    found = []
    for record in records:
        if not isinstance(record, dict) or not record.get("build"): continue
        bundle = Path(record.get("runtimeBundlePath") or "")
        name = bundle.stem if bundle.suffix == ".simruntime" else \
            f"{PLATFORMS.get(record.get('platformIdentifier'), record.get('platformIdentifier'))} {record.get('version')}"
        file, unreadable = image_file(record)
        identifier, build = record.get("runtimeIdentifier"), record["build"]
        superseded = chosen.get(identifier) if chosen.get(identifier, build) != build else None
        kept = superseded and (build in pinned or recorded is None or build in recorded)
        found.append({"runtime": f"{name} ({build})", "name": name, "build": build,
                      "identifier": record.get("identifier"), "state": record.get("state"),
                      "sizeBytes": record.get("sizeBytes"), "architectures": record.get("supportedArchitectures") or [],
                      "file": file, "unreadable": unreadable, "supersededBy": superseded,
                      "users": None if users is None or not identifier else [] if superseded and not kept
                      else users.get(identifier, [])})
    return sorted(found, key=lambda image: image["runtime"])


def delete_runtime(developer_dir, build, pinned=()):
    """Delete the runtime image of build once simctl lists it and no simulator but Xcode's defaults
    uses it, both read again now: none uses a superseded build that nothing pins (images). Return None
    once it is deleted, else why it was not."""
    found = [image for image in images(developer_dir, pinned) or [] if image["build"] == build]
    if len(found) != 1: return f"simctl lists {len(found)} images of {build}"
    image = found[0]
    if image["users"] is None: return "simctl did not list the simulators that might use it"
    if image["users"]: return f"{', '.join(image['users'])} use{'s' if len(image['users']) == 1 else ''} it"
    try:
        done = subprocess.run(["xcrun", "simctl", "runtime", "delete", image["identifier"]], capture_output=True,
                              text=True, env={**os.environ, "DEVELOPER_DIR": developer_dir}, timeout=300)
    except (OSError, subprocess.SubprocessError) as error:
        return f"simctl runtime delete failed: {error}"
    if done.returncode: return f"simctl runtime delete exited {done.returncode}: {done.stderr.strip()}"
    return None


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


def listing(command, timeout=60):
    """A command's standard output whatever its status, or None when it cannot run: lsof exits 1
    when it finds nothing, or when a process it looked at has ended."""
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout).stdout
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


def admission_directory():
    """Where the admission log is: a HEAVY_SLOT_DIR namespace's own, else in the account's durable state."""
    return Path(os.environ["HEAVY_SLOT_DIR"]) if os.environ.get("HEAVY_SLOT_DIR") else \
        Path(pwd.getpwuid(os.getuid()).pw_dir) / ADMISSION_STATE


def slot_directory():
    """Where admission's slot files are, as resources.slot_directory finds them: a HEAVY_SLOT_DIR namespace,
    else the legacy namespace in the user's temporary directory, whatever TMPDIR a command sets."""
    if os.environ.get("HEAVY_SLOT_DIR"): return Path(os.environ["HEAVY_SLOT_DIR"])
    if sys.platform != "darwin": return Path("/tmp") / SLOTS
    base = output(["/usr/bin/getconf", "DARWIN_USER_TEMP_DIR"])
    if not base: raise RuntimeError("Cannot establish the host resource directory")
    return Path(base) / SLOTS


def limits():
    """The admission limits the capacity reading needs, by key: HEAVY_SLOTS, HEAVY_SLOTS_MAX, HEAVY_BUSY_LIMIT and
    HEAVY_SETTLE_SECONDS, each from its environment variable, else the host's limits file, admission-limits.json
    beside the admission log, as a number or {"perCore": F}, else its default, as resources.limits reads them,
    which this file cannot import (#49). A file or variable admission could not read raises ValueError naming it, as admission does:
    its owner fixes it rather than eng guessing."""
    cores = os.cpu_count() or 1
    path = admission_directory() / ADMISSION_LIMITS
    try: text = path.read_text()
    except FileNotFoundError: text = None
    except OSError as error: raise ValueError(f"Invalid resource limits: {path} cannot be read: {error}")
    configured = {}
    if text is not None:
        try: data = json.loads(text)
        except ValueError as error: raise ValueError(f"Invalid resource limits: {path} is not JSON: {error}")
        if not isinstance(data, dict): raise ValueError(f"Invalid resource limits: {path} holds no JSON object")
        for key, value in data.items():
            if key not in LIMIT_KEYS: continue  # whatever a later toolkit adds
            counts = LIMIT_KEYS[key][1]
            number = lambda found: isinstance(found, (int, float)) and not isinstance(found, bool) and found >= 0
            if isinstance(value, dict) and set(value) == {"perCore"} and number(value["perCore"]) and value["perCore"] > 0:
                share = value["perCore"] * cores
                configured[key] = max(1, int(share)) if counts else float(share)
            elif number(value) and (not counts or float(value).is_integer()):
                configured[key] = int(value) if counts else float(value)
            else:
                raise ValueError(f"Invalid resource limits: {path} sets {key} to {json.dumps(value)}, not a "
                                 f"{'whole ' if counts else ''}number at or above 0 or {{\"perCore\": F}} with F above 0")
    found, slots = {}, None
    for key, (variable, counts, default) in LIMIT_KEYS.items():
        if variable in os.environ:
            try: found[key] = int(os.environ[variable]) if counts else float(os.environ[variable])
            except ValueError:
                raise ValueError(f"Invalid resource limits: {variable}={os.environ[variable]} is not a "
                                 f"{'whole ' if counts else ''}number")
        elif key in configured: found[key] = configured[key]
        else: found[key] = default(cores, slots)
        if key == "slots": slots = found[key]
    if found["slots"] < 1 or found["slotsMax"] < found["slots"] or found["settleSeconds"] < 0:
        raise ValueError(f"Invalid resource limits: HEAVY_SLOTS={found['slots']} must be at least 1, "
                         f"HEAVY_SLOTS_MAX={found['slotsMax']} at least HEAVY_SLOTS, and HEAVY_SETTLE_SECONDS at least 0")
    return found


def busy_slots(directory, count):
    """The slots among the first count whose lock a holder holds: each slot file in the directory is held with
    flock by the admission that took it (resources.take_slot). A shared lock tried without waiting tells, and
    is given up at once; a slot file that is missing was never taken, and one that cannot be opened counts as
    busy, so that a Mac eng cannot read never reads as idle."""
    found = []
    for slot in range(count):
        try: fd = os.open(directory / f"slot{slot}", os.O_RDONLY)
        except FileNotFoundError: continue
        except OSError:
            found.append(slot)
            continue
        try: fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError: found.append(slot)
        finally: os.close(fd)
    return found


def since_admission(directory, count):
    """Seconds since a holder last wrote its identity into one of the first count slots, as resources.last_admission
    reads it for admission's settle time; None when none has. Probing leaves a new slot file empty."""
    latest = None
    for slot in range(count):
        try: status = (directory / f"slot{slot}").stat()
        except OSError: continue
        if status.st_size: latest = status.st_mtime if latest is None else max(latest, status.st_mtime)
    return None if latest is None else round(max(0.0, time.time() - latest), 1)


# The CPU's busy share, read as resources.cpu_busy reads it, from the samples every process on the host keeps in
# the slot directory: CPU_SAMPLES, BUSY_WINDOW, BUSY_MINIMUM, BUSY_QUICK and BUSY_KEEP as resources has them.
CPU_SAMPLES, BUSY_WINDOW, BUSY_MINIMUM, BUSY_QUICK, BUSY_KEEP = "cpu-samples.json", 15.0, 5.0, 2.0, 120.0
HOST_CPU_LOAD_INFO, CPU_STATES = 3, 4


def cpu_ticks():
    """The CPU's cumulative ticks, user, system, idle and nice, as resources.cpu_ticks reads them, or None."""
    if sys.platform != "darwin": return None
    try:
        library = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
        library.mach_host_self.restype = ctypes.c_uint
        info, count = (ctypes.c_uint * CPU_STATES)(), ctypes.c_uint(CPU_STATES)
        if library.host_statistics(library.mach_host_self(), HOST_CPU_LOAD_INFO, info, ctypes.byref(count)) != 0:
            return None
    except (OSError, AttributeError): return None
    return list(info)


def busy_share(earlier, later):
    """The percentage of the CPU's ticks between two readings spent outside idle, as resources.busy_share gives it."""
    passed = [(after - before) % (1 << 32) for before, after in zip(earlier, later)]
    total = sum(passed)
    return None if total == 0 else round(100.0 * (total - passed[2]) / total, 1)


def cpu_busy(directory, *, record=True):
    """The CPU's busy share in percent now, None when unreadable, as resources.cpu_busy reads it with wait: against
    a kept sample when one is old enough, else over BUSY_QUICK seconds of its own; it adds its sample."""
    current, at = cpu_ticks(), time.time()
    if current is None: return None
    path = Path(directory) / CPU_SAMPLES
    try:
        kept = [sample for sample in json.loads(path.read_text())
                if isinstance(sample, list) and len(sample) == 1 + CPU_STATES
                and all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in sample)
                and at - BUSY_KEEP <= sample[0] <= at]
    except (OSError, ValueError, TypeError): kept = []
    if record and (not kept or at - max(sample[0] for sample in kept) >= 1):
        with contextlib.suppress(OSError):
            Path(directory).mkdir(parents=True, exist_ok=True)
            partial = path.with_name(f"{CPU_SAMPLES}.{os.getpid()}.{threading.get_ident()}")
            partial.write_text(json.dumps(sorted(kept + [[round(at, 3), *current]])))
            os.replace(partial, path)
    old = [sample for sample in kept if at - sample[0] >= BUSY_MINIMUM]
    full = [sample for sample in old if at - sample[0] >= BUSY_WINDOW]
    base = max(full) if full else min(old) if old else None
    if base is not None: return busy_share(base[1:], current)
    time.sleep(BUSY_QUICK)
    later = cpu_ticks()
    return None if later is None else busy_share(current, later)


def memory_level():
    """The memory pressure level macOS reports, as resources.memory_level reads it: 1 normal, 2 warning, 4 critical,
    or None when it cannot be read."""
    level = output(["/usr/sbin/sysctl", "-n", "kern.memorystatus_vm_pressure_level"]) if sys.platform == "darwin" else None
    return int(level) if level and level.isdigit() else None


def guard_entries():
    """The owners heavy-work admission's guard records, as it writes them, each with its identity."""
    try:
        state = json.loads(Path(os.environ.get("SW_GATE_LOCK_FILE", GATE_LOCK) + ".warm-push-guard").read_text())
    except (OSError, ValueError):
        return []
    owners = state.get("owners") if isinstance(state, dict) and state.get("vocaroEngineering") == 1 else None
    if not isinstance(owners, dict): return []
    return [{**owner, "identity": identity} for identity, owner in owners.items()
            if isinstance(owner, dict) and isinstance(owner.get("pid"), int) and isinstance(owner.get("start"), str)]


def alive(owner):
    """Whether a guard owner's process still runs as itself: its PID has the start its entry records."""
    found = output(["ps", "-p", str(owner["pid"]), "-o", "lstart="])
    return bool(found) and " ".join(found.split()) == " ".join(owner["start"].split())


def names(path, identity):
    """Whether the slot file at path names identity as its last holder, as admission writes it (resources.Admission._names)."""
    try: written = Path(path).read_text(errors="replace").split(" ", 2)
    except OSError: return False
    return len(written) > 1 and written[1] == identity


def stranded_slots(directory, maximum, owners):
    """The slots admission counts the dead or uncertain guard entries among owners as holding, as
    resources.Admission._count_stranded counts them: every slot for exclusive work, else the slots whose files
    name the entry, else the lowest slot not already counted. Admission first recovers each such entry and counts
    only those recovery leaves; this reading runs no census, so it counts every one, and an entry the next
    admission removes may be counted once more here."""
    reserved, unnamed = set(), 0
    for owner in owners:
        if not owner.get("uncertain") and alive(owner): continue
        if owner.get("exclusive"):
            reserved.update(range(maximum))
            continue
        slots = [slot for slot in range(maximum) if names(directory / f"slot{slot}", owner["identity"])]
        if slots: reserved.update(slots)
        else: unnamed += 1
    for _ in range(unnamed):
        free = next((slot for slot in range(maximum) if slot not in reserved), None)
        if free is not None: reserved.add(free)
    return reserved


def queued_ahead(directory, slots, maximum, owners, own=None):
    """The request queued ahead that holds this Mac's later heavy work back now, as ("wholeHost", WAITER), for
    which admission keeps the guaranteed slots, or ("exclusive", WAITER), behind which every later request waits;
    or None. It applies the rule resources.Admission._attempt applies (vocaro-engineering#174, #186, #191): the
    first queued request that holds later work back, an exclusive one whose process runs or a whole-host one
    whose process runs and last asked within HEAVY_PLACE_SECONDS, unless a dead or uncertain guard entry is
    counted as a slot it would wait for, any slot for an exclusive request and a guaranteed one for a whole-host
    request, when it holds nothing back (#89). A request whose process ended and one that has not asked lately
    hold nothing back, and the reading looks past them, as admission does. own is the identity of the asking
    work's own place in this queue (resources.Place): what is queued behind it holds nothing back from it."""
    try: recorded = json.loads((directory / QUEUE).read_text())["waiters"]
    except (OSError, ValueError, KeyError, TypeError): return None
    if not isinstance(recorded, list): return None
    try: hold = float(os.environ.get("HEAVY_PLACE_SECONDS", str(PLACE_SECONDS)))
    except ValueError: hold = PLACE_SECONDS
    for waiter in recorded:
        if not isinstance(waiter, dict) or not isinstance(waiter.get("pid"), int) or not isinstance(waiter.get("start"), str):
            continue
        # The asking work's own place: nothing queued behind it holds it back.
        if own is not None and waiter.get("identity") == own: return None
        if not alive(waiter): continue
        if not waiter.get("wholeHost"):
            # Exclusive work queued ahead holds every request back, unless a stranded entry is counted as any slot.
            return None if stranded_slots(directory, maximum, owners) else ("exclusive", waiter)
        seen = waiter.get("seen")
        if not (isinstance(seen, (int, float)) and not isinstance(seen, bool) and time.time() - seen < hold): continue
        if any(slot < slots for slot in stranded_slots(directory, maximum, owners)): return None
        return "wholeHost", waiter
    return None


def capacity(own=None, *, diagnostic=False):
    """This Mac's spare capacity for heavy work, which `eng run` reads on both Macs before it chooses one
    (remote.spare; vocaro-engineering#168): its guaranteed slots and their maximum; the slots busy among those
    up to the maximum; its CPU's busy share in percent, None when unavailable (cpuBusy), and the limit at or above
    which its admission finds no spare capacity (busyLimit), with its one-minute load beside them, which no longer
    judges (load, vocaro-engineering#200); and the labels of the whole-host lanes and the exclusive work its guard records as
    running; the label of the whole-host request queued ahead for which admission keeps the guaranteed slots, or
    None (kept; own is the asking work's own place, vocaro-engineering#186); the label of the exclusive request
    queued ahead that holds every later request back, or None (queuedExclusive, vocaro-engineering#191), both
    read by queued_ahead; the seconds since admission last took a slot, None when it never has (sinceAdmission),
    HEAVY_SETTLE_SECONDS (settle) and its memory pressure level, None when unread (memory), by which its admission
    withholds a slot beyond the guaranteed ones (resources.Admission._extra_refusal, vocaro-engineering#201); and
    its console screen (screen), which a lane that needs it unlocked reads. A reading it cannot take says why, as
    error, as pressure does. A diagnostic keeps no CPU sample and identifies blockers by admission and process,
    since their command labels can be long and need not identify the work."""
    try: found = limits()
    except ValueError as error: return {"error": str(error)}
    try: directory = slot_directory()
    except RuntimeError as error: return {"error": str(error)}
    try: load = round(os.getloadavg()[0], 2)
    except OSError: load = None
    owners = guard_entries()
    running = [owner for owner in owners if alive(owner)]
    kind, ahead = queued_ahead(directory, found["slots"], found["slotsMax"], owners, own) or (None, None)
    def label(owner, default="an unnamed owner"):
        return f"PID {owner['pid']}" if diagnostic else str(owner.get("label") or default)
    blockers = []
    if diagnostic:
        for owner in running:
            owner_kind = "exclusive" if owner.get("exclusive") else "wholeHost" if owner.get("wholeHost") else None
            if owner_kind:
                blockers.append({"state": "running", "kind": owner_kind, "pid": owner["pid"],
                                 "identity": owner["identity"], "processStart": owner["start"]})
        if ahead:
            blockers.append({"state": "queued", "kind": kind, "pid": ahead["pid"],
                             "identity": ahead.get("identity"), "processStart": ahead["start"]})
    return {"slots": found["slots"], "slotsMax": found["slotsMax"], "busy": busy_slots(directory, found["slotsMax"]),
            "load": load, "cpuBusy": cpu_busy(directory, record=not diagnostic), "busyLimit": found["busyLimit"],
            "wholeHost": [label(owner) for owner in running if owner.get("wholeHost") and not owner.get("exclusive")],
            "exclusive": [label(owner) for owner in running if owner.get("exclusive")],
            "kept": label(ahead, "an unnamed request") if kind == "wholeHost" else None,
            "queuedExclusive": label(ahead, "an unnamed request") if kind == "exclusive" else None,
            "sinceAdmission": since_admission(directory, found["slotsMax"]), "settle": found["settleSeconds"],
            "memory": memory_level(), "screen": screen(), **({"blockers": blockers} if diagnostic else {})}


def admission_log(since):
    """The lines of this Mac's admission log, both generations, oldest first, written at or after since, an epoch
    time, for `eng admissions usage` on the sending Mac (resources.read_entry reads them there). A line whose time
    cannot be read is passed on for that Mac to count as unreadable."""
    directory, found = admission_directory(), []
    for name in ADMISSION_LOGS:
        try: lines = (directory / name).read_text(errors="replace").splitlines()
        except OSError: continue
        for line in lines:
            try: moment = datetime.fromisoformat(json.loads(line)["time"]).timestamp()
            except (ValueError, KeyError, TypeError): moment = None
            if moment is None or moment >= since: found.append(line)
    return {"lines": found}


def unended():
    """(PID, when it was admitted, in seconds since the epoch) of each admission the admission log
    records without an end: one still held, or one SIGKILL ended."""
    directory = admission_directory()
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
    file, as its checkout's slot lock records it, for the moments between its admissions, such as its
    checkout's placement. Some of a run's processes launchd starts or adopts, and those a live run holds are
    its own, with everything below them. A simulator's processes descend from launchd_sim, never
    from a run, so a device is a live run's while the device record holds it for one of that run's
    processes, as the pool records the process that reserved a member, or while one of them names
    it. An Xcode program, such as the DTServiceHub that xcodebuild leaves behind, or a bundle's
    executable in the checkout, such as the app a UI test launches and the XPC services it starts,
    is a live run's when it started in a checkout after a live admission there began. Each leftover whose parent is
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
        """Whether a live run holds the process: its simulator, or, for an Xcode program or a bundle's
        executable in the checkout, the checkout it started in after that run's admission there began."""
        if device: return device in held or device in named
        if kind != "checkout": return False
        args = table[pid]["args"]
        bundle = BUNDLE.match(args)
        if not XCODE.match(args) and not (bundle and slot(bundle.group(0)) == slot(path)): return False
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
    # This file stays ASCII: it reaches the second Mac as a command-line argument.
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
    run's own offload_host.py, whose plan names it while the run lasts; and each leftover that started
    since the run did. Another recovery's census of the device, whose request names it too, only reads,
    so it is not one: two recoveries checking at once would otherwise each hold the other's (#80).
    It reads and reports, and stops nothing."""
    udid = request["udid"].upper()
    table = process_table(listed(["ps", "-axww", "-o", "pid=,ppid=,uid=,lstart=,args="]))
    if not table: return {"error": "ps listed no processes"}
    own, uid = set(lineage(table, os.getpid())), os.getuid()
    # The start of the request remote.device_users sends for this device.
    census = json.dumps({"device": {"udid": udid}})[:-2]
    naming = [{"pid": pid, "start": process["start"], "command": process["args"][:COMMAND_LIMIT]}
              for pid, process in table.items()
              if process["uid"] == uid and pid not in own and udid in process["args"].upper()
              and census not in process["args"]]
    census = leftovers(request["root"], listed, written)
    if "error" in census: return census
    since = request.get("since")
    later = [row for row in census["leftovers"] if since is None or (started(row["start"]) or since) >= since - 1]
    return {**census, "leftovers": later, "naming": naming}


class ReapProblem(RuntimeError):
    """What reap could not read or do, which leaves the simulator running."""


def start_shutdown(udid):
    """`xcrun simctl shutdown UDID` started: the only change reap makes to a simulator."""
    return subprocess.Popen(["xcrun", "simctl", "shutdown", udid], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)


def known_pools(root):
    """The simulator pools that the projects' checkouts under root name in engineering.json."""
    found = set()
    for path in (Path.home() / root / "checkouts").glob("*/*/*/engineering.json"):
        try: name = json.loads(path.read_text()).get("simulatorPool")
        except (OSError, ValueError, AttributeError): continue
        if isinstance(name, str) and name: found.add(name)
    return found


def pool_of(name, pools, runtime=""):
    """The pool a simulator belongs to and as what: (POOL, "member") for a name `POOL LABEL N`, and
    (POOL, "template") for its kind's warmed template, `POOL LABEL template (RUNTIME)` on that runtime, as
    simulators/pool.py lists one; else (None, None). Reaping may shut down a template too, by the owner's
    decision of 2026-11-04 (guides/shared-host.md, Stale pool simulators)."""
    for pool in sorted(pools):
        if not name.startswith(pool + " "): continue
        rest = name[len(pool) + 1:]
        if MEMBER.fullmatch(rest): return pool, "member"
        match = TEMPLATE.fullmatch(rest)
        if match and match["runtime"] == runtime.rsplit(".", 1)[-1]: return pool, "template"
    return None, None


def template_marker(device):
    """The per-device provenance file, outside the data directory a clone or copy inherits."""
    udid, data = device.get("udid"), device.get("dataPath")
    if not isinstance(udid, str) or not DEVICE.fullmatch(udid) or not isinstance(data, str): return None
    path = Path(data)
    if not path.is_absolute() or path.name != "data" or path.parent.name.upper() != udid.upper(): return None
    return path.parent / ".vocaro-pool-template.json"


def template_provenance(device):
    """An exact name, runtime and UUID match to a pool template recorded before its first boot."""
    marker = template_marker(device)
    if marker is None or marker.is_symlink() or not marker.is_file(): return None
    try: origin = json.loads(marker.read_text())
    except (OSError, ValueError): return None
    if not isinstance(origin, dict) or type(origin.get("version")) is not int or origin["version"] != 1: return None
    pool, holder = origin.get("pool"), origin.get("holder")
    name, runtime, udid = (device.get(key) for key in ("name", "runtime", "udid"))
    if any(origin.get(key) != value for key, value in (("name", name), ("runtime", runtime), ("udid", udid.upper()))):
        return None
    if not isinstance(pool, str) or not pool or not isinstance(name, str) or not isinstance(runtime, str) or \
            pool_of(name, [pool], runtime) != (pool, "template"):
        return None
    if not isinstance(holder, dict) or type(holder.get("pid")) is not int or holder["pid"] <= 0 or \
            not isinstance(holder.get("start"), str) or started(holder["start"]) is None:
        return None
    return origin


def device_entries():
    """The device record's entries by key. A record that cannot be read as a whole raises, because
    any entry in it may hold the simulator; none yet holds nothing."""
    path = record_path()
    try:
        record = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:
        raise ReapProblem(f"the device record {path} could not be read ({error})") from None
    entries = record.get("devices") if isinstance(record, dict) and record.get("vocaroDeviceHandoff") == 1 else None
    if not isinstance(entries, dict) or not all(isinstance(entry, dict) for entry in entries.values()):
        raise ReapProblem(f"{path} is no version 1 device record")
    return entries


def require_admissions():
    """Raise unless the guard and the admission log can be read, which the census otherwise reads as
    no admission at all. Neither exists until something is admitted."""
    guard = Path(os.environ.get("SW_GATE_LOCK_FILE", GATE_LOCK) + ".warm-push-guard")
    try:
        state = json.loads(guard.read_text())
        if not isinstance(state, dict) or state.get("vocaroEngineering") != 1 or not isinstance(state.get("owners"), dict):
            raise ValueError("it is no guard")
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as error:
        raise ReapProblem(f"the admission guard {guard} could not be read ({error})") from None
    for name in ADMISSION_LOGS:
        try: (admission_directory() / name).read_bytes()
        except FileNotFoundError: continue
        except OSError as error:
            raise ReapProblem(f"the admission log could not be read ({error})") from None


def reservation_hold(key, entry, table, due_holds=True):
    """Why a record entry keeps its simulator running, or None, and what a reservation that does not
    says of itself. A recorded process is live while its PID runs with its recorded start. An entry
    that records none, or none that can be read, or that is marked uncertain, may be live: only a
    process known to be gone, or a PID with another start, shows that it is not. With due_holds False,
    an expected release not yet passed holds nothing here, for release_dead to judge."""
    if entry.get("uncertain") or entry.get("state") == "restoration-unverified":
        return f"its reservation {key} is uncertain, kept for recovery", None
    if entry.get("state") == "released" and not entry.get("activeRun"): return None, None
    due = entry.get("expectedReleaseBy")
    if due is not None:
        # A session's `simulator-pool take` ends when it prints the UDID, so until its release is due
        # the session's word holds the member, whatever became of that process.
        try: ends = datetime.fromisoformat(str(due).replace("Z", "+00:00")).timestamp()
        except (ValueError, OverflowError, OSError):
            return f"its reservation {key} records an expected release that cannot be read", None
        if ends > time.time() and due_holds:
            return f"its reservation {key} is not due for release until {due}", None
    recorded = ([entry[HOLDER]] if HOLDER in entry else [])
    if entry.get("activeRun"):
        recorded.append({"pid": entry.get("hostProcessIdentifier"), "start": entry.get("hostProcessStart")})
    owned = entry.get("ownedProcesses") or []
    recorded += owned if isinstance(owned, list) else [owned]
    if not recorded: return f"its reservation {key} records no process, so it may be live", None
    for item in recorded:
        pid = item.get("pid") if isinstance(item, dict) else None
        if type(pid) is not int or pid <= 0:
            return f"its reservation {key} records a process that cannot be read", None
        if pid not in table: continue
        start = item.get("start")
        if not isinstance(start, str):
            return f"its reservation {key} records PID {pid}, which runs, with no start to compare", None
        if " ".join(start.split()) == table[pid]["start"]:
            return f"its reservation {key} is live: PID {pid} still runs", None
    return None, f"reservation {key}'s recorded process is gone, and reap releases its entry once it proves it dead"


def recoverable(entries, table, early=False):
    """The record's held entries that nothing keeps: each records a process, known to be gone, and an
    expected release that has passed (reservation_hold says the rest). Reap releases each that it proves
    dead (release_dead) and names the rest, with why it left each and the command that recovers it
    (vocaro-engineering#97, #156). With early, instead the entries like these whose expected release has
    not yet passed, each marked early."""
    found = []
    for key, entry in entries.items():
        hold, gone = reservation_hold(key, entry, table, due_holds=not early)
        if hold is not None or gone is None or entry.get("expectedReleaseBy") is None: continue
        if early != (passed(entry["expectedReleaseBy"]) is False): continue
        owned = entry.get("ownedProcesses") or []
        run = [{"pid": entry.get("hostProcessIdentifier")}] if entry.get("activeRun") else []
        recorded = [entry.get(HOLDER), *run, *(owned if isinstance(owned, list) else [owned])]
        found.append({"key": key, "device": entry.get("device"), "owner": entry.get("ownerChatID"),
                      "due": entry.get("expectedReleaseBy"), "pids": [item[0] for item in identities(recorded)],
                      "revision": entry.get("revision", 0), **({"early": True} if early else {}),
                      "command": f"python3 -m vocaro_engineering.devices recover {shlex.quote(key)} --by SESSION "
                                 f"--revision {entry.get('revision', 0)} --verified 'WHAT YOU CHECKED'"})
    return found


def passed(due):
    """Whether an expected release, as ISO 8601 text, has passed; None when it cannot be read."""
    try: return datetime.fromisoformat(str(due).replace("Z", "+00:00")).timestamp() <= time.time()
    except (ValueError, OverflowError, OSError): return None


def recoverable_line(item, host=None):
    """What is said of one entry recoverable found and reap left, with why it left it when it says. For an
    entry in another Mac's record, host, the command runs there, in its toolkit checkout, since a recovery
    here would read this Mac's record and refuse its revision (vocaro-engineering#156)."""
    command = item["command"]
    if host:
        command = f"ssh {shlex.quote(host)} {shlex.quote('cd ' + TOOLKIT + ' && ' + command)}"
    left = f"; reap did not release it: {item['hold']}" if item.get("hold") else ""
    return (f"{item['key']} stays held for owner {item['owner']}, though its process "
            f"{', '.join(map(str, item['pids']))} is gone and its release "
            f"{'is due' if item.get('early') else 'was due'} {item['due']}{left}. Once its "
            f"work has stopped, its owner or a session they agreed recovers it: {command}")


def released_line(item):
    """What is said of one entry release_dead released, or in a dry run would release."""
    done = "would release" if item.get("dryRun") else "released"
    return (f"{done} {item['key']} ({item['device']}), held for owner {item['owner']}: {item['verified']} "
            f"(a recovery by reap in the device record; {REAP_LOG})")


def device_record_whole():
    """The device record as a whole, for a change; none yet is an empty one, as devices.Record.load reads it.
    One that cannot be read raises."""
    path = record_path()
    try:
        record = json.loads(path.read_text())
    except FileNotFoundError:
        return {"vocaroDeviceHandoff": 1, "revision": 0, "devices": {}}
    except (OSError, ValueError) as error:
        raise ReapProblem(f"the device record {path} could not be read ({error})") from None
    if not isinstance(record, dict) or record.get("vocaroDeviceHandoff") != 1 or \
            not isinstance(record.get("devices"), dict):
        raise ReapProblem(f"{path} is no version 1 device record")
    return record


def replace_record(record):
    """Write the record whole, as devices.Record._replace does, which this file cannot import: its revision
    one higher, staged beside it, synced, and moved into place."""
    path = record_path()
    record["revision"] = record.get("revision", 0) + 1
    record["updatedAt"] = datetime.now(timezone.utc).isoformat()
    staged = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=".device-handoff-", delete=False) as stream:
            staged = Path(stream.name)
            json.dump(record, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staged, path)
        staged = None
    finally:
        if staged is not None:
            with contextlib.suppress(OSError): staged.unlink()
    directory = os.open(path.parent, os.O_RDONLY)
    try: os.fsync(directory)
    finally: os.close(directory)


def release_hold(item, entries, table, simulators, pools, stopping=(), *, discover_templates=False):
    """Why release_dead must leave one entry recoverable found, or None when this Mac proves it dead: a pool
    simulator's reservation, of the device simctl here lists under that name and shows Shutdown, whose run
    went to no other Mac, and which no other entry of the device still holds, with this reap's own process
    in the listing that shows the others gone. recoverable has already shown that every process the entry
    records is gone, by PID and start, and that its expected release has passed. A dry run passes in stopping
    the devices it would shut down first, which it judges as if they were Shutdown."""
    key, entry = item["key"], entries[item["key"]]
    udid = str(entry.get("device") or "").upper()
    if not DEVICE.fullmatch(udid) or PHYSICAL.fullmatch(udid):
        return "it records no simulator, and reap releases only a simulator pool's reservations"
    run = entry.get("run")
    if isinstance(run, dict) and run.get("host"):
        return f"its run went to {run['host']}, whose processes this Mac cannot check"
    device = simulators.get(udid)
    if device is None: return f"simctl here does not list {udid}"
    if device.get("name") != key: return f"simctl lists {udid} as {device.get('name')!r}, not {key!r}"
    if pool_of(key, pools, device.get("runtime") or "")[0] is None:
        origin = template_provenance(device) if discover_templates else None
        if not origin or identities([origin["holder"]]) != identities([entry.get(HOLDER)]):
            return "it is neither a member nor a warmed template of the pools " + ", ".join(sorted(pools))
    if device.get("state") != "Shutdown" and udid not in stopping:
        return f"simctl shows it {device.get('state') or 'in no state'}, not Shutdown"
    for other, held in entries.items():
        if other != key and str(held.get("device") or "").upper() == udid and \
                (held.get("state") != "released" or held.get("activeRun") or held.get("uncertain")):
            return f"its device is also held as {other}"
    if os.getpid() not in table:
        return "the process listing does not show this reap's own process, so it cannot show another is gone"
    if item.get("early") and entry.get(ALONE) is not True:
        return (f"its release is not due until {item['due']}, and it does not record that only its holder held it, "
                "as `eng simulator-pool take`'s does not: that command's session holds the member by its word, and a "
                "session between commands leaves no process that names it")
    return None


def accounted(item, environments, admissions, simulator=None):
    """What accounts for the owner of an entry release_hold would release early, or None when nothing does:
    a live admission, as the guard or the admission log records it, whose process or a descendant names the
    owner in its command line or environment; any other process of the account that does; or one that names
    the device's UDID. environments is the table of an ENVIRONMENTS listing, and admissions the PIDs of live
    admissions. An owner that cannot be searched for, or a listing that shows no environment, accounts for
    it, so that only a search that could have found the owner releases the entry. simulator, the PID of the
    device's own launchd_sim while it still runs, is left out of the search for its UDID with its descendants,
    whose paths all name it."""
    owner, udid = item.get("owner"), str(item.get("device") or "").upper()
    if not isinstance(owner, str) or len(owner.strip()) < 8:
        return f"its owner {owner!r} is too short to search processes for"
    own = environments.get(os.getpid())
    if own is None or "PATH=" not in own["args"]:
        return "ps did not show this reap's own environment, so it cannot show that no process names the owner"
    uid = os.getuid()
    for pid in sorted(admissions):
        for other, process in environments.items():
            if pid in lineage(environments, other) and owner in process["args"]:
                return f"live admission PID {pid} accounts for its owner: PID {other} names {owner}"
    for other, process in sorted(environments.items()):
        # This reap's own processes too: a session that reaps may be the owner.
        if process["uid"] != uid: continue
        if owner in process["args"]: return f"PID {other} names its owner {owner}"
        if udid and udid in process["args"].upper() and simulator not in lineage(environments, other):
            return f"PID {other} names its device {udid}"
    return None


def live_admissions(table):
    """The PIDs of the admissions that the guard or the admission log records as live in table, as the census
    counts them (live_runs). Either that cannot be read raises."""
    require_admissions()
    return live_runs(table, guard_owners(), unended())


def release_dead(found, entries, table, pools, listed, dry_run, stopping=None, *, discover_templates=False):
    """Release each entry of found that release_hold proves dead, as `devices recover --by reap` would, and
    log it; the rest come back with why each stays (owner's decision, 2026-11-04, vocaro-engineering#156).
    entries is the record as read before table's ps listing, so that a process gone from that listing was
    gone after the entry recorded it; under the record's lock, an entry whose revision has moved since is
    left. stopping maps each device this reap shut down, or in a dry run would, to its launchd_sim's PID.
    Return (released, left)."""
    stopping = stopping or {}
    try:
        groups = json.loads(listed(list(SIMULATORS)) or "")["devices"]
        simulators = {device["udid"].upper(): {**device, "runtime": runtime}
                      for runtime, group in groups.items() for device in group}
    except (TypeError, ValueError, KeyError, AttributeError) as error:
        why = f"simctl did not list its devices ({error!r}), so no device can be shown Shutdown"
        return [], [{**item, "hold": why} for item in found]
    released, left, ready = [], [], []
    for item in found:
        hold = release_hold(item, entries, table, simulators, pools, stopping if dry_run else (),
                            discover_templates=discover_templates)
        if hold: left.append({**item, "hold": hold})
        else: ready.append(item)
    if any(item.get("early") for item in ready):
        # Before its expected release, an entry goes only when nothing accounts for its owner, by a census that
        # can see each process's environment (vocaro-engineering#156).
        try:
            environments = process_table(listed(list(ENVIRONMENTS)))
            if not environments: raise ReapProblem("ps listed no processes with their environments")
            admissions, problem = live_admissions(environments), None
        except Exception as error:
            problem = str(error) if isinstance(error, ReapProblem) else f"{type(error).__name__}: {error}"
        waiting, ready = [item for item in ready if item.get("early")], [item for item in ready if not item.get("early")]
        for item in waiting:
            why = (f"its release is not due until {item['due']}, and its owner could not be checked: {problem}"
                   if problem else accounted(item, environments, admissions,
                                             stopping.get(str(item.get("device") or "").upper())))
            if why and not problem: why = f"its release is not due until {item['due']}, and {why}"
            if why: left.append({**item, "hold": why})
            else: ready.append({**item, "checked": "no live admission or other process of the account names its "
                                                   f"owner {item['owner']} or its device, in ps with environments"})
    for item in ready:
        entry = entries[item["key"]]
        owned = entry.get("ownedProcesses") or []
        recorded = [entry.get(HOLDER), *([{"pid": entry.get("hostProcessIdentifier"),
                                            "start": entry.get("hostProcessStart")}] if entry.get("activeRun") else []),
                    *(owned if isinstance(owned, list) else [owned])]
        pids = ", ".join(f"PID {pid}" + (f" (started {start})" if start else "") for pid, start in identities(recorded))
        due = (f"its expected release, {item['due']}, had not passed, but it recorded that only its holder held "
               f"it, and {item['checked']}" if item.get("early") else f"its expected release, {item['due']}, had passed")
        shown = (f"simctl would show {str(entry['device']).upper()} Shutdown once this reap stopped it"
                 if dry_run and str(entry["device"]).upper() in stopping
                 else f"simctl showed {str(entry['device']).upper()} Shutdown")
        item.update(verified=f"reap on this Mac found {pids} no longer running, by PID and start in ps; {due}; and "
                             f"{shown}",
                    qualified="early: nothing accounts for its owner" if item.get("early") else "its release passed",
                    condition="orphan" if entry.get("activeRun") else "held")
    if dry_run:
        return [{**item, "dryRun": True} for item in ready], left
    if not ready: return [], left
    with record_lock():
        record = device_record_whole()
        current, moment = record["devices"], datetime.now(timezone.utc).isoformat()
        for item in ready:
            entry = current.get(item["key"])
            if not isinstance(entry, dict) or entry.get("revision", 0) != item["revision"]:
                left.append({**item, "hold": "its entry changed after reap read it"})
                continue
            changed = {name: value for name, value in entry.items()
                       if name not in ("revision", "uncertain", "uncertainReason")}
            changed.update({"state": "released", "activeRun": False, "ownerChatID": None,
                            "lastOwnerChatID": entry.get("ownerChatID"), "releasedAt": moment,
                            "recovery": {"by": "reap", "at": moment, "verified": item["verified"],
                                         "condition": item["condition"], "state": entry.get("state"),
                                         "qualified": item["qualified"],
                                         "uncertainReason": entry.get("uncertainReason")}})
            current[item["key"]] = {"revision": item["revision"] + 1, **changed}
            released.append(item)
        if not released: return [], left
        # Logged first, as a stop is: a log that cannot be written releases nothing.
        for item in released: log_release("releasing", item)
        replace_record(record)
    for item in released: log_release("released", item)
    at = datetime.fromisoformat(moment)
    for item in released:
        entry = entries[item["key"]]
        if entry.get(TAKEN) and entry.get(ALONE) is not True:
            log_hold(hold_line("reap", item["key"], entry, at, after=None,
                               ended=f"Released by reap: {item['qualified']}"))
    return released, left


def stale_hold(key, entry, table, udid, launchd, seen):
    """reservation_hold for a booted simulator observe looks at, but a run's reservation (ALONE) not yet due
    holds it only while something accounts for its owner, by the census release_dead reads (accounted), with
    the simulator's own processes left out: one whose recorded processes are all gone and whose owner nothing
    accounts for is stale before its expected release, so that reap shuts it down and then releases its entry
    in the same run (the owner's decision, 2026-11-05, vocaro-engineering#156). A take's reservation, which
    records no ALONE, and any other keep today's rule: held until its release is due."""
    hold, note = reservation_hold(key, entry, table)
    if hold is None or entry.get(ALONE) is not True: return hold, note
    later, gone = reservation_hold(key, entry, table, due_holds=False)
    if later is not None or gone is None: return hold, note
    try:
        environments = process_table(seen(list(ENVIRONMENTS)))
        if not environments: raise ReapProblem("ps listed no processes with their environments")
        admissions = live_admissions(environments)
    except Exception as error:
        why = str(error) if isinstance(error, ReapProblem) else f"{type(error).__name__}: {error}"
        return f"{hold}, and its owner could not be checked: {why}", None
    why = accounted({"owner": entry.get("ownerChatID"), "device": udid}, environments, admissions, launchd)
    if why: return f"{hold}, and {why}", None
    return None, (f"reservation {key} is a run's whose recorded processes are gone, and nothing accounts for its "
                  f"owner, so reap shuts it down before its expected release, {entry.get('expectedReleaseBy')}, "
                  "and then releases its entry")


def observe(root, pools, listed, *, discover_templates=False):
    """One look at the simulators the census lists as left over: for each launchd_sim by UDID, its
    PID, start, age, name and reason, and hold, why it must keep running, or None. Nothing here
    lists simctl's devices unless the census finds a simulator. Anything unread raises."""
    answers = {}

    def seen(command):
        # One listing of each kind per look, so that every condition reads the same moment.
        key = tuple(command)
        if key not in answers: answers[key] = listed(command)
        return answers[key]

    census = leftovers(root, seen, lambda pid: None)
    if "error" in census: raise ReapProblem(census["error"])
    if census["unchecked"]: raise ReapProblem("the census could not read " + "; ".join(census["unchecked"]))
    require_admissions()
    rows = [row for row in census["leftovers"] if row.get("simulator") and LAUNCHD_SIM.match(row["command"])]
    if not rows: return {}
    table = process_table(seen(["ps", "-axww", "-o", "pid=,ppid=,uid=,lstart=,args="]))
    try:
        groups = json.loads(seen(list(SIMULATORS)))["devices"]
        simulators = {device["udid"].upper(): {**device, "runtime": runtime}
                      for runtime, group in groups.items() for device in group}
    except (TypeError, ValueError, KeyError, AttributeError) as error:
        raise ReapProblem(f"simctl did not list its devices ({error!r})") from None
    entries = device_entries()
    own, uid, found = set(lineage(table, os.getpid())), os.getuid(), {}
    for row in rows:
        udid, pid = row["simulator"], row["pid"]
        device, seconds = simulators.get(udid) or {}, row.get("seconds")
        name = device.get("name") or "unnamed"
        reason, hold = None, None
        keys = [key for key, entry in entries.items() if key == name or str(entry.get("device") or "").upper() == udid]
        holds = [stale_hold(key, entries[key], table, udid, pid, seen) for key in keys]
        naming = [other for other, process in table.items() if process["uid"] == uid and other not in own
                  and pid not in lineage(table, other) and udid in process["args"].upper()]
        pool, what = pool_of(name, pools, device.get("runtime") or "")
        origin = template_provenance(device) if what == "template" or (discover_templates and pool is None) else None
        creator = origin["holder"] if origin else None
        if origin and pool is None:
            pool, what = origin["pool"], "template"
        if sum(other["simulator"] == udid for other in rows) > 1: hold = "more than one launchd_sim runs for it"
        elif device.get("state") != "Booted": hold = f"simctl shows it {device.get('state') or 'unlisted'}, not Booted"
        elif pool is None: hold = "it is neither a member of a simulator pool nor a pool's warmed template"
        elif creator and table.get(creator["pid"], {}).get("start") == " ".join(creator["start"].split()):
            hold = f"its template creator PID {creator['pid']} still runs"
        elif naming: hold = f"PID {naming[0]} names its UDID"
        elif any(item[0] for item in holds): hold = next(item[0] for item in holds if item[0])
        elif seconds is None: hold = "its launchd_sim's start cannot be read"
        elif seconds < REAP_AFTER: hold = f"it has run {seconds / 3600:.1f} h, under {REAP_AFTER / 3600:g} h"
        if hold is None:
            note = next((item[1] for item in holds if item[1]), None)
            reason = (f"{'warmed template' if what == 'template' else 'member'} of pool {pool}, left over: no live "
                      f"run, admission or process accounts for it, no live reservation holds it, and it has run "
                      f"{seconds / 3600:.1f} h") + (f"; {note}" if note else "")
        found[udid] = {"udid": udid, "name": name, "pid": pid, "start": row["start"], "seconds": seconds,
                       "hold": hold, "reason": reason}
    return found


@contextlib.contextmanager
def record_lock():
    """The lock each change of the device record takes (devices.Record), held only for the last check
    and the start of a stop, so that no reservation lands between them."""
    path = record_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path.with_name(path.name + ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        began = time.monotonic()
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() - began >= REAP_LOCK_SECONDS:
                    raise ReapProblem(f"the device record stayed locked for {REAP_LOCK_SECONDS}s") from None
                time.sleep(0.02)
        yield
    finally:
        os.close(descriptor)


def log_reap(event, item, **more):
    """Append one line to the log of stops, which stays small: one stop writes two lines of about 400 bytes."""
    line = {"time": datetime.now(timezone.utc).isoformat(timespec="seconds"), "event": event,
            **{key: item[key] for key in ("udid", "name", "pid", "start", "seconds", "reason")}, **more}
    path = admission_directory() / REAP_LOG
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NONBLOCK, 0o644)
    try: os.write(descriptor, (json.dumps(line) + "\n").encode())
    finally: os.close(descriptor)


def log_release(event, item):
    """Append one release of an entry to the log of stops, with what reap checked."""
    line = {"time": datetime.now(timezone.utc).isoformat(timespec="seconds"), "event": event,
            **{key: item.get(key) for key in ("key", "device", "owner", "due", "pids", "revision", "qualified",
                                              "verified")}}
    path = admission_directory() / REAP_LOG
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NONBLOCK, 0o644)
    try: os.write(descriptor, (json.dumps(line) + "\n").encode())
    finally: os.close(descriptor)


def hold_line(event, key, entry, at, *, after, ended=None, **more):
    """A HOLD_LOG line for one reservation of a member taken with `take`: entry is its record entry before this
    event, at when the event happened (a datetime), after its expected release once the event is done (None
    once it has ended) and ended how it ended (None while it is held)."""
    start = entry.get("reservedAt")
    try: held = round(at.timestamp() - datetime.fromisoformat(str(start).replace("Z", "+00:00")).timestamp())
    except (ValueError, OverflowError, OSError): held = None
    return {"time": at.isoformat(timespec="seconds"), "event": event, "pool": entry.get(TAKEN), "key": key,
            "udid": str(entry.get("device") or "").upper() or None, "owner": entry.get("ownerChatID"),
            "reservedAt": start, "heldSeconds": held, "expectedReleaseBefore": entry.get("expectedReleaseBy"),
            "expectedReleaseAfter": after, "ended": ended, **more}


def log_hold(line, directory=None):
    """Append a line to HOLD_LOG; True once written. It never fails its command: a log that cannot be written
    is warned about on stderr and the command goes on."""
    try:
        path = (directory or admission_directory()) / HOLD_LOG
        data = (json.dumps(line) + "\n").encode()
        path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            size = path.stat().st_size
            if size and size + len(data) > HOLD_LOG_LIMIT:
                path.replace(path.with_name(PREVIOUS_HOLD_LOG))
        descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NONBLOCK, 0o644)
        try: os.write(descriptor, data)
        finally: os.close(descriptor)
        return True
    except Exception as error:
        with contextlib.suppress(Exception):
            print(f"eng simulator-pool: the hold log {HOLD_LOG} was not written ({error}); nothing else changed",
                  file=sys.stderr, flush=True)
        return False


def stop_simulator(item, reader, budget, begin, report, *, discover_templates=False):
    """Check item once more under the device record's lock, and if it is still the same stale simulator,
    log it and start its one shutdown; then report how that ended. Nothing retries it."""
    with record_lock():
        again = observe(report["root"], report["pools"], reader(min(budget(), REAP_CHECK_SECONDS)),
                        discover_templates=discover_templates).get(item["udid"])
        if again is None or again["hold"] or (again["pid"], again["start"]) != (item["pid"], item["start"]):
            why = "the census no longer lists it" if again is None else again["hold"] or "another launchd_sim runs for it"
            report["skipped"].append({**item, "hold": f"it changed before the second check passed: {why}"})
            return
        item = again
        log_reap("stopping", item)
        process = begin(item["udid"])
    try:
        # At least long enough to say something of a stop that started late in the budget.
        said = process.communicate(timeout=max(budget(), 10))[1]
    except subprocess.TimeoutExpired:
        # This ends only xcrun, which this file started, and asks nothing of the simulator.
        process.kill()
        with contextlib.suppress(subprocess.TimeoutExpired): process.communicate(timeout=1)
        report["failed"].append({**item, "error": "simctl shutdown did not finish in time, so its state is unknown"})
        log_reap("failed", item, error=report["failed"][-1]["error"])
        return
    if process.returncode:
        error = f"simctl shutdown exited {process.returncode}: {(said or '').strip()}"
        report["failed"].append({**item, "error": error})
        log_reap("failed", item, error=error)
        return
    report["stopped"].append(item)
    log_reap("stopped", item)


def reap(root, pools, dry_run=False, scan=True, listed=None, begin=None, clock=time.monotonic):
    """stop_stale, then release_dead: each held entry of the record that it proves dead it releases, in
    released (in a dry run, says it would), and the others that nothing keeps it names in recoverable, each
    with why it stays. A problem in stop_stale releases nothing. It never raises."""
    report = stop_stale(root, pools, dry_run, scan, listed, begin, clock)
    report["recoverable"], report["released"] = [], []
    try:
        # Read before the processes are listed: an entry that records a process the listing then lacks
        # recorded it before it was gone.
        entries = device_entries()
        # As if no process ran: what could be named at all, without which nothing need be listed.
        if recoverable(entries, {}) or recoverable(entries, {}, early=True):
            look = listed or (lambda command: listing(command, REAP_CHECK_SECONDS))
            processes = look(["ps", "-axww", "-o", "pid=,ppid=,uid=,lstart=,args="])
            if not processes: raise ReapProblem("the processes could not be listed, so no held entry can be named")
            table = process_table(processes)
            found = recoverable(entries, table) + recoverable(entries, table, early=True)
            if found and report["problem"]:
                why = "this reap met a problem, so it releases nothing"
                report["recoverable"] = [{**item, "hold": why} for item in found]
            elif found and not report["pools"] and not scan:
                report["recoverable"] = [{**item, "hold": "no simulator pool was named or found"} for item in found]
            elif found:
                report["recoverable"] = found
                stopping = {item["udid"]: item["pid"] for item in report["stopped"] + report["eligible"]}
                report["released"], report["recoverable"] = release_dead(found, entries, table, report["pools"],
                                                                         look, dry_run, stopping, discover_templates=scan)
    except Exception as error:
        report["problem"] = report["problem"] or (str(error) if isinstance(error, ReapProblem)
                                                  else f"{type(error).__name__}: {error}")
    return report


def stop_stale(root, pools, dry_run=False, scan=True, listed=None, begin=None, clock=time.monotonic):
    """Shut down the simulators that are stale: `eng simulator-pool reap`, and before work is sent to the
    second Mac. One goes only when it is a member of a pool or the pool's warmed template, among pools or,
    with scan, those the checkouts name; the census lists it left over; nothing naming its UDID runs outside its own processes;
    the device record holds no live reservation of it; and its launchd_sim has run REAP_AFTER; all
    checked again immediately before the stop, under the record's lock. It reads and logs, and never
    erases, deletes or kills. It never raises: whatever it cannot read or do, it says in problem and
    stops nothing further. It takes about REAP_SECONDS at most; listed and begin are for tests.
    It answers with stopped, failed, skipped (stale at first, then changed or out of time), eligible
    (for a dry run) and kept, each with why."""
    report = {"root": root, "pools": sorted(set(pools)), "dryRun": dry_run, "stopped": [], "failed": [], "skipped": [],
              "eligible": [], "kept": [], "problem": None}
    try:
        deadline = clock() + REAP_SECONDS
        budget = lambda: max(1, deadline - clock())

        def reader(seconds):
            # What one look may spend across its commands.
            if listed: return listed
            end = clock() + seconds
            return lambda command: listing(command, max(1, end - clock()))

        if scan: report["pools"] = sorted({*pools, *known_pools(root)})
        if not report["pools"] and not scan: return report
        found = observe(root, report["pools"], reader(min(budget(), 60)), discover_templates=scan)
        report["kept"] = [item for item in found.values() if item["hold"]]
        stale = [item for item in found.values() if not item["hold"]]
        if dry_run:
            report["eligible"] = stale
            return report
        for item in stale:
            if deadline - clock() < REAP_STOP_SECONDS:
                report["skipped"].append({**item, "hold": f"this reap's {REAP_SECONDS}s were used up"})
                continue
            stop_simulator(item, reader, budget, begin or start_shutdown, report, discover_templates=scan)
    except Exception as error:
        report["problem"] = f"{type(error).__name__}: {error}" if not isinstance(error, ReapProblem) else str(error)
    return report


def advertise(repository):
    """Name each commit that a checkout of repository holds as CACHE + COMMIT, and remove every other
    ref under CACHE, so that the next push to repository sends only what its checkouts lack. The refs
    are no more than the checkouts. One ref per commit, not per checkout: sessions that advertise at
    once write the same refs, and a checkout that moves meanwhile costs a push its saving, never its
    commit. Return the commits advertised, or why none were."""
    try:
        held = set()
        for block in git("--git-dir", str(repository), "worktree", "list", "--porcelain").split("\n\n"):
            fields = dict((line.split(" ", 1) + [""])[:2] for line in block.splitlines())
            # A checkout whose directory is gone is pruned when its slot is next claimed.
            if "prunable" not in fields and COMMIT.fullmatch(fields.get("HEAD", "")) and fields["HEAD"].strip("0"):
                held.add(fields["HEAD"])
        refs = set(git("--git-dir", str(repository), "for-each-ref", "--format=%(refname)", CACHE).split())
        wanted = {CACHE + commit for commit in held}
        changes = "".join([f"update {ref} {ref[len(CACHE):]}\n" for ref in sorted(wanted - refs)]
                          + [f"delete {ref}\n" for ref in sorted(refs - wanted)])
        # Another session's transaction can hold a ref's lock for a moment; its refs serve as well.
        for attempt in range(2):
            if not changes: break
            done = subprocess.run(["git", "--git-dir", str(repository), "update-ref", "--stdin"], input=changes,
                                  capture_output=True, text=True, timeout=60)
            if done.returncode == 0: break
            if attempt: raise RuntimeError(f"git update-ref failed: {done.stderr.strip()}")
            time.sleep(0.2)
        return {"commits": sorted(held)}
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        return {"error": str(error)}


def pressure():
    """This Mac's CPU's busy share in percent, None when unavailable (cpuBusy); the limit at or above which its
    heavy-work admission finds no spare capacity, HEAVY_BUSY_LIMIT, the host's limits file or 75, as
    resources.busy_limit sets it (limits); its memory pressure level, None when unread; and its one-minute load,
    which the merge queue logs. The merge queue judges them as admission does (resources.judged) before it runs
    again a lane that ran here and gave no verdict (guides/maintenance.md, The merge queue)."""
    try: load = round(os.getloadavg()[0], 2)
    except OSError: load = None
    try: limit, directory = limits()["busyLimit"], slot_directory()
    except (ValueError, RuntimeError) as error: return {"error": str(error)}
    return {"load": load, "cpuBusy": cpu_busy(directory), "busyLimit": limit, "memory": memory_level()}


def guarded_update(request):
    """update_toolkit's answer, or what kept it from answering: it rides on a dispatch's preparing connection,
    which nothing of it may end (vocaro-engineering#203)."""
    try: return update_toolkit(request)
    except Exception as error:
        return {"state": "failed", "path": request.get("path", TOOLKIT_CHECKOUT),
                "reason": f"{type(error).__name__}: {error}"}


def answer(request):
    """offload_host.py's answer to a request other than a plan, for `eng doctor`, `eng remote`,
    `eng remote setup`, `eng remote foreground`, `devices recover` and `eng keychain recover`. A census before a dispatch can
    also name repositories to advertise, and then says what each advertises and which Git does so."""
    if "toolchain" in request: return toolchain(request["toolchain"])
    if "keychain" in request: return keychain(request["keychain"])
    if "foreground" in request: return foreground()
    if "pressure" in request: return pressure()
    if "capacity" in request: return capacity(diagnostic=request["capacity"] == "diagnostic")
    if "admissionLog" in request: return admission_log(request["admissionLog"])
    if "screen" in request: return screen()
    if "leftovers" in request or "device" in request:
        reaped = reap(request["leftovers"], **request["reap"]) if "reap" in request else None
        # A census that fails says why: it must never stop the work it is printed before.
        try: found = leftovers(request["leftovers"]) if "leftovers" in request else device_users(request["device"])
        except Exception as error: found = {"error": f"{type(error).__name__}: {error}"}
        if reaped is not None: found["reap"] = reaped
        if request.get("advertise"):
            found.update(git=output(["git", "--version"]),
                         advertised={path: advertise(Path.home() / path) for path in request["advertise"]})
        if request.get("toolkitCheckout"): found["toolkitCheckout"] = guarded_update(request["toolkitCheckout"])
        return found
    if "toolkitCheckout" in request: return {"toolkitCheckout": guarded_update(request["toolkitCheckout"])}
    if "deleteRuntime" in request:
        deleted = request["deleteRuntime"]
        return {"problem": delete_runtime(deleted["developer"], deleted["build"], deleted.get("pinned") or ())}
    if "images" in request:
        return {"images": images(request["images"], request.get("pinned") or ()),
                **stage(Path.home() / request["staging"], request.get("clear"))}
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


def gather_changes(checkout, path):
    """Write to path, as a binary-safe patch against the commit the checkout holds, the changes a command left in
    its files that Git does not ignore: tracked edits and deletions, and new untracked files no .gitignore names,
    staged in a temporary index as remote.snapshot stages a tree. Then restore the checkout to that commit, so that
    the next run can claim it (claim passes over a checkout with changes), and return the paths changed. Ignored
    files, such as the command's build products, stay as they are. The sending Mac applies the patch to its own
    tree (`eng admit`; the owner's decision, 2026-11-07). A failure raises before the checkout
    is touched, and leaves the changes there."""
    environment = {key: value for key, value in os.environ.items() if key not in GIT_LOCATION}

    def run(*arguments, variables=None, text=False):
        done = subprocess.run(["git", *arguments], cwd=checkout, env=variables or environment, capture_output=True)
        if done.returncode:
            raise RuntimeError(f"git {' '.join(arguments)} failed: {done.stderr.decode(errors='replace').strip()}")
        return done.stdout.decode() if text else done.stdout

    index = Path(run("rev-parse", "--path-format=absolute", "--git-path", "index", text=True).strip())
    with tempfile.TemporaryDirectory(prefix="eng-changes-") as directory:
        variables = {**environment, "GIT_INDEX_FILE": str(Path(directory) / "index")}
        # A copy of the checkout's index keeps its file stats, so git add reads only the files that changed.
        if index.is_file(): shutil.copy2(index, variables["GIT_INDEX_FILE"])
        else: run("read-tree", "HEAD", variables=variables)
        run("add", "-A", variables=variables)
        patch = run("diff", "--cached", "--binary", "--no-color", "--no-ext-diff", "--no-textconv", "--no-renames",
                    "HEAD", variables=variables)
        files = run("diff", "--cached", "--name-only", "--no-renames", "-z", "HEAD", variables=variables,
                    text=True).split("\0")
    files = [name for name in files if name]
    # The patch first, under .build/engineering, which every project ignores (guides/adoption.md), so the clean
    # below leaves it.
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(patch)
    if files:
        run("reset", "--hard", "--quiet", "HEAD")
        run("clean", "-d", "--force", "--quiet")
    return files


def distance(checkout, commit):
    """How many files differ between the tree a checkout last held and commit's, or None when that is
    unknown: no checkout yet, or a commit it cannot compare. The files that differ are what its builds
    redo (vocaro-engineering#183). It compares two trees, so it reads a checkout another run holds
    without touching its index or files."""
    if not commit or not checkout.exists():
        return None
    result = subprocess.run(["git", "diff", "--name-only", "HEAD", commit], cwd=checkout, capture_output=True,
                            text=True)
    return None if result.returncode else len(result.stdout.splitlines())


def nearest(base, count, directory, commit):
    """The slots in the order to try them: those whose checkout last held the tree nearest commit's
    first, then those whose distance is unknown, each in index order. Without a commit, index order."""
    distances = {index: distance(base / str(index) / directory, commit) for index in range(count)}
    return sorted(distances, key=lambda index: (distances[index] is None, distances[index] or 0, index)), distances


def nearness(files, checkout):
    """What a slot's distance says of its builds, for the run's log."""
    if files is None:
        return "its last tree could not be compared with the commit's" if checkout.exists() else \
            "no checkout yet, so its builds are cold"
    if not files:
        return "its last tree is the commit's, so its builds are warm"
    return f"{files} {'file differs' if files == 1 else 'files differ'} from its last tree"


def holding(base, slot):
    """Who holds a checkout slot's lock, as its holder wrote into it (claim), for a line that reports a busy slot;
    nothing when it wrote nothing readable."""
    try:
        record = json.loads((base / f"{slot}.lock").read_text())
        return f", held by PID {record['pid']}, started {record['start']}"
    except (OSError, ValueError, KeyError, TypeError):
        return ""


def claim(base, count, directory, wait=True, commit=None, slot=None, timeout=CLAIM_TIMEOUT):
    """Lock a free checkout slot without changes, waiting while every one is in use, if wait, for timeout seconds.

    Of the free slots it takes the one whose checkout last held the tree nearest commit's, so the
    run's builds redo the fewest files (nearest), and says how near it was. The lock is an flock the
    run's command inherits, so it holds while that command lives. A checkout with changes is left as
    it is, for inspection, and passed over. The lock's holder writes its PID and start into it, so
    that a census of leftover processes knows a live run's processes from those a lost run left
    (leftovers).

    With slot, only that slot will do (vocaro-engineering#223): another that is free does not answer the request.
    It waits for that slot under the same lock, and changes in it are a refusal, since no other slot may stand in.
    """
    base.mkdir(parents=True, exist_ok=True)
    started, reported, named = time.monotonic(), None, set()
    record = json.dumps({"pid": os.getpid(), "start": output(["ps", "-p", str(os.getpid()), "-o", "lstart="])})
    order, distances = nearest(base, count, directory, commit)
    if slot is not None: order = [slot]
    while not _stop.is_set():
        changed = 0
        for index in order:
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
                if commit: say(f"slot {index}: {nearness(distances[index], checkout)}")
                return index, descriptor
            os.close(descriptor)
            if slot is not None:
                repository = base.parents[1] / "repositories" / f"{directory}.git"
                raise Refusal(f"checkout slot {slot} of {directory}, {checkout}, has changes a run left:\n{left}\n",
                              f"inspect and remove them, with `git --git-dir {repository} worktree remove --force "
                              f"{checkout}`")
            changed += 1
            if index not in named:
                say(f"passing over {checkout}, which a run left with changes:\n{left}")
                named.add(index)
        if changed == count:
            repository = base.parents[1] / "repositories" / f"{directory}.git"
            raise Refusal(f"every checkout under {base} has changes a run left",
                          f"inspect and remove them, each with `git --git-dir {repository} worktree remove --force PATH`")
        named = f"checkout slot {slot} of {directory}"
        busy = f"every checkout of {directory} is in use" if slot is None else f"{named} is in use{holding(base, slot)}"
        if not wait:
            raise Refusal(busy, busy=True)
        waited = int(time.monotonic() - started)
        if time.monotonic() - started >= timeout:
            raise Refusal(f"no checkout of {directory} came free in {timeout}s" if slot is None else
                          f"{named} did not come free in {timeout:g}s{holding(base, slot)}", busy=True)
        if reported is None or waited - reported >= REPORT_SECONDS:
            say(f"waiting ({waited}s): {busy}")
            reported = waited
        time.sleep(1)
    return None


def local_url(url):
    """Whether a Git remote URL names files on its own Mac, as a path or file:// does, and so names
    another checkout, or nothing, on the other Mac. Git reads a URL with no scheme as a path unless a
    colon comes before any slash, as in scp's host:path (vocaro-engineering#78)."""
    if "://" in url: return url.lower().startswith("file://")
    return ":" not in url.split("/", 1)[0]


def place(repository, checkout, commit, ref, origin=None):
    """Check commit out, detached, in a persistent worktree whose ignored inputs and builds stay.

    Only the toolkit names an origin: a heavy lane's policy refresh fetches it. Every checkout of the
    repository shares its origin, so one naming a path on the sending Mac is refused before it is
    written: here it names another checkout, or nothing, and every run's refresh would fail."""
    if origin and local_url(origin):
        raise Refusal(f"the toolkit's origin is {origin}, a path on the sending Mac, from which no checkout here "
                      "can fetch the policy",
                      "set the sending checkout's origin to https://github.com/vocaro/vocaro-engineering.git and "
                      "send the work again (guides/adoption.md, Locating the toolkit from a worktree)")
    if origin:
        # Different checkout slots can place the toolkit at once. Hold the shared lock only for its config.
        with (repository / "eng-origin.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            command = ["--git-dir", str(repository), "config"]
            if output(["git", *command, "--get", "remote.origin.url"]) != origin:
                git(*command, "remote.origin.url", origin)
    if checkout.exists():
        git("checkout", "--detach", "--quiet", commit, cwd=checkout)
    else:
        git("--git-dir", str(repository), "worktree", "prune")
        git("--git-dir", str(repository), "worktree", "add", "--detach", "--quiet", str(checkout), commit)
    if git("rev-parse", "HEAD", cwd=checkout) != commit:
        raise RuntimeError(f"{checkout} is not at {commit}")
    # The detached worktree keeps the commit reachable; the pushed ref only carried it here. The
    # checkouts' commits are named instead, for the next push (advertise).
    subprocess.run(["git", "--git-dir", str(repository), "update-ref", "-d", ref], capture_output=True)
    advertised = advertise(repository)
    if "error" in advertised: say(f"could not name what the checkouts of {repository} hold: {advertised['error']}")


def credentials(environment, path):
    """Give the run's git the second Mac's credential file and no other helper.

    SwiftPM clones private dependencies with git, and an SSH session cannot unlock the keychain
    that osxkeychain, Xcode's git default, reads. The variables reach only this run, so the
    owner's own git configuration on the second Mac is unchanged."""
    count = int(environment.get("GIT_CONFIG_COUNT", "0"))
    for offset, value in enumerate(["", f"store --file={shlex.quote(str(path))}"]):
        environment[f"GIT_CONFIG_KEY_{count + offset}"] = "credential.helper"
        environment[f"GIT_CONFIG_VALUE_{count + offset}"] = value
    environment["GIT_CONFIG_COUNT"] = str(count + 2)


# What a hook or the sending side may export for its own repository, which must not aim git at another.
GIT_LOCATION = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
                "GIT_COMMON_DIR", "GIT_PREFIX")


def update_toolkit(request, home=None, seconds=UPDATE_SECONDS, listed=listing):
    """Fast-forward this Mac's own toolkit checkout, request["path"] under the home, to its origin's main
    (vocaro-engineering#203), and return what became of it.

    Work dispatched here runs from the dispatched toolkit checkouts under ROOT, at the commit sent, and never
    from this one; but whatever runs `eng` straight from it, such as a recovery recoverable_line names, runs
    its code, which nothing else here updates (owner's decision, 2026-11-05: "it should be auto-updated by
    the eng scripts"). It is fast-forwarded, without hooks, only as this Mac's shared checkout follows a
    landing (merge_queue's follow, vocaro-engineering#104): when it is on main, has no changed or untracked
    path, holds no commit origin/main lacks, is behind, and no process of this account runs from it (its
    working directory or an argument lies in it; running_from). Otherwise it is left as it is, with the
    reason, which is its owner's to decide: state "missing", "left" (another branch, a detached HEAD,
    changes, commits origin/main lacks, a process running from it) or "failed" (a fetch, listing or merge
    that failed or timed out).

    With request["want"], a commit the sending Mac's origin/main names, a checkout that holds it already is
    "current" without a fetch, so most connections cost a few git reads. Every step shares one deadline,
    seconds, the fetch at most UPDATE_FETCH_SECONDS of it, the process listing at least
    UPDATE_LISTING_SECONDS past it, and git can never prompt. While another
    connection updates the checkout, this one leaves it to that one ("busy"). Nothing here raises for the
    checkout's sake: whatever went wrong is the answer's reason."""
    home = Path.home() if home is None else Path(home)
    checkout = home / request.get("path", TOOLKIT_CHECKOUT)
    found = {"checkout": str(checkout), "path": request.get("path", TOOLKIT_CHECKOUT)}
    deadline = time.monotonic() + seconds
    environment = {key: value for key, value in os.environ.items() if key not in GIT_LOCATION}
    environment.update(GIT_TERMINAL_PROMPT="0", GIT_SSH_COMMAND=environment.get("GIT_SSH_COMMAND") or
                       "ssh -o BatchMode=yes -o ConnectTimeout=10")
    root = home / request.get("root", "Development/build-host")
    if (root / "git-credentials").is_file(): credentials(environment, root / "git-credentials")

    def run(*arguments, limit=None):
        """Git's output, stripped, and None; or None and why it failed."""
        name = next((word for word in arguments if not word.startswith("-") and "=" not in word), "")
        left = deadline - time.monotonic()
        allowed = min(left, limit) if limit else left
        if allowed <= 0: return None, f"`git {name}` had no time left of the update's {seconds}s"
        try:
            # In a session of its own, so that a timeout stops what git started too, such as a fetch's
            # git-remote-https or ssh, which would otherwise hold its output open past the deadline.
            child = subprocess.Popen(["git", "-C", str(checkout), *arguments], stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=environment,
                                     start_new_session=True)
        except OSError as error:
            return None, f"`git {name}` could not run: {error}"
        try:
            stdout, stderr = child.communicate(timeout=allowed)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(OSError): os.killpg(child.pid, signal.SIGKILL)
            with contextlib.suppress(subprocess.TimeoutExpired): child.communicate(timeout=DRAIN_SECONDS)
            return None, f"`git {name}` did not finish in {round(allowed)}s"
        if child.returncode:
            return None, f"`git {' '.join(arguments)}` exited {child.returncode}: " + \
                ((stderr.strip().splitlines() or ["no output"])[-1])
        return stdout.strip(), None

    def ended(state, reason=None, **more):
        found.update(state=state, **({"reason": reason} if reason else {}), **more)
        return found

    if not (checkout / ".git").exists():
        return ended("missing", f"there is no git checkout at ~/{found['path']}")
    try:
        root.mkdir(parents=True, exist_ok=True)
        lock = open(root / UPDATE_LOCK, "a")
    except OSError as error:
        return ended("failed", f"its lock in {root} could not be opened: {error}")
    with lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return ended("busy", "another connection is updating it now")
        head, why = run("rev-parse", "--verify", "HEAD^{commit}")
        if head is None: return ended("failed", why)
        found["head"] = head
        branch, _ = run("symbolic-ref", "--quiet", "--short", "HEAD")
        if branch is None: return ended("left", f"its HEAD is detached at {head[:12]}, not on main")
        if branch != "main": return ended("left", f"it is on branch {branch}, not main", branch=branch)
        # Staged or not, against HEAD, and untracked files that no .gitignore names: clean, as this Mac's
        # shared checkout must be to follow a landing (merge_queue's follow, vocaro-engineering#104).
        changed, why = run("diff", "--name-only", "HEAD")
        untracked, why_untracked = run("ls-files", "--others", "--exclude-standard")
        if changed is None or untracked is None: return ended("failed", why or why_untracked)
        files = [*changed.splitlines(), *untracked.splitlines()]
        if files:
            shown = ", ".join(files[:3]) + (f" and {len(files) - 3} more" if len(files) > 3 else "")
            return ended("left", f"it has {len(files)} changed or untracked path{'s' * (len(files) != 1)}: {shown}")
        want = request.get("want")
        if want and run("merge-base", "--is-ancestor", want, "HEAD")[0] is not None:
            return ended("current", fetched=False)
        _, why = run("fetch", "--quiet", "--no-tags", "origin", "refs/heads/main:refs/remotes/origin/main",
                     limit=UPDATE_FETCH_SECONDS)
        if why: return ended("failed", f"fetching origin's main failed: {why}")
        upstream, why = run("rev-parse", "--verify", "refs/remotes/origin/main^{commit}")
        if upstream is None: return ended("failed", why)
        behind, why = run("rev-list", "--count", f"HEAD..{upstream}")
        ahead, why_ahead = run("rev-list", "--count", f"{upstream}..HEAD")
        if behind is None or ahead is None: return ended("failed", why or why_ahead)
        behind, ahead = int(behind), int(ahead)
        found.update(upstream=upstream, behind=behind, ahead=ahead, fetched=True)
        if ahead:
            return ended("left", f"it has {ahead} commit{'s' * (ahead != 1)} that origin/main lacks"
                                 + (f" and lacks {behind} of origin/main's, so the two have diverged" if behind else ""))
        if not behind: return ended("current")
        # A run from it, such as a recovery, keeps the modules it loaded and reads others as it goes, so a
        # move under it could mix two toolkits. What runs in a session's worktree inside it, or in Git's own
        # directory, does not run from its tree.
        listed_worktrees, why = run("worktree", "list", "--porcelain")
        if listed_worktrees is None: return ended("failed", why)
        apart = [line[len("worktree "):] for line in listed_worktrees.splitlines()[1:] if line.startswith("worktree ")]
        apart.append(str(checkout / ".git"))
        users, why = running_from(checkout, listing_budget(deadline), listed, apart)
        if users is None: return ended("failed", f"{why}, so what runs from it is unknown")
        if users:
            more = f"; and {len(users) - 3} more" if len(users) > 3 else ""
            return ended("left", f"a process runs from it: {'; '.join(users[:3])}{more}")
        _, why = run("-c", "core.hooksPath=/dev/null", "merge", "--ff-only", "--quiet", upstream)
        if why: return ended("failed", f"the fast-forward to {upstream[:12]} failed: {why}")
        return ended("updated", to=upstream)


def listing_budget(deadline, now=None):
    """Seconds update_toolkit's process listing has: what is left before deadline, a time.monotonic() value, and
    never under UPDATE_LISTING_SECONDS (vocaro-engineering#221)."""
    return max(UPDATE_LISTING_SECONDS, deadline - (time.monotonic() if now is None else now))


def running_from(checkout, seconds, listed=listing, apart=()):
    """What of this account, other than this process and its ancestors, may run code from checkout's own tree, as
    lines naming each process: its working directory, as lsof lists it, or an argument is below checkout, not at
    checkout itself and not below any of apart, where an argument relative to its working directory counts when
    it names a file there, or a folder by a path with a slash. This is disk.runners' rule, which this file cannot
    import, without open files (vocaro-engineering#182). ps and lsof each have at most seconds. Returned with
    None, or with why there are none: ps that did not finish or could not run, or that listed no processes."""
    output = listed(["ps", "-axww", "-o", "pid=,ppid=,uid=,lstart=,args="], timeout=seconds)
    if output is None: return None, f"ps did not finish within {seconds:.0f} s, or could not run"
    table = process_table(output)
    if not table: return None, "ps listed no processes"
    uid = os.getuid()
    directories = working_directories(listed(["lsof", "-nPw", "-a", "-u", str(uid), "-d", "cwd", "-Fpn"],
                                             timeout=seconds))
    top, others = forms(checkout), [form for path in apart for form in forms(path)]
    inside = lambda value, prefixes: any((value.rstrip("/") + "/").startswith(prefix) for prefix in prefixes)
    below = lambda value: bool(value) and (value.rstrip("/") + "/") not in top and inside(value, top) \
        and not inside(value, others)
    mine, found = set(lineage(table, os.getpid())), []
    for pid, row in sorted(table.items()):
        if pid in mine or row["uid"] != uid: continue
        where = directories.get(pid)
        uses = [(where, "working in")]
        for token in row["args"].split():
            if token.startswith("/"): uses.append((token, "naming"))
            elif token and not token.startswith("-") and where and inside(where, top):
                named = os.path.join(where, token)
                if os.path.isfile(named) or ("/" in token and os.path.exists(named)): uses.append((named, "naming"))
        hit = next(((how, value) for value, how in uses if below(value)), None)
        if hit: found.append(f"PID {pid}, {hit[0]} {hit[1]}: {row['args']}")
    return found, None


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
                      "decide which Mac's record holds physical devices and resolve that in the second Mac's record "
                      "(guides/shared-host.md, Physical devices and the second Mac)")
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


def wait_state(path):
    """What the lane's wait record at path says, to send back, or None for no record or a wait whose process is gone.
    The record's fields are lane_wait's."""
    try: record = json.loads(path.read_text())
    except (OSError, ValueError): return None
    if not isinstance(record, dict): return None
    if record.get("kind"):
        try: os.kill(record.get("pid"), 0)
        except (OSError, OverflowError, TypeError): return None
    return {"kind": record.get("kind"), "reason": record.get("reason"), "stranded": record.get("stranded", False),
            "admitted": record.get("admitted") is True}


def relay_wait(path, done, work):
    """Send the lane's wait record back as it changes, as `W` frames, and that a wait whose process is gone is over
    (lane_wait.relayed). work is whether this step is the lane's own: that the provision lane before it was admitted
    says nothing of the lane, so it does not clear a wait that goes on across offers. Nor does the step's own end: a
    run that declined leaves its wait, whose process is gone by then, and the dispatcher's next state replaces it."""
    last = None
    while True:
        finished = done.is_set()
        state = wait_state(path)
        if state != last and not (finished and state is None):
            if state is None:
                if last is not None and last.get("kind"): emit(b"W", b"null")
                last = None
            elif state.get("kind") or work:
                emit(b"W", json.dumps(state).encode())
                last = state
        if finished: break
        time.sleep(WAIT_POLL)


def send_back(directory, paths):
    """Send each of these files under directory that exists, whole: an `F` line naming it, then `D`
    lines of its bytes in base64, so that a file's every byte arrives and each line stays one frame.
    A path that ends in / sends every file under it, by its path under directory, links followed nowhere."""
    files = []
    for path in paths:
        if not path.endswith("/"):
            files.append(path)
            continue
        for parent, folders, names in os.walk(directory / path):
            folders.sort()
            files += [Path(parent, name).relative_to(directory).as_posix() for name in sorted(names)
                      if not Path(parent, name).is_symlink()]
    for path in files:
        try: stream = (directory / path).open("rb")
        except OSError: continue
        with stream:
            emit(b"F", json.dumps({"path": path}).encode())
            for chunk in iter(lambda: stream.read(FILE_CHUNK), b""):
                emit(b"D", base64.b64encode(chunk))


def step(command, directory, environment, lock, log, wait=None, work=False):
    """Run one eng command in directory; a stop sends it SIGTERM, which it carries out as a supervised stop. wait is
    the path of the lane's wait record, sent back as it changes (relay_wait)."""
    if log is not None: log.unlink(missing_ok=True)
    child = subprocess.Popen(command, cwd=directory, env=environment, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True,
                             pass_fds=(lock,))
    done = threading.Event()
    threads = [threading.Thread(target=relay, args=(child.stdout,), daemon=True)]
    if log is not None: threads.append(threading.Thread(target=follow, args=(log, done), daemon=True))
    if wait is not None: threads.append(threading.Thread(target=relay_wait, args=(wait, done, work), daemon=True))
    for thread in threads: thread.start()
    forwarded, killed = None, False
    while child.poll() is None:
        if _stop.is_set() and forwarded is None:
            say("stop requested; sending SIGTERM to the run, which stops its command and leaves admission")
            try: os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError: pass
            forwarded = time.monotonic()
        # An attended stop has its dispatcher's wait after it; one that bounds a run nobody attends has only this.
        if _overran.is_set() and forwarded is not None and not killed \
                and time.monotonic() - forwarded > OVERRUN_KILL_SECONDS:
            say(f"the run did not stop in {OVERRUN_KILL_SECONDS} s; sending SIGKILL")
            try: os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError: pass
            killed = True
        time.sleep(0.1)
    done.set()
    for thread in threads: thread.join(DRAIN_SECONDS)
    return child.returncode


def words():
    """Each line of standard input as it comes, until its end.

    It reads the descriptor, not sys.stdin: Python 3.9 aborts at exit when a daemon thread is
    blocked holding the buffered reader's lock (observed on macOS 27.2, October 2026)."""
    pending = b""
    try:
        while True:
            data = os.read(0, 65536)
            if not data: return
            *lines, pending = (pending + data).split(b"\n")
            yield from lines
    except OSError:
        return


def watch(silence=None):
    """The dispatcher's words, the end of its connection (connection_ended), and, when silence is given, that many
    seconds with nothing read: the dispatcher writes a heartbeat (remote.heartbeat), and a Mac that crashed or lost
    power closes nothing, so sshd here could hold the connection open for hours (vocaro-engineering#287). A journaled
    run its dispatcher has heard start is exempt, since a Mac that sleeps is silent too (#321).

    The silence is measured on a clock that stops while this Mac sleeps, so this Mac's own sleep is not counted."""
    heard, pending = time.monotonic(), b""
    try:
        while True:
            if silence is not None and not (_journal is not None and _attached.is_set()):
                left = heard + silence - time.monotonic()
                if left <= 0:
                    # In this order, so that the stop's own words wait on no connection.
                    _silent.set()
                    _stop.set()
                    write_frame(b"O", f"second Mac: nothing from the dispatcher for {silence}s; stopping this run, "
                                      "which leaves admission, and sending no result".encode())
                    return
                if not select.select([0], [], [], min(left, WATCH_SECONDS))[0]: continue
            data = os.read(0, 65536)
            if not data: break
            heard = time.monotonic()
            *lines, pending = (pending + data).split(b"\n")
            for word in lines:
                if word == b"stop": _stop.set()
                elif word == b"attached": _attached.set()
                elif word == b"received": _received.set()
    except OSError:
        pass
    connection_ended()


def main(plan):
    global _journal
    for number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(number, lambda *_: _stop.set())
    # sshd's hangup when the connection goes, which is no stop of a run that keeps a journal.
    signal.signal(signal.SIGHUP, lambda *_: connection_ended())
    threading.Thread(target=watch, args=(plan.get("silence"),), daemon=True).start()
    detach = plan.get("detach")
    if detach:
        journals = Path.home() / plan["root"] / JOURNALS
        try:
            prune_journals(journals)
            _journal = Journal(journals / detach["id"], detach["seconds"])
            emit(b"J", json.dumps({"id": detach["id"]}).encode())
        except OSError as error:
            say(f"keeps no journal, so a lost connection stops this run: {error}")
    # The seconds each stage here took: the wait for a free checkout, placing the commits in it, and each
    # eng command, whose own receipt tells its queue's wait from its run.
    phases = {}
    result = {"started": time.time(), "exitStatus": None, "stopped": False,
              "host": identity(plan.get("developerDir")), "phases": phases}
    timed = lambda name, began: phases.__setitem__(name, round(time.monotonic() - began, 3))
    # Until the first command starts, a failure is a refusal: nothing ran. The dispatcher may run
    # the work on its own Mac instead only when the refusal is busy.
    begun, back = False, None
    try:
        expected, found = plan.get("xcode"), result["host"]["xcode"]
        if expected and found != expected:
            version = lambda text: " ".join(text.split())
            raise Refusal(f"the Xcode at {plan['developerDir']} is {version(found or 'missing')}, not the dispatcher's "
                          f"{version(expected)}", "install this Mac's Xcode at that path on the second Mac "
                          "(guides/shared-host.md, Setting up the second Mac), or select with DEVELOPER_DIR an Xcode "
                          "both Macs have at the same path")
        root, project, toolkit = Path.home() / plan["root"], plan["project"], plan.get("toolkit")
        screen_declined(plan)
        unlocked = unlock_for_run(root)
        if unlocked: say(f"unlocked {unlocked} for this run's session, with the password keep-unlocked kept")
        base = root / "checkouts" / project["directory"]
        began = time.monotonic()
        claimed = claim(base, plan["checkouts"], project["directory"], wait=not plan.get("decline"),
                        commit=project["commit"], slot=plan.get("checkoutSlot"),
                        timeout=plan.get("checkoutWait", CLAIM_TIMEOUT))
        timed("claim", began)
        if claimed is None:
            result["stopped"] = True
            return result
        index, lock = claimed
        checkout = base / str(index) / project["directory"]
        began = time.monotonic()
        place(root / project["repository"], checkout, project["commit"], project["ref"], project.get("origin"))
        kit = checkout
        if toolkit is not None:
            kit = base / str(index) / toolkit["directory"]
            place(root / toolkit["repository"], kit, toolkit["commit"], toolkit["ref"], toolkit.get("origin"))
        timed("place", began)
        result.update(checkout=str(checkout), slot=index)
        say(f"{checkout} at {project['commit'][:12]}")
        environment = {**os.environ, "VOCARO_ENGINEERING_ROOT": str(kit), **NO_BYTECODE}
        if plan.get("deviceRecord"): defer_devices(kit, plan["deviceRecord"])
        # The reservations, held in the sending Mac's record, that this run may use (devices.LEASE).
        if plan.get("lease"): environment["VOCARO_DEVICE_LEASE"] = json.dumps(plan["lease"])
        if plan.get("developerDir"): environment["DEVELOPER_DIR"] = plan["developerDir"]
        # A receipt the work writes here says that it ran on the second Mac, sent from this Mac (remote.DISPATCHER).
        if plan.get("dispatcher"): environment["VOCARO_DISPATCHED_FROM"] = plan["dispatcher"]
        if (root / "git-credentials").is_file(): credentials(environment, root / "git-credentials")
        eng = [sys.executable, str(kit / "bin/eng"), "--project", str(checkout)]
        # The provision lane is admitted here as every lane is, and runs here, never offered on. Offered work that
        # declines when it cannot start now has it decline too, rather than wait for a slot with the checkout held.
        provision = [*eng, "run", "--here", *(["--if-free"] if plan.get("decline") else []), "provision"]
        steps = [("provision", provision, checkout / ".build/engineering/provision/run.log",
                  checkout)] if plan.get("provision") else []
        # `eng admit --verify` runs its command in the checkout's directory that matches the sender's.
        steps.append(("work", [*eng, *plan["work"]], checkout / plan["log"] if plan.get("log") else None,
                      checkout / plan["subdirectory"] if plan.get("subdirectory") else checkout))
        # The lane's wait record (lane_wait) is shared by provision and work.
        wait = checkout / plan["wait"] if plan.get("wait") else None
        if wait is not None: environment["VOCARO_LANE_WAIT"] = str(wait)
        returns = plan.get("returns")
        if returns:
            back = checkout / returns["directory"], returns["paths"]
            # The work writes them afresh, so none that an earlier run left goes back as this run's.
            for path in back[1]:
                if path.endswith("/"): shutil.rmtree(back[0] / path, ignore_errors=True)
                else: (back[0] / path).unlink(missing_ok=True)
        for stage, command, log, directory in steps:
            if _stop.is_set(): break
            # Neither an earlier dispatch nor provision can establish this step's admission.
            if wait is not None: wait.unlink(missing_ok=True)
            begun, began = True, time.monotonic()
            work_environment = environment
            if stage == "work" and "cleanupSeconds" in (plan.get("lease") or {}):
                work_environment = {**environment, "VOCARO_DEVICE_WINDOW": environment["VOCARO_DEVICE_LEASE"]}
            result.update(stage=stage, exitStatus=step(command, directory, work_environment, lock, log, wait,
                                                       stage == "work"))
            timed(stage, began)
            if result["exitStatus"] != 0: break
        result["stopped"] = _stop.is_set()
        # What `eng admit`'s command changed in the checkout goes back as a patch, however it ended, and the
        # checkout is restored for the next run (gather_changes).
        if plan.get("changes") and result.get("stage") == "work":
            try:
                result["changes"] = {"files": gather_changes(checkout, checkout / plan["changes"])}
            except (OSError, RuntimeError) as error:
                result["changes"] = {"error": str(error)}
        receipt = checkout / plan["receipt"]
        # A command that failed before it wrote a receipt leaves the previous run's in place.
        fresh = receipt.is_file() and receipt.stat().st_mtime >= result["started"]
        if fresh:
            result["receipt"] = json.loads(receipt.read_text())
        # `eng run --if-free` declined the lane, or its provision lane, before running it, and wrote no receipt.
        if plan.get("decline") and result.get("stage") in ("provision", "work") and result["exitStatus"] == plan["busy"] \
                and not fresh:
            result.update(refused="its heavy-work queue cannot take the lane now", busy=True)
    except Refusal as refusal:
        result.update(refused=str(refusal), busy=refusal.busy, fix=refusal.fix)
    except (OSError, RuntimeError, ValueError) as error:
        result["error" if begun else "refused"] = str(error)
    finally:
        result["finished"] = time.time()
        if _detached_at is not None:
            result["detached"] = {"seconds": round(time.monotonic() - _detached_at, 3), "limit": detach["seconds"],
                                  "overran": _overran.is_set()}
        # However the work ended, a stop or a failure included, what it wrote that the plan asks back.
        if back is not None and begun and not _silent.is_set():
            try: send_back(*back)
            except OSError as error: say(f"could not send back what the run wrote in {back[0]}: {error}")
        emit(b"R", json.dumps(result).encode())
        if _journal is not None and not _silent.is_set(): _journal.finish()
    return result


if __name__ == "__main__":
    request = json.loads(sys.argv[1])
    if "keepUnlocked" in request: sys.exit(keep_unlocked(request["keepUnlocked"]))
    if "reattach" in request: sys.exit(reattach(request["reattach"]))
    if {"toolchain", "images", "deleteRuntime", "receive", "leftovers", "device", "keychain", "foreground",
            "pressure", "capacity", "admissionLog", "screen", "toolkitCheckout"} & request.keys():
        print(json.dumps(answer(request)))
    else:
        main(request)
        # A write stuck on a connection nobody reads would make Python abort at exit (watch).
        if _silent.is_set(): os._exit(0)
