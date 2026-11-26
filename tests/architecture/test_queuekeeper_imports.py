"""Executable component boundaries for source, fixtures, and distribution ownership."""
import ast
import importlib.util
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest

import host_isolation

ROOT = Path(__file__).resolve().parents[2]
COMPONENTS = json.loads((ROOT / "queuekeeper-workspace.json").read_text())
PACKAGES = {"queuekeeper": ROOT / "src/python", **{name: ROOT / item["source"] for name, item in COMPONENTS.items()}}


def imports(source, module, package=False):
    """Resolve static and literal dynamic imports, including relative imports."""
    context = module if package else module.rpartition(".")[0]
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = importlib.util.resolve_name("." * node.level + (node.module or ""), context) if node.level else node.module
            found.append((node.lineno, base))
            found.extend((node.lineno, base + "." + alias.name) for alias in node.names)
        elif isinstance(node, ast.Call) and node.args:
            called = getattr(node.func, "attr", getattr(node.func, "id", None))
            if called in {"import_module", "__import__"} and isinstance(node.args[0], ast.Constant):
                found.append((node.lineno, node.args[0].value))
    return found


def violations(source, module, package=False):
    owner = module.split(".")[0]
    allowed = {owner, "queuekeeper", *COMPONENTS.get(owner, {}).get("requires", [])}
    found = []
    for line, name in imports(source, module, package):
        if not isinstance(name, str): continue
        target = name.split(".")[0]
        if any(name == "queuekeeper." + old or name.startswith("queuekeeper." + old + ".")
               for old in ("apple", "mac_desktop", "mac_hardware", "unity")):
            found.append(f"{module}:{line} uses obsolete plugin namespace {name}")
        if target in PACKAGES and target not in allowed or target == "vocaro_engineering":
            found.append(f"{module}:{line} imports {name}")
        if name.startswith("queuekeeper.visibility."):
            feature = ".".join(name.split(".")[:3])
            if not (module == feature or module.startswith(feature + ".")):
                found.append(f"{module}:{line} imports {name}")
    return found


class Packages(unittest.TestCase):
    def test_test_modules_have_a_component_or_integration_owner(self):
        self.assertEqual(sorted(path.name for path in (ROOT / "tests").glob("test_*.py")), [])

    def test_no_module_imports_what_its_component_does_not_require(self):
        found = []
        for owner, base in PACKAGES.items():
            for path in (base / owner).rglob("*.py"):
                parts = path.relative_to(base).with_suffix("").parts
                package = parts[-1] == "__init__"
                module = ".".join(parts[:-1] if package else parts)
                found.extend(violations(path.read_text(), module, package))
        self.assertEqual(found, [])

    def test_every_plugin_has_its_own_distribution_and_declares_its_dependencies(self):
        for name, item in COMPONENTS.items():
            folder = ROOT / item["source"]
            metadata = tomllib.loads((folder.parent / "pyproject.toml").read_text())
            self.assertEqual(metadata["project"]["name"], item.get("distribution", name.replace("_", "-")))
            deps = {re.match(r"[A-Za-z0-9_.-]+", dep).group() for dep in metadata["project"]["dependencies"]}
            self.assertEqual(deps, {"queuekeeper", *(dep.replace("_", "-") for dep in item["requires"])})
            packages = {p.name for p in folder.iterdir()
                        if p.is_dir() and p.name != "__pycache__" and not p.name.endswith(".egg-info")}
            self.assertEqual(packages, {name})
            self.assertTrue((ROOT / item["tests"] / "__init__.py").is_file())
        self.assertFalse(any((ROOT / "src/python/queuekeeper" / name / "__init__.py").exists()
                             for name in ("apple", "unity", "mac_desktop", "mac_hardware")))

    def test_core_runs_without_any_plugin_on_its_import_path(self):
        probe = '''import importlib.util, json
from unittest.mock import patch
from queuekeeper import cli, config, plugins, remote
assert config.PLUGINS == ()
assert plugins.HOST_INVENTORY_CHECKS == []
# Without the Apple plugin, a simulator's identifier keeps no admitted command on this Mac.
assert plugins.COMMAND_KEPT_HERE == []
assert not hasattr(remote, "simulators")
assert plugins.kept_here(["xcrun", "simctl", "boot", "5B0E6C51-2D3A-4F7B-9C1E-0A4B8D2F6E13"]) is None
# Without the Apple plugin, `qk remote setup --runtimes` is refused before it reaches another Mac.
assert plugins.REMOTE_SETUP_RUNTIMES == []
assert not any(hasattr(remote, name) for name in ("match_runtimes", "runtime_plan", "download_command"))
try:
    remote.setup("fixture", [], [], True, runtimes=True)
except ValueError as refusal:
    assert "no plugin loaded here provides it" in str(refusal), refusal
else:
    raise AssertionError("--runtimes was not refused")
with (
    patch.object(remote, "connection", return_value={}),
    patch.object(remote, "slow_connection", return_value=None),
    patch.object(remote, "developer_directory", return_value="/Xcode"),
    patch.object(remote, "reachable"),
    patch.object(remote, "survey", return_value={"xcode": "other"}),
    patch.object(remote, "leftover_census", return_value=({"leftovers": []}, None)),
    patch.object(remote, "compare_device_records"),
    patch.object(remote.offload_host, "toolchain", return_value={"xcode": "selected"}),
    patch.object(remote.offload_host, "python_crash_reports", return_value={}),
):
    report = remote.compare_hosts("fixture")
assert report["remote"]["xcode"] == "other"
assert report["errors"] == []
assert config.parse(json.dumps({"schemaVersion": 1, "name": "core", "plugins": [], "lanes": {}}))
for name in %r:
    assert importlib.util.find_spec(name) is None, name
assert cli.main(["--version"]) == 0
''' % list(COMPONENTS)
        with tempfile.TemporaryDirectory() as directory:
            done = subprocess.run([sys.executable, "-S", "-c", probe], cwd=directory, capture_output=True, text=True,
                                  env={**os.environ, "PYTHONPATH": str(ROOT / "src/python")},
                                  timeout=host_isolation.stretched(60))
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_remote_host_bootstrap_imports_only_the_standard_library(self):
        # The sender transmits this program before the receiver has a toolkit checkout or plugins.
        source = (PACKAGES["queuekeeper"] / "queuekeeper/offload_host.py").read_text()
        found = [(line, name) for line, name in imports(source, "queuekeeper.offload_host")
                 if name.split(".", 1)[0] not in sys.stdlib_module_names]
        self.assertEqual(found, [])

    def test_each_plugin_loads_with_only_its_declared_dependencies(self):
        for name, item in COMPONENTS.items():
            names = [name, *item["requires"]]
            paths = [ROOT / "src/python", *(ROOT / COMPONENTS[dependency]["source"] for dependency in names)]
            probe = ("from queuekeeper import plugins; plugins.load([" + repr(name) + "]); "
                     "assert set(plugins.LOADED) == " + repr(set(names)))
            with self.subTest(component=name), tempfile.TemporaryDirectory() as directory:
                done = subprocess.run([sys.executable, "-S", "-c", probe], cwd=directory, text=True, capture_output=True,
                                      env={**os.environ, "PYTHONPATH": os.pathsep.join(map(str, paths))},
                                      timeout=host_isolation.stretched(60))
                self.assertEqual(done.returncode, 0, done.stderr)

    def test_component_tests_import_only_declared_components(self):
        found = []
        for owner, item in COMPONENTS.items():
            # Visibility's integration suite exercises the composition; plugin suites are independent.
            if item["tests"] == "tests/visibility": continue
            allowed = {owner, "queuekeeper", *item["requires"]}
            for path in (ROOT / item["tests"]).rglob("test*.py"):
                module = ".".join(path.relative_to(ROOT / "tests").with_suffix("").parts)
                for line, target in imports(path.read_text(), module):
                    if target.split(".")[0] in PACKAGES and target.split(".")[0] not in allowed:
                        found.append(f"{path}:{line} imports {target}")
        self.assertEqual(found, [])

    def test_test_modules_do_not_import_each_other(self):
        found = []
        for path in (ROOT / "tests").rglob("test_*.py"):
            module = ".".join(path.relative_to(ROOT / "tests").with_suffix("").parts)
            for line, target in imports(path.read_text(), module):
                if any(part.startswith("test_") and part != "test_support" for part in target.split(".")):
                    found.append(f"{path}:{line} imports {target}")
        self.assertEqual(found, [])

    def test_native_resources_are_owned_and_included_by_their_distribution(self):
        native = ROOT / COMPONENTS["queuekeeper_apple"]["source"] / "queuekeeper_apple/_native"
        self.assertEqual({path.name for path in native.iterdir()}, {"duo-pose-helper.c", "sim-key-helper.m", "simulator-voices.swift"})
        self.assertFalse((ROOT / "src/objc/Simulator").exists())
        self.assertFalse((ROOT / "src/swift/Simulator").exists())
        metadata = tomllib.loads((native.parents[2] / "pyproject.toml").read_text())
        self.assertIn("_native/*", metadata["tool"]["setuptools"]["package-data"]["queuekeeper_apple"])


class Controls(unittest.TestCase):
    def test_core_cannot_import_plugins_and_siblings_cannot_import_each_other(self):
        for source, module in [("import queuekeeper_apple", "queuekeeper.cli"),
                               ("from queuekeeper_unity import editor", "queuekeeper_apple.lane"),
                               ("import importlib; importlib.import_module('queuekeeper_apple.devices')", "queuekeeper.remote"),
                               ("import queuekeeper_notify as notify", "queuekeeper.cli")]:
            with self.subTest(source=source): self.assertTrue(violations(source, module))

    def test_declared_dependencies_and_internal_relative_imports_are_allowed(self):
        for source, module in [("from queuekeeper import plugins", "queuekeeper_apple.lane"),
                               ("from queuekeeper_mac_desktop import reservations", "queuekeeper_apple.devices"),
                               ("from . import devices", "queuekeeper_apple.lane"),
                               ("from .. import pool", "queuekeeper_apple.simulators.child")]:
            with self.subTest(source=source): self.assertEqual(violations(source, module), [])
