"""The README's screenshot script (tools/dashboard-screenshots.py): that the data it makes up still has the shape
each panel gives its script, so that a panel which changes is noticed here and not in a picture.

No browser is run here: the pictures themselves are looked at when they are retaken."""
from contextlib import redirect_stderr
import importlib.util
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src/python"))
import host_isolation  # noqa: F401 -- first, as every test module
from queuekeeper_dashboard import panels
from test_support import dashboard as test_dashboard

SCRIPT = Path(__file__).resolve().parents[2] / "tools/dashboard-screenshots.py"


def loaded():
    spec = importlib.util.spec_from_file_location("dashboard_screenshots", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MadeUpData(test_dashboard.Fixture):
    def setUp(self):
        super().setUp()
        self.script = loaded()

    def test_every_panel_has_a_tab_and_made_up_data(self):
        app = self.script.App()
        self.assertEqual([found["name"] for found in panels.listed()], list(self.script.TABS))
        for name in self.script.TABS:
            self.assertIsInstance(app.answer(f"/r/{self.script.REPOSITORY}/panel/{name}", {}), dict)

    def test_every_repositorys_made_up_data_has_the_fields_the_panel_gives_for_every_repository(self):
        app = self.script.App()
        for name in self.script.TABS:
            with self.subTest(panel=name):
                made_up = app.answer(f"/all/panel/{name}", {})
                self.assertEqual(set(made_up), set(panels.load(name).everywhere({"project": self.one}, {})))
        for name, found in self.script.MADE_UP.items():
            for tab in ("queue", "history"):
                with self.subTest(repository=name, panel=tab):
                    self.assertEqual(set(found[tab]), set(panels.load(tab).data(self.one, {})))
        (self.one.logs / "one-gate.log").parent.mkdir(parents=True, exist_ok=True)
        (self.one.logs / "one-gate.log").write_text("a line\n")
        real = panels.load("logs").everywhere({"project": self.one}, {"name": "one-gate.log", "repository": "project"})
        self.assertEqual(set(app.answer("/all/panel/logs", {"name": "one-gate.log", "repository": "orbit-game"})), set(real))

    def test_each_panels_made_up_data_has_the_fields_the_panel_gives(self):
        for name, made_up in self.script.PANELS.items():
            with self.subTest(panel=name):
                self.assertEqual(set(panels.load(name).data(self.one, {})), set(made_up))

    def test_the_made_up_logs_have_the_fields_the_logs_panel_gives(self):
        (self.one.logs / "one-gate.log").parent.mkdir(parents=True, exist_ok=True)
        (self.one.logs / "one-gate.log").write_text("a line\n")
        real = panels.load("logs").data(self.one, {"name": "one-gate.log"})
        made_up = self.script.logs({"name": "one-gate.log"})
        self.assertEqual(set(real), set(made_up))
        self.assertEqual(set(real["logs"][0]), set(made_up["logs"][0]))
        self.assertEqual(set(self.script.logs({})), set(panels.load("logs").data(self.one, {})))

    def test_an_entry_and_a_gate_have_only_fields_the_queue_panel_shows(self):
        queue = panels.load("queue")
        shown = {*queue.ENTRY, "log", "commits", "flakes", "ungated"}
        for found in [*self.script.WAITING, *self.script.RECENT]: self.assertLessEqual(set(found), shown)
        for found in self.script.PANELS["queue"]["round"]["gates"]:
            self.assertLessEqual(set(found), {*queue.GATE, "role", "log", "phase"})


class SentToAnotherMac(unittest.TestCase):
    def test_it_refuses_where_admission_sent_it_and_starts_no_browser(self):
        script = loaded()
        with tempfile.TemporaryDirectory() as folder, \
                patch.dict(os.environ, {"QUEUEKEEPER_DISPATCHED_FROM": "Sender"}), \
                patch.object(script, "serve") as serve, redirect_stderr(io.StringIO()) as said:
            self.assertEqual(script.main(["--out", str(Path(folder) / "shots")]), 2)
            self.assertFalse((Path(folder) / "shots").exists())
        serve.assert_not_called()
        self.assertIn("not through qk admit", said.getvalue())
