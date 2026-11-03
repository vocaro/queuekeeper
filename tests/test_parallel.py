import contextlib
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch
SOURCE = Path(__file__).resolve().parents[1] / "src/python"
sys.path.insert(0, str(SOURCE))
from vocaro_engineering import cli, evidence, resources
import host_isolation
from host_isolation import stretched

ENG = Path(__file__).resolve().parents[1] / "scripts/eng"

# Waits until the file named by its second argument exists, writing the first: two such commands
# end only when both run at once. The third argument bounds the wait in seconds.
RENDEZVOUS = textwrap.dedent("""
    import pathlib, sys, time
    mine, theirs, limit = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), float(sys.argv[3])
    mine.touch()
    deadline = time.monotonic() + limit
    while not theirs.exists():
        if time.monotonic() > deadline: sys.exit(f"{theirs.name} never appeared")
        time.sleep(0.05)
    print("Ran 1 test")
""")


def is_open(fd):
    try: os.fstat(fd)
    except OSError: return False
    return True


def module(name, body):
    return name, textwrap.dedent("""
        import os, pathlib, sys, time, unittest
        HERE = pathlib.Path(__file__).resolve().parent
    """) + textwrap.dedent(body)


# A unittest module that meets another at a rendezvous, as RENDEZVOUS does.
def meeting(mine, theirs, group=None):
    return textwrap.dedent(f"""
        {f'serial_group = "{group}"' if group else ''}
        class Meet(unittest.TestCase):
            def test_meet(self):
                (HERE / "{mine}.pid").write_text(str(os.getpid()))
                (HERE / "{mine}").touch()
                deadline = time.monotonic() + float(os.environ["MEET_LIMIT"])
                while not (HERE / "{theirs}").exists():
                    if time.monotonic() > deadline: self.fail("{theirs} never appeared")
                    time.sleep(0.05)
    """)


class UnittestRunnerTests(unittest.TestCase):
    def suite(self, *modules):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        for name, source in modules:
            (root / f"{name}.py").write_text(source)
        return root

    def run_suite(self, root, *arguments, limit=None, cwd=None):
        # A meeting's limit stretches by load where the modules should meet; a negative control's 1 s stays
        # fixed, since load only makes a module that cannot meet the other fail surer.
        limit = stretched(60) if limit is None else limit
        finished = subprocess.run([sys.executable, str(ENG), "unittest", "-s", str(root), *arguments],
                                  capture_output=True, text=True, cwd=cwd,
                                  env={"MEET_LIMIT": str(limit), "PATH": "/usr/bin:/bin"})
        return finished.returncode, finished.stdout + finished.stderr

    def test_modules_run_side_by_side_and_one_job_runs_them_one_after_another(self):
        root = self.suite(module("test_left", meeting("left", "right")), module("test_right", meeting("right", "left")))
        status, output = self.run_suite(root, "-j", "2")
        self.assertEqual(status, 0, output)
        self.assertNotEqual((root / "left.pid").read_text(), (root / "right.pid").read_text())
        self.assertIn("eng unittest: 2 tests (0 skipped) in 2 processes, 2 at a time", output)
        # The negative control: one at a time, neither module can meet the other.
        for marker in ("left", "right", "left.pid", "right.pid"): (root / marker).unlink()
        status, output = self.run_suite(root, "-j", "1", limit=1)
        self.assertEqual(status, 1, output)
        self.assertIn("right never appeared", output)

    def test_a_process_past_its_timeout_is_stopped_and_leaves_the_run_unmeasured(self):
        # Stretched by load: the quick module and the failing one run under the same limit as the hung one, which
        # sleeps twelve times as long, so it is still stopped.
        limit = round(stretched(5))
        root = self.suite(module("test_hung", f"class H(unittest.TestCase):\n    def test_hung(self): time.sleep({12 * limit})\n"),
                          module("test_quick", "class Q(unittest.TestCase):\n    def test_quick(self): pass\n"))
        status, output = self.run_suite(root, "--timeout", str(limit))
        self.assertEqual(status, evidence.UNMEASURED, output)
        self.assertIn(f"== test_hung: timed out after {limit}s (0 tests", output)
        self.assertIn("== test_quick: OK (1 tests", output)
        self.assertIn(f"UNMEASURED: test_hung (timed out after {limit}s)", output)
        # A failure beside it still fails the run.
        (root / "test_fail.py").write_text("import unittest\nclass F(unittest.TestCase):\n    def test_f(self): self.fail('planted')\n")
        status, output = self.run_suite(root, "--timeout", str(limit))
        self.assertEqual(status, 1, output)
        self.assertRegex(output, r"FAILED: test_fail .*; UNMEASURED: test_hung")
        self.assertEqual(self.run_suite(root, "--timeout", "0")[0], 2)

    def test_a_serial_group_runs_its_modules_in_one_process(self):
        root = self.suite(module("test_left", meeting("left", "right", group="shared")),
                          module("test_right", meeting("right", "left", group="shared")),
                          module("test_alone", "class Alone(unittest.TestCase):\n    def test_alone(self): pass\n"))
        status, output = self.run_suite(root, "-j", "3", limit=1)
        # The group's modules never run at once, so the first cannot meet the second.
        self.assertEqual(status, 1, output)
        self.assertIn("== group:shared: FAILED (failures=1) (2 tests", output)
        self.assertIn("== test_alone: OK (1 tests", output)
        self.assertEqual((root / "left.pid").read_text(), (root / "right.pid").read_text())

    def test_each_test_counts_once_and_failures_and_import_errors_fail_the_run(self):
        root = self.suite(
            module("test_pass", "class P(unittest.TestCase):\n    def test_a(self): pass\n    def test_b(self): pass\n"),
            module("test_fail", "class F(unittest.TestCase):\n    def test_a(self): self.fail('planted')\n"),
            module("test_broken", "import a_module_that_does_not_exist\n"))
        status, output = self.run_suite(root, "-v")
        self.assertEqual(status, 1, output)
        self.assertIn("FAILED: test_broken (FAILED (errors=1), status 1), test_fail (FAILED (failures=1), status 1)", output)
        self.assertIn("No module named 'a_module_that_does_not_exist'", output)
        # The runner's own summary is not a "Ran N tests" line, so a lane counts each test once.
        self.assertEqual(evidence.executed_tests(output), 4)

    def test_a_passing_suite_reports_its_unmeasured_skips_and_its_count(self):
        root = self.suite(module("test_mixed", """
            class M(unittest.TestCase):
                def test_measured(self): pass
                def test_busy(self): self.skipTest("Unmeasured: the host was busy")
        """))
        status, output = self.run_suite(root, "-v")
        self.assertEqual(status, 0, output)
        # The skipped test ran nothing, so only the measured one counts.
        self.assertEqual((evidence.executed_tests(output), evidence.skipped_tests(output)), (1, 1))
        self.assertIn("eng unittest: 2 tests (1 skipped) in 1 processes", output)
        self.assertEqual(len(evidence.unmeasured_tests(output)), 1, output)

    def test_a_process_that_ends_before_its_tests_have_run_fails(self):
        root = self.suite(module("test_exits", """
            class E(unittest.TestCase):
                def test_exits(self): os._exit(0)
                def test_never_runs(self): pass
        """))
        status, output = self.run_suite(root)
        self.assertEqual(status, 1, output)
        self.assertIn("FAILED: test_exits (ran 0 of 2 tests)", output)

    def test_a_class_or_test_split_runs_each_in_its_own_process(self):
        root = self.suite(module("test_split", """
            class A(unittest.TestCase):
                def test_one(self): (HERE / f"one").write_text(str(os.getpid()))
                def test_two(self): (HERE / f"two").write_text(str(os.getpid()))
            class B(unittest.TestCase):
                def test_three(self): (HERE / f"three").write_text(str(os.getpid()))
        """))
        pids = lambda: [(root / name).read_text() for name in ("one", "two", "three")]
        self.assertEqual(self.run_suite(root, "--split", "class")[0], 0)
        one, two, three = pids()
        self.assertEqual(one, two)
        self.assertNotEqual(one, three)
        self.assertEqual(self.run_suite(root, "--split", "test")[0], 0)
        self.assertEqual(len(set(pids())), 3)

    def test_a_module_loaded_under_a_name_no_process_can_import_runs_whole_in_its_loaders_process(self):
        root = self.suite(module("test_loader", """
            import importlib.util
            class Own(unittest.TestCase):
                def test_own(self): (HERE / "own").write_text(str(os.getpid()))
            def load_tests(loader, tests, pattern):
                spec = importlib.util.spec_from_file_location("loaded_tests", HERE / "probes" / "check.py")
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                tests.addTests(loader.loadTestsFromModule(loaded))
                return tests
        """), module("test_other", "class Other(unittest.TestCase):\n    def test_other(self): pass\n"))
        (root / "probes").mkdir()
        (root / "probes/check.py").write_text(module("check", """
            class Loaded(unittest.TestCase):
                def test_one(self): (HERE.parent / "one").write_text(str(os.getpid()))
                def test_two(self): pass
        """)[1])
        # Every split runs the loader module's tests, its loaded module's among them, in one process.
        for split in ("module", "class", "test"):
            status, output = self.run_suite(root, "--split", split)
            self.assertEqual(status, 0, output)
            self.assertIn("== test_loader: OK (3 tests", output)
            self.assertIn("eng unittest: 4 tests (0 skipped) in 2 processes", output)
            self.assertEqual((root / "own").read_text(), (root / "one").read_text())

    def test_a_module_imports_from_and_runs_in_the_directory_eng_runs_in(self):
        root = self.suite(module("test_invoked", """
            from helpers import VALUE
            class Invoked(unittest.TestCase):
                def test_value(self): self.assertEqual(VALUE, 7)
                def test_directory(self): self.assertTrue(pathlib.Path("marker").exists())
        """))
        invoked = root / "project"
        (invoked / "helpers").mkdir(parents=True)
        (invoked / "helpers/__init__.py").write_text("VALUE = 7\n")
        (invoked / "marker").touch()
        status, output = self.run_suite(root, cwd=invoked)
        self.assertEqual(status, 0, output)
        self.assertIn("eng unittest: 2 tests (0 skipped) in 1 processes", output)
        # The negative control: from another directory the package is not there to import.
        status, output = self.run_suite(root, cwd=root)
        self.assertEqual(status, 1, output)
        self.assertIn("No module named 'helpers'", output)

    def test_a_suite_without_tests_is_not_a_pass(self):
        root = self.suite(module("helper", "VALUE = 1\n"))
        status, output = self.run_suite(root)
        self.assertEqual(status, 5, output)
        self.assertIn("no tests found", output)

    def test_jobs_needs_at_least_one(self):
        root = self.suite()
        self.assertEqual(self.run_suite(root, "-j", "0")[0], 2)


# A unittest module that records the file its process holds at each number PROBE_FDS names, by
# inode or None for none, and whether it inherits an admission: True, False or why it is refused.
PROBE = textwrap.dedent("""
    import json, os, pathlib, unittest
    from vocaro_engineering import resources

    class Probe(unittest.TestCase):
        def test_probe(self):
            held = {}
            for number in os.environ["PROBE_FDS"].split(","):
                try: held[number] = os.fstat(int(number)).st_ino
                except OSError: held[number] = None
            try: admission = resources.inherited()
            except RuntimeError as error: admission = str(error)
            found = {"held": held, "inherited": admission}
            (pathlib.Path(__file__).resolve().parent / "probe.json").write_text(json.dumps(found))
""")


class ShardAdmissionTests(unittest.TestCase):
    """eng unittest passes each module's process the admission lock descriptors that it holds itself
    and no other descriptor, so a nested admission in a test reuses the admission, as it does under
    `python3 -m unittest discover`. Each module's process used to have the token without its
    descriptor, and a nested admission there was refused (#70)."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.here = Path(temporary.name)
        (self.here / "suite").mkdir()
        (self.here / "suite/test_probe.py").write_text(PROBE)
        # Admission here and in the module's process uses these slots and this guard, never the host's.
        self.isolated = {"HEAVY_SLOT_DIR": str(self.here / "slots"), "SW_GATE_LOCK_FILE": str(self.here / "gate")}
        variables = patch.dict(os.environ, self.isolated)
        variables.start()
        self.addCleanup(variables.stop)
        # The caller's own token is restored afterward.
        os.environ.pop(resources.TOKEN, None)
        (self.here / "unrelated").write_text("not a lock\n")
        self.unrelated = self.held_at(self.here / "unrelated")

    def held_at(self, path):
        """A number above those Popen's own pipes take, open here on path until the test ends."""
        number = next(fd for fd in range(200, 1000) if not is_open(fd))
        opened = os.open(path, os.O_RDWR)
        os.dup2(opened, number)
        os.close(opened)
        self.addCleanup(os.close, number)
        return number

    def run_suite(self, token, held, probed):
        """Run eng unittest holding these descriptors, as eng admit starts a command, under this token
        or none. Returns its status, its output and what its module's process found."""
        environment = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(SOURCE), **self.isolated,
                       "PROBE_FDS": ",".join(str(fd) for fd in probed), **({resources.TOKEN: token} if token else {})}
        finished = subprocess.run([sys.executable, str(ENG), "unittest", "-s", str(self.here / "suite")],
                                  capture_output=True, text=True, env=environment, pass_fds=held)
        output, probe = finished.stdout + finished.stderr, self.here / "suite/probe.json"
        found = json.loads(probe.read_text()) if probe.exists() else None
        probe.unlink(missing_ok=True)
        # Nothing fails to start: the module's process ran its one test.
        self.assertIn("eng unittest: 1 tests (0 skipped) in 1 processes", output)
        return finished.returncode, output, found

    def test_a_module_holds_the_admission_eng_unittest_holds_and_reuses_it(self):
        with contextlib.redirect_stderr(StringIO()), resources.Admission("outer", timeout=0):
            token = os.environ[resources.TOKEN]
            (item,) = json.loads(token)["locks"]
            fd, lock = item["fd"], os.stat(item["path"]).st_ino
            # The lock reaches the module's process as the lock file itself, and a nested admission
            # there reuses it; another descriptor eng unittest holds does not.
            status, output, found = self.run_suite(token, (fd, self.unrelated), (fd, self.unrelated))
            self.assertEqual(status, 0, output)
            self.assertEqual(found, {"held": {str(fd): lock, str(self.unrelated): None}, "inherited": True})
            # Started without the descriptor, as a plain subprocess call starts its child, eng unittest
            # has the token alone and passes on nothing: a nested admission is refused, not given a slot.
            status, output, found = self.run_suite(token, (), (fd,))
            self.assertEqual(status, 0, output)
            self.assertNotEqual(found["held"][str(fd)], lock)
            self.assertRegex(found["inherited"], "^Invalid inherited resource admission: ")
            # With no token, eng unittest holds the lock and passes on nothing.
            status, output, found = self.run_suite(None, (fd, self.unrelated), (fd, self.unrelated))
            self.assertEqual(status, 0, output)
            self.assertNotEqual(found["held"][str(fd)], lock)
            self.assertEqual((found["held"][str(self.unrelated)], found["inherited"]), (None, False))

    def test_a_module_gets_no_descriptor_that_is_not_the_lock_of_the_tokens_owner(self):
        lock = self.here / "lock"
        lock.write_text(f"{os.getpid()} forged label\n")
        number = self.held_at(lock)
        token = lambda fd, nonce: json.dumps({"schemaVersion": 1, "exclusive": False, "model": False,
            "guard": str(self.here / "gate.warm-push-guard"), "locks": [{"fd": fd, "path": str(lock), "nonce": nonce}]})
        closed = next(fd for fd in range(200, 1000) if not is_open(fd))
        # Closed in eng unittest's process, open there on another file, or open on the lock without
        # the token's owner in it, the token's number is not passed on: passed, a closed one stops
        # the module's process from starting with EBADF. The positive control: open on the lock that
        # names the owner, it is passed.
        cases = [("closed", closed, "forged", None), ("another file", self.unrelated, "forged", None),
                 ("another owner", number, "another", None), ("the owner's lock", number, "forged", os.stat(lock).st_ino)]
        for case, fd, nonce, expected in cases:
            with self.subTest(case):
                status, output, found = self.run_suite(token(fd, nonce), (self.unrelated, number), (fd,))
                self.assertEqual(status, 0, output)
                self.assertEqual(found["held"], {str(fd): expected})
                # This guard records no such owner, so even the passed lock admits nothing.
                self.assertRegex(found["inherited"], "^Invalid inherited resource admission: ")


class LaneGroupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def configure(self, lanes):
        (self.root / "engineering.json").write_text(json.dumps({"schemaVersion": 1, "name": "Group", "lanes": lanes}))

    def run_lane(self, *arguments):
        with patch.object(cli, "source_identity", return_value={}), contextlib.redirect_stdout(StringIO()) as output, \
                contextlib.redirect_stderr(StringIO()) as error:
            status = cli.main(["--project", str(self.root), "run", *arguments])
        return status, output.getvalue() + error.getvalue()

    def meet(self, mine, theirs, limit):
        return {"command": [sys.executable, "-c", RENDEZVOUS, str(self.root / mine), str(self.root / theirs), str(limit)],
                "minimumTests": 1}

    def test_a_group_runs_its_lanes_side_by_side_and_serial_runs_them_in_turn(self):
        self.configure({"left": self.meet("left", "right", stretched(60)), "right": self.meet("right", "left", stretched(60)),
                        "both": {"parallel": ["left", "right"]}})
        status, output = self.run_lane("both")
        self.assertEqual(status, 0, output)
        self.assertIn("PASS left", output)
        self.assertIn("PASS right", output)
        receipt = json.loads((self.root / ".build/engineering/both/source.json").read_text())
        self.assertEqual((receipt["exitStatus"], receipt["jobs"], sorted(receipt["lanes"])), (0, 2, ["left", "right"]))
        self.assertIn("Ran 1 test", (self.root / ".build/engineering/both/lanes/left.log").read_text())
        # Each lane's receipt sits beside its log, with the tests its log counts.
        own = json.loads((self.root / ".build/engineering/both/lanes/left.json").read_text())
        self.assertEqual({key: own[key] for key in ("lane", "status", "outcome", "executedTests", "skippedTests", "log")},
                         {"lane": "left", "status": 0, "outcome": "passed", "executedTests": 1, "skippedTests": 0,
                          "log": "left.log"})
        self.assertEqual(receipt["lanes"]["right"]["executedTests"], 1)
        # The negative control: in turn, the first lane cannot meet the second. Its 1 s stays fixed: load only
        # makes a lane that cannot meet the other fail surer.
        self.configure({"left": self.meet("late", "never", 1), "right": self.meet("never", "late", 1),
                        "both": {"parallel": ["left", "right"]}})
        status, output = self.run_lane("--serial", "both")
        self.assertEqual(status, 1, output)
        self.assertIn("never never appeared", output)
        self.assertEqual(json.loads((self.root / ".build/engineering/both/source.json").read_text())["jobs"], 1)

    def test_any_failure_fails_the_group_and_otherwise_any_unmeasured_lane_leaves_it_unmeasured(self):
        say = lambda text, status=0, **lane: {"command": [sys.executable, "-c", f"print({text!r}); raise SystemExit({status})"],
                                              **lane}
        unmeasured = "/p/T.swift:5: -[P.T testA] : Test skipped - Unmeasured: busy\nRan 1 test"
        lanes = {"pass": say("Ran 2 tests", minimumTests=2), "fail": say("Ran 2 tests", 1),
                 "unmeasured": say(unmeasured), "short": say("Ran 1 test", minimumTests=2),
                 # Fixed: the slow lane's 1 s timeout is the behavior under test; the others have none.
                 "slow": {"command": [sys.executable, "-c", "import time; time.sleep(60)"], "timeout": 1}}
        cases = {("pass",): 0, ("pass", "unmeasured"): 3, ("pass", "slow"): 3, ("pass", "unmeasured", "fail"): 1,
                 ("pass", "short"): 1}
        for members, expected in cases.items():
            with self.subTest(members):
                self.configure({**lanes, "group": {"parallel": list(members)}})
                status, output = self.run_lane("group")
                self.assertEqual(status, expected, output)
                receipt = json.loads((self.root / ".build/engineering/group/source.json").read_text())
                # Every lane runs to its own end, whatever the others did.
                self.assertEqual(sorted(receipt["lanes"]), sorted(members))
                self.assertEqual(receipt["outcome"], {0: "passed", 1: "failed", 3: "unmeasured"}[expected])
                for member in members:
                    own = json.loads((self.root / f".build/engineering/group/lanes/{member}.json").read_text())
                    self.assertEqual(own["outcome"], {"pass": "passed", "fail": "failed", "short": "failed",
                                                      "unmeasured": "unmeasured", "slow": "unmeasured"}[member])
        self.assertIn("Expected at least 2 executed tests; observed 1", output)
        self.assertIn("FAIL short", output)
        # A lane too short for its minimum, and one its timeout ended, say why beside their counts.
        short = json.loads((self.root / ".build/engineering/group/lanes/short.json").read_text())
        self.assertEqual((short["status"], short["executedTests"], short["error"]),
                         (2, 1, "Expected at least 2 executed tests; observed 1"))
        slow = json.loads((self.root / ".build/engineering/group/lanes/slow.json").read_text())
        self.assertEqual((slow["status"], slow["executedTests"]), (3, None))
        self.assertIn("timed out after 1s", slow["error"])

    def test_lanes_start_under_an_admission_token_whose_descriptor_this_process_does_not_hold(self):
        # A process can hold the admission's token without its descriptor, as each module's process
        # eng unittest started under eng admit did. Passed on, that closed descriptor stopped a lane
        # from starting.
        closed = next(fd for fd in range(200, 1000) if not is_open(fd))
        token = json.dumps({"schemaVersion": 1, "locks": [{"fd": closed, "path": str(self.root / "slot0"), "nonce": "outer"}]})
        self.configure({"one": {"command": [sys.executable, "-c", "print('Ran 1 test')"], "minimumTests": 1},
                        "two": {"command": [sys.executable, "-c", "print('Ran 1 test')"], "minimumTests": 1},
                        "group": {"parallel": ["one", "two"]}})
        for lane in ["one", "group"]:
            with self.subTest(lane), patch.dict(os.environ, {"VOCARO_RESOURCE_TOKEN": token}):
                status, output = self.run_lane(lane)
                self.assertEqual(status, 0, output)
                self.assertNotIn("did not start", output)

    def test_a_group_takes_no_arguments_and_serial_needs_a_group(self):
        self.configure({"one": {"command": [sys.executable, "-c", "pass"]}, "group": {"parallel": ["one"]}})
        for arguments in (["group", "--", "x"], ["--expect", "1", "group"], ["--serial", "one"]):
            with self.subTest(arguments):
                self.assertEqual(self.run_lane(*arguments)[0], 2)
        self.assertFalse((self.root / ".build/engineering/group").exists())

    def test_a_group_names_runnable_lanes_and_holds_their_heaviest_resource(self):
        command = {"command": ["true"]}
        lanes = {"one": command, "two": command, "heavy": {**command, "resource": "heavy"},
                 "screen": {**command, "resource": "heavy", "foreground": True}}
        invalid = [{"parallel": []}, {"parallel": ["one", "one"]}, {"parallel": ["missing"]},
                   {"parallel": ["one", "inner"]}, {"parallel": ["heavy"]},
                   {"parallel": ["heavy"], "resource": "heavy", "foreground": True},
                   {"parallel": ["screen"], "resource": "heavy"}, {"parallel": ["one", "two"], "jobs": 0},
                   {"parallel": ["one", "two"], "jobs": 3}, {"parallel": ["one"], "command": ["true"]},
                   {"parallel": ["one"], "buildHost": True}, {"parallel": ["one"], "minimumTests": 1}]
        for group in invalid:
            with self.subTest(group):
                self.configure({**lanes, "inner": {"parallel": ["one"]}, "group": group})
                self.assertEqual(self.run_lane("group")[0], 2)
        # A group that gives buildHost, which vocaro-engineering#58 removed, is refused for it, true or false.
        for value in [True, False]:
            with self.subTest(buildHost=value):
                self.configure({**lanes, "group": {"parallel": ["heavy", "one"], "resource": "exclusive",
                                                   "buildHost": value}})
                status, said = self.run_lane("group")
                self.assertEqual(status, 2, said)
                self.assertIn("engineering.json gives group \"buildHost\", which vocaro-engineering#58 removed", said)
        # The positive controls, the second the group above without buildHost.
        for group in ({"parallel": ["one", "two"], "jobs": 1}, {"parallel": ["heavy", "one"], "resource": "exclusive"}):
            with self.subTest(group), contextlib.redirect_stderr(StringIO()) as error:
                self.configure({**lanes, "group": group})
                cli.load(self.root)
            self.assertEqual(error.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
