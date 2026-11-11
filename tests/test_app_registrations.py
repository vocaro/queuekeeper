"""What a run built leaves LaunchServices and pluginkit when the run ends (#87), over stub lsregister and
pluginkit scripts that log their arguments, and a stub xcodebuild, so no test registers anything on this Mac."""
from contextlib import redirect_stderr
from io import StringIO
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
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/python"))
import host_isolation
from host_isolation import stretched
from vocaro_engineering import app_registrations as registrations, cli, processes

SOURCE = Path(__file__).resolve().parents[1] / "src/python"


def executable(path, text):
    return host_isolation.executable(path, text)


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.slots = self.base / "slots"
        self.log = self.base / "tools.log"
        self.lsregister = executable(self.base / "lsregister", f'#!/bin/sh\necho "lsregister $*" >> "{self.log}"\n')
        self.pluginkit = executable(self.base / "pluginkit", f'#!/bin/sh\necho "pluginkit $*" >> "{self.log}"\n')
        self.tools = self.base / "bin"
        self.tools.mkdir()
        # ps lists it by the name a run is watched for, not its shell's. It holds for STUB_HOLD seconds,
        # or until its run's journal exists, however busy the host.
        executable(self.tools / "xcodebuild", """#!/bin/bash
exec -a xcodebuild /bin/sh -c '
n=0
if [ -n "$STUB_HOLD" ]; then sleep "$STUB_HOLD"
else until ls "$HEAVY_SLOT_DIR"/app-registrations/*.json >/dev/null 2>&1 || [ $n -ge 1200 ]; do sleep 0.1; n=$((n+1)); done
fi
exit "${STUB_STATUS:-0}"' "$@"
""")
        # The real ps, over this test's own processes: every process these tests start descends from it. Reading
        # every command line on the Mac took tens of seconds at a gate's start, and once over the 60 s a cleanup
        # gives ps, which left the cleanup to the next run and failed two of these tests (#361, #362). A run that
        # cannot list the processes has a test of its own. Other ps commands run as given.
        executable(self.tools / "ps", """#!/bin/sh
if [ "$1" = "-axww" ] && [ -n "$PS_ROOT" ]; then
  shift
  pids=$(/bin/ps -axo pid=,ppid= | awk -v root="$PS_ROOT" '
    { children[$2] = children[$2] " " $1 }
    END {
      found = root; pending = root
      while (pending != "") {
        count = split(pending, at, " "); pending = ""
        for (i = 1; i <= count; i++) {
          n = split(children[at[i]], below, " ")
          for (j = 1; j <= n; j++) { found = found "," below[j]; pending = pending " " below[j] }
        }
      }
      print found
    }')
  exec /bin/ps -ww -p "$pids" "$@"
fi
exec /bin/ps "$@"
""")
        for patcher in (patch.dict(os.environ, {"HEAVY_SLOT_DIR": str(self.slots), "PS_ROOT": str(os.getpid()),
                                                "PATH": f"{self.tools}:{os.environ['PATH']}"}),
                        patch.object(registrations, "LSREGISTER", str(self.lsregister)),
                        patch.object(registrations, "PLUGINKIT", str(self.pluginkit))):
            patcher.start()
            self.addCleanup(patcher.stop)
        os.environ.pop(registrations.RUN, None)
        self.derived = self.base / "derived"
        self.app = self.make_app(self.derived, "Reader")

    def make_app(self, derived, name):
        app = derived / f"Build/Products/Debug/{name}.app"
        (app / "Contents/PlugIns/Thumbnails.appex/Contents").mkdir(parents=True)
        (app / "Contents/Info.plist").write_text("")
        return app

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def journals(self):
        return sorted((self.slots / registrations.JOURNALS).glob("*.json"))

    def run_command(self, command, **options):
        """Run one command as eng does; its status, or the exception that ended it."""
        stderr = StringIO()
        with redirect_stderr(stderr):
            try:
                with registrations.around_run([command], self.base):
                    return processes.supervise(command, cwd=self.base, stdout=subprocess.DEVNULL, **options)
            except (processes.TimedOut, processes.Interrupted) as error:
                return error
            finally:
                self.said = stderr.getvalue()

    def build_command(self, derived=None):
        return ["xcodebuild", "build", "-derivedDataPath", str(derived or self.derived)]

    def expected(self, app=None):
        app = app or self.app
        return [f"pluginkit -r {app / 'Contents/PlugIns/Thumbnails.appex'}", f"lsregister -u {app}"]

    def assert_settled(self):
        self.assertCountEqual(self.calls(), self.expected())
        self.assertEqual(self.journals(), [])

    def wait_for(self, seen, seconds=15):
        end = time.monotonic() + stretched(seconds)
        while not seen():
            self.assertLess(time.monotonic(), end, "never happened")
            time.sleep(0.05)

    def child(self, body):
        """A process that runs body with the stub tools, in another run's place."""
        return textwrap.dedent(f"""\
            import subprocess, sys, time
            sys.path.insert(0, {str(SOURCE)!r})
            from pathlib import Path
            from vocaro_engineering import app_registrations as r, processes
            r.LSREGISTER, r.PLUGINKIT = {str(self.lsregister)!r}, {str(self.pluginkit)!r}
            command, base, log = {self.build_command()!r}, Path({str(self.base)!r}), Path({str(self.log)!r})
        """) + textwrap.dedent(body)

    def test_a_passing_run_unregisters_what_it_built(self):
        self.assertEqual(self.run_command(self.build_command()), 0)
        self.assert_settled()

    def test_a_failing_run_unregisters_what_it_built(self):
        with patch.dict(os.environ, STUB_STATUS="65"):
            self.assertEqual(self.run_command(self.build_command()), 65)
        self.assert_settled()

    def test_a_run_past_its_limit_unregisters_what_it_built(self):
        with patch.dict(os.environ, STUB_HOLD="30"):
            # Fixed: the 1 s limit is the behavior under test, and the 30 s hold outlasts it.
            self.assertIsInstance(self.run_command(self.build_command(), timeout=1), processes.TimedOut)
        self.assert_settled()

    def test_a_stopped_run_unregisters_what_it_built_and_nothing_before_it_ends(self):
        script = self.child("""
            with processes.forwarding_signals():
                with r.around_run([command], base):
                    processes.supervise(command, cwd=base)
        """)
        with patch.dict(os.environ, STUB_HOLD=str(stretched(60))):  # it outlives the wait for its stop
            run = subprocess.Popen([sys.executable, "-c", script], start_new_session=True, stderr=subprocess.DEVNULL)
            self.addCleanup(run.wait)
            self.addCleanup(lambda: run.poll() is None and run.kill())
            self.wait_for(self.journals)
            self.assertEqual(self.calls(), [], "a run in progress keeps its registrations")
            run.send_signal(signal.SIGTERM)
            run.wait(stretched(30))
        self.assert_settled()

    def test_a_run_in_progress_keeps_its_registrations_until_it_ends(self):
        # Give the sweep a successful census with no other process using the build folder.
        start = "Thu Jan 1 00:00:00 1970"
        listing = f"{os.getpid()} {os.getppid()} {os.getuid()} {start} python3\n"
        with patch.object(registrations, "process_start", return_value=start), \
                patch.object(registrations.offload_host, "listing", return_value=listing):
            with registrations.around_run([self.build_command()], self.base):
                self.assertEqual(self.calls(), [])
                self.assertEqual(len(self.journals()), 1)
        self.assert_settled()

    def test_a_folder_a_script_names_is_found_by_watching_its_descendants(self):
        script = executable(self.base / "lane.sh", f'#!/bin/sh\nxcodebuild test -derivedDataPath "{self.derived}" '
                                                   "-only-testing:Reader CODE_SIGN_IDENTITY=-\n")
        self.assertEqual(self.run_command([str(script)]), 0)
        self.assert_settled()

    def test_a_relative_folder_is_read_against_the_builds_own_directory(self):
        elsewhere = self.base / "elsewhere"
        app = self.make_app(elsewhere / "out", "Other")
        script = executable(self.base / "lane.sh", f'#!/bin/sh\ncd "{elsewhere}"\nxcodebuild test -derivedDataPath out\n')
        self.assertEqual(self.run_command([str(script)]), 0)
        self.assertCountEqual(self.calls(), self.expected(app))

    def test_a_path_with_spaces_is_read_whole(self):
        derived = self.base / "with a space"
        app = self.make_app(derived, "Spaced")
        script = executable(self.base / "lane.sh", f'#!/bin/sh\nxcodebuild -derivedDataPath "{derived}" -jobs 2 test\n')
        self.assertEqual(self.run_command([str(script)]), 0)
        self.assertCountEqual(self.calls(), self.expected(app))

    def test_a_run_without_xcodebuild_unregisters_nothing_and_keeps_no_journal(self):
        self.assertEqual(self.run_command(["/bin/sh", "-c", f"sleep 1; echo -derivedDataPath {self.derived}"]), 0)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.journals(), [])

    def test_nothing_outside_its_folder_is_touched(self):
        # A neighbor whose name starts the same, and links in the folder to an app elsewhere.
        neighbor = self.make_app(self.base / "derived2", "Neighbor")
        outside = self.make_app(self.base / "outside", "Outside")
        (self.derived / "Build/Products/Debug/Linked.app").symlink_to(outside, target_is_directory=True)
        (self.app / "Contents/PlugIns/Linked.appex").symlink_to(outside / "Contents/PlugIns/Thumbnails.appex")
        self.assertEqual(self.run_command(self.build_command()), 0)
        self.assert_settled()
        self.assertFalse([call for call in self.calls() if str(neighbor) in call or str(outside) in call])

    def test_an_ios_app_is_not_a_mac_app(self):
        (self.derived / "Build/Products/Debug-iphonesimulator/Reader.app").mkdir(parents=True)
        self.assertEqual(self.run_command(self.build_command()), 0)
        self.assert_settled()

    def test_a_path_that_cannot_be_derived_data_is_refused(self):
        home = Path(registrations.pwd.getpwuid(os.getuid()).pw_dir).resolve()
        checkout = self.base / "checkout"
        (checkout / ".git").mkdir(parents=True)
        self.make_app(checkout, "Checked")
        for folder in (home, self.base, checkout, home / "Library/Developer/Xcode/DerivedData"):
            self.assertIsNotNone(registrations.refusal(folder, self.base), folder)
            with patch.dict(os.environ, STUB_HOLD="0.1"):
                self.run_command(self.build_command(folder))
            self.assertIn("will not unregister", self.said)
        self.assertIsNone(registrations.refusal(self.derived, self.base))
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.journals(), [])

    def journal(self, pid, start, folders=None):
        path = self.slots / registrations.JOURNALS / f"{pid}-other.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"pid": pid, "start": start,
                                    "folders": [str(item) for item in folders or [self.derived]]}))
        return path

    def owners_gone(self):
        """Each journal's PID now has another start, as when its run has ended."""
        for path in self.journals():
            path.write_text(json.dumps({**json.loads(path.read_text()), "start": "Thu Jan  1 00:00:00 1970"}))

    def live_process(self, *arguments):
        # It must outlive the test that holds it, however slowly the host runs it (#111).
        process = subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({stretched(60)})", *arguments])
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        self.wait_for(lambda: registrations.process_start(process.pid))
        return process.pid, registrations.process_start(process.pid)

    def test_a_folder_another_live_run_shares_keeps_its_registrations_until_that_run_has_gone(self):
        pid, start = self.live_process()
        self.journal(pid, start)
        self.assertEqual(self.run_command(self.build_command()), 0)
        self.assertEqual(self.calls(), [])
        self.assertIn("left registered", self.said)
        self.assertEqual(len(self.journals()), 2)
        # Both runs have gone, and the next run sweeps both.
        self.owners_gone()
        self.run_command(["/usr/bin/true"])
        self.assertCountEqual(set(self.calls()), self.expected())
        self.assertEqual(self.journals(), [])

    def test_another_live_runs_separate_folder_does_not_hold_this_one_and_is_not_touched(self):
        pid, start = self.live_process()
        theirs = self.make_app(self.base / "theirs", "Theirs")
        other = self.journal(pid, start, [self.base / "theirs"])
        self.assertEqual(self.run_command(self.build_command()), 0)
        self.assertCountEqual(self.calls(), self.expected())
        self.assertFalse([call for call in self.calls() if str(theirs) in call])
        self.assertTrue(other.exists())

    def test_a_process_that_names_the_folder_holds_it(self):
        self.live_process(str(self.app))
        self.assertEqual(self.run_command(self.build_command()), 0)
        self.assertEqual(self.calls(), [])
        self.assertEqual(len(self.journals()), 1)

    def test_a_failed_tool_leaves_the_journal_for_the_next_run_and_keeps_the_runs_status(self):
        executable(self.lsregister, "#!/bin/sh\nexit 3\n")
        self.assertEqual(self.run_command(self.build_command()), 0)
        self.assertIn("lsregister exited 3", self.details())
        self.assertEqual(len(self.journals()), 1)
        executable(self.lsregister, f'#!/bin/sh\necho "lsregister $*" >> "{self.log}"\n')
        # This process stands in for the run, so its journal says the run has gone.
        self.owners_gone()
        self.run_command(["/usr/bin/true"])
        self.assertIn(f"lsregister -u {self.app}", self.calls())
        self.assertEqual(self.journals(), [])

    def details(self):
        """What the file the run's one line names holds."""
        return Path(re.search(r"\(details: (.+)\)$", self.said, re.M).group(1)).read_text()

    def test_many_runs_left_on_the_same_folders_are_said_in_one_line_with_each_folder_once(self):
        # On 2026-11-07 a lane printed 130 blocks for the same few folders, each on its own failing tool (#257).
        executable(self.lsregister, "#!/bin/sh\necho 'failed for a real reason' >&2\nexit 3\n")
        other = self.make_app(self.base / "other", "Other")
        for number in range(1, 6):
            journal = self.journal(number, "Thu Jan  1 00:00:00 1970", [self.derived, self.base / "other"])
            journal.rename(journal.with_name(f"{number}-other.json"))
        self.assertEqual(self.run_command(["/usr/bin/true"]), 0)
        self.assertEqual(len(self.said.splitlines()), 1, self.said)
        self.assertIn("unregistered 10 app(s) and 10 extension(s)", self.said)
        self.assertIn("2 folder(s) left registered for the next sweep (details: ", self.said)
        details = self.details().splitlines()
        self.assertEqual(sorted(line for line in details if not line.startswith(" ")), sorted([str(self.derived), str(other.parents[3])]))
        self.assertEqual(sum("failed for a real reason" in line for line in details), 2)
        self.assertEqual(len(self.journals()), 5, "a tool that failed for another reason keeps its journal")

    def test_a_sweep_lists_the_processes_once_for_all_the_journals_it_settles(self):
        # One listing each made a sweep of five journals list every process six times, each able to fail alone (#362).
        for number in range(1, 6):
            self.journal(number, "Thu Jan  1 00:00:00 1970")
        listings = []
        real = registrations.offload_host.listing
        def listing(command, *arguments, **options):
            if command == registrations.PROCESSES: listings.append(command)
            return real(command, *arguments, **options)
        tally = registrations.Tally(lambda text: None)
        with patch.object(registrations.offload_host, "listing", listing):
            registrations.sweep(tally)
        self.assertEqual(len(listings), 1)
        self.assertEqual(self.journals(), [])
        self.assertEqual((tally.apps, tally.extensions), (5, 5))

    def test_a_cleanup_that_cannot_list_the_processes_keeps_the_journal_for_the_next_run(self):
        # As when ps outlasts its bound: nothing is unregistered while what holds a folder is unknown.
        with patch.object(registrations.offload_host, "listing", return_value=None):
            self.assertEqual(self.run_command(self.build_command()), 0)
        self.assertEqual(self.said, "eng: unregistered nothing: the processes could not be listed; the next run tries again\n")
        self.assertEqual(self.calls(), [])
        self.assertEqual(len(self.journals()), 1)
        self.owners_gone()
        self.run_command(["/usr/bin/true"])
        self.assert_settled()

    def test_a_run_with_nothing_to_say_says_nothing(self):
        self.assertEqual(self.run_command(["/usr/bin/true"]), 0)
        self.assertEqual(self.said, "")

    def test_what_the_system_says_is_not_registered_is_dropped_rather_than_swept_again(self):
        # What lsregister and pluginkit printed for such builds, whose journals every later run retried.
        executable(self.lsregister, "#!/bin/sh\nprintf 'failed to scan %s: -10814\\n from spotlight\\n' \"$2\" >&2\nexit 1\n")
        executable(self.pluginkit, "#!/bin/sh\necho \"remove: no plugin at $2\" >&2\nexit 1\n")
        self.assertEqual(self.run_command(self.build_command()), 0)
        self.assertEqual(self.journals(), [])
        self.assertNotIn("left registered", self.said)
        self.assertNotIn("from spotlight", self.said)

    def test_a_tools_multiline_message_stays_on_the_one_line_of_its_folder_in_the_details(self):
        executable(self.lsregister, "#!/bin/sh\nprintf 'first\\n second\\n' >&2\nexit 1\n")
        self.assertEqual(self.run_command(self.build_command()), 0)
        self.assertIn("lsregister exited 1: first second\n", self.details())

    def test_a_killed_run_stays_registered_until_the_next_run_sweeps_it(self):
        # The run must outlive the test that kills it, however slowly the host runs it (#111).
        script = self.child(f"""
            with r.around_run([command], base):
                print("ready", flush=True)
                time.sleep({stretched(60)})
        """)
        run = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
        self.addCleanup(run.stdout.close)
        self.addCleanup(run.wait)
        self.addCleanup(lambda: run.poll() is None and run.kill())
        self.assertEqual(run.stdout.readline().strip(), "ready")
        # A sweep while it lives leaves it.
        self.run_command(["/usr/bin/true"])
        self.assertEqual(self.calls(), [])
        self.assertEqual(len(self.journals()), 1)
        run.kill()
        run.wait()
        self.assertEqual(self.calls(), [], "SIGKILL runs no cleanup")
        self.assertEqual(len(self.journals()), 1)
        self.run_command(["/usr/bin/true"])
        self.assert_settled()

    def test_a_reused_pid_with_another_start_is_a_run_that_has_gone(self):
        pid, _start = self.live_process()
        self.journal(pid, "Thu Jan  1 00:00:00 1970")
        self.run_command(["/usr/bin/true"])
        self.assert_settled()

    def test_a_nested_run_leaves_the_work_to_the_outermost(self):
        nested = executable(self.base / "lane.py", f"#!{sys.executable}\n" + self.child("""
            with r.around_run([command], base):
                subprocess.run(command)
            sys.exit(7 if log.exists() else 0)
        """))
        self.assertEqual(self.run_command([str(nested)]), 0, "the nested run unregistered when it ended")
        self.assert_settled()

    def test_a_marker_from_no_ancestor_does_not_stop_the_cleanup(self):
        pid, _start = self.live_process()
        with patch.dict(os.environ, {registrations.RUN: str(pid)}):
            self.assertEqual(self.run_command(self.build_command()), 0)
        self.assert_settled()

    def test_a_lane_and_eng_admit_unregister_through_the_command_line(self):
        root = self.base / "project"
        root.mkdir()
        lane = self.build_command()
        (root / "engineering.json").write_text(json.dumps({
            "schemaVersion": 1, "name": "Example", "toolkit": {"path": str(cli.TOOLKIT_ROOT)}, "capabilities": [],
            "lanes": {"mac": {"command": lane, "resource": "light"}}, "exceptions": []}))
        previous = Path.cwd()
        os.chdir(root)
        self.addCleanup(os.chdir, previous)
        with patch.object(cli, "refresh_policy", return_value=None), patch.dict(os.environ, STUB_HOLD="0.1"), \
                redirect_stderr(StringIO()):
            self.assertEqual(cli.main(["--project", str(root), "run", "mac", "--here"]), 0)
            self.assert_settled()
            self.log.unlink()
            self.assertEqual(cli.main(["--project", str(root), "admit", "--", *lane]), 0)
            self.assert_settled()


class ParsingTests(unittest.TestCase):
    def test_the_folder_of_a_command_line_as_ps_prints_it(self):
        read = registrations.named_folders
        self.assertEqual(read("xcodebuild -derivedDataPath /a b/c -jobs 4 test"), ["/a b/c"])
        self.assertEqual(read("xcodebuild test -derivedDataPath out CODE_SIGN_IDENTITY=-"), ["out"])
        self.assertEqual(read("xcodebuild -derivedDataPath out build-for-testing"), ["out"])
        self.assertEqual(read("xcodebuild test-without-building -xctestrun /d/Build/Products/X_macos.xctestrun"), ["/d"])
        self.assertEqual(read("xcodebuild -list"), [])

    def test_only_xcodebuild_itself_counts(self):
        self.assertTrue(registrations.is_xcodebuild("/Applications/Xcode.app/Contents/Developer/usr/bin/xcodebuild test"))
        self.assertTrue(registrations.is_xcodebuild("xcrun xcodebuild test"))
        self.assertFalse(registrations.is_xcodebuild("python3 run.py xcodebuild"))
        self.assertFalse(registrations.is_xcodebuild("grep xcodebuild log"))

    def test_a_build_without_a_folder_is_said(self):
        self.assertTrue(registrations.builds_unnamed("xcodebuild test -scheme S"))
        self.assertFalse(registrations.builds_unnamed("xcodebuild -version"))
        self.assertFalse(registrations.builds_unnamed("xcodebuild test -derivedDataPath out"))


if __name__ == "__main__":
    unittest.main()
