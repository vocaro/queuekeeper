"""TalkBack's state on a device or emulator, kept across a run that may toggle it.

TalkBack is on when its service is among the secure setting enabled_accessibility_services and
accessibility_enabled is 1. A run that turns it on for an accessibility test and dies leaves it on, and the next
run's taps then go through TalkBack's gestures, so a run's guard reads both settings first and writes back what it
read, as the Apple plugin does for the Mac's VoiceOver (voiceover.state). Google's own TalkBack ships in
com.google.android.marvin.talkback; a vendor build may name another, which `--service` names.
"""
import contextlib

from . import adb

SERVICE = "com.google.android.marvin.talkback/com.google.android.marvin.talkback.TalkBackService"
SERVICES, ENABLED = "enabled_accessibility_services", "accessibility_enabled"


def read(serial):
    """{"services", "enabled"}: the device's two settings as they stand, "" for an unset one."""
    services = adb.shell(serial, "settings", "get", "secure", SERVICES, timeout=15)
    enabled = adb.shell(serial, "settings", "get", "secure", ENABLED, timeout=15)
    unset = lambda value: "" if value in ("null", "") else value
    return {"services": unset(services), "enabled": unset(enabled)}


def on(state, service=SERVICE):
    return state.get("enabled") == "1" and service in (state.get("services") or "").split(":")


def write(serial, state):
    """Set both settings to state's; whether they read back as written."""
    for name, value in ((SERVICES, state.get("services")), (ENABLED, state.get("enabled"))):
        if value: adb.shell(serial, "settings", "put", "secure", name, value, timeout=15)
        else: adb.shell(serial, "settings", "delete", "secure", name, timeout=15, check=False)
    return read(serial) == {"services": state.get("services") or "", "enabled": state.get("enabled") or ""}


def toggled(state, wanted, service=SERVICE):
    """state with TalkBack's service added or taken out, as wanted says."""
    services = [name for name in (state.get("services") or "").split(":") if name and name != service]
    if wanted: services.append(service)
    return {"services": ":".join(services), "enabled": "1" if wanted or services else "0"}


@contextlib.contextmanager
def preserved(serial, said=print):
    """Read TalkBack's state before the body and write it back after, saying when it could not be put back."""
    original = read(serial)
    try:
        yield original
    finally:
        try:
            if read(serial) != original and not write(serial, original):
                said(f"talkback: {serial}'s accessibility settings did not read back as they were before the run: "
                     f"{original}")
        except adb.Failed as error:
            said(f"talkback: could not put back {serial}'s accessibility settings ({error}); they were {original}")
