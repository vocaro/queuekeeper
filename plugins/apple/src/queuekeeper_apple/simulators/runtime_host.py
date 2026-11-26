"""Another Mac's side of the Apple plugin's runtime images, sent over SSH and run there by that Mac's own Python,
and this Mac's side of the same reads, for `qk remote setup --runtimes` and `qk doctor` (simulators.runtimes) and
`qk disk` (disk.runtimes_there).

Like core queuekeeper/offload_host.py, it imports nothing but the standard library, because it runs before the
toolkit or this plugin is checked out there, and Python 3.9, a Mac's system Python, runs it. Core remote.ask sends
it whole, with a request as its one argument (remote.ask's program): `{"images": DEVELOPER_DIR, ...}` lists the
Mac's runtime images and what an earlier copy left staged (images, stage), `{"deleteRuntime": ...}` deletes one that
no simulator but Xcode's defaults uses (delete_runtime), and `{"receive": ...}` adds one read from standard input,
which core remote.send_file streams to it (receive). It prints its answer as one line of JSON.

Since the sending Mac sends this file with each request, the other Mac's own toolkit and plugin revisions do not
matter to it. A core from before this file existed answered these requests itself, in offload_host.py."""
from datetime import datetime
import glob
import json
import os
from pathlib import Path
import plistlib
import pwd
import re
import shutil
import signal
import subprocess
import sys
import threading

# Bytes of a runtime image read or written at once.
CHUNK = 8 << 20
# The platform a runtime image names when simctl gives no runtime bundle to name it by.
PLATFORMS = {"com.apple.platform.iphonesimulator": "iOS", "com.apple.platform.appletvsimulator": "tvOS",
             "com.apple.platform.watchsimulator": "watchOS", "com.apple.platform.xrsimulator": "visionOS"}
# The device record, relative to the home, as devices.py finds it, which QUEUEKEEPER_DEVICE_RECORD overrides for
# a test's (core offload_host.DEVICE_RECORD).
DEVICE_RECORD = "Library/Application Support/queuekeeper/device-handoff.json"
# Device records older toolkits left, which devices.LEGACY names and disk still reads.
LEGACY_RECORDS = "/private/tmp/*device-handoff*.json"
# Set by a stop or a dropped connection, which ends a copy there (receive).
_stop = threading.Event()


def output(command, environment=None, *, timeout=60):
    try:
        result = subprocess.run(command, capture_output=True, text=True, env=environment, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def record_path():
    return Path(os.environ.get("QUEUEKEEPER_DEVICE_RECORD") or Path(pwd.getpwuid(os.getuid()).pw_dir) / DEVICE_RECORD)


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


def xcode_defaults():
    """The UDIDs CoreSimulator's device set names as Xcode's default device for each runtime, as
    simulators.pool.default_devices reads them; this module imports nothing from the toolkit."""
    try:
        with open(Path.home() / "Library/Developer/CoreSimulator/Devices/device_set.plist", "rb") as handle:
            defaults = plistlib.load(handle).get("DefaultDevices", {})
    except (OSError, plistlib.InvalidFileException, ValueError, AttributeError):
        return frozenset()
    return frozenset(udid.upper() for types in defaults.values() if isinstance(types, dict)
                     for udid in types.values() if isinstance(udid, str))


def runtime_users(developer_dir):
    """The names of the simulators on each runtime identifier, but Xcode's defaults, which Xcode makes
    again: what deleting a runtime would take from someone. None when simctl lists no devices."""
    listed = output(["xcrun", "simctl", "list", "devices", "-j"], {**os.environ, "DEVELOPER_DIR": developer_dir})
    try:
        found = json.loads(listed)["devices"]
        defaults = xcode_defaults()
        return {runtime: sorted(device["name"] for device in devices if str(device["udid"]).upper() not in defaults)
                for runtime, devices in found.items()}
    except (TypeError, ValueError, KeyError, AttributeError):
        return None


def running_simulators(developer_dir):
    """Each simulator that is not shut down, as NAME, STATE, Xcode's defaults among them; None when simctl
    lists no devices. Deleting a runtime, even one no device uses, can crash the launch host CoreSimulator
    serves every simulator from, which ends the tests in all of them (vocaro/queuekeeper#154)."""
    listed = output(["xcrun", "simctl", "list", "devices", "-j"], {**os.environ, "DEVELOPER_DIR": developer_dir})
    try:
        return sorted(f"{device['name']}, {device.get('state')}" for devices in json.loads(listed)["devices"].values()
                      for device in devices if device.get("state") != "Shutdown")
    except (TypeError, ValueError, KeyError, AttributeError):
        return None


def used_at(stamp):
    """The time an ISO 8601 stamp such as simctl's lastUsedAt names, or None. Python 3.9's fromisoformat
    reads neither a Z nor a fraction of other than 3 or 6 digits, so both are rewritten first."""
    matched = re.fullmatch(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:?\d\d)?", str(stamp or ""))
    if not matched: return None
    whole, fraction, zone = matched.groups()
    zone = "+00:00" if zone in (None, "Z") else zone if ":" in zone else f"{zone[:3]}:{zone[3:]}"
    try: return datetime.fromisoformat(f"{whole}.{(fraction or '0')[:6].ljust(6, '0')}{zone}").timestamp()
    except ValueError: return None


def chosen_builds(records, matches):
    """For each runtime identifier with several images in records (`simctl runtime list -j`), the build
    CoreSimulator resolves its devices to: the build `simctl runtime match list -j` (matches) chooses
    for an installed SDK, else the build whose image CoreSimulator last used. An identifier whose choice
    neither shows is left out. disk.chosen_builds applies the same rule on this Mac; this module imports
    nothing from the toolkit, since it runs on another Mac's own Python (vocaro-engineering#190)."""
    groups = {}
    for record in records:
        if isinstance(record, dict): groups.setdefault(record.get("runtimeIdentifier", ""), []).append(record)
    sdks = {match["chosenRuntimeBuild"] for match in (matches or {}).values()
            if isinstance(match, dict) and match.get("chosenRuntimeBuild")}
    chosen = {}
    for identifier, group in groups.items():
        builds = {record.get("build") for record in group}
        if len(group) < 2 or None in builds or len(builds) < len(group): continue
        matched = [record["build"] for record in group if record["build"] in sdks]
        if len(matched) == 1:
            chosen[identifier] = matched[0]
            continue
        if matched: continue
        stamps = sorted(((used_at(record.get("lastUsedAt")), record["build"]) for record in group
                         if used_at(record.get("lastUsedAt")) is not None), reverse=True)
        if stamps and (len(stamps) == 1 or stamps[0][0] != stamps[1][0]): chosen[identifier] = stamps[0][1]
    return chosen


def recorded_builds():
    """Every build word this Mac's device records hold but under installedBuilds, the census of builds
    installed when a template was warmed, as disk.recorded_builds reads them; None when a record exists
    but cannot be read. A record that is not JSON is taken whole, word by word."""
    paths = list(dict.fromkeys([record_path(), Path(pwd.getpwuid(os.getuid()).pw_dir) / DEVICE_RECORD]))
    paths += sorted(Path(path) for path in glob.glob(LEGACY_RECORDS))
    # A trial reads its own record alone (trial.py, which this file cannot import).
    if os.environ.get("QUEUEKEEPER_TRIAL", "").strip(): paths = [record_path()]
    found = set()
    for path in paths:
        if not path.exists(): continue
        try: text = path.read_text(errors="replace")
        except OSError: return None
        try: stack = [json.loads(text)]
        except ValueError:
            found.update(re.findall(r"[A-Za-z0-9]+", text))
            continue
        while stack:
            value = stack.pop()
            if isinstance(value, dict): stack += [item for key, item in value.items() if key != "installedBuilds"]
            elif isinstance(value, list): stack += value
            elif isinstance(value, str): found.update(re.findall(r"[A-Za-z0-9]+", value))
    return found


def images(developer_dir, pinned=()):
    """The runtime disk images `simctl runtime list` reports, each with the runtime it holds named as
    toolchain names it, NAME (BUILD), its image file, and in users the simulators on its runtime
    identifier but Xcode's defaults (runtime_users), or None when those cannot be listed. None when
    simctl lists no images.

    A superseded build, one whose identifier CoreSimulator resolves to another build (chosen_builds), runs
    none of those simulators, so it has no users and supersededBy names that build (vocaro-engineering#190).
    A pinned one keeps its identifier's users: a build in pinned, which the caller names because this
    module cannot read the toolkit (disk.pinned_builds), or one this Mac's device records name; and every
    superseded build keeps them while a record cannot be read."""
    environment = {**os.environ, "DEVELOPER_DIR": developer_dir}
    listed = output(["xcrun", "simctl", "runtime", "list", "-j"], environment)
    try:
        records = list(json.loads(listed).values())
    except (TypeError, ValueError, AttributeError):
        return None
    users = runtime_users(developer_dir)
    try: matches = json.loads(output(["xcrun", "simctl", "runtime", "match", "list", "-j"], environment))
    except (TypeError, ValueError): matches = None
    chosen = chosen_builds(records, matches if isinstance(matches, dict) else None)
    recorded = recorded_builds() if chosen else set()
    found = []
    for record in records:
        if not isinstance(record, dict) or not record.get("build"): continue
        bundle = Path(record.get("runtimeBundlePath") or "")
        name = bundle.stem if bundle.suffix == ".simruntime" else \
            f"{PLATFORMS.get(record.get('platformIdentifier'), record.get('platformIdentifier'))} {record.get('version')}"
        file, unreadable = image_file(record)
        identifier, build = record.get("runtimeIdentifier"), record["build"]
        superseded = chosen.get(identifier) if chosen.get(identifier, build) != build else None
        kept = superseded and (build in pinned or recorded is None or build in recorded)
        found.append({"runtime": f"{name} ({build})", "name": name, "build": build,
                      "identifier": record.get("identifier"), "state": record.get("state"),
                      "sizeBytes": record.get("sizeBytes"), "architectures": record.get("supportedArchitectures") or [],
                      "file": file, "unreadable": unreadable, "supersededBy": superseded,
                      "users": None if users is None or not identifier else [] if superseded and not kept
                      else users.get(identifier, [])})
    return sorted(found, key=lambda image: image["runtime"])


def delete_runtime(developer_dir, build, pinned=()):
    """Delete the runtime image of build once simctl lists it, no simulator but Xcode's defaults uses it
    and no simulator on any runtime is running (running_simulators), all read again now: none uses a
    superseded build that nothing pins (images). Return None once it is deleted, else why it was not."""
    found = [image for image in images(developer_dir, pinned) or [] if image["build"] == build]
    if len(found) != 1: return f"simctl lists {len(found)} images of {build}"
    image = found[0]
    if image["users"] is None: return "simctl did not list the simulators that might use it"
    if image["users"]: return f"{', '.join(image['users'])} use{'s' if len(image['users']) == 1 else ''} it"
    running = running_simulators(developer_dir)
    if running is None: return "simctl did not list the simulators that might be running"
    if running:
        more = f" and {len(running) - 3} more" if len(running) > 3 else ""
        return (f"{len(running)} simulator{' is' if len(running) == 1 else 's are'} running "
                f"({'; '.join(running[:3])}{more}), and deleting any runtime can crash the launch host every "
                "simulator shares; run it again once none is running")
    try:
        done = subprocess.run(["xcrun", "simctl", "runtime", "delete", image["identifier"]], capture_output=True,
                              text=True, env={**os.environ, "DEVELOPER_DIR": developer_dir}, timeout=300)
    except (OSError, subprocess.SubprocessError) as error:
        return f"simctl runtime delete failed: {error}"
    if done.returncode: return f"simctl runtime delete exited {done.returncode}: {done.stderr.strip()}"
    return None


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


def answer(request):
    """The answer to one of the requests this file's docstring names."""
    if "deleteRuntime" in request:
        deleted = request["deleteRuntime"]
        return {"problem": delete_runtime(deleted["developer"], deleted["build"], deleted.get("pinned") or ())}
    if "images" in request:
        return {"images": images(request["images"], request.get("pinned") or ()),
                **stage(Path.home() / request["staging"], request.get("clear"))}
    # A stop or a dropped connection ends the copy, and the staged file is removed.
    for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(number, lambda *_: _stop.set())
    copy = request["receive"]
    return {"failed": receive(sys.stdin.buffer, Path.home() / copy["staging"], copy["build"], copy["size"],
                              copy["developerDir"])}


if __name__ == "__main__":
    print(json.dumps(answer(json.loads(sys.argv[1]))))
