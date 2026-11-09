import contextlib
from functools import partial
from io import StringIO
import json
from pathlib import Path
import os
import signal
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import call, patch
SOURCE = Path(__file__).resolve().parents[1] / "src/python"
sys.path.insert(0, str(SOURCE))
from vocaro_engineering import processes, resources
import host_isolation
from host_isolation import stretched

# Starts each descendant in its own session, as Xcode starts DTServiceHub apart from xcodebuild's
# group, and exits 3 once each is ready and the supervisor has seen it under this root.
ROOT = """
import os, subprocess, sys, time
from pathlib import Path
here = Path(sys.argv[1])
children = [subprocess.Popen([sys.executable, "-c", code, str(here)], start_new_session=True).pid for code in sys.argv[2:]]
(here / "listing").write_text(" ".join(map(str, [os.getpid(), *children])))
(here / "listing").replace(here / "children")
while not all((here / name).exists() for name in ["adopted", *(f"ready-{pid}" for pid in children)]): time.sleep(0.01)
raise SystemExit(3)
"""
READY = "import os, signal, sys, time\nfrom pathlib import Path\nhere = Path(sys.argv[1])\n"
IGNORES_TERM = READY + "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n(here / f'ready-{os.getpid()}').touch()\ntime.sleep(120)"
HANDLES_TERM = READY + ("def stop(*_): (here / 'terminated').touch(); os._exit(0)\n"
                        "signal.signal(signal.SIGTERM, stop)\n(here / f'ready-{os.getpid()}').touch()\ntime.sleep(120)")
FINISHES = READY + ("parent = os.getppid()\n(here / f'ready-{os.getpid()}').touch()\n"
                    "while os.getppid() == parent: time.sleep(0.01)\n(here / 'finished').touch()")
# ROOT that starts /bin/cat with the command line of each stand-in. Its input stays open in the test
# process, so the stand-in runs until signaled and exits on EOF if the test ends before it is observed.
HELPERS_ROOT = ROOT.replace(
    'children = [subprocess.Popen([sys.executable, "-c", code, str(here)], start_new_session=True).pid for code in sys.argv[2:]]',
    'with (here / "stand-in-input").open("rb", buffering=0) as hold:\n'
    '    children = [subprocess.Popen([str(here / path)], executable="/bin/cat", stdin=hold,\n'
    '                                 start_new_session=True).pid for path in sys.argv[2:]]\n'
    'for child in children: (here / f"ready-{child}").touch()')
DTSERVICEHUB = "Xcode.app/Contents/SharedFrameworks/DVTInstrumentsFoundation.framework/Resources/DTServiceHub"
IBTOOLD = "Xcode.app/Contents/Developer/usr/bin/ibtoold"
XCODEBUILD = "Xcode.app/Contents/Developer/usr/bin/xcodebuild"
# ROOT that stays once its descendants are adopted, until a signal ends it.
STAYS = ROOT.replace("raise SystemExit(3)", "time.sleep(120)")
# Logs each SIGTERM and takes half a second to shut down after the first, as a test runner can.
COUNTS_TERM = READY + ("stopping = []\n"
                       "def stop(*_):\n    (here / 'terms').open('a').write('TERM\\n')\n    stopping.append(time.monotonic())\n"
                       "signal.signal(signal.SIGTERM, stop)\n"
                       "while not stopping or time.monotonic() - stopping[0] < 0.5: time.sleep(0.01)")
# Double forks a grandchild that leaves the root's process group but stays in its session, as a
# daemon can, and once its parent is reaped exits 5, or with "stays" waits for a stop. The
# grandchild runs until the test's directory goes, or with "ends" exits at once, which the root awaits.
ORPHANS = """
import os, sys, time
from pathlib import Path
here, how = Path(sys.argv[1]), sys.argv[2]
if os.fork() == 0:
    if os.fork() == 0:
        os.setpgid(0, 0)
        (here / "pid").write_text(str(os.getpid()))
        (here / "pid").replace(here / "orphan")
        began = time.monotonic()
        while how != "ends" and here.exists() and time.monotonic() - began < 120: time.sleep(0.05)
    os._exit(0)
os.wait()
while not (here / "orphan").exists(): time.sleep(0.01)
orphan = int((here / "orphan").read_text())
while how == "ends":
    try: os.getsid(orphan)
    except ProcessLookupError: break
    time.sleep(0.01)
(here / "orphaned").touch()
if how == "stays": time.sleep(120)
raise SystemExit(5)
"""
# An admission owner that never settles: it records itself in the guard, as eng admit does, and
# ignores SIGTERM.
STUCK_OWNER = READY + """import json, subprocess
signal.signal(signal.SIGTERM, signal.SIG_IGN)
start = subprocess.run(["ps", "-p", str(os.getpid()), "-o", "lstart="], capture_output=True, text=True).stdout.strip()
Path(os.environ["SW_GATE_LOCK_FILE"] + ".warm-push-guard").write_text(json.dumps(
    {"vocaroEngineering": 1, "owners": {"stuck": {"pid": os.getpid(), "start": start, "label": "stuck"}}}))
(here / f'ready-{os.getpid()}').touch()
time.sleep(120)
"""
# On SIGTERM starts a child, as an admission starts its ps samples; as "owner" it first records
# itself in the guard. Once the supervisor has sampled that child twice, it leaves the guard and ends.
SAMPLES = READY + """import json, subprocess
role = "ROLE"
guard = Path(os.environ["SW_GATE_LOCK_FILE"] + ".warm-push-guard")
def stop(*_):
    sample = subprocess.Popen([sys.executable, "-c", "import sys, time\\nfrom pathlib import Path\\n"
                               "while Path(sys.argv[1]).exists(): time.sleep(0.05)", str(here)])
    (here / f"sample-{role}.tmp").write_text(str(sample.pid))
    (here / f"sample-{role}.tmp").replace(here / f"sample-{role}")
signal.signal(signal.SIGTERM, stop)
if role == "owner":
    start = subprocess.run(["ps", "-p", str(os.getpid()), "-o", "lstart="], capture_output=True, text=True).stdout.strip()
    guard.write_text(json.dumps({"vocaroEngineering": 1, "owners": {"owner": {"pid": os.getpid(), "start": start}}}))
(here / f'ready-{os.getpid()}').touch()
while not (here / f"sampled-{role}").exists(): time.sleep(0.01)
if role == "owner": guard.unlink(missing_ok=True)
"""
# A lane's script that runs a nested admission, outlives a stop and records how the admission ended.
LANE = """
import signal, subprocess, sys
from pathlib import Path
signal.signal(signal.SIGTERM, lambda *_: None)
status = subprocess.call(sys.argv[2:])
Path(sys.argv[1], "status").write_text(str(status))
raise SystemExit(status)
"""
# A nested admission's command that takes longer than the outer supervisor's KILL_AFTER to end
# after SIGTERM, as DTServiceHub or a simulator's test runner can: it ends once that supervisor
# reports SIGKILL waiting, the cue "due". With "leaves" it first leaves a descendant in its
# session, as a daemon started between two samples does, which runs until the test's directory goes.
SLOW_TO_END = """
import os, signal, sys, time
from pathlib import Path
here, how = Path(sys.argv[1]), sys.argv[2]
if how == "leaves":
    if os.fork() == 0:
        if os.fork() == 0:
            os.setpgid(0, 0)
            (here / "orphan.tmp").write_text(str(os.getpid()))
            (here / "orphan.tmp").replace(here / "orphan")
            began = time.monotonic()
            while here.exists() and time.monotonic() - began < 120: time.sleep(0.05)
        os._exit(0)
    os.wait()
    while not (here / "orphan").exists(): time.sleep(0.01)
signal.signal(signal.SIGTERM, lambda *_: (here / "terminated").touch())
(here / "command.tmp").write_text(str(os.getpid()))
(here / "command.tmp").replace(here / "command")
began = time.monotonic()
while not (here / "due").exists() and time.monotonic() - began < 120: time.sleep(0.01)
"""
# The resource CLI, sampling only once its command is ready, so that a descendant the command
# left first is never observed, as if it had been left between two samples.
UNOBSERVING = """
import sys, time
from pathlib import Path
from vocaro_engineering import processes, resources
here, real = Path(sys.argv[1]), processes.snapshot
def unobserved():
    while not (here / "command").exists(): time.sleep(0.01)
    return real()
processes.snapshot = unobserved
raise SystemExit(resources.main(sys.argv[2:]))
"""
# A stand-in's 120 s life, and the 120 s backstop that ends one a failed test left, stretched by load: each
# outlasts the stretched bound on the stall it stands in for, however slowly the host runs.
IGNORES_TERM, HANDLES_TERM, STAYS, ORPHANS, STUCK_OWNER, SLOW_TO_END = (
    script.replace("time.sleep(120)", f"time.sleep({stretched(120)})").replace("began < 120", f"began < {stretched(120)}")
    for script in (IGNORES_TERM, HANDLES_TERM, STAYS, ORPHANS, STUCK_OWNER, SLOW_TO_END))


class ProcessTests(unittest.TestCase):
    def hold_stand_ins(self, directory):
        fifo = Path(directory) / "stand-in-input"
        os.mkfifo(fifo)
        return fifo.open("rb+", buffering=0)

    # supervise takes its kill_after when it is defined, so a test passes the grace it needs:
    # patching KILL_AFTER changes nothing, and with 10 s a loaded host failed these tests (#81).
    def outlive_the_root(self, directory, *descendants, root=ROOT, timeout=None, kill_after=processes.KILL_AFTER,
                         late=0, awaits=None, **limits):
        """Supervise ROOT with these descendants; return its result or timeout, stderr and their start identities.

        late delays the first sample after the root is reaped by that many seconds, as a busy host
        can. With awaits, each sample after the stop's report waits for that file in directory, so
        that a descendant's SIGTERM handler runs however long the host takes to schedule it."""
        real, births, cue = processes.snapshot, {}, {"at": time.monotonic()}
        # The grace a test gives is a wait the supervisor makes on purpose, without a cue, so a stall is bounded
        # beyond it: a grace stretched by load at a test's start once outlasted the stall bound read later (#128).
        grace = limits.get("LINGER", processes.LINGER)
        stall = stretched(30)
        # The deadline checked between samples also needs a bound on each real ps call.
        run = partial(processes.subprocess.run, timeout=stall)
        def observed():
            # Bound a stall here, from the latest cue: the start, the root's listing of its
            # descendants, their adoption. A timeout bounds only the root, so a lingering
            # descendant must not hold the run until the test is stopped.
            if time.monotonic() - cue["at"] > grace + stall:
                self.fail("supervision stalled for 30 s past its grace, stretched by load")
            if awaits is not None and "stopping" in error.getvalue():
                while not (Path(directory) / awaits).exists():
                    if time.monotonic() - cue["at"] > grace + stall:
                        self.fail(f"no {awaits} within 30 s, stretched by load, of the stop")
                    time.sleep(0.01)
            current = real()
            try: root, *children = map(int, (Path(directory) / "children").read_text().split())
            except FileNotFoundError: return current
            if late and births and root not in current and "late" not in cue:
                cue["late"] = True
                time.sleep(late)
            if "listed" not in cue: cue.update(listed=True, at=time.monotonic())
            if all(current.get(pid, (None,))[0] == root for pid in children):
                if not births: cue["at"] = time.monotonic()
                births.update({pid: current[pid][2] for pid in children})
                (Path(directory) / "adopted").touch()
            return current
        def cleanup():
            if not births: return
            with patch.object(processes.subprocess, "run", run):
                current = real()
            for pid, birth in births.items():
                if current.get(pid, (0, 0, None))[2] == birth:
                    with contextlib.suppress(ProcessLookupError): os.kill(pid, signal.SIGKILL)
        self.addCleanup(cleanup)
        with patch.object(processes, "snapshot", observed), contextlib.redirect_stderr(StringIO()) as error, \
             patch.object(processes.subprocess, "run", run), patch.dict(processes.__dict__, limits):
            try: result = processes.supervise([sys.executable, "-c", root, directory, *descendants], cwd=directory, timeout=timeout,
                                                kill_after=kill_after)
            except TimeoutError as expired: result = expired
            current = real()
        self.assertEqual({pid for pid, birth in births.items() if current.get(pid, (0, 0, None))[2] == birth}, set())
        return result, error.getvalue(), births

    def test_bounded_stops_its_whole_group_at_its_timeout_and_otherwise_keeps_the_status_and_output(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "child.pid"
            # A descendant that ignores SIGTERM, so that only the group's SIGKILL ends it.
            stubborn = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
            script = (f"import pathlib, subprocess, sys, time\nchild = subprocess.Popen([sys.executable, '-c', {stubborn!r}])\n"
                      f"pathlib.Path({str(marker)!r}).write_text(str(child.pid))\nprint('started', flush=True)\ntime.sleep(60)")
            began = time.monotonic()
            # Fixed: the 10 s timeout is the behavior under test, and the script's 60 s outlasts it; only the
            # ceiling on the stop and the wait for the descendant's end stretch.
            status, output = processes.bounded([sys.executable, "-c", script], timeout=10, kill_after=1)
            self.assertIsNone(status)
            self.assertLess(time.monotonic() - began, stretched(30))
            self.assertIn("started", output)
            child = int(marker.read_text())
            deadline = time.monotonic() + stretched(10)
            while time.monotonic() < deadline:
                try: os.kill(child, 0)
                except ProcessLookupError: break
                time.sleep(0.05)
            else:
                self.fail(f"descendant {child} outlived the group's SIGKILL")
        # The positive control: a command that ends in time keeps its status and output.
        self.assertEqual(processes.bounded([sys.executable, "-c", "print('done'); raise SystemExit(4)"],
                                           timeout=stretched(60)),
                         (4, "done\n"))

    def test_descendants_that_outlive_the_root_are_reported_then_stopped_keeping_its_status(self):
        with tempfile.TemporaryDirectory() as directory:
            # The first sample after the root ends comes past LINGER and kill_after together, as a
            # busy host's can; SIGTERM must still come first. The sample after it waits for the
            # handler to run, so that how soon the host schedules it does not matter (#90).
            # Fixed: the 0.2 s grace and 0.5 s kill_after are the behavior under test, and awaits holds the
            # samples after the stop for the handler however slowly the host runs.
            result, error, births = self.outlive_the_root(directory, IGNORES_TERM, HANDLES_TERM, LINGER=0.2, kill_after=0.5,
                                                          late=1, awaits="terminated")
            self.assertEqual(result, 3)
            # SIGTERM came first, so a process that handles it can end cleanly.
            self.assertTrue((Path(directory) / "terminated").exists())
            root = int((Path(directory) / "children").read_text().split()[0])
            self.assertIn("stopping 2 process(es)", error)
            for pid in births:
                # Each descendant calls setsid just after fork, so a sample in between first sees it
                # in the root's group: on a busy host it can.
                self.assertTrue(any(f"{pid} with parent {root}, group {group}" in error for group in (pid, root)), error)
            self.assertIn("PPID", error)

    def test_a_root_that_exits_in_time_keeps_its_status_when_its_timeout_expires_during_the_grace(self):
        with tempfile.TemporaryDirectory() as directory:
            # The root exits at once; its timeout expires before the grace stops the descendant. The
            # root starts two interpreters first, which on a busy host took more than 2 s. Both stretch by
            # load, keeping the timeout inside the grace.
            result, error, _births = self.outlive_the_root(directory, IGNORES_TERM, timeout=stretched(8),
                                                           LINGER=stretched(10), kill_after=0.5)
            self.assertEqual(result, 3)
            self.assertIn("stopping 1 process(es)", error)

    def test_xcodes_own_programs_left_to_launchd_are_stopped_without_the_grace(self):
        with tempfile.TemporaryDirectory() as directory, self.hold_stand_ins(directory):
            # Fixed: a grace of 600 s, which the run must not wait; the bound on it stretches by load.
            began = time.monotonic()
            result, error, births = self.outlive_the_root(directory, DTSERVICEHUB, IBTOOLD, root=HELPERS_ROOT,
                                                          LINGER=600, kill_after=0.5)
            self.assertLess(time.monotonic() - began, stretched(30))
            self.assertEqual(result, 3)
            self.assertIn("stopping 2 Xcode program(s) that launchd adopted when the command", error)
            self.assertIn("without waiting 600s, with SIGTERM, then SIGKILL after 0.5s", error)
            self.assertIn(str(Path(directory) / DTSERVICEHUB), error)
            self.assertEqual(len(births), 2)

    def test_anything_else_still_running_keeps_the_whole_grace_for_all(self):
        for name, descendants in (("beside a process of its own", (DTSERVICEHUB, "ordinary-program")),
                                  ("xcodebuild itself", (XCODEBUILD,)),
                                  ("a process of its own", ("ordinary-program",))):
            with self.subTest(name), tempfile.TemporaryDirectory() as directory, self.hold_stand_ins(directory):
                # Fixed: waiting out the grace is the behavior under test. Only the stall bound stretches.
                grace = 2
                began = time.monotonic()
                with patch.object(processes, "xcode_helper", wraps=processes.xcode_helper) as classify:
                    result, error, _births = self.outlive_the_root(directory, *descendants, root=HELPERS_ROOT,
                                                                   LINGER=grace, kill_after=0.5)
                self.assertGreaterEqual(time.monotonic() - began, grace)
                self.assertEqual(result, 3)
                self.assertIn(f"stopping {len(descendants)} process(es) still running {grace:g}s after the command",
                              error)
                self.assertNotIn("Xcode program", error)
                if XCODEBUILD in descendants:
                    self.assertIn(call(1, str(Path(directory) / XCODEBUILD)), classify.call_args_list)

    def test_an_xcode_helper_is_a_program_inside_an_xcode_that_launchd_adopted_other_than_xcodebuild(self):
        hub = "/Applications/Xcode.app/Contents/SharedFrameworks/DVTInstrumentsFoundation.framework/Resources/DTServiceHub"
        for parent, command, expected in (
                (1, hub, True),
                (1, "/Applications/Xcode.app/Contents/Developer/usr/bin/ibtoold --sending-client-environment", True),
                (1, "/Applications/Xcode-beta.app/Contents/Developer/usr/bin/ibtoold", True),
                (4242, hub, False),
                (1, "/Applications/Xcode.app/Contents/Developer/usr/bin/xcodebuild test-without-building", False),
                (1, "/Applications/Xcode.app/Contents/Developer/usr/bin/xcodebuild", False),
                (1, f"/usr/bin/python3 {hub}", False),
                (1, "/Applications/MyXcode.app/Contents/MacOS/tool", False),
                (1, "/Applications/Xcode.app.old/Contents/MacOS/tool", False),
                (1, None, False), (1, "", False)):
            with self.subTest(parent=parent, command=command):
                self.assertEqual(processes.xcode_helper(parent, command), expected)

    def test_a_descendant_that_ends_within_the_grace_is_awaited_and_never_signaled(self):
        with tempfile.TemporaryDirectory() as directory:
            # The grace stretches by load: the descendant must end within it.
            result, error, _births = self.outlive_the_root(directory, FINISHES, LINGER=stretched(30))
            self.assertEqual(result, 3)
            self.assertTrue((Path(directory) / "finished").exists())
            self.assertEqual(error, "")

    def test_nonzero_exit_status_and_output_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "run.log"
            result = processes.run([sys.executable, "-c", "print('observed'); raise SystemExit(7)"],
                                   cwd=directory, log_path=log)
            self.assertEqual(result, 7)
            self.assertEqual(log.read_text().strip(), "observed")

    def test_a_command_that_cannot_start_raises_not_started(self):
        with tempfile.TemporaryDirectory() as directory:
            unexecutable = Path(directory) / "script.sh"
            unexecutable.write_text("#!/bin/sh\nexit 0\n")
            cases = [([str(Path(directory) / "missing")], directory), ([str(unexecutable)], directory),
                     ([sys.executable, "-c", "pass"], str(Path(directory) / "absent"))]
            for command, cwd in cases:
                with self.subTest(command=command, cwd=cwd):
                    with self.assertRaises(processes.NotStarted) as raised:
                        processes.supervise(command, cwd=cwd)
                    self.assertIsInstance(raised.exception, OSError)

    def test_on_start_runs_before_the_command_and_the_command_is_otherwise_the_same(self):
        with tempfile.TemporaryDirectory() as directory:
            ran = Path(directory) / "ran"
            seen = []
            script = f"(/usr/bin/yes; echo $? >{directory}/pipe) | /usr/bin/head -n 1 >/dev/null; " \
                     f"/usr/bin/env >{directory}/env; /bin/ls /dev/fd >{directory}/fds; echo \"$0 $*\" >{ran}"
            command = ["sh", "-c", script, "zero", "one two"]
            environment = {"PATH": "/bin:/usr/bin", "VOCARO_TEST": "kept"}
            def run(**options):
                self.assertEqual(processes.supervise(command, cwd=directory, env=environment, **options), 0)
                return {name: (Path(directory) / name).read_text() for name in ("pipe", "env", "fds", "ran")}
            bare = run()
            ran.unlink()
            def on_start(pid):
                # Long enough for a command that was not held to have written its file. Fixed: a window in which
                # nothing may happen, which load can only make pass.
                time.sleep(1)
                seen.append((pid, ran.exists()))
            self.assertEqual(run(on_start=on_start), bare)
            [(pid, early)] = seen
            self.assertFalse(early)
            self.assertGreater(pid, 0)
            # sh keeps an ignored SIGPIPE from its children, so yes shows the disposition it got.
            self.assertEqual(bare["pipe"].strip(), str(128 + signal.SIGPIPE))
            self.assertEqual(bare["ran"].strip(), "zero one two")

    def test_a_command_that_cannot_start_after_on_start_is_not_started(self):
        with tempfile.TemporaryDirectory() as directory:
            unexecutable = Path(directory) / "script.sh"
            unexecutable.write_text("#!/bin/sh\nexit 0\n")
            for command in [str(Path(directory) / "missing")], [str(unexecutable)]:
                with self.subTest(command=command), self.assertRaises(processes.NotStarted) as raised:
                    processes.supervise(command, cwd=directory, on_start=lambda pid: None)
                self.assertIn("did not start", str(raised.exception))

    def test_an_on_start_that_raises_runs_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            ran = Path(directory) / "ran"
            def on_start(pid):
                # Long enough for a command that was not held to have written its file. Fixed: a window in which
                # nothing may happen, which load can only make pass.
                time.sleep(1)
                raise ValueError("refused")
            with self.assertRaisesRegex(ValueError, "refused"):
                processes.supervise([sys.executable, "-c", f"open({str(ran)!r}, 'w')"], cwd=directory, on_start=on_start)
            self.assertFalse(ran.exists())

    def test_a_timeout_is_not_a_successful_terminal_result(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(TimeoutError):
                # Fixed: the 0.1 s timeout is the behavior under test, and the 30 s sleep outlasts it.
                processes.run([sys.executable, "-c", "import time; time.sleep(30)"],
                              cwd=directory, log_path=Path(directory)/"run.log", timeout=0.1)

    def test_a_timeout_stops_descendants_with_sigterm_then_sigkill_and_raises_timed_out_once_they_end(self):
        with tempfile.TemporaryDirectory() as directory:
            # The root stays until the timeout ends it; one descendant handles SIGTERM, one ignores it. The timeout
            # stretches by load, so that the descendant has set its handler before it comes.
            limit = round(stretched(2))
            result, error, _births = self.outlive_the_root(directory, IGNORES_TERM, HANDLES_TERM, root=STAYS,
                                                           timeout=limit, kill_after=0.5)
            self.assertIsInstance(result, processes.TimedOut)
            self.assertTrue((Path(directory) / "terminated").exists())
            self.assertIn(f"timed out after {limit}s; sending SIGTERM", error)

    def test_a_cancel_ends_the_command_as_a_timeout_does_and_says_it_was_canceled(self):
        """The merge queue throws away a gate stacked on a batch that did not land (vocaro-engineering#241): a set
        cancel ends the command and raises Canceled, a TimedOut; one never set leaves the command's status."""
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stderr(StringIO()) as said:
            cancel = threading.Event()
            threading.Timer(0.2, cancel.set).start()
            began = time.monotonic()
            with self.assertRaises(processes.Canceled) as raised:
                # Fixed: the 30 s sleep outlasts the 0.2 s cancel under any load.
                processes.run([sys.executable, "-c", "import time; time.sleep(30)"], cwd=directory,
                              log_path=Path(directory) / "run.log", cancel=cancel, kill_after=5)
            self.assertIsInstance(raised.exception, processes.TimedOut)
            self.assertLess(time.monotonic() - began, 25)
            self.assertIn("was canceled", str(raised.exception))
            self.assertIn("supervise: canceled; sending SIGTERM", said.getvalue())
            # Negative control: a cancel never set changes nothing.
            self.assertEqual(processes.run([sys.executable, "-c", "raise SystemExit(3)"], cwd=directory,
                                           log_path=Path(directory) / "other.log", cancel=threading.Event()), 3)

    def test_a_timeout_gives_a_command_that_handles_sigterm_one_signal(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stderr(StringIO()):
            with self.assertRaises(processes.TimedOut):
                # Stretched by load, so that the command has set its handler before the timeout comes.
                processes.supervise([sys.executable, "-c", COUNTS_TERM, directory], cwd=directory, timeout=stretched(1.5),
                                    kill_after=30)
            self.assertEqual((Path(directory) / "terms").read_text(), "TERM\n")

    def test_a_stop_reaches_descendants_in_other_sessions_then_raises_interrupted(self):
        # A task stop can deliver its SIGTERM twice, to the process group and to the process; the
        # second must not cut the command's own SIGTERM handling short with SIGKILL.
        self.addCleanup(processes._stops.clear)
        for stops in [[signal.SIGTERM], [signal.SIGTERM, signal.SIGTERM]]:
            with self.subTest(stops=len(stops)), tempfile.TemporaryDirectory() as directory:
                processes._stops.clear()
                here, real, births, begun, marks = Path(directory), processes.snapshot, {}, time.monotonic(), []
                def observed():
                    if time.monotonic() - begun > stretched(30): self.fail("supervision was still waiting after 30 s, stretched by load")
                    current = real()
                    try: root, *children = map(int, (here / "children").read_text().split())
                    except FileNotFoundError: return current
                    # ROOT waits for an adoption that never comes, so only the stop ends it.
                    if not births and all(current.get(pid, (None,))[0] == root for pid in children) \
                            and all((here / f"ready-{pid}").exists() for pid in children):
                        births.update({pid: current[pid][2] for pid in children})
                        for number in stops: processes.request_stop(number)
                    return current
                def cleanup():
                    for pid, birth in births.items():
                        if real().get(pid, (0, 0, None))[2] == birth: os.kill(pid, signal.SIGKILL)
                self.addCleanup(cleanup)
                with patch.object(processes, "snapshot", observed), contextlib.redirect_stderr(StringIO()) as error, \
                     self.assertRaises(processes.Interrupted) as raised:
                    processes.supervise([sys.executable, "-c", ROOT, directory, IGNORES_TERM, HANDLES_TERM], cwd=directory,
                                        on_stop=lambda: marks.append((here / "terminated").exists()), kill_after=0.3)
                self.assertEqual(raised.exception.signum, signal.SIGTERM)
                # on_stop ran once, before any process had the signal, and SIGTERM came first.
                self.assertEqual(marks, [False])
                self.assertTrue((here / "terminated").exists())
                current = real()
                self.assertEqual({pid for pid, birth in births.items() if current.get(pid, (0, 0, None))[2] == birth}, set())
                self.assertIn("SIGTERM received; forwarding it to the command", error.getvalue())

    def owner_never_settles(self, how, directory, kill_after, settle, waits):
        """Supervise ROOT with STUCK_OWNER, an admission owner that ignores SIGTERM and never settles, and end it
        by a stop or, with how "linger", by ROOT's exit. supervise's SETTLE_WAIT is waits; the test's is settle.

        Each sample checks the bound: once a sample returns kill_after + settle seconds after the owner's SIGTERM,
        which supervise sends after it starts timing SIGKILL, supervise must send SIGKILL before its next
        sample, however loaded the host is. Return the stderr, the owner, the moments the stop under test
        started and the owner ended, and the grace before the SIGTERM."""
        here, real, births, moments, sent, kill, linger = Path(directory), processes.snapshot, {}, {}, [], os.kill, 0.2
        # A hang's backstop only, never the bound under test: it counts from the latest progress (a file the
        # command writes, the owner seen, a signal to it) and starts after supervise's deliberate wait (#128).
        progress = {"at": time.monotonic(), "state": None, "due": False}
        def observed():
            if progress["due"] and signal.SIGKILL not in sent:
                self.fail(f"SIGKILL did not reach the owner at the first sample {kill_after:g}s + {settle:g}s "
                          "after its SIGTERM")
            state = (sorted(path.name for path in here.iterdir()), bool(births), len(sent))
            if state != progress["state"]: progress.update(at=time.monotonic(), state=state)
            if time.monotonic() - progress["at"] > (linger + kill_after + settle if births else 0) + stretched(60):
                self.fail("supervision made no progress for 60 s, stretched by load")
            current = real()
            returned = time.monotonic()
            try: root, owner = map(int, (here / "children").read_text().split())
            except FileNotFoundError: return current
            if not births and current.get(owner, (None,))[0] == root and (here / f"ready-{owner}").exists():
                births[owner] = current[owner][2]
                # A stop, or ROOT's exit once it sees its descendant adopted, starts the stop under test.
                moments["started"] = time.monotonic()
                if how == "stop": processes.request_stop(signal.SIGTERM)
                else: (here / "adopted").touch()
            alive = bool(births) and current.get(owner, (0, 0, None))[2] == births[owner]
            if births and "ended" not in moments and not alive:
                moments["ended"] = time.monotonic()
            if alive and "term" in moments and returned - moments["term"] >= kill_after + settle:
                progress["due"] = True
            return current
        def recorded(pid, number):
            if pid in births:
                if number == signal.SIGTERM: moments.setdefault("term", time.monotonic())
                sent.append(number)
            kill(pid, number)
        def cleanup():
            for pid, birth in births.items():
                if real().get(pid, (0, 0, None))[2] == birth: kill(pid, signal.SIGKILL)
        self.addCleanup(cleanup)
        with patch.object(processes, "snapshot", observed), patch.object(processes.os, "kill", recorded), \
             patch.multiple(processes, create=True, LINGER=linger, SETTLE_WAIT=waits), \
             patch.dict(os.environ, {"SW_GATE_LOCK_FILE": str(here / "gate")}), \
             contextlib.redirect_stderr(StringIO()) as error:
            command = [sys.executable, "-c", ROOT, directory, STUCK_OWNER]
            if how == "stop":
                with self.assertRaises(processes.Interrupted):
                    processes.supervise(command, cwd=directory, kill_after=kill_after)
            else:
                self.assertEqual(processes.supervise(command, cwd=directory, kill_after=kill_after), 3)
        (owner, _birth), = births.items()
        self.assertEqual(sent[:1], [signal.SIGTERM])
        self.assertIn(signal.SIGKILL, sent)
        return error.getvalue(), owner, moments, linger if how == "linger" else 0

    def test_sigkill_waits_a_bounded_time_for_an_admission_owner_that_never_settles(self):
        # An owner that never settles, as one behind a hung coordination lock would not, still ends:
        # SIGKILL reaches it SETTLE_WAIT seconds after it was due, after a stop and after the grace.
        # supervise sees the owner only at its first sample once SIGKILL is due, and waits only if that
        # comes within SETTLE_WAIT: a loaded host's samples can come seconds apart, so it is stretched.
        # Each sample checks the upper bound (owner_never_settles), not a wall-clock limit: a stall
        # that the load average did not show outlasted one on the second Mac (#314).
        self.addCleanup(processes._stops.clear)
        kill_after = 0.5
        for how in ["stop", "linger"]:
            with self.subTest(how=how), tempfile.TemporaryDirectory() as directory:
                processes._stops.clear()
                settle = stretched(2)
                error, owner, moments, grace = self.owner_never_settles(how, directory, kill_after, settle, settle)
                self.assertIn(f"then SIGKILL after {kill_after:g}s", error)
                self.assertIn(f"admission owner(s) PID {owner} still running; SIGKILL waits", error)
                # A lower bound only, which a busy host cannot break.
                self.assertGreaterEqual(moments["ended"] - moments["started"], grace + kill_after + settle)
            # Negative control: a SIGKILL that waits for the owner forever fails at the first sample it was due.
            with self.subTest(how=how, waits="forever"), tempfile.TemporaryDirectory() as directory:
                processes._stops.clear()
                with self.assertRaisesRegex(AssertionError, "SIGKILL did not reach the owner at the first sample"):
                    # Fixed: the check's own bound, which no clock enforces: supervise never sends SIGKILL.
                    self.owner_never_settles(how, directory, kill_after, 0.5, float("inf"))

    def test_a_stop_leaves_an_admission_owners_new_children_to_it(self):
        # Sent the stop's SIGTERM as each appeared, three of a nested admission's ps samples in a
        # row ended, its supervision failed and its entry stayed uncertain (2026-10-27).
        self.addCleanup(processes._stops.clear)
        with tempfile.TemporaryDirectory() as directory:
            here, real, births, begun, sampled = Path(directory), processes.snapshot, {}, time.monotonic(), set()
            def observed():
                if time.monotonic() - begun > stretched(60): self.fail("supervision was still waiting after 60 s, stretched by load")
                current = real()
                try: root, *children = map(int, (here / "children").read_text().split())
                except FileNotFoundError: return current
                if not births and all(current.get(pid, (None,))[0] == root and (here / f"ready-{pid}").exists()
                                      for pid in children):
                    births.update({pid: current[pid][2] for pid in children})
                    processes.request_stop(signal.SIGTERM)
                for role in ["owner", "plain"]:
                    # A child the previous sample saw has had its signal, or been left alone.
                    sample = int((here / f"sample-{role}").read_text()) if (here / f"sample-{role}").exists() else None
                    if sample in sampled: (here / f"sampled-{role}").touch()
                    if sample in current:
                        sampled.add(sample)
                        births.setdefault(sample, current[sample][2])
                return current
            def cleanup():
                for pid, birth in births.items():
                    if real().get(pid, (0, 0, None))[2] == birth: os.kill(pid, signal.SIGKILL)
            self.addCleanup(cleanup)
            kill, sent = os.kill, []
            def recorded(pid, number):
                sent.append((pid, number))
                kill(pid, number)
            with patch.object(processes, "snapshot", observed), \
                 patch.object(processes, "SETTLE_WAIT", stretched(60), create=True), patch.object(processes.os, "kill", recorded), \
                 patch.dict(os.environ, {"SW_GATE_LOCK_FILE": str(here / "gate")}), contextlib.redirect_stderr(StringIO()):
                with self.assertRaises(processes.Interrupted):
                    processes.supervise([sys.executable, "-c", ROOT, directory, *(SAMPLES.replace("ROLE", role)
                                         for role in ["owner", "plain"])], cwd=directory, kill_after=0.5)
            owner, plain = (int((here / f"sample-{role}").read_text()) for role in ["owner", "plain"])
            self.assertIn((plain, signal.SIGTERM), sent, "a new child of another process was not sent the stop")
            self.assertNotIn((owner, signal.SIGTERM), sent, "a new child of an admission owner was sent the stop")
            self.assertIn((owner, signal.SIGKILL), sent)

    def orphaning(self, directory, how):
        """Supervise ORPHANS, sampling only once it has orphaned its grandchild, as if that all
        happened between two samples, so that only the census can find the grandchild."""
        here, real, begun = Path(directory), processes.snapshot, time.monotonic()
        def unobserved():
            while not (here / "orphaned").exists():
                if time.monotonic() - begun > stretched(30): self.fail("the command orphaned nothing within 30 s, stretched by load")
                time.sleep(0.01)
            if how == "stays" and processes.requested() is None: processes.request_stop(signal.SIGTERM)
            return real()
        with patch.object(processes, "snapshot", unobserved), contextlib.redirect_stderr(StringIO()):
            return processes.supervise([sys.executable, "-c", ORPHANS, directory, how], cwd=directory)

    def test_a_process_left_unobserved_in_the_command_session_keeps_a_failure_or_stop_uncertain(self):
        self.addCleanup(processes._stops.clear)
        for how, cause in [("exits", "exited with status 5"), ("stays", "Stopped by SIGTERM")]:
            with self.subTest(how=how), tempfile.TemporaryDirectory() as directory:
                processes._stops.clear()
                with self.assertRaisesRegex(RuntimeError, cause) as raised:
                    self.orphaning(directory, how)
                orphan = int((Path(directory) / "orphan").read_text())
                self.assertIn(f"PID {orphan} (group {orphan}, session ", str(raised.exception))
                self.assertIn("reservation retained", str(raised.exception))

    def test_a_failed_command_whose_orphan_has_ended_keeps_its_status(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(self.orphaning(directory, "ends"), 5)

    def test_the_census_skips_an_id_that_is_now_a_live_process_pid(self):
        # The command used session 100 and group 200. Once group 200 ended, a newcomer took PID 200
        # and its child joined its group; 300 is still in the command's session.
        listing = {200: (1, 200, "newcomer"), 201: (200, 200, "its child"), 300: (1, 300, "left behind")}
        sessions = {200: 200, 201: 200, 300: 100}
        with patch.object(processes, "snapshot", lambda: listing), patch.object(processes, "session", sessions.get):
            self.assertEqual(processes.survivors({100, 200}), [(300, 300, 100)])

    def test_the_census_skips_a_process_born_after_its_group_was_found_empty(self):
        # #240: group 200 was found without the command at closing. The kernel then gave PID 200 to a
        # launcher, which made group 200 for 201 and exited. 202 is in group 200 but was born before,
        # 203 in the same second as closing (lstart drops the fraction), 204 with a start ps printed
        # oddly: each could be the command's, so each is still found. 300 is the command's leftover
        # in session 100, which was never found empty.
        closing = time.mktime(time.strptime("Sat Nov  7 01:50:00 2026", "%a %b %d %H:%M:%S %Y")) + 0.5
        def at(seconds): return time.strftime("%a %b %e %H:%M:%S %Y", time.localtime(closing + seconds))
        listing = {201: (1, 200, at(180)), 202: (1, 200, at(-60)), 203: (1, 200, at(0)), 204: (1, 200, "soon"),
                   300: (1, 300, at(600)), 400: (1, 400, at(-600))}
        sessions = {201: 1, 202: 1, 203: 1, 204: 1, 300: 100, 400: 1}
        with patch.object(processes, "snapshot", lambda: listing), patch.object(processes, "session", sessions.get):
            self.assertEqual(processes.survivors({100, 200}, {200: closing}),
                             [(202, 200, 1), (203, 200, 1), (204, 200, 1), (300, 300, 100)])
            # With no recorded close time, every member is counted.
            self.assertEqual([pid for pid, _group, _sid in processes.survivors({100, 200})], [201, 202, 203, 204, 300])

    def test_a_failed_command_gives_the_census_when_its_group_was_found_empty(self):
        given = []
        def census(spaces, closed=None):
            given.append((set(spaces), dict(closed or {})))
            return []
        with tempfile.TemporaryDirectory() as directory, patch.object(processes, "survivors", census):
            before = time.time()
            self.assertEqual(processes.supervise([sys.executable, "-c", "raise SystemExit(5)"], cwd=directory), 5)
            after = time.time()
        [(spaces, closed)] = given
        self.assertEqual(closed.keys(), spaces)
        self.assertTrue(all(before <= moment <= after for moment in closed.values()), closed)

    def test_a_stop_before_the_command_starts_runs_nothing(self):
        self.addCleanup(processes._stops.clear)
        processes.request_stop(signal.SIGHUP)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(processes.Interrupted, "SIGHUP before"):
                processes.supervise([sys.executable, "-c", f"open({str(Path(directory) / 'ran')!r}, 'w')"], cwd=directory)
            self.assertFalse((Path(directory) / "ran").exists())


class NestedAdmissionTests(unittest.TestCase):
    """A light lane that runs eng admit itself, stopped as eng run stops it. BreezEPUB's book-opening
    lane admits its own preparation (#46): a stop's SIGKILL ended its eng admit while the admitted
    command was still ending, and the entry it left refused every session's heavy work until
    attended recovery (#53)."""

    def stop(self, how, lane=True):
        """Run the resource CLI, with a guard of its own, as a light lane's command or inside the
        lane's script, and stop it once its command is ready. Returns the outcome, the supervisor's
        stderr, the directory, how the admission ended as the script saw it, and the guard's owners."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        here = Path(temporary.name)
        guard = here / "gate.warm-push-guard"
        environment = {name: value for name, value in os.environ.items() if name != "VOCARO_RESOURCE_TOKEN"}
        environment.update(PYTHONPATH=str(SOURCE), HEAVY_SLOT_DIR=str(here / "slots"), SW_GATE_LOCK_FILE=str(here / "gate"))
        nested = [sys.executable, "-c", SLOW_TO_END, str(here), how]
        admission = ([sys.executable, "-m", "vocaro_engineering.resources", "--", *nested] if how == "ends"
                     else [sys.executable, "-c", UNOBSERVING, str(here), "--", *nested])
        command = [sys.executable, "-c", LANE, str(here), *admission] if lane else admission
        real, begun, births, roles, error = processes.snapshot, time.monotonic(), {}, {}, StringIO()
        def running(role, current):
            return roles.get(role) in births and current.get(roles[role], (0, 0, None))[2] == births[roles[role]]
        def observed():
            # The first sample waits for the command, after any descendant it leaves.
            while not (here / "command").exists():
                if time.monotonic() - begun > stretched(60): self.fail("the admitted command was not ready within 60 s, stretched by load")
                time.sleep(0.01)
            if time.monotonic() - begun > stretched(120): self.fail("supervision was still waiting after 120 s, stretched by load")
            current = real()
            if not births:
                owner = next(iter(json.loads(guard.read_text())["owners"].values()))["pid"]
                roles.update(command=int((here / "command").read_text()), admission=owner)
                if lane and owner in current: roles["script"] = current[owner][0]
                if (here / "orphan").exists(): roles["orphan"] = int((here / "orphan").read_text())
                births.update({pid: current[pid][2] for pid in roles.values() if pid in current})
                processes.request_stop(signal.SIGTERM)
            # The command ends only once this supervisor's SIGKILL is due and waits for the admission.
            waiting = "SIGKILL waits" in error.getvalue()
            if waiting: (here / "due").touch()
            # Once the admission it waits for has ended, the script records how before this
            # supervisor sees it gone and sends SIGKILL to what remains, the script included.
            while waiting and running("script", current) and not running("admission", current) \
                    and not (here / "status").exists():
                if time.monotonic() - begun > stretched(120): self.fail("the lane's script recorded nothing within 120 s, stretched by load")
                time.sleep(0.01)
                current = real()
            return current
        def cleanup():
            for pid, birth in births.items():
                if real().get(pid, (0, 0, None))[2] == birth: os.kill(pid, signal.SIGKILL)
        self.addCleanup(cleanup)
        processes._stops.clear()
        self.addCleanup(processes._stops.clear)
        # This supervisor's KILL_AFTER is shorter than the admitted command takes to end, and than
        # the nested admission's own. SETTLE_WAIT is long, so that a busy host cannot reach it.
        with patch.object(processes, "snapshot", observed), \
             patch.object(processes, "SETTLE_WAIT", stretched(300), create=True), contextlib.redirect_stderr(error):
            try: outcome = processes.run(command, cwd=here, log_path=here / "lane.log", env=environment, kill_after=1)
            except (processes.Interrupted, RuntimeError) as raised: outcome = raised
        status = (here / "status").read_text() if (here / "status").exists() else None
        owners = json.loads(guard.read_text())["owners"] if guard.exists() else {}
        return outcome, error.getvalue(), here, status, owners

    def test_a_stopped_lane_lets_its_nested_admission_settle_before_sigkill_and_leaves_no_entry(self):
        for lane in [True, False]:
            with self.subTest(lane=lane):
                outcome, error, here, status, owners = self.stop("ends", lane)
                log = (here / "lane.log").read_text()
                self.assertEqual(owners, {}, f"{error}\n{log}")
                self.assertIsInstance(outcome, processes.Interrupted, f"{error}\n{log}")
                self.assertTrue((here / "terminated").exists())
                if lane:
                    # The admission ended by the stop it carried out, not by SIGKILL.
                    self.assertEqual(status, str(-signal.SIGTERM), f"{error}\n{log}")

    def test_a_nested_admission_whose_command_leaves_a_descendant_still_leaves_its_entry_uncertain(self):
        outcome, error, here, status, owners = self.stop("leaves")
        log = (here / "lane.log").read_text()
        orphan = int((here / "orphan").read_text())
        # The admission settled its entry itself, as uncertain, once its census found the descendant.
        self.assertEqual(status, str(-signal.SIGTERM), f"{error}\n{log}")
        self.assertEqual([owner.get("uncertain") for owner in owners.values()], [True], f"{error}\n{log}")
        self.assertIsInstance(outcome, RuntimeError)
        self.assertIn(f"PID {orphan} (group {orphan}, session ", str(outcome))
        self.assertIn("reservation retained", str(outcome))


# Prints the inode open at descriptor argv[1], or "closed".
DESCRIPTOR = """
import os, sys
try: print("open", os.fstat(int(sys.argv[1])).st_ino)
except OSError: print("closed")
"""
# Validates the inherited admission, then runs a command that validates it again: nested work two deep.
REUSES = """
import sys
from pathlib import Path
from vocaro_engineering import processes, resources
print("child", resources.inherited(), flush=True)
log = Path(sys.argv[1]) / "grandchild.log"
status = processes.run([sys.executable, "-c", "from vocaro_engineering import resources; print('grandchild', resources.inherited())"],
                       cwd=sys.argv[1], log_path=log)
print(log.read_text(), end="")
raise SystemExit(status)
"""


def is_open(fd):
    try: os.fstat(fd)
    except OSError: return False
    return True


class InheritedDescriptorTests(unittest.TestCase):
    """A command inherits the admission lock descriptors that the token names only while this process
    holds them. eng unittest under eng admit started each module's process with the token and without
    its descriptor, and a lane group's lane in one failed to start with EBADF (#70)."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.here = Path(temporary.name)
        # The caller's own token is restored afterward.
        variables = patch.dict(os.environ, {"PYTHONPATH": str(SOURCE), "HEAVY_SLOT_DIR": str(self.here / "slots"),
                                            "SW_GATE_LOCK_FILE": str(self.here / "gate")})
        variables.start()
        self.addCleanup(variables.stop)
        os.environ.pop(resources.TOKEN, None)

    def run_with(self, token, *command):
        """Run a Python command under this token, as a lane runs, and return its status and output."""
        log = self.here / "command.log"
        status = processes.run([sys.executable, "-c", *command], cwd=self.here, log_path=log,
                               env={**os.environ, resources.TOKEN: token})
        return status, log.read_text()

    def test_a_command_inherits_an_admissions_descriptors_and_reuses_it_two_deep(self):
        with contextlib.redirect_stderr(StringIO()), resources.Admission("outer", timeout=0):
            token = os.environ[resources.TOKEN]
            self.assertEqual(self.run_with(token, REUSES, str(self.here)), (0, "child True\ngrandchild True\n"))
            # The descriptor reaches the command as the lock file itself.
            (item,) = json.loads(token)["locks"]
            self.assertEqual(self.run_with(token, DESCRIPTOR, str(item["fd"])),
                             (0, f"open {os.stat(item['path']).st_ino}\n"))

    def test_a_descriptor_this_process_does_not_hold_as_the_tokens_lock_is_not_passed(self):
        # A number nothing here holds, above the descriptors Popen's own pipes take.
        number = next(fd for fd in range(200, 1000) if not is_open(fd))
        lock, unrelated = self.here / "lock", self.here / "unrelated"
        lock.write_text(f"{os.getpid()} forged label\n")
        unrelated.write_text("not a lock\n")
        token = lambda nonce="forged": json.dumps({"schemaVersion": 1, "exclusive": False, "model": False,
            "guard": str(self.here / "gate.warm-push-guard"), "locks": [{"fd": number, "path": str(lock), "nonce": nonce}]})
        # Closed here, as the token's descriptor was in each module's process eng unittest started:
        # passing it stopped the command from starting with EBADF. A token that is not JSON names
        # nothing to pass.
        for case, value in {"closed": token(), "not JSON": "{"}.items():
            with self.subTest(case):
                self.assertEqual(self.run_with(value, DESCRIPTOR, str(number)), (0, "closed\n"))
        # Open on another file, or on the lock without the token's owner in it: passing it gave the
        # command that file. The positive control: open on the lock that names the owner, it is passed.
        cases = [("another file", unrelated, "forged", "closed"), ("another owner", lock, "another", "closed"),
                 ("the owner's lock", lock, "forged", f"open {os.stat(lock).st_ino}")]
        for case, path, nonce, expected in cases:
            with self.subTest(case):
                fd = os.open(path, os.O_RDWR)
                try:
                    os.dup2(fd, number)
                    result = self.run_with(token(nonce), DESCRIPTOR, str(number))
                finally:
                    os.close(fd)
                    os.close(number)
                self.assertEqual(result, (0, f"{expected}\n"))
