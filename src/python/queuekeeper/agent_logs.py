"""Read recorded logs from their beginning in bounded, resumable UTF-8 chunks.

Offsets count bytes, not characters. A cursor pins the selected file and the bytes last returned,
so replacing or truncating a log requires an explicit restart rather than silently skipping output.
No command here tails a file or contacts a remote host.
"""
import base64
import codecs
import hashlib
import json
import os
from pathlib import Path
import stat
import sys

VERSION = 1
DEFAULT_LIMIT = 16384
MAX_LIMIT = 1 << 20
LOG_KINDS = ("run-log", "dispatch-log", "queue-log")


class LogError(ValueError):
    def __init__(self, code, message, status=1):
        super().__init__(message)
        self.code, self.status = code, status


def encode_cursor(value):
    return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).decode().rstrip("=")


def decode_cursor(value):
    try:
        if not isinstance(value, str) or len(value) > 8192: raise ValueError()
        found = json.loads(base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True))
        if not isinstance(found, dict) or found.get("v") != VERSION or not isinstance(found.get("path"), str):
            raise ValueError()
        for key in ("device", "inode", "offset", "size", "guardStart"):
            if type(found.get(key)) is not int or found[key] < 0: raise ValueError()
        if not 0 <= found["guardStart"] <= found["offset"] <= found["size"]: raise ValueError()
        if found["offset"] - found["guardStart"] > MAX_LIMIT: raise ValueError()
        if not isinstance(found.get("guard"), str) or len(found["guard"]) != 64: raise ValueError()
        return found
    except (ValueError, UnicodeError, TypeError):
        raise LogError("invalid-log-cursor", "Invalid log cursor; start again at --offset 0", 2) from None


def selected(artifacts, selector=None, cursor=None):
    logs = [item for item in artifacts if item.get("kind") in LOG_KINDS]
    if cursor is not None:
        path = decode_cursor(cursor)["path"]
        logs = [item for item in logs if item["path"] == path]
        if not logs:
            raise LogError("log-cursor-stale", "The cursor's log is no longer recorded for this entry; select a log and restart")
    if selector is not None:
        logs = [item for item in logs if selector in (item["kind"], item["path"])]
        if not logs: raise LogError("unknown-log-artifact", "No recorded log matches --artifact; use a listed kind or full path", 2)
        if len(logs) > 1:
            raise LogError("ambiguous-log-artifact", "More than one log has that kind; select its full path with --artifact", 2)
    if not logs: raise LogError("log-not-recorded", "This entry has no recorded log yet")
    current = [item for item in logs if item.get("current")]
    if current: return current[-1]
    dispatch = [item for item in logs if item["kind"] == "run-log"]
    if dispatch: return dispatch[-1]
    primary = [item for item in logs if item.get("primary")]
    if primary: return primary[-1]
    queue_logs = [item for item in logs if item["kind"] == "queue-log"]
    return queue_logs[-1] if queue_logs else logs[-1]


def splits_character(stream, offset):
    """Whether an explicit offset is inside a valid UTF-8 sequence, rather than bad log bytes."""
    start = max(0, offset - 3)
    stream.seek(start)
    window = stream.read(offset - start + 3)
    boundary = offset - start
    for index, byte in enumerate(window[:boundary]):
        length = 2 if 0xC2 <= byte <= 0xDF else 3 if 0xE0 <= byte <= 0xEF else 4 if 0xF0 <= byte <= 0xF4 else 0
        if not index < boundary < index + length <= len(window): continue
        try: window[index:index + length].decode("utf-8")
        except UnicodeDecodeError: continue
        return True
    return False


def chunk(artifact, *, offset=0, limit=DEFAULT_LIMIT, cursor=None, complete=False):
    """Return one chunk without advancing past a split UTF-8 character or a changed log."""
    if type(offset) is not int or offset < 0:
        raise LogError("invalid-log-offset", "--offset must be a nonnegative byte offset", 2)
    if type(limit) is not int or not 4 <= limit <= MAX_LIMIT:
        raise LogError("invalid-log-limit", f"--limit must be between 4 and {MAX_LIMIT} bytes", 2)
    prior = decode_cursor(cursor) if cursor is not None else None
    if prior is not None:
        if offset != 0:
            raise LogError("invalid-log-offset", "Use --cursor or --offset, not both", 2)
        offset = prior["offset"]
        if prior["path"] != artifact["path"]:
            raise LogError("log-cursor-stale", "The cursor identifies another log; select it or restart at --offset 0")
    if artifact.get("location", "local") != "local":
        raise LogError("log-unavailable", "The recorded log is remote and has not been returned locally")
    path = Path(artifact["path"])
    try:
        if not stat.S_ISREG(path.stat().st_mode):
            raise LogError("log-unavailable", "The recorded log is not a regular file")
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise LogError("log-unavailable", "The recorded log is not a regular file")
            size = before.st_size
            if prior is not None:
                if (before.st_dev, before.st_ino) != (prior["device"], prior["inode"]) or size < prior["size"]:
                    raise LogError("log-cursor-stale", "The log was replaced or truncated; restart at --offset 0")
                stream.seek(prior["guardStart"])
                guard = stream.read(offset - prior["guardStart"])
                if hashlib.sha256(guard).hexdigest() != prior["guard"]:
                    raise LogError("log-cursor-stale", "Previously returned log bytes changed; restart at --offset 0")
            if offset > size:
                raise LogError("log-offset-past-end", "The byte offset is past the log's end; restart at --offset 0", 2)
            stream.seek(offset)
            raw = stream.read(limit)
            if prior is None and offset and raw and 0x80 <= raw[0] <= 0xBF and splits_character(stream, offset):
                raise LogError("invalid-log-offset", "The byte offset splits a UTF-8 character; use nextOffset or nextCursor", 2)
            decoder = codecs.getincrementaldecoder("utf-8")("replace")
            final = complete and offset + len(raw) >= size
            content = decoder.decode(raw, final=final)
            pending = decoder.getstate()[0]
            consumed = len(raw) - len(pending)
            next_offset = offset + consumed
            after = os.fstat(stream.fileno())
            # The pathname may have been rotated while the old descriptor remained readable.
            current = path.stat()
            if after.st_size < size or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
                raise LogError("log-cursor-stale", "The log changed while reading it; restart at --offset 0")
            next_cursor = encode_cursor({"v": VERSION, "path": artifact["path"], "device": before.st_dev,
                                         "inode": before.st_ino, "offset": next_offset, "size": after.st_size,
                                         "guardStart": offset, "guard": hashlib.sha256(raw[:consumed]).hexdigest()})
    except LogError:
        raise
    except OSError as error:
        raise LogError("log-unavailable", f"Cannot read the recorded log: {error}") from None
    return {"offset": offset, "nextOffset": next_offset, "limit": limit, "sizeBytes": after.st_size,
            "content": content, "encoding": "utf-8", "hasMore": next_offset < after.st_size,
            "truncated": next_offset < after.st_size, "pendingBytes": len(pending),
            "waitingForBytes": bool(pending) and offset + len(raw) >= after.st_size, "nextCursor": next_cursor}


def main(root, args):
    from . import agent_results, merge_queue

    artifacts, chosen = [], None
    payload = {"schemaVersion": VERSION, "operation": "queue.logs", "id": args.id}
    try:
        queue = merge_queue.Queue(root)
        entry = agent_results.read_entry(queue, args.id)
        payload.update(state=entry["state"], terminal=entry["state"] not in agent_results.WAITING)
        artifacts, _ = agent_results.entry_artifacts(queue, entry)
        logs = [item for item in artifacts if item.get("kind") in LOG_KINDS]
        chosen = selected(logs, getattr(args, "artifact", None), getattr(args, "cursor", None))
        payload.update(chunk(chosen, offset=args.offset, limit=args.limit, cursor=getattr(args, "cursor", None),
                             complete=entry["state"] not in agent_results.WAITING))
        payload.update(available=True, reasonCode=None)
        status = 0
    except (LogError, OSError, ValueError) as error:
        status = error.status if isinstance(error, LogError) else 2
        payload.update(available=False, reasonCode=error.code if isinstance(error, LogError) else "usage-or-configuration-error",
                       message=str(error), content="", nextOffset=None, nextCursor=None, hasMore=False, truncated=False)
    payload.update(exitStatus=status, artifact=chosen,
                   artifacts=[item for item in artifacts if item.get("kind") in LOG_KINDS])
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2))
    elif status:
        print(f"queuekeeper: {payload['message']}", file=sys.stderr)
    else:
        print(payload["content"], end="")
        print(f"\nqueuekeeper: nextOffset={payload['nextOffset']} hasMore={str(payload['hasMore']).lower()} "
              f"nextCursor={payload['nextCursor']}", file=sys.stderr)
    return status
