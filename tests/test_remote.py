import contextlib
import fcntl
from io import StringIO
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/python"))
from vocaro_engineering import build_host, cli, config, remote, resources

# Stands in for ssh: the build host is a home directory on this Mac. Like sshd, it runs the
# command in that home through a shell, and git's own ssh transport reaches it the same way.
# FAKE_UNREACHABLE makes it fail as ssh does when the build host's name does not resolve.
FAKE_SSH = """#!/bin/sh
while [ $# -gt 0 ]; do
    case "$1" in
        -o|-p|-i|-l|-F|-J) shift 2 ;;
        -*) shift ;;
        *) break ;;
    esac
done
if [ -n "$FAKE_UNREACHABLE" ]; then
    echo "ssh: Could not resolve hostname $1: nodename nor servname provided, or not known" >&2
    exit 255
fi
shift
cd "$FAKE_HOME" || exit 255
# The build host has its own heavy-work queue.
HOME="$FAKE_HOME" SHELL="$FAKE_SHELL" HEAVY_SLOT_DIR="$FAKE_SLOTS" SW_GATE_LOCK_FILE="$FAKE_GATE" exec /bin/sh -c "$*"
"""
# A login shell that reads no profile, so the test's PATH reaches the build host.
FAKE_SHELL = """#!/bin/sh
[ "$1" = -l ] && shift
exec /bin/sh "$@"
"""


def git(directory, *arguments):
    return subprocess.run(["git", "-C", str(directory), *arguments], check=True, capture_output=True,
                          text=True).stdout.strip()


def commit_all(directory):
    git(directory, "add", "-A")
    git(directory, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-q", "-m", "state")
    return git(directory, "rev-parse", "HEAD")


class RemoteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.home = base / "build-host-home"
        self.home.mkdir()
        self.tools = tools = base / "bin"
        tools.mkdir()
        for name, text in [("ssh", FAKE_SSH), ("login-shell", FAKE_SHELL)]:
            (tools / name).write_text(text)
            (tools / name).chmod(0o755)
        # The build host's Python is the Mac's own when it has one: Python 3.9 without Homebrew.
        system = Path("/usr/bin/python3")
        (tools / "python3").symlink_to(system if sys.platform == "darwin" and system.exists() else sys.executable)
        self.toolkit = self.make_toolkit(base / "vocaro-engineering")
        self.project = base / "example"
        self.project.mkdir()
        python = sys.executable
        lanes = {
            "provision": {"command": [python, "-c", "import pathlib; pathlib.Path('.build').mkdir(exist_ok=True); "
                                      "pathlib.Path('.build/provisioned').touch()"]},
            "check": {"command": [python, "-c", "import os, sys; assert os.path.exists('.build/provisioned'); "
                                  "print('Ran', 2 + len(sys.argv) - 1, 'tests'); print(os.environ['DEVELOPER_DIR'] "
                                  "if 'DEVELOPER_DIR' in os.environ else 'no developer dir')"],
                      "resource": "heavy", "minimumTests": 2},
            "fail": {"command": [python, "-c", "print('Ran 1 tests'); raise SystemExit(3)"]},
            "slow": {"command": [python, "-c", "import os, pathlib, time; "
                                 "pathlib.Path('.build/slow.pid').write_text(str(os.getpid())); time.sleep(60)"],
                     "resource": "heavy"},
            "screen": {"command": [python, "-c", "pass"], "foreground": True},
            "gate": {"command": [python, "-c", "import os; print('Ran 2 tests in', os.getcwd())"],
                     "resource": "heavy", "minimumTests": 2, "buildHost": True},
            "failing": {"command": [python, "-c", "print('Ran 2 tests'); raise SystemExit(5)"],
                        "resource": "heavy", "minimumTests": 2, "buildHost": True},
            "build": {"command": [python, "-c", "print('Ran 1 tests')"], "resource": "heavy"},
            # Asks for a credential only through the store helper, never this Mac's keychain.
            "helpers": {"command": [python, "-c", "import os, subprocess; "
                "env = {**os.environ, 'GIT_TERMINAL_PROMPT': '0'}; "
                "helpers = subprocess.run(['git', 'config', '--get-all', 'credential.helper'], capture_output=True, "
                "text=True, env=env).stdout.splitlines(); print('helpers', helpers); "
                "fill = subprocess.run(['git', 'credential', 'fill'], input='protocol=https\\nhost=example.invalid\\n\\n', "
                "capture_output=True, text=True, env=env).stdout if helpers[-1:] and 'store' in helpers[-1] else ''; "
                "print([line for line in fill.splitlines() if line.startswith('username=')])"]},
        }
        (self.project / "engineering.json").write_text(json.dumps(
            {"schemaVersion": 1, "name": "Example", "lanes": lanes}, indent=2))
        (self.project / ".gitignore").write_text(".build/\n")
        git(base, "init", "-q", str(self.project))
        self.commit = commit_all(self.project)
        self.environment = patch.dict(os.environ, {
            "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}", "FAKE_HOME": str(self.home),
            "FAKE_SHELL": str(tools / "login-shell"), "HEAVY_SLOT_DIR": str(base / "slots"),
            "SW_GATE_LOCK_FILE": str(base / "gate"), "HEAVY_POLL_SECONDS": "0.01", "VOCARO_BUILD_HOST": "fake",
            "FAKE_SLOTS": str(base / "build-host-slots"), "FAKE_GATE": str(base / "build-host-gate")})
        self.environment.start()
        os.environ.pop("VOCARO_RESOURCE_TOKEN", None)
        os.environ.pop("DEVELOPER_DIR", None)
        # A run on the real build host hands its git the credential file through these.
        for key in [key for key in os.environ if key.startswith("GIT_CONFIG_") and key != "GIT_CONFIG_GLOBAL"]:
            del os.environ[key]
        self.patches = [patch.object(remote, "TOOLKIT_ROOT", self.toolkit),
                        patch.object(remote, "developer_directory", return_value=None)]
        for item in self.patches: item.start()

    def tearDown(self):
        for item in self.patches: item.stop()
        self.environment.stop()
        self.temp.cleanup()

    def make_toolkit(self, root):
        """A committed copy of this toolkit, whose origin is itself, as the build host receives it."""
        for part in ["bin", "integrations", "policies", "agents", "src/python"]:
            shutil.copytree(config.TOOLKIT_ROOT / part, root / part, ignore=shutil.ignore_patterns("__pycache__"))
        (root / "engineering.json").write_text(json.dumps({"schemaVersion": 1, "name": "vocaro-engineering",
                                                           "toolkit": {"path": "."}}))
        subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
        commit_all(root)
        git(root, "remote", "add", "origin", str(root))
        return root

    def eng(self, *arguments):
        with contextlib.redirect_stdout(StringIO()) as output, contextlib.redirect_stderr(StringIO()) as error:
            status = cli.main(["--project", str(self.project), *arguments])
        return status, output.getvalue(), error.getvalue()

    def checkout(self, slot):
        return self.home / remote.ROOT / "checkouts/example" / str(slot) / "example"

    def test_a_lane_runs_at_the_commit_on_the_build_host_after_its_provision_lane(self):
        status, output, error = self.eng("remote", "run", "check")
        self.assertEqual(status, 0, output + error)
        self.assertEqual(git(self.checkout(0), "rev-parse", "HEAD"), self.commit)
        self.assertTrue((self.checkout(0) / ".build/provisioned").exists())
        results = self.project / ".build/engineering/remote/check"
        self.assertIn("Ran 2 tests", (results / "run.log").read_text())
        receipt = json.loads((results / "source.json").read_text())
        self.assertEqual((receipt["commit"], receipt["host"], receipt["result"]["exitStatus"]), (self.commit, "fake", 0))
        self.assertEqual(receipt["toolkit"], git(self.toolkit, "rev-parse", "HEAD"))
        # The build host ran its own admission and wrote its own receipt.
        self.assertEqual(receipt["result"]["receipt"]["project"]["revision"], self.commit)
        self.assertIn("Policy source:", output)
        self.assertIn(f"check at {self.commit[:12]}", error)
        # The toolkit checkout sits beside the project's, at the dispatched commit.
        kit = self.checkout(0).parent / "vocaro-engineering"
        self.assertEqual(git(kit, "rev-parse", "HEAD"), git(self.toolkit, "rev-parse", "HEAD"))
        # The pushed refs only carried the commits; the checkouts keep them.
        self.assertEqual(git(self.home / remote.ROOT / "repositories/example.git", "for-each-ref"), "")
        self.assertEqual(self.eng("remote", "passed", "check")[0], 0)

    def test_a_whole_lane_counts_only_for_its_exact_commit_and_a_failure_is_its_status(self):
        self.assertEqual(self.eng("remote", "run", "fail")[0], 3)
        self.assertEqual(self.eng("remote", "passed", "fail")[0], 1)
        self.assertEqual(self.eng("remote", "run", "check", "--", "a")[0], 0)
        self.assertIn("Ran 3 tests", (self.project / ".build/engineering/remote/check/run.log").read_text())
        # A filtered run is part of the lane, not a pass of it.
        self.assertEqual(self.eng("remote", "passed", "check")[0], 1)
        self.assertEqual(self.eng("remote", "run", "check")[0], 0)
        self.assertEqual(self.eng("remote", "passed", "check")[0], 0)
        (self.project / "README.md").write_text("next\n")
        commit_all(self.project)
        self.assertEqual(self.eng("remote", "passed", "check")[0], 1)
        self.assertEqual(self.eng("remote", "passed", "check", "--commit", self.commit)[0], 0)

    def test_a_toolkit_with_uncommitted_changes_and_foreground_lanes_are_refused_before_anything_is_sent(self):
        # Only the project's working tree goes as a snapshot; the toolkit runs at its commit.
        (self.toolkit / "engineering.json").write_text((self.toolkit / "engineering.json").read_text() + "\n")
        for operation in [["run", "check"], ["admit", "--", "true"]]:
            with self.subTest(operation):
                status, _output, error = self.eng("remote", *operation)
                self.assertEqual(status, 2)
                self.assertIn(f"{self.toolkit.resolve()} has changes the build host would not run; commit them first",
                              error)
        git(self.toolkit, "checkout", "--", "engineering.json")
        self.assertIn("foreground lanes run only here", self.eng("remote", "run", "screen")[2])
        self.assertFalse((self.home / remote.ROOT).exists())

    def test_a_different_xcode_on_the_build_host_is_refused(self):
        local = {**build_host.identity(None), "xcode": "Xcode 99.0\nBuild version 99A1"}
        with patch.object(remote, "developer_directory", return_value=self.temp.name), \
                patch.object(remote.build_host, "identity", return_value=local):
            status, _output, error = self.eng("remote", "run", "check")
        self.assertEqual(status, 2)
        self.assertIn("not the dispatcher's Xcode 99.0", error)
        self.assertFalse(self.checkout(0).exists())

    def test_admit_runs_a_command_in_the_checkout_root(self):
        status, output, _error = self.eng("remote", "admit", "--", sys.executable, "-c",
                                          "import os; print('ran in', os.getcwd())")
        self.assertEqual(status, 0)
        self.assertIn(f"ran in {self.checkout(0).resolve()}", output)
        self.assertIn("ran in", (self.project / ".build/engineering/remote/admit/output.log").read_text())

    @patch.object(remote, "CHECKOUTS", 2)
    def test_a_checkout_a_run_left_changed_is_kept_and_passed_over(self):
        self.assertEqual(self.eng("remote", "run", "check")[0], 0)
        (self.checkout(0) / "stray.txt").write_text("left by a run\n")
        status, output, _error = self.eng("remote", "run", "check")
        self.assertEqual(status, 0)
        self.assertIn("passing over", output)
        self.assertTrue((self.checkout(0) / "stray.txt").exists())
        self.assertEqual(git(self.checkout(1), "rev-parse", "HEAD"), self.commit)
        (self.checkout(1) / "stray.txt").write_text("left by a run\n")
        status, _output, error = self.eng("remote", "run", "check")
        self.assertEqual(status, 2)
        self.assertIn("inspect and remove them", error)

    def test_a_stop_ends_the_build_host_run_and_releases_its_checkout_and_admission(self):
        command = [sys.executable, str(self.toolkit / "bin/eng"), "--project", str(self.project), "remote", "run", "slow"]
        dispatcher = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        marker = self.checkout(0) / ".build/slow.pid"
        deadline = time.monotonic() + 60
        while not marker.exists() and dispatcher.poll() is None and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertTrue(marker.exists(), dispatcher.stdout.read().decode() if dispatcher.poll() is not None else "")
        time.sleep(0.2)
        lane = int(marker.read_text())
        dispatcher.send_signal(signal.SIGTERM)
        output = dispatcher.communicate(timeout=60)[0].decode()
        self.assertEqual(dispatcher.returncode, -signal.SIGTERM, output)
        self.assertIn("asking fake to stop the run", output)
        with self.assertRaises(ProcessLookupError):
            os.kill(lane, 0)
        receipt = json.loads((self.project / ".build/engineering/remote/slow/source.json").read_text())
        self.assertTrue(receipt["result"]["stopped"])
        lock = os.open(self.home / remote.ROOT / "checkouts/example/0.lock", os.O_RDWR)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(lock)
        self.assertFalse(Path(os.environ["FAKE_GATE"] + ".warm-push-guard").exists())

    def test_the_build_hosts_credential_file_is_the_runs_only_git_credential_helper(self):
        log = self.project / ".build/engineering/remote/helpers/run.log"
        self.assertEqual(self.eng("remote", "run", "helpers")[0], 0)
        self.assertNotIn("store", log.read_text())
        credentials = self.home / remote.ROOT / "git-credentials"
        credentials.write_text("https://someone:dummy-not-a-token@example.invalid\n")
        self.assertEqual(self.eng("remote", "run", "helpers")[0], 0)
        text = log.read_text()
        # An empty value resets the helpers that system and global configuration name.
        self.assertIn(f"'', 'store --file={credentials}']", text)
        self.assertIn("['username=someone']", text)

    def run_here(self, *arguments):
        """eng run, with this Mac's policy refresh, which would fetch GitHub, left out."""
        with patch.object(cli, "refresh_policy", return_value="policy"):
            return self.eng("run", *arguments)

    def test_a_build_host_lane_runs_there_first(self):
        status, output, error = self.eng("run", "gate")
        self.assertEqual(status, 0, output + error)
        self.assertIn(f"Ran 2 tests in {self.checkout(0).resolve()}",
                      (self.project / ".build/engineering/remote/gate/run.log").read_text())
        self.assertFalse((self.project / ".build/engineering/gate").exists())
        self.assertEqual(self.eng("remote", "passed", "gate")[0], 0)

    @contextlib.contextmanager
    def busy_build_host(self):
        """Exclusive work that holds every slot of the build host's queue while the block runs."""
        holder = subprocess.Popen([sys.executable, "-m", "vocaro_engineering.resources", "--exclusive", "--",
                                   sys.executable, "-c", "print('held', flush=True); import time; time.sleep(120)"],
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env={
                                      **os.environ, "PYTHONPATH": str(config.TOOLKIT_ROOT / "src/python"),
                                      "HEAVY_SLOT_DIR": os.environ["FAKE_SLOTS"],
                                      "SW_GATE_LOCK_FILE": os.environ["FAKE_GATE"]})
        try:
            self.assertEqual(holder.stdout.readline().strip(), "held")
            yield
        finally:
            holder.send_signal(signal.SIGTERM)
            holder.wait(timeout=60)
            holder.stdout.close()

    def test_a_busy_build_host_sends_the_lane_back_here(self):
        with self.busy_build_host():
            status, output, error = self.run_here("gate")
        self.assertEqual(status, 0, output + error)
        self.assertIn("running gate on this Mac: fake did not take it: its heavy-work queue cannot take the lane now",
                      error)
        self.assertIn(f"Ran 2 tests in {self.project.resolve()}",
                      (self.project / ".build/engineering/gate/run.log").read_text())
        # Nothing ran there, so no result of the build host's is recorded.
        self.assertFalse((self.project / ".build/engineering/remote/gate").exists())
        self.assertEqual(self.eng("remote", "passed", "gate")[0], 1)

    @patch.object(remote, "CHECKOUTS", 1)
    def test_a_build_host_with_every_checkout_in_use_sends_the_lane_back_here(self):
        base = self.home / remote.ROOT / "checkouts/example"
        base.mkdir(parents=True)
        lock = os.open(base / "0.lock", os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            status, output, error = self.run_here("gate")
        finally:
            os.close(lock)
        self.assertEqual(status, 0, output + error)
        self.assertIn("running gate on this Mac: fake did not take it: every checkout of example is in use", error)
        self.assertIn(f"Ran 2 tests in {self.project.resolve()}",
                      (self.project / ".build/engineering/gate/run.log").read_text())

    def misconfigurations(self):
        """Each configuration problem of the build host's that stops a lane, as a function that
        brings it about in an ExitStack, with what eng says of it."""
        def unreachable(stack):
            stack.enter_context(patch.dict(os.environ, {"FAKE_UNREACHABLE": "1"}))

        def xcode(stack):
            # The build host has nothing at this Mac's developer directory.
            local = {**build_host.identity(None), "xcode": "Xcode 99.0\nBuild version 99A1"}
            stack.enter_context(patch.object(remote, "developer_directory", return_value=self.temp.name))
            stack.enter_context(patch.object(remote.build_host, "identity", return_value=local))

        def toolkit(stack):
            # The build host's login shell has no python3 to run the toolkit.
            python = self.tools / "python3"
            target = os.readlink(python)
            python.unlink()
            python.write_text('#!/bin/sh\necho "zsh:1: command not found: python3" >&2\nexit 127\n')
            python.chmod(0o755)
            stack.callback(lambda: (python.unlink(), python.symlink_to(target)))

        def provisioning(stack):
            settings = json.loads((self.project / "engineering.json").read_text())
            settings["lanes"]["provision"]["command"] = [sys.executable, "-c", "raise SystemExit(4)"]
            (self.project / "engineering.json").write_text(json.dumps(settings, indent=2))
            commit_all(self.project)

        return [("unreachable", unreachable, ["fake cannot be reached: ssh: Could not resolve hostname fake",
                                              "until `ssh fake true` succeeds"]),
                ("xcode", xcode, ["fake refused it: the Xcode at", "not the dispatcher's Xcode 99.0 Build version 99A1",
                                  "install this Mac's Xcode at that path on the build host"]),
                ("toolkit", toolkit, ["fake could not run the toolkit", "command not found: python3",
                                      "eng remote setup"]),
                ("provisioning", provisioning, ["fake could not provision its checkout: the provision lane exited 4",
                                                "what provisioning needs"])]

    def test_a_misconfigured_build_host_stops_the_lane_until_here_runs_it_on_this_mac(self):
        local = self.project / ".build/engineering/gate"
        for name, misconfigure, said in self.misconfigurations():
            with self.subTest(name), contextlib.ExitStack() as stack:
                shutil.rmtree(local, ignore_errors=True)
                misconfigure(stack)
                status, output, error = self.run_here("gate")
                # A configuration problem waits for the owner's decision: nothing runs here.
                self.assertFalse(local.exists(), f"{name} ran the lane here:\n{error}")
                self.assertEqual(status, remote.MISCONFIGURED, output + error)
                for text in ["eng: gate not run: ", *said, "eng: to fix it, ", "the owner's decision",
                             "`eng run --here gate`"]:
                    self.assertIn(text, error)
                self.assertNotIn("running gate on this Mac", error)
                status, output, error = self.run_here("--here", "gate")
                self.assertEqual(status, 0, output + error)
                self.assertIn(f"Ran 2 tests in {self.project.resolve()}", (local / "run.log").read_text())

    def test_a_push_that_loses_the_race_for_a_ref_the_build_host_holds_has_sent_the_commit(self):
        # Stands in for git: with FAKE_PUSH push lands the ref and then fails, as the push that loses
        # to another session's push of the same commit does, and with FAKE_PUSH=fail it lands nothing.
        real = shutil.which("git")
        wrapper = self.tools / "git"
        wrapper.write_text(f"""#!/bin/sh
if [ "$3" = push ] && [ -n "$FAKE_PUSH" ]; then
    [ "$FAKE_PUSH" = fail ] || {shlex.quote(real)} "$@" || exit
    echo "error: cannot lock ref 'refs/vocaro/build-host/x': reference already exists" >&2
    exit 1
fi
exec {shlex.quote(real)} "$@"
""")
        wrapper.chmod(0o755)
        with patch.dict(os.environ, {"FAKE_PUSH": "race"}):
            status, output, error = self.run_here("gate")
        self.assertEqual(status, 0, output + error)
        self.assertIn(f"Ran 2 tests in {self.checkout(0).resolve()}",
                      (self.project / ".build/engineering/remote/gate/run.log").read_text())
        # A push that lands nothing is still a configuration problem, and nothing runs here.
        settings = json.loads((self.project / "engineering.json").read_text())
        settings["lanes"]["gate"]["minimumTests"] = 1
        (self.project / "engineering.json").write_text(json.dumps(settings, indent=2))
        commit = commit_all(self.project)
        with patch.dict(os.environ, {"FAKE_PUSH": "fail"}):
            status, output, error = self.run_here("gate")
        self.assertEqual(status, remote.MISCONFIGURED, output + error)
        self.assertIn(f"fake did not receive {commit[:12]}", error)
        self.assertFalse((self.project / ".build/engineering/gate").exists())

    def test_build_host_only_passes_the_build_hosts_status_through_and_never_runs_the_lane_here(self):
        status, output, error = self.run_here("--build-host-only", "gate")
        self.assertEqual(status, 0, output + error)
        self.assertIn(f"Ran 2 tests in {self.checkout(0).resolve()}",
                      (self.project / ".build/engineering/remote/gate/run.log").read_text())
        self.assertEqual(self.eng("remote", "passed", "gate")[0], 0)
        self.assertEqual(self.run_here("--build-host-only", "failing")[0], 5)
        self.assertIn("Ran 2 tests", (self.project / ".build/engineering/remote/failing/run.log").read_text())
        for lane in ["gate", "failing"]:
            self.assertFalse((self.project / ".build/engineering" / lane).exists())

    def test_build_host_only_exits_busy_when_the_build_host_declines_and_stops_when_it_is_misconfigured(self):
        with self.busy_build_host():
            status, output, error = self.run_here("--build-host-only", "gate")
        self.assertEqual(status, resources.BUSY, output + error)
        self.assertIn("eng: gate not run: fake did not take it: its heavy-work queue cannot take the lane now", error)
        with patch.dict(os.environ, {"VOCARO_BUILD_HOST": "none"}):
            status, output, error = self.run_here("--build-host-only", "gate")
        self.assertEqual(status, resources.BUSY, output + error)
        self.assertIn("eng: gate not run: no build host is configured", error)
        with patch.dict(os.environ, {"FAKE_UNREACHABLE": "1"}):
            status, output, error = self.run_here("--build-host-only", "gate")
        self.assertEqual(status, remote.MISCONFIGURED, output + error)
        self.assertIn("fake cannot be reached", error)
        (self.toolkit / "engineering.json").write_text((self.toolkit / "engineering.json").read_text() + "\n")
        status, output, error = self.run_here("--build-host-only", "gate")
        self.assertEqual(status, resources.BUSY, output + error)
        self.assertIn("eng: gate not run: ", error)
        self.assertIn("commit them first", error)
        # Never here, and no result of the build host's either.
        self.assertFalse((self.project / ".build/engineering/gate").exists())
        self.assertFalse((self.project / ".build/engineering/remote/gate").exists())
        # A lane the build host never takes, and --here, contradict the mode.
        status, _output, error = self.run_here("--build-host-only", "build")
        self.assertEqual(status, 2)
        self.assertIn("build is not marked buildHost", error)
        with self.assertRaises(SystemExit):
            self.run_here("--build-host-only", "--here", "gate")

    def test_a_lane_stays_here_when_asked_unmarked_without_a_build_host_or_with_a_changed_toolkit(self):
        # Uncommitted work would go as a snapshot, so none of these stays here for want of a commit.
        (self.project / "notes.txt").write_text("uncommitted\n")
        for arguments in [["--here", "gate"], ["build"]]:
            with self.subTest(arguments):
                self.assertEqual(self.run_here(*arguments)[0], 0)
        for setting in [{"VOCARO_BUILD_HOST": "none"}, {}]:
            with self.subTest(setting), patch.dict(os.environ, setting):
                # Without VOCARO_BUILD_HOST, `ssh -G` names no build-host alias.
                if not setting: del os.environ["VOCARO_BUILD_HOST"]
                self.assertEqual(self.run_here("gate")[0], 0)
        self.assertFalse((self.home / remote.ROOT).exists())
        (self.toolkit / "engineering.json").write_text((self.toolkit / "engineering.json").read_text() + "\n")
        status, _output, error = self.run_here("gate")
        self.assertEqual(status, 0)
        self.assertIn(f"running gate on this Mac: {self.toolkit.resolve()} has changes", error)
        self.assertIn("commit them first", error)
        self.assertFalse((self.home / remote.ROOT / "checkouts").exists())

    def work_in_progress(self, checkout=None):
        """Uncommitted work of every kind on a new commit: a tracked edit beyond what is staged, a
        deletion, a new untracked file and an ignored input. Returns that commit."""
        checkout = checkout or self.project
        (checkout / ".gitignore").write_text(".build/\nignored-input.txt\n")
        for name in ["edited.txt", "deleted.txt"]: (checkout / name).write_text("committed\n")
        base = commit_all(checkout)
        (checkout / "edited.txt").write_text("staged\n")
        git(checkout, "add", "edited.txt")
        (checkout / "edited.txt").write_text("edited\n")
        (checkout / "deleted.txt").unlink()
        (checkout / "new.txt").write_text("new\n")
        (checkout / "ignored-input.txt").write_text("this Mac's own\n")
        return base

    def session(self, checkout=None):
        """What a snapshot leaves as it was: HEAD and its branch, the index and the working tree."""
        checkout = checkout or self.project
        files = {name: (checkout / name).read_text() if (checkout / name).exists() else None
                 for name in ["edited.txt", "deleted.txt", "new.txt", "ignored-input.txt"]}
        return {"HEAD": git(checkout, "rev-parse", "HEAD"), "branch": git(checkout, "symbolic-ref", "-q", "HEAD"),
                "cached": git(checkout, "diff", "--cached"), "stage": git(checkout, "ls-files", "--stage"),
                "status": git(checkout, "status", "--porcelain", "--untracked-files=all"),
                "diff": git(checkout, "diff"), "files": files}

    def index(self, checkout=None):
        return Path(git(checkout or self.project, "rev-parse", "--path-format=absolute", "--git-path", "index"))

    def test_uncommitted_work_runs_on_the_build_host_as_a_snapshot_and_leaves_the_session_as_it_was(self):
        base = self.work_in_progress()
        before = self.session()
        self.assertTrue(before["cached"] and before["diff"] and before["branch"], before)
        results = self.project / ".build/engineering/remote/gate"
        for filtered in [[], ["--", "a"]]:
            with self.subTest(filtered):
                # Read after git status, which may refresh the index's file stats.
                index = self.index().read_bytes()
                status, output, error = self.run_here("gate", *filtered)
                self.assertEqual(status, 0, output + error)
                self.assertEqual(self.index().read_bytes(), index)
                self.assertEqual(self.session(), before)
                receipt = json.loads((results / "source.json").read_text())
                snapshot = receipt["commit"]
                self.assertEqual((receipt["head"], receipt["snapshot"], receipt["arguments"]), (base, True, filtered[1:]))
                self.assertEqual(git(self.project, "rev-parse", f"{snapshot}^@"), base)
                self.assertEqual(receipt["result"]["receipt"]["project"]["revision"], snapshot)
                self.assertIn(f"remote: sending {snapshot[:12]}, a snapshot of the working tree on {base[:12]}, to fake",
                              error)
                self.assertIn(f"gate at {snapshot[:12]}, a snapshot of the working tree on {base[:12]}, on ", error)
                # The build host ran the working tree: the edit rather than what is staged, without the
                # deleted file, with the new one, and without the ignored input, which stays here.
                checkout = self.checkout(0)
                self.assertEqual(git(checkout, "rev-parse", "HEAD"), snapshot)
                self.assertIn(f"Ran 2 tests in {checkout.resolve()}", (results / "run.log").read_text())
                self.assertEqual((checkout / "edited.txt").read_text(), "edited\n")
                self.assertFalse((checkout / "deleted.txt").exists())
                self.assertEqual((checkout / "new.txt").read_text(), "new\n")
                self.assertFalse((checkout / "ignored-input.txt").exists())
                self.assertNotIn("ignored-input.txt", git(self.project, "ls-tree", "-r", "--name-only", snapshot))
                self.assertFalse((self.project / ".build/engineering/gate").exists())
        # No branch or other ref of the session's holds a snapshot.
        self.assertEqual(git(self.project, "for-each-ref", "--contains", snapshot), "")
        self.assertEqual(git(self.project, "stash", "list"), "")

    def test_a_snapshots_result_never_counts_as_a_pass_of_head_or_of_the_snapshot(self):
        base = self.work_in_progress()
        self.assertEqual(self.run_here("gate")[0], 0)
        receipt = self.project / ".build/engineering/remote/gate/source.json"
        snapshot = json.loads(receipt.read_text())["commit"]
        self.assertEqual(json.loads(receipt.read_text())["result"]["exitStatus"], 0)
        for commit in [base, snapshot]:
            with self.subTest(commit):
                self.assertEqual(self.eng("remote", "passed", "gate", "--commit", commit)[0], 1)
                self.assertFalse((remote.store(self.project) / commit).exists())
        self.assertEqual(self.eng("remote", "passed", "gate")[0], 1)
        # Even a snapshot's receipt kept by commit counts for neither.
        for commit in [base, snapshot]:
            (remote.store(self.project) / commit).mkdir(parents=True)
            shutil.copyfile(receipt, remote.store(self.project) / commit / "gate.json")
            self.assertIsNone(remote.passed(self.project, "gate", commit))
        # The same work committed is a commit's run, and its pass counts.
        commit = commit_all(self.project)
        self.assertEqual(git(self.project, "rev-parse", f"{commit}^{{tree}}"),
                         git(self.project, "rev-parse", f"{snapshot}^{{tree}}"))
        status, output, error = self.run_here("gate")
        self.assertEqual(status, 0, output + error)
        self.assertEqual(json.loads(receipt.read_text())["snapshot"], False)
        self.assertEqual(git(self.checkout(0), "rev-parse", "HEAD"), commit)
        self.assertEqual(self.eng("remote", "passed", "gate")[0], 0)

    def test_uncommitted_work_comes_back_here_from_a_busy_build_host_and_stops_for_a_misconfigured_one(self):
        self.work_in_progress()
        before = self.session()
        with self.busy_build_host():
            status, output, error = self.run_here("gate")
        self.assertEqual(status, 0, output + error)
        self.assertIn("running gate on this Mac: fake did not take it: its heavy-work queue cannot take the lane now",
                      error)
        self.assertIn(f"Ran 2 tests in {self.project.resolve()}",
                      (self.project / ".build/engineering/gate/run.log").read_text())
        self.assertFalse((self.project / ".build/engineering/remote/gate").exists())
        shutil.rmtree(self.project / ".build/engineering/gate")
        with patch.dict(os.environ, {"FAKE_UNREACHABLE": "1"}):
            status, output, error = self.run_here("gate")
        self.assertEqual(status, remote.MISCONFIGURED, output + error)
        self.assertIn("fake cannot be reached", error)
        self.assertFalse((self.project / ".build/engineering/gate").exists())
        self.assertEqual(self.session(), before)

    def test_build_host_only_sends_uncommitted_work_as_a_snapshot(self):
        base = self.work_in_progress()
        status, output, error = self.run_here("--build-host-only", "gate", "--", "a")
        self.assertEqual(status, 0, output + error)
        receipt = json.loads((self.project / ".build/engineering/remote/gate/source.json").read_text())
        self.assertEqual((receipt["head"], receipt["snapshot"], receipt["arguments"]), (base, True, ["a"]))
        self.assertEqual(git(self.checkout(0), "rev-parse", "HEAD"), receipt["commit"])
        with self.busy_build_host():
            status, output, error = self.run_here("--build-host-only", "gate")
        self.assertEqual(status, resources.BUSY, output + error)
        self.assertIn("eng: gate not run: fake did not take it", error)
        self.assertFalse((self.project / ".build/engineering/gate").exists())

    def test_eng_remote_run_and_admit_send_uncommitted_work_as_a_snapshot(self):
        base = self.work_in_progress()
        before = self.session()
        status, output, error = self.eng("remote", "run", "check", "--", "a")
        self.assertEqual(status, 0, output + error)
        receipt = json.loads((self.project / ".build/engineering/remote/check/source.json").read_text())
        self.assertEqual((receipt["head"], receipt["snapshot"]), (base, True))
        status, output, error = self.eng("remote", "admit", "--", sys.executable, "-c",
                                         "print('new is', open('new.txt').read().strip())")
        self.assertEqual(status, 0, output + error)
        self.assertIn("new is new", output)
        receipt = json.loads((self.project / ".build/engineering/remote/admit/source.json").read_text())
        self.assertEqual((receipt["head"], receipt["snapshot"]), (base, True))
        self.assertEqual(self.session(), before)

    def test_a_tree_changed_only_in_ignored_files_runs_at_head_and_a_linked_worktree_keeps_its_own_index(self):
        (self.project / ".build").mkdir()
        (self.project / ".build/input").write_text("ignored\n")
        self.assertEqual(remote.snapshot(self.project), (self.commit, self.commit))
        # A session's linked worktree has an index of its own, which the snapshot copies.
        linked = Path(self.temp.name) / "linked"
        git(self.project, "worktree", "add", "-q", "-b", "session", str(linked))
        base = self.work_in_progress(linked)
        before, index = self.session(linked), self.index(linked).read_bytes()
        main = self.index().read_bytes()
        commit, head = remote.snapshot(linked)
        self.assertEqual(head, base)
        self.assertNotEqual(commit, base)
        self.assertEqual((self.index(linked).read_bytes(), self.index().read_bytes()), (index, main))
        self.assertEqual(self.session(linked), before)
        self.assertEqual(git(linked, "show", f"{commit}:edited.txt"), "edited")
        self.assertEqual(git(linked, "ls-tree", "-r", "--name-only", commit).split(),
                         [".gitignore", "edited.txt", "engineering.json", "new.txt"])

    def test_nested_work_runs_its_lane_inside_the_admission_it_inherits(self):
        inner = [sys.executable, str(self.toolkit / "bin/eng"), "--project", str(self.project), "run", "gate"]
        with patch.object(cli, "refresh_policy", return_value="policy"):
            self.assertEqual(self.eng("admit", "--", "/bin/sh", "-c", f"exec {shlex.join(inner)} >/dev/null 2>&1")[0], 0)
        self.assertIn(f"Ran 2 tests in {self.project.resolve()}",
                      (self.project / ".build/engineering/gate/run.log").read_text())
        self.assertFalse((self.home / remote.ROOT).exists())

    def test_only_a_heavy_lane_that_keeps_off_the_screen_may_go_to_the_build_host(self):
        for lane in [{"command": ["true"], "buildHost": True}, {"command": ["true"], "resource": "heavy", "buildHost": 1},
                     {"command": ["true"], "resource": "heavy", "foreground": True, "buildHost": True}]:
            with self.subTest(lane):
                (self.project / "engineering.json").write_text(json.dumps(
                    {"schemaVersion": 1, "name": "Example", "lanes": {"lane": lane}}))
                with self.assertRaises(ValueError): config.load(self.project)

    def test_the_toolkit_runs_itself_without_a_second_checkout(self):
        settings = json.loads((self.toolkit / "engineering.json").read_text())
        # Heavy, so the build host's policy refresh fetches the toolkit's origin from its own checkout.
        settings["lanes"] = {"check": {"command": [sys.executable, "-c", "print('Ran 1 tests')"], "minimumTests": 1,
                                       "resource": "heavy"}}
        (self.toolkit / "engineering.json").write_text(json.dumps(settings))
        commit = commit_all(self.toolkit)
        with contextlib.redirect_stdout(StringIO()), contextlib.redirect_stderr(StringIO()) as error:
            self.assertEqual(cli.main(["--project", str(self.toolkit), "remote", "run", "check"]), 0, error.getvalue())
        slot = self.home / remote.ROOT / "checkouts/vocaro-engineering/0"
        self.assertEqual([path.name for path in slot.iterdir()], ["vocaro-engineering"])
        self.assertEqual(git(slot / "vocaro-engineering", "rev-parse", "HEAD"), commit)
        self.assertEqual([path.name for path in (self.home / remote.ROOT / "repositories").iterdir()],
                         ["vocaro-engineering.git"])


if __name__ == "__main__":
    unittest.main()


# The build host's Homebrew, in a prefix of its own: it records what it installs, links a python3
# when python is installed, and answers `brew info --json=v2` from those records.
FAKE_BREW = """#!/bin/sh
prefix=$(cd "$(dirname "$0")/.." && pwd)
case "$1" in
    shellenv) echo "export PATH=\\"$prefix/bin:\\$PATH\\"" ;;
    --prefix) echo "$prefix" ;;
    install)
        shift
        echo "$*" >> "$prefix/installs.log"
        for formula in "$@"; do
            touch "$prefix/installed-$formula"
            if [ "$formula" = python ]; then ln -sf "$FAKE_PYTHON" "$prefix/bin/python3"; fi
        done ;;
    info)
        if [ -e "$prefix/installed-$3" ]; then echo '{"formulae": [{"installed": [{"version": "1"}]}]}'
        else echo '{"formulae": [{"installed": []}]}'; fi ;;
    *) exit 1 ;;
esac
"""
# ssh to a build host whose PATH holds only the system's tools, as a fresh Mac's does, so this
# Mac's own Homebrew is never reached.
SETUP_SSH = FAKE_SSH.replace('HOME="$FAKE_HOME"', 'PATH="$FAKE_PATH" HOME="$FAKE_HOME"')
# A login shell that reads ~/.zprofile, as zsh's does.
PROFILE_SHELL = """#!/bin/sh
if [ "$1" = -l ]; then shift; if [ -f "$HOME/.zprofile" ]; then . "$HOME/.zprofile"; fi; fi
exec /bin/sh "$@"
"""


class RemoteSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.home = base / "build-host-home"
        self.home.mkdir()
        tools = base / "bin"
        tools.mkdir()
        for name, text in [("ssh", SETUP_SSH), ("login-shell", PROFILE_SHELL)]:
            (tools / name).write_text(text)
            (tools / name).chmod(0o755)
        self.prefix = base / "homebrew"
        self.brew = self.prefix / "bin/brew"
        self.project = base / "example"
        self.project.mkdir()
        (self.project / "engineering.json").write_text(json.dumps({"schemaVersion": 1, "name": "Example"}))
        self.environment = patch.dict(os.environ, {
            "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}", "FAKE_HOME": str(self.home),
            "FAKE_SHELL": str(tools / "login-shell"), "FAKE_PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "FAKE_PYTHON": sys.executable})
        self.environment.start()
        self.patches = [patch.object(remote, "BREW", [str(self.brew)]),
                        patch.object(remote, "developer_directory", return_value=None)]
        for item in self.patches: item.start()

    def tearDown(self):
        for item in self.patches: item.stop()
        self.environment.stop()
        self.temp.cleanup()

    def install_homebrew(self):
        self.brew.parent.mkdir(parents=True)
        self.brew.write_text(FAKE_BREW)
        self.brew.chmod(0o755)

    def eng(self, *arguments):
        with contextlib.redirect_stdout(StringIO()) as output, contextlib.redirect_stderr(StringIO()) as error:
            status = cli.main(["--project", str(self.project), "remote", "setup", "--host", "fake", *arguments])
        return status, output.getvalue(), error.getvalue()

    def installs(self):
        log = self.prefix / "installs.log"
        return log.read_text().splitlines() if log.exists() else []

    def test_a_build_host_without_homebrew_is_left_unchanged_and_the_owner_told_to_install_it(self):
        status, output, error = self.eng("numpy")
        self.assertEqual(status, 1)
        self.assertIn("Homebrew is not installed on this build host", output)
        self.assertIn("https://brew.sh", output)
        self.assertFalse((self.home / ".zprofile").exists())
        self.assertNotIn("ready", output)

    def test_setup_puts_homebrew_on_the_login_path_installs_and_checks_from_a_new_login_shell(self):
        self.install_homebrew()
        status, output, error = self.eng("numpy", "epubcheck", "--import", "json")
        self.assertEqual(status, 0, output + error)
        self.assertEqual(self.installs(), [" ".join([*remote.SETUP_FORMULAE, "numpy", "epubcheck"])])
        profile = (self.home / ".zprofile").read_text()
        self.assertEqual(profile.count(f'eval "$({self.brew} shellenv)"'), 1)
        for line in ["ok      Homebrew at", f"ok      python3 is {self.prefix}/bin/python3", "ok      numpy",
                     "ok      epubcheck", "ok      import json", "fake is ready for eng remote"]:
            self.assertIn(line, output)
        # Again: the PATH line is not written twice, and what is installed stays installed.
        status, output, error = self.eng("numpy", "epubcheck")
        self.assertEqual(status, 0, output + error)
        self.assertEqual((self.home / ".zprofile").read_text(), profile)

    def test_homebrew_after_the_system_on_the_path_is_put_first(self):
        # brew is found, but /usr/bin's python3 comes first.
        self.install_homebrew()
        with patch.dict(os.environ, {"FAKE_PATH": f"/usr/bin:/bin:/usr/sbin:/sbin:{self.prefix}/bin"}):
            status, output, error = self.eng("--check")
            self.assertEqual(status, 1)
            self.assertIn("MISSING Homebrew's python3 first on the login shell's PATH (found /usr/bin/python3)", output)
            status, output, error = self.eng()
        self.assertEqual(status, 0, output + error)
        self.assertIn("setup: put Homebrew first on the login shell's PATH in ~/.zprofile", output)
        self.assertIn(f"ok      python3 is {self.prefix}/bin/python3", output)

    def test_check_installs_nothing_and_names_what_is_missing(self):
        self.install_homebrew()
        status, output, _ = self.eng("--check", "numpy", "--import", "no_such_module")
        self.assertEqual(status, 1)
        self.assertEqual(self.installs(), [])
        self.assertIn("MISSING Homebrew on the login shell's PATH", output)
        self.assertIn("fake is not ready", output)
        # On the PATH but with nothing installed, every formula and import is named.
        (self.home / ".zprofile").write_text(f'eval "$({self.brew} shellenv)"\n')
        status, output, _ = self.eng("--check", "numpy", "--import", "no_such_module")
        self.assertEqual(status, 1)
        for line in ["MISSING Homebrew's python3 first", "MISSING numpy", "MISSING import no_such_module"]:
            self.assertIn(line, output)

    def test_a_different_xcode_on_the_build_host_is_named(self):
        self.install_homebrew()
        # This Mac's Xcode is one version, the build host's at the same path another.
        with patch.object(remote, "developer_directory", return_value="/Applications/Xcode.app/Contents/Developer"), \
                patch.object(build_host, "output", return_value="Xcode 99.0\nBuild version 99A1"):
            status, output, _ = self.eng()
        self.assertEqual(status, 1)
        self.assertIn("MISSING Xcode 99.0 Build version 99A1 at /Applications/Xcode.app/Contents/Developer, "
                      "as on this Mac (found ", output)

    def test_names_that_are_not_formulae_or_modules_are_refused_before_anything_runs(self):
        self.install_homebrew()
        for arguments in [["numpy; rm -rf ~"], ["--import", "os; import shutil"]]:
            status, _, error = self.eng(*arguments)
            self.assertEqual(status, 2, error)
            self.assertEqual(self.installs(), [])


# Two Macs on one file system, for eng doctor: FAKE_MAC names the Mac a command runs on. An Xcode is
# a directory whose version.MAC and runtimes.MAC.json say what that Mac has at its path, and
# FAKE_XCODES/default.MAC is that Mac's `xcode-select -p`.
FAKE_XCODE_SELECT = """#!/bin/sh
[ "$1" = -p ] && exec cat "$FAKE_XCODES/default.$FAKE_MAC"
exit 1
"""
FAKE_XCRUN = """#!/bin/sh
developer=${DEVELOPER_DIR:-$(xcode-select -p)}
case "$1" in
    xcodebuild) exec cat "$developer/version.$FAKE_MAC" ;;
    simctl) exec cat "$developer/runtimes.$FAKE_MAC.json" ;;
esac
exit 1
"""
# The build host is the other Mac.
DOCTOR_SSH = FAKE_SSH.replace('HOME="$FAKE_HOME"', 'FAKE_MAC=there HOME="$FAKE_HOME"')
RELEASE = "Xcode 27.1\nBuild version 27A9275"
BETA = "Xcode 27.2\nBuild version 27B5028f"
RUNTIMES = [("iOS 27.1", "24B5070a"), ("macOS 27.1", "26B5091g")]


class BuildHostDoctorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        home = base / "build-host-home"
        home.mkdir()
        tools = base / "bin"
        tools.mkdir()
        for name, text in [("ssh", DOCTOR_SSH), ("login-shell", FAKE_SHELL), ("xcode-select", FAKE_XCODE_SELECT),
                           ("xcrun", FAKE_XCRUN)]:
            (tools / name).write_text(text)
            (tools / name).chmod(0o755)
        system = Path("/usr/bin/python3")
        (tools / "python3").symlink_to(system if sys.platform == "darwin" and system.exists() else sys.executable)
        self.xcodes = base / "xcodes"
        self.xcodes.mkdir()
        self.project = base / "example"
        self.project.mkdir()
        (self.project / "engineering.json").write_text(json.dumps({"schemaVersion": 1, "name": "Example",
                                                                   "integration": "direct-main"}))
        self.environment = patch.dict(os.environ, {
            "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}", "FAKE_HOME": str(home),
            "FAKE_SHELL": str(tools / "login-shell"), "FAKE_XCODES": str(self.xcodes), "FAKE_MAC": "here",
            "VOCARO_BUILD_HOST": "fake"})
        self.environment.start()
        os.environ.pop("DEVELOPER_DIR", None)
        # Both Macs select the same Xcode 27.1, with the same runtimes.
        self.release = self.xcode("Xcode.app", here=(RELEASE, RUNTIMES), there=(RELEASE, RUNTIMES))
        self.select(here=self.release, there=self.release)

    def tearDown(self):
        self.environment.stop()
        self.temp.cleanup()

    def xcode(self, name, **macs):
        """An Xcode at name, with the version and runtimes each Mac named has at that path."""
        developer = Path(self.temp.name).resolve() / name / "Contents/Developer"
        developer.mkdir(parents=True, exist_ok=True)
        for mac, (version, runtimes) in macs.items():
            (developer / f"version.{mac}").write_text(version + "\n")
            (developer / f"runtimes.{mac}.json").write_text(json.dumps({"runtimes": [
                {"name": runtime, "buildversion": build, "isAvailable": True} for runtime, build in runtimes]}))
        return developer

    def select(self, **macs):
        for mac, developer in macs.items():
            (self.xcodes / f"default.{mac}").write_text(f"{developer}\n")

    def doctor(self):
        with contextlib.redirect_stdout(StringIO()) as output, contextlib.redirect_stderr(StringIO()):
            status = cli.main(["--project", str(self.project), "doctor"])
        report = json.loads(output.getvalue())
        self.assertEqual(report["project"], "Example")
        return status, report["buildHost"]

    def test_matching_macs_pass(self):
        status, found = self.doctor()
        self.assertEqual((status, found["errors"], found["unchecked"]), (0, [], []), found)
        self.assertEqual(found["host"], "fake")
        for side in ["local", "remote"]:
            self.assertEqual(found[side]["defaultDeveloperDir"], str(self.release))
            self.assertEqual(found[side]["xcode"], RELEASE)
            self.assertEqual(found[side]["runtimes"], ["iOS 27.1 (24B5070a)", "macOS 27.1 (26B5091g)"])

    def test_a_differing_default_or_selected_xcode_or_runtime_is_an_error(self):
        with self.subTest("default"):
            # As on 2026-10-26: the build host has this Mac's Xcode, but selects a beta by default.
            beta = self.xcode("Xcode-beta.app", there=(BETA, RUNTIMES))
            self.select(there=beta)
            status, found = self.doctor()
            self.assertEqual(status, 1)
            self.assertEqual(len(found["errors"]), 1, found)
            self.assertIn(f"`xcode-select -p` is {self.release} (Xcode 27.1, Build version 27A9275) on this Mac and "
                          f"{beta} (Xcode 27.2, Build version 27B5028f) on fake", found["errors"][0])
            self.assertIn("sudo xcode-select -s", found["errors"][0])
            self.select(there=self.release)
        with self.subTest("selected"):
            # The Xcode a run there uses differs: one error says so, not a second for the default.
            self.xcode("Xcode.app", there=(BETA, RUNTIMES))
            status, found = self.doctor()
            self.assertEqual(status, 1)
            self.assertEqual(len(found["errors"]), 1, found)
            self.assertIn(f"the Xcode at this Mac's developer directory, {self.release}, is Xcode 27.1, Build version "
                          "27A9275 on this Mac and Xcode 27.2, Build version 27B5028f on fake", found["errors"][0])
            self.assertIn("eng remote refuses", found["errors"][0])
            self.xcode("Xcode.app", there=(RELEASE, RUNTIMES))
        with self.subTest("runtimes"):
            self.xcode("Xcode.app", here=(RELEASE, [*RUNTIMES, ("iOS 27.2", "24C5028f")]))
            status, found = self.doctor()
            self.assertEqual(status, 1)
            self.assertEqual(len(found["errors"]), 1, found)
            self.assertIn("only this Mac has iOS 27.2 (24C5028f)", found["errors"][0])

    def test_an_unreachable_or_unconfigured_build_host_is_named_with_what_was_not_checked(self):
        with patch.dict(os.environ, {"FAKE_UNREACHABLE": "1"}):
            status, found = self.doctor()
        self.assertEqual(status, 1)
        self.assertEqual(len(found["errors"]), 1, found)
        self.assertIn("fake cannot be reached: ssh: Could not resolve hostname fake", found["errors"][0])
        self.assertIn("`ssh fake true`", found["errors"][0])
        self.assertEqual(len(found["unchecked"]), 1, found)
        self.assertIn("fake's default Xcode, its Xcode at this Mac's developer directory and its simulator runtimes",
                      found["unchecked"][0])
        # This Mac's side is still reported.
        self.assertEqual(found["local"]["xcode"], RELEASE)
        with patch.dict(os.environ, {"VOCARO_BUILD_HOST": "none"}):
            status, found = self.doctor()
        self.assertEqual((status, found["host"], found["errors"]), (0, None, []), found)
        self.assertIn("no build host is configured", found["unchecked"][0])
