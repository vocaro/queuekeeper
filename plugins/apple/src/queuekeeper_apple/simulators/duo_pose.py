"""Fold, open or turn an iPhone Duo simulator through the pose helper, by its exact UDID.

    scripts/duo-pose UDID DEGREES              the hinge: 0 closed, 120 partly folded, 180 flat
    scripts/duo-pose UDID orientation NAME     portrait, pud, landscape-left or landscape-right

This is the command a project's Duo lane takes as its hinge script. It builds
plugins/apple/src/queuekeeper_apple/_native/duo-pose-helper.c against the iOS Simulator SDK into the toolkit's ignored
.build/duo-pose, signs it ad hoc, and runs it inside the booted simulator with `xcrun simctl
spawn`. A built helper is named by a hash of its source, its compiler flags and the SDK, so it is
rebuilt only when one of them changes, and a helper that failed to build or sign is never kept.
The helper posts a private, undocumented simulator event: development tooling only, never linked
into an app, a package or a gate, which any Xcode or runtime can break. Sending an event does not
prove a pose; confirm it from the displays.

The exit status is `simctl spawn`'s, which is the helper's once it runs; 64 for arguments it does
not take; and 1 when the helper could not be built, signed or run in time.
"""
import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from ..sources import BUILD_ROOT, ROOT, source_path

BUILD = BUILD_ROOT / "duo-pose"
FLAGS = ("-target", "arm64-apple-ios27.0-simulator", "-O2", "-Wall",
         "-framework", "CoreFoundation", "-framework", "IOKit")
ORIENTATIONS = ("portrait", "pud", "landscape-left", "landscape-right")
UDID = re.compile(r"[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}")
DEGREES = re.compile(r"[0-9]+(?:\.[0-9]+)?")
USAGE = """usage: duo-pose UDID DEGREES
       duo-pose UDID orientation portrait|pud|landscape-left|landscape-right
DEGREES runs from 0, closed, to 180, flat; UDID is the simulator's own, never `booted`."""
# Seconds each step may take before the wrapper gives up on it.
QUERY_TIMEOUT, BUILD_TIMEOUT, SPAWN_TIMEOUT = 60, 180, 60


def helper_arguments(arguments):
    """The UDID and the helper's arguments for the wrapper's, or ValueError."""
    if not arguments or not UDID.fullmatch(arguments[0]):
        raise ValueError("the first argument is the simulator's UDID")
    udid, rest = arguments[0], list(arguments[1:])
    if len(rest) == 1 and DEGREES.fullmatch(rest[0]) and float(rest[0]) <= 180:
        return udid, ["hinge", rest[0]]
    if len(rest) == 2 and rest[0] == "orientation" and rest[1] in ORIENTATIONS:
        return udid, rest
    raise ValueError("after the UDID come degrees from 0 to 180, or `orientation` and a name")


def run(*command, timeout):
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{' '.join(command)} took more than {timeout} seconds") from None
    except OSError as error:
        raise RuntimeError(f"{command[0]} did not run: {error}") from None


def sdk_identity():
    """The iOS Simulator SDK's path and build version, as the developer directory selects them."""
    identity = []
    for query in ("--show-sdk-path", "--show-sdk-build-version"):
        result = run("xcrun", "--sdk", "iphonesimulator", query, timeout=QUERY_TIMEOUT)
        if result.returncode != 0 or not result.stdout.strip():
            raise RuntimeError(f"no iOS Simulator SDK: xcrun {query} failed\n{result.stderr.strip()}")
        identity.append(result.stdout.strip())
    return identity


def helper(build=BUILD, source=None):
    """The signed helper for the current source, flags and SDK, built first when there is none."""
    source = Path(source or source_path("duo-pose-helper.c"))
    sdk_path, sdk_build = sdk_identity()
    digest = hashlib.sha256(source.read_bytes())
    for part in (*FLAGS, sdk_path, sdk_build):
        digest.update(b"\0" + part.encode())
    binary = Path(build) / f"duo-pose-helper-{digest.hexdigest()[:16]}"
    if binary.is_file() and os.access(binary, os.X_OK):
        return binary
    binary.parent.mkdir(parents=True, exist_ok=True)
    # Built and signed under a name of its own, so a concurrent run never sees it half made.
    handle, scratch = tempfile.mkstemp(prefix=binary.name + ".", suffix=".tmp", dir=binary.parent)
    os.close(handle)
    try:
        built = run("xcrun", "--sdk", "iphonesimulator", "clang", *FLAGS, str(source), "-o", scratch,
                    timeout=BUILD_TIMEOUT)
        if built.returncode != 0:
            raise RuntimeError(f"the helper did not build\n{built.stdout}{built.stderr}".rstrip())
        signed = run("codesign", "--force", "--sign", "-", "--identifier", "duo-pose-helper", scratch,
                     timeout=QUERY_TIMEOUT)
        if signed.returncode != 0:
            raise RuntimeError(f"the helper did not sign\n{signed.stdout}{signed.stderr}".rstrip())
        os.chmod(scratch, 0o755)
        os.replace(scratch, binary)
    finally:
        if os.path.exists(scratch):
            os.unlink(scratch)
    print(f"duo-pose: built {binary} against the iOS Simulator SDK {sdk_build}", file=sys.stderr)
    return binary


def main(argv=None):
    arguments = sys.argv[1:] if argv is None else list(argv)
    try:
        udid, rest = helper_arguments(arguments)
    except ValueError as error:
        print(f"duo-pose: {error}\n{USAGE}", file=sys.stderr)
        return 64
    try:
        binary = helper()
        result = run("xcrun", "simctl", "spawn", udid, str(binary), *rest, timeout=SPAWN_TIMEOUT)
    except (OSError, RuntimeError) as error:
        print(f"duo-pose: {error}", file=sys.stderr)
        return 1
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.returncode
