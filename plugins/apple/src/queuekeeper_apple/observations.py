"""Physical device observations contributed by the Apple plugin to host visibility."""
import json
import subprocess
import time
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


def read_devices():
    """Physical-device reservations alongside Xcode's observed connection status, without changing either."""
    try:
        record = offload_host.device_record_whole()
        connections, connection_error, checked = {}, None, None
        # A deferring Mac's inventory says nothing about connections on the Mac managing its devices.
        if not record.get(offload_host.DEFERRED):
            try:
                connections = read_connections()
                checked = time.time()
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                connection_error = str(error)
        found = []
        for key, entry in record["devices"].items():
            if not isinstance(entry, dict) or not offload_host.PHYSICAL.fullmatch(str(entry.get("device") or "").upper()):
                continue
            state = entry.get("state") or "held"
            if entry.get("uncertain") or state == "restoration-unverified": state = "needs recovery"
            elif entry.get("activeRun"): state = "run recorded"
            found.append({"name": key, "state": state, "owner": entry.get("ownerChatID") if state != "released" else None,
                          "purpose": entry.get("purpose") if state != "released" else None,
                          "releaseBy": entry.get("expectedReleaseBy") if state != "released" else None,
                          "waiting": len(record.get("queues", {}).get(key, [])),
                          **connections.get(str(entry["device"]).upper(), {
                              "connection": "notDetected" if checked is not None else "unknown", "transport": None,
                              "kind": "other", "model": None})})
        return {"devices": sorted(found, key=lambda item: item["name"]),
                "deferredTo": record.get(offload_host.DEFERRED), "error": None,
                "connectionError": connection_error, "checkedAt": checked}
    except Exception as error:  # noqa: BLE001
        return {"devices": [], "deferredTo": None, "error": str(error), "connectionError": None, "checkedAt": None}
