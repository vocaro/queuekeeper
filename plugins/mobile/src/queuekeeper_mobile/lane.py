"""What the mobile plugins do around a lane's run and an admitted command's: the device windows it waits in, the
reservations a dispatch to another Mac carries and the device pass it reads (queuekeeper.plugins.DEVICES), for
every platform switched on (platforms.py). A slot has one provider, so this plugin provides it, and the Apple and
Android plugins each register a platform with it instead."""
import json
import os
import sys

from queuekeeper import plugins, resources
from queuekeeper.resources import BUSY
from . import device_pass, devices, leases


def this_mac():
    from queuekeeper import cli  # cli loads the plugin
    return cli.this_mac()


class Devices(leases.Devices):
    device_owner = f"{devices.OWNER}, else the agent session"

    def lane_devices(self, lane):
        from .settings import lane_devices
        return lane_devices(lane)

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
        waiting = devices.WindowWait(window, measured, budget=fallback if measured is None and budget is None else budget)
        if waiting.left() > 0: print(f"queuekeeper: {name} waits for admission at most until {waiting.latest.isoformat()}: {waiting}",
                                     file=sys.stderr, flush=True)
        return waiting

    def window_closed(self, name, waiting):
        """Refuse an estimated run that can no longer fit. An unestimated diagnostic can try an immediate
        admission and then meets the device runner's own bound."""
        if waiting is None or waiting.estimate is None or waiting.left() > 0: return None
        print(f"queuekeeper: {name} not run on {this_mac()}: {waiting}. No device result was recorded", file=sys.stderr, flush=True)
        return BUSY

    def not_admitted(self, name, waiting, status):
        """status, the end of a wait for admission, after saying that the device window is what cut it short."""
        if waiting is not None and waiting.limited and status == BUSY:
            print(f"queuekeeper: {name} stopped waiting: {waiting}. No device result was recorded", file=sys.stderr, flush=True)
        return status

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
