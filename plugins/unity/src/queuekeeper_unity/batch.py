"""A Unity batch run that is ended when it goes silent.

Unity in batch mode can stall for good: licensing that never initializes, a prompt nobody can answer, a deadlock.
A stuck run keeps the project's lock, and every later run on the project refuses until someone ends it. The guard
is on the log's silence, not the run's length: honest runs take from seconds to many minutes, and one that has
written nothing for ten minutes is stuck."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from . import project as projects

# The seconds a run's log may stay unwritten.
QUIET = 600
VARIABLE = "QUEUEKEEPER_UNITY_QUIET"
# run's status for a run it ended, as timeout(1) gives.
HUNG = 124


def quiet(environ=None):
    """The seconds of silence allowed, which QUEUEKEEPER_UNITY_QUIET sets for a project whose runs differ."""
    named = (os.environ if environ is None else environ).get(VARIABLE, "")
    return int(named) if named.isdigit() and int(named) else QUIET


def say(line):
    print(line, file=sys.stderr, flush=True)


def run(command, log, project, *, quiet=QUIET, poll=5, grace=5, say=say):
    """command's exit status, or HUNG once it was ended for leaving log unwritten for quiet seconds. command is a
    Unity batch run on project that writes log (-logFile). Its own output is dropped, as the log holds it. A run
    that is ended takes its import workers with it, which are this project's alone, and the lock they leave."""
    log = Path(log)
    log.write_bytes(b"")
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    while True:
        try: return process.wait(poll)
        except subprocess.TimeoutExpired: pass
        try: age = time.time() - log.stat().st_mtime
        except OSError: age = 0
        if age <= quiet: continue
        say(f"TIMEOUT: Unity wrote nothing for {int(age)}s; treating it as hung and ending it")
        process.terminate()
        try: process.wait(grace)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        # Import workers outlive the editor that started them.
        try: left = projects.on(project, projects.editors())
        except projects.Unreadable: left = []
        for pid, _ in left:
            try: os.kill(pid, signal.SIGTERM)
            except OSError: pass
        try: projects.clear_stale_lock(project, say=say)
        except projects.Held as held: say(f"WARNING: {held}")
        return HUNG
