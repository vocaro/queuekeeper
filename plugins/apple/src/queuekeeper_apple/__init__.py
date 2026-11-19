"""Queuekeeper's Apple plugin: simulator pools, device reservations and the iOS test runners. It builds on the
Mac desktop plugin, which guards an unattended Mac's session. It imports the core; the core never imports it."""
# The plugins this one builds on, which load first (queuekeeper.plugins.load).
REQUIRES = ("queuekeeper_mac_desktop", "queuekeeper_mobile")


def register():
    """Add the plugin to the core (queuekeeper.plugins.load)."""
    from . import disk, doctor, lane, settings, shims
    from queuekeeper import plugins
    if shims.environment not in plugins.COMMAND_WRAPPERS:
        plugins.COMMAND_WRAPPERS.append(shims.environment)
    settings.register()
    disk.register()
    doctor.register()
    lane.register()
