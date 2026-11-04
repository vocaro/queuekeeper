"""A project's simulator pool: take, create and prune its members under the host's device record.

A member is a simulator named `POOL LABEL N`, such as `Pagepour Duo 2`; its kind is its device type
and runtime. Taking one reserves a shut-down member whose record entries are released, observes it
still shut down and erases it, which keeps its UDID and the simulator tool's approval. When no
member of the kind is free, the next one is created under the naming scheme, taking the lowest free
number, and stays in the pool: a clone of the kind's warmed template, `POOL LABEL template (RUNTIME)`,
which is made, or remade when the runtime's installed builds change, under its own reservation
(template.py). A take with keep_state reserves and boots a member without erasing it. Pruning deletes
a member unused for 14 days, never the lowest-numbered of its kind, and records the deletion
(guides/simulator-testing.md, Reserved simulator pool). Nothing else here deletes or renames a
simulator, except a template whose runtime moved, which is replaced. Reaping shuts down a member left
booted past its time (offload_host.reap; guides/shared-host.md, Processes left over from runs).
Checking a UDID refuses any device outside the pool, such as one of Xcode's default devices, which a
script that picks a device by name finds instead of a member (vocaro-engineering#91); checking it
as held also refuses a member that the caller holds no live reservation of (vocaro-engineering#122).

    eng simulator-pool list|take|release|prune|reap|check [--pool NAME] ...; eng simulator-pool -h for the rest
"""
import argparse
import contextlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import plistlib
import re
import signal
import subprocess
import sys
import uuid

from .. import devices as device_record, evidence
from ..resources import process_start
from . import lifecycle, template as warmed

PRUNE_AFTER = timedelta(days=14)
DEVICE_TYPE = "com.apple.CoreSimulator.SimDeviceType."
RUNTIME = "com.apple.CoreSimulator.SimRuntime."
NUMBER = re.compile(r"(?P<label>.+) (?P<number>[1-9][0-9]*)")
# A kind's warmed template, `POOL LABEL template (RUNTIME)`, which NUMBER never matches.
TEMPLATE = re.compile(r"(?P<label>.+) template \((?P<runtime>[^()]+)\)")
PRESERVATION = "Synthetic test state; the next holder may erase this pool member"
# What a member taken without an erase holds, for a lane that needs no fresh installation.
KEPT_STATE = ("Synthetic test state kept from earlier holders, not erased: their apps, app data and settings, and "
              "its finished first boot; the next holder may erase this pool member")
# A new member cloned from its kind's warmed template.
CLONED = ("Synthetic test state: a clone of the warmed template {template}, booted once on runtime build {build} "
          "with VoiceOver started, and nothing installed; the next holder may erase this pool member")
# How long a reservation is expected to last; an overdue one releases nothing.
HOLD = timedelta(hours=8)


def qualified(prefix, name):
    return name if name.startswith("com.apple.") else prefix + name


def simctl(*arguments, timeout=600):
    """xcrun simctl's result, never raising for its status. A command past its limit reads 124."""
    try:
        return subprocess.run(["xcrun", "simctl", *arguments], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(arguments, 124, "", f"timed out after {timeout} s")


default_owner = device_record.default_owner


def default_devices(directory=None):
    """The UDIDs CoreSimulator's device set names as Xcode's default device for each runtime.

    The set keeps naming a default after it is deleted, so only a device simctl lists is one."""
    directory = Path(directory) if directory else Path.home() / "Library/Developer/CoreSimulator/Devices"
    try:
        with open(directory / "device_set.plist", "rb") as handle:
            defaults = plistlib.load(handle).get("DefaultDevices", {})
    except (OSError, plistlib.InvalidFileException, ValueError, AttributeError):
        return frozenset()
    return frozenset(udid.upper() for types in defaults.values() if isinstance(types, dict)
                     for udid in types.values() if isinstance(udid, str))


def listed_defaults(listing, defaults=None):
    """Xcode's default devices a `simctl list devices -j` listing holds, as `NAME (RUNTIME, UDID)`."""
    defaults = default_devices() if defaults is None else defaults
    return sorted(f"{entry.get('name')} ({runtime.rsplit('.', 1)[-1]}, {entry['udid'].upper()})"
                  for runtime, entries in listing.get("devices", {}).items() for entry in entries
                  if str(entry.get("udid", "")).upper() in defaults)


def canonical(udid):
    """A simulator's canonical uppercase UDID; ValueError for a name, `booted` or anything else."""
    try:
        return str(uuid.UUID(udid)).upper()
    except (ValueError, TypeError, AttributeError):
        raise ValueError(f"{udid!r} is not a simulator UDID: pass a pool member's UDID, never a name or `booted`, "
                         "which can select one of Xcode's default devices instead") from None


def configured(root):
    """The pool a project's engineering.json names, or None."""
    try:
        return json.loads((Path(root) / "engineering.json").read_text()).get("simulatorPool")
    except (OSError, ValueError, AttributeError):
        return None


def instant(value):
    return device_record.moment(value)


@dataclass(frozen=True)
class Member:
    name: str
    udid: str
    label: str
    number: int
    device_type: str
    runtime: str
    state: str
    last_used: datetime = None
    available: bool = True

    @property
    def kind(self):
        return self.device_type, self.runtime


def members(pool, listing):
    """Every member of the pool in a `simctl list devices -j` listing, by kind and then number."""
    found = []
    for runtime, entries in listing.get("devices", {}).items():
        for entry in entries:
            name = entry.get("name", "")
            match = NUMBER.fullmatch(name[len(pool) + 1:]) if name.startswith(pool + " ") else None
            if match is None: continue
            found.append(Member(name, entry["udid"].upper(), match["label"], int(match["number"]),
                                entry.get("deviceTypeIdentifier", ""), runtime, entry.get("state", ""),
                                instant(entry.get("lastUsedAt") or entry.get("lastBootedAt")),
                                entry.get("isAvailable", True)))
    return sorted(found, key=lambda member: (member.kind, member.number))


@dataclass(frozen=True)
class Template:
    """A kind's warmed template: never a member, never handed to a lane, only cloned."""
    name: str
    udid: str
    label: str
    device_type: str
    runtime: str
    state: str
    available: bool = True

    @property
    def kind(self):
        return self.device_type, self.runtime


def template_name(pool, label, runtime):
    return f"{pool} {label} template ({runtime.rsplit('.', 1)[-1]})"


def templates(pool, listing):
    """Every warmed template of the pool in a `simctl list devices -j` listing, by kind."""
    found = []
    for runtime, entries in listing.get("devices", {}).items():
        for entry in entries:
            name = entry.get("name", "")
            match = TEMPLATE.fullmatch(name[len(pool) + 1:]) if name.startswith(pool + " ") else None
            if match is None or match["runtime"] != runtime.rsplit(".", 1)[-1]: continue
            found.append(Template(name, entry["udid"].upper(), match["label"], entry.get("deviceTypeIdentifier", ""),
                                  runtime, entry.get("state", ""), entry.get("isAvailable", True)))
    return sorted(found, key=lambda template: (template.kind, template.name))


def template_current(template, entry, builds):
    """(True, why) when the pool may clone new members from the template, else (False, why)."""
    details = (entry or {}).get("template") or {}
    if (entry or {}).get("device") != template.udid or details.get("udid") != template.udid:
        return False, "it was never warmed to the end"
    # VoiceOver's setting is in the data container: a clone of a template it was left on in boots with it
    # on, and speaks through the Mac's output once the volume comes back. A record from before
    # voiceOverOff was written says so in its voiceOver.
    off = details.get("voiceOverOff")
    if off is None: off = "could not be confirmed off" not in str(details.get("voiceOver") or "")
    if not off:
        return False, "VoiceOver could not be confirmed off, so a clone would boot with it on"
    if not template.available:
        return False, "its runtime is no longer installed"
    if builds is not None and details.get("installedBuilds") != builds:
        return False, (f"the runtime moved: warmed on {details.get('runtimeBuild')} among builds "
                       f"{', '.join(details.get('installedBuilds') or []) or 'none'}, now {', '.join(builds) or 'none'}")
    return True, f"warmed on runtime build {details.get('runtimeBuild')} at {details.get('warmedAt')}: {details.get('voiceOver')}"


def entries_for(member, record):
    """The record's entries for a member: its name's, and any other key naming its UDID."""
    return {key: entry for key, entry in record.get("devices", {}).items() if isinstance(entry, dict) and
            (key == member.name or str(entry.get("device") or "").upper() == member.udid)}


def prunable(member, pool_members, entries, now):
    """(True, reason) when the pruning rule lets a session delete the member, else (False, reason)."""
    kind = [other for other in pool_members if other.kind == member.kind]
    if member.number == min(other.number for other in kind):
        return False, "the lowest-numbered member of its kind, which keeps the kind and its approval"
    if member.state != "Shutdown":
        return False, f"{member.state}: a session is using it"
    if member.last_used is None:
        return False, "simctl records no last use"
    if now - member.last_used <= PRUNE_AFTER:
        return False, f"last used {member.last_used:%Y-%m-%d}, within {PRUNE_AFTER.days} days"
    for key, entry in entries.items():
        if device_record.condition(entry) != "released":
            return False, f"its record entry {key} is {device_record.condition(entry)}"
        released = instant(entry.get("releasedAt"))
        if released is None or now - released <= PRUNE_AFTER:
            return False, f"its record entry {key} was released {entry.get('releasedAt') or 'at an unrecorded time'}"
    gone = "" if member.available else "; its runtime is no longer installed"
    return True, f"unused since {member.last_used:%Y-%m-%d}, more than {PRUNE_AFTER.days} days{gone}"


@dataclass
class Lease:
    """A member this owner holds: its record key and the revision its last change left. kept says the take
    did not erase it, and cloned names the template a new member was cloned from."""
    key: str
    udid: str
    revision: int
    created: bool = False
    kept: bool = False
    cloned: str = None


class Pool:
    def __init__(self, name, *, owner, record=None, run=None, log=None, clock=None, warm=True):
        """warm warms a new template, by default template.warm; None creates new members instead of cloning them."""
        if not name:
            raise ValueError("a pool needs its name")
        self.name, self.owner, self.run = name, owner, run or simctl
        self.record = record or device_record.Record()
        self.log = log or (lambda message: print(message, file=sys.stderr, flush=True))
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.warm = warmed.warm if warm is True else warm

    def listing(self):
        result = self.run("list", "devices", "-j", timeout=120)
        if result.returncode != 0:
            raise RuntimeError(f"simctl list failed with status {result.returncode}: {result.stderr.strip()}")
        return json.loads(result.stdout)

    def members(self, listing=None):
        return members(self.name, listing or self.listing())

    def check(self, udid, listing=None, defaults=None):
        """The member this UDID names. Any other device's UDID, or anything but a UDID, is refused."""
        udid = canonical(udid)
        listing = listing or self.listing()
        member = next((member for member in self.members(listing) if member.udid == udid), None)
        if member is not None:
            return member
        entry = next((entry for entries in listing.get("devices", {}).values() for entry in entries
                      if str(entry.get("udid", "")).upper() == udid), None)
        if entry is None:
            raise RuntimeError(f"simctl lists no simulator {udid}")
        kind = "one of Xcode's default devices" if udid in (default_devices() if defaults is None else defaults) \
            else "the pool's warmed template, which is only cloned, never tested on, and a device" \
            if any(template.udid == udid for template in templates(self.name, listing)) else "a device"
        raise RuntimeError(f"{entry.get('name')} ({udid}) is {kind} outside the {self.name} simulator pool: take a "
                           "member with `eng simulator-pool take --device TYPE --runtime RUNTIME` and pass its UDID "
                           "(guides/simulator-testing.md, Reserved simulator pool)")

    def check_held(self, udid, listing=None, defaults=None):
        """The member this UDID names, when this owner holds a live reservation of it: its record entry,
        under the member's name or any key naming its UDID, is held or in an active run for this owner.
        Anything check refuses is refused, and so is a member that is unreserved, held by another owner,
        or only in an orphaned or uncertain reservation, which a recovery settles first."""
        member = self.check(udid, listing, defaults)
        if not self.owner:
            raise ValueError(f"name the owner whose reservation holds {member.name}: --owner, "
                             f"${device_record.OWNER} or the agent's session")
        entries = entries_for(member, self.record.load())
        conditions = {key: device_record.condition(entry) for key, entry in entries.items()}
        if any(entry.get("ownerChatID") == self.owner and str(entry.get("device") or "").upper() == member.udid
               and conditions[key] in {"held", "active"} for key, entry in entries.items()):
            return member
        live = {key: entry for key, entry in entries.items() if conditions[key] != "released"}
        ours = [key for key, entry in live.items() if entry.get("ownerChatID") == self.owner]
        if ours:
            why = f"its reservation {ours[0]} is {conditions[ours[0]]}: recover it before using the member"
        elif live:
            key, entry = next(iter(live.items()))
            why = f"{entry.get('ownerChatID') or 'another session'} holds it ({key})"
        else:
            why = "nobody holds it"
        raise device_record.Refused(f"{member.name} ({member.udid}) is a {self.name} pool member that {self.owner} "
                                    f"holds no reservation of; {why}. Take one with `eng simulator-pool take "
                                    "--device TYPE --runtime RUNTIME` and pass the UDID it prints "
                                    "(guides/simulator-testing.md, Reserved simulator pool)")

    def state(self, udid):
        """A fresh observation of this exact device, or None when simctl no longer lists it."""
        return next((entry.get("state") for entries in self.listing().get("devices", {}).values()
                     for entry in entries if entry["udid"].upper() == udid), None)

    def label(self, device_type):
        """A new member's label: its device type's display name, such as iPhone 17 Pro."""
        result = self.run("list", "devicetypes", "-j", timeout=120)
        try:
            names = {item["identifier"]: item["name"] for item in json.loads(result.stdout)["devicetypes"]}
        except (ValueError, KeyError, TypeError):
            names = {}
        return names.get(device_type) or device_type.rsplit(".", 1)[-1]

    def installed_builds(self, runtime):
        """The builds simctl lists installed for a runtime identifier, sorted; None when it cannot list them."""
        result = self.run("list", "runtimes", "-j", timeout=120)
        try:
            entries = json.loads(result.stdout)["runtimes"]
            return sorted({entry["buildversion"] for entry in entries if entry.get("identifier") == runtime
                           and entry.get("isAvailable", True) and entry.get("buildversion")})
        except (ValueError, KeyError, TypeError):
            return None

    def kind_label(self, device_type, runtime, found=None):
        """The pool's own label for the kind, else for the device type, else the type's display name."""
        found = self.members() if found is None else found
        return next((member.label for member in found if member.kind == (device_type, runtime)), None) \
            or next((member.label for member in found if member.device_type == device_type), None) \
            or self.label(device_type)

    def templates(self, listing=None):
        """Each warmed template with whether new members may be cloned from it, and why."""
        listing = listing or self.listing()
        record, builds = self.record.load(), {}
        return [(template, *template_current(template, record["devices"].get(template.name),
                                             builds.setdefault(template.runtime, self.installed_builds(template.runtime))))
                for template in templates(self.name, listing)]

    def template(self, device_type, runtime, label, purpose, *, boot_timeout=warmed.TAKE_BOOT,
                 voiceover_limit=warmed.TAKE_VOICEOVER_LIMIT, require_voiceover=False):
        """A held, shut-down, current template of the kind to clone a new member from, warming one first
        when the kind has none current, as when its runtime's installed builds changed; None when there is
        none to clone, such as while another session holds the template, and the member is created.

        A take warms within the shorter template.TAKE_* limits, so that a kind's first creation does not
        eat a lane's time; require_voiceover also replaces a current template whose VoiceOver stayed
        cold, as `eng simulator-pool template` does with the full limits."""
        if self.warm is None: return None
        device_type, runtime = qualified(DEVICE_TYPE, device_type), qualified(RUNTIME, runtime)
        builds = self.installed_builds(runtime)
        if not builds:
            self.log(f"no installed build of {runtime} could be read, so no template is warmed or cloned")
            return None
        key = template_name(self.name, label, runtime)
        found = [template for template in templates(self.name, self.listing()) if template.name == key]
        if len(found) > 1:
            self.log(f"{len(found)} simulators are named {key}; none is cloned or replaced")
            return None
        existing = found[0] if found else None
        current = self.record.entry(key)
        if current is not None and device_record.condition(current) != "released":
            self.log(f"{key} is {device_record.condition(current)} by {current.get('ownerChatID')}, so the new "
                     "member is created instead")
            return None
        current_now, why = template_current(existing, current, builds) if existing is not None else (False, None)
        if current_now and require_voiceover and not ((current or {}).get("template") or {}).get("voiceOverWarmed"):
            current_now, why = False, "its VoiceOver was not warmed"
        usable = existing is not None and existing.kind == (device_type, runtime) and current_now
        details = {"template": current["template"]} if usable else {}
        try:
            revision = self._reserve(key, f"{'Clone' if usable else 'Warm'} the template for {purpose}",
                                     existing.udid if existing else None,
                                     revision=(current or {}).get("revision", 0), **details)
        except device_record.Refused as error:
            self.log(f"{key}: {error}; the new member is created instead")
            return None
        lease = Lease(key, existing.udid if existing else None, revision)
        try:
            # A fresh observation under the reservation: never clone, delete or boot a device booted since.
            if existing is not None and self.state(existing.udid) != "Shutdown":
                self.log(f"{key} is no longer shut down; its reservation is kept for recovery")
                return None
            if usable:
                return lease
            if existing is not None:
                if existing.kind != (device_type, runtime): why = f"it is a {existing.device_type}, not a {device_type}"
                deleted = self.run("delete", existing.udid, timeout=300)
                if deleted.returncode != 0 or self.state(existing.udid) is not None:
                    self.log(f"{key} ({existing.udid}) could not be replaced: simctl delete exited {deleted.returncode}")
                    self.release(lease, outcome=f"Not replaced: simctl delete exited {deleted.returncode}",
                                 shutdown=False)
                    return None
                self.log(f"REPLACED the template {key} ({existing.udid}): {why}")
                lease.revision = self.record.update(key, owner=self.owner, revision=lease.revision, device=None,
                                                    replaced={"udid": existing.udid, "why": why,
                                                              "at": self.clock().isoformat()})["revision"]
                lease.udid = None
            created = self.run("create", key, device_type, runtime, timeout=300)
            udid = created.stdout.strip().upper() if created.returncode == 0 else None
            if udid is None:
                stored = [entry["udid"].upper() for entries in self.listing().get("devices", {}).values()
                          for entry in entries if entry.get("name") == key]
                if len(stored) != 1:
                    self.log(f"simctl create {key} failed with status {created.returncode}: {created.stderr.strip()}")
                    if not stored:
                        self.record.release(key, owner=self.owner, revision=lease.revision,
                                            outcome="Not warmed: the template was not created",
                                            restoration="Nothing was created")
                    return None
                udid = stored[0]
            lease.udid = udid
            lease.revision = self.record.update(key, owner=self.owner, revision=lease.revision, device=udid,
                                                createdAt=self.clock().isoformat())["revision"]
            self.log(f"WARMING the template {key} ({udid}): boot, then VoiceOver until it speaks")
            try:
                warmth = self.warm(udid, runtime, simctl=self.run, boot_timeout=boot_timeout,
                                   voiceover_limit=voiceover_limit)
            except warmed.WarmFailed as error:
                self.log(f"{key} ({udid}) was not warmed: {error}")
                self.release(lease, outcome=f"Not warmed: {error}")
                return None
            lease.revision = self.record.update(key, owner=self.owner, revision=lease.revision, template={
                "udid": udid, "deviceType": device_type, "runtime": runtime, "installedBuilds": builds,
                "warmedAt": self.clock().isoformat(), **warmth})["revision"]
            self.run("shutdown", udid, timeout=300)
            if self.state(udid) != "Shutdown":
                self.release(lease, outcome="Warmed, but its shutdown could not be confirmed", shutdown=False)
                return None
            clonable, reason = template_current(Template(key, udid, label, device_type, runtime, "Shutdown"),
                                                self.record.entry(key), builds)
            if not clonable:
                # Never cloned: the next creation replaces it.
                self.log(f"{key} ({udid}) is not cloned: {reason}")
                self.release(lease, outcome=f"Warmed, not cloned: {reason}", shutdown=False)
                return None
            self.log(f"WARMED the template {key} ({udid}) on runtime build {warmth['runtimeBuild']}: "
                     f"{warmth.get('voiceOver')}")
            return lease
        except BaseException as error:
            if lease.udid is not None:
                self.release(lease, outcome=f"Stopped while preparing the template for {purpose}")
            else:
                # Stopped before a UDID was recorded, as during simctl create or after a stale template's
                # delete: release the name unless a device of that name now exists, which keeps it for
                # recovery, as create() does for a member.
                held = self.record.entry(key) or {}
                if held.get("ownerChatID") == self.owner and not held.get("device") and not self._named(key):
                    self.record.release(key, owner=self.owner, revision=held["revision"],
                                        outcome=f"Not warmed: {str(error) or type(error).__name__}",
                                        restoration="Nothing was created")
                else:
                    self.log(f"{key}: stopped with a device of its name stored but not recorded; its reservation "
                             "is kept for recovery")
            raise

    @staticmethod
    def _release_by():
        return (datetime.now(timezone.utc) + HOLD).isoformat()

    def _reserve(self, key, purpose, device=None, revision=None, **details):
        """Reserve a key at the revision read, by default its current one; the new revision. The
        entry names this process as its holder, by its PID and start, so that a census of leftover
        processes counts the booted member as a live run's while this process is one of that run's.
        The holder only helps that census, so a start that `ps` cannot give (a project's tests may run
        the pool with no `ps` on PATH) leaves it out rather than failing the reservation."""
        if not self.owner:
            raise ValueError(f"name the owner whose reservation takes {key}")
        if revision is None:
            current = self.record.entry(key)
            revision = current.get("revision", 0) if current else 0
        try:
            start = process_start(os.getpid())
        except OSError:
            start = None
        if start is not None:
            details[device_record.HOLDER] = {"pid": os.getpid(), "start": start}
        entry = self.record.reserve(key, owner=self.owner, revision=revision, device=device, purpose=purpose,
                                    release_by=self._release_by(), details=details)
        return entry["revision"]

    def take(self, device_type, runtime, *, purpose, label=None, preservation=None, prune=True, keep_state=False):
        """A free member of the kind, reserved and erased, or a new one; the caller boots it.

        keep_state reserves the member without erasing it, for a lane that needs no fresh installation:
        it keeps what earlier holders left, their apps and data among it, and boots without the first
        boot's data migration. Its preservation note says so."""
        device_type, runtime = qualified(DEVICE_TYPE, device_type), qualified(RUNTIME, runtime)
        if prune: self.prune(device_type, runtime)
        listing = self.listing()
        record = self.record.load()
        for member in self.members(listing):
            if member.kind != (device_type, runtime) or member.state != "Shutdown" or not member.available: continue
            if any(device_record.condition(entry) != "released" for entry in entries_for(member, record).values()):
                continue
            details = {"preservation": preservation or (KEPT_STATE if keep_state else PRESERVATION)}
            if keep_state: details["keptState"] = True
            try:
                revision = self._reserve(member.name, purpose, member.udid, **details)
            except device_record.Refused:
                continue  # Another session reserved it first, perhaps under another key.
            lease = Lease(member.name, member.udid, revision, kept=keep_state)
            try:
                # The listing predates the reservation. Never erase or boot a device booted since then.
                if self.state(member.udid) != "Shutdown":
                    self.log(f"{member.name} is no longer shut down; its reservation is kept for recovery")
                    continue
                erased = None if keep_state else self.run("erase", member.udid)
            except BaseException:
                # A stop before the lease is handed back releases it once the member is shut down.
                self.release(lease, outcome=f"Stopped before {purpose}", shutdown=False)
                raise
            if erased is not None and erased.returncode != 0:
                self.release(lease, outcome=f"Erase failed before {purpose}: {erased.stderr.strip()}", shutdown=False)
                continue
            self.log(f"TOOK {member.name} ({member.udid})" + (", not erased: it keeps its state" if keep_state else ""))
            return lease
        return self.create(device_type, runtime, purpose=purpose, label=label, preservation=preservation or PRESERVATION)

    def create(self, device_type, runtime, *, purpose, label=None, preservation=PRESERVATION):
        """The next member of the kind, reserved under the lowest free number; its first boot is the caller's."""
        device_type, runtime = qualified(DEVICE_TYPE, device_type), qualified(RUNTIME, runtime)
        label = label or self.kind_label(device_type, runtime)
        names = {entry.get("name") for entries in self.listing().get("devices", {}).values() for entry in entries}
        number = 0
        while True:
            number += 1
            key = f"{self.name} {label} {number}"
            current = self.record.entry(key)
            if key in names or (current is not None and device_record.condition(current) != "released"): continue
            try:
                revision = self._reserve(key, purpose, preservation=preservation, created=True)
            except device_record.Refused:
                continue
            break
        try:
            return self._create(key, revision, device_type, runtime, purpose=purpose, label=label,
                                preservation=preservation)
        except BaseException as error:
            # A stop or failure after the reservation and before a UDID was recorded: release the
            # name unless a device of that name now exists, which keeps it for recovery.
            current = self.record.entry(key) or {}
            if current.get("ownerChatID") == self.owner and not current.get("device") and \
                    not any(entry.get("name") == key for entries in self.listing().get("devices", {}).values()
                            for entry in entries):
                self.record.release(key, owner=self.owner, revision=current["revision"],
                                    outcome=f"Not created: {error or type(error).__name__}",
                                    restoration="Nothing was created")
            raise

    def _create(self, key, revision, device_type, runtime, *, purpose, label, preservation):
        # Another session may have created the name between the listing and the reservation.
        if any(entry.get("name") == key for entries in self.listing().get("devices", {}).values() for entry in entries):
            self.record.release(key, owner=self.owner, revision=revision, outcome="Not created: the name appeared",
                                restoration="Nothing was created")
            return self.create(device_type, runtime, purpose=purpose, label=label, preservation=preservation)
        source = self.template(device_type, runtime, label, purpose)
        if source is not None:
            details = (self.record.entry(source.key) or {}).get("template") or {}
            try:
                created = self.run("clone", source.udid, key, timeout=600)
            finally:
                # Cloning reads the shut-down template and changes nothing in it.
                self.release(source, outcome=f"Cloned {key} for {purpose}", shutdown=False)
            how = "simctl clone"
        if source is None or created.returncode != 0 and not self._named(key):
            if source is not None:
                self.log(f"simctl clone {source.key} {key} failed with status {created.returncode}: "
                         f"{created.stderr.strip()}; the member is created instead")
                source = None
            created = self.run("create", key, device_type, runtime, timeout=300)
            how = "simctl create"
        udid = created.stdout.strip().upper() if created.returncode == 0 else None
        if udid is None:
            # An interrupted create or clone can store the device without printing its UDID.
            found = self._named(key)
            if len(found) > 1:
                raise RuntimeError(f"{len(found)} simulators are named {key}; its reservation is kept for recovery")
            if not found:
                raise RuntimeError(f"{how} {key} failed with status {created.returncode}: "
                                   f"{created.stderr.strip()}")
            udid = found[0]
        changes = {"device": udid, "createdAt": self.clock().isoformat()}
        if source is not None:
            changes.update(clonedFrom={"template": source.key, "udid": source.udid,
                                       "runtimeBuild": details.get("runtimeBuild"), "warmedAt": details.get("warmedAt")},
                           preservation=CLONED.format(template=source.key, build=details.get("runtimeBuild")))
        revision = self.record.update(key, owner=self.owner, revision=revision, **changes)["revision"]
        if source is not None:
            self.log(f"CREATED {key} ({udid}), cloned from {source.key}, warmed on runtime build "
                     f"{details.get('runtimeBuild')}")
        else:
            self.log(f"CREATED {key} ({udid}); its first boot finishes before it is shut down")
        return Lease(key, udid, revision, created=True, cloned=source.key if source is not None else None)

    def _named(self, key):
        return [entry["udid"].upper() for entries in self.listing().get("devices", {}).values()
                for entry in entries if entry.get("name") == key]

    def release(self, lease, *, outcome, shutdown=True):
        """Shut the member down and release the unchanged reservation once a fresh observation
        confirms it; otherwise the reservation is kept for recovery and this returns False."""
        try:
            current = self.record.entry(lease.key)
            if current is None or current.get("revision") != lease.revision or \
                    current.get("ownerChatID") != self.owner or str(current.get("device")).upper() != lease.udid:
                raise device_record.Refused("the reservation changed")
            if shutdown: self.run("shutdown", lease.udid, timeout=300)
            state = self.state(lease.udid)
            if state != "Shutdown":
                raise device_record.Refused(f"its state is {state}, not Shutdown")
            self.record.release(lease.key, owner=self.owner, revision=lease.revision, outcome=outcome,
                                restoration="Confirmed the exact simulator is Shutdown")
        except (OSError, ValueError, RuntimeError, TimeoutError) as error:
            self.log(f"{lease.key} ({lease.udid}): reservation kept for recovery: {error}")
            return False
        return True

    def held(self, udid):
        """This owner's lease on a member, from the record, for a release by another process."""
        for key, entry in self.record.load()["devices"].items():
            if str(entry.get("device")).upper() == udid.upper() and entry.get("ownerChatID") == self.owner \
                    and device_record.condition(entry) != "released":
                return Lease(key, udid.upper(), entry["revision"], bool(entry.get("created")))
        raise device_record.Refused(f"{self.owner} holds no reservation of {udid}")

    def eligible(self, device_type=None, runtime=None, listing=None):
        """Each member with whether pruning may delete it, and why."""
        found, record, now = self.members(listing), self.record.load(), self.clock()
        return [(member, *prunable(member, found, entries_for(member, record), now)) for member in found
                if device_type is None or member.kind == (device_type, runtime)]

    def prune(self, device_type=None, runtime=None, udids=None):
        """Delete eligible members, the highest-numbered first, and return their names."""
        pruned = []
        candidates = [member for member, allowed, _ in self.eligible(device_type, runtime) if allowed
                      and (udids is None or member.udid in udids)]
        for member in sorted(candidates, key=lambda member: -member.number):
            # Reserve at the revision that was eligible, so a use since then refuses the reservation.
            entries = entries_for(member, self.record.load())
            found = self.members()
            if member not in found or not prunable(member, found, entries, self.clock())[0]: continue
            try:
                revision = self._reserve(member.name, "Prune an unused pool member", member.udid,
                                         revision=entries.get(member.name, {}).get("revision", 0))
            except device_record.Refused as error:
                self.log(f"{member.name}: not pruned: {error}")
                continue
            lease = Lease(member.name, member.udid, revision)
            # Check again under the reservation that simctl shows it shut down and unchanged.
            others = {key: entry for key, entry in entries_for(member, self.record.load()).items() if key != member.name}
            found = self.members()
            if member not in found or not prunable(member, found, others, self.clock())[0]:
                self.release(lease, outcome="Not pruned: it changed before deletion", shutdown=False)
                continue
            deleted = self.run("delete", member.udid, timeout=300)
            if deleted.returncode != 0 or self.state(member.udid) is not None:
                self.log(f"{member.name}: simctl delete failed: {deleted.stderr.strip()}")
                self.release(lease, outcome=f"Pruning failed: simctl delete exited {deleted.returncode}", shutdown=False)
                continue
            revision = self.record.update(member.name, owner=self.owner, revision=revision, pruned={
                "udid": member.udid, "at": self.clock().isoformat(), "deviceType": member.device_type,
                "runtime": member.runtime, "lastUsedAt": member.last_used.isoformat()})["revision"]
            self.record.release(member.name, owner=self.owner, revision=revision,
                                outcome=f"Pruned: deleted {member.udid}, unused since {member.last_used:%Y-%m-%d}",
                                restoration="simctl no longer lists the device")
            self.log(f"PRUNED {member.name} ({member.udid}), unused since {member.last_used:%Y-%m-%d}")
            pruned.append(member.name)
        return pruned


class BootTimedOut(TimeoutError):
    """A member's boot reached its limit, which leaves the work unmeasured; the member was released."""


class ReleaseUnconfirmed(RuntimeError):
    """A member's shutdown could not be confirmed, so its reservation is kept for recovery."""


@contextlib.contextmanager
def member(name, device_type, runtime, *, purpose, owner=None, boot_timeout=600, log=None, keep_state=False):
    """A booted member of the pool for the block, then shut down and released however it ends; keep_state
    takes it without erasing it (Pool.take).

    It raises BootTimedOut when the boot reaches its limit, RuntimeError when it fails, and
    ReleaseUnconfirmed after a block that ended normally when the release cannot confirm shutdown."""
    pool = Pool(name, owner=owner or default_owner() or f"{name}-{uuid.uuid4()}", log=log)
    lease = pool.take(device_type, runtime, purpose=purpose, keep_state=keep_state)
    status = "stopped"
    try:
        booted = boot(pool, lease, boot_timeout)
        if booted == evidence.UNMEASURED:
            status = "the boot reached its limit"
            raise BootTimedOut(f"{lease.key} did not finish booting within {boot_timeout}s")
        if booted is not None:
            status = f"the boot failed with status {booted}"
            raise RuntimeError(f"{lease.key} failed to boot (status {booted})")
        yield lifecycle.Simulator(lease.udid)
        status = "finished"
    finally:
        released = pool.release(lease, outcome=f"{purpose}: {status}")
    if not released:
        raise ReleaseUnconfirmed(f"{lease.key} could not be confirmed shut down; its reservation is kept for recovery")


def boot(pool, lease, timeout):
    """Boot a member to bootstatus -b; None when it booted, else a status (UNMEASURED for a timeout)."""
    started = pool.run("boot", lease.udid, timeout=120)
    if started.returncode != 0:
        return started.returncode
    finished = pool.run("bootstatus", lease.udid, "-b", timeout=timeout)
    return None if finished.returncode == 0 else evidence.UNMEASURED if finished.returncode == 124 else finished.returncode


class Stopped(BaseException):
    """SIGTERM or SIGHUP during a take, raised so that its cleanup releases what it holds."""
    def __init__(self, number):
        super().__init__(number)
        self.number = number


def stopped(number, _frame):
    raise Stopped(number)


def reap_reports(pool, project, *, dry_run, here):
    """offload_host.reap's report from this Mac and, unless here, from the second Mac, each with where it
    ran. pool alone when it is named, otherwise every pool the project and the checkouts each Mac can
    see name."""
    # remote and disk import this module.
    from .. import disk, offload_host, remote
    if pool: pools = [pool]
    else:
        pools = sorted({*disk.pools(Path.home() / "Development"), *filter(None, [configured(project or Path.cwd())])})
    request = {"pools": pools, "dry_run": dry_run, "scan": not pool}
    reports = [("this Mac", offload_host.reap(remote.ROOT, **request))]
    host = None if here else remote.configured()
    if host: reports.append((host, remote.reap_there(host, request)))
    return reports


def reap_command(pool, project, *, dry_run, here):
    """`eng simulator-pool reap`: reap_reports, said. It exits 1 when anything failed."""
    from .. import remote
    reports = reap_reports(pool, project, dry_run=dry_run, here=here)
    for where, report in reports:
        lines = remote.reap_lines(where, report)
        print("\n".join(lines) if lines else f"on {where}, no stale pool simulator")
    return 1 if any(report.get("problem") or report.get("failed") for _, report in reports) else 0


def main(argv, project=None):
    parser = argparse.ArgumentParser(prog="eng simulator-pool", description=__doc__.split("\n\n")[0])
    parser.add_argument("--pool", help="the pool's name, the start of every member's (default: the project's "
                        "simulatorPool in engineering.json)")
    parser.add_argument("--owner", help=f"the session reservations name (default: ${device_record.OWNER}, else the "
                        "agent's session)")
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("list", help="every member, its state and whether pruning may delete it")
    take = commands.add_parser("take", help="reserve, erase (unless --keep-state) and boot a free member of a "
                               "kind, or create the next; prints its UDID")
    take.add_argument("--device", required=True, metavar="TYPE", help="a device type, such as iPhone-17-Pro")
    take.add_argument("--runtime", required=True, help="a runtime, such as iOS-27-1")
    take.add_argument("--label", help="a new member's label (default: the device type's name)")
    take.add_argument("--purpose", default="Simulator tests")
    take.add_argument("--keep-state", action="store_true", help="reserve and boot the member without erasing it, "
                      "for a lane that needs no fresh installation: it keeps earlier holders' apps, data and "
                      "settings, and skips the first boot's migration")
    take.add_argument("--boot-timeout", type=int, default=600, help="seconds the boot may take (default 600); "
                      f"past it the member is released and the command exits {evidence.UNMEASURED}")
    release = commands.add_parser("release", help="shut a member you hold down and release it")
    release.add_argument("udid")
    release.add_argument("--outcome", default="Released")
    prune = commands.add_parser("prune", help="delete members unused for 14 days, never the lowest of a kind")
    prune.add_argument("udids", nargs="*", metavar="UDID", help="only these members")
    reap = commands.add_parser("reap", help="shut down members left booted for 2 hours that nothing accounts for, on "
                               "this Mac and the second Mac (guides/shared-host.md, Processes left over from runs)")
    reap.add_argument("--dry-run", action="store_true", help="say what it would stop and keep, and stop nothing")
    reap.add_argument("--here", action="store_true", help="only this Mac")
    template = commands.add_parser("template", help="warm the kind's template now, with VoiceOver's full start "
                                   "limit, or remake it when its runtime moved or its VoiceOver stayed cold, so that "
                                   "a lane's new members are cloned from it")
    template.add_argument("--device", required=True, metavar="TYPE", help="a device type, such as iPhone-17-Pro")
    template.add_argument("--runtime", required=True, help="a runtime, such as iOS-27-1")
    template.add_argument("--label", help="the kind's label (default: the pool's own, else the device type's name)")
    check = commands.add_parser("check", help="print a pool member's canonical UDID; exit 1 for any other device, "
                                "such as one of Xcode's default devices, a name or `booted`")
    check.add_argument("--held", action="store_true", help="also exit 1 unless the owner holds a live "
                       "reservation of the member, as `take` leaves it")
    check.add_argument("udid")
    args = parser.parse_args(argv)
    if args.action == "reap":
        try:
            return reap_command(args.pool, project, dry_run=args.dry_run, here=args.here)
        except (OSError, ValueError, RuntimeError) as error:
            print(f"eng simulator-pool: {error}; nothing further was stopped", file=sys.stderr)
            return 1
    name = args.pool or configured(project or Path.cwd()) or configured(Path.cwd())
    owner = args.owner or default_owner()
    if not name:
        parser.error("name the pool with --pool, or in engineering.json's simulatorPool")
    if not owner and (args.action not in {"list", "check"} or getattr(args, "held", False)):
        parser.error(f"name the owner with --owner or ${device_record.OWNER}")
    try:
        pool = Pool(name, owner=owner)
        if args.action == "list":
            listing = pool.listing()
            for member, allowed, reason in pool.eligible(listing=listing):
                print(f"{member.name}  {member.udid}  {member.runtime.rsplit('.', 1)[-1]}  {member.state}  "
                      f"{'prunable' if allowed else 'kept'}: {reason}")
            for found, current, reason in pool.templates(listing):
                print(f"{found.name}  {found.udid}  {found.runtime.rsplit('.', 1)[-1]}  {found.state}  "
                      f"{'template' if current else 'stale template'}: {reason}")
        elif args.action == "template":
            for number in (signal.SIGTERM, signal.SIGHUP):
                signal.signal(number, stopped)
            device_type, runtime = qualified(DEVICE_TYPE, args.device), qualified(RUNTIME, args.runtime)
            lease = pool.template(device_type, runtime, args.label or pool.kind_label(device_type, runtime),
                                  "eng simulator-pool template", boot_timeout=600,
                                  voiceover_limit=warmed.VOICEOVER_LIMIT, require_voiceover=True)
            if lease is None:
                print(f"eng simulator-pool: no current template of {args.device} on {args.runtime}; new members are "
                      "created instead", file=sys.stderr)
                return 1
            details = (pool.record.entry(lease.key) or {}).get("template") or {}
            released = pool.release(lease, outcome="Warmed or confirmed current", shutdown=False)
            print(f"{lease.key}: warmed on runtime build {details.get('runtimeBuild')}: {details.get('voiceOver')}")
            return 0 if released else 1
        elif args.action == "take":
            # A stop runs the cleanup below rather than leaving a held, booted member.
            for number in (signal.SIGTERM, signal.SIGHUP):
                signal.signal(number, stopped)
            lease = pool.take(args.device, args.runtime, purpose=args.purpose, label=args.label,
                              keep_state=args.keep_state)
            try:
                status = boot(pool, lease, args.boot_timeout)
            except BaseException:
                pool.release(lease, outcome=f"Stopped while booting before {args.purpose}")
                raise
            if status is not None:
                pool.release(lease, outcome=f"The boot ended with status {status} before {args.purpose}")
                print(f"eng simulator-pool: {lease.key} did not finish booting (status {status})", file=sys.stderr)
                return status if status != 124 else evidence.UNMEASURED
            print(lease.udid)
        elif args.action == "check":
            print((pool.check_held if args.held else pool.check)(args.udid).udid)
        elif args.action == "release":
            return 0 if pool.release(pool.held(args.udid), outcome=args.outcome) else 1
        elif args.action == "prune":
            wanted = {str(uuid.UUID(udid)).upper() for udid in args.udids} or None
            pruned = pool.prune(udids=wanted)
            print("\n".join(pruned) if pruned else "Nothing was pruned")
            return 1 if wanted and len(pruned) != len(wanted) else 0
        return 0
    except Stopped as stop:
        print(f"eng simulator-pool: stopped by signal {stop.number}", file=sys.stderr)
        return 128 + stop.number
    except (OSError, ValueError, RuntimeError, TimeoutError) as error:
        print(f"eng simulator-pool: {error}", file=sys.stderr)
        return 1
