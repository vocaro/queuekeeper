"""A long run's progress, kept in files whatever its standard output is, and `qk progress`, which reads it from
another shell (vocaro/queuekeeper#22).

A run of `qk unittest`, and any runner a bare command admits first (cli.admitted_runner), keeps a folder of its own
under RUNS below the directory it started in, and prints that folder's path as its first line, before any work
starts. Output piped through `tail` shows nothing until the run ends, and unittest's dots name no test, so the
two-hour run of test_remote that #22 reported on 2026-11-12, on a Mac at a load of 700, showed no sign that it moved
but new processes in `ps`. The folder holds:

- LOG, `qk unittest`'s own: a line as each of its processes finishes, with the count of all of them and of those
  that failed, then that process's whole output;
- STATE, `qk unittest`'s own: the same as JSON, with the processes running now and when each started;
- OUTPUT, an admitted runner's: everything it printed, as it printed it, which on the second Mac's run is all this Mac
  sees of it;
- RECORD, an admitted runner's: its command, its process and how it ended.
"""
import argparse
import contextlib
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
import shutil
import socket
import stat
import sys
import threading
import time

RUNS = ".build/queuekeeper/runs"
# Names the folder an admitted runner made to the run it admits on this Mac, so that both keep their files in one.
VARIABLE = "QUEUEKEEPER_PROGRESS_DIR"
LOG, STATE, OUTPUT, RECORD = "progress.log", "progress.json", "output.log", "run.json"
# Ended runs' folders kept, the newest; a running one's is never removed.
KEPT = 20


def stamp(now=None):
    return datetime.fromtimestamp(time.time() if now is None else now).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def new_folder(base=None, now=None):
    """A new folder of its own under base's RUNS (the current directory's), named for now and this process; ended
    runs' folders beyond the newest KEPT are removed first."""
    runs = Path(base or Path.cwd()) / RUNS
    runs.mkdir(parents=True, exist_ok=True)
    for old in sorted(path for path in runs.iterdir() if path.is_dir())[:-KEPT]:
        if not running(old): shutil.rmtree(old, ignore_errors=True)
    now = now or datetime.now(timezone.utc)
    folder = runs / f"{now.strftime('%Y%m%dT%H%M%SZ')}-{os.getpid()}"
    folder.mkdir()
    return folder


def piped(descriptor=1):
    """Whether descriptor is a pipe, as it is under `| tail`."""
    try: return stat.S_ISFIFO(os.fstat(descriptor).st_mode)
    except OSError: return False


# What begins each line a run prints of its progress folder: not `queuekeeper unittest: `, which only its summary and
# its refusals begin, as the merge queue and the tests read them.
PREFIX = "progress: "


def announce(folder):
    """Print where folder keeps the run's progress, first on standard output and, when that is a pipe, which may
    hold it back until the run ends, on standard error too."""
    line = f"{PREFIX}{folder} keeps this run's progress; qk progress reads it from another shell"
    print(line, flush=True)
    if piped(): print(line, file=sys.stderr, flush=True)


def read(path):
    try: found = json.loads(Path(path).read_text())
    except (OSError, ValueError): return None
    return found if isinstance(found, dict) else None


def write(path, data):
    """Write data as JSON to path whole, so that a reader never sees half of it."""
    path = Path(path)
    partial = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}")
    partial.write_text(json.dumps(data, indent=2) + "\n")
    partial.replace(path)


def alive(pid):
    if not isinstance(pid, int) or pid <= 0: return False
    try: os.kill(pid, 0)
    except ProcessLookupError: return False
    except PermissionError: return True
    return True


def running(folder):
    """Whether folder's run has not ended and its process lives: the outer, admitted runner's when it has one."""
    for name in (RECORD, STATE):
        found = read(Path(folder) / name)
        if found is not None:
            return found.get("ended") is None and found.get("host") in (None, socket.gethostname()) and alive(found.get("pid"))
    return False


def started_record(command):
    return {"pid": os.getpid(), "host": socket.gethostname(), "command": command, "cwd": os.getcwd(),
            "started": time.time(), "ended": None, "status": None}


class Tracker:
    """`qk unittest`'s progress in folder: LOG and STATE, kept as each process starts and ends. Thread-safe."""

    def __init__(self, folder, command):
        self.folder, self.lock, self.summary = Path(folder), threading.Lock(), None
        self.state = {**started_record(command), "phase": "discovering", "updated": time.time(), "processes": None,
                      "tests": None, "jobs": None, "finished": 0, "ran": 0, "failed": [], "unmeasured": [],
                      "running": {}, "last": None}
        with self.lock:
            with (self.folder / LOG).open("a") as log:
                print(f"[{stamp()}] {shlex.join(command)}, process {os.getpid()} in {os.getcwd()}", file=log)
            write(self.folder / STATE, self.state)

    def planned(self, processes, tests, jobs):
        with self.lock:
            self.state.update(phase="running", processes=processes, tests=tests, jobs=jobs, updated=time.time())
            with (self.folder / LOG).open("a") as log:
                print(f"[{stamp()}] {tests} tests in {processes} processes, {jobs} at a time", file=log)
            write(self.folder / STATE, self.state)

    def started(self, name):
        with self.lock:
            self.state["running"][name] = time.time()
            write(self.folder / STATE, self.state)

    def finished(self, name, result):
        """Record the end of name's process: result is run_shard's, or None when a stop came before it started."""
        with self.lock:
            now = time.time()
            self.state["running"].pop(name, None)
            if result is None:
                write(self.folder / STATE, self.state)
                return
            state = self.state
            state["finished"] += 1
            state["ran"] += result["ran"]
            if result["status"] is None: state["unmeasured"].append(name)
            elif result["status"] != 0: state["failed"].append(name)
            state.update(updated=now, last={"name": name, "result": result["result"], "at": now})
            line = (f"[{stamp(now)}] {state['finished']} of {state['processes']} processes, {state['ran']} tests, "
                    f"{len(state['failed'])} failed, {len(state['unmeasured'])} unmeasured, {len(state['running'])} "
                    f"running: {name}: {result['result']} ({result['ran']} tests, {result['seconds']:.1f}s)")
            with (self.folder / LOG).open("a") as log:
                print(line, file=log)
                print(result["output"].rstrip("\n"), file=log)
            write(self.folder / STATE, state)

    def end(self, status, summary=None):
        with self.lock:
            now = time.time()
            self.state.update(phase="ended", ended=now, status=status, updated=now, running={})
            with (self.folder / LOG).open("a") as log:
                print(f"[{stamp(now)}] {summary or 'ended'}; status {status}", file=log)
            write(self.folder / STATE, self.state)


@contextlib.contextmanager
def teed(path):
    """Copy everything written to standard output's descriptor, by this process and the processes it starts, to path
    as well, as it is written. A reader that goes away, as `head` does, leaves the copy in path whole."""
    sys.stdout.flush()
    try: kept = os.dup(1)
    except OSError:
        yield
        return
    reader, writer = os.pipe()
    os.dup2(writer, 1)
    os.close(writer)

    def pump():
        shown = True
        with open(path, "ab", buffering=0) as log:
            while chunk := os.read(reader, 65536):
                log.write(chunk)
                while shown and chunk:
                    try: chunk = chunk[os.write(kept, chunk):]
                    except OSError: shown = False
        os.close(reader)
        os.close(kept)

    thread = threading.Thread(target=pump, name="progress tee", daemon=True)
    thread.start()
    try: yield
    finally:
        sys.stdout.flush()
        # Standard output is kept's again; a process this one started that still holds the pipe keeps the copy going.
        os.dup2(kept, 1)
        thread.join(timeout=5)


@contextlib.contextmanager
def admitted(command, environment=os.environ):
    """An admitted runner's folder (cli.admitted_runner): announced, with its OUTPUT teed and its RECORD kept, and
    named to the run inside it in VARIABLE. It yields a dict whose "status" the caller sets to the run's."""
    outcome = {"status": None}
    try: folder = new_folder()
    except OSError as error:
        print(f"{PREFIX}no folder: {error}", file=sys.stderr, flush=True)
        yield outcome
        return
    announce(folder)
    outcome["output"] = folder / OUTPUT
    record = started_record(command)
    write(folder / RECORD, record)
    previous = environment.get(VARIABLE)
    environment[VARIABLE] = str(folder)
    try:
        with teed(folder / OUTPUT): yield outcome
    finally:
        if previous is None: environment.pop(VARIABLE, None)
        else: environment[VARIABLE] = previous
        with contextlib.suppress(OSError):
            write(folder / RECORD, {**record, "ended": time.time(), "status": outcome["status"]})


def tracker(command):
    """`qk unittest`'s Tracker: in the folder an admitted runner named in VARIABLE, which it takes out of the
    environment its tests inherit, else in a new folder of its own, which it announces. None when neither can be
    written, which costs the run nothing but its progress files."""
    named = os.environ.pop(VARIABLE, None)
    try:
        folder = Path(named) if named and Path(named).is_dir() else new_folder()
        found = Tracker(folder, command)
    except OSError as error:
        print(f"{PREFIX}no folder: {error}", file=sys.stderr, flush=True)
        return None
    if not named or folder != Path(named): announce(folder)
    return found


def span(seconds):
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    return f"{hours}h{rest // 60:02d}m" if hours else f"{rest // 60}m{rest % 60:02d}s" if seconds >= 60 else f"{seconds}s"


def describe(folder, now=None):
    """The lines `qk progress` prints for folder's run."""
    now = time.time() if now is None else now
    folder = Path(folder)
    outer, inner = read(folder / RECORD), read(folder / STATE)
    head = outer or inner or {}
    lines = [str(folder)]
    if head.get("command"): lines.append(f"  command: {shlex.join(head['command'])}")
    begun = head.get("started")
    if head.get("ended") is not None:
        lines.append(f"  ended {stamp(head['ended'])} with status {head.get('status')}, after "
                     f"{span(head['ended'] - begun) if begun else 'an unknown time'}")
    elif running(folder):
        lines.append(f"  running for {span(now - begun) if begun else 'an unknown time'}, process {head.get('pid')}")
    else:
        lines.append(f"  not running: process {head.get('pid')} on {head.get('host')} ended without a status, or "
                     "runs on another Mac")
    if inner and inner.get("processes") is not None:
        lines.append(f"  {inner['finished']} of {inner['processes']} processes, {inner['ran']} of {inner['tests']} "
                     f"tests, {len(inner['failed'])} failed, {len(inner['unmeasured'])} unmeasured, {inner['jobs']} "
                     "at a time")
        if inner["failed"]: lines.append(f"  failed: {', '.join(inner['failed'])}")
        for name, since in sorted(inner.get("running", {}).items(), key=lambda item: item[1]):
            lines.append(f"  running: {name}, for {span(now - since)}")
    elif inner and inner.get("ended") is None:
        lines.append("  discovering tests")
    moved = inner.get("updated") if inner else None
    output = folder / OUTPUT
    if output.exists():
        try: moved = max(moved or 0, output.stat().st_mtime)
        except OSError: pass
        if not inner or inner.get("processes") is None:
            # A run on the second Mac: its finished processes are the headings in its output here.
            lines.extend(f"  {line}" for line in output_facts(output))
    if moved: lines.append(f"  last moved {span(now - moved)} ago, at {stamp(moved)}")
    last = (inner or {}).get("last")
    if last: lines.append(f"  last finished: {last['name']}: {last['result']}")
    return lines


def output_facts(output):
    """What an admitted runner's OUTPUT says of its progress: the processes `qk unittest` planned and those that
    finished, and its last line."""
    try: text = output.read_text(errors="replace")
    except OSError: return []
    planned = next((line for line in reversed(text.splitlines()) if line.startswith(PREFIX) and line.endswith(" at a time")),
                   None)
    finished = sum(1 for line in text.splitlines() if line.startswith("== "))
    last = next((line for line in reversed(text.splitlines()) if line.strip()), None)
    facts = [planned.removeprefix(PREFIX)] if planned else []
    if planned or finished: facts.append(f"{finished} processes finished")
    if last: facts.append(f"last line: {last[:200]}")
    return facts


def main(argv, project=None):
    parser = argparse.ArgumentParser(prog="qk progress", description="Say how far each long run started from this "
        "directory, or the project's root, has got: `qk unittest` and any runner admitted for a bare command. "
        "With no RUN, each run still running, or the newest when none is.")
    parser.add_argument("--all", action="store_true", help="every kept run, newest last")
    parser.add_argument("runs", nargs="*", metavar="RUN", help="a run's folder, as its first line named it")
    args = parser.parse_args(argv)
    if args.runs:
        folders = [Path(run) for run in args.runs]
        missing = [str(folder) for folder in folders if not folder.is_dir()]
        if missing:
            print(f"qk progress: no run at {', '.join(missing)}", file=sys.stderr)
            return 1
    else:
        bases = dict.fromkeys(path.resolve() for path in (Path.cwd(), Path(project or Path.cwd())))
        folders = sorted((folder for base in bases if (base / RUNS).is_dir() for folder in (base / RUNS).iterdir()
                          if folder.is_dir()), key=lambda folder: folder.name)
        if not folders:
            print(f"qk progress: no run kept under {' or '.join(str(base / RUNS) for base in bases)}", file=sys.stderr)
            return 1
        if not args.all: folders = [folder for folder in folders if running(folder)] or folders[-1:]
    for folder in folders: print(*describe(folder), sep="\n")
    return 0
