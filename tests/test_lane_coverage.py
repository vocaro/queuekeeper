"""lane_coverage: every test has a lane, some test has none, and could not tell are three answers (#289)."""
import contextlib
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/python"))
import host_isolation  # noqa: F401 -- isolates this module's host state as every test module's
from queuekeeper import cli, config, lane_coverage
from queuekeeper.lane_coverage import COULD_NOT_TELL, EVERY_TEST_HAS_A_LANE, NO_TESTS_FOUND, TESTS_WITHOUT_A_LANE

MANIFEST = 'let package = Package(name: "P", targets: [.testTarget(name: "PTests")])\n'
XCTEST = "import XCTest\nfinal class {name}: XCTestCase {{\n    func testIt() {{ }}\n}}\n"
SWIFT_TESTING = '@testable import P\nimport Testing\n\n@Suite("words")\nstruct {name} {{\n    @Test func it() {{ }}\n}}\n'
SCRIPT = ["bash", "scripts/ci.sh"]


def lane(*command):
    return {"command": list(command)}


class Project(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.files = []

    def write(self, path, text=""):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        self.files.append(path)

    def package(self):
        self.write("Package.swift", MANIFEST)
        self.write("Tests/PTests/Covered.swift", XCTEST.format(name="Covered"))

    def report(self, lanes, **settings):
        return lane_coverage.report(self.root, {"lanes": lanes, "testLanes": settings}, tracked=self.files)


class Answers(Project):
    def test_a_test_a_lane_selects_is_every_test_has_a_lane(self):
        self.package()
        result = self.report({"tests": lane("swift", "test", "--jobs", "4")})
        self.assertEqual((result["result"], result["tests"], result["withLane"]), (EVERY_TEST_HAS_A_LANE, 1, 1))
        self.assertEqual(lane_coverage.doctor_warnings(result), [])

    def test_a_test_no_lane_selects_is_named_once_every_lane_was_read(self):
        self.package()
        self.write("Examples/Host/UITests/Stale.swift", XCTEST.format(name="Stale"))
        result = self.report({"tests": lane("swift", "test"), "docs": lane("python3", "scripts/qk", "docs", "check")})
        self.assertEqual(result["result"], TESTS_WITHOUT_A_LANE)
        self.assertEqual(result["noLane"], ["Examples/Host/UITests/Stale.swift: Stale"])
        self.assertEqual(result["undetermined"], {})
        warning, = lane_coverage.doctor_warnings(result)
        self.assertIn("1 test(s) belong to no lane", warning)
        self.assertIn("Stale", warning)

    def test_an_unread_lane_makes_an_unselected_test_could_not_tell_and_never_a_pass_or_a_finding(self):
        self.package()
        self.write("Examples/Host/UITests/Stale.swift", XCTEST.format(name="Stale"))
        result = self.report({"tests": lane("swift", "test"), "ui": lane(*SCRIPT)})
        self.assertEqual(result["result"], COULD_NOT_TELL)
        self.assertEqual(result["noLane"], [])  # it may be in the unread lane, so it is not reported as having none
        self.assertEqual(result["undetermined"], {"Examples/Host/UITests": 1})
        self.assertIn("bash scripts/ci.sh", result["unreadLanes"]["ui"])
        warning, = lane_coverage.doctor_warnings(result)
        self.assertIn("could not tell", warning)
        self.assertIn("not a pass", warning)

    def test_an_unread_lane_does_not_matter_once_a_read_lane_selects_every_test(self):
        """could-not-tell's negative control: the unread lane can add nothing to a test already known to have a lane."""
        self.package()
        result = self.report({"tests": lane("swift", "test"), "ui": lane(*SCRIPT)})
        self.assertEqual(result["result"], EVERY_TEST_HAS_A_LANE)
        self.assertIn("ui", result["unreadLanes"])

    def test_a_test_file_that_cannot_be_read_is_could_not_tell_though_every_other_test_has_a_lane(self):
        self.package()
        self.files.append("Tests/PTests/Gone.swift")
        result = self.report({"tests": lane("swift", "test")})
        self.assertEqual(result["result"], COULD_NOT_TELL)
        self.assertIn("Tests/PTests/Gone.swift", result["problems"][0])
        self.assertIn("not a pass", lane_coverage.doctor_warnings(result)[0])

    def test_a_project_git_fails_to_list_is_could_not_tell(self):
        self.package()
        failed = subprocess.CompletedProcess([], 128, b"", b"fatal: index file corrupt")
        with patch.object(lane_coverage.subprocess, "run", return_value=failed):
            result = lane_coverage.report(self.root, {"lanes": {"tests": lane("swift", "test")}})
        self.assertEqual(result["result"], COULD_NOT_TELL)
        self.assertIn("could not be listed", result["problems"][0])
        self.assertIn("not a pass", lane_coverage.doctor_warnings(result)[0])

    def test_a_directory_that_is_no_repository_tracks_no_test(self):
        """The negative control of the last: no failure of git, and no pass either."""
        self.package()
        result = lane_coverage.report(self.root, {"lanes": {"tests": lane("swift", "test")}})
        self.assertEqual((result["result"], result["problems"]), (NO_TESTS_FOUND, []))

    def test_finding_no_test_is_no_pass_and_no_warning(self):
        self.write("Sources/P/P.swift", "struct P { }\n")
        result = self.report({"build": lane("swift", "build")})
        self.assertEqual(result["result"], NO_TESTS_FOUND)
        self.assertEqual(lane_coverage.doctor_warnings(result), [])


class Excluded(Project):
    def test_a_skipped_path_is_out_and_counted(self):
        self.package()
        self.write("Examples/Kept/Record.swift", XCTEST.format(name="Record"))
        result = self.report({"tests": lane("swift", "test")}, skip={"Examples/Kept/": "a qualification kept as a record"})
        self.assertEqual((result["result"], result["tests"], result["skipped"]), (EVERY_TEST_HAS_A_LANE, 1, 1))

    def test_a_skip_key_without_a_slash_is_one_file(self):
        self.package()
        self.write("Examples/Kept/Record.swift", XCTEST.format(name="Record"))
        self.write("Examples/Kept/Other.swift", XCTEST.format(name="Other"))
        result = self.report({"tests": lane("swift", "test")}, skip={"Examples/Kept/Record.swift": "a record"})
        self.assertEqual(result["noLane"], ["Examples/Kept/Other.swift: Other"])

    def test_measurements_evidence_and_vendored_code_stay_out_without_being_named(self):
        self.package()
        for path in ["measurements/a/T.swift", "evidence/b/T.swift", "Sources/ThirdParty/x/T.swift"]:
            self.write(path, XCTEST.format(name="Frozen"))
        result = self.report({"tests": lane("swift", "test")})
        self.assertEqual((result["result"], result["tests"], result["skipped"]), (EVERY_TEST_HAS_A_LANE, 1, 3))

    def test_a_nested_folder_named_evidence_is_still_read(self):
        """The negative control: only the top of a project holds its record directories."""
        self.package()
        self.write("Examples/evidence/T.swift", XCTEST.format(name="Live"))
        self.assertEqual(self.report({"tests": lane("swift", "test")})["result"], TESTS_WITHOUT_A_LANE)


class Declared(Project):
    def test_a_stated_selection_answers_for_a_lane_that_cannot_be_read(self):
        self.write("App/UITests/Flow.swift", XCTEST.format(name="Flow"))
        result = self.report({"ui": lane(*SCRIPT)}, selects={"ui": ["App/UITests/"]})
        self.assertEqual((result["result"], result["unreadLanes"]), (EVERY_TEST_HAS_A_LANE, {}))

    def test_a_stated_selection_that_misses_a_test_finds_it(self):
        self.write("App/UITests/Flow.swift", XCTEST.format(name="Flow"))
        result = self.report({"ui": lane(*SCRIPT)}, selects={"ui": []})
        self.assertEqual(result["result"], TESTS_WITHOUT_A_LANE)

    def test_a_selection_stated_for_no_lane_is_could_not_tell(self):
        self.package()
        result = self.report({"tests": lane("swift", "test")}, selects={"gone": []})
        self.assertEqual(result["result"], COULD_NOT_TELL)
        self.assertIn("gone", result["problems"][0])

    def test_engineering_json_refuses_a_malformed_setting(self):
        for bad in [{"skip": {"a/": ""}}, {"selects": {"x": "a/"}}, {"other": {}}, []]:
            with self.subTest(bad), self.assertRaisesRegex(ValueError, "testLanes"):
                config.parse(json.dumps({"schemaVersion": 1, "name": "P", "testLanes": bad}))
        config.parse(json.dumps({"schemaVersion": 1, "name": "P", "testLanes": {"skip": {"a/": "why"}, "selects": {"x": []}}}))

    def test_the_schema_names_the_setting(self):
        schema = json.loads((config.TOOLKIT_ROOT / "schemas/queuekeeper.schema.json").read_text())
        self.assertEqual(set(schema["properties"]["testLanes"]["properties"]), {"skip", "selects"})


class Reading(Project):
    def selected(self, *command):
        return self.report({"lane": lane(*command)})

    def test_swift_test_reads_the_named_package_and_a_target_path(self):
        self.write("Sub/Package.swift", '.testTarget(name: "S", path: "Checks/S")')
        self.write("Sub/Checks/S/A.swift", XCTEST.format(name="A"))
        self.write("Tests/PTests/Other.swift", XCTEST.format(name="Other"))
        result = self.selected("swift", "test", "--package-path", "Sub")
        self.assertEqual((result["withLane"], result["noLane"]), (1, ["Tests/PTests/Other.swift: Other"]))

    def test_eng_xctest_reads_as_swift_test_does(self):
        self.package()
        self.assertEqual(self.selected("python3", "scripts/qk", "xctest", "--jobs", "6")["result"], EVERY_TEST_HAS_A_LANE)

    def test_a_filter_selects_the_classes_it_matches_and_skip_removes_them(self):
        self.package()
        self.write("Tests/PTests/Other.swift", XCTEST.format(name="Other"))
        result = self.selected("env", "A=1", "swift", "test", "--filter", "PTests.Covered")
        self.assertEqual(result["noLane"], ["Tests/PTests/Other.swift: Other"])
        result = self.selected("swift", "test", "--skip", "Other$")
        self.assertEqual(result["noLane"], ["Tests/PTests/Other.swift: Other"])

    def test_a_filter_that_may_name_single_tests_cannot_be_read(self):
        self.package()
        for pattern in ["Covered/testIt", "testIt"]:  # one spells a test, and the other matches no class
            with self.subTest(pattern):
                self.assertEqual(self.selected("swift", "test", "--filter", pattern)["result"], COULD_NOT_TELL)

    def test_a_manifest_that_computes_its_targets_cannot_be_read(self):
        self.write("Package.swift", "let package = Package(name: \"P\", targets: names.map(Target.testTarget))\n")
        self.write("Tests/PTests/A.swift", XCTEST.format(name="A"))
        self.assertEqual(self.selected("swift", "test")["result"], COULD_NOT_TELL)

    def test_a_missing_manifest_cannot_be_read(self):
        self.write("Tests/PTests/A.swift", XCTEST.format(name="A"))
        self.assertIn("Package.swift", self.selected("swift", "test")["unreadLanes"]["lane"])

    def test_a_scheme_or_a_whole_target_cannot_be_read_but_a_named_class_can(self):
        self.write("App/AUITests/Flow.swift", XCTEST.format(name="Flow"))
        self.assertEqual(self.selected("xcodebuild", "test", "-scheme", "App")["result"], COULD_NOT_TELL)
        self.assertEqual(self.selected("xcodebuild", "test", "-only-testing:AUITests")["result"], COULD_NOT_TELL)
        self.assertEqual(self.selected("xcodebuild", "test", "-skip-testing:AUITests/Flow")["result"], COULD_NOT_TELL)
        self.assertEqual(self.selected("xcodebuild", "test", "-only-testing:AUITests/Flow/testIt")["result"],
                         EVERY_TEST_HAS_A_LANE)
        self.assertEqual(self.selected("xcodebuild", "test", "-only-testing:AUITests/Elsewhere")["result"],
                         TESTS_WITHOUT_A_LANE)

    def test_eng_simulator_tests_reads_its_named_classes_as_xcodebuild_does(self):
        self.write("App/AUITests/Flow.swift", XCTEST.format(name="Flow"))
        run = ("python3", "scripts/qk", "simulator-tests", "--scheme", "P", "--device", "iPhone-17-Pro", "--")
        self.assertEqual(self.selected(*run, "-only-testing:AUITests/Flow")["result"], EVERY_TEST_HAS_A_LANE)
        self.assertEqual(self.selected(*run, "-only-testing:AUITests/Elsewhere")["result"], TESTS_WITHOUT_A_LANE)
        # Negative controls: what a scheme holds, a whole target and a skip are not spelled out class by class.
        for words in ((), ("-only-testing:AUITests",), ("-skip-testing:AUITests/Flow",)):
            with self.subTest(words):
                self.assertEqual(self.selected(*run, *words)["result"], COULD_NOT_TELL)

    def test_commands_that_run_no_test_select_none_and_are_read(self):
        self.write("App/AUITests/Flow.swift", XCTEST.format(name="Flow"))
        for command in (["xcodebuild", "build", "-scheme", "A"], ["swift", "build"], ["swift", "run", "Tool"],
                        ["python3", "scripts/qk", "spelling"], ["python3", "scripts/qk", "run", "--serial", "group"]):
            with self.subTest(command):
                self.assertEqual(self.selected(*command)["result"], TESTS_WITHOUT_A_LANE)

    def test_a_group_of_lanes_needs_no_reading(self):
        self.package()
        result = self.report({"tests": lane("swift", "test"), "all": {"parallel": ["tests"]}})
        self.assertEqual((result["result"], result["unreadLanes"]), (EVERY_TEST_HAS_A_LANE, {}))

    def python(self):
        self.write("tests/test_a.py", "import unittest\nclass T(unittest.TestCase):\n    def test_x(self): pass\n")

    def test_unittest_selects_by_start_directory_and_pattern(self):
        self.python()
        self.write("tools/test_b.py", "def test_y(): pass\n")
        for command in (["python3", "scripts/qk", "unittest", "-s", "tests", "--split", "test", "-v"],
                        ["python3", "scripts/qk", "unittest", "-s", "tests", "--split", "class", "--batch-size", "4"],
                        ["python3", "-B", "-m", "unittest", "discover", "-s", "tests", "-v"]):
            with self.subTest(command):
                self.assertEqual(self.selected(*command)["noLane"], ["tools/test_b.py: tools.test_b"])
        self.assertEqual(self.selected("python3", "scripts/qk", "unittest", "-s", "tests", "-p", "other*.py")["withLane"], 0)

    def test_unittest_discovery_does_not_reach_a_folder_that_is_no_package(self):
        self.python()
        self.write("tests/plain/test_c.py", "def test_z(): pass\n")
        self.write("tests/pack/__init__.py")
        self.write("tests/pack/test_d.py", "def test_z(): pass\n")
        result = self.selected("python3", "-m", "unittest", "discover", "-s", "tests")
        self.assertEqual(result["noLane"], ["tests/plain/test_c.py: tests.plain.test_c"])

    def test_eng_unittest_names_select_their_module(self):
        self.python()
        self.write("tests/test_b.py", "def test_y(): pass\n")
        result = self.selected("python3", "scripts/qk", "unittest", "-s", "tests", "test_a.T")
        self.assertEqual(result["noLane"], ["tests/test_b.py: tests.test_b"])

    def test_a_name_pattern_cannot_be_read(self):
        self.python()
        self.assertEqual(self.selected("python3", "scripts/qk", "unittest", "-s", "tests", "-k", "x")["result"], COULD_NOT_TELL)

    def test_a_python_file_named_for_tests_that_holds_none_is_no_test(self):
        self.write("tests/testing_support.py", "def helper(): pass\n")
        self.assertEqual(self.selected("swift", "build")["result"], NO_TESTS_FOUND)


class Finding(Project):
    def found(self, **files):
        for name, text in files.items(): self.write(f"{name}.swift", text)
        result = self.report({"none": lane("swift", "build")})
        return sorted(line.split(": ")[1] for line in result["noLane"])

    def test_xctest_classes_and_swift_testing_suites(self):
        self.assertEqual(self.found(A=XCTEST.format(name="A"), B=SWIFT_TESTING.format(name="B")), ["A", "B"])

    def test_a_class_deriving_from_another_files_test_case_is_found_and_a_bare_base_is_not(self):
        self.assertEqual(self.found(
            Base="import XCTest\nclass Base: XCTestCase { func helper() { } }\n",
            Derived="import XCTest\nfinal class Derived: Base { func testIt() { } }\n"), ["Derived"])

    def test_a_test_in_a_struct_without_suite_is_its_structs_and_a_free_test_its_files(self):
        self.assertEqual(self.found(
            InStruct="import Testing\nstruct Plain {\n    @Test func a() { }\n}\n",
            Free="import Testing\n@Test func b() { }\n"), ["Free", "Plain"])

    def test_a_test_in_a_comment_or_a_string_is_no_test(self):
        self.assertEqual(self.found(
            Quiet='import XCTest\n// final class Hidden: XCTestCase { func testA() { } }\n'
                  'let text = """\nfinal class Quoted: XCTestCase { func testB() { } }\n"""\n'
                  'let line = "class Inline: XCTestCase { func testC() { } }"\n'), [])

    def test_a_class_method_declares_no_class(self):
        self.assertEqual(self.found(
            Holder="import XCTest\nfinal class Holder: XCTestCase {\n    class func make() { }\n    func testIt() { }\n}\n"),
            ["Holder"])

    def test_at_test_without_the_testing_import_is_no_test(self):
        self.assertEqual(self.found(Macro="@Test func notSwiftTesting() { }\n"), [])


class Doctor(Project):
    def doctor(self, lanes):
        (self.root / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "P", "integration": "direct-main",
                                                                "lanes": lanes}))
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(["git", "-C", str(self.root), "add", "."], check=True)
        with patch.object(cli.shutil, "which", return_value="/usr/bin/true"), \
                patch.object(cli, "source_identity", return_value={}), \
                patch.object(cli.remote, "compare_hosts", return_value={"errors": []}), \
                patch.object(cli, "fix_hosts", return_value={"errors": []}), \
                contextlib.redirect_stdout(StringIO()) as output:
            status = cli.main(["--project", str(self.root), "doctor"])
        return status, json.loads(output.getvalue())

    def test_doctor_states_every_test_has_a_lane(self):
        self.package()
        status, shown = self.doctor({"tests": {**lane("swift", "test"), "timeout": 60}})
        self.assertEqual((status, shown["testLanes"]["result"]), (0, EVERY_TEST_HAS_A_LANE))
        self.assertFalse([w for w in shown["warnings"] if lane_coverage.RULE in w])

    def test_doctor_warns_of_a_test_with_no_lane_and_still_exits_zero(self):
        """A warning, which changes no gate: whether this ever fails is the owner's to decide (#289)."""
        self.package()
        self.write("Examples/UITests/Stale.swift", XCTEST.format(name="Stale"))
        status, shown = self.doctor({"tests": {**lane("swift", "test"), "timeout": 60}})
        self.assertEqual((status, shown["testLanes"]["result"]), (0, TESTS_WITHOUT_A_LANE))
        self.assertTrue(any("belong to no lane" in w and "Stale" in w for w in shown["warnings"]))

    def test_doctor_keeps_could_not_tell_apart_from_a_pass(self):
        self.package()
        self.write("Examples/UITests/Stale.swift", XCTEST.format(name="Stale"))
        status, shown = self.doctor({"ui": {**lane(*SCRIPT), "timeout": 60}})
        self.assertEqual((status, shown["testLanes"]["result"]), (0, COULD_NOT_TELL))
        self.assertTrue(any("could not tell" in w for w in shown["warnings"]))


class ThisRepository(unittest.TestCase):
    def test_the_toolkits_python_tests_are_read_as_its_gate_selects_them(self):
        root = config.TOOLKIT_ROOT
        modules = [path.relative_to(root).as_posix() for path in (root / "tests").glob("test_*.py")]
        result = lane_coverage.report(root, config.load(root), tracked=modules)
        self.assertEqual((result["result"], result["withLane"]), (EVERY_TEST_HAS_A_LANE, len(modules)))
        # Over the whole tree: a lane filtered to Swift classes, storekit-tests, reads as unread among Python tests alone.
        whole = lane_coverage.report(root, config.load(root))
        self.assertEqual(whole["unreadLanes"], {})
        for suite in ("AppKitSnapshotTests", "GoldenTests", "OffscreenWindowTests", "SRGBPixelSamplerTests",
                      "VoiceOverSpeechTests", "UIKitSnapshotTests", "SpeechFixtureTests"):
            with self.subTest(suite):
                self.assertFalse([line for line in whole["noLane"] if line.endswith(f": {suite}")], "belongs to a gate lane")


if __name__ == "__main__":
    unittest.main()
