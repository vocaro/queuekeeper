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
DIAGNOSTIC_LIMIT = 1024


def diagnostic_text(value):
    if not value: return ""
    prefix = value[:DIAGNOSTIC_LIMIT]
    if isinstance(prefix, bytes): prefix = prefix.decode(errors="replace")
    return prefix + (" [truncated]" if len(value) > DIAGNOSTIC_LIMIT else "")


def cpu(identities, *, unavailable=None):
    """Cumulative CPU readings only for these exact PID/start identities; None if any is unreadable."""
    began = time.monotonic() if unavailable is not None else None

    def failed(reason, **evidence):
        if unavailable is not None:
            unavailable(reason, {"elapsedSeconds": time.monotonic() - began,
                                 "requested": [{"pid": pid, "start": start} for pid, start in sorted(identities.items())],
                                 **evidence})
        return None

    if not identities: return failed("no-identities")
    try:
        listed = subprocess.run(["ps", "-o", "pid=,lstart=,time=", "-p", ",".join(map(str, sorted(identities)))],
                                capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=INTERVAL)
    except subprocess.TimeoutExpired as error:
        return failed("process-list-timeout", stdout=diagnostic_text(error.stdout), stderr=diagnostic_text(error.stderr))
    except OSError as error:
        return failed("process-list-failed", error=diagnostic_text(str(error)))
    except ValueError as error:
        return failed("malformed-output", error=diagnostic_text(str(error)))
    if listed.returncode:
        # On macOS a valid selection with no live PIDs exits 1 without output on either stream.
        reason = ("missing-identity" if listed.returncode == 1 and not listed.stdout and not listed.stderr
                  else "process-list-failed")
        return failed(reason, returncode=listed.returncode, stdout=diagnostic_text(listed.stdout),
                      stderr=diagnostic_text(listed.stderr))
    found = {}
    for line in listed.stdout.splitlines():
        parts = line.split()
        if len(parts) != 7: return failed("malformed-output", row=diagnostic_text(line))
        try:
            pid = int(parts[0])
            start = " ".join(parts[1:6])
            if pid not in identities: return failed("malformed-output", row=diagnostic_text(line))
            if identities[pid] != start:
                return failed("changed-identity", pid=pid, observedStart=diagnostic_text(start))
            # ps uses MM:SS.hh, HH:MM:SS, and optionally a leading day count.
            days, separator, clock = parts[6].partition("-")
            seconds = 0
            for component in (clock if separator else parts[6]).split(":"):
                seconds = seconds * 60 + float(component)
            if separator: seconds += float(days) * 86400
            found[pid] = (start, seconds)
        except ValueError:
            return failed("malformed-output", row=diagnostic_text(line))
    if found.keys() != identities.keys():
        return failed("missing-identity", missingPids=sorted(identities.keys() - found.keys()))
    return found


class Quiet:
    """Consecutive, observable quiet samples. A new process or changed output counts as progress."""
    def __init__(self):
        self.previous, self.since, self.wall, self.monotonic = None, None, None, None
        self.gap = False

    def sample(self, now, wall, state):
        self.gap = self.wall is not None and (wall - self.wall > 3 * INTERVAL or now - self.monotonic > 3 * INTERVAL
                                              or wall < self.wall or now < self.monotonic)
        if state is None or self.gap:
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
        self.sampling_gaps = 0
        self.cpu_unavailable_samples, self.last_cpu_unavailable = {}, None
        self.log_unavailable_samples, self.last_log_unavailable = {}, None
        self.publication_failures, self.last_publication_failure = 0, None
        self.live_mirror_failures, self.last_live_mirror_failure = 0, None
        self.publication_failed = False
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
        readings = cpu(work, unavailable=self.record_cpu_unavailable)
        try:
            stat = self.log.stat()
            output = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
        except OSError as error:
            reason = "missing-log" if isinstance(error, FileNotFoundError) else "log-stat-failed"
            self.log_unavailable_samples[reason] = self.log_unavailable_samples.get(reason, 0) + 1
            self.last_log_unavailable = {"reason": reason, "observedAt": wall, "error": diagnostic_text(str(error))}
            output = None
        quiet = self.quiet.sample(now, wall, (readings, output) if readings is not None and output is not None else None)
        if self.quiet.gap:
            self.sampling_gaps += 1
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
            self.publish_live()
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

    def record_cpu_unavailable(self, reason, evidence):
        self.cpu_unavailable_samples[reason] = self.cpu_unavailable_samples.get(reason, 0) + 1
        self.last_cpu_unavailable = {"reason": reason, "observedAt": time.time(), **evidence}

    def record_live_mirror_failure(self, error):
        self.live_mirror_failures += 1
        self.last_live_mirror_failure = {"observedAt": time.time(), "error": diagnostic_text(str(error))}

    def publish_live(self):
        if self.live_paths:
            from . import lane_progress
            unavailable = self.record_live_mirror_failure if self.last["phase"] == "running" else None
            lane_progress.publish(self.last, self.live_paths, unavailable=unavailable)

    def write(self):
        self.status.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.status.with_name(self.status.name + ".tmp-" + self.run)
        try:
            retained = {**self.last, "samplingGapSamples": self.sampling_gaps,
                        "cpuUnavailableSamples": self.cpu_unavailable_samples,
                        "logUnavailableSamples": self.log_unavailable_samples,
                        "publicationFailures": self.publication_failures,
                        "liveMirrorFailures": self.live_mirror_failures}
            if self.last_cpu_unavailable is not None: retained["lastCpuUnavailable"] = self.last_cpu_unavailable
            if self.last_log_unavailable is not None: retained["lastLogUnavailable"] = self.last_log_unavailable
            if self.last_publication_failure is not None: retained["lastPublicationFailure"] = self.last_publication_failure
            if self.last_live_mirror_failure is not None: retained["lastLiveMirrorFailure"] = self.last_live_mirror_failure
            temporary.write_text(json.dumps(retained, indent=2) + "\n")
            temporary.replace(self.status)
        except OSError:
            try: temporary.unlink()
            except OSError: pass
            raise

    def publish(self):
        try:
            self.write()
        except OSError as error:
            self.publication_failures += 1
            self.last_publication_failure = {"observedAt": time.time(), "error": diagnostic_text(str(error))}
            if not self.publication_failed:
                import sys
                print("supervise: progress observations unavailable: could not write the retained receipt; "
                      "no stall stop requested",
                      file=sys.stderr, flush=True)
            self.publication_failed = True
            return False
        self.publication_failed = False
        self.publish_live()
        return True

    def finish(self, state, settled=True):
        if self.last is not None:
            self.last.update(phase="ended" if settled else "uncertain", state=state, finishedAt=time.time())
            self.publish()
            self.publish_live()
