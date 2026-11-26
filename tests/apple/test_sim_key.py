import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src/python"))
from queuekeeper_apple.simulators import sim_key
import host_isolation
from host_isolation import stretched

UDID = "0C1F2E3D-4B5A-6978-8796-A5B4C3D2E1F0"
DEVELOPER_DIR = "/Fake/Xcode.app/Contents/Developer"
SOURCE = ROOT / "plugins/apple/src/queuekeeper_apple/_native/sim-key-helper.m"
# Stands in for xcrun: the macOS SDK queries and clang, whose -o file is a stand-in helper that logs its
# arguments and refuses to run until codesign has signed it. Each call is logged to FAKE_LOG.
FAKE_XCRUN = """#!/bin/sh
echo "xcrun $*" >> "$FAKE_LOG"
if [ "$1" = --sdk ] && [ "$2" = macosx ]; then
    shift 2
    [ -n "$FAKE_NO_SDK" ] && { echo "xcrun: error: SDK \\"macosx\\" cannot be located" >&2; exit 1; }
    case "$1" in
        --show-sdk-path) echo "/Fake/Xcode/MacOSX.sdk"; exit 0 ;;
        --show-sdk-build-version) echo "${FAKE_SDK_BUILD:-26A100}"; exit 0 ;;
        clang)
            [ -n "$FAKE_CLANG_FAIL" ] && { echo "sim-key-helper.m:1:1: error: fake" >&2; exit 1; }
            while [ $# -gt 0 ]; do [ "$1" = -o ] && out="$2"; shift; done
            printf '#!/bin/sh\\ngrep -q "^# signed" "$0" || { echo "unsigned helper" >&2; exit 98; }\\n' > "$out"
            printf 'echo "helper $*" >> "$FAKE_LOG"\\nexit "${FAKE_HELPER_STATUS:-0}"\\n' >> "$out"
            exit 0 ;;
    esac
fi
exit 99
"""
FAKE_CODESIGN = """#!/bin/sh
echo "codesign $*" >> "$FAKE_LOG"
[ -n "$FAKE_CODESIGN_FAIL" ] && { echo "fake: signing failed" >&2; exit 1; }
for last; do :; done
printf '# signed\\n' >> "$last"
"""
FAKE_XCODE_SELECT = f"""#!/bin/sh
echo "xcode-select $*" >> "$FAKE_LOG"
echo "{DEVELOPER_DIR}"
"""
NOTICE = [
    "https://github.com/facebook/idb",
    "Copyright (c) Meta Platforms, Inc. and affiliates.",
    "Permission is hereby granted, free of charge, to any person obtaining a copy",
    "The above copyright notice and this permission notice shall be included in all",
    "THE SOFTWARE IS PROVIDED \"AS IS\", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR",
    "SOFTWARE.",
]


class SimKeyTableTests(unittest.TestCase):
    def test_the_keys_a_reader_pages_with_have_their_keyboard_usages(self):
        expected = {"pageup": 0x4B, "pagedown": 0x4E, "home": 0x4A, "end": 0x4D, "right": 0x4F, "left": 0x50,
                    "down": 0x51, "up": 0x52, "return": 0x28, "escape": 0x29, "delete": 0x2A, "tab": 0x2B,
                    "space": 0x2C, "forwarddelete": 0x4C, "a": 0x04, "z": 0x1D, "1": 0x1E, "9": 0x26, "0": 0x27,
                    "f1": 0x3A, "f12": 0x45, "control": 0xE0, "shift": 0xE1, "option": 0xE2, "command": 0xE3}
        for name, code in expected.items():
            with self.subTest(name=name):
                self.assertEqual(sim_key.usage_code(name), code)
        self.assertEqual(len(sim_key.KEYS), 26 + 10 + 12 + 18)
        # Every name has its own usage.
        self.assertEqual(len(set(sim_key.KEYS.values())), len(sim_key.KEYS))

    def test_a_usage_is_taken_in_hexadecimal_and_a_name_only_as_written(self):
        self.assertEqual(sim_key.usage_code("0x4b"), 0x4B)
        self.assertEqual(sim_key.usage_code("0x4B"), 0x4B)
        self.assertEqual(sim_key.usage_code("0xff"), 0xFF)
        # Negative controls: a usage out of range or not in hexadecimal, and names not in the table.
        for key in ("0x0", "0x00", "0x100", "75", "4b", "0x", "PageUp", "page-up", "fn", "f13", "", " a"):
            with self.subTest(key=key):
                self.assertRaises(ValueError, sim_key.usage_code, key)

    def test_keys_joined_with_plus_are_held_and_let_go_in_reverse(self):
        self.assertEqual(sim_key.events("pagedown"), ["down:78", "up:78"])
        self.assertEqual(sim_key.events("shift+right"), ["down:225", "down:79", "up:79", "up:225"])
        self.assertEqual(sim_key.events("command+shift+0x04"),
                         ["down:227", "down:225", "down:4", "up:4", "up:225", "up:227"])
        for key in ("shift+", "+right", "shift++right"):
            with self.subTest(key=key):
                self.assertRaises(ValueError, sim_key.events, key)


class SimKeyTests(unittest.TestCase):
    """scripts/sim-key, run as a qualification runs it, from a copy of the toolkit with fake tools."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name).resolve()
        self.kit = base / "vocaro-engineering"
        (self.kit / "scripts").mkdir(parents=True)
        shutil.copy2(ROOT / "scripts/sim-key", self.kit / "scripts/sim-key")
        shutil.copytree(ROOT / "src/python/queuekeeper", self.kit / "src/python/queuekeeper",
                        ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(ROOT / "plugins", self.kit / "plugins", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy2(ROOT / "queuekeeper-workspace.json", self.kit / "queuekeeper-workspace.json")
        self.source = self.kit / "plugins/apple/src/queuekeeper_apple/_native/sim-key-helper.m"
        self.build = self.kit / ".build/sim-key"
        tools = base / "bin"
        tools.mkdir()
        for name, text in [("xcrun", FAKE_XCRUN), ("codesign", FAKE_CODESIGN), ("xcode-select", FAKE_XCODE_SELECT)]:
            host_isolation.executable(tools / name, text)
        self.log = base / "calls.log"
        self.environment = {key: value for key, value in os.environ.items() if not key.startswith("FAKE_")}
        self.environment.update(PATH=f"{tools}{os.pathsep}{os.environ['PATH']}", FAKE_LOG=str(self.log))

    def tearDown(self):
        self.temp.cleanup()

    def key(self, *arguments, **fakes):
        """Runs the wrapper as an executable, as a project's qualification runs it."""
        return subprocess.run([str(self.kit / "scripts/sim-key"), *arguments], capture_output=True, text=True,
                              env={**self.environment, **fakes}, timeout=stretched(60))

    def calls(self):
        """The fake tools' calls since the last read."""
        lines = self.log.read_text().splitlines() if self.log.exists() else []
        self.log.unlink(missing_ok=True)
        return lines

    def helpers(self):
        return sorted(self.build.iterdir()) if self.build.exists() else []

    def test_a_helper_is_built_and_signed_once_and_rebuilt_only_for_a_changed_source_or_sdk(self):
        first = self.key(UDID, "pagedown")
        self.assertEqual(first.returncode, 0, first.stderr)
        calls = self.calls()
        builds = [call for call in calls if " clang " in call]
        self.assertEqual(len(builds), 1, calls)
        self.assertIn("xcrun --sdk macosx clang -target arm64-apple-macos14.0 -fobjc-arc", builds[0])
        self.assertIn(f"-framework Foundation {self.source} -o {self.build}/", builds[0])
        signs = [call for call in calls if call.startswith("codesign --force --sign - --identifier sim-key-helper ")]
        self.assertEqual(len(signs), 1, calls)
        # Only the signed helper is left, and it is executable.
        [helper] = self.helpers()
        self.assertTrue(os.access(helper, os.X_OK))
        self.assertEqual(calls[-1], f"helper {DEVELOPER_DIR} {UDID} down:78 up:78")
        self.assertIn("sim-key: built", first.stderr)

        second = self.key(UDID, "home", "end")
        self.assertEqual(second.returncode, 0, second.stderr)
        calls = self.calls()
        self.assertEqual([call for call in calls if " clang " in call or call.startswith("codesign")], [])
        self.assertEqual(calls[-1], f"helper {DEVELOPER_DIR} {UDID} down:74 up:74 down:77 up:77")
        self.assertNotIn("built", second.stderr)

        # Negative controls: an edited source, and then another SDK, each build a new helper.
        self.source.write_text(self.source.read_text() + "// edited\n")
        third = self.key(UDID, "up")
        self.assertEqual(third.returncode, 0, third.stderr)
        self.assertEqual(len([call for call in self.calls() if " clang " in call]), 1)
        fourth = self.key(UDID, "up", FAKE_SDK_BUILD="26B5")
        self.assertEqual(fourth.returncode, 0, fourth.stderr)
        self.assertEqual(len([call for call in self.calls() if " clang " in call]), 1)
        self.assertEqual(len(self.helpers()), 3)

    def test_only_a_udid_and_keys_reach_the_helper(self):
        # Negative controls: none of these builds the helper or runs anything.
        refused = [(), (UDID,), ("booted", "pageup"), ("iPad Air", "pageup"), (UDID[:-1], "pageup"),
                   (UDID, "PageUp"), (UDID, "pageup", "fn"), (UDID, "75"), (UDID, "0x100"), (UDID, ""),
                   (UDID, "shift+"), ("pageup", UDID)]
        for arguments in refused:
            with self.subTest(arguments=arguments):
                result = self.key(*arguments)
                self.assertEqual(result.returncode, 64, result.stderr)
                self.assertIn("usage: sim-key UDID KEY...", result.stderr)
                self.assertEqual(self.calls(), [])
        self.assertEqual(self.helpers(), [])
        accepted = [(("pageup",), "down:75 up:75"), (("0x4e",), "down:78 up:78"),
                    (("right", "left"), "down:79 up:79 down:80 up:80"),
                    (("shift+down",), "down:225 down:81 up:81 up:225")]
        for keys, helper_events in accepted:
            with self.subTest(keys=keys):
                result = self.key(UDID, *keys)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.calls()[-1], f"helper {DEVELOPER_DIR} {UDID} {helper_events}")

    def test_a_helper_that_did_not_build_or_sign_is_neither_run_nor_kept(self):
        for fake, message in [("FAKE_NO_SDK", "xcrun --sdk macosx --show-sdk-path failed"),
                              ("FAKE_CLANG_FAIL", "the helper did not build"),
                              ("FAKE_CODESIGN_FAIL", "the helper did not sign")]:
            with self.subTest(fake=fake):
                result = self.key(UDID, "end", **{fake: "1"})
                self.assertEqual(result.returncode, 1)
                self.assertIn(f"sim-key: {message}", result.stderr)
                self.assertFalse([call for call in self.calls() if call.startswith("helper")])
                self.assertEqual(self.helpers(), [])
        # Positive control: the same call builds, signs and runs the helper once nothing fails.
        result = self.key(UDID, "end")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls()[-1], f"helper {DEVELOPER_DIR} {UDID} down:77 up:77")
        self.assertEqual(len(self.helpers()), 1)

    def test_the_exit_status_is_the_helpers(self):
        # 2: not a booted simulator; 3: the HID service cannot be reached, so the caller reports unmeasured.
        for status in ("2", "3"):
            with self.subTest(status=status):
                self.assertEqual(self.key(UDID, "home", FAKE_HELPER_STATUS=status).returncode, int(status))
        # Positive control: a helper that succeeds exits 0.
        self.assertEqual(self.key(UDID, "home").returncode, 0)


@unittest.skipUnless(shutil.which("xcrun") and sys.platform == "darwin", "needs Xcode's macOS SDK")
class SimKeyHelperTests(unittest.TestCase):
    """The real helper, built for this Mac, on paths that reach no simulator."""

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        try:
            cls.helper = sim_key.helper(build=cls.temp.name, source=SOURCE)
        except RuntimeError as error:
            cls.temp.cleanup()
            raise unittest.SkipTest(f"the helper did not build here: {error}")

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def run_helper(self, *arguments):
        return subprocess.run([str(self.helper), *arguments], capture_output=True, text=True, timeout=stretched(60))

    def test_arguments_it_does_not_take_exit_64(self):
        for arguments in [(), ("/Applications/Xcode.app/Contents/Developer", UDID),
                          ("relative/Developer", UDID, "down:75"), ("/Developer", "booted", "down:75"),
                          ("/Developer", UDID, "down:0"), ("/Developer", UDID, "down:256"),
                          ("/Developer", UDID, "down:0x4b"), ("/Developer", UDID, "press:75"),
                          ("/Developer", UDID, "down:75", "up:")]:
            with self.subTest(arguments=arguments):
                result = self.run_helper(*arguments)
                self.assertEqual(result.returncode, 64, result.stderr)
                self.assertIn("usage: sim-key-helper", result.stderr)

    def test_a_developer_directory_without_simulatorkit_exits_3_as_unreachable(self):
        with tempfile.TemporaryDirectory() as developer_dir:
            result = self.run_helper(developer_dir, UDID, "down:75", "up:75")
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("the simulator's HID service cannot be reached: SimulatorKit did not load", result.stderr)


class SimKeySourceTests(unittest.TestCase):
    def test_the_helper_builds_into_a_directory_git_ignores(self):
        self.assertEqual(sim_key.BUILD, ROOT / ".build/sim-key")
        if not (ROOT / ".git").exists():
            self.skipTest("not a Git checkout")
        def ignored(path):
            return subprocess.run(["git", "-C", str(ROOT), "check-ignore", "-q", path]).returncode == 0
        self.assertTrue(ignored(".build/sim-key/sim-key-helper-0123456789abcdef"))
        # Negative control: the helper's source is not ignored.
        self.assertFalse(ignored("plugins/apple/src/queuekeeper_apple/_native/sim-key-helper.m"))

    def test_the_helper_source_and_the_notices_keep_idbs_mit_notice(self):
        def missing(text):
            return [line for line in NOTICE if line not in text]
        self.assertEqual(missing(SOURCE.read_text()), [])
        notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text()
        self.assertIn("## idb", notices)
        self.assertEqual(missing(notices.split("## idb", 1)[1]), [])
        # Negative control: a notice cut short is caught.
        self.assertEqual(missing(SOURCE.read_text().replace("SOFTWARE.", "")), ["SOFTWARE."])

    def test_the_helper_and_the_wrapper_agree_on_the_exit_codes(self):
        text = SOURCE.read_text()
        self.assertIn(f"kExitUnreachable = {sim_key.EXIT_UNREACHABLE},", text)
        self.assertIn(f"kExitUsage = {sim_key.EXIT_USAGE},", text)


if __name__ == "__main__":
    unittest.main()
