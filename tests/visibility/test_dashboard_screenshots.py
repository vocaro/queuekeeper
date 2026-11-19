"""The README's screenshot script (tools/dashboard-screenshots.py): that the data it makes up still has the shape
each panel gives its script, so that a panel which changes is noticed here and not in a picture.

No browser is run here: the pictures themselves are looked at when they are retaken."""
from contextlib import redirect_stderr, redirect_stdout
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
        shown = {*queue.ENTRY, "log", "commits", "flakes", "ungated", "by"}
        for found in [*self.script.WAITING, *self.script.RECENT]: self.assertLessEqual(set(found), shown)
        for found in self.script.PANELS["queue"]["round"]["gates"]:
            self.assertLessEqual(set(found), {*queue.GATE, "role", "log", "phase"})

    def test_home_is_saved_as_its_picture_with_the_flow_map_moving_over_it_unless_motion_is_reduced(self):
        from xml.dom import minidom
        drawn = ('<html data-height="1200" data-flow="50 300.5 900 200" data-flow-under="rgb(18, 23, 34)"><main>'
                 '<div class="flow-map"><svg viewBox="0 0 900 200" role="group" aria-label="Agents&#39; entries">'
                 '<path id="flow-edge-1" class="edge submit live p3" d="M0 0 C 1 1, 2 2, 3 3"></path>'
                 '<circle class="moving submit p3" r="3.5"><animateMotion dur="1.6s" begin="-0.4s" repeatCount="indefinite">'
                 '<mpath href="#flow-edge-1"></mpath></animateMotion></circle>'
                 '<a href="/r/x/queue"><title>Only x</title><rect class="node running" x="0" y="0" width="9" height="9"></rect></a>'
                 '</svg></div></main></html>')
        saved = self.script.moving(b"picture", drawn, "dark")
        document = minidom.parseString(saved)
        root = document.documentElement
        self.assertEqual((root.getAttribute("viewBox"), root.getAttribute("height")), ("0 0 1000 1200", "1200"))
        picture = document.getElementsByTagName("image")[0]
        self.assertEqual((picture.getAttribute("href"), picture.getAttribute("height")), ("data:image/png;base64,cGljdHVyZQ==", "1200"))
        # The map where the page drew it, over the picture's own, on the color it was drawn on.
        map_ = root.getElementsByTagName("svg")[0]
        self.assertEqual([map_.getAttribute(name) for name in ("x", "y", "width", "height", "viewBox")],
                         ["50", "300.5", "900", "200", "0 0 900 200"])
        self.assertEqual(map_.getElementsByTagName("rect")[0].getAttribute("fill"), "rgb(18, 23, 34)")
        self.assertIn("Agents' entries", document.getElementsByTagName("title")[0].firstChild.data)
        self.assertEqual((len(document.getElementsByTagName("a")), len(document.getElementsByTagName("title"))), (0, 1))
        self.assertEqual(len(document.getElementsByTagName("animateMotion")), 1)
        style = document.getElementsByTagName("style")[0].firstChild.data
        # The theme's colors, the Flow rules without the page's box, and the dots stilled only for reduced motion.
        self.assertIn("--card: #121722", style)
        self.assertIn(".node.running {", style)
        self.assertNotIn(".flow-map", style)
        self.assertIn("@media (prefers-reduced-motion: reduce) { .moving { display: none; } }", style)
        self.assertNotRegex(style, r"(?m)^\s*\{")  # no rule left without its selector
        self.assertNotRegex(style.split("@media")[0], r"\.moving\s*\{\s*display: none")
        self.assertNotIn("--card: #121722", self.script.moving(b"picture", drawn, "light"))
        self.assertIsNone(self.script.moving(b"picture", '<html data-height="1200"><main>no map</main></html>', "dark"))


class Served(test_dashboard.Fixture):
    def test_its_server_answers_the_page_and_the_api(self):
        import http.client
        script = loaded()
        made = script.serve("dark")
        self.addCleanup(made.server_close)
        self.addCleanup(made.shutdown)
        for path in ("/", "/api/panels", "/api/all/panel/queue", "/static/app.js"):
            connection = http.client.HTTPConnection("127.0.0.1", made.server_address[1], timeout=host_isolation.stretched(20))
            self.addCleanup(connection.close)
            connection.request("GET", path, headers={"Host": f"localhost:{made.server_address[1]}"})
            self.assertEqual(connection.getresponse().status, 200, path)


class CheckMotion(unittest.TestCase):
    """--check-motion: that it passes when a dot is elsewhere at each time and fails when none moved. Chrome is not
    run; dots() is replaced by where it would say the dots are."""

    def setUp(self):
        self.script = loaded()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        (self.folder / "dashboard-home.svg").write_text("<svg/>")
        self.chrome = self.folder / "chrome"
        self.chrome.write_text("")

    def check(self, found):
        arguments = ["--out", str(self.folder), "--chrome", str(self.chrome), "--check-motion", str(self.folder / "stills")]
        with patch.object(self.script, "dots", return_value=found) as dots, patch.object(self.script, "serve") as serve, \
                redirect_stderr(io.StringIO()) as said, redirect_stdout(io.StringIO()) as told:
            code = self.script.main(arguments)
        serve.assert_not_called()
        return code, dots, said.getvalue() + told.getvalue()

    def test_dots_somewhere_else_at_each_time_pass(self):
        code, dots, said = self.check([(0, Path("a.png"), [[1, 2], [3, 4]]), (0.5, Path("b.png"), [[1, 2], [5, 6]])])
        self.assertEqual(code, 0, said)
        dots.assert_called_once_with(str(self.chrome), self.folder / "dashboard-home.svg", self.folder / "stills")
        self.assertIn("the dots move", said)

    def test_dots_in_the_same_place_or_none_fail(self):
        code, _, said = self.check([(0, Path("a.png"), [[1, 2]]), (0.5, Path("b.png"), [[1, 2]])])
        self.assertEqual(code, 1)
        self.assertIn("every dot is where it was at 0s again at 0.5s", said)
        code, _, said = self.check([(0, Path("a.png"), []), (0.5, Path("b.png"), [])])
        self.assertEqual(code, 1)
        self.assertIn("no moving dots", said)

    def test_no_image_is_refused_before_chrome_starts(self):
        (self.folder / "dashboard-home.svg").unlink()
        code, dots, said = self.check([])
        self.assertEqual(code, 2)
        dots.assert_not_called()
        self.assertIn("no image at", said)


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
