"""One machine-readable result for queue mutations and waits, with progress on standard error.

The text commands remain the authority for their actions and exit statuses. This adapter projects their
recorded results through the same public schema as `queue result`, rather than parsing their output.
"""
import contextlib
import json
import subprocess
import sys


def execute(root, operation, command, *, reference=None, identifiers=(), details=None):
    from . import agent_results, merge_queue as queue, recovery, remote, extensions

    details = {} if details is None else details
    requested = list(dict.fromkeys(identifiers))
    payload = None
    with contextlib.redirect_stdout(sys.stderr):
        try:
            if requested:
                store = queue.Queue(root)
                for identifier in requested: agent_results.read_entry(store, identifier)
            status = command()
            target = details.get("root", root)
            ids = [details["id"]] if details.get("id") else requested
            store = queue.Queue(target)
            entries = [agent_results.read_entry(store, identifier) for identifier in ids]
            results = []
            for entry in entries:
                row = agent_results.entry_result(store, entry)
                if status == queue.resources.BUSY and entry["state"] in queue.WAITING:
                    row["recovery"] = recovery.for_entry(entry, recovery.read_hold(store.hold_file, store.held()),
                                                         limit_reached=not bool(store.held()))
                results.append(row)
            if len(results) == 1:
                payload = results[0]
            elif results:
                payload = {"schemaVersion": 1, "entries": results}
            else:
                commit = queue.text(target, "rev-parse", "--verify", f"{reference}^{{commit}}") if reference else None
                reason = details.get("refused")
                payload = {"schemaVersion": 1, "id": None, "commit": commit, "landedAs": None,
                           "state": "refused" if status == 1 else "waiting" if status == queue.resources.BUSY else "already_present",
                           "failure": {"category": "refused", "message": reason} if reason else None,
                           "artifacts": [], "terminal": status != queue.resources.BUSY,
                           "recovery": recovery.advice("submission-refused" if reason else "already-present",
                                                       "session" if reason else None, False, "fix-submission" if reason else "none",
                                                       reason or "The commit is already present on origin/main.")}
                if details.get("closed"): payload["recovery"] = recovery.for_closed(details["closed"], reason)
                if details.get("waitsFor"):
                    payload["waitsFor"] = details["waitsFor"]
                    held = recovery.read_hold(store.hold_file, store.held())
                    payload["recovery"] = held or recovery.advice(
                        "wait-limit", "session", True, "wait", "The wait ended before this submission could be queued.",
                        [["qk", "queue", "wait", *details["waitsFor"], "--json"]])
        except queue.Refused as error:
            status = 1
            print(f"queuekeeper: {error}", file=sys.stderr)
            payload = error_result("submission-refused", str(error), "refused")
            payload["waitsFor"] = list(error.waits_for)
            if error.closed: payload["recovery"] = recovery.for_closed(error.closed, str(error))
        except (remote.Lost, extensions.Unavailable) as error:
            status = queue.lane_results.UNMEASURED
            print(f"queuekeeper: {error}", file=sys.stderr)
            payload = error_result("unmeasured", str(error))
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            status = 2
            print(f"queuekeeper: {error}", file=sys.stderr)
            payload = error_result("usage-or-configuration-error", str(error))
    target = details.get("root", root)
    if reference and payload.get("commit") is None:
        with contextlib.suppress(OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            resolved = queue.git(target, "rev-parse", "--verify", f"{reference}^{{commit}}")
            if resolved.returncode == 0: payload["commit"] = resolved.stdout.strip()
    if payload.get("recovery"):
        payload["recovery"] = recovery.in_project(payload["recovery"], target)
    for row in payload.get("entries", []):
        if row.get("recovery"): row["recovery"] = recovery.in_project(row["recovery"], target)
    payload.update(schemaVersion=1, operation=operation, exitStatus=status)
    if requested:
        payload["requestedIds"] = requested
    print(json.dumps(payload, indent=2))
    return status


def error_result(code, message, state="error"):
    from . import recovery
    return {"schemaVersion": 1, "id": None, "commit": None, "state": state, "landedAs": None,
            "failure": {"category": code, "message": message}, "artifacts": [],
            "terminal": True,
            "recovery": recovery.advice(code, "session", False, "inspect-error", message)}
