"""Offline state contracts: all state reads/toggles are fakes, with the real lease record."""
import contextvars
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src/python"))
from queuekeeper import processes
from queuekeeper_apple import devices
from queuekeeper_mac_desktop import reservations
from queuekeeper_mac_desktop import foreground
import host_isolation
from queuekeeper_apple.voiceover import guard, state


def idle(test):
    """An idle host for the test: every bound is its base value, so a fake clock's arithmetic stays exact."""
    for patcher in (patch.object(state.os, "getloadavg", return_value=(0.5, 0.5, 0.5)),
                    patch.object(state.waiting, "busy_now", return_value=None)):
        patcher.start()
        test.addCleanup(patcher.stop)


def system(public="true\n", processes=(), comm=None):
    """A fake subprocess.run for the state reader: osascript's getter output, then pgrep's PIDs and ps's name."""
    calls = []
    def run(command, **options):
        calls.append((command, options))
        if command[0] == "osascript": return subprocess.CompletedProcess(command, 0, public, "")
        if command[0] == "pgrep":
            found = " ".join(processes)
            return subprocess.CompletedProcess(command, 0 if found else 1, found + "\n" if found else "", "")
        if command[0] == "ps": return subprocess.CompletedProcess(command, 0, (comm or state.VOICEOVER) + "\n", "")
        raise AssertionError(f"unexpected command {command}")
    return run, calls


class PublicReadTests(unittest.TestCase):
    def setUp(self):
        idle(self)

    def test_only_exact_public_boolean_output_is_accepted(self):
        for output, expected in [("true\n", True), ("false\n", False)]:
            with self.subTest(output), patch.object(state.subprocess, "run", return_value=
                    subprocess.CompletedProcess([], 0, output)) as command:
                self.assertIs(state._public(), expected)
                self.assertEqual(command.call_args.args[0][:3], ["osascript", "-l", "JavaScript"])
                self.assertIn("isVoiceOverEnabled", command.call_args.args[0][-1])
                self.assertEqual(command.call_args.kwargs["timeout"], state.WAIT)
        for output in ['"true"', "1", "0", "null", "undefined", "", "false\nwarning", "{}"]:
            with self.subTest(output), patch.object(state.subprocess, "run", return_value=
                    subprocess.CompletedProcess([], 0, output)), self.assertRaises(state.UnknownState):
                state._public()
        for error in [OSError("missing"), subprocess.TimeoutExpired("osascript", state.WAIT)]:
            with self.subTest(error), patch.object(state.subprocess, "run", side_effect=error), \
                    self.assertRaises(state.UnknownState): state._read()
        with patch.object(state.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "true")), \
                self.assertRaises(state.UnknownState): state._read()


class StaleKeyTests(unittest.TestCase):
    """isVoiceOverEnabled follows voiceOverOnOffKey, which an AppleScript quit leaves at 1 (#137)."""
    def setUp(self):
        idle(self)

    def test_on_with_voiceovers_process_reads_on(self):
        run, calls = system(processes=["4242"])
        with patch.object(state.subprocess, "run", side_effect=run):
            self.assertIs(state._read(), True)
        self.assertEqual([command[0] for command, _ in calls], ["osascript", "pgrep", "ps"])
        self.assertEqual(calls[2][0], ["ps", "-p", "4242", "-o", "comm="])

    def test_a_stale_on_key_with_no_voiceover_process_reads_off(self):
        # The getter says on and no VoiceOver runs: the key an AppleScript quit left. Before #137 this read on,
        # so the next run sent no Command-F5 and every session skipped.
        run, calls = system(processes=())
        with patch.object(state, "WAIT", 0.2), patch.object(state.subprocess, "run", side_effect=run):
            self.assertIs(state._read(), False)
        self.assertIn("pgrep", [command[0] for command, _ in calls])

    def test_off_reads_off_without_a_process_query(self):
        run, calls = system(public="false\n", processes=["4242"])
        with patch.object(state.subprocess, "run", side_effect=run):
            self.assertIs(state._read(), False)
        self.assertEqual([command[0] for command, _ in calls], ["osascript"])

    def test_on_whose_process_starts_within_the_wait_reads_on_and_unknown_processes_refuse(self):
        clock, starts = [0.0], iter([False, False, True])
        def sleep(seconds): clock[0] += seconds
        with patch.object(state.time, "monotonic", side_effect=lambda: clock[0]), \
             patch.object(state.time, "sleep", side_effect=sleep):
            self.assertIs(state._read(public=lambda: True, running=lambda: next(starts)), True)
            self.assertLess(clock[0], state.WAIT)
            # Absent for the whole stretched wait: stale.
            self.assertIs(state._read(public=lambda: True, running=lambda: False), False)
            self.assertGreaterEqual(clock[0], state.WAIT)
        run, _ = system(processes=["4242"], comm="/tmp/VoiceOver")
        with patch.object(state.subprocess, "run", side_effect=run), self.assertRaises(state.UnknownState):
            state._read()


class StretchTests(unittest.TestCase):
    def setUp(self):
        quiet = patch.object(state.waiting, "busy_now", return_value=None)
        quiet.start()
        self.addCleanup(quiet.stop)

    def test_a_low_load_beside_a_saturated_cpu_stretches_to_the_limit(self):
        # build-host on 2026-11-12: a 5 s Command-F5 bound ended KEY_LATE with the load at or below 10 on 10 cores,
        # the CPU 100% busy and the load around it 490 to 630 (vocaro/queuekeeper#43).
        for load, busy, expected in [(7.45, 100.0, 5 * state.STRETCH_LIMIT), (10, 99.0, 5),
                                     (7.45, 50.0, 5), (7.45, None, 5), (250, 100.0, 5 * state.STRETCH_LIMIT)]:
            with self.subTest(load=load, busy=busy), patch.object(state.os, "getloadavg", return_value=(load,) * 3), \
                    patch.object(state.os, "cpu_count", return_value=10), \
                    patch.object(state.waiting, "busy_now", return_value=busy):
                self.assertAlmostEqual(state.stretched(5), expected)
                self.assertEqual(state.stretch_reading()["factor"], expected / 5)

    def test_a_command_f5_timeout_reports_the_reading_taken_when_the_key_was_sent(self):
        def timed_out(command, *, timeout, **_):
            self.assertEqual(timeout, 120)
            raise subprocess.TimeoutExpired(command, timeout)
        lease = type("Lease", (), {"check": lambda self, restoring=False: None, "scopes": [[]]})()
        with patch.object(state.os, "getloadavg", side_effect=[(7.45,) * 3]), \
                patch.object(state.os, "cpu_count", return_value=10), \
                patch.object(state.waiting, "busy_now", side_effect=[100.0]), \
                patch.object(state.subprocess, "run", side_effect=timed_out), \
                self.assertRaises(state.TransitionFailed) as failed:
            state._set(lease, False, lambda: True, state._toggle)
        evidence = ("Command-F5 exceeded its 120s bound (stretched 24 times, by a one-minute load of 7.45 on 10 cores "
                    "with the CPU 100% busy)")
        self.assertIn(evidence, str(failed.exception))
        self.assertIn(evidence, lease.scopes[-1][0])

    def test_a_verification_timeout_reports_its_own_reading(self):
        lease = type("Lease", (), {"check": lambda self, restoring=False: None, "scopes": [[]]})()
        clock = [0]

        def slow_key():
            clock[0] += 15

        def load():
            return (1,) * 3 if clock[0] == 0 else (7.45,) * 3

        def sleep(seconds):
            clock[0] += seconds

        with patch.object(state.os, "getloadavg", side_effect=load), \
                patch.object(state.os, "cpu_count", return_value=10), \
                patch.object(state.waiting, "busy_now", side_effect=lambda: 50.0 if clock[0] == 0 else 100.0), \
                patch.object(state.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(state.time, "sleep", side_effect=sleep), \
                self.assertRaises(state.TransitionFailed) as failed:
            state._set(lease, False, lambda: True, slow_key, poll=1)
        self.assertEqual(clock[0], 135)
        self.assertIn("VoiceOver did not reach off within 120s", str(failed.exception))
        self.assertIn("(stretched 24 times, by a one-minute load of 7.45 on 10 cores with the CPU 100% busy)",
                      str(failed.exception))
        self.assertIn("stretched 24 times", lease.scopes[-1][0])

    def test_bounds_stretch_with_load_per_core_up_to_the_limit(self):
        for load, cores, expected in [(0.5, 10, 5), (10, 10, 5), (217, 10, 108.5), (5000, 10, 5 * state.STRETCH_LIMIT)]:
            with self.subTest(load=load), patch.object(state.os, "getloadavg", return_value=(load,) * 3), \
                    patch.object(state.os, "cpu_count", return_value=cores):
                self.assertAlmostEqual(state.stretched(5), expected)
        with patch.object(state.os, "getloadavg", side_effect=OSError("no load")):
            self.assertEqual(state.stretched(5), 5 * state.STRETCH_LIMIT)

    def test_command_f5_waits_by_the_hosts_load_not_a_fixed_five_seconds(self):
        # Another Mac's incident: loads 217/126/205 on 10 cores; the fixed 5 s timed out (#137).
        for load, expected in [((217, 126, 205), 108.5), ((1, 1, 1), state.WAIT)]:
            with self.subTest(load=load), patch.object(state.os, "getloadavg", return_value=load), \
                    patch.object(state.os, "cpu_count", return_value=10), \
                    patch.object(state.subprocess, "run") as command:
                state._toggle()
            self.assertIn("key code 96 using {command down}", command.call_args.args[0][-1])
            self.assertAlmostEqual(command.call_args.kwargs["timeout"], expected)


class ReadinessQueryTests(unittest.TestCase):
    def setUp(self):
        idle(self)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.record = devices.Record(Path(temporary.name) / "record.json")
        self.owner = "offline readiness query"
        self.entry = self.record.reserve(devices.FOREGROUND, owner=self.owner, revision=0, purpose="offline test",
            release_by=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
        self.lease = state._Lease(self.owner, self.record)

    def test_absence_is_readiness_only_for_current_user_and_exact_empty_success(self):
        result = subprocess.CompletedProcess([], 1, "", "")
        with patch.object(state.os, "getuid", return_value=501), \
             patch.object(state.subprocess, "run", return_value=result) as command:
            self.assertIs(state._ready_to_enable(self.lease), True)
        self.assertEqual(command.call_args.args[0], ["pgrep", "-u", "501", "-x", "VoiceOver"])
        self.assertGreater(command.call_args.kwargs["timeout"], 0)
        self.assertLessEqual(command.call_args.kwargs["timeout"], state.WAIT)

    def test_recognized_process_means_not_ready_and_every_query_checks_the_lease(self):
        results = [subprocess.CompletedProcess([], 0, "123\n", ""),
                   subprocess.CompletedProcess([], 0, state.VOICEOVER + "\n", "")]
        with patch.object(state.subprocess, "run", side_effect=results) as command, \
             patch.object(self.lease, "check", wraps=self.lease.check) as checked:
            self.assertIs(state._ready_to_enable(self.lease), False)
        self.assertEqual(command.call_count, 2)
        self.assertEqual(command.call_args.args[0], ["ps", "-p", "123", "-o", "comm="])
        self.assertEqual(checked.call_count, 4)

    def test_query_errors_and_unrecognized_processes_are_unknown_not_absent(self):
        for result in [subprocess.CompletedProcess([], 1, "", "query error"),
                       subprocess.CompletedProcess([], 1, "\n", ""),
                       subprocess.CompletedProcess([], 0, "", ""),
                       subprocess.CompletedProcess([], 0, "not-a-pid", ""),
                       subprocess.CompletedProcess([], 2, "", "")]:
            with self.subTest(result), patch.object(state.subprocess, "run", return_value=result), \
                    self.assertRaises(state.UnknownState): state._ready_to_enable(self.lease)
        for process in [subprocess.CompletedProcess([], 0, "/tmp/VoiceOver", ""),
                        subprocess.CompletedProcess([], 0, state.VOICEOVER, "query error")]:
            with self.subTest(process), patch.object(state.subprocess, "run", side_effect=[
                    subprocess.CompletedProcess([], 0, "123", ""), process]), \
                    self.assertRaises(state.UnknownState): state._ready_to_enable(self.lease)
        with patch.object(state.subprocess, "run", side_effect=subprocess.TimeoutExpired("pgrep", 5)), \
                self.assertRaises(state.UnknownState): state._ready_to_enable(self.lease)

    def test_a_process_ending_between_queries_needs_fresh_absence_confirmation(self):
        with patch.object(state.subprocess, "run", side_effect=[
                subprocess.CompletedProcess([], 0, "123", ""), subprocess.CompletedProcess([], 1, "", "")]):
            self.assertIs(state._ready_to_enable(self.lease), False)
        with patch.object(state.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "")):
            self.assertIs(state._ready_to_enable(self.lease), True)

    def test_readiness_allows_only_the_same_scope_to_restore_its_own_exact_uncertainty(self):
        self.lease.uncertain("owned transition failure")
        with patch.object(state.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "")) as query:
            self.assertIs(state._ready_to_enable(self.lease, restoring=True), True)
            with self.assertRaises(devices.Refused): state._ready_to_enable(self.lease)
        self.assertEqual(query.call_count, 1)


    def test_replacement_during_pgrep_stops_before_process_identity_or_any_further_query(self):
        def replaced(*args, **options):
            entry = self.record.entry(devices.FOREGROUND)
            self.record.release(devices.FOREGROUND, owner=self.owner, revision=entry["revision"],
                                outcome="test replacement", restoration="test", frontmost="not-ours")
            self.record.reserve(devices.FOREGROUND, owner="other", revision=entry["revision"] + 1,
                                purpose="new holder", release_by=entry["expectedReleaseBy"])
            return subprocess.CompletedProcess([], 0, "123", "")
        with patch.object(state.subprocess, "run", side_effect=replaced) as command, \
                self.assertRaises(devices.Refused): state._ready_to_enable(self.lease)
        self.assertEqual(command.call_count, 1)


class StateTests(unittest.TestCase):
    def setUp(self):
        idle(self)
        # Restoration's settle window after a failed transition, short for offline fakes.
        settle = patch.object(state, "SETTLE", 0.3)
        settle.start()
        self.addCleanup(settle.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.record = devices.Record(Path(temporary.name) / "record.json")
        self.owner = "owned offline contract"
        self.entry = self.record.reserve(devices.FOREGROUND, owner=self.owner, revision=0, purpose="offline test",
            release_by=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
        readiness = patch.object(state, "_ready_to_enable", return_value=True, create=True)
        readiness.start()
        self.addCleanup(readiness.stop)
        self.enabled = False
        self.actions = []

    def read(self):
        self.actions.append(("read", self.enabled))
        return self.enabled

    def toggle(self):
        self.actions.append(("toggle", not self.enabled))
        self.enabled = not self.enabled

    def scope(self, **options):
        options.setdefault("read_state", self.read)
        options.setdefault("toggle", self.toggle)
        return state.preserved(self.owner, record=self.record, **options)

    def toggles(self):
        return [value for action, value in self.actions if action == "toggle"]

    def assert_uncertain(self):
        entry = self.record.entry(devices.FOREGROUND)
        self.assertEqual(devices.condition(entry), "uncertain")
        self.assertFalse(entry["voiceOverRestoration"]["verified"])
        with self.assertRaisesRegex(devices.Refused, "unresolved uncertainty"):
            self.record.release(devices.FOREGROUND, owner=self.owner, revision=entry["revision"],
                                outcome="failed", restoration="unverified", frontmost="not-ours")

    def test_quiet_test_restores_each_actual_original_and_never_blindly_toggles(self):
        for original in (False, True):
            with self.subTest(original):
                self.enabled = original
                self.actions.clear()
                with self.scope(target=False) as captured:
                    self.assertIs(captured, original)
                    self.assertIs(self.enabled, False)
                self.assertIs(self.enabled, original)
                self.assertEqual(self.toggles(), [False, True] if original else [])

    def test_default_preserves_on_and_nested_speech_session_restores_independently(self):
        self.enabled = True
        with self.scope():
            self.assertTrue(self.enabled)
            self.assertEqual(self.toggles(), [])
            with self.scope(target=False):
                with self.scope(target=True): self.assertTrue(self.enabled)
                self.assertFalse(self.enabled)
            self.assertTrue(self.enabled)
        self.assertEqual(self.toggles(), [False, True, False, True])

    def test_error_and_stop_restore_original_before_they_propagate(self):
        for error in (RuntimeError("body"), guard.Stopped("canceled"), KeyboardInterrupt()):
            with self.subTest(error):
                self.enabled = True
                self.actions.clear()
                with self.assertRaises(type(error)):
                    with self.scope(target=False): raise error
                self.assertTrue(self.enabled)
                self.assertEqual(self.toggles(), [False, True])
                self.assertNotEqual(devices.condition(self.record.entry(devices.FOREGROUND)), "uncertain")

    def test_signal_restores_before_propagating_and_keeps_supervisor_handler(self):
        self.enabled = True
        old = signal.getsignal(signal.SIGTERM)
        # Controlled signal to this unit-test process only; no UI state is accessed.
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        self.addCleanup(signal.signal, signal.SIGTERM, old)
        with self.assertRaises(guard.Stopped):
            with self.scope(target=False): os.kill(os.getpid(), signal.SIGTERM)
        self.assertTrue(self.enabled)
        self.assertIs(signal.getsignal(signal.SIGTERM), signal.SIG_DFL)
        with processes.forwarding_signals():
            handler = signal.getsignal(signal.SIGTERM)
            with self.scope(): self.assertIs(signal.getsignal(signal.SIGTERM), handler)
            self.assertIs(signal.getsignal(signal.SIGTERM), handler)

    def test_first_stop_during_restoration_is_delivered_only_after_original_is_verified(self):
        self.enabled = True
        old = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        self.addCleanup(signal.signal, signal.SIGTERM, old)
        def toggle_with_stop_while_restoring():
            if self.enabled is False:
                os.kill(os.getpid(), signal.SIGTERM)
                self.assertFalse(self.enabled)  # Stop did not interrupt before the toggle.
            self.toggle()
        with self.assertRaises(guard.Stopped):
            with self.scope(target=False, toggle=toggle_with_stop_while_restoring): pass
        self.assertTrue(self.enabled)
        self.assertEqual(self.toggles(), [False, True])
        self.assertNotEqual(devices.condition(self.record.entry(devices.FOREGROUND)), "uncertain")
        self.assertIs(signal.getsignal(signal.SIGTERM), signal.SIG_DFL)

    def test_stop_after_applied_setup_toggle_restores_before_propagating(self):
        self.enabled = True
        def toggle_then_stop():
            self.toggle()
            if self.enabled is False: raise guard.Stopped("stop during transition")
        with self.assertRaises(guard.Stopped):
            with self.scope(target=False, toggle=toggle_then_stop): self.fail("stopped setup ran")
        self.assertTrue(self.enabled)
        self.assertEqual(self.toggles(), [False, True])


    def test_unreadable_initial_state_never_changes_anything(self):
        for value in (None, "false", 0):
            with self.subTest(value), self.assertRaises(state.UnknownState):
                with self.scope(read_state=lambda: value, target=False): self.fail("unknown initial state ran")
        self.assertEqual(self.actions, [])
        self.assertNotEqual(devices.condition(self.record.entry(devices.FOREGROUND)), "uncertain")

    def test_unreadable_final_state_retains_real_record_uncertainty(self):
        self.enabled = True
        with self.assertRaises(state.TransitionFailed):
            with self.scope(target=False):
                self.enabled = None
        self.assertEqual(self.toggles(), [False])
        self.assert_uncertain()

    def assert_releasable(self):
        entry = self.record.entry(devices.FOREGROUND)
        self.assertNotEqual(devices.condition(entry), "uncertain")
        self.assertNotIn("voiceOverRestoration", entry)
        released = self.record.release(devices.FOREGROUND, owner=self.owner, revision=entry["revision"],
                                       outcome="failed transition, original restored", restoration="verified",
                                       frontmost="restored")
        self.assertEqual(released["state"], "released")

    def test_applied_setup_toggle_that_reports_failure_restores_and_releases(self):
        self.enabled = True
        def applied_then_error():
            self.toggle()
            if not self.enabled: raise OSError("lost toggle reply after application")
        with self.assertRaises(state.TransitionFailed) as failed:
            with self.scope(target=False, toggle=applied_then_error): self.fail("failed setup ran body")
        self.assertTrue(failed.exception.restored)
        self.assertTrue(self.enabled)
        self.assertEqual(self.toggles(), [False, True])
        self.assert_releasable()

    def test_public_setter_applied_error_restores_under_local_preserved_scope_and_releases(self):
        self.enabled = False
        def applied_then_error():
            self.toggle()
            if self.enabled: raise OSError("reply lost after enable")
        with patch.object(state, "_read", self.read), patch.object(state, "_toggle", applied_then_error):
            with self.assertRaises(state.TransitionFailed) as failed:
                with self.scope(): state.set_enabled(True, self.owner, record=self.record)
        self.assertTrue(failed.exception.restored)
        self.assertFalse(self.enabled)
        self.assertEqual(self.toggles(), [True, False])
        # Leaving the scope drops its failures: a later scope starts clean and may run.
        self.actions.clear()
        with self.scope(): pass
        self.assertEqual(self.toggles(), [])
        self.assert_releasable()

    def test_timed_out_command_f5_that_applies_late_is_undone_and_the_lease_releases(self):
        # #137: the driver's Command-F5 timed out on a loaded Mac and VoiceOver came on afterwards.
        # The scope sees the late key while it settles, turns VoiceOver off again and verifies it stays off.
        self.enabled = False
        late = []
        def read():
            if late and late[0] == 0:
                late.clear()
                self.enabled = True  # System Events delivered the timed-out key.
            elif late: late[0] -= 1
            return self.read()
        def timed_out():
            if self.enabled: return self.toggle()
            self.actions.append(("toggle", "timed out"))
            late.append(2)
            raise subprocess.TimeoutExpired("osascript", state.WAIT)
        with patch.object(state, "_read", read), patch.object(state, "_toggle", timed_out):
            with self.assertRaises(state.TransitionFailed) as failed:
                with self.scope(read_state=read, toggle=timed_out):
                    state.set_enabled(True, self.owner, record=self.record)
        self.assertTrue(failed.exception.restored)
        self.assertFalse(self.enabled)
        self.assertEqual(self.toggles(), ["timed out", False])
        self.assert_releasable()

    def test_late_key_that_restoration_cannot_undo_keeps_the_lease_uncertain(self):
        # Negative control: the late key arrives and every restoring key fails, so nothing verifies the original.
        self.enabled = False
        def timed_out():
            self.actions.append(("toggle", "timed out"))
            self.enabled = True if not any(action == ("toggle", "timed out") for action in self.actions[:-1]) \
                else self.enabled
            raise subprocess.TimeoutExpired("osascript", state.WAIT)
        with patch.object(state, "_read", self.read), patch.object(state, "_toggle", timed_out):
            with self.assertRaisesRegex(state.TransitionFailed, "original state was not restored") as failed:
                with self.scope(toggle=timed_out): state.set_enabled(True, self.owner, record=self.record)
        self.assertFalse(failed.exception.restored)
        self.assertTrue(self.enabled)
        self.assertEqual(len(self.toggles()), 1 + state.RESTORE_ATTEMPTS)
        self.assert_uncertain()


    def test_restore_on_waits_for_quitting_to_finish_instead_of_using_public_false_as_readiness(self):
        self.enabled = True
        quitting = [False]
        keys = []
        polls = []
        def toggle():
            keys.append((not self.enabled, quitting[0]))
            self.enabled = not self.enabled
            if not self.enabled: quitting[0] = True
        def ready(*args, **options):
            polls.append(quitting[0])
            return not quitting[0]
        def settled(_seconds): quitting[0] = False
        with patch.object(state, "_ready_to_enable", side_effect=ready, create=True), \
             patch.object(state.time, "sleep", side_effect=settled):
            with self.scope(target=False, toggle=toggle): self.assertFalse(self.enabled)
        self.assertTrue(self.enabled)
        self.assertEqual(keys, [(False, False), (True, False)])
        self.assertEqual(polls, [True, False])

    def test_permanently_quitting_refuses_without_an_enable_key_at_the_twenty_second_bound(self):
        clock = [0.0]
        lease = state._Lease(self.owner, self.record)
        def sleep(seconds): clock[0] += seconds
        with patch.object(state.time, "monotonic", side_effect=lambda: clock[0]), \
             patch.object(state.time, "sleep", side_effect=sleep), self.assertRaises(state.NotReady):
            state._set(lease, True, self.read, self.toggle, ready=lambda *a, **kw: False)
        self.assertEqual(clock[0], 20)
        self.assertEqual(self.toggles(), [])
        self.assertNotEqual(devices.condition(self.record.entry(devices.FOREGROUND)), "uncertain")

    def test_unknown_readiness_does_not_send_an_enable_key(self):
        lease = state._Lease(self.owner, self.record)
        for ready in (lambda *a, **kw: None, lambda *a, **kw: (_ for _ in ()).throw(state.UnknownState("query failed"))):
            with self.subTest(ready), self.assertRaises(state.UnknownState):
                state._set(lease, True, self.read, self.toggle, ready=ready)
        self.assertEqual(self.toggles(), [])

    def test_state_is_rechecked_after_readiness_and_an_already_on_state_is_never_toggled(self):
        lease = state._Lease(self.owner, self.record)
        def ready(*args, **options):
            self.enabled = True
            return True
        state._set(lease, True, self.read, self.toggle, ready=ready)
        self.assertTrue(self.enabled)
        self.assertEqual(self.toggles(), [])

    def test_lost_lease_while_quitting_forbids_any_further_read_or_enable(self):
        lease = state._Lease(self.owner, self.record)
        def replaced(*args, **options):
            entry = self.record.entry(devices.FOREGROUND)
            self.record.release(devices.FOREGROUND, owner=self.owner, revision=entry["revision"],
                                outcome="test replacement", restoration="test", frontmost="not-ours")
            self.record.reserve(devices.FOREGROUND, owner="other", revision=entry["revision"] + 1,
                                purpose="new test holder", release_by=entry["expectedReleaseBy"])
            self.actions.clear()
            return True
        with self.assertRaises(devices.Refused): state._set(lease, True, self.read, self.toggle, ready=replaced)
        self.assertEqual(self.actions, [])


    def test_unverified_transition_times_out_and_keeps_real_uncertainty(self):
        attempts = []
        lease = state._Lease(self.owner, self.record)
        with self.assertRaises(state.TransitionFailed):
            state._set(lease, True, self.read, lambda: attempts.append("sent"), wait=0, poll=0)
        self.assertEqual(attempts, ["sent"])
        self.assertFalse(self.enabled)
        self.assert_uncertain()


    def test_failed_restore_retains_reservation(self):
        self.enabled = True
        def fails_restoring():
            if self.enabled is False: raise OSError("toggle not sent")
            self.toggle()
        with self.assertRaises(state.TransitionFailed):
            with self.scope(target=False, toggle=fails_restoring): pass
        self.assertFalse(self.enabled)
        self.assert_uncertain()

    def test_foreign_or_replaced_lease_is_never_read_toggled_or_marked(self):
        with self.assertRaises(devices.Refused):
            with state.preserved("other", record=self.record, read_state=self.read, toggle=self.toggle): pass
        self.assertEqual(self.actions, [])
        self.enabled = True
        with self.assertRaises(state.TransitionFailed):
            with self.scope(target=False):
                entry = self.record.entry(devices.FOREGROUND)
                self.record.release(devices.FOREGROUND, owner=self.owner, revision=entry["revision"],
                                    outcome="test replacement", restoration="test", frontmost="not-ours")
                self.record.reserve(devices.FOREGROUND, owner=self.owner, revision=entry["revision"] + 1,
                                    purpose="new same-name lease", release_by=entry["expectedReleaseBy"])
                self.actions.clear()
        self.assertEqual(self.actions, [])
        self.assertNotEqual(devices.condition(self.record.entry(devices.FOREGROUND)), "uncertain")

    def test_unrelated_uncertainty_cannot_authorize_a_restoration_toggle(self):
        self.enabled = True
        with self.assertRaises(state.TransitionFailed):
            with self.scope(target=False):
                entry = self.record.entry(devices.FOREGROUND)
                self.record.update(devices.FOREGROUND, owner=self.owner, revision=entry["revision"], uncertain=True,
                                   run="unconfirmed process termination")
                self.actions.clear()
        self.assertEqual(self.actions, [])
        self.assertTrue(self.record.entry(devices.FOREGROUND)["uncertain"])

    def test_expired_approval_still_allows_original_restoration_but_no_new_work(self):
        self.enabled = True
        with self.scope(target=False):
            with patch.object(reservations, "require_window", side_effect=devices.Refused("expired")):
                with self.assertRaises(devices.Refused): state.read(self.owner, record=self.record)
                # Restoration bypasses only the approval deadline, not lease ownership.
                # Keep patch through __exit__ using an independently entered context below.
        self.assertTrue(self.enabled)
        scope = self.scope(target=False)
        scope.__enter__()
        with patch.object(reservations, "require_window", side_effect=devices.Refused("expired")):
            scope.__exit__(None, None, None)
        self.assertTrue(self.enabled)


class RecoverRestorationTests(unittest.TestCase):
    """The run's own runner may set VoiceOver back over a session's unverified restoration, and nothing else
    (owner's decision, 2026-11-12, vocaro/EPUBLib#416)."""
    def setUp(self):
        idle(self)
        settle = patch.object(state, "SETTLE", 0.3)
        settle.start()
        self.addCleanup(settle.stop)
        readiness = patch.object(state, "_ready_to_enable", return_value=True, create=True)
        readiness.start()
        self.addCleanup(readiness.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.record = devices.Record(Path(temporary.name) / "record.json")
        self.owner = "queuekeeper 4242 on another Mac"
        entry = self.record.reserve(devices.FOREGROUND, owner=self.owner, revision=0, purpose="offline test",
            release_by=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
        # As foreground.slot leaves it: this process is the run, and the original it read is recorded.
        entry = self.record.update(devices.FOREGROUND, owner=self.owner, revision=entry["revision"], activeRun=True,
                                   hostProcessIdentifier=os.getpid())
        self.record.update(devices.FOREGROUND, owner=self.owner, revision=entry["revision"], voiceOverOriginal={
            "enabled": False, **{name: entry[name] for name in ("reservedAt", "hostProcessIdentifier",
                                                                "hostProcessStart")}})
        self.enabled = False
        self.actions = []
        # Public reads still check the real lease, but their state comes from this offline fixture.
        self.enterContext(patch.object(state, "_read", self.read))

    def read(self):
        self.actions.append(("read", self.enabled))
        return self.enabled

    def toggle(self):
        self.actions.append(("toggle", not self.enabled))
        self.enabled = not self.enabled

    def session_leaves_voiceover_on(self):
        """A session's own preserved() scope, in a context of its own as in its own process, whose body turns
        VoiceOver on and whose restoration cannot send the key: it marks the lease uncertain and exits."""
        def cannot_toggle(): raise OSError("System Events refused the key")
        def session():
            with self.assertRaises(state.TransitionFailed):
                with state.preserved(self.owner, record=self.record, read_state=self.read, toggle=cannot_toggle):
                    self.enabled = True
        contextvars.Context().run(session)
        self.actions.clear()
        entry = self.record.entry(devices.FOREGROUND)
        self.assertEqual(devices.condition(entry), "uncertain")
        self.assertIs(entry["voiceOverRestoration"]["verified"], False)
        # What the next session meets today: every read under the lease refuses.
        with self.assertRaisesRegex(devices.Refused, "uncertain"):
            state.read(self.owner, record=self.record)
        return entry

    def recover(self, **options):
        options.setdefault("read_state", self.read)
        options.setdefault("toggle", self.toggle)
        return state.recover_restoration(self.owner, record=self.record, by="the run's session runner", **options)

    def edit(self, **fields):
        """Change the entry's fields in place, at its revision, as no record operation would: the state a guard
        must refuse."""
        with self.record._exclusive():
            value = self.record.load()
            value["devices"][devices.FOREGROUND].update(fields)
            self.record._replace(value)

    def assert_refused_untouched(self, pattern):
        before = self.record.entry(devices.FOREGROUND)
        with self.assertRaisesRegex(devices.Refused, pattern):
            self.recover()
        self.assertEqual(self.actions, [])
        self.assertTrue(self.enabled)
        self.assertEqual(self.record.entry(devices.FOREGROUND), before)

    def test_the_run_restores_its_recorded_original_and_its_next_session_may_start(self):
        self.session_leaves_voiceover_on()
        self.assertIs(self.recover(), False)
        self.assertFalse(self.enabled)
        self.assertEqual(self.toggles(), [False])
        entry = self.record.entry(devices.FOREGROUND)
        self.assertEqual(devices.condition(entry), "active")
        restoration = entry["voiceOverRestoration"]
        self.assertEqual((restoration["verified"], restoration["recoveredBy"], restoration["original"]),
                         (True, "the run's session runner", False))
        self.assertIsNotNone(devices.moment(restoration["at"]))
        self.assertIn("not verified", restoration["reason"])
        # The next session can read the restored state under the same lease.
        self.actions.clear()
        self.assertIs(state.read(self.owner, record=self.record), False)
        self.assertEqual(self.actions, [("read", False)])
        # And the run's end releases as usual.
        entry = self.record.update(devices.FOREGROUND, owner=self.owner, revision=entry["revision"], activeRun=False)
        self.record.release(devices.FOREGROUND, owner=self.owner, revision=entry["revision"], outcome="ended",
                            restoration="verified", frontmost="restored")

    def toggles(self):
        return [value for action, value in self.actions if action == "toggle"]

    def test_a_lease_whose_identity_changed_is_refused(self):
        self.session_leaves_voiceover_on()
        # The original was read under another reservation of the same owner name.
        self.edit(reservedAt=(datetime.now(timezone.utc) - timedelta(hours=2)).isoformat())
        self.assert_refused_untouched("recorded under another lease")

    def test_a_lease_replaced_during_restoration_stops_before_any_further_key(self):
        self.session_leaves_voiceover_on()
        def replaced_then_toggle():
            self.edit(reservedAt=datetime.now(timezone.utc).isoformat())
            self.toggle()
        with self.assertRaisesRegex(devices.Refused, "lease changed"):
            self.recover(toggle=replaced_then_toggle)
        # The key already sent stands; nothing is read or sent under the replaced lease.
        self.assertEqual(self.toggles(), [False])
        self.assertEqual(devices.condition(self.record.entry(devices.FOREGROUND)), "uncertain")

    def test_a_run_whose_host_process_ended_is_refused(self):
        entry = self.session_leaves_voiceover_on()
        # The run's process ended: its PID now has another start, though the lease and its original are the same.
        with patch.object(devices, "process_start", return_value="a start of another process"):
            self.assert_refused_untouched("no longer alive")
        # A run no longer marked active is no run to recover for.
        self.edit(activeRun=False)
        self.assert_refused_untouched("no longer alive")

    def test_any_other_uncertainty_is_refused(self):
        self.session_leaves_voiceover_on()
        entry = self.record.entry(devices.FOREGROUND)
        # Written after the marker: a run whose termination is unconfirmed.
        self.record.update(devices.FOREGROUND, owner=self.owner, revision=entry["revision"], uncertain=True,
                           run="unconfirmed process termination")
        self.assert_refused_untouched("changed after")
        self.edit(uncertainReason="imported records hold this device for different runs")
        self.assert_refused_untouched("not only a VoiceOver restoration")

    def test_uncertainty_without_a_restoration_marker_or_an_original_is_refused(self):
        self.enabled = True
        entry = self.record.entry(devices.FOREGROUND)
        self.record.update(devices.FOREGROUND, owner=self.owner, revision=entry["revision"], uncertain=True)
        self.assert_refused_untouched("not an unverified VoiceOver restoration")
        self.setUp()
        self.session_leaves_voiceover_on()
        self.edit(voiceOverOriginal=None)
        self.assert_refused_untouched("no original")

    def test_another_owner_is_refused(self):
        self.session_leaves_voiceover_on()
        with self.assertRaisesRegex(devices.Refused, "not held by"):
            state.recover_restoration("another run", record=self.record, read_state=self.read, toggle=self.toggle)
        self.assertEqual(self.actions, [])

    def test_a_restoration_that_fails_stays_uncertain_and_is_not_tried_again(self):
        self.session_leaves_voiceover_on()
        def fails(): raise OSError("System Events refused the key")
        with self.assertRaisesRegex(state.TransitionFailed, "stays uncertain"):
            self.recover(toggle=fails)
        self.assertTrue(self.enabled)
        entry = self.record.entry(devices.FOREGROUND)
        self.assertEqual(devices.condition(entry), "uncertain")
        self.assertIn("refused the key", entry["voiceOverRestoration"]["recoveryFailed"]["reason"])
        self.actions.clear()
        self.assert_refused_untouched("already failed")

    def test_a_restoration_that_does_not_hold_is_not_verified(self):
        # A late key turns VoiceOver on again during every settle window.
        self.session_leaves_voiceover_on()
        def late():
            self.actions.append(("read", self.enabled))
            value, self.enabled = self.enabled, True
            return value
        with self.assertRaises(state.TransitionFailed):
            self.recover(read_state=late)
        self.assertEqual(devices.condition(self.record.entry(devices.FOREGROUND)), "uncertain")


if __name__ == "__main__": unittest.main()
