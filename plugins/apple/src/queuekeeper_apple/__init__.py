"""Queuekeeper's Apple extension: simulator pools, device reservations and the iOS test runners. It builds on the
Mac desktop extension, which guards an unattended Mac's session. It imports the core; the core never imports it."""
# The extensions this one builds on, which load first (queuekeeper.extensions.load).
REQUIRES = ("queuekeeper_mac_desktop", "queuekeeper_mobile")


def register():
    """Add the extension to the core (queuekeeper.extensions.load)."""
    from . import disk, doctor, lane, settings, shims
    from queuekeeper import extensions
    if shims.environment not in extensions.COMMAND_WRAPPERS:
        extensions.COMMAND_WRAPPERS.append(shims.environment)
    settings.register()
    disk.register()
    doctor.register()
    lane.register()
