"""What every project's Mac VoiceOver session runner shares on a Mac that keeps its state, the second Mac
(guides/second-mac-foreground.md): a session run again when VoiceOver moved to XCTest's Automation Running
banner, which that Mac cannot unload, and a stop that restores VoiceOver before anything else.

    from vocaro_engineering.voiceover.guard import (BANNER_STOP, BannerNamed, Stopped, check_banner,
                                                    run_again, stop_signals)

A driver calls check_banner with what VoiceOver has said before each step, exits BANNER_STOP when it raises
BannerNamed, and runs its session inside stop_signals, catching Stopped to restore VoiceOver at once. A session
runner runs each session through run_again, which runs it again while the banner stopped it.
"""
import contextlib
import signal

# XCTest's banner, a window of its own that can take the VoiceOver cursor.
BANNER = "Automation Running"
# A driver's exit status for a session stopped because VoiceOver named the banner: unmeasured, never passed or
# failed, and run again.
BANNER_STOP = 123
# How many times a session runs while the banner stops it. On the second Mac, whose System Integrity Protection
# keeps the banner loaded, the kernel ended it for its 15 MB memory limit and launchd started it again about every
# 10 s, and VoiceOver moved to a new one in two of EPUBLib's seven sessions there, two of the three at 600 points
# (2026-10-30). The owner keeps SIP there and has such sessions run again (owner's decision, 2026-10-30).
BANNER_ATTEMPTS = 3
# The signals a stop sends: eng's, when the dispatching Mac's connection ends or the session asks, is SIGTERM,
# then SIGKILL processes.FOREGROUND_KILL_AFTER seconds later for a foreground run.
STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)


class BannerNamed(RuntimeError):
    """VoiceOver named the banner, so its cursor may be there and not on the app under test."""


class Stopped(BaseException):
    """A stop signal, raised where the session is, so that its restoration runs. Not an Exception, so that no
    handler of a step's failure takes it."""


def check_banner(phrases, moment):
    """Raise BannerNamed when any phrase VoiceOver said names the banner; moment says when, for the message."""
    if any(BANNER in phrase for phrase in phrases):
        raise BannerNamed(f"VoiceOver named XCTest's {BANNER} banner {moment}, so its cursor may be on the banner "
                          "and not on the app; session stopped")


@contextlib.contextmanager
def stop_signals():
    """Within the block a stop signal raises Stopped, once: later ones are ignored, so the restoration that
    follows runs to its end. Without it a stop ends the driver at once with VoiceOver on, which a Mac that keeps
    its state then keeps, as the second Mac did on 2026-10-30. The previous handlers come back after the block."""
    def stopped(number, frame):
        for later in STOP_SIGNALS:
            signal.signal(later, signal.SIG_IGN)
        raise Stopped(f"Stopped by {signal.Signals(number).name}")
    handlers = {number: signal.signal(number, stopped) for number in STOP_SIGNALS}
    try:
        yield
    finally:
        for number, handler in handlers.items():
            signal.signal(number, handler)


def run_again(session, may_run_again=lambda: True, attempts=BANNER_ATTEMPTS):
    """Run session(attempt), which returns its exit status and transcript, until it ends otherwise than
    BANNER_STOP, at most attempts times, and again only while may_run_again() allows, as the run's time does.
    Return the last status and transcript, and the transcripts of the earlier attempts the banner stopped,
    which are unmeasured and kept."""
    stopped = []
    while True:
        status, transcript = session(len(stopped) + 1)
        if status != BANNER_STOP or len(stopped) + 1 >= attempts or not may_run_again():
            return status, transcript, stopped
        stopped.append(transcript)
