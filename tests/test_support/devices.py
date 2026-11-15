import contextlib


from datetime import datetime, timedelta, timezone


import fcntl


from io import StringIO


import json


import os


from pathlib import Path


import pwd


import re


import signal


import subprocess


import sys


import tempfile


import threading


import time


import unittest


from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[2] / "src/python"


from queuekeeper import cli, resources


from queuekeeper_apple import device_notifications, devices


from queuekeeper_mac_desktop import reservations


import host_isolation


from host_isolation import stretched


from queuekeeper.resources import process_start


PHONE = "00008130-001A2C3E0C38001C"


TABLET = "00008103-000E4C1A3E90801E"


DUO = "5B0E6C51-2D3A-4F7B-9C1E-0A4B8D2F6E13"


SANDBOX_EXEC = "/usr/bin/sandbox-exec"


DENY_PS = '(version 1)(allow default)(deny process-exec (literal "/bin/ps"))'


PROBE = ("import json, sys; entry = json.load(open(sys.argv[1]))['devices'][sys.argv[2]]; "
         "open(sys.argv[3], 'w').write(json.dumps(entry))")


WRITER = r"""
import contextlib, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from queuekeeper_apple import devices
path, key, ready, count, mode, release_by, window, wait = sys.argv[2:10]
record = devices.Record(path, timeout=float(wait))
if mode == "unlocked":
    record._exclusive = contextlib.nullcontext
load = record.load
def composing():
    value = load()
    time.sleep(float(window))
    return value
record.load = composing
Path(ready, key).touch()
while len(list(Path(ready).iterdir())) < int(count):
    time.sleep(0.005)
record.reserve(key, owner=key, revision=0, purpose="concurrent claim", release_by=release_by)
"""


def later(minutes=30):
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


def live():
    """A process that stands in for a live run: it outlives the test that ends it, however slowly the host runs it."""
    return subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({stretched(60)})"])


def approval(**changes):
    """The owner's approval of a foreground window: given a minute ago, for the next hour."""
    return {"by": "owner", "at": later(-1), "until": later(60), **changes}


class Later(datetime):
    """Two hours from now, for a deadline or an approval that has passed."""

    @classmethod
    def now(cls, tz=None):
        return datetime.now(tz) + timedelta(hours=2)


def scratch():
    """A test directory outside /private/tmp, which the record refuses, even when TMPDIR is there."""
    base = Path(tempfile.gettempdir())
    if devices.restart_emptied(base):
        if sys.platform == "darwin":
            base = Path(subprocess.check_output(["/usr/bin/getconf", "DARWIN_USER_TEMP_DIR"], text=True).strip())
        else:
            base = Path(__file__).resolve().parents[2] / ".build"
            base.mkdir(exist_ok=True)
    return tempfile.TemporaryDirectory(dir=base)


class Devicectl:
    """devicectl as the notification shortcuts reach it: each command it is given, answered by answers in turn."""

    def __init__(self, *answers):
        self.answers, self.commands = list(answers), []

    def __call__(self, command):
        self.commands.append(command)
        return self.answers.pop(0) if self.answers else (0, "")

    def shortcuts(self):
        """The shortcut each command had Shortcuts run, with the device it named."""
        named = []
        for command in self.commands:
            named.append((command[command.index("--device") + 1], command[command.index("--payload-url") + 1],
                          command[-1]))
        return named


LOGS = Path(__file__).resolve().parents[1] / "apple/fixtures/DeviceLog"


def alive(pid):
    try: os.kill(pid, 0)
    except ProcessLookupError: return False
    return True


class Watches:
    """device_notifications.watch as a run reaches it: each watch's stop answers what it saw, in turn."""

    def __init__(self, *seen):
        self.seen, self.devices = list(seen), []

    def __call__(self, udid):
        self.devices.append(udid)
        seen = self.seen.pop(0) if self.seen else {"watched": True, "presented": 0}
        return type("Watch", (), {"stop": lambda _self: seen})()


BANNER = {"watched": True, "presented": 1, "first": "00:52:13"}


NO_TESTS = ("Test Suite 'Selected tests' passed at 2026-11-11 08:33:58.699.\n"
            "\t Executed 0 tests, with 0 failures (0 unexpected) in 0.000 (0.001) seconds\n"
            "** TEST EXECUTE SUCCEEDED **\n")


SOME_TESTS = NO_TESTS.replace("Executed 0 tests", "Executed 3 tests")
