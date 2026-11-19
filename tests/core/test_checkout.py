"""The import path a child process needs to run a Queuekeeper module (checkout.python_roots, `qk python-path`), which a
consumer once built by hand and lost a plugin from when the mobile plugin split out (vocaro/queuekeeper#252)."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src/python"))
import host_isolation  # noqa: F401 -- first, as every test module
from host_isolation import stretched
from queuekeeper import checkout, cli

ROOT = Path(__file__).resolve().parents[2]


def source(name):
    return ROOT / ("src/python" if name == "queuekeeper" else checkout.components(ROOT)[name]["source"])


class PythonRoots(unittest.TestCase):
    def test_a_module_gets_the_core_its_package_and_every_plugin_its_package_requires(self):
        # queuekeeper_apple requires queuekeeper_mobile since the split, which the consumer's path left out.
        self.assertEqual(checkout.python_roots("queuekeeper_apple.devices"),
                         [source(name) for name in ("queuekeeper", "queuekeeper_apple", "queuekeeper_mac_desktop",
                                                    "queuekeeper_mobile")])
        self.assertEqual(checkout.python_path("queuekeeper.processes"), str(source("queuekeeper")))

    def test_each_root_is_named_once_however_many_modules_need_it(self):
        roots = checkout.python_roots("queuekeeper_apple.devices", "queuekeeper_android", "queuekeeper.cli")
        self.assertEqual(len(roots), len(set(roots)))
        self.assertEqual(roots[0], source("queuekeeper"))
        self.assertIn(source("queuekeeper_android"), roots)

    def test_a_child_given_only_that_path_imports_each_component(self):
        for name in checkout.components(ROOT):
            with self.subTest(component=name), tempfile.TemporaryDirectory() as directory:
                done = subprocess.run([sys.executable, "-S", "-c", f"import {name}"], cwd=directory,
                                      capture_output=True, text=True, timeout=stretched(60),
                                      env={**os.environ, "PYTHONPATH": checkout.python_path(name)})
                self.assertEqual(done.returncode, 0, done.stderr)

    def test_requirements_are_followed_through_other_components_of_the_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for folder, name in [("src/python", "queuekeeper"), ("plugins/a/src", "kit_a"), ("plugins/b/src", "kit_b"),
                                 ("plugins/c/src", "kit_c")]:
                (root / folder / name).mkdir(parents=True)
            (root / checkout.MANIFEST).write_text(json.dumps({
                "kit_a": {"source": "plugins/a/src", "requires": ["kit_b"]},
                "kit_b": {"source": "plugins/b/src", "requires": ["kit_c"]},
                "kit_c": {"source": "plugins/c/src", "requires": []}}))
            self.assertEqual(checkout.python_roots("kit_a.deep.module", root=root),
                             [(root / folder).resolve() for folder in ("src/python", "plugins/a/src", "plugins/b/src",
                                                                       "plugins/c/src")])

    def test_a_package_outside_the_manifest_is_found_where_this_interpreter_imports_it(self):
        self.assertEqual(checkout.python_roots("json", root=ROOT)[1], Path(json.__file__).resolve().parent.parent)

    def test_a_package_found_nowhere_is_refused(self):
        with self.assertRaises(ModuleNotFoundError) as raised:
            checkout.python_roots("queuekeeper_nowhere.module")
        self.assertEqual(raised.exception.name, "queuekeeper_nowhere")


class Command(unittest.TestCase):
    def qk(self, *arguments):
        with contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()) as error:
            status = cli.main(["python-path", *arguments])
        return status, output.getvalue(), error.getvalue()

    def test_it_prints_the_path_for_a_caller_that_is_not_python(self):
        status, printed, said = self.qk("queuekeeper_apple.devices")
        self.assertEqual(status, 0, said)
        self.assertEqual(printed, checkout.python_path("queuekeeper_apple.devices") + "\n")

    def test_it_prints_the_path_and_its_folders_as_json(self):
        status, printed, said = self.qk("--json", "queuekeeper_mobile", "queuekeeper.processes")
        self.assertEqual(status, 0, said)
        roots = checkout.python_roots("queuekeeper_mobile")
        self.assertEqual(json.loads(printed), {"modules": ["queuekeeper_mobile", "queuekeeper.processes"],
                                               "pythonPath": os.pathsep.join(map(str, roots)),
                                               "roots": list(map(str, roots))})

    def test_it_refuses_a_module_it_cannot_find(self):
        status, printed, said = self.qk("queuekeeper_nowhere")
        self.assertEqual((status, printed), (cli.USAGE, ""))
        self.assertIn("No module named 'queuekeeper_nowhere'", said)


if __name__ == "__main__":
    unittest.main()
