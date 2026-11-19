"""Queuekeeper's Unity plugin: running a Unity project's editor unattended on a shared Mac. It picks the editor
the project pins, clears a project lock nothing holds, stops an orphaned licensing helper, ends a batch run that has
gone silent, and gives a project the commands `qk unity closed`, `test` and `open` (vocaro/queuekeeper#6).
It builds on the Mac desktop plugin, which guards the Mac's session. It imports the core; the core never imports
it."""
# The plugins this one builds on, which load first (queuekeeper.plugins.load).
REQUIRES = ("queuekeeper_mac_desktop",)


def register():
    """Add the plugin to the core (queuekeeper.plugins.load)."""
    from . import commands, disk, doctor, host, lane, results, settings
    settings.register()
    commands.register()
    doctor.register()
    disk.register()
    lane.register()
    host.register()
    results.register()
