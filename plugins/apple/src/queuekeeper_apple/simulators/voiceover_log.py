"""VoiceOver's own log of a pool member a `qk simulator-tests --voiceover --voiceover-log` run holds, kept beside its
xcodebuild log (vocaro/queuekeeper#358).

XCTest's XCUIVoiceOverService.currentSpeech() gives only the latest utterance, so a phrase spoken and then replaced,
as while XCUIApplication.launch() blocks, is lost to the tests. VoiceOver's own process, vot, logs every speech
request at debug level: a `SEND req:… OutputRequest:` entry and `Action: '…'` lines (iOS 27.2 24B8117, Xcode 27B52).
That log reads only while the member is booted: a copy of its log store taken after it shuts down does not read back,
so it is streamed from just after the boot until the member's tests end, as the pool's template warm-up streams it
(template.log_stream).

The stream is large, about 7 MB a member for a three-minute run, and the logs folder is overwritten by the next run.
By default it is kept whole, since which lines a reviewer needs is not settled; `--voiceover-log speech` keeps only
the speech requests' entries (speech_entries) and the stream's own first lines."""
import contextlib
import os
from pathlib import Path
import re

from queuekeeper import processes
from . import template

# Each log of a member, beside its xcodebuild log KIND.log.
SUFFIX = ".voiceover.log"
# What --voiceover-log keeps: every line of the stream, or the speech requests' entries.
KEEP = ("full", "speech")
# Text that marks an entry as part of a speech request: vot's request and its actions, and the speech extension's
# line for each phrase it speaks (template.SPEECH).
SPEECH_MARKERS = ("SEND req:", "OutputRequest:", "Action:", template.SPEECH)
# The start of each entry in `log stream --style compact`; a line without it continues the entry before it.
ENTRY = re.compile(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d")


def path(logs, kind):
    return Path(logs) / f"{kind}{SUFFIX}"


def speech_entries(lines):
    """The lines of a compact stream that belong to a speech request's entry, each entry with its continuation lines,
    and the lines before the first entry, in which the stream says what it filters."""
    entry, started = [], False
    for line in lines:
        if ENTRY.match(line):
            if not started or any(marker in text for text in entry for marker in SPEECH_MARKERS): yield from entry
            entry, started = [line], True
        else:
            entry.append(line)
    if not started or any(marker in text for text in entry for marker in SPEECH_MARKERS): yield from entry


class Stream:
    """VoiceOver's log of the booted simulator udid, written to path from start() until stop(). The stream runs in a
    session of its own, so that a stop the run receives reaches it only through stop(), which every exit path of the
    member's run calls. start=template.log_stream, the command that starts it."""

    def __init__(self, udid, path, keep="full", start=template.log_stream):
        self.udid, self.path, self.keep, self.starter = udid, Path(path), keep, start
        self.process = None

    def start(self):
        """Start streaming; OSError when the stream cannot start."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.process = self.starter(self.udid, self.path)

    def stop(self):
        """End the stream, keep what keep asks for, and return what was kept, as a sentence; None when it never
        started. RuntimeError when the stream would not end on SIGINT, SIGTERM or SIGKILL (processes.stop)."""
        if self.process is None: return None
        process, self.process = self.process, None
        early = process.poll()
        processes.stop(process)
        if not self.path.is_file(): return f"the stream wrote no file at {self.path}"
        total = kept = 0
        if self.keep == "speech":
            written = self.path.with_name(f"{self.path.name}.{os.getpid()}")
            try:
                with self.path.open(errors="replace") as source, written.open("w") as target:
                    def counted():
                        nonlocal total
                        for line in source:
                            total += 1
                            yield line
                    for line in speech_entries(counted()):
                        target.write(line)
                        kept += 1
                os.replace(written, self.path)
            finally:
                with contextlib.suppress(FileNotFoundError): written.unlink()
            what = f"{kept} of {total} lines, those of its speech requests,"
        else:
            with self.path.open("rb") as source: kept = sum(1 for _ in source)
            what = f"{kept} lines"
        ended = f"; the stream had ended by itself with status {early}" if early is not None else ""
        return f"{what} in {self.path} ({self.path.stat().st_size} bytes){ended}"
