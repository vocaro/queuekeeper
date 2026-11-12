"""The Logs tab: the queue's logs, newest first, and one of them read from an offset so that it can be followed.

Only a file in the queue's own logs folder, or its worker.log, is read, by a name with no folder in it."""
import os
import stat

TITLE, ORDER = "Logs", 30
MOST, TAIL, CHUNK = 60, 64 << 10, 256 << 10
WORKER = "worker.log"


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


def path_of(repository, name):
    """The log named name, or a LookupError for a name that is none of the queue's logs."""
    if not name or os.path.basename(name) != name or name.startswith("."): raise LookupError(f"{name!r} names no log")
    path = repository.directory / WORKER if name == WORKER else repository.logs / name
    try: status = os.lstat(path)
    except OSError: raise LookupError(f"the queue has no log named {name}") from None
    if not stat.S_ISREG(status.st_mode): raise LookupError(f"{name} is not a log file")
    return path, status.st_size


def data(repository, query):
    name = query.get("name")
    if not name: return {"logs": listing(repository)}
    path, size = path_of(repository, name)
    try: offset = int(query["offset"]) if "offset" in query else max(0, size - TAIL)
    except ValueError: raise LookupError("offset is a number of bytes") from None
    offset = min(max(0, offset), size)
    with open(path, "rb") as stream:
        stream.seek(offset)
        chunk = stream.read(CHUNK)
    return {"name": name, "size": size, "offset": offset, "next": offset + len(chunk),
            "text": chunk.decode(errors="replace"), "logs": listing(repository)}
