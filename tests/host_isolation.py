"""Keep every test out of the host's heavy-work admission (#88).

Every test module imports this first, and test_host_isolation fails one that does not. Importing it
points HEAVY_SLOT_DIR and SW_GATE_LOCK_FILE at a directory private to this process, whatever they
named before, so that a test which admits work without redirecting them, by `cli.main([... "run"
...])`, `eng admit` or a subprocess that inherits the environment, admits it there. An entry a
test leaves has no process record, so recovery leaves it, and in the host's guard it blocked
every session's heavy work until it was removed by hand (2026-10-31). The guard's default path is
redirected too, for a test that clears the environment. A test that needs another namespace still
sets its own; the process removes this one when it exits. The admission token the suite inherits is
removed as well: the toolkit's gate is an admitted lane (the owner's decision, 2026-11-07,
vocaro-engineering#200), so its tests run inside the gate's admission, and a test that admits work would
otherwise reuse the host's admission rather than take one in its own namespace, and an exclusive one
would be refused inside it.

Every lane is admitted and offered to the second Mac first, so VOCARO_OFFLOAD_HOST is set to none: no lane a
test runs, in this process or a child, reaches the real second Mac through the ssh alias. A test of the
second Mac names its fake one, or clears the variable to read the alias, itself.

`eng doctor` also fixes both Macs itself: it evens out their runtimes, deleting or copying them, and
shuts down stale pool simulators (cli.fix_hosts). Most doctor tests compare this Mac with the real
second Mac, so cli.fix_hosts is replaced here by one that changes nothing; a test of the fixes calls
FIX_HOSTS, the real one, against fakes. A heavy run here reaps this Mac first (cli.reap_here); VOCARO_REAP_BEFORE_RUN
is set to off, so no run a test makes does.

VOCARO_EVIDENCE_RETRY is set to off, so no eng a test runs offloads this Mac's evidence archives.
"""
import atexit
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/python"))
from vocaro_engineering import cli, crash_alerts, device_notifications, notification_center, offload_host, remote, resources
from vocaro_engineering.waiting import stretched  # noqa: F401 -- the tests' bound on a stall, re-exported


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


ROOT = Path(tempfile.mkdtemp(prefix="vocaro-test-host-")).resolve()
# Only by this process: a child forked from it exits through atexit too.
atexit.register(lambda owner=os.getpid(): os.getpid() == owner and shutil.rmtree(ROOT, True))
GATE = str(ROOT / "gate")
os.environ.update(HEAVY_SLOT_DIR=str(ROOT / "slots"), SW_GATE_LOCK_FILE=GATE)
os.environ.pop(resources.TOKEN, None)
os.environ["VOCARO_OFFLOAD_HOST"] = "none"
# A suite the second Mac runs for a dispatch, as the toolkit's gate is, inherits the dispatch's variables, which make every
# receipt a test writes say it ran there; a test of a dispatch sets them itself.
for variable in (remote.DISPATCHER, "VOCARO_DEVICE_LEASE"): os.environ.pop(variable, None)
# The gate is a lane, so its suite inherits the name of the gate's own wait record: a lane a test runs would report its
# waits there, into what `eng queue status` says of the gate, and never into its project's record (vocaro-engineering#335).
os.environ.pop(remote.lane_wait.VARIABLE, None)
resources.GATE_LOCK = offload_host.GATE_LOCK = GATE
# Nor does any reach the host's crash-report setting or its screen's alerts (#118): a foreground run a test
# makes reads DialogType unset and no alert, and changes nothing. A test of either replaces these again.
os.environ["VOCARO_CRASH_DIALOG_RECORD"] = str(ROOT / "crash-dialog.json")
crash_alerts.run = lambda command, environment=None: "" if command[0] == "osascript" else None
crash_alerts.change = lambda value: True
# Nor does any reach the screen's Notification Center windows (#204): a foreground run a test makes on a second Mac
# finds none, and closes nothing. A test of them replaces this again.
notification_center.run = lambda command, environment=None: "[]" if command[:3] == ["osascript", "-l", "JavaScript"] \
    else None
# Nor does any reach a paired device's Focus (#144): a reservation a test makes that would run the owner's
# notification shortcuts finds devicectl unable to open Shortcuts. A test of them replaces this again.
device_notifications.run = lambda command: (None, "tests never reach a device")
# Nor does any run a test makes, in this process or a child, relay a device's log: its watch starts no relay and
# says so. A test of the watch sets its own relay.
os.environ[device_notifications.WATCH] = "off"
# Nor does any host identity or lane receipt a test makes read this Mac's scroll-bar setting and HID devices, a second
# of ioreg each time (#229): it reads the setting unset and no pointing device. A test of the reading sets its own.
(ROOT / "scroll-bars.json").write_text(json.dumps({
    "defaults": [1, "", "Error: Could not find key 'AppleShowScrollBars' in domain 'kCFPreferencesAnyApplication'.\n"],
    "ioreg": [0, "", ""]}))
os.environ[offload_host.SCROLL_BARS_RECORDING] = str(ROOT / "scroll-bars.json")


FIX_HOSTS = cli.fix_hosts

# Nor does any reach check a test makes in this process ask macOS which process is responsible for it
# (remote.responsible_for): run by a queue worker whose session ended, every test of an unreachable second Mac would
# read that its own process has lost Local Network access (vocaro-engineering#282). A test of it calls RESPONSIBLE_FOR.
RESPONSIBLE_FOR = remote.responsible_for
remote.responsible_for = lambda pid: None

# Nor does any eng a test runs, in this process or a child, start the evidence offload's background retry,
# which would offer this Mac's real evidence archives to the real second Mac (vocaro-engineering#157). A test
# of retry_soon clears it.
os.environ["VOCARO_EVIDENCE_RETRY"] = "off"

# Nor does any heavy run a test makes here, in this process or a child, reap this Mac first (cli.reap_here), which
# would shut down its stale pool simulators and release its dead pool reservations (vocaro-engineering#156). A test
# of the reap clears it.
os.environ[cli.REAP_SWITCH] = "off"

# Nor does any doctor a test runs list this host's own leftover worktrees of the toolkit
# (toolkit_copy.leftover_worktrees), which come and go with other sessions. A test of the list clears it.
os.environ[cli.toolkit_copy.LEFTOVER_SWITCH] = "off"
# Nor does any name the gap between the toolkit copy running the tests and origin/main (toolkit_copy.gap_line), which
# a session worktree opens as others land, so that its doctor tests fail only at their own warnings (#224). A test of
# the gap clears it.
os.environ[cli.toolkit_copy.GAP_SWITCH] = "off"


def unfixed(root, hosts):
    hosts.pop("runtimesDiffer", None)
    return {**hosts, "reaped": [], "deletedDefaults": []}


cli.fix_hosts = unfixed
