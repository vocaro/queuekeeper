"""One device handoff record for the shared host, changed only under its lock. The record itself, and its
mac-foreground entry, are the Mac desktop extension's (queuekeeper.mac_desktop.reservations); this module adds what the
record holds of devices, and has its command line.

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
exception is a simulator pool's reservation that `qk simulator-pool reap` proves dead, every process
it records gone and its simulator shut down, which reap releases as a recovery by reap: once its
expected release has passed, or before then when only its holder held it (ALONE) and nothing accounts
for its owner, no live admission or other process naming the owner or the device (offload_host.release_dead;
vocaro-engineering#156).

The mac-foreground entry reserves a Mac's screen, keyboard, pointer, focus and speech. It names no
device and, like a physical device's, records no approval (owner's decision, 2026-11-06,
vocaro-engineering#207): the record schedules it, one holder at a time, and its expected release
is its window. On this Mac, the owner's workstation, a session agrees the window with the owner
first and reserves it for that window; foreground actions are refused once the expected release
has passed. The second Mac is not a workstation: queuekeeper reserves its entry for each run without
asking (foreground.slot), and a run there may outlast its expected release. Foreground drivers
call require_foreground before acting; release waits for the processes the session recorded as
its own, and says whether the previous frontmost app was restored.

Each Mac's simulators are its own, but a paired iPhone or iPad is reachable from both Macs, so a
physical device is reserved in one record only: the Mac that sends runs to the second Mac. The
second Mac's record defers physical devices to that Mac and refuses to reserve one. A run there
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
reached as a banner is unmeasured and run once more; so does `qk remote --device`, from the sending Mac (#192).
"""
import argparse
import codecs
from datetime import datetime, timedelta, timezone
import glob
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time

from . import device_notifications
from .. import offload_host, processes
from ..mac_desktop import reservations
from ..mac_desktop.reservations import ABOUT, DEFERRED, FOREGROUND, FRONTMOST, MANAGED, OVERRIDE, OWNER, POLL, QUEUES, SCHEMA, \
    Refused, Stale, _owned, approval_ended, approval_refusal, condition, default_owner, default_path, moment, now, \
    record_path, require_foreground, require_window, restart_emptied, running, waiting, window_ended  # noqa: F401
from ..lane_results import UNMEASURED, counted
from ..resources import process_start

# Earlier ad hoc records. Status names one whose current bytes were never imported.
LEGACY = "/private/tmp/*device-handoff*.json"
UDID = re.compile(r"[0-9A-F]{8}(?:-[0-9A-F]{4,16}){1,4}|[0-9A-F]{40}")
# A physical device's hardware UDID: 8 and 16 hex digits, or 40 on older devices. A simulator's is a UUID.
PHYSICAL = re.compile(r"[0-9A-F]{8}-[0-9A-F]{16}|[0-9A-F]{40}")
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
LEASE = "QUEUEKEEPER_DEVICE_LEASE"
# A lease's timing report, passed only to the top-level work on the second Mac.
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
# The paired devices `take` waits in line for, by their entries' keys.
PAIRED = ("iPad", "iPhone")
# The owner's word that he made the notification shortcuts on a paired device, by its entry's key, kept
# beside the entries with the device it names (device_notifications).
SETUP = "notificationSetup"
# What the entry's notifications record says of a run's silencing that let it start protected: queuekeeper ran the
# owner's shortcut, or the owner turned Do Not Disturb on himself.
SILENCED = ("requested", "owner")
# The process that reserved a simulator pool member and holds it, {"pid", "start"}: while it runs
# as one of a live run's processes, the second Mac's census of leftover processes counts the
# member's simulator as that run's (offload_host.leftovers).
HOLDER = "holderProcess"
# Set beside HOLDER when that process alone holds the reservation, as a run's pool records it and
# `qk simulator-pool take` never does: reap may release such an entry before its expected release once
# its processes are gone and nothing accounts for its owner (offload_host.ALONE; vocaro-engineering#156).
ALONE = offload_host.ALONE
TIMES = ("leaseRevision", "releasedAt", "reservedAt", "leasedAt", "updatedAt")
EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def physical(udid):
    """Whether udid is a physical device's hardware UDID rather than a simulator's."""
    return isinstance(udid, str) and PHYSICAL.fullmatch(udid.upper()) is not None


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


def unrecoverable_restoration(entry):
    """Why the run's own runner may not restore VoiceOver over this mac-foreground entry's uncertainty, or None.

    It may only when the uncertainty is a VoiceOver restoration a session could not verify and nothing else: the
    entry is uncertain with no other reason, its voiceOverRestoration says unverified and was the last thing written
    to it, no earlier recovery of it failed, the run that holds it is still alive, and the original VoiceOver state
    was recorded under this same lease (owner's decision, 2026-11-12, vocaro/EPUBLib#416)."""
    if not entry.get("uncertain"):
        return "it is not uncertain"
    if entry.get("uncertainReason") or entry.get("state") == "restoration-unverified":
        return f"its uncertainty is not only a VoiceOver restoration: {entry.get('uncertainReason') or entry.get('state')}"
    marker = entry.get("voiceOverRestoration")
    if not isinstance(marker, dict) or marker.get("verified") is not False:
        return "its uncertainty is not an unverified VoiceOver restoration"
    if marker.get("recoveryFailed"):
        return f"a recovery of its VoiceOver restoration already failed: {marker['recoveryFailed'].get('reason')}"
    if marker.get("revision") != entry.get("revision"):
        return "it changed after its VoiceOver restoration was marked unverified"
    pid, start = entry.get("hostProcessIdentifier"), entry.get("hostProcessStart")
    if not entry.get("activeRun") or not (isinstance(pid, int) and pid > 0 and start is not None
                                          and process_start(pid) == start):
        return "the run that holds it is no longer alive"
    original = entry.get("voiceOverOriginal")
    if not isinstance(original, dict) or type(original.get("enabled")) is not bool:
        return "it records no original VoiceOver state"
    if (original.get("reservedAt"), original.get("hostProcessIdentifier"), original.get("hostProcessStart")) != \
            (entry.get("reservedAt"), pid, start):
        return "its original VoiceOver state was recorded under another lease"
    return None


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


class Record(reservations.Record):
    """The record with what the Apple extension keeps in it: physical devices, which one Mac's record holds for
    both, the line a session waits in for a paired one, and the owner's notifications on it."""

    def _named(self, device):
        if not UDID.fullmatch(device):
            raise ValueError("name the device by its canonical uppercase UDID, never a name or 'booted'")

    def _placed(self, key, identifier, record):
        if record.get(DEFERRED) and physical(identifier):
            raise deferred_refusal(record[DEFERRED], key)

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

    def restoration_recovered(self, key, *, owner, revision, by):
        """End the uncertainty an unverified VoiceOver restoration left in owner's live mac-foreground entry, once
        the run's own runner set the recorded original again and saw it hold (voiceover.state.recover_restoration).
        The entry stays held by the run; its voiceOverRestoration records who recovered it and when. Refused, with
        nothing changed, for any other uncertainty (unrecoverable_restoration) or an entry that moved past
        revision."""
        if key != FOREGROUND:
            raise ValueError(f"only {FOREGROUND} records a VoiceOver restoration")
        if not by:
            raise ValueError("the recovery records who restored VoiceOver")

        def change(current, _record):
            _owned(key, current, owner)
            problem = unrecoverable_restoration(current)
            if problem:
                raise Refused(f"{key} cannot be recovered by its run: {problem}. Nothing was changed")
            entry = {name: value for name, value in current.items() if name != "uncertain"}
            entry["voiceOverRestoration"] = {**current["voiceOverRestoration"], "verified": True, "recoveredBy": by,
                                             "at": now(), "original": current["voiceOverOriginal"]["enabled"]}
            return entry
        return self._change(key, revision, change)

    def _checked_elsewhere(self, key, revision, unchecked, users):
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
            from ..remote import device_users as users
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


def git_object(word, root=None):
    """Whether the Git of root's checkout, by default the current directory's, knows word as an object.
    A device's UDID never is one; Git that cannot answer in time knows nothing."""
    from ..remote import environment
    try:
        return subprocess.run(["git", "cat-file", "-e", word], cwd=root, capture_output=True, env=environment(),
                              timeout=OBJECT_TIMEOUT).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def named(words, known=(), root=None, *, commits=()):
    """The device identifiers words name, uppercased: every hardware UDID, and each of known that appears.

    Forty hex digits are an older device's UDID, a commit's whole SHA-1 or a code-signing identity's,
    so such a word that a signing identity's setting or option takes (SIGNING), or that the Git of
    root's checkout knows as an object (git_object), names no device. Explicitly verified foreign
    commits exempt only a whole argument, never an identifier in a device option or shell string.
    Known devices remain devices even when their identifiers also name Git objects."""
    words = [str(word) for word in words]
    text = " ".join(words).upper()
    commits = {commit.upper() for commit in commits}
    # Retain a placeholder rather than deleting a word: deleting a signing identity's value
    # could make its option appear to take the next argument instead. An option's value remains
    # subject to the ordinary check; the declaration is for bare positional commit arguments.
    scanned = " ".join("_" if word.upper() in commits and not (
        index and words[index - 1].startswith("-") and words[index - 1] != "--") else word
        for index, word in enumerate(words)).upper()
    found = {match.group() for match in NAMED_PHYSICAL.finditer(scanned) if len(match.group()) != 40 or not (
        SIGNING.search(scanned, 0, match.start()) or git_object(match.group(), root))}
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
            self.revisions = {}
            self.devices, self.deadline = self._held(keys, entries, revision)
        held = set(self.devices.values())
        for udid in sorted(named(words, [entry.get("device") for entry in entries.values()], root, commits=commits) - held):
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
    notification presented as a banner; its exit status and what the watch saw, which the entry's run records
    (device_notifications.Watch, Together). With kept, a list, the command's output is shown through a
    Transcript and its text appended to kept."""
    keys = keyed(key)
    watches = {each: device_notifications.watch(run.devices[each]) for each in keys}
    watch = watches[keys[0]] if len(keys) == 1 else device_notifications.Together(watches)
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
    test it counts skipped or none ran (lane_results.counted, guides/parallel-tests.md). None when it ran a test or
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

    Such a run is unmeasured: its transcript may hold the notification and VoiceOver's cursor may have moved. Its
    rerun's status is the run's; a rerun that one reaches too, or no time left for one, raises Unmeasured. A run
    whose device log could not be read is said to be unwatched and keeps its status."""
    keys = keyed(key)
    code, seen = watched(run, keys, command, extra, cwd, env, kept)
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
    code, seen = watched(run, keys, command, {**extra, "rerunOf": {**first, "measured": False}}, cwd, env, kept)
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
          f"{FOREGROUND}: free; " + ("queuekeeper reserves it for each run" if value.get(DEFERRED) else
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
    parser = argparse.ArgumentParser(prog="python3 -m queuekeeper.apple.devices",
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
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        print(f"devices: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
