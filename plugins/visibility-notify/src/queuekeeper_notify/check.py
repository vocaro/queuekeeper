"""A look for a queue that is stuck without having written an event: what `qk queue doctor` finds.

Entries that wait with no worker, gates in a row with no verdict and the like change nothing in the journal, so no
delivery tells of them. Each command that reaches the hosts therefore starts a look, apart from itself and at most
once every EVERY seconds for a repository (spawn): the signed launcher runs such a command every two minutes, and
agents run them all day. A finding is told once it has lasted PERSIST seconds, so that the moment between a worker
ending and the next one starting is not news. If no channel takes the notice, the next look tries again while the
finding is present. An uncertain outcome is remembered so that channel does not receive a retry.
Once a channel accepts it, it is told again only after it has gone and come back. What it has
found is kept beside the journal:

    <common>/queuekeeper-queue/visibility-notify-check.json   each finding's first sighting, and whether it was told
"""
import fcntl
import json
import os
from pathlib import Path
import time

from queuekeeper import git_state, merge_queue, visibility
from . import channels, deliver, rules

STATE, LOCK = "visibility-notify-check.json", "visibility-notify-check.lock"
EVERY, PERSIST = 120, 300
RULE = "stuck"
# A hold is told as its event, and an entry that came back unchanged is its author's to deal with.
SKIPPED = {"held", "came back"}


def directory_of(root):
    return Path(root) / ".git" / "queuekeeper-queue"


def read(directory):
    try: found = json.loads((Path(directory) / STATE).read_text())
    except (OSError, ValueError): return {}
    return found if isinstance(found, dict) and isinstance(found.get("findings"), dict) else {}


def write(directory, state):
    path = Path(directory) / STATE
    staged = path.with_suffix(f".{os.getpid()}.tmp")
    staged.write_text(json.dumps(state, indent=2) + "\n")
    os.replace(staged, path)


def spawn():
    """Start a look at the repository this command runs in, apart from this process, unless one was started within
    EVERY seconds (plugins.HOST_COMMAND). It runs for stuck findings or a pending owner-hold retry.
    It returns at once and does nothing where the folder has no queue."""
    if visibility.switched_off(): return
    on = rules.chosen(deliver.settings())
    if RULE not in on and "held-owner" not in on: return
    folder = Path.cwd()
    root = git_state.main_checkout(folder) or folder
    directory = directory_of(root)
    if not directory.is_dir(): return
    if RULE not in on and not (deliver.read_cursor(directory) or {}).get(deliver.HOLD_RETRY): return
    mark = directory / STATE
    try:
        if time.time() - mark.stat().st_mtime < EVERY: return
        os.utime(mark)  # before the look starts, so that commands started together start one
    except OSError: write(directory, {"findings": {}})
    deliver.start("--check", str(root))


def check(root, now=None, diagnose=None):
    """Look at the queue of the repository at root, and return (its findings, how many were told of now). A
    finding that has lasted PERSIST seconds is sent through every channel that is on. It stays eligible until
    at least one channel accepts it. Channels whose outcomes are uncertain are excluded from later attempts."""
    now = time.time() if now is None else now
    directory = directory_of(root)
    try: deliver.retry_owner_hold(directory, now)
    except OSError as error: deliver.log(f"the owner-hold retry for {root} stopped: {error}")
    found = deliver.settings()
    with (directory / LOCK).open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        before = read(directory).get("findings", {})
        try:
            findings = [finding for finding in (diagnose or merge_queue.diagnose)(root)[0]
                        if finding.get("signal") not in SKIPPED]
        except Exception as error:  # noqa: BLE001
            deliver.log(f"the check of {root} stopped: {error}")
            findings = [{"signal": rules.CHECK_FAILED, "diagnosis": "Queue doctor could not complete its check",
                         "remedy": "Read notify.log, then run `qk queue doctor` to check the queue"}]
            # An unknown queue state cannot clear an incident seen on an earlier successful observation.
            kept = dict(before)
        else:
            kept = {}
        pending = {}
        for finding in findings:
            signal = finding["signal"]
            seen = before.get(signal)
            seen = seen if isinstance(seen, dict) and isinstance(seen.get("first"), (int, float)) else {"first": now}
            kept[signal] = seen
            if seen.get("told") or now - seen["first"] < PERSIST or RULE not in rules.chosen(found): continue
            pending.setdefault(signal, finding)
        told = 0
        for signal, finding in pending.items():
            seen = kept[signal]
            skipped = seen.get("uncertain", [])
            if not isinstance(skipped, list): skipped = []
            notice = rules.finding_notice(finding, deliver.repository_of(directory))
            sent, refused, uncertain = deliver.send(notice, found, skip=skipped)
            if sent:
                seen["told"] = True
                told += 1
            if uncertain: seen["uncertain"] = sorted({*skipped, *uncertain})
            if sent or uncertain: write(directory, {"findings": kept})
            for words in (*refused.values(), *uncertain.values()): deliver.log(words)
            if not channels.on(found): deliver.log(f"a notice that {root}'s queue looks stuck went nowhere: no channel is on")
        write(directory, {"findings": kept})
        return findings, told
