"""One record of reservations on a Mac, changed only under its lock: who holds what, and until when.

An entry reserves one thing for one session at a time. This module's own entry is mac-foreground, a Mac's screen,
keyboard, pointer, focus and speech. An extension adds entries of its own kinds to the same record by building on
Record, as the Apple extension does for physical devices and its simulator pool (queuekeeper_apple.devices, which
also has the record's command line).

The record lives in the account's Application Support directory, found independently of HOME
and TMPDIR so that every session finds the same one. A change takes an exclusive lock, reads the
record fresh, refuses unless the entry is still at the revision the caller read, and replaces the
file whole. The lock covers one change, never a reservation, and the kernel drops it with its
process. Like a heavy-work guard, an entry is never released by a deadline or a vanished process:
an active run whose recorded process is gone is an orphan, kept for attended recovery.

The mac-foreground entry names no device and records no approval (owner's decision, 2026-11-06,
vocaro-engineering#207): the record schedules it, one holder at a time, and its expected release
is its window. On this Mac, the owner's workstation, a session agrees the window with the owner
first and reserves it for that window; foreground actions are refused once the expected release
has passed. The second Mac is not a workstation: queuekeeper reserves its entry for each run without
asking (foreground.slot), and a run there may outlast its expected release. Foreground drivers
call require_foreground before acting; release waits for the processes the session recorded as
its own, and says whether the previous frontmost app was restored.

A record that names another Mac under DEFERRED is the second Mac's: every run sent there makes it so
(offload_host.defer_devices), and what it defers to that Mac's record is the extension's to say.
"""
import contextlib
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import pwd
import sys
import tempfile
import time

from queuekeeper.resources import process_start

SCHEMA = 1
OVERRIDE = "QUEUEKEEPER_DEVICE_RECORD"
ABOUT = ("Device reservations on this Mac. Change entries only through "
         "`qk devices` (vocaro-engineering guides/shared-host.md).")
# The Mac whose record holds physical devices, in a record that defers them to it: the second Mac's.
DEFERRED = "physicalDevicesRecordedOn"
# Written only by the record's own operations, which say who changed ownership and why.
MANAGED = {"revision", "ownerChatID", "lastOwnerChatID", "previousLease", "recovery", "importedFrom",
           "importedVersions", "uncertainReason", "hostProcessStart", "approval", "ownedProcesses", "frontmost",
           "launchedApps", "appsQuit", "notifications"}
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
    if entry.get("uncertain") or entry.get("state") == "restoration-unverified":
        return "uncertain"
    if entry.get("activeRun"):
        pid, start = entry.get("hostProcessIdentifier"), entry.get("hostProcessStart")
        alive = isinstance(pid, int) and pid > 0 and start is not None and process_start(pid) == start
        return "active" if alive else "orphan"
    return "released" if entry.get("state") == "released" else "held"


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

        An extension's record may make an entry wait for a check elsewhere too (_checked_elsewhere), which
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


def require_foreground(owner, record=None):
    """Refuse foreground work unless owner holds mac-foreground, and on this Mac within its window.

    Drivers call it before acting, on either Mac. On this Mac the window is the reservation's expected
    release, which the session agreed with the owner. On the second Mac, whose record defers physical
    devices, queuekeeper holds the entry for the run and names it in QUEUEKEEPER_DEVICE_OWNER (foreground.slot), and
    the run may outlast its expected release."""
    if not owner:
        raise Refused(f"name the session whose {FOREGROUND} reservation covers this work (--owner or {OWNER})")
    value = (record or Record()).load()
    entry = value["devices"].get(FOREGROUND)
    state = condition(entry) if entry is not None else "released"
    if state == "released":
        raise Refused(f"nobody holds {FOREGROUND}; " + ("queuekeeper reserves it for each run" if value.get(DEFERRED) else
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
