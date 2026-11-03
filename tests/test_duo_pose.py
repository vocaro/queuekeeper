import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src/python"))
from vocaro_engineering.simulators import duo_pose
import host_isolation
from host_isolation import stretched

UDID = "0C1F2E3D-4B5A-6978-8796-A5B4C3D2E1F0"
# Stands in for xcrun: the SDK queries, clang writing its -o file, and simctl spawn, which refuses
# a helper codesign did not sign. Each call is logged to FAKE_LOG.
FAKE_XCRUN = """#!/bin/sh
echo "xcrun $*" >> "$FAKE_LOG"
if [ "$1" = --sdk ] && [ "$2" = iphonesimulator ]; then
    shift 2
    [ -n "$FAKE_NO_SDK" ] && { echo "xcrun: error: SDK \\"iphonesimulator\\" cannot be located" >&2; exit 1; }
    case "$1" in
        --show-sdk-path) echo "/Fake/Xcode/iPhoneSimulator.sdk"; exit 0 ;;
        --show-sdk-build-version) echo "${FAKE_SDK_BUILD:-24B113}"; exit 0 ;;
        clang)
            [ -n "$FAKE_CLANG_FAIL" ] && { echo "duo-pose-helper.c:1:1: error: fake" >&2; exit 1; }
            while [ $# -gt 0 ]; do [ "$1" = -o ] && out="$2"; shift; done
            printf 'built\\n' > "$out"
            exit 0 ;;
    esac
fi
if [ "$1" = simctl ] && [ "$2" = spawn ]; then
    grep -q signed "$4" || { echo "unsigned helper" >&2; exit 98; }
    exit "${FAKE_SPAWN_STATUS:-0}"
fi
exit 99
"""
FAKE_CODESIGN = """#!/bin/sh
echo "codesign $*" >> "$FAKE_LOG"
[ -n "$FAKE_CODESIGN_FAIL" ] && { echo "fake: signing failed" >&2; exit 1; }
for last; do :; done
printf 'signed\\n' >> "$last"
"""
NOTICE = [
    "Copyright (c) 2026 Artem Novichkov",
    "Permission is hereby granted, free of charge, to any person obtaining a copy",
    "The above copyright notice and this permission notice shall be included in all",
    "THE SOFTWARE IS PROVIDED \"AS IS\", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR",
    "SOFTWARE.",
    "https://github.com/skarol/duoctl",
]


class DuoPoseTests(unittest.TestCase):
    """scripts/duo-pose, run as a lane runs it, from a copy of the toolkit with fake tools."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name).resolve()
        self.kit = base / "vocaro-engineering"
        (self.kit / "scripts").mkdir(parents=True)
        shutil.copy2(ROOT / "scripts/duo-pose", self.kit / "scripts/duo-pose")
        shutil.copytree(ROOT / "src/python/vocaro_engineering", self.kit / "src/python/vocaro_engineering",
                        ignore=shutil.ignore_patterns("__pycache__"))
        self.source = self.kit / "src/objc/Simulator/duo-pose-helper.c"
        self.source.parent.mkdir(parents=True)
        shutil.copy2(ROOT / "src/objc/Simulator/duo-pose-helper.c", self.source)
        self.build = self.kit / ".build/duo-pose"
        tools = base / "bin"
        tools.mkdir()
        for name, text in [("xcrun", FAKE_XCRUN), ("codesign", FAKE_CODESIGN)]:
            (tools / name).write_text(text)
            (tools / name).chmod(0o755)
        self.log = base / "calls.log"
        self.environment = {key: value for key, value in os.environ.items() if not key.startswith("FAKE_")}
        self.environment.update(PATH=f"{tools}{os.pathsep}{os.environ['PATH']}", FAKE_LOG=str(self.log))

    def tearDown(self):
        self.temp.cleanup()

    def pose(self, *arguments, **fakes):
        """Runs the wrapper as an executable, as a lane's --hinge-script runs it."""
        return subprocess.run([str(self.kit / "scripts/duo-pose"), *arguments], capture_output=True, text=True,
                              env={**self.environment, **fakes}, timeout=stretched(60))

    def calls(self):
        """The fake tools' calls since the last read."""
        lines = self.log.read_text().splitlines() if self.log.exists() else []
        self.log.unlink(missing_ok=True)
        return lines

    def helpers(self):
        return sorted(self.build.iterdir()) if self.build.exists() else []

    def test_a_helper_is_built_and_signed_once_and_rebuilt_only_for_a_changed_source_or_sdk(self):
        first = self.pose(UDID, "120")
        self.assertEqual(first.returncode, 0, first.stderr)
        calls = self.calls()
        builds = [call for call in calls if " clang " in call]
        self.assertEqual(len(builds), 1, calls)
        self.assertIn("xcrun --sdk iphonesimulator clang -target arm64-apple-ios27.0-simulator", builds[0])
        self.assertIn(f"-framework CoreFoundation -framework IOKit {self.source} -o {self.build}/", builds[0])
        self.assertEqual(len([call for call in calls if call.startswith("codesign --force --sign - --identifier duo-pose-helper ")]), 1, calls)
        # Only the signed helper is left: no scratch file, and it is executable.
        [helper] = self.helpers()
        self.assertEqual(helper.read_text(), "built\nsigned\n")
        self.assertTrue(os.access(helper, os.X_OK))
        self.assertEqual(calls[-1], f"xcrun simctl spawn {UDID} {helper} hinge 120")
        self.assertIn("duo-pose: built", first.stderr)

        second = self.pose(UDID, "orientation", "landscape-left")
        self.assertEqual(second.returncode, 0, second.stderr)
        calls = self.calls()
        self.assertEqual([call for call in calls if " clang " in call or call.startswith("codesign")], [])
        self.assertEqual(calls[-1], f"xcrun simctl spawn {UDID} {helper} orientation landscape-left")
        self.assertNotIn("built", second.stderr)

        # Negative controls: an edited source, and then another SDK, each build a new helper.
        self.source.write_text(self.source.read_text() + "// edited\n")
        third = self.pose(UDID, "180")
        self.assertEqual(third.returncode, 0, third.stderr)
        calls = self.calls()
        self.assertEqual(len([call for call in calls if " clang " in call]), 1, calls)
        [edited] = [path for path in self.helpers() if path != helper]
        self.assertEqual(calls[-1], f"xcrun simctl spawn {UDID} {edited} hinge 180")
        fourth = self.pose(UDID, "180", FAKE_SDK_BUILD="24C5")
        self.assertEqual(fourth.returncode, 0, fourth.stderr)
        self.assertEqual(len([call for call in self.calls() if " clang " in call]), 1)
        self.assertEqual(len(self.helpers()), 3)

    def test_only_a_udid_and_degrees_or_an_orientation_reach_the_simulator(self):
        # Negative controls: none of these builds the helper or runs anything.
        refused = [(), (UDID,), ("booted", "120"), ("iPhone Duo", "120"), (UDID[:-1], "120"),
                   (UDID, "181"), (UDID, "180.5"), (UDID, "-1"), (UDID, "nan"), (UDID, "1e2"), (UDID, ""),
                   (UDID, "hinge", "120"), (UDID, "120", "120"), (UDID, "orientation"),
                   (UDID, "orientation", "sideways"), (UDID, "orientation", "Portrait"),
                   (UDID, "orientation", "portrait", "pud"), ("orientation", "portrait", UDID)]
        for arguments in refused:
            with self.subTest(arguments=arguments):
                result = self.pose(*arguments)
                self.assertEqual(result.returncode, 64, result.stderr)
                self.assertIn("usage: duo-pose UDID DEGREES", result.stderr)
                self.assertEqual(self.calls(), [])
        self.assertEqual(self.helpers(), [])
        accepted = [("0",), ("120",), ("180",), ("97.5",), ("orientation", "portrait"), ("orientation", "pud"),
                    ("orientation", "landscape-left"), ("orientation", "landscape-right")]
        for rest in accepted:
            with self.subTest(rest=rest):
                result = self.pose(UDID, *rest)
                self.assertEqual(result.returncode, 0, result.stderr)
                helper_arguments = ["hinge", rest[0]] if len(rest) == 1 else list(rest)
                self.assertRegex(self.calls()[-1], f"^xcrun simctl spawn {UDID} \\S+ {' '.join(helper_arguments)}$")

    def test_a_helper_that_did_not_build_or_sign_is_neither_run_nor_kept(self):
        for fake, message in [("FAKE_NO_SDK", "no iOS Simulator SDK"), ("FAKE_CLANG_FAIL", "the helper did not build"),
                              ("FAKE_CODESIGN_FAIL", "the helper did not sign")]:
            with self.subTest(fake=fake):
                result = self.pose(UDID, "120", **{fake: "1"})
                self.assertEqual(result.returncode, 1)
                self.assertIn(f"duo-pose: {message}", result.stderr)
                self.assertFalse([call for call in self.calls() if "simctl" in call])
                self.assertEqual(self.helpers(), [])
        # Positive control: the same call builds, signs and runs the helper once nothing fails.
        result = self.pose(UDID, "120")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.calls()[-1].startswith(f"xcrun simctl spawn {UDID} "))
        self.assertEqual(len(self.helpers()), 1)

    def test_the_exit_status_is_the_spawned_helpers(self):
        self.assertEqual(self.pose(UDID, "0", FAKE_SPAWN_STATUS="3").returncode, 3)
        # Positive control: a helper that succeeds exits 0.
        self.assertEqual(self.pose(UDID, "0").returncode, 0)


class DuoPoseSourceTests(unittest.TestCase):
    def test_the_helper_builds_into_a_directory_git_ignores(self):
        self.assertEqual(duo_pose.BUILD, ROOT / ".build/duo-pose")
        if not (ROOT / ".git").exists():
            self.skipTest("not a Git checkout")
        def ignored(path):
            return subprocess.run(["git", "-C", str(ROOT), "check-ignore", "-q", path]).returncode == 0
        self.assertTrue(ignored(".build/duo-pose/duo-pose-helper-0123456789abcdef"))
        # Negative control: the helper's source is not ignored.
        self.assertFalse(ignored("src/objc/Simulator/duo-pose-helper.c"))

    def test_the_helper_source_keeps_its_mit_notice_and_credits(self):
        def missing(text):
            return [line for line in NOTICE if line not in text]
        text = (ROOT / "src/objc/Simulator/duo-pose-helper.c").read_text()
        self.assertEqual(missing(text), [])
        # Negative control: a notice cut short is caught.
        self.assertEqual(missing(text.replace("SOFTWARE.", "")), ["SOFTWARE."])


if __name__ == "__main__":
    unittest.main()
