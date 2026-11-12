"""Which Unity editor runs a project: the version its ProjectSettings/ProjectVersion.txt pins.

The pinned version comes first because opening a project in a newer editor is not a harmless mismatch: the editor
reimports the whole Library and rewrites the tracked ProjectVersion.txt to its own version. A newest-first pick once
chose a patch release installed beside the pinned one, and a test run left an editor upgrade in the working tree
(vocaro/mpm, 2026-09-28). A Mac that lacks the pinned version falls back to its newest, and the caller says so."""
import os
from pathlib import Path
import re
import subprocess

# Where Unity Hub installs editors, one folder per version.
HUB = Path("/Applications/Unity/Hub/Editor")
# An editor's binary below its version's folder, which is also how a running editor's command line names it.
BINARY = "Unity.app/Contents/MacOS/Unity"
VERSION = re.compile(r"^m_EditorVersion: (\S+)", re.M)
# The variable that names an editor's binary outright, before the hub is asked.
VARIABLE = "UNITY"


def pinned(project):
    """The editor version the project pins, or None when it names none."""
    try: found = VERSION.search((Path(project) / "ProjectSettings/ProjectVersion.txt").read_text())
    except OSError: return None
    return found.group(1) if found else None


def order(version):
    """A version's sort key, by which 6000.10.0f1 is newer than 6000.9.0f1."""
    return [(0, int(part), "") if part.isdigit() else (1, 0, part) for part in re.findall(r"\d+|\D+", version)]


def resolve(hub, want):
    """The binary of the editor want names when hub has it, else of hub's newest editor, else None. An install
    whose binary cannot be executed is passed over."""
    def binary(version):
        path = Path(hub) / version / BINARY
        return path if os.access(path, os.X_OK) else None
    if want and binary(want): return binary(want)
    try: installed = sorted((entry.name for entry in Path(hub).iterdir() if entry.is_dir()), key=order, reverse=True)
    except OSError: return None
    return next((binary(version) for version in installed if binary(version)), None)


def find(project, environ=None, hub=None):
    """(binary, where it came from, the pinned version) for the project's editor; the binary is None when this Mac
    has no editor. $UNITY, when it names an executable, is taken over the hub's."""
    environ = os.environ if environ is None else environ
    want = pinned(project)
    named = environ.get(VARIABLE)
    if named and os.access(named, os.X_OK): return Path(named), f"${VARIABLE}", want
    return resolve(HUB if hub is None else hub, want), "hub", want


def matches(binary, want):
    """Whether binary is the editor of the version want names; true when the project pins none."""
    return not want or want in Path(binary).parts


def described(binary, source, want):
    """The line that says which editor a run uses, and whether it is the pinned one."""
    if matches(binary, want): return f"editor {binary} (from {source}, matches the pinned {want or 'version'})"
    return f"WARNING: using {binary}, but the project pins {want}, which this Mac does not have"


def app(binary):
    """The application bundle that holds an editor's binary."""
    return Path(binary).parents[2]


def launch(binary, project):
    """Open a new editor window on project. It returns once the launch is issued, not once the editor is ready."""
    subprocess.run(["/usr/bin/open", "-n", str(app(binary)), "--args", "-projectPath", str(project)], check=True)
