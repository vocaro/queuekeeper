"""The owner's notifications during a run on his iPhone or iPad (vocaro-engineering#144).

The paired iPhone is the owner's personal phone and the iPad gets his notifications too. A notification that
arrives mid-session moves VoiceOver's cursor and puts the owner's words in the transcript, so the session is
discarded: on 2026-11-04 one spoiled a session in an approved iPhone window. A run silences them for itself and
puts back what was there before, as foreground runs do with the Mac's crash-report dialogs (#118).

Nothing documented sets a device's Focus from the Mac. Xcode 27's `devicectl` (651.32) sets appearance, audio,
biometrics, StandBy and VoiceOver, and reads no Focus; its `notification` commands post Darwin notifications, not
user ones. The notification-settings configuration profile payload needs a supervised device, which the owner's
are not. So the owner keeps two shortcuts on each device, made once by hand (guides/device-testing.md,
Notifications during a run), and queuekeeper runs them by opening Shortcuts with a `shortcuts://run-shortcut` URL through
`devicectl device process launch --payload-url`: SILENCE turns Do Not Disturb on when no Focus is on and leaves a
marker, and RESTORE turns it off only when that marker is there, so it puts back what was there before and does
nothing twice. The Mac reads no device's Focus, so queuekeeper never confirms the silencing: it records that it asked and
how devicectl answered. devices.py records each step in the reservation entry and runs RESTORE at release or at
attended recovery.

A notification can still get through: the shortcuts were never made, the owner's Do Not Disturb lets someone
through, or the silencing failed unseen. So `devices run` also watches the device's own log while its command
runs (Watch): idevicesyslog relays SpringBoard's lines, over USB and over the network at once since either may
be how the device is reached, into a temporary directory only this account can read. When the command ends, the
lines are scanned for SpringBoard presenting a banner and deleted unread otherwise; nothing from them is printed
or recorded except how many presentations there were and the device's clock at the first, because what they
say is the owner's. A run that saw one is unmeasured, and devices.py runs it once more. A run sent to the second
Mac with `queuekeeper remote --device` is watched the same way from the Mac that sends it, which holds its reservation
(remote.Lease, vocaro-engineering#192), every leased device at once (Together).
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
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
    """What a session asks the owner, in the message that agrees the window, when queuekeeper cannot silence key."""
    return (f"ask the owner, in the same message as the window, to turn on Do Not Disturb on the {key} for the run "
            f"and to say when it is on; record it with `devices notifications by-owner {key}`. Making the "
            f"{SILENCE!r} and {RESTORE!r} shortcuts on the {key} once lets queuekeeper do it ({GUIDE})")


# The relay of a device's log, from libimobiledevice, which guides/device-testing.md already uses to read the
# test daemons; "off" in WATCH starts none, as tests set it for every process they start (host_isolation).
RELAY = "idevicesyslog"
WATCH = "VOCARO_DEVICE_LOG_WATCH"
# A line as idevicesyslog prints it: the device's clock, then the process, with the library that logged in
# parentheses, its pid and the level: `Nov  5 17:03:47.851141 SpringBoard(UserNotificationsKit)[27395] <Notice>: …`,
# as it printed the owner's iPhone's on iOS 27.2 (2026-11-05). Some versions put the device's name, which may
# have spaces, before the process; that is allowed too.
LINE = re.compile(r"^(?P<month>[A-Z][a-z]{2}) +\d{1,2} (?P<clock>\d\d:\d\d:\d\d)(?:\.\d+)? (?:\S.*? )??"
                  r"(?P<process>[^\s(\[]+)(?:\([^)]*\))?\[\d+\] <\w+>: ", re.M)
# SpringBoard showing a notification as a banner, which it logs once per banner, after the banner appears:
# `Destination BulletinDestinationBanner presentable did appear as banner: <SBNotificationPresentableViewController
# …>`. Observed on the owner's iPhone (iOS 27.2) on 2026-11-05 (vocaro-engineering#144). Posting a notification to
# SBNotificationBannerDestination is not one: a notification Do Not Disturb holds back is posted there too and
# then logged as `Banner for notification ID was dropped. suppressed: 1 … do not disturb: 1`.
PRESENTED = re.compile(r"\bpresentable did appear as banner\b")
# Seconds the relays have to send their first line before the command starts, and to end when asked.
READY, ENDING = 3, 5
# What runs each relay: it ends the relay, and deletes what it wrote, once the process that started it is gone,
# so that a devices run killed outright leaves no relay copying the owner's log.
GUARD = """
import os, shutil, signal, subprocess, sys, time
parent, directory, command = int(sys.argv[1]), sys.argv[2], sys.argv[3:]
child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
def end(*_):
    child.terminate()
    try: child.wait(5)
    except subprocess.TimeoutExpired: child.kill()
    raise SystemExit(0)
signal.signal(signal.SIGTERM, end)
signal.signal(signal.SIGINT, signal.SIG_IGN)
while child.poll() is None:
    if os.getppid() != parent:
        shutil.rmtree(directory, True)
        end()
    time.sleep(1)
"""


def relay(udid, network):
    """The command that relays SpringBoard's lines from udid's log, reached over the network or over USB; -x ends it
    when the device goes, so that a relay that lost the device is seen to have stopped."""
    return [RELAY, "-u", udid, *(["-n"] if network else []), "-x", "--no-colors", "-p", "SpringBoard"]


# What idevicesyslog prints when it reaches the device and when it loses it (libimobiledevice 1.4.0).
CONNECTED, DISCONNECTED = "[connected:{}]", "[disconnected:{}]"


def presentations(text):
    """The device clock at each banner SpringBoard presented in text, a relay's lines; nothing else of them."""
    found = []
    for line in text.splitlines():
        match = LINE.match(line)
        if match and match["process"] == "SpringBoard" and PRESENTED.search(line[match.end():]):
            found.append(match["clock"])
    return found


def lines(text):
    """Whether text holds any line of the device's log, which shows the relay reached it."""
    return LINE.search(text) is not None


def reached(text, udid):
    """Whether a relay's text shows it reached udid: its connection, or a line of the log."""
    return CONNECTED.format(udid) in text or lines(text)


class Watch:
    """The device udid's log, relayed while a run lasts and read once for notifications presented (#144)."""

    def __init__(self, udid):
        self.udid, self.relays, self.failure, self.directory = udid, [], None, None
        if os.environ.get(WATCH) == "off":
            self.failure = f"{WATCH} is off"
            return
        if shutil.which(RELAY) is None:
            self.failure = f"{RELAY} is not installed (brew install libimobiledevice)"
            return
        # mkdtemp makes it readable by this account alone, and each file in it is made so too.
        self.directory = tempfile.mkdtemp(prefix="vocaro-device-log-")
        for network in (False, True):
            path = os.path.join(self.directory, "network" if network else "usb")
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                process = subprocess.Popen([sys.executable, "-I", "-c", GUARD, str(os.getpid()), self.directory,
                                            *relay(udid, network)], stdin=subprocess.DEVNULL, stdout=descriptor,
                                           stderr=subprocess.DEVNULL)
            except OSError as error:
                self.failure = str(error)
                continue
            finally:
                os.close(descriptor)
            self.relays.append((path, process))
        # Wait for every relay still running, not the first: over USB the owner's iPhone answered at once and over the
        # network 0.42 s later (2026-11-06, vocaro-engineering#194), and a relay that is waited for reads the whole
        # run. One that cannot reach the device costs the whole of READY.
        deadline = time.monotonic() + READY
        while self.relays and time.monotonic() < deadline and not all(
                process.poll() is not None or reached(self._read(path), udid) for path, process in self.relays):
            time.sleep(0.1)
        # Which relays had reached the device when the command starts: one that reaches it later missed the start.
        self.ready = {path for path, _ in self.relays if reached(self._read(path), udid)}

    @staticmethod
    def _read(path):
        try:
            with open(path, errors="replace") as handle: return handle.read()
        except OSError:
            return ""

    def stop(self):
        """End the relays, read what they relayed for presentations, and delete it: what the run's entry records,
        {"watched", "presented", "first", "transport"} and why it was not watched, with nothing the lines say.

        The run is watched only through a relay that had reached the device before the command started, relayed its
        log, and was still relaying it when the command ended: one that lost the device, or ended, during the run,
        or reached it only after the run had started, leaves part of the run unread (vocaro-engineering#194)."""
        ended = {path for path, process in self.relays if process.poll() is not None}
        # Whether each lost the device is read before ending it: idevicesyslog prints `[disconnected:UDID]` when
        # SIGTERM ends it too (libimobiledevice 1.4.0, 2026-11-06, vocaro-engineering#194).
        lost_before = {path for path, _ in self.relays if DISCONNECTED.format(self.udid) in self._read(path)}
        for _path, process in self.relays:
            if process.poll() is None: process.terminate()
        for _path, process in self.relays:
            try: process.wait(ENDING)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        texts = {path: self._read(path) for path, _ in self.relays}
        if self.directory: shutil.rmtree(self.directory, True)
        # The same line can come over both relays.
        found = sorted(set(clock for text in texts.values() for clock in presentations(text)))
        name = lambda path: os.path.basename(path)
        relayed = [path for path, text in texts.items() if lines(text)]
        late = [path for path in relayed if path not in self.ready]
        lost = [path for path in relayed if path in ended or path in lost_before]
        whole = [path for path in relayed if path not in late and path not in lost]
        seen = {"watched": bool(whole), "presented": len(found)}
        if found: seen["first"] = found[0]
        if whole: seen["transport"] = "+".join(name(path) for path in whole)
        if not seen["watched"]:
            if not relayed:
                seen["detail"] = self.failure or f"{RELAY} relayed no line of {self.udid}'s log over USB or the network"
            else:
                seen["detail"] = "; ".join(
                    [f"its {name(path)} relay reached {self.udid} only after the run had started" for path in late]
                    + [f"its {name(path)} relay lost {self.udid} during the run" for path in lost if path not in late])
        return seen


# What watches a run's device, alone, so that tests replace it.
watch = Watch


class Together:
    """The watches of several devices' logs, {KEY: watch}, stopped as one for a run that uses them all, as a run
    sent to the second Mac may (remote.Lease). Its stop says what Watch.stop does, over all of them: every
    device's banners counted, the earliest device clock among their first banners, watched only when each log
    was read, and what each saw under devices."""

    def __init__(self, watches):
        self.watches = dict(watches)

    def stop(self):
        each = {key: watch.stop() for key, watch in self.watches.items()}
        firsts = sorted(seen["first"] for seen in each.values() if seen.get("first"))
        seen = {"watched": all(seen["watched"] for seen in each.values()),
                "presented": sum(seen["presented"] for seen in each.values())}
        if firsts: seen["first"] = firsts[0]
        unread = [f"{key}: {seen.get('detail')}" for key, seen in each.items() if not seen["watched"]]
        if unread: seen["detail"] = "; ".join(unread)
        return {**seen, "devices": each}


def presented(seen):
    """What a session is told of the notifications a watch saw, without anything they said."""
    count = seen["presented"]
    return (f"{'a notification was' if count == 1 else f'{count} notifications were'} presented as a banner, the "
            f"first at {seen['first']} by the device's clock; what it said is the owner's and was not read")
