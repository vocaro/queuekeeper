"""Notification Center windows over the second Mac's screen before a foreground run (vocaro-engineering#204).

A notification macOS's Notification Center presents stays in a window of its own, at Quartz window layer 23,
over every app's windows. On the second Mac on 2026-11-05 (macOS 27.2 26B123) one stayed on the screen across
two exclusive foreground admissions, seven looks over 60 s among them with Finder in front, and stopped a
BreezEPUB benchmark from launching until the owner closed it by hand (BreezEPUB#15). Crash-report alerts are
UserNotificationCenter's and crash_alerts.py's (#118); this module is about NotificationCenter's own window.

queuekeeper finds the window by the window list, which needs no permission: an on-screen window at layer 23 whose owner
process runs NotificationCenter.app's executable. Only inside the second Mac's foreground lease, held by the run
VOCARO_DEVICE_OWNER names, it closes each notification the window presents with that notification's own Close
action through the accessibility API, the action its close button performs, and then looks at the window list
again until the window has left the screen. It never presses a notification, which would open it, never uses
Clear All on a stack, and never touches a notification that asks for a password, permission or access: those
are the owner's (crash_alerts.OWNERS). A window it cannot identify, read, close, or see gone stops the run as
unmeasured, naming the window. Nothing a notification says is printed or recorded: only the window's number,
process, layer and frame, and each closed notification's kind and frame (guides/second-mac-foreground.md,
Notification Center windows).
"""
import json
import os
import re
import sys
import time

from . import crash_alerts, devices, foreground
from .. import lane_results, offload_host

# The process whose windows these are, by its executable, which the window list's owner name (on macOS 27.2,
# "Notification Center", with a space) does not establish alone; and the layer its notifications show at.
EXECUTABLE = "/System/Library/CoreServices/NotificationCenter.app/Contents/MacOS/NotificationCenter"
LAYER = 23
# The accessibility subroles of a notification Notification Center presents, a banner or an alert. A stack of
# them (…Stack) closes only with Clear All, which queuekeeper never uses.
SUBROLES = ("AXNotificationCenterBanner", "AXNotificationCenterAlert")
CLOSE = "Close"
# Seconds queuekeeper looks for the window to leave the screen after closing its notifications, and between looks;
# rounds of closing, each for notifications that showed after the last round's.
GONE = 5
STEP = 0.5
ROUNDS = 3
GUIDE = "guides/second-mac-foreground.md"
SECTION = "Notification Center windows"
# What the toolkit reads and changes on the Mac, through this alone, so that tests replace it (host_isolation).
run = offload_host.output
trusted = offload_host.accessibility_trusted

# The on-screen windows, each as its number, owner process, layer, alpha and bounds, and never its name, which
# can hold what a notification says. CGWindowListCopyWindowInfo needs no permission for these.
WINDOWS_SCRIPT = """ObjC.import('CoreGraphics');
var list = ObjC.deepUnwrap(ObjC.castRefToObject($.CGWindowListCopyWindowInfo($.kCGWindowListOptionOnScreenOnly,
    $.kCGNullWindowID))) || [];
JSON.stringify(list.map(function (w) {
  var b = w.kCGWindowBounds || {};
  return {number: w.kCGWindowNumber, pid: w.kCGWindowOwnerPID, layer: w.kCGWindowLayer, alpha: w.kCGWindowAlpha,
          frame: [b.X, b.Y, b.Width, b.Height]};
}));"""

# Every element at or under el whose subrole begins AXNotificationCenter, found by walking UI elements: on macOS
# 27.2 `entire contents` of Notification Center's window (SwiftUI) lists none of them, though the banner is there,
# an AXGroup under a scroll area (2026-11-07, second Mac, #204).
PRESENTED_HANDLER = """on presented(el, depth)
    set found_ to {}
    set kind_ to ""
    set kids_ to {}
    tell application "System Events"
        try
            set kind_ to subrole of el as text
        end try
        try
            set kids_ to UI elements of el
        end try
    end tell
    if kind_ starts with "AXNotificationCenter" then set end of found_ to el
    if depth < 12 then
        repeat with k_ in kids_
            set found_ to found_ & my presented(contents of k_, depth + 1)
        end repeat
    end if
    return found_
end presented
"""

# Lists, as one line each, every presented notification in the windows of the process with unix id %(pid)d: its
# subrole, x, y, width and height, its actions' descriptions and its texts, the last two separated by the unit
# separator. It reads only: nothing here performs an action.
ELEMENTS_SCRIPT = PRESENTED_HANDLER + """on joined(found)
    set out to ""
    repeat with item_ in found
        try
            set out to out & (item_ as text) & (ASCII character 31)
        end try
    end repeat
    return out
end joined
set out to ""
tell application "System Events" to set windows_ to windows of (first process whose unix id is %(pid)d)
repeat with window_ in windows_
    repeat with element_ in my presented(contents of window_, 0)
        set element_ to contents of element_
        tell application "System Events"
            set kind_ to subrole of element_ as text
            set place_ to position of element_
            set extent_ to size of element_
            set labels_ to ""
            try
                repeat with action_ in actions of element_
                    set label_ to ""
                    try
                        set label_ to description of action_ as text
                    end try
                    -- A custom action's name runs over lines, "Name:Close" first.
                    if label_ is "" or label_ is "missing value" then set label_ to paragraph 1 of (name of action_ as text)
                    set labels_ to labels_ & label_ & (ASCII character 31)
                end repeat
            end try
            set texts_ to ""
            try
                set texts_ to my joined(value of static texts of element_)
            end try
            try
                set texts_ to texts_ & (description of element_ as text) & (ASCII character 31)
            end try
            set out to out & kind_ & tab & (item 1 of place_) & tab & (item 2 of place_) & tab & (item 1 of extent_) & tab & (item 2 of extent_) & tab & labels_ & tab & texts_ & linefeed
        end tell
    end repeat
end repeat
return out"""

# Performs the Close action of the notification of subrole %(kind)s at the frame given, in the process with unix
# id %(pid)d, found again by both; "closed" when it did, "none" when the notification has no Close action, and
# "gone" when no such notification is there.
CLOSE_SCRIPT = PRESENTED_HANDLER + """tell application "System Events" to set windows_ to windows of (first process whose unix id is %(pid)d)
repeat with window_ in windows_
    repeat with element_ in my presented(contents of window_, 0)
        set element_ to contents of element_
        tell application "System Events"
            set kind_ to subrole of element_ as text
            if kind_ is "%(kind)s" then
                set place_ to position of element_
                set extent_ to size of element_
                if place_ is {%(x)d, %(y)d} and extent_ is {%(width)d, %(height)d} then
                    repeat with action_ in actions of element_
                        set label_ to ""
                        try
                            set label_ to description of action_ as text
                        end try
                        if label_ is "Close" or (name of action_ as text) starts with "Name:Close" then
                            perform action_
                            return "closed"
                        end if
                    end repeat
                    return "none"
                end if
            end if
        end tell
    end repeat
end repeat
return "gone\""""


class Blocked(Exception):
    """A Notification Center window queuekeeper cannot clear is over the screen, so the run cannot be measured."""


def label(action):
    """An action's name as a person reads it: a custom action's "Name:Close\\nTarget:…" is Close."""
    found = re.match(r"Name:([^\n]*)", action)
    return found.group(1) if found else action


def executable(pid):
    return run(["ps", "-p", str(pid), "-o", "comm="])


def windows():
    """The Notification Center windows on the screen, each as {"number", "pid", "layer", "frame": [x, y, width,
    height]}, or None when the window list cannot be read. A window is one when it is on the screen at LAYER,
    visible and of some extent, and its owner process runs EXECUTABLE."""
    found = run(["osascript", "-l", "JavaScript", "-e", WINDOWS_SCRIPT])
    if found is None: return None
    try: listed = json.loads(found)
    except ValueError: return None
    if not isinstance(listed, list): return None
    result, checked = [], {}
    for window in listed:
        try:
            frame = [int(float(value)) for value in window["frame"]]
            number, pid, layer = int(window["number"]), int(window["pid"]), int(window["layer"])
            alpha = float(window.get("alpha", 1))
        except (KeyError, TypeError, ValueError):
            continue
        if layer != LAYER or alpha <= 0 or not frame[2] or not frame[3]: continue
        if pid not in checked: checked[pid] = executable(pid) == EXECUTABLE
        if checked[pid]: result.append({"number": number, "pid": pid, "layer": layer, "frame": frame})
    return result


def describe(window):
    x, y, width, height = window["frame"]
    return (f"Notification Center's window {window['number']} (pid {window['pid']}, layer {window['layer']}) at "
            f"x {x}, y {y}, {width}×{height}")


def notifications(pid):
    """The notifications Notification Center's process pid presents, each as {"kind", "frame", "actions",
    "texts"}, read through System Events, which needs accessibility; None when they cannot be read. The texts
    are for owners() alone and are never printed or kept."""
    found = run(["osascript", "-e", ELEMENTS_SCRIPT % {"pid": pid}])
    if found is None: return None
    result = []
    for line in found.splitlines():
        fields = line.split("\t")
        if len(fields) != 7: continue
        try: frame = [int(float(value)) for value in fields[1:5]]
        except ValueError: continue
        actions, texts = ([part for part in field.split("\x1f") if part and part != "missing value"]
                          for field in fields[5:7])
        result.append({"kind": fields[0], "frame": frame, "actions": [label(action) for action in actions],
                       "texts": texts})
    return result


def owners(notification):
    """Whether notification asks for a password, permission or access, which is the owner's to answer."""
    return any(crash_alerts.OWNERS.search(text) for text in notification["texts"])


def closable(notification):
    return notification["kind"] in SUBROLES and CLOSE in notification["actions"]


def shown(notification):
    """A notification's kind and frame, which is all queuekeeper says of one."""
    x, y, width, height = notification["frame"]
    return f"{notification['kind'].removeprefix('AXNotificationCenter').lower() or 'element'} at x {x}, y {y}, {width}×{height}"


def close(pid, notification):
    """Perform notification's Close action, finding it again by its kind and frame; whether System Events did."""
    x, y, width, height = notification["frame"]
    return run(["osascript", "-e", CLOSE_SCRIPT % {"pid": pid, "kind": notification["kind"], "x": x, "y": y,
                                                    "width": width, "height": height}]) == "closed"


def lease(owner=None, record=None):
    """Refuse, with devices.Refused, unless this is the second Mac and owner, by default VOCARO_DEVICE_OWNER,
    holds its mac-foreground lease: queuekeeper closes the owner's notifications only on a Mac nobody works at, and only
    for the run that holds its screen."""
    record = record or devices.Record()
    if not foreground.second_mac(record):
        raise devices.Refused("queuekeeper closes Notification Center's notifications only on the second Mac, under the "
                              f"foreground lease a run holds there; this Mac's are the owner's ({GUIDE}, {SECTION})")
    devices.require_foreground(owner if owner is not None else os.environ.get(devices.OWNER), record)


def gone(number, wait):
    """Whether the window numbered number has left the screen within wait seconds; None when the window list
    cannot be read."""
    deadline = time.monotonic() + wait
    while True:
        found = windows()
        if found is None: return None
        if all(window["number"] != number for window in found): return True
        if time.monotonic() >= deadline: return False
        time.sleep(STEP)


def dismiss(said=None, *, owner=None, record=None, wait=GONE):
    """Close the notifications of every Notification Center window on the screen, under the held foreground
    lease, and confirm each window gone; a report of what was found and closed, without what any notification
    says. With no such window it does nothing and needs no lease. It raises devices.Refused when a window is
    there and no lease is held, and Blocked, naming the window, when the window list or the window cannot be
    read, a notification is the owner's or cannot be closed, or the window stays on the screen."""
    said = said or (lambda text: print(text, file=sys.stderr, flush=True))
    found = windows()
    if found is None:
        raise Blocked("the window list cannot be read, so queuekeeper cannot tell whether a Notification Center window is "
                      "over the screen")
    report = {"found": [describe(window) for window in found], "closed": []}
    if not found: return report
    lease(owner, record)
    if not trusted():
        raise Blocked(f"{describe(found[0])} is over the screen, and this process is not trusted for accessibility "
                      "(AXIsProcessTrusted is false), so queuekeeper cannot close its notifications; the grant is the "
                      f"owner's ({GUIDE}, Setting up the second Mac's foreground)")
    for window in found:
        for _ in range(ROUNDS):
            presented = notifications(window["pid"])
            if presented is None:
                raise Blocked(f"{describe(window)} is over the screen, and System Events cannot read its notifications")
            if any(owners(notification) for notification in presented):
                raise Blocked(f"{describe(window)} presents a notification that asks for a password, permission or "
                              "access, which is the owner's to answer and queuekeeper never touches")
            targets = [notification for notification in presented if closable(notification)]
            # Anything else in the window, such as a stack, or the open panel's history and widgets, means it is
            # not only banners and alerts over the screen: queuekeeper closes nothing then, so no history entry goes.
            others = [notification for notification in presented if not closable(notification)]
            if not targets or others:
                kinds = ", ".join(shown(notification) for notification in others or presented) or "no notification"
                raise Blocked(f"{describe(window)} is over the screen with something queuekeeper may not close ({kinds}); "
                              "queuekeeper closes only banners and alerts with their own Close action, and only when the "
                              "window presents nothing else, never Clear All")
            for notification in targets:
                if not close(window["pid"], notification):
                    raise Blocked(f"{describe(window)}: System Events did not close its {shown(notification)}")
                said(f"queuekeeper: closed a Notification Center {shown(notification)} in {describe(window)}")
                report["closed"].append(f"{shown(notification)} in window {window['number']}")
            left = gone(window["number"], wait)
            if left is None:
                raise Blocked(f"the window list cannot be read after closing {describe(window)}'s notifications, so "
                              "queuekeeper cannot confirm it gone")
            if left: break
        else:
            raise Blocked(f"{describe(window)} stays on the screen after {ROUNDS} rounds of closing its notifications")
    return report


def main(argv=None):
    """`python3 -m queuekeeper.apple.notification_center list|check`: list says which Notification Center windows
    are over this Mac's screen, exiting 0 when none is and lane_results.UNMEASURED when one is; check, under the
    second Mac's foreground lease the caller holds, closes their notifications as a run does before it starts,
    exiting 0 once none is left, lane_results.UNMEASURED naming one it could not clear, and 1 without a lease."""
    argv = sys.argv[1:] if argv is None else argv
    if argv not in (["list"], ["check"]):
        print("usage: python3 -m queuekeeper.apple.notification_center list|check", file=sys.stderr)
        return 2
    said = lambda text: print(text, file=sys.stderr, flush=True)
    if argv == ["list"]:
        found = windows()
        if found is None:
            said("queuekeeper: the window list cannot be read")
            return 1
        for window in found: print(describe(window))
        if not found: print("no Notification Center window is over the screen")
        return lane_results.UNMEASURED if found else 0
    try:
        report = dismiss(said)
    except devices.Refused as error:
        said(f"queuekeeper: {error}")
        return 1
    except Blocked as error:
        said(f"queuekeeper: unmeasured: {error} ({GUIDE}, {SECTION})")
        return lane_results.UNMEASURED
    if not report["found"]: said("queuekeeper: no Notification Center window is over the screen")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
