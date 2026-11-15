"""How extensions build on each other and share the core's hook points (vocaro/queuekeeper#6).

An extension names the extensions it requires, which load first. FOREGROUND and DEVICES take one provider each,
and what any number of extensions may add is a list."""
import contextlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import host_isolation  # noqa: F401 -- first, as every test module

from queuekeeper import extensions

# What each stand-in extension's register appends its own name to.
REGISTERED = []


class RequiresTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(extensions, "LOADED", []))
        self.enterContext(patch.object(sys, "path", [str(self.folder), *sys.path]))
        self.enterContext(patch.dict(sys.modules))
        REGISTERED.clear()

    def extension(self, name, requires=()):
        (self.folder / f"{name}.py").write_text(
            f"REQUIRES = {tuple(requires)!r}\n"
            f"def register():\n    import integration.test_extensions as test_extensions\n    test_extensions.REGISTERED.append({name!r})\n")

    def load(self, *names):
        # The stand-ins import this module by its file's name, which it does not have when run as a script.
        sys.modules.setdefault("test_extensions", sys.modules[__name__])
        extensions.load(names)

    def test_a_required_extension_registers_before_the_one_that_requires_it(self):
        self.extension("qk_shared")
        self.extension("qk_leaf", ["qk_shared"])
        self.load("qk_leaf")
        self.assertEqual(REGISTERED, ["qk_shared", "qk_leaf"])
        self.assertEqual(extensions.LOADED, ["qk_shared", "qk_leaf"])

    def test_an_extension_two_others_require_registers_once(self):
        self.extension("qk_shared")
        self.extension("qk_one", ["qk_shared"])
        self.extension("qk_two", ["qk_shared"])
        self.load("qk_one", "qk_two", "qk_shared")
        self.assertEqual(REGISTERED, ["qk_shared", "qk_one", "qk_two"])

    def test_a_requirement_of_a_requirement_loads_first(self):
        self.extension("qk_base")
        self.extension("qk_middle", ["qk_base"])
        self.extension("qk_top", ["qk_middle"])
        self.load("qk_top")
        self.assertEqual(REGISTERED, ["qk_base", "qk_middle", "qk_top"])

    def test_extensions_that_require_each_other_are_refused_and_none_registers(self):
        self.extension("qk_left", ["qk_right"])
        self.extension("qk_right", ["qk_left"])
        with self.assertRaisesRegex(ValueError, "cycle: qk_left -> qk_right -> qk_left"): self.load("qk_left")
        self.assertEqual((REGISTERED, extensions.LOADED), ([], []))

    def test_an_extension_that_names_no_requirements_loads_as_before(self):
        (self.folder / "qk_plain.py").write_text(
            "def register():\n    import integration.test_extensions as test_extensions\n    test_extensions.REGISTERED.append('qk_plain')\n")
        self.load("qk_plain")
        self.assertEqual(REGISTERED, ["qk_plain"])


class SlotTests(unittest.TestCase):
    def setUp(self):
        # The suite runs with the Apple extension's providers in the slots; these start from the core's own.
        self.enterContext(patch.object(extensions, "FOREGROUND", extensions.Foreground()))
        self.enterContext(patch.object(extensions, "DEVICES", extensions.Devices()))

    def test_one_extension_fills_a_slot_and_registering_again_keeps_its_provider(self):
        class Mine(extensions.Foreground): pass
        first = Mine()
        extensions.provide("FOREGROUND", first)
        extensions.provide("FOREGROUND", Mine())
        self.assertIs(extensions.FOREGROUND, first)

    def test_a_second_extension_is_refused_a_slot_another_fills(self):
        class Mine(extensions.Devices): pass
        class Theirs(extensions.Devices): pass
        extensions.provide("DEVICES", Mine())
        with self.assertRaisesRegex(ValueError, "provides DEVICES, which .* already does"):
            extensions.provide("DEVICES", Theirs())
        self.assertIsInstance(extensions.DEVICES, Mine)

    def test_each_slot_is_filled_apart_from_the_other(self):
        class Screen(extensions.Foreground): pass
        class Phones(extensions.Devices): pass
        extensions.provide("FOREGROUND", Screen())
        extensions.provide("DEVICES", Phones())
        self.assertIsInstance(extensions.FOREGROUND, Screen)
        self.assertIsInstance(extensions.DEVICES, Phones)

    def test_a_slot_refuses_what_is_not_its_kind(self):
        with self.assertRaises(TypeError): extensions.provide("FOREGROUND", extensions.Devices())

    def test_the_desktop_extension_fills_the_foreground_and_the_apple_extension_the_devices(self):
        from queuekeeper_apple import lane
        from queuekeeper_mac_desktop import lane as desktop_lane
        desktop_lane.register()
        lane.register()
        self.assertIsInstance(extensions.FOREGROUND, desktop_lane.Foreground)
        self.assertIsInstance(extensions.DEVICES, lane.Devices)
        # What the dispatch asks of the devices is on the same provider as what a lane's run does.
        self.assertTrue(callable(extensions.DEVICES.lease) and callable(extensions.DEVICES.take_window))


class SharedHookTests(unittest.TestCase):
    def test_every_extension_s_context_holds_around_a_run_and_closes_in_reverse(self):
        seen = []

        def around(name):
            @contextlib.contextmanager
            def held(commands, root):
                seen.append(f"enter {name} {root}")
                try: yield
                finally: seen.append(f"leave {name}")
            return held

        with patch.object(extensions, "AROUND_RUN", [around("one"), around("two")]):
            with extensions.around_run([["true"]], "ROOT"): seen.append("run")
        self.assertEqual(seen, ["enter one ROOT", "enter two ROOT", "run", "leave two", "leave one"])

    def test_no_extension_s_context_still_runs_the_work(self):
        with patch.object(extensions, "AROUND_RUN", []):
            with extensions.around_run([], "ROOT"): ran = True
        self.assertTrue(ran)

    def test_the_first_extension_that_sets_a_test_allowance_gives_it(self):
        with patch.object(extensions, "TEST_ALLOWANCE", [lambda words: None, lambda words: 90, lambda words: 5]):
            self.assertEqual(extensions.test_allowance(["swift", "test"]), 90)
        with patch.object(extensions, "TEST_ALLOWANCE", [lambda words: None]):
            self.assertIsNone(extensions.test_allowance(["swift", "test"]))

    def test_every_extension_recovers_and_reports(self):
        calls = []
        with patch.object(extensions, "RECOVER", [lambda: calls.append("a"), lambda: calls.append("b")]), \
                patch.object(extensions, "REPORT_RESULTS", [calls.append]):
            extensions.recover()
            extensions.report_results("results")
        self.assertEqual(calls, ["a", "b", "results"])

    def test_the_apple_extension_requires_the_desktop_extension(self):
        import queuekeeper_apple
        self.assertEqual(queuekeeper_apple.REQUIRES, ("queuekeeper_mac_desktop",))

    def test_the_desktop_and_apple_extensions_register_their_shared_hooks_once(self):
        from queuekeeper_apple import lane
        from queuekeeper_mac_desktop import lane as desktop_lane
        before = [list(hooks) for hooks in (extensions.RECOVER, extensions.AROUND_RUN, extensions.REPORT_RESULTS,
                                            extensions.TEST_ALLOWANCE)]
        desktop_lane.register()
        lane.register()
        after = [list(hooks) for hooks in (extensions.RECOVER, extensions.AROUND_RUN, extensions.REPORT_RESULTS,
                                           extensions.TEST_ALLOWANCE)]
        self.assertEqual(before, after)
        # Their own hooks, one in each list; another extension loaded beside them, as Unity's, may add its own.
        theirs = [[hook for hook in hooks if getattr(hook, "__module__", "").startswith(("queuekeeper_apple", "queuekeeper_mac_desktop"))]
                  for hooks in after]
        self.assertTrue(all(len(hooks) == 1 for hooks in theirs), after)


class LaneKeyTests(unittest.TestCase):
    """A key an extension adds to a lane: checked by the extension, and refused by name without it."""

    CONFIG = {"schemaVersion": 1, "name": "Project", "extensions": [],
              "lanes": {"mac": {"command": ["true"], "resource": "heavy", "display": True}}}

    def parse(self, **changes):
        import json
        from queuekeeper import config
        return config.parse(json.dumps({**self.CONFIG, **changes}))

    def test_a_key_wraps_the_lane_s_command_only_for_a_lane_that_says_it(self):
        class Wrapped(extensions.LaneKey):
            def command(self, lane, command): return ["wrap", lane["wrapped"], *command]
        with patch.dict(extensions.LANE_KEYS, {"wrapped": Wrapped()}, clear=True):
            self.assertEqual(extensions.lane_command({"wrapped": "x"}, ["swift", "test"]), ["wrap", "x", "swift", "test"])
            self.assertEqual(extensions.lane_command({}, ["swift", "test"]), ["swift", "test"])

    def test_only_a_key_that_says_so_has_the_screen_read(self):
        class Reads(extensions.LaneKey): reads_screen = True
        with patch.dict(extensions.LANE_KEYS, {"reads": Reads(), "plain": extensions.LaneKey()}, clear=True):
            self.assertTrue(extensions.reads_screen({"reads": True}))
            self.assertFalse(extensions.reads_screen({"reads": False}))
            self.assertFalse(extensions.reads_screen({"plain": True}))

    def test_a_lane_takes_a_key_an_extension_adds_and_the_extension_checks_it(self):
        class Positive(extensions.LaneKey):
            def check(self, name, lane):
                if lane["tries"] < 1: raise ValueError(f"{name}.tries must be at least 1")
        lanes = lambda tries: {"mac": {"command": ["true"], "tries": tries}}
        with patch.dict(extensions.LANE_KEYS, {"tries": Positive()}, clear=True):
            self.assertEqual(self.parse(lanes=lanes(2))["lanes"]["mac"]["tries"], 2)
            with self.assertRaisesRegex(ValueError, "mac.tries must be at least 1"): self.parse(lanes=lanes(0))
        with patch.dict(extensions.LANE_KEYS, {}, clear=True), self.assertRaisesRegex(ValueError, "Invalid lane mac"):
            self.parse(lanes=lanes(2))

    def test_display_without_the_desktop_extension_is_refused_naming_it(self):
        with patch.dict(extensions.LANE_KEYS, {}, clear=True), \
                self.assertRaisesRegex(ValueError, "mac.display is the queuekeeper_mac_desktop extension's key"):
            self.parse()

    def test_a_project_that_lists_the_desktop_extension_or_one_that_requires_it_gets_display(self):
        from queuekeeper_mac_desktop import display
        for listed in (["queuekeeper_mac_desktop"], ["queuekeeper_apple"]):
            with self.subTest(listed=listed), patch.dict(extensions.LANE_KEYS, {}, clear=True), \
                    patch.object(extensions, "LOADED", []):
                self.assertTrue(self.parse(extensions=listed)["lanes"]["mac"]["display"])
                self.assertIsInstance(extensions.LANE_KEYS["display"], display.Display)
                self.assertIn("queuekeeper_mac_desktop", extensions.LOADED)

    def test_display_keeps_the_display_awake_and_needs_an_admitted_lane(self):
        from queuekeeper_mac_desktop import display
        key = display.Display()
        self.assertEqual(key.command({"display": True}, ["swift", "test"]), [*display.AWAKE, "swift", "test"])
        self.assertEqual(key.command({"display": False}, ["swift", "test"]), ["swift", "test"])
        self.assertIn("caffeinate -d", display.AWAKE[2])
        with self.assertRaisesRegex(ValueError, "mac.display needs an admitted lane"):
            key.check("mac", {"display": True, "resource": "self"})
        with self.assertRaisesRegex(ValueError, "mac.display must be true or false"): key.check("mac", {"display": "yes"})


if __name__ == "__main__":
    unittest.main()


class HistoricalConfigurationTests(unittest.TestCase):
    def test_historical_main_configuration_loads_the_new_plugin_packages(self):
        import json
        from queuekeeper import config, merge_queue
        source = json.dumps({"schemaVersion": 1, "name": "historical", "extensions": ["queuekeeper.apple"],
                             "simulatorPool": "Historical", "lanes": {"gate": {"command": ["true"], "display": True}}})
        with patch.object(merge_queue, "config_source", return_value=source), patch.object(extensions, "LOADED", []):
            parsed = merge_queue.config_at(Path("/unused"), "origin/main")
            self.assertEqual(parsed["extensions"], ["queuekeeper_apple"])
            self.assertEqual(extensions.LOADED, ["queuekeeper_mac_desktop", "queuekeeper_apple"])
            self.assertNotIn("queuekeeper.apple", sys.modules)

    def test_all_historical_builtin_names_normalize_without_mutating_the_input(self):
        from queuekeeper import config
        declared = ["queuekeeper.apple", {"module": "queuekeeper.mac_desktop", "path": "../kit/src"},
                    "queuekeeper.mac_hardware", "queuekeeper.unity", "custom.hooks"]
        self.assertEqual(config.extensions(declared),
                         ["queuekeeper_apple", {"module": "queuekeeper_mac_desktop", "path": "../kit/src"},
                          "queuekeeper_mac_hardware", "queuekeeper_unity", "custom.hooks"])
        self.assertEqual(declared[0], "queuekeeper.apple")
        self.assertEqual(declared[1]["module"], "queuekeeper.mac_desktop")
