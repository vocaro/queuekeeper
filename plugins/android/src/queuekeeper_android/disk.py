"""The Android plugin's read-only items in the host's disk inventory."""
import os
from pathlib import Path

from queuekeeper import disk
from queuekeeper_mobile import devices

from .emulators import avd, pool


CATEGORIES = {
    "android-avds": "Android emulator pool members and templates",
    "android-logs": "Android emulator test logs in project checkouts",
    "gradle-cache": "Gradle caches and wrapper distributions",
}


def real_directory(path):
    """Whether path is a directory reached without following a link."""
    return path.is_dir() and disk.canonical(path) == disk.canonical(os.path.realpath(path))


def pool_avds(folder, prefixes, records):
    """Name, directory and reservation for AVDs known to the pool on this account."""
    for directory in sorted(folder.glob("*.avd")):
        name = directory.stem
        entry = records.get(name) if records is not None else None
        if not isinstance(entry, dict) or entry.get("device") != f"avd:{name}":
            entry = None
        if not entry and not name.startswith(prefixes):
            continue
        if not real_directory(directory):
            continue
        if not entry:
            config = directory / "config.ini"
            if config.is_symlink() or not config.is_file():
                continue
            settings = avd.ini(config)
            image, profile = settings.get("image.sysdir.1"), settings.get("hw.device.name")
            if not image or not profile:
                continue
            kind = pool.label(avd.image_package(image), profile)
            for prefix in prefixes:
                local = name.removeprefix(f"{prefix}{kind}")
                if local == "_template" or pool.NUMBER.fullmatch(local):
                    break
            else:
                continue
        yield name, directory, entry


def avds(host, usage, sizes, projects):
    folder = avd.home(account_home=host.home).absolute()
    if not real_directory(folder):
        return []
    names = (pool.configured(root) for root in projects)
    prefixes = tuple(sorted({f"{word}_" for name in names if isinstance(name, str) and (word := pool.word(name))}))
    try:
        records = devices.Record().load()["devices"]
    except (devices.Refused, OSError, ValueError):
        records = None
    found = []
    for name, directory, entry in pool_avds(folder, prefixes, records):
        reason = "pool member"
        if name.endswith("_template"):
            reason = "warmed pool template" if (directory / pool.WARMED).is_file() else "pool template"
        idle = None
        if records is None:
            reason += "; the device record could not be read"
        elif entry:
            condition = devices.condition(entry)
            if condition != "released":
                reason += f"; {condition} reservation"
            elif released := devices.moment(entry.get("releasedAt")):
                idle = max(0, host.now - released.timestamp())
                reason += f"; reservation released {disk.age(idle)} ago"
            else:
                reason += "; reservation released at an unknown time"
        else:
            reason += "; no reservation is recorded"
        if use := usage.of(directory):
            reason += "; in use: " + disk.described(use)
        found.append(disk.Item("android-avds", str(directory), disk.KEPT, reason, sizes.of(directory), idle=idle))
    return found


def items(host, usage, sizes, wanted):
    found = []
    if wanted & {"android-avds", "android-logs"}:
        projects = disk.main_checkouts(host)
        if "android-avds" in wanted:
            found += avds(host, usage, sizes, projects)
        if "android-logs" in wanted:
            for root in projects:
                path = root / ".build/queuekeeper/emulator-tests"
                if real_directory(path):
                    found.append(disk.Item("android-logs", str(path), disk.KEPT, "emulator test logs and timings",
                                           sizes.of(path)))
    if "gradle-cache" in wanted:
        override = os.environ.get("GRADLE_USER_HOME")
        gradle = Path(override).expanduser().absolute() if override else host.home / ".gradle"
        for subpath, reason in (("caches", "Gradle build cache"), ("wrapper/dists", "Gradle wrapper distributions")):
            path = gradle / subpath
            if real_directory(path):
                if use := usage.of(path):
                    reason += "; in use: " + disk.described(use)
                found.append(disk.Item("gradle-cache", str(path), disk.KEPT, reason, sizes.of(path)))
    return found


def register():
    disk.extend(CATEGORIES, items)
