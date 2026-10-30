"""Identify a held, booted simulator's guest runtime through public read-only simctl.

Catalog identifiers can be duplicated across installed builds. The catalog is supporting
evidence, never the source of the running guest's build. Callers treat unavailable identity
as unmeasured and retain their existing reservation/lifecycle policy.
"""
import hashlib
import json
from pathlib import Path
import plistlib
import subprocess
import uuid

from .lifecycle import command


class RuntimeIdentityUnavailable(ValueError):
    """The running guest's identity could not be established; not a passing observation."""


def _explicit_root(root):
    return (root.is_absolute() and root.name == "RuntimeRoot" and root.parent.name == "Resources"
            and root.parent.parent.name == "Contents" and root.parent.parent.parent.suffix == ".simruntime")


def _query(run, *arguments):
    try:
        return run(*arguments)
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeIdentityUnavailable(f"simctl {' '.join(arguments)} failed: {error}") from error


def catalog_candidates(identifier, *, run=command):
    """Preserve every catalog entry for an identifier, including unavailable duplicates."""
    try:
        entries = json.loads(_query(run, "list", "runtimes", "-j"))["runtimes"]
        if not isinstance(entries, list):
            raise ValueError("runtimes is not a list")
        candidates = [entry for entry in entries if entry.get("identifier") == identifier]
        if not candidates:
            raise ValueError(f"no catalog entries for {identifier}")
        return candidates
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        raise RuntimeIdentityUnavailable(f"Runtime catalog unavailable: {error}") from error


def runtime_for_device(udid, expected_runtime, *, run=command):
    """Return actual guest metadata for this exact booted UDID and expected runtime.

    SIMULATOR_ROOT must name the explicit .simruntime RuntimeRoot. Read only that
    root's SystemVersion.plist, never the host plist or an inferred catalog bundle.
    The original plist bytes are fingerprinted and all matching catalog rows retained.
    """
    try:
        canonical = str(uuid.UUID(udid)).upper()
        listing = json.loads(_query(run, "list", "devices", "-j"))["devices"]
        matches = [(runtime, device) for runtime, devices in listing.items() for device in devices
                   if str(device.get("udid", "")).upper() == canonical]
        if len(matches) != 1:
            raise ValueError(f"expected exactly one device {canonical}; found {len(matches)}")
        runtime, device = matches[0]
        if runtime != expected_runtime:
            raise ValueError(f"device runtime {runtime} differs from expected {expected_runtime}")
        if device.get("state") != "Booted" or device.get("isAvailable") is not True:
            raise ValueError("device is not booted and available")
        candidates = catalog_candidates(runtime, run=run)
        root = Path(_query(run, "getenv", canonical, "SIMULATOR_ROOT").strip())
        if not _explicit_root(root):
            raise ValueError("SIMULATOR_ROOT is not an explicit simulator RuntimeRoot")
        root = root.resolve(strict=True)
        if not _explicit_root(root):
            raise ValueError("resolved SIMULATOR_ROOT is not an explicit simulator RuntimeRoot")
        path = root / "System/Library/CoreServices/SystemVersion.plist"
        raw = path.read_bytes()
        system = plistlib.loads(raw)
        for key in ("ProductName", "ProductVersion", "ProductBuildVersion"):
            if not isinstance(system.get(key), str) or not system[key].strip():
                raise ValueError(f"guest SystemVersion.plist lacks {key}")
        if system["ProductName"] in ("macOS", "Mac OS X"):
            raise ValueError("SystemVersion.plist describes the host OS")
        if system["ProductVersion"] not in {entry.get("version") for entry in candidates}:
            raise ValueError("guest version differs from expected runtime catalog versions")
        return {"identifier": runtime, "version": system["ProductVersion"],
                "buildversion": system["ProductBuildVersion"], "udid": canonical,
                "device": device, "runtimeRoot": str(root), "systemVersionPath": str(path),
                "systemVersion": system, "systemVersionSHA256": hashlib.sha256(raw).hexdigest(),
                "catalogCandidates": candidates}
    except RuntimeIdentityUnavailable:
        raise
    except (OSError, ValueError, KeyError, TypeError, AttributeError, plistlib.InvalidFileException) as error:
        raise RuntimeIdentityUnavailable(f"Simulator runtime identity unavailable: {error}") from error
