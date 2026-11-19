"""Read what siglog.py and watch.py recorded: each probe's timeline and what ended each process.

usage: report.py DIR
Reads every NAME.log in DIR with the NAME.watch beside it, as doc/guides/process-supervision.md, Probing stop signals
starts them. Times are seconds from the first signal any probe in DIR logged, or from the first start
when none logged one. siglog.py logs SIGTERM, SIGINT and SIGHUP and keeps running; any other signal,
SIGKILL among them, ends it without a line, so a process the watcher saw go without an "exiting" line
was killed.
"""
import pathlib
import sys

ORDER = {"parent": 0, "child": 1, "session-child": 2}


def read(path):
    try:
        text = path.read_text()
    except FileNotFoundError:
        return []
    rows = []
    for line in text.splitlines():
        stamp, _, rest = line.partition(" ")
        try:
            rows.append((float(stamp), rest))
        except ValueError:
            pass
    return rows


def processes(log):
    """Each process the log names, and the child's parent (the command's shell), in that order."""
    found = {}
    for when, rest in log:
        fields = rest.split()
        if len(fields) < 3 or not fields[1].isdigit():
            continue
        role, pid, event = fields[0], int(fields[1]), fields[2:]
        entry = found.setdefault(pid, {"role": role, "signals": [], "exited": None})
        if event[0] == "started" and role == "child" and "ppid" in event[:-1]:
            parent = int(event[event.index("ppid") + 1])
            found.setdefault(parent, {"role": "parent", "signals": [], "exited": None})
        elif event[0] == "got" and len(event) > 1:
            entry["signals"].append((when, event[1]))
        elif event[0] == "exiting":
            entry["exited"] = when
    return sorted(found.items(), key=lambda item: ORDER.get(item[1]["role"], 3))


def watched(watch):
    """When the watcher saw each PID go, and, once it ended, which PIDs were still alive."""
    gone, alive = {}, None
    for when, rest in watch:
        if rest.startswith("gone "):
            gone[int(rest.split()[1])] = when
        elif rest.startswith("done, alive"):
            alive = {int(pid) for pid in rest.partition("[")[2].rstrip("]").split(",") if pid.strip()}
    return gone, alive


def verdict(pid, entry, gone, alive, at):
    signals = ", ".join(f"{name} at {at(when)}" for when, name in entry["signals"])
    if entry["exited"] is not None:
        return f"ran to its own end at {at(entry['exited'])}, " + (f"after {signals}" if signals else
                                                                    "no signal logged")
    if pid in gone:
        if entry["role"] == "parent":
            return f"gone at {at(gone[pid])}"
        if signals:
            first = entry["signals"][0][0]
            return (f"got {signals}; gone {gone[pid] - first:.3f} s after the first, without exiting: "
                    "killed (SIGKILL, or a signal siglog.py does not log)")
        return (f"gone at {at(gone[pid])}, no signal logged, without exiting: SIGKILL alone (or a signal "
                "siglog.py does not log)")
    if alive is not None and pid in alive:
        return "still running when the watcher ended" + (f", after {signals}" if signals else "")
    return "not seen to end: its watcher is still running or never saw it" + (f"; got {signals}" if signals
                                                                              else "")


def main(argv):
    if len(argv) != 1:
        sys.exit(__doc__.strip())
    directory = pathlib.Path(argv[0])
    probes = [(path, read(path), read(path.with_suffix(".watch"))) for path in sorted(directory.glob("*.log"))]
    if not probes:
        sys.exit(f"no NAME.log in {directory}")
    signals = [when for _, log, _ in probes for _, entry in processes(log) for when, _ in entry["signals"]]
    starts = [when for _, log, _ in probes for when, _ in log]
    zero = min(signals or starts or [0.0])
    print(f"zero is {zero:.3f}, the first {'signal' if signals else 'start'} logged")

    def at(when):
        return f"{when - zero:+.3f}"

    for path, log, watch in probes:
        source = path.with_suffix(".watch")
        print(f"\n== {path.stem}: {path}, {source}{'' if source.exists() else ' (missing)'}")
        for when, rest in sorted(log + [(when, f"watch {rest}") for when, rest in watch], key=lambda row: row[0]):
            print(f"{when:.3f} {at(when):>9} {rest}")
        gone, alive = watched(watch)
        for pid, entry in processes(log):
            print(f"{entry['role']} {pid}: {verdict(pid, entry, gone, alive, at)}")


if __name__ == "__main__":
    main(sys.argv[1:])
