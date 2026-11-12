"""The environment of a lane's command, with the build-tool wrappers first on its PATH.

The wrappers are `bin/shims/xcodebuild` and `bin/shims/swift`, which run a build once more when it failed on a
stale module cache (queuekeeper.apple.stale_modules). A tool with no wrapper there runs as it is."""
import os
from pathlib import Path

SHIMS = Path(__file__).resolve().parents[3] / "bin/shims"
# The checkout a lane runs in: only a folder inside it is removed.
ROOT_VARIABLE = "QUEUEKEEPER_STALE_MODULES_ROOT"


def environment(base, root):
    """base, a lane's environment, with SHIMS first on its PATH, once, and ROOT_VARIABLE naming root."""
    entries = [entry for entry in base.get("PATH", os.defpath).split(os.pathsep) if entry and entry != str(SHIMS)]
    return {**base, "PATH": os.pathsep.join([str(SHIMS), *entries]), ROOT_VARIABLE: str(Path(root).resolve())}
