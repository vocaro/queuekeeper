"""What the Apple extension does around a lane's run and an admitted command's: the device windows it waits in and
the device pass it reads (queuekeeper.extensions.DEVICES), and the app registrations it keeps. The foreground a run
holds is the Mac desktop extension's (queuekeeper_mac_desktop.lane)."""
import contextlib
import json
import os
import sys

from queuekeeper import extensions, resources
from queuekeeper.resources import BUSY
from . import app_builds, app_registrations, device_pass, devices, leases, run_results, xctest_shards
from queuekeeper_mac_desktop import foreground, reservations
from .simulators import matrix, pool as simulator_pool
from .voiceover import state as voiceover_state


def this_mac():
    from queuekeeper import cli  # cli loads the extension
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
        return ["-m", "queuekeeper_apple.devices", "defer", mac]

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


@contextlib.contextmanager
def preserved_voiceover(owner, *, record, said, voiceover=None):
    """The Mac's VoiceOver state, kept for a run that holds its foreground (mac_desktop.foreground.GUARDS): read once
    the foreground is held, by voiceover when given, and put back before it is released. Always settled: the run's
    child driver may have died between a failed toggle and its own restoration, and nothing tells this process so.
    The foreground is released only once the original state has held (#137). The original is recorded in the entry
    as voiceOverOriginal, with the lease it was read under, for the run's own runner to restore over a session's
    unverified restoration (voiceover_state.recover_restoration; vocaro/EPUBLib#416). A passive driver opts into
    voiceover.state.preserved(target=False) inside the lease itself."""
    with voiceover_state.preserved(owner, record=record, read_state=voiceover, said=said, settle=True) as original:
        current = record.entry(reservations.FOREGROUND)
        reserved, pid, start = reservations.identity(current)
        record.update(reservations.FOREGROUND, owner=owner, revision=current["revision"], voiceOverOriginal={
            "enabled": original, "reservedAt": reserved, "hostProcessIdentifier": pid, "hostProcessStart": start})
        yield


def register():
    """Put the extension's lane hooks and commands in the core."""
    extensions.provide("DEVICES", Devices())
    if preserved_voiceover not in foreground.GUARDS: foreground.GUARDS.append(preserved_voiceover)
    for hooks, hook in ((extensions.AROUND_RUN, app_registrations.around_run), (extensions.REPORT_RESULTS, run_results.report),
                        (extensions.TEST_ALLOWANCE, matrix.default_test_allowance)):
        if hook not in hooks: hooks.append(hook)
    # Each runs tests itself, so one started outside an admission is admitted first (cli.bare_runner).
    extensions.RUNNERS.update({("simulator-tests",), ("xctest",)})
    extensions.COMMANDS.update({
        "simulator-tests": ("build a scheme's tests once and run them on several members of the project's simulator "
                            "pool side by side: qk simulator-tests --scheme S --runtime R --device T --device T; "
                            "-h for the rest", matrix.main),
        "device-pass": ("a release's physical-device pass, as a release lane's command: run COMMAND once on each paired "
                        "device named, in turn, under its reservation, taking the iPad in turn and using an iPhone the "
                        "session reserved within the owner's window: qk device-pass --device iPad --device "
                        "iPhone -- COMMAND; -h for the rest (guides/devices.md, The device pass)", device_pass.main),
        "simulator-pool": ("list, take, release, prune, reap or check members of the project's simulator pool under "
                           "device-record reservations: qk simulator-pool list|take|release|prune|reap|check; "
                           "-h for the rest", simulator_pool.main),
        "results": ("keep a test run's per-test outcomes and seconds in the lane's results/ and print them, for a "
                    "script that runs xcodebuild itself: qk results keep; -h for the rest",
                    lambda arguments, project: run_results.main(arguments)),
        "xctest": ("build a Swift package's tests once and run its XCTest classes in xctest processes of their own, "
                   "side by side, each reporting its skips: qk xctest [-j JOBS] [--filter REGEX]; "
                   "qk xctest -h for the rest", lambda arguments, project: xctest_shards.main(arguments)),
    })
    extensions.PARSED_COMMANDS.update({
        "app": ("run the last good build of the project's app: qk app run opens the newest kept Mac build, and "
                "qk app run --iphone or --ipad installs and launches the device build on that paired device, "
                "building origin/main first when the kept build is older; qk app keep builds and keeps one, as "
                "the merge queue does after each gated landing; qk app status lists them (guides/app-builds.md)",
                app_builds.add_arguments, app_builds.execute, True),
    })
    if keep_app not in extensions.AFTER_LANDING: extensions.AFTER_LANDING.append(keep_app)


def keep_app(checkout, config, head, log, environment):
    """Keep the project's app at head once a gated batch has landed, when its configuration asks for one
    (app_builds.py): the build is started and left to run. Returns whether one was started."""
    if not app_builds.after_landing(config): return None
    return app_builds.spawn_keep(checkout, head, log, env=environment)
