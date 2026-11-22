"""Recognize package state that still names a farm checkout before it moved (#201)."""

import json
import os
from pathlib import Path
import stat

NOTICE = "queuekeeper: STALE PACKAGE PATHS"
STATE = "workspace-state.json"
STATE_LIMIT = 4 * 1024 * 1024


def option(arguments, name):
    """The last explicit value of a path option before the argument separator."""
    value = None
    for index, argument in enumerate(arguments):
        if argument == "--": break
        flag, separator, inline = argument.partition("=")
        if flag != name: continue
        after = inline
        if not separator and index + 1 < len(arguments): after = arguments[index + 1]
        value = after if after and not after.startswith("-") else None
    return value


def state_file(tool, arguments, cwd):
    """The package state selected by the build's explicit cache options, if any."""
    if tool == "swift":
        # SwiftPM's environment override takes precedence over either explicit option.
        if os.environ.get("SWIFTPM_BUILD_DIR"): return None
        scratch = option(arguments, "--scratch-path") or option(arguments, "--build-path")
        return Path(cwd) / scratch / STATE if scratch else None
    if tool != "xcodebuild": return None
    if packages := option(arguments, "-clonedSourcePackagesDirPath"):
        return Path(cwd) / packages / STATE
    if derived := option(arguments, "-derivedDataPath"):
        return Path(cwd) / derived / "SourcePackages" / STATE
    return None


def moved_from(root):
    """The matching checkout at the farm's previous root, even if a link now leads here."""
    from queuekeeper.offload_host import EARLIER_ROOT
    from queuekeeper.remote import ROOT

    home = Path.home().resolve()
    current = (home / ROOT / "checkouts").resolve()
    if not root.is_relative_to(current): return None
    parts = root.relative_to(current).parts
    if len(parts) != 3 or parts[0] != parts[2] or not parts[1].isdecimal(): return None
    old = home.joinpath(EARLIER_ROOT, "checkouts", *parts)
    if old == root or old.exists() and old.resolve() != root: return None
    return old


def names_old_checkout(state, old):
    descriptor = os.open(state, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as file:
        if not stat.S_ISREG(os.fstat(file.fileno()).st_mode): return False
        data = file.read(STATE_LIMIT + 1)
    if len(data) > STATE_LIMIT: return False
    remaining = [json.loads(data)]
    while remaining:
        value = remaining.pop()
        if isinstance(value, dict):
            for key, item in value.items():
                # Compare the recorded names before following symlinks: the old link may still resolve.
                if (key in ("location", "path") and isinstance(item, str) and item.startswith("/")
                        and Path(os.path.normpath(item)).is_relative_to(old)):
                    return True
                if isinstance(item, (dict, list)): remaining.append(item)
        elif isinstance(value, list):
            remaining.extend(item for item in value if isinstance(item, (dict, list)))
    return False


def warning(tool, arguments, root, cwd="."):
    """A confirmed stale path in this build's own package state, or None."""
    state = state_file(tool, arguments, cwd)
    if state is None: return None
    try:
        root, state = Path(root).resolve(), state.resolve()
        if not state.is_relative_to(root) or not state.is_file(): return None
        if not (old := moved_from(root)) or not names_old_checkout(state, old): return None
    except (OSError, ValueError, RecursionError):
        return None
    return (f"{NOTICE}: {state} still names the checkout at {old}. "
            "Use a fresh build directory to rule out state kept under the old farm path (vocaro/queuekeeper#201).")
