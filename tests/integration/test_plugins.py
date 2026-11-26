"""How plugins build on each other and share the core's hook points (vocaro/queuekeeper#6).

A plugin names the plugins it requires, which load first. FOREGROUND and DEVICES take one provider each,
and what any number of plugins may add is a list."""
import contextlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import host_isolation  # noqa: F401 -- first, as every test module

from queuekeeper import plugins, remote

# What each stand-in plugin's register appends its own name to.
REGISTERED = []


class RequiresTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(plugins, "LOADED", []))
        self.enterContext(patch.object(sys, "path", [str(self.folder), *sys.path]))
        self.enterContext(patch.dict(sys.modules))
        REGISTERED.clear()

    def plugin(self, name, requires=()):
        (self.folder / f"{name}.py").write_text(
            f"REQUIRES = {tuple(requires)!r}\n"
            f"def register():\n    import integration.test_plugins as test_plugins\n    test_plugins.REGISTERED.append({name!r})\n")

    def load(self, *names):
        # The stand-ins import this module by its file's name, which it does not have when run as a script.
        sys.modules.setdefault("test_plugins", sys.modules[__name__])
        plugins.load(names)

    def test_a_required_plugin_registers_before_the_one_that_requires_it(self):
        self.plugin("qk_shared")
        self.plugin("qk_leaf", ["qk_shared"])
        self.load("qk_leaf")
        self.assertEqual(REGISTERED, ["qk_shared", "qk_leaf"])
        self.assertEqual(plugins.LOADED, ["qk_shared", "qk_leaf"])

    def test_a_plugin_two_others_require_registers_once(self):
        self.plugin("qk_shared")
        self.plugin("qk_one", ["qk_shared"])
        self.plugin("qk_two", ["qk_shared"])
        self.load("qk_one", "qk_two", "qk_shared")
        self.assertEqual(REGISTERED, ["qk_shared", "qk_one", "qk_two"])

    def test_a_requirement_of_a_requirement_loads_first(self):
        self.plugin("qk_base")
        self.plugin("qk_middle", ["qk_base"])
        self.plugin("qk_top", ["qk_middle"])
        self.load("qk_top")
        self.assertEqual(REGISTERED, ["qk_base", "qk_middle", "qk_top"])

    def test_plugins_that_require_each_other_are_refused_and_none_registers(self):
        self.plugin("qk_left", ["qk_right"])
        self.plugin("qk_right", ["qk_left"])
        with self.assertRaisesRegex(ValueError, "cycle: qk_left -> qk_right -> qk_left"): self.load("qk_left")
        self.assertEqual((REGISTERED, plugins.LOADED), ([], []))

    def test_a_plugin_that_names_no_requirements_loads_as_before(self):
        (self.folder / "qk_plain.py").write_text(
            "def register():\n    import integration.test_plugins as test_plugins\n    test_plugins.REGISTERED.append('qk_plain')\n")
        self.load("qk_plain")
        self.assertEqual(REGISTERED, ["qk_plain"])


class SlotTests(unittest.TestCase):
    def setUp(self):
        # The suite runs with the Apple plugin's providers in the slots; these start from the core's own.
        self.enterContext(patch.object(plugins, "FOREGROUND", plugins.Foreground()))
        self.enterContext(patch.object(plugins, "DEVICES", plugins.Devices()))

    def test_one_plugin_fills_a_slot_and_registering_again_keeps_its_provider(self):
        class Mine(plugins.Foreground): pass
        first = Mine()
        plugins.provide("FOREGROUND", first)
        plugins.provide("FOREGROUND", Mine())
        self.assertIs(plugins.FOREGROUND, first)

    def test_a_second_plugin_is_refused_a_slot_another_fills(self):
        class Mine(plugins.Devices): pass
        class Theirs(plugins.Devices): pass
        plugins.provide("DEVICES", Mine())
        with self.assertRaisesRegex(ValueError, "provides DEVICES, which .* already does"):
            plugins.provide("DEVICES", Theirs())
        self.assertIsInstance(plugins.DEVICES, Mine)

    def test_each_slot_is_filled_apart_from_the_other(self):
        class Screen(plugins.Foreground): pass
        class Phones(plugins.Devices): pass
        plugins.provide("FOREGROUND", Screen())
        plugins.provide("DEVICES", Phones())
        self.assertIsInstance(plugins.FOREGROUND, Screen)
        self.assertIsInstance(plugins.DEVICES, Phones)

    def test_a_slot_refuses_what_is_not_its_kind(self):
        with self.assertRaises(TypeError): plugins.provide("FOREGROUND", plugins.Devices())

    def test_the_desktop_plugin_fills_the_foreground_and_the_mobile_plugin_the_devices(self):
        from queuekeeper_apple import lane as apple_lane
        from queuekeeper_mobile import lane, platforms
        from queuekeeper_mac_desktop import lane as desktop_lane
        desktop_lane.register()
        lane.register()
        apple_lane.register()
        # The Apple plugin fills no slot of its own: it registers its platform with the mobile one.
        self.assertIn("apple", [platform.name for platform in platforms.PLATFORMS])
        self.assertIsInstance(plugins.FOREGROUND, desktop_lane.Foreground)
        self.assertIsInstance(plugins.DEVICES, lane.Devices)
        # What the dispatch asks of the devices is on the same provider as what a lane's run does.
        self.assertTrue(callable(plugins.DEVICES.lease) and callable(plugins.DEVICES.take_window))


class SharedHookTests(unittest.TestCase):
    def test_every_plugin_s_context_holds_around_a_run_and_closes_in_reverse(self):
        seen = []

        def around(name):
            @contextlib.contextmanager
            def held(commands, root):
                seen.append(f"enter {name} {root}")
                try: yield
                finally: seen.append(f"leave {name}")
            return held

        with patch.object(plugins, "AROUND_RUN", [around("one"), around("two")]):
            with plugins.around_run([["true"]], "ROOT"): seen.append("run")
        self.assertEqual(seen, ["enter one ROOT", "enter two ROOT", "run", "leave two", "leave one"])

    def test_no_plugin_s_context_still_runs_the_work(self):
        with patch.object(plugins, "AROUND_RUN", []):
            with plugins.around_run([], "ROOT"): ran = True
        self.assertTrue(ran)

    def test_the_first_plugin_that_sets_a_test_allowance_gives_it(self):
        with patch.object(plugins, "TEST_ALLOWANCE", [lambda words: None, lambda words: 90, lambda words: 5]):
            self.assertEqual(plugins.test_allowance(["swift", "test"]), 90)
        with patch.object(plugins, "TEST_ALLOWANCE", [lambda words: None]):
            self.assertIsNone(plugins.test_allowance(["swift", "test"]))

    def test_the_first_plugin_that_keeps_a_command_here_says_why_and_none_keeps_it_without_one(self):
        command = ["xcrun", "simctl", "boot", "5B0E6C51-2D3A-4F7B-9C1E-0A4B8D2F6E13"]
        with patch.object(plugins, "COMMAND_KEPT_HERE", [lambda words: None, lambda words: f"it names {words[1]}",
                                                         lambda words: "later"]):
            self.assertEqual(plugins.kept_here(command), "it names simctl")
        with patch.object(plugins, "COMMAND_KEPT_HERE", []):
            self.assertIsNone(plugins.kept_here(command))

    def test_the_first_plugin_that_evens_out_runtimes_answers_remote_setup_runtimes(self):
        calls = []
        first = lambda host, check: calls.append(("first", host, check)) or 3
        with patch.object(plugins, "REMOTE_SETUP_RUNTIMES", [first, lambda host, check: calls.append("later")]):
            self.assertEqual(remote.setup("fake", [], [], False, runtimes=True), 3)
            # Formulae or modules alongside are refused before any plugin is asked.
            with self.assertRaisesRegex(ValueError, "evens out only the simulator runtimes"):
                remote.setup("fake", ["numpy"], [], False, runtimes=True)
        self.assertEqual(calls, [("first", "fake", False)])

    def test_every_plugin_recovers_and_reports(self):
        calls = []
        with patch.object(plugins, "RECOVER", [lambda: calls.append("a"), lambda: calls.append("b")]), \
                patch.object(plugins, "REPORT_RESULTS", [calls.append]):
            plugins.recover()
            plugins.report_results("results")
        self.assertEqual(calls, ["a", "b", "results"])

    def test_the_apple_and_android_plugins_require_the_desktop_and_mobile_plugins(self):
        import queuekeeper_android
        import queuekeeper_apple
        import queuekeeper_mobile
        self.assertEqual(queuekeeper_apple.REQUIRES, ("queuekeeper_mac_desktop", "queuekeeper_mobile"))
        self.assertEqual(queuekeeper_android.REQUIRES, ("queuekeeper_mac_desktop", "queuekeeper_mobile"))
        self.assertEqual(queuekeeper_mobile.REQUIRES, ("queuekeeper_mac_desktop",))

    def test_the_desktop_and_apple_plugins_register_their_shared_hooks_once(self):
        from queuekeeper_apple import lane
        from queuekeeper_mac_desktop import lane as desktop_lane
        before = [list(hooks) for hooks in (plugins.RECOVER, plugins.AROUND_RUN, plugins.REPORT_RESULTS,
                                            plugins.TEST_ALLOWANCE, plugins.LANE_TEST_ALLOWANCE)]
        desktop_lane.register()
        lane.register()
        after = [list(hooks) for hooks in (plugins.RECOVER, plugins.AROUND_RUN, plugins.REPORT_RESULTS,
                                           plugins.TEST_ALLOWANCE, plugins.LANE_TEST_ALLOWANCE)]
        self.assertEqual(before, after)
        # Their own hooks, one in each list; another plugin loaded beside them, as Unity's, may add its own.
        theirs = [[hook for hook in hooks if getattr(hook, "__module__", "").startswith(("queuekeeper_apple", "queuekeeper_mac_desktop"))]
                  for hooks in after]
        self.assertTrue(all(len(hooks) == 1 for hooks in theirs), after)


class LaneKeyTests(unittest.TestCase):
    """A key a plugin adds to a lane: checked by the plugin, and refused by name without it."""

    CONFIG = {"schemaVersion": 1, "name": "Project", "plugins": [],
              "lanes": {"mac": {"command": ["true"], "resource": "heavy", "display": True}}}

    def parse(self, **changes):
        import json
        from queuekeeper import config
        return config.parse(json.dumps({**self.CONFIG, **changes}))

    def test_the_old_extensions_key_is_refused_naming_plugins(self):
        with self.assertRaisesRegex(ValueError, "^extensions is now plugins .*: rename the key in queuekeeper.json"):
            self.parse(extensions=[])

    def test_a_key_wraps_the_lane_s_command_only_for_a_lane_that_says_it(self):
        class Wrapped(plugins.LaneKey):
            def command(self, lane, command): return ["wrap", lane["wrapped"], *command]
        with patch.dict(plugins.LANE_KEYS, {"wrapped": Wrapped()}, clear=True):
            self.assertEqual(plugins.lane_command({"wrapped": "x"}, ["swift", "test"]), ["wrap", "x", "swift", "test"])
            self.assertEqual(plugins.lane_command({}, ["swift", "test"]), ["swift", "test"])

    def test_only_a_key_that_says_so_has_the_screen_read(self):
        class Reads(plugins.LaneKey): reads_screen = True
        with patch.dict(plugins.LANE_KEYS, {"reads": Reads(), "plain": plugins.LaneKey()}, clear=True):
            self.assertTrue(plugins.reads_screen({"reads": True}))
            self.assertFalse(plugins.reads_screen({"reads": False}))
            self.assertFalse(plugins.reads_screen({"plain": True}))

    def test_a_lane_takes_a_key_a_plugin_adds_and_the_plugin_checks_it(self):
        class Positive(plugins.LaneKey):
            def check(self, name, lane):
                if lane["tries"] < 1: raise ValueError(f"{name}.tries must be at least 1")
        lanes = lambda tries: {"mac": {"command": ["true"], "tries": tries}}
        with patch.dict(plugins.LANE_KEYS, {"tries": Positive()}, clear=True):
            self.assertEqual(self.parse(lanes=lanes(2))["lanes"]["mac"]["tries"], 2)
            with self.assertRaisesRegex(ValueError, "mac.tries must be at least 1"): self.parse(lanes=lanes(0))
        with patch.dict(plugins.LANE_KEYS, {}, clear=True), self.assertRaisesRegex(ValueError, "Invalid lane mac"):
            self.parse(lanes=lanes(2))

    def test_display_without_the_desktop_plugin_is_refused_naming_it(self):
        with patch.dict(plugins.LANE_KEYS, {}, clear=True), \
                self.assertRaisesRegex(ValueError, "mac.display is the queuekeeper_mac_desktop plugin's key"):
            self.parse()

    def test_a_project_that_lists_the_desktop_plugin_or_one_that_requires_it_gets_display(self):
        from queuekeeper_mac_desktop import display
        for listed in (["queuekeeper_mac_desktop"], ["queuekeeper_apple"]):
            with self.subTest(listed=listed), patch.dict(plugins.LANE_KEYS, {}, clear=True), \
                    patch.object(plugins, "LOADED", []):
                self.assertTrue(self.parse(plugins=listed)["lanes"]["mac"]["display"])
                self.assertIsInstance(plugins.LANE_KEYS["display"], display.Display)
                self.assertIn("queuekeeper_mac_desktop", plugins.LOADED)

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


class RenamedKeyCommandTests(unittest.TestCase):
    """A command a plugin adds, asked for in a project whose queuekeeper.json still lists its plugins under
    "extensions", which loads none of them (#371)."""

    def perform(self, configuration, *words):
        """(its exit code, what it said of errors)."""
        import io
        import json
        from queuekeeper import cli
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (folder / "queuekeeper.json").write_text(json.dumps(configuration))
        errors = io.StringIO()
        # The suite loads the Apple plugin; this project's command finds it not loaded.
        with patch.object(plugins, "COMMANDS", {}), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(errors), self.assertRaises(SystemExit) as stopped:
            cli.perform(["--project", str(folder), *words], None)
        return stopped.exception.code, errors.getvalue()

    def release(self, key):
        code, said = self.perform({"schemaVersion": 1, "name": "Project", key: ["queuekeeper_apple"], "lanes": {}},
                                  "simulator-pool", "release", "--mine")
        self.assertEqual(code, 2)
        self.assertIn("invalid choice: 'simulator-pool'", said)
        return said

    def test_the_old_key_says_why_the_command_is_missing(self):
        self.assertIn("queuekeeper: extensions is now plugins (vocaro/queuekeeper#48): rename the key in "
                      "queuekeeper.json, which lists the same modules. Until then no plugin it names is loaded "
                      "(queuekeeper_apple), so the commands they add are missing\n", self.release("extensions"))

    def test_the_new_key_gives_argparse_s_refusal_alone(self):
        self.assertNotIn("extensions", self.release("plugins"))

    def test_help_says_nothing_of_the_old_key(self):
        self.assertEqual(self.perform({"schemaVersion": 1, "name": "P", "extensions": []}, "--help"), (0, ""))


class OldPluginNameTests(unittest.TestCase):
    """The plugins' names before they left the core, such as queuekeeper.apple, are no plugins (#187)."""
    def test_each_old_name_is_refused_naming_its_package(self):
        from queuekeeper import config
        for old, package in [("queuekeeper.apple", "queuekeeper_apple"),
                             ("queuekeeper.mac_desktop", "queuekeeper_mac_desktop"),
                             ("queuekeeper.unity", "queuekeeper_unity")]:
            for declared in ([old], [{"module": old, "path": "../kit/src"}]):
                with self.subTest(declared=declared), self.assertRaisesRegex(
                        ValueError, f"plugins names {old}, which is no module of Queuekeeper's; "
                                    f"the plugin is the package {package}: list {package}$"):
                    config.plugins(declared)

    def test_a_project_that_lists_an_old_name_is_refused_and_loads_nothing(self):
        import json
        from queuekeeper import config
        source = json.dumps({"schemaVersion": 1, "name": "old", "plugins": ["queuekeeper.apple"], "lanes": {}})
        with patch.object(plugins, "LOADED", []), \
                self.assertRaisesRegex(ValueError, "list queuekeeper_apple$"):
            config.parse(source)
        self.assertNotIn("queuekeeper.apple", sys.modules)

    def test_an_old_name_whose_folder_a_checkout_keeps_with_only_ignored_files_is_refused(self):
        import queuekeeper
        from queuekeeper import config
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "apple" / "__pycache__").mkdir(parents=True)
            with patch.object(queuekeeper, "__path__", [*queuekeeper.__path__, folder]), patch.dict(sys.modules), \
                    self.assertRaisesRegex(ValueError, "list queuekeeper_apple$"):
                config.plugins(["queuekeeper.apple"])

    def test_a_name_in_queuekeeper_that_no_package_matches_names_none(self):
        from queuekeeper import config
        for named in ("queuekeeper.nothing", "queuekeeper.nothing.deeper"):
            with self.subTest(named=named), \
                    self.assertRaisesRegex(ValueError, f"plugins names {named}, which is no module of Queuekeeper's$"):
                config.plugins([named])

    def test_the_package_names_and_a_projects_own_modules_are_kept_as_given(self):
        from queuekeeper import config
        declared = ["queuekeeper_apple", {"module": "queuekeeper_mac_desktop", "path": "../kit/src"}, "custom.hooks"]
        self.assertEqual(config.plugins(declared), declared)


class ChipsGoneTests(unittest.TestCase):
    """A lane's chips and the Mac hardware plugin that gave it are refused, saying that labels replace them
    (vocaro/queuekeeper#224)."""
    def parse(self, **value):
        import json
        from queuekeeper import config
        return config.parse(json.dumps({"schemaVersion": 1, "name": "chips", **value}))

    def test_a_lane_that_lists_chips_is_refused_naming_labels(self):
        with patch.object(plugins, "LOADED", []), self.assertRaisesRegex(
                ValueError, r"^gate\.chips, which kept a lane on a Mac with a listed chip, is gone from Queuekeeper: "
                            r"labels replace it\. .*mac-settings\.json"):
            self.parse(lanes={"gate": {"command": ["true"], "chips": ["apple-m5-max"]}})

    def test_the_plugin_is_refused_by_either_name_and_loads_nothing(self):
        for named in ("queuekeeper_mac_hardware", "queuekeeper.mac_hardware"):
            with self.subTest(named=named), patch.object(plugins, "LOADED", []), self.assertRaisesRegex(
                    ValueError, rf"^plugins names {named}, which kept a lane .* labels replace it"):
                self.parse(plugins=[named], lanes={})
            self.assertNotIn(named, sys.modules)

    def test_the_same_lane_with_labels_is_taken(self):
        with patch.object(plugins, "LOADED", []):
            self.assertEqual(self.parse(lanes={"gate": {"command": ["true"], "labels": ["apple-m5-max"]}})
                             ["lanes"]["gate"]["labels"], ["apple-m5-max"])
