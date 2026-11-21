"""One device handoff record for the shared host, changed only under its lock. The record itself, and its
mac-foreground entry, are the Mac desktop plugin's (queuekeeper_mac_desktop.reservations); this module adds what the
record holds of mobile devices, an iPhone's and an Android phone's alike, and has its command line. What differs
between the platforms, how a device is named, silenced and watched, each platform plugin says (platforms.py).

Physical devices and reserved-pool simulators are used only under a reservation recorded here.
Sessions used to keep ad hoc JSON files in /private/tmp. Two live copies let two sessions reserve
one device in different files, unlocked read-modify-write could lose entries, and the restart
after the 2026-10-14 panic emptied /private/tmp while device work could still be running.

The record lives in the account's Application Support directory, found independently of HOME
and TMPDIR so that every session finds the same one. A change takes an exclusive lock, reads the
record fresh, refuses unless the entry is still at the revision the caller read, and replaces the
file whole. The lock covers one change, never a reservation, and the kernel drops it with its
process. Like a heavy-work guard, an entry is never released by a deadline or a vanished process:
an active run whose recorded process is gone is an orphan. Queuekeeper recovers a physical device's orphaned run by
itself once its end is proven, ending the run and putting back what its record says it changed while the reservation
stays with its holder, and keeps attended recovery for what it cannot prove (upkeep.py; owner's decision,
2026-11-19, vocaro/queuekeeper#230). The one exception to keeping a reservation is a simulator pool's reservation that `qk simulator-pool reap` proves dead, every process
it records gone and its simulator shut down, which reap releases as a recovery by reap: once its
expected release has passed, or before then when only its holder held it (ALONE) and nothing accounts
for its owner, no live admission or other process naming the owner or the device (offload_host.release_dead;
vocaro-engineering#156).

The mac-foreground entry reserves a Mac's screen, keyboard, pointer, focus and speech. It names no
device and, like a physical device's, records no approval (owner's decision, 2026-11-06,
vocaro-engineering#207): the record schedules it, one holder at a time, and its expected release
is its window. On this Mac, the owner's workstation, a session agrees the window with the owner
first and reserves it for that window; foreground actions are refused once the expected release
has passed. Another Mac is not a workstation: queuekeeper reserves its entry for each run without
asking (foreground.slot), and a run there may outlast its expected release. Foreground drivers
call require_foreground before acting; release waits for the processes the session recorded as
its own, and says whether the previous frontmost app was restored.

Each Mac's simulators and emulators are its own, but a paired phone or tablet is reachable from the Macs, so a
physical device is reserved in one record only: the Mac that sends runs to another Mac. Another
Mac's record defers physical devices to that Mac and refuses to reserve one. A run there
uses one under a reservation held in the sending Mac's record, which `qk remote --device` carries.

A physical device's reservation records no approval (owner's decision, 2026-11-06): the record schedules
the devices, first come first served, one holder at a time, and a session that reserves one has the
owner's agreement already, which is a matter between the session and the owner. The reservation's
expected release is its window, and a bounded run stops before it.

A bounded run (`devices run KEY -- COMMAND`, BoundedRun) runs one command on physical devices the
session holds, refusing a command that names any other device, and
stops it before the reservation ends, keeping time for its termination and the session's
restoration. It records the run in the entry and never releases the device. A run that exits 0 but whose
output counts no test that ran, such as an xcodebuild selection that matched none, is unmeasured unless
`--no-tests` says it was meant to select none (vocaro/queuekeeper#100). `qk remote --device`
bounds a run on another Mac the same way, and there a bounded run takes its reservation from the lease.

`devices take KEY` takes a paired device (platforms.paired) in turn: it joins the device's queue, waits while another
session holds it or waits ahead, and reserves it. The queue lives
beside the entries, so joining it never changes a holder's revision, and a waiter whose process is
gone drops out. While anyone waits, only the first in line may reserve the entry. A session records each app it
launches on its device (`update --launched BUNDLE`), and releasing an entry that records any says how
they were quit.

A physical device's run keeps the owner's notifications out (vocaro-engineering#144): reserving or taking one
silences them, as its platform can (Platform.silence: on an iPhone the owner's shortcut, once he has recorded making
it there with `devices notifications setup`; on an Android phone Do Not Disturb over adb), and releasing or recovering
the entry puts them back. The entry's `notifications` records each step, and a run without them silenced says so.
A held device left silenced more than a grace period past its expected release has them put back by Queuekeeper,
which tells the owner and keeps the reservation held (upkeep.py; owner's decision, 2026-11-19,
vocaro/queuekeeper#174).
`devices run` watches the device's log while its command runs (Platform.watch), and a run a notification reached is
unmeasured and run once more; so does `qk remote --device`, from the sending Mac (#192).
"""
import argparse
import codecs
from datetime import datetime, timedelta, timezone
import glob
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

from . import platforms, reach
from queuekeeper import offload_host, processes
from queuekeeper_mac_desktop import reservations
from queuekeeper_mac_desktop.reservations import (
    ABOUT,
    DEFERRED,
    FOREGROUND,
    FRONTMOST,
    MANAGED,
    OVERRIDE,
    OWNER,
    POLL,
    QUEUES,
    SCHEMA,
    Refused,
    Stale,
    _owned,
    approval_ended,
    approval_refusal,
    condition,
    default_owner,
    default_path,
    moment,
    now,
    record_path,
    require_foreground,
    require_window,
    restart_emptied,
    running,
    waiting,
    window_ended,
)  # noqa: F401
from queuekeeper.lane_results import UNMEASURED, counted
from queuekeeper.resources import process_start

# Earlier ad hoc records. Status names one whose current bytes were never imported.
LEGACY = "/private/tmp/*device-handoff*.json"
# Seconds Git has to say whether forty hex digits name one of its objects, such as a commit.
OBJECT_TIMEOUT = 10
# The reservations a run on another Mac may use, held in the sending Mac's record, as JSON:
# {"record": MAC, "devices": {KEY: UDID}, "until": the reservations' end}. offload_host.py sets it for the run.
LEASE = "QUEUEKEEPER_DEVICE_LEASE"
# A lease's timing report, passed only to the top-level work on another Mac.
WINDOW = "QUEUEKEEPER_DEVICE_WINDOW"
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
# The owner's word that he made the notification setup a platform needs on a paired device, by its entry's key, kept
# beside the entries with the device it names (Platform.setup).
SETUP = "notificationSetup"
# What the entry's notifications record says of a run's silencing that let it start protected: queuekeeper silenced
# them, or the owner turned Do Not Disturb on himself.
SILENCED = ("requested", "owner")
# The process that reserved a simulator pool member and holds it, {"pid", "start"}: while it runs
# as one of a live run's processes, another Mac's census of leftover processes counts the
# member's simulator as that run's (offload_host.leftovers).
HOLDER = "holderProcess"
# Set beside HOLDER when that process alone holds the reservation, as a run's pool records it and
# `qk simulator-pool take` never does: reap may release such an entry before its expected release once
# its processes are gone and nothing accounts for its owner (offload_host.ALONE; vocaro-engineering#156).
ALONE = offload_host.ALONE
TIMES = ("leaseRevision", "releasedAt", "reservedAt", "leasedAt", "updatedAt")
EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def physical(udid):
    """Whether udid is a physical device's identifier, by any platform switched on, rather than a simulator's or an
    emulator's."""
    return platforms.physical(udid)


def canonical(udid):
    """The form of a device's identifier the record keeps and compares: an Apple UDID uppercased, an adb serial as
    adb prints it."""
    return platforms.canonical(udid)


def paired():
    """The keys `devices take` waits in line for, every platform's (Platform.paired)."""
    return platforms.paired()


def deferred_refusal(mac, key):
    return Refused(f"physical devices are reserved in {mac}'s device record, not this one: reserve {key} there and "
                   f"send the run here with `qk remote run|admit --device {key}`. Nothing was changed")


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


def _latest(entry):
    times = [moment(entry.get(name)) for name in TIMES]
    return max([value for value in times if value is not None] or [EPOCH])


def _content(entry):
    return {key: value for key, value in entry.items()
            if key not in ("revision", "importedFrom", "importedVersions", "uncertain", "uncertainReason",
                           offload_host.PHYSICAL_DEVICE)}


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


class Record(reservations.Record):
    """The record with what the mobile plugins keep in it: physical devices, which one Mac's record holds for
    both, the line a session waits in for a paired one, and the owner's notifications on it."""

    def _named(self, device):
        if not platforms.known(device) or canonical(device) != device:
            raise ValueError(f"name the device by its canonical {platforms.identifier_names()}, as the platform's own "
                             "tools print it, never a name, 'booted' or a default")

    def _placed(self, key, identifier, record):
        deferred = record.get(DEFERRED)
        if deferred:
            current = record["devices"].get(key) or {}
            if physical(identifier) or (identifier == current.get("device") and
                                        offload_host.physical_device_entry(current)):
                raise deferred_refusal(deferred, key)

    def _change(self, key, revision, change):
        """Classify the device as part of the same write that changes its entry."""
        def classify(current, record):
            entry = change(current, record)
            identifier = entry.get("device")
            previous = current or {}
            marker = offload_host.PHYSICAL_DEVICE
            if physical(identifier) or (identifier == previous.get("device") and previous.get(marker) is True):
                entry[marker] = True
            else:
                entry.pop(marker, None)
            return entry
        return super()._change(key, revision, classify)

    def take(self, key, *, owner, purpose, release_by, timeout, title=None, details=None, poll=POLL, said=None):
        """Reserve key in turn, first come first served, waiting up to timeout seconds in its line.

        The entry already records the device, from its first reservation, so the take names no other. An
        orphaned or uncertain entry needs attended recovery; waiting leaves the line on any end."""
        if key not in paired():
            raise ValueError(f"take waits only for {' or '.join(paired()) or 'a paired device'}; reserve {key} with "
                             "`devices reserve`")
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
                                  f"{key} --device ID`, records the device. Nothing was changed")
                state = condition(entry)
                line = waiting(value, key)
                if state == "released" and line and line[0]["owner"] == owner:
                    try:
                        return self.reserve(key, owner=owner, revision=entry["revision"], purpose=purpose,
                                            release_by=release_by, title=title, details=details)
                    except Stale:
                        continue
                # What is due by itself is put right while waiting: a run proven gone, and the owner's notifications
                # left silenced past the holder's window (upkeep).
                from . import upkeep
                if any(due == key for due, _kind in upkeep.due(value)):
                    upkeep.upkeep(self, keys=[key], said=said)
                    if (self.entry(key) or {}).get("revision") != entry.get("revision"): continue
                if state in ("orphan", "uncertain"):
                    why = upkeep.unproven(key, entry) if state == "orphan" else entry.get("uncertainReason")
                    raise Refused(f"{key} is {state}, held by {entry.get('ownerChatID')} for {entry.get('purpose')}"
                                  f"{f' ({why})' if why else ''}; it needs attended recovery (`devices recover`) first")
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

    def notifications(self, key, *, owner, revision, value, launched=()):
        """Record in the caller's own entry what was done to the owner's notifications on its device: value's
        fields join the entry's notifications. launched adds apps queuekeeper opened there, as update's does, and notes them
        as queuekeeper's, which a release does not ask the session about (vocaro-engineering#292)."""
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
        """Record the owner's word that he made the notification setup its platform needs on key's device, such as
        an iPhone's shortcuts, which the entry names; the setup recorded. A later device under the key needs his word
        again. A platform that needs none refuses (Platform.setup)."""
        if not by or moment(at) is None or moment(at) > datetime.now(timezone.utc):
            raise ValueError("the setup records who made it and when: a past ISO 8601 time with a time zone")
        with self._exclusive():
            record = self.load()
            entry = record["devices"].get(key) or {}
            if record.get(DEFERRED):
                raise deferred_refusal(record[DEFERRED], key)
            if not physical(entry.get("device")):
                raise Refused(f"{key} records no physical device, so there is none to have the setup. "
                              "Nothing was changed")
            setup = {"device": canonical(entry["device"]), "by": by, "at": at,
                     **platforms.of(entry["device"]).setup(key, entry), **({"words": words} if words else {})}
            record.setdefault(SETUP, {})[key] = setup
            self._replace(record)
            return setup

    def declare(self, key, *, by, reach_ways=None, cable_on=None, absent=None, clear=False, macs=None):
        """Record the owner's declaration of key's physical device: the ways a Mac may reach it, in his order, the
        Mac its cable is expected on, and what it means when no Mac reaches it (reach.py); or with clear, remove
        it. It is kept beside the entries, under key, so it changes no entry: a held, orphaned or uncertain entry's
        reservation, its revision included, stays as it was. macs are the names a cable may name. Returns the
        declaration, or None once cleared."""
        if not by:
            raise ValueError("a declaration records who made it: the owner")
        declaration = None if clear else reach.declared(reach_ways or [], cable_on, absent, by=by, at=now())
        wrong = [] if clear else reach.problems(declaration, macs)
        if wrong:
            raise ValueError("; ".join(wrong) + ". Nothing was changed")
        with self._exclusive():
            record = self.load()
            if record.get(DEFERRED):
                raise Refused(f"physical devices are declared in {record[DEFERRED]}'s device record, which holds them, "
                              "not this one. Nothing was changed")
            declarations = record.get(reach.DECLARATIONS) or {}
            if clear:
                if key not in declarations:
                    raise Refused(f"{key} is not declared. Nothing was changed")
                declarations.pop(key)
            else:
                if not physical((record["devices"].get(key) or {}).get("device")):
                    raise Refused(f"{key} records no physical device, so there is none to declare; its first "
                                  f"reservation, `devices reserve {key} --device ID`, records it. Nothing was changed")
                declarations[key] = declaration
            record[reach.DECLARATIONS] = declarations
            self._replace(record)
            return declaration

    def _checked_elsewhere(self, key, revision, unchecked, users):
        """For a recovery of a physical device whose run went to another Mac: that host's check,
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
            from queuekeeper.remote import device_users as users
        since = moment(run.get("startedAt"))
        try:
            still = users(host, canonical(current["device"]), since.timestamp() if since else None)
        except RuntimeError as error:
            raise Refused(f"{key}'s run went to {host}, which could not be checked: {error}. Recovery waits for it, "
                          "or records why it cannot be checked (--offload-unchecked). Nothing was changed") from None
        if still:
            raise Refused(f"{key}'s run went to {host}, where these may still use its device; recovery waits for "
                          "them. Nothing was changed:\n  " + "\n  ".join(still))
        return {"host": host, "checkedAt": now()}

    def defer(self, mac, *, replace=False):
        """Defer physical devices to mac's record, after which this record refuses to reserve one.

        Another Mac's record defers them to the Mac that sends it runs; None ends a deferral.
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
                    if (offload_host.physical_device_entry(entry) or physical(entry.get("device")))
                    and condition(entry) != "released"]
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
                    if physical(merged.get("device")):
                        merged[offload_host.PHYSICAL_DEVICE] = True
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
                    holders.setdefault(canonical(entry["device"]), []).append(key)
            for keys in holders.values():
                for key in keys if len(keys) > 1 else []:
                    entry = record["devices"][key]
                    if not entry.get("uncertain"):
                        entry.update(revision=entry.get("revision", 0) + 1, uncertain=True,
                                     uncertainReason=f"the same device is also held as {', '.join(k for k in keys if k != key)}")
            if len(report) > sum(line.endswith("already imported") for line in report):
                self._replace(record)
        return report


def git_object(word, root=None):
    """Whether the Git of root's checkout, by default the current directory's, knows word as an object.
    A device's UDID never is one; Git that cannot answer in time knows nothing."""
    from queuekeeper.remote import environment
    try:
        return subprocess.run(["git", "cat-file", "-e", word], cwd=root, capture_output=True, env=environment(),
                              timeout=OBJECT_TIMEOUT).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def named(words, known=(), root=None, *, commits=()):
    """The device identifiers words name, in their canonical forms: each that a platform finds by its pattern alone
    (Platform.named, such as every hardware UDID), and each of known that appears, whatever its case.

    A platform's pattern judges the text itself: forty hex digits, say, are an older iPhone's UDID, a commit's whole
    SHA-1 or a code-signing identity's, so the Apple platform leaves out one that Git knows (git_object). Explicitly
    verified foreign commits exempt only a whole argument, never an identifier in a device option or shell string:
    such an argument is replaced by a placeholder before any platform reads the text. Known devices remain devices
    even when their identifiers also name Git objects."""
    words = [str(word) for word in words]
    text = " ".join(words).upper()
    commits = {commit.upper() for commit in commits}
    # Retain a placeholder rather than deleting a word: deleting a signing identity's value
    # could make its option appear to take the next argument instead. An option's value remains
    # subject to the ordinary check; the declaration is for bare positional commit arguments.
    scanned = " ".join("_" if word.upper() in commits and not (
        index and words[index - 1].startswith("-") and words[index - 1] != "--") else word
        for index, word in enumerate(words)).upper()
    found = {identifier for platform in platforms.PLATFORMS for identifier in platform.named(scanned, root)}
    found.update(canonical(udid) for udid in known if isinstance(udid, str) and udid and udid.upper() in text)
    return found


class BoundedRun:
    """One command on physical devices the session holds, stopped before their reservations end and
    never releasing them.

    Here the reservations come from this Mac's record: the session holds each with no run active,
    at revision when one is given. Each entry records the run: active,
    with this process as the run's and the command as the entry's own process, then its outcome.
    On another Mac, whose record defers physical devices, they come from the lease the dispatch
    carries (LEASE), and the dispatch records the run in the sending Mac's record. Every device
    identifier the command names, a hardware UDID or any device the record knows, is one it holds;
    an object of root's Git, by default the current directory's, is none (named).
    The bound is the reservations' remaining time, less what the run keeps for its termination
    and the session's restoration, and at most limit."""

    def __init__(self, keys, owner, words, *, record=None, revision=None, limit=None, restoration=None, root=None,
                 commits=()):
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
            # A run before this one whose process is proven gone ends here, rather than refusing this one as an
            # orphan for attended recovery (vocaro/queuekeeper#230).
            # The revision the session read names the entry as it was before that recovery.
            if any(condition(entries[key]) == "orphan" for key in keys if key in entries):
                from . import upkeep
                before = entries[keys[0]].get("revision", 0) if keys[0] in entries else None
                upkeep.upkeep(self.record, keys=keys, said=lambda text: print(text, file=sys.stderr, flush=True))
                value = self.record.load()
                entries = value["devices"]
                if revision is not None and revision == before and keys[0] in entries:
                    revision = entries[keys[0]].get("revision", 0)
            self.revisions = {}
            self.devices, self.deadline = self._held(keys, entries, revision)
        held = set(self.devices.values())
        for udid in sorted(named(words, [entry.get("device") for entry in entries.values()], root, commits=commits) - held):
            holders = [key for key, entry in entries.items() if canonical(str(entry.get("device") or "")) == udid]
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
                              "and emulators come from a project's pool. Nothing ran")
            try: require_window(key, entry)
            except Refused as error: raise Refused(f"{error}. Nothing ran") from None
            until = (entry.get("approval") or {}).get("until")
            release = moment(entry.get("expectedReleaseBy"))
            if release is None:
                raise Refused(f"{key} records no expected release to bound the run by. Nothing ran")
            ends += [release] + ([] if until in (None, "released") else [moment(until)])
            devices[key] = canonical(entry["device"])
            self.revisions[key] = entry.get("revision", 0)
        return devices, min(ends)

    def _leased(self, keys, mac):
        """The reservations held in mac's record that this run on another Mac carries."""
        if not os.environ.get(LEASE):
            raise deferred_refusal(mac, keys[0])
        try:
            lease = json.loads(os.environ[LEASE])
            carried = {key: canonical(udid) for key, udid in lease["devices"].items()}
            deadline = moment(lease["until"])
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            raise Refused(f"{LEASE} is not a lease of devices and their end ({error!r}). Nothing ran") from error
        if lease.get("record") != mac:
            raise Refused(f"the run's lease is from {lease.get('record')}'s record, not {mac}'s, which this record "
                          "defers physical devices to. Nothing ran")
        missing = [key for key in keys if key not in carried]
        if missing:
            raise Refused(f"the run's lease carries {', '.join(carried) or 'no device'}, not {', '.join(missing)}: "
                          f"send the run from {mac} with `qk remote run|admit --device {missing[0]}`. Nothing ran")
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

    def _record(self, key, **changes):
        """Record changes to the run in key's entry, once the entry marks it active. The session that holds the
        entry may change it meanwhile, extending its window, say, so a stale revision is read again while the
        entry is still held by the owner with this run active, begun by this process (vocaro/queuekeeper#230)."""
        while True:
            try:
                return self._update(key, **changes)
            except Stale:
                entry = self.record.entry(key) or {}
                if not (entry.get("ownerChatID") == self.owner and entry.get("state") != "released"
                        and entry.get("activeRun") and entry.get("hostProcessIdentifier") == os.getpid()
                        and (entry.get("run") or {}).get("startedAt") == self.run["startedAt"]):
                    raise
                self.revisions[key] = entry.get("revision", 0)

    def begin(self, run):
        """Start the bound and mark each entry's run active, with this process as the run's; the bound's seconds."""
        bound = int(self.seconds())
        self.until = datetime.now(timezone.utc) + timedelta(seconds=bound)
        self.run = {**run, "boundSeconds": bound, "stopsAt": self.until.isoformat(), "processes": []}
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
        """Record pid, which the run started, as each entry's own process: release and recovery wait for it. The
        run names it among its processes too, which an automatic recovery stops once this process is gone
        (upkeep.recover_run)."""
        self.run = {**self.run, "processes": [*self.run.get("processes", []), pid]}
        for key in self.revisions or ():
            self._record(key, own=[pid], run=self.run)

    def expired(self):
        """Whether the bound begin started has passed, by the wall clock, which counts a sleep."""
        return self.until is not None and datetime.now(timezone.utc) >= self.until

    def finish(self, outcome, *, uncertain=False):
        """Record the run's outcome in each entry: inactive, or still active and uncertain when its end is
        unconfirmed, for attended recovery. The devices stay reserved."""
        self.run = {**self.run, "endedAt": now(), "outcome": outcome}
        for key in self.revisions or ():
            try:
                self._record(key, run=self.run, **({"uncertain": True} if uncertain else {"activeRun": False}))
            except (Refused, OSError, TimeoutError) as error:
                print(f"devices: {key}'s entry did not record the run's end, so it shows the run orphaned until its "
                      f"owner releases or recovers it: {error}", file=sys.stderr, flush=True)

    def execute(self, command, *, cwd=None, stdout=None, run=None, watch=None, env=None):
        """Run command under the bound, recorded in each entry, and return its exit status.

        A timeout, a stop, or a command that did not start is recorded and raised once the command
        and its observed descendants have ended. A run whose end is unconfirmed stays active. A watch
        of the device's log (Platform.watch) is stopped as the command ends, and what it
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


class Transcript:
    """A command's standard output, shown on this process's standard output as it comes and kept, so that the run can read the
    tests it reports (lane_results.counted). Popen writes to it by its descriptor."""

    def __enter__(self):
        self.read, self.write = os.pipe()
        self.chunks = []
        self.thread = threading.Thread(target=self._copy, daemon=True)
        self.thread.start()
        return self

    def fileno(self):
        return self.write

    def _copy(self):
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        with os.fdopen(self.read, "rb") as source:
            for chunk in iter(lambda: source.read1(65536), b""):
                text = decoder.decode(chunk)
                self.chunks.append(text)
                sys.stdout.write(text)
                sys.stdout.flush()
        text = decoder.decode(b"", final=True)
        if text:
            self.chunks.append(text)
            sys.stdout.write(text)

    def __exit__(self, *_):
        # The command and its observed descendants have ended, so closing this copy ends the output.
        os.close(self.write)
        self.thread.join(timeout=10)

    @property
    def text(self):
        return "".join(self.chunks)


def watched(run, key, command, extra, cwd=None, env=None, kept=None):
    """Run command under run while watching key's device log, or each of several keys' devices' logs, for a
    notification that reached it; its exit status and what the watch saw, which the entry's run records
    (Platform.watch, platforms.Together). With kept, a list, the command's output is shown through a
    Transcript and its text appended to kept."""
    keys = keyed(key)
    watches = {each: platforms.of(run.devices[each]).watch(run.devices[each]) for each in keys}
    watch = watches[keys[0]] if len(keys) == 1 else platforms.Together(watches)
    if kept is None:
        code = run.execute(command, run=extra, watch=watch, cwd=cwd, env=env)
    else:
        transcript = Transcript()
        try:
            with transcript:
                code = run.execute(command, run=extra, watch=watch, cwd=cwd, env=env, stdout=transcript)
        finally:
            kept.append(transcript.text)
    return code, run.run["notificationWatch"]


def no_tests(text):
    """Why a run whose output is text measured nothing though it may exit 0: it reports a test count, and every
    test it counts skipped or none ran (lane_results.counted). None when it ran a test or
    reports no count, as a command that runs no tests does."""
    found = counted(text)
    if found is None or found[0] > found[1]:
        return None
    ran, skipped = found
    return f"it reported {ran} tests executed, {skipped} of them skipped" if skipped else \
        f"it reported {ran} tests executed"


def watched_run(run, key, command, extra, said, cwd=None, env=None, kept=None):
    """Run command under run, and once more when a notification was presented on key's device during it (#144);
    key may be several keys, whose devices are all watched (remote.Lease).

    Such a run is unmeasured: its transcript may hold the notification and the screen reader's focus may have moved. Its
    rerun's status is the run's; a rerun that one reaches too, or no time left for one, raises Unmeasured. A run
    whose device log could not be read is said to be unwatched and keeps its status."""
    keys = keyed(key)
    code, seen = watched(run, keys, command, extra, cwd, env, kept)
    # What the session is told names every device watched, in the words of the first one's platform.
    who = " and ".join(keys)
    platform = platforms.of(run.devices[keys[0]])
    if not seen["presented"]:
        if not seen["watched"]:
            said(f"devices: {who}'s log could not be read during the run ({seen['detail']}), so a notification that "
                 "reached it would not be seen; read the transcript for one")
        return code
    said(f"devices: unmeasured: {platform.presented(seen)}. The run's transcript may hold it and the screen "
         f"reader's focus may have moved, so its status {code} measures nothing; running it once more")
    try:
        run.seconds()
    except Refused as error:
        said(f"devices: unmeasured, not run again: {error}")
        raise Unmeasured() from None
    first = {name: value for name, value in run.run.items() if name not in ("boundSeconds", "stopsAt")}
    code, seen = watched(run, keys, command, {**extra, "rerunOf": {**first, "measured": False}}, cwd, env, kept)
    if seen["presented"]:
        said(f"devices: unmeasured again: {platform.presented(seen)}. Not run a third time: ask the "
             f"owner to check Do Not Disturb on {who} ({platform.guide})")
        raise Unmeasured()
    said(f"devices: the rerun saw no notification; its status {code} is the run's")
    return code


def silence_notifications(record, key, *, owner, revision, said):
    """Silence the owner's notifications on the physical device key's entry holds, for the caller's run, as its
    platform can (Platform.silence), or else say what to ask him.

    The entry records each step, the restore as pending before the device is changed, so that a run that dies
    after it still has its release or recovery put them back. Each request records the restore as pending again, a
    request after a completed restore too, so that the release or recovery puts back what it silenced (#144). It
    returns the entry; an entry still silenced and not yet restored, silenced by the owner himself, or holding no
    physical device, is left."""
    value = record.load()
    entry = value["devices"].get(key)
    _owned(key, entry, owner)
    notifications = entry.get("notifications") or {}
    done = notifications.get("silence")
    silenced = done == "owner" or (done in SILENCED and notifications.get("restore") == "pending")
    if not physical(entry.get("device")) or silenced or value.get(DEFERRED):
        return entry
    return platforms.of(entry["device"]).silence(record, key, entry, value, owner=owner, revision=revision, said=said)


def restore_notifications(record, key, *, owner, revision, said):
    """Put back the owner's notifications on key's device when the caller's run silenced them; the entry. A restore
    that fails is recorded and said, for the owner, and the release goes on."""
    entry = record.entry(key)
    _owned(key, entry, owner)
    if (entry.get("notifications") or {}).get("restore") != "pending":
        return entry
    outcome = restore_for_recovery(key, entry, said)
    return record.notifications(key, owner=owner, revision=revision, value=outcome)


def restore_for_recovery(key, entry, said):
    """Put back the owner's notifications on entry's device when its run left a restore pending (Platform.restore);
    what to record in its notifications, or None when nothing was pending."""
    if (entry.get("notifications") or {}).get("restore") != "pending":
        return None
    outcome = platforms.of(entry.get("device")).restore(key, entry, said)
    return {**outcome, "restoredAt": now()} if outcome is not None else None


def _notification_state(key, entry):
    """What status says of the owner's notifications on entry's physical device, or None."""
    value = entry.get("notifications") or {}
    platform = platforms.of(entry.get("device"))
    if condition(entry) == "released":
        return (f"notifications: not put back after the last run ({value.get('restoreDetail')}); Do Not Disturb may "
                "still be on, tell the owner") if value.get("restore") in ("failed", "pending") else None
    silence = value.get("silence")
    failed = (f"; the last automatic restore failed ({value['lastRestoreFailure']})"
              if value.get("restore") == "pending" and value.get("lastRestoreFailure") else "")
    if silence == "requested": return f"notifications: {platform.silenced_words(value)}{failed}"
    if silence == "owner": return f"notifications: Do Not Disturb turned on by {value.get('by')} at {value.get('at')}"
    return (f"notifications: not silenced ({value.get('detail') or 'nothing was done'}); "
            f"`devices notifications silence {key}`, or {platform.ask(key)}")


def _describe(key, entry, line=(), window=True):
    """reservations.describe, with what this record says of a physical device: an approval an earlier reservation
    recorded that has ended, the owner's notifications, and how to take it in turn."""
    device = physical(entry.get("device"))
    held = ["the approval an earlier reservation recorded has ended; work on the device is refused"] \
        if device and key != FOREGROUND and approval_ended(entry) else []
    notified = _notification_state(key, entry) if device else None
    if device and condition(entry) == "orphan":
        from .upkeep import unproven
        held.append(f"not recovered by itself: {unproven(key, entry) or 'its recovery is due'}")
    recovered = (entry.get("runRecoveries") or [None])[-1] if device and condition(entry) != "released" else None
    if isinstance(recovered, dict):
        held.append(f"Queuekeeper recovered its last run at {recovered.get('at')}, since {recovered.get('proof')}")
    return reservations.describe(key, entry, line, window, held=held, notes=[notified] if notified else [],
                                 hints=[f"take it in turn with `devices take {key}`"] if key in paired() else [])


def overdue_silences(entries, at=None):
    """What to say of each held physical device past its expected release whose silencing of the owner's
    notifications is not yet put back (vocaro/queuekeeper#174): Do Not Disturb may still be on his device after the
    time he agreed. Saying so changes nothing; Queuekeeper puts them back by itself once the grace past the expected
    release has passed (upkeep.restore_overdue), keeping the reservation held."""
    from .upkeep import grace
    at = at or datetime.now(timezone.utc)
    found = []
    for key, entry in entries.items():
        if not physical(entry.get("device")) or condition(entry) == "released": continue
        notifications = entry.get("notifications") or {}
        if notifications.get("restore") != "pending": continue
        deadline = moment(entry.get("expectedReleaseBy"))
        if deadline is None or deadline >= at: continue
        owner = entry.get("ownerChatID")
        restore = (f"`qk devices notifications restore {key} --owner {owner} --revision {entry.get('revision', 0)}`")
        if notifications.get("lastRestoreFailure"):
            then = (f"Queuekeeper could not put them back by itself ({notifications['lastRestoreFailure']}) and tries "
                    "again every ten minutes; tell the owner")
        else:
            then = (f"Queuekeeper puts them back by itself at {(deadline + timedelta(seconds=grace())).isoformat()}, "
                    "keeping the reservation held, and tells the owner")
        found.append(f"{key} is held by {owner} past its expected release, {entry.get('expectedReleaseBy')}, with "
                     "the owner's notifications not yet put back: Do Not Disturb may still be on his device after the "
                     f"time he agreed. Its holder restores them with {restore} or releases it; otherwise {then}")
    return found


def status(record, macs=None):
    """Print every entry's condition (reservations.status), then each device's declaration and what each Mac reads of
    it now: this Mac's and macs', (name, SSH destination) pairs, by default the farm's."""
    macs = reach.farm_macs() if macs is None else macs
    # What is due by itself first, so that the entries below show what it did (upkeep).
    from . import upkeep
    for line in upkeep.upkeep(record): print(f"Put right: {line.removeprefix('devices: ')}")
    value = reservations.status(record, _describe)
    for line in overdue_silences(value["devices"]): print(f"Overdue silencing: {line}")
    # Which Macs reach each device, and how, as each lists it now, beside what the owner declared (reach.py).
    found = reach.readings(macs=macs)
    names = [reach.mac_here(), *(name for name, _host in macs)]
    for line in reach.lines(reach.judged(value, record.path, found, names), value.get(DEFERRED)): print(line)
    imported = {item.get("sha256") for item in value.get("imports", [])}
    for legacy in sorted(glob.glob(LEGACY)):
        try:
            digest = hashlib.sha256(Path(legacy).read_bytes()).hexdigest()
        except OSError:
            continue
        if digest not in imported:
            print(f"Legacy record {legacy} has entries not imported: import it, then stop writing it")


def main(argv=None):
    # Run alone, as another Mac runs it before a dispatch, no plugin registered a platform.
    if not platforms.PLATFORMS: platforms.discover()
    parser = argparse.ArgumentParser(prog="qk devices",
                                     description="Read or change the shared device handoff record, or run a "
                                     "bounded command on a physical device it reserves.")
    commands = parser.add_subparsers(dest="action", required=True)
    reservations.add_actions(commands, status_help="every entry's condition, each device's declaration and which "
                             "Macs reach it now, and legacy records not yet imported")
    declare = commands.add_parser("declare", help="the owner's declaration of a physical device: the ways a Mac may "
                                  "reach it, in order, and what it means when no Mac does; it changes no entry")
    declare.add_argument("key")
    declare.add_argument("--by", required=True, help="who declares it: the owner")
    declare.add_argument("--reach", metavar="WAYS", help=f"the ways, in order, comma-separated: "
                         f"{', '.join(reach.WAYS)}, or both")
    declare.add_argument("--cable-on", metavar="MAC", help="the Mac its cable is expected on, by its name in the farm "
                         "or this Mac's")
    declare.add_argument("--absent", choices=reach.ABSENT, help="reached by no Mac is an error, or the device is "
                         "offline, which is no error")
    declare.add_argument("--clear", action="store_true", help="remove the declaration")
    take = commands.add_parser("take", help=f"wait in line for {' or '.join(paired()) or 'a paired device'}, first "
                               "come first served, "
                               "and reserve it; prints the entry")
    take.add_argument("key", choices=sorted(paired()))
    take.add_argument("--owner", default=default_owner(), help=f"the session (default: {OWNER}, else the agent session)")
    take.add_argument("--purpose", required=True)
    take.add_argument("--release-by", required=True)
    take.add_argument("--title")
    take.add_argument("--set", action="append", metavar="FIELD=JSON")
    take.add_argument("--wait", type=float, metavar="SECONDS", default=3600,
                      help="give up after waiting this long (default 3600)")
    importer = commands.add_parser("import", help="merge earlier ad hoc records, preserving every entry")
    importer.add_argument("paths", nargs="+")
    importer.add_argument("--by", required=True)
    defer = commands.add_parser("defer", help="reserve physical devices only in MAC's record, as another Mac's "
                                "record does; prints what it changed")
    target = defer.add_mutually_exclusive_group(required=True)
    target.add_argument("mac", nargs="?", help="the Mac whose record holds physical devices")
    target.add_argument("--end", action="store_true", help="hold physical devices in this record again")
    defer.add_argument("--replace", action="store_true", help="defer to MAC when the record defers to another Mac")
    notices = commands.add_parser("notifications", help="the owner's notifications on a paired device during a run: "
                                  "record his one-time setup, silence them, put them back, or record that he "
                                  f"silenced them ({'; '.join(platform.guide for platform in platforms.PLATFORMS)})")
    notice = notices.add_subparsers(dest="notifications", required=True)
    setup = notice.add_parser("setup", help="record the owner's word that he made the one-time setup KEY's platform "
                              "needs on its device, such as an iPhone's silencing and restoring shortcuts")
    setup.add_argument("key")
    setup.add_argument("--by", required=True, help="who made them: the owner")
    setup.add_argument("--at", required=True, help="when, ISO 8601 with a time zone")
    setup.add_argument("--words", help="the owner's words, quoted")
    for name, text in (("silence", "silence the notifications on your held device, as its platform can"),
                       ("restore", "put them back, when your run silenced the device"),
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
                                  "[--restoration SECONDS] [--no-tests] key -- COMMAND ...",
                                  help="run COMMAND on KEY's physical device, held by the session, and stop it before "
                                  "the reservation ends; it records the run in the entry and never releases the device")
    bounded.add_argument("key")
    bounded.add_argument("--owner", default=default_owner(), help=f"the session holding KEY (default: {OWNER}, "
                         "else the agent session)")
    bounded.add_argument("--revision", type=int, help="refuse unless the entry is still at the revision the session "
                         "verified")
    bounded.add_argument("--limit", type=float, metavar="SECONDS", help="stop COMMAND after at most this long")
    bounded.add_argument("--restoration", type=float, metavar="SECONDS", default=RESTORATION,
                         help=f"time kept before the reservation ends for the session's restoration, after "
                         f"{TERMINATION}s for the run's termination (default {RESTORATION})")
    bounded.add_argument("--no-tests", action="store_true", help="let COMMAND exit 0 though it reports a test count "
                         "and ran no test; without it, such a run is unmeasured (exit 3)")
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
        if args.action == "status":
            status(record)
        elif args.action == "declare":
            if args.clear and (args.reach or args.cable_on or args.absent):
                raise ValueError("--clear removes the declaration, and takes no --reach, --cable-on or --absent")
            if not args.clear and not (args.reach and args.absent):
                raise ValueError("a declaration names --reach and --absent")
            macs = [reach.mac_here(), *(name for name, _host in reach.farm_macs())]
            declaration = record.declare(args.key, by=args.by, clear=args.clear, cable_on=args.cable_on,
                                         absent=args.absent, macs=macs,
                                         reach_ways=[way.strip() for way in (args.reach or "").split(",")])
            print(json.dumps({"key": args.key, "declaration": declaration}, indent=2))
        elif args.action == "reserve":
            entry = record.reserve(args.key, owner=args.owner, revision=args.revision,
                purpose=args.purpose, release_by=args.release_by, device=args.device, title=args.title,
                details=reservations.fields(args.set), approval=reservations.given_approval(args))
            reservations.report(args.key, silence_notifications(record, args.key, owner=args.owner,
                                                                revision=entry["revision"], said=said))
        elif args.action == "take":
            if not args.owner:
                raise ValueError(f"name the session: --owner, or {OWNER}")
            entry = record.take(args.key, owner=args.owner, purpose=args.purpose,
                                release_by=args.release_by, timeout=args.wait, title=args.title,
                                details=reservations.fields(args.set))
            reservations.report(args.key, silence_notifications(record, args.key, owner=args.owner,
                                                                revision=entry["revision"], said=said))
        elif args.action == "release":
            # A run whose process is proven gone ends first, so that its holder releases it rather than an attended
            # recovery (vocaro/queuekeeper#230); the revision read before names the entry that recovery ended.
            entry = record.entry(args.key) or {}
            if entry.get("ownerChatID") == args.owner and entry.get("revision") == args.revision and \
                    condition(entry) == "orphan":
                from . import upkeep
                upkeep.upkeep(record, keys=[args.key], said=said)
                after = record.entry(args.key) or {}
                if condition(after) == "held" and after.get("ownerChatID") == args.owner:
                    entry, args.revision = after, after.get("revision")
            # The owner's notifications first, when the entry is still the caller's at the revision named.
            if entry.get("ownerChatID") == args.owner and entry.get("revision") == args.revision and \
                    not entry.get("activeRun"):
                args.revision = restore_notifications(record, args.key, owner=args.owner, revision=args.revision,
                                                      said=said)["revision"]
            return reservations.act(args, record)
        elif args.action == "recover":
            return reservations.act(args, record, restore=restore_for_recovery, said=said)
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
            reservations.report(args.key, entry)
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
                         f"reach this run's transcript and move the screen reader's focus; discard a session that picks one "
                         f"up. Run `devices notifications silence {args.key}` first, or "
                         f"{platforms.of(run.devices[args.key]).ask(args.key)}")
            kept = []
            with processes.forwarding_signals():
                try:
                    code = watched_run(run, args.key, command, {"notifications": notified} if notified else {},
                                       said, kept=kept)
                except processes.TimedOut as error:
                    print(f"devices: {error}; {args.key} stays reserved", file=sys.stderr)
                    return TIMED_OUT
                except Unmeasured:
                    print(f"devices: the run is unmeasured; {args.key} stays reserved", file=sys.stderr)
                    return UNMEASURED
            print(f"devices: {command[0]} exited with status {code}; {args.key} stays reserved", file=sys.stderr)
            # A test run whose selection matched no test passes nothing (vocaro/queuekeeper#100).
            empty = no_tests(kept[-1]) if code == 0 and kept and not args.no_tests else None
            if empty:
                print(f"devices: unmeasured: {empty}, so its selection matched no test and its status 0 measures "
                      f"nothing; check the selection, such as -only-testing:, or pass --no-tests to a run meant to "
                      f"select none", file=sys.stderr)
                return UNMEASURED
            # A command a signal ended has a negative status; a shell reports it as 128 plus the signal.
            return 128 - code if code < 0 else code
        else:
            return reservations.act(args, record)
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        print(f"devices: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
