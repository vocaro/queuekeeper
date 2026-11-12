import concurrent.futures
import contextlib
from io import StringIO
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch
SOURCE = Path(__file__).resolve().parents[1] / "src/python"
sys.path.insert(0, str(SOURCE))
from queuekeeper import cli, lane_results, merge_queue, parallel, resources
from queuekeeper.parallel import SPLITS
import host_isolation
from host_isolation import stretched

ENG = Path(__file__).resolve().parents[1] / "scripts/qk"

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
        # In the suite's own directory by default, where a failing run's kept output goes with the suite.
        finished = subprocess.run([sys.executable, str(ENG), "unittest", "-s", str(root), *arguments],
                                  capture_output=True, text=True, cwd=root if cwd is None else cwd,
                                  env={"MEET_LIMIT": str(limit), "PATH": "/usr/bin:/bin"})
        # Standard error first: queuekeeper's line about a toolkit copy behind origin/main is not what a run prints last.
        return finished.returncode, finished.stderr + finished.stdout

    def test_modules_run_side_by_side_and_one_job_runs_them_one_after_another(self):
        root = self.suite(module("test_left", meeting("left", "right")), module("test_right", meeting("right", "left")))
        status, output = self.run_suite(root, "-j", "2")
        self.assertEqual(status, 0, output)
        self.assertNotEqual((root / "left.pid").read_text(), (root / "right.pid").read_text())
        self.assertIn("queuekeeper unittest: 2 tests (0 skipped) in 2 processes, 2 at a time", output)
        # The negative control: one at a time, neither module can meet the other.
        for marker in ("left", "right", "left.pid", "right.pid"): (root / marker).unlink()
        status, output = self.run_suite(root, "-j", "1", limit=1)
        self.assertEqual(status, 1, output)
        self.assertIn("right never appeared", output)

    def test_a_test_process_has_sigint_and_sigquit_at_their_defaults_though_its_runner_ignores_them(self):
        # A background job's shell ignores both, and a test that stops its child by SIGINT inherited that through
        # queuekeeper unittest (vocaro/BreezEPUB#47); the owner chose to reset them in the test processes alone (#385).
        root = self.suite(module("test_dispositions", """
            import signal, subprocess
            NAMES = {signal.SIG_IGN: "ignored", signal.SIG_DFL: "default", signal.default_int_handler: "python"}
            PROBE = ("import signal; print(' '.join('ignored' if signal.getsignal(n) == signal.SIG_IGN else 'not'"
                     " for n in (signal.SIGINT, signal.SIGQUIT)))")
            class D(unittest.TestCase):
                def test_record(self):
                    child = subprocess.run([sys.executable, "-c", PROBE], capture_output=True, text=True).stdout.split()
                    mine = [NAMES.get(signal.getsignal(n), "other") for n in (signal.SIGINT, signal.SIGQUIT)]
                    (HERE / "dispositions").write_text(" ".join(mine + child))
        """))
        def ignoring():
            for number in (signal.SIGINT, signal.SIGQUIT): signal.signal(number, signal.SIG_IGN)
        environment = {"PATH": "/usr/bin:/bin"}
        finished = subprocess.run([sys.executable, str(ENG), "unittest", "-s", str(root)], capture_output=True,
                                  text=True, cwd=root, env=environment, preexec_fn=ignoring)
        self.assertEqual(finished.returncode, 0, finished.stderr + finished.stdout)
        # The test process has Python's own SIGINT handler and the default SIGQUIT, and its children both defaults.
        self.assertEqual((root / "dispositions").read_text().split(), ["python", "default", "not", "not"])
        # The negative control: run by unittest itself from the same runner, the test inherits both ignores.
        finished = subprocess.run([sys.executable, "-m", "unittest", "test_dispositions"], capture_output=True,
                                  text=True, cwd=root, env=environment, preexec_fn=ignoring)
        self.assertEqual(finished.returncode, 0, finished.stderr)
        self.assertEqual((root / "dispositions").read_text().split(), ["ignored", "ignored", "ignored", "ignored"])

    def test_a_suite_with_host_isolation_runs_no_module_that_skips_it(self):
        # The merge queue's full suite found a new module without the import (#209's capture_approvals); queuekeeper unittest,
        # which a change's own modules run under, now refuses it before discovery imports anything.
        passing = "import unittest\nclass P(unittest.TestCase):\n    def test_p(self): pass\n"
        root = self.suite(("host_isolation", ""), ("test_isolated", "import host_isolation\n" + passing),
                          ("test_forgot", passing))
        status, output = self.run_suite(root)
        self.assertEqual(status, 1, output)
        self.assertIn("test_forgot.py must import host_isolation at its top level", output)
        self.assertIn("nothing ran", output)
        self.assertNotIn("==", output)
        status, output = self.run_suite(root, "-p", "test_isolated.py", "-p", "test_forgot.py")
        self.assertEqual(status, 1, output)
        self.assertIn("test_forgot.py must import host_isolation", output)
        # Controls: selecting only isolated modules runs, and a suite without host_isolation.py is not checked.
        status, output = self.run_suite(root, "-p", "test_isolated.py", cwd=root)
        self.assertEqual(status, 0, output)
        for name in ("host_isolation.py", "test_isolated.py"): (root / name).unlink()
        status, output = self.run_suite(root)
        self.assertEqual(status, 0, output)
        self.assertIn("== test_forgot: OK (1 tests", output)

    def test_a_module_that_does_not_parse_refuses_the_run_with_a_line_not_a_traceback(self):
        # A gate it ended with a traceback printed no summary and no refusal line, and bisected blind (#290).
        passing = "import unittest\nimport host_isolation\nclass P(unittest.TestCase):\n    def test_p(self): pass\n"
        root = self.suite(("host_isolation", ""), ("test_isolated", passing), ("test_broken", "def (:\n"),
                          ("test_forgot", "import unittest\n"))
        status, output = self.run_suite(root)
        self.assertEqual(status, 1, output)
        self.assertIn("queuekeeper unittest: test_broken.py does not parse (invalid syntax, line 1); nothing ran", output)
        self.assertIn("queuekeeper unittest: test_forgot.py must import host_isolation", output)
        self.assertNotIn("test_broken.py, ", output)
        self.assertNotIn("Traceback", output)
        self.assertNotIn("==", output)
        # The control: without it, the rest runs.
        for name in ("test_broken.py", "test_forgot.py"): (root / name).unlink()
        status, output = self.run_suite(root)
        self.assertEqual(status, 0, output)

    def test_a_process_past_its_timeout_is_stopped_and_leaves_the_run_unmeasured(self):
        # Stretched by load: the quick module and the failing one run under the same limit as the hung one, which
        # sleeps twelve times as long, so it is still stopped.
        limit = round(stretched(5))
        modules = (module("test_hung", f"class H(unittest.TestCase):\n    def test_hung(self): time.sleep({12 * limit})\n"),
                   module("test_quick", "class Q(unittest.TestCase):\n    def test_quick(self): pass\n"))
        root = self.suite(*modules)
        # A failure beside it still fails the run. Both runs wait out the hung module's limit, so they run at once.
        failing = self.suite(*modules, ("test_fail", "import unittest\nclass F(unittest.TestCase):\n"
                                                     "    def test_f(self): self.fail('planted')\n"))
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            alone, beside = [pool.submit(self.run_suite, suite, "--timeout", str(limit)) for suite in (root, failing)]
            (status, output), failed = alone.result(), beside.result()
        self.assertEqual(status, lane_results.UNMEASURED, output)
        self.assertIn(f"== test_hung: timed out after {limit}s (0 tests", output)
        self.assertIn("== test_quick: OK (1 tests", output)
        self.assertIn(f"UNMEASURED: test_hung (timed out after {limit}s)", output)
        status, output = failed
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
        self.assertEqual(lane_results.executed_tests(output), 4)

    def test_a_passing_suite_reports_its_unmeasured_skips_and_its_count(self):
        root = self.suite(module("test_mixed", """
            class M(unittest.TestCase):
                def test_measured(self): pass
                def test_busy(self): self.skipTest("Unmeasured: the host was busy")
        """))
        status, output = self.run_suite(root, "-v")
        self.assertEqual(status, 0, output)
        # The skipped test ran nothing, so only the measured one counts.
        self.assertEqual((lane_results.executed_tests(output), lane_results.skipped_tests(output)), (1, 1))
        self.assertIn("queuekeeper unittest: 2 tests (1 skipped) in 1 processes", output)
        self.assertEqual(len(lane_results.unmeasured_tests(output)), 1, output)

    def after_summary(self, output):
        """The lines queuekeeper unittest prints after its summary, which comes once."""
        lines = output.splitlines()
        summaries = [number for number, line in enumerate(lines) if line.startswith("queuekeeper unittest: ")]
        self.assertEqual(len(summaries), 1, output)
        return lines[summaries[0] + 1:]

    def test_the_last_lines_of_a_failed_run_say_why_each_test_failed_and_where_the_whole_output_is(self):
        root = self.suite(
            module("test_pass", "class P(unittest.TestCase):\n    def test_a(self): pass\n"),
            module("test_fail", """
                class F(unittest.TestCase):
                    def test_planted(self): self.fail('the planted reason')
                    def test_chained(self):
                        try: raise KeyError('first')
                        except KeyError as error: raise ValueError('the last of the chain') from error
                    def test_lines(self): self.fail('its first line\\nits second line')
                    def test_cases(self):
                        for case in (1, 2):
                            with self.subTest(case=case): self.assertEqual(case, 0)
            """),
            module("test_broken", "import a_module_that_does_not_exist\n"),
            module("test_dies", """
                class D(unittest.TestCase):
                    def test_dies(self):
                        print("the last it said", flush=True)
                        os._exit(7)
            """))
        status, output = self.run_suite(root)
        self.assertEqual(status, 1, output)
        after = self.after_summary(output)
        self.assertRegex(after[0], r"^Why each failed, the first line of each reason; the whole output is in .*\.log:$")
        self.assertEqual(sorted(after[1:]), sorted([
            "    test_broken: ModuleNotFoundError: No module named 'a_module_that_does_not_exist'",
            "    test_dies: its output ends: the last it said",
            "    test_fail.F.test_cases (case=1): AssertionError: 1 != 0",
            "    test_fail.F.test_cases (case=2): AssertionError: 2 != 0",
            "    test_fail.F.test_chained: ValueError: the last of the chain",
            "    test_fail.F.test_lines: AssertionError: its first line",
            "    test_fail.F.test_planted: AssertionError: the planted reason"]))
        # The rest can be read afterwards, in a record the kept-failure rule prunes.
        log = Path(after[0].split("the whole output is in ", 1)[1][:-1])
        self.assertTrue(log.is_relative_to((root / parallel.FAILED_RUNS).resolve()), log)
        saved = log.read_text()
        self.assertIn("its second line", saved)
        self.assertIn("queuekeeper unittest: ", saved)
        self.assertIn("Failed: test_fail.F.test_planted", (log.parent / "failures.txt").read_text())

    def test_a_passing_run_adds_nothing_after_its_summary_and_keeps_nothing(self):
        root = self.suite(module("test_pass", "class P(unittest.TestCase):\n    def test_a(self): pass\n"))
        status, output = self.run_suite(root)
        self.assertEqual(status, 0, output)
        self.assertEqual(self.after_summary(output), [])
        self.assertFalse((root / ".build").exists())

    def test_a_run_left_unmeasured_adds_nothing_after_its_summary(self):
        root = self.suite(module("test_hung", "class H(unittest.TestCase):\n    def test_hung(self): time.sleep(600)\n"))
        status, output = self.run_suite(root, "--timeout", "1")
        self.assertEqual(status, lane_results.UNMEASURED, output)
        self.assertEqual(self.after_summary(output), [])
        self.assertFalse((root / ".build").exists())

    def test_the_reasons_after_the_summary_are_bounded_and_say_what_was_cut(self):
        count, width = parallel.REASONS_SHOWN + 5, parallel.REASON_WIDTH
        methods = "".join(f"    def test_{number:02}(self): self.fail('words ' * 1000)\n" for number in range(count))
        root = self.suite(("test_many", f"import unittest\nclass M(unittest.TestCase):\n{methods}"))
        status, output = self.run_suite(root)
        self.assertEqual(status, 1, output)
        after = self.after_summary(output)
        self.assertEqual(len(after), 2 + parallel.REASONS_SHOWN, output)
        for line in after[1:-1]:
            self.assertLess(len(line), 2 * width, line)
            self.assertRegex(line, r"AssertionError: words words.* \[cut: \d+ more characters\]$")
        self.assertEqual(after[-1], f"    [cut: 5 more failures of {count}; the whole output has them]")
        # The control: as many failures as are shown, each within its width, are shown whole.
        self.assertEqual(parallel.clipped("a b", 3), "a b")
        reasons = [(f"test_{number}", "why") for number in range(parallel.REASONS_SHOWN)]
        self.assertEqual(len(parallel.reason_lines(reasons, "kept")), 1 + parallel.REASONS_SHOWN)
        self.assertNotIn("[cut", "".join(parallel.reason_lines(reasons, "kept")))

    def test_a_cut_reason_ends_at_a_word_so_no_path_is_shortened_into_another(self):
        # Cut at the width, this reason would end in src/longer/name.py, a path the queue could blame a change for.
        path, width = "src/longer/name.pyi", parallel.REASON_WIDTH
        text = "x" * (width - len(path)) + " " + path + " the rest"
        self.assertTrue(merge_queue.named_in("src/longer/name.py", text[:width] + " [cut]"))
        cut = parallel.clipped(text, width)
        self.assertFalse(merge_queue.named_in("src/longer/name.py", cut), cut)
        self.assertIn("[cut: ", cut)
        self.assertEqual(parallel.clipped("x" * (width + 1), width), f"[cut: {width + 1} more characters]")

    def test_a_failure_message_that_quotes_another_runs_failures_gives_one_reason(self):
        quoted = "\n" + "=" * 70 + "\nFAIL: test_inner (test_other.O.test_inner)\n" + "-" * 70 + "\nAssertionError: inner"
        root = self.suite(module("test_outer", f"""
            class O(unittest.TestCase):
                def test_outer(self): self.assertEqual(1, 2, {quoted!r})
        """))
        status, output = self.run_suite(root)
        self.assertEqual(status, 1, output)
        self.assertIn("AssertionError: inner", output)
        self.assertEqual(self.after_summary(output)[1:], ["    test_outer.O.test_outer: AssertionError: 1 != 2 : "])
        # The control: with no test of the process named, as a load_tests module's, each failure read is kept.
        self.assertEqual(len(parallel.failure_reasons(output, ["test_elsewhere"])), 2)

    def test_the_queue_reads_a_failed_run_with_its_reasons_as_it_reads_one_without_them(self):
        # Each reason quotes a line the queue reads, from the start of a line; the block must not start any of them.
        decoys = ["FAIL: test_decoy (test_decoy.D.test_decoy)", "ERROR: test_decoy (test_decoy.D.test_decoy)",
                  "== test_decoy: FAILED (failures=1) (1 tests, 0.1s)",
                  "queuekeeper unittest: 1 tests (0 skipped) in 1 processes, 1 at a time, 0.1s: OK",
                  "queuekeeper unittest: test_decoy.py does not parse; nothing ran"]
        methods = "".join(f"    def test_{number}(self): raise RuntimeError({decoy!r})\n"
                          for number, decoy in enumerate(decoys))
        root = self.suite(("test_real", f"import unittest\nclass R(unittest.TestCase):\n{methods}"),
                          module("test_pass", "class P(unittest.TestCase):\n    def test_a(self): pass\n"))
        status, output = self.run_suite(root)
        self.assertEqual(status, 1, output)
        after = self.after_summary(output)
        self.assertEqual(len(after), 1 + len(decoys), output)
        lines = output.splitlines()
        without = "\n".join(lines[:len(lines) - len(after)]) + "\n"
        ids = [f"test_real.R.test_{number}" for number in range(len(decoys))]
        for name, log in (("without the reasons", without), ("with them", output)):
            with self.subTest(name):
                self.assertEqual(merge_queue.failed_tests(log), (ids, None))
                self.assertEqual(merge_queue.UNITTEST_SUMMARY.findall(log), merge_queue.UNITTEST_SUMMARY.findall(without))
                self.assertEqual(merge_queue.REFUSAL.findall(log), [])
                self.assertEqual(lane_results.counted(log), lane_results.counted(without))
        # The negative control: the same lines not indented do change what the queue reads, so the test can fail.
        unindented = without + "\n".join(decoys) + "\n"
        self.assertNotEqual(merge_queue.failed_tests(unindented), (ids, None))

    def test_a_failed_run_that_cannot_save_its_output_says_so_and_keeps_its_result(self):
        root = self.suite(module("test_fail", "class F(unittest.TestCase):\n    def test_a(self): self.fail('planted')\n"))
        (root / ".build").write_text("a file where the folder would go")
        status, output = self.run_suite(root)
        self.assertEqual(status, 1, output)
        after = self.after_summary(output)
        self.assertRegex(after[0], r"^Why each failed, the first line of each reason; the whole output was not saved: ")
        self.assertEqual(after[1:], ["    test_fail.F.test_a: AssertionError: planted"])

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
        # The negative control: by default the module is one process.
        self.assertEqual(self.run_suite(root)[0], 0)
        self.assertEqual(len(set(pids())), 1)

    def test_a_module_that_sets_shard_by_is_split_that_finely_and_never_more_coarsely_than_split(self):
        source = """
            class A(unittest.TestCase):
                def test_one(self): (HERE / f"one").write_text(str(os.getpid()))
                def test_two(self): (HERE / f"two").write_text(str(os.getpid()))
            class B(unittest.TestCase):
                def test_three(self): (HERE / f"three").write_text(str(os.getpid()))
            class Grouped(unittest.TestCase):
                serial_group = "kept"
                def test_four(self): (HERE / f"four").write_text(str(os.getpid()))
            class AlsoGrouped(unittest.TestCase):
                serial_group = "kept"
                def test_five(self): (HERE / f"five").write_text(str(os.getpid()))
        """
        root = self.suite(module("test_split", 'shard_by = "class"\n' + textwrap.dedent(source)),
                          module("test_whole", "class W(unittest.TestCase):\n"
                                               "    def test_six(self): (HERE / 'six').write_text(str(os.getpid()))\n"
                                               "    def test_seven(self): (HERE / 'seven').write_text(str(os.getpid()))\n"))
        names = ("one", "two", "three", "four", "five", "six", "seven")
        pids = lambda: dict(zip(names, ((root / name).read_text() for name in names)))
        status, output = self.run_suite(root)
        self.assertEqual(status, 0, output)
        self.assertIn("7 tests (0 skipped) in 4 processes", output)
        seen = pids()
        # Its classes apart, and a class serial_group still together; the module that sets none stays whole.
        self.assertEqual(seen["one"], seen["two"])
        self.assertEqual(len({seen["one"], seen["three"], seen["four"], seen["six"]}), 4)
        self.assertEqual((seen["four"], seen["six"]), (seen["five"], seen["seven"]))
        # A finer --split still splits it by test, and the group stays one process.
        self.assertEqual(self.run_suite(root, "--split", "test")[0], 0)
        seen = pids()
        self.assertEqual(seen["four"], seen["five"])
        self.assertEqual(len(set(seen.values())), 6)

    def test_class_batches_keep_the_selected_tests_and_respect_finer_or_serial_shards(self):
        root = self.suite(module("test_batched", """
            class A(unittest.TestCase):
                def test_one(self): pass
                def test_two(self): pass
                def test_three(self): pass
            class Serial(A):
                serial_group = "kept"
        """), module("test_fine", """
            shard_by = "test"
            class F(unittest.TestCase):
                def test_one(self): pass
                def test_two(self): pass
        """))
        batch = ["--split", "class", "--batch-size", "2"]
        status, output = self.run_suite(root, *batch)
        self.assertEqual(status, 0, output)
        self.assertIn("== test_batched.A[1]: OK (2 tests", output)
        self.assertIn("== test_batched.A[2]: OK (1 tests", output)
        self.assertIn("== group:kept: OK (3 tests", output)
        self.assertIn("8 tests (0 skipped) in 5 processes", output)
        status, output = self.run_suite(root, *batch, "test_batched.A.test_three", "-v")
        self.assertEqual(status, 0, output)
        self.assertIn("test_batched.A.test_three", output)
        self.assertIn("1 tests (0 skipped) in 1 processes", output)
        self.assertEqual(self.run_suite(root, "--batch-size", "2")[0], 2)
        self.assertEqual(self.run_suite(root, "--split", "class", "--batch-size", "0")[0], 2)

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
            self.assertIn("queuekeeper unittest: 4 tests (0 skipped) in 2 processes", output)
            self.assertEqual((root / "own").read_text(), (root / "one").read_text())

    def selectable(self):
        """Two modules whose tests each touch a marker named for themselves when they run."""
        return self.suite(
            module("test_alpha", """
                class Landing(unittest.TestCase):
                    def test_add(self): (HERE / "alpha.Landing.add").touch()
                    def test_drop(self): (HERE / "alpha.Landing.drop").touch()
                class LandingTail(unittest.TestCase):
                    def test_add(self): (HERE / "alpha.LandingTail.add").touch()
                class Other(unittest.TestCase):
                    def test_add(self): (HERE / "alpha.Other.add").touch()
            """),
            module("test_beta", """
                class Landing(unittest.TestCase):
                    def test_keep(self): (HERE / "beta.Landing.keep").touch()
            """))

    def ran(self, root):
        found = sorted(path.name for path in root.iterdir() if path.name.count(".") == 2)
        for name in found: (root / name).unlink()
        return found

    def test_k_selects_tests_as_unittests_k_does_under_every_split(self):
        root = self.selectable()
        for split in SPLITS:
            with self.subTest(split=split):
                # A pattern without a * matches as a substring of the test id, in every module.
                status, output = self.run_suite(root, "--split", split, "-k", "Landing")
                self.assertEqual(status, 0, output)
                self.assertIn("queuekeeper unittest: 4 tests (0 skipped)", output)
                self.assertEqual(self.ran(root), ["alpha.Landing.add", "alpha.Landing.drop", "alpha.LandingTail.add",
                                                  "beta.Landing.keep"])
                # One with a * is an fnmatch pattern on the whole id, and -k repeats as an or.
                status, output = self.run_suite(root, "--split", split, "-k", "*.Landing.test_drop",
                                                "-k", "test_beta.*")
                self.assertEqual(status, 0, output)
                self.assertEqual(self.ran(root), ["alpha.Landing.drop", "beta.Landing.keep"])

    def test_a_name_selects_a_module_class_or_test_and_what_it_holds(self):
        root = self.selectable()
        for split in SPLITS:
            with self.subTest(split=split):
                # A class name is not a prefix of the next class's: Landing leaves LandingTail out.
                status, output = self.run_suite(root, "--split", split, "test_alpha.Landing")
                self.assertEqual(status, 0, output)
                self.assertIn("queuekeeper unittest: 2 tests (0 skipped)", output)
                self.assertEqual(self.ran(root), ["alpha.Landing.add", "alpha.Landing.drop"])
                status, output = self.run_suite(root, "--split", split, "test_beta", "test_alpha.Other.test_add")
                self.assertEqual(status, 0, output)
                self.assertEqual(self.ran(root), ["alpha.Other.add", "beta.Landing.keep"])
                # With -k, a test must be under a name and match a pattern.
                status, output = self.run_suite(root, "--split", split, "-k", "add", "test_alpha")
                self.assertEqual(status, 0, output)
                self.assertEqual(self.ran(root), ["alpha.Landing.add", "alpha.LandingTail.add", "alpha.Other.add"])

    def test_a_selection_that_matches_nothing_fails_and_runs_nothing(self):
        root = self.selectable()
        for arguments, words in ((["-k", "NoSuchTest"], "is selected by -k NoSuchTest"),
                                 (["-k", "keep", "test_alpha"], "is selected by -k keep and test_alpha"),
                                 # A name that matches nothing fails the run though another matches.
                                 (["test_alpha.Landing", "test_alpha.Landng"], "is or is under test_alpha.Landng"),
                                 (["test_alpha.Land"], "is or is under test_alpha.Land")):
            with self.subTest(arguments=arguments):
                status, output = self.run_suite(root, *arguments)
                self.assertEqual(status, 5, output)
                self.assertIn(words, output)
                self.assertNotIn("queuekeeper unittest: 0 tests", output)
                self.assertEqual(self.ran(root), [])

    def test_a_selection_reaches_a_module_that_runs_whole(self):
        root = self.suite(module("test_loader", """
            import importlib.util
            class Own(unittest.TestCase):
                def test_own(self): (HERE / "loader.Own.own").touch()
            def load_tests(loader, tests, pattern):
                spec = importlib.util.spec_from_file_location("loaded_tests", HERE / "probes" / "check.py")
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                tests.addTests(loader.loadTestsFromModule(loaded))
                return tests
        """))
        (root / "probes").mkdir()
        (root / "probes/check.py").write_text(module("check", """
            class Loaded(unittest.TestCase):
                def test_one(self): (HERE.parent / "loaded.Loaded.one").touch()
                def test_two(self): (HERE.parent / "loaded.Loaded.two").touch()
        """)[1])
        for split in SPLITS:
            with self.subTest(split=split):
                # Its process runs only the selected test, so its count matches discovery's.
                status, output = self.run_suite(root, "--split", split, "-k", "test_one")
                self.assertEqual(status, 0, output)
                self.assertIn("== test_loader: OK (1 tests", output)
                self.assertEqual(self.ran(root), ["loaded.Loaded.one"])
                status, output = self.run_suite(root, "--split", split, "loaded_tests.Loaded.test_two")
                self.assertEqual(status, 0, output)
                self.assertEqual(self.ran(root), ["loaded.Loaded.two"])

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
        self.assertIn("queuekeeper unittest: 2 tests (0 skipped) in 1 processes", output)
        # The negative control: from another directory the package is not there to import.
        status, output = self.run_suite(root, cwd=root)
        self.assertEqual(status, 1, output)
        self.assertIn("No module named 'helpers'", output)

    def test_each_pattern_adds_the_modules_it_matches_and_a_module_two_match_runs_once(self):
        """A second -p once replaced the first without a word: a run meant for three modules ran one."""
        root = self.suite(
            module("test_alpha", "class A(unittest.TestCase):\n    def test_a(self): pass\n"),
            module("test_beta", "class B(unittest.TestCase):\n    def test_b(self): pass\n    def test_c(self): pass\n"),
            module("test_gamma", "class G(unittest.TestCase):\n    def test_g(self): self.fail('not asked for')\n"))
        status, output = self.run_suite(root, "-p", "test_alpha.py", "-p", "test_beta.py")
        self.assertEqual(status, 0, output)
        self.assertEqual(lane_results.executed_tests(output), 3)
        self.assertIn("3 tests (0 skipped) in 2 processes", output)
        # Overlapping patterns run a module once.
        status, output = self.run_suite(root, "-p", "test_beta.py", "-p", "test_b*.py")
        self.assertEqual((status, lane_results.executed_tests(output)), (0, 2), output)
        # Negative controls: one pattern alone is as before, and the default still finds every module.
        self.assertEqual(lane_results.executed_tests(self.run_suite(root, "-p", "test_alpha.py")[1]), 1)
        status, output = self.run_suite(root)
        self.assertEqual((status, lane_results.executed_tests(output)), (1, 4), output)
        # A pattern that matches nothing names every pattern asked for.
        status, output = self.run_suite(root, "-p", "test_none.py", "-p", "test_nada.py")
        self.assertEqual(status, 5, output)
        self.assertIn("matching test_none.py or test_nada.py", output)

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
    from queuekeeper import resources

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
    """queuekeeper unittest passes each module's process the admission lock descriptors that it holds itself
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
        """Run queuekeeper unittest holding these descriptors, as queuekeeper admit starts a command, under this token
        or none. Returns its status, its output and what its module's process found."""
        environment = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(SOURCE), **self.isolated,
                       "PROBE_FDS": ",".join(str(fd) for fd in probed), **({resources.TOKEN: token} if token else {})}
        finished = subprocess.run([sys.executable, str(ENG), "unittest", "-s", str(self.here / "suite")],
                                  capture_output=True, text=True, env=environment, pass_fds=held)
        output, probe = finished.stdout + finished.stderr, self.here / "suite/probe.json"
        found = json.loads(probe.read_text()) if probe.exists() else None
        probe.unlink(missing_ok=True)
        # Nothing fails to start: the module's process ran its one test.
        self.assertIn("queuekeeper unittest: 1 tests (0 skipped) in 1 processes", output)
        return finished.returncode, output, found

    def test_a_module_holds_the_admission_eng_unittest_holds_and_reuses_it(self):
        with contextlib.redirect_stderr(StringIO()), resources.Admission("outer", timeout=0):
            token = os.environ[resources.TOKEN]
            (item,) = json.loads(token)["locks"]
            fd, lock = item["fd"], os.stat(item["path"]).st_ino
            # The lock reaches the module's process as the lock file itself, and a nested admission
            # there reuses it; another descriptor queuekeeper unittest holds does not.
            status, output, found = self.run_suite(token, (fd, self.unrelated), (fd, self.unrelated))
            self.assertEqual(status, 0, output)
            self.assertEqual(found, {"held": {str(fd): lock, str(self.unrelated): None}, "inherited": True})
            # Started without the descriptor, as a plain subprocess call starts its child, queuekeeper unittest
            # has the token alone and passes on nothing: a nested admission is refused, not given a slot.
            status, output, found = self.run_suite(token, (), (fd,))
            self.assertEqual(status, 0, output)
            self.assertNotEqual(found["held"][str(fd)], lock)
            self.assertRegex(found["inherited"], "^Invalid inherited resource admission: ")
            # With no token, queuekeeper unittest holds the lock and passes on nothing.
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
        # Closed in queuekeeper unittest's process, open there on another file, or open on the lock without
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
        # queuekeeper unittest started under queuekeeper admit did. Passed on, that closed descriptor stopped a lane
        # from starting.
        closed = next(fd for fd in range(200, 1000) if not is_open(fd))
        token = json.dumps({"schemaVersion": 1, "locks": [{"fd": closed, "path": str(self.root / "slot0"), "nonce": "outer"}]})
        # Unadmitted, as the lanes were before every lane was admitted (2026-11-07): an admitted lane checks the token.
        self.configure({"one": {"command": [sys.executable, "-c", "print('Ran 1 test')"], "minimumTests": 1,
                                "resource": "self"},
                        "two": {"command": [sys.executable, "-c", "print('Ran 1 test')"], "minimumTests": 1}})
        for lane in ["one"]:
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
                   {"parallel": ["one", "inner"]}, {"parallel": ["one"], "resource": "self"},
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
        # A group that gives no resource is heavy, as its lanes are (the owner's decision, 2026-11-07).
        for group in ({"parallel": ["one", "two"], "jobs": 1}, {"parallel": ["heavy", "one"], "resource": "exclusive"},
                      {"parallel": ["heavy"]}):
            with self.subTest(group), contextlib.redirect_stderr(StringIO()) as error:
                self.configure({**lanes, "group": group})
                cli.load(self.root)
            self.assertEqual(error.getvalue(), "")


class HandUpTests(unittest.TestCase):
    """A lane nested in another copies its results/ into the outer lane's (vocaro-engineering#218)."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.inner, self.outer = self.root / "suites/results", self.root / "gate/results"
        (self.inner / "ios-tests/simulator-tests").mkdir(parents=True)
        (self.inner / "ios-tests/simulator-tests/iPhone-1.log").write_text("✔ Test \"t\" passed after 0.7 seconds.\n")
        self.outer.mkdir(parents=True)

    def hand_up(self, results=None, outer=None):
        environment = {} if outer is False else {parallel.RESULTS_VARIABLE: str(outer or self.outer)}
        return parallel.hand_up(results or self.inner, "suites", environment)

    def test_a_nested_lanes_results_go_into_the_outer_lanes_under_its_name(self):
        self.assertEqual(self.hand_up(), self.outer / "suites")
        self.assertIn("passed after 0.7", (self.outer / "suites/ios-tests/simulator-tests/iPhone-1.log").read_text())
        # Again, over what an earlier copy left, as a gate that runs its suites twice would.
        (self.inner / "ios-tests/simulator-tests/iPhone-2.log").write_text("second\n")
        self.assertEqual(self.hand_up(), self.outer / "suites")
        self.assertEqual(sorted(p.name for p in (self.outer / "suites/ios-tests/simulator-tests").iterdir()),
                         ["iPhone-1.log", "iPhone-2.log"])

    def test_nothing_is_copied_outside_a_lane_from_an_empty_results_or_into_its_own(self):
        self.assertIsNone(self.hand_up(outer=False))
        empty = self.root / "empty/results"
        empty.mkdir(parents=True)
        self.assertIsNone(self.hand_up(results=empty))
        self.assertIsNone(self.hand_up(results=self.root / "absent"))
        # The lane's own results/, or a group's lane's folder inside them, is not another lane's.
        self.assertIsNone(self.hand_up(outer=self.inner))
        self.assertIsNone(self.hand_up(outer=self.inner / "ios-tests"))
        self.assertEqual(list(self.outer.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
