"""Unity's per-user licensing helper, which an editor can leave alive and unanswering.

Unity 6000.5 and later can leave the editor's licensing helper running after the last editor receives SIGTERM. The
helper keeps the well-known named pipe but no longer answers it: the next editor waits 60 seconds, starts a second
helper, and reports that licensing did not initialize. `repair` stops only that orphan. Unity Hub uses another
helper with a random pipe, which is never matched, and while any editor or batch run is alive the helper may still
be in use, so nothing is done."""
import os
import signal
import time

from . import project

HELPER = ("Unity.app/Contents/Helpers/UnityLicensingClient.app/Contents/MacOS/Unity.Licensing.Client"
          ".*--namedPipe Unity-LicenseClient-")
# What a log says when licensing did not start. The first is also logged by runs that go on to finish, so neither
# alone says a run never started: the caller asks only once the run left no results.
NEVER_STARTED = ("Found 0 entitlement groups", "attempt to write a readonly database")


class Unrepaired(RuntimeError):
    """An orphaned helper is still there, or could not be looked for. No editor should be started."""


def alive(pid):
    try: os.kill(pid, 0)
    except ProcessLookupError: return False
    except PermissionError: return True
    return True


def repair(say=print, wait=10, pause=time.sleep):
    """Stop the editor's licensing helper when no editor is left to use it, and return the PIDs stopped. The helper
    is asked with SIGTERM and never force-killed: one that stays raises Unrepaired."""
    try:
        if project.editors(): return []
        helpers = [pid for pid, _ in project.pgrep(HELPER)]
    except project.Unreadable as error:
        raise Unrepaired(f"{error}; no licensing repair attempted") from None
    for pid in helpers:
        say(f"license-client: stopping orphaned editor helper pid={pid} (SIGTERM)")
        try: os.kill(pid, signal.SIGTERM)
        except ProcessLookupError: pass
        except OSError as error:
            raise Unrepaired(f"could not stop the orphaned licensing helper pid={pid} ({error})") from None
    for _ in range(wait):
        if not any(alive(pid) for pid in helpers): break
        pause(1)
    else:
        if any(alive(pid) for pid in helpers):
            raise Unrepaired("the orphaned licensing helper did not exit after SIGTERM, and is not force-killed")
    if helpers: say("license-client: stale helper cleared; the next Unity launch starts a fresh one")
    return helpers


def never_started(log):
    """Whether a run's log says licensing did not initialize."""
    return any(sign in log for sign in NEVER_STARTED)
