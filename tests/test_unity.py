"""The Unity extension: the editor a project pins, its lock, the licensing helper, a batch run that goes silent, and
the `queuekeeper unity` commands (vocaro/queuekeeper#6). No test starts, closes or signals a Unity process: the
process list and the launch are stand-ins."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import host_isolation  # first, as every test module

from queuekeeper import config, disk, extensions, unity
from queuekeeper.lane_results import UNMEASURED, executed_tests
from queuekeeper.unity import batch, commands, disk as unity_disk, doctor, editor, host, lane, licensing, project, settings

unity.register()


def fake_editor(hub, version, executable=True):
    binary = Path(hub) / version / editor.BINARY
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\nexit 0\n")
    return host_isolation.executable(binary) if executable else binary


class Folder(unittest.TestCase):
    def setUp(self):
        # Resolved, as lsof and a process's command line name a path.
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()

    def unity_project(self, version="6000.6.0f1", name="lab"):
        found = self.folder / "repo" / name
        (found / "ProjectSettings").mkdir(parents=True)
        (found / "Temp").mkdir()
        (found / "ProjectSettings/ProjectVersion.txt").write_text(
            f"m_EditorVersion: {version}\nm_EditorVersionWithRevision: {version} (abc)\n")
        return found


class EditorTests(Folder):
    def pick(self, hub, want):
        found = editor.resolve(self.folder / hub, want)
        return found.relative_to(self.folder / hub).parts[0] if found else None

    def test_the_pinned_editor_is_picked_over_a_newer_patch_beside_it(self):
        for version in ("6000.6.0f1", "6000.6.1f1"): fake_editor(self.folder / "hub", version)
        self.assertEqual(self.pick("hub", "6000.6.0f1"), "6000.6.0f1")

    def test_a_mac_without_the_pinned_editor_falls_back_to_its_newest_by_number(self):
        for version in ("6000.5.0f1", "6000.6.1f1"): fake_editor(self.folder / "absent", version)
        self.assertEqual(self.pick("absent", "6000.6.0f1"), "6000.6.1f1")
        for version in ("6000.9.0f1", "6000.10.0f1"): fake_editor(self.folder / "numeric", version)
        self.assertEqual(self.pick("numeric", "6000.6.0f1"), "6000.10.0f1")
        self.assertEqual(self.pick("numeric", None), "6000.10.0f1")

    def test_a_pinned_install_that_cannot_run_is_passed_over_and_an_empty_hub_finds_nothing(self):
        fake_editor(self.folder / "broken", "6000.6.0f1", executable=False)
        fake_editor(self.folder / "broken", "6000.6.1f1")
        self.assertEqual(self.pick("broken", "6000.6.0f1"), "6000.6.1f1")
        (self.folder / "empty").mkdir()
        self.assertIsNone(self.pick("empty", "6000.6.0f1"))
        self.assertIsNone(self.pick("missing", "6000.6.0f1"))

    def test_the_project_s_pin_is_read_and_an_editor_named_by_the_variable_is_taken_first(self):
        found = self.unity_project("6000.6.0f1")
        self.assertEqual(editor.pinned(found), "6000.6.0f1")
        self.assertIsNone(editor.pinned(self.folder / "nowhere"))
        pinned = fake_editor(self.folder / "hub", "6000.6.0f1")
        named = fake_editor(self.folder / "other", "6000.7.0f1")
        self.assertEqual(editor.find(found, {}, self.folder / "hub"), (pinned, "hub", "6000.6.0f1"))
        self.assertEqual(editor.find(found, {"UNITY": str(named)}, self.folder / "hub"), (named, "$UNITY", "6000.6.0f1"))
        self.assertEqual(editor.find(found, {"UNITY": "/no/such/editor"}, self.folder / "hub")[0], pinned)

    def test_a_run_says_when_its_editor_is_not_the_pinned_one(self):
        pinned = fake_editor(self.folder / "hub", "6000.6.0f1")
        newer = fake_editor(self.folder / "hub", "6000.6.1f1")
        self.assertIn("matches the pinned 6000.6.0f1", editor.described(pinned, "hub", "6000.6.0f1"))
        self.assertIn("WARNING", editor.described(newer, "hub", "6000.6.0f1"))
        self.assertEqual(editor.app(pinned), self.folder / "hub/6000.6.0f1/Unity.app")


class LockTests(Folder):
    """The five cases of vocaro/mpm's tools/unity/test-with-editor-closed.sh."""

    def setUp(self):
        super().setUp()
        self.project = self.unity_project()
        self.lock = self.project / project.LOCK
        self.said = []

    def clear(self):
        return project.clear_stale_lock(self.project, say=self.said.append)

    def test_a_lock_no_process_holds_is_removed(self):
        self.lock.touch()
        self.assertEqual(self.clear(), project.CLEARED)
        self.assertFalse(self.lock.exists())
        self.assertIn("cleared stale", self.said[0])

    def test_a_lock_a_process_holds_open_is_kept_and_its_holder_named(self):
        self.lock.touch()
        with open(self.lock) as held:
            holder = subprocess.Popen(["sleep", "60"], stdin=held)
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        for _ in range(50):
            if str(holder.pid) in subprocess.run(["lsof", "-w", "-t", "--", str(self.lock)], capture_output=True,
                                                 text=True).stdout.split(): break
            time.sleep(0.1)
        else: self.skipTest("could not make a process hold the lock file")
        with self.assertRaises(project.Held) as refused: self.clear()
        self.assertIn(holder.pid, refused.exception.pids)
        self.assertIn(str(holder.pid), str(refused.exception))
        self.assertTrue(self.lock.exists())

    def test_an_lsof_that_cannot_answer_is_not_proof_and_the_lock_stays(self):
        # It fails as a denied one does: an error and exit 1, which is also its status for "no holders".
        (self.folder / "bin").mkdir()
        host_isolation.executable(
            self.folder / "bin/lsof",
            '#!/bin/sh\necho "lsof: cannot read process table: Operation not permitted" >&2\nexit 1\n')
        self.lock.touch()
        with patch.dict(os.environ, {"PATH": f"{self.folder / 'bin'}:{os.environ['PATH']}"}):
            self.assertEqual(self.clear(), project.KEPT)
        self.assertTrue(self.lock.exists())
        self.assertIn("could not ask lsof", self.said[0])
        self.assertIn("Operation not permitted", self.said[0])

    def test_no_lock_is_no_notice(self):
        self.assertEqual(self.clear(), project.ABSENT)
        self.assertEqual(self.said, [])

    def test_a_lock_whose_path_uses_a_symlink_is_kept_with_what_it_points_to(self):
        other = self.folder / "other-lock"
        other.touch()
        self.lock.symlink_to(other)
        self.assertEqual(self.clear(), project.KEPT)
        self.assertTrue(self.lock.is_symlink() and other.exists())
        self.assertIn("symlink", self.said[0])


class ProcessTests(unittest.TestCase):
    FOUND = [(11, "/Applications/Unity/Hub/Editor/6/Unity.app/Contents/MacOS/Unity -projectPath /w/one/lab"),
             (12, "/Applications/Unity/Hub/Editor/6/Unity.app/Contents/MacOS/Unity -batchMode -projectPath /w/one/lab -x"),
             (13, "/Applications/Unity/Hub/Editor/6/Unity.app/Contents/MacOS/Unity -projectPath /w/two/lab")]

    def test_editors_are_matched_by_their_own_project_path_and_a_batch_run_is_not_a_window(self):
        mine = project.on("/w/one/lab", self.FOUND)
        self.assertEqual([pid for pid, _ in mine], [11, 12])
        self.assertEqual([pid for pid, _ in project.interactive(mine)], [11])

    def test_a_process_list_that_cannot_be_read_is_not_an_empty_one(self):
        denied = subprocess.CompletedProcess([], 3, "", "pgrep: Cannot get process list")
        with patch.object(subprocess, "run", return_value=denied), self.assertRaises(project.Unreadable):
            project.editors()
        none = subprocess.CompletedProcess([], 1, "", "")
        listed = subprocess.CompletedProcess([], 0, "41 /x/Unity.app/Contents/MacOS/Unity -projectPath /p\n", "")
        with patch.object(subprocess, "run", side_effect=[none, listed]):
            self.assertEqual(project.editors(), [])
            self.assertEqual(project.editors(), [(41, "/x/Unity.app/Contents/MacOS/Unity -projectPath /p")])


class LicensingTests(unittest.TestCase):
    def repair(self, editors, helpers, alive=()):
        """What repair said and signaled, with the Mac's editors, helpers and the helpers that stay alive."""
        said, signaled = [], []
        with patch.object(project, "editors", return_value=editors), \
                patch.object(project, "pgrep", return_value=[(pid, "helper") for pid in helpers]), \
                patch.object(os, "kill", side_effect=lambda pid, sent: signaled.append((pid, sent))), \
                patch.object(licensing, "alive", side_effect=lambda pid: pid in alive):
            stopped = licensing.repair(say=said.append, pause=lambda seconds: None)
        return stopped, said, signaled

    def test_an_orphaned_helper_is_asked_to_stop_and_never_killed(self):
        import signal
        stopped, said, signaled = self.repair([], [71])
        self.assertEqual((stopped, signaled), ([71], [(71, signal.SIGTERM)]))
        self.assertIn("pid=71", said[0])

    def test_a_helper_is_left_alone_while_any_editor_runs_and_nothing_is_done_without_one(self):
        self.assertEqual(self.repair([(5, "Unity")], [71]), ([], [], []))
        self.assertEqual(self.repair([], []), ([], [], []))

    def test_a_helper_that_stays_or_a_list_that_cannot_be_read_stops_the_run(self):
        with self.assertRaisesRegex(licensing.Unrepaired, "not force-killed"): self.repair([], [71], alive=[71])
        with patch.object(project, "editors", side_effect=project.Unreadable("cannot inspect processes")), \
                self.assertRaisesRegex(licensing.Unrepaired, "no licensing repair attempted"):
            licensing.repair(say=lambda line: None)

    def test_a_log_says_when_licensing_never_started(self):
        self.assertTrue(licensing.never_started("x\nFound 0 entitlement groups\n"))
        self.assertTrue(licensing.never_started("attempt to write a readonly database"))
        self.assertFalse(licensing.never_started("Licensing initialized"))


class BatchTests(Folder):
    def run_batch(self, script, quiet):
        said = []
        log = self.folder / "run.log"
        with patch.object(project, "editors", return_value=[]):
            status = batch.run([sys.executable, "-c", script, str(log)], log, self.folder, quiet=quiet, poll=0.1,
                               grace=2, say=said.append)
        return status, said

    def test_a_run_that_ends_gives_its_own_status(self):
        self.assertEqual(self.run_batch("import sys; sys.exit(7)", 30), (7, []))
        self.assertEqual(self.run_batch("pass", 30), (0, []))

    def test_a_run_whose_log_goes_silent_is_ended_and_one_that_keeps_writing_is_not(self):
        status, said = self.run_batch("import time; time.sleep(60)", 0.3)
        self.assertEqual(status, batch.HUNG)
        self.assertIn("treating it as hung", said[0])
        writing = ("import sys, time\nfor _ in range(8):\n    open(sys.argv[1], 'a').write('.')\n    time.sleep(0.15)\n")
        self.assertEqual(self.run_batch(writing, 0.8), (0, []))

    def test_the_silence_allowed_is_ten_minutes_unless_the_variable_says(self):
        self.assertEqual(batch.quiet({}), 600)
        self.assertEqual(batch.quiet({"QUEUEKEEPER_UNITY_QUIET": "90"}), 90)
        self.assertEqual(batch.quiet({"QUEUEKEEPER_UNITY_QUIET": "soon"}), 600)


class SettingsTests(unittest.TestCase):
    BASE = {"schemaVersion": 1, "name": "Project", "extensions": ["queuekeeper.unity"]}

    def parse(self, **changes):
        return config.parse(json.dumps({**self.BASE, **changes}))

    def test_a_project_names_its_unity_project_and_layout(self):
        found = self.parse(unity={"project": "lab", "layout": "tools/layout.dwlt"})
        self.assertEqual(settings.project("/r", found), Path("/r/lab"))
        self.assertEqual(settings.layout("/r", found), Path("/r/tools/layout.dwlt"))
        self.assertEqual(settings.project("/r", self.parse()), Path("/r"))
        self.assertIsNone(settings.layout("/r", self.parse()))

    def test_a_path_outside_the_checkout_or_an_unknown_key_is_refused(self):
        for value in ({"project": "../lab"}, {"project": "/lab"}, {"project": ""}, {"project": 1}, {"editor": "x"}, "lab"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "unity"):
                self.parse(unity=value)

    def test_the_key_without_the_extension_is_refused_naming_it(self):
        with patch.dict(extensions.CONFIG_KEYS, {}, clear=True), patch.object(extensions, "LOADED", ["queuekeeper.unity"]), \
                self.assertRaisesRegex(ValueError, "unity is the queuekeeper.unity extension's key"):
            self.parse(extensions=[], unity={"project": "lab"})

    def test_the_extension_requires_the_desktop_extension_and_adds_its_command(self):
        self.assertEqual(unity.REQUIRES, ("queuekeeper.mac_desktop",))
        self.assertIs(extensions.COMMANDS["unity"][1], commands.main)

    def test_the_schema_lists_the_key_s_own_keys(self):
        schema = json.loads((config.TOOLKIT_ROOT / "schemas/queuekeeper.schema.json").read_text())
        self.assertEqual(set(schema["properties"]["unity"]["properties"]), settings.KEYS)


RESULTS = """<?xml version="1.0"?>
<test-run total="3" passed="{passed}" failed="{failed}" skipped="1" inconclusive="0">
  <test-case fullname="Lab.Grid.Wraps" result="Passed"/>
  <test-case fullname="Lab.Grid.Skips" result="Skipped"/>
  <test-case fullname="Lab.Grid.Clamps" result="{result}"><failure><message>  Expected: 4
  But was: 5</message></failure></test-case>
</test-run>
"""


class CommandTests(Folder):
    def setUp(self):
        super().setUp()
        self.project = self.unity_project()
        self.root = self.project.parent
        (self.root / "queuekeeper.json").write_text(json.dumps({
            "schemaVersion": 1, "name": "Project", "extensions": ["queuekeeper.unity"],
            "unity": {"project": "lab", "layout": "layout.dwlt"}}))
        (self.root / "layout.dwlt").write_text("layout")
        self.binary = fake_editor(self.folder / "hub", "6000.6.0f1")
        self.editors = []
        self.launched = []
        self.signaled = []
        for target, name, stand_in in (
                (editor, "HUB", self.folder / "hub"),
                (project, "editors", lambda: list(self.editors)),
                (licensing, "repair", lambda say=None: []),
                (editor, "launch", lambda binary, found: self.launched.append((binary, found))),
                (os, "kill", self.kill)):
            self.enterContext(patch.object(target, name, stand_in))
        self.enterContext(patch.dict(os.environ))
        for variable in ("UNITY", commands.OPEN_VARIABLE, batch.VARIABLE): os.environ.pop(variable, None)

    def kill(self, pid, sent):
        self.signaled.append(pid)
        self.editors = [found for found in self.editors if found[0] != pid]

    def window(self, pid=31):
        return (pid, f"{self.binary} -projectPath {self.project}")

    def unity(self, *arguments):
        """(status, what it printed, what it warned of) for `queuekeeper unity ARGUMENTS` in the project."""
        output, error = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            status = commands.main(list(arguments), str(self.root))
        return status, output.getvalue(), error.getvalue()

    def editor_writes(self, results=None, log=""):
        """Make the batch run a stand-in that writes the editor's log and, when given, its results."""
        def ran(command, log_path, found, **options):
            self.assertEqual(command[:4], [str(self.binary), "-batchmode", "-projectPath", str(self.project)])
            self.assertNotIn("-nographics", command)
            Path(log_path).write_text(log)
            if results: Path(command[command.index("-testResults") + 1]).write_text(results)
            return 0
        self.enterContext(patch.object(batch, "run", ran))

    def test_test_reports_the_counts_a_lane_reads_and_passes(self):
        self.editor_writes(RESULTS.format(passed=2, failed=0, result="Passed"))
        status, output, _ = self.unity("test", "--results", str(self.folder / "out"))
        self.assertEqual(status, 0)
        self.assertIn("2/3 passed, 0 failed", output)
        self.assertEqual(executed_tests(output), 2)
        self.assertTrue((self.folder / "out/tests.log").exists())

    def test_test_fails_naming_each_test_that_failed_with_its_first_line(self):
        self.editor_writes(RESULTS.format(passed=1, failed=1, result="Failed"))
        status, output, _ = self.unity("test", "--results", str(self.folder / "out"))
        self.assertEqual(status, 1)
        self.assertIn("Failed: Lab.Grid.Clamps", output)
        self.assertIn("Expected: 4", output)
        self.assertNotIn("Lab.Grid.Skips", output)

    def test_test_fails_on_compile_errors_and_on_no_results(self):
        self.editor_writes(log="Assets/A.cs(3,1): error CS0103: The name 'x' does not exist\n" * 2)
        status, _, error = self.unity("test", "--results", str(self.folder / "out"))
        self.assertEqual(status, 1)
        self.assertIn("1 compile error(s)", error)
        self.editor_writes(log="Something else broke\n")
        status, _, error = self.unity("test", "--results", str(self.folder / "out"))
        self.assertEqual(status, 1)
        self.assertIn("no test results were written", error)

    def test_test_is_unmeasured_when_licensing_never_started_or_the_run_hung(self):
        self.editor_writes(log="Found 0 entitlement groups\n")
        status, _, error = self.unity("test", "--results", str(self.folder / "out"))
        self.assertEqual(status, UNMEASURED)
        self.assertIn("licensing did not initialize", error)
        self.enterContext(patch.object(batch, "run", lambda *a, **k: batch.HUNG))
        status, _, error = self.unity("test", "--results", str(self.folder / "out"))
        self.assertEqual(status, UNMEASURED)
        self.assertIn("hung", error)

    def test_test_starts_nothing_while_an_editor_has_the_project_or_this_mac_has_no_editor(self):
        self.enterContext(patch.object(batch, "run", lambda *a, **k: self.fail("a batch run started")))
        self.editors = [self.window()]
        status, _, error = self.unity("test")
        self.assertEqual(status, UNMEASURED)
        self.assertIn("queuekeeper unity closed --", error)
        self.editors = [(32, f"{self.binary} -projectPath {self.folder}/other/lab")]
        with patch.object(editor, "HUB", self.folder / "no-hub"):
            status, _, error = self.unity("test")
        self.assertEqual(status, UNMEASURED)
        self.assertIn("no Unity editor", error)

    def test_closed_closes_the_editor_runs_the_command_and_opens_the_editor_again(self):
        self.editors = [self.window(), (33, f"{self.binary} -projectPath {self.folder}/other/lab")]
        ran = self.folder / "ran"
        status, output, _ = self.unity("closed", "--", "sh", "-c", f"touch '{ran}'; exit 5")
        self.assertEqual((status, self.signaled), (5, [31]))
        self.assertTrue(ran.exists())
        self.assertEqual(self.launched, [(self.binary, self.project)])
        self.assertIn("command exit=5", output)

    def test_closed_with_no_editor_open_runs_the_command_without_a_stale_lock_and_opens_nothing(self):
        (self.project / project.LOCK).touch()
        seen = self.folder / "seen"
        probe = f"if [ -e '{self.project / project.LOCK}' ]; then echo present; else echo absent; fi > '{seen}'"
        status, output, error = self.unity("closed", "--", "sh", "-c", probe)
        self.assertEqual((status, seen.read_text().strip(), self.launched, self.signaled), (0, "absent", [], []))
        self.assertIn("running directly", output)
        self.assertIn("cleared stale", error)

    def test_closed_runs_nothing_when_the_editor_stays_or_the_lock_is_held(self):
        ran = self.folder / "ran"
        self.editors = [self.window()]
        with patch.object(os, "kill", lambda pid, sent: None), patch.object(commands, "CLOSE_WAIT", 4), \
                patch.object(commands.time, "sleep", lambda seconds: None):
            status, _, error = self.unity("closed", "--", "touch", str(ran))
        self.assertEqual((status, ran.exists(), self.launched), (UNMEASURED, False, []))
        self.assertIn("not force-killed", error)
        held = project.Held(self.project / project.LOCK, [99])
        with patch.object(project, "clear_stale_lock", side_effect=held):
            status, _, error = self.unity("closed", "--", "touch", str(ran))
        self.assertEqual((status, ran.exists(), self.launched), (UNMEASURED, False, []))
        self.assertIn("pid 31) was not opened again", error)

    def test_closed_takes_its_command_after_the_dashes_and_only_it_does(self):
        for arguments in (["closed"], ["closed", "--"], ["test", "--", "true"]):
            with self.subTest(arguments=arguments), self.assertRaises(SystemExit), \
                    contextlib.redirect_stderr(io.StringIO()):
                commands.main(arguments, str(self.root))

    def test_ready_becomes_the_command_once_a_batch_run_may_start(self):
        became = []
        self.enterContext(patch.object(commands, "replace", became.append))
        self.assertIsNone(self.unity("ready", "--", "check.sh", "--gates")[0])
        self.assertEqual(became, [["check.sh", "--gates"]])

    def test_ready_runs_nothing_while_an_editor_has_the_project_or_its_lock_is_held(self):
        self.enterContext(patch.object(commands, "replace", lambda command: self.fail("the command ran")))
        self.editors = [self.window()]
        status, _, error = self.unity("ready", "--", "check.sh")
        self.assertEqual(status, UNMEASURED)
        self.assertIn("Unity is running on", error)
        self.editors = []
        with patch.object(project, "clear_stale_lock", side_effect=project.Held(self.project / project.LOCK, [99])):
            self.assertEqual(self.unity("ready", "--", "check.sh")[0], UNMEASURED)
        with patch.object(licensing, "repair", side_effect=licensing.Unrepaired("the helper stayed")):
            self.assertEqual(self.unity("ready", "--", "check.sh")[0], UNMEASURED)

    def worktree(self, linked=True):
        own = self.folder / ("git/worktrees/w" if linked else "git")
        own.mkdir(parents=True, exist_ok=True)
        self.enterContext(patch.object(commands, "git_directories", lambda root: (own, self.folder / "git")))
        return own / commands.MARKER

    def test_open_opens_a_linked_worktree_s_editor_once_with_the_project_s_layout(self):
        marker = self.worktree()
        status, output, _ = self.unity("open")
        self.assertEqual((status, self.launched), (0, [(self.binary, self.project)]))
        self.assertEqual((self.project / "UserSettings/Layouts/default-6000.dwlt").read_text(), "layout")
        self.assertIn("result=launched", marker.read_text())
        status, output, _ = self.unity("open")
        self.assertEqual((status, len(self.launched)), (0, 1))
        self.assertIn("already handled", output)
        self.unity("open", "--ensure")
        self.assertEqual(len(self.launched), 2)

    def test_open_keeps_a_layout_the_worktree_already_has(self):
        self.worktree()
        own = self.project / "UserSettings/Layouts/default-6000.dwlt"
        own.parent.mkdir(parents=True)
        own.write_text("mine")
        self.unity("open")
        self.assertEqual(own.read_text(), "mine")

    def test_open_does_nothing_in_the_main_checkout_or_for_a_session_that_declines(self):
        marker = self.worktree(linked=False)
        self.assertEqual(self.unity("open")[0], 0)
        marker = self.worktree()
        os.environ[commands.OPEN_VARIABLE] = "0"
        status, output, _ = self.unity("open")
        self.assertEqual((status, self.launched, marker.exists()), (0, [], False))
        self.assertIn("QUEUEKEEPER_UNITY_OPEN=0", output)

    def test_open_finds_an_editor_already_open_and_status_only_looks(self):
        marker = self.worktree()
        self.assertEqual(self.unity("open", "--status")[0], 1)
        self.editors = [self.window()]
        self.assertEqual(self.unity("open", "--status")[0], 0)
        self.assertEqual((self.launched, marker.exists()), ([], False))
        self.assertEqual(self.unity("open")[0], 0)
        self.assertEqual(self.launched, [])
        self.assertIn("result=already-open", marker.read_text())

    def test_open_starts_no_editor_when_the_processes_cannot_be_read(self):
        self.worktree()
        with patch.object(project, "editors", side_effect=project.Unreadable("cannot inspect processes")):
            status, _, error = self.unity("open")
        self.assertEqual((status, self.launched), (UNMEASURED, []))
        self.assertIn("no editor was opened", error)

    def test_a_project_with_no_unity_project_where_it_says_is_told_the_key(self):
        (self.root / "queuekeeper.json").write_text(json.dumps({
            "schemaVersion": 1, "name": "Project", "extensions": ["queuekeeper.unity"]}))
        status, _, error = self.unity("test")
        self.assertEqual(status, 2)
        self.assertIn('"unity": {"project": "lab"}', error)


class DoctorTests(Folder):
    def setUp(self):
        super().setUp()
        self.project = self.unity_project()
        self.config = {"unity": {"project": "lab"}, "lanes": {"gate": {"command": ["qk", "unity", "test"]}}}
        self.enterContext(patch.object(editor, "HUB", self.folder / "hub"))
        self.there = True
        self.enterContext(patch.object(doctor, "second_mac", lambda: "studio"))
        self.enterContext(patch.object(doctor, "installed_there", lambda host, binary: self.there))

    def warnings(self):
        return doctor.warnings(self.project.parent, self.config)

    def test_a_project_whose_pinned_editor_both_macs_have_is_warned_of_nothing(self):
        fake_editor(self.folder / "hub", "6000.6.0f1")
        self.assertEqual(self.warnings(), [])
        self.assertIn(doctor.warnings, extensions.DOCTOR)

    def test_a_mac_without_the_pinned_editor_is_named(self):
        fake_editor(self.folder / "hub", "6000.6.1f1")
        self.there = False
        here, there = self.warnings()
        self.assertIn("Unity 6000.6.0f1, which lab pins, is not installed on this Mac", here)
        self.assertIn("is not installed on studio", there)

    def test_a_second_mac_that_could_not_be_asked_is_not_warned_of(self):
        fake_editor(self.folder / "hub", "6000.6.0f1")
        self.there = None
        self.assertEqual(self.warnings(), [])

    def test_a_lane_that_runs_without_a_graphics_device_is_named(self):
        fake_editor(self.folder / "hub", "6000.6.0f1")
        self.config["lanes"]["fast"] = {"command": ["Unity", "-batchmode", "-nographics"]}
        self.assertEqual(len(self.warnings()), 1)
        self.assertIn("fast passes -nographics", self.warnings()[0])

    def test_a_missing_unity_project_or_pin_is_said(self):
        (self.project / "ProjectSettings/ProjectVersion.txt").write_text("")
        self.assertIn("pins no editor version", self.warnings()[0])
        self.config["unity"] = {"project": "elsewhere"}
        self.assertIn("there is no Unity project at", self.warnings()[0])


class DiskTests(Folder):
    NOW = 2_000_000_000

    def setUp(self):
        super().setUp()
        self.development = self.folder / "Development"
        self.main = self.development / "Game"
        self.lab = self.checkout(self.main)
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.main)], check=True)
        (self.main / "queuekeeper.json").write_text(json.dumps({
            "schemaVersion": 1, "name": "Game", "extensions": ["queuekeeper.unity"], "unity": {"project": "lab"}}))

    def checkout(self, root):
        lab = root / "lab"
        for name in ("ProjectSettings", "Library", "Temp", "Logs"): (lab / name).mkdir(parents=True)
        for path in lab.rglob("*"): os.utime(path, (self.NOW - 10 * 86400,) * 2)
        os.utime(lab, (self.NOW - 10 * 86400,) * 2)
        return lab

    def items(self, processes=(), worktrees=()):
        host = disk.Host(home=self.folder, development=self.development, now=self.NOW, min_idle=86400,
                         processes=list(processes))
        listed = [{"worktree": str(self.main)}, *({"worktree": str(root)} for root in worktrees)]
        with patch.object(unity_disk.git_state, "worktrees", lambda checkout: listed):
            found = unity_disk.items(host, disk.Usage(host.processes), disk.Sizes(measure=False), {unity_disk.CATEGORY})
        return {item.path: item for item in found}

    def test_each_checkout_s_generated_folders_are_candidates_once_idle(self):
        other = self.folder / "worktrees/w"
        linked = self.checkout(other)
        found = self.items(worktrees=[other])
        self.assertEqual(set(found), {str(lab / name) for lab in (self.lab, linked) for name in ("Library", "Temp", "Logs")})
        library = found[str(self.lab / "Library")]
        self.assertEqual((library.status, library.command), (disk.CANDIDATE, ["rm", "-rf", "--", str(self.lab / "Library")]))
        self.assertIn("rebuilds on its next open", library.reason)

    def test_a_project_an_editor_has_open_keeps_its_folders_and_another_s_editor_does_not_count(self):
        mine = disk.Process(41, "Unity", args=f"/x/Unity.app/Contents/MacOS/Unity -projectPath {self.lab}")
        self.assertEqual({item.status for item in self.items([mine]).values()}, {disk.KEPT})
        self.assertIn("PID 41 has the project open", self.items([mine])[str(self.lab / "Library")].reason)
        theirs = disk.Process(42, "Unity", args="/x/Unity.app/Contents/MacOS/Unity -projectPath /elsewhere/lab")
        self.assertEqual({item.status for item in self.items([theirs]).values()}, {disk.CANDIDATE})

    def test_a_folder_changed_lately_is_kept(self):
        os.utime(self.lab / "Library", (self.NOW - 60,) * 2)
        found = self.items()
        self.assertEqual(found[str(self.lab / "Library")].status, disk.KEPT)
        self.assertEqual(found[str(self.lab / "Temp")].status, disk.CANDIDATE)

    def test_a_project_that_does_not_switch_the_extension_on_lists_nothing(self):
        (self.main / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Game"}))
        self.assertEqual(self.items(), {})
        self.assertEqual(unity_disk.items(None, None, None, {"simulator"}), [])
        self.assertIn(unity_disk.CATEGORY, disk.CATEGORIES)


class LaneKeyTests(unittest.TestCase):
    BASE = {"schemaVersion": 1, "name": "Project"}

    def parse(self, value=True, listed=("queuekeeper.unity",)):
        lanes = {"gate": {"command": ["./check.sh"], "resource": "heavy", "unity": value}}
        return config.parse(json.dumps({**self.BASE, "extensions": list(listed), "lanes": lanes}))["lanes"]["gate"]

    def test_a_lane_that_says_unity_runs_under_the_readiness_check_and_no_other_does(self):
        gate = self.parse()
        wrapped = extensions.lane_command(gate, gate["command"])
        self.assertEqual(wrapped[1:], [str(config.TOOLKIT_ROOT / "bin/qk"), "unity", "ready", "--", "./check.sh"])
        self.assertTrue((config.TOOLKIT_ROOT / "bin/qk").exists())
        self.assertEqual(lane.UnityLane().command({"unity": False}, ["./check.sh"]), ["./check.sh"])
        self.assertEqual(extensions.lane_command({}, ["./check.sh"]), ["./check.sh"])
        self.assertFalse(extensions.reads_screen(gate))

    def test_the_key_takes_true_or_false_and_is_refused_by_name_without_the_extension(self):
        with self.assertRaisesRegex(ValueError, "gate.unity must be true or false"): self.parse("yes")
        with patch.dict(extensions.LANE_KEYS, {}, clear=True), patch.object(extensions, "LOADED", ["queuekeeper.unity"]), \
                self.assertRaisesRegex(ValueError, "gate.unity is the queuekeeper.unity extension's key"):
            self.parse(listed=())


class HostReadTests(Folder):
    HUB = "/Applications/Unity/Hub/Editor/6000.6.0f1/Unity.app/Contents/MacOS/Unity"

    def read(self, editors, json_output=False):
        import argparse
        lines = []
        for version in ("6000.10.0f1", "6000.6.0f1"):
            if not (self.folder / "hub" / version).exists(): fake_editor(self.folder / "hub", version)
        with patch.object(editor, "HUB", self.folder / "hub"), patch.object(project, "editors", editors):
            status = host.read(argparse.Namespace(json=json_output), out=lines.append)
        return status, lines

    def test_it_lists_each_editor_with_its_project_and_the_versions_installed(self):
        running = [(11, f"{self.HUB} -projectPath /w/one/lab"),
                   (12, f"{self.HUB} -batchMode -projectPath /w/My Game/lab -runTests -testPlatform EditMode")]
        status, lines = self.read(lambda: running)
        self.assertEqual(status, 0)
        self.assertEqual(lines, ["installed: 6000.6.0f1, 6000.10.0f1",
                                 "PID 11: editor, Unity 6000.6.0f1, on /w/one/lab",
                                 "PID 12: batch run, Unity 6000.6.0f1, on /w/My Game/lab"])
        found = json.loads(self.read(lambda: running, json_output=True)[1][0])
        self.assertEqual(found["editors"][1], {"pid": 12, "project": "/w/My Game/lab", "version": "6000.6.0f1", "batch": True})

    def test_it_says_when_none_runs_and_when_the_processes_cannot_be_read(self):
        self.assertEqual(self.read(lambda: [])[1][1], "no Unity editor is running")
        def denied(): raise project.Unreadable("cannot inspect processes")
        status, lines = self.read(denied)
        self.assertEqual((status, lines), (2, ["queuekeeper: cannot inspect processes"]))
        self.assertIs(extensions.HOST_READS["editors"][2], host.read)


if __name__ == "__main__":
    unittest.main()
