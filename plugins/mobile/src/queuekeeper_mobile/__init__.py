"""Queuekeeper's shared mobile extension: the device record of physical phones and tablets, its bounded runs, the
reservations a run sent to another Mac carries, the device pass, and which Mac reaches each device. The Apple and
Android extensions build on it, each registering the platform that names, silences and watches its own devices
(platforms.py). It imports the core and the Mac desktop extension, whose record it adds devices to; the core never
imports it."""
# The extensions this one builds on, which load first (queuekeeper.extensions.load).
REQUIRES = ("queuekeeper_mac_desktop",)


def register():
    """Add the extension to the core (queuekeeper.extensions.load)."""
    from . import doctor, lane, settings
    settings.register()
    doctor.register()
    lane.register()
