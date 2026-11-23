"""Bounded, versioned results for an entry, independent of the status view's recent window.

Artifact paths identify the exact recorded run. Missing or pruned artifacts stay in the result
with available=false; a later run's artifacts are never substituted for them.
"""
import json
from pathlib import Path
import re
import shlex
import stat
import sys
import time

from . import lane_report, lane_timing

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
    if entry.get("doctorLog"):
        artifacts.append(artifact("queue-doctor-log", entry["doctorLog"]))
    if entry.get("setupDoctorLog"):
        artifacts.append(artifact("setup-doctor-log", entry["setupDoctorLog"]))
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
            gates.append({key: gate.get(key) for key in ("lane", "mac", "began", "waiting", "timing")})
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
    # The timing warnings, failures and limit notices the run's logs hold (lane_timing), each once.
    timing = []
    for text, _ in texts:
        timing += [line for line in lane_timing.found_in(text) if line not in timing]
    failure = None
    # The entries this one fails together with, though each passes alone (merge_queue.Worker.together).
    partners = [dict(partner) for partner in entry.get("conflictWith") or []]
    if state in ("failed", "conflict"):
        category = "conflict" if state == "conflict" else "fails-together" if partners else "gate-failed"
        if entry.get("noVerdict"): category = "gate-no-verdict"
        if entry.get("publicationRefused"): category = "publication-refused"
        failure = {"category": category,
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
            "timing": timing[:SHOWN],
            "flakes": [{**flake, "reruns": {"status": "passed", "count": len(flake["rerunLogs"])
                                           if "rerunLogs" in flake else None}}
                       for flake in entry.get("flakes", [])],
            "artifacts": artifacts, "gates": gates,
            "resultsSummary": (entry.get("gate") or {}).get("results"),
            **({"gateExecution": execution} if (execution := entry.get("gateExecution")) else {}),
            "docsOnly": entry.get("docsOnly"), "docsNamed": entry.get("docsNamed"), "ungated": entry.get("ungated"),
            "partialGate": entry.get("partialGate"),
            "reusedGates": entry.get("reusedGates"),
            "publication": publication(queue, entry) if pending else None,
            "removedWhy": entry.get("removedWhy"), "confirmed": entry.get("confirmed"),
            "recovery": recovery.in_project(
                detail := recovery.for_entry(entry, held, unverified=unverified(queue, entry)), queue.root),
            "nextAction": recovery.next_action(entry, detail, queue.root)}


def unverified(queue, entry):
    """Whether entry, still waiting, was pushed without an answer: its push timed out or lost its connection
    (unanswered), or no worker runs to hear one, as when the worker died mid-push."""
    if entry.get("state") not in WAITING: return False
    if entry.get("pushedBefore"): return True
    marker = entry.get("publishing")
    return bool(marker) and (bool(marker.get("unanswered")) or not queue.working())


def publication(queue, entry):
    """The push a waiting entry's record keeps until it is answered (Worker.push's publishing): unverified, as
    unverified says, or pushing; None when there is none."""
    markers = [*(entry.get("pushedBefore") or []), entry.get("publishing")]
    heads = list(dict.fromkeys(attempt["head"] for marker in markers if marker
                               for attempt in marker.get("attempts", []) if attempt.get("head")))
    if not heads: return None
    return {"status": "unverified" if unverified(queue, entry) else "pushing", "heads": heads}


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
        for lane, source in (found["reusedGates"] or {}).items():
            print(f"{lane}: reused the passing local run at {source['commit'][:12]}")
        if found["removedWhy"]: print(f"removed: {found['removedWhy']}")
        if found["failure"]:
            print(found["failure"]["message"] or found["failure"]["category"])
            for test in found["failure"]["tests"]:
                print(f"  failed: {test['name']}" + (f" at {test['location']}" if test["location"] else ""))
        for item in found["artifacts"]:
            print(f"{item['kind']}: {item['path']}" + ("" if item["available"] else " (unavailable or pruned)"))
        if found["nextAction"]: print(f"next: {shlex.join(found['nextAction']['command'])}")
    return 0


# How long a settled outcome stays in `qk queue inbox` unconfirmed: long enough for a session that died to be taken over.
INBOX_DAYS = 14
OUTCOMES = {"landed": "landed", "failed": "sent-back", "conflict": "sent-back", "removed": "removed"}


def current_branch(root):
    from . import merge_queue
    found = merge_queue.git(root, "symbolic-ref", "--short", "-q", "HEAD")
    branch = found.stdout.strip() if found.returncode == 0 else ""
    return branch if branch and branch not in ("main", "HEAD") else None


def outcomes(queue, root, now=None):
    """The entries of root's worktree or branch whose outcome nobody has confirmed (`qk queue confirm`), oldest first:
    those settled in the last INBOX_DAYS, and those waiting while the queue is held. Of a branch's entries only the
    newest counts, as landing a branch again replaces its earlier entries."""
    from . import recovery

    now = time.time() if now is None else now
    directory, branch = str(Path(root).resolve()), current_branch(root)
    mine = [entry for entry in queue.all() if entry.get("directory") == directory
            or (branch is not None and entry.get("branch") == branch)]
    newest = {}
    for entry in mine:
        if entry.get("branch"): newest[entry["branch"]] = entry
    held = recovery.read_hold(queue.hold_file, queue.held())
    found = []
    for entry in mine:
        if entry.get("branch") and newest[entry["branch"]] is not entry: continue
        state = entry["state"]
        if state in WAITING:
            if not held: continue
            outcome, reason, detail = "held", held["message"], held
        else:
            if entry.get("confirmed") or now - entry.get("settled", 0) > INBOX_DAYS * 86400: continue
            outcome = OUTCOMES.get(state, state)
            reason = entry.get("removedWhy") if state == "removed" else entry.get("why")
            detail = recovery.for_entry(entry)
        found.append({"id": entry["id"], "state": state, "outcome": outcome, "subject": entry.get("subject"),
                      "branch": entry.get("branch"), "settled": entry.get("settled"), "landedAs": entry.get("landedAs"),
                      "reason": reason, "recovery": recovery.in_project(detail, root),
                      "nextAction": recovery.next_action(entry, detail, root)})
    return found, branch


def inbox(root, as_json=False):
    """`qk queue inbox`: exit 0 once read, whatever it holds."""
    from . import merge_queue, recovery

    found, branch = outcomes(merge_queue.Queue(root), root)
    following = (found[0]["nextAction"] if len(found) == 1 else
                 recovery.action(recovery.qualified(["qk", "queue", "inbox", "--json"], root),
                                 "Act on each outcome by its nextAction.") if found else None)
    if as_json:
        print(json.dumps({"schemaVersion": VERSION, "operation": "queue.inbox", "project": str(root),
                          "directory": str(Path(root).resolve()), "branch": branch, "entries": found,
                          "nextAction": following, "exitStatus": 0}, indent=2))
        return 0
    if not found: print("inbox: no unconfirmed outcome for this worktree or branch")
    for item in found:
        print(f"{item['outcome']:9} {item['id']}  {item['subject'] or ''}")
        if item["reason"]: print(f"          {item['reason']}")
        if item["nextAction"]: print(f"          next: {shlex.join(item['nextAction']['command'])}")
    return 0


def confirm(root, identifier, as_json=False):
    """`qk queue confirm`: 0 confirmed or confirmed already, 1 the entry still waits, 2 no such entry."""
    from . import merge_queue

    queue = merge_queue.Queue(root)
    try:
        with queue.locked():
            entry = read_entry(queue, identifier)
            already = entry.get("confirmed")
            if entry["state"] not in WAITING and not already:
                entry["confirmed"] = time.time()
                queue.save(entry)
    except (ValueError, OSError) as error:
        print(f"queuekeeper: {error}", file=sys.stderr)
        if as_json:
            from .agent_cli import error_result
            failed = error_result("usage-or-configuration-error", str(error))
            failed.update(operation="queue.confirm", exitStatus=2, requestedIds=[identifier])
            print(json.dumps(failed, indent=2))
        return 2
    waiting = entry["state"] in WAITING
    status = 1 if waiting else 0
    message = (f"{identifier} is still {entry['state']}: follow it with `qk queue wait {identifier}`" if waiting else
               f"{identifier} ({entry['state']}) was already confirmed" if already else
               f"confirmed {identifier} ({entry['state']})")
    if as_json:
        print(json.dumps({"schemaVersion": VERSION, "operation": "queue.confirm", "id": identifier,
                          "state": entry["state"], "confirmed": entry.get("confirmed"),
                          "alreadyConfirmed": bool(already), "message": message, "exitStatus": status}, indent=2))
    else:
        print(message, file=sys.stderr if waiting else sys.stdout)
    return status


def queue_next_action(queue, root, state):
    """`qk queue status --json`'s nextAction, for the session asking: a hold first, then this worktree's or branch's
    unconfirmed outcomes, then its entry still waiting, then a waiting queue with no worker; None when nothing is."""
    from . import recovery

    detail = state.get("recovery")
    if state.get("held") and detail and detail.get("suggestedCommands"):
        return recovery.action(detail["suggestedCommands"][0], detail["message"])
    try: found, branch = outcomes(queue, root)
    except (OSError, ValueError, KeyError): found, branch = [], None
    found = [item for item in found if item["outcome"] != "held"]
    if len(found) == 1: return found[0]["nextAction"]
    if found:
        return recovery.action(recovery.qualified(["qk", "queue", "inbox", "--json"], root),
                               f"{len(found)} outcomes of this worktree or branch are unconfirmed.")
    directory = str(Path(root).resolve())
    own = [entry for entry in state.get("waiting", []) if entry.get("directory") == directory
           or (branch is not None and entry.get("branch") == branch)]
    if own:
        return recovery.action(recovery.qualified(["qk", "queue", "wait", own[-1]["id"], "--json"], root),
                               "Follow this worktree's entry to its result.")
    if detail and detail.get("suggestedCommands"):
        return recovery.action(detail["suggestedCommands"][0], detail["message"])
    return None
