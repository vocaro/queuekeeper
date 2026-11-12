"""What an extension, or a project's own tooling, adds to the core.

The core never imports an extension. Each name here is a hook point: an extension's `register` fills it, and
`load` imports the extensions it is given and calls each one's `register`."""
import importlib


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
# Each (root, hosts) -> hosts: what `doctor` fixes of the hosts' comparison itself.
HOST_FIXES = []
LOADED = []


def policy(root, stream=None):
    """What the registered policy reader read, or None when none is registered."""
    return POLICY[-1](root, stream) if POLICY else None


def command_environment():
    values = {}
    for added in COMMAND_ENVIRONMENT: values.update(added())
    return values


def load(names):
    """Import each extension named and call its `register`, once."""
    for name in names:
        if name in LOADED: continue
        importlib.import_module(name).register()
        LOADED.append(name)
