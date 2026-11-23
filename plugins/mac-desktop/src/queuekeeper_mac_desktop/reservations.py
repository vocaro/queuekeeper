"""One record of reservations on a Mac, changed only under its lock: who holds what, and until when.

An entry reserves one thing for one session at a time. This module's own entry is mac-foreground, a Mac's screen,
keyboard, pointer, focus and speech. A plugin adds entries of its own kinds to the same record by building on
Record, as the mobile plugin does for physical devices and simulator pools (queuekeeper_mobile.devices).

`qk devices` is the record's command line, and this module's main is its part for every entry: path, status, show,
reserve, update, release, check and recover. A project that lists only a plugin built on this one, such as
Unity, has it for its foreground; the mobile plugin adds its devices' actions to it (add_actions, act).

The record lives in the account's Application Support directory, found independently of HOME
and TMPDIR so that every session finds the same one. A change takes an exclusive lock, reads the
record fresh, refuses unless the entry is still at the revision the caller read, and replaces the
file whole. The lock covers one change, never a reservation, and the kernel drops it with its
process. Like a heavy-work guard, an entry is never released by a deadline or a vanished process:
an active run whose recorded process is gone is an orphan, kept for attended recovery here. A plugin may end such a
run by itself where it can prove the run gone, keeping the reservation held, as the mobile plugin does for a physical
device's (queuekeeper_mobile.upkeep).

The mac-foreground entry names no device and records no approval (owner's decision, 2026-11-06,
vocaro-engineering#207): the record schedules it, one holder at a time, and its expected release
is its window. On this Mac, the owner's workstation, a session agrees the window with the owner
first and reserves it for that window; foreground actions are refused once the expected release
has passed. Another Mac is not a workstation: queuekeeper reserves its entry for each run without
asking (foreground.slot), and a run there may outlast its expected release. Foreground drivers
call require_foreground before acting; release waits for the processes the session recorded as
its own, and says whether the previous frontmost app was restored.

A record that names another Mac under DEFERRED is another Mac's: every run sent there makes it so
(offload_host.defer_devices), and what it defers to that Mac's record is the plugin's to say. It says nothing
of the Mac's foreground: whether queuekeeper takes that for a run is the Mac's own setting (takes_foreground).
"""
import argparse
import contextlib
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import pwd
import subprocess
import sys
import tempfile
import time

from queuekeeper import offload_host
from queuekeeper.config import TOOLKIT_ROOT
from queuekeeper.resources import process_start

SCHEMA = 1
OVERRIDE = "QUEUEKEEPER_DEVICE_RECORD"
ABOUT = ("Device reservations on this Mac. Change entries only through "
         "`qk devices`.")
# The Mac whose record holds physical devices, in a record that defers them to it: another Mac's.
DEFERRED = "physicalDevicesRecordedOn"
# Written only by the record's own operations, which say who changed ownership and why.
MANAGED = {"revision", "ownerChatID", "lastOwnerChatID", "previousLease", "recovery", "importedFrom",
           "importedVersions", "uncertainReason", "hostProcessStart", "approval", "ownedProcesses", "frontmost",
           "launchedApps", "appsQuit", "notifications", "runRecoveries", "runRecoveryFailed", offload_host.PHYSICAL_DEVICE}
# Sessions waiting in line for an entry, by its key, kept beside the entries.
QUEUES = "queues"
# Seconds between looks at an entry while `take` waits for it.
POLL = 10
# The owner's screen, keyboard, pointer, focus and speech, held like a device.
FOREGROUND = "mac-foreground"
# The session a foreground driver acts for, as its reservation names it.
OWNER = "QUEUEKEEPER_DEVICE_OWNER"
# How a foreground release left the frontmost app: restored, or no longer the session's to restore.
FRONTMOST = ("restored", "not-ours")


class Refused(RuntimeError):
    """The record refused a change; nothing was written."""


class Stale(Refused):
    """The entry changed after the caller read it."""


def default_path():
    """The account's record. A HOME or TMPDIR private to one session must not start another."""
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    if sys.platform == "darwin":
        return home / "Library/Application Support/queuekeeper/device-handoff.json"
    return home / ".local/state/queuekeeper/device-handoff.json"


def record_path():
    """QUEUEKEEPER_DEVICE_RECORD names an isolated record for tests; sessions use the default."""
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


def condition(entry):
    """released, held, active, orphan or uncertain. Reading a condition never changes an entry."""
    return offload_host.reservation_condition(entry, lambda pid, start: "active" if process_start(pid) == start else "orphan")


def identity(entry):
    """What tells one holding of an entry from the next, which an owner's name alone does not: when it was
    reserved and the process recorded as its run."""
    return (entry.get("reservedAt"), entry.get("hostProcessIdentifier"), entry.get("hostProcessStart"))


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
        if device is not None: self._named(device)
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
            self._placed(key, identifier, record)
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
            self._placed(key, changes.get("device"), record)
            entry = {**current, **changes}
            if owned:
                entry["ownedProcesses"] = [*current.get("ownedProcesses", []), *owned]
            if launched:
                entry["launchedApps"] = list(dict.fromkeys([*current.get("launchedApps", []), *launched]))
                # An app the session says it launched is the session's to account for, even one queuekeeper opened too.
                said = entry.get("notifications") or {}
                if set(launched) & set(said.get("opened", [])):
                    entry["notifications"] = {**said, "opened": [app for app in said["opened"] if app not in launched]}
            return entry
        return self._change(key, revision, change)

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

        A plugin's record may make an entry wait for a check elsewhere too (_checked_elsewhere), which
        unchecked and users are for. notifications, what was done to put the owner's notifications back, joins
        the entry's notifications."""
        if not by or not verified:
            raise ValueError("recovery records who verified the device and what they verified")
        checked = self._checked_elsewhere(key, revision, unchecked, users)

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

    def _named(self, device):
        """Refuse a device identifier this record does not take. This record, which knows no devices, takes any."""

    def _placed(self, key, identifier, record):
        """Refuse an entry for identifier that belongs in another Mac's record. This record refuses none."""

    def _checked_elsewhere(self, key, revision, unchecked, users):
        """What a recovery of key checked on another Mac first, for its entry's recovery to record, or None."""
        return None


def takes_foreground():
    """Whether this Mac's foreground is queuekeeper's to take for a run, without a window agreed beforehand: its own
    settings say it takes foreground work (takes.foreground in its mac-settings.json, offload_host.mac_settings),
    on whichever Mac that is, the one a command starts on included (the owner's decision, 2026-11-17,
    vocaro/queuekeeper#211). Settings that do not say, or cannot be read, are no: the screen is someone's until its
    owner says otherwise. Until then a Mac was taken to be one when its device record deferred physical devices to
    another Mac's, which every run sent to it made it, so that the Mac that sent work never was."""
    try: return offload_host.mac_settings()["takes"]["foreground"] is True
    except ValueError: return False


def require_foreground(owner, record=None):
    """Refuse foreground work unless owner holds mac-foreground, and on a Mac that takes no foreground work by
    itself within its window.

    Drivers call it before acting, on any Mac. On a workstation the window is the reservation's expected
    release, which the session agreed with the owner. On a Mac whose settings say it takes foreground work
    (takes_foreground), queuekeeper holds the entry for the run and names it in QUEUEKEEPER_DEVICE_OWNER
    (foreground.slot), and the run may outlast its expected release."""
    if not owner:
        raise Refused(f"name the session whose {FOREGROUND} reservation covers this work (--owner or {OWNER})")
    value = (record or Record()).load()
    entry = value["devices"].get(FOREGROUND)
    state = condition(entry) if entry is not None else "released"
    if state == "released":
        raise Refused(f"nobody holds {FOREGROUND}; " + ("queuekeeper reserves it for each run" if takes_foreground() else
                      "before foreground work, agree a window with the owner and reserve it until the window ends"))
    if entry.get("ownerChatID") != owner:
        raise Refused(f"{FOREGROUND} is held by {entry.get('ownerChatID')} for {entry.get('purpose')}, expected "
                      f"release {entry.get('expectedReleaseBy')}; wait for its release")
    if state in ("orphan", "uncertain"):
        raise Refused(f"{FOREGROUND} is {state}; it needs attended recovery before foreground work")
    if not takes_foreground() and window_ended(entry):
        raise Refused(f"the window agreed for {FOREGROUND} ended at {entry.get('expectedReleaseBy')}; stop, restore "
                      "the owner's settings and release")
    return require_window(FOREGROUND, entry)


# The record's command line, `qk devices`: the actions every entry takes. A plugin that adds entries of its own
# kinds adds its own actions beside these (add_actions) and handles these through act.


def fields(pairs):
    """{FIELD: VALUE} from --set FIELD=VALUE pairs, each value read as JSON when it is JSON."""
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


def given_approval(args):
    """The owner's window the --approved-* options name, or None."""
    given = {"by": args.approved_by, "at": args.approved_at, "until": args.approved_until,
             "words": args.approval_words}
    return {name: value for name, value in given.items() if value is not None} or None


def report(key, entry):
    print(json.dumps({"key": key, "revision": (entry or {}).get("revision", 0),
                      "condition": condition(entry) if entry else None, "entry": entry}, indent=2))


def describe(key, entry, line=(), window=True, held=(), notes=(), hints=()):
    """One line of status for entry: its condition and holder, then held, what a plugin says of a held entry
    of its own kind; notes, what it says of any; the sessions in line; and hints, what to run to take it."""
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
        parts.extend(held)
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
    parts.extend(notes)
    if line:
        parts.append("waiting: " + ", ".join(f"{item['owner']} ({item.get('purpose')})" for item in line))
    parts.extend(hints)
    parts.append(f"revision {entry.get('revision', 0)}")
    return "; ".join(parts)


def recoverable_lines(entries):
    """What to say of each held entry whose process is gone and whose release has passed. When the
    processes cannot be listed, nothing can be said of any entry, and it says that."""
    listed = offload_host.listing(["ps", "-axww", "-o", "pid=,ppid=,uid=,lstart=,args="])
    if listed is None: return ["The processes could not be listed, so no held entry that nothing keeps can be named"]
    return [f"Recoverable: {offload_host.recoverable_line(item, toolkit=TOOLKIT_ROOT)}"
            for item in offload_host.recoverable(entries, offload_host.process_table(listed))]


def status(record, describe=describe):
    """Print the record's revision, the foreground's condition, held or free, then every other entry's, each as
    describe says it, and the held entries nothing keeps. Returns the record as read."""
    value = record.load()
    source = f" (from {OVERRIDE})" if os.environ.get(OVERRIDE) else ""
    print(f"Device record {record.path}{source}: revision {value.get('revision', 0)}, updated {value.get('updatedAt', 'never')}")
    if value.get(DEFERRED):
        print(f"Physical devices are reserved in {value[DEFERRED]}'s device record; this one refuses them")
    # The owner's screen first, held or free, so a session about to take it sees who has it.
    foreground = value["devices"].get(FOREGROUND)
    taken = takes_foreground()
    print(describe(FOREGROUND, foreground, window=not taken) if foreground is not None else
          f"{FOREGROUND}: free; " + ("queuekeeper reserves it for each run" if taken else
                                     "agree a window with the owner, then reserve it until the window ends"))
    for key, entry in value["devices"].items():
        if key != FOREGROUND: print(describe(key, entry, waiting(value, key)))
    for line in recoverable_lines(value["devices"]): print(line)
    return value


def recover_there(args):
    """`qk devices recover --host HOST`: the same recovery, with the same checks, in the record of the Mac with that
    SSH name, run by its own toolkit checkout in its login shell (remote.toolkit_qk); what it printed and its status
    come back unchanged. A connection that fails or times out leaves the outcome unknown, and nothing is recovered
    here in its place: `devices show` there says what became of the entry (vocaro/queuekeeper#167)."""
    from queuekeeper import remote
    arguments = ["devices", "recover"]
    for option, value in (("--by", args.by), ("--revision", args.revision), ("--verified", args.verified),
                          ("--offload-unchecked", args.offload_unchecked)):
        if value is None: continue
        # A quoted value beginning with a dash still looks like an option to the receiving parser.
        if isinstance(value, str) and value.startswith("-"): arguments.append(f"{option}={value}")
        else: arguments.extend((option, value))
    if args.key.startswith("-"): arguments += ["--", args.key]
    else: arguments.insert(2, args.key)
    try:
        return remote.toolkit_qk(args.host, arguments, project=True, timeout=remote.SURVEY_TIMEOUT)
    except subprocess.SubprocessError as error:
        raise RuntimeError(f"{args.host} did not answer, so whether {args.key} was recovered there is not known: {error}")


def add_actions(commands, *, status_help="every entry's condition and the held entries nothing keeps"):
    """Add the actions every entry takes to commands, a parser's subparsers."""
    commands.add_parser("path", help="print the record's location")
    commands.add_parser("status", help=status_help)
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
    recover.add_argument("--host", metavar="HOST", help="recover the entry in this SSH host's record, using its "
                         "login shell and ~/Development/queuekeeper/bin/qk; without it, in this Mac's")
    recover.add_argument("--offload-unchecked", metavar="REASON", help="the owner's reason to recover without "
                         "checking another Mac the entry's run went to, such as that it is gone")
    # Its name until vocaro-engineering#58, which act refuses, naming the new one.
    recover.add_argument("--build-host-unchecked", metavar="REASON", help=argparse.SUPPRESS)


def act(args, record, *, restore=None, said=None):
    """Carry out one of add_actions' actions on record; its exit status. restore(key, entry, said), when given,
    puts back what an entry's run changed on its device before its recovery and says what to record of it."""
    if args.action == "path":
        print(record.path)
    elif args.action == "status":
        status(record)
    elif args.action == "show":
        report(args.key, record.entry(args.key))
    elif args.action == "reserve":
        report(args.key, record.reserve(args.key, owner=args.owner, revision=args.revision, purpose=args.purpose,
                                        release_by=args.release_by, device=args.device, title=args.title,
                                        details=fields(args.set), approval=given_approval(args)))
    elif args.action == "update":
        if not args.set and not args.own and not args.launched:
            raise ValueError("update takes --set FIELD=JSON, --own PID or --launched BUNDLE")
        report(args.key, record.update(args.key, owner=args.owner, revision=args.revision, own=args.own,
                                       launched=args.launched, **fields(args.set)))
    elif args.action == "release":
        report(args.key, record.release(args.key, owner=args.owner, revision=args.revision, outcome=args.outcome,
                                        restoration=args.restoration, frontmost=args.frontmost,
                                        apps_quit=args.apps_quit))
    elif args.action == "check":
        entry = require_foreground(args.owner, record)
        print(f"{FOREGROUND}: held by {args.owner} for {entry.get('purpose')}, expected release "
              f"{entry.get('expectedReleaseBy')}")
    elif args.action == "recover":
        if args.build_host_unchecked is not None:
            raise ValueError("--build-host-unchecked was --offload-unchecked's name until vocaro-engineering#58, "
                             "and is no longer read: use --offload-unchecked")
        if args.host: return recover_there(args)
        entry = record.entry(args.key) or {}
        # Only once recovery would go ahead: never while its run's process still runs.
        restored = restore(args.key, entry, said) if restore and entry.get("revision") == args.revision and \
            condition(entry) in ("orphan", "uncertain", "held") and not running(entry) else None
        report(args.key, record.recover(args.key, by=args.by, revision=args.revision, verified=args.verified,
                                        unchecked=args.offload_unchecked, notifications=restored))
    return 0


def main(argv=None):
    """`qk devices` where no plugin adds devices to the record: its foreground and any entry already in it."""
    parser = argparse.ArgumentParser(prog="qk devices", description="Read or change this Mac's device record, "
                                     f"which reserves its foreground, {FOREGROUND}.")
    add_actions(parser.add_subparsers(dest="action", required=True))
    args = parser.parse_args(sys.argv[1:] if argv is None else list(argv))
    try:
        return act(args, Record())
    except (OSError, ValueError, RuntimeError) as error:
        print(f"devices: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
