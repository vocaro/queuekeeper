"""The Unity extension's disk items for `qk disk`: the folders the editor generates in each checkout of a
Unity project. Library/ is the import cache, several GB a worktree, which the editor rebuilds on its next open,
taking minutes; Temp/ and Logs/ are small. The core lists and removes them with its own (queuekeeper.disk)."""
import json
from pathlib import Path

from queuekeeper import config as configuration, disk, git_state
from queuekeeper.disk import Item, KEPT, cache_item
from . import settings

CATEGORY = "unity-generated"
CATEGORIES = {CATEGORY: "Unity projects' generated folders (Library, Temp, Logs)"}
FOLDERS = {"Library": "the import cache, which the editor rebuilds on its next open, in minutes",
           "Temp": "the editor's temporary files", "Logs": "the editor's logs"}
MODULE = __package__


def unity_project(checkout):
    """Where the Unity project is in a checkout whose queuekeeper.json switches this extension on, as a path
    relative to it; None for any other checkout."""
    try: found = json.loads((Path(checkout) / configuration.FILE).read_text())
    except (OSError, ValueError): return None
    if not isinstance(found, dict): return None
    listed = [entry.get("module") if isinstance(entry, dict) else entry for entry in found.get("extensions") or []]
    if MODULE not in listed: return None
    try: settings.check(found.get(settings.KEY, {}))
    except ValueError: return None
    return found.get(settings.KEY, {}).get("project", ".")


def projects(host):
    """Each Unity project folder in the projects' checkouts, linked worktrees among them. A worktree keeps the
    place its main checkout names."""
    found = []
    for checkout in disk.main_checkouts(host):
        place = unity_project(checkout)
        if place is None: continue
        worktrees = [Path(entry["worktree"]) for entry in git_state.worktrees(checkout)] or [checkout]
        found += [worktree / place for worktree in worktrees if (worktree / place / "ProjectSettings").is_dir()]
    return found


def items(host, usage, sizes, wanted):
    """The extension's items of the categories in wanted."""
    if CATEGORY not in wanted: return []
    found = []
    for project in projects(host):
        # An editor names its project, not the folders in it, and holds them for as long as it runs.
        editor = usage.naming(f"-projectPath {project}")
        for name, what in FOLDERS.items():
            folder = project / name
            if not folder.is_dir() or folder.is_symlink(): continue
            if editor:
                found.append(Item(CATEGORY, str(folder), KEPT, f"in use: PID {editor.pid} has the project open",
                                  sizes.of(folder)))
            else: found.append(cache_item(host, usage, sizes, CATEGORY, folder, what))
    return found


def register():
    """Add these items to `qk disk`."""
    disk.extend(CATEGORIES, items)
