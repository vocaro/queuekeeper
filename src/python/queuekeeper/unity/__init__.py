"""Queuekeeper's Unity extension: running a Unity project's editor unattended on a shared Mac. It picks the editor
the project pins, clears a project lock nothing holds, stops an orphaned licensing helper, ends a batch run that has
gone silent, and gives a project the commands `queuekeeper unity closed`, `test` and `open` (vocaro/queuekeeper#6).
It builds on the Mac desktop extension, which guards the Mac's session. It imports the core; the core never imports
it."""
# The extensions this one builds on, which load first (queuekeeper.extensions.load).
REQUIRES = ("queuekeeper.mac_desktop",)


def register():
    """Add the extension to the core (queuekeeper.extensions.load)."""
    from . import commands, doctor, settings
    settings.register()
    commands.register()
    doctor.register()
