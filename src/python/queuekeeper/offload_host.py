"""Another Mac's side of `qk remote`, sent over SSH and run there by that Mac's own Python.

It imports nothing from the toolkit, because it runs before the toolkit is checked out there,
and Python 3.9, a Mac's system Python, runs it. It looks nothing up by a computed name either: a plan is sent
only the functions that main reads by name (remote.plan_program). It claims a persistent checkout of the project,
places the dispatched commits, and runs `qk run` or `qk admit` there under another Mac's
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
`qk doctor`; given `{"images": ...}`, `{"receive": ...}` or `{"deleteRuntime": ...}`, which the Apple plugin's
own host program now answers, it refuses them (MOVED_REQUESTS); given `{"leftovers": ROOT}`,
it lists the processes left over from runs there, which `qk remote` prints before it sends work,
and with `"advertise"` names as refs the commits the named repositories' checkouts hold, so that
the push that follows sends only what they lack, and with `"reap"` first shuts down the stale pool
simulators that census finds (reap), and with `"toolkitCheckout"`, alone too, fast-forwards this Mac's
own toolkit checkout to origin/main when it can, and says why not when it cannot (update_toolkit);
given `{"device": ...}`, what there may still use a physical device a run was given, for
`devices recover`; given `{"keychain": PATH}`, the Mac's name and whether that keychain file is
there, for `qk keychain recover`; given `{"keepUnlocked": ...}`, at the owner's terminal, it asks for
the login keychain's password and keeps it for runs to unlock that keychain with, for `qk keychain
keep-unlocked`; given `{"foreground": true}`, what the Mac lacks for foreground work started over
SSH and who holds its mac-foreground, for `qk remote foreground`; given `{"pressure": true}`, its
CPU's busy share, the limit admission sets on it and its memory pressure, for the merge queue's settle wait;
given `{"capacity": true}`, its spare capacity for heavy work, which `qk run` reads on the Macs
before it chooses one (remote.spare); given `{"admissionLog": SINCE}`, its admission log's lines from
SINCE, an epoch time, for `qk admissions usage`; given `{"waitLog": SINCE}`, the lines of its history of lanes' waits
that ended from SINCE, for `qk admissions waits` (wait_log); given `{"admissionsNow": 1}`, who holds and who waits for its
admission now, for `qk admissions now` (admissions_now); given `{"screen": true}`, whether its console screen is locked, for
`qk host screen --remote` (screen).
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
import stat
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
# Reuse the wait stream for coarse work observations without journaling a timing frame every poll.
WORK_OBSERVATION_SECONDS = 30
# Bytes of a file sent back in one D line, before base64.
FILE_CHUNK = 48 << 10
# Requests an earlier offload_host.py answered that a plugin's own host program now answers, by that plugin: the
# Apple plugin's runtime images, their delete and their receive (queuekeeper_apple/simulators/runtime_host.py).
# One sent here, by an older plugin, is refused (moved_request) rather than run as a plan.
MOVED_REQUESTS = {"images": "the Apple plugin", "deleteRuntime": "the Apple plugin", "receive": "the Apple plugin"}
# Where heavy-work admission keeps its guard and its log, as resources.py names them, which this
# file cannot import: the guard beside the legacy gate lock, and the log's two generations, older
# first, in the account's durable state unless HEAVY_SLOT_DIR names a namespace of its own.
GATE_LOCK = "/tmp/queuekeeper-gate-suite.lock"
ADMISSION_STATE = "Library/Application Support/queuekeeper"
ADMISSION_LOGS = ("admissions.1.jsonl", "admissions.jsonl")
# Its history of lanes' waits beside it, previous generation first, as wait_history names them (vocaro/queuekeeper#233).
WAIT_LOGS = ("waits.1.jsonl", "waits.jsonl")
# The host's admission limits file, beside its log, and its slot files' directory: the legacy user-temp
# namespace unless HEAVY_SLOT_DIR names one, as resources.py names them (#49).
ADMISSION_LIMITS = "admission-limits.json"
SLOTS = "queuekeeper-heavy-slots"
# The queue of exclusive and whole-host requests in the slot directory, and HEAVY_PLACE_SECONDS's default, how
# long a whole-host request that last asked then keeps the guaranteed slots, as resources.QUEUE and
# resources.PLACE_SECONDS have them (vocaro-engineering#174, #186).
QUEUE, PLACE_SECONDS = "exclusive-queue.json", 180
# Each admission limit the capacity reading needs, by its key in the limits file: its environment variable,
# whether it counts slots, and its default given the cores and the guaranteed slots, as resources.LIMIT_KEYS
# has them. The two must agree: queuekeeper chooses a Mac by what each Mac's admission would admit.
LIMIT_KEYS = {
    "slots": ("HEAVY_SLOTS", True, lambda cores, slots: 3),
    "slotsMax": ("HEAVY_SLOTS_MAX", True, lambda cores, slots: max(slots, cores // 2)),
    "busyLimit": ("HEAVY_BUSY_LIMIT", False, lambda cores, slots: 90.0),
    "settleSeconds": ("HEAVY_SETTLE_SECONDS", False, lambda cores, slots: 30.0),
}
# A Mac's own settings, beside its limits file: which kinds of work it takes and which labels it has
# (mac_settings; the owner's decision of 2026-11-17, vocaro/queuekeeper#211). A file of its own, since an older
# toolkit's admission refuses a limits file with a key it does not know.
MAC_SETTINGS = "mac-settings.json"
# The kinds of work a Mac's settings may decline, by their key under "takes", each with what a message calls it.
KINDS = {"wholeHost": "whole-host gates", "exclusive": "exclusive work", "model": "model runs",
         "foreground": "foreground work"}
# A label, as a Mac's settings give it and a lane names it: lowercase words joined by hyphens, as a chip is named.
LABEL = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
# The simulators' devices, relative to the home: a booted device's processes name its directory.
DEVICES = "Library/Developer/CoreSimulator/Devices"
DEVICE = re.compile(r"[0-9A-F]{8}-(?:[0-9A-F]{4}-){3}[0-9A-F]{12}", re.IGNORECASE)
# The device record, relative to the home, as devices.py finds it, which QUEUEKEEPER_DEVICE_RECORD
# overrides for a test's; its key for the Mac it defers physical devices to; and the marker mobile
# writers put on physical-device entries, which this standalone reader can read without a plugin.
DEVICE_RECORD = "Library/Application Support/queuekeeper/device-handoff.json"
DEFERRED = "physicalDevicesRecordedOn"
PHYSICAL_DEVICE = "physicalDevice"
# The setting that shows an app's "quit unexpectedly" alert, and where a foreground run that has it off
# keeps its value from before the run, relative to the home (crash_alerts.py).
CRASH_DIALOG = ("com.apple.CrashReporter", "DialogType")
CRASH_DIALOG_RECORD = "Library/Application Support/queuekeeper/crash-dialog.json"
# The processes whose windows are system alerts over a foreground run's windows: macOS's crash-report and
# other user notification alerts, and the password, keychain and permission prompts that stay the owner's.
ALERT_PROCESSES = ("UserNotificationCenter", "Problem Reporter", "CrashReporter", "SecurityAgent", "coreautha",
                   "universalAccessAuthWarn", "CoreServicesUIAgent")
# Legacy Apple records do not mark their physical devices.
PHYSICAL = re.compile(r"[0-9A-F]{8}-[0-9A-F]{16}|[0-9A-F]{40}")
# Where a Mac keeps its users' keychain files, relative to the home, and its login keychain's file there.
KEYCHAINS = "Library/Keychains"
LOGIN_KEYCHAIN = "login.keychain-db"
# The file in qk remote's root that holds the login keychain's password, once the owner has run
# `qk keychain keep-unlocked`, from which each run unlocks that keychain in its own session.
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
# (simulators/pool.py, Pool(alone=True)), and never on `qk simulator-pool take`'s, whose session holds the
# member by its word once that command has ended. Only such a reservation may be released before its expected
# release (release_dead; vocaro-engineering#156).
ALONE = "heldOnlyWhileHolderRuns"
# What ps lists for an early release's census: each process's environment after its command line, which
# carries the session a command runs for (CLAUDE_CODE_SESSION_ID, CODEX_THREAD_ID, QUEUEKEEPER_DEVICE_OWNER).
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
# another Mac has from its refs alone, and a detached checkout's HEAD is none of them, so without
# these a push sends the commit's whole history (vocaro-engineering#67).
CACHE = "refs/queuekeeper/checkouts/"
COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")

# Seconds a pool simulator's launchd_sim must have run before reap may shut it down.
REAP_AFTER = 2 * 60 * 60
# Seconds one reap may take, which bounds what it adds to a dispatch; seconds it must have left to start
# another stop; seconds the check before a stop may hold the device record's lock, under the 30 another
# session's change waits for it (devices.Record); and seconds it waits for that lock.
REAP_SECONDS, REAP_STOP_SECONDS, REAP_CHECK_SECONDS, REAP_LOCK_SECONDS = 60, 20, 20, 5
# The durable log of its stops, beside the admission log, whose audit reads only admissions.
REAP_LOG = "simulator-reaps.jsonl"
# Leftovers queuekeeper stops itself (vocaro-engineering#353, stop_leftovers): the record beside each checkout slot's lock,
# SLOT.LAUNCHED, of what launchd started or adopted from the slot's build products while a run held it, and how many
# of its lines a claim keeps; how often a run samples them; the log of stops, beside REAP_LOG; the seconds a stop
# waits for a process to end after SIGTERM, and that all of one cleanup's stops may take.
LAUNCHED, LAUNCHED_KEPT, LAUNCH_SAMPLE_SECONDS = "launched.jsonl", 1000, 5
STOP_LOG, STOP_WAIT_SECONDS, STOP_SECONDS = "leftover-stops.jsonl", 5, 30
# The log of how long sessions hold the members `qk simulator-pool take` hands them, beside the reap log: a
# line for each take, extend, release and reap release of one, so that TAKE_HOLD (simulators/pool.py) is sized
# from measurement (vocaro-engineering#179). A line that would take it past HOLD_LOG_LIMIT bytes first makes it
# the previous generation, as the admission log does, so the two stay within twice it.
HOLD_LOG, PREVIOUS_HOLD_LOG, HOLD_LOG_LIMIT = "simulator-holds.jsonl", "simulator-holds.1.jsonl", 4 << 20
# The log of listings (`listing`) that took SLOW_LISTING_SECONDS or more, beside the reap log, kept as HOLD_LOG is,
# and how many processes one line names at most in each of its lists. A `ps -axww ... args=` listing of every
# process took over a minute beside the Swift lanes at a gate's start on 2026-11-10, where it normally takes under
# a second, and was not reproduced by load alone; each line records what it could have waited on
# (vocaro-engineering#386).
SLOW_LISTING_SECONDS, SLOW_LISTING_KEPT = 5, 40
SLOW_LISTING_LOG, PREVIOUS_SLOW_LISTING_LOG, SLOW_LISTING_LIMIT = "slow-listings.jsonl", "slow-listings.1.jsonl", 1 << 20
# Set on the reservation `qk simulator-pool take` makes, never a run's own (ALONE), naming the pool, so that
# reap logs its release in HOLD_LOG.
TAKEN = "takenFromPool"
# A simulator's own launchd_sim, as ps prints it.
LAUNCHD_SIM = re.compile(r"(?:\S*/)?launchd_sim\s")
# What follows a pool's name in a member's: LABEL N, as simulators/pool.py names one; and in its kind's
# warmed template's, LABEL template (RUNTIME), with pool.py's own pattern, which this file cannot import
# because it runs on another Mac alone.
MEMBER = re.compile(r".+ [1-9][0-9]*")
TEMPLATE = re.compile(r"(?P<label>.+) template \((?P<runtime>[^()]+)\)")
SIMULATORS = ("xcrun", "simctl", "list", "devices", "-j")
# The toolkit's own checkout on each Mac, relative to its home, which update_toolkit keeps at origin/main
# there (vocaro-engineering#203); and the shell words that run the Apple plugin's `qk devices` from any folder
# on any Mac: that checkout's own launcher, which puts its plugins on the import path, with that checkout as the
# project, which declares the plugin. `python3 -m queuekeeper_mobile.devices` finds no such module outside a
# process qk started, since the plugins left src/python (vocaro/queuekeeper#167).
TOOLKIT_CHECKOUT = "Development/queuekeeper"
QK_DEVICES = f"python3 ~/{TOOLKIT_CHECKOUT}/bin/qk --project ~/{TOOLKIT_CHECKOUT} devices"
# Where a Mac keeps the checkouts runs are pinned to, under the home: NAME/COMMIT/NAME for each folder a project's
# plugins load from and each commit a run pinned it at, with NAME/COMMIT.ready beside one that is whole
# (pinned_checkout); the days one stays unused before it is pruned; and the variable that carries a run's pin set to
# every qk command under it, {NAME: {"commit", "root"}} as JSON (pins).
PINS, PIN_READY, PIN_DAYS, PIN_VARIABLE = "Development/queuekeeper-pins", ".ready", 7, "QUEUEKEEPER_SIBLINGS"
# The variable naming another directory for them than the home's, which the test suite sets so that no test adds
# a checkout to the Mac's own (tests/host_isolation.py).
PINS_DIRECTORY = "QUEUEKEEPER_PINS_DIR"
# Seconds one update_toolkit may take in all, the bound it adds to the connection that prepares a dispatch,
# and at most for its fetch; and its lock beside another Mac's repositories, under ROOT.
UPDATE_SECONDS, UPDATE_FETCH_SECONDS = 40, 25
# Seconds its ps and its lsof each have at least, whatever is left of that bound: under load, the git steps
# before them used nearly all of it, and a ps given the last second listed nothing (vocaro-engineering#221).
UPDATE_LISTING_SECONDS = 10
UPDATE_LOCK = "toolkit-update.lock"

# The journals of dispatches a connection's end does not stop, under qk remote's root, each in a directory named
# for its dispatch; the frames in one, and the file a later connection's stop leaves there for the run.
JOURNALS, FRAMES, STOP_FILE = "dispatches", "frames", "stop"
# Seconds an uncollected journal stays: past any run's bound, for a dispatcher that never came back.
JOURNAL_KEPT = 7 * 24 * 60 * 60
# Seconds the end of a run waits on a connection that takes no bytes, and then for the dispatcher's `received`.
# The journal holds what they did not carry, for a later connection, so neither wait need cover a slow Mac.
JOURNAL_STALL_SECONDS, RECEIVED_SECONDS = 30, 10
# Seconds a run stopped at its unattended bound has between SIGTERM and SIGKILL: queuekeeper's own stop of a foreground
# run takes up to about 190 s of LINGER and FOREGROUND_KILL_AFTER (processes.py), and nobody is left to close it.
OVERRUN_KILL_SECONDS = 300
# The longest watch goes without looking at the clock for the dispatcher's silence.
WATCH_SECONDS = 5

_output = threading.Lock()
_stop = threading.Event()
_stop_details = {}
# The dispatcher's `attached` and `received`, and that the run outlasted its unattended bound.
_attached, _received, _overran = threading.Event(), threading.Event(), threading.Event()
# Set once the dispatcher has gone silent past the plan's bound (watch): nobody is left to read this run's output.
_silent = threading.Event()
# The run's Journal, when its plan keeps one, and when its connection ended, by time.monotonic.
_journal, _detached_at = None, None


class Refusal(RuntimeError):
    """Another Mac will not start the work, and nothing of it ran. A busy Mac cannot start
    it now; any other refusal is its configuration's, and fix says what fixing that takes."""

    def __init__(self, reason, fix=None, busy=False):
        super().__init__(reason)
        self.fix, self.busy = fix, busy


# The variable naming the file into which a declining admission writes why (cli.admitted_here), and that file's
# place in the checkout. remote.DECLINED names the same variable.
DECLINED, DECLINED_NOTE = "QUEUEKEEPER_DECLINED_FILE", ".build/queuekeeper/declined.txt"
ADMISSION_OFFER = "QUEUEKEEPER_ADMISSION_OFFER"
ADMISSION_OFFER_DEADLINE = "QUEUEKEEPER_ADMISSION_OFFER_DEADLINE"
# Characters of a declining admission's reason kept: it names the request ahead and the holders of every slot.
DECLINED_LIMIT = 600


def declined_offer(path, identifier):
    """The reason explicitly recorded before admission, for this dispatch only."""
    if path is None: return None
    try: value = json.loads(path.read_text())
    except (OSError, ValueError): return None
    if not isinstance(value, dict) or value.get("offer") != identifier: return None
    reason = value.get("reason")
    return (" ".join(reason.split())[:DECLINED_LIMIT] or None) if isinstance(reason, str) else None


def declined_reason(path):
    """Why another Mac's queue declined offered work, for the dispatcher's Busy: what its admission wrote in path,
    such as the exclusive request queued ahead that holds every later one back, or only that its queue could not
    take the work, from a toolkit that writes nothing (2026-11-12: the dispatcher saw only that, and the cause, an
    exclusive request queued there, took a look at another Mac's own admission log)."""
    try:
        said = " ".join(path.read_text().split())[:DECLINED_LIMIT] if path is not None else ""
    except OSError:
        said = ""
    return f"its heavy-work queue cannot take the lane now: {said}" if said else "its heavy-work queue cannot take the lane now"


def emit(kind, data):
    """Write one framed line, unless the dispatcher has gone silent. A connection nobody reads can take a write
    and never finish it, and a result saying the run stopped would read, to a dispatcher that wakes, as the
    work's own failure; with none, that dispatcher calls the run unmeasured, as it does any lost run."""
    if not _silent.is_set(): write_frame(kind, data)


def request_stop(reason):
    """Record the first observed reason before waking the run. A disconnect often follows another stop."""
    _stop_details.setdefault("reason", reason)
    _stop.set()


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
            request_stop("could not write to the dispatch connection" if _journal is None
                         else "could not write the dispatch journal")


def connection_ended(reason="the dispatch connection ended"):
    """The connection to the dispatcher ended. Once the dispatcher has heard the run start and while the run keeps a
    journal, the run goes on, and later connections can collect it. Otherwise this is a stop: a dispatcher that
    never heard the run start takes it for one that never ran. Called from a signal handler, so it writes no frame."""
    global _detached_at
    if _received.is_set(): return
    if _journal is None or not _attached.is_set(): request_stop(reason)
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
        """A stop that a later connection left, and the bound on a run nobody is attending. qk run and qk admit
        each stop their own work at its timeout; this is for an queuekeeper that does not."""
        announced = False
        while not self.closed.wait(0.5):
            if (self.directory / STOP_FILE).exists(): request_stop("a stop was requested for the dispatch journal")
            if _detached_at is None: continue
            if not announced:
                announced = True
                say(f"the connection to the dispatching Mac ended; the run goes on, for at most {self.limit:g} s more")
            if not _overran.is_set() and time.monotonic() - _detached_at > self.limit:
                _overran.set()
                request_stop(f"the run outlasted its {self.limit:g} s unattended bound")
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
    emit(b"O", f"{mac_name()}: {text}".encode())


def output(command, environment=None, *, timeout=60):
    try:
        result = subprocess.run(command, capture_output=True, text=True, env=environment, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def xcode(developer_dir):
    """`xcodebuild -version` of the Xcode at developer_dir, or None."""
    if developer_dir and Path(developer_dir).is_dir():
        return output(["xcrun", "xcodebuild", "-version"], {**os.environ, "DEVELOPER_DIR": developer_dir})
    return None


def sandbox_check():
    """{"mac", "problem"}: why /usr/bin/sandbox-exec cannot apply a trivial profile on this Mac, or None, for `qk
    doctor` (queuekeeper.sandbox.doctor_warnings). A Mac where it cannot ends every sandboxed lane not started."""
    executable = "/usr/bin/sandbox-exec"
    if not os.path.exists(executable): return {"mac": mac_name(), "problem": f"it has no {executable}"}
    try:
        tried = subprocess.run([executable, "-p", "(version 1)(allow default)", "/usr/bin/true"], capture_output=True,
                               text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as error:
        return {"mac": mac_name(), "problem": f"{executable} could not start: {error}"}
    if tried.returncode == 0: return {"mac": mac_name(), "problem": None}
    why = (tried.stderr or tried.stdout).strip() or f"exit {tried.returncode}"
    return {"mac": mac_name(), "problem": f"{executable} applies no profile: {why}"}


def mac_name():
    return output(["scutil", "--get", "LocalHostName"]) or os.uname().nodename


# The variable that names a Mac's hardware UUID in place of the IORegistry's, for a test whose Macs are one machine.
HARDWARE_VARIABLE = "QUEUEKEEPER_HARDWARE_UUID"
HARDWARE = re.compile(r'"IOPlatformUUID"\s*=\s*"([0-9A-Fa-f-]+)"')
# Where a Mac's repositories and checkouts were until the farm had more Macs than one, relative to its home.
EARLIER_ROOT = "Development/build-host"


def hardware_id():
    """This Mac's hardware UUID, which stays when its name or address changes, or None when it cannot be read."""
    if os.environ.get(HARDWARE_VARIABLE): return os.environ[HARDWARE_VARIABLE]
    found = HARDWARE.search(output(["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"]) or "")
    return found.group(1).upper() if found else None


def adopt_root(root, earlier=EARLIER_ROOT, home=None):
    """Move the directory this Mac kept its repositories and checkouts in, earlier under its home, to root, so that
    its warm checkouts stay, and return what became of it: {"moved": True} with the worktrees repaired, {"moved":
    False} when there is nothing to move, and with "left", why earlier stays where it is. Each checkout is a worktree
    of a bare repository, and each records the other's path, so Git repairs both after the move. A build's own
    state holds the old path too, as Xcode's record of a package's binary artifacts does, and nothing repairs that:
    every BreezEPUB build on a moved Mac failed for a framework it looked for under the old path (2026-11-16). So
    the old path stays as a link to the new one, "linked" in what is returned, and a Mac moved before the link was
    made is given it when it is added again. Nothing moves while a run there holds a checkout."""
    home = Path.home() if home is None else Path(home)
    old, new = home / earlier, home / root
    if old == new or old.is_symlink(): return {"moved": False}
    if not old.is_dir():
        # Moved already, by a toolkit that left no link: its checkouts' build state still names the old path.
        if not old.exists() and (new / "checkouts").is_dir() and link(old, new): return {"moved": False, "linked": str(old)}
        return {"moved": False}
    if new.exists(): return {"moved": False, "left": f"{old} stays, since {new} exists already: merge or remove it"}
    running = [pid for pid, start in dispatchers(old / "checkouts")
               if start is None or alive({"pid": pid, "start": start})]
    if running:
        return {"moved": False, "left": f"{old} stays while a run holds a checkout in it (process "
                                        f"{', '.join(map(str, running))}): add the Mac again once it has ended"}
    try:
        new.parent.mkdir(parents=True, exist_ok=True)
        old.rename(new)
    except OSError as error:
        return {"moved": False, "left": f"{old} could not be moved to {new}: {error}"}
    unrepaired = []
    for repository in sorted((new / "repositories").glob("*.git")):
        checkouts = [str(path) for path in sorted((new / "checkouts").glob("*/*/*")) if (path / ".git").is_file()
                     and f"/{repository.name}/" in (path / ".git").read_text(errors="replace")]
        try:
            if checkouts: git("--git-dir", str(repository), "worktree", "repair", *checkouts)
        except (OSError, RuntimeError) as error:
            unrepaired.append(f"{repository.name}: {error}")
    return {"moved": True, "from": str(old), "to": str(new), **({"linked": str(old)} if link(old, new) else {}),
            **({"unrepaired": unrepaired} if unrepaired else {})}


def link(old, new):
    """Make old a link to new, relative when they share a folder, and say whether it is one now."""
    try:
        old.parent.mkdir(parents=True, exist_ok=True)
        old.symlink_to(new.name if old.parent == new.parent else new, target_is_directory=True)
    except OSError:
        return False
    return True


def join(request):
    """What `qk farm add` asks of a Mac it adds: its hardware UUID and its own name, and its earlier directory moved
    to request["root"] (adopt_root)."""
    return {"id": hardware_id(), "name": mac_name(), **adopt_root(request["root"])}


SCREEN_KEYS = ("CGSSessionScreenIsLocked", "IOConsoleLocked")
# A recorded `ioreg -n Root -d1 -a` to read in place of the IORegistry, for tests, which never lock a real screen.
SCREEN_RECORDING = "QUEUEKEEPER_SCREEN_IOREG"


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
SCROLL_BARS_RECORDING = "QUEUEKEEPER_SCROLL_BARS_RECORDING"
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
    return Path(os.environ.get("QUEUEKEEPER_DEVICE_RECORD") or Path(pwd.getpwuid(os.getuid()).pw_dir) / DEVICE_RECORD)


def physical_device_entry(entry):
    """Whether the entry records a physical device, including older records of Apple hardware."""
    device = entry.get("device")
    return isinstance(device, str) and bool(device) and (
        entry.get(PHYSICAL_DEVICE) is True or PHYSICAL.fullmatch(device.upper()) is not None)


def device_record():
    """The Mac its device record defers physical devices to, and the entries that hold a physical
    device, which `qk doctor` compares. It reads without the record's lock: every change replaces
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
                  and physical_device_entry(entry))
    return {"path": str(path), "deferredTo": record.get(DEFERRED), "physical": held}


def keychain(path):
    """The Mac's name, its home, the keychain file path names and whether it is there, and the keychain
    files in ~/Library/Keychains, for `qk keychain recover`. path is absolute, starts with ~, or is a
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
    """`qk keychain keep-unlocked` on another Mac, in the owner's Terminal there or through the
    `ssh -t` the sending Mac runs: ask for this Mac's login password, and once it has unlocked the login
    keychain, keep it in request["root"], under the home, readable only by this account, for each qk
    remote run to unlock that keychain in its own session (the Mac desktop plugin's keychain.unlock_for_run). Then set the keychain to
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
        refusal = (f"{root} is not on {here}: set up the farm's other Macs first, and run qk remote there once "
                   "(see the user guide's section on setting up another Mac)")
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
    out(f"Kept it in {kept}, readable only by {pwd.getpwuid(os.getuid()).pw_name}: each qk remote run here now "
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


def system_events_accessibility(path="/Library/Application Support/com.apple.TCC/TCC.db"):
    """True when System Events may post events, False when the grant is missing, None when it cannot be read.
    Read only its Accessibility entry. In particular, do not ask System Events to do anything."""
    found = output(["/usr/bin/sqlite3", "-batch", "-init", "/dev/null", "-readonly", str(path),
                    "SELECT DISTINCT auth_value FROM access WHERE service = 'kTCCServiceAccessibility' "
                    "AND client = 'com.apple.systemevents'"])
    if found == "2":
        return True
    if found in ("", "0"):
        return False
    return None


def screen_capture_allowed():
    """Whether this process, and with it the command that started it, may capture the screen, asked without a
    prompt (CGPreflightScreenCaptureAccess)."""
    try:
        graphics = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        graphics.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
        return bool(graphics.CGPreflightScreenCaptureAccess())
    except (OSError, AttributeError):
        return False


def foreground_problems(system_events=...):
    """What this Mac lacks for foreground work, a Mac VoiceOver session among it, run by a command started
    over SSH, as [problem, the owner's fix] pairs; none when it is ready. Each is a setting of the owner's
    Mac, which the owner changes, never a run.
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
    if system_events is ...:
        system_events = system_events_accessibility()
    if system_events is False:
        found.append(["System Events lacks its own Accessibility grant to post keys",
                      "in System Settings, Privacy & Security, Device Control and Data Access (Accessibility), "
                      "turn on System Events; if it is not listed, add /System/Library/CoreServices/System Events.app"])
    elif system_events is None:
        found.append(["System Events' Accessibility grant could not be read from the system TCC database",
                      "check that the system TCC database is readable by Remote Login's commands on this Mac"])
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
CAPTURE_RUNNERS = "Library/Application Support/queuekeeper/foreground-runners"
# The programs a command started over SSH runs under: Remote Login's commands' own captures prompt for them.
CAPTURE_SSH_HOSTS = ("/usr/libexec/sshd-keygen-wrapper", "/usr/libexec/sshd-session", "com.apple.sshd-session")


def capture_approval_name(key):
    """An approval's key as a path where it is a file URL, else as recorded."""
    if key.startswith("file://"):
        return unquote(urlparse(key).path).rstrip("/")
    return key


def screen_capture_approvals(home=None, path=None):
    """macOS's direct screen-capture approvals that queuekeeper's runs depend on (capture_approvals.py): the stable
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
    crash_alerts.py writes it, which QUEUEKEEPER_CRASH_DIALOG_RECORD overrides for a test's."""
    return Path(os.environ.get("QUEUEKEEPER_CRASH_DIALOG_RECORD")
                or Path(pwd.getpwuid(os.getuid()).pw_dir) / CRASH_DIALOG_RECORD)


# Both the alert listing and the crash-report press read a window's text in the same order.
ALERT_TEXT_SCRIPT = """on joined(found)
    set out to ""
    repeat with item_ in found
        try
            set value_ to item_ as text
            if value_ is not "" and value_ is not "missing value" then
                set out to out & value_ & (ASCII character 31)
            end if
        end try
    end repeat
    return out
end joined

on alertText(window_)
    set texts to ""
    tell application "System Events"
        try
            set texts to my joined(value of static texts of window_)
        end try
        try
            set texts to texts & my joined({name of window_})
        end try
    end tell
    return texts
end alertText
"""

# Lists each window of the processes that raise system alerts as one line: the process, the window's
# x, y, width and height, its texts and its buttons, the last two separated by the unit separator.
ALERTS_SCRIPT = ALERT_TEXT_SCRIPT + """\
set out to ""
tell application "System Events"
    repeat with name_ in {%s}
        if exists process (name_ as text) then
            tell process (name_ as text)
                repeat with window_ in windows
                    set place to position of window_
                    set extent to size of window_
                    set texts to my alertText(window_)
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


# On-screen windows front to back, by owner, layer and bounds alone, which needs no permission; read as
# window_placement.windows (#14).
ORDER_SCRIPT = """ObjC.import('CoreGraphics');
var list = ObjC.deepUnwrap(ObjC.castRefToObject($.CGWindowListCopyWindowInfo(
    $.kCGWindowListOptionOnScreenOnly | $.kCGWindowListExcludeDesktopElements, $.kCGNullWindowID))) || [];
JSON.stringify(list.filter(function (w) { return !(w.kCGWindowAlpha === 0); }).map(function (w) {
  var b = w.kCGWindowBounds || {};
  return {app: w.kCGWindowOwnerName, layer: w.kCGWindowLayer, frame: [b.X, b.Y, b.Width, b.Height]};
}));"""


ORDER = ["osascript", "-l", "JavaScript", "-e", ORDER_SCRIPT]


def crash_dialogs(*, read_alerts=True):
    """DialogType, the record a run that has it off keeps, and the system alerts on the screen, for
    `qk remote foreground`."""
    path = crash_dialog_record()
    try: kept = json.loads(path.read_text())
    except FileNotFoundError: kept = None
    except (OSError, ValueError) as error: kept = {"error": f"{path} cannot be read: {error}"}
    alerts = system_alerts() if read_alerts else None
    return {"value": crash_dialog(), "record": kept, "alerts": alerts, "windows": alerts and output(ORDER)}


def reservation_condition(entry, run_state):
    """released, held, active, orphan, uncertain or unknown, as admission and foreground readiness read it.
    run_state(pid, start) reads the recorded process on the Mac that owns the reservation."""
    if entry is None:
        return "released"
    if entry.get("uncertain") or entry.get("state") == "restoration-unverified":
        return "uncertain"
    if entry.get("activeRun"):
        pid, start = entry.get("hostProcessIdentifier"), entry.get("hostProcessStart")
        if not isinstance(pid, int) or pid <= 0 or start is None:
            return "orphan"
        return run_state(pid, start)
    return "released" if entry.get("state") == "released" else "held"


def foreground_run_state(pid, start):
    state = process_state({"pid": pid, "start": start}, census([pid]))
    return {"running": "active", "ended": "orphan", "unknown": "unknown"}[state]


def foreground():
    """The Mac's name, foreground readiness and mac-foreground entry, for `qk remote foreground`.
    VoiceOver is read under the lease only."""
    try:
        entry = device_entries().get("mac-foreground")
    except ReapProblem as error:
        entry, condition = {"error": str(error)}, "unknown"
    else:
        condition = reservation_condition(entry, foreground_run_state)
    # VoiceOver may belong to the current holder. Even its baseline is read only after a
    # run acquires the lease; on is a valid baseline, preserved by the foreground state guard.
    system_events = system_events_accessibility()
    return {"mac": mac_name(), "problems": foreground_problems(system_events), "voiceoverManagedUnderLease": True,
            "entry": entry, "condition": condition, "crashDialogs": crash_dialogs(read_alerts=system_events is True),
            "captureApprovals": screen_capture_approvals()}


def python_crash_reports(directory=None, now=None):
    """Read the last day's Python reports without changing files or any run's verdict.
    An ips file has a metadata JSON line followed by the report JSON (macOS crash reporter)."""
    directory = Path(directory) if directory is not None else Path.home() / "Library/Logs/DiagnosticReports"
    since = (time.time() if now is None else now) - 86400
    result = {"since": datetime.fromtimestamp(since, timezone.utc).isoformat(), "reports": [], "notes": []}
    try:
        paths = sorted(directory.glob("Python-*.ips"))
    except OSError as error:
        result["notes"].append(f"cannot list {directory}: {error}")
        return result
    for path in paths:
        try:
            if path.stat().st_mtime < since: continue
            text = path.read_text()
            decoder = json.JSONDecoder()
            first, end = decoder.raw_decode(text.lstrip())
            rest = text.lstrip()[end:].strip()
            report = decoder.raw_decode(rest)[0] if rest else first
            if not isinstance(first, dict) or not isinstance(report, dict):
                raise ValueError("report must be an object")
            threads = report.get("threads", [])
            if not isinstance(threads, list) or any(not isinstance(thread, dict) for thread in threads):
                raise ValueError("threads must be a list of objects")
            fault = report.get("faultingThread")
            crashed = next((thread for thread in threads if thread.get("triggered")), None)
            if crashed is None and isinstance(fault, int) and 0 <= fault < len(threads): crashed = threads[fault]
            frames = (crashed or {}).get("frames", [])
            if not isinstance(frames, list) or any(not isinstance(frame, dict) for frame in frames):
                raise ValueError("frames must be a list of objects")
            frames = frames[:5]
            if not isinstance(report.get("exception", {}), dict):
                raise ValueError("exception must be an object")
            result["reports"].append({"path": str(path), "time": report.get("captureTime", first.get("timestamp")),
                "signal": report.get("exception", {}).get("signal"), "parentProcess": report.get("parentProc"),
                "frames": frames})
        except (OSError, ValueError, TypeError, AttributeError) as error:
            result["notes"].append(f"cannot read {path}: {error}")
    return result


# How many of a Mac's processes host_health names, largest by resident memory first.
HEALTH_LARGEST = 5
# What host_health lists every process with: no process's arguments, which a slow `ps ... args=` waits on.
HEALTH_LISTING = ("ps", "-A", "-o", "pid=,rss=,etime=,comm=")
# A JSON file to read in place of this Mac's memory, for tests: a reading as host_health returns it. A suite's own load
# brings memory pressure to warning and fills swap, so a `qk doctor` a test runs found this Mac short of memory and
# exited 1 (vocaro/queuekeeper#215).
HEALTH_RECORDING = "QUEUEKEEPER_HOST_HEALTH_RECORDING"
# vm.swapusage's sizes, as `total = 32768.00M  used = 31744.00M  free = 1024.00M  (encrypted)`.
SWAP_SIZE = re.compile(r"(total|used)\s*=\s*([\d.]+)([KMGT]?)")


def swap_usage(text):
    """(used, total) bytes in a vm.swapusage reading, or None for one that names neither."""
    sizes = {name: float(number) * 1024 ** "BKMGT".index(unit or "B")
             for name, number, unit in SWAP_SIZE.findall(text or "")}
    return (int(sizes["used"]), int(sizes["total"])) if {"used", "total"} <= sizes.keys() else None


def wired_bytes(text):
    """System wired bytes from one vm_stat snapshot, using the page size reported in that snapshot."""
    if not text: return None
    page_size = re.search(r"^Mach Virtual Memory Statistics: \(page size of ([0-9]{1,20}) bytes\)$", text, re.MULTILINE)
    pages = re.search(r"^Pages wired down:[ \t]*([0-9]{1,20})\.$", text, re.MULTILINE)
    if page_size and pages and (size := int(page_size[1])) > 0:
        return int(pages[1]) * size
    return None


def doctor_wired_bytes(health):
    """An optional local-only observation, kept out of the routine host-health and placement surveys."""
    if os.environ.get(HEALTH_RECORDING): return (health or {}).get("wiredBytes")
    return wired_bytes(output(["/usr/bin/vm_stat"], {**os.environ, "LC_ALL": "C"}, timeout=2))


def host_health():
    """What `qk doctor` and the Farm tab judge a Mac's memory by (host_health.warnings): its physical memory
    (memoryBytes), its swap used and total and its memory pressure level (memoryLevel, as memory_level reads it),
    None when unread, and its HEALTH_LARGEST largest processes by resident
    memory, each with its PID, executable name, resident bytes and seconds since it started. It lists processes
    without their arguments. On 2026-11-15 another Mac's fseventsd held 34 of its 64 GB after 15 days and swap
    was full, so a gate timed out with none of its tests failed; nothing named the process. With HEALTH_RECORDING
    set, the reading it names, or None when it cannot be read."""
    recording = os.environ.get(HEALTH_RECORDING)
    if recording:
        try: found = json.loads(Path(recording).read_text())
        except (OSError, ValueError): return None
        return found if isinstance(found, dict) else None
    memory = output(["sysctl", "-n", "hw.memsize"])
    swap = swap_usage(output(["sysctl", "-n", "vm.swapusage"]))
    reading = {"memoryBytes": int(memory) if memory and memory.isdigit() else None,
               "swapUsedBytes": swap and swap[0], "swapTotalBytes": swap and swap[1],
               "memoryLevel": memory_level(), "largest": None}
    listed = output(list(HEALTH_LISTING))
    if listed is not None:
        rows = [row for row in (line.split(None, 3) for line in listed.splitlines())
                if len(row) == 4 and row[0].isdigit() and row[1].isdigit()]
        rows.sort(key=lambda row: -int(row[1]))
        reading["largest"] = [{"pid": int(pid), "name": Path(command).name, "residentBytes": int(rss) * 1024,
                               "seconds": None if elapsed(etime) == float("inf") else elapsed(etime)}
                              for pid, rss, etime, command in rows[:HEALTH_LARGEST]]
    return reading


def apple_toolchain_inventory(developer_dir, selected_xcode):
    """The default Xcode and the runtime inventory for the selected Xcode."""
    default = output(["xcode-select", "-p"])
    runtimes = None
    if selected_xcode:
        listed = output(["xcrun", "simctl", "list", "runtimes", "-j"], {**os.environ, "DEVELOPER_DIR": developer_dir})
        try:
            runtimes = sorted(f"{runtime['name']} ({runtime['buildversion']})"
                              for runtime in json.loads(listed)["runtimes"] if runtime.get("isAvailable"))
        except (TypeError, ValueError, KeyError):
            pass
    return {"defaultDeveloperDir": default, "defaultXcode": xcode(default), "runtimes": runtimes}


def toolchain(developer_dir):
    """The Mac's identity, Apple toolchain inventory and local diagnostics that `qk doctor` compares."""
    found = identity(developer_dir)
    return {**found, **apple_toolchain_inventory(developer_dir, found["xcode"]),
            "deviceRecord": device_record(), "pythonCrashes": python_crash_reports(), "hostHealth": host_health()}


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


def listing(command, timeout=60, clock=time.monotonic):
    """A command's standard output whatever its status, or None when it cannot run: lsof exits 1
    when it finds nothing, or when a process it looked at has ended. One that takes SLOW_LISTING_SECONDS or
    more, or outlasts its timeout after that long, is logged in SLOW_LISTING_LOG (log_slow_listing)."""
    began, outcome, found = clock(), "ended", None
    try:
        found = subprocess.run(command, capture_output=True, text=True, timeout=timeout).stdout
    except subprocess.TimeoutExpired:
        outcome = "timed out"
    except (OSError, subprocess.SubprocessError):
        return None
    took = clock() - began
    if took >= SLOW_LISTING_SECONDS: log_slow_listing(command, took, outcome, timeout)
    return found


def elapsed(text):
    """Seconds in a ps etime, [[DD-]HH:]MM:SS, or infinity for one it cannot read."""
    days, _, rest = text.rpartition("-")
    try:
        seconds = 0
        for part in rest.split(":"): seconds = seconds * 60 + int(part)
        return seconds + int(days or 0) * 86400
    except ValueError:
        return float("inf")


def log_slow_listing(command, took, outcome, timeout, directory=None):
    """Append a line on one slow listing to SLOW_LISTING_LOG; True once written. Beside the command and how long
    it took, it records what a slow `ps ... args=` could have waited on (vocaro-engineering#386): the load, the
    memory pressure and swap, and from a listing that reads no process's arguments, how many processes there are,
    those in uninterruptible wait (U), and the youngest by command name. It never raises: the listing's caller
    goes on whatever happens here."""
    try:
        line = {"time": datetime.now(timezone.utc).isoformat(timespec="seconds"), "command": list(command),
                "seconds": round(took, 2), "outcome": outcome, "timeout": timeout, "pid": os.getpid(),
                "caller": " ".join(sys.argv[:3])[:200]}
        with contextlib.suppress(OSError): line["load"] = [round(value, 1) for value in os.getloadavg()]
        def quick(command):
            try: return subprocess.run(command, capture_output=True, text=True, timeout=10).stdout.strip() or None
            except (OSError, subprocess.SubprocessError): return None
        line["pressure"] = quick(["sysctl", "-n", "kern.memorystatus_vm_pressure_level"])
        line["swap"] = quick(["sysctl", "-n", "vm.swapusage"])
        states = quick(["ps", "-A", "-o", "pid=,stat=,etime=,comm="])
        if states is not None:
            rows = [row for row in (text.split(None, 3) for text in states.splitlines())
                    if len(row) == 4 and row[0].isdigit()]
            line["processes"] = len(rows)
            line["uninterruptible"] = [" ".join(row) for row in rows if "U" in row[1]][:SLOW_LISTING_KEPT]
            names = {}
            for row in rows:
                if elapsed(row[2]) <= took + 5: names[Path(row[3]).name] = names.get(Path(row[3]).name, 0) + 1
            line["startedDuring"] = dict(sorted(names.items(), key=lambda item: -item[1])[:SLOW_LISTING_KEPT])
        path = (directory or admission_directory()) / SLOW_LISTING_LOG
        data = (json.dumps(line) + "\n").encode()
        path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            size = path.stat().st_size
            if size and size + len(data) > SLOW_LISTING_LIMIT:
                path.replace(path.with_name(PREVIOUS_SLOW_LISTING_LOG))
        descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NONBLOCK, 0o644)
        try: os.write(descriptor, data)
        finally: os.close(descriptor)
        return True
    except Exception:
        return False


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
        state = json.loads(Path(os.environ.get("QUEUEKEEPER_GATE_LOCK_FILE", GATE_LOCK) + ".warm-push-guard").read_text())
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
    if not base or not os.path.isabs(base):
        raise RuntimeError(f"Cannot establish the host resource directory: getconf DARWIN_USER_TEMP_DIR answered "
                           f"{str(base)[:80]!r}, not an absolute path")
    return Path(base) / SLOTS


def limits(sources=None):
    """The admission limits the capacity reading needs, by key: HEAVY_SLOTS, HEAVY_SLOTS_MAX, HEAVY_BUSY_LIMIT and
    HEAVY_SETTLE_SECONDS, each from its environment variable, else the host's limits file, admission-limits.json
    beside the admission log, as a number or {"perCore": F}, else its default, as resources.limits reads them,
    which this file cannot import (#49). A file or variable admission could not read raises ValueError naming it, as admission does:
    its owner fixes it rather than queuekeeper guessing. sources, a dict, is given where each comes from, by key:
    its variable's name, the file's path or "default", as resources.limits names them, so that a line quoting another
    Mac's limit can say where it is set (limit_sources, vocaro/queuekeeper#29)."""
    sources = {} if sources is None else sources
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
            sources[key] = variable
        elif key in configured: found[key], sources[key] = configured[key], str(path)
        else: found[key], sources[key] = default(cores, slots), "default"
        if key == "slots": slots = found[key]
    if found["slots"] < 1 or found["slotsMax"] < found["slots"] or found["settleSeconds"] < 0:
        raise ValueError(f"Invalid resource limits: HEAVY_SLOTS={found['slots']} must be at least 1, "
                         f"HEAVY_SLOTS_MAX={found['slotsMax']} at least HEAVY_SLOTS, and HEAVY_SETTLE_SECONDS at least 0")
    return found


def mac_settings():
    """This Mac's own settings for the work it takes, from mac-settings.json beside its limits file:

        {"takes": {"wholeHost": false, "foreground": true}, "labels": ["unity-license"]}

    A Mac's settings say which kinds of work it takes (KINDS) and which labels it has, and a lane says which labels
    it needs; no lane is named in a Mac's settings (the owner's decision, 2026-11-17, vocaro/queuekeeper#211).
    Returns {"takes": {each kind: True or False}, "set": [the kinds the file says], "labels": [its labels, sorted],
    "file": the file's path, "ignored": [keys of it this toolkit does not know]}. A kind the file leaves out keeps
    its default: a Mac takes whole-host gates, exclusive work and model runs, and no foreground work, which takes
    its screen: that it takes only when its settings say so, on whichever Mac that is (the foreground mark alone;
    `qk farm foreground NAME` writes it, mark_foreground). Until 2026-11-17 a Mac was taken to be one that takes
    foreground work when its device record deferred physical devices to another Mac's, and the Mac that sent work
    took the farm's first Mac when it marked none. No file is every default.
    A key this toolkit does not know is passed over and named in "ignored", so that a later toolkit's key never
    stops this one's work, as an unknown limit does (#49). A file that cannot be read, is no JSON object, or gives
    a known key a value of the wrong kind raises ValueError naming it: its owner fixes it rather than queuekeeper
    guessing what the Mac takes."""
    path = admission_directory() / MAC_SETTINGS
    wrong = lambda what: ValueError(f"Invalid Mac settings: {path} {what}")
    try: data = json.loads(path.read_text())
    except FileNotFoundError: data = {}
    except OSError as error: raise wrong(f"cannot be read: {error}")
    except ValueError as error: raise wrong(f"is not JSON: {error}")
    if not isinstance(data, dict): raise wrong("holds no JSON object")
    takes, labels = data.get("takes", {}), data.get("labels", [])
    if not isinstance(takes, dict): raise wrong("gives takes as something other than an object of kinds")
    for kind, value in takes.items():
        if kind in KINDS and not isinstance(value, bool):
            raise wrong(f"sets takes.{kind} to {json.dumps(value)}, not true or false")
    if not isinstance(labels, list) or any(not isinstance(label, str) or not LABEL.fullmatch(label) for label in labels):
        raise wrong("gives labels as something other than a list of labels, each lowercase words joined by hyphens, "
                    "such as [\"unity-license\"]")
    ignored = sorted([key for key in data if key not in ("takes", "labels")]
                     + [f"takes.{kind}" for kind in takes if kind not in KINDS])
    found = {kind: takes.get(kind, kind != "foreground") for kind in KINDS}
    return {"takes": found, "set": sorted(kind for kind in takes if kind in KINDS), "labels": sorted(set(labels)),
            "file": str(path), "ignored": ignored}


def mark_foreground(on):
    """Have this Mac's own settings say that it takes foreground work, or with on false say nothing of it, which
    is none, and return mac_settings() as it then reads: `qk farm foreground NAME [--off]`, the one command that
    writes the file. Whatever else the file holds stays, keys this toolkit does not know among them, and the file
    is replaced whole. A file that cannot be read as settings is left as it is, and mac_settings' ValueError says
    what is wrong with it."""
    mac_settings()
    path = admission_directory() / MAC_SETTINGS
    try: data = json.loads(path.read_text())
    except FileNotFoundError: data = {}
    takes = dict(data.get("takes", {}))
    takes.pop("foreground", None)
    if on: takes["foreground"] = True
    data = {key: value for key, value in data.items() if key != "takes"}
    if takes: data = {"takes": takes, **data}
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(f".{MAC_SETTINGS}.{os.getpid()}")
    try:
        staged.write_text(json.dumps(data, indent=2) + "\n")
        os.replace(staged, path)
    finally:
        with contextlib.suppress(OSError): staged.unlink()
    return mac_settings()


def limit_sources(sources):
    """What a reading adds so that a line quoting one of this Mac's limits can say where it is set: where each
    limit comes from (limitsFrom, as limits gives sources) and this Mac's limits file (limitsFile), which a
    default names as where to set it (resources.limit_words, vocaro/queuekeeper#29)."""
    return {"limitsFrom": dict(sources), "limitsFile": str(admission_directory() / ADMISSION_LIMITS)}


def busy_slots(directory, count):
    """The slots among the first count whose lock a holder holds: each slot file in the directory is held with
    flock by the admission that took it (resources.take_slot). A shared lock tried without waiting tells, and
    is given up at once; a slot file that is missing was never taken, and one that cannot be opened counts as
    busy, so that a Mac queuekeeper cannot read never reads as idle."""
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
        state = json.loads(Path(os.environ.get("QUEUEKEEPER_GATE_LOCK_FILE", GATE_LOCK) + ".warm-push-guard").read_text())
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
    """The slots admission counts the uncertain guard entries among owners whose owners still run as holding, as
    resources.Admission._count_stranded counts them: every slot for exclusive work, else the slots whose files
    name the entry, else the lowest slot not already counted. An entry whose owner has ended, or cannot be confirmed
    running, holds nothing: admission frees it at its next attempt (vocaro/queuekeeper#23). Admission first recovers
    each uncertain entry and counts only those recovery leaves; this reading runs no census, so it counts every one,
    and an entry the next admission removes may be counted once more here."""
    reserved, unnamed = set(), 0
    for owner in owners:
        if not owner.get("uncertain") or not alive(owner): continue
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


def queued_ahead(directory, slots, maximum, owners, own=None, before=None):
    """The request queued ahead that holds this Mac's later heavy work back now, as ("wholeHost", WAITER), for
    which admission keeps the guaranteed slots, or ("exclusive", WAITER), behind which every later request waits;
    or None. It applies the rule resources.Admission._attempt applies (vocaro-engineering#174, #186, #191): the
    first queued request that holds later work back, an exclusive one whose process runs or a whole-host one
    whose process runs and last asked within HEAVY_PLACE_SECONDS, unless a dead or uncertain guard entry is
    counted as a slot it would wait for, any slot for an exclusive request and a guaranteed one for a whole-host
    request, when it holds nothing back (#89). A request whose process ended and one that has not asked lately
    hold nothing back, and the reading looks past them, as admission does. own is the identity of the asking
    work's own place in this queue (resources.Place): what is queued behind it holds nothing back from it.
    before is this request's arrival on this Mac's clock, carried unchanged after the first dispatch;
    newer dated arrivals cannot hold it back. Ordinary waiters share and are not capacity blockers."""
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
        if before is not None and waiter.get("queuedAt", float("-inf")) > before: continue
        if not alive(waiter) or waiter.get("ordinary"): continue
        if "seen" in waiter:
            seen = waiter["seen"]
            if not isinstance(seen, (int, float)) or isinstance(seen, bool) or time.time() - seen >= hold: continue
        if not waiter.get("wholeHost"):
            # Exclusive work queued ahead holds every request back, unless a stranded entry is counted as any slot.
            return None if stranded_slots(directory, maximum, owners) else ("exclusive", waiter)
        seen = waiter.get("seen")
        if not (isinstance(seen, (int, float)) and not isinstance(seen, bool) and time.time() - seen < hold): continue
        if any(slot < slots for slot in stranded_slots(directory, maximum, owners)): return None
        return "wholeHost", waiter
    return None


def capacity(own=None, *, diagnostic=False, before=None):
    """This Mac's spare capacity for heavy work, which `qk run` reads on the Macs before it chooses one
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
    its console screen (screen), which a lane that needs it unlocked reads; and the kinds of work it takes, its
    labels, where its settings are and the keys of them it passes over (takes, takesSet, labels, settingsFile,
    settingsIgnored; mac_settings), by which placement
    passes over a Mac that does not take the work (remote.unfit). A reading it cannot take says why, as
    error, as pressure does. A diagnostic keeps no CPU sample and identifies blockers by admission and process,
    since their command labels can be long and need not identify the work."""
    sources = {}
    try: found = limits(sources)
    except ValueError as error: return {"error": str(error)}
    try: directory = slot_directory()
    except RuntimeError as error: return {"error": str(error)}
    try: settings = mac_settings()
    except ValueError as error: return {"error": str(error)}
    try: load = round(os.getloadavg()[0], 2)
    except OSError: load = None
    owners = guard_entries()
    running = [owner for owner in owners if alive(owner)]
    kind, ahead = queued_ahead(directory, found["slots"], found["slotsMax"], owners, own, before) or (None, None)
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
            "memory": memory_level(), "screen": screen(), **limit_sources(sources),
            "takes": settings["takes"], "takesSet": settings["set"], "labels": settings["labels"],
            "settingsFile": settings["file"], "settingsIgnored": settings["ignored"],
            **({"blockers": blockers} if diagnostic else {})}


# The variable by which work sent here names the place it belongs to in this Mac's queue, as resources.PLACE has it:
# its own admissions here are not held back by that place, which its sender holds while it waits (place_holder).
PLACE_VARIABLE = "QUEUEKEEPER_ADMISSION_PLACE"


def admission_log(since):
    """The lines of this Mac's admission log, both generations, oldest first, written at or after since, an epoch
    time, for `qk admissions usage` on the sending Mac (resources.read_entry reads them there). A line whose time
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


def wait_log(since):
    """The lines of this Mac's history of lanes' waits, both generations, oldest first, for waits that ended at or
    after since, an epoch time, for `qk admissions waits` on the sending Mac (wait_history.read_entry reads them
    there). A line whose time cannot be read is passed on for that Mac to count as unreadable."""
    directory, found = admission_directory(), []
    for name in WAIT_LOGS:
        try: lines = (directory / name).read_text(errors="replace").splitlines()
        except OSError: continue
        for line in lines:
            try: moment = datetime.fromisoformat(json.loads(line)["time"]).timestamp()
            except (ValueError, KeyError, TypeError): moment = None
            if moment is None or moment >= since: found.append(line)
    return {"lines": found}


# `qk admissions now` (vocaro-engineering#342): the folder beside the slot files where each waiting request keeps its
# observation record, as resources.WAITS has it; the schema both write and read; how many records and how many bytes a
# snapshot reads and sends at most; and how old a live waiter's sample may be before it is shown as stale.
WAITS, NOW_SCHEMA, NOW_RECORDS, NOW_BYTES, NOW_STALE = "admission-waits", 1, 512, 512 << 10, 30
# How far back `qk admissions now` shows the holders admission reclaimed, how many at most, and how much of the end of
# the admission log it reads for them (vocaro/queuekeeper#23).
NOW_RECLAIMED_SECONDS, NOW_RECLAIMED, NOW_LOG_TAIL = 3600, 16, 256 << 10


def census(pids):
    """{PID: process start with its spacing collapsed} for those of pids that run, or None when nothing can answer:
    an absent PID is proven absent only by an answer, never by a failure (admissions_now). One bounded `ps` reads them
    all; when it fails or times out, each PID is asked of the kernel instead (kernel_start), so that one bad PID or a
    ps that fails once does not hide every holder (vocaro/queuekeeper#23). A PID the kernel says runs but whose start
    it will not give maps to None, its process unknown, never ended: a live holder slow to confirm keeps its slot."""
    pids = sorted({pid for pid in pids if type(pid) is int and pid > 0})
    if not pids: return {}
    try:
        found = subprocess.run(["ps", "-p", ",".join(map(str, pids)), "-o", "pid=,lstart="], capture_output=True,
                               text=True, timeout=5, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError): found = None
    # ps exits 1 with nothing said when a PID it was asked about does not run; anything it says is a failure.
    if found is None or found.returncode not in (0, 1) or found.stderr.strip():
        listed = {}
        for pid in pids:
            try: started = kernel_start(pid)
            except OSError: started = None
            except LookupError: continue  # the kernel says no such process runs
            listed[pid] = started
        return listed if any(start is not None for start in listed.values()) or len(listed) < len(pids) else None
    listed = {}
    for line in found.stdout.splitlines():
        parts = line.split()
        if len(parts) > 1 and parts[0].isdigit(): listed[int(parts[0])] = " ".join(parts[1:])
    return listed


def cpu_use(pids):
    """{PID: the percent of one core it and every process under it use, by ps's own decaying %cpu} for those of pids
    that run, or None when ps cannot say: what each holder `qk admissions now` reads costs the Mac, for `qk queue
    doctor` to name beside a gate it holds back (vocaro/queuekeeper#89). A holder's own process mostly waits on what it
    started, so its children count. One bounded ps reads every process; nothing here judges a holder."""
    pids = {pid for pid in pids if type(pid) is int and pid > 0}
    if not pids: return {}
    try:
        found = subprocess.run(["ps", "-axo", "pid=,ppid=,%cpu="], capture_output=True, text=True, timeout=5,
                               stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError): return None
    if found.returncode: return None
    used, children = {}, {}
    for line in found.stdout.splitlines():
        parts = line.split()
        try: pid, parent, share = int(parts[0]), int(parts[1]), float(parts[2].replace(",", "."))
        except (IndexError, ValueError): continue
        used[pid] = share
        children.setdefault(parent, []).append(pid)
    totals = {}
    for pid in pids & set(used):
        total, left, seen = 0.0, [pid], set()
        while left:
            each = left.pop()
            if each in seen: continue
            seen.add(each)
            total += used.get(each, 0.0)
            left += children.get(each, [])
        totals[pid] = round(total, 1)
    return totals


def kernel_start(pid):
    """When pid started, as `ps -o lstart=` prints it with its spacing collapsed, from the kernel's own record of the
    process (sysctl KERN_PROC_PID), which reads none of its arguments and starts no process: LookupError when no such
    process runs, OSError when the kernel cannot say. As resources.kernel_start reads it, which this module, run on
    another Mac before the toolkit is there, cannot import."""
    try: os.kill(pid, 0)
    except ProcessLookupError: raise LookupError(f"no process {pid} runs") from None
    except PermissionError: pass
    if sys.platform != "darwin": raise OSError(f"the start of PID {pid} is read only from macOS's kernel")
    libc = ctypes.CDLL(None, use_errno=True)
    mib = (ctypes.c_int * 4)(1, 14, 1, pid)  # CTL_KERN, KERN_PROC, KERN_PROC_PID
    found = ctypes.create_string_buffer(1024)
    size = ctypes.c_size_t(len(found))
    if libc.sysctl(mib, 4, found, ctypes.byref(size), None, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, f"sysctl KERN_PROC_PID {pid}: {os.strerror(error)}")
    if size.value == 0: raise LookupError(f"no process {pid} runs")
    # A kinfo_proc begins with its extern_proc, whose first field is p_starttime (a timeval, seconds first) and whose
    # p_pid is at byte 40.
    if int.from_bytes(found.raw[40:44], sys.byteorder, signed=True) != pid:
        raise OSError(f"sysctl KERN_PROC_PID {pid} described another process")
    started = int.from_bytes(found.raw[0:8], sys.byteorder, signed=True)
    return " ".join(time.strftime("%a %b %e %H:%M:%S %Y", time.localtime(started)).split())


def process_state(record, listed):
    """running, ended (its PID runs no more, or runs another process) or unknown (neither ps nor the kernel could say:
    the census failed, or its PID runs and its start could not be read)."""
    if listed is None: return "unknown"
    pid = record.get("pid")
    if pid not in listed: return "ended"
    found = listed[pid]
    if found is None: return "unknown"
    return "running" if found == " ".join(str(record.get("start")).split()) else "ended"


def read_json(path, limit=NOW_BYTES):
    """A file's JSON without following a link or waiting on a FIFO, or raise OSError or ValueError."""
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode): raise ValueError(f"{path} is no regular file")
        data = stream.read(limit + 1)
    if len(data) > limit: raise ValueError(f"{path} is larger than {limit} bytes")
    return json.loads(data)


# The fields of a holder's live progress observation (progress.Monitor) that `qk admissions now` passes on, and how
# many of a holder's observations, at most, it reads: one a leaf, and a holder runs few leaves at once.
PROGRESS_FIELDS, HOLDER_OBSERVATIONS = ("lane", "host", "state", "action", "quietSeconds", "thresholdSeconds",
                                        "maximumQuietSeconds", "samples", "unobservableSamples", "observedSeconds",
                                        "maximumObservationGapSeconds"), 16


def holder_progress(guard, identity, at):
    """The live progress observations a holder's leaves publish under its admission (lane_progress.destinations):
    each fresh, running record in the holder's folder beside the guard, with its reporter's PID and start for the
    caller to check against its one ps, cut to PROGRESS_FIELDS, and how long ago it last saw progress. Only a lane
    that declares `stall` publishes one (vocaro/queuekeeper#70). Nothing that cannot be read counts: its holder
    is shown with no observation, never as quiet."""
    found = []
    if not re.fullmatch(r"[0-9A-Za-z]+", identity): return found
    try: names = sorted(os.listdir(str(guard) + ".progress/" + identity))
    except OSError: return found
    for name in names[:2 * HOLDER_OBSERVATIONS]:
        if not name.endswith(".json") or len(found) >= HOLDER_OBSERVATIONS: continue
        try: record = read_json(Path(str(guard) + ".progress") / identity / name, 64 << 10)
        except (OSError, ValueError): continue
        when = record.get("observedAt") if isinstance(record, dict) else None
        if not (isinstance(record, dict) and record.get("phase") == "running" and type(when) in (int, float)
                and 0 <= at - when <= 15 and type(record.get("reporter")) is int and record["reporter"] > 0
                and isinstance(record.get("reporterStart"), str)):
            continue
        kept = {field: record.get(field) for field in PROGRESS_FIELDS
                if type(record.get(field)) in (int, float, str) and not isinstance(record.get(field), bool)}
        for field in ("lane", "host", "state", "action"):
            if field in kept: kept[field] = str(kept[field])[:160]
        last = record.get("lastProgressAt")
        kept["lastProgressSeconds"] = round(max(0.0, at - last), 1) if type(last) in (int, float) else None
        found.append({**kept, "pid": record["reporter"], "start": record["reporterStart"]})
    return found


# Where a holder's work writes as it goes, as run_progress keeps it, which this file cannot import: below the directory
# it was admitted in, `qk unittest`'s progress folders (RUNS) and `qk run`'s lanes' logs (LANES, LANE_LOGS), and the
# files of a progress folder. A test keeps them the same as run_progress's. A reading looks at the newest
# HOLDER_RUN_FOLDERS progress folders and stats at most HOLDER_FILES files a holder.
HOLDER_RUNS, HOLDER_LANES = ".build/queuekeeper/runs", ".build/queuekeeper"
HOLDER_LANE_LOGS = ("*/run.log", "*/lanes/*.log", ".runs/*/*/run.log", ".runs/*/*/lanes/*.log")
HOLDER_RUN_FILES = ("progress.log", "progress.json", "output.log", "run.json")
HOLDER_RUN_FOLDERS, HOLDER_FILES, HOLDER_PATH = 32, 512, 1024


def holder_activity(owner, since, at):
    """How long the files a running holder's work writes have gone unchanged, for a holder whose lanes declare no
    `stall` and so publish no observation (holder_progress, vocaro/queuekeeper#70): {"quietSeconds", "source"}. Its
    guard entry names the directory it was admitted in and, for a runner admitted for a bare command, its progress
    folder (resources.work_place). The files are that folder's, those of each progress folder under the directory
    (HOLDER_RUNS) and its lanes' logs (HOLDER_LANE_LOGS), each counted only from since, its admission. quietSeconds
    is from the latest change to one of them, or from since when none changed, and source names which kind changed
    last, or is None. None when its entry names no directory, as an older toolkit's does, or since is unknown and
    nothing changed. It reads file times only, so it is evidence of movement and never a verdict: work that
    computes or waits without writing looks quiet, and another run in the same directory moves it too. Nothing here
    stops anything."""
    def usable(path): return isinstance(path, str) and 0 < len(path) <= HOLDER_PATH and os.path.isabs(path)
    directory, folder = owner.get("directory"), owner.get("progressRun")
    if not usable(directory): return None
    found, counted = [], [0]

    def look(paths, what):
        for path in paths:
            if counted[0] >= HOLDER_FILES: return
            counted[0] += 1
            try: changed = os.stat(path).st_mtime
            except OSError: continue
            if since is None or changed >= since: found.append((changed, what))

    if usable(folder): look([os.path.join(folder, name) for name in HOLDER_RUN_FILES], "its progress folder")
    runs = os.path.join(directory, HOLDER_RUNS)
    try: names = sorted(name for name in os.listdir(runs) if not name.startswith("."))[-HOLDER_RUN_FOLDERS:]
    except OSError: names = []
    look([os.path.join(runs, name, file) for name in reversed(names) for file in HOLDER_RUN_FILES],
         "a run's progress folder")
    lanes = glob.escape(os.path.join(directory, HOLDER_LANES))
    for pattern in HOLDER_LANE_LOGS:
        try: look(sorted(glob.glob(os.path.join(lanes, pattern)))[:HOLDER_FILES], "a lane's log")
        except OSError: continue
    when, what = max(found) if found else (since, None)
    if when is None: return None
    return {"quietSeconds": round(max(0.0, at - when), 1), "source": what}


# How queue doctor's note on an unmoved holder learns how long that lane's files may go unchanged, from its own past
# holds on this Mac (vocaro/queuekeeper#70): the latest QUIET_HOLDS admissions of its label that ended released, of
# which it needs QUIET_HOLDS_NEEDED, and QUIET_MARGIN times the longest of them. A quiet interval falls inside one hold,
# so no healthy run's quiet was longer than its longest hold. The developer guide says why these numbers.
QUIET_HOLDS, QUIET_HOLDS_NEEDED, QUIET_MARGIN = 20, 5, 1.5


def past_holds(labels):
    """{label: the seconds of its latest QUIET_HOLDS holds that ended released, oldest first} for each of labels, from
    this Mac's admission log, previous generation first; None when the log cannot be read. A log not yet written
    records none. It reads without admission's lock, as the log is only appended to and a torn last line is skipped."""
    found = {label: [] for label in labels}
    if not found: return found
    directory = admission_directory()
    for name in ADMISSION_LOGS:
        try:
            with (directory / name).open("rb") as stream: lines = stream.read().splitlines()
        except FileNotFoundError: continue
        except OSError: return None
        for line in lines:
            if b'"ended"' not in line: continue
            try: entry = json.loads(line)
            except ValueError: continue
            if not isinstance(entry, dict) or entry.get("event") != "ended" or entry.get("outcome") != "released":
                continue
            held, kept = entry.get("held"), found.get(entry.get("label"))
            if kept is None or type(held) not in (int, float) or held < 0: continue
            kept.append(float(held))
            if len(kept) > QUIET_HOLDS: del kept[0]
    return found


def quiet_bound(held):
    """What a holder's past holds (past_holds) say of how long its files may go unchanged: {"holds", "longestSeconds",
    "needed", "seconds"}, seconds None when fewer than QUIET_HOLDS_NEEDED are logged. None when they could not be read."""
    if held is None: return None
    longest = max(held) if held else None
    return {"holds": len(held), "longestSeconds": None if longest is None else round(longest, 1),
            "needed": QUIET_HOLDS_NEEDED,
            "seconds": round(QUIET_MARGIN * longest, 1) if len(held) >= QUIET_HOLDS_NEEDED else None}


def reclaimed_lately(at):
    """The holders admission freed in the NOW_RECLAIMED_SECONDS before at, their owners ended or unknown
    (resources.free_holder), newest first, at most NOW_RECLAIMED, from the end of this Mac's admission log: each with
    when, why and what was signaled. FileNotFoundError is no failure: a log not yet written records none."""
    path = admission_directory() / ADMISSION_LOGS[-1]
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode): raise ValueError(f"{path} is no regular file")
        size = os.fstat(stream.fileno()).st_size
        stream.seek(max(0, size - NOW_LOG_TAIL))
        lines = stream.read(NOW_LOG_TAIL).splitlines()[1 if size > NOW_LOG_TAIL else 0:]
    found = []
    for line in reversed(lines):
        try: entry = json.loads(line)
        except ValueError: continue
        if not isinstance(entry, dict) or entry.get("event") != "reclaimed" or not isinstance(entry.get("identity"), str):
            continue
        try: moment = datetime.fromisoformat(entry["time"]).timestamp()
        except (KeyError, TypeError, ValueError): continue
        if at - moment > NOW_RECLAIMED_SECONDS: break
        whole = lambda key: [item for item in entry.get(key) or [] if type(item) is int] \
            if isinstance(entry.get(key), list) else []
        found.append({"identity": entry["identity"][:64], "label": str(entry.get("label") or "an unnamed owner")[:160],
                      "pid": entry.get("pid") if type(entry.get("pid")) is int else None,
                      "kind": str(entry.get("kind"))[:32], "wholeHost": entry.get("wholeHost") is True,
                      "slots": whole("slots"), "model": entry.get("model") is True,
                      "process": entry.get("process") if entry.get("process") in ("ended", "unknown") else None,
                      "why": str(entry.get("why") or "")[:300] or None,
                      "reclaimedAt": moment, "reclaimedSeconds": round(max(0.0, at - moment), 1),
                      "signaled": [{"pid": item["pid"], "signals": [str(name)[:16] for name in item.get("signals") or []][:4]}
                                    for item in entry.get("signaled") or [] if isinstance(item, dict)
                                    and type(item.get("pid")) is int][:32],
                      "survived": whole("survived")[:32],
                      "unsignaled": str(entry["unsignaled"])[:300] if entry.get("unsignaled") else None,
                      "replaced": [str(name)[:16] for name in entry.get("replaced") or []][:32]
                      if isinstance(entry.get("replaced"), list) else []})
        if len(found) >= NOW_RECLAIMED: break
    return found


def slot_names(directory, count):
    """The identity each of the first count slot files names as its last holder, None for a file that names none,
    and absent for a slot never taken. Read without a lock: a name is who last took it, not that its lock is held."""
    found = {}
    for slot in range(count):
        try: written = (directory / f"slot{slot}").read_text(errors="replace").split(" ", 2)
        except FileNotFoundError: continue
        except OSError: written = []
        found[slot] = written[1] if len(written) > 1 and written[1] else None
    return found


def checkpoint_readings(directory, reasons):
    """Read release checkpoints whose writers still hold their project locks.

    The toolkit sets `runsIn` when its dedicated worktree is ready, and names a lane when it starts one.
    A record can establish that a checkpoint is live before it can identify which admissions belong to it.
    """
    folder = directory / "checkpoints"
    try: names = sorted(name for name in os.listdir(folder) if name.endswith(".json") and not name.startswith("."))
    except FileNotFoundError: return []
    except OSError as error:
        reasons.append(f"the checkpoint records in {folder} cannot be listed: {error}")
        return None
    before = len(reasons)
    if len(names) > NOW_RECORDS:
        reasons.append(f"{len(names)} checkpoint records, of which only {NOW_RECORDS} were read")
    found = []
    for name in names[:NOW_RECORDS]:
        record = folder / name
        lock = record.with_suffix(".lock")
        try:
            fd = os.open(lock, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode): continue
                try: fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                except BlockingIOError: checkpoint = read_json(record, 64 << 10)
                else: continue
            finally: os.close(fd)
        except FileNotFoundError: continue  # the checkpoint ended as it was read
        except (OSError, ValueError) as error:
            reasons.append(f"the checkpoint record {record} cannot be read: {error}")
            continue
        if not isinstance(checkpoint, dict):
            reasons.append(f"the checkpoint record {record} is not an object")
            continue
        project, commit, tree, lane = (checkpoint.get(key) for key in ("project", "commit", "runsIn", "lane"))
        index, count = checkpoint.get("index"), checkpoint.get("count")
        if (all(isinstance(value, str) and value for value in (project, commit))
                and (tree is None or isinstance(tree, str) and len(tree) <= 300 and os.path.isabs(tree))
                and type(index) is int and type(count) is int and 0 <= index <= count
                and (lane is None if index == 0 else isinstance(lane, str) and bool(lane))):
            found.append({"project": project[:160], "commit": commit[:64], "runsIn": tree,
                          "lane": None if lane is None else lane[:160],
                          "index": index, "count": count})
        else:
            reasons.append(f"the checkpoint record {record} has invalid fields")
    return found if found or len(reasons) == before else None


def admissions_now(at=None):
    """Who holds and who waits for heavy-work admission on this Mac now, for `qk admissions now` here and on the second
    Mac (vocaro-engineering#342). It changes nothing: it reads the guard, the queue, the slot files' names and the
    waiters' observation records as they stand, without any of admission's locks, and checks the processes they name
    with one ps, or the kernel PID by PID when ps fails (census). What it cannot read is null with a reason and makes
    the reading partial, never empty: a host that cannot be observed is never reported idle. state is observed,
    partial or unknown."""
    at = time.time() if at is None else at
    reasons = []
    isolated = {name: os.environ[name] for name in ("HEAVY_SLOT_DIR", "QUEUEKEEPER_GATE_LOCK_FILE") if os.environ.get(name)}
    guard = Path(os.environ.get("QUEUEKEEPER_GATE_LOCK_FILE", GATE_LOCK) + ".warm-push-guard")
    reading = {"schemaVersion": NOW_SCHEMA, "mac": mac_name(), "observedAt": at, "state": "observed", "reasons": reasons,
               "namespace": {"shared": not isolated, **isolated, "guard": str(guard)}, "limits": None, "slots": None,
               "model": None, "holders": None, "unattributed": None, "waiters": None, "reclaimed": None,
               "checkpoints": None}
    try: directory = slot_directory()
    except RuntimeError as error:
        return {**reading, "state": "unknown", "reasons": [str(error)]}
    reading["namespace"]["slotDirectory"] = str(directory)
    try:
        found = limits()
        slots, maximum = found["slots"], found["slotsMax"]
        reading["limits"] = {"slots": slots, "slotsMax": maximum}
    except ValueError as error:
        reasons.append(str(error))
        slots = maximum = None
    # The guard: a missing one records no holder; one that cannot be read, or is not admission's, says nothing.
    owners = None
    try: state = read_json(guard)
    except FileNotFoundError: owners = {}
    except (OSError, ValueError) as error: reasons.append(f"the guard {guard} cannot be read: {error}")
    else:
        recorded = state.get("owners") if isinstance(state, dict) and state.get("vocaroEngineering") == 1 else None
        if isinstance(recorded, dict) and all(isinstance(owner, dict) and type(owner.get("pid")) is int
                                              and isinstance(owner.get("start"), str) for owner in recorded.values()):
            owners = recorded
        else: reasons.append(f"the guard {guard} is not in admission's format")
    # The queue: every waiting request, in its order; a missing queue holds none.
    queued = None
    try: queue = read_json(directory / QUEUE)
    except FileNotFoundError: queued = []
    except (OSError, ValueError) as error: reasons.append(f"the queue {directory / QUEUE} cannot be read: {error}")
    else:
        waiters = queue.get("waiters") if isinstance(queue, dict) else None
        if isinstance(waiters, list): queued = [waiter for waiter in waiters if isinstance(waiter, dict)]
        else: reasons.append(f"the queue {directory / QUEUE} is not in admission's format")
    # The waiters' own records, each the latest a waiting request wrote of its wait.
    observed = {}
    try: names = sorted(os.listdir(directory / WAITS))
    except FileNotFoundError: names = []
    except OSError as error:
        names = []
        reasons.append(f"the waiters' records in {directory / WAITS} cannot be listed: {error}")
        observed = None
    names = [name for name in names if name.endswith(".json") and not name.startswith(".")]
    if len(names) > NOW_RECORDS:
        reasons.append(f"{len(names)} waiters' records, of which only {NOW_RECORDS} were read")
        names = names[:NOW_RECORDS]
    unread = 0
    for name in names:
        try: record = read_json(directory / WAITS / name, 64 << 10)
        except FileNotFoundError: continue  # its request stopped waiting as it was read
        except (OSError, ValueError): record = None
        if not (isinstance(record, dict) and record.get("schemaVersion") == NOW_SCHEMA and isinstance(record.get("identity"), str)
                and type(record.get("pid")) is int and isinstance(record.get("start"), str)):
            unread += 1
            continue
        if observed is not None: observed[record["identity"]] = record
    if unread: reasons.append(f"{unread} waiters' record{'' if unread == 1 else 's'} could not be read")
    # Each holder's live progress observations, whose reporters the same ps checks.
    observations = {identity: holder_progress(guard, identity, at) for identity, owner in (owners or {}).items()
                    if owner.get("uncertain") is not True}
    # One ps for every process named.
    pids = [owner["pid"] for owner in (owners or {}).values()]
    pids += [record["pid"] for records in observations.values() for record in records]
    pids += [waiter.get("pid") for waiter in queued or []] + [record["pid"] for record in (observed or {}).values()]
    listed = census(pids)
    if listed is None and pids:
        reasons.append("neither ps nor the kernel could say which of the processes named still run")
    elif listed and None in listed.values():
        unread = sorted(pid for pid, start in listed.items() if start is None)
        reasons.append(f"ps failed, and the kernel could not say whether PID {', '.join(map(str, unread))} still "
                       f"run{'s' if len(unread) == 1 else ''} as the process named")
    # Slots: the guaranteed ones, then those beyond them up to HEAVY_SLOTS_MAX, each with the identity its file names.
    count = maximum
    if count is None:
        # Without the limits, the slots are those whose files exist.
        try: count = 1 + max((int(name[4:]) for name in os.listdir(directory) if re.fullmatch(r"slot\d+", name)), default=-1)
        except OSError: count = 0
    named = slot_names(directory, min(count, NOW_RECORDS))
    # The holders admission reclaimed lately, which hold nothing now: shown as reclaimed, never as held.
    try: reading["reclaimed"] = reclaimed_lately(at)
    except FileNotFoundError: reading["reclaimed"] = []
    except (OSError, ValueError) as error:
        reasons.append(f"the admission log cannot be read for the holders admission reclaimed: {error}")
    try: model = (directory / "model").read_text(errors="replace").split(" ", 2)
    except OSError: model = []
    model_name = model[1] if len(model) > 1 else None
    if owners is not None:
        holders = []
        for identity, owner in owners.items():
            exclusive = owner.get("exclusive") is True
            held = list(range(count)) if exclusive else sorted(slot for slot, name in named.items() if name == identity)
            try: since = datetime.fromisoformat(owner["admitted"]).timestamp()
            except (KeyError, TypeError, ValueError): since = None
            process = process_state(owner, listed)
            progress = None
            if process == "running" and identity in observations:
                records = observations[identity]
                progress = [{key: value for key, value in record.items() if key not in ("pid", "start")}
                            for record in records if process_state(record, listed) == "running"]
                # If no leaf was verified, an unreadable reporter leaves progress unknown.
                if not progress and any(process_state(record, listed) == "unknown" for record in records):
                    progress = None
            holders.append({"identity": identity, "label": str(owner.get("label") or "an unnamed owner")[:160],
                            "pid": owner["pid"], "start": owner["start"], "admittedAt": since,
                            "heldSeconds": None if since is None else round(max(0.0, at - since), 1),
                            "exclusive": exclusive, "wholeHost": owner.get("wholeHost") is True,
                            "origin": origin_of(owner.get("origin")),
                            "model": owner.get("model") is True or exclusive,
                            # Slots its files name; None for an entry that held a slot no file still names, such as one
                            # an older toolkit wrote over, which admission counts but no reading can place.
                            "slots": held if held or exclusive else None,
                            "guaranteed": [slot for slot in held if slots is None or slot < slots],
                            "extra": [slot for slot in held if slots is not None and slot >= slots],
                            "process": process, "progress": progress,
                            # When the files its work writes last changed, for one whose lanes publish no observation.
                            "activity": holder_activity(owner, since, at)
                            if process == "running" and owner.get("uncertain") is not True else None,
                            # One whose process has ended or cannot be confirmed running, admission frees at its next
                            # attempt, stopping what is left of it (vocaro/queuekeeper#23).
                            "reclaimable": process in ("ended", "unknown"),
                            # An uncertain entry whose owner still runs is stranded: admission counts it until recovery
                            # removes it.
                            "stranded": owner.get("uncertain") is True and process == "running"})
        # What each running holder costs the CPU now, None when that cannot be read or it does not run (#89).
        running = [holder["pid"] for holder in holders if holder["process"] == "running"]
        used = cpu_use(running) if running else {}
        if used is None: reasons.append("ps could not say how much CPU the holders use")
        for holder in holders:
            holder["cpuPercent"] = (used or {}).get(holder["pid"]) if holder["process"] == "running" else None
        # How long each holder whose files are read may go unchanged, from its lane's own past holds here (#70).
        watched = [holder for holder in holders if holder["activity"] is not None]
        held = past_holds({holder["label"] for holder in watched}) if watched else {}
        for holder in holders:
            holder["quietBound"] = quiet_bound(None if held is None else held[holder["label"]]) \
                if holder["activity"] is not None else None
        reading["holders"] = sorted(holders, key=lambda holder: holder["admittedAt"] or 0)
        # A slot whose file still names a holder admission reclaimed is free, and says so (vocaro/queuekeeper#23).
        freed = {item["identity"] for item in reading["reclaimed"] or []}
        reading["slots"] = [{"slot": slot, "guaranteed": slots is None or slot < slots,
                             "holder": named.get(slot) if named.get(slot) in owners else None,
                             "reclaimed": named.get(slot) if named.get(slot) in freed and named.get(slot) not in owners
                             else None, "file": slot in named} for slot in range(min(count, NOW_RECORDS))]
        # A slot file that names no guard entry: its holder released it, or holds it outside admission's guard (an
        # old slot API caller); its lock was not probed, since a reading takes no lock.
        reading["unattributed"] = [slot for slot, name in sorted(named.items()) if name not in owners and name not in freed
                                   and not any(owner.get("exclusive") is True for owner in owners.values())]
        reading["model"] = {"holder": model_name if model_name in owners and (owners[model_name].get("model") is True
                                                                             or owners[model_name].get("exclusive") is True)
                            else next((identity for identity, owner in owners.items() if owner.get("exclusive") is True), None)}
    if queued is not None and observed is not None:
        waiting, position, seen = [], 0, set()
        try: hold = float(os.environ.get("HEAVY_PLACE_SECONDS", str(PLACE_SECONDS)))
        except ValueError: hold = PLACE_SECONDS
        for waiter in queued:
            identity = waiter.get("identity")
            kind = "exclusive" if not (waiter.get("ordinary") or waiter.get("wholeHost")) else \
                "wholeHost" if waiter.get("wholeHost") else "ordinary"
            if kind != "ordinary": position += 1
            record = observed.get(identity)
            sender = waiter.get("heldFor") if kind == "wholeHost" else None
            farm_place = isinstance(sender, str) and bool(sender)
            # A farm place belongs to its keeper. An observation of the same arrival may name another process.
            process = process_state(waiter if farm_place else record or waiter, listed)
            if process == "ended": continue
            if farm_place:
                renewed = waiter.get("seen")
                if process != "running" or type(renewed) not in (int, float) or not at - renewed < hold:
                    continue
                if record and process_state(record, listed) != "running": record = None
            row = waiter_reading(waiter, record, kind, position if kind != "ordinary" else None, process, at, hold)
            if farm_place: row.update(heldFor=sender[:160], away=True, holdsBack=True)
            waiting.append(row)
            seen.add(identity)
        for identity, record in observed.items():
            if identity in seen: continue
            process = process_state(record, listed)
            if process == "ended": continue
            waiting.append(waiter_reading({}, record, record.get("queueKind") or "ordinary", None, process, at, hold))
        reading["waiters"] = sorted(waiting, key=lambda waiter: -(waiter["waitedSeconds"] or 0))
    reading["checkpoints"] = checkpoint_readings(directory, reasons)
    if reasons: reading["state"] = "partial"
    return bounded(reading)


def wait_lane_name(name):
    """A bounded lane name, kept whole because a truncated label cannot identify a lane."""
    if isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,159}", name): return name
    return None


def waiter_reading(waiter, record, kind, position, process, at, hold):
    """One waiting request: from its queue record and its own observation record, either of which may be missing. A
    queued request without a record is a place kept while its process is away, such as offering its work to another
    Mac, or one an older toolkit queued, which writes none; it says when it last asked and whether it still holds later
    work back (HEAVY_PLACE_SECONDS), and no reason."""
    number = lambda value: value if type(value) in (int, float) else None
    record = record or {}
    queued_at, sampled, waited = number(waiter.get("queuedAt") or record.get("queuedAt")), number(record.get("sampledAt")), \
        number(record.get("waited"))
    # While its caller says it still waits (resources.Place.still_waiting), `seen` is when it last said so and
    # `asked` when the request last asked.
    seen, kept = number(waiter.get("seen")), number(waiter.get("asked")) is not None
    asked = number(waiter.get("asked")) if kept else seen
    if queued_at is not None: total = max(0.0, at - queued_at)
    elif waited is not None and sampled is not None: total = max(0.0, waited + at - sampled)
    else: total = None
    lane = wait_lane_name(record.get("lane")) if (process == "running" and not waiter.get("heldFor")
                                                  and sampled is not None and 0 <= at - sampled <= NOW_STALE) else None
    return {"identity": waiter.get("identity") or record.get("identity"), "request": record.get("request"), "kind": kind,
            "model": record.get("model") is True, "label": str(waiter.get("label") or record.get("label") or "an unnamed request")[:160],
            "pid": waiter.get("pid") if type(waiter.get("pid")) is int else record.get("pid"), "process": process,
            "position": position, "queued": bool(waiter), "waitedSeconds": None if total is None else round(total, 1),
            "refusal": record.get("refusal"), "reason": record.get("reason"), "origin": origin_of(record.get("origin")),
            "sampledSeconds": None if sampled is None else round(max(0.0, at - sampled), 1),
            "stale": sampled is not None and at - sampled > NOW_STALE,
            # A queued request with no record that asked lately is asking here, under a toolkit that writes no record or
            # before its first refusal; one that has not is away, its place kept.
            "away": not record and (kept or seen is None or at - seen > NOW_STALE),
            "lastAskedSeconds": None if asked is None else round(max(0.0, at - asked), 1),
            "holdsBack": None if record or seen is None else at - seen < hold, **({"lane": lane} if lane else {})}


def origin_of(found):
    """Where an admission's work comes from, as its guard entry or wait record says (resources.work_origin): its string
    fields, each cut to 300 characters, or None."""
    if not isinstance(found, dict): return None
    kept = {field: found[field][:300] for field in ("repository", "branch", "worktree", "session", "mac")
            if isinstance(found.get(field), str) and found[field]}
    return kept or None


def bounded(reading):
    """The reading, cut to NOW_BYTES of JSON by leaving out the longest waits' and the oldest holders' tails, partial."""
    if len(json.dumps(reading)) <= NOW_BYTES: return reading
    for key in ("waiters", "holders", "slots", "checkpoints"):
        while reading.get(key) and len(json.dumps(reading)) > NOW_BYTES:
            reading[key] = reading[key][:len(reading[key]) // 2]
    reading["state"] = "partial"
    reading["reasons"] = [*reading["reasons"], f"the reading was cut to {NOW_BYTES} bytes"]
    return reading


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
    simulator's device, and that no live run accounts for: what `qk remote` prints before it sends
    work, and `qk doctor` reports. It reads and reports, and stops nothing.

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
    # live run names: a run can name a device by its kind alone, as qk simulator-tests does.
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
        # IMTransferAgent did on the Macs (macOS 27, October 2026).
        if process["uid"] != uid or pid in own or pid in runs or process["args"].startswith("/System/"): continue
        where = place(pid)
        if where is not None: placed[pid] = where
    # What a live run holds is its own with everything below it, as a simulator's processes are.
    holding = {pid for pid, where in placed.items() if holds(pid, *where)}
    if holding: runs |= {pid for pid in table if holding.intersection(ancestry(pid))}
    candidates = {pid: where for pid, where in placed.items() if pid not in runs}
    children = {}
    for pid, process in table.items(): children.setdefault(process["parent"], []).append(pid)
    # This file stays ASCII: it reaches another Mac as a command-line argument.
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


PS = ["ps", "-axww", "-o", "pid=,ppid=,uid=,lstart=,args="]


def collapsed(start):
    """A start with its spacing collapsed, or None."""
    return " ".join(start.split()) if isinstance(start, str) else None


class Launches:
    """The record a run keeps, while it holds a checkout slot, of each process launchd starts or adopts from the
    slot's build products: the bundle executable (.app, .xpc or .appex) that a UI test or an extension's host
    launches, which descends from no process of the run and so is in no admission's process record. Each line names
    the process by PID, start and command line, the run by its PID and start, and when the run claimed the slot and
    first saw the process. It is stop_leftovers' positive evidence that the process is that run's, which the census
    alone cannot give: a path, parent 1 and no live owner say only that nothing claims it now. A failure to sample or
    to write never stops the run; it leaves the process unrecorded, which only keeps it running."""

    def __init__(self, base, index, listed=listing, clock=time.time):
        self.path, self.listed, self.clock = base / f"{index}.{LAUNCHED}", listed, clock
        self.prefixes, self.uid, self.broken = forms(base / str(index)), os.getuid(), False
        self.run = {"pid": os.getpid(), "start": collapsed(output(["ps", "-p", str(os.getpid()), "-o", "lstart="]))}
        self.claimed, self.seen = clock(), set()
        try:
            # What earlier runs recorded stays while its process runs, for the stop that proves it theirs.
            lines = self.path.read_text(errors="replace").splitlines()[-LAUNCHED_KEPT:]
            if not lines: return
            # Pruning needs PID and start time. Only sampling new launches needs arguments.
            table = process_table(listed(["ps", "-ax", "-o", "pid=,ppid=,uid=,lstart="]))
            if table:
                kept = [line for line in lines if self.running(line, table)]
                temporary = self.path.with_name(self.path.name + ".new")
                temporary.write_text("".join(line + "\n" for line in kept))
                temporary.replace(self.path)
        except (OSError, ValueError):
            pass

    @staticmethod
    def running(line, table):
        try: entry = json.loads(line)
        except ValueError: return False
        found = table.get(entry.get("pid")) if isinstance(entry, dict) else None
        return found is not None and found["start"] == collapsed(entry.get("start"))

    def sample(self):
        if self.broken or self.run["start"] is None: return
        try:
            table, now, lines = process_table(self.listed(PS)), self.clock(), []
            for pid, process in table.items():
                bundle = BUNDLE.match(process["args"])
                began = started(process["start"])
                if process["uid"] != self.uid or process["parent"] != 1 or bundle is None or began is None \
                        or began < self.claimed - 1 or (pid, process["start"]) in self.seen \
                        or not any(bundle.group(0).startswith(prefix) for prefix in self.prefixes): continue
                self.seen.add((pid, process["start"]))
                lines.append({"pid": pid, "start": process["start"], "args": process["args"], "run": self.run,
                              "claimed": self.claimed, "seen": now})
            if lines:
                with open(self.path, "a") as stream: stream.write("".join(json.dumps(line) + "\n" for line in lines))
        except Exception as error:
            self.broken = True
            with contextlib.suppress(Exception):
                say(f"records no further process launched from this checkout slot, which stays running after the run "
                    f"rather than be stopped by a later cleanup: {type(error).__name__}: {error}")


def launch_records(path, limit=NOW_BYTES * 4):
    """The lines of a slot's Launches record, or None when it exists and cannot be read."""
    try: entries = [json.loads(line) for line in read_text(path, limit).splitlines() if line.strip()]
    except FileNotFoundError: return []
    except (OSError, ValueError): return None
    return [entry for entry in entries if isinstance(entry, dict)]


def read_text(path, limit):
    """A file's text without following a link or waiting on a FIFO, or raise OSError or ValueError."""
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode): raise ValueError(f"{path} is no regular file")
        data = stream.read(limit + 1)
    if len(data) > limit: raise ValueError(f"{path} is larger than {limit} bytes")
    return data.decode(errors="replace")


def proofs(root, listed=listing, written=disk_writes):
    """Which leftovers in a checkout slot stop_leftovers may stop, each with why it qualifies, and which it keeps,
    each with the evidence that is missing, and anything that makes the whole census uncertain, which stops nothing.
    A leftover qualifies only when every one of these holds:

    - The census lists it, with nothing it could not check: no live run, admission or reservation accounts for it.
    - Its parent is launchd (PID 1), it has no descendants, and its command line starts with a bundle executable
      inside the checkout slot it runs from.
    - The slot's Launches record names this very process, by PID, start and command line, and the run that saw it
      start after that run claimed the slot; and names no other run for it.
    - That run has ended: its PID runs no more, or runs a process with another start.
    - No live run holds the slot now, as its lock's record says; a lock that cannot be read keeps it."""
    census = leftovers(root, listed, written)
    if "error" in census: return [], [], census["error"]
    if census.get("unchecked"):
        return [], [], "the census could not check " + "; ".join(census["unchecked"])
    if not census.get("leftovers"): return [], [], None
    table = process_table(listed(PS))
    if not table: return [], [], "ps listed no processes for the check"
    checkouts = Path.home() / root / "checkouts"
    prefixes = forms(checkouts)
    proven, kept = [], []
    for row in census["leftovers"]:
        if row["place"] != "checkout": continue
        pid, why = row["pid"], None
        item = {"pid": pid, "start": row["start"], "command": row["command"], "path": row["path"]}
        process = table.get(pid)
        where = next((row["path"][len(prefix):].split("/") for prefix in prefixes
                      if (row["path"] + "/").startswith(prefix)), [])
        bundle = BUNDLE.match(process["args"]) if process else None
        slot = checkouts / where[0] / where[1] if len(where) > 1 and where[0] and where[1] else None
        if process is None or process["start"] != row["start"] or process["parent"] != row["parent"]:
            why = "it changed while it was checked"
        elif row["parent"] != 1:
            why = f"its parent is PID {row['parent']}, not launchd, so it is not a process launchd started or adopted"
        elif row["descendants"]:
            why = f"it has {len(row['descendants'])} descendants, which no run recorded"
        elif slot is None or bundle is None or not any(bundle.group(0).startswith(form) for form in forms(slot)):
            why = "its command line does not start with a bundle executable in the checkout slot it runs from"
        if why is None:
            item.update(executable=bundle.group(0), slot=str(slot))
            records = launch_records(slot.parent / f"{slot.name}.{LAUNCHED}")
            named = [entry for entry in records or [] if entry.get("pid") == pid
                     and collapsed(entry.get("start")) == process["start"]]
            runs = {json.dumps(entry.get("run"), sort_keys=True) for entry in named}
            run = named[0].get("run") if named else None
            run = run if isinstance(run, dict) else {}
            if records is None:
                why = f"its slot's record of launched processes, {slot.name}.{LAUNCHED}, cannot be read"
            elif not named:
                why = ("no run's record names this process with its start, so nothing ties it to a run that ended: "
                       "a run that started it was not seeing it, or it was started by hand or after the run")
            elif len(runs) > 1 or any(entry.get("args") != process["args"] for entry in named):
                why = "its slot's record names it for more than one run, or with another command line"
            elif not isinstance(run.get("pid"), int) or not run.get("start"):
                why = "its slot's record does not name the run that saw it start"
            elif table.get(run["pid"], {}).get("start") == collapsed(run["start"]):
                why = f"the run that saw it start, PID {run['pid']}, still runs"
            elif not all(isinstance(named[0].get(key), (int, float)) for key in ("claimed", "seen")) or \
                    started(process["start"]) is None or \
                    not named[0]["claimed"] - 1 <= started(process["start"]) <= named[0]["seen"] + 1:
                why = "its start does not lie between its run's claim of the slot and when the run saw it"
            else:
                # A claim truncates the lock before it writes its holder, so an empty one may be a live run's.
                try: lock = json.loads(read_text(slot.parent / f"{slot.name}.lock", 4096))
                except FileNotFoundError: lock = None
                except (OSError, ValueError): lock = False
                holder = identities([lock]) if isinstance(lock, dict) else []
                if lock is False or (lock is not None and not holder):
                    why = "its slot's lock names no holder that can be read, so a live run may hold the slot"
                elif holder and table.get(holder[0][0], {}).get("start") == holder[0][1]:
                    why = f"a live run, PID {holder[0][0]}, holds its checkout slot"
                else:
                    item.update(run=run, reason=(
                        f"launchd started it from checkout slot {slot}'s build products at {process['start']}, while "
                        f"the run PID {run['pid']}, started {run['start']}, held the slot and recorded it; that run "
                        "has ended, and no live run, admission or reservation accounts for it"))
        (kept if why else proven).append({**item, "hold": why} if why else item)
    return proven, kept, None


def log_stop(event, item, **more):
    """Append one line to the log of leftover stops; raise OSError when it cannot be written."""
    line = {"time": datetime.now(timezone.utc).isoformat(timespec="seconds"), "event": event,
            **{key: item.get(key) for key in ("pid", "start", "command", "executable", "slot", "run", "reason")}, **more}
    path = admission_directory() / STOP_LOG
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NONBLOCK, 0o644)
    try: os.write(descriptor, (json.dumps(line) + "\n").encode())
    finally: os.close(descriptor)


def stop_leftovers(root, dry_run=False, listed=None, kill=os.kill, clock=time.monotonic, sleep=time.sleep):
    """Stop each leftover that proofs proves an ended run's, in the routine cleanup before a dispatch to the second
    Mac and in `qk simulator-pool reap` there (vocaro-engineering#353). Immediately before the signal it runs the
    census and every check again from fresh listings, and signals only a process with the same PID, start, command
    line and run; it logs the stop before SIGTERM and how it ended after, and sends nothing more: one still running
    STOP_WAIT_SECONDS later is reported. Whatever is uncertain it keeps running and reports with the evidence that is
    missing. It never raises; listed, kill, clock and sleep are for tests. It answers with stopped, failed, kept,
    eligible (for a dry run) and problem."""
    report = {"dryRun": dry_run, "stopped": [], "failed": [], "kept": [], "eligible": [], "problem": None}
    look = listed or (lambda command: listing(command, 20))
    try:
        deadline = clock() + STOP_SECONDS
        found, report["kept"], report["problem"] = proofs(root, look)
        if dry_run or report["problem"]:
            report["eligible"] = found if dry_run else []
            return report
        identity = lambda item: (item["pid"], item["start"], item.get("executable"), json.dumps(item.get("run")))
        for item in found:
            if deadline - clock() < STOP_WAIT_SECONDS + 5:
                report["kept"].append({**item, "hold": f"this cleanup's {STOP_SECONDS}s were used up"})
                continue
            again, kept, problem = proofs(root, look)
            match = next((other for other in again if identity(other) == identity(item)), None)
            if match is None:
                why = problem or next((other["hold"] for other in kept if other["pid"] == item["pid"]),
                                      "the census no longer lists it")
                report["kept"].append({**item, "hold": f"it changed before the second check passed: {why}"})
                continue
            try: log_stop("stopping", match)
            except OSError as error:
                report["failed"].append({**match, "error": f"the log {STOP_LOG} could not be written, so it was not "
                                                           f"stopped: {error}"})
                continue
            try: kill(match["pid"], signal.SIGTERM)
            except ProcessLookupError:
                report["stopped"].append({**match, "ended": "before SIGTERM"})
                with contextlib.suppress(OSError): log_stop("gone", match)
                continue
            except OSError as error:
                report["failed"].append({**match, "error": f"SIGTERM failed: {error}"})
                with contextlib.suppress(OSError): log_stop("failed", match, error=report["failed"][-1]["error"])
                continue
            waited = clock() + STOP_WAIT_SECONDS
            while True:
                table = process_table(look(PS))
                if table and table.get(match["pid"], {}).get("start") != match["start"]:
                    report["stopped"].append(match)
                    with contextlib.suppress(OSError): log_stop("stopped", match)
                    break
                if clock() >= waited:
                    error = (f"it still ran {STOP_WAIT_SECONDS}s after SIGTERM, and nothing more is sent"
                             if table else "ps could not say whether it ended after SIGTERM")
                    report["failed"].append({**match, "error": error})
                    with contextlib.suppress(OSError): log_stop("failed", match, error=error)
                    break
                sleep(0.2)
    except Exception as error:
        report["problem"] = f"{type(error).__name__}: {error}"
    return report


class ReapProblem(RuntimeError):
    """What reap could not read or do, which leaves the simulator running."""


def start_shutdown(udid):
    """`xcrun simctl shutdown UDID` started: the only change reap makes to a simulator."""
    return subprocess.Popen(["xcrun", "simctl", "shutdown", udid], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)


def known_pools(root):
    """The simulator pools that the projects' checkouts under root name in queuekeeper.json."""
    found = set()
    for path in (Path.home() / root / "checkouts").glob("*/*/*/queuekeeper.json"):
        try: name = json.loads(path.read_text()).get("simulatorPool")
        except (OSError, ValueError, AttributeError): continue
        if isinstance(name, str) and name: found.add(name)
    return found


def pool_of(name, pools, runtime=""):
    """The pool a simulator belongs to and as what: (POOL, "member") for a name `POOL LABEL N`, and
    (POOL, "template") for its kind's warmed template, `POOL LABEL template (RUNTIME)` on that runtime, as
    simulators/pool.py lists one; else (None, None). Reaping may shut down a template too, by the owner's
    decision of 2026-11-04."""
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
    return path.parent / ".queuekeeper-pool-template.json"


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
    guard = Path(os.environ.get("QUEUEKEEPER_GATE_LOCK_FILE", GATE_LOCK) + ".warm-push-guard")
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
                      "command": recover_command(key, entry.get("revision", 0))})
    return found


def recover_command(key, revision, verified="'WHAT YOU CHECKED'", host=None, *, toolkit=None):
    """The shell command that recovers the entry key at revision, from any folder using the caller's toolkit
    checkout when available. A bootstrap or wheel with no checkout uses the standard checkout path. --by and
    --verified are the reader's to fill in. With --host, that Mac's login shell
    runs its own Queuekeeper as `qk admissions recover --host` does. A command given to ssh itself finds
    the system's python3, which is too old to start qk (vocaro/queuekeeper#167)."""
    there = f"--host {shlex.quote(host)} " if host else ""
    launcher = QK_DEVICES
    if toolkit is not None and (Path(toolkit) / "bin/qk").is_file():
        launcher = f"python3 {shlex.quote(str(Path(toolkit) / 'bin/qk'))} --project {shlex.quote(str(toolkit))} devices"
    options = f"--by SESSION --revision {revision} --verified {verified}"
    name = shlex.quote(key)
    arguments = f"{options} -- {name}" if key.startswith("-") else f"{name} {options}"
    return f"{launcher} recover {there}{arguments}"


def passed(due):
    """Whether an expected release, as ISO 8601 text, has passed; None when it cannot be read."""
    try: return datetime.fromisoformat(str(due).replace("Z", "+00:00")).timestamp() <= time.time()
    except (ValueError, OverflowError, OSError): return None


def recoverable_line(item, host=None, *, toolkit=None):
    """What is said of one entry recoverable found and reap left, with why it left it when it says. For an
    entry in another Mac's record, host, the command names it with --host and so runs there, through its toolkit checkout
    (recover_command), since a recovery here would read this Mac's record and refuse its revision
    (vocaro-engineering#156)."""
    command = (recover_command(item["key"], item.get("revision", 0), host=host, toolkit=toolkit)
               if toolkit is not None or host else item["command"])
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


def device_folder(udid):
    """The folder CoreSimulator keeps a simulator in on this Mac, as Path, whether or not it exists."""
    return Path.home() / DEVICES / udid


def absent_hold(key, udid, simulators, pools, item):
    """Why release_hold must leave an entry whose simulator simctl here does not list, or None when the
    simulator is shown not to exist on this Mac: simctl listed other devices, so that its listing was a real
    one; CoreSimulator keeps no folder for the UDID; the entry is a pool member's or warmed template's by
    name; and its expected release has passed. recoverable has already shown its recorded processes gone by
    PID and start. A simulator deleted while held, as `xcrun simctl delete` or a reinstall does, can then be
    held by nothing, and a hand recovery was the only way out (vocaro/queuekeeper#195)."""
    unlisted = f"simctl here does not list {udid}"
    if not simulators: return f"{unlisted}, and lists no device at all, so its absence shows nothing"
    try:
        os.lstat(device_folder(udid))
        return f"{unlisted}, but its folder {device_folder(udid)} remains"
    except FileNotFoundError:
        pass
    except OSError as error:
        return f"{unlisted}, and whether its folder {device_folder(udid)} remains could not be read ({error})"
    # A template's name carries its runtime, the one simctl would have listed it on.
    template = TEMPLATE.fullmatch(key)
    if pool_of(key, pools, template["runtime"] if template else "")[0] is None:
        return (f"{unlisted}, and its name is neither a member's nor a warmed template's of the pools "
                + ", ".join(sorted(pools)))
    if item.get("early"): return f"{unlisted}, and its release is not due until {item['due']}"
    return None


def release_hold(item, entries, table, simulators, pools, stopping=(), *, discover_templates=False):
    """Why release_dead must leave one entry recoverable found, or None when this Mac proves it dead: a pool
    simulator's reservation, of the device simctl here lists under that name and shows Shutdown, or that
    absent_hold shows no longer exists on this Mac, whose run went to no other Mac, and which no other entry
    of the device still holds, with this reap's own process in the listing that shows the others gone. recoverable has already shown that every process the entry
    records is gone, by PID and start, and that its expected release has passed. A dry run passes in stopping
    the devices it would shut down first, which it judges as if they were Shutdown."""
    key, entry = item["key"], entries[item["key"]]
    udid = str(entry.get("device") or "").upper()
    if not DEVICE.fullmatch(udid) or physical_device_entry(entry):
        return "it records no simulator, and reap releases only a simulator pool's reservations"
    run = entry.get("run")
    if isinstance(run, dict) and run.get("host"):
        return f"its run went to {run['host']}, whose processes this Mac cannot check"
    device = simulators.get(udid)
    if device is None:
        why = absent_hold(key, udid, simulators, pools, item)
        if why: return why
    elif device.get("name") != key: return f"simctl lists {udid} as {device.get('name')!r}, not {key!r}"
    for other, held in entries.items():
        if other != key and str(held.get("device") or "").upper() == udid and \
                (held.get("state") != "released" or held.get("activeRun") or held.get("uncertain")):
            return f"its device is also held as {other}"
    if os.getpid() not in table:
        return "the process listing does not show this reap's own process, so it cannot show another is gone"
    if device is None: return None
    if pool_of(key, pools, device.get("runtime") or "")[0] is None:
        origin = template_provenance(device) if discover_templates else None
        if not origin or identities([origin["holder"]]) != identities([entry.get(HOLDER)]):
            return "it is neither a member nor a warmed template of the pools " + ", ".join(sorted(pools))
    if device.get("state") != "Shutdown" and udid not in stopping:
        return f"simctl shows it {device.get('state') or 'in no state'}, not Shutdown"
    if item.get("early") and entry.get(ALONE) is not True:
        return (f"its release is not due until {item['due']}, and it does not record that only its holder held it, "
                "as `qk simulator-pool take`'s does not: that command's session holds the member by its word, and a "
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
        udid = str(entry["device"]).upper()
        absent = udid not in simulators
        shown = (f"simctl listed {len(simulators)} other devices but not {udid}, and CoreSimulator kept no folder "
                 f"{device_folder(udid)}, so the simulator no longer exists on this Mac" if absent
                 else f"simctl would show {udid} Shutdown once this reap stopped it"
                 if dry_run and udid in stopping else f"simctl showed {udid} Shutdown")
        qualified = "early: nothing accounts for its owner" if item.get("early") else "its release passed"
        item.update(verified=f"reap on this Mac found {pids} no longer running, by PID and start in ps; {due}; and "
                             f"{shown}",
                    qualified=f"{qualified}, and its simulator no longer exists" if absent else qualified,
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
            print(f"queuekeeper simulator-pool: the hold log {HOLD_LOG} was not written ({error}); nothing else changed",
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
    """Shut down the simulators that are stale: `qk simulator-pool reap`, and before work is sent to another
    Mac. One goes only when it is a member of a pool or the pool's warmed template, among pools or,
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
    heavy-work admission finds no spare capacity, HEAVY_BUSY_LIMIT, the host's limits file or 90, as
    resources.busy_limit sets it (limits); its memory pressure level, None when unread; and its one-minute load,
    which the merge queue logs. The merge queue judges them as admission does (resources.judged) before it runs
    again a lane that ran here and gave no verdict."""
    try: load = round(os.getloadavg()[0], 2)
    except OSError: load = None
    sources = {}
    try: limit, directory = limits(sources)["busyLimit"], slot_directory()
    except (ValueError, RuntimeError) as error: return {"error": str(error)}
    return {"load": load, "cpuBusy": cpu_busy(directory), "busyLimit": limit, "memory": memory_level(),
            "hardware": hardware(), **limit_sources(sources)}


_hardware = None


def hardware(model=None, battery=None):
    """This Mac's model identifier (hw.model) and its kind, for the dashboard's icon: `laptop` when it has a built-in
    battery or its model names a MacBook, `desktop` for another Mac with no battery, and `unknown` when either cannot
    be read. A model alone cannot tell: Apple silicon names laptops and desktops alike MacNN,N. model and battery
    are the readings, `sysctl -n hw.model` and `ioreg -rc AppleSmartBattery -d1`, read once a process when None."""
    global _hardware
    if model is None and battery is None and _hardware is not None: return _hardware
    found_model = model if model is not None else output(["sysctl", "-n", "hw.model"])
    found_battery = battery if battery is not None else output(["ioreg", "-rc", "AppleSmartBattery", "-d1"])
    if found_model and found_model.startswith("MacBook"): kind = "laptop"
    elif found_battery is None or not found_model: kind = "unknown"
    else: kind = "laptop" if '"BatteryInstalled" = Yes' in found_battery else "desktop"
    found = {"model": found_model or None, "kind": kind}
    if model is None and battery is None: _hardware = found
    return found


def guarded_update(request):
    """update_toolkit's answer, or what kept it from answering: it rides on a dispatch's preparing connection,
    which nothing of it may end (vocaro-engineering#203)."""
    try: return update_toolkit(request)
    except Exception as error:
        return {"state": "failed", "path": request.get("path", TOOLKIT_CHECKOUT),
                "reason": f"{type(error).__name__}: {error}"}


def answer(request):
    """offload_host.py's answer to a request other than a plan, for `qk doctor`, `qk remote`,
    `qk remote setup`, `qk remote foreground`, `devices recover` and `qk keychain recover`. A census before a dispatch can
    also name repositories to advertise, and then says what each advertises and which Git does so."""
    if "toolchain" in request: return toolchain(request["toolchain"])
    if "join" in request: return join(request["join"])
    if "keychain" in request: return keychain(request["keychain"])
    if "foreground" in request: return foreground()
    if "markForeground" in request:
        try: return {"mac": mac_name(), **mark_foreground(request["markForeground"] is True)}
        except (OSError, ValueError) as error: return {"mac": mac_name(), "error": str(error)}
    # The Farm tab asks for health beside pressure; the merge queue, which reads pressure between runs, does not.
    if "pressure" in request: return {**pressure(), **({"health": host_health()} if request.get("health") else {})}
    if "capacity" in request:
        options = ({"before": request["requestStarted"]} if "requestStarted" in request else
                   {"before": time.time() - request["requestAge"]} if "requestAge" in request else {})
        # The place the asking work holds in this Mac's queue (place_holder) is no request queued ahead of it.
        if isinstance(request.get("own"), str): options["own"] = request["own"]
        return capacity(diagnostic=request["capacity"] == "diagnostic", **options)
    if "admissionLog" in request: return admission_log(request["admissionLog"])
    if "waitLog" in request: return wait_log(request["waitLog"])
    if "admissionsNow" in request:
        return admissions_now() if request["admissionsNow"] == NOW_SCHEMA else \
            {"error": f"this Mac's toolkit reads admissionsNow version {NOW_SCHEMA}, not {request['admissionsNow']!r}"}
    if "screen" in request: return screen()
    if "sandbox" in request: return sandbox_check()
    if "leftovers" in request or "device" in request:
        reaped = reap(request["leftovers"], **request["reap"]) if "reap" in request else None
        # The routine cleanup there also stops what it proves an ended run left (stop_leftovers), before the census;
        # `qk doctor`, a diagnostic, asks only what it would stop (leftoverStops "list").
        if reaped is not None and "leftovers" in request:
            reaped["processes"] = stop_leftovers(request["leftovers"], dry_run=bool(
                request["reap"].get("dry_run") or request.get("leftoverStops") == "list"))
        # A census that fails says why: it must never stop the work it is printed before.
        try: found = leftovers(request["leftovers"]) if "leftovers" in request else device_users(request["device"])
        except Exception as error: found = {"error": f"{type(error).__name__}: {error}"}
        if reaped is not None: found["reap"] = reaped
        if request.get("advertise"):
            found.update(git=output(["git", "--version"]),
                         advertised={path: advertise(Path.home() / path) for path in request["advertise"]})
        if request.get("toolkitCheckout"): found["toolkitCheckout"] = guarded_update(request["toolkitCheckout"])
        # The folders a project's plugins load from, of which a dispatch's pinned checkouts are worktrees (place_siblings).
        if request.get("siblingCheckouts"):
            found["siblingCheckouts"] = [guarded_update(each) for each in request["siblingCheckouts"]]
        return found
    if "toolkitCheckout" in request:
        return {"toolkitCheckout": guarded_update(request["toolkitCheckout"]),
                **({"missingSiblings": missing_siblings(request["siblingFolders"])} if "siblingFolders" in request else {})}
    return moved_request(request)


def moved_request(request):
    """The refusal of a request in MOVED_REQUESTS, which is never run as a plan: in the words each one's older caller
    reads a failure from, its error, problem and failed."""
    moved = next(key for key in MOVED_REQUESTS if key in request)
    refusal = (f"this Mac's toolkit no longer answers {moved!r} in offload_host.py: {MOVED_REQUESTS[moved]} answers "
               "it with a host program of its own; use the core and its plugins from the same Queuekeeper revision")
    return {"error": refusal, "problem": refusal, "failed": refusal}


def git(*arguments, cwd=None):
    result = subprocess.run(["git", *arguments], cwd=cwd, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f"git {' '.join(arguments)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def changes(checkout):
    """Tracked changes and nonignored untracked files, which a run left in a checkout."""
    return git("status", "--porcelain", "--untracked-files=normal", cwd=checkout) if checkout.exists() else ""


def altered(checkout, commit):
    """The tracked paths a run left different from commit in checkout, the one placed there, with "HEAD" first when it
    moved off commit: what the merge queue refuses of a gate's lane, since it pushes commit, not the tree the lane
    left (Worker.gate). Files Git does not track, such as build products, are no change."""
    files = [name for name in git("diff", "--name-only", "--no-renames", "-z", commit, cwd=checkout).split("\0") if name]
    return (["HEAD"] if git("rev-parse", "HEAD", cwd=checkout) != commit else []) + files


def gather_changes(checkout, path):
    """Write to path, as a binary-safe patch against the commit the checkout holds, the changes a command left in
    its files that Git does not ignore: tracked edits and deletions, and new untracked files no .gitignore names,
    staged in a temporary index as remote.snapshot stages a tree. Then restore the checkout to that commit, so that
    the next run can claim it (claim passes over a checkout with changes), and return the paths changed. Ignored
    files, such as the command's build products, stay as they are. The sending Mac applies the patch to its own
    tree (`qk admit`; the owner's decision, 2026-11-07). A failure raises before the checkout
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
    # The patch first, under .build/queuekeeper, which every project ignores, so the clean
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


def changed_files(left):
    """The files git status's porcelain lines name, each once by its last component, a rename by its new name,
    for a line that says why a checkout is passed over."""
    names = []
    for line in left.splitlines():
        name = Path(line[3:].split(" -> ")[-1].strip('"')).name
        if name and name not in names: names.append(name)
    return names


def unusable(count, directory, busy, passed):
    """Why no checkout of directory can be claimed while some are passed over, passed holding the files each of
    those a run left changed: how many are in use, how many are passed over and which files they hold, so that
    the owner sees that cleaning them is what is needed (vocaro/queuekeeper#44)."""
    names = []
    for files in passed:
        names.extend(name for name in files if name not in names)
    shown = ", ".join(names[:3]) + (f" and {len(names) - 3} more" if len(names) > 3 else "")
    verb = lambda number: "is" if number == 1 else "are"
    return (f"{busy} of {count} checkouts of {directory} {verb(busy)} in use and {len(passed)} {verb(len(passed))} "
            f"passed over, changed by a run ({shown})")


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
    started, reported = time.monotonic(), None
    reported_changes = set()
    record = json.dumps({"pid": os.getpid(), "start": output(["ps", "-p", str(os.getpid()), "-o", "lstart="])})
    order, distances = nearest(base, count, directory, commit)
    if slot is not None: order = [slot]
    while not _stop.is_set():
        changed, passed = 0, []
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
            passed.append(changed_files(left))
            if index not in reported_changes:
                say(f"passing over {checkout}, which a run left with changes:\n{left}")
                reported_changes.add(index)
        if changed == count:
            repository = base.parents[1] / "repositories" / f"{directory}.git"
            raise Refusal(f"every checkout under {base} has changes a run left",
                          f"inspect and remove them, each with `git --git-dir {repository} worktree remove --force PATH`")
        slot_name = f"checkout slot {slot} of {directory}"
        # A slot a run left changed is no busy one, so the line says so and names its files (vocaro/queuekeeper#44).
        busy = f"{slot_name} is in use{holding(base, slot)}" if slot is not None else \
            unusable(count, directory, count - changed, passed) if passed else f"every checkout of {directory} is in use"
        if not wait:
            raise Refusal(busy, busy=True)
        waited = int(time.monotonic() - started)
        if time.monotonic() - started >= timeout:
            raise Refusal(f"no checkout of {directory} came free in {timeout}s{': ' + busy if passed else ''}"
                          if slot is None else
                          f"{slot_name} did not come free in {timeout:g}s{holding(base, slot)}", busy=True)
        if reported is None or waited - reported >= REPORT_SECONDS:
            say(f"waiting ({waited}s): {busy}")
            reported = waited
        time.sleep(1)
    return None


def local_url(url):
    """Whether a Git remote URL names files on its own Mac, as a path or file:// does, and so names
    another checkout, or nothing, on another Mac. Git reads a URL with no scheme as a path unless a
    colon comes before any slash, as in scp's host:path (vocaro-engineering#78)."""
    if "://" in url: return url.lower().startswith("file://")
    return ":" not in url.split("/", 1)[0]


def pins_root(home=None):
    """Where this Mac keeps its pinned checkouts: PINS under home when given, else where PINS_DIRECTORY names, else
    PINS under the account's home."""
    if home is not None: return Path(home) / PINS
    return Path(os.environ[PINS_DIRECTORY]) if os.environ.get(PINS_DIRECTORY) else Path.home() / PINS


def pin_head(checkout):
    """The commit a pinned checkout is at, or None when it is none: a detached worktree's HEAD, read from its
    private Git directory without running Git, as every qk command under a pinned run reads it (pins.verify)."""
    try:
        line = (Path(checkout) / ".git").read_text().strip()
        if not line.startswith("gitdir:"): return None
        return (Path(checkout) / line[len("gitdir:"):].strip() / "HEAD").read_text().strip()
    except OSError:
        return None


def pin_git(clone, *arguments, environment=None, timeout=120):
    """Git in clone, a Mac's own checkout of a folder a project's plugins load from: its output, stripped. It
    never prompts, and a failure or a timeout is a RuntimeError saying git's own words."""
    variables = {key: value for key, value in (os.environ if environment is None else environment).items()
                 if key not in GIT_LOCATION}
    variables.update(GIT_TERMINAL_PROMPT="0",
                     GIT_SSH_COMMAND=variables.get("GIT_SSH_COMMAND") or "ssh -o BatchMode=yes -o ConnectTimeout=10")
    try:
        done = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(clone), *arguments],
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, env=variables, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"`git {' '.join(arguments)}` in {clone} did not end within {timeout}s")
    if done.returncode:
        said = " ".join((done.stderr or done.stdout).split())
        raise RuntimeError(f"`git {' '.join(arguments)}` in {clone} failed: {said or f'exit {done.returncode}'}")
    return done.stdout.strip()


def pin_path(name, commit, home=None):
    """Where the checkout of name at commit is: NAME/COMMIT/NAME under pins_root. It ends in the folder's own name,
    since tools name a folder by its last component: SwiftPM calls a local package by it, and Xcode then sought
    apple-engineering's products under a commit's name and found none (vocaro-engineering#497)."""
    return pins_root(home) / name / commit / name


def pinned_checkout(clone, name, commit, home=None, fetch=True, environment=None):
    """The one checkout of name at commit that every run on this Mac pinned to it loads from, made when it is not
    there yet: ~/Development/queuekeeper-pins/NAME/COMMIT/NAME (pin_path), a detached worktree of clone, this Mac's
    own checkout of name, with its files read-only (vocaro-engineering#497). Nothing is sent: a commit the clone lacks is fetched
    from its origin, all of it first and then the commit by name, when fetch allows.

    A run loads from it while others make, use and prune checkouts of other commits beside it, and nothing ever
    moves it to another commit, so what a lane loads cannot change under it. The file beside it, COMMIT.ready, says
    it is whole: it is written last, under the folder's lock, so no run reads a checkout another is still making, and
    its time is each use's, by which an unused one is pruned (stale_pins). One left half-made is made again.
    A RuntimeError or OSError says why it could not be made."""
    folder = pins_root(home) / name
    checkout, ready = pin_path(name, commit, home), folder / f"{commit}{PIN_READY}"
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if ready.is_file() and pin_head(checkout) == commit:
            with contextlib.suppress(OSError): os.utime(ready)
            return checkout
        with contextlib.suppress(OSError): ready.unlink()

        def has():
            found = subprocess.run(["git", "-C", str(clone), "cat-file", "-e", f"{commit}^{{commit}}"],
                                   stdin=subprocess.DEVNULL, capture_output=True)
            return found.returncode == 0
        if not has() and fetch:
            # Everything its origin's branches hold first, which most often brings the commit; then the commit by
            # name, for one that only another ref there holds.
            with contextlib.suppress(RuntimeError):
                pin_git(clone, "fetch", "--no-tags", "--quiet", "origin", environment=environment,
                        timeout=UPDATE_FETCH_SECONDS)
            if not has():
                pin_git(clone, "fetch", "--no-tags", "--quiet", "origin", commit, environment=environment,
                        timeout=UPDATE_FETCH_SECONDS)
        if not has(): raise RuntimeError(f"{clone} does not hold {commit}")
        # Whatever is in the commit's folder goes: one left half-made, or a checkout of another layout.
        if checkout.parent.is_symlink(): checkout.parent.unlink()
        elif checkout.parent.exists():
            subprocess.run(["git", "-C", str(clone), "worktree", "remove", "--force", "--force", str(checkout)],
                           stdin=subprocess.DEVNULL, capture_output=True)
            shutil.rmtree(checkout.parent, ignore_errors=True)
        checkout.parent.mkdir()
        pin_git(clone, "worktree", "prune")
        pin_git(clone, "worktree", "add", "--detach", "--quiet", str(checkout), commit)
        if pin_head(checkout) != commit: raise RuntimeError(f"{checkout} is not at {commit}")
        for directory, _folders, files in os.walk(checkout):
            for file in files:
                path = Path(directory) / file
                with contextlib.suppress(OSError):
                    if not path.is_symlink(): path.chmod(path.stat().st_mode & ~0o222)
        ready.write_text(f"{clone}\n")
    return checkout


def stale_pins(home=None, days=PIN_DAYS, now=None):
    """The pinned checkouts no run has used for days, as (checkout, its ready file, this Mac's checkout it is a
    worktree of, or None when its ready file does not say): what `qk disk` lists and remove_pin reclaims. One with no ready
    file, left half-made, counts from its own time. A ready file with no commit's folder beside it, which a folder
    removed by hand or by another tool leaves (vocaro/queuekeeper#360), is listed too, however recent: pinned_checkout
    writes one only last, under the folder's lock, beside a whole checkout, so no run can be making it."""
    now, found = time.time() if now is None else now, []
    for held in sorted(pins_root(home).glob("*/*")):
        if held.name.endswith(PIN_READY) and not held.is_symlink() and held.is_file():
            commit = held.parent / held.name[:-len(PIN_READY)]
            if not commit.exists() and not commit.is_symlink():
                try: clone = Path(held.read_text().strip())
                except OSError: clone = None
                found.append((commit / held.parent.name, held, clone))
            continue
        if not held.is_dir() or held.is_symlink(): continue
        checkout, ready = held / held.parent.name, held.with_name(f"{held.name}{PIN_READY}")
        try: used = (ready if ready.is_file() else held).stat().st_mtime
        except OSError: continue
        if now - used < days * 24 * 60 * 60: continue
        try: clone = Path(ready.read_text().strip())
        except OSError: clone = None
        found.append((checkout, ready, clone))
    return found


def remove_pin(checkout, ready, clone, days=PIN_DAYS, now=None):
    """Remove one pinned checkout stale_pins found, under its folder's lock, unless a run has used it since: its ready
    file, or its folder when it has none, is newer than days. A ready file with no commit's folder beside it is
    deleted alone; and when a run made that commit's checkout after stale_pins looked, it is in use and stays.
    Returns whether it removed anything."""
    with (checkout.parent.parent / "lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        held = checkout.parent
        if not held.exists() and not held.is_symlink():
            with contextlib.suppress(OSError): ready.unlink()
            return True
        now = time.time() if now is None else now
        try: used = (ready if ready.is_file() else held).stat().st_mtime
        except OSError: used = None
        if used is not None and now - used < days * 24 * 60 * 60: return False
        with contextlib.suppress(OSError): ready.unlink()
        if clone is not None and clone.is_dir():
            subprocess.run(["git", "-C", str(clone), "worktree", "remove", "--force", "--force", str(checkout)],
                           stdin=subprocess.DEVNULL, capture_output=True)
        shutil.rmtree(checkout.parent, ignore_errors=True)
        if clone is not None and clone.is_dir():
            subprocess.run(["git", "-C", str(clone), "worktree", "prune"], stdin=subprocess.DEVNULL, capture_output=True)
    return True


def place_siblings(slot, siblings, environment=None, home=None):
    """Put beside a dispatched checkout, in its slot, the folders its project's plugins load from, each at the
    commit the sending Mac pinned for the run ({"name", "commit"}; pins.dispatched): a link to this Mac's one
    checkout of that folder at that commit (pinned_checkout), made from this Mac's own ~/Development/NAME, which is
    not sent. It is no longer a link to that folder itself, which every connection fast-forwards and which a landing
    mid-run therefore changed under a lane's later commands (vocaro-engineering#497). Whatever an earlier layout
    left under the name goes first.

    A folder sent with no commit, which the sending Mac could not pin, is a link to this Mac's own
    ~/Development/NAME as it stands, and is in no pin set.

    Returns the run's pin set here, for QUEUEKEEPER_SIBLINGS: {NAME: {"commit", "root"}}, with the "variable" the
    sending Mac names for a folder that code finds by one. A folder this Mac has no
    checkout of, or a commit it holds none of and cannot fetch, is said and given an "error" in place of a checkout,
    and qk then ends before any lane as Queuekeeper that could not start, saying it (pins.verify)."""
    pins = {}
    for sibling in siblings:
        name, commit = sibling["name"], sibling.get("commit")
        clone, link = (Path.home() if home is None else Path(home)) / "Development" / name, Path(slot) / name
        if commit is None:
            # A folder the sending Mac had no commit to pin, as one that is no Git checkout there: this Mac's own,
            # as it stands.
            if not clone.is_dir():
                say(f"this Mac has no {clone} for the project's plugin to load from")
                continue
            if not (link.is_symlink() and link.resolve() == clone.resolve()):
                if link.is_symlink(): link.unlink()
                elif link.exists(): shutil.rmtree(link)
                link.symlink_to(clone, target_is_directory=True)
                say(f"{link} is this Mac's {clone}, as it stands: the sending Mac pinned no commit of it")
            continue
        try:
            if not clone.is_dir():
                raise RuntimeError(f"this Mac has no {clone}, its own checkout, to take it from")
            checkout = pinned_checkout(clone, name, commit, home=home, environment=environment)
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            say(f"no checkout of {name} at {commit[:12]} for the project's plugin to load from: {error}")
            pins[name] = {"commit": commit, "root": str(pin_path(name, commit, home)), "error": str(error)}
            continue
        if not (link.is_symlink() and link.resolve() == checkout.resolve()):
            if link.is_symlink(): link.unlink()
            elif link.exists(): shutil.rmtree(link)
            link.symlink_to(checkout, target_is_directory=True)
            say(f"{link} is {name} at {commit[:12]}, {checkout}")
        pins[name] = {"commit": commit, "root": str(checkout),
                      **({"variable": sibling["variable"]} if sibling.get("variable") else {})}
    return pins


def missing_siblings(names, home=None):
    """Those of names, the folders a project's plugins load from (config.sibling_folders), that this Mac has no
    ~/Development/NAME of for place_siblings to take a pinned checkout from, which `qk doctor` asks before any dispatch finds it
    (vocaro/queuekeeper#117). It only looks: nothing is fetched or created."""
    home = Path.home() if home is None else Path(home)
    return [name for name in names if not (home / "Development" / name).is_dir()]


def place(repository, checkout, commit, ref, origin=None):
    """Check commit out, detached, in a persistent worktree whose ignored inputs and builds stay.

    Only the toolkit names an origin: a heavy lane's policy refresh fetches it. Every checkout of the
    repository shares its origin, so one naming a path on the sending Mac is refused before it is
    written: here it names another checkout, or nothing, and every run's refresh would fail."""
    if origin and local_url(origin):
        raise Refusal(f"the toolkit's origin is {origin}, a path on the sending Mac, from which no checkout here "
                      "can fetch the policy",
                      "set the sending checkout's origin to https://github.com/vocaro/queuekeeper.git and "
                      "send the work again")
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
    """Give the run's git another Mac's credential file and no other helper.

    SwiftPM clones private dependencies with git, and an SSH session cannot unlock the keychain
    that osxkeychain, Xcode's git default, reads. The variables reach only this run, so the
    owner's own git configuration on another Mac is unchanged."""
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
    from this one; but whatever runs `queuekeeper` straight from it, such as a recovery recoverable_line names, runs
    its code, which nothing else here updates (owner's decision, 2026-11-05: "it should be auto-updated by
    the queuekeeper scripts"). It is fast-forwarded, without hooks, only as this Mac's shared checkout follows a
    landing (merge_queue's follow, vocaro-engineering#104): when it is on main, has no changed or untracked
    path, holds no commit origin/main lacks, is behind, and no process of this account runs from it (its
    working directory or an argument lies in it; running_from). Otherwise it is left as it is, with the
    reason, which is its owner's to decide: state "release" (detached at a release tag, named by "tag", where an
    install from a release keeps it), "missing", "left" (another branch, a detached HEAD,
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
    root = home / request.get("root", "Development/queuekeeper-farm")
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
        if branch is None:
            # A Mac installed from a release checks out its tag (vocaro/queuekeeper#351): that is where it belongs,
            # and the owner moves it to the next release. The pattern is git_state.RELEASE_TAG's, which this file,
            # run on its own on another Mac, cannot import.
            tags, _ = run("tag", "--points-at", "HEAD")
            releases = [tag for tag in (tags or "").split() if re.fullmatch(r"v\d+\.\d+\.\d+", tag)]
            if releases:
                return ended("release", tag=max(releases, key=lambda tag: tuple(map(int, tag[1:].split(".")))))
            return ended("left", f"its HEAD is detached at {head[:12]}, not on main")
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


# The status with which the device setup command says that the record itself refuses (Devices.remote_setup).
SETUP_REFUSED = 2


def missing_module(command, sources):
    """The module a `-m MODULE` command names when none of sources holds it, or None."""
    if len(command) < 2 or command[0] != "-m": return None
    parts = command[1].split(".")
    for source in sources:
        base = Path(source).joinpath(*parts)
        if base.with_name(base.name + ".py").is_file() or (base / "__main__.py").is_file(): return None
    return command[1]


def defer_devices(kit, mac, command=(), toolkit=None):
    """Make this Mac's device record defer physical devices to mac's, the sending Mac's.

    A paired iPhone or iPad is reachable from the Macs, so it is reserved in one record only;
    this one then refuses to reserve it. A record that defers them to another Mac, or holds one,
    is a configuration problem for the owner, so the work does not start.

    The command is the sending Mac's, and runs in the toolkit it sent (toolkit, the plan's), another version
    when a checkout that is behind sends origin/main (remote.followed). A toolkit without the command's module,
    or a command that fails otherwise than by the record's refusal, is no finding about the record, and the
    refusal says so: a sender one landing before the plugins left the core ran `-m queuekeeper.apple.devices`
    in a toolkit that held queuekeeper_apple.devices, and its refusal sent the owner to a record that nothing
    had read (vocaro/queuekeeper#155)."""
    if not command: return
    manifest = kit / "queuekeeper-workspace.json"
    sources = [kit / "src/python"]
    if manifest.is_file():
        sources.extend(kit / item["source"] for item in json.loads(manifest.read_text()).values())
    spelled = shlex.join(["python3", *command])
    absent = missing_module(command, sources)
    if absent:
        sent = f" at {toolkit['commit'][:12]}" if toolkit else ""
        if toolkit and toolkit.get("checkoutHead"):
            sent += f", origin/main's, from a checkout at {toolkit['checkoutHead'][:12]}"
        raise Refusal(f"the toolkit sent{sent} has no module {absent}, which the sending Mac's device setup runs "
                      f"(`{spelled}`): the sending Mac's Queuekeeper and the toolkit it sent are different versions, "
                      "and no device record was read or changed",
                      "bring the sending Mac's Queuekeeper checkout to the toolkit it sends, which it follows once "
                      "nothing runs from it, and send the work again")
    try:
        deferred = subprocess.run([sys.executable, *command], capture_output=True,
                                  text=True, env={**os.environ, "PYTHONPATH": os.pathsep.join(map(str, sources)), **NO_BYTECODE},
                                  timeout=60)
    except subprocess.TimeoutExpired:
        deferred = subprocess.CompletedProcess([], 1, "", "it did not finish in 60s")
    if deferred.returncode:
        problem = deferred.stderr.strip().splitlines()[-1:] or [f"exit {deferred.returncode}"]
        if deferred.returncode != SETUP_REFUSED:
            raise Refusal(f"its device setup (`{spelled}`) failed with status {deferred.returncode}, which is not "
                          f"its device record's refusal: {problem[0]}",
                          "repair what that error names in the toolkit sent or on another Mac; nothing here says "
                          "that its device record is misconfigured")
        raise Refusal(f"its device record cannot defer physical devices to {mac}: {problem[0]}",
                      "decide which Mac's record holds physical devices and resolve that in another Mac's record")
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


MEMBER_DEVICE = re.compile(r"(?:iPhone|iPad)(?:-[A-Za-z0-9]+)*")
# The simulator pool and the project lane scripts report these stages. The rest of each
# line belongs in the returned member log, including any device identifier or paths.
MEMBER_STAGE_LINES = (
    ("Taking", re.compile(rb"Taking: a ([A-Za-z0-9-]+)(?: [(]| member\b)")),
    ("TOOK", re.compile(rb"TOOK ([A-Za-z0-9-]+) [(]")),
    ("Testing", re.compile(rb"Testing: ([A-Za-z0-9-]+), ")),
    ("Passed", re.compile(rb"Passed: ([A-Za-z0-9-]+), ")),
    ("Failed", re.compile(rb"Failed: ([A-Za-z0-9-]+)(?:, | [(])")),
    ("Unmeasured", re.compile(rb"Unmeasured: ([A-Za-z0-9-]+)(?:, | [(])")),
)
MEMBER_STAGES = frozenset(stage for stage, _pattern in MEMBER_STAGE_LINES)
MEMBER_LINE_LIMIT = 4096


def device_stage(line):
    """The stage and kind of device described by a recognized simulator log line."""
    for stage, pattern in MEMBER_STAGE_LINES:
        if (match := pattern.match(line)) is None: continue
        device = match[1].decode("ascii")
        if len(device) <= 80 and MEMBER_DEVICE.fullmatch(device): return stage, device
        break
    return None


def open_member_log(path):
    """Open an expected regular member file, without following a replacement symlink or blocking on a pipe."""
    if path.parent.is_symlink(): return None
    try: descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError: return None
    if stat.S_ISREG(os.fstat(descriptor).st_mode): return os.fdopen(descriptor, "rb")
    os.close(descriptor)
    return None


def follow_members(directory, paths, done):
    """Send only described device stages from member logs that this dispatch is already bringing back."""
    candidates = {path.stem: directory / path for entry in paths
                  if (path := Path(entry)).parent == Path("lanes") and path.suffix == ".log"}
    opened, overlong = {}, set()
    with contextlib.ExitStack() as stack:
        while True:
            finished = done.is_set()
            for name, path in candidates.items():
                stream = opened.get(name)
                if stream is None:
                    if (stream := open_member_log(path)) is None: continue
                    opened[name] = stack.enter_context(stream)
                # Bound work per pass so one noisy member cannot crowd out the rest.
                for _ in range(128):
                    offset = stream.tell()
                    line = stream.readline(MEMBER_LINE_LIMIT)
                    if not line.endswith(b"\n"):
                        if len(line) == MEMBER_LINE_LIMIT:
                            overlong.add(name)
                            continue
                        if line and not finished: stream.seek(offset)
                        break
                    if name in overlong:
                        overlong.discard(name)
                        continue
                    if (report := device_stage(line)) is not None:
                        stage, device = report
                        emit(b"M", json.dumps({"member": name, "stage": stage, "device": device}).encode())
            if finished: break
            done.wait(0.2)


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


def timing_state(path):
    """The lane's live timing record beside its wait record at path (lane_timing.LIVE), to send back, or None."""
    try:
        data = Path(str(path) + ".timing").read_bytes()
        record = json.loads(data) if len(data) <= 65536 else None
    except (OSError, ValueError): return None
    return record if isinstance(record, dict) and (isinstance(record.get("words"), str)
                                                    or record.get("state") in ("baseline", "started")) else None


def progress_state(path):
    """A bounded snapshot of exact live reporter identities beside the lane's wait record."""
    records = []
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode): return []
            data = stream.read(65537)
        if len(data) > 65536: return []
        source = json.loads(data)
        namespace = source.get("progressRun") if isinstance(source, dict) else None
        if not isinstance(namespace, str) or not re.fullmatch(r"[0-9a-f]{32}", namespace): return []
        for index, candidate in enumerate((Path(str(path) + ".progress") / namespace).iterdir()):
            if index >= 257 or len(records) >= 128: break
            if candidate.suffix != ".json" or candidate.is_symlink(): continue
            try:
                fd = os.open(candidate, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
                with os.fdopen(fd, "rb") as stream:
                    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode): continue
                    data = stream.read(65537)
                if len(data) > 65536: continue
                record = json.loads(data)
                when = record.get("observedAt")
                if record.get("phase") != "running" or not isinstance(when, (int, float)) or not 0 <= time.time() - when <= 15:
                    continue
                pid = record.get("reporter")
                if type(pid) is int and pid > 0: records.append(record)
            except (OSError, ValueError, AttributeError, subprocess.SubprocessError): pass
    except (OSError, ValueError): pass
    if not records: return []
    try:
        found = subprocess.run(["ps", "-p", ",".join(map(str, sorted({r["reporter"] for r in records}))),
                                "-o", "pid=,lstart="], capture_output=True, text=True, timeout=1,
                               stdin=subprocess.DEVNULL)
        identities = {int(parts[0]): " ".join(parts[1:]) for line in found.stdout.splitlines()
                      if len(parts := line.split()) == 6 and parts[0].isdigit()}
        return [r for r in records if identities.get(r["reporter"]) == r.get("reporterStart")]
    except (OSError, ValueError, subprocess.SubprocessError): return []


def relay_wait(path, done, work, timing_owner=None):
    """Send the lane's wait record back as it changes, as `W` frames, and that a wait whose process is gone is over
    (lane_wait.relayed). work is whether this step is the lane's own: that the provision lane before it was admitted
    says nothing of the lane, so it does not clear a wait that goes on across offers. Nor does the step's own end: a
    run that declined leaves its wait, whose process is gone by then, and the dispatcher's next state replaces it."""
    last, last_progress, last_work = None, [], None
    while True:
        finished = done.is_set()
        state = wait_state(path)
        if work:
            fresh_progress = [] if finished else progress_state(path)
            if fresh_progress or last_progress:
                state = {**(state or {}), "progress": fresh_progress}
            last_progress = fresh_progress
            timing = timing_state(path)
            if timing is not None:
                began = timing.pop("startedMonotonic", None)
                reporter = timing.pop("timingReporter", None)
                if timing.get("state") != "started":
                    state = {**(state or {}), "timing": timing}
                # Only the dispatched qk process can attest to its work. Old slot records, provision and nested
                # commands can share this path, and cannot establish its start.
                if not finished and timing_owner is not None and reporter == timing_owner[0] \
                        and type(began) in (float, int) and timing_owner[1] <= began <= (now := time.monotonic()):
                    elapsed = int(now - began) // WORK_OBSERVATION_SECONDS * WORK_OBSERVATION_SECONDS
                    state = {**(state or {}), "workObservation": {"lane": timing.get("lane"), "elapsedSeconds": elapsed}}
            live_work = state.get("workObservation") if state else None
            if last_work is not None and live_work is None:
                state = {**(state or {"timing": None}), "workObservation": None}
            last_work = live_work
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


def step(command, directory, environment, lock, log, wait=None, work=False, launches=None,
         member_logs=None):
    """Run one queuekeeper command in directory; a stop sends it SIGTERM, which it carries out as a supervised stop. wait is
    the path of the lane's wait record, sent back as it changes (relay_wait). launches, the slot's Launches, samples
    what launchd starts from the slot while it runs, and once more when it has ended."""
    if log is not None: log.unlink(missing_ok=True)
    started = time.monotonic()
    child = subprocess.Popen(command, cwd=directory, env=environment, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True,
                             pass_fds=(lock,))
    done = threading.Event()
    threads = [threading.Thread(target=relay, args=(child.stdout,), daemon=True)]
    if log is not None: threads.append(threading.Thread(target=follow, args=(log, done), daemon=True))
    if member_logs is not None:
        threads.append(threading.Thread(target=follow_members, args=(*member_logs, done), daemon=True))
    if wait is not None:
        threads.append(threading.Thread(target=relay_wait, args=(wait, done, work, (child.pid, started)), daemon=True))
    for thread in threads: thread.start()
    forwarded, killed, sampled = None, False, time.monotonic()
    while child.poll() is None:
        if launches is not None and time.monotonic() - sampled >= LAUNCH_SAMPLE_SECONDS:
            launches.sample()
            sampled = time.monotonic()
        if _stop.is_set() and forwarded is None:
            reason = _stop_details.get("reason", "stop requested")
            say(f"{reason}; sending SIGTERM to the run, which stops its command and leaves admission")
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
    if launches is not None: launches.sample()
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
                    request_stop(f"nothing received from the dispatcher for {silence}s")
                    write_frame(b"O", f"{mac_name()}: nothing from the dispatcher for {silence}s; stopping this run, "
                                      "which leaves admission, and sending no result".encode())
                    return
                if not select.select([0], [], [], min(left, WATCH_SECONDS))[0]: continue
            data = os.read(0, 65536)
            if not data: break
            heard = time.monotonic()
            *lines, pending = (pending + data).split(b"\n")
            for word in lines:
                if word == b"stop": request_stop("the dispatcher asked to stop the run")
                elif word == b"attached": _attached.set()
                elif word == b"received": _received.set()
    except OSError:
        pass
    connection_ended()


def stop_signal(number, _frame):
    """The Python signal handler sees the signal number, with no information about the sender."""
    reason = f"the dispatch host process received {signal.Signals(number).name} (sender unknown)"
    if number == signal.SIGHUP: connection_ended(reason)
    else: request_stop(reason)


def main(plan):
    global _journal
    offer_seconds = plan.get("admissionOfferSeconds")
    offer_deadline = time.monotonic() + offer_seconds if offer_seconds is not None else None
    # sshd's hangup when the connection goes, which is no stop of a run that keeps a journal.
    for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(number, stop_signal)
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
    # queuekeeper command, whose own receipt tells its queue's wait from its run.
    phases = {}
    result = {"started": time.time(), "exitStatus": None, "stopped": False,
              "host": identity(plan.get("developerDir")), "phases": phases}
    if "requestAge" in plan:
        result["requestStarted"] = plan.get("requestStarted", result["started"] - plan["requestAge"])
    timed = lambda name, began: phases.__setitem__(name, round(time.monotonic() - began, 3))
    # Until the first command starts, a failure is a refusal: nothing ran. The dispatcher may run
    # the work on its own Mac instead only when the refusal is busy.
    begun, back = False, None
    try:
        expected, found = plan.get("xcode"), result["host"]["xcode"]
        if expected and found != expected:
            version = lambda text: " ".join(text.split())
            raise Refusal(f"the Xcode at {plan['developerDir']} is {version(found or 'missing')}, not the dispatcher's "
                          f"{version(expected)}", "install this Mac's Xcode at that path on another Mac, "
                          "or select with DEVELOPER_DIR an Xcode "
                          "the Macs have at the same path")
        root, project, toolkit = Path.home() / plan["root"], plan["project"], plan.get("toolkit")
        screen_declined(plan)
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
        # Before anything runs in the slot, so that what launchd starts from it is recorded as this run's.
        launches = Launches(base, index)
        checkout = base / str(index) / project["directory"]
        began = time.monotonic()
        place(root / project["repository"], checkout, project["commit"], project["ref"], project.get("origin"))
        kit = checkout
        if toolkit is not None:
            kit = base / str(index) / toolkit["directory"]
            place(root / toolkit["repository"], kit, toolkit["commit"], toolkit["ref"], toolkit.get("origin"))
        fetching = dict(os.environ)
        if (root / "git-credentials").is_file(): credentials(fetching, root / "git-credentials")
        pins = place_siblings(base / str(index), plan.get("siblings", []), fetching)
        timed("place", began)
        result.update(checkout=str(checkout), slot=index)
        if pins: result["siblings"] = pins
        say(f"{checkout} at {project['commit'][:12]}")
        environment = {**os.environ, "QUEUEKEEPER_ROOT": str(kit), **NO_BYTECODE}
        # The run's pin set, which every qk command of it loads the project's plugins by (pins.held).
        # An empty one too: a qk run here is a command of the sending Mac's run, and pins nothing of its own.
        if "siblings" in plan: environment[PIN_VARIABLE] = json.dumps(pins)
        if "requestAge" in plan:
            environment["QUEUEKEEPER_ADMISSION_REQUEST_STARTED"] = str(result["requestStarted"])
        # The place the work holds in this Mac's queue while it waits (place_holder), which its admissions here pass.
        if isinstance(plan.get("heldPlace"), str): environment[PLACE_VARIABLE] = plan["heldPlace"]
        if plan.get("deviceRecord"): defer_devices(kit, plan["deviceRecord"], plan.get("deviceSetup", []), toolkit)
        # The reservations, held in the sending Mac's record, that this run may use (devices.LEASE).
        if plan.get("lease"): environment["QUEUEKEEPER_DEVICE_LEASE"] = json.dumps(plan["lease"])
        if plan.get("developerDir"): environment["DEVELOPER_DIR"] = plan["developerDir"]
        # A receipt the work writes here says that it ran on another Mac, sent from this Mac (remote.DISPATCHER).
        if plan.get("dispatcher"): environment["QUEUEKEEPER_DISPATCHED_FROM"] = plan["dispatcher"]
        # The lanes' timing history marked stale on the sending Mac (lane_timing.STALE_VARIABLE).
        if isinstance(plan.get("timingStale"), dict): environment["QUEUEKEEPER_TIMING_STALE"] = json.dumps(plan["timingStale"])
        # Where the work comes from on the sending Mac, which this Mac's admissions name (resources.ORIGIN).
        if isinstance(plan.get("origin"), dict): environment["QUEUEKEEPER_ADMISSION_ORIGIN"] = json.dumps(plan["origin"])
        if (root / "git-credentials").is_file(): credentials(environment, root / "git-credentials")
        qk = [sys.executable, str(kit / "bin/qk"), "--project", str(checkout)]
        # The provision lane is admitted here as every lane is, and runs here, never offered on. Offered work that
        # declines when it cannot start now has it decline too, rather than wait for a slot with the checkout held.
        provision = [*qk, "run", "--here", *(["--if-free"] if plan.get("decline") else []), "provision"]
        # With a pinned folder that could not be had, no qk starts, and the work's own start says so.
        unpinned = any("error" in pin for pin in pins.values())
        steps = [("provision", provision, checkout / ".build/queuekeeper/provision/run.log",
                  checkout)] if plan.get("provision") and not unpinned else []
        # `qk admit --verify` runs its command in the checkout's directory that matches the sender's.
        steps.append(("work", [*qk, *plan["work"]], checkout / plan["log"] if plan.get("log") else None,
                      checkout / plan["subdirectory"] if plan.get("subdirectory") else checkout))
        # The lane's wait record (lane_wait) is shared by provision and work.
        wait = checkout / plan["wait"] if plan.get("wait") else None
        if wait is not None:
            environment["QUEUEKEEPER_LANE_WAIT"] = str(wait)
            environment["QUEUEKEEPER_LANE_PROGRESS_RUN"] = os.urandom(16).hex()
            wait.unlink(missing_ok=True)
        # Where `qk run --if-free` or `qk admit --if-free` writes why its admission declined (cli.admitted_here),
        # which the decline below says instead of naming only the queue.
        declined = checkout / DECLINED_NOTE if plan.get("decline") else None
        if declined is not None:
            environment[DECLINED] = str(declined)
            if plan.get("admissionOffer"): environment[ADMISSION_OFFER] = plan["admissionOffer"]
            if offer_deadline is not None: environment[ADMISSION_OFFER_DEADLINE] = str(offer_deadline)
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
            if declined is not None: declined.unlink(missing_ok=True)
            begun, began = True, time.monotonic()
            work_environment = environment
            if stage == "work" and "cleanupSeconds" in (plan.get("lease") or {}):
                work_environment = {**environment, "QUEUEKEEPER_DEVICE_WINDOW": environment["QUEUEKEEPER_DEVICE_LEASE"]}
            member_logs = back if stage == "work" and returns and plan.get("memberStages") else None
            result.update(stage=stage, exitStatus=step(command, directory, work_environment, lock, log, wait,
                                                       stage == "work", launches, member_logs))
            timed(stage, began)
            if result["exitStatus"] != 0: break
        result["stopped"] = _stop.is_set()
        # What `qk admit`'s command changed in the checkout goes back as a patch, however it ended, and the
        # checkout is restored for the next run (gather_changes).
        if plan.get("changes") and result.get("stage") == "work":
            try:
                result["changes"] = {"files": gather_changes(checkout, checkout / plan["changes"])}
            except (OSError, RuntimeError) as error:
                result["changes"] = {"error": str(error)}
        # What a lane changed of the tracked tree it tested, which a gate must not push untested (Worker.gate).
        elif result.get("stage") == "work" and result["exitStatus"] == 0:
            try: result["altered"] = altered(checkout, project["commit"])
            except (OSError, RuntimeError) as error: result["altered"] = {"error": str(error)}
        receipt = checkout / plan["receipt"]
        # A command that failed before it wrote a receipt leaves the previous run's in place.
        fresh = receipt.is_file() and receipt.stat().st_mtime >= result["started"]
        if fresh:
            result["receipt"] = json.loads(receipt.read_text())
        # `qk run --if-free` declined the lane, or its provision lane, before running it, and wrote no receipt.
        if plan.get("decline") and result.get("stage") in ("provision", "work") and result["exitStatus"] == plan["busy"] \
                and not fresh:
            if offer := plan.get("admissionOffer"):
                if reason := declined_offer(declined, offer):
                    result.update(refused=f"its heavy-work queue cannot take the lane now: {reason}",
                                  busy=True, notStarted=offer)
                else:
                    result.update(refused="no confirmed admission outcome", busy=True)
            else:
                result.update(refused=declined_reason(declined), busy=True)
    except Refusal as refusal:
        result.update(refused=str(refusal), busy=refusal.busy, fix=refusal.fix)
    except (OSError, RuntimeError, ValueError) as error:
        result["error" if begun else "refused"] = str(error)
    finally:
        if not begun and result.get("refused") and plan.get("admissionOffer"):
            result["notStarted"] = plan["admissionOffer"]
        if result["stopped"] and "reason" in _stop_details:
            result["stopReason"] = _stop_details["reason"]
        elif (status := result["exitStatus"]) is not None and status < 0:
            try: name = signal.Signals(-status).name
            except ValueError: name = f"signal {-status}"
            result["stopReason"] = f"the run process exited by {name} (sender unknown)"
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


# A plan and a reattach are sent only what main and reattach read of this file, a blank line in place of every other
# function's (remote.plan_program, vocaro/queuekeeper#216): theirs are the only branches below that such a copy can
# take. Every other request is sent the whole file.
if __name__ == "__main__":
    request = json.loads(sys.argv[1])
    if "keepUnlocked" in request: sys.exit(keep_unlocked(request["keepUnlocked"]))
    if "reattach" in request: sys.exit(reattach(request["reattach"]))
    if {"toolchain", "join", "leftovers", "device", "keychain", "foreground", "pressure", "capacity", "admissionLog",
            "waitLog", "admissionsNow", "screen", "toolkitCheckout", "markForeground", "sandbox", *MOVED_REQUESTS} \
            & request.keys():
        print(json.dumps(answer(request)))
    else:
        main(request)
        # A write stuck on a connection nobody reads would make Python abort at exit (watch).
        if _silent.is_set(): os._exit(0)
