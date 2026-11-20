"""Queuekeeper's shared mobile plugin: the device record of physical phones and tablets, its bounded runs, the
reservations a run sent to another Mac carries, the device pass, and which Mac reaches each device. The Apple and
Android plugins build on it, each registering the platform that names, silences and watches its own devices
(platforms.py). It imports the core and the Mac desktop plugin, whose record it adds devices to; the core never
imports it."""
# The plugins this one builds on, which load first (queuekeeper.plugins.load).
REQUIRES = ("queuekeeper_mac_desktop",)


def register():
    """Add the plugin to the core (queuekeeper.plugins.load)."""
    from . import doctor, lane, settings, upkeep
    from queuekeeper import plugins
    settings.register()
    doctor.register()
    lane.register()
    # What the device record has due by itself, looked at before each command that reaches the hosts (upkeep).
    if upkeep.spawn not in plugins.HOST_COMMAND: plugins.HOST_COMMAND.append(upkeep.spawn)
