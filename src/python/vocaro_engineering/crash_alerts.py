"""macOS's crash-report alerts during a foreground run, on either Mac (vocaro-engineering#118).

An app that quits unexpectedly raises a "quit unexpectedly" alert in UserNotificationCenter that stays on the
screen over the windows a foreground UI run clicks, so its clicks reach the alert and the run fails, as EPUBLib's
toolbar runs did on the second Mac on 2026-11-02 (macOS 27.2 26B123). The owner wants these alerts on between
runs, so a foreground run turns them off for itself alone: it keeps com.apple.CrashReporter's DialogType from
before the run in a record, sets it to none, and puts that exact value back however the run ends. A run whose eng
died keeps its record, and the next eng command on that Mac puts the value back (recover). Before the run takes
the screen and while it runs, eng dismisses any crash-report alert with Ignore or OK, never Reopen or Report, and
leaves every other system alert alone: a password, permission or keychain prompt is the owner's. One it cannot
clear before the run leaves the run unmeasured with nothing run; one met during a run that then fails leaves that
run unmeasured too, never failed (guides/second-mac-foreground.md, Crash-report alerts).
"""
import contextlib
from datetime import datetime, timezone
import json
import os
import re
import subprocess
import sys
import threading

from . import evidence, offload_host, resources

DOMAIN, KEY = offload_host.CRASH_DIALOG
OFF = "none"
# Seconds between looks at the screen's system alerts while a run holds the foreground. An alert came about 1 s
# after its app's crash on the second Mac, and a look takes under one (2026-11-03).
WATCH = 2
# Crash-report alerts dismissed in one clearing, each looked for again after the last one went.
ATTEMPTS = 10
# The buttons that dismiss a crash-report alert, in preference order. Reopen starts the app again and
# Report… opens another window; neither is pressed.
DISMISS = ("Ignore", "OK")
CRASH = re.compile(r"quit unexpectedly|unexpectedly quit|crashed", re.IGNORECASE)
# An alert about any of these is the owner's, whatever process raised it.
OWNERS = re.compile(r"password|passcode|keychain|permission|allow|access|authenticat|touch id|administrator",
                    re.IGNORECASE)
PROMPTS = ("SecurityAgent", "coreautha", "universalAccessAuthWarn")
GUIDE = "guides/second-mac-foreground.md"
# What the toolkit reads and changes on the Mac, through these alone, so that tests replace them (host_isolation).
run = offload_host.output


def change(value):
    """Set DialogType to value, or delete it for None, macOS's default; whether defaults succeeded."""
    command = ["defaults", "delete", DOMAIN, KEY] if value is None else \
        ["defaults", "write", DOMAIN, KEY, "-string", value]
    try: return subprocess.run(command, capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.SubprocessError): return False


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
    """System alerts that eng cannot clear are over the screen, so the run cannot be measured."""


def describe(alert):
    x, y, width, height = alert["frame"]
    said = next(iter(alert["texts"]), "no text")
    return f"{alert['app']}'s window at x {x}, y {y}, {width}×{height}: {said!r}"


def owners(alert):
    """Whether alert is the owner's to answer: a password, permission or keychain prompt."""
    return alert["app"] in PROMPTS or any(OWNERS.search(text) for text in alert["texts"])


def dismissal(alert):
    """The button that dismisses alert, when it is a crash report eng may dismiss, or None."""
    if owners(alert) or alert["app"] not in ("UserNotificationCenter", "Problem Reporter", "CrashReporter"):
        return None
    if not any(CRASH.search(text) for text in alert["texts"]): return None
    return next((button for button in DISMISS if button in alert["buttons"]), None)


def clear(said, windows=None):
    """Dismiss the screen's crash-report alerts, one at a time, saying each; (cleared, left), left being the
    system alerts still there, or None when they cannot be read."""
    windows = windows or (lambda: offload_host.system_alerts(run))
    cleared, missed = [], []
    for _ in range(ATTEMPTS):
        found = windows()
        if found is None: return cleared, None
        # One press missed stays: one the window took after it went, as when a run's watcher and a driver's
        # check both press it (2026-11-03), is looked for again and is gone.
        target = next((alert for alert in found if dismissal(alert) and alert not in missed), None)
        if target is None: return cleared, found
        button = dismissal(target)
        if not press(target, button):
            missed.append(target)
            continue
        said(f"eng: dismissed a crash-report alert with {button}: {describe(target)}")
        cleared.append(target)
    return cleared, windows()


def stopping(left):
    """Why left, the system alerts eng could not clear, stop a run."""
    named = "; ".join(describe(alert) for alert in left)
    if any(owners(alert) for alert in left):
        return (f"a password, permission or keychain prompt is over the screen, which is the owner's to answer and "
                f"eng never touches: {named}")
    return f"a system alert eng cannot dismiss is over the screen: {named}"


def record_path():
    return offload_host.crash_dialog_record()


def restore(kept, said):
    """Put back the DialogType value kept, and remove its record once it reads back; whether it did."""
    before = kept.get("before")
    # The value read back decides: deleting a key already gone fails, as it did on 2026-11-03.
    change(before)
    if offload_host.crash_dialog(run) == before:
        record_path().unlink(missing_ok=True)
        said(f"eng: put back crash-report dialogs: {DOMAIN} {KEY} is {before or 'unset'} again")
        return True
    said(f"eng: could not put back {DOMAIN} {KEY} {before or 'unset'}; the next eng command on this Mac tries again "
         f"from {record_path()}")
    return False


def recover(said=None):
    """At eng's start: put back the DialogType value a foreground run kept whose eng no longer runs. A run that
    still runs keeps its own."""
    path = record_path()
    if not path.exists(): return
    said = said or (lambda text: print(text, file=sys.stderr, flush=True))
    try: kept = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        said(f"eng: {path} cannot be read, so crash-report dialogs may still be off: {error}")
        return
    if kept.get("start") is not None and resources.process_start(kept.get("pid")) == kept["start"]: return
    said(f"eng: {kept.get('owner', 'a foreground run')}, which no longer runs, left crash-report dialogs off")
    restore(kept, said)


class Run:
    """What a guarded run met: crash-report alerts cleared and system alerts left over the screen."""

    def __init__(self, said, windows):
        self.said, self.windows = said, windows
        self.cleared, self.left = [], []
        self.lock = threading.Lock()

    def look(self):
        cleared, left = clear(self.said, self.windows)
        with self.lock:
            self.cleared += cleared
            for alert in left or []:
                if alert not in self.left:
                    self.said(f"eng: a system alert stays over the screen: {describe(alert)}")
                    self.left.append(alert)
        return left

    def judged(self, status):
        """status, or UNMEASURED for a run that did not pass while a system alert eng could not clear was over
        the screen, at a look during the run or one as it ended: its clicks may have gone to the alert."""
        if status in (0, evidence.UNMEASURED): return status
        self.look()
        if not self.left: return status
        print(f"eng: unmeasured: the run ended with status {status} while {stopping(self.left)}", file=sys.stderr,
              flush=True)
        return evidence.UNMEASURED

    def summary(self):
        return {"cleared": [describe(alert) for alert in self.cleared], "left": [describe(alert) for alert in self.left]}


@contextlib.contextmanager
def guarded(purpose, *, said=None, receipt=None, windows=None, watch=None):
    """Turn crash-report dialogs off for the run inside the block, and clear and watch the screen's alerts.

    It raises Blocked, with nothing run, when a system alert it cannot clear is over the screen before the
    run. The block's Run judges the run's status. However the block ends, DialogType's value from before it is
    put back, and the record that keeps it stays only when that fails, for the next eng command."""
    said = said or (lambda text: print(text, file=sys.stderr, flush=True))
    watch = WATCH if watch is None else watch
    recover(said)
    before = offload_host.crash_dialog(run)
    path = record_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # The record first, so that a run that dies after the change leaves what puts it back.
    kept = {"before": before, "owner": f"eng {os.getpid()} for {purpose}", "pid": os.getpid(),
            "start": resources.process_start(os.getpid()), "at": datetime.now(timezone.utc).isoformat()}
    path.write_text(json.dumps(kept))
    state = Run(said, windows)
    # In the receipt from the start, for a receipt written while the run still holds it.
    report = {"dialogTypeBefore": before}
    if receipt is not None: receipt["crashAlerts"] = report
    stop = threading.Event()
    watcher = None
    try:
        if before != OFF:
            if change(OFF): said(f"eng: crash-report dialogs off for the run: {DOMAIN} {KEY} was {before or 'unset'}")
            else: said(f"eng: could not turn crash-report dialogs off ({DOMAIN} {KEY}); alerts are cleared instead")
        left = state.look()
        if left is None:
            said("eng: the screen's system alerts cannot be read (System Events needs accessibility), so none is "
                 "cleared")
        elif left:
            raise Blocked(stopping(left))
        def watching():
            while not stop.wait(watch): state.look()
        if left is not None:
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
    """`python3 -m vocaro_engineering.crash_alerts check`: clear this Mac's crash-report alerts, as a driver does
    between steps under the foreground it holds; exit 0 when no system alert is left over the screen,
    evidence.UNMEASURED when one is, which it names, and 1 when they cannot be read."""
    argv = sys.argv[1:] if argv is None else argv
    if argv != ["check"]:
        print("usage: python3 -m vocaro_engineering.crash_alerts check", file=sys.stderr)
        return 2
    said = lambda text: print(text, file=sys.stderr, flush=True)
    _cleared, left = clear(said)
    if left is None:
        said("eng: the screen's system alerts cannot be read (System Events needs accessibility)")
        return 1
    if left:
        said(f"eng: unmeasured: {stopping(left)}")
        return evidence.UNMEASURED
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
