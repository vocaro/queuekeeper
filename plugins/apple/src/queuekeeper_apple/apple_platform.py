"""The Apple platform, as the shared mobile device code asks it (queuekeeper_mobile.platforms): how an iPhone or iPad
is named, silenced and watched, whether CoreDeviceService answers for it, and how a Mac lists the ones it reaches.

A physical device is named by its hardware UDID, 8 and 16 hex digits or 40 on older devices, uppercased; a
simulator by its UUID. The owner's notifications are kept out by his two shortcuts (device_notifications), which
the Mac cannot confirm, and a run is watched by idevicesyslog for a banner.
"""
import re

from queuekeeper_mobile import devices, platforms
from . import core_device, device_listing, device_notifications

# A hardware UDID, or a simulator's UUID, uppercased.
UDID = re.compile(r"[0-9A-F]{8}(?:-[0-9A-F]{4,16}){1,4}|[0-9A-F]{40}")
# A physical device's hardware UDID: 8 and 16 hex digits, or 40 on older devices. A simulator's is a UUID.
PHYSICAL = re.compile(r"[0-9A-F]{8}-[0-9A-F]{16}|[0-9A-F]{40}")
# A hardware UDID as a word of uppercased text, not part of a longer run of hex digits such as a SHA-256.
NAMED_PHYSICAL = re.compile(rf"(?<![0-9A-F])(?:{PHYSICAL.pattern})(?![0-9A-F])")
# What comes, in uppercased text, right before forty hex digits that name a code-signing identity by its
# SHA-1, as codesign, xcodebuild and security take one: a setting or option whose name ends in IDENTITY,
# such as CODE_SIGN_IDENTITY=, SIGN_IDENTITY= or --identity, or codesign's -s or --sign (#86).
SIGNING = re.compile(r"""(?:IDENTITY=|--IDENTITY\s+|\bCODESIGN\b[^;&|\n]*?\s(?:-S|--SIGN)(?:=|\s+))["']?\Z""")
# The paired devices `devices take` waits in line for, by their entries' keys.
PAIRED = ("iPad", "iPhone")


class Apple(platforms.Platform):
    name = "apple"
    paired = PAIRED
    identifier_name = "UDID"
    guide = device_notifications.GUIDE

    def physical(self, identifier):
        return isinstance(identifier, str) and PHYSICAL.fullmatch(identifier.upper()) is not None

    def virtual(self, identifier):
        return isinstance(identifier, str) and UDID.fullmatch(identifier.upper()) is not None \
            and not self.physical(identifier)

    def canonical(self, identifier):
        return identifier.upper()

    def named(self, text, root=None):
        """Every hardware UDID in text, except forty hex digits that a signing identity's setting or option takes
        (SIGNING) or that the Git of root's checkout knows as an object (devices.git_object): an older device's UDID,
        a commit's whole SHA-1 and a code-signing identity's look alike."""
        return {match.group() for match in NAMED_PHYSICAL.finditer(text) if len(match.group()) != 40 or not (
            SIGNING.search(text, 0, match.start()) or devices.git_object(match.group(), root))}

    def health(self, identifier, key):
        return core_device.check(identifier, key)

    def silence(self, record, key, entry, value, *, owner, revision, said):
        """Run the owner's silencing shortcut on key's device when he has recorded making it on that device, or else
        say what to ask him. The Mac reads no Focus, so the result is never confirmed."""
        setup = value.get(devices.SETUP, {}).get(key)
        if not setup or setup.get("device") != entry["device"].upper():
            why = (f"the owner has recorded no notification shortcuts on {key}'s device" if not setup else
                   f"the owner's notification shortcuts were recorded on another device under {key}")
            # Nothing was done, so the entry stays as it was; a run on it says it is not silenced.
            said(f"devices: {key}'s notifications are not silenced: {why}. A notification can reach a session's "
                 f"transcript and move VoiceOver's cursor: {self.ask(key)}")
            return entry
        entry = record.notifications(key, owner=owner, revision=revision, launched=[device_notifications.APP],
                                     value={"silence": "requesting", "restore": "pending", "at": devices.now()})
        opened, detail = device_notifications.trigger(entry["device"], device_notifications.SILENCE)
        entry = record.notifications(key, owner=owner, revision=entry["revision"],
                                     value={"silence": "requested" if opened else "failed", "detail": detail,
                                            "confirmed": False})
        if opened:
            said(f"devices: ran the owner's {device_notifications.SILENCE!r} shortcut on {key}, which turns on Do Not "
                 f"Disturb unless a Focus is already on; the Mac cannot read a device's Focus, so it is not confirmed")
        else:
            said(f"devices: {key}'s notifications are not silenced: {detail}. Retry with `devices notifications "
                 f"silence {key}` once the device is unlocked and reachable, or {self.ask(key)}")
        return entry

    def restore(self, key, entry, said):
        opened, detail = device_notifications.trigger(entry["device"], device_notifications.RESTORE)
        if opened:
            said(f"devices: ran the owner's {device_notifications.RESTORE!r} shortcut on {key}, which turns Do Not "
                 "Disturb off only if the run's silencing turned it on")
        else:
            said(f"devices: could not put back the owner's notifications on {key}: {detail}. Do Not Disturb may still "
                 f"be on there: tell the owner, who can run {device_notifications.RESTORE!r} or turn it off himself")
        return {"restore": "done" if opened else "failed", "restoreDetail": detail}

    def silenced_words(self, notifications):
        return "silencing shortcut run, not confirmed (the Mac reads no Focus)"

    def watch(self, identifier):
        # Looked up at each call, so that tests replace it (host_isolation).
        return device_notifications.watch(identifier)

    def presented(self, seen):
        return device_notifications.presented(seen)

    def ask(self, key):
        return device_notifications.ask(key)

    def setup(self, key, entry):
        return {"silence": device_notifications.SILENCE, "restore": device_notifications.RESTORE}

    def listing_here(self):
        return device_listing.read_here()[0]

    def listing_there(self, host):
        return device_listing.read_there(host)[0]

    def connections(self):
        from .observations import read_connections
        return read_connections()

    def pool(self, root):
        from .simulators import pool
        return pool.configured(root)


PLATFORM = Apple()
# Whatever imports this module uses the record as the Apple plugin does, naming iPhones, iPads and simulators by their
# UDIDs, so the platform is switched on here: a script or a child process that imports the plugin's modules without
# its register has it all the same.
platforms.register(PLATFORM)
