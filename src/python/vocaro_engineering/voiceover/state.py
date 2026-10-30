"""VoiceOver state belongs to the current foreground lease, never to a waiting run.

``preserved(owner)`` leaves the initial state in place and restores it on exit. A passive
test opts into ``preserved(owner, target=False)`` to run quietly. A speech driver can enable
VoiceOver inside either context; its inner restoration finishes before the outer lease ends.
No preference is written: a checked Command-F5 is sent only when a known state differs.
"""
import contextlib
import json
import os
import subprocess
import time

from .. import devices
from . import guard

WAIT = 5
POLL = 0.1
READ = """ObjC.import('AppKit');
var enabled = $.NSWorkspace.sharedWorkspace.isVoiceOverEnabled;
if (typeof enabled !== 'boolean') { throw new Error('VoiceOver state is not a boolean'); }
JSON.stringify(enabled);"""


class UnknownState(RuntimeError):
    """Neither on nor off is established; no toggle may be inferred from it."""


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


def _known(lease, reader, *, restoring=False):
    lease.check(restoring=restoring)
    value = reader()
    if type(value) is not bool:
        raise UnknownState("VoiceOver state is not a known boolean")
    lease.check(restoring=restoring)
    return value


def read(owner=None, *, record=None):
    """Known on/off under this owner's exact foreground lease; nothing is changed."""
    return _known(_Lease(owner or os.environ.get(devices.OWNER), record), _read)


def _set(lease, desired, reader, toggle, *, restoring=False, wait=WAIT, poll=POLL):
    if type(desired) is not bool: raise ValueError("VoiceOver target must be on or off")
    if _known(lease, reader, restoring=restoring) == desired: return
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
    """Choose a known state while holding the lease, normally inside preserved()."""
    _set(_Lease(owner or os.environ.get(devices.OWNER), record), desired, _read, _toggle)


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
    lease = _Lease(owner or os.environ.get(devices.OWNER), record)
    reader, toggle = read_state or _read, toggle or _toggle
    said = said or (lambda text: None)
    # Nothing has been changed if this strict initial read fails.
    original = _known(lease, reader)
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
