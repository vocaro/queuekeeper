"""A look for a queue that is stuck without having written an event: what `qk queue doctor` finds.

Entries that wait with no worker, gates in a row with no verdict and the like change nothing in the journal, so no
delivery tells of them. Each command that reaches the hosts therefore starts a look, apart from itself and at most
once every EVERY seconds for a repository (spawn): the signed launcher runs such a command every two minutes, and
agents run them all day. A finding is told once it has lasted PERSIST seconds, so that the moment between a worker
ending and the next one starting is not news, and told again only after it has gone and come back. What it has
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
    EVERY seconds (plugins.HOST_COMMAND). It returns at once, and does nothing where the rule is off or the
    folder has no queue."""
    if visibility.switched_off() or RULE not in rules.chosen(deliver.settings()): return
    folder = Path.cwd()
    root = git_state.main_checkout(folder) or folder
    directory = directory_of(root)
    if not directory.is_dir(): return
    mark = directory / STATE
    try:
        if time.time() - mark.stat().st_mtime < EVERY: return
        os.utime(mark)  # before the look starts, so that commands started together start one
    except OSError: write(directory, {"findings": {}})
    deliver.start("--check", str(root))


def check(root, now=None, diagnose=None):
    """Look at the queue of the repository at root, and return (its findings, how many were told of now). A
    finding is told once, when it has lasted PERSIST seconds, through every channel that is on."""
    now = time.time() if now is None else now
    directory = directory_of(root)
    found = deliver.settings()
    with (directory / LOCK).open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        findings = [finding for finding in (diagnose or merge_queue.diagnose)(root)[0]
                    if finding.get("signal") not in SKIPPED]
        before, kept, told = read(directory).get("findings", {}), {}, 0
        for finding in findings:
            seen = before.get(finding["signal"])
            seen = seen if isinstance(seen, dict) and isinstance(seen.get("first"), (int, float)) else {"first": now}
            kept[finding["signal"]] = seen
            if seen.get("told") or now - seen["first"] < PERSIST or RULE not in rules.chosen(found): continue
            # Marked before it is sent: a finding is told at most once, as an event is.
            seen["told"] = True
            write(directory, {"findings": kept})
            sent, refused = deliver.send(rules.finding_notice(finding, deliver.repository_of(directory)), found)
            told += bool(sent)
            for words in refused: deliver.log(words)
            if not channels.on(found): deliver.log(f"a notice that {root}'s queue looks stuck went nowhere: no channel is on")
        write(directory, {"findings": kept})
        return findings, told
