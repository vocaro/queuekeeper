"""Queuekeeper's Mac hardware plugin: a lane's "chips", the chips its results are valid on. A lane whose output
differs by GPU, such as one that compares rendered images byte for byte, lists them, and runs only on a Mac that
has one. A lane without the key runs on any Mac, as before. It imports the core; the core never imports it."""


def register():
    """Add the plugin to the core (queuekeeper.plugins.load)."""
    from . import lane
    lane.register()
