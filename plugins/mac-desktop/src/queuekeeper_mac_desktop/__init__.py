"""Queuekeeper's Mac desktop plugin: what a run needs of a Mac's logged-in session when nobody is at it. It holds a
Mac's foreground for one run at a time, clears the crash-report alerts and notifications over its screen, keeps the
login keychain unlocked, reports its screen-capture approvals, and keeps the display awake for a lane that says
"display": true. The plugins for what is built and tested there, such as the Apple one, require it. It imports
the core; the core never imports it."""


def register():
    """Add the plugin to the core (queuekeeper.plugins.load)."""
    from . import display, lane
    display.register()
    lane.register()
