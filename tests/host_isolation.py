"""Keep every test out of the host's heavy-work admission (#88).

Every test module imports this first, and test_host_isolation fails one that does not. Importing it
points HEAVY_SLOT_DIR and SW_GATE_LOCK_FILE at a directory private to this process, whatever they
named before, so that a test which admits work without redirecting them, by `cli.main([... "run"
...])`, `eng admit` or a subprocess that inherits the environment, admits it there. An entry a
test leaves has no process record, so recovery leaves it, and in the host's guard it blocked
every session's heavy work until it was removed by hand (2026-10-31). The guard's default path is
redirected too, for a test that clears the environment. A test that needs another namespace still
sets its own; the process removes this one when it exits.
"""
import atexit
import os
from pathlib import Path
import shutil
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/python"))
from vocaro_engineering import offload_host, resources

ROOT = Path(tempfile.mkdtemp(prefix="vocaro-test-host-")).resolve()
# Only by this process: a child forked from it exits through atexit too.
atexit.register(lambda owner=os.getpid(): os.getpid() == owner and shutil.rmtree(ROOT, True))
GATE = str(ROOT / "gate")
os.environ.update(HEAVY_SLOT_DIR=str(ROOT / "slots"), SW_GATE_LOCK_FILE=GATE)
resources.GATE_LOCK = offload_host.GATE_LOCK = GATE
