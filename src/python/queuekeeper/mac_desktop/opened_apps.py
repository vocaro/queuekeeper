"""The apps a foreground run opened on the second Mac, quit before it releases the screen (vocaro/queuekeeper#104).

On 2026-11-12 an EPUBLib `voiceover-mac` run that opened Safari for a MathML comparison released the second Mac's
foreground with Safari's window still in front; 6 of the next 7 foreground runs met it, and a Mac VoiceOver session
read it into its transcript (vocaro/EPUBLib#1034). So foreground.slot reads the regular apps, those with a Dock icon,
running once it holds the foreground, and again before it releases it. An app that started in between is the run's
to quit when nothing else could have started it: the second Mac is no workstation, and the run holds its whole
foreground, so an app installed in an Applications folder, or built in the run's own checkout, or one the session
recorded as launched (`devices update mac-foreground --launched BUNDLE`), is the run's. queuekeeper asks each to quit,
as the Dock's Quit does, waits up to QUIT_WAIT seconds for it to end, and never forces it.

Any other new app is reported and left running: one built somewhere else, such as a gate's test host in its own
checkout or in DerivedData, may be another run's, since background lanes run beside a foreground one; one whose
bundle a copy ran before the run is the owner's to have; and macOS's own, in /System/Library/CoreServices, is not
the run's to quit. An app running when the run took the foreground is never touched.
"""
import json
import os
from pathlib import Path
import subprocess
import time

from .. import offload_host

GUIDE = "guides/foreground.md"
SECTION = "Apps a run opened"
# "off" reads no running apps, so that nothing is quit: the test suite sets it for every run it makes, in its own
# process and its children, so that no test quits an app on the Mac that runs it.
SWITCH = "QUEUEKEEPER_QUIT_OPENED_APPS"
# Seconds queuekeeper waits for the apps it asked to quit to end, all at once, before it reports those still running.
QUIT_WAIT = 30
STEP = 0.5
# macOS's own apps, such as Finder, Dock and Problem Reporter, which a run never quits.
SYSTEM = "/System/Library/CoreServices/"
# The folders an installed app runs from, Safari from its cryptex. An app nested in another, such as Xcode's Device
# Hub, is not installed there in this sense: a background simulator lane may have opened it.
APPLICATIONS = ("/Applications", "/System/Applications", "/System/Volumes/Preboot/Cryptexes/App/System/Applications")

run = offload_host.output

# The regular apps running, each with its process, name, bundle identifier and bundle path, through AppKit.
LIST_SCRIPT = """ObjC.import('AppKit');
var found = [];
var apps = $.NSWorkspace.sharedWorkspace.runningApplications;
for (var i = 0; i < apps.count; i++) {
  var app = apps.objectAtIndex(i);
  if (app.activationPolicy != 0) continue;
  var url = app.bundleURL;
  found.push({pid: app.processIdentifier, name: ObjC.unwrap(app.localizedName) || null,
              bundle: ObjC.unwrap(app.bundleIdentifier) || null, path: url.isNil() ? null : ObjC.unwrap(url.path)});
}
JSON.stringify(found);"""
# Ask one app, by its process, to quit, as the Dock's Quit does: an app with unsaved work may ask first.
QUIT_SCRIPT = """ObjC.import('AppKit');
var app = $.NSRunningApplication.runningApplicationWithProcessIdentifier(%(pid)d);
app.isNil() ? 'gone' : (app.terminate ? 'asked' : 'refused');"""


def read():
    """The regular apps running, each {"pid", "name", "bundle", "path"}, or None when they cannot be read."""
    if os.environ.get(SWITCH) == "off": return []
    found = run(["osascript", "-l", "JavaScript", "-e", LIST_SCRIPT])
    if found is None: return None
    try: listed = json.loads(found)
    except ValueError: return None
    if not isinstance(listed, list): return None
    apps = []
    for app in listed:
        try: apps.append({"pid": int(app["pid"]), "name": app.get("name"), "bundle": app.get("bundle"),
                          "path": app.get("path")})
        except (KeyError, TypeError, ValueError, AttributeError): continue
    return apps


def checkout(directory=None):
    """The top of the git checkout the run works in, whose build products are its own, or None outside one."""
    try:
        done = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=directory or os.getcwd(),
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    top = done.stdout.strip()
    return os.path.realpath(top) if done.returncode == 0 and top else None


def describe(app):
    return f"{app.get('name') or '?'} ({app.get('bundle') or 'no bundle identifier'}, pid {app['pid']})"


def installed(path):
    """Whether the bundle at path is an app installed in an Applications folder, not nested in another app."""
    parent = Path(path).parent
    if parent.name == "Utilities": parent = parent.parent
    folders = {*APPLICATIONS, str(Path.home() / "Applications")}
    return Path(path).suffix == ".app" and str(parent) in folders


def within(path, directory):
    path, directory = os.path.realpath(path), os.path.realpath(directory)
    return path == directory or path.startswith(directory.rstrip("/") + "/")


def why_kept(app, before, own, launched=()):
    """None when the new app is the run's to quit; otherwise why it is left running."""
    if app.get("bundle") and app["bundle"] in launched: return None
    path = app.get("path")
    if not path: return "its bundle cannot be read, so whose it is is unknown"
    if path.startswith(SYSTEM): return "it is macOS's own"
    if app.get("bundle") and app["bundle"] in {other.get("bundle") for other in before}:
        return "a copy of it ran before the run took the foreground"
    if installed(path) or (own and within(path, own)): return None
    return ("it is neither installed in an Applications folder nor built in the run's checkout, so it may be another "
            "run's, such as a gate's test host")


def alive(pid):
    try: os.kill(pid, 0)
    except ProcessLookupError: return False
    except PermissionError: return True
    return True


def ask(app):
    """Ask the app to quit: True once asked, or when it had ended already."""
    return run(["osascript", "-l", "JavaScript", "-e", QUIT_SCRIPT % {"pid": app["pid"]}]) in ("asked", "gone")


def settle(before, own, *, said, launched=(), wait=None, step=None, sleep=time.sleep, ending=None):
    """Quit the regular apps that started since before, the apps running when the run took the foreground, that
    are the run's (why_kept), and say what was quit and what is left: {"quit": [...], "left": [...]}, each a
    description, a left one with why, or None when nothing is new. Unreadable apps, now or before, quit nothing."""
    ending, wait, step = ending or alive, QUIT_WAIT if wait is None else wait, STEP if step is None else step
    now = read()
    if before is None or now is None:
        said(f"queuekeeper: could not read the second Mac's running apps {'before' if before is None else 'after'} "
             f"the run, so it quit none it may have opened ({GUIDE}, {SECTION})")
        return {"quit": [], "left": ["the running apps could not be read"]}
    known = {app["pid"] for app in before}
    new = [app for app in now if app["pid"] not in known and app["pid"] != os.getpid()]
    if not new: return None
    left, asked = [], []
    for app in new:
        reason = why_kept(app, before, own, launched)
        if reason is None and not ask(app): reason = "it could not be asked to quit"
        if reason is None: asked.append(app)
        else: left.append(f"{describe(app)}: {reason}")
    limit = time.monotonic() + wait
    running = [app for app in asked if ending(app["pid"])]
    while running and time.monotonic() < limit:
        sleep(step)
        running = [app for app in running if ending(app["pid"])]
    quit_ = [describe(app) for app in asked if app not in running]
    left += [f"{describe(app)}: it was asked to quit and still ran after {wait:g}s, perhaps asking to save"
             for app in running]
    for line in quit_: said(f"queuekeeper: quit {line}, which the run opened")
    for line in left: said(f"queuekeeper: left running {line} ({GUIDE}, {SECTION})")
    return {"quit": quit_, "left": left}


def summary(settled):
    """The release's words for what settle did, or None when no app was new."""
    if settled is None: return None
    parts = []
    if settled["quit"]: parts.append("quit " + "; ".join(settled["quit"]))
    if settled["left"]: parts.append("left running " + "; ".join(settled["left"]))
    return "apps the run opened: " + ", and ".join(parts)
