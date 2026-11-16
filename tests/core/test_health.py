"""health: the queue's verdict, whose good is only ever confirmed and whose unknown is never good."""
import contextlib
from io import StringIO
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src/python"))
import host_isolation  # noqa: F401 -- isolates this module's host state as every test module's
from queuekeeper import health

NOW = 1_800_000_000.0
USUAL = 900  # the median of the passing gate runs in JOURNAL


def journal(passing=6, landed=(NOW - 600,), back=0):
    """Passing gate runs of USUAL seconds, entries landed at the times given, and then `back` entries that came back."""
    events = [{"kind": "gate.ended", "lane": "gate", "status": 0, "seconds": USUAL, "time": NOW - 7200 + index}
              for index in range(passing)]
    events += [{"kind": "entry.landed", "time": at} for at in landed]
    events += [{"kind": "entry.failed", "time": NOW - 300 + index} for index in range(back)]
    return events


def state(gate=None, waiting=1, held=None, worker=True):
    """A queue with `waiting` entries, one of them in a round whose batch gate is `gate`, when one is given."""
    entries = [{"id": f"e{index}", "state": "testing" if index == 0 and gate else "queued", "submitted": NOW - 7200}
               for index in range(waiting)]
    found = {"worker": worker, "held": held, "waiting": entries, "round": None}
    if gate is not None:
        found["round"] = {"round": "r1", "started": NOW - 700, "entries": ["e0"], "gates": {"batch": {
            "lane": "gate", "mac": "build-host", "log": "/log", **gate}}}
    return found


def macs(memory="normal", second=None, read_at=NOW - 5):
    """This Mac's reading, and another Mac's: well, or the reading `second` gives."""
    well = {"memory": memory, "load": 3.0, "cpuBusy": 20, "busyLimit": 85, "error": None}
    return [{"name": "this Mac", "reading": well, "readAt": read_at},
            {"name": "build-host", "reading": second if second is not None else well, "readAt": read_at}]


DEVICES = {"devices": [{"name": "Test iPad", "connection": "connected", "state": "released"}], "error": None,
           "connectionError": None, "deferredTo": None}


def assess(found, events=None, readings="well", record=DEVICES, findings=(), log_changed=NOW - 20):
    return health.assess(found, journal() if events is None else events, now=NOW,
                         readings=macs() if readings == "well" else readings, record=record, findings=list(findings),
                         modified=lambda path: log_changed)


def levels(found):
    return {item["name"]: item["level"] for item in found["signs"]}


class Verdict(unittest.TestCase):
    def test_a_moving_round_with_every_sign_confirmed_is_good_and_forecast(self):
        found = assess(state({"began": NOW - 300}, waiting=2))
        self.assertEqual((found["verdict"], found["exitStatus"]), ("good", health.HEALTHY), found["sentence"])
        self.assertEqual(levels(found), {"progress": "ok", "pace": "ok", "macs": "ok", "devices": "ok", "cameBack": "ok"})
        # What is left of the usual gate, then one more round for the entry not in this one.
        self.assertEqual(found["forecast"], {"waiting": 2, "clearsAt": round(NOW + (USUAL - 300) + USUAL)})
        self.assertIn("land by about", found["sentence"])

    def test_a_reading_that_is_missing_failed_or_stale_is_never_good(self):
        moving = state({"began": NOW - 300})
        for why, readings in (("not read", None),
                              ("failed", macs(second={"error": "ssh: connect to host build-host: Operation timed out"})),
                              ("stale", macs(read_at=NOW - 600)),
                              ("pressure unread", macs(memory=None))):
            with self.subTest(why):
                found = assess(moving, readings=readings)
                self.assertEqual((found["verdict"], found["exitStatus"], found["label"]),
                                 ("unknown", health.UNKNOWN_STATUS, "Can't tell"), found["sentence"])
                self.assertEqual(levels(found)["macs"], "unknown")
                self.assertIn("may be fine", found["sentence"])
        # The devices too, when the round may need them.
        found = assess(moving, record=None)
        self.assertEqual((found["verdict"], levels(found)["devices"]), ("unknown", "unknown"))

    def test_too_few_gate_runs_to_know_the_usual_time_is_unknown_not_good(self):
        found = assess(state({"began": NOW - 300}), events=journal(passing=4))
        self.assertEqual((found["verdict"], levels(found)["progress"], levels(found)["pace"]), ("unknown", "unknown", "unknown"))
        self.assertIsNone(found["forecast"])
        self.assertEqual(assess(state({"began": NOW - 300}), events=journal(passing=5))["verdict"], "good")

    def test_a_definite_problem_outranks_an_unknown_one_and_names_it_too(self):
        found = assess(state({"began": NOW - 300}), readings=None, log_changed=NOW - health.SILENT - 60)
        self.assertEqual((found["verdict"], found["exitStatus"], levels(found)["progress"]), ("stuck", health.ATTENTION, "danger"))
        self.assertIn("has written nothing for 16m", found["sentence"])
        self.assertIn("What could not be checked: no Mac's reading was made", found["sentence"])

    def test_a_gate_whose_log_is_unchanged_while_its_run_moves_is_not_stuck(self):
        # `qk unittest` says its progress in its own folder, and the gate's log stays as it was until it ends.
        silent = NOW - health.SILENT - 60
        found = assess(state({"began": NOW - 800, "moved": NOW - 5}), log_changed=silent)
        self.assertEqual((found["verdict"], levels(found)["progress"]), ("good", "ok"))
        # Silence is since the later of the two, and a time that is no number, or is after now, moves nothing.
        found = assess(state({"began": NOW - 2400, "moved": NOW - health.SILENT - 240}), log_changed=NOW - 3000)
        self.assertEqual(found["verdict"], "stuck")
        self.assertIn("has written nothing for 19m", found["sentence"])
        for moved in (None, "now", float("nan")):
            self.assertEqual(assess(state({"began": NOW - 800, "moved": moved}), log_changed=silent)["verdict"], "stuck")
        self.assertEqual(assess(state({"began": NOW - 800, "moved": NOW + 9e9}), log_changed=silent)["verdict"], "good")

    def test_a_gate_past_its_usual_time_is_slow_with_a_later_forecast(self):
        found = assess(state({"began": NOW - 2 * USUAL}))
        self.assertEqual((found["verdict"], found["label"], levels(found)["progress"]), ("slow", "Slower than usual", "warning"))
        self.assertIn("is 30m in, 2.0× its usual 15m", found["sentence"])
        self.assertEqual(found["signs"][0]["short"], "30m in, usually 15m")
        self.assertEqual(found["forecast"]["clearsAt"], round(NOW + USUAL / 4))
        # The negative control: at its usual time it is moving.
        self.assertEqual(levels(assess(state({"began": NOW - USUAL})))["progress"], "ok")

    def test_a_long_wait_for_a_mac_is_slow_and_a_short_one_is_not(self):
        waiting = lambda seconds: state({"began": NOW - seconds, "waiting": "admission on build-host: "
                                        "all 3 slots are busy", "waitingSince": NOW - seconds})
        self.assertEqual(levels(assess(waiting(health.WAIT_LONG)))["progress"], "warning")
        self.assertEqual(levels(assess(waiting(120)))["progress"], "ok")

    def test_nothing_landed_for_three_gates_while_entries_wait_is_slow(self):
        found = assess(state(), events=journal(landed=(NOW - 3 * USUAL - health.STARTING - 60,)))
        self.assertEqual(levels(found)["pace"], "warning")
        self.assertEqual(levels(assess(state(), events=journal(landed=(NOW - 3 * USUAL,))))["pace"], "ok")

    def test_a_held_queue_or_one_with_no_worker_waits_on_someone(self):
        found = assess(state(held="for the owner: the pre-push hook changed"))
        self.assertEqual((found["verdict"], found["label"], found["exitStatus"]), ("held", "Waiting on you", health.ATTENTION))
        found = assess(state(worker=False))
        self.assertEqual((found["verdict"], found["label"]), ("stopped", "Stopped"))
        self.assertIn("`qk queue resume` starts one", found["sentence"])

    def test_a_quiet_queue_is_quiet_only_with_its_macs_confirmed(self):
        found = assess(state(waiting=0))
        self.assertEqual((found["verdict"], found["exitStatus"]), ("quiet", health.HEALTHY))
        self.assertIn("The last entry landed 10m ago", found["sentence"])
        self.assertEqual(assess(state(waiting=0), readings=None)["verdict"], "unknown")

    def test_a_device_a_gate_waits_for_that_is_not_connected_stops_it(self):
        record = {**DEVICES, "devices": [{"name": "Test iPad", "connection": "notDetected", "state": "held"}]}
        gate = {"began": NOW - 60, "waiting": "admission on this Mac: Test iPad is reserved", "waitingSince": NOW - 60}
        found = assess(state(gate), record=record)
        self.assertEqual((found["verdict"], levels(found)["devices"]), ("stuck", "danger"))
        self.assertIn("A gate waits for Test iPad, which is not detected.", found["sentence"])
        # The negative control: a device no gate waits for is no problem, connected or not.
        self.assertEqual(levels(assess(state({"began": NOW - 60}), record=record))["devices"], "ok")
        # With no device recorded, or the devices managed on another Mac, the sign stands aside.
        self.assertEqual(levels(assess(state({"began": NOW - 60}), record={**DEVICES, "devices": []}))["devices"], "idle")

    def test_a_mac_short_of_memory_is_slow_and_one_at_critical_pressure_stops_it(self):
        short = {"signal": "short of memory", "mac": "build-host",
                 "diagnosis": "build-host has swap 97% full; fseventsd holds 34 GB"}
        found = assess(state({"began": NOW - 60}), findings=[short])
        self.assertEqual((found["verdict"], levels(found)["macs"]), ("slow", "warning"))
        self.assertIn("fseventsd holds 34 GB", found["sentence"])
        self.assertNotIn("doctor has", found["sentence"])  # a finding a sign already names is not said twice
        found = assess(state({"began": NOW - 60}), readings=macs(memory=4))
        self.assertEqual((found["verdict"], levels(found)["macs"]), ("stuck", "danger"))

    def test_entries_coming_back_is_slow_and_doctors_other_findings_need_a_look(self):
        found = assess(state({"began": NOW - 60}), events=journal(back=3))
        self.assertEqual((found["verdict"], levels(found)["cameBack"]), ("slow", "warning"))
        self.assertIn("3 of the last 4 came back", found["sentence"])
        found = assess(state({"began": NOW - 60}), findings=[{"signal": "refused", "diagnosis": "d", "remedy": "r"}])
        self.assertEqual((found["verdict"], found["label"], found["exitStatus"]), ("slow", "Needs a look", health.ATTENTION))
        self.assertIn("Doctor has 1 finding (refused).", found["sentence"])


class Command(unittest.TestCase):
    def run_main(self, as_json=False, **changes):
        gathered = {"state": state({"began": NOW - 300}), "journal": journal(), "readings": macs(), "record": DEVICES,
                    "findings": [], **changes}
        said = StringIO()
        with patch.object(health, "gather", return_value=gathered), patch.object(health.time, "time", return_value=NOW), \
                patch.object(health, "changed_at", return_value=NOW - 20), \
                contextlib.redirect_stdout(said):
            return health.main(Path("/project"), as_json), said.getvalue()

    def test_it_prints_the_verdict_and_each_sign_and_exits_by_the_verdict(self):
        status, said = self.run_main()
        self.assertEqual(status, health.HEALTHY)
        self.assertTrue(said.startswith("All good. Everything is moving normally."), said)
        self.assertIn("  ✓ Progress   the gate on build-host is 5m in, usually 15m", said)
        status, said = self.run_main(readings=None)
        self.assertEqual(status, health.UNKNOWN_STATUS)
        self.assertIn("  ? Macs       no Mac's reading was made", said)
        status, said = self.run_main(as_json=True, findings=[{"signal": "refused", "diagnosis": "d", "remedy": "r"}])
        self.assertEqual(status, health.ATTENTION)
        self.assertIn('"verdict": "slow"', said)


if __name__ == "__main__":
    unittest.main()
