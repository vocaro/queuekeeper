"""Private native sources owned and shipped by the Apple plugin."""
from pathlib import Path
from queuekeeper.config import TOOLKIT_ROOT

ROOT = TOOLKIT_ROOT
BUILD_ROOT = (ROOT / ".build" if (ROOT / "queuekeeper-workspace.json").is_file()
              else Path.home() / "Library/Caches/queuekeeper/apple")
NATIVE = Path(__file__).resolve().parent / "_native"


def source_path(name):
    if name not in {"duo-pose-helper.c", "simulator-voices.swift"}:
        raise KeyError(name)
    return NATIVE / name
