"""The physical devices this Mac or another reaches, as `xcrun devicectl list devices` lists them: the Apple platform's
listing for the shared reach (queuekeeper_mobile.reach, Platform.listing_here).

Reach is `connectionProperties.transportType`: `wired` is a cable and `localNetwork` the network. A simulator lists
as `sameMachine` and is left out. `tunnelState` is not reach: on 2026-11-17 it read `disconnected` for a device
plainly attached by cable."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import time

from queuekeeper import offload_host

# devicectl's transport for each way a declaration may name.
TRANSPORTS = {"wired": "cable", "localNetwork": "network"}
# devicectl's transport for a simulator, which runs on the Mac that lists it.
SAME_MACHINE = "sameMachine"
# A file holding a devicectl listing that this Mac's reading takes in place of devicectl's, for tests only: every
# test module sets it to an empty listing (tests/host_isolation.py), so no test reads this Mac's devices.
LISTING = "QUEUEKEEPER_DEVICE_LISTING"
# The listing, read-only. devicectl's own --timeout bounds its wait for devices to answer.
COMMAND = ("xcrun", "devicectl", "list", "devices", "--quiet", "--timeout", "10", "--json-output", "-")
# Seconds one Mac's listing may take, over SSH included. On 2026-11-17 it took 0.17 to 0.42 s here and 0.56 s from
# another Mac over SSH.
TIMEOUT = 30


def listed(answer):
    """Each physical device a devicectl listing names, by hardware UDID: the way it reaches the Mac that listed it
    now (cable, network, or None when it is listed by no way a declaration may name), devicectl's own transport,
    and the device's kind, model and name. Simulators are left out. Current and older devicectl JSON alike."""
    result = answer.get("result") if isinstance(answer, dict) else None
    devices = result.get("devices") if isinstance(result, dict) else None
    if not isinstance(devices, list): raise ValueError("devicectl's listing holds no devices")
    found = {}
    for device in devices:
        if not isinstance(device, dict): continue
        properties = device.get("properties") if isinstance(device.get("properties"), dict) else {}
        hardware = properties.get("hardware") or device.get("hardwareProperties") or {}
        connection = properties.get("connection") or device.get("connectionProperties") or {}
        about = properties.get("device") or device.get("deviceProperties") or {}
        udid = str(hardware.get("udid") or "").upper()
        transport = connection.get("transportType")
        if hardware.get("reality") == "simulated" or transport == SAME_MACHINE or \
                not offload_host.PHYSICAL.fullmatch(udid):
            continue
        text = lambda value: value if isinstance(value, str) else None
        found[udid] = {"way": TRANSPORTS.get(transport), "transport": text(transport),
                       "kind": text(hardware.get("deviceType")), "model": text(hardware.get("marketingName")),
                       "name": text(about.get("name"))}
    return found


def read_here(run=subprocess.run):
    """This Mac's devices, as listed: (listed's answer, seconds the listing took)."""
    started = time.monotonic()
    if os.environ.get(LISTING):
        text = Path(os.environ[LISTING]).read_text()
    else:
        done = run(list(COMMAND), capture_output=True, text=True, timeout=TIMEOUT, stdin=subprocess.DEVNULL)
        if done.returncode:
            raise RuntimeError(f"devicectl exited {done.returncode}: {(done.stderr or '').strip()[-300:]}")
        text = done.stdout
    return listed(json.loads(text)), time.monotonic() - started


def read_there(host):
    """The devices another Mac lists, read over SSH in its login shell: (listed's answer, seconds)."""
    from queuekeeper import remote
    started = time.monotonic()
    done = remote.ssh(host, shlex.join(COMMAND), stdin=subprocess.DEVNULL, capture_output=True, text=True,
                      timeout=TIMEOUT)
    if done.returncode:
        said = (done.stderr or "").strip().splitlines()
        raise RuntimeError(f"its devicectl listing exited {done.returncode}" + (f": {said[-1][-300:]}" if said else ""))
    return listed(json.loads(done.stdout)), time.monotonic() - started


