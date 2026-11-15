"""Simulator speech observation, mute restoration and transcript acceptance mechanisms."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import plistlib
import re
import signal
import subprocess
import time
from xml.parsers.expat import ExpatError
from . import __name__ as _package_name

TEST_ENDED = re.compile(r"^Test Case '.*testCaptureNavigation\]' (passed|failed) \(([\d.]+) seconds\)", re.M)
FAILURES = re.compile(r"^.* error: (?:-\[.*?\] : )?(.*)$", re.M)
# The apps that show a simulator live, by process name: Device Hub from Xcode 27, Simulator.app before.
SIMULATOR_VIEWS = {"DeviceHub": "Device Hub", "Simulator": "Simulator.app"}
# VoiceOver's Sounds switch (Settings > Accessibility > VoiceOver > Audio > Sounds & Haptics), which
# its process reads at start from its own preferences domain. Its sound cues play through the
# simulator's system-sound server, which ignores the volume the mute sets, so a muted simulator still
# plays them through the Mac (AXVerify's guides/voiceover-simulator.md, Muted speech capture).
CUE_DOMAIN, CUE_KEY = "com.apple.VoiceOverTouch", "VoiceOverUseSoundEffects"
# Where a simulator keeps that domain, under its data folder.
CUE_PREFERENCES = Path("Library/Preferences") / f"{CUE_DOMAIN}.plist"
# Tries of each spawned command in silence_cues, 30 s each: a host that gave no answer in 30 s on one take
# answered in 4 to 5 s on another at a higher load (vocaro-engineering#310), so a limit taken from the load
# would not help. How often a second try answers is unmeasured.
CUE_ATTEMPTS = 3


class NoAnswer(RuntimeError):
    """A command reached its limit, which says nothing of what it would have answered."""

def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def run(*command, timeout=60):
    """One bounded command: its output, or a RuntimeError that names it."""
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise NoAnswer(f"No answer in {timeout} s: {' '.join(command)}") from None
    if result.returncode:
        raise RuntimeError(f"{' '.join(command)}: {(result.stderr or result.stdout).strip()}")
    return result.stdout


def state(udid, kind, *change, timeout=20, run_command=None):
    """devicectl's reading of a simulator's `audio` or `voiceover` state, after an optional change."""
    run = run_command or globals()["run"]
    if change:
        run("xcrun", "devicectl", "device", "settings", kind, "--device", udid, *change, timeout=timeout)
    answer = run("xcrun", "devicectl", "device", "info", kind, "--device", udid, "--quiet", "--json-output", "-", timeout=timeout)
    return json.loads(answer)["result"]


def silence_cues(udid, run_command=None):
    """Switch VoiceOver's sound cues off on the booted simulator udid; whether they then read off.

    VoiceOver reads the switch when it starts, so call this before VoiceOver is switched on. The
    setting is in the simulator's data container: a member cloned from a template that has it keeps
    it. A command that gives no answer is tried again, up to CUE_ATTEMPTS times; one that fails, or never
    answers, raises RuntimeError, as run does. A member the pool boots has the setting before its boot
    (silence_cues_at_rest), which needs no answer from the simulator."""
    run = run_command or globals()["run"]
    def answered(*arguments):
        for attempt in range(1, CUE_ATTEMPTS + 1):
            try:
                return run("xcrun", "simctl", "spawn", udid, "defaults", *arguments, timeout=30)
            except NoAnswer:
                if attempt == CUE_ATTEMPTS: raise
    answered("write", CUE_DOMAIN, CUE_KEY, "-bool", "false")
    return answered("read", CUE_DOMAIN, CUE_KEY).strip() == "0"


def silence_cues_at_rest(data):
    """Switch VoiceOver's sound cues off in the data folder of a shut-down simulator, for it to read when it
    boots. Unlike silence_cues it waits on no simulator, so a loaded host cannot time it out
    (vocaro-engineering#310). The domain's other settings stay, and a file that already holds it off is not
    touched: a reset or a clone shares its blocks with its template. OSError or ValueError when it cannot
    be written or the file is not a dictionary property list."""
    path = Path(data) / CUE_PREFERENCES
    try:
        settings = plistlib.loads(path.read_bytes())
    except FileNotFoundError:
        settings = {}
    except ExpatError as error:
        raise ValueError(f"{path} is not a valid property list: {error}") from error
    if not isinstance(settings, dict):
        raise ValueError(f"{path} is not a dictionary")
    if settings.get(CUE_KEY) is False:
        return
    settings[CUE_KEY] = False
    path.parent.mkdir(parents=True, exist_ok=True)
    written = path.with_name(f"{path.name}.{os.getpid()}")
    written.write_bytes(plistlib.dumps(settings, fmt=plistlib.FMT_BINARY))
    os.replace(written, path)


# Readings of volume 0 in a row, one a second, before VoiceOver may speak on a member: 20 of 24 kept or
# cloned members put their volume back to 60 3 to 17 s after the mute (vocaro-engineering#146), so the
# mute is held as the pool's template warm-up holds it (template.MUTE_HELD and MUTE_LIMIT).
MUTE_HELD = 20
MUTE_LIMIT = 90


def hold_mute(udid, *, state_reader=None, readings=MUTE_HELD, limit=MUTE_LIMIT, sleep=time.sleep, clock=time.monotonic):
    """Mute the booted simulator udid and read its volume each second until it has read 0 `readings` times
    in a row, muting it again on any other reading; a reading with no answer neither counts nor breaks the
    run. Returns (seconds it took, the other volumes read); RuntimeError when it did not hold within limit
    seconds, or the first mute failed (AXVerify's guides/voiceover-simulator.md, Muted speech capture)."""
    state = state_reader or globals()["state"]
    unmuted, held, began = [], 0, clock()
    state(udid, "audio", "--volume", "0")
    while held < readings:
        if clock() - began > limit:
            raise RuntimeError(f"the volume did not hold at 0 for {readings} readings in {limit:g} s"
                               + (f" (it read {', '.join(map(str, unmuted))})" if unmuted else ""))
        try:
            volume = state(udid, "audio", timeout=5)["volume"]
        except (RuntimeError, ValueError, KeyError):
            volume = None
        if volume == 0:
            held += 1
        elif volume is not None:
            held = 0
            unmuted.append(volume)
            state(udid, "audio", "--volume", "0")
        if held < readings: sleep(1)
    return clock() - began, unmuted


def keep_muted(session, state_reader=None):
    state = state_reader or globals()["state"]
    """A simulator whose display server restarts comes back at its default volume, VoiceOver still on."""
    try:
        # Briefly: a slow answer must not hold up the watch on the other simulators.
        volume = state(session.udid, "audio", timeout=5)["volume"]
        if volume != 0:
            state(session.udid, "audio", "--volume", "0")
            session.unmuted.append(f"{now()} UNMUTED the volume was {volume}; muted again")
    except (RuntimeError, ValueError, KeyError):
        session.unread.append(f"{now()} VOLUME gave no reading for this check")


def simulator_app():
    """The name of a running app that shows simulators live, or None; it starts nothing.

    Xcode 27 has no Simulator.app: its Device Hub runs as a `DeviceHub` process
    (apple-engineering's guides/simulator-testing.md#device-hub).
    """
    for process, name in SIMULATOR_VIEWS.items():
        if subprocess.run(["pgrep", "-x", process], capture_output=True).stdout.strip():
            return name
    return None


def stop(process):
    if process.poll() is not None:
        return
    os.killpg(process.pid, signal.SIGINT)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)


def events(text):
    return [line.removeprefix("VOICEOVER_EVENT ") for line in text.splitlines() if line.startswith("VOICEOVER_EVENT ")]


# What checks a run that turned VoiceOver on, by its `evidence_failures(log, vot[, guest])`: the toolkit's
# VoiceOver framework names its activation module here, since the extension never imports the toolkit.
ACTIVATION = None


def staged(bundle):
    """The probe's output as an unfinished result bundle holds it, for an xcodebuild that will not end."""
    found = [events(path.read_text(errors="replace"))
             for path in bundle.glob("Staging/*/Diagnostics/**/StandardOutputAndStandardError.txt")]
    return max(found, key=len, default=[])


def report(session, *, state_reader=None, test_ended=TEST_ENDED, failure_pattern=FAILURES, activation_module=None):
    state = state_reader or globals()["state"]
    activation = activation_module or ACTIVATION
    """Confirm a simulator's VoiceOver is off, write its transcript and print the outcome."""
    log = session.log.read_text(errors="replace") if session.log.exists() else ""
    heard, source = events(log), "xcodebuild's output"
    if len(session.recovered) > len(heard):
        heard, source = session.recovered, "the unfinished result bundle"
    ended = test_ended.search(log)
    failures = sorted(set(failure_pattern.findall(log)))
    counts = re.findall(r"Executed (\d+) tests?[, ]", log)
    if not counts or any(count != "1" for count in counts):
        failures.append("the filtered run did not confirm exactly one executed test")
    if session.activation and activation is None:
        failures.append("the run turned VoiceOver on, and nothing that checks its activation is registered "
                        "(simulators.speech.ACTIVATION)")
    elif session.activation:
        vot = session.vot.read_text(errors="replace") if session.vot.exists() else ""
        # The guest build the runner read from the booted member (activation.guest_identity).
        guest = getattr(session, "guest_build", None)
        failures += activation.evidence_failures(log, vot, guest) if guest else activation.evidence_failures(log, vot)
    if "Restarting after unexpected exit, crash, or test timeout" in log:
        failures.append("the test runner exited unexpectedly")
    # The probe's last line: without it some steps never ran, whatever the test session reports.
    complete = any(" finished: " in line for line in heard)
    if session.process is None:
        outcome, status = "the probe was not started", 125
    elif ended and ended[1] == "passed" and complete and not failures:
        outcome, status = f"the probe passed in {float(ended[2]):.0f} s", 0
    elif ended:
        outcome, status = f"the probe failed after {float(ended[2]):.0f} s", 1
    else:
        outcome, status = f"the probe {'ran every step but its test never ended' if complete else 'did not finish'}", 124 if session.late else 1
    lines = [f"RESULT {outcome}"] + [f"FAILURE {failure}" for failure in failures]
    if session.note:
        lines.append(f"STOPPED {session.note}; the speech is from {source}")
    try:
        # After a stopped or crashed probe VoiceOver stays on, at whatever volume the simulator has.
        if state(session.udid, "voiceover")["enabled"]:
            enabled = state(session.udid, "voiceover", "--disable")["enabled"]
            lines.append(f"VOICEOVER was left on; it {'is still on' if enabled else 'has been switched off'}")
            status = max(status, 1)
        else:
            lines.append("VOICEOVER off after the run")
        volume = state(session.udid, "audio")["volume"]
        unread = f"; {len(session.unread)} checks during it got no reading" if session.unread else ""
        lines.append(f"VOLUME {volume} after the run{unread}")
        # A simulator that was heard, or could have been, is not a clean run.
        if volume != 0 or session.unmuted:
            status = max(status, 1)
    except (RuntimeError, ValueError, KeyError) as error:
        lines.append(f"VOICEOVER state unknown: {error}")
        status = max(status, 1)
    if session.started is not None:
        lines.append(f"XCODEBUILD ran for {(session.finished or time.monotonic()) - session.started:.0f} s")
    # Ordered by time alone: phrases of one instant keep the order they were spoken in.
    transcript = sorted(session.transcript + heard + session.unmuted + session.unread + [f"{now()} {line}" for line in lines],
                        key=lambda line: line.split(" ", 1)[0])
    path = session.log.with_name(session.log.stem + ".transcript.txt")
    path.write_text("\n".join(transcript) + "\n")
    print(f"{session.kind}: " + "; ".join(lines + session.unmuted))
    print(f"  Transcript: {path}\n  Log: {session.log}\n  Result bundle: {session.bundle}")
    if session.stream is not None:
        print(f"  VoiceOver's log: {session.vot}")
    return status
