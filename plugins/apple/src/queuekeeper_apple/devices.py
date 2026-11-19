"""The device record as the Apple plugin uses it: the shared record of mobile devices (queuekeeper_mobile.devices),
whose names this module gives under its earlier one, and what only a Mac's VoiceOver adds to its mac-foreground
entry. `qk devices` is the shared module's command line; `python3 -m queuekeeper_apple.devices` still runs it, for a
Mac that sends work with an earlier toolkit's command (offload_host.defer_devices)."""
from queuekeeper_mobile.devices import *  # noqa: F401,F403
from queuekeeper_mobile.devices import _owned, main  # noqa: F401
from queuekeeper_mobile.devices import FOREGROUND, Refused, now, process_start
from .apple_platform import NAMED_PHYSICAL, PAIRED, PHYSICAL, SIGNING, UDID  # noqa: F401 -- switches it on


def unrecoverable_restoration(entry):
    """Why the run's own runner may not restore VoiceOver over this mac-foreground entry's uncertainty, or None.

    It may only when the uncertainty is a VoiceOver restoration a session could not verify and nothing else: the
    entry is uncertain with no other reason, its voiceOverRestoration says unverified and was the last thing written
    to it, no earlier recovery of it failed, the run that holds it is still alive, and the original VoiceOver state
    was recorded under this same lease (owner's decision, 2026-11-12, vocaro/EPUBLib#416)."""
    if not entry.get("uncertain"):
        return "it is not uncertain"
    if entry.get("uncertainReason") or entry.get("state") == "restoration-unverified":
        return f"its uncertainty is not only a VoiceOver restoration: {entry.get('uncertainReason') or entry.get('state')}"
    marker = entry.get("voiceOverRestoration")
    if not isinstance(marker, dict) or marker.get("verified") is not False:
        return "its uncertainty is not an unverified VoiceOver restoration"
    if marker.get("recoveryFailed"):
        return f"a recovery of its VoiceOver restoration already failed: {marker['recoveryFailed'].get('reason')}"
    if marker.get("revision") != entry.get("revision"):
        return "it changed after its VoiceOver restoration was marked unverified"
    pid, start = entry.get("hostProcessIdentifier"), entry.get("hostProcessStart")
    if not entry.get("activeRun") or not (isinstance(pid, int) and pid > 0 and start is not None
                                          and process_start(pid) == start):
        return "the run that holds it is no longer alive"
    original = entry.get("voiceOverOriginal")
    if not isinstance(original, dict) or type(original.get("enabled")) is not bool:
        return "it records no original VoiceOver state"
    if (original.get("reservedAt"), original.get("hostProcessIdentifier"), original.get("hostProcessStart")) != \
            (entry.get("reservedAt"), pid, start):
        return "its original VoiceOver state was recorded under another lease"
    return None


def restoration_recovered(record, key, *, owner, revision, by):
    """In record, end the uncertainty an unverified VoiceOver restoration left in owner's live mac-foreground entry, once
    the run's own runner set the recorded original again and saw it hold (voiceover.state.recover_restoration).
    The entry stays held by the run; its voiceOverRestoration records who recovered it and when. Refused, with
    nothing changed, for any other uncertainty (unrecoverable_restoration) or an entry that moved past
    revision."""
    if key != FOREGROUND:
        raise ValueError(f"only {FOREGROUND} records a VoiceOver restoration")
    if not by:
        raise ValueError("the recovery records who restored VoiceOver")

    def change(current, _record):
        _owned(key, current, owner)
        problem = unrecoverable_restoration(current)
        if problem:
            raise Refused(f"{key} cannot be recovered by its run: {problem}. Nothing was changed")
        entry = {name: value for name, value in current.items() if name != "uncertain"}
        entry["voiceOverRestoration"] = {**current["voiceOverRestoration"], "verified": True, "recoveredBy": by,
                                         "at": now(), "original": current["voiceOverOriginal"]["enabled"]}
        return entry
    return record._change(key, revision, change)


if __name__ == "__main__":
    raise SystemExit(main())
