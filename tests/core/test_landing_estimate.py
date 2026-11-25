"""Landing estimates from the queue's own passing rounds (queuekeeper.landing_estimate, vocaro/queuekeeper#49), by fake
histories: no queue, worker or Mac is involved."""
import unittest

import host_isolation  # noqa: F401 -- first, as every test module
from queuekeeper import landing_estimate as estimate

CONFIG = {"lanes": {"preflight": {"command": ["true"]}, "gate": {"command": ["qk", "unittest"]},
                    "docs": {"command": ["qk", "docs", "check"]}}}
CODE = estimate.workload(CONFIG, ["preflight", "gate"])
DOCS = estimate.workload(CONFIG, ["docs"])
NOW = 10_000.0


def passed(load, mac, seconds, at):
    return {"kind": estimate.KIND, "time": at, "roundSeconds": seconds, "workload": load, "mac": mac}


def history(load, mac, seconds, start=0):
    """A journal's passing rounds of load on mac, one per value of seconds, oldest first."""
    return [passed(load, mac, value, start + index) for index, value in enumerate(seconds)]


def entry(identifier, state="queued", **options):
    return {"id": identifier, "state": state, **options}


def running(entries, started=NOW - 100, load=CODE, lane="gate", mac="this Mac", **more):
    return {"round": "r1", "started": started, "entries": entries, "workload": load,
            "gates": {"batch": {"lane": lane, "mac": mac, "entries": entries}}, **more}


def estimates(state, waiting, events, kind_of=None, marks=None):
    loads = {"code": CODE["id"], "docs": DOCS["id"], "ungated": None}
    return estimate.estimates(state, waiting, estimate.samples(events), lambda kind, batch: loads[kind], kind_of,
                              at=NOW, marks=marks)


class Workload(unittest.TestCase):
    def test_a_workload_changes_with_its_lanes_their_members_and_their_definitions(self):
        self.assertEqual(CODE, estimate.workload(CONFIG, ["preflight", "gate"]))
        self.assertNotEqual(CODE["id"], estimate.workload(CONFIG, ["gate"])["id"])
        self.assertNotEqual(CODE["id"], estimate.workload(CONFIG, ["preflight", "gate"], {"gate": ["a"]})["id"])
        changed = {"lanes": {**CONFIG["lanes"], "gate": {"command": ["qk", "unittest", "-s", "tests"]}}}
        self.assertNotEqual(CODE["id"], estimate.workload(changed, ["preflight", "gate"])["id"])
        unrelated = {"lanes": {**CONFIG["lanes"], "release": {"command": ["make"]}}}
        self.assertEqual(CODE["id"], estimate.workload(unrelated, ["preflight", "gate"])["id"])

    def test_this_mac_is_one_destination_however_a_record_spells_it(self):
        for said in ("this Mac", " on this Mac (desk)", "this Mac (desk)"):
            self.assertEqual(estimate.destination(said), estimate.HERE)
        self.assertEqual(estimate.destination(" on studio"), "studio")
        self.assertIsNone(estimate.destination(""))


class History(unittest.TestCase):
    def test_rounds_are_compared_only_by_workload_and_mac(self):
        events = [*history(CODE, "this Mac", [100] * 5), *history(CODE, "studio", [400] * 5),
                  *history(DOCS, "this Mac", [5] * 5),
                  {"kind": estimate.KIND, "time": 1, "entries": ["old"]}]  # a pass recorded before rounds were timed
        found = estimate.history(estimate.samples(events), CODE["id"])
        self.assertEqual({mac: (value["median"], value["p90"], value["samples"]) for mac, value in found.items()},
                         {"this Mac": (100, 100, 5), "studio": (400, 400, 5)})

    def test_too_few_rounds_and_old_rounds_make_no_history(self):
        events = history(CODE, "this Mac", [100] * (estimate.FEWEST - 1))
        self.assertEqual(estimate.history(estimate.samples(events), CODE["id"]), {})
        events = history(CODE, "this Mac", [999] * 30 + [100] * estimate.RECENT)
        self.assertEqual(estimate.history(estimate.samples(events), CODE["id"])["this Mac"]["p90"], 100)

    def test_a_stale_mark_on_any_of_the_lanes_leaves_out_the_rounds_before_it(self):
        events = history(CODE, "this Mac", [100] * 5) + history(CODE, "this Mac", [50] * 5, start=100)
        found = estimate.history(estimate.samples(events), CODE["id"], marks={"gate": 50})
        self.assertEqual(found["this Mac"]["median"], 50)
        self.assertEqual(estimate.history(estimate.samples(events), CODE["id"], marks={"*": 200}), {})


class Estimates(unittest.TestCase):
    def test_the_running_round_gives_its_remaining_range_on_its_own_mac(self):
        events = history(CODE, "this Mac", [300, 320, 340, 360, 600]) + history(CODE, "studio", [900] * 5)
        found = estimates(running(["a"]), [entry("a", "testing")], events)["a"]
        self.assertEqual((found["low"], found["high"], found["rounds"]), (240, 500, 1))
        self.assertIn("5 passing rounds on this Mac", estimate.words(found, at=NOW))

    def test_a_round_not_yet_on_its_last_lanes_mac_takes_the_widest_range_of_every_mac(self):
        events = history(CODE, "this Mac", [300] * 5) + history(CODE, "studio", [900] * 5)
        state = running(["a"], lane="preflight")
        found = estimates(state, [entry("a", "testing")], events)["a"]
        self.assertEqual((found["low"], found["high"]), (200, 800))

    def test_entries_behind_add_the_rounds_ahead_of_them_by_the_queues_batching(self):
        events = history(CODE, "this Mac", [300] * 5)
        waiting = [entry("a", "testing"), entry("b"), entry("c"), entry("d", noGate=True), entry("e")]
        loads = {"code": CODE["id"], "ungated": DOCS["id"]}
        events += history(DOCS, "this Mac", [10] * 5)
        found = estimate.estimates(running(["a"]), waiting, estimate.samples(events),
                                   lambda kind, batch: loads[kind],
                                   lambda batch, alone, left: ("ungated" if batch[0].get("noGate") else "code", batch),
                                   at=NOW)
        # b and c ride one batch, d lands alone without the gate, then e.
        self.assertEqual([(found[key]["low"], found[key]["high"], found[key]["rounds"]) for key in "abcde"],
                         [(200, 200, 1), (500, 500, 2), (500, 500, 2), (510, 510, 3), (810, 810, 4)])

    def test_a_front_entry_goes_first_and_a_passed_batch_is_pushed_first(self):
        events = history(CODE, "this Mac", [300] * 5)
        waiting = [entry("p", "passed"), entry("b"), entry("f", front=True)]
        found = estimates({"round": None, "gates": {}}, waiting, events)
        self.assertEqual([(found[key]["low"], found[key]["rounds"]) for key in "pfb"], [(0, 1), (300, 2), (600, 3)])

    def test_unknown_history_is_said_and_never_guessed(self):
        found = estimates(running(["a"]), [entry("a", "testing"), entry("b")], [])
        self.assertIn("fewer than 5 comparable passing rounds on this Mac", found["a"]["unknown"])
        self.assertEqual(found["b"], found["a"])
        # The running round known, a round ahead of another workload with no history is unknown too.
        events = history(CODE, "this Mac", [300] * 5)
        found = estimates(running(["a"]), [entry("a", "testing"), entry("b")], events,
                          kind_of=lambda batch, alone, left: ("docs", batch))
        self.assertIn("low", found["a"])
        self.assertEqual(found["b"], {"unknown": "a docs round ahead has fewer than 5 comparable passing rounds on any Mac"})
        self.assertTrue(estimate.words(found["b"]).startswith("landing estimate unknown: "))

    def test_a_round_past_its_p90_a_bisect_a_failure_or_a_stack_is_not_estimated(self):
        events = history(CODE, "this Mac", [300] * 5)
        late = estimates(running(["a"], started=NOW - 400), [entry("a", "testing")], events)["a"]
        self.assertIn("past the p90", late["unknown"])
        bisect = estimates(running(["a"], bisect={"suspects": 4}), [entry("a", "testing")], events)["a"]
        self.assertIn("bisect", bisect["unknown"])
        failed = estimates(running(["a"], failure={"lane": "gate"}), [entry("a", "testing")], events)["a"]
        self.assertIn("gate failed", failed["unknown"])
        state = running(["a"])
        state["gates"]["stacked"] = {"lane": "gate", "entries": ["s"]}
        found = estimates(state, [entry("a", "testing"), entry("s", "testing"), entry("b")], events)
        self.assertIn("stacked", found["s"]["unknown"])
        self.assertEqual(found["b"]["rounds"], 2)
        self.assertEqual(estimates(None, [entry("a")], events)["a"], {"unknown": "no worker runs"})
        no_load = running(["a"], load=None)
        self.assertIn("no workload", estimates(no_load, [entry("a", "testing")], events)["a"]["unknown"])


if __name__ == "__main__":
    unittest.main()
