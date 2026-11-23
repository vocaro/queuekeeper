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
