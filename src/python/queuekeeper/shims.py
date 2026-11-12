"""The environment of a lane's command, with the build-tool wrappers first on its PATH.

Copied unchanged from the toolkit's stale_modules.py (vocaro/vocaro-engineering#403). The wrappers themselves,
`bin/shims/xcodebuild` and `bin/shims/swift`, and the rebuild they make when a module cache is stale, are not
here yet (PORTING.md): until they are, SHIMS names a folder that does not exist, and a lane's own tools run."""
import os
from pathlib import Path

SHIMS = Path(__file__).resolve().parents[3] / "bin/shims"
# The checkout a lane runs in: only a folder inside it is removed.
ROOT_VARIABLE = "ENG_STALE_MODULES_ROOT"


def environment(base, root):
    """base, a lane's environment, with SHIMS first on its PATH, once, and ROOT_VARIABLE naming root."""
    entries = [entry for entry in base.get("PATH", os.defpath).split(os.pathsep) if entry and entry != str(SHIMS)]
    return {**base, "PATH": os.pathsep.join([str(SHIMS), *entries]), ROOT_VARIABLE: str(Path(root).resolve())}
