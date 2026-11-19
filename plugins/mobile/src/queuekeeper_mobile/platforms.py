"""The mobile platforms switched on in this process, and what the shared device code asks each of them.

The device record, its bounded runs, the device pass and the reach of each Mac are the same for an iPhone and an
Android phone; what differs is how a device is named, how its notifications are silenced and watched, whether its
platform's own service answers for it, and how a Mac lists the devices it reaches. Each platform plugin registers one
Platform here, in its register(), and the shared code asks the platform that owns a device's identifier.

An identifier is owned by the first platform, in the order registered, whose `physical` or `virtual` takes it. Apple's
hardware UDIDs and Android's adb serials do not overlap: a UDID has a dash or is forty hex digits, which no adb serial
of a cable or network device has.
"""


class Platform:
    """A mobile platform: what the shared device code asks of it. This base knows no device."""

    # A short name, such as "apple" or "android", which the record's status and refusals use.
    name = "none"
    # The keys of the paired devices `devices take` waits in line for, such as ("iPad", "iPhone").
    paired = ()
    # What the platform's devices call a device's identifier in the words a command takes, such as "UDID".
    identifier_name = "identifier"
    # Where the owner reads how the platform keeps notifications out of a run.
    guide = "plugins/mobile/src/queuekeeper_mobile/README.md, Platforms"

    def physical(self, identifier):
        """Whether identifier is a physical device of this platform's."""
        return False

    def virtual(self, identifier):
        """Whether identifier is a simulator or emulator of this platform's, as a pool's reservation records one."""
        return False

    def canonical(self, identifier):
        """The form of identifier the record keeps and a command names: an Apple UDID uppercased, an adb serial as
        adb prints it."""
        return identifier

    def named(self, text, root=None):
        """The physical devices of this platform that text, a command's words joined and uppercased, names by a
        pattern alone, in their canonical form; ones the record knows are found apart from this (devices.named).
        root is the checkout whose Git may say that a word is an object rather than a device (devices.git_object)."""
        return set()

    def health(self, identifier, key):
        """Why the platform's own service cannot work with the device now, or None when it can: asked once, bounded,
        before a device pass runs on it."""
        return None

    def silence(self, record, key, entry, value, *, owner, revision, said):
        """Silence the owner's notifications on key's physical device for the caller's run, recording each step in
        its entry (record.notifications), the restore as pending before the device is changed; the entry, as
        devices.silence_notifications returns it. value is the whole record as read."""
        said(f"devices: {key}'s notifications are not silenced: the {self.name} platform cannot silence them")
        return entry

    def restore(self, key, entry, said):
        """Put back the owner's notifications on entry's device, whose run left a restore pending; what to record in
        its notifications: {"restore": "done" or "failed", "restoreDetail"}."""
        return {"restore": "failed", "restoreDetail": f"the {self.name} platform cannot put them back"}

    def silenced_words(self, notifications):
        """What the record's status says of a run's silencing that was requested (notifications["silence"])."""
        return "silenced" + ("" if notifications.get("confirmed") else ", not confirmed")

    def watch(self, identifier):
        """A watch of the device's log for a notification that reached it while a run lasts: an object whose stop()
        says {"watched", "presented", "first"?, "transport"?, "detail"?}."""
        return Unwatched(f"the {self.name} platform watches no device's log")

    def presented(self, seen):
        """What a session is told of the notifications a watch saw, without anything they said."""
        count = seen["presented"]
        first = f", the first {seen['first']}" if seen.get("first") else ""
        return f"{count} notification{'s' if count != 1 else ''} reached the device during the run{first}"

    def ask(self, key):
        """What to ask the owner when key's notifications could not be silenced."""
        return f"ask the owner to turn on Do Not Disturb on {key}"

    def setup(self, key, entry):
        """The fields a record of the owner's one-time notification setup on key's device keeps, or a ValueError
        when the platform needs none."""
        raise ValueError(f"the {self.name} platform needs no notification setup")

    def listing_here(self):
        """{identifier: {"way", "transport", "kind", "model", "name"}} of the physical devices this Mac reaches now,
        where way is "cable", "network" or None (reach.listed)."""
        return {}

    def listing_there(self, host):
        """The same for the Mac at the SSH destination host."""
        return {}

    def connections(self):
        """{identifier: {"connection", "transport", "kind", "model"}} for the dashboard (observations)."""
        return {}

    def pool(self, root):
        """The project's pool of this platform's simulators or emulators, or None."""
        return None


class Unwatched:
    """A watch that read nothing, and says why."""

    def __init__(self, why):
        self.why = why

    def stop(self):
        return {"watched": False, "presented": 0, "detail": self.why}


class Together:
    """The watches of several devices' logs, {KEY: watch}, stopped as one for a run that uses them all, as a run
    sent to another Mac may (leases.Lease). Its stop says what one watch's stop does, over all of them: every
    device's notifications counted, the earliest first among them, watched only when each log was read, and what
    each saw under devices."""

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


PLATFORMS = []
NONE = Platform()
# The platforms shipped beside this extension, by the module that registers each when imported. A process that loads
# no extension, such as `python3 -m queuekeeper_mobile.devices defer` that another Mac runs before a dispatch, switches
# on each that is installed (discover), so that the record still knows an iPhone or an Android phone it holds.
SHIPPED = ("queuekeeper_apple.apple_platform", "queuekeeper_android.android_platform")


def discover():
    """Switch on each shipped platform whose plugin is installed, by importing the module that registers it."""
    import importlib
    import importlib.util
    for name in SHIPPED:
        if importlib.util.find_spec(name.split(".")[0]) is not None: importlib.import_module(name)


def register(platform):
    """Switch platform on, once per type."""
    if not any(type(each) is type(platform) for each in PLATFORMS): PLATFORMS.append(platform)


def of(identifier):
    """The platform that owns identifier, or the base, which knows no device."""
    if isinstance(identifier, str) and identifier:
        for platform in PLATFORMS:
            if platform.physical(identifier) or platform.virtual(identifier): return platform
    return NONE


def physical(identifier):
    """Whether identifier is a physical device of any platform switched on."""
    return isinstance(identifier, str) and any(platform.physical(identifier) for platform in PLATFORMS)


def known(identifier):
    """Whether any platform switched on owns identifier, as a physical device or a simulator or emulator."""
    return of(identifier) is not NONE


def canonical(identifier):
    return of(identifier).canonical(identifier) if isinstance(identifier, str) else identifier


def paired():
    """The keys `devices take` waits in line for, every platform's."""
    return tuple(key for platform in PLATFORMS for key in platform.paired)


def identifier_names():
    """What the platforms switched on call a device's identifier: "UDID", "adb serial", or both."""
    return " or ".join(dict.fromkeys(platform.identifier_name for platform in PLATFORMS)) or "identifier"
