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
    return {**detail, "suggestedCommands": [qualified(command, root) for command in detail.get("suggestedCommands", [])]}


def qualified(command, root):
    """A suggested command, with --project naming root when it is a Queuekeeper command that names no project."""
    command = list(command)
    if (command and command[0] == "qk" and not any(
            part == "--project" or part.startswith("--project=") for part in command[1:])):
        command[1:1] = ["--project", str(Path(root).resolve())]
    return command


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
    reopens it, or once the closure lapses at reopensAt, its deadline (vocaro/queuekeeper#286), which the refused
    session waits for with `qk queue closed`, exit 1 once the queue is open."""
    until = closure.get("until") if isinstance(closure, dict) else None
    return {**advice("queue-closed", "unknown", False, "wait-for-reopen", message, [["qk", "queue", "closed", "--json"]]),
            "closed": dict(closure), "reopensAt": until if isinstance(until, (int, float)) else None}


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


def for_entry(entry, held=None, *, limit_reached=False, unverified=False):
    """Recovery for a specific entry; a settled entry is unaffected by a later queue hold. unverified says a
    waiting entry was pushed without an answer, which the worker settles by a fetch of origin/main."""
    state = entry.get("state")
    identifier = entry["id"]
    if state in ("queued", "testing", "passed") and unverified:
        message = ("The entry was pushed but no answer came, so whether it landed is unknown. The worker's next "
                   "round fetches origin/main and lands it if main holds that push, or pushes or gates it again. "
                   "Do not submit it again.")
        if held:
            hold = for_hold(held) if isinstance(held, str) else dict(held)
            return advice("publication-unverified", hold["responsibleParty"], False, "inspect-hold",
                          f"{message} The queue is held: {hold['message']}", hold["suggestedCommands"])
        return advice("publication-unverified", "session", True, "wait", message,
                      [["qk", "queue", "wait", identifier, "--json"]])
    if state in ("queued", "testing", "passed"):
        if held: return for_hold(held) if isinstance(held, str) else dict(held)
        code = "wait-limit" if limit_reached else "entry-pending"
        message = ("The wait ended at its limit; the entry is still in the queue." if limit_reached else
                   "The entry is still in the queue; follow this entry rather than submitting it again.")
        return advice(code, "session", True, "wait", message,
                      [["qk", "queue", "wait", identifier, "--json"]])
    if state == "landed":
        return advice("entry-landed", None, False, "none", "The entry landed.")
    # A send-back's result names the failed tests or conflicting files, and its logs.
    if state == "conflict":
        return advice("entry-conflict", "session", False, "fix-conflict",
                      entry.get("why") or "Resolve the conflict and submit the corrected commits.",
                      [["qk", "queue", "result", identifier, "--json"]])
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
                      entry.get("why") or "Inspect the result and fix the failure before submitting again.",
                      [["qk", "queue", "result", identifier, "--json"]])
    if state == "removed":
        return advice("entry-removed", "session", False, "none", "The entry was removed from the queue"
                      + (f": {entry['removedWhy']}" if entry.get("removedWhy") else "."))
    return advice("entry-unknown-state", "unknown", False, "inspect-entry",
                  "This version does not recognize the entry's state.")


def for_queue(hold, *, working, waiting):
    """A queue hold or missing worker needs action; an ordinary running or idle queue does not."""
    if hold: return hold
    if waiting and not working:
        return advice("worker-not-running", "session", True, "start-worker",
                      "Entries are waiting without a running worker.", [["qk", "queue", "resume"]])
    return None


def action(command, message):
    return {"command": list(command), "message": message}


def next_action(entry, detail, root):
    """The single command an agent runs next about entry, as {"command", "message"}, given its recovery detail; None
    once nothing is left to do. A settled outcome nobody has confirmed is confirmed (`qk queue confirm`), which takes it
    out of `qk queue inbox`; one sent back is fixed first, as confirming it changes nothing about the fix."""
    state, identifier = entry.get("state"), entry["id"]
    found = None
    if state == "failed":
        found = action(["qk", "queue", "logs", identifier, "--json"], "Read the failing run's log, fix the failure, "
                       "commit, and land again with `qk land --wait --json`.")
    elif state == "conflict":
        found = action(["git", "pull", "--rebase", "origin", "main"], "Rebase onto origin/main, resolve the conflict, "
                       "commit, and land again with `qk land --wait --json`.")
    elif state in ("queued", "testing", "passed"):
        commands = (detail or {}).get("suggestedCommands") or [["qk", "queue", "wait", identifier, "--json"]]
        found = action(commands[0], (detail or {}).get("message") or "Follow the entry to its result.")
    elif state in ("landed", "removed") and not entry.get("confirmed"):
        found = action(["qk", "queue", "confirm", identifier, "--json"],
                       "Acknowledge the outcome" + (f" ({entry['removedWhy']})" if entry.get("removedWhy") else "")
                       + ", which takes it out of `qk queue inbox`.")
    if found: found["command"] = qualified(found["command"], root)
    return found
