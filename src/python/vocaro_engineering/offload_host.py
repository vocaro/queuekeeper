"""The second Mac's side of `eng remote`, sent over SSH and run there by that Mac's own Python.

It imports nothing from the toolkit, because it runs before the toolkit is checked out there,
and Python 3.9, a Mac's system Python, runs it. It claims a persistent checkout of the project,
places the dispatched commits, and runs `eng run` or `eng admit` there under the second Mac's
own heavy-work admission, with its device record deferring physical devices to the sending Mac's
and the run given the reservations it carries. Standard output carries framed lines: `O` for
output, `L` for the lane's log as it grows, then, once the work has ended, an `F` naming each file the plan asks back,
such as a lane group's lanes' logs and receipts, followed by `D` lines of its bytes in base64, and
a closing `R` with the result as JSON. Standard input is the
dispatcher's lifeline: its end, which a stop, the dispatcher's death or a dropped connection
sends, stops the run as a stop signal stops a local one. Given `{"toolchain": DEVELOPER_DIR}`
instead of a plan, it prints the Mac's Xcodes, simulator runtimes and device record for
`eng doctor`; given `{"images": ...}` or `{"receive": ...}`, it lists the Mac's runtime images or
adds one read from standard input, for `eng remote setup --runtimes`; given `{"leftovers": ROOT}`,
it lists the processes left over from runs there, which `eng remote` prints before it sends work,
and with `"advertise"` names as refs the commits the named repositories' checkouts hold, so that
the push that follows sends only what they lack, and with `"reap"` first shuts down the stale pool
simulators that census finds (reap);
given `{"device": ...}`, what there may still use a physical device a run was given, for
`devices recover`; given `{"keychain": PATH}`, the Mac's name and whether that keychain file is
there, for `eng keychain recover`; given `{"keepUnlocked": ...}`, at the owner's terminal, it asks for
the login keychain's password and keeps it for runs to unlock that keychain with, for `eng keychain
keep-unlocked`; given `{"foreground": true}`, what the Mac lacks for foreground work started over
SSH and who holds its mac-foreground, for `eng remote foreground`.
"""
import base64
import contextlib
import ctypes
from datetime import datetime, timezone
import fcntl
import getpass
import json
import os
from pathlib import Path
import pwd
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time

# Seconds between reports while every checkout is in use, and before waiting for one gives up.
REPORT_SECONDS, CLAIM_TIMEOUT = 300, 5400
# Seconds the output of a finished command may take to drain: a descendant can hold the pipe.
DRAIN_SECONDS = 5
# Bytes of a runtime image read or written at once.
CHUNK = 8 << 20
# Bytes of a file sent back in one D line, before base64.
FILE_CHUNK = 48 << 10
# The platform a runtime image names when simctl gives no runtime bundle to name it by.
PLATFORMS = {"com.apple.platform.iphonesimulator": "iOS", "com.apple.platform.appletvsimulator": "tvOS",
             "com.apple.platform.watchsimulator": "watchOS", "com.apple.platform.xrsimulator": "visionOS"}
# Where heavy-work admission keeps its guard and its log, as resources.py names them, which this
# file cannot import: the guard beside the legacy gate lock, and the log's two generations, older
# first, in the account's durable state unless HEAVY_SLOT_DIR names a namespace of its own.
GATE_LOCK = "/tmp/studywright-gate-suite.lock"
ADMISSION_STATE = "Library/Application Support/vocaro-engineering"
ADMISSION_LOGS = ("admissions.1.jsonl", "admissions.jsonl")
# The simulators' devices, relative to the home: a booted device's processes name its directory.
DEVICES = "Library/Developer/CoreSimulator/Devices"
DEVICE = re.compile(r"[0-9A-F]{8}-(?:[0-9A-F]{4}-){3}[0-9A-F]{12}", re.IGNORECASE)
# The device record, relative to the home, as devices.py finds it, which VOCARO_DEVICE_RECORD
# overrides for a test's; its key for the Mac it defers physical devices to; and a physical
# device's hardware UDID, as devices.PHYSICAL matches it.
DEVICE_RECORD = "Library/Application Support/vocaro-engineering/device-handoff.json"
DEFERRED = "physicalDevicesRecordedOn"
PHYSICAL = re.compile(r"[0-9A-F]{8}-[0-9A-F]{16}|[0-9A-F]{40}")
# Where a Mac keeps its users' keychain files, relative to the home, and its login keychain's file there.
KEYCHAINS = "Library/Keychains"
LOGIN_KEYCHAIN = "login.keychain-db"
# The file in eng remote's root that holds the login keychain's password, once the owner has run
# `eng keychain keep-unlocked`, from which each run unlocks that keychain in its own session.
KEYCHAIN_PASSWORD = "keychain-password"
# What SecKeychainGetStatus sets for an unlocked keychain (kSecUnlockStateStatus), and the OSStatus of
# a wrong password (errSecAuthFailed) and of a missing keychain file (errSecNoSuchKeychain).
UNLOCK_STATE, AUTH_FAILED, NO_SUCH_KEYCHAIN = 1, -25293, -25294
# How many passwords keep_unlocked asks for before it gives up, as sudo does.
PASSWORD_TRIES = 3
# The process that holds a simulator pool member's reservation, {"pid", "start"}, as
# simulators/pool.py records it when it reserves the member.
HOLDER = "holderProcess"
# A program inside an Xcode, such as the DTServiceHub that xcodebuild starts for a test run,
# which launchd adopts once xcodebuild has ended.
XCODE = re.compile(r"[^\s'\"]*/Xcode[^/\s'\"]*\.app/Contents/")
# Characters of a leftover's command line that the census reports.
COMMAND_LIMIT = 1000
# How ps prints a process's start (lstart), with its spacing collapsed.
STARTED = "%a %b %d %H:%M:%S %Y"
# What the work's Python processes are given: no bytecode, whose __pycache__ directories a
# project's .gitignore need not name, and which would leave its checkout with changes (claim).
NO_BYTECODE = {"PYTHONDONTWRITEBYTECODE": "1"}
# The refs under which a repository here names each commit a checkout of it holds. A push learns what
# the second Mac has from its refs alone, and a detached checkout's HEAD is none of them, so without
# these a push sends the commit's whole history (vocaro-engineering#67).
CACHE = "refs/vocaro/checkouts/"
COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")

# Seconds a pool simulator's launchd_sim must have run before reap may shut it down.
REAP_AFTER = 2 * 60 * 60
# Seconds one reap may take, which bounds what it adds to a dispatch; seconds it must have left to start
# another stop; seconds the check before a stop may hold the device record's lock, under the 30 another
# session's change waits for it (devices.Record); and seconds it waits for that lock.
REAP_SECONDS, REAP_STOP_SECONDS, REAP_CHECK_SECONDS, REAP_LOCK_SECONDS = 60, 20, 20, 5
# The durable log of its stops, beside the admission log, whose audit reads only admissions.
REAP_LOG = "simulator-reaps.jsonl"
# A simulator's own launchd_sim, as ps prints it.
LAUNCHD_SIM = re.compile(r"(?:\S*/)?launchd_sim\s")
# What follows a pool's name in a member's: LABEL N, as simulators/pool.py names one.
MEMBER = re.compile(r".+ [1-9][0-9]*")
SIMULATORS = ("xcrun", "simctl", "list", "devices", "-j")

_output = threading.Lock()
_stop = threading.Event()


class Refusal(RuntimeError):
    """The second Mac will not start the work, and nothing of it ran. A busy second Mac cannot start
    it now; any other refusal is its configuration's, and fix says what fixing that takes."""

    def __init__(self, reason, fix=None, busy=False):
        super().__init__(reason)
        self.fix, self.busy = fix, busy


def emit(kind, data):
    """Write one framed line. A closed connection is a stop: nobody is left to read the result."""
    if not data.endswith(b"\n"): data += b"\n"
    with _output:
        try:
            sys.stdout.buffer.write(kind + b" " + data)
            sys.stdout.buffer.flush()
        except OSError:
            _stop.set()


def say(text):
    emit(b"O", f"second Mac: {text}".encode())


def output(command, environment=None):
    try:
        result = subprocess.run(command, capture_output=True, text=True, env=environment, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def xcode(developer_dir):
    """`xcodebuild -version` of the Xcode at developer_dir, or None."""
    if developer_dir and Path(developer_dir).is_dir():
        return output(["xcrun", "xcodebuild", "-version"], {**os.environ, "DEVELOPER_DIR": developer_dir})
    return None


def mac_name():
    return output(["scutil", "--get", "LocalHostName"]) or os.uname().nodename


def identity(developer_dir):
    """The Mac a result was measured on, its macOS and the Xcode at developer_dir."""
    number = lambda name: int(output(["sysctl", "-n", name]) or 0) or None
    return {"name": mac_name(),
            "model": output(["sysctl", "-n", "hw.model"]), "cpus": number("hw.ncpu"),
            "memoryBytes": number("hw.memsize"), "macOS": output(["sw_vers", "-productVersion"]),
            "build": output(["sw_vers", "-buildVersion"]), "developerDir": developer_dir, "xcode": xcode(developer_dir)}


def record_path():
    return Path(os.environ.get("VOCARO_DEVICE_RECORD") or Path(pwd.getpwuid(os.getuid()).pw_dir) / DEVICE_RECORD)


def device_record():
    """The Mac its device record defers physical devices to, and the entries that hold a physical
    device, which `eng doctor` compares. It reads without the record's lock: every change replaces
    the record whole."""
    path = record_path()
    try:
        record = json.loads(path.read_text())
    except FileNotFoundError:
        return {"path": str(path), "deferredTo": None, "physical": []}
    except (OSError, ValueError) as error:
        return {"path": str(path), "error": f"it cannot be read: {error}"}
    entries = record.get("devices") if isinstance(record, dict) else None
    if not isinstance(entries, dict):
        return {"path": str(path), "error": "it is no device record"}
    held = sorted(key for key, entry in entries.items() if isinstance(entry, dict) and entry.get("state") != "released"
                  and PHYSICAL.fullmatch(str(entry.get("device") or "").upper()))
    return {"path": str(path), "deferredTo": record.get(DEFERRED), "physical": held}


def keychain(path):
    """The Mac's name, its home, the keychain file path names and whether it is there, and the keychain
    files in ~/Library/Keychains, for `eng keychain recover`. path is absolute, starts with ~, or is a
    file name in ~/Library/Keychains; it is never completed, so a misspelled name is reported missing.
    It reads names and file types only: nothing opens a keychain."""
    home = Path.home()
    folder = home / KEYCHAINS
    given = Path(path).expanduser() if path.startswith(("/", "~")) else home / path if "/" in path else folder / path
    try:
        names = sorted(entry.name for entry in folder.iterdir()
                       if entry.name.endswith((".keychain-db", ".keychain")) and entry.is_file())
    except OSError:
        names = []
    return {"mac": mac_name(), "home": str(home), "path": str(given), "exists": given.is_file(),
            "keychains": [str(folder / name) for name in names]}


def security_unlock(path, password):
    """Unlock the keychain file at path with password, bytes, in this process's security session,
    which the processes it starts share: SecKeychainUnlock's OSStatus, and whether SecKeychainGetStatus
    then reads the keychain unlocked. The password goes to Security alone, never into a command's
    arguments or environment, and with a password given Security shows no dialog."""
    security = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
    release = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation").CFRelease
    security.SecKeychainOpen.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p)]
    security.SecKeychainUnlock.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p, ctypes.c_ubyte]
    security.SecKeychainGetStatus.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    for function in (security.SecKeychainOpen, security.SecKeychainUnlock, security.SecKeychainGetStatus):
        function.restype = ctypes.c_int32
    release.argtypes = [ctypes.c_void_p]
    reference = ctypes.c_void_p()
    status = security.SecKeychainOpen(str(path).encode(), ctypes.byref(reference))
    if status: return status, False
    try:
        status = security.SecKeychainUnlock(reference, len(password), password, 1)
        if status: return status, False
        state = ctypes.c_uint32()
        status = security.SecKeychainGetStatus(reference, ctypes.byref(state))
        return status, not status and bool(state.value & UNLOCK_STATE)
    finally:
        release(reference)


def unlock_for_run(root, unlock=security_unlock):
    """Unlock the login keychain in this run's own SSH session, when the owner has kept its password
    in root (keep_unlocked): an unlock in another session, the owner's desktop or an earlier SSH login,
    does not reach this one (guides/shared-host.md, Keychains on the right Mac). The path it unlocked,
    or None when no password is kept. A kept password that does not unlock it is the owner's to fix,
    and the run does not start: work that signs would fail later, and less plainly."""
    kept = root / KEYCHAIN_PASSWORD
    if not kept.is_file(): return None
    path = Path.home() / KEYCHAINS / LOGIN_KEYCHAIN
    fix = (f"on the Mac that sends work here, run `python3 scripts/eng keychain keep-unlocked` and type this Mac's "
           f"login password at its prompt, or delete {kept} to send work without the unlock "
           "(guides/shared-host.md, Keychains on the right Mac)")
    if not path.is_file(): raise Refusal(f"{kept} holds the password of {path}, which is not there", fix)
    try:
        password = kept.read_bytes()
    except OSError as error:
        raise Refusal(f"{kept} cannot be read: {error}", fix) from None
    # A file the owner wrote by hand may end in a newline, which no typed password does.
    if password.endswith(b"\n"): password = password[:-1]
    try:
        status, unlocked = unlock(path, password)
    except (OSError, AttributeError) as error:
        raise Refusal(f"Security could not be called to unlock {path}: {error}", fix) from None
    if status == AUTH_FAILED:
        raise Refusal(f"the password in {kept} does not unlock {path} (errSecAuthFailed): this Mac's login password "
                      "may have changed since it was kept", fix)
    if status or not unlocked:
        raise Refusal(f"{path} is not unlocked with the password in {kept}: OSStatus {status}", fix)
    return str(path)


def keep(path, password):
    """Write password to path whole, readable and writable only by this account, replacing any file there."""
    temporary = path.with_name(f".{path.name}.{os.getpid()}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(password)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def keep_unlocked(request, ask=None, unlock=security_unlock, run=subprocess.run, out=print, terminal=None):
    """`eng keychain keep-unlocked` on the second Mac, in the owner's Terminal there or through the
    `ssh -t` the sending Mac runs: ask for this Mac's login password, and once it has unlocked the login
    keychain, keep it in request["root"], under the home, readable only by this account, for each eng
    remote run to unlock that keychain in its own session (unlock_for_run). Then set the keychain to
    lock neither after a time nor on sleep, so that the owner's own session keeps it unlocked too. The
    password goes only to Security and that file. Exit 0 when both are done, 1 when no password given
    unlocked the keychain, which changes nothing, or the settings were not changed, and 2 when it
    refused, having asked for nothing."""
    ask = ask or getpass.getpass
    terminal = sys.stdin.isatty() if terminal is None else terminal
    here, expected = mac_name(), request.get("mac")
    root, path = Path.home() / request["root"], Path.home() / KEYCHAINS / LOGIN_KEYCHAIN
    refusal = None
    if expected and here != expected:
        refusal = f"this is {here}, not {expected}, the Mac whose login keychain the request was for"
    elif not path.is_file():
        refusal = f"{path} is not on {here}"
    elif not root.is_dir():
        refusal = (f"{root} is not on {here}: set up the second Mac first, and run eng remote there once "
                   "(guides/shared-host.md, Setting up the second Mac)")
    elif not terminal:
        refusal = "it asks for the password at a terminal, and this has none: run it in your own Terminal"
    if refusal:
        out(f"Refused: {refusal}. Nothing was asked for or changed.")
        return 2
    for _ in range(PASSWORD_TRIES):
        password = ask(f"Login password of {here}, for its login keychain (not shown): ").encode()
        status, unlocked = unlock(path, password)
        if not status and unlocked: break
        out("That password did not unlock it." if status == AUTH_FAILED else
            f"{path} is not unlocked with that password: OSStatus {status}.")
    else:
        out(f"No password unlocked {path}; nothing was kept or changed.")
        return 1
    kept = root / KEYCHAIN_PASSWORD
    try:
        keep(kept, password)
    except OSError as error:
        out(f"It unlocked the keychain, but {kept} could not be written: {error}. Nothing was kept or changed.")
        return 1
    out(f"Kept it in {kept}, readable only by {pwd.getpwuid(os.getuid()).pw_name}: each eng remote run here now "
        "unlocks the login keychain in its own session.")
    # The keychain is unlocked in this session, so neither command needs a dialog; without -l, -u and -t,
    # set-keychain-settings sets no timeout and no lock on sleep.
    for command in (["security", "set-keychain-settings", str(path)], ["security", "show-keychain-info", str(path)]):
        try:
            done = run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as error:
            done = subprocess.CompletedProcess(command, 1, "", str(error))
        said = " ".join(((done.stderr or "") + " " + (done.stdout or "")).split())
        if done.returncode:
            out(f"`{shlex.join(command)}` failed, exit {done.returncode}: {said}. The password is kept; the keychain's "
                "lock settings may still lock it in the owner's own session.")
            return 1
    out(f"Its settings now: {said}")
    return 0


def console_user():
    """The account logged in on the Mac's own screen, or None; root while the login window shows."""
    try: return pwd.getpwuid(os.stat("/dev/console").st_uid).pw_name
    except (OSError, KeyError): return None


def accessibility_trusted():
    """Whether this process, and with it the command that started it, is trusted for accessibility."""
    try:
        services = ctypes.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        services.AXIsProcessTrusted.restype = ctypes.c_bool
        return bool(services.AXIsProcessTrusted())
    except (OSError, AttributeError):
        return False


def screen_capture_allowed():
    """Whether this process, and with it the command that started it, may capture the screen, asked without a
    prompt (CGPreflightScreenCaptureAccess)."""
    try:
        graphics = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        graphics.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
        return bool(graphics.CGPreflightScreenCaptureAccess())
    except (OSError, AttributeError):
        return False


def foreground_problems():
    """What this Mac lacks for foreground work, a Mac VoiceOver session among it, run by a command started
    over SSH, as [problem, the owner's fix] pairs; none when it is ready. Each is a setting of the owner's
    Mac, which the owner changes, never a run (guides/second-mac-foreground.md).
    It reads and changes nothing else: nothing here prompts, activates or sends a key."""
    found = []
    user, console = pwd.getpwuid(os.getuid()).pw_name, console_user()
    if console != user:
        found.append([f"{console or 'nobody'} is logged in on the screen, not {user}",
                      f"log in as {user} on the Mac's own screen; fast user switching to another account hides it"])
    front = output(["lsappinfo", "front"])
    name = output(["lsappinfo", "info", "-only", "name", front]) if front else None
    if name is not None and '"loginwindow"' in name:
        found.append(["the screen is locked, or the login window is in front",
                      "unlock it, and keep it from locking again: in System Settings, Lock Screen, set the screen "
                      "saver and turning the display off to Never and requiring a password after either to Never"])
    if not accessibility_trusted():
        found.append(["a command started over SSH is not trusted for accessibility (AXIsProcessTrusted is false), so "
                      "it can neither send VoiceOver keys through System Events nor read speech's observer",
                      "in System Settings, Privacy & Security, Device Control and Data Access (macOS 27.2's name for "
                      "Accessibility), add /usr/libexec/sshd-keygen-wrapper, which runs Remote Login's commands, and "
                      "turn it on"])
    if not screen_capture_allowed():
        found.append(["a command started over SSH may not capture the screen (CGPreflightScreenCaptureAccess is "
                      "false), so a run's screenshots can fail or raise a permission prompt that takes the focus",
                      "in System Settings, Privacy & Security, Screen & System Audio Recording, add "
                      "/usr/libexec/sshd-keygen-wrapper and turn it on"])
    mode = output(["automationmodetool"]) or ""
    if "DOES NOT REQUIRE" not in mode:
        found.append(["XCTest's automation mode asks for authentication, which nobody at the screen gives",
                      "run `sudo automationmodetool enable-automationmode-without-authentication` there"])
    if output(["defaults", "read", "com.apple.VoiceOverTraining", "doNotShowSplashScreen"]) != "1":
        found.append(["VoiceOver opens its welcome dialog when it starts, which takes the VoiceOver cursor",
                      "run `defaults write com.apple.VoiceOverTraining doNotShowSplashScreen -bool true` there"])
    if output(["defaults", "read", "com.apple.HIToolbox", "AppleDictationAutoEnable"]) != "0":
        found.append(["after a few VoiceOver commands, each of which presses Control, macOS offers to enable "
                      "Dictation, and the prompt takes the VoiceOver cursor",
                      "run `defaults write com.apple.HIToolbox AppleDictationAutoEnable -int 0` there"])
    return found


def foreground():
    """The Mac's name, foreground readiness and mac-foreground entry, for `eng remote foreground`.
    VoiceOver is read under the lease only."""
    path = record_path()
    try: entry = json.loads(path.read_text()).get("devices", {}).get("mac-foreground")
    except FileNotFoundError: entry = None
    except (OSError, ValueError, AttributeError) as error: entry = {"error": f"the device record cannot be read: {error}"}
    # VoiceOver may belong to the current holder. Even its baseline is read only after a
    # run acquires the lease; on is a valid baseline, preserved by the foreground state guard.
    return {"mac": mac_name(), "problems": foreground_problems(), "voiceoverManagedUnderLease": True, "entry": entry}


def toolchain(developer_dir):
    """The Mac's identity with the Xcode at developer_dir, its default Xcode (`xcode-select -p`), the
    simulator runtimes the Xcode at developer_dir lists and its device record, which `eng doctor`
    compares."""
    found = identity(developer_dir)
    default = output(["xcode-select", "-p"])
    runtimes = None
    if found["xcode"]:
        listed = output(["xcrun", "simctl", "list", "runtimes", "-j"], {**os.environ, "DEVELOPER_DIR": developer_dir})
        try:
            runtimes = sorted(f"{runtime['name']} ({runtime['buildversion']})"
                              for runtime in json.loads(listed)["runtimes"] if runtime.get("isAvailable"))
        except (TypeError, ValueError, KeyError):
            pass
    return {**found, "defaultDeveloperDir": default, "defaultXcode": xcode(default), "runtimes": runtimes,
            "deviceRecord": device_record()}


def image_file(record):
    """The file of the disk image that a record of `simctl runtime list` names, and None; or None and
    why no such file can be read. simctl names a disk image by its own path, and a cryptex by its
    asset's AssetData folder, whose Restore folder holds the image beside the cryptex's signatures.
    Either file's size is the record's sizeBytes (Xcode 27.2 on macOS 27.0.1 and 27.2, October 2026)."""
    path, size = record.get("path"), record.get("sizeBytes")
    if not path or not isinstance(size, int):
        return None, "simctl names no path and size of its image"
    path = Path(path)
    try:
        candidates = [path] if path.is_file() else sorted((path / "Restore").glob("*.dmg"))
        matching = [candidate for candidate in candidates if candidate.is_file() and candidate.stat().st_size == size]
    except OSError as error:
        return None, f"{path} cannot be read: {error}"
    if len(matching) != 1:
        return None, f"{path} holds no single disk image of its {size} bytes"
    if not os.access(matching[0], os.R_OK):
        return None, f"{matching[0]} cannot be read"
    return str(matching[0]), None


def images(developer_dir):
    """The runtime disk images `simctl runtime list` reports, each with the runtime it holds named as
    toolchain names it, NAME (BUILD), and its image file, or None when simctl lists none."""
    listed = output(["xcrun", "simctl", "runtime", "list", "-j"], {**os.environ, "DEVELOPER_DIR": developer_dir})
    try:
        records = list(json.loads(listed).values())
    except (TypeError, ValueError, AttributeError):
        return None
    found = []
    for record in records:
        if not isinstance(record, dict) or not record.get("build"): continue
        bundle = Path(record.get("runtimeBundlePath") or "")
        name = bundle.stem if bundle.suffix == ".simruntime" else \
            f"{PLATFORMS.get(record.get('platformIdentifier'), record.get('platformIdentifier'))} {record.get('version')}"
        file, unreadable = image_file(record)
        found.append({"runtime": f"{name} ({record['build']})", "name": name, "build": record["build"],
                      "identifier": record.get("identifier"), "state": record.get("state"),
                      "sizeBytes": record.get("sizeBytes"), "architectures": record.get("supportedArchitectures") or [],
                      "file": file, "unreadable": unreadable})
    return sorted(found, key=lambda image: image["runtime"])


def stage(directory, clear):
    """The free bytes where runtime images are staged, and the staged images an earlier copy left
    there, which clear removes. Only clear creates the directory."""
    directory = Path(directory)
    if clear: directory.mkdir(parents=True, exist_ok=True)
    left = sorted(path.name for path in directory.glob("*.dmg*")) if directory.is_dir() else []
    if clear:
        for name in left: (directory / name).unlink(missing_ok=True)
    existing = next(path for path in (directory, *directory.parents) if path.exists())
    return {"free": shutil.disk_usage(existing).free, "left": left}


def receive(stream, directory, build, size, developer_dir, progress=None):
    """Stage a runtime image read from stream in directory, then have simctl add it, moving it into
    simctl's own storage. Only an image of exactly size bytes is added, and whatever fails removes
    the staged file, so a failed copy adds nothing and leaves nothing behind. Return None once the
    image is added, else why it was not. progress, given the bytes received so far, may raise to stop."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    partial, staged = directory / f"{build}.dmg.partial", directory / f"{build}.dmg"
    try:
        received = 0
        with partial.open("wb") as file:
            while not _stop.is_set():
                chunk = stream.read(CHUNK)
                if not chunk: break
                file.write(chunk)
                received += len(chunk)
                if progress is not None: progress(received)
        if _stop.is_set(): return "stopped before it was added"
        if received != size: return f"received {received} of its {size} bytes"
        partial.rename(staged)
        # In a session of its own, so a terminal's interrupt cannot end it halfway through.
        added = subprocess.run(["xcrun", "simctl", "runtime", "add", str(staged), "--move"], stdin=subprocess.DEVNULL,
                               capture_output=True, text=True, env={**os.environ, "DEVELOPER_DIR": developer_dir},
                               start_new_session=True)
        if added.returncode:
            lines = [line.strip() for line in (added.stderr + added.stdout).splitlines() if line.strip()]
            return f"simctl runtime add exited {added.returncode}: {lines[-1] if lines else 'no output'}"
        return None
    except OSError as error:
        return f"staging it in {directory} failed: {error}"
    finally:
        # simctl removes the staged file once it holds the image; a failure leaves it to remove.
        for path in (partial, staged): path.unlink(missing_ok=True)


class Usage(ctypes.Structure):
    """<sys/resource.h>'s struct rusage_info_v4, which proc_pid_rusage fills for RUSAGE_INFO_V4."""
    _fields_ = [("uuid", ctypes.c_uint8 * 16), ("counters", ctypes.c_uint64 * 16), ("bytesRead", ctypes.c_uint64),
                ("bytesWritten", ctypes.c_uint64), ("times", ctypes.c_uint64 * 9), ("logicalWrites", ctypes.c_uint64),
                ("rest", ctypes.c_uint64 * 7)]


_libproc = []


def disk_writes(pid):
    """The bytes a process has written to files since it started, or None where they cannot be read.
    proc_pid_rusage reads the account's own processes without sudo. Its logical writes count a write
    when it is made, flushed or not, and to a file deleted since, but no write to a pipe or /dev/null;
    its disk I/O counted a buffered 4 MiB not at all, only what the process flushed itself (macOS
    27.0, October 2026)."""
    if not _libproc:
        try: _libproc.append(ctypes.CDLL("/usr/lib/libproc.dylib"))
        except OSError: _libproc.append(None)
    usage = Usage()
    if _libproc[0] is None or _libproc[0].proc_pid_rusage(pid, 4, ctypes.byref(usage)) != 0:
        return None
    return usage.logicalWrites


def listing(command, timeout=60):
    """A command's standard output whatever its status, or None when it cannot run: lsof exits 1
    when it finds nothing, or when a process it looked at has ended."""
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return None


def process_table(text):
    """The processes a `ps -axww -o pid=,ppid=,uid=,lstart=,args=` listing names, by PID: each
    one's parent, user ID, start with its spacing collapsed, and command line."""
    table = {}
    for line in (text or "").splitlines():
        parts = line.split(None, 8)
        if len(parts) < 8 or not all(part.isdigit() for part in parts[:3]): continue
        table[int(parts[0])] = {"parent": int(parts[1]), "uid": int(parts[2]), "start": " ".join(parts[3:8]),
                                "args": parts[8] if len(parts) > 8 else ""}
    return table


def working_directories(text):
    """Each process's working directory, from an `lsof -Fpn -d cwd` listing."""
    found, pid = {}, None
    for line in (text or "").splitlines():
        if line[:1] == "p" and line[1:].isdigit(): pid = int(line[1:])
        elif line[:1] == "n" and pid is not None: found[pid] = line[1:]
    return found


def started(start):
    """A start as ps prints it, in seconds since the epoch, or None."""
    try: return time.mktime(time.strptime(start, STARTED))
    except (TypeError, ValueError, OverflowError): return None


def identities(records):
    """(PID, start with its spacing collapsed) of each record that names an integer PID, the start
    None where a record has none."""
    return [(record["pid"], " ".join(record["start"].split()) if isinstance(record.get("start"), str) else None)
            for record in records if isinstance(record, dict) and isinstance(record.get("pid"), int)]


def guard_owners():
    """The owners heavy-work admission's guard records, as resources.guard_owners reads them."""
    try:
        state = json.loads(Path(os.environ.get("SW_GATE_LOCK_FILE", GATE_LOCK) + ".warm-push-guard").read_text())
    except (OSError, ValueError):
        return []
    owners = state.get("owners") if isinstance(state, dict) and state.get("vocaroEngineering") == 1 else None
    if not isinstance(owners, dict): return []
    return identities([owner for owner in owners.values()
                       if isinstance(owner, dict) and isinstance(owner.get("start"), str)])


def admission_directory():
    """Where the admission log is: a HEAVY_SLOT_DIR namespace's own, else in the account's durable state."""
    return Path(os.environ["HEAVY_SLOT_DIR"]) if os.environ.get("HEAVY_SLOT_DIR") else \
        Path(pwd.getpwuid(os.getuid()).pw_dir) / ADMISSION_STATE


def unended():
    """(PID, when it was admitted, in seconds since the epoch) of each admission the admission log
    records without an end: one still held, or one SIGKILL ended."""
    directory = admission_directory()
    admitted, ended = {}, set()
    for name in ADMISSION_LOGS:
        try: lines = (directory / name).read_bytes().splitlines()
        except OSError: continue
        for line in lines:
            try: entry = json.loads(line)
            except ValueError: continue
            if not isinstance(entry, dict): continue
            if entry.get("event") == "admitted" and isinstance(entry.get("pid"), int):
                admitted[entry.get("identity")] = entry
            elif entry.get("event") == "ended": ended.add(entry.get("identity"))
    found = []
    for identity, entry in admitted.items():
        if identity in ended: continue
        try: moment = datetime.fromisoformat(entry["time"]).timestamp()
        except (TypeError, ValueError, KeyError): moment = None
        found.append((entry["pid"], moment))
    return found


def dispatchers(checkouts):
    """The runs that each project's checkout slot locks record, as claim writes them: (PID, start)."""
    records = []
    for path in sorted(Path(checkouts).glob("*/*.lock")):
        try: records.append(json.loads(path.read_text()))
        except (OSError, ValueError): continue
    return identities(records)


def reservations():
    """(UDID, PID, start with its spacing collapsed) of each process that the device record names as
    holding a simulator it has not released: the process that reserved a pool member, as
    simulators/pool.py records it, and an active run's process. With them, why the record could not
    be read, or None. It reads without the record's lock: every change replaces the record whole."""
    path = record_path()
    try:
        record = json.loads(path.read_text())
    except FileNotFoundError:
        return [], None
    except (OSError, ValueError) as error:
        return [], (f"the device record {path}, which could not be read ({error}), so no reservation accounts "
                    "for a simulator")
    entries = record.get("devices") if isinstance(record, dict) else None
    if not isinstance(entries, dict):
        return [], f"the device record {path}, which is no device record, so no reservation accounts for a simulator"
    found = []
    for entry in entries.values():
        if not isinstance(entry, dict) or entry.get("state") == "released": continue
        udid = str(entry.get("device") or "").upper()
        if not DEVICE.fullmatch(udid): continue
        holders = [entry.get(HOLDER)]
        if entry.get("activeRun"):
            holders.append({"pid": entry.get("hostProcessIdentifier"), "start": entry.get("hostProcessStart")})
        found += [(udid, pid, start) for pid, start in identities(holders) if start is not None]
    return found, None


def live_runs(table, recorded, admitted):
    """The PIDs of live runs: each recorded (PID, start) whose process still has that start (a record
    without one counts while its PID runs), and each admission the log has not ended whose process
    started no later than it was admitted, since a process that took its PID over started later."""
    roots = {pid for pid, start in recorded if pid in table and start in (None, table[pid]["start"])}
    for pid, moment in admitted:
        began = started(table[pid]["start"]) if pid in table else None
        if pid in table and (moment is None or began is None or began <= moment + 1): roots.add(pid)
    return roots


def forms(path):
    """The forms a directory takes in a command line or in lsof's report, each with a closing slash:
    as given, resolved, and with and without the /private that /tmp and /var resolve to."""
    found = set()
    for form in {str(path), os.path.realpath(path)}:
        form = form.rstrip("/")
        if not form: continue
        found.add(form + "/")
        if form.startswith("/private/"): found.add(form[len("/private"):] + "/")
        if form.startswith(("/tmp/", "/var/")): found.add("/private" + form + "/")
    return sorted(found)


def lineage(table, pid):
    """pid and its ancestors in table, nearest first."""
    line = []
    while pid in table and pid not in line:
        line.append(pid)
        pid = table[pid]["parent"]
    return line


def leftovers(root, listed=listing, written=disk_writes):
    """The account's processes that run from a checkout under root, the temporary directory or a
    simulator's device, and that no live run accounts for: what `eng remote` prints before it sends
    work, and `eng doctor` reports. It reads and reports, and stops nothing.

    A process runs from a place when its working directory lies in it or its command line names a
    path in it; a program under /System is macOS's own. It is a live run's when it or an ancestor
    is a live admission owner, as the guard or the admission log records it, or a live run of this
    file, as its checkout's slot lock records it: a light lane and a provision lane take no
    admission. Some of a run's processes launchd starts or adopts, and those a live run holds are
    its own, with everything below them. A simulator's processes descend from launchd_sim, never
    from a run, so a device is a live run's while the device record holds it for one of that run's
    processes, as the pool records the process that reserved a member, or while one of them names
    it. An Xcode program, such as the DTServiceHub that xcodebuild leaves behind, is a live run's
    when it started in a checkout after a live admission there began. Each leftover whose parent is
    no leftover is listed, with its PID, start, command, how much it has written, and each of its
    descendants with what that one has written."""
    home, uid = Path.home(), os.getuid()
    table = process_table(listed(["ps", "-axww", "-o", "pid=,ppid=,uid=,lstart=,args="]))
    if not table: return {"error": "ps listed no processes"}
    unchecked = []
    found = listed(["lsof", "-nPw", "-a", "-u", str(uid), "-d", "cwd", "-Fpn"])
    if not found:
        unchecked.append("the processes' working directories, which lsof did not list, so only their command lines "
                         "were read")
    directories = working_directories(found)
    temporary = os.environ.get("TMPDIR") or (listed(["getconf", "DARWIN_USER_TEMP_DIR"]) or "").strip()
    if not temporary: unchecked.append("the temporary directory, which neither TMPDIR nor getconf named")
    checkouts = home / root / "checkouts"
    places = [(kind, forms(path)) for kind, path in
              [("checkout", checkouts), ("temporary directory", temporary), ("simulator", home / DEVICES)] if path]

    ancestry = lambda pid: lineage(table, pid)
    slots = forms(checkouts)

    def slot(path):
        """The checkout slot a path lies in, as (project, slot), or None."""
        for prefix in slots:
            if path and (path + "/").startswith(prefix):
                parts = path[len(prefix):].split("/")
                if len(parts) > 1 and parts[0] and parts[1]: return parts[0], parts[1]
        return None

    def place(pid):
        """Where the process runs from: (kind, the path that places it there, device or None), or None."""
        cwd = directories.get(pid)
        for kind, prefixes in places:
            for prefix in prefixes:
                if cwd and (cwd + "/").startswith(prefix):
                    path = cwd
                else:
                    mention = re.search(re.escape(prefix) + r"[^\s'\"]*", table[pid]["args"])
                    if mention is None: continue
                    path = mention.group(0)
                device = DEVICE.match(path[len(prefix):]) if kind == "simulator" else None
                return kind, path, device and device.group(0).upper()
        return None

    owners, logged = guard_owners(), unended()
    roots = live_runs(table, owners + dispatchers(checkouts), logged)
    own = set(ancestry(os.getpid()))
    runs = {pid for pid in table if roots.intersection(ancestry(pid))}
    # The devices the device record holds for one of a live run's processes, by UDID, and those a
    # live run names: a run can name a device by its kind alone, as eng simulator-tests does.
    reserved, problem = reservations()
    if problem: unchecked.append(problem)
    held = {udid for udid, pid, start in reserved if pid in runs and table[pid]["start"] == start}
    named = " ".join(table[pid]["args"] for pid in runs).upper()
    # When each live admission began, by the checkout slot its owner works in: as the log records
    # it, or else from the start of the owner the guard records.
    begun = {pid: started(start) for pid, start in owners if pid in table and table[pid]["start"] == start}
    begun.update((pid, moment) for pid, moment in logged
                 if moment is not None and live_runs(table, [], [(pid, moment)]))
    windows = {}
    for pid, moment in begun.items():
        where = slot(directories.get(pid))
        if where is not None and moment is not None: windows[where] = min(moment, windows.get(where, moment))

    def holds(pid, kind, path, device):
        """Whether a live run holds the process: its simulator, or, for an Xcode program, the
        checkout it started in after that run's admission there began."""
        if device: return device in held or device in named
        if kind != "checkout" or not XCODE.match(table[pid]["args"]): return False
        since, began = windows.get(slot(path)), started(table[pid]["start"])
        return since is not None and began is not None and began >= since - 1

    placed = {}
    for pid, process in table.items():
        # macOS's own agents, which launchd starts, can work in the temporary directory, as
        # IMTransferAgent did on both Macs (macOS 27, October 2026).
        if process["uid"] != uid or pid in own or pid in runs or process["args"].startswith("/System/"): continue
        where = place(pid)
        if where is not None: placed[pid] = where
    # What a live run holds is its own with everything below it, as a simulator's processes are.
    holding = {pid for pid, where in placed.items() if holds(pid, *where)}
    if holding: runs |= {pid for pid in table if holding.intersection(ancestry(pid))}
    candidates = {pid: where for pid, where in placed.items() if pid not in runs}
    children = {}
    for pid, process in table.items(): children.setdefault(process["parent"], []).append(pid)
    # This file stays ASCII: it reaches the second Mac as a command-line argument.
    command = lambda pid: table[pid]["args"][:COMMAND_LIMIT] + ("..." if len(table[pid]["args"]) > COMMAND_LIMIT else "")
    now, rows = time.time(), []
    for pid, (kind, path, device) in candidates.items():
        # A leftover below another is listed among that one's descendants.
        if any(above in candidates for above in ancestry(pid)[1:]): continue
        descendants, pending, seen = [], list(children.get(pid, [])), {pid}
        while pending:
            child = pending.pop()
            if child in seen or child in own or child in runs: continue
            seen.add(child)
            descendants.append({"pid": child, "command": command(child), "written": written(child)})
            pending.extend(children.get(child, []))
        began = started(table[pid]["start"])
        rows.append({"pid": pid, "parent": table[pid]["parent"], "start": table[pid]["start"],
                     "seconds": round(now - began) if began is not None else None, "command": command(pid),
                     "place": kind, "path": path, **({"simulator": device} if device else {}),
                     "cwd": directories.get(pid), "written": written(pid),
                     "descendants": sorted(descendants, key=lambda descendant: -(descendant["written"] or 0))})
    total = lambda row: (row["written"] or 0) + sum(descendant["written"] or 0 for descendant in row["descendants"])
    return {"leftovers": sorted(rows, key=lambda row: (-total(row), row["pid"])), "unchecked": unchecked,
            "processes": sum(1 for process in table.values() if process["uid"] == uid)}


def device_users(request, listed=listing, written=disk_writes):
    """What here may still use the physical device a run was given, for `devices recover`: each of
    the account's processes whose command line names its UDID, live or left over, among them the
    run's own offload_host.py, whose plan names it while the run lasts; and each leftover that started
    since the run did. Another recovery's census of the device, whose request names it too, only reads,
    so it is not one: two recoveries checking at once would otherwise each hold the other's (#80).
    It reads and reports, and stops nothing."""
    udid = request["udid"].upper()
    table = process_table(listed(["ps", "-axww", "-o", "pid=,ppid=,uid=,lstart=,args="]))
    if not table: return {"error": "ps listed no processes"}
    own, uid = set(lineage(table, os.getpid())), os.getuid()
    # The start of the request remote.device_users sends for this device.
    census = json.dumps({"device": {"udid": udid}})[:-2]
    naming = [{"pid": pid, "start": process["start"], "command": process["args"][:COMMAND_LIMIT]}
              for pid, process in table.items()
              if process["uid"] == uid and pid not in own and udid in process["args"].upper()
              and census not in process["args"]]
    census = leftovers(request["root"], listed, written)
    if "error" in census: return census
    since = request.get("since")
    later = [row for row in census["leftovers"] if since is None or (started(row["start"]) or since) >= since - 1]
    return {**census, "leftovers": later, "naming": naming}


class ReapProblem(RuntimeError):
    """What reap could not read or do, which leaves the simulator running."""


def start_shutdown(udid):
    """`xcrun simctl shutdown UDID` started: the only change reap makes to a simulator."""
    return subprocess.Popen(["xcrun", "simctl", "shutdown", udid], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)


def known_pools(root):
    """The simulator pools that the projects' checkouts under root name in engineering.json."""
    found = set()
    for path in (Path.home() / root / "checkouts").glob("*/*/*/engineering.json"):
        try: name = json.loads(path.read_text()).get("simulatorPool")
        except (OSError, ValueError, AttributeError): continue
        if isinstance(name, str) and name: found.add(name)
    return found


def pool_of(name, pools):
    """The pool whose member a simulator's name makes it, or None."""
    return next((pool for pool in sorted(pools) if name.startswith(pool + " ") and MEMBER.fullmatch(name[len(pool) + 1:])), None)


def device_entries():
    """The device record's entries by key. A record that cannot be read as a whole raises, because
    any entry in it may hold the simulator; none yet holds nothing."""
    path = record_path()
    try:
        record = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:
        raise ReapProblem(f"the device record {path} could not be read ({error})") from None
    entries = record.get("devices") if isinstance(record, dict) and record.get("vocaroDeviceHandoff") == 1 else None
    if not isinstance(entries, dict) or not all(isinstance(entry, dict) for entry in entries.values()):
        raise ReapProblem(f"{path} is no version 1 device record")
    return entries


def require_admissions():
    """Raise unless the guard and the admission log can be read, which the census otherwise reads as
    no admission at all. Neither exists until something is admitted."""
    guard = Path(os.environ.get("SW_GATE_LOCK_FILE", GATE_LOCK) + ".warm-push-guard")
    try:
        state = json.loads(guard.read_text())
        if not isinstance(state, dict) or state.get("vocaroEngineering") != 1 or not isinstance(state.get("owners"), dict):
            raise ValueError("it is no guard")
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as error:
        raise ReapProblem(f"the admission guard {guard} could not be read ({error})") from None
    for name in ADMISSION_LOGS:
        try: (admission_directory() / name).read_bytes()
        except FileNotFoundError: continue
        except OSError as error:
            raise ReapProblem(f"the admission log could not be read ({error})") from None


def reservation_hold(key, entry, table):
    """Why a record entry keeps its simulator running, or None, and what a reservation that does not
    says of itself. A recorded process is live while its PID runs with its recorded start. An entry
    that records none, or none that can be read, or that is marked uncertain, may be live: only a
    process known to be gone, or a PID with another start, shows that it is not."""
    if entry.get("uncertain") or entry.get("state") == "restoration-unverified":
        return f"its reservation {key} is uncertain, kept for recovery", None
    if entry.get("state") == "released" and not entry.get("activeRun"): return None, None
    due = entry.get("expectedReleaseBy")
    if due is not None:
        # A session's `simulator-pool take` ends when it prints the UDID, so until its release is due
        # the session's word holds the member, whatever became of that process.
        try: ends = datetime.fromisoformat(str(due).replace("Z", "+00:00")).timestamp()
        except (ValueError, OverflowError, OSError):
            return f"its reservation {key} records an expected release that cannot be read", None
        if ends > time.time(): return f"its reservation {key} is not due for release until {due}", None
    recorded = ([entry[HOLDER]] if HOLDER in entry else [])
    if entry.get("activeRun"):
        recorded.append({"pid": entry.get("hostProcessIdentifier"), "start": entry.get("hostProcessStart")})
    owned = entry.get("ownedProcesses") or []
    recorded += owned if isinstance(owned, list) else [owned]
    if not recorded: return f"its reservation {key} records no process, so it may be live", None
    for item in recorded:
        pid = item.get("pid") if isinstance(item, dict) else None
        if type(pid) is not int or pid <= 0:
            return f"its reservation {key} records a process that cannot be read", None
        if pid not in table: continue
        start = item.get("start")
        if not isinstance(start, str):
            return f"its reservation {key} records PID {pid}, which runs, with no start to compare", None
        if " ".join(start.split()) == table[pid]["start"]:
            return f"its reservation {key} is live: PID {pid} still runs", None
    return None, f"reservation {key}'s recorded process is gone, and its entry stays for its owner or recovery"


def observe(root, pools, listed):
    """One look at the simulators the census lists as left over: for each launchd_sim by UDID, its
    PID, start, age, name and reason, and hold, why it must keep running, or None. Nothing here
    lists simctl's devices unless the census finds a simulator. Anything unread raises."""
    answers = {}

    def seen(command):
        # One listing of each kind per look, so that every condition reads the same moment.
        key = tuple(command)
        if key not in answers: answers[key] = listed(command)
        return answers[key]

    census = leftovers(root, seen, lambda pid: None)
    if "error" in census: raise ReapProblem(census["error"])
    if census["unchecked"]: raise ReapProblem("the census could not read " + "; ".join(census["unchecked"]))
    require_admissions()
    rows = [row for row in census["leftovers"] if row.get("simulator") and LAUNCHD_SIM.match(row["command"])]
    if not rows: return {}
    table = process_table(seen(["ps", "-axww", "-o", "pid=,ppid=,uid=,lstart=,args="]))
    try:
        groups = json.loads(seen(list(SIMULATORS)))["devices"]
        simulators = {device["udid"].upper(): device for group in groups.values() for device in group}
    except (TypeError, ValueError, KeyError, AttributeError) as error:
        raise ReapProblem(f"simctl did not list its devices ({error!r})") from None
    entries = device_entries()
    own, uid, found = set(lineage(table, os.getpid())), os.getuid(), {}
    for row in rows:
        udid, pid = row["simulator"], row["pid"]
        device, seconds = simulators.get(udid) or {}, row.get("seconds")
        name = device.get("name") or "unnamed"
        reason, hold = None, None
        keys = [key for key, entry in entries.items() if key == name or str(entry.get("device") or "").upper() == udid]
        holds = [reservation_hold(key, entries[key], table) for key in keys]
        naming = [other for other, process in table.items() if process["uid"] == uid and other not in own
                  and pid not in lineage(table, other) and udid in process["args"].upper()]
        pool = pool_of(name, pools)
        if sum(other["simulator"] == udid for other in rows) > 1: hold = "more than one launchd_sim runs for it"
        elif device.get("state") != "Booted": hold = f"simctl shows it {device.get('state') or 'unlisted'}, not Booted"
        elif pool is None: hold = "it is no member of a simulator pool"
        elif naming: hold = f"PID {naming[0]} names its UDID"
        elif any(item[0] for item in holds): hold = next(item[0] for item in holds if item[0])
        elif seconds is None: hold = "its launchd_sim's start cannot be read"
        elif seconds < REAP_AFTER: hold = f"it has run {seconds / 3600:.1f} h, under {REAP_AFTER / 3600:g} h"
        if hold is None:
            note = next((item[1] for item in holds if item[1]), None)
            reason = (f"member of pool {pool}, left over: no live run, admission or process accounts for it, no live "
                      f"reservation holds it, and it has run {seconds / 3600:.1f} h") + (f"; {note}" if note else "")
        found[udid] = {"udid": udid, "name": name, "pid": pid, "start": row["start"], "seconds": seconds,
                       "hold": hold, "reason": reason}
    return found


@contextlib.contextmanager
def record_lock():
    """The lock each change of the device record takes (devices.Record), held only for the last check
    and the start of a stop, so that no reservation lands between them."""
    path = record_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path.with_name(path.name + ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        began = time.monotonic()
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() - began >= REAP_LOCK_SECONDS:
                    raise ReapProblem(f"the device record stayed locked for {REAP_LOCK_SECONDS}s") from None
                time.sleep(0.02)
        yield
    finally:
        os.close(descriptor)


def log_reap(event, item, **more):
    """Append one line to the log of stops, which stays small: one stop writes two lines of about 400 bytes."""
    line = {"time": datetime.now(timezone.utc).isoformat(timespec="seconds"), "event": event,
            **{key: item[key] for key in ("udid", "name", "pid", "start", "seconds", "reason")}, **more}
    path = admission_directory() / REAP_LOG
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NONBLOCK, 0o644)
    try: os.write(descriptor, (json.dumps(line) + "\n").encode())
    finally: os.close(descriptor)


def stop_simulator(item, reader, budget, begin, report):
    """Check item once more under the device record's lock, and if it is still the same stale simulator,
    log it and start its one shutdown; then report how that ended. Nothing retries it."""
    with record_lock():
        again = observe(report["root"], report["pools"], reader(min(budget(), REAP_CHECK_SECONDS))).get(item["udid"])
        if again is None or again["hold"] or (again["pid"], again["start"]) != (item["pid"], item["start"]):
            why = "the census no longer lists it" if again is None else again["hold"] or "another launchd_sim runs for it"
            report["skipped"].append({**item, "hold": f"it changed before the second check passed: {why}"})
            return
        item = again
        log_reap("stopping", item)
        process = begin(item["udid"])
    try:
        # At least long enough to say something of a stop that started late in the budget.
        said = process.communicate(timeout=max(budget(), 10))[1]
    except subprocess.TimeoutExpired:
        # This ends only xcrun, which this file started, and asks nothing of the simulator.
        process.kill()
        with contextlib.suppress(subprocess.TimeoutExpired): process.communicate(timeout=1)
        report["failed"].append({**item, "error": "simctl shutdown did not finish in time, so its state is unknown"})
        log_reap("failed", item, error=report["failed"][-1]["error"])
        return
    if process.returncode:
        error = f"simctl shutdown exited {process.returncode}: {(said or '').strip()}"
        report["failed"].append({**item, "error": error})
        log_reap("failed", item, error=error)
        return
    report["stopped"].append(item)
    log_reap("stopped", item)


def reap(root, pools, dry_run=False, scan=True, listed=None, begin=None, clock=time.monotonic):
    """Shut down the simulators that are stale: `eng simulator-pool reap`, and before work is sent to the
    second Mac. One goes only when it is a member of a pool, among pools or, with scan, those the
    checkouts name; the census lists it left over; nothing naming its UDID runs outside its own processes;
    the device record holds no live reservation of it; and its launchd_sim has run REAP_AFTER; all
    checked again immediately before the stop, under the record's lock. It reads and logs, and never
    erases, deletes or kills. It never raises: whatever it cannot read or do, it says in problem and
    stops nothing further. It takes about REAP_SECONDS at most; listed and begin are for tests.
    It answers with stopped, failed, skipped (stale at first, then changed or out of time), eligible
    (for a dry run) and kept, each with why."""
    report = {"root": root, "pools": sorted(set(pools)), "dryRun": dry_run, "stopped": [], "failed": [], "skipped": [],
              "eligible": [], "kept": [], "problem": None}
    try:
        deadline = clock() + REAP_SECONDS
        budget = lambda: max(1, deadline - clock())

        def reader(seconds):
            # What one look may spend across its commands.
            if listed: return listed
            end = clock() + seconds
            return lambda command: listing(command, max(1, end - clock()))

        if scan: report["pools"] = sorted({*pools, *known_pools(root)})
        if not report["pools"]: return report
        found = observe(root, report["pools"], reader(min(budget(), 60)))
        report["kept"] = [item for item in found.values() if item["hold"]]
        stale = [item for item in found.values() if not item["hold"]]
        if dry_run:
            report["eligible"] = stale
            return report
        for item in stale:
            if deadline - clock() < REAP_STOP_SECONDS:
                report["skipped"].append({**item, "hold": f"this reap's {REAP_SECONDS}s were used up"})
                continue
            stop_simulator(item, reader, budget, begin or start_shutdown, report)
    except Exception as error:
        report["problem"] = f"{type(error).__name__}: {error}" if not isinstance(error, ReapProblem) else str(error)
    return report


def advertise(repository):
    """Name each commit that a checkout of repository holds as CACHE + COMMIT, and remove every other
    ref under CACHE, so that the next push to repository sends only what its checkouts lack. The refs
    are no more than the checkouts. One ref per commit, not per checkout: sessions that advertise at
    once write the same refs, and a checkout that moves meanwhile costs a push its saving, never its
    commit. Return the commits advertised, or why none were."""
    try:
        held = set()
        for block in git("--git-dir", str(repository), "worktree", "list", "--porcelain").split("\n\n"):
            fields = dict((line.split(" ", 1) + [""])[:2] for line in block.splitlines())
            # A checkout whose directory is gone is pruned when its slot is next claimed.
            if "prunable" not in fields and COMMIT.fullmatch(fields.get("HEAD", "")) and fields["HEAD"].strip("0"):
                held.add(fields["HEAD"])
        refs = set(git("--git-dir", str(repository), "for-each-ref", "--format=%(refname)", CACHE).split())
        wanted = {CACHE + commit for commit in held}
        changes = "".join([f"update {ref} {ref[len(CACHE):]}\n" for ref in sorted(wanted - refs)]
                          + [f"delete {ref}\n" for ref in sorted(refs - wanted)])
        # Another session's transaction can hold a ref's lock for a moment; its refs serve as well.
        for attempt in range(2):
            if not changes: break
            done = subprocess.run(["git", "--git-dir", str(repository), "update-ref", "--stdin"], input=changes,
                                  capture_output=True, text=True, timeout=60)
            if done.returncode == 0: break
            if attempt: raise RuntimeError(f"git update-ref failed: {done.stderr.strip()}")
            time.sleep(0.2)
        return {"commits": sorted(held)}
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        return {"error": str(error)}


def answer(request):
    """offload_host.py's answer to a request other than a plan, for `eng doctor`, `eng remote`,
    `eng remote setup`, `eng remote foreground`, `devices recover` and `eng keychain recover`. A census before a dispatch can
    also name repositories to advertise, and then says what each advertises and which Git does so."""
    if "toolchain" in request: return toolchain(request["toolchain"])
    if "keychain" in request: return keychain(request["keychain"])
    if "foreground" in request: return foreground()
    if "leftovers" in request or "device" in request:
        reaped = reap(request["leftovers"], **request["reap"]) if "reap" in request else None
        # A census that fails says why: it must never stop the work it is printed before.
        try: found = leftovers(request["leftovers"]) if "leftovers" in request else device_users(request["device"])
        except Exception as error: found = {"error": f"{type(error).__name__}: {error}"}
        if reaped is not None: found["reap"] = reaped
        if request.get("advertise"):
            found.update(git=output(["git", "--version"]),
                         advertised={path: advertise(Path.home() / path) for path in request["advertise"]})
        return found
    if "images" in request:
        return {"images": images(request["images"]), **stage(Path.home() / request["staging"], request.get("clear"))}
    # A stop or a dropped connection ends the copy, and the staged file is removed.
    for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(number, lambda *_: _stop.set())
    copy = request["receive"]
    return {"failed": receive(sys.stdin.buffer, Path.home() / copy["staging"], copy["build"], copy["size"],
                              copy["developerDir"])}


def git(*arguments, cwd=None):
    result = subprocess.run(["git", *arguments], cwd=cwd, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f"git {' '.join(arguments)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def changes(checkout):
    """Tracked changes and nonignored untracked files, which a run left in a checkout."""
    return git("status", "--porcelain", "--untracked-files=normal", cwd=checkout) if checkout.exists() else ""


def claim(base, count, directory, wait=True):
    """Lock a free checkout slot without changes, waiting while every one is in use, if wait.

    The lock is an flock the run's command inherits, so it holds while that command lives. A
    checkout with changes is left as it is, for inspection, and passed over. The lock's holder
    writes its PID and start into it, so that a census of leftover processes knows a live run's
    processes from those a lost run left (leftovers).
    """
    base.mkdir(parents=True, exist_ok=True)
    started, reported, named = time.monotonic(), None, set()
    record = json.dumps({"pid": os.getpid(), "start": output(["ps", "-p", str(os.getpid()), "-o", "lstart="])})
    while not _stop.is_set():
        changed = 0
        for index in range(count):
            descriptor = os.open(base / f"{index}.lock", os.O_RDWR | os.O_CREAT, 0o644)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(descriptor)
                continue
            # Before git status, whose process is this run's too.
            os.ftruncate(descriptor, 0)
            os.pwrite(descriptor, (record + "\n").encode(), 0)
            checkout = base / str(index) / directory
            left = changes(checkout)
            if not left:
                return index, descriptor
            os.close(descriptor)
            changed += 1
            if index not in named:
                say(f"passing over {checkout}, which a run left with changes:\n{left}")
                named.add(index)
        if changed == count:
            repository = base.parents[1] / "repositories" / f"{directory}.git"
            raise Refusal(f"every checkout under {base} has changes a run left",
                          f"inspect and remove them, each with `git --git-dir {repository} worktree remove --force PATH`")
        if not wait:
            raise Refusal(f"every checkout of {directory} is in use", busy=True)
        waited = int(time.monotonic() - started)
        if waited >= CLAIM_TIMEOUT:
            raise Refusal(f"no checkout of {directory} came free in {CLAIM_TIMEOUT}s", busy=True)
        if reported is None or waited - reported >= REPORT_SECONDS:
            say(f"waiting ({waited}s): every checkout of {directory} is in use")
            reported = waited
        time.sleep(1)
    return None


def place(repository, checkout, commit, ref, origin=None):
    """Check commit out, detached, in a persistent worktree whose ignored inputs and builds stay.

    Only the toolkit names an origin: a heavy lane's policy refresh fetches it."""
    if origin and output(["git", "--git-dir", str(repository), "config", "--get", "remote.origin.url"]) != origin:
        git("--git-dir", str(repository), "config", "remote.origin.url", origin)
    if checkout.exists():
        git("checkout", "--detach", "--quiet", commit, cwd=checkout)
    else:
        git("--git-dir", str(repository), "worktree", "prune")
        git("--git-dir", str(repository), "worktree", "add", "--detach", "--quiet", str(checkout), commit)
    if git("rev-parse", "HEAD", cwd=checkout) != commit:
        raise RuntimeError(f"{checkout} is not at {commit}")
    # The detached worktree keeps the commit reachable; the pushed ref only carried it here. The
    # checkouts' commits are named instead, for the next push (advertise).
    subprocess.run(["git", "--git-dir", str(repository), "update-ref", "-d", ref], capture_output=True)
    advertised = advertise(repository)
    if "error" in advertised: say(f"could not name what the checkouts of {repository} hold: {advertised['error']}")


def credentials(environment, path):
    """Give the run's git the second Mac's credential file and no other helper.

    SwiftPM clones private dependencies with git, and an SSH session cannot unlock the keychain
    that osxkeychain, Xcode's git default, reads. The variables reach only this run, so the
    owner's own git configuration on the second Mac is unchanged."""
    count = int(environment.get("GIT_CONFIG_COUNT", "0"))
    for offset, value in enumerate(["", f"store --file={shlex.quote(str(path))}"]):
        environment[f"GIT_CONFIG_KEY_{count + offset}"] = "credential.helper"
        environment[f"GIT_CONFIG_VALUE_{count + offset}"] = value
    environment["GIT_CONFIG_COUNT"] = str(count + 2)


def defer_devices(kit, mac):
    """Make this Mac's device record defer physical devices to mac's, the sending Mac's.

    A paired iPhone or iPad is reachable from both Macs, so it is reserved in one record only;
    this one then refuses to reserve it. A record that defers them to another Mac, or holds one,
    is a configuration problem for the owner, so the work does not start."""
    try:
        deferred = subprocess.run([sys.executable, "-m", "vocaro_engineering.devices", "defer", mac], capture_output=True,
                                  text=True, env={**os.environ, "PYTHONPATH": str(kit / "src/python"), **NO_BYTECODE},
                                  timeout=60)
    except subprocess.TimeoutExpired:
        deferred = subprocess.CompletedProcess([], 1, "", "it did not finish in 60s")
    if deferred.returncode:
        problem = deferred.stderr.strip().splitlines()[-1:] or [f"exit {deferred.returncode}"]
        raise Refusal(f"its device record cannot defer physical devices to {mac}: {problem[0]}",
                      "decide which Mac's record holds physical devices and resolve that in the second Mac's record "
                      "(guides/shared-host.md, Physical devices and the second Mac)")
    if deferred.stdout.strip(): say(deferred.stdout.strip())


def relay(stream):
    for line in iter(stream.readline, b""):
        emit(b"O", line)


def follow(path, done):
    """Send the lane's log from its start as it grows. The lane creates it afresh."""
    stream, pending = None, b""
    try:
        while True:
            finished = done.is_set()
            if stream is None:
                try: stream = path.open("rb")
                except FileNotFoundError: pass
            if stream is not None:
                *lines, pending = (pending + stream.read()).split(b"\n")
                for line in lines: emit(b"L", line)
            if finished: break
            time.sleep(0.2)
    finally:
        if stream is not None: stream.close()
    if pending: emit(b"L", pending)


def send_back(directory, paths):
    """Send each of these files under directory that exists, whole: an `F` line naming it, then `D`
    lines of its bytes in base64, so that a file's every byte arrives and each line stays one frame.
    A path that ends in / sends every file under it, by its path under directory, links followed nowhere."""
    files = []
    for path in paths:
        if not path.endswith("/"):
            files.append(path)
            continue
        for parent, folders, names in os.walk(directory / path):
            folders.sort()
            files += [Path(parent, name).relative_to(directory).as_posix() for name in sorted(names)
                      if not Path(parent, name).is_symlink()]
    for path in files:
        try: stream = (directory / path).open("rb")
        except OSError: continue
        with stream:
            emit(b"F", json.dumps({"path": path}).encode())
            for chunk in iter(lambda: stream.read(FILE_CHUNK), b""):
                emit(b"D", base64.b64encode(chunk))


def step(command, directory, environment, lock, log):
    """Run one eng command in directory; a stop sends it SIGTERM, which it carries out as a supervised stop."""
    if log is not None: log.unlink(missing_ok=True)
    child = subprocess.Popen(command, cwd=directory, env=environment, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True,
                             pass_fds=(lock,))
    done = threading.Event()
    threads = [threading.Thread(target=relay, args=(child.stdout,), daemon=True)]
    if log is not None: threads.append(threading.Thread(target=follow, args=(log, done), daemon=True))
    for thread in threads: thread.start()
    forwarded = False
    while child.poll() is None:
        if _stop.is_set() and not forwarded:
            say("stop requested; sending SIGTERM to the run, which stops its command and leaves admission")
            try: os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError: pass
            forwarded = True
        time.sleep(0.1)
    done.set()
    for thread in threads: thread.join(DRAIN_SECONDS)
    return child.returncode


def watch():
    """The end of standard input is the dispatcher's stop.

    It reads the descriptor, not sys.stdin: Python 3.9 aborts at exit when a daemon thread is
    blocked holding the buffered reader's lock (observed on macOS 27.2, October 2026)."""
    try:
        while os.read(0, 65536): pass
    except OSError:
        pass
    _stop.set()


def main(plan):
    for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(number, lambda *_: _stop.set())
    threading.Thread(target=watch, daemon=True).start()
    # The seconds each stage here took: the wait for a free checkout, placing the commits in it, and each
    # eng command, whose own receipt tells its queue's wait from its run.
    phases = {}
    result = {"started": time.time(), "exitStatus": None, "stopped": False,
              "host": identity(plan.get("developerDir")), "phases": phases}
    timed = lambda name, began: phases.__setitem__(name, round(time.monotonic() - began, 3))
    # Until the first command starts, a failure is a refusal: nothing ran. The dispatcher may run
    # the work on its own Mac instead only when the refusal is busy.
    begun, back = False, None
    try:
        expected, found = plan.get("xcode"), result["host"]["xcode"]
        if expected and found != expected:
            version = lambda text: " ".join(text.split())
            raise Refusal(f"the Xcode at {plan['developerDir']} is {version(found or 'missing')}, not the dispatcher's "
                          f"{version(expected)}", "install this Mac's Xcode at that path on the second Mac "
                          "(guides/shared-host.md, Setting up the second Mac), or select with DEVELOPER_DIR an Xcode "
                          "both Macs have at the same path")
        root, project, toolkit = Path.home() / plan["root"], plan["project"], plan.get("toolkit")
        unlocked = unlock_for_run(root)
        if unlocked: say(f"unlocked {unlocked} for this run's session, with the password keep-unlocked kept")
        base = root / "checkouts" / project["directory"]
        began = time.monotonic()
        claimed = claim(base, plan["checkouts"], project["directory"], wait=not plan.get("decline"))
        timed("claim", began)
        if claimed is None:
            result["stopped"] = True
            return result
        index, lock = claimed
        checkout = base / str(index) / project["directory"]
        began = time.monotonic()
        place(root / project["repository"], checkout, project["commit"], project["ref"], project.get("origin"))
        kit = checkout
        if toolkit is not None:
            kit = base / str(index) / toolkit["directory"]
            place(root / toolkit["repository"], kit, toolkit["commit"], toolkit["ref"], toolkit.get("origin"))
        timed("place", began)
        result.update(checkout=str(checkout), slot=index)
        say(f"{checkout} at {project['commit'][:12]}")
        environment = {**os.environ, "VOCARO_ENGINEERING_ROOT": str(kit), **NO_BYTECODE}
        if plan.get("deviceRecord"): defer_devices(kit, plan["deviceRecord"])
        # The reservations, held in the sending Mac's record, that this run may use (devices.LEASE).
        if plan.get("lease"): environment["VOCARO_DEVICE_LEASE"] = json.dumps(plan["lease"])
        if plan.get("developerDir"): environment["DEVELOPER_DIR"] = plan["developerDir"]
        # A receipt the work writes here says that it ran on the second Mac, sent from this Mac (remote.DISPATCHER).
        if plan.get("dispatcher"): environment["VOCARO_DISPATCHED_FROM"] = plan["dispatcher"]
        if (root / "git-credentials").is_file(): credentials(environment, root / "git-credentials")
        eng = [sys.executable, str(kit / "bin/eng"), "--project", str(checkout)]
        steps = [("provision", [*eng, "run", "provision"], checkout / ".build/engineering/provision/run.log",
                  checkout)] if plan.get("provision") else []
        # `eng admit --verify` runs its command in the checkout's directory that matches the sender's.
        steps.append(("work", [*eng, *plan["work"]], checkout / plan["log"] if plan.get("log") else None,
                      checkout / plan["subdirectory"] if plan.get("subdirectory") else checkout))
        returns = plan.get("returns")
        if returns:
            back = checkout / returns["directory"], returns["paths"]
            # The work writes them afresh, so none that an earlier run left goes back as this run's.
            for path in back[1]:
                if path.endswith("/"): shutil.rmtree(back[0] / path, ignore_errors=True)
                else: (back[0] / path).unlink(missing_ok=True)
        for stage, command, log, directory in steps:
            if _stop.is_set(): break
            begun, began = True, time.monotonic()
            result.update(stage=stage, exitStatus=step(command, directory, environment, lock, log))
            timed(stage, began)
            if result["exitStatus"] != 0: break
        result["stopped"] = _stop.is_set()
        receipt = checkout / plan["receipt"]
        # A command that failed before it wrote a receipt leaves the previous run's in place.
        fresh = receipt.is_file() and receipt.stat().st_mtime >= result["started"]
        if fresh:
            result["receipt"] = json.loads(receipt.read_text())
        # `eng run --if-free` declined the lane before running it, and wrote no receipt.
        if plan.get("decline") and result.get("stage") == "work" and result["exitStatus"] == plan["busy"] \
                and not fresh:
            result.update(refused="its heavy-work queue cannot take the lane now", busy=True)
    except Refusal as refusal:
        result.update(refused=str(refusal), busy=refusal.busy, fix=refusal.fix)
    except (OSError, RuntimeError, ValueError) as error:
        result["error" if begun else "refused"] = str(error)
    finally:
        result["finished"] = time.time()
        # However the work ended, a stop or a failure included, what it wrote that the plan asks back.
        if back is not None and begun:
            try: send_back(*back)
            except OSError as error: say(f"could not send back what the run wrote in {back[0]}: {error}")
        emit(b"R", json.dumps(result).encode())
    return result


if __name__ == "__main__":
    request = json.loads(sys.argv[1])
    if "keepUnlocked" in request: sys.exit(keep_unlocked(request["keepUnlocked"]))
    if {"toolchain", "images", "receive", "leftovers", "device", "keychain", "foreground"} & request.keys():
        print(json.dumps(answer(request)))
    else: main(request)
