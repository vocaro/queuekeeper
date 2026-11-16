"""A runner started outside an admission, such as a bare `qk unittest`: it is admitted and placed first, as
`qk admit -- COMMAND` admits any command, unless the caller writes --unadmitted. Every admission here is taken
on the tests' own host (host_isolation), never on this Mac's."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import host_isolation  # first, as every test module

from queuekeeper import cli, extensions, resources

# A suite of one test, which passes only inside an admission.
INSIDE = ("import os, unittest\n"
          "class T(unittest.TestCase):\n"
          f"    def test_it_runs_inside_an_admission(self): self.assertTrue(os.environ.get({resources.TOKEN!r}))\n")


class Folder(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        # No test here runs inside the admission of the lane the suite runs in.
        self.enterContext(patch.dict(os.environ))
        os.environ.pop(resources.TOKEN, None)

    def suite(self):
        # A module name of its own: the run with no project discovers the suite in this process, which keeps it.
        (self.root / "tests").mkdir()
        (self.root / f"tests/test_inside_{self.root.name}.py").write_text(INSIDE)

    def project(self):
        (self.root / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Example", "lanes": {}}))

    def main(self, *arguments):
        """(status, what it printed) of the command line's arguments. What the commands it starts print is kept
        too, by descriptor: a stand-in suite's own `Ran 1 test` among this module's output would be counted as
        this module's by the runner the suite runs under."""
        said = io.StringIO()
        with tempfile.TemporaryFile("w+") as kept:
            sys.stdout.flush(), sys.stderr.flush()
            saved = os.dup(1), os.dup(2)
            os.dup2(kept.fileno(), 1), os.dup2(kept.fileno(), 2)
            try:
                with patch.object(cli, "refresh_policy", return_value=None), contextlib.redirect_stderr(said), \
                        contextlib.redirect_stdout(said):
                    status = cli.main(list(arguments))
            finally:
                for descriptor, number in zip(saved, (1, 2)):
                    os.dup2(descriptor, number)
                    os.close(descriptor)
            kept.seek(0)
            return status, said.getvalue() + kept.read()


class WhichCommands(Folder):
    def bare(self, *arguments, first=0):
        return cli.bare_runner(list(arguments), first)

    def test_a_runner_with_no_admission_is_bare_and_other_commands_are_not(self):
        self.assertEqual(self.bare("unittest", "-s", "tests"), (["unittest", "-s", "tests"], True))
        self.assertEqual(self.bare("--project", "p", "unittest", first=2)[1], True)
        for command in (["doctor"], ["run", "gate"], ["admit", "--", "python3", "scripts/qk", "unittest"], []):
            with self.subTest(command): self.assertEqual(self.bare(*command), (command, False))

    def test_an_extensions_runner_is_bare_and_its_other_commands_are_not(self):
        with patch.object(extensions, "RUNNERS", {("unity", "test"), ("xctest",)}):
            self.assertTrue(self.bare("unity", "test", "--platform", "PlayMode")[1])
            self.assertTrue(self.bare("xctest", "-j", "4")[1])
            self.assertFalse(self.bare("unity", "open")[1])
            self.assertFalse(self.bare("unity")[1])

    def test_the_shipped_extensions_name_their_runners(self):
        import queuekeeper_apple as apple; import queuekeeper_unity as unity
        apple.register()
        unity.register()
        self.assertLessEqual({("xctest",), ("simulator-tests",), ("unity", "test")}, extensions.RUNNERS)

    def test_a_runner_inside_an_admission_is_not_bare(self):
        with patch.dict(os.environ, {resources.TOKEN: "{}"}):
            self.assertEqual(self.bare("unittest", "-s", "tests"), (["unittest", "-s", "tests"], False))

    def test_unadmitted_is_taken_out_and_the_runner_is_not_bare(self):
        self.assertEqual(self.bare("unittest", "--unadmitted", "-s", "tests"), (["unittest", "-s", "tests"], False))
        self.assertEqual(self.bare("--project", "p", "unittest", "-s", "tests", "--unadmitted", first=2),
                         (["--project", "p", "unittest", "-s", "tests"], False))

    def test_unadmitted_after_the_separator_belongs_to_the_command_there(self):
        with patch.object(extensions, "RUNNERS", {("wrap",)}):
            command = ["wrap", "--", "tool", "--unadmitted"]
            self.assertEqual(self.bare(*command), (command, True))

    def test_a_request_for_help_is_not_bare(self):
        for option in ("-h", "--help"):
            self.assertFalse(self.bare("unittest", option)[1])


class CommandSent(Folder):
    def test_unittest_repeats_its_result_after_admission_notes_without_counting_it_twice(self):
        summary = "queuekeeper unittest: 7 tests (0 skipped) in 1 processes, 4 at a time, 1.0s: OK"
        for note in ("Verified cached artifact: https://example.com/artifact.zip",
                     "queuekeeper: the command ran on another Mac, so its build products stayed there"):
            for status in (0, 73):
                with self.subTest(note=note, status=status):
                    output = self.root / "output.log"
                    output.write_text(summary + "\n" + note + "\n")
                    @contextlib.contextmanager
                    def admitted(command):
                        yield {"status": None, "output": output}
                    said = io.StringIO()
                    with patch.object(cli.run_progress, "admitted", admitted), \
                            patch.object(cli, "admitted_run", return_value=status), contextlib.redirect_stderr(said):
                        self.assertEqual(cli.admitted_runner(self.root, ["unittest"], 0), status)
                    self.assertEqual(said.getvalue().splitlines()[-1], f"  {summary} (command exit {status})")
                    self.assertEqual(cli.merge_queue.UNITTEST_SUMMARY.findall(output.read_text() + said.getvalue()), ["OK"])

    def test_a_project_with_a_launcher_is_run_again_by_its_path_from_here(self):
        (self.root / "scripts").mkdir()
        (self.root / "scripts/qk").write_text("")
        self.assertEqual(cli.runner_command(self.root, ["unittest", "-s", "tests"]),
                         ["python3", "scripts/qk", "unittest", "-s", "tests"])
        (self.root / "tests").mkdir()
        os.chdir(self.root / "tests")
        self.assertEqual(cli.runner_command(self.root, ["unittest"]), ["python3", "../scripts/qk", "unittest"])

    def test_a_project_with_no_launcher_is_run_again_by_the_toolkits_command(self):
        self.assertEqual(cli.runner_command(self.root, ["xctest"]),
                         ["python3", str(cli.TOOLKIT_ROOT / "bin/qk"), "--project", ".", "xctest"])

    def test_in_a_project_the_runner_goes_to_admit_with_the_project(self):
        self.project()
        with patch.object(cli, "main", return_value=7) as main:
            self.assertEqual(cli.admitted_runner(self.root, ["--project", str(self.root), "unittest", "-s", "tests"], 2), 7)
        main.assert_called_once_with(["--project", str(self.root), "admit", "--", "python3",
                                      str(cli.TOOLKIT_ROOT / "bin/qk"), "--project", ".", "unittest", "-s", "tests"])

    def test_only_the_runner_run_again_changes_its_python3_on_this_mac(self):
        self.project()
        again = cli.runner_command(self.root, ["unittest", "-s", "tests"])
        self.assertEqual(cli.here_command(again), again)
        def main(argv):
            command = argv[argv.index("--") + 1:]
            self.assertEqual(command, again)
            self.assertEqual(cli.here_command(command), [sys.executable, *again[1:]])
            self.assertEqual(cli.here_command(["python3", "build.py"]), ["python3", "build.py"])
            return 7
        with patch.object(cli, "main", main):
            self.assertEqual(cli.admitted_runner(self.root, ["unittest", "-s", "tests"], 0), 7)
        self.assertEqual(cli.here_command(again), again)


class Runs(Folder):
    def test_a_bare_unittest_in_a_project_runs_inside_an_admission(self):
        self.project()
        self.suite()
        status, said = self.main("--project", str(self.root), "unittest", "-s", "tests")
        self.assertEqual(status, 0, said)

    def runs_under_this_python(self, *arguments):
        """Run the bare runner in the project while the first python3 on PATH is too old for Queuekeeper, as Xcode's
        Python 3.9 was on 2026-11-14 (vocaro/queuekeeper#152): a stand-in that runs nothing and exits 2, as the
        launcher does under such an interpreter. The second Mac is offered the command that names python3, and this
        Mac runs it under this interpreter: the suite's one test runs, is counted once and passes."""
        self.project()
        self.suite()
        tools = self.root / "older"
        tools.mkdir()
        host_isolation.executable(tools / "python3", "#!/bin/sh\necho 'the older python3 ran' >&2\nexit 2\n")
        os.environ["PATH"] = f"{tools}{os.pathsep}{os.environ['PATH']}"
        with patch.object(cli.remote, "first_command", wraps=cli.remote.first_command) as offered:
            status, said = self.main(*arguments, "unittest", "-s", "tests")
        self.assertEqual(status, 0, said)
        self.assertNotIn("the older python3 ran", said)
        self.assertRegex(said, r"(?m)^queuekeeper unittest: 1 tests ")
        self.assertEqual(cli.merge_queue.UNITTEST_SUMMARY.findall(said), ["OK"], said)
        self.assertEqual(offered.call_args.args[2][0], "python3")

    def test_a_bare_unittest_runs_under_the_python_that_started_it_whatever_python3_the_path_finds(self):
        self.runs_under_this_python("--project", str(self.root))

    def test_a_launchers_bare_unittest_runs_under_the_python_that_started_it(self):
        (self.root / "scripts").mkdir()
        (self.root / "scripts/qk").write_text((cli.TOOLKIT_ROOT / "scripts/qk").read_text())
        os.environ["QUEUEKEEPER_ROOT"] = str(cli.TOOLKIT_ROOT)
        self.runs_under_this_python()

    def test_a_bare_unittest_in_a_folder_with_no_project_runs_inside_an_admission(self):
        self.suite()
        status, said = self.main("unittest", "-s", "tests")
        self.assertEqual(status, 0, said)
        self.assertIsNone(os.environ.get(resources.TOKEN))

    def test_unadmitted_runs_it_with_no_admission(self):
        self.suite()
        status, said = self.main("unittest", "--unadmitted", "-s", "tests")
        self.assertEqual(status, 1, said)
        self.assertIn("test_it_runs_inside_an_admission", said)

    def test_with_no_project_and_no_slot_nothing_runs_and_the_status_is_busy(self):
        self.suite()
        with patch.object(resources.Admission, "__enter__", side_effect=TimeoutError("no slot came in 0s")):
            status, said = self.main("unittest", "-s", "tests")
        self.assertEqual(status, cli.BUSY)
        self.assertIn("unittest not run on", said)
        self.assertIn("no slot came in 0s", said)
        self.assertNotIn("Ran 1 test", said)


if __name__ == "__main__":
    unittest.main()
