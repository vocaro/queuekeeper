"""What the Apple extension does around a lane's run and an admitted command's: the device windows it waits in, the
second Mac's foreground it holds, the crash-report alerts and notifications it clears, the app registrations it
keeps, and the device pass it reads. The core's lane runner calls these through queuekeeper.extensions.LANE."""
import json
import os
import sys

from .. import extensions, lane_results, resources
from ..resources import BUSY
from . import app_builds, app_registrations, crash_alerts, device_pass, devices, foreground, keychain, notification_center, \
    run_results, xctest_shards
from .simulators import matrix, pool as simulator_pool


def this_mac():
    from .. import cli  # cli loads the extension
    return cli.this_mac()


class Lane(extensions.Lane):
    device_owner = f"{devices.OWNER}, else the agent session"

    def recover(self):
        # A foreground run whose command died left crash-report dialogs off; the next command puts them back.
        crash_alerts.recover()

    def take_window(self):
        # Only the remote work itself reports its admission. Provisioning and nested commands do not.
        window = os.environ.pop(devices.WINDOW, None)
        return json.loads(window) if window is not None else None

    def foreground_mac(self):
        return foreground.second_mac()

    def require_foreground(self):
        devices.require_foreground(os.environ.get(devices.OWNER))

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

    def foreground_held(self, stack, name, label, timeout, expected=None):
        """None once the second Mac's foreground is held for the work label names, entered in stack; or BUSY, after
        saying so, when the wait for it ran out, as admission's does (vocaro-engineering#297). Nothing ran, and the
        TimeoutError raised, an OSError, would be queuekeeper's own error, exit 2, which the merge queue counts as a
        failure. Only entering is caught: a release that fails once the work has run does not make the work not run."""
        options = {"expected": foreground.EXPECTED if expected is None else expected}
        try:
            stack.enter_context(foreground.slot(label, timeout=timeout, **options))
        except TimeoutError as error:
            print(f"queuekeeper: {name} not run on {this_mac()}: {error}", file=sys.stderr)
            return BUSY
        return None

    def guarded_alerts(self, stack, purpose, receipt, apps=()):
        """Foreground work's crash_alerts.Run, with crash-report dialogs off and the screen's crash-report alerts
        cleared until the stack closes; or UNMEASURED, with nothing run, when an alert queuekeeper cannot clear is
        over the screen (crash_alerts.py). apps names the app under test, as a crash-report alert names its process."""
        try:
            return stack.enter_context(crash_alerts.guarded(purpose, receipt=receipt, apps=apps))
        except crash_alerts.Blocked as error:
            print(f"queuekeeper: unmeasured: {error}; nothing ran ({crash_alerts.GUIDE}, Crash-report alerts)", file=sys.stderr)
            receipt["outcome"] = "unmeasured"
            return lane_results.UNMEASURED

    def cleared_notifications(self, receipt):
        """None once no Notification Center window is over the second Mac's screen, queuekeeper having closed the
        notifications of any that was, under the foreground the run holds; or UNMEASURED, with nothing run, naming
        a window it could not clear (notification_center.py). The receipt says what was found and closed, never
        what a notification said."""
        try:
            receipt["notificationCenter"] = notification_center.dismiss()
            return None
        except (notification_center.Blocked, devices.Refused) as error:
            print(f"queuekeeper: unmeasured: {error}; nothing ran ({notification_center.GUIDE}, {notification_center.SECTION})",
                  file=sys.stderr)
            receipt["notificationCenter"] = {"left": str(error)}
            receipt["outcome"] = "unmeasured"
            return lane_results.UNMEASURED

    def around_run(self, commands, root):
        return app_registrations.around_run(commands, root)

    def report_results(self, results):
        run_results.report(results)

    def device_lane_passed(self, results, keys):
        return device_pass.passed(results, keys)


def notifications_arguments(parser):
    parser.add_argument("--dismiss", action="store_true", help="close the windows' notifications; needs the second "
                        "Mac's mac-foreground lease, named by VOCARO_DEVICE_OWNER")


def register():
    """Put the extension's lane hooks and commands in the core."""
    if not isinstance(extensions.LANE, Lane): extensions.LANE = Lane()
    extensions.COMMANDS.update({
        "simulator-tests": ("build a scheme's tests once and run them on several members of the project's simulator "
                            "pool side by side: queuekeeper simulator-tests --scheme S --runtime R --device T --device T; "
                            "-h for the rest", matrix.main),
        "device-pass": ("a release's physical-device pass, as a release lane's command: run COMMAND once on each paired "
                        "device named, in turn, under its reservation, taking the iPad in turn and using an iPhone the "
                        "session reserved within the owner's window: queuekeeper device-pass --device iPad --device "
                        "iPhone -- COMMAND; -h for the rest (guides/releasing.md, The device pass)", device_pass.main),
        "simulator-pool": ("list, take, release, prune, reap or check members of the project's simulator pool under "
                           "device-record reservations: queuekeeper simulator-pool list|take|release|prune|reap|check; "
                           "-h for the rest", simulator_pool.main),
        "results": ("keep a test run's per-test outcomes and seconds in the lane's results/ and print them, for a "
                    "script that runs xcodebuild itself: queuekeeper results keep; -h for the rest",
                    lambda arguments, project: run_results.main(arguments)),
        "xctest": ("build a Swift package's tests once and run its XCTest classes in xctest processes of their own, "
                   "side by side, each reporting its skips: queuekeeper xctest [-j JOBS] [--filter REGEX]; "
                   "queuekeeper xctest -h for the rest", lambda arguments, project: xctest_shards.main(arguments)),
    })
    extensions.PARSED_COMMANDS.update({
        "app": ("run the last good build of the project's app: queuekeeper app run opens the newest kept Mac build, and "
                "queuekeeper app run --iphone or --ipad installs and launches the device build on that paired device, "
                "building origin/main first when the kept build is older; queuekeeper app keep builds and keeps one, as "
                "the merge queue does after each gated landing; queuekeeper app status lists them (guides/app-builds.md)",
                app_builds.add_arguments, app_builds.execute, True),
        "keychain": ("before the owner unlocks a keychain, say which Mac's keychain the work needs and print commands "
                     "that run only there (recover); keep the second Mac's login keychain unlocked for every queuekeeper "
                     "remote run (keep-unlocked); where the automation runs, check the keychain, the signing identity "
                     "and signing with it, without a prompt (check)",
                     keychain.add_arguments, lambda root, args, config: keychain.execute(args), False),
    })
    extensions.HOST_READS["notifications"] = (
        "list the Notification Center windows over this Mac's screen, exiting 0 when none is and 3 when one is; with "
        "--dismiss, on the second Mac under the foreground lease the caller holds, close their notifications and "
        "confirm each window gone, as a foreground run does before it starts (exit 0, 3 naming a window left, 1 "
        "without a lease)", notifications_arguments,
        lambda args: notification_center.main(["check" if args.dismiss else "list"]))
    if keep_app not in extensions.AFTER_LANDING: extensions.AFTER_LANDING.append(keep_app)


def keep_app(checkout, config, head, log, environment):
    """Keep the project's app at head once a gated batch has landed, when its configuration asks for one
    (app_builds.py): the build is started and left to run. Returns whether one was started."""
    if not app_builds.after_landing(config): return None
    return app_builds.spawn_keep(checkout, head, log, env=environment)
