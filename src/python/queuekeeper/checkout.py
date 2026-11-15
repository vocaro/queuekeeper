"""Compose this repository's independently packaged components for checkout launchers.

Installed packages do not call this module: their dependencies come from distribution metadata.
"""
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
MANIFEST = "queuekeeper-workspace.json"


def components(root=ROOT):
    path = Path(root) / MANIFEST
    return json.loads(path.read_text()) if path.is_file() else {}


def source_paths(root=ROOT):
    root = Path(root)
    return [root / "src/python", *(root / item["source"] for item in components(root).values())]


def activate(root=ROOT):
    """Expose the checkout composition explicitly, including to Python child processes."""
    paths = [str(path) for path in source_paths(root) if path.is_dir()]
    sys.path[:0] = [path for path in paths if path not in sys.path]
    inherited = os.environ.get("PYTHONPATH", "").split(os.pathsep)
    os.environ["PYTHONPATH"] = os.pathsep.join(dict.fromkeys([*paths, *(path for path in inherited if path)]))


def import_paths(*modules):
    """Import roots of explicitly named top-level packages, for Python subprocesses."""
    import importlib.util
    paths = []
    for module in modules:
        spec = importlib.util.find_spec(module)
        if spec is None or spec.origin is None:
            raise ModuleNotFoundError(module)
        path = Path(spec.origin).resolve().parent.parent
        if path not in paths: paths.append(path)
    return paths
