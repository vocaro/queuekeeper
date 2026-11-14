"""VoiceOver state belongs to the current foreground lease, never to a waiting run.

``preserved(owner)`` leaves the initial state in place and restores it on exit. A passive
test opts into ``preserved(owner, target=False)`` to run quietly. A speech driver can enable
VoiceOver inside either context; its inner restoration finishes before the outer lease ends.
No preference is written: a checked Command-F5 is sent only when a known state differs.

Every bound is stretched by the host's load (#137): on a second Mac at a load average near 200 a fixed
5 s Command-F5 timed out, and the late key left VoiceOver on. A load under one per core beside a saturated CPU
is doubted and stretches by the limit (vocaro/queuekeeper#43). A transition that fails inside a
preserved() scope is not yet uncertainty: the scope restores the original state, watches it stay
there for a stretched settle window, and only an unverified restoration retains the lease.
"""
import contextlib
import contextvars
import json
import os
import subprocess
import time

from .. import devices
from ...mac_desktop import reservations
from ... import waiting
from . import guard

WAIT = 5
POLL = 0.1
QUIT_WAIT = 20  # Existing Mac speech-runner contract: a quitting VoiceOver ignores Command-F5.
# After a transition that failed or timed out, how long restoration watches the original state stay put, so that
# a key System Events delivers late is seen and undone under the same lease. Stretched by load like every bound.
SETTLE = 5
# How many times restoration sets the original state again when it did not hold.
RESTORE_ATTEMPTS = 3
# The most any bound stretches: 24 times, 120 s for a 5 s bound, at a load of 24 per core or more.
STRETCH_LIMIT = 24
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
    """A state change was attempted but not verified. restored is True when the enclosing preserved() scope then
    verified the original state under the same lease, which then releases; otherwise the lease remains uncertain."""
    restored = False


def stretch_reading():
    """The reading this module's bounds stretch by now, as waiting.stretch_reading gives it, its factor at most
    STRETCH_LIMIT and the limit itself when the load cannot be read or reads under one per core beside a saturated
    CPU: on 2026-11-12 build-host's load read 10 or less on 10 cores, with the CPU 100% busy and the loads around
    it 490 to 630, and a 5 s Command-F5 bound ended KEY_LATE (vocaro/queuekeeper#43)."""
    reading = waiting.stretch_reading()
    factor = STRETCH_LIMIT if reading["load"] is None else min(STRETCH_LIMIT, reading["factor"])
    return {**reading, "factor": factor}


def stretched(seconds, reading=None):
    """seconds scaled by stretch_reading's factor, or by that of reading, a reading already taken: the one-minute
    load average per core, as the tests' host_isolation.stretched scales theirs, at most STRETCH_LIMIT times."""
    return seconds * (reading or stretch_reading())["factor"]


def _public(timeout=None):
    """Public NSWorkspace.isVoiceOverEnabled, with exact boolean output and a bounded query."""
    try:
        result = subprocess.run(["osascript", "-l", "JavaScript", "-e", READ],
                                capture_output=True, text=True, timeout=stretched(WAIT) if timeout is None else timeout)
        if result.returncode: raise UnknownState("Public VoiceOver state query failed")
        value = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        raise UnknownState(f"Cannot read VoiceOver state: {error}") from error
    if type(value) is not bool: raise UnknownState("Public VoiceOver state query did not return a boolean")
    return value


def _process(query):
    """Whether this user's exact system VoiceOver executable runs: True, False when pgrep finds none, None when
    every process it found ended before ps read it (a fresh pgrep decides). UnknownState otherwise."""
    found = query(["pgrep", "-u", str(os.getuid()), "-x", "VoiceOver"])
    if found.returncode == 1 and found.stdout == "": return False
    pids = found.stdout.split()
    if found.returncode != 0 or not pids or any(not pid.isdigit() or int(pid) <= 0 for pid in pids):
        raise UnknownState("VoiceOver process query is unknown")
    running = None
    for pid in pids:
        process = query(["ps", "-p", pid, "-o", "comm="])
        if process.returncode == 1 and process.stdout == "":
            continue
        if process.returncode != 0 or process.stdout.strip() != VOICEOVER:
            raise UnknownState("VoiceOver process query found an unrecognized process")
        running = True
    return running


def _bounded(command):
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=stretched(WAIT))
    except (OSError, subprocess.SubprocessError) as error:
        raise UnknownState(f"VoiceOver process could not be read: {error}") from error
    if result.stderr: raise UnknownState("VoiceOver process query reported an error")
    return result


def _read(public=None, running=None, *, wait=None, poll=POLL):
    """The public getter's on/off, except that on needs VoiceOver's process too.

    isVoiceOverEnabled follows com.apple.universalaccess voiceOverOnOffKey, which quitting VoiceOver through
    AppleScript leaves at 1 (#137): the next run then took a VoiceOver that was not running as on and sent no
    Command-F5. An on that no VoiceOver process confirms within a stretched WAIT, which allows a process that is
    still starting, is that stale key, and reads off: nothing that is not running can speak. An off is the getter's alone; a
    process still quitting is the enable-readiness check's concern."""
    public = public or _public
    running = running or (lambda: _process(_bounded))
    if not public(): return False
    deadline = time.monotonic() + (stretched(WAIT) if wait is None else wait)
    while True:
        if running(): return True
        if time.monotonic() >= deadline: return False
        time.sleep(poll)


def _toggle():
    # Stretched: on a loaded second Mac a fixed 5 s timed out and System Events delivered the key later (#137).
    subprocess.run(["osascript", "-e", 'tell application "System Events" to key code 96 using {command down}'],
                   check=True, capture_output=True, text=True, timeout=stretched(WAIT))


class _Lease:
    def __init__(self, owner, record):
        self.owner = owner
        self.record = record or reservations.Record()
        entry = reservations.require_foreground(owner, self.record)
        self.identity = self._identity(entry)
        self.uncertainty_revision = None
        # One list per open preserved() scope, innermost last: its transitions that failed, which it restores.
        self.scopes = []

    @classmethod
    def adopting(cls, owner, record, entry):
        """A lease for entry's exact identity that adopts the uncertainty at entry's revision as its own, so that
        it may restore over it and nothing else: any later write to the entry ends that permission."""
        lease = cls.__new__(cls)
        lease.owner, lease.record = owner, record
        lease.identity = cls._identity(entry)
        lease.uncertainty_revision = entry["revision"]
        lease.scopes = []
        return lease

    @staticmethod
    def _identity(entry):
        return reservations.identity(entry)

    def check(self, *, restoring=False):
        if restoring:
            entry = self.record.entry(reservations.FOREGROUND)
            own_failure = entry is not None and self.uncertainty_revision is not None \
                and entry.get("revision") == self.uncertainty_revision \
                and entry.get("state") != "restoration-unverified" \
                and (not entry.get("activeRun") or reservations.process_start(entry.get("hostProcessIdentifier"))
                     == entry.get("hostProcessStart"))
            if entry is None or entry.get("ownerChatID") != self.owner \
                    or (reservations.condition(entry) not in ("held", "active") and not own_failure):
                raise reservations.Refused("The original foreground lease no longer permits VoiceOver restoration")
        else:
            entry = reservations.require_foreground(self.owner, self.record)
        identity = self._identity(entry)
        if identity != self.identity:
            raise reservations.Refused("The foreground lease changed before VoiceOver state access")
        return entry

    def uncertain(self, reason):
        # Never write into a subsequent holder's entry, even if a PID/owner string is reused.
        try:
            entry = self.record.entry(reservations.FOREGROUND)
            if entry is None or entry.get("ownerChatID") != self.owner or entry.get("state") == "released": return
            if self._identity(entry) != self.identity: return
            # Only this instance's own uncertainty may permit a restoration retry. Never
            # adopt a marker written by another state scope or any unrelated run failure.
            if reservations.condition(entry) == "uncertain" and entry["revision"] != self.uncertainty_revision: return
            # The marker names the revision it writes, so that recover_restoration can tell that nothing was
            # written to the entry after it.
            changed = self.record.update(reservations.FOREGROUND, owner=self.owner, revision=entry["revision"],
                                         uncertain=True, voiceOverRestoration={"verified": False, "reason": reason,
                                                                               "revision": entry["revision"] + 1})
            self.uncertainty_revision = changed["revision"]
        except (reservations.Refused, OSError, TimeoutError):
            # The enclosing foreground scope also retains an active/orphan entry if its
            # record could not accept the uncertainty marker. Never mask the state failure.
            return


def _lease(owner, record):
    owner = owner or os.environ.get(reservations.OWNER)
    record = record or reservations.Record()
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


def _ready_to_enable(lease, *, restoring=False, timeout=None):
    """Process absence is readiness to enable, never an inference of VoiceOver's on/off state.

    The public getter remains the state authority. Check only this user's exact system
    VoiceOver executable, and recheck the lease around every bounded lifecycle query.
    """
    deadline = time.monotonic() + (stretched(WAIT) if timeout is None else timeout)
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
    # A process that ended after pgrep (None) is not readiness: a fresh pgrep, in the next poll, proves absence.
    return _process(query) is False


def _wait_to_enable(lease, ready, *, restoring=False, wait=None, poll=POLL):
    wait = stretched(QUIT_WAIT) if wait is None else wait
    deadline = time.monotonic() + wait
    while True:
        lease.check(restoring=restoring)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise NotReady(f"VoiceOver is off but has not finished quitting within {wait:g}s; no enable key sent")
        value = ready(lease, restoring=restoring, timeout=min(stretched(WAIT), remaining))
        lease.check(restoring=restoring)
        if type(value) is not bool: raise UnknownState("VoiceOver enable readiness is not a known boolean")
        if value: return
        time.sleep(min(poll, max(0, deadline - time.monotonic())))


def read(owner=None, *, record=None):
    """Known on/off under this owner's exact foreground lease; nothing is changed."""
    return _known(_lease(owner, record), _read)


def _set(lease, desired, reader, toggle, *, restoring=False, wait=None, poll=POLL, ready=None, ready_wait=None):
    """Reach desired with one checked Command-F5. A failure inside a preserved() scope, or while restoring,
    is the scope's to restore; only one with no scope to restore it marks the lease uncertain at once."""
    if type(desired) is not bool: raise ValueError("VoiceOver target must be on or off")
    # The reading its bounds stretch by, taken as the transition begins, goes into its failure as the run's evidence
    # of it (vocaro/queuekeeper#43).
    reading = stretch_reading()
    wait = stretched(WAIT, reading) if wait is None else wait
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
        said = f"{error} ({waiting.stretch_words(reading)})"
        reason = f"VoiceOver transition to {'on' if desired else 'off'} was not verified: {said}"
        if restoring or lease.scopes:
            if not restoring: lease.scopes[-1].append(reason)
            raise TransitionFailed(f"VoiceOver transition was not verified; the original state is restored under "
                                   f"the same lease: {said}") from error
        lease.uncertain(reason)
        raise TransitionFailed(f"VoiceOver transition was not verified; foreground lease retained: {said}") from error


def _steady(lease, desired, reader, *, wait, poll=POLL):
    """Whether desired holds at every read for wait seconds: a key that arrives late shows here."""
    deadline = time.monotonic() + wait
    while True:
        if _known(lease, reader, restoring=True) != desired: return False
        if time.monotonic() >= deadline: return True
        time.sleep(min(poll, max(0, deadline - time.monotonic())))


def _restore(lease, original, reader, toggle, *, settle, attempts=None):
    """Set original again and, after any failed or ambiguous transition, see it hold for a stretched SETTLE.

    A lease that no longer permits restoration refuses at once; other failures try again, at most
    RESTORE_ATTEMPTS times in all, and the last is raised."""
    error = None
    for _ in range(RESTORE_ATTEMPTS if attempts is None else attempts):
        try:
            _set(lease, original, reader, toggle, restoring=True)
            if not settle or _steady(lease, original, reader, wait=stretched(SETTLE)): return
            error = UnknownState(f"VoiceOver left its original {'on' if original else 'off'} after restoration")
        except (TransitionFailed, UnknownState) as failure:
            # A restoration key that failed may also arrive late: watch the next attempt settle.
            error, settle = failure, True
    raise error


def set_enabled(desired, owner=None, *, record=None):
    """Choose a known state inside this process's preserved() scope for failure restoration.
    The outer queuekeeper process is a separate guard; it does not share a child driver's context.
    """
    _set(_lease(owner, record), desired, _read, _toggle)


def recover_restoration(owner=None, *, record=None, by=None, read_state=None, toggle=None):
    """Set VoiceOver back to the original the run's foreground guard recorded, over the uncertainty a session's own
    unverified restoration left, so that the run's next session can start (owner's decision, 2026-11-12,
    vocaro/EPUBLib#416). Returns that original, True for on.

    Only the run that holds the lease may, and only once: it refuses with devices.Refused, changing nothing, unless
    owner holds mac-foreground and devices.unrecoverable_restoration finds nothing against it, which needs the run's
    host process alive, the same lease identity, the original recorded under that lease, and no uncertainty but the
    restoration's. It then restores the original as preserved() does after a failed transition, rechecking the lease
    before every query and key and watching the state hold for a stretched SETTLE, and on success clears the
    uncertainty, recording who recovered it and when. A restoration it cannot verify raises TransitionFailed and
    leaves the entry uncertain, recording the failed attempt, which ends this permission: attended recovery follows.
    """
    owner = owner or os.environ.get(devices.OWNER)
    record = record or devices.Record()
    by = by or f"{owner} through process {os.getpid()}"
    if not owner:
        raise devices.Refused(f"name the session whose {devices.FOREGROUND} reservation covers this work "
                              f"(--owner or {devices.OWNER})")
    entry = record.entry(devices.FOREGROUND)
    if entry is None or entry.get("ownerChatID") != owner or entry.get("state") == "released":
        raise devices.Refused(f"{devices.FOREGROUND} is not held by {owner}; VoiceOver was not changed")
    problem = devices.unrecoverable_restoration(entry)
    if problem:
        raise devices.Refused(f"{devices.FOREGROUND} cannot be recovered by its run: {problem}. It needs attended "
                              f"recovery; VoiceOver was not changed")
    original = entry["voiceOverOriginal"]["enabled"]
    lease = _Lease.adopting(owner, record, entry)
    try:
        with guard.restoring_signals():
            _restore(lease, original, read_state or _read, toggle or _toggle, settle=True)
    except (TransitionFailed, UnknownState, devices.Refused) as error:
        try:
            current = record.entry(devices.FOREGROUND)
            if current is not None and current.get("revision") == entry["revision"]:
                record.update(devices.FOREGROUND, owner=owner, revision=entry["revision"], voiceOverRestoration={
                    **entry["voiceOverRestoration"], "recoveryFailed": {"by": by, "at": devices.now(),
                                                                       "reason": str(error)}})
        except (devices.Refused, OSError, TimeoutError):
            pass
        if isinstance(error, devices.Refused): raise
        raise TransitionFailed(f"VoiceOver's original {'on' if original else 'off'} was not restored and verified; "
                               f"the foreground stays uncertain for attended recovery: {error}") from error
    record.restoration_recovered(devices.FOREGROUND, owner=owner, revision=entry["revision"], by=by)
    return original


@contextlib.contextmanager
def preserved(owner=None, *, target=None, record=None, read_state=None, toggle=None, said=None, settle=None):
    """Capture after acquisition; optionally choose target; restore before lease release.

    target=None preserves existing clients' initial on/off behavior. target=False is the
    explicit quiet-test opt-in. Dependencies are injectable for offline contract tests.
    settle=None watches the restored state hold for a stretched SETTLE only after this scope's own failed
    transition; settle=True always does, as queuekeeper's guard around a child driver must: a child killed between its
    failed toggle and its own restoration tells the parent nothing, and its late key may still arrive.
    An initial read failure acted on nothing and may release cleanly. A failed transition,
    unreadable final state or lost ownership retains uncertainty for attended recovery.
    Existing queuekeeper/driver signal handlers keep ownership; otherwise stop_signals handles stops.
    """
    if target is not None and type(target) is not bool: raise ValueError("VoiceOver target must be on or off")
    lease = _lease(owner, record)
    reader, toggle = read_state or _read, toggle or _toggle
    said = said or (lambda text: None)
    # Nothing has been changed if this strict initial read fails.
    original = _known(lease, reader)
    token = _CURRENT.set(lease)
    failures, failure = [], None
    lease.scopes.append(failures)
    try:
        with guard.stop_signals(only_unhandled=True):
            try:
                said(f"VoiceOver original {'on' if original else 'off'}")
                if target is not None: _set(lease, target, reader, toggle)
                yield original
            except TransitionFailed as error:
                failure = error
                raise
            finally:
                # The body and its supervised children have ended. A first stop arriving during
                # bounded restoration is delivered only after the original state is verified.
                with guard.restoring_signals():
                    try:
                        _restore(lease, original, reader, toggle,
                                 settle=bool(failures) if settle is None else settle)
                        said(f"VoiceOver restored to original {'on' if original else 'off'}")
                    except BaseException as error:
                        lease.uncertain(f"VoiceOver original state restoration was not verified: {error}")
                        raise TransitionFailed(f"VoiceOver original state was not restored; foreground lease retained: {error}") from error
                # Verified under the same lease, and nothing marked it uncertain: the failed transition is the
                # run's failure, not the lease's, and the lease releases.
                if failure is not None and lease.uncertainty_revision is None: failure.restored = True
    finally:
        # By identity: an outer scope's list can be equal to this one.
        lease.scopes[:] = [scope for scope in lease.scopes if scope is not failures]
        _CURRENT.reset(token)
