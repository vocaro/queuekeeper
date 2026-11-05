"""Keep every test out of the host's heavy-work admission (#88).

Every test module imports this first, and test_host_isolation fails one that does not. Importing it
points HEAVY_SLOT_DIR and SW_GATE_LOCK_FILE at a directory private to this process, whatever they
named before, so that a test which admits work without redirecting them, by `cli.main([... "run"
...])`, `eng admit` or a subprocess that inherits the environment, admits it there. An entry a
test leaves has no process record, so recovery leaves it, and in the host's guard it blocked
every session's heavy work until it was removed by hand (2026-10-31). The guard's default path is
redirected too, for a test that clears the environment. A test that needs another namespace still
sets its own; the process removes this one when it exits.

`eng doctor` also fixes both Macs itself: it evens out their runtimes, deleting or copying them, and
shuts down stale pool simulators (cli.fix_hosts). Most doctor tests compare this Mac with the real
second Mac, so cli.fix_hosts is replaced here by one that changes nothing; a test of the fixes calls
FIX_HOSTS, the real one, against fakes.

VOCARO_EVIDENCE_RETRY is set to off, so no eng a test runs offloads this Mac's evidence archives.
"""
import atexit
import os
from pathlib import Path
import shutil
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/python"))
from vocaro_engineering import cli, crash_alerts, device_notifications, offload_host, resources


def stretched(seconds):
    """These seconds stretched by the host's one-minute load per core, as a bound on a stall is (#128).

    A test's wall-clock bound that only catches a hang, such as a subprocess's `timeout=`, an `--limit`, a
    wait for an event or a ceiling on elapsed time, takes this, and so does the life of a stand-in process
    that must outlive its test: at loads of 145 to 590 on 18 cores, work that takes seconds on an idle Mac
    outlived fixed bounds of 20 to 120 s (#81, #90, #111, #119, #120, #121). A bound that is the behavior
    under test stays fixed, with a comment, and stretches only its control (db166cf). These stay fixed for a
    reason stated here rather than at every site: an `Admission(timeout=0)`, which asks once and does not
    wait; a lower bound on elapsed time, which load only lengthens; a hang's stand-in that sleeps 60 s or
    more, which a fixed bound under test stops at a second or so, or which a quick in-process test kills; a
    take or slot refused before it waits; and a timeout a fake receives or a test reads back, which no clock
    enforces. The stretch is finite, so a test that really hangs still fails at it (test_host_isolation)."""
    return seconds * max(1, os.getloadavg()[0] / (os.cpu_count() or 1))


ROOT = Path(tempfile.mkdtemp(prefix="vocaro-test-host-")).resolve()
# Only by this process: a child forked from it exits through atexit too.
atexit.register(lambda owner=os.getpid(): os.getpid() == owner and shutil.rmtree(ROOT, True))
GATE = str(ROOT / "gate")
os.environ.update(HEAVY_SLOT_DIR=str(ROOT / "slots"), SW_GATE_LOCK_FILE=GATE)
resources.GATE_LOCK = offload_host.GATE_LOCK = GATE
# Nor does any reach the host's crash-report setting or its screen's alerts (#118): a foreground run a test
# makes reads DialogType unset and no alert, and changes nothing. A test of either replaces these again.
os.environ["VOCARO_CRASH_DIALOG_RECORD"] = str(ROOT / "crash-dialog.json")
crash_alerts.run = lambda command, environment=None: "" if command[0] == "osascript" else None
crash_alerts.change = lambda value: True
# Nor does any reach a paired device's Focus (#144): a reservation a test makes that would run the owner's
# notification shortcuts finds devicectl unable to open Shortcuts. A test of them replaces this again.
device_notifications.run = lambda command: (None, "tests never reach a device")
# Nor does any run a test makes, in this process or a child, relay a device's log: its watch starts no relay and
# says so. A test of the watch sets its own relay.
os.environ[device_notifications.WATCH] = "off"


FIX_HOSTS = cli.fix_hosts

# Nor does any eng a test runs, in this process or a child, start the evidence offload's background retry,
# which would offer this Mac's real evidence archives to the real second Mac (vocaro-engineering#157). A test
# of retry_soon clears it.
os.environ["VOCARO_EVIDENCE_RETRY"] = "off"


def unfixed(root, hosts):
    hosts.pop("runtimesDiffer", None)
    return {**hosts, "reaped": [], "deletedDefaults": []}


cli.fix_hosts = unfixed
