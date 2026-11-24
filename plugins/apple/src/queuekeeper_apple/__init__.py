"""Queuekeeper's Apple plugin: simulator pools, device reservations and the iOS test runners. It builds on the
Mac desktop plugin, which guards an unattended Mac's session. It imports the core; the core never imports it."""
# The plugins this one builds on, which load first (queuekeeper.plugins.load).
REQUIRES = ("queuekeeper_mac_desktop", "queuekeeper_mobile")


def register():
    """Add the plugin to the core (queuekeeper.plugins.load)."""
    from . import disk, doctor, flakes, lane, settings, shims, stale_toolchain, system_crash
    from queuekeeper import plugins
    if shims.environment not in plugins.COMMAND_WRAPPERS:
        plugins.COMMAND_WRAPPERS.append(shims.environment)
    if stale_toolchain.host_fault not in plugins.HOST_FAULTS:
        plugins.HOST_FAULTS.append(stale_toolchain.host_fault)
    if system_crash.crash not in plugins.SYSTEM_CRASHES:
        plugins.SYSTEM_CRASHES.append(system_crash.crash)
    settings.register()
    disk.register()
    doctor.register()
    lane.register()
    flakes.register()
