"""What Unity's own command, `unity`, says of this Mac: whether Unity Hub is signed in, and whether an open editor
is in Play mode.

Unity Hub 3.21 and later installs the command at ~/.unity/bin/unity. `unity doctor` reads the Hub's sign-in.
`unity command editor_status` asks one editor, through the Pipeline package (com.unity.pipeline) that the editor's
project lists, and so answers only for a project that has that package and an editor open on it.

Every reading here is None when it could not be made: no command on this Mac, a project without the package, a
keychain the command may not read, a reply in a form not known. None is never taken for signed out or for stopped.
The reply of `editor_status` was not read from a live editor when this was written (2026-11-12, command
1.0.0-beta.13): its "playMode" is looked for anywhere in the reply."""
import json
import os
from pathlib import Path
import shutil
import subprocess

# The variable that names the command outright.
VARIABLE = "UNITY_CLI"
INSTALLED = Path.home() / ".unity/bin/unity"
PLAYING, PAUSED, STOPPED = "playing", "paused", "stopped"


def binary(environ=None):
    """The `unity` command on this Mac, or None."""
    environ = os.environ if environ is None else environ
    for named in (environ.get(VARIABLE), INSTALLED, shutil.which("unity")):
        if named and os.access(named, os.X_OK): return Path(named)
    return None


def asked(words, timeout=20):
    """The reply to `unity WORDS --format json`, or None when there is no command or no reply that is JSON. The
    command exits nonzero with a reply too, as when no editor answers."""
    command = binary()
    if command is None: return None
    try:
        ran = subprocess.run([str(command), *words, "--format", "json"], stdin=subprocess.DEVNULL,
                             capture_output=True, text=True, timeout=timeout)
        reply = json.loads(ran.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return reply if isinstance(reply, dict) else None


def signed_in():
    """Whether Unity Hub is signed in on this Mac: True, False, or None when that could not be read, as in a
    session that may not read the keychain."""
    auth = ((asked(["doctor"]) or {}).get("data") or {}).get("auth")
    if not isinstance(auth, dict) or auth.get("sessionState") == "unreadable": return None
    signed = auth.get("loggedIn")
    return signed if isinstance(signed, bool) else None


def found(reply, key):
    """The first value under key anywhere in reply, which may hold its result as JSON in a string."""
    if isinstance(reply, str) and reply.lstrip()[:1] in ("{", "["):
        try: reply = json.loads(reply)
        except ValueError: return None
    if isinstance(reply, dict):
        if key in reply: return reply[key]
        reply = list(reply.values())
    if isinstance(reply, list):
        for value in reply:
            value = found(value, key)
            if value is not None: return value
    return None


def play_mode(project):
    """PLAYING, PAUSED or STOPPED for the editor open on project, or None when it could not be asked."""
    reply = asked(["command", "editor_status", "--project-path", str(project)], timeout=10)
    if not reply or reply.get("success") is False: return None
    mode = found(reply.get("data"), "playMode")
    mode = mode.lower() if isinstance(mode, str) else None
    return mode if mode in (PLAYING, PAUSED, STOPPED) else None
