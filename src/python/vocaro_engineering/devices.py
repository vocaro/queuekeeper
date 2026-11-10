"""One device handoff record for the shared host, changed only under its lock.

Physical devices and reserved-pool simulators are used only under a reservation recorded here.
Sessions used to keep ad hoc JSON files in /private/tmp. Two live copies let two sessions reserve
one device in different files, unlocked read-modify-write could lose entries, and the restart
after the 2026-10-14 panic emptied /private/tmp while device work could still be running.

The record lives in the account's Application Support directory, found independently of HOME
and TMPDIR so that every session finds the same one. A change takes an exclusive lock, reads the
record fresh, refuses unless the entry is still at the revision the caller read, and replaces the
file whole. The lock covers one change, never a reservation, and the kernel drops it with its
process. Like a heavy-work guard, an entry is never released by a deadline or a vanished process:
an active run whose recorded process is gone is an orphan, kept for attended recovery. The one
exception is a simulator pool's reservation that `eng simulator-pool reap` proves dead, every process
it records gone and its simulator shut down, which reap releases as a recovery by reap: once its
expected release has passed, or before then when only its holder held it (ALONE) and nothing accounts
for its owner, no live admission or other process naming the owner or the device (offload_host.release_dead;
vocaro-engineering#156).

The mac-foreground entry reserves a Mac's screen, keyboard, pointer, focus and speech. It names no
device and, like a physical device's, records no approval (owner's decision, 2026-11-06,
vocaro-engineering#207): the record schedules it, one holder at a time, and its expected release
is its window. On this Mac, the owner's workstation, a session agrees the window with the owner
first and reserves it for that window; foreground actions are refused once the expected release
has passed. The second Mac is not a workstation: eng reserves its entry for each run without
asking (foreground.slot), and a run there may outlast its expected release. Foreground drivers
call require_foreground before acting; release waits for the processes the session recorded as
its own, and says whether the previous frontmost app was restored.

Each Mac's simulators are its own, but a paired iPhone or iPad is reachable from both Macs, so a
physical device is reserved in one record only: the Mac that sends runs to the second Mac. The
second Mac's record defers physical devices to that Mac and refuses to reserve one. A run there
uses one under a reservation held in the sending Mac's record, which `eng remote --device` carries.

A physical device's reservation records no approval (owner's decision, 2026-11-06): the record schedules
the devices, first come first served, one holder at a time, and a session that reserves one has the
owner's agreement already, which is a matter between the session and the owner. The reservation's
expected release is its window, and a bounded run stops before it.

A bounded run (`devices run KEY -- COMMAND`, BoundedRun) runs one command on physical devices the
session holds, refusing a command that names any other device, and
stops it before the reservation ends, keeping time for its termination and the session's
restoration. It records the run in the entry and never releases the device. `eng remote --device`
bounds a run on the second Mac the same way, and there a bounded run takes its reservation from the lease.

`devices take KEY` takes a paired device in turn: it joins the device's queue, waits while another
session holds it or waits ahead, and reserves it. The queue lives
beside the entries, so joining it never changes a holder's revision, and a waiter whose process is
gone drops out. While anyone waits, only the first in line may reserve the entry. A session records each app it
launches on its device (`update --launched BUNDLE`), and releasing an entry that records any says how
they were quit.

A physical device's run keeps the owner's notifications out (vocaro-engineering#144): reserving or taking one
runs the owner's silencing shortcut on it, once he has recorded making it there (`devices notifications setup`),
and releasing or recovering the entry runs the restoring one. The entry's `notifications` records each step.
The Mac reads no device's Focus, so the silencing is never confirmed, and a run without it says so
(device_notifications). `devices run` watches the device's log while its command runs, and a run a notification
reached as a banner is unmeasured and run once more; so does `eng remote --device`, from the sending Mac (#192).
"""
import argparse
import contextlib
from datetime import datetime, timedelta, timezone
import fcntl
import glob
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import subprocess
import sys
import tempfile
import time

from . import device_notifications, offload_host, processes
from .evidence import UNMEASURED
from .resources import process_start

SCHEMA = 1
OVERRIDE = "VOCARO_DEVICE_RECORD"
ABOUT = ("Device reservations on this Mac. Change entries only through "
         "`python3 -m vocaro_engineering.devices` (vocaro-engineering guides/shared-host.md).")
# Earlier ad hoc records. Status names one whose current bytes were never imported.
LEGACY = "/private/tmp/*device-handoff*.json"
UDID = re.compile(r"[0-9A-F]{8}(?:-[0-9A-F]{4,16}){1,4}|[0-9A-F]{40}")
# A physical device's hardware UDID: 8 and 16 hex digits, or 40 on older devices. A simulator's is a UUID.
PHYSICAL = re.compile(r"[0-9A-F]{8}-[0-9A-F]{16}|[0-9A-F]{40}")
# The Mac whose record holds physical devices, in a record that defers them to it: the second Mac's.
DEFERRED = "physicalDevicesRecordedOn"
# A hardware UDID as a word of uppercased text, not part of a longer run of hex digits such as a SHA-256.
NAMED_PHYSICAL = re.compile(rf"(?<![0-9A-F])(?:{PHYSICAL.pattern})(?![0-9A-F])")
# Seconds Git has to say whether forty hex digits name one of its objects, such as a commit.
OBJECT_TIMEOUT = 10
# What comes, in uppercased text, right before forty hex digits that name a code-signing identity by its
# SHA-1, as codesign, xcodebuild and security take one: a setting or option whose name ends in IDENTITY,
# such as CODE_SIGN_IDENTITY=, SIGN_IDENTITY= or --identity, or codesign's -s or --sign (#86).
SIGNING = re.compile(r"""(?:IDENTITY=|--IDENTITY\s+|\bCODESIGN\b[^;&|\n]*?\s(?:-S|--SIGN)(?:=|\s+))["']?\Z""")
# The reservations a run on the second Mac may use, held in the sending Mac's record, as JSON:
# {"record": MAC, "devices": {KEY: UDID}, "until": the reservations' end}. offload_host.py sets it for the run.
LEASE = "VOCARO_DEVICE_LEASE"
# A lease's timing report, passed only to the top-level work on the second Mac.
WINDOW = "VOCARO_DEVICE_WINDOW"
# Seconds a bounded run keeps before its reservation ends for its own end once its bound expires:
# supervision's wait for descendants that outlive the command, then its SIGTERM, settling and
# SIGKILL waits (processes.py).
TERMINATION = processes.LINGER + processes.KILL_AFTER + processes.SETTLE_WAIT + processes.KILL_WAIT
# Seconds it keeps after that, unless the caller names its own, for the session's restoration queries.
RESTORATION = 60
# Seconds below which a bounded run is refused as too short to start.
MINIMUM = 30
# A bounded run's exit status when its bound expired and the command then ended, as timeout(1)'s.
TIMED_OUT = 124
# Written only by the record's own operations, which say who changed ownership and why.
MANAGED = {"revision", "ownerChatID", "lastOwnerChatID", "previousLease", "recovery", "importedFrom",
           "importedVersions", "uncertainReason", "hostProcessStart", "approval", "ownedProcesses", "frontmost",
           "launchedApps", "appsQuit", "notifications"}
# The paired devices `take` waits in line for, by their entries' keys.
PAIRED = ("iPad", "iPhone")
# Sessions waiting in line for an entry, by its key, kept beside the entries.
QUEUES = "queues"
# The owner's word that he made the notification shortcuts on a paired device, by its entry's key, kept
# beside the entries with the device it names (device_notifications).
SETUP = "notificationSetup"
# What the entry's notifications record says of a run's silencing that let it start protected: eng ran the
# owner's shortcut, or the owner turned Do Not Disturb on himself.
SILENCED = ("requested", "owner")
# Seconds between looks at an entry while `take` waits for it.
POLL = 10
# The owner's screen, keyboard, pointer, focus and speech, held like a device.
FOREGROUND = "mac-foreground"
# The session a foreground driver acts for, as its reservation names it.
OWNER = "VOCARO_DEVICE_OWNER"
# The process that reserved a simulator pool member and holds it, {"pid", "start"}: while it runs
# as one of a live run's processes, the second Mac's census of leftover processes counts the
# member's simulator as that run's (offload_host.leftovers).
HOLDER = "holderProcess"
# Set beside HOLDER when that process alone holds the reservation, as a run's pool records it and
# `eng simulator-pool take` never does: reap may release such an entry before its expected release once
# its processes are gone and nothing accounts for its owner (offload_host.ALONE; vocaro-engineering#156).
ALONE = offload_host.ALONE
# How a foreground release left the frontmost app: restored, or no longer the session's to restore.
FRONTMOST = ("restored", "not-ours")
TIMES = ("leaseRevision", "releasedAt", "reservedAt", "leasedAt", "updatedAt")
EPOCH = datetime.min.replace(tzinfo=timezone.utc)


class Refused(RuntimeError):
    """The record refused a change; nothing was written."""


class Stale(Refused):
    """The entry changed after the caller read it."""


def default_path():
    """The account's record. A HOME or TMPDIR private to one session must not start another."""
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    if sys.platform == "darwin":
        return home / "Library/Application Support/vocaro-engineering/device-handoff.json"
    return home / ".local/state/vocaro-engineering/device-handoff.json"


def record_path():
    """VOCARO_DEVICE_RECORD names an isolated record for tests; sessions use the default."""
    return Path(os.environ[OVERRIDE]) if os.environ.get(OVERRIDE) else default_path()


def restart_emptied(path):
    path = Path(os.path.abspath(path))
    for candidate in (path, path.resolve()):
        for root in (Path("/tmp"), Path("/private/tmp")):
            if candidate == root or root in candidate.parents:
                return True
    return False


def default_owner():
    """The session a reservation names: its device-record owner, else its agent session."""
    return os.environ.get(OWNER) or os.environ.get("CODEX_THREAD_ID") or os.environ.get("CLAUDE_CODE_SESSION_ID")


def physical(udid):
    """Whether udid is a physical device's hardware UDID rather than a simulator's."""
    return isinstance(udid, str) and PHYSICAL.fullmatch(udid.upper()) is not None


def deferred_refusal(mac, key):
    return Refused(f"physical devices are reserved in {mac}'s device record, not this one: reserve {key} there and "
                   f"send the run here with `eng remote run|admit --device {key}`. Nothing was changed")


def approval_refusal(key):
    return ValueError(f"a reservation of {key} records no approval: the record only schedules it, and the owner's "
                      "agreement is between the session and the owner (owner's decision, 2026-11-06). Reserve it "
                      f"without --approved-*, until the end of the window agreed for {key}. Nothing was changed")


def now():
    return datetime.now(timezone.utc).isoformat()


def moment(value):
    """A time-zone-aware time from ISO 8601 text, or None."""
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return result if result.tzinfo else None


def report_window(window, queued=None):
    """Report the fixed device window before dispatch or after the work's admission."""
    deadline = moment(window["until"])
    left = max(0, (deadline - datetime.now(timezone.utc)).total_seconds())
    cleanup, budget = window["cleanupSeconds"], window.get("budgetSeconds")
    phase = "before dispatch" if queued is None else f"after {queued:.2f}s waiting for admission"
    message = (f"devices: {' and '.join(window['devices'])} {phase}: {left:.0f}s left in the reservation "
               f"(ends {deadline.isoformat()}, {cleanup:g}s reserved for cleanup)")
    if budget is not None and budget + cleanup > left:
        message += f". Warning: the {budget:g}s run budget plus cleanup no longer fits"
    print(message, file=sys.stderr, flush=True)


def held_window(keys, owner, record=None):
    """The window of the devices keys names that owner holds here, as report_window reads one, or None when it
    holds none of them: the earliest expected release, which is the end of the time the owner agreed, and what a
    bounded run keeps before it (BoundedRun). The record schedules, so this reads no approval (owner's decision,
    2026-11-06)."""
    if not keys or not owner: return None
    value = (record or Record()).load()
    if value.get(DEFERRED): return None
    found = {key: moment(value["devices"][key].get("expectedReleaseBy")) for key in keys
             if key in value["devices"] and condition(value["devices"][key]) == "held"
             and value["devices"][key].get("ownerChatID") == owner}
    ends = {key: deadline for key, deadline in found.items() if deadline is not None}
    if not ends: return None
    return {"devices": list(ends), "until": min(ends.values()).isoformat(),
            "cleanupSeconds": TERMINATION + RESTORATION, "budgetSeconds": None}


class WindowWait:
    """How long work on the devices of window may wait for admission (vocaro-engineering#292): until the last start
    that leaves room for cleanup and the larger of the measured time and budget.
    A diagnostic without an estimate leaves room for the shortest bounded run."""

    def __init__(self, window, measured=None, *, budget=None):
        self.window, self.measured, self.limited = window, measured, False
        self.budget = budget
        self.estimate = max(measured or 0, budget or 0) if measured is not None or budget is not None else None
        needs = max(MINIMUM, self.estimate or 0) + window["cleanupSeconds"]
        self.latest = moment(window["until"]) - timedelta(seconds=needs)

    def left(self):
        """Seconds until the last start, below zero once it has passed."""
        return (self.latest - datetime.now(timezone.utc)).total_seconds()

    def seconds(self, timeout):
        """timeout, or the shorter time left to wait."""
        left = max(self.left(), 0)
        if left < timeout: self.limited = True
        return min(timeout, left)

    def __str__(self):
        if self.budget is not None and self.estimate == self.budget:
            run = f"its full-pass budget of {self.budget:.0f}s"
        elif self.measured is not None:
            run = f"its measured {self.measured:.0f}s"
        else:
            run = f"no measured run of it, so the shortest bounded run, {MINIMUM}s"
        return (f"the window on {' and '.join(self.window['devices'])} ends {self.window['until']}, and the last start "
                f"that can finish within it is {self.latest.isoformat()}: {run}, and "
                f"{self.window['cleanupSeconds']:g}s kept for cleanup")


def condition(entry):
    """released, held, active, orphan or uncertain. Reading a condition never changes an entry."""
    if entry.get("uncertain") or entry.get("state") == "restoration-unverified":
        return "uncertain"
    if entry.get("activeRun"):
        pid, start = entry.get("hostProcessIdentifier"), entry.get("hostProcessStart")
        alive = isinstance(pid, int) and pid > 0 and start is not None and process_start(pid) == start
        return "active" if alive else "orphan"
    return "released" if entry.get("state") == "released" else "held"


def approval_ended(entry):
    """Whether the approval an older entry recorded has ended; an entry without one has none to end. No
    reservation records one since 2026-11-06."""
    if not entry.get("approval"):
        return False
    until = entry["approval"].get("until")
    if until == "released":
        return False
    deadline = moment(until)
    return deadline is None or deadline <= datetime.now(timezone.utc)


def window_ended(entry):
    """Whether an entry's expected release, the window it was reserved for, has passed or is unreadable."""
    deadline = moment(entry.get("expectedReleaseBy"))
    return deadline is None or deadline <= datetime.now(timezone.utc)


def require_window(key, entry):
    """Refuse work on key under an older reservation whose recorded approval has ended.

    An ended window releases nothing: the session stops, restores and releases."""
    if approval_ended(entry):
        until = (entry.get("approval") or {}).get("until")
        raise Refused(f"the owner's approval of {key} ended at {until}; stop, restore the owner's settings and "
                      "release")
    return entry


def running(entry):
    """The processes an entry records as its own that still run with their recorded start."""
    return [item for item in entry.get("ownedProcesses", [])
            if isinstance(item, dict) and item.get("start") and process_start(item.get("pid")) == item["start"]]


def waiting(record, key):
    """The sessions in line for key whose waiting process still runs, first first."""
    return [item for item in record.get(QUEUES, {}).get(key, [])
            if isinstance(item, dict) and item.get("start") and process_start(item.get("pid")) == item["start"]]


def _owned(key, entry, owner):
    if entry is None:
        raise Refused(f"{key} has no entry; nothing was changed")
    if entry.get("ownerChatID") != owner or entry.get("state") == "released":
        raise Refused(f"{key} is not held by {owner} (owner {entry.get('ownerChatID')}, state "
                      f"{entry.get('state')}); nothing was changed")


def _latest(entry):
    times = [moment(entry.get(name)) for name in TIMES]
    return max([value for value in times if value is not None] or [EPOCH])


def _content(entry):
    return {key: value for key, value in entry.items()
            if key not in ("revision", "importedFrom", "importedVersions", "uncertain", "uncertainReason")}


def _merge(current, incoming):
    """One entry from the record's version and an imported one, discarding neither; None if known.

    A held version stays current over a released one, so a live reservation is preserved; among
    equals the later activity wins. Two held versions leave the entry uncertain for recovery.
    """
    if current is None:
        return {"revision": 1, **incoming}
    earlier = current.get("importedVersions", [])
    if any(_content(incoming) == _content(version) for version in [current, *earlier]):
        return None
    present = {key: value for key, value in current.items() if key != "importedVersions"}
    held = [version for version in (present, incoming) if condition(version) != "released"]
    winner = max(held or [present, incoming], key=_latest)
    loser = incoming if winner is present else present
    merged = {"revision": current.get("revision", 0) + 1,
              **{key: value for key, value in winner.items() if key != "revision"}}
    merged["importedVersions"] = [*earlier, loser]
    if len(held) > 1 or current.get("uncertain"):
        merged["uncertain"] = True
        merged["uncertainReason"] = current.get("uncertainReason") or (
            "imported records hold this device for different runs; resolve ownership before use")
    return merged


class Record:
    def __init__(self, path=None, timeout=30):
        self.path = Path(os.path.abspath(path or record_path()))
        if restart_emptied(self.path):
            raise ValueError(f"{self.path} is emptied by a restart; the device record is {default_path()}")
        self.lock = self.path.with_name(self.path.name + ".lock")
        self.timeout = timeout

    def load(self):
        """The record as last replaced. Reading needs no lock: every change replaces the file whole."""
        try:
            value = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {"vocaroDeviceHandoff": SCHEMA, "about": ABOUT, "revision": 0, "devices": {}, "imports": []}
        except ValueError as error:
            raise Refused(f"{self.path} cannot be read ({error}); nothing was changed") from error
        if not isinstance(value, dict) or value.get("vocaroDeviceHandoff") != SCHEMA or not isinstance(value.get("devices"), dict):
            raise Refused(f"{self.path} is not a version {SCHEMA} device record; nothing was changed")
        return value

    def entry(self, key):
        return self.load()["devices"].get(key)

    @contextlib.contextmanager
    def _exclusive(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(self.lock, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            started = time.monotonic()
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() - started >= self.timeout:
                        raise TimeoutError(f"{self.lock} stayed locked for {self.timeout}s; nothing was changed") from None
                    time.sleep(0.02)
            yield
        finally:
            os.close(descriptor)

    def _replace(self, record):
        record["revision"] = record.get("revision", 0) + 1
        record["updatedAt"] = now()
        staged = None
        try:
            with tempfile.NamedTemporaryFile("w", dir=self.path.parent, prefix=".device-handoff-", delete=False) as stream:
                staged = Path(stream.name)
                json.dump(record, stream, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(staged, self.path)
        finally:
            if staged is not None:
                staged.unlink(missing_ok=True)
        directory = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def _change(self, key, revision, change):
        """Compare-and-replace one entry: refuse unless it is still at the revision the caller read."""
        with self._exclusive():
            record = self.load()
            current = record["devices"].get(key)
            found = current.get("revision", 0) if current is not None else 0
            if found != revision:
                raise Stale(f"{key} is at revision {found}, not {revision}; read it again. Nothing was changed")
            entry = change(current, record)
            entry.pop("revision", None)
            record["devices"][key] = {"revision": found + 1, **entry}
            self._replace(record)
            return record["devices"][key]

    def reserve(self, key, *, owner, revision, purpose, release_by, device=None, title=None, details=None,
                approval=None):
        """Claim a released or new entry. The previous lease is kept, one generation deep.

        No reservation records an approval: approval names the --approved-* options only to refuse them."""
        details = dict(details or {})
        if not owner or not purpose:
            raise ValueError("a reservation names its owner and purpose")
        if MANAGED & details.keys():
            raise ValueError(f"the record writes {', '.join(sorted(MANAGED & details.keys()))} itself")
        deadline = moment(release_by)
        if deadline is None or deadline <= datetime.now(timezone.utc):
            raise ValueError("the expected release must be a future ISO 8601 time with a time zone")
        if device is not None and not UDID.fullmatch(device):
            raise ValueError("name the device by its canonical uppercase UDID, never a name or 'booted'")
        if key == FOREGROUND and device is not None:
            raise ValueError(f"{FOREGROUND} names no device: it is the owner's screen, keyboard, pointer, focus and speech")
        if approval is not None:
            raise approval_refusal(key)
        json.dumps(details)

        def change(current, record):
            if current is not None and condition(current) != "released":
                raise Refused(f"{key} is {condition(current)} by {current.get('ownerChatID')}; obtain its "
                              "release or attended recovery first. Nothing was changed")
            identifier = device or (current or {}).get("device")
            if record.get(DEFERRED) and physical(identifier):
                raise deferred_refusal(record[DEFERRED], key)
            line = waiting(record, key)
            if line and line[0]["owner"] != owner:
                raise Refused(f"{len(line)} waiting for {key}, first {line[0]['owner']} for {line[0].get('purpose')}; "
                              f"wait your turn with `devices take {key}`. Nothing was changed")
            if line:
                record[QUEUES][key] = line[1:]
            for other, entry in record["devices"].items():
                same = isinstance(entry.get("device"), str) and identifier and entry["device"].upper() == identifier.upper()
                if other != key and same and condition(entry) != "released":
                    raise Refused(f"the same device is {condition(entry)} as {other}; nothing was changed")
            entry = {**details, "device": identifier, "ownerChatID": owner, "ownerChatTitle": title,
                     "state": "reserved", "activeRun": False, "purpose": purpose, "reservedAt": now(),
                     "expectedReleaseBy": release_by}
            if current is not None:
                entry["previousLease"] = {name: value for name, value in current.items() if name != "previousLease"}
            return {name: value for name, value in entry.items() if value is not None}
        return self._change(key, revision, change)

    def _line(self, key, change):
        """Change key's queue under the lock, writing the record only when it changed."""
        with self._exclusive():
            record = self.load()
            before = record.get(QUEUES, {}).get(key, [])
            after = change(waiting(record, key))
            if after != before:
                queues = record.setdefault(QUEUES, {})
                if after: queues[key] = after
                else: queues.pop(key, None)
                self._replace(record)
            return after

    def join(self, key, *, owner, purpose, pid=None):
        """Put owner at the end of key's line, waiting as pid, by default this process; its place."""
        pid = os.getpid() if pid is None else pid
        start = process_start(pid)
        if start is None:
            raise Refused(f"process {pid} is not running; nothing was changed")

        def change(line):
            if any(item["owner"] == owner for item in line):
                raise Refused(f"{owner} already waits for {key}; nothing was changed")
            return [*line, {"owner": owner, "purpose": purpose, "pid": pid, "start": start, "joinedAt": now()}]
        return len(self._line(key, change))

    def leave(self, key, *, owner):
        """Take owner out of key's line, if it is there."""
        self._line(key, lambda line: [item for item in line if item["owner"] != owner])

    def take(self, key, *, owner, purpose, release_by, timeout, title=None, details=None, poll=POLL, said=None):
        """Reserve key in turn, first come first served, waiting up to timeout seconds in its line.

        The entry already records the device, from its first reservation, so the take names no other. An
        orphaned or uncertain entry needs attended recovery; waiting leaves the line on any end."""
        if key not in PAIRED:
            raise ValueError(f"take waits only for {' or '.join(PAIRED)}; reserve {key} with `devices reserve`")
        said = said or (lambda text: print(text, file=sys.stderr, flush=True))
        place = self.join(key, owner=owner, purpose=purpose)
        said(f"devices: {owner} waits for {key}, place {place} in line")
        deadline, announced = time.monotonic() + timeout, None
        try:
            while True:
                value = self.load()
                entry = value["devices"].get(key)
                if entry is None or not physical(entry.get("device")):
                    raise Refused(f"{key} records no physical device yet; its first reservation, `devices reserve "
                                  f"{key} --device UDID`, records the device. Nothing was changed")
                state = condition(entry)
                line = waiting(value, key)
                if state == "released" and line and line[0]["owner"] == owner:
                    try:
                        return self.reserve(key, owner=owner, revision=entry["revision"], purpose=purpose,
                                            release_by=release_by, title=title, details=details)
                    except Stale:
                        continue
                if state in ("orphan", "uncertain"):
                    raise Refused(f"{key} is {state}, held by {entry.get('ownerChatID')} for {entry.get('purpose')}; "
                                  "it needs attended recovery (`devices recover`) first")
                ahead = entry.get("ownerChatID") if state != "released" else line[0]["owner"] if line else None
                if ahead != announced:
                    said(f"devices: waiting for {key}, " + (
                        f"held by {ahead} for {entry.get('purpose')}, expected release {entry.get('expectedReleaseBy')}"
                        if state != "released" else f"next in line {ahead}"))
                    announced = ahead
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"{key} stayed held or claimed by {ahead} for {timeout:g}s; nothing was reserved")
                time.sleep(poll)
        finally:
            self.leave(key, owner=owner)

    def update(self, key, *, owner, revision, own=(), launched=(), **changes):
        """Change the caller's own held entry. An active run names the process whose end ends it.

        own adds running processes, by PID, that the entry's release then waits for; launched adds the
        bundle identifiers of apps the session launched on its device, which its release says it quit."""
        if MANAGED & changes.keys():
            raise ValueError(f"the record writes {', '.join(sorted(MANAGED & changes.keys()))} itself")
        if changes.get("state") == "released":
            raise ValueError("release with release(), which records the outcome and restoration")
        if "uncertain" in changes and not changes["uncertain"]:
            raise ValueError("uncertainty ends only by release or recovery, which record the verification")
        if changes.get("activeRun") and "hostProcessIdentifier" not in changes:
            raise ValueError("an active run records hostProcessIdentifier, the process whose end ends it")
        if changes.get("hostProcessIdentifier") is not None:
            changes["hostProcessStart"] = process_start(changes["hostProcessIdentifier"])
            if changes["hostProcessStart"] is None:
                raise Refused("the run's process is not running; record its terminal result instead")
        owned = []
        for pid in own:
            start = process_start(pid) if type(pid) is int and pid > 0 else None
            if start is None:
                raise Refused(f"process {pid} is not running; nothing was changed")
            owned.append({"pid": pid, "start": start})
        json.dumps(changes)

        def change(current, record):
            _owned(key, current, owner)
            if record.get(DEFERRED) and physical(changes.get("device")):
                raise deferred_refusal(record[DEFERRED], key)
            entry = {**current, **changes}
            if owned:
                entry["ownedProcesses"] = [*current.get("ownedProcesses", []), *owned]
            if launched:
                entry["launchedApps"] = list(dict.fromkeys([*current.get("launchedApps", []), *launched]))
                # An app the session says it launched is the session's to account for, even one eng opened too.
                said = entry.get("notifications") or {}
                if set(launched) & set(said.get("opened", [])):
                    entry["notifications"] = {**said, "opened": [app for app in said["opened"] if app not in launched]}
            return entry
        return self._change(key, revision, change)

    def notifications(self, key, *, owner, revision, value, launched=()):
        """Record in the caller's own entry what was done to the owner's notifications on its device: value's
        fields join the entry's notifications. launched adds apps eng opened there, as update's does, and notes them
        as eng's, which a release does not ask the session about (vocaro-engineering#292)."""
        json.dumps(value)

        def change(current, _record):
            _owned(key, current, owner)
            entry = {**current, "notifications": {**current.get("notifications", {}), **value}}
            if launched:
                entry["launchedApps"] = list(dict.fromkeys([*current.get("launchedApps", []), *launched]))
                opened = list(dict.fromkeys([*entry["notifications"].get("opened", []), *launched]))
                entry["notifications"] = {**entry["notifications"], "opened": opened}
            return entry
        return self._change(key, revision, change)

    def notification_setup(self, key, *, by, at, words=None):
        """Record the owner's word that he made the notification shortcuts on key's device, which the entry
        names; the setup recorded. A later device under the key needs his word again."""
        if not by or moment(at) is None or moment(at) > datetime.now(timezone.utc):
            raise ValueError("the setup records who made the shortcuts and when: a past ISO 8601 time with a time zone")
        with self._exclusive():
            record = self.load()
            entry = record["devices"].get(key) or {}
            if record.get(DEFERRED):
                raise deferred_refusal(record[DEFERRED], key)
            if not physical(entry.get("device")):
                raise Refused(f"{key} records no physical device, so there is none to have the shortcuts. "
                              "Nothing was changed")
            setup = {"device": entry["device"].upper(), "by": by, "at": at, "silence": device_notifications.SILENCE,
                     "restore": device_notifications.RESTORE, **({"words": words} if words else {})}
            record.setdefault(SETUP, {})[key] = setup
            self._replace(record)
            return setup

    def release(self, key, *, owner, revision, outcome, restoration, frontmost=None, apps_quit=None):
        """Release the caller's own entry with its terminal outcome and restoration checks.

        It waits for the processes the entry records as its own. Releasing mac-foreground also says
        whether the previous frontmost app was restored or the foreground was no longer the session's.
        An entry that records apps the session launched says how they were quit (apps_quit).
        Explicit unresolved uncertainty requires attended recover(), even when no live PID is known."""
        if not outcome or not restoration:
            raise ValueError("a release records the terminal outcome and the restoration checks")
        if key == FOREGROUND and frontmost not in FRONTMOST:
            raise ValueError(f"releasing {FOREGROUND} records frontmost: restored, or not-ours when the session "
                             "no longer owned the foreground")
        if frontmost is not None and frontmost not in FRONTMOST:
            raise ValueError(f"frontmost is one of {', '.join(FRONTMOST)}")

        def change(current, _record):
            _owned(key, current, owner)
            if current.get("activeRun"):
                raise Refused(f"{key} has an active run; record its confirmed terminal result first. "
                              "Nothing was changed")
            if current.get("uncertain"):
                raise Refused(f"{key} has unresolved uncertainty: "
                              f"{current.get('uncertainReason') or 'termination is not verified'}. "
                              "Use attended recovery after verifying the work stopped. Nothing was changed")
            alive = running(current)
            if alive:
                raise Refused(f"{key}'s own processes {', '.join(str(item['pid']) for item in alive)} are still "
                              "running; quit them first. Nothing was changed")
            opened = (current.get("notifications") or {}).get("opened", [])
            theirs = [app for app in current.get("launchedApps", []) if app not in opened]
            if theirs and not apps_quit:
                raise Refused(f"{key} records launched apps {', '.join(theirs)}: quit them on the "
                              "device and say how (--apps-quit). Nothing was changed")
            if (current.get("notifications") or {}).get("restore") == "pending":
                raise Refused(f"{key}'s run silenced the owner's notifications, which are not yet put back: "
                              f"`devices release` runs the restoring shortcut first, as `devices notifications "
                              f"restore {key}` does. Nothing was changed")
            entry = {name: value for name, value in current.items() if name not in ("uncertain", "uncertainReason")}
            entry.update({"state": "released", "activeRun": False, "ownerChatID": None,
                          "lastOwnerChatID": owner, "releasedAt": now(), "run": outcome,
                          "restoration": restoration})
            if frontmost is not None:
                entry["frontmost"] = frontmost
            if apps_quit:
                entry["appsQuit"] = apps_quit
            return entry
        return self._change(key, revision, change)

    def recover(self, key, *, by, revision, verified, unchecked=None, users=None, notifications=None):
        """Attended recovery of an entry whose owner cannot release it, once its work has stopped.

        An entry whose run went to the second Mac also waits until nothing there may still use its
        device, as users (remote.device_users) lists it, unless unchecked records why that host
        cannot be checked, which is the owner's decision. notifications, the restoring shortcut's
        outcome (restore_for_recovery), joins the entry's notifications."""
        if not by or not verified:
            raise ValueError("recovery records who verified the device and what they verified")
        checked = self._checked_offload_host(key, revision, unchecked, users)

        def change(current, _record):
            state = condition(current) if current is not None else "absent"
            if state in ("absent", "released"):
                raise Refused(f"{key} is {state}; there is nothing to recover")
            if state == "active":
                raise Refused(f"{key}'s run process {current.get('hostProcessIdentifier')} is still running; "
                              "recovery waits for it. Nothing was changed")
            alive = running(current)
            if alive:
                raise Refused(f"{key}'s own processes {', '.join(str(item['pid']) for item in alive)} are still "
                              "running; recovery waits for them. Nothing was changed")
            entry = {name: value for name, value in current.items() if name not in ("uncertain", "uncertainReason")}
            entry.update({"state": "released", "activeRun": False, "ownerChatID": None,
                          "lastOwnerChatID": current.get("ownerChatID"), "releasedAt": now(),
                          "recovery": {"by": by, "at": now(), "verified": verified, "condition": state,
                                       "state": current.get("state"),
                                       "uncertainReason": current.get("uncertainReason"),
                                       **({"offload": checked} if checked else {})}})
            if notifications:
                entry["notifications"] = {**current.get("notifications", {}), **notifications}
            return entry
        return self._change(key, revision, change)

    def _checked_offload_host(self, key, revision, unchecked, users):
        """For a recovery of a physical device whose run went to the second Mac: that host's check,
        or a refusal while something there may still use the device. None for any other entry, and
        for one that recovery refuses anyway, which it then does under the lock."""
        current = self.entry(key)
        run = (current or {}).get("run")
        host = run.get("host") if isinstance(run, dict) else None
        if not host or not physical(current.get("device")) or current.get("revision") != revision or \
                condition(current) in ("released", "active") or running(current):
            return None
        if unchecked:
            return {"host": host, "unchecked": unchecked}
        if users is None:
            from .remote import device_users as users
        since = moment(run.get("startedAt"))
        try:
            still = users(host, current["device"].upper(), since.timestamp() if since else None)
        except RuntimeError as error:
            raise Refused(f"{key}'s run went to {host}, which could not be checked: {error}. Recovery waits for it, "
                          "or records why it cannot be checked (--offload-unchecked). Nothing was changed") from None
        if still:
            raise Refused(f"{key}'s run went to {host}, where these may still use its device; recovery waits for "
                          "them. Nothing was changed:\n  " + "\n  ".join(still))
        return {"host": host, "checkedAt": now()}

    def defer(self, mac, *, replace=False):
        """Defer physical devices to mac's record, after which this record refuses to reserve one.

        The second Mac's record defers them to the Mac that sends it runs; None ends a deferral.
        Changing which Mac holds them is the owner's decision, so another Mac needs replace. It
        returns whether the record changed."""
        with self._exclusive():
            record = self.load()
            current = record.get(DEFERRED)
            if current == mac:
                return False
            if current is not None and mac is not None and not replace:
                raise Refused(f"this record defers physical devices to {current}, not {mac}; which Mac's record holds "
                              "them is the owner's decision (`devices defer MAC --replace`). Nothing was changed")
            held = [key for key, entry in record["devices"].items()
                    if physical(entry.get("device")) and condition(entry) != "released"]
            if mac is not None and held:
                raise Refused(f"{', '.join(held)} holds a physical device in this record; release or recover it "
                              f"before deferring physical devices to {mac}. Nothing was changed")
            if mac is None:
                record.pop(DEFERRED)
            else:
                record[DEFERRED] = mac
            self._replace(record)
            return True

    def import_legacy(self, paths, *, by):
        """Merge earlier ad hoc records once each, preserving every entry; the files stay untouched."""
        if not by:
            raise ValueError("name the importing session")
        sources = []
        for path in map(Path, paths):
            data = path.read_bytes()
            try:
                value = json.loads(data)
            except ValueError as error:
                raise Refused(f"{path} cannot be read ({error}); nothing was imported") from error
            entries = value.get("devices") if isinstance(value, dict) else None
            if not isinstance(entries, dict) or not all(isinstance(entry, dict) for entry in entries.values()):
                raise Refused(f"{path} has no devices object of entries; nothing was imported")
            sources.append((path, data, value, path.stat().st_mtime))
        report = []
        with self._exclusive():
            record = self.load()
            imports = record.setdefault("imports", [])
            for path, data, value, modified in sources:
                digest = hashlib.sha256(data).hexdigest()
                if any(item.get("sha256") == digest for item in imports):
                    report.append(f"{path}: already imported")
                    continue
                counts = {"added": 0, "merged": 0, "already present": 0}
                for key, legacy in value["devices"].items():
                    incoming = dict(legacy)
                    if "revision" in incoming:
                        incoming["legacyRevision"] = incoming.pop("revision")
                    incoming["importedFrom"] = {"path": str(path), "sha256": digest}
                    current = record["devices"].get(key)
                    merged = _merge(current, incoming)
                    if merged is None:
                        counts["already present"] += 1
                        continue
                    counts["added" if current is None else "merged"] += 1
                    record["devices"][key] = merged
                imports.append({"path": str(path), "sha256": digest, "bytes": len(data),
                                "modifiedAt": datetime.fromtimestamp(modified, timezone.utc).isoformat(),
                                "importedAt": now(), "importedBy": by, "devices": list(value["devices"]),
                                "fields": {name: field for name, field in value.items() if name != "devices"}})
                report.append(f"{path}: " + ", ".join(f"{count} {name}" for name, count in counts.items()))
            # One device held under two keys, from different records, is unclear ownership.
            holders = {}
            for key, entry in record["devices"].items():
                if isinstance(entry.get("device"), str) and condition(entry) != "released":
                    holders.setdefault(entry["device"].upper(), []).append(key)
            for keys in holders.values():
                for key in keys if len(keys) > 1 else []:
                    entry = record["devices"][key]
                    if not entry.get("uncertain"):
                        entry.update(revision=entry.get("revision", 0) + 1, uncertain=True,
                                     uncertainReason=f"the same device is also held as {', '.join(k for k in keys if k != key)}")
            if len(report) > sum(line.endswith("already imported") for line in report):
                self._replace(record)
        return report


def require_foreground(owner, record=None):
    """Refuse foreground work unless owner holds mac-foreground, and on this Mac within its window.

    Drivers call it before acting, on either Mac. On this Mac the window is the reservation's expected
    release, which the session agreed with the owner. On the second Mac, whose record defers physical
    devices, eng holds the entry for the run and names it in VOCARO_DEVICE_OWNER (foreground.slot), and
    the run may outlast its expected release."""
    if not owner:
        raise Refused(f"name the session whose {FOREGROUND} reservation covers this work (--owner or {OWNER})")
    value = (record or Record()).load()
    entry = value["devices"].get(FOREGROUND)
    state = condition(entry) if entry is not None else "released"
    if state == "released":
        raise Refused(f"nobody holds {FOREGROUND}; " + ("eng reserves it for each run" if value.get(DEFERRED) else
                      "before foreground work, agree a window with the owner and reserve it until the window ends"))
    if entry.get("ownerChatID") != owner:
        raise Refused(f"{FOREGROUND} is held by {entry.get('ownerChatID')} for {entry.get('purpose')}, expected "
                      f"release {entry.get('expectedReleaseBy')}; wait for its release")
    if state in ("orphan", "uncertain"):
        raise Refused(f"{FOREGROUND} is {state}; it needs attended recovery before foreground work")
    if not value.get(DEFERRED) and window_ended(entry):
        raise Refused(f"the window agreed for {FOREGROUND} ended at {entry.get('expectedReleaseBy')}; stop, restore "
                      "the owner's settings and release")
    return require_window(FOREGROUND, entry)


def git_object(word, root=None):
    """Whether the Git of root's checkout, by default the current directory's, knows word as an object.
    A device's UDID never is one; Git that cannot answer in time knows nothing."""
    from .remote import environment
    try:
        return subprocess.run(["git", "cat-file", "-e", word], cwd=root, capture_output=True, env=environment(),
                              timeout=OBJECT_TIMEOUT).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def named(words, known=(), root=None):
    """The device identifiers words name, uppercased: every hardware UDID, and each of known that appears.

    Forty hex digits are an older device's UDID, a commit's whole SHA-1 or a code-signing identity's,
    so such a word that a signing identity's setting or option takes (SIGNING), or that the Git of
    root's checkout knows as an object (git_object), names no device."""
    text = " ".join(map(str, words)).upper()
    found = {match.group() for match in NAMED_PHYSICAL.finditer(text) if len(match.group()) != 40 or not (
        SIGNING.search(text, 0, match.start()) or git_object(match.group(), root))}
    found.update(udid.upper() for udid in known if isinstance(udid, str) and udid and udid.upper() in text)
    return found


class BoundedRun:
    """One command on physical devices the session holds, stopped before their reservations end and
    never releasing them (guides/shared-host.md, Bounded device runs).

    Here the reservations come from this Mac's record: the session holds each with no run active,
    at revision when one is given. Each entry records the run: active,
    with this process as the run's and the command as the entry's own process, then its outcome.
    On the second Mac, whose record defers physical devices, they come from the lease the dispatch
    carries (LEASE), and the dispatch records the run in the sending Mac's record. Every device
    identifier the command names, a hardware UDID or any device the record knows, is one it holds;
    an object of root's Git, by default the current directory's, is none (named).
    The bound is the reservations' remaining time, less what the run keeps for its termination
    and the session's restoration, and at most limit."""

    def __init__(self, keys, owner, words, *, record=None, revision=None, limit=None, restoration=None, root=None):
        keys = list(dict.fromkeys(keys))
        if not keys:
            raise ValueError("a bounded run names the device entries it uses")
        if revision is not None and len(keys) != 1:
            raise ValueError("a revision is one entry's")
        if limit is not None and limit < MINIMUM:
            raise ValueError(f"a bounded run's limit is at least {MINIMUM}s")
        self.record, self.owner, self.limit = record or Record(), owner, limit
        self.keep = TERMINATION + (RESTORATION if restoration is None else restoration)
        # The run as its entries record it, when its bound ends, and whether the bound ended it.
        self.run, self.until, self.timed_out = None, None, False
        value = self.record.load()
        entries = value["devices"]
        if value.get(DEFERRED):
            # The dispatch records the run in the sending Mac's record, so nothing here is changed.
            self.revisions = None
            self.devices, self.deadline = self._leased(keys, value[DEFERRED])
        else:
            if not owner:
                raise ValueError(f"name the session that holds {', '.join(keys)}: --owner, or {OWNER}")
            self.revisions = {}
            self.devices, self.deadline = self._held(keys, entries, revision)
        held = set(self.devices.values())
        for udid in sorted(named(words, [entry.get("device") for entry in entries.values()], root) - held):
            holders = [key for key, entry in entries.items() if str(entry.get("device") or "").upper() == udid]
            whose = f", {' and '.join(holders)}'s device," if holders else ""
            raise Refused(f"the command names {udid}{whose} but the run holds only "
                          f"{', '.join(f'{key} ({device})' for key, device in self.devices.items())}: every device "
                          "it names is one it holds. Nothing ran")
        self.seconds()

    def _held(self, keys, entries, revision):
        devices, ends = {}, []
        for key in keys:
            entry = entries.get(key)
            state = condition(entry) if entry is not None else "absent"
            if state != "held" or entry.get("ownerChatID") != self.owner:
                holder = f" by {entry.get('ownerChatID')}" if state not in ("absent", "released") else ""
                raise Refused(f"{key}: the device record shows it {state}{holder}, not held by {self.owner} with no "
                              "run active; reserve it first. Nothing ran")
            if revision is not None and entry.get("revision", 0) != revision:
                raise Stale(f"{key} is at revision {entry.get('revision', 0)}, not {revision}: it changed after the "
                            "session verified it. Read it again. Nothing ran")
            if not physical(entry.get("device")):
                raise Refused(f"{key} holds {entry.get('device') or 'no device'}, not a physical device; simulators "
                              "come from a project's pool. Nothing ran")
            try: require_window(key, entry)
            except Refused as error: raise Refused(f"{error}. Nothing ran") from None
            until = (entry.get("approval") or {}).get("until")
            release = moment(entry.get("expectedReleaseBy"))
            if release is None:
                raise Refused(f"{key} records no expected release to bound the run by. Nothing ran")
            ends += [release] + ([] if until in (None, "released") else [moment(until)])
            devices[key] = entry["device"].upper()
            self.revisions[key] = entry.get("revision", 0)
        return devices, min(ends)

    def _leased(self, keys, mac):
        """The reservations held in mac's record that this run on the second Mac carries."""
        if not os.environ.get(LEASE):
            raise deferred_refusal(mac, keys[0])
        try:
            lease = json.loads(os.environ[LEASE])
            carried = {key: udid.upper() for key, udid in lease["devices"].items()}
            deadline = moment(lease["until"])
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            raise Refused(f"{LEASE} is not a lease of devices and their end ({error!r}). Nothing ran") from error
        if lease.get("record") != mac:
            raise Refused(f"the run's lease is from {lease.get('record')}'s record, not {mac}'s, which this record "
                          "defers physical devices to. Nothing ran")
        missing = [key for key in keys if key not in carried]
        if missing:
            raise Refused(f"the run's lease carries {', '.join(carried) or 'no device'}, not {', '.join(missing)}: "
                          f"send the run from {mac} with `eng remote run|admit --device {missing[0]}`. Nothing ran")
        if deadline is None:
            raise Refused("the run's lease names no end for its reservations. Nothing ran")
        return {key: carried[key] for key in keys}, deadline

    def seconds(self):
        """The seconds the command may run from now, refused when too few."""
        left = (self.deadline - datetime.now(timezone.utc)).total_seconds() - self.keep
        if self.limit is not None: left = min(left, self.limit)
        if left < MINIMUM:
            raise Refused(f"{' and '.join(self.devices)}'s reservation ends at {self.deadline.isoformat()}, leaving "
                          f"{max(left, 0):.0f}s for a run after the {self.keep:.0f}s kept for its termination and "
                          f"restoration; a run needs {MINIMUM}s. Nothing ran")
        return left

    def _update(self, key, **changes):
        self.revisions[key] = self.record.update(key, owner=self.owner, revision=self.revisions[key],
                                                 **changes)["revision"]

    def begin(self, run):
        """Start the bound and mark each entry's run active, with this process as the run's; the bound's seconds."""
        bound = int(self.seconds())
        self.until = datetime.now(timezone.utc) + timedelta(seconds=bound)
        self.run = {**run, "boundSeconds": bound, "stopsAt": self.until.isoformat()}
        marked = []
        try:
            for key in self.revisions or ():
                self._update(key, activeRun=True, hostProcessIdentifier=os.getpid(), run=self.run)
                marked.append(key)
        except BaseException:
            for key in marked: self._update(key, activeRun=False, run={**self.run, "outcome": "not run"})
            raise
        return bound

    def own(self, pid):
        """Record pid, which the run started, as each entry's own process: release and recovery wait for it."""
        for key in self.revisions or ():
            self._update(key, own=[pid])

    def expired(self):
        """Whether the bound begin started has passed, by the wall clock, which counts a sleep."""
        return self.until is not None and datetime.now(timezone.utc) >= self.until

    def finish(self, outcome, *, uncertain=False):
        """Record the run's outcome in each entry: inactive, or still active and uncertain when its end is
        unconfirmed, for attended recovery. The devices stay reserved."""
        self.run = {**self.run, "endedAt": now(), "outcome": outcome}
        for key in self.revisions or ():
            try:
                self._update(key, run=self.run, **({"uncertain": True} if uncertain else {"activeRun": False}))
            except (Refused, OSError, TimeoutError) as error:
                print(f"devices: {key}'s entry did not record the run's end, so it shows the run orphaned until its "
                      f"owner releases or recovers it: {error}", file=sys.stderr, flush=True)

    def execute(self, command, *, cwd=None, stdout=None, run=None, watch=None, env=None):
        """Run command under the bound, recorded in each entry, and return its exit status.

        A timeout, a stop, or a command that did not start is recorded and raised once the command
        and its observed descendants have ended. A run whose end is unconfirmed stays active. A watch
        of the device's log (device_notifications.Watch) is stopped as the command ends, and what it
        saw is recorded with the run's end as its notificationWatch."""
        command = list(command)
        try:
            timeout = self.begin({**(run or {}), "command": command, "startedAt": now()})
        except BaseException:
            if watch is not None: watch.stop()
            raise
        try:
            try:
                status = processes.supervise(command, cwd=cwd, stdout=stdout, timeout=timeout, env=env,
                                             on_start=self.own)
            finally:
                if watch is not None: self.run = {**self.run, "notificationWatch": watch.stop()}
        except processes.TimedOut as error:
            self.timed_out = True
            self.finish(str(error))
            raise
        except (processes.Interrupted, processes.NotStarted) as error:
            self.finish(str(error))
            raise
        except BaseException as error:
            self.finish(f"{error}; its end is unconfirmed, so the run stays active for attended recovery",
                        uncertain=True)
            raise
        self.finish(f"exit {status}")
        return status


class Unmeasured(Exception):
    """A run that a notification reached twice: neither its run nor its rerun is a measurement."""


def keyed(key):
    """The keys a watched run names: one key, or a sequence of them."""
    return [key] if isinstance(key, str) else list(dict.fromkeys(key))


def watched(run, key, command, extra, cwd=None, env=None):
    """Run command under run while watching key's device log, or each of several keys' devices' logs, for a
    notification presented as a banner; its exit status and what the watch saw, which the entry's run records
    (device_notifications.Watch, Together)."""
    keys = keyed(key)
    watches = {each: device_notifications.watch(run.devices[each]) for each in keys}
    watch = watches[keys[0]] if len(keys) == 1 else device_notifications.Together(watches)
    code = run.execute(command, run=extra, watch=watch, cwd=cwd, env=env)
    return code, run.run["notificationWatch"]


def watched_run(run, key, command, extra, said, cwd=None, env=None):
    """Run command under run, and once more when a notification was presented on key's device during it (#144);
    key may be several keys, whose devices are all watched (remote.Lease).

    Such a run is unmeasured: its transcript may hold the notification and VoiceOver's cursor may have moved. Its
    rerun's status is the run's; a rerun that one reaches too, or no time left for one, raises Unmeasured. A run
    whose device log could not be read is said to be unwatched and keeps its status."""
    keys = keyed(key)
    code, seen = watched(run, keys, command, extra, cwd, env)
    # What the session is told names every device watched.
    who = " and ".join(keys)
    if not seen["presented"]:
        if not seen["watched"]:
            said(f"devices: {who}'s log could not be read during the run ({seen['detail']}), so a notification that "
                 "reached it would not be seen; read the transcript for one")
        return code
    said(f"devices: unmeasured: {device_notifications.presented(seen)}. The run's transcript may hold it and "
         f"VoiceOver's cursor may have moved, so its status {code} measures nothing; running it once more")
    try:
        run.seconds()
    except Refused as error:
        said(f"devices: unmeasured, not run again: {error}")
        raise Unmeasured() from None
    first = {name: value for name, value in run.run.items() if name not in ("boundSeconds", "stopsAt")}
    code, seen = watched(run, keys, command, {**extra, "rerunOf": {**first, "measured": False}}, cwd, env)
    if seen["presented"]:
        said(f"devices: unmeasured again: {device_notifications.presented(seen)}. Not run a third time: ask the "
             f"owner to check Do Not Disturb on {who} ({device_notifications.GUIDE})")
        raise Unmeasured()
    said(f"devices: the rerun saw no notification; its status {code} is the run's")
    return code


def silence_notifications(record, key, *, owner, revision, said):
    """Silence the owner's notifications on the physical device key's entry holds, for the caller's run: run his
    silencing shortcut there when he has recorded making it on that device, or else say what to ask him.

    The entry records each step, the restore as pending before the shortcut runs, so that a run that dies
    after it still has its release or recovery run the restoring one. The Mac reads no Focus, so the result is
    never confirmed. Each request records the restore as pending again, a request after a completed restore
    too, so that the release or recovery puts back what it silenced (#144). It returns the entry; an entry still
    silenced and not yet restored, silenced by the owner himself, or holding no physical device, is left."""
    value = record.load()
    entry = value["devices"].get(key)
    _owned(key, entry, owner)
    notifications = entry.get("notifications") or {}
    done = notifications.get("silence")
    silenced = done == "owner" or (done in SILENCED and notifications.get("restore") == "pending")
    if not physical(entry.get("device")) or silenced or value.get(DEFERRED):
        return entry
    setup = value.get(SETUP, {}).get(key)
    if not setup or setup.get("device") != entry["device"].upper():
        why = (f"the owner has recorded no notification shortcuts on {key}'s device" if not setup else
               f"the owner's notification shortcuts were recorded on another device under {key}")
        # Nothing was done, so the entry stays as it was; a run on it says it is not silenced.
        said(f"devices: {key}'s notifications are not silenced: {why}. A notification can reach a session's "
             f"transcript and move VoiceOver's cursor: {device_notifications.ask(key)}")
        return entry
    entry = record.notifications(key, owner=owner, revision=revision, launched=[device_notifications.APP],
                                 value={"silence": "requesting", "restore": "pending", "at": now()})
    opened, detail = device_notifications.trigger(entry["device"], device_notifications.SILENCE)
    entry = record.notifications(key, owner=owner, revision=entry["revision"],
                                 value={"silence": "requested" if opened else "failed", "detail": detail,
                                        "confirmed": False})
    if opened:
        said(f"devices: ran the owner's {device_notifications.SILENCE!r} shortcut on {key}, which turns on Do Not "
             f"Disturb unless a Focus is already on; the Mac cannot read a device's Focus, so it is not confirmed")
    else:
        said(f"devices: {key}'s notifications are not silenced: {detail}. Retry with `devices notifications silence "
             f"{key}` once the device is unlocked and reachable, or {device_notifications.ask(key)}")
    return entry


def restore_notifications(record, key, *, owner, revision, said):
    """Put back the owner's notifications on key's device, by his restoring shortcut, when the caller's run
    silenced them; the entry. A restore that fails is recorded and said, for the owner, and the release goes on."""
    entry = record.entry(key)
    _owned(key, entry, owner)
    if (entry.get("notifications") or {}).get("restore") != "pending":
        return entry
    outcome = restore_for_recovery(key, entry, said)
    return record.notifications(key, owner=owner, revision=revision, value=outcome)


def restore_for_recovery(key, entry, said):
    """Run the owner's restoring shortcut on entry's device when its run left a restore pending; what to record
    in its notifications, or None when nothing was pending."""
    if (entry.get("notifications") or {}).get("restore") != "pending":
        return None
    opened, detail = device_notifications.trigger(entry["device"], device_notifications.RESTORE)
    if opened:
        said(f"devices: ran the owner's {device_notifications.RESTORE!r} shortcut on {key}, which turns Do Not "
             "Disturb off only if the run's silencing turned it on")
    else:
        said(f"devices: could not put back the owner's notifications on {key}: {detail}. Do Not Disturb may still "
             f"be on there: tell the owner, who can run {device_notifications.RESTORE!r} or turn it off himself")
    return {"restore": "done" if opened else "failed", "restoreDetail": detail, "restoredAt": now()}


def _notification_state(key, entry):
    """What status says of the owner's notifications on entry's physical device, or None."""
    value = entry.get("notifications") or {}
    if condition(entry) == "released":
        return (f"notifications: not put back after the last run ({value.get('restoreDetail')}); Do Not Disturb may "
                "still be on, tell the owner") if value.get("restore") in ("failed", "pending") else None
    silence = value.get("silence")
    if silence == "requested": return "notifications: silencing shortcut run, not confirmed (the Mac reads no Focus)"
    if silence == "owner": return f"notifications: Do Not Disturb turned on by {value.get('by')} at {value.get('at')}"
    return (f"notifications: not silenced ({value.get('detail') or 'nothing was done'}); "
            f"`devices notifications silence {key}`, or {device_notifications.ask(key)}")


def _describe(key, entry, line=(), window=True):
    state = condition(entry)
    parts = [f"{key}: {state}"]
    if state == "released":
        parts.append(f"last owner {entry.get('lastOwnerChatID') or entry.get('ownerChatID') or 'unrecorded'}, "
                     f"released {entry.get('releasedAt') or 'at an unrecorded time'}")
    else:
        parts.append(f"owner {entry.get('ownerChatID')}, state {entry.get('state')}, "
                     f"expected release {entry.get('expectedReleaseBy')}")
        deadline = moment(entry.get("expectedReleaseBy"))
        if deadline is not None and deadline < datetime.now(timezone.utc):
            parts.append("overdue, which releases nothing")
        if entry.get("approval"):
            approval = entry["approval"]
            parts.append(f"approved by {approval.get('by')} at {approval.get('at')} until {approval.get('until')}")
        if key == FOREGROUND and window and window_ended(entry):
            parts.append("its window has ended; foreground actions are refused")
        elif key == FOREGROUND and approval_ended(entry):
            parts.append("approval ended; foreground actions are refused")
        elif physical(entry.get("device")) and approval_ended(entry):
            parts.append("the approval an earlier reservation recorded has ended; work on the device is refused")
        alive = running(entry)
        if alive:
            parts.append(f"own processes running: {', '.join(str(item['pid']) for item in alive)}")
        if state == "orphan":
            parts.append(f"run process {entry.get('hostProcessIdentifier')} is gone or unrecorded; kept for attended recovery")
        if state == "uncertain":
            parts.append((entry.get("uncertainReason") or "termination or restoration is unverified")
                         + "; kept for attended recovery")
        if entry.get("launchedApps"):
            parts.append(f"launched apps: {', '.join(entry['launchedApps'])}")
    if physical(entry.get("device")) and _notification_state(key, entry):
        parts.append(_notification_state(key, entry))
    if line:
        parts.append("waiting: " + ", ".join(f"{item['owner']} ({item.get('purpose')})" for item in line))
    if key in PAIRED:
        parts.append(f"take it in turn with `devices take {key}`")
    parts.append(f"revision {entry.get('revision', 0)}")
    return "; ".join(parts)


def recoverable_lines(entries):
    """What to say of each held entry whose process is gone and whose release has passed. When the
    processes cannot be listed, nothing can be said of any entry, and it says that."""
    listed = offload_host.listing(["ps", "-axww", "-o", "pid=,ppid=,uid=,lstart=,args="])
    if listed is None: return ["The processes could not be listed, so no held entry that nothing keeps can be named"]
    return [f"Recoverable: {offload_host.recoverable_line(item)}"
            for item in offload_host.recoverable(entries, offload_host.process_table(listed))]


def status(record):
    value = record.load()
    source = f" (from {OVERRIDE})" if os.environ.get(OVERRIDE) else ""
    print(f"Device record {record.path}{source}: revision {value.get('revision', 0)}, updated {value.get('updatedAt', 'never')}")
    if value.get(DEFERRED):
        print(f"Physical devices are reserved in {value[DEFERRED]}'s device record; this one refuses them")
    # The owner's screen first, held or free, so a session about to take it sees who has it.
    foreground = value["devices"].get(FOREGROUND)
    print(_describe(FOREGROUND, foreground, window=not value.get(DEFERRED)) if foreground is not None else
          f"{FOREGROUND}: free; " + ("eng reserves it for each run" if value.get(DEFERRED) else
                                     "agree a window with the owner, then reserve it until the window ends"))
    for key, entry in value["devices"].items():
        if key != FOREGROUND: print(_describe(key, entry, waiting(value, key)))
    for line in recoverable_lines(value["devices"]): print(line)
    imported = {item.get("sha256") for item in value.get("imports", [])}
    for legacy in sorted(glob.glob(LEGACY)):
        try:
            digest = hashlib.sha256(Path(legacy).read_bytes()).hexdigest()
        except OSError:
            continue
        if digest not in imported:
            print(f"Legacy record {legacy} has entries not imported: import it, then stop writing it")


def _fields(pairs):
    result = {}
    for pair in pairs or []:
        name, separator, text = pair.partition("=")
        if not separator or not name:
            raise ValueError(f"--set takes FIELD=VALUE, not {pair}")
        try:
            result[name] = json.loads(text)
        except ValueError:
            result[name] = text
    return result


def _given_approval(args):
    """The owner's window the --approved-* options name, or None."""
    given = {"by": args.approved_by, "at": args.approved_at, "until": args.approved_until,
             "words": args.approval_words}
    return {name: value for name, value in given.items() if value is not None} or None


def _report(key, entry):
    print(json.dumps({"key": key, "revision": (entry or {}).get("revision", 0),
                      "condition": condition(entry) if entry else None, "entry": entry}, indent=2))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python3 -m vocaro_engineering.devices",
                                     description="Read or change the shared device handoff record, or run a "
                                     "bounded command on a physical device it reserves.")
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("path", help="print the record's location")
    commands.add_parser("status", help="every entry's condition, and legacy records not yet imported")
    show = commands.add_parser("show", help="one entry with the revision a change must name")
    show.add_argument("key")
    reserve = commands.add_parser("reserve", help="claim a released or new entry")
    reserve.add_argument("key")
    reserve.add_argument("--owner", required=True)
    reserve.add_argument("--revision", type=int, required=True)
    reserve.add_argument("--purpose", required=True)
    reserve.add_argument("--release-by", required=True)
    reserve.add_argument("--device")
    reserve.add_argument("--title")
    reserve.add_argument("--set", action="append", metavar="FIELD=JSON")
    # Refused since 2026-11-06: a reservation records no approval. Kept so the refusal can say why.
    for option in ("--approved-by", "--approved-at", "--approved-until", "--approval-words"):
        reserve.add_argument(option, help=argparse.SUPPRESS)
    update = commands.add_parser("update", help="change your own held entry")
    update.add_argument("key")
    update.add_argument("--owner", required=True)
    update.add_argument("--revision", type=int, required=True)
    update.add_argument("--set", action="append", metavar="FIELD=JSON")
    update.add_argument("--own", action="append", type=int, metavar="PID", default=[],
                        help="a process this session launched, which release waits for")
    update.add_argument("--launched", action="append", metavar="BUNDLE", default=[],
                        help="an app this session launched on the device, which release says it quit")
    take = commands.add_parser("take", help=f"wait in line for {' or '.join(PAIRED)}, first come first served, "
                               "and reserve it; prints the entry")
    take.add_argument("key", choices=sorted(PAIRED))
    take.add_argument("--owner", default=default_owner(), help=f"the session (default: {OWNER}, else the agent session)")
    take.add_argument("--purpose", required=True)
    take.add_argument("--release-by", required=True)
    take.add_argument("--title")
    take.add_argument("--set", action="append", metavar="FIELD=JSON")
    take.add_argument("--wait", type=float, metavar="SECONDS", default=3600,
                      help="give up after waiting this long (default 3600)")
    release = commands.add_parser("release", help="release your own entry")
    release.add_argument("key")
    release.add_argument("--owner", required=True)
    release.add_argument("--revision", type=int, required=True)
    release.add_argument("--outcome", required=True)
    release.add_argument("--restoration", required=True)
    release.add_argument("--frontmost", choices=FRONTMOST, help=f"required for {FOREGROUND}: whether the previous "
                         "frontmost app was restored, or the session no longer owned the foreground")
    release.add_argument("--apps-quit", help="required when the entry records launched apps: how they were quit")
    check = commands.add_parser("check", help=f"refuse unless OWNER holds {FOREGROUND}, on this Mac within its window")
    check.add_argument("--owner", default=os.environ.get(OWNER), help=f"the session; default ${OWNER}")
    recover = commands.add_parser("recover", help="attended release of an entry its owner cannot release")
    recover.add_argument("key")
    recover.add_argument("--by", required=True)
    recover.add_argument("--revision", type=int, required=True)
    recover.add_argument("--verified", required=True)
    recover.add_argument("--offload-unchecked", metavar="REASON", help="the owner's reason to recover without "
                         "checking the second Mac the entry's run went to, such as that it is gone")
    # Its name until vocaro-engineering#58, which main refuses, naming the new one.
    recover.add_argument("--build-host-unchecked", metavar="REASON", help=argparse.SUPPRESS)
    importer = commands.add_parser("import", help="merge earlier ad hoc records, preserving every entry")
    importer.add_argument("paths", nargs="+")
    importer.add_argument("--by", required=True)
    defer = commands.add_parser("defer", help="reserve physical devices only in MAC's record, as the second Mac's "
                                "record does; prints what it changed")
    target = defer.add_mutually_exclusive_group(required=True)
    target.add_argument("mac", nargs="?", help="the Mac whose record holds physical devices")
    target.add_argument("--end", action="store_true", help="hold physical devices in this record again")
    defer.add_argument("--replace", action="store_true", help="defer to MAC when the record defers to another Mac")
    notices = commands.add_parser("notifications", help="the owner's notifications on a paired device during a run: "
                                  "record his one-time shortcuts, silence them, put them back, or record that he "
                                  f"silenced them ({device_notifications.GUIDE})")
    notice = notices.add_subparsers(dest="notifications", required=True)
    setup = notice.add_parser("setup", help=f"record the owner's word that he made the {device_notifications.SILENCE!r} "
                              f"and {device_notifications.RESTORE!r} shortcuts on KEY's device")
    setup.add_argument("key")
    setup.add_argument("--by", required=True, help="who made them: the owner")
    setup.add_argument("--at", required=True, help="when, ISO 8601 with a time zone")
    setup.add_argument("--words", help="the owner's words, quoted")
    for name, text in (("silence", "run the silencing shortcut on your held device"),
                       ("restore", "run the restoring shortcut, when your run silenced the device"),
                       ("by-owner", "record that the owner turned Do Not Disturb on himself for your run")):
        action = notice.add_parser(name, help=text)
        action.add_argument("key")
        action.add_argument("--owner", default=default_owner(), help=f"the session (default: {OWNER}, else the "
                            "agent session)")
        action.add_argument("--revision", type=int, required=True)
        if name == "by-owner":
            action.add_argument("--by", required=True, help="who turned it on: the owner")
            action.add_argument("--at", required=True, help="when he said it was on, ISO 8601 with a time zone")
    bounded = commands.add_parser("run", usage="%(prog)s [-h] [--owner SESSION] [--revision N] [--limit SECONDS] "
                                  "[--restoration SECONDS] key -- COMMAND ...", help="run COMMAND on KEY's physical "
                                  "device, held by the session, and stop it before the "
                                  "reservation ends; it records the run in the entry and never releases the device")
    bounded.add_argument("key")
    bounded.add_argument("--owner", default=default_owner(), help=f"the session holding KEY (default: {OWNER}, "
                         "else the agent session)")
    bounded.add_argument("--revision", type=int, help="refuse unless the entry is still at the revision the session "
                         "verified")
    bounded.add_argument("--limit", type=float, metavar="SECONDS", help="stop COMMAND after at most this long")
    bounded.add_argument("--restoration", type=float, metavar="SECONDS", default=RESTORATION,
                         help=f"time kept before the reservation ends for the session's restoration, after "
                         f"{TERMINATION}s for the run's termination (default {RESTORATION})")
    argv = sys.argv[1:] if argv is None else list(argv)
    # What follows run's -- is the command, whatever its options.
    command = None
    if argv[:1] == ["run"] and "--" in argv:
        split = argv.index("--")
        argv, command = argv[:split], argv[split + 1:]
    args = parser.parse_args(argv)
    said = lambda text: print(text, file=sys.stderr, flush=True)
    try:
        record = Record()
        if args.action == "path":
            print(record.path)
        elif args.action == "status":
            status(record)
        elif args.action == "show":
            _report(args.key, record.entry(args.key))
        elif args.action == "reserve":
            entry = record.reserve(args.key, owner=args.owner, revision=args.revision,
                purpose=args.purpose, release_by=args.release_by, device=args.device, title=args.title,
                details=_fields(args.set), approval=_given_approval(args))
            _report(args.key, silence_notifications(record, args.key, owner=args.owner, revision=entry["revision"],
                                                    said=said))
        elif args.action == "update":
            if not args.set and not args.own and not args.launched:
                raise ValueError("update takes --set FIELD=JSON, --own PID or --launched BUNDLE")
            _report(args.key, record.update(args.key, owner=args.owner, revision=args.revision, own=args.own,
                                            launched=args.launched, **_fields(args.set)))
        elif args.action == "take":
            if not args.owner:
                raise ValueError(f"name the session: --owner, or {OWNER}")
            entry = record.take(args.key, owner=args.owner, purpose=args.purpose,
                                release_by=args.release_by, timeout=args.wait, title=args.title,
                                details=_fields(args.set))
            _report(args.key, silence_notifications(record, args.key, owner=args.owner, revision=entry["revision"],
                                                    said=said))
        elif args.action == "release":
            # The owner's notifications first, when the entry is still the caller's at the revision named.
            entry = record.entry(args.key) or {}
            if entry.get("ownerChatID") == args.owner and entry.get("revision") == args.revision and \
                    not entry.get("activeRun"):
                args.revision = restore_notifications(record, args.key, owner=args.owner, revision=args.revision,
                                                      said=said)["revision"]
            _report(args.key, record.release(args.key, owner=args.owner, revision=args.revision,
                                             outcome=args.outcome, restoration=args.restoration,
                                             frontmost=args.frontmost, apps_quit=args.apps_quit))
        elif args.action == "check":
            entry = require_foreground(args.owner, record)
            print(f"{FOREGROUND}: held by {args.owner} for {entry.get('purpose')}, expected release "
                  f"{entry.get('expectedReleaseBy')}")
        elif args.action == "recover":
            if args.build_host_unchecked is not None:
                raise ValueError("--build-host-unchecked was --offload-unchecked's name until vocaro-engineering#58, "
                                 "and is no longer read: use --offload-unchecked (guides/shared-host.md, Earlier names)")
            entry = record.entry(args.key) or {}
            # Only once recovery would go ahead: never while its run's process still runs.
            restored = restore_for_recovery(args.key, entry, said) if entry.get("revision") == args.revision and \
                condition(entry) in ("orphan", "uncertain", "held") and not running(entry) else None
            _report(args.key, record.recover(args.key, by=args.by, revision=args.revision, verified=args.verified,
                                             unchecked=args.offload_unchecked, notifications=restored))
        elif args.action == "import":
            print("\n".join(record.import_legacy(args.paths, by=args.by)))
        elif args.action == "defer":
            if record.defer(None if args.end else args.mac, replace=args.replace):
                print(f"{record.path}: physical devices are reserved here again" if args.end else
                      f"{record.path}: physical devices are reserved in {args.mac}'s device record from now on")
        elif args.action == "notifications":
            if args.notifications == "setup":
                setup = record.notification_setup(args.key, by=args.by, at=args.at, words=args.words)
                print(json.dumps({"key": args.key, "setup": setup}, indent=2))
                return 0
            if not args.owner:
                raise ValueError(f"name the session: --owner, or {OWNER}")
            if args.notifications in ("silence", "restore"):
                entry = record.entry(args.key)
                _owned(args.key, entry, args.owner)
                if entry.get("revision") != args.revision:
                    raise Stale(f"{args.key} is at revision {entry.get('revision')}, not {args.revision}; read it "
                                "again. Nothing was changed")
                # Asked again, silence tries again unless it is done: a failed silencing is not a done one.
                act = silence_notifications if args.notifications == "silence" else restore_notifications
                entry = act(record, args.key, owner=args.owner, revision=args.revision, said=said)
            else:
                if moment(args.at) is None:
                    raise ValueError("--at is when the owner said Do Not Disturb was on, ISO 8601 with a time zone")
                entry = record.notifications(args.key, owner=args.owner, revision=args.revision,
                                             value={"silence": "owner", "by": args.by, "at": args.at,
                                                    "detail": "the owner turned Do Not Disturb on himself, and "
                                                              "turns it off"})
            _report(args.key, entry)
        elif args.action == "run":
            if not command:
                raise ValueError("name the command after --: devices run KEY -- COMMAND")
            run = BoundedRun([args.key], args.owner, command, record=record, revision=args.revision,
                             limit=args.limit, restoration=args.restoration)
            # A run that starts without its notifications silenced says so, and its entry records it.
            notified = None
            if run.revisions is not None:
                notified = (record.entry(args.key).get("notifications") or {}).get("silence") or "nothing was done"
                if notified not in SILENCED:
                    said(f"devices: {args.key}'s notifications are not silenced ({notified}), so a notification can "
                         f"reach this run's transcript and move VoiceOver's cursor; discard a session that picks one "
                         f"up. Run `devices notifications silence {args.key}` first, or "
                         f"{device_notifications.ask(args.key)}")
            with processes.forwarding_signals():
                try:
                    code = watched_run(run, args.key, command, {"notifications": notified} if notified else {},
                                       said)
                except processes.TimedOut as error:
                    print(f"devices: {error}; {args.key} stays reserved", file=sys.stderr)
                    return TIMED_OUT
                except Unmeasured:
                    print(f"devices: the run is unmeasured; {args.key} stays reserved", file=sys.stderr)
                    return UNMEASURED
            print(f"devices: {command[0]} exited with status {code}; {args.key} stays reserved", file=sys.stderr)
            # A command a signal ended has a negative status; a shell reports it as 128 plus the signal.
            return 128 - code if code < 0 else code
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        print(f"devices: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
