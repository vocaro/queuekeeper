"""Start a command that outlives whatever stops its caller, and print its PID.

usage: detach.py COMMAND [ARGUMENT ...]
The command runs in a session of its own, with its input and output on /dev/null, and this
process exits at once, so launchd becomes its parent. A stop that signals the caller's process
group, its session or the descendants a process scan finds from the caller's shell reaches none
of it, and it holds no pipe of the caller's. Run watch.py this way to see what ends a probe:
doc/guides/process-supervision.md, Probing stop signals.
"""
import subprocess
import sys

if len(sys.argv) < 2:
    sys.exit(__doc__.strip())
process = subprocess.Popen(sys.argv[1:], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, start_new_session=True)
print(process.pid)
