"""Queuekeeper's Android extension: an emulator pool, a runner that builds instrumented tests once and runs them on
several emulators side by side, and the Android phones and tablets the shared device record reserves. It builds on
the mobile extension, which holds the device record, bounded runs and the device pass for every platform, and on the
Mac desktop extension beneath it. It imports the core; the core never imports it."""
# The extensions this one builds on, which load first (queuekeeper.extensions.load).
REQUIRES = ("queuekeeper_mac_desktop", "queuekeeper_mobile")


def register():
    """Add the extension to the core (queuekeeper.extensions.load)."""
    from . import doctor, lane, settings
    settings.register()
    doctor.register()
    lane.register()
