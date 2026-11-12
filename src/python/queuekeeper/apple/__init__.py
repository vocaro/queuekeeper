"""Queuekeeper's Apple extension: simulator pools, device reservations and the iOS test runners. It builds on the
Mac desktop extension, which guards an unattended Mac's session. It imports the core; the core never imports it."""
# The extensions this one builds on, which load first (queuekeeper.extensions.load).
REQUIRES = ("queuekeeper.mac_desktop",)


def register():
    """Add the extension to the core (queuekeeper.extensions.load)."""
    from . import disk, doctor, lane
    disk.register()
    doctor.register()
    lane.register()
