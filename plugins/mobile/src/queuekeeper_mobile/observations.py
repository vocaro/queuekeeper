"""Physical device observations contributed by the mobile plugins to host visibility."""
import subprocess
import time
from queuekeeper import offload_host
from . import platforms, reach


def read_connections():
    """{identifier: what the dashboard shows} of every platform's devices this Mac reaches (Platform.connections),
    each a bounded, read-only query run in the farm's background refresh, never on a page request. A platform that
    fails fails the reading: what it would have said is unknown."""
    found = {}
    for platform in platforms.PLATFORMS:
        found.update(platform.connections())
    return found


def read_devices():
    """Physical-device reservations alongside each platform's observed connection status, without changing either."""
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
            if not isinstance(entry, dict) or not (
                    offload_host.physical_device_entry(entry) or platforms.physical(entry.get("device"))):
                continue
            state = entry.get("state") or "held"
            if entry.get("uncertain") or state == "restoration-unverified": state = "needs recovery"
            elif entry.get("activeRun"): state = "run recorded"
            found.append({"name": key, "state": state, "owner": entry.get("ownerChatID") if state != "released" else None,
                          "purpose": entry.get("purpose") if state != "released" else None,
                          "releaseBy": entry.get("expectedReleaseBy") if state != "released" else None,
                          "waiting": len(record.get("queues", {}).get(key, [])),
                          **connections.get(platforms.canonical(str(entry["device"])), {
                              "connection": "notDetected" if checked is not None else "unknown", "transport": None,
                              "kind": "other", "model": None})})
        # Each declared device's absence, judged from every Mac's reading after the settle time (reach.py).
        declared, declared_error = [], None
        try:
            declared = reach.health(offload_host.record_path())
        except (OSError, ValueError, RuntimeError) as error:
            declared_error = str(error)
        return {"devices": sorted(found, key=lambda item: item["name"]),
                "deferredTo": record.get(offload_host.DEFERRED), "error": None,
                "connectionError": connection_error, "checkedAt": checked,
                "declared": declared, "declaredError": declared_error}
    except Exception as error:  # noqa: BLE001
        return {"devices": [], "deferredTo": None, "error": str(error), "connectionError": None, "checkedAt": None}
