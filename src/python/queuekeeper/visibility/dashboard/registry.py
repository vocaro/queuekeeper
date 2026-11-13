"""The repositories the dashboard shows: those whose queues have journaled an event while the feature was on, and
those `qk serve` was started in or given. They are kept as paths, beside the owner's settings, in
dashboard-repositories.json; one whose folder or queue is gone is left out when the list is read."""
import contextlib
import json
import os
from pathlib import Path

from ... import visibility

FILE = "dashboard-repositories.json"
QUEUE = "queuekeeper-queue"
NOTED = set()  # the queue directories this process has noted, so that each is written once


def path():
    return visibility.settings_path().parent / FILE


def stored():
    try: found = json.loads(path().read_text()).get("repositories", [])
    except (OSError, ValueError, AttributeError): return []
    return [entry for entry in found if isinstance(entry, str)] if isinstance(found, list) else []


def add(root):
    """Add the repository whose main checkout is root, when the list lacks it."""
    root, found = str(Path(root).resolve()), stored()
    if root in found: return
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    staged = target.with_suffix(f".{os.getpid()}.tmp")
    staged.write_text(json.dumps({"repositories": [*found, root]}, indent=2) + "\n")
    os.replace(staged, target)


def checkout_of(directory):
    """The main checkout of the repository whose queue is kept in directory, <checkout>/.git/queuekeeper-queue, or
    None for a queue kept anywhere else, such as a bare repository's."""
    common = Path(directory).parent
    return common.parent if common.name == ".git" and Path(directory).name == QUEUE else None


def note(directory, event):
    """Add the repository whose queue wrote event to the list, once a process (extensions.AFTER_EVENT)."""
    if directory in NOTED or visibility.switched_off(): return
    NOTED.add(directory)
    root = checkout_of(directory)
    if root is not None:
        with contextlib.suppress(OSError): add(root)


def known():
    """The main checkout of each listed repository that still has a queue, in the order listed."""
    return [Path(root) for root in stored() if (Path(root) / ".git" / QUEUE).is_dir()]
