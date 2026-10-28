import contextlib
from datetime import datetime, timedelta, timezone
import fcntl
from io import StringIO
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/python"))
from vocaro_engineering import build_host, cli, config, devices, evidence, processes, remote, resources

# Stands in for ssh: the build host is a home directory on this Mac. Like sshd, it runs the
# command in that home through a shell, and git's own ssh transport reaches it the same way.
# FAKE_UNREACHABLE makes it fail as ssh does when the build host's name does not resolve, and
# FAKE_SSH_SAYS fail with the lines it holds, as ssh says them of a failure of its own.
# FAKE_TMPDIR gives the build host a temporary directory of its own, and FAKE_DEVICE_RECORD its
# device record, which is never this Mac's.
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
if [ -n "$FAKE_SSH_SAYS" ]; then
    printf '%s\\n' "$FAKE_SSH_SAYS" >&2
    exit 255
fi
shift
cd "$FAKE_HOME" || exit 255
# The build host has its own heavy-work queue.
HOME="$FAKE_HOME" SHELL="$FAKE_SHELL" HEAVY_SLOT_DIR="$FAKE_SLOTS" SW_GATE_LOCK_FILE="$FAKE_GATE" \
    TMPDIR="${FAKE_TMPDIR:-$TMPDIR}" VOCARO_DEVICE_RECORD="${FAKE_DEVICE_RECORD:-$FAKE_HOME/device-handoff.json}" \
    exec /bin/sh -c "$*"
"""
# A login shell that reads no profile, so the test's PATH reaches the build host.
FAKE_SHELL = """#!/bin/sh
[ "$1" = -l ] && shift
exec /bin/sh "$@"
"""
# Well-formed, invented identifiers: a hardware UDID and a simulator UUID.
PHONE = "00008130-001A2C3E0C38001C"
DUO = "5B0E6C51-2D3A-4F7B-9C1E-0A4B8D2F6E13"
# A lane that says it has started, then puts its PID in .build/NAME.pid, whole, and waits to be stopped.
NAP = ("import os, pathlib, sys, time; name = sys.argv[1]; print(name, 'started', flush=True); "
       "partial = pathlib.Path('.build', name + '.partial'); partial.write_text(str(os.getpid())); "
       "partial.rename(pathlib.Path('.build', name + '.pid')); time.sleep(300)")
# Stands in for ssh in front of FAKE_SSH, whose path FAKE_SSH_REAL names: the connection that carries
# a run (its command holds the plan's work) is lost once the file FAKE_DROP names exists. ssh then
# says why, as it does when the build host stops answering, and exits 255; the end of the build
# host's standard input is its end of the connection closing.
DROPPING_SSH = """#!/bin/sh
case "$*" in
    *'"work": ['*) exec "$FAKE_PYTHON" "$FAKE_DROPPER" "$@" ;;
esac
exec "$FAKE_SSH_REAL" "$@"
"""
DROPPER = """import os, subprocess, sys, threading, time
arguments = sys.argv[1:]
while arguments[0].startswith("-"):
    arguments = arguments[2:] if arguments[0] in ("-o", "-p", "-i", "-l", "-F", "-J") else arguments[1:]
host, command = arguments[0], " ".join(arguments[1:])
environment = {**os.environ, "HOME": os.environ["FAKE_HOME"], "SHELL": os.environ["FAKE_SHELL"],
               "HEAVY_SLOT_DIR": os.environ["FAKE_SLOTS"], "SW_GATE_LOCK_FILE": os.environ["FAKE_GATE"],
               "TMPDIR": os.environ["FAKE_TMPDIR"]}
child = subprocess.Popen(["/bin/sh", "-c", command], cwd=os.environ["FAKE_HOME"], env=environment,
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
def forward():
    for line in iter(child.stdout.readline, b""):
        sys.stdout.buffer.write(line)
        sys.stdout.buffer.flush()
relay = threading.Thread(target=forward, daemon=True)
relay.start()
while child.poll() is None and not os.path.exists(os.environ["FAKE_DROP"]):
    time.sleep(0.05)
if child.poll() is None:
    sys.stderr.write(f"Timeout, server {host} not responding.\\n")
    sys.stderr.flush()
    child.stdin.close()
    os._exit(255)
relay.join()
sys.exit(child.returncode)
"""


class Later(datetime):
    """Two hours from now, for an owner's window that has ended."""

    @classmethod
    def now(cls, tz=None):
        return datetime.now(tz) + timedelta(hours=2)


def durable_scratch():
    """A test directory outside /private/tmp, which the device record refuses, even when TMPDIR is there."""
    base = Path(tempfile.gettempdir())
    if devices.restart_emptied(base):
        base = Path(subprocess.check_output(["/usr/bin/getconf", "DARWIN_USER_TEMP_DIR"], text=True).strip()) \
            if sys.platform == "darwin" else Path(__file__).resolve().parents[1] / ".build"
        base.mkdir(exist_ok=True)
    return tempfile.TemporaryDirectory(dir=base)


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
            "linger": {"command": [python, "-c", "import os, pathlib, time; "
                                   "pathlib.Path('.build/linger.pid').write_text(str(os.getpid())); time.sleep(60)"]},
            "gate": {"command": [python, "-c", "import os; print('Ran 2 tests in', os.getcwd())"],
                     "resource": "heavy", "minimumTests": 2, "buildHost": True},
            "failing": {"command": [python, "-c", "print('Ran 2 tests'); raise SystemExit(5)"],
                        "resource": "heavy", "minimumTests": 2, "buildHost": True},
            "build": {"command": [python, "-c", "print('Ran 1 tests')"], "resource": "heavy"},
            # Lane groups and their lanes: right's count names a skip, overdue outlasts its timeout,
            # and napper and dozer say they have started, then wait to be stopped.
            "left": {"command": [python, "-c", "print('Ran 2 tests')"], "resource": "heavy", "minimumTests": 2},
            "right": {"command": [python, "-c", "print('Ran 3 tests\\n\\nOK (skipped=1)')"], "resource": "heavy"},
            "broken": {"command": [python, "-c", "print('Ran 1 tests'); raise SystemExit(5)"], "resource": "heavy"},
            "overdue": {"command": [python, "-c", "import time; print('waiting', flush=True); time.sleep(300)"],
                        "timeout": 1},
            "napper": {"command": [python, "-c", NAP, "napper"]},
            "dozer": {"command": [python, "-c", NAP, "dozer"]},
            "pair": {"parallel": ["left", "right"], "resource": "heavy", "buildHost": True},
            "mixed": {"parallel": ["left", "broken", "overdue"], "resource": "heavy", "buildHost": True},
            "stalled": {"parallel": ["napper", "dozer"], "resource": "heavy", "buildHost": True},
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
        # Each Mac's device record, where a restart does not empty it, which the record requires.
        records = durable_scratch()
        self.addCleanup(records.cleanup)
        self.record_here = Path(records.name) / "here" / "device-handoff.json"
        self.record_there = Path(records.name) / "there" / "device-handoff.json"
        self.environment = patch.dict(os.environ, {
            "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}", "FAKE_HOME": str(self.home),
            "FAKE_SHELL": str(tools / "login-shell"), "HEAVY_SLOT_DIR": str(base / "slots"),
            "SW_GATE_LOCK_FILE": str(base / "gate"), "HEAVY_POLL_SECONDS": "0.01", "VOCARO_BUILD_HOST": "fake",
            "FAKE_SLOTS": str(base / "build-host-slots"), "FAKE_GATE": str(base / "build-host-gate"),
            "FAKE_TMPDIR": str(base / "build-host-tmp"), devices.OVERRIDE: str(self.record_here),
            "FAKE_DEVICE_RECORD": str(self.record_there)})
        (base / "build-host-tmp").mkdir()
        self.environment.start()
        # Python writes bytecode, as it does for a session that sets no PYTHONDONTWRITEBYTECODE and in
        # the build host's login shell, which never has this Mac's environment.
        os.environ.pop("PYTHONDONTWRITEBYTECODE", None)
        # No session holds a device unless a test names one.
        for name in (devices.OWNER, "CODEX_THREAD_ID", "CLAUDE_CODE_SESSION_ID"):
            os.environ.pop(name, None)
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
        # Its .gitignore keeps out the bytecode that running it writes beside its source.
        shutil.copyfile(config.TOOLKIT_ROOT / ".gitignore", root / ".gitignore")
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

    def hold(self, key="iPhone", device=PHONE, owner="session-a"):
        record = devices.Record(self.record_here)
        revision = (record.entry(key) or {}).get("revision", 0)
        moment = lambda minutes: (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()
        # A physical device's reservation records the window the owner agreed.
        window = {"by": "owner", "at": moment(-1), "until": moment(90)} if devices.physical(device) else None
        return record.reserve(key, owner=owner, revision=revision, purpose="device run", release_by=moment(60),
                              device=device, approval=window)

    def nothing_sent(self):
        return not (self.home / remote.ROOT / "repositories").exists()

    def test_every_run_makes_the_build_hosts_record_defer_physical_devices_to_this_mac(self):
        self.assertEqual(self.eng("remote", "run", "check")[0], 0)
        there = devices.Record(self.record_there)
        self.assertEqual(there.load()[devices.DEFERRED], build_host.mac_name())
        revision = there.load()["revision"]
        self.assertEqual(self.eng("remote", "admit", "--", sys.executable, "-c", "pass")[0], 0)
        self.assertEqual(there.load()["revision"], revision)
        self.assertNotIn(devices.DEFERRED, devices.Record(self.record_here).load())
        # A run there cannot reserve a physical device in the build host's own record, even one its command
        # spells in two parts, which the dispatch here does not see as a hardware UDID.
        udid = PHONE.replace("C", "D")
        helper = ("import os, runpy, sys; sys.path.insert(0, os.path.join(os.environ['VOCARO_ENGINEERING_ROOT'], "
                  f"'src/python')); sys.argv[-1] = {udid[:9]!r} + {udid[9:]!r}; "
                  "runpy.run_module('vocaro_engineering.devices', run_name='__main__')")
        status, output, _error = self.eng("remote", "admit", "--", sys.executable, "-c", helper,
            "reserve", "iPhone", "--owner", "there", "--revision", "0", "--purpose", "p",
            "--release-by", "2099-01-01T00:00:00Z", "--device", "UDID")
        self.assertNotEqual(status, 0)
        self.assertIn(f"reserved in {build_host.mac_name()}'s device record", output)
        self.assertIsNone(there.entry("iPhone"))

    def test_a_build_host_record_that_defers_elsewhere_or_holds_a_physical_device_stops_the_run(self):
        there = devices.Record(self.record_there)
        there.defer("another-mac")
        status, _output, error = self.eng("remote", "run", "check")
        self.assertEqual(status, 2)
        self.assertIn("cannot defer physical devices", error)
        self.assertIn("defers physical devices to another-mac", error)
        self.assertFalse((self.checkout(0) / ".build/provisioned").exists())
        there.defer(None)
        approved = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        there.reserve("iPhone", owner="there", revision=0, purpose="p", release_by="2099-01-01T00:00:00Z",
                      device=PHONE, approval={"by": "owner", "at": approved, "until": "released"})
        status, _output, error = self.eng("remote", "run", "check")
        self.assertEqual(status, 2)
        self.assertIn("iPhone holds a physical device", error)
        self.assertNotIn(devices.DEFERRED, there.load())

    def test_a_run_carries_a_reservation_held_here_and_records_its_run_in_the_entry(self):
        held = self.hold()
        # During the run the entry here is active, with the dispatcher's process as the run's.
        probe = ("import json, os, sys; lease = json.loads(os.environ['VOCARO_DEVICE_LEASE']); "
                 "entry = json.load(open(sys.argv[1]))['devices']['iPhone']; "
                 "print('lease', lease['record'], lease['devices'], lease['until']); "
                 "print('entry', entry['activeRun'], entry['hostProcessIdentifier'], len(entry['ownedProcesses']))")
        status, output, error = self.eng("remote", "admit", "--device", "iPhone", "--device-owner", "session-a",
                                         "--", sys.executable, "-c", probe, str(self.record_here))
        self.assertEqual(status, 0, error)
        self.assertIn(f"lease {build_host.mac_name()} {{'iPhone': '{PHONE}'}} {held['expectedReleaseBy']}", output)
        self.assertIn(f"entry True {os.getpid()} 1", output)
        entry = devices.Record(self.record_here).entry("iPhone")
        self.assertEqual(devices.condition(entry), "held")
        self.assertEqual((entry["ownerChatID"], entry["state"]), ("session-a", "reserved"))
        self.assertEqual((entry["run"]["host"], entry["run"]["outcome"]), ("fake", "exit 0"))
        self.assertEqual(entry["run"]["work"][:2], ["admit", "--"])
        self.assertFalse(devices.running(entry))
        self.assertEqual(entry["revision"], held["revision"] + 3)
        # A failing run's status is its outcome, and the device stays held for the session to release.
        os.environ[devices.OWNER] = "session-a"
        self.assertEqual(self.eng("remote", "run", "fail", "--device", "iPhone")[0], 3)
        entry = devices.Record(self.record_here).entry("iPhone")
        self.assertEqual((devices.condition(entry), entry["run"]["outcome"]), ("held", "exit 3"))

    def test_a_device_the_session_does_not_hold_idle_is_refused_before_anything_is_sent(self):
        record = devices.Record(self.record_here)
        arguments = ["remote", "admit", "--device", "iPhone", "--device-owner", "session-a", "--",
                     sys.executable, "-c", "pass"]
        status, _output, error = self.eng(*arguments)
        self.assertEqual(status, 2)
        self.assertIn("shows it absent", error)
        self.hold(owner="session-b")
        self.assertIn("shows it held by session-b", self.eng(*arguments)[2])
        entry = record.entry("iPhone")
        record.update("iPhone", owner="session-b", revision=entry["revision"], activeRun=True,
                      hostProcessIdentifier=os.getpid())
        self.assertIn("shows it active by session-b", self.eng(*arguments)[2])
        self.hold("Example Duo 1", device=DUO)
        status, _output, error = self.eng("remote", "admit", "--device", "Example Duo 1", "--device-owner",
                                          "session-a", "--", sys.executable, "-c", "pass")
        self.assertEqual(status, 2)
        self.assertIn("not a physical device", error)
        status, _output, error = self.eng("remote", "admit", "--device", "Example Duo 1", "--", sys.executable, "-c", "pass")
        self.assertIn("name the session whose reservations --device carries", error)
        # Once the owner's window has ended, the session's own reservation sends nothing.
        self.hold("iPad", device=PHONE.replace("C", "D"))
        with patch.object(devices, "datetime", Later):
            status, _output, error = self.eng("remote", "admit", "--device", "iPad", "--device-owner", "session-a",
                                              "--", sys.executable, "-c", "pass")
        self.assertEqual(status, 2)
        self.assertIn("the owner's approval of iPad ended at", error)
        self.assertTrue(self.nothing_sent())
        self.assertEqual(record.entry("iPhone")["ownerChatID"], "session-b")
        self.assertEqual(devices.condition(record.entry("iPad")), "held")

    def test_a_command_that_names_a_physical_device_no_reservation_covers_is_refused(self):
        self.hold()
        name = ["--", sys.executable, "-c", "import sys; print('named', sys.argv[1])", f"id={PHONE.lower()}"]
        status, _output, error = self.eng("remote", "admit", *name)
        self.assertEqual(status, 2)
        self.assertIn(f"names iPhone's device {PHONE}, which no --device covers", error)
        self.assertTrue(self.nothing_sent())
        # A simulator's UDID needs no reservation here, and a covered device runs.
        self.assertEqual(self.eng("remote", "admit", "--", sys.executable, "-c", "pass", DUO)[0], 0)
        status, output, _error = self.eng("remote", "admit", "--device", "iPhone", "--device-owner", "session-a", *name)
        self.assertEqual(status, 0)
        self.assertIn(f"named id={PHONE.lower()}", output)
        # eng run keeps a build-host lane that names a physical device on this Mac.
        status, output, error = self.eng("run", "gate", "--", PHONE)
        self.assertEqual(status, 0, error)
        self.assertIn("the build host uses only under a reservation", error)
        self.assertIn(f"Ran 2 tests in {self.project.resolve()}", (self.project / ".build/engineering/gate/run.log").read_text())

    def test_a_command_that_names_a_hardware_udid_no_entry_holds_is_refused_without_a_device(self):
        # The build host reaches a paired device whether or not this Mac's record has an entry for it.
        self.hold()
        older = "ab12" * 10
        # Forty hex digits that the checkout's Git does not know, even another repository's commit, may be
        # an older device's UDID.
        elsewhere = git(self.toolkit, "rev-parse", "HEAD")
        for words, udid in [(["admit", "--", sys.executable, "-c", "pass", "id=00008020-0011223344556677"],
                             "00008020-0011223344556677"),
                            (["run", "check", "--", f"platform=iOS,id={older}"], older.upper()),
                            (["admit", "--", "git", "show", elsewhere], elsewhere.upper())]:
            with self.subTest(words):
                status, _output, error = self.eng("remote", *words)
                self.assertEqual(status, 2)
                self.assertIn(f"names the hardware UDID {udid}, which no entry of this Mac's record holds and no "
                              "--device covers: reserve its device here under a key and add --device KEY. "
                              "Nothing was sent", error)
                self.assertTrue(self.nothing_sent())
        # Positive controls: nothing named, a simulator's UUID, the checkout's commit by its whole hash and
        # abbreviated, and a SHA-256.
        self.assertEqual(self.eng("remote", "admit", "--", sys.executable, "-c", "pass")[0], 0)
        named = [DUO, self.commit, self.commit[:12], "ab" * 32]
        status, output, error = self.eng("remote", "admit", "--", sys.executable, "-c",
                                         "import sys; print('named', *sys.argv[1:])", *named)
        self.assertEqual(status, 0, error)
        self.assertIn(f"named {' '.join(named)}", output)

    def test_recovering_a_run_sent_here_waits_for_what_on_the_build_host_names_its_device(self):
        # Its own device, which no other test's processes name.
        udid = PHONE.replace("1", "7")
        held = self.hold(device=udid)
        record = devices.Record(self.record_here)
        dispatcher = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(dispatcher.wait)
        self.addCleanup(dispatcher.kill)
        entry = record.update("iPhone", owner="session-a", revision=held["revision"], activeRun=True,
                              hostProcessIdentifier=dispatcher.pid, run={"host": "fake", "startedAt": devices.now()})
        dispatcher.kill()
        dispatcher.wait()
        # The fake build host is this Mac: a process there that names the device keeps it.
        user = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", f"platform=iOS,id={udid}"])
        self.addCleanup(user.wait)
        self.addCleanup(user.kill)
        with self.assertRaisesRegex(devices.Refused, f"(?s)went to fake, where these may still use its device.*PID {user.pid}, "):
            record.recover("iPhone", by="owner", revision=entry["revision"], verified="nothing on the device")
        self.assertEqual(devices.condition(record.entry("iPhone")), "orphan")
        user.kill()
        user.wait()
        # So does a process left over in a checkout there since the run started, which names nothing.
        checkout = self.checkout(0)
        checkout.mkdir(parents=True)
        left = subprocess.Popen(["sleep", "60"], cwd=checkout)
        self.addCleanup(left.wait)
        self.addCleanup(left.kill)
        with self.assertRaisesRegex(devices.Refused, f"PID {left.pid}, started .* since the run did, left over from a run"):
            record.recover("iPhone", by="owner", revision=entry["revision"], verified="nothing on the device")
        left.kill()
        left.wait()
        recovered = record.recover("iPhone", by="owner", revision=entry["revision"], verified="nothing on the device")
        self.assertEqual((recovered["state"], recovered["recovery"]["buildHost"]["host"]), ("released", "fake"))

    def test_a_device_run_names_only_its_devices_and_is_bounded(self):
        held = self.hold()
        # A hardware UDID the record holds under no key, in either form, is a device the run does not hold, as
        # is another entry's simulator; the record's own device, in any case, a UUID it does not know, a commit
        # of the checkout by its whole hash and a SHA-256 are not.
        self.hold("Example Duo 1", device=DUO, owner="session-b")
        device = ["--device", "iPhone", "--device-owner", "session-a", "--", sys.executable, "-c", "pass"]
        for other in ["00008020-0011223344556677", "AB12" * 10, DUO]:
            with self.subTest(other):
                status, _output, error = self.eng("remote", "admit", *device, f"id={other}")
                self.assertEqual(status, 2)
                self.assertIn(f"the command names {other}", error)
                self.assertTrue(self.nothing_sent())
        status, _output, error = self.eng("remote", "admit", *device, f"id={PHONE.lower()}",
                                          "C6E2A1F4-1D2B-4E5F-8A9B-0C1D2E3F4A5B", self.commit, "ab" * 32)
        self.assertEqual(status, 0, error)
        self.assertEqual(devices.condition(devices.Record(self.record_here).entry("iPhone")), "held")
        # The dispatch stops the run there once the reservation leaves no more than its reserve.
        entry = devices.Record(self.record_here).entry("iPhone")
        remaining = (devices.moment(entry["expectedReleaseBy"]) - datetime.now(timezone.utc)).total_seconds()
        sleeper = ["--device", "iPhone", "--device-owner", "session-a", "--", sys.executable, "-c",
                   "import time; print('sleeping', flush=True); time.sleep(120)"]
        with patch.object(devices, "MINIMUM", 1), \
                patch.object(devices, "TERMINATION", remaining - devices.RESTORATION - 6):
            started = time.monotonic()
            status, _output, error = self.eng("remote", "admit", *sleeper)
        self.assertLess(time.monotonic() - started, 60, error)
        self.assertNotEqual(status, 0)
        self.assertIn("bound, which its device reservations set; asking fake to stop it", error)
        entry = devices.Record(self.record_here).entry("iPhone")
        self.assertEqual(devices.condition(entry), "held")
        self.assertRegex(entry["run"]["outcome"], r"^stopped at its [1-6]s bound, then exit ")
        self.assertGreater(entry["revision"], held["revision"])

    def test_a_bounded_run_on_the_build_host_takes_its_reservation_from_the_lease(self):
        self.hold()
        helper = ("import os, runpy, sys; sys.path.insert(0, os.path.join(os.environ['VOCARO_ENGINEERING_ROOT'], "
                  "'src/python')); runpy.run_module('vocaro_engineering.devices', run_name='__main__')")
        launch = [sys.executable, "-c", helper, "run"]
        # The checkout there knows the commit's whole hash too, so it names no device there either.
        ran = [sys.executable, "-c", "print('ran there')", self.commit]
        status, output, error = self.eng("remote", "admit", "--device", "iPhone", "--device-owner", "session-a", "--",
                                         *launch, "iPhone", "--", *ran)
        self.assertEqual(status, 0, output + error)
        self.assertIn("ran there", output)
        self.assertIn("exited with status 0; iPhone stays reserved", output)
        # Without the reservation it carries, the build host's record refuses the device.
        status, output, _error = self.eng("remote", "admit", "--", *launch, "iPhone", "--", *ran)
        self.assertNotEqual(status, 0)
        self.assertIn(f"physical devices are reserved in {build_host.mac_name()}'s device record", output)
        self.assertNotIn("ran there", output)

    def test_admit_passes_the_model_or_exclusive_admission_on_to_the_build_host(self):
        probe = (f"import json, os; token = json.loads(os.environ[{resources.TOKEN!r}]); "
                 "print('held', token.get('exclusive'), token.get('model'))")
        for options, held in [([], "False False"), (["--model"], "False True"), (["--exclusive"], "True True")]:
            with self.subTest(options):
                status, output, error = self.eng("remote", "admit", *options, "--", sys.executable, "-c", probe)
                self.assertEqual(status, 0, error)
                self.assertIn(f"held {held}", output)
        # Exclusive admission holds the model already.
        with self.assertRaises(SystemExit) as refused:
            self.eng("remote", "admit", "--model", "--exclusive", "--", "true")
        self.assertEqual(refused.exception.code, 2)

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

    def test_a_run_that_lost_its_dispatcher_is_listed_before_the_next_is_sent_and_left_running(self):
        command = [sys.executable, str(self.toolkit / "bin/eng"), "--project", str(self.project), "remote", "run",
                   "linger"]
        dispatcher = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        marker = self.checkout(0) / ".build/linger.pid"
        deadline = time.monotonic() + 60
        while not marker.exists() and dispatcher.poll() is None and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertTrue(marker.exists(), dispatcher.stdout.read().decode() if dispatcher.poll() is not None else "")
        time.sleep(0.2)
        lane, left = int(marker.read_text()), []
        try:
            # A light lane takes no admission: its live run, which the checkout's slot lock records, accounts for it.
            census, missing = remote.leftover_census("fake")
            self.assertIsNone(missing)
            self.assertEqual(census["leftovers"], [])
            record = json.loads((self.home / remote.ROOT / "checkouts/example/0.lock").read_text())
            self.assertEqual(record["start"], resources.process_start(record["pid"]))
            # The run there loses its dispatcher, as SIGKILL takes it, and leaves its lane running.
            os.kill(record["pid"], signal.SIGKILL)
            dispatcher.communicate(timeout=60)
            census, missing = remote.leftover_census("fake")
            self.assertIsNone(missing)
            left = [row["pid"] for row in census["leftovers"]]
            self.assertEqual(len(left), 1, census)
            row = census["leftovers"][0]
            self.assertIn("run --here linger", row["command"])
            self.assertEqual((row["place"], row["path"]), ("checkout", str(self.checkout(0).resolve())))
            # Beside the lane, the orphaned eng still samples it with ps from time to time.
            self.assertIn(lane, [descendant["pid"] for descendant in row["descendants"]])
            # The next run is told before its work is sent, and runs; nothing is stopped.
            status, output, error = self.eng("remote", "run", "check")
            self.assertEqual(status, 0, output + error)
            self.assertIn("remote: fake runs 1 process left over from runs, which no live run or admission there "
                          "accounts for. Nothing was stopped; tell the owner", error)
            self.assertIn(f"remote:   PID {left[0]}, parent 1, started {row['start']}, has written", error)
            self.assertLess(error.index("left over from runs"), error.index("check at"))
            self.assertEqual(git(self.checkout(1), "rev-parse", "HEAD"), self.commit)
            os.kill(lane, 0)
        finally:
            for pid in [lane, *left]:
                with contextlib.suppress(ProcessLookupError): os.kill(pid, signal.SIGKILL)
            if dispatcher.poll() is None: dispatcher.kill()
            dispatcher.communicate()

    def test_a_lost_connection_says_what_ssh_said_when_it_was_lost_and_that_this_mac_slept(self):
        # As on 2026-10-26, when the dispatching Mac slept for five minutes with a run under way there.
        marker = self.checkout(0) / ".build/linger.pid"
        real, ssh = self.tools / "fake-ssh", self.tools / "ssh"
        ssh.rename(real)
        ssh.write_text(DROPPING_SSH)
        ssh.chmod(0o755)
        (self.tools / "drop.py").write_text(DROPPER)
        variables = {"FAKE_SSH_REAL": str(real), "FAKE_DROPPER": str(self.tools / "drop.py"),
                     "FAKE_PYTHON": sys.executable, "FAKE_DROP": str(marker)}
        lane = None
        try:
            # Read before the connection that prepares the build host, at the dispatch, and once it is lost.
            with patch.dict(os.environ, variables), \
                    patch.object(remote, "asleep", side_effect=[1000.0, 1000.0, 1300.0]):
                status, output, error = self.eng("remote", "run", "linger")
            lane = int(marker.read_text())
            self.assertEqual(status, 2, output + error)
            self.assertRegex(error, r"eng: No result from fake \(ssh exited 255: Timeout, server fake not responding\.\); "
                                    r"dispatched at \d{4}-\d\d-\d\d \d\d:\d\d:\d\d UTC and lost at \d{4}-\d\d-\d\d "
                                    r"\d\d:\d\d:\d\d UTC, and this Mac slept for 300 s of that\. The run is unmeasured, "
                                    r"and fake stops it once its end of the connection closes\.")
            results = self.project / ".build/engineering/remote/linger"
            self.assertIn("Timeout, server fake not responding.", (results / "output.log").read_text())
            self.assertFalse((results / "source.json").exists())
            self.assertEqual(self.eng("remote", "passed", "linger")[0], 1)
            # Its end of the connection closed, so the build host stopped the run.
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                try: os.kill(lane, 0)
                except ProcessLookupError: break
                time.sleep(0.1)
            with self.assertRaises(ProcessLookupError):
                os.kill(lane, 0)
        finally:
            if lane is not None:
                with contextlib.suppress(ProcessLookupError): os.kill(lane, signal.SIGKILL)

    def test_a_lost_run_names_sleep_only_when_this_mac_slept_and_ssh_only_for_its_own_failure(self):
        output = Path("/build/output.log")
        cases = [((255, "Timeout, server fake not responding.", 10.0, 1300.0), "ssh exited 255: Timeout", "slept for 1290 s"),
                 ((255, "Timeout, server fake not responding.", 10.0, 10.0 + remote.SLEEP_NOTED - 0.5), "ssh exited 255:",
                  None),
                 ((255, None, None, 50.0), "(ssh exited 255);", None),
                 ((1, "a line of the shell's", 10.0, 10.0), "(ssh exited 1);", None)]
        for (status, said, before, after), cause, sleep in cases:
            with self.subTest(status=status, said=said, before=before, after=after), \
                    patch.object(remote, "asleep", return_value=after):
                message = remote.lost("fake", status, said, 0, before, output)
            self.assertIn(cause, message)
            self.assertIn("dispatched at 1970-01-01 00:00:00 UTC and lost at ", message)
            self.assertIn("The run is unmeasured", message)
            if sleep: self.assertIn(f"this Mac {sleep} of that", message)
            else: self.assertNotIn("slept", message)
            if status != 255: self.assertNotIn(said, message)

    def test_a_connection_this_mac_slept_through_says_so_rather_than_that_the_build_host_is_away(self):
        # As at 22:13 UTC on 2026-10-27: the build host accepted eng's first connection, this Mac slept
        # for 210 s with it open, and ssh timed out two minutes after this Mac woke. eng said to wake
        # the build host, which was awake.
        said = "Connection to 192.0.2.7 port 22 timed out"
        with patch.dict(os.environ, {"FAKE_SSH_SAYS": said}), \
                patch.object(remote, "asleep", side_effect=[1000.0, 1210.0]):
            status, output, error = self.eng("remote", "run", "check")
        self.assertEqual(status, 2, output + error)
        self.assertIn(f"eng: fake cannot be reached: {said}; ssh's TCP connection was made, so the address answered, "
                      "and then the SSH exchange timed out; this Mac slept for 210 s while ssh waited, and a Mac "
                      "asleep answers nothing, so the timeout says nothing of fake; to fix it, keep this Mac awake "
                      "while eng connects to fake; `ssh fake true` shows whether fake answers now", error)
        self.assertNotIn("wake fake", error)
        self.assertFalse(self.checkout(0).exists())
        # Awake, the same timeout says only that the build host answered and then fell silent.
        with patch.dict(os.environ, {"FAKE_SSH_SAYS": said}), \
                patch.object(remote, "asleep", side_effect=[1000.0, 1001.0]):
            status, output, error = self.eng("remote", "run", "check")
        self.assertEqual(status, 2, output + error)
        self.assertIn("the SSH exchange timed out; to fix it, see whether fake is overloaded or its sshd is stuck",
                      error)
        self.assertNotIn("slept", error)

    def test_this_macs_sleep_reads_as_a_difference_that_does_not_run_while_it_is_awake(self):
        first = remote.asleep()
        if first is None: self.skipTest("this system has no pair of clocks that differ by sleep")
        time.sleep(0.2)
        # Awake, the clock that runs through sleep and the one that stops for it keep together.
        self.assertLess(abs(remote.asleep() - first), 0.05)

    def test_a_census_that_fails_is_reported_and_stops_no_run(self):
        with patch.object(remote, "census_command", return_value="sh -c 'echo census broke >&2; exit 3'"):
            status, output, error = self.eng("remote", "run", "check")
        self.assertEqual(status, 0, output + error)
        self.assertIn("remote: could not list the processes left over from runs on fake: exit 0: census broke", error)
        self.assertIn(f"check at {self.commit[:12]}", error)
        # A census that runs and finds nothing says nothing.
        status, output, error = self.eng("remote", "run", "check")
        self.assertEqual(status, 0, output + error)
        self.assertNotIn("left over", error)

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

    def lane_files(self, group, lane, place=None):
        """A lane's log and receipt among its group's results: here, or in the directory place names."""
        lanes = (place or self.project / ".build/engineering/remote" / group) / "lanes"
        return (lanes / f"{lane}.log").read_text(), json.loads((lanes / f"{lane}.json").read_text())

    def test_a_group_on_the_build_host_brings_each_lanes_log_and_receipt_back(self):
        results = self.project / ".build/engineering/remote/pair"
        for operation in (["run"], ["remote", "run"]):
            with self.subTest(operation):
                shutil.rmtree(results, ignore_errors=True)
                status, output, error = self.eng(*operation, "pair")
                self.assertEqual(status, 0, output + error)
                # It ran there, and each lane's log and receipt came back beside the group's.
                self.assertFalse((self.project / ".build/engineering/pair").exists())
                log, receipt = self.lane_files("pair", "left")
                self.assertIn("Ran 2 tests", log)
                self.assertEqual({key: receipt[key] for key in ("lane", "status", "outcome", "executedTests",
                                                                "skippedTests", "log")},
                                 {"lane": "left", "status": 0, "outcome": "passed", "executedTests": 2,
                                  "skippedTests": 0, "log": "left.log"})
                # A lane's counts read here, from its log and from its receipt.
                log, receipt = self.lane_files("pair", "right")
                self.assertEqual((evidence.executed_tests(log), evidence.skipped_tests(log)), (2, 1))
                self.assertEqual((receipt["executedTests"], receipt["skippedTests"], receipt["outcome"]),
                                 (2, 1, "passed"))
                kept = json.loads((results / "source.json").read_text())
                self.assertEqual(kept["returned"], ["lanes/left.log", "lanes/left.json", "lanes/right.log",
                                                    "lanes/right.json"])
                self.assertEqual(kept["result"]["receipt"]["lanes"]["right"]["executedTests"], 2)
                self.assertIn(f"remote: the logs and receipts of pair's lanes are in "
                              f"{self.project.resolve()}/.build/engineering/remote/pair/lanes: left, right", error)
        # The negative control: without the copy-back, the lanes' files stay in the build host's checkout.
        there = self.checkout(0) / ".build/engineering/pair"
        shutil.rmtree(results)
        with patch.object(remote, "group_files", return_value=None):
            status, output, error = self.eng("remote", "run", "pair")
        self.assertEqual(status, 0, output + error)
        self.assertTrue((results / "source.json").exists())
        self.assertFalse((results / "lanes").exists())
        self.assertEqual(self.lane_files("pair", "right", there)[1]["executedTests"], 2)
        # A later run that never reaches its lanes sends back nothing an earlier run left there.
        settings = json.loads((self.project / "engineering.json").read_text())
        settings["lanes"]["provision"]["command"] = [sys.executable, "-c", "raise SystemExit(4)"]
        (self.project / "engineering.json").write_text(json.dumps(settings, indent=2))
        commit_all(self.project)
        status, output, error = self.eng("remote", "run", "pair")
        self.assertEqual(status, 2, output + error)
        self.assertIn("could not provision its checkout", error)
        self.assertTrue((results / "run.log").exists())
        self.assertFalse((results / "lanes").exists())
        self.assertFalse((there / "lanes/right.log").exists())

    def test_a_failed_lane_and_one_its_timeout_ended_bring_their_logs_and_receipts_back(self):
        results = self.project / ".build/engineering/remote/mixed"
        status, output, error = self.eng("run", "mixed")
        self.assertEqual(status, 1, output + error)
        self.assertEqual(json.loads((results / "source.json").read_text())["result"]["exitStatus"], 1)
        log, receipt = self.lane_files("mixed", "broken")
        self.assertIn("Ran 1 tests", log)
        self.assertEqual((receipt["status"], receipt["outcome"], receipt["executedTests"]), (5, "failed", 1))
        log, receipt = self.lane_files("mixed", "overdue")
        self.assertIn("waiting", log)
        self.assertEqual((receipt["status"], receipt["outcome"], receipt["executedTests"]), (3, "unmeasured", None))
        self.assertIn("timed out after 1s", receipt["error"])
        self.assertEqual(self.lane_files("mixed", "left")[1]["outcome"], "passed")
        # The negative control: without the copy-back, the lanes' files stay in the build host's checkout.
        shutil.rmtree(results)
        with patch.object(remote, "group_files", return_value=None):
            status, output, error = self.eng("run", "mixed")
        self.assertEqual(status, 1, output + error)
        self.assertTrue((results / "source.json").exists())
        self.assertFalse((results / "lanes").exists())
        there = self.checkout(0) / ".build/engineering/mixed"
        self.assertEqual(self.lane_files("mixed", "broken", there)[1]["outcome"], "failed")

    def stop_group(self):
        """Run `eng run stalled` from a dispatcher of its own, stop it once both of the group's lanes
        run on the build host, and return its output once it has ended."""
        markers = [self.checkout(0) / f".build/{lane}.pid" for lane in ("napper", "dozer")]
        for marker in markers: marker.unlink(missing_ok=True)
        command = [sys.executable, str(self.toolkit / "bin/eng"), "--project", str(self.project), "run", "stalled"]
        dispatcher = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            # A bound for a busy host, not a measure: the markers end the wait.
            deadline = time.monotonic() + 300
            while not all(marker.exists() for marker in markers) and dispatcher.poll() is None \
                    and time.monotonic() < deadline:
                time.sleep(0.1)
            self.assertTrue(all(marker.exists() for marker in markers),
                            dispatcher.stdout.read().decode() if dispatcher.poll() is not None else "")
            lanes = [int(marker.read_text()) for marker in markers]
            dispatcher.send_signal(signal.SIGTERM)
            output = dispatcher.communicate(timeout=300)[0].decode()
        finally:
            if dispatcher.poll() is None:
                dispatcher.kill()
                dispatcher.communicate()
        self.assertEqual(dispatcher.returncode, -signal.SIGTERM, output)
        self.assertIn("asking fake to stop the run", output)
        for lane in lanes:
            with self.assertRaises(ProcessLookupError):
                os.kill(lane, 0)
        return output

    def test_a_stopped_group_brings_its_lanes_logs_and_receipts_back(self):
        results = self.project / ".build/engineering/remote/stalled"
        output = self.stop_group()
        self.assertIn("the logs and receipts of stalled's lanes are in", output)
        for lane in ("napper", "dozer"):
            log, receipt = self.lane_files("stalled", lane)
            self.assertIn(f"{lane} started", log)
            self.assertEqual((receipt["status"], receipt["outcome"]), (None, "stopped"))
            self.assertIn("Stopped by SIGTERM", receipt["error"])
        kept = json.loads((results / "source.json").read_text())
        self.assertTrue(kept["result"]["stopped"])
        self.assertEqual(len(kept["returned"]), 4)
        # The negative control: a toolkit without the copy-back leaves the lanes' files in the build
        # host's checkout. The dispatcher runs the toolkit's commit, so the change is committed.
        source = self.toolkit / "src/python/vocaro_engineering/remote.py"
        text = source.read_text()
        source.write_text(text.replace("def group_files(config, lane):\n",
                                       "def group_files(config, lane):\n    return None\n", 1))
        self.assertNotEqual(source.read_text(), text)
        # The dispatcher wrote bytecode beside the toolkit's source, which stays out of its commit.
        self.assertTrue((source.parent / "__pycache__").is_dir())
        commit_all(self.toolkit)
        self.assertNotIn("__pycache__", git(self.toolkit, "ls-files"))
        shutil.rmtree(results)
        self.stop_group()
        self.assertTrue(json.loads((results / "source.json").read_text())["result"]["stopped"])
        self.assertFalse((results / "lanes").exists())
        there = self.checkout(0) / ".build/engineering/stalled"
        self.assertEqual(self.lane_files("stalled", "napper", there)[1]["outcome"], "stopped")

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

    def test_a_snapshot_holds_an_edit_that_kept_a_files_size_and_second_after_it_was_staged(self):
        # git trusts an entry's stats when the index is newer than the entry, in whole seconds, and
        # otherwise reads the file. Here the staging, the edit and the index all have one past second,
        # as a session's do when it stages a file and rewrites it at its size within a second, and the
        # snapshot comes later. ctime, which no test can set, is left out of git's comparison.
        git(self.project, "config", "core.trustctime", "false")
        edited = self.project / "edited.txt"
        edited.write_text("committed\n")
        base = commit_all(self.project)
        second = (int(time.time()) - 60) * 10**9
        edited.write_text("staged\n")
        os.utime(edited, ns=(second, second))
        git(self.project, "add", "edited.txt")
        edited.write_text("edited\n")
        for path in (edited, self.index()): os.utime(path, ns=(second, second))
        index = self.index().read_bytes()
        commit, head = remote.snapshot(self.project)
        self.assertEqual((git(self.project, "show", f"{commit}:edited.txt"), head), ("edited", base))
        self.assertEqual(self.index().read_bytes(), index)
        # The negative control: a copy of the index that is newer than its entries trusts the stats and
        # holds what was staged.
        copy = Path(self.temp.name) / "newer-index"
        shutil.copyfile(self.index(), copy)
        variables = {**remote.environment(), "GIT_INDEX_FILE": str(copy)}
        remote.git(self.project, "add", "-A", variables=variables)
        tree = remote.git(self.project, "write-tree", variables=variables)
        self.assertEqual(git(self.project, "show", f"{tree}:edited.txt"), "staged")
        # The session's own git, with its own index, reads the edit as the snapshot did.
        self.assertEqual(git(self.project, "diff", "--name-only"), "edited.txt")

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
        self.home = home
        (base / "build-host-tmp").mkdir()
        # Each Mac's device record; the build host's defers physical devices to this Mac, as a run makes it.
        records = durable_scratch()
        self.addCleanup(records.cleanup)
        self.record_here = devices.Record(Path(records.name) / "here" / "device-handoff.json")
        self.record_there = devices.Record(Path(records.name) / "there" / "device-handoff.json")
        self.record_there.defer(build_host.mac_name())
        self.environment = patch.dict(os.environ, {
            "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}", "FAKE_HOME": str(home),
            "FAKE_SHELL": str(tools / "login-shell"), "FAKE_XCODES": str(self.xcodes), "FAKE_MAC": "here",
            "VOCARO_BUILD_HOST": "fake", "FAKE_SLOTS": str(base / "build-host-slots"),
            "FAKE_GATE": str(base / "build-host-gate"), "FAKE_TMPDIR": str(base / "build-host-tmp"),
            devices.OVERRIDE: str(self.record_here.path), "FAKE_DEVICE_RECORD": str(self.record_there.path)})
        self.environment.start()
        os.environ.pop("DEVELOPER_DIR", None)
        # Both Macs select the same Xcode 27.1, with the same runtimes.
        self.release = self.xcode("Xcode.app", here=(RELEASE, RUNTIMES), there=(RELEASE, RUNTIMES))
        self.select(here=self.release, there=self.release)
        # The fake build host is no address; ConnectionTests measure real ones.
        self.reached = {"hostname": "fake.local", "port": 22, "addressFamily": "any",
                        "addresses": [{"address": "192.0.2.7", "seconds": 0.004, "outcome": "connected"}]}
        self.connection = patch.object(remote, "connection", side_effect=lambda host: self.reached)
        self.connection.start()

    def tearDown(self):
        self.connection.stop()
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
        self.assertEqual((status, found["errors"], found["unchecked"], found["leftovers"]), (0, [], [], []), found)
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
            # The fix is the command that brings both Macs to the union, which deletes none.
            self.assertIn("to fix it, run `python3 scripts/eng remote setup --runtimes`, which copies each runtime to "
                          "the Mac that lacks it and deletes none", found["errors"][0])

    def test_device_records_that_do_not_keep_physical_devices_here_alone_are_errors(self):
        name = build_host.mac_name()
        self.assertEqual(self.doctor()[1]["remote"]["deviceRecord"]["deferredTo"], name)
        release_by = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
        window = {"by": "owner", "at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(), "until": release_by}
        with self.subTest("not yet deferred"):
            self.record_there.defer(None)
            status, found = self.doctor()
            self.assertEqual((status, found["errors"]), (0, []), found)
            self.assertEqual(found["unchecked"], ["whether fake's device record refuses physical devices: it defers "
                                                  "them to no Mac until the next eng remote run there"])
        with self.subTest("held there"):
            self.record_there.reserve("iPhone", owner="there", revision=0, purpose="p", release_by=release_by,
                                      device=PHONE, approval=window)
            status, found = self.doctor()
            self.assertEqual(status, 1)
            self.assertIn("fake's device record holds the physical device of iPhone", found["errors"][0])
            self.record_there.release("iPhone", owner="there", revision=1, outcome="o", restoration="r")
        with self.subTest("deferred elsewhere"):
            self.record_there.defer("another-mac")
            status, found = self.doctor()
            self.assertEqual(status, 1)
            self.assertEqual(len(found["errors"]), 1, found)
            self.assertIn(f"defers physical devices to another-mac, not this Mac ({name})", found["errors"][0])
            self.assertIn(f"`devices defer {name} --replace` there", found["errors"][0])
            self.record_there.defer(name, replace=True)
        with self.subTest("this Mac deferring"):
            self.record_here.defer("another-mac")
            status, found = self.doctor()
            self.assertEqual(status, 1)
            self.assertEqual(len(found["errors"]), 1, found)
            self.assertIn("this Mac's device record defers physical devices to another-mac", found["errors"][0])
            self.record_here.defer(None)
        self.assertEqual(self.doctor()[0], 0)

    def test_a_process_left_in_a_checkout_is_listed_and_is_no_error(self):
        checkout = self.home / remote.ROOT / "checkouts/Example/0/Example"
        checkout.mkdir(parents=True)
        left = subprocess.Popen(["sleep", "60"], cwd=checkout)
        # The owner's own process elsewhere is not the toolkit's.
        other = subprocess.Popen(["sleep", "61"], cwd=self.home)
        try:
            status, found = self.doctor()
            start = " ".join(resources.process_start(left.pid).split())
        finally:
            for process in [left, other]:
                process.kill()
                process.wait()
        self.assertEqual((status, found["errors"], found["unchecked"]), (0, [], []), found)
        self.assertEqual([(row["pid"], row["start"], row["command"], row["place"], row["path"])
                          for row in found["leftovers"]], [(left.pid, start, "sleep 60", "checkout", str(checkout.resolve()))])

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

    def test_first_addresses_that_keep_each_connection_waiting_are_an_error(self):
        # As until 2026-10-27: the .local name resolved first to a link-local IPv6 address that never
        # answered, so each connection waited 10 s before trying IPv4.
        dead = {"address": "fe80::1%en0", "seconds": 10.001, "outcome": "no answer in 10 s"}
        used = {"address": "192.0.2.7", "seconds": 0.004, "outcome": "connected"}
        later = {"address": "2001:db8::7", "outcome": "not tried: ssh connects to the one before it"}
        self.reached["addresses"] = [dead, used, later]
        status, found = self.doctor()
        self.assertEqual(status, 1)
        self.assertEqual(found["connection"], self.reached)
        self.assertEqual(len(found["errors"]), 1, found)
        for text in ["ssh tries 1 address of fake.local that does not answer before 192.0.2.7, which answered in 0.00 s: "
                     "fe80::1%en0, no answer in 10 s (10.0 s)", "up to ConnectTimeout's 10 s for it",
                     "add `AddressFamily inet` under `Host fake` in ~/.ssh/config, so ssh tries only IPv4"]:
            self.assertIn(text, found["errors"][0])
        # The rest of the comparison still runs, and passes.
        self.assertEqual((found["unchecked"], found["leftovers"]), ([], []), found)
        with self.subTest("the same family"):
            self.reached["addresses"] = [{**dead, "address": "192.0.2.9"}, used]
            status, found = self.doctor()
            self.assertEqual(status, 1)
            self.assertIn("ssh tries 1 address of fake.local that does not answer before 192.0.2.7", found["errors"][0])
            self.assertIn("make those addresses answer, or name in `Host fake`", found["errors"][0])
            self.assertNotIn("AddressFamily", found["errors"][0])
        with self.subTest("refused at once"):
            # ssh moves on from a refusal at once, so it costs nothing.
            self.reached["addresses"] = [{**dead, "seconds": 0.001, "outcome": "Connection refused"}, used]
            self.assertEqual(self.doctor()[0], 0)
        with self.subTest("the first answers"):
            self.reached["addresses"] = [used, later]
            self.assertEqual(self.doctor()[0], 0)
        with self.subTest("none answers"):
            # The comparison that connects says what is wrong; this adds nothing.
            self.reached["addresses"] = [dead, {**dead, "address": "192.0.2.7"}]
            status, found = self.doctor()
            self.assertEqual((status, found["errors"]), (0, []), found)
        with self.subTest("through a proxy"):
            self.reached = {"hostname": "fake", "port": 22, "addressFamily": "any",
                            "unchecked": "how ssh connects to fake, which it reaches through jump"}
            status, found = self.doctor()
            self.assertEqual((status, found["errors"], found["unchecked"]),
                             (0, [], ["how ssh connects to fake, which it reaches through jump"]), found)


# `ssh -G` for ConnectionTests: the settings FAKE_SETTINGS names, one per line.
SETTINGS_SSH = """#!/bin/sh
[ "$1" = -G ] || exit 255
printf '%b' "$FAKE_SETTINGS"
"""


class ConnectionTests(unittest.TestCase):
    """remote.connection over real sockets on this Mac: a port that listens and one that refuses, with
    getaddrinfo answering for the build host's name."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        tools = Path(self.temp.name) / "bin"
        tools.mkdir()
        (tools / "ssh").write_text(SETTINGS_SSH)
        (tools / "ssh").chmod(0o755)
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(8)
        self.port = self.listener.getsockname()[1]
        closed = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        closed.bind(("127.0.0.1", 0))
        self.closed = closed.getsockname()[1]
        closed.close()
        self.environment = patch.dict(os.environ, {"PATH": f"{tools}{os.pathsep}{os.environ['PATH']}",
                                                   "FAKE_SETTINGS": f"hostname fake.local\\nport {self.port}\\n"})
        self.environment.start()
        self.asked = []

    def tearDown(self):
        self.environment.stop()
        self.listener.close()
        self.temp.cleanup()

    def resolving(self, *ports):
        """getaddrinfo for fake.local: 127.0.0.1 at each of ports, in order."""
        def resolve(name, port, family=0, kind=0, *rest):
            self.asked.append((name, port, family, kind))
            if name != "fake.local": raise socket.gaierror(socket.EAI_NONAME, "nodename nor servname provided")
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", each)) for each in ports]
        return patch.object(remote.socket, "getaddrinfo", side_effect=resolve)

    def test_addresses_are_connected_to_in_order_until_one_answers_as_ssh_does(self):
        with self.resolving(self.closed, self.port, self.port):
            found = remote.connection("build-host")
        self.assertEqual(self.asked, [("fake.local", self.port, socket.AF_UNSPEC, socket.SOCK_STREAM)])
        self.assertEqual((found["hostname"], found["port"], found["addressFamily"]), ("fake.local", self.port, "any"))
        outcomes = [(entry["address"], entry["outcome"]) for entry in found["addresses"]]
        self.assertEqual(outcomes, [("127.0.0.1", "Connection refused"), ("127.0.0.1", "connected"),
                                    ("127.0.0.1", "not tried: ssh connects to the one before it")])
        self.assertNotIn("seconds", found["addresses"][2])
        # A refusal costs ssh nothing, so it is no error.
        self.assertIsNone(remote.slow_connection("build-host", found))

    def test_an_address_that_does_not_answer_keeps_ssh_waiting_for_its_connect_timeout(self):
        real = remote.reach
        def reach(address, family, timeout):
            # The first address takes no connection and sends no refusal, as a link-local address
            # that nothing on the network carries does.
            if address[1] == self.closed: return timeout, f"no answer in {timeout} s"
            return real(address, family, timeout)
        with self.resolving(self.closed, self.port), patch.object(remote, "reach", side_effect=reach):
            found = remote.connection("build-host")
        self.assertEqual([entry["outcome"] for entry in found["addresses"]], ["no answer in 10 s", "connected"])
        self.assertIn("ssh tries 1 address of fake.local that does not answer before 127.0.0.1",
                      remote.slow_connection("build-host", found))

    def test_reach_times_a_connection_and_names_a_refusal_or_silence(self):
        seconds, failure = remote.reach(("127.0.0.1", self.port), socket.AF_INET, 5)
        self.assertIsNone(failure)
        self.assertLess(seconds, 5)
        self.assertEqual(remote.reach(("127.0.0.1", self.closed), socket.AF_INET, 5)[1], "Connection refused")
        with patch.object(socket.socket, "connect", side_effect=socket.timeout("timed out")):
            self.assertEqual(remote.reach(("127.0.0.1", self.port), socket.AF_INET, 3)[1], "no answer in 3 s")

    def test_ssh_settings_choose_the_family_and_a_proxy_or_unresolved_name_is_reported(self):
        with self.subTest("AddressFamily inet"):
            with patch.dict(os.environ, {"FAKE_SETTINGS": f"hostname fake.local\\nport {self.port}\\naddressfamily inet\\n"}), \
                    self.resolving(self.port):
                found = remote.connection("build-host")
            self.assertEqual(self.asked[-1][2], socket.AF_INET)
            self.assertEqual(found["addresses"][0]["outcome"], "connected")
        with self.subTest("a proxy"):
            with patch.dict(os.environ, {"FAKE_SETTINGS": "hostname fake.local\\nproxyjump gateway\\n"}), \
                    self.resolving(self.port):
                asked = len(self.asked)
                found = remote.connection("build-host")
            self.assertEqual(found["unchecked"], "how ssh connects to build-host, which it reaches through gateway")
            self.assertNotIn("addresses", found)
            self.assertEqual(len(self.asked), asked)
        with self.subTest("unresolved"):
            with patch.dict(os.environ, {"FAKE_SETTINGS": "hostname gone.local\\n"}), self.resolving(self.port):
                found = remote.connection("build-host")
            self.assertEqual((found["hostname"], found["port"]), ("gone.local", 22))
            self.assertIn("nodename nor servname provided", found["unresolved"])
            self.assertIsNone(remote.slow_connection("build-host", found))
        with self.subTest("no settings"):
            # ssh that says nothing leaves the host's own name.
            with patch.dict(os.environ, {"FAKE_SETTINGS": ""}), self.resolving(self.port):
                found = remote.connection("fake.local")
            self.assertEqual((found["hostname"], found["port"], found["addressFamily"]), ("fake.local", 22, "any"))

    def test_an_unresolved_local_name_is_named_as_multicast_dns(self):
        local = remote.unreachable("build-host", "ssh: Could not resolve hostname mac.local: nodename nor servname "
                                                 "provided, or not known")
        self.assertIn("build-host cannot be reached: ssh: Could not resolve hostname mac.local", local.problem)
        self.assertIn("mac.local is a multicast DNS name, which resolves only while both Macs are awake on one network",
                      local.fix)
        self.assertIn("`dscacheutil -q host -a name mac.local`", local.fix)
        self.assertIn("until `ssh build-host true` succeeds without a prompt", local.fix)
        for detail in ["ssh: Could not resolve hostname mac.example.com: nodename nor servname provided, or not known",
                       "ssh: connect to host mac.local port 22: Operation timed out", None]:
            with self.subTest(detail):
                other = remote.unreachable("build-host", detail)
                self.assertNotIn("multicast", other.fix)
                self.assertIn("wake build-host and connect it to the network", other.fix)

    def test_a_timeout_says_whether_the_address_answered_and_whether_this_mac_slept_meanwhile(self):
        # ssh's lines for each phase, as OpenSSH 10.3p1 said them against a listener on 127.0.0.1.
        silent = "Connection to 192.0.2.7 port 22 timed out"
        made = "ssh's TCP connection was made, so the address answered, and then"
        cases = [
            ("no answer", "ssh: connect to host 192.0.2.7 port 22: Operation timed out", 1000.0, 1000.0,
             ["nothing answered ssh's TCP connection in ConnectTimeout's 10 s"], ["wake build-host and connect it"],
             made),
            ("no banner", f"{remote.NO_BANNER}\n{silent}", 1000.0, 1000.0,
             [f"{silent}; {made} no SSH banner came in ConnectTimeout's 10 s"],
             ["see whether build-host is overloaded or its sshd is stuck"], "wake build-host"),
            ("silent", silent, 1000.0, 1000.0 + remote.SLEEP_NOTED - 0.5, [f"{made} the SSH exchange timed out"],
             ["see whether build-host is overloaded", "until `ssh build-host true` succeeds"], "slept"),
            ("slept", silent, 1000.0, 1210.0,
             [f"{made} the SSH exchange timed out; this Mac slept for 210 s while ssh waited, and a Mac asleep "
              "answers nothing, so the timeout says nothing of build-host"],
             ["keep this Mac awake while eng connects to build-host", "`ssh build-host true` shows whether"],
             "wake build-host"),
            ("slept resolving", "ssh: Could not resolve hostname mac.local: nodename nor servname provided, or not "
             "known", 1000.0, 1300.0, ["this Mac slept for 300 s while ssh waited"], ["keep this Mac awake"],
             "multicast"),
            ("unread", silent, None, 1210.0, [f"{made} the SSH exchange timed out"], ["see whether"], "slept"),
            ("unreadable", silent, 1000.0, None, [f"{made} the SSH exchange timed out"], ["see whether"], "slept"),
        ]
        for name, detail, before, after, problem, fix, absent in cases:
            with self.subTest(name), patch.object(remote, "asleep", return_value=after) as clock:
                found = remote.unreachable("build-host", detail, before)
            self.assertTrue(found.problem.startswith("build-host cannot be reached: "), found.problem)
            for text in problem: self.assertIn(text, found.problem)
            for text in fix: self.assertIn(text, found.fix)
            self.assertNotIn(absent, found.problem + found.fix)
            # Without a reading from when ssh started, there is nothing to compare.
            if before is None: clock.assert_not_called()


# Starts as a fake ps listing prints them: the day of the month is padded, which a guard's record
# and a slot lock's are not.
EARLY, LATER, PADDED = "Mon Oct 26 03:12:45 2026", "Tue Oct 27 10:40:01 2026", "Wed Oct  7 09:05:03 2026"
# When a run admitted at LATER booted its simulator, and when its test run's xcodebuild started DTServiceHub.
BOOTED, TESTING = "Tue Oct 27 10:40:03 2026", "Tue Oct 27 10:43:12 2026"
DTSERVICEHUB = "/Applications/Xcode.app/Contents/SharedFrameworks/DVTInstrumentsFoundation.framework/Resources/DTServiceHub"
DEVICE = "0A1B2C3D-4E5F-4A6B-8C7D-9E0F1A2B3C4D"


class LeftoverCensusTests(unittest.TestCase):
    """build_host.leftovers over fake ps and lsof listings of a build host whose home is a temporary
    directory, with its own admission guard and log."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name).resolve()
        self.home = base / "home"
        self.checkout = self.home / remote.ROOT / "checkouts/Example/0/Example"
        self.checkout.mkdir(parents=True)
        self.slots = base / "slots"
        self.slots.mkdir()
        # As the build host's TMPDIR names it; lsof names the same directory under /private.
        self.temporary = "/var/folders/zz/fake/T/"
        self.record = base / "device-handoff.json"
        self.environment = patch.dict(os.environ, {"HOME": str(self.home), "TMPDIR": self.temporary,
                                                   "HEAVY_SLOT_DIR": str(self.slots), "SW_GATE_LOCK_FILE": str(base / "gate"),
                                                   "VOCARO_DEVICE_RECORD": str(self.record)})
        self.environment.start()
        self.guard = Path(os.environ["SW_GATE_LOCK_FILE"] + ".warm-push-guard")
        self.log = self.slots / "admissions.jsonl"
        self.uid = os.getuid()
        self.table, self.directories, self.writes = {}, {}, {}

    def tearDown(self):
        self.environment.stop()
        self.temp.cleanup()

    def process(self, pid, parent, args, cwd=None, start=EARLY, uid=None, written=0):
        self.table[pid] = (parent, self.uid if uid is None else uid, start, args)
        if cwd is None: self.directories.pop(pid, None)
        else: self.directories[pid] = cwd
        self.writes[pid] = written

    def listed(self, command):
        if command[0] == "ps":
            return "".join(f"{pid:>6} {parent:>6} {uid:>5} {start} {args}\n"
                           for pid, (parent, uid, start, args) in self.table.items())
        if command[0] == "lsof":
            # lsof lists the census's own process too.
            return "".join(f"p{pid}\nfcwd\nn{cwd}\n" for pid, cwd in [(os.getpid(), self.home), *self.directories.items()])
        return None

    def census(self):
        found = build_host.leftovers(remote.ROOT, listed=self.listed, written=self.writes.get)
        self.assertEqual(found["unchecked"], [])
        return {row["pid"]: row for row in found["leftovers"]}

    def owner(self, pid, start):
        self.guard.write_text(json.dumps({"vocaroEngineering": 1, "owners": {"a" * 32: {
            "pid": pid, "start": start, "label": "swift build", "exclusive": False, "model": False}}}))

    def admission(self, pid, start, *events):
        """The admission log's lines for pid's admission, each event that many seconds after start."""
        lines = [{"time": datetime.fromtimestamp(build_host.started(start) + after, timezone.utc).isoformat(timespec="seconds"),
                  "event": event, "identity": "c" * 32, "pid": pid, "kind": "heavy", "label": "Example ios-tests",
                  **({"slots": [0], "waited": 0.01} if event == "admitted" else
                     {"outcome": "released", "slots": [0], "held": 400.0})} for event, after in events]
        self.log.write_text("".join(json.dumps(line) + "\n" for line in lines))

    def reserve(self, udid, **entry):
        """A device record whose pool member udid is reserved, as simulators/pool.py reserves it."""
        self.record.write_text(json.dumps({"vocaroDeviceHandoff": 1, "revision": 1, "devices": {"Example iPhone 17 Pro 1": {
            "revision": 1, "device": udid, "ownerChatID": "simulator-tests-1", "state": "reserved", "activeRun": False,
            "purpose": "eng simulator-tests Example", **entry}}}))

    def ios_tests(self):
        """An ios-tests lane's run in the checkout, admitted at LATER, as the build host runs one: eng
        simulator-tests names its devices by their kind, and CoreSimulator starts the reserved member's
        launchd_sim under launchd, with the simulator's processes below it."""
        self.process(5000, 900, f"/usr/bin/python3 {self.checkout.parent}/vocaro-engineering/bin/eng --project "
                     f"{self.checkout} run ios-tests", cwd=str(self.checkout), start=LATER)
        self.process(5010, 5000, "/usr/bin/python3 scripts/eng simulator-tests --scheme Example --runtime "
                     "com.apple.CoreSimulator.SimRuntime.iOS-27-0 --device iPhone-17-Pro", cwd=str(self.checkout), start=LATER)
        devices = self.home / build_host.DEVICES
        self.process(7000, 1, f"launchd_sim {devices}/{DEVICE}/data/var/run/launchd_bootstrap.plist", start=BOOTED)
        self.process(7001, 7000, "/Library/Developer/CoreSimulator/Volumes/iOS_24B5070a/Library/Developer/"
                     "CoreSimulator/Profiles/Runtimes/iOS 27.0.simruntime/Contents/Resources/RuntimeRoot/System/Library/"
                     "CoreServices/SpringBoard.app/SpringBoard", start=BOOTED)
        self.process(7002, 7000, f"{devices}/{DEVICE}/data/Containers/Bundle/Application/X/Example.app/Example",
                     start=BOOTED, written=1 << 20)
        # The test runner in the simulator names the bundle the run built in its checkout.
        self.process(7003, 7000, f"xctest {self.checkout}/.build/ios-validation/Build/Products/Debug-iphonesimulator/"
                     "ExampleTests.xctest", start=BOOTED)
        self.admission(5000, LATER, ("admitted", 1))

    def test_an_orphan_is_listed_with_its_start_command_and_writes_unless_a_live_run_holds_it(self):
        self.process(4242, 1, "python3 -m unittest discover -s tests", cwd=str(self.checkout), written=1_700_000_000_000)
        self.process(4250, 4242, "/usr/bin/python3 writer.py", cwd="/private/tmp", written=3 << 30)
        # Named by its command line alone, as a compiler names its sources.
        self.process(4300, 1, f"/usr/bin/swift-frontend -frontend -c {self.checkout}/Sources/A.swift -o A.o")
        rows = self.census()
        self.assertEqual(list(rows), [4242, 4300])
        row = rows[4242]
        self.assertEqual((row["parent"], row["start"], row["command"], row["place"], row["path"], row["written"]),
                         (1, EARLY, "python3 -m unittest discover -s tests", "checkout", str(self.checkout),
                          1_700_000_000_000))
        self.assertEqual(row["descendants"], [{"pid": 4250, "command": "/usr/bin/python3 writer.py", "written": 3 << 30}])
        self.assertEqual((rows[4300]["path"], rows[4300]["cwd"]), (f"{self.checkout}/Sources/A.swift", None))
        # Negative control: under a live run of build_host.py, which its checkout's slot lock records.
        self.process(900, 1, "python3 -c SOURCE PLAN", cwd=str(self.home))
        self.process(4242, 900, "python3 -m unittest discover -s tests", cwd=str(self.checkout))
        self.process(4300, 4242, f"/usr/bin/swift-frontend -frontend -c {self.checkout}/Sources/A.swift -o A.o")
        record = self.checkout.parents[1] / "0.lock"
        record.write_text(json.dumps({"pid": 900, "start": EARLY}) + "\n")
        self.assertEqual(self.census(), {})
        # A record whose PID another process now has is no live run's.
        record.write_text(json.dumps({"pid": 900, "start": LATER}) + "\n")
        self.assertEqual(list(self.census()), [4242])

    def test_an_admitted_runs_processes_are_not_listed_until_its_admission_ends(self):
        self.process(5000, 1, f"/usr/bin/python3 {self.checkout.parent}/vocaro-engineering/bin/eng admit -- swift build",
                     cwd=str(self.checkout), start=PADDED)
        self.process(5001, 5000, f"swift-build --package-path {self.checkout}", start=PADDED, written=5 << 30)
        with self.subTest("guard"):
            self.owner(5000, "Wed Oct 7 09:05:03 2026")
            self.assertEqual(self.census(), {})
            # Negative control: an owner whose PID another process took over accounts for nothing.
            self.owner(5000, "Wed Oct 7 09:05:04 2026")
            rows = self.census()
            self.assertEqual(list(rows), [5000])
            self.assertEqual([descendant["pid"] for descendant in rows[5000]["descendants"]], [5001])
            self.guard.unlink()
        with self.subTest("log"):
            began = build_host.started("Wed Oct 7 09:05:03 2026")
            def line(event, after, **fields):
                moment = datetime.fromtimestamp(began + after, timezone.utc).isoformat(timespec="seconds")
                return json.dumps({"time": moment, "event": event, "identity": "b" * 32, "pid": 5000, "kind": "heavy",
                                   "label": "swift build", **fields}) + "\n"
            self.log.write_text(line("admitted", 2, slots=[0], waited=0.5))
            self.assertEqual(self.census(), {})
            # Negative control: its end, logged in the newer generation, leaves nothing accounting for it.
            self.log.rename(self.slots / "admissions.1.jsonl")
            self.log.write_text(line("ended", 90, outcome="uncertain", slots=[0], held=88.0))
            self.assertEqual(list(self.census()), [5000])
            # And an admission older than the process is another process's, whose PID it took over.
            (self.slots / "admissions.1.jsonl").unlink()
            self.log.write_text(line("admitted", -3600, slots=[0], waited=0.5))
            self.assertEqual(list(self.census()), [5000])

    def test_the_owners_other_processes_are_not_listed_unless_they_run_from_a_runs_places(self):
        self.process(6000, 1, "/Applications/Safari.app/Contents/MacOS/Safari", cwd=str(self.home))
        # macOS's own agent in the temporary directory, as IMTransferAgent works on both Macs.
        self.process(6001, 1, "/System/Library/PrivateFrameworks/IMTransferServices.framework/IMTransferAgent.app/"
                     "Contents/MacOS/IMTransferAgent", cwd="/private" + self.temporary + "com.apple.imtransferservices")
        # Another account's shell in a checkout.
        self.process(6002, 1, "-zsh", cwd=str(self.checkout), uid=self.uid + 1)
        self.assertEqual(self.census(), {})
        # Negative controls: the same program working in a checkout, and the owner's own in the temporary directory.
        self.process(6000, 1, "/Applications/Safari.app/Contents/MacOS/Safari", cwd=str(self.checkout))
        self.process(6003, 1, "/usr/bin/python3 helper.py", cwd="/private" + self.temporary + "tmpabc")
        rows = self.census()
        self.assertEqual(sorted(rows), [6000, 6003])
        self.assertEqual((rows[6003]["place"], rows[6003]["path"]),
                         ("temporary directory", "/private" + self.temporary + "tmpabc"))

    def test_a_simulator_no_live_run_names_is_listed_with_its_processes(self):
        devices = self.home / build_host.DEVICES
        self.process(7000, 1, f"launchd_sim {devices}/{DEVICE}/data/var/run/launchd_bootstrap.plist", written=10 << 20)
        self.process(7001, 7000, "/Library/Developer/CoreSimulator/Volumes/iOS_24B5070a/Library/Developer/"
                     "CoreSimulator/Profiles/Runtimes/iOS 27.1.simruntime/Contents/Resources/RuntimeRoot/System/Library/"
                     "CoreServices/SpringBoard.app/SpringBoard", written=1 << 20)
        self.process(7002, 7000, f"{devices}/{DEVICE}/data/Containers/Bundle/Application/X/Example.app/Example",
                     written=200 << 30)
        rows = self.census()
        self.assertEqual(list(rows), [7000])
        self.assertEqual((rows[7000]["place"], rows[7000]["simulator"]), ("simulator", DEVICE))
        self.assertEqual([descendant["pid"] for descendant in rows[7000]["descendants"]], [7002, 7001])
        # Negative control: a live admitted run that names the device by its identifier accounts for it.
        self.process(5000, 1, "/usr/bin/python3 eng admit -- xcodebuild test", start=LATER)
        self.process(5001, 5000, f"xcodebuild test -destination id={DEVICE.lower()}", start=LATER)
        self.owner(5000, LATER)
        self.assertEqual(self.census(), {})

    def test_a_simulator_the_device_record_holds_for_a_live_run_is_not_listed_until_its_admission_ends(self):
        self.ios_tests()
        # The pool records the process that reserved the member, however its start is spaced.
        self.reserve(DEVICE, **{devices.HOLDER: {"pid": 5010, "start": "Tue Oct 27  10:40:01  2026"}})
        self.assertEqual(self.census(), {})
        with self.subTest("an active run's process"):
            self.reserve(DEVICE, activeRun=True, hostProcessIdentifier=5010, hostProcessStart=LATER)
            self.assertEqual(self.census(), {})
            # Negative control: an entry that records no active run names no process.
            self.reserve(DEVICE, activeRun=False, hostProcessIdentifier=5010, hostProcessStart=LATER)
            self.assertEqual(list(self.census()), [7000])
        self.reserve(DEVICE, **{devices.HOLDER: {"pid": 5010, "start": LATER}})
        # Negative controls. The device's launchd_sim is listed, with every process below it, once
        # the admission has ended and its processes are gone, though the record still holds the member.
        self.admission(5000, LATER, ("admitted", 1), ("ended", 455))
        del self.table[5000], self.table[5010]
        rows = self.census()
        self.assertEqual(list(rows), [7000])
        self.assertEqual((rows[7000]["parent"], rows[7000]["place"], rows[7000]["simulator"]), (1, "simulator", DEVICE))
        self.assertEqual(sorted(descendant["pid"] for descendant in rows[7000]["descendants"]), [7001, 7002, 7003])
        # So it is while the holder runs after its admission ended: no live run is left to hold the device.
        self.ios_tests()
        self.admission(5000, LATER, ("admitted", 1), ("ended", 455))
        rows = self.census()
        self.assertIn(7000, rows)
        self.assertNotIn(7003, rows)
        # And while the admission is live, for a holder whose PID another process took over, and for a released entry.
        self.admission(5000, LATER, ("admitted", 1))
        self.reserve(DEVICE, **{devices.HOLDER: {"pid": 5010, "start": EARLY}})
        self.assertEqual(list(self.census()), [7000])
        self.reserve(DEVICE, **{devices.HOLDER: {"pid": 5010, "start": LATER}, "state": "released"})
        self.assertEqual(list(self.census()), [7000])

    def test_an_xcode_service_that_started_in_a_checkout_during_a_live_admission_there_is_not_listed_until_it_ends(self):
        self.ios_tests()
        del self.table[7000], self.table[7001], self.table[7002], self.table[7003]
        # xcodebuild has ended, and launchd has adopted the DTServiceHub it started in the checkout.
        self.process(8000, 1, DTSERVICEHUB, cwd=str(self.checkout), start=TESTING, written=4 << 10)
        self.assertEqual(self.census(), {})
        with self.subTest("the guard's owner, without the log"):
            self.log.unlink()
            self.owner(5000, LATER)
            self.assertEqual(self.census(), {})
            self.guard.unlink()
            self.admission(5000, LATER, ("admitted", 1))
        # Negative controls, while the admission is live: one that started before the admission, from
        # an earlier run; one in another checkout; and a program of the run's that is not Xcode's.
        self.process(8001, 1, DTSERVICEHUB, cwd=str(self.checkout), start=EARLY)
        other = self.checkout.parents[1] / "1/Example"
        self.process(8002, 1, DTSERVICEHUB, cwd=str(other), start=TESTING)
        self.process(8003, 1, "/usr/bin/python3 writer.py", cwd=str(self.checkout), start=TESTING)
        self.assertEqual(sorted(self.census()), [8001, 8002, 8003])
        # Once the admission has ended, the run's own DTServiceHub is listed too.
        for pid in (8001, 8002, 8003): del self.table[pid]
        self.admission(5000, LATER, ("admitted", 1), ("ended", 455))
        del self.table[5000], self.table[5010]
        rows = self.census()
        self.assertEqual(list(rows), [8000])
        self.assertEqual((rows[8000]["place"], rows[8000]["cwd"], rows[8000]["written"]),
                         ("checkout", str(self.checkout), 4 << 10))

    def test_a_census_that_cannot_read_says_so_and_one_that_fails_says_why(self):
        self.assertEqual(build_host.leftovers(remote.ROOT, listed=lambda command: None), {"error": "ps listed no processes"})
        # Without lsof's working directories, only command lines place a process.
        self.process(4242, 1, "python3 -m unittest", cwd=str(self.checkout))
        found = build_host.leftovers(remote.ROOT, listed=lambda command: self.listed(command) if command[0] == "ps" else None,
                                     written=self.writes.get)
        self.assertEqual(found["leftovers"], [])
        self.assertEqual(len(found["unchecked"]), 1, found)
        self.assertIn("working directories, which lsof did not list", found["unchecked"][0])
        # A device record that cannot be read holds no simulator for a run, and says so.
        del self.table[4242]
        self.ios_tests()
        self.reserve(DEVICE, **{devices.HOLDER: {"pid": 5010, "start": LATER}})
        self.assertEqual(self.census(), {})
        self.record.write_text("{")
        found = build_host.leftovers(remote.ROOT, listed=self.listed, written=self.writes.get)
        self.assertEqual([row["pid"] for row in found["leftovers"]], [7000])
        self.assertEqual(len(found["unchecked"]), 1, found)
        self.assertIn(f"the device record {self.record}, which could not be read", found["unchecked"][0])
        with patch.object(build_host, "leftovers", side_effect=OSError("broken")):
            self.assertEqual(build_host.answer({"leftovers": remote.ROOT}), {"error": "OSError: broken"})

    @unittest.skipUnless(sys.platform == "darwin", "proc_pid_rusage is macOS's")
    def test_disk_writes_count_what_a_process_has_written(self):
        script = ("import os, sys, time; stream = open(sys.argv[1], 'wb'); stream.write(os.urandom(int(sys.argv[2]))); "
                  "stream.flush(); print('written', flush=True); time.sleep(30)")
        found = {}
        for size in [4 << 20, 0]:
            child = subprocess.Popen([sys.executable, "-c", script, str(Path(self.temp.name) / f"{size}.bin"), str(size)],
                                     stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(child.stdout.readline(), "written\n")
                found[size] = build_host.disk_writes(child.pid)
            finally:
                child.kill()
                child.communicate()
        self.assertGreaterEqual(found[4 << 20], 4 << 20)
        # Negative control: a process that wrote nothing has written less than that.
        self.assertLess(found[0], 4 << 20)
        # Another account's process, here launchd's, cannot be read without sudo.
        self.assertIsNone(build_host.disk_writes(1))


# Two Macs' simulator runtimes on one file system, for eng remote setup --runtimes. FAKE_MAC names
# the Mac a command runs on, and FAKE_RUNTIMES/MAC holds its runtime images as simctl's storage
# holds cryptexes: NAME.asset/AssetData/Restore/BUILD.dmg, a file whose first line is the runtime
# it holds and whose size is its image's. An image whose record says unreadable is listed with
# another size, so no file is its image. FAKE_RUNTIMES/catalog holds what xcodebuild downloads, and
# FAKE_ADD_FAIL makes simctl refuse every image it is given. Nothing reaches the real runtimes.
RUNTIME_XCRUN = """#!PYTHON
import json, os, pathlib, shutil, sys
runtimes = pathlib.Path(os.environ["FAKE_RUNTIMES"])
store, arguments = runtimes / os.environ["FAKE_MAC"], sys.argv[1:]

def held():
    for image in sorted(store.glob("*.asset/AssetData/Restore/*.dmg")):
        with image.open("rb") as file: yield image, json.loads(file.readline())

def install(image, record):
    target = store / f"{record['build']}.asset/AssetData/Restore/{record['build']}.dmg"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(image, target)

def log(name):
    with (store / name).open("a") as file: print(" ".join(arguments), file=file)

if arguments == ["xcodebuild", "-version"]:
    version = store / "version"
    print(version.read_text().strip() if version.exists() else "Xcode 27.2\\nBuild version 27B48")
elif arguments[:2] == ["xcodebuild", "-downloadPlatform"]:
    log("downloads.log")
    build = arguments[arguments.index("-buildVersion") + 1]
    offered = runtimes / "catalog" / f"{build}.asset/AssetData/Restore/{build}.dmg"
    if not offered.exists():
        print("xcodebuild: error: no such runtime is offered", file=sys.stderr)
        sys.exit(70)
    print("Downloading iOS Simulator: 100%")
    with offered.open("rb") as file: install(offered, json.loads(file.readline()))
elif arguments == ["simctl", "list", "runtimes", "-j"]:
    print(json.dumps({"runtimes": [{"name": record["name"], "buildversion": record["build"], "isAvailable": True}
                                   for _, record in held()]}))
elif arguments == ["simctl", "runtime", "list", "-j"]:
    print(json.dumps({record["build"]: {
        "build": record["build"], "identifier": record["build"], "kind": "Patchable Cryptex Disk Image",
        "path": str(image.parents[1]), "sizeBytes": image.stat().st_size + bool(record.get("unreadable")),
        "state": "Ready", "version": record["name"].split()[-1], "supportedArchitectures": ["arm64"],
        "platformIdentifier": "com.apple.platform.iphonesimulator",
        "runtimeBundlePath": f"/Library/Developer/CoreSimulator/Volumes/{record['build']}/{record['name']}.simruntime",
    } for image, record in held()}))
elif arguments[:3] == ["simctl", "runtime", "add"]:
    log("adds.log")
    if os.environ.get("FAKE_ADD_FAIL"):
        print("simctl: the image could not be verified", file=sys.stderr)
        sys.exit(1)
    image = pathlib.Path(arguments[3])
    with image.open("rb") as file: install(image, json.loads(file.readline()))
    if "--move" in arguments: image.unlink()
else:
    sys.exit(1)
"""
# ssh to the build host, which is the other Mac. FAKE_CUT drops the connection once that many bytes
# have gone either way.
RUNTIME_SSH = """#!/bin/sh
while [ $# -gt 0 ]; do
    case "$1" in
        -o|-p|-i|-l|-F|-J) shift 2 ;;
        -*) shift ;;
        *) break ;;
    esac
done
shift
cd "$FAKE_HOME" || exit 255
if [ -n "$FAKE_CUT" ]; then
    head -c "$FAKE_CUT" | FAKE_MAC=there HOME="$FAKE_HOME" SHELL="$FAKE_SHELL" /bin/sh -c "$*" | head -c "$FAKE_CUT"
    exit
fi
FAKE_MAC=there HOME="$FAKE_HOME" SHELL="$FAKE_SHELL" exec /bin/sh -c "$*"
"""
# A runtime image's bytes in these tests, and where a copy drops the connection: past every answer,
# short of an image.
IMAGE = 1 << 20
CUT = 300000
# What each Mac held on 2026-10-26: this Mac four runtimes the build host lacked, and the build host
# a beta this Mac lacked.
HERE_RUNTIMES = [("iOS 26.5", "23F73"), ("iOS 27.0", "24A434"), ("iOS 27.1", "24A94401"), ("iOS 27.2", "24B5110a"),
                 ("iOS 27.2", "24B91011")]
THERE_RUNTIMES = [("iOS 27.0", "24A434"), ("iOS 27.0", "24A5423a")]
EVERY_RUNTIME = sorted(f"{name} ({build})" for name, build in {*HERE_RUNTIMES, *THERE_RUNTIMES})
XCODE = "Xcode 27.2, Build version 27B48"


class RemoteRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name).resolve()
        # The build host's home and this Mac's, each with the staging folder a copy to it uses.
        self.home, self.local = base / "build-host-home", base / "home"
        self.home.mkdir()
        self.local.mkdir()
        self.staging = {"here": self.local / remote.LOCAL_RUNTIME_STAGING, "there": self.home / remote.RUNTIME_STAGING}
        tools = base / "bin"
        tools.mkdir()
        for name, text in [("ssh", RUNTIME_SSH), ("login-shell", FAKE_SHELL),
                           ("xcrun", RUNTIME_XCRUN.replace("PYTHON", sys.executable, 1)),
                           ("xcode-select", '#!/bin/sh\n[ "$1" = -p ] && echo "$FAKE_DEVELOPER"\n')]:
            (tools / name).write_text(text)
            (tools / name).chmod(0o755)
        system = Path("/usr/bin/python3")
        (tools / "python3").symlink_to(system if sys.platform == "darwin" and system.exists() else sys.executable)
        self.runtimes = base / "runtimes"
        for mac in ("here", "there", "catalog"): (self.runtimes / mac).mkdir(parents=True)
        self.developer = base / "Xcode.app/Contents/Developer"
        self.developer.mkdir(parents=True)
        self.project = base / "example"
        self.project.mkdir()
        (self.project / "engineering.json").write_text(json.dumps({"schemaVersion": 1, "name": "Example"}))
        self.environment = patch.dict(os.environ, {
            "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}", "FAKE_HOME": str(self.home), "HOME": str(self.local),
            "FAKE_SHELL": str(tools / "login-shell"), "FAKE_RUNTIMES": str(self.runtimes), "FAKE_MAC": "here",
            "FAKE_DEVELOPER": str(self.developer), "VOCARO_BUILD_HOST": "fake"})
        self.environment.start()
        for name in ("DEVELOPER_DIR", "FAKE_CUT", "FAKE_ADD_FAIL"): os.environ.pop(name, None)
        # The real volumes' free space decides nothing here; the space test sets its own margin.
        self.patches = [patch.object(remote, "developer_directory", return_value=str(self.developer)),
                        patch.object(remote, "RUNTIME_MARGIN", 0)]
        for item in self.patches: item.start()

    def tearDown(self):
        for item in self.patches: item.stop()
        self.environment.stop()
        self.temp.cleanup()

    def image(self, mac, name, build, **record):
        """An image of the runtime NAME (BUILD) in a Mac's store, or the catalog's."""
        path = self.runtimes / mac / f"{build}.asset/AssetData/Restore/{build}.dmg"
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"name": name, "build": build, **record}).encode() + b"\n"
        path.write_bytes(line + build.encode().ljust(IMAGE - len(line), b"\0"))
        return path

    def give(self, mac, runtimes):
        for name, build in runtimes: self.image(mac, name, build)

    def held(self, mac):
        """The runtimes in a Mac's store, as simctl lists them."""
        found = []
        for path in (self.runtimes / mac).glob("*.asset/AssetData/Restore/*.dmg"):
            with path.open("rb") as file: record = json.loads(file.readline())
            found.append(f"{record['name']} ({record['build']})")
        return sorted(found)

    def logged(self, mac, name):
        path = self.runtimes / mac / name
        return path.read_text().splitlines() if path.exists() else []

    def staged(self, mac):
        """What a copy left in the Mac's staging folder, but the lock."""
        folder = self.staging[mac]
        return sorted(path.name for path in folder.iterdir() if path.name != ".lock") if folder.exists() else []

    def eng(self, *arguments):
        with contextlib.redirect_stdout(StringIO()) as output, contextlib.redirect_stderr(StringIO()) as error:
            status = cli.main(["--project", str(self.project), "remote", "setup", "--host", "fake", "--runtimes",
                               *arguments])
        return status, output.getvalue(), error.getvalue()

    def same(self, runtimes):
        return (f"runtimes: this Mac and fake list the same {len(runtimes)} runtimes of {XCODE} at {self.developer}: "
                f"{', '.join(runtimes)}")

    def test_matching_macs_need_nothing(self):
        self.give("here", THERE_RUNTIMES)
        self.give("there", THERE_RUNTIMES)
        status, output, error = self.eng()
        self.assertEqual(status, 0, output + error)
        self.assertEqual(output.strip(), self.same(["iOS 27.0 (24A434)", "iOS 27.0 (24A5423a)"]))
        self.assertEqual(self.logged("here", "adds.log") + self.logged("there", "adds.log"), [])
        # What an earlier copy left staged, as a reboot leaves it, the next run removes on both Macs.
        for mac in ("here", "there"): (self.staging[mac] / "24A5423a.dmg.partial").write_bytes(b"part")
        status, output, error = self.eng()
        self.assertEqual(status, 0, output + error)
        for side in ("this Mac", "fake"):
            self.assertIn(f"runtimes: removed 24A5423a.dmg.partial, which an earlier copy left staged on {side}", output)
        self.assertEqual((self.staged("here"), self.staged("there")), ([], []))

    def test_a_runtime_only_here_is_copied_there_and_added(self):
        self.give("here", [("iOS 27.0", "24A434"), ("iOS 27.1", "24A94401")])
        self.give("there", [("iOS 27.0", "24A434")])
        status, output, error = self.eng()
        self.assertEqual(status, 0, output + error)
        for line in ["runtimes: iOS 27.1 (24A94401), 0.0 GiB: copying from this Mac to fake",
                     "runtimes: iOS 27.1 (24A94401): 0.0 GiB of 0.0 GiB copied",
                     "runtimes: iOS 27.1 (24A94401): added on fake",
                     self.same(["iOS 27.0 (24A434)", "iOS 27.1 (24A94401)"])]:
            self.assertIn(line, output)
        self.assertTrue(output.strip().endswith(self.same(["iOS 27.0 (24A434)", "iOS 27.1 (24A94401)"])), output)
        # Added from the build host's staging folder with --move, which leaves no second copy there.
        self.assertEqual(self.logged("there", "adds.log"),
                         [f"simctl runtime add {self.staging['there'] / '24A94401.dmg'} --move"])
        self.assertEqual(self.staged("there"), [])
        self.assertEqual(self.held("there"), self.held("here"))

    def test_a_runtime_only_there_is_copied_here_and_added(self):
        self.give("here", [("iOS 27.0", "24A434")])
        self.give("there", THERE_RUNTIMES)
        status, output, error = self.eng()
        self.assertEqual(status, 0, output + error)
        self.assertIn("runtimes: iOS 27.0 (24A5423a), 0.0 GiB: copying from fake to this Mac", output)
        self.assertIn("runtimes: iOS 27.0 (24A5423a): added on this Mac", output)
        self.assertEqual(self.logged("here", "adds.log"),
                         [f"simctl runtime add {self.staging['here'] / '24A5423a.dmg'} --move"])
        self.assertEqual(self.staged("here"), [])
        self.assertEqual(self.held("here"), ["iOS 27.0 (24A434)", "iOS 27.0 (24A5423a)"])

    def test_both_directions_at_once_give_both_macs_the_union_and_delete_nothing(self):
        self.give("here", HERE_RUNTIMES)
        self.give("there", THERE_RUNTIMES)
        status, output, error = self.eng()
        self.assertEqual(status, 0, output + error)
        self.assertEqual(output.count("added on fake"), 4, output)
        self.assertEqual(output.count("added on this Mac"), 1, output)
        self.assertTrue(output.strip().endswith(self.same(EVERY_RUNTIME)), output)
        self.assertEqual((self.held("here"), self.held("there")), (EVERY_RUNTIME, EVERY_RUNTIME))
        # Each copy is its source's image, byte for byte.
        for mac, other, build in [("there", "here", "24B5110a"), ("here", "there", "24A5423a")]:
            path = f"{build}.asset/AssetData/Restore/{build}.dmg"
            self.assertEqual((self.runtimes / mac / path).read_bytes(), (self.runtimes / other / path).read_bytes())
        self.assertEqual((self.staged("here"), self.staged("there")), ([], []))
        # Run again, there is nothing to do.
        status, output, error = self.eng()
        self.assertEqual((status, output.strip()), (0, self.same(EVERY_RUNTIME)), output + error)

    def test_check_says_what_it_would_do_and_changes_nothing(self):
        self.give("here", HERE_RUNTIMES)
        self.give("there", THERE_RUNTIMES)
        status, output, error = self.eng("--check")
        self.assertEqual(status, 1, output + error)
        for runtime in ["iOS 26.5 (23F73)", "iOS 27.1 (24A94401)", "iOS 27.2 (24B5110a)", "iOS 27.2 (24B91011)"]:
            self.assertIn(f"runtimes: {runtime}, 0.0 GiB: would be copied from this Mac to fake, which has ", output)
        self.assertIn("runtimes: iOS 27.0 (24A5423a), 0.0 GiB: would be copied from fake to this Mac, which has ", output)
        self.assertIn("runtimes: 5 runtime(s) differ; `eng remote setup --runtimes` brings both Macs to their union",
                      output)
        self.assertNotIn("copying", output)
        self.assertEqual((self.held("here"), self.held("there")),
                         (sorted(f"{n} ({b})" for n, b in HERE_RUNTIMES), sorted(f"{n} ({b})" for n, b in THERE_RUNTIMES)))
        self.assertEqual(self.logged("here", "adds.log") + self.logged("there", "adds.log"), [])
        # Not even a staging folder or the lock is made.
        self.assertFalse(self.staging["here"].exists())
        self.assertFalse(self.staging["there"].exists())

    def test_a_copy_that_fails_adds_nothing_and_leaves_nothing_staged(self):
        self.give("here", [("iOS 27.0", "24A434"), ("iOS 27.1", "24A94401")])
        self.give("there", THERE_RUNTIMES)
        before = (self.held("here"), self.held("there"))
        for failure, variables, lines in [
                ("dropped", {"FAKE_CUT": str(CUT)},
                 [f"runtimes: iOS 27.1 (24A94401): not added on fake: received {CUT} of its {IMAGE} bytes; nothing of it "
                  "stays staged there",
                  f"runtimes: iOS 27.0 (24A5423a): not added on this Mac: received {CUT} of its {IMAGE} bytes; nothing of "
                  "it stays staged there"]),
                ("refused", {"FAKE_ADD_FAIL": "1"},
                 ["runtimes: iOS 27.1 (24A94401): not added on fake: simctl runtime add exited 1: simctl: the image "
                  "could not be verified; nothing of it stays staged there",
                  "runtimes: iOS 27.0 (24A5423a): not added on this Mac: simctl runtime add exited 1: simctl: the image "
                  "could not be verified; nothing of it stays staged there"])]:
            with self.subTest(failure), patch.dict(os.environ, variables):
                status, output, error = self.eng()
                self.assertEqual(status, 1, output + error)
                for line in lines: self.assertIn(line, output)
                self.assertIn(f"runtimes: the runtimes of {XCODE} at {self.developer} still differ: only this Mac has "
                              "iOS 27.1 (24A94401); only fake has iOS 27.0 (24A5423a)", output)
                self.assertEqual((self.held("here"), self.held("there")), before)
                self.assertEqual((self.staged("here"), self.staged("there")), ([], []))
        # Only the refused images reached simctl: a dropped copy was never added.
        self.assertEqual(len(self.logged("here", "adds.log") + self.logged("there", "adds.log")), 2)

    def test_a_stop_during_a_copy_adds_nothing_and_leaves_nothing_staged(self):
        def stopping(runtime, size):
            def report(done):
                if done >= size // 2: raise processes.Interrupted(signal.SIGTERM, f"while copying {runtime}")
            return report
        # Small reads, so the stop comes halfway through the image.
        with patch.object(build_host, "CHUNK", 1 << 16), patch.object(remote, "reporter", stopping):
            for direction, here, there in [("to the build host", [("iOS 27.1", "24A94401")], []),
                                           ("from the build host", [], [("iOS 27.0", "24A5423a")])]:
                with self.subTest(direction):
                    for mac, runtimes in [("here", here), ("there", there)]:
                        shutil.rmtree(self.runtimes / mac)
                        (self.runtimes / mac).mkdir()
                        self.give(mac, runtimes)
                    with self.assertRaises(processes.Interrupted):
                        self.eng()
                    self.assertEqual(self.held("here") + self.held("there"), [f"{n} ({b})" for n, b in here + there])
                    self.assertEqual((self.staged("here"), self.staged("there")), ([], []))
        self.assertEqual(self.logged("here", "adds.log") + self.logged("there", "adds.log"), [])

    def test_a_copy_that_would_leave_too_little_free_space_is_refused_before_it_starts(self):
        self.give("here", [("iOS 27.0", "24A434"), ("iOS 27.1", "24A94401")])
        self.give("there", THERE_RUNTIMES)
        with patch.object(remote, "RUNTIME_MARGIN", 1 << 62):
            status, output, error = self.eng("--check")
            self.assertEqual(status, 1, output + error)
            self.assertIn("runtimes: iOS 27.1 (24A94401), 0.0 GiB: would be refused on fake: ", output)
            status, output, error = self.eng()
        self.assertEqual(status, 1, output + error)
        for runtime, mac in [("iOS 27.1 (24A94401)", "fake"), ("iOS 27.0 (24A5423a)", "this Mac")]:
            self.assertIn(f"runtimes: {runtime}, 0.0 GiB: refused on {mac}: ", output)
        self.assertIn("more would leave less than the 4294967296.0 GiB it must keep free; free space there, then run it "
                      "again", output)
        self.assertNotIn("copying", output)
        self.assertIn("still differ", output)
        self.assertEqual(self.logged("here", "adds.log") + self.logged("there", "adds.log"), [])
        self.assertEqual((self.staged("here"), self.staged("there")), ([], []))

    def test_a_runtime_whose_image_cannot_be_read_is_downloaded_by_its_build(self):
        self.image("here", "iOS 27.0", "24A5423a", unreadable=True)
        status, output, error = self.eng("--check")
        self.assertEqual(status, 1, output + error)
        self.assertIn("runtimes: iOS 27.0 (24A5423a), 0.0 GiB: would be downloaded on fake, which has ", output)
        self.assertIn("free, with `xcodebuild -downloadPlatform iOS -buildVersion 24A5423a -architectureVariant arm64`, "
                      "since its image on this Mac cannot be copied: ", output)
        self.assertIn(f"/24A5423a.asset/AssetData holds no single disk image of its {IMAGE + 1} bytes", output)
        # Apple no longer offers the build: the download fails, and the runtime stays on this Mac only.
        status, output, error = self.eng()
        self.assertEqual(status, 1, output + error)
        self.assertIn("runtimes: fake: xcodebuild: error: no such runtime is offered", output)
        self.assertIn("runtimes: iOS 27.0 (24A5423a): xcodebuild exited 70 on fake", output)
        self.assertIn("still differ: only this Mac has iOS 27.0 (24A5423a)", output)
        # It is offered.
        self.image("catalog", "iOS 27.0", "24A5423a")
        status, output, error = self.eng()
        self.assertEqual(status, 0, output + error)
        self.assertIn("runtimes: iOS 27.0 (24A5423a): downloaded on fake", output)
        self.assertEqual(self.held("there"), ["iOS 27.0 (24A5423a)"])
        self.assertEqual(self.logged("there", "downloads.log"),
                         ["xcodebuild -downloadPlatform iOS -buildVersion 24A5423a -architectureVariant arm64"] * 2)

    def test_another_xcode_another_run_or_other_setup_work_is_refused_before_anything_changes(self):
        self.give("here", [("iOS 27.1", "24A94401")])
        with self.subTest("xcode"):
            (self.runtimes / "there/version").write_text("Xcode 27.1\nBuild version 27A9275\n")
            status, output, error = self.eng()
            self.assertEqual(status, 1, output + error)
            self.assertIn(f"runtimes: the Xcode at {self.developer} is {XCODE} on this Mac and Xcode 27.1, Build version "
                          "27A9275 on fake; install this Mac's Xcode at that path there first", output)
            (self.runtimes / "there/version").unlink()
        with self.subTest("another run"):
            lock = remote.runtime_lock()
            try:
                status, output, error = self.eng()
            finally:
                os.close(lock)
            self.assertEqual(status, 1, output + error)
            self.assertIn("runtimes: another `eng remote setup --runtimes` is running on this Mac", output)
        with self.subTest("formulae"):
            status, output, error = self.eng("numpy")
            self.assertEqual(status, 2, output + error)
            self.assertIn("--runtimes brings only the simulator runtimes to their union", error)
        with self.subTest("unreachable"):
            with patch.dict(os.environ, {"FAKE_HOME": str(self.home / "missing")}):
                status, output, error = self.eng()
            self.assertEqual(status, 1, output + error)
            self.assertIn("runtimes: fake cannot be reached", output)
        self.assertEqual(self.held("there"), [])
        self.assertEqual(self.logged("there", "adds.log"), [])

    def test_an_image_is_its_listed_file_or_the_one_its_asset_restores_at_its_size(self):
        image = self.image("here", "iOS 26.5", "23F73")
        size = image.stat().st_size
        # A disk image simctl names by its own path, as iOS 26.5's, and a cryptex by its AssetData folder.
        self.assertEqual(build_host.image_file({"path": str(image), "sizeBytes": size}), (str(image), None))
        self.assertEqual(build_host.image_file({"path": str(image.parents[1]), "sizeBytes": size}), (str(image), None))
        for record in [{"path": str(image.parents[1]), "sizeBytes": size + 1}, {"path": str(image), "sizeBytes": 1},
                       {"path": str(image.parent / "missing.dmg"), "sizeBytes": size}, {"sizeBytes": size}]:
            file, why = build_host.image_file(record)
            self.assertIsNone(file, record)
            self.assertTrue(why, record)
