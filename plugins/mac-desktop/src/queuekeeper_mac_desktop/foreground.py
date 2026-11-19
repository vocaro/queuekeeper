"""A Mac's foreground for one run: another Mac's taken by queuekeeper, this Mac's only within the owner's window.

Another Mac is used only for file serving and build hosting, not as a workstation, so a run takes
its whole foreground, screen, keyboard, pointer, focus and speech, without asking the owner (owner's
decisions, 2026-10-30 and 2026-11-06, vocaro-engineering#207). queuekeeper takes it for a run as admission takes
a slot: it waits while another run holds another Mac's mac-foreground entry, reserves the entry,
which records no approval, runs the work with
QUEUEKEEPER_DEVICE_OWNER naming the reservation, so that drivers' `devices check` passes, and releases it.
This Mac is the owner's workstation. Its foreground needs a window the owner agreed beforehand, which
the session reserves itself, and queuekeeper never sends
foreground work to it.

Which Mac is which is each Mac's own setting, `takes.foreground` in its mac-settings.json, which
`qk farm foreground NAME` writes (reservations.takes_foreground; the owner's decision, 2026-11-17,
vocaro/queuekeeper#211): a Mac whose settings say it takes foreground work has it taken for a run, and any
other is a workstation. Until then a Mac was told by its device record deferring physical devices to another
Mac's, which every run sent there makes it do (offload_host.defer_devices).
"""
import contextlib
from datetime import datetime, timedelta, timezone
import os
import subprocess
import sys
import time

from . import opened_apps, reservations
from queuekeeper import lane_wait, offload_host, processes

# Seconds between looks at another Mac's mac-foreground entry while another run holds it.
POLL = 10
# Seconds a run is expected to hold the foreground when its lane names no timeout. A deadline releases
# nothing: the run releases the entry when its work ends, however long that takes.
EXPECTED = 3 * 3600
# Seconds a waiter keeps looking once the holder's expected release falls after its own deadline, in case the holder
# releases early, before it ends with nothing run (vocaro/queuekeeper#95). A holder already ending its run
# releases within about this long: its command has up to processes.FOREGROUND_KILL_AFTER, 120 s, between SIGTERM and
# SIGKILL, and its VoiceOver restoration settles after that. An early release past it is no likelier in the next few
# minutes than in any others of the holder's window, so a longer grace would only spend the waiter's time.
GRACE = 180
# Each (owner, record=, said=, **options) -> a context held for the run while it holds the foreground: what an
# extension that builds on this one saves before the run and puts back after it, as the Apple extension does
# VoiceOver's state. options are slot's own, passed on. A guard that could not put back what it changed raises
# an error whose `restored` is False, and the foreground then stays held for attended recovery.
GUARDS = []


def farm_mac(record=None):
    """Whether this Mac's foreground is queuekeeper's to take for a run: its own settings say it takes foreground
    work, on whichever Mac that is (reservations.takes_foreground; the owner's decision, 2026-11-17,
    vocaro/queuekeeper#211). record is no longer read: until then a Mac was one when its device record deferred
    physical devices to another Mac's."""
    return reservations.takes_foreground()


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
    return reservations.Refused(f"{offload_host.mac_name()}'s foreground is not ready for work started over SSH, and setting it up is "
                           "the owner's:\n" + "\n".join(lines))


@contextlib.contextmanager
def slot(purpose, *, timeout, expected=EXPECTED, record=None, poll=POLL, ready=None, said=None, again=None, **options):
    """Hold another Mac's mac-foreground for the run inside the block, without asking the owner.

    It refuses on any other Mac, and, before taking anything, on another Mac that problems() finds not
    ready, which is a configuration problem for the owner. While another run holds the entry it waits,
    up to timeout seconds, then raises TimeoutError. A holder whose expected release falls after that deadline
    is judged at once: the wait says so, looks for GRACE seconds more in case it releases early, and then raises
    TimeoutError naming the release and again, the command to run after it (vocaro/queuekeeper#95). A new holder is
    judged anew. A stop raises processes.Interrupted before taking
    the foreground. An orphaned or uncertain entry needs attended recovery, except a case that a guard's own
    extension lets the run recover itself, while it runs. Only after acquisition, each of GUARDS
    is entered for the run, with options, and left before the release.
    The entry records this process as its active run, so that the entry outlives a run that
    dies only as an orphan, and QUEUEKEEPER_DEVICE_OWNER names it for the block. The release records the
    block's outcome and whether the application in front before the run is in front after it. Before it releases
    the foreground, while the lease is still the run's, it quits the apps the run opened and reports those it leaves
    running (opened_apps.settle, vocaro/queuekeeper#104)."""
    record = record or reservations.Record()
    said = said or (lambda text: print(text, file=sys.stderr, flush=True))
    if not farm_mac(record):
        raise reservations.Refused("queuekeeper takes the foreground only on a Mac whose own settings say it takes "
                              "foreground work (`qk farm foreground NAME`); this Mac's needs a window the owner "
                              "agreed beforehand, which the session reserves as mac-foreground itself")
    found = (ready or problems)()
    if found: raise refusal(found)
    owner = f"queuekeeper {os.getpid()} on {offload_host.mac_name()}"
    began = time.monotonic()
    deadline, announced = began + timeout, None
    # The wait's deadline as a time, to judge a holder's expected release by, and when the wait gives up on a holder
    # whose release falls after it: None while it waits for the deadline, as for a holder with no expected release.
    ends, judged, gives_up = datetime.now(timezone.utc) + timedelta(seconds=timeout), None, None
    again = again or "the same command"
    while True:
        entry = record.entry(reservations.FOREGROUND)
        if processes.requested() is not None:
            raise processes.Interrupted(processes.requested(), f"while waiting for {offload_host.mac_name()}'s foreground")
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
            raise reservations.Refused(f"{offload_host.mac_name()}'s {reservations.FOREGROUND} is {state}, held by {entry.get('ownerChatID')} "
                                  f"for {entry.get('purpose')}; it needs attended recovery first, on that Mac: "
                                  f"`{offload_host.recover_command(reservations.FOREGROUND, entry.get('revision'), 'WHAT')}`, "
                                  "or from another Mac the same with `--host` and that Mac's SSH name after "
                                  "`recover`, never as a command given to ssh")
        holder, due = entry.get("ownerChatID"), entry.get("expectedReleaseBy")
        lane_wait.waiting(lane_wait.FOREGROUND, f"held by {holder} for {entry.get('purpose')}, expected release {due}")
        now = time.monotonic()
        if (holder, due) != judged:
            # Judged against each holder, and again when its expected release moves.
            release = reservations.moment(due)
            gives_up = min(now + GRACE, deadline) if release is not None and release > ends else None
            judged = (holder, due)
        if judged != announced:
            after = (f", after this wait's deadline, {ends.isoformat()}: looking again for {gives_up - now:.0f}s in case "
                     f"it releases early, then ending with nothing run; run `{again}` after {due}"
                     if gives_up is not None else "")
            said(f"queuekeeper: waiting for {offload_host.mac_name()}'s foreground, held by {holder} for {entry.get('purpose')}, "
                 f"expected release {due}{after}")
            announced = judged
        late = (f"; its expected release, {due}, falls after this wait's deadline, {ends.isoformat()}. Run `{again}` "
                f"after {due}" if gives_up is not None else "")
        if now >= deadline:
            raise TimeoutError(f"{offload_host.mac_name()}'s foreground stayed held by {holder} for {timeout:g}s{late}")
        if gives_up is not None and now >= gives_up:
            raise TimeoutError(f"{offload_host.mac_name()}'s foreground stayed held by {holder} for {now - began:.0f}s{late}")
        time.sleep(poll)
    entry = record.update(reservations.FOREGROUND, owner=owner, revision=entry["revision"], activeRun=True,
                          hostProcessIdentifier=os.getpid())
    identity = reservations.identity(entry)
    before = front()
    opened, own = opened_apps.read(), opened_apps.checkout()
    said(f"queuekeeper: holding {offload_host.mac_name()}'s foreground as {owner} for {purpose}; {before or 'no application'} is in front")
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
        current = record.entry(reservations.FOREGROUND)
        same_lease = current is not None and current.get("ownerChatID") == owner \
            and reservations.identity(current) == identity
        # Only while the lease is the run's: an app that opened after another run took the foreground is that run's.
        launched = (current or {}).get("launchedApps", [])
        try:
            # Any Mac but the second is the owner's workstation, where only the apps the run is shown to own are its.
            settled = opened_apps.settle(opened, own, said=said, launched=launched,
                                         workstation=not farm_mac(record)) \
                if same_lease and reservations.condition(current) != "released" else None
        except Exception as error:  # a release goes ahead however the apps' look went
            said(f"queuekeeper: could not quit the apps the run opened: {error}")
            settled = {"quit": [], "left": [f"the running apps could not be settled: {error}"]}
        apps = opened_apps.summary(settled) or \
            (f"apps the run opened: none of {', '.join(launched)} ran at the release" if launched else None)
        after = front()
        if uncertain or not same_lease or reservations.condition(current) in ("orphan", "uncertain", "released"):
            said("queuekeeper: foreground retained: a guard's restoration or owned-process state is unverified")
        else:
            entry = record.update(reservations.FOREGROUND, owner=owner, revision=current["revision"], activeRun=False)
            restored = before is not None and before == after
            record.release(reservations.FOREGROUND, owner=owner, revision=entry["revision"], outcome=outcome,
                           restoration=f"in front before the run: {before or 'unread'}; after it: {after or 'unread'}; "
                                       "the lease's guards put back what they changed, or changed nothing"
                                       + (f"; {apps}" if apps else ""),
                           frontmost="restored" if restored else "not-ours", apps_quit=apps)
            said(f"queuekeeper: released {offload_host.mac_name()}'s foreground; {after or 'no application'} is in front")


def main(argv=None):
    """`python3 -m queuekeeper_mac_desktop.foreground check`: what this Mac lacks for foreground work started over
    SSH, each with the owner's fix; exit 0 when nothing."""
    argv = sys.argv[1:] if argv is None else argv
    if argv != ["check"]:
        print("usage: python3 -m queuekeeper_mac_desktop.foreground check", file=sys.stderr)
        return 2
    found = problems()
    for problem, fix in found: print(f"{problem}: {fix}")
    if not found: print("ready for foreground work started over SSH")
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
