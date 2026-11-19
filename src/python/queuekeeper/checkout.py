"""Compose this repository's independently packaged components for checkout launchers, and say which import roots
a child process needs to run one of their modules (python_path).

An installed Queuekeeper has no workspace manifest: its components' dependencies come from distribution metadata,
and python_path finds each named package where this interpreter imports it from.
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


def python_roots(*modules, root=ROOT):
    """The import roots a Python child process needs to run each of modules, such as "queuekeeper_apple.devices" for
    `python3 -m queuekeeper_apple.devices`: the folder of the core's package first, then the folder of each module's
    top-level package, and of every component it requires by the workspace manifest, directly or through another.

    A consumer that built the path from the packages it imported itself left out one a module needs once it moved:
    after the mobile plugin split, queuekeeper_apple.devices imports queuekeeper_mobile, and every such child failed
    with ModuleNotFoundError (vocaro/queuekeeper#252). A layout change stays inside Queuekeeper this way.

    A component of root's manifest is found in root; any other package, as every one is in an installed Queuekeeper,
    where this interpreter imports it from. Raises ModuleNotFoundError for a top-level package found in neither."""
    manifest = components(root)
    packages, pending = [], ["queuekeeper", *(module.split(".")[0] for module in modules)]
    while pending:
        name = pending.pop(0)
        if name in packages: continue
        packages.append(name)
        pending.extend(manifest.get(name, {}).get("requires", []))
    roots = []
    for name in packages:
        found = package_root(name, root, manifest)
        if found not in roots: roots.append(found)
    return roots


def python_path(*modules, root=ROOT):
    """python_roots(*modules) as a PYTHONPATH value."""
    return os.pathsep.join(map(str, python_roots(*modules, root=root)))


def package_root(name, root=ROOT, manifest=None):
    """The folder holding the top-level package name: in root's checkout when it is the core or a component of the
    manifest there, else where this interpreter imports it from."""
    import importlib.util
    manifest = components(root) if manifest is None else manifest
    source = Path(root) / ("src/python" if name == "queuekeeper" else manifest.get(name, {}).get("source", ""))
    if (name == "queuekeeper" or name in manifest) and (source / name).is_dir(): return source.resolve()
    try: spec = importlib.util.find_spec(name)
    except (ImportError, ValueError): spec = None
    if spec is not None and spec.origin is not None and spec.origin not in {"built-in", "frozen"}:
        return Path(spec.origin).resolve().parent.parent
    if spec is not None and spec.submodule_search_locations:
        return Path(next(iter(spec.submodule_search_locations))).resolve().parent
    raise ModuleNotFoundError(f"No module named {name!r}", name=name)


def import_paths(*modules):
    """Import roots of explicitly named top-level packages, for Python subprocesses. python_roots also finds those
    they require, and is what a caller should use."""
    import importlib.util
    paths = []
    for module in modules:
        spec = importlib.util.find_spec(module)
        if spec is None or spec.origin is None:
            raise ModuleNotFoundError(module)
        path = Path(spec.origin).resolve().parent.parent
        if path not in paths: paths.append(path)
    return paths
