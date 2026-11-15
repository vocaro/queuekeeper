"""One repository as the panels read it: its queue's snapshot and journal, each read at most once a moment."""
from pathlib import Path
import threading
import time

from queuekeeper import events, merge_queue

FRESH = 1.0  # seconds a snapshot serves every panel that asks, however many tabs poll
RECENT = 20


class Repository:
    def __init__(self, name, root):
        self.name, self.root = name, Path(root)
        self.lock = threading.Lock()
        self.read_at, self.state, self.journal_mark, self.journal = 0.0, None, None, []

    @property
    def directory(self):
        return self.root / ".git" / "queuekeeper-queue"

    @property
    def logs(self):
        return self.directory / "logs"

    def snapshot(self):
        with self.lock:
            if self.state is None or time.monotonic() - self.read_at > FRESH:
                self.state, self.read_at = merge_queue.snapshot(self.root, RECENT), time.monotonic()
            return self.state

    def events(self):
        """The journal's events, oldest first, read again only when its file has changed."""
        with self.lock:
            try: stat = events.path_of(self.directory).stat()
            except OSError: return self.journal
            mark = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
            if mark != self.journal_mark: self.journal_mark, self.journal = mark, events.read(self.directory)
            return self.journal


def named(roots):
    """A Repository for each of roots under a name of its own: its folder's, with a number after it for each
    later one of the same name."""
    found = {}
    for root in roots:
        name, number = Path(root).name, 2
        while name in found: name, number = f"{Path(root).name}-{number}", number + 1
        found[name] = Repository(name, root)
    return found
