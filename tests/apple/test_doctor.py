"""What the Apple plugin warns of in `qk doctor` from a project's lanes alone."""
import json
from pathlib import Path
import subprocess
import tempfile
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
        self.assertIn("is not counted", found[0])

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
        source = {"schemaVersion": 1, "name": "Beta", "plugins": ["queuekeeper_apple"], "lanes": {"ios": lane}}
        self.assertEqual(config.parse(json.dumps(source))["lanes"]["ios"]["idioms"], lane["idioms"])
        for wrong in ("", "  ", True, ["why"], 1):
            with self.subTest(wrong), self.assertRaisesRegex(ValueError, "ios.idioms must say why"):
                lane["idioms"] = wrong
                config.parse(json.dumps(source))


class OneSelectionPerDimension(unittest.TestCase):
    """vocaro/queuekeeper#105: only the snapshot tests of a view multiply by appearance or content size."""
    def warned(self, lanes, **settings):
        return doctor.dimension_warnings({"lanes": lanes, **settings})

    def test_a_lane_per_appearance_or_content_size_of_one_selection_is_warned_of(self):
        light = {"command": [*QK, *PHONE, "--appearance", "light", "--", "-only-testing:ExampleUITests"]}
        dark = {"command": [*QK, *PHONE, "--appearance=dark", "--", "-only-testing:ExampleUITests"]}
        # A lane at the default content size and one at an accessibility size, set through simctl in a shell line.
        default = {"command": ["bash", "-c", "xcodebuild test -scheme Example -only-testing:ExampleUITests"]}
        large = {"command": ["bash", "-c", "xcrun simctl ui booted content_size accessibility-extra-large && "
                             "xcodebuild test -scheme Example -only-testing:ExampleUITests"]}
        # One command given two appearances.
        both = {"command": ["python3", "scripts/ios-tests.py", "--appearance", "light", "--appearance", "dark"]}
        found = self.warned({"light": light, "dark": dark, "default": default, "large": large, "both": both,
                             "gate": {"parallel": ["light", "dark"]}})
        self.assertEqual(len(found), 1, found)
        self.assertIn("5 lane(s) run one selection once per appearance or content size, so each test whose result "
                      "depends on neither runs again for each: both, dark, default, large, light;", found[0])
        self.assertIn('"dimensions"', found[0])
        self.assertIn("multiply only the snapshot tests of a view", found[0])

    def test_different_selections_a_release_lane_and_a_stated_exception_are_not(self):
        light = {"command": [*QK, *PHONE, "--appearance", "light", "--", "-only-testing:ExampleUITests"]}
        dark = {"command": [*QK, *PHONE, "--appearance", "dark", "--", "-only-testing:ExampleUITests"]}
        cases = {
            # The measured exception: a deliberately narrow second selection, in the lanes' own words.
            "a narrow second selection": ({"light": light, "dark": {"command": [
                *QK, *PHONE, "--appearance", "dark", "--", "-only-testing:ExampleUITests/SystemDrawnScene"]}}, {}),
            "one appearance": ({"light": light}, {}),
            "the same appearance twice": ({"a": light, "b": dict(light)}, {}),
            "no dimension": ({"a": {"command": [*QK, *PHONE, "--"]}, "b": {"command": [*QK, *PAD, "--"]}}, {}),
            # A wrapper that sets the appearance itself owns the selection, which the lane's words do not show.
            "a wrapper script": ({"ios": {"command": ["bash", "scripts/test-ios.sh", "accessibility"]}}, {}),
            "release lanes": ({"light": light, "dark": dark}, {"release": {"lanes": ["light", "dark"]}}),
            "a release group": ({"light": light, "dark": dark, "all": {"parallel": ["light", "dark"]}},
                                {"release": {"lanes": ["all"]}}),
            "the project's stated exception": ({"light": {**light, "dimensions": "selects only snapshot tests"},
                                                "dark": {**dark, "dimensions": "selects only snapshot tests"}}, {}),
            "an option with no value": ({"a": {"command": [*QK, "--appearance"]}}, {}),
            "no lanes": ({}, {}),
        }
        for name, (lanes, settings) in cases.items():
            with self.subTest(name):
                self.assertEqual(self.warned(lanes, **settings), [])

    def test_the_words_that_name_a_dimension(self):
        named, rest = doctor.dimension_words(["app", "-AppleInterfaceStyle", "Dark",
                                              "-UIPreferredContentSizeCategoryName", "UICTContentSizeCategoryXL",
                                              "--content-size-category=large", "--dynamic-type", "small", "--"])
        self.assertEqual(named, {"appearance": {"Dark"},
                                 "content size": {"UICTContentSizeCategoryXL", "large", "small"}})
        self.assertEqual(rest, ("app", "--"))

    def test_a_lanes_dimensions_is_the_reason_in_words(self):
        lane = {"command": [*QK, *PHONE, "--"], "dimensions": "Selects only snapshot tests"}
        source = {"schemaVersion": 1, "name": "Beta", "plugins": ["queuekeeper_apple"], "lanes": {"ios": lane}}
        self.assertEqual(config.parse(json.dumps(source))["lanes"]["ios"]["dimensions"], lane["dimensions"])
        for wrong in ("", "  ", True, ["why"], 1):
            with self.subTest(wrong), self.assertRaisesRegex(ValueError, "ios.dimensions must say why"):
                lane["dimensions"] = wrong
                config.parse(json.dumps(source))


UI_TEST = """import XCTest

final class {name}: XCTestCase {{
    func testOpens() {{
        let app = XCUIApplication()
        app.launch()
{body}
    }}
}}
"""
HELPER = """import XCTest

extension XCUIElement {
    func appears(within timeout: TimeInterval) -> Bool {
        exists || waitForExistence(timeout: timeout)
    }
    func disappears(within timeout: TimeInterval) -> Bool {
        !exists || waitForNonExistence(timeout: timeout)
    }
}
"""


class CheckFirstWaitHelper(unittest.TestCase):
    """vocaro/queuekeeper#105: a UI test target puts its waits in one helper file that checks first."""
    def warned(self, files):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, text in files.items():
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_text(text)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            if files: subprocess.run(["git", "-C", str(root), "add", *files], check=True)
            return doctor.wait_helper_warnings(root, {})

    def test_direct_waits_in_more_than_one_file_of_a_target_are_warned_of(self):
        wait = '        XCTAssertTrue(app.buttons["Help"].waitForExistence(timeout: 10))'
        gone = '        XCTAssertTrue(app.sheets.firstMatch.waitForNonExistence(timeout: 5))'
        found = self.warned({"ExampleUITests/LaunchTests.swift": UI_TEST.format(name="LaunchTests", body=wait),
                             "ExampleUITests/Settings/SettingsTests.swift":
                                 UI_TEST.format(name="SettingsTests", body=f"{wait}\n{gone}"),
                             # Another target, with its waits in one file, is not counted.
                             "OtherUITests/OtherTests.swift": UI_TEST.format(name="OtherTests", body=f"{wait}\n{wait}")})
        self.assertEqual(len(found), 1, found)
        self.assertIn("1 UI test target(s) call XCTest's waitForExistence or waitForNonExistence directly from more "
                      "than one file", found[0])
        self.assertIn("ExampleUITests: 3 in 2 files, ExampleUITests/Settings/SettingsTests.swift, "
                      "ExampleUITests/LaunchTests.swift.", found[0])
        self.assertIn("`// xctest-wait: REASON`", found[0])

    def test_a_target_with_one_helper_and_marked_waits_is_not(self):
        helped = '        XCTAssertTrue(app.buttons["Help"].appears(within: 10))'
        marked = ('        // xctest-wait: inside the measured launch window\n'
                  '        XCTAssertTrue(app.buttons["Help"].waitForExistence(timeout: 10))\n'
                  '        _ = app.alerts.firstMatch.waitForExistence(timeout: 30)  // xctest-wait: watches for a change')
        cases = {
            "a helper file": {"ExampleUITests/Waits.swift": HELPER,
                              "ExampleUITests/LaunchTests.swift": UI_TEST.format(name="LaunchTests", body=helped),
                              "ExampleUITests/SettingsTests.swift": UI_TEST.format(name="SettingsTests", body=helped)},
            "marked waits": {"ExampleUITests/Waits.swift": HELPER,
                             "ExampleUITests/LaunchTests.swift": UI_TEST.format(name="LaunchTests", body=marked)},
            "a call in a comment": {"ExampleUITests/Waits.swift": HELPER, "ExampleUITests/LaunchTests.swift":
                                    UI_TEST.format(name="LaunchTests", body="        // was waitForExistence(timeout: 9)")},
            # Unit tests that are not UI tests, and recorded originals under evidence/.
            "no UI test": {"Tests/A.swift": "import XCTest\nlet ok = expectation.waitForExistence(timeout: 1)\n",
                           "Tests/B.swift": "import XCTest\nlet ok = expectation.waitForExistence(timeout: 1)\n"},
            "evidence": {"evidence/UITests/A.swift": UI_TEST.format(name="A", body="x.waitForExistence(timeout: 1)"),
                         "evidence/UITests/B.swift": UI_TEST.format(name="B", body="x.waitForExistence(timeout: 1)")},
            "no Swift": {},
        }
        for name, files in cases.items():
            with self.subTest(name):
                self.assertEqual(self.warned(files), [])

    def test_a_folder_that_is_not_a_repository_has_no_warning(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(doctor.wait_helper_warnings(directory, {}), [])
