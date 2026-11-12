"""A project's simulator pool: take, create and prune its members under the host's device record.

A member is a simulator named `POOL LABEL N`, such as `Pagepour Duo 2`; its kind is its device type
and runtime. Taking one reserves a shut-down member whose record entries are released, observes it
still shut down and resets it: a copy of the kind's warmed template's data into the member's data
folder, which keeps its UDID and the simulator tool's approval, so that the member, and xcodebuild's
clones of it, boot with the template's finished first boot, VoiceOver's voice and the system's voices
settled (vocaro-engineering#172); when the pool has no settled template to copy, the member is erased
instead and the log says why. When no member of the kind is free, the next one is created under the
naming scheme, taking the lowest free number, and stays in the pool: a clone of the kind's warmed
template, `POOL LABEL template (RUNTIME)`, which is made, or remade when the runtime's installed builds
change or `queuekeeper simulator-pool template --refresh` asks, under its own reservation (template.py). A take
with erase erases the member as before, and one with keep_state reserves and boots it without resetting
or erasing it. Pruning deletes a member
unused and released for an hour, never the lowest-numbered of its kind, and a template whose runtime
is no longer installed, and records each deletion (guides/simulator-testing.md, Reserved simulator pool). Nothing
else here deletes or renames a simulator, except a template whose runtime moved, which is replaced.
Reaping shuts down a member, or a template, left booted past its time, such as by a killed warm-up
(offload_host.reap; guides/shared-host.md, Stale pool simulators).
Checking a UDID refuses any device outside the pool, such as one of Xcode's default devices, which a
script that picks a device by name finds instead of a member (vocaro-engineering#91); checking it
as held also refuses a member that the caller holds no live reservation of (vocaro-engineering#122).

    queuekeeper simulator-pool list|take|release|prune|reap|template|check [--pool NAME] ...; queuekeeper simulator-pool -h for the rest
"""
import argparse
import contextlib
import ctypes
import ctypes.util
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from xml.parsers.expat import ExpatError

from .. import devices as device_record
from ... import lane_results, offload_host
from ...resources import process_start
from . import lifecycle, speech, template as warmed

# How long a member other than its kind's lowest-numbered must be unused, and its record entry released, before
# pruning deletes it: a grace that keeps a member for a lane between two takes. A member is no warmer than a new
# clone of its kind's template, which takes seconds, so no longer idle limit keeps one (owner's decision,
# 2026-11-07, vocaro-engineering#259, replacing the 14 days of 2026-10-26).
PRUNE_AFTER = timedelta(hours=1)
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
# A member reset from its kind's warmed template, whose voices had settled.
RESET = ("Synthetic test state: a copy of the warmed template {template}, made {at} on runtime build {build} (booted, "
         "VoiceOver started once, the system's voices used twice with a reboot between), with nothing installed; the "
         "next holder may reset or erase this pool member")
# How long a reservation is expected to last; an overdue one releases nothing. A run's holds its members while
# its process runs, so this bounds only how long reap waits on one whose process has gone.
HOLD = timedelta(hours=8)
# How long a member `queuekeeper simulator-pool take` hands to a session is held: the take/release leases the record kept
# lasted 2 to 6 min and the longest pool lease of any kind 42 min, so a session that needs longer extends it, and a
# member a crashed session leaves is released by reap an hour after its take, not eight (vocaro-engineering#179).
TAKE_HOLD = timedelta(hours=1)
# What `release` records of a member it releases by UDID, and `release --mine` of each, unless --outcome says.
RELEASED, CLOSEOUT = "Released", "Released at the session's closeout"
# Seconds a take or a creation waits for the kind's template while another session holds it, as for its
# own copy or clone of a few seconds, polling every TEMPLATE_POLL, before it erases or creates instead.
TEMPLATE_WAIT, TEMPLATE_POLL = 60, 2
# Where CoreSimulator's default device set keeps a device's folders, when simctl does not say.
DEVICES = Path.home() / "Library/Developer/CoreSimulator/Devices"
# clonefile(2)'s flag: symbolic links are cloned as links, never followed.
CLONE_NOFOLLOW = 0x0001
# What a take records when the caller asked for an erase in place of the reset.
ERASE_ASKED = "asked for with --erase"
# How much of simctl's error text a failed boot logs: more than the few lines an error of its takes.
BOOT_ERROR_SHOWN = 2000
# The simulator's setting that has the system load UIKit's and SwiftUI's own accessibility into every app
# process with no assistive technology running, so that an in-process test hosted in an app reads their
# views (AccessibilitySnapshot.readsSystemViews). Apple documents no such key: verified on the iOS 27.2
# simulator, runtime build 24B8117, and checked again by the OS-drift procedure (vocaro-engineering#181;
# guides/simulator-testing.md, App accessibility).
APP_ACCESSIBILITY_DOMAIN, APP_ACCESSIBILITY_KEY = "com.apple.Accessibility", "ApplicationAccessibilityEnabled"


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
    """The pool a project's queuekeeper.json names, or None."""
    try:
        return json.loads((Path(root) / "queuekeeper.json").read_text()).get("simulatorPool")
    except (OSError, ValueError, AttributeError):
        return None


def running(project=None):
    """The pool of the project a command runs in: the queuekeeper.json in the working directory, which a lane's
    is, and the launcher's project only where the directory has none. A launcher always names its own checkout,
    and the toolkit's, which names a pool of its own, runs in other projects' roots too."""
    here = Path.cwd()
    return configured(here if (here / "queuekeeper.json").exists() else project or here)


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
    data: str = None

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
                                entry.get("isAvailable", True), entry.get("dataPath")))
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
    data: str = None

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
                                  runtime, entry.get("state", ""), entry.get("isAvailable", True), entry.get("dataPath")))
    return sorted(found, key=lambda template: (template.kind, template.name))


def template_settled(entry):
    """(True, why) when the template's record entry says its voices settled, else (False, why)."""
    voices = ((entry or {}).get("template") or {}).get("voices") or {}
    if voices.get("settled"):
        return True, voices.get("outcome") or "its voices settled"
    return False, "its voices did not settle" + (f": {voices['outcome']}" if voices.get("outcome") else "")


def template_current(template, entry, builds):
    """(True, why) when the pool may clone new members from the template, else (False, why). A current
    template whose voices did not settle is cloned from, but never copied into a member (template_settled)."""
    details = (entry or {}).get("template") or {}
    if not template.available:
        return False, "its runtime is no longer installed"
    if (entry or {}).get("device") != template.udid or details.get("udid") != template.udid:
        return False, "it was never warmed to the end"
    if "voices" not in details:
        return False, "it was warmed before templates settled the system's voices (vocaro-engineering#172)"
    # VoiceOver's setting is in the data container: a clone of a template it was left on in boots with it
    # on, and speaks through the Mac's output once the volume comes back. A record from before
    # voiceOverOff was written says so in its voiceOver.
    off = details.get("voiceOverOff")
    if off is None: off = "could not be confirmed off" not in str(details.get("voiceOver") or "")
    if not off:
        return False, "VoiceOver could not be confirmed off, so a clone would boot with it on"
    # devicectl reads the running system, so it can confirm VoiceOver off while the file a copy takes still says
    # on: the record of the template behind vocaro-engineering#263 said off, and its members booted with it on.
    if saved_voiceover_on(data_path({"dataPath": template.data}, template.udid)):
        return False, "its saved settings have VoiceOver on, so a clone or copy would boot with it on"
    if builds is not None and details.get("installedBuilds") != builds:
        return False, (f"the runtime moved: warmed on {details.get('runtimeBuild')} among builds "
                       f"{', '.join(details.get('installedBuilds') or []) or 'none'}, now {', '.join(builds) or 'none'}")
    return True, (f"warmed on runtime build {details.get('runtimeBuild')} at {details.get('warmedAt')}: "
                  f"{details.get('voiceOver')}; voices: {template_settled(entry)[1]}")


def origin(entry):
    """What a member's last take left it with, from its record entry: the template it was reset from or
    cloned from and when that was made, the state it kept, or that it was erased and why."""
    if not entry:
        return "no recorded take"
    when = f"last take {entry.get('reservedAt')}: "
    for key, verb in (("resetFrom", "reset from"), ("clonedFrom", "cloned from")):
        found = entry.get(key)
        # An entry written before 2026-11-04 names the template alone.
        if isinstance(found, str):
            return when + f"{verb} {found}"
        if found:
            return when + f"{verb} {found.get('template')}, made {found.get('warmedAt')}"
    if entry.get("keptState"):
        return when + "kept its state, not erased"
    if entry.get("erasedBecause"):
        return when + f"erased: {entry['erasedBecause']}"
    if entry.get("preservation") == PRESERVATION:
        return when + "erased"
    return "no recorded take"


def data_path(entry, udid):
    """A device's data folder, as simctl lists it, else where CoreSimulator's default device set keeps it."""
    return Path(entry.get("dataPath") or DEVICES / udid / "data")


def saved_voiceover_on(data):
    """Whether a shut-down device's data folder has VoiceOver saved on, the setting it boots with. A device that
    never saved the setting has it off, and a file that cannot be read says nothing of it."""
    try:
        with open(Path(data) / ACCESSIBILITY_SETTINGS, "rb") as stream:
            saved = plistlib.load(stream)
    except (OSError, ValueError, ExpatError):
        return False
    return isinstance(saved, dict) and bool(saved.get(VOICEOVER_SETTING))


def libc():
    found = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    found.clonefile.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32)
    found.clonefile.restype = ctypes.c_int
    return found


def copy_data(source, target):
    """Replace the folder target with a clone of the folder source: clonefile(2), which APFS makes in a
    moment whatever the size, the files sharing their blocks with the source until written, every mode,
    owner, time, flag and extended attribute kept, and symbolic links as they are. A 2.2 GB template of
    16,810 entries cloned in 0.24 s where `cp -c -R` took 2.3 s and `cp -c -R -p` 12.5 s, adding a
    provenance attribute to 9,209 files (2026-11-04). OSError when the clone fails, as across volumes;
    the old target is gone by then, so the caller erases the device."""
    source, target = Path(source), Path(target)
    if target.is_symlink() or target.is_file(): target.unlink()
    elif target.exists(): shutil.rmtree(target)
    if libc().clonefile(os.fsencode(str(source)), os.fsencode(str(target)), CLONE_NOFOLLOW) != 0:
        number = ctypes.get_errno()
        raise OSError(number, f"clonefile: {os.strerror(number)}", str(source), None, str(target))


# Another device's folder in a symbolic link's target: its data or its logs (vocaro-engineering#216).
FOREIGN = re.compile(r"(?<=/CoreSimulator/Devices/)([0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12})(?=/|$)"
                     r"|(?<=/Logs/CoreSimulator/)([0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12})(?=/|$)")
# LaunchServices' store, which keeps each container's absolute path: the copy's names the source's folders,
# so simctl get_app_container, and the system's own lookups, answered with the template's Files storage
# until it was removed. The system rebuilds it at the next boot.
LAUNCH_SERVICES = Path("var/db/lsd")
# Where a data folder keeps the setting VoiceOver boots with.
ACCESSIBILITY_SETTINGS = Path("Library/Preferences/com.apple.Accessibility.plist")
VOICEOVER_SETTING = "VoiceOverTouchEnabled"


def foreign(target, udid):
    """The other device a link target names, else None."""
    for match in FOREIGN.finditer(target):
        named = match.group(1) or match.group(2)
        if named != udid: return named
    return None


def relocate(data, udid):
    """Make a copied data folder its own device's: every symbolic link into another device's data or logs is
    pointed at this device's, and LaunchServices' store, which keeps the copy's source's container paths, is
    removed for the boot to rebuild. Without it the member's app-group containers, Files' storage among them,
    resolved into the template's folder, so lanes staged books into the template and members shared one
    storage (vocaro-engineering#216). The number of links changed."""
    data, changed = Path(data), 0
    for folder, names, files in os.walk(data):
        for name in names + files:
            path = Path(folder) / name
            if not path.is_symlink(): continue
            target = os.readlink(path)
            if foreign(target, udid) is None: continue
            fixed = FOREIGN.sub(udid, target)
            path.unlink()
            os.symlink(fixed, path)
            changed += 1
    store = data / LAUNCH_SERVICES
    if store.is_dir():
        for path in store.glob("*.csstore"): path.unlink()
    return changed


def stray(data, udid):
    """Where a data folder still points into another device: the first link that does, or LaunchServices'
    copied store, else None. A reset or clone that leaves one is not used."""
    data = Path(data)
    for folder, names, files in os.walk(data):
        for name in names + files:
            path = Path(folder) / name
            if path.is_symlink() and foreign(os.readlink(path), udid) is not None:
                return f"{path.relative_to(data)} links to {os.readlink(path)}"
    store = data / LAUNCH_SERVICES
    if store.is_dir() and any(store.glob("*.csstore")):
        return f"{LAUNCH_SERVICES} still holds the copied LaunchServices store"
    return None


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
        return False, f"last used {member.last_used:%Y-%m-%d %H:%M}, within {span(PRUNE_AFTER)}"
    for key, entry in entries.items():
        if device_record.condition(entry) != "released":
            return False, f"its record entry {key} is {device_record.condition(entry)}"
        released = instant(entry.get("releasedAt"))
        if released is None or now - released <= PRUNE_AFTER:
            return False, f"its record entry {key} was released {entry.get('releasedAt') or 'at an unrecorded time'}"
    gone = "" if member.available else "; its runtime is no longer installed"
    return True, f"unused since {member.last_used:%Y-%m-%d %H:%M}, more than {span(PRUNE_AFTER)}{gone}"


def span(delta):
    """A limit as words, in hours."""
    hours = delta / timedelta(hours=1)
    return f"{hours:g} hour{'s' if hours != 1 else ''}"


@dataclass
class Lease:
    """A member this owner holds: its record key and the revision its last change left. kept says the take
    did not erase it, reset names the template the take copied into it, and cloned names the template a new
    member was cloned from."""
    key: str
    udid: str
    revision: int
    created: bool = False
    kept: bool = False
    cloned: str = None
    reset: str = None


class Pool:
    def __init__(self, name, *, owner, record=None, run=None, log=None, clock=None, warm=True, sleep=time.sleep,
                 alone=False, hold=HOLD, hold_log=None):
        """hold is how long each reservation is expected to last, TAKE_HOLD for `take`. warm warms a new template, by default template.warm; None keeps no templates: it creates new members
        instead of cloning them and erases taken ones instead of resetting them. sleep waits for a held template.
        alone says that this process holds what it reserves only while it runs, as a run does that releases its
        members when it ends, so that reap may release one whose holder has gone before its expected release
        (devices.ALONE); never for `queuekeeper simulator-pool take`, whose session holds a member once it has ended.
        hold_log is the directory of the log of take holds (offload_host.HOLD_LOG), by default the reap log's."""
        if not name:
            raise ValueError("a pool needs its name")
        self.name, self.owner, self.run = name, owner, run or simctl
        self.record = record or device_record.Record()
        self.log = log or (lambda message: print(message, file=sys.stderr, flush=True))
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.warm = warmed.warm if warm is True else warm
        self.sleep = sleep
        self.hold = hold
        self.alone = alone
        self.hold_log = hold_log

    def log_hold(self, event, key, entry, *, after, ended=None, **more):
        """Log one event of a member's take hold, entry its record entry before the event (offload_host.hold_line);
        a log that cannot be written is warned about and changes nothing else."""
        return offload_host.log_hold({**offload_host.hold_line(event, key, entry, self.clock(), after=after,
                                                               ended=ended, **more), "pool": self.name},
                                     self.hold_log)

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
                           "member with `queuekeeper simulator-pool take --device TYPE --runtime RUNTIME` and pass its UDID "
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
                                    f"holds no reservation of; {why}. Take one with `queuekeeper simulator-pool take "
                                    "--device TYPE --runtime RUNTIME` and pass the UDID it prints "
                                    "(guides/simulator-testing.md, Reserved simulator pool)")

    def state(self, udid):
        """A fresh observation of this exact device, or None when simctl no longer lists it."""
        return next((entry.get("state") for entries in self.listing().get("devices", {}).values()
                     for entry in entries if entry["udid"].upper() == udid), None)

    def entries(self, listing=None):
        """Each listed device's `simctl list devices` entry, by UDID."""
        return {entry["udid"].upper(): entry for entries in (listing or self.listing()).get("devices", {}).values()
                for entry in entries}

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
                 voiceover_limit=warmed.TAKE_VOICEOVER_LIMIT, voices_limit=warmed.TAKE_VOICES_LIMIT,
                 require_voiceover=False, settled=False, refresh=False, wait=TEMPLATE_WAIT, use="clone"):
        """(lease, None): a held, shut-down, current template of the kind, to clone a new member from or to
        copy into a taken one, warmed first when the kind has none current, as when its runtime's installed
        builds changed or it predates settled voices; else (None, why), why None only while the pool keeps
        no templates. The caller releases the lease once it has cloned or copied.

        settled asks for a template whose voices settled, as a reset does: a current one whose voices did
        not settle within its limits is left as it is, for `queuekeeper simulator-pool template --refresh`, and why
        says so. refresh replaces the template whatever its state. While another session holds the template,
        as for its own copy or clone of a few seconds, this waits up to wait seconds. A take warms within the
        shorter template.TAKE_* limits, so that a kind's first creation or reset does not eat a lane's time;
        require_voiceover, as `queuekeeper simulator-pool template` passes it with the full limits, also replaces a
        current template whose VoiceOver stayed cold or whose voices did not settle. use, clone or copy,
        names what the caller does with the template, for the reasons."""
        if self.warm is None: return None, None
        used = {"clone": "cloned", "copy": "copied"}[use]
        device_type, runtime = qualified(DEVICE_TYPE, device_type), qualified(RUNTIME, runtime)
        builds = self.installed_builds(runtime)
        if not builds:
            return None, f"no installed build of {runtime} could be read, so no template is warmed or {used}"
        key = template_name(self.name, label, runtime)
        found = [template for template in templates(self.name, self.listing()) if template.name == key]
        if len(found) > 1:
            return None, f"{len(found)} simulators are named {key}; none is {used} or replaced"
        existing = found[0] if found else None
        current, waited = self.record.entry(key), 0
        while current is not None and device_record.condition(current) != "released" and waited < wait:
            self.sleep(TEMPLATE_POLL)
            waited += TEMPLATE_POLL
            current = self.record.entry(key)
        if current is not None and device_record.condition(current) != "released":
            if waited: self.log(f"WAITED {waited} s for {key}, still held")
            return None, f"{key} is {device_record.condition(current)} by {current.get('ownerChatID')}"
        current_now, why = template_current(existing, current, builds) if existing is not None else (False, None)
        if refresh and existing is not None:
            current_now, why = False, "queuekeeper simulator-pool template --refresh asked for a new one"
        if current_now and require_voiceover and not ((current or {}).get("template") or {}).get("voiceOverWarmed"):
            current_now, why = False, "its VoiceOver was not warmed"
        if current_now and require_voiceover and not template_settled(current)[0]:
            current_now, why = False, template_settled(current)[1]
        if current_now and settled and not template_settled(current)[0]:
            return None, f"{key}: {template_settled(current)[1]}; {self.refresh_hint(device_type, runtime)}"
        usable = existing is not None and existing.kind == (device_type, runtime) and current_now
        details = {"template": current["template"]} if usable else {}
        try:
            revision = self._reserve(key, (f"{'Copy' if use == 'copy' else 'Clone'} the template for {purpose}" if usable
                                           else f"Warm the template for {purpose}"),
                                     existing.udid if existing else None,
                                     revision=(current or {}).get("revision", 0), **details)
        except device_record.Refused as error:
            return None, f"{key}: {error}"
        lease = Lease(key, existing.udid if existing else None, revision)
        try:
            # A fresh observation under the reservation: never clone, delete or boot a device booted since.
            if existing is not None and self.state(existing.udid) != "Shutdown":
                return None, f"{key} is no longer shut down; its reservation is kept for recovery"
            if usable:
                return lease, None
            if existing is not None:
                if existing.kind != (device_type, runtime): why = f"it is a {existing.device_type}, not a {device_type}"
                deleted = self.run("delete", existing.udid, timeout=300)
                if deleted.returncode != 0 or self.state(existing.udid) is not None:
                    self.release(lease, outcome=f"Not replaced: simctl delete exited {deleted.returncode}",
                                 shutdown=False)
                    return None, (f"{key} ({existing.udid}) could not be replaced: simctl delete exited "
                                  f"{deleted.returncode}")
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
                    if not stored:
                        self.record.release(key, owner=self.owner, revision=lease.revision,
                                            outcome="Not warmed: the template was not created",
                                            restoration="Nothing was created")
                    return None, f"simctl create {key} failed with status {created.returncode}: {created.stderr.strip()}"
                udid = stored[0]
            lease.udid = udid
            entry = self.record.update(key, owner=self.owner, revision=lease.revision, device=udid,
                                       createdAt=self.clock().isoformat())
            lease.revision = entry["revision"]
            if created.returncode == 0 and entry.get(device_record.HOLDER):
                device = self.entries().get(udid) or {}
                marker = offload_host.template_marker(device)
                if marker is not None:
                    with marker.open("x") as stream:
                        json.dump({"version": 1, "pool": self.name, "udid": udid, "name": key, "runtime": runtime,
                                   "holder": entry[device_record.HOLDER]}, stream)
            self.log(f"WARMING the template {key} ({udid}): boot, VoiceOver until it speaks, then the system's voices "
                     "twice with a reboot between")
            try:
                warmth = self.warm(udid, runtime, simctl=self.run, boot_timeout=boot_timeout,
                                   voiceover_limit=voiceover_limit, voices_limit=voices_limit)
            except warmed.WarmFailed as error:
                self.release(lease, outcome=f"Not warmed: {error}")
                return None, f"{key} ({udid}) was not warmed: {error}"
            lease.revision = self.record.update(key, owner=self.owner, revision=lease.revision, template={
                "udid": udid, "deviceType": device_type, "runtime": runtime, "installedBuilds": builds,
                "warmedAt": self.clock().isoformat(), **warmth})["revision"]
            self.run("shutdown", udid, timeout=300)
            if self.state(udid) != "Shutdown":
                self.release(lease, outcome="Warmed, but its shutdown could not be confirmed", shutdown=False)
                return None, f"{key} ({udid}) was warmed, but its shutdown could not be confirmed"
            # Read once it is shut down, when the system has written what the copy will take.
            data = (self.entries().get(udid) or {}).get("dataPath")
            clonable, reason = template_current(Template(key, udid, label, device_type, runtime, "Shutdown", data=data),
                                                self.record.entry(key), builds)
            if not clonable:
                # Never cloned: the next creation replaces it.
                self.release(lease, outcome=f"Warmed, not cloned: {reason}", shutdown=False)
                return None, f"{key} ({udid}) is not {used}: {reason}"
            voices = warmth.get("voices") or {}
            self.log(f"WARMED the template {key} ({udid}) on runtime build {warmth['runtimeBuild']}: "
                     f"{warmth.get('voiceOver')}; voices: {voices.get('outcome')}")
            if settled and not voices.get("settled"):
                # A reset needs settled voices. The template keeps its first boot and VoiceOver's start, so new
                # members are cloned from it; the command, with the full limits, replaces it.
                self.release(lease, outcome=f"Warmed, voices not settled: {voices.get('outcome')}", shutdown=False)
                return None, (f"{key} ({udid}): {template_settled(self.record.entry(key))[1]} within the take's "
                              f"limits; {self.refresh_hint(device_type, runtime)}")
            return lease, None
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
    def refresh_hint(device_type, runtime):
        return (f"refresh it with `queuekeeper simulator-pool template --device {device_type.rsplit('.', 1)[-1]} --runtime "
                f"{runtime.rsplit('.', 1)[-1]} --refresh`")

    def reset(self, lease, source, *, purpose):
        """Copy the template source holds into the member lease holds, in place of an erase: (True, None) once
        the member carries the template's data, else (False, why), the member left for the caller to erase.

        The copy keeps the member's identity: its device.plist, and with it its UDID, name and the simulator
        tool's approval, stay, and only its data folder changes, to a clone of the template's (copy_data,
        clonefile(2), a moment for 2 GB). `simctl clone` would make a device with a new UDID, which the device
        record keys on and the simulator tool asks the owner to approve again. `simctl erase` runs first, so
        that the member starts from its own pristine state and a copy that fails leaves it erased, or the
        caller's erase after a failed copy does."""
        found = self.entries()
        member, template = found.get(lease.udid), found.get(source.udid)
        details = (self.record.entry(source.key) or {}).get("template") or {}
        if member is None or template is None:
            return False, f"simctl no longer lists {lease.key if member is None else source.key}"
        if member.get("state") != "Shutdown" or template.get("state") != "Shutdown":
            return False, f"{lease.key if member.get('state') != 'Shutdown' else source.key} is no longer shut down"
        started = time.monotonic()
        erased = self.run("erase", lease.udid)
        if erased.returncode != 0:
            return False, f"simctl erase {lease.key} exited {erased.returncode}: {erased.stderr.strip()}"
        try:
            copy_data(data_path(template, source.udid), data_path(member, lease.udid))
        except OSError as error:
            return False, f"the copy of {source.key}'s data into {lease.key} failed: {error}"
        target = data_path(member, lease.udid)
        try:
            relocate(target, lease.udid)
        except OSError as error:
            return False, f"the copy of {source.key}'s data into {lease.key} could not be made its own: {error}"
        left = stray(target, lease.udid)
        if left is not None:
            return False, f"the copy of {source.key}'s data into {lease.key} still points into another device: {left}"
        seconds = round(time.monotonic() - started, 1)
        voices = details.get("voices") or {}
        lease.revision = self.record.update(lease.key, owner=self.owner, revision=lease.revision, resetFrom={
            "template": source.key, "udid": source.udid, "runtimeBuild": details.get("runtimeBuild"),
            "warmedAt": details.get("warmedAt"), "at": self.clock().isoformat(), "seconds": seconds,
            "voices": {use: (voices.get(use) or {}).get("seconds") for use in ("firstUse", "secondUse")}},
            preservation=RESET.format(template=source.key, at=details.get("warmedAt"),
                                      build=details.get("runtimeBuild")))["revision"]
        lease.reset = source.key
        self.log(f"RESET {lease.key} ({lease.udid}) from {source.key}, made {details.get('warmedAt')} on runtime build "
                 f"{details.get('runtimeBuild')}, in {seconds} s")
        return True, None

    def _release_by(self, hold=None):
        return (datetime.now(timezone.utc) + (self.hold if hold is None else hold)).isoformat()

    def extend(self, udid, hold=TAKE_HOLD):
        """Move the expected release of this owner's live reservation of a member to hold from now; the new
        expected release. Anything check_held refuses is refused."""
        member = self.check_held(udid)
        lease = self.held(member.udid)
        until = self._release_by(hold)
        before = self.record.entry(lease.key) or {}
        self.record.update(lease.key, owner=self.owner, revision=lease.revision, expectedReleaseBy=until)
        self.log_hold("extend", lease.key, before, after=until)
        return until

    def _reserve(self, key, purpose, device=None, revision=None, taken=False, **details):
        """Reserve a key at the revision read, by default its current one; the new revision. The
        entry names this process as its holder, by its PID and start, so that a census of leftover
        processes counts the booted member as a live run's while this process is one of that run's.
        The holder only helps that census, so a start that `ps` cannot give (a project's tests may run
        the pool with no `ps` on PATH) leaves it out rather than failing the reservation. taken marks a
        member's reservation by a take or creation that a session, not a run, holds (offload_host.TAKEN)."""
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
            if self.alone: details[device_record.ALONE] = True
        if taken and not self.alone: details[offload_host.TAKEN] = self.name
        entry = self.record.reserve(key, owner=self.owner, revision=revision, device=device, purpose=purpose,
                                    release_by=self._release_by(), details=details)
        return entry["revision"]

    def take(self, device_type, runtime, *, purpose, label=None, preservation=None, prune=True, keep_state=False,
             erase=False):
        """A free member of the kind, reserved and reset from the kind's warmed template, or erased when no
        settled template can be copied, or a new one; the caller boots it.

        erase erases the member as before this reset existed, for a lane that wants pristine data, and the
        record says it was asked for. keep_state reserves the member without resetting or erasing it, for a
        lane that needs no fresh installation: it keeps what earlier holders left, their apps and data among
        it, and boots without the first boot's data migration. Its preservation note says so."""
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
                revision = self._reserve(member.name, purpose, member.udid, taken=True, **details)
            except device_record.Refused:
                continue  # Another session reserved it first, perhaps under another key.
            lease = Lease(member.name, member.udid, revision, kept=keep_state)
            erased, why = None, None
            try:
                # The listing predates the reservation. Never reset, erase or boot a device booted since then.
                if self.state(member.udid) != "Shutdown":
                    self.log(f"{member.name} is no longer shut down; its reservation is kept for recovery")
                    continue
                if erase:
                    erased, why = self.run("erase", member.udid), ERASE_ASKED
                elif not keep_state:
                    source, why = self.template(device_type, runtime, member.label, purpose, settled=True, use="copy")
                    done = False
                    if source is not None:
                        try:
                            done, why = self.reset(lease, source, purpose=purpose)
                        finally:
                            # The copy reads the shut-down template and changes nothing in it.
                            self.release(source, outcome=f"{'Copied' if done else 'Not copied'} into {member.name} "
                                                         f"for {purpose}", shutdown=False)
                    if not done:
                        erased = self.run("erase", member.udid)
            except BaseException:
                # A stop before the lease is handed back releases it once the member is shut down.
                self.release(lease, outcome=f"Stopped before {purpose}", shutdown=False)
                raise
            if erased is not None and erased.returncode != 0:
                self.release(lease, outcome=f"Erase failed before {purpose}: {erased.stderr.strip()}", shutdown=False)
                continue
            if keep_state:
                self.log(f"TOOK {member.name} ({member.udid}), not erased: it keeps its state")
            elif lease.reset is not None:
                self.log(f"TOOK {member.name} ({member.udid}), reset from {lease.reset}")
            else:
                if why:
                    lease.revision = self.record.update(member.name, owner=self.owner, revision=lease.revision,
                                                        erasedBecause=why)["revision"]
                self.log(f"TOOK {member.name} ({member.udid}), erased" + (f": {why}" if why else ""))
            return lease
        return self.create(device_type, runtime, purpose=purpose, label=label, preservation=preservation or PRESERVATION,
                           erase=erase)

    def create(self, device_type, runtime, *, purpose, label=None, preservation=PRESERVATION, erase=False):
        """The next member of the kind, reserved under the lowest free number; its first boot is the caller's.
        erase makes it with `simctl create`, never as a clone of the kind's template, as a take with erase asks
        of a member it reuses (#316)."""
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
                revision = self._reserve(key, purpose, taken=True, preservation=preservation, created=True)
            except device_record.Refused:
                continue
            break
        try:
            return self._create(key, revision, device_type, runtime, purpose=purpose, label=label,
                                preservation=preservation, erase=erase)
        except BaseException as error:
            # A stop or failure after the reservation and before a UDID was recorded: release the
            # name unless a device of that name now exists, which keeps it for recovery.
            current = self.record.entry(key) or {}
            if current.get("ownerChatID") == self.owner and not current.get("device") and \
                    not any(entry.get("name") == key for entries in self.listing().get("devices", {}).values()
                            for entry in entries):
                self.record.release(key, owner=self.owner, revision=current["revision"],
                                    # An exception is truthy even when its message is empty.
                                    outcome=f"Not created: {str(error) or type(error).__name__}",
                                    restoration="Nothing was created")
            raise

    def _create(self, key, revision, device_type, runtime, *, purpose, label, preservation, erase=False):
        # Another session may have created the name between the listing and the reservation.
        if any(entry.get("name") == key for entries in self.listing().get("devices", {}).values() for entry in entries):
            self.record.release(key, owner=self.owner, revision=revision, outcome="Not created: the name appeared",
                                restoration="Nothing was created")
            return self.create(device_type, runtime, purpose=purpose, label=label, preservation=preservation,
                               erase=erase)
        if erase:
            # A pristine device, as an erase leaves a member it reuses: no look at the template.
            source, why = None, None
            self.log(f"{key} is created, not cloned: {ERASE_ASKED}")
        else:
            source, why = self.template(device_type, runtime, label, purpose)
        if source is None and why:
            self.log(f"{why}, so the new member is created instead")
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
        if erase: changes["erasedBecause"] = ERASE_ASKED
        if source is not None:
            # simctl clone copies the template's links and LaunchServices store as they are (#216).
            target = data_path(self.entries().get(udid) or {}, udid)
            try:
                relocate(target, udid)
                left = stray(target, udid)
            except OSError as error:
                left = str(error)
            if left is not None:
                erased = self.run("erase", udid)
                self.log(f"the clone {key} still pointed into another device ({left}), so it was erased")
                if erased.returncode != 0:
                    raise RuntimeError(f"simctl erase {key} exited {erased.returncode}: {erased.stderr.strip()}")
                source = None
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

    def release_mine(self, outcome=CLOSEOUT):
        """Shut down and release every member this owner holds from a take: (released, kept), each a list
        of (key, udid, why). A take's holder is the `take` command, which has ended once the session has the
        UDID; a reservation whose holder still runs is a run's own, in progress, and stays with it. One that
        is orphaned or uncertain stays for its recovery, and one whose shutdown is not confirmed is kept, as
        release keeps it (vocaro-engineering#179). Each release is logged (offload_host.HOLD_LOG)."""
        listing = self.listing()
        udids = {member.udid for member in self.members(listing)}
        released, kept = [], []
        for key, entry in sorted(self.record.load().get("devices", {}).items()):
            if not isinstance(entry, dict) or entry.get("ownerChatID") != self.owner: continue
            udid = str(entry.get("device") or "").upper()
            if udid not in udids: continue
            state = device_record.condition(entry)
            if state == "released": continue
            if state != "held":
                kept.append((key, udid, f"its reservation is {state}: recover it"))
                continue
            holder = entry.get(device_record.HOLDER)
            if isinstance(holder, dict) and isinstance(holder.get("pid"), int) and holder.get("start") is not None:
                try:
                    running = process_start(holder["pid"]) == holder["start"]
                except OSError as error:
                    kept.append((key, udid, f"whether its holder process {holder['pid']} runs is unknown: {error}"))
                    continue
                if running:
                    kept.append((key, udid, f"its holder process {holder['pid']} still runs"))
                    continue
            lease = Lease(key, udid, entry["revision"], bool(entry.get("created")))
            if self.release(lease, outcome=outcome):
                released.append((key, udid, "shut down and released"))
                self.log_hold("release", key, entry, after=None, ended=outcome, form="mine")
            else:
                kept.append((key, udid, "its shutdown or release was not confirmed; kept for recovery"))
        return released, kept

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
        if device_type is None:
            pruned += self.prune_templates(udids)
        return pruned

    def prune_templates(self, udids=None):
        """Delete the shut-down, unheld templates whose runtime is no longer installed, and return their
        names. Nothing else prunes a template: one outlives every member of its kind while its runtime is
        installed, and only its replacement deletes it then."""
        pruned = []
        for template, _, why in self.templates():
            if template.available or (udids is not None and template.udid not in udids): continue
            if template.state != "Shutdown":
                self.log(f"{template.name}: not pruned: it is {template.state}")
                continue
            entry = self.record.entry(template.name)
            try:
                revision = self._reserve(template.name, "Prune a template whose runtime is gone", template.udid,
                                         revision=(entry or {}).get("revision", 0))
            except device_record.Refused as error:
                self.log(f"{template.name}: not pruned: {error}")
                continue
            lease = Lease(template.name, template.udid, revision)
            if self.state(template.udid) != "Shutdown":
                self.release(lease, outcome="Not pruned: it changed before deletion", shutdown=False)
                continue
            deleted = self.run("delete", template.udid, timeout=300)
            if deleted.returncode != 0 or self.state(template.udid) is not None:
                self.log(f"{template.name}: simctl delete failed: {deleted.stderr.strip()}")
                self.release(lease, outcome=f"Pruning failed: simctl delete exited {deleted.returncode}", shutdown=False)
                continue
            revision = self.record.update(template.name, owner=self.owner, revision=revision, pruned={
                "udid": template.udid, "at": self.clock().isoformat(), "deviceType": template.device_type,
                "runtime": template.runtime, "why": why})["revision"]
            self.record.release(template.name, owner=self.owner, revision=revision,
                                outcome=f"Pruned: deleted the template {template.udid}; {why}",
                                restoration="simctl no longer lists the device")
            self.log(f"PRUNED the template {template.name} ({template.udid}): {why}")
            pruned.append(template.name)
        return pruned


class BootTimedOut(TimeoutError):
    """A member's boot reached its limit, which leaves the work unmeasured; the member was released."""


class ReleaseUnconfirmed(RuntimeError):
    """A member's shutdown could not be confirmed, so its reservation is kept for recovery."""


@contextlib.contextmanager
def member(name, device_type, runtime, *, purpose, owner=None, boot_timeout=600, log=None, keep_state=False,
           erase=False, app_accessibility=False):
    """A booted member of the pool for the block, then shut down and released however it ends; keep_state
    takes it without resetting or erasing it, erase erases it in place of the reset (Pool.take), and
    app_accessibility switches UIKit's and SwiftUI's own accessibility on in its app processes (boot).

    It raises BootTimedOut when the boot reaches its limit, RuntimeError when it fails, and
    ReleaseUnconfirmed after a block that ended normally when the release cannot confirm shutdown."""
    pool = Pool(name, owner=owner or default_owner() or f"{name}-{uuid.uuid4()}", log=log, alone=True)
    lease = pool.take(device_type, runtime, purpose=purpose, keep_state=keep_state, erase=erase)
    status = "stopped"
    try:
        booted = boot(pool, lease, boot_timeout, app_accessibility=app_accessibility)
        if booted == lane_results.UNMEASURED:
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


def set_app_accessibility(run, udid, enabled, *, kept):
    """Give a booted member's app processes UIKit's and SwiftUI's own accessibility, or not; None when the
    setting reads as asked, else why not. run is simctl's stand-in, as Pool.run.

    Asked for, the setting is written on every boot and read back as 1: a reset copies the template's data,
    which never holds it, and an erase clears it, so neither carries it from one take to the next. Not asked
    for, nothing is written, since the reset and the erase leave it unset, except on a member that kept its
    state, where an earlier holder's setting is deleted, so that a lane reads system views only when it asks."""
    domain, key = APP_ACCESSIBILITY_DOMAIN, APP_ACCESSIBILITY_KEY
    if enabled:
        written = run("spawn", udid, "defaults", "write", domain, key, "-bool", "true", timeout=30)
    elif kept:
        # A key that is already absent fails the delete, which the reading below then confirms.
        written = run("spawn", udid, "defaults", "delete", domain, key, timeout=30)
    else:
        return None
    read = run("spawn", udid, "defaults", "read", domain, key, timeout=30)
    value = read.stdout.strip() if read.returncode == 0 else None
    if value == ("1" if enabled else None):
        return None
    if enabled and written.returncode != 0:
        return f"simctl spawn defaults write exited {written.returncode}: {written.stderr.strip()}"
    return f"{domain} {key} reads {value!r} after it was " + ("written true" if enabled else "deleted")


def boot_failure(command, result):
    """Which simctl command ended a boot, its status and its error text, on one line. Of a text over
    BOOT_ERROR_SHOWN characters the start and the end are kept, as simctl states the error first and any
    underlying error last."""
    text = " ".join((result.stderr or "").split())
    if len(text) > BOOT_ERROR_SHOWN:
        half = BOOT_ERROR_SHOWN // 2
        text = f"{text[:half]} [cut: {len(text) - 2 * half} characters] {text[-half:]}"
    return f"BOOT FAILED: `simctl {command}` exited {result.returncode}: {text or 'no error text'}"


def boot(pool, lease, timeout, *, app_accessibility=False):
    """Boot a member to bootstatus -b; None when it booted, else a status (UNMEASURED for a timeout). A
    command that fails is logged with its error text (boot_failure).

    The member boots with VoiceOver's sound cues already off, written into its data while it is shut
    down: an erase clears the setting, the mute leaves the cues audible through the Mac
    (guides/voiceover-simulator.md, Muted speech capture), and a spawned write in the booted member
    timed out on every take of a loaded host (vocaro-engineering#310). A setting that cannot be written
    is logged, not a failed boot: a lane without VoiceOver plays no cues. With app_accessibility its app
    processes get UIKit's and SwiftUI's own accessibility (set_app_accessibility); a setting that does not read as asked fails the boot with status 1, since the
    lane asked for it, and is logged."""
    try:
        speech.silence_cues_at_rest(data_path(pool.entries().get(lease.udid) or {}, lease.udid))
    except (OSError, ValueError) as error:
        pool.log(f"CUES {lease.key}: VoiceOver's sound cues could not be switched off, so VoiceOver would play "
                 f"them through the Mac: {error}")
    started = pool.run("boot", lease.udid, timeout=120)
    if started.returncode != 0:
        pool.log(f"{lease.key}: {boot_failure('boot', started)}")
        return started.returncode
    finished = pool.run("bootstatus", lease.udid, "-b", timeout=timeout)
    if finished.returncode != 0:
        pool.log(f"{lease.key}: {boot_failure('bootstatus -b', finished)}")
        return lane_results.UNMEASURED if finished.returncode == 124 else finished.returncode
    why = set_app_accessibility(pool.run, lease.udid, app_accessibility, kept=lease.kept)
    if why is not None:
        pool.log(f"APP ACCESSIBILITY {lease.key}: " + ("could not be switched on, so a hosted test reads no system "
                 "views: " if app_accessibility else "an earlier holder's setting could not be removed: ") + why)
        if app_accessibility: return 1
    return None


class Stopped(BaseException):
    """SIGTERM or SIGHUP during a take, raised so that its cleanup releases what it holds."""
    def __init__(self, number):
        super().__init__(number)
        self.number = number


def stopped(number, _frame):
    raise Stopped(number)


def reap_reports(pool, project, *, dry_run, here, stop_leftovers=True):
    """offload_host.reap's report from this Mac and, unless here, from the second Mac, each with where it
    ran. pool alone when it is named, otherwise every pool the project and the checkouts each Mac can
    see name. Without stop_leftovers, as for `queuekeeper doctor`, the second Mac only lists the leftover processes
    it would stop (offload_host.stop_leftovers)."""
    # remote and disk import this module.
    from ... import offload_host, remote
    from .. import disk
    if pool: pools = [pool]
    else:
        pools = sorted({*disk.pools(Path.home() / "Development"), *filter(None, [configured(project or Path.cwd())])})
    request = {"pools": pools, "dry_run": dry_run, "scan": not pool}
    reports = [(remote.HERE, offload_host.reap(remote.ROOT, **request))]
    host = None if here else remote.configured()
    if host: reports.append((host, remote.reap_there(host, request, stop_leftovers)))
    return reports


def reap_command(pool, project, *, dry_run, here):
    """`queuekeeper simulator-pool reap`: reap_reports, said. It exits 1 when anything failed."""
    from ... import remote
    reports = reap_reports(pool, project, dry_run=dry_run, here=here)
    for where, report in reports:
        lines = remote.reap_lines(where, report)
        print("\n".join(lines) if lines else f"on {where}, no stale pool simulator")
    processes = lambda report: report.get("processes") if isinstance(report.get("processes"), dict) else {}
    return 1 if any(report.get("problem") or report.get("failed") or processes(report).get("problem")
                    or processes(report).get("failed") for _, report in reports) else 0


def main(argv, project=None):
    # --pool and --owner read the same before or after the action, as in `template --owner NAME`: each
    # parser has them, and an action's own leave the value given before it unless they are given again.
    def scoping(parser, default):
        parser.add_argument("--pool", default=default, help="the pool's name, the start of every member's "
                            "(default: the project's simulatorPool in queuekeeper.json)")
        parser.add_argument("--owner", default=default, help=f"the session reservations name (default: "
                            f"${device_record.OWNER}, else the agent's session)")
        return parser
    parser = scoping(argparse.ArgumentParser(prog="queuekeeper simulator-pool", description=__doc__.split("\n\n")[0]), None)
    commands = parser.add_subparsers(dest="action", required=True)
    def action(name, **options): return scoping(commands.add_parser(name, **options), argparse.SUPPRESS)
    action("list", help="every member, its state and whether pruning may delete it")
    take = action("take", help="reserve, reset from the kind's template (or erase: --erase, or when "
                               "no settled template can be copied; neither with --keep-state) and boot a free "
                               "member of a kind, or create the next; prints its UDID")
    take.add_argument("--device", required=True, metavar="TYPE", help="a device type, such as iPhone-17-Pro")
    take.add_argument("--runtime", required=True, help="a runtime, such as iOS-27-1")
    take.add_argument("--label", help="a new member's label (default: the device type's name)")
    take.add_argument("--purpose", default="Simulator tests")
    fresh = take.add_mutually_exclusive_group()
    fresh.add_argument("--keep-state", action="store_true", help="reserve and boot the member without resetting or "
                       "erasing it, for a lane that needs no fresh installation: it keeps earlier holders' apps, "
                       "data and settings, and skips the first boot's migration")
    fresh.add_argument("--erase", action="store_true", help="erase the member in place of the reset from its kind's "
                       "template, for a lane that wants pristine data")
    take.add_argument("--app-accessibility", action="store_true", help="switch UIKit's and SwiftUI's own "
                      "accessibility on in the member's app processes, for tests hosted in an app that read system "
                      "views (guides/simulator-testing.md, App accessibility); a setting that does not read on fails "
                      "the take")
    take.add_argument("--boot-timeout", type=int, default=600, help="seconds the boot may take (default 600); "
                      f"past it the member is released and the command exits {lane_results.UNMEASURED}")
    take.add_argument("--hold", type=int, default=int(TAKE_HOLD.total_seconds() // 60), metavar="MINUTES",
                      help=f"minutes the member is held before reap may release it once its session has gone "
                           f"(default {int(TAKE_HOLD.total_seconds() // 60)}); `extend` moves it later")
    extend = action("extend", help="hold a member you took for longer: its expected release moves to --hold "
                                   "minutes from now")
    extend.add_argument("udid")
    extend.add_argument("--hold", type=int, default=int(TAKE_HOLD.total_seconds() // 60), metavar="MINUTES",
                        help=f"minutes from now (default {int(TAKE_HOLD.total_seconds() // 60)})")
    release = action("release", help="shut a member you hold down and release it; --mine, every member you "
                                     "hold from a take, as closeout does")
    release.add_argument("udid", nargs="?")
    release.add_argument("--mine", action="store_true", help="every member the owner holds whose take has ended, "
                         "not a running lane's own; exit 1 if any is kept")
    release.add_argument("--outcome", help=f"what the record says of the release (default: {RELEASED!r}; with "
                         f"--mine, {CLOSEOUT!r})")
    prune = action("prune", help=f"delete members unused and released for {span(PRUNE_AFTER)}, never the "
                                "lowest of a kind, and templates whose runtime is no longer installed")
    prune.add_argument("udids", nargs="*", metavar="UDID", help="only these members or templates")
    reap = action("reap", help="shut down members and warmed templates left booted for 2 hours that "
                               "nothing accounts for, on this Mac and the second Mac (guides/shared-host.md, Stale "
                               "pool simulators)")
    reap.add_argument("--dry-run", action="store_true", help="say what it would stop and keep, and stop nothing")
    reap.add_argument("--here", action="store_true", help="only this Mac")
    template = action("template", help="warm the kind's template now, with the full limits for VoiceOver's "
                                   "start and each use of the system's voices, or remake it when its runtime moved, its "
                                   "VoiceOver stayed cold or its voices did not settle, so that a lane's members are "
                                   "reset from it and new ones cloned from it")
    template.add_argument("--device", required=True, metavar="TYPE", help="a device type, such as iPhone-17-Pro")
    template.add_argument("--runtime", required=True, help="a runtime, such as iOS-27-1")
    template.add_argument("--label", help="the kind's label (default: the pool's own, else the device type's name)")
    template.add_argument("--refresh", action="store_true", help="remake the template whatever its state")
    check = action("check", help="print a pool member's canonical UDID; exit 1 for any other device, "
                                "such as one of Xcode's default devices, a name or `booted`")
    check.add_argument("--held", action="store_true", help="also exit 1 unless the owner holds a live "
                       "reservation of the member, as `take` leaves it")
    check.add_argument("udid")
    args = parser.parse_args(argv)
    if args.action == "release" and (args.udid is None) == (not args.mine):
        parser.error("name one member's UDID or --mine: one of them, not both")
    if args.action == "reap":
        try:
            return reap_command(args.pool, project, dry_run=args.dry_run, here=args.here)
        except (OSError, ValueError, RuntimeError) as error:
            print(f"queuekeeper simulator-pool: {error}; nothing further was stopped", file=sys.stderr)
            return 1
    name = args.pool or running(project)
    owner = args.owner or default_owner()
    if not name:
        parser.error("name the pool with --pool, or in queuekeeper.json's simulatorPool")
    if not owner and (args.action not in {"list", "check"} or getattr(args, "held", False)):
        parser.error(f"name the owner with --owner or ${device_record.OWNER}")
    try:
        # Only take hands its member to a session that holds it after this command has ended.
        if getattr(args, "hold", 1) <= 0: parser.error("--hold is a positive number of minutes")
        pool = Pool(name, owner=owner, alone=args.action != "take",
                    hold=timedelta(minutes=args.hold) if args.action == "take" else HOLD)
        if args.action == "list":
            listing = pool.listing()
            record = pool.record.load().get("devices", {})
            for member, allowed, reason in pool.eligible(listing=listing):
                print(f"{member.name}  {member.udid}  {member.runtime.rsplit('.', 1)[-1]}  {member.state}  "
                      f"{'prunable' if allowed else 'kept'}: {reason}; {origin(record.get(member.name))}")
            for found, current, reason in pool.templates(listing):
                kind = "template" if current else "stale template" + (", prunable" if not found.available else "")
                print(f"{found.name}  {found.udid}  {found.runtime.rsplit('.', 1)[-1]}  {found.state}  {kind}: {reason}")
        elif args.action == "template":
            for number in (signal.SIGTERM, signal.SIGHUP):
                signal.signal(number, stopped)
            device_type, runtime = qualified(DEVICE_TYPE, args.device), qualified(RUNTIME, args.runtime)
            lease, why = pool.template(device_type, runtime, args.label or pool.kind_label(device_type, runtime),
                                       "queuekeeper simulator-pool template", boot_timeout=600,
                                       voiceover_limit=warmed.VOICEOVER_LIMIT, voices_limit=warmed.VOICES_LIMIT,
                                       require_voiceover=True, refresh=args.refresh, use="copy")
            if lease is None:
                print(f"queuekeeper simulator-pool: no current template of {args.device} on {args.runtime}"
                      + (f": {why}" if why else "") + "; members are erased and new ones created instead",
                      file=sys.stderr)
                return 1
            details = (pool.record.entry(lease.key) or {}).get("template") or {}
            released = pool.release(lease, outcome="Warmed or confirmed current", shutdown=False)
            voices = details.get("voices") or {}
            print(f"{lease.key}: warmed on runtime build {details.get('runtimeBuild')} at {details.get('warmedAt')}: "
                  f"{details.get('voiceOver')}; voices: {voices.get('outcome')}")
            return 0 if released else 1
        elif args.action == "take":
            # A stop runs the cleanup below rather than leaving a held, booted member.
            for number in (signal.SIGTERM, signal.SIGHUP):
                signal.signal(number, stopped)
            lease = pool.take(args.device, args.runtime, purpose=args.purpose, label=args.label,
                              keep_state=args.keep_state, erase=args.erase)
            try:
                status = boot(pool, lease, args.boot_timeout, app_accessibility=args.app_accessibility)
            except BaseException:
                pool.release(lease, outcome=f"Stopped while booting before {args.purpose}")
                raise
            if status is not None:
                pool.release(lease, outcome=f"The boot ended with status {status} before {args.purpose}")
                print(f"queuekeeper simulator-pool: {lease.key} did not finish booting (status {status})" +
                      (", or its app accessibility could not be switched on (APP ACCESSIBILITY, above)"
                       if args.app_accessibility and status == 1 else ""), file=sys.stderr)
                return status if status != 124 else lane_results.UNMEASURED
            entry = pool.record.entry(lease.key) or {}
            pool.log_hold("take", lease.key, entry, after=entry.get("expectedReleaseBy"), expectedReleaseBefore=None)
            print(lease.udid)
        elif args.action == "extend":
            print(pool.extend(args.udid, timedelta(minutes=args.hold)))
        elif args.action == "check":
            print((pool.check_held if args.held else pool.check)(args.udid).udid)
        elif args.action == "release" and args.mine:
            released, kept = pool.release_mine(CLOSEOUT if args.outcome is None else args.outcome)
            for key, udid, why in released: print(f"RELEASED {key} ({udid}): {why}")
            for key, udid, why in kept: print(f"KEPT {key} ({udid}): {why}")
            if not released and not kept: print(f"{owner} holds no {name} pool member")
            return 1 if kept else 0
        elif args.action == "release":
            lease = pool.held(args.udid)
            entry, outcome = pool.record.entry(lease.key) or {}, RELEASED if args.outcome is None else args.outcome
            if not pool.release(lease, outcome=outcome): return 1
            pool.log_hold("release", lease.key, entry, after=None, ended=outcome, form="udid")
        elif args.action == "prune":
            wanted = {str(uuid.UUID(udid)).upper() for udid in args.udids} or None
            pruned = pool.prune(udids=wanted)
            print("\n".join(pruned) if pruned else "Nothing was pruned")
            return 1 if wanted and len(pruned) != len(wanted) else 0
        return 0
    except Stopped as stop:
        print(f"queuekeeper simulator-pool: stopped by signal {stop.number}", file=sys.stderr)
        return 128 + stop.number
    except (OSError, ValueError, RuntimeError, TimeoutError) as error:
        print(f"queuekeeper simulator-pool: {error}", file=sys.stderr)
        return 1
