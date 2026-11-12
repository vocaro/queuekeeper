"""What an extension, or a project's own tooling, adds to the core.

The core never imports an extension. Each name here is a hook point: an extension's `register` fills it, and
`load` imports the extensions it is given and calls each one's `register`."""
import contextlib
import importlib
import sys


class Unavailable(RuntimeError):
    """Something a run needs before it starts could not be read, which says nothing of the work: the run is
    unmeasured, not failed."""


# Each () -> {variable: value}: what the environment holds while a command runs.
COMMAND_ENVIRONMENT = []
# Each (): called before a command that reaches the hosts starts (run, admit, remote, land, queue, disk).
HOST_COMMAND = []
# Each (root, config): called before a lane or an admitted command is placed; a ValueError refuses the run.
BEFORE_LANE = []
# Each (root, config): called on the Mac that runs an admitted lane or command, before its command starts.
PREPARE_LANE = []
# At most one (root, stream) -> the identity of what it read, which a run's receipt records as "policy". It runs
# before every admitted lane and command, and raises Unavailable when it cannot read it.
POLICY = []
# Each (root, config) -> [warning]: checks `doctor` adds to its own.
DOCTOR = []
# Each (root, config) -> [warning]: what a landing warns of after its result, beside the gate's budget.
LANDING_WARNINGS = []
# Each (config) -> the commands doctor requires on this Mac for the project, beside its own.
REQUIRED_COMMANDS = []
# Each (root, hosts) -> hosts: what `doctor` fixes of the hosts' comparison itself.
HOST_FIXES = []
# Each (directory, event): called after the queue kept in directory wrote event to its journal (events.emit), by
# the process that wrote it. What one raises is dropped, and none may wait: the journal never holds the queue up.
AFTER_EVENT = []
LOADED = []

class Lane:
    """What an extension does around a lane's run and an admitted command's: the devices and the foreground it holds
    for them, and the alerts it clears. This one, the core's own, does nothing: it knows no devices and no foreground.
    An extension puts its own in LANE."""

    # The words a command's help uses for where the session holding a device is named.
    device_owner = "the agent session"

    def recover(self):
        """Put back what a run that died left changed on this Mac, before the next command starts."""

    def take_window(self):
        """The device window an enclosing run leased to this command, read once, or None."""
        return None

    def foreground_mac(self):
        """The second Mac whose foreground a foreground run takes, or None: the run holds this Mac's."""
        return None

    def require_foreground(self):
        """Refuse foreground work on this Mac unless the session holds its foreground."""

    def held_window(self, keys):
        """The window the session holds the devices named by keys for, or None."""
        return None

    def report_window(self, window, queued=None):
        """Tell the Mac that leased window how long the run waited in the queue."""

    def window_wait(self, name, window, label, *, keys=None, budget=None, fallback=None):
        """The wait for admission that window bounds, after saying its last safe start, or None without a window."""
        return None

    def window_closed(self, name, waiting):
        """BUSY, after saying so, when the run can no longer fit in its device window; otherwise None."""
        return None

    def not_admitted(self, name, waiting, status):
        """status, the end of a wait for admission, after saying so when the device window cut it short."""
        return status

    def foreground_held(self, stack, name, label, timeout, expected=None):
        """None once the second Mac's foreground is held for the work, entered in stack, or BUSY after saying so."""
        return None

    def guarded_alerts(self, stack, purpose, receipt, apps=()):
        """What judges a foreground run by the alerts over the screen, entered in stack; None when nothing does;
        or a status, with nothing run, when the screen could not be cleared."""
        return None

    def cleared_notifications(self, receipt):
        """None once nothing covers the second Mac's screen, or a status, with nothing run."""
        return None

    def around_run(self, commands, root):
        """A context that holds for the run of commands in root."""
        return contextlib.nullcontext()

    def report_results(self, results):
        """Print what a lane kept in its results folder."""

    def device_lane_passed(self, results, keys):
        """Whether the results record a pass on every device of keys."""
        return False


LANE = Lane()


class Dispatch:
    """What an extension adds to a dispatch to the second Mac. This one, the core's own, knows no devices."""

    def lease(self, keys, owner, words, root=None, *, budget=None, commits=()):
        """The reservations held here that a run on the second Mac carries, or None when it uses no device."""
        if keys: raise ValueError("--device names a physical device, which no extension switched on here reserves")
        return None

    def carrying(self, held, root, host, work, name, send):
        """send()'s status, with the run it makes recorded for what held reserves."""
        return send()

    def test_allowance(self, words):
        """The seconds one test may run by default under the lane command words, or None when it has no such limit."""
        return None

    def capture_lines(self, found):
        """The lines that say what the second Mac reported of its capture approvals."""
        return []

    def reserved_devices(self, words):
        """{KEY: IDENTIFIER} of the devices in this Mac's record that words name."""
        return {}

    def named_devices(self, command, root):
        """The hardware a command's words name, which keeps it on this Mac."""
        return []

    def pool(self, root):
        """The project's simulator pool, or None."""
        return None


DISPATCH = Dispatch()
# name -> (help, main(arguments, project)): commands an extension adds that parse their own arguments.
COMMANDS = {}
# name -> (help, add_arguments(parser), execute(root, args, config), whether it reads the project's configuration):
# commands an extension adds to the command's own parser. One that reads no configuration gets None for it.
PARSED_COMMANDS = {}
# name -> (help, add_arguments(parser), read(args)): what an extension adds to `host`, which reads a Mac's state.
HOST_READS = {}
# Each (root, config, head, log, environment): called by the queue's worker after a gated landing.
AFTER_LANDING = []


def policy(root, stream=None):
    """What the registered policy reader read, or None when none is registered."""
    return POLICY[-1](root, stream) if POLICY else None


def command_environment():
    values = {}
    for added in COMMAND_ENVIRONMENT: values.update(added())
    return values


def load(named):
    """Import each extension named and call its `register`, once. Each is a module's name, or (name, folder) for a
    module whose package is in folder, which goes on the import path."""
    for entry in named:
        name, folder = (entry, None) if isinstance(entry, str) else entry
        if name in LOADED: continue
        if folder and folder not in sys.path: sys.path.insert(0, folder)
        importlib.import_module(name).register()
        LOADED.append(name)
