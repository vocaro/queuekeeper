"""What the Mac desktop plugin does around a lane's run and an admitted command's: another Mac's foreground it
holds, and the crash-report alerts and notifications it clears. The core's lane runner calls these through
queuekeeper.plugins.FOREGROUND."""
import os
import sys

from queuekeeper import plugins, lane_results
from queuekeeper.resources import BUSY
from . import capture_approvals, crash_alerts, foreground, keychain, notification_center, reservations


def this_mac():
    from queuekeeper import cli  # cli loads the plugin
    return cli.this_mac()


class Foreground(plugins.Foreground):
    def foreground_mac(self):
        return foreground.farm_mac()

    def require_foreground(self):
        reservations.require_foreground(os.environ.get(reservations.OWNER))

    def foreground_held(self, stack, name, label, timeout, expected=None, again=None):
        """None once another Mac's foreground is held for the work label names, entered in stack; or BUSY, after
        saying so, when the wait for it ran out, as admission's does (vocaro-engineering#297). Nothing ran, and the
        TimeoutError raised, an OSError, would be queuekeeper's own error, exit 2, which the merge queue counts as a
        failure. Only entering is caught: a release that fails once the work has run does not make the work not run.
        A wait that gives up on a holder whose expected release falls after its deadline names again, the command
        to run after that release (vocaro/queuekeeper#95)."""
        options = {"expected": foreground.EXPECTED if expected is None else expected, "again": again}
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
            print(f"queuekeeper: unmeasured: {error}; nothing ran", file=sys.stderr)
            receipt["outcome"] = "unmeasured"
            return lane_results.UNMEASURED

    def cleared_notifications(self, receipt):
        """None once no Notification Center window is over another Mac's screen, queuekeeper having closed the
        notifications of any that was, under the foreground the run holds; or UNMEASURED, with nothing run, naming
        a window it could not clear (notification_center.py). The receipt says what was found and closed, never
        what a notification said."""
        try:
            receipt["notificationCenter"] = notification_center.dismiss()
            return None
        except (notification_center.Blocked, reservations.Refused) as error:
            print(f"queuekeeper: unmeasured: {error}; nothing ran",
                  file=sys.stderr)
            receipt["notificationCenter"] = {"left": str(error)}
            receipt["outcome"] = "unmeasured"
            return lane_results.UNMEASURED

    def capture_lines(self, found):
        return capture_approvals.report(found)

    def capture_runner_problem(self, found, runner):
        return capture_approvals.runner_problem(found, runner)


def notifications_arguments(parser):
    parser.add_argument("--dismiss", action="store_true", help="close the windows' notifications; needs that "
                        "Mac's mac-foreground lease, named by QUEUEKEEPER_DEVICE_OWNER")


def register():
    """Put the plugin's lane hooks and commands in the core."""
    plugins.provide("FOREGROUND", Foreground())
    # A foreground run whose command died left crash-report dialogs off; the next command puts them back.
    if crash_alerts.recover not in plugins.RECOVER: plugins.RECOVER.append(crash_alerts.recover)
    # A run sent to another Mac unlocks its login keychain there, for its own session, before its command.
    if keychain.unlock_run not in plugins.PREPARE_LANE: plugins.PREPARE_LANE.append(keychain.unlock_run)
    # The device record's command line, for its foreground; the mobile plugin, loaded after this one, replaces
    # it with one that adds its devices' actions.
    plugins.COMMANDS["devices"] = (
        "read or change this Mac's device record, which reserves its foreground: qk devices "
        "status|show|reserve|update|release|check|recover; -h for the rest",
        lambda arguments, project: reservations.main(arguments))
    plugins.PARSED_COMMANDS.update({
        "keychain": ("before the owner unlocks a keychain, say which Mac's keychain the work needs and print commands "
                     "that run only there (recover); keep another Mac's login keychain unlocked for every qk "
                     "remote run (keep-unlocked); where the automation runs, check the keychain, the signing identity "
                     "and signing with it, without a prompt (check)",
                     keychain.add_arguments, lambda root, args, config: keychain.execute(args), False),
    })
    plugins.HOST_READS["notifications"] = (
        "list the Notification Center windows over this Mac's screen, exiting 0 when none is and 3 when one is; with "
        "--dismiss, on another Mac under the foreground lease the caller holds, close their notifications and "
        "confirm each window gone, as a foreground run does before it starts (exit 0, 3 naming a window left, 1 "
        "without a lease)", notifications_arguments,
        lambda args: notification_center.main(["check" if args.dismiss else "list"]))
