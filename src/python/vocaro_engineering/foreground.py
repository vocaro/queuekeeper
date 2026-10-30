"""A Mac's foreground for one run: the second Mac's taken by eng, this Mac's only within the owner's window.

The second Mac is not used as a workstation, so the owner approved, once, taking its whole foreground,
screen, keyboard, pointer, focus and speech, for any run eng admits there (owner's decision, 2026-10-30).
eng takes it for a run as admission takes a slot: it waits while another run holds the second Mac's
mac-foreground entry, reserves the entry with that standing approval, runs the work with
VOCARO_DEVICE_OWNER naming the reservation, so that drivers' `devices check` passes, and releases it.
This Mac is the owner's workstation. Its foreground needs a window the owner agreed beforehand, which
the session reserves itself (guides/shared-host.md, Attended Mac sessions), and eng never sends
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

from . import devices, offload_host
from .voiceover import state as voiceover_state

# The owner's standing approval of the second Mac's foreground, in the owner's words.
STANDING = {"by": "owner", "at": "2026-10-30T16:53:26Z",
            "words": "it is not used as a workstation so taking over the entire mac foreground for a voiceover "
                     "test is not a problem"}
# Seconds between looks at the second Mac's mac-foreground entry while another run holds it.
POLL = 10
# Seconds a run is expected to hold the foreground when its lane names no timeout. A deadline releases
# nothing: the run releases the entry when its work ends, however long that takes.
EXPECTED = 3 * 3600
GUIDE = "guides/second-mac-foreground.md"


def second_mac(record=None):
    """Whether this Mac is the second Mac: its device record defers physical devices to another Mac's."""
    return bool((record or devices.Record()).load().get(devices.DEFERRED))


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
    return devices.Refused("the second Mac's foreground is not ready for work started over SSH, and setting it up is "
                           f"the owner's ({GUIDE}):\n" + "\n".join(lines))


@contextlib.contextmanager
def slot(purpose, *, timeout, expected=EXPECTED, record=None, poll=POLL, ready=None, voiceover=None, said=None):
    """Hold the second Mac's mac-foreground for the run inside the block, with the owner's standing approval.

    It refuses on any other Mac, and, before taking anything, on a second Mac that problems() finds not
    ready, which is a configuration problem for the owner. While another run holds the entry it waits,
    up to timeout seconds, then raises TimeoutError; an orphaned or uncertain entry needs attended
    recovery. Only after acquisition, the run captures and preserves the actual VoiceOver state.
    A passive driver explicitly opts into voiceover.state.preserved(target=False) inside this lease.
    The entry records this process as its active run, so that the entry outlives a run that
    dies only as an orphan, and VOCARO_DEVICE_OWNER names it for the block. The release records the
    block's outcome and whether the application in front before the run is in front after it."""
    record = record or devices.Record()
    said = said or (lambda text: print(text, file=sys.stderr, flush=True))
    if not second_mac(record):
        raise devices.Refused("eng takes the foreground only on the second Mac; this Mac's needs a window the owner "
                              "agreed beforehand, which the session reserves as mac-foreground itself "
                              "(guides/shared-host.md, Attended Mac sessions)")
    found = (ready or problems)()
    if found: raise refusal(found)
    owner = f"eng {os.getpid()} on {offload_host.mac_name()}"
    deadline, announced = time.monotonic() + timeout, None
    while True:
        entry = record.entry(devices.FOREGROUND)
        state = devices.condition(entry) if entry is not None else "released"
        if state == "released":
            release_by = (datetime.now(timezone.utc) + timedelta(seconds=expected)).isoformat()
            try:
                entry = record.reserve(devices.FOREGROUND, owner=owner, revision=(entry or {}).get("revision", 0),
                                       purpose=purpose, release_by=release_by, title=os.environ.get("VOCARO_DISPATCHED_FROM"),
                                       approval={**STANDING, "until": "released"})
                break
            except devices.Stale:
                continue
        if state in ("orphan", "uncertain"):
            raise devices.Refused(f"the second Mac's {devices.FOREGROUND} is {state}, held by {entry.get('ownerChatID')} "
                                  f"for {entry.get('purpose')}; it needs attended recovery (`devices recover`) first")
        holder = entry.get("ownerChatID")
        if holder != announced:
            said(f"eng: waiting for the second Mac's foreground, held by {holder} for {entry.get('purpose')}, "
                 f"expected release {entry.get('expectedReleaseBy')}")
            announced = holder
        if time.monotonic() >= deadline:
            raise TimeoutError(f"the second Mac's foreground stayed held by {holder} for {timeout:g}s")
        time.sleep(poll)
    entry = record.update(devices.FOREGROUND, owner=owner, revision=entry["revision"], activeRun=True,
                          hostProcessIdentifier=os.getpid())
    identity = voiceover_state._Lease._identity(entry)
    before = front()
    said(f"eng: holding the second Mac's foreground as {owner} for {purpose}; {before or 'no application'} is in front")
    previous = os.environ.get(devices.OWNER)
    os.environ[devices.OWNER] = owner
    outcome = "ended by an exception"
    uncertain = False
    try:
        with voiceover_state.preserved(owner, record=record, read_state=voiceover, said=said):
            yield owner
        outcome = "the run's work ended"
    except voiceover_state.TransitionFailed:
        uncertain = True
        raise
    finally:
        if previous is None: os.environ.pop(devices.OWNER, None)
        else: os.environ[devices.OWNER] = previous
        after = front()
        current = record.entry(devices.FOREGROUND)
        same_lease = current is not None and current.get("ownerChatID") == owner \
            and voiceover_state._Lease._identity(current) == identity
        if uncertain or not same_lease or devices.condition(current) in ("orphan", "uncertain", "released"):
            said("eng: foreground retained: VoiceOver restoration or owned-process state is unverified")
        else:
            entry = record.update(devices.FOREGROUND, owner=owner, revision=current["revision"], activeRun=False)
            restored = before is not None and before == after
            record.release(devices.FOREGROUND, owner=owner, revision=entry["revision"], outcome=outcome,
                           restoration=f"in front before the run: {before or 'unread'}; after it: {after or 'unread'}; "
                                       "VoiceOver was preserved by the lease guard, or untouched after an initial read refusal",
                           frontmost="restored" if restored else "not-ours")
            said(f"eng: released the second Mac's foreground; {after or 'no application'} is in front")


def main(argv=None):
    """`python3 -m vocaro_engineering.foreground check`: what this Mac lacks for foreground work started over
    SSH, each with the owner's fix; exit 0 when nothing."""
    argv = sys.argv[1:] if argv is None else argv
    if argv != ["check"]:
        print("usage: python3 -m vocaro_engineering.foreground check", file=sys.stderr)
        return 2
    found = problems()
    for problem, fix in found: print(f"{problem}: {fix}")
    if not found: print("ready for foreground work started over SSH")
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
