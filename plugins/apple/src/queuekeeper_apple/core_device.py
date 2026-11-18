"""Whether this Mac's CoreDeviceService answers for a physical device, asked once and bounded before devicectl or
XCTest work on it (vocaro/queuekeeper#227, doc/guides/devices.md, A stuck CoreDeviceService).

CoreDeviceService can get stuck while its devices stay reachable (apple-engineering's guides/device-testing.md,
Diagnosing a device): `devicectl list devices` still lists them, as `available (paired)` or `connecting`, but every
command to one hangs until its timeout. On 2026-11-17 a release's device pass found out only after XCTest's
30-minute allowance and the pass's bound, about 90 minutes, and the owner's notifications were not put back after
a 120 s wait. One `devicectl device info details` per device, bounded at TIMEOUT, tells a stuck service from a
reachable device: on a healthy service it answered in 0.19 to 0.23 s for the owner's iPhone and iPad on a cable,
and for a device CoreDevice does not know it refused in 0.14 s (2026-11-18, Xcode 27.2).

The fix is to restart CoreDeviceService, which breaks every device session on the Mac. Queuekeeper names it and does
not take it: whether to restart the service by itself when no device run holds the Mac is the owner's to decide
(vocaro/queuekeeper#227)."""
import os
import subprocess
import time

from . import reach

# Seconds devicectl has to answer for the device, and the wait for devicectl itself beyond that.
TIMEOUT = 30
GRACE = 15
# "off" answers that every device's service answers, with no devicectl, as tests set it for every process they
# start (host_isolation).
PROBE = "QUEUEKEEPER_DEVICE_PROBE"
GUIDE = "apple-engineering's guides/device-testing.md, Diagnosing a device"
RESTART = "pkill -x CoreDeviceService"


def command(udid):
    return ["xcrun", "devicectl", "--timeout", str(TIMEOUT), "device", "info", "details", "--device", udid,
            "--quiet", "--json-output", "/dev/null"]


def execute(arguments, clock=time.monotonic):
    """Run arguments; (exit status, the end of what they said), or (None, why) when they did not answer within
    TIMEOUT, by devicectl's own timeout or by the wait beyond it."""
    started = clock()
    try:
        done = subprocess.run(arguments, capture_output=True, text=True, timeout=TIMEOUT + GRACE,
                              stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return None, f"no answer within {TIMEOUT + GRACE}s"
    except OSError as error:
        return 127, str(error)
    said = " ".join((done.stderr or done.stdout or "").split())[-300:]
    if done.returncode and clock() - started >= TIMEOUT:
        return None, f"no answer within {TIMEOUT}s: {said or 'no output'}"
    return done.returncode, said


# What reaches the device, and what lists this Mac's devices, alone, so that tests replace them.
run = execute
listing = lambda: reach.read_here()[0]


def check(udid, key=None):
    """None when this Mac's CoreDeviceService answers for the physical device udid; otherwise why no devicectl or
    XCTest work should start on it, with the remedy. key names the device in what it says."""
    if os.environ.get(PROBE) == "off":
        return None
    name = key or udid
    status, said = run(command(udid))
    if status == 0:
        return None
    if status is not None:
        return (f"devicectl could not reach {name} (`devicectl device info details` exit {status}: "
                f"{said or 'no output'}); check that it is unlocked, trusted and on its cable or network, then "
                f"take it again")
    try:
        listed = udid.upper() in listing()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        listed = None
    if listed is False:
        return (f"devicectl does not list {name}, and `devicectl device info details` had {said}; check that it is "
                f"unlocked, trusted and on its cable or network, then take it again")
    return (f"this Mac's CoreDeviceService looks stuck: devicectl {'lists' if listed else 'may list'} {name}, but "
            f"`devicectl device info details` had {said}, so its run would only wait out devicectl's and XCTest's "
            f"timeouts. Restarting the service fixes it but breaks every device session on this Mac: once no other "
            f"device run is in progress here, quit Device Hub and run `{RESTART}` ({GUIDE}); Queuekeeper does not "
            f"restart it itself")
