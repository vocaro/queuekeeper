"""Compose environment wrappers supplied by enabled extensions."""
from . import extensions


def environment(base, root):
    values = dict(base)
    for wrap in extensions.COMMAND_WRAPPERS:
        values = wrap(values, root)
    return values
