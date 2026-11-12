"""Opt-in quiet-interval observations for one supervised lane, never an inactivity default."""
import json
import os
from pathlib import Path
import socket
import subprocess
import time

# A cadence, not a stopping bound. Three missed cadences invalidate the quiet interval; no
# conclusion about idleness is made across a host sleep or a delayed/unavailable observation.
INTERVAL = 1.0


def cpu(identities):
    """Cumulative CPU readings only for these exact PID/start identities; None if any is unreadable."""
    if not identities: return None
    try:
        listed = subprocess.run(["ps", "-o", "pid=,lstart=,time=", "-p", ",".join(map(str, sorted(identities)))],
                                capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=INTERVAL)
        if listed.returncode: return None
        found = {}
        for line in listed.stdout.splitlines():
            parts = line.split()
            if len(parts) != 7: return None
            pid, start, used = int(parts[0]), " ".join(parts[1:6]), parts[6]
            if identities.get(pid) != start: return None
            # ps uses MM:SS.hh, HH:MM:SS, and optionally a leading day count.
            days, separator, clock = used.partition("-")
            seconds = 0
            for component in (clock if separator else used).split(":"):
                seconds = seconds * 60 + float(component)
            if separator: seconds += float(days) * 86400
            found[pid] = (start, seconds)
        return found if found.keys() == identities.keys() else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


class Quiet:
    """Consecutive, observable quiet samples. A new process or changed output counts as progress."""
    def __init__(self):
        self.previous, self.since, self.wall, self.monotonic = None, None, None, None

    def sample(self, now, wall, state):
        gap = self.wall is not None and (wall - self.wall > 3 * INTERVAL or now - self.monotonic > 3 * INTERVAL
                                        or wall < self.wall or now < self.monotonic)
        if state is None or gap:
            self.previous, self.since = None, None
        elif state != self.previous:
            self.previous, self.since = state, now
        self.wall, self.monotonic = wall, now
        return None if self.since is None else now - self.since


class Monitor:
    def __init__(self, settings, log, status):
        self.settings, self.log = settings, Path(log)
        self.status = Path(status) if status is not None else self.log.with_suffix(".progress.json")
        self.quiet, self.next, self.warned, self.last = Quiet(), 0, False, None
        self.run = f"{os.getpid()}-{time.time_ns()}"
        self.began = time.time()
        self.maximum, self.samples, self.unobservable = 0, 0, 0
        self.write_error = None
        from . import lane_progress
        self.live_paths = lane_progress.destinations(self.run, self.status)
        self.reporter_start = lane_progress.start(os.getpid()) if self.live_paths else None
        self.observed_seconds, self.maximum_gap, self.observed_previous = 0, 0, None
        self.previously_observable = False
        self.observed_wall = None

    def observe(self, identities, excluded=(), force=False):
        now, wall = time.monotonic(), time.time()
        if not force and now < self.next: return None
        self.next = now + INTERVAL
        work = {pid: " ".join(start.split()) for pid, start in identities.items() if pid not in excluded}
        readings = cpu(work)
        try:
            stat = self.log.stat()
            output = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
        except OSError:
            output = None
        quiet = self.quiet.sample(now, wall, (readings, output) if readings is not None and output is not None else None)
        added_coverage = 0
        if self.observed_previous is not None:
            gap = now - self.observed_previous
            self.maximum_gap = max(self.maximum_gap, gap, wall - self.observed_wall)
            if self.previously_observable and quiet is not None and 0 <= gap <= 3 * INTERVAL:
                added_coverage = gap
                self.observed_seconds += gap
        self.observed_previous, self.observed_wall = now, wall
        self.previously_observable = quiet is not None
        self.samples += 1
        if quiet is None: self.unobservable += 1
        else: self.maximum = max(self.maximum, quiet)
        threshold = self.settings["seconds"]
        stalled = quiet is not None and quiet >= threshold
        self.last = {"run": self.run, "host": socket.gethostname(), "phase": "running", "observedAt": wall,
                     "startedAt": self.began,
                     "quietSeconds": quiet, "thresholdSeconds": threshold, "action": self.settings.get("action", "report"),
                     "state": "quiet" if stalled else "observing" if quiet is not None else "unobservable",
                     "maximumQuietSeconds": self.maximum, "samples": self.samples,
                     "unobservableSamples": self.unobservable, "reason": self.settings.get("reason"),
                     "lastProgressAt": None if quiet is None else wall - quiet,
                     "processes": [{"pid": pid, "start": start} for pid, start in sorted(work.items())],
                     "wait": "unknown", "reporter": os.getpid(), "reporterStart": self.reporter_start,
                     "lane": self.log.stem if self.log.name != "run.log" else self.log.parent.name,
                     "observedSeconds": self.observed_seconds, "maximumObservationGapSeconds": self.maximum_gap}
        if not self.publish():
            # An optional observer must never turn its own I/O failure into a stop, or
            # preserve a quiet interval that it could not record for review.
            self.quiet.sample(now, wall, None)
            self.previously_observable = False
            self.observed_seconds -= added_coverage
            self.last["observedSeconds"] = self.observed_seconds
            if quiet is not None: self.unobservable += 1
            self.last.update(state="unobservable", quietSeconds=None, lastProgressAt=None,
                             unobservableSamples=self.unobservable)
            if self.live_paths:
                from . import lane_progress
                lane_progress.publish(self.last, self.live_paths)
            self.warned = False
            return None
        if not stalled:
            self.warned = False
            return None
        detail = (f"no observed CPU or log progress for {quiet:.1f}s (configured {threshold:g}s); "
                  f"host {self.last['host']}; owned work PIDs {', '.join(map(str, sorted(work)))}; wait unknown; "
                  f"observations: {self.status}")
        if not self.warned:
            import sys
            print(f"supervise: quiet lane: {detail}; action {self.last['action']}", file=sys.stderr, flush=True)
            self.warned = True
        return detail if self.last["action"] == "stop" else None

    def write(self):
        self.status.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.status.with_name(self.status.name + ".tmp-" + self.run)
        try:
            temporary.write_text(json.dumps(self.last, indent=2) + "\n")
            temporary.replace(self.status)
        except OSError:
            try: temporary.unlink()
            except OSError: pass
            raise

    def publish(self):
        try:
            self.write()
        except OSError as error:
            detail = str(error)
            if detail != self.write_error:
                import sys
                print(f"supervise: progress observations unavailable: {detail}; no stall stop requested",
                      file=sys.stderr, flush=True)
            self.write_error = detail
            self.last["observationError"] = detail
            return False
        self.write_error = None
        if self.live_paths:
            from . import lane_progress
            lane_progress.publish(self.last, self.live_paths)
        return True

    def finish(self, state, settled=True):
        if self.last is not None:
            self.last.update(phase="ended" if settled else "uncertain", state=state, finishedAt=time.time())
            self.publish()
            if self.live_paths:
                from . import lane_progress
                lane_progress.publish(self.last, self.live_paths)
