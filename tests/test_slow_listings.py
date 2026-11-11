"""A slow listing is logged with what it could have waited on (vocaro-engineering#386)."""
import host_isolation  # noqa: F401 -- first, as every test module

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from vocaro_engineering import offload_host

STATES = """    1 Ss   16:30:41 /sbin/launchd
  501 U       00:03 /Applications/Xcode.app/Contents/Developer/Toolchains/XcodeDefault.xctoolchain/usr/bin/swift-frontend
  502 R       00:02 /Applications/Xcode.app/Contents/Developer/Toolchains/XcodeDefault.xctoolchain/usr/bin/swift-frontend
  503 S    01:00:00 /usr/bin/xcodebuild
  504 S  2-01:00:00 /usr/sbin/cfprefsd
"""


class SlowListings(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.directory, ignore_errors=True))
        patcher = mock.patch.dict(os.environ, {"HEAVY_SLOT_DIR": str(self.directory)})
        patcher.start()
        self.addCleanup(patcher.stop)
        real = subprocess.run
        def run(command, *arguments, **options):
            if list(command[:2]) == ["ps", "-A"]: return subprocess.CompletedProcess(command, 0, STATES, "")
            return real(command, *arguments, **options)
        patcher = mock.patch.object(offload_host.subprocess, "run", run)
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def clock(*times):
        values = iter(times)
        return lambda: next(values)

    def lines(self, name=offload_host.SLOW_LISTING_LOG):
        path = self.directory / name
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_a_listing_under_the_bound_logs_nothing(self):
        self.assertEqual(offload_host.listing(["/bin/echo", "hi"], clock=self.clock(0, 4.9)), "hi\n")
        self.assertEqual(self.lines(), [])

    def test_a_slow_listing_is_logged_with_what_it_could_have_waited_on(self):
        self.assertEqual(offload_host.listing(["/bin/echo", "hi"], 30, clock=self.clock(100, 107.5)), "hi\n")
        [line] = self.lines()
        self.assertEqual((line["command"], line["seconds"], line["outcome"], line["timeout"]),
                         (["/bin/echo", "hi"], 7.5, "ended", 30))
        self.assertEqual(line["processes"], 5)
        self.assertEqual(len(line["uninterruptible"]), 1)
        self.assertIn("501 U", line["uninterruptible"][0])
        self.assertEqual(line["startedDuring"], {"swift-frontend": 2})
        self.assertEqual(len(line["load"]), 3)

    def test_a_listing_that_outlasts_its_timeout_after_the_bound_is_logged_and_answers_none(self):
        self.assertIsNone(offload_host.listing(["/bin/sleep", "5"], 0.2, clock=self.clock(0, 61)))
        [line] = self.lines()
        self.assertEqual((line["outcome"], line["seconds"]), ("timed out", 61))

    def test_a_short_timeout_is_not_logged(self):
        # A bound under the threshold, as a test's own, is the caller's choice and no stall.
        self.assertIsNone(offload_host.listing(["/bin/sleep", "5"], 0.2, clock=self.clock(0, 0.2)))
        self.assertEqual(self.lines(), [])

    def test_a_command_that_cannot_run_is_not_logged(self):
        self.assertIsNone(offload_host.listing([str(self.directory / "missing")], clock=self.clock(0, 9)))
        self.assertEqual(self.lines(), [])

    def test_a_log_that_cannot_be_written_leaves_the_listing_as_it_was(self):
        (self.directory / "file").write_text("")
        with mock.patch.dict(os.environ, {"HEAVY_SLOT_DIR": str(self.directory / "file" / "below")}):
            self.assertEqual(offload_host.listing(["/bin/echo", "hi"], clock=self.clock(0, 9)), "hi\n")
        self.assertFalse(offload_host.log_slow_listing(["ps"], 9, "ended", 60, self.directory / "file" / "below"))

    def test_the_log_keeps_one_previous_generation(self):
        with mock.patch.object(offload_host, "SLOW_LISTING_LIMIT", 100):
            for seconds in (6, 7, 8):
                self.assertTrue(offload_host.log_slow_listing(["ps"], seconds, "ended", 60))
        self.assertEqual(([line["seconds"] for line in self.lines(offload_host.PREVIOUS_SLOW_LISTING_LOG)],
                          [line["seconds"] for line in self.lines()]), ([7], [8]))

    def test_elapsed_reads_each_form_ps_prints(self):
        self.assertEqual([offload_host.elapsed(text) for text in ("00:03", "01:00:00", "2-01:00:00")],
                         [3, 3600, 2 * 86400 + 3600])
        self.assertEqual(offload_host.elapsed("?"), float("inf"))


if __name__ == "__main__":
    unittest.main()
