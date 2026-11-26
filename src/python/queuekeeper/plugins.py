"""What a plugin, or a project's own tooling, adds to the core.

The core never imports a plugin. Each name here is a hook point: a plugin's `register` fills it, and
`load` imports the plugins it is given and calls each one's `register`."""
import contextlib
import importlib
import sys


class Unavailable(RuntimeError):
    """Something a run needs before it starts could not be read, which says nothing of the work: the run is
    unmeasured, not failed."""


class HostFault(str):
    """A host-fault explanation with a stable kind that newer workers can recognize."""

    def __new__(cls, words, kind):
        explanation = super().__new__(cls, words)
        explanation.kind = kind
        return explanation


HOST_BUILD_CACHE = "host-build-cache"


# Each () -> {variable: value}: what the environment holds while a command runs.
COMMAND_ENVIRONMENT = []
# Each (environment, root) -> environment: wrappers for the command, in registration order.
COMMAND_WRAPPERS = []
# Each (): called before a command that reaches the hosts starts (run, admit, remote, land, queue, disk).
HOST_COMMAND = []
# Each (lane) -> why the lane cannot run in a sandbox, or None: a lane whose "sandbox" one refuses is refused when the
# configuration is read (sandbox.check). The Apple plugin refuses lanes that run Xcode or SwiftPM.
SANDBOX_REFUSALS = []
# Each (log text) -> the program that applied a sandbox of its own inside a lane's, which macOS refused, or None: what
# a sandboxed lane's failed run says of it (sandbox.observed). The Apple plugin names xcodebuild and SwiftPM.
SANDBOX_NESTED = []
# Each (results) -> why a failed run was its Mac's fault, as text or a HostFault with a recognized kind, or None.
# The result comes from what the lane's commands left in its results folder. The Apple plugin names a build plan
# that kept a Metal Toolchain mount macOS has since replaced (vocaro/queuekeeper#281).
HOST_FAULTS = []
# Each (results, log, began, ended) -> what shows that a lane's failed run failed only because a simulator's system
# process crashed, or None: {"words", "process", "simulator", "report", "captured", "tests"}, from its results folder,
# its log's text and its command's start and end, epoch times. The run ends unmeasured, and the merge queue gates it
# again once, holding the queue when it happens again at the same commit (system_crash). The Apple plugin reads
# XCTest's "Critical process SpringBoard crashed" and the simulator's crash report (vocaro/queuekeeper#12).
SYSTEM_CRASHES = []
# Each (root, config): called before a lane or an admitted command is placed; a ValueError refuses the run.
BEFORE_LANE = []
# Each (root, config): called on the Mac that runs an admitted lane or command, before its command starts.
PREPARE_LANE = []
# At most one (root, stream) -> the identity of what it read, which a run's receipt records as "policy". It runs
# before every admitted lane and command, and raises Unavailable when it cannot read it.
POLICY = []
# Each (root, config) -> [warning]: checks `doctor` adds to its own.
DOCTOR = []
# Explicit opt-in for check-only. Map a pure doctor callback to itself or a repairing callback to a pure variant.
DOCTOR_WITHOUT_REPAIRS = {}
# Each (root, config) -> (errors, warnings): checks `doctor` adds whose errors fail it, as a difference between the
# Macs does.
DOCTOR_CHECKS = []
# Each (root, config) -> [warning]: what a landing warns of after its result, beside the gate's budget.
LANDING_WARNINGS = []
# Each (config) -> the commands doctor requires on this Mac for the project, beside its own.
REQUIRED_COMMANDS = []
# Each (report, selected_location): compare the host inventories in a doctor report, before repairs.
# Called with a local inventory and either a remote inventory or a refused comparison. Pure reporting only.
HOST_INVENTORY_CHECKS = []
# Each (root, hosts) -> hosts: what `doctor` fixes of the hosts' comparison itself.
HOST_FIXES = []
# Each (root, config, reports): what a plugin reads and compares across `doctor`'s Macs after the fixes.
HOST_CHECKS = []
# Each (directory, event): called after the queue kept in directory wrote event to its journal (events.emit), by
# the process that wrote it. What one raises is dropped, and none may wait: the journal never holds the queue up.
AFTER_EVENT = []
LOADED = []
# The folders load put plugins' packages in on the import path: a linked worktree's are its main checkout's, which
# `qk unittest` sets aside while it discovers the tree under test's own modules (parallel.tree_imports).
FOLDERS = []


class Foreground:
    """What a plugin does with a Mac's foreground, its screen, keyboard and focus, around a lane's run and an
    admitted command's: the foreground it holds for them and the alerts it clears. This one, the core's own, does
    nothing: it knows no foreground. One plugin puts its own in FOREGROUND, with `provide`."""

    def foreground_mac(self):
        """Another Mac whose foreground a foreground run takes, or None: the run holds this Mac's."""
        return None

    def require_foreground(self):
        """Refuse foreground work on this Mac unless the session holds its foreground."""

    def window_wait(self, name, label, *, fallback=None):
        """The wait for admission that the window the session holds this Mac's foreground for bounds, as
        Devices.window_wait's, after saying its last safe start; or None without such a window. label names the
        admissions that measure the run, or is None for a run none measures; fallback is the run's estimate
        without a measurement (vocaro/queuekeeper#362)."""
        return None

    def foreground_held(self, stack, name, label, timeout, expected=None, again=None):
        """None once another Mac's foreground is held for the work, entered in stack, or BUSY after saying so.
        again is the command that runs the work again, which a wait that gives up names."""
        return None

    def guarded_alerts(self, stack, purpose, receipt, apps=()):
        """What judges a foreground run by the alerts over the screen, entered in stack; None when nothing does;
        or a status, with nothing run, when the screen could not be cleared."""
        return None

    def cleared_notifications(self, receipt):
        """None once nothing covers another Mac's screen, or a status, with nothing run."""
        return None

    def capture_lines(self, found):
        """The lines that say what another Mac reported of its capture approvals."""
        return []

    def capture_runner_problem(self, found, runner):
        """The reason the exact runner's capture approval cannot be confirmed, or None when it can."""
        return "checking a runner's direct screen-capture approval requires the Mac desktop plugin"


class Devices:
    """What a plugin does with the devices a run uses: the windows it waits in around a lane's run and an
    admitted command's, and the reservations a dispatch to another Mac carries. This one, the core's own, knows no
    devices. One plugin puts its own in DEVICES, with `provide`."""

    # The words a command's help uses for where the session holding a device is named.
    device_owner = "the agent session"

    def observations(self):
        """Device information contributed to visibility, empty without a device provider. Its declared lists each
        device the owner declared, as {"name", "state", "absent", "since", "text"}: state is reached, settling,
        unknown or unreachable, and absent says whether an unreachable one is an error or offline; declaredError says
        why they could not be judged."""
        return {"devices": [], "deferredTo": None, "error": None, "connectionError": None, "checkedAt": None,
                "declared": [], "declaredError": None}

    def remote_setup(self, mac):
        """Python arguments for plugin setup on a receiving Mac, or none. Its command exits 2 when the record it
        sets up refuses; the receiving Mac reports that status alone as the record's configuration
        (offload_host.defer_devices)."""
        return []

    def lane_devices(self, lane):
        """The reservations named by a lane, or none without a device provider."""
        return []

    def placement(self, keys, name, *, away=False, on=None, among=None):
        """Where a run of name on the devices of keys may start, as the owner declared how a Mac may reach each
        (vocaro/queuekeeper#211), or None when none of them is declared, which leaves the run where it was placed.
        Otherwise {"status", "mac", "lines", "receipt"}: status None runs it on mac, saying lines and keeping receipt
        in its receipt; "hold" and "not run" do not run it, saying lines. Without away the run starts on this Mac;
        away asks for another Mac, on for that one by its name in the farm, and among names the Macs it may choose.
        It may wait while a device settles."""
        return None

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
        """BUSY, after saying so, when an estimated run can no longer fit in the window waiting is bounded by, from
        this slot's window_wait or the foreground's; otherwise None. An unestimated diagnostic can try an immediate
        admission and then meets the device runner's own bound, but a window no bounded run enforces, as this
        Mac's foreground's is, refuses it too (vocaro/queuekeeper#362)."""
        if waiting is None or waiting.left() > 0: return None
        if waiting.estimate is None and waiting.window.get("bounded", True): return None
        from queuekeeper import cli, resources  # cli loads the plugins
        print(f"queuekeeper: {name} not run on {cli.this_mac()}: {waiting}. {waiting.unrecorded}", file=sys.stderr,
              flush=True)
        return resources.BUSY

    def not_admitted(self, name, waiting, status):
        """status, the end of a wait for admission, after saying that the window is what cut it short."""
        from queuekeeper import resources
        if waiting is not None and waiting.limited and status == resources.BUSY:
            print(f"queuekeeper: {name} stopped waiting: {waiting}. {waiting.unrecorded}", file=sys.stderr, flush=True)
        return status

    def device_lane_passed(self, results, keys):
        """Whether the results record a pass on every device of keys."""
        return False

    def lease(self, keys, owner, words, root=None, *, budget=None, commits=()):
        """The reservations held here that a run on another Mac carries, or None when it uses no device."""
        if keys: raise ValueError("--device names a physical device, which no plugin switched on here reserves")
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


# The slots one plugin fills, each with the core's own until then (provide).
FOREGROUND = Foreground()
DEVICES = Devices()
SLOTS = {"FOREGROUND": Foreground, "DEVICES": Devices}
# Each (): puts back what a run that died left changed on this Mac, before the next command starts.
RECOVER = []
# Each (commands, root) -> a context that holds for the run of commands in root.
AROUND_RUN = []
# Each (lane, names) -> names from this Mac's environment the lane could use but a dispatch did not send.
REMOTE_ENVIRONMENT = []
# Each () -> (prefix, shell line, report): a probe sent in the remote setup login shell. report receives the
# text after prefix and returns (line to print, passed).
REMOTE_SETUP_CHECKS = []
# Each (host, check) -> the exit status of `qk remote setup --runtimes`, which gives this Mac and host the same
# simulator runtimes, or with check says what it would do; the first registered answers, and with none the core
# refuses --runtimes (remote.setup). The Apple plugin evens out the runtimes of the Xcode this Mac selects.
REMOTE_SETUP_RUNTIMES = []
# Each (results): prints what a lane kept in its results folder.
REPORT_RESULTS = []
# Each (command) -> why another Mac cannot run an admitted command as it runs here because its words name something
# of this Mac's alone, or None (remote.first_command). The Apple plugin names a simulator of this Mac's pool.
COMMAND_KEPT_HERE = []
# Each (words) -> the default allowance in seconds for one simulator test, or None, from the runner's arguments.
TEST_ALLOWANCE = []
# Each (command, arguments) -> the default allowance in seconds for one test, or None. command is the lane's
# configured argv and arguments are the caller's appended words.
LANE_TEST_ALLOWANCE = []


def provide(slot, provider):
    """Put provider in slot, FOREGROUND or DEVICES. A slot has one provider: a second plugin's is refused, since
    the core asks a slot one question and takes one answer. The same plugin registering again changes nothing."""
    held = globals()[slot]
    if not isinstance(provider, SLOTS[slot]): raise TypeError(f"{slot} takes a plugins.{SLOTS[slot].__name__}")
    if type(held) is type(provider): return
    if type(held) is not SLOTS[slot]:
        raise ValueError(f"{type(provider).__module__} provides {slot}, which {type(held).__module__} already does: "
                         "a slot has one provider")
    globals()[slot] = provider


def recover():
    for put_back in RECOVER: put_back()


@contextlib.contextmanager
def around_run(commands, root):
    """Every plugin's context for the run of commands in root, entered in the order registered."""
    with contextlib.ExitStack() as stack:
        for around in AROUND_RUN: stack.enter_context(around(commands, root))
        yield


def report_results(results):
    for report in REPORT_RESULTS: report(results)


def host_fault(results):
    """The first reason a plugin gives that the failed run whose results folder is results was its Mac's fault, or
    None. A reader that cannot read raises nothing here: the run keeps its own verdict."""
    for fault in HOST_FAULTS:
        try: words = fault(results)
        except Exception: continue
        if words: return words
    return None


def system_crash(results, log, began, ended):
    """The first corroborated system process crash a plugin finds to be all that failed the run whose results folder
    is results, or None. A reader that cannot read raises nothing here: the run keeps its own verdict."""
    for crash in SYSTEM_CRASHES:
        try: found = crash(results, log, began, ended)
        except Exception: continue
        if isinstance(found, dict) and found.get("words"): return found
    return None


def kept_here(command):
    """The first reason a plugin gives that command's words keep it on this Mac, in the order registered, or None."""
    for keep in COMMAND_KEPT_HERE:
        reason = keep(command)
        if reason: return reason
    return None


def test_allowance(words):
    for allowance in TEST_ALLOWANCE:
        seconds = allowance(words)
        if seconds is not None: return seconds
    return None


def lane_test_allowance(command, arguments):
    for allowance in LANE_TEST_ALLOWANCE:
        seconds = allowance(command, arguments)
        if seconds is not None: return seconds
    return None


class LaneKey:
    """A key a plugin adds to a lane in queuekeeper.json, and what it does to the lane's run. A plugin puts
    one in LANE_KEYS under the key's name. The key is still listed in schemas/queuekeeper.schema.json, which an editor
    reads without loading any plugin."""

    # Whether a lane that says the key has its Mac's screen read at its start and when it ends unmeasured.
    reads_screen = False

    def check(self, name, lane):
        """Raise ValueError, naming the lane, when its value for the key is not one the key takes."""

    def command(self, lane, command):
        """The command the lane runs, given the one it would run otherwise."""
        return command


# key -> LaneKey: the keys plugins add to a lane.
LANE_KEYS = {}


class Reruns:
    """How a plugin names the tests a failed run of a lane failed and reruns one alone (RERUNS): what a lane's
    failedTests says with a JUnit XML file and a rerun command, for a runner whose results only the plugin reads.

    rerun is the argv, run in the checkout, that reruns one test alone, with {test} for its name. failed(results) is
    ([test], None), the names, in order, of the tests failed by the run whose results folder is results; or (None, why
    it names none). It raises nothing. no_verdict, when true, says that the rerun exits as qk's own runners do, so
    that an unmeasured or unstarted rerun gives no verdict, as a qk unittest rerun's does, and is not a real failure."""

    def __init__(self, rerun, failed, no_verdict=False):
        self.rerun, self.failed, self.no_verdict = list(rerun), failed, no_verdict


# Each (lane) -> a Reruns for a lane, a lane's settings in queuekeeper.json, whose command runs tests the plugin reads,
# or None. The merge queue then reruns a failed run's tests alone before a bisect, as it does a qk unittest lane's, and
# records one that passes alone as a flake (vocaro/queuekeeper#109). The Unity plugin reads a qk unity test lane's, and
# the Apple plugin a qk simulator-tests lane's (vocaro/queuekeeper#2). The lane may not have been checked yet: one it
# cannot read gives None.
RERUNS = []


def lane_reruns(lane):
    """The first Reruns a plugin gives for lane, in the order registered, or None."""
    for reruns in RERUNS:
        found = reruns(lane)
        if found is not None: return found
    return None


def lane_command(lane, command):
    """The command a lane runs once each key a plugin added to it has had its say, in the order registered."""
    for key, added in LANE_KEYS.items():
        if key in lane: command = added.command(lane, command)
    return command


def reads_screen(lane):
    """Whether a key a plugin added makes the lane's result depend on its Mac's screen."""
    return any(added.reads_screen and lane.get(key) for key, added in LANE_KEYS.items())


# key -> check(value): the top-level keys plugins add to queuekeeper.json. check raises ValueError, naming the key,
# when the project's value is not one it takes. The key is still listed in schemas/queuekeeper.schema.json.
CONFIG_KEYS = {}
# name -> (help, main(arguments, project)): commands a plugin adds that parse their own arguments.
COMMANDS = {}
# The words that start each command of a plugin's that runs tests or other heavy work itself, as a lane's command
# does, such as ("xctest",) or ("unity", "test"). Started outside an admission, such a command is admitted and placed
# as `qk admit -- COMMAND` would be (cli.bare_runner).
RUNNERS = set()
# name -> (help, add_arguments(parser), execute(root, args, config), whether it reads the project's configuration):
# commands a plugin adds to the command's own parser. One that reads no configuration gets None for it.
PARSED_COMMANDS = {}
# name -> (help, add_arguments(parser), read(args)): what a plugin adds to `host`, which reads a Mac's state.
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
    """Import each plugin named and call its `register`, once. Each is a module's name, or (name, folder) for a
    module whose package is in folder, which goes on the import path.

    A plugin that builds on others names them in its module's REQUIRES, a tuple of module names. They are loaded
    first, so a project lists only the plugins it uses itself. requiring is the chain of plugins being loaded,
    by which a cycle is refused."""
    for entry in named:
        name, folder = (entry, None) if isinstance(entry, str) else entry
        if name in LOADED: continue
        if name in requiring:
            raise ValueError(f"plugins require each other in a cycle: {' -> '.join([*requiring, name])}")
        if folder and folder not in sys.path: sys.path.insert(0, folder)
        if folder and folder not in FOLDERS: FOLDERS.append(folder)
        module = importlib.import_module(name)
        load(getattr(module, "REQUIRES", ()), (*requiring, name))
        module.register()
        LOADED.append(name)
