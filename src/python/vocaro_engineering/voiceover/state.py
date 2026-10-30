"""VoiceOver state belongs to the current foreground lease, never to a waiting run.

``preserved(owner)`` leaves the initial state in place and restores it on exit. A passive
test opts into ``preserved(owner, target=False)`` to run quietly. A speech driver can enable
VoiceOver inside either context; its inner restoration finishes before the outer lease ends.
No preference is written: a checked Command-F5 is sent only when a known state differs.
"""
import contextlib
import contextvars
import json
import os
import subprocess
import time

from .. import devices
from . import guard

WAIT = 5
POLL = 0.1
QUIT_WAIT = 20  # Existing Mac speech-runner contract: a quitting VoiceOver ignores Command-F5.
VOICEOVER = "/System/Library/CoreServices/VoiceOver.app/Contents/MacOS/VoiceOver"
_CURRENT = contextvars.ContextVar("voiceover_lease", default=None)
READ = """ObjC.import('AppKit');
var enabled = $.NSWorkspace.sharedWorkspace.isVoiceOverEnabled;
if (typeof enabled !== 'boolean') { throw new Error('VoiceOver state is not a boolean'); }
JSON.stringify(enabled);"""


class UnknownState(RuntimeError):
    """Neither on nor off is established; no toggle may be inferred from it."""


class NotReady(UnknownState):
    """VoiceOver is off, but its previous process has not finished quitting; no enable key is safe."""


class TransitionFailed(RuntimeError):
    """A state change was attempted but not verified; the lease remains uncertain."""


def _read():
    """Public NSWorkspace.isVoiceOverEnabled, with exact boolean output and a bounded query."""
    try:
        result = subprocess.run(["osascript", "-l", "JavaScript", "-e", READ],
                                capture_output=True, text=True, timeout=WAIT)
        if result.returncode: raise UnknownState("Public VoiceOver state query failed")
        value = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        raise UnknownState(f"Cannot read VoiceOver state: {error}") from error
    if type(value) is not bool: raise UnknownState("Public VoiceOver state query did not return a boolean")
    return value


def _toggle():
    subprocess.run(["osascript", "-e", 'tell application "System Events" to key code 96 using {command down}'],
                   check=True, capture_output=True, text=True, timeout=WAIT)


class _Lease:
    def __init__(self, owner, record):
        self.owner = owner
        self.record = record or devices.Record()
        entry = devices.require_foreground(owner, self.record)
        self.identity = self._identity(entry)
        self.uncertainty_revision = None

    @staticmethod
    def _identity(entry):
        return (entry.get("reservedAt"), entry.get("hostProcessIdentifier"), entry.get("hostProcessStart"))

    def check(self, *, restoring=False):
        if restoring:
            entry = self.record.entry(devices.FOREGROUND)
            own_failure = entry is not None and self.uncertainty_revision is not None \
                and entry.get("revision") == self.uncertainty_revision \
                and entry.get("state") != "restoration-unverified" \
                and (not entry.get("activeRun") or devices.process_start(entry.get("hostProcessIdentifier"))
                     == entry.get("hostProcessStart"))
            if entry is None or entry.get("ownerChatID") != self.owner \
                    or (devices.condition(entry) not in ("held", "active") and not own_failure):
                raise devices.Refused("The original foreground lease no longer permits VoiceOver restoration")
        else:
            entry = devices.require_foreground(self.owner, self.record)
        identity = self._identity(entry)
        if identity != self.identity:
            raise devices.Refused("The foreground lease changed before VoiceOver state access")
        return entry

    def uncertain(self, reason):
        # Never write into a subsequent holder's entry, even if a PID/owner string is reused.
        try:
            entry = self.record.entry(devices.FOREGROUND)
            if entry is None or entry.get("ownerChatID") != self.owner or entry.get("state") == "released": return
            if self._identity(entry) != self.identity: return
            # Only this instance's own uncertainty may permit a restoration retry. Never
            # adopt a marker written by another state scope or any unrelated run failure.
            if devices.condition(entry) == "uncertain" and entry["revision"] != self.uncertainty_revision: return
            changed = self.record.update(devices.FOREGROUND, owner=self.owner, revision=entry["revision"],
                                         uncertain=True, voiceOverRestoration={"verified": False, "reason": reason})
            self.uncertainty_revision = changed["revision"]
        except (devices.Refused, OSError, TimeoutError):
            # The enclosing foreground scope also retains an active/orphan entry if its
            # record could not accept the uncertainty marker. Never mask the state failure.
            return


def _lease(owner, record):
    owner = owner or os.environ.get(devices.OWNER)
    record = record or devices.Record()
    current = _CURRENT.get()
    if current is not None and current.owner == owner and current.record.path.resolve() == record.path.resolve():
        # Share restoration provenance only within this process/context's active scope.
        # A replaced reservation or uncertain entry still refuses all new work.
        current.check()
        return current
    return _Lease(owner, record)


def _known(lease, reader, *, restoring=False):
    lease.check(restoring=restoring)
    value = reader()
    if type(value) is not bool:
        raise UnknownState("VoiceOver state is not a known boolean")
    lease.check(restoring=restoring)
    return value


def _ready_to_enable(lease, *, restoring=False, timeout=WAIT):
    """Process absence is readiness to enable, never an inference of VoiceOver's on/off state.

    The public getter remains the state authority. Check only this user's exact system
    VoiceOver executable, and recheck the lease around every bounded lifecycle query.
    """
    deadline = time.monotonic() + timeout
    def query(command):
        lease.check(restoring=restoring)
        remaining = deadline - time.monotonic()
        if remaining <= 0: raise NotReady("VoiceOver enable-readiness query exceeded its bound")
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=remaining)
        except (OSError, subprocess.SubprocessError) as error:
            lease.check(restoring=restoring)
            raise UnknownState(f"VoiceOver enable readiness could not be read: {error}") from error
        lease.check(restoring=restoring)
        if result.stderr: raise UnknownState("VoiceOver enable-readiness query reported an error")
        return result
    found = query(["pgrep", "-u", str(os.getuid()), "-x", "VoiceOver"])
    if found.returncode == 1 and found.stdout == "": return True
    pids = found.stdout.split()
    if found.returncode != 0 or not pids or any(not pid.isdigit() or int(pid) <= 0 for pid in pids):
        raise UnknownState("VoiceOver enable-readiness process query is unknown")
    for pid in pids:
        process = query(["ps", "-p", pid, "-o", "comm="])
        if process.returncode == 1 and process.stdout == "":
            # It ended after pgrep. A fresh pgrep, in the next bounded poll, proves absence.
            continue
        if process.returncode != 0 or process.stdout.strip() != VOICEOVER:
            raise UnknownState("VoiceOver enable readiness found an unrecognized process")
    return False


def _wait_to_enable(lease, ready, *, restoring=False, wait=QUIT_WAIT, poll=POLL):
    deadline = time.monotonic() + wait
    while True:
        lease.check(restoring=restoring)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise NotReady(f"VoiceOver is off but has not finished quitting within {wait:g}s; no enable key sent")
        value = ready(lease, restoring=restoring, timeout=min(WAIT, remaining))
        lease.check(restoring=restoring)
        if type(value) is not bool: raise UnknownState("VoiceOver enable readiness is not a known boolean")
        if value: return
        time.sleep(min(poll, max(0, deadline - time.monotonic())))


def read(owner=None, *, record=None):
    """Known on/off under this owner's exact foreground lease; nothing is changed."""
    return _known(_lease(owner, record), _read)


def _set(lease, desired, reader, toggle, *, restoring=False, wait=WAIT, poll=POLL, ready=None, ready_wait=QUIT_WAIT):
    if type(desired) is not bool: raise ValueError("VoiceOver target must be on or off")
    if _known(lease, reader, restoring=restoring) == desired: return
    if desired:
        _wait_to_enable(lease, ready or _ready_to_enable, restoring=restoring, wait=ready_wait, poll=poll)
        # The state can change while the previous process quits. Never toggle an already-on state.
        if _known(lease, reader, restoring=restoring): return
    lease.check(restoring=restoring)
    try:
        toggle()
        deadline = time.monotonic() + wait
        while True:
            try:
                if _known(lease, reader, restoring=restoring) == desired: return
            except UnknownState:
                # A temporary query failure while the known transition settles is bounded.
                pass
            if time.monotonic() >= deadline:
                raise UnknownState(f"VoiceOver did not reach {'on' if desired else 'off'} within {wait:g}s")
            time.sleep(poll)
    except Exception as error:
        lease.uncertain(f"VoiceOver transition to {'on' if desired else 'off'} was not verified: {error}")
        raise TransitionFailed(f"VoiceOver transition was not verified; foreground lease retained: {error}") from error


def set_enabled(desired, owner=None, *, record=None):
    """Choose a known state inside this process's preserved() scope for failure restoration.
    The outer eng process is a separate guard; it does not share a child driver's context.
    """
    _set(_lease(owner, record), desired, _read, _toggle)


@contextlib.contextmanager
def preserved(owner=None, *, target=None, record=None, read_state=None, toggle=None, said=None):
    """Capture after acquisition; optionally choose target; restore before lease release.

    target=None preserves existing clients' initial on/off behavior. target=False is the
    explicit quiet-test opt-in. Dependencies are injectable for offline contract tests.
    An initial read failure acted on nothing and may release cleanly. A failed transition,
    unreadable final state or lost ownership retains uncertainty for attended recovery.
    Existing eng/driver signal handlers keep ownership; otherwise stop_signals handles stops.
    """
    if target is not None and type(target) is not bool: raise ValueError("VoiceOver target must be on or off")
    lease = _lease(owner, record)
    reader, toggle = read_state or _read, toggle or _toggle
    said = said or (lambda text: None)
    # Nothing has been changed if this strict initial read fails.
    original = _known(lease, reader)
    token = _CURRENT.set(lease)
    try:
        with guard.stop_signals(only_unhandled=True):
            try:
                said(f"VoiceOver original {'on' if original else 'off'}")
                if target is not None: _set(lease, target, reader, toggle)
                yield original
            finally:
                # The body and its supervised children have ended. A first stop arriving during
                # bounded restoration is delivered only after the original state is verified.
                with guard.restoring_signals():
                    try:
                        _set(lease, original, reader, toggle, restoring=True)
                        said(f"VoiceOver restored to original {'on' if original else 'off'}")
                    except BaseException as error:
                        lease.uncertain(f"VoiceOver original state restoration was not verified: {error}")
                        raise TransitionFailed(f"VoiceOver original state was not restored; foreground lease retained: {error}") from error
    finally:
        _CURRENT.reset(token)
