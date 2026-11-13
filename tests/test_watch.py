"""The watch feature (queuekeeper.visibility.watch): the view it draws, and its command once it is turned on."""
import io
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/python"))
import host_isolation  # noqa: F401 -- first, as every test module
from queuekeeper import cli, events, extensions, merge_queue as mq, visibility
from queuekeeper.visibility import watch
import test_merge_queue

NOW = 1_000_000.0
IDLE = {"worker": False, "held": None, "round": None, "waiting": [], "recent": [], "ungated": []}


def entry(identifier, state, subject, **fields):
    return {"id": identifier, "state": state, "subject": subject, "submitted": NOW - 600, **fields}


class View(unittest.TestCase):
    def frame(self, state, told=(), **how):
        return watch.frame("project", {**IDLE, **state}, list(told), now=NOW, **how)

    def test_an_idle_queue_is_two_lines(self):
        lines = self.frame({})
        self.assertRegex(lines[0], r"^project  \d\d:\d\d:\d\d$")
        self.assertEqual(lines[1:], ["Idle: nothing is waiting"])

    def test_the_headline_says_what_the_queue_is_doing(self):
        waiting = [entry("a", "testing", "A"), entry("b", "queued", "B"), entry("c", "queued", "C")]
        for state, said in (
                ({"held": "for the owner, since nobody else can fix it: a refused key", "worker": True,
                  "waiting": waiting}, "Held for the owner, since nobody else can fix it: a refused key"),
                ({"worker": True, "round": {"round": "r", "entries": ["a"]}, "waiting": waiting},
                 "Testing 1 entry, 2 more waiting"),
                ({"worker": True, "round": {"round": "r", "entries": ["a", "b", "c"]}, "waiting": waiting},
                 "Testing 3 entries"),
                ({"worker": True, "waiting": waiting}, "Worker running, 3 waiting"),
                ({"waiting": waiting}, "3 waiting with no worker: `qk queue resume` starts one")):
            with self.subTest(said=said):
                self.assertEqual(watch.headline({**IDLE, **state}), said)

    def test_each_gate_shows_its_lane_its_mac_its_time_and_what_it_waits_for(self):
        gates = {"batch": {"lane": "gate", "mac": "this Mac", "began": NOW - 372, "progress": [{"phase": "testing"}]},
                 "stacked": {"lane": "gate", "mac": None, "began": NOW - 40, "waiting": "admission on the second Mac",
                             "waitingSince": NOW - 30, "stuck": "no output for 20 minutes"}}
        lines = self.frame({"worker": True, "round": {"round": "r", "entries": ["a"], "gates": gates}})
        self.assertEqual(lines[2:], ["", "Gates", "  batch: gate lane on this Mac, 6m 12s", "    phase: testing",
                                     "  stacked: gate lane on no Mac yet, 40s",
                                     "    waiting: admission on the second Mac, for 30s", "    no output for 20 minutes"])

    def test_entries_show_their_state_their_age_and_where_they_landed(self):
        lines = self.frame({"waiting": [entry("a", "queued", "Add a")], "ungated": ["z"],
                            "recent": [entry("y", "failed", "Break y", settled=NOW - 7300),
                                       entry("z", "landed", "Add z", settled=NOW - 90, landedAs="0123456789abcdef")]})
        self.assertEqual(lines[2:], ["", "Queue", "  queued   a  Add a  (10m 0s)", "",
                                     "Ungated on main until the next gate covers them: z", "", "Recent",
                                     "  landed   z  Add z -> 0123456789ab  (1m 30s ago)",
                                     "  failed   y  Break y  (2h 1m ago)"])

    def test_the_last_events_are_shown_newest_first_without_their_date(self):
        told = [{"kind": "entry.queued", "time": NOW - 5, "entry": "a", "subject": "Add a"},
                {"kind": "queue.held", "time": NOW, "why": "for a test"}]
        lines = self.frame({}, told)
        self.assertEqual(lines[2:4], ["", "Events"])
        self.assertRegex(lines[4], r"^  \d\d:\d\d:\d\d held for a test$")
        self.assertRegex(lines[5], r"^  \d\d:\d\d:\d\d a queued: Add a$")

    def test_a_line_longer_than_the_terminal_is_cut_to_it(self):
        lines = self.frame({"waiting": [entry("a", "queued", "A subject that runs on " * 10)]}, width=40)
        self.assertTrue(all(len(line) <= 40 for line in lines))
        self.assertTrue(lines[4].endswith("…"))

    def test_a_snapshot_with_keys_missing_still_draws(self):
        self.assertEqual(watch.frame("project", {}, [], now=NOW)[1], "Idle: nothing is waiting")
        watch.frame("project", {"worker": True, "round": {"round": "r", "gates": {"batch": {}}},
                                "waiting": [{"id": "a"}], "recent": [{}]}, [{"kind": "later.kind"}], now=NOW)

    def test_a_span_is_its_two_largest_units(self):
        self.assertEqual([watch.span(seconds) for seconds in (-3, 0, 59, 60, 372, 3600, 7500, 90000)],
                         ["0s", "0s", "59s", "1m 0s", "6m 12s", "1h 0m", "2h 5m", "1d 1h"])


class Command(test_merge_queue.Fixture):
    """`qk watch`, which is a command only while the feature is on."""

    def setUp(self):
        super().setUp()
        settings = patch.object(visibility, "settings_path", lambda: self.base / "settings" / visibility.FILE)
        settings.start()
        self.addCleanup(settings.stop)
        for kept in (extensions.COMMANDS, extensions.LOADED):
            before = kept.copy()
            self.addCleanup(lambda kept=kept, before=before: (kept.clear(), kept.update(before) if isinstance(kept, dict)
                                                              else kept.extend(before)))
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop(visibility.SWITCH, None)
        extensions.COMMANDS.pop("watch", None)
        if watch.__name__ in extensions.LOADED: extensions.LOADED.remove(watch.__name__)

    def command(self, *words, project=None):
        """(its status, or None when the command's parser refused it, what it printed, what it said of errors)."""
        printed, errors, status = io.StringIO(), io.StringIO(), None
        with patch("sys.stdout", printed), patch("sys.stderr", errors):
            try: status = cli.main(["--project", str(project or self.repo), "watch", *words])
            except SystemExit: pass
        return status, printed.getvalue(), errors.getvalue()

    def test_it_is_no_command_until_the_feature_is_on(self):
        status, _, errors = self.command("--once")
        self.assertIsNone(status)
        self.assertIn("invalid choice: 'watch'", errors)

    def test_once_on_it_prints_the_view_once_where_its_output_is_no_terminal(self):
        visibility.switch("watch", True)
        self.commit("a.txt", "one")
        identifier, = self.add("one")
        status, printed, _ = self.command()
        self.assertEqual(status, 0)
        lines = printed.splitlines()
        self.assertTrue(lines[0].startswith("project  "))
        self.assertEqual(lines[1], "1 waiting with no worker: `qk queue resume` starts one")
        self.assertIn(f"  queued   {identifier}  Add a.txt", printed)
        self.assertIn(f"{identifier} queued: Add a.txt", lines[-1])
        self.assertNotIn(watch.CLEAR, printed)

    def test_it_shows_as_many_events_as_asked_and_none_for_zero(self):
        visibility.switch("watch", True)
        directory = mq.Queue(self.repo).directory
        for number in range(5): events.emit(directory, "queue.held", why=f"cause {number}")
        _, printed, _ = self.command("--once", "--events", "2")
        self.assertEqual([line[-7:] for line in printed.splitlines()[-2:]], ["cause 4", "cause 3"])
        self.assertNotIn("cause 2", printed)
        self.assertNotIn("Events", self.command("--once", "--events", "0")[1])

    def test_a_folder_with_no_repository_is_refused(self):
        visibility.switch("watch", True)
        empty = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, empty, True)
        status, _, errors = self.command("--once", project=empty)
        self.assertEqual(status, 2)
        self.assertIn("has no queue to watch", errors)


class Following(test_merge_queue.Fixture):
    """`qk queue events --follow` shows the journal once, however its reading and its following overlap."""

    def test_what_is_read_is_not_shown_again_and_what_was_written_meanwhile_is_not_missed(self):
        directory = mq.Queue(self.repo).directory
        events.emit(directory, "queue.held", why="before")
        read = events.read
        def reading(*arguments, **how):
            found = read(*arguments, **how)
            events.emit(directory, "queue.held", why="meanwhile")  # written after the reading, before the following
            return found
        shown = []
        def out(line, flush=False):
            shown.append(line.split(" held ")[1])
            if len(shown) == 2: raise KeyboardInterrupt
        with patch.object(events, "read", reading):
            self.assertEqual(mq.journal(self.repo, following=True, out=out), 0)
        self.assertEqual(shown, ["before", "meanwhile"])


if __name__ == "__main__":
    unittest.main()
