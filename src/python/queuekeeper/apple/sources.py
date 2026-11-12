"""Where the extension's native sources are in a checkout of Queuekeeper."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]


def source_path(name):
    paths = {
        "duo-pose-helper.c": "src/objc/Simulator/duo-pose-helper.c",
        "simulator-voices.swift": "src/swift/Simulator/simulator-voices.swift",
    }
    return ROOT / paths[name]
