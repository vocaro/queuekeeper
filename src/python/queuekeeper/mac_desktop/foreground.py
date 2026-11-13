"""A Mac's foreground for one run: the second Mac's taken by queuekeeper, this Mac's only within the owner's window.

The second Mac is used only for file serving and build hosting, not as a workstation, so a run takes
its whole foreground, screen, keyboard, pointer, focus and speech, without asking the owner (owner's
decisions, 2026-10-30 and 2026-11-06, vocaro-engineering#207). queuekeeper takes it for a run as admission takes
a slot: it waits while another run holds the second Mac's mac-foreground entry, reserves the entry,
which records no approval, runs the work with
QUEUEKEEPER_DEVICE_OWNER naming the reservation, so that drivers' `devices check` passes, and releases it.
This Mac is the owner's workstation. Its foreground needs a window the owner agreed beforehand, which
the session reserves itself (guides/shared-host.md, Attended Mac sessions), and queuekeeper never sends
foreground work to it.

A Mac's device record that defers physical devices to another Mac's is the second Mac's: every run
sent there makes it so (offload_host.defer_devices).
"""
import contextlib
from datetime import datetime, timedelta, timezone
import os
import subprocess
import sys
import time

from . import reservations
from .. import lane_wait, offload_host, processes

# Seconds between looks at the second Mac's mac-foreground entry while another run holds it.
POLL = 10
# Seconds a run is expected to hold the foreground when its lane names no timeout. A deadline releases
# nothing: the run releases the entry when its work ends, however long that takes.
EXPECTED = 3 * 3600
GUIDE = "guides/foreground.md"
# Each (owner, record=, said=, **options) -> a context held for the run while it holds the foreground: what an
# extension that builds on this one saves before the run and puts back after it, as the Apple extension does
# VoiceOver's state. options are slot's own, passed on. A guard that could not put back what it changed raises
# an error whose `restored` is False, and the foreground then stays held for attended recovery.
GUARDS = []


def second_mac(record=None):
    """Whether this Mac is the second Mac: its device record defers physical devices to another Mac's."""
    return bool((record or reservations.Record()).load().get(reservations.DEFERRED))


def front():
    """The application in front, by LaunchServices' ASN and name, or None when it cannot be read."""
    try:
        asn = subprocess.run(["lsappinfo", "front"], capture_output=True, text=True, timeout=10).stdout.strip()
        if not asn: return None
        found = subprocess.run(["lsappinfo", "info", "-only", "name", asn], capture_output=True, text=True,
                               timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    name = found.split('"')[1] if found.count('"') >= 2 else "?"
    return f"{name} ({asn})"


def problems():
    """offload_host.foreground_problems on this Mac, as (problem, fix) pairs; none when it is ready."""
    return [tuple(item) for item in offload_host.foreground_problems()]


def refusal(found):
    lines = [f"- {problem}: {fix}" for problem, fix in found]
    return reservations.Refused("the second Mac's foreground is not ready for work started over SSH, and setting it up is "
                           f"the owner's ({GUIDE}):\n" + "\n".join(lines))


@contextlib.contextmanager
def slot(purpose, *, timeout, expected=EXPECTED, record=None, poll=POLL, ready=None, said=None, **options):
    """Hold the second Mac's mac-foreground for the run inside the block, without asking the owner.

    It refuses on any other Mac, and, before taking anything, on a second Mac that problems() finds not
    ready, which is a configuration problem for the owner. While another run holds the entry it waits,
    up to timeout seconds, then raises TimeoutError; a stop raises processes.Interrupted before taking
    the foreground. An orphaned or uncertain entry needs attended recovery, except a case that a guard's own
    extension lets the run recover itself, while it runs. Only after acquisition, each of GUARDS
    is entered for the run, with options, and left before the release.
    The entry records this process as its active run, so that the entry outlives a run that
    dies only as an orphan, and QUEUEKEEPER_DEVICE_OWNER names it for the block. The release records the
    block's outcome and whether the application in front before the run is in front after it."""
    record = record or reservations.Record()
    said = said or (lambda text: print(text, file=sys.stderr, flush=True))
    if not second_mac(record):
        raise reservations.Refused("queuekeeper takes the foreground only on the second Mac; this Mac's needs a window the owner "
                              "agreed beforehand, which the session reserves as mac-foreground itself "
                              "(guides/shared-host.md, Attended Mac sessions)")
    found = (ready or problems)()
    if found: raise refusal(found)
    owner = f"queuekeeper {os.getpid()} on {offload_host.mac_name()}"
    deadline, announced = time.monotonic() + timeout, None
    while True:
        entry = record.entry(reservations.FOREGROUND)
        if processes.requested() is not None:
            raise processes.Interrupted(processes.requested(), "while waiting for the second Mac's foreground")
        state = reservations.condition(entry) if entry is not None else "released"
        if state == "released":
            release_by = (datetime.now(timezone.utc) + timedelta(seconds=expected)).isoformat()
            try:
                entry = record.reserve(reservations.FOREGROUND, owner=owner, revision=(entry or {}).get("revision", 0),
                                       purpose=purpose, release_by=release_by, title=os.environ.get("QUEUEKEEPER_DISPATCHED_FROM"))
                break
            except reservations.Stale:
                continue
        if state in ("orphan", "uncertain"):
            raise reservations.Refused(f"the second Mac's {reservations.FOREGROUND} is {state}, held by {entry.get('ownerChatID')} "
                                  f"for {entry.get('purpose')}; it needs attended recovery first, on the second Mac: "
                                  f"`python3 -m queuekeeper.apple.devices recover {reservations.FOREGROUND} --by SESSION "
                                  f"--revision {entry.get('revision')} --verified WHAT` ({GUIDE})")
        holder = entry.get("ownerChatID")
        lane_wait.waiting(lane_wait.FOREGROUND, f"held by {holder} for {entry.get('purpose')}, expected release "
                          f"{entry.get('expectedReleaseBy')}")
        if holder != announced:
            said(f"queuekeeper: waiting for the second Mac's foreground, held by {holder} for {entry.get('purpose')}, "
                 f"expected release {entry.get('expectedReleaseBy')}")
            announced = holder
        if time.monotonic() >= deadline:
            raise TimeoutError(f"the second Mac's foreground stayed held by {holder} for {timeout:g}s")
        time.sleep(poll)
    entry = record.update(reservations.FOREGROUND, owner=owner, revision=entry["revision"], activeRun=True,
                          hostProcessIdentifier=os.getpid())
    identity = reservations.identity(entry)
    before = front()
    said(f"queuekeeper: holding the second Mac's foreground as {owner} for {purpose}; {before or 'no application'} is in front")
    previous = os.environ.get(reservations.OWNER)
    os.environ[reservations.OWNER] = owner
    outcome = "ended by an exception"
    uncertain = False
    try:
        with contextlib.ExitStack() as guards:
            for guard in GUARDS: guards.enter_context(guard(owner, record=record, said=said, **options))
            yield owner
        outcome = "the run's work ended"
    except BaseException as error:
        # A guard's failure whose original state it verified afterwards leaves the lease to release.
        uncertain = getattr(error, "restored", True) is False
        raise
    finally:
        if previous is None: os.environ.pop(reservations.OWNER, None)
        else: os.environ[reservations.OWNER] = previous
        after = front()
        current = record.entry(reservations.FOREGROUND)
        same_lease = current is not None and current.get("ownerChatID") == owner \
            and reservations.identity(current) == identity
        if uncertain or not same_lease or reservations.condition(current) in ("orphan", "uncertain", "released"):
            said("queuekeeper: foreground retained: a guard's restoration or owned-process state is unverified")
        else:
            entry = record.update(reservations.FOREGROUND, owner=owner, revision=current["revision"], activeRun=False)
            restored = before is not None and before == after
            record.release(reservations.FOREGROUND, owner=owner, revision=entry["revision"], outcome=outcome,
                           restoration=f"in front before the run: {before or 'unread'}; after it: {after or 'unread'}; "
                                       "the lease's guards put back what they changed, or changed nothing",
                           frontmost="restored" if restored else "not-ours")
            said(f"queuekeeper: released the second Mac's foreground; {after or 'no application'} is in front")


def main(argv=None):
    """`python3 -m queuekeeper.mac_desktop.foreground check`: what this Mac lacks for foreground work started over
    SSH, each with the owner's fix; exit 0 when nothing."""
    argv = sys.argv[1:] if argv is None else argv
    if argv != ["check"]:
        print("usage: python3 -m queuekeeper.mac_desktop.foreground check", file=sys.stderr)
        return 2
    found = problems()
    for problem, fix in found: print(f"{problem}: {fix}")
    if not found: print("ready for foreground work started over SSH")
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
