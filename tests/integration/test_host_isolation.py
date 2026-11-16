import ast
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src/python"))
from queuekeeper import offload_host, parallel, resources, shims, waiting
from queuekeeper.parallel import unisolated
import host_isolation
from host_isolation import stretched

TESTS = Path(__file__).resolve().parents[1]
# A test marking a file executable itself, not through host_isolation.executable.
MARKS_EXECUTABLE = re.compile(r"\.chmod\(\s*0o7|\.chmod\(.*S_IEXEC|os\.chmod\(")


def starts_qk_unisolated(module):
    """Where module starts qk, `qk` or a path ending in /qk, with an `env=` built neither from os.environ nor by
    host_isolation.reduced and with no --unadmitted, as "test_x.py:LINE" (vocaro/queuekeeper#45).

    Such an environment drops what host_isolation set, so the qk it starts admits into this Mac's real namespace. A
    name is followed to what it is assigned in the function or the module, and self.NAME to what it is assigned in
    the class. An environment counts as built from os.environ when it is os.environ, a copy of it, a dict that spreads
    it or comprehends over it, or a name every assignment of which is one of these; reading one variable of it is not."""
    tree = ast.parse(module.read_text(), str(module))
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}

    def key(node):
        if isinstance(node, ast.Name): return node.id
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self":
            return f"self.{node.attr}"

    def assignments(scope):
        found = []
        for node in ast.walk(scope):
            targets = node.targets if isinstance(node, ast.Assign) else \
                [node.target] if isinstance(node, (ast.AnnAssign, ast.AugAssign)) and node.value is not None else []
            found += [(key(target), node.value) for each in targets for target in ast.walk(each) if key(target)]
        return found

    def enclosing(node, kinds):
        while node in parents:
            node = parents[node]
            if isinstance(node, kinds): return node

    def environ(node):
        return isinstance(node, ast.Attribute) and node.attr == "environ" and isinstance(node.value, ast.Name) \
            and node.value.id == "os"

    def carries(node, names):
        """Whether node is an environment built from os.environ or by reduced, given the names that are."""
        if environ(node) or key(node) in names: return True
        if isinstance(node, ast.Dict):
            return any(name is None and carries(value, names) for name, value in zip(node.keys, node.values))
        if isinstance(node, ast.DictComp):
            return any(carries(each.iter.func.value if isinstance(each.iter, ast.Call)
                               and isinstance(each.iter.func, ast.Attribute) else each.iter, names)
                       for each in node.generators)
        if isinstance(node, ast.Call):
            function = node.func
            if isinstance(function, ast.Attribute) and function.attr == "reduced": return True
            if isinstance(function, ast.Attribute) and function.attr == "copy": return carries(function.value, names)
            if isinstance(function, ast.Name) and function.id == "dict" and node.args:
                return carries(node.args[0], names)
        return False

    def mentions(node, names, constant):
        return any(constant(each) or key(each) in names for each in ast.walk(node))

    is_qk = lambda node: isinstance(node, ast.Constant) and isinstance(node.value, str) \
        and (node.value == "qk" or node.value.endswith("/qk"))
    is_unadmitted = lambda node: isinstance(node, ast.Constant) and node.value == "--unadmitted"
    module_level = [pair for statement in tree.body if isinstance(statement, ast.Assign) for pair in assignments(statement)]
    found = set()
    for call in ast.walk(tree):
        environment = next((each.value for each in getattr(call, "keywords", ()) if each.arg == "env"), None)
        if not isinstance(call, ast.Call) or environment is None or not call.args: continue
        function, cls = enclosing(call, (ast.FunctionDef, ast.AsyncFunctionDef)), enclosing(call, ast.ClassDef)
        pairs = module_level + [pair for pair in (assignments(cls) if cls else []) if pair[0].startswith("self.")] \
            + (assignments(function) if function else [])
        # Names whose any assignment mentions qk or --unadmitted, followed through names that mention those.
        reach = lambda constant: _closure(pairs, lambda value, names: mentions(value, names, constant))
        if not mentions(call.args[0], reach(is_qk), is_qk) or mentions(call.args[0], reach(is_unadmitted), is_unadmitted):
            continue
        # Names every assignment of which carries the isolation.
        carrying = {name for name, _ in pairs}
        while True:
            fewer = {name for name, value in pairs if name in carrying and not carries(value, carrying)}
            if not fewer: break
            carrying -= fewer
        if not carries(environment, carrying): found.add(f"{module.name}:{call.lineno}")
    return sorted(found, key=lambda place: int(place.rpartition(":")[2]))


def _closure(pairs, holds):
    names = set()
    while more := {name for name, value in pairs if name not in names and holds(value, names)}: names |= more
    return names


# Admits a heavy job with whatever namespace the environment names, after importing host_isolation
# when the first argument says so, and prints the guard and slot directory the admission used.
PROBE = """
import json, os, sys
sys.path.insert(0, sys.argv[1])
if sys.argv[2] == "isolated": import host_isolation
from queuekeeper import resources
with resources.Admission("forgot to redirect", timeout=0) as admission:
    print(json.dumps({"guard": str(admission.guard), "slots": str(admission.directory),
                      "held": admission.guard.exists()}))
"""


# Tests that bound a hang with stretched bounds, at a load of at least four per core, as on a loaded Mac: one
# command that never ends, one wait for what never happens, and a positive control that ends at once.
HANGS = textwrap.dedent("""
    import os, subprocess, sys, time, unittest
    from unittest.mock import patch
    import host_isolation
    from host_isolation import stretched

    class Hangs(unittest.TestCase):
        def setUp(self):
            load = max(4 * (os.cpu_count() or 1), os.getloadavg()[0])
            loaded = patch.object(host_isolation.os, "getloadavg", return_value=(load,) * 3)
            loaded.start()
            self.addCleanup(loaded.stop)

        def test_a_command_that_hangs(self):
            print(f"bound {stretched(0.25)}", file=sys.stderr, flush=True)
            subprocess.run(["sleep", "3600"], timeout=stretched(0.25))

        def test_a_wait_that_never_ends(self):
            deadline = time.monotonic() + stretched(0.25)
            while time.monotonic() < deadline: time.sleep(0.01)
            self.fail("what it waited for never happened")

        def test_a_command_that_ends(self):
            subprocess.run(["true"], timeout=stretched(30), check=True)
""")


class HostIsolationTests(unittest.TestCase):
    def test_every_test_module_imports_host_isolation(self):
        modules = sorted(TESTS.rglob("test*.py"))
        self.assertGreater(len(modules), 20)
        self.assertEqual(unisolated(modules), [])

    def test_the_check_finds_a_module_that_does_not_import_host_isolation(self):
        with tempfile.TemporaryDirectory() as directory:
            forgot, isolated, nested = (Path(directory) / name for name in ("test_forgot.py", "test_isolated.py",
                                                                            "test_nested.py"))
            forgot.write_text("import unittest\nfrom queuekeeper import cli\n")
            isolated.write_text("import unittest\nimport host_isolation\n")
            nested.write_text("def setUpModule():\n    import host_isolation\n")
            self.assertEqual(unisolated([forgot, isolated, nested]), ["test_forgot.py", "test_nested.py"])

    def test_no_test_module_marks_a_file_executable_itself(self):
        # A stand-in on PATH goes through host_isolation.executable, which checks that it runs (#214). This
        # module's own negative control below makes one that cannot run, so it is not scanned.
        found = [f"{module.name}:{number}" for module in sorted(TESTS.rglob("test*.py")) if module.name != Path(__file__).name
                 for number, line in enumerate(module.read_text().splitlines(), 1) if MARKS_EXECUTABLE.search(line)]
        self.assertEqual(found, [])
        for line in ("tool.chmod(0o755)", "path.chmod(path.stat().st_mode | stat.S_IEXEC)", "os.chmod(tool, 0o755)"):
            self.assertTrue(MARKS_EXECUTABLE.search(line), line)
        # A read-only directory is not a stand-in.
        self.assertIsNone(MARKS_EXECUTABLE.search("self.root.chmod(0o500)"))

    def test_no_test_module_starts_qk_with_an_environment_that_drops_the_isolation(self):
        # On 2026-11-12 three tests started `qk unittest` with only PATH and a limit, and each stand-in suite waited for
        # a slot in this Mac's real admission (vocaro/queuekeeper#45).
        found = [place for module in sorted(TESTS.rglob("test*.py")) for place in starts_qk_unisolated(module)]
        self.assertEqual(found, [])

    def test_the_check_finds_a_test_that_starts_qk_with_an_environment_that_drops_the_isolation(self):
        planted = textwrap.dedent('''
            import os, subprocess, sys
            from pathlib import Path
            import host_isolation
            QK = Path(__file__).resolve().parents[1] / "scripts/qk"

            class T:
                def setUp(self):
                    self.environment = {**os.environ, "A": "1"}
                    self.bare = {"PATH": "/usr/bin:/bin"}

                def test_bare(self):
                    subprocess.run([sys.executable, str(QK), "unittest", "-s", "x"], env={"PATH": "/usr/bin:/bin"})  # dropped

                def test_one_variable_read(self):
                    subprocess.run(["bin/qk", "--version"], env={"HEAVY_SLOT_DIR": os.environ["HEAVY_SLOT_DIR"]})  # dropped

                def test_named(self):
                    command = [sys.executable, str(QK), "admit", "--", "true"]
                    environment = {"PATH": "/usr/bin:/bin"}
                    subprocess.run(command, env=environment)  # dropped

                def test_named_twice(self):
                    environment = dict(os.environ)
                    environment = {"PATH": "/usr/bin:/bin"}
                    subprocess.run([str(Path("local") / "qk")], env=environment)  # dropped

                def test_the_class(self):
                    subprocess.Popen([sys.executable, str(QK), "run", "gate"], env=self.bare)  # dropped

                def test_inherited(self):
                    subprocess.run([sys.executable, str(QK), "run", "gate"], env=os.environ)
                    subprocess.run([sys.executable, str(QK), "run", "gate"], env={**os.environ, "A": "1"})
                    subprocess.run([sys.executable, str(QK), "run", "gate"], env=dict(os.environ, A="1"))
                    subprocess.run([sys.executable, str(QK), "run", "gate"], env=os.environ.copy())
                    subprocess.run([sys.executable, str(QK), "run", "gate"],
                                   env={k: v for k, v in os.environ.items() if k != "B"})
                    subprocess.run([sys.executable, str(QK), "run", "gate"], env={**self.environment, "C": "1"})
                    subprocess.run([sys.executable, str(QK), "run", "gate"], env=host_isolation.reduced(PATH="/bin"))

                def test_unadmitted(self):
                    command = [sys.executable, str(QK), "unittest", "--unadmitted", "-s", "x"]
                    subprocess.run(command, env={"PATH": "/usr/bin:/bin"})

                def test_not_qk(self):
                    subprocess.run([sys.executable, "-c", "pass"], env={"PATH": "/usr/bin:/bin"})
        ''')
        with tempfile.TemporaryDirectory() as directory:
            module = Path(directory) / "test_planted.py"
            module.write_text(planted)
            expected = [f"test_planted.py:{number}" for number, line in enumerate(planted.splitlines(), 1)
                        if line.endswith("# dropped")]
            self.assertEqual(starts_qk_unisolated(module), expected)

    def test_a_reduced_environment_carries_the_isolation_and_what_the_test_gives_it(self):
        environment = host_isolation.reduced({"PATH": "/bin"}, HOME="/h", HEAVY_SLOT_DIR="/mine")
        self.assertEqual({name: environment[name] for name in ("PATH", "HOME", "HEAVY_SLOT_DIR")},
                         {"PATH": "/bin", "HOME": "/h", "HEAVY_SLOT_DIR": "/mine"})
        self.assertEqual(environment["QUEUEKEEPER_GATE_LOCK_FILE"], host_isolation.GATE)
        self.assertEqual(host_isolation.reduced()["HEAVY_SLOT_DIR"], os.environ["HEAVY_SLOT_DIR"])
        # Each variable it carries is the one this process runs with.
        self.assertEqual({name: os.environ.get(name) for name in host_isolation.ISOLATION}, host_isolation.ISOLATION)

    def test_a_stand_in_that_would_not_run_is_refused_before_the_real_tool_runs_in_its_place(self):
        with tempfile.TemporaryDirectory() as directory:
            tools = Path(directory)
            # The negative control first: execvp runs the next match on PATH when a stand-in cannot run.
            indented = tools / "true"
            indented.write_text("    #!/bin/sh\nexit 3\n")
            indented.chmod(0o755)
            ran = subprocess.run(["true"], env={"PATH": f"{tools}:/usr/bin:/bin"})
            self.assertEqual(ran.returncode, 0, "the real true ran in the unrunnable stand-in's place")
            refused = {
                "indented": "    #!/bin/sh\nexit 0\n",
                "no-interpreter": "#!/no/such/interpreter\n",
                "missing-on-path": "#!/usr/bin/env no-such-interpreter-214\n",
                "python": f"#!{sys.executable}\nprint('a\n')\n",
                "env-python": "#!/usr/bin/env python3\nif True:\nprint(1)\n",
                "shell": "#!/bin/sh\nif then\n",
            }
            for name, text in refused.items():
                with self.subTest(name=name), self.assertRaises(AssertionError):
                    host_isolation.executable(tools / name, text)
                self.assertFalse(os.access(tools / name, os.X_OK), name)
            for name, text in {"sh": "#!/bin/sh\nexit 4\n", "python": f"#!{sys.executable}\nraise SystemExit(5)\n"}.items():
                with self.subTest(name=name):
                    made = host_isolation.executable(tools / f"good-{name}", text)
                    self.assertEqual(subprocess.run([str(made)]).returncode, {"sh": 4, "python": 5}[name])
            # Written beforehand, it is checked the same way.
            (tools / "late").write_text("#!/bin/sh\nexit 6\n")
            self.assertEqual(subprocess.run([str(host_isolation.executable(tools / "late"))]).returncode, 6)

    def test_this_process_resolves_admission_to_its_own_namespace(self):
        root = host_isolation.ROOT
        self.assertTrue(resources.guard_path(os.environ).is_relative_to(root))
        self.assertTrue(resources.slot_directory().is_relative_to(root))
        self.assertTrue(resources.log_path().is_relative_to(root))
        # A test that clears the environment still reaches no host guard.
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(resources.guard_path(os.environ).is_relative_to(root))
            self.assertTrue(Path(offload_host.GATE_LOCK).is_relative_to(root))

    def test_a_suite_run_inside_an_admission_takes_its_tests_out_of_it_and_off_the_second_mac(self):
        # The toolkit's gate is admitted (#200), so its suite inherits the gate's token; a test must not reuse it, nor
        # send a lane it runs to the real second Mac, nor report that lane's waits into the gate's own record (#335).
        token = '{"schemaVersion": 1, "locks": []}'
        source = str(Path(resources.__file__).resolve().parents[1])
        record = host_isolation.remote.lane_wait.VARIABLE
        environment = {**os.environ, resources.TOKEN: token, "HEAVY_SLOT_DIR": "/the/host's", record: "gate.json"}
        seen = {}
        for mode, imports in (("isolated", "import host_isolation; "), ("control", "")):
            code = (f"import os, sys; sys.path.insert(0, sys.argv[1]); {imports}"
                    "print(os.environ.get(sys.argv[2]), os.environ.get('HEAVY_SLOT_DIR') == \"/the/host's\", os.environ.get(sys.argv[3]))")
            result = subprocess.run([sys.executable, "-c", code, source, resources.TOKEN, record], env=environment, cwd=TESTS,
                                    capture_output=True, text=True, timeout=stretched(60))
            self.assertEqual(result.returncode, 0, result.stderr)
            seen[mode] = result.stdout.strip()
        self.assertEqual(seen, {"isolated": "None False None", "control": f"{token} True gate.json"})

    def test_a_run_outside_any_admission_and_outside_qk_unittest_is_told_so_once_and_runs_on(self):
        # On 2026-11-13 a bare `python3 -m unittest` took no slot while the host's were all held (#36). The warning
        # never fails the run, and is said once: neither a second import nor a process a test starts says it again.
        source = str(Path(resources.__file__).resolve().parents[1])
        marks = (resources.TOKEN, parallel.SHARD_VARIABLE, host_isolation.TOLD)
        bare = {name: value for name, value in os.environ.items() if name not in marks}
        code = ("import os, subprocess, sys; sys.path.insert(0, sys.argv[1]); import host_isolation; import host_isolation; "
                "subprocess.run([sys.executable, '-c', 'import sys; sys.path.insert(0, sys.argv[1]); import host_isolation', "
                "sys.argv[1]], check=True, cwd=os.getcwd()); print('ran on')")
        for name, value in (("bare", None), *((mark, '{"schemaVersion": 1, "locks": []}') for mark in marks)):
            with self.subTest(name=name):
                environment = bare if value is None else {**bare, name: value}
                result = subprocess.run([sys.executable, "-c", code, source], env=environment, cwd=TESTS,
                                        capture_output=True, text=True, timeout=stretched(60))
                self.assertEqual((result.returncode, result.stdout.strip()), (0, "ran on"), result.stderr)
                self.assertEqual(result.stderr.count(host_isolation.UNADMITTED), 1 if value is None else 0, result.stderr)
        # End to end: a module qk unittest's own test process runs is not told, and the same module under a bare
        # `python3 -m unittest` is.
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "test_probe.py").write_text(
                "import unittest\nimport host_isolation\n\nclass T(unittest.TestCase):\n    def test_t(self): pass\n")
            environment = {**bare, "PYTHONPATH": str(TESTS)}
            for how, command in (("qk unittest", [sys.executable, "-c", parallel.SHARD, directory, "null", "test_probe"]),
                                 ("bare", [sys.executable, "-m", "unittest", "test_probe"])):
                with self.subTest(how=how):
                    result = subprocess.run(command, env=environment, cwd=directory, capture_output=True, text=True,
                                            timeout=stretched(60))
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("Ran 1 test", result.stderr)
                    self.assertEqual(result.stderr.count(host_isolation.UNADMITTED), 0 if how == "qk unittest" else 1,
                                     result.stderr)

    def test_a_suite_run_inside_a_lane_drops_what_that_lane_set_for_its_own_command(self):
        # On 2026-11-09 fixtures wrote into the real gate's results/, and its summary named a fixture's test as failed (#289).
        cli = host_isolation.cli
        names = (cli.parallel.RESULTS_VARIABLE, "QUEUEKEEPER_STALE_MODULES_ROOT", cli.SELF_LANE, host_isolation.app_registrations.RUN,
                 host_isolation.devices.WINDOW, host_isolation.devices.OWNER, "QUEUEKEEPER_ROOT", "QUEUEKEEPER_LANE_PROGRESS_RUN")
        source = str(Path(resources.__file__).resolve().parents[1])
        for name in names:
            for mode, imports, expected in (("isolated", "import host_isolation; ", "None"), ("control", "", "outer")):
                with self.subTest(name=name, mode=mode):
                    code = f"import os, sys; sys.path.insert(0, sys.argv[1]); {imports}print(os.environ.get(sys.argv[2]))"
                    result = subprocess.run([sys.executable, "-c", code, source, name], env={**os.environ, name: "outer"},
                                            cwd=TESTS, capture_output=True, text=True, timeout=stretched(60))
                    self.assertEqual((result.returncode, result.stdout.strip()), (0, expected), result.stderr)

    def test_an_admission_that_forgets_to_redirect_stays_out_of_the_namespace_it_inherited(self):
        # The inherited namespace stands in for the host's: a test must never admit into the real one.
        with tempfile.TemporaryDirectory() as directory:
            host = Path(directory).resolve()
            environment = {**os.environ, "HEAVY_SLOT_DIR": str(host / "slots"), "QUEUEKEEPER_GATE_LOCK_FILE": str(host / "gate"),
                           "HEAVY_SLOTS": "1", "HEAVY_POLL_SECONDS": "0.01"}
            environment.pop(resources.TOKEN, None)
            source = str(Path(resources.__file__).resolve().parents[1])
            outcomes = {}
            for mode in ("isolated", "control"):
                result = subprocess.run([sys.executable, "-c", PROBE, source, mode], env=environment, cwd=TESTS,
                                        capture_output=True, text=True, timeout=stretched(60))
                self.assertEqual(result.returncode, 0, result.stderr)
                outcomes[mode] = json.loads(result.stdout.splitlines()[-1])
                self.assertTrue(outcomes[mode]["held"])
                if mode == "isolated":
                    # It wrote nothing at all to the namespace it inherited.
                    self.assertEqual(list(host.iterdir()), [])
            # The control, without host_isolation, admits into the inherited namespace: the probe sees a leak.
            self.assertEqual(outcomes["control"]["guard"], str(host / "gate.warm-push-guard"))
            self.assertEqual(outcomes["control"]["slots"], str(host / "slots"))
            # With it, the admission went elsewhere, and its namespace went with the process.
            for key in ("guard", "slots"):
                self.assertFalse(Path(outcomes["isolated"][key]).is_relative_to(host))
                self.assertIn("queuekeeper-test-host-", outcomes["isolated"][key])
                self.assertFalse(Path(outcomes["isolated"][key]).exists())


TOOLKIT = TESTS.parent
# A variable of the toolkit's, as its code names it.
VARIABLE = re.compile(rf"\b(?:{'|'.join(host_isolation.PREFIXES)})[A-Z0-9_]*[A-Z0-9]\b")
# Prints the toolkit's variables, with their values, that a process importing host_isolation is left with.
LEFT = ("import json, os, sys; sys.path.insert(0, sys.argv[1]); import host_isolation; print(json.dumps({name: value "
        "for name, value in os.environ.items() if name.startswith(host_isolation.PREFIXES)}))")


def toolkit_variables():
    """Every variable the toolkit's code names: the launchers, the package and the tools."""
    code = [*sorted((TOOLKIT / "plugins").rglob("*.py")), TOOLKIT / "bin/qk", TOOLKIT / "scripts/qk", *sorted((TOOLKIT / "src").rglob("*.py")),
            *sorted((TOOLKIT / "tools").glob("*.py"))]
    return sorted({name for path in code for name in VARIABLE.findall(path.read_text())})


class InheritedVariableTests(unittest.TestCase):
    """What host_isolation leaves of the toolkit's variables, which a run sets for the commands it starts and a suite
    run as one of them inherits (vocaro/queuekeeper#46). Each is set by host_isolation, cleared, or listed in INHERITED
    with why it is safe."""

    PLANTED = "planted by test_host_isolation"
    CONTROL = "QUEUEKEEPER_PLANTED_BY_TEST_HOST_ISOLATION"

    def left(self, planted):
        """The toolkit's variables a process importing host_isolation is left with, given the environment this
        process started with and planted."""
        result = subprocess.run([sys.executable, "-c", LEFT, str(TOOLKIT / "src/python")], cwd=TESTS,
                                env={**host_isolation.RECEIVED, **planted}, capture_output=True, text=True,
                                timeout=stretched(60))
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout.splitlines()[-1])

    def unexplained(self, left):
        """The variables in left that host_isolation neither set over what was planted nor lists."""
        return sorted(name for name, value in left.items() if name not in host_isolation.INHERITED
                      and (name not in host_isolation.ISOLATION or value == self.PLANTED))

    def test_what_this_suite_inherited_leaves_no_variable_that_is_neither_set_nor_listed(self):
        # On the second Mac, run for a dispatch, the suite inherits what the dispatch set; here, what this Mac's run did.
        self.assertEqual(self.unexplained(self.left({})), [])

    def test_every_variable_the_toolkit_names_is_set_cleared_or_listed(self):
        names = toolkit_variables()
        self.assertGreater(len(names), 40)
        # The keychain's, which reached the second Mac's suite before it was cleared, is among those found.
        self.assertIn(host_isolation.keychain.UNLOCKED, names)
        left = self.left(dict.fromkeys([*names, self.CONTROL], self.PLANTED))
        # The control, a variable neither set nor listed, is the only one found.
        self.assertEqual(self.unexplained(left), [self.CONTROL])
        self.assertNotIn(host_isolation.keychain.UNLOCKED, left)
        for name in host_isolation.ISOLATION: self.assertNotEqual(left.get(name), self.PLANTED, name)

    def test_each_inherited_variable_is_one_the_toolkit_names_and_says_why_it_is_safe(self):
        names = toolkit_variables()
        for name, reason in host_isolation.INHERITED.items():
            with self.subTest(name=name):
                self.assertIn(name, names)
                self.assertNotIn(name, host_isolation.ISOLATION)
                self.assertGreater(len(reason.split()), 5)


class SignalDispositionTests(unittest.TestCase):
    """default_signals, from a runner that ignores every stop signal, as a background job's shell or nohup can.

    Each stop signal but SIGQUIT is sent, and stops the child only with default_signals. SIGQUIT is not sent: its
    default ends a process with a core, so macOS writes a crash report and raises a "Python quit unexpectedly" alert
    over the screen each time, six times over foreground runs on the second Mac by 2026-11-12
    (vocaro/queuekeeper#42). Its disposition is read instead, which is what default_signals changes: the default
    with it, ignored without it."""

    # The stop signals sent: each one's default ends a process without a core or a crash report.
    SENT = tuple(number for number in host_isolation.STOP_SIGNALS if number != signal.SIGQUIT)
    # Prints SIGQUIT's disposition.
    QUIT = "import signal; print('default' if signal.getsignal(signal.SIGQUIT) == signal.SIG_DFL else 'ignored')"

    # Says it is ready, then waits for the test's go, which only an ignored stop reaches.
    CHILD = ("import os, sys, time\nfrom pathlib import Path\nhere = Path(sys.argv[1])\n(here / 'ready').touch()\n"
             "began = time.monotonic()\n"
             "while not (here / 'go').exists() and time.monotonic() - began < 120: time.sleep(0.01)")

    def stopped(self, number, **options):
        """The child's status after it is sent number and then told to go."""
        with tempfile.TemporaryDirectory() as directory:
            here = Path(directory)
            child = subprocess.Popen([sys.executable, "-I", "-c", self.CHILD, directory], **options)
            self.addCleanup(lambda: child.poll() is None and child.kill())
            deadline = time.monotonic() + stretched(30)
            while not (here / "ready").exists():
                self.assertIsNone(child.poll(), "the child ended before it was ready")
                self.assertLess(time.monotonic(), deadline, "the child was never ready")
                time.sleep(0.01)
            # An ignored signal is discarded when sent, so the go comes after it either way.
            child.send_signal(number)
            (here / "go").touch()
            return child.wait(stretched(30))

    def test_a_child_started_with_default_signals_is_stopped_though_its_runner_ignores_them(self):
        for number in host_isolation.STOP_SIGNALS:
            self.addCleanup(signal.signal, number, signal.signal(number, signal.SIG_IGN))
        self.assertEqual(set(self.SENT) | {signal.SIGQUIT}, set(host_isolation.STOP_SIGNALS))
        for number in self.SENT:
            with self.subTest(signal=number.name):
                self.assertEqual(self.stopped(number, preexec_fn=host_isolation.default_signals), -number)
                # The negative control: without it the child inherits the ignore and ends at the go.
                self.assertEqual(self.stopped(number), 0)
        with self.subTest(signal="SIGQUIT"):
            disposition = lambda **options: subprocess.run([sys.executable, "-I", "-c", self.QUIT], capture_output=True,
                                                           text=True, timeout=stretched(30), **options).stdout.strip()
            self.assertEqual(disposition(preexec_fn=host_isolation.default_signals), "default")
            self.assertEqual(disposition(), "ignored")


class StretchedTests(unittest.TestCase):
    def test_a_bound_stretches_by_the_load_per_core_and_never_shrinks(self):
        cores = os.cpu_count() or 1
        # With no busy share to doubt a low load by (waiting.stretch_reading, vocaro/queuekeeper#43).
        for load, expected in ((0.0, 2), (cores / 2, 2), (cores, 2), (3 * cores, 6)):
            with self.subTest(load=load), patch.object(host_isolation.os, "getloadavg", return_value=(load,) * 3), \
                    patch.object(waiting, "busy_now", return_value=None):
                self.assertAlmostEqual(stretched(2), expected)

    def test_a_stretched_bound_still_fails_a_test_that_really_hangs(self):
        """The negative control: the stretch is finite, so a test that really hangs fails at its bound instead of
        holding the suite, and a test that ends passes under the same load."""
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "test_hangs.py").write_text(HANGS)
            environment = {**os.environ, "PYTHONPATH": os.pathsep.join([str(TESTS), str(TESTS.parent / "src/python")])}
            result = subprocess.run([sys.executable, "-m", "unittest", "-v", "test_hangs"], cwd=directory,
                                    env=environment, capture_output=True, text=True, timeout=stretched(120))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("Ran 3 tests", result.stderr)
        self.assertIn("FAILED (failures=1, errors=1)", result.stderr)
        self.assertRegex(result.stderr, r"test_a_command_that_hangs \(test_hangs\.Hangs\.test_a_command_that_hangs\) "
                                         r"\.\.\. (bound \S+\n)?ERROR")
        self.assertIn("TimeoutExpired: Command '['sleep', '3600']' timed out after", result.stderr)
        self.assertRegex(result.stderr, r"test_a_wait_that_never_ends .* FAIL")
        self.assertIn("what it waited for never happened", result.stderr)
        self.assertRegex(result.stderr, r"test_a_command_that_ends .* ok")
        # The bound it failed at was stretched: four times 0.25 s at least.
        self.assertGreaterEqual(float(re.search(r"bound (\S+)\n", result.stderr).group(1)), 1)


if __name__ == "__main__":
    unittest.main()
