"""A live view of the queue in the terminal: `qk watch`.

It shows what the queue is doing in one line, then each running gate, the entries waiting, the last few settled
and the journal's last events, and draws it again every few seconds until interrupted. Where its output is not a
terminal, or with --once, it prints the view once. It changes nothing: it reads the queue's snapshot and journal,
as every visibility feature does.
"""
import argparse
import contextlib
from pathlib import Path
import shutil
import sys
import time

from queuekeeper import events, plugins, merge_queue

HELP = "show the queue's state in the terminal, drawn again as it changes"
CLEAR = "\x1b[H\x1b[2J"


def register():
    plugins.COMMANDS["watch"] = (HELP, main)


def span(seconds):
    """A duration in its two largest units: 42s, 6m 12s, 2h 5m, 3d 4h."""
    seconds = max(0, int(seconds))
    for unit, size, below, part in (("d", 86400, "h", 3600), ("h", 3600, "m", 60), ("m", 60, "s", 1)):
        if seconds >= size: return f"{seconds // size}{unit} {seconds % size // part}{below}"
    return f"{seconds}s"


def count(number, noun, plural=None):
    return f"{number} {noun if number == 1 else plural or noun + 's'}"


def headline(state):
    """What the queue is doing, in one line: a hold first, since nothing moves until it is cleared."""
    waiting = len(state.get("waiting") or [])
    if state.get("held"): return f"Held {state['held']}"
    current = state.get("round") or {}
    if state.get("worker") and current.get("round"):
        testing = len(current.get("entries") or [])
        behind = waiting - testing
        return f"Testing {count(testing, 'entry', 'entries')}" + (f", {behind} more waiting" if behind > 0 else "")
    if state.get("worker"): return f"Worker running, {waiting} waiting"
    if waiting: return f"{waiting} waiting with no worker: `qk queue resume` starts one"
    return "Idle: nothing is waiting"


def gate_lines(state, now):
    lines = []
    for role, gate in sorted(((state.get("round") or {}).get("gates") or {}).items()):
        began = gate.get("began")
        lines.append(f"  {role}: {gate.get('lane')} lane on {gate.get('mac') or 'no Mac yet'}"
                     + (f", {span(now - began)}" if isinstance(began, (int, float)) else ""))
        if gate.get("waiting"):
            since = gate.get("waitingSince")
            lines.append(f"    waiting: {gate['waiting']}" + (f", for {span(now - since)}" if since else ""))
        for record in gate.get("progress") or []:
            if isinstance(record, dict) and record.get("phase"): lines.append(f"    phase: {record['phase']}")
        if gate.get("stuck"): lines.append(f"    {gate['stuck']}")
    return lines


def entry_line(entry, now):
    line = f"  {entry.get('state', '?'):8} {entry.get('id')}  {entry.get('subject') or ''}"
    if entry.get("landedAs"): line += f" -> {entry['landedAs'][:12]}"
    if entry.get("settled"): return f"{line}  ({span(now - entry['settled'])} ago)"
    return f"{line}  ({span(now - entry['submitted'])})" if entry.get("submitted") else line


def frame(name, state, recent_events, now=None, width=None):
    """The view's lines for a queue named name: state is its snapshot (merge_queue.snapshot) and recent_events
    the journal's last events, oldest first. A line longer than width is cut to it."""
    now = time.time() if now is None else now
    lines = [f"{name}  {time.strftime('%H:%M:%S', time.localtime(now))}", headline(state)]
    gates = gate_lines(state, now)
    if gates: lines += ["", "Gates", *gates]
    if state.get("waiting"): lines += ["", "Queue", *(entry_line(entry, now) for entry in state["waiting"])]
    if state.get("ungated"): lines += ["", f"Ungated on main until the next gate covers them: {', '.join(state['ungated'])}"]
    if state.get("recent"): lines += ["", "Recent", *(entry_line(entry, now) for entry in reversed(state["recent"]))]
    if recent_events:
        lines += ["", "Events", *(f"  {events.words(event)[11:]}" for event in reversed(recent_events))]
    if width: lines = [line if len(line) <= width else line[:max(0, width - 1)] + "…" for line in lines]
    return lines


class Journal:
    """The journal's last events, read again only when its file has changed."""

    def __init__(self, directory, most):
        self.directory, self.most, self.seen, self.last = directory, most, None, []

    def read(self):
        if not self.most: return []
        try: stat = events.path_of(self.directory).stat()
        except OSError: return self.last
        mark = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
        if mark != self.seen: self.seen, self.last = mark, events.read(self.directory)[-self.most:]
        return self.last


def main(argv, project=None):
    parser = argparse.ArgumentParser(prog="qk watch", description=HELP)
    parser.add_argument("--once", action="store_true", help="print the view once and exit, as it does by itself "
                        "where its output is not a terminal")
    parser.add_argument("--interval", type=float, default=2.0, metavar="SECONDS", help="how often it is drawn again "
                        "(default 2)")
    parser.add_argument("--events", type=int, default=8, metavar="N", help="how many of the journal's last events it "
                        "shows (default 8; 0 for none)")
    args = parser.parse_args(argv)
    root = Path(project or ".").resolve()
    try: queue = merge_queue.Queue(root)
    except (OSError, ValueError, RuntimeError) as error:
        print(f"queuekeeper: {root} has no queue to watch: {error}", file=sys.stderr)
        return 2
    journal = Journal(queue.directory, max(0, args.events))
    live = sys.stdout.isatty() and not args.once
    with contextlib.suppress(KeyboardInterrupt):
        while True:
            size = shutil.get_terminal_size() if live else None
            lines = frame(queue.main_checkout.name, merge_queue.snapshot(root), journal.read(),
                          width=size.columns if size else None)
            if size: lines = lines[:max(1, size.lines - 1)]
            print((CLEAR if live else "") + "\n".join(lines), flush=True)
            if not live: break
            time.sleep(max(0.2, args.interval))
    return 0
