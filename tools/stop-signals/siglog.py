"""Log the stop signals this process receives, and spawn a child in its own session that does too.

usage: siglog.py LOG ROLE [SECONDS]
It logs each SIGTERM, SIGINT and SIGHUP it receives and runs on; as ROLE child it also starts a child that
leads a session of its own and does the same. Each writes `exiting` after SECONDS (20 by default). Any other
signal, SIGKILL among them, ends a process without a line.

To probe a stop, start the watcher detached, where the action under test cannot reach it, have the agent or
tool under test run this probe as its command, and read the report once the action is done:

    K=~/Development/queuekeeper/tools/stop-signals D=/private/tmp/stop-probe/NAME
    mkdir -p "$D" && python3 -I "$K/detach.py" python3 -I "$K/watch.py" "$D/probe.log" "$D/probe.watch" 1800
    python3 -I "$K/siglog.py" "$D/probe.log" child 300 && true    # run by the command under test
    python3 -I "$K/report.py" "$D"

`&& true` keeps a shell given the line with -c as the probe's parent; zsh otherwise replaces itself with a
command that ends its string.
"""
import os
import signal
import sys
import time

log_path, role = sys.argv[1], sys.argv[2]
seconds = float(sys.argv[3]) if len(sys.argv) > 3 else 20


def write(text):
    with open(log_path, "a") as stream:
        stream.write(f"{time.time():.3f} {role} {os.getpid()} {text}\n")


def handler(number, _frame):
    write(f"got {signal.Signals(number).name}")


for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
    signal.signal(number, handler)
write(f"started pgid {os.getpgrp()} sid {os.getsid(0)} ppid {os.getppid()}")
if role == "child" and os.fork() == 0:
    os.setsid()
    os.execv(sys.executable, [sys.executable, "-I", __file__, log_path, "session-child", str(seconds)])
end = time.time() + seconds
while time.time() < end:
    time.sleep(0.05)
write(f"exiting after {seconds:g}s")
