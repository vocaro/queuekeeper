"""Every module name a project may import stays importable: a module that moves leaves a forwarder at its old name
(vocaro/queuekeeper#103; the developer guide's "Moving a module")."""
from datetime import date
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import host_isolation

ROOT = Path(__file__).resolve().parents[2]
COMPONENTS = json.loads((ROOT / "queuekeeper-workspace.json").read_text())
PACKAGES = {"queuekeeper": ROOT / "src/python", **{name: ROOT / item["source"] for name, item in COMPONENTS.items()}}
NAMES = ROOT / "tests/architecture/module_names.json"
GUIDE = "doc/developer_guide.md#moving-a-module"


def modules():
    """Each module of the core and the plugins by its import name, a package by its own."""
    found = set()
    for owner, base in PACKAGES.items():
        for path in (base / owner).rglob("*.py"):
            parts = path.relative_to(base).with_suffix("").parts
            if "__pycache__" in parts: continue
            found.add(".".join(parts[:-1] if parts[-1] == "__init__" else parts))
    return found


def differences(present, listed, forwarded):
    """Each listed name the tree no longer has, as a line that says what to do. A module not yet listed is no
    failure: entries gated together often each add one, and a check that every module is listed would fail each
    batch that holds two of them, blaming entries that broke nothing."""
    found = [f"{name} is gone with no forwarder: leave one at its old name and list it under forwarded ({GUIDE})"
             for name in sorted((set(listed) | set(forwarded)) - present)]
    found.extend(f"{name} is listed both as a module and as forwarded" for name in sorted(set(listed) & set(forwarded)))
    return found


class ModuleNames(unittest.TestCase):
    def setUp(self):
        self.names = json.loads(NAMES.read_text())

    def test_every_listed_module_name_is_still_there(self):
        self.assertEqual(differences(modules(), self.names["modules"], self.names["forwarded"]), [])

    def test_the_list_is_sorted(self):
        self.assertEqual(self.names["modules"], sorted(self.names["modules"]))

    def test_each_forwarder_names_its_module_and_its_dates(self):
        for old, entry in self.names["forwarded"].items():
            with self.subTest(old=old):
                self.assertIn(entry["to"], self.names["modules"])
                self.assertLess(date.fromisoformat(entry["moved"]), date.fromisoformat(entry["until"]))
                source = (PACKAGES[old.split(".")[0]] / Path(*old.split("."))).with_suffix(".py").read_text()
                self.assertIn(f'forwarding.forward(__name__, "{entry["to"]}", moved="{entry["moved"]}", '
                              f'until="{entry["until"]}")', source)

    def test_each_forwarder_is_its_new_module_and_warns(self):
        forwarded = self.names["forwarded"]
        probe = '''import importlib, json, sys, warnings
from unittest import mock
for old, entry in json.loads(sys.argv[1]).items():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        module = __import__(old, fromlist=["_"])  # As an import statement does, so the warning names this file.
    assert module is importlib.import_module(entry["to"]), old
    parent, _, leaf = old.rpartition(".")
    assert getattr(importlib.import_module(parent), leaf) is module, old
    assert any(w.category is DeprecationWarning and entry["to"] in str(w.message) and w.filename == __file__
               for w in caught), (old, [str(w.message) for w in caught])
    name = next(n for n in vars(module) if not n.startswith("_"))
    with mock.patch(old + "." + name, "patched"):
        assert getattr(module, name) == "patched", old
'''
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "probe.py"
            script.write_text(probe)
            done = subprocess.run([sys.executable, "-S", str(script), json.dumps(forwarded)], cwd=directory,
                                  capture_output=True, text=True, timeout=host_isolation.stretched(60),
                                  env={**os.environ, "PYTHONPATH": os.pathsep.join(map(str, set(PACKAGES.values())))})
        self.assertEqual(done.returncode, 0, done.stderr)


class Controls(unittest.TestCase):
    def test_a_module_gone_without_a_forwarder_is_reported_and_one_not_yet_listed_is_not(self):
        found = differences({"queuekeeper.cli", "queuekeeper.new"}, ["queuekeeper.cli", "queuekeeper.old"], {})
        self.assertEqual(len(found), 1)
        self.assertIn("queuekeeper.old is gone with no forwarder", found[0])

    def test_a_forwarder_that_is_there_is_not_reported(self):
        self.assertEqual(differences({"queuekeeper.new", "queuekeeper.old"}, ["queuekeeper.new"],
                                     {"queuekeeper.old": {"to": "queuekeeper.new"}}), [])


if __name__ == "__main__":
    unittest.main()
