"""The Apple plugin's disk items for `qk disk`: simulators and their runtimes, Xcode's DerivedData and device
support, the SwiftPM cache, kept app builds (decision E of vocaro/vocaro-engineering#403) and the large caches macOS
tools keep under ~/Library/Caches (vocaro/queuekeeper#292). The core lists and removes them with its own
(queuekeeper.disk); `register` adds them."""
from dataclasses import dataclass, field
import datetime
import glob
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess

from queuekeeper import disk, farm
from queuekeeper.config import TOOLKIT_ROOT
from queuekeeper.disk import (
    CANDIDATE,
    Item,
    KEPT,
    OWNER,
    age,
    cache_item,
    described,
    git,
    last_activity,
    project_workspace,
)
from queuekeeper_mobile import devices as device_record
from . import app_builds as kept_apps, apple_platform  # noqa: F401 -- switches the Apple platform on
from .simulators import pool as simulator_pool

CATEGORIES = {"simulator": "Simulators", "runtime": "Simulator runtimes", "derived-data": "Xcode DerivedData",
              "device-support": "Xcode device support (symbols copied from connected devices)",
              "swiftpm-cache": "SwiftPM shared cache",
              "app-builds": "Kept app builds and their build caches (qk app)",
              "known-caches": "Large caches of macOS tools in ~/Library/Caches"}
# A runtime no device uses is removed once it has gone unused this long, as a pool member is pruned: a
# lane may make its devices on it next.
RUNTIME_IDLE_DAYS = 14
UUID = re.compile(r"[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}", re.IGNORECASE)
# Xcode names a workspace's DerivedData folder NAME-<28 letters>; other folders are shared caches.
WORKSPACE_FOLDER = re.compile(r".+-[a-z]{28}")
# What an xcodebuild leaves in a folder it writes no info.plist for, when it resolves packages or tests
# without building: logs, resolved package checkouts and test results, and no build products to lose.
UNBUILT = frozenset({"Logs", "SourcePackages", "TestResults"})
# A Build folder that holds nothing but ProfileData holds a test run's coverage data, not build products:
# xcodebuild test-without-building without -derivedDataPath leaves one per run (vocaro-engineering#158).
COVERAGE_ONLY = frozenset({"ProfileData"})
# qk app's store of kept builds under the account's home (app_builds.store_root).
APP_STORE = "Library/Application Support/queuekeeper/apps"
# Where Xcode copies a connected device's symbols, one folder for each OS build: "iOS DeviceSupport", and the same
# for watchOS, tvOS and visionOS. Xcode names a folder "MODEL VERSION (BUILD)", as "iPhone17,1 27.2 (24B119)", or
# without the model before Xcode 14, and some with the architecture after it, as "16.0 (20A362) arm64e".
DEVICE_SUPPORT = "Library/Developer/Xcode"
SYMBOLS = re.compile(r"(?:(?P<model>\S+) )?(?P<version>[0-9]+(?:\.[0-9]+)*) \((?P<build>[0-9]+[A-Z][0-9]+[a-z]?)\)"
                     r"(?: (?P<arch>\S+))?")
# Large caches under ~/Library/Caches that a macOS tool fills and fills again as it needs: each folder's name, what
# it holds and whether nothing in it can be wanted (CANDIDATE) or only the owner knows (OWNER) (vocaro/queuekeeper#292).
KNOWN_CACHES = {
    "com.apple.python": ("the bytecode the system's Python, which runs qk, compiles again as it imports", CANDIDATE),
    "com.microsoft.VSCode.ShipIt": ("VS Code's downloads of its past updates, which it fetches again when it updates",
                                    CANDIDATE),
    "Homebrew": ("Homebrew's downloads of the current formulae, which `brew cleanup` keeps and which Homebrew fetches "
                 "again only when it reinstalls one", OWNER),
}


def _json(run, *arguments):
    try:
        result = run(["xcrun", "simctl", *arguments], capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        return json.loads(result.stdout) if result.returncode == 0 else None
    except ValueError:
        return None


def pools(development, run=subprocess.run):
    """The simulator pools the projects' main checkouts name in queuekeeper.json, in the working
    tree or at origin/main, since a main checkout can lag the project's main."""
    if not Path(development).is_dir(): return frozenset()
    found = set()
    for path in Path(development).iterdir():
        if not (path / ".git").is_dir(): continue
        found.add(simulator_pool.configured(path))
        try:
            text = git(run, path, "show", "origin/main:queuekeeper.json", timeout=30).stdout
            found.add(json.loads(text).get("simulatorPool"))
        except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
            pass
    return frozenset(filter(None, found))


def default_devices(directory):
    """The devices CoreSimulator made for each runtime, which any session may use."""
    return simulator_pool.default_devices(directory)


def device_records():
    """Every device reservation record's text, or None when a record exists but cannot be read.

    The devices module owns the paths. The account's record is read even under an override, so an
    isolated record never hides a real reservation, and legacy records still count."""
    paths = list(dict.fromkeys([device_record.record_path(), device_record.default_path()]))
    paths += sorted(map(Path, glob.glob(device_record.LEGACY)))
    records = {}
    for path in paths:
        if not path.exists(): continue
        try: records[str(path)] = path.read_text(errors="replace")
        except OSError: records[str(path)] = None
    return records


def disposable_name(name):
    """A disposable device carries a UUID in its unique name; a pool or default device never does."""
    return bool(UUID.search(name))


def idle_since(stamp, now):
    """Seconds since an ISO 8601 stamp such as simctl's lastUsedAt, or None when there is none to read."""
    if not stamp: return None
    try: return now - datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError): return None


def last_used(device, directory, now):
    idle = idle_since(device.get("lastUsedAt") or device.get("lastBootedAt"), now)
    if idle is not None: return idle
    return now - last_activity(directory) if directory and os.path.isdir(directory) else None


def prune_command(pool, udid):
    """Pruning reserves the member, checks it again and records the deletion."""
    return ["python3", str(TOOLKIT_ROOT / "bin/qk"), "simulator-pool", "--pool", pool, "--owner", "qk disk",
            "prune", udid]


def pool_members(host):
    """Each pool member's UDID: its pool, the member and the pool's members."""
    found = {}
    for pool in host.pools:
        every = simulator_pool.members(pool, host.simulators)
        found.update({member.udid: (pool, member, every) for member in every})
    return found


def pool_templates(host):
    """Each pool's warmed templates by UDID: the pool and the template."""
    return {template.udid: (pool, template) for pool in host.pools
            for template in simulator_pool.templates(pool, host.simulators)}


def record_entries(host):
    """Every readable record's entries, as one record for the pruning rule."""
    entries = {}
    for text in host.records.values():
        try: value = json.loads(text) if text else {}
        except ValueError: continue
        if isinstance(value, dict) and isinstance(value.get("devices"), dict): entries.update(value["devices"])
    return {"devices": entries}


def simulators(host, usage, sizes):
    if host.simulators is None:
        return [Item("simulator", "simctl", KEPT, "`xcrun simctl list -j` failed, so no simulator can be shown unowned")]
    unreadable = [path for path, text in host.records.items() if text is None]
    pooled, entries, templated = pool_members(host), record_entries(host), pool_templates(host)
    now = datetime.datetime.fromtimestamp(host.now, datetime.timezone.utc)
    items, listed = [], set()
    for runtime, devices in host.simulators.get("devices", {}).items():
        for device in devices:
            udid, name = device["udid"].upper(), device.get("name", "")
            listed.add(udid)
            data = device.get("dataPath")
            directory = os.path.dirname(data) if data else (str(host.device_directory / udid) if host.device_directory else "")
            size = sizes.of(directory) if directory and os.path.isdir(directory) else \
                int(device.get("dataPathSize") or 0) + int(device.get("logPathSize") or 0)
            idle = last_used(device, directory, host.now)
            label = f"{udid} {name} ({runtime.rsplit('.', 1)[-1]})"
            def item(status, reason, command=None):
                return Item("simulator", label, status, reason, size, command, idle, directory or None)
            command = ["xcrun", "simctl", "delete", udid]
            # A record names a device by UDID or by its name as a JSON string.
            record = next((path for path, text in host.records.items() if text and (udid in text.upper() or
                          (name and json.dumps(name).upper() in text.upper()))), None)
            use = usage.of(directory) if directory else None
            process = usage.naming(udid)
            if device.get("state") != "Shutdown": items.append(item(KEPT, f"{device.get('state')}: a session is using it"))
            elif unreadable: items.append(item(KEPT, "a device record could not be read: " + ", ".join(unreadable)))
            elif use: items.append(item(KEPT, "in use: " + described(use)))
            elif process: items.append(item(KEPT, f"named in the arguments of PID {process.pid}"))
            # The pruning rule reads the record's entries for a member itself.
            elif udid in pooled:
                pool, member, every = pooled[udid]
                allowed, why = simulator_pool.prunable(member, every, simulator_pool.entries_for(member, entries), now,
                                                       idle_kinds=True)
                items.append(item(CANDIDATE, f"a member of the {pool} pool eligible for pruning: {why}",
                                  prune_command(pool, udid)) if allowed else
                             item(KEPT, f"a member of the {pool} pool, kept: {why}"))
            elif udid in templated:
                pool, template = templated[udid]
                # Pruning the last member of a kind unused for 7 days deletes the kind's template with it.
                allowed, why = simulator_pool.template_prunable(
                    template, simulator_pool.members(pool, host.simulators),
                    simulator_pool.entries_for(template, entries), now)
                gone = "unavailable, its runtime gone" if not device.get("isAvailable", True) else why
                items.append(item(CANDIDATE, f"the {pool} pool's warmed template of its kind, {gone}; the next new "
                                             "member of the kind warms another", prune_command(pool, udid))
                             if allowed else
                             item(KEPT, f"the {pool} pool's warmed template of its kind, kept: {why}"))
            elif record: items.append(item(KEPT, f"named in the device record {record}"))
            elif disposable_name(name):
                if idle is not None and idle >= host.min_idle:
                    gone = "" if device.get("isAvailable", True) else "; unavailable, its runtime is gone"
                    items.append(item(CANDIDATE, f"a disposable device (unique name) left shut down and unreferenced; "
                                                 f"last used {age(idle)} ago{gone}", command))
                else: items.append(item(KEPT, "a disposable device with no recorded use" if idle is None
                                        else f"a disposable device last used {age(idle)} ago"))
            elif not device.get("isAvailable", True):
                items.append(item(CANDIDATE, f"unavailable ({device.get('availabilityError', 'runtime missing')}), "
                                             "so nothing can run on it", command))
            elif udid in host.default_devices:
                items.append(item(CANDIDATE, "Xcode's default device for its runtime, which Xcode makes again; a "
                                             "script that picks a device by name fills it instead of a pool member",
                                  command))
            else:
                items.append(item(KEPT, "a named device in no project's pool, perhaps made by hand; "
                                        "only its owner removes it"))
    if host.device_directory and host.device_directory.is_dir():
        for directory in host.device_directory.iterdir():
            if directory.is_dir() and UUID.fullmatch(directory.name) and directory.name.upper() not in listed:
                use = usage.of(directory)
                items.append(Item("simulator", str(directory), KEPT if use else CANDIDATE,
                                  "in use: " + described(use) if use else "a device directory CoreSimulator no longer lists",
                                  sizes.of(directory), None if use else ["rm", "-rf", "--", str(directory)],
                                  host.now - last_activity(directory)))
    return items


def runtimes_there(second):
    """Another Mac's runtime images, read as `qk remote setup --runtimes` reads them, each with its build and
    the simulators on its runtime identifier there (users); {"error": why} when they cannot be read, and None
    without another Mac."""
    if not second: return None
    from queuekeeper import remote  # remote imports this module
    from .simulators import runtimes as simulator_runtimes  # which imports remote
    try:
        developer = remote.developer_directory()
        if developer is None: return {"error": "this Mac has no Xcode selected"}
        found = simulator_runtimes.runtime_images(second, second, developer)
    except Exception as error:  # an unreachable or misconfigured Mac, whatever it raises
        return {"error": str(error).splitlines()[0][:200] if str(error) else type(error).__name__}
    if not isinstance(found, dict) or not isinstance(found.get("images"), list):
        return {"error": "simctl did not list its runtime images"}
    return {"images": found["images"]}


def across(host, item, build):
    """A runtime candidate as another Mac's copy of it leaves it. The Macs keep the same runtimes
    (vocaro-engineering#123): `qk remote setup --runtimes` deletes a runtime only one Mac lists from that Mac
    when no simulator on its identifier uses it there, and copies it to another Mac otherwise. So a build
    removed here while another Mac's copy counts as used would only be copied back."""
    macs = host.offload_hosts or tuple(filter(None, [host.offload_host]))
    if not macs or item.status != CANDIDATE: return item
    def kept(why): return Item(item.category, item.path, KEPT, f"{item.reason}; but {why}", item.size, None, item.idle)
    # Each of the farm's Macs in turn: one whose copy counts as used, or cannot be judged, keeps the build here.
    for mac in macs:
        there = (host.remote_runtimes_by or {}).get(mac) or (host.remote_runtimes if mac == host.offload_host else None) \
            or {"error": "not read"}
        second = farm.named(mac)
        if "error" in there:
            return kept(f"{second}'s runtimes could not be read ({there['error']}), so whether `qk remote setup "
                        "--runtimes` would copy it back is unknown")
        copies = [image for image in there["images"] if isinstance(image, dict) and image.get("build") == build]
        if not copies:
            item.reason += f"; {second} holds no copy, so removing it here also leaves the Macs alike"
        elif any(image.get("users") is None for image in copies):
            return kept(f"{second} holds a copy whose simulators could not be listed, so whether `qk remote setup "
                        "--runtimes` would copy it back is unknown")
        elif any(image["users"] for image in copies):
            users = max(len(image["users"]) for image in copies)
            return kept(f"{second} holds a copy, which the {users} simulators on its identifier there count as used, so "
                        f"`qk remote setup --runtimes` would copy it back here; it can go once {second}'s copy can")
        else:
            item.reason += (f"; {second} holds a copy no simulator uses there, which `qk remote setup --runtimes` then "
                            "deletes, leaving the Macs alike")
    return item


# What else names runtime builds to keep: each returns {build: where}. The toolkit's VoiceOver framework adds the
# builds it has qualified for guarded activation.
PINNED = []


def pinned_builds():
    """The runtime builds something outside the plugin names, each with where (PINNED)."""
    found = {}
    for pinned in PINNED: found.update(pinned())
    return found


def recorded_builds(text):
    """Every string a device record holds but under installedBuilds, the census of builds installed when a
    template was warmed: a build the census names runs nothing, and leaving it only makes the pool warm that
    template again (simulators.pool.template_current). A record that is not JSON is taken whole, word by word."""
    try: value = json.loads(text)
    except ValueError: return set(re.findall(r"[A-Za-z0-9]+", text))
    found, stack = set(), [value]
    while stack:
        value = stack.pop()
        if isinstance(value, dict): stack += [item for key, item in value.items() if key != "installedBuilds"]
        elif isinstance(value, list): stack += value
        elif isinstance(value, str): found.update(re.findall(r"[A-Za-z0-9]+", value))
    return found


def chosen_builds(host):
    """For each runtime identifier with several installed builds, the build CoreSimulator resolves its devices
    to and how that is known: the build `simctl runtime match list` chooses for an installed SDK, else the build
    whose image CoreSimulator last used. An identifier whose choice neither shows is left out."""
    groups = {}
    for image in host.runtimes.values():
        groups.setdefault(image.get("runtimeIdentifier", ""), []).append(image)
    sdks = {}
    for sdk, match in (host.matches or {}).items():
        if isinstance(match, dict) and match.get("chosenRuntimeBuild"): sdks.setdefault(match["chosenRuntimeBuild"], sdk)
    chosen = {}
    for identifier, images in groups.items():
        builds = {image.get("build") for image in images}
        if len(images) < 2 or None in builds or len(builds) < len(images): continue
        matched = [image for image in images if image["build"] in sdks]
        if len(matched) == 1:
            build = matched[0]["build"]
            chosen[identifier] = (build, f"`simctl runtime match list` chooses for the {sdks[build]} SDK")
            continue
        if matched: continue
        stamps = sorted(((idle_since(image.get("lastUsedAt"), host.now), image["build"]) for image in images
                         if idle_since(image.get("lastUsedAt"), host.now) is not None))
        if stamps and (len(stamps) == 1 or stamps[0][0] != stamps[1][0]):
            chosen[identifier] = (stamps[0][1], "CoreSimulator used last, no installed SDK choosing a build")
    return chosen


def simulators_running(host):
    """Why no runtime can be deleted now, or None: a simulator that is not shut down, or one a reservation in a
    device record still holds, whose run boots it next. CoreSimulator serves every simulator from one launch
    host, and deleting a runtime, even one no device uses, can crash it and end the tests in all of them: on
    2026-11-13 two deletions of unused iOS 27.2 builds crashed SimLaunchHost under another project's gate on
    iOS 27.0, and each of its four shards lost a test (vocaro/queuekeeper#154)."""
    devices = [device for listed in host.simulators.get("devices", {}).values() for device in listed]
    live = sorted(f"{device.get('name', '?')}, {device.get('state')}" for device in devices
                  if device.get("state") != "Shutdown")
    if live:
        more = f" and {len(live) - 3} more" if len(live) > 3 else ""
        return (f"{len(live)} simulator{' is' if len(live) == 1 else 's are'} running "
                f"({'; '.join(live[:3])}{more})")
    names = {device.get("name") for device in devices}
    udids = {str(device.get("udid", "")).upper() for device in devices}
    for key, entry in sorted(record_entries(host)["devices"].items()):
        if not isinstance(entry, dict) or not (key in names or str(entry.get("device") or "").upper() in udids):
            continue
        state = device_record.condition(entry)
        if state != "released": return f"a device record's reservation of the simulator {key} is {state}"
    return None


def runtimes(host):
    """Each runtime image, none of them a candidate while simulators run (simulators_running)."""
    busy = None if host.runtimes is None or host.simulators is None else simulators_running(host)
    def waits(item):
        if not busy or item.status != CANDIDATE: return item
        return Item(item.category, item.path, KEPT, f"{item.reason}; but {busy}, and deleting any runtime can "
                    "crash the launch host every simulator shares, so it waits until none is running or reserved",
                    item.size, None, item.idle)
    return [waits(item) for item in listed_runtimes(host)]


def listed_runtimes(host):
    if host.runtimes is None or host.simulators is None: return []
    used = {}
    for runtime, devices in host.simulators.get("devices", {}).items():
        used[runtime] = used.get(runtime, 0) + len(devices)
    chosen = chosen_builds(host)
    unreadable = [path for path, text in host.records.items() if text is None]
    items = []
    for key, image in host.runtimes.items():
        identifier = image.get("runtimeIdentifier", "")
        label = f"{image.get('identifier', key)} {identifier.rsplit('.', 1)[-1]} ({image.get('build', '?')})"
        size, stamp, build = int(image.get("sizeBytes") or 0), image.get("lastUsedAt"), image.get("build")
        command = ["xcrun", "simctl", "runtime", "delete", image.get("identifier", key)]
        idle = idle_since(stamp, host.now)
        if identifier in chosen and chosen[identifier][0] != build:
            # A superseded build: CoreSimulator resolves the identifier's devices to another build, so none runs
            # on this one (vocaro-engineering#188), unless the toolkit or a reservation names it.
            other, how = chosen[identifier]
            superseded = f"superseded by {other}, the build of {identifier.rsplit('.', 1)[-1]} {how}"
            record = next((path for path, text in host.records.items() if text and build in recorded_builds(text)), None)
            if build in host.pinned: items.append(Item("runtime", label, KEPT, f"{superseded}, but {host.pinned[build]}", size))
            elif unreadable:
                items.append(Item("runtime", label, KEPT, f"{superseded}, but a device record could not be read: "
                                  + ", ".join(unreadable), size))
            elif record: items.append(Item("runtime", label, KEPT, f"{superseded}, but the device record {record} names it", size))
            elif image.get("deletable") is False:
                items.append(Item("runtime", label, KEPT, f"{superseded}, but not deletable: it came with Xcode", size))
            elif idle is not None and idle < RUNTIME_IDLE_DAYS * 86400:
                items.append(Item("runtime", label, KEPT, f"{superseded}, but used {age(idle)} ago, within "
                                  f"{RUNTIME_IDLE_DAYS} days", size, idle=idle))
            else:
                items.append(across(host, Item("runtime", label, CANDIDATE, f"{superseded}, so no device runs on it; "
                                  f"no pin or reservation names it; last used {stamp or 'never'}, over "
                                  f"{RUNTIME_IDLE_DAYS} days ago or never", size, command, idle), build))
        elif used.get(identifier):
            items.append(Item("runtime", label, KEPT, f"{used[identifier]} devices use it" +
                              (f": the build of its identifier {chosen[identifier][1]}" if identifier in chosen else ""), size))
        elif build in host.pinned: items.append(Item("runtime", label, KEPT, f"no device uses it, but {host.pinned[build]}", size))
        elif image.get("deletable") is False:
            items.append(Item("runtime", label, KEPT, "not deletable: it came with Xcode", size))
        elif idle is not None and idle < RUNTIME_IDLE_DAYS * 86400:
            items.append(Item("runtime", label, KEPT, f"no device uses it, but it was used {age(idle)} ago, within "
                              f"{RUNTIME_IDLE_DAYS} days; a lane may create its devices on it next", size, idle=idle))
        else:
            items.append(across(host, Item("runtime", label, CANDIDATE, f"no device uses it; last used "
                                           f"{stamp or 'never'}, over {RUNTIME_IDLE_DAYS} days ago or never; xcodebuild "
                                           "downloads it again", size, command, idle), build))
    return items


def derived_data(host, usage, sizes):
    root = host.home / "Library/Developer/Xcode/DerivedData"
    if not root.is_dir(): return []
    items = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.is_symlink(): continue
        if not WORKSPACE_FOLDER.fullmatch(child.name):
            items.append(Item("derived-data", str(child), KEPT, "shared by every Xcode build", sizes.of(child)))
            continue
        try:
            with open(child / "info.plist", "rb") as handle:
                workspace = plistlib.load(handle).get("WorkspacePath")
        except (OSError, plistlib.InvalidFileException, ValueError):
            workspace = None
        if workspace and not project_workspace(host, workspace): continue  # another workspace's build
        if not workspace:
            names = sorted(entry.name for entry in child.iterdir())
            held = ", ".join(names[:5]) or "nothing"
            built = sorted(entry.name for entry in (child / "Build").iterdir()) if "Build" in names \
                and (child / "Build").is_dir() and not (child / "Build").is_symlink() else None
            if set(names) - {"Build"} <= UNBUILT and ("Build" not in names or built == sorted(COVERAGE_ONLY)):
                # No project to trace it to, but nothing in it a build could need again: a candidate once
                # idle and unused, as a folder whose workspace is gone.
                what = "no build products" if "Build" not in names else "Build holding only coverage data"
                items.append(cache_item(host, usage, sizes, "derived-data", child,
                                        f"records no workspace; holds only {held}, {what}"))
                continue
            use = usage.of(child)
            items.append(Item("derived-data", str(child), KEPT if use else OWNER,
                              "in use: " + described(use) if use else f"records no workspace; holds {held}",
                              sizes.of(child), None if use else ["rm", "-rf", "--", str(child)],
                              host.now - last_activity(child)))
        elif os.path.exists(workspace):
            items.append(Item("derived-data", str(child), KEPT, f"its workspace {workspace} exists", sizes.of(child)))
        else:
            items.append(cache_item(host, usage, sizes, "derived-data", child, f"its workspace {workspace} is gone"))
    return items


def symbols_build(name):
    """(device, version, build, beta) a device support folder's name gives, or None for any other name: device is
    the model and architecture the name has, either None in a name without it, version a tuple to compare and beta
    whether the build is a beta's, whose build number ends in a letter, as 24B5110a."""
    match = SYMBOLS.fullmatch(name)
    if not match: return None
    build = match["build"]
    return ((match["model"], match["arch"]), tuple(int(part) for part in match["version"].split(".")), build,
            build[-1].islower())


def build_order(build):
    """A build's place among one release's builds, as 24A335 before 24B119."""
    match = re.fullmatch(r"([0-9]+)([A-Z])([0-9]+)([a-z]?)", build)
    return (int(match[1]), match[2], int(match[3]), match[4]) if match else (0, "", 0, build)


def device_support(host, usage, sizes):
    """The symbols Xcode copies from a connected device for each OS build it runs, several GiB a folder, which
    Xcode copies again when a device on that build next connects. A folder is a candidate once idle when it is
    for a beta build, or a folder of the same platform has a later release build for the same model (the folders
    whose older names give no model counting as one model); any other folder is the owner's decision, since only
    the owner knows which devices still run that build (vocaro/queuekeeper#292)."""
    root = host.home / DEVICE_SUPPORT
    if not root.is_dir(): return []
    items = []
    for platform in sorted(root.glob("*DeviceSupport")):
        if not platform.is_dir() or platform.is_symlink(): continue
        folders = [(child, symbols_build(child.name)) for child in sorted(platform.iterdir())
                   if child.is_dir() and not child.is_symlink()]
        releases = {}
        for _, found in folders:
            if found and not found[3]: releases.setdefault(found[0], []).append(found)
        for child, found in folders:
            if not found:
                use = usage.of(child)
                items.append(Item("device-support", str(child), KEPT, "in use: " + described(use) if use else
                                  "a name Xcode does not give its device support folders", sizes.of(child)))
                continue
            model, version, build, beta = found
            device = f"the {model[0]}" if model[0] else "devices"
            later = max((other for other in releases.get(model, ()) if (other[1], build_order(other[2])) >
                         (version, build_order(build))), default=None, key=lambda other: (other[1], build_order(other[2])))
            again = f"Xcode copies it again if {device} connects on {build}"
            if beta:
                items.append(cache_item(host, usage, sizes, "device-support", child,
                                        f"symbols of {device} on the beta build {build}; {again}"))
            elif later:
                items.append(cache_item(host, usage, sizes, "device-support", child,
                                        f"symbols of {device} on {build}, superseded by the release build {later[2]} "
                                        f"({'.'.join(map(str, later[1]))}); {again}"))
            else:
                use = usage.of(child)
                items.append(Item("device-support", str(child), KEPT if use else OWNER,
                                  "in use: " + described(use) if use else
                                  f"symbols of {device} on the newest release build {build} it has; only the owner "
                                  f"knows whether a device still runs it; {again}", sizes.of(child),
                                  None if use else ["rm", "-rf", "--", str(child)], host.now - last_activity(child)))
    return items


def known_caches(host, usage, sizes):
    """The large caches macOS tools keep under ~/Library/Caches that `qk disk` knows (KNOWN_CACHES), so a Mac short
    of space shows them beside its builds' caches: a candidate once idle and unused when nothing in it can be wanted,
    the owner's decision otherwise (vocaro/queuekeeper#292)."""
    root = host.home / "Library/Caches"
    items = []
    for name, (what, status) in KNOWN_CACHES.items():
        folder = root / name
        if not folder.is_dir() or folder.is_symlink(): continue
        item = cache_item(host, usage, sizes, "known-caches", folder, what)
        if status == OWNER and item.status == CANDIDATE:
            item.status, item.reason = OWNER, item.reason + "; only the owner knows whether to keep them, to work offline"
        items.append(item)
    return items


def app_builds(host, usage, sizes):
    """qk app's store (app_builds.py): each project's kept builds, which stay, being what `qk app run` opens, and
    its DerivedData for each platform, which only makes the next keep incremental and goes once idle."""
    root = host.home / APP_STORE
    if not root.is_dir(): return []
    items = []
    for project in sorted(child for child in root.iterdir() if child.is_dir() and not child.is_symlink()):
        if (project / "kept").is_dir():
            items.append(Item("app-builds", str(project / "kept"), KEPT, "the last good builds `qk app run` opens, "
                              "the newest two of each platform", sizes.of(project / "kept")))
        derived = project / "derived"
        if derived.is_dir() and kept_apps.Store(project.name, root).busy():
            # The census reads a build's arguments word by word, and this path has a space in it: the lock says.
            items += [Item("app-builds", str(child), KEPT, "a keep is building with it now", sizes.of(child))
                      for child in sorted(derived.iterdir()) if child.is_dir() and not child.is_symlink()]
        elif derived.is_dir():
            items += [cache_item(host, usage, sizes, "app-builds", child, "qk app's build cache, which the next keep "
                                 "builds again in full") for child in sorted(derived.iterdir())
                      if child.is_dir() and not child.is_symlink()]
        # Each checkout's probe builds (`qk app build`): never kept, so a cache once no build of it runs.
        probes = project / kept_apps.PROBES
        for probe in sorted(probes.iterdir()) if probes.is_dir() else []:
            if not probe.is_dir() or probe.is_symlink(): continue
            if kept_apps.Store(probe.name, probes).busy():
                items.append(Item("app-builds", str(probe), KEPT, "a probe build is building with it now",
                                  sizes.of(probe)))
            else:
                items.append(cache_item(host, usage, sizes, "app-builds", probe, "a checkout's probe builds from "
                                        "`qk app build`, which are never kept"))
    return items


def swiftpm_cache(host, usage, sizes):
    root = host.home / "Library/Caches/org.swift.swiftpm"
    items = []
    for kind, what in (("repositories", "a cached clone"), ("artifacts", "a cached binary artifact")):
        directory = root / kind
        if directory.is_dir():
            items += [cache_item(host, usage, sizes, "swiftpm-cache", child, what + " SwiftPM fetches again when needed")
                      for child in sorted(directory.iterdir()) if child.is_dir() and not child.is_symlink()]
    return items


@dataclass
class Host(disk.Host):
    """The core's Host, with what the plugin's items read from the machine."""
    simulators: dict = None
    runtimes: dict = None
    default_devices: frozenset = frozenset()
    device_directory: Path = None
    records: dict = field(default_factory=dict)
    pools: frozenset = frozenset()
    matches: dict = None  # `simctl runtime match list -j`: the runtime build chosen for each SDK
    pinned: dict = field(default_factory=dict)  # runtime builds something else names, each with where
    # Another Mac's runtime images as `qk remote setup --runtimes` reads them (simulators.runtimes), or
    # {"error": why} when they cannot be read; None without another Mac.
    remote_runtimes: dict = None
    # The same for each of the farm's Macs that take work now, by its SSH destination.
    remote_runtimes_by: dict = None

    @classmethod
    def current(cls, min_idle, development=None, run=subprocess.run):
        host = super().current(min_idle, development, run)
        devices = host.home / "Library/Developer/CoreSimulator/Devices"
        host.simulators, host.runtimes = _json(run, "list", "-j"), _json(run, "runtime", "list", "-j")
        host.default_devices, host.device_directory, host.records = default_devices(devices), devices, device_records()
        host.pools = pools(host.development, run)
        host.matches, host.pinned = _json(run, "runtime", "match", "list", "-j"), pinned_builds()
        host.remote_runtimes_by = {mac: runtimes_there(mac) for mac in host.offload_hosts}
        host.remote_runtimes = host.remote_runtimes_by.get(host.offload_host) if host.offload_hosts \
            else runtimes_there(host.offload_host)
        return host


def items(host, usage, sizes, wanted):
    """The plugin's items of the categories in wanted."""
    found = []
    if "simulator" in wanted: found += simulators(host, usage, sizes)
    if "runtime" in wanted: found += runtimes(host)
    if "derived-data" in wanted: found += derived_data(host, usage, sizes)
    if "device-support" in wanted: found += device_support(host, usage, sizes)
    if "swiftpm-cache" in wanted: found += swiftpm_cache(host, usage, sizes)
    if "app-builds" in wanted: found += app_builds(host, usage, sizes)
    if "known-caches" in wanted: found += known_caches(host, usage, sizes)
    return found


def prime(host):
    """The folders that hold many small items, each worth one du pass."""
    return [(host.home / "Library/Developer/Xcode/DerivedData", 1), (host.device_directory, 1),
            (host.home / "Library/Caches/org.swift.swiftpm", 2),
            *((folder, 1) for folder in sorted((host.home / DEVICE_SUPPORT).glob("*DeviceSupport")))]


def pins():
    """What remote reads of pinned_builds, looked up when it asks."""
    return pinned_builds()


def register():
    """Add these items to `qk disk`."""
    from queuekeeper import remote  # remote imports the core's disk
    disk.extend(CATEGORIES, items, prime, Host)
    if pins not in remote.PINNED_BUILDS: remote.PINNED_BUILDS.append(pins)
