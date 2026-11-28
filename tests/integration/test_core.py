import contextlib
from datetime import datetime, timedelta, timezone
import fcntl
from io import StringIO
import json
import os
import pwd
import re
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src/python"))
from queuekeeper import cli, config, lane_results, offload_host, place_holder, processes, remote, resources
from queuekeeper_mobile import devices
from queuekeeper_mac_desktop import reservations
from queuekeeper_mac_desktop import display, foreground
from queuekeeper_apple.simulators import pool as simulator_pool
import host_isolation
from test_support import farm as farm_fixture
from host_isolation import stretched


def mine(limit, where="set in the environment"):
    """limit, such as HEAVY_SLOTS_MAX=3, as admission quotes it: saying it is this Mac's, by its name, and where it is
    set (vocaro/queuekeeper#29)."""
    from queuekeeper import offload_host
    return f"{limit} ({offload_host.mac_name()}'s limit, {where})"


def setUpModule():
    # qk doctor compares this Mac with another Mac. These tests configure none, so that it
    # never reaches the owner's; test_remote's doctor tests reach a fake one. It also checks the
    # account's agent skill links: these tests install no agent host, and test_skills installs some.
    nowhere = str(Path(tempfile.gettempdir()) / f"no-agent-host-{uuid.uuid4()}")
    environment = patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": nowhere,
                                          "CODEX_HOME": nowhere})
    environment.start()
    unittest.addModuleCleanup(environment.stop)


class ConfigurationTests(unittest.TestCase):
    def test_a_key_the_configuration_does_not_know_and_a_pool_with_no_name_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cases = [{"capabilities": ["simulator-pool"]}, {"toolkit": {"path": "../kit"}}, {"simulatorPool": " Example"},
                     {"simulatorPool": ""}, {"plugins": "queuekeeper_apple"}, {"plugins": ["not a module"]},
                     {"plugins": [{"path": "../kit/src/python"}]}, {"plugins": [{"module": "kit.hooks", "other": 1}]}]
            for case in cases:
                with self.subTest(case):
                    (root / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Example", **case}))
                    with self.assertRaises(ValueError): config.load(root)
            (root / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Example", "simulatorPool": "Example",
                "plugins": ["queuekeeper_apple", {"module": "kit.hooks", "path": "../kit/src/python"}]}))
            self.assertEqual(config.load(root)["simulatorPool"], "Example")
            self.assertEqual(config.plugins_of(root), [("queuekeeper_apple", None),
                                                          ("kit.hooks", str((root / "../kit/src/python").resolve()))])
            # A linked worktree's plugins are found from its main checkout, and a project that names none, or a
            # folder with no project, gets the default.
            self.assertEqual(config.plugins_of(root, root / "main")[1][1], str((root / "main/../kit/src/python").resolve()))
            # A queue follows its landings unless it says false (vocaro/queuekeeper#159); anything else is refused.
            for value, accepted in [(True, True), (False, True), ("yes", False)]:
                (root / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Example",
                    "lanes": {"gate": {"command": ["true"]}}, "queue": {"followLandings": value},
                    "plugins": ["queuekeeper_apple", {"module": "kit.hooks", "path": "../kit/src/python"}]}))
                with self.subTest(followLandings=value):
                    if accepted: self.assertIs(config.load(root)["queue"]["followLandings"], value)
                    else:
                        with self.assertRaises(ValueError): config.load(root)
            (root / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Example", "simulatorPool": "Example",
                "plugins": ["queuekeeper_apple", {"module": "kit.hooks", "path": "../kit/src/python"}]}))
            # The folders beside the project that its plugins load from, which another Mac links (sibling_folders).
            self.assertEqual(config.sibling_folders(root), ["kit"])
            self.assertEqual(config.sibling_variables(root), {"kit": None})
            self.assertEqual(config.sibling_folders(root / "none"), [])
            (root / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Example"}))
            self.assertEqual(config.plugins_of(root), [])
            self.assertEqual(config.plugins_of(root / "none"), [])

    def test_environment_inputs_accept_distinct_shell_variable_names_on_a_lane(self):
        def settings(names, group=False):
            lanes = {"purchases": {"command": ["./wrapper"]}}
            target = lanes.setdefault("group", {"parallel": ["purchases"]}) if group else lanes["purchases"]
            target["environmentInputs"] = names
            return config.parse(json.dumps({"schemaVersion": 1, "name": "Example", "lanes": lanes}))

        names = ["PROJECT_SIM_RUNTIME", "_wrapperFlag2"]
        self.assertEqual(settings(names)["lanes"]["purchases"]["environmentInputs"], names)
        invalid = (None, [], "SETTING", ["SETTING", "SETTING"], ["SETTING", {}], ["SETTING=value"],
                   ["SETTING*"], ["3SETTING"], ["SETTING\n"])
        for names in invalid:
            with self.subTest(names=names), self.assertRaisesRegex(ValueError, r"purchases\.environmentInputs"):
                settings(names)
        with self.assertRaisesRegex(ValueError, "Lane group group takes only"):
            settings(["PROJECT_SIM_RUNTIME"], group=True)

    def test_the_schema_lists_the_lane_and_group_keys_config_accepts(self):
        # vocaro-engineering#248: config took unlockedScreen, and lane groups, while the schema refused them.
        def drift(schema):
            entry = schema["properties"]["lanes"]["additionalProperties"]
            forms = {tuple(form["required"]): set(form["propertyNames"]["enum"]) for form in entry["oneOf"]}
            found = {"lane": (forms.get(("command",), set()), config.LANE_KEYS),
                     "group": (forms.get(("parallel",), set()), config.GROUP_KEYS),
                     "properties": (set(entry["properties"]), config.LANE_KEYS | config.GROUP_KEYS)}
            return {f"{where}: {sorted(listed - accepted)} extra, {sorted(accepted - listed)} missing"
                    for where, (listed, accepted) in found.items() if listed != accepted}
        schema = json.loads((config.TOOLKIT_ROOT / "schemas/queuekeeper.schema.json").read_text())
        self.assertEqual(drift(schema), set())
        entry = schema["properties"]["lanes"]["additionalProperties"]
        self.assertFalse(entry["additionalProperties"])
        self.assertEqual(entry["properties"]["unlockedScreen"]["type"], "boolean")
        # The negative controls: a key missing from the schema, or extra in it, is drift.
        for form in entry["oneOf"]:
            form["propertyNames"]["enum"].remove("unlockedScreen")
        del entry["properties"]["unlockedScreen"]
        self.assertEqual(drift(schema), {"lane: [] extra, ['unlockedScreen'] missing",
                                         "group: [] extra, ['unlockedScreen'] missing",
                                         "properties: [] extra, ['unlockedScreen'] missing"})
        entry["oneOf"][0]["propertyNames"]["enum"].append("jobs")
        self.assertIn("lane: ['jobs'] extra, ['unlockedScreen'] missing", drift(schema))
        # Config validates with the same sets: each form takes its keys and refuses the other's.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def load(lanes):
                (root / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Example",
                                                                   "lanes": lanes}))
                return config.load(root)
            lane = {"command": ["true"], "unlockedScreen": True}
            self.assertTrue(load({"one": lane, "group": {"parallel": ["one"], "jobs": 1, "unlockedScreen": True}})
                            ["lanes"]["group"]["unlockedScreen"])
            with self.assertRaisesRegex(ValueError, "Invalid lane one"): load({"one": {**lane, "jobs": 1}})
            with self.assertRaisesRegex(ValueError, "Lane group group takes only"):
                load({"one": lane, "group": {"parallel": ["one"], "command": ["true"], "unlockedScreen": True}})

    def test_lane_commands_are_argv_not_shell_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Example",
                "lanes": {"bad": {"command": "printf success"}}}))
            with self.assertRaises(ValueError): config.load(root)
            (root / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Example",
                "lanes": {"ui": {"command": ["xcodebuild", "test"], "foreground": "yes"}}}))
            with self.assertRaisesRegex(ValueError, "foreground must be true or false"): config.load(root)

    def test_a_model_lane_is_heavy_and_a_group_that_runs_one_holds_the_model(self):
        command = {"command": ["true"]}
        generate = {**command, "resource": "heavy", "model": True}
        invalid = [({"generate": {**generate, "model": "yes"}}, "generate.model must be true or false"),
                   ({"generate": {**command, "resource": "self", "model": True}}, "generate.model needs a heavy lane"),
                   ({"generate": {**generate, "resource": "exclusive"}}, "generate.model needs a heavy lane"),
                   ({"group": {"parallel": ["generate", "one"], "resource": "heavy"}},
                    "group runs generate under its own admission, so it needs \"model\": true"),
                   ({"group": {"parallel": ["generate"], "resource": "heavy", "model": 1}}, "group.model must be true or false"),
                   ({"group": {"parallel": ["generate"], "resource": "exclusive", "model": True}}, "group.model needs a heavy lane")]
        valid = [{}, {"group": {"parallel": ["generate", "one"], "resource": "heavy", "model": True}},
                 {"group": {"parallel": ["generate", "one"], "resource": "exclusive"}},
                 {"group": {"parallel": ["one"], "resource": "heavy", "model": True}},
                 {"build": {**command, "resource": "heavy", "model": False}}]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def configure(lanes):
                (root / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Example",
                    "lanes": {"one": command, "generate": generate, **lanes}}))
            for lanes, reason in invalid:
                with self.subTest(lanes):
                    configure(lanes)
                    with self.assertRaisesRegex(ValueError, re.escape(reason)): config.load(root)
            for lanes in valid:
                with self.subTest(lanes):
                    configure(lanes)
                    config.load(root)

    def test_a_whole_host_lane_is_heavy_and_a_group_that_runs_one_takes_the_host_too(self):
        # vocaro-engineering#168: a gate that runs the project's suites side by side takes every guaranteed slot.
        command = {"command": ["true"]}
        gate = {**command, "resource": "heavy", "wholeHost": True}
        invalid = [({"gate": {**gate, "wholeHost": "yes"}}, "gate.wholeHost must be true or false"),
                   ({"gate": {**command, "resource": "self", "wholeHost": True}}, "gate.wholeHost needs a heavy lane"),
                   ({"gate": {**gate, "resource": "exclusive"}}, "gate.wholeHost needs a heavy lane"),
                   ({"group": {"parallel": ["gate", "one"], "resource": "heavy"}},
                    "group runs gate under its own admission, so it needs \"wholeHost\": true"),
                   ({"group": {"parallel": ["gate"], "resource": "heavy", "wholeHost": 1}}, "group.wholeHost must be true or false"),
                   ({"group": {"parallel": ["gate"], "resource": "exclusive", "wholeHost": True}}, "group.wholeHost needs a heavy lane")]
        valid = [{}, {"group": {"parallel": ["gate", "one"], "resource": "heavy", "wholeHost": True}},
                 {"group": {"parallel": ["gate", "one"], "resource": "exclusive"}},
                 {"group": {"parallel": ["one"], "resource": "heavy", "wholeHost": True}},
                 {"build": {**command, "resource": "heavy", "wholeHost": False}}]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def configure(lanes):
                (root / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Example",
                    "lanes": {"one": command, "gate": gate, **lanes}}))
            for lanes, reason in invalid:
                with self.subTest(lanes):
                    configure(lanes)
                    with self.assertRaisesRegex(ValueError, re.escape(reason)): config.load(root)
            for lanes in valid:
                with self.subTest(lanes):
                    configure(lanes)
                    loaded = config.load(root)
                    self.assertTrue(config.whole_host_lane(loaded["lanes"], "gate"))
                    self.assertFalse(config.whole_host_lane(loaded["lanes"], "one"))
        schema = json.loads((config.TOOLKIT_ROOT / "schemas/queuekeeper.schema.json").read_text())
        self.assertEqual(schema["properties"]["lanes"]["additionalProperties"]["properties"]["wholeHost"]["type"], "boolean")

    def test_every_resource_but_self_is_admitted_and_light_means_heavy(self):
        # The owner's decision, 2026-11-07 (vocaro-engineering#200): light and no resource load, as heavy work.
        command = {"command": ["true"]}
        for lane, admission in [({**command}, "heavy"), ({**command, "resource": "light"}, "heavy"),
                                ({**command, "resource": "heavy"}, "heavy"),
                                ({**command, "resource": "exclusive"}, "exclusive"),
                                ({**command, "resource": "self"}, "self")]:
            with self.subTest(lane=lane):
                self.assertEqual(config.admission(lane), admission)
                self.assertEqual(config.unadmitted(lane), admission == "self")
        invalid = [({"one": {**command, "resource": "medium"}}, "Invalid resource for one: 'medium'"),
                   ({"one": {**command, "resource": "self"}, "group": {"parallel": ["one"], "resource": "heavy"}},
                    "group runs one under its own admission, inside which one's script could not"),
                   ({"one": command, "group": {"parallel": ["one"], "resource": "self"}},
                    "group runs its lanes under its one admission, so it cannot be a self lane"),
                   ({"one": {**command, "resource": "exclusive"}, "group": {"parallel": ["one"], "resource": "light"}},
                    "group runs one under its own admission, so it needs a resource of at least exclusive"),
                   ({"one": {**command, "resource": "self", "wholeHost": True}}, "one.wholeHost needs a heavy lane")]
        # A light group runs heavy lanes under its admission, a light one being heavy; a light lane may say wholeHost.
        valid = [{"one": {**command, "resource": "heavy"}, "group": {"parallel": ["one"], "resource": "light"}},
                 {"one": {**command, "resource": "light", "wholeHost": True, "here": "a device"}}]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for lanes, reason in invalid:
                with self.subTest(reason):
                    (root / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Example",
                                                                       "lanes": lanes}))
                    with self.assertRaisesRegex(ValueError, re.escape(reason)): config.load(root)
            for lanes in valid:
                with self.subTest(lanes):
                    (root / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Example",
                                                                       "lanes": lanes}))
                    config.load(root)

    def test_a_display_lane_is_admitted_and_a_group_takes_the_display_from_its_lanes(self):
        command = {"command": ["true"]}
        mac = {**command, "resource": "heavy", "display": True}
        invalid = [({"mac": {**mac, "display": "yes"}}, "mac.display must be true or false"),
                   ({"mac": {**mac, "resource": "self"}}, "mac.display needs an admitted lane"),
                   ({"group": {"parallel": ["one"], "resource": "heavy", "display": True}},
                    "Lane group group takes only parallel")]
        # A light lane, and one that gives no resource, are admitted as heavy work (the owner, 2026-11-07).
        valid = [{"mac": mac}, {"mac": {**mac, "resource": "exclusive"}}, {"mac": {**mac, "display": False}},
                 {"mac": {**command, "display": True}}, {"mac": {**mac, "resource": "light"}},
                 {"mac": {**command, "display": False}},
                 {"mac": mac, "group": {"parallel": ["mac", "one"], "resource": "heavy"}}]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def configure(lanes):
                (root / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Example",
                    "lanes": {"one": command, **lanes}}))
            for lanes, reason in invalid:
                with self.subTest(lanes):
                    configure(lanes)
                    with self.assertRaisesRegex(ValueError, re.escape(reason)): config.load(root)
            for lanes in valid:
                with self.subTest(lanes):
                    configure(lanes)
                    config.load(root)
        schema = json.loads((config.TOOLKIT_ROOT / "schemas/queuekeeper.schema.json").read_text())
        self.assertEqual(schema["properties"]["lanes"]["additionalProperties"]["properties"]["display"]["type"], "boolean")

    def test_a_group_that_runs_several_model_lanes_runs_them_one_at_a_time(self):
        # Its one model lock cannot keep its own lanes from driving the model at once (#28).
        generate = {"command": ["true"], "resource": "heavy", "model": True}
        lanes = {"one": {"command": ["true"]}, "generate": generate, "again": generate}
        reason = "group runs the model lanes generate, again, which would drive the model at once, so it needs \"jobs\": 1"
        invalid = [{"parallel": ["generate", "again", "one"], "resource": "heavy", "model": True},
                   {"parallel": ["generate", "again", "one"], "resource": "exclusive", "jobs": 2}]
        valid = [{"parallel": ["generate", "again", "one"], "resource": "heavy", "model": True, "jobs": 1},
                 {"parallel": ["generate", "one"], "resource": "heavy", "model": True}]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def configure(group):
                (root / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Example",
                    "lanes": {**lanes, "group": group}}))
            for group in invalid:
                with self.subTest(group):
                    configure(group)
                    with self.assertRaisesRegex(ValueError, re.escape(reason)): config.load(root)
            for group in valid:
                with self.subTest(group):
                    configure(group)
                    config.load(root)

    def test_missing_prerequisites_are_reported_without_starting_work(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Example"}))
            with patch.object(cli.shutil, "which", return_value=None), patch.object(cli, "source_identity", return_value={}), contextlib.redirect_stdout(StringIO()):
                self.assertEqual(cli.main(["--project", directory, "doctor"]), 1)

    def test_doctor_lists_the_lanes_that_rely_on_the_default_time_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            lanes = {"tests": {"command": ["true"]}, "docs": {"command": ["true"]},
                     "gate": {"command": ["true"], "timeout": 14400}, "all": {"parallel": ["tests", "gate"]}}
            (Path(directory) / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Example",
                "integration": "direct-main", "lanes": lanes}))
            with patch.object(cli.shutil, "which", return_value="/usr/bin/true"), \
                    patch.object(cli, "source_identity", return_value={}), contextlib.redirect_stdout(StringIO()) as output:
                self.assertEqual(cli.main(["--project", directory, "doctor"]), 0)
            warnings = [w for w in json.loads(output.getvalue())["warnings"] if "timeout" in w]
            self.assertEqual(len(warnings), 1, warnings)
            self.assertIn(f"2 lane(s) declare no timeout and are stopped, unmeasured, after queuekeeper's default "
                          f"{processes.DEFAULT_LIMIT}s: docs, tests;", warnings[0])

    def test_doctor_warns_while_xcode_default_simulators_exist(self):
        default, member = "11111111-2222-3333-4444-555555555555", "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"
        listing = {"devices": {"com.apple.CoreSimulator.SimRuntime.iOS-27-2": [
            {"udid": default, "name": "iPhone 17"}, {"udid": member, "name": "Example iPhone 17 Pro 1"}]}}
        def simctl(*arguments, timeout=600):
            self.assertEqual(arguments, ("list", "devices", "-j"))
            return subprocess.CompletedProcess(arguments, 0, json.dumps(listing), "")
        cases = [(["simulator-pool"], {default}, True), (["simulator-pool"], {"FFFFFFFF-0000-0000-0000-000000000000"}, False),
                 (["simulator-pool"], set(), False), (["measurement"], {default}, False)]
        with tempfile.TemporaryDirectory() as directory:
            for capabilities, defaults, warned in cases:
                with self.subTest(capabilities=capabilities, defaults=defaults):
                    (Path(directory) / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Example",
                        "integration": "direct-main",
                        **({"simulatorPool": "Example"} if "simulator-pool" in capabilities else {})}))
                    with patch.object(cli.shutil, "which", return_value="/usr/bin/true"), patch.object(cli, "source_identity", return_value={}), \
                            patch.object(simulator_pool, "simctl", side_effect=simctl), \
                            patch.object(simulator_pool, "default_devices", return_value=frozenset(defaults)), \
                            contextlib.redirect_stdout(StringIO()) as output:
                        self.assertEqual(cli.main(["--project", directory, "doctor"]), 0)
                    found = [w for w in json.loads(output.getvalue())["warnings"] if "default simulators" in w]
                    self.assertEqual(bool(found), warned, found)
                    if warned:
                        self.assertIn(f"1 of Xcode's default simulators exist on this Mac: iPhone 17 (iOS-27-2, {default})",
                                      found[0])
                        self.assertNotIn(member, found[0])

    def test_doctor_warns_when_a_direct_main_project_names_no_gate(self):
        lane = {"command": ["true"], "timeout": 60}
        cases = [({"integration": "direct-main", "lanes": {"check": lane}}, True),
                 ({"integration": "direct-main", "lanes": {"gate": lane}}, False),
                 ({"integration": "direct-main", "lanes": {"check": lane}, "queue": {"lanes": ["check"]}}, False),
                 ({"integration": "pull-request", "lanes": {"check": lane}}, False),
                 ({"integration": "direct-main"}, False)]
        with tempfile.TemporaryDirectory() as directory:
            for declared, warned in cases:
                with self.subTest(declared=declared):
                    (Path(directory) / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Example",
                        **declared}))
                    # What a pull-request project needs of gh is test_pull_requests's, against a stand-in: here gh
                    # is never asked (vocaro/queuekeeper#404).
                    from queuekeeper import pull_requests
                    with patch.object(cli.shutil, "which", return_value="/usr/bin/true"), patch.object(cli, "source_identity", return_value={}), \
                            patch.object(pull_requests, "doctor_errors", return_value=[]), contextlib.redirect_stdout(StringIO()) as output:
                        self.assertEqual(cli.main(["--project", directory, "doctor"]), 0)
                    found = [w for w in json.loads(output.getvalue())["warnings"] if "named gate" in w]
                    self.assertEqual(bool(found), warned, found)

    def test_release_settings_are_checked(self):
        invalid = [({"lanes": "device"}, "release.lanes must list"), ({"lanes": ["device", "device"]}, "release.lanes"),
                   ({"lanes": [""]}, "release.lanes"), ({"lanes": ["gate"]}, "never gate"),
                   ({"maxCommits": 0}, "maxCommits"), ({"maxCommits": True}, "maxCommits"),
                   ({"maxDays": 0}, "release.maxDays"), ({"maxDays": "14"}, "release.maxDays"),
                   ({"limits": []}, "release.limits must map"),
                   ({"lanes": ["device"], "limits": {"corpus": {"maxDays": 30}}}, "names corpus, which is not in"),
                   ({"lanes": ["device"], "limits": {"device": {"budget": 1}}}, "release.limits.device supports only"),
                   ({"lanes": ["device"], "limits": {"device": 30}}, "release.limits.device supports only"),
                   ({"lanes": ["device"], "limits": {"device": {"maxDays": 0}}}, "release.limits.device.maxDays"),
                   ({"lanes": ["device"], "limits": {"device": {"maxCommits": False}}},
                    "release.limits.device.maxCommits"),
                   ({"budget": 1}, "release supports only"), ([], "release supports only")]
        valid = [{}, {"lanes": []}, {"lanes": ["device", "corpus"], "maxCommits": 50},
                 {"lanes": ["device", "corpus"], "maxCommits": 50, "maxDays": 14,
                  "limits": {"device": {"maxCommits": 200, "maxDays": 30}, "corpus": {"maxDays": 7}}},
                 {"lanes": ["device"], "limits": {}}]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for declared, reason in invalid:
                with self.subTest(declared=declared):
                    (root / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Example",
                                                                       "release": declared}))
                    with self.assertRaisesRegex(ValueError, re.escape(reason)): config.load(root)
            for declared in valid:
                with self.subTest(declared=declared):
                    (root / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Example",
                                                                       "release": declared}))
                    self.assertEqual(config.load(root)["release"], declared)
            # Queuekeeper applies no maxCommits or maxDays of its own: one left out is the toolkit's to choose.
            (root / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Example",
                                                               "release": {"lanes": ["device"]}}))
            self.assertNotIn("maxCommits", config.load(root)["release"])
        schema = json.loads((config.TOOLKIT_ROOT / "schemas/queuekeeper.schema.json").read_text())
        release = schema["properties"]["release"]
        self.assertEqual(set(release["properties"]), {"lanes", "maxCommits", "maxDays", "limits"})
        self.assertNotIn("default", release["properties"]["maxCommits"])
        self.assertEqual(set(release["properties"]["limits"]["additionalProperties"]["properties"]),
                         {"maxCommits", "maxDays"})

    def test_a_release_lane_is_no_lane_a_landing_runs(self):
        lane = {"command": ["true"], "timeout": 60}
        heavy = {"command": ["true"], "timeout": 60, "resource": "heavy"}
        # Each a release lane that a landing would run too, and what the error says runs it.
        landing = [
            ({"lanes": {"gate": lane, "lint": lane}}, {"preflight": ["lint"]}, "lint", "before the gate"),
            ({"lanes": {"gate": lane, "provision": lane}}, {}, "provision", "before the gate"),
            ({"lanes": {"gate": lane, "app-gate": lane}}, {"lanes": ["gate", "app-gate"]}, "app-gate",
             "one of queue.lanes"),
            ({"lanes": {"gate": lane, "docs": lane}}, {"unread": {"paths": {"guides/": "prose"}}},
             "docs", "docs-only batch"),
            ({"lanes": {"tests": lane, "device": heavy, "gate": {"parallel": ["tests", "device"], "resource": "heavy"}}},
             {}, "device", "lane group gate"),
            ({"lanes": {"gate": lane, "device": heavy, "tier": {"parallel": ["device"], "resource": "heavy"}}},
             {"lanes": ["gate", "tier"]}, "device", "lane group tier"),
        ]
        # The negative controls: the same lanes with the release lane outside what a landing runs.
        apart = [
            ({"lanes": {"gate": lane, "device": heavy}}, {}, "device"),
            ({"lanes": {"gate": lane, "app-gate": lane, "device": heavy}}, {"lanes": ["gate", "app-gate"]}, "device"),
            ({"lanes": {"gate": lane, "docs": lane, "device": heavy}},
             {"unread": {"paths": {"guides/": "prose"}}}, "device"),
            # A group that no landing runs may hold a release lane, and gate may stand beside it.
            ({"lanes": {"gate": lane, "device": heavy, "corpus": heavy,
                        "release-pass": {"parallel": ["device", "corpus"], "resource": "heavy"}}}, {}, "release-pass"),
            ({"lanes": {"gate": lane, "device": heavy, "corpus": heavy,
                        "release-pass": {"parallel": ["device", "corpus"], "resource": "heavy"}}}, {}, "corpus"),
            # Queue lanes replace gate, so a project whose queue runs others may name gate's old lane.
            ({"lanes": {"gate": lane, "fast": lane, "device": heavy}}, {"lanes": ["fast"]}, "device"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def write(lanes, queue, named):
                (root / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Example", **lanes,
                    **({"queue": queue} if queue else {}), "release": {"lanes": [named]}}))
            for lanes, queue, named, why in landing:
                with self.subTest(lanes=lanes, queue=queue, named=named):
                    write(lanes, queue, named)
                    with self.assertRaisesRegex(ValueError, f"release.lanes names {named}, which a landing runs too: "
                                                f".*{re.escape(why)}.*out of release.lanes$"):
                        config.load(root)
            for lanes, queue, named in apart:
                with self.subTest(lanes=lanes, queue=queue, named=named):
                    write(lanes, queue, named)
                    self.assertEqual(config.load(root)["release"], {"lanes": [named]})

    def test_landing_lanes_accounts_for_provision_even_when_the_gate_runs_it(self):
        from queuekeeper import merge_queue
        lanes = {name: {"command": ["true"], "timeout": 60} for name in ("lint", "format", "provision", "gate")}
        for queue in ({"preflight": ["format", "lint"]}, {"preflight": []}):
            with self.subTest(queue=queue):
                project = {"name": "p", "lanes": lanes, "queue": queue}
                landing = list(config.landing_lanes(lanes, queue))
                # Validation counts every lane a landing can run. A normal gate provisions its own checkout.
                before_docs = landing[:landing.index("gate") + 1]
                self.assertEqual([lane for lane in before_docs if lane != "provision"],
                                 merge_queue.lanes_of(project, ["src/app.py"]))
                self.assertEqual(before_docs, merge_queue.lanes_of(project, ["queuekeeper.json"]))

    def test_doctor_warns_of_a_self_lane_that_calls_eng_admit_nowhere(self):
        # BreezEPUB's script and PDFReflowLib's calibration admit their parts as these do (#200).
        shell = "#!/bin/bash\n# runs unadmitted\npython3 scripts/qk admit --exclusive -- xcodebuild test\n"
        python = 'queuekeeper = [sys.executable, "scripts/qk"]\nrun([*queuekeeper, "admit", "--", "swiftc", "probe.swift"])\n'
        bare = "#!/bin/bash\n# qk run starts this lane unadmitted\nxcodebuild test\n"
        cases = [(["bash", "scripts/step.sh"], shell, False), (["python3", "scripts/step.sh"], python, False),
                 (["sh", "-c", "python3 scripts/qk admit -- swift test"], None, False),
                 (["bash", "scripts/step.sh"], bare, True), (["xcodebuild", "test"], None, True),
                 # An interpreter named by its absolute path is no file of the checkout to read (test_remote's probe).
                 ([sys.executable, "scripts/step.sh"], shell, False), ([sys.executable, "-c", "pass"], None, True)]
        for command, text, warned in cases:
            with self.subTest(command=command, text=text), tempfile.TemporaryDirectory() as directory:
                if text is not None:
                    (Path(directory) / "scripts").mkdir()
                    (Path(directory) / "scripts/step.sh").write_text(text)
                (Path(directory) / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Example",
                    "integration": "direct-main", "lanes": {
                        "gate": {"command": ["true"], "timeout": 60},
                        "step": {"command": command, "resource": "self", "timeout": 60},
                        # A heavy lane that admits nothing is admitted by qk run, and is no self lane's concern.
                        "plain": {"command": ["xcodebuild", "test"], "resource": "heavy", "timeout": 60}}}))
                with patch.object(cli.shutil, "which", return_value="/usr/bin/true"), \
                        patch.object(cli, "source_identity", return_value={}), \
                        contextlib.redirect_stdout(StringIO()) as output:
                    self.assertEqual(cli.main(["--project", directory, "doctor"]), 0)
                found = [warning for warning in json.loads(output.getvalue())["warnings"] if "self lane" in warning]
                self.assertEqual(len(found), int(warned), found)
                if warned:
                    self.assertIn("step is a self lane", found[0])
                    self.assertIn("scripts/step.sh" if text else "any file of the checkout", found[0])

    def test_doctor_warns_of_a_display_lane_that_does_not_say_whether_it_needs_an_unlocked_screen(self):
        # EPUBLib's swift-tests said "display": true alone, and the queue sent its gate to a locked Mac three times,
        # each run unmeasured (#293). An explicit false says its tests measure behind a lock.
        suite = {"command": ["swift", "test"], "timeout": 60}
        cases = [({"display": True}, {}, ["suite"]),
                 ({"display": True}, {"gate": {"parallel": ["suite", "docs"]}}, ["suite", "group gate"]),
                 ({"display": True, "unlockedScreen": True}, {"gate": {"parallel": ["suite", "docs"],
                                                                       "unlockedScreen": True}}, []),
                 ({"display": True, "unlockedScreen": False}, {"gate": {"parallel": ["suite", "docs"]}}, []),
                 ({"display": False}, {}, []), ({}, {"gate": {"parallel": ["suite", "docs"]}}, []),
                 ({"unlockedScreen": True}, {}, [])]
        for keys, groups, warned in cases:
            with self.subTest(keys=keys, groups=sorted(groups)), tempfile.TemporaryDirectory() as directory:
                lanes = {"suite": {**suite, **keys}, "docs": {"command": ["true"], "timeout": 60}, **groups}
                lanes.setdefault("gate", {"command": ["true"], "timeout": 60})
                (Path(directory) / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Example",
                    "integration": "direct-main", "lanes": lanes}))
                with patch.object(cli.shutil, "which", return_value="/usr/bin/true"), \
                        patch.object(cli, "source_identity", return_value={}), \
                        contextlib.redirect_stdout(StringIO()) as output:
                    # A warning is not a missing prerequisite.
                    self.assertEqual(cli.main(["--project", directory, "doctor"]), 0)
                found = [w for w in json.loads(output.getvalue())["warnings"] if "unlockedScreen" in w]
                self.assertEqual(len(found), int(bool(warned)), found)
                if warned:
                    self.assertTrue(found[0].startswith('suite says "display": true'), found[0])
                    self.assertIn('it needs "unlockedScreen": true', found[0])
                    self.assertIn('say "unlockedScreen": false', found[0])
                    self.assertEqual("group that runs it, gate," in found[0], "group gate" in warned, found[0])

    def test_lane_arguments_reach_its_command_and_a_filtered_run_needs_a_test_or_its_expected_count(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # Reports one executed test for each argument it receives.
            count = [sys.executable, "-c", "import sys; print('Ran', len(sys.argv) - 1, 'tests')"]
            (root / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Lanes", "lanes": {
                "test": {"command": count, "minimumTests": 5}, "check": {"command": [sys.executable, "-c", "pass"]}}}))
            cases = [(["test"], 2, None),  # the whole lane still needs its minimumTests
                     (["test", "--", "a", "b"], 0, ["a", "b"]),
                     (["test", "--expect", "2", "--", "a", "b"], 0, ["a", "b"]),
                     (["--expect", "2", "test", "--", "a", "b"], 0, ["a", "b"]),
                     (["test", "--expect", "3", "--", "a", "b"], 2, ["a", "b"]),
                     (["test", "--expect", "1", "--", "a", "b"], 2, ["a", "b"]),
                     (["test", "--", "a", "--", "b"], 0, ["a", "--", "b"]),
                     (["test", "--expect", "0", "--", "a"], 2, "not run"),
                     (["check", "--", "x"], 0, ["x"])]  # a lane without minimumTests counts no tests
            for arguments, status, passed in cases:
                with self.subTest(arguments):
                    receipt = root / ".build/queuekeeper" / arguments[arguments[0] == "--expect" and 2] / "source.json"
                    receipt.unlink(missing_ok=True)
                    with patch.object(cli, "source_identity", return_value={}), contextlib.redirect_stderr(StringIO()) as error:
                        self.assertEqual(cli.main(["--project", directory, "run", *arguments]), status, error.getvalue())
                    if passed == "not run":
                        self.assertFalse(receipt.exists())
                        continue
                    recorded = json.loads(receipt.read_text())
                    self.assertEqual(recorded["arguments"], passed or [])
                    self.assertEqual(recorded["command"][3:], passed or [])
            self.assertIn("Expected exactly 3 executed tests; observed 2", self.run_lane(directory, "test", "--expect", "3", "--", "a", "b"))
            for arguments in [["run", "test", "-k", "x"], ["doctor", "--", "x"]]:
                with self.subTest(arguments), self.assertRaises(SystemExit), contextlib.redirect_stderr(StringIO()):
                    cli.main(["--project", directory, *arguments])

    def test_an_expect_mismatch_says_the_counts_executed_and_skipped_and_that_skipped_tests_do_not_count(self):
        """On 2026-11-15 three of ten EPUBLib subagents counted a filter's skipping tests in --expect: 675 asked for 673
        executed and 2 skipped. Each run ended unmeasured or failed, and was run again without the output saying which
        count --expect meant (vocaro/queuekeeper#181)."""
        with tempfile.TemporaryDirectory() as directory:
            # unittest's lines for one test per argument, those named skip skipped.
            count = [sys.executable, "-c", "import sys; n = len(sys.argv) - 1; s = sys.argv.count('skip'); "
                     "print('Ran', n, 'tests'); print(f'OK (skipped={s})' if s else 'OK')"]
            series = [sys.executable, "-c", "print('Repeat series: asked 2 repeats of X (2 runs); runs: 2 passed'); "
                      "print('Ran 6 tests'); print('OK')"]
            (Path(directory) / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Lanes", "lanes": {
                "test": {"command": count, "minimumTests": 1}, "series": {"command": series, "minimumTests": 1}}}))
            rule = "counts executed tests, not skipped or canceled ones"
            cases = [(["--expect", "3", "test", "--", "a", "b", "skip"], lane_results.UNMEASURED,
                      [f"--expect 3 {rule}: 2 executed and 1 skipped, 3 selected, 1 fewer executed than the 3 expected. "
                       "The 1 skipped did not run, so the run is unmeasured: if they are meant to skip here, --expect 2 "
                       "counts the tests that ran"]),
                     (["--expect", "4", "test", "--", "a", "b", "skip"], 2,
                      [f"--expect 4 {rule}: 2 executed and 1 skipped, 3 selected, 2 fewer executed than the 4 expected. "
                       "If these are the tests meant, --expect 2 is their count"]),
                     (["--expect", "1", "test", "--", "a", "b"], 2,
                      [f"--expect 1 {rule}: 2 executed, 1 more executed than the 1 expected. If these are the tests "
                       "meant, --expect 2 is their count"]),
                     (["--expect", "1", "test", "--", "skip"], lane_results.UNMEASURED,
                      [f"--expect 1 {rule}: 0 executed and 1 skipped, 1 selected, 1 fewer executed than the 1 expected"]),
                     (["--repeat", "2", "--expect", "4", "series"], 2,
                      [f"--expect 4 counts each run's executed tests, not skipped or canceled ones: 6 executed over 2 "
                       "runs, 2 fewer executed than the 8 expected. If these are the tests meant, --expect 3 is their "
                       "count"])]
            for arguments, code, said in cases:
                with self.subTest(arguments), patch.object(cli, "source_identity", return_value={}), \
                        contextlib.redirect_stderr(StringIO()) as error:
                    self.assertEqual(cli.main(["--project", directory, "run", "--here", *arguments]), code, error.getvalue())
                    for words in said: self.assertIn(words, error.getvalue())
                    lane = "series" if "series" in arguments else "test"
                    recorded = json.loads((Path(directory) / ".build/queuekeeper" / lane / "source.json").read_text())
                    cause = recorded["unmeasured"][0] if code == lane_results.UNMEASURED else recorded["error"]
                    self.assertIn(said[0], cause)

    def test_a_lane_that_says_its_count_is_exact_fails_when_it_runs_more_tests_than_its_minimum(self):
        """BreezEPUB's navigation-keys had a minimumTests of 1 and ran 5, so four of its tests could have stopped
        running with nothing said, and a test another landing added left seven lanes' floors short
        (vocaro/queuekeeper#77)."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # Reports one executed test for each argument it receives, one of them skipped where an argument is "skip".
            count = [sys.executable, "-c", "import sys; n = len(sys.argv) - 1; "
                     "print('Ran', n, 'tests\\n\\nOK' + (' (skipped=1)' if 'skip' in sys.argv else ''))"]
            def lane(*selected, **keys):
                return {"command": [*count, *selected], "minimumTests": 2, **keys}
            (root / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Lanes", "lanes": {
                "floor": lane("a", "b", "c"), "exact": lane("a", "b", exactTests=True),
                "grown": lane("a", "b", "c", exactTests=True), "shrunk": lane("a", exactTests=True),
                "skipped": lane("a", "b", "skip", exactTests=True), "unasked": lane("a", "b", "c", exactTests=False),
                "group": {"parallel": ["grown"]}}}))
            cases = [(["floor"], 0, "passed"),  # a floor alone still passes whatever runs above it
                     (["unasked"], 0, "passed"),
                     (["exact"], 0, "passed"),
                     (["grown"], 2, "failed"),
                     (["shrunk"], 2, "failed"),
                     (["skipped"], 0, "passed"),  # a skipped test ran nothing, and two ran
                     (["grown", "--", "d"], 0, "passed"),  # arguments select part of the lane, which needs one test
                     (["grown", "--expect", "4", "--", "d"], 0, "passed"),
                     (["group"], 1, "failed")]  # a group's member is judged as it is alone, and fails its group
            for arguments, status, outcome in cases:
                with self.subTest(arguments):
                    with patch.object(cli, "source_identity", return_value={}), contextlib.redirect_stderr(StringIO()) as error:
                        self.assertEqual(cli.main(["--project", directory, "run", *arguments]), status, error.getvalue())
                    recorded = json.loads((root / ".build/queuekeeper" / arguments[0] / "source.json").read_text())
                    self.assertEqual(recorded["outcome"], outcome)
            said = self.run_lane(directory, "grown")
            self.assertIn('queuekeeper: Expected exactly 2 executed tests; observed 3. The lane says "exactTests": true, so '
                          "its minimumTests, 2, is the count of the tests it selects: if these 3 are the tests it should "
                          "run, set its minimumTests to 3, and otherwise correct what it selects\n", said)
            recorded = json.loads((root / ".build/queuekeeper/grown/source.json").read_text())
            self.assertEqual(recorded["exitStatus"], 0)
            self.assertIn("Expected exactly 2 executed tests; observed 3", recorded["error"])
            self.assertIn("Expected exactly 2 executed tests; observed 1. The lane says", self.run_lane(directory, "shrunk"))
            # The key is true or false, and makes exact the minimumTests the lane gives.
            for keys, message in (({"minimumTests": 2, "exactTests": "yes"}, "gate.exactTests must be true or false"),
                                  ({"exactTests": True}, "gate.exactTests needs the lane's minimumTests"),
                                  ({"exactTests": False}, None)):
                (root / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Lanes", "lanes": {
                    "gate": {"command": count, **keys}}}))
                with self.subTest(keys):
                    if message is None: config.load(root)
                    else:
                        with self.assertRaisesRegex(ValueError, message): config.load(root)
            (root / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Lanes", "lanes": {
                "gate": lane(), "group": {"parallel": ["gate"], "exactTests": True}}}))
            with self.assertRaisesRegex(ValueError, "Lane group group takes only"): config.load(root)

    def test_a_lane_with_no_minimum_still_fails_a_run_whose_counted_tests_all_skipped_or_none_ran(self):
        """A lane needs no minimumTests to catch a gate that tested nothing: a log that reports a count must report a
        test that ran. A lane whose log reports no count, such as a spelling check, is held to none."""
        with tempfile.TemporaryDirectory() as directory:
            def lane(output):
                return {"command": [sys.executable, "-c", f"print({output!r})"]}
            (Path(directory) / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Lanes", "lanes": {
                "none": lane("Ran 0 tests"), "skipped": lane("Ran 2 tests\n\nOK (skipped=2)"),
                "ran": lane("Ran 2 tests\n\nOK (skipped=1)"), "uncounted": lane("no misspellings"),
                # cargo test's line for each test binary (vocaro/queuekeeper#366).
                "cargo": lane("test result: ok. 3 passed; 0 failed; 1 ignored; 0 measured; 0 filtered out\n"
                              "test result: ok. 0 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out"),
                "cargo-none": lane("test result: ok. 0 passed; 0 failed; 0 ignored; 0 measured; 2 filtered out")}}))
            for name, status, said in [("none", 2, "Expected at least 1 executed tests; observed 0"),
                                       ("cargo-none", 2, "Expected at least 1 executed tests; observed 0"),
                                       ("cargo", 0, None),
                                       ("skipped", 2, "observed 0 (2 skipped, which do not count)"),
                                       ("ran", 0, None), ("uncounted", 0, None)]:
                with self.subTest(name), patch.object(cli, "source_identity", return_value={}), \
                        contextlib.redirect_stderr(StringIO()) as error:
                    self.assertEqual(cli.main(["--project", directory, "run", name]), status, error.getvalue())
                    recorded = json.loads((Path(directory) / ".build/queuekeeper" / name / "source.json").read_text())
                    if said: self.assertIn(said, recorded["error"])
                    # The receipt keeps the counts the log reports, which a later run is held to (count_history).
                    if name == "ran": self.assertEqual(recorded["tests"], {"executed": 1, "skipped": 1})
                    if name == "cargo": self.assertEqual(recorded["tests"], {"executed": 3, "skipped": 1})
                    if name == "uncounted": self.assertNotIn("tests", recorded)

    def test_a_whole_run_that_executed_fewer_tests_than_its_last_pass_on_main_fails_without_a_minimum(self):
        """Projects committed about 100 edits to their lanes' minimumTests in seven weeks, the floor following the
        tests. A whole run is held to the count of its lane's last passing whole run on main on this Mac instead."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def git(*arguments):
                return subprocess.run(["git", "-C", directory, "-c", "user.name=Test", "-c", "user.email=t@example.com",
                                       *arguments], check=True, capture_output=True, text=True).stdout.strip()
            git("init", "-q", "-b", "main")
            (root / ".gitignore").write_text(".build/\n")
            count = [sys.executable, "-c", "import pathlib; print('Ran', pathlib.Path('count.txt').read_text().strip(), 'tests')"]
            # cargo test's line for each test binary: the count's tests in one, none in a documentation test's.
            cargo = [sys.executable, "-c", "import pathlib; n = pathlib.Path('count.txt').read_text().strip(); "
                     "line = 'test result: ok. {} passed; 0 failed; 0 ignored; 0 measured; 0 filtered out'; "
                     "print(line.format(n)); print(line.format(0))"]
            (root / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Lanes", "lanes": {
                "test": {"command": count}, "exact": {"command": count, "minimumTests": 4, "exactTests": True},
                "cargo": {"command": cargo}}}))
            def landed(tests, message="state"):
                (root / "count.txt").write_text(f"{tests}\n")
                git("add", "-A")
                git("commit", "-q", "--allow-empty", "-m", message)
                return git("rev-parse", "HEAD")
            def run(*arguments):
                with patch.object(cli, "refresh_policy", return_value="policy"), \
                        contextlib.redirect_stderr(StringIO()) as error:
                    status = cli.main(["--project", directory, "run", *arguments])
                lane = arguments[0]
                return status, error.getvalue(), json.loads((root / ".build/queuekeeper" / lane / "source.json").read_text())
            first = landed(5)
            # The pass kept by commit, as keep_here keeps one where the toolkit has no working changes.
            for lane in ("test", "exact", "cargo"):
                (remote.store(root) / first).mkdir(parents=True, exist_ok=True)
                (remote.store(root) / first / f"{lane}.json").write_text(json.dumps({
                    "ranOn": remote.HERE, "commit": first, "head": first, "lane": lane, "arguments": [],
                    "result": {"exitStatus": 0, "outcome": "passed", "stopped": False,
                               "receipt": {"mac": offload_host.mac_name(), "tests": {"executed": 5, "skipped": 0}}}}))
            landed(4)
            status, said, recorded = run("test")
            self.assertEqual((status, recorded["outcome"]), (2, "failed"), said)
            words = (f"test executed 4 tests, 1 fewer than the 5 its last passing whole run on {offload_host.mac_name()} "
                     f"executed at {first[:12]}")
            self.assertIn(words, said)
            self.assertIn("add the trailer `Tests-Removed: test` to a commit after", recorded["error"])
            self.assertEqual(recorded["tests"], {"executed": 4, "skipped": 0})
            # cargo test's count is held to its last pass alike (vocaro/queuekeeper#366).
            status, said, recorded = run("cargo")
            self.assertEqual((status, recorded["outcome"], recorded["tests"]), (2, "failed", {"executed": 4, "skipped": 0}),
                             said)
            self.assertIn("cargo executed 4 tests, 1 fewer than the 5", said)
            # A run with arguments selects part of the lane, and is compared with no whole run.
            self.assertEqual(run("test", "--", "x")[0], 0)
            # A lane that says its count is exact is held to that count alone.
            self.assertEqual(run("exact")[0], 0)
            # Additions need nothing.
            landed(9)
            self.assertEqual(run("test")[0], 0)
            # A commit that says its tests were removed lets the count fall.
            landed(4, "Remove a test\n\nTests-Removed: test")
            status, said, recorded = run("test")
            self.assertEqual((status, recorded["outcome"]), (0, "passed"), said)
            # Against the pass of 9 when this toolkit's tree is clean, which keeps it; else against the first's 5.
            self.assertRegex(said, r"test executed 4 tests, \d+ fewer than the (9|5) its last passing whole run on .*"
                                   r"/test\.json\), which a commit since says with Tests-Removed: test")

    def test_whole_parallel_and_serial_groups_hold_each_member_to_its_own_count(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def git(*arguments):
                return subprocess.run(["git", "-C", directory, "-c", "user.name=Test", "-c", "user.email=t@example.com",
                                       *arguments], check=True, capture_output=True, text=True).stdout.strip()
            git("init", "-q", "-b", "main")
            (root / ".gitignore").write_text(".build/\n")
            def counter(name):
                return {"command": [sys.executable, "-c",
                                    f"from pathlib import Path; print('Ran', Path({name!r}).read_text(), 'tests')"]}
            (root / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Lanes", "lanes": {
                "ios": counter("ios"), "mac": counter("mac"), "suites": {"parallel": ["ios", "mac"]},
                "gate": {"command": ["sh", "-c", '"$0" "$1" --project . run --here suites; status=$?; '
                                     'cat .build/queuekeeper/suites/lanes/ios.log .build/queuekeeper/suites/lanes/mac.log; '
                                     'exit "$status"', sys.executable, str(config.TOOLKIT_ROOT / "bin/qk")]}}}))
            def landed(ios, mac, message="state"):
                (root / "ios").write_text(str(ios))
                (root / "mac").write_text(str(mac))
                git("add", "-A")
                git("commit", "-q", "--allow-empty", "-m", message)
                return git("rev-parse", "HEAD")
            def identity(path):
                value = config.source_identity(path)
                if path == config.TOOLKIT_ROOT: value["workingChangesSHA256"] = remote.CLEAN
                return value
            def run(*arguments):
                with patch.object(cli, "source_identity", side_effect=identity), \
                        patch.object(cli, "refresh_policy", return_value="policy"), \
                        contextlib.redirect_stdout(StringIO()) as output, contextlib.redirect_stderr(StringIO()) as error:
                    status = cli.main(["--project", directory, "run", "--here", *arguments])
                return status, output.getvalue() + error.getvalue()
            first = landed(5, 3)
            baseline = remote.store(root) / first / "suites.json"
            self.assertEqual(run("suites", "--", "ios")[0], 0)
            self.assertFalse(baseline.exists())
            self.assertEqual(run("--serial", "suites")[0], 0)
            self.assertFalse(baseline.exists())
            self.assertEqual(run("suites")[0], 0)
            saved = json.loads(baseline.read_text())
            self.assertEqual(saved["result"]["receipt"]["lanes"]["ios"]["executedTests"], 5)

            # A new Mac uses the selected count from the other Mac's receipt.
            saved["result"]["receipt"]["mac"] = "Other Mac"
            baseline.write_text(json.dumps(saved))
            fell = landed(4, 4, "Wrong name\n\nTests-Removed: suites")
            self.assertEqual(run("suites", "--", "ios")[0], 0)
            self.assertFalse((remote.store(root) / fell / "suites.json").exists())
            for command in (("suites",), ("--serial", "suites"), ("gate",)):
                with self.subTest(command=command):
                    status, said = run(*command)
                    if command == ("gate",):
                        said += (root / ".build/queuekeeper/gate/run.log").read_text()
                    self.assertEqual(status, 1, said)
                    self.assertIn("ios executed 4 tests, 1 fewer than the 5", said)
                    self.assertIn("held to Other Mac's", said)
                    self.assertIn("Tests-Removed: ios", said)
            member = json.loads((root / ".build/queuekeeper/suites/lanes/ios.json").read_text())
            self.assertEqual((member["outcome"], member["executedTests"]), ("failed", 4))
            landed(4, 4, "Remove an iOS test\n\nTests-Removed: ios")
            status, said = run("suites")
            self.assertEqual(status, 0, said)
            self.assertIn("which a commit since says with Tests-Removed: ios", said)

    def test_a_run_whose_checkout_moves_while_it_runs_is_no_commits_pass(self):
        """BreezEPUB, 2026-11-19: a session ran `git switch` in the worktree an hour into a local run, which built and
        tested the switched files with no word of it (vocaro/queuekeeper#310). A run on another commit by its end is
        unmeasured and kept as neither commit's; a run whose tracked files changed keeps its verdict as no commit's
        pass; a group says so as its lane ends; and a run left alone passes and counts, as before."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def git(*arguments):
                return subprocess.run(["git", "-C", directory, "-c", "user.name=Test", "-c", "user.email=t@example.com",
                                       *arguments], check=True, capture_output=True, text=True).stdout.strip()
            def lane(*commands):
                # The session's Git command, run mid-run from the lane's own command, which then passes.
                return {"command": [sys.executable, "-c", "import subprocess, sys\n"
                                    f"for command in {list(commands)!r}: subprocess.run(command, check=True)\n"
                                    "print('Ran 1 tests'); print(); print('OK')"]}
            git("init", "-q", "-b", "main")
            (root / ".gitignore").write_text(".build/\n")
            (root / "project.yml").write_text("pin: 1\n")
            (root / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Lanes", "lanes": {
                "still": lane(), "switch": lane(["git", "switch", "-q", "other"]),
                "edit": lane(["git", "checkout", "other", "--", "project.yml"]),
                "untracked": lane([sys.executable, "-c", "open('output.txt', 'w').write('made')"]),
                "group": {"parallel": ["still", "switch"]}}}))
            git("add", "-A")
            git("commit", "-q", "-m", "start")
            start = git("rev-parse", "HEAD")
            git("switch", "-q", "-c", "other")
            (root / "project.yml").write_text("pin: 2\n")
            git("commit", "-q", "-am", "other")
            other = git("rev-parse", "HEAD")
            def run(name):
                git("switch", "-q", "--discard-changes", "main")
                (root / "output.txt").unlink(missing_ok=True)
                with patch.object(cli, "refresh_policy", return_value="policy"), \
                        patch.object(cli.remote, "keep_here", return_value=None) as kept, \
                        contextlib.redirect_stderr(StringIO()) as error:
                    status = cli.main(["--project", directory, "run", name])
                receipt = json.loads((root / ".build/queuekeeper" / name / "source.json").read_text())
                # Whether the run would be kept as its commit's pass (remote.keep_here's whole).
                return status, error.getvalue(), receipt, kept.call_args.args[4]
            # The negative control: nothing moves, and the run passes and counts for its commit.
            status, said, receipt, counts = run("still")
            self.assertEqual((status, receipt["outcome"], receipt["project"]["revision"], counts),
                             (0, "passed", start, True), said)
            self.assertNotIn("checkoutMoved", receipt)
            self.assertNotIn("checkout", said)
            # A lane's own untracked outputs move nothing.
            status, said, receipt, counts = run("untracked")
            self.assertEqual((status, counts), (0, True), said)
            self.assertNotIn("checkoutChanged", receipt)
            # A branch switch mid-run: unmeasured, naming both commits, and kept as neither's.
            status, said, receipt, counts = run("switch")
            self.assertEqual((status, receipt["outcome"], counts), (lane_results.UNMEASURED, "unmeasured", False), said)
            self.assertEqual(receipt["checkoutMoved"], {"from": start, "to": other})
            self.assertIn(f"queuekeeper: unmeasured: the checkout {root.resolve()} moved from {start[:12]} to {other[:12]} (other) "
                          "while switch ran", said)
            self.assertIn("its result is neither commit's", receipt["error"])
            self.assertNotIn("tests", receipt)
            # Another file checked out at the same commit: the verdict stands, as no commit's pass.
            status, said, receipt, counts = run("edit")
            self.assertEqual((status, receipt["outcome"], counts), (0, "passed", False), said)
            self.assertEqual(receipt["checkoutChanged"], {"commit": start, "files": ["project.yml"]})
            self.assertIn(f"the checkout's tracked files changed while edit ran (project.yml), so its result is what it "
                          f"read but no pass of {start[:12]}", said)
            # A group says so when its lane ends, and ends unmeasured.
            status, said, receipt, counts = run("group")
            self.assertEqual((status, receipt["outcome"], counts), (lane_results.UNMEASURED, "unmeasured", False), said)
            self.assertIn("the group runs on, and its result will be unmeasured", said)
            self.assertEqual(receipt["checkoutMoved"], {"from": start, "to": other})

    def test_a_run_whose_checkout_moved_while_it_waited_names_the_commit_it_reads(self):
        """A checkout that moved during the wait for a slot is read as it is once the command starts, and the receipt
        names that commit, not the one `qk run` was called at (vocaro/queuekeeper#310)."""
        with tempfile.TemporaryDirectory() as directory:
            def git(*arguments):
                return subprocess.run(["git", "-C", directory, "-c", "user.name=Test", "-c", "user.email=t@example.com",
                                       *arguments], check=True, capture_output=True, text=True).stdout.strip()
            git("init", "-q", "-b", "main")
            git("commit", "-q", "--allow-empty", "-m", "first")
            first = git("rev-parse", "HEAD")
            git("commit", "-q", "--allow-empty", "-m", "second")
            second = git("rev-parse", "HEAD")
            receipt = {"project": {"revision": first}}
            with contextlib.redirect_stderr(StringIO()) as error:
                began = cli.started_here(Path(directory), "gate", receipt)
            self.assertEqual((began["head"], receipt["project"]["revision"]), (second, second))
            self.assertIn(f"moved from {first[:12]} to {second[:12]} while gate waited, so it runs {second[:12]}",
                          error.getvalue())
            # Unmoved, it says nothing; and a folder that is no repository has no reading to compare.
            with contextlib.redirect_stderr(StringIO()) as error:
                self.assertEqual(cli.started_here(Path(directory), "gate", receipt)["head"], second)
                self.assertIsNone(cli.checkout_moved(Path(directory), "gate", None))
            self.assertEqual(error.getvalue(), "")
            with tempfile.TemporaryDirectory() as elsewhere:
                self.assertIsNone(cli.checkout_state(Path(elsewhere)))

    def test_a_lane_that_takes_no_arguments_refuses_them_before_anything_runs_or_is_sent(self):
        """BreezEPUB's ways-in on 2026-11-13 refused two -only-testing arguments in its own script, after the run had
        been staged and sent to another Mac (vocaro/queuekeeper#137)."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ran = root / "ran"
            touch = [sys.executable, "-c", f"import pathlib; pathlib.Path({str(ran)!r}).touch()"]
            def configure(**keys):
                (root / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Lanes", "lanes": {
                    "ways-in": {"command": touch, **keys}}}))
            def run(*arguments):
                ran.unlink(missing_ok=True)
                with patch.object(cli, "source_identity", return_value={}), contextlib.redirect_stderr(StringIO()) as error, \
                        patch.object(cli.lane_wait, "begin", wraps=cli.lane_wait.begin) as begun, \
                        patch.object(cli.remote, "first", wraps=cli.remote.first) as offered:
                    status = cli.main(["--project", directory, "run", "ways-in", *arguments])
                return status, error.getvalue().splitlines(), ran.exists(), begun.called or offered.called
            configure(arguments=False)
            for arguments in (["--", "-only-testing:UITests/WaysInTests/testOne", "two words"], ["--repeat", "2"]):
                with self.subTest(arguments):
                    status, said, started, placed = run(*arguments)
                    self.assertEqual((status, started, placed), (2, False, False), said)
                    self.assertFalse((root / ".build/queuekeeper/ways-in").exists())
                    self.assertEqual(said[-1], "qk run ways-in: not run (exit 2); no tests ran")
            status, said, started, placed = run("--", "-only-testing:UITests/WaysInTests/testOne", "two words")
            self.assertEqual(said[-2], "queuekeeper: ways-in takes no arguments, as its lane says (\"arguments\": false), and was "
                                       "given -only-testing:UITests/WaysInTests/testOne 'two words'; nothing ran. Run it "
                                       "without them: qk run ways-in")
            # Without arguments it runs, and a lane that says true, or nothing, takes them as before.
            self.assertEqual(run()[::2], (0, True))
            for keys in ({"arguments": True}, {}):
                configure(**keys)
                self.assertEqual(run("--", "x")[::2], (0, True), keys)
            # The key is true or false, and a lane's alone: a group already takes none.
            configure(arguments="no")
            with self.assertRaisesRegex(ValueError, "ways-in.arguments must be true or false"): config.load(root)
            (root / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Lanes", "lanes": {
                "ways-in": {"command": touch}, "group": {"parallel": ["ways-in"], "arguments": False}}}))
            with self.assertRaisesRegex(ValueError, "Lane group group takes only"): config.load(root)

    def test_a_repeated_run_is_a_series_its_command_reports_or_it_is_unmeasured(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # A lane that reports a series of the size it was asked for, and one that ignores the ask and passes.
            reporting = [sys.executable, "-c", "import sys; n = sys.argv[sys.argv.index('--repeat') + 1]; "
                         "print(f'Repeat series: asked {n} repeats of X (3 runs); runs: 3 passed'); print('Ran 6 tests')"]
            ignoring = [sys.executable, "-c", "print('Ran 1 test')"]
            (root / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Lanes", "lanes": {
                "series": {"command": reporting, "minimumTests": 1, "timeout": 100},
                "once": {"command": ignoring, "minimumTests": 1}}}))
            def run(*arguments):
                with patch.object(cli, "source_identity", return_value={}), contextlib.redirect_stderr(StringIO()) as error:
                    return cli.main(["--project", directory, "run", "--here", *arguments]), error.getvalue()
            status, error = run("--repeat", "3", "--erase", "--only", "T/a", "series")
            self.assertEqual(status, 0, error)
            receipt = json.loads((root / ".build/queuekeeper/series/source.json").read_text())
            self.assertEqual(receipt["arguments"], ["--repeat", "3", "--erase", "--only", "T/a"])
            self.assertEqual(receipt["timeout"], 300, "a series' lane has N times its limit")
            status, error = run("--repeat", "2", "--expect", "3", "series")
            self.assertEqual(status, 0, error)
            status, error = run("--repeat", "3", "once")
            self.assertEqual(status, lane_results.UNMEASURED, error)
            self.assertIn("reports no series of 3 runs", error)
            self.assertEqual(json.loads((root / ".build/queuekeeper/once/source.json").read_text())["outcome"], "unmeasured")
            status, error = run("once")
            self.assertEqual(status, 0, error)
            self.assertNotIn("series", error)
            for arguments, refusal in ((["--repeat", "0", "series"], "at least 1 run"),
                                       (["--repeat", "-2", "series"], "at least 1 run"),
                                       (["--erase", "series"], "give --repeat N too"),
                                       (["--only", "T/a", "series"], "give --repeat N too")):
                with self.subTest(arguments):
                    (root / ".build/queuekeeper/series/source.json").unlink(missing_ok=True)
                    status, error = run(*arguments)
                    self.assertEqual(status, 2, error)
                    self.assertIn(refusal, error)
                    self.assertFalse((root / ".build/queuekeeper/series/source.json").exists())

    def run_lane(self, directory, *arguments):
        with patch.object(cli, "source_identity", return_value={}), contextlib.redirect_stderr(StringIO()) as error:
            cli.main(["--project", directory, "run", *arguments])
        return error.getvalue()

    def test_a_lane_that_declares_no_timeout_is_stopped_at_engs_default(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # The quick lane runs under the same default as the hung one, so the limit leaves it room on a host
            # slowed in a way the load does not show. The hung one sleeps twenty times as long and would exit 0
            # if nothing stopped it: its sleep, not a ceiling on this test's elapsed time, is the bound. At a load
            # of at most one per core, where stretched(30) is 30, this test took 37.5 s on another Mac (#319).
            limit = round(stretched(3))
            lanes = {"hung": {"command": [sys.executable, "-c", f"import time; time.sleep({20 * limit})"]},
                     "quick": {"command": [sys.executable, "-c", "print('Ran 1 test')"]},
                     "both": {"parallel": ["hung", "quick"]}}
            (root / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Lanes", "lanes": lanes}))
            with patch.object(processes, "DEFAULT_LIMIT", limit), contextlib.redirect_stdout(StringIO()):
                for name, status in (("hung", lane_results.UNMEASURED), ("quick", 0), ("both", lane_results.UNMEASURED)):
                    with self.subTest(name), patch.object(cli, "source_identity", return_value={}), \
                            contextlib.redirect_stderr(StringIO()) as error:
                        self.assertEqual(cli.main(["--project", directory, "run", name]), status, error.getvalue())
            stopped = f"timed out after {limit:g}s"
            recorded = json.loads((root / ".build/queuekeeper/hung/source.json").read_text())
            self.assertEqual((recorded["outcome"], recorded["timeout"]), ("unmeasured", limit))
            self.assertIn(stopped, recorded["error"])
            self.assertEqual(json.loads((root / ".build/queuekeeper/quick/source.json").read_text())["outcome"], "passed")
            members = {name: json.loads((root / f".build/queuekeeper/both/lanes/{name}.json").read_text())
                       for name in ("hung", "quick")}
            self.assertEqual({name: member["outcome"] for name, member in members.items()},
                             {"hung": "unmeasured", "quick": "passed"})
            self.assertIn(stopped, members["hung"]["error"])

    def test_a_lane_that_reports_an_unmeasured_test_or_reaches_its_timeout_is_unmeasured(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            unmeasured = "/p/T.swift:6: -[P.T testA] : Test skipped - Unmeasured: the 80 s backstop passed"
            def lane(output, status=0, **options):
                return {"command": [sys.executable, "-c", f"import sys; print({output!r}); sys.exit({status})"], **options}
            lanes = {"unmeasured": lane(f"{unmeasured}\nRan 2 tests"),
                     "skipped": lane("/p/T.swift:6: -[P.T testA] : Test skipped - needs a Mac\nRan 2 tests"),
                     # A lane that exits 3 itself may not have run every test.
                     "short": lane("Ran 1 test", 3, minimumTests=2),
                     "short-passed": lane("Ran 1 test", minimumTests=2),
                     # A test the host left unmeasured ran nothing, so a run it leaves short stays unmeasured.
                     "short-unmeasured": lane(f"{unmeasured}\nRan 2 tests\n\nOK (skipped=1)", minimumTests=2),
                     "failed": lane(f"{unmeasured}\nRan 2 tests", 1),
                     # Fixed: the slow lane's 1 s timeout is the behavior under test; the others have none.
                     "slow": {"command": [sys.executable, "-c", "import time; time.sleep(60)"], "timeout": 1}}
            (root / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Lanes", "lanes": lanes}))
            # The outcome is the lane's verdict: a zero exit that ran too few tests failed.
            cases = {"unmeasured": (3, "unmeasured"), "skipped": (0, "passed"), "short": (3, "unmeasured"),
                     "short-passed": (2, "failed"), "short-unmeasured": (3, "unmeasured"), "failed": (1, "failed"),
                     "slow": (3, "unmeasured")}
            for name, (status, outcome) in cases.items():
                with self.subTest(name):
                    with patch.object(cli, "source_identity", return_value={}), contextlib.redirect_stderr(StringIO()) as error:
                        self.assertEqual(cli.main(["--project", directory, "run", name]), status, error.getvalue())
                    recorded = json.loads((root / ".build/queuekeeper" / name / "source.json").read_text())
                    self.assertEqual(recorded.get("outcome"), outcome)
            recorded = json.loads((root / ".build/queuekeeper/unmeasured/source.json").read_text())
            self.assertEqual((recorded["exitStatus"], recorded["unmeasured"]), (0, [unmeasured]))
            self.assertIn("timed out after 1s", json.loads((root / ".build/queuekeeper/slow/source.json").read_text())["error"])
            recorded = json.loads((root / ".build/queuekeeper/short-passed/source.json").read_text())
            self.assertEqual(recorded["exitStatus"], 0)
            self.assertIn("Expected at least 2 executed tests; observed 1", recorded["error"])
            self.assertIn("Expected at least 2 executed tests; observed 1", self.run_lane(directory, "short"))


def malformed_guards(identity, entry):
    """Guards with admission's marker whose owners are not entries in admission's format, as JSON text by what
    is wrong: each differs by that alone from the guard admission wrote, whose one entry is entry, by identity.
    Admission raised AttributeError or KeyError at some instead of refusing them (#72)."""
    def guard(owners):
        return {"vocaroEngineering": 1, "owners": owners}
    cases = {"no owners": {"vocaroEngineering": 1}, "owners an empty list": guard([]),
             "owners a list of its entry": guard([entry]), "owners a string": guard("held"), "owners a number": guard(1),
             "owners null": guard(None), "an entry that is a string": guard({identity: "held"}),
             "an entry that is null": guard({identity: None}),
             **{f"an entry without its {field}": guard({identity: {key: value for key, value in entry.items() if key != field}})
                for field in ["pid", "start", "label", "exclusive"]},
             **{f"an entry whose {field} is {value!r}": guard({identity: {**entry, field: value}})
                for field, value in [("pid", str(entry["pid"])), ("pid", True), ("pid", 0), ("start", None), ("label", 7),
                                     ("exclusive", "no"), ("model", 1), ("admitted", 0), ("uncertain", "yes")]},
             "a stray entry beside it": guard({identity: entry, "stray": {"pid": entry["pid"]}})}
    return {name: json.dumps(value) for name, value in cases.items()}


class CommandEnvironmentTests(unittest.TestCase):
    def test_a_command_runs_with_what_the_projects_plugins_add_to_its_environment(self):
        # The plugins are loaded before their additions are read: a lane that generates an Xcode project needs
        # the path the toolkit's hooks name (vocaro-engineering#403).
        from queuekeeper import plugins
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "project"
            hooks = root.parent / "kit/src/python/examplekit"
            hooks.mkdir(parents=True)
            root.mkdir()
            (hooks / "__init__.py").write_text("")
            (hooks / "hooks.py").write_text(
                "from queuekeeper import plugins\n"
                "def added(): return {'EXAMPLE_KIT_ROOT': 'from the kit'}\n"
                "def register(): plugins.COMMAND_ENVIRONMENT.append(added)\n")
            (root / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Example", "plugins": [
                {"module": "examplekit.hooks", "path": "../kit/src/python"}]}))
            before, paths, seen = list(plugins.COMMAND_ENVIRONMENT), list(sys.path), []

            def restore():
                plugins.COMMAND_ENVIRONMENT[:] = before
                sys.path[:] = paths
                for name in [name for name in sys.modules if name.split(".")[0] == "examplekit"]: del sys.modules[name]
            self.addCleanup(restore)
            with patch.object(cli, "perform", side_effect=lambda argv, report: seen.append(
                    os.environ.get("EXAMPLE_KIT_ROOT")) or 0), patch.object(cli.lane_report.Report, "finish"):
                self.assertEqual(cli.main(["--project", str(root), "doctor"]), 0)
            self.assertEqual(seen, ["from the kit"])
            self.assertNotIn("EXAMPLE_KIT_ROOT", os.environ)


class ResourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"HEAVY_SLOT_DIR": str(root / "slots"), "HEAVY_SLOTS": "2",
            "HEAVY_SLOTS_MAX": "3", "HEAVY_POLL_SECONDS": "0.01", "QUEUEKEEPER_GATE_LOCK_FILE": str(root / "gate")})
        self.env.start()
        self.saved = os.environ.pop(resources.TOKEN, None)
        # The CPU limit is the default, or a limits file's, unless a test sets it (host_isolation sets one for all).
        os.environ.pop("HEAVY_BUSY_LIMIT", None)
        # The CPU reads as mostly idle unless a test says otherwise: no test depends on the host's real reading, and
        # none waits for a reading of its own (resources.cpu_busy, offload_host.cpu_busy; vocaro-engineering#200).
        from queuekeeper import offload_host
        for patcher in [patch.object(resources, "cpu_busy", return_value=(10.0, 15.0)),
                        patch.object(offload_host, "cpu_busy", return_value=10.0)]:
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        if self.saved is not None: os.environ[resources.TOKEN] = self.saved
        else: os.environ.pop(resources.TOKEN, None)
        self.env.stop()
        self.temp.cleanup()

    def test_private_command_tmpdirs_do_not_create_separate_host_budgets(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = dict(os.environ)
            environment.pop("HEAVY_SLOT_DIR", None)
            command = [sys.executable, "-c", "from queuekeeper.resources import slot_directory; print(slot_directory())"]
            environment["PYTHONPATH"] = str(config.TOOLKIT_ROOT / "src/python")
            outputs = []
            for name in ["first", "second"]:
                folder = Path(directory) / name
                folder.mkdir()
                result = subprocess.check_output(command, text=True, env={**environment, "TMPDIR": str(folder)})
                outputs.append(result.strip())
            self.assertEqual(outputs[0], outputs[1])

    def hold(self, *slots):
        directory = resources.slot_directory()
        directory.mkdir(parents=True, exist_ok=True)
        for slot in slots:
            _slot, fd = resources.take_slot(directory, slot + 1, start=slot)
            os.write(fd, b"other holder\n")
            self.addCleanup(os.close, fd)

    def test_spare_capacity_admits_jobs_beyond_the_guaranteed_slots_up_to_the_maximum(self):
        self.hold(0, 1)
        os.environ["HEAVY_SETTLE_SECONDS"] = "0"
        with patch.object(resources, "cpu_busy", return_value=(10.0, 15.0)), patch.object(resources, "memory_level", return_value=1), contextlib.redirect_stderr(StringIO()):
            with resources.Admission("extra", timeout=0) as admitted:
                self.assertEqual([slot for slot, _fd in admitted.descriptors], [2])
            self.hold(2)
            with self.assertRaisesRegex(TimeoutError, "so is every slot up to HEAVY_SLOTS_MAX=3"):
                with resources.Admission("beyond", timeout=0): pass

    def test_a_whole_host_lane_takes_every_guaranteed_slot_and_no_lighter_lane_shares_it(self):
        """vocaro-engineering#168: two whole-host lanes never run on one Mac at once; #196: while one runs, no
        lighter heavy lane is admitted beside it, not even beyond the guaranteed slots while the load shows spare
        capacity, and it is admitted once the lane ends; exclusive work is unchanged."""
        os.environ["HEAVY_SETTLE_SECONDS"] = "0"
        quiet = lambda: contextlib.ExitStack()
        with contextlib.redirect_stderr(StringIO()), resources.Admission("gate", whole_host=True, timeout=0) as gate:
            self.assertEqual([slot for slot, _fd in gate.descriptors], [0, 1])
            self.assertTrue(gate.whole_host)
            token = json.loads(os.environ[resources.TOKEN])
            self.assertEqual((token["wholeHost"], token["exclusive"], token["model"]), (True, False, False))
            owners = json.loads(gate.guard.read_text())["owners"]
            self.assertEqual((owners[gate.identity]["wholeHost"], owners[gate.identity]["exclusive"]), (True, False))
            # Nested work reuses it, whole-host or not.
            with resources.Admission("nested", timeout=0) as nested: self.assertTrue(nested.reused)
            with patch.dict(os.environ): 
                os.environ.pop(resources.TOKEN)
                with self.assertRaisesRegex(TimeoutError, "a whole-host lane needs every guaranteed slot \\(HEAVY_SLOTS=2\\) "
                                                          "free, and 2 are busy"):
                    with resources.Admission("second gate", whole_host=True, timeout=0): pass
                with self.assertRaisesRegex(TimeoutError, re.escape(f"exclusive work needs every slot up to "
                                                                    f"{mine('HEAVY_SLOTS_MAX=3')} free, and 2 are busy")):
                    with resources.Admission("timed", exclusive=True, timeout=0): pass
                # A lighter heavy lane is refused beside it, even with a slot beyond the guaranteed ones free and the
                # host showing spare capacity: the lane's load dips between its phases (#196).
                with patch.object(resources, "cpu_busy", return_value=(10.0, 15.0)), \
                        patch.object(resources, "memory_level", return_value=1):
                    with self.assertRaisesRegex(TimeoutError, "it waits behind a whole-host lane holding the host: gate"):
                        with resources.Admission("tests", timeout=0): self.fail("admitted beside a whole-host lane")
                # And refused alike while the host shows no spare capacity.
                with patch.object(resources, "cpu_busy", return_value=(90.0, 15.0)), \
                        patch.dict(os.environ, {"HEAVY_BUSY_LIMIT": "40"}):
                    with self.assertRaisesRegex(TimeoutError, "it waits behind a whole-host lane holding the host: gate"):
                        with resources.Admission("tests", timeout=0): pass
        # Once the lane ends, the lighter lane is admitted in a guaranteed slot.
        with contextlib.redirect_stderr(StringIO()), patch.dict(os.environ):
            os.environ.pop(resources.TOKEN, None)
            with resources.Admission("tests", timeout=0) as lighter:
                self.assertEqual([slot for slot, _fd in lighter.descriptors], [0])
                self.assertFalse(lighter.whole_host)
        # Its log line says it was whole-host; a heavy line says nothing of it.
        lines = [json.loads(line) for line in resources.log_path().read_text().splitlines()]
        admitted = [line for line in lines if line["event"] == "admitted"]
        self.assertEqual([(line["label"], line["kind"], line.get("wholeHost"), line["slots"]) for line in admitted],
                         [("gate", "heavy", True, [0, 1]), ("tests", "heavy", None, [0])])
        # Exclusive work holds every slot, whole-host or not: the flag adds nothing to it.
        with contextlib.redirect_stderr(StringIO()), resources.Admission("timed", exclusive=True, whole_host=True, timeout=0) \
                as timed:
            self.assertFalse(timed.whole_host)
            self.assertEqual([slot for slot, _fd in timed.descriptors], [0, 1, 2])
            self.assertNotIn("wholeHost", json.loads(timed.guard.read_text())["owners"][timed.identity])
        # One guaranteed slot busy keeps a whole-host lane out, while a lighter lane takes the other.
        self.hold(0)
        with contextlib.redirect_stderr(StringIO()):
            with self.assertRaisesRegex(TimeoutError, "a whole-host lane needs every guaranteed slot \\(HEAVY_SLOTS=2\\) "
                                                      "free, and 1 is busy"):
                with resources.Admission("gate", whole_host=True, timeout=0): pass
            with resources.Admission("tests", timeout=0) as lighter:
                self.assertEqual([slot for slot, _fd in lighter.descriptors], [1])

    def test_the_capacity_reading_shows_the_slots_held_the_busy_limit_and_what_runs(self):
        """offload_host.capacity, which qk run reads on the Macs before it chooses one (vocaro-engineering#168),
        agrees with admission: the limits admission reads, the slots it holds and the guard entries it writes."""
        from queuekeeper import offload_host
        os.environ["HEAVY_SETTLE_SECONDS"] = "0"
        read = offload_host.capacity()
        self.assertEqual((read["slots"], read["slotsMax"], read["busy"], read["wholeHost"], read["exclusive"]),
                         (2, 3, [], [], []))
        self.assertEqual((read["busyLimit"], read["cpuBusy"]), (resources.busy_limit(), 10.0))
        self.assertIsInstance(read["load"], float)
        with contextlib.redirect_stderr(StringIO()), resources.Admission("EPUBLib gate", whole_host=True, timeout=0):
            read = offload_host.capacity()
            self.assertEqual((read["busy"], read["wholeHost"], read["exclusive"]), ([0, 1], ["EPUBLib gate"], []))
            # The reading took no slot: the third is still free, though a heavy request is refused beside the
            # running whole-host lane (#196), as the reading says the Mac has no spare capacity.
            with patch.object(resources, "cpu_busy", return_value=(10.0, 15.0)), \
                    patch.object(resources, "memory_level", return_value=1), patch.dict(os.environ):
                os.environ.pop(resources.TOKEN)
                with self.assertRaisesRegex(TimeoutError, "it waits behind a whole-host lane holding the host: EPUBLib gate"):
                    with resources.Admission("tests", timeout=0): pass
                self.assertEqual(offload_host.capacity()["busy"], [0, 1])
        self.assertEqual(offload_host.capacity()["busy"], [])
        with contextlib.redirect_stderr(StringIO()), resources.Admission("timed run", exclusive=True, timeout=0):
            read = offload_host.capacity()
            self.assertEqual((read["busy"], read["wholeHost"], read["exclusive"]), ([0, 1, 2], [], ["timed run"]))
        # An entry whose process has ended is not running, whatever its slots' files say.
        guard = resources.guard_path(os.environ)
        guard.write_text(json.dumps({"vocaroEngineering": 1, "owners": {"gone": {
            "pid": 2 ** 22 - 1, "start": "Tue Nov  4 00:00:00 2026", "label": "ended gate", "exclusive": False,
            "wholeHost": True}}}))
        self.assertEqual(offload_host.capacity()["wholeHost"], [])
        guard.unlink()
        # The limits come from the environment, the host's limits file or the defaults, as admission's do.
        resources.limits_path().write_text(json.dumps({"slots": 1, "slotsMax": 4, "busyLimit": 60,
                                                       "settleSeconds": 1}))
        for setting in [{}, {"HEAVY_SLOTS": "2", "HEAVY_SLOTS_MAX": "3", "HEAVY_BUSY_LIMIT": "70"}]:
            with self.subTest(setting), patch.dict(os.environ, setting):
                for name in ["HEAVY_SLOTS", "HEAVY_SLOTS_MAX"]:
                    if name not in setting: os.environ.pop(name)
                expected = resources.limits()
                read = offload_host.capacity()
                self.assertEqual((read["slots"], read["slotsMax"], read["busyLimit"], read["settle"]),
                                 (expected["slots"][0], expected["slotsMax"][0], expected["busyLimit"][0],
                                  expected["settleSeconds"][0]))
                self.assertEqual(offload_host.pressure()["busyLimit"], expected["busyLimit"][0])
        resources.limits_path().write_text("{")
        self.assertIn("is not JSON", offload_host.capacity()["error"])
        resources.limits_path().unlink()
        with patch.dict(os.environ, {"HEAVY_BUSY_LIMIT": "x"}):
            self.assertIn("HEAVY_BUSY_LIMIT=x is not a number", offload_host.capacity()["error"])
        # A settle time admission refuses, the reading refuses too.
        with patch.dict(os.environ, {"HEAVY_SETTLE_SECONDS": "-1"}):
            self.assertRaises(ValueError, resources.limits)
            self.assertIn("HEAVY_SETTLE_SECONDS at least 0", offload_host.capacity()["error"])
        # Answered as a request, from the file as a run sends it, not taken for a plan to run.
        done = subprocess.run([sys.executable, str(config.TOOLKIT_ROOT / "src/python/queuekeeper/offload_host.py"),
                               json.dumps({"capacity": True})], capture_output=True, text=True, timeout=stretched(60))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(set(json.loads(done.stdout.splitlines()[-1])),
                         {"slots", "slotsMax", "busy", "load", "cpuBusy", "busyLimit", "wholeHost", "exclusive", "kept",
                          "queuedExclusive", "sinceAdmission", "settle", "memory", "screen", "limitsFrom", "limitsFile",
                          "takes", "takesSet", "labels", "settingsFile", "settingsIgnored"})

    def test_a_busy_cpu_memory_pressure_or_an_unsettled_admission_withholds_extra_slots(self):
        self.hold(0, 1)
        os.environ["HEAVY_BUSY_LIMIT"] = "40"
        cases = [(f"the CPU is 90% busy, at or above {mine('HEAVY_BUSY_LIMIT=40%')}", (90.0, 15.0), 1, "0"),
            ("memory pressure is level 2 (warning), not 1 (normal)", (10.0, 15.0), 2, "0"),
            ("s ago, within HEAVY_SETTLE_SECONDS=3600", (10.0, 15.0), 1, "3600"),
            # Samples that do not span a reading yet withhold it as settling, not as a busy CPU.
            ("the CPU has been read over 2s, short of the 5s a reading spans", (None, 2.0), 1, "0"),
            (f"the CPU's busy share, which {mine('HEAVY_BUSY_LIMIT=40%')} bounds, is unavailable", (None, None), 1, "0")]
        for reason, load, memory, settle in cases:
            with self.subTest(reason):
                os.environ["HEAVY_SETTLE_SECONDS"] = settle
                with patch.object(resources, "cpu_busy", return_value=load), patch.object(resources, "memory_level", return_value=memory), contextlib.redirect_stderr(StringIO()) as error:
                    with self.assertRaisesRegex(TimeoutError, re.escape(reason)):
                        with resources.Admission("blocked", timeout=0): pass
                self.assertIn(reason, error.getvalue())

    def test_adaptive_limits_default_to_the_host_and_reject_a_maximum_below_the_guarantee(self):
        os.environ.pop("HEAVY_SLOTS_MAX")
        self.assertEqual(resources.Admission("default").maximum, max(2, (os.cpu_count() or 1) // 2))
        os.environ["HEAVY_SLOTS_MAX"] = "1"
        with self.assertRaises(ValueError): resources.Admission("invalid")

    def write_limits(self, value):
        path = resources.limits_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value if isinstance(value, str) else json.dumps(value))
        return path

    def test_without_a_limits_file_admission_keeps_the_limits_it_had(self):
        for name in ["HEAVY_SLOTS", "HEAVY_SLOTS_MAX"]: os.environ.pop(name)
        self.assertEqual(resources.limits_path(), resources.slot_directory() / "admission-limits.json")
        self.assertFalse(resources.limits_path().exists())
        for cores in [10, 18, 4]:
            with self.subTest(cores=cores), patch.object(resources.os, "cpu_count", return_value=cores):
                admission = resources.Admission("default")
                self.assertEqual((admission.slots, admission.maximum, admission.busy_limit, admission.settle),
                                 (3, max(3, cores // 2), 90.0, 30.0))
                self.assertTrue(all(source.startswith("default") for _value, source in resources.limits().values()))
                # The reading placement takes of a Mac defaults as that Mac's admission does.
                from queuekeeper import offload_host
                self.assertEqual(offload_host.limits(), {key: value for key, (value, _source) in resources.limits().items()})

    def test_setup_gives_its_mac_a_cpu_limit_below_the_default_and_never_changes_one_that_is_set(self):
        # The owner's decision, 2026-11-17 (vocaro/queuekeeper#211): 70% for the Mac setup runs on, and 90% as the
        # default, which a Mac added to the farm keeps. Until then the default was 75% and no command wrote the file.
        path = resources.limits_path()
        self.assertEqual(resources.busy_limit(), 90.0)
        self.assertEqual(resources.setup_limits(), (True, 70.0))
        self.assertEqual(json.loads(path.read_text()), {"busyLimit": 70})
        self.assertEqual(resources.limits()["busyLimit"], (70.0, str(path)))
        self.assertEqual(resources.setup_limits(), (False, 70.0))
        for text, limit in (('{"busyLimit": 90}', 90.0), ('{"slots": 2, "busyLimit": 12.5}', 12.5), ('{"busyLimit": 0}', 0.0)):
            with self.subTest(text):
                path.write_text(text)
                self.assertEqual(resources.setup_limits(), (False, limit))
                self.assertEqual(path.read_text(), text)
        # A file with other limits gains the one key, and keeps the others as they were written.
        path.write_text('{"slotsMax": {"perCore": 0.5}, "settleSeconds": 5}')
        self.assertEqual(resources.setup_limits(), (True, 70.0))
        self.assertEqual(json.loads(path.read_text()), {"slotsMax": {"perCore": 0.5}, "settleSeconds": 5, "busyLimit": 70})
        self.assertEqual(set(json.loads(path.read_text())) - set(resources.LIMIT_KEYS), set())
        # One admission cannot read is its owner's to fix, and is not replaced.
        for text in ("{", '{"loadLimit": 30}', '{"slots": 2.5}'):
            with self.subTest(text):
                path.write_text(text)
                with self.assertRaisesRegex(ValueError, re.escape(str(path))): resources.setup_limits()
                self.assertEqual(path.read_text(), text)
        self.assertEqual([entry.name for entry in path.parent.iterdir() if entry.name.startswith(".")], [])

    def test_setup_leaves_a_limits_file_that_appears_while_it_writes(self):
        path = resources.limits_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        real_read, real_link = resources.host_limits, os.link

        def read_then_written(*arguments, **options):
            # The file is absent when setup checks it, and its owner writes one before setup reads it to add its key.
            found = real_read(*arguments, **options)
            if not path.exists(): path.write_text('{"busyLimit": 85}')
            return found
        with patch.object(resources, "host_limits", side_effect=read_then_written):
            self.assertEqual(resources.setup_limits(), (False, 85.0))
        self.assertEqual(path.read_text(), '{"busyLimit": 85}')
        path.unlink()

        def written_then_linked(source, target):
            # Or its owner writes one after setup read none, before setup's own is in place: a link never replaces.
            if not path.exists(): path.write_text('{"busyLimit": 80, "slots": 2}')
            return real_link(source, target)
        with patch.object(resources.os, "link", side_effect=written_then_linked):
            self.assertEqual(resources.setup_limits(), (False, 80.0))
        self.assertEqual(path.read_text(), '{"busyLimit": 80, "slots": 2}')
        self.assertEqual([entry.name for entry in path.parent.iterdir() if entry.name.startswith(".")], [])

    def test_a_hosts_limits_file_sets_its_limits_as_numbers_or_shares_of_the_cores(self):
        for name in ["HEAVY_SLOTS", "HEAVY_SLOTS_MAX"]: os.environ.pop(name)
        path = self.write_limits({"slots": {"perCore": 0.4}, "slotsMax": 6, "busyLimit": 90,
                                  "settleSeconds": 5})
        with patch.object(resources.os, "cpu_count", return_value=10):
            admission = resources.Admission("configured")
            self.assertEqual((admission.slots, admission.maximum, admission.busy_limit, admission.settle), (4, 6, 90.0, 5.0))
            self.assertEqual({key: source for key, (_value, source) in resources.limits().items()},
                             dict.fromkeys(resources.LIMIT_KEYS, str(path)))
            self.assertEqual(resources.busy_limit(), 90.0)
            # A share of the cores rounds a slot count down, never below one.
            self.write_limits({"slots": {"perCore": 0.01}, "slotsMax": {"perCore": 0.55}})
            self.assertEqual((resources.Admission("rounded").slots, resources.Admission("rounded").maximum), (1, 5))
            # Unset in the file, slotsMax defaults against the file's guaranteed slots.
            self.write_limits({"slots": 7})
            self.assertEqual(resources.Admission("floor").maximum, 7)

    def test_a_limits_environment_variable_overrides_the_hosts_file_for_its_process_alone(self):
        self.write_limits({"slots": 4, "slotsMax": 8, "busyLimit": 85})
        os.environ["HEAVY_BUSY_LIMIT"] = "60"
        found = resources.limits()
        self.assertEqual(found["slots"], (2, "HEAVY_SLOTS"))
        self.assertEqual(found["slotsMax"], (3, "HEAVY_SLOTS_MAX"))
        self.assertEqual(found["busyLimit"], (60.0, "HEAVY_BUSY_LIMIT"))
        self.assertEqual(found["settleSeconds"][0], 30.0)
        os.environ.pop("HEAVY_BUSY_LIMIT")
        self.assertEqual(resources.limits()["busyLimit"], (85.0, str(resources.limits_path())))

    def test_a_quoted_limit_says_it_is_the_macs_and_where_it_is_set(self):
        # On 2026-11-12 "at or above HEAVY_BUSY_LIMIT=70%" was read as a project's limit, when it was the Mac's, from
        # its limits file (vocaro/queuekeeper#29). Each source by its words, here and in another Mac's reading.
        os.environ.pop("HEAVY_SLOTS_MAX")
        path = self.write_limits({"busyLimit": 70})
        os.environ["HEAVY_BUSY_LIMIT"] = "60"
        said = resources.limits_words()
        self.assertEqual(said["busyLimit"], "HEAVY_BUSY_LIMIT=60% (this Mac's limit, set in the environment)")
        self.assertEqual(said["slotsMax"], f"HEAVY_SLOTS_MAX={resources.limits()['slotsMax'][0]} (this Mac's limit, "
                                           f"the default, since {path} does not set it)")
        os.environ.pop("HEAVY_BUSY_LIMIT")
        self.assertEqual(resources.limits_words()["busyLimit"],
                         f"HEAVY_BUSY_LIMIT=70% (this Mac's limit, set in {path})")
        with patch.object(resources, "cpu_busy", return_value=(76.0, 15.0)):
            self.assertEqual(resources.pressure(), ("cpu", f"the CPU is 76% busy, at or above HEAVY_BUSY_LIMIT=70% "
                                                           f"(this Mac's limit, set in {path})"))
        # Another Mac's reading says where it sets each limit, and the line names that Mac; an older toolkit's
        # reading does not say where, and the line says only whose the limit is.
        from queuekeeper import offload_host, remote
        os.environ.pop("HEAVY_SLOTS")
        reading = offload_host.capacity()
        self.assertEqual((reading["limitsFrom"]["busyLimit"], reading["limitsFrom"]["slots"], reading["limitsFile"]),
                         (str(path), "default", str(path)))
        self.assertEqual(remote.limit_words(reading, "busyLimit", 70.0, "build-host"),
                         f"HEAVY_BUSY_LIMIT=70% (build-host's limit, set in {path})")
        self.assertEqual(remote.limit_words(reading, "slots", 3, "build-host"),
                         f"HEAVY_SLOTS=3 (build-host's limit, the default, since {path} does not set it)")
        self.assertEqual(remote.limit_words({}, "slotsMax", 5, "build-host"), "HEAVY_SLOTS_MAX=5 (build-host's limit)")

    def test_a_limits_file_admission_cannot_read_refuses_admission_and_names_the_file(self):
        cases = {"not JSON": ("{slots: 4", "is not JSON"), "a list": ([4], "holds no JSON object"),
                 "an unknown key": ({"slot": 4}, "names slot, not one of slots"),
                 "a fractional slot count": ({"slots": 2.5}, "sets slots to 2.5"),
                 "a negative limit": ({"busyLimit": -1}, "sets busyLimit to -1"),
                 "a boolean": ({"settleSeconds": True}, "sets settleSeconds to true"),
                 "a text value": ({"busyLimit": "30"}, 'sets busyLimit to "30"'),
                 "a zero share": ({"busyLimit": {"perCore": 0}}, "sets busyLimit to"),
                 # The load average no longer judges, so a limit on it is no longer one admission reads.
                 "the former load limit": ({"loadLimit": 30}, "names loadLimit, not one of"),
                 "a share beside another key": ({"slots": {"perCore": 0.4, "max": 5}}, "sets slots to"),
                 "a maximum below the guarantee": ({"slotsMax": 1}, f"HEAVY_SLOTS_MAX=1 ({resources.limits_path()})")}
        os.environ.pop("HEAVY_SLOTS_MAX")
        os.environ.pop("HEAVY_SLOTS")
        for name, (value, reason) in cases.items():
            with self.subTest(name):
                path = self.write_limits(value)
                with self.assertRaisesRegex(ValueError, re.escape(reason)) as raised:
                    resources.Admission("refused")
                self.assertIn(str(path), str(raised.exception))
                output, error = StringIO(), StringIO()
                with contextlib.redirect_stderr(error):
                    status = resources.limits_main([], out=lambda line: output.write(line + "\n"))
                self.assertEqual((status, output.getvalue()), (2, ""))
                self.assertIn(reason, error.getvalue())
        # Positive control: the same path with a valid file admits.
        self.write_limits({"slots": 2, "slotsMax": 3})
        with resources.Admission("valid", timeout=0) as admitted: self.assertEqual(len(admitted.descriptors), 1)

    def test_the_files_busy_limit_opens_or_withholds_extra_slots_and_the_message_names_it(self):
        self.hold(0, 1)
        for limit, admitted in [(95, True), (40, False)]:
            with self.subTest(limit=limit):
                self.write_limits({"busyLimit": limit, "settleSeconds": 0})
                with patch.object(resources, "cpu_busy", return_value=(90.0, 15.0)), \
                        patch.object(resources, "memory_level", return_value=1), contextlib.redirect_stderr(StringIO()):
                    if admitted:
                        with resources.Admission("extra", timeout=0) as admission:
                            self.assertEqual([slot for slot, _fd in admission.descriptors], [2])
                        self.assertIsNone(resources.pressure())
                    else:
                        with self.assertRaisesRegex(TimeoutError, re.escape(
                                "all 2 guaranteed slots are busy and the CPU is 90% busy, at or above "
                                + mine("HEAVY_BUSY_LIMIT=40%", f"set in {resources.limits_path()}"))):
                            with resources.Admission("blocked", timeout=0): pass
                        # The merge queue's settle test reads the same limit, quoted as this Mac's.
                        self.assertEqual(resources.pressure(), ("cpu", "the CPU is 90% busy, at or above HEAVY_BUSY_LIMIT=40% "
                                                                       f"(this Mac's limit, set in {resources.limits_path()})"))

    def test_the_files_guaranteed_slots_admit_under_the_cpu_limit_and_no_more(self):
        for name in ["HEAVY_SLOTS", "HEAVY_SLOTS_MAX"]: os.environ.pop(name)
        self.hold(0, 1)
        self.write_limits({"slots": 3, "slotsMax": 3})
        with patch.object(resources, "cpu_busy", return_value=(89.9, 15.0)), contextlib.redirect_stderr(StringIO()):
            with resources.Admission("third", timeout=0) as admission:
                self.assertEqual([slot for slot, _fd in admission.descriptors], [2])
            self.write_limits({"slots": 2, "slotsMax": 2})
            with self.assertRaisesRegex(TimeoutError, re.escape(
                    "all 2 guaranteed slots are busy and "
                    f"{mine('HEAVY_SLOTS_MAX=2', f'set in {resources.limits_path()}')} allows none")):
                with resources.Admission("refused", timeout=0): pass

    def test_over_its_cpu_limit_a_mac_admits_only_its_first_job(self):
        """The owner's decision, 2026-11-17 (vocaro/queuekeeper#211): a Mac's CPU limit bounds every admission but the
        first. It reversed 2026-11-07's rule that a guaranteed slot admits at any reading."""
        path = self.write_limits({"busyLimit": 70, "settleSeconds": 0})
        over = ("the CPU is 92% busy, at or above " + mine("HEAVY_BUSY_LIMIT=70%", f"set in {path}")
                + ", and admitted work runs here: over its CPU limit a Mac admits only its first job")
        busy = lambda share: patch.object(resources, "cpu_busy", return_value=(share, 15.0))
        with busy(92.0), patch.object(resources, "memory_level", return_value=1), contextlib.redirect_stderr(StringIO()):
            # Nothing admitted runs here, so one job is admitted whatever the CPU reads: load that never asks
            # admission can delay work, and never stop all of it.
            with resources.Admission("first", timeout=0) as first, patch.dict(os.environ):
                self.assertEqual([slot for slot, _fd in first.descriptors], [0])
                os.environ.pop(resources.TOKEN)
                # A second finds a guaranteed slot free, and the Mac over its limit with work running.
                with self.assertRaisesRegex(TimeoutError, re.escape(over)) as refused:
                    with resources.Admission("second", timeout=0): self.fail("admitted over the limit beside work")
                self.assertIn("Resource queue busy on", str(refused.exception))
                waited = resources.Admission("second", timeout=0)
                self.assertFalse(waited._attempt())
                self.assertEqual((waited.refusal, waited.waiting), ("cpu", over))
                self.assertEqual(waited.descriptors, [])
                waited._withdraw(TimeoutError())
                # A model run is held back the same way, and keeps neither a slot nor the model.
                with self.assertRaisesRegex(TimeoutError, re.escape(over)):
                    with resources.Admission("generate", model=True, timeout=0): pass
                with busy(10.0), resources.Admission("generate", model=True, timeout=0) as model:
                    self.assertIsNotNone(model.held_model)
                # At the limit is over it; under it, the slot opens; and a share that cannot be read, or spans too
                # little yet, refuses nothing, as before.
                with busy(70.0), self.assertRaisesRegex(TimeoutError, "the CPU is 70% busy, at or above "):
                    with resources.Admission("second", timeout=0): pass
                for share in ((69.9, 15.0), (None, None), (None, 2.0)):
                    with self.subTest(share=share), patch.object(resources, "cpu_busy", return_value=share):
                        with resources.Admission("second", timeout=0) as second:
                            self.assertEqual([slot for slot, _fd in second.descriptors], [1])
                        os.environ.pop(resources.TOKEN, None)
            os.environ.pop(resources.TOKEN, None)
            # A slot held with no guard entry, as by a caller of the old slot API, is work that runs here too.
            self.hold(1)
            with self.assertRaisesRegex(TimeoutError, re.escape(over)):
                with resources.Admission("beside an old holder", timeout=0): pass

    def test_a_whole_host_lane_waits_out_a_mac_over_its_limit_and_starts_on_one_with_nothing_admitted(self):
        self.write_limits({"busyLimit": 70, "settleSeconds": 0})
        with patch.object(resources, "memory_level", return_value=1), contextlib.redirect_stderr(StringIO()):
            # Work beyond the guaranteed slots leaves them all free, which a whole-host lane needs, and still runs.
            self.hold(2)
            with patch.object(resources, "cpu_busy", return_value=(92.0, 15.0)):
                with self.assertRaisesRegex(TimeoutError, "the CPU is 92% busy, at or above .*admitted work runs here"):
                    with resources.Admission("gate", whole_host=True, timeout=0): pass
            with patch.object(resources, "cpu_busy", return_value=(50.0, 15.0)), patch.dict(os.environ):
                with resources.Admission("gate", whole_host=True, timeout=0) as gate:
                    self.assertEqual([slot for slot, _fd in gate.descriptors], [0, 1])

    def test_a_mac_with_nothing_admitted_starts_a_whole_host_lane_or_exclusive_work_at_any_reading(self):
        self.write_limits({"busyLimit": 70, "settleSeconds": 0})
        with patch.object(resources, "cpu_busy", return_value=(100.0, 15.0)), \
                patch.object(resources, "memory_level", return_value=4), contextlib.redirect_stderr(StringIO()):
            for need in ({"whole_host": True}, {"exclusive": True}, {}):
                with self.subTest(need=need), patch.dict(os.environ):
                    with resources.Admission("alone", timeout=0, **need) as alone:
                        self.assertTrue(alone.descriptors)

    def test_an_entry_recovery_left_whose_owner_ended_is_not_work_that_keeps_an_over_limit_mac_shut(self):
        # Counted as the slot it held, it must not also count as running work: over the limit it would then hold
        # every request back until someone removed it.
        self.write_limits({"busyLimit": 70, "settleSeconds": 0})
        request = resources.Admission("beside a stranded entry", timeout=0)
        state = {"vocaroEngineering": 1, "owners": {"dead": {"pid": 1, "start": "never", "label": "gone"}}}
        with patch.object(resources, "cpu_busy", return_value=(95.0, 15.0)):
            request.busy = resources.cpu_busy(request.directory)
            request.stranded = {"dead": (state["owners"]["dead"], "its processes could not be checked")}
            self.assertIsNone(request._over_limit(state))
            request.stranded = {}
            self.assertIn("admitted work runs here", request._over_limit(state))

    def place(self, label="Example gate (waiting, sent by studio)", age=60.0):
        """A waiting whole-host lane's place in this queue as its sender's holder keeps it (place_holder.hold_place),
        owned here by this process: its identity."""
        from queuekeeper import offload_host
        identity = uuid.uuid4().hex
        place_holder.place_held(identity, {"identity": identity, "pid": os.getpid(), "start": resources.process_start(os.getpid()),
                                           "label": label, "queuedAt": time.time() - age, "wholeHost": True,
                                           "heldFor": "studio"})
        return identity

    def queued(self):
        return [waiter["identity"] for waiter in json.loads((resources.slot_directory() / resources.QUEUE).read_text())["waiters"]]

    def test_a_place_held_for_a_gate_that_waits_on_another_mac_keeps_the_guaranteed_slots_and_its_own_run_passes_it(self):
        """vocaro/queuekeeper#211: a waiting gate takes a place in each Mac's queue. Here it is a queued whole-host
        request, so the guaranteed slots drain toward it; the run its sender then offers names it, and is not held
        back by its own place."""
        os.environ["HEAVY_SLOTS_MAX"] = "2"
        identity = self.place()
        held = "a whole-host lane queued ahead of it: Example gate (waiting, sent by studio)"
        with contextlib.redirect_stderr(StringIO()):
            # Later work takes no guaranteed slot, and a later gate waits its turn.
            with self.assertRaisesRegex(TimeoutError, re.escape(f"a guaranteed slot is free but kept for {held}")):
                with resources.Admission("later", timeout=0): pass
            with self.assertRaisesRegex(TimeoutError, re.escape(f"it waits behind {held}")):
                with resources.Admission("another gate", whole_host=True, timeout=0): pass
            # The run sent for the place: its provision lane, one slot, and then the gate, each admitted past it.
            with patch.dict(os.environ, {resources.PLACE: identity}):
                with resources.Admission("provision", timeout=0) as provision:
                    self.assertEqual([slot for slot, _fd in provision.descriptors], [0])
                    # What a command starts does not pass the place its lane was sent for.
                    with resources.child_requests():
                        self.assertNotIn(resources.PLACE, os.environ)
                    self.assertEqual(os.environ[resources.PLACE], identity)
                os.environ.pop(resources.TOKEN, None)
                with resources.Admission("Example gate", whole_host=True, timeout=0) as gate:
                    self.assertEqual([slot for slot, _fd in gate.descriptors], [0, 1])
                    # The place is its holder's to take out, not the run's.
                    self.assertEqual(self.queued(), [identity])
                os.environ.pop(resources.TOKEN, None)
            # Another run's place is not this run's: it holds this one back.
            with patch.dict(os.environ, {resources.PLACE: uuid.uuid4().hex}):
                with self.assertRaisesRegex(TimeoutError, re.escape(f"it waits behind {held}")):
                    with resources.Admission("another gate", whole_host=True, timeout=0): pass
            # Work older than the place still goes first, the run's own included behind it.
            older = uuid.uuid4().hex
            from queuekeeper import offload_host
            place_holder.place_held(older, {"identity": older, "pid": os.getpid(), "start": resources.process_start(os.getpid()),
                                            "label": "an older gate", "queuedAt": time.time() - 600, "wholeHost": True})
            self.assertEqual(self.queued(), [older, identity])
            with patch.dict(os.environ, {resources.PLACE: identity}):
                with self.assertRaisesRegex(TimeoutError, "it waits behind a whole-host lane queued ahead of it: an older gate"):
                    with resources.Admission("Example gate", whole_host=True, timeout=0): pass
            place_holder.place_held(older)
            # A place its sender has not renewed within HEAVY_PLACE_SECONDS holds nothing back.
            with patch.dict(os.environ, {"HEAVY_PLACE_SECONDS": "0"}):
                with resources.Admission("later", timeout=0) as later:
                    self.assertEqual([slot for slot, _fd in later.descriptors], [0])
                os.environ.pop(resources.TOKEN, None)

    def test_a_reading_of_this_mac_does_not_count_the_asking_works_own_place_as_queued_ahead(self):
        from queuekeeper import offload_host, remote
        identity = self.place()
        reading = offload_host.capacity()
        self.assertEqual(reading["kept"], "Example gate (waiting, sent by studio)")
        self.assertIn("which the lane would wait behind", remote.spare({**reading, "screen": None}, "this Mac", whole_host=True))
        for own in (offload_host.capacity(own=identity), offload_host.answer({"capacity": True, "own": identity})):
            self.assertIsNone(own["kept"])
            self.assertIsNone(remote.spare({**own, "screen": None}, "this Mac", whole_host=True))
        # The sender names it in each reading it asks of another Mac while it offers the work.
        with resources.request_order(time.time() - 60, identity), patch.object(remote, "ask", return_value=reading) as ask:
            remote.capacity_there("fake")
        self.assertEqual(ask.call_args.args[1]["own"], identity)
        self.assertIsNone(resources.request_place())
        with patch.object(remote, "ask", return_value=reading) as ask:
            remote.capacity_there("fake")
        self.assertNotIn("own", ask.call_args.args[1])

    def test_eng_admissions_limits_prints_each_limit_and_its_source(self):
        path = self.write_limits({"busyLimit": 80})
        lines = []
        with patch.object(resources.os, "cpu_count", return_value=10):
            self.assertEqual(resources.limits_main([], out=lines.append), 0)
        self.assertEqual(lines, [f"10 cores; limits file {path}", "HEAVY_SLOTS=2  (slots; HEAVY_SLOTS)",
                                 "HEAVY_SLOTS_MAX=3  (slotsMax; HEAVY_SLOTS_MAX)", f"HEAVY_BUSY_LIMIT=80  (busyLimit; {path})",
                                 "HEAVY_SETTLE_SECONDS=30  (settleSeconds; default: 30)",
                                 # What the Mac takes, beside how much (vocaro/queuekeeper#211): every default here.
                                 "takes: whole-host gates yes (default), exclusive work yes (default), model runs yes "
                                 "(default), foreground work no (default); settings file "
                                 f"{Path(path).with_name('mac-settings.json')}", "labels: none"])

    def test_exclusive_admission_holds_all_legacy_slots_and_nested_calls_reuse_it(self):
        with resources.Admission("test", exclusive=True, timeout=0):
            self.assertIsNone(resources.take_slot(resources.slot_directory(), 3))
            with resources.Admission("nested", timeout=0) as nested:
                self.assertTrue(nested.reused)
            self.assertTrue(Path(os.environ["QUEUEKEEPER_GATE_LOCK_FILE"] + ".warm-push-guard").exists())
        self.assertFalse(Path(os.environ["QUEUEKEEPER_GATE_LOCK_FILE"] + ".warm-push-guard").exists())

    def test_exclusive_escalation_inside_shared_work_fails_instead_of_deadlocking(self):
        with resources.Admission("shared", timeout=0):
            with self.assertRaises(RuntimeError):
                with resources.Admission("exclusive", exclusive=True, timeout=0): pass

    def request(self, label, exclusive=False, model=False, variables=None, whole_host=False):
        """A request in a process of its own, and the first line it printed: its admission, or why it waits.

        Once admitted it appends its label to admitted.log, then holds admission until its stdin closes.
        """
        log = Path(self.temp.name) / "admitted.log"
        script = ("import sys\nfrom queuekeeper import resources\n"
                  f"with resources.Admission({label!r}, exclusive={exclusive}, timeout={stretched(60)}, model={model}, "
                  f"whole_host={whole_host}):\n"
                  f"    with open({str(log)!r}, 'a') as log: log.write({label!r} + '\\n')\n"
                  "    sys.stdin.read()\n")
        environment = {**os.environ, **(variables or {}), "PYTHONPATH": host_isolation.pythonpath()}
        # Its descriptors stay here, so it cannot inherit an admission this process holds.
        environment.pop(resources.TOKEN, None)
        request = subprocess.Popen([sys.executable, "-c", script], stdin=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, env=environment)
        def end():
            if request.poll() is None:
                request.kill()
                request.wait()
            request.stdin.close()
            request.stderr.close()
        self.addCleanup(end)
        return request, request.stderr.readline()

    def request_exclusive(self, label, **variables):
        """An exclusive request in a process of its own, and the line it printed when it began to wait."""
        request, line = self.request(label, exclusive=True, variables=variables)
        # Any whole seconds: at a load near 500 a first attempt took over a second (#128).
        self.assertRegex(line, r"heavy: waiting \(\d+s\): ")
        return request, line

    def admitted(self, count):
        """The labels of the requests admitted so far, once there are count of them."""
        log, deadline = Path(self.temp.name) / "admitted.log", time.monotonic() + stretched(30)
        while True:
            labels = log.read_text().split() if log.exists() else []
            if len(labels) >= count: return labels
            self.assertLess(time.monotonic(), deadline, f"{count} requests were not admitted")
            time.sleep(0.01)

    def test_heavy_work_requested_behind_an_exclusive_request_waits_until_the_exclusive_job_has_run(self):
        # Heavy jobs kept taking freed slots while exclusive gates waited for them, for hours (#21).
        directory, log = resources.slot_directory(), Path(self.temp.name) / "admitted.log"
        directory.mkdir(parents=True)
        # Heavy work already running when the exclusive request asks.
        running = os.fdopen(resources.take_slot(directory, 1)[1])
        self.addCleanup(running.close)
        exclusive, line = self.request_exclusive("exclusive")
        self.assertIn(f"exclusive work needs every slot up to {mine('HEAVY_SLOTS_MAX=3')} free, and 1 is busy", line)
        # It ends as soon as it is admitted.
        exclusive.stdin.close()
        # A guaranteed slot is free, and the heavy request still waits.
        with contextlib.redirect_stderr(StringIO()) as error, self.assertRaisesRegex(TimeoutError, "behind exclusive work queued ahead of it: exclusive"):
            with resources.Admission("heavy", timeout=0): self.fail("admitted ahead of exclusive work")
        self.assertRegex(error.getvalue(), r"heavy: waiting \(\d+s\): "
                         + re.escape("it waits behind exclusive work queued ahead of it: exclusive\n"))
        # The running job finishes normally, then the exclusive job runs, and only then the heavy one.
        running.close()
        with contextlib.redirect_stderr(StringIO()), resources.Admission("heavy", timeout=stretched(30)) as heavy:
            self.assertEqual(log.read_text() if log.exists() else "", "exclusive\n")
            self.assertEqual([slot for slot, _fd in heavy.descriptors], [0])
        self.assertEqual(exclusive.wait(timeout=stretched(30)), 0)
        self.assertFalse((directory / "exclusive-queue.json").exists())

    def test_an_exclusive_request_that_ended_or_changed_identity_does_not_hold_heavy_work(self):
        queue = resources.slot_directory() / "exclusive-queue.json"
        self.hold(0)
        killed, _line = self.request_exclusive("killed")
        self.assertEqual([waiter["label"] for waiter in json.loads(queue.read_text())["waiters"]], ["killed"])
        # SIGKILL leaves its record behind.
        killed.kill()
        killed.wait()
        self.assertTrue(queue.exists())
        with contextlib.redirect_stderr(StringIO()), resources.Admission("heavy", timeout=0) as heavy:
            self.assertEqual([slot for slot, _fd in heavy.descriptors], [1])
        self.assertFalse(queue.exists())
        # A PID that now names another process: this one, which asked for nothing exclusive.
        queue.write_text(json.dumps({"vocaroEngineering": 1, "waiters": [{"identity": "reused", "pid": os.getpid(),
                                     "start": "Thu Jan  1 00:00:00 1970", "label": "reused"}]}))
        with contextlib.redirect_stderr(StringIO()), resources.Admission("heavy", timeout=0): pass
        self.assertFalse(queue.exists())

    def test_exclusive_requests_are_admitted_one_at_a_time_in_arrival_order(self):
        directory = resources.slot_directory()
        directory.mkdir(parents=True)
        running = os.fdopen(resources.take_slot(directory, 1)[1])
        self.addCleanup(running.close)
        # The second polls a hundred times as often, so it would usually take freed slots first.
        first, _line = self.request_exclusive("first", HEAVY_POLL_SECONDS="1")
        second, line = self.request_exclusive("second")
        self.assertIn("it waits behind exclusive work queued ahead of it: first", line)
        running.close()
        self.assertEqual(self.admitted(1), ["first"])
        # The second waits for the first job, which holds every slot. Fixed: a window in which nothing may happen,
        # which load can only make pass.
        time.sleep(0.2)
        self.assertEqual(self.admitted(1), ["first"])
        first.stdin.close()
        self.assertEqual(self.admitted(2), ["first", "second"])
        second.stdin.close()
        self.assertEqual((first.wait(timeout=stretched(30)), second.wait(timeout=stretched(30))), (0, 0))
        self.assertFalse((directory / "exclusive-queue.json").exists())

    def test_an_exclusive_request_that_times_out_or_is_stopped_leaves_the_queue(self):
        queue = resources.slot_directory() / "exclusive-queue.json"
        self.hold(0)
        real, queued = resources.Admission._attempt, []
        def attempt(admission):
            admitted = real(admission)
            queued.append([waiter["label"] for waiter in json.loads(queue.read_text())["waiters"]] if queue.exists() else [])
            return admitted
        self.addCleanup(processes._stops.clear)
        cases = [("timed out", TimeoutError, re.escape(f"exclusive work needs every slot up to {mine('HEAVY_SLOTS_MAX=3')} "
                                                       "free, and 1 is busy"), 0),
                 ("stopped", processes.Interrupted, "while waiting for admission", 60)]
        for label, failure, reason, timeout in cases:
            with self.subTest(label), patch.object(resources.Admission, "_attempt", attempt), contextlib.redirect_stderr(StringIO()):
                queued.clear()
                if failure is processes.Interrupted: processes.request_stop(signal.SIGTERM)
                with self.assertRaisesRegex(failure, reason):
                    with resources.Admission(label, exclusive=True, timeout=timeout): self.fail("admitted")
                processes._stops.clear()
                # It waited in the queue, and left it.
                self.assertEqual(queued, [[label]])
                self.assertFalse(queue.exists())
                with resources.Admission("heavy", timeout=0): pass

    def waiter(self, label, **fields):
        """A queued request's record, as admission writes it, for this process, which runs as itself."""
        return {"identity": uuid.uuid4().hex, "pid": os.getpid(), "start": resources.process_start(os.getpid()),
                "label": label, **fields}

    def queue(self, *waiters):
        queue = resources.slot_directory() / resources.QUEUE
        queue.parent.mkdir(parents=True, exist_ok=True)
        queue.write_text(json.dumps({"vocaroEngineering": 1, "waiters": list(waiters)}))
        return queue

    def withdrawn(self):
        """The kind of the last refusal of each withdrawn request the log records."""
        lines = [json.loads(line) for line in resources.log_path().read_text().splitlines()]
        return [line["wait"]["last"] for line in lines if line["event"] == "withdrawn"]

    def test_a_waiting_whole_host_lane_keeps_the_guaranteed_slots_from_later_heavy_work_until_they_drain(self):
        """vocaro-engineering#174: a whole-host lane waiting for the guaranteed slots held nothing, so heavy jobs started
        one after another, each taking the slot the last freed, kept one busy for good and the gate waited until its
        limit. Now the slots drain toward it, while the later heavy work goes on beyond them."""
        directory = resources.slot_directory()
        directory.mkdir(parents=True)
        os.environ["HEAVY_SETTLE_SECONDS"] = "0"
        # Heavy work already running when the gate asks.
        running = os.fdopen(resources.take_slot(directory, 1)[1])
        self.addCleanup(running.close)
        gate, line = self.request("gate", whole_host=True)
        self.assertIn("a whole-host lane needs every guaranteed slot (HEAVY_SLOTS=2) free, and 1 is busy", line)
        # The regression: a guaranteed slot is free, and a later heavy request no longer takes it. Before, it took
        # slot 1 here, the next took slot 0 once the first job freed it, and so on for as long as heavy work came.
        with patch.dict(os.environ, {"HEAVY_SLOTS_MAX": "2"}), contextlib.redirect_stderr(StringIO()) as error, \
                self.assertRaisesRegex(TimeoutError, re.escape("a guaranteed slot is free but kept for a whole-host lane queued "
                                                               f"ahead of it: gate; and {mine('HEAVY_SLOTS_MAX=2')} "
                                                               "allows none beyond them")):
            with resources.Admission("tests", timeout=0): self.fail("took a guaranteed slot the gate waits for")
        # A loaded host may take a second over the attempt (#15).
        self.assertRegex(error.getvalue(), r"heavy: waiting \(\d+s\): a guaranteed slot is free but kept")
        # The gate waits in the queue, which a whole-host request joins as an exclusive one does.
        queued = json.loads((directory / resources.QUEUE).read_text())["waiters"]
        self.assertEqual([(waiter["label"], waiter["wholeHost"]) for waiter in queued], [("gate", True)])
        # Beyond the guaranteed slots, while the host shows spare capacity, heavy work still goes on.
        with patch.object(resources, "cpu_busy", return_value=(10.0, 15.0)), \
                patch.object(resources, "memory_level", return_value=1), contextlib.redirect_stderr(StringIO()), \
                resources.Admission("tests", timeout=0) as beside:
            self.assertEqual([slot for slot, _fd in beside.descriptors], [2])
            # With no slot beyond them free, the next waits for one, not for a guaranteed one.
            with patch.dict(os.environ), self.assertRaisesRegex(TimeoutError, re.escape(
                    "kept for a whole-host lane queued ahead of it: gate; and every slot beyond them up to "
                    f"{mine('HEAVY_SLOTS_MAX=3')} is busy")):
                os.environ.pop(resources.TOKEN)
                with resources.Admission("more tests", timeout=0): self.fail("admitted")
            # The job running when the gate asked ends, and the gate is admitted beside the later heavy work.
            running.close()
            self.assertEqual(self.admitted(1), ["gate"])
        gate.stdin.close()
        self.assertEqual(gate.wait(timeout=stretched(30)), 0)
        self.assertFalse((directory / resources.QUEUE).exists())
        # The log counts those refusals as the whole-host lane's.
        self.assertEqual(self.withdrawn(), ["wholeHost", "wholeHost"])
        # With every guaranteed slot busy, a heavy request's refusal is the slots', whatever waits.
        self.hold(0, 1)
        self.queue(self.waiter("gate", wholeHost=True, seen=time.time()))
        with patch.dict(os.environ, {"HEAVY_SLOTS_MAX": "2"}), contextlib.redirect_stderr(StringIO()), \
                self.assertRaisesRegex(TimeoutError, re.escape("inspection (all 2 guaranteed slots are busy and "
                                                               f"{mine('HEAVY_SLOTS_MAX=2')} allows none beyond them)")):
            with resources.Admission("tests", timeout=0): self.fail("admitted")
        self.assertEqual(self.withdrawn()[-1], "slots")

    def test_whole_host_and_exclusive_requests_are_admitted_in_arrival_order(self):
        """A whole-host request queued ahead holds back the exclusive and whole-host requests behind it, and an
        exclusive request queued ahead holds back a whole-host one, as it does every other request."""
        whole, exclusive = self.waiter("EPUBLib gate", wholeHost=True, seen=time.time()), self.waiter("bench")
        behind_whole = "it waits behind a whole-host lane queued ahead of it: EPUBLib gate"
        cases = [([whole], {"whole_host": True}, behind_whole), ([whole], {"exclusive": True}, behind_whole),
                 ([exclusive], {"whole_host": True}, "it waits behind exclusive work queued ahead of it: bench"),
                 ([whole, exclusive], {}, None), ([exclusive, whole], {}, "it waits behind exclusive work queued ahead of it: bench")]
        for waiters, kind, reason in cases:
            with self.subTest(waiters=[waiter["label"] for waiter in waiters], **kind), contextlib.redirect_stderr(StringIO()):
                queue = self.queue(*waiters)
                if reason is None:
                    # Heavy work behind a whole-host request takes a slot beyond the guaranteed ones.
                    with patch.object(resources, "cpu_busy", return_value=(10.0, 15.0)), \
                            patch.object(resources, "memory_level", return_value=1), \
                            patch.dict(os.environ, {"HEAVY_SETTLE_SECONDS": "0"}), \
                            resources.Admission("tests", timeout=0) as admitted:
                        self.assertEqual([slot for slot, _fd in admitted.descriptors], [2])
                else:
                    with self.assertRaisesRegex(TimeoutError, re.escape(reason)):
                        with resources.Admission("asked", timeout=0, **kind): self.fail("admitted")
                # The request behind left the queue; those ahead keep their places.
                self.assertEqual(json.loads(queue.read_text())["waiters"], waiters)
        self.assertEqual(self.withdrawn(), ["wholeHost", "wholeHost", "exclusive", "exclusive"])

    def test_a_whole_host_request_that_ended_or_stopped_asking_holds_nothing_back(self):
        """A dead waiter must not leave the host blocked: a whole-host request whose process ended is dropped, as
        an exclusive one is, and one whose process has not asked within HEAVY_PLACE_SECONDS holds nothing back."""
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        dead = {**self.waiter("dead gate", wholeHost=True, seen=time.time()), "pid": process.pid,
                "start": resources.process_start(process.pid)}
        process.kill()
        process.wait()
        stale = self.waiter("away gate", wholeHost=True, seen=time.time() - resources.PLACE_SECONDS - 1)
        fresh = self.waiter("waiting gate", wholeHost=True, seen=time.time())
        unseen = self.waiter("unseen gate", wholeHost=True)
        with patch.dict(os.environ, {"HEAVY_SLOTS_MAX": "2"}), contextlib.redirect_stderr(StringIO()):
            # The negative control: one that asks holds the guaranteed slots back.
            queue = self.queue(fresh)
            with self.assertRaisesRegex(TimeoutError, "kept for a whole-host lane queued ahead of it: waiting gate"):
                with resources.Admission("tests", timeout=0): self.fail("admitted")
            self.queue(dead)
            with resources.Admission("tests", timeout=0) as admitted:
                self.assertEqual([slot for slot, _fd in admitted.descriptors], [0])
            self.assertFalse(queue.exists())
            for waiter in [stale, unseen]:
                with self.subTest(waiter["label"]):
                    self.queue(waiter)
                    with resources.Admission("tests", timeout=0) as admitted:
                        self.assertEqual([slot for slot, _fd in admitted.descriptors], [0])
                    # It keeps its place: its process runs, and may ask again.
                    self.assertEqual(json.loads(queue.read_text())["waiters"], [waiter])
            # One that stopped asking holds back no exclusive request either; one that asks, behind it, does.
            self.queue(stale, fresh)
            with self.assertRaisesRegex(TimeoutError, "behind a whole-host lane queued ahead of it: waiting gate"):
                with resources.Admission("bench", exclusive=True, timeout=0): self.fail("admitted")
            self.queue(stale)
            with resources.Admission("bench", exclusive=True, timeout=0) as admitted:
                self.assertEqual([slot for slot, _fd in admitted.descriptors], [0, 1])
            self.assertEqual(json.loads(queue.read_text())["waiters"], [stale])
            # HEAVY_PLACE_SECONDS sets how long one holds work back after it last asked.
            with patch.dict(os.environ, {"HEAVY_PLACE_SECONDS": str(10 * resources.PLACE_SECONDS)}), \
                    self.assertRaisesRegex(TimeoutError, "kept for a whole-host lane queued ahead of it: away gate"):
                with resources.Admission("tests", timeout=0): self.fail("admitted")

    def test_a_whole_host_place_said_to_wait_still_is_admitted_before_a_later_one_when_the_slots_free(self):
        """vocaro/queuekeeper#160: a gate offered to the Macs was away from this Mac's queue for about 260 s of each
        cycle, longer than HEAVY_PLACE_SECONDS, so its place held nothing back when the slots were freed and a gate
        that arrived 42 minutes after it was admitted. A place whose caller says that its work still waits
        (Place.still_waiting, as Place.kept does every 60 s) holds the later gate back however long it is away."""
        clock = [time.time()]
        def gate(label, place, slots=None):
            asked = resources.Admission(label, whole_host=True, place=place, timeout=0)
            if slots is None:
                with self.assertRaises(TimeoutError) as refused:
                    with asked: self.fail("admitted")
                return str(refused.exception)
            with asked as admitted: self.assertEqual([slot for slot, _fd in admitted.descriptors], slots)
        for said in (False, True):
            with self.subTest(said=said), patch.object(resources.time, "time", side_effect=lambda: clock[0]), \
                    contextlib.redirect_stderr(StringIO()):
                older, later = resources.Place(), resources.Place()
                older.started, later.started = clock[0] - 2400, clock[0] - 60
                directory = resources.slot_directory()
                directory.mkdir(parents=True, exist_ok=True)
                queue = directory / resources.QUEUE
                # A gate runs here, holding both guaranteed slots.
                running = [os.fdopen(resources.take_slot(directory, 2)[1]) for _slot in range(2)]
                for held in running: self.addCleanup(held.close)
                self.assertIn("a whole-host lane needs every guaranteed slot", gate("older gate", older))
                asked = clock[0]
                # It offers itself to another Mac, which declines it 260 s later, saying meanwhile that it waits.
                for _minute in range(4):
                    clock[0] += 60
                    if said: older.still_waiting()
                clock[0] += 10
                # The slots are freed, and the later gate is the one asking.
                for held in running: held.close()
                if not said:
                    # The negative control, and the fault: nothing said that the older gate still waited.
                    self.assertEqual(json.loads(queue.read_text())["waiters"][0]["seen"], asked)
                    gate("later gate", later, slots=[0, 1])
                else:
                    self.assertEqual([(waiter["label"], waiter["seen"], waiter["asked"])
                                      for waiter in json.loads(queue.read_text())["waiters"]],
                                     [("older gate", asked + 240, asked)])
                    self.assertIn("it waits behind a whole-host lane queued ahead of it: older gate", gate("later gate", later))
                    clock[0] += 10
                    # It returns from the offer and asks: admitted, ahead of the later gate, which keeps its place.
                    with resources.Admission("older gate", whole_host=True, place=older, timeout=0) as admitted:
                        self.assertEqual([slot for slot, _fd in admitted.descriptors], [0, 1])
                        self.assertEqual([waiter["label"] for waiter in json.loads(queue.read_text())["waiters"]],
                                         ["later gate"])
                    gate("later gate", later, slots=[0, 1])
                for place in (older, later): place.leave()
                self.assertFalse(queue.exists())

    def test_an_attempt_records_that_a_place_said_to_wait_still_asks_again(self):
        """`asked` says when a place said to wait still last asked, for `qk admissions now`; its next attempt asks
        now, and removes it."""
        place = resources.Place()
        directory = resources.slot_directory()
        directory.mkdir(parents=True)
        running = os.fdopen(resources.take_slot(directory, 1)[1])
        self.addCleanup(running.close)
        with contextlib.redirect_stderr(StringIO()):
            queue = directory / resources.QUEUE
            for _attempt in range(2):
                with self.assertRaises(TimeoutError):
                    with resources.Admission("older gate", whole_host=True, place=place, timeout=0): self.fail("admitted")
                found, = json.loads(queue.read_text())["waiters"]
                self.assertNotIn("asked", found)
                place.still_waiting()
                kept, = json.loads(queue.read_text())["waiters"]
                self.assertEqual(kept["asked"], found["seen"])
                # Said again, it still names the attempt, not the last saying.
                place.still_waiting()
                self.assertEqual(json.loads(queue.read_text())["waiters"][0]["asked"], found["seen"])
        place.leave()

    def test_a_place_is_kept_only_while_its_work_is_said_to_be_unplaced(self):
        """Place.kept says that the work still waits only while its caller says no Mac has taken it (#160). Once another
        Mac has taken the lane, or its placement cannot be told, nothing more is said, so the place stops holding
        later work back HEAVY_PLACE_SECONDS after it last asked, as before, and keeps its arrival. A place whose
        process has ended is dropped whatever it said."""
        place = resources.Place()
        away = time.time() - resources.PLACE_SECONDS - 80
        stale = {**self.waiter("away gate", wholeHost=True, seen=away), "identity": place.identity}
        with contextlib.redirect_stderr(StringIO()):
            # Unplaced: said at once here, every 60 s by default.
            queue, said, real = self.queue(stale), threading.Event(), place.still_waiting
            with patch.object(place, "still_waiting", side_effect=lambda: (real(), said.set())), \
                    place.kept(lambda: True, every=0.001):
                self.assertTrue(said.wait(30))
            found, = json.loads(queue.read_text())["waiters"]
            self.assertEqual(found["asked"], away)
            self.assertLess(time.time() - found["seen"], resources.PLACE_SECONDS)
            with self.assertRaisesRegex(TimeoutError, "behind a whole-host lane queued ahead of it: away gate"):
                with resources.Admission("later gate", whole_host=True, timeout=0): self.fail("admitted")
            # Taken by another Mac, or unknown: the keeper asks once and ends, having said nothing.
            for answer in (False, None):
                with self.subTest(unplaced=answer):
                    self.queue(stale)
                    asked = threading.Event()
                    with patch.object(place, "still_waiting") as told:
                        with place.kept(lambda: (asked.set(), answer)[1], every=0.001):
                            self.assertTrue(asked.wait(30))
                        told.assert_not_called()
                    with resources.Admission("later gate", whole_host=True, timeout=0) as admitted:
                        self.assertEqual([slot for slot, _fd in admitted.descriptors], [0, 1])
                    self.assertEqual(json.loads(queue.read_text())["waiters"], [stale])
            # Its process ended while it was said to wait: dropped, and what a keeper says later puts nothing back.
            process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            dead = {**stale, "pid": process.pid, "start": resources.process_start(process.pid), "seen": time.time(),
                    "asked": away}
            process.kill()
            process.wait()
            self.queue(dead)
            with resources.Admission("later gate", whole_host=True, timeout=0) as admitted:
                self.assertEqual([slot for slot, _fd in admitted.descriptors], [0, 1])
            self.assertFalse(queue.exists())
            place.still_waiting()
            self.assertFalse(queue.exists())
            # Nor does it put back a place that left the queue, admitted or not.
            self.queue(self.waiter("another gate", wholeHost=True, seen=away))
            place.still_waiting()
            self.assertEqual([waiter["label"] for waiter in json.loads(queue.read_text())["waiters"]], ["another gate"])

    def test_the_capacity_reading_keeps_the_guaranteed_slots_for_a_whole_host_request_as_admission_does(self):
        """offload_host.capacity, which queuekeeper reads before it offers work to a Mac (remote.spare), applies admission's
        rule for a queued whole-host request (vocaro-engineering#186): one whose process runs and asked within
        HEAVY_PLACE_SECONDS keeps the guaranteed slots, while a dead, stale or unseen one, one behind exclusive
        work, the asking work's own place and one that waits for a stranded guaranteed slot keep nothing."""
        from queuekeeper import offload_host, remote
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        ended = (process.pid, resources.process_start(process.pid))
        process.kill()
        process.wait()
        dead = {**self.waiter("dead gate", wholeHost=True, seen=time.time()), "pid": ended[0], "start": ended[1]}
        stale = self.waiter("away gate", wholeHost=True, seen=time.time() - resources.PLACE_SECONDS - 1)
        fresh = self.waiter("waiting gate", wholeHost=True, seen=time.time())
        unseen = self.waiter("unseen gate", wholeHost=True)
        exclusive = self.waiter("bench")
        guard = resources.guard_path(os.environ)
        def stranded(slot):
            """An uncertain guard entry whose owner, this process, still runs, named by that slot's file as its last
            holder: one whose owner has ended holds nothing, since admission frees it (vocaro/queuekeeper#23)."""
            guard.write_text(json.dumps({"vocaroEngineering": 1, "owners": {"gone": {
                "pid": os.getpid(), "start": resources.process_start(os.getpid()), "label": "stranded",
                "exclusive": False, "uncertain": True}}}))
            (resources.slot_directory() / f"slot{slot}").write_text(f"{os.getpid()} gone stranded\n")
        cases = [("one that asks", [fresh], None, "waiting gate"), ("its process ended", [dead], None, None),
                 ("it stopped asking", [stale], None, None), ("it never asked", [unseen], None, None),
                 ("behind one that stopped asking", [stale, fresh], None, "waiting gate"),
                 ("behind exclusive work", [exclusive, fresh], None, None),
                 ("the work's own place", [fresh], fresh["identity"], None),
                 ("behind the work's own place", [stale, fresh], stale["identity"], None),
                 ("beside a stranded guaranteed slot", [fresh], None, None),
                 ("beside a stranded slot beyond them", [fresh], None, "waiting gate")]
        with patch.dict(os.environ, {"HEAVY_SLOTS_MAX": "2"}), contextlib.redirect_stderr(StringIO()):
            for name, waiters, own, kept in cases:
                with self.subTest(name):
                    guard.unlink(missing_ok=True)
                    for slot in range(3): (resources.slot_directory() / f"slot{slot}").unlink(missing_ok=True)
                    os.environ["HEAVY_SLOTS_MAX"] = "3" if "stranded" in name else "2"
                    if "stranded" in name: stranded(0 if "guaranteed" in name else 2)
                    self.queue(*waiters)
                    self.assertEqual(offload_host.capacity(own=own)["kept"], kept)
                    # Admission's side of behind exclusive work and of stranded entries has tests of its own.
                    if own is None and "stranded" not in name and "exclusive" not in name:
                        # Admission agrees: a lighter lane is refused exactly when the reading says the slots are kept.
                        if kept is None:
                            with resources.Admission("tests", timeout=0): pass
                        else:
                            with self.assertRaisesRegex(TimeoutError, f"kept for a whole-host lane queued ahead of it: {kept}"):
                                with resources.Admission("tests", timeout=0): self.fail("admitted")
            guard.unlink()
            os.environ["HEAVY_SLOTS_MAX"] = "2"
            # HEAVY_PLACE_SECONDS sets how long one keeps them after it last asked, here as in admission.
            self.queue(stale)
            with patch.dict(os.environ, {"HEAVY_PLACE_SECONDS": str(10 * resources.PLACE_SECONDS)}):
                self.assertEqual(offload_host.capacity()["kept"], "away gate")
            # So queuekeeper offers a lighter lane no Mac whose guaranteed slots are kept and that has no slot beyond them.
            self.queue(fresh)
            read = {**offload_host.capacity(), "load": 0.0, "screen": None}
            self.assertEqual(remote.spare(read, "this Mac"), "the guaranteed slots of this Mac are kept for a whole-host "
                             "lane queued ahead: waiting gate, and every slot beyond them up to HEAVY_SLOTS_MAX=2 (this "
                             "Mac's limit, set in the environment) is busy")
            self.queue(stale)
            self.assertIsNone(remote.spare({**offload_host.capacity(), "load": 0.0, "screen": None}, "this Mac"))

    def test_the_capacity_reading_reports_exclusive_work_queued_ahead_as_admission_holds_it(self):
        """offload_host.capacity reports a live exclusive request queued ahead (queuedExclusive), behind which
        admission holds every later request, so that queuekeeper offers no lane to that Mac (vocaro-engineering#191). One
        whose process ended, and one queued beside a stranded entry counted as any slot, hold nothing back (#89);
        one behind a whole-host request that asks is that request's, which keeps the guaranteed slots instead."""
        from queuekeeper import offload_host, remote
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        ended = (process.pid, resources.process_start(process.pid))
        process.kill()
        process.wait()
        live = self.waiter("bench")
        dead = {**self.waiter("dead bench"), "pid": ended[0], "start": ended[1]}
        fresh = self.waiter("waiting gate", wholeHost=True, seen=time.time())
        stale = self.waiter("away gate", wholeHost=True, seen=time.time() - resources.PLACE_SECONDS - 1)
        guard = resources.guard_path(os.environ)
        def stranded(slot):
            """An uncertain guard entry whose owner, this process, still runs, named by that slot's file as its last
            holder: one whose owner has ended holds nothing, since admission frees it (vocaro/queuekeeper#23)."""
            guard.write_text(json.dumps({"vocaroEngineering": 1, "owners": {"gone": {
                "pid": os.getpid(), "start": resources.process_start(os.getpid()), "label": "stranded",
                "exclusive": False, "uncertain": True}}}))
            (resources.slot_directory() / f"slot{slot}").write_text(f"{os.getpid()} gone stranded\n")
        # (name, queue, stranded slot, queuedExclusive, kept)
        cases = [("one whose process runs", [live], None, "bench", None),
                 ("its process ended", [dead], None, None, None),
                 ("behind a dead one", [dead, live], None, "bench", None),
                 ("behind one that stopped asking", [stale, live], None, "bench", None),
                 ("behind one that asks", [fresh, live], None, None, "waiting gate"),
                 ("beside a stranded guaranteed slot", [live], 0, None, None),
                 ("beside a stranded slot beyond them", [live], 2, None, None),
                 ("nothing queued", [], None, None, None)]
        with patch.dict(os.environ, {"HEAVY_SLOTS_MAX": "3"}), contextlib.redirect_stderr(StringIO()):
            self.queue(live)
            with patch.object(offload_host, "cpu_busy", return_value=10) as cpu:
                diagnostic = offload_host.capacity(diagnostic=True)
            cpu.assert_called_once_with(resources.slot_directory(), record=False)
            self.assertEqual(diagnostic["queuedExclusive"], f"PID {live['pid']}")
            self.assertEqual(diagnostic["blockers"], [{"state": "queued", "kind": "exclusive", "pid": live["pid"],
                                                       "identity": live["identity"], "processStart": live["start"]}])
            for name, waiters, slot, queued, kept in cases:
                with self.subTest(name):
                    guard.unlink(missing_ok=True)
                    for each in range(3): (resources.slot_directory() / f"slot{each}").unlink(missing_ok=True)
                    if slot is not None: stranded(slot)
                    self.queue(*waiters)
                    read = offload_host.capacity()
                    self.assertEqual((read["queuedExclusive"], read["kept"]), (queued, kept))
                    read = {**read, "load": 0.0, "screen": None}
                    for need in [{}, {"whole_host": True}, {"exclusive": True}]:
                        refused = remote.spare(read, "this Mac", **need)
                        if queued is not None:
                            # queuekeeper offers that Mac no lane of any kind.
                            self.assertEqual(refused, "exclusive work is queued ahead on this Mac: bench", need)
                        elif kept is None:
                            self.assertIsNone(refused, need)
                    # Admission's side of stranded entries, which its recovery may remove, has tests of its own.
                    if slot is None:
                        # Admission agrees: a lighter lane is refused exactly when the reading names the request.
                        if queued is not None:
                            held = f"it waits behind exclusive work queued ahead of it: {queued}"
                            with self.assertRaisesRegex(TimeoutError, held):
                                with resources.Admission("tests", timeout=0): self.fail("admitted")
                        elif kept is None:
                            with resources.Admission("tests", timeout=0): pass

    def test_the_capacity_reading_withholds_a_slot_beyond_the_guaranteed_ones_as_admission_does(self):
        """While the guaranteed slots are kept for a whole-host request queued ahead, admission takes a slot beyond
        them only once the Mac has settled since its last admission and while its memory pressure is normal
        (_extra_refusal); offload_host.capacity reports both, and remote.spare refuses that slot by the same rule,
        so the reading and admission agree (vocaro-engineering#201)."""
        from queuekeeper import offload_host, remote
        directory = resources.slot_directory()
        held = "the guaranteed slots of this Mac are kept for a whole-host lane queued ahead: waiting gate, and "
        # (name, HEAVY_SETTLE_SECONDS, whether a slot was taken just now, memory level, spare's refusal, admission's)
        cases = [("settled, under normal pressure", "0", True, 1, None, None),
                 ("never admitted, under normal pressure", "3600", False, 1, None, None),
                 ("within the settle time", "3600", True, 1,
                  held + "the CPU of this Mac does not yet show the job admitted {since}s ago, within HEAVY_SETTLE_SECONDS=3600",
                  "s ago, within HEAVY_SETTLE_SECONDS=3600"),
                 ("under warning pressure", "0", True, 2, held + "on this Mac memory pressure is level 2 (warning), not 1 (normal)",
                  "memory pressure is level 2 (warning), not 1 (normal)"),
                 ("under critical pressure", "0", False, 4,
                  held + "on this Mac memory pressure is level 4 (critical), not 1 (normal)",
                  "memory pressure is level 4 (critical), not 1 (normal)"),
                 ("with pressure unread", "0", False, None,
                  held + "on this Mac memory pressure, which must be level 1 (normal), cannot be read",
                  "memory pressure, which must be level 1 (normal), cannot be read")]
        with patch.object(resources, "cpu_busy", return_value=(10.0, 15.0)), contextlib.redirect_stderr(StringIO()):
            for name, settle, recent, level, refused, reason in cases:
                with self.subTest(name), patch.dict(os.environ, {"HEAVY_SETTLE_SECONDS": settle}), \
                        patch.object(resources, "memory_level", return_value=level), \
                        patch.object(offload_host, "memory_level", return_value=level):
                    directory.mkdir(parents=True, exist_ok=True)
                    for slot in range(3): (directory / f"slot{slot}").unlink(missing_ok=True)
                    # A holder that took a slot and has ended, as admission's last admission is read.
                    if recent: (directory / "slot0").write_text("1 ended holder\n")
                    self.queue(self.waiter("waiting gate", wholeHost=True, seen=time.time()))
                    read = offload_host.capacity()
                    self.assertEqual((read["kept"], read["settle"], read["memory"]), ("waiting gate", float(settle), level))
                    self.assertEqual(read["sinceAdmission"] is None, not recent)
                    # The seconds are the reading's own, which a slow host may make more than 0 (#15).
                    expected = refused and refused.replace("{since}", str(int(read["sinceAdmission"] or 0)))
                    self.assertEqual(remote.spare({**read, "screen": None}, "this Mac"), expected)
                    # Admission agrees: it takes the slot beyond them exactly when the reading finds it spare.
                    if refused is None:
                        with resources.Admission("tests", timeout=0) as admitted:
                            self.assertEqual([slot for slot, _fd in admitted.descriptors], [2])
                    else:
                        with self.assertRaisesRegex(TimeoutError, "kept for a whole-host lane queued ahead of it: "
                                                    f"waiting gate; and .*{re.escape(reason)}"):
                            with resources.Admission("tests", timeout=0): self.fail("admitted")
                    # A guaranteed slot is spare whatever the settle time or memory pressure, here as in admission.
                    self.queue()
                    self.assertIsNone(remote.spare({**offload_host.capacity(), "screen": None}, "this Mac"))
                    with resources.Admission("tests", timeout=0) as admitted:
                        self.assertEqual([slot for slot, _fd in admitted.descriptors], [0])

    def test_a_whole_host_request_keeps_its_place_across_waits_until_it_leaves(self):
        """resources.Place: work that alternates between this Mac's waits and offers to another Mac keeps one place
        in arrival order, and leaves it once it no longer waits here; a stop takes it out at once."""
        queue = resources.slot_directory() / resources.QUEUE
        self.hold(0)
        place = resources.Place()
        places = lambda: [waiter["label"] for waiter in json.loads(queue.read_text())["waiters"]] if queue.exists() else []
        with contextlib.redirect_stderr(StringIO()):
            with self.assertRaisesRegex(TimeoutError, "a whole-host lane needs every guaranteed slot"):
                with resources.Admission("gate", whole_host=True, timeout=0, place=place): self.fail("admitted")
            (kept,) = json.loads(queue.read_text())["waiters"]
            self.assertEqual((kept["identity"], kept["label"], kept["wholeHost"]), (place.identity, "gate", True))
            # Another whole-host request comes meanwhile, and stays behind it at its next wait.
            self.queue(kept, self.waiter("later gate", wholeHost=True, seen=time.time()))
            with self.assertRaisesRegex(TimeoutError, "a whole-host lane needs every guaranteed slot"):
                with resources.Admission("gate", whole_host=True, timeout=0, place=place): self.fail("admitted")
            self.assertEqual(places(), ["gate", "later gate"])
            # Without a place, a wait that times out leaves the queue, as before.
            with self.assertRaisesRegex(TimeoutError, "behind a whole-host lane queued ahead of it: gate"):
                with resources.Admission("third gate", whole_host=True, timeout=0): self.fail("admitted")
            self.assertEqual(places(), ["gate", "later gate"])
            place.leave()
            self.assertEqual(places(), ["later gate"])
            place.leave()
            self.assertEqual(places(), ["later gate"])
            queue.unlink()
            # A stop takes a place out at once.
            self.addCleanup(processes._stops.clear)
            processes.request_stop(signal.SIGTERM)
            with self.assertRaisesRegex(processes.Interrupted, "while waiting for admission"):
                with resources.Admission("gate", whole_host=True, timeout=60, place=place): self.fail("admitted")
            processes._stops.clear()
            self.assertEqual(places(), [])
            # qk run's admission here keeps a place, and leaves it once it no longer waits here, admitted or not.
            left = []
            class Recorded(resources.Place):
                def leave(self):
                    left.append(places())
                    super().leave()
            # One that waits out its time is BUSY, not raised: nothing ran (#268).
            with patch.object(resources, "Place", Recorded), contextlib.ExitStack() as stack, \
                    patch("sys.stderr", new_callable=StringIO) as said:
                self.assertEqual(cli.admitted_here(stack, "gate", "gate", 0, whole_host=True), resources.BUSY)
            self.assertIn("queuekeeper: gate not run on this Mac", said.getvalue())
            self.assertIn("every guaranteed slot", said.getvalue())
            self.assertEqual((left, places()), ([["gate"]], []))

    def test_a_live_ordinary_waiter_is_not_overtaken_by_a_later_exclusive_waiter(self):
        directory = resources.slot_directory()
        directory.mkdir(parents=True)
        with patch.dict(os.environ, {"HEAVY_SLOTS": "1", "HEAVY_SLOTS_MAX": "1"}):
            with os.fdopen(resources.take_slot(directory, 1)[1]) as running:
                ordinary, line = self.request("ordinary-first")
                self.assertIn("waiting", line)
                exclusive, line = self.request_exclusive("exclusive-later")
                self.assertIn("ordinary work queued ahead of it: ordinary-first", line)
                ordinary.stdin.close()
                exclusive.stdin.close()
                running.close()
            self.assertEqual(self.admitted(2), ["ordinary-first", "exclusive-later"])
            self.assertEqual(ordinary.wait(timeout=stretched(30)), 0)
            self.assertEqual(exclusive.wait(timeout=stretched(30)), 0)
        self.assertFalse((directory / resources.QUEUE).exists())

    def test_child_requests_keep_inherited_admission_without_reusing_transport_age(self):
        old = time.time() - 60
        with patch.dict(os.environ, {resources.REQUEST_STARTED: str(old)}), \
                resources.request_order(old), contextlib.redirect_stderr(StringIO()):
            with resources.Admission("dispatched", timeout=0) as admitted:
                self.assertEqual(admitted.ordered, old)
                with resources.child_requests():
                    self.assertNotIn(resources.REQUEST_STARTED, os.environ)
                    self.assertGreater(resources.request_started(), old + 30)
                    self.assertTrue(resources.inherited())
                self.assertEqual(resources.request_started(), old)
                self.assertEqual(os.environ[resources.REQUEST_STARTED], str(old))

    def test_an_older_ordinary_request_runs_before_later_exclusive_arrivals(self):
        # All slots are held when ordinary work arrives. Later exclusive work must wait for it,
        # as ordinary work already waits for an older exclusive request in the opposite control.
        directory = resources.slot_directory()
        directory.mkdir(parents=True)
        with patch.dict(os.environ, {"HEAVY_SLOTS_MAX": "2"}), contextlib.redirect_stderr(StringIO()):
            with os.fdopen(resources.take_slot(directory, 1)[1]) as first, \
                    os.fdopen(resources.take_slot(directory, 2, start=1)[1]):
                ordinary, later = resources.Place(), [resources.Place() for _ in range(3)]
                with self.assertRaises(TimeoutError):
                    with resources.Admission("ordinary", timeout=0, place=ordinary): self.fail("admitted")
                for index, place in enumerate(later):
                    with self.assertRaisesRegex(TimeoutError, "ordinary work queued ahead of it: ordinary"):
                        with resources.Admission(f"later bench {index}", exclusive=True, timeout=0, place=place): self.fail("admitted")
                first.close()
                with resources.Admission("ordinary", timeout=0, place=ordinary) as admitted:
                    self.assertEqual([slot for slot, _ in admitted.descriptors], [0])
                ordinary.leave()
                for place in later: place.leave()
            # Cancellation/total-timeout cleanup leaves no ghost request to hold the next one.
            self.assertFalse((directory / resources.QUEUE).exists())
            with resources.Admission("next", exclusive=True, timeout=0): pass

    def test_repeated_remote_declines_keep_the_original_age_when_the_request_rejoins(self):
        # A remote decline's process ends and its queue record disappears. Recreated admissions
        # must sort by the original elapsed request age, rather than the latest retry's process.
        from queuekeeper import offload_host
        directory = resources.slot_directory()
        directory.mkdir(parents=True)
        older, later = time.time() - 60, time.time() - 30
        with os.fdopen(resources.take_slot(directory, 1)[1]) as held, \
                patch.dict(os.environ, {"HEAVY_SLOTS": "1", "HEAVY_SLOTS_MAX": "1"}), \
                contextlib.redirect_stderr(StringIO()):
            with resources.request_order(later): place = resources.Place()
            with self.assertRaises(TimeoutError):
                with resources.Admission("later bench", exclusive=True, place=place, timeout=0): pass
            for _ in range(3):
                with resources.request_order(older), self.assertRaises(TimeoutError):
                    with resources.Admission("older remote", timeout=0): self.fail("admitted")
                # Without a retained place, a declined dispatch leaves its record and checkout.
                queued = json.loads((directory / resources.QUEUE).read_text())["waiters"]
                self.assertEqual([waiter["label"] for waiter in queued], ["later bench"])
                # A capacity preflight must not let a later exclusive waiter hide capacity from this request.
                self.assertIsNone(offload_host.queued_ahead(directory, 1, 1, [], before=older))
                self.assertEqual(offload_host.queued_ahead(directory, 1, 1, [])[0], "exclusive")
            held.close()
            with resources.request_order(older), resources.Admission("older remote", timeout=0): pass
            with resources.Admission("later bench", exclusive=True, place=place, timeout=0): pass
            place.leave()
            self.assertFalse((directory / resources.QUEUE).exists())

    def test_ordinary_places_are_removed_on_stop_and_do_not_block_other_ordinary_work(self):
        directory = resources.slot_directory()
        directory.mkdir(parents=True)
        place = resources.Place()
        with os.fdopen(resources.take_slot(directory, 1)[1]) as held, \
                patch.dict(os.environ, {"HEAVY_SLOTS": "1", "HEAVY_SLOTS_MAX": "1"}), \
                contextlib.redirect_stderr(StringIO()):
            with self.assertRaises(TimeoutError):
                with resources.Admission("older", timeout=0, place=place): pass
            held.close()
            with resources.Admission("another ordinary", timeout=0): pass
            self.addCleanup(processes._stops.clear)
            processes.request_stop(signal.SIGTERM)
            with os.fdopen(resources.take_slot(directory, 1)[1]), self.assertRaises(processes.Interrupted):
                with resources.Admission("older", timeout=0, place=place): pass
            processes._stops.clear()
            self.assertFalse((directory / resources.QUEUE).exists())

    def test_away_places_keep_their_age_without_holding_back_a_free_host(self):
        from queuekeeper import offload_host
        for ordinary in [False, True]:
            with self.subTest(ordinary=ordinary), contextlib.redirect_stderr(StringIO()):
                waiter = self.waiter("away", queuedAt=time.time() - 600,
                                     seen=time.time() - resources.PLACE_SECONDS - 1, ordinary=ordinary)
                queue = self.queue(waiter)
                with resources.Admission("later bench", exclusive=True, timeout=0): pass
                self.assertEqual(json.loads(queue.read_text())["waiters"], [waiter])
                self.assertIsNone(offload_host.queued_ahead(resources.slot_directory(), 2, 3, []))
                for malformed in [None, True, "not a timestamp"]:
                    self.queue({**waiter, "seen": malformed})
                    with resources.Admission("later bench", exclusive=True, timeout=0): pass
                    self.assertIsNone(offload_host.queued_ahead(resources.slot_directory(), 2, 3, []))
                queue.unlink()

    def test_nested_work_does_not_wait_behind_an_exclusive_request_that_waits_for_it(self):
        with contextlib.redirect_stderr(StringIO()), resources.Admission("running", timeout=0):
            exclusive, line = self.request_exclusive("exclusive")
            self.assertIn(f"exclusive work needs every slot up to {mine('HEAVY_SLOTS_MAX=3')} free, and 1 is busy", line)
            # Waiting would deadlock until the timeout: the exclusive request waits for this job.
            with resources.Admission("nested", timeout=0) as nested:
                self.assertTrue(nested.reused)
            exclusive.stdin.close()
        self.assertEqual(self.admitted(1), ["exclusive"])
        self.assertEqual(exclusive.wait(timeout=stretched(30)), 0)

    def test_model_runs_exclude_each_other_while_heavy_work_runs_beside_them(self):
        # One model workload per machine is a limit of the hardware: two processes driving one Mac's
        # on-device model both stalled. Builds and tests need not wait for it (#28).
        model = resources.slot_directory() / "model"
        first, line = self.request("first", model=True)
        self.assertIn("heavy: slot 0 and the model after", line)
        with contextlib.redirect_stderr(StringIO()) as error, self.assertRaisesRegex(TimeoutError, "the on-device model, one run at a time, is held by first"):
            with resources.Admission("second", model=True, timeout=0): self.fail("admitted beside a model run")
        self.assertRegex(error.getvalue(), r"heavy: waiting \(\d+s\): "
                         + re.escape("the on-device model, one run at a time, is held by first\n"))
        with contextlib.redirect_stderr(StringIO()), resources.Admission("heavy", timeout=0) as heavy:
            self.assertEqual([slot for slot, _fd in heavy.descriptors], [1])
        first.stdin.close()
        self.assertEqual(first.wait(timeout=stretched(30)), 0)
        with contextlib.redirect_stderr(StringIO()), resources.Admission("second", model=True, timeout=0) as second:
            self.assertEqual([slot for slot, _fd in second.descriptors], [0])
            self.assertIn(second.identity, model.read_text())
        # Both or neither: a model request that finds no slot leaves the model free.
        self.hold(0, 1, 2)
        with contextlib.redirect_stderr(StringIO()), self.assertRaisesRegex(TimeoutError, "slots are busy"):
            with resources.Admission("unslotted", model=True, timeout=0): self.fail("admitted without a slot")
        with model.open("a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_exclusive_work_waits_for_a_model_run_and_holds_the_model_itself(self):
        generate, _line = self.request("generate", model=True)
        self.assertEqual(self.admitted(1), ["generate"])
        exclusive, line = self.request_exclusive("exclusive")
        self.assertIn("the on-device model, one run at a time, is held by generate", line)
        generate.stdin.close()
        self.assertEqual(self.admitted(2), ["generate", "exclusive"])
        # Exclusive admission holds the host, the model with it.
        with contextlib.redirect_stderr(StringIO()), self.assertRaisesRegex(TimeoutError, "it waits behind exclusive work holding the host: exclusive"):
            with resources.Admission("beside", model=True, timeout=0): self.fail("admitted beside exclusive work")
        exclusive.stdin.close()
        self.assertEqual((generate.wait(timeout=stretched(30)), exclusive.wait(timeout=stretched(30))), (0, 0))

    def wait_behind(self, holder, waiter_timeout, **options):
        """Wait waiter_timeout seconds behind holder, held in this process as exclusive or whole-host work with options:
        the holder's guard entry, the TimeoutError that ended the wait, what the wait printed and how long it took."""
        os.environ["HEAVY_SETTLE_SECONDS"] = "0"
        printed = StringIO()
        with contextlib.redirect_stderr(StringIO()), resources.Admission(holder, timeout=0, **options) as held:
            entry = json.loads(held.guard.read_text())["owners"][held.identity]
            with patch.dict(os.environ), contextlib.redirect_stderr(printed):
                os.environ.pop(resources.TOKEN)
                began = time.monotonic()
                with self.assertRaises(TimeoutError) as ended:
                    with resources.Admission("FlexolioLib check", timeout=waiter_timeout): self.fail("admitted beside it")
                took = time.monotonic() - began
        return entry, ended.exception, printed.getvalue(), took

    def test_a_holder_whose_stated_limit_ends_after_the_deadline_is_said_at_once_and_the_wait_goes_on(self):
        """vocaro/queuekeeper#255: an exclusive or whole-host holder admitted with a time limit of its own that ends
        after a waiter's deadline (2026-11-27: 8,100 s against 5,400 s) is named at once, with when its limit ends, the
        waiter's deadline and how to outlast it, once; the waiter still waits out its whole deadline."""
        for kind in ({"exclusive": True}, {"whole_host": True}):
            with self.subTest(**kind):
                entry, error, printed, took = self.wait_behind("Pagepour bench", 0.3, limit=8100, **kind)
                ends = datetime.fromisoformat(entry["limitEndsAt"])
                self.assertAlmostEqual((ends - datetime.now(timezone.utc)).total_seconds(), 8100, delta=30)
                late = [line for line in printed.splitlines() if "time limit" in line]
                self.assertEqual(len(late), 1, printed)
                self.assertIn(f"heavy: Pagepour bench holds the host with a time limit that ends at {entry['limitEndsAt']}, "
                              "after this wait's deadline, ", late[0])
                self.assertIn("It waits to its deadline all the same", late[0])
                outlast = int(re.search(r"--timeout (\d+)", late[0]).group(1))
                self.assertAlmostEqual(outlast, 8100 + 60, delta=30)
                self.assertIn("or run the work on another Mac", late[0])
                # Said before the first five-minute report would have been due, and the wait was not shortened.
                self.assertLess(printed.index("time limit"), printed.index("heavy: waiting"))
                self.assertGreaterEqual(took, 0.3)
                self.assertIn(f"Pagepour bench's time limit ends at {entry['limitEndsAt']}, after this wait's deadline; "
                              f"a wait of {outlast}s outlasts it", str(error))
                self.assertIn("nothing ran", str(error))

    def test_a_holder_whose_limit_ends_in_time_or_that_states_none_waits_as_before(self):
        """vocaro/queuekeeper#255: a holder whose stated limit ends before the waiter's deadline, or that states no limit
        of its own, adds nothing to the wait's words."""
        for name, options in [("limit within the deadline", {"limit": 0.5}), ("no limit", {})]:
            with self.subTest(name):
                entry, error, printed, took = self.wait_behind("Pagepour bench", 2, exclusive=True, **options)
                self.assertEqual("limitEndsAt" in entry, "limit" in options)
                self.assertNotIn("time limit", printed)
                self.assertNotIn("time limit", str(error))
                self.assertIn("it waits behind exclusive work holding the host: Pagepour bench", str(error))
                self.assertGreaterEqual(took, 2)

    def test_a_model_run_inside_heavy_admission_is_refused_and_inside_model_or_exclusive_admission_reuses_it(self):
        guard = Path(os.environ["QUEUEKEEPER_GATE_LOCK_FILE"] + ".warm-push-guard")
        advice = 'Request it in the outer admission with `qk admit --model` or set "model": true on the outer lane'
        with contextlib.redirect_stderr(StringIO()), resources.Admission("heavy", timeout=0):
            with self.assertRaisesRegex(RuntimeError, re.escape(advice)):
                with resources.Admission("generate", model=True, timeout=0): self.fail("admitted without the model")
            # Nor can a heavy owner claim the model in its token.
            token = json.loads(os.environ[resources.TOKEN])
            token["model"] = True
            os.environ[resources.TOKEN] = json.dumps(token)
            with self.assertRaisesRegex(RuntimeError, "capability"):
                resources.inherited(model=True)
        for options in [{"model": True}, {"exclusive": True}]:
            with self.subTest(**options), contextlib.redirect_stderr(StringIO()), \
                    resources.Admission("outer", timeout=0, **options) as outer:
                token = json.loads(os.environ[resources.TOKEN])
                self.assertTrue(token["model"])
                self.assertTrue(json.loads(guard.read_text())["owners"][outer.identity]["model"])
                self.assertIn(str(resources.slot_directory() / "model"), [item["path"] for item in token["locks"]])
                for nested in [{"model": True}, {}]:
                    with resources.Admission("nested", timeout=0, **nested) as inner:
                        self.assertTrue(inner.reused)
                if options.get("model"):
                    with self.assertRaisesRegex(RuntimeError, "exclusive"):
                        with resources.Admission("exclusive", exclusive=True, timeout=0): pass
        self.assertFalse(guard.exists())

    def test_a_model_run_requested_behind_an_exclusive_request_waits_until_the_exclusive_job_has_run(self):
        directory, log = resources.slot_directory(), Path(self.temp.name) / "admitted.log"
        directory.mkdir(parents=True)
        running = os.fdopen(resources.take_slot(directory, 1)[1])
        self.addCleanup(running.close)
        exclusive, line = self.request_exclusive("exclusive")
        self.assertIn(f"exclusive work needs every slot up to {mine('HEAVY_SLOTS_MAX=3')} free, and 1 is busy", line)
        exclusive.stdin.close()
        # A guaranteed slot and the model are free, and the model run still waits.
        with contextlib.redirect_stderr(StringIO()) as error, self.assertRaisesRegex(TimeoutError, "behind exclusive work queued ahead of it: exclusive"):
            with resources.Admission("generate", model=True, timeout=0): self.fail("admitted ahead of exclusive work")
        self.assertRegex(error.getvalue(), r"heavy: waiting \(\d+s\): "
                         + re.escape("it waits behind exclusive work queued ahead of it: exclusive\n"))
        running.close()
        with contextlib.redirect_stderr(StringIO()), resources.Admission("generate", model=True, timeout=stretched(30)) as generate:
            self.assertEqual(log.read_text() if log.exists() else "", "exclusive\n")
            self.assertEqual([slot for slot, _fd in generate.descriptors], [0])
        self.assertEqual(exclusive.wait(timeout=stretched(30)), 0)

    def test_a_model_lane_and_a_group_that_runs_it_hold_the_model(self):
        root = Path(self.temp.name)
        probe = [sys.executable, "-c", f"import json, os; print('model', json.loads(os.environ[{resources.TOKEN!r}]).get('model'))"]
        (root / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Model", "lanes": {
            "generate": {"command": probe, "resource": "heavy", "model": True},
            "build": {"command": probe, "resource": "heavy"},
            "both": {"parallel": ["generate", "build"], "resource": "heavy", "model": True}}}))
        with patch.object(cli, "refresh_policy", return_value="policy"), patch.object(cli, "source_identity", return_value={}), \
                contextlib.redirect_stdout(StringIO()), contextlib.redirect_stderr(StringIO()):
            for lane in ["generate", "build", "both"]:
                self.assertEqual(cli.main(["--project", str(root), "run", lane]), 0, lane)
        logs = root / ".build/queuekeeper"
        for log, held in [("generate/run.log", True), ("build/run.log", False), ("both/lanes/generate.log", True),
                          ("both/lanes/build.log", True)]:
            self.assertIn(f"model {held}", (logs / log).read_text(), log)

    def test_a_lane_names_the_main_checkout_and_leaves_its_process_as_it_was(self):
        """vocaro/queuekeeper#186: qk run in-process once left QUEUEKEEPER_PROJECT_CHECKOUT set for every later test."""
        root = Path(self.temp.name)
        probe = [sys.executable, "-c", f"import os; print('at', os.environ[{cli.PROJECT_CHECKOUT_VARIABLE!r}])"]
        (root / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Where", "lanes": {
            "where": {"command": probe, "resource": "heavy"}}}))
        self.assertNotIn(cli.PROJECT_CHECKOUT_VARIABLE, os.environ)
        with patch.object(cli, "refresh_policy", return_value="policy"), patch.object(cli, "source_identity", return_value={}), \
                contextlib.redirect_stdout(StringIO()), contextlib.redirect_stderr(StringIO()):
            self.assertEqual(cli.main(["--project", str(root), "run", "where"]), 0)
        self.assertIn(f"at {root.resolve()}", (root / ".build/queuekeeper/where/run.log").read_text())
        self.assertNotIn(cli.PROJECT_CHECKOUT_VARIABLE, os.environ)

    def test_a_display_lane_runs_under_a_wake_and_then_the_display_assertion_and_its_receipt_shows_them(self):
        # A caffeinate on PATH that records each call and, for -d, runs the command in its own process, as
        # the real one does, so that no test wakes or holds this Mac's display.
        root = Path(self.temp.name)
        tools, record = root / "tools", root / "record"
        tools.mkdir()
        (tools / "caffeinate").write_text('#!/bin/sh\necho "caffeinate $*" >> "$RECORD"\n'
                                        'if [ "$1" = -d ] && [ "$2" = -- ]; then shift 2; exec "$@"; fi\n')
        host_isolation.executable(tools / "caffeinate")
        def lane(name, status=0, **options):
            return {"command": ["sh", "-c", f'echo {name} >> "$RECORD"; exit {status}'], "resource": "heavy", **options}
        lanes = {"mac": lane("mac", display=True), "ios": lane("ios"), "failing": lane("failing", 4, display=True),
                 "suites": {"parallel": ["mac", "ios"], "resource": "heavy", "jobs": 1}}
        (root / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Display", "lanes": lanes}))
        held = lambda name: f"caffeinate -d -- sh -c {lanes[name]['command'][2]}"
        receipts = root / ".build/queuekeeper"
        with patch.dict(os.environ, {"PATH": f"{tools}{os.pathsep}{os.environ['PATH']}", "RECORD": str(record)}), \
                patch.object(cli, "refresh_policy", return_value="policy"), patch.object(cli, "source_identity", return_value={}), \
                contextlib.redirect_stdout(StringIO()), contextlib.redirect_stderr(StringIO()):
            for name, status, calls in [
                    ("mac", 0, ["caffeinate -u -t 1", held("mac"), "mac"]),
                    # Without display, nothing calls caffeinate.
                    ("ios", 0, ["ios"]),
                    # The command's own status passes through both assertions.
                    ("failing", 4, ["caffeinate -u -t 1", held("failing"), "failing"]),
                    # A group wakes the display for, and holds it during, only its lanes that say display.
                    ("suites", 0, ["caffeinate -u -t 1", held("mac"), "mac", "ios"])]:
                with self.subTest(name):
                    record.unlink(missing_ok=True)
                    self.assertEqual(cli.main(["--project", str(root), "run", name]), status)
                    self.assertEqual(record.read_text().splitlines(), calls)
            for path, name in [("mac/source.json", "mac"), ("ios/source.json", "ios"), ("failing/source.json", "failing"),
                               ("suites/lanes/mac.json", "mac"), ("suites/lanes/ios.json", "ios")]:
                with self.subTest(path):
                    command = lanes[name]["command"]
                    expected = [*display.AWAKE, *command] if lanes[name].get("display") else command
                    self.assertEqual(json.loads((receipts / path).read_text())["command"], expected)
            # Arguments go to the command, not to either assertion.
            record.unlink()
            self.assertEqual(cli.main(["--project", str(root), "run", "mac", "--", "extra"]), 0)
            self.assertEqual(json.loads((receipts / "mac/source.json").read_text())["command"],
                             [*display.AWAKE, *lanes["mac"]["command"], "extra"])
            self.assertEqual(record.read_text().splitlines(), ["caffeinate -u -t 1", f"{held('mac')} extra", "mac"])

    def test_an_unreadable_unknown_or_uncertain_guard_is_never_reclaimed(self):
        guard = Path(os.environ["QUEUEKEEPER_GATE_LOCK_FILE"] + ".warm-push-guard")
        # Whatever wrote a guard admission cannot read may still run what it records, so admission waits and
        # leaves its bytes as they are: a torn write, JSON that is no guard, another version's, another program's
        # such as an older push keeper's, and owners without admission's marker.
        for text in ['{"vocaroEngineering": 1, "owners": {', "[]", '{"vocaroEngineering": 2, "owners": {}}',
                     '{"keeper": 123456, "checkoutLock": "/tmp/lock"}', '{"owners": {}}']:
            with self.subTest(text):
                guard.write_text(text)
                with contextlib.redirect_stderr(StringIO()), self.assertRaisesRegex(TimeoutError, re.escape(
                        f"the guard {guard} is unreadable or not in admission's format and requires inspection")):
                    with resources.Admission("blocked", timeout=0): self.fail("admitted")
                self.assertEqual(guard.read_text(), text)
        guard.unlink()
        with self.assertRaisesRegex(RuntimeError, "uncertain"):
            with resources.Admission("owned", timeout=0): raise RuntimeError("uncertain")
        self.assertTrue(guard.exists())
        # The uncertain entry is counted as the slot it held (#89): exclusive work waits behind it, and work within
        # the free slots goes on beside it.
        with contextlib.redirect_stderr(StringIO()), self.assertRaisesRegex(TimeoutError, "is counted as slot 0"):
            with resources.Admission("blocked", exclusive=True, timeout=0): pass
        with contextlib.redirect_stderr(StringIO()), resources.Admission("beside", timeout=0) as beside:
            self.assertEqual([slot for slot, _fd in beside.descriptors], [1])
        self.assertTrue(json.loads(guard.read_text())["owners"])

    def test_a_guard_whose_owners_are_not_entries_in_admissions_format_is_refused_and_left_byte_for_byte(self):
        # Such a guard made admission raise AttributeError or KeyError, or admit work beside an entry it could not
        # read, where it refuses a guard it cannot parse (#72). Each attempt, nested work and a job's own stop or
        # end now treat it alike, and leave its bytes as they are.
        guard = Path(os.environ["QUEUEKEEPER_GATE_LOCK_FILE"] + ".warm-push-guard")
        refused = f"the guard {guard} is unreadable or not in admission's format"
        waiting = f"({refused} and requires inspection; `python3 scripts/qk admissions recover` lists it); no lock was reclaimed"
        with contextlib.redirect_stderr(StringIO()), resources.Admission("held", timeout=0) as held:
            written = guard.read_text()
            entry = json.loads(written)["owners"][held.identity]
            # The entry admission writes, which each case changes.
            self.assertEqual(set(entry), {"pid", "start", "label", "exclusive", "model", "admitted", "origin", "directory"})
            # The positive controls, first, so that no case before them can hold a slot: the guard as admission
            # wrote it, beside an older toolkit's entry, which has no model or admission time, or with a field a
            # newer toolkit may add, admits work beside it and nested work.
            older = {field: entry[field] for field in ["pid", "start", "label", "exclusive"]}
            for name, text in [("as admission wrote it", written),
                               ("beside an older toolkit's entry",
                                json.dumps({"vocaroEngineering": 1, "owners": {held.identity: entry, "older": older}})),
                               ("with a field a newer toolkit may add",
                                json.dumps({"vocaroEngineering": 1, "owners": {held.identity: {**entry, "newer": [1]}}}))]:
                with self.subTest(name):
                    guard.write_text(text)
                    with patch.dict(os.environ):
                        os.environ.pop(resources.TOKEN)
                        with resources.Admission("beside", timeout=0) as beside:
                            self.assertEqual(set(json.loads(guard.read_text())["owners"]),
                                             {*json.loads(text)["owners"], beside.identity})
                    self.assertEqual(json.loads(guard.read_text()), json.loads(text))
                    with resources.Admission("nested", timeout=0) as nested: self.assertTrue(nested.reused)
            for name, text in malformed_guards(held.identity, entry).items():
                with self.subTest(name):
                    guard.write_text(text)
                    with patch.dict(os.environ):
                        os.environ.pop(resources.TOKEN)
                        resources.log_path().unlink(missing_ok=True)
                        beside = resources.Admission("beside", timeout=0)
                        with self.assertRaisesRegex(TimeoutError, re.escape(waiting)):
                            with beside: self.fail("admitted")
                    (line,) = [json.loads(logged) for logged in resources.log_path().read_text().splitlines()]
                    self.assertEqual(line["identity"], beside.identity)
                    self.assertEqual((line["event"], line["holders"], line["wait"]["refusals"]), ("withdrawn", None, {"guard": 1}))
                    with self.assertRaisesRegex(RuntimeError, re.escape(f"Invalid inherited resource admission: {refused}")):
                        with resources.Admission("nested", timeout=0): self.fail("reused")
                    # A stop's first step, marking the job's own entry uncertain, changes nothing either.
                    held.interrupted()
                    self.assertEqual(guard.read_text(), text)
            guard.write_text(written)
        self.assertFalse(guard.exists())
        # A job leaving such a guard leaves it as it is, its own entry in it, for inspection with the rest.
        for failure in [None, RuntimeError]:
            with self.subTest(failure=failure):
                with contextlib.redirect_stderr(StringIO()), contextlib.suppress(RuntimeError):
                    with resources.Admission("leaving", timeout=0) as leaving:
                        owners = json.loads(guard.read_text())["owners"]
                        self.assertEqual(list(owners), [leaving.identity])
                        text = json.dumps({"vocaroEngineering": 1, "owners": {**owners, "stray": {"pid": os.getpid()}}})
                        guard.write_text(text)
                        if failure: raise failure("children may remain")
                self.assertEqual(guard.read_text(), text)
                guard.unlink()

    def test_a_gate_lock_held_at_the_namespaces_path_neither_holds_admission_back_nor_is_touched(self):
        # Admission took StudyWright's former host gate lock at each attempt, so that a gate holding it refused
        # every session's heavy work. No checkout runs that gate any more, and admission ignores the lock (#25).
        lock = Path(os.environ["QUEUEKEEPER_GATE_LOCK_FILE"])
        if Path("/usr/bin/shlock").is_file():
            subprocess.run(["/usr/bin/shlock", "-f", str(lock), "-p", str(os.getppid())], check=True)
        else:
            lock.write_text(f"{os.getppid()}\n")
        held = lock.read_bytes()
        self.assertEqual(held.strip(), str(os.getppid()).encode())
        with contextlib.redirect_stderr(StringIO()) as error, resources.Admission("beside a gate", timeout=0) as admitted:
            self.assertEqual(len(admitted.descriptors), 1)
            self.assertEqual(lock.read_bytes(), held)
            # Nothing reads a holder of the retired lock: StudyWright stopped in 657ba3272.
            self.assertFalse(hasattr(admitted, "legacy"))
        self.assertNotIn("waiting", error.getvalue())
        self.assertEqual(lock.read_bytes(), held)
        (line,) = [json.loads(text) for text in resources.log_path().read_text().splitlines()
                   if json.loads(text)["event"] == "admitted"]
        self.assertNotIn("wait", line)

    def test_a_killed_owner_does_not_strand_the_release_of_a_job_admitted_beside_it(self):
        # A job admitted beside one killed by SIGKILL changes only its own entry when it leaves.
        # Changing nothing stranded the queue for 80 minutes (2026-10-22): every job that finished
        # while the killed job's entry stood became another dead owner.
        guard = Path(os.environ["QUEUEKEEPER_GATE_LOCK_FILE"] + ".warm-push-guard")
        holder = ("import sys\nfrom queuekeeper import resources\n"
                  "with resources.Admission('killed', timeout=0):\n    print(flush=True)\n    sys.stdin.read()\n")
        environment = {**os.environ, "PYTHONPATH": host_isolation.pythonpath()}
        for failure in [None, RuntimeError]:
            with self.subTest(failure=failure):
                guard.unlink(missing_ok=True)
                # Closing its stdin releases the holder if an assertion fails before the kill.
                with subprocess.Popen([sys.executable, "-c", holder], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL, text=True, env=environment) as killed:
                    self.assertEqual(killed.stdout.readline(), "\n")
                    recorded = json.loads(guard.read_text())["owners"]
                    with contextlib.redirect_stderr(StringIO()), contextlib.suppress(RuntimeError):
                        with resources.Admission("beside", timeout=0) as beside:
                            killed.kill()
                            killed.wait()
                            if failure: raise failure("children may remain")
                owners = json.loads(guard.read_text())["owners"]
                if failure:
                    self.assertTrue(owners.pop(beside.identity).get("uncertain"))
                self.assertEqual(owners, recorded)
                # The killed job's entry is freed at the next attempt, its owner ended (vocaro/queuekeeper#23); the
                # uncertain entry of the job beside it, whose owner, this process, still runs, is counted as the slot it
                # held (#89), so exclusive work waits behind it only then.
                if failure:
                    with self.assertRaisesRegex(TimeoutError, "is uncertain and requires inspection"), \
                            contextlib.redirect_stderr(StringIO()):
                        with resources.Admission("blocked", exclusive=True, timeout=0): pass
                    self.assertEqual(set(json.loads(guard.read_text())["owners"]), {beside.identity})
                else:
                    with contextlib.redirect_stderr(StringIO()) as said, \
                            resources.Admission("admitted", exclusive=True, timeout=0) as admitted:
                        self.assertEqual(set(json.loads(guard.read_text())["owners"]), {admitted.identity})
                    self.assertIn(f"heavy: reclaimed killed (PID {killed.pid})", said.getvalue())

    def test_a_stop_signal_ends_the_command_then_releases_admission(self):
        # An agent's task stop sent qk admit SIGTERM, which ended it without leaving admission
        # while its command ran on in its own session (2026-10-22).
        root = Path(self.temp.name)
        guard = Path(os.environ["QUEUEKEEPER_GATE_LOCK_FILE"] + ".warm-push-guard")
        marker = root / "command.pid"
        command = [sys.executable, "-c", f"import os, pathlib, time\npathlib.Path({str(marker)!r}).write_text(str(os.getpid()))\ntime.sleep(120)"]
        (root / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Stop",
            "lanes": {"stopped": {"command": command, "resource": "heavy"}}}))
        launch = ("import sys\nfrom unittest.mock import patch\nfrom queuekeeper import cli\n"
                  "with patch.object(cli, 'refresh_policy', return_value='policy'), patch.object(cli, 'source_identity', return_value={}):\n"
                  "    raise SystemExit(cli.main(sys.argv[1:]))\n")
        entries = {"resources": [sys.executable, "-m", "queuekeeper.resources", "--", *command],
                   "admit": [sys.executable, "-c", launch, "--project", str(root), "admit", "--", *command],
                   "run": [sys.executable, "-c", launch, "--project", str(root), "run", "stopped"]}
        cases = [(entry, signal.SIGTERM) for entry in entries] + [("resources", signal.SIGHUP), ("resources", signal.SIGINT)]
        environment = {**os.environ, "PYTHONPATH": host_isolation.pythonpath()}
        for entry, number in cases:
            with self.subTest(entry=entry, signal=number.name):
                guard.unlink(missing_ok=True)
                marker.unlink(missing_ok=True)
                # Each signal as a terminal's foreground job has it, whatever the runner ignores.
                admitted = subprocess.Popen(entries[entry], cwd=root, env=environment, stderr=subprocess.DEVNULL,
                                            preexec_fn=host_isolation.default_signals)
                pid = birth = None
                try:
                    deadline = time.monotonic() + stretched(30)
                    while not (marker.exists() and marker.read_text()):
                        self.assertIsNone(admitted.poll(), "the command was never started")
                        self.assertLess(time.monotonic(), deadline, "the command was never started")
                        time.sleep(0.01)
                    pid = int(marker.read_text())
                    birth = resources.process_start(pid)
                    os.kill(admitted.pid, number)
                    self.assertEqual(admitted.wait(timeout=stretched(30)), -number)
                    self.assertNotEqual(resources.process_start(pid), birth, "the command outlived its stop")
                    self.assertFalse(guard.exists())
                    if entry != "resources":
                        receipts = {"admit": "admit", "run": "stopped"}
                        receipt = json.loads((root / ".build/queuekeeper" / receipts[entry] / "source.json").read_text())
                        self.assertEqual(receipt["outcome"], "failed-or-interrupted")
                        self.assertIn(f"Stopped by {number.name} after its command", receipt["error"])
                finally:
                    if admitted.poll() is None:
                        admitted.kill()
                        admitted.wait()
                    if pid is not None and resources.process_start(pid) == birth:
                        os.kill(pid, signal.SIGKILL)

    def test_a_task_stop_beside_a_finishing_job_strands_neither_entry(self):
        # A task stop signaled one job's whole process tree while another job ran beside it (#15,
        # 2026-10-22). The stopped job's entry died with its admitting process, and the job beside
        # it, finishing normally while that entry stood, left its own: two dead entries refused
        # every session's heavy work until attended recovery.
        root = Path(self.temp.name)
        guard = Path(os.environ["QUEUEKEEPER_GATE_LOCK_FILE"] + ".warm-push-guard")
        marker = root / "command.pids"
        # The command ends on SIGTERM, as the lane's script did, and has a child in its own session.
        command = [sys.executable, "-c", "import os, pathlib, subprocess, sys, time\n"
                   "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'], start_new_session=True)\n"
                   f"pathlib.Path({str(marker)!r}).write_text(f'{{os.getpid()}} {{child.pid}}')\ntime.sleep(120)"]
        environment = {**os.environ, "PYTHONPATH": host_isolation.pythonpath()}
        stopped = subprocess.Popen([sys.executable, "-m", "queuekeeper.resources", "--", *command],
                                   cwd=root, env=environment, stderr=subprocess.DEVNULL,
                                   preexec_fn=host_isolation.default_signals)
        tree = {}
        try:
            deadline = time.monotonic() + stretched(30)
            while not (marker.exists() and len(marker.read_text().split()) == 2):
                self.assertIsNone(stopped.poll(), "the command was never started")
                self.assertLess(time.monotonic(), deadline, "the command was never started")
                time.sleep(0.01)
            with contextlib.redirect_stderr(StringIO()), resources.Admission("beside", timeout=0):
                # Claude Code's TaskStop sends SIGTERM to every process descended from the task.
                current, pending = processes.snapshot(), [stopped.pid]
                while pending:
                    pid = pending.pop()
                    tree[pid] = current[pid][2]
                    pending.extend(child for child, (parent, _group, _birth) in current.items() if parent == pid)
                self.assertLessEqual({int(pid) for pid in marker.read_text().split()}, tree.keys())
                for pid in tree:
                    with contextlib.suppress(ProcessLookupError): os.kill(pid, signal.SIGTERM)
                self.assertEqual(stopped.wait(timeout=stretched(30)), -signal.SIGTERM)
            survivors = [pid for pid, (_parent, _group, birth) in processes.snapshot().items() if tree.get(pid) == birth]
            self.assertEqual(survivors, [])
            self.assertFalse(guard.exists())
            with contextlib.redirect_stderr(StringIO()), resources.Admission("next", timeout=0): pass
        finally:
            if stopped.poll() is None:
                stopped.kill()
                stopped.wait()
            for pid, (_parent, _group, birth) in processes.snapshot().items():
                if tree.get(pid) == birth:
                    with contextlib.suppress(ProcessLookupError): os.kill(pid, signal.SIGKILL)

    def test_a_stop_that_cannot_confirm_every_process_ended_leaves_the_entry_uncertain(self):
        guard = Path(os.environ["QUEUEKEEPER_GATE_LOCK_FILE"] + ".warm-push-guard")
        real, roots, marked = processes.snapshot, [], []
        # Above macOS's PID limit, so no signal reaches a real process: it never ends.
        phantom = 99999999
        def observed():
            current = real()
            if not roots:
                roots.extend(pid for pid, (parent, group, _birth) in current.items() if parent == os.getpid() and group == pid)
                if roots: processes.request_stop(signal.SIGTERM)
            elif guard.exists():
                marked.append(json.loads(guard.read_text())["owners"][admission.identity].get("uncertain"))
            if roots: current[phantom] = (roots[0], phantom, "phantom")
            return current
        self.addCleanup(processes._stops.clear)
        with patch.object(processes, "snapshot", observed), patch.object(processes, "KILL_WAIT", 0.2), contextlib.redirect_stderr(StringIO()):
            with self.assertRaisesRegex(RuntimeError, "SIGTERM, but owned descendants have no confirmed terminal result"):
                with resources.Admission("stopped", timeout=0) as admission:
                    processes.supervise([sys.executable, "-c", "import time; time.sleep(30)"], cwd=self.temp.name,
                                        on_stop=admission.interrupted, kill_after=0.1)
        # Marked while the stop drained, in case SIGKILL had ended this process first.
        self.assertTrue(marked and all(marked))
        self.assertTrue(json.loads(guard.read_text())["owners"][admission.identity]["uncertain"])

    def test_a_timed_out_run_releases_admission_once_its_command_ended(self):
        guard = Path(os.environ["QUEUEKEEPER_GATE_LOCK_FILE"] + ".warm-push-guard")
        with contextlib.redirect_stderr(StringIO()), self.assertRaises(processes.TimedOut):
            with resources.Admission("timed out", timeout=0):
                processes.supervise([sys.executable, "-c", "import time; time.sleep(30)"], cwd=self.temp.name, timeout=0.2)
        self.assertFalse(guard.exists())
        with contextlib.redirect_stderr(StringIO()), resources.Admission("next", timeout=0): pass

    def test_a_timeout_that_cannot_confirm_every_process_ended_leaves_the_entry_uncertain(self):
        guard = Path(os.environ["QUEUEKEEPER_GATE_LOCK_FILE"] + ".warm-push-guard")
        real, roots = processes.snapshot, []
        # Above macOS's PID limit, so no signal reaches a real process: it never ends.
        phantom = 99999999
        def observed():
            current = real()
            if not roots:
                roots.extend(pid for pid, (parent, group, _birth) in current.items() if parent == os.getpid() and group == pid)
            if roots: current[phantom] = (roots[0], phantom, "phantom")
            return current
        with patch.object(processes, "snapshot", observed), patch.object(processes, "KILL_WAIT", 0.2), contextlib.redirect_stderr(StringIO()):
            with self.assertRaisesRegex(RuntimeError, "Timed out after 0.2s, but owned descendants have no confirmed terminal result"):
                with resources.Admission("timed out", timeout=0) as admission:
                    processes.supervise([sys.executable, "-c", "import time; time.sleep(30)"], cwd=self.temp.name, timeout=0.2,
                                        kill_after=0.1)
        self.assertTrue(json.loads(guard.read_text())["owners"][admission.identity]["uncertain"])

    def test_a_stop_before_the_command_starts_leaves_no_entry(self):
        guard = Path(os.environ["QUEUEKEEPER_GATE_LOCK_FILE"] + ".warm-push-guard")
        self.addCleanup(processes._stops.clear)
        processes.request_stop(signal.SIGTERM)
        with self.subTest("admitted"), contextlib.redirect_stderr(StringIO()):
            with self.assertRaisesRegex(processes.Interrupted, "before its command started"):
                with resources.Admission("admitted", timeout=0) as admission:
                    processes.supervise([sys.executable, "-c", "pass"], cwd=self.temp.name, on_stop=admission.interrupted)
            self.assertFalse(guard.exists())
        with self.subTest("queued"), contextlib.redirect_stderr(StringIO()):
            self.hold(0, 1, 2)
            # Without the stop this waits the whole minute and then times out. Fixed: the stop ends it at once, so
            # the minute only bounds a failure.
            with self.assertRaisesRegex(processes.Interrupted, "while waiting for admission"):
                with resources.Admission("queued", timeout=60): self.fail("admitted")
            self.assertFalse(guard.exists())

    def test_zero_test_failure_releases_a_confirmed_finished_lane(self):
        root = Path(self.temp.name)
        (root / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Zero",
            "lanes": {"empty": {"command": ["unused"], "resource": "heavy", "minimumTests": 1}}}))
        def finished(*args, **kwargs):
            kwargs["log_path"].write_text("Executed 0 tests, with 0 failures")
            return 0
        with patch.object(cli, "refresh_policy", return_value="policy"), patch.object(cli, "source_identity", return_value={}), patch.object(cli.processes, "run", side_effect=finished), contextlib.redirect_stderr(StringIO()):
            self.assertEqual(cli.main(["--project", str(root), "run", "empty"]), 2)
        self.assertFalse(Path(os.environ["QUEUEKEEPER_GATE_LOCK_FILE"] + ".warm-push-guard").exists())

    def test_a_lane_whose_timeout_leaves_a_process_unconfirmed_is_unmeasured_and_keeps_its_reservation(self):
        # On 2026-11-15 such a gate, 2235 tests passed and none failed, exited 2 and was sent back as failed.
        root = Path(self.temp.name)
        (root / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Slow",
            "lanes": {"slow": {"command": ["unused"], "resource": "heavy", "timeout": 1}}}))
        guard = Path(os.environ["QUEUEKEEPER_GATE_LOCK_FILE"] + ".warm-push-guard")
        self.addCleanup(guard.unlink, missing_ok=True)
        for timed_out, status in ((True, lane_results.UNMEASURED), (False, 2)):
            with self.subTest(timed_out=timed_out):
                guard.unlink(missing_ok=True)
                left = processes.Unconfirmed("Timed out after 1s, but 1 process(es) remain; reservation retained", timed_out)
                with patch.object(cli, "refresh_policy", return_value="policy"), \
                        patch.object(cli, "source_identity", return_value={}), \
                        patch.object(cli.processes, "run", side_effect=left), contextlib.redirect_stderr(StringIO()):
                    self.assertEqual(cli.main(["--project", str(root), "run", "slow"]), status)
                owners = json.loads(guard.read_text())["owners"]
                self.assertEqual([owner.get("uncertain") for owner in owners.values()], [True])

    def test_a_shared_owner_cannot_forge_exclusive_admission(self):
        with resources.Admission("shared", timeout=0):
            token = json.loads(os.environ[resources.TOKEN])
            token["exclusive"] = True
            os.environ[resources.TOKEN] = json.dumps(token)
            with self.assertRaisesRegex(RuntimeError, "capability"):
                resources.inherited(exclusive=True)

    def test_forged_inherited_descriptor_is_rejected(self):
        os.environ[resources.TOKEN] = json.dumps({"schemaVersion": 1, "locks": [{"fd": 999999, "path": "/not-a-lock", "nonce": "bad"}]})
        with self.assertRaises(RuntimeError): resources.inherited()

    def admit(self, *arguments):
        root = Path(self.temp.name)
        (root / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Admit"}))
        receipt = root / ".build/queuekeeper/admit/source.json"
        receipt.unlink(missing_ok=True)
        with patch.object(cli, "refresh_policy", return_value="policy") as policy, patch.object(cli, "source_identity", return_value={}), contextlib.redirect_stderr(StringIO()) as error:
            result = cli.main(["--project", str(root), "admit", *arguments])
        if policy.called:
            # The policy goes to stderr, leaving stdout to the command.
            self.assertEqual(policy.call_args.args, (root.resolve(), error))
        return result, policy, json.loads(receipt.read_text()) if receipt.exists() else None

    def test_admit_stops_a_command_at_its_time_limit_and_leaves_it_unmeasured(self):
        hung = ["--", sys.executable, "-c", "import time; time.sleep(60)"]
        # Its own limit, then queuekeeper's default for a command that declares none.
        for arguments, default in ((["--time-limit", "1"], processes.DEFAULT_LIMIT), ([], 1)):
            with self.subTest(arguments=arguments), patch.object(processes, "DEFAULT_LIMIT", default):
                began = time.monotonic()
                result, _policy, receipt = self.admit(*arguments, *hung)
                self.assertEqual(result, lane_results.UNMEASURED)
                # The 1 s limit is the behavior under test and stays fixed; only the ceiling on the stop stretches.
                self.assertLess(time.monotonic() - began, stretched(30))
                self.assertEqual((receipt["outcome"], receipt["timeLimit"]), ("unmeasured", 1))
                self.assertIn("timed out after 1s", receipt["error"])
        # The positive control: a command that ends within its limit keeps its status.
        # Its limit stretches by load, so that a command that ends at once on an idle host ends within it.
        limit = round(stretched(60))
        result, _policy, receipt = self.admit("--time-limit", str(limit), "--", sys.executable, "-c", "raise SystemExit(7)")
        self.assertEqual((result, receipt["exitStatus"], receipt["timeLimit"]), (7, 7, limit))
        self.assertEqual(self.admit("--time-limit", "0", "--", "true")[0], 2)

    def test_admit_runs_a_command_in_the_current_directory_inside_admission(self):
        work = Path(self.temp.name) / "work"
        work.mkdir()
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(work)
        probe = ("from pathlib import Path\nfrom queuekeeper import resources\n"
                 "assert resources.inherited()\nheld = []\n"
                 "for kind in ['exclusive', 'model']:\n"
                 "    try: resources.inherited(**{kind: True})\n    except RuntimeError: pass\n    else: held.append(kind)\n"
                 "Path('admitted').write_text(' '.join(held) or 'shared')\nraise SystemExit(7)")
        guard = Path(os.environ["QUEUEKEEPER_GATE_LOCK_FILE"] + ".warm-push-guard")
        for options, held in [([], "shared"), (["--model"], "model"), (["--exclusive"], "exclusive model")]:
            with self.subTest(held), patch.dict(os.environ, {"PYTHONPATH": host_isolation.pythonpath()}):
                result, policy, receipt = self.admit(*options, "--", sys.executable, "-c", probe)
                self.assertEqual(result, 7)
                self.assertEqual((work / "admitted").read_text(), held)
                policy.assert_called_once()
                self.assertEqual(receipt["command"], [sys.executable, "-c", probe])
                self.assertEqual((receipt["exitStatus"], receipt["policy"], receipt["exclusive"], receipt["model"]),
                                 (7, "policy", options == ["--exclusive"], options == ["--model"]))
                self.assertEqual(Path(receipt["directory"]).resolve(), work.resolve())
                self.assertFalse(guard.exists())
                self.assertIsNone(os.environ.get(resources.TOKEN))

    def test_admit_passes_a_later_separator_to_its_command(self):
        for arguments in [["--", sys.executable, "-c", "pass", "--", "x"], [sys.executable, "-c", "pass", "--", "x"]]:
            with self.subTest(arguments):
                result, _policy, receipt = self.admit(*arguments)
                self.assertEqual(result, 0)
                self.assertEqual(receipt["command"], [sys.executable, "-c", "pass", "--", "x"])

    def test_admit_without_a_command_refreshes_and_admits_nothing(self):
        result, policy, receipt = self.admit("--exclusive")
        self.assertEqual(result, 2)
        policy.assert_not_called()
        self.assertIsNone(receipt)
        self.assertFalse(resources.slot_directory().exists())

    def test_a_command_that_cannot_start_releases_admission(self):
        guard = Path(os.environ["QUEUEKEEPER_GATE_LOCK_FILE"] + ".warm-push-guard")
        unexecutable = Path(self.temp.name) / "check-all.sh"
        unexecutable.write_text("#!/bin/sh\nexit 0\n")
        for command in [str(Path(self.temp.name) / "swfit-typo"), str(unexecutable)]:
            with self.subTest(command):
                # A retained guard would make every later admission wait; fail instead.
                result, _policy, receipt = self.admit("--timeout", "0", "--", command, "test")
                self.assertEqual(result, 2)
                self.assertEqual(receipt["outcome"], "failed-or-interrupted")
                self.assertIn("did not start", receipt["error"])
                self.assertFalse(guard.exists())
                with contextlib.redirect_stderr(StringIO()):
                    with self.assertRaises(processes.NotStarted):
                        resources.main(["--timeout", "0", "--", command])
                self.assertFalse(guard.exists())
                with contextlib.redirect_stderr(StringIO()):
                    with resources.Admission("next", timeout=0): pass


class HeldPlaceTests(unittest.TestCase):
    """place_holder.hold_place: a waiting whole-host lane's place in this Mac's queue, held by a process of its own for
    the Mac that sent it and renewed by that Mac's words on its standard input (vocaro/queuekeeper#211)."""

    # The holder runs its file by itself, as another Mac is sent it: nothing of the toolkit is on its path.
    HOLD = str(Path(place_holder.__file__))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"HEAVY_SLOT_DIR": str(root / "slots"), "HEAVY_SLOTS": "2", "HEAVY_SLOTS_MAX": "2",
                                           "HEAVY_POLL_SECONDS": "0.01", "QUEUEKEEPER_GATE_LOCK_FILE": str(root / "gate")})
        self.env.start()
        self.addCleanup(self.env.stop)
        os.environ.pop(resources.TOKEN, None)
        self.identity = uuid.uuid4().hex

    def holder(self, identity=..., age=60.0, label="Example gate (waiting, sent by studio)", needs_screen=False, **variables):
        """A holder of the place, and what it first said."""
        request = {"identity": self.identity if identity is ... else identity, "label": label, "age": age, "from": "studio",
                   **({"unlockedScreen": True} if needs_screen else {})}
        holder = subprocess.Popen([sys.executable, "-I", self.HOLD, json.dumps(request)], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                  preexec_fn=host_isolation.default_signals, env={**os.environ, **variables})
        def end():
            if holder.poll() is None: holder.kill()
            holder.wait()
            for stream in (holder.stdin, holder.stdout, holder.stderr):
                with contextlib.suppress(OSError, ValueError): stream.close()
        self.addCleanup(end)
        return holder, json.loads(holder.stdout.readline() or "null")

    def waiters(self):
        """The live queued requests, as admission reads them."""
        directory = resources.slot_directory()
        with (directory / "coordination.lock").open("a+") as bridge:
            fcntl.flock(bridge, fcntl.LOCK_EX)
            return resources.queued_requests(directory / resources.QUEUE)

    def ended(self, holder):
        holder.wait(timeout=stretched(30))
        return holder.returncode

    def until(self, condition, what):
        deadline = time.monotonic() + stretched(30)
        while not condition():
            self.assertLess(time.monotonic(), deadline, what)
            time.sleep(0.02)

    def test_the_holders_file_stands_alone_and_names_the_queue_as_admission_does(self):
        # It is sent to another Mac by itself, so it imports nothing of the toolkit; and it stays out of
        # offload_host.py, which every dispatch carries within what one ssh packet holds.
        import ast
        from queuekeeper import offload_host, remote
        source = Path(place_holder.__file__).read_text()
        imported = {(node.module if isinstance(node, ast.ImportFrom) else alias.name).split(".")[0]
                    for node in ast.walk(ast.parse(source)) if isinstance(node, (ast.Import, ast.ImportFrom))
                    for alias in node.names}
        self.assertLessEqual(imported, set(sys.stdlib_module_names))
        self.assertNotIn("hold_place", Path(offload_host.__file__).read_text())
        self.assertLess(len(remote.login(remote.host_command(source, json.dumps({"label": "x" * 400}))).encode()), 16 * 1024)
        self.assertEqual((place_holder.QUEUE, place_holder.PLACE_SECONDS, place_holder.SLOTS),
                         (resources.QUEUE, resources.PLACE_SECONDS, offload_host.SLOTS))
        self.assertEqual(place_holder.slot_directory(), resources.slot_directory())
        self.assertEqual(place_holder.slot_directory(), offload_host.slot_directory())
        with patch.dict(os.environ):
            os.environ.pop("HEAVY_SLOT_DIR")
            self.assertEqual(place_holder.slot_directory(), offload_host.slot_directory())
        self.assertEqual(offload_host.PLACE_VARIABLE, resources.PLACE)

    def test_it_takes_the_place_at_the_lanes_arrival_as_a_request_admission_knows(self):
        from queuekeeper import offload_host
        mine = {"pid": os.getpid(), "start": resources.process_start(os.getpid())}
        for name, age in (("older", 600), ("newer", 5)):
            place_holder.place_held(name * 8, {**mine, "identity": name * 8, "label": name, "queuedAt": time.time() - age,
                                               "ordinary": True})
        before = time.time()
        holder, said = self.holder(age=60.0)
        self.assertEqual((said["place"], said["pid"]), ("held", holder.pid))
        waiters = self.waiters()
        self.assertEqual([waiter["label"] for waiter in waiters], ["older", "Example gate (waiting, sent by studio)", "newer"])
        place = waiters[1]
        self.assertEqual((place["identity"], place["pid"], place["wholeHost"], place["heldFor"]),
                         (self.identity, holder.pid, True, "studio"))
        self.assertNotIn("ordinary", place)
        # Its arrival is the lane's, by this Mac's clock; and admission of any toolkit finds its owner running.
        self.assertAlmostEqual(place["queuedAt"], before - 60.0, delta=stretched(15))
        self.assertTrue(resources.running(place))
        self.assertLessEqual(time.time() - place["seen"], stretched(15))
        # Admission keeps the guaranteed slots for it, and a reading says so, except to the work it is held for.
        with contextlib.redirect_stderr(StringIO()):
            with self.assertRaisesRegex(TimeoutError, "kept for a whole-host lane queued ahead of it: Example gate"):
                with resources.Admission("later", timeout=0): pass
        self.assertEqual(offload_host.capacity()["kept"], "Example gate (waiting, sent by studio)")
        self.assertIsNone(offload_host.capacity(own=self.identity)["kept"])
        # It holds no slot, no checkout and no admission.
        self.assertEqual(offload_host.capacity()["busy"], [])
        self.assertFalse(resources.guard_path(os.environ).exists())

    def test_the_senders_word_renews_it_and_puts_back_a_place_that_was_dropped(self):
        from queuekeeper import offload_host
        holder, _said = self.holder()
        first = self.waiters()[0]["seen"]
        time.sleep(0.05)
        holder.stdin.write("still\n")
        holder.stdin.flush()
        self.until(lambda: self.waiters() and self.waiters()[0]["seen"] > first, "the place was not renewed")
        arrived = self.waiters()[0]["queuedAt"]
        # Dropped, as by a queue admission could not read and began again: the next word puts it back as it arrived.
        place_holder.place_held(self.identity)
        self.assertEqual(self.waiters(), [])
        holder.stdin.write("still\n")
        holder.stdin.flush()
        self.until(lambda: self.waiters(), "the place was not put back")
        self.assertEqual((self.waiters()[0]["identity"], self.waiters()[0]["queuedAt"]), (self.identity, arrived))
        self.assertIsNone(holder.poll())

    def test_the_senders_leave_its_end_and_a_stop_each_take_the_place_out_at_once(self):
        def leave(holder):
            holder.stdin.write("leave\n")
            holder.stdin.flush()
        for name, how in (("leave", leave), ("the connection's end", lambda holder: holder.stdin.close()),
                          ("SIGTERM", lambda holder: holder.terminate()),
                          ("SIGHUP", lambda holder: holder.send_signal(signal.SIGHUP))):
            with self.subTest(name):
                holder, _said = self.holder()
                self.assertEqual(len(self.waiters()), 1)
                how(holder)
                self.assertEqual(self.ended(holder), 0, holder.stderr.read())
                self.assertFalse((resources.slot_directory() / resources.QUEUE).exists())

    def test_a_holder_that_is_killed_leaves_a_place_admission_drops(self):
        holder, _said = self.holder()
        holder.kill()
        holder.wait()
        # Its record is still written, and holds nothing: admission drops a request whose process has ended.
        self.assertEqual(json.loads((resources.slot_directory() / resources.QUEUE).read_text())["waiters"][0]["identity"],
                         self.identity)
        self.assertEqual(self.waiters(), [])
        with contextlib.redirect_stderr(StringIO()), resources.Admission("later", timeout=0) as later:
            self.assertEqual([slot for slot, _fd in later.descriptors], [0])

    def test_a_silent_sender_lets_the_place_lapse_and_then_the_holder_takes_it_out(self):
        from queuekeeper import offload_host
        # The connection stays open and says nothing, as a sender asleep leaves it.
        holder, _said = self.holder(HEAVY_PLACE_SECONDS="1")
        self.assertEqual(offload_host.capacity()["kept"], "Example gate (waiting, sent by studio)")
        with patch.dict(os.environ, {"HEAVY_PLACE_SECONDS": "1"}):
            # Within HEAVY_PLACE_SECONDS of its last word it holds nothing back, though its holder still runs.
            self.until(lambda: offload_host.capacity()["kept"] is None, "the place went on holding work back")
            with contextlib.redirect_stderr(StringIO()), resources.Admission("later", timeout=0) as later:
                self.assertEqual([slot for slot, _fd in later.descriptors], [0])
        self.assertEqual(self.ended(holder), 0, holder.stderr.read())
        self.assertFalse((resources.slot_directory() / resources.QUEUE).exists())

    def test_a_second_holder_of_the_same_place_replaces_the_first_ones_record(self):
        first, _said = self.holder()
        second, said = self.holder()
        self.assertEqual(said["place"], "held")
        self.assertEqual([waiter["pid"] for waiter in self.waiters()], [second.pid])
        # The first one's end takes out the record of that identity, and the second's next word puts it back.
        first.stdin.close()
        self.ended(first)
        second.stdin.write("still\n")
        second.stdin.flush()
        self.until(lambda: [waiter["pid"] for waiter in self.waiters()] == [second.pid], "the place was not put back")

    # Recorded `ioreg -n Root -d1 -a` of each Mac's screens (tests/mac_desktop).
    SCREENS = Path(__file__).resolve().parents[1] / "mac_desktop/fixtures/ScreenState"

    def show(self, recording, name):
        """Have recording, the file a holder reads its Mac's screen from, be the recorded screen name, whole at once."""
        staged = recording.with_name("staged.plist")
        shutil.copy(self.SCREENS / name, staged)
        os.replace(staged, recording)

    def test_the_holder_reads_a_screen_as_placement_reads_it(self):
        # The holder imports nothing of the toolkit, so it reads the screen itself: the two readings must agree, or a
        # place would be kept on a Mac that placement never sends the lane to (vocaro/queuekeeper#214).
        import plistlib
        from queuekeeper import offload_host
        self.assertEqual(place_holder.SCREEN_RECORDING, offload_host.SCREEN_RECORDING)
        recordings = sorted(self.SCREENS.glob("*.plist"))
        self.assertGreaterEqual(len(recordings), 8)
        damaged = [b"", b"not a property list", plistlib.dumps([]), plistlib.dumps({"IOConsoleUsers": 3}),
                   plistlib.dumps({"IOConsoleUsers": [{"kCGSSessionOnConsoleKey": True, "CGSSessionScreenIsLocked": "yes"}]}),
                   plistlib.dumps({"IOConsoleLocked": True, "IOConsoleUsers": [{"kCGSSessionOnConsoleKey": True}]})]
        states = set()
        for name, found in [*((recording.name, recording.read_bytes()) for recording in recordings),
                            *((f"damaged {index}", found) for index, found in enumerate(damaged))]:
            state, why = place_holder.screen(found)
            self.assertEqual(state, offload_host.screen(found)["state"], name)
            self.assertEqual(why is None, state == "unlocked", name)
            states.add(state)
        self.assertEqual(states, {"unlocked", "locked", "unknown"})
        self.assertEqual(place_holder.screen((self.SCREENS / "second-mac-locked-by-hand.plist").read_bytes()),
                         ("locked", "CGSSessionScreenIsLocked and IOConsoleLocked"))
        self.assertEqual(place_holder.screen((self.SCREENS / "second-mac-no-console-session.plist").read_bytes()),
                         ("unknown", "no login session is on the console"))
        # A recording that cannot be read is unknown to both, never unlocked.
        with patch.dict(os.environ, {place_holder.SCREEN_RECORDING: str(Path(self.temp.name) / "missing.plist")}):
            self.assertEqual((place_holder.screen()[0], offload_host.screen()["state"]), ("unknown", "unknown"))

    def test_a_lane_that_needs_an_unlocked_screen_has_its_place_only_while_the_screen_reads_unlocked(self):
        """vocaro/queuekeeper#214: a place on a Mac whose screen is locked would keep its guaranteed slots for a lane
        that Mac cannot run, so such a lane took no place anywhere. Its holder now keeps the place only while its
        Mac's screen reads unlocked."""
        from queuekeeper import offload_host
        recording = Path(self.temp.name) / "screen.plist"
        # A lapse no step here outlasts, so that the holder looks at the screen every PLACE_POLL seconds.
        reads = {place_holder.SCREEN_RECORDING: str(recording), "HEAVY_PLACE_SECONDS": "600"}

        def word(holder):
            holder.stdin.write("still\n")
            holder.stdin.flush()

        said = lambda holder: json.loads(holder.stdout.readline() or "null")
        self.show(recording, "second-mac-locked-idle.plist")
        holder, first = self.holder(needs_screen=True, **reads)
        # Locked as it starts: it says so, holds no place, and keeps nothing from later work.
        self.assertEqual(first, {"place": "none", "pid": holder.pid, "screen": "locked", "why": "CGSSessionScreenIsLocked"})
        self.assertEqual(self.waiters(), [])
        self.assertIsNone(offload_host.capacity()["kept"])
        with contextlib.redirect_stderr(StringIO()), resources.Admission("later", timeout=0) as later:
            self.assertEqual([slot for slot, _fd in later.descriptors], [0])
        # Each word below is followed by the line it brings, so that the holder has read every word sent before
        # the screen changes again: on a busy Mac it may read a word well after the test wrote it. A screen that
        # cannot be told to be unlocked holds no place either, as placement never guesses one unlocked.
        self.show(recording, "second-mac-no-console-session.plist")
        word(holder)
        self.assertEqual(said(holder), {"place": "none", "pid": holder.pid, "screen": "unknown",
                                        "why": "no login session is on the console"})
        self.assertEqual(self.waiters(), [])
        # Unlocked, the place comes with the sender's next word and not before it, at the lane's arrival.
        self.show(recording, "second-mac-unlocked.plist")
        time.sleep(0.3)
        self.assertEqual(self.waiters(), [])
        before = time.time()
        word(holder)
        held = said(holder)
        self.assertEqual((held["place"], held["pid"]), ("held", holder.pid))
        self.until(self.waiters, "the place was not taken once the screen read unlocked")
        [place] = self.waiters()
        self.assertEqual((place["identity"], place["wholeHost"], place["queuedAt"]), (self.identity, True, held["queuedAt"]))
        self.assertAlmostEqual(place["queuedAt"], before - 60.0, delta=stretched(15))
        self.assertEqual(offload_host.capacity()["kept"], "Example gate (waiting, sent by studio)")
        # Locked again, the place goes within the holder's own look at the screen, before any word from the sender.
        self.show(recording, "second-mac-locked-by-hand.plist")
        self.assertEqual(said(holder), {"place": "none", "pid": holder.pid, "screen": "locked",
                                        "why": "CGSSessionScreenIsLocked and IOConsoleLocked"})
        self.until(lambda: not self.waiters(), "the place outlived the lock")
        self.assertFalse((resources.slot_directory() / resources.QUEUE).exists())
        # Unlocked again, the next word puts it back.
        self.show(recording, "second-mac-unlocked.plist")
        word(holder)
        self.assertEqual(said(holder)["place"], "held")
        self.until(self.waiters, "the place was not taken again")
        # It ends as any holder does, and leaves nothing.
        holder.stdin.close()
        self.assertEqual(self.ended(holder), 0, holder.stderr.read())
        self.assertFalse((resources.slot_directory() / resources.QUEUE).exists())

    def test_a_lane_that_needs_no_unlocked_screen_keeps_its_place_on_a_locked_mac(self):
        recording = Path(self.temp.name) / "screen.plist"
        self.show(recording, "second-mac-locked-by-hand.plist")
        holder, said = self.holder(**{place_holder.SCREEN_RECORDING: str(recording)})
        self.assertEqual(said["place"], "held")
        self.assertEqual([waiter["identity"] for waiter in self.waiters()], [self.identity])

    def test_a_request_with_no_identity_of_a_place_is_refused(self):
        for identity in ("", "not-hex", "A" * 32, None, 7):
            with self.subTest(identity=identity):
                holder, said = self.holder(identity=identity)
                self.assertIn("a place needs an identity of 32 hex digits", said["error"])
                self.assertEqual(self.ended(holder), 2)
                self.assertFalse((resources.slot_directory() / resources.QUEUE).exists())


class MacSettingsTests(unittest.TestCase):
    """offload_host.mac_settings: a Mac's own settings for the work it takes, mac-settings.json beside its limits file
    (the owner's decision, 2026-11-17, vocaro/queuekeeper#211): kinds of work and labels, and no lane's name."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.record = root / "device-handoff.json"
        self.env = patch.dict(os.environ, {"HEAVY_SLOT_DIR": str(root / "slots"), "HEAVY_SLOTS": "2", "HEAVY_SLOTS_MAX": "2",
                                           "QUEUEKEEPER_GATE_LOCK_FILE": str(root / "gate"),
                                           "QUEUEKEEPER_DEVICE_RECORD": str(self.record)})
        self.env.start()
        self.addCleanup(self.env.stop)
        (root / "slots").mkdir()
        self.file = root / "slots" / "mac-settings.json"

    def test_a_mac_with_no_settings_takes_every_kind_but_foreground_work_and_has_no_label(self):
        from queuekeeper import offload_host
        self.assertEqual(offload_host.mac_settings(),
                         {"takes": {"wholeHost": True, "exclusive": True, "model": True, "foreground": False}, "set": [],
                          "labels": [], "file": str(self.file), "ignored": []})
        # The file is beside the limits file, and is another file: an older toolkit refuses a limits key it does not know.
        self.assertEqual(self.file.parent, resources.limits_path().parent)
        self.assertNotEqual(self.file, resources.limits_path())
        # Foreground work it takes only when its settings say so: a device record that defers physical devices to
        # another Mac's, which told such a Mac until 2026-11-17, says nothing of it.
        self.record.write_text(json.dumps({"devices": {}, offload_host.DEFERRED: "studio"}))
        self.assertIs(offload_host.mac_settings()["takes"]["foreground"], False)
        self.assertIs(offload_host.capacity()["takes"]["foreground"], False)

    def test_its_settings_say_the_kinds_it_takes_and_its_labels(self):
        from queuekeeper import offload_host
        self.file.write_text(json.dumps({"takes": {"wholeHost": False, "foreground": True},
                                         "labels": ["unity-license", "apple-m5-max", "unity-license"]}))
        found = offload_host.mac_settings()
        self.assertEqual(found["takes"], {"wholeHost": False, "exclusive": True, "model": True, "foreground": True})
        self.assertEqual((found["set"], found["labels"]), (["foreground", "wholeHost"], ["apple-m5-max", "unity-license"]))
        self.file.write_text(json.dumps({"takes": {"foreground": False}}))
        self.assertEqual(offload_host.mac_settings()["takes"]["foreground"], False)
        # The reading placement takes says the same, with where it is set.
        reading = offload_host.capacity()
        self.assertEqual((reading["takes"], reading["takesSet"], reading["labels"], reading["settingsFile"]),
                         ({"wholeHost": True, "exclusive": True, "model": True, "foreground": False}, ["foreground"], [],
                          str(self.file)))

    def test_a_key_this_toolkit_does_not_know_is_passed_over_and_named(self):
        from queuekeeper import offload_host
        self.file.write_text(json.dumps({"takes": {"model": False, "gpuWork": True}, "labels": ["a"], "devices": {}}))
        found = offload_host.mac_settings()
        self.assertEqual((found["takes"]["model"], found["ignored"]), (False, ["devices", "takes.gpuWork"]))
        # The reading another Mac takes of this one names them too, which `qk admissions usage` prints there
        # (vocaro/queuekeeper#224).
        reading = offload_host.capacity()
        self.assertNotIn("error", reading)
        self.assertEqual(reading["settingsIgnored"], ["devices", "takes.gpuWork"])

    def test_settings_that_cannot_be_read_stop_the_reading_rather_than_guess_what_the_mac_takes(self):
        from queuekeeper import offload_host
        for text, wrong in (("{", "is not JSON"), ("[]", "holds no JSON object"),
                            (json.dumps({"takes": ["wholeHost"]}), "gives takes as something other than an object"),
                            (json.dumps({"takes": {"wholeHost": "no"}}), 'sets takes.wholeHost to "no", not true or false'),
                            (json.dumps({"takes": {"model": 0}}), "sets takes.model to 0, not true or false"),
                            (json.dumps({"labels": "gpu"}), "gives labels as something other than a list of labels"),
                            (json.dumps({"labels": ["Unity License"]}), "gives labels as something other than a list")):
            with self.subTest(text=text):
                self.file.write_text(text)
                with self.assertRaisesRegex(ValueError, f"Invalid Mac settings: {re.escape(str(self.file))} {re.escape(wrong)}"):
                    offload_host.mac_settings()
                self.assertIn(f"Invalid Mac settings: {self.file}", offload_host.capacity()["error"])

    def test_a_lane_says_the_labels_it_needs_and_names_no_mac(self):
        base = {"schemaVersion": 1, "name": "Project"}
        parse = lambda lanes: config.parse(json.dumps({**base, "lanes": lanes}))["lanes"]
        lanes = parse({"suite": {"command": ["true"], "resource": "heavy", "labels": ["unity-license", "gpu"]},
                       "docs": {"command": ["true"], "labels": ["gpu", "display"]},
                       "gate": {"parallel": ["suite", "docs"], "resource": "heavy", "labels": ["license"]}})
        self.assertEqual(config.needed_labels(lanes, "suite"), ("unity-license", "gpu"))
        # A group runs its lanes on one Mac, which needs what each of them needs.
        self.assertEqual(config.needed_labels(lanes, "gate"), ("license", "unity-license", "gpu", "display"))
        self.assertEqual(config.needed_labels(parse({"plain": {"command": ["true"]}}), "plain"), ())
        for wrong, said in (("gpu", "must be a non-empty list of labels"), ([], "must be a non-empty list of labels"),
                            (["Unity License"], "each lowercase words joined by hyphens"), (["gpu", "gpu"], "named once"),
                            ([7], "must be a non-empty list of labels")):
            with self.subTest(wrong=wrong), self.assertRaisesRegex(ValueError, f"suite.labels.*{said}"):
                parse({"suite": {"command": ["true"], "labels": wrong}})
        with self.assertRaisesRegex(ValueError, "suite.labels needs an admitted lane"):
            parse({"suite": {"command": ["true"], "resource": "self", "labels": ["gpu"]}})
        # A lane's label and a Mac's are spelled by one rule.
        from queuekeeper import offload_host
        self.assertEqual(config.LABEL.pattern, offload_host.LABEL.pattern)

    def test_admissions_limits_prints_what_the_mac_takes_beside_how_much(self):
        self.file.write_text(json.dumps({"takes": {"wholeHost": False}, "labels": ["gpu"], "later": 1}))
        said = []
        self.assertEqual(resources.limits_main([], out=said.append), 0)
        self.assertIn(f"takes: whole-host gates no (set), exclusive work yes (default), model runs yes (default), "
                      f"foreground work no (default); settings file {self.file}", said)
        self.assertIn("labels: gpu", said)
        self.assertIn("ignored: later, which this toolkit does not know", said)
        self.file.write_text("{")
        with contextlib.redirect_stderr(StringIO()) as error:
            self.assertEqual(resources.limits_main([], out=said.append), 2)
        self.assertIn("Invalid Mac settings", error.getvalue())


class CpuBusyTests(unittest.TestCase):
    """The CPU's busy share, which judges the slots beyond the guaranteed ones in place of the load average
    (vocaro-engineering#200): read from the kernel's ticks against samples every process keeps in the slot directory."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.clock, self.slept = [1000.0], []
        # user, system, idle, nice; each step adds the ticks of one second at the busy share it names.
        self.counters = [0, 0, 0, 0]

    def run_for(self, seconds, busy):
        """Let seconds pass at busy percent, a hundred ticks a second."""
        ticks = 100 * seconds
        self.counters[0] += round(ticks * busy / 100)
        self.counters[2] += ticks - round(ticks * busy / 100)
        self.clock[0] += seconds

    def read(self, wait=False, ticks=None):
        def sleep(seconds):
            self.slept.append(seconds)
            self.run_for(seconds, 50)
        return resources.cpu_busy(self.directory, wait=wait, ticks=ticks or (lambda: list(self.counters)),
                                  now=lambda: self.clock[0], sleep=sleep)

    def test_a_reading_spans_the_window_from_samples_other_readings_kept(self):
        # The first reading has nothing to read against, and says how long the samples span: none yet.
        self.assertEqual(self.read(), (None, 0.0))
        self.run_for(3, 90)
        # Short of BUSY_MINIMUM it still has none, and says how long it has read.
        self.assertEqual(self.read(), (None, 3.0))
        self.run_for(3, 30)
        # Past BUSY_MINIMUM, the oldest sample: 90% for 3s and 30% for 3s.
        self.assertEqual(self.read(), (60.0, 6.0))
        # Past BUSY_WINDOW, the youngest sample at least that old: the 30s at 20% alone, not the early 90%.
        for _ in range(30):
            self.run_for(1, 20)
            self.read()
        busy, seconds = self.read()
        self.assertEqual((busy, seconds), (20.0, 15.0))
        self.assertEqual(self.slept, [])
        # Samples beyond BUSY_KEEP are dropped, so the file stays small.
        self.run_for(int(resources.BUSY_KEEP) + 1, 0)
        self.assertEqual(self.read(), (None, 0.0))
        self.assertEqual(len(json.loads((self.directory / resources.CPU_SAMPLES).read_text())), 1)

    def test_a_one_off_reading_waits_for_its_own_only_when_no_sample_spans_one(self):
        self.assertEqual(self.read(wait=True), (50.0, resources.BUSY_QUICK))
        self.assertEqual(self.slept, [resources.BUSY_QUICK])
        # Negative control: with a sample old enough it waits for nothing, and reads from that sample: 2s at 50%
        # and 10s at 80%.
        self.run_for(10, 80)
        self.assertEqual(self.read(wait=True), (75.0, 12.0))
        self.assertEqual(self.slept, [resources.BUSY_QUICK])

    def test_ticks_that_cannot_be_read_and_a_file_that_cannot_be_read(self):
        self.assertEqual(self.read(ticks=lambda: None), (None, None))
        self.assertEqual(self.read(wait=True, ticks=lambda: None), (None, None))
        # A damaged samples file is set aside, not trusted, and the next readings rebuild it.
        (self.directory / resources.CPU_SAMPLES).write_text("{")
        self.assertEqual(self.read(), (None, 0.0))
        (self.directory / resources.CPU_SAMPLES).write_text(json.dumps([["x", 1, 2, 3, 4], [999.0, 1, 2], True]))
        self.assertEqual(self.read(), (None, 0.0))
        self.run_for(6, 70)
        self.assertEqual(self.read()[0], 70.0)

    def test_the_share_counts_every_state_but_idle_and_survives_a_counter_wrapping(self):
        self.assertEqual(resources.busy_share([0, 0, 0, 0], [25, 25, 40, 10]), 60.0)
        self.assertIsNone(resources.busy_share([5, 5, 5, 5], [5, 5, 5, 5]))
        top = 1 << 32
        self.assertEqual(resources.busy_share([top - 10, 0, top - 10, 0], [10, 0, 10, 0]), 50.0)

    def test_offload_host_reads_the_samples_as_admission_does(self):
        from queuekeeper import offload_host
        self.assertEqual((offload_host.CPU_SAMPLES, offload_host.BUSY_WINDOW, offload_host.BUSY_MINIMUM,
                          offload_host.BUSY_QUICK, offload_host.BUSY_KEEP),
                         (resources.CPU_SAMPLES, resources.BUSY_WINDOW, resources.BUSY_MINIMUM, resources.BUSY_QUICK,
                          resources.BUSY_KEEP))
        self.read()
        self.run_for(10, 40)
        with patch.object(offload_host, "cpu_ticks", return_value=list(self.counters)), \
                patch.object(offload_host.time, "time", return_value=self.clock[0]), \
                patch.object(offload_host.time, "sleep", side_effect=AssertionError("waited with a sample to read")):
            self.assertEqual(offload_host.cpu_busy(self.directory), 40.0)
        self.assertEqual(offload_host.busy_share([0, 0, 0, 0], [25, 25, 40, 10]), 60.0)

    @unittest.skipUnless(sys.platform == "darwin", "the kernel's ticks are read on macOS")
    def test_this_macs_ticks_are_read_and_rise(self):
        self.assert_ticks_rise(stretched(resources.BUSY_MINIMUM))
        # Negative control: ticks that never change fail it, at its bound, and do not wait past it.
        stuck = resources.cpu_ticks()
        began = time.monotonic()
        with patch.object(resources, "cpu_ticks", lambda: list(stuck)), self.assertRaises(AssertionError):
            self.assert_ticks_rise(0.2)
        self.assertLess(time.monotonic() - began, stretched(5))
        from queuekeeper import offload_host
        self.assertEqual(len(offload_host.cpu_ticks()), offload_host.CPU_STATES)

    def assert_ticks_rise(self, seconds):
        """The summed ticks rise within seconds. The kernel adds to them in steps, and at loads of 385 to 440 on
        18 cores two reads 0.2 s apart were equal (#237), so it polls until they rise, and fails at the bound."""
        first = resources.cpu_ticks()
        self.assertEqual(len(first), resources.CPU_STATES)
        deadline = time.monotonic() + seconds
        while sum(resources.cpu_ticks()) == sum(first) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertGreater(sum(resources.cpu_ticks()), sum(first))


class ExclusiveSettleTests(unittest.TestCase):
    """An exclusive admission holds every slot and waits for the CPU to read quiet before its command starts, bounded,
    and tells the command what it read (vocaro/queuekeeper#121): mpm's timed gates, admitted the moment two whole-host
    lanes ended, read the load those lanes left behind and voided (2026-11-13)."""
    setUp, tearDown = ResourceTests.setUp, ResourceTests.tearDown

    def lines(self):
        log = resources.log_path()
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def admit(self, readings, quiet_seconds="60", **options):
        """Admit exclusive work while the CPU reads each of readings in turn, the last for good, and return the
        command's environment as the admission told it, what admission said and the ended line."""
        readings = list(readings)
        busy = lambda *_args, **_kwargs: readings.pop(0) if len(readings) > 1 else readings[0]
        told = {}
        with patch.dict(os.environ, {"HEAVY_QUIET_SECONDS": quiet_seconds, "HEAVY_QUIET_LIMIT": "25"}), \
                patch.object(resources, "cpu_busy", side_effect=busy), contextlib.redirect_stderr(StringIO()) as said:
            with resources.Admission("timed gates", exclusive=True, timeout=0, **options):
                told = {name: os.environ.get(name) for name in resources.HOST_READING}
            after = {name: os.environ.get(name) for name in resources.HOST_READING}
        self.assertEqual(after, {name: None for name in resources.HOST_READING})
        return told, said.getvalue(), self.lines()[-1]

    def test_it_waits_while_the_work_it_waited_out_still_shows_and_starts_once_the_cpu_is_quiet(self):
        # The first reading is the attempt's, which takes the slots; the settle reads the rest, a poll apart, long
        # enough that the seconds it waited round to more than none.
        os.environ["HEAVY_POLL_SECONDS"] = "0.05"
        with patch.object(resources, "one_minute_load", return_value=25.9):
            told, said, ended = self.admit([(90.0, 15.0), (90.0, 15.0), (None, 2.0), (60.0, 15.0), (12.0, 15.0)])
        self.assertIn("heavy: waiting for the Mac to settle: the CPU is 90% busy, at or above HEAVY_QUIET_LIMIT=25%; "
                      "it starts within HEAVY_QUIET_SECONDS=60 either way", said)
        self.assertEqual(said.count("waiting for the Mac to settle"), 1)
        self.assertRegex(said, r"heavy: the Mac settled after [0-9.]+s: the CPU is 12% busy")
        self.assertEqual((told[resources.HOST_BUSY], told[resources.HOST_BUSY_SECONDS], told[resources.HOST_QUIET]),
                         ("12", "15", "1"))
        self.assertEqual(told[resources.HOST_LOAD], "25.9")
        self.assertGreater(float(told[resources.HOST_SETTLED]), 0)
        self.assertEqual((ended["event"], ended["outcome"], ended["settled"]["busy"], ended["settled"]["quiet"]),
                         ("ended", "released", 12.0, True))
        self.assertGreater(ended["settled"]["seconds"], 0)

    def test_a_mac_already_quiet_starts_at_once_and_says_nothing(self):
        told, said, ended = self.admit([(8.0, 15.0)])
        self.assertNotIn("settle", said)
        self.assertEqual((told[resources.HOST_BUSY], told[resources.HOST_QUIET]), ("8", "1"))
        self.assertEqual(ended["settled"], {"seconds": 0.0, "busy": 8.0, "quiet": True})

    def test_a_mac_that_stays_busy_starts_at_the_bound_and_says_it_is_not_quiet(self):
        told, said, ended = self.admit([(90.0, 15.0)], quiet_seconds="0.05")
        self.assertRegex(said, r"heavy: starting without a quiet Mac: the CPU is still 90% busy after [0-9.]+s, at or "
                               r"above HEAVY_QUIET_LIMIT=25%")
        self.assertEqual((told[resources.HOST_BUSY], told[resources.HOST_QUIET]), ("90", "0"))
        self.assertEqual((ended["settled"]["busy"], ended["settled"]["quiet"]), (90.0, False))

    def test_the_final_poll_uses_only_the_time_left_to_settle(self):
        now, sleeps = 0.0, []

        def sleep(seconds):
            nonlocal now
            sleeps.append(seconds)
            now += seconds

        with patch.object(resources, "time", wraps=time, monotonic=lambda: now, sleep=sleep), \
                patch.dict(os.environ, {"HEAVY_POLL_SECONDS": "1"}):
            told, _, _ = self.admit([(90.0, 15.0)], quiet_seconds="1.25")
        self.assertEqual(sleeps, [1.0, 0.25])
        self.assertEqual(told[resources.HOST_QUIET], "0")

    def test_a_busy_share_that_cannot_be_read_starts_at_once_unsettled(self):
        told, said, ended = self.admit([(None, None)])
        self.assertIn("heavy: starting without a quiet Mac: the CPU's busy share cannot be read", said)
        self.assertEqual((told[resources.HOST_BUSY], told[resources.HOST_BUSY_SECONDS], told[resources.HOST_QUIET]),
                         (None, None, "0"))
        self.assertEqual(ended["settled"], {"seconds": 0.0, "busy": None, "quiet": False})

    def test_no_settle_waits_for_nothing_and_logs_nothing_but_still_tells_the_reading(self):
        told, said, ended = self.admit([(90.0, 15.0)], quiet_seconds="0")
        self.assertNotIn("settle", said)
        self.assertNotIn("quiet Mac", said)
        self.assertEqual((told[resources.HOST_BUSY], told[resources.HOST_QUIET]), ("90", "0"))
        self.assertNotIn("settled", ended)

    def test_a_stop_while_the_mac_settles_releases_every_slot_and_starts_nothing(self):
        stops = iter([None, signal.SIGTERM])
        with patch.object(processes, "requested", side_effect=lambda: next(stops, signal.SIGTERM)), \
                self.assertRaisesRegex(processes.Interrupted, "while the Mac settled for exclusive work"):
            self.admit([(90.0, 15.0)])
        ended = self.lines()[-1]
        self.assertEqual((ended["event"], ended["outcome"], ended["slots"]), ("ended", "stopped", [0, 1, 2]))
        self.assertFalse(resources.guard_path(os.environ).exists())
        self.assertIsNone(os.environ.get(resources.TOKEN))
        with contextlib.redirect_stderr(StringIO()), resources.Admission("next", exclusive=True, timeout=0):
            pass

    def test_heavy_and_whole_host_work_neither_settle_nor_are_told(self):
        for options in [{}, {"whole_host": True}, {"model": True}]:
            with self.subTest(options), patch.dict(os.environ, {"HEAVY_QUIET_SECONDS": "60"}), \
                    patch.object(resources, "cpu_busy", return_value=(90.0, 15.0)), \
                    contextlib.redirect_stderr(StringIO()) as said:
                with resources.Admission("work", timeout=0, **options):
                    self.assertEqual({name: os.environ.get(name) for name in resources.HOST_READING},
                                     {name: None for name in resources.HOST_READING})
                self.assertNotIn("settle", said.getvalue())
                self.assertNotIn("settled", self.lines()[-1])

    def test_invalid_settle_limits_are_refused(self):
        for variable in ["HEAVY_QUIET_SECONDS", "HEAVY_QUIET_LIMIT"]:
            for value in ["-1", "inf", "nan"]:
                with self.subTest(variable=variable, value=value), patch.dict(os.environ, {variable: value}), \
                        self.assertRaisesRegex(ValueError, "Invalid resource limits"):
                    resources.Admission("timed", exclusive=True)


class SlowAttempts:
    """Admission's clock, as resources reads it, with each attempt to admit taking two seconds more by it, as an
    attempt may on a loaded host, so that a test cannot pass only because its host was fast (#15)."""
    skew = 0.0

    class Clock:
        def __getattr__(self, name): return getattr(time, name)
        def monotonic(self): return time.monotonic() + SlowAttempts.skew
    clock = Clock()

    def attempt(admission, _attempt=resources.Admission._attempt):
        try: return _attempt(admission)
        finally: SlowAttempts.skew += 2


class AdmissionLogTests(unittest.TestCase):
    """The host's admission log, from which an audit counts admissions, waits and holds (#51)."""
    setUp, tearDown = ResourceTests.setUp, ResourceTests.tearDown
    hold, request = ResourceTests.hold, ResourceTests.request

    def lines(self):
        """The current log's lines, oldest first."""
        log = resources.log_path()
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def test_the_default_log_is_in_the_accounts_durable_state_whatever_home_a_session_sets(self):
        os.environ.pop("HEAVY_SLOT_DIR")
        state = "Library/Application Support" if sys.platform == "darwin" else ".local/state"
        expected = Path(pwd.getpwuid(os.getuid()).pw_dir) / state / "queuekeeper/admissions.jsonl"
        with patch.dict(os.environ, {"HOME": self.temp.name}):
            self.assertEqual(resources.log_path(), expected)

    def test_an_admission_writes_a_line_and_its_end_another_with_the_seconds_held(self):
        for kind, options, slots in [("heavy", {}, [0]), ("model", {"model": True}, [0]),
                                     ("exclusive", {"exclusive": True}, [0, 1, 2])]:
            with self.subTest(kind), contextlib.redirect_stderr(StringIO()):
                resources.log_path().unlink(missing_ok=True)
                with resources.Admission(kind, timeout=0, **options) as admission:
                    self.assertEqual([line["event"] for line in self.lines()], ["admitted"])
                    # Nested work reuses the admission and writes nothing.
                    with resources.Admission("nested", timeout=0) as nested:
                        self.assertTrue(nested.reused)
                    self.assertEqual(len(self.lines()), 1)
                    time.sleep(0.05)
                admitted, ended = self.lines()
                for line, event in [(admitted, "admitted"), (ended, "ended")]:
                    self.assertEqual((line["event"], line["identity"], line["pid"], line["kind"], line["label"], line["slots"]),
                                     (event, admission.identity, os.getpid(), kind, kind, slots))
                    self.assertIsNotNone(datetime.fromisoformat(line["time"]).tzinfo)
                self.assertGreaterEqual(admitted["waited"], 0)
                # At least the time it slept; a busy host only lengthens it.
                self.assertGreaterEqual(ended["held"], 0.05)
                self.assertEqual(ended["outcome"], "released")

    def test_each_end_is_logged_with_the_outcome_that_settled_its_guard_entry(self):
        guard = Path(os.environ["QUEUEKEEPER_GATE_LOCK_FILE"] + ".warm-push-guard")
        cases = [("released", None), ("not-started", processes.NotStarted("Command did not start")),
                 ("stopped", processes.Interrupted(signal.SIGTERM, "after its command")),
                 ("timed-out", processes.TimedOut("Command timed out")), ("uncertain", RuntimeError("children may remain"))]
        for outcome, failure in cases:
            with self.subTest(outcome):
                guard.unlink(missing_ok=True)
                with contextlib.redirect_stderr(StringIO()), contextlib.ExitStack() as stack:
                    if failure is not None: stack.enter_context(self.assertRaises(type(failure)))
                    with resources.Admission(outcome, timeout=0) as admission:
                        if failure is not None: raise failure
                ended = self.lines()[-1]
                self.assertEqual((ended["event"], ended["identity"], ended["outcome"]), ("ended", admission.identity, outcome))
                # Only an uncertain end keeps its entry, marked for inspection.
                owners = json.loads(guard.read_text())["owners"] if guard.exists() else {}
                self.assertEqual(owners.get(admission.identity, {}).get("uncertain"), True if outcome == "uncertain" else None)

    def test_a_request_that_stops_waiting_writes_one_line_and_its_refused_attempts_none(self):
        self.hold(0, 1, 2)
        queue = resources.slot_directory() / "exclusive-queue.json"
        real, refused = resources.Admission._attempt, []
        def attempt(admission):
            if admission.label == "failed": raise RuntimeError("Cannot establish the admission owner's process identity")
            admitted = real(admission)
            refused.append(len(self.lines()))
            if admission.label == "stopped" and len(refused) == 3: processes.request_stop(signal.SIGTERM)
            return admitted
        self.addCleanup(processes._stops.clear)
        cases = [("timed-out", "timed-out", TimeoutError, {}, 0), ("stopped", "stopped", processes.Interrupted, {}, 60),
                 ("failed", "failed", RuntimeError, {}, 60), ("exclusive", "timed-out", TimeoutError, {"exclusive": True}, 0)]
        for label, outcome, failure, options, timeout in cases:
            with self.subTest(label), patch.object(resources.Admission, "_attempt", attempt), \
                    contextlib.redirect_stderr(StringIO()):
                resources.log_path().unlink(missing_ok=True)
                refused.clear()
                admission = resources.Admission(label, timeout=timeout, **options)
                with self.assertRaises(failure):
                    with admission: self.fail("admitted")
                processes._stops.clear()
                # Each refusal left the log empty.
                self.assertEqual(refused, [0] * (3 if label == "stopped" else 0 if label == "failed" else 1))
                (line,) = self.lines()
                self.assertEqual((line["event"], line["identity"], line["kind"], line["label"], line["outcome"]),
                                 ("withdrawn", admission.identity, admission.kind, label, outcome))
                self.assertGreaterEqual(line["waited"], 0)
                self.assertFalse(queue.exists())

    def test_a_log_that_cannot_be_written_never_fails_or_holds_up_admission(self):
        log, guard = resources.log_path(), Path(os.environ["QUEUEKEEPER_GATE_LOCK_FILE"] + ".warm-push-guard")
        log.parent.mkdir(parents=True)
        # A FIFO no process reads, which a plain open would wait on for a reader. In a process of its
        # own, so that a wait fails the test instead of hanging the suite.
        os.mkfifo(log)
        script = "from queuekeeper import resources\nwith resources.Admission('fifo', timeout=0): print('admitted')\n"
        environment = {**os.environ, "PYTHONPATH": host_isolation.pythonpath()}
        result = subprocess.run([sys.executable, "-c", script], env=environment, capture_output=True, text=True,
                                timeout=stretched(120))
        self.assertEqual((result.returncode, result.stdout), (0, "admitted\n"), result.stderr)
        self.assertEqual(result.stderr.count("heavy: the admission log was not written: "), 2, result.stderr)
        self.assertFalse(guard.exists())
        # A directory, which cannot be opened for writing.
        log.unlink()
        log.mkdir()
        with contextlib.redirect_stderr(StringIO()) as error:
            with resources.Admission("unwritten", timeout=0) as admission:
                self.assertIn(admission.identity, json.loads(guard.read_text())["owners"])
            self.assertFalse(guard.exists())
            # A request that stops waiting fails as it would have: `qk run --if-free` tells a busy host by it.
            self.hold(0, 1, 2)
            with self.assertRaisesRegex(TimeoutError, "Resource queue busy"):
                with resources.Admission("refused", timeout=0): self.fail("admitted")
        self.assertEqual(error.getvalue().count("heavy: the admission log was not written: "), 3, error.getvalue())
        # Nor does a coordination lock that cannot be opened to log the withdrawal.
        with patch.dict(os.environ, {"HEAVY_SLOT_DIR": str(Path(self.temp.name) / "gone")}), \
                patch.object(resources.Admission, "_attempt", side_effect=TimeoutError("Resource queue busy")):
            with self.assertRaisesRegex(TimeoutError, "Resource queue busy"):
                with resources.Admission("refused", timeout=0): self.fail("admitted")

    def test_the_log_stays_within_its_bound_and_keeps_the_newest_lines(self):
        log, limit, identities = resources.log_path(), 2000, []
        with patch.object(resources, "LOG_LIMIT", limit), contextlib.redirect_stderr(StringIO()):
            for number in range(20):
                with resources.Admission(f"job {number}", timeout=0) as admission:
                    identities.append(admission.identity)
        sizes = [log.stat().st_size, log.with_name(resources.PREVIOUS_LOG).stat().st_size]
        self.assertTrue(all(size <= limit for size in sizes), sizes)
        entries, unreadable = resources.read_log()
        self.assertEqual(unreadable, 0)
        kept = [entry["identity"] for entry in entries if entry["event"] == "admitted"]
        # The admissions written outgrew both generations, so the oldest were dropped.
        self.assertLess(len(kept), len(identities))
        # The newest, oldest first, with none missing between the generations.
        self.assertEqual(kept, identities[-len(kept):])
        self.assertEqual((entries[-1]["event"], entries[-1]["identity"]), ("ended", identities[-1]))

    def test_concurrent_admissions_are_logged_in_the_order_they_held_their_slots(self):
        # Two slots and no adaptive ones, so that six requests in processes of their own contend for them.
        labels = ["a", "b", "c", "d", "e", "f"]
        requests = [self.request(label, variables={"HEAVY_SLOTS_MAX": "2"}) for label in labels]
        for label, (_request, line) in zip(labels, requests):
            self.assertRegex(line, r"heavy: slot " if label in "ab" else r"heavy: waiting \(\d+s\): all 2 guaranteed slots are busy")
        for request, _line in requests: request.stdin.close()
        self.assertEqual([request.wait(timeout=stretched(120)) for request, _line in requests], [0] * len(labels))
        entries = self.lines()
        self.assertEqual(sorted((entry["label"], entry["event"]) for entry in entries),
                         sorted((label, event) for label in labels for event in ["admitted", "ended"]))
        # Replayed in order, the log holds no more admissions at once than there are slots: an end is
        # written before its slot can be taken again, and an admission while it holds its slots.
        holding, most = set(), 0
        for entry in entries:
            if entry["event"] == "admitted": holding.add(entry["identity"])
            else: holding.remove(entry["identity"])
            most = max(most, len(holding))
        self.assertEqual(most, 2)
        waited = {entry["label"]: entry["waited"] for entry in entries if entry["event"] == "admitted"}
        self.assertTrue(all(waited[label] > 0 for label in "cdef"), waited)

    def test_the_summary_counts_the_requests_of_a_period_per_kind_and_label(self):
        def line(identity, event, kind, label, time, **fields):
            return json.dumps({"time": time, "event": event, "identity": identity, "pid": 1, "kind": kind,
                               "label": label, **fields}) + "\n"
        log = resources.log_path()
        log.parent.mkdir(parents=True)
        previous = [
            # Admitted before the log's first line: only its end is left, and it counts in no period.
            line("old", "ended", "exclusive", "gate", "2026-10-19T22:00:00+00:00", outcome="released", held=100),
            # Admitted before the period.
            line("x1", "admitted", "exclusive", "gate", "2026-10-19T23:00:00+00:00", waited=50),
            line("x1", "ended", "exclusive", "gate", "2026-10-20T01:00:00+00:00", outcome="released", held=7200)]
        current = [
            line("x2", "admitted", "exclusive", "gate", "2026-10-20T08:00:00+00:00", waited=600),
            line("h1", "admitted", "heavy", "test", "2026-10-20T09:00:00+00:00", waited=5),
            line("x2", "ended", "exclusive", "gate", "2026-10-20T08:30:00+00:00", outcome="released", held=1800),
            line("x3", "admitted", "exclusive", "gate", "2026-10-20T10:00:00+00:00", waited=3000),
            line("x3", "ended", "exclusive", "gate", "2026-10-20T10:01:00+00:00", outcome="uncertain", held=60),
            line("h1", "ended", "heavy", "test", "2026-10-20T09:05:00+00:00", outcome="released", held=300),
            # Admitted with no end logged: still held, or killed.
            line("x4", "admitted", "exclusive", "bench", "2026-10-20T12:00:00+00:00", waited=0),
            line("w1", "withdrawn", "exclusive", "gate", "2026-10-20T13:00:00+00:00", outcome="timed-out", waited=5400),
            line("m1", "admitted", "model", "generate", "2026-10-20T14:00:00+00:00", waited=30),
            "a torn line\n",
            line("m1", "ended", "model", "generate", "2026-10-20T14:01:30+00:00", outcome="stopped", held=90),
            # Admitted after the period.
            line("h2", "admitted", "heavy", "test", "2026-10-21T01:00:00+00:00", waited=1)]
        log.with_name(resources.PREVIOUS_LOG).write_text("".join(previous))
        log.write_text("".join(current))
        expected = [
            "Requests made from 2026-10-20 00:00 UTC until 2026-10-21 00:00 UTC:",
            "exclusive: 3 admitted, 1 withdrawn (timed-out 1); waited mean 37m30s, max 1h30m; held mean 15m30s, "
            "max 30m00s, total 31m00s; ends released 1, uncertain 1, unlogged 1",
            "  gate: 2 admitted, 1 withdrawn (timed-out 1); waited mean 50m00s, max 1h30m; held mean 15m30s, "
            "max 30m00s, total 31m00s; ends released 1, uncertain 1",
            "  bench: 1 admitted; waited mean 0s, max 0s; ends unlogged 1",
            "model: 1 admitted; waited mean 30s, max 30s; held mean 1m30s, max 1m30s, total 1m30s; ends stopped 1",
            "  generate: 1 admitted; waited mean 30s, max 30s; held mean 1m30s, max 1m30s, total 1m30s; ends stopped 1",
            "heavy: 1 admitted; waited mean 5s, max 5s; held mean 5m00s, max 5m00s, total 5m00s; ends released 1",
            "  test: 1 admitted; waited mean 5s, max 5s; held mean 5m00s, max 5m00s, total 5m00s; ends released 1",
            "1 line(s) could not be read."]
        copies = Path(self.temp.name) / "copies"
        copies.mkdir()
        for name in [resources.PREVIOUS_LOG, resources.LOG]: shutil.copy(log.with_name(name), copies / name)
        period = ["--since", "2026-10-20T00:00:00+00:00", "--until", "2026-10-21T00:00+00:00"]
        with patch.dict(os.environ, {"TZ": "UTC"}):
            time.tzset()
            self.addCleanup(time.tzset)
            for source in [[], [str(copies / resources.PREVIOUS_LOG), str(copies / resources.LOG)]]:
                with self.subTest(source=source), contextlib.redirect_stdout(StringIO()) as output:
                    self.assertEqual(cli.main(["admissions", *period, *source]), 0)
                printed = output.getvalue().splitlines()
                self.assertEqual(printed[0], f"Admission log {', '.join(source) or log}, from 2026-10-19 22:00 UTC "
                                             "to 2026-10-21 01:00 UTC.")
                self.assertEqual(printed[1:], expected)
            # A period that begins before the log says what it no longer holds.
            with contextlib.redirect_stdout(StringIO()) as output:
                self.assertEqual(cli.main(["admissions", "--since", "2026-10-01T00:00+00:00"]), 0)
            self.assertIn("The period begins before the log does: requests made before 2026-10-19 22:00 UTC are no "
                          "longer in it.", output.getvalue())

    def test_each_kind_of_refusal_is_logged_with_what_the_request_saw_and_its_message_names_the_limit(self):
        # The log recorded no refusal, so an audit rebuilt why requests waited from the holders logged, and a
        # waiting message named a threshold but not which limit it was, or no value at all (#49).
        os.environ["HEAVY_BUSY_LIMIT"] = "40"
        guard = Path(os.environ["QUEUEKEEPER_GATE_LOCK_FILE"] + ".warm-push-guard")
        queue, born = resources.slot_directory() / "exclusive-queue.json", resources.process_start(os.getpid())

        def hold(*slots):
            """Slots held as a caller of the old slot API holds them, outside the guard, for one case."""
            def setup(stack):
                directory = resources.slot_directory()
                directory.mkdir(parents=True, exist_ok=True)
                for slot in slots:
                    _slot, fd = resources.take_slot(directory, slot + 1, start=slot)
                    os.write(fd, b"other holder\n")
                    stack.callback(os.close, fd)
            return setup

        def holding(label, **options):
            """An admission held here, which a request from this process then makes without inheriting it. It
            ends released whatever the case does, so that a failed case leaves no uncertain entry for the next."""
            def setup(stack):
                admission = resources.Admission(label, timeout=0, **options).__enter__()
                stack.callback(admission.__exit__, None, None, None)
                stack.enter_context(patch.dict(os.environ))
                os.environ.pop(resources.TOKEN)
            return setup

        def write(path, value):
            def setup(stack):
                path.parent.mkdir(parents=True, exist_ok=True)
                contents = value() if callable(value) else value
                path.write_text(contents if isinstance(contents, str) else json.dumps(contents))
                stack.callback(path.unlink, missing_ok=True)
            return setup

        listed = "; `python3 scripts/qk admissions recover` lists it"
        recover = " and requires inspection" + listed
        busy = "all 2 guaranteed slots are busy and "
        # Each kind, what refuses the request with it, and the waiting message it prints, word for word: the limit that
        # refused it and the limit's value, beside what the attempt read. A busy share or level of None cannot be read.
        cases = [
            ("cpu", [hold(0, 1)], {}, 90.0, 1, {}, 0, busy + f"the CPU is 90% busy, at or above {mine('HEAVY_BUSY_LIMIT=40%')}"),
            ("cpu", [hold(0, 1)], {}, None, 1, {}, 0,
             busy + f"the CPU's busy share, which {mine('HEAVY_BUSY_LIMIT=40%')} bounds, is unavailable"),
            ("memory", [hold(0, 1)], {}, 10.0, 2, {}, 0, busy + "memory pressure is level 2 (warning), not 1 (normal)"),
            ("memory", [hold(0, 1)], {}, 10.0, 4, {}, 0, busy + "memory pressure is level 4 (critical), not 1 (normal)"),
            ("memory", [hold(0, 1)], {}, 10.0, None, {}, 0, busy + "memory pressure, which must be level 1 (normal), cannot be read"),
            ("settle", [hold(0, 1)], {}, 10.0, 1, {"HEAVY_SETTLE_SECONDS": "3600"}, 0,
             busy + "the CPU does not yet show the job admitted {seconds}s ago, within HEAVY_SETTLE_SECONDS=3600"),
            ("slots", [hold(0, 1, 2)], {}, 10.0, 1, {}, 0, busy + f"so is every slot up to {mine('HEAVY_SLOTS_MAX=3')}"),
            ("slots", [hold(0, 1)], {}, 10.0, 1, {"HEAVY_SLOTS_MAX": "2"}, 0,
             busy + f"{mine('HEAVY_SLOTS_MAX=2')} allows none beyond them"),
            ("slots", [hold(0)], {"exclusive": True}, 10.0, 1, {}, 0,
             f"exclusive work needs every slot up to {mine('HEAVY_SLOTS_MAX=3')} free, and 1 is busy"),
            ("slots", [hold(0, 2)], {"exclusive": True}, 10.0, 1, {}, 0,
             f"exclusive work needs every slot up to {mine('HEAVY_SLOTS_MAX=3')} free, and 2 are busy"),
            ("slots", [hold(0)], {"whole_host": True}, 10.0, 1, {}, 0,
             "a whole-host lane needs every guaranteed slot (HEAVY_SLOTS=2) free, and 1 is busy"),
            ("slots", [hold(0)], {"whole_host": True}, 40.0, 1, {}, 0,
             "a whole-host lane needs every guaranteed slot (HEAVY_SLOTS=2) free, and 1 is busy"),
            ("slots", [hold(0)], {"whole_host": True}, None, 1, {}, 0,
             "a whole-host lane needs every guaranteed slot (HEAVY_SLOTS=2) free, and 1 is busy"),
            ("slots", [holding("whole", whole_host=True)], {"whole_host": True}, 10.0, 1, {}, 1,
             "a whole-host lane needs every guaranteed slot (HEAVY_SLOTS=2) free, and 2 are busy"),
            ("slots", [write(queue, lambda: {"vocaroEngineering": 1, "waiters": [{"identity": "ahead", "pid": os.getpid(),
             "start": born, "label": "tests", "ordinary": True, "seen": time.time()}]})], {"whole_host": True}, 10.0, 1, {}, 0,
             "it waits behind ordinary work queued ahead of it: tests"),
            ("wholeHost", [write(queue, lambda: {"vocaroEngineering": 1, "waiters": [{"identity": "ahead",
             "pid": os.getpid(), "start": born, "label": "whole", "wholeHost": True, "seen": time.time()}]})],
             {"whole_host": True}, 10.0, 1, {}, 0, "it waits behind a whole-host lane queued ahead of it: whole"),
            # A stranded entry recovery leaves is counted as the slot it held, here the lowest, since no slot file names
            # it, so that only work beyond the free slots waits (#89).
            ("stranded", [write(guard, {"vocaroEngineering": 1, "owners": {"stuck": {"pid": os.getpid(), "start": born,
             "label": "stuck", "exclusive": False, "uncertain": True}}}), hold(1, 2)], {}, 10.0, 1, {}, 1,
             busy + f"so is every slot up to {mine('HEAVY_SLOTS_MAX=3')}; the guard entry of stuck, PID {os.getpid()}, is uncertain "
             "and requires inspection (its owner still runs), and is counted as slot 0" + listed),
            # One whose owner's PID another process now has, main's dead case, is no longer counted: admission frees it
            # at once (vocaro/queuekeeper#23; test_a_dead_entry_is_freed_at_once_and_counted_in_no_refusal). An uncertain
            # one whose owner still runs is.
            ("stranded", [write(guard, {"vocaroEngineering": 1, "owners": {"stuck": {"pid": os.getpid(), "start": born,
             "label": "stuck", "exclusive": False, "uncertain": True}}})], {"exclusive": True}, 10.0, 1, {}, 1,
             f"exclusive work needs every slot up to {mine('HEAVY_SLOTS_MAX=3')} free, and 1 is busy; the guard entry of stuck, PID "
             f"{os.getpid()}, is uncertain and requires inspection (its owner still runs), and is counted as slot 0" + listed),
            ("guard", [write(guard, {"vocaroEngineering": 2, "owners": {}})], {}, 10.0, 1, {}, None,
             f"the guard {guard} is unreadable or not in admission's format" + recover),
            ("guard", [write(guard, "{")], {}, 10.0, 1, {}, None,
             f"the guard {guard} is unreadable or not in admission's format" + recover),
            ("exclusive", [write(queue, {"vocaroEngineering": 1, "waiters": [{"identity": "ahead", "pid": os.getpid(),
             "start": born, "label": "bench"}]})], {}, 10.0, 1, {}, 0, "it waits behind exclusive work queued ahead of it: bench"),
            # Exclusive work that holds the host refuses by its slots and the model, and the message names that work.
            ("exclusive", [holding("bench", exclusive=True)], {}, 10.0, 1, {}, 1,
             "it waits behind exclusive work holding the host: bench"),
            ("exclusive", [holding("bench", exclusive=True)], {"model": True}, 10.0, 1, {}, 1,
             "it waits behind exclusive work holding the host: bench"),
            # Another model run is not exclusive work.
            ("model", [holding("generate", model=True)], {"model": True}, 10.0, 1, {}, 1,
             "the on-device model, one run at a time, is held by generate")]
        for kind, setups, options, busy_read, level, variables, holders, message in cases:
            with self.subTest(message), contextlib.ExitStack() as stack:
                stack.enter_context(patch.dict(os.environ, {"HEAVY_SETTLE_SECONDS": "0", **variables}))
                # The load is logged beside the busy share, though it no longer refuses.
                stack.enter_context(patch.object(resources.os, "getloadavg", return_value=(3.0, 3.0, 3.0)))
                stack.enter_context(patch.object(resources, "cpu_busy",
                                                 return_value=(None, None) if busy_read is None else (busy_read, 15.0)))
                stack.enter_context(patch.object(resources, "memory_level", return_value=level))
                # On a loaded Mac one attempt took over a second, and the wait was logged as 1s, not 0s (#15). Each
                # attempt here takes two seconds by admission's clock, so the wait logged is never 0s and is not pinned.
                stack.enter_context(patch.object(resources, "time", SlowAttempts.clock))
                stack.enter_context(patch.object(resources.Admission, "_attempt", SlowAttempts.attempt))
                error = stack.enter_context(contextlib.redirect_stderr(StringIO()))
                for setup in setups: setup(stack)
                resources.log_path().unlink(missing_ok=True)
                admission = resources.Admission("asked", timeout=0, **options)
                with self.assertRaises(TimeoutError) as raised:
                    with admission: self.fail("admitted")
                # The seconds since the last admission, and the wait, are the parts a slow host may change.
                pattern = re.escape(message).replace(re.escape("{seconds}"), r"\d+")
                self.assertRegex(error.getvalue(), rf"heavy: waiting \([1-9]\d*s\): {pattern}\n")
                if message.startswith("a whole-host lane needs") and holders:
                    self.assertRegex(str(raised.exception), rf"^Resource queue busy on \S+: {pattern}; held by "
                                     r"whole \(slot0, slot1\); waited \d+s; nothing ran$")
                elif kind in ("exclusive", "wholeHost", "model") or message.startswith("it waits behind ordinary"):
                    # Work known to be running or queued only made it wait (#270).
                    self.assertRegex(str(raised.exception), rf"^Resource queue busy on \S+: {pattern}; waited \d+s; nothing ran$")
                    self.assertNotIn("orphan", str(raised.exception))
                else:
                    # Here the slots are held outside the guard, or the guard or an entry requires inspection.
                    self.assertRegex(str(raised.exception), rf"\({pattern}\); no lock was reclaimed.*; waited \d+s on \S+; nothing ran$")
                    self.assertIn("an orphan requires inspection", str(raised.exception))
                (line,) = [line for line in self.lines() if line["identity"] == admission.identity]
                self.assertEqual((line["event"], line["holders"], line["load"], line["busy"]),
                                 ("withdrawn", holders, 3.0, busy_read))
                self.assertEqual(line["wholeHost"], options.get("whole_host", False))
                blocker = kind
                if message.startswith("a whole-host lane needs"):
                    if holders:
                        blocker = "wholeHostHolding"
                    else:
                        blocker = {None: "guaranteedSlotsCpuUnknown", 10.0: "guaranteedSlotsCpuUnder",
                                   40.0: "guaranteedSlotsCpuAtLimit"}[busy_read]
                elif message.startswith("it waits behind ordinary"):
                    blocker = "ordinaryAhead"
                elif message.startswith("it waits behind a whole-host"):
                    blocker = "wholeHostAhead"
                elif message.startswith("it waits behind exclusive"):
                    blocker = "exclusiveHolding" if holders else "exclusiveAhead"
                self.assertEqual(line["wait"], {"refusals": {kind: 1}, "first": kind, "last": kind,
                                                "blockers": {blocker: 1},
                                                "load": {"last": 3.0, "max": 3.0},
                                                "busy": {"last": busy_read, "max": busy_read}, "memory": level})

    def test_a_dead_entry_is_freed_at_once_and_counted_in_no_refusal(self):
        # The dead entry the refusal table counted until #23, its owner's PID now another process's (this one's) and
        # with no process record, is freed by the next attempt instead, which nothing signals, and the exclusive
        # request it refused is admitted (the owner's decision, 2026-11-13, vocaro/queuekeeper#23).
        guard = Path(os.environ["QUEUEKEEPER_GATE_LOCK_FILE"] + ".warm-push-guard")
        guard.parent.mkdir(parents=True, exist_ok=True)
        guard.write_text(json.dumps({"vocaroEngineering": 1, "owners": {"ended": {
            "pid": os.getpid(), "start": "Thu Jan  1 00:00:00 1970", "label": "ended", "exclusive": False}}}))
        directory = resources.slot_directory()
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "slot0").write_text(f"{os.getpid()} ended ended\n")
        with contextlib.redirect_stderr(StringIO()) as error, \
                resources.Admission("asked", exclusive=True, timeout=0) as admitted:
            self.assertEqual(list(json.loads(guard.read_text())["owners"]), [admitted.identity])
            self.assertEqual(admitted.counted, [])
        self.assertIn(f"heavy: reclaimed ended (PID {os.getpid()}), slot 0: its process has ended (its PID "
                      f"{os.getpid()} runs another process, started ", error.getvalue())
        (reclaimed,) = [line for line in self.lines() if line["event"] == "reclaimed"]
        self.assertEqual((reclaimed["identity"], reclaimed["process"], reclaimed["slots"], reclaimed["signaled"]),
                         ("ended", "ended", [0], []))
        (logged,) = [line for line in self.lines() if line["identity"] == admitted.identity and line["event"] == "admitted"]
        self.assertEqual((logged["reclaimed"], "stranded" in logged, "wait" in logged), (["ended"], False, False))

    def test_a_give_up_says_by_its_cause_which_mac_what_held_the_slots_the_wait_and_that_nothing_ran(self):
        # On 2026-11-07 a command behind running whole-host gates gave up with a message that suggested a broken lock (#270).
        from queuekeeper import offload_host
        os.environ.update({"HEAVY_BUSY_LIMIT": "40", "HEAVY_SETTLE_SECONDS": "0", "HEAVY_POLL_SECONDS": "0.05"})
        refused = lambda timeout=0, **options: resources.Admission("asked", timeout=timeout, **options)

        def gave_up(admission):
            with self.assertRaises(TimeoutError) as raised:
                with admission: self.fail("admitted")
            # A loaded host may take a second over an attempt; the wait itself is checked below.
            return re.sub(r"waited \d+s", "waited Ns", str(raised.exception))

        def held(stack, label, **options):
            admission = stack.enter_context(resources.Admission(label, timeout=0, **options))
            stack.enter_context(patch.dict(os.environ))
            os.environ.pop(resources.TOKEN)
            return admission

        def outside(stack, slot):
            """A slot held as a caller of the old slot API holds it, outside the guard."""
            _slot, fd = resources.take_slot(resources.slot_directory(), slot + 1, start=slot)
            os.write(fd, b"other holder\n")
            stack.callback(os.close, fd)

        limit = ("all 2 guaranteed slots are busy and so is every slot up to HEAVY_SLOTS_MAX=3 (Mac-A's limit, set in the "
                 "environment)")
        with patch.object(offload_host, "mac_name", return_value="Mac-A"), \
                patch.object(resources, "cpu_busy", return_value=(10.0, 15.0)), \
                patch.object(resources, "memory_level", return_value=1), contextlib.redirect_stderr(StringIO()):
            # Running admissions in every slot: an ordinary wait, naming each and its slots.
            with self.subTest("running admissions"), contextlib.ExitStack() as stack:
                for label in ("gate A", "gate B", "docs"): held(stack, label)
                said = gave_up(refused())
                self.assertEqual(said, f"Resource queue busy on Mac-A: {limit}; held by gate A (slot0), gate B (slot1), "
                                       "docs (slot2); waited Ns; nothing ran")
                # The time is the wait's own, not a constant.
                with self.assertRaisesRegex(TimeoutError, r"; waited [1-9]\d*s; nothing ran$"):
                    with refused(timeout=1): self.fail("admitted")
            # A whole-host lane holding the host, as on 2026-11-07, is named by the reason alone.
            with self.subTest("whole-host lane"), contextlib.ExitStack() as stack:
                held(stack, "BreezEPUB gate", whole_host=True)
                self.assertEqual(gave_up(refused()), "Resource queue busy on Mac-A: it waits behind a whole-host lane "
                                                     "holding the host: BreezEPUB gate; waited Ns; nothing ran")
            # The model's holder, running, is named once.
            with self.subTest("model held by a running admission"), contextlib.ExitStack() as stack:
                held(stack, "generate", model=True)
                self.assertEqual(gave_up(refused(model=True)), "Resource queue busy on Mac-A: the on-device model, one "
                                                               "run at a time, is held by generate; waited Ns; nothing ran")
            # The negative controls: the same limit with a lock no running admission vouches for keeps the orphan wording.
            orphan = (f"Resource queue busy or an orphan requires inspection ({limit}); no lock was reclaimed; "
                      "busy with no running admission named: ")
            with self.subTest("every slot unverified"), contextlib.ExitStack() as stack:
                for slot in range(3): outside(stack, slot)
                self.assertEqual(gave_up(refused()), orphan + "slot0, slot1, slot2; waited Ns on Mac-A; nothing ran")
            with self.subTest("one slot unverified beside running admissions"), contextlib.ExitStack() as stack:
                for label in ("gate A", "gate B"): held(stack, label)
                outside(stack, 2)
                self.assertEqual(gave_up(refused()), orphan + "slot2; waited Ns on Mac-A; nothing ran")
            # A slot file naming an entry that is gone from the guard vouches for nothing.
            with self.subTest("a slot naming an admission the guard no longer holds"), contextlib.ExitStack() as stack:
                for label in ("gate A", "gate B", "docs"): held(stack, label)
                guard = Path(os.environ["QUEUEKEEPER_GATE_LOCK_FILE"] + ".warm-push-guard")
                written = guard.read_text()
                stack.callback(guard.write_text, written)
                recorded = json.loads(written)
                del recorded["owners"][next(identity for identity, owner in recorded["owners"].items()
                                            if owner["label"] == "docs")]
                guard.write_text(json.dumps(recorded))
                self.assertEqual(gave_up(refused()), orphan + "slot2; waited Ns on Mac-A; nothing ran")
            with self.subTest("model held outside the guard"), contextlib.ExitStack() as stack:
                fd = os.open(resources.slot_directory() / "model", os.O_RDWR | os.O_CREAT, 0o644)
                stack.callback(os.close, fd)
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                os.ftruncate(fd, 0)
                self.assertEqual(gave_up(refused(model=True)),
                                 "Resource queue busy or an orphan requires inspection (the on-device model, one run "
                                 "at a time, is held by an unnamed holder); no lock was reclaimed; busy with no running "
                                 "admission named: model; waited Ns on Mac-A; nothing ran")

    def test_a_give_up_whose_locks_cannot_be_read_still_fails_as_a_timeout(self):
        # `qk run --if-free` tells a busy host by the TimeoutError (#270).
        self.hold(0, 1, 2)
        with patch.object(resources.Admission, "_holding", side_effect=PermissionError("no entry")), \
                contextlib.redirect_stderr(StringIO()), \
                self.assertRaisesRegex(TimeoutError, r"orphan requires inspection .*the locks, unread: no entry; waited \d+s"):
            with resources.Admission("refused", timeout=0): self.fail("admitted")

    def test_a_request_admitted_at_once_logs_no_wait_and_one_that_waited_logs_each_refusal_in_order(self):
        os.environ.update({"HEAVY_BUSY_LIMIT": "40", "HEAVY_SETTLE_SECONDS": "0"})
        conditions = {"busy": 10.0, "level": 1}
        real, attempts = resources.Admission._attempt, []
        # A busy CPU refuses the first attempt and memory pressure the next two; the fourth is admitted.
        sequence = [(90.0, 1), (20.0, 2), (30.0, 4), (15.0, 1)]
        def attempt(admission):
            conditions["busy"], conditions["level"] = sequence[len(attempts)]
            attempts.append(admission.label)
            return real(admission)
        base = {"time", "event", "identity", "pid", "kind", "label", "slots", "waited"}
        with patch.object(resources.os, "getloadavg", return_value=(2.0, 2.0, 2.0)), \
                patch.object(resources, "cpu_busy", side_effect=lambda *args, **kwargs: (conditions["busy"], 15.0)), \
                patch.object(resources, "memory_level", side_effect=lambda: conditions["level"]), \
                contextlib.redirect_stderr(StringIO()), resources.Admission("beside", timeout=0), patch.dict(os.environ):
            os.environ.pop(resources.TOKEN)
            with resources.Admission("at once", timeout=0) as once: pass
            self.hold(1)
            with patch.object(resources.Admission, "_attempt", attempt), resources.Admission("waited", timeout=stretched(30)) as waited:
                self.assertEqual([slot for slot, _fd in waited.descriptors], [2])
        lines = {line["identity"]: line for line in self.lines() if line["event"] == "admitted"}
        # Admitted at once: only the holders, the load and the busy share it saw.
        self.assertEqual(set(lines[once.identity]), base | {"holders", "load", "busy"})
        self.assertEqual((lines[once.identity]["holders"], lines[once.identity]["load"], lines[once.identity]["busy"]),
                         (1, 2.0, 10.0))
        self.assertEqual(attempts, ["waited"] * 4)
        line = lines[waited.identity]
        self.assertEqual(set(line), base | {"holders", "load", "busy", "wait"})
        self.assertEqual((line["holders"], line["load"], line["busy"]), (1, 2.0, 90.0))
        self.assertEqual(line["wait"], {"refusals": {"cpu": 1, "memory": 2}, "first": "cpu", "last": "memory",
                                        "blockers": {"cpu": 1, "memory": 2},
                                        "load": {"last": 2.0, "max": 2.0}, "busy": {"last": 15.0, "max": 90.0},
                                        "memory": 4})

    def test_lines_an_older_toolkit_wrote_still_read_and_a_wait_that_cannot_be_read_does_not(self):
        old = {"time": "2026-10-27T18:39:00+00:00", "event": "admitted", "identity": "a" * 32, "pid": 1, "kind": "heavy",
               "label": "EPUBLib gate", "slots": [0], "waited": 12.5}
        new = {**old, "holders": 3, "load": 412.3, "wait": {"refusals": {"load": 9}, "first": "load", "last": "load",
                                                             "load": {"last": 400.0, "max": 412.3}, "memory": 1}}
        for line in [old, {**old, "event": "withdrawn", "outcome": "timed-out"}, new,
                     {**new, "load": None, "wait": {**new["wait"], "load": {"last": None, "max": None}, "memory": None}}]:
            with self.subTest(line):
                entry = resources.read_entry(json.dumps(line))
                self.assertEqual({**entry, "time": entry["time"].isoformat()}, line)
        for broken in [{"load": "412.3"}, {"wait": []}, {"wait": {**new["wait"], "refusals": {}}},
                       {"wait": {**new["wait"], "refusals": {"load": "9"}}}, {"wait": {**new["wait"], "refusals": {"load": 0}}},
                       {"wait": {**new["wait"], "load": None}}, {"wait": {**new["wait"], "load": {"max": "high"}}}]:
            with self.subTest(broken):
                self.assertIsNone(resources.read_entry(json.dumps({**new, **broken})))

    def test_the_summary_counts_the_kinds_that_held_requests_back_and_the_loads_they_saw(self):
        def line(identity, event, kind, label, hour, **fields):
            return json.dumps({"time": f"2026-10-28T{hour:02d}:00:00+00:00", "event": event, "identity": identity,
                               "pid": 1, "kind": kind, "label": label, **fields})
        def wait(refusals, last, peak, memory=1):
            return {"refusals": refusals, "first": next(iter(refusals)), "last": list(refusals)[-1],
                    "load": {"last": last, "max": peak}, "memory": memory}
        lines = [
            # The load refused it twice as often as the slots did, so two thirds of its wait was behind the load.
            line("h1", "admitted", "heavy", "EPUBLib gate", 1, waited=300, holders=3, load=400.0,
                 wait=wait({"load": 200, "slots": 100}, 10.0, 858.0)),
            line("h1", "ended", "heavy", "EPUBLib gate", 2, outcome="released", held=600),
            # Admitted at once: its load counts, and it waited behind nothing.
            line("h2", "admitted", "heavy", "EPUBLib gate", 3, waited=0.01, holders=1, load=5.0),
            line("h2", "ended", "heavy", "EPUBLib gate", 3, outcome="released", held=60),
            line("h3", "withdrawn", "heavy", "tests", 4, outcome="timed-out", waited=6, holders=3, load=700.0,
                 wait=wait({"slots": 5}, 650.0, 752.0, 2)),
            # An older toolkit's lines: beside lines that say what a request saw, unrecorded.
            line("h4", "admitted", "heavy", "tests", 5, waited=120),
            line("h4", "ended", "heavy", "tests", 6, outcome="released", held=30),
            # A label with only an older toolkit's lines is summarized as before.
            line("h5", "admitted", "heavy", "old", 7, waited=0),
            line("h5", "ended", "heavy", "old", 7, outcome="released", held=10),
            # No load could be read while it waited.
            line("m1", "admitted", "model", "generate", 8, waited=60, holders=2, load=3.0,
                 wait=wait({"model": 60}, None, None, None)),
            line("m1", "ended", "model", "generate", 9, outcome="released", held=90),
            line("x1", "admitted", "exclusive", "bench", 10, waited=90, holders=1, load=2.0,
                 wait=wait({"legacy": 30, "guard": 60}, 2.0, 2.5)),
            line("x1", "ended", "exclusive", "bench", 11, outcome="released", held=1200)]
        entries = [resources.read_entry(text) for text in lines]
        self.assertNotIn(None, entries)
        heavy = ("4 admitted, 1 withdrawn (timed-out 1); waited mean 1m25s, max 5m00s; waited behind slots 2 (1m46s), "
                 "load 1 (3m20s), unrecorded 2 (2m00s); load when asked mean 368.3, max 700.0, while waiting max 858.0; "
                 "held mean 2m55s, max 10m00s, total 11m40s; ends released 4")
        model = ("1 admitted; waited mean 1m00s, max 1m00s; waited behind model 1 (1m00s); load when asked mean 3.0, max 3.0; "
                 "held mean 1m30s, max 1m30s, total 1m30s; ends released 1")
        exclusive = ("1 admitted; waited mean 1m30s, max 1m30s; waited behind guard 1 (1m00s), legacy 1 (30s); load when "
                     "asked mean 2.0, max 2.0, while waiting max 2.5; held mean 20m00s, max 20m00s, total 20m00s; ends released 1")
        self.assertEqual(resources.summarize(entries), [
            f"exclusive: {exclusive}", f"  bench: {exclusive}", f"model: {model}", f"  generate: {model}", f"heavy: {heavy}",
            "  EPUBLib gate: 2 admitted; waited mean 2m30s, max 5m00s; waited behind load 1 (3m20s), slots 1 (1m40s); load "
            "when asked mean 202.5, max 400.0, while waiting max 858.0; held mean 5m30s, max 10m00s, total 11m00s; ends released 2",
            "  tests: 1 admitted, 1 withdrawn (timed-out 1); waited mean 1m03s, max 2m00s; waited behind slots 1 (6s), "
            "unrecorded 1 (2m00s); load when asked mean 700.0, max 700.0, while waiting max 752.0; held mean 30s, max 30s, "
            "total 30s; ends released 1",
            "  old: 1 admitted; waited mean 0s, max 0s; held mean 10s, max 10s, total 10s; ends released 1"])
        # Without the lines that say what a request saw, the summary is what an older toolkit's log gave.
        self.assertTrue(all("waited behind" not in text and "load when asked" not in text
                            for text in resources.summarize([entries[index] for index in [5, 6, 7, 8]])))

    def test_eng_admissions_counts_the_legacy_refusals_an_older_toolkit_logged(self):
        # Admission once also waited while a gate held StudyWright's former host lock, and logged those refusals as
        # legacy (#25). No attempt refuses with that kind now, and lines that record it are read and counted.
        identity = "a" * 32
        common = {"identity": identity, "pid": 1, "kind": "heavy", "label": "EPUBLib gate", "slots": [0]}
        log = Path(self.temp.name) / "copy.jsonl"
        log.write_text(json.dumps({"time": "2026-10-28T10:00:00+00:00", "event": "admitted", **common, "waited": 40,
                                   "holders": 1, "load": 2.0, "wait": {"refusals": {"legacy": 3, "slots": 1},
                                   "first": "legacy", "last": "slots", "load": {"last": 2.0, "max": 3.0}, "memory": 1}})
                       + "\n" + json.dumps({"time": "2026-10-28T10:05:00+00:00", "event": "ended", **common,
                                            "outcome": "released", "held": 300}) + "\n")
        with contextlib.redirect_stdout(StringIO()) as output:
            self.assertEqual(cli.main(["admissions", str(log)]), 0)
        summary = ("1 admitted; waited mean 40s, max 40s; waited behind legacy 1 (30s), slots 1 (10s); load when asked "
                   "mean 2.0, max 2.0, while waiting max 3.0; held mean 5m00s, max 5m00s, total 5m00s; ends released 1")
        self.assertEqual(output.getvalue().splitlines()[1:], ["Requests made at any time the log covers:",
                                                              f"heavy: {summary}", f"  EPUBLib gate: {summary}"])

    def test_admissions_wait_breakdown_reports_scope_and_nested_blockers_without_guessing_historical_data(self):
        log = Path(self.temp.name) / "copy.jsonl"
        common = {"time": "2026-11-20T10:00:00+00:00", "kind": "heavy", "label": "gate",
                  "pid": 1, "holders": 1, "event": "withdrawn", "outcome": "timed-out"}
        historical = {"refusals": {"slots": 1}, "load": {"max": None}}
        measured = {"refusals": {"slots": 4, "cpu": 1}, "load": {"max": None},
                    "blockers": {"ordinaryAhead": 1, "guaranteedSlotsCpuAtLimit": 2,
                                 "wholeHostHolding": 1, "cpu": 1}}
        lines = [{**common, "identity": "older", "waited": 60, "wait": historical},
                 {**common, "identity": "whole", "waited": 100, "wholeHost": True, "wait": measured},
                 {**common, "identity": "ordinary", "waited": 20, "wholeHost": False, "wait": historical}]
        log.write_text("".join(json.dumps(line) + "\n" for line in lines))
        with contextlib.redirect_stdout(StringIO()) as output:
            self.assertEqual(cli.main(["admissions", "--wait-breakdown", str(log)]), 0)
        self.assertIn("Wait time by blocker is estimated", output.getvalue())
        self.assertIn("whole-host: 0 admitted, 1 withdrawn (timed-out 1); waited mean 1m40s, max 1m40s; "
                      "waited behind cpu 1 (20s), guaranteed slots busy (CPU at or over limit) 1 (40s), "
                      "one-slot work queued ahead 1 (20s), whole-host work holding the Mac 1 (20s)", output.getvalue())
        self.assertIn("one-slot: 0 admitted, 1 withdrawn (timed-out 1); waited mean 20s, max 20s; "
                      "waited behind slots (detail unrecorded) 1 (20s)", output.getvalue())
        self.assertIn("scope unrecorded: 0 admitted, 1 withdrawn (timed-out 1); waited mean 1m00s, max 1m00s; "
                      "waited behind slots (detail unrecorded) 1 (1m00s)", output.getvalue())
        groups = dict(line.split(": ", 1) for line in output.getvalue().splitlines()[3:])
        self.assertIn("detailed blockers for 1 of 1 requests with refusals", groups["whole-host"])
        self.assertIn("detailed blockers for 0 of 1 requests with refusals", groups["scope unrecorded"])

    def test_admitted_wait_percentiles_exclude_withdrawals_and_report_detailed_blocker_coverage(self):
        common = {"time": "2026-11-20T10:00:00+00:00", "event": "admitted", "kind": "heavy", "pid": 1,
                  "label": "tests", "holders": 1}
        wait = {"refusals": {"slots": 2}, "load": {"max": None}}
        lines = [{**common, "identity": str(seconds), "waited": seconds} for seconds in (0, 2, 5, 10)]
        lines[1]["wait"] = {**wait, "blockers": {"ordinaryAhead": 2}}
        lines[2]["wait"] = {**wait, "blockers": {"ordinaryAhead": 1}}
        lines.append({**common, "identity": "withdrawal", "event": "withdrawn", "outcome": "timed-out",
                      "wholeHost": False, "waited": 100, "wait": wait})
        entries = [resources.read_entry(json.dumps(line)) for line in lines]

        summary, = resources.summarize(entries, wait_breakdown=True)
        self.assertIn("admitted waits p50 2s, p95 10s, max 10s", summary)
        self.assertIn("detailed blockers for 1 of 3 requests with refusals", summary)
        self.assertTrue(all("p95" not in line for line in resources.summarize(entries)))


class UsageTests(unittest.TestCase):
    """qk admissions usage: whether another Mac is used and why work is on this Mac, from fake readings and logs."""
    UNTIL = datetime.fromisoformat("2026-11-06T12:00:00+00:00")
    SINCE = UNTIL - timedelta(hours=3)
    IDLE = {"slots": 3, "slotsMax": 9, "busy": [], "load": 1.0, "cpuBusy": 5.0, "busyLimit": 75.0, "wholeHost": [],
            "exclusive": [], "kept": None, "queuedExclusive": None, "memory": 1}

    def job(self, identity, start, end=None, label="Alpha gate", whole=False, placement=None, ended="ended"):
        """An admission's lines, start and end being minutes after SINCE; with no end, none is logged."""
        moment = lambda minutes: (self.SINCE + timedelta(minutes=minutes)).isoformat()
        lines = [{"time": moment(start), "event": "admitted", "identity": identity, "pid": 1, "kind": "heavy",
                  "label": label, "slots": [0], "waited": 0, "holders": 0, "load": 1.0, "busy": None,
                  **({"wholeHost": True} if whole else {}), **({"placement": placement} if placement else {})}]
        if end is not None:
            lines.append({"time": moment(end), "event": ended, "identity": identity, "pid": 1, "kind": "heavy",
                          "label": label, **({"outcome": "released", "held": (end - start) * 60} if ended == "ended" else {})})
        return [resources.read_entry(json.dumps(line)) for line in lines]

    def report(self, here, there, here_log, there_log):
        with contextlib.redirect_stdout(StringIO()) as output:
            self.assertEqual(resources.usage(("this Mac (Here)", "this Mac", here, here_log),
                                             ("build-host", "build-host", there, there_log),
                                             self.SINCE, self.UNTIL), 0)
        return output.getvalue().splitlines()

    def test_a_busy_farm_mac_and_the_work_never_offered_to_it_are_shown_apart(self):
        # The positive control, as on 2026-11-06: this Mac ran one job most of the period, a whole-host gate queuekeeper
        # never offered among them, while another Mac ran three or four.
        busy = {**self.IDLE, "busy": [0, 1, 2, 3], "cpuBusy": 97.0, "memory": 2}
        here_log = (self.job("g", 0, 150, whole=True, placement={"offered": False, "because": "--here"})
                    + self.job("a", 150, 160, label="swift build --jobs 4",
                               placement={"offered": False, "because": "qk admit runs here without --verify"})
                    + self.job("d", 170, 180, label="Beta suite", placement={"offered": True,
                               "because": "build-host is busy (CPU 97% busy, at or above its HEAVY_BUSY_LIMIT=75%)"})
                    + self.job("e", 175, 178, label="Beta suite", placement={"offered": True,
                               "because": "build-host is busy (CPU 91% busy, at or above its HEAVY_BUSY_LIMIT=75%)"})
                    + self.job("o", 10, 20, label="Gamma gate"))
        # Work placed here because this Mac had the most headroom is no decline by another Mac (vocaro/queuekeeper#211).
        chosen = [resources.placed(entry) for headroom in ("41.5", "38")
                  for entry in self.job("c", 0, 1, placement={"offered": True, "because":
                      f"this Mac has the most headroom of the Macs with spare capacity, {headroom} points under its CPU "
                      "limit: build-host 12 points under its CPU limit"})[:1]]
        self.assertEqual(set(chosen), {("chosen", "this Mac has the most headroom of the Macs with spare capacity, N "
                                                  "points under its CPU limit: build-host N points under its CPU limit")})
        self.assertEqual(resources.placed(self.job("d", 0, 1, placement={"offered": True, "because": "build-host is busy"})[0]),
                         ("declined", "build-host is busy"))
        there_log = [entry for index in range(4) for entry in self.job(f"t{index}", 0, 180 if index < 3 else 90,
                     placement={"dispatchedFrom": "Here"})]
        lines = self.report(self.IDLE, busy, here_log, there_log)
        self.assertEqual(lines[1].split(), ["this", "Mac", "(Here)", "|", "build-host"])
        rows = {line.split()[0]: line for line in lines[2:11]}
        self.assertEqual(rows["slots"].split("  ")[-1].strip(),
                         "0 of 3 guaranteed, 9 at most | 4 of 3 guaranteed, 9 at most (slots 0, 1, 2, 3)")
        self.assertIn("5% (limit 75%) | 97% (limit 75%)", rows["CPU"])
        self.assertIn("normal | warning", rows["memory"])
        # Over its CPU limit with admitted work running, that Mac takes no more (vocaro/queuekeeper#211).
        self.assertIn("spare capacity | build-host is busy (CPU 97% busy, at or above its HEAVY_BUSY_LIMIT=75%) and "
                      "admitted work runs there, so it takes no more", rows["heavy"])
        self.assertIn("spare capacity | a whole-host lane needs every guaranteed slot of build-host (HEAVY_SLOTS=3) "
                      "free, and 3 are busy", lines[9])
        self.assertIn("  this Mac (Here): 5 admitted; time at N heavy jobs: 0 6%, 1 87%, 2 7%; mean 1.0; "
                      "a whole-host lane held it 83%", lines)
        self.assertIn("  build-host: 4 admitted; time at N heavy jobs: 3 50%, 4 50%; mean 3.5; "
                      "a whole-host lane held it 0%", lines)
        placements = lines[lines.index("Why this Mac's admissions made in the period ran here:") + 1:]
        self.assertEqual(placements, [
            "  never offered to another Mac: 2, held 2h40m",
            "    qk run --here: 1, held 2h30m",
            "    qk admit without --verify: 1, held 10m00s",
            "  offered to another Mac and declined: 2, held 13m00s",
            "    build-host is busy (CPU N% busy, at or above its HEAVY_BUSY_LIMIT=N%): 2, held 13m00s",
            "  placement unrecorded (an older toolkit's line, or a caller's own admission, such as qk admit's "
            "before it logged placement): 1, held 10m00s",
            "    a lane: 1, held 10m00s",
            "      Gamma gate: held 10m00s",
            "  Light lanes take no admission and are not logged: each runs where it starts, never offered."])

    def test_usage_counts_historical_local_path_exclusions_as_never_offered(self):
        reasons = [
            "it names /tmp/task-1234, a path outside the checkout, which another Mac does not have",
            "it names ~/task-1234, under this Mac's home, which another Mac's checkout does not hold",
            "it names this checkout's path /tmp/project-1234, which another Mac's checkout is not",
        ]
        log = [entry for index, reason in enumerate(reasons)
               for entry in self.job(str(index), 0, 1, placement={"offered": True, "because": reason})]
        log += self.job("declined", 1, 2, placement={"offered": True, "because": "another Mac is busy"})
        lines = self.report(self.IDLE, self.IDLE, log, [])
        self.assertIn("  never offered to another Mac: 3, held 3m00s", lines)
        for reason in reasons:
            self.assertIn(f"    {reason}: 1, held 1m00s", lines)
        self.assertIn("  offered to another Mac and declined: 1, held 1m00s", lines)
        self.assertIn("    another Mac is busy: 1, held 1m00s", lines)

    def test_idle_macs_old_and_unended_admissions_and_unreadable_capacity_count_nothing_busy(self):
        # The negative controls: an admission wholly before the period, one with no end logged begun a day before,
        # whose process died unrecorded, and a recovered one that ended before the period count no time; nor does
        # a log with nothing in it. A Mac whose capacity cannot be read says why, never that it has room.
        here_log = (self.job("before", -120, -60) + self.job("dead", -24 * 60)
                    + self.job("recovered", -100, -30, ended="recovered"))
        lines = self.report(self.IDLE, "build-host did not report its capacity: it answered None", here_log, [])
        self.assertIn("  this Mac (Here): 0 admitted; time at N heavy jobs: 0 100%; mean 0.0; a whole-host lane held "
                      "it 0%; 1 admission(s) with no end logged, begun over 12 h before, not counted", lines)
        self.assertIn("  build-host: 0 admitted; time at N heavy jobs: 0 100%; mean 0.0; a whole-host "
                      "lane held it 0%", lines)
        self.assertIn("unavailable: build-host did not report its capacity: it answered None", lines[2])
        self.assertEqual(lines[9], "  whole-host lane  spare capacity | -")
        self.assertEqual(lines[-2:], ["  none", "  Light lanes take no admission and are not logged: each runs where "
                                      "it starts, never offered."])
        # An admission still running at the period's end, begun within it, counts until then; one the period
        # holds only part of counts only that part.
        seconds, whole, dead = resources.occupancy(self.job("now", 120) + self.job("early", -60, 30), self.SINCE, self.UNTIL)
        self.assertEqual((seconds, whole, dead), ({0: 90 * 60, 1: 90 * 60}, 0.0, 0))
        # Another Mac whose log cannot be fetched says why.
        lines = self.report(self.IDLE, self.IDLE, [], "build-host did not send its admission log")
        self.assertIn("  build-host: log unavailable: build-host did not send its admission log", lines)


class UsageSettingsTests(unittest.TestCase):
    """qk admissions usage prints what each Mac takes and its labels beside its capacity, from the reading it takes
    already, and where foreground lanes go (vocaro/queuekeeper#224)."""
    UNTIL, SINCE, IDLE, report = UsageTests.UNTIL, UsageTests.SINCE, UsageTests.IDLE, UsageTests.report

    def settings(self, file, takes=None, labels=(), ignored=()):
        return {**self.IDLE, "takes": {"wholeHost": True, "exclusive": True, "model": True, "foreground": False,
                                       **(takes or {})},
                "takesSet": sorted(takes or {}), "labels": list(labels), "settingsFile": file,
                "settingsIgnored": list(ignored)}

    def rows(self, lines):
        return {line.split("  ")[1].strip(): line for line in lines[2:] if line.startswith("  ") and " | " in line}

    def test_each_macs_kinds_labels_and_settings_are_shown_and_where_foreground_lanes_go(self):
        here = self.settings("/here/mac-settings.json", {"wholeHost": False})
        there = self.settings("/there/mac-settings.json", {"foreground": True}, ["unity-license", "apple-m5-max"],
                              ["takes.gpuWork"])
        lines = self.report(here, there, [], [])
        rows = self.rows(lines)
        self.assertEqual(rows["takes"].split("  ", 2)[-1].strip(),
                         "exclusive work, model runs; not whole-host gates, foreground work (wholeHost set, the rest by "
                         "default) | whole-host gates, exclusive work, model runs, foreground work (foreground set, the "
                         "rest by default)")
        self.assertTrue(rows["labels"].endswith("none | unity-license, apple-m5-max"), rows["labels"])
        self.assertTrue(rows["settings"].endswith("/here/mac-settings.json | /there/mac-settings.json (ignored: "
                                                  "takes.gpuWork)"), rows["settings"])
        self.assertIn("Foreground lanes go to: build-host", lines)
        # This Mac too, after the farm's Macs, as `qk farm foreground` says; and no Mac when none takes them.
        here["takes"]["foreground"] = True
        self.assertIn("Foreground lanes go to: build-host, this Mac", self.report(here, there, [], []))
        here["takes"]["foreground"] = there["takes"]["foreground"] = False
        self.assertIn("Foreground lanes go to: no Mac", self.report(here, there, [], []))

    def test_a_mac_whose_settings_are_not_read_is_unknown_never_lacking(self):
        # A reading from an older toolkit carries no settings, and one that could not be taken says why.
        for there in (self.IDLE, "build-host did not report its capacity: it answered None"):
            with self.subTest(there=there):
                lines = self.report(self.settings("/here/mac-settings.json"), there, [], [])
                rows = self.rows(lines)
                self.assertNotIn("none", rows["labels"].split("|")[-1])
                self.assertIn("Foreground lanes go to: no Mac; unknown whether build-host takes them, whose settings "
                              "could not be read", lines)
        lines = self.report(self.settings("/here/mac-settings.json"), self.IDLE, [], [])
        self.assertTrue(self.rows(lines)["takes"].endswith("| unknown: its toolkit does not report its settings"))
        # A farm with no other Mac names none as unknown.
        with contextlib.redirect_stdout(StringIO()) as output:
            resources.usage(("this Mac (Here)", "this Mac", self.settings("/here/mac-settings.json"), []),
                            ("the farm's other Macs", "another Mac", "none", "none"), self.SINCE, self.UNTIL, farm=False)
        self.assertIn("Foreground lanes go to: no Mac\n", output.getvalue())


class RecoveryTests(unittest.TestCase):
    """qk admissions recover removes a dead or uncertain entry once a census against its process record
    finds nothing of its commands still running (#61). A push keeper ended by an agent's task stop had left
    nothing running, and the host admitted no heavy work until its entry was removed by hand (2026-10-27)."""
    setUp, tearDown = ResourceTests.setUp, ResourceTests.tearDown
    # Leaves a grandchild in its session but out of its group, orphaned before supervision first samples, as
    # a daemon a build starts between two samples is, then fails. The grandchild runs while its file does.
    ORPHAN = ("import os, sys, time\nfrom pathlib import Path\nhere = Path(sys.argv[1])\n"
              "if os.fork() == 0:\n    if os.fork() == 0:\n        os.setpgid(0, 0)\n"
              "        (here / 'orphan.tmp').write_text(str(os.getpid()))\n        (here / 'orphan.tmp').replace(here / 'orphan')\n"
              "        began = time.monotonic()\n"
              # It must outlive the test that ends it, however slowly the host runs it.
              f"        while (here / 'orphan').exists() and time.monotonic() - began < {stretched(120)}: time.sleep(0.05)\n"
              "    os._exit(0)\nos.wait()\nwhile not (here / 'orphan').exists(): time.sleep(0.01)\n"
              "(here / 'command').touch()\nraise SystemExit(5)\n")
    # The resource CLI, sampling only once its command is ready, so that what the command left first is never observed.
    UNOBSERVING = ("import sys, time\nfrom pathlib import Path\nfrom queuekeeper import processes, resources\n"
                   "here, real = Path(sys.argv[1]), processes.snapshot\n"
                   "def unobserved():\n    while not (here / 'command').exists(): time.sleep(0.01)\n    return real()\n"
                   "processes.snapshot = unobserved\nraise SystemExit(resources.main(sys.argv[2:]))\n")

    @property
    def guard(self):
        return Path(os.environ["QUEUEKEEPER_GATE_LOCK_FILE"] + ".warm-push-guard")

    def owners(self):
        return json.loads(self.guard.read_text())["owners"] if self.guard.exists() else {}

    def recover(self, *owner):
        """qk admissions recover's status and what it printed."""
        with contextlib.redirect_stdout(StringIO()) as output, contextlib.redirect_stderr(StringIO()):
            status = cli.main(["admissions", "recover", *owner])
        return status, output.getvalue()

    def until(self, condition, what, seconds=30):
        deadline = time.monotonic() + stretched(seconds)
        while not condition():
            self.assertLess(time.monotonic(), deadline, what)
            time.sleep(0.01)

    def environment(self):
        environment = {**os.environ, "PYTHONPATH": host_isolation.pythonpath()}
        environment.pop(resources.TOKEN, None)
        return environment

    def ended_process(self):
        """The PID and start of a process that has ended."""
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        start = resources.process_start(process.pid)
        process.kill()
        process.wait()
        return process.pid, start

    def stop_later(self, pid, start):
        """End this test's process at cleanup if it still runs as itself."""
        def end():
            if resources.process_start(pid) == start:
                with contextlib.suppress(ProcessLookupError): os.kill(pid, signal.SIGKILL)
        self.addCleanup(end)

    def end(self, pid, start):
        with contextlib.suppress(ProcessLookupError): os.kill(pid, signal.SIGKILL)
        self.until(lambda: resources.process_start(pid) != start, f"PID {pid} did not end")

    @staticmethod
    def recorded(record):
        """The (PID, start) pairs a process record holds so far; a line still being written is skipped."""
        found = set()
        for text in record.read_text().splitlines() if record.exists() else []:
            with contextlib.suppress(ValueError): found.update(map(tuple, json.loads(text).get("processes", [])))
        return found

    def stranded(self, record=None, live=False, **entry):
        """A guard entry whose owner has ended, with this process record: its identity. With live, an uncertain entry of
        this process, which still runs, as a stop being carried out leaves it: the only entry admission counts as
        stranded, since it frees one whose owner has ended at once (vocaro/queuekeeper#23)."""
        identity = uuid.uuid4().hex
        pid, start = (os.getpid(), resources.process_start(os.getpid())) if live else self.ended_process()
        if live: entry = {"uncertain": True, **entry}
        owners = self.owners()
        owners[identity] = {"pid": pid, "start": start, "label": "stranded", "exclusive": False, "model": False,
                            "admitted": "2026-10-27T10:00:00+00:00", **entry}
        resources.publish_guard(self.guard, {"vocaroEngineering": 1, "owners": owners})
        if record is not None:
            resources.record_path(self.guard, identity).write_text("".join(json.dumps(line) + "\n" for line in record))
        return identity

    def test_a_dead_owner_whose_recorded_processes_have_ended_is_removed_alone_and_the_next_admission_proceeds(self):
        marker = Path(self.temp.name) / "command.pids"
        # The command has a child in its own session, so that the census covers both.
        command = [sys.executable, "-c", "import os, pathlib, subprocess, sys, time\n"
                   "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'], start_new_session=True)\n"
                   f"pathlib.Path({str(marker)!r}).write_text(f'{{os.getpid()}} {{child.pid}}')\ntime.sleep(120)"]
        for beside in [True, False]:
            with self.subTest(beside=beside), contextlib.redirect_stderr(StringIO()), contextlib.ExitStack() as stack:
                marker.unlink(missing_ok=True)
                live = stack.enter_context(resources.Admission("beside", timeout=0)) if beside else None
                owner = subprocess.Popen([sys.executable, "-m", "queuekeeper.resources", "--", *command],
                                         env=self.environment(), stderr=subprocess.DEVNULL)
                self.addCleanup(lambda owner=owner: owner.poll() is None and (owner.kill(), owner.wait()))
                self.until(lambda: marker.exists() and len(marker.read_text().split()) == 2, "the command never started")
                pids = list(map(int, marker.read_text().split()))
                starts = {pid: resources.process_start(pid) for pid in pids}
                for pid in pids: self.stop_later(pid, starts[pid])
                identity = next(identity for identity, entry in self.owners().items() if entry["pid"] == owner.pid)
                record = resources.record_path(self.guard, identity)
                self.until(lambda: {pid for pid, _start in self.recorded(record)} >= set(pids), "the record lacks the command")
                # A task stop's SIGKILL ends the admitting process; its command runs on in its own session.
                owner.kill()
                owner.wait()
                status, output = self.recover(str(owner.pid))
                self.assertEqual(status, 1, output)
                for pid in pids: self.assertIn(f"PID {pid} (a recorded process, still running)", output)
                self.assertIn(identity, self.owners())
                for pid in pids: self.end(pid, starts[pid])
                status, output = self.recover(str(owner.pid))
                self.assertEqual(status, 0, output)
                self.assertIn("Recovered", output)
                self.assertFalse(record.exists())
                if beside:
                    # Only that entry: the live one beside it stays, and the guard with it.
                    self.assertEqual(list(self.owners()), [live.identity])
                    stack.close()
                else:
                    # The last entry takes the guard with it.
                    self.assertFalse(self.guard.exists())
                    self.assertIn("and the guard with it", output)
                with resources.Admission("next", timeout=0): pass
                entries, unreadable = resources.read_log()
                self.assertEqual(unreadable, 0)
                (recovered,) = [entry for entry in entries if entry["event"] == "recovered" and entry["identity"] == identity]
                self.assertEqual((recovered["pid"], recovered["kind"], recovered["commands"]), (owner.pid, "heavy", 1))
                self.assertGreaterEqual(recovered["processes"], 2)
                # The admission it ended counts as recovered, not as unlogged.
                summary = resources.summarize([entry for entry in entries if entry["identity"] == identity])
                self.assertTrue(summary[0].endswith("ends recovered 1"), summary)

    # A supervised owner, as qk run is, killed before its first recorded command: it has started a helper in its own
    # process group, as a lane's setup can, and records nothing of it. Each run leads a session and group of its own.
    BEFORE_FIRST = ("import subprocess, sys, time\nfrom pathlib import Path\nfrom queuekeeper import resources\n"
                    "here = Path(sys.argv[1])\n"
                    "with resources.Admission('BreezEPUB ways-in', timeout=0, supervised=True):\n"
                    "    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
                    "    (here / 'child.tmp').write_text(str(child.pid))\n"
                    "    (here / 'child.tmp').replace(here / 'child')\n"
                    "    time.sleep(120)\n")

    def killed_before_first_command(self):
        """Admit a supervised owner that starts a child before its first recorded command, then SIGKILL the owner, as
        a task stop on its shell did on 2026-11-07 (#315): the owner's PID and identity and the child's PID and start."""
        here = Path(self.temp.name)
        (here / "child").unlink(missing_ok=True)
        owner = subprocess.Popen([sys.executable, "-c", self.BEFORE_FIRST, str(here)], env=self.environment(),
                                 stderr=subprocess.DEVNULL, start_new_session=True)
        self.addCleanup(lambda: owner.poll() is None and (owner.kill(), owner.wait()))
        self.until(lambda: (here / "child").exists(), "the owner never started its child")
        child = int((here / "child").read_text())
        child_start = resources.process_start(child)
        self.stop_later(child, child_start)
        identity = next(identity for identity, entry in self.owners().items() if entry["pid"] == owner.pid)
        owner.kill()
        owner.wait()
        return owner.pid, identity, child, child_start

    def test_admission_with_no_opening_record_does_not_publish_a_guard_or_hold_slots(self):
        admission = resources.Admission("opening write fails", whole_host=True, timeout=0, supervised=True)
        publish = resources.publish_guard

        def fail_record(path, state):
            if path == admission.process_record:
                raise OSError("no space for the opening record")
            return publish(path, state)

        with patch.object(resources, "publish_guard", side_effect=fail_record):
            with self.assertRaisesRegex(RuntimeError, "opening process record; nothing ran"):
                with admission:
                    self.fail("admitted without an opening record")
        self.assertFalse(self.guard.exists())
        self.assertFalse(admission.process_record.exists())
        with resources.Admission("next gate", whole_host=True, timeout=0) as gate:
            self.assertEqual(list(self.owners()), [gate.identity])

    def test_the_cli_reports_a_failed_opening_record_as_no_admission(self):
        failure = resources.AdmissionRecordError("Cannot write the admission's opening process record; nothing ran")
        with contextlib.ExitStack() as stack, contextlib.redirect_stderr(StringIO()) as error, \
                patch.object(resources.Admission, "_open_record", side_effect=failure):
            self.assertEqual(cli.admitted_here(stack, "gate", "gate", 0, whole_host=True), resources.BUSY)
        self.assertIn(str(failure), error.getvalue())
        self.assertFalse(self.guard.exists())

    def test_an_interrupt_at_guard_publication_leaves_the_opening_record_for_recovery(self):
        admission = resources.Admission("interrupted", timeout=0, supervised=True)
        publish = resources.publish_guard

        def interrupt_after_publish(path, state):
            publish(path, state)
            if path == self.guard:
                raise KeyboardInterrupt

        with patch.object(resources, "publish_guard", side_effect=interrupt_after_publish):
            with self.assertRaises(KeyboardInterrupt):
                with admission:
                    self.fail("admitted after the interrupt")
        self.assertEqual(list(self.owners()), [admission.identity])
        opened = json.loads(admission.process_record.read_text())
        self.assertEqual((opened["admission"], opened["supervised"]), (admission.identity, True))

    def test_a_truncated_opening_record_cannot_prove_its_owner_ended_without_work(self):
        identity = self.stranded([])
        status, output = self.recover(identity)
        self.assertEqual(status, 1, output)
        self.assertIn("its process record is empty", output)
        self.assertIn(identity, self.owners())

    def test_an_owner_killed_before_its_first_command_is_recovered_once_nothing_it_started_runs(self):
        # On 2026-11-07 three lanes killed between admission and their first recorded command left entries with no
        # process record, which neither admission nor `qk admissions recover` would remove, and BreezEPUB's gate
        # waited 86 min behind them (#315). Admission now writes the record, with the owner's process group, as it
        # adds the entry, and recovery checks that group for anything the owner started.
        owner, identity, child, child_start = self.killed_before_first_command()
        record = resources.record_path(self.guard, identity)
        (opened,) = [json.loads(text) for text in record.read_text().splitlines()]
        self.assertEqual((opened["admission"], opened["owner"][0], opened["group"], opened["session"], opened["supervised"]),
                         (identity, owner, owner, owner, True))
        # While its child runs, attended recovery leaves the entry.
        status, output = self.recover(str(owner))
        self.assertEqual(status, 1, output)
        self.assertIn(f"PID {child} (in its owner's process group {owner}, started since its owner)", output)
        self.assertIn(identity, self.owners())
        # Once nothing it started runs, attended recovery removes it.
        self.end(child, child_start)
        status, output = self.recover(str(owner))
        self.assertEqual(status, 0, output)
        self.assertIn(f"Recovered BreezEPUB ways-in (PID {owner}): it ended before its first command, and nothing "
                      "started since it still runs in its process group or session", output)
        self.assertFalse(record.exists())
        self.assertFalse(self.guard.exists())
        entries, _unreadable = resources.read_log()
        (recovered,) = [entry for entry in entries if entry["event"] == "recovered" and entry["identity"] == identity]
        self.assertEqual((recovered.get("automatic"), recovered["commands"]), (None, 0))
        # Admission does not wait for it: an owner that has ended is freed at once, its child still running. The child
        # is in its group, not below a process proven the owner's, so it is not signaled (vocaro/queuekeeper#23).
        owner, identity, child, child_start = self.killed_before_first_command()
        with contextlib.redirect_stderr(StringIO()) as error, resources.Admission("next", exclusive=True, timeout=0) as admitted:
            self.assertEqual(list(self.owners()), [admitted.identity])
        self.assertIn(f"heavy: reclaimed BreezEPUB ways-in (PID {owner})", error.getvalue())
        self.assertEqual(resources.process_start(child), child_start)
        entries, _unreadable = resources.read_log()
        (reclaimed,) = [entry for entry in entries if entry["event"] == "reclaimed" and entry["identity"] == identity]
        self.assertEqual((reclaimed["process"], reclaimed["signaled"]), ("ended", []))

    def test_an_owner_killed_before_its_first_command_whose_pid_was_reused_counts_as_ended(self):
        # PID 8641's entry read a start of 22:24 while an iOS simulator process started at 00:25 had its PID (#315).
        owner, identity, child, child_start = self.killed_before_first_command()
        self.end(child, child_start)
        reuser = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        self.addCleanup(lambda: reuser.poll() is None and (reuser.kill(), reuser.wait()))
        owners = self.owners()
        # Positive control: the PID with its own start is a live owner, whose entry stays.
        owners[identity].update(pid=reuser.pid, start=resources.process_start(reuser.pid))
        resources.publish_guard(self.guard, {"vocaroEngineering": 1, "owners": owners})
        status, output = self.recover(identity)
        self.assertEqual(status, 1, output)
        self.assertIn("still runs, so its entry stays", output)
        # The same PID with the dead owner's start is another process: the owner has ended.
        owners[identity]["start"] = "Thu Jan  1 00:00:00 1970"
        resources.publish_guard(self.guard, {"vocaroEngineering": 1, "owners": owners})
        status, output = self.recover(identity)
        self.assertEqual(status, 0, output)
        self.assertIn(f"Recovered BreezEPUB ways-in (PID {reuser.pid}): it ended before its first command", output)
        self.assertFalse(self.guard.exists())
        self.assertIsNone(reuser.poll())

    def test_admission_frees_a_stranded_entry_whose_owner_has_ended_and_admits_the_request(self):
        # A task stop's SIGKILL stranded entries whose commands had all ended, and every session's heavy work on
        # the Macs waited until the owner removed them by hand (2026-11-01). Admission now frees such an entry
        # itself at once, marked uncertain or not, and logs it as reclaimed (vocaro/queuekeeper#23).
        for uncertain in [False, True]:
            with self.subTest(uncertain=uncertain), contextlib.ExitStack() as stack:
                error = stack.enter_context(contextlib.redirect_stderr(StringIO()))
                live = stack.enter_context(resources.Admission("beside", timeout=0))
                stack.enter_context(patch.dict(os.environ))
                os.environ.pop(resources.TOKEN)
                identity = self.stranded([{"command": "a", "argv": ["true"]}, {"command": "a", "settled": True}],
                                         **({"uncertain": True} if uncertain else {}))
                record = resources.record_path(self.guard, identity)
                with resources.Admission("next", timeout=0) as admitted:
                    self.assertEqual(set(self.owners()), {live.identity, admitted.identity})
                self.assertFalse(record.exists())
                self.assertIn("heavy: reclaimed stranded (PID ", error.getvalue())
                entries, _unreadable = resources.read_log()
                (reclaimed,) = [entry for entry in entries if entry["event"] == "reclaimed" and entry["identity"] == identity]
                self.assertEqual((reclaimed["process"], reclaimed["uncertain"], reclaimed["by"]["identity"]),
                                 ("ended", uncertain, admitted.identity))
        # An attended recovery's line does not say automatic.
        identity = self.stranded([{"command": "a", "argv": ["true"]}, {"command": "a", "settled": True}])
        self.assertEqual(self.recover(identity)[0], 0)
        entries, _unreadable = resources.read_log()
        (recovered,) = [entry for entry in entries if entry["event"] == "recovered" and entry["identity"] == identity]
        self.assertNotIn("automatic", recovered)

    def test_admission_leaves_an_uncertain_entry_whose_owner_still_runs_and_checks_it_again_later(self):
        identity = self.stranded([{"command": "a", "argv": ["sleep"]}], live=True)
        with contextlib.redirect_stderr(StringIO()), patch.dict(os.environ, {"HEAVY_RECOVER_SECONDS": "3600"}):
            # Counted as the slot it held, so exclusive work waits behind it (#89).
            with self.assertRaisesRegex(TimeoutError, re.escape("is uncertain and requires inspection (its owner still "
                                                                "runs), and is counted as slot 0")):
                with resources.Admission("refused", exclusive=True, timeout=0): self.fail("admitted")
            self.assertIn(identity, self.owners())
            waiting = resources.Admission("waiting", exclusive=True, timeout=0)
            self.assertFalse(waiting._attempt())
            # Its owner settles the entry as ended, as one whose stop has drained but whose process lingers: within
            # HEAVY_RECOVER_SECONDS it is not recovered again, so a census runs once a minute at most.
            with patch.object(resources, "reclaim", side_effect=AssertionError("checked again")):
                self.assertFalse(waiting._attempt())
            self.assertIn(identity, self.owners())
            # Once its owner has ended, the next attempt frees it at once (vocaro/queuekeeper#23).
            owners = self.owners()
            owners[identity]["pid"], owners[identity]["start"] = self.ended_process()
            resources.publish_guard(self.guard, {"vocaroEngineering": 1, "owners": owners})
            self.assertTrue(waiting._attempt())
            self.assertEqual(list(self.owners()), [waiting.identity])
            waiting.__exit__(None, None, None)
        self.assertFalse(self.guard.exists())

    def slot_names(self, slot, identity):
        """Write a slot's file as an admission writes the slots it holds, naming its holder's identity."""
        directory = resources.slot_directory()
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"slot{slot}").write_text(f"1 {identity} stranded\n")

    def test_a_stranded_entry_counts_as_the_slots_it_held_so_only_work_beyond_them_waits(self):
        # An entry recovery left refused every session's heavy work on its host (#89). It now counts as the slots
        # it held, its own slot here, which its slot file names; work within the free slots is admitted beside it,
        # and work beyond them, exclusive work among it, waits, with the entry and its slots named.
        os.environ["HEAVY_SLOTS_MAX"] = "2"
        # An uncertain entry whose owner still runs, with no process record or with one: they count alike. One whose
        # owner has ended is freed instead (vocaro/queuekeeper#23).
        why = "its owner still runs"
        for name, record in [("no record", None), ("recorded", [{"command": "a", "argv": ["true"]}])]:
            with self.subTest(name), contextlib.ExitStack() as stack:
                error = stack.enter_context(contextlib.redirect_stderr(StringIO()))
                self.guard.unlink(missing_ok=True)
                resources.log_path().unlink(missing_ok=True)
                identity = self.stranded(record, live=True)
                self.slot_names(1, identity)
                entry = self.owners()[identity]
                beside = stack.enter_context(resources.Admission("beside", timeout=0))
                stack.enter_context(patch.dict(os.environ))
                os.environ.pop(resources.TOKEN)
                # Within the free slots: admitted beside the entry, in the slot it does not hold.
                self.assertEqual([slot for slot, _fd in beside.descriptors], [0])
                counted = (f"the guard entry of stranded, PID {entry['pid']}, is uncertain and requires inspection ({why}), "
                           "and is counted as slot 1; `python3 scripts/qk admissions recover` lists it")
                self.assertIn(f"heavy: admitted beside a stranded entry: {counted}", error.getvalue())
                # Beyond them, and exclusive work: each waits, naming the entry and its slot.
                for options, reason in [({}, f"all 2 guaranteed slots are busy and {mine('HEAVY_SLOTS_MAX=2')} allows none "
                                             "beyond them"),
                                        ({"exclusive": True},
                                         f"exclusive work needs every slot up to {mine('HEAVY_SLOTS_MAX=2')} free, and 2 are "
                                         "busy")]:
                    refused = resources.Admission("beyond", timeout=0, **options)
                    with self.assertRaisesRegex(TimeoutError, re.escape(f"({reason}; {counted}); no lock was reclaimed")):
                        with refused: self.fail("admitted")
                    self.assertRegex(error.getvalue(), r"heavy: waiting \(\d+s\): " + re.escape(f"{reason}; {counted}\n"))
                    (line,) = [line for line in self.read() if line["identity"] == refused.identity]
                    self.assertEqual(line["wait"]["refusals"], {"stranded": 1})
                    self.assertEqual(line["stranded"], [{"identity": identity, "pid": entry["pid"], "label": "stranded",
                                                         "uncertain": True, "why": why, "slots": [1], "model": False}])
                (admitted,) = [line for line in self.read() if line["identity"] == beside.identity]
                self.assertEqual(admitted["stranded"][0]["slots"], [1])
                # Never removed, nor its record: its owner still runs.
                self.assertEqual(self.owners()[identity], entry)
                self.assertEqual(resources.record_path(self.guard, identity).exists(), record is not None)
                self.assertFalse(any(line["event"] == "recovered" for line in self.read()))
        # The negative control: without the stranded entry, the same request beyond the first is admitted in its slot.
        self.guard.unlink()
        with contextlib.redirect_stderr(StringIO()), resources.Admission("first", timeout=0), patch.dict(os.environ):
            os.environ.pop(resources.TOKEN)
            with resources.Admission("second", timeout=0) as second:
                self.assertEqual([slot for slot, _fd in second.descriptors], [1])
                self.assertEqual(second.counted, [])

    def read(self):
        entries, _unreadable = resources.read_log()
        return entries

    def test_a_stranded_exclusive_or_model_entry_counts_as_every_slot_or_the_model_it_held(self):
        os.environ["HEAVY_SLOTS_MAX"] = "2"
        cases = [({"exclusive": True, "model": True}, {}, "slots 0, 1 and the model",
                  f"all 2 guaranteed slots are busy and {mine('HEAVY_SLOTS_MAX=2')} allows none beyond them"),
                 ({"model": True}, {"model": True}, "slot 0 and the model",
                  "the on-device model, one run at a time, is counted as held")]
        for entry, options, held, reason in cases:
            with self.subTest(held), contextlib.redirect_stderr(StringIO()):
                self.guard.unlink(missing_ok=True)
                identity = self.stranded(live=True, **entry)
                counted = (f"the guard entry of stranded, PID {self.owners()[identity]['pid']}, is uncertain and requires "
                           f"inspection (its owner still runs), and is counted as {held}")
                with self.assertRaisesRegex(TimeoutError, re.escape(f"({reason}; {counted}; `python3 scripts/qk")):
                    with resources.Admission("refused", timeout=0, **options): self.fail("admitted")
                self.assertIn(identity, self.owners())
        # The positive control: a stranded model entry holds back no heavy work that does not use the model.
        with contextlib.redirect_stderr(StringIO()), resources.Admission("heavy", timeout=0) as heavy:
            self.assertEqual([slot for slot, _fd in heavy.descriptors], [1])

    def test_an_exclusive_request_queued_behind_a_stranded_entry_holds_back_no_work_within_the_free_slots(self):
        # The exclusive request cannot be admitted until the entry is recovered, so it holds nothing back meanwhile.
        queue = resources.slot_directory() / "exclusive-queue.json"
        queue.parent.mkdir(parents=True, exist_ok=True)
        queued = {"vocaroEngineering": 1, "waiters": [{"identity": "ahead", "pid": os.getpid(),
                                                       "start": resources.process_start(os.getpid()), "label": "bench"}]}
        queue.write_text(json.dumps(queued))
        # The negative control: with no stranded entry, the queued request holds the work back.
        with contextlib.redirect_stderr(StringIO()), self.assertRaisesRegex(TimeoutError, "queued ahead of it: bench"):
            with resources.Admission("held back", timeout=0): self.fail("admitted")
        identity = self.stranded(live=True)
        with contextlib.redirect_stderr(StringIO()), resources.Admission("beside", timeout=0) as beside:
            self.assertEqual([slot for slot, _fd in beside.descriptors], [1])
        self.assertIn(identity, self.owners())
        self.assertEqual(json.loads(queue.read_text()), queued)

    def test_a_whole_host_request_queued_behind_a_stranded_guaranteed_slot_holds_back_no_work(self):
        # It cannot be admitted until an entry counted as a guaranteed slot is recovered, so it holds nothing back
        # meanwhile; an entry counted beyond the guaranteed slots does not stop it, so it still keeps them (#174).
        queue = resources.slot_directory() / resources.QUEUE
        queue.parent.mkdir(parents=True, exist_ok=True)
        queued = {"vocaroEngineering": 1, "waiters": [{"identity": "ahead", "pid": os.getpid(), "label": "gate",
                                                       "start": resources.process_start(os.getpid()), "wholeHost": True,
                                                       "seen": time.time() + 3600}]}
        queue.write_text(json.dumps(queued))
        # The negative control: with no stranded entry, the queued request keeps the guaranteed slots.
        with patch.dict(os.environ, {"HEAVY_SLOTS_MAX": "2"}), contextlib.redirect_stderr(StringIO()), \
                self.assertRaisesRegex(TimeoutError, "kept for a whole-host lane queued ahead of it: gate"):
            with resources.Admission("held back", timeout=0): self.fail("admitted")
        beyond = self.stranded(live=True)
        (resources.slot_directory() / "slot2").write_text(f"1 {beyond} stranded\n")
        with contextlib.redirect_stderr(StringIO()), \
                self.assertRaisesRegex(TimeoutError, "kept for a whole-host lane queued ahead of it: gate; and .*"
                                                     "is counted as slot 2"):
            with resources.Admission("still held back", timeout=0): self.fail("admitted")
        guaranteed = self.stranded(live=True)
        with contextlib.redirect_stderr(StringIO()), resources.Admission("beside", timeout=0) as beside:
            self.assertEqual([slot for slot, _fd in beside.descriptors], [1])
        self.assertEqual({beyond, guaranteed} - set(self.owners()), set())
        self.assertEqual(json.loads(queue.read_text()), queued)

    def test_an_uncertain_entry_stays_while_a_process_runs_in_a_recorded_session_and_goes_once_it_ends(self):
        here = Path(self.temp.name)
        # To a file: the orphan keeps what it inherited open, so a pipe would not close until it ends.
        with (here / "owner.log").open("w") as log:
            subprocess.run([sys.executable, "-c", self.UNOBSERVING, str(here), "--", sys.executable, "-c", self.ORPHAN,
                            str(here)], env=self.environment(), stdout=subprocess.DEVNULL, stderr=log,
                           timeout=stretched(120))
        orphan = int((here / "orphan").read_text())
        born = resources.process_start(orphan)
        self.stop_later(orphan, born)
        # The run's own census found the orphan and left the entry uncertain; its owner has ended.
        self.assertIn(f"PID {orphan} (group {orphan}, session ", (here / "owner.log").read_text())
        ((identity, entry),) = self.owners().items()
        self.assertTrue(entry["uncertain"])
        record = resources.record_path(self.guard, identity)
        # The orphan was never observed: only the command's session holds it.
        self.assertNotIn(orphan, {pid for pid, _start in self.recorded(record)})
        status, output = self.recover()
        self.assertEqual(status, 0, output)
        self.assertIn(f"{identity} {' '.join(entry['label'].split())[:resources.LABEL_LIMIT]}: PID {entry['pid']}, admitted ", output)
        self.assertIn("uncertain, and its owner has ended; a process record", output)
        for name in [identity, str(entry["pid"])]:
            status, output = self.recover(name)
            self.assertEqual(status, 1, output)
            self.assertIn(f"PID {orphan} (group {orphan}, session ", output)
            self.assertIn("still run, so its entry stays", output)
            self.assertEqual(list(self.owners()), [identity])
        (here / "orphan").unlink()
        self.until(lambda: resources.process_start(orphan) != born, "the orphan did not end")
        status, output = self.recover(identity)
        self.assertEqual(status, 0, output)
        self.assertFalse(self.guard.exists())
        self.assertFalse(record.exists())

    def test_a_live_owners_entry_is_never_touched_whatever_is_asked(self):
        with contextlib.redirect_stderr(StringIO()), resources.Admission("live", timeout=0) as live:
            # A command whose end the record settled, so that only the owner's life keeps the entry.
            self.assertEqual(processes.supervise([sys.executable, "-c", "pass"], cwd=self.temp.name), 0)
            for uncertain in [False, True]:
                with self.subTest(uncertain=uncertain):
                    # A stop being carried out marks the entry uncertain while the owner still runs.
                    if uncertain: live.interrupted()
                    owners = self.owners()
                    status, output = self.recover()
                    self.assertEqual(status, 0, output)
                    if uncertain:
                        self.assertIn(f"{live.identity} live: PID {os.getpid()}, admitted ", output)
                        self.assertIn("uncertain, and its owner still runs; a process record", output)
                    else:
                        self.assertIn("No entry of", output)
                    for name in [str(os.getpid()), live.identity]:
                        status, output = self.recover(name)
                        self.assertEqual(status, 1, output)
                        self.assertIn(f"live (PID {os.getpid()}) still runs, so its entry stays", output)
                        self.assertEqual(self.owners(), owners)
        self.assertFalse(self.guard.exists())
        # An owner that neither ps nor the kernel can read is taken as running; one whose PID another process has,
        # as ended, whether ps or, when ps cannot say, the kernel reads the other start (vocaro/queuekeeper#23).
        with patch.object(resources.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "")):
            with patch.object(resources, "kernel_start", side_effect=OSError("the kernel would not say")):
                self.assertFalse(resources.ended({"pid": os.getpid(), "start": "Thu Jan  1 00:00:00 1970"}))
            if sys.platform == "darwin":
                self.assertTrue(resources.ended({"pid": os.getpid(), "start": "Thu Jan  1 00:00:00 1970"}))
                self.assertFalse(resources.ended({"pid": os.getpid(), "start": resources.kernel_start(os.getpid())}))
        self.assertTrue(resources.ended({"pid": os.getpid(), "start": "Thu Jan  1 00:00:00 1970"}))
        self.assertTrue(resources.ended({"pid": self.ended_process()[0], "start": "Thu Jan  1 00:00:00 1970"}))

    def test_an_entry_without_a_process_record_it_can_trust_is_left_for_a_check_by_hand(self):
        # A toolkit before #61 recorded no admission time and no process record.
        pid, start = self.ended_process()
        older = {"pid": pid, "start": start, "label": "gate", "exclusive": False, "model": False}
        resources.publish_guard(self.guard, {"vocaroEngineering": 1, "owners": {"older": older}})
        status, output = self.recover()
        self.assertIn(f"older gate: PID {pid}, its owner started ", output)
        self.assertIn("its owner has ended; no process record", output)
        status, output = self.recover("older")
        self.assertEqual(status, 1, output)
        self.assertIn("gate (PID {}) has no process record, so its entry stays".format(pid), output)
        self.assertIn("check by hand that nothing its owner started still runs", output)
        self.assertIn("admissions remove OWNER --why TEXT", output)
        self.assertEqual(self.owners(), {"older": older})
        self.guard.unlink()
        # A caller that runs its own commands without recording them, as an older push keeper does, leaves none either.
        holder =("import sys\nfrom queuekeeper import resources\n"
                  "with resources.Admission('warm push 0123abc', timeout=0):\n    print(flush=True)\n    sys.stdin.read()\n")
        with subprocess.Popen([sys.executable, "-c", holder], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, text=True, env=self.environment()) as killed:
            self.assertEqual(killed.stdout.readline(), "\n")
            killed.kill()
            killed.wait()
        status, output = self.recover(str(killed.pid))
        self.assertEqual(status, 1, output)
        self.assertIn("has no process record", output)
        self.assertIn("runs its own commands without recording them, as an older push keeper does", output)
        self.assertIn("owner.json", output)
        self.assertEqual(len(self.owners()), 1)
        self.guard.unlink()
        # A record cut short, and one that began a command it never named, cannot vouch for it.
        root, root_start = self.ended_process()
        for record, reason in [
            (None, "cannot be read"),
            ([{"command": "a", "closed": [[root, float("nan")]]}], "cannot be read"),
            ([{"command": "b", "argv": ["swift", "test"]}], "may have started"),
        ]:
            with self.subTest(reason):
                identity = self.stranded([{"command": "a", "argv": ["swift"]}, {"command": "a", "root": root},
                                          {"command": "a", "processes": [[root, root_start]], "spaces": []},
                                          {"command": "a", "settled": "exited"}] + (record or []))
                if record is None:
                    with resources.record_path(self.guard, identity).open("a") as stream: stream.write('{"command": "b", "pro')
                status, output = self.recover(identity)
                self.assertEqual(status, 1, output)
                self.assertIn("process record cannot vouch for every command", output)
                self.assertIn(reason, output)
                self.assertIn(identity, self.owners())
                self.guard.unlink()

    def remove(self, owner, *why):
        """qk admissions remove's status and what it printed."""
        with contextlib.redirect_stdout(StringIO()) as output, contextlib.redirect_stderr(StringIO()) as error:
            try: status = cli.main(["admissions", "remove", owner, *why])
            except SystemExit as exit: status = exit.code
        return status, output.getvalue() + error.getvalue()

    def killed_leader(self):
        """A process leading a group and session of its own, killed after starting a child in both: its PID and start,
        and its child's."""
        here = Path(self.temp.name)
        (here / "child").unlink(missing_ok=True)
        leader = subprocess.Popen([sys.executable, "-c", "import subprocess, sys, time\nfrom pathlib import Path\n"
                                   "here = Path(sys.argv[1])\n"
                                   f"child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep({stretched(120)})'])\n"
                                   "(here / 'child.tmp').write_text(str(child.pid))\n(here / 'child.tmp').replace(here / 'child')\n"
                                   f"time.sleep({stretched(120)})", str(here)], start_new_session=True)
        self.addCleanup(lambda: leader.poll() is None and (leader.kill(), leader.wait()))
        self.until(lambda: (here / "child").exists(), "the leader never started its child")
        child = int((here / "child").read_text())
        child_start = resources.process_start(child)
        self.stop_later(child, child_start)
        start = resources.process_start(leader.pid)
        leader.kill()
        leader.wait()
        return leader.pid, start, child, child_start

    def test_an_entry_recovery_cannot_prove_finished_is_removed_by_hand_once_nothing_its_owner_started_runs(self):
        # The owner's decision of 2026-11-08 (#315): an entry with no process record, or one that cannot vouch for every
        # command, is removed by a command of its own, which runs the census it can first and refuses while anything the
        # owner started still runs. Before it, a person edited the guard by hand.
        sleeper = subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({stretched(120)})"])
        self.addCleanup(lambda: sleeper.poll() is None and (sleeper.kill(), sleeper.wait()))
        sleeper_start = resources.process_start(sleeper.pid)
        cases = [
            # An older toolkit's entry, with no record: only a group or session its owner led can be checked.
            ("no record", lambda pid, start: None, resources.UNRECORDED),
            # A caller that runs its own commands unrecorded: its admission line names the owner's group.
            ("unrecorded", lambda pid, start: [{"admission": "x", "owner": [pid, start], "group": pid, "session": pid,
                                                "ancestors": [], "supervised": False}], resources.UNRECORDED),
            # A command whose process the record never named, beside a recorded process that still runs.
            ("unvouched", lambda pid, start: [{"command": "a", "argv": ["swift", "test"]},
                                              {"command": "b", "argv": ["sleep"]},
                                              {"command": "b", "processes": [[sleeper.pid, sleeper_start]]}],
             resources.UNVOUCHED),
        ]
        for name, record, unproven in cases:
            with self.subTest(name):
                self.guard.unlink(missing_ok=True)
                owner, start, child, child_start = self.killed_leader()
                identity = uuid.uuid4().hex
                entry = {"pid": owner, "start": start, "label": f"lane {name}", "exclusive": False, "model": False}
                resources.publish_guard(self.guard, {"vocaroEngineering": 1, "owners": {identity: entry}})
                lines = record(owner, start)
                path = resources.record_path(self.guard, identity)
                if lines is not None:
                    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
                # Recovery leaves it, and says to remove it by hand with the new command.
                status, output = self.recover(identity)
                self.assertEqual(status, 1, output)
                self.assertIn(f"admissions remove OWNER --why TEXT", output)
                # A removal without a reason is refused by its parser, and an empty one by the command.
                status, output = self.remove(identity)
                self.assertEqual(status, 2, output)
                self.assertIn("--why", output)
                status, output = self.remove(identity, "--why", "  ")
                self.assertEqual(status, 1, output)
                self.assertIn(identity, self.owners())
                # Negative control: what the owner started still runs, so it is named and nothing is removed.
                status, output = self.remove(str(owner), "--why", "checked")
                self.assertEqual(status, 1, output)
                self.assertIn(f"PID {child} (in its owner's process group {owner}, started since its owner)", output)
                if name == "unvouched":
                    self.assertIn(f"PID {sleeper.pid} (a recorded process, still running)", output)
                    sleeper.kill()
                    sleeper.wait()
                self.assertIn("Nothing was removed.", output)
                self.assertEqual(self.owners(), {identity: entry})
                self.assertEqual(path.exists(), lines is not None)
                self.assertFalse(any(line["event"] == "recovered" and line["identity"] == identity for line in self.read()))
                self.end(child, child_start)
                status, output = self.remove(identity, "--why", "ps shows nothing of lane " + name)
                self.assertEqual(status, 0, output)
                self.assertIn(f"Removed lane {name} (PID {owner}) by hand: {unproven}", output)
                self.assertIn("and the guard with it", output)
                self.assertFalse(self.guard.exists())
                self.assertFalse(path.exists())
                (removed,) = [line for line in self.read() if line["event"] == "recovered" and line["identity"] == identity]
                self.assertEqual((removed["pid"], removed["byHand"], removed["unproven"], removed["why"]),
                                 (owner, True, unproven, "ps shows nothing of lane " + name))
                self.assertNotIn("automatic", removed)

    def test_removal_by_hand_leaves_a_live_owner_and_recovers_a_provably_finished_entry(self):
        # A live owner's entry is never removed, by recovery or by hand.
        live = {"pid": os.getpid(), "start": resources.process_start(os.getpid()), "label": "live", "exclusive": False,
                "model": False}
        resources.publish_guard(self.guard, {"vocaroEngineering": 1, "owners": {"live": live}})
        status, output = self.remove("live", "--why", "checked")
        self.assertEqual(status, 1, output)
        self.assertIn("live (PID {}) still runs, so its entry stays".format(os.getpid()), output)
        self.assertEqual(self.owners(), {"live": live})
        # An unknown owner changes nothing.
        status, output = self.remove("nobody", "--why", "checked")
        self.assertEqual(status, 1, output)
        self.assertIn("has the PID or identity nobody; nothing was changed", output)
        self.guard.unlink()
        # A provably finished entry is recovered, as `qk admissions recover` would, and logged as recovery logs it.
        identity = self.stranded([{"command": "a", "argv": ["true"]}, {"command": "a", "settled": "exited"}])
        status, output = self.remove(identity, "--why", "checked")
        self.assertEqual(status, 0, output)
        self.assertIn("Recovered stranded", output)
        self.assertFalse(self.guard.exists())
        (recovered,) = [line for line in self.read() if line["event"] == "recovered" and line["identity"] == identity]
        self.assertNotIn("byHand", recovered)

    def test_the_census_skips_an_id_that_is_now_a_live_processs_pid_and_checks_no_settled_command(self):
        # A process that leads a group and session of its own, with a child in both.
        # Both outlive the test that ends them, however slowly the host runs it.
        leader = subprocess.Popen([sys.executable, "-c", "import subprocess, sys, time\n"
                                   f"subprocess.Popen([sys.executable, '-c', 'import time; time.sleep({stretched(60)})'])\n"
                                   f"time.sleep({stretched(60)})"],
                                  start_new_session=True)
        def end():
            with contextlib.suppress(ProcessLookupError): os.killpg(leader.pid, signal.SIGKILL)
            leader.wait()
        self.addCleanup(end)
        self.until(lambda: any(group == leader.pid and pid != leader.pid
                               for pid, (_parent, group, _birth) in processes.snapshot().items()), "the child never started")
        born = " ".join(resources.process_start(leader.pid).split())
        root, root_start = self.ended_process()
        command = [{"command": "a", "argv": ["xcodebuild"]}, {"command": "a", "root": root, "spaces": [root]},
                   {"command": "a", "processes": [[root, root_start]], "spaces": []}]
        cases = [
            # The command used group and session leader.pid, which ended before another process took that PID.
            ("taken over", command + [{"command": "a", "spaces": [leader.pid]}], 0),
            # The command's own process still runs there: a negative control.
            ("its own", command + [{"command": "a", "processes": [[leader.pid, born]], "spaces": [leader.pid]}], 1),
            # A command the record settled was confirmed ended, so nothing of it is checked.
            ("settled", command + [{"command": "a", "processes": [[leader.pid, born]], "spaces": [leader.pid]},
                                   {"command": "a", "settled": "exited"}], 0)]
        for name, record, expected in cases:
            with self.subTest(name):
                identity = self.stranded(record)
                status, output = self.recover(identity)
                self.assertEqual(status, expected, output)
                self.assertEqual(identity in self.owners(), bool(expected))
                if expected: self.assertIn(f"PID {leader.pid} (a recorded process, still running)", output)
                self.guard.unlink(missing_ok=True)

    def test_recovery_checks_process_birth_against_each_commands_recorded_close_times(self):
        closing = datetime(2026, 11, 7, 12).timestamp()
        born = time.strftime(processes.STARTED, time.localtime(closing + 60))
        listing = {201: (1, 200, born)}
        first = [{"command": "a", "argv": ["swift"]}, {"command": "a", "root": 100, "spaces": [100, 200]}]
        second = [{"command": "b", "argv": ["swift"]}, {"command": "b", "root": 101, "spaces": [101, 200]}]
        closed = {"command": "a", "closed": [[200, closing]]}
        cases = [
            ("reused group", first + [closed], 0),
            ("older or partial record", first, 1),
            ("born before close", first + [{"command": "a", "closed": [[200, closing + 120]]}], 1),
            ("reopened group", first + [closed, {"command": "a", "closed": [[200, None]]}], 1),
            ("other command lacks close time", first + [closed] + second, 1),
            ("other command closed it later", first + [closed] + second
             + [{"command": "b", "closed": [[200, closing + 120]]}], 1),
            ("other command settled", first + [closed] + second + [{"command": "b", "settled": "exited"}], 0),
        ]
        with patch.object(processes, "snapshot", return_value=listing), patch.object(processes, "session", return_value=1):
            for name, record, expected in cases:
                with self.subTest(name):
                    identity = self.stranded(record)
                    status, output = self.recover(identity)
                    self.assertEqual(status, expected, output)
                    self.assertEqual(identity in self.owners(), bool(expected))
                    if expected: self.assertIn("PID 201 (group 200, session 1)", output)

    def test_supervision_in_an_admission_records_each_command_and_release_removes_the_record(self):
        here = Path(self.temp.name)
        child = here / "child"
        nested = [sys.executable, "-m", "queuekeeper.resources", "--", sys.executable, "-c", "pass"]
        with contextlib.redirect_stderr(StringIO()), patch.dict(os.environ, {"PYTHONPATH": host_isolation.pythonpath()}):
            with resources.Admission("recorded", timeout=0) as admission:
                # The grandchild, in a session of its own, stays until the record lists it, so that a sample a
                # busy host delays still finds it (#215); a census that never records it fails at the bound.
                grandchild = ("import json, os, pathlib, time\n"
                              f"record, began = pathlib.Path({str(admission.process_record)!r}), time.monotonic()\n"
                              "def recorded():\n"
                              "    for text in record.read_text().splitlines() if record.exists() else []:\n"
                              "        try: line = json.loads(text)\n"
                              "        except ValueError: continue\n"
                              "        if any(pid == os.getpid() for pid, _start in line.get('processes', [])): return True\n"
                              "    return False\n"
                              f"while not recorded() and time.monotonic() - began < {stretched(60)}: time.sleep(0.01)\n")
                command = [sys.executable, "-c", "import pathlib, subprocess, sys\n"
                           f"process = subprocess.Popen([sys.executable, '-c', {grandchild!r}], start_new_session=True)\n"
                           f"pathlib.Path({str(child)!r}).write_text(str(process.pid))\nprocess.wait()"]
                self.assertEqual(processes.supervise(command, cwd=here), 0)
                # A nested admission in another process reuses this one and writes nothing to its record.
                self.assertEqual(processes.supervise(nested, cwd=here), 0)
                opened, *lines = [json.loads(text) for text in admission.process_record.read_text().splitlines()]
            self.assertFalse(admission.process_record.exists())
            self.assertFalse(self.guard.exists())
        # The admission line comes first, written as the entry is added: the owner, its group and session (#315).
        self.assertEqual({key: opened[key] for key in ("admission", "owner", "group", "session", "supervised")},
                         {"admission": admission.identity, "owner": [os.getpid(), resources.process_start(os.getpid())],
                          "group": os.getpgid(0), "session": os.getsid(0), "supervised": False})
        self.assertEqual(opened["ancestors"][0], [os.getppid(), " ".join(resources.process_start(os.getppid()).split())])
        first, second = list(dict.fromkeys(line["command"] for line in lines))
        mine = [line for line in lines if line["command"] == first]
        # The command before it starts, then its root, its processes and their groups and sessions, then its end.
        self.assertEqual(mine[0], {"command": first, "argv": command})
        root = mine[1]["root"]
        self.assertEqual(mine[1], {"command": first, "root": root, "spaces": [root]})
        pid = int(child.read_text())
        seen = {pid for line in mine for pid, _start in line.get("processes", [])}
        self.assertEqual(seen, {root, pid})
        self.assertIn(pid, {space for line in mine for space in line.get("spaces", [])})
        self.assertIn(pid, {space for line in mine for space, moment in line.get("closed", []) if moment is not None})
        self.assertEqual(mine[-1], {"command": first, "settled": "exited"})
        self.assertEqual([line for line in lines if line["command"] == second][-1], {"command": second, "settled": "exited"})
        self.assertEqual(lines[-1]["command"], second)
        # Supervision outside admission records nothing.
        self.assertEqual(processes.supervise([sys.executable, "-c", "pass"], cwd=here), 0)
        self.assertEqual(sorted(self.guard.parent.glob(self.guard.name + "*")), [])
        # A line that cannot be written removes the record and ends recording, so that no record lacks a line.
        with contextlib.redirect_stderr(StringIO()) as error, resources.Admission("unwritten", timeout=0) as admission:
            admission.observe({"command": "a", "argv": []})
            self.assertTrue(admission.process_record.exists())
            with patch.object(resources.os, "write", return_value=0): admission.observe({"command": "a", "root": 1})
            self.assertFalse(admission.process_record.exists())
            admission.observe({"command": "b", "argv": []})
            self.assertFalse(admission.process_record.exists())
        self.assertEqual(error.getvalue().count("heavy: the process record was not written"), 1, error.getvalue())

    def test_a_caller_that_supervises_its_own_command_and_records_it_with_observe_is_recovered_as_supervisions(self):
        # StudyWright's push keeper runs its commands without processes.supervise and writes only the lines
        # an external supervisor writes, so that is all this owner writes.
        here = Path(self.temp.name)
        keeper = ("import subprocess, sys, time\nfrom pathlib import Path\nfrom queuekeeper import resources\n"
                  "here = Path(sys.argv[1])\n"
                  "with resources.Admission('warm push 0123abc', timeout=0) as admission:\n"
                  "    note = lambda **seen: admission.observe({'command': '3', **seen})\n"
                  "    argv = [sys.executable, '-c', 'import time; time.sleep(120)']\n"
                  "    note(argv=argv)\n"
                  "    child = subprocess.Popen(argv, start_new_session=True)\n"
                  "    note(root=child.pid, spaces=[child.pid])\n"
                  "    note(processes=[[child.pid, resources.process_start(child.pid)]], spaces=[])\n"
                  "    (here / 'command.tmp').write_text(str(child.pid))\n"
                  "    (here / 'command.tmp').replace(here / 'command')\n"
                  "    time.sleep(120)\n")
        owner = subprocess.Popen([sys.executable, "-c", keeper, str(here)], env=self.environment(),
                                 stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: owner.poll() is None and (owner.kill(), owner.wait()))
        self.until(lambda: (here / "command").exists(), "the command never started")
        pid = int((here / "command").read_text())
        start = resources.process_start(pid)
        self.stop_later(pid, start)
        ((identity, entry),) = self.owners().items()
        self.assertEqual(entry["pid"], owner.pid)
        owner.kill()
        owner.wait()
        status, output = self.recover(str(owner.pid))
        self.assertEqual(status, 1, output)
        self.assertIn(f"PID {pid} (a recorded process, still running)", output)
        self.assertIn(identity, self.owners())
        self.end(pid, start)
        status, output = self.recover(str(owner.pid))
        self.assertEqual(status, 0, output)
        self.assertIn("Recovered warm push 0123abc", output)
        self.assertFalse(self.guard.exists())
        self.assertFalse(resources.record_path(self.guard, identity).exists())

    def test_a_guard_in_no_format_of_admissions_is_left_alone_and_admission_names_the_command_when_it_refuses(self):
        for text in ['{"vocaroEngineering": 1, "owners": {', '{"keeper": 123456, "checkoutLock": "/tmp/lock"}']:
            self.guard.write_text(text)
            for owner in [[], ["123456"]]:
                with self.subTest(text, owner=owner):
                    status, output = self.recover(*owner)
                    self.assertEqual(status, 1, output)
                    self.assertIn(f"{self.guard} is unreadable or not in admission's format. Recovery leaves it alone",
                                  output)
            self.assertEqual(self.guard.read_text(), text)
        self.guard.unlink()
        identity = self.stranded(admitted=(datetime.now().astimezone() - timedelta(hours=2)).isoformat(timespec="seconds"))
        # An uncertain entry whose owner still runs is counted, and admission names the command; the dead one beside it
        # admission would free (vocaro/queuekeeper#23), so the refusal is checked with the dead one set aside.
        dead = self.owners()
        self.guard.unlink()
        self.stranded(live=True)
        with contextlib.redirect_stderr(StringIO()) as error, self.assertRaisesRegex(TimeoutError, "qk admissions recover"):
            with resources.Admission("refused", exclusive=True, timeout=0): self.fail("admitted")
        self.assertIn("requires inspection (its owner still runs), and is counted as slot 0; "
                      "`python3 scripts/qk admissions recover` lists it", error.getvalue())
        resources.publish_guard(self.guard, {"vocaroEngineering": 1, "owners": dead})
        status, output = self.recover()
        self.assertEqual(status, 0, output)
        self.assertIn(f"1 entry of {self.guard} is dead or uncertain:", output)
        self.assertIn(f"{identity} stranded: PID {self.owners()[identity]['pid']}, admitted 2h00m ago, its owner has "
                      "ended; no process record", output)
        status, output = self.recover("not-an-owner")
        self.assertEqual(status, 1, output)
        self.assertIn("has the PID or identity not-an-owner; nothing was changed", output)

    def test_a_guard_whose_owners_are_not_entries_in_admissions_format_is_left_alone_as_admission_refuses_it(self):
        # Recovery listed such a guard's entries one by one, an unreadable one beside a dead one that admission
        # never saw, or read some guards admission raised at as in its format (#72). It leaves each alone, as
        # admission refuses it, whether it lists entries or is named one by identity or PID.
        identity = self.stranded([{"command": "a", "argv": ["true"]}, {"command": "a", "settled": True}])
        written, entry = self.guard.read_text(), self.owners()[identity]
        record = resources.record_path(self.guard, identity)
        recorded = record.read_bytes()
        # The positive control: the guard as admission wrote it lists its dead entry, and, below, recovers it.
        status, output = self.recover()
        self.assertEqual(status, 0, output)
        self.assertIn(f"  {identity} stranded: PID {entry['pid']}, admitted ", output)
        for name, text in malformed_guards(identity, entry).items():
            self.guard.write_text(text)
            for owner in [[], [identity], [str(entry["pid"])]]:
                with self.subTest(name, owner=owner):
                    status, output = self.recover(*owner)
                    self.assertEqual(status, 1, output)
                    self.assertEqual(output, f"{self.guard} is unreadable or not in admission's format. Recovery leaves "
                                             "it alone; inspect it by hand.\n")
                    self.assertEqual(self.guard.read_text(), text)
                    self.assertEqual(record.read_bytes() if record.exists() else None, recorded)
        # The control's recovery, of the guard and record as admission wrote them, whatever a case changed.
        self.guard.write_text(written)
        record.write_bytes(recorded)
        status, output = self.recover(str(entry["pid"]))
        self.assertEqual(status, 0, output)
        self.assertIn("Recovered stranded", output)
        self.assertFalse(self.guard.exists())


class ReclamationTests(unittest.TestCase):
    """A holder admission cannot confirm running, its PID gone, reused or its start unreadable, loses its slot at the
    next attempt: what is left of it is stopped, by PID and start only, its entry and slot freed, and the next waiter
    admitted (the owner's decision, 2026-11-13, vocaro/queuekeeper#23). On 2026-11-12 a ps census that failed left two
    holders whose PIDs were gone holding slots 3 and 4 while a gate and two lanes waited. Every signal goes through a
    recorder that passes on only those the test expects, to processes the test started, in a private namespace."""
    setUp, tearDown = ResourceTests.setUp, ResourceTests.tearDown

    @property
    def guard(self):
        return Path(os.environ["QUEUEKEEPER_GATE_LOCK_FILE"] + ".warm-push-guard")

    def owners(self):
        return json.loads(self.guard.read_text())["owners"] if self.guard.exists() else {}

    def lines(self, event="reclaimed"):
        entries, _unreadable = resources.read_log()
        return [entry for entry in entries if entry["event"] == event]

    def sleeper(self, child=False):
        """A process this test started, sleeping, reaped as soon as it ends: its PID and start, and, with child, those
        of a child it started below it."""
        code = ("import subprocess, sys, time\n"
                + ("child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
                   "print(child.pid, flush=True)\n" if child else "print(0, flush=True)\n") + "time.sleep(120)\n")
        process = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
        below = int(process.stdout.readline())
        threading.Thread(target=process.wait, daemon=True).start()
        found = [(process.pid, resources.process_start(process.pid))]
        if child: found.append((below, resources.process_start(below)))
        for pid, start in found:
            self.addCleanup(lambda pid=pid, start=start: resources.process_start(pid) == start
                            and os.kill(pid, signal.SIGKILL))
        self.addCleanup(process.stdout.close)
        return found

    def ended_process(self):
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        start = resources.process_start(process.pid)
        process.kill()
        process.wait()
        return process.pid, start

    def hold(self, pid, start, slot=0, record=None, **fields):
        """A guard entry for pid and start, its slot file naming it, and its process record: its identity."""
        identity = uuid.uuid4().hex
        owners = self.owners()
        owners[identity] = {"pid": pid, "start": start, "label": f"holder {pid}", "exclusive": False, "model": False,
                            "admitted": "2026-11-12T17:44:00+00:00", **fields}
        resources.publish_guard(self.guard, {"vocaroEngineering": 1, "owners": owners})
        directory = resources.slot_directory()
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"slot{slot}").write_text(f"{pid} {identity} holder {pid}\n")
        if record is not None:
            resources.record_path(self.guard, identity).write_text("".join(json.dumps(line) + "\n" for line in record))
        return identity

    @contextlib.contextmanager
    def signals(self, allowed=()):
        """Record every signal but the existence check, and pass on only those to the PIDs allowed."""
        real, sent = os.kill, []
        def kill(pid, number):
            if number == 0: return real(pid, number)
            sent.append((pid, signal.Signals(number).name))
            if pid in allowed: return real(pid, number)
        with patch.object(resources.os, "kill", side_effect=kill):
            yield sent

    def failing(self, pids, error):
        """subprocess.run, but `ps -p` of one of pids raises error, as a ps that fails or times out does."""
        real = subprocess.run
        def run(command, *args, **kwargs):
            if command[:2] == ["ps", "-p"] and set(command[2].split(",")) & {str(pid) for pid in pids}: raise error
            return real(command, *args, **kwargs)
        return patch.object(resources.subprocess, "run", side_effect=run)

    def admitted_next(self, slot=0):
        """The next waiter's admission, which must take the freed slot."""
        with contextlib.redirect_stderr(StringIO()) as error, resources.Admission("next", timeout=0) as admitted:
            self.assertEqual([taken for taken, _fd in admitted.descriptors], [slot])
            self.assertIn(admitted.identity, self.owners())
        return admitted, error.getvalue()

    def test_a_holder_whose_pid_is_gone_is_freed_and_what_is_left_of_it_stopped(self):
        os.environ.update(HEAVY_SLOTS="1", HEAVY_SLOTS_MAX="1")
        pid, start = self.ended_process()
        (left, left_start), (below, below_start) = self.sleeper(child=True)
        mine = resources.process_start(os.getpid())
        # Its record names a process still running as itself, with a child below it, and this process and its parent,
        # which are never signaled.
        identity = self.hold(pid, start, record=[{"command": "a", "argv": ["swift", "test"]},
                                                 {"command": "a", "root": left, "processes": [[left, left_start]]},
                                                 {"command": "b", "processes": [[os.getpid(), mine]]}])
        with self.signals(allowed={left, below}) as sent:
            admitted, said = self.admitted_next()
        self.assertEqual(sorted(sent), sorted([(left, "SIGTERM"), (below, "SIGTERM")]))
        self.assertNotEqual(resources.process_start(left), left_start)
        self.assertNotEqual(resources.process_start(below), below_start)
        self.assertNotIn(identity, self.owners())
        self.assertFalse(resources.record_path(self.guard, identity).exists())
        (line,) = self.lines()
        self.assertEqual((line["identity"], line["pid"], line["process"], line["why"], line["slots"], line["model"]),
                         (identity, pid, "ended", f"its PID {pid} is gone", [0], False))
        self.assertEqual(sorted((item["pid"], tuple(item["signals"])) for item in line["signaled"]),
                         sorted([(left, ("SIGTERM",)), (below, ("SIGTERM",))]))
        self.assertEqual((line["survived"], line["replaced"], line["by"]["identity"]), ([], [], admitted.identity))
        self.assertIn(f"heavy: reclaimed holder {pid} (PID {pid}), slot 0: its process has ended (its PID {pid} is gone); "
                      f"signaled PID ", said)
        (logged,) = [entry for entry in self.lines("admitted") if entry["identity"] == admitted.identity]
        self.assertEqual(logged["reclaimed"], [identity])

    def test_a_root_the_record_names_before_its_start_is_stopped_when_born_before_the_records_last_write(self):
        # The owner was killed between starting its command and the first line that gives the command's start: the
        # record names only its root. On 2026-11-17 StudyWright's warm-push test killed its keeper there, and admission
        # replaced slot0 while the lane ran on (vocaro/queuekeeper#176).
        os.environ.update(HEAVY_SLOTS="1", HEAVY_SLOTS_MAX="1")
        pid, start = self.ended_process()
        (root, root_start), (below, below_start) = self.sleeper(child=True)
        record = [{"command": "a", "argv": ["swift", "test"]}, {"command": "a", "root": root, "spaces": [root]}]
        identity = self.hold(pid, start, record=record)
        with self.signals(allowed={root, below}) as sent:
            self.admitted_next()
        self.assertEqual(sorted(sent), sorted([(root, "SIGTERM"), (below, "SIGTERM")]))
        self.assertNotEqual(resources.process_start(root), root_start)
        self.assertNotEqual(resources.process_start(below), below_start)
        self.assertNotIn(identity, self.owners())
        (line,) = self.lines()
        self.assertEqual(sorted(item["pid"] for item in line["signaled"]), sorted([root, below]))

    def test_a_root_born_after_the_records_last_write_is_another_process_and_never_signaled(self):
        # Its PID went to another process once the owner had ended, after the record's last write.
        os.environ.update(HEAVY_SLOTS="1", HEAVY_SLOTS_MAX="1")
        pid, start = self.ended_process()
        ((other, other_start),) = self.sleeper()
        identity = self.hold(pid, start, record=[{"command": "a", "argv": ["swift", "test"]},
                                                 {"command": "a", "root": other, "spaces": [other]}])
        written = processes.birth_time(other_start) - 2
        os.utime(resources.record_path(self.guard, identity), (written, written))
        with self.signals() as sent:
            self.admitted_next()
        self.assertEqual(sent, [])
        self.assertEqual(resources.process_start(other), other_start)
        self.assertNotIn(identity, self.owners())
        (line,) = self.lines()
        self.assertEqual(line["signaled"], [])

    def test_a_reused_pid_is_freed_and_never_signaled(self):
        os.environ.update(HEAVY_SLOTS="1", HEAVY_SLOTS_MAX="1")
        ((other, other_start),) = self.sleeper()
        # Another process now has the holder's PID, and a process its record names: neither start matches.
        identity = self.hold(other, "Thu Jan  1 00:00:00 1970",
                             record=[{"command": "a", "processes": [[other, "Fri Jan  2 00:00:00 1970"]]}])
        with self.signals() as sent:
            self.admitted_next()
        self.assertEqual(sent, [])
        self.assertEqual(resources.process_start(other), other_start)
        self.assertNotIn(identity, self.owners())
        (line,) = self.lines()
        self.assertEqual((line["process"], line["signaled"]), ("ended", []))
        self.assertIn(f"its PID {other} runs another process, started {' '.join(other_start.split())}", line["why"])

    def test_a_ps_census_that_fails_or_times_out_frees_what_the_kernel_reads_as_ended_and_keeps_a_live_holder(self):
        if sys.platform != "darwin": self.skipTest("the kernel's record of a process is read on macOS")
        os.environ.update(HEAVY_SLOTS="2", HEAVY_SLOTS_MAX="2")
        for error in [subprocess.TimeoutExpired(["ps"], resources.ENDED_PS_SECONDS), OSError("ps could not start")]:
            with self.subTest(type(error).__name__):
                self.guard.unlink(missing_ok=True)
                resources.log_path().unlink(missing_ok=True)
                ((live, live_start),), ((other, _other_start),) = self.sleeper(), self.sleeper()
                kept = self.hold(live, live_start, slot=1)
                # The live holder's lock, which it holds as long as it runs.
                _slot, held = resources.take_slot(resources.slot_directory(), 2, start=1)
                reused = self.hold(other, "Thu Jan  1 00:00:00 1970")
                try:
                    with self.failing([live, other], error), self.signals() as sent:
                        self.admitted_next(slot=0)
                finally:
                    os.close(held)
                self.assertEqual(sent, [])
                self.assertEqual(set(self.owners()), {kept})
                (line,) = self.lines()
                self.assertEqual((line["identity"], line["process"]), (reused, "ended"))
                self.assertIn("runs another process", line["why"])

    def test_a_holder_whose_start_cannot_be_read_is_freed_and_nothing_of_it_is_signaled(self):
        os.environ.update(HEAVY_SLOTS="1", HEAVY_SLOTS_MAX="1")
        (pid, start), (below, below_start) = self.sleeper(child=True)
        identity = self.hold(pid, start, record=[{"command": "a", "processes": [[pid, start], [below, below_start]]}])
        # Something still holds its slot's lock, as the holder would if it runs: the lock is replaced.
        _slot, held = resources.take_slot(resources.slot_directory(), 1)
        self.addCleanup(os.close, held)
        real = resources.kernel_start
        def kernel(asked):
            if asked == pid: raise OSError("sysctl KERN_PROC_PID: the kernel would not say")
            return real(asked)
        with self.failing([pid], subprocess.TimeoutExpired(["ps"], resources.ENDED_PS_SECONDS)), \
                patch.object(resources, "kernel_start", side_effect=kernel), self.signals() as sent:
            _admitted, said = self.admitted_next()
        # Its PID is not proven its own, so neither it nor its child is signaled.
        self.assertEqual(sent, [])
        self.assertEqual((resources.process_start(pid), resources.process_start(below)), (start, below_start))
        self.assertNotIn(identity, self.owners())
        (line,) = self.lines()
        self.assertEqual((line["process"], line["signaled"], line["replaced"]), ("unknown", [], ["slot0"]))
        self.assertIn(f"ps took more than {resources.ENDED_PS_SECONDS}s", line["why"])
        self.assertIn("neither it nor anything under it was signaled", line["unsignaled"])
        self.assertIn("its process could not be checked", said)

    def test_a_live_confirmed_holder_keeps_its_slot(self):
        # The control: a holder whose PID runs with its recorded start is never freed or signaled.
        os.environ.update(HEAVY_SLOTS="1", HEAVY_SLOTS_MAX="1")
        ((pid, start),) = self.sleeper()
        identity = self.hold(pid, start)
        _slot, held = resources.take_slot(resources.slot_directory(), 1)
        self.addCleanup(os.close, held)
        with self.signals() as sent, contextlib.redirect_stderr(StringIO()), self.assertRaises(TimeoutError):
            with resources.Admission("next", timeout=0): self.fail("admitted")
        self.assertEqual(sent, [])
        self.assertEqual(list(self.owners()), [identity])
        self.assertEqual(resources.process_start(pid), start)
        self.assertEqual(self.lines(), [])

    def test_stop_holder_signals_only_what_runs_as_itself_and_never_this_process_or_its_ancestors(self):
        # With a fake process table: the owner runs as itself, a child below it, this process, its parent and a
        # process whose PID was reused since the record named it.
        me, parent = os.getpid(), os.getppid()
        table = {5001: (1, 5001, "Thu Nov 12 17:00:00 2026"), 5002: (5001, 5001, "Thu Nov 12 17:00:01 2026"),
                 5003: (1, 5003, "Thu Nov 12 18:00:00 2026"), me: (parent, me, "now"), parent: (1, parent, "then")}
        gone = set()
        def kill(pid, number):
            if number == signal.SIGTERM: gone.add(pid)
        def start_of(pid):
            return None if pid in gone else table[pid][2] if pid in table else None
        identity = uuid.uuid4().hex
        resources.record_path(self.guard, identity).parent.mkdir(parents=True, exist_ok=True)
        resources.record_path(self.guard, identity).write_text("".join(json.dumps(line) + "\n" for line in [
            {"command": "a", "processes": [[5003, "Wed Nov 11 09:00:00 2026"], [me, "now"], [parent, "then"]]}]))
        entry = {"pid": 5001, "start": "Thu Nov 12 17:00:00 2026"}
        stopped = resources.stop_holder(self.guard, identity, entry, "ended", listing=lambda: table, start_of=start_of,
                                        kill=kill, clock=time.monotonic, sleep=lambda _seconds: None)
        self.assertEqual([(item["pid"], item["signals"]) for item in stopped["signaled"]],
                         [(5001, ["SIGTERM"]), (5002, ["SIGTERM"])])
        self.assertEqual(stopped["survived"], [])
        # What survives SIGTERM gets SIGKILL after the bound; one that survives that is named.
        gone.clear()
        stubborn = []
        with patch.object(resources, "RECLAIM_TERM_SECONDS", 0), patch.object(resources, "RECLAIM_KILL_SECONDS", 0):
            stopped = resources.stop_holder(self.guard, identity, entry, "ended", listing=lambda: table,
                                            start_of=lambda pid: table[pid][2] if pid in table else None,
                                            kill=lambda pid, number: stubborn.append((pid, number)), sleep=lambda _s: None)
        self.assertEqual(stubborn, [(5001, signal.SIGTERM), (5002, signal.SIGTERM),
                                    (5001, signal.SIGKILL), (5002, signal.SIGKILL)])
        self.assertEqual(stopped["survived"], [5001, 5002])
        # An unknown holder is signaled nothing.
        self.assertEqual(resources.stop_holder(self.guard, identity, entry, "unknown", listing=lambda: table,
                                               start_of=start_of, kill=lambda *_: self.fail("signaled"))["signaled"], [])


class LaneLabelTests(unittest.TestCase):
    """A lane run's admission label names its project, so that two projects' lanes of one name stay apart in
    waiting messages, the guard and the admission log (#51). A label a caller gives, an qk admit command
    line, and entries written before labels named the project are kept as they are."""
    setUp, tearDown = ResourceTests.setUp, ResourceTests.tearDown
    hold, lines = ResourceTests.hold, AdmissionLogTests.lines
    LAUNCH = ("import sys\nfrom unittest.mock import patch\nfrom queuekeeper import cli\n"
              "with patch.object(cli, 'refresh_policy', return_value='policy'), "
              "patch.object(cli, 'source_identity', return_value={}):\n"
              "    raise SystemExit(cli.main(sys.argv[1:]))\n")
    QUICK = [sys.executable, "-c", "pass"]

    def project(self, name, **lanes):
        root = Path(self.temp.name) / name
        root.mkdir()
        (root / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": name, "lanes": lanes}))
        return root

    def marker(self, root, lane, state):
        return Path(self.temp.name) / f"{root.name}-{lane}.{state}"

    def held(self, project, lane):
        """A command for project's lane that marks itself started, then runs until it is released."""
        started, released = (str(Path(self.temp.name) / f"{project}-{lane}.{state}") for state in ["started", "released"])
        return [sys.executable, "-c", f"import pathlib, time\npathlib.Path({started!r}).touch()\n"
                f"while not pathlib.Path({released!r}).exists(): time.sleep(0.01)\n"]

    def run_lane(self, root, *arguments):
        """qk run here, with --if-free waiting for nothing: its status and what it printed on stderr."""
        with patch.object(cli, "refresh_policy", return_value="policy"), \
                patch.object(cli, "source_identity", return_value={}), patch.object(cli, "FREE_WAIT", 0), \
                contextlib.redirect_stdout(StringIO()), contextlib.redirect_stderr(StringIO()) as error:
            return cli.main(["--project", str(root), "run", *arguments]), error.getvalue()

    def admit(self, root, command, *options):
        with patch.object(cli, "refresh_policy", return_value="policy"), \
                patch.object(cli, "source_identity", return_value={}), \
                contextlib.redirect_stdout(StringIO()), contextlib.redirect_stderr(StringIO()):
            self.assertEqual(cli.main(["--project", str(root), "admit", *options, "--", *command]), 0)

    def holding(self, root, lane, ready=None):
        """A run of root's lane in a process of its own, once ready() holds: by default, once its command started."""
        ready = ready or self.marker(root, lane, "started").exists
        environment = {**os.environ, "PYTHONPATH": host_isolation.pythonpath()}
        run = subprocess.Popen([sys.executable, "-c", self.LAUNCH, "--project", str(root), "run", lane],
                               env=environment, stderr=subprocess.DEVNULL)
        def end():
            if run.poll() is None:
                run.kill()
                run.wait()
        self.addCleanup(end)
        deadline = time.monotonic() + stretched(30)
        while not ready():
            self.assertIsNone(run.poll(), f"{root.name}'s {lane} ended early")
            self.assertLess(time.monotonic(), deadline, f"{root.name}'s {lane} never got ready")
            time.sleep(0.01)
        return run

    def release(self, root, lane, run):
        self.marker(root, lane, "released").touch()
        self.assertEqual(run.wait(timeout=stretched(30)), 0)

    def test_fresh_local_whole_lane_wait_identifies_the_configured_lane(self):
        suite = {"command": self.QUICK}
        root = self.project("Beta", suite=suite, local={**suite, "here": "it needs a local input"},
                            gate={"parallel": ["suite", "local"]})
        self.hold(0, 1, 2)
        original = resources.Admission._unobserve
        observed = []

        def unobserve(admission):
            try:
                waiter, = offload_host.admissions_now()["waiters"]
                observed.append(waiter.get("lane"))
            finally:
                original(admission)

        cases = [
            (("--here", "suite"), "suite"),
            (("local",), "local"),
            (("--here", "suite", "--", "filter"), None),
            (("--here", "gate"), None),
        ]
        with patch.object(resources.Admission, "_unobserve", unobserve):
            for words, identity in cases:
                with self.subTest(words=words):
                    status, error = self.run_lane(root, "--if-free", *words)
                    self.assertEqual(status, resources.BUSY, error)
                    self.assertEqual(observed.pop(), identity)

    def test_a_request_waiting_behind_another_projects_lane_names_that_project(self):
        alpha = self.project("Alpha", generate={"command": self.held("Alpha", "generate"), "resource": "heavy", "model": True},
                             gate={"command": self.QUICK, "resource": "exclusive"})
        beta = self.project("Beta", generate={"command": self.QUICK, "resource": "heavy", "model": True},
                            gate={"command": self.QUICK, "resource": "heavy"})
        # Alpha's generate holds the model, which Beta's generate waits for.
        generate = self.holding(alpha, "generate")
        status, error = self.run_lane(beta, "--if-free", "generate")
        self.assertEqual(status, resources.BUSY, error)
        self.assertRegex(error, r"heavy: waiting \(\d+s\): "
                         + re.escape("the on-device model, one run at a time, is held by Alpha generate\n"))
        self.assertNotIn("held by generate", error)
        self.release(alpha, "generate", generate)
        # Alpha's gate waits in the exclusive queue for a slot other work holds, and Beta's gate behind it.
        self.hold(0)
        queue = resources.slot_directory() / "exclusive-queue.json"
        gate = self.holding(alpha, "gate", queue.exists)
        status, error = self.run_lane(beta, "--if-free", "gate")
        self.assertEqual(status, resources.BUSY, error)
        self.assertRegex(error, r"heavy: waiting \(\d+s\): "
                         + re.escape("it waits behind exclusive work queued ahead of it: Alpha gate\n"))
        self.assertNotIn("queued ahead of it: gate", error)
        gate.terminate()
        gate.wait(timeout=stretched(30))

    def test_the_guard_records_each_projects_lane_apart_beside_older_entries(self):
        guard = Path(os.environ["QUEUEKEEPER_GATE_LOCK_FILE"] + ".warm-push-guard")
        copy = Path(self.temp.name) / "guard.json"
        snapshot = [sys.executable, "-c", f"import shutil; shutil.copy({str(guard)!r}, {str(copy)!r})"]
        alpha = self.project("Alpha", gate={"command": self.held("Alpha", "gate"), "resource": "heavy"})
        beta = self.project("Beta", gate={"command": snapshot, "resource": "heavy"})
        # A running owner that a toolkit from before #51 recorded with the lane alone.
        older = {"pid": os.getpid(), "start": resources.process_start(os.getpid()), "label": "gate",
                 "exclusive": False, "model": False}
        resources.publish_guard(guard, {"vocaroEngineering": 1, "owners": {"older": older}})
        gate = self.holding(alpha, "gate")
        status, error = self.run_lane(beta, "gate")
        self.assertEqual(status, 0, error)
        owners = json.loads(copy.read_text())["owners"]
        self.assertEqual(sorted(owner["label"] for owner in owners.values()), ["Alpha gate", "Beta gate", "gate"])
        self.release(alpha, "gate", gate)
        # Each run removed its own entry and left the older one.
        self.assertEqual(json.loads(guard.read_text())["owners"], {"older": older})
        # A label a caller gives, and an qk admit command line, are recorded as they are.
        with contextlib.redirect_stderr(StringIO()), resources.Admission("warm push 0123abc", timeout=0) as push:
            self.assertEqual(json.loads(guard.read_text())["owners"][push.identity]["label"], "warm push 0123abc")
        self.admit(beta, snapshot)
        labels = sorted(owner["label"] for owner in json.loads(copy.read_text())["owners"].values())
        self.assertEqual(labels, sorted([" ".join(snapshot), "gate"]))

    def test_admission_log_lines_name_each_projects_lane_apart(self):
        alpha = self.project("Alpha", gate={"command": self.QUICK, "resource": "heavy"})
        beta = self.project("Beta", gate={"command": self.QUICK, "resource": "heavy"})
        for root in [alpha, beta]:
            self.assertEqual(self.run_lane(root, "gate")[0], 0)
        self.admit(beta, self.QUICK)
        # A request that stops waiting is logged under its project too.
        self.hold(0, 1, 2)
        self.assertEqual(self.run_lane(beta, "--if-free", "gate")[0], resources.BUSY)
        # The log keeps a label's first LABEL_LIMIT characters, which a long sys.executable can pass (#117).
        command = " ".join(self.QUICK)[:resources.LABEL_LIMIT]
        self.assertEqual([(line["event"], line["label"]) for line in self.lines()],
                         [("admitted", "Alpha gate"), ("ended", "Alpha gate"), ("admitted", "Beta gate"),
                          ("ended", "Beta gate"), ("admitted", command), ("ended", command), ("withdrawn", "Beta gate")])

    def test_a_lane_whose_admission_wait_runs_out_is_busy_not_an_eng_error(self):
        """Seen in the toolkit's queue on 2026-11-07: a gate waited 5104 s behind whole-host gates, and its
        TimeoutError, an OSError, made qk run exit 2, which the queue took for a failure (#268)."""
        from queuekeeper import remote
        ran = self.marker(Path("Beta"), "gate", "ran")
        beta = self.project("Beta", gate={"command": [sys.executable, "-c", f"open({str(ran)!r}, 'w')"],
                                          "resource": "heavy"})
        # Negative control: an OSError of queuekeeper's own, with the host free, is still queuekeeper's error, exit 2.
        with patch.object(cli, "admitted_tree", side_effect=OSError("the disk went away")):
            status, error = self.run_lane(beta, "gate")
        self.assertEqual(status, 2, error)
        self.assertIn("queuekeeper: the disk went away", error)
        self.assertFalse(ran.exists())
        self.hold(0, 1, 2)
        with patch.object(remote, "OVERFLOW_TIMEOUT", 0.2):
            status, error = self.run_lane(beta, "gate")
        self.assertEqual(status, resources.BUSY, error)
        self.assertIn("queuekeeper: gate not run on this Mac", error)
        self.assertIn("Resource queue busy", error)
        self.assertFalse(ran.exists())

    def foreground_project(self):
        """Beta, whose gate lane takes the foreground, on another Mac whose device record is this test's and whose
        foreground reads ready, and the marker that lane leaves if it runs."""
        ran = self.marker(Path("Beta"), "gate", "ran")
        beta = self.project("Beta", gate={"command": [sys.executable, "-c", f"open({str(ran)!r}, 'w')"],
                                          "resource": "heavy", "foreground": True})
        environment = patch.dict(os.environ, {devices.OVERRIDE: str(Path(self.temp.name) / "device-handoff.json")})
        environment.start()
        self.addCleanup(environment.stop)
        ready = patch.object(foreground, "problems", return_value=[])
        ready.start()
        self.addCleanup(ready.stop)
        devices.Record().defer("Trevors-MacBook-Pro-715")
        farm_fixture.takes_foreground(self)
        return beta, ran

    def admit_foreground(self, root, command):
        """qk admit --foreground --here of command, waiting for nothing: its status and what it printed on stderr."""
        with patch.object(cli, "refresh_policy", return_value="policy"), \
                patch.object(cli, "source_identity", return_value={}), \
                contextlib.redirect_stdout(StringIO()), contextlib.redirect_stderr(StringIO()) as error:
            return cli.main(["--project", str(root), "admit", "--foreground", "--here", "--timeout", "0", "--",
                             *command]), error.getvalue()

    def test_work_whose_wait_for_the_farm_macs_foreground_runs_out_is_busy_not_an_eng_error(self):
        """Its TimeoutError, an OSError, made qk run exit 2, which the queue takes for a failure (#297), as an
        admission wait's did (#268). Waiting for no time reaches it without foreground.POLL's sleep."""
        from queuekeeper import remote
        beta, ran = self.foreground_project()
        command = [sys.executable, "-c", f"open({str(ran)!r}, 'w')"]
        # Negative control: an OSError of queuekeeper's own, raised where the wait's is, is still queuekeeper's error, exit 2.
        with patch.object(foreground, "slot", side_effect=OSError("the disk went away")):
            for status, error in [self.run_lane(beta, "--here", "gate"), self.admit_foreground(beta, command)]:
                self.assertEqual(status, 2, error)
                self.assertIn("queuekeeper: the disk went away", error)
                self.assertNotIn("not run on", error)
        record = devices.Record()
        release_by = (datetime.now().astimezone() + timedelta(hours=1)).isoformat()
        held = record.reserve(devices.FOREGROUND, owner="another run", revision=0, purpose="its tests",
                              release_by=release_by)
        with patch.object(remote, "OVERFLOW_TIMEOUT", 0):
            status, error = self.run_lane(beta, "--here", "gate")
        self.assertEqual(status, resources.BUSY, error)
        self.assertIn("queuekeeper: gate not run on this Mac", error)
        self.assertIn("foreground stayed held by another run for 0s", error)
        status, error = self.admit_foreground(beta, command)
        self.assertEqual(status, resources.BUSY, error)
        self.assertIn("queuekeeper: the command not run on this Mac", error)
        self.assertIn("foreground stayed held by another run for 0s", error)
        self.assertFalse(ran.exists())
        # The wait took nothing from the run that holds it.
        self.assertEqual(record.entry(devices.FOREGROUND)["revision"], held["revision"])

    def test_canceling_a_foreground_lane_while_waiting_for_admission_releases_its_lease(self):
        beta, ran = self.foreground_project()
        self.hold(0, 1, 2)
        record = devices.Record()
        observed, restored = [], []
        observe = resources.Admission._observe

        @contextlib.contextmanager
        def guard(owner, **options):
            try:
                yield
            finally:
                entry = record.entry(devices.FOREGROUND)
                restored.append((entry["ownerChatID"], devices.condition(entry)))

        def cancel_after_observing(admission):
            observe(admission)
            waiter = json.loads(admission.wait_record.read_text())
            entry = record.entry(devices.FOREGROUND)
            self.assertEqual(devices.condition(entry), "active")
            self.assertEqual((waiter["pid"], waiter["start"]),
                             (entry["hostProcessIdentifier"], entry["hostProcessStart"]))
            self.assertFalse(admission.descriptors)
            self.assertFalse(ran.exists())
            observed.append((entry["ownerChatID"], admission.wait_record))

        # Deliver the synthetic stop at the first published host wait, without using a real signal or the desktop.
        with patch.object(foreground, "GUARDS", [guard]), patch.object(foreground, "front", return_value="Finder"), \
                patch.object(foreground.opened_apps, "read", return_value=[]), \
                patch.object(foreground.opened_apps, "settle", return_value=None), \
                patch.object(resources.Admission, "_observe", cancel_after_observing), \
                patch.object(processes, "requested", side_effect=lambda: signal.SIGTERM if observed else None):
            with self.assertRaisesRegex(processes.Interrupted, "SIGTERM while waiting for admission"):
                self.run_lane(beta, "--here", "gate")

        [(owner, wait_record)] = observed
        self.assertEqual(restored, [(owner, "active")])
        self.assertEqual(record.entry(devices.FOREGROUND)["state"], "released")
        self.assertFalse(wait_record.exists())
        self.assertFalse((resources.slot_directory() / resources.QUEUE).exists())
        self.assertFalse(ran.exists())
        self.assertEqual([(line["event"], line["outcome"]) for line in self.lines()], [("withdrawn", "stopped")])

    def test_a_foreground_release_that_times_out_after_the_lane_ran_is_not_reported_as_not_run(self):
        """Only the wait to enter is BUSY: the same TimeoutError once the lane has run must not say it did not."""
        beta, ran = self.foreground_project()
        @contextlib.contextmanager
        def slot(purpose, **options):
            yield "owner"
            raise TimeoutError("the device record stayed locked for 30s; nothing was changed")
        with patch.object(foreground, "slot", slot), patch.object(cli.plugins.FOREGROUND, "guarded_alerts", return_value=None), \
                patch.object(cli.plugins.FOREGROUND, "cleared_notifications", return_value=None):
            status, error = self.run_lane(beta, "--here", "gate")
        self.assertEqual(status, 2, error)
        self.assertTrue(ran.exists())
        self.assertIn("stayed locked", error)
        self.assertNotIn("not run on", error)

    def test_the_summary_counts_two_projects_lanes_of_one_name_apart_from_each_other_and_older_lines(self):
        log = resources.log_path()
        log.parent.mkdir(parents=True, exist_ok=True)
        # Lines a toolkit from before #51 wrote, with the lane alone.
        log.write_text("".join(json.dumps({"time": moment, "event": event, "identity": "older", "pid": 1, "kind": "heavy",
                                           "label": "gate", **fields}) + "\n" for moment, event, fields in [
            ("2026-10-20T08:00:00+00:00", "admitted", {"slots": [0], "waited": 0}),
            ("2026-10-20T08:01:00+00:00", "ended", {"slots": [0], "held": 60, "outcome": "released"})]))
        alpha = self.project("Alpha", gate={"command": self.QUICK, "resource": "heavy"})
        beta = self.project("Beta", gate={"command": self.QUICK, "resource": "heavy"})
        for root in [alpha, beta, alpha]:
            self.assertEqual(self.run_lane(root, "gate")[0], 0)
        with contextlib.redirect_stderr(StringIO()), resources.Admission("warm push 0123abc", timeout=0): pass
        with contextlib.redirect_stdout(StringIO()) as output:
            self.assertEqual(cli.main(["admissions"]), 0)
        groups = [line.split(";")[0] for line in output.getvalue().splitlines() if line.startswith("  ")]
        self.assertEqual(groups, ["  Alpha gate: 2 admitted", "  Beta gate: 1 admitted", "  gate: 1 admitted",
                                  "  warm push 0123abc: 1 admitted"])
        self.assertIn("  gate: 1 admitted; waited mean 0s, max 0s; held mean 1m00s, max 1m00s, total 1m00s; "
                      "ends released 1", output.getvalue())

    def test_each_runs_admission_line_says_whether_eng_offered_it_to_the_farm_mac_and_why_it_ran_here(self):
        # The positive controls: a lane that stays here, qk run --here, qk admit --here, and a lane and a plain qk
        # admit that queuekeeper offered first, which with no other Mac configured are declined, each say so on their
        # admitted line (qk admit is offered first since the owner's decision of 2026-11-07).
        root = self.project("Alpha", gate={"command": self.QUICK, "resource": "heavy"},
                            device={"command": self.QUICK, "resource": "heavy", "here": "it needs this Mac's iPhone"})
        for arguments in [["device"], ["--here", "gate"], ["gate"]]:
            self.assertEqual(self.run_lane(root, *arguments)[0], 0)
        self.admit(root, self.QUICK, "--here")
        self.admit(root, self.QUICK)
        # Work another Mac runs for another Mac says which sent it.
        with patch.dict(os.environ, {"QUEUEKEEPER_DISPATCHED_FROM": "Sender"}):
            self.assertEqual(self.run_lane(root, "--here", "gate")[0], 0)
        # The negative control: a caller's own admission names no placement.
        with contextlib.redirect_stderr(StringIO()), resources.Admission("warm push 0123abc", timeout=0): pass
        admitted = [line for line in self.lines() if line["event"] == "admitted"]
        placements = [line.get("placement") for line in admitted]
        offered = [placements[2], placements[4]]
        self.assertEqual(placements[:2] + placements[3:4] + placements[5:], [
            {"offered": False, "because": "the lane says: it needs this Mac's iPhone"},
            {"offered": False, "because": "--here"},
            {"offered": False, "because": "qk admit --here"},
            {"dispatchedFrom": "Sender"}, None])
        for placement in offered:
            self.assertEqual(placement, {"offered": True, "because": "the farm has no other Mac that takes work now"})
        entries = [resources.read_entry(json.dumps(line)) for line in admitted]
        self.assertEqual([resources.placed(entry) for entry in entries[:2] + entries[3:4] + entries[5:]], [
            ("never", "a lane that stays on this Mac"), ("never", "qk run --here"),
            ("never", "qk admit --here"), ("sent", "sent by Sender"), ("unrecorded", "a command")])
        self.assertEqual([resources.placed(entries[index])[0] for index in (2, 4)], ["declined", "declined"])
        # A line an earlier toolkit wrote, when qk admit without --verify ran here, keeps its category.
        self.assertEqual(resources.placed({"placement": {"offered": False, "because": "qk admit runs here without "
                                                         "--verify"}}), ("never", "qk admit without --verify"))
        # qk admissions usage reads this log, and says why it cannot read another Mac's.
        with contextlib.redirect_stdout(StringIO()) as output:
            self.assertEqual(cli.main(["admissions", "usage"]), 0)
        text = output.getvalue()
        self.assertIn("  never offered to another Mac: 3, held ", text)
        self.assertIn("    qk run --here: 1, held ", text)
        self.assertIn("    qk admit --here: 1, held ", text)
        self.assertIn("  offered to another Mac and declined: 2, held ", text)
        self.assertIn("  the farm's other Macs: log unavailable: the farm has no other Mac that takes work now", text)

    def test_a_project_without_a_name_is_named_by_its_checkout(self):
        root = Path(self.temp.name) / "Gamma"
        root.mkdir()
        self.assertEqual(cli.lane_label(root, {"name": "Alpha"}, "gate"), "Alpha gate")
        self.assertEqual(cli.lane_label(root, {}, "gate"), "Gamma gate")
        # A linked worktree, named for its session, takes its main checkout's name.
        private = root / ".git/worktrees/session"
        private.mkdir(parents=True)
        (private / "commondir").write_text("../..\n")
        worktree = Path(self.temp.name) / "session"
        worktree.mkdir()
        (worktree / ".git").write_text(f"gitdir: {private}\n")
        self.assertEqual(cli.lane_label(worktree, {}, "gate"), "Gamma gate")


if __name__ == "__main__": unittest.main()


class DeviceWindowWaitTests(unittest.TestCase):
    """A device lane waits for admission only until the last start that can finish within the window the session
    holds the device for, so that a window is not spent queued (vocaro-engineering#292)."""
    setUp, tearDown = ResourceTests.setUp, ResourceTests.tearDown
    hold = ResourceTests.hold
    project, marker, run_lane = LaneLabelTests.project, LaneLabelTests.marker, LaneLabelTests.run_lane

    def lane(self, name="device", **options):
        """Beta with the lane name and its marker, in a device record of this test's, the session being session-a."""
        environment = patch.dict(os.environ, {devices.OVERRIDE: str(Path(self.temp.name) / "device-handoff.json"),
                                              devices.OWNER: "session-a"})
        environment.start()
        self.addCleanup(environment.stop)
        ran = self.marker(Path("Beta"), name, "ran")
        return self.project("Beta", **{name: {"command": [sys.executable, "-c", f"open({str(ran)!r}, 'w')"],
                                              "resource": "heavy", "here": "a physical device", "timeout": 120,
                                              **options}}), ran

    def reserve(self, seconds, owner="session-a", key="iPhone"):
        until = (datetime.now().astimezone() + timedelta(seconds=seconds)).isoformat()
        devices.Record().reserve(key, owner=owner, revision=0, purpose="the owner's window", release_by=until)

    def measure(self, label, *seconds, outcome="released", keys=None):
        for held in seconds:
            event = {"event": "ended"} if keys is None else {"event": "device-lane-passed", "devices": keys}
            resources.append_log({**event, "identity": str(uuid.uuid4()), "pid": 1, "kind": "heavy",
                                  "label": label[:resources.LABEL_LIMIT], "outcome": outcome, "slots": [0],
                                  "held": held})

    def test_a_window_that_runs_out_during_the_wait_ends_it_busy_with_nothing_run(self):
        """The wait ends at the last start and not at the default admission timeout, which here would take an hour."""
        root, ran = self.lane(devices=["iPhone"])
        self.reserve(3600)
        self.hold(0, 1, 2)
        with patch.object(devices.WindowWait, "left", return_value=0.05):
            status, error = self.run_lane(root, "device")
        self.assertEqual(status, resources.BUSY, error)
        self.assertIn("queuekeeper: device not run on this Mac", error)
        self.assertIn("queuekeeper: device stopped waiting: the window on iPhone ends ", error)
        self.assertIn("No device result was recorded", error)
        self.assertFalse(ran.exists())
        self.assertFalse((root / ".build/queuekeeper/device/source.json").exists())

    def test_if_free_caps_its_direct_probe_at_the_last_start_in_the_device_window(self):
        root, ran = self.lane(devices=["iPhone"])
        self.measure("Beta device", 60, keys=["iPhone"])
        for remaining in (None, 2.5):
            with self.subTest(remaining=remaining):
                now = datetime.now().astimezone()
                if remaining is not None:
                    self.reserve(devices.TERMINATION + devices.RESTORATION + 60 + remaining)
                    # Align the fake clock exactly with the deadline the fixture wrote.
                    window = devices.held_window(["iPhone"], "session-a")
                    now = devices.WindowWait(window, 60).latest - timedelta(seconds=remaining)
                began = now

                def busy(label, *, timeout, **options):
                    nonlocal now
                    now += timedelta(seconds=timeout)
                    raise TimeoutError("Resource queue busy")

                with patch.object(devices, "datetime", wraps=datetime) as clock, patch.object(reservations, "datetime", clock), \
                        patch.object(resources, "Admission", side_effect=busy) as admission, \
                        patch.object(cli, "refresh_policy", return_value="policy"), \
                        patch.object(cli, "source_identity", return_value={}), \
                        contextlib.redirect_stdout(StringIO()), contextlib.redirect_stderr(StringIO()) as error:
                    clock.now.side_effect = lambda zone: now.astimezone(zone)
                    started = time.monotonic()
                    status = cli.main(["--project", str(root), "run", "--if-free", "device"])
                    spent = time.monotonic() - started
                said = error.getvalue()
                self.assertEqual(status, resources.BUSY, said)
                admission.assert_called_once()
                # The probe waits the cap, less at most the real time the run took before it, which a wait that counts
                # down on time.monotonic loses, and timedelta's rounding to a microsecond: on a loaded Mac an exact
                # comparison failed with 4.999999 != 5 (vocaro/queuekeeper#258).
                cap, probed = cli.FREE_WAIT if remaining is None else remaining, (now - began).total_seconds()
                self.assertLessEqual(probed, cap)
                self.assertGreaterEqual(probed, cap - spent - 1e-6)
                self.assertRegex(said, r"queuekeeper: device not run on this Mac .*: Resource queue busy")
                self.assertEqual("queuekeeper: device stopped waiting: the window on iPhone ends " in said, remaining is not None)
                self.assertEqual("No device result was recorded" in said, remaining is not None)
                self.assertFalse(ran.exists())
                self.assertFalse((root / ".build/queuekeeper/device/source.json").exists())

    def test_a_default_timeout_inside_a_window_with_time_left_blames_no_window(self):
        root, ran = self.lane(devices=["iPhone"])
        self.reserve(3600)
        self.hold(0, 1, 2)
        with patch.object(cli.remote, "OVERFLOW_TIMEOUT", 0.05):
            status, error = self.run_lane(root, "device")
        self.assertEqual(status, resources.BUSY, error)
        self.assertIn("queuekeeper: device not run on this Mac", error)
        self.assertIn("Resource queue busy", error)
        self.assertNotIn("stopped waiting", error)
        self.assertFalse(ran.exists())

    def test_a_window_with_time_left_runs_the_lane_and_reports_what_is_left(self):
        root, ran = self.lane(devices=["iPhone"])
        self.reserve(3600)
        self.measure("Beta device", 7000)  # An old release entry does not establish a pass.
        status, error = self.run_lane(root, "device")
        self.assertEqual(status, 0, error)
        self.assertTrue(ran.exists())
        self.assertIn("queuekeeper: device waits for admission at most until ", error)
        self.assertIn("its full-pass budget of 120s", error)
        self.assertRegex(error, r"devices: iPhone after [\d.]+s waiting for admission: \d+s left in the reservation")

    def test_a_start_that_cannot_finish_is_not_made_even_on_a_free_host(self):
        root, ran = self.lane(devices=["iPhone"])
        self.reserve(3600)
        self.measure("Beta device", 7000, 7200, 6800, keys=["iPhone"])
        status, error = self.run_lane(root, "device")
        self.assertEqual(status, resources.BUSY, error)
        self.assertRegex(error, r"queuekeeper: device not run on this Mac.*: the window on iPhone ends ")
        self.assertIn("its measured 7000s", error)
        self.assertNotIn("stopped waiting", error)
        self.assertFalse(ran.exists())
        # A run with arguments is not the run those measured, and fits.
        status, error = self.run_lane(root, "device", "--", "one test")
        self.assertEqual(status, 0, error)
        self.assertTrue(ran.exists())

    def test_a_measured_run_that_fits_starts(self):
        root, ran = self.lane(devices=["iPhone"])
        self.reserve(3600)
        self.measure("Beta device", 600, keys=["iPhone"])
        status, error = self.run_lane(root, "device")
        self.assertEqual(status, 0, error)
        self.assertIn("its measured 600s", error)
        self.assertTrue(ran.exists())

    def test_a_lane_that_names_no_device_is_not_bound_by_a_window_the_session_holds(self):
        """Negative control: a measured time that would refuse a device lane, on a lane that does not use the device."""
        root, ran = self.lane(name="gate")
        self.reserve(3600)
        self.measure("Beta gate", 7000)
        status, error = self.run_lane(root, "gate")
        self.assertEqual(status, 0, error)
        self.assertTrue(ran.exists())
        self.assertNotIn("window", error)
        self.assertNotIn("waits for admission", error)

    def test_a_device_another_session_holds_gives_this_session_no_window(self):
        root, ran = self.lane(devices=["iPhone"])
        self.reserve(3600, owner="session-b")
        self.measure("Beta device", 7000)
        status, error = self.run_lane(root, "device")
        self.assertEqual(status, 0, error)
        self.assertTrue(ran.exists())
        self.assertNotIn("waits for admission", error)

    def test_the_earliest_of_the_lanes_devices_is_its_window(self):
        self.lane()
        self.reserve(3600)
        self.reserve(1800, key="iPad")
        self.reserve(60, key="Other")
        window = devices.held_window(["iPhone", "iPad"], "session-a")
        self.assertEqual(sorted(window["devices"]), ["iPad", "iPhone"])
        self.assertLess(devices.moment(window["until"]), datetime.now().astimezone() + timedelta(seconds=1801))
        self.assertGreater(devices.moment(window["until"]), datetime.now().astimezone() + timedelta(seconds=1000))
        self.assertIsNone(devices.held_window(["iPhone"], "session-b"))
        self.assertIsNone(devices.held_window([], "session-a"))

    def test_the_wait_is_the_time_left_before_the_run_and_its_cleanup(self):
        until = (datetime.now().astimezone() + timedelta(seconds=3600)).isoformat()
        waiting = devices.WindowWait({"devices": ["iPhone"], "until": until, "cleanupSeconds": 100}, 500)
        self.assertAlmostEqual(waiting.seconds(5400), 3000, delta=120)
        self.assertTrue(waiting.limited)
        # The shorter default timeout stands, and then the window cut nothing short.
        waiting = devices.WindowWait({"devices": ["iPhone"], "until": until, "cleanupSeconds": 100}, 500)
        self.assertEqual(waiting.seconds(60), 60)
        self.assertFalse(waiting.limited)
        # No measurement leaves the shortest bounded run.
        waiting = devices.WindowWait({"devices": ["iPhone"], "until": until, "cleanupSeconds": 100})
        self.assertAlmostEqual(waiting.seconds(5400), 3600 - 100 - devices.MINIMUM, delta=120)

    def test_work_a_lease_sent_is_bound_by_the_leases_window(self):
        """The window comes from the Mac that sent the work, on a lane that names no device; a window past its last
        start leaves work no run has measured no time to wait."""
        root, ran = self.lane(name="gate")
        over = (datetime.now().astimezone() + timedelta(seconds=1)).isoformat()
        window = json.dumps({"devices": {"iPhone": "UDID"}, "until": over, "cleanupSeconds": 100, "budgetSeconds": None})
        command = [sys.executable, "-c", f"open({str(ran)!r}, 'w')"]
        self.hold(0, 1, 2)
        with patch.dict(os.environ, {devices.WINDOW: window}):
            status, error = self.run_lane(root, "gate")
        self.assertEqual(status, resources.BUSY, error)
        self.assertIn("queuekeeper: gate stopped waiting: the window on iPhone ends ", error)
        self.assertFalse(ran.exists())
        with patch.dict(os.environ, {devices.WINDOW: window}), patch.object(cli, "refresh_policy", return_value="p"), \
                patch.object(cli, "source_identity", return_value={}), contextlib.redirect_stderr(StringIO()) as said:
            status = cli.main(["--project", str(root), "admit", "--here", "--", *command])
        self.assertEqual(status, resources.BUSY, said.getvalue())
        self.assertIn("queuekeeper: the command stopped waiting: the window on iPhone ends ", said.getvalue())
        self.assertFalse(ran.exists())
        # Its label's measured time refuses the same command before any wait.
        self.measure(" ".join(command), 60)
        with patch.dict(os.environ, {devices.WINDOW: window}), patch.object(cli, "refresh_policy", return_value="p"), \
                patch.object(cli, "source_identity", return_value={}), contextlib.redirect_stderr(StringIO()) as said:
            status = cli.main(["--project", str(root), "admit", "--here", "--", *command])
        self.assertEqual(status, resources.BUSY, said.getvalue())
        self.assertRegex(said.getvalue(), r"queuekeeper: the command not run on this Mac.*: the window on iPhone ends ")
        self.assertFalse(ran.exists())

    def test_only_a_filtered_device_lane_leaves_an_unestimated_run_to_its_reservation_bound(self):
        root, ran = self.lane(devices=["iPhone"])
        self.reserve(1)
        status, error = self.run_lane(root, "device")
        self.assertEqual(status, resources.BUSY, error)
        self.assertFalse(ran.exists())
        status, error = self.run_lane(root, "device", "--", "filter")
        self.assertEqual(status, 0, error)
        self.assertTrue(ran.exists())

    def test_a_full_pass_budget_and_cleanup_must_fit_even_with_a_shorter_passing_sample(self):
        keys = ["iPad", "iPhone"]
        root, ran = self.lane(devices=keys, deviceRunMinutes=90)
        self.reserve(5500)  # 5400 seconds for the run fits, but the additional 165 seconds do not.
        self.measure("Beta device", 5300, keys=keys)
        self.measure("Beta device", 36000, keys=["iPhone"])
        status, error = self.run_lane(root, "device")
        self.assertEqual(status, resources.BUSY, error)
        self.assertIn("its full-pass budget of 5400s", error)
        self.assertIn("165s kept for cleanup", error)
        self.assertFalse(ran.exists())
        self.assertFalse((root / ".build/queuekeeper/device/results/device-pass.json").exists())

    def test_a_longer_passing_sample_overrides_the_configured_budget(self):
        keys = ["iPad", "iPhone"]
        root, ran = self.lane(devices=keys, deviceRunMinutes=90)
        self.reserve(5900)
        self.measure("Beta device", 6000, keys=keys)
        status, error = self.run_lane(root, "device")
        self.assertEqual(status, resources.BUSY, error)
        self.assertIn("its measured 6000s", error)
        self.assertFalse(ran.exists())

    def test_a_cutoff_crossed_during_preparation_or_provisioning_refuses_the_start(self):
        root, ran = self.lane(devices=["iPad", "iPhone"], deviceRunMinutes=90)
        self.reserve(7200)
        window = devices.held_window(["iPhone"], "session-a")
        latest = devices.WindowWait(window, budget=5400).latest
        for step in ("reap_here", "provisioned_here"):
            with self.subTest(step=step):
                now = latest - timedelta(seconds=60)

                def prepare(*_args):
                    nonlocal now
                    now = latest + timedelta(seconds=1)

                with patch.object(devices, "datetime", wraps=datetime) as clock, \
                        patch.object(reservations, "datetime", clock), patch.object(cli, step, side_effect=prepare) as run:
                    clock.now.side_effect = lambda zone: now.astimezone(zone)
                    status, error = self.run_lane(root, "device")
                run.assert_called_once()
                self.assertEqual(status, resources.BUSY, error)
                self.assertIn("No device result was recorded", error)
                self.assertFalse(ran.exists())

    def test_only_a_judged_whole_pass_on_both_devices_records_a_sample(self):
        keys = ["iPad", "iPhone"]
        command = """import json, os, sys
from pathlib import Path
keys = ["iPad"] if os.environ.get("PARTIAL") else ["iPad", "iPhone"]
parts = [{"key": key, "outcome": "passed"} for key in keys]
(Path(os.environ["QUEUEKEEPER_RESULTS_DIR"]) / "device-pass.json").write_text(json.dumps({"status": 0, "devices": parts}))
print("Ran " + os.environ.get("TEST_COUNT", "12") + " tests")
sys.exit(int(os.environ.get("EXIT", "0")))
"""
        root, _ = self.lane(devices=keys, minimumTests=12, command=[sys.executable, "-c", command])
        for variables, args, expected in [({}, ["--", "filter"], 0), ({}, ["--expect", "12"], 0),
                                           ({"PARTIAL": "1"}, [], 0), ({"TEST_COUNT": "6"}, [], 2),
                                           ({"EXIT": "3"}, [], 3), ({"EXIT": "1"}, [], 1)]:
            with self.subTest(variables=variables, args=args), patch.dict(os.environ, variables):
                status, error = self.run_lane(root, "device", *args)
                self.assertEqual(status, expected, error)
                self.assertIsNone(resources.measured_seconds("Beta device", devices=keys))
        status, error = self.run_lane(root, "device")
        self.assertEqual(status, 0, error)
        self.assertIsNotNone(resources.measured_seconds("Beta device", devices=keys))

    def test_measured_time_is_the_median_of_the_latest_runs_that_ended_on_their_own(self):
        self.assertIsNone(resources.measured_seconds("Beta device"))
        self.measure("Beta device", 9000, 100, 200, 300, 400, 500)
        self.measure("Beta device", 90000, outcome="timed-out")
        self.measure("Beta gate", 90000)
        self.assertEqual(resources.measured_seconds("Beta device"), 300)
        keys = ["iPad", "iPhone"]
        self.assertIsNone(resources.measured_seconds("Beta device", devices=keys))
        self.measure("Beta device", 9000, 100, 200, 300, 400, 500, keys=keys)
        self.measure("Beta device", 2, 2, 2)
        self.measure("Beta device", 2, 2, 2, keys=["iPad"])
        self.assertEqual(resources.measured_seconds("Beta device", devices=keys), 300)

    def test_a_lanes_devices_come_from_its_key_or_a_device_pass_up_to_its_own_arguments(self):
        words = ["python3", "scripts/qk", "device-pass", "--device", "iPad", "--device=iPhone", "--wait", "0", "--",
                 "run", "--device", "{udid}"]
        self.assertEqual(config.lane_devices({"command": words}), ["iPad", "iPhone"])
        self.assertEqual(config.lane_devices({"command": ["run.py"], "devices": ["iPhone"]}), ["iPhone"])
        self.assertEqual(config.lane_devices({"command": ["run.py", "--device", "x"]}), [])
        self.assertEqual(config.lane_devices({"parallel": ["a"]}), [])

    def test_a_lanes_devices_are_named_entries_of_an_admitted_lane(self):
        source = {"schemaVersion": 1, "name": "Beta", "lanes": {"device": {"command": ["run"], "devices": ["iPhone"]}}}
        self.assertEqual(config.parse(json.dumps(source))["lanes"]["device"]["devices"], ["iPhone"])
        for wrong in ([], "iPhone", [""], [1]):
            with self.subTest(wrong), self.assertRaisesRegex(ValueError, "device.devices must be a non-empty list"):
                source["lanes"]["device"]["devices"] = wrong
                config.parse(json.dumps(source))
        source["lanes"]["device"].update(devices=["iPhone"], resource="self")
        with self.assertRaisesRegex(ValueError, "device.devices needs an admitted lane"):
            config.parse(json.dumps(source))

    def test_a_device_budget_is_a_positive_integer_on_an_admitted_device_lane(self):
        lane = {"command": ["run"], "devices": ["iPad", "iPhone"], "deviceRunMinutes": 90}
        source = {"schemaVersion": 1, "name": "Beta", "lanes": {"device": lane}}
        self.assertEqual(config.parse(json.dumps(source))["lanes"]["device"]["deviceRunMinutes"], 90)
        for wrong in (0, -1, 1.5, True):
            with self.subTest(wrong), self.assertRaisesRegex(ValueError, "deviceRunMinutes must be a positive integer"):
                lane["deviceRunMinutes"] = wrong
                config.parse(json.dumps(source))
        lane["deviceRunMinutes"] = 90
        del lane["devices"]
        with self.assertRaisesRegex(ValueError, "deviceRunMinutes needs an admitted device lane"):
            config.parse(json.dumps(source))


class ForegroundWindowWaitTests(unittest.TestCase):
    """A foreground lane run here under the session's mac-foreground reservation waits for admission only until the
    last start that can finish before the reservation's expected release, as a device lane does in its device's
    window (vocaro/queuekeeper#362). No screen is taken: what a run past admission would clear is stubbed to end it
    there with ADMITTED."""
    setUp, tearDown = ResourceTests.setUp, ResourceTests.tearDown
    hold = ResourceTests.hold
    project, marker, run_lane = LaneLabelTests.project, LaneLabelTests.marker, LaneLabelTests.run_lane
    measure = DeviceWindowWaitTests.measure
    ADMITTED = 77

    def lane(self):
        """Beta with a foreground lane, screen, on this Mac, a workstation, in a device record of this test's, the
        session being session-a."""
        from queuekeeper import plugins
        environment = patch.dict(os.environ, {devices.OVERRIDE: str(Path(self.temp.name) / "device-handoff.json"),
                                              devices.OWNER: "session-a"})
        environment.start()
        self.addCleanup(environment.stop)
        for patcher in (patch.object(reservations, "takes_foreground", return_value=False),
                        patch.object(plugins.FOREGROUND, "guarded_alerts", return_value=self.ADMITTED)):
            patcher.start()
            self.addCleanup(patcher.stop)
        ran = self.marker(Path("Beta"), "screen", "ran")
        return self.project("Beta", screen={"command": [sys.executable, "-c", f"open({str(ran)!r}, 'w')"],
                                            "resource": "heavy", "foreground": True, "timeout": 120}), ran

    def reserve(self, seconds, owner="session-a"):
        until = (datetime.now().astimezone() + timedelta(seconds=seconds)).isoformat()
        devices.Record().reserve(devices.FOREGROUND, owner=owner, revision=0, purpose="the owner's window",
                                 release_by=until)

    def test_a_window_that_runs_out_during_the_wait_ends_it_busy_with_nothing_run(self):
        root, ran = self.lane()
        self.reserve(3600)
        self.hold(0, 1, 2)
        with patch.object(reservations.WindowWait, "left", return_value=0.05):
            status, error = self.run_lane(root, "--here", "screen")
        self.assertEqual(status, resources.BUSY, error)
        self.assertIn("queuekeeper: screen not run on this Mac", error)
        self.assertIn("queuekeeper: screen stopped waiting: the window on mac-foreground ends ", error)
        self.assertIn("Nothing ran", error)
        self.assertNotIn("device result", error)
        self.assertFalse(ran.exists())

    def test_a_measured_run_that_fits_is_admitted_after_saying_its_last_start(self):
        root, ran = self.lane()
        self.reserve(3600)
        self.measure("Beta screen", 600)
        status, error = self.run_lane(root, "--here", "screen")
        self.assertEqual(status, self.ADMITTED, error)
        self.assertIn("queuekeeper: screen waits for admission at most until ", error)
        self.assertIn("the window on mac-foreground ends ", error)
        self.assertIn("its measured 600s", error)

    def test_a_whole_run_with_no_measurement_needs_the_timeout_its_lane_sets_to_fit(self):
        """A lane that sets none is unestimated, and fits a 30-minute window, as tests/apple/test_devices.py's
        foreground lane does."""
        root, ran = self.lane()
        self.reserve(3600)
        status, error = self.run_lane(root, "--here", "screen")
        self.assertEqual(status, self.ADMITTED, error)
        self.assertIn("its full-pass budget of 120s", error)

    def test_a_start_that_cannot_finish_is_not_made_even_on_a_free_host(self):
        root, ran = self.lane()
        self.reserve(3600)
        self.measure("Beta screen", 7000, 7200, 6800)
        status, error = self.run_lane(root, "--here", "screen")
        self.assertEqual(status, resources.BUSY, error)
        self.assertRegex(error, r"queuekeeper: screen not run on this Mac.*: the window on mac-foreground ends ")
        self.assertIn("its measured 7000s", error)
        self.assertIn("Nothing ran", error)
        self.assertNotIn("stopped waiting", error)
        self.assertFalse(ran.exists())
        # A run with arguments is not the run those measured, and fits.
        status, error = self.run_lane(root, "--here", "screen", "--", "one test")
        self.assertEqual(status, self.ADMITTED, error)
        self.assertIn("no measured run of it, so the shortest bounded run", error)

    def test_an_unmeasured_run_past_the_last_start_is_refused_too(self):
        """No bounded run enforces the foreground's window, as the device runner does a device's, so a run with
        arguments is refused once even the shortest run and its cleanup no longer fit."""
        root, ran = self.lane()
        self.reserve(reservations.TERMINATION + reservations.RESTORATION + reservations.MINIMUM - 5)
        status, error = self.run_lane(root, "--here", "screen", "--", "one test")
        self.assertEqual(status, resources.BUSY, error)
        self.assertRegex(error, r"queuekeeper: screen not run on this Mac.*: the window on mac-foreground ends ")
        self.assertFalse(ran.exists())

    def test_foreground_work_admitted_here_is_bound_the_same_way(self):
        root, ran = self.lane()
        self.reserve(reservations.TERMINATION + reservations.RESTORATION + reservations.MINIMUM - 5)
        command = [sys.executable, "-c", f"open({str(ran)!r}, 'w')"]
        with patch.object(cli, "refresh_policy", return_value="policy"), \
                patch.object(cli, "source_identity", return_value={}), \
                contextlib.redirect_stdout(StringIO()), contextlib.redirect_stderr(StringIO()) as said:
            status = cli.main(["--project", str(root), "admit", "--foreground", "--here", "--", *command])
        self.assertEqual(status, resources.BUSY, said.getvalue())
        self.assertRegex(said.getvalue(), r"queuekeeper: the command not run on this Mac.*: the window on mac-foreground "
                                          r"ends .*\. Nothing ran")
        self.assertFalse(ran.exists())

    def test_a_lane_that_takes_no_foreground_is_not_bound_by_the_foreground_window(self):
        """Negative control: a measured time that would refuse the foreground lane, on a lane that takes no screen."""
        root, ran = self.lane()
        gate = self.project("Gamma", gate={"command": [sys.executable, "-c", "pass"], "resource": "heavy",
                                           "timeout": 120})
        self.reserve(3600)
        self.measure("Gamma gate", 7000)
        status, error = self.run_lane(gate, "--here", "gate")
        self.assertEqual(status, 0, error)
        self.assertNotIn("window", error)
        self.assertNotIn("waits for admission", error)

    def test_the_window_is_the_sessions_reservation_with_an_expected_release_on_a_workstation(self):
        self.lane()
        self.reserve(1800)
        window = reservations.foreground_window("session-a")
        self.assertEqual(window["devices"], [reservations.FOREGROUND])
        self.assertEqual(window["cleanupSeconds"], reservations.TERMINATION + reservations.RESTORATION)
        self.assertGreater(reservations.moment(window["until"]), datetime.now().astimezone() + timedelta(seconds=1000))
        self.assertIsNone(reservations.foreground_window("session-b"))
        self.assertIsNone(reservations.foreground_window(None))
        # A Mac that takes foreground work by itself holds the entry for each run, which may outlast its release.
        with patch.object(reservations, "takes_foreground", return_value=True):
            self.assertIsNone(reservations.foreground_window("session-a"))
        # An entry without an expected release bounds nothing.
        value = devices.Record().load()
        value["devices"][reservations.FOREGROUND].pop("expectedReleaseBy")
        record = type("Record", (), {"load": lambda self: value})()
        self.assertIsNone(reservations.foreground_window("session-a", record))


class EntryPointTests(unittest.TestCase):
    def test_every_committed_script_with_a_shebang_is_executable(self):
        # A checkout on another Mac takes its modes from the index, not this working tree.
        root = Path(__file__).resolve().parents[2]
        listing = subprocess.run(["git", "ls-files", "-s", "bin", "scripts"], cwd=root, capture_output=True,
                                 text=True, check=True).stdout.splitlines()
        self.assertTrue(listing)
        for line in listing:
            mode, _, _, path = line.split(maxsplit=3)
            if (root / path).read_bytes().startswith(b"#!"):
                self.assertEqual(mode, "100755", path)


class DeviceReachTests(unittest.TestCase):
    """A lane on a device the owner declared runs only on a Mac that reaches it by a declared way, and says how
    (vocaro/queuekeeper#211). Synthetic Macs, owner-mac (this one) and studio, read as told, and a synthetic iPhone."""
    setUp, tearDown = ResourceTests.setUp, ResourceTests.tearDown
    project, marker, run_lane = LaneLabelTests.project, LaneLabelTests.marker, LaneLabelTests.run_lane
    PHONE = "00008130-001A2C3E0C38001C"

    def lane(self, absent="offline", missing_since=None):
        """Beta's device lane on the iPhone, declared cable then network, in a record of this test's own."""
        from queuekeeper_mobile import reach
        record = Path(self.temp.name) / "device-handoff.json"
        for patcher in (patch.dict(os.environ, {devices.OVERRIDE: str(record), devices.OWNER: "session-a"}),
                        patch.object(reach, "mac_here", return_value="owner-mac")):
            patcher.start()
            self.addCleanup(patcher.stop)
        record.write_text(json.dumps({"vocaroDeviceHandoff": 1, "revision": 1, "devices": {
            "iPhone": {"device": self.PHONE, "state": "released", "revision": 1}},
            "declarations": {"iPhone": {"reach": ["cable", "network"], "absent": absent}}}))
        if missing_since is not None:
            reach.history_path(record).write_text(json.dumps({"version": 1, "devices": {self.PHONE: {
                "reachedAt": None, "missingSince": missing_since, "ways": {}}}}))
        ran = self.marker(Path("Beta"), "device", "ran")
        return self.project("Beta", device={"command": [sys.executable, "-c", f"open({str(ran)!r}, 'w')"],
                                            "resource": "heavy", "here": "a physical device", "timeout": 120,
                                            "devices": ["iPhone"]}), ran

    def reads(self, here=None, there=None):
        """Each Mac's reading: the way it reaches the iPhone, or None for not listing it."""
        from queuekeeper_mobile import reach

        def one(mac, host, way):
            found = {} if way is None else {self.PHONE: {
                "way": way, "transport": "wired" if way == "cable" else "localNetwork", "kind": "iPhone",
                "model": "iPhone", "name": "iPhone"}}
            return {"mac": mac, "host": host, "readAt": 0, "devices": found, "seconds": 0.1}
        return patch.object(reach, "readings", return_value=[one("owner-mac", None, here),
                                                             one("studio", "studio.test", there)])

    def test_a_lane_this_mac_reaches_by_cable_runs_and_its_receipt_says_how(self):
        root, ran = self.lane()
        with self.reads(here="cable", there="network"):
            status, error = self.run_lane(root, "device")
        self.assertEqual(status, 0, error)
        self.assertTrue(ran.exists())
        self.assertIn("queuekeeper: device runs on owner-mac, which reaches iPhone by cable", error)
        receipt = json.loads((root / ".build/queuekeeper/device/source.json").read_text())
        self.assertEqual(receipt["deviceReach"], {"mac": "owner-mac", "ways": {"iPhone": "cable"}, "fallback": []})

    def test_a_lane_only_another_mac_reaches_is_not_run_and_says_how_to_send_it(self):
        root, ran = self.lane()
        with self.reads(there="network"):
            status, error = self.run_lane(root, "device")
        self.assertEqual(status, resources.BUSY, error)
        self.assertFalse(ran.exists())
        self.assertIn("`qk remote run --device iPhone --host studio device`", error)

    def test_a_device_declared_always_there_that_no_mac_reaches_holds_the_lane(self):
        root, ran = self.lane(absent="error", missing_since=time.time() - 1000)
        with self.reads():
            status, error = self.run_lane(root, "device")
        self.assertEqual(status, remote.MISCONFIGURED, error)
        self.assertIn("iPhone, declared always reachable, is reached by no Mac", error)
        self.assertFalse(ran.exists())

    def test_a_device_that_may_be_away_leaves_the_lane_not_run_when_no_mac_reaches_it(self):
        root, ran = self.lane(missing_since=time.time() - 1000)
        with self.reads():
            status, error = self.run_lane(root, "device")
        self.assertEqual(status, resources.BUSY, error)
        self.assertIn("iPhone is offline since", error)
        self.assertFalse(ran.exists())

    def test_remote_run_goes_to_the_mac_that_reaches_the_device_and_refuses_one_that_does_not(self):
        root, _ran = self.lane()
        farm_fixture.use(self, farm_fixture.mac("other.test", "other"), farm_fixture.mac("studio.test", "studio"))
        sent = []

        def execute(root, config, host, *rest, **options):
            sent.append(host)
            return 0
        with self.reads(here="cable", there="network"), \
                patch.object(remote.plugins.DEVICES, "lease", return_value=None), \
                patch.object(remote, "execute", side_effect=execute), \
                contextlib.redirect_stderr(StringIO()) as error:
            self.assertEqual(cli.main(["--project", str(root), "remote", "run", "--device", "iPhone", "device"]), 0,
                             error.getvalue())
            self.assertEqual(sent, ["studio.test"])
            self.assertIn("device falls back: iPhone by network, not by cable, which only owner-mac reaches it by",
                          error.getvalue())
            self.assertEqual(cli.main(["--project", str(root), "remote", "run", "--device", "iPhone", "--host", "other",
                                       "device"]), resources.BUSY)
        self.assertEqual(sent, ["studio.test"])
        self.assertIn("is reached by a declared way only from owner-mac, studio", error.getvalue())
