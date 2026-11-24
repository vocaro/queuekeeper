"""Recognize package state that still names a farm checkout before it moved (#201)."""

import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile

NOTICE = "queuekeeper: STALE PACKAGE PATHS"
STATE = "workspace-state.json"
STATE_LIMIT = 4 * 1024 * 1024
REFRESH = "queuekeeper: REFRESH MOVED BUILD CACHE"


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


def build_cache(tool, arguments, cwd):
    """The build directory of an explicit command that can recreate it from sources."""
    before_separator = arguments[:arguments.index("--")] if "--" in arguments else arguments
    flags = {word.partition("=")[0] for word in before_separator}
    if tool == "xcodebuild":
        if flags & {"test-without-building", "clean", "-xctestrun", "-clonedSourcePackagesDirPath",
                    "-showBuildSettings", "-showdestinations", "-list", "-version"}:
            return None
        if not flags & {"build", "build-for-testing", "test", "archive"}:
            return None
    elif tool != "swift" or not before_separator or before_separator[0] not in ("build", "test"):
        return None
    if state := state_file(tool, arguments, cwd):
        return state.parent.parent if tool == "xcodebuild" else state.parent
    return None


def git(root, *arguments):
    return subprocess.run(["git", *arguments], cwd=root, capture_output=True, text=True, timeout=5)


def moved_state(tool, cache, old):
    state = cache / "SourcePackages" / STATE if tool == "xcodebuild" else cache / STATE
    return state.resolve() == state and names_old_checkout(state, old)


def quarantine(tool, cache, root, old):
    try:
        if not moved_state(tool, cache, old):
            return None
        relative = str(cache.relative_to(root))
        if git(root, "check-ignore", "--quiet", "--", relative).returncode:
            return None
        tracked = git(root, "--literal-pathspecs", "ls-files", "--", relative)
        if tracked.returncode or tracked.stdout:
            return None
        preserved = Path(tempfile.mkdtemp(prefix=f".{cache.name}.old-farm-", dir=cache.parent))
        try:
            return cache.rename(preserved / cache.name)
        except OSError:
            preserved.rmdir()
            return None
    except (OSError, ValueError, RecursionError, subprocess.SubprocessError):
        return None


def refresh_moved_build(tool, arguments, root, cwd="."):
    """Preserve a proven old build cache so a dispatched build can recreate it at the same path."""
    cache = build_cache(tool, arguments, cwd) if root and os.environ.get("QUEUEKEEPER_DISPATCHED_FROM") else None
    if cache is None:
        return None
    try:
        root = Path(root).resolve()
        cache = Path(os.path.abspath(cache))
        if not cache.is_relative_to(root / ".build") or cache == root / ".build" \
                or cache.resolve() != cache or not (old := moved_from(root)) or not moved_state(tool, cache, old):
            return None
        repository = git(root, "rev-parse", "--show-toplevel", "--absolute-git-dir")
        if repository.returncode:
            return None
        top, directory = repository.stdout.splitlines()
        if Path(top).resolve() != root:
            return None
        # The check and rename share a stable lock across wrappers. The build itself uses the
        # tool's usual locking at its configured path, which may also be used by nested commands.
        cache_id = hashlib.sha256(os.fsencode(cache)).hexdigest()
        lock = Path(directory) / f"queuekeeper-build-refresh-{cache_id}.lock"
        descriptor = os.open(lock, os.O_CREAT | os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        with os.fdopen(descriptor, "rb") as lock_file:
            if not stat.S_ISREG(os.fstat(lock_file.fileno()).st_mode):
                return None
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            return quarantine(tool, cache, root, old)
    except (OSError, ValueError, RecursionError, subprocess.SubprocessError):
        return None


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
