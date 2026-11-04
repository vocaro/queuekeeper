"""A pool's warmed template: one simulator per kind (device type and runtime) that has booted once and
heard VoiceOver speak, from which the pool clones new members (guides/simulator-testing.md, Warmed
templates).

A new simulator's first boot migrates its data, and VoiceOver's first start on it waits for its voice
database, which took 40 to 60 s on a quiet Mac and minutes on a loaded one. `simctl clone` copies a
shut-down device's data, so a member cloned from a warmed template boots past both. warm() does the
warming on a template the pool holds and has created: boot to `bootstatus -b`, read the guest's
runtime build, mute it, switch VoiceOver on until it speaks and off again. The pool shuts it down,
records what warm() returns in the template's device-record entry and releases it. Nothing here
reserves, creates, clones or deletes a device.
"""
import json
import subprocess
import tempfile
import time
from pathlib import Path

from . import runtime_identity

# What the speech extension logs for each phrase VoiceOver speaks (guides/voiceover-simulator.md,
# VoiceOver's vot log); the first one says VoiceOver has its voice.
SPEECH = "Processing markup for voice locale"
PREDICATE = 'process == "vot" OR process == "MauiAUSP"'
# Seconds VoiceOver may stay silent after it is switched on; under saturation it took up to 292 s.
VOICEOVER_LIMIT = 600
# A member booted without the first boot's migration, kept or cloned, put its volume back to 60 3 to 17 s
# after it was muted, in 20 of 24 sessions (vocaro-engineering#146), so VoiceOver starts only once the
# mute has held this many readings in a row, one a second, within MUTE_LIMIT seconds.
MUTE_HELD = 20
MUTE_LIMIT = 90


class WarmFailed(RuntimeError):
    """The template did not boot, or its runtime build could not be read, so it is not a template."""


def devicectl(*arguments, timeout=30):
    """xcrun devicectl device's result, never raising for its status. A command past its limit reads 124."""
    try:
        return subprocess.run(["xcrun", "devicectl", "device", *arguments], capture_output=True, text=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(arguments, 124, "", f"timed out after {timeout} s")


def log_stream(udid, path):
    """VoiceOver's and the speech extension's log on this simulator, written to path until stopped."""
    with open(path, "w") as output:
        return subprocess.Popen(["xcrun", "simctl", "spawn", udid, "log", "stream", "--level", "debug", "--style",
                                 "compact", "--predicate", PREDICATE], stdout=output, stderr=subprocess.STDOUT,
                                start_new_session=True)


def reading(devicectl, udid, kind):
    """devicectl's reading of the simulator's `audio` or `voiceover` state, or None without an answer."""
    answer = devicectl("info", kind, "--device", udid, "--quiet", "--json-output", "-", timeout=10)
    try:
        return json.loads(answer.stdout)["result"] if answer.returncode == 0 else None
    except (ValueError, KeyError, TypeError):
        return None


def warm(udid, runtime, *, simctl, devicectl=devicectl, stream=log_stream, boot_timeout=600,
         voiceover_limit=VOICEOVER_LIMIT, clock=time.monotonic, sleep=time.sleep):
    """Warm the held, shut-down template udid on runtime; what the pool records of it.

    It raises WarmFailed when the boot fails or reaches its limit, or the guest's runtime build cannot
    be read. VoiceOver that stays silent past voiceover_limit, or a volume that cannot be set to 0,
    leaves the template booted and migrated but VoiceOver cold, and the record says so. VoiceOver
    speaks through the Mac's own output device, so it starts only once the volume has read 0 MUTE_HELD
    times in a row, and the volume is read again each second while it is on. The caller shuts the
    template down, however this ends."""
    started = clock()
    booted = simctl("boot", udid, timeout=120)
    if booted.returncode:
        raise WarmFailed(f"simctl boot exited {booted.returncode}: {(booted.stderr or booted.stdout).strip()}")
    finished = simctl("bootstatus", udid, "-b", timeout=boot_timeout)
    if finished.returncode:
        raise WarmFailed(f"the boot did not finish within {boot_timeout} s" if finished.returncode == 124 else
                         f"simctl bootstatus exited {finished.returncode}: {finished.stderr.strip()}")
    record = {"bootSeconds": round(clock() - started, 1)}

    def query(*arguments):
        result = simctl(*arguments, timeout=120)
        if result.returncode:
            raise subprocess.CalledProcessError(result.returncode, ["simctl", *arguments], result.stdout, result.stderr)
        return result.stdout
    try:
        found = runtime_identity.runtime_for_device(udid, runtime, run=query)
    except runtime_identity.RuntimeIdentityUnavailable as error:
        raise WarmFailed(f"its runtime build could not be read: {error}") from None
    record.update(runtimeBuild=found["buildversion"], runtimeVersion=found["version"])
    unmuted, held, muting = [], 0, clock()
    devicectl("settings", "audio", "--device", udid, "--volume", "0")
    while held < MUTE_HELD and clock() - muting < MUTE_LIMIT:
        volume = (reading(devicectl, udid, "audio") or {}).get("volume")
        held = held + 1 if volume == 0 else 0
        if volume not in (0, None):
            devicectl("settings", "audio", "--device", udid, "--volume", "0")
            unmuted.append(volume)
        if held < MUTE_HELD: sleep(1)
    if held < MUTE_HELD:
        record["voiceOver"] = f"not started: the volume did not hold at 0 for {MUTE_HELD} readings in {MUTE_LIMIT} s"
        if unmuted: record["unmuted"] = unmuted
        return record
    with tempfile.TemporaryDirectory(prefix="pool-template-") as directory:
        path = Path(directory) / "vot.log"
        process = stream(udid, path)
        try:
            switched = clock()
            devicectl("settings", "voiceover", "--device", udid, "--enable")
            spoke = None
            while spoke is None and clock() - switched < voiceover_limit:
                sleep(1)
                if SPEECH in path.read_text(errors="replace"):
                    spoke = round(clock() - switched, 1)
                volume = (reading(devicectl, udid, "audio") or {}).get("volume")
                if volume not in (0, None):
                    devicectl("settings", "audio", "--device", udid, "--volume", "0")
                    unmuted.append(volume)
        finally:
            devicectl("settings", "voiceover", "--device", udid, "--disable")
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
    off = (reading(devicectl, udid, "voiceover") or {}).get("enabled") is False
    record["voiceOver"] = (f"spoke {spoke} s after it was switched on" if spoke is not None else
                           f"silent for {voiceover_limit} s, so it is not warmed") + \
        ("" if off else "; it could not be confirmed off")
    record["voiceOverWarmed"] = spoke is not None
    if unmuted: record["unmuted"] = unmuted
    return record
