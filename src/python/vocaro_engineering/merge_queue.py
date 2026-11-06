"""A local merge queue for a direct-main repository: `eng queue add`, and a session moves on.

One worker per repository takes everything waiting, cherry-picks it onto origin/main in a checkout
of its own, runs the project's queue lanes there once, and pushes on a pass. When the lanes fail on
a batch it halves the batch until it finds the commit to send back, landing the rest. Only a lane's
verdict blames a commit: a run the host left unmeasured runs again, each time once the host has settled,
until queue.noVerdictRuns runs in a row (3 by default) have given none, and then holds the queue for the
owner, with every commit still waiting (guides/maintenance.md, The merge queue). A batch whose lanes
passed is recorded as passed before its push, so a hold the push meets keeps that verdict, and
`eng queue resume` pushes the commit that passed instead of gating it again. Each round the worker moves to
the toolkit at origin/main, starting itself again from its copy, so that it judges main with the toolkit the
lanes run rather than the code it was started from (vocaro-engineering#173). A hold says whom it is for: the
owner only for a cause nobody else can fix; a session for every other, which resumes the queue itself once the
cause is dealt with or gone, after the tries a cause that clears by itself gets (worded, run).

The state is in the repository's common Git directory, so every worktree shares it, and a ref holds
each waiting commit, so a branch that moves or goes takes nothing with it:

    <common>/eng-queue/entries/ID.json   one commit range and its state
    <common>/eng-queue/logs/             what each round's lanes printed
    <common>/eng-queue/hold              why the queue stopped, until `eng queue resume`
    refs/eng-queue/ID                    the range's tip, until it lands or is removed
    ../vocaro-queue/NAME/NAME            the worker's checkout, in a folder beside the main checkouts
    ../vocaro-queue/NAME/TOOLKIT         its copy of the toolkit, which ../TOOLKIT names from the checkout
"""
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

from . import app_builds, budgets, disk, evidence, landing, processes, remote, resources
from .config import TOOLKIT_ROOT, load, parse

MAIN = "refs/remotes/origin/main"
FETCH = ["fetch", "--no-tags", "origin", f"refs/heads/main:{MAIN}"]
QUEUED, TESTING, PASSED, LANDED, FAILED, CONFLICT, REMOVED = \
    "queued", "testing", "passed", "landed", "failed", "conflict", "removed"
# Passed is a batch whose lanes passed and whose push has not landed it: Worker.keep and Worker.push_passed.
WAITING = {QUEUED, TESTING, PASSED}
# What a lane exits with when it says nothing about the commits: unmeasured, no admission, and a
# shared host's configuration, which stops for the owner.
NO_VERDICT = {evidence.UNMEASURED, resources.BUSY, remote.MISCONFIGURED}
# Whom a hold is for, in its reason's first words (worded): the owner only for a cause nobody else can fix, a
# misconfigured shared host, a refused push or credentials; a session for every other, and the session that deals
# with the cause, or finds it gone, clears it with `eng queue resume` itself, never asking the owner (owner's
# decision, 2026-11-04, vocaro-engineering#173). CLEARS, a cause that clears by itself, is tried again first (run).
OWNER, SESSION, CLEARS = "owner", "session", "clears"
# Seconds a whole lane may take with its wait for admission: a lane's own limit is 2 h by default
# (processes.DEFAULT_LIMIT), and this allows another two of them in the host's queue.
LANE_LIMIT = 3 * processes.DEFAULT_LIMIT
# How many runs in a row may give no verdict before the queue holds, unless main's queue.noVerdictRuns says
# otherwise: a busy host clears, so each run after the first waits for it to settle. On 2026-11-04, 8 of 12 of
# EPUBLib's gates on the second Mac gave none, as the simulators' voices were not ready in time (EPUBLib#946),
# and with one retry each pair held the queue until a person resumed it. A third run costs at most one gate and
# one settle wait more, and still holds the queue for a cause that does not clear.
NO_VERDICT_RUNS = 3
# Seconds each retry waits for the host to settle, by admission's load and memory test (resources.pressure), and
# between its looks. A retry started at once met the load that voided the run it repeats, and each such run cost
# mpm's queue a gate of about an hour (#124); a wait no longer than that run, past which the queue holds as
# before, costs no more than the retry it saves.
SETTLE_LIMIT = 3600
SETTLE_POLL = 15
# Seconds between the wait's lines in the worker's log while the host stays busy.
SETTLE_REPORT = 600
# eng's reading of a Mac's screen in a lane's log (cli.screen_reading, offload_host.screen_line).
SCREEN_LINE = re.compile(r"^eng: (screen: .*)$", re.MULTILINE)
# The most of an unmeasured test's line a hold's reason quotes.
UNMEASURED_SHOWN = 300
# Times main may move past a passed batch, in carried-over steps, before the batch is gated again.
CARRIES = 3
# What git says, over HTTPS or SSH, when it never reached the remote or the connection ended: a name that did
# not resolve, a connection refused or timed out, one that dropped. It says nothing of the commits, and a brief
# outage clears by itself (vocaro-engineering#113).
UNREACHED = re.compile(r"Could not resolve host|Temporary failure in name resolution|Failed to connect|Couldn't connect|"
                       r"Connection (refused|reset|closed|timed out)|Operation timed out|Network is unreachable|"
                       r"No route to host|hung up unexpectedly|Could not read from remote|send disconnect|early EOF|"
                       r"unexpected disconnect|SSL_ERROR_SYSCALL|Recv failure|Send failure|Empty reply from server")
# What says the remote answered and refused, which no retry changes: an authentication failure, a missing
# repository, a rejected push. SSH ends a refused key with the "Could not read from remote" of a dropped connection.
REFUSED = re.compile(r"denied|Authentication failed|could not read Username|returned error: 4\d\d|not found|"
                     r"rejected|Host key verification failed|does not appear to be a git repository")
# Seconds before each further try of a fetch or push that did not reach the remote; after the last, the round fails.
BACKOFF = (10, 30, 90, 270)
# Seconds before the round runs again after a cause that clears by itself, a remote out of reach past its backoff:
# one, five and fifteen minutes, about an hour with the backoffs, no longer than the gate a hold costs; after the
# last, the queue holds for a session (run).
CLEARING_WAITS = (60, 300, 900)
# What a remote says when it refused the key or the token. During GitHub's incident of 2026-11-04 its SSH said so of
# the owner's key from one second to the next, for over an hour, while the same key fetched (vocaro-engineering#151).
# A push refused so, seconds after a fetch from the same URL went through, is tried again (Worker.push_again).
AUTHENTICATION = re.compile(r"Permission denied \(publickey\)|Authentication failed|Invalid username or token")
# GitHub's status page, and its component for Git over SSH and HTTPS, which says whether a refusal may be GitHub's.
GITHUB_STATUS = "https://www.githubstatus.com/api/v2/components.json"
GIT_OPERATIONS = "Git Operations"
OPERATIONAL = "operational"
# Seconds between the tries of a refused push while GitHub says Git Operations is not operational, which go on for
# up to SETTLE_LIMIT: as long as a gate, which a hold would cost again.
REFUSAL_POLL = 60
POLL = 5
# Seconds between the worker's tries to bring the shared checkout to origin/main once nothing waits, while only a
# process running from it kept it behind, and how long it goes on: longer than a gate run from it (Worker.catch_up).
FOLLOW_POLL = 30
FOLLOW_LIMIT = 3600
# How Worker.stays begins the reasons catch_up tries again, and how many of the processes it names.
IN_USE = "a process started from it may still run its toolkit"
UNKNOWN_USE = "whether a process started from it still runs is unknown"
SHOWN_USERS = 5
# What a project's launcher reads first to find the toolkit.
TOOLKIT_VARIABLE = "VOCARO_ENGINEERING_ROOT"
# What carries a worker's bisect progress to the worker that replaces it on a newer toolkit (Worker.move_to).
PROGRESS_VARIABLE = "VOCARO_QUEUE_PROGRESS"
# The file in the queue's directory that names the toolkit commit a worker moved to (Worker.move_to) until the
# worker started from it finishes a round (Worker.arrived): one that died or held first is not moved to again.
MOVED = "moved"
# Seconds the toolkit at origin/main has to import what a worker runs before a worker moves to it (starts).
STARTS_LIMIT = 120
BEFORE_GATE = ["spelling", "provision"]
# eng land's emergency options, in every repository, by the entry field each sets: an entry tested next as a batch
# of its own, and one landed alone on the light lanes without the gate, whose written reason --why gives (owner's
# decision, 2026-11-04, vocaro-engineering#163). Neither is routine.
OPTIONS = {"front": "--front", "noGate": "--no-gate"}
# The lanes an ungated landing runs where the project has them: the light check a hand push ran.
UNGATED_LANES = ["spelling"]
# The trailer an ungated landing adds to its tip, so that main's history says so (Worker.mark_ungated).
UNGATED = "Landed-without-gate"
WAIT_LIMIT = 6 * 3600
KEPT_LOGS = 200
GIT_TIMEOUT = 600
# The folder, beside the main checkouts and outside every repository, that holds each queue's checkouts as
# the second Mac holds a run's (offload_host, checkouts/NAME/N/...): QUEUES/NAME/NAME and, beside it, the
# toolkit's copy, so that a project's ../vocaro-engineering finds that copy (owner's decision,
# vocaro-engineering#104). The folders beside the main checkout it replaces, NAME-queue and NAME-queue-toolkit,
# move there once nothing uses them.
QUEUES = "vocaro-queue"
# What locks eng land's scratch worktree while its trial integration lasts (trial), so that no sweep takes it.
TRIAL_REASON = "eng land's trial integration of a new entry, removed when it ends"
# The file whose conflicts the queue may settle, by its name in any directory and in any case.
CHANGELOG = "changelog.md"
HEADING = re.compile(rb"#{1,6}\s")
# The first line of a Markdown list item: a changelog entry.
ITEM = re.compile(rb"([-*+]|\d{1,9}[.)])\s")
# Long enough that no line of a changelog reads as one of `git merge-file`'s markers.
MARKER_SIZE = 32
# What SwiftPM keeps in a .build that records the checkout's absolute path: a moved checkout whose
# workspace-state.json still named ~/Development/studywright-queue failed its build (#114). Xcode's
# SourcePackages, a folder of a .build's DerivedData, keeps the same and goes whole.
PACKAGE_STATE = ("workspace-state.json", "artifacts", "checkouts", "repositories")
# Folders outside a .build that hold no package of the project's, and are too large to walk.
UNWALKED = {".git", ".venv", "node_modules"}


def forget(checkout, names):
    """Remove the package state under checkout whose workspace-state.json names any of names, the
    checkout's old paths, so that SwiftPM and Xcode resolve its packages again, and return the folders
    cleared. Build products stay. A workspace-state.json sits at the top of a .build, or in a
    SourcePackages two or three levels below it (.build/LANE/, .build/DerivedData/LANE/), so a walk goes no
    deeper than that inside one."""
    cleared = []
    for top, folders, files in os.walk(checkout):
        here = Path(top)
        inside = next((depth for depth, part in enumerate(reversed(here.relative_to(checkout).parts))
                       if part == ".build"), None)
        if "workspace-state.json" in files and any(
                name in (here / "workspace-state.json").read_text(errors="replace") for name in names):
            if here.name == "SourcePackages":
                shutil.rmtree(here)
                folders[:] = []
            else:
                for name in PACKAGE_STATE:
                    state = here / name
                    if state.is_dir() and not state.is_symlink(): shutil.rmtree(state)
                    elif state.exists() or state.is_symlink(): state.unlink()
            cleared.append(here)
        folders[:] = [folder for folder in folders if folder not in UNWALKED] if inside is None else \
            [folder for folder in folders if inside < 3 and folder not in PACKAGE_STATE[1:]]
    return cleared


def environment():
    """The environment without what a hook or an admission left in it: a pushing session's GIT_DIR
    would aim the worker's Git at its own checkout, and an inherited admission token would run the
    lanes as nested work whose owner has gone."""
    names = subprocess.run(["git", "rev-parse", "--local-env-vars"], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL)
    drop = set(names.stdout.split()) | {resources.TOKEN}
    return {key: value for key, value in os.environ.items() if key not in drop}


def git(directory, *arguments, hooks=True, check=False, timeout=GIT_TIMEOUT):
    """Git in directory. Without hooks, for what the worker does in its own checkout: a project's
    checkout hook may open an editor on the owner's screen (guides/maintenance.md)."""
    command = ["git", *([] if hooks else ["-c", "core.hooksPath=/dev/null"]), "-C", str(directory), *arguments]
    done = subprocess.run(command, capture_output=True, text=True, stdin=subprocess.DEVNULL, env=environment(),
                          timeout=timeout)
    if check: checked(done, arguments)
    return done


class Hold(ValueError):
    """Why the worker stops, and for whom: OWNER, SESSION or CLEARS (worded, run)."""
    def __init__(self, kind, why):
        super().__init__(why)
        self.kind = kind


class Refused(ValueError):
    """An entry eng land queued nothing of, since the round's integration of it does not apply (check). eng land and
    `eng queue add` exit 1 for it, as `eng land --wait` does for an entry that came back."""


def worded(kind, cause):
    """A hold's reason: whom it is for, in its first words, then its cause, then who runs `eng queue resume`. A hold
    is for the owner only when nobody else can fix its cause; every other is a session's, and the session that deals
    with the cause, or finds it gone, resumes the queue itself and says so, never asking the owner, whose standing
    instruction is that landings keep moving without him (2026-11-04, vocaro-engineering#173)."""
    if kind == OWNER:
        return (f"for the owner, since nobody else can fix it: {cause}; once it is fixed, any session that finds the "
                "cause gone runs `eng queue resume` and says so")
    return (f"for a session, not the owner: {cause}; the session that fixes it, or any session that finds it gone, "
            "runs `eng queue resume` itself and says so")


def failure(done, arguments, tries=1):
    return (f"`git {shlex.join(arguments)}` exited {done.returncode}{f' {tries} times' if tries > 1 else ''}: "
            f"{done.stderr.strip() or 'no output'}")


def checked(done, arguments, tries=1):
    if done.returncode: raise ValueError(failure(done, arguments, tries))
    return done


def unreached(said):
    """Whether git's stderr says it did not reach the remote, rather than that the remote refused."""
    return bool(UNREACHED.search(said)) and not REFUSED.search(said)


def pause(seconds):
    """The backoff's wait, apart from every other sleep, for a test to replace."""
    time.sleep(seconds)


def now():
    """The monotonic clock a settling wait reads, for a test to replace."""
    return time.monotonic()


def reach(directory, *arguments, say=print, check=False):
    """Git in directory for what talks to the remote, a fetch or a push: one that did not reach it is tried
    again after each of BACKOFF, and only then fails, as anything else does at once. With check, a failure is a
    Hold: one that clears by itself for a remote still out of reach, the owner's for a refusal, which only he
    can fix, and a session's for anything else."""
    for tries, delay in enumerate((*BACKOFF, None), start=1):
        done = git(directory, *arguments)
        said = done.stderr.strip()
        if done.returncode == 0 or delay is None or not unreached(said): break
        say(f"`git {arguments[0]}` did not reach the remote, so trying again in {delay}s: {said.splitlines()[-1]}")
        pause(delay)
    if check and done.returncode:
        raise Hold(CLEARS if unreached(said) else OWNER if REFUSED.search(said) else SESSION, failure(done, arguments, tries))
    return done


def on_github(url):
    """Whether a remote's URL is GitHub's, whose status page can say whether a refusal is its own."""
    return bool(re.match(r"(?:ssh://)?git@github\.com[:/]|https://(?:[^@/]+@)?github\.com/", url))


def git_operations():
    """(status, None) of GitHub's Git Operations, such as operational or partial_outage, or (None, why not)."""
    asked = urllib.request.Request(GITHUB_STATUS, headers={"User-Agent": "vocaro-engineering", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(asked, timeout=20) as answer: components = json.load(answer)["components"]
        status = next((part.get("status") for part in components if part.get("name") == GIT_OPERATIONS), None)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        return None, f"GitHub's status could not be read: {error}"
    if not isinstance(status, str): return None, f"GitHub's status names no {GIT_OPERATIONS}"
    return status, None


def text(directory, *arguments):
    return git(directory, *arguments, check=True).stdout.strip()


def lanes_of(config):
    """The lanes a batch must pass, in order: the project's spelling and provision lanes when it has
    them, the cheap checks and setup that a gate assumes, and then `queue.lanes` or its gate lane."""
    lanes = config.get("lanes", {})
    named = config.get("queue", {}).get("lanes") or (["gate"] if "gate" in lanes else None)
    if not named:
        raise ValueError(f"{config.get('name')} has no gate lane, so the queue has nothing to run: name the lane "
                         "holding what a commit must pass before it lands gate (guides/maintenance.md, "
                         "The merge queue)")
    return [name for name in BEFORE_GATE if name in lanes and name not in named] + named


def config_at(root, commit):
    """The project's configuration as commit has it, not as a checkout's working tree does, which
    may lag: the queue serves every session from one main checkout that nobody keeps current."""
    shown = git(root, "show", f"{commit}:engineering.json")
    if shown.returncode: raise ValueError(f"{commit} has no engineering.json: {shown.stderr.strip() or 'no output'}")
    return parse(shown.stdout)


def blank(line):
    return not line.strip()


def both_added(before, ours, base, theirs, held):
    """What one conflict block becomes, or None unless each side only adds whole lines at one place in a
    section, with none of the commit's lines already in the file: the new lines, then those ahead of them.
    The place is directly below a heading or, when each side's lines start an entry, below an entry, as
    when two branches each put their line second (#130). A blank line both sides add around their lines
    stays where it is; any other blank line, or a heading, is no longer a list of entries, and keeping
    both could put them under the wrong section."""
    if base or not ours or not theirs or not all(line.endswith(b"\n") for line in ours + theirs): return None
    start = end = 0
    shortest = min(len(ours), len(theirs))
    while start < shortest and blank(ours[start]) and blank(theirs[start]): start += 1
    while end < shortest - start and blank(ours[-1 - end]) and blank(theirs[-1 - end]): end += 1
    new, old = theirs[start:len(theirs) - end], ours[start:len(ours) - end]
    if not new or not old or any(blank(line) or HEADING.match(line) for line in new + old): return None
    above = next((line for line in reversed(before + ours[:start]) if not blank(line)), b"")
    if not HEADING.match(above) and not (ITEM.match(above) and ITEM.match(new[0]) and ITEM.match(old[0])): return None
    stripped = [line.strip() for line in new]
    if len(set(stripped)) != len(stripped) or set(stripped) & held: return None
    return ours[:start] + new + old + ours[len(ours) - end:]


def keep_both(base, ours, theirs):
    """The file, as bytes, with each of its conflicts settled by both_added, or None when it has none or
    both_added refuses any: theirs is the commit being applied, and ours what is ahead of it."""
    with tempfile.TemporaryDirectory() as directory:
        paths = [Path(directory) / name for name in ("ours", "base", "theirs")]
        for path, content in zip(paths, (ours, base, theirs)): path.write_bytes(content)
        merged = subprocess.run(["git", "merge-file", "-p", "--diff3", f"--marker-size={MARKER_SIZE}", *map(str, paths)],
                                capture_output=True, stdin=subprocess.DEVNULL, env=environment(), timeout=GIT_TIMEOUT)
    # Its status is how many conflicts it left, and above 127 an error.
    if not 0 < merged.returncode <= 127: return None
    open_, middle, split, close = (mark * MARKER_SIZE for mark in (b"<", b"|", b"=", b">"))
    held = {line.strip() for line in ours.splitlines()}
    kept, block = [], None
    for line in merged.stdout.splitlines(keepends=True):
        if line.startswith(open_): block = current = [[], [], []]; side = 0
        elif block is None: kept.append(line)
        elif line.startswith(middle): side = 1
        elif line.startswith(split): side = 2
        elif line.startswith(close):
            settled = both_added(kept, current[0], current[1], current[2], held)
            if settled is None: return None
            kept += settled
            block = None
        else: current[side].append(line)
    return b"".join(kept)


def named(path, key):
    """Whether a queue setting's key names path: a directory ending in /, or the file itself."""
    return path.startswith(key) if key.endswith("/") else path == key


def no_verdict_runs(config):
    """How many runs in a row may give no verdict before the queue holds: main's queue.noVerdictRuns, or
    NO_VERDICT_RUNS."""
    return config.get("queue", {}).get("noVerdictRuns", NO_VERDICT_RUNS)


def ungated_lanes(config):
    """The lanes an ungated landing runs instead of the queue's: UNGATED_LANES that the project has."""
    return [name for name in UNGATED_LANES if name in config.get("lanes", {})]


def ahead(front, no_gate, why):
    """The entry fields eng land's options set, or a refusal: --no-gate needs a written reason, --why, which the
    landing records everywhere it says it skipped the gate; --front takes one too, but needs none."""
    if no_gate and not (why and why.strip()):
        raise ValueError("--no-gate needs a written reason: `eng land --no-gate --why TEXT`, which the entry, the log, "
                         "the landed commit's trailer and `eng queue status` record (guides/maintenance.md, Landing "
                         "ahead of the queue)")
    if why and not (front or no_gate): raise ValueError("--why is the reason for --front or --no-gate")
    flags = {name: True for name, wanted in (("front", front), ("noGate", no_gate)) if wanted}
    # On one line, as a trailer needs.
    if flags and why and why.strip(): flags["aheadWhy"] = " ".join(why.split())
    return flags


def docs_lanes(config, changed):
    """The lanes main's queue.docsOnly names when every path a batch changes is one it lists, else None:
    a batch that changes nothing, or anything else, runs every lane."""
    declared = config.get("queue", {}).get("docsOnly")
    if not declared or not changed: return None
    if all(any(named(path, key) for key in declared["paths"]) for path in changed): return declared["lanes"]
    return None


def carried_over(path, config, batch_files):
    """Whether main changing path leaves a passed batch's result standing: a path the project lists
    in queue.carryOver as read by none of its queue lanes, or a Markdown document, and in either
    case one the batch does not touch."""
    if path in batch_files: return False
    listed = config.get("queue", {}).get("carryOver", {})
    return path.endswith(".md") or any(named(path, key) for key in listed)


def carry_checks(config, moved, base):
    """The commands a carry-over over moved must pass on the rebased tree: `eng docs check` when a document
    moved, then each of queue.carryOverChecks whose paths include a moved file, or that has none. {base} in
    a command is the commit the result passed on, so a check that compares with origin/main, which the
    batch now sits on, can read what main's new commits added."""
    commands = [["python3", "scripts/eng", "docs", "check"]] if any(path.endswith(".md") for path in moved) else []
    for check in config.get("queue", {}).get("carryOverChecks", []):
        if "paths" not in check or any(named(path, key) for path in moved for key in check["paths"]):
            commands.append([word.replace("{base}", base) for word in check["command"]])
    return commands


class Queue:
    def __init__(self, root):
        self.root = Path(root)
        common = Path(text(root, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        self.main_checkout = common.parent
        self.directory = common / "eng-queue"
        self.entries = self.directory / "entries"
        self.logs = self.directory / "logs"
        self.hold_file = self.directory / "hold"
        self.home = self.main_checkout.parent / QUEUES / self.main_checkout.name
        self.checkout = self.home / self.main_checkout.name
        # Where an earlier worker kept it, beside the main checkout, until it moves (Worker.place).
        self.old_checkout = self.main_checkout.parent / f"{self.main_checkout.name}-queue"
        self.entries.mkdir(parents=True, exist_ok=True)
        self.logs.mkdir(exist_ok=True)

    @contextlib.contextmanager
    def locked(self):
        """Every read-then-write of the entries, between sessions and the worker."""
        with (self.directory / "state.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def save(self, entry):
        path = self.entries / f"{entry['id']}.json"
        staged = path.with_suffix(f".{os.getpid()}.tmp")
        staged.write_text(json.dumps(entry, indent=2) + "\n")
        os.replace(staged, path)

    def read(self, identifier):
        try: return json.loads((self.entries / f"{identifier}.json").read_text())
        except (OSError, ValueError): return None

    def all(self):
        """Every entry, oldest first."""
        found = []
        for path in self.entries.glob("*.json"):
            with contextlib.suppress(OSError, ValueError): found.append(json.loads(path.read_text()))
        return sorted(found, key=lambda entry: (entry["submitted"], entry["id"]))

    def waiting(self):
        return [entry for entry in self.all() if entry["state"] in WAITING]

    def settle(self, entry, state, **fields):
        """A final state. The ref goes with a landed or removed range; a failed one keeps it until
        its author removes the entry, since its branch may be gone."""
        with self.locked():
            current = self.read(entry["id"]) or entry
            if current["state"] not in WAITING: return current
            current.update(state=state, settled=time.time(), **fields)
            self.save(current)
        if state in (LANDED, REMOVED): git(self.root, "update-ref", "-d", f"refs/eng-queue/{entry['id']}")
        return current

    def record(self, entry, **fields):
        """Fields of a waiting entry's record, for the entry nobody has removed meanwhile."""
        with self.locked():
            current = self.read(entry["id"])
            if current and current["state"] in WAITING:
                current.update(fields)
                self.save(current)

    def mark(self, entries, state):
        """Queued or testing, for the entries nobody has removed meanwhile. An entry that goes back to the
        queue leaves behind the batch whose pass it held, and so that pass."""
        with self.locked():
            for entry in entries:
                current = self.read(entry["id"])
                if current and current["state"] in WAITING:
                    current["state"] = state
                    current.pop("passed", None)
                    self.save(current)

    def held(self):
        try: return self.hold_file.read_text().strip()
        except OSError: return None

    def hold(self, why):
        self.hold_file.write_text(why + "\n")
        print(f"queue: held: {why}", flush=True)

    @contextlib.contextmanager
    def worker(self):
        """The repository's one worker, or None while another process is it."""
        with (self.directory / "worker.lock").open("w") as lock:
            try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                yield False
                return
            yield True

    def working(self):
        with self.worker() as free: return not free

    def command(self, toolkit):
        """What runs this repository's worker on the toolkit at toolkit: `eng queue run` for the main checkout, from
        that toolkit's Python, as start starts one and as a worker starts itself again on a newer toolkit
        (Worker.move_to)."""
        script = ("import sys; sys.path.insert(0, sys.argv[1]); from vocaro_engineering.cli import main; "
                  "sys.exit(main(['--project', sys.argv[2], 'queue', 'run']))")
        return [sys.executable, "-c", script, str(Path(toolkit) / "src/python"), str(self.main_checkout)]

    def start(self):
        """Start a worker apart from this session, so that it outlives the session's turn and its
        task stop. One that finds another working exits at once."""
        if self.held() or not self.waiting() or self.working(): return
        with (self.directory / "worker.log").open("a") as log:
            started = subprocess.Popen(self.command(TOOLKIT_ROOT), cwd=self.main_checkout, stdin=subprocess.DEVNULL,
                                       stdout=log, stderr=subprocess.STDOUT, env=environment(), start_new_session=True)
            # It is meant to outlive this process, which must not wait for it, but one that ends first
            # stays a zombie child until reaped: a long-lived caller, `eng land --wait` or a test run, sees it.
            threading.Thread(target=started.wait, daemon=True).start()


def repository(root, named):
    """The repository `eng land --repo` names: a path, or the name of a project beside this one's
    main checkout, as a session fixing a dependency names it."""
    candidates = [Path(named).expanduser(), Queue(root).main_checkout.parent / named]
    for path in candidates:
        if (path / "engineering.json").is_file() and git(path, "rev-parse", "--git-dir").returncode == 0:
            return path.resolve()
    raise ValueError(f"--repo {named} names no project: give a checkout's path, or the name of a project beside "
                     f"{Queue(root).main_checkout}")


def land(root, args):
    """`eng land`: add to this repository's queue or, with --repo, another's, whose own queue, lanes and
    worker take it exactly as they take that repository's own sessions' commits. With another
    repository's main checkout HEAD would be whatever it has checked out, so the commit is named."""
    target = root
    if args.repo:
        target = repository(root, args.repo)
        if args.reference is None:
            raise ValueError(f"--repo lands a commit you name: `eng land --repo {args.repo} BRANCH`, since HEAD "
                             "there is another session's")
    try:
        identifier = add(target, args.reference or "HEAD", args.note, quiet_wait=args.wait,
                         front=getattr(args, "front", False), no_gate=getattr(args, "no_gate", False),
                         why=getattr(args, "why", None))
    except Refused as error:
        print(f"eng: {error}", file=sys.stderr)
        return 1
    status_ = wait(target, identifier, args.limit) if args.wait and identifier is not None else 0
    warn(target)
    return status_


def warn(root):
    """After eng land's result, the gate over its budget and main past its release commit limit, by main's
    settings (budgets.py)."""
    for warning in budgets.landing_warnings(root, config_at(root, MAIN), Queue(root).all()):
        print(f"warning: {warning}", flush=True)


def add(root, reference, note=None, quiet_wait=False, front=False, no_gate=False, why=None):
    """Put the commits reference has and origin/main lacks in the queue, and return at once with the
    entry's ID, or None when there is nothing to queue. Whether it may be queued is what a round would
    find: origin/main's settings, and origin/main's lanes or, until it has any, the commit's, and the round's
    integration of it after the entries ahead of it, which a conflict refuses before it is queued (check). front and
    no_gate, eng land's emergency options --front and --no-gate, and why, the reason --why gives (ahead), go
    in the entry; an entry already queued with the same tip takes them."""
    flags = ahead(front, no_gate, why)
    queue = Queue(root)
    reach(root, *FETCH, say=lambda words: print(f"queue: {words}", file=sys.stderr), check=True)
    tip = text(root, "rev-parse", "--verify", f"{reference}^{{commit}}")
    config = config_at(root, MAIN)
    if config.get("integration", "direct-main") != "direct-main":
        raise ValueError(f"{config.get('name')} integrates by pull request, which the queue does not push to")
    try: lanes_of(config)
    except ValueError: lanes_of(config_at(root, tip))
    if git(root, "merge-base", "--is-ancestor", tip, MAIN).returncode == 0:
        print(f"origin/main already holds {tip[:12]}: nothing to queue")
        return None
    base = text(root, "merge-base", tip, MAIN)
    commits = text(root, "rev-list", "--reverse", f"{base}..{tip}").split()
    merges = text(root, "rev-list", "--merges", f"{base}..{tip}").split()
    if merges:
        raise ValueError(f"{merges[0][:12]} is a merge, which a queue would have to put on main: rebase the branch's "
                         "own commits onto origin/main and add it again")
    if reference == "HEAD" and git(root, "status", "--porcelain").stdout.strip():
        print("queue: the checkout has uncommitted changes, which stay out: only commits are queued", file=sys.stderr)
    branch = git(root, "symbolic-ref", "--short", "-q", "HEAD").stdout.strip() if reference == "HEAD" else \
        git(root, "rev-parse", "--abbrev-ref", reference).stdout.strip()
    with queue.locked():
        waiting = [entry for entry in queue.all() if entry["state"] in WAITING]
        same = next((entry for entry in waiting if entry["tip"] == tip), None)
    if same:
        print(f"{same['id']} already holds {tip[:12]} ({same['state']})")
        if flags: promote(queue, same, flags)
        queue.start()
        return same["id"]
    # A branch added again replaces what it added before; main names nobody's work in particular.
    replaced = [entry for entry in waiting
                if branch and branch not in ("main", "HEAD") and entry.get("branch") == branch]
    # What a round would find, before anything is queued or replaced: a refused entry leaves the queue as it was.
    check(root, queue, {"id": "new", "tip": tip, "commits": commits},
          [] if front else round_order([entry for entry in waiting if entry not in replaced]), front)
    for entry in replaced:
        remove(queue, entry["id"], quiet=True)
        print(f"queue: replaced {entry['id']}, the branch's earlier commits", file=sys.stderr)
    now = time.time()
    identifier = f"{time.strftime('%Y%m%d-%H%M%S', time.localtime(now))}-{tip[:8]}"
    entry = {"id": identifier, "tip": tip, "base": base, "commits": commits, "branch": branch or None, "note": note,
             "subject": text(root, "log", "-1", "--format=%s", tip), "submitted": now, "state": QUEUED,
             "directory": str(root), **flags}
    text(root, "update-ref", f"refs/eng-queue/{identifier}", tip)
    with queue.locked(): queue.save(entry)
    print(f"queued {identifier}: {len(commits)} commit(s), {entry['subject']}")
    for name in OPTIONS:
        if name in flags: print(f"queue: {identifier} {asked_for(name)}{because(flags)}")
    why = queue.held()
    if why: print(f"queue: {identifier} waits, since the queue is held {why}", file=sys.stderr)
    queue.start()
    if not quiet_wait:
        print("nothing else is needed for it to land; to be told at once if it comes back, run as a background "
              f"command: python3 {shlex.quote(str(root))}/scripts/eng queue wait {identifier}")
    return identifier


def check(root, queue, entry, ahead, front):
    """Refuse entry, queuing nothing, when the trial integration a round would make now does not apply it onto
    origin/main after ahead, the waiting entries ahead of it: --front tests it alone, so against origin/main only.
    It is a hint, found while the author still has the context: the round integrates again and sends back what this
    could not see, as main or the entries ahead change in between. A trial that cannot be made queues the entry, for
    the round to find any conflict."""
    base = text(root, "rev-parse", MAIN)
    began = now()
    try: found = trial(queue, entry, ahead, base)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        print(f"queue: could not check it against the entries ahead, so the round finds any conflict: {error}",
              file=sys.stderr)
        return
    if found["why"]: raise Refused(refusal(entry["tip"], found, base))
    for line in tried(found, base, front, now() - began): print(f"queue: {line}", file=sys.stderr)


def asked_for(name):
    """What an entry's option, a name of OPTIONS, does, for eng land's lines and `eng queue status`."""
    return ("is tested next, ahead of everything waiting, as a batch of its own" if name == "front" else
            "lands alone on the light lanes, without the gate, and is recorded as ungated")


def because(entry):
    """The reason --why gave an entry's options, for a line that names them."""
    return f", because {entry['aheadWhy']}" if entry.get("aheadWhy") else ""


def promote(queue, entry, flags):
    """Give a queued entry the options flags holds, as eng land does an entry already holding its tip. A batch in its
    gate, or one that passed, finishes as it is, so for one of those the options change nothing."""
    with queue.locked():
        current = queue.read(entry["id"])
        if current and current["state"] == QUEUED:
            current.update(flags)
            queue.save(current)
    if current and current["state"] == QUEUED:
        for name in OPTIONS:
            if name in flags: print(f"queue: {entry['id']} now {asked_for(name)}{because(current)}")
        return
    print(f"queue: {entry['id']} is {current['state'] if current else 'gone'}, so "
          f"{' and '.join(OPTIONS[name] for name in OPTIONS if name in flags)} changes nothing now", file=sys.stderr)


def covered(root, entry, entries):
    """Whether a gate on main has passed over an ungated landing since: a later landing whose gate ran on a base
    that holds it (Worker.push's gatedOn)."""
    return any(other.get("gatedOn") and other.get("settled", 0) >= entry.get("settled", 0)
               and git(root, "merge-base", "--is-ancestor", entry["landedAs"], other["gatedOn"]).returncode == 0
               for other in entries if other["state"] == LANDED)


def remove(queue, identifier, quiet=False):
    entry = queue.read(identifier)
    if entry is None: raise ValueError(f"The queue has no entry {identifier}")
    if entry["state"] in WAITING: queue.settle(entry, REMOVED)
    else: git(queue.root, "update-ref", "-d", f"refs/eng-queue/{identifier}")
    if not quiet: print(f"removed {identifier}")
    return 0


def describe(entry):
    line = f"{entry['state']:8} {entry['id']}  {entry['subject']}"
    if entry.get("landedAs"):
        # The entry's own last commit on main; the batch's tip beside it when another entry's commit is the tip.
        line += f"  -> {entry['landedAs'][:12]}"
        if entry.get("landedCommits", 1) > 1: line += f" ({entry['landedCommits']} commits)"
        if entry.get("landedTip", entry["landedAs"]) != entry["landedAs"]:
            line += f", in batch tip {entry['landedTip'][:12]}"
    if entry["state"] == PASSED:
        verdict = entry["passed"]
        line += (f"\n         passed at {verdict['head'][:12]} on {verdict['base'][:12]}: the push lands it without "
                 "gating it again")
    if entry.get("docsOnly"): line += f"\n         docs lanes only ({', '.join(entry['docsOnly'])}): no gate of that commit"
    if entry.get("ungated"):
        line += (f"\n         landed without the gate, on {', '.join(entry['ungated']['lanes']) or 'no lane'}, because "
                 f"{entry['ungated']['why']}; the next gate on main covers it")
    elif entry["state"] in WAITING:
        for name in OPTIONS:
            if entry.get(name): line += f"\n         {asked_for(name)} ({OPTIONS[name]}){because(entry)}"
    for kept in entry.get("kept", []): line += f"\n         {kept}"
    if entry.get("why"): line += f"\n         {entry['why']}"
    if entry.get("log"): line += f"\n         log: {entry['log']}"
    return line


def status(root, as_json=False, recent=5):
    queue = Queue(root)
    entries = queue.all()
    waiting = [entry for entry in entries if entry["state"] in WAITING]
    done = sorted((entry for entry in entries if entry["state"] not in WAITING),
                  key=lambda entry: entry.get("settled", 0))[-recent:]
    working, why = queue.working(), queue.held()
    # Shown until a gate on main passes over it, however long ago it landed.
    ungated = [entry for entry in entries if entry["state"] == LANDED and entry.get("ungated")
               and not covered(root, entry, entries)]
    if as_json:
        print(json.dumps({"worker": working, "held": why, "waiting": waiting, "recent": done,
                          "ungated": [entry["id"] for entry in ungated]}, indent=2))
        return 0
    print(f"worker: {'running' if working else 'none'}" + (f"; held {why}" if why else ""))
    if waiting and not working and not why: print("waiting with no worker: `eng queue resume` starts one")
    for entry in ungated:
        print(f"ungated on main: {entry['id']} landed {entry['landedAs'][:12]} without the gate, which the next gate "
              "on main covers")
    for entry in waiting + done: print(describe(entry))
    return 0


def wait(root, identifier, limit=WAIT_LIMIT):
    """Exit 0 once the entry lands, 1 when it comes back to its author, 75 while the queue is held or
    the limit passes with the entry still waiting. A worker a reboot ended starts again here."""
    queue = Queue(root)
    deadline = time.monotonic() + limit
    while True:
        entry = queue.read(identifier)
        if entry is None: raise ValueError(f"The queue has no entry {identifier}")
        if entry["state"] not in WAITING:
            print(describe(entry))
            # In full, for the pin a dependent project moves to.
            if entry["state"] == LANDED: print(f"landed: {entry['landedAs']}")
            return 0 if entry["state"] == LANDED else 1
        why = queue.held()
        if why:
            print(f"{identifier} is waiting, and the queue is held {why}")
            return resources.BUSY
        if time.monotonic() >= deadline:
            print(f"{identifier} is still {entry['state']} after {limit:g}s")
            return resources.BUSY
        queue.start()
        time.sleep(POLL)


def running():
    """The commit the worker's own code came from: HEAD of the toolkit checkout that scripts/eng resolved, or
    None where that is no repository."""
    head = git(TOOLKIT_ROOT, "rev-parse", "--verify", "-q", "HEAD")
    return head.stdout.strip() if head.returncode == 0 and head.stdout.strip() else None


def execute(command, env):
    """Replace this process with command in env, for a test to replace: a worker's move to a newer toolkit
    (Worker.move_to). It returns only when the exec failed."""
    os.execve(command[0], command, env)


def starts(toolkit, commit):
    """None when the toolkit at commit imports what a worker runs, else why not: a worker checks this in a
    process of its own, from commit's files, before it moves there (Worker.toolkit), so that a commit that does
    not start leaves the worker running on its own code rather than replacing it with nothing."""
    archive = subprocess.run(["git", "-C", str(toolkit), "archive", "--format=tar", commit, "src/python"],
                             capture_output=True, stdin=subprocess.DEVNULL, env=environment(), timeout=GIT_TIMEOUT)
    if archive.returncode:
        return f"`git archive` of it failed: {archive.stderr.decode(errors='replace').strip() or 'no output'}"
    with tempfile.TemporaryDirectory(prefix="eng-queue-toolkit-") as folder:
        # By tar rather than tarfile, whose safe extraction Python 3.9 lacks; the archive is the toolkit's own.
        unpacked = subprocess.run(["tar", "-x", "-f", "-", "-C", folder], input=archive.stdout, capture_output=True,
                                  timeout=GIT_TIMEOUT)
        if unpacked.returncode:
            return f"its files could not be unpacked: {unpacked.stderr.decode(errors='replace').strip() or 'no output'}"
        script = ("import sys; sys.path.insert(0, sys.argv[1]); "
                  "import vocaro_engineering.cli, vocaro_engineering.merge_queue")
        try:
            tried = subprocess.run([sys.executable, "-c", script, str(Path(folder) / "src/python")], cwd=folder,
                                   capture_output=True, text=True, stdin=subprocess.DEVNULL, env=environment(),
                                   timeout=STARTS_LIMIT)
        except subprocess.TimeoutExpired: return f"importing it took more than {STARTS_LIMIT}s"
    if tried.returncode == 0: return None
    said = [line for line in tried.stderr.splitlines() if line.strip()]
    return f"importing it exited {tried.returncode}: {said[-1].strip() if said else 'no output'}"


def carried():
    """The bisect progress the worker this one replaces left in the environment (Worker.move_to), taken out of it
    so that no lane inherits it, or None."""
    kept = os.environ.pop(PROGRESS_VARIABLE, None)
    if not kept: return None
    try: progress = json.loads(kept)
    except ValueError: return None
    return progress if isinstance(progress, dict) else None


class Worker:
    def __init__(self, queue, progress=None):
        self.queue = queue
        self.checkout = queue.checkout
        self.limit = None  # how many of the oldest entries the next round takes, while bisecting
        self.suspects = 0  # how many of the oldest entries hold a commit that fails
        self.no_verdicts = 0  # the runs in a row that gave no verdict, held at no_verdict_runs
        if progress:  # what the worker this one replaces had reached (progress)
            self.limit, self.suspects = progress.get("limit"), progress.get("suspects", 0)
            self.no_verdicts = progress.get("no_verdicts", 0)
        self.code = running()  # the commit this worker's code came from, which toolkit compares with the copy's
        self.rounds = 0  # the rounds this worker has finished, which run reads to count its tries
        self.deferred = None  # why follow last left the shared checkout behind for a run from it, or None
        self.timed_out = None  # why the last gate's lane reached its limit, or None (gate)
        self.ran_on = None  # the second Mac that ran the last lane the gate ran, or None (gate)
        self.environment = environment()  # what the lanes, the hook and the carry-over checks run in
        self.measured = None  # the passing gate run of the last round, which its landed entries record
        self.alone = False  # whether this round's batch is one entry an option of eng land put on its own (choose)

    def say(self, words):
        print(f"queue: {time.strftime('%H:%M:%S')} {words}", flush=True)

    def progress(self):
        """What a worker that takes over from this one, on a newer toolkit or after a cause that cleared, goes on
        from: its bisect and its count of runs with no verdict. Every entry's state is on disk already."""
        return {"limit": self.limit, "suspects": self.suspects, "no_verdicts": self.no_verdicts}

    def look(self, host, said):
        """(pressure, one-minute load, where it was read) of the Mac a lane ran on: host, the second Mac, by
        remote.pressure_there, or with host None this Mac. A second Mac that cannot be read gives this Mac's
        reading, saying so when that starts, in said, and again once it can be read."""
        if host is not None:
            try:
                busy, load = remote.pressure_there(host)
                if said.get("fallback"): self.say(f"{host}'s load can be read again: reading it")
                said["fallback"] = False
                return (None if busy is None else (busy[0], f"on {host}, {busy[1]}")), load, host
            except remote.Unavailable as error:
                if not said.get("fallback"):
                    self.say(f"{host}, where the lanes ran, cannot be read, so the wait reads this Mac's load instead: "
                             f"{error}")
                said["fallback"] = True
        return resources.pressure(), resources.one_minute_load(), "the host"

    def settle(self, what, host=None):
        """None once the host has spare capacity by admission's test (resources.pressure), so that the retry of
        what gave no verdict does not meet the load that voided it; else, past SETTLE_LIMIT, why it has none.
        With host, the second Mac that ran what gave no verdict, the test reads that Mac (look). It logs what
        it saw, and a stop request ends it as one ends a lane (processes.Interrupted)."""
        start = reported = now()
        looks, seen = 0, None
        loads, said = [], {}
        while True:
            if processes.requested():
                raise processes.Interrupted(processes.requested(), f"while {what} waited for the host to settle")
            busy, load, where = self.look(host, said)
            looks += 1
            if load is not None: loads.append(load)
            waited = now() - start
            span = (f"; one-minute load first {loads[0]:g}, highest {max(loads):g}, last {loads[-1]:g}"
                    if loads else "; the load average was unavailable")
            if busy is None:
                if looks > 1: self.say(f"{where} settled after {int(waited)}s and {looks} looks{span}: {what} again")
                return None
            kind, reason = busy
            if looks == 1:
                self.say(f"{reason}: {what} again once {where} settles, waiting up to {SETTLE_LIMIT}s")
            elif (kind, where) != seen or now() - reported >= SETTLE_REPORT:
                self.say(f"still waiting after {int(waited)}s: {reason}")
                reported = now()
            seen = kind, where
            if waited >= SETTLE_LIMIT:
                self.say(f"{where} did not settle in {int(waited)}s and {looks} looks{span}")
                return f"{where} did not settle in {int(waited)}s ({reason})"
            pause(min(SETTLE_POLL, max(0, SETTLE_LIMIT - waited)))

    def prepare(self):
        """The worker's own checkout. Locked, so that no sweep takes it for an abandoned worktree."""
        self.place(self.queue.main_checkout, self.checkout, self.queue.old_checkout, "the merge queue's checkout")

    def place(self, repository, path, old, reason):
        """A locked worktree of repository at path. One an earlier worker kept at old moves there, with the
        ignored inputs its lanes provisioned, when no process uses it: this worker holds the queue, so no gate
        of its own runs there, and a process that does, a lane an earlier worker left running among them, keeps
        it where it is. Then path is made afresh, and old stays, locked as retired, for `eng disk` to list."""
        listed = text(repository, "worktree", "list", "--porcelain").splitlines()
        if f"worktree {path}" in listed: return
        if path.exists():
            raise Hold(SESSION, f"{path} exists and is no worktree of {repository}: move it")
        path.parent.mkdir(parents=True, exist_ok=True)
        if f"worktree {old}" in listed:
            why = self.move(repository, old, path)
            if why is None: return
            self.say(f"{old} stays where it is, {why}: making {path} afresh and leaving {old} for removal")
            # Relocked as retired, so that `eng disk` can show the owner that nothing needs it.
            git(repository, "worktree", "unlock", str(old), hooks=False)
            git(repository, "worktree", "lock", "--reason", f"{disk.RETIRED}: replaced by {path}", str(old), hooks=False)
        git(repository, "worktree", "add", "--detach", "--lock", "--reason", reason, str(path), MAIN, hooks=False,
            check=True)

    def move(self, repository, old, new):
        """Move the worktree at old to new and return None, or say why not, with it left as it was."""
        try: users = disk.users(disk.census(), old)
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            return f"since whether a process uses it is unknown: {error}"
        if users: return f"since {disk.described((users[0], 'working directory, an open file or an argument'))}"
        names = {str(old), str(old.resolve())}
        # Twice --force moves a locked worktree, which stays locked, with the reason it had.
        moved = git(repository, "worktree", "move", "--force", "--force", str(old), str(new), hooks=False)
        if moved.returncode: return f"since `git worktree move` failed: {moved.stderr.strip() or 'no output'}"
        self.say(f"moved {old} to {new}")
        try: cleared = forget(new, names)
        except OSError as error: self.say(f"could not clear the package state in {new} that names {old}: {error}")
        else:
            for state in cleared: self.say(f"cleared {state}, which named {old}")
        return None

    def toolkit(self):
        """Run this round's lanes on the toolkit at origin/main, in a copy no session edits: a shared
        checkout that lags, or holds a session's changes, says nothing of the commits (guides/maintenance.md,
        The merge queue). Each worker has its own, so that moving it changes no other project's running
        gate. The toolkit's own queue tests its batch, and an explicit VOCARO_ENGINEERING_ROOT stands.
        A copy that cannot be made or moved holds the queue, as a fetch of the project that keeps failing does.
        A copy at another commit than this worker's own code then replaces the worker with one started from it
        (move_to): what judges main, config.load among it, is the copy's code too, not the older code the worker was
        started from. The toolkit's own queue moves its worker the same way, to a copy of its own that no lane
        reads, since a running worker keeps the modules it loaded when follow moves the shared checkout under it
        (vocaro-engineering#177). A toolkit at origin/main that does not start, or that a worker moved to and
        stopped in before it finished a round, is not moved to (refuses): the worker goes on with its own code, and
        a project's lanes with the toolkit it was started from, so that the fix can land through this queue. A
        toolkit that is no repository, or an explicit root, keeps the code the worker runs."""
        self.environment = environment()
        if os.environ.get(TOOLKIT_VARIABLE): return
        common = git(TOOLKIT_ROOT, "rev-parse", "--path-format=absolute", "--git-common-dir")
        if common.returncode: return  # a toolkit that is no repository has no origin/main to follow
        own = common.stdout.strip() == text(self.queue.main_checkout, "rev-parse", "--path-format=absolute",
                                            "--git-common-dir")
        # The toolkit's own origin/main is the queue's, which the round has just fetched.
        if not own: reach(TOOLKIT_ROOT, *FETCH, say=self.say, check=True)
        head = text(TOOLKIT_ROOT, "rev-parse", MAIN)
        if self.code and head != self.code and self.refuses(head): head = self.code
        if own and (not self.code or head == self.code): return
        if own:
            # Beside the queue's checkout, which already has the toolkit's own name.
            copy = self.queue.home / f"{self.queue.main_checkout.name}-toolkit"
        else:
            # Named as the toolkit's main checkout is, which a project's relative path names from its own checkout.
            kit = Path(common.stdout.strip())
            copy = self.queue.home / (kit.parent.name if kit.name == ".git" else Path(TOOLKIT_ROOT).resolve().name)
        old = self.queue.old_checkout.with_name(f"{self.queue.old_checkout.name}-toolkit")
        self.place(TOOLKIT_ROOT, copy, old, "the merge queue's copy of the toolkit")
        git(copy, "checkout", "--force", "--detach", head, hooks=False, check=True)
        git(copy, "clean", "-fdq", hooks=False, check=True)
        head = text(copy, "rev-parse", "HEAD")
        self.say(f"toolkit {head[:12]} in {copy}")
        if not own: self.environment = {**environment(), TOOLKIT_VARIABLE: str(copy)}
        if self.code and head != self.code: self.move_to(copy, head)

    def refuses(self, head):
        """Why this worker does not move to the toolkit at head, origin/main, or None: a worker moved there before
        and stopped before it finished a round (MOVED), dying or holding, or head does not start (starts). Either
        would leave this queue with no worker that can land the fix; this worker's own code can."""
        why = ("a worker moved to it before and stopped before it finished a round" if self.moved() == head
               else starts(TOOLKIT_ROOT, head))
        if why: self.say(f"staying on the toolkit at {self.code[:12]}, which this worker was started from, rather "
                         f"than moving to {head[:12]}, origin/main: {why}")
        return why

    def moved(self):
        """The toolkit commit the last worker's move went to while no worker started there has finished a round."""
        try: return json.loads((self.queue.directory / MOVED).read_text()).get("to")
        except (OSError, ValueError, AttributeError): return None

    def arrived(self):
        """A worker that a move started (move_to) has finished a round: later workers may move to its code too."""
        if self.code and self.moved() == self.code: self.unmoved()

    def unmoved(self):
        """Forget the last move: its toolkit finished a round (arrived), or never ran, as an exec that failed."""
        with contextlib.suppress(OSError): (self.queue.directory / MOVED).unlink()

    def move_to(self, copy, head):
        """Go on as a worker started from copy, the toolkit at origin/main, in place of this process: the same
        command as Queue.start's, this process's environment, and this worker's progress. The worker judged main
        with the code it was started from, which lags origin/main once the toolkit lands under it: on 2026-11-04
        EPUBLib's worker, from a checkout older than vocaro-engineering#168, landed a commit whose gate lane set
        `wholeHost`, a key the copy accepted, and then held the queue on it, as its own config module refused it,
        until a person resumed it (vocaro-engineering#173). Nothing of a round is in flight here: it runs before
        the round's entries are touched, so every entry's state is on disk, and the new process takes the worker's
        lock as this one's closes with the exec. One that returns is a failed exec, which holds."""
        self.say(f"moving to the toolkit at {head[:12]}, origin/main, from {self.code[:12]}, which this worker was "
                 "started from: the next worker is started from the copy")
        sys.stdout.flush()
        (self.queue.directory / MOVED).write_text(json.dumps({"from": self.code, "to": head, "at": time.time()}) + "\n")
        try: execute(self.queue.command(copy), {**environment(), PROGRESS_VARIABLE: json.dumps(self.progress())})
        except OSError:
            self.unmoved()
            raise
        self.unmoved()
        raise RuntimeError(f"the worker could not start itself again from {copy}")

    def toolkits_own(self):
        """Whether this is the toolkit's own queue: the toolkit running the worker is a checkout of its repository."""
        common = git(TOOLKIT_ROOT, "rev-parse", "--path-format=absolute", "--git-common-dir")
        return common.returncode == 0 and common.stdout.strip() == \
            text(self.queue.main_checkout, "rev-parse", "--path-format=absolute", "--git-common-dir")

    def follow(self, quiet=False):
        """After a landing in the toolkit's own queue, fast-forward the shared checkout, the toolkit's main
        checkout, which every session's eng and the next worker read, to origin/main. Only when it is on main,
        clean, holds no commit of its own, is behind, and no process started from it still runs there, since
        such a run would mix two toolkits (owner's decision, vocaro-engineering#104). Otherwise it is left as it
        is, and the log says why; nothing of this holds the queue. One left only for such a run is deferred, which
        catch_up tries again; quiet, for those tries, says only a fast-forward."""
        shared = self.queue.main_checkout
        self.deferred = None
        try:
            why = self.stays(shared)
            if why is None:
                before = text(shared, "rev-parse", "HEAD")
                merged = git(shared, "merge", "--ff-only", "--quiet", MAIN, hooks=False)
                why = None if merged.returncode == 0 else \
                    f"`git merge --ff-only` failed: {merged.stderr.strip() or 'no output'}"
                if why is None:
                    self.say(f"fast-forwarded {shared} from {before[:12]} to {text(shared, 'rev-parse', 'HEAD')[:12]}")
                    return
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            why = str(error)
        if why and why.startswith((IN_USE, UNKNOWN_USE)): self.deferred = why
        if why and not quiet:
            self.say(f"left {shared} as it is: {why}" + ("; tried again once nothing waits" if self.deferred else ""))

    def stays(self, shared):
        """Why the shared checkout is not fast-forwarded now, '' when it is current, or None."""
        branch = git(shared, "symbolic-ref", "--short", "-q", "HEAD").stdout.strip()
        if branch != "main": return f"it is on {branch or 'a detached HEAD'}, not main"
        changed = git(shared, "status", "--porcelain", "--untracked-files=normal", check=True).stdout.splitlines()
        if changed: return f"it has {len(changed)} changed or untracked path(s), such as {changed[0][3:]}"
        head, target = text(shared, "rev-parse", "HEAD"), text(shared, "rev-parse", MAIN)
        if head == target: return ""
        if git(shared, "merge-base", "--is-ancestor", head, target).returncode:
            return f"its HEAD {head[:12]} has commits origin/main lacks"
        # What runs in a session's worktree inside it, or opens Git's own files, is not running from its tree; nor
        # is what names only the checkout itself, as every session names its project root (disk.runners, #182).
        listed = text(shared, "worktree", "list", "--porcelain").splitlines()
        apart = [Path(line[len("worktree "):]) for line in listed[1:] if line.startswith("worktree ")]
        apart.append(Path(text(shared, "rev-parse", "--path-format=absolute", "--git-common-dir")))
        try: users = disk.runners(disk.census(), shared, apart)
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            return f"{UNKNOWN_USE}: {error}"
        if users:
            named = "; ".join(f"PID {process.pid} ({process.command or process.args.split(' ')[0] or '?'}), "
                              f"{how} {path}" for process, how, path in users[:SHOWN_USERS])
            more = f"; and {len(users) - SHOWN_USERS} more" if len(users) > SHOWN_USERS else ""
            return f"{IN_USE}: {named}{more}"
        return None

    def catch_up(self):
        """After the worker's rounds, while nothing waits, try follow again every FOLLOW_POLL seconds for up to
        FOLLOW_LIMIT while only a process running from the shared checkout, or a census that failed, kept it
        behind; such a run ends by itself (vocaro-engineering#182). A checkout left for any other reason is a
        person's to settle. The next landing tries again either way. It looks once first, since a worker that
        moved to a newer toolkit after a landing (move_to) has not followed itself."""
        if self.queue.held() or not self.toolkits_own(): return
        self.follow(quiet=True)
        deadline = time.monotonic() + FOLLOW_LIMIT
        while self.deferred and time.monotonic() < deadline:
            pause(FOLLOW_POLL)
            if self.queue.held() or self.queue.waiting(): return
            self.follow(quiet=True)
        if self.deferred:
            self.say(f"left {self.queue.main_checkout} behind after {FOLLOW_LIMIT}s of tries: {self.deferred}")

    def reset(self, commit):
        """Exactly commit's tracked files. Ignored inputs a lane provisioned stay for the next round."""
        git(self.checkout, "cherry-pick", "--abort", hooks=False)
        git(self.checkout, "checkout", "--force", "--detach", commit, hooks=False, check=True)
        git(self.checkout, "clean", "-fdq", hooks=False, check=True)

    def head(self):
        return text(self.checkout, "rev-parse", "HEAD")

    def pick(self, entry, ahead=()):
        """Cherry-pick entry's commits onto HEAD. None when they apply, else why, with HEAD as it was. ahead
        names the entries applied before it, which a conflict names too, so that its author knows what to wait
        for before rebasing (#130)."""
        before = self.head()
        self.kept = []  # each changelog conflict it settled, which integrate records on the entry
        for commit in entry["commits"]:
            picked = git(self.checkout, "cherry-pick", commit, hooks=False)
            if picked.returncode == 0: continue
            conflicted = text(self.checkout, "diff", "--name-only", "--diff-filter=U").split()
            if not conflicted and not git(self.checkout, "status", "--porcelain").stdout.strip():
                # main already has this commit's change, as another route may have landed it.
                git(self.checkout, "cherry-pick", "--skip", hooks=False, check=True)
                continue
            if conflicted and self.keep_changelogs(conflicted, f"{entry['commits'][0]}^", before, commit):
                # -C, for the commit's own message and author, without Git's note of the conflict.
                git(self.checkout, "commit", "--no-verify", "-C", commit, hooks=False, check=True)
                self.kept.append(f"{commit[:12]}: kept both sides of {', '.join(conflicted)}, the new lines first")
                self.say(self.kept[-1])
                continue
            self.reset(before)
            self.conflicted = conflicted  # which Trial reads, to name the entries ahead that change those files
            files = ", ".join(conflicted) or picked.stderr.strip()
            if not ahead:
                return f"{commit[:12]} conflicts with origin/main, in {files}: rebase onto origin/main and add it again"
            return (f"{commit[:12]} conflicts with what is ahead of it, origin/main and {', '.join(ahead)}, in {files}: "
                    "once those land, rebase onto origin/main and add it again")
        return None

    def blob(self, identifier):
        return subprocess.run(["git", "-C", str(self.checkout), "cat-file", "blob", identifier], capture_output=True,
                              check=True, stdin=subprocess.DEVNULL, env=environment(), timeout=GIT_TIMEOUT).stdout

    def keep_changelogs(self, conflicted, start, before, commit):
        """Settle the cherry-pick of commit in progress when every file in conflict is a changelog whose
        conflicts keep_both settles, and say whether it did; otherwise change nothing. keep_both merges the
        entry's whole change to the file, from start, its first commit's parent, to commit, onto before, what
        is ahead of the entry: so a later commit that rewords the line an earlier one added is that line
        added, not a change to a line ahead of it (#130)."""
        settled = {}
        for path in conflicted:
            if Path(path).name.lower() != CHANGELOG: return False
            stages = {}
            for line in text(self.checkout, "ls-files", "-u", "--", path).splitlines():
                mode, identifier, stage = line.split("\t")[0].split()
                stages[stage] = (mode, identifier)
            # Three stages of one ordinary file: a rename, a deletion or a change of mode is no added line.
            if set(stages) != {"1", "2", "3"} or len({mode for mode, _ in stages.values()}) != 1 \
                    or not stages["1"][0].startswith("100"):
                return False
            sides = [git(self.checkout, "rev-parse", "--verify", "-q", f"{revision}:{path}").stdout.strip()
                     for revision in (start, before, commit)]
            if not all(sides): return False
            base, ours, theirs = map(self.blob, sides)
            settled[path] = keep_both(base, ours, theirs)
            if settled[path] is None: return False
        for path, content in settled.items():
            (self.checkout / path).write_bytes(content)
            git(self.checkout, "add", "--", path, hooks=False, check=True)
        return True

    def integrate(self, batch, base):
        """The entries of batch that are now on top of base, in order; the rest are settled (left_out). eng land's
        trial of a new entry runs this same code in a scratch checkout, settling nothing (Trial)."""
        self.reset(base)
        applied = []
        for entry in batch:
            how = landing.contained(lambda *words: git(self.checkout, *words), base, entry["tip"])
            if how:
                self.left_out(entry, LANDED, landedAs=base, why=f"origin/main already holds it ({how})")
                continue
            why = self.pick(entry, [ahead["id"] for ahead in applied])
            if not why and self.kept: self.noted(entry, kept=self.kept)
            if why:
                self.say(f"{entry['id']}: {why}")
                self.left_out(entry, CONFLICT, why=why)
                continue
            applied.append(entry)
        return applied

    def left_out(self, entry, state, **fields):
        """What becomes of an entry that integrate does not put on top: the queue settles it."""
        self.queue.settle(entry, state, **fields)

    def noted(self, entry, **fields):
        """What integrate records of an entry it put on top, the changelog conflicts it settled: in the queue."""
        self.queue.record(entry, **fields)

    def gate(self, lanes, round_name):
        """(status of the first of lanes that did not pass or 0, its log). ran_on is the second Mac that ran
        that lane, by its dispatch's record (remote.dispatched), or None when it ran here or nowhere. A pass of
        the gate lane leaves its wall time and the parts its log reports in self.measured, for the gate's
        budget (budgets.py)."""
        log, self.ran_on, self.measured, self.timed_out, self.ran_where = None, None, None, None, ""
        for lane in lanes:
            log = self.queue.logs / f"{round_name}-{lane}.log"
            # A lane run here logs to its own file, and one on the second Mac prints here. What an
            # earlier round left there is not this one's.
            lane_log = self.checkout / ".build/engineering" / lane / "run.log"
            with contextlib.suppress(OSError): lane_log.unlink()
            self.say(f"{lane} on {self.head()[:12]}")
            began = time.time()  # wall clock, to match the dispatch records' start times
            started = time.monotonic()  # for the gate's measured duration
            try:
                status_ = processes.run(["python3", "scripts/eng", "run", lane], cwd=self.checkout, log_path=log,
                                        timeout=LANE_LIMIT, env=self.environment)
            except processes.TimedOut as error:
                self.ran_on = remote.dispatched(self.checkout, lane, began)
                self.ran_where = self.mac(lane, began)
                self.timed_out = f"{lane} reached its limit: {error}"
                self.say(f"{lane}: unmeasured: {error}")
                return evidence.UNMEASURED, log
            self.ran_on = remote.dispatched(self.checkout, lane, began)
            with contextlib.suppress(OSError), log.open("a") as kept: kept.write(lane_log.read_text(errors="replace"))
            self.ran_where = self.mac(lane, began)
            self.say(f"{lane} exited {status_}{self.placed(lane, began)}")
            if status_: return status_, log
            if lane == budgets.GATE:
                with contextlib.suppress(OSError):
                    self.measured = {"seconds": round(time.monotonic() - started, 1),
                                     "parts": budgets.parts(log.read_text(errors="replace"))[:budgets.KEPT_PARTS]}
        return 0, log

    def placed(self, lane, began):
        """Where the lane ran, for the log, which eng run chose by both Macs' capacity (vocaro-engineering#168):
        on the second Mac, by its dispatch's record, or on this Mac, with why, by the receipt the run wrote here
        since began (cli.placement); nothing when neither says."""
        if self.ran_on: return f" on {self.ran_on}"
        receipt = self.checkout / ".build/engineering" / lane / "source.json"
        try:
            if receipt.stat().st_mtime < began: return ""
            said = json.loads(receipt.read_text())
        except (OSError, ValueError):
            return ""
        if not isinstance(said, dict) or said.get("ranOn") != "this Mac": return ""
        return f" on this Mac ({said.get('hereBecause')})"

    def mac(self, lane, began):
        """The Mac the lane ran on, for a hold's reason (vocaro-engineering#185): ` on the second Mac (HOST)` by its
        dispatch's record, ` on this Mac (NAME)` by the receipt the run wrote here since began, or nothing when
        neither says."""
        if self.ran_on: return f" on the second Mac ({self.ran_on})"
        receipt = self.checkout / ".build/engineering" / lane / "source.json"
        try:
            if receipt.stat().st_mtime < began: return ""
            said = json.loads(receipt.read_text())
        except (OSError, ValueError):
            return ""
        if not isinstance(said, dict) or said.get("ranOn") != "this Mac": return ""
        return f" on this Mac ({said.get('mac') or 'unnamed'})"

    def changed(self, old, new):
        return text(self.checkout, "diff", "--name-only", old, new).split("\n")

    def carry_passes(self, commands):
        """Whether every command exits 0 in the checkout, each bounded and with a log of its own. One that
        fails, times out or cannot start says nothing of the commits: it only ends the carry-over."""
        for number, command in enumerate(commands):
            log = self.queue.logs / f"{time.strftime('%Y%m%d-%H%M%S')}-{self.head()[:8]}-carry-{number}.log"
            self.say(f"carry-over check: {shlex.join(command)}")
            try: status_ = processes.run(command, cwd=self.checkout, log_path=log, timeout=LANE_LIMIT, env=self.environment)
            except OSError as error: status_ = error
            if status_:
                self.say(f"carry-over check {shlex.join(command)} ended with {status_}: gating again; see {log}")
                return False
        return True

    def carry(self, config, batch, base, head):
        """(new base, new head) with the batch on origin/main, when main moved past base only by files the
        lanes do not read and the batch does not touch, else None: guides/maintenance.md, Push only onto
        the main you tested. The patch must be the one that passed, and the documents and the project's
        carry-over checks must pass on the rebased tree."""
        new_base = text(self.checkout, "rev-parse", MAIN)
        batch_files = set(self.changed(base, head))
        moved = [path for path in self.changed(base, new_base) if path]
        refused = [path for path in moved if not carried_over(path, config, batch_files)]
        if refused:
            self.say(f"main moved by {refused[0]}, which the result does not cover: gating again")
            return None
        patch = text(self.checkout, "diff", base, head)
        self.reset(new_base)
        if any(self.pick(entry) for entry in batch):
            self.say("the batch no longer applies to main: gating again")
            return None
        if text(self.checkout, "diff", new_base, "HEAD") != patch:
            self.say("the batch's patch changed on main: gating again")
            return None
        if not self.carry_passes(carry_checks(config, moved, base)): return None
        self.say(f"carried the result over {len(moved)} file(s) on main: {', '.join(moved[:5])}")
        return new_base, self.head()

    def push(self, config, batch, base, docs=None, ungated=None):
        """Push HEAD to main. True on a push, False to gate again; a refusal that is no race holds, as a remote
        that stays out of reach does.
        The project's pre-push hook runs here rather than inside the push, unless main's queue.hookCoveredBy
        says which passed lanes cover it: a long hook then holds no connection open, and its refusal is its own.
        A docs-only batch, whose lanes docs names, skips it too: the hook is part of lanes that read none of the
        batch's paths. The landed entries record docs, since such a landing is no gate of the pushed commit.
        An ungated landing, whose lanes ungated names, runs the hook unless those lanes cover it, which the gate's then
        do and it skips; its entry records ungated, and its tip carries the UNGATED trailer, again after a carry-over.
        Any other landing records gatedOn, the base its gate ran on, by which `eng queue status` knows that a gate on
        main has passed over an earlier ungated landing (covered)."""
        head = self.head()
        gated_on = base
        unsure = False
        checked = None
        covered = config.get("queue", {}).get("hookCoveredBy")
        for _ in range(CARRIES):
            reach(self.checkout, *FETCH, say=self.say, check=True)
            if git(self.checkout, "merge-base", "--is-ancestor", MAIN, head).returncode:
                carried = self.carry(config, batch, base, head)
                if carried is None: return False
                base, head = carried
                if ungated: head = self.mark_ungated(ungated, base)
            if covered:
                self.say(f"no pre-push hook: main's queue.hookCoveredBy says {', '.join(covered)} cover it"
                         + (", and this landing skips them" if ungated and not set(covered) <= set(ungated["lanes"]) else ""))
            elif checked != head:
                result, log = self.pre_push(head)
                if result in NO_VERDICT and not unsure:
                    unsure = True
                    self.say(f"the pre-push hook gave no verdict (exit {result})")
                    busy = self.settle("the pre-push hook runs")
                    if busy:
                        raise Hold(SESSION, f"the pre-push hook gave no verdict, exit {result}, on {head[:12]}, and "
                                            f"{busy}; see {log}")
                    continue
                if result in NO_VERDICT:
                    raise Hold(SESSION, f"the pre-push hook gave no verdict, exit {result}, twice on {head[:12]}; see {log}")
                if result: raise Hold(SESSION, f"the pre-push hook refused {head[:12]}, exit {result}; see {log}")
                checked = head
            # A connection that dropped pushes again here, without running the hook again.
            pushed = reach(self.checkout, "push", "--no-verify", "origin", "HEAD:refs/heads/main", say=self.say)
            pushed, status = self.push_again(pushed)
            if pushed.returncode == 0:
                git(self.checkout, *FETCH)
                self.say(f"pushed {head[:12]}" + (", landed without the gate, which the next gate on main covers"
                                                  if ungated else ""))
                own = self.landed(batch, base, head)
                for entry in batch:
                    commit, count = own.get(entry["id"], (head, 0))
                    self.queue.settle(entry, LANDED, landedAs=commit, landedTip=head,
                                      **({"landedCommits": count} if count else {}),
                                      **({"docsOnly": docs} if docs else {}),
                                      **({"ungated": ungated} if ungated else {}),
                                      **({"gate": self.measured} if self.measured and not docs else {}),
                                      **({} if docs or ungated else {"gatedOn": gated_on}))
                if not docs and not ungated: self.keep_app(config, head)
                if self.toolkits_own(): self.follow()
                return True
            said = pushed.stderr.strip()
            if "rejected" in said and ("fetch first" in said or "non-fast-forward" in said): continue
            if unreached(said):
                raise Hold(CLEARS, f"the push of {head[:12]} did not reach the remote in {len(BACKOFF) + 1} tries: {said}")
            raise Hold(OWNER, f"the push of {head[:12]} was refused: {said or 'no output'}{status}")
        return False

    def keep_app(self, config, head):
        """Start keeping the app built at head, the gated commit just pushed, in the background, when main's
        engineering.json keeps one after landing (app_builds.py): the round goes on while it builds, and a build
        that fails leaves the build kept before it. A docs-only or ungated landing keeps none."""
        if not app_builds.after_landing(config): return
        log = self.queue.logs / f"app-keep-{head[:12]}.log"
        try:
            app_builds.spawn_keep(self.checkout, head, log, env=self.environment)
        except OSError as error:
            self.say(f"could not start keeping the app built at {head[:12]}: {error}")
            return
        self.say(f"keeping the app built at {head[:12]} in the background; see {log}")

    def landed(self, batch, base, head):
        """{entry id: (its last commit in base..head, how many of its commits that range holds)}, so that each entry
        records its own commit on main rather than the batch's tip (vocaro-engineering#184). A pushed commit is its
        entry's by author, author date and subject, which a cherry-pick, a carry-over and the UNGATED amend all keep,
        matched in order, as the batch was picked in order. An entry none of whose commits the range holds, as when
        main already had each, is left out, and records the tip, which holds it."""
        form = "--format=%H%x00%an%x00%ae%x00%ad%x00%s"
        def keyed(output):
            return [(line.split("\0", 1)[0], line.split("\0", 1)[1]) for line in output.splitlines() if line]
        expected = []
        for entry in batch:
            originals = keyed(text(self.checkout, "show", "-s", "--date=raw", form, *entry["commits"]))
            expected += [(entry["id"], key) for _, key in originals]
        own, position = {}, 0
        for commit, key in keyed(text(self.checkout, "log", "--reverse", "--date=raw", form, f"{base}..{head}")):
            match = next((index for index in range(position, len(expected)) if expected[index][1] == key), None)
            if match is None: continue
            identifier = expected[match][0]
            own[identifier] = (commit, own.get(identifier, (None, 0))[1] + 1)
            position = match + 1
        return own

    def push_again(self, pushed):
        """(the last push, what a hold adds of GitHub's status). A push the remote refused for its credentials, when
        the fetch at the top of push went through from the same URL a moment before, is tried again after each of
        BACKOFF; and then, on GitHub, while its status says Git Operations is not operational, every REFUSAL_POLL
        seconds for up to SETTLE_LIMIT, since a hold costs a gate (vocaro-engineering#151). Any other answer ends
        the tries: a push, a rejection that push handles, a refusal of another kind, or one with Git Operations
        operational, which holds as before."""
        said = pushed.stderr.strip()
        if pushed.returncode == 0 or not AUTHENTICATION.search(said): return pushed, ""
        url = text(self.checkout, "remote", "get-url", "--push", "origin")
        if url != text(self.checkout, "remote", "get-url", "origin"): return pushed, ""
        backoff, started, status = list(BACKOFF), now(), None
        while True:
            if backoff: delay = backoff.pop(0)
            else:
                if not on_github(url): return pushed, ""
                status, why = git_operations()
                if status is None: return pushed, f"; {why}"
                if status == OPERATIONAL: return pushed, f"; GitHub's {GIT_OPERATIONS} is {status}"
                waited = now() - started
                if waited >= SETTLE_LIMIT:
                    return pushed, f"; GitHub's {GIT_OPERATIONS} was still {status} after {int(waited)}s of tries"
                delay = min(REFUSAL_POLL, SETTLE_LIMIT - waited)
            self.say(f"the push was refused as the fetch before it was not, so trying again in {delay:g}s"
                     + (f" while GitHub's {GIT_OPERATIONS} is {status}" if status else "") + f": {said.splitlines()[-1]}")
            pause(delay)
            pushed = reach(self.checkout, "push", "--no-verify", "origin", "HEAD:refs/heads/main", say=self.say)
            said = pushed.stderr.strip()
            if pushed.returncode == 0 or not AUTHENTICATION.search(said): return pushed, ""

    def pre_push(self, head):
        """(status, log) of the project's pre-push hook for pushing head to main, called as Git would
        call it, before any connection to the remote. Status 0 when the project has none."""
        log = self.queue.logs / f"{time.strftime('%Y%m%d-%H%M%S')}-{head[:8]}-pre-push.log"
        told = self.queue.directory / "pre-push.stdin"
        told.write_text(f"HEAD {head} refs/heads/main {text(self.checkout, 'rev-parse', MAIN)}\n")
        url = text(self.checkout, "remote", "get-url", "--push", "origin")
        self.say(f"pre-push hook on {head[:12]}")
        try:
            result = processes.run(["git", "hook", "run", "--ignore-missing", f"--to-stdin={told}", "pre-push", "--",
                                    "origin", url], cwd=self.checkout, log_path=log, timeout=LANE_LIMIT, env=self.environment)
        except processes.TimedOut as error:
            self.say(f"pre-push hook: unmeasured: {error}")
            return evidence.UNMEASURED, log
        self.say(f"pre-push hook exited {result}")
        return result, log

    def failed(self, applied, log):
        """Bisect: the oldest half next. A lone entry that fails is the one to send back."""
        if len(applied) == 1:
            why = "the queue's lanes fail on this commit on top of origin/main: fix it and add it again"
            self.say(f"{applied[0]['id']}: {why}")
            self.queue.settle(applied[0], FAILED, why=why, log=str(log))
            # An entry on its own was no suspect of a bisect, which goes on where it was.
            if not self.alone: self.limit, self.suspects = None, 0
            return
        self.queue.mark(applied, QUEUED)
        self.suspects = len(applied)
        self.limit = max(1, len(applied) // 2)
        self.say(f"{len(applied)} commits fail together: next the oldest {self.limit}")

    def passed(self, count):
        if self.alone: return
        self.suspects = max(0, self.suspects - count)
        self.limit = self.suspects or None

    def choose(self, waiting):
        """This round's batch of waiting, oldest first. A batch that passed comes first, to push. Then the oldest
        entry eng land --front put first, alone; then the oldest entries, as many as a bisect allows, up to the first
        one eng land --no-gate lands alone, or that entry alone when it is the oldest. alone says whether the batch is
        one entry an option put on its own, whose outcome leaves a bisect where it was."""
        batch = waiting[:self.limit] if self.limit else waiting
        self.alone = False
        if any(entry["state"] == PASSED for entry in batch): return batch
        first = next((entry for entry in waiting if entry.get("front")), None)
        if first is None and batch and batch[0].get("noGate"): first = batch[0]
        if first is not None:
            self.alone = True
            return [first]
        return batch[:next((index for index, entry in enumerate(batch) if entry.get("noGate")), len(batch))]

    def round(self):
        queue = self.queue
        batch = self.choose(queue.waiting())
        if not batch: return False
        passed = [entry for entry in batch if entry["state"] == PASSED]
        reach(queue.main_checkout, *FETCH, say=self.say, check=True)
        self.prepare()
        self.toolkit()
        if passed: return self.push_passed(passed)
        base = text(queue.main_checkout, "rev-parse", MAIN)
        applied = self.integrate(batch, base)
        if not applied:
            if not self.alone: self.limit, self.suspects = None, 0
            return True
        head = self.head()
        config = self.settings(base, head)
        queue.mark(applied, TESTING)
        self.say(f"testing {len(applied)} entr{'y' if len(applied) == 1 else 'ies'} at {head[:12]} on {base[:12]}"
                 + (" at the front of the queue, as eng land --front asked" if applied[0].get("front") else ""))
        ungated = self.ungated(applied, config)
        # Without renames, so that a file moved into a docs path also names the path it left.
        docs = None if ungated else docs_lanes(config, [path for path in text(
            self.checkout, "diff", "--name-only", "--no-renames", base, head).splitlines() if path])
        if docs: self.say(f"every path the batch changes is in main's queue.docsOnly: running only {', '.join(docs)}, "
                          "which is no gate of the pushed commit")
        round_name = f"{time.strftime('%Y%m%d-%H%M%S')}-{head[:8]}"
        result, log = self.gate(ungated["lanes"] if ungated else docs or lanes_of(config), round_name)
        self.prune()
        if result in NO_VERDICT:
            queue.mark(applied, QUEUED)
            self.no_verdicts += 1
            runs = no_verdict_runs(config)
            gave = (f"the lanes gave no verdict in {self.no_verdicts} run{'' if self.no_verdicts == 1 else 's'} in a "
                    f"row, the last with exit {result}{self.ran_where}, {self.no_verdict_cause(result, log)}")
            # A shared host's configuration is the owner's; a host that stays busy, or tests left unmeasured, a
            # session reads, and resumes once the host has settled or the cause is gone.
            kind = OWNER if result == remote.MISCONFIGURED else SESSION
            if self.no_verdicts >= runs:
                queue.hold(worded(kind, f"{gave}, the most queue.noVerdictRuns allows ({runs}); see {log}"))
                return True
            self.say(f"no verdict (exit {result})" + (f" from {self.ran_on}" if self.ran_on else "")
                     + f", run {self.no_verdicts} of at most {runs}")
            # Every retry waits, so that none starts into the load that voided the run before it.
            busy = self.settle("the lanes run", self.ran_on)
            if busy: queue.hold(worded(kind, f"{gave}, and {busy}; see {log}"))
            return True
        self.no_verdicts = 0
        if result:
            self.failed(applied, log)
            return True
        # An entry removed during the gate was part of what passed, so what is left has not been tested.
        current = [queue.read(entry["id"]) for entry in applied]
        if any(entry is None or entry["state"] not in WAITING for entry in current):
            self.say("an entry was removed during the run: gating what is left")
            queue.mark(applied, QUEUED)
            return True
        if ungated: head = self.mark_ungated(ungated, base)
        self.keep(applied, base, head, docs, ungated)
        if self.push(config, applied, base, docs, ungated): self.passed(len(applied))
        else: queue.mark(applied, QUEUED)
        return True

    def ungated(self, applied, config):
        """What an ungated landing of applied records, the reason --why gave and the lanes it runs, when it is one
        entry eng land --no-gate put on its own; else None."""
        if not (self.alone and applied[0].get("noGate")): return None
        ungated = {"why": applied[0].get("aheadWhy") or "no reason was recorded", "lanes": ungated_lanes(config)}
        self.say(f"{applied[0]['id']} lands without the gate, as eng land --no-gate asked, because {ungated['why']}: "
                 f"running only {', '.join(ungated['lanes']) or 'no lane'}; the next gate on main covers it")
        return ungated

    def mark_ungated(self, ungated, base):
        """Add the UNGATED trailer to HEAD, an ungated landing's tip, and return the new HEAD; a HEAD at base, whose
        commits main already has, stays. The commit keeps its author, message and patch, by which Closeout finds its
        branch landed."""
        if self.head() == base: return base
        words = (f"eng land --no-gate ran {', '.join(ungated['lanes']) or 'no lane'} and the pre-push checks, not the "
                 f"gate, because {ungated['why']}; the next gate on main covers it")
        git(self.checkout, "commit", "--amend", "--no-verify", "--no-edit", "--allow-empty", "--trailer",
            f"{UNGATED}: {words}", hooks=False, check=True)
        return self.head()

    def no_verdict_cause(self, result, log):
        """Why the gate that exited result gave no verdict, for a hold's reason: the lane's limit it reached, the
        tests its log reports unmeasured, no admission, or a shared host's configuration."""
        if self.timed_out: return f"unmeasured: {self.timed_out}"
        if result == evidence.UNMEASURED:
            try: said = log.read_text(errors="replace") if log else ""
            except OSError: said = ""
            found = evidence.unmeasured_tests(said)
            if not found: return "unmeasured, with no test its log names"
            first = found[0] if len(found[0]) <= UNMEASURED_SHOWN else found[0][:UNMEASURED_SHOWN - 3] + "..."
            # The run's last reading of its screen, which eng writes for a lane whose result depends on it.
            screens = SCREEN_LINE.findall(said)
            return (f"unmeasured: {len(found)} test{'' if len(found) == 1 else 's'}, the first {first}"
                    + (f"; the last reading {screens[-1].strip()}" if screens else ""))
        if result == resources.BUSY: return "no admission: the host stayed at capacity"
        return "a shared host's configuration stopped it"

    def settings(self, base, head):
        """The project's lanes and settings as they stand on main at base, so that no branch chooses its own gate,
        with the checkout left at head. Until main names any lanes, the batch's: the commit that first names a
        project's lanes has to land."""
        self.reset(base)
        config = self.judged(f"main at {base[:12]}")
        self.reset(head)
        try: lanes_of(config)
        except ValueError:
            config = self.judged(f"the batch at {head[:12]}")
            # The batch may name its lanes, but only main may say the hook need not run, or which paths are docs.
            config.get("queue", {}).pop("hookCoveredBy", None)
            config.get("queue", {}).pop("docsOnly", None)
        return config

    def judged(self, what):
        """The checkout's configuration as this worker's code judges it: the toolkit at origin/main where a copy
        exists (toolkit, move_to), which the hold names. What it refuses, a session fixes, on main or in the
        toolkit, and nobody has to ask the owner."""
        try: return load(self.checkout)
        except ValueError as error:
            raise Hold(SESSION, f"{error}, judging {what} with the toolkit at "
                                f"{self.code[:12] if self.code else 'no commit'}") from error

    def keep(self, batch, base, head, docs, ungated=None):
        """Record a batch's pass before its push. A hold the push meets, a refusal or a remote out of reach, then
        leaves the batch passed rather than queued, and `eng queue resume` pushes head instead of gating the same
        tree again (vocaro-engineering#150): EPUBLib's queue spent an hour of the second Mac doing that."""
        verdict = {"head": head, "base": base, "batch": [entry["id"] for entry in batch], "at": time.time()}
        if docs: verdict["docsOnly"] = docs
        if ungated: verdict["ungated"] = ungated
        if self.alone: verdict["alone"] = True
        if self.measured: verdict["gate"] = self.measured
        for entry in batch: self.queue.record(entry, state=PASSED, passed=verdict)

    def push_passed(self, passed):
        """Push the batch whose pass keep recorded, when it is still the batch that passed: the same entries, none
        removed or added again since, and its commit still in the repository. Main having moved since is push's to
        decide, by carry, as for any batch that passed. Otherwise its entries queue again and are gated as any
        others."""
        verdict = passed[0]["passed"]
        whole = sorted(entry["id"] for entry in passed) == sorted(verdict["batch"]) \
            and all(entry.get("passed") == verdict for entry in passed) \
            and not git(self.checkout, "cat-file", "-e", f"{verdict['head']}^{{commit}}").returncode
        if not whole:
            self.say(f"what passed at {verdict['head'][:12]} is no longer the batch waiting: gating its entries again")
            self.queue.mark(passed, QUEUED)
            return True
        batch = sorted(passed, key=lambda entry: verdict["batch"].index(entry["id"]))
        base, head, docs, ungated = verdict["base"], verdict["head"], verdict.get("docsOnly"), verdict.get("ungated")
        config = self.settings(base, head)
        self.measured = verdict.get("gate")
        self.alone = bool(verdict.get("alone"))
        self.say(f"{len(batch)} entr{'y' if len(batch) == 1 else 'ies'} passed at {head[:12]} on {base[:12]}: "
                 "pushing without gating again")
        if self.push(config, batch, base, docs, ungated): self.passed(len(batch))
        else: self.queue.mark(batch, QUEUED)
        return True

    def prune(self):
        logs = sorted(self.queue.logs.iterdir(), key=lambda path: path.stat().st_mtime)
        for path in logs[:-KEPT_LOGS]:
            with contextlib.suppress(OSError): path.unlink()

    def drain(self):
        while not self.queue.held():
            more = self.round()
            self.arrived()
            if not more: return
            self.rounds += 1


class Trial(Worker):
    """eng land's trial integration of a new entry (trial): the worker's own integrate, pick and changelog
    resolution, so that the trial and the round cannot disagree on what conflicts, in a scratch checkout, settling,
    recording and saying nothing."""
    def __init__(self, queue, checkout):
        super().__init__(queue)
        self.checkout = checkout
        self.outcomes = {}  # (state, why) of each entry integrate left out, by its ID
        self.notes = {}  # what integrate noted of each entry it put on top, by its ID

    def say(self, words):
        pass

    def left_out(self, entry, state, **fields):
        self.outcomes[entry["id"]] = (state, fields.get("why"))

    def noted(self, entry, **fields):
        self.notes[entry["id"]] = fields


def round_order(entries):
    """Waiting entries in the order rounds put them on main (Worker.choose): a batch that passed, then a batch in
    its gate, then those eng land --front put first, then the rest, each oldest first."""
    rank = {PASSED: 0, TESTING: 1, QUEUED: 2}
    return sorted(entries, key=lambda entry: (rank.get(entry["state"], 2), not entry.get("front"),
                                              entry["submitted"], entry["id"]))


def trial(queue, entry, ahead, base):
    """What a round would make of entry now: the worker's integrate (Trial) of ahead, the waiting entries ahead of it
    in round_order, and then entry, onto base, origin/main, in a scratch worktree of the repository outside every
    checkout, which goes when it ends (vocaro-engineering#178). A dict: why, why entry does not apply, or None when
    it applies or main holds it already; against, the entries ahead that went on top; left, those ahead that a round
    would itself leave out, with why; touching, those of against that change a file in conflict; kept, the
    changelog conflicts of entry that a round would settle."""
    folder = Path(tempfile.mkdtemp(prefix="eng-land-trial-"))
    path = folder / queue.main_checkout.name
    try:
        git(queue.root, "worktree", "add", "--detach", "--lock", "--reason", TRIAL_REASON, str(path), base,
            hooks=False, check=True)
        worker = Trial(queue, path)
        applied = worker.integrate([*ahead, entry], base)
        state, why = worker.outcomes.get(entry["id"], (None, None))
        why = why if state == CONFLICT else None
        against = [other for other in applied if other["id"] != entry["id"]]
        touching = []
        if why:
            conflicted = set(getattr(worker, "conflicted", ()))
            for other in against:
                changed = text(path, "diff", "--name-only", "--no-renames", f"{other['commits'][0]}^", other["tip"])
                if conflicted & set(changed.splitlines()): touching.append(other["id"])
        return {"why": why, "against": [other["id"] for other in against], "touching": touching,
                "left": {other["id"]: worker.outcomes[other["id"]][1] for other in ahead
                         if other["id"] in worker.outcomes},
                "kept": worker.notes.get(entry["id"], {}).get("kept", [])}
    finally:
        # Unlocked first, so that a worktree the removal leaves registered is one Git's own pruning takes.
        git(queue.root, "worktree", "unlock", str(path), hooks=False)
        git(queue.root, "worktree", "remove", "--force", "--force", str(path), hooks=False)
        shutil.rmtree(folder, ignore_errors=True)


def tried(found, base, front, elapsed):
    """What eng land says of a trial that let its entry through, line by line."""
    after = f" after {', '.join(found['against'])}" if found["against"] else ""
    lines = [f"checked in {elapsed:.1f}s: it applies onto origin/main at {base[:12]}{after}"
             + (", alone, as --front tests it" if front else "") + ", as a round would integrate it now"]
    lines += [f"a round would settle {kept}" for kept in found["kept"]]
    lines += [f"left out of the check, as a round would leave it: {identifier}, {why}"
              for identifier, why in found["left"].items()]
    return lines


def refusal(tip, found, base):
    """Why eng land queues nothing: the trial's conflict, with what it was checked against."""
    checked_ = f"origin/main at {base[:12]}" + (f" and, in order, {', '.join(found['against'])}" if found["against"]
                                                else "")
    words = f"{tip[:12]} is not queued: {found['why']}. Checked against {checked_}"
    if found["touching"]:
        many = len(found["touching"]) > 1
        words += f"; {', '.join(found['touching'])} change{'' if many else 's'} the files in conflict"
    if found["left"]:
        words += f"; left out, as a round would leave {'them' if len(found['left']) > 1 else 'it'}: " + \
            ", ".join(found["left"])
    return words + (". An entry ahead that comes back may let it apply: `eng queue status` says, and `eng land` again "
                    "checks again (guides/maintenance.md, The merge queue)")


def run(root):
    """Be the repository's worker until nothing waits. A second worker exits at once. Anything
    unexpected holds the queue, with every entry still waiting, rather than guessing at a verdict; the hold's
    reason says whom it is for (worded). A cause that clears by itself, a remote out of reach past its backoff,
    runs the round again after each of CLEARING_WAITS first, this process staying the worker through the wait,
    and holds for a session only when the last try fails too."""
    queue = Queue(root)
    progress, tries = carried(), 0
    while True:
        with queue.worker() as free:
            if not free: return 0
            # Whatever a worker that died left testing was never pushed. What it left passed is pushed first,
            # and a push that had gone through lands it then, as a push of what main already has.
            queue.mark([entry for entry in queue.all() if entry["state"] == TESTING], QUEUED)
            worker = Worker(queue, progress)
            progress = None
            try:
                with processes.forwarding_signals():
                    worker.drain()
                    worker.catch_up()
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                queue.mark([entry for entry in queue.all() if entry["state"] == TESTING], QUEUED)
                if worker.rounds: tries = 0  # a round that went through since the last try starts the count again
                kind = error.kind if isinstance(error, Hold) else SESSION
                if kind == CLEARS and tries < len(CLEARING_WAITS):
                    delay = CLEARING_WAITS[tries]
                    tries += 1
                    worker.say(f"{error}: a cause that clears by itself, so the round runs again in {delay}s, "
                               f"try {tries} of {len(CLEARING_WAITS)}")
                    progress = worker.progress()
                    pause(delay)
                    continue
                cause = f"{error}{passed_push(queue)}"
                if kind == CLEARS:
                    kind = SESSION
                    cause = (f"{error}, after {tries} more tr{'y' if tries == 1 else 'ies'} of the round over "
                             f"{sum(CLEARING_WAITS[:tries])}s did not clear it{passed_push(queue)}")
                queue.hold(worded(kind, cause))
                return 1
        # An entry added after the last look, while its adder found this worker running, is this
        # worker's to take: its adder's own worker has already exited.
        if queue.held() or not queue.waiting(): return 0


def passed_push(queue):
    """What a hold keeps of a batch that passed, for its reason: the push it waits for."""
    passed = [entry for entry in queue.all() if entry["state"] == PASSED]
    if not passed: return ""
    verdict = passed[0]["passed"]
    return (f"; passed {verdict['head'][:12]} on {verdict['base'][:12]} ({len(passed)} "
            f"entr{'y' if len(passed) == 1 else 'ies'}), which the resume pushes without gating again")


def resume(root):
    queue = Queue(root)
    with contextlib.suppress(OSError): queue.hold_file.unlink()
    queue.start()
    print(f"{len(queue.waiting())} waiting; worker {'running' if queue.working() else 'not needed'}")
    return 0


def land_arguments(parser):
    parser.add_argument("reference", nargs="?", help="the commit or branch; HEAD by default, and required with --repo")
    parser.add_argument("--repo", help="another project to land in: its path, or its name beside this project")
    parser.add_argument("--wait", action="store_true", help="follow it to its result and print the landed commit: "
                        "0 landed, 1 came back or was refused, as it conflicts with what is ahead of it, 75 held or "
                        "still waiting at the limit")
    parser.add_argument("--limit", type=float, default=WAIT_LIMIT, metavar="SECONDS")
    parser.add_argument("--note", help="what the entry is, for `eng queue status`")
    options(parser)


def options(parser):
    """eng land's emergency options, in every repository, which `eng queue add` takes too (guides/maintenance.md,
    Landing ahead of the queue)."""
    parser.add_argument("--front", action="store_true", help="for an emergency, such as an important fix stuck behind "
                        "queued changes: test it next, ahead of everything waiting, as a batch of its own, once a gate "
                        "already running finishes. Again with a commit already queued, it moves that entry")
    parser.add_argument("--no-gate", action="store_true", help="for an emergency, such as an important fix held up by "
                        "an unrelated flaky test: land it alone on the spelling lane, where the project has one, and "
                        "the pre-push hook, without the gate, and record it as ungated, which the next gate on main "
                        "covers. Needs --why; tell the owner you used it and why. With --front it lands at once")
    parser.add_argument("--why", metavar="TEXT", help="the reason for --front or --no-gate, which --no-gate requires: "
                        "the entry, the log, the landed commit's trailer and `eng queue status` record it")


def add_arguments(parser):
    operations = parser.add_subparsers(dest="operation", required=True)
    adding = operations.add_parser("add", help="queue the commits a ref has and origin/main lacks, and return at once")
    adding.add_argument("reference", nargs="?", default="HEAD")
    adding.add_argument("--note", help="what the entry is, for `eng queue status`")
    options(adding)
    state = operations.add_parser("status", help="say what is waiting, what is running, what came back and, when the "
                                  "queue is held, why and for whom")
    state.add_argument("--json", action="store_true")
    following = operations.add_parser("wait", help="follow an entry to its result: 0 landed, 1 came back, 75 "
                                      "held or still waiting at the limit")
    following.add_argument("id")
    following.add_argument("--limit", type=float, default=WAIT_LIMIT, metavar="SECONDS")
    removing = operations.add_parser("remove", help="take an entry out; a run in progress finishes, and lands nothing")
    removing.add_argument("id")
    operations.add_parser("resume", help="clear a hold and start the worker: any session does this itself once the "
                          "hold's cause is dealt with or gone, and asks the owner only for a hold that says it is his")
    operations.add_parser("run", help="be the worker in the foreground; `add` starts one by itself")


def main(root, args):
    if args.operation == "add":
        try: add(root, args.reference, args.note, front=args.front, no_gate=args.no_gate, why=args.why)
        except Refused as error:
            print(f"eng: {error}", file=sys.stderr)
            return 1
        return 0
    if args.operation == "status": return status(root, args.json)
    if args.operation == "wait": return wait(root, args.id, args.limit)
    if args.operation == "remove": return remove(Queue(root), args.id)
    if args.operation == "resume": return resume(root)
    return run(root)
