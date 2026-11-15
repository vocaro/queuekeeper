"""Read complete recorded logs in resumable chunks without hiding earlier failures."""
import contextlib
from io import StringIO
import json
import os
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src/python"))
import host_isolation  # noqa: F401 -- first, as every test module
from queuekeeper import agent_logs, merge_queue


class LogChunks(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "run.log"
        self.artifact = {"kind": "run-log", "path": str(self.path), "location": "local",
                         "producedOn": "build-host", "available": True}

    def test_from_beginning_and_resume_returns_every_byte_even_after_append(self):
        text = "FAIL: first test\n" + "later output\n" * 3000
        self.path.write_text(text)
        first = agent_logs.chunk(self.artifact, limit=32)
        self.assertTrue(first["content"].startswith("FAIL: first test\n"))
        self.assertTrue(first["hasMore"])
        self.assertEqual(first["offset"], 0)
        with self.path.open("a") as stream: stream.write("last output\n")
        found, page = [first["content"]], first
        while page["hasMore"]:
            page = agent_logs.chunk(self.artifact, cursor=page["nextCursor"], limit=4096)
            found.append(page["content"])
        self.assertEqual("".join(found), text + "last output\n")
        self.assertEqual(page["nextOffset"], self.path.stat().st_size)
        self.assertFalse(page["truncated"])
        self.assertEqual(self.path.read_text(), text + "last output\n")
        # Keep the cursor after EOF: output appended afterward is still read exactly once.
        with self.path.open("a") as stream: stream.write("after EOF\n")
        self.assertEqual(agent_logs.chunk(self.artifact, cursor=page["nextCursor"])["content"], "after EOF\n")

    def test_multibyte_boundaries_and_explicit_offsets_are_lossless(self):
        text = "ab🌱é漢字\n" * 7
        self.path.write_text(text)
        parts, offset = [], 0
        while offset < self.path.stat().st_size:
            page = agent_logs.chunk(self.artifact, offset=offset, limit=4)
            self.assertLessEqual(page["nextOffset"] - offset, 4)
            self.assertGreater(page["nextOffset"], offset)
            parts.append(page["content"])
            offset = page["nextOffset"]
        self.assertEqual("".join(parts), text)
        with self.assertRaisesRegex(agent_logs.LogError, "splits a UTF-8"):
            agent_logs.chunk(self.artifact, offset=3)

    def test_partial_character_at_current_end_is_held_until_append_finishes_it(self):
        encoded = "🌱".encode()
        self.path.write_bytes(b"ab" + encoded[:2])
        first = agent_logs.chunk(self.artifact, limit=4)
        self.assertEqual(first["content"], "ab")
        self.assertEqual(first["pendingBytes"], 2)
        self.assertTrue(first["waitingForBytes"])
        self.assertEqual(first["nextOffset"], 2)
        with self.path.open("ab") as stream: stream.write(encoded[2:])
        page = agent_logs.chunk(self.artifact, cursor=first["nextCursor"], limit=4)
        self.assertEqual(page["content"], "🌱")
        self.assertFalse(page["hasMore"])

    def test_settled_incomplete_utf8_ends_with_explicit_replacement_instead_of_stalling(self):
        self.path.write_bytes(b"ab\xf0\x9f")
        page = agent_logs.chunk(self.artifact, complete=True)
        self.assertEqual(page["content"], "ab\ufffd")
        self.assertEqual(page["nextOffset"], 4)
        self.assertEqual(page["pendingBytes"], 0)
        self.assertFalse(page["waitingForBytes"])
        self.assertFalse(page["hasMore"])

    def test_malformed_utf8_does_not_block_cursor_or_explicit_byte_positions(self):
        raw = b"abcd\x80rest\xff\xfe\xc3\x00\xc2"
        self.path.write_bytes(raw)
        page = agent_logs.chunk(self.artifact, limit=4, complete=True)
        parts = [page["content"]]
        while page["hasMore"]:
            old_offset = page["nextOffset"]
            page = agent_logs.chunk(self.artifact, cursor=page["nextCursor"], limit=4, complete=True)
            self.assertEqual(page["offset"], old_offset)
            self.assertGreater(page["nextOffset"], old_offset)
            parts.append(page["content"])
        self.assertEqual("".join(parts), raw.decode("utf-8", errors="replace"))
        self.assertEqual(page["nextOffset"], len(raw))
        explicit = agent_logs.chunk(self.artifact, offset=4, limit=4, complete=True)
        self.assertEqual(explicit["content"], "\ufffdres")
        self.path.write_bytes(b"\x80bad")
        self.assertEqual(agent_logs.chunk(self.artifact, complete=True)["content"], "\ufffdbad")

    def test_nonregular_log_is_rejected_without_waiting_for_a_writer(self):
        os.mkfifo(self.path)
        with self.assertRaises(agent_logs.LogError) as raised: agent_logs.chunk(self.artifact)
        self.assertEqual(raised.exception.code, "log-unavailable")

    def test_cursor_detects_rotation_truncation_and_rewritten_returned_bytes(self):
        for change in ("rotate", "truncate", "rewrite"):
            with self.subTest(change=change):
                self.path.write_text("FAIL: first test\n" + "later output\n" * 20)
                first = agent_logs.chunk(self.artifact, limit=32)
                if change == "rotate":
                    self.path.rename(self.path.with_suffix(".old"))
                    self.path.write_text("another log\n" * 30)
                elif change == "truncate": self.path.write_text("short\n")
                else:
                    with self.path.open("r+b") as stream: stream.write(b"PASS")
                with self.assertRaises(agent_logs.LogError) as raised:
                    agent_logs.chunk(self.artifact, cursor=first["nextCursor"])
                self.assertEqual(raised.exception.code, "log-cursor-stale")

    def test_invalid_requests_and_remote_or_pruned_logs_are_explicit(self):
        self.path.write_text("output\n")
        for options in ({"offset": -1}, {"offset": 100}, {"limit": 0}, {"limit": 1 << 22}, {"cursor": "bad"}):
            with self.subTest(options=options), self.assertRaises(agent_logs.LogError) as raised:
                agent_logs.chunk(self.artifact, **options)
            self.assertEqual(raised.exception.status, 2)
        first = agent_logs.chunk(self.artifact, limit=4)
        with self.assertRaisesRegex(agent_logs.LogError, "not both"):
            agent_logs.chunk(self.artifact, offset=1, cursor=first["nextCursor"])
        with self.assertRaises(agent_logs.LogError) as raised:
            agent_logs.chunk({**self.artifact, "location": "remote"})
        self.assertEqual(raised.exception.code, "log-unavailable")
        self.path.unlink()
        with self.assertRaises(agent_logs.LogError) as raised: agent_logs.chunk(self.artifact)
        self.assertEqual(raised.exception.code, "log-unavailable")

    def test_selector_requires_full_path_for_ambiguous_kinds_and_cursor_pins_file(self):
        self.path.write_text("output\n")
        other = {**self.artifact, "path": str(self.path.parent / "other.log")}
        logs = [self.artifact, other]
        with self.assertRaisesRegex(agent_logs.LogError, "More than one"):
            agent_logs.selected(logs, "run-log")
        self.assertEqual(agent_logs.selected(logs, str(self.path)), self.artifact)
        first = agent_logs.chunk(self.artifact, limit=4)
        self.assertEqual(agent_logs.selected(list(reversed(logs)), cursor=first["nextCursor"]), self.artifact)
        with self.assertRaises(agent_logs.LogError) as raised:
            agent_logs.selected([other], cursor=first["nextCursor"])
        self.assertEqual(raised.exception.code, "log-cursor-stale")

    def test_default_selects_live_then_exact_dispatch_then_primary_or_latest_gate(self):
        old = {**self.artifact, "kind": "queue-log", "path": "old.log"}
        latest = {**old, "path": "latest.log"}
        primary = {**old, "path": "primary.log", "primary": True}
        current = {**old, "path": "current.log", "current": True}
        self.assertEqual(agent_logs.selected([old, current, self.artifact, primary, latest]), current)
        self.assertEqual(agent_logs.selected([old, self.artifact, primary, latest]), self.artifact)
        self.assertEqual(agent_logs.selected([old, primary, latest]), primary)
        self.assertEqual(agent_logs.selected([old, latest]), latest)


class LogCommand(unittest.TestCase):
    def test_json_is_one_document_with_artifacts_and_error_details(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gate.log"
            path.write_text("FAIL: earliest failure\n" + "progress\n" * 10)
            queue = SimpleNamespace(read=lambda identifier: {"id": identifier, "state": "testing"},
                                    working=lambda: True,
                                    round_state=lambda: {"gates": {"gate": {
                                        "entries": ["entry-1"], "log": str(path), "mac": "build-host", "lane": "gate"}}})
            args = SimpleNamespace(id="entry-1", json=True, offset=0, limit=32, artifact=None, cursor=None)
            with patch.object(merge_queue, "Queue", return_value=queue):
                output = StringIO()
                with contextlib.redirect_stdout(output): status = agent_logs.main(Path(directory), args)
                found = json.loads(output.getvalue())
                self.assertEqual(status, 0)
                self.assertEqual(found["schemaVersion"], 1)
                self.assertEqual(found["id"], "entry-1")
                self.assertTrue(found["content"].startswith("FAIL: earliest failure\n"))
                self.assertEqual(found["artifact"]["path"], str(path))
                self.assertEqual(found["artifact"]["producedOn"], "build-host")
                path.unlink()
                output = StringIO()
                with contextlib.redirect_stdout(output): status = agent_logs.main(Path(directory), args)
                found = json.loads(output.getvalue())
                self.assertEqual(status, 1)
                self.assertEqual(found["reasonCode"], "log-unavailable")
                self.assertFalse(found["available"])
                self.assertFalse(found["artifacts"][0]["available"])

    def test_no_recorded_log_and_unknown_entry_produce_json_errors(self):
        queue = SimpleNamespace(read=lambda identifier: {"id": identifier, "state": "queued"}, working=lambda: False)
        args = SimpleNamespace(id="entry-1", json=True, offset=0, limit=32, artifact=None, cursor=None)
        with patch.object(merge_queue, "Queue", return_value=queue):
            output = StringIO()
            with contextlib.redirect_stdout(output): status = agent_logs.main(Path("."), args)
            self.assertEqual(status, 1)
            self.assertEqual(json.loads(output.getvalue())["reasonCode"], "log-not-recorded")
            args.id = "../elsewhere"
            output = StringIO()
            with contextlib.redirect_stdout(output): status = agent_logs.main(Path("."), args)
            self.assertEqual(status, 2)
            self.assertEqual(json.loads(output.getvalue())["reasonCode"], "usage-or-configuration-error")


if __name__ == "__main__":
    unittest.main()
