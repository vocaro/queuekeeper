"""The Apple plugin's part of the dashboard's device observations: what devicectl says of each paired device's
connection (queuekeeper_mobile.observations reads the record and asks each platform)."""
import json
import subprocess

from queuekeeper import offload_host


def connections_of(answer):
    """Physical devices by hardware UDID, from current or older Xcode devicectl JSON.
    CoreDevice identifiers and simulator UUIDs must never be matched to a reservation by name.
    """
    result = answer.get("result") if isinstance(answer, dict) else None
    devices = result.get("devices") if isinstance(result, dict) else None
    if not isinstance(devices, list): raise ValueError("Xcode returned no device inventory")
    found = {}
    for device in devices:
        if not isinstance(device, dict): continue
        properties = device.get("properties") or {}
        hardware = properties.get("hardware") or device.get("hardwareProperties") or {}
        udid = str(hardware.get("udid") or "").upper()
        if hardware.get("reality") == "simulated" or not offload_host.PHYSICAL.fullmatch(udid): continue
        connection = properties.get("connection") or device.get("connectionProperties") or {}
        state = connection.get("state") or connection.get("tunnelState")
        kind = str(hardware.get("deviceType") or "")
        found[udid] = {"connection": state if state in ("connected", "disconnected", "connecting", "unavailable") else "unknown",
                       "transport": connection.get("transportType"),
                       # What the dashboard's icon and line name: iPhone, iPad or another kind, and its marketing name.
                       "kind": {"iphone": "iphone", "ipad": "ipad"}.get(kind.lower(), "other"),
                       "model": hardware.get("marketingName") if isinstance(hardware.get("marketingName"), str) else None}
    return found


def read_connections():
    """A bounded, read-only Xcode query; runs in the farm's background refresh, never on a page request."""
    result = subprocess.run(["xcrun", "devicectl", "list", "devices", "--timeout", "10", "--quiet", "--json-output", "-"],
                            capture_output=True, text=True, timeout=15)
    if result.returncode:
        raise RuntimeError(f"Xcode device query exited {result.returncode}: {result.stderr.strip()[-400:]}")
    return connections_of(json.loads(result.stdout))


def read_connections():
    """A bounded, read-only Xcode query; runs in the farm's background refresh, never on a page request."""
    result = subprocess.run(["xcrun", "devicectl", "list", "devices", "--timeout", "10", "--quiet", "--json-output", "-"],
                            capture_output=True, text=True, timeout=15)
    if result.returncode:
        raise RuntimeError(f"Xcode device query exited {result.returncode}: {result.stderr.strip()[-400:]}")
    return connections_of(json.loads(result.stdout))
