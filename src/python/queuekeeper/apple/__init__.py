"""Queuekeeper's Apple extension: simulator pools, device reservations, capture approvals, the iOS test runners and
the guards for unattended runs. It imports the core; the core never imports it."""


def register():
    """Add the extension to the core (queuekeeper.extensions.load)."""
    from . import disk, doctor, lane, leases
    disk.register()
    doctor.register()
    lane.register()
    leases.register()
