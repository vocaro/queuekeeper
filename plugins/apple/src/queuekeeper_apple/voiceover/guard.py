"""What every project's Mac VoiceOver session runner shares on a Mac that keeps its state, the second Mac
(doc/guides/foreground.md): a session run again when VoiceOver moved to XCTest's Automation Running
banner, which that Mac cannot unload, and a stop that restores VoiceOver before anything else.

    from queuekeeper_apple.voiceover.guard import (BANNER_STOP, BannerNamed, Stopped, check_banner,
                                                    run_again, stop_signals)

A driver calls check_banner with what VoiceOver has said before each step, exits BANNER_STOP when it raises
BannerNamed, and runs its session inside stop_signals, catching Stopped to restore VoiceOver at once. A session
runner runs each session through run_again, which runs it again while the banner stopped it.
"""
import contextlib
import signal
import threading

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
# The signals a stop sends: queuekeeper's, when the dispatching Mac's connection ends or the session asks, is SIGTERM,
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
def stop_signals(*, only_unhandled=False):
    """Within the block a stop signal raises Stopped, once: later ones are ignored, so the restoration that
    follows runs to its end. Without it a stop ends the driver at once with VoiceOver on, which a Mac that keeps
    its state then keeps, as the second Mac did on 2026-10-30. The previous handlers come back after the block.
    only_unhandled preserves an existing supervisor/driver handler and ignored signals; this lets an outer
    state guard wait for a supervised child to end before restoration instead of interrupting its supervisor."""
    if only_unhandled and threading.current_thread() is not threading.main_thread():
        yield
        return
    numbers = [number for number in STOP_SIGNALS if not only_unhandled or
               signal.getsignal(number) in (signal.SIG_DFL, signal.default_int_handler)]
    def stopped(number, frame):
        for later in numbers:
            signal.signal(later, signal.SIG_IGN)
        raise Stopped(f"Stopped by {signal.Signals(number).name}")
    handlers = {number: signal.signal(number, stopped) for number in numbers}
    try:
        yield
    finally:
        for number, handler in handlers.items():
            signal.signal(number, handler)


@contextlib.contextmanager
def restoring_signals():
    """Defer stops during bounded restoration, then deliver one to its previous handler.

    Enter only after the body (including a supervised child) has ended. A first stop during
    cleanup must not abandon the restoration. Already ignored signals stay ignored. A cleanup
    failure propagates instead of a pending stop, so its unverified-restoration result survives.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    pending = []
    def defer(number, frame):
        if not pending: pending.append((number, frame))
    handlers = {number: signal.getsignal(number) for number in STOP_SIGNALS}
    for number, handler in handlers.items():
        if handler != signal.SIG_IGN: signal.signal(number, defer)
    completed = False
    try:
        yield
        completed = True
    finally:
        for number, handler in handlers.items(): signal.signal(number, handler)
        if completed and pending:
            number, frame = pending[0]
            handler = handlers[number]
            if callable(handler): handler(number, frame)
            else: raise Stopped(f"Stopped by {signal.Signals(number).name} after restoration")


def run_again(session, may_run_again=lambda: True, attempts=BANNER_ATTEMPTS, before_again=None):
    """Run session(attempt), which returns its exit status and transcript, until it ends otherwise than
    BANNER_STOP, at most attempts times, and again only while may_run_again() allows, as the run's time does.
    Once an attempt will run again, before_again(stopped_transcript, next_attempt), when given, can set the
    stopped attempt aside, as a runner that keeps one folder per session must; whatever it returns is kept in
    that attempt's place. Return the last status and transcript, and the transcripts of the earlier attempts
    the banner stopped, which are unmeasured and kept."""
    stopped = []
    while True:
        status, transcript = session(len(stopped) + 1)
        if status != BANNER_STOP or len(stopped) + 1 >= attempts or not may_run_again():
            return status, transcript, stopped
        if before_again is not None:
            transcript = before_again(transcript, len(stopped) + 2)
        stopped.append(transcript)
