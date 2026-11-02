import contextlib
from datetime import datetime, timedelta, timezone
import fcntl
from io import StringIO
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/python"))
from vocaro_engineering import cli, config, devices, evidence, offload_host, processes, release, remote, resources
import host_isolation

# remote.advertised as it is, before any test patches it.
ADVERTISED = remote.advertised


def stretched(seconds):
    """These seconds stretched by the host's one-minute load per core, as a bound on a stall is: at a
    load of 450 to 590 on 18 cores, a holder that ends in 0.1 s on an idle Mac outlived 60 s (#111)."""
    return seconds * max(1, os.getloadavg()[0] / (os.cpu_count() or 1))

# Stands in for ssh: the second Mac is a home directory on this Mac. Like sshd, it runs the
# command in that home through a shell, and git's own ssh transport reaches it the same way.
# `ssh -G fake` names this Mac's loopback address and FAKE_PORT, where the test listens as the
# second Mac's sshd would, or FAKE_HOSTNAME instead, and so does each alias FAKE_ALIASES names, as
# ~/.ssh/config would define it; any other name is an alias that ~/.ssh/config does not define,
# which ssh resolves to itself. FAKE_UNREACHABLE makes it fail as ssh does when
# the second Mac's name does not resolve, FAKE_DENIED as it does when the second Mac refuses the
# login, and FAKE_SSH_SAYS fail with the lines it holds, as ssh says them of a failure of its own.
# FAKE_TMPDIR gives the second Mac a temporary directory of its own, and FAKE_DEVICE_RECORD its
# device record, which is never this Mac's.
FAKE_SSH = """#!/bin/sh
if [ "$1" = -G ]; then
    case " fake $FAKE_ALIASES " in
        *" $2 "*) printf 'hostname %s\\nport %s\\naddressfamily any\\n' "${FAKE_HOSTNAME:-127.0.0.1}" "$FAKE_PORT" ;;
        *) printf 'hostname %s\\nport 22\\n' "$2" ;;
    esac
    exit 0
fi
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
if [ -n "$FAKE_DENIED" ]; then
    echo "someone@$1: Permission denied (publickey,password,keyboard-interactive)." >&2
    exit 255
fi
if [ -n "$FAKE_SSH_SAYS" ]; then
    printf '%s\\n' "$FAKE_SSH_SAYS" >&2
    exit 255
fi
shift
cd "$FAKE_HOME" || exit 255
# The second Mac has its own heavy-work queue.
HOME="$FAKE_HOME" SHELL="$FAKE_SHELL" HEAVY_SLOT_DIR="$FAKE_SLOTS" SW_GATE_LOCK_FILE="$FAKE_GATE" \
    TMPDIR="${FAKE_TMPDIR:-$TMPDIR}" VOCARO_DEVICE_RECORD="${FAKE_DEVICE_RECORD:-$FAKE_HOME/device-handoff.json}" \
    exec /bin/sh -c "$*"
"""
# A login shell that reads no profile, so the test's PATH reaches the second Mac.
FAKE_SHELL = """#!/bin/sh
[ "$1" = -l ] && shift
exec /bin/sh "$@"
"""
# Well-formed, invented identifiers: a hardware UDID and a simulator UUID.
PHONE = "00008130-001A2C3E0C38001C"
DUO = "5B0E6C51-2D3A-4F7B-9C1E-0A4B8D2F6E13"
# An invented code-signing identity's SHA-1, forty hex digits as an older device's UDID is.
IDENTITY = "96DAB4C8A3D308C37BCC5C7E987AB6FE05F16008"
# A lane that says it has started, then puts its PID in .build/NAME.pid, whole, and waits to be stopped.
NAP = ("import os, pathlib, sys, time; name = sys.argv[1]; print(name, 'started', flush=True); "
       "partial = pathlib.Path('.build', name + '.partial'); partial.write_text(str(os.getpid())); "
       "partial.rename(pathlib.Path('.build', name + '.pid')); time.sleep(300)")
# Stands in for ssh in front of FAKE_SSH, whose path FAKE_SSH_REAL names: the connection that carries
# a run (its command holds the plan's work) is lost once the file FAKE_DROP names exists. ssh then
# says why, as it does when the second Mac stops answering, and exits 255; the end of the second
# Mac's standard input is its end of the connection closing.
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
# Stands in for ssh in front of FAKE_SSH, whose path FAKE_SSH_REAL names: a push's connection to
# receive-pack goes through RELAY, which FAKE_RELAY names. With FAKE_DROP_PROTOCOL, the second Mac
# never sees GIT_PROTOCOL, as a stock macOS sshd, which accepts only LANG and LC_*, never passes it.
RELAYING_SSH = """#!/bin/sh
case "$*" in
    *receive-pack*) exec "$FAKE_PYTHON" "$FAKE_RELAY" "$@" ;;
esac
[ -n "$FAKE_DROP_PROTOCOL" ] && unset GIT_PROTOCOL
exec "$FAKE_SSH_REAL" "$@"
"""
# Runs receive-pack in the second Mac's home and counts the bytes that reach it, adding a line with
# them to the file FAKE_PASSED names once the push ends. With FAKE_THROTTLE, it passes that many bytes
# each 0.2 s, as a slow link would, after writing its PID and receive-pack's to FAKE_THROTTLED.
# receive-pack runs in a session of its own, as on the second Mac, where a push that ends here closes
# its input and sends it no signal.
RELAY = """import os, subprocess, sys, time
arguments = sys.argv[1:]
while arguments[0].startswith("-"):
    arguments = arguments[2:] if arguments[0] in ("-o", "-p", "-i", "-l", "-F", "-J") else arguments[1:]
child = subprocess.Popen(["/bin/sh", "-c", " ".join(arguments[1:])], cwd=os.environ["FAKE_HOME"],
                         env={**os.environ, "HOME": os.environ["FAKE_HOME"]}, stdin=subprocess.PIPE,
                         start_new_session=True)
throttle, passed = int(os.environ.get("FAKE_THROTTLE") or 0), 0
if throttle:
    with open(os.environ["FAKE_THROTTLED"], "w") as marker: marker.write(f"{os.getpid()} {child.pid}\\n")
while True:
    chunk = os.read(0, throttle or 65536)
    if not chunk: break
    try:
        child.stdin.write(chunk)
        child.stdin.flush()
    except BrokenPipeError:
        break
    passed += len(chunk)
    if throttle: time.sleep(0.2)
try: child.stdin.close()
except BrokenPipeError: pass
status = child.wait()
with open(os.environ["FAKE_PASSED"], "a") as counted: counted.write(f"{passed}\\n")
sys.exit(status)
"""
# Runs eng with the publication of results as the code before vocaro-engineering#67 made it, in two
# steps, held between them: once a dispatch has removed .build/engineering/remote/admit, it waits for
# the other to have done so too, up to 10 s, before it renames its own into place.
BARRIER_DRIVER = """import os, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from vocaro_engineering import cli, remote
removing = remote.shutil.rmtree
def rmtree(path, *arguments, **options):
    removing(path, *arguments, **options)
    path = Path(path)
    if path.name == "admit" and path.parent.name == "remote":
        mark = Path(os.environ["FAKE_BARRIER"]) / str(os.getpid())
        mark.touch()
        deadline = time.monotonic() + 10
        while len(list(mark.parent.iterdir())) < 2 and time.monotonic() < deadline: time.sleep(0.05)
remote.shutil.rmtree = rmtree
sys.exit(cli.main(sys.argv[2:]))
"""
# Megabytes enough to tell a push of a commit's whole history from one of what is new, and few
# enough for the suite: random, so that Git cannot compress them.
LARGE = 4 << 20


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
        self.home = base / "second-mac-home"
        self.home.mkdir()
        self.tools = tools = base / "bin"
        tools.mkdir()
        for name, text in [("ssh", FAKE_SSH), ("login-shell", FAKE_SHELL)]:
            (tools / name).write_text(text)
            (tools / name).chmod(0o755)
        # The second Mac's Python is the Mac's own when it has one: Python 3.9 without Homebrew.
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
            "screen": {"command": [python, "-c", "pass"], "resource": "heavy", "foreground": True},
            "transcripts": {"command": [python, "-c", "import pathlib, sys\nif 'skip' not in sys.argv[1:]:\n"
                                        "    folder = pathlib.Path('.build/engineering/transcripts/results/session-1')\n"
                                        "    folder.mkdir(parents=True, exist_ok=True)\n"
                                        "    (folder / 'transcript.txt').write_text('VO: Next Page')\n"
                                        "print('Ran 1 tests')"], "resource": "heavy", "minimumTests": 1},
            # Outlives any test that holds it, which stops it: on a loaded Mac a test that expected a
            # 60 s lane to be running found it ended (#111).
            "linger": {"command": [python, "-c", "import os, pathlib, time; "
                                   "pathlib.Path('.build/linger.pid').write_text(str(os.getpid())); time.sleep(3600)"]},
            "gate": {"command": [python, "-c", "import os; print('Ran 2 tests in', os.getcwd())"],
                     "resource": "heavy", "minimumTests": 2},
            "failing": {"command": [python, "-c", "print('Ran 2 tests'); raise SystemExit(5)"],
                        "resource": "heavy", "minimumTests": 2},
            "build": {"command": [python, "-c", "print('Ran 1 tests')"], "resource": "heavy",
                      "here": "its goldens come from this Mac's GPU"},
            # Stays here because one of its lanes does.
            "local-pair": {"parallel": ["left", "build"], "resource": "heavy"},
            # Lane groups and their lanes: right's count names a skip, overdue outlasts its timeout,
            # and napper and dozer say they have started, then wait to be stopped.
            "left": {"command": [python, "-c", "print('Ran 2 tests')"], "resource": "heavy", "minimumTests": 2},
            "right": {"command": [python, "-c", "print('Ran 3 tests\\n\\nOK (skipped=1)')"], "resource": "heavy"},
            "broken": {"command": [python, "-c", "print('Ran 1 tests'); raise SystemExit(5)"], "resource": "heavy"},
            "overdue": {"command": [python, "-c", "import time; print('waiting', flush=True); time.sleep(300)"],
                        "timeout": 1},
            "napper": {"command": [python, "-c", NAP, "napper"]},
            "dozer": {"command": [python, "-c", NAP, "dozer"]},
            # first holds .build/first.running while it runs, and second says whether it found it.
            "first": {"command": [python, "-c", "import pathlib, time; running = pathlib.Path('.build/first.running'); "
                                  "running.touch(); time.sleep(0.5); running.unlink(); print('Ran 1 tests')"]},
            "second": {"command": [python, "-c", "import pathlib; print('first running:', "
                                   "pathlib.Path('.build/first.running').exists()); print('Ran 1 tests')"]},
            "turns": {"parallel": ["first", "second"], "resource": "heavy"},
            # Imports a module of the project's, as a project's scripts/eng imports scripts/_engineering.py,
            # and says whether its Python may write bytecode.
            "imports": {"command": [python, "-c", "import sys; sys.path.insert(0, 'scripts'); import helper; "
                                    "print('bytecode', 'off' if sys.dont_write_bytecode else 'on'); print('Ran 1 tests')"]},
            "pair": {"parallel": ["left", "right"], "resource": "heavy"},
            "mixed": {"parallel": ["left", "broken", "overdue"], "resource": "heavy"},
            "stalled": {"parallel": ["napper", "dozer"], "resource": "heavy"},
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
        # A directory of the checkout's own, for a command that runs in one.
        (self.project / "pkg").mkdir()
        (self.project / "pkg/README.md").write_text("a package\n")
        git(base, "init", "-q", str(self.project))
        self.commit = commit_all(self.project)
        # Each Mac's device record, where a restart does not empty it, which the record requires.
        records = durable_scratch()
        self.addCleanup(records.cleanup)
        self.record_here = Path(records.name) / "here" / "device-handoff.json"
        self.record_there = Path(records.name) / "there" / "device-handoff.json"
        # The second Mac's ssh port, which the check before each dispatch connects to. The kernel
        # completes each connection into the backlog, so nothing needs to accept it.
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(128)
        self.addCleanup(self.listener.close)
        self.environment = patch.dict(os.environ, {
            "FAKE_PORT": str(self.listener.getsockname()[1]),
            "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}", "FAKE_HOME": str(self.home),
            "FAKE_SHELL": str(tools / "login-shell"), "HEAVY_SLOT_DIR": str(base / "slots"),
            "SW_GATE_LOCK_FILE": str(base / "gate"), "HEAVY_POLL_SECONDS": "0.01", "VOCARO_OFFLOAD_HOST": "fake",
            "FAKE_SLOTS": str(base / "second-mac-slots"), "FAKE_GATE": str(base / "second-mac-gate"),
            "FAKE_TMPDIR": str(base / "second-mac-tmp"), devices.OVERRIDE: str(self.record_here),
            "FAKE_DEVICE_RECORD": str(self.record_there)})
        (base / "second-mac-tmp").mkdir()
        self.environment.start()
        # Python writes bytecode, as it does for a session that sets no PYTHONDONTWRITEBYTECODE and in
        # the second Mac's login shell, which never has this Mac's environment.
        os.environ.pop("PYTHONDONTWRITEBYTECODE", None)
        # No session holds a device unless a test names one.
        for name in (devices.OWNER, "CODEX_THREAD_ID", "CLAUDE_CODE_SESSION_ID"):
            os.environ.pop(name, None)
        os.environ.pop("VOCARO_RESOURCE_TOKEN", None)
        os.environ.pop("DEVELOPER_DIR", None)
        # VOCARO_OFFLOAD_HOST's old name, which eng refuses, is set only where a test sets it.
        os.environ.pop(remote.OLD_HOST_VARIABLE, None)
        # A run on the real second Mac hands its git the credential file through these.
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
        """A committed copy of this toolkit, as the second Mac receives it. Its origin is the toolkit's
        upstream URL, which Git on both Macs reads as the copy itself, so no test fetches the network."""
        for part in ["bin", "integrations", "policies", "agents", "src/python"]:
            shutil.copytree(config.TOOLKIT_ROOT / part, root / part, ignore=shutil.ignore_patterns("__pycache__"))
        # Its .gitignore keeps out the bytecode that running it writes beside its source.
        shutil.copyfile(config.TOOLKIT_ROOT / ".gitignore", root / ".gitignore")
        (root / "engineering.json").write_text(json.dumps({"schemaVersion": 1, "name": "vocaro-engineering",
                                                           "toolkit": {"path": "."}}))
        subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
        commit_all(root)
        git(root, "remote", "add", "origin", remote.UPSTREAM)
        # Here in the copy's own configuration, and there in the second Mac's home's.
        git(root, "config", f"url.{root}.insteadOf", remote.UPSTREAM)
        (self.home / ".gitconfig").write_text(f'[url "{root}"]\n\tinsteadOf = {remote.UPSTREAM}\n')
        return root

    def test_a_toolkit_origin_naming_a_path_here_is_refused_before_anything_is_sent(self):
        settings = json.loads((self.toolkit / "engineering.json").read_text())
        settings["lanes"] = {"check": {"command": [sys.executable, "-c", "print('Ran 1 tests')"], "minimumTests": 1,
                                       "resource": "heavy"}}
        (self.toolkit / "engineering.json").write_text(json.dumps(settings))
        commit_all(self.toolkit)
        # A clone of a checkout here, as VOCARO_ENGINEERING_ROOT may name: its origin is that checkout's path.
        clone = Path(self.temp.name) / "clone/vocaro-engineering"
        subprocess.run(["git", "clone", "-q", str(self.toolkit), str(clone)], check=True)
        for url, upstream in [(str(self.toolkit), remote.UPSTREAM), (f"file://{self.toolkit}", remote.UPSTREAM),
                              ("../../vocaro-engineering", remote.UPSTREAM)]:
            with self.subTest(url=url):
                git(clone, "remote", "set-url", "origin", url)
                with patch.object(remote, "TOOLKIT_ROOT", clone), contextlib.redirect_stdout(StringIO()), \
                        contextlib.redirect_stderr(StringIO()) as error:
                    status = cli.main(["--project", str(clone), "remote", "run", "check"])
                self.assertNotEqual(status, 0)
                said = " ".join(error.getvalue().split())
                self.assertIn(f"origin is {url}, a path on this Mac", said)
                self.assertIn(f"remote set-url origin {upstream}`", said)
                # Nothing reached the second Mac, so its shared origin is as it was: never written.
                self.assertFalse((self.home / remote.ROOT / "repositories").exists())
        # A checkout whose own origin is another path, or none, is pointed at the upstream.
        bare = Path(self.temp.name) / "bare.git"
        subprocess.run(["git", "clone", "-q", "--bare", str(clone), str(bare)], check=True)
        git(clone, "remote", "set-url", "origin", str(bare))
        with self.assertRaisesRegex(RuntimeError, f"set-url origin {re.escape(remote.UPSTREAM)}`"):
            remote.origin(clone)
        # The positive control: a checkout's network origin is named in the fix, and URLs pass.
        git(bare, "config", "remote.origin.url", "https://example.com/fork/vocaro-engineering.git")
        with self.assertRaisesRegex(RuntimeError, "set-url origin https://example.com/fork/vocaro-engineering.git`"):
            remote.origin(clone)
        for url, sent in [(remote.UPSTREAM, remote.UPSTREAM),
                          ("git@github.com:vocaro/vocaro-engineering.git", remote.UPSTREAM),
                          ("ssh://git@github.com/vocaro/vocaro-engineering.git",
                           "ssh://git@github.com/vocaro/vocaro-engineering.git"),
                          ("build-host:repositories/vocaro-engineering.git", "build-host:repositories/vocaro-engineering.git")]:
            git(clone, "remote", "set-url", "origin", url)
            self.assertEqual(remote.origin(clone), sent)

    def eng(self, *arguments):
        with contextlib.redirect_stdout(StringIO()) as output, contextlib.redirect_stderr(StringIO()) as error:
            status = cli.main(["--project", str(self.project), *arguments])
        return status, output.getvalue(), error.getvalue()

    def checkout(self, slot):
        return self.home / remote.ROOT / "checkouts/example" / str(slot) / "example"

    def test_a_lane_runs_at_the_commit_on_the_second_mac_after_its_provision_lane(self):
        status, output, error = self.eng("remote", "run", "check")
        self.assertEqual(status, 0, output + error)
        self.assertEqual(git(self.checkout(0), "rev-parse", "HEAD"), self.commit)
        self.assertTrue((self.checkout(0) / ".build/provisioned").exists())
        results = self.project / ".build/engineering/remote/check"
        self.assertIn("Ran 2 tests", (results / "run.log").read_text())
        receipt = json.loads((results / "source.json").read_text())
        self.assertEqual((receipt["commit"], receipt["host"], receipt["result"]["exitStatus"]), (self.commit, "fake", 0))
        self.assertEqual(receipt["toolkit"], git(self.toolkit, "rev-parse", "HEAD"))
        # The second Mac ran its own admission and wrote its own receipt.
        self.assertEqual(receipt["result"]["receipt"]["project"]["revision"], self.commit)
        self.assertIn("Policy source:", output)
        self.assertIn(f"check at {self.commit[:12]}", error)
        # The toolkit checkout sits beside the project's, at the dispatched commit.
        kit = self.checkout(0).parent / "vocaro-engineering"
        self.assertEqual(git(kit, "rev-parse", "HEAD"), git(self.toolkit, "rev-parse", "HEAD"))
        # The pushed refs only carried the commits; the checkouts keep them, and refs name what they hold.
        self.assertEqual(git(self.home / remote.ROOT / "repositories/example.git", "for-each-ref", "--format=%(refname)"),
                         f"{offload_host.CACHE}{self.commit}")
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

    def test_a_toolkit_with_uncommitted_changes_goes_as_a_snapshot_and_counts_for_no_commit(self):
        # vocaro-engineering#104: the toolkit's working tree goes as the project's does, where it used to
        # be refused and the work kept on this Mac, which reads that same tree.
        head = git(self.toolkit, "rev-parse", "HEAD")
        settings = self.toolkit / "engineering.json"
        settings.write_text(settings.read_text() + "\n")
        (self.toolkit / "untracked.txt").write_text("new\n")
        status, output, error = self.eng("remote", "run", "check")
        self.assertEqual(status, 0, output + error)
        receipt = json.loads((self.project / ".build/engineering/remote/check/source.json").read_text())
        self.assertEqual((receipt["commit"], receipt["snapshot"]), (self.commit, False))
        self.assertEqual((receipt["toolkitHead"], receipt["toolkitSnapshot"]), (head, True))
        self.assertEqual(git(self.toolkit, "rev-parse", f"{receipt['toolkit']}^"), head)
        self.assertIn(f"with the toolkit at {receipt['toolkit'][:12]}, a snapshot of its working tree on {head[:12]}",
                      error)
        # The second Mac ran the snapshot, untracked file included.
        kit = self.checkout(0).parent / "vocaro-engineering"
        self.assertEqual(git(kit, "rev-parse", "HEAD"), receipt["toolkit"])
        self.assertEqual((kit / "engineering.json").read_text(), settings.read_text())
        self.assertTrue((kit / "untracked.txt").is_file())
        # The toolkit checkout's own HEAD, index and working tree are as they were.
        self.assertEqual(git(self.toolkit, "rev-parse", "HEAD"), head)
        self.assertEqual(git(self.toolkit, "status", "--porcelain"), "M engineering.json\n?? untracked.txt")
        # A run on a toolkit no branch holds is no pass of the project's commit.
        self.assertEqual(self.eng("remote", "passed", "check")[0], 1)
        status, _output, error = self.eng("remote", "admit", "--", "true")
        self.assertEqual(status, 0, error)
        # The positive control: with the toolkit committed, the same commit's run is a pass.
        git(self.toolkit, "checkout", "--", "engineering.json")
        (self.toolkit / "untracked.txt").unlink()
        status, output, error = self.eng("remote", "run", "check")
        self.assertEqual(status, 0, output + error)
        receipt = json.loads((self.project / ".build/engineering/remote/check/source.json").read_text())
        self.assertEqual((receipt["toolkit"], receipt["toolkitHead"], receipt["toolkitSnapshot"]), (head, head, False))
        self.assertEqual(self.eng("remote", "passed", "check")[0], 0)

    def test_a_different_xcode_on_the_second_mac_is_refused(self):
        local = {**offload_host.identity(None), "xcode": "Xcode 99.0\nBuild version 99A1"}
        with patch.object(remote, "developer_directory", return_value=self.temp.name), \
                patch.object(remote.offload_host, "identity", return_value=local):
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

    def test_every_run_makes_the_second_macs_record_defer_physical_devices_to_this_mac(self):
        self.assertEqual(self.eng("remote", "run", "check")[0], 0)
        there = devices.Record(self.record_there)
        self.assertEqual(there.load()[devices.DEFERRED], offload_host.mac_name())
        revision = there.load()["revision"]
        self.assertEqual(self.eng("remote", "admit", "--", sys.executable, "-c", "pass")[0], 0)
        self.assertEqual(there.load()["revision"], revision)
        self.assertNotIn(devices.DEFERRED, devices.Record(self.record_here).load())
        # A run there cannot reserve a physical device in the second Mac's own record, even one its command
        # spells in two parts, which the dispatch here does not see as a hardware UDID.
        udid = PHONE.replace("C", "D")
        helper = ("import os, runpy, sys; sys.path.insert(0, os.path.join(os.environ['VOCARO_ENGINEERING_ROOT'], "
                  f"'src/python')); sys.argv[-1] = {udid[:9]!r} + {udid[9:]!r}; "
                  "runpy.run_module('vocaro_engineering.devices', run_name='__main__')")
        status, output, _error = self.eng("remote", "admit", "--", sys.executable, "-c", helper,
            "reserve", "iPhone", "--owner", "there", "--revision", "0", "--purpose", "p",
            "--release-by", "2099-01-01T00:00:00Z", "--device", "UDID")
        self.assertNotEqual(status, 0)
        self.assertIn(f"reserved in {offload_host.mac_name()}'s device record", output)
        self.assertIsNone(there.entry("iPhone"))

    def test_a_second_mac_record_that_defers_elsewhere_or_holds_a_physical_device_stops_the_run(self):
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
        self.assertIn(f"lease {offload_host.mac_name()} {{'iPhone': '{PHONE}'}} {held['expectedReleaseBy']}", output)
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
        # eng run keeps a lane that would go to the second Mac on this Mac when it names a physical device.
        status, output, error = self.eng("run", "gate", "--", PHONE)
        self.assertEqual(status, 0, error)
        self.assertIn("the second Mac uses only under a reservation", error)
        self.assertIn(f"Ran 2 tests in {self.project.resolve()}", (self.project / ".build/engineering/gate/run.log").read_text())

    def test_a_command_that_names_a_hardware_udid_no_entry_holds_is_refused_without_a_device(self):
        # The second Mac reaches a paired device whether or not this Mac's record has an entry for it.
        self.hold()
        older = "ab12" * 10
        # Forty hex digits that the checkout's Git does not know, even another repository's commit, may be
        # an older device's UDID.
        elsewhere = git(self.toolkit, "rev-parse", "HEAD")
        for words, udid in [(["admit", "--", sys.executable, "-c", "pass", "id=00008020-0011223344556677"],
                             "00008020-0011223344556677"),
                            (["run", "check", "--", f"platform=iOS,id={older}"], older.upper()),
                            (["admit", "--", "git", "show", elsewhere], elsewhere.upper()),
                            # A signing identity's setting covers only the digits it takes (#86).
                            (["admit", "--", "env", f"SIGN_IDENTITY={IDENTITY}", sys.executable, "-c", "pass", older],
                             older.upper())]:
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
        # A code-signing identity's SHA-1, which has an older UDID's shape, where a signing setting or
        # codesign's option takes it (#86).
        status, output, error = self.eng(
            "remote", "admit", "--", "env", f"SIGN_IDENTITY={IDENTITY}", sys.executable, "-c",
            "import os, sys; print('signed', os.environ['SIGN_IDENTITY'], *sys.argv[1:])",
            f"CODE_SIGN_IDENTITY={IDENTITY.lower()}", "codesign -f -s", IDENTITY)
        self.assertEqual(status, 0, error)
        self.assertIn(f"signed {IDENTITY} CODE_SIGN_IDENTITY={IDENTITY.lower()} codesign -f -s {IDENTITY}", output)

    def test_recovering_a_run_sent_here_waits_for_what_on_the_second_mac_names_its_device(self):
        # Its own device, which no other test's processes name, nor another run's of this suite (#80).
        udid = f"00008730-{uuid.uuid4().hex[:16].upper()}"
        held = self.hold(device=udid)
        record = devices.Record(self.record_here)
        dispatcher = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(dispatcher.wait)
        self.addCleanup(dispatcher.kill)
        entry = record.update("iPhone", owner="session-a", revision=held["revision"], activeRun=True,
                              hostProcessIdentifier=dispatcher.pid, run={"host": "fake", "startedAt": devices.now()})
        dispatcher.kill()
        dispatcher.wait()
        # The fake second Mac is this Mac: a process there that names the device keeps it.
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
        # Another recovery's census of the device names it but only reads, so it holds nothing (#80).
        census = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)",
                                   json.dumps({"device": {"udid": udid, "root": remote.ROOT, "since": None}})])
        self.addCleanup(census.wait)
        self.addCleanup(census.kill)
        recovered = record.recover("iPhone", by="owner", revision=entry["revision"], verified="nothing on the device")
        self.assertEqual((recovered["state"], recovered["recovery"]["offload"]["host"]), ("released", "fake"))

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

    def test_a_bounded_run_on_the_second_mac_takes_its_reservation_from_the_lease(self):
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
        # Without the reservation it carries, the second Mac's record refuses the device.
        status, output, _error = self.eng("remote", "admit", "--", *launch, "iPhone", "--", *ran)
        self.assertNotEqual(status, 0)
        self.assertIn(f"physical devices are reserved in {offload_host.mac_name()}'s device record", output)
        self.assertNotIn("ran there", output)

    def test_admit_passes_the_model_or_exclusive_admission_on_to_the_second_mac(self):
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

    @patch.object(remote, "CHECKOUTS", 2)
    def test_work_on_the_second_mac_leaves_no_bytecode_in_its_checkout(self):
        # The project's .gitignore names no __pycache__, as many projects' do not.
        (self.project / "scripts").mkdir()
        (self.project / "scripts/helper.py").write_text("NAME = 'helper'\n")
        commit_all(self.project)
        command = json.loads((self.project / "engineering.json").read_text())["lanes"]["imports"]["command"]
        results = lambda name: self.project / ".build/engineering/remote" / name
        slot = lambda name: json.loads((results(name) / "source.json").read_text())["result"]["slot"]
        # A lane's output is in its log, and a command's in its output.
        for operation, name, said in [(["run", "imports"], "imports", "run.log"),
                                      (["admit", "--", *command], "admit", "output.log")]:
            with self.subTest(operation):
                status, output, error = self.eng("remote", *operation)
                self.assertEqual(status, 0, output + error)
                self.assertIn("bytecode off", (results(name) / said).read_text())
                self.assertNotIn("passing over", output)
                self.assertEqual(slot(name), 0)
                self.assertFalse((self.checkout(0) / "scripts/__pycache__").exists())
                self.assertEqual(git(self.checkout(0), "status", "--porcelain", "--untracked-files=all"), "")
        # The negative control: a second Mac that lets the work's Python write bytecode leaves it in the
        # checkout, and the next run passes that checkout over.
        sent = Path(self.temp.name) / "offload_host.py"
        text = Path(offload_host.__file__).read_text()
        sent.write_text(text.replace('NO_BYTECODE = {"PYTHONDONTWRITEBYTECODE": "1"}', "NO_BYTECODE = {}", 1))
        self.assertNotEqual(sent.read_text(), text)
        with patch.object(remote.offload_host, "__file__", str(sent)):
            status, output, error = self.eng("remote", "run", "imports")
        self.assertEqual(status, 0, output + error)
        self.assertIn("bytecode on", (results("imports") / "run.log").read_text())
        self.assertTrue(list((self.checkout(0) / "scripts/__pycache__").glob("helper.*.pyc")))
        status, output, error = self.eng("remote", "run", "imports")
        self.assertEqual(status, 0, output + error)
        self.assertIn(f"passing over {self.checkout(0)}, which a run left with changes:", output)
        self.assertIn("?? scripts/__pycache__/", output)
        self.assertEqual(slot("imports"), 1)

    def test_a_stop_ends_the_second_mac_run_and_releases_its_checkout_and_admission(self):
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
                          "accounts for. These were not stopped; tell the owner", error)
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
            # Read before the connection that prepares the second Mac, at the dispatch, and once it is lost.
            with patch.dict(os.environ, variables), \
                    patch.object(remote, "asleep", side_effect=[1000.0, 1000.0, 1300.0]):
                status, output, error = self.eng("remote", "run", "linger")
            lane = int(marker.read_text())
            self.assertEqual(status, evidence.UNMEASURED, output + error)
            self.assertRegex(error, r"eng: No result from fake \(ssh exited 255: Timeout, server fake not responding\.\); "
                                    r"dispatched at \d{4}-\d\d-\d\d \d\d:\d\d:\d\d UTC and lost at \d{4}-\d\d-\d\d "
                                    r"\d\d:\d\d:\d\d UTC, and this Mac slept for 300 s of that\. The run is unmeasured, "
                                    r"and fake stops it once its end of the connection closes\.")
            results = self.project / ".build/engineering/remote/linger"
            self.assertIn("Timeout, server fake not responding.", (results / "output.log").read_text())
            self.assertFalse((results / "source.json").exists())
            self.assertEqual(self.eng("remote", "passed", "linger")[0], 1)
            # Its end of the connection closed, so the second Mac stopped the run.
            deadline = time.monotonic() + stretched(60)
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

    def test_a_connection_this_mac_slept_through_says_so_rather_than_that_the_second_mac_is_away(self):
        # As at 22:13 UTC on 2026-10-27: the second Mac accepted eng's first connection, this Mac slept
        # for 210 s with it open, and ssh timed out two minutes after this Mac woke. eng said to wake
        # the second Mac, which was awake.
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
        # Awake, the same timeout says only that the second Mac answered and then fell silent.
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

    def test_a_reap_before_a_dispatch_is_printed_and_its_problem_stops_no_run(self):
        item = {"name": "Example Duo 1", "udid": "U", "pid": 7, "reason": "it has run 3.0 h"}
        for report, said in [({"stopped": [item], "problem": None}, "remote: on fake, stopped Example Duo 1 (U), PID 7: it has run 3.0 h"),
                             ({"stopped": [], "problem": "the device record could not be read"},
                              "remote: on fake, stopped no further simulator: the device record could not be read")]:
            answer = shlex.quote(json.dumps({"leftovers": [], "reap": report}))
            with self.subTest(said), patch.object(remote, "census_command", return_value=f"echo {answer}") as census:
                status, output, error = self.eng("remote", "run", "check")
            self.assertEqual(status, 0, output + error)
            self.assertIn(said, error)
            self.assertEqual(census.call_args[0][1], {"pools": []})
            self.assertIn(f"check at {self.commit[:12]}", error)

    def test_the_second_macs_credential_file_is_the_runs_only_git_credential_helper(self):
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

    def test_a_kept_keychain_password_that_cannot_unlock_the_login_keychain_refuses_the_run(self):
        # Without a kept password a run unlocks nothing, as before keep-unlocked.
        status, output, error = self.eng("remote", "admit", "--", sys.executable, "-c", "print('the work ran')")
        self.assertEqual(status, 0, output + error)
        self.assertIn("the work ran", output)
        self.assertNotIn("unlocked", output + error)
        # A kept password for a login keychain this second Mac does not have: the run does not start.
        kept = self.home / remote.ROOT / offload_host.KEYCHAIN_PASSWORD
        kept.write_text("not a real password\n")
        status, output, error = self.eng("remote", "admit", "--", sys.executable, "-c", "print('the work ran')")
        self.assertEqual(status, 2, output + error)
        login = self.home / offload_host.KEYCHAINS / offload_host.LOGIN_KEYCHAIN
        self.assertIn(f"{kept} holds the password of {login}, which is not there", error)
        self.assertIn("`python3 scripts/eng keychain keep-unlocked`", error)
        self.assertNotIn("the work ran", output)

    def run_here(self, *arguments):
        """eng run, with this Mac's policy refresh, which would fetch GitHub, left out."""
        with patch.object(cli, "refresh_policy", return_value="policy"):
            return self.eng("run", *arguments)

    def test_a_heavy_lane_runs_on_the_second_mac_first(self):
        status, output, error = self.eng("run", "gate")
        self.assertEqual(status, 0, output + error)
        self.assertIn(f"Ran 2 tests in {self.checkout(0).resolve()}",
                      (self.project / ".build/engineering/remote/gate/run.log").read_text())
        self.assertFalse((self.project / ".build/engineering/gate").exists())
        self.assertEqual(self.eng("remote", "passed", "gate")[0], 0)
        # The new names run without a refusal: VOCARO_OFFLOAD_HOST, and a lane with no buildHost.
        self.assertNotIn("vocaro-engineering#58", error)

    def test_every_heavy_lane_goes_to_the_second_mac_unless_it_says_why_and_each_receipt_says_where_it_ran(self):
        # check is heavy and says nothing of the second Mac: it goes there, where its provision lane runs first.
        status, output, error = self.run_here("check")
        self.assertEqual(status, 0, output + error)
        self.assertTrue((self.checkout(0) / ".build/provisioned").exists())
        self.assertFalse((self.project / ".build/engineering/check").exists())
        receipt = json.loads((self.project / ".build/engineering/remote/check/source.json").read_text())
        self.assertEqual((receipt["ranOn"], receipt["mac"], receipt["host"]), ("second Mac", offload_host.mac_name(), "fake"))
        # The second Mac's own receipt says so too, and names the Mac that sent the run.
        there = receipt["result"]["receipt"]
        self.assertEqual((there["ranOn"], there["dispatchedFrom"]), ("second Mac", offload_host.mac_name()))
        self.assertNotIn("hereBecause", there)
        # build says why it stays here, and a group that runs it stays with it; a light lane runs where it starts.
        for lane, because in [("build", "the lane says: its goldens come from this Mac's GPU"),
                              ("local-pair", "its lane build says: its goldens come from this Mac's GPU"),
                              ("fail", "a light lane runs where it starts")]:
            with self.subTest(lane):
                status, output, error = self.run_here(lane)
                self.assertEqual(status, 3 if lane == "fail" else 0, output + error)
                receipt = json.loads((self.project / ".build/engineering" / lane / "source.json").read_text())
                self.assertEqual((receipt["ranOn"], receipt["mac"], receipt["hereBecause"]),
                                 ("this Mac", offload_host.mac_name(), because))
                self.assertFalse((self.project / ".build/engineering/remote" / lane).exists())

    def test_buildhost_on_a_lane_or_group_is_refused_before_anything_runs_on_either_mac(self):
        # vocaro-engineering#58 removed it. A checkout that still gives it, true to send a lane there or
        # false to keep it here, is refused, rather than having its work go where it did not say.
        refused = ("eng: engineering.json gives {} \"buildHost\", which vocaro-engineering#58 removed: every heavy or "
                   "exclusive lane goes to the second Mac unless it says why it stays, with \"here\": \"REASON\", so "
                   "delete {}, and rebase a checkout older than that change onto main (guides/shared-host.md, "
                   "Earlier names)\n")
        original = (self.project / "engineering.json").read_text()
        for marks, named, pronoun in [({"gate": True}, "gate", "it"), ({"gate": False}, "gate", "it"),
                                      ({"pair": True}, "pair", "it"), ({"pair": False}, "pair", "it"),
                                      ({"gate": True, "pair": False}, "gate and pair", "each"),
                                      ({"fail": True, "gate": False, "pair": True}, "fail, gate and pair", "each")]:
            with self.subTest(marks):
                settings = json.loads(original)
                for name, value in marks.items(): settings["lanes"][name]["buildHost"] = value
                (self.project / "engineering.json").write_text(json.dumps(settings, indent=2))
                for arguments in (["gate"], ["--here", "gate"], ["--offload-only", "gate"], ["pair"], ["fail"]):
                    status, output, error = self.run_here(*arguments)
                    self.assertEqual((status, error), (2, refused.format(named, pronoun)), output + error)
                status, output, error = self.eng("remote", "run", "gate")
                self.assertEqual((status, error), (2, refused.format(named, pronoun)), output + error)
                # Nothing ran on either Mac: no result here, and nothing reached the second Mac.
                for place in ["gate", "pair", "fail", "remote/gate", "remote/pair"]:
                    self.assertFalse((self.project / ".build/engineering" / place).exists(), place)
                self.assertFalse((self.home / remote.ROOT).exists())
        # The positive control: the same lanes without it run, the heavy one on the second Mac.
        (self.project / "engineering.json").write_text(original)
        status, output, error = self.run_here("gate")
        self.assertEqual(status, 0, output + error)
        self.assertEqual(json.loads((self.project / ".build/engineering/remote/gate/source.json").read_text())["ranOn"],
                         "second Mac")
        self.assertNotIn("buildHost", error)

    def test_a_receipt_from_before_the_rename_counts_as_one_of_now(self):
        # Receipts written before vocaro-engineering#58 say the second Mac's run ran on the build host.
        self.assertEqual(self.run_here("gate")[0], 0)
        kept = remote.store(self.project) / self.commit / "gate.json"
        receipt = json.loads(kept.read_text())
        self.assertEqual((receipt["ranOn"], receipt["result"]["receipt"]["ranOn"]), ("second Mac", "second Mac"))
        for name in ["build host", "second Mac"]:
            with self.subTest(name):
                receipt["ranOn"] = receipt["result"]["receipt"]["ranOn"] = name
                kept.write_text(json.dumps(receipt))
                self.assertEqual(remote.passed(self.project, "gate")["ranOn"], name)
                status, output, _error = self.eng("remote", "passed", "gate")
                self.assertEqual(status, 0)
                self.assertIn(f"gate passed at {self.commit}", output)
                self.assertTrue(release.gate_passed(self.project, self.commit).startswith("on the second Mac, "))
        # The negative control: a receipt of a run that failed counts for nothing.
        receipt["result"]["exitStatus"] = 1
        kept.write_text(json.dumps(receipt))
        self.assertIsNone(remote.passed(self.project, "gate"))

    def test_serial_runs_a_group_here_as_the_serial_reference(self):
        status, output, error = self.run_here("--serial", "pair")
        self.assertEqual(status, 0, output + error)
        receipt = json.loads((self.project / ".build/engineering/pair/source.json").read_text())
        self.assertEqual((receipt["ranOn"], receipt["hereBecause"], receipt["jobs"]),
                         ("this Mac", "--serial runs the serial reference here", 1))
        self.assertFalse((self.project / ".build/engineering/remote/pair").exists())
        self.assertFalse((self.home / remote.ROOT).exists())
        # The negative control: without --serial the group goes to the second Mac.
        self.assertEqual(self.run_here("pair")[0], 0)
        self.assertTrue((self.project / ".build/engineering/remote/pair/source.json").exists())

    def lane_files(self, group, lane, place=None):
        """A lane's log and receipt among its group's results: here, or in the directory place names."""
        lanes = (place or self.project / ".build/engineering/remote" / group) / "lanes"
        return (lanes / f"{lane}.log").read_text(), json.loads((lanes / f"{lane}.json").read_text())

    def test_what_a_lane_writes_in_its_results_comes_back_from_the_second_mac(self):
        results = self.project / ".build/engineering/remote/transcripts"
        status, output, error = self.eng("remote", "run", "transcripts")
        self.assertEqual(status, 0, output + error)
        self.assertEqual((results / "results/session-1/transcript.txt").read_text(), "VO: Next Page")
        self.assertEqual(json.loads((results / "source.json").read_text())["returned"],
                         ["results/session-1/transcript.txt"])
        self.assertIn(f"remote: 1 file the run wrote in its results/ came back in {results.resolve()}/results", error)
        # A later run that writes nothing there sends back nothing an earlier run left.
        status, output, error = self.eng("remote", "run", "transcripts", "--", "skip")
        self.assertEqual(status, 0, output + error)
        self.assertFalse((results / "results").exists())
        self.assertNotIn("returned", json.loads((results / "source.json").read_text()))
        # Only a file under a returned directory, by a path that stays in it, or one named exactly.
        for path, wanted in [("results/a.txt", True), ("results/x/b.txt", True), ("lanes/x.log", True),
                             ("results/", False), ("results/../run.log", False), ("/results/a", False),
                             ("other/a.txt", False), ("lanes/y.log", False), (None, False)]:
            with self.subTest(path):
                self.assertEqual(remote.returned_path(path, ["lanes/x.log", "results/"]), wanted)

    def test_a_group_on_the_second_mac_brings_each_lanes_log_and_receipt_back(self):
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
        # The negative control: without the copy-back, the lanes' files stay in the second Mac's checkout.
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
        # The negative control: without the copy-back, the lanes' files stay in the second Mac's checkout.
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
        run on the second Mac, and return its output once it has ended."""
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
        # The negative control: a toolkit without the copy-back leaves the lanes' files in the second
        # Mac's checkout. The dispatcher runs the toolkit's commit, so the change is committed.
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

    def test_serial_runs_a_groups_lanes_in_turn_on_the_second_mac_and_counts_for_no_commit(self):
        # Only a group runs in turn: anything else is refused before anything is sent.
        status, output, error = self.eng("remote", "run", "--serial", "check")
        self.assertEqual(status, 2, output + error)
        self.assertIn("--serial runs a lane group, and check is not one", error)
        self.assertTrue(self.nothing_sent())
        results = self.project / ".build/engineering/remote/turns"
        status, output, error = self.eng("remote", "run", "--serial", "turns")
        self.assertEqual(status, 0, output + error)
        kept = json.loads((results / "source.json").read_text())
        self.assertEqual((kept["work"], kept["serial"]), (["run", "--here", "--serial", "turns"], True))
        # The second Mac ran the lanes one at a time, under its own admission, and each came back.
        self.assertEqual(kept["result"]["receipt"]["jobs"], 1)
        self.assertIn("heavy: slot", output)
        self.assertIn(f"{self.checkout(0)} at {self.commit[:12]}", output)
        self.assertIn("first running: False", self.lane_files("turns", "second")[0])
        self.assertEqual([self.lane_files("turns", lane)[1]["outcome"] for lane in ("first", "second")],
                         ["passed", "passed"])
        # The serial reference is no pass of the group's.
        self.assertEqual(self.eng("remote", "passed", "turns")[0], 1)
        # The negative control: without --serial, the second Mac runs them side by side, and their pass counts.
        status, output, error = self.eng("remote", "run", "turns")
        self.assertEqual(status, 0, output + error)
        kept = json.loads((results / "source.json").read_text())
        self.assertEqual((kept["work"], kept["result"]["receipt"]["jobs"]), (["run", "--here", "turns"], 2))
        self.assertNotIn("serial", kept)
        self.assertEqual(self.eng("remote", "passed", "turns")[0], 0)

    @contextlib.contextmanager
    def busy_mac(self, slots="FAKE_SLOTS", gate="FAKE_GATE"):
        """Exclusive work that holds every slot of a Mac's queue while the block runs: the second Mac's,
        or with HEAVY_SLOT_DIR and SW_GATE_LOCK_FILE this Mac's."""
        holder = subprocess.Popen([sys.executable, "-m", "vocaro_engineering.resources", "--exclusive", "--",
                                   sys.executable, "-c", "print('held', flush=True); import time; time.sleep(300)"],
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env={
                                      **os.environ, "PYTHONPATH": str(config.TOOLKIT_ROOT / "src/python"),
                                      "HEAVY_SLOT_DIR": os.environ[slots], "SW_GATE_LOCK_FILE": os.environ[gate]})
        try:
            self.assertEqual(holder.stdout.readline().strip(), "held")
            yield
        finally:
            holder.send_signal(signal.SIGTERM)
            holder.wait(timeout=stretched(60))
            holder.stdout.close()

    def busy_second_mac(self):
        return self.busy_mac()

    def busy_here(self):
        return self.busy_mac("HEAVY_SLOT_DIR", "SW_GATE_LOCK_FILE")

    def waits_there(self, lane, phrase, release, hold=0):
        """Run `eng run LANE` from a dispatcher of its own, call release once its output has said phrase
        and the dispatcher has gone on waiting for hold seconds more, and return its status and output
        once it has ended."""
        log = Path(self.temp.name) / "dispatch.log"
        with log.open("w") as output:
            dispatcher = subprocess.Popen([sys.executable, str(self.toolkit / "bin/eng"), "--project", str(self.project),
                                           "run", lane], stdout=output, stderr=subprocess.STDOUT)
        try:
            # A bound for a busy host, not a measure: the phrase ends the wait.
            deadline = time.monotonic() + 120
            while phrase not in log.read_text() and dispatcher.poll() is None and time.monotonic() < deadline:
                time.sleep(0.1)
            self.assertIn(phrase, log.read_text())
            time.sleep(hold)
            self.assertIsNone(dispatcher.poll(), log.read_text())
            self.assertFalse((self.project / ".build/engineering" / lane).exists(), log.read_text())
            release()
            dispatcher.wait(timeout=300)
        finally:
            if dispatcher.poll() is None:
                dispatcher.kill()
                dispatcher.wait()
        return dispatcher.returncode, log.read_text()

    def ran_here(self, lane="gate"):
        receipt = json.loads((self.project / ".build/engineering" / lane / "source.json").read_text())
        self.assertIn(f"Ran 2 tests in {self.project.resolve()}",
                      (self.project / ".build/engineering" / lane / "run.log").read_text())
        return receipt

    def test_a_busy_second_mac_sends_the_lane_to_this_mac_when_this_mac_admits_it(self):
        with self.busy_second_mac():
            status, output, error = self.run_here("gate")
        self.assertEqual(status, 0, output + error)
        busy = "the second Mac is busy: fake did not take it: its heavy-work queue cannot take the lane now"
        self.assertIn(f"eng: trying gate on this Mac: {busy}", error)
        receipt = self.ran_here()
        self.assertEqual((receipt["ranOn"], receipt["hereBecause"]), ("this Mac", busy))
        # Its wait in this Mac's queue, as a run on the second Mac's receipt gives it there.
        self.assertIsInstance(receipt["queueSeconds"], float)
        # It was offered there and declined: no result of the second Mac's is kept.
        self.assertFalse((self.project / ".build/engineering/remote/gate").exists())
        # A second Mac whose every checkout is in use is busy too.
        shutil.rmtree(self.project / ".build/engineering/gate")
        base = self.home / remote.ROOT / "checkouts/example"
        base.mkdir(parents=True, exist_ok=True)
        locks = [os.open(base / f"{slot}.lock", os.O_RDWR | os.O_CREAT, 0o644) for slot in range(remote.CHECKOUTS)]
        try:
            for lock in locks: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            status, output, error = self.run_here("gate")
        finally:
            for lock in locks: os.close(lock)
        self.assertEqual(status, 0, output + error)
        self.assertIn("eng: trying gate on this Mac: the second Mac is busy: fake did not take it: every checkout of "
                      "example is in use", error)
        self.assertIn("every checkout of example is in use", self.ran_here()["hereBecause"])
        # The negative control: free, the second Mac takes it.
        shutil.rmtree(self.project / ".build/engineering/gate")
        self.assertEqual(self.run_here("gate")[0], 0)
        self.assertFalse((self.project / ".build/engineering/gate").exists())
        self.assertTrue((self.project / ".build/engineering/remote/gate/source.json").exists())

    def test_while_both_macs_are_busy_the_lane_runs_on_whichever_admits_it_first(self):
        waiting = "waits for whichever Mac admits it first"
        with self.subTest("the second Mac"), contextlib.ExitStack() as here, contextlib.ExitStack() as there:
            here.enter_context(self.busy_here())
            there.enter_context(self.busy_second_mac())
            status, output = self.waits_there("gate", waiting, there.close)
            self.assertEqual(status, 0, output)
            self.assertIn(f"Ran 2 tests in {self.checkout(0).resolve()}",
                          (self.project / ".build/engineering/remote/gate/run.log").read_text())
            self.assertFalse((self.project / ".build/engineering/gate").exists())
            # Offered again without a second snapshot, preparing connection or push.
            self.assertEqual(output.count("remote: sending "), 1, output)
            self.assertIn(" to fake again", output)
            # Its receipt keeps what the first offer's preparation cost.
            receipt = json.loads((self.project / ".build/engineering/remote/gate/source.json").read_text())
            self.assertTrue(receipt["preparation"]["offeredAgain"])
            self.assertEqual([sent["status"] for sent in receipt["preparation"]["sends"]], [0, 0])
            self.assertIn("remote: sent at the first offer, after staging ", output)
        shutil.rmtree(self.project / ".build/engineering/remote/gate")
        with self.subTest("this Mac"), contextlib.ExitStack() as here, contextlib.ExitStack() as there:
            here.enter_context(self.busy_here())
            there.enter_context(self.busy_second_mac())
            status, output = self.waits_there("gate", waiting, here.close)
            self.assertEqual(status, 0, output)
            receipt = self.ran_here()
            self.assertTrue(receipt["hereBecause"].startswith("the second Mac is busy: "), receipt)
            self.assertFalse((self.project / ".build/engineering/remote/gate").exists())

    def test_a_lane_this_mac_takes_after_sending_a_snapshot_runs_only_the_tree_it_sent(self):
        # vocaro-engineering#79: a session removed a file after the snapshot went, and this Mac then ran
        # the lane over the tree without it.
        self.work_in_progress()
        waiting, lane = "waits for whichever Mac admits it first", self.project / ".build/engineering/gate"
        with self.subTest("unchanged"), contextlib.ExitStack() as here, contextlib.ExitStack() as there:
            here.enter_context(self.busy_here())
            there.enter_context(self.busy_second_mac())
            status, output = self.waits_there("gate", waiting, here.close)
            self.assertEqual(status, 0, output)
            receipt = self.ran_here()
            sent = re.search(r"remote: sending (\w+), a snapshot of the working tree", output).group(1)
            self.assertTrue(receipt["sent"].startswith(sent), receipt)
            # The tree the run read, as it stood once admitted.
            self.assertEqual(receipt["project"], config.source_identity(self.project))
        shutil.rmtree(lane)
        with self.subTest("changed"), contextlib.ExitStack() as here, contextlib.ExitStack() as there:
            here.enter_context(self.busy_here())
            there.enter_context(self.busy_second_mac())
            def change():
                (self.project / "new.txt").unlink()
                here.close()
            status, output = self.waits_there("gate", waiting, change)
            self.assertEqual(status, 2, output)
            self.assertIn("eng: gate not run on this Mac: the working tree changed after ", output)
            self.assertIn("D\tnew.txt", output)
            self.assertFalse(lane.exists(), output)
        (self.project / "new.txt").write_text("new\n")
        # The toolkit's snapshot is held to what was sent the same way (vocaro-engineering#104).
        settings = self.toolkit / "engineering.json"
        settings.write_text(settings.read_text() + "\n")
        with self.subTest("toolkit changed"), contextlib.ExitStack() as here, contextlib.ExitStack() as there:
            here.enter_context(self.busy_here())
            there.enter_context(self.busy_second_mac())
            def change():
                settings.write_text(settings.read_text() + "\n")
                here.close()
            status, output = self.waits_there("gate", waiting, change)
            self.assertEqual(status, 2, output)
            self.assertIn("eng: gate not run on this Mac: the toolkit's working tree changed after ", output)
            self.assertIn("M\tengineering.json", output)
            self.assertFalse(lane.exists(), output)

    @patch.object(remote, "OVERFLOW_TIMEOUT", 8)
    def test_a_lane_that_neither_mac_admits_in_time_runs_nowhere_and_if_free_asks_each_once(self):
        with self.busy_here(), self.busy_second_mac():
            started = time.monotonic()
            status, output, error = self.run_here("gate")
            waited = time.monotonic() - started
            self.assertEqual(status, resources.BUSY, output + error)
            self.assertIn("eng: gate not run: neither Mac admitted it in 8s; the second Mac is busy: fake did not take "
                          "it", error)
            self.assertIn("waits for whichever Mac admits it first", error)
            self.assertGreaterEqual(waited, 8)
            status, output, error = self.run_here("--if-free", "gate")
            self.assertEqual(status, resources.BUSY, output + error)
            self.assertIn("eng: gate not run: neither Mac admitted it now", error)
            self.assertNotIn(" again", error)
        for place in (".build/engineering/gate", ".build/engineering/remote/gate"):
            self.assertFalse((self.project / place).exists())

    def offline(self):
        """Each way the second Mac cannot be reached, as a function that brings it about in an ExitStack,
        with what eng says of it."""
        def unresolved(stack):
            # RFC 6761: an .invalid name never resolves.
            stack.enter_context(patch.dict(os.environ, {"FAKE_HOSTNAME": "fake.invalid"}))

        def silent(stack):
            # A port nothing listens on refuses at once, as a second Mac without sshd running does.
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as closed:
                closed.bind(("127.0.0.1", 0))
                port = closed.getsockname()[1]
            stack.enter_context(patch.dict(os.environ, {"FAKE_PORT": str(port)}))

        def ssh(stack):
            # The check's connection answered, but ssh itself could not resolve the name.
            stack.enter_context(patch.dict(os.environ, {"FAKE_UNREACHABLE": "1"}))

        return [("unresolved", unresolved, "fake cannot be reached: ssh: Could not resolve hostname fake.invalid"),
                ("silent", silent, "fake cannot be reached: nothing answered on port"),
                ("ssh", ssh, "fake cannot be reached: ssh: Could not resolve hostname fake")]

    def test_a_second_mac_that_cannot_be_reached_runs_the_lane_here_and_its_receipt_says_why(self):
        local = self.project / ".build/engineering/gate"
        for name, take_offline, said in self.offline():
            with self.subTest(name), contextlib.ExitStack() as stack:
                shutil.rmtree(local, ignore_errors=True)
                take_offline(stack)
                started = time.monotonic()
                status, output, error = self.run_here("gate")
                self.assertEqual(status, 0, output + error)
                # One line says why, and the check took seconds, not ConnectTimeout for each address.
                lines = [line for line in error.splitlines() if line.startswith("eng: running gate on this Mac: ")]
                self.assertEqual(len(lines), 1, error)
                self.assertIn(said, lines[0])
                self.assertNotIn("not run", error)
                self.assertLess(time.monotonic() - started, 60)
                self.assertIn(f"Ran 2 tests in {self.project.resolve()}", (local / "run.log").read_text())
                receipt = json.loads((local / "source.json").read_text())
                self.assertEqual(receipt["ranOn"], "this Mac")
                self.assertIn(said, receipt["hereBecause"])
                # Nothing was sent.
                self.assertFalse((self.home / remote.ROOT / "checkouts").exists())
        # The negative control: reached, it goes there.
        self.assertEqual(self.run_here("gate")[0], 0)
        self.assertTrue((self.home / remote.ROOT / "checkouts").exists())

    def test_a_second_mac_that_goes_offline_after_the_check_runs_the_lane_here_while_nothing_was_sent(self):
        # ssh fails at the connection that prepares the second Mac, before anything is sent.
        with patch.object(remote, "reachable"), patch.dict(os.environ, {"FAKE_UNREACHABLE": "1"}):
            status, output, error = self.run_here("gate")
        self.assertEqual(status, 0, output + error)
        self.assertIn("eng: running gate on this Mac: fake cannot be reached: ssh: Could not resolve hostname fake", error)
        self.assertIn(f"Ran 2 tests in {self.project.resolve()}",
                      (self.project / ".build/engineering/gate/run.log").read_text())

    def test_a_run_whose_connection_is_lost_after_it_was_sent_is_unmeasured_and_never_runs_here(self):
        marker = self.checkout(0) / ".build/slow.pid"
        real, ssh = self.tools / "fake-ssh", self.tools / "ssh"
        ssh.rename(real)
        ssh.write_text(DROPPING_SSH)
        ssh.chmod(0o755)
        (self.tools / "drop.py").write_text(DROPPER)
        variables = {"FAKE_SSH_REAL": str(real), "FAKE_DROPPER": str(self.tools / "drop.py"),
                     "FAKE_PYTHON": sys.executable, "FAKE_DROP": str(marker)}
        lane = runner = None
        try:
            with patch.dict(os.environ, variables):
                status, output, error = self.run_here("slow")
            lane = int(marker.read_text())
            # offload_host.py, which claimed the checkout and ends once the run there has left its admission.
            runner = json.loads((self.home / remote.ROOT / "checkouts/example/0.lock").read_text())["pid"]
            self.assertEqual(status, evidence.UNMEASURED, output + error)
            self.assertIn("eng: No result from fake (ssh exited 255: Timeout, server fake not responding.)", error)
            self.assertIn("The run is unmeasured", error)
            self.assertNotIn("running slow on this Mac", error)
            self.assertFalse((self.project / ".build/engineering/slow").exists())
            # Its end of the connection closed, so the second Mac stopped the run.
            for pid in (lane, runner):
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    try: os.kill(pid, 0)
                    except ProcessLookupError: break
                    time.sleep(0.1)
                with self.assertRaises(ProcessLookupError):
                    os.kill(pid, 0)
        finally:
            for pid in (lane, runner):
                if pid is not None:
                    with contextlib.suppress(ProcessLookupError): os.kill(pid, signal.SIGKILL)

    def test_the_check_before_a_dispatch_tells_an_offline_second_mac_from_a_misconfigured_one(self):
        reached = {"hostname": "fake.local", "port": 22, "addressFamily": "any",
                   "addresses": [{"address": "192.0.2.7", "seconds": 0.004, "outcome": "connected"}]}
        silent = {**reached, "addresses": [{"address": "fe80::1%en0", "seconds": 3.0, "outcome": "no answer in 3 s"},
                                           {"address": "192.0.2.7", "seconds": 0.001, "outcome": "Connection refused"}]}
        done = lambda status, said="": subprocess.CompletedProcess([], status, "", said)
        cases = [
            ({**reached, "addresses": None, "unresolved": "[Errno 8] nodename nor servname provided"}, None,
             remote.Unreachable, "Could not resolve hostname fake.local: [Errno 8]"),
            (silent, None, remote.Unreachable, "nothing answered on port 22 of fake.local (fe80::1%en0: no answer in 3 s; "
                                               "192.0.2.7: Connection refused)"),
            (reached, done(255, "ssh: connect to host fake.local port 22: Operation timed out\n"), remote.Unreachable,
             "Operation timed out"),
            (reached, subprocess.TimeoutExpired(["ssh"], remote.REACH_SECONDS), remote.Unreachable,
             f"`ssh fake true` did not finish in {remote.REACH_SECONDS}s"),
            (reached, done(255, "someone@fake.local: Permission denied (publickey).\n"), remote.Misconfigured,
             "ssh reached fake but could not log in: someone@fake.local: Permission denied"),
            (reached, done(255, "Host key verification failed.\n"), remote.Misconfigured, "could not log in"),
            (reached, done(1, "zsh: no such file or directory: /opt/homebrew/bin/brew\n"), remote.Misconfigured,
             "fake's login shell could not run `true` (exit 1)"),
            # A host reached through a proxy is checked by ssh alone.
            ({"hostname": "fake.local", "port": 22, "addressFamily": "any", "unchecked": "through a proxy"}, done(0),
             None, None),
            (reached, done(0), None, None)]
        for found, checked, kind, said in cases:
            with self.subTest(found=found, checked=checked), \
                    patch.object(remote, "connection", return_value=found) as measured, \
                    patch.object(remote, "ssh", side_effect=[checked]) as ssh:
                if kind is None:
                    remote.reachable("fake")
                else:
                    with self.assertRaises(kind) as raised:
                        remote.reachable("fake")
                    self.assertIn(said, str(raised.exception))
                    # A misconfiguration is never taken for an offline second Mac, nor the reverse.
                    self.assertEqual(isinstance(raised.exception, remote.Misconfigured), kind is remote.Misconfigured)
            # Each address has REACH_TIMEOUT, and ssh REACH_SECONDS, not ConnectTimeout for each address.
            self.assertEqual(measured.call_args.args, ("fake", remote.REACH_TIMEOUT))
            if ssh.called: self.assertEqual(ssh.call_args.kwargs["timeout"], remote.REACH_SECONDS)
        # After the work was sent, only a line that says ssh never connected is an offline second Mac.
        self.assertIsInstance(remote.ssh_failure("fake", "Timeout, server fake not responding.", sent=True),
                              remote.Misconfigured)
        self.assertIsInstance(remote.ssh_failure("fake", "Timeout, server fake not responding."), remote.Unreachable)
        self.assertIsInstance(remote.ssh_failure("fake", "ssh: connect to host fake port 22: No route to host",
                                                 sent=True), remote.Unreachable)

    def misconfigurations(self):
        """Each configuration problem of the second Mac's that stops a lane, as a function that
        brings it about in an ExitStack, with what eng says of it."""
        def login(stack):
            stack.enter_context(patch.dict(os.environ, {"FAKE_DENIED": "1"}))

        def xcode(stack):
            # The second Mac has nothing at this Mac's developer directory.
            local = {**offload_host.identity(None), "xcode": "Xcode 99.0\nBuild version 99A1"}
            stack.enter_context(patch.object(remote, "developer_directory", return_value=self.temp.name))
            stack.enter_context(patch.object(remote.offload_host, "identity", return_value=local))

        def toolkit(stack):
            # The second Mac's login shell has no python3 to run the toolkit.
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

        return [("login", login, ["ssh reached fake but could not log in: someone@fake: Permission denied",
                                  "authorized_keys, until `ssh fake true` succeeds"]),
                ("xcode", xcode,["fake refused it: the Xcode at", "not the dispatcher's Xcode 99.0 Build version 99A1",
                                  "install this Mac's Xcode at that path on the second Mac"]),
                ("toolkit", toolkit, ["fake could not run the toolkit", "command not found: python3",
                                      "eng remote setup"]),
                ("provisioning", provisioning, ["fake could not provision its checkout: the provision lane exited 4",
                                                "what provisioning needs"])]

    def test_a_misconfigured_second_mac_stops_the_lane_until_here_runs_it_on_this_mac(self):
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

    def test_a_push_that_loses_the_race_for_a_ref_the_second_mac_holds_has_sent_the_commit(self):
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

    def test_offload_only_passes_the_second_macs_status_through_and_never_runs_the_lane_here(self):
        status, output, error = self.run_here("--offload-only", "gate")
        self.assertEqual(status, 0, output + error)
        self.assertIn(f"Ran 2 tests in {self.checkout(0).resolve()}",
                      (self.project / ".build/engineering/remote/gate/run.log").read_text())
        self.assertEqual(self.eng("remote", "passed", "gate")[0], 0)
        self.assertEqual(self.run_here("--offload-only", "failing")[0], 5)
        self.assertIn("Ran 2 tests", (self.project / ".build/engineering/remote/failing/run.log").read_text())
        for lane in ["gate", "failing"]:
            self.assertFalse((self.project / ".build/engineering" / lane).exists())

    def test_offload_only_exits_busy_when_the_second_mac_declines_and_stops_when_it_is_misconfigured(self):
        with self.busy_second_mac():
            status, output, error = self.run_here("--offload-only", "gate")
        self.assertEqual(status, resources.BUSY, output + error)
        self.assertIn("eng: gate not run: the second Mac is busy: fake did not take it: its heavy-work queue cannot "
                      "take the lane now", error)
        with patch.dict(os.environ, {"VOCARO_OFFLOAD_HOST": "none"}):
            status, output, error = self.run_here("--offload-only", "gate")
        self.assertEqual(status, resources.BUSY, output + error)
        self.assertIn("eng: gate not run: no second Mac is configured", error)
        # An offline second Mac declines too, and the caller runs the lane here itself.
        with patch.dict(os.environ, {"FAKE_UNREACHABLE": "1"}):
            status, output, error = self.run_here("--offload-only", "gate")
        self.assertEqual(status, resources.BUSY, output + error)
        self.assertIn("eng: gate not run: fake cannot be reached", error)
        with patch.dict(os.environ, {"FAKE_DENIED": "1"}):
            status, output, error = self.run_here("--offload-only", "gate")
        self.assertEqual(status, remote.MISCONFIGURED, output + error)
        self.assertIn("ssh reached fake but could not log in", error)
        # Never here, and no result of the second Mac's either.
        self.assertFalse((self.project / ".build/engineering/gate").exists())
        self.assertFalse((self.project / ".build/engineering/remote/gate").exists())
        # A toolkit checkout with changes no longer declines: it goes as a snapshot (vocaro-engineering#104).
        (self.toolkit / "engineering.json").write_text((self.toolkit / "engineering.json").read_text() + "\n")
        status, output, error = self.run_here("--offload-only", "gate")
        self.assertEqual(status, 0, output + error)
        self.assertTrue(json.loads((self.project / ".build/engineering/remote/gate/source.json").read_text())
                        ["toolkitSnapshot"])
        self.assertFalse((self.project / ".build/engineering/gate").exists())
        git(self.toolkit, "checkout", "--", "engineering.json")
        shutil.rmtree(self.project / ".build/engineering/remote/gate")
        # A lane the second Mac never takes, and --here, contradict the mode.
        for lane, because in [("build", "the lane says: its goldens"), ("local-pair", "its lane build says"),
                              ("fail", "a light lane runs where it starts")]:
            status, _output, error = self.run_here("--offload-only", lane)
            self.assertEqual(status, 2)
            self.assertIn(f"{lane} stays on this Mac ({because}", error)
        with self.assertRaises(SystemExit):
            self.run_here("--offload-only", "--here", "gate")

    def test_the_old_name_of_offload_only_is_refused_with_the_new_one_and_hidden_from_help(self):
        refused = ("eng: --build-host-only was --offload-only's name until vocaro-engineering#58, and is no longer "
                   "read: use --offload-only (guides/shared-host.md, Earlier names)\n")
        # Refused whatever the lane and beside any other flag, before anything runs on either Mac.
        for arguments in (["gate"], ["failing"], ["build"], ["fail"], ["pair"], ["--here", "gate"],
                          ["--offload-only", "gate"], ["--if-free", "gate"], ["--serial", "pair"], ["missing"]):
            with self.subTest(arguments):
                status, output, error = self.run_here("--build-host-only", *arguments)
                self.assertEqual((status, error), (2, refused), output + error)
        for place in ["gate", "failing", "build", "fail", "pair", "remote/gate", "remote/failing", "remote/pair"]:
            self.assertFalse((self.project / ".build/engineering" / place).exists(), place)
        self.assertFalse((self.home / remote.ROOT).exists())
        # Help names only the new flag.
        with contextlib.redirect_stdout(StringIO()) as shown, self.assertRaises(SystemExit):
            cli.main(["--project", str(self.project), "run", "--help"])
        self.assertIn("--offload-only", shown.getvalue())
        self.assertNotIn("build-host", shown.getvalue())
        # The positive control: the new name offers the lane to the second Mac, which runs it.
        status, output, error = self.run_here("--offload-only", "gate")
        self.assertEqual(status, 0, output + error)
        self.assertEqual(json.loads((self.project / ".build/engineering/remote/gate/source.json").read_text())["ranOn"],
                         "second Mac")
        self.assertNotIn("vocaro-engineering#58", error)

    def test_the_old_name_of_the_offload_variable_is_refused_before_eng_chooses_a_mac(self):
        refused = ("VOCARO_BUILD_HOST was VOCARO_OFFLOAD_HOST's name until vocaro-engineering#58, and is no longer "
                   "read: unset it, and set VOCARO_OFFLOAD_HOST instead, to the same value (none keeps work on this "
                   "Mac). eng chooses no Mac while it is set, since ignoring it could send work to the second Mac "
                   "that it was meant to keep here (guides/shared-host.md, Earlier names)")
        receipt = lambda place: json.loads((self.project / ".build/engineering" / place / "source.json").read_text())
        # The positive control: the new name alone names the second Mac, or none, without a word.
        for new, found in [("fake", "fake"), ("none", None)]:
            with patch.dict(os.environ, {"VOCARO_OFFLOAD_HOST": new}), contextlib.redirect_stderr(StringIO()) as said:
                self.assertEqual(remote.configured(), found)
            self.assertEqual(said.getvalue(), "")
        probe = self.probe()
        # Set at all, alone or beside the new name, either way they point, the old name is refused wherever
        # eng would read the new one: before it chooses a Mac for a lane or a command, in eng remote without
        # --host, and in eng doctor. Nothing runs on either Mac.
        for new, old in [(None, "fake"), (None, "none"), (None, ""), ("fake", "none"), ("none", "fake"), ("fake", "fake")]:
            with self.subTest(new=new, old=old), patch.dict(os.environ, {"VOCARO_BUILD_HOST": old}):
                if new is None: os.environ.pop("VOCARO_OFFLOAD_HOST")
                else: os.environ["VOCARO_OFFLOAD_HOST"] = new
                with self.assertRaises(ValueError) as raised: remote.configured()
                self.assertEqual(str(raised.exception), refused)
                for arguments in (["gate"], ["--offload-only", "gate"], ["--if-free", "gate"], ["pair"]):
                    status, output, error = self.run_here(*arguments)
                    self.assertEqual((status, error), (2, f"eng: {refused}\n"), output + error)
                status, output, error = self.admit_in(self.project, "--verify", "--", *probe)
                self.assertEqual((status, error), (2, f"eng: {refused}\n"), output + error)
                for arguments in (["remote", "run", "check"], ["remote", "admit", "--", *probe], ["doctor"]):
                    status, output, error = self.eng(*arguments)
                    self.assertEqual((status, output, error), (2, "", f"eng: {refused}\n"), arguments)
                for place in ["gate", "pair", "check", "admit", "remote/gate", "remote/pair", "remote/check",
                              "remote/admit"]:
                    self.assertFalse((self.project / ".build/engineering" / place).exists(), place)
                self.assertFalse(self.ran.exists())
                self.assertFalse((self.home / remote.ROOT).exists())
        # A command that neither chooses a Mac nor reaches the second Mac never read it, and runs as before:
        # a light lane, a lane that says here, --here, and eng admit without --verify, all here; and eng remote
        # given --host, there.
        with patch.dict(os.environ, {"VOCARO_BUILD_HOST": "none"}):
            for arguments, status, place in [(["fail"], 3, "fail"), (["build"], 0, "build"), (["--here", "gate"], 0, "gate")]:
                result = self.run_here(*arguments)
                self.assertEqual(result[0], status, result)
                self.assertEqual(receipt(place)["ranOn"], "this Mac")
                self.assertNotIn("vocaro-engineering#58", result[2])
            status, output, error = self.admit_in(self.project, "--", *probe)
            self.assertEqual((status, self.ran_in()), (0, self.project.resolve()), output + error)
            status, output, error = self.eng("remote", "run", "--host", "fake", "check")
            self.assertEqual(status, 0, output + error)
            self.assertEqual((receipt("remote/check")["ranOn"], receipt("remote/check")["host"]), ("second Mac", "fake"))
            self.assertNotIn("vocaro-engineering#58", error)

    def test_without_the_variable_the_second_mac_is_the_alias_that_ssh_config_defines(self):
        del os.environ["VOCARO_OFFLOAD_HOST"]
        # build-host, the default, comes first; second-mac serves once it is the one defined.
        for defined, found in [("build-host second-mac", "build-host"), ("second-mac", "second-mac"),
                               ("build-host", "build-host"), ("", None)]:
            with self.subTest(defined), patch.dict(os.environ, {"FAKE_ALIASES": defined}):
                self.assertEqual(remote.configured(), found)
        with patch.dict(os.environ, {"FAKE_ALIASES": "second-mac"}):
            status, output, error = self.run_here("gate")
            self.assertEqual(status, 0, output + error)
            receipt = json.loads((self.project / ".build/engineering/remote/gate/source.json").read_text())
            self.assertEqual((receipt["ranOn"], receipt["host"]), ("second Mac", "second-mac"))
            # eng remote names it too when no --host is given.
            self.assertEqual(self.eng("remote", "run", "check")[0], 0)
            self.assertEqual(json.loads((self.project / ".build/engineering/remote/check/source.json").read_text())
                             ["host"], "second-mac")
        # With neither alias defined, eng remote tries the default.
        self.assertEqual(self.eng("remote", "run", "check")[0], 0)
        self.assertEqual(json.loads((self.project / ".build/engineering/remote/check/source.json").read_text())["host"],
                         "build-host")

    def test_a_lane_stays_here_when_asked_or_when_it_says_why_or_without_a_second_mac(self):
        # Uncommitted work would go as a snapshot, so none of these stays here for want of a commit.
        (self.project / "notes.txt").write_text("uncommitted\n")
        receipt = lambda: json.loads((self.project / ".build/engineering/gate/source.json").read_text())
        for arguments in [["--here", "gate"], ["build"]]:
            with self.subTest(arguments):
                self.assertEqual(self.run_here(*arguments)[0], 0)
        self.assertEqual((receipt()["ranOn"], receipt()["hereBecause"]), ("this Mac", "--here"))
        for setting in [{"VOCARO_OFFLOAD_HOST": "none"}, {}]:
            with self.subTest(setting), patch.dict(os.environ, setting):
                # Without VOCARO_OFFLOAD_HOST, `ssh -G` names no alias of the second Mac.
                if not setting: del os.environ["VOCARO_OFFLOAD_HOST"]
                self.assertEqual(self.run_here("gate")[0], 0)
                self.assertEqual(receipt()["hereBecause"], "no second Mac is configured")
        self.assertFalse((self.home / remote.ROOT).exists())
        # A toolkit checkout with changes no longer keeps the lane here: its tree goes as a snapshot
        # (vocaro-engineering#104).
        (self.project / ".build/engineering/gate/source.json").unlink()
        (self.toolkit / "engineering.json").write_text((self.toolkit / "engineering.json").read_text() + "\n")
        status, output, error = self.run_here("gate")
        self.assertEqual(status, 0, output + error)
        self.assertNotIn("on this Mac", error)
        self.assertFalse((self.project / ".build/engineering/gate/source.json").exists())
        sent = json.loads((self.project / ".build/engineering/remote/gate/source.json").read_text())
        self.assertEqual((sent["ranOn"], sent["toolkitSnapshot"]), ("second Mac", True))

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

    def test_uncommitted_work_runs_on_the_second_mac_as_a_snapshot_and_leaves_the_session_as_it_was(self):
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
                # The second Mac ran the working tree: the edit rather than what is staged, without the
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

    def test_uncommitted_work_runs_here_when_the_second_mac_cannot_be_reached_and_stops_for_a_misconfigured_one(self):
        self.work_in_progress()
        before = self.session()
        with patch.dict(os.environ, {"FAKE_UNREACHABLE": "1"}):
            status, output, error = self.run_here("gate")
        self.assertEqual(status, 0, output + error)
        self.assertIn("running gate on this Mac: fake cannot be reached", error)
        self.assertIn(f"Ran 2 tests in {self.project.resolve()}",
                      (self.project / ".build/engineering/gate/run.log").read_text())
        self.assertFalse((self.project / ".build/engineering/remote/gate").exists())
        shutil.rmtree(self.project / ".build/engineering/gate")
        with patch.dict(os.environ, {"FAKE_DENIED": "1"}):
            status, output, error = self.run_here("gate")
        self.assertEqual(status, remote.MISCONFIGURED, output + error)
        self.assertIn("ssh reached fake but could not log in", error)
        self.assertFalse((self.project / ".build/engineering/gate").exists())
        self.assertEqual(self.session(), before)

    def test_offload_only_sends_uncommitted_work_as_a_snapshot(self):
        base = self.work_in_progress()
        status, output, error = self.run_here("--offload-only", "gate", "--", "a")
        self.assertEqual(status, 0, output + error)
        receipt = json.loads((self.project / ".build/engineering/remote/gate/source.json").read_text())
        self.assertEqual((receipt["head"], receipt["snapshot"], receipt["arguments"]), (base, True, ["a"]))
        self.assertEqual(git(self.checkout(0), "rev-parse", "HEAD"), receipt["commit"])
        with self.busy_second_mac():
            status, output, error = self.run_here("--offload-only", "gate")
        self.assertEqual(status, resources.BUSY, output + error)
        self.assertIn("eng: gate not run: the second Mac is busy: fake did not take it", error)
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
                         [".gitignore", "edited.txt", "engineering.json", "new.txt", "pkg/README.md"])

    def test_nested_work_runs_its_lane_inside_the_admission_it_inherits(self):
        inner = [sys.executable, str(self.toolkit / "bin/eng"), "--project", str(self.project), "run", "gate"]
        with patch.object(cli, "refresh_policy", return_value="policy"):
            self.assertEqual(self.eng("admit", "--", "/bin/sh", "-c", f"exec {shlex.join(inner)} >/dev/null 2>&1")[0], 0)
        self.assertIn(f"Ran 2 tests in {self.project.resolve()}",
                      (self.project / ".build/engineering/gate/run.log").read_text())
        self.assertEqual(json.loads((self.project / ".build/engineering/gate/source.json").read_text())["hereBecause"],
                         "nested in the admission it inherits")
        self.assertFalse((self.home / remote.ROOT).exists())

    def test_configuration_refuses_buildhost_on_any_lane_or_group_whatever_its_value(self):
        # vocaro-engineering#58 removed it. True, which sent a lane to the second Mac, and false, which no longer
        # kept one here, are refused alike, on a lane of any kind and on a group, before any other check.
        refused = ("engineering.json gives {} \"buildHost\", which vocaro-engineering#58 removed: every heavy or "
                   "exclusive lane goes to the second Mac unless it says why it stays, with \"here\": \"REASON\", so "
                   "delete {}, and rebase a checkout older than that change onto main (guides/shared-host.md, "
                   "Earlier names)")
        heavy = {"command": ["true"], "resource": "heavy"}
        kinds = [{"lane": heavy}, {"lane": {"command": ["true"]}}, {"lane": {**heavy, "foreground": True}},
                 {"lane": {**heavy, "here": "GPU goldens"}}, {"lane": heavy, "group": {"parallel": ["lane"], "resource": "heavy"}}]
        for lanes in kinds:
            marked = "group" if "group" in lanes else "lane"
            for value in [True, False, 1, None]:
                with self.subTest(lanes=lanes, value=value):
                    (self.project / "engineering.json").write_text(json.dumps({"schemaVersion": 1, "name": "Example",
                        "lanes": {**lanes, marked: {**lanes[marked], "buildHost": value}}}))
                    with self.assertRaises(ValueError) as raised: config.load(self.project)
                    self.assertEqual(str(raised.exception), refused.format(marked, "it"))
        # It names every lane and group that gives it, so that one change fixes them all.
        (self.project / "engineering.json").write_text(json.dumps({"schemaVersion": 1, "name": "Example", "lanes": {
            "a": {**heavy, "buildHost": True}, "b": heavy, "c": {**heavy, "buildHost": False},
            "g": {"parallel": ["b"], "resource": "heavy", "buildHost": True}}}))
        with self.assertRaises(ValueError) as raised: config.load(self.project)
        self.assertEqual(str(raised.exception), refused.format("a, c and g", "each"))
        # The positive control: the same lanes without it load, and the heavy ones go to the second Mac.
        for lanes, reason in zip(kinds, [None, "a light lane runs where it starts", None,
                                         "the lane says: GPU goldens", None]):
            with self.subTest(lanes=lanes), contextlib.redirect_stderr(StringIO()) as said:
                (self.project / "engineering.json").write_text(json.dumps({"schemaVersion": 1, "name": "Example",
                                                                           "lanes": lanes}))
                loaded = config.load(self.project)["lanes"]
                self.assertEqual(config.stays(loaded, "group" if "group" in lanes else "lane"), reason)
            self.assertEqual(said.getvalue(), "")

    def test_a_lane_that_stays_here_says_why_and_runs_here(self):
        heavy = {"command": ["true"], "resource": "heavy"}
        invalid = {
            "a lane that stays here gives no reason": {"lane": {**heavy, "here": ""}},
            "a reason is words": {"lane": {**heavy, "here": True}},
            "a blank reason": {"lane": {**heavy, "here": "  "}},
            "a light lane runs here already": {"lane": {"command": ["true"], "here": "GPU goldens"}},
            "a light foreground lane is not admitted": {"lane": {"command": ["true"], "foreground": True}},
            "a group's own reason": {"other": heavy, "group": {"parallel": ["other"], "resource": "heavy", "here": ""}}}
        for case, lanes in invalid.items():
            with self.subTest(case):
                (self.project / "engineering.json").write_text(json.dumps(
                    {"schemaVersion": 1, "name": "Example", "lanes": lanes}))
                with self.assertRaises(ValueError): config.load(self.project)
        # The positive controls: a lane, and a group, that say why, a group that runs such a lane, and a
        # lane that says nothing, which goes to the second Mac.
        goldens = {"command": [sys.executable, "-c", "print('Ran 1 tests')"], "resource": "heavy",
                   "minimumTests": 1, "here": "its goldens come from this Mac's GPU"}
        lanes = {"goldens": goldens, "other": heavy,
                 "group": {"parallel": ["goldens", "other"], "resource": "heavy"},
                 "own": {"parallel": ["other"], "resource": "heavy", "here": "a measurement tied to this Mac"}}
        (self.project / "engineering.json").write_text(json.dumps({"schemaVersion": 1, "name": "Example", "lanes": lanes}))
        loaded = config.load(self.project)["lanes"]
        self.assertEqual([config.stays(loaded, name) for name in lanes],
                         ["the lane says: its goldens come from this Mac's GPU", None,
                          "its lane goldens says: its goldens come from this Mac's GPU",
                          "the lane says: a measurement tied to this Mac"])
        # eng run runs it here, though every heavy lane that says nothing goes to the second Mac.
        status, output, error = self.run_here("goldens")
        self.assertEqual(status, 0, output + error)
        self.assertTrue((self.project / ".build/engineering/goldens/run.log").exists())
        self.assertFalse((self.home / remote.ROOT).exists())

    def admit_in(self, directory, *arguments):
        """eng admit run from directory, with this Mac's policy refresh left out."""
        previous = os.getcwd()
        os.chdir(directory)
        try:
            with patch.object(cli, "refresh_policy", return_value="policy"):
                return self.eng("admit", *arguments)
        finally:
            os.chdir(previous)

    def probe(self):
        """A command that appends the directory it runs in to a file outside the checkout, which both Macs
        reach: a command run here writes to this terminal, not to the output eng captures."""
        self.ran = Path(self.temp.name) / "ran-in.txt"
        return [sys.executable, "-c", "import os, sys; open(sys.argv[1], 'a').write(os.getcwd() + '\\n')", str(self.ran)]

    def ran_in(self):
        return Path(self.ran.read_text().splitlines()[-1])

    def test_admit_verify_sends_a_command_to_the_second_mac_to_run_in_the_matching_directory(self):
        probe = self.probe()
        (self.project / "pkg/new.txt").write_text("uncommitted\n")
        status, output, error = self.admit_in(self.project / "pkg", "--verify", "--", *probe)
        self.assertEqual(status, 0, output + error)
        self.assertEqual(self.ran_in(), (self.checkout(0) / "pkg").resolve())
        receipt = json.loads((self.project / ".build/engineering/remote/admit/source.json").read_text())
        self.assertEqual((receipt["ranOn"], receipt["subdirectory"], receipt["snapshot"]), ("second Mac", "pkg", True))
        self.assertEqual((receipt["result"]["receipt"]["ranOn"], receipt["result"]["receipt"]["directory"]),
                         ("second Mac", str((self.checkout(0) / "pkg").resolve())))
        self.assertFalse((self.project / ".build/engineering/admit").exists())
        # From the checkout's root it runs in the root there.
        status, output, error = self.admit_in(self.project, "--verify", "--", *probe)
        self.assertEqual(status, 0, output + error)
        self.assertEqual(self.ran_in(), self.checkout(0).resolve())
        # A busy second Mac declines it, as `eng admit --if-free` there, and this Mac admits it.
        with self.busy_second_mac():
            status, output, error = self.admit_in(self.project / "pkg", "--verify", "--", *probe)
        self.assertEqual(status, 0, output + error)
        self.assertEqual(self.ran_in(), (self.project / "pkg").resolve())
        receipt = json.loads((self.project / ".build/engineering/admit/source.json").read_text())
        self.assertEqual(receipt["ranOn"], "this Mac")
        self.assertTrue(receipt["hereBecause"].startswith("the second Mac is busy: fake did not take it"), receipt)
        shutil.rmtree(self.project / ".build/engineering/admit")
        # The negative control: without --verify it runs here.
        status, output, error = self.admit_in(self.project / "pkg", "--", *probe)
        self.assertEqual(status, 0, output + error)
        self.assertEqual(self.ran_in(), (self.project / "pkg").resolve())
        receipt = json.loads((self.project / ".build/engineering/admit/source.json").read_text())
        self.assertEqual((receipt["ranOn"], receipt["hereBecause"]), ("this Mac", "eng admit runs here without --verify"))

    def test_admit_verify_keeps_here_what_the_second_mac_could_not_run_as_it_runs_here(self):
        probe = self.probe()
        (self.project / ".build/scratch").mkdir(parents=True)
        outside = Path(self.temp.name) / "elsewhere"
        outside.mkdir()
        cases = [(outside, [], f"its directory {outside.resolve()} is outside the checkout"),
                 (self.project / ".build/scratch", [], "its directory .build/scratch is one Git ignores"),
                 (self.project, [str(self.project.resolve() / "pkg")], "it names this checkout's path"),
                 (self.project, [f"platform=iOS Simulator,id={DUO}"], "it names a simulator by its identifier"),
                 # A hardware UDID that no entry of this Mac's record holds, as #57 refuses it without --device.
                 (self.project, [f"platform=iOS,id={PHONE}"], f"it names the hardware UDID {PHONE}"),
                 (self.project, [], "fake cannot be reached")]
        for directory, words, said in cases:
            with self.subTest(said), patch.dict(os.environ, {"FAKE_UNREACHABLE": "1"} if "reached" in said else {}):
                status, output, error = self.admit_in(directory, "--verify", "--", *probe, *words)
                self.assertEqual(status, 0, output + error)
                self.assertEqual(self.ran_in(), directory.resolve())
                self.assertIn(f"eng: running the command on this Mac: {said}", error)
                receipt = json.loads((self.project / ".build/engineering/admit/source.json").read_text())
                self.assertEqual(receipt["ranOn"], "this Mac")
                self.assertIn(said, receipt["hereBecause"])
        self.assertFalse((self.home / remote.ROOT / "checkouts").exists())
        # Nested in an admission, it runs inside it, here.
        inner = [sys.executable, str(self.toolkit / "bin/eng"), "--project", str(self.project), "admit", "--verify",
                 "--", *probe]
        status, output, error = self.admit_in(self.project, "--", "/bin/sh", "-c", f"exec {shlex.join(inner)} 2>/dev/null")
        self.assertEqual(status, 0, output + error)
        self.assertEqual(self.ran_in(), self.project.resolve())
        self.assertFalse((self.home / remote.ROOT / "checkouts").exists())
        # A misconfigured second Mac stops it, and nothing runs here.
        shutil.rmtree(self.project / ".build/engineering/admit")
        self.ran.unlink()
        with patch.dict(os.environ, {"FAKE_DENIED": "1"}):
            status, output, error = self.admit_in(self.project, "--verify", "--", *probe)
        self.assertEqual(status, remote.MISCONFIGURED, output + error)
        self.assertFalse(self.ran.exists())
        self.assertIn("then `eng admit` without --verify", error)
        self.assertFalse((self.project / ".build/engineering/admit").exists())

    def test_the_toolkit_runs_itself_without_a_second_checkout(self):
        settings = json.loads((self.toolkit / "engineering.json").read_text())
        # Heavy, so the second Mac's policy refresh fetches the toolkit's origin from its own checkout.
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

    def relaying(self, **variables):
        """RELAYING_SSH in front of the stand-in ssh while the block runs, with these variables."""
        real, ssh = self.tools / "fake-ssh", self.tools / "ssh"
        if not real.exists():
            ssh.rename(real)
            ssh.write_text(RELAYING_SSH)
            ssh.chmod(0o755)
            (self.tools / "relay.py").write_text(RELAY)
        return patch.dict(os.environ, {"FAKE_SSH_REAL": str(real), "FAKE_RELAY": str(self.tools / "relay.py"),
                                       "FAKE_PYTHON": sys.executable, "FAKE_PASSED": str(self.passed_file()),
                                       **variables})

    def passed_file(self):
        return Path(self.temp.name) / "passed"

    def passed(self):
        """The bytes that reached receive-pack in each push since the last call, as RELAY counted them."""
        counted = self.passed_file()
        found = [int(line) for line in counted.read_text().split()] if counted.exists() else []
        counted.unlink(missing_ok=True)
        return found

    def bare(self, name="example"):
        return self.home / remote.ROOT / f"repositories/{name}.git"

    def dispatched(self, lane="check"):
        """Run `eng remote run LANE`; return the bytes that reached the project's receive-pack and the
        receipt."""
        status, output, error = self.eng("remote", "run", lane)
        self.assertEqual(status, 0, output + error)
        passed = self.passed()
        # The project's push comes first, then the toolkit's.
        self.assertEqual(len(passed), 2, error)
        return passed[0], json.loads((self.project / f".build/engineering/remote/{lane}/source.json").read_text()), error

    def test_a_push_sends_only_what_the_second_macs_checkouts_lack(self):
        (self.project / "large.bin").write_bytes(os.urandom(LARGE))
        first = commit_all(self.project)
        kit = git(self.toolkit, "rev-parse", "HEAD")
        cached = lambda: git(self.bare(), "for-each-ref", "--format=%(refname)", offload_host.CACHE).split()
        with self.relaying():
            # Cold: the second Mac holds nothing, so the whole history goes. Placing it deletes the ref that
            # carried it; its checkout holds it, detached. The same commit again: the second Mac names what
            # its checkout holds, and nothing goes.
            cold, receipt, error = self.dispatched()
            self.assertEqual(git(self.bare(), "for-each-ref", remote.REF), "")
            passed, again, said = self.dispatched()
            self.assertGreaterEqual(cold, LARGE)
            self.assertLess(passed, 64 << 10)
            sent = receipt["preparation"]["sends"][0]
            self.assertEqual((sent["commit"], sent["status"], sent["transfer"], sent["advertised"]), (first, 0, "cold", 0))
            self.assertGreaterEqual(sent["bytes"], LARGE)
            self.assertEqual(list(sent["phases"]), ["connect", "pack", "transfer", "receiver"])
            self.assertEqual(sent["pushNegotiate"], None)
            self.assertIn(f"remote: sent {first[:12]} to ~/{remote.ROOT}/repositories/example.git in ", error)
            self.assertIn("cold: the second Mac's checkouts hold no commit; it sent ", error)
            receipt, error = again, said
            sent = receipt["preparation"]["sends"][0]
            self.assertEqual((sent["transfer"], sent["advertised"], sent["held"], sent["objects"]), ("warm", 1, 1, 0))
            self.assertIn("it sent nothing, the commit being there", error)
            self.assertEqual(receipt["preparation"]["sends"][1]["transfer"], "warm")
            self.assertEqual(cached(), [offload_host.CACHE + first])
            # A small change on it: only the new objects go.
            (self.project / "README.md").write_text("a small change\n")
            second = commit_all(self.project)
            passed, receipt, error = self.dispatched()
            self.assertLess(passed, 64 << 10)
            sent = receipt["preparation"]["sends"][0]
            self.assertEqual((sent["commit"], sent["transfer"]), (second, "warm"))
            self.assertLessEqual(sent["objects"], 3)
            self.assertLess(sent["bytes"], 4 << 10)
            self.assertEqual(git(self.checkout(0), "rev-parse", "HEAD"), second)
            self.assertEqual(git(self.checkout(0).parent / "vocaro-engineering", "rev-parse", "HEAD"), kit)
            # A repository whose checkouts hold its commits only as detached worktrees, as one prepared before
            # vocaro-engineering#67 does. The negative control: pushed as eng pushed then, without the refs,
            # the next change takes its whole history along.
            for ref in cached(): git(self.bare(), "update-ref", "-d", ref)
            (self.project / "README.md").write_text("another change\n")
            third = commit_all(self.project)
            with contextlib.redirect_stderr(StringIO()):
                sent = remote.send(self.project, "fake", f"{remote.ROOT}/repositories/example.git", third)
            self.assertGreaterEqual(self.passed()[0], LARGE)
            self.assertEqual((sent["status"], sent["transfer"]), (0, "unknown"))
            git(self.bare(), "update-ref", "-d", f"{remote.REF}{third}")
            # Once the preparing connection names what the checkouts hold, the next change goes alone.
            (self.project / "README.md").write_text("a third change\n")
            fourth = commit_all(self.project)
            passed, receipt, error = self.dispatched()
            self.assertLess(passed, 64 << 10)
            self.assertEqual(receipt["preparation"]["sends"][0]["transfer"], "warm")
            self.assertEqual(git(self.checkout(0), "rev-parse", "HEAD"), fourth)
            # A missing cache: with the checkout gone, the refs go too, and the whole history again, although
            # its objects are still there.
            git(self.bare(), "worktree", "remove", "--force", str(self.checkout(0)))
            (self.project / "README.md").write_text("a fourth change\n")
            fifth = commit_all(self.project)
            passed, receipt, error = self.dispatched()
            self.assertGreaterEqual(passed, LARGE)
            sent = receipt["preparation"]["sends"][0]
            self.assertEqual((sent["transfer"], sent["advertised"]), ("cold", 0))
            self.assertEqual(git(self.checkout(0), "rev-parse", "HEAD"), fifth)
        # The preparation's other parts, the second Mac's, and the queue's wait apart from them.
        seconds = receipt["preparation"]["seconds"]
        self.assertEqual(sorted(seconds), ["connection", "send", "staging"])
        self.assertEqual(sorted(receipt["result"]["phases"]), ["claim", "place", "provision", "work"])
        self.assertIsInstance(receipt["result"]["receipt"]["queueSeconds"], float)
        for side in ("here", "there"): self.assertTrue(receipt["preparation"]["git"][side].startswith("git version "))
        self.assertIn("remote: before the queue: here, staging ", error)
        self.assertIn("; then the queue's wait, ", error)
        # Each checkout's commit, and nothing else, is named: as many refs as checkouts at most.
        self.assertEqual(cached(), [offload_host.CACHE + fifth])

    def test_a_failed_push_negotiation_is_said_and_recorded_and_the_push_goes_on(self):
        negotiate = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "push.negotiate", "GIT_CONFIG_VALUE_0": "true"}
        failed = "warning: push negotiation failed; proceeding anyway with push"
        # Without protocol v2 at the second Mac, Git says that negotiation failed, and pushes all the same.
        with self.relaying(FAKE_DROP_PROTOCOL="1", **negotiate):
            passed, receipt, error = self.dispatched()
        self.assertIn(f"remote: git push said: {failed}", error)
        for sent in receipt["preparation"]["sends"]:
            self.assertEqual((sent["status"], sent["pushNegotiate"], sent["negotiationFailed"]), (0, "true", True))
            self.assertIn(failed, sent["said"])
        self.assertEqual(git(self.checkout(0), "rev-parse", "HEAD"), self.commit)
        # The positive control: with protocol v2 there, it negotiates, and a push says nothing beyond its progress.
        (self.project / "README.md").write_text("next\n")
        commit = commit_all(self.project)
        with self.relaying(**negotiate):
            passed, receipt, error = self.dispatched()
        self.assertNotIn("remote: git push said:", error)
        for sent in receipt["preparation"]["sends"]:
            self.assertEqual((sent["status"], sent["negotiationFailed"], sent["said"]), (0, False, []))
        self.assertEqual(git(self.checkout(0), "rev-parse", "HEAD"), commit)

    def throttled(self):
        """A commit with LARGE bytes that the relay takes about 26 s to pass, and the file that names the
        relay's PID and receive-pack's once it has started."""
        (self.project / "large.bin").write_bytes(os.urandom(LARGE))
        return commit_all(self.project), Path(self.temp.name) / "throttled"

    def ended(self, marker):
        """Wait, up to 30 s, for the processes the throttled relay's marker names to end."""
        for pid in [int(word) for word in marker.read_text().split()]:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                try: os.kill(pid, 0)
                except ProcessLookupError: break
                time.sleep(0.05)
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    def nothing_received(self, commit):
        """The second Mac's repository holds nothing of a push that did not finish: not its commit, any object or
        its ref. receive-pack, which the closed connection ends with SIGPIPE, can leave its quarantine directory
        outside the repository's objects, which Git's prune removes."""
        self.assertNotEqual(subprocess.run(["git", "--git-dir", str(self.bare()), "cat-file", "-e", commit],
                                           capture_output=True).returncode, 0)
        counted = dict(line.split(": ") for line in git(self.bare(), "count-objects", "-v").splitlines())
        self.assertEqual((counted["count"], counted["in-pack"]), ("0", "0"))
        self.assertEqual([path.name for path in (self.bare() / "objects").iterdir() if path.name not in ("info", "pack")
                          and not path.name.startswith("tmp_objdir-incoming-")], [])
        self.assertEqual(git(self.bare(), "for-each-ref", remote.REF), "")
        self.assertFalse(self.checkout(0).exists())

    def failure_record(self, lane):
        records = list((self.project / ".build/engineering/remote" / remote.DISPATCHES / lane).glob("*/source.json"))
        self.assertEqual(len(records), 1, records)
        record = json.loads(records[0].read_text())
        self.assertEqual((record["stage"], record["outcome"]), ("preparation", "failed-or-interrupted"))
        self.assertNotIn("result", record)
        return record

    @patch.object(remote, "SEND_TIMEOUT", 2)
    def test_a_push_that_outlasts_its_bound_is_stopped_and_leaves_its_record_and_nothing_there(self):
        commit, marker = self.throttled()
        with self.relaying(FAKE_THROTTLE=str(32 << 10), FAKE_THROTTLED=str(marker)):
            started = time.monotonic()
            status, output, error = self.run_here("gate")
            self.assertLess(time.monotonic() - started, 20)
        self.assertEqual(status, remote.MISCONFIGURED, output + error)
        self.assertIn(f"eng: gate not run: the push of {commit[:12]} into ~/{remote.ROOT}/repositories/example.git on "
                      "fake did not finish in 2s and was stopped, so nothing ran there: ", error)
        self.ended(marker)
        self.nothing_received(commit)
        # Nothing ran here either, and the latest results hold no run of it.
        for place in (".build/engineering/gate", ".build/engineering/remote/gate"):
            self.assertFalse((self.project / place).exists())
        sent = self.failure_record("gate")["preparation"]["sends"][0]
        self.assertTrue(sent["timedOut"])
        self.assertNotEqual(sent["status"], 0)
        self.assertGreaterEqual(sent["seconds"], 2)
        self.assertIn("nothing ran on fake; the record of what was sent is ", error)
        self.assertEqual(self.eng("remote", "passed", "gate")[0], 1)
        # The positive control: at full speed the same commit goes, cold, and runs.
        with self.relaying():
            status, output, error = self.run_here("gate")
        self.assertEqual(status, 0, output + error)
        self.assertGreaterEqual(self.passed()[0], LARGE)
        receipt = json.loads((self.project / ".build/engineering/remote/gate/source.json").read_text())
        self.assertEqual(receipt["preparation"]["sends"][0]["transfer"], "cold")
        self.assertEqual(self.eng("remote", "passed", "gate")[0], 0)

    def test_a_stop_during_a_push_ends_it_at_once_and_runs_nothing(self):
        commit, marker = self.throttled()
        with self.relaying(FAKE_THROTTLE=str(32 << 10), FAKE_THROTTLED=str(marker)):
            dispatcher = subprocess.Popen([sys.executable, str(self.toolkit / "bin/eng"), "--project", str(self.project),
                                           "remote", "run", "check"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            try:
                deadline = time.monotonic() + 60
                while not marker.exists() and dispatcher.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(marker.exists(), dispatcher.stdout.read().decode() if dispatcher.poll() is not None else "")
                time.sleep(0.5)
                stopped = time.monotonic()
                dispatcher.send_signal(signal.SIGTERM)
                output = dispatcher.communicate(timeout=60)[0].decode()
            finally:
                if dispatcher.poll() is None:
                    dispatcher.kill()
                    dispatcher.communicate()
        # The push alone would take about 26 s more.
        self.assertLess(time.monotonic() - stopped, 10, output)
        self.assertEqual(dispatcher.returncode, -signal.SIGTERM, output)
        self.ended(marker)
        self.nothing_received(commit)
        record = self.failure_record("check")
        self.assertIn(f"Stopped by SIGTERM while sending {commit[:12]} to fake", record["error"])
        self.assertTrue(record["preparation"]["sends"][0]["stopped"])
        self.assertFalse((self.project / ".build/engineering/remote/check").exists())

    def test_concurrent_dispatches_of_one_commit_each_keep_their_own_results(self):
        # Two `eng remote admit` dispatches at once send one commit and file their results under one name,
        # the second held as the code before vocaro-engineering#67 could be (BARRIER_DRIVER): first to a
        # second Mac that has no repository yet, then with a new commit to one that has.
        base, barrier = self.project / ".build/engineering/remote", Path(self.temp.name) / "barrier"
        commit, filed = self.commit, set()
        for state in ("fresh", "warm"):
            with self.subTest(state):
                if state == "warm":
                    (self.project / "README.md").write_text("next\n")
                    commit = commit_all(self.project)
                shutil.rmtree(barrier, ignore_errors=True)
                barrier.mkdir()
                dispatchers = {}
                with patch.dict(os.environ, {"FAKE_BARRIER": str(barrier)}):
                    for label in ("first", "second"):
                        dispatchers[label] = subprocess.Popen(
                            [sys.executable, "-c", BARRIER_DRIVER, str(self.toolkit / "src/python"), "--project",
                             str(self.project), "remote", "admit", "--", sys.executable, "-c", f"print('ran {label}')"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                    outputs = {label: dispatcher.communicate(timeout=300)[0].decode()
                               for label, dispatcher in dispatchers.items()}
                for label, dispatcher in dispatchers.items():
                    self.assertEqual(dispatcher.returncode, 0, outputs[label])
                own = sorted(set((base / remote.DISPATCHES / "admit").iterdir()) - filed)
                filed |= set(own)
                self.assertEqual(len(own), 2)
                receipts = {}
                for directory in own:
                    receipt = json.loads((directory / "source.json").read_text())
                    label = receipt["work"][-1].split("'")[1].split()[1]
                    receipts[label] = receipt
                    self.assertEqual((directory / "output.log").read_text().splitlines()[-1], f"ran {label}")
                    self.assertEqual((receipt["commit"], receipt["result"]["exitStatus"]), (commit, 0))
                    # Each push succeeded, or lost the race for the commit's ref to the other's.
                    for sent in receipt["preparation"]["sends"]:
                        self.assertTrue(sent["status"] == 0 or "race" in sent, sent)
                    self.assertIn(f"this dispatch's alone in {directory.resolve()}", outputs[label])
                self.assertEqual(sorted(receipts), ["first", "second"])
                # The latest is one of them, whole, and nothing staged is left.
                self.assertIn(json.loads((base / "admit/source.json").read_text()), list(receipts.values()))
                self.assertEqual(sorted(path.name for path in base.iterdir()),
                                 sorted([remote.DISPATCHES, ".admit.lock", "admit"]))
                self.assertEqual({git(self.checkout(slot), "rev-parse", "HEAD") for slot in (0, 1)}, {commit})


class DispatchResultsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name) / "remote"

    def staged(self, label):
        staging = self.base / f".admit.{label}"
        staging.mkdir(parents=True)
        (staging / "output.log").write_text(f"ran {label}\n")
        return staging

    def test_dispatches_of_one_name_file_their_results_in_turn_and_each_keeps_its_own(self):
        inside, release, copying = threading.Event(), threading.Event(), shutil.copytree

        def copy(source, destination, **options):
            # The first dispatch to copy its results holds there, inside the lock, until released.
            if not inside.is_set():
                inside.set()
                release.wait(30)
            return copying(source, destination, **options)
        filed = {}
        filing = lambda label: filed.__setitem__(label, remote.file_results(
            self.base, "admit", f"20261029T120000Z-{label}", self.staged(label), {"run": label}))
        with patch.object(remote.shutil, "copytree", side_effect=copy):
            first = threading.Thread(target=filing, args=("first",))
            first.start()
            self.assertTrue(inside.wait(30))
            second = threading.Thread(target=filing, args=("second",))
            second.start()
            time.sleep(0.5)
            # The second waits for the first, whose results are not yet the latest.
            self.assertTrue(second.is_alive())
            self.assertNotIn("second", filed)
            self.assertFalse((self.base / "admit").exists())
            release.set()
            for thread in (first, second): thread.join(30)
        for label in ("first", "second"):
            self.assertEqual((filed[label] / "output.log").read_text(), f"ran {label}\n")
            self.assertEqual(json.loads((filed[label] / "source.json").read_text()), {"run": label})
        self.assertEqual((self.base / "admit/output.log").read_text(), "ran second\n")
        self.assertEqual(sorted(path.name for path in self.base.iterdir()),
                         sorted([remote.DISPATCHES, ".admit.lock", "admit"]))
        # A record of a dispatch that ran nothing leaves the latest as it was.
        record = remote.file_results(self.base, "admit", "20261029T120001Z-third", receipt={"run": "third"}, latest=False)
        self.assertEqual(json.loads((record / "source.json").read_text()), {"run": "third"})
        self.assertEqual((self.base / "admit/output.log").read_text(), "ran second\n")

    def test_each_name_keeps_its_newest_dispatches_and_names_each_one_removed(self):
        with contextlib.redirect_stderr(StringIO()) as error:
            for number in range(remote.DISPATCHES_KEPT + 2):
                remote.file_results(self.base, "admit", f"20261029T1200{number:02d}Z-1", self.staged(str(number)))
        kept = sorted(path.name for path in (self.base / remote.DISPATCHES / "admit").iterdir())
        self.assertEqual(kept, [f"20261029T1200{number:02d}Z-1" for number in range(2, remote.DISPATCHES_KEPT + 2)])
        for number in (0, 1):
            self.assertIn(f"remote: removed {self.base / remote.DISPATCHES / 'admit' / f'20261029T1200{number:02d}Z-1'}",
                          error.getvalue())
        self.assertEqual((self.base / "admit/output.log").read_text(), f"ran {remote.DISPATCHES_KEPT + 1}\n")
        # Two dispatches in one second of one process each keep their own.
        with contextlib.redirect_stderr(StringIO()):
            first = remote.file_results(self.base, "admit", "20261029T130000Z-1", self.staged("a"))
            second = remote.file_results(self.base, "admit", "20261029T130000Z-1", self.staged("b"))
        self.assertNotEqual(first, second)
        self.assertEqual((second / "output.log").read_text(), "ran b\n")


class PushRecordTests(unittest.TestCase):
    def test_a_push_record_reads_each_phase_and_what_git_reports(self):
        lines = [(0.035, "Enumerating objects: 3, done."), (0.035, "Counting objects: 100% (3/3), done."),
                 (0.036, "Writing objects:  33% (1/3)"),
                 (0.470, "Writing objects: 100% (3/3), 24.01 MiB | 55.24 MiB/s, done."),
                 (0.470, "Total 3 (delta 0), reused 0 (delta 0), pack-reused 0 (from 0)"),
                 (0.954, "To fake:Development/build-host/repositories/source.git"),
                 (0.954, " * [new reference]   19ddd2a5 -> refs/vocaro/build-host/19ddd2a5")]
        found = remote.pushed(lines)
        self.assertEqual(found["phases"], {"connect": 0.035, "pack": 0.001, "transfer": 0.434, "receiver": 0.484})
        self.assertEqual((found["objects"], found["reused"], found["written"], found["bytes"]),
                         (3, 0, "24.01 MiB", round(24.01 * (1 << 20))))
        self.assertEqual((found["said"], found["negotiationFailed"]), ([], False))
        # A push that found the commit there writes no pack; one whose negotiation failed says so.
        found = remote.pushed([(0.026, "Everything up-to-date")])
        self.assertEqual((found["phases"], found["said"]), ({}, ["Everything up-to-date"]))
        self.assertNotIn("bytes", found)
        said = ["warning: --negotiate-only requires protocol v2", "fatal: the remote end hung up unexpectedly",
                "warning: push negotiation failed; proceeding anyway with push"]
        found = remote.pushed([(0.01 * index, line) for index, line in enumerate(said)]
                              + [(0.05, "Writing objects: 100% (6/6), 282 bytes | 282.00 KiB/s, done."),
                                 (0.05, "Total 6 (delta 0), reused 0 (delta 0)")])
        self.assertEqual((found["said"], found["negotiationFailed"], found["bytes"]), (said, True, 282))
        # The phases stop at the first mark the lines lack: a push cut off while it wrote.
        found = remote.pushed([(0.03, "Enumerating objects: 3, done."), (0.04, "Writing objects:  33% (1/3)")])
        self.assertEqual(found["phases"], {"connect": 0.03, "pack": 0.01})


if __name__ == "__main__":
    unittest.main()


# The second Mac's Homebrew, in a prefix of its own: it records what it installs, links a python3
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
# ssh to a second Mac whose PATH holds only the system's tools, as a fresh Mac's does, so this
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
        self.home = base / "second-mac-home"
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

    def test_a_second_mac_without_homebrew_is_left_unchanged_and_the_owner_told_to_install_it(self):
        status, output, error = self.eng("numpy")
        self.assertEqual(status, 1)
        self.assertIn("Homebrew is not installed on the second Mac", output)
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

    def test_a_different_xcode_on_the_second_mac_is_named(self):
        self.install_homebrew()
        # This Mac's Xcode is one version, the second Mac's at the same path another.
        with patch.object(remote, "developer_directory", return_value="/Applications/Xcode.app/Contents/Developer"), \
                patch.object(offload_host, "output", return_value="Xcode 99.0\nBuild version 99A1"):
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
# The second Mac is the other Mac.
DOCTOR_SSH = FAKE_SSH.replace('HOME="$FAKE_HOME"', 'FAKE_MAC=there HOME="$FAKE_HOME"')
RELEASE = "Xcode 27.1\nBuild version 27A9275"
BETA = "Xcode 27.2\nBuild version 27B5028f"
RUNTIMES = [("iOS 27.1", "24B5070a"), ("macOS 27.1", "26B5091g")]


class SecondMacDoctorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        home = base / "second-mac-home"
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
        (base / "second-mac-tmp").mkdir()
        # Each Mac's device record; the second Mac's defers physical devices to this Mac, as a run makes it.
        records = durable_scratch()
        self.addCleanup(records.cleanup)
        self.record_here = devices.Record(Path(records.name) / "here" / "device-handoff.json")
        self.record_there = devices.Record(Path(records.name) / "there" / "device-handoff.json")
        self.record_there.defer(offload_host.mac_name())
        self.environment = patch.dict(os.environ, {
            "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}", "FAKE_HOME": str(home),
            "FAKE_SHELL": str(tools / "login-shell"), "FAKE_XCODES": str(self.xcodes), "FAKE_MAC": "here",
            "VOCARO_OFFLOAD_HOST": "fake", "FAKE_SLOTS": str(base / "second-mac-slots"),
            "FAKE_GATE": str(base / "second-mac-gate"), "FAKE_TMPDIR": str(base / "second-mac-tmp"),
            devices.OVERRIDE: str(self.record_here.path), "FAKE_DEVICE_RECORD": str(self.record_there.path)})
        self.environment.start()
        os.environ.pop("DEVELOPER_DIR", None)
        # Both Macs select the same Xcode 27.1, with the same runtimes.
        self.release = self.xcode("Xcode.app", here=(RELEASE, RUNTIMES), there=(RELEASE, RUNTIMES))
        self.select(here=self.release, there=self.release)
        # The fake second Mac is no address; ConnectionTests measure real ones.
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
        # Its section was buildHost until vocaro-engineering#58, and no script reads the report.
        self.assertNotIn("buildHost", report)
        return status, report["offload"]

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
        name = offload_host.mac_name()
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

    def test_an_unreachable_or_unconfigured_second_mac_is_named_with_what_was_not_checked(self):
        # As when this Mac is away from the second Mac's network: work then runs here, so it is no error.
        with patch.dict(os.environ, {"FAKE_UNREACHABLE": "1"}):
            status, found = self.doctor()
        self.assertEqual((status, found["errors"]), (0, []), found)
        self.assertEqual(len(found["unchecked"]), 1, found)
        self.assertIn("fake, which cannot be reached, so work runs on this Mac until it can: fake cannot be reached: "
                      "ssh: Could not resolve hostname fake", found["unchecked"][0])
        # This Mac's side is still reported.
        self.assertEqual(found["local"]["xcode"], RELEASE)
        # A second Mac that answers but refuses the login is misconfigured, and an error.
        with patch.dict(os.environ, {"FAKE_DENIED": "1"}):
            status, found = self.doctor()
        self.assertEqual(status, 1)
        self.assertEqual(len(found["errors"]), 1, found)
        self.assertIn("ssh reached fake but could not log in: someone@fake: Permission denied", found["errors"][0])
        self.assertIn("fake's default Xcode, its Xcode at this Mac's developer directory and its simulator runtimes",
                      found["unchecked"][0])
        with patch.dict(os.environ, {"VOCARO_OFFLOAD_HOST": "none"}):
            status, found = self.doctor()
        self.assertEqual((status, found["host"], found["errors"]), (0, None, []), found)
        self.assertIn("no second Mac is configured", found["unchecked"][0])

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
    getaddrinfo answering for the second Mac's name."""

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
    """offload_host.leftovers over fake ps and lsof listings of a second Mac whose home is a temporary
    directory, with its own admission guard and log."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name).resolve()
        self.home = base / "home"
        self.checkout = self.home / remote.ROOT / "checkouts/Example/0/Example"
        self.checkout.mkdir(parents=True)
        self.slots = base / "slots"
        self.slots.mkdir()
        # As the second Mac's TMPDIR names it; lsof names the same directory under /private.
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
        found = offload_host.leftovers(remote.ROOT, listed=self.listed, written=self.writes.get)
        self.assertEqual(found["unchecked"], [])
        return {row["pid"]: row for row in found["leftovers"]}

    def owner(self, pid, start):
        self.guard.write_text(json.dumps({"vocaroEngineering": 1, "owners": {"a" * 32: {
            "pid": pid, "start": start, "label": "swift build", "exclusive": False, "model": False}}}))

    def admission(self, pid, start, *events):
        """The admission log's lines for pid's admission, each event that many seconds after start."""
        lines = [{"time": datetime.fromtimestamp(offload_host.started(start) + after, timezone.utc).isoformat(timespec="seconds"),
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
        """An ios-tests lane's run in the checkout, admitted at LATER, as the second Mac runs one: eng
        simulator-tests names its devices by their kind, and CoreSimulator starts the reserved member's
        launchd_sim under launchd, with the simulator's processes below it."""
        self.process(5000, 900, f"/usr/bin/python3 {self.checkout.parent}/vocaro-engineering/bin/eng --project "
                     f"{self.checkout} run ios-tests", cwd=str(self.checkout), start=LATER)
        self.process(5010, 5000, "/usr/bin/python3 scripts/eng simulator-tests --scheme Example --runtime "
                     "com.apple.CoreSimulator.SimRuntime.iOS-27-0 --device iPhone-17-Pro", cwd=str(self.checkout), start=LATER)
        devices = self.home / offload_host.DEVICES
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
        # Negative control: under a live run of offload_host.py, which its checkout's slot lock records.
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
            began = offload_host.started("Wed Oct 7 09:05:03 2026")
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
        devices = self.home / offload_host.DEVICES
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
        self.assertEqual(offload_host.leftovers(remote.ROOT, listed=lambda command: None), {"error": "ps listed no processes"})
        # Without lsof's working directories, only command lines place a process.
        self.process(4242, 1, "python3 -m unittest", cwd=str(self.checkout))
        found = offload_host.leftovers(remote.ROOT, listed=lambda command: self.listed(command) if command[0] == "ps" else None,
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
        found = offload_host.leftovers(remote.ROOT, listed=self.listed, written=self.writes.get)
        self.assertEqual([row["pid"] for row in found["leftovers"]], [7000])
        self.assertEqual(len(found["unchecked"]), 1, found)
        self.assertIn(f"the device record {self.record}, which could not be read", found["unchecked"][0])
        with patch.object(offload_host, "leftovers", side_effect=OSError("broken")):
            self.assertEqual(offload_host.answer({"leftovers": remote.ROOT}), {"error": "OSError: broken"})

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
                found[size] = offload_host.disk_writes(child.pid)
            finally:
                child.kill()
                child.communicate()
        self.assertGreaterEqual(found[4 << 20], 4 << 20)
        # Negative control: a process that wrote nothing has written less than that.
        self.assertLess(found[0], 4 << 20)
        # Another account's process, here launchd's, cannot be read without sudo.
        self.assertIsNone(offload_host.disk_writes(1))


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
# ssh to the second Mac, which is the other Mac. FAKE_CUT drops the connection once that many bytes
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
# What each Mac held on 2026-10-26: this Mac four runtimes the second Mac lacked, and the second Mac
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
        # The second Mac's home and this Mac's, each with the staging folder a copy to it uses.
        self.home, self.local = base / "second-mac-home", base / "home"
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
            "FAKE_DEVELOPER": str(self.developer), "VOCARO_OFFLOAD_HOST": "fake"})
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
        # Added from the second Mac's staging folder with --move, which leaves no second copy there.
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
        with patch.object(offload_host, "CHUNK", 1 << 16), patch.object(remote, "reporter", stopping):
            for direction, here, there in [("to the second Mac", [("iOS 27.1", "24A94401")], []),
                                           ("from the second Mac", [], [("iOS 27.0", "24A5423a")])]:
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
        self.assertEqual(offload_host.image_file({"path": str(image), "sizeBytes": size}), (str(image), None))
        self.assertEqual(offload_host.image_file({"path": str(image.parents[1]), "sizeBytes": size}), (str(image), None))
        for record in [{"path": str(image.parents[1]), "sizeBytes": size + 1}, {"path": str(image), "sizeBytes": 1},
                       {"path": str(image.parent / "missing.dmg"), "sizeBytes": size}, {"sizeBytes": size}]:
            file, why = offload_host.image_file(record)
            self.assertIsNone(file, record)
            self.assertTrue(why, record)


class RenamedSecondMacTests(unittest.TestCase):
    """A second Mac that macOS renamed is found again by the key known_hosts holds for its old name."""

    BROWSED = ["Browsing for _ssh._tcp.local.\n",
               "Timestamp     A/R    Flags  if Domain               Service Type         Instance Name\n",
               "11:02:14.905  Add        3  30 local.               _ssh._tcp.           Media’s MacBook Pro\n",
               "11:02:14.906  Add        3  14 local.               _ssh._tcp.           Media’s MacBook Pro\n",
               "11:02:14.907  Add        2  30 local.               _ssh._tcp.           Studio\n"]

    def setUp(self):
        remote.RENAMED.clear()
        self.addCleanup(remote.RENAMED.clear)
        self.settings = {"hostname": "mac-3.local", "port": "22"}
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(remote, "ssh_settings", side_effect=lambda host: dict(self.settings)))
        stack.enter_context(patch.object(offload_host, "mac_name", return_value="Here"))
        self.error = stack.enter_context(patch("sys.stderr", new_callable=StringIO))

    def test_advertised_resolves_each_browsed_mac_once(self):
        def watch(command, seconds, until=None):
            if command[1] == "-B": return self.BROWSED
            name = {"Media’s MacBook Pro": "Mac-4", "Studio": "Here"}[command[2]]
            return [f"11:02:22.628  x._ssh._tcp.local. can be reached at {name}.local.:22 (interface 30)\n"]
        with patch.object(remote, "watch", side_effect=watch) as watched:
            self.assertEqual(ADVERTISED(), [("mac-4.local", "22"), ("here.local", "22")])
        # One instance seen on two interfaces is resolved once.
        self.assertEqual([call.args[0][2] for call in watched.call_args_list[1:]], ["Media’s MacBook Pro", "Studio"])
        with patch.object(remote, "watch", return_value=[]):
            self.assertEqual(ADVERTISED(), [])

    def test_watch_stops_the_command_at_its_line_or_its_time(self):
        started = time.monotonic()
        found = remote.watch([sys.executable, "-c", "import time\nprint('one', flush=True)\nprint('two', flush=True)\n"
                                                    "time.sleep(60)"], 10, lambda line: line.startswith("two"))
        self.assertEqual(found, ["one\n", "two\n"])
        self.assertEqual(remote.watch([sys.executable, "-c", "import time; time.sleep(60)"], 0.5), [])
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(remote.watch(["/nonexistent/dns-sd"], 1), [])

    def test_the_one_mac_presenting_the_old_key_is_the_second_mac_under_its_new_name(self):
        names = [("here.local", "22"), ("mac-3.local", "22"), ("other.local", "22"), ("mac-4.local", "22"),
                 ("mac-5.local", "2222")]
        with patch.object(remote, "advertised", return_value=names), \
                patch.object(remote, "presents", side_effect=lambda host, name, alias: name == "mac-4.local") as asked:
            self.assertEqual(remote.rediscover("fake"), "mac-4.local")
        # Neither this Mac, the old name nor another port is asked, and each is asked for the old name's key.
        self.assertEqual([call.args for call in asked.call_args_list],
                         [("fake", "other.local", "mac-3.local"), ("fake", "mac-4.local", "mac-3.local")])
        self.assertEqual(remote.moved("fake"), ["-o", "HostName=mac-4.local", "-o", "HostKeyAlias=mac-3.local"])
        self.assertEqual(remote.moved("another"), [])
        self.assertIn("fake was renamed: mac-3.local no longer reaches it, and mac-4.local", self.error.getvalue())
        self.assertIn("set `HostName mac-4.local` under `Host fake`", self.error.getvalue())
        # Every connection of this process then goes there: ssh, git's ssh and the measurement.
        with patch.object(remote.subprocess, "run") as run:
            remote.ssh("fake", "true")
        self.assertEqual(run.call_args.args[0][len(remote.SSH):len(remote.SSH) + 5],
                         ["-o", "HostName=mac-4.local", "-o", "HostKeyAlias=mac-3.local", "fake"])
        with patch.object(remote.socket, "getaddrinfo", side_effect=OSError("no")):
            self.assertEqual(remote.connection("fake")["hostname"], "mac-4.local")

    def test_a_key_set_by_host_key_alias_is_the_one_asked_for(self):
        self.settings["hostkeyalias"] = "build-host"
        with patch.object(remote, "advertised", return_value=[("mac-4.local", "22")]), \
                patch.object(remote, "presents", return_value=True) as asked:
            self.assertEqual(remote.rediscover("fake"), "mac-4.local")
        self.assertEqual(asked.call_args.args, ("fake", "mac-4.local", "build-host"))

    def test_none_or_several_macs_presenting_the_key_leave_the_name_as_it_is(self):
        names = [("a.local", "22"), ("b.local", "22")]
        for presenting, said in [(set(), ""), ({"a.local", "b.local"}, "a.local, b.local all present its key")]:
            with self.subTest(presenting=presenting), patch.object(remote, "advertised", return_value=names), \
                    patch.object(remote, "presents", side_effect=lambda host, name, alias: name in presenting):
                self.assertIsNone(remote.rediscover("fake"))
                self.assertEqual(remote.RENAMED, {})
                self.assertIn(said, self.error.getvalue())
        # A name that is not multicast DNS is never searched for.
        self.settings["hostname"] = "mac.example.com"
        with patch.object(remote, "advertised") as browsed:
            self.assertIsNone(remote.rediscover("fake"))
        browsed.assert_not_called()

    def test_presents_asks_ssh_to_refuse_any_other_key_and_add_none(self):
        with patch.object(remote.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            self.assertTrue(remote.presents("fake", "mac-4.local", "mac-3.local"))
        command = run.call_args.args[0]
        for option in ["HostName=mac-4.local", "HostKeyAlias=mac-3.local", "StrictHostKeyChecking=yes",
                       "UpdateHostKeys=no", "CheckHostIP=no", f"ConnectTimeout={remote.REACH_TIMEOUT}"]:
            self.assertIn(option, command)
        # ssh takes the first value of an option, so the short timeout comes before SSH's own.
        self.assertLess(command.index(f"ConnectTimeout={remote.REACH_TIMEOUT}"),
                        command.index(f"ConnectTimeout={remote.CONNECT_TIMEOUT}"))
        self.assertEqual(command[-2:], ["fake", "true"])
        for outcome in [subprocess.CompletedProcess([], 255), subprocess.TimeoutExpired(["ssh"], 1), OSError("no")]:
            with self.subTest(outcome=outcome), patch.object(remote.subprocess, "run", side_effect=[outcome]):
                self.assertFalse(remote.presents("fake", "mac-4.local", "mac-3.local"))

    def test_the_check_before_a_dispatch_finds_a_renamed_second_mac_and_only_on_a_failure_a_rename_explains(self):
        away = remote.Unreachable("fake cannot be reached: ssh: Could not resolve hostname mac-3.local", "wake it")
        other_key = remote.Misconfigured("ssh reached fake but could not log in: Host key verification failed.", "fix")
        refused = remote.Misconfigured("ssh reached fake but could not log in: Permission denied (publickey).", "fix")
        cases = [([away, None], "mac-4.local", None), ([other_key, None], "mac-4.local", None),
                 ([away], None, remote.Unreachable), ([other_key], None, remote.Misconfigured),
                 # A refused login and a working name are never taken for a rename.
                 ([refused], "unused", remote.Misconfigured), ([None], "unused", None)]
        for outcomes, found, kind in cases:
            with self.subTest(outcomes=outcomes, found=found), \
                    patch.object(remote, "check", side_effect=outcomes) as checked, \
                    patch.object(remote, "rediscover", return_value=found) as searched:
                if kind is None:
                    remote.reachable("fake")
                else:
                    with self.assertRaises(kind) as raised:
                        remote.reachable("fake")
                    self.assertIs(raised.exception, outcomes[0])
                self.assertEqual(checked.call_count, len(outcomes) if found != "unused" or kind is None else 1)
                self.assertEqual(searched.called, outcomes[0] in (away, other_key))


class SharedOriginTests(unittest.TestCase):
    """The second Mac keeps the toolkit's shared origin when a plan names a path (vocaro-engineering#78)."""

    def test_a_local_url_is_told_from_a_network_one(self):
        for url in ["/Users/someone/Development/vocaro-engineering", "../vocaro-engineering", "vocaro-engineering",
                    "file:///Users/someone/Development/vocaro-engineering", "FILE:///tmp/kit", "./a:b"]:
            self.assertTrue(offload_host.local_url(url), url)
        for url in [remote.UPSTREAM, "http://example.com/kit.git", "ssh://git@github.com/vocaro/vocaro-engineering.git",
                    "git://example.com/kit.git", "git@github.com:vocaro/vocaro-engineering.git", "build-host:kit.git"]:
            self.assertFalse(offload_host.local_url(url), url)

    def test_place_refuses_a_path_origin_and_leaves_the_shared_one(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "source"
            subprocess.run(["git", "init", "-q", "-b", "main", str(source)], check=True)
            (source / "README.md").write_text("kit\n")
            commit = commit_all(source)
            repository = base / "vocaro-engineering.git"
            subprocess.run(["git", "clone", "-q", "--bare", str(source), str(repository)], check=True)
            git(repository, "config", "remote.origin.url", remote.UPSTREAM)
            with self.assertRaises(offload_host.Refusal) as refused:
                offload_host.place(repository, base / "0/vocaro-engineering", commit, "refs/heads/main",
                                   "/Users/someone/Development/vocaro-engineering")
            self.assertIn("a path on the sending Mac", str(refused.exception))
            self.assertIn(remote.UPSTREAM, refused.exception.fix)
            self.assertEqual(git(repository, "config", "--get", "remote.origin.url"), remote.UPSTREAM)
            self.assertFalse((base / "0").exists())
            # The positive control: a URL is written, and the commit placed.
            fork = "https://example.com/fork/vocaro-engineering.git"
            offload_host.place(repository, base / "0/vocaro-engineering", commit, "refs/heads/main", fork)
            self.assertEqual(git(repository, "config", "--get", "remote.origin.url"), fork)
            self.assertEqual(git(base / "0/vocaro-engineering", "rev-parse", "HEAD"), commit)


class OfferAgainTests(unittest.TestCase):
    """While both Macs are busy, a second Mac that cannot be reached at one offer is asked at the next."""

    def offer(self):
        offered = remote.Offer("gate", "`eng run --here gate`", lambda host, sent: 0)
        offered.host, offered.busy = "fake", "every checkout is in use"
        return offered

    def test_a_second_mac_away_at_one_offer_is_asked_again_and_runs_the_work_once_back(self):
        away = remote.Unreachable("fake cannot be reached: ssh: Could not resolve hostname fake.local", "wake it")
        offered = self.offer()
        with patch.object(remote, "reachable", side_effect=[away, away, None]), \
                patch("sys.stderr", new_callable=StringIO) as error:
            self.assertIsNone(offered.again())
            self.assertIsNone(offered.again())
            self.assertEqual((offered.busy, offered.away, offered.status),
                             ("every checkout is in use", away.problem, away.problem))
            self.assertEqual(offered.again(), 0)
        self.assertEqual(error.getvalue().count("asks the second Mac again at the next offer"), 1, error.getvalue())
        self.assertEqual((offered.busy, offered.away), (None, None))

    def test_a_second_mac_that_can_no_longer_take_the_work_is_not_asked_again(self):
        # The negative control: what is no unreachable second Mac still ends the offers.
        offered = self.offer()
        with patch.object(remote, "reachable", side_effect=[remote.Unavailable("the lane names a physical device")]), \
                patch("sys.stderr", new_callable=StringIO) as error:
            self.assertIsNone(offered.again())
            self.assertIsNone(offered.again())
        self.assertIsNone(offered.busy)
        self.assertIn("waits for this Mac alone: the lane names a physical device", error.getvalue())

    def test_neither_mac_admitting_says_the_second_mac_was_away(self):
        offered = self.offer()
        offered.away = "fake cannot be reached: it is asleep"
        with patch("sys.stderr", new_callable=StringIO) as error:
            self.assertEqual(remote.overflow(offered, lambda seconds: None, once=True), remote.BUSY)
        self.assertIn("neither Mac admitted it now; the second Mac cannot be reached: fake cannot be reached: it is asleep",
                      error.getvalue())


class EarlierNameTests(unittest.TestCase):
    def test_the_source_names_each_old_name_only_where_it_refuses_it(self):
        # vocaro-engineering#58 removed these, and nothing reads one as it did: every line of the toolkit's
        # source that names one is part of its refusal. The names that stay, such as the alias build-host,
        # the directory Development/build-host and refs/vocaro/build-host/, are not among them.
        old = re.compile(r"buildHost|BUILD_HOST|OLD_HOST_VARIABLE|build_host|build-host-only|build-host-unchecked")
        named = {}
        for path in sorted((config.TOOLKIT_ROOT / "src/python").rglob("*.py")):
            for line in path.read_text().splitlines():
                if old.search(line): named.setdefault(path.name, []).append(line.strip())
        self.assertEqual(named, {
            "config.py": [
                '"""Refuse "buildHost", true or false, on any lane or group. It once sent a lane to the second Mac, and',
                'marked = [name for name, lane in lanes.items() if isinstance(lane, dict) and "buildHost" in lane]',
                'raise ValueError(f"engineering.json gives {named} \\"buildHost\\", which vocaro-engineering#58 '
                'removed: every heavy "'],
            "remote.py": [
                'HOST_VARIABLE, OLD_HOST_VARIABLE = "VOCARO_OFFLOAD_HOST", "VOCARO_BUILD_HOST"',
                "VOCARO_BUILD_HOST, its name until vocaro-engineering#58, is refused when it is set at all, beside the",
                "if OLD_HOST_VARIABLE in os.environ:",
                'raise ValueError(f"{OLD_HOST_VARIABLE} was {HOST_VARIABLE}\'s name until vocaro-engineering#58, and '
                'is no "'],
            "cli.py": [
                'run.add_argument("--build-host-only", action="store_true", help=argparse.SUPPRESS)',
                "if args.build_host_only:",
                'raise ValueError("--build-host-only was --offload-only\'s name until vocaro-engineering#58, and is no "'],
            "devices.py": [
                'recover.add_argument("--build-host-unchecked", metavar="REASON", help=argparse.SUPPRESS)',
                "if args.build_host_unchecked is not None:",
                'raise ValueError("--build-host-unchecked was --offload-unchecked\'s name until '
                'vocaro-engineering#58, "']})
        # The negative control: the pattern finds reads such as those the refusals replaced.
        for read in ['if lane.get("buildHost"):', 'os.environ.get("VOCARO_BUILD_HOST")', "if args.build_host_only:",
                     "unchecked = args.build_host_unchecked"]:
            self.assertTrue(old.search(read), read)
