"""macOS's crash-report alerts during a foreground run, on any Mac (vocaro-engineering#118).

An app that quits unexpectedly raises a "quit unexpectedly" alert in UserNotificationCenter that stays on the
screen over the windows a foreground UI run clicks, so its clicks reach the alert and the run fails, as EPUBLib's
toolbar runs did on another Mac on 2026-11-02 (macOS 27.2 26B123). The owner wants these alerts on between
runs, so a foreground run turns them off for itself alone: it keeps com.apple.CrashReporter's DialogType from
before the run in a record, sets it to none, and puts that exact value back however the run ends. A run whose queuekeeper
died keeps its record, and the next queuekeeper command on that Mac puts the value back (recover). Before the run takes
the screen and while it runs, queuekeeper dismisses any crash-report alert with Ignore or OK, never Reopen or Report, and
leaves every other system alert alone: a password, permission or keychain prompt is the owner's. One it cannot
clear before the run leaves the run unmeasured with nothing run; one met during a run that then fails leaves that
run unmeasured too, never failed. One it cleared during a run that then fails leaves it unmeasured when the alert
stood longer than one look and was for a process other than the app under test; a crash of the app under test,
or of a process queuekeeper cannot name, stays a failure (owner's decision, 2026-11-07, vocaro-engineering#226). The
receipt keeps each cleared alert's process, start and how long it stood (doc/guides/foreground.md,
Crash-report alerts).
"""
import contextlib
from datetime import datetime, timezone
import json
import os
import re
import subprocess
import sys
import threading
import time

from queuekeeper import lane_results, offload_host, resources, window_placement

DOMAIN, KEY = offload_host.CRASH_DIALOG
OFF = "none"
# Seconds between looks at the screen's system alerts while a run holds the foreground: the wait from one look's
# end to the next one's start, not their cadence. An alert came about 1 s after its app's crash on another Mac,
# and a look took under one (2026-11-03); under load a System Events look took 17 s and 30 s there (2026-11-07,
# #225), so each look first glances at the window list, which answers at once, and asks System Events only when
# the list shows a window of a process that raises system alerts.
WATCH = 2
# Seconds after which a look asks System Events even when the window list shows no such window, in case a
# process's window-list owner name differs from the name System Events knows it by.
RECHECK = 30
# The on-screen windows of the processes that raise system alerts (offload_host.ALERT_PROCESSES), each as its
# owner, layer and bounds, which CGWindowListCopyWindowInfo gives without any permission (#204); a crash report is
# UserNotificationCenter's window at layer 8. Never a window's name.
GLANCE_SCRIPT = """ObjC.import('CoreGraphics');
var names = %s;
var list = ObjC.deepUnwrap(ObjC.castRefToObject($.CGWindowListCopyWindowInfo($.kCGWindowListOptionOnScreenOnly,
    $.kCGNullWindowID))) || [];
JSON.stringify(list.filter(function (w) { return names.indexOf(w.kCGWindowOwnerName) >= 0; }).map(function (w) {
  var b = w.kCGWindowBounds || {};
  return {app: w.kCGWindowOwnerName, layer: w.kCGWindowLayer, frame: [b.X, b.Y, b.Width, b.Height]};
}));""" % json.dumps(list(offload_host.ALERT_PROCESSES))
# Failed or repeated presses allowed in one clearing. Successful dismissals that make progress have no limit.
ATTEMPTS = 10
# The buttons that dismiss a crash-report alert, in preference order. Reopen starts the app again and
# Report… opens another window; neither is pressed.
DISMISS = ("Ignore", "OK")
CRASH = re.compile(r"quit unexpectedly|unexpectedly quit|crashed", re.IGNORECASE)
# The process an alert's text names as the one that quit: "Crasher944 quit unexpectedly.", or "The application
# Finder quit unexpectedly."
CRASHED = re.compile(r"^\s*(?:the application\s+)?[“\"]?(?P<name>[^“”\"]+?)[”\"]?\s+"
                     r"(?:quit unexpectedly|unexpectedly quit|crashed)\b", re.IGNORECASE)
# What such a text says in place of a name, which names no process.
UNNAMED = {"the application", "an application", "application", "the app", "an app", "app"}
# Seconds a cleared alert may stand and still count as cleared at the look that met it: the wait between looks
# (WATCH), which a look that answers promptly fits well within, as on 2026-11-03. One that stood longer stood past
# that look, as two did on another Mac on 2026-11-07, 17 s and 30 s under load (#225, #226).
ONE_LOOK = WATCH
# An alert about any of these is the owner's, whatever process raised it.
OWNERS = re.compile(r"password|passcode|keychain|permission|allow|access|authenticat|touch id|administrator",
                    re.IGNORECASE)
PROMPTS = ("SecurityAgent", "coreautha", "universalAccessAuthWarn")
GUIDE = "doc/guides/foreground.md"
# What the toolkit reads and changes on the Mac, through these alone, so that tests replace them (host_isolation).
run = offload_host.output
clock = time.time


def change(value):
    """Set DialogType to value, or delete it for None, macOS's default; whether defaults succeeded."""
    command = ["defaults", "delete", DOMAIN, KEY] if value is None else \
        ["defaults", "write", DOMAIN, KEY, "-string", value]
    try: return subprocess.run(command, capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.SubprocessError): return False


def glance():
    """The on-screen windows of the processes that raise system alerts, each as {"app", "layer", "frame"}, from
    the window list, which needs no permission; None when it cannot be read."""
    found = run(["osascript", "-l", "JavaScript", "-e", GLANCE_SCRIPT])
    if found is None: return None
    try: listed = json.loads(found)
    except ValueError: return None
    if not isinstance(listed, list): return None
    windows = []
    for window in listed:
        try:
            app, layer = str(window["app"]), int(window["layer"])
            frame = [int(float(value)) for value in window["frame"]]
        except (KeyError, TypeError, ValueError):
            continue
        # A window with no extent covers nothing.
        if app in offload_host.ALERT_PROCESSES and len(frame) == 4 and frame[2] and frame[3]:
            windows.append({"app": app, "layer": layer, "frame": frame})
    return windows


def press(alert, button):
    """Press button on alert's window, found again by its process and frame; whether System Events pressed it."""
    x, y, width, height = alert["frame"]
    script = f"""tell application "System Events" to tell process "{alert['app']}"
    repeat with window_ in windows
        if (position of window_ as list) is {{{x}, {y}}} and (size of window_ as list) is {{{width}, {height}}} then
            click button "{button}" of window_
            return "pressed"
        end if
    end repeat
end tell"""
    return run(["osascript", "-e", script]) == "pressed"


class Blocked(Exception):
    """System alerts that queuekeeper cannot clear are over the screen, so the run cannot be measured."""


def describe(alert):
    x, y, width, height = alert["frame"]
    said = next(iter(alert["texts"]), "no text")
    return (f"{alert['app']}'s window at x {x}, y {y}, {width}×{height}: {said!r}"
            f"{window_placement.placed(alert.get('placement'))}")


def owners(alert):
    """Whether alert is the owner's to answer: a password, permission or keychain prompt."""
    return alert["app"] in PROMPTS or any(OWNERS.search(text) for text in alert["texts"])


def dismissal(alert):
    """The button that dismisses alert, when it is a crash report queuekeeper may dismiss, or None."""
    if owners(alert) or alert["app"] not in ("UserNotificationCenter", "Problem Reporter", "CrashReporter"):
        return None
    if not any(CRASH.search(text) for text in alert["texts"]): return None
    return next((button for button in DISMISS if button in alert["buttons"]), None)


def clear(said, windows=None, pressed=None):
    """Dismiss the screen's crash-report alerts, one at a time, saying each and telling pressed, when given, of
    each as its press returns; (cleared, left), left being the system alerts still there, or None when they cannot
    be read."""
    windows = windows or (lambda: offload_host.system_alerts(run))
    cleared, missed, counts = [], [], {}
    attempts = 0
    while attempts < ATTEMPTS:
        found = windows()
        if found is None: return cleared, None
        # One press missed stays: one the window took after it went, as when a run's watcher and a driver's
        # check both press it (2026-11-03), is looked for again and is gone.
        target = next((alert for alert in found if dismissal(alert) and alert not in missed), None)
        if target is None: return cleared, found
        name = key(target)
        count = sum(key(alert) == name for alert in found)
        # Identical crash reports can be stacked at the same frame. A smaller count proves progress;
        # an unchanged or larger count after a successful press may be a stuck or returning alert.
        if name in counts and count >= counts[name]: attempts += 1
        counts[name] = count
        button = dismissal(target)
        if not press(target, button):
            attempts += 1
            missed.append(target)
            continue
        said(f"queuekeeper: dismissed a crash-report alert with {button}: {describe(target)}")
        if pressed is not None: pressed(target)
        cleared.append(target)
    return cleared, windows()


def crashed(alert):
    """The process alert says quit unexpectedly, or None when its text names none."""
    for text in alert["texts"]:
        found = CRASHED.match(text)
        if found and found.group("name").strip().casefold() not in UNNAMED: return found.group("name").strip()
    return None


def key(alert):
    """What tells one alert from another across looks."""
    return alert["app"], tuple(alert["frame"]), tuple(alert["texts"])


def stamp(when):
    return datetime.fromtimestamp(when, timezone.utc).isoformat(timespec="seconds")


def stopping(left):
    """Why left, the system alerts queuekeeper could not clear, stop a run, with where each sits among the
    screen's windows, so that the owner finds one hidden behind others (#14)."""
    left = window_placement.place_alerts(left, run(offload_host.ORDER))
    prompts = [alert for alert in left if owners(alert)]
    others = [alert for alert in left if not owners(alert)]
    reasons = []
    if prompts:
        named = "; ".join(describe(alert) for alert in prompts)
        reasons.append(f"a password, permission or keychain prompt is over the screen, which is the owner's to answer and "
                       f"queuekeeper never touches: {named}")
    if others:
        named = "; ".join(describe(alert) for alert in others)
        reasons.append(f"a system alert queuekeeper cannot dismiss is over the screen: {named}")
    return "; also, ".join(reasons)


def record_path():
    return offload_host.crash_dialog_record()


def restore(kept, said):
    """Put back the DialogType value kept, and remove its record once it reads back; whether it did."""
    before = kept.get("before")
    # The value read back decides: deleting a key already gone fails, as it did on 2026-11-03.
    change(before)
    if offload_host.crash_dialog(run) == before:
        record_path().unlink(missing_ok=True)
        said(f"queuekeeper: put back crash-report dialogs: {DOMAIN} {KEY} is {before or 'unset'} again")
        return True
    said(f"queuekeeper: could not put back {DOMAIN} {KEY} {before or 'unset'}; the next queuekeeper command on this Mac tries again "
         f"from {record_path()}")
    return False


def recover(said=None):
    """At queuekeeper's start: put back the DialogType value a foreground run kept whose queuekeeper no longer runs. A run that
    still runs keeps its own."""
    path = record_path()
    if not path.exists(): return
    said = said or (lambda text: print(text, file=sys.stderr, flush=True))
    try: kept = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        said(f"queuekeeper: {path} cannot be read, so crash-report dialogs may still be off: {error}")
        return
    if kept.get("start") is not None and resources.process_start(kept.get("pid")) == kept["start"]: return
    said(f"queuekeeper: {kept.get('owner', 'a foreground run')}, which no longer runs, left crash-report dialogs off")
    restore(kept, said)


class Run:
    """What a guarded run met: crash-report alerts cleared, each with when queuekeeper first saw it and when its press
    returned, and system alerts left over the screen. apps names the app under test, as a crash alert names its
    process; running is set once the run has started, so that an alert cleared before it is told apart. A look
    that cannot read the alerts is followed by the next, WATCH s later, as any other (#225); looks counts the
    looks that read them, that failed, and that the window list showed no need for."""

    def __init__(self, said, windows, apps=(), watch=WATCH):
        self.said, self.windows, self.watch = said, windows, watch
        self.apps = [name for name in apps if name]
        self.cleared, self.left = [], []
        self.seen = {}
        self.running = False
        self.failing = False
        self.asked = None
        self.looks = {"read": 0, "failed": 0, "glanced": 0}
        self.lock = threading.Lock()

    def quiet(self, now):
        """Whether the window list shows no window of a process that raises system alerts, so that this look
        need not ask System Events, which under load takes many seconds: never with windows given, as a test
        gives them, nor RECHECK s after System Events was last asked."""
        if self.windows is not None or self.asked is None or now - self.asked > RECHECK: return False
        return glance() == []

    def listed(self, started):
        """The screen's system alerts, each first seen, however many looks it then stands, at the start of the
        first look that listed it."""
        alerts = (self.windows or (lambda: offload_host.system_alerts(run)))()
        with self.lock:
            for alert in alerts or []: self.seen.setdefault(key(alert), started)
        return alerts

    def look(self, full=False):
        """The system alerts left over the screen once this look has cleared what it may, or None when they
        cannot be read. A full look always asks System Events; another first glances at the window list."""
        started, presses = clock(), {}
        if not full and self.quiet(started):
            with self.lock: self.looks["glanced"] += 1
            return []
        self.asked = started
        cleared, left = clear(self.said, lambda: self.listed(started),
                              lambda alert: presses.setdefault(key(alert), clock()))
        with self.lock:
            self.looks["failed" if left is None else "read"] += 1
            # Said once as looks start failing and once as one reads them again, however many fail between.
            if left is None and not self.failing:
                self.said(f"queuekeeper: the screen's system alerts cannot be read (System Events needs accessibility, or did "
                          f"not answer), so none is cleared now; queuekeeper looks again every {self.watch:g} s")
            elif left is not None and self.failing:
                self.said("queuekeeper: the screen's system alerts can be read again")
            self.failing = left is None
            for alert in cleared:
                self.cleared.append({"alert": alert, "since": self.seen.pop(key(alert), started),
                                     "at": presses.get(key(alert), clock()), "during": self.running})
            # One gone by itself, or pressed by another, is seen afresh when it comes back.
            if left is not None:
                standing = {key(alert) for alert in left}
                self.seen = {name: since for name, since in self.seen.items() if name in standing}
            for alert in left or []:
                if alert not in self.left:
                    self.said(f"queuekeeper: a system alert stays over the screen: {describe(alert)}")
                    self.left.append(alert)
        return left

    def is_app_under_test(self, process):
        return process is not None and any(process.casefold() == name.casefold() for name in self.apps)

    def verdict(self, entry):
        """(excuses, why): whether a crash-report alert queuekeeper cleared during a failed run makes the run unmeasured,
        and why or why not. It does when the alert stood longer than one look and was for a process other than
        the app under test (owner's decision, 2026-11-07, #226); one whose process queuekeeper cannot name, or a run that
        names no app under test, counts as the app under test's."""
        process, stood = crashed(entry["alert"]), entry["at"] - entry["since"]
        standing = f"stood {stood:.1f} s from {stamp(entry['since'])}"
        if process is None:
            return False, f"names no process queuekeeper can read, so queuekeeper counts it as the app under test's; it {standing}"
        if not self.apps:
            return False, (f"was for {process}, and the run names no app under test (a lane's \"apps\", or qk admit "
                           f"--app), so queuekeeper counts it as the app under test's; it {standing}")
        if self.is_app_under_test(process):
            return False, f"was for {process}, the app under test, whose crash is the run's failure; it {standing}"
        if stood <= ONE_LOOK:
            return False, f"was for {process} and {standing}, within one look ({ONE_LOOK} s)"
        return True, (f"was for {process}, not the app under test ({', '.join(self.apps)}), and {standing}, longer "
                      f"than one look ({ONE_LOOK} s)")

    def judged(self, status):
        """status, or UNMEASURED for a run that did not pass while a system alert queuekeeper could not clear was over
        the screen, at a look during the run or one as it ended, or after queuekeeper cleared during it a crash-report
        alert for a process other than the app under test that stood longer than one look: its clicks may have
        gone to the alert. A crash report cleared during the run for the app under test preserves the failure
        even when another alert could make the run unmeasured."""
        if status in (0, lane_results.UNMEASURED): return status
        self.look()
        cleared_during_run = [entry for entry in self.cleared if entry["during"]]
        app_crashed = any(self.is_app_under_test(crashed(entry["alert"])) for entry in cleared_during_run)
        if self.left and not app_crashed:
            print(f"queuekeeper: unmeasured: the run ended with status {status} while {stopping(self.left)}", file=sys.stderr,
                  flush=True)
            return lane_results.UNMEASURED
        judged = [(entry, *self.verdict(entry)) for entry in cleared_during_run]
        excusing = [(entry, why) for entry, excuses, why in judged if excuses and not app_crashed]
        for entry, why in excusing:
            print(f"queuekeeper: unmeasured: the run ended with status {status} after queuekeeper cleared a crash-report alert during "
                  f"it that {why}, so its clicks may have gone to the alert: {describe(entry['alert'])}",
                  file=sys.stderr, flush=True)
        if excusing: return lane_results.UNMEASURED
        for entry, _excuses, why in judged:
            print(f"queuekeeper: the run's status {status} stands: the crash-report alert queuekeeper cleared during it {why}: "
                  f"{describe(entry['alert'])}", file=sys.stderr, flush=True)
        if not self.looks["read"] and not self.looks["glanced"]:
            print(f"queuekeeper: the run's status {status} stands, though no look during it could read the screen's system "
                  "alerts, so queuekeeper cannot say whether one took its clicks", file=sys.stderr, flush=True)
        return status

    def summary(self):
        cleared = [{"alert": describe(entry["alert"]), "process": crashed(entry["alert"]),
                    "start": stamp(entry["since"]), "stoodSeconds": round(entry["at"] - entry["since"], 1),
                    "duringRun": entry["during"]} for entry in self.cleared]
        return {"appsUnderTest": self.apps, "cleared": cleared, "left": [describe(alert) for alert in self.left],
                "looks": dict(self.looks)}


@contextlib.contextmanager
def guarded(purpose, *, said=None, receipt=None, windows=None, watch=None, apps=()):
    """Turn crash-report dialogs off for the run inside the block, and clear and watch the screen's alerts.
    apps names the app under test, as a crash-report alert names the process that quit.

    It raises Blocked, with nothing run, when a system alert it cannot clear is over the screen before the
    run. The block's Run judges the run's status. However the block ends, DialogType's value from before it is
    put back, and the record that keeps it stays only when that fails, for the next queuekeeper command."""
    said = said or (lambda text: print(text, file=sys.stderr, flush=True))
    watch = WATCH if watch is None else watch
    recover(said)
    before = offload_host.crash_dialog(run)
    path = record_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # The record first, so that a run that dies after the change leaves what puts it back.
    kept = {"before": before, "owner": f"queuekeeper {os.getpid()} for {purpose}", "pid": os.getpid(),
            "start": resources.process_start(os.getpid()), "at": datetime.now(timezone.utc).isoformat()}
    path.write_text(json.dumps(kept))
    state = Run(said, windows, apps, watch)
    # In the receipt from the start, for a receipt written while the run still holds it.
    report = {"dialogTypeBefore": before}
    if receipt is not None: receipt["crashAlerts"] = report
    stop = threading.Event()
    watcher = None
    try:
        if before != OFF:
            if change(OFF): said(f"queuekeeper: crash-report dialogs off for the run: {DOMAIN} {KEY} was {before or 'unset'}")
            else: said(f"queuekeeper: could not turn crash-report dialogs off ({DOMAIN} {KEY}); alerts are cleared instead")
        left = state.look(full=True)
        if left: raise Blocked(stopping(left))
        # What a look clears from here on, it clears during the run.
        state.running = True
        # Watched even when this look could not read the alerts: on another Mac on 2026-11-07 one such look
        # was a slow System Events call, not a missing grant, and the alert it missed stood the rest of the run.
        def watching():
            while not stop.wait(watch): state.look()
        watcher = threading.Thread(target=watching, name="crash-alerts", daemon=True)
        watcher.start()
        yield state
    finally:
        stop.set()
        if watcher is not None: watcher.join(timeout=60)
        if before == OFF: path.unlink(missing_ok=True)
        else: restore(kept, said)
        report.update(restored=not path.exists(), **state.summary())


def main(argv=None):
    """`python3 -m queuekeeper_mac_desktop.crash_alerts check`: clear this Mac's crash-report alerts, as a driver does
    between steps under the foreground it holds; exit 0 when no system alert is left over the screen,
    lane_results.UNMEASURED when one is, which it names, and 1 when they cannot be read."""
    argv = sys.argv[1:] if argv is None else argv
    if argv != ["check"]:
        print("usage: python3 -m queuekeeper_mac_desktop.crash_alerts check", file=sys.stderr)
        return 2
    said = lambda text: print(text, file=sys.stderr, flush=True)
    _cleared, left = clear(said)
    if left is None:
        said("queuekeeper: the screen's system alerts cannot be read (System Events needs accessibility)")
        return 1
    if left:
        said(f"queuekeeper: unmeasured: {stopping(left)}")
        return lane_results.UNMEASURED
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
