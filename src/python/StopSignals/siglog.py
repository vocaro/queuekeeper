"""Log the stop signals this process receives, and spawn a child in its own session that does too.

usage: siglog.py LOG ROLE [SECONDS]
How to run it beside watch.py, and read both: guides/shared-host.md, Probing stop signals.
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
