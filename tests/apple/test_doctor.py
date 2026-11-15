"""What the Apple extension warns of in `qk doctor` from a project's lanes alone."""
import json
import unittest
import host_isolation
from queuekeeper import config
from queuekeeper_apple import doctor

QK = ["python3", "scripts/qk", "simulator-tests", "--scheme", "Example", "--runtime", "iOS-27-0"]
PHONE, PAD = ["--device", "iPhone-17-Pro"], ["--device=iPad-Air-13-inch-M4"]


class TwoIdiomLanes(unittest.TestCase):
    def warned(self, lanes, **settings):
        return doctor.idiom_warnings({"lanes": lanes, **settings})

    def test_a_lane_that_runs_one_selection_on_an_iphone_and_an_ipad_simulator_is_warned_of(self):
        # vocaro/queuekeeper#105: a simulator-tests lane, and a project's script that takes both types.
        lanes = {"ios": {"command": [*QK, *PHONE, *PAD, "--", "-only-testing:ExampleTests/Views"]},
                 "script": {"command": ["python3", "scripts/ios-tests.py", "landing", *PAD, *PHONE]},
                 "gate": {"parallel": ["ios", "script"]}}
        found = self.warned(lanes)
        self.assertEqual(len(found), 1, found)
        self.assertIn("2 lane(s) run one selection on an iPhone and an iPad simulator, so each test whose result "
                      "does not depend on the idiom runs twice: ios, script;", found[0])
        self.assertIn('"idioms"', found[0])
        self.assertIn("Test dimensions and the snapshot layer", found[0])

    def test_a_lane_of_one_idiom_a_release_lane_and_a_stated_exception_are_not(self):
        both = {"command": [*QK, *PHONE, *PAD, "--"]}
        cases = {
            "one idiom": ({"ios": {"command": [*QK, *PHONE, "--device", "iPhone-17", "--"]}}, {}),
            "a lane for each idiom": ({"phone": {"command": [*QK, *PHONE, "--"]}, "pad": {"command": [*QK, *PAD, "--"]},
                                       "gate": {"parallel": ["phone", "pad"]}}, {}),
            "no device": ({"ios": {"command": ["bash", "scripts/test-ios.sh", "smoke"]}}, {}),
            # The control for platform divergence runs every class on every simulator.
            "a release lane": ({"all": both}, {"release": {"lanes": ["all"]}}),
            "a release group's lane": ({"all": both, "release-suites": {"parallel": ["all"]}},
                                       {"release": {"lanes": ["release-suites"]}}),
            "the project's stated exception": ({"ios": {**both, "idioms": "Every class it selects draws a view; "
                                                        "Tests/Classes.txt lists each as view"}}, {}),
            # A --device after a device pass's own -- names a physical device's UDID, not a simulator type.
            "a device pass": ({"devices": {"command": ["python3", "scripts/qk", "device-pass", "--device", "iPad",
                                                       "--device", "iPhone", "--", "run"]}}, {}),
            "an option with no value": ({"ios": {"command": [*QK, *PHONE, "--device"]}}, {}),
            "no lanes": ({}, {}),
        }
        for name, (lanes, settings) in cases.items():
            with self.subTest(name):
                self.assertEqual(self.warned(lanes, **settings), [])

    def test_only_the_lanes_without_an_exception_are_named(self):
        both = {"command": [*QK, *PHONE, *PAD, "--"]}
        found = self.warned({"a": both, "b": {**both, "idioms": "why"}, "c": both}, release={"lanes": ["c"]})
        self.assertIn("1 lane(s) run one selection on an iPhone and an iPad simulator", found[0])
        self.assertIn("twice: a;", found[0])

    def test_a_lanes_idioms_is_the_reason_in_words(self):
        lane = {"command": [*QK, *PHONE, *PAD, "--"], "idioms": "Every class it selects draws a view"}
        source = {"schemaVersion": 1, "name": "Beta", "extensions": ["queuekeeper_apple"], "lanes": {"ios": lane}}
        self.assertEqual(config.parse(json.dumps(source))["lanes"]["ios"]["idioms"], lane["idioms"])
        for wrong in ("", "  ", True, ["why"], 1):
            with self.subTest(wrong), self.assertRaisesRegex(ValueError, "ios.idioms must say why"):
                lane["idioms"] = wrong
                config.parse(json.dumps(source))
