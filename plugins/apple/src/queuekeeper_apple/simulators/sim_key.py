"""Press keys on a booted iOS simulator's hardware keyboard through the key helper, by its exact UDID.

    scripts/sim-key UDID KEY...

Each KEY is a name from KEYS (pageup, pagedown, home, end, left, right, up, down, return, a, 1, f5,
shift and so on), or a keyboard usage (HID usage page 7) written in hexadecimal, such as 0x4b for
Page Up. Join keys with + to press them together: shift+right holds Shift, presses Right Arrow and
then lets go of Shift. The keys are pressed in order, each down and then up, and reach whatever
has the simulator's keyboard focus, as keys typed on a Mac keyboard attached to Simulator.app do.

It builds plugins/apple/src/queuekeeper_apple/_native/sim-key-helper.m for this Mac into the
toolkit's ignored .build/sim-key, signs it ad hoc, and runs it on the host with the developer
directory `xcode-select -p` names. A built helper is named by a hash of its source, its compiler
flags and the macOS SDK, so it is rebuilt only when one of them changes, and a helper that failed
to build or sign is never kept. The helper drives private, undocumented CoreSimulator and
SimulatorKit API, as Meta's idb does: development tooling only, never linked into an app, a
package or a gate, which any Xcode can break. Sending a key does not prove that the app acted on
it; confirm that from the app.

Exit status: 0 once every key is sent; 2 when UDID is not a booted simulator; 3 when the
simulator's HID service cannot be reached, so that a caller can report its measurement as not
taken; 64 for arguments it does not take; and 1 when the helper could not be built, signed or run
in time.
"""
import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from ..sources import BUILD_ROOT, source_path

BUILD = BUILD_ROOT / "sim-key"
FLAGS = ("-target", "arm64-apple-macos14.0", "-fobjc-arc", "-O2", "-Wall", "-framework", "Foundation")
UDID = re.compile(r"[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}")
USAGE_CODE = re.compile(r"0x[0-9A-Fa-f]{1,2}")
EXIT_UNREACHABLE = 3
EXIT_USAGE = 64
# Seconds each step may take before the wrapper gives up on it.
QUERY_TIMEOUT, BUILD_TIMEOUT, SEND_TIMEOUT = 60, 180, 120


def _keys():
    keys = {chr(ord("a") + index): 0x04 + index for index in range(26)}
    keys.update({str(digit): 0x1E + (digit - 1) % 10 for digit in range(10)})
    keys.update({f"f{number}": 0x3A + number - 1 for number in range(1, 13)})
    keys.update({
        "return": 0x28, "escape": 0x29, "delete": 0x2A, "tab": 0x2B, "space": 0x2C,
        "home": 0x4A, "pageup": 0x4B, "forwarddelete": 0x4C, "end": 0x4D, "pagedown": 0x4E,
        "right": 0x4F, "left": 0x50, "down": 0x51, "up": 0x52,
        "control": 0xE0, "shift": 0xE1, "option": 0xE2, "command": 0xE3,
    })
    return keys


# Key names and their keyboard usages (HID usage page 7). delete is Backspace; control, shift,
# option and command are the left-hand modifiers.
KEYS = _keys()
USAGE = """usage: sim-key UDID KEY...
KEY is a name (pageup, pagedown, home, end, left, right, up, down, return, escape, tab, space,
delete, forwarddelete, a-z, 0-9, f1-f12, control, shift, option, command) or a keyboard usage in
hexadecimal (0x4b), and keys joined with + are pressed together (shift+right). UDID is the
simulator's own, never `booted`."""


def usage_code(key):
    """The keyboard usage for one key's name or hexadecimal usage, or ValueError."""
    if key in KEYS:
        return KEYS[key]
    if USAGE_CODE.fullmatch(key) and int(key, 16) > 0:
        return int(key, 16)
    raise ValueError(f"{key!r} is not a key name or a keyboard usage from 0x01 to 0xff")


def events(key):
    """The helper's events for one KEY: each part down in order, then each up in reverse."""
    codes = [usage_code(part) for part in key.split("+")]
    return [f"down:{code}" for code in codes] + [f"up:{code}" for code in reversed(codes)]


def helper_arguments(arguments):
    """The UDID and the helper's events for the wrapper's arguments, or ValueError."""
    if not arguments or not UDID.fullmatch(arguments[0]):
        raise ValueError("the first argument is the simulator's UDID")
    if len(arguments) < 2:
        raise ValueError("after the UDID come one or more keys")
    return arguments[0], [event for key in arguments[1:] for event in events(key)]


def run(*command, timeout):
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{' '.join(command)} took more than {timeout} seconds") from None
    except OSError as error:
        raise RuntimeError(f"{command[0]} did not run: {error}") from None


def query(*command):
    result = run(*command, timeout=QUERY_TIMEOUT)
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError(f"{' '.join(command)} failed\n{result.stderr.strip()}".rstrip())
    return result.stdout.strip()


def helper(build=BUILD, source=None):
    """The signed helper for the current source, flags and SDK, built first when there is none."""
    source = Path(source or source_path("sim-key-helper.m"))
    sdk_path = query("xcrun", "--sdk", "macosx", "--show-sdk-path")
    sdk_build = query("xcrun", "--sdk", "macosx", "--show-sdk-build-version")
    digest = hashlib.sha256(source.read_bytes())
    for part in (*FLAGS, sdk_path, sdk_build):
        digest.update(b"\0" + part.encode())
    binary = Path(build) / f"sim-key-helper-{digest.hexdigest()[:16]}"
    if binary.is_file() and os.access(binary, os.X_OK):
        return binary
    binary.parent.mkdir(parents=True, exist_ok=True)
    # Built and signed under a name of its own, so a concurrent run never sees it half made.
    handle, scratch = tempfile.mkstemp(prefix=binary.name + ".", suffix=".tmp", dir=binary.parent)
    os.close(handle)
    try:
        built = run("xcrun", "--sdk", "macosx", "clang", *FLAGS, str(source), "-o", scratch,
                    timeout=BUILD_TIMEOUT)
        if built.returncode != 0:
            raise RuntimeError(f"the helper did not build\n{built.stdout}{built.stderr}".rstrip())
        signed = run("codesign", "--force", "--sign", "-", "--identifier", "sim-key-helper", scratch,
                     timeout=QUERY_TIMEOUT)
        if signed.returncode != 0:
            raise RuntimeError(f"the helper did not sign\n{signed.stdout}{signed.stderr}".rstrip())
        os.chmod(scratch, 0o755)
        os.replace(scratch, binary)
    finally:
        if os.path.exists(scratch):
            os.unlink(scratch)
    print(f"sim-key: built {binary} against the macOS SDK {sdk_build}", file=sys.stderr)
    return binary


def main(argv=None):
    arguments = sys.argv[1:] if argv is None else list(argv)
    try:
        udid, helper_events = helper_arguments(arguments)
    except ValueError as error:
        print(f"sim-key: {error}\n{USAGE}", file=sys.stderr)
        return EXIT_USAGE
    try:
        binary = helper()
        developer_dir = query("xcode-select", "--print-path")
        result = run(str(binary), developer_dir, udid, *helper_events, timeout=SEND_TIMEOUT)
    except (OSError, RuntimeError) as error:
        print(f"sim-key: {error}", file=sys.stderr)
        return 1
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    # A helper ended by a signal sent nothing it can vouch for.
    return result.returncode if result.returncode >= 0 else 1
