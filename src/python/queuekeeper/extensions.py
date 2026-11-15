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
# Each (environment, root) -> environment: wrappers for the command, in registration order.
COMMAND_WRAPPERS = []
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


class Foreground:
    """What an extension does with a Mac's foreground, its screen, keyboard and focus, around a lane's run and an
    admitted command's: the foreground it holds for them and the alerts it clears. This one, the core's own, does
    nothing: it knows no foreground. One extension puts its own in FOREGROUND, with `provide`."""

    def foreground_mac(self):
        """The second Mac whose foreground a foreground run takes, or None: the run holds this Mac's."""
        return None

    def require_foreground(self):
        """Refuse foreground work on this Mac unless the session holds its foreground."""

    def foreground_held(self, stack, name, label, timeout, expected=None, again=None):
        """None once the second Mac's foreground is held for the work, entered in stack, or BUSY after saying so.
        again is the command that runs the work again, which a wait that gives up names."""
        return None

    def guarded_alerts(self, stack, purpose, receipt, apps=()):
        """What judges a foreground run by the alerts over the screen, entered in stack; None when nothing does;
        or a status, with nothing run, when the screen could not be cleared."""
        return None

    def cleared_notifications(self, receipt):
        """None once nothing covers the second Mac's screen, or a status, with nothing run."""
        return None

    def capture_lines(self, found):
        """The lines that say what the second Mac reported of its capture approvals."""
        return []


class Devices:
    """What an extension does with the devices a run uses: the windows it waits in around a lane's run and an
    admitted command's, and the reservations a dispatch to the second Mac carries. This one, the core's own, knows no
    devices. One extension puts its own in DEVICES, with `provide`."""

    # The words a command's help uses for where the session holding a device is named.
    device_owner = "the agent session"

    def observations(self):
        """Device information contributed to visibility, empty without a device provider."""
        return {"devices": [], "deferredTo": None, "error": None, "connectionError": None, "checkedAt": None}

    def remote_setup(self, mac):
        """Python arguments for plugin setup on a receiving Mac, or none. Its command exits 2 when the record it
        sets up refuses; the receiving Mac reports that status alone as the record's configuration
        (offload_host.defer_devices)."""
        return []

    def lane_devices(self, lane):
        """The reservations named by a lane, or none without a device provider."""
        return []

    def take_window(self):
        """The device window an enclosing run leased to this command, read once, or None."""
        return None

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

    def device_lane_passed(self, results, keys):
        """Whether the results record a pass on every device of keys."""
        return False

    def lease(self, keys, owner, words, root=None, *, budget=None, commits=()):
        """The reservations held here that a run on the second Mac carries, or None when it uses no device."""
        if keys: raise ValueError("--device names a physical device, which no extension switched on here reserves")
        return None

    def carrying(self, held, root, host, work, name, send):
        """send()'s status, with the run it makes recorded for what held reserves."""
        return send()

    def reserved_devices(self, words):
        """{KEY: IDENTIFIER} of the devices in this Mac's record that words name."""
        return {}

    def named_devices(self, command, root):
        """The hardware a command's words name, which keeps it on this Mac."""
        return []

    def pool(self, root):
        """The project's simulator pool, or None."""
        return None


# The slots one extension fills, each with the core's own until then (provide).
FOREGROUND = Foreground()
DEVICES = Devices()
SLOTS = {"FOREGROUND": Foreground, "DEVICES": Devices}
# Each (): puts back what a run that died left changed on this Mac, before the next command starts.
RECOVER = []
# Each (commands, root) -> a context that holds for the run of commands in root.
AROUND_RUN = []
# Each (results): prints what a lane kept in its results folder.
REPORT_RESULTS = []
# Each (words) -> the seconds one test may run by default under the lane command words, or None when it sets no
# such limit. The first that sets one is the allowance.
TEST_ALLOWANCE = []


def provide(slot, provider):
    """Put provider in slot, FOREGROUND or DEVICES. A slot has one provider: a second extension's is refused, since
    the core asks a slot one question and takes one answer. The same extension registering again changes nothing."""
    held = globals()[slot]
    if not isinstance(provider, SLOTS[slot]): raise TypeError(f"{slot} takes an extensions.{SLOTS[slot].__name__}")
    if type(held) is type(provider): return
    if type(held) is not SLOTS[slot]:
        raise ValueError(f"{type(provider).__module__} provides {slot}, which {type(held).__module__} already does: "
                         "a slot has one provider")
    globals()[slot] = provider


def recover():
    for put_back in RECOVER: put_back()


@contextlib.contextmanager
def around_run(commands, root):
    """Every extension's context for the run of commands in root, entered in the order registered."""
    with contextlib.ExitStack() as stack:
        for around in AROUND_RUN: stack.enter_context(around(commands, root))
        yield


def report_results(results):
    for report in REPORT_RESULTS: report(results)


def test_allowance(words):
    for allowance in TEST_ALLOWANCE:
        seconds = allowance(words)
        if seconds is not None: return seconds
    return None


class LaneKey:
    """A key an extension adds to a lane in queuekeeper.json, and what it does to the lane's run. An extension puts
    one in LANE_KEYS under the key's name. The key is still listed in schemas/queuekeeper.schema.json, which an editor
    reads without loading any extension."""

    # Whether a lane that says the key has its Mac's screen read at its start and when it ends unmeasured.
    reads_screen = False

    def check(self, name, lane):
        """Raise ValueError, naming the lane, when its value for the key is not one the key takes."""

    def command(self, lane, command):
        """The command the lane runs, given the one it would run otherwise."""
        return command

    def placement(self, lane):
        """(HERE, why) for a lane that must run on this Mac, (THERE, why) for one that must run on the second Mac,
        or None for one either may run. ValueError says that neither Mac can run it."""
        return None


# key -> LaneKey: the keys extensions add to a lane.
LANE_KEYS = {}


def lane_command(lane, command):
    """The command a lane runs once each key an extension added to it has had its say, in the order registered."""
    for key, added in LANE_KEYS.items():
        if key in lane: command = added.command(lane, command)
    return command


# Where a key an extension added says its lane must run (LaneKey.placement): on this Mac, or only on the second Mac.
HERE, THERE = "here", "there"


def placement(lanes, name):
    """(HERE or THERE, why) when a key an extension added says the lane name, or a lane of its group, must run on
    one Mac, else None. A group runs its lanes on one Mac, so lanes of it that must run apart are refused."""
    found = {}
    for member in [name, *lanes[name].get("parallel", [])]:
        for key, added in LANE_KEYS.items():
            if key not in lanes[member]: continue
            try: placed = added.placement(lanes[member])
            except ValueError as error: raise ValueError(f"{member}.{key}: {error}") from None
            if placed: found.setdefault(placed[0], f"{member}.{key}: {placed[1]}")
    if len(found) > 1:
        raise ValueError(f"{name} runs its lanes on one Mac, and they must run apart ({found[HERE]}; {found[THERE]})")
    return next(iter(found.items()), None)


def reads_screen(lane):
    """Whether a key an extension added makes the lane's result depend on its Mac's screen."""
    return any(added.reads_screen and lane.get(key) for key, added in LANE_KEYS.items())


# key -> check(value): the top-level keys extensions add to queuekeeper.json. check raises ValueError, naming the key,
# when the project's value is not one it takes. The key is still listed in schemas/queuekeeper.schema.json.
CONFIG_KEYS = {}
# name -> (help, main(arguments, project)): commands an extension adds that parse their own arguments.
COMMANDS = {}
# The words that start each command of an extension's that runs tests or other heavy work itself, as a lane's command
# does, such as ("xctest",) or ("unity", "test"). Started outside an admission, such a command is admitted and placed
# as `qk admit -- COMMAND` would be (cli.bare_runner).
RUNNERS = set()
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


def load(named, requiring=()):
    """Import each extension named and call its `register`, once. Each is a module's name, or (name, folder) for a
    module whose package is in folder, which goes on the import path.

    An extension that builds on others names them in its module's REQUIRES, a tuple of module names. They are loaded
    first, so a project lists only the extensions it uses itself. requiring is the chain of extensions being loaded,
    by which a cycle is refused."""
    for entry in named:
        name, folder = (entry, None) if isinstance(entry, str) else entry
        if name in LOADED: continue
        if name in requiring:
            raise ValueError(f"extensions require each other in a cycle: {' -> '.join([*requiring, name])}")
        if folder and folder not in sys.path: sys.path.insert(0, folder)
        module = importlib.import_module(name)
        load(getattr(module, "REQUIRES", ()), (*requiring, name))
        module.register()
        LOADED.append(name)
