"""Who has a Unity project open, and its lock.

A Unity project admits one editor at a time. The editor holds Temp/UnityLockfile open for as long as it has the
project open, and that open file, not the file's existence, is what keeps a second editor off the project: an editor
started over a leftover file opens normally. Unity leaves the file on disk after a clean SIGTERM, so a closed editor
routinely leaves one behind.

Editors are matched by the absolute project path they were started with, so that a sibling worktree's editor, a
separate Unity project, is never taken for this one's. The lock is judged by who holds the file open, which a
project path spelled another way (a symlink, a relative path) cannot hide."""
from pathlib import Path
import subprocess

from . import editor

LOCK = "Temp/UnityLockfile"
CLEARED, ABSENT, KEPT = "cleared", "absent", "kept"


class Unreadable(RuntimeError):
    """This Mac's processes could not be listed, which is not the same as none running: a sandbox without host
    access answers so."""


class Held(RuntimeError):
    """A process holds the project's lock open."""

    def __init__(self, lock, pids):
        super().__init__(f"{lock} is held open by process {', '.join(map(str, pids))}")
        self.pids = pids


def pgrep(pattern):
    """Each running process whose command line matches pattern, as (PID, command line)."""
    ran = subprocess.run(["pgrep", "-fil", pattern], capture_output=True, text=True)
    if ran.returncode > 1: raise Unreadable(f"cannot inspect processes (pgrep exit {ran.returncode})")
    found = []
    for line in ran.stdout.splitlines():
        pid, _, command = line.strip().partition(" ")
        if pid.isdigit(): found.append((int(pid), command))
    return found


def editors():
    """Every Unity editor running on this Mac, interactive or batch, as (PID, command line)."""
    return pgrep(editor.BINARY)


def on(project, found):
    """Those of found that were started on project."""
    return [(pid, command) for pid, command in found if str(project) in command]


def interactive(found):
    """Those of found that are an editor with a window, not a batch run or one of its workers."""
    return [(pid, command) for pid, command in found if "-batchmode" not in command.lower()]


def clear_stale_lock(project, say=print):
    """Remove project's lock file when no process holds it open. CLEARED once it is removed, ABSENT when there was
    none, and KEPT, after saying why, when nothing proved it stale: its path uses a symlink, or lsof could not
    answer. Held is raised when a process holds it, and nothing is removed."""
    project = Path(project)
    lock = project / LOCK
    if project.is_symlink() or lock.parent.is_symlink() or lock.is_symlink():
        say(f"WARNING: leaving {lock} in place because its path uses a symlink")
        return KEPT
    if not lock.exists(): return ABSENT
    if not lock.is_file():
        say(f"WARNING: {lock} is not a regular file")
        return KEPT
    try: ran = subprocess.run(["lsof", "-w", "-t", "--", str(lock)], capture_output=True, text=True)
    except OSError as error:
        say(f"WARNING: could not ask lsof who holds {lock} ({error}); leaving it in place")
        return KEPT
    holders = [int(line) for line in ran.stdout.split() if line.isdigit()]
    if holders: raise Held(lock, holders)
    # lsof exits 1 for "no holders" and for a failure alike; only silence beside it says nobody holds the file.
    said = (ran.stdout + ran.stderr).strip()
    if ran.returncode != 1 or said:
        first = f": {said.splitlines()[0]}" if said else ""
        say(f"WARNING: could not ask lsof who holds {lock} (exit {ran.returncode}{first}); leaving it in place")
        return KEPT
    try: lock.unlink()
    except OSError as error:
        say(f"WARNING: could not remove {lock} ({error})")
        return KEPT
    say(f"cleared stale {lock} (no process holds it open)")
    return CLEARED
