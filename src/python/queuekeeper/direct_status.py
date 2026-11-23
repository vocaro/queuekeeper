"""Local, read-only inspection of observations from exact direct lane invocations."""
import argparse
from contextlib import contextmanager
from contextvars import ContextVar
import os
from pathlib import Path
import re
import time
import uuid

from . import lane_progress

ACTIVE = ContextVar("direct_lane_status", default=None)
RUN_ID = re.compile(r"[0-9a-f]{32}")


def directory(root):
    return Path(root) / ".build/queuekeeper/.direct-status"


@contextmanager
def tracking(root, lane):
    """Publish under an invocation's own identity, leaving no state an overlapping invocation can overwrite."""
    pid = os.getpid()
    started = lane_progress.start(pid) if ACTIVE.get() is None else None
    if started is None:
        yield
        return
    run_id = uuid.uuid4().hex
    path = directory(root) / f"{run_id}.json"
    record = {"lane": lane, "pid": pid, "processStart": started, "runID": run_id, "host": "this Mac"}
    token = ACTIVE.set((path, record))
    try:
        observe(state="starting")
        yield
    finally:
        ACTIVE.reset(token)
        try: path.unlink(missing_ok=True)
        except OSError: pass


def observe(**fields):
    """Save only supplied observations. No status reader is needed for the run to proceed."""
    if (active := ACTIVE.get()) is None: return
    path, record = active
    if all(record.get(key) == value for key, value in fields.items()): return
    updated = {**record, **fields, "observedAt": time.time()}
    try: lane_progress.write(path, updated)
    except OSError: return
    record.update(updated)


def records(root, lane, run_id=None):
    base = directory(root)
    if run_id is not None and not RUN_ID.fullmatch(run_id): return []
    observed = []
    try:
        paths = (base / f"{run_id}.json",) if run_id else base.glob("*.json")
        for path in paths:
            found = lane_progress.read(path)
            if (isinstance(found, dict) and found.get("lane") == lane and RUN_ID.fullmatch(path.stem)
                    and found.get("runID") == path.stem and type(found.get("pid")) is int and found["pid"] > 0
                    and all(isinstance(found.get(key), str) for key in ("processStart", "host", "state"))):
                observed.append(found)
    except OSError: pass
    live = lane_progress.starts([found["pid"] for found in observed])
    return [found for found in observed if live.get(found["pid"]) == found["processStart"]]


def show(root, lane, run_id=None):
    found = records(root, lane, run_id)
    if not found:
        print(f"No live direct run of {lane} found from this checkout.")
        return 1
    for record in sorted(found, key=lambda item: item["runID"]):
        print(f"{record['lane']} on {record['host']}: {record['state']} (run {record['runID']})")
        report = record.get("stage") if record["host"] != "this Mac" else None
        if isinstance(report, str):
            print(f"  Last reported stage: {report}")
    return 0


def main(root, arguments):
    parser = argparse.ArgumentParser(prog="qk run --status", description="Read live direct-run observations")
    parser.add_argument("lane")
    parser.add_argument("--run-id", metavar="ID")
    args = parser.parse_args(arguments)
    if args.run_id is not None and not RUN_ID.fullmatch(args.run_id):
        parser.error("--run-id must be the run ID shown by run --status")
    return show(Path(root or ".").resolve(), args.lane, args.run_id)
