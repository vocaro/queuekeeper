"""The Android platform, as the shared mobile device code asks it (queuekeeper_mobile.platforms): how an Android
phone or tablet is named, silenced and watched, whether adb answers for it, and how a Mac lists the ones it reaches.

A physical device is named by its adb serial, exactly as `adb devices` prints it: the serial number of a device on a
cable, or the address or wireless-debugging service name of one on the network. An emulator in a project's pool is
named in the record as avd:NAME, by its Android Virtual Device, since the serial adb gives an emulator, emulator-PORT,
lasts only while it runs. A serial is case-sensitive, so the record keeps it as adb prints it.

The physical-device paths were written without an Android phone to try them on (2026-11-18): they follow adb's and
Android's documented behavior, and the emulator paths were tried on this Mac's emulators.
"""
import os
from pathlib import Path
import re
import subprocess

from queuekeeper_mobile import devices, platforms
from . import adb, device_notifications, sdk

# The paired devices `devices take` waits in line for, by their entries' keys.
PAIRED = ("AndroidPhone", "AndroidTablet")
# An emulator pool member's identifier in the record.
AVD = re.compile(r"avd:(?P<name>[A-Za-z0-9._ -]+)")
# A serial on a cable: letters and digits, eight to thirty-two of them, two digits among them at least, so that a
# word such as a device's name is never taken for one; never forty hex digits alone, which are an older iPhone's UDID.
SERIAL = re.compile(r"(?=(?:[^0-9]*[0-9]){2})[A-Za-z0-9]{8,32}")
UDID_LIKE = re.compile(r"[0-9A-Fa-f]{40}")
# A file holding `adb devices -l` output that this Mac's reading takes in place of adb's, for tests only: the test
# suite sets it to an empty file (tests/host_isolation.py), so no test reads this Mac's devices.
LISTING = "QUEUEKEEPER_ADB_LISTING"
# What lists another Mac's devices over SSH, in its login shell: its SDK's adb wherever that Mac keeps it, and an
# empty listing from a Mac with none, which reaches no Android device.
THERE = ('for a in "${ANDROID_HOME:-/nonexistent}/platform-tools/adb" '
         '"${ANDROID_SDK_ROOT:-/nonexistent}/platform-tools/adb" "$HOME/Library/Android/sdk/platform-tools/adb" '
         '/opt/homebrew/share/android-commandlinetools/platform-tools/adb '
         '/usr/local/share/android-commandlinetools/platform-tools/adb "$(command -v adb)"; do '
         '[ -x "$a" ] && exec "$a" devices -l; done; exit 0')
TIMEOUT = 30


def physical(serial):
    """Whether serial names a physical Android device: a cable's serial number, an address, or a wireless-debugging
    service name."""
    if not isinstance(serial, str) or adb.emulator_port(serial) is not None or UDID_LIKE.fullmatch(serial):
        return False
    return bool(adb.NETWORK.fullmatch(serial) or adb.WIRELESS.fullmatch(serial) or SERIAL.fullmatch(serial))


def reached(text):
    """{serial: {"way", "transport", "kind", "model", "name", "state"}} of the physical devices `adb devices -l`
    text lists. A device that adb lists unauthorized or offline is listed, since the Mac reaches it, with its state."""
    found = {}
    for serial, device in adb.listed(text).items():
        if not physical(serial): continue
        found[serial] = {"way": device["way"], "transport": "usb" if device["way"] == "cable" else "tcp",
                         "kind": "android", "model": (device.get("model") or "").replace("_", " ") or None,
                         "name": device.get("device"), "state": device["state"]}
    return found


class Android(platforms.Platform):
    name = "android"
    paired = PAIRED
    identifier_name = "adb serial"
    guide = device_notifications.GUIDE

    def physical(self, identifier):
        return physical(identifier)

    def virtual(self, identifier):
        return isinstance(identifier, str) and AVD.fullmatch(identifier) is not None

    def health(self, identifier, key):
        """Why adb cannot work with the device now: it is not listed, or listed unauthorized or offline."""
        try:
            listed = adb.devices()
        except (adb.Failed, sdk.Missing) as error:
            return f"adb could not list devices: {error}"
        state = (listed.get(identifier) or {}).get("state")
        if state == "device": return None
        name = key or identifier
        if state is None:
            return f"adb does not list {name} ({identifier}); check its cable or wireless debugging"
        if state == "unauthorized":
            return f"{name} has not authorized this Mac for USB debugging: the owner accepts the prompt on it"
        return f"adb lists {name} as {state}"

    def silence(self, record, key, entry, value, *, owner, revision, said):
        """Turn on Do Not Disturb over adb and read it back; the mode before is recorded so that release puts it
        back. A device already in Do Not Disturb is left, as the owner's."""
        serial = entry["device"]
        entry = record.notifications(key, owner=owner, revision=revision,
                                     value={"silence": "requesting", "restore": "pending", "at": devices.now()})
        outcome = device_notifications.silence(serial)
        if outcome["silence"] == "owner":
            outcome = {**outcome, "restore": "not needed", "by": "the device", "at": devices.now()}
        entry = record.notifications(key, owner=owner, revision=entry["revision"], value=outcome)
        if outcome["silence"] == "requested":
            said(f"devices: turned on Do Not Disturb on {key} ({outcome['detail']})")
        elif outcome["silence"] == "owner":
            said(f"devices: {key} was already in Do Not Disturb ({outcome['before']}), so it is left as it is")
        else:
            said(f"devices: {key}'s notifications are not silenced: {outcome['detail']}. Retry with `devices "
                 f"notifications silence {key}`, or {self.ask(key)}")
        return entry

    def restore(self, key, entry, said):
        outcome = device_notifications.restore(entry["device"], entry.get("notifications"))
        if outcome["restore"] == "done":
            said(f"devices: put back Do Not Disturb on {key} as it was before the run ({outcome['restoreDetail']})")
        else:
            said(f"devices: could not put back Do Not Disturb on {key}: {outcome['restoreDetail']}. Tell the owner, "
                 f"who can turn it off himself")
        return outcome

    def silenced_words(self, notifications):
        return ("Do Not Disturb on, read back" if notifications.get("confirmed") else
                "Do Not Disturb asked for, not read back")

    def watch(self, identifier):
        return device_notifications.watch(identifier)

    def presented(self, seen):
        return device_notifications.presented(seen)

    def ask(self, key):
        return device_notifications.ask(key)

    def setup(self, key, entry):
        raise ValueError(f"{key} is an Android device, which needs no setup: queuekeeper sets Do Not Disturb over adb")

    def listing_here(self):
        if os.environ.get(LISTING) is not None:
            path = os.environ[LISTING]
            return reached(Path(path).read_text()) if path else {}
        try:
            return reached(adb.run(["devices", "-l"], timeout=TIMEOUT).stdout)
        except sdk.Missing:
            # A Mac with no Android SDK reaches no Android device.
            return {}

    def listing_there(self, host):
        from queuekeeper import remote
        done = remote.ssh(host, THERE, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=TIMEOUT)
        if done.returncode:
            said = (done.stderr or "").strip().splitlines()
            raise RuntimeError(f"its adb listing exited {done.returncode}" + (f": {said[-1][-300:]}" if said else ""))
        return reached(done.stdout)

    def connections(self):
        states = {"device": "connected", "unauthorized": "unavailable", "offline": "disconnected",
                  "connecting": "connecting", "authorizing": "connecting"}
        return {serial: {"connection": states.get(device["state"], "unknown"), "transport": device["transport"],
                         "kind": "other", "model": device["model"]}
                for serial, device in self.listing_here().items()}


PLATFORM = Android()
# Whatever imports this module names Android devices as the plugin does, so the platform is switched on here.
platforms.register(PLATFORM)
