"""A lane's "unity": true, which says its command runs the project's Unity editor.

Such a lane's command runs under `qk unity ready --`, on whichever Mac runs it. That checks, before the
command starts, that this Mac has an editor, that no editor has the project open, that no stale lock is left and
that no orphaned licensing helper waits for the next editor. A lane that cannot start so ends unmeasured, with
nothing run, where its command would have failed or hung for a reason that says nothing of the commit.

An exclusive lane measures, and admission holds off only admitted work: an editor left in Play mode keeps
simulating on the GPU. So an exclusive lane is also checked for that (`ready --exclusive`), on every project's
editor, and ends unmeasured while one plays."""
import sys

from queuekeeper import extensions
from queuekeeper.config import TOOLKIT_ROOT

KEY = "unity"


class UnityLane(extensions.LaneKey):
    def check(self, name, lane):
        if type(lane[KEY]) is not bool: raise ValueError(f"{name}.{KEY} must be true or false")

    def command(self, lane, command):
        if not lane.get(KEY): return command
        exclusive = ["--exclusive"] if lane.get("resource") == "exclusive" else []
        return [sys.executable, str(TOOLKIT_ROOT / "bin/qk"), "unity", "ready", *exclusive, "--", *command]


def register():
    """Add the key to the core."""
    if not isinstance(extensions.LANE_KEYS.get(KEY), UnityLane): extensions.LANE_KEYS[KEY] = UnityLane()
