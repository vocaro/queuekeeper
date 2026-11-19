"""The owner's notifications on a physical Android device during a run, and a watch of its log for one that alerted.

A notification that reaches a device mid-run moves TalkBack's focus and puts private text on screen, as on an iPhone
(the Apple plugin's device_notifications). Android needs no owner-made shortcut: adb's shell may set Do Not Disturb
itself, `cmd notification set_dnd priority`, and read it back from the global setting zen_mode, so the silencing
is confirmed, which the Apple plugin's never is. The mode the device was in before is recorded and put back.

The watch reads the device's event log, `adb logcat -b events`, for notification_enqueue and notification_alert:
an enqueue is any notification posted, an alert one that sounded, vibrated or blinked, which a heads-up banner goes
with. Only an alert, or an enqueue from a package outside the run's own while Do Not Disturb was off, counts as a
notification that reached the run. This was written without an Android phone to try it on (2026-11-18): the event
tags are Android's own (frameworks/base services EventLogTags), but which of them a banner on a given vendor's build
logs is unverified.
"""
import os
import re
import subprocess

import tempfile
import time

from . import adb, sdk

# zen_mode's values (android.provider.Settings.Global): off, priority only, total silence, alarms only.
ZEN = {"0": "off", "1": "priority", "2": "none", "3": "alarms"}
# What `cmd notification set_dnd` takes for each.
SET_DND = {"off": "off", "priority": "priority", "none": "none", "alarms": "alarms"}
# The mode a run sets: priority only, which lets alarms and starred contacts through and nothing else.
SILENT = "priority"
WATCH = "QUEUEKEEPER_DEVICE_LOG_WATCH"
GUIDE = "plugins/android/src/queuekeeper_android/README.md, Notifications"
EVENT = re.compile(r"\b(?P<tag>notification_enqueue|notification_alert)\b\s*:?\s*\[(?P<fields>[^\]]*)\]")
# Seconds a change of the mode has to read back.
SETTLE = 5
# Seconds the watch waits for logcat to start relaying before the run starts.
READY = 5
ENDING = 5


def mode(serial):
    """The device's Do Not Disturb mode by zen_mode's name, or None when it could not be read."""
    try: value = adb.shell(serial, "settings", "get", "global", "zen_mode", timeout=15)
    except (adb.Failed, sdk.Missing): return None
    return ZEN.get(value.strip())


def set_mode(serial, wanted, settle=SETTLE, sleep=time.sleep):
    """Set Do Not Disturb to wanted and read it back: (whether it holds, what was read or why it failed). The
    notification service applies the mode a moment after the command returns, so the reading is repeated for up to
    settle seconds: on an Android 16 emulator an immediate read still gave the mode before (2026-11-18)."""
    try:
        adb.shell(serial, "cmd", "notification", "set_dnd", SET_DND[wanted], timeout=15)
    except (adb.Failed, sdk.Missing) as error:
        return False, str(error)
    deadline = time.monotonic() + settle
    while True:
        read = mode(serial)
        if read == wanted or time.monotonic() >= deadline: break
        sleep(0.25)
    return read == wanted, f"zen_mode reads {read or 'nothing'}"


def silence(serial):
    """Turn on Do Not Disturb for a run; what the entry's notifications record: the mode before, so that restore puts
    it back, and whether the silencing was confirmed."""
    before = mode(serial)
    if before is None:
        # Nothing was changed, so there is nothing to put back.
        return {"silence": "failed", "detail": "zen_mode could not be read over adb", "confirmed": False,
                "restore": "not needed"}
    if before != "off":
        return {"silence": "owner", "before": before, "detail": f"Do Not Disturb was already {before}",
                "confirmed": True}
    held, detail = set_mode(serial, SILENT)
    # A failed set may still have changed the mode, so the restore stays pending either way: it sets the mode found.
    return {"silence": "requested" if held else "failed", "before": before, "detail": detail, "confirmed": held,
            "restore": "pending"}


def restore(serial, notifications):
    """Put back the mode silence found; what to record."""
    before = (notifications or {}).get("before") or "off"
    held, detail = set_mode(serial, before)
    return {"restore": "done" if held else "failed", "restoreDetail": detail}


def ask(key):
    return (f"ask the owner to turn on Do Not Disturb on {key} by hand, or check that USB debugging is still "
            f"authorized for this Mac ({GUIDE})")


def events(text, own=()):
    """The notifications text, logcat's events, records: [{"tag", "package", "line"}], leaving out an enqueue from a
    package in own, the run's own app and test app, whose notifications are part of what it tests."""
    found = []
    for line in text.splitlines():
        match = EVENT.search(line)
        if not match: continue
        fields = [field.strip() for field in match.group("fields").split(",")]
        # notification_enqueue: uid, pid, pkg, id, tag, userid, notification, status. notification_alert: key, buzz,
        # beep, blink, where the key is userid|pkg|id|tag|uid.
        if match.group("tag") == "notification_enqueue":
            package = fields[2] if len(fields) > 2 else ""
        else:
            key = fields[0].split("|") if fields else []
            package = key[1] if len(key) > 1 else ""
        if match.group("tag") == "notification_enqueue" and package in own: continue
        found.append({"tag": match.group("tag"), "package": package, "line": line.strip()[:300]})
    return found


class Watch:
    """serial's event log, relayed by `adb logcat` while a run lasts and read once for a notification (events)."""

    def __init__(self, serial, own=()):
        self.serial, self.own, self.process, self.path, self.failure = serial, tuple(own), None, None, None
        if os.environ.get(WATCH) == "off":
            self.failure = f"{WATCH} is off"
            return
        try:
            command = [sdk.tool("adb"), "-s", serial, "logcat", "-b", "events", "-v", "epoch", "-T", "1"]
        except sdk.Missing as error:
            self.failure = str(error)
            return
        descriptor, self.path = tempfile.mkstemp(prefix="queuekeeper-android-log-")
        try:
            self.process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=descriptor,
                                            stderr=subprocess.DEVNULL, env=sdk.environment())
        except OSError as error:
            self.failure = str(error)
        finally:
            os.close(descriptor)
        deadline = time.monotonic() + READY
        while self.process and self.process.poll() is None and time.monotonic() < deadline and not self._read():
            time.sleep(0.1)
        self.ready = bool(self._read())

    def _read(self):
        try:
            with open(self.path, errors="replace") as handle: return handle.read()
        except (OSError, TypeError):
            return ""

    def stop(self):
        """End the relay and read it: {"watched", "presented", "first"?, "transport"?, "detail"?}, as the Apple
        plugin's watch says it, with nothing the notifications say beyond their packages."""
        ended = self.process is not None and self.process.poll() is not None
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try: self.process.wait(ENDING)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        text = self._read()
        if self.path:
            try: os.unlink(self.path)
            except OSError: pass
        found = events(text, self.own)
        seen = {"watched": bool(text) and self.ready and not ended, "presented": len(found)}
        if found: seen["first"] = f"{found[0]['tag']} from {found[0]['package'] or 'an unnamed package'}"
        if seen["watched"]: seen["transport"] = "adb"
        else:
            seen["detail"] = self.failure or (f"logcat lost {self.serial} during the run" if ended else
                                              f"logcat relayed nothing of {self.serial}'s event log")
        return seen


def watch(serial, own=()):
    return Watch(serial, own)


def presented(seen):
    return f"{seen['presented']} notification{'s' if seen['presented'] != 1 else ''} reached the device during the " \
           f"run, the first {seen.get('first')}"
