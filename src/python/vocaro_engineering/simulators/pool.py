"""A project's simulator pool: take, create and prune its members under the host's device record.

A member is a simulator named `POOL LABEL N`, such as `Pagepour Duo 2`; its kind is its device type
and runtime. Taking one reserves a shut-down member whose record entries are released, observes it
still shut down and erases it, which keeps its UDID and the simulator tool's approval. When no
member of the kind is free, the next one is created under the naming scheme, taking the lowest free
number, and stays in the pool. Pruning deletes a member unused for 14 days, never the
lowest-numbered of its kind, and records the deletion (guides/simulator-testing.md, Reserved
simulator pool). Nothing else here deletes or renames a simulator.

    eng simulator-pool list|take|release|prune [--pool NAME] ...; eng simulator-pool -h for the rest
"""
import argparse
import contextlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import uuid

from .. import devices as device_record, evidence
from . import lifecycle

PRUNE_AFTER = timedelta(days=14)
DEVICE_TYPE = "com.apple.CoreSimulator.SimDeviceType."
RUNTIME = "com.apple.CoreSimulator.SimRuntime."
NUMBER = re.compile(r"(?P<label>.+) (?P<number>[1-9][0-9]*)")
PRESERVATION = "Synthetic test state; the next holder may erase this pool member"
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


def default_owner():
    """The session a reservation names: its device-record owner, else its agent session."""
    return (os.environ.get(device_record.OWNER) or os.environ.get("CODEX_THREAD_ID")
            or os.environ.get("CLAUDE_CODE_SESSION_ID"))


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
    """A member this owner holds: its record key and the revision its last change left."""
    key: str
    udid: str
    revision: int
    created: bool = False


class Pool:
    def __init__(self, name, *, owner, record=None, run=None, log=None, clock=None):
        if not name:
            raise ValueError("a pool needs its name")
        self.name, self.owner, self.run = name, owner, run or simctl
        self.record = record or device_record.Record()
        self.log = log or (lambda message: print(message, file=sys.stderr, flush=True))
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def listing(self):
        result = self.run("list", "devices", "-j", timeout=120)
        if result.returncode != 0:
            raise RuntimeError(f"simctl list failed with status {result.returncode}: {result.stderr.strip()}")
        return json.loads(result.stdout)

    def members(self, listing=None):
        return members(self.name, listing or self.listing())

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

    @staticmethod
    def _release_by():
        return (datetime.now(timezone.utc) + HOLD).isoformat()

    def _reserve(self, key, purpose, device=None, revision=None, **details):
        """Reserve a key at the revision read, by default its current one; the new revision."""
        if not self.owner:
            raise ValueError(f"name the owner whose reservation takes {key}")
        if revision is None:
            current = self.record.entry(key)
            revision = current.get("revision", 0) if current else 0
        entry = self.record.reserve(key, owner=self.owner, revision=revision, device=device, purpose=purpose,
                                    release_by=self._release_by(), details=details)
        return entry["revision"]

    def take(self, device_type, runtime, *, purpose, label=None, preservation=PRESERVATION, prune=True):
        """A free member of the kind, reserved and erased, or a new one; the caller boots it."""
        device_type, runtime = qualified(DEVICE_TYPE, device_type), qualified(RUNTIME, runtime)
        if prune: self.prune(device_type, runtime)
        listing = self.listing()
        record = self.record.load()
        for member in self.members(listing):
            if member.kind != (device_type, runtime) or member.state != "Shutdown" or not member.available: continue
            if any(device_record.condition(entry) != "released" for entry in entries_for(member, record).values()):
                continue
            try:
                revision = self._reserve(member.name, purpose, member.udid, preservation=preservation)
            except device_record.Refused:
                continue  # Another session reserved it first, perhaps under another key.
            lease = Lease(member.name, member.udid, revision)
            try:
                # The listing predates the reservation. Never erase a device booted since then.
                if self.state(member.udid) != "Shutdown":
                    self.log(f"{member.name} is no longer shut down; its reservation is kept for recovery")
                    continue
                erased = self.run("erase", member.udid)
            except BaseException:
                # A stop before the lease is handed back releases it once the member is shut down.
                self.release(lease, outcome=f"Stopped before {purpose}", shutdown=False)
                raise
            if erased.returncode != 0:
                self.release(lease, outcome=f"Erase failed before {purpose}: {erased.stderr.strip()}", shutdown=False)
                continue
            self.log(f"TOOK {member.name} ({member.udid})")
            return lease
        return self.create(device_type, runtime, purpose=purpose, label=label, preservation=preservation)

    def create(self, device_type, runtime, *, purpose, label=None, preservation=PRESERVATION):
        """The next member of the kind, reserved under the lowest free number; its first boot is the caller's."""
        device_type, runtime = qualified(DEVICE_TYPE, device_type), qualified(RUNTIME, runtime)
        # The pool's own label for the kind, else for the device type, else the type's display name.
        found = self.members()
        label = label or next((member.label for member in found if member.kind == (device_type, runtime)), None) \
            or next((member.label for member in found if member.device_type == device_type), None) \
            or self.label(device_type)
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
        created = self.run("create", key, device_type, runtime, timeout=300)
        udid = created.stdout.strip().upper() if created.returncode == 0 else None
        if udid is None:
            # An interrupted create can store the device without printing its UDID.
            found = [entry["udid"].upper() for entries in self.listing().get("devices", {}).values()
                     for entry in entries if entry.get("name") == key]
            if len(found) > 1:
                raise RuntimeError(f"{len(found)} simulators are named {key}; its reservation is kept for recovery")
            if not found:
                raise RuntimeError(f"simctl create {key} failed with status {created.returncode}: "
                                   f"{created.stderr.strip()}")
            udid = found[0]
        revision = self.record.update(key, owner=self.owner, revision=revision, device=udid,
                                      createdAt=self.clock().isoformat())["revision"]
        self.log(f"CREATED {key} ({udid}); its first boot finishes before it is shut down")
        return Lease(key, udid, revision, created=True)

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
def member(name, device_type, runtime, *, purpose, owner=None, boot_timeout=600, log=None):
    """A booted member of the pool for the block, then shut down and released however it ends.

    It raises BootTimedOut when the boot reaches its limit, RuntimeError when it fails, and
    ReleaseUnconfirmed after a block that ended normally when the release cannot confirm shutdown."""
    pool = Pool(name, owner=owner or default_owner() or f"{name}-{uuid.uuid4()}", log=log)
    lease = pool.take(device_type, runtime, purpose=purpose)
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


def main(argv, project=None):
    parser = argparse.ArgumentParser(prog="eng simulator-pool", description=__doc__.split("\n\n")[0])
    parser.add_argument("--pool", help="the pool's name, the start of every member's (default: the project's "
                        "simulatorPool in engineering.json)")
    parser.add_argument("--owner", help=f"the session reservations name (default: ${device_record.OWNER}, else the "
                        "agent's session)")
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("list", help="every member, its state and whether pruning may delete it")
    take = commands.add_parser("take", help="reserve, erase and boot a free member of a kind, or create the next; "
                               "prints its UDID")
    take.add_argument("--device", required=True, metavar="TYPE", help="a device type, such as iPhone-17-Pro")
    take.add_argument("--runtime", required=True, help="a runtime, such as iOS-27-1")
    take.add_argument("--label", help="a new member's label (default: the device type's name)")
    take.add_argument("--purpose", default="Simulator tests")
    take.add_argument("--boot-timeout", type=int, default=600, help="seconds the boot may take (default 600); "
                      f"past it the member is released and the command exits {evidence.UNMEASURED}")
    release = commands.add_parser("release", help="shut a member you hold down and release it")
    release.add_argument("udid")
    release.add_argument("--outcome", default="Released")
    prune = commands.add_parser("prune", help="delete members unused for 14 days, never the lowest of a kind")
    prune.add_argument("udids", nargs="*", metavar="UDID", help="only these members")
    args = parser.parse_args(argv)
    name = args.pool or configured(project or Path.cwd()) or configured(Path.cwd())
    owner = args.owner or default_owner()
    if not name:
        parser.error("name the pool with --pool, or in engineering.json's simulatorPool")
    if not owner and args.action != "list":
        parser.error(f"name the owner with --owner or ${device_record.OWNER}")
    try:
        pool = Pool(name, owner=owner)
        if args.action == "list":
            for member, allowed, reason in pool.eligible():
                print(f"{member.name}  {member.udid}  {member.runtime.rsplit('.', 1)[-1]}  {member.state}  "
                      f"{'prunable' if allowed else 'kept'}: {reason}")
        elif args.action == "take":
            # A stop runs the cleanup below rather than leaving a held, booted member.
            for number in (signal.SIGTERM, signal.SIGHUP):
                signal.signal(number, stopped)
            lease = pool.take(args.device, args.runtime, purpose=args.purpose, label=args.label)
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
