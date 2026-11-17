"""The Logs tab: the queue's logs, newest first, each named by its round, commit and lane with how its run ended, and
one of them read in chunks of whole lines with an index of where its failures, suites and heartbeats are.

Only a file in the queue's own logs folder, or its worker.log, is read, by a name with no folder in it."""
import bisect
import os
import re
import stat

from queuekeeper import farm

TITLE, ORDER = "Logs", 100
MOST, TAIL, CHUNK, BACK = 100, 64 << 10, 256 << 10, 8 << 10
WORKER = "worker.log"
INDEXED = 32 << 20  # the largest log that is indexed; a larger one is read without line numbers or marks
MARKS, MATCHES, KEPT = 5000, 2000, 16
REASON = 300  # the characters of a failure's reason that are kept

# A queue log's name: when its round began, the commit it tested, and its lane, a rerun's being rerun-TEST-TRY.
NAMED = re.compile(r"(\d{8}-\d{6})-([0-9a-f]{7,40})-(.+)\.log")
RERUN = re.compile(r"rerun-(\d+)-(\d+)")
FAILED = re.compile(rb"(FAIL|ERROR): \S+ \(([\w.]+)\)")
SUITE = re.compile(rb"== (.+?): (OK|FAILED)\b")
SUMMARY = re.compile(rb"\s*\S+ unittest: \d+ tests\b")
EXCEPTION = re.compile(rb"[A-Za-z_][\w.]*(Error|Exception|Failure|Interrupt|Exit)\b")
HEARTBEAT = b"remote: heartbeat "

CACHE = {}  # path -> ((size, mtime), index), at most KEPT of them
FOUND = {}  # (path, size, mtime, text) -> matches, at most KEPT of them


def listing(repository):
    found = []
    try: names = os.listdir(repository.logs)
    except OSError: names = []
    for name in names:
        try: status = os.lstat(repository.logs / name)
        except OSError: continue
        if stat.S_ISREG(status.st_mode) and not name.startswith("."):
            found.append({"name": name, "size": status.st_size, "modified": status.st_mtime})
    found = sorted(found, key=lambda log: log["modified"], reverse=True)[:MOST]
    try:
        status = os.lstat(repository.directory / WORKER)
        if stat.S_ISREG(status.st_mode): found.insert(0, {"name": WORKER, "size": status.st_size, "modified": status.st_mtime})
    except OSError: pass
    return found


def described(repository, found):
    """found, each log with what its name and the journal say of it: its round, commit and lane, and, for a lane
    run the journal saw end, its status, seconds and Mac (None for this Mac), and running for one it saw begin and
    not end; a rerun says which failed test it
    reran and which try it was, and whether it passed by its own summary line."""
    ended, began = {}, set()
    for event in repository.events():
        if event["kind"] == "gate.ended" and isinstance(event.get("log"), str): ended[os.path.basename(event["log"])] = event
        if event["kind"] in ("gate.began", "gate.ended"):
            (began.add if event["kind"] == "gate.began" else began.discard)(f"{event.get('round')}-{event.get('lane')}.log")
    if ended:
        from . import load
        try: names = {mac["ssh"]: mac["name"] for mac in farm.read()}
        except Exception:  # noqa: BLE001 - a farm list that cannot be read leaves each Mac as the journal names it
            names = {}
        mac_name = load("history").mac_name
    for log in found:
        named = NAMED.fullmatch(log["name"])
        if not named: continue
        log.update(round=named[1], commit=named[2], lane=named[3])
        rerun = RERUN.fullmatch(named[3])
        if rerun:
            log.update(lane="rerun", test=int(rerun[1]), attempt=int(rerun[2]))
            summary = (index(repository.logs / log["name"], log["size"]) or {}).get("summary")
            if summary: log["status"] = 0 if summary.rstrip().endswith(("OK", "OK (command exit 0)")) else 1
        if log["name"] in began: log["running"] = True
        event = ended.get(log["name"])
        if event:
            log.update(status=event.get("status"), seconds=event.get("seconds"), mac=mac_name(event.get("mac"), names),
                       ended=event.get("time"))
    return found


def path_of(repository, name):
    """The log named name, or a LookupError for a name that is none of the queue's logs."""
    if not name or os.path.basename(name) != name or name.startswith("."): raise LookupError(f"{name!r} names no log")
    path = repository.directory / WORKER if name == WORKER else repository.logs / name
    try: status = os.lstat(path)
    except OSError: raise LookupError(f"the queue has no log named {name}") from None
    if not stat.S_ISREG(status.st_mode): raise LookupError(f"{name} is not a log file")
    return path, status.st_size


def remember(cache, key, value):
    if len(cache) >= KEPT: cache.pop(next(iter(cache)))
    cache[key] = value
    return value


def index(path, size):
    """Where each line of the log at path starts, and its marks, or None for a log larger than INDEXED: each
    failing test with its line, offset and the first line of its reason; each suite's result, heartbeat and
    summary as [line, kind]; and the last summary line. It is read again only when the log has changed."""
    if size > INDEXED: return None
    try: mark = (os.stat(path).st_size, os.stat(path).st_mtime_ns)
    except OSError: return None
    kept = CACHE.get(path)
    if kept and kept[0] == mark: return kept[1]
    starts, marks, failures, summary, offset, waiting = [], [], [], None, 0, None
    with open(path, "rb") as stream:
        for number, line in enumerate(stream, 1):
            starts.append(offset)
            if waiting is not None and EXCEPTION.match(line) and "reason" not in waiting:
                waiting["reason"] = line.decode(errors="replace").rstrip()[:REASON]
            if (failed := FAILED.match(line)):
                waiting = {"test": failed[2].decode(errors="replace"), "line": number, "offset": offset}
                failures.append(waiting)
                marks.append([number, "fail"])
            elif (suite := SUITE.match(line)): marks.append([number, "pass" if suite[2] == b"OK" else "failed"])
            elif line.startswith(HEARTBEAT): marks.append([number, "beat"])
            elif SUMMARY.match(line):
                summary = line.decode(errors="replace").strip()
                marks.append([number, "summary"])
            offset += len(line)
    found = {"starts": starts, "lines": len(starts), "marks": marks[-MARKS:], "failures": failures[:MARKS],
             "summary": summary, "size": offset}
    return remember(CACHE, path, (mark, found))[1]


def line_start(found, offset):
    """The start of the line holding offset, and its number from 1, by the index found; offset itself and None
    without one."""
    if not found or not found["starts"]: return offset, (1 if offset == 0 else None)
    at = max(0, bisect.bisect_right(found["starts"], offset) - 1)
    return found["starts"][at], at + 1


def aligned(found, offset):
    """offset moved back to the start of its line, unless that line starts more than BACK before it."""
    start = line_start(found, offset)[0]
    return start if offset - start <= BACK else offset


def matches(path, size, text, found):
    """Each line of the log holding text, ignoring case, as [line, offset], at most MATCHES of them, and how many
    there are."""
    key = (path, size, text.lower())
    if key in FOUND: return FOUND[key]
    wanted, lines, count = text.lower().encode(), [], 0
    with open(path, "rb") as stream:
        offset = 0
        for number, line in enumerate(stream, 1):
            if wanted in line.lower():
                count += 1
                if len(lines) < MATCHES: lines.append([number, offset])
            offset += len(line)
    return remember(FOUND, key, {"text": text, "lines": lines, "count": count})


def number(query, key):
    try: return int(query[key])
    except ValueError: raise LookupError(f"{key} is a number" + ("" if key == "line" else " of bytes")) from None


def data(repository, query):
    """The logs; and with name, a chunk of that log. The chunk starts at offset when given, ends at before, starts
    BACK before at or before the start of line number line, or is otherwise the log's last TAIL bytes. All but
    offset start where a line does, unless that line is longer than BACK, and a chunk cut at CHUNK ends where a
    line does. With the log's index unless known gives the size the page already has it for, with find's matches,
    and with ask echoed so the page can tell which of its requests this answers."""
    logs = described(repository, listing(repository))
    name = query.get("name")
    if not name: return {"logs": logs}
    path, size = path_of(repository, name)
    found = index(path, size)
    end = size
    if "offset" in query: offset = min(max(0, number(query, "offset")), size)
    elif "before" in query:
        end = min(max(0, number(query, "before")), size)
        offset = aligned(found, max(0, end - CHUNK))
        if offset == end and end: offset = aligned(found, end - 1)
    elif "at" in query: offset = aligned(found, max(0, min(number(query, "at"), size) - BACK))
    elif "line" in query and found and found["starts"]:
        at = found["starts"][min(max(1, number(query, "line")), len(found["starts"])) - 1]
        offset = aligned(found, max(0, at - BACK))
    else:
        offset = aligned(found, max(0, size - TAIL))
    with open(path, "rb") as stream:
        stream.seek(offset)
        chunk = stream.read(min(CHUNK, end - offset))
    if end == size and len(chunk) == CHUNK and b"\n" in chunk[:-1]: chunk = chunk[:chunk.rindex(b"\n") + 1]
    answer = {"name": name, "size": size, "offset": offset, "next": offset + len(chunk), "line": line_start(found, offset)[1],
              "text": chunk.decode(errors="replace"), "path": str(path), "ask": query.get("ask"), "logs": logs,
              "log": next((log for log in logs if log["name"] == name), {"name": name, "size": size}), "reruns": [], "flaky": {}}
    if found and query.get("known") != str(found["size"]):
        answer["index"] = {key: found[key] for key in ("lines", "marks", "failures", "summary", "size")}
    if query.get("find"): answer["found"] = matches(path, size, query["find"][:200], found)
    log = answer["log"]
    if log.get("round"):
        answer["reruns"] = [other for other in logs if other.get("lane") == "rerun" and other.get("round") == log["round"]
                            and other.get("commit") == log["commit"]]
        answer["flaky"] = {event["test"]: event.get("issue") for event in repository.events() if event["kind"] == "test.flaky"
                           and isinstance(event.get("test"), str) and str(event.get("commit", "")).startswith(log["commit"])}
    return answer
