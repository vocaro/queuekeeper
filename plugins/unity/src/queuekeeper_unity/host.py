"""`qk host editors`: the Unity editors open on this Mac, each one's Play state, and the versions installed on it.

Only an editor itself knows whether it is in Play mode. It is asked through Unity's own command (unity_cli), which
answers for a project that lists the Pipeline package. An editor that cannot be asked has no Play state here, which
is not the same as stopped."""
import json
import re

from queuekeeper import extensions
from . import editor, project, unity_cli

PROJECT_PATH = re.compile(r"-projectPath (.+?)(?= -[A-Za-z]|$)", re.I)


def running():
    """Each Unity editor and batch run on this Mac, as {pid, project, version, batch}, and for an editor with a
    window and a project, "play": its Play state, or None when it could not be asked."""
    found = []
    for pid, command in project.editors():
        named = PROJECT_PATH.search(command)
        version = re.search(r"/Editor/([^/]+)/" + re.escape(editor.BINARY), command)
        entry = {"pid": pid, "project": named.group(1).strip() if named else None,
                 "version": version.group(1) if version else None, "batch": "-batchmode" in command.lower()}
        if entry["project"] and not entry["batch"]: entry["play"] = unity_cli.play_mode(entry["project"])
        found.append(entry)
    return found


def in_play():
    """The editors on this Mac that are in Play mode, on any project: each keeps simulating, and using the GPU,
    while it is unfocused."""
    return [entry for entry in running() if entry.get("play") == unity_cli.PLAYING]


def reading():
    """{"editors": [{pid, project, version, batch, play}], "installed": [version]} for this Mac."""
    found = running()
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
        play = {unity_cli.PLAYING: ", in Play mode", unity_cli.PAUSED: ", paused in Play mode"}.get(entry.get("play"), "")
        out(f"PID {entry['pid']}: {kind}, Unity {entry['version'] or '?'}, on {entry['project'] or 'no project named'}{play}")
    return 0


def register():
    """Add the reading to `qk host`."""
    extensions.HOST_READS["editors"] = (
        "list the Unity editors and batch runs on this Mac, each with its project, version and Play state, and "
        "the editor versions installed", arguments, read)
