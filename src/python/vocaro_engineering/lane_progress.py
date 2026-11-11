"""Live observations beside wait records; never a verdict or a reason to alter admission."""
import json
import fcntl
import hashlib
import re
from itertools import islice
import math
import os
from pathlib import Path
import subprocess
import stat
import time

from . import lane_wait

LIMIT, MAX_BYTES, FRESH = 128, 65536, 15
VARIABLE = "VOCARO_LANE_PROGRESS_RUN"


def directory(wait, namespace=None):
    if namespace is None:
        record = read(wait)
        namespace = record.get("progressRun") if isinstance(record, dict) else None
    if not isinstance(namespace, str) or not re.fullmatch(r"[0-9a-f]{32}", namespace): return None
    return Path(str(wait) + ".progress") / namespace


def starts(pids):
    if not pids: return {}
    try:
        found = subprocess.run(["ps", "-p", ",".join(map(str, sorted(set(pids)))), "-o", "pid=,lstart="],
                               capture_output=True, text=True, timeout=1, stdin=subprocess.DEVNULL)
        return {int(parts[0]): " ".join(parts[1:]) for line in found.stdout.splitlines()
                if len(parts := line.split()) == 6 and parts[0].isdigit()}
    except (OSError, ValueError, subprocess.SubprocessError): return {}


def start(pid):
    return starts([pid]).get(pid)


def write(path, record):
    path = Path(path)
    staged = path.with_name(path.name + f".tmp-{os.getpid()}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        (path.parent / ".gitignore").write_text("*\n")
        staged.write_text(json.dumps(record) + "\n")
        staged.replace(path)
    except OSError:
        try: staged.unlink()
        except OSError: pass


def read(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode): return None
            data = stream.read(MAX_BYTES + 1)
        return json.loads(data) if len(data) <= MAX_BYTES else None
    except (OSError, ValueError): return None


def valid(record, since=None, at=None):
    if not isinstance(record, dict) or record.get("phase") != "running": return False
    when = record.get("observedAt")
    now = time.time() if at is None else at
    return (type(when) in (int, float) and math.isfinite(when) and 0 <= now - when <= FRESH
            and (since is None or (type(record.get("startedAt")) in (int, float) and record["startedAt"] >= since)) and isinstance(record.get("run"), str)
            and type(record.get("reporter")) is int and record["reporter"] > 0
            and isinstance(record.get("reporterStart"), str)
            and all(record.get(key) is None or isinstance(record.get(key), str)
                    for key in ("lane", "host", "state", "wait"))
            and all(record.get(key) is None or type(record.get(key)) in (int, float)
                    for key in ("quietSeconds", "thresholdSeconds", "maximumQuietSeconds")))


def current(wait, since=None):
    folder = directory(wait)
    return in_directory(folder, since) if folder is not None else []


def in_directory(folder, since=None, *, verify=True, limit=LIMIT):
    records = []
    try:
        paths = Path(folder).iterdir()
        for index, path in enumerate(paths):
            if index >= 2 * limit + 1 or len(records) >= limit: break
            if path.suffix != ".json" or path.is_symlink(): continue
            record = read(path)
            if valid(record, since): records.append(record)
    except OSError: pass
    if verify:
        identities = starts([record["reporter"] for record in records])
        records = [record for record in records if identities.get(record["reporter"]) == record["reporterStart"]]
    return sorted(records, key=lambda record: (record.get("lane") or "", record["run"]))


def publish(record, paths):
    for path in paths:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.with_suffix(".lock").open("a+") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                previous = read(path)
                if not isinstance(previous, dict): previous = None
                if previous and previous.get("run") != record.get("run"):
                    if record.get("phase") != "running" or previous.get("startedAt", 0) > record.get("startedAt", 0): continue
                if record.get("phase") != "running": path.unlink(missing_ok=True)
                else: write(path, record)
        except (OSError, TypeError): pass


def destinations(run, leaf=None):
    paths = []
    key = hashlib.sha256(str(leaf if leaf is not None else run).encode()).hexdigest()
    wait = os.environ.get(lane_wait.VARIABLE)
    if wait:
        folder = directory(wait, os.environ.get(VARIABLE))
        if folder is not None: paths.append(folder / f"{key}.json")
    from . import resources
    try:
        token = json.loads(os.environ.get(resources.TOKEN) or "{}")
        identities = {lock["nonce"] for lock in token.get("locks", [])}
        guard = token.get("guard")
        guard = Path(guard) if isinstance(guard, str) and guard else resources.guard_path(os.environ)
        for identity in identities:
            if isinstance(identity, str) and identity.isalnum():
                paths.append(Path(str(guard) + ".progress") / identity / f"{key}.json")
    except (ValueError, TypeError, KeyError, AttributeError): pass
    return paths


def relayed(records, host):
    wait = os.environ.get(lane_wait.VARIABLE)
    if not wait or not isinstance(records, list): return
    reporter, born = os.getpid(), start(os.getpid())
    if not born: return
    # A relay is a snapshot: remove only this dispatcher's prior records, including completed members.
    prefix = f"relay-{reporter}-"
    folder = directory(wait, os.environ.get(VARIABLE))
    if folder is None: return
    try:
        for path in islice(folder.iterdir(), 2 * LIMIT + 1):
            if path.name.startswith(prefix): path.unlink(missing_ok=True)
    except OSError: pass
    for record in records[:LIMIT]:
        if not valid(record): continue
        safe = {**record, "host": host, "reporter": reporter, "reporterStart": born}
        # Never use remote content as a path component.
        key = hashlib.sha256(record["run"].encode()).hexdigest()
        write(folder / f"{prefix}{key}.json", safe)


def blockers(guard, owners):
    candidates = []
    owners = dict(list(owners.items())[:LIMIT])
    identities = starts([owner["pid"] for owner in owners.values() if type(owner.get("pid")) is int])
    for identity, owner in owners.items():
        if (owner.get("uncertain") or type(owner.get("pid")) is not int or not isinstance(owner.get("start"), str)
                or identities.get(owner["pid"]) != owner["start"]): continue
        for record in in_directory(Path(str(guard) + ".progress") / identity,
                                   verify=False, limit=LIMIT - len(candidates)):
            candidates.append({**record, "admission": identity, "label": owner.get("label")})
        if len(candidates) >= LIMIT: break
    reporters = starts([record["reporter"] for record in candidates])
    return [record for record in candidates if reporters.get(record["reporter"]) == record["reporterStart"]]


def words(record):
    quiet = record.get("quietSeconds")
    interval = "unobservable" if quiet is None else f"quiet {quiet:.1f}s / {record.get('thresholdSeconds')}s"
    label = f"{record['label']}: " if record.get("label") else ""
    last = record.get("lastProgressAt")
    last = f"{max(0, time.time() - last):.0f}s ago" if type(last) in (int, float) else "unknown"
    def seconds(key):
        value = record.get(key)
        return f"{value:.1f}s" if type(value) in (int, float) else "unknown"
    return (f"{label}{record.get('lane') or 'lane'} on {record.get('host')}: {record.get('state')}, {interval}; "
            f"phase {record.get('phase')}; last progress {last}; max quiet {seconds('maximumQuietSeconds')}; "
            f"samples {record.get('samples')} ({record.get('unobservableSamples')} unobservable); "
            f"observed {seconds('observedSeconds')}, max sampling gap {seconds('maximumObservationGapSeconds')}; "
            f"wait {record.get('wait', 'unknown')}")
