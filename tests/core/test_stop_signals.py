"""The stop-signal probes in tools/stop-signals."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import host_isolation
from host_isolation import stretched

KIT = Path(__file__).resolve().parents[2] / "tools/stop-signals"

# Recorded on vocaro-engineering#19 (trial 3, a subagent's TaskStop) and #17 (the second interrupt, whose child in
# its own session was missed), as siglog.py and watch.py wrote them.
TASK_STOP = ("""1793155041.564 child 25439 started pgid 25438 sid 25438 ppid 25438
1793155041.581 session-child 25444 started pgid 25444 sid 25444 ppid 25439
1793155055.873 session-child 25444 got SIGTERM
1793155055.873 child 25439 got SIGTERM
""", """1793155041.566 watching [25438, 25439]
1793155041.585 watching [25444]
1793155055.874 gone 25438
1793155057.357 gone 25439
1793155057.363 gone 25444
1793155057.363 done, alive []
""")
MISSED = ("""1792775980.600 child 68158 started pgid 68155 sid 68155 ppid 68155
1792775980.630 session-child 68165 started pgid 68165 sid 68165 ppid 68158
1792775989.554 child 68158 got SIGTERM
1792776040.680 session-child 68165 exiting after 60s
""", """1792775980.736 watching [68155, 68158, 68165]
1792775989.559 gone 68155
1792775990.560 gone 68158
1792776040.693 gone 68165
1792776040.701 done, alive []
""")


def wait_for(predicate, limit):
    """Whether predicate held within these seconds, stretched by load: every caller waits for what should happen."""
    end = time.monotonic() + stretched(limit)
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class ReportTests(unittest.TestCase):
    def report(self, **probes):
        with tempfile.TemporaryDirectory() as directory:
            for name, (log, watch) in probes.items():
                Path(directory, f"{name}.log").write_text(log)
                if watch is not None:
                    Path(directory, f"{name}.watch").write_text(watch)
            result = subprocess.run([sys.executable, "-I", str(KIT / "report.py"), directory], capture_output=True,
                                    text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_a_task_stop_reads_as_sigterm_then_a_kill(self):
        text = self.report(probe=TASK_STOP)
        self.assertIn("zero is 1793155055.873, the first signal logged", text)
        self.assertIn("parent 25438: gone at +0.001", text)
        self.assertIn("child 25439: got SIGTERM at +0.000; gone 1.484 s after the first, without exiting: killed", text)
        self.assertIn("session-child 25444: got SIGTERM at +0.000; gone 1.490 s after the first", text)

    def test_a_missed_child_in_its_own_session_runs_to_its_end(self):
        text = self.report(probe=MISSED)
        self.assertIn("child 68158: got SIGTERM at +0.000; gone 1.006 s after the first", text)
        self.assertIn("session-child 68165: ran to its own end at +51.126, no signal logged", text)

    def test_sigkill_alone_survival_and_an_unfinished_watch_read_apart(self):
        text = self.report(
            killed=("100.000 child 11 started pgid 10 sid 10 ppid 10\n"
                    "100.010 session-child 12 started pgid 12 sid 12 ppid 11\n",
                    "100.001 watching [10, 11]\n100.011 watching [12]\n"
                    "105.000 gone 10\n105.000 gone 11\n105.001 gone 12\n105.001 done, alive []\n"),
            ran=("100.020 child 21 started pgid 20 sid 20 ppid 20\n"
                 "100.030 session-child 22 started pgid 22 sid 22 ppid 21\n"
                 "160.020 child 21 exiting after 60s\n160.030 session-child 22 exiting after 60s\n",
                 "100.021 watching [20, 21]\n100.031 watching [22]\n160.021 gone 21\n160.031 gone 22\n"
                 "200.021 done, alive [20]\n"),
            open=("100.000 child 31 started pgid 30 sid 30 ppid 30\n100.500 child 31 got SIGHUP\n", None))
        self.assertIn("zero is 100.500, the first signal logged", text)
        self.assertIn("child 11: gone at +4.500, no signal logged, without exiting: SIGKILL alone", text)
        self.assertIn("session-child 12: gone at +4.501, no signal logged", text)
        self.assertIn("child 21: ran to its own end at +59.520, no signal logged", text)
        self.assertIn("parent 20: still running when the watcher ended", text)
        self.assertIn("open.watch (missing)", text)
        self.assertIn("child 31: not seen to end: its watcher is still running or never saw it; got SIGHUP at +0.000",
                      text)

    def test_an_empty_directory_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, "-I", str(KIT / "report.py"), directory], capture_output=True,
                                    text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("no NAME.log in", result.stderr)


class ProbeTests(unittest.TestCase):
    """Real processes, each started and signaled by the test alone."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.addCleanup(self.stop_leftovers)

    def stop_leftovers(self):
        # Only a process whose command names this test's directory is the test's own.
        for line in subprocess.run(["ps", "-A", "-o", "pid=,command="], capture_output=True, text=True).stdout.splitlines():
            pid, _, command = line.strip().partition(" ")
            if str(self.directory) in command and int(pid) != os.getpid():
                try:
                    os.kill(int(pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def detach(self, *command):
        result = subprocess.run([sys.executable, "-I", str(KIT / "detach.py"), *command], capture_output=True,
                                text=True, check=True)
        self.assertRegex(result.stdout, r"^\d+\n$")
        return int(result.stdout)

    def probe(self, name, seconds):
        log, watch = self.directory / f"{name}.log", self.directory / f"{name}.watch"
        # The watcher must outlast what it watches, however slowly the host runs it.
        self.detach(sys.executable, "-I", str(KIT / "watch.py"), str(log), str(watch), str(stretched(30)))
        shell = self.detach("/bin/sh", "-c", f'"$0" -I "$1" "$2" child {seconds}; :', sys.executable,
                            str(KIT / "siglog.py"), str(log))
        self.assertTrue(wait_for(lambda: log.exists() and log.read_text().count(" started ") == 2, 10), "never started")
        events = [line.split(" ", 1)[1] for line in log.read_text().splitlines()]
        child, session_child = (int(next(event.split()[1] for event in events if event.split()[0] == role))
                                for role in ("child", "session-child"))
        # The child runs in the shell's group and session; its own child leads a session of its own.
        self.assertIn(f"child {child} started pgid {shell} sid {shell} ppid {shell}", events)
        self.assertIn(f"session-child {session_child} started pgid {session_child} sid {session_child} ppid {child}",
                      events)
        self.assertTrue(wait_for(lambda: watch.exists() and all(str(pid) in watch.read_text()
                                                                for pid in (shell, child, session_child)), 10))
        return shell, child, session_child, log, watch

    def report(self):
        return subprocess.run([sys.executable, "-I", str(KIT / "report.py"), str(self.directory)], capture_output=True,
                              text=True, check=True).stdout

    def test_sigterm_then_sigkill_shows_the_order_the_gap_and_a_log_that_stops(self):
        # It must outlive the test that stops it, however slowly the host runs it.
        shell, child, session_child, log, watch = self.probe("stopped", stretched(30))
        sent = time.time()
        os.killpg(shell, signal.SIGTERM)
        os.kill(session_child, signal.SIGTERM)
        # The handlers only log, so both run on until the SIGKILL, sent once both have logged the SIGTERM.
        self.assertTrue(wait_for(lambda: log.read_text().count(" got SIGTERM") == 2, 10), log.read_text())
        time.sleep(0.3)
        killed = time.time()
        os.killpg(shell, signal.SIGKILL)
        os.kill(session_child, signal.SIGKILL)
        self.assertTrue(wait_for(lambda: "done, alive []" in watch.read_text(), 10), watch.read_text())
        text = log.read_text()
        self.assertNotIn("exiting", text)
        for pid in (child, session_child):
            got = [float(line.split()[0]) for line in text.splitlines() if line.endswith(f" {pid} got SIGTERM")]
            gone = [float(line.split()[0]) for line in watch.read_text().splitlines() if line.endswith(f"gone {pid}")]
            self.assertEqual(len(got), 1, text)
            self.assertTrue(sent - 0.01 <= got[0] < killed, (sent, got, killed))
            self.assertGreaterEqual(gone[0], killed - 0.01)
        report = self.report()
        self.assertIn(f"parent {shell}: gone at", report)
        self.assertRegex(report, rf"\nchild {child}: got SIGTERM at \+0\.\d+; gone \d+\.\d+ s after the first, "
                                 r"without exiting: killed")
        self.assertRegex(report, rf"\nsession-child {session_child}: got SIGTERM .* without exiting: killed")

    def test_a_probe_left_alone_runs_to_its_end(self):
        shell, child, session_child, log, watch = self.probe("alone", 0.5)
        self.assertTrue(wait_for(lambda: "done, alive []" in watch.read_text(), 10), watch.read_text())
        text = self.report()
        self.assertRegex(text, rf"\nchild {child}: ran to its own end at \+\d\.\d+, no signal logged")
        self.assertRegex(text, rf"session-child {session_child}: ran to its own end at \+\d\.\d+, no signal logged")

    def test_detach_leaves_the_command_outside_the_callers_group_session_and_tree(self):
        go, detached, plain = self.directory / "go", self.directory / "detached", self.directory / "plain"
        record = ("import os, sys, time\nwhile not os.path.exists(sys.argv[1]): time.sleep(0.01)\n"
                  "open(sys.argv[2], 'w').write(f'{os.getppid()} {os.getpgrp()} {os.getsid(0)} {os.getpid()}')\n")
        pid = self.detach(sys.executable, "-I", "-c", record, str(go), str(detached))
        # The control: the same command started plainly stays in the caller's group, session and tree.
        process = subprocess.Popen([sys.executable, "-I", "-c", record, str(go), str(plain)])
        go.touch()
        self.assertEqual(process.wait(stretched(10)), 0)
        self.assertTrue(wait_for(lambda: detached.exists() and detached.read_text(), 10))
        self.assertEqual(detached.read_text().split(), ["1", str(pid), str(pid), str(pid)])
        self.assertEqual(plain.read_text().split(), [str(os.getpid()), str(os.getpgrp()), str(os.getsid(0)),
                                                     str(process.pid)])


if __name__ == "__main__":
    unittest.main()
