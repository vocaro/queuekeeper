"""Queuekeeper's packages import only what they may (vocaro/vocaro-engineering#403).

The core, `queuekeeper` outside its extensions, imports neither the toolkit (`vocaro_engineering`)
nor an extension. The Mac desktop extension, `queuekeeper.mac_desktop`, may import the core. The Apple
extension, `queuekeeper.apple`, and the Unity one, `queuekeeper.unity`, which require it, may import both.
None imports the toolkit, the Mac desktop extension imports neither of the two that require it, and
those two never import each other.

A visibility feature, a module or package of its own in `queuekeeper.visibility`, may import the core and never
another feature, and nothing outside a feature imports it: the switch loads it by its name (queuekeeper.visibility).

The check also reads an import written for the toolkit's layout, such as `vocaro_engineering.remote` or a
relative import of a module that is on the other side here, as the import it stands for.

`python3 tests/test_queuekeeper_imports.py --report` prints what it finds, by kind."""
import ast
from pathlib import Path
import sys
import unittest

import host_isolation  # noqa: F401 -- first, as every test module

SOURCE = Path(__file__).resolve().parents[1] / "src/python"
CORE = "queuekeeper"
APPLE = "queuekeeper.apple"
DESKTOP = "queuekeeper.mac_desktop"
UNITY = "queuekeeper.unity"
HARDWARE = "queuekeeper.mac_hardware"
EXTENSIONS = (APPLE, DESKTOP, UNITY, HARDWARE)
# What an extension may import of the others: the ones it requires.
REQUIRED = {APPLE: (DESKTOP,), UNITY: (DESKTOP,)}
# How a finding names a package: the core's is None.
LABEL = {None: "core", APPLE: "Apple", DESKTOP: "Mac desktop", UNITY: "Unity", HARDWARE: "Mac hardware"}
TOOLKIT = "vocaro_engineering"
VISIBILITY = "queuekeeper.visibility"
# A call that imports the module its first argument names.
IMPORTERS = {"import_module", "__import__"}

CORE_TOOLKIT = "core imports toolkit"
CORE_APPLE = "core imports Apple"
APPLE_TOOLKIT = "Apple imports toolkit"
CORE_DESKTOP = "core imports Mac desktop"
DESKTOP_APPLE = "Mac desktop imports Apple"
DESKTOP_TOOLKIT = "Mac desktop imports toolkit"
# An import the rules allow, written for the toolkit's layout: `vocaro_engineering.processes` for
# `queuekeeper.processes`, or the extension's `from . import remote` for the core's `remote`. It
# names no module here, so its module does not import until the port rewrites it.
OLD_NAME = "old name"
KINDS = tuple(dict.fromkeys(
    [CORE_TOOLKIT, CORE_APPLE, APPLE_TOOLKIT, CORE_DESKTOP, DESKTOP_APPLE, DESKTOP_TOOLKIT]
    + [f"{LABEL[owner]} imports toolkit" for owner in LABEL]
    + [f"{LABEL[owner]} imports {LABEL[other]}" for owner in LABEL for other in EXTENSIONS
       if other != owner and other not in REQUIRED.get(owner, ())] + [OLD_NAME]))


def within(module, package):
    return module == package or module.startswith(package + ".")


def extension(module):
    """The extension module is part of, or None for one of the core's."""
    return next((package for package in EXTENSIONS if within(module, package)), None)


def inner(name):
    """name below its extension's package, or below the core's: its place in the toolkit's one package."""
    return name[len(extension(name) or CORE) + 1:]


def statements(source):
    """Each import in source as (line, level, base, names).

    `level` counts a relative import's dots, `base` is the module named, or None for `from . import
    y`, and `names` are what a `from` import takes, each of which may be a submodule. An import by
    a call counts when its name is a literal."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found += [(node.lineno, 0, alias.name, ()) for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = tuple(alias.name for alias in node.names if alias.name != "*")
            found.append((node.lineno, node.level, node.module, names))
        elif isinstance(node, ast.Call) and node.args:
            called = node.func
            name = called.attr if isinstance(called, ast.Attribute) else getattr(called, "id", None)
            first = node.args[0]
            if name in IMPORTERS and isinstance(first, ast.Constant) and isinstance(first.value, str):
                found.append((node.lineno, 0, first.value, ()))
    return found


def absolute(level, base, module, package):
    """The absolute name of an import's base, when module's source holds it.

    `package` says the source is a package's `__init__.py`, whose relative imports start at module
    itself."""
    if not level: return base
    parts = module.split(".")
    kept = len(parts) - level + (1 if package else 0)
    return ".".join(parts[:kept] + ([base] if base else []))


def imported(source, module, package=False):
    """The absolute names source imports, as (line, name) pairs, when it is module's source.

    A `from X import y` counts X and X.y, since y may be a submodule."""
    found = []
    for line, level, base, names in statements(source):
        base = absolute(level, base, module, package)
        found.append((line, base))
        found += [(line, f"{base}.{name}") for name in names]
    return found


def feature(module):
    """The visibility feature module is, or is in, by its name: None for the switch itself and for every other."""
    if not within(module, VISIBILITY) or module == VISIBILITY: return None
    return ".".join(module.split(".")[:VISIBILITY.count(".") + 2])


def forbidden(module):
    """The packages module may not import, a visibility feature other than its own apart (violations)."""
    if not within(module, CORE): return []
    owner = extension(module)
    return [TOOLKIT] + [other for other in EXTENSIONS if other != owner and other not in REQUIRED.get(owner, ())]


def violations(source, module, package=False):
    """One line for each import in module's source of a package it may not import, by its name
    alone. This is the whole rule once every import is written for Queuekeeper's layout."""
    return [f"{module}:{line} imports {name}"
            for line, name in imported(source, module, package)
            if any(within(name, barred) for barred in forbidden(module))
            or (name and feature(name) and feature(name) != feature(module))]


def modules(root):
    """Each Python file below root's `queuekeeper`, as (path, module name, whether it is a package)."""
    for path in sorted((root / "queuekeeper").rglob("*.py")):
        parts = list(path.relative_to(root).with_suffix("").parts)
        package = parts[-1] == "__init__"
        yield path, ".".join(parts[:-1] if package else parts), package


def bound(source):
    """The names a module's source binds at its top level, other than by importing."""
    names = set()
    for node in ast.parse(source).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names |= {name.id for target in targets for name in ast.walk(target) if isinstance(name, ast.Name)}
    return names


class Layout:
    """The modules of one source tree, under their names here and their names in the toolkit.

    `sources` maps each module's name to (source, whether it is a package). In the toolkit every
    module sat in one package, `vocaro_engineering`: `queuekeeper.remote` was
    `vocaro_engineering.remote` and `queuekeeper.apple.simulators.pool` was
    `vocaro_engineering.simulators.pool`."""

    def __init__(self, sources):
        self.sources = sources
        self.new = {name: name for name in sources}
        self.old = {}
        for name in sources:
            if name in EXTENSIONS: continue
            self.old[TOOLKIT + ("." + inner(name) if inner(name) else "")] = name

    @classmethod
    def read(cls, root):
        return cls({name: (path.read_text(), package) for path, name, package in modules(root)})

    @staticmethod
    def in_toolkit(name):
        """The toolkit's name for what `name` would be here: every module there sat in one package."""
        if within(name, TOOLKIT): return name
        return TOOLKIT + ("." + inner(name) if inner(name) else "")

    def named(self, name, known):
        """What `name` means when `known` maps names to modules: (the module, None), when it names a
        module or something its source binds; (None, a missing module's name), when it names a
        module that is not there; or (None, None) when no part of it is known."""
        parts = name.split(".")
        for end in range(len(parts), 0, -1):
            prefix = ".".join(parts[:end])
            if prefix not in known: continue
            module = known[prefix]
            source, package = self.sources[module]
            rest = parts[end:]
            dunder = bool(rest) and rest[0].startswith("__") and rest[0].endswith("__")  # `__name__` and the like.
            if not rest or not package or dunder or rest[0] in bound(source): return module, None
            return None, f"{prefix}.{rest[0]}"
        return None, None

    def kinds(self, module):
        """Each rule module's source breaks, as (kind, what it imports), and each allowed import it
        writes under a toolkit name."""
        source, package = self.sources[module]
        owner = extension(module)
        found = set()
        for _line, level, base, names in statements(source):
            here = absolute(level, base, module, package)
            for suffix in [f".{name}" for name in names] or [""]:
                target, missing = self.named(here + suffix, self.new)
                stale = False
                if target is None and (missing or within(here, TOOLKIT)):
                    # Not a module here: it is one the toolkit kept, or one here under its toolkit name.
                    stale = True
                    target, missing = self.named(self.in_toolkit(here + suffix), self.old)
                if target is None and missing is None: continue  # The library, or another project.
                if target is None:
                    found.add((f"{LABEL[owner]} imports toolkit", missing))
                elif extension(target) not in (None, owner, *REQUIRED.get(owner, ())):
                    found.add((f"{LABEL[owner]} imports {LABEL[extension(target)]}", target))
                elif stale:
                    found.add((OLD_NAME, target))
        return found

    def lines(self):
        """One line for each finding: `KIND: MODULE -> WHAT`, sorted."""
        return sorted(f"{kind}: {module} -> {what}" for module in self.sources for kind, what in self.kinds(module))


def counts(lines):
    return {kind: sum(line.startswith(kind + ": ") for line in lines) for kind in KINDS}


def summary(lines):
    return ", ".join(f"{count} {kind}" for kind, count in counts(lines).items())


class Packages(unittest.TestCase):
    def test_the_core_and_both_extensions_are_there(self):
        names = {name for _, name, _ in modules(SOURCE)}
        self.assertIn(CORE, names)
        self.assertIn(APPLE, names)
        self.assertIn(DESKTOP, names)
        self.assertIn(UNITY, names)
        self.assertIn(HARDWARE, names)

    def test_no_module_imports_what_it_may_not(self):
        self.assertEqual(Layout.read(SOURCE).lines(), [])
        found = [line for path, name, package in modules(SOURCE)
                 for line in violations(path.read_text(), name, package)]
        self.assertEqual(found, [])


class Controls(unittest.TestCase):
    """The check finds each kind of import, so an empty result means there is none."""

    def test_the_core_may_not_import_the_toolkit(self):
        for source in ("import vocaro_engineering", "import vocaro_engineering.remote as remote",
                       "from vocaro_engineering import remote", "from vocaro_engineering.remote import dispatch",
                       "def late():\n    from vocaro_engineering import disk",
                       "import importlib\nimportlib.import_module('vocaro_engineering.disk')",
                       "__import__('vocaro_engineering')"):
            with self.subTest(source=source):
                self.assertTrue(violations(source, "queuekeeper.merge_queue"))

    def test_the_core_may_not_import_the_extension(self):
        for source, module, package in (
                ("import queuekeeper.apple", "queuekeeper.remote", False),
                ("from queuekeeper import apple", "queuekeeper.remote", False),
                ("from queuekeeper.apple import devices", "queuekeeper.remote", False),
                ("from . import apple", "queuekeeper.remote", False),
                ("from .apple import devices", "queuekeeper.remote", False),
                ("from .apple.simulators import pool", "queuekeeper", True),
                ("from ..apple import devices", "queuekeeper.hosts.agent", False),
                ("from .. import apple", "queuekeeper.hosts", True)):
            with self.subTest(source=source, module=module):
                self.assertTrue(violations(source, module, package))

    def test_the_extension_may_import_the_core_and_not_the_toolkit(self):
        for source in ("from .. import remote", "from ..resources import BUSY", "import queuekeeper.remote",
                       "from . import devices", "from .simulators import pool"):
            with self.subTest(source=source):
                self.assertEqual(violations(source, "queuekeeper.apple.devices"), [])
        self.assertEqual(violations("from ... import remote", "queuekeeper.apple.simulators.pool"), [])
        self.assertTrue(violations("from vocaro_engineering import evidence", "queuekeeper.apple.devices"))
        self.assertTrue(violations("from vocaro_engineering.voiceover import state", "queuekeeper.apple"))

    def test_the_desktop_extension_may_import_the_core_and_neither_apple_nor_the_toolkit(self):
        for source in ("from .. import remote", "from ..resources import process_start", "from . import reservations"):
            with self.subTest(source=source):
                self.assertEqual(violations(source, "queuekeeper.mac_desktop.foreground"), [])
        for source in ("from ..apple import devices", "from queuekeeper.apple import devices", "import queuekeeper.apple",
                       "from vocaro_engineering import evidence"):
            with self.subTest(source=source):
                self.assertTrue(violations(source, "queuekeeper.mac_desktop.foreground"))
        self.assertTrue(violations("from ..apple.voiceover import state", "queuekeeper.mac_desktop.foreground"))

    def test_apple_may_import_the_desktop_extension_and_the_core_may_not(self):
        for source in ("from ..mac_desktop import reservations", "from queuekeeper.mac_desktop.reservations import Record"):
            with self.subTest(source=source):
                self.assertEqual(violations(source, "queuekeeper.apple.devices"), [])
        self.assertEqual(violations("from ...mac_desktop import reservations", "queuekeeper.apple.voiceover.state"), [])
        for source in ("from .mac_desktop import foreground", "from queuekeeper.mac_desktop import foreground", "from . import mac_desktop"):
            with self.subTest(source=source):
                self.assertTrue(violations(source, "queuekeeper.remote"))

    def test_unity_may_import_the_core_and_the_desktop_extension_and_never_apple_nor_apple_it(self):
        for source in ("from .. import extensions", "from ..mac_desktop import foreground", "from . import editor"):
            with self.subTest(source=source):
                self.assertEqual(violations(source, "queuekeeper.unity.commands"), [])
        for source, module in (("from ..apple import devices", "queuekeeper.unity.commands"),
                               ("from vocaro_engineering import evidence", "queuekeeper.unity.commands"),
                               ("from ..unity import editor", "queuekeeper.apple.lane"),
                               ("from ..unity import editor", "queuekeeper.mac_desktop.lane"),
                               ("from .unity import editor", "queuekeeper.remote")):
            with self.subTest(source=source, module=module):
                self.assertTrue(violations(source, module))
        sources = {"queuekeeper": ("", True), "queuekeeper.unity": ("", True), "queuekeeper.apple": ("", True),
                   "queuekeeper.apple.devices": ("", False), "queuekeeper.unity.editor": ("", False),
                   "queuekeeper.unity.commands": ("from ..apple import devices", False),
                   "queuekeeper.apple.lane": ("from ..unity import editor", False)}
        layout = Layout(sources)
        self.assertEqual(layout.kinds("queuekeeper.unity.commands"), {("Unity imports Apple", "queuekeeper.apple.devices")})
        self.assertEqual(layout.kinds("queuekeeper.apple.lane"), {("Apple imports Unity", "queuekeeper.unity.editor")})

    def test_the_core_may_import_itself_the_library_and_names_that_only_start_alike(self):
        for source in ("import os\nfrom pathlib import Path", "from . import processes", "from .resources import BUSY",
                       "import queuekeeper.processes", "import vocaro_engineering_other",
                       "from queuekeeper import apple_notes", "from . import applesauce"):
            with self.subTest(source=source):
                self.assertEqual(violations(source, "queuekeeper.remote"), [])

    def test_nothing_imports_a_visibility_feature_but_itself(self):
        for source, module, package in (
                ("from .visibility import notify", "queuekeeper.cli", False),
                ("import queuekeeper.visibility.dashboard.server", "queuekeeper.merge_queue", False),
                ("from . import notify", "queuekeeper.visibility", True),
                ("from .. import notify", "queuekeeper.visibility.dashboard", True),
                ("from ..notify import rules", "queuekeeper.visibility.dashboard.server", False),
                ("from ..visibility import dashboard", "queuekeeper.apple.lane", False)):
            with self.subTest(source=source, module=module):
                self.assertTrue(violations(source, module, package))

    def test_a_visibility_feature_may_import_the_core_and_itself(self):
        for source, module, package in (
                ("from ... import events, merge_queue", "queuekeeper.visibility.notify", True),
                ("from . import rules\nfrom .channels import banner", "queuekeeper.visibility.notify", True),
                ("from .. import rules", "queuekeeper.visibility.notify.channels", True),
                ("from .. import visibility", "queuekeeper.cli", False),
                ("from .. import extensions, resources", "queuekeeper.visibility", True)):
            with self.subTest(source=source, module=module):
                self.assertEqual(violations(source, module, package), [])
        self.assertTrue(violations("from ...apple import devices", "queuekeeper.visibility.notify", True))

    def test_the_toolkit_may_import_either(self):
        self.assertEqual(violations("from queuekeeper.apple import devices\nimport queuekeeper",
                                    "vocaro_engineering.voiceover.speech"), [])


class KindControls(unittest.TestCase):
    """The reading of imports still written for the toolkit's layout finds each kind."""

    def kinds(self, source, module="queuekeeper.remote", package=False):
        sources = {"queuekeeper": ("__version__ = '1'", True), "queuekeeper.remote": ("", False),
                   "queuekeeper.processes": ("def run(): pass", False), "queuekeeper.apple": ("", True),
                   "queuekeeper.apple.devices": ("", False), "queuekeeper.apple.simulators": ("", True),
                   "queuekeeper.apple.simulators.pool": ("", False), "queuekeeper.apple.voiceover": ("", True),
                   "queuekeeper.apple.voiceover.state": ("", False)}
        sources[module] = (source, package)
        return Layout(sources).kinds(module)

    def test_the_core_imports_a_module_that_stayed_in_the_toolkit(self):
        for source, what in (("from . import evidence", "vocaro_engineering.evidence"),
                             ("from .evidence import RAN", "vocaro_engineering.evidence"),
                             ("from vocaro_engineering import release", "vocaro_engineering.release"),
                             ("def late():\n    from vocaro_engineering.release import lanes", "vocaro_engineering.release"),
                             ("from vocaro_engineering.voiceover import speech", "vocaro_engineering.voiceover.speech")):
            with self.subTest(source=source):
                self.assertEqual(self.kinds(source), {(CORE_TOOLKIT, what)})

    def test_the_core_imports_the_extension_under_either_name(self):
        for source, what in (("from . import devices", "queuekeeper.apple.devices"),
                             ("from .simulators import pool", "queuekeeper.apple.simulators.pool"),
                             ("from vocaro_engineering import devices", "queuekeeper.apple.devices"),
                             ("from vocaro_engineering.voiceover.state import preserved", "queuekeeper.apple.voiceover.state"),
                             ("from .apple import devices", "queuekeeper.apple.devices"),
                             ("import queuekeeper.apple", "queuekeeper.apple")):
            with self.subTest(source=source):
                self.assertEqual(self.kinds(source), {(CORE_APPLE, what)})

    def test_the_extension_imports_a_module_that_stayed_in_the_toolkit(self):
        for source, module, what in (
                ("from . import support", "queuekeeper.apple.devices", "vocaro_engineering.support"),
                ("from .. import support", "queuekeeper.apple.simulators.pool", "vocaro_engineering.support"),
                ("from . import speech", "queuekeeper.apple.voiceover.state", "vocaro_engineering.voiceover.speech"),
                ("from vocaro_engineering import evidence", "queuekeeper.apple.devices", "vocaro_engineering.evidence")):
            with self.subTest(source=source):
                self.assertEqual(self.kinds(source, module), {(APPLE_TOOLKIT, what)})

    def test_an_allowed_import_under_a_toolkit_name_is_an_old_name(self):
        for source, module, what in (
                ("from vocaro_engineering import processes", "queuekeeper.remote", "queuekeeper.processes"),
                ("from vocaro_engineering.processes import run", "queuekeeper.remote", "queuekeeper.processes"),
                ("import vocaro_engineering", "queuekeeper.remote", "queuekeeper"),
                ("from vocaro_engineering import __version__", "queuekeeper.remote", "queuekeeper"),
                ("from . import remote", "queuekeeper.apple.devices", "queuekeeper.remote"),
                ("from .. import remote", "queuekeeper.apple.simulators.pool", "queuekeeper.remote"),
                ("from vocaro_engineering.simulators import pool", "queuekeeper.apple.devices",
                 "queuekeeper.apple.simulators.pool")):
            with self.subTest(source=source, module=module):
                self.assertEqual(self.kinds(source, module), {(OLD_NAME, what)})

    def test_an_import_written_for_this_layout_is_no_finding(self):
        for source, module in (("from . import processes\nfrom .processes import run\nimport os", "queuekeeper.remote"),
                               ("from . import __version__", "queuekeeper.remote"),
                               ("from .. import remote\nfrom . import devices\nfrom .simulators import pool",
                                "queuekeeper.apple.devices"),
                               ("from ... import remote\nfrom .. import devices\nfrom ..devices import take\nfrom . import pool",
                                "queuekeeper.apple.simulators.pool"),
                               ("import queuekeeper.remote\nfrom queuekeeper.apple import devices",
                                "queuekeeper.apple.devices")):
            with self.subTest(source=source, module=module):
                self.assertEqual(self.kinds(source, module), set())


if __name__ == "__main__":
    if sys.argv[1:] == ["--report"]:
        held = Layout.read(SOURCE).lines()
        print("\n".join(held))
        print(f"{len(held)} in all: {summary(held)}")
    else:
        unittest.main()
