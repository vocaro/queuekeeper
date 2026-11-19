"""Actions an agent can take after a queue observation, without parsing its explanation.

``retryable`` means the same operation can be repeated without first repairing a cause.
Suggested commands are argument lists, never shell source. A hold suggests diagnosis, not an
unconditional resume: its cause must be fixed before the worker is allowed to continue.
"""
import json
import os
from pathlib import Path


def advice(code, party, retryable, action, message, commands=()):
    return {"reasonCode": code, "responsibleParty": party, "retryable": retryable,
            "action": action, "message": message, "suggestedCommands": [list(command) for command in commands]}


def in_project(detail, root):
    """Make suggested Queuekeeper commands refer to the observed queue from any working directory."""
    if detail is None: return None
    project = str(Path(root).resolve())
    commands = []
    for command in detail.get("suggestedCommands", []):
        qualified = list(command)
        if (qualified and qualified[0] == "qk" and not any(
                part == "--project" or part.startswith("--project=") for part in qualified[1:])):
            qualified[1:1] = ["--project", project]
        commands.append(qualified)
    return {**detail, "suggestedCommands": commands}


def for_hold(why, responsible_party=None):
    """A hold's broad category; legacy reasons use only the documented responsibility prefix."""
    if not why: return None
    if responsible_party not in ("owner", "session", "unknown"):
        responsible_party = ("owner" if why.startswith("for the owner") else
                             "session" if why.startswith("for a session") else "unknown")
    return advice("queue-held", responsible_party, False, "inspect-hold", why,
                  [["qk", "queue", "doctor", "--json"]])


def for_closed(closure, message):
    """A submission refused by a queue closed to new entries: the same submission works once whoever closed the queue
    reopens it, which the refused session watches for and does not do itself."""
    return {**advice("queue-closed", "unknown", False, "wait-for-reopen", message, [["qk", "queue", "status", "--json"]]),
            "closed": dict(closure)}


def write_hold(path, why, responsible_party=None):
    """Persist the structured hold beside the original text file; a failure cannot stop the queue."""
    target = Path(path).with_name("hold.json")
    staged = target.with_suffix(f".{os.getpid()}.tmp")
    try:
        staged.write_text(json.dumps({"schemaVersion": 1, "reason": why,
                                      "recovery": for_hold(why, responsible_party)}) + "\n")
        os.replace(staged, target)
    except OSError:
        try: staged.unlink()
        except OSError: pass


def read_hold(path, why):
    """Return only metadata for the current hold; stale or old worker records are harmless."""
    if not why: return None
    try:
        record = json.loads(Path(path).with_name("hold.json").read_text())
        detail = record.get("recovery")
        if (record.get("schemaVersion") == 1 and record.get("reason") == why and isinstance(detail, dict)
                and detail.get("responsibleParty") in ("owner", "session", "unknown")):
            return for_hold(why, detail["responsibleParty"])
    except (OSError, ValueError, AttributeError): pass
    return for_hold(why)


def for_entry(entry, held=None, *, limit_reached=False):
    """Recovery for a specific entry; a settled entry is unaffected by a later queue hold."""
    state = entry.get("state")
    identifier = entry["id"]
    if state in ("queued", "testing", "passed"):
        if held: return for_hold(held) if isinstance(held, str) else dict(held)
        code = "wait-limit" if limit_reached else "entry-pending"
        message = ("The wait ended at its limit; the entry is still in the queue." if limit_reached else
                   "The entry is still in the queue; follow this entry rather than submitting it again.")
        return advice(code, "session", True, "wait", message,
                      [["qk", "queue", "wait", identifier, "--json"]])
    if state == "landed":
        return advice("entry-landed", None, False, "none", "The entry landed.")
    if state == "conflict":
        return advice("entry-conflict", "session", False, "fix-conflict",
                      entry.get("why") or "Resolve the conflict and submit the corrected commits.")
    if state == "failed" and entry.get("conflictWith"):
        # It fails only together with these entries, each sent back too (merge_queue.Worker.together): their IDs
        # and commits, to rebase onto, and how to read why each came back.
        partners = [dict(partner) for partner in entry["conflictWith"]]
        return {**advice("entry-fails-together", "session", False, "fix-interaction",
                         entry.get("why") or "Rebase onto the entries it fails with, fix how the changes combine, "
                                             "and submit the result again.",
                         [["qk", "queue", "result", partner["id"], "--json"] for partner in partners]),
                "conflictWith": partners}
    if state == "failed":
        return advice("entry-failed", "session", False, "fix-failure",
                      entry.get("why") or "Inspect the result and fix the failure before submitting again.")
    if state == "removed":
        return advice("entry-removed", "session", False, "none", "The entry was removed from the queue.")
    return advice("entry-unknown-state", "unknown", False, "inspect-entry",
                  "This version does not recognize the entry's state.")


def for_queue(hold, *, working, waiting):
    """A queue hold or missing worker needs action; an ordinary running or idle queue does not."""
    if hold: return hold
    if waiting and not working:
        return advice("worker-not-running", "session", True, "start-worker",
                      "Entries are waiting without a running worker.", [["qk", "queue", "resume"]])
    return None
