"""A local merge queue for a direct-main repository: `eng queue add`, and a session moves on.

One worker per repository takes everything waiting, cherry-picks it onto origin/main in a checkout
of its own, runs the project's queue lanes there once, and pushes on a pass. When the lanes fail on
a batch it halves the batch until it finds the commit to send back, landing the rest, with each bisect's two halves
gated at once, the newer on the second Mac when it can start them (Beside), and the entries that arrive during a
gate gated stacked on its tip on the other Mac, landing once it has (Stack, vocaro-engineering#241); first, when the lane is
eng unittest, it reruns the failed tests alone, and a batch whose failed tests all pass alone and that changes none
of the files they import lands, with the flakes recorded (vocaro-engineering#231), and a batch whose real failures
all point at one entry, by a path their output names or a file they import, sends that entry back and gates the
rest at once instead of bisecting (Worker.blame). Only a lane's
verdict blames a commit: a run the host left unmeasured runs again, each time once the host has settled,
until queue.noVerdictRuns runs in a row (3 by default) have given none, and then holds the queue for the
owner (a run a locked screen voided is not counted, and its lane runs next only on a Mac whose screen reads
unlocked, vocaro-engineering#293), with every commit still waiting (guides/maintenance.md, The merge queue). A batch whose lanes
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
import ast
import collections
import contextlib
import fcntl
import fnmatch
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

from . import app_builds, budgets, disk, evidence, landing, offload_host, parallel, processes, remote, resources
from .config import TOOLKIT_ROOT, load, parse, stays

MAIN = "refs/remotes/origin/main"
FETCH = ["fetch", "--no-tags", "origin", f"refs/heads/main:{MAIN}"]
QUEUED, TESTING, PASSED, LANDED, FAILED, CONFLICT, REMOVED = \
    "queued", "testing", "passed", "landed", "failed", "conflict", "removed"
# Passed is a batch whose lanes passed and whose push has not landed it: Worker.keep and Worker.push_passed.
WAITING = {QUEUED, TESTING, PASSED}
# What Worker.older returns for a batch whose lanes gave no verdict, which is no entry's state.
NO_VERDICT_OUTCOME = "no verdict"
# What a lane exits with when it says nothing about the commits: unmeasured, no admission, a
# shared host's configuration, which stops for the owner, and a worker that lost Local Network access (Worker.older).
NO_VERDICT = {evidence.UNMEASURED, resources.BUSY, remote.MISCONFIGURED, remote.LOST_ACCESS}
# Of those, the exits of a lane that did not run: no Mac admitted it, a shared host's configuration stopped it, or this
# worker can no longer reach the second Mac.
UNRUN = {resources.BUSY, remote.MISCONFIGURED, remote.LOST_ACCESS}
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
SPELLING = "spelling"
# The toolkit's spelling check, which the queue runs as the spelling lane of a project that has none, before the gate,
# on an ungated landing and before a docs-only batch's lanes, unless main's queue.spelling is false: a British spelling
# then fails in a minute, not after a full gate and a bisect (vocaro-engineering, 2026-11-07). It runs in the queue's
# checkout, on this Mac, as a carry-over check does: a project's own spelling lane, which eng run places, comes first.
IMPLICIT_SPELLING = ["python3", "scripts/eng", SPELLING]
# Seconds eng land gives the spelling lane on a new entry's commit before it queues the entry unchecked, and how many
# of a failed run's last lines its refusal quotes (spelled_before_queued, vocaro-engineering#283).
SPELLING_LIMIT = 300
SPELLING_SHOWN = 40
# eng land's emergency options, in every repository, by the entry field each sets: an entry tested next as a batch
# of its own, and one landed alone on the spelling lane without the gate, whose written reason --why gives (owner's
# decision, 2026-11-04, vocaro-engineering#163). Neither is routine.
OPTIONS = {"front": "--front", "noGate": "--no-gate"}
# The lanes an ungated landing runs where the project has them: the short check a hand push ran.
UNGATED_LANES = ["spelling"]
# The trailer an ungated landing adds to its tip, so that main's history says so (Worker.mark_ungated).
UNGATED = "Landed-without-gate"
# While a bisect goes on, the newer half of its suspects gates beside the older half, at once, in a second checkout:
# the older half on this Mac, the newer on the second Mac when it can start them now, which never runs them here
# (owner's decision of 2026-11-07, vocaro-engineering#241). A second Mac that declines leaves the newer half waiting,
# as before. The second checkout sits beside the worker's, with this suffix (Beside).
HERE = ("--here",)
BESIDE = ("--offload-only",)
BESIDE_SUFFIX = "-beside"
# While a batch gates, the entries that arrive behind it gate stacked on its tip, on the other Mac, in the checkout
# its gate does not use; the batch's push makes that tip origin/main, so the stacked gate's pass lands, and its
# failure or no landing throws the stacked gate away (owner's decision of 2026-11-07, vocaro-engineering#241). The
# worker looks for them this often (Worker.watched).
STACK_POLL = 30
# What a gate's lanes return when `eng queue remove` took an entry it tests out of the queue, which stopped them by the
# cancel a thrown-away stacked gate's lanes end with, here or on the second Mac, freeing their admission there: no
# verdict on anything, and what is left of the batch gates again (Worker.gate, vocaro-engineering#247). The gate looks
# for such a removal this often, and records it on the entries it tests, for `eng queue remove` to say (stopping).
WITHDRAWN = "withdrawn"
REMOVAL_POLL = 5
# A failed eng unittest gate's failed tests are each rerun alone on the batch's commit this many times before a
# bisect, and any rerun's failure makes the failure real; a gate that failed more tests than RERUN_MOST bisects at
# once, as that is no flake (Worker.rerun, owner's decision of 2026-11-07, vocaro-engineering#231).
RERUNS = 3
RERUN_MOST = 5
# The trailer a landing whose gate failed only flaky tests adds to its tip, one per test (Worker.mark_flaky).
FLAKY = "Flaky-test"
# What eng unittest prints last (parallel.unittest_main), each failed process in its verdict as `NAME (RESULT)`,
# each process's output under `== NAME: RESULT (N tests, S s)`, and unittest's line for each failed test.
UNITTEST_SUMMARY = re.compile(r"^eng unittest: \d+ tests \(\d+ skipped\) in \d+ processes, \d+ at a time, "
                              r"[\d.]+s: (.*)$", re.M)
# What eng unittest prints when it refuses a suite before running anything (parallel.refusals).
REFUSAL = re.compile(r"^eng unittest: .*; nothing ran$", re.M)
# How many of the newest gate logs `eng queue doctor` reads back to the last that gave a verdict, and how many of
# them in a row with no verdict make a finding (doctor).
DOCTOR_LOGS = 12
DOCTOR_NO_VERDICTS = 2
FAILED_SHARD = re.compile(r"(\S+) \(((?:[^()]|\([^()]*\))*)\)")
SHARD_HEADER = re.compile(r"^== (\S+): .* \(\d+ tests, [\d.]+s\)$", re.M)
FAILED_TEST = re.compile(r"^(?:FAIL|ERROR): (\S+) \(([^()\s]+)\)", re.M)
# The options of eng unittest a rerun keeps, and those it drops, each with its value: it names its own test and
# runs one process at a time.
RERUN_KEPT = {"-s", "--start-directory", "-p", "--pattern", "-t", "--top-level-directory", "--split", "--timeout"}
RERUN_DROPPED = {"-j", "--jobs", "-k"}
GH_TIMEOUT = 60
# Seconds between readings of the CPU busy share of the Mac a gate lane runs on, which the gate's record keeps
# beside its wall time, so that the budget's median can leave out runs made on a loaded host (vocaro-engineering#235).
BUSY_SAMPLE = 60
GH_ENV = {"GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1"}
# The worker's account of the round it runs, in eng-queue, which `eng queue status` shows so that nobody has to read
# worker.log to learn why entries wait: the bisect's halves, each gate's lane, Mac and start, and what each waits for
# (Queue.note, GateWatch). Each running gate's log is read for that this often.
ROUND_FILE = "round.json"
WATCH_POLL = 10
# The most of a lane's results folders that the line naming them lists (result_files): a gate's suites write a few.
RESULT_FOLDERS_NAMED = 8
# What eng run prints while a lane waits (resources.Admission, remote.Offer, remote.overflow), once this Mac admits
# it, and as it sends the lane to the second Mac (remote.dispatch): GateWatch reads the newest of them in the gate's
# log. A dispatch's own folder is named only once its run ends, so until then the log alone says where it runs.
ADMISSION_WAIT = re.compile(r"^heavy: waiting \(\d+s\): (.+)$")
OFFER_WAIT = re.compile(r"^eng: \S+ (waits for .+)$")
DECLINED = re.compile(r"^eng: trying \S+ on this Mac: (.+)$")
ADMITTED = re.compile(r"^heavy: slot ")
SENT = re.compile(r"^remote: sending .* to (\S+)$")
# A gate whose wait names a dead or uncertain guard entry that admission has not recovered (resources.Admission's
# stranded refusal) is stuck, not waiting, once it has waited this long: on 2026-11-07 BreezEPUB's gate waited 86 min
# behind three dead entries while status said only "waiting" (#315).
STUCK_SECONDS = 15 * 60
DEAD_ENTRY = re.compile(r"is (?:dead|uncertain) and requires inspection")
WAIT_LIMIT = 6 * 3600
KEPT_LOGS = 200
GIT_TIMEOUT = 600
# The folder, beside the main checkouts and outside every repository, that holds each queue's checkouts as
# the second Mac holds a run's (offload_host, checkouts/NAME/N/...): QUEUES/NAME/NAME and, beside it, the
# toolkit's copy, so that a project's ../vocaro-engineering finds that copy (owner's decision,
# vocaro-engineering#104). The folders beside the main checkout it replaces, NAME-queue and NAME-queue-toolkit,
# move there once nothing uses them.
QUEUES = "vocaro-queue"
# What locks eng land's scratch worktrees while its trial integration (trial) or its replay of a branch's own
# commits (linear) lasts, so that no sweep takes them.
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


def shortened(said):
    """said as a hold's reason quotes it: at most UNMEASURED_SHOWN characters."""
    return said if len(said) <= UNMEASURED_SHOWN else said[:UNMEASURED_SHOWN - 3] + "..."


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


class NoLocalNetwork(Exception):
    """The worker's process has lost Local Network access (remote.local_network_lost): it exits, holding nothing, so
    that a live session starts the next worker (run)."""


class Refused(ValueError):
    """An entry eng land queued nothing of, since the round's integration of it does not apply (check). eng land and
    `eng queue add` exit 1 for it, as `eng land --wait` does for an entry that came back. waits_for names the entries
    ahead that it conflicts with alone, not with origin/main, which `eng land --wait` waits for before it checks again
    (vocaro-engineering#302); it is empty for any other refusal."""
    def __init__(self, words, waits_for=()):
        super().__init__(words)
        self.waits_for = list(waits_for)


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


def implicit_spelling(config):
    """Whether the queue runs the toolkit's spelling check, IMPLICIT_SPELLING, as the project's spelling lane: when
    the project has no spelling lane of its own and main's queue.spelling does not opt out with false."""
    return SPELLING not in config.get("lanes", {}) and config.get("queue", {}).get("spelling", True) is not False


def spelled(config, name):
    """Whether name is a lane the queue can run for this project: one it configures, or the implicit spelling lane."""
    return name in config.get("lanes", {}) or (name == SPELLING and implicit_spelling(config))


def lanes_of(config):
    """The lanes a batch must pass, in order: the project's spelling lane, or the toolkit's when it has
    none (implicit_spelling), and its provision lane when it has one, the cheap checks and setup that a
    gate assumes, and then `queue.lanes` or its gate lane."""
    lanes = config.get("lanes", {})
    named = config.get("queue", {}).get("lanes") or (["gate"] if "gate" in lanes else None)
    if not named:
        raise ValueError(f"{config.get('name')} has no gate lane, so the queue has nothing to run: name the lane "
                         "holding what a commit must pass before it lands gate (guides/maintenance.md, "
                         "The merge queue)")
    return [name for name in BEFORE_GATE if spelled(config, name) and name not in named] + named


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
    """The lanes an ungated landing runs instead of the queue's: UNGATED_LANES that the project has, its spelling lane
    the toolkit's when it has none (implicit_spelling)."""
    return [name for name in UNGATED_LANES if spelled(config, name)]


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
    """The lanes main's queue.docsOnly names when every path a batch changes is one it lists, after the spelling
    lane, the project's or the toolkit's (implicit_spelling), where they do not name it, since documents are where
    words change; else None: a batch that changes nothing, or anything else, runs every lane."""
    declared = config.get("queue", {}).get("docsOnly")
    if not declared or not changed: return None
    if not all(any(named(path, key) for key in declared["paths"]) for path in changed): return None
    first = [SPELLING] if spelled(config, SPELLING) and SPELLING not in declared["lanes"] else []
    return first + declared["lanes"]


def offerable(config, lanes):
    """Why one of lanes runs only on this Mac, as its configuration says (config.stays), so that a bisect's newer half
    cannot gate on the second Mac beside the older half (Beside); None when every one can go there."""
    for lane in lanes:
        # The toolkit's spelling check runs in whichever checkout gates, on this Mac, and holds no half back.
        if lane == SPELLING and implicit_spelling(config): continue
        why = stays(config.get("lanes", {}), lane) if lane in config.get("lanes", {}) else "it is no configured lane"
        if why: return f"{lane} runs only on this Mac: {why}"
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


def rerun_command(command):
    """The command, without its test, that reruns one test of a lane whose command is eng unittest
    (vocaro-engineering#231): the lane's command through `unittest`, its -s, -p, -t, --split and --timeout, one
    process at a time and verbose. None for a lane that is not eng unittest, as a project's own lane, a group or
    one with an option the rerun cannot carry, which then bisects as before."""
    if not isinstance(command, list) or not all(isinstance(word, str) for word in command): return None
    at = next((index for index, word in enumerate(command[:-1])
               if Path(word).name == "eng" and command[index + 1] == "unittest"), None)
    if at is None: return None
    kept, words, index = command[:at + 2], command[at + 2:], 0
    while index < len(words):
        word = words[index]
        name, equals, _ = word.partition("=")
        if word in ("-v", "--verbose"):
            index += 1
        elif name in RERUN_KEPT | RERUN_DROPPED:
            taken = words[index:index + 1] if equals else words[index:index + 2]
            if len(taken) < (1 if equals else 2): return None
            if name in RERUN_KEPT: kept += taken
            index += len(taken)
        elif word.startswith("-"):
            return None
        else:
            index += 1  # a name the lane selects: the rerun names its own test
    return kept + ["-j", "1", "-v"]


def suite_of(command):
    """(start directory, patterns or None) of a lane whose command is eng unittest, by the options rerun_command keeps,
    or None for any other lane."""
    words = rerun_command(command)
    if words is None: return None
    words = words[words.index("unittest") + 1:]
    start, patterns, index = ".", [], 0
    while index < len(words):
        name, equals, value = words[index].partition("=")
        if not equals and name in RERUN_KEPT and index + 1 < len(words):
            index += 1
            value = words[index]
        if name in ("-s", "--start-directory"): start = value
        elif name in ("-p", "--pattern"): patterns.append(value)
        index += 1
    return start, patterns or None


def unittest_lanes(config):
    """{lane: (start, patterns)} of config's queue lanes (lanes_of) whose command is eng unittest."""
    lanes, found = config.get("lanes", {}), {}
    for lane in lanes_of(config):
        suite = suite_of(lanes[lane].get("command")) if isinstance(lanes.get(lane), dict) else None
        if suite: found[lane] = suite
    return found


def suite_tree(root, commit, start, patterns, into):
    """Write the files at the top of commit's start directory that eng unittest's checks before a run read
    (parallel.refusals), its Python files and those patterns match, into into. False when commit has no such
    directory."""
    relative = os.path.normpath(start)
    listed = git(root, "ls-tree", "-z", commit if relative == "." else f"{commit}:{relative}")
    if listed.returncode: return False
    names = {}
    for record in listed.stdout.split("\0"):
        meta, _, name = record.partition("\t")
        fields = meta.split()
        # A file, not a directory, a submodule or a symbolic link, which eng unittest's checks would follow.
        if not name or len(fields) != 3 or fields[0] not in ("100644", "100755"): continue
        if name.endswith(".py") or any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns or ["test*.py"]):
            names[name] = fields[2]
    into.mkdir(parents=True, exist_ok=True)
    if not names: return True
    done = subprocess.run(["git", "-C", str(root), "cat-file", "--batch"], capture_output=True, env=environment(),
                          input="".join(f"{sha}\n" for sha in names.values()).encode(), timeout=GIT_TIMEOUT)
    if done.returncode: raise RuntimeError(f"git cat-file could not read {commit[:12]}:{relative}")
    blobs, offset = {}, 0
    while offset < len(done.stdout):
        end = done.stdout.index(b"\n", offset)
        sha, _, size = done.stdout[offset:end].decode().split()
        blobs[sha] = done.stdout[end + 1:end + 1 + int(size)]
        offset = end + 2 + int(size)
    for name, sha in names.items(): (into / name).write_bytes(blobs[sha])
    return True


def prerun(root, commit, config):
    """The lines eng unittest would refuse each of config's eng unittest lanes with on commit's tree before running
    anything (parallel.refusals), once each, each suite's directory named under root, as a gate in a checkout of
    root's repository would print them: a gate they refuse runs nothing, in seconds, and gives no failed test to blame
    (vocaro-engineering#290)."""
    lines = []
    for start, patterns in unittest_lanes(config).values():
        if Path(start).is_absolute(): continue
        with tempfile.TemporaryDirectory(prefix="eng-prerun-") as folder:
            copy = Path(folder).resolve()
            if not suite_tree(root, commit, start, patterns, copy / start): continue
            for line in parallel.refusals(copy / start, patterns):
                line = line.replace(str(copy), str(root))
                if line not in lines: lines.append(line)
    return lines


def refused_alone(root, tip, config):
    """The lines of prerun that tip's tree gives and origin/main's does not: what the entry itself brings, which a
    gate of any batch holding it would refuse."""
    lines = prerun(root, tip, config)
    if not lines: return []
    before = prerun(root, MAIN, config)
    return [line for line in lines if line not in before]


def checked_before_running(root, tip, config):
    """Refuse tip, queuing nothing, when eng unittest would refuse a gate holding it before running anything: a
    module that does not import host_isolation, or does not parse. These checks take seconds, where the gate they
    refuse costs a round, and a bisect blind, since it names no failed test (vocaro-engineering#290)."""
    own = refused_alone(root, tip, config)
    if own:
        raise Refused(f"{tip[:12]} is not queued: eng unittest would refuse every gate holding it before running "
                      "anything:\n  " + "\n  ".join(own) + "\nFix that and land it again (guides/maintenance.md, The "
                      "merge queue, Checked before it is queued)")


def claims_before_queued(root, base, tip):
    """Refuse tip, queuing nothing, when a message of its commits after base denies a closing claim, as "This does not
    fix #43." does: GitHub closes the issue when the commit reaches main (landing.negated, vocaro-engineering#286)."""
    found = landing.negated_claims(lambda *words: git(root, *words), f"{base}..{tip}")
    if found is None: raise Refused(f"{tip[:12]} is not queued: its commit messages could not be read")
    if not found: return
    issue = found[0][3]
    raise Refused(f"{tip[:12]} is not queued: GitHub closes an issue a commit message names after a closing keyword, "
                  "even in a negated phrase:\n  "
                  + "\n  ".join(f"{commit[:12]} line {number} closes {named}: {line.strip()}"
                                for commit, number, line, named in found)
                  + f'\nFor an issue that stays open, write "Refs {issue}" or "leaves {issue} open"; to close it, write '
                    f'"Closes {issue}." on its own line. Reword the commit and land it again (guides/maintenance.md, '
                    "The merge queue, Checked before it is queued)")


def spelling_run(queue, root, commit):
    """(exit status, the lines it printed, the command) of the spelling lane on commit, in a scratch worktree outside
    every checkout, as the worker picks it (Worker.gate): the project's own spelling lane when commit's engineering.json
    configures one (Worker.has_lane), run as its command, here and with no admission, or the toolkit's check,
    IMPLICIT_SPELLING, when it does not. A status of None says why it was not run: a lane that is no one command, or
    a run that reached SPELLING_LIMIT."""
    with scratch(queue, commit, "spelling") as path:
        try: lanes = load(path).get("lanes", {})
        except (OSError, ValueError): lanes = {}
        lane = lanes.get(SPELLING)
        command = IMPLICIT_SPELLING if SPELLING not in lanes else lane.get("command") if isinstance(lane, dict) else None
        if not (isinstance(command, list) and command) or (SPELLING in lanes and any(
                lane.get(key) for key in ("foreground", "display", "unlockedScreen", "parallel", "wholeHost", "model"))):
            return None, ["the project's spelling lane is not one command that eng land can run here"], None
        env = environment()
        if not os.environ.get(TOOLKIT_VARIABLE):
            # A project's launcher finds the toolkit beside its own checkout, which a scratch worktree has not: this
            # one runs it. The toolkit's own entry runs the toolkit it brings, as its queue's gate does.
            ours = git(TOOLKIT_ROOT, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
            if ours != text(root, "rev-parse", "--path-format=absolute", "--git-common-dir"):
                env[TOOLKIT_VARIABLE] = str(TOOLKIT_ROOT)
        log = path.parent / "spelling.log"
        try: status_ = processes.run(command, cwd=path, log_path=log, timeout=SPELLING_LIMIT, env=env)
        except processes.TimedOut as error: return None, [f"{shlex.join(command)} reached its limit: {error}"], command
        printed = [line for line in log.read_text(errors="replace").splitlines() if line.strip()]
        return status_, printed, command


def spelled_before_queued(root, queue, tip, config):
    """Refuse tip, queuing nothing, when the spelling lane that config, origin/main's, has the queue run before the
    gate (spelled) finds a British spelling, exit 1, in tip's commits replayed onto origin/main, quoting its findings.
    The lane takes seconds, where a slip it finds costs a queue round, about 40 minutes on EPUBLib on 2026-11-07
    (vocaro-engineering#283). It is a hint, as check is: the worker still runs the lane on the batch. A check that
    cannot be made, or a lane that fails otherwise, queues the entry for the round to judge, and so does a failure
    origin/main gives too, which is not the entry's to fix."""
    if not spelled(config, SPELLING): return
    base = text(root, "rev-parse", MAIN)
    began = now()
    try:
        replayed = tip if git(root, "merge-base", "--is-ancestor", MAIN, tip).returncode == 0 else linear(queue, root, tip)
        if replayed is None:
            print("queue: could not check its spelling, as its commits do not apply onto origin/main alone; the round "
                  "runs the spelling lane", file=sys.stderr)
            return
        status_, printed, command = spelling_run(queue, root, replayed)
        main_status = spelling_run(queue, root, base)[0] if status_ == 1 else None
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        print(f"queue: could not check its spelling, so the round runs the spelling lane: {error}", file=sys.stderr)
        return
    ran = shlex.join(command) if command else "the spelling lane"
    if status_ is None:
        print(f"queue: could not check its spelling, so the round runs the spelling lane: {printed[0]}",
              file=sys.stderr)
    elif status_ == 0:
        print(f"queue: spelling checked in {now() - began:.1f}s: {ran} passes on it replayed onto origin/main at "
              f"{base[:12]}", file=sys.stderr)
    elif status_ != 1:
        print(f"queue: {ran} exited {status_} on it, which is no finding, so the round runs the spelling lane: "
              + (printed[-1] if printed else "it printed nothing"), file=sys.stderr)
    elif main_status == 1:
        print(f"queue: {ran} fails on origin/main at {base[:12]} too, so its findings are not this entry's alone; the "
              "round runs the spelling lane", file=sys.stderr)
    else:
        raise Refused(f"{tip[:12]} is not queued: {ran} fails on it replayed onto origin/main at {base[:12]}:\n  "
                      + "\n  ".join(printed[-SPELLING_SHOWN:]) + "\nFix that and land it again (guides/maintenance.md, "
                      "The merge queue, Checked before it is queued)")


def failed_tests(log):
    """(the ids of the tests an eng unittest lane's log reports failed, None), or (None, why it reports none it
    can rerun): its last summary must say FAILED, with nothing UNMEASURED, and each failed process's output must
    name at least one failed test, none of them a module that did not load, and none ran other than it was given."""
    summaries = UNITTEST_SUMMARY.findall(log)
    if not summaries: return None, "its log has no eng unittest summary"
    verdict = summaries[-1]
    if "UNMEASURED: " in verdict: return None, "eng unittest also left tests unmeasured"
    if not verdict.startswith("FAILED: "): return None, f"eng unittest's verdict is {verdict}"
    shards = FAILED_SHARD.findall(verdict[len("FAILED: "):])
    if not shards: return None, f"eng unittest's verdict names no process: {verdict}"
    headers = list(SHARD_HEADER.finditer(log))
    outputs = {}
    for number, header in enumerate(headers):
        end = headers[number + 1].start() if number + 1 < len(headers) else len(log)
        outputs.setdefault(header.group(1), log[header.end():end])
    found = []
    for name, detail in shards:
        if detail.startswith("ran "): return None, f"{name} {detail}"
        ids = []
        for match in FAILED_TEST.finditer(outputs.get(name, "")):
            method, where = match.groups()
            # unittest names the test's id in parentheses since Python 3.11, and its class before.
            ids.append(where if where.endswith(f".{method}") else f"{where}.{method}")
        if not ids: return None, f"the output of {name} names no failed test"
        if any(identity.startswith("unittest.") for identity in ids): return None, f"{name} did not load"
        found += ids
    return list(dict.fromkeys(found)), None


def imported_files(checkout, top, test):
    """The repository's files that the module of test, a test id, is and imports, each relative to checkout: the
    module's own import statements, at any depth of it, each with the packages' __init__.py on the way, not what those
    files import in turn (owner's decision of 2026-11-07, vocaro-engineering#231: "the toolkit files it imports"). An
    absolute import is looked for under top, the suite's top-level directory, then src/python, src and checkout, where
    a test finds the project's code; one found under none, as the standard library's, is not the project's. None when
    no file under top is the test's module."""
    checkout, top = Path(checkout).resolve(), Path(top).resolve()
    roots = [top, checkout / "src/python", checkout / "src", checkout]

    def module(base, parts):
        """The files that importing parts from base loads: each package's __init__.py on the way, and the module."""
        found, place = [], base
        for number, part in enumerate(parts):
            place = place / part
            if (place / "__init__.py").is_file(): found.append(place / "__init__.py")
            elif number == len(parts) - 1 and place.with_suffix(".py").is_file(): found.append(place.with_suffix(".py"))
            else: return []
        return found

    def absolute(dotted):
        parts = dotted.split(".")
        return next((found for found in (module(root, parts) for root in roots) if found), [])

    parts = test.split(".")
    start = next((found[-1] for count in range(len(parts), 0, -1) for found in [module(top, parts[:count])]
                  if found), None)
    if start is None: return None
    files = [start]
    try: tree = ast.parse(start.read_text(errors="replace"), str(start))
    except (OSError, SyntaxError, ValueError): tree = ast.Module(body=[], type_ignores=[])
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names: files += absolute(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.level:
            base = start.parent
            for _ in range(node.level - 1): base = base.parent
            named = node.module.split(".") if node.module else []
            files += module(base, named) if named else [base / "__init__.py"] if (base / "__init__.py").is_file() else []
            for alias in node.names: files += module(base, [*named, alias.name])
        elif isinstance(node, ast.ImportFrom):
            files += absolute(node.module)
            for alias in node.names: files += absolute(f"{node.module}.{alias.name}")
    found = {path.resolve() for path in files}
    return sorted(path.relative_to(checkout).as_posix() for path in found if path.is_relative_to(checkout))


def named_in(path, output):
    """Whether output, a failing test's, names path, a repository path: whole, not inside a longer path or word,
    and with a directory or an extension, so that a bare word such as a top-level file's name is not taken for it."""
    if "/" not in path and "." not in path: return False
    return re.search(rf"(?<![\w./-]){re.escape(path)}(?![\w/-]|\.\w)", output) is not None


def gh_runner(checkout):
    """gh in checkout, whose origin names the project's repository, never prompting, bounded by GH_TIMEOUT."""
    def gh(*arguments):
        try:
            return subprocess.run(["gh", *arguments], cwd=checkout, capture_output=True, text=True, timeout=GH_TIMEOUT,
                                  stdin=subprocess.DEVNULL, env={**os.environ, **GH_ENV})
        except FileNotFoundError: return subprocess.CompletedProcess(["gh"], 127, "", "gh is not installed")
        except subprocess.TimeoutExpired: return subprocess.CompletedProcess(["gh"], 124, "", f"gh took over {GH_TIMEOUT}s")
    return gh


def flake_words(flake):
    """One line for a flake: in the worker's log, the landed tip's trailer and the tracker."""
    return (f"{flake['test']} failed the gate at {flake['commit'][:12]} and passed alone {len(flake['rerunLogs'])} "
            f"times there; gate log {flake['gateLog']}; reruns {', '.join(flake['rerunLogs'])}")


def hardening(checkout, flake, gh=None):
    """(the issue's URL, None) once the project's tracker has the flake: a comment on the first open issue a search
    made just before finds naming the test's name, or a new hardening issue; (None, why) when gh could not, which
    stops nothing (owner's decision of 2026-11-07, vocaro-engineering#231, and the rule that every flaky test gets a
    hardening issue). gh(*arguments) runs gh for the project, gh_runner's by default; tests give their own."""
    gh = gh or gh_runner(checkout)
    name = flake["test"].rsplit(".", 1)[-1]
    body = (f"The merge queue's gate failed `{flake['test']}` at {flake['commit'][:12]}, and it then passed alone "
            f"{len(flake['rerunLogs'])} of {len(flake['rerunLogs'])} times on that commit, so it is flaky. "
            f"{flake['outcome']}\n\n- Gate log: `{flake['gateLog']}`\n"
            + "".join(f"- Rerun: `{path}`\n" for path in flake["rerunLogs"])
            + f"- Mac: {flake.get('mac') or 'this Mac'}\n\n"
            "Recorded by the merge queue (vocaro/vocaro-engineering#231).\n")
    listed = gh("issue", "list", "--state", "open", "--search", f"\"{name}\" in:title,body,comments", "--json",
                "number,url,title", "--limit", "10")
    if listed.returncode:
        return None, "gh issue list failed: " + (listed.stderr.strip().splitlines() or ["no message"])[0]
    try: found = json.loads(listed.stdout or "[]")
    except ValueError as error: return None, f"gh issue list printed no issue list ({error})"
    if found and isinstance(found[0], dict) and found[0].get("url"):
        said = gh("issue", "comment", str(found[0]["number"]), "--body", body)
        if said.returncode:
            return None, f"gh issue comment on {found[0]['url']} failed: " + (said.stderr.strip().splitlines() or ["no message"])[0]
        return found[0]["url"], None
    title = f"Harden the flaky test {name} ({flake['test'].rsplit('.', 1)[0]})"
    made = gh("issue", "create", "--title", title, "--body", body)
    if made.returncode: return None, "gh issue create failed: " + (made.stderr.strip().splitlines() or ["no message"])[0]
    return (made.stdout.strip().splitlines() or [None])[-1], None


def busy_of(host):
    """(the CPU busy share in percent, the busy limit it is judged by, the Mac read) of host, the second Mac, by
    remote.busy_there, or with host None of this Mac, as admission reads it; None when it cannot be read."""
    if host:
        try: busy, limit = remote.busy_there(host)
        except remote.Unavailable: return None
        return None if busy is None else (busy, limit, host)
    try: limit = resources.busy_limit()
    except ValueError: return None
    busy = resources.cpu_busy(resources.slot_directory(), wait=True)[0]
    return None if busy is None else (busy, limit, "this Mac")


class BusySampler:
    """The CPU busy share of the Mac a gate lane runs on, read every BUSY_SAMPLE seconds while it runs, and once at its
    end when no reading was taken (vocaro-engineering#235). The Mac is the second Mac once the lane's dispatch names it
    (remote.dispatched), and this Mac before that. A reading that fails is left out."""
    def __init__(self, checkout, lane, began):
        self.checkout, self.lane, self.began = checkout, lane, began
        self.readings = []
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self.every, name="gate busy share", daemon=True)

    def read(self):
        # A measurement only: nothing a reading raises may end the gate or the round.
        try: found = busy_of(remote.dispatched(self.checkout, self.lane, self.began))
        except Exception: found = None  # noqa: BLE001
        if found: self.readings.append(found)

    def every(self):
        while not self.stopped.wait(BUSY_SAMPLE): self.read()

    def start(self):
        self.thread.start()
        return self

    def finish(self):
        """What the gate's record keeps of the readings: cpuBusy, their mean, cpuBusyHighest, busyLimit and busyOn of
        the last, and busyReadings; nothing when none was taken."""
        self.stopped.set()
        self.thread.join(timeout=GH_TIMEOUT)
        if not self.readings: self.read()
        if not self.readings: return {}
        shares = [reading[0] for reading in self.readings]
        return {"cpuBusy": round(sum(shares) / len(shares), 1), "cpuBusyHighest": max(shares),
                "busyLimit": self.readings[-1][1], "busyOn": self.readings[-1][2], "busyReadings": len(shares)}


def waited_for(lines, waiting=None, where=None):
    """(what the lane waits for, or None, and where it runs: HERE_MAC, the second Mac's name, or None while it waits)
    after lines of its gate log, from waiting and where as the lines before them left them: the newest wait eng run
    reported, cleared by an admission on this Mac or a send to the second Mac that follows it."""
    for line in lines:
        line = line.strip()
        found = ADMISSION_WAIT.match(line)
        sent = SENT.match(line)
        if found: waiting = f"admission on this Mac: {found.group(1)}"
        elif OFFER_WAIT.match(line): waiting, where = OFFER_WAIT.match(line).group(1), None
        elif DECLINED.match(line):
            waiting, where = f"this Mac's admission, as the second Mac declined it: {DECLINED.match(line).group(1)}", None
        elif ADMITTED.match(line): waiting, where = None, HERE_MAC
        elif sent: waiting, where = None, sent.group(1)
    return waiting, where


# What waited_for says of a lane this Mac admitted.
HERE_MAC = "this Mac"


def stuck(gate, at=None):
    """What `eng queue status` and the worker log say of a gate held up by dead guard entries, or None: its wait,
    since waitingSince, names a dead or uncertain entry and has lasted STUCK_SECONDS (#315)."""
    since, waiting = gate.get("waitingSince"), gate.get("waiting") or ""
    if not isinstance(since, (int, float)) or not DEAD_ENTRY.search(waiting) or \
            (at or time.time()) - since < STUCK_SECONDS:
        return None
    return (f"stuck: the gate has waited {ago(since, at)} for admission held up by dead or uncertain guard entries that "
            "admission has not recovered; `python3 scripts/eng admissions recover` lists them (guides/shared-host.md, "
            "Lock reclamation)")


class GateWatch:
    """The record of a running gate lane in the round's account (Queue.note): where it runs, by its dispatch's
    record (remote.dispatched) or by its log, a send to the second Mac or this Mac's admission, and what it waits
    for, by its log (waited_for), read every WATCH_POLL seconds. Nothing it reads or fails to read may end the gate."""
    def __init__(self, worker, lane, began, log):
        self.queue, self.checkout, self.role = worker.queue, worker.checkout, worker.role
        self.lane, self.began, self.log = lane, began, log
        self.offset, self.waiting, self.where, self.partial = 0, None, None, ""
        # When the current wait began, and whether the worker log has said it is stuck (stuck).
        self.since, self.said_stuck, self.say = None, False, worker.say
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self.every, name="gate watch", daemon=True)
        self.queue.note(lambda state: state.setdefault("gates", {}).__setitem__(self.role, {
            "lane": lane, "began": began, "entries": list(worker.gating), "head": worker.head(), "log": str(log),
            "mac": None, "waiting": None}))

    def look(self):
        try:
            with open(self.log, "rb") as log:
                log.seek(self.offset)
                said = log.read()
            self.offset += len(said)
            lines = (self.partial + said.decode(errors="replace")).split("\n")
            self.partial = lines.pop()
            self.waiting, self.where = waited_for(lines, self.waiting, self.where)
        except OSError: pass
        try: host = remote.dispatched(self.checkout, self.lane, self.began)
        except Exception: host = None  # noqa: BLE001
        host = host or (self.where if self.where != HERE_MAC else None)
        mac = f"the second Mac ({host})" if host else HERE_MAC if self.where else None
        self.since = (self.since or time.time()) if self.waiting else None
        held_up = stuck({"waiting": self.waiting, "waitingSince": self.since})
        if held_up and not self.said_stuck: self.say(f"{self.lane}: {held_up}")
        self.said_stuck = bool(held_up)
        self.queue.gate_note(self.role, mac=mac, waiting=self.waiting, waitingSince=self.since)

    def every(self):
        while not self.stopped.wait(WATCH_POLL):
            try: self.look()
            except Exception: pass  # noqa: BLE001

    def start(self):
        self.thread.start()
        return self

    def finish(self):
        """Stop watching, and take the gate's record out of the account: it is no longer running."""
        self.stopped.set()
        if self.thread.ident is not None: self.thread.join(timeout=GH_TIMEOUT)
        self.queue.note(lambda state: state.get("gates", {}).pop(self.role, None))


class Queue:
    def __init__(self, root):
        self.root = Path(root)
        common = Path(text(root, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        self.main_checkout = common.parent
        self.directory = common / "eng-queue"
        self.entries = self.directory / "entries"
        self.logs = self.directory / "logs"
        self.hold_file = self.directory / "hold"
        self.round_file = self.directory / ROUND_FILE
        self.round_lock = threading.Lock()  # between the threads of one worker, the only writer of round_file
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
                    if state != TESTING: current.pop("stopsWhenRemoved", None)
                    self.save(current)

    def round_state(self):
        """The worker's account of its round (ROUND_FILE), or None when there is none."""
        try: found = json.loads(self.round_file.read_text())
        except (OSError, ValueError): return None
        return found if isinstance(found, dict) else None

    def note(self, change):
        """Apply change to the round's account and write it, atomically. An account that cannot be written is
        left as it was: it explains the queue, and never stops it."""
        with self.round_lock:
            state = self.round_state() or {}
            change(state)
            state["updated"] = time.time()
            staged = self.round_file.with_suffix(f".{os.getpid()}.tmp")
            try:
                staged.write_text(json.dumps(state, indent=2) + "\n")
                os.replace(staged, self.round_file)
            except OSError: pass

    def gate_note(self, role, **fields):
        """Set fields of the record of role's running gate, while it has one."""
        def change(state):
            gate = state.get("gates", {}).get(role)
            if gate is not None: gate.update(fields)
        self.note(change)

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


def land(root, args, directory=None):
    """`eng land`: add to this repository's queue or, with --repo, another's, whose own queue, lanes and
    worker take it exactly as they take that repository's own sessions' commits. With another
    repository's main checkout HEAD would be whatever it has checked out, so the commit is named.
    directory, the command's current directory, must be in this repository unless --repo names one:
    each project's launcher lands its own repository, so another project's `scripts/eng land` run from
    here would queue that project's HEAD, or report it landed (vocaro-engineering#210)."""
    elsewhere = None if args.repo or directory is None else other_repository(root, directory)
    if elsewhere is not None:
        print(f"eng: this eng lands {root}, but the current directory is in {elsewhere}: run that "
              "checkout's own scripts/eng land, or name the commit with --repo", file=sys.stderr)
        return 1
    target = root
    if args.repo:
        target = repository(root, args.repo)
        if args.reference is None:
            raise ValueError(f"--repo lands a commit you name: `eng land --repo {args.repo} BRANCH`, since HEAD "
                             "there is another session's")
    deadline = time.monotonic() + args.limit
    while True:
        try:
            identifier = add(target, args.reference or "HEAD", args.note, quiet_wait=args.wait,
                             front=getattr(args, "front", False), no_gate=getattr(args, "no_gate", False),
                             why=getattr(args, "why", None))
            break
        except Refused as error:
            print(f"eng: {error}", file=sys.stderr)
            if not args.wait or not error.waits_for: return 1
            # Refused for entries ahead alone: wait for them, then check again against the whole queue, which may
            # name an entry queued meanwhile (vocaro-engineering#302).
            print(f"eng land --wait: waits for {', '.join(error.waits_for)}, then checks again against the whole "
                  "queue", file=sys.stderr, flush=True)
            waited = wait_for(target, error.waits_for, max(0.0, deadline - time.monotonic()))
            if waited == resources.BUSY: return waited
    left = max(0.0, deadline - time.monotonic())
    status_ = wait(target, identifier, left) if args.wait and identifier is not None else 0
    warn(target)
    return status_


def other_repository(root, directory):
    """The top of the Git repository directory is in, when that is not root's repository (a worktree
    of root's is root's), or None. A directory in no repository is None too."""
    def common(path):
        done = git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
        return Path(done.stdout.strip()).resolve() if done.returncode == 0 else None
    here = common(directory)
    if here is None or here == common(root): return None
    return text(directory, "rev-parse", "--show-toplevel")


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
    gating = config
    try: lanes_of(config)
    except ValueError: lanes_of(gating := config_at(root, tip))
    if git(root, "merge-base", "--is-ancestor", tip, MAIN).returncode == 0:
        print(f"origin/main already holds {tip[:12]}: nothing to queue")
        return None
    base = text(root, "merge-base", tip, MAIN)
    commits = text(root, "rev-list", "--reverse", f"{base}..{tip}").split()
    merges = text(root, "rev-list", "--merges", f"{base}..{tip}").split()
    if merges:
        # A branch refreshed by merging main, as the app's sync does, lands as its own commits replayed (#217).
        replayed = linear(queue, root, tip)
        if replayed is None:
            raise ValueError(merge_refusal(root, merges, tip))
        if replayed == text(root, "rev-parse", MAIN):
            print(f"origin/main already holds the commits of {tip[:12]} but its merges: nothing to queue")
            return None
        print(f"queue: dropped {', '.join(merge[:12] for merge in merges)}, "
              f"{'merges' if len(merges) > 1 else 'a merge'} of origin/main, and queued the branch's own commits "
              f"replayed onto origin/main as {replayed[:12]}; the branch is left as it is", file=sys.stderr)
        replays, tip = tip, replayed
        base = text(root, "merge-base", tip, MAIN)
        commits = text(root, "rev-list", "--reverse", f"{base}..{tip}").split()
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
    # A negated closing claim closes its issue on landing, whatever a gate finds (#286): refused before anything else.
    claims_before_queued(root, base, tip)
    # What a round would find, before anything is queued or replaced: a refused entry leaves the queue as it was. First
    # eng unittest's own checks before a run, which a gate holding it would fail in seconds and bisect blind (#290).
    checked_before_running(root, tip, gating)
    check(root, queue, {"id": "new", "tip": tip, "commits": commits},
          [] if front else round_order([entry for entry in waiting if entry not in replaced]), front)
    # Then the spelling lane, on its commits replayed onto origin/main, which takes seconds where a slip costs a round.
    spelled_before_queued(root, queue, tip, gating)
    for entry in replaced:
        remove(queue, entry["id"], quiet=True)
        print(f"queue: replaced {entry['id']}, the branch's earlier commits", file=sys.stderr)
        if entry["state"] == TESTING: print(f"queue: {stopping(queue, entry)}", file=sys.stderr)
    now = time.time()
    identifier = f"{time.strftime('%Y%m%d-%H%M%S', time.localtime(now))}-{tip[:8]}"
    entry = {"id": identifier, "tip": tip, "base": base, "commits": commits, "branch": branch or None, "note": note,
             "subject": text(root, "log", "-1", "--format=%s", tip), "submitted": now, "state": QUEUED,
             "directory": str(root), **({"replays": replays} if merges else {}), **flags}
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
    if found["why"]: raise Refused(refusal(entry["tip"], found, base, root), found["waits"])
    for line in tried(found, base, front, now() - began): print(f"queue: {line}", file=sys.stderr)


def asked_for(name):
    """What an entry's option, a name of OPTIONS, does, for eng land's lines and `eng queue status`."""
    return ("is tested next, ahead of everything waiting, as a batch of its own" if name == "front" else
            "lands alone on the spelling lane, without the gate, and is recorded as ungated")


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
    if not quiet:
        print(f"removed {identifier}")
        if entry["state"] == TESTING: print(stopping(queue, entry))
    return 0


def stopping(queue, entry):
    """What becomes of the gate of entry, removed while it was testing (vocaro-engineering#247): a worker that records
    stopsWhenRemoved on the entries it gates stops their lanes once it sees the removal (Worker.gate); an older one
    lets them run on, holding the queue until they end."""
    if not queue.working():
        return f"{entry['id']} was testing, but no worker runs, so no gate of it is left to stop"
    seconds = entry.get("stopsWhenRemoved")
    if seconds:
        return (f"{entry['id']} was testing: the worker stops its gate within about {seconds:g}s, on this Mac or the "
                "second, which frees its admission there, and gates the rest of its batch again")
    return (f"{entry['id']} was testing under a worker that does not stop a gate for a removal: its gate runs on until "
            f"its lanes end, at most {LANE_LIMIT / 3600:g} h a lane, and holds the queue until then")


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
    for flake in entry.get("flakes", []):
        line += (f"\n         flaky: {flake['test']} failed the gate at {flake['commit'][:12]} and passed alone "
                 f"{len(flake['rerunLogs'])} times"
                 + (f"; {flake['issue']}" if flake.get("issue") else f"; not on the tracker: {flake.get('trackerError')}"))
    if entry.get("why"): line += f"\n         {entry['why']}"
    if entry.get("log"): line += f"\n         log: {entry['log']}"
    return line


def ago(began, at=None):
    """How long ago began, an epoch time, was, in minutes or seconds."""
    seconds = max(0, int((at or time.time()) - began))
    return f"{seconds // 60}m{seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


def entries_word(count):
    return f"{count} entr{'y' if count == 1 else 'ies'}"


def round_lines(state, at=None):
    """What `eng queue status` says of the worker's round from its account (ROUND_FILE): whether it bisects and how
    far, each running gate's lane, Mac and time, what each gate and the round wait for, and any stacked gate."""
    lines = []
    if state.get("round"):
        line = f"round {state['round']}: {entries_word(len(state.get('entries', [])))}"
        if state.get("adopted"): line += ", whose gate began stacked on the batch that landed before it"
        bisect = state.get("bisect")
        if bisect:
            line += (f"; bisecting {entries_word(bisect.get('suspects', 0))}: older half "
                     f"{entries_word(bisect.get('older', 0))}, newer half "
                     + (f"{bisect['newer']}" if bisect.get("newer") else "waiting for the older"))
        lines.append(line)
    order = {"batch": 0, "older half": 1, "newer half": 2, "stacked": 3}
    for role, gate in sorted(state.get("gates", {}).items(), key=lambda item: (order.get(item[0], 9), item[0])):
        what = "stacked gate" if role == "stacked" else role
        line = (f"  {what} ({', '.join(gate.get('entries', [])) or 'no entry'}): {gate.get('lane')} lane"
                f" on {gate.get('mac') or 'no Mac yet'}, running {ago(gate.get('began', 0), at)}")
        if gate.get("waiting"): line += f"\n    waiting: {gate['waiting']}"
        if stuck(gate, at): line += f"\n    {stuck(gate, at)}"
        if gate.get("log"): line += f"\n    log: {gate['log']}"
        lines.append(line)
    if state.get("waiting"):
        lines.append(f"  waiting for {state['waiting']}"
                     + (f", for {ago(state['waitingSince'], at)}" if state.get("waitingSince") else ""))
    return lines


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
    # A worker that stopped leaves no round: what it wrote is not the queue's state.
    current = queue.round_state() if working else None
    for gate in (current or {}).get("gates", {}).values():
        if isinstance(gate.get("began"), (int, float)): gate["runningSeconds"] = round(time.time() - gate["began"])
        if stuck(gate): gate["stuck"] = stuck(gate)
    if as_json:
        print(json.dumps({"worker": working, "held": why, "round": current, "waiting": waiting, "recent": done,
                          "ungated": [entry["id"] for entry in ungated]}, indent=2))
        return 0
    print(f"worker: {'running' if working else 'none'}" + (f"; held {why}" if why else ""))
    if waiting and not working and not why: print("waiting with no worker: `eng queue resume` starts one")
    for line in round_lines(current or {}): print(line)
    for entry in ungated:
        print(f"ungated on main: {entry['id']} landed {entry['landedAs'][:12]} without the gate, which the next gate "
              "on main covers")
    for entry in waiting + done: print(describe(entry))
    return 0


def refused_modules(line):
    """The test modules a refusal of eng unittest's names (parallel.refusals), by file name, whatever checkout it
    was printed in."""
    said = line.removeprefix("eng unittest: ")
    for ending in (" must import ", " does not parse "):
        if ending in said: return set(said.split(ending, 1)[0].split(", "))
    return set()


def refusal_key(line):
    """A refusal of eng unittest's without the directories its paths name, so that the same refusal printed in two
    checkouts, as a bisect's two halves print it, is one (Worker.older)."""
    return re.sub(r"\S*/", "", line)


def patch_of(root, entry):
    """The stable patch ID of the change entry's commits make together, which a rebase keeps, or None when they cannot
    be read."""
    commits = entry.get("commits") or []
    if not commits or not entry.get("tip"): return None
    diff = git(root, "diff", "--no-color", f"{commits[0]}^", entry["tip"])
    if diff.returncode or not diff.stdout: return None
    done = subprocess.run(["git", "-C", str(root), "patch-id", "--stable"], input=diff.stdout, capture_output=True,
                          text=True, env=environment(), timeout=GIT_TIMEOUT)
    return done.stdout.split()[0] if done.returncode == 0 and done.stdout.split() else None


def gate_logs(queue, lanes, working):
    """The logs of the newest DOCTOR_LOGS gates of lanes, eng unittest lanes, newest first, back to the last that gave
    a verdict, OK or FAILED: [(name, the refusal it printed or None, its eng unittest verdict or None)]. A gate that
    still runs is no gate without a verdict, so its log is left out."""
    running = {gate.get("log") for gate in (queue.round_state() or {}).get("gates", {}).values()} if working else set()
    logs = []
    for lane in lanes:
        for path in queue.logs.glob(f"*-{lane}.log"):
            if str(path) in running: continue
            with contextlib.suppress(OSError): logs.append((path.stat().st_mtime, path.name, path))
    found = []
    for _, name, path in sorted(logs)[-DOCTOR_LOGS:][::-1]:
        said = path.read_text(errors="replace")
        summaries = UNITTEST_SUMMARY.findall(said)
        if summaries and "UNMEASURED: " not in summaries[-1]: break
        refused = REFUSAL.findall(said)
        found.append((name, refused[-1] if refused else None, summaries[-1] if summaries else None))
    return found


def doctor(root, as_json=False):
    """`eng queue doctor`: print diagnose's findings, or that the queue is healthy. 0 when it finds the queue healthy,
    1 when it finds anything."""
    findings, waiting, working = diagnose(root)
    if as_json:
        print(json.dumps({"healthy": not findings, "waiting": len(waiting), "worker": working, "findings": findings},
                         indent=2))
        return 1 if findings else 0
    if not findings:
        print(f"eng queue doctor: healthy: {entries_word(len(waiting))} waiting, worker "
              f"{'running' if working else 'not needed'}, and no gate since the last verdict ran nothing")
        return 0
    print(f"eng queue doctor: {len(findings)} finding{'' if len(findings) == 1 else 's'}, and it changed nothing:")
    for finding in findings:
        print(f"{finding['signal']}: {finding['diagnosis']}\n  next: {finding['remedy']}")
    return 1


def diagnose(root):
    """(findings, the waiting entries, whether a worker runs): read the queue's entries, its hold, its worker and its
    newest gate logs for the signs of a queue that is stuck rather than testing, and say for each what it means and
    what would heal it (vocaro-engineering#290): a hold; entries waiting with no worker (#287); gates that eng unittest
    refused before running anything, which give no failed test to rerun (#280), with the entry that brings the
    refusal; gates in a row with no verdict (#253, #268, #282); a waiting entry that eng unittest would refuse; and an
    entry that came back unchanged after the same change failed or was removed. It changes nothing, neither entries
    nor the hold. The worker acts on a refusal itself (Worker.blame_refusal, Worker.refused_again) and puts this
    diagnosis in the hold it makes when it cannot blame one entry."""
    queue = Queue(root)
    entries = queue.all()
    waiting = [entry for entry in entries if entry["state"] in WAITING]
    working, held = queue.working(), queue.held()
    findings = []

    def found(signal, diagnosis, remedy):
        findings.append({"signal": signal, "diagnosis": diagnosis, "remedy": remedy})

    if held:
        found("held", f"the queue is held {held}", "deal with the cause the hold names, then run `eng queue resume`, "
              "unless the hold says it is for the owner")
    elif waiting and not working:
        found("no worker", f"{entries_word(len(waiting))} wait and no worker runs, so nothing gates them "
              "(vocaro-engineering#287)", "`eng queue resume` starts one")
    try: suites = unittest_lanes(config := config_at(root, MAIN))
    except ValueError as error:
        suites = {}
        found("configuration", f"origin/main's queue lanes cannot be read: {error}", "fix engineering.json on main")
    # Each waiting entry whose tree alone eng unittest refuses, as eng land now refuses before queuing: one queued
    # before that check, or by a toolkit without it.
    refusing = {}
    for entry in waiting if suites else []:
        try: own = refused_alone(root, entry["tip"], config)
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError): own = []
        if own: refusing[entry["id"]] = own
    gates = gate_logs(queue, suites, working)
    refused = [gate for gate in gates if gate[1]]
    if refused:
        line = refused[0][1]
        same = [name for name, said, _ in gates if said and refused_modules(said) == refused_modules(line)]
        culprits = [identifier for identifier, own in refusing.items()
                    if any(refused_modules(said) & refused_modules(line) for said in own)]
        diagnosis = (f"{len(same)} gate{'' if len(same) == 1 else 's'} since the last verdict ran nothing, as eng "
                     f"unittest refused {'it' if len(same) == 1 else 'them'} before running anything: {line} (logs: "
                     f"{', '.join(same)}). A refusal is no test failure, so no rerun can confirm one, and a bisect "
                     "through it is blind (vocaro-engineering#280)")
        if len(culprits) == 1:
            remedy = (f"{culprits[0]} brings it, as its tree alone is refused so: the worker sends it back by itself "
                      "from the next gate that holds it (vocaro-engineering#290), or take it out now with `eng queue "
                      f"remove {culprits[0]}`, and fix it in its session")
        elif culprits:
            remedy = (f"{', '.join(culprits)} each bring a module it names, so none is the one cause: fix each, or "
                      "let the bisect find them")
        else:
            remedy = ("no waiting entry's tree alone is refused so: the cause is on origin/main, in how entries "
                      "combine, or gone with an entry that left; read the newest log")
        found("refused gates", diagnosis, remedy)
    blank = [name for name, said, _ in gates if not said]
    if len(blank) >= DOCTOR_NO_VERDICTS:
        found("no verdict", f"{len(blank)} gates since the last verdict gave none, each leaving tests unmeasured or "
              f"printing no eng unittest summary: {', '.join(blank)} (vocaro-engineering#253, #268, #282)",
              f"the worker runs it again up to queue.noVerdictRuns times and then holds; read {blank[0]} for the "
              "cause: a lane that reached its limit, or a second Mac out of reach")
    for identifier, own in refusing.items():
        found("refused before running", f"eng unittest refuses {identifier}'s tree before running anything: "
              + "; ".join(own) + ". Every gate holding it runs nothing",
              f"take it out with `eng queue remove {identifier}`, fix it and land it again, which now checks this")
    # An entry that came back as it was, after the same change from the same branch failed or was taken out.
    gone = [entry for entry in entries if entry["state"] in (FAILED, REMOVED)]
    patches = {}
    for entry in waiting:
        for other in gone:
            if other["submitted"] >= entry["submitted"]: continue
            if not ((entry.get("branch") and entry.get("branch") == other.get("branch"))
                    or entry.get("subject") == other.get("subject")): continue
            for each in (entry, other):
                if each["id"] not in patches: patches[each["id"]] = patch_of(root, each)
            if patches[entry["id"]] and patches[entry["id"]] == patches[other["id"]]:
                found("came back", f"{entry['id']} holds the very change {other['id']} held, which "
                      f"{'failed' if other['state'] == FAILED else 'was taken out'}"
                      + (f": {other['why']}" if other.get("why") else ""),
                      "unless that cause was outside the change, it comes back the same way: take it out with "
                      f"`eng queue remove {entry['id']}` and fix it first")
                break
    return findings, waiting, working


def result_files(folder):
    """How many files a lane's results folder holds and in which of its folders, in words, the fullest first and at
    most RESULT_FOLDERS_NAMED of them; empty when it holds none or cannot be read."""
    try: found = collections.Counter(str(path.parent.relative_to(folder)) for path in folder.rglob("*") if path.is_file())
    except OSError: return ""
    if not found: return ""
    named = found.most_common(RESULT_FOLDERS_NAMED)
    rest = len(found) - len(named)
    total = sum(found.values())
    return (f"{total} file{'' if total == 1 else 's'} ({', '.join(f'{name}: {count}' for name, count in named)}"
            + (f", and {rest} more folder{'' if rest == 1 else 's'}" if rest else "") + ")")


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
            if entry["state"] == LANDED:
                gate = entry.get("gate")
                # Where the gate's per-test results are (Worker.results_named), from a gate that named them.
                if isinstance(gate, dict) and gate.get("results"): print(f"{gate['results']}\ngate log: {gate['log']}")
                print(f"landed: {entry['landedAs']}")
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


def wait_for(root, identifiers, limit=WAIT_LIMIT):
    """`eng queue wait ID ID ...`, as an eng land refusal names it: return once none of the entries is queued, in its
    gate or passed, so that one a `--front` entry sends back to queued keeps the wait going, saying where each ended
    and what is worth doing next: 0 each landed, so rebase; 1 one came back, so the commit it conflicted with may
    apply as it is; 75 while the queue is held or the limit passes with one still waiting (vocaro-engineering#304).
    `eng queue wait` with one ID is `wait`'s, which follows an entry of your own; eng land --wait waits here."""
    identifiers = list(dict.fromkeys(identifiers))
    queue = Queue(root)
    unknown = [identifier for identifier in identifiers if queue.read(identifier) is None]
    if unknown: raise ValueError(f"The queue has no entr{'y' if len(unknown) == 1 else 'ies'} {', '.join(unknown)}")
    deadline = time.monotonic() + limit
    while True:
        entries = [queue.read(identifier) or {"id": identifier, "state": REMOVED, "subject": "(no record)"}
                   for identifier in identifiers]
        waiting = [entry for entry in entries if entry["state"] in WAITING]
        if not waiting:
            for entry in entries: print(describe(entry))
            back = [entry["id"] for entry in entries if entry["state"] != LANDED]
            if not back:
                print("all landed: rebase onto origin/main, then `eng land` again")
                return 0
            print(f"{', '.join(back)} came back, so a commit that conflicted with {'them' if len(back) > 1 else 'it'} "
                  "may apply as it is: `eng land` again checks again")
            return 1
        said = ", ".join(f"{entry['id']} ({entry['state']})" for entry in waiting)
        why = queue.held()
        if why:
            print(f"{said} waiting, and the queue is held {why}")
            return resources.BUSY
        if time.monotonic() >= deadline:
            print(f"{said} still waiting after {limit:g}s")
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


class Removal:
    """The cancel a gate's lanes run with (processes.run): set once cancel is, or once one of the entries ids that the
    gate tests has left the queue, as `eng queue remove` or a branch added again takes it out, which it looks for at
    most every REMOVAL_POLL seconds. gone names those entries (vocaro-engineering#247)."""
    def __init__(self, queue, ids, cancel=None):
        self.queue, self.ids, self.cancel = queue, tuple(ids), cancel
        self.gone, self.looked = [], None

    def look(self):
        """Whether an entry of ids has left the queue, by its record now."""
        self.looked = time.monotonic()
        self.gone = [identifier for identifier in self.ids
                     if (self.queue.read(identifier) or {}).get("state") not in WAITING]
        return bool(self.gone)

    def is_set(self):
        if self.cancel is not None and self.cancel.is_set(): return True
        if self.gone: return True
        if not self.ids or (self.looked is not None and time.monotonic() - self.looked < REMOVAL_POLL): return False
        return self.look()


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
        # The lanes whose next run goes only to a Mac whose screen reads unlocked, since a locked screen left their
        # last run without a verdict (screen_locked, vocaro-engineering#293).
        self.unlocked = set(progress.get("unlocked", ())) if progress else set()
        self.code = running()  # the commit this worker's code came from, which toolkit compares with the copy's
        self.rounds = 0  # the rounds this worker has finished, which run reads to count its tries
        self.deferred = None  # why follow last left the shared checkout behind for a run from it, or None
        self.timed_out = None  # why the last gate's lane reached its limit, or None (gate)
        self.ran_on = None  # the second Mac that ran the last lane the gate ran, or None (gate)
        self.environment = environment()  # what the lanes, the hook and the carry-over checks run in
        self.measured = None  # the passing gate run of the last round, which its landed entries record
        self.alone = False  # whether this round's batch is one entry an option of eng land put on its own (choose)
        self.failed_lane = None  # the lane whose failure the last gate returned, or None (gate)
        self.confirmed = None  # the failed tests the last rerun confirmed, which blame reads (rerun)
        self.refused = None  # (lane, line, config) of the last gate eng unittest refused before running, for blame (rerun)
        self.unblamed = None  # {"key", "line"} of that refusal when blame found no one entry that brings it (blame)
        # {refusal_key: which gate} of the refusals that blamed no entry in the gates just before this one, the
        # previous gate's or a bisect's newer half beside it, which the same refusal again holds (older, Beside.weigh).
        self.refusals = {}
        self.blamed = False  # whether the last failed batch older settled sent back one entry by blame
        self.lane_now = None  # (lane, began, where) of the lane the gate runs or ran last (gate)
        self.stacked = None  # a Stack whose batch below landed, which the next round adopts as its own (after_stack)
        self.pending = None  # the Stack started on this round's batch while it gates (watched)
        self.gating = ()  # the IDs of the entries integrate last put on top, which the gate tests (Removal)
        self.withdrawn = []  # those of them that left the queue, which stopped the last gate (gate)
        self.role = "batch"  # what its gate is in the round's account (GateWatch): the batch, or a bisect's older half

    def say(self, words):
        print(f"queue: {time.strftime('%H:%M:%S')} {words}", flush=True)

    def progress(self):
        """What a worker that takes over from this one, on a newer toolkit or after a cause that cleared, goes on
        from: its bisect and its count of runs with no verdict. Every entry's state is on disk already."""
        progress = {"limit": self.limit, "suspects": self.suspects, "no_verdicts": self.no_verdicts}
        if self.unlocked: progress["unlocked"] = sorted(self.unlocked)
        return progress

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
                if looks > 1: self.queue.note(lambda state: state.update(waiting=None, waitingSince=None))
                return None
            kind, reason = busy
            if looks == 1:
                self.say(f"{reason}: {what} again once {where} settles, waiting up to {SETTLE_LIMIT}s")
                self.queue.note(lambda state: state.update(
                    waiting=f"{where} to settle before {what} again, since {reason}", waitingSince=time.time()))
            elif (kind, where) != seen or now() - reported >= SETTLE_REPORT:
                self.say(f"still waiting after {int(waited)}s: {reason}")
                reported = now()
            seen = kind, where
            if waited >= SETTLE_LIMIT:
                self.say(f"{where} did not settle in {int(waited)}s and {looks} looks{span}")
                self.queue.note(lambda state: state.update(waiting=None, waitingSince=None))
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
        # A stacked gate runs with the copy as it is: it is thrown away before the copy moves (vocaro-engineering#241).
        if self.stacked and text(copy, "rev-parse", "HEAD") != head:
            self.drop_stacked(f"the toolkit copy moves to {head[:12]}")
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
        self.drop_stacked("the worker moves to a newer toolkit")
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
        clean, holds no commit of its own (one landed through the queue, whose change origin/main holds under another
        SHA, is not), is behind, and no process started from it still runs there, since
        such a run would mix two toolkits (owner's decision, vocaro-engineering#104). Otherwise it is left as it
        is, and the log says why; nothing of this holds the queue. One left only for such a run is deferred, which
        catch_up tries again; quiet, for those tries, says only a fast-forward."""
        shared = self.queue.main_checkout
        self.deferred = None
        try:
            why = self.stays(shared)
            if why is None:
                before = text(shared, "rev-parse", "HEAD")
                # A commit made in the checkout and landed through the queue is on origin/main under another
                # SHA, so the checkout is not behind it but beside it: move main to origin/main, which holds
                # every change of its own (stays checked each by its patch).
                landed = git(shared, "merge-base", "--is-ancestor", before, MAIN).returncode != 0
                dropped = text(shared, "rev-list", "--count", f"{MAIN}..{before}") if landed else "0"
                moved = git(shared, "reset", "--keep", "--quiet", MAIN, hooks=False) if landed else \
                    git(shared, "merge", "--ff-only", "--quiet", MAIN, hooks=False)
                how = "`git reset --keep`" if landed else "`git merge --ff-only`"
                why = None if moved.returncode == 0 else f"{how} failed: {moved.stderr.strip() or 'no output'}"
                if why is None:
                    after = text(shared, 'rev-parse', 'HEAD')[:12]
                    self.say(f"moved {shared} from {before[:12]} to {after}, past {dropped} commit(s) of its own "
                             "that origin/main already holds by their changes" if landed else
                             f"fast-forwarded {shared} from {before[:12]} to {after}")
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
        if git(shared, "merge-base", "--is-ancestor", head, target).returncode and self.unlanded(shared, head):
            own = self.unlanded(shared, head)
            return (f"its HEAD {head[:12]} has commits origin/main lacks, whose changes it does not hold either: "
                    f"{len(own)}, such as {own[0][:12]}")
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

    @staticmethod
    def unlanded(shared, head):
        """The commits of head that origin/main lacks and whose change it does not hold either, by
        `git cherry`'s patch identity: what a commit made in the checkout and landed through the queue,
        which gives it another SHA, does not leave."""
        listed = text(shared, "cherry", MAIN, head).splitlines()
        return [line[2:] for line in listed if line.startswith("+ ")]

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
        holds the entries applied before it, which a conflict names too, so that its author knows what to wait
        for before rebasing (#130, conflict)."""
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
            hunks = self.hunks_of(conflicted)
            self.reset(before)
            self.conflicted = conflicted  # which Trial reads, to name the entries ahead that change those files
            self.hunks = hunks
            return self.conflict(commit, conflicted, ", ".join(conflicted) or picked.stderr.strip(), ahead)
        return None

    def conflict(self, commit, conflicted, files, ahead):
        """Why commit, of an entry behind ahead, did not apply in conflicted, files in words: what it conflicts with
        (vocaro-engineering#247). When no entry ahead changes a file in conflict, HEAD's files are origin/main's, so
        origin/main; when some do and origin/main holds those files as commit's parent did, those entries alone, by
        ID; otherwise origin/main and every entry ahead. only_ahead holds those entries in the second case alone, the
        ones `eng land --wait` waits for (vocaro-engineering#302)."""
        self.only_ahead = []
        if not ahead:
            return f"{commit[:12]} conflicts with origin/main, in {files}: rebase onto origin/main and add it again"
        touching = [other["id"] for other in ahead if set(conflicted) & set(text(
            self.checkout, "diff", "--name-only", "--no-renames", f"{other['commits'][0]}^", other["tip"]).splitlines())]
        if conflicted and not touching:
            return f"{commit[:12]} conflicts with origin/main, in {files}: rebase onto origin/main and add it again"
        if touching and not git(self.checkout, "diff", "--quiet", f"{commit}^", MAIN, "--", *conflicted).returncode:
            many, self.only_ahead = len(touching) > 1, touching
            return (f"{commit[:12]} conflicts with {', '.join(touching)}, ahead of it, not with origin/main, in {files}: "
                    f"once {'they land' if many else 'it lands'}, rebase onto origin/main and add it again")
        return (f"{commit[:12]} conflicts with what is ahead of it, origin/main and {', '.join(o['id'] for o in ahead)}, "
                f"in {files}: once those land, rebase onto origin/main and add it again")

    def hunks_of(self, conflicted):
        """What a conflict in progress leaves to name, before the reset: nothing for a round, whose send-back names the
        files; Trial names the hunks."""
        return []

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

    def integrate(self, batch, base, below=()):
        """The entries of batch that are now on top of base, in order; the rest are settled (left_out). below holds
        the entries that base already holds on top of origin/main, as for a stacked gate (Stack), which a conflict
        names as it does those of batch ahead of it. eng land's trial of a new entry runs this same code in a scratch
        checkout, settling nothing (Trial)."""
        self.reset(base)
        self.gating = ()
        applied = []
        for entry in batch:
            how = landing.contained(lambda *words: git(self.checkout, *words), base, entry["tip"])
            if how:
                self.left_out(entry, LANDED, landedAs=base, why=f"origin/main already holds it ({how})")
                continue
            why = self.pick(entry, [*below, *applied])
            if not why and self.kept: self.noted(entry, kept=self.kept)
            if why:
                self.say(f"{entry['id']}: {why}")
                self.left_out(entry, CONFLICT, why=why)
                continue
            applied.append(entry)
        self.gating = tuple(entry["id"] for entry in applied)
        return applied

    def left_out(self, entry, state, **fields):
        """What becomes of an entry that integrate does not put on top: the queue settles it."""
        self.queue.settle(entry, state, **fields)

    def noted(self, entry, **fields):
        """What integrate records of an entry it put on top, the changelog conflicts it settled: in the queue."""
        self.queue.record(entry, **fields)

    def gate(self, lanes, round_name, where=(), cancel=None):
        """(status of the first of lanes that did not pass or 0, its log). ran_on is the second Mac that ran
        that lane, by its dispatch's record (remote.dispatched), or None when it ran here or nowhere. A pass of
        the gate lane leaves its wall time and the parts its log reports in self.measured, for the gate's
        budget (budgets.py). where is eng run's placement option for each lane, HERE or BESIDE while a bisect's
        halves run at once (Beside), and none otherwise, so that eng run chooses. A set cancel ends the lane as its
        limit would (processes.Canceled), for a stacked gate thrown away (Stack). So does an entry it tests that leaves
        the queue, as `eng queue remove` takes it out, and the gate then returns WITHDRAWN, with self.withdrawn naming
        those entries (Removal, vocaro-engineering#247). lane_now is (the lane running, when it began, where), by which
        a gate stacked on this one goes to the other Mac (Worker.other_mac)."""
        log, self.ran_on, self.measured, self.timed_out, self.ran_where = None, None, None, None, ""
        self.failed_lane, self.withdrawn = None, []
        removal = Removal(self.queue, self.gating, cancel)
        for identifier in self.gating: self.queue.record({"id": identifier}, stopsWhenRemoved=REMOVAL_POLL)
        for lane in lanes:
            if removal.look():
                self.withdrawn = removal.gone
                self.say(f"{lane} not started, as {', '.join(removal.gone)} left the queue")
                return WITHDRAWN, log
            log = self.queue.logs / f"{round_name}-{lane}.log"
            # A lane run here logs to its own file, and one on the second Mac to its dispatch's run.log. What an
            # earlier round left in the lane's own file is not this one's.
            lane_log = self.checkout / ".build/engineering" / lane / "run.log"
            with contextlib.suppress(OSError): lane_log.unlink()
            self.say(f"{lane} on {self.head()[:12]}")
            began = time.time()  # wall clock, to match the dispatch records' start times
            started = time.monotonic()  # for the gate's measured duration
            self.lane_now = (lane, began, tuple(where))
            sampler = BusySampler(self.checkout, lane, began).start() if lane == budgets.GATE else None
            # A lane a locked screen left without a verdict goes only to a Mac whose screen reads unlocked (#293).
            screen = ["--unlocked-screen"] if lane in self.unlocked else []
            if screen: self.say(f"{lane}: only on a Mac whose screen reads unlocked, since a locked screen left its "
                                "last run without a verdict")
            command = ["python3", "scripts/eng", "run", *where, *screen, lane]
            if lane == SPELLING and not self.has_lane(lane):
                command = IMPLICIT_SPELLING
                self.say(f"{lane}: the project has no spelling lane, so the queue runs the toolkit's: "
                         f"{shlex.join(command)}")
            watch = GateWatch(self, lane, began, log).start()
            try:
                try:
                    status_ = processes.run(command, cwd=self.checkout, log_path=log, timeout=LANE_LIMIT,
                                            env={**self.environment, remote.QUEUE_GATE: "1"}, cancel=removal)
                finally: watch.finish()
            except processes.TimedOut as error:
                if sampler: sampler.stopped.set()
                self.unlocked.discard(lane)
                self.ran_on = remote.dispatched(self.checkout, lane, began)
                self.ran_where = self.mac(lane, began)
                if isinstance(error, processes.Canceled) and removal.gone:
                    self.withdrawn = removal.gone
                    self.say(f"{lane}: stopped{self.ran_where}, as {', '.join(removal.gone)} left the queue")
                    return WITHDRAWN, log
                self.timed_out = f"{lane} reached its limit: {error}"
                self.say(f"{lane}: unmeasured: {error}")
                return evidence.UNMEASURED, log
            except BaseException:
                if sampler: sampler.stopped.set()
                raise
            ended = time.monotonic()
            # It ran, unless no Mac admitted it or a host's configuration stopped it: it is placed as before next.
            if status_ not in UNRUN: self.unlocked.discard(lane)
            ran, self.ran_on = remote.dispatch(self.checkout, lane, began)
            # What the lane printed: here in its own file; on the second Mac in its dispatch's run.log, whose eng
            # unittest summary names the failed tests that rerun reruns alone.
            printed = lane_log if ran is None else ran / "run.log"
            with contextlib.suppress(OSError), log.open("a") as kept: kept.write(printed.read_text(errors="replace"))
            self.ran_where = self.mac(lane, began)
            busy = sampler.finish() if sampler else {}
            self.say(f"{lane} exited {status_}{self.placed(lane, began)}")
            if status_:
                self.failed_lane = lane
                return status_, log
            if lane == budgets.GATE:
                with contextlib.suppress(OSError):
                    self.measured = {"seconds": round(ended - started, 1),
                                     "parts": budgets.parts(log.read_text(errors="replace"))[:budgets.KEPT_PARTS], **busy}
                if self.measured: self.say(f"{lane} took {budgets.minutes(self.measured['seconds'])}; {budgets.busy_words(busy)}")
                said = self.results_named(lane, ran, began)
                if said:
                    with contextlib.suppress(OSError), log.open("a") as kept: kept.write(said + "\n")
                    self.say(said)
                    if self.measured: self.measured.update(results=said, log=str(log))
        return 0, log

    def has_lane(self, lane):
        """Whether the checkout's own engineering.json, which eng run reads, configures lane: one that does not runs
        the toolkit's check in its place (IMPLICIT_SPELLING)."""
        try: return lane in load(self.checkout).get("lanes", {})
        except (OSError, ValueError): return False

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

    def results_named(self, lane, ran, began):
        """The line that says where a passing lane's per-test results are, for its log and `eng land --wait`, naming
        them and copying none (vocaro-engineering#266); ran is the lane's dispatch, or None when it ran here. Results
        the second Mac wrote came back with its dispatch, so they are on this Mac; a dispatch that sent none back is
        said to have, and a lane run here that wrote none says nothing, as before."""
        if ran is not None:
            folder = ran / remote.RESULTS
            kept = result_files(folder)
            if not kept:
                return f"{lane} per-test results: none: the second Mac ({self.ran_on}) sent none back to {folder}, on this Mac"
            return (f"{lane} per-test results: {kept} in {folder}, on this Mac, sent back by the second Mac ({self.ran_on}); "
                    f"eng keeps the newest {remote.DISPATCHES_KEPT} dispatches of a lane")
        folder = self.checkout / ".build/engineering" / lane / "results"
        try:
            if folder.stat().st_mtime < int(began): return None  # an earlier run's
        except OSError:
            return None
        kept = result_files(folder)
        return f"{lane} per-test results: {kept} in {folder}, on this Mac; its next run here replaces them" if kept else None

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

    def push(self, config, batch, base, docs=None, ungated=None, flakes=None):
        """Push HEAD to main. True on a push, False to gate again; a refusal that is no race holds, as a remote
        that stays out of reach does.
        The project's pre-push hook runs here rather than inside the push, unless main's queue.hookCoveredBy
        says which passed lanes cover it: a long hook then holds no connection open, and its refusal is its own.
        A docs-only batch, whose lanes docs names, skips it too: the hook is part of lanes that read none of the
        batch's paths. The landed entries record docs, since such a landing is no gate of the pushed commit.
        An ungated landing, whose lanes ungated names, runs the hook unless those lanes cover it, which the gate's then
        do and it skips; its entry records ungated, and its tip carries the UNGATED trailer, again after a carry-over.
        A landing whose gate failed only flaky tests (rerun) carries a FLAKY trailer for each, again after a carry-over.
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
                if flakes: head = self.mark_flaky(flakes, base)
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
                if not docs and not ungated: self.keep_app(head)
                if self.toolkits_own(): self.follow()
                return True
            said = pushed.stderr.strip()
            if "rejected" in said and ("fetch first" in said or "non-fast-forward" in said): continue
            if unreached(said):
                raise Hold(CLEARS, f"the push of {head[:12]} did not reach the remote in {len(BACKOFF) + 1} tries: {said}")
            raise Hold(OWNER, f"the push of {head[:12]} was refused: {said or 'no output'}{status}")
        return False

    def keep_app(self, head):
        """Start keeping the app built at head, the gated commit just pushed, in the background, when head's
        engineering.json keeps one after landing (app_builds.py): the round goes on while it builds, and a build
        that fails leaves the build kept before it. A docs-only or ungated landing keeps none. Head's settings, not
        main's at the batch's base as the gate's: keeping a build gates nothing, and so the commit that first gives
        `app` keeps its build too (Pagepour's, 2026-11-06, kept none)."""
        try:
            config = config_at(self.checkout, head)
        except ValueError as error:
            self.say(f"keeping no app build of {head[:12]}: {error}")
            return
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

    def rerun(self, config, applied, base, head, log, round_name):
        """The flakes a batch whose gate failed lands with, or None to bisect it as before (owner's decision of
        2026-11-07, vocaro-engineering#231). When the failed lane is eng unittest and its log names the failed tests,
        the worker waits for this Mac, where they run, to settle as for a retry, and reruns each alone on head up to
        RERUNS times. A rerun that fails makes the failure real. When every rerun passes, each test is a flake,
        recorded in the entries and on the tracker (hardening), and the batch lands only when none of its commits
        changes a file the test's module is or imports (imported_files); otherwise it bisects. In a batch of several
        entries, a rerun that fails goes on to rerun the other failed tests, so that blame can weigh them all."""
        self.confirmed = self.refused = None
        lane = self.failed_lane
        command = rerun_command(config.get("lanes", {}).get(lane, {}).get("command")) if lane else None
        if command is None: return None
        try: said = log.read_text(errors="replace")
        except OSError as error:
            self.say(f"{lane}'s log cannot be read, so no failed test is rerun: {error}")
            return None
        tests, why = failed_tests(said)
        refusals = REFUSAL.findall(said)
        if tests is None and refusals and not UNITTEST_SUMMARY.findall(said):
            # A refusal is no test failure, and the same tree gives it again: blame reads it, with no rerun (#290).
            self.refused = (lane, refusals[-1], config)
            self.say(f"{lane} ran nothing, as eng unittest refused it before running anything, which no rerun would "
                     f"change: {refusals[-1]}")
            return None
        if tests is None:
            self.say(f"{lane} failed with no failed test to rerun alone ({why}): bisecting")
            return None
        if len(tests) > RERUN_MOST:
            self.say(f"{lane} failed {len(tests)} tests, more than the {RERUN_MOST} a flake would: bisecting")
            return None
        self.say(f"{lane} failed {len(tests)} test{'' if len(tests) == 1 else 's'} at {head[:12]}: {', '.join(tests)}; "
                 f"rerunning each alone up to {RERUNS} times on that commit before a bisect")
        busy = self.settle("the failed tests run alone")
        if busy:
            self.say(f"the failed tests are not rerun, as {busy}: bisecting")
            return None
        if self.head() != head: self.reset(head)
        flakes, real = [], {}
        for number, test in enumerate(tests, 1):
            logs = []
            for attempt in range(1, RERUNS + 1):
                path = self.queue.logs / f"{round_name}-rerun-{number}-{attempt}.log"
                logs.append(str(path))
                try: status_ = processes.run([*command, test], cwd=self.checkout, log_path=path, timeout=LANE_LIMIT,
                                             env=self.environment)
                except processes.TimedOut as error: status_ = f"unmeasured, {error}"
                except OSError as error: status_ = f"not started, {error}"
                if status_:
                    self.say(f"{test} failed again alone, on rerun {attempt} (exit {status_}), so the failure is real; "
                             f"see {path}")
                    # Only a rerun that ran and failed confirms the failure, whose output blame reads.
                    real[test] = path if isinstance(status_, int) else None
                    break
            if test in real:
                if len(applied) == 1: return None
                continue
            flakes.append({"test": test, "commit": head, "gateLog": str(log), "rerunLogs": logs,
                           "mac": offload_host.mac_name()})
        if real:
            if flakes:
                self.say(f"{', '.join(flake['test'] for flake in flakes)} passed alone beside the real failure, so "
                         "no commit is blamed")
            elif None in real.values():
                self.say("a rerun ended with no verdict, so no commit is blamed")
            else:
                self.confirmed = {"tests": real, "top": self.suite_top(command)}
            return None
        changed = {path for path in text(self.checkout, "log", "--format=", "--name-only", "--no-renames",
                                         f"{base}..{head}").splitlines() if path}
        top = self.suite_top(command)
        touching = {}
        for flake in flakes:
            files = imported_files(self.checkout, top, flake["test"])
            reached = sorted(changed & set(files)) if files is not None else None
            if reached is None: touching[flake["test"]] = "whose module the worker cannot find"
            elif reached: touching[flake["test"]] = f"which changes {', '.join(reached[:5])}, which it imports"
        for flake in flakes:
            flake["outcome"] = ("The batch was bisected, as one of its commits changes the module of a failed test, or "
                                "a file it imports." if touching else
                                f"No commit of the batch's {len(applied)} changes its module or a file it imports, so "
                                "the batch landed.")
            flake["issue"], flake["trackerError"] = hardening(self.checkout, flake)
            if flake["issue"] is None: del flake["issue"]
            else: del flake["trackerError"]
            self.say(f"flaky: {flake_words(flake)}; "
                     + (f"tracker: {flake['issue']}" if "issue" in flake else f"the tracker has no record: {flake['trackerError']}"))
        for entry in applied:
            current = self.queue.read(entry["id"]) or entry
            self.queue.record(entry, flakes=current.get("flakes", []) + flakes)
        if touching:
            self.say(f"every failed test passed alone, but the batch reaches {'; '.join(f'{test}, {why}' for test, why in touching.items())}: bisecting")
            return None
        self.say(f"every failed test passed alone {RERUNS} times and no commit of the batch changes its module or a file "
                 "it imports: landing the batch")
        return flakes

    def blame(self, applied):
        """(the one entry of applied that every failed test the last rerun confirmed reaches, why), to send back while
        the rest gate again at once, sparing the bisect's gates; or None to bisect. A test reaches an entry whose
        commits change a path its failing rerun's output names, as `eng spelling`'s `PATH:LINE:` lines do, or a file
        its module is or imports (imported_files). Any doubt bisects: no confirmed failure, a module the worker cannot
        find, a test that reaches no entry or several, or tests that reach different entries. A gate eng unittest
        refused before running anything is blamed by its refusal instead (blame_refusal)."""
        confirmed, self.confirmed = self.confirmed, None
        refused, self.refused, self.unblamed = self.refused, None, None
        if refused: return self.blame_refusal(applied, *refused)
        if not confirmed or len(applied) < 2: return None
        changes = {entry["id"]: self.changed_by(entry) for entry in applied}
        prefix = f"{self.checkout.resolve()}/"
        found, whys = set(), []
        for test, path in confirmed["tests"].items():
            try: output = Path(path).read_text(errors="replace").replace(prefix, "").replace(f"{self.checkout}/", "")
            except OSError as error:
                self.say(f"the rerun log of {test} cannot be read, so no commit is blamed: {error}; bisecting")
                return None
            files = imported_files(self.checkout, confirmed["top"], test)
            if files is None:
                self.say(f"the worker cannot find the module of {test}, so no commit is blamed: bisecting")
                return None
            reached = {}
            for identifier, changed in changes.items():
                named = sorted(changed_path for changed_path in changed if named_in(changed_path, output))
                imported = sorted(changed & set(files))
                if named or imported:
                    reached[identifier] = (f"its failure names {', '.join(named[:5])}" if named else
                                           f"it imports {', '.join(imported[:5])}")
            if len(reached) != 1:
                self.say(f"{test} reaches {'no entry' if not reached else f'{len(reached)} entries'} of the batch's "
                         f"{len(applied)}" + (f" ({'; '.join(f'{key}: {why}' for key, why in reached.items())})"
                                              if reached else "") + ", so no commit is blamed: bisecting")
                return None
            [(identifier, why)] = reached.items()
            found.add(identifier)
            whys.append(f"{test}, which failed alone, as {why}, which only {identifier} changes")
        if len(found) != 1:
            self.say(f"the failed tests reach different entries ({'; '.join(whys)}), so no commit is blamed: bisecting")
            return None
        [identifier] = found
        entry = next(entry for entry in applied if entry["id"] == identifier)
        why = "; ".join(whys)
        self.say(f"blaming {identifier} ({entry.get('subject') or entry['tip'][:12]}): {why}; sending it back and "
                 f"gating the other {len(applied) - 1} at once instead of bisecting")
        return entry, why

    def changed_by(self, entry):
        """The paths entry's commits change, each by its name before and after a move."""
        return {path for path in text(self.checkout, "log", "--no-walk", "--format=", "--name-only", "--no-renames",
                                      *entry["commits"]).splitlines() if path}

    def blame_refusal(self, applied, lane, line, config):
        """(the one entry of applied that brings line, eng unittest's refusal of lane's gate before it ran anything,
        why), to send back while the rest gate again at once; or None, with unblamed set, to bisect (owner's decision
        of 2026-11-08, vocaro-engineering#290). A refusal is no test failure: the same tree gives it again, so no rerun
        confirms it. An entry brings it when its tree alone gives a refusal naming one of its modules (refused_alone),
        or its commits change one of them in the suite's directory (#280). Any doubt bisects: a refusal that names
        no module, no entry or several that bring it, or a tree eng unittest's checks cannot read."""
        modules = refused_modules(line)
        suite = suite_of(config.get("lanes", {}).get(lane, {}).get("command"))
        folder = os.path.normpath(suite[0]) if suite else None
        reached, doubt = {}, None
        for entry in applied if modules else []:
            try: own = refused_alone(self.queue.main_checkout, entry["tip"], config)
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
                doubt = f"eng unittest's checks cannot read {entry['id']}'s tree: {error}"
                break
            alone = any(refused_modules(said) & modules for said in own)
            named = sorted(path for path in self.changed_by(entry)
                           if Path(path).name in modules and os.path.normpath(os.path.dirname(path)) == folder)
            reasons = (["its tree alone is refused so"] if alone else []) + ([f"it changes {', '.join(named)}"]
                                                                              if named else [])
            if reasons: reached[entry["id"]] = " and ".join(reasons)
        if doubt or len(reached) != 1:
            self.unblamed = {"key": refusal_key(line), "line": line}
            if doubt: because = doubt
            elif not modules: because = "the refusal names no test module"
            else:
                because = (f"{'no entry' if not reached else f'{len(reached)} entries'} of the batch's {len(applied)} "
                           + ("bring it" if len(reached) > 1 else "brings it")
                           + (f" ({'; '.join(f'{key}: {why}' for key, why in reached.items())})" if reached else ""))
            self.say(f"eng unittest refused {lane} before running anything, and {because}, so no commit is blamed")
            return None
        [(identifier, reason)] = reached.items()
        entry = next(entry for entry in applied if entry["id"] == identifier)
        why = (f"eng unittest refused {lane} before running anything, which no rerun would change, and only "
               f"{identifier} brings the refusal, as {reason}: {line}")
        rest = len(applied) - 1
        self.say(f"blaming {identifier} ({entry.get('subject') or entry['tip'][:12]}): {why}; sending it back"
                 + (f" and gating the other {rest} at once instead of bisecting" if rest else ""))
        return entry, why

    def send_back(self, applied, log, entry, why):
        """Settle entry, the one of applied that blame found, as failed, and queue the rest to gate again at once."""
        self.queue.settle(entry, FAILED, why=f"the queue's lanes fail on this commit on top of origin/main: {why}. Fix "
                                            "it and add it again", log=str(log))
        self.queue.mark([other for other in applied if other["id"] != entry["id"]], QUEUED)

    def suite_top(self, command):
        """The top-level directory of the suite command runs, as eng unittest takes it: -t, else -s, else the checkout."""
        found = {}
        for index, word in enumerate(command):
            name, equals, value = word.partition("=")
            if name in ("-s", "--start-directory", "-t", "--top-level-directory"):
                found[name.lstrip("-")[0]] = value if equals else (command[index + 1] if index + 1 < len(command) else ".")
        return self.checkout / found.get("t", found.get("s", "."))

    def mark_flaky(self, flakes, base):
        """Add a FLAKY trailer for each flake to HEAD, the tip of a batch that lands with them, and return the new
        HEAD, as mark_ungated does; a HEAD at base stays."""
        if self.head() == base: return base
        trailers = [word for flake in flakes for word in ("--trailer", f"{FLAKY}: {flake_words(flake)}"
                                                          + (f"; {flake['issue']}" if flake.get("issue") else ""))]
        git(self.checkout, "commit", "--amend", "--no-verify", "--no-edit", "--allow-empty", *trailers, hooks=False,
            check=True)
        return self.head()

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
        waiting = queue.waiting()
        if self.stacked:
            why = self.stacked.stale(waiting)
            if why: self.drop_stacked(why)
        batch = self.stacked.entries if self.stacked else self.choose(waiting)
        if not batch: return False
        passed = [entry for entry in batch if entry["state"] == PASSED] if not self.stacked else []
        reach(queue.main_checkout, *FETCH, say=self.say, check=True)
        self.prepare()
        self.toolkit()
        if self.stacked: return self.adopted()
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
        beside = self.beside(waiting, batch, base, config, docs, ungated)
        self.role = "older half" if beside else "batch"
        self.begun(round_name, head, base, applied, beside)
        if beside: result, log = beside.both(self, lanes_of(config), round_name)
        elif self.alone or docs or ungated or self.limit:
            result, log = self.gate(ungated["lanes"] if ungated else docs or lanes_of(config), round_name)
        else:
            lanes = lanes_of(config)
            result, log = self.watched(self, config, lanes, base, head, applied,
                                       lambda: self.gate(lanes, round_name))
        self.prune()
        if not beside:
            outcome = self.older(config, applied, base, head, docs, ungated, result, log, round_name)
            self.after_stack(outcome, self)
            return True
        flakes = None
        if result and result not in NO_VERDICT and result != WITHDRAWN:
            flakes = self.rerun(config, applied, base, head, log, round_name)
        older_passes = not result or bool(flakes)
        # What passed beside lands at once while the older half bisects on; after the older half's push when both
        # passed, since the older go first when both can (Beside.weigh).
        if not older_passes: beside.weigh(config, base, older_landed=False)
        outcome = self.older(config, applied, base, head, docs, ungated, result, log, round_name, flakes, reran=True)
        if older_passes: beside.weigh(config, base, older_landed=outcome == LANDED)
        self.after_beside(beside, applied, outcome)
        return True

    def begun(self, round_name, head, base, applied, beside=None, adopted=False):
        """Start the round's account (ROUND_FILE): its batch, and, while bisecting, how many entries the bisect
        suspects and the size of each half, the newer 0 while it waits for the older. The gates still running, a
        stacked one among them, keep their records."""
        bisect = None
        if self.limit:
            bisect = {"suspects": self.suspects, "older": len(applied), "newer": len(beside.newer) if beside else 0}
        def change(state):
            state.update(round=round_name, started=time.time(), head=head, base=base, pid=os.getpid(),
                         entries=[entry["id"] for entry in applied], bisect=bisect, adopted=adopted, waiting=None,
                         waitingSince=None)
            state.setdefault("gates", {})
        self.queue.note(change)

    def older(self, config, applied, base, head, docs, ungated, result, log, round_name, flakes=None, reran=False,
              runner=None):
        """What becomes of applied, the round's batch, or a bisect's older half, after its lanes exited result:
        NO_VERDICT_OUTCOME after no verdict, FAILED once bisected or sent back, QUEUED when it gates again, and
        LANDED once pushed. reran says that rerun has already been asked, and flakes is what it found. runner is
        the checkout's worker that gated it, this one's own unless it was a stacked gate (Stack)."""
        queue = self.queue
        runner = runner or self
        self.blamed = False
        # Only a refusal that blames no entry is remembered, for the next gate; any other outcome ends the row.
        before, self.refusals = self.refusals, {}
        if result == WITHDRAWN:
            self.say(f"{', '.join(runner.withdrawn)} left the queue, which stopped the gate: gating what is left")
            queue.mark(applied, QUEUED)
            return QUEUED
        if result in NO_VERDICT:
            queue.mark(applied, QUEUED)
            # This worker exits, as before a round, so the run counts toward no bound (vocaro-engineering#300). The
            # worker's own answer decides, so that a lane that merely exits with this status cannot end worker after
            # worker.
            if result == remote.LOST_ACCESS: self.connected()
            locked = runner.screen_locked(result, log)
            if locked:
                # The owner's decision of 2026-11-08 (vocaro-engineering#293): a run a locked screen voided does not
                # count toward the hold, and the lane's next run goes only to a Mac whose screen reads unlocked.
                lane, reading = locked
                self.unlocked.add(lane)
                self.say(f"no verdict (exit {result}){runner.ran_where}: {lane} ran behind a locked screen, the last "
                         f"reading {reading}, which queue.noVerdictRuns does not count; its next run goes only to a "
                         "Mac whose screen reads unlocked")
                busy = self.settle("the lanes run", runner.ran_on)
                if busy: queue.hold(worded(SESSION, f"{lane} ran behind a locked screen, and {busy}; see {log}"))
                return NO_VERDICT_OUTCOME
            self.no_verdicts += 1
            runs = no_verdict_runs(config)
            gave = (f"the lanes gave no verdict in {self.no_verdicts} run{'' if self.no_verdicts == 1 else 's'} in a "
                    f"row, the last with exit {result}{runner.ran_where}, {runner.no_verdict_cause(result, log)}")
            # A shared host's configuration is the owner's; a host that stays busy, or tests left unmeasured, a
            # session reads, and resumes once the host has settled or the cause is gone.
            kind = OWNER if result == remote.MISCONFIGURED else SESSION
            if self.no_verdicts >= runs:
                queue.hold(worded(kind, f"{gave}, the most queue.noVerdictRuns allows ({runs}); see {log}"))
                return NO_VERDICT_OUTCOME
            self.say(f"no verdict (exit {result})" + (f" from {runner.ran_on}" if runner.ran_on else "")
                     + f", run {self.no_verdicts} of at most {runs}")
            # Every retry waits, so that none starts into the load that voided the run before it.
            busy = self.settle("the lanes run", runner.ran_on)
            if busy: queue.hold(worded(kind, f"{gave}, and {busy}; see {log}"))
            return NO_VERDICT_OUTCOME
        self.no_verdicts = 0
        if result:
            # A failure that a rerun alone does not repeat lands, when the batch cannot reach the test (#231).
            if not reran: flakes = runner.rerun(config, applied, base, head, log, round_name)
            if not flakes:
                blamed = runner.blame(applied)
                if blamed:
                    self.send_back(applied, log, *blamed)
                    if not self.alone: self.limit, self.suspects = None, 0
                    self.blamed = True
                    return FAILED
                if runner.unblamed and runner.unblamed["key"] in before:
                    return self.refused_again(applied, runner.unblamed["line"], before[runner.unblamed["key"]], log)
                if runner.unblamed: self.refusals[runner.unblamed["key"]] = "the gate before it"
                self.failed(applied, log)
                return FAILED
        # An entry removed during the gate was part of what passed, so what is left has not been tested.
        current = [queue.read(entry["id"]) for entry in applied]
        if any(entry is None or entry["state"] not in WAITING for entry in current):
            self.say("an entry was removed during the run: gating what is left")
            queue.mark(applied, QUEUED)
            return QUEUED
        if ungated: head = runner.mark_ungated(ungated, base)
        if flakes: head = runner.mark_flaky(flakes, base)
        runner.keep(applied, base, head, docs, ungated, flakes)
        if runner.push(config, applied, base, docs, ungated, flakes):
            self.passed(len(applied))
            return LANDED
        queue.mark(applied, QUEUED)
        return QUEUED

    def refused_again(self, applied, line, other, log):
        """Hold the queue for a session, applied queued again, when eng unittest refused this gate before running
        anything as it refused other, the gate before it or a bisect's half beside it, and no one entry brings the
        refusal: a bisect would only send back, one by one, entries the refusal does not name, while the cause, on
        origin/main or in how entries combine, stays (owner's decision of 2026-11-08, vocaro-engineering#290). The
        hold's reason, which `eng queue status` and the worker log show, carries `eng queue doctor`'s diagnosis."""
        self.queue.mark(applied, QUEUED)
        if not self.alone: self.limit, self.suspects = None, 0
        try:
            findings = diagnose(self.queue.main_checkout)[0]
            diagnosis = "; ".join(f"{finding['signal']}: {finding['diagnosis']}; next: {finding['remedy']}"
                                  for finding in findings) or "healthy"
        except Exception as error:  # the diagnosis explains the hold, and never stops it
            diagnosis = f"it could not run: {error}"
        self.queue.hold(worded(SESSION, f"eng unittest refused this gate and {other} before running anything, the "
                                        f"same way, and no one entry of the batch brings the refusal, so a bisect "
                                        f"would only send back entries it does not name: {line}; see {log}; eng queue "
                                        f"doctor: {diagnosis}"))
        return QUEUED

    def adopted(self):
        """This round's batch is the Stack gated on the tip of the batch that landed before it: it gates on, with
        the entries that arrived meanwhile stacked on its tip in turn, and its result settles it as any round's
        does. One whose base is no longer origin/main, or whose lanes main's configuration no longer names, is
        thrown away, its entries queued again."""
        stacked, self.stacked = self.stacked, None
        base = text(self.queue.main_checkout, "rev-parse", MAIN)
        if base != stacked.base:
            stacked.drop(f"origin/main is {base[:12]}, not the tip {stacked.base[:12]} it was gated on")
            return True
        try:
            config = config_at(self.queue.main_checkout, base)
            lanes = lanes_of(config)
        except ValueError as error:
            stacked.drop(f"main's configuration at {base[:12]} cannot be read: {error}")
            return True
        if lanes != stacked.lanes:
            stacked.drop(f"main at {base[:12]} names the lanes {', '.join(lanes)}, not those it ran")
            return True
        applied = [self.queue.read(entry["id"]) or entry for entry in stacked.entries]
        self.alone = False
        self.say(f"testing {len(applied)} entr{'y' if len(applied) == 1 else 'ies'} at {stacked.tip[:12]} on "
                 f"{base[:12]}, whose gate began on top of the batch that just landed")
        self.begun(stacked.name, stacked.tip, base, applied, adopted=True)
        result, log = self.watched(stacked.runner, config, lanes, base, stacked.tip, applied, stacked.wait)
        self.prune()
        outcome = self.older(config, applied, base, stacked.tip, None, None, result, log, stacked.name,
                             runner=stacked.runner)
        self.after_stack(outcome, stacked.runner)
        return True

    def watched(self, runner, config, lanes, base, head, applied, gate):
        """gate(), the lanes of applied at head in runner's checkout, while a thread looks every STACK_POLL for
        entries that arrive behind them and gates them stacked on head in the other checkout, on the other Mac
        (stack), which after_stack keeps or throws away."""
        self.pending = None
        stop, said = threading.Event(), set()
        def stacker():
            while not stop.wait(STACK_POLL):
                try: self.stack(runner, config, lanes, head, applied, stop, said)
                except Exception as error:  # a stacked gate is a saving, never the round's verdict
                    self.say(f"no gate stacked on {head[:12]}: {error}")
                    return
        thread = threading.Thread(target=stacker, name="stacker", daemon=True)
        thread.start()
        try: return gate()
        finally:
            stop.set()
            thread.join()

    def stack(self, runner, config, lanes, head, applied, stop, said):
        """Start self.pending, the waiting entries not in applied, the batch at head, gated stacked on head, when
        there are any and none is pending: on the Mac runner's lane does not run on (other_mac), in the checkout it
        does not use. One the second Mac declined or left with no verdict, or one an entry's removal stopped, is
        thrown away, for a later look to start again. An entry eng land put first, or that it lands without the gate,
        starts none: it goes first, on main; nor does a batch an entry of which has left the queue, as its gate stops
        (Worker.gate, vocaro-engineering#247)."""
        if self.pending is not None:
            if not self.pending.done(): return
            why = self.pending.void()
            if why is None: return
            self.pending.drop(why)
            self.pending = None
        ids = {entry["id"] for entry in applied}
        waiting = self.queue.waiting()
        if not ids <= {entry["id"] for entry in waiting}: return
        fresh = [entry for entry in waiting if entry["id"] not in ids]
        if not fresh or any(entry.get("front") or entry.get("noGate") or entry["state"] != QUEUED for entry in fresh):
            return
        where = self.other_mac(runner)
        why = offerable(config, lanes) if where == BESIDE else None
        if why:
            if why not in said: self.say(f"the entries behind the batch wait for it, since {why}")
            said.add(why)
            return
        other = Beside(self, self.queue.checkout if runner.checkout != self.queue.checkout else None, "stacked",
                       settles=False)
        stacked = Stack(self, other, fresh, head, lanes, where, below=applied)
        if not stop.is_set() and stacked.begin(): self.pending = stacked

    def other_mac(self, runner):
        """The placement option of eng run for a gate stacked on runner's: HERE when runner's lane runs on the
        second Mac, by its option or the dispatch record it left (remote.dispatched), else BESIDE."""
        lane, began, where = runner.lane_now or (None, time.time(), ())
        if where and where == BESIDE: return HERE
        if where and where == HERE: return BESIDE
        return HERE if lane and remote.dispatched(runner.checkout, lane, began) else BESIDE

    def after_stack(self, outcome, runner):
        """Keep the pending Stack for the next round when the batch below it landed at exactly the tip it was
        stacked on, and the queue goes on; else throw it away."""
        pending, self.pending = self.pending, None
        if pending is None: return
        if pending.done() and pending.void():
            pending.drop(pending.void())
        elif outcome == LANDED and runner.head() == pending.base and not self.queue.held():
            self.say(f"the gate stacked on {pending.base[:12]} goes on as the next round, as that is origin/main now")
            self.stacked = pending
        else:
            pending.drop("the batch below it did not land at the tip it was stacked on")

    def drop_stacked(self, why):
        """Throw away every stacked gate, its entries queued again: before the worker moves or stops."""
        for name in ("pending", "stacked"):
            stacked = getattr(self, name, None)
            setattr(self, name, None)
            if stacked is not None: stacked.drop(why)

    def beside(self, waiting, batch, base, config, docs, ungated):
        """The Beside that gates a bisect's newer half while batch, its older half, gates here, or None to gate batch
        alone, as before: no bisect is going on, batch is an entry an option put on its own, a docs-only or ungated
        round, a newer entry an option of eng land marked, a lane that runs only here (offerable), or a newer entry
        that does not apply to origin/main without the older half, which waits for it (vocaro-engineering#241)."""
        if self.alone or docs or ungated or not self.limit or self.suspects <= len(batch): return None
        rest = waiting[len(batch):self.suspects]
        if not rest or any(entry.get("front") or entry.get("noGate") or entry["state"] == PASSED for entry in rest):
            return None
        why = offerable(config, lanes_of(config))
        if why:
            self.say(f"the newer {len(rest)} of the bisect wait for the older half, since {why}")
            return None
        other = Beside(self)
        other.prepare()
        newer = other.integrate(rest, base)
        if other.unreachable:
            self.say(f"the newer {len(rest)} of the bisect wait for the older half, since "
                     f"{', '.join(entry['id'] for entry in other.unreachable)} does not apply to origin/main without it")
            return None
        if not newer: return None
        other.newer, other.tip = newer, other.head()
        self.queue.mark(newer, TESTING)
        self.say(f"testing the newer {len(newer)} entr{'y' if len(newer) == 1 else 'ies'} of the bisect at "
                 f"{other.tip[:12]} on {base[:12]} beside them, on the second Mac if it can start them now")
        return other

    def after_beside(self, beside, applied, outcome):
        """The bisect's next step once both halves are weighed, outcome the older half's (Worker.older) and
        beside.outcome the newer's (Beside.weigh): the newer half alone, halved, when only it fails; the older half,
        halved, when the newer landed beside it and the older gave no verdict; no bisect once the half that failed
        has been sent back, alone or by blame, or both passed. Otherwise, as the older half's outcome left it."""
        older, newer = len(applied), len(beside.newer)
        if beside.outcome == FAILED and outcome != FAILED:
            # The one newer entry was sent back, which explains the failure: the older half goes on unsuspected.
            self.limit, self.suspects = None, 0
        elif beside.outcome == BISECT and (outcome == LANDED or (outcome == FAILED and (older == 1 or self.blamed))):
            self.suspects, self.limit = newer, max(1, newer // 2)
        elif beside.outcome == LANDED and outcome in (NO_VERDICT_OUTCOME, QUEUED):
            self.suspects, self.limit = older, max(1, older // 2)
        elif beside.outcome == PASSED and outcome == LANDED:
            self.limit, self.suspects = None, 0

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
        tests its log reports unmeasured, else the error its receipt records (#288), no admission, or a shared host's
        configuration."""
        if self.timed_out: return f"unmeasured: {self.timed_out}"
        if result == evidence.UNMEASURED:
            try: said = log.read_text(errors="replace") if log else ""
            except OSError: said = ""
            found = evidence.unmeasured_tests(said)
            if not found:
                error = self.receipt_error()
                return f"unmeasured: {shortened(error)}" if error else "unmeasured, with no test its log names"
            first = shortened(found[0])
            # The run's last reading of its screen, which eng writes for a lane whose result depends on it.
            screens = SCREEN_LINE.findall(said)
            return (f"unmeasured: {len(found)} test{'' if len(found) == 1 else 's'}, the first {first}"
                    + (f"; the last reading {screens[-1].strip()}" if screens else ""))
        if result == resources.BUSY: return "no admission: the host stayed at capacity"
        return "a shared host's configuration stopped it"

    def receipt_error(self):
        """The error this run's receipt records for the lane the last gate stopped at, on one line, or None when no
        receipt written since the lane began can be read or says one: the dispatch's, for a run on the second Mac,
        else the one here. A dispatch's holds the lane's own receipt from that Mac, and that Mac's error, under
        result (remote.execute)."""
        if not self.lane_now: return None
        lane, began = self.lane_now[0], self.lane_now[1]
        ran = remote.dispatch(self.checkout, lane, began)[0]
        paths = ([ran / "source.json"] if ran is not None else []) \
            + [self.checkout / ".build/engineering" / lane / "source.json"]
        for path in paths:
            try:
                if path.stat().st_mtime < began: continue
                receipt = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            there = receipt.get("result") if isinstance(receipt, dict) else None
            for said in (there.get("receipt") if isinstance(there, dict) else None, there, receipt):
                error = said.get("error") if isinstance(said, dict) else None
                if isinstance(error, str) and error.strip(): return " ".join(error.split())
        return None

    def screen_locked(self, result, log):
        """(the lane, the screen reading) when the lane at which the last gate stopped with result, which gave no
        verdict, ran behind a locked screen; otherwise None. The last reading decides: the receipt's screenAfter,
        which eng writes when a lane that depends on the screen ends unmeasured, else the log's last `screen:`
        line. A dispatch's receipt holds the lane's own, and so that reading, under result. No admission and a
        host's configuration ran nothing, and count as before (#185, #293)."""
        if result in UNRUN or not self.lane_now: return None
        lane, began = self.lane_now[0], self.lane_now[1]
        ran = remote.dispatch(self.checkout, lane, began)[0]
        receipts = ([(ran / "source.json", "the second Mac", ("result", "receipt"))] if ran is not None else []) \
            + [(self.checkout / ".build/engineering" / lane / "source.json", HERE_MAC, ())]
        for receipt, mac, within in receipts:
            try:
                if receipt.stat().st_mtime < began: continue
                after = json.loads(receipt.read_text())
                for key in (*within, "screenAfter"): after = after.get(key)
            except (OSError, ValueError, AttributeError):
                continue
            if isinstance(after, dict) and after.get("state"):
                if after["state"] != "locked": return None
                return lane, offload_host.screen_line(after, f"{mac} ({after.get('mac') or 'unnamed'})")
        try: said = log.read_text(errors="replace") if log else ""
        except OSError: said = ""
        screens = SCREEN_LINE.findall(said)
        return (lane, screens[-1].strip()) if screens and screens[-1].startswith("screen: locked") else None

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

    def keep(self, batch, base, head, docs, ungated=None, flakes=None):
        """Record a batch's pass before its push. A hold the push meets, a refusal or a remote out of reach, then
        leaves the batch passed rather than queued, and `eng queue resume` pushes head instead of gating the same
        tree again (vocaro-engineering#150): EPUBLib's queue spent an hour of the second Mac doing that."""
        verdict = {"head": head, "base": base, "batch": [entry["id"] for entry in batch], "at": time.time()}
        if docs: verdict["docsOnly"] = docs
        if ungated: verdict["ungated"] = ungated
        if flakes: verdict["flakes"] = flakes
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
        flakes = verdict.get("flakes")
        config = self.settings(base, head)
        self.measured = verdict.get("gate")
        self.alone = bool(verdict.get("alone"))
        self.say(f"{len(batch)} entr{'y' if len(batch) == 1 else 'ies'} passed at {head[:12]} on {base[:12]}: "
                 "pushing without gating again")
        if self.push(config, batch, base, docs, ungated, flakes): self.passed(len(batch))
        else: self.queue.mark(batch, QUEUED)
        return True

    def prune(self):
        logs = sorted(self.queue.logs.iterdir(), key=lambda path: path.stat().st_mtime)
        for path in logs[:-KEPT_LOGS]:
            with contextlib.suppress(OSError): path.unlink()

    def drain(self):
        try:
            while not self.queue.held():
                self.connected()
                more = self.round()
                self.arrived()
                if not more: return
                self.rounds += 1
        finally:
            self.drop_stacked("the worker stops")
            with contextlib.suppress(OSError): self.queue.round_file.unlink()

    def connected(self):
        """Raise NoLocalNetwork, before a round, when a second Mac is configured and macOS denies this process the
        local network, as it does once the Claude session that started the worker ends: every gate would then go to
        this Mac, the second Mac seeming away to this process alone, as from 13:48 to 16:28 on 2026-11-07
        (vocaro-engineering#282). A worker started by the next live session reaches it."""
        if remote.configured() is None: return
        lost = remote.local_network_lost()
        if lost: raise NoLocalNetwork(lost)


# What Beside.weigh records of a newer half that failed with several entries, which bisect once the older half has.
BISECT = "bisect"


class Beside(Worker):
    """A bisect's newer half, gated in a checkout beside the worker's, on the second Mac when it can start it now,
    while the older half gates on this Mac (owner's decision of 2026-11-07, vocaro-engineering#241). Both halves run
    on the same origin/main. What passes beside lands while the older half bisects on; when both pass, the older lands
    first and the newer gates again on the main that made, unless its result carries over (Worker.carry)."""
    def __init__(self, worker, checkout=None, label="newer half", settles=True):
        super().__init__(worker.queue)
        self.worker, self.label, self.settles = worker, label, settles
        self.role = label
        self.checkout = checkout or worker.queue.home / f"{worker.queue.main_checkout.name}{BESIDE_SUFFIX}"
        self.code, self.environment = worker.code, worker.environment
        self.unlocked = worker.unlocked  # the worker's own, which a lock either half meets adds to (screen_locked)
        self.unreachable = []  # the newer entries that apply to origin/main only after the older half (left_out)
        self.kept_for = []  # the entries whose changelog resolutions it recorded (noted)
        self.newer, self.tip = [], None  # the newer half on top of origin/main, and its commit (Worker.beside)
        self.result = None  # (status, log) of its lanes (both)
        self.ran_where, self.round_name = "", None  # where its lanes ran (gate), and its round's name (both)
        self.outcome = None  # LANDED, FAILED, BISECT, PASSED (passed, gating again) or QUEUED (weigh)
        self.prior = dict(worker.refusals)  # the refusals the gate before this round gave and blamed no entry for

    def say(self, words):
        self.worker.say(f"{self.label}: {words}")

    def prepare(self):
        if self.checkout == self.queue.checkout: return self.worker.prepare()
        self.place(self.queue.main_checkout, self.checkout, self.checkout,
                   "the merge queue's checkout for a bisect's newer half or a stacked gate, beside its own")

    def left_out(self, entry, state, **fields):
        """A newer entry that conflicts may need an older one of the bisect: it waits rather than come back. A
        stacked gate settles nothing (settles), since the batch below it may not land."""
        if state == CONFLICT or not self.settles: self.unreachable.append(entry)
        else: super().left_out(entry, state, **fields)

    def noted(self, entry, **fields):
        """Record the changelog conflicts it settled, as the round's integrate does, and which entries it did so
        for, which a stacked gate thrown away clears again (Stack.drop)."""
        self.kept_for.append(entry["id"])
        super().noted(entry, **fields)

    def both(self, worker, lanes, round_name):
        """Gate the newer half here, on the second Mac (BESIDE), while worker gates the older half on this Mac
        (HERE), and return the older half's (status, log). A second Mac that cannot start them now leaves the newer
        half with no verdict (resources.BUSY), which weigh queues again."""
        box = {}
        name = f"{time.strftime('%Y%m%d-%H%M%S')}-{self.tip[:8]}"
        self.round_name = name
        def gate():
            try: box["result"] = self.gate(lanes, name, where=BESIDE)
            except BaseException as error: box["error"] = error
        thread = threading.Thread(target=gate, name="newer half", daemon=True)
        thread.start()
        try: older = worker.gate(lanes, round_name, where=HERE)
        finally: thread.join()
        error = box.get("error")
        if isinstance(error, processes.Interrupted):
            self.queue.mark(self.newer, QUEUED)
            raise error
        if error is not None: self.say(f"its lanes could not run: {error}")
        self.result = box.get("result") or (resources.BUSY, None)
        return older

    def weigh(self, config, base, older_landed):
        """Settle the newer half by its lanes' result: send back the one entry that failed, queue several that failed
        to bisect next (BISECT), land a pass, or, once the older half landed, land it only when its result carries
        over, else queue it to gate again (PASSED). No verdict, the second Mac declining among them, queues it."""
        result, log = self.result
        if result == WITHDRAWN:
            self.say(f"{', '.join(self.withdrawn)} left the queue, which stopped its gate: queued again")
            self.queue.mark(self.newer, QUEUED)
            self.outcome = QUEUED
            return
        if result in NO_VERDICT:
            locked = self.screen_locked(result, log)
            if locked: self.unlocked.add(locked[0])
            self.say(f"no verdict (exit {result}){self.ran_where}: queued again, behind the older half"
                     + (f"; {locked[0]} ran behind a locked screen, and goes next only to a Mac whose screen reads "
                        "unlocked" if locked else ""))
            self.queue.mark(self.newer, QUEUED)
            self.outcome = QUEUED
            return
        flakes = None
        if result:
            flakes = self.rerun(config, self.newer, base, self.tip, log, self.round_name)
            if not flakes:
                blamed = self.blame(self.newer)
                if self.unblamed and self.repeats(log):
                    self.outcome = QUEUED
                    return
                if blamed:
                    self.send_back(self.newer, log, *blamed)
                    self.outcome = FAILED
                elif len(self.newer) == 1:
                    self.failed(self.newer, log)
                    self.outcome = FAILED
                else:
                    self.say(f"{len(self.newer)} commits fail together here too: they bisect once the older half has")
                    self.queue.mark(self.newer, QUEUED)
                    self.outcome = BISECT
                return
        current = [self.queue.read(entry["id"]) for entry in self.newer]
        if any(entry is None or entry["state"] not in WAITING for entry in current):
            self.say("an entry was removed during the run: gating what is left")
            self.queue.mark(self.newer, QUEUED)
            self.outcome = QUEUED
            return
        if older_landed: self.say("passed beside the older half, which landed first: landing on top of it if the "
                                  "result carries over, else gating again")
        head = self.mark_flaky(flakes, base) if flakes else self.head()
        self.keep(self.newer, base, head, None, None, flakes)
        if self.push(config, self.newer, base, None, None, flakes):
            self.outcome = LANDED
        else:
            self.queue.mark(self.newer, QUEUED)
            self.outcome = PASSED

    def repeats(self, log):
        """Whether the refusal no newer entry brings (unblamed) is the one the gate before this round gave, or the
        older half's beside it, whose refusal the worker has read (Worker.rerun) when it failed too: then the worker
        holds (Worker.refused_again) and sends back no newer entry, not even a lone one, whose tree the refusal does
        not name. Either way it records the refusal, on which the older half's gate, or the next gate, holds when it
        gives the same (Worker.older)."""
        key, line = self.unblamed["key"], self.unblamed["line"]
        older = self.worker.refused
        other = self.prior.get(key) or ("the bisect's older half beside it"
                                        if older and refusal_key(older[1]) == key else None)
        self.worker.refusals[key] = "the bisect's newer half beside it"
        if other: self.worker.refused_again(self.newer, line, other, log)
        return bool(other)

    def failed(self, applied, log):
        """The one newer entry that failed comes back, as a lone entry that fails does (Worker.failed)."""
        why = "the queue's lanes fail on this commit on top of origin/main: fix it and add it again"
        self.say(f"{applied[0]['id']}: {why}")
        self.queue.settle(applied[0], FAILED, why=why, log=str(log))


class Stack:
    """Entries that arrived while a batch gates, gated stacked on its tip, head, in runner's checkout, on the other
    Mac (where), in a thread of their own (owner's decision of 2026-11-07, vocaro-engineering#241). The batch's push
    makes head origin/main, and then the next round adopts the stacked gate as its own (Worker.adopted); any other
    outcome throws it away (drop), canceling its lanes, with its entries queued again. It settles nothing itself."""
    def __init__(self, worker, runner, entries, base, lanes, where, below=()):
        self.worker, self.runner, self.entries, self.base, self.lanes = worker, runner, entries, base, lanes
        self.where, self.tip, self.name = where, None, None
        self.below = list(below)  # the batch whose tip base is, which a conflict names (Worker.conflict)
        self.cancel, self.box, self.thread = threading.Event(), {}, None

    def begin(self):
        """Put the entries on base in runner's checkout and start their lanes; False when none applies there."""
        self.runner.prepare()
        applied = self.runner.integrate(self.entries, self.base, below=self.below)
        if not applied: return False
        self.entries, self.tip = applied, self.runner.head()
        self.name = f"{time.strftime('%Y%m%d-%H%M%S')}-{self.tip[:8]}"
        self.worker.queue.mark(applied, TESTING)
        self.worker.say(f"testing {len(applied)} entr{'y' if len(applied) == 1 else 'ies'} that arrived during the gate, "
                        f"at {self.tip[:12]}, stacked on {self.base[:12]}, "
                        + ("on this Mac" if self.where == HERE else "on the second Mac if it can start them now"))
        def gate():
            try: self.box["result"] = self.runner.gate(self.lanes, self.name, where=self.where, cancel=self.cancel)
            except BaseException as error: self.box["error"] = error
        self.thread = threading.Thread(target=gate, name="stacked gate", daemon=True)
        self.thread.start()
        return True

    def done(self):
        return self.thread is None or not self.thread.is_alive()

    def peek(self):
        """(status, log) of a stacked gate that has ended, None for one whose lanes raised."""
        return self.box.get("result")

    def void(self):
        """Why a stacked gate that has ended cannot be kept, or None: it gave no verdict, its lanes raised, or an
        entry of it left the queue, which stopped them (Worker.gate)."""
        result = self.peek()
        if result is not None and result[0] == WITHDRAWN: return f"{', '.join(self.runner.withdrawn)} left the queue"
        if result is None or result[0] in NO_VERDICT:
            return f"it gave no verdict (exit {result[0] if result else resources.BUSY})"
        return None

    def stale(self, waiting):
        """Why the next round cannot adopt it, or None: an entry gone from the queue, or one eng land put first or
        lands without the gate, or a batch that passed waiting for its push, all of which go first."""
        ids = {entry["id"] for entry in waiting}
        gone = [entry["id"] for entry in self.entries if entry["id"] not in ids]
        if gone: return f"{', '.join(gone)} left the queue"
        first = next((entry for entry in waiting if entry["id"] not in {e["id"] for e in self.entries}
                      and (entry.get("front") or entry.get("noGate") or entry["state"] == PASSED)), None)
        if first: return f"{first['id']} goes first"
        return None

    def wait(self):
        """(status, log) of its lanes once they end. A stop request that ended them ends the round too."""
        while self.thread.is_alive(): self.thread.join(1)
        error = self.box.get("error")
        if isinstance(error, processes.Interrupted): raise error
        if error is not None:
            self.runner.say(f"its lanes could not run: {error}")
            return resources.BUSY, None
        return self.box["result"]

    def drop(self, why):
        """Cancel its lanes, wait for them to end, and queue its entries again."""
        self.cancel.set()
        if self.thread is not None: self.thread.join()
        # A resolution on top of a batch that did not land is no entry's: the next integration records its own.
        for entry in self.entries:
            if entry["id"] in self.runner.kept_for: self.worker.queue.record(entry, kept=[])
        self.worker.say(f"the gate of {', '.join(entry['id'] for entry in self.entries)} stacked on {self.base[:12]} is "
                        f"thrown away, since {why}: queued again")
        self.worker.queue.mark(self.entries, QUEUED)


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

    def hunks_of(self, conflicted):
        return conflict_hunks(self.checkout, conflicted)


def conflict_hunks(checkout, conflicted):
    """Each conflict block Git left in the files of conflicted in checkout, as `PATH:FIRST-LAST`, the lines of the
    picked commit's own version of the file that the block holds (`PATH:N` for one line, `PATH before line N` where
    the commit's side is empty), so that a refused author knows which hunks to resolve without replaying the queue
    (vocaro-engineering#302). A file in conflict with no block, as a deletion leaves, gives none."""
    hunks = []
    for path in conflicted:
        try: lines = (Path(checkout) / path).read_text(errors="replace").splitlines()
        except OSError: continue
        # line is the next line of the commit's version; side, which part of a block a line is in, or None outside.
        line, side, first = 1, None, None
        for content in lines:
            if side is None and content.startswith("<<<<<<< "): side = "ours"
            elif side in ("ours", "base") and content.startswith("||||||| "): side = "base"
            elif side in ("ours", "base") and content == "=======": side, first = "theirs", line
            elif side == "theirs" and content.startswith(">>>>>>> "):
                last = line - 1
                hunks.append(f"{path}:{first}" if last == first else f"{path}:{first}-{last}" if last > first
                             else f"{path} before line {first}")
                side = None
            elif side in (None, "theirs"): line += 1
    return hunks


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
    changelog conflicts of entry that a round would settle; waits, the entries ahead it conflicts with alone, not
    with origin/main (Worker.conflict); hunks, its conflict blocks (conflict_hunks)."""
    with scratch(queue, base, "trial") as path:
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
                "waits": list(getattr(worker, "only_ahead", ())) if why else [],
                "hunks": list(getattr(worker, "hunks", ())) if why else [],
                "left": {other["id"]: worker.outcomes[other["id"]][1] for other in ahead
                         if other["id"] in worker.outcomes},
                "kept": worker.notes.get(entry["id"], {}).get("kept", [])}


@contextlib.contextmanager
def scratch(queue, base, kind):
    """A detached worktree of the repository at base, locked with TRIAL_REASON so that no sweep takes it, in a
    folder of its own outside every checkout, which goes when the block ends (vocaro-engineering#178)."""
    folder = Path(tempfile.mkdtemp(prefix=f"eng-land-{kind}-"))
    path = folder / queue.main_checkout.name
    try:
        git(queue.root, "worktree", "add", "--detach", "--lock", "--reason", TRIAL_REASON, str(path), base,
            hooks=False, check=True)
        yield path
    finally:
        # Unlocked first, so that a worktree the removal leaves registered is one Git's own pruning takes.
        git(queue.root, "worktree", "unlock", str(path), hooks=False)
        git(queue.root, "worktree", "remove", "--force", "--force", str(path), hooks=False)
        shutil.rmtree(folder, ignore_errors=True)


def linear(queue, root, tip):
    """The commit that tip's own commits, replayed in order onto origin/main, make, or None when they cannot be:
    a merge in origin/main..tip brings in work of its own (landing.own_merges), as a merge of another branch does, or a
    commit conflicts. The merges are refreshes of the branch from main, as the desktop app's sync makes, which a
    queue must not put on main; the branch and its worktree are left as they are, and a commit main already has
    is passed over, as a round's pick does (vocaro-engineering#217)."""
    if landing.own_merges(lambda *words: git(root, *words), MAIN, tip) != []: return None
    own = text(root, "rev-list", "--reverse", "--no-merges", f"{MAIN}..{tip}").split()
    with scratch(queue, text(root, "rev-parse", MAIN), "linear") as path:
        for commit in own:
            if git(path, "cherry-pick", commit, hooks=False).returncode == 0: continue
            if text(path, "diff", "--name-only", "--diff-filter=U") or git(path, "status", "--porcelain").stdout.strip():
                git(path, "cherry-pick", "--abort", hooks=False)
                return None
            git(path, "cherry-pick", "--skip", hooks=False, check=True)
        return text(path, "rev-parse", "HEAD")


# How many commits a refused merge's message lists; the rest it only counts.
MERGED_IN_LISTED = 10


def merge_refusal(root, merges, tip):
    """Why eng land queues nothing for a branch with merges, which linear could not replay. A merge that brings in
    commits origin/main lacks is another branch's work, which rebasing the branch's own commits would drop, so the
    refusal names those commits and the two ways to carry them (vocaro-engineering#262). Any other merge, as one
    whose replay conflicts, has only the branch's own commits to move."""
    read = lambda *words: git(root, *words)
    bringing = landing.own_merges(read, MAIN, tip)
    commits = landing.brought_in(read, MAIN, bringing) if bringing else None
    if not commits:
        return (f"{merges[0][:12]} is a merge, which a queue would have to put on main: rebase the branch's own "
                "commits onto origin/main and add it again")
    many = len(bringing) > 1
    listed = "; ".join(f"{commit[:12]} {subject}" for commit, subject in commits[:MERGED_IN_LISTED])
    if len(commits) > MERGED_IN_LISTED: listed += f"; and {len(commits) - MERGED_IN_LISTED} more not listed here"
    them = "them" if many else "it"
    return (f"{', '.join(merge[:12] for merge in bringing)} "
            f"{'are merges that bring' if many else 'is a merge that brings'} in {len(commits)} "
            f"commit{'' if len(commits) == 1 else 's'} origin/main lacks: {listed}. A queue would have to put {them} on "
            f"main: take {them} off the branch and cherry-pick those commits onto it, or wait until their own branch "
            "lands and merge origin/main, and add the branch again")


def tried(found, base, front, elapsed):
    """What eng land says of a trial that let its entry through, line by line."""
    after = f" after {', '.join(found['against'])}" if found["against"] else ""
    lines = [f"checked in {elapsed:.1f}s: it applies onto origin/main at {base[:12]}{after}"
             + (", alone, as --front tests it" if front else "") + ", as a round would integrate it now"]
    lines += [f"a round would settle {kept}" for kept in found["kept"]]
    lines += [f"left out of the check, as a round would leave it: {identifier}, {why}"
              for identifier, why in found["left"].items()]
    return lines


def refusal(tip, found, base, root):
    """Why eng land queues nothing: the trial's conflict, with what it was checked against, its hunks, and the command
    that waits for the entries ahead that change the files in conflict (vocaro-engineering#304)."""
    checked_ = f"origin/main at {base[:12]}" + (f" and, in order, {', '.join(found['against'])}" if found["against"]
                                                else "")
    words = f"{tip[:12]} is not queued: {found['why']}. Checked against {checked_}"
    if found["touching"]:
        many = len(found["touching"]) > 1
        words += f"; {', '.join(found['touching'])} change{'' if many else 's'} the files in conflict"
    if found["left"]:
        words += f"; left out, as a round would leave {'them' if len(found['left']) > 1 else 'it'}: " + \
            ", ".join(found["left"])
    if found["hunks"]:
        words += f"; its lines in conflict: {', '.join(found['hunks'])}"
    words += (". An entry ahead that comes back may let it apply: `eng queue status` says, and `eng land` again "
              "checks again (guides/maintenance.md, The merge queue)")
    if found["touching"]:
        many = len(found["touching"]) > 1
        words += (f". To be told when {'they leave' if many else 'it leaves'} the queue, run as a background command: "
                  f"python3 {shlex.quote(str(root))}/scripts/eng queue wait {' '.join(found['touching'])}, which says "
                  f"whether {'they all' if many else 'it'} landed, so rebase, or came back, so `eng land` may apply it "
                  "as it is" + (f"; `eng land --wait` waits for {'them' if many else 'it'} itself and checks again"
                                if found["waits"] else ""))
    return words


def run(root):
    """Be the repository's worker until nothing waits. A second worker exits at once. Anything
    unexpected holds the queue, with every entry still waiting, rather than guessing at a verdict; the hold's
    reason says whom it is for (worded). A cause that clears by itself, a remote out of reach past its backoff,
    runs the round again after each of CLEARING_WAITS first, this process staying the worker through the wait,
    and holds for a session only when the last try fails too."""
    # Its gate lanes inherit its signal dispositions, which must not depend on whether `eng land` ran as `&`.
    processes.default_background_signals()
    queue = Queue(root)
    progress, tries = carried(), 0
    while True:
        with queue.worker() as free:
            if not free: return 0
            # Whatever a worker that died left testing was never pushed. What it left passed is pushed first,
            # and a push that had gone through lands it then, as a push of what main already has.
            queue.mark([entry for entry in queue.all() if entry["state"] == TESTING], QUEUED)
            # Nor is its account of its round this worker's (ROUND_FILE).
            with contextlib.suppress(OSError): queue.round_file.unlink()
            worker = Worker(queue, progress)
            progress = None
            try:
                with processes.forwarding_signals():
                    worker.drain()
                    worker.catch_up()
            except NoLocalNetwork as lost:
                # Holding nothing: the next `eng land`, `eng queue wait` or `eng queue resume` starts a worker of its
                # session's, and a hold would wait for a session to resume it instead.
                queue.mark([entry for entry in queue.all() if entry["state"] == TESTING], QUEUED)
                worker.say(f"exits, holding nothing, since this worker cannot reach the second Mac: {lost}; the next "
                           "`eng land`, `eng queue wait` or `eng queue resume` from a live session starts another")
                return 0
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
                    queue.note(lambda state: state.update(
                        waiting=f"the round to run again after {delay}s, since {error}", waitingSince=time.time()))
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
                        "still waiting at the limit. Refused for entries ahead alone, it waits for them and checks "
                        "again")
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
                        "an unrelated flaky test: land it alone on the spelling lane, the project's or the toolkit's, and "
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
    doctoring = operations.add_parser("doctor", help="read the queue for the signs that it is stuck rather than testing, "
                                      "such as gates that ran nothing, and say what each means and what heals it; it "
                                      "changes nothing: 0 healthy, 1 with findings")
    doctoring.add_argument("--json", action="store_true")
    following = operations.add_parser("wait", help="follow an entry to its result, or several, such as the entries an "
                                       "eng land conflict names, until none waits: 0 landed, or each did, 1 came back, "
                                       "or one did, 75 held or still waiting at the limit")
    following.add_argument("id", nargs="+")
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
    if args.operation == "doctor": return doctor(root, args.json)
    if args.operation == "wait":
        return wait(root, args.id[0], args.limit) if len(args.id) == 1 else wait_for(root, args.id, args.limit)
    if args.operation == "remove": return remove(Queue(root), args.id)
    if args.operation == "resume": return resume(root)
    return run(root)
