"""The owner's notifications during a run on his iPhone or iPad (vocaro-engineering#144).

The paired iPhone is the owner's personal phone and the iPad gets his notifications too. A notification that
arrives mid-session moves VoiceOver's cursor and puts the owner's words in the transcript, so the session is
discarded: on 2026-11-04 one spoiled a session in an approved iPhone window. A run silences them for itself and
puts back what was there before, as foreground runs do with the Mac's crash-report dialogs (#118).

Nothing documented sets a device's Focus from the Mac. Xcode 27's `devicectl` (651.32) sets appearance, audio,
biometrics, StandBy and VoiceOver, and reads no Focus; its `notification` commands post Darwin notifications, not
user ones. The notification-settings configuration profile payload needs a supervised device, which the owner's
are not. So the owner keeps two shortcuts on each device, made once by hand (guides/device-testing.md,
Notifications during a run), and eng runs them by opening Shortcuts with a `shortcuts://run-shortcut` URL through
`devicectl device process launch --payload-url`: SILENCE turns Do Not Disturb on when no Focus is on and leaves a
marker, and RESTORE turns it off only when that marker is there, so it puts back what was there before and does
nothing twice. The Mac reads no device's Focus, so eng never confirms the silencing: it records that it asked and
how devicectl answered. devices.py records each step in the reservation entry and runs RESTORE at release or at
attended recovery.
"""
import subprocess
from urllib.parse import quote

# The app that runs a shortcut by URL, and the owner's two shortcuts, by the names he gives them.
APP = "com.apple.shortcuts"
SILENCE = "Vocaro Silence Notifications"
RESTORE = "Vocaro Restore Notifications"
# Seconds devicectl has to open Shortcuts, and the wait for devicectl itself beyond that. A Wi-Fi launch
# can take a minute on a busy host (guides/device-testing.md, devicectl states and timeouts).
TIMEOUT = 120
GUIDE = "guides/device-testing.md, Notifications during a run"


def url(name):
    """The URL that has Shortcuts run the shortcut name (Shortcuts User Guide, Run a shortcut from a URL)."""
    return "shortcuts://run-shortcut?name=" + quote(name, safe="")


def command(udid, name):
    return ["xcrun", "devicectl", "--timeout", str(TIMEOUT), "device", "process", "launch", "--device", udid,
            "--payload-url", url(name), APP]


def execute(arguments):
    """Run arguments; (exit status, the end of what they said), or (None, why) when they did not finish."""
    try:
        done = subprocess.run(arguments, capture_output=True, text=True, timeout=TIMEOUT + 30)
    except subprocess.TimeoutExpired:
        return None, f"no answer within {TIMEOUT + 30}s"
    except OSError as error:
        return None, str(error)
    return done.returncode, " ".join((done.stderr or done.stdout or "").split())[-400:]


# What reaches a device, alone, so that tests replace it (host_isolation).
run = execute


def trigger(udid, name):
    """Have the device udid run the owner's shortcut name; (whether devicectl opened Shortcuts with it, what it said).

    Opening Shortcuts is all the Mac can see: whether the shortcut then changed the Focus it cannot read."""
    status, said = run(command(udid, name))
    if status == 0:
        return True, f"devicectl opened Shortcuts with {url(name)}"
    return False, (f"devicectl could not open Shortcuts with {url(name)} "
                   f"({'it did not finish' if status is None else f'exit {status}'}): {said or 'no output'}")


def ask(key):
    """What a session asks the owner, in the message that agrees the window, when eng cannot silence key."""
    return (f"ask the owner, in the same message as the window, to turn on Do Not Disturb on the {key} for the run "
            f"and to say when it is on; record it with `devices notifications by-owner {key}`. Making the "
            f"{SILENCE!r} and {RESTORE!r} shortcuts on the {key} once lets eng do it ({GUIDE})")
