"""Offline state contracts: all state reads/toggles are fakes, with the real lease record."""
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/python"))
from vocaro_engineering import devices, foreground, processes
from vocaro_engineering.voiceover import guard, state


class PublicReadTests(unittest.TestCase):
    def test_only_exact_public_boolean_output_is_accepted(self):
        for output, expected in [("true\n", True), ("false\n", False)]:
            with self.subTest(output), patch.object(state.subprocess, "run", return_value=
                    subprocess.CompletedProcess([], 0, output)) as command:
                self.assertIs(state._read(), expected)
                self.assertEqual(command.call_args.args[0][:3], ["osascript", "-l", "JavaScript"])
                self.assertIn("isVoiceOverEnabled", command.call_args.args[0][-1])
                self.assertEqual(command.call_args.kwargs["timeout"], state.WAIT)
        for output in ['"true"', "1", "0", "null", "undefined", "", "false\nwarning", "{}"]:
            with self.subTest(output), patch.object(state.subprocess, "run", return_value=
                    subprocess.CompletedProcess([], 0, output)), self.assertRaises(state.UnknownState):
                state._read()
        for error in [OSError("missing"), subprocess.TimeoutExpired("osascript", state.WAIT)]:
            with self.subTest(error), patch.object(state.subprocess, "run", side_effect=error), \
                    self.assertRaises(state.UnknownState): state._read()
        with patch.object(state.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "true")), \
                self.assertRaises(state.UnknownState): state._read()


class ReadinessQueryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.record = devices.Record(Path(temporary.name) / "record.json")
        self.owner = "offline readiness query"
        self.entry = self.record.reserve(devices.FOREGROUND, owner=self.owner, revision=0, purpose="offline test",
            release_by=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            approval={**foreground.STANDING, "until": "released"})
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
                                purpose="new holder", release_by=entry["expectedReleaseBy"], approval=entry["approval"])
            return subprocess.CompletedProcess([], 0, "123", "")
        with patch.object(state.subprocess, "run", side_effect=replaced) as command, \
                self.assertRaises(devices.Refused): state._ready_to_enable(self.lease)
        self.assertEqual(command.call_count, 1)


class StateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.record = devices.Record(Path(temporary.name) / "record.json")
        self.owner = "owned offline contract"
        self.entry = self.record.reserve(devices.FOREGROUND, owner=self.owner, revision=0, purpose="offline test",
            release_by=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            approval={**foreground.STANDING, "until": "released"})
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

    def test_applied_setup_toggle_that_reports_failure_still_restores_then_retains_uncertainty(self):
        self.enabled = True
        def applied_then_error():
            self.toggle()
            if not self.enabled: raise OSError("lost toggle reply after application")
        with self.assertRaises(state.TransitionFailed):
            with self.scope(target=False, toggle=applied_then_error): self.fail("failed setup ran body")
        self.assertTrue(self.enabled)
        self.assertEqual(self.toggles(), [False, True])
        self.assert_uncertain()

    def test_public_setter_applied_error_uses_local_preserved_restoration_provenance(self):
        self.enabled = False
        def applied_then_error():
            self.toggle()
            if self.enabled: raise OSError("reply lost after enable")
        with patch.object(state, "_read", self.read), patch.object(state, "_toggle", applied_then_error):
            with self.assertRaises(state.TransitionFailed):
                with self.scope(): state.set_enabled(True, self.owner, record=self.record)
        self.assertFalse(self.enabled)
        self.assertEqual(self.toggles(), [True, False])
        self.assert_uncertain()
        # Leaving the scope drops its restoration provenance: a new client cannot adopt it.
        self.actions.clear()
        with self.assertRaises(devices.Refused):
            with self.scope(): self.fail("uncertainty adopted by a later scope")
        self.assertEqual(self.actions, [])


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
                                purpose="new test holder", release_by=entry["expectedReleaseBy"], approval=entry["approval"])
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
                                    purpose="new same-name lease", release_by=entry["expectedReleaseBy"],
                                    approval=entry["approval"])
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
            with patch.object(devices, "require_window", side_effect=devices.Refused("expired")):
                with self.assertRaises(devices.Refused): state.read(self.owner, record=self.record)
                # Restoration bypasses only the approval deadline, not lease ownership.
                # Keep patch through __exit__ using an independently entered context below.
        self.assertTrue(self.enabled)
        scope = self.scope(target=False)
        scope.__enter__()
        with patch.object(devices, "require_window", side_effect=devices.Refused("expired")):
            scope.__exit__(None, None, None)
        self.assertTrue(self.enabled)


if __name__ == "__main__": unittest.main()
