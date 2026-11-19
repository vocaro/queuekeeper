"""Compose environment wrappers supplied by enabled plugins."""
from . import plugins


def environment(base, root):
    values = dict(base)
    for wrap in plugins.COMMAND_WRAPPERS:
        values = wrap(values, root)
    return values
