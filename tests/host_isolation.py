"""Keep every test out of the host's heavy-work admission (#88).

Every test module imports this first, and test_host_isolation fails one that does not. Importing it
points HEAVY_SLOT_DIR and QUEUEKEEPER_GATE_LOCK_FILE at a directory private to this process, whatever they
named before, so that a test which admits work without redirecting them, by `cli.main([... "run"
...])`, `qk admit` or a subprocess that inherits the environment, admits it there. An entry a
test leaves has no process record, so recovery leaves it, and in the host's guard it blocked
every session's heavy work until it was removed by hand (2026-10-31). The guard's default path is
redirected too, for a test that clears the environment. A test that needs another namespace still
sets its own; the process removes this one when it exits. The admission token the suite inherits is
removed as well: the toolkit's gate is an admitted lane (the owner's decision, 2026-11-07,
vocaro-engineering#200), so its tests run inside the gate's admission, and a test that admits work would
otherwise reuse the host's admission rather than take one in its own namespace, and an exclusive one
would be refused inside it.

Every lane is admitted and offered to the farm's Macs first, and the farm is the namespace's own (queuekeeper.farm),
so it has no Mac: no lane a test runs, in this process or a child, reaches a real Mac of the owner's farm. A test of
a farm Mac writes its fake one into its farm (test_support.farm).

`qk doctor` also fixes the Macs itself: it evens out their runtimes, deleting or copying them, and
shuts down stale pool simulators (cli.fix_hosts). Most doctor tests compare this Mac with the real
second Mac, so cli.fix_hosts is replaced here by one that changes nothing; a test of the fixes calls
FIX_HOSTS, the real one, against fakes. A heavy run here reaps this Mac first (cli.reap_here); QUEUEKEEPER_REAP_BEFORE_RUN
is set to off, so no run a test makes does.

QUEUEKEEPER_EVIDENCE_RETRY is set to off, so no queuekeeper a test runs offloads this Mac's evidence archives.
"""
import atexit
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile

# The environment this process started with, before this module changed it: test_host_isolation reads what this module
# leaves of it (vocaro/queuekeeper#46).
RECEIVED = dict(os.environ)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/python"))
from queuekeeper import cli, offload_host, pre_push, remote, resources, run_progress, shims
from queuekeeper.waiting import stretched  # noqa: F401 -- the tests' bound on a stall, re-exported


def executable(path, text=None):
    """Make path an executable stand-in, writing text there first when given, and return it, once its script
    is checked to run (#214). Every test that puts a stand-in for a tool on PATH makes it with this, and
    test_host_isolation fails a module that marks a file executable itself.

    A stand-in the kernel cannot run is not an error to subprocess or a shell: execvp treats the failure
    as "not here" and runs the next match on PATH, the real tool. An indented `#!` line, a missing
    interpreter or a script its interpreter refuses therefore runs the real xcrun, simctl or git under
    the test; on 2026-11-06 a fake xcrun that did not parse created and booted real simulators while its
    module hung. So the first line must be a `#!` naming an interpreter that exists, a Python script must
    compile, and a shell script must pass its shell's -n."""
    path = Path(path)
    if text is not None: path.write_text(text)
    text = path.read_text()
    first, _, body = text.partition("\n")
    if not first.startswith("#!"):
        raise AssertionError(f"{path}: a stand-in's first line is its #! line, not {first[:60]!r}")
    interpreter, *arguments = first[2:].split()
    named = Path(arguments[0] if Path(interpreter).name == "env" and arguments else interpreter).name
    if Path(interpreter).name == "env" and arguments:
        if shutil.which(arguments[0]) is None:
            raise AssertionError(f"{path}: its interpreter {arguments[0]} is not on PATH")
    elif not os.access(interpreter, os.X_OK):
        raise AssertionError(f"{path}: its interpreter {interpreter} cannot run")
    if named.startswith("python"):
        try:
            compile(body, str(path), "exec")
        except SyntaxError as error:
            raise AssertionError(f"{path}: the stand-in does not compile: {error}") from None
    elif named in ("sh", "bash", "zsh"):
        shell = shutil.which(named) if Path(interpreter).name == "env" else interpreter
        checked = subprocess.run([shell, "-n", str(path)], capture_output=True, text=True)
        if checked.returncode:
            raise AssertionError(f"{path}: {named} -n refuses the stand-in: {checked.stderr.strip()}")
    path.chmod(0o755)
    return path


STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT)


def default_signals():
    """Put each of STOP_SIGNALS back to its default. A test that stops a child it started by one of them passes
    this as the child's preexec_fn (vocaro-engineering#385).

    A child inherits every signal its runner ignores, and the runner is whatever started the suite: a background
    job of a non-interactive shell ignores SIGINT and SIGQUIT, nohup ignores SIGHUP. Python installs no
    KeyboardInterrupt for a SIGINT ignored at its start, and processes.forwarding_signals leaves an ignored signal
    ignored, as it must for nohup. So the stop is dropped and the test fails or waits out its bound, as
    vocaro/BreezEPUB#47 did in two gates. SignalDispositionTests in test_host_isolation runs a child from a
    runner that ignores them, with this and without it."""
    for number in STOP_SIGNALS: signal.signal(number, signal.SIG_DFL)


# A run of these tests goes through `qk unittest`, a lane or `qk admit`, never a bare `python3 -m unittest`, which takes
# no slot and which `qk admissions now` does not show (doc/agent_interface.md, Verifying; vocaro/queuekeeper#36). A run
# with neither an admission's token, as bare_runner reads it, nor qk unittest's mark on its process is told so once on
# standard error, and runs on: TOLD, set here, keeps the processes its tests start from saying it again.
TOLD = "QUEUEKEEPER_UNADMITTED_TESTS_TOLD"
UNADMITTED = ("host_isolation: these tests run outside any admission and outside qk unittest, so they take no slot and "
              "`qk admissions now` does not show them; run them with `python3 scripts/qk unittest -s tests NAME...` "
              "(doc/agent_interface.md, Verifying)")
if not any(os.environ.get(name) for name in (resources.TOKEN, cli.parallel.SHARD_VARIABLE, TOLD)):
    print(UNADMITTED, file=sys.stderr, flush=True)
os.environ[TOLD] = "1"

ROOT = Path(tempfile.mkdtemp(prefix="queuekeeper-test-host-")).resolve()
# Only by this process: a child forked from it exits through atexit too.
atexit.register(lambda owner=os.getpid(): os.getpid() == owner and shutil.rmtree(ROOT, True))
GATE = str(ROOT / "gate")
os.environ.update(HEAVY_SLOT_DIR=str(ROOT / "slots"), QUEUEKEEPER_GATE_LOCK_FILE=GATE)
os.environ.pop(resources.TOKEN, None)
# A suite another Mac runs for a dispatch, as the toolkit's gate is, inherits the dispatch's variables, which make every
# receipt a test writes say it ran there; a test of a dispatch sets them itself.
for variable in (remote.DISPATCHER, "QUEUEKEEPER_DEVICE_LEASE"): os.environ.pop(variable, None)
# Nor does a Mac a test adds to its farm take a hardware UUID the suite was handed for its own (farm.add).
os.environ.pop(offload_host.HARDWARE_VARIABLE, None)
# The gate is a lane, so its suite inherits the name of the gate's own wait record: a lane a test runs would report its
# waits there, into what `qk queue status` says of the gate, and never into its project's record (vocaro-engineering#335).
os.environ.pop(remote.lane_wait.VARIABLE, None)
os.environ.pop("QUEUEKEEPER_LANE_PROGRESS_RUN", None)
# Nor does a lane a test runs write into what the enclosing lane set up for its own command: its results/, whose fixture
# files then read as the gate's own failures and per-test results (vocaro-engineering#289), the checkout whose module
# cache a stale build may clear, its self lane, app registrations, device window and owner, the toolkit it dispatched,
# or the project checkout it named (vocaro/queuekeeper#69), which a lane a test runs names for itself.
for variable in (cli.parallel.RESULTS_VARIABLE, "QUEUEKEEPER_STALE_MODULES_ROOT", cli.SELF_LANE, 'QUEUEKEEPER_APP_REGISTRATIONS_RUN',
                 'QUEUEKEEPER_DEVICE_WINDOW', 'QUEUEKEEPER_DEVICE_OWNER', "QUEUEKEEPER_ROOT",
                 cli.PROJECT_CHECKOUT_VARIABLE):
    os.environ.pop(variable, None)
# Nor does a dispatch a test sends count the keychain as unlocked because the dispatch that runs the suite on the second
# Mac unlocked the real one: it would unlock nothing, and a test of a kept password that cannot unlock would pass the run.
os.environ.pop('QUEUEKEEPER_KEYCHAIN_UNLOCKED', None)
# Nor does a test run as a trial because the suite was started inside one (vocaro/queuekeeper#34): every cli.main a test
# calls would move its namespaces there. A test of a trial sets it itself.
os.environ.pop(cli.trial.VARIABLE, None)
# Nor does a test start as a process that restarted itself because the suite was started by one
# (restartable.RESTARTED): `qk serve` would not open its page. A test of a restart sets it itself.
os.environ.pop("QUEUEKEEPER_RESTARTED", None)
# Nor does a test read what a dispatch, an admission, a device pass or the merge queue tells the command it starts, which
# the suite inherits whenever it is that command (vocaro/queuekeeper#46): the sending Mac's origin and arrival, which
# every admission a test takes would report as its own, and the place it holds here for the run, which every admission
# a test takes would pass; the file where a declined offer's reason goes; what exclusive
# admission read of the Mac; the device a device pass lends; the queue's placement scope, bisect progress, worker lock and push; the
# progress folder an admitted runner names to the run it admits (vocaro/queuekeeper#22). A test of any of them sets it
# itself.
for variable in (resources.ORIGIN, resources.REQUEST_STARTED, resources.PLACE, offload_host.DECLINED, *resources.HOST_READING,
                 'QUEUEKEEPER_DEVICE_KEY', 'QUEUEKEEPER_DEVICE_UDID', remote.QUEUE_GATE, remote.QUEUE_GATE_EXCLUDE,
                 "QUEUEKEEPER_QUEUE_PROGRESS", "QUEUEKEEPER_QUEUE_LOCK", pre_push.QUEUE_VARIABLE, run_progress.VARIABLE):
    os.environ.pop(variable, None)
resources.GATE_LOCK = offload_host.GATE_LOCK = GATE
# Nor does exclusive work a test admits wait for this Mac to settle (vocaro/queuekeeper#121): the suite runs inside the
# gate's admission, so the CPU it reads is never quiet, and each such admission would wait out HEAVY_QUIET_SECONDS. In
# this process too, for a test that clears the environment. A test of the settle sets its own.
os.environ["HEAVY_QUIET_SECONDS"] = "0"
resources.QUIET_SECONDS = 0
# Nor does any admission a test takes depend on how busy this Mac's CPU reads (vocaro/queuekeeper#211). At or above
# its CPU limit a Mac admits only its first job, and the suite runs inside a gate that keeps the CPU busy: a test's
# second admission would wait for a reading under the limit, and Macs that read one CPU would be chosen by its noise.
# So the limit is one no reading reaches. A test of the limit sets its own, and one of the default or of a limits
# file removes this, since a limit's variable overrides both.
os.environ["HEAVY_BUSY_LIMIT"] = "1000"
# Nor does any lane or admitted command a test runs wait for, or stop for, this Mac's free disk space
# (vocaro/queuekeeper#162): the suite runs on whatever this Mac has free, which says nothing of the code it tests. A test
# of either floor sets its own.
os.environ["QUEUEKEEPER_START_FREE_GB"] = os.environ["QUEUEKEEPER_RUN_FREE_GB"] = "0"
os.environ["QUEUEKEEPER_CRASH_DIALOG_RECORD"] = str(ROOT / "crash-dialog.json")
os.environ['QUEUEKEEPER_DEVICE_LOG_WATCH'] = "off"
os.environ['QUEUEKEEPER_QUIT_OPENED_APPS'] = "off"
# Nor does any host identity or lane receipt a test makes read this Mac's scroll-bar setting and HID devices, a second
# of ioreg each time (#229): it reads the setting unset and no pointing device. A test of the reading sets its own.
(ROOT / "scroll-bars.json").write_text(json.dumps({
    "defaults": [1, "", "Error: Could not find key 'AppleShowScrollBars' in domain 'kCFPreferencesAnyApplication'.\n"],
    "ioreg": [0, "", ""]}))
os.environ[offload_host.SCROLL_BARS_RECORDING] = str(ROOT / "scroll-bars.json")
# Nor does any `devices status`, `doctor` or health reading a test makes list this Mac's devices with devicectl, or
# read the owner's device record and write what it read beside it (vocaro/queuekeeper#211): it reads that this Mac
# lists none, in a record of its own that holds nothing. A test of the reading or the record names its own.
(ROOT / "device-listing.json").write_text(json.dumps({"result": {"devices": []}}))
os.environ["QUEUEKEEPER_DEVICE_LISTING"] = str(ROOT / "device-listing.json")
# The record refuses a place a restart empties, which ROOT is when TMPDIR is /private/tmp.
RECORDS = ROOT
if RECORDS == Path("/private/tmp") or Path("/private/tmp") in RECORDS.parents:
    RECORDS = Path(tempfile.mkdtemp(prefix="queuekeeper-test-devices-", dir=subprocess.check_output(
        ["/usr/bin/getconf", "DARWIN_USER_TEMP_DIR"], text=True).strip())).resolve()
    atexit.register(lambda owner=os.getpid(), kept=RECORDS: os.getpid() == owner and shutil.rmtree(kept, True))
os.environ["QUEUEKEEPER_DEVICE_RECORD"] = str(RECORDS / "devices" / "device-handoff.json")


FIX_HOSTS = cli.fix_hosts

# Nor does any reach check a test makes in this process ask macOS which process is responsible for it
# (remote.responsible_for): run by a queue worker whose session ended, every test of an unreachable second Mac would
# read that its own process has lost Local Network access (vocaro-engineering#282). A test of it calls RESPONSIBLE_FOR.
# Nor does one in a queuekeeper a test starts, such as the qk run of a lane a test's queue worker gates: it reads an
# answer that macOS does not say (remote.RESPONSIBLE). Asked of macOS there, eight tests of the merge queue failed in
# the queue's own gate on 2026-11-17, which a worker whose session had ended ran on its own Mac
# (vocaro/queuekeeper#220).
def RESPONSIBLE_FOR(pid):
    """What macOS answers of pid, whatever this module set for the queuekeepers a test starts."""
    kept = os.environ.pop(remote.RESPONSIBLE, None)
    try: return ASKED_OF_MACOS(pid)
    finally:
        if kept is not None: os.environ[remote.RESPONSIBLE] = kept


ASKED_OF_MACOS = remote.responsible_for
remote.responsible_for = lambda pid: None
os.environ[remote.RESPONSIBLE] = "unsaid"

# Nor does any queuekeeper a test runs, in this process or a child, start the evidence offload's background retry,
# which would offer this Mac's real evidence archives to the real Mac of the farm (vocaro-engineering#157). A test
# of retry_soon clears it.
os.environ["QUEUEKEEPER_EVIDENCE_RETRY"] = "off"

# Nor does any heavy run a test makes here, in this process or a child, reap this Mac first (cli.reap_here), which
# would shut down its stale pool simulators and release its dead pool reservations (vocaro-engineering#156). A test
# of the reap clears it.
os.environ[cli.REAP_SWITCH] = "off"

# Nor does any run a test makes add a pinned checkout to this Mac's own (offload_host.pinned_checkout): each is made
# in this process's private directory. The pin set of a run the suite was started from inside is removed, since a
# test's own run makes its own (pins).
os.environ[offload_host.PINS_DIRECTORY] = str(ROOT / "pins")
os.environ.pop(offload_host.PIN_VARIABLE, None)


def unfixed(root, hosts):
    hosts.pop("runtimesDiffer", None)
    return {**hosts, "reaped": [], "deletedDefaults": []}


cli.fix_hosts = unfixed


# What this module sets in the environment, which every process a test starts carries when it inherits that environment.
ISOLATION = {name: os.environ[name] for name in (
    "HEAVY_SLOT_DIR", "QUEUEKEEPER_GATE_LOCK_FILE", "HEAVY_QUIET_SECONDS", "HEAVY_BUSY_LIMIT", "QUEUEKEEPER_START_FREE_GB",
    "QUEUEKEEPER_RUN_FREE_GB",
    "QUEUEKEEPER_CRASH_DIALOG_RECORD", 'QUEUEKEEPER_DEVICE_LOG_WATCH', offload_host.SCROLL_BARS_RECORDING,
    "QUEUEKEEPER_EVIDENCE_RETRY", cli.REAP_SWITCH, offload_host.PINS_DIRECTORY, 'QUEUEKEEPER_QUIT_OPENED_APPS', TOLD,
    remote.RESPONSIBLE, "QUEUEKEEPER_DEVICE_LISTING", "QUEUEKEEPER_DEVICE_RECORD")}


# The prefixes of the toolkit's variables, and those of them this module lets a test inherit, each with why that is
# safe. A variable a run sets for the commands it starts reaches the suite whenever the suite is one of them, on any
# Mac: on 2026-11-12 another Mac's dispatch had unlocked the real keychain, and a test of a password that cannot
# unlock it failed there every time and passed here (vocaro/queuekeeper#46). So test_host_isolation fails when what
# this module leaves holds one that it neither set (ISOLATION) nor lists here, and it plants every such variable the
# toolkit's code names to find one: a new variable is cleared above, or listed here with its reason.
PREFIXES = ("QUEUEKEEPER_", "HEAVY_")
OWNER_LIMIT = "the owner's override of an admission limit or interval, which no run sets for the commands it starts"
OWNER_PATH = "a path the owner or a test names, which no run sets for the commands it starts"
OWNER_SWITCH = "the owner's own switch, which no run sets for the commands it starts"
INHERITED = {
    **dict.fromkeys(("HEAVY_SLOTS", "HEAVY_SLOTS_MAX", "HEAVY_SETTLE_SECONDS", "HEAVY_QUIET_LIMIT",
                     "HEAVY_POLL_SECONDS", "HEAVY_RECOVER_SECONDS", "HEAVY_PLACE_SECONDS"), OWNER_LIMIT),
    **dict.fromkeys(("QUEUEKEEPER_APP_STORE", "QUEUEKEEPER_SCREEN_IOREG"), OWNER_PATH),
    **dict.fromkeys(("QUEUEKEEPER_VISIBILITY", "QUEUEKEEPER_PREPUSH", "QUEUEKEEPER_UNITY_OPEN", "QUEUEKEEPER_UNITY_QUIET"),
                    OWNER_SWITCH),
    cli.parallel.SHARD_VARIABLE: "qk unittest's mark on the test processes it starts, which this module reads above to "
                                 "know the run is accounted for; a suite a test starts gets the mark from its own qk unittest",
    "QUEUEKEEPER_LAUNCHER_PYTHON": "scripts/qk and bin/qk set it only to run themselves again under another Python, and "
                                   "take it out before they run anything, so no command they start inherits it",
}


def reduced(variables=(), **more):
    """An environment of only variables and more, for a process a test starts with less than its own environment,
    which still carries ISOLATION; variables and more win over it.

    A bare `env={...}` drops ISOLATION. On 2026-11-12 three tests started `qk unittest` with only PATH and a limit,
    and each stand-in suite asked this Mac's real admission for a slot and waited behind another project's gate
    (vocaro/queuekeeper#45). test_host_isolation fails a test module that starts qk with an environment that is
    built neither from os.environ nor by this, unless the command passes --unadmitted."""
    return {**ISOLATION, **dict(variables), **more}


def pythonpath():
    """The source composition explicitly supplied to this test process."""
    return os.environ.get("PYTHONPATH", str(Path(__file__).resolve().parents[1] / "src/python"))
