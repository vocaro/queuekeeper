"""`qk host editors`: the Unity editors open on this Mac and the versions installed on it.

Whether an editor is in Play mode is not read: only the editor itself knows, and it says so through a project's own
tooling, not to another process."""
import json
import re

from .. import extensions
from . import editor, project

PROJECT_PATH = re.compile(r"-projectPath (.+?)(?= -[A-Za-z]|$)", re.I)


def reading():
    """{"editors": [{pid, project, version, batch}], "installed": [version]} for this Mac."""
    found = []
    for pid, command in project.editors():
        named = PROJECT_PATH.search(command)
        version = re.search(r"/Editor/([^/]+)/" + re.escape(editor.BINARY), command)
        found.append({"pid": pid, "project": named.group(1).strip() if named else None,
                      "version": version.group(1) if version else None, "batch": "-batchmode" in command.lower()})
    try: installed = sorted((entry.name for entry in editor.HUB.iterdir()
                             if (entry / editor.BINARY).exists()), key=editor.order)
    except OSError: installed = []
    return {"editors": found, "installed": installed}


def arguments(parser):
    parser.add_argument("--json", action="store_true", help="print the reading as JSON")


def read(args, out=print):
    """Print this Mac's editors and exit 0, or 2 when its processes cannot be listed."""
    try: found = reading()
    except project.Unreadable as error:
        out(f"queuekeeper: {error}")
        return 2
    if args.json:
        out(json.dumps(found, indent=2))
        return 0
    out(f"installed: {', '.join(found['installed']) or 'none'}")
    if not found["editors"]: out("no Unity editor is running")
    for entry in found["editors"]:
        kind = "batch run" if entry["batch"] else "editor"
        out(f"PID {entry['pid']}: {kind}, Unity {entry['version'] or '?'}, on {entry['project'] or 'no project named'}")
    return 0


def register():
    """Add the reading to `qk host`."""
    extensions.HOST_READS["editors"] = (
        "list the Unity editors and batch runs on this Mac, each with its project and version, and the editor "
        "versions installed", arguments, read)
