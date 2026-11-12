"""A project's "unity" key in queuekeeper.json: where its Unity project is, and the window layout a new worktree's
editor starts with.

    "unity": {"project": "lab", "layout": "tools/unity/layouts/worktree-default.dwlt"}

Both are paths relative to the checkout. "project" is the folder that holds ProjectSettings, and is the checkout
itself when left out."""
from pathlib import Path, PurePosixPath

from .. import extensions

KEY = "unity"
KEYS = {"project", "layout"}


def check(value):
    """Raise ValueError when value is not what "unity" takes."""
    if not isinstance(value, dict) or set(value) - KEYS:
        raise ValueError(f"{KEY} takes {' and '.join(sorted(KEYS))}")
    for key, path in value.items():
        parts = PurePosixPath(path).parts if isinstance(path, str) else ()
        if not parts or ".." in parts or PurePosixPath(path).is_absolute():
            raise ValueError(f"{KEY}.{key} must be a path within the checkout, such as \"lab\"")


def project(root, config):
    """The Unity project's folder in the checkout at root."""
    return Path(root) / (config or {}).get(KEY, {}).get("project", ".")


def layout(root, config):
    """The window layout a new worktree's editor starts with, or None when the project names none."""
    named = (config or {}).get(KEY, {}).get("layout")
    return Path(root) / named if named else None


def register():
    """Add the key to the core."""
    extensions.CONFIG_KEYS[KEY] = check
