"""A trial run: one switch, QUEUEKEEPER_TRIAL, that keeps a run off the account's real Queuekeeper state and off
every other Mac (vocaro/queuekeeper#34).

QUEUEKEEPER_TRIAL names a folder, an absolute path, that the run keeps all of its own Queuekeeper state in. Set,
`qk` points every namespace its commands and lanes read and write at that folder before it reads a command, and the
processes it starts inherit them: its admission (slots, admission log, limits and farm.json), the admission guard,
the device record, the store of kept app builds, its pinned checkouts and the crash-dialog record. A trial's farm
has no Mac (farm.read), so no lane, comparison or fix reaches a second Mac, and `qk farm` changes none. A heavy run
reaps nothing first, and `qk doctor` applies none of its fixes to this Mac's simulators or devices or to another
Mac's checkout (cli.fix_hosts), and says so. A queue's state is its repository's own, in its Git directory, so a
trial lands in a throwaway clone. Before this switch a trial had to set HEAVY_SLOT_DIR, the device record and the
second Mac by hand, and forgetting any one of them reached the live system.
"""

import os
from pathlib import Path

VARIABLE = "QUEUEKEEPER_TRIAL"
# What a trial points at its folder, each by the name under it: the variable that moves it, and its place.
PLACES = {
    "HEAVY_SLOT_DIR": "admission",
    "QUEUEKEEPER_GATE_LOCK_FILE": "gate.lock",
    "QUEUEKEEPER_DEVICE_RECORD": "device-handoff.json",
    "QUEUEKEEPER_APP_STORE": "apps",
    "QUEUEKEEPER_PINS_DIR": "pins",
    "QUEUEKEEPER_CRASH_DIALOG_RECORD": "crash-dialog.json",
}
# What a trial turns off: a heavy run's reap of this Mac's stale pool simulators (cli.REAP_SWITCH), the watch
# of devices' logs and quitting the apps a run opened, each of which acts on this Mac beyond the trial.
OFF = ("QUEUEKEEPER_REAP_BEFORE_RUN", "QUEUEKEEPER_DEVICE_LOG_WATCH", "QUEUEKEEPER_QUIT_OPENED_APPS")


def folder(environment=None):
    """The trial's folder, or None when this is no trial. A relative one is refused: it would be a different
    folder in each directory a lane runs in."""
    named = (os.environ if environment is None else environment).get(VARIABLE, "").strip()
    if not named: return None
    if not os.path.isabs(named):
        raise ValueError(f"{VARIABLE} names {named!r}, not an absolute path: name the folder a trial keeps its "
                         "state in by its full path")
    return Path(named)


def active():
    return folder() is not None


def apply(environment=None):
    """Point every namespace at the trial's folder in environment (os.environ by default), over whatever named
    them, and make the folder. Returns the folder, or None when this is no trial."""
    environment = os.environ if environment is None else environment
    found = folder(environment)
    if found is None: return None
    (found / PLACES["HEAVY_SLOT_DIR"]).mkdir(parents=True, exist_ok=True)
    environment.update({name: str(found / place) for name, place in PLACES.items()})
    environment.update(dict.fromkeys(OFF, "off"))
    return found


def refusal(what):
    """Why a trial does not do what, for the command that refuses it to say."""
    return f"{what}: refused, as {VARIABLE} makes this a trial, which acts on no real Mac, simulator or device"
