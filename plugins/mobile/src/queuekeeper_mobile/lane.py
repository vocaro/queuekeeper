"""What the mobile plugins do around a lane's run and an admitted command's: the device windows it waits in, the
reservations a dispatch to another Mac carries and the device pass it reads (queuekeeper.plugins.DEVICES), for
every platform switched on (platforms.py). A slot has one provider, so this plugin provides it, and the Apple and
Android plugins each register a platform with it instead."""
import json
import os

from queuekeeper import plugins, resources
from queuekeeper_mac_desktop import reservations
from . import device_pass, devices, leases


class Devices(leases.Devices):
    device_owner = f"{devices.OWNER}, else the agent session"

    def lane_devices(self, lane):
        from .settings import lane_devices
        return lane_devices(lane)

    def placement(self, keys, name, *, away=False, on=None, among=None):
        from queuekeeper import offload_host
        from . import reach
        return reach.placement(offload_host.record_path(), keys, name, away=away, on=on, among=among)

    def observations(self):
        from .observations import read_devices
        return read_devices()

    def remote_setup(self, mac):
        return ["-m", "queuekeeper_mobile.devices", "defer", mac]

    def take_window(self):
        # Only the remote work itself reports its admission. Provisioning and nested commands do not.
        window = os.environ.pop(devices.WINDOW, None)
        return json.loads(window) if window is not None else None

    def held_window(self, keys):
        return devices.held_window(keys, devices.default_owner())

    def report_window(self, window, queued=None):
        devices.report_window(window, queued)

    def window_wait(self, name, window, label, *, keys=None, budget=None, fallback=None):
        """Report the last safe start. For a named device plan use passing samples and its budget, or the
        lane timeout when it has neither. An ad hoc command keeps its label-based estimate."""
        if window is None: return None
        measured = resources.measured_seconds(label, devices=keys) if label is not None else None
        return reservations.window_wait(name, window, measured,
                                        budget=fallback if measured is None and budget is None else budget)

    def device_lane_passed(self, results, keys):
        return device_pass.passed(results, keys)


def register():
    """Put the device slot and the device commands in the core."""
    plugins.provide("DEVICES", Devices())
    plugins.COMMANDS.update({
        "device-pass": ("a release's physical-device pass, as a release lane's command: run COMMAND once on each paired "
                        "device named, an iPhone, an iPad or an Android phone, in turn, under its reservation, taking a "
                        "paired device in turn and using one the session reserved within the owner's window: qk "
                        "device-pass --device iPad --device iPhone -- COMMAND; -h for the rest", device_pass.main),
        "devices": ("read or change this Mac's device record, which reserves its physical devices, simulator pool "
                    "members and foreground: qk devices status|show|reserve|update|take|release|recover|run; "
                    "-h for the rest",
                    lambda arguments, project: devices.main(arguments)),
    })
