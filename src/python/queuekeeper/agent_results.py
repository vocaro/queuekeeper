"""Bounded, versioned results for an entry, independent of the status view's recent window.

Artifact paths identify the exact recorded run. Missing or pruned artifacts stay in the result
with available=false; a later run's artifacts are never substituted for them.
"""
import json
from pathlib import Path
import re
import stat
import sys

from . import lane_report

VERSION = 1
# The most of a log a result reads. The largest queue log on the owner's Macs on 2026-11-15 was 5 MB, so
# a result reads each such log whole: the count in the last 1 MiB of a 4.4 MB gate log was once reported
# as the run's (vocaro/queuekeeper#163).
LOG_BYTES = 64 << 20
SHOWN = 20
WAITING = {"queued", "testing", "passed"}


def diagnostic_excerpt(path):
    """At most LOG_BYTES of a local file, and whether earlier bytes were omitted."""
    try:
        if not stat.S_ISREG(Path(path).stat().st_mode): return "", False
        with Path(path).open("rb") as stream:
            stream.seek(0, 2)
            size = stream.tell()
            stream.seek(max(0, size - LOG_BYTES))
            return stream.read(LOG_BYTES).decode("utf-8", errors="replace"), size > LOG_BYTES
    except OSError:
        return "", False


def artifact(kind, path, host=None):
    """Returned dispatch files live locally even when a different host produced them."""
    path = Path(path)
    return {"kind": kind, "path": str(path), "location": "local", "producedOn": host,
            "available": path.exists()}


def entry_artifacts(queue, entry):
    """Recorded artifact paths and current gates, without opening any log."""
    artifacts = []
    host = None
    dispatch = entry.get("failedDispatch") or {}
    if dispatch.get("directory"):
        directory = Path(dispatch["directory"])
        raw, _ = diagnostic_excerpt(directory / "source.json")
        try: receipt = json.loads(raw)
        except ValueError: receipt = {}
        if isinstance(receipt, dict):
            host = receipt.get("host") or receipt.get("mac")
            if not isinstance(host, str): host = None
        for kind, name in (("run-log", "run.log"), ("dispatch-log", "output.log"),
                           ("receipt", "source.json"), ("results", "results")):
            artifacts.append(artifact(kind, directory / name, host))
    log = entry.get("log") or (entry.get("gate") or {}).get("log")
    if log:
        artifacts.append({**artifact("queue-log", log, host), "primary": True})
    for reference in entry.get("logs", []):
        if not reference.get("path"): continue
        item = artifact("queue-log", reference["path"], reference.get("mac"))
        item["lane"] = reference.get("lane")
        item["status"] = reference.get("status")
        artifacts.append(item)
    gates = []
    if entry["state"] in WAITING and queue.working():
        for gate in (queue.round_state() or {}).get("gates", {}).values():
            if entry["id"] not in gate.get("entries", []): continue
            gates.append({key: gate.get(key) for key in ("lane", "mac", "began", "waiting")})
            if gate.get("log"):
                artifacts.append({**artifact("queue-log", gate["log"], gate.get("mac")),
                                  "current": True, "lane": gate.get("lane")})
    unique = {}
    for item in artifacts:
        unique.setdefault(item["path"], {}).update(item)
    return list(unique.values()), gates


def entry_result(queue, entry):
    """Normalize one persisted entry without listing other entries or contacting another host."""
    from . import recovery

    state = entry["state"]
    pending = state in WAITING
    held = recovery.read_hold(queue.hold_file, queue.held()) if pending else None
    artifacts, gates = entry_artifacts(queue, entry)
    texts, summaries, truncated = [], [], False
    # A log longer than LOG_BYTES is read only at its end, and logTruncated says so. The cursor-based
    # logs command reads the complete file.
    diagnostic_logs = [item for item in artifacts if item["kind"] == "run-log"]
    primary = [item for item in artifacts if item.get("primary")]
    # Earlier failed/bisected rounds are history, not evidence about the final attempt. A
    # custom successful lane may have no measured gate, so its newest completed log is used.
    completed = [item for item in artifacts if item["kind"] == "queue-log"]
    diagnostic_logs.extend(primary or completed[-1:])
    for item in diagnostic_logs:
        if pending: continue
        text, clipped = diagnostic_excerpt(item["path"])
        texts.append((text, clipped))
        truncated |= clipped
        ended = lane_report.run_summary(text, Path(item["path"]).name)
        if ended is not None: summaries.append(ended)
    # The summary qk run ends with counts every lane of a group, whose log also holds its qk unittest lane's own
    # output, which counts only that lane's tests (vocaro/queuekeeper#193); it is whole at a log's end. Without one,
    # the exact dispatch's complete test output is preferred to the queue's potentially repeated summary.
    # Only a log read whole is counted: the end of a log counts part of a run (vocaro/queuekeeper#163).
    counts = next((found for ended in summaries if (found := lane_report.summary_counts(ended)) is not None), None) \
        or next((found for text, clipped in texts
                 if not clipped and (found := lane_report.counts(text)) is not None), None)
    failures, unnamed = {}, 0
    for ended in summaries:
        for name, place in ended["names"]:
            if name not in failures or place: failures[name] = place
        unnamed = max(unnamed, ended["more"])
    # A summary names each failed test once, as its lanes' logs, which can name one in two ways, may not.
    for text, _ in [] if failures else texts:
        for name, place in lane_report.failing(text):
            if name not in failures or place: failures[name] = place
    failure = None
    # The entries this one fails together with, though each passes alone (merge_queue.Worker.together).
    partners = [dict(partner) for partner in entry.get("conflictWith") or []]
    if state in ("failed", "conflict"):
        failure = {"category": "conflict" if state == "conflict" else "fails-together" if partners else "gate-failed",
                   "message": entry.get("why"), "tests": [
                       {"name": name, "location": place} for name, place in list(failures.items())[:SHOWN]],
                   "moreTests": max(0, len(failures) - SHOWN) + unnamed, "testsTruncated": truncated,
                   "excerpt": ((entry.get("failedDispatch") or {}).get("excerpt") or [])[:SHOWN],
                   "conflictWith": partners}
    return {"schemaVersion": VERSION, "project": str(queue.root),
            "id": entry["id"], "commit": entry.get("tip"),
            "state": state, "terminal": not pending, "landedAs": entry.get("landedAs"),
            "subject": entry.get("subject"), "branch": entry.get("branch"),
            "submitted": entry.get("submitted"), "settled": entry.get("settled"),
            "failure": failure, "conflictWith": partners, "tests": counts, "logTruncated": truncated,
            "flakes": [{**flake, "reruns": {"status": "passed", "count": len(flake["rerunLogs"])
                                           if "rerunLogs" in flake else None}}
                       for flake in entry.get("flakes", [])],
            "artifacts": artifacts, "gates": gates,
            "resultsSummary": (entry.get("gate") or {}).get("results"),
            "docsOnly": entry.get("docsOnly"), "docsNamed": entry.get("docsNamed"), "ungated": entry.get("ungated"),
            "publication": publication(queue, entry) if pending else None,
            "recovery": recovery.in_project(recovery.for_entry(entry, held, unverified=unverified(queue, entry)),
                                            queue.root)}


def unverified(queue, entry):
    """Whether entry, still waiting, was pushed without an answer: its push timed out or lost its connection
    (unanswered), or no worker runs to hear one, as when the worker died mid-push."""
    marker = entry.get("publishing") if entry.get("state") in WAITING else None
    return bool(marker) and (bool(marker.get("unanswered")) or not queue.working())


def publication(queue, entry):
    """The push a waiting entry's record keeps until it is answered (Worker.push's publishing): unverified, as
    unverified says, or pushing; None when there is none."""
    marker = entry.get("publishing")
    if not marker: return None
    return {"status": "unverified" if unverified(queue, entry) else "pushing",
            "heads": [attempt.get("head") for attempt in marker.get("attempts", [])]}


def read_entry(queue, identifier):
    """Read a safe entry ID directly, rather than searching the bounded status snapshot."""
    if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", identifier):
        raise ValueError("An entry ID must be a name, without path separators")
    entry = queue.read(identifier)
    if not isinstance(entry, dict): raise ValueError(f"The queue has no entry {identifier}")
    return entry


def lookup(queue, identifier):
    return entry_result(queue, read_entry(queue, identifier))


def result(root, identifier, as_json=False):
    """A successful lookup exits 0 for any state; queue wait supplies the verdict exit status."""
    from . import merge_queue

    try:
        found = lookup(merge_queue.Queue(root), identifier)
    except (ValueError, OSError) as error:
        if not as_json: raise
        from .agent_cli import error_result
        print(f"queuekeeper: {error}", file=sys.stderr)
        found = error_result("usage-or-configuration-error", str(error))
        found.update(operation="queue.result", exitStatus=2, requestedIds=[identifier])
        print(json.dumps(found, indent=2))
        return 2
    found.update(operation="queue.result", exitStatus=0)
    if as_json:
        print(json.dumps(found, indent=2))
    else:
        print(f"{found['state']:8} {found['id']}  {found['subject'] or ''}")
        if found["landedAs"]: print(f"landed: {found['landedAs']}")
        if found["failure"]:
            print(found["failure"]["message"] or found["failure"]["category"])
            for test in found["failure"]["tests"]:
                print(f"  failed: {test['name']}" + (f" at {test['location']}" if test["location"] else ""))
        for item in found["artifacts"]:
            print(f"{item['kind']}: {item['path']}" + ("" if item["available"] else " (unavailable or pruned)"))
    return 0
