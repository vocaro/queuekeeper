"""Queuekeeper's Android plugin: an emulator pool, a runner that builds instrumented tests once and runs them on
several emulators side by side, and the Android phones and tablets the shared device record reserves. It builds on
the mobile plugin, which holds the device record, bounded runs and the device pass for every platform, and on the
Mac desktop plugin beneath it. It imports the core; the core never imports it."""
# The plugins this one builds on, which load first (queuekeeper.plugins.load).
REQUIRES = ("queuekeeper_mac_desktop", "queuekeeper_mobile")


def register():
    """Add the plugin to the core (queuekeeper.plugins.load)."""
    from . import disk, doctor, lane, settings
    settings.register()
    doctor.register()
    lane.register()
    disk.register()
