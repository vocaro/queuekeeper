"""Time each probe process's exit: poll kill(pid, 0) every 5 ms for the PIDs siglog.py logs.

usage: watch.py LOG OUT SECONDS
Watches each logged process and the parent of each "child", which is the command's shell.
How to run it beside siglog.py, and read both: doc/guides/shared-host.md, Probing stop signals.
"""
import os
import sys
import time

log_path, out_path, seconds = sys.argv[1], sys.argv[2], float(sys.argv[3])


def write(text):
    with open(out_path, "a") as stream:
        stream.write(f"{time.time():.3f} {text}\n")


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


watched, gone, offset = [], set(), 0
end = time.time() + seconds
while time.time() < end:
    try:
        with open(log_path) as stream:
            stream.seek(offset)
            lines = stream.read()
            offset = stream.tell()
    except FileNotFoundError:
        lines = ""
    added = []
    for line in lines.splitlines():
        fields = line.split()
        if len(fields) > 3 and fields[3] == "started":
            pids = [int(fields[2])]
            if fields[1] == "child":
                pids.insert(0, int(fields[fields.index("ppid") + 1]))
            added += [pid for pid in pids if pid not in watched]
    if added:
        watched += added
        write(f"watching {added}")
    for pid in watched:
        if pid not in gone and not alive(pid):
            gone.add(pid)
            write(f"gone {pid}")
    if watched and len(gone) == len(watched):
        break
    time.sleep(0.005)
write(f"done, alive {[pid for pid in watched if pid not in gone]}")
