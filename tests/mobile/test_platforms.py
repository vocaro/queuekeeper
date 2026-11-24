"""The shared mobile device code asks each platform switched on about its own devices (queuekeeper_mobile.platforms),
and packs shards the same way for every platform (queuekeeper_mobile.shards)."""
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import host_isolation  # noqa: F401
from queuekeeper_mobile import devices, platforms, reach, shards


class Fake(platforms.Platform):
    """A platform whose physical devices are named FAKE-N and whose simulators sim:N, case and all."""
    name = "fake"
    paired = ("FakePhone",)
    identifier_name = "fake serial"

    def __init__(self):
        self.silenced, self.restored, self.watched = [], [], []

    def physical(self, identifier):
        return isinstance(identifier, str) and identifier.startswith("FAKE-")

    def virtual(self, identifier):
        return isinstance(identifier, str) and identifier.startswith("sim:")

    def named(self, text, root=None):
        return {word for word in text.split() if word.startswith("FAKE-") and word[5:].isdigit()}

    def silence(self, record, key, entry, value, *, owner, revision, said):
        self.silenced.append(key)
        return record.notifications(key, owner=owner, revision=revision,
                                    value={"silence": "requested", "restore": "pending", "confirmed": True})

    def restore(self, key, entry, said):
        self.restored.append(key)
        return {"restore": "done", "restoreDetail": "put back"}

    def watch(self, identifier):
        self.watched.append(identifier)
        return platforms.Unwatched("the fake platform reads no log")

    def listing_here(self):
        return {"FAKE-1": {"way": "cable", "transport": "usb", "kind": "fake", "model": None, "name": None}}


def later(minutes=30):
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


class Registry(unittest.TestCase):
    def setUp(self):
        self.fake = Fake()
        patcher = patch.object(platforms, "PLATFORMS", [self.fake])
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_an_identifier_belongs_to_the_platform_that_takes_it_and_nothing_else_is_known(self):
        self.assertIs(platforms.of("FAKE-1"), self.fake)
        self.assertIs(platforms.of("sim:2"), self.fake)
        self.assertIs(platforms.of("iPhone"), platforms.NONE)
        self.assertTrue(platforms.physical("FAKE-1"))
        self.assertFalse(platforms.physical("sim:2"))
        self.assertTrue(platforms.known("sim:2"))
        self.assertFalse(platforms.known(None))
        self.assertEqual(platforms.paired(), ("FakePhone",))
        self.assertEqual(platforms.identifier_names(), "fake serial")

    def test_registering_a_platform_twice_switches_it_on_once(self):
        platforms.register(Fake())
        self.assertEqual(len(platforms.PLATFORMS), 1)

    def test_together_counts_every_watch_and_says_which_was_not_read(self):
        class Seen:
            def __init__(self, seen): self.seen = seen
            def stop(self): return self.seen
        together = platforms.Together({"a": Seen({"watched": True, "presented": 1, "first": "10:02"}),
                                       "b": Seen({"watched": False, "presented": 2, "first": "10:01",
                                                  "detail": "lost"})})
        seen = together.stop()
        self.assertEqual((seen["watched"], seen["presented"], seen["first"], seen["detail"]),
                         (False, 3, "10:01", "b: lost"))

    def test_the_base_platform_says_what_it_cannot_do(self):
        base = platforms.Platform()
        self.assertEqual(base.watch("x").stop()["watched"], False)
        self.assertIn("cannot put them back", base.restore("k", {}, print)["restoreDetail"])
        with self.assertRaises(ValueError): base.setup("k", {})


class Record(unittest.TestCase):
    def setUp(self):
        self.fake = Fake()
        patcher = patch.object(platforms, "PLATFORMS", [self.fake])
        patcher.start()
        self.addCleanup(patcher.stop)
        self.temp = tempfile.TemporaryDirectory(dir=os.environ["QUEUEKEEPER_DEVICE_RECORD"].rsplit("/devices/", 1)[0])
        self.addCleanup(self.temp.cleanup)
        self.record = devices.Record(Path(self.temp.name) / "record.json")

    def test_the_record_takes_only_identifiers_a_platform_owns_in_their_canonical_form(self):
        with self.assertRaisesRegex(ValueError, "fake serial"):
            self.record.reserve("phone", owner="a", revision=0, purpose="p", release_by=later(), device="iPhone")
        entry = self.record.reserve("phone", owner="a", revision=0, purpose="p", release_by=later(), device="FAKE-1")
        self.assertEqual(entry["device"], "FAKE-1")

    def test_a_command_that_names_a_device_the_run_does_not_hold_is_refused(self):
        self.record.reserve("phone", owner="a", revision=0, purpose="p", release_by=later(), device="FAKE-1")
        self.record.reserve("other", owner="b", revision=0, purpose="p", release_by=later(), device="FAKE-2")
        with self.assertRaisesRegex(devices.Refused, "FAKE-2, other's device"):
            devices.BoundedRun(["phone"], "a", ["run", "--on", "fake-2"], record=self.record)
        with self.assertRaisesRegex(devices.Refused, "FAKE-3"):
            devices.BoundedRun(["phone"], "a", ["run", "FAKE-3"], record=self.record)
        devices.BoundedRun(["phone"], "a", ["run", "FAKE-1"], record=self.record)

    def test_a_bounded_run_is_watched_by_its_platform_and_records_its_outcome(self):
        self.record.reserve("phone", owner="a", revision=0, purpose="p", release_by=later(), device="FAKE-1")
        run = devices.BoundedRun(["phone"], "a", [sys.executable, "-c", "pass"], record=self.record)
        said = []
        code = devices.watched_run(run, "phone", [sys.executable, "-c", "pass"], {}, said.append)
        self.assertEqual(code, 0)
        self.assertEqual(self.fake.watched, ["FAKE-1"])
        entry = self.record.entry("phone")
        self.assertEqual((entry["activeRun"], entry["run"]["outcome"]), (False, "exit 0"))
        self.assertIn("could not be read", said[0])

    def test_silencing_and_restoring_go_to_the_device_s_platform_once_each(self):
        entry = self.record.reserve("phone", owner="a", revision=0, purpose="p", release_by=later(), device="FAKE-1")
        entry = devices.silence_notifications(self.record, "phone", owner="a", revision=entry["revision"], said=print)
        devices.silence_notifications(self.record, "phone", owner="a", revision=entry["revision"], said=print)
        self.assertEqual(self.fake.silenced, ["phone"])
        entry = devices.restore_notifications(self.record, "phone", owner="a", revision=entry["revision"], said=print)
        self.assertEqual(self.fake.restored, ["phone"])
        self.assertEqual(entry["notifications"]["restore"], "done")
        self.assertIn("restoredAt", entry["notifications"])

    def test_take_waits_only_for_a_platform_s_paired_keys(self):
        with self.assertRaisesRegex(ValueError, "FakePhone"):
            self.record.take("tablet", owner="a", purpose="p", release_by=later(), timeout=1)

    def test_a_pool_release_waits_for_a_confirmed_stop_of_the_unchanged_reservation(self):
        entry = self.record.reserve("Pool Kind 1", owner="a", revision=0, purpose="p", release_by=later(),
                                    device="sim:1")
        stops, kept = [], []

        def release(owner="a", unchanged=lambda current: True, stopped=True):
            def stop(current):
                stops.append(current["revision"])
                return None if stopped else "it still runs"
            return devices.release_confirmed(self.record, "Pool Kind 1", owner=owner, outcome="done",
                                             restoration="Confirmed it stopped", unchanged=unchanged, stop=stop,
                                             kept=kept.append)

        # Another owner's release, or one the platform says no longer matches the lease, stops nothing.
        self.assertFalse(release(owner="b"))
        self.assertFalse(release(unchanged=lambda current: False))
        self.assertEqual(stops, [])
        # An unconfirmed stop keeps the reservation for recovery.
        self.assertFalse(release(stopped=False))
        self.assertEqual(devices.condition(self.record.entry("Pool Kind 1")), "held")
        self.assertEqual([str(error) for error in kept],
                         ["the reservation changed", "the reservation changed", "it still runs"])
        self.assertTrue(release())
        released = self.record.entry("Pool Kind 1")
        self.assertEqual(stops, [entry["revision"], entry["revision"]])
        self.assertEqual((devices.condition(released), released["run"]), ("released", "done"))

    def test_reach_merges_every_platform_s_listing(self):
        found, _seconds = reach.read_here()
        self.assertEqual(set(found), {"FAKE-1"})


class PoolReservations(unittest.TestCase):
    def test_a_race_skips_one_member_and_reserves_only_as_many_as_requested(self):
        reserved = []

        def reserve(candidate):
            reserved.append(candidate)
            if candidate == "first":
                raise devices.Refused("another run got there first")
            return len(reserved)

        candidates = devices.reserve_candidates(["first", "second", "third"], reserve)
        self.assertEqual(next(candidates), ("second", 2))
        self.assertEqual(reserved, ["first", "second"])
        self.assertEqual(list(candidates), [("third", 3)])

    def test_a_reset_runs_under_each_reservation_and_a_stop_in_it_abandons_that_member(self):
        reset, abandoned = [], []

        def reset_member(lease):
            reset.append(lease)
            if lease[0] == "third": raise KeyboardInterrupt
            return lease[0] != "first"

        def reserve(candidate):
            if candidate == "second": raise devices.Refused("another run got there first")
            return len(reset) + 1

        members = devices.reset_candidates(["first", "second", "third", "fourth"], reserve,
                                           lambda candidate, revision: (candidate, revision), reset_member,
                                           abandon=abandoned.append)
        # The caller reads each result and passes over a member whose reset said no.
        self.assertEqual(next(members), (("first", 1), False))
        with self.assertRaises(KeyboardInterrupt):
            next(members)
        self.assertEqual((reset, abandoned), ([("first", 1), ("third", 2)], [("third", 2)]))

    def test_a_new_member_is_made_under_the_lowest_free_number_and_a_failure_abandons_its_name(self):
        abandoned = []

        def create(key, revision):
            if key == "Pool 3": raise RuntimeError("the clone failed")
            return key, revision

        made = devices.create_numbered("Pool ", lambda key: key != "Pool 1", lambda key: 7, create,
                                       abandon=lambda *args: abandoned.append(args))
        self.assertEqual(made, ("Pool 2", 7))
        with self.assertRaisesRegex(RuntimeError, "the clone failed"):
            devices.create_numbered("Pool ", lambda key: key not in ("Pool 1", "Pool 2"), lambda key: 8, create,
                                    abandon=lambda *args: abandoned.append(args))
        self.assertEqual([(key, revision, str(error)) for key, revision, error in abandoned],
                         [("Pool 3", 8, "the clone failed")])


class PoolTemplates(unittest.TestCase):
    def test_a_waiter_tries_again_each_poll_while_another_owner_holds_the_template(self):
        tries, slept = [], []

        def attempt():
            tries.append(len(slept))
            if len(tries) < 3: raise devices.Held("Pool template is held by another session")
            return "reserved"

        self.assertEqual(devices.await_template(attempt, wait=10, poll=2, sleep=slept.append), "reserved")
        self.assertEqual((tries, slept), ([0, 1, 2], [2, 2]))

    def test_a_waiter_gives_up_past_its_wait_by_the_polls_slept_or_by_its_clock(self):
        slept = []

        def held():
            raise devices.Held("Pool template is held by another session")

        with self.assertRaisesRegex(devices.Held, "held by another session") as raised:
            devices.await_template(held, wait=6, poll=2, sleep=slept.append)
        self.assertEqual((raised.exception.waited, slept), (6, [2, 2, 2]))
        # No wait at all: one look, no sleep.
        slept.clear()
        with self.assertRaises(devices.Held) as raised:
            devices.await_template(held, wait=0, poll=2, sleep=slept.append)
        self.assertEqual((raised.exception.waited, slept), (0, []))
        # Measured by a clock, the time spent in each attempt counts too.
        now = [100.0]

        def slow():
            now[0] += 4
            raise devices.Held("still warming")

        with self.assertRaises(devices.Held) as raised:
            devices.await_template(slow, wait=10, poll=5, sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
                                   clock=lambda: now[0])
        self.assertEqual(raised.exception.waited, 13)

    def test_a_template_is_found_or_made_warmed_and_recorded_under_its_reservation(self):
        steps = []

        def step(name, result=None):
            return lambda *args: steps.append((name, *args)) or result

        made = devices.prepare_template("lease", find=step("find"), create=step("create"), warm=step("warm", "warmth"),
                                        ready=step("ready", "ready"), abandon=step("abandon"))
        self.assertEqual(made, "ready")
        self.assertEqual(steps, [("find", "lease"), ("create", "lease"), ("warm", "lease"),
                                 ("ready", "lease", "warmth")])
        # A template ready already is handed back as it is.
        steps.clear()
        self.assertEqual(devices.prepare_template("lease", find=step("find", "current"), create=step("create"),
                                                  warm=step("warm"), ready=step("ready"), abandon=step("abandon")),
                         "current")
        self.assertEqual(steps, [("find", "lease")])

    def test_a_step_that_stops_short_keeps_its_own_release_and_any_other_failure_abandons_the_template(self):
        abandoned = []

        def unprepared(_lease):
            raise devices.Unprepared("the template was warmed, but its shutdown could not be confirmed")

        with self.assertRaisesRegex(devices.Unprepared, "shutdown could not be confirmed"):
            devices.prepare_template("lease", create=lambda lease: None, warm=lambda lease: {}, ready=lambda *_: None,
                                     find=unprepared, abandon=lambda *args: abandoned.append(args))
        self.assertEqual(abandoned, [])

        def stopped(_lease):
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            devices.prepare_template("lease", create=lambda lease: None, warm=stopped, ready=lambda *_: None,
                                     abandon=lambda *args: abandoned.append(args))
        self.assertEqual([(lease, type(error)) for lease, error in abandoned], [("lease", KeyboardInterrupt)])


class Shards(unittest.TestCase):
    def test_units_go_longest_first_into_the_emptiest_shard_and_unknown_ones_count_the_median(self):
        packed = shards.packed(["a", "b", "c", "d", "e"], {"a": 10, "b": 6, "c": 4, "d": 1}, 2)
        self.assertEqual(packed, [["a", "c"], ["b", "e", "d"]])

    def test_a_count_of_one_keeps_every_unit_together_and_no_timings_pack_by_name(self):
        self.assertEqual(shards.packed(["b", "a"], {}, 1), [["a", "b"]])
        self.assertEqual(shards.packed(["b", "a", "c"], {}, 2), [["a", "c"], ["b"]])

    def test_timings_are_kept_and_merged_newest_over_oldest(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "t" / "timings.json"
            self.assertEqual(shards.read_timings(path), {})
            shards.keep_timings(path, {"a": 1.0, "b": 2.0})
            shards.keep_timings(path, {"b": 3.0})
            self.assertEqual(shards.read_timings(path), {"a": 1.0, "b": 3.0})


if __name__ == "__main__":
    unittest.main()
