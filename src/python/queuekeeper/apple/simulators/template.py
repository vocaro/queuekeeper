"""A pool's warmed template: one simulator per kind (device type and runtime) that has booted once, heard
VoiceOver speak and used the system's voices twice with a reboot between, from which the pool clones new
members and resets taken ones (guides/simulator-testing.md, Warmed templates).

A new simulator's first boot migrates its data, and VoiceOver's first start on it waits for its voice
database, which took 40 to 60 s on a quiet Mac and minutes on a loaded one. The first request for the
system's voices (`AVSpeechSynthesisVoice.speechVoices()`) on a freshly erased simulator makes its
accessibility assets daemon build its voice database in memory, an idle wait of 285 to 344 s in each of
a member's xcodebuild clones with the second Mac to itself and past 1,900 s beside another gate, while
clones of a member that had used its voices twice, with a reboot between, answered in 11 to 13 s
(vocaro/EPUBLib#946, vocaro-engineering#172). `simctl clone` copies a shut-down device's data, and so
does the pool's reset, so a member cloned from or reset to a settled template boots past all of it.
warm() does the warming on a template the pool holds and has created: boot to `bootstatus -b`, read
the guest's runtime build, switch VoiceOver's sound cues off, mute it, switch VoiceOver on until it
speaks and off again, then use the voices, by a program `simctl spawn` runs in the simulator as a test
process would (voices_tool), reboot and use them again; a template whose second use answered is
settled. The pool shuts it down, records what warm() returns in the template's device-record entry
and releases it. Nothing here reserves, creates, clones or deletes a device.
"""
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import tempfile
import time

from ..sources import ROOT, source_path
from . import runtime_identity
from .speech import CUE_DOMAIN, CUE_KEY

# What the speech extension logs for each phrase VoiceOver speaks (guides/voiceover-simulator.md,
# VoiceOver's vot log); the first one says VoiceOver has its voice.
SPEECH = "Processing markup for voice locale"
PREDICATE = 'process == "vot" OR process == "MauiAUSP"'
# Seconds VoiceOver may stay silent after it is switched on; under saturation it took up to 292 s.
VOICEOVER_LIMIT = 600
# Seconds one use of the system's voices may take. A member's own first use took 53 to 173 s with the
# second Mac to itself and 24 s after a reboot; beside another gate a clone's first use passed 1,900 s.
VOICES_LIMIT = 900
# Inside a take, a kind's first creation or reset warms its template before the lane's own work begins,
# so the boot, VoiceOver and each use of the voices get shorter limits there: VoiceOver the probe's
# default start limit. A template VoiceOver left cold is still cloned, booted once, and one whose
# voices did not settle is still cloned but never copied into a member; `eng simulator-pool template`
# warms it again with the full limits.
TAKE_BOOT = 300
TAKE_VOICEOVER_LIMIT = 180
TAKE_VOICES_LIMIT = 300
# A member booted without the first boot's migration, kept or cloned, put its volume back to 60 3 to 17 s
# after it was muted, in 20 of 24 sessions (vocaro-engineering#146), so VoiceOver starts only once the
# mute has held this many readings in a row, one a second, within MUTE_LIMIT seconds.
MUTE_HELD = 20
MUTE_LIMIT = 90
# The program that makes the voices' first use as a test process does: the voices' list, then each
# language's own voice (src/swift/Simulator/simulator-voices.swift). It is built once per source, SDK
# and target beside the toolkit, as duo_pose builds its helper, and signed ad hoc so that it runs.
VOICES_SOURCE = "simulator-voices.swift"
VOICES_BUILD = ROOT / ".build" / "simulator-voices"
VOICES_TARGET = f"{platform.machine()}-apple-ios17.0-simulator"
BUILD_TIMEOUT, QUERY_TIMEOUT = 300, 60
# Its lines: the seconds since it started, then what it did.
SPEECH_VOICES = re.compile(r"^(?P<seconds>\d+\.\d+) speechVoices (?P<count>\d+)$", re.M)
LANGUAGES = re.compile(r"^(?P<seconds>\d+\.\d+) languages (?P<own>\d+) of (?P<count>\d+) have their own voice$", re.M)


class WarmFailed(RuntimeError):
    """The template did not boot, or its runtime build could not be read, so it is not a template."""


class ToolUnbuilt(RuntimeError):
    """The voices program could not be built, so no template settles its voices until it can."""


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


def confirmed_off(devicectl, udid):
    """Whether devicectl reads VoiceOver off, switching it off once more when it does not."""
    if (reading(devicectl, udid, "voiceover") or {}).get("enabled") is False:
        return True
    devicectl("settings", "voiceover", "--device", udid, "--disable")
    return (reading(devicectl, udid, "voiceover") or {}).get("enabled") is False


def run(*command, timeout):
    """One bounded command's result, never raising for its status; 124 past its limit."""
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(command, 124, "", f"timed out after {timeout} s")


def voices_tool(build=VOICES_BUILD, source=None, run=run):
    """The signed voices program for the current source, target and iOS Simulator SDK, built first when
    there is none; ToolUnbuilt when it cannot be built."""
    source = Path(source or source_path(VOICES_SOURCE))
    identity = []
    for query in ("--show-sdk-path", "--show-sdk-build-version"):
        answer = run("xcrun", "--sdk", "iphonesimulator", query, timeout=QUERY_TIMEOUT)
        if answer.returncode != 0 or not answer.stdout.strip():
            raise ToolUnbuilt(f"no iOS Simulator SDK: xcrun {query} failed: {answer.stderr.strip() or 'no output'}")
        identity.append(answer.stdout.strip())
    digest = hashlib.sha256(source.read_bytes())
    for part in (VOICES_TARGET, *identity):
        digest.update(b"\0" + part.encode())
    binary = Path(build) / f"simulator-voices-{digest.hexdigest()[:16]}"
    if binary.is_file() and os.access(binary, os.X_OK):
        return binary
    binary.parent.mkdir(parents=True, exist_ok=True)
    # Built and signed under a name of its own, so a concurrent warm-up never sees it half made.
    handle, scratch = tempfile.mkstemp(prefix=binary.name + ".", suffix=".tmp", dir=binary.parent)
    os.close(handle)
    try:
        built = run("xcrun", "--sdk", "iphonesimulator", "swiftc", "-O", "-target", VOICES_TARGET, str(source), "-o",
                    scratch, timeout=BUILD_TIMEOUT)
        if built.returncode != 0:
            raise ToolUnbuilt(f"the voices program did not build: {(built.stderr or built.stdout).strip()[-500:]}")
        signed = run("codesign", "--force", "--sign", "-", "--identifier", "simulator-voices", scratch,
                     timeout=QUERY_TIMEOUT)
        if signed.returncode != 0:
            raise ToolUnbuilt(f"the voices program did not sign: {(signed.stderr or signed.stdout).strip()[-500:]}")
        os.chmod(scratch, 0o755)
        os.replace(scratch, binary)
    finally:
        if os.path.exists(scratch):
            os.unlink(scratch)
    return binary


def use_voices(udid, tool, *, simctl, limit):
    """One use of the system's voices on the booted simulator udid, by the voices program tool: the
    seconds until the voices' list came and their count, and the seconds until each language had its own
    voice; or no seconds and why."""
    answer = simctl("spawn", udid, str(tool), timeout=limit)
    if answer.returncode == 124:
        return {"seconds": None, "outcome": f"no answer in {limit} s"}
    listed = SPEECH_VOICES.search(answer.stdout or "")
    if answer.returncode != 0 or listed is None:
        detail = (answer.stderr or answer.stdout or "").strip()[-300:] or "no output"
        return {"seconds": None, "outcome": f"the voices program exited {answer.returncode}: {detail}"}
    found = {"seconds": float(listed["seconds"]), "count": int(listed["count"])}
    languages = LANGUAGES.search(answer.stdout)
    if languages is not None:
        found.update(languagesSeconds=float(languages["seconds"]), languages=int(languages["count"]),
                     languagesWithAVoice=int(languages["own"]))
    return found


def settle_voices(udid, *, simctl, tool, limit, boot_timeout, clock=time.monotonic):
    """Two uses of the voices on the booted template udid with a reboot between, and whether they settled
    it: what the record keeps as the template's `voices`. settled is True only when both uses answered.
    A reboot that fails leaves the template as it is, and the record says so; the caller shuts it down."""
    record = {"limit": limit, "settled": False, "firstUse": use_voices(udid, tool, simctl=simctl, limit=limit)}
    if record["firstUse"]["seconds"] is None:
        record["outcome"] = f"the first use gave no answer: {record['firstUse']['outcome']}"
        return record
    started = clock()
    rebooted = simctl("shutdown", udid, timeout=300)
    if rebooted.returncode == 0: rebooted = simctl("boot", udid, timeout=120)
    if rebooted.returncode == 0: rebooted = simctl("bootstatus", udid, "-b", timeout=boot_timeout)
    if rebooted.returncode != 0:
        detail = "reached its limit" if rebooted.returncode == 124 else \
            f"exited {rebooted.returncode}: {(rebooted.stderr or rebooted.stdout).strip()[-300:]}"
        record["outcome"] = f"the reboot between the uses {detail}"
        return record
    record["rebootSeconds"] = round(clock() - started, 1)
    record["secondUse"] = use_voices(udid, tool, simctl=simctl, limit=limit)
    if record["secondUse"]["seconds"] is None:
        record["outcome"] = f"the second use, after a reboot, gave no answer: {record['secondUse']['outcome']}"
        return record
    record["settled"] = True
    record["outcome"] = (f"settled: {record['firstUse']['count']} voices listed {record['firstUse']['seconds']} s into "
                         f"the first use and {record['secondUse']['seconds']} s into the second, after a reboot")
    return record


def warm(udid, runtime, *, simctl, devicectl=devicectl, stream=log_stream, boot_timeout=600,
         voiceover_limit=VOICEOVER_LIMIT, voices_limit=VOICES_LIMIT, tool=None, clock=time.monotonic,
         sleep=time.sleep):
    """Warm the held, shut-down template udid on runtime; what the pool records of it.

    It raises WarmFailed when the boot fails or reaches its limit, or the guest's runtime build cannot
    be read. VoiceOver that stays silent past voiceover_limit, or a volume that cannot be set to 0,
    leaves the template booted and migrated but VoiceOver cold, and the record says so, as do sound
    cues that do not read off once switched off (cueSoundsOff). VoiceOver
    speaks through the Mac's own output device, so it starts only once the volume has read 0 MUTE_HELD
    times in a row, a reading with no answer neither counting nor breaking the run, within MUTE_LIMIT
    seconds, and the volume is read again each second while it is on. voiceOverOff records whether
    devicectl confirmed VoiceOver off at the end: the setting is in the data container, so a clone of
    a template it did not confirm would boot with VoiceOver on. Whatever VoiceOver did, the system's
    voices are then used twice with a reboot between, each use within voices_limit, by tool, the voices
    program (voices_tool builds it when tool is None); `voices` records both uses and whether they
    settled the template. The caller shuts the template down, however this ends."""
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
    start_voiceover(udid, record, simctl=simctl, devicectl=devicectl, stream=stream, voiceover_limit=voiceover_limit,
                    clock=clock, sleep=sleep)
    try:
        tool = tool or voices_tool()
    except ToolUnbuilt as error:
        record["voices"] = {"limit": voices_limit, "settled": False, "outcome": str(error)}
        return record
    record["voices"] = settle_voices(udid, simctl=simctl, tool=tool, limit=voices_limit, boot_timeout=boot_timeout,
                                     clock=clock)
    return record


def start_voiceover(udid, record, *, simctl, devicectl, stream, voiceover_limit, clock, sleep):
    """Switch VoiceOver on until it speaks, muted and with its sound cues off, then off again, on the
    booted template udid, writing what happened into record (warm says what)."""
    # The mute leaves VoiceOver's sound cues audible; with the switch off they do not play at all, and
    # every member cloned from this template keeps it.
    written = simctl("spawn", udid, "defaults", "write", CUE_DOMAIN, CUE_KEY, "-bool", "false", timeout=30)
    read = simctl("spawn", udid, "defaults", "read", CUE_DOMAIN, CUE_KEY, timeout=30)
    record["cueSoundsOff"] = written.returncode == 0 and read.returncode == 0 and read.stdout.strip() == "0"
    if not record["cueSoundsOff"]:
        record["voiceOver"] = "not started: VoiceOver's sound cues could not be switched off"
        record["voiceOverOff"] = confirmed_off(devicectl, udid)
        return
    unmuted, held, muting = [], 0, clock()
    devicectl("settings", "audio", "--device", udid, "--volume", "0")
    while held < MUTE_HELD and clock() - muting < MUTE_LIMIT:
        volume = (reading(devicectl, udid, "audio") or {}).get("volume")
        # No answer says nothing of the volume; only a reading of another volume breaks the run.
        if volume == 0:
            held += 1
        elif volume is not None:
            held = 0
            devicectl("settings", "audio", "--device", udid, "--volume", "0")
            unmuted.append(volume)
        if held < MUTE_HELD: sleep(1)
    if held < MUTE_HELD:
        record["voiceOver"] = f"not started: the volume did not hold at 0 for {MUTE_HELD} readings in {MUTE_LIMIT} s"
        record["voiceOverOff"] = confirmed_off(devicectl, udid)
        if unmuted: record["unmuted"] = unmuted
        return
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
    off = confirmed_off(devicectl, udid)
    record["voiceOver"] = (f"spoke {spoke} s after it was switched on" if spoke is not None else
                           f"silent for {voiceover_limit} s, so it is not warmed") + \
        ("" if off else "; it could not be confirmed off")
    record["voiceOverWarmed"] = spoke is not None
    record["voiceOverOff"] = off
    if unmuted: record["unmuted"] = unmuted
