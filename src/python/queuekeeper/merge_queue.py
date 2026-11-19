"""A local merge queue for a direct-main repository: `qk queue add`, and a session moves on.

One worker per repository takes everything waiting, cherry-picks it onto origin/main in a checkout
of its own, runs the project's queue lanes there once, and pushes on a pass. When the lanes fail on
a batch it halves the batch until it finds the commit to send back, landing the rest, with each bisect's two halves
gated at once, the newer on another Mac when it can start them (Beside), and the entries that arrive during a
gate gated stacked on its tip on another Mac, landing once it has (Stack, vocaro-engineering#241); first, when the lane is
qk unittest, it reruns the failed tests alone, and a batch whose failed tests all pass alone and that changes none
of the files they import lands, with the flakes recorded (vocaro-engineering#231), and a batch whose real failures
all point at one entry, by a path their output names or a file they import, sends that entry back and gates the
rest at once instead of bisecting (Worker.blame). Only a lane's
verdict blames a commit: a run the host left unmeasured runs again, each time once the host has settled,
until queue.noVerdictRuns runs in a row (3 by default) have given none, and then holds the queue for the
owner (a run a locked screen voided is not counted, and its lane runs next only on a Mac whose screen reads
unlocked, until LOCKED_SCREEN_RUNS such runs in a row hold the queue for a session, vocaro-engineering#293), with every commit still waiting. A batch whose lanes
passed is recorded as passed before its push, so a hold the push meets keeps that verdict, and
`qk queue resume` pushes the commit that passed instead of gating it again. Each round the worker moves to
the toolkit at origin/main, starting itself again from its copy, so that it judges main with the toolkit the
lanes run rather than the code it was started from (vocaro-engineering#173). A hold says whom it is for: the
owner only for a cause nobody else can fix; a session for every other, which resumes the queue itself once the
cause is dealt with or gone, after the tries a cause that clears by itself gets (worded, run).

The state is in the repository's common Git directory, so every worktree shares it, and a ref holds
each waiting commit, so a branch that moves or goes takes nothing with it:

    <common>/queuekeeper-queue/entries/ID.json   one commit range and its state
    <common>/queuekeeper-queue/logs/             what each round's lanes printed
    <common>/queuekeeper-queue/hold              why the queue stopped, until `qk queue resume`
    <common>/queuekeeper-queue/closed.json       who closed the queue to new entries and why, until `qk queue reopen`
    refs/queuekeeper/queue/ID                    the range's tip, until it lands or is removed
    ../queuekeeper-workers/NAME/NAME            the worker's checkout, in a folder beside the main checkouts
    ../queuekeeper-workers/NAME/TOOLKIT         its copy of the toolkit, which ../TOOLKIT names from the checkout
"""
import ast
import collections
import contextlib
import fcntl
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.request
from xml.etree import ElementTree

from . import budgets, disk, extensions, farm, host_health, lane_results, landing_proofs, lane_progress, lane_report, lane_wait, offload_host, parallel, pins, processes, remote, \
    resources
from . import events, pre_push, recovery, restartable
from .config import DOCS, IMPLICIT_DOCS, NOT_PREFLIGHT, PREFLIGHT_LIMIT, \
    PROJECT_CHECKOUT_VARIABLE, PROVISION, TOOLKIT_ROOT, admission, implicit_command, \
    lane_to_run, legacy_carried, legacy_unread_warning, load, names_path, needed_labels, parse, preflight, project_checkout, \
    siblings_in, stays, toolkit_command, affected_by, affected_lanes, gate_lanes, unlocked_screen, unread, unread_declared, unread_lanes, unread_named

MAIN = "refs/remotes/origin/main"
FETCH = ["fetch", "--no-tags", "origin", f"refs/heads/main:{MAIN}"]
LAUNCHER_BUNDLE = "com.vocaro.queuekeeper.launcher"
LAUNCHER_PROGRAM = "QueueLauncher"
LAUNCHER_REQUIREMENT = f'-R=anchor apple generic and identifier "{LAUNCHER_BUNDLE}"'
# The signed launcher's executable at the start of its arguments, and the app that holds it.
LAUNCHER_ARGUMENTS = re.compile(rf"^(/.+?\.app)/Contents/MacOS/{LAUNCHER_PROGRAM}(?: |$)")
LAUNCHER_INTERVAL = 120
LAUNCHCTL = "/bin/launchctl"
QUEUED, TESTING, PASSED, LANDED, FAILED, CONFLICT, REMOVED = \
    "queued", "testing", "passed", "landed", "failed", "conflict", "removed"
# Passed is a batch whose lanes passed and whose push has not landed it: Worker.keep and Worker.push_passed.
WAITING = {QUEUED, TESTING, PASSED}
# A waiting entry whose push may have landed, cut off before its answer came by a timeout, a dropped connection or
# the worker's death: its record keeps the push (publishing) until a fetch of origin/main says (Worker.reconcile).
PUBLICATION_UNVERIFIED = "publication-unverified"
# What an entry's record gives the event of its final state (Queue.settle), where it has them. The tip lets a reader,
# such as health.came_back, count one commit added again as one (vocaro/queuekeeper#219).
EVENT_FIELDS = ("tip", "why", "log", "landedAs", "landedTip", "landedCommits", "docsOnly", "docsNamed", "ungated",
                "conflictWith")
# What Worker.older returns for a batch whose lanes gave no verdict, which is no entry's state.
NO_VERDICT_OUTCOME = "no verdict"
# What a lane exits with when it says nothing about the commits: unmeasured, no admission, a
# shared host's configuration, which stops for the owner, and a worker that lost Local Network access (Worker.older).
NO_VERDICT = {lane_results.UNMEASURED, resources.BUSY, remote.MISCONFIGURED, remote.LOST_ACCESS}
# Of those, the exits of a lane that did not run: no Mac admitted it, a shared host's configuration stopped it, or this
# worker can no longer reach another Mac.
UNRUN = {resources.BUSY, remote.MISCONFIGURED, remote.LOST_ACCESS}
# Whom a hold is for, in its reason's first words (worded): the owner only for a cause nobody else can fix, a
# misconfigured shared host, a refused push or credentials; a session for every other, and the session that deals
# with the cause, or finds it gone, clears it with `qk queue resume` itself, never asking the owner (owner's
# decision, 2026-11-04, vocaro-engineering#173). CLEARS, a cause that clears by itself, is tried again first (run).
OWNER, SESSION, CLEARS = "owner", "session", "clears"
# Seconds a whole lane may take with its wait for admission: a lane's own limit is 2 h by default
# (processes.DEFAULT_LIMIT), and this allows another two of them in the host's queue.
LANE_LIMIT = 3 * processes.DEFAULT_LIMIT
# How many runs in a row may give no verdict before the queue holds, unless main's queue.noVerdictRuns says
# otherwise: a busy host clears, so each run after the first waits for it to settle. On 2026-11-04, 8 of 12 of
# EPUBLib's gates on another Mac gave none, as the simulators' voices were not ready in time (EPUBLib#946),
# and with one retry each pair held the queue until a person resumed it. A third run costs at most one gate and
# one settle wait more, and still holds the queue for a cause that does not clear.
NO_VERDICT_RUNS = 3
# How many runs in a row may end behind a screen that locked during them before the queue holds: the runs
# queue.noVerdictRuns does not count, which would otherwise retry without end a lane whose screen locks during every
# run. The number is the owner's judgment, accepted on 2026-11-09, not a measurement (vocaro-engineering#293).
LOCKED_SCREEN_RUNS = 5
# Seconds each retry waits for the host to settle, by admission's load and memory test (resources.pressure), and
# between its looks. A retry started at once met the load that voided the run it repeats, and each such run cost
# mpm's queue a gate of about an hour (#124); a wait no longer than that run, past which the queue holds as
# before, costs no more than the retry it saves.
SETTLE_LIMIT = 3600
SETTLE_POLL = 15
# Seconds between the wait's lines in the worker's log while the host stays busy.
SETTLE_REPORT = 600
# queuekeeper's reading of a Mac's screen in a lane's log (cli.screen_reading, offload_host.screen_line).
SCREEN_LINE = re.compile(r"^queuekeeper: (screen: .*)$", re.MULTILINE)
# The line in which a lane says it was not run on a Mac, and why, such as a locked screen, as qk run writes it before
# exiting 75; indented where a group quotes its member's log (parallel.lanes).
NOT_RUN_LINE = re.compile(r"^[ \t]*queuekeeper: (\S+ not run on .+)$", re.MULTILINE)
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
# process running from it kept it behind (Worker.catch_up). The tries go on until it moves, with no bound
# (owner's decision, 2026-11-11, vocaro-engineering#384): a test sets FOLLOW_LIMIT, in seconds, to end them.
FOLLOW_POLL = 30
FOLLOW_LIMIT = None
# How Worker.stays begins the reasons catch_up tries again, and how many of the processes it names.
IN_USE = "a process started from it may still run its toolkit"
UNKNOWN_USE = "whether a process started from it still runs is unknown"
SHOWN_USERS = 5
# What a process that holds the shared checkout is, by its arguments, for checkout_holder: a queue worker started from it
# (Queue.command) and the project it serves, the Apple plugin's build-tool wrapper and the tool, and a qk command.
WORKER_OF = re.compile(r"'queue', 'run'.*?/src/python (\S+)")
SHIMMED = re.compile(r"/_shims/([\w.-]+)(?:\s|$)")
QK_COMMAND = re.compile(r"/(?:bin|scripts)/qk\s+([a-z][\w-]*)")
# What a project's launcher reads first to find the toolkit.
TOOLKIT_VARIABLE = "QUEUEKEEPER_ROOT"
# What carries a worker's bisect progress to the worker that replaces it on a newer toolkit (Worker.move_to).
PROGRESS_VARIABLE = "QUEUEKEEPER_QUEUE_PROGRESS"
# The worker's lock, as a descriptor the worker that replaces it inherits (Worker.move_to, kept_lock).
LOCK_VARIABLE = "QUEUEKEEPER_QUEUE_LOCK"
# The file in the queue's directory that names the toolkit commit a worker moved to (Worker.move_to) until the
# worker started from it finishes a round (Worker.arrived): one that died or held first is not moved to again.
MOVED = "moved"
# In a queue's directory: the last pin set its worker started Queuekeeper with (Worker.pin).
PINS_STARTED = "pins.json"
# Seconds the toolkit at origin/main has to import what a worker runs before a worker moves to it (starts).
STARTS_LIMIT = 120
# How many of a failed run's last lines qk land's refusal quotes (preflight_before_queued, vocaro-engineering#283).
PREFLIGHT_SHOWN = 40
# qk land's emergency options, in every repository, by the entry field each sets: an entry tested next as a batch
# of its own, and one landed alone on the preflight lanes without the gate, whose written reason --why gives (owner's
# decision, 2026-11-04, vocaro-engineering#163). Neither is routine.
OPTIONS = {"front": "--front", "noGate": "--no-gate"}
# The trailer an ungated landing adds to its tip, so that main's history says so (Worker.mark_ungated).
UNGATED = "Landed-without-gate"
# While a bisect goes on, the newer half of its suspects gates beside the older half, at once, in a second checkout,
# the two on different Macs (owner's decision of 2026-11-07, vocaro-engineering#241): the older half is offered as any
# gate is, and the newer goes to any Mac but the one that admitted the older, if one can start it now (apart). A newer
# half no Mac starts now waits, as before. Until vocaro/queuekeeper#211 the older half ran only on this Mac
# (`qk run --here`) and the newer never on it (`--offload-only`). The second checkout sits beside the worker's, with
# this suffix (Beside), and the newer half looks this often for the Mac that admitted the older.
BESIDE_SUFFIX = "-beside"
BESIDE_POLL = 2
# When both halves of a bisect pass outright, though together they failed, no entry fails alone: the worker gates
# subsets of them, one at a time in the newer half's checkout, each subset's verdict kept, and drops entries greedily
# while the rest still fail, down to the fewest that fail together; it sends each of those back naming the others,
# by ID and commit (conflictWith), and the rest gate again (Worker.together). Until then the older half landed and the
# newer gated again on top of it, to come back alone with no word of the entry it fails with. The search follows
# mergetrain's minimize_joint_failure (yongjip/mergetrain, src/mergetrain/git_runner.py, MIT License; see NOTICE).
TOGETHER = "fails together"
# While a batch gates, the entries that arrive behind it gate stacked on its tip, on another Mac, in the checkout
# its gate does not use; the batch's push makes that tip origin/main, so the stacked gate's pass lands, and its
# failure or no landing throws the stacked gate away (owner's decision of 2026-11-07, vocaro-engineering#241). The
# worker looks for them this often (Worker.watched).
STACK_POLL = 30
# A busy Mac can stay busy over many polls. Wait four polls before reintegrating a declined stack (#348).
STACK_ADMISSION_BACKOFF = 4 * STACK_POLL
STACK_ADMISSION_DECLINED = "queue: no Mac admitted the last lane of this stacked gate"
# Most stacks used to wait to be declined (exit 75) on a busy farm. A stack starts only when a Mac it may go to is
# under its heavy busy limit with normal memory pressure (Worker.stack_room), and else backs off as a declined one.
STACK_ROOMLESS = "no room for a stack"
NO_ROOM = "no Mac but the batch's"
# A docs-only entry never rides a code gate (owner's rule of 2026-11-11, vocaro-engineering#394). Queued beside code
# entries, it lands first in a round of docs entries alone (Worker.docs_ahead); while a code batch gates, it lands
# beside the gate in a checkout of its own with this suffix (Worker.docs_beside), and the code batch's pass carries over
# the documents main gained (Worker.carry). It goes ahead of an older entry only when the two change no path in common.
DOCS_SUFFIX = "-docs"
# The seconds a preflight lane or `qk docs check` may run unadmitted, in the worker's checkout, as a batch's lane
# (Worker.quick): a docs-only batch's since vocaro-engineering#394, and a code batch's since vocaro/queuekeeper#169,
# whose round of 2026-11-15 waited 34 min 33 s for admission to check spelling and 8 min 3 s to check docs. The spelling and docs checks took 0.2-1.3 s on this Mac at a load of
# 15-44 on 2026-11-11; one that reaches this limit runs again admitted, as any lane, so a slow check is never a failure.
DOCS_CHECK_LIMIT = 60
# The seconds Queuekeeper may take to start in the worker's checkout, loading the project's extensions and running no
# lane, when the worker asks whether a batch's commits are why it could not start (Worker.starts,
# vocaro/queuekeeper#158). A start that reaches this limit says nothing, and the queue holds as it would unasked.
START_LIMIT = 60
# What a gate's lanes return when `qk queue remove` took an entry it tests out of the queue, which stopped them by the
# cancel a thrown-away stacked gate's lanes end with, here or on another Mac, freeing their admission there: no
# verdict on anything, and what is left of the batch gates again (Worker.gate, vocaro-engineering#247). The gate looks
# for such a removal this often, and records it on the entries it tests, for `qk queue remove` to say (stopping). A gate
# whose lane still waits to start is stopped the same way for an entry qk land --front --no-gate put first, which then
# lands ahead of it (Worker.first_ungated, vocaro/queuekeeper#26).
WITHDRAWN = "withdrawn"
# A gate's status for a lane that passed but changed the tracked tree it tested (Worker.unaltered): a failure of the
# batch, which no rerun clears.
ALTERED = 1
REMOVAL_POLL = 5
# A failed qk unittest gate's failed tests are each rerun alone on the batch's commit this many times before a
# bisect, and any rerun's failure makes the failure real; a gate that failed more tests than RERUN_MOST bisects at
# once, as that is no flake (Worker.rerun, owner's decision of 2026-11-07, vocaro-engineering#231).
RERUNS = 3
RERUN_MOST = 5
# The trailer a landing whose gate failed only flaky tests adds to its tip, one per test (Worker.mark_flaky).
FLAKY = "Flaky-test"
# What qk unittest prints last (parallel.unittest_main), each failed process in its verdict as `NAME (RESULT)`,
# each process's output under `== NAME: RESULT (N tests, S s)`, and unittest's line for each failed test.
UNITTEST_SUMMARY = re.compile(r"^queuekeeper unittest: \d+ tests \(\d+ skipped\) in \d+ processes, \d+ at a time, "
                              r"[\d.]+s: (.*)$", re.M)
# What qk unittest prints when it refuses a suite before running anything (parallel.refusals).
REFUSAL = re.compile(r"^queuekeeper unittest: .*; nothing ran$", re.M)
# How many of the newest gate logs `qk queue doctor` reads back to the last that gave a verdict, and how many of
# them in a row with no verdict make a finding (doctor).
DOCTOR_LOGS = 12
DOCTOR_NO_VERDICTS = 2
FAILED_SHARD = re.compile(r"(\S+) \(((?:[^()]|\([^()]*\))*)\)")
SHARD_HEADER = re.compile(r"^== (\S+): .* \(\d+ tests, [\d.]+s\)$", re.M)
FAILED_TEST = re.compile(r"^(?:FAIL|ERROR): (\S+) \(([^()\s]+)\)", re.M)
# The options of qk unittest a rerun keeps, and those it drops, each with its value: it names its own test and
# runs one process at a time.
RERUN_KEPT = {"-s", "--start-directory", "-p", "--pattern", "-t", "--top-level-directory", "--split", "--batch-size",
              "--timeout"}
RERUN_DROPPED = {"-j", "--jobs", "-k"}
# The options of qk unittest that take no value and that a rerun drops: one test alone has nothing to stop early.
RERUN_DROPPED_FLAGS = {"-v", "--verbose", "--fail-fast"}
GH_TIMEOUT = 60
# Seconds between readings of the CPU busy share of the Mac a gate lane runs on, which the gate's record keeps
# beside its wall time, so that the budget's median can leave out runs made on a loaded host (vocaro-engineering#235).
# Only readings taken once the lane is admitted count (vocaro/queuekeeper#112).
BUSY_SAMPLE = 60
# Seconds between readings of a gate lane's wait record until it says the lane was admitted (Admission).
ADMISSION_POLL = 1
# How many failed tests, and changed files, a rejection names (Worker.touched_words).
REJECTION_SHOWN = 5
GH_ENV = {"GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1"}
# The worker's account of the round it runs, in queuekeeper-queue, which `qk queue status` shows so that nobody has to read
# worker.log to learn why entries wait: the bisect's halves, each gate's lane, Mac and start, and what each waits for
# (Queue.note, GateWatch). Each running gate's wait record (lane_wait) is read for that this often.
ROUND_FILE = "round.json"
WATCH_POLL = 10
# The most of a lane's results folders that the line naming them lists (result_files): a gate's suites write a few.
RESULT_FOLDERS_NAMED = 8
# A gate whose wait is on a dead or uncertain guard entry that admission has not recovered (resources.Admission's
# stranded refusal, which its wait record says) is stuck, not waiting, once it has waited this long: on 2026-11-07
# BreezEPUB's gate waited 86 min behind three dead entries while status said only "waiting" (#315).
STUCK_SECONDS = 15 * 60
# A gate that has waited this long for admission on a Mac whose CPU busy share is under its limit is held back by
# what holds admission there, not by load, and `qk queue doctor` names the holders (idle_waits). On 2026-11-11
# BreezEPUB's gate waited 45 min on this Mac, 83% idle, behind one slot held by an iPad Quick Look run at about 3% CPU
# (vocaro/queuekeeper#89). The bound is STUCK_SECONDS's: once a slot frees, an idle Mac admits within
# HEAVY_SETTLE_SECONDS (30 s), so a quarter hour's wait is no settle and no short lane ahead, and it is already three
# quarters of the owner's 20-minute gate goal; a shorter bound would name holders running an ordinary short lane.
IDLE_WAIT_SECONDS = STUCK_SECONDS
WAIT_LIMIT = 6 * 3600
# While nothing about an entry it follows changes, `qk land --wait` and `qk queue wait` say what it still waits for
# this often, the gate's own heartbeat cadence (Follow). On 2026-11-12 an entry's wait printed nothing for 90 minutes,
# behind a failed round and then in its own gate on another Mac (vocaro/queuekeeper#41).
HEARTBEAT = 5 * 60
KEPT_LOGS = 200
GIT_TIMEOUT = 600
# The folder, beside the main checkouts and outside every repository, that holds each queue's checkouts as
# another Mac holds a run's (offload_host, checkouts/NAME/N/...): QUEUES/NAME/NAME and, beside it, the
# toolkit's copy, so that a project's ../vocaro-engineering finds that copy (owner's decision,
# vocaro-engineering#104). The folders beside the main checkout it replaces, NAME-queue and NAME-queue-toolkit,
# move there once nothing uses them.
QUEUES = "queuekeeper-workers"
# What locks qk land's scratch worktrees while its trial integration (trial) or its replay of a branch's own
# commits (linear) lasts, so that no sweep takes them.
TRIAL_REASON = "qk land's trial integration of a new entry, removed when it ends"
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
    # Nor the pin set of a run the worker was started from inside: each round makes its own (Worker.pin).
    drop = set(names.stdout.split()) | {resources.TOKEN, pins.VARIABLE}
    return {key: value for key, value in os.environ.items() if key not in drop}


def git(directory, *arguments, hooks=True, check=False, timeout=GIT_TIMEOUT):
    """Git in directory. Without hooks, for what the worker does in its own checkout: a project's
    checkout hook may open an editor on the owner's screen."""
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
    """An entry qk land queued nothing of, since the round's integration of it does not apply (check). qk land and
    `qk queue add` exit 1 for it, as `qk land --wait` does for an entry that came back. waits_for names the entries
    ahead that it conflicts with alone, not with origin/main, which `qk land --wait` waits for before it checks again
    (vocaro-engineering#302); it is empty for any other refusal. closed is the closure's record (Queue.closed) when
    the queue refused it as closed to new entries, and None for any other refusal."""
    def __init__(self, words, waits_for=(), closed=None):
        super().__init__(words)
        self.waits_for = list(waits_for)
        self.closed = closed


def closed_words(closure):
    """A closure (Queue.closed) as `qk queue status` and a refusal say it: who closed the queue, when and why."""
    since = closure.get("at")
    when = f" since {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(since))}" if isinstance(since, (int, float)) else ""
    return (f"closed to new entries by {closure.get('by') or 'an unnamed session'}{when}: "
            f"{closure.get('why') or 'no reason given'}")


def closed_refusal(tip, closure, also=""):
    """The refusal of tip by a queue closed to new entries (vocaro/queuekeeper#32): nothing was queued, the entries
    already in the queue finish, and whoever closed it reopens it, not the session it refused."""
    return Refused(f"{tip[:12]} is not queued: the queue is {closed_words(closure)}{also}. Entries already in it "
                   "finish. Whoever closed it runs `qk queue reopen`; run `qk land` again once `qk queue status` no "
                   "longer says closed", closed=closure)


def worded(kind, cause):
    """A hold's reason: whom it is for, in its first words, then its cause, then who runs `qk queue resume`. A hold
    is for the owner only when nobody else can fix its cause; every other is a session's, and the session that deals
    with the cause, or finds it gone, resumes the queue itself and says so, never asking the owner, whose standing
    instruction is that landings keep moving without him (2026-11-04, vocaro-engineering#173)."""
    if kind == OWNER:
        return (f"for the owner, since nobody else can fix it: {cause}; once it is fixed, any session that finds the "
                "cause gone runs `qk queue resume` and says so")
    return (f"for a session, not the owner: {cause}; the session that fixes it, or any session that finds it gone, "
            "runs `qk queue resume` itself and says so")


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
    asked = urllib.request.Request(GITHUB_STATUS, headers={"User-Agent": "queuekeeper", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(asked, timeout=20) as answer: components = json.load(answer)["components"]
        status = next((part.get("status") for part in components if part.get("name") == GIT_OPERATIONS), None)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        return None, f"GitHub's status could not be read: {error}"
    if not isinstance(status, str): return None, f"GitHub's status names no {GIT_OPERATIONS}"
    return status, None


def text(directory, *arguments):
    return git(directory, *arguments, check=True).stdout.strip()


def altered(checkout, head):
    """The tracked paths checkout no longer has as head has them, staged or not, with "HEAD" first when its HEAD moved
    off head: what a lane that passed changed of the tree it tested. The queue pushes head, the commit it built, so a
    lane that rewrote it tested a tree that would not land (Worker.gate). Files Git does not track, such as a lane's
    build products and results, are no change."""
    files = [name for name in git(checkout, "diff", "--name-only", "--no-renames", "-z", head, hooks=False,
                                  check=True).stdout.split("\0") if name]
    return (["HEAD"] if text(checkout, "rev-parse", "HEAD") != head else []) + files


def altered_remotely(dispatch):
    """What offload_host.altered found a lane changed in another Mac's checkout, by the dispatch directory's receipt:
    a list of paths, empty for none or when the receipt says nothing of it, or a string, why it could not be read."""
    try: found = json.loads((Path(dispatch) / "source.json").read_text()).get("result", {}).get("altered") or []
    except (OSError, ValueError, AttributeError) as error: return f"its receipt could not be read: {error}"
    if isinstance(found, dict): return found.get("error") or "it could not be read"
    return [name for name in found if isinstance(name, str)] if isinstance(found, list) else []


def altered_words(lane, files, where=""):
    """The sentence that sends a batch back for files, the tracked paths lane changed (altered)."""
    named = ", ".join(files[:10]) + (f" and {len(files) - 10} more" if len(files) > 10 else "")
    return (f"{lane} passed but changed the tracked files it tested{where}: {named}. The queue lands the commit it "
            "built, not the tree a lane leaves, so a lane must leave tracked files as it found them")


def configured(config, name):
    """Whether name is a lane the queue can run for this project: one its configuration gives."""
    return name in config.get("lanes", {})


def preflighted(config):
    """The preflight lanes the queue runs for this project, in order (config.preflight), that it can run (configured)."""
    return [name for name in preflight(config) if configured(config, name)]


def lanes_of(config, changed=()):
    """The lanes a batch must pass, in order: the project's preflight lanes (preflighted), and its provision lane
    when it has one, the cheap checks and setup that a gate assumes, then the docs lanes of a batch whose changed
    paths hold documents beside code (docs_also), and then `queue.lanes` or its gate lane: those of them that main's
    queue.affected selects for the paths changed (affected_lanes), every one unless each changed path is covered."""
    named = affected_lanes(config, changed)
    if not named:
        raise ValueError(f"{config.get('name')} has no gate lane, so the queue has nothing to run: name the lane "
                         "holding what a commit must pass before it lands gate")
    first = [name for name in [*preflighted(config), PROVISION] if configured(config, name) and name not in named]
    return first + [lane for lane in docs_also(config, changed, named) if lane not in first] + named


def docs_also(config, changed, named):
    """The docs lanes that a batch changing both documents and code runs as well as named, its gate lanes: those
    docs_lanes gives for its docs paths, when it changes some and other paths too, less a lane named holds, itself
    or as a group's member, and less the preflight lanes, which lanes_of runs anyway. A gate need
    not check what the docs lanes check, and on 2026-11-12 vocaro/mpm's gate ran three times on a commit that took
    its AGENTS.md past the 32 KiB its docs lane allows, and never said so (vocaro/queuekeeper#122). Empty for a
    batch of documents alone, which docs_lanes takes, or of code alone."""
    changed = sorted(changed)
    docs = [path for path in changed if docs_lanes(config, [path])]
    if not docs or len(docs) == len(changed): return []
    lanes = config.get("lanes", {})
    held = set(named) | {member for name in named if isinstance(lanes.get(name), dict)
                         for member in lanes[name].get("parallel", ())}
    held.update(preflighted(config))
    return [lane for lane in docs_lanes(config, docs) or [] if lane not in held]


def config_source(root, commit):
    """The configuration text at commit. A checkout's working tree can lag behind it."""
    shown = git(root, "show", f"{commit}:queuekeeper.json")
    if shown.returncode: raise ValueError(f"{commit} has no queuekeeper.json: {shown.stderr.strip() or 'no output'}")
    return shown.stdout


def config_at(root, commit):
    """The project's validated configuration at commit, for queue decisions."""
    return parse(config_source(root, commit))


def has_config(root, commit):
    """Whether commit holds a queuekeeper.json, which origin/main lacks until a project's first landing (#10)."""
    return git(root, "cat-file", "-e", f"{commit}:queuekeeper.json").returncode == 0


# The settings only main may give, since each loosens what the queue checks: the pre-push hook a lane covers, the
# paths that skip the gate, and the preflight lanes, which can be none. A batch whose lanes the queue runs, because main
# names none yet, gives none of them (#10, vocaro/queuekeeper#69).
MAIN_ONLY = ("hookCoveredBy", "unread", "docsOnly", "carryOver", "preflight", "affected")


def as_batch(config):
    """config, a batch's own, with what only main may say (MAIN_ONLY) left out."""
    for key in MAIN_ONLY: config.get("queue", {}).pop(key, None)
    return config


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


named = names_path


def no_verdict_runs(config):
    """How many runs in a row may give no verdict before the queue holds: main's queue.noVerdictRuns, or
    NO_VERDICT_RUNS."""
    return config.get("queue", {}).get("noVerdictRuns", NO_VERDICT_RUNS)


def ungated_lanes(config):
    """The lanes an ungated landing runs instead of the queue's: its preflight lanes (preflighted), the short checks a
    hand push ran."""
    return preflighted(config)


def ahead(front, no_gate, why):
    """The entry fields qk land's options set, or a refusal: --no-gate needs a written reason, --why, which the
    landing records everywhere it says it skipped the gate; --front takes one too, but needs none."""
    if no_gate and not (why and why.strip()):
        raise ValueError("--no-gate needs a written reason: `qk land --no-gate --why TEXT`, which the entry, the log, "
                         "the landed commit's trailer and `qk queue status` record")
    if why and not (front or no_gate): raise ValueError("--why is the reason for --front or --no-gate")
    flags = {name: True for name, wanted in (("front", front), ("noGate", no_gate)) if wanted}
    # On one line, as a trailer needs.
    if flags and why and why.strip(): flags["aheadWhy"] = " ".join(why.split())
    return flags


def docs_lanes(config, changed):
    """The lanes a batch that changes only the paths changed runs in place of the gate: its preflight lanes, then main's
    queue.unread.lanes, and the lanes queue.unread.paths names for them (unread_named), when no code lane reads any of
    them (config.unread), else None. Both sides of a rename arrive in changed, so moving a source or fixture into
    documentation still runs the code gate."""
    lanes = unread_lanes(config)
    if not changed or lanes is None or not all(unread(config, path) for path in changed): return None
    lanes += [name for name in unread_named(config, changed) if name not in lanes]
    return [name for name in preflighted(config) if name not in lanes] + lanes


def named_said(named):
    """The lanes queue.unread.paths named for a docs-only batch (unread_named), as a line says them, with why."""
    return "; ".join(f"{lane} for {', '.join(f'{path} ({why})' for path, why in paths.items())}"
                     for lane, paths in named.items())


def docs_first(entries, config, changed_by, blocked=(), gating=()):
    """The entries, oldest first, that land on the docs lanes ahead of the rest (vocaro-engineering#394): each one
    queued, put first by no option of qk land, whose own commits change only docs paths (docs_lanes), no path that an
    older entry staying in line changes, nor one of blocked, the paths of a batch gating now; and none of the entries it
    waits for (its waitsFor, from a run of its docs lanes that failed ahead of them) still in entries or in gating, the
    identifiers of that batch. An entry left in line holds
    back every later entry that changes one of its paths, so that two entries that change a path in common keep their
    order, and integration and conflict detection stay what they were."""
    held, taken = set(blocked), []
    present = {entry["id"] for entry in entries} | set(gating)
    for entry in entries:
        paths = changed_by(entry)
        if entry["state"] == QUEUED and not (entry.get("front") or entry.get("noGate")) \
                and not present & set(entry.get("waitsFor", ())) and not paths & held \
                and docs_lanes(config, sorted(paths)):
            taken.append(entry)
        else: held |= paths
    return taken


def without_docs(batch, config, changed_by):
    """batch, a round's batch, less its docs-only entries, which land on the docs lanes in rounds of their own: up to
    the first code entry that changes a path one of them does, which waits for them, so that no two entries that
    change a path in common change order (vocaro-engineering#394)."""
    kept, left = [], set()
    for entry in batch:
        paths = changed_by(entry)
        if docs_lanes(config, sorted(paths)): left |= paths
        elif paths & left: break
        else: kept.append(entry)
    return kept


def quick_lanes(config, code=False):
    """The lanes a batch runs unadmitted first (Worker.quick): its preflight lanes and the docs check. For a batch that
    changes code, less the lanes main names as its gate (`queue.lanes`, or gate), which always take admission
    (vocaro/queuekeeper#169)."""
    quick = {*preflighted(config), DOCS}
    return quick - set(config.get("queue", {}).get("lanes") or [budgets.GATE]) if code else quick


def offerable(config, lanes):
    """Why one of lanes runs only on this Mac, as its configuration says (config.stays), so that a bisect's newer half
    cannot gate on another Mac beside the older half (Beside); None when every one can go there."""
    for lane in lanes:
        if lane == DOCS and DOCS not in config.get("lanes", {}): continue
        why = stays(config.get("lanes", {}), lane) if lane in config.get("lanes", {}) else "it is no configured lane"
        if why: return f"{lane} runs only on this Mac: {why}"
    return None


def carried_over(path, config, batch_files):
    """Whether main changing path leaves a passed batch's result standing: a path no code lane reads, by main's
    queue.unread (config.unread), and one the batch does not touch. It is the same reading that lets a batch of such
    paths skip the gate, so a docs entry that lands beside a gate (Worker.docs_beside) never stops its pass carrying
    over. A path whose queue.unread.paths entry names lanes is read by those lanes, so it never carries over
    unchecked: main gaining it gates again (vocaro/queuekeeper#27)."""
    return path not in batch_files and (unread(config, path) and not unread_named(config, [path])
                                        or legacy_carried(config, path))


def carried_by_rules(path, config, batch_files, ran):
    """Whether main changing path leaves standing the pass of a batch whose own paths select the gate lanes ran
    (lanes_of): path, which queue.unread does not take (carried_over decides those) and the batch does not touch, is
    covered by main's queue.affected, and selects none of ran. Only those lanes read path, and the batch changed nothing
    they read, so main's own gate of path still holds with the batch on top, and the batch's of its lanes with path
    beneath. A provision lane, which sets up every gate, counts as reading every path. ran None, as for a docs-only or
    ungated landing, carries nothing over this way."""
    if ran is None or path in batch_files or PROVISION in ran or unread(config, path): return False
    selected = affected_by(config, path)
    return selected is not None and not selected & set(ran)


def carry_checks(config, moved, base, docs_check=IMPLICIT_DOCS):
    """The commands a carry-over over moved must pass on the rebased tree: `qk docs check`, docs_check, when a
    document moved, then each of queue.carryOverChecks whose paths include a moved file, or that has none. {base} in
    a command is the commit the result passed on, so a check that compares with origin/main, which the
    batch now sits on, can read what main's new commits added."""
    commands = [list(docs_check)] if any(path.endswith(".md") for path in moved) else []
    for check in config.get("queue", {}).get("carryOverChecks", []):
        if "paths" not in check or any(named(path, key) for path in moved for key in check["paths"]):
            commands.append([word.replace("{base}", base) for word in check["command"]])
    return commands


def rerun_command(command):
    """The command, without its test, that reruns one test of a lane whose command is qk unittest
    (vocaro-engineering#231): the lane's command through `unittest`, its selection and shard options, one
    process at a time and verbose. None for a lane that is not qk unittest, as a project's own lane, or one with an
    option the rerun cannot carry, which then bisects as before; a group's qk unittest lane is unittest_member's."""
    if not isinstance(command, list) or not all(isinstance(word, str) for word in command): return None
    at = next((index for index, word in enumerate(command[:-1])
               if Path(word).name == "qk" and command[index + 1] == "unittest"), None)
    if at is None: return None
    kept, words, index = command[:at + 2], command[at + 2:], 0
    while index < len(words):
        word = words[index]
        name, equals, _ = word.partition("=")
        if word in RERUN_DROPPED_FLAGS:
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
    """(start directory, patterns or None) of a lane whose command is qk unittest, by the options rerun_command keeps,
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


def unittest_member(lanes, lane):
    """The lane whose command would be qk unittest for lane: lane itself, or the one such lane of the group lane
    names, as the toolkit's own gate has (#289). None for a group with none or several, or a lane that is no lane."""
    settings = lanes.get(lane)
    if not isinstance(settings, dict): return None
    if "parallel" not in settings: return lane
    found = [name for name in settings["parallel"] if isinstance(lanes.get(name), dict) and suite_of(lanes[name].get("command"))]
    return found[0] if len(found) == 1 else None


def unittest_lanes(config):
    """{lane: (start, patterns)} of config's queue lanes (lanes_of) whose command is qk unittest, or that are a group
    holding one such lane."""
    lanes, found = config.get("lanes", {}), {}
    for lane in lanes_of(config):
        member = unittest_member(lanes, lane)
        suite = suite_of(lanes[member].get("command")) if member else None
        if suite: found[lane] = suite
    return found


def suite_tree(root, commit, start, patterns, into):
    """Write the files throughout commit's start directory that qk unittest's checks before a run read
    (parallel.refusals), its Python files and those patterns match, into into. False when commit has no such
    directory."""
    relative = os.path.normpath(start)
    listed = git(root, "ls-tree", "-r", "-z", commit if relative == "." else f"{commit}:{relative}")
    if listed.returncode: return False
    names = {}
    for record in listed.stdout.split("\0"):
        meta, _, name = record.partition("\t")
        fields = meta.split()
        # A file, not a directory, a submodule or a symbolic link, which qk unittest's checks would follow.
        if not name or len(fields) != 3 or fields[0] not in ("100644", "100755"): continue
        if name.endswith(".py") or any(fnmatch.fnmatchcase(Path(name).name, pattern) for pattern in patterns or ["test*.py"]):
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
    for name, sha in names.items():
        target = into / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blobs[sha])
    # A component lane may start below its shared host bootstrap.
    for depth, parent in enumerate(Path(relative).parents, 1):
        bootstrap = git(root, "show", f"{commit}:{parent / parallel.ISOLATION}")
        if bootstrap.returncode == 0:
            target = into.parents[depth - 1] / parallel.ISOLATION
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(bootstrap.stdout)
            break
    return True


def prerun(root, commit, config):
    """The lines qk unittest would refuse each of config's qk unittest lanes with on commit's tree before running
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
    """Refuse tip, queuing nothing, when qk unittest would refuse a gate holding it before running anything: a
    module that does not import host_isolation, or does not parse. These checks take seconds, where the gate they
    refuse costs a round, and a bisect blind, since it names no failed test (vocaro-engineering#290)."""
    own = refused_alone(root, tip, config)
    if own:
        raise Refused(f"{tip[:12]} is not queued: qk unittest would refuse every gate holding it before running "
                      "anything:\n  " + "\n  ".join(own) + "\nFix that and land it again")


def claims_before_queued(root, base, tip):
    """Refuse tip, queuing nothing, when a message of its commits after base denies a closing claim, as "This does not
    fix #43." does: GitHub closes the issue when the commit reaches main (landing_proofs.negated, vocaro-engineering#286)."""
    found = landing_proofs.negated_claims(lambda *words: git(root, *words), f"{base}..{tip}")
    if found is None: raise Refused(f"{tip[:12]} is not queued: its commit messages could not be read")
    if not found: return
    issue = found[0][3]
    raise Refused(f"{tip[:12]} is not queued: GitHub closes an issue a commit message names after a closing keyword, "
                  "even in a negated phrase:\n  "
                  + "\n  ".join(f"{commit[:12]} line {number} closes {named}: {line.strip()}"
                                for commit, number, line, named in found)
                  + f'\nFor an issue that stays open, write "Refs {issue}" or "leaves {issue} open"; to close it, write '
                    f'"Closes {issue}." on its own line. Reword the commit and land it again')


def preflight_run(queue, root, commit, name):
    """(exit status, the lines it printed, the command) of the preflight lane name on commit, in a scratch worktree
    outside every checkout, as the worker picks it (Worker.gate): the lane commit's queuekeeper.json configures, run as
    its command, here and with no admission. The command finds the project's main checkout in PROJECT_CHECKOUT_VARIABLE, as a lane qk run runs does. A status of None says
    why it was not run: a lane that is no one command, or a run that reached its limit, its timeout up to
    PREFLIGHT_LIMIT."""
    with scratch(queue, commit, name) as path:
        env = environment()
        env[PROJECT_CHECKOUT_VARIABLE] = project_checkout(root)
        if not os.environ.get(TOOLKIT_VARIABLE):
            # A project's launcher finds the toolkit beside its own checkout, which a scratch worktree has not: this
            # one runs it. The toolkit's own entry runs the toolkit it brings, as its queue's gate does.
            ours = git(TOOLKIT_ROOT, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
            if ours != text(root, "rev-parse", "--path-format=absolute", "--git-common-dir"):
                env[TOOLKIT_VARIABLE] = str(TOOLKIT_ROOT)
        try: lanes = load(path).get("lanes", {})
        except (OSError, ValueError): lanes = {}
        lane = lanes.get(name)
        command = lane.get("command") if isinstance(lane, dict) else None
        if not (isinstance(command, list) and command) or (isinstance(lane, dict) and any(
                lane.get(key) for key in (*NOT_PREFLIGHT, *extensions.LANE_KEYS))):
            return None, [f"the project's {name} lane is not one command that qk land can run here"], None
        limit = min(lane.get("timeout", PREFLIGHT_LIMIT), PREFLIGHT_LIMIT) if isinstance(lane, dict) else PREFLIGHT_LIMIT
        log = path.parent / f"{name}.log"
        try: status_ = processes.run(command, cwd=path, log_path=log, timeout=limit, env=env)
        except processes.TimedOut as error: return None, [f"{shlex.join(command)} reached its limit: {error}"], command
        except processes.NotStarted as error: return None, [f"{shlex.join(command)} could not start: {error}"], command
        printed = [line for line in log.read_text(errors="replace").splitlines() if line.strip()]
        return status_, printed, command


def preflight_before_queued(root, queue, tip, config):
    """Refuse tip, queuing nothing, when a preflight lane that config, origin/main's, has the queue run before the gate
    (preflighted) finds something, exit 1, in tip's commits replayed onto origin/main, quoting its findings. The lanes
    run in order, and the first finding refuses. A preflight lane takes seconds, where a slip it finds costs a queue
    round, about 40 minutes on EPUBLib on 2026-11-07 (vocaro-engineering#283). It is a hint, as check is: the worker
    still runs the lanes on the batch. A lane that cannot be run, or that fails otherwise, queues the entry for the
    round to judge, and so does a failure origin/main gives too, which is not the entry's to fix."""
    unread_warned = legacy_unread_warning(config)
    if unread_warned: print(f"queue: {unread_warned}", file=sys.stderr)
    names = preflighted(config)
    if not names: return
    base = text(root, "rev-parse", MAIN)
    try: replayed = tip if git(root, "merge-base", "--is-ancestor", MAIN, tip).returncode == 0 \
        else linear(queue, root, tip)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        print(f"queue: could not replay it onto origin/main, so the round runs its preflight lanes: {error}",
              file=sys.stderr)
        return
    if replayed is None:
        print(f"queue: could not check its {spoken(names).removeprefix('the ')}, as its commits do not apply onto "
              "origin/main alone; the round runs them", file=sys.stderr)
        return
    for name in names: preflight_lane_before_queued(root, queue, tip, replayed, base, name)


def preflight_lane_before_queued(root, queue, tip, replayed, base, name):
    """preflight_before_queued for the one preflight lane name, on replayed, tip's commits replayed onto base."""
    began = now()
    try:
        status_, printed, command = preflight_run(queue, root, replayed, name)
        main_status = preflight_run(queue, root, base, name)[0] if status_ == 1 else None
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        print(f"queue: could not check its {name}, so the round runs the {name} lane: {error}", file=sys.stderr)
        return
    ran = shlex.join(command) if command else f"the {name} lane"
    if status_ is None:
        print(f"queue: could not check its {name}, so the round runs the {name} lane: {printed[0]}",
              file=sys.stderr)
    elif status_ == 0:
        print(f"queue: {name} checked in {now() - began:.1f}s: {ran} passes on it replayed onto origin/main at "
              f"{base[:12]}", file=sys.stderr)
    elif status_ != 1:
        print(f"queue: {ran} exited {status_} on it, which is no finding, so the round runs the {name} lane: "
              + (printed[-1] if printed else "it printed nothing"), file=sys.stderr)
    elif main_status == 1:
        print(f"queue: {ran} fails on origin/main at {base[:12]} too, so its findings are not this entry's alone; the "
              f"round runs the {name} lane", file=sys.stderr)
    else:
        raise Refused(f"{tip[:12]} is not queued: {ran} fails on it replayed onto origin/main at {base[:12]}:\n  "
                      + "\n  ".join(printed[-PREFLIGHT_SHOWN:]) + "\n"
                      + (preflight_staged(root, base, tip, name) or "Fix that and land it again"))


def preflight_staged(root, base, tip, name):
    """The end of a refusal by the preflight lane name, which origin/main, base, lists in queue.preflight, when tip's own
    queuekeeper.json leaves name out of its queue.preflight or changes the lane: only main's list counts (MAIN_ONLY),
    so the lane ran on the entry although the entry stops relying on it, and the entry cannot both drop the lane and
    what it reads. That lands in two steps (vocaro/queuekeeper#186). None otherwise, or when either cannot be read."""
    try: main, own = config_at(root, base), config_at(root, tip)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError): return None
    lane = own.get("lanes", {}).get(name)
    if name not in preflight(own): change = f"leaves {name} out of its queue.preflight"
    elif lane != main.get("lanes", {}).get(name): change = f"changes the {name} lane"
    else: return None
    return (f"origin/main's queue.preflight lists {name}, and only main's list counts, so {name} ran as main has it "
            f"although this entry's queuekeeper.json {change}. Land the change to queue.preflight and the {name} lane "
            f"on its own first, and what {name} reads in a second landing once that is on main")


def main_copies(config, changed):
    """A sentence naming the paths of changed under a folder inside the project that config's extensions load from, or
    "". A lane run in a linked worktree of the project's main checkout, as the queue's checkout is, loads such a folder
    from the main checkout (config.extensions_of), so a round judges a change to it by main's copy, not the batch's,
    and an entry that changes the folder and starts relying on the change fails there although it is right. That
    lands in two steps (vocaro/queuekeeper#186). A folder beside the project is pinned instead (pins), and not named."""
    found = []
    for entry in config.get("extensions", []):
        path = entry.get("path") if isinstance(entry, dict) else None
        if not isinstance(path, str) or not path or os.path.isabs(path): continue
        folder = os.path.normpath(path)
        if folder == os.pardir or folder.startswith(os.pardir + os.sep): continue
        under = sorted(changed) if folder == os.curdir else sorted(
            name for name in changed if name == folder or name.startswith(folder + "/"))
        if under: found.append((folder, entry["module"], under))
    if not found: return ""
    more = sum(len(under) for _, _, under in found) - REJECTION_SHOWN
    paths = [name for _, _, under in found for name in under][:REJECTION_SHOWN]
    folders = " and ".join(f"{folder}, which {module} loads from" for folder, module, _ in found)
    return (f"This entry changes {', '.join(paths)}{f' and {more} more' if more > 0 else ''} in {folders}: the "
            "round's lanes load the project's extensions from its main checkout, not from the batch, so main's copy "
            "judged this entry. If the failure comes from that copy, land the change to the folder on its own first, "
            "and what relies on it once that is on main")


def hooks_elsewhere(checkout):
    """What a refusal by the pre-push hook adds when core.hooksPath names a folder outside checkout, the queue's, such
    as the main checkout's .githooks: the hook that ran is that folder's copy, not the batch's, and on 2026-11-16
    Pagepour's old hook refused a batch that had moved what it read (vocaro/queuekeeper#186). "" otherwise."""
    named = git(checkout, "config", "--get", "core.hooksPath").stdout.strip()
    if not named or not os.path.isabs(os.path.expanduser(named)): return ""
    folder = Path(named).expanduser().resolve()
    if folder.is_relative_to(Path(checkout).resolve()): return ""
    return (f". The hook ran from {folder}, which core.hooksPath names outside the queue's checkout, so it was that "
            "folder's copy and not the batch's: a change to the hook, or to what it reads, that the old copy refuses "
            "lands in two steps, keeping what the old copy needs for one landing and dropping it in a second")


def failed_tests(log):
    """(the ids of the tests an qk unittest lane's log reports failed, None), or (None, why it reports none it
    can rerun): its last summary must say FAILED, with nothing UNMEASURED, and each failed process's output must
    name at least one failed test, none of them a module that did not load, and none ran other than it was given.
    What a --fail-fast run left unrun after a failure that repeated alone (parallel.STOPPED) is no failure: the
    tests that did fail are read as any run's (cut_short)."""
    summaries = UNITTEST_SUMMARY.findall(log)
    if not summaries: return None, "its log has no qk unittest summary"
    verdict = summaries[-1].split(f"; {parallel.STOPPED}", 1)[0]
    if "UNMEASURED: " in verdict: return None, "qk unittest also left tests unmeasured"
    if not verdict.startswith("FAILED: "): return None, f"qk unittest's verdict is {verdict}"
    shards = FAILED_SHARD.findall(verdict[len("FAILED: "):])
    if not shards: return None, f"qk unittest's verdict names no process: {verdict}"
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


def cut_short(log):
    """Whether an qk unittest lane's log ends with a run that --fail-fast stopped, once a failed test failed again
    alone, before the rest of its suite ran (parallel.STOPPED): its failed tests passing alone then cannot land a
    batch, as nothing ran the tests it left (Worker.rerun)."""
    summaries = UNITTEST_SUMMARY.findall(log)
    return bool(summaries) and f"; {parallel.STOPPED}" in summaries[-1]


def junit_failures(path):
    """The names, classname.name, of the test cases a JUnit XML file says failed or erred, in its order; or (None, why
    it names none to rerun), as failed_tests gives (vocaro/queuekeeper#109)."""
    try: root = ElementTree.parse(path).getroot()
    except FileNotFoundError: return None, f"the lane wrote no {Path(path).name} in its results folder"
    except (OSError, ElementTree.ParseError) as error: return None, f"{path} cannot be read: {error}"
    found = []
    for case in root.iter("testcase"):
        if case.find("failure") is None and case.find("error") is None: continue
        name, owner = case.get("name"), case.get("classname")
        if not name: return None, f"{path} has a failed test case with no name"
        found.append(f"{owner}.{name}" if owner else name)
    if not found: return None, f"{path} names no failed test"
    return list(dict.fromkeys(found)), None


def declared_failures(lanes, lane, failed):
    """(the lane, of lane itself or of the group it names, whose failedTests say how to rerun its failed tests, those
    settings), when that lane is the only one of failed, the lanes of a group that did not pass; or None. A lane of the
    toolkit's qk unittest reruns by rerun_command instead (vocaro/queuekeeper#109)."""
    settings = lanes.get(lane)
    if not isinstance(settings, dict): return None
    if "parallel" not in settings:
        return (lane, settings["failedTests"]) if isinstance(settings.get("failedTests"), dict) else None
    if len(failed) != 1 or failed[0] not in settings["parallel"]: return None
    member = lanes.get(failed[0])
    return (failed[0], member["failedTests"]) if isinstance(member, dict) and isinstance(member.get("failedTests"), dict) \
        else None


def module_files(base, parts):
    """The files that importing parts, a dotted name's parts, from base loads: each package's __init__.py on the way,
    and the module; none when base holds no such module."""
    found, place = [], Path(base)
    for number, part in enumerate(parts):
        place = place / part
        if (place / "__init__.py").is_file(): found.append(place / "__init__.py")
        elif number == len(parts) - 1 and place.with_suffix(".py").is_file(): found.append(place.with_suffix(".py"))
        else: return []
    return found


def module_of_test(top, test):
    """(the file under top, the suite's top-level directory, that is the module of test, a test id, the rest of the
    id: its class and method), or None when no file under top is its module."""
    parts = test.split(".")
    for count in range(len(parts), 0, -1):
        found = module_files(Path(top), parts[:count])
        if found: return found[-1], parts[count:]
    return None


def defines(source, names):
    """Whether source, a test module's text, defines names, a test's class and then its method, each in the body of
    the one before: a test a base class gives, or one made at run time, is not found. None when source does not
    parse."""
    try: tree = ast.parse(source)
    except (SyntaxError, ValueError): return None
    body = tree.body
    for name in names:
        node = next((node for node in body if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                     and node.name == name), None)
        if node is None: return False
        body = node.body
    return True


def added_test(checkout, top, test, base):
    """(the file of test's module, relative to checkout, the test's class and method) when the batch checked out
    there adds test, a test id, that origin/main at base lacks (vocaro/queuekeeper#129): the module defines it in
    checkout, and at base it is missing or does not. None when base has it, or when either cannot be told, as for a
    test a base class gives, so that such a test is judged as before."""
    found = module_of_test(top, test)
    if found is None or not found[1]: return None
    path, names = found
    checkout = Path(checkout).resolve()
    try:
        relative = path.resolve().relative_to(checkout).as_posix()
        if defines(path.read_text(errors="replace"), names) is not True: return None
    except (OSError, ValueError): return None
    if git(checkout, "cat-file", "-e", f"{base}^{{commit}}").returncode: return None
    try: shown = git(checkout, "show", f"{base}:{relative}")
    except (UnicodeDecodeError, subprocess.SubprocessError): return None
    if shown.returncode: return relative, names
    return (relative, names) if defines(shown.stdout, names) is False else None


def imported_files(checkout, top, test):
    """The repository's files that the module of test, a test id, is and imports, each relative to checkout: the
    module's own import statements, at any depth of it, each with the packages' __init__.py on the way, not what those
    files import in turn (owner's decision of 2026-11-07, vocaro-engineering#231: "the toolkit files it imports"). An
    absolute import is looked for under top, the suite's top-level directory, then src/python, src and checkout, where
    a test finds the project's code; one found under none, as the standard library's, is not the project's. None when
    no file under top is the test's module."""
    checkout, top = Path(checkout).resolve(), Path(top).resolve()
    roots = [top, checkout / "src/python", checkout / "src", checkout]
    module = module_files

    def absolute(dotted):
        parts = dotted.split(".")
        return next((found for found in (module(root, parts) for root in roots) if found), [])

    found = module_of_test(top, test)
    if found is None: return None
    start = found[0]
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


def placed_file(path, files):
    """The one of files, repository paths, that path, a file as a failed test's summary names it, is: a bare name as
    Swift Testing gives, a path within the repository, or an absolute path in a checkout of it on whichever Mac ran
    the test. None when no file or more than one could be it, so that a file is never guessed
    (vocaro/queuekeeper#83)."""
    path = path.removeprefix("./")
    found = {file for file in files if file == path or path.endswith(f"/{file}") or file.endswith(f"/{path}")}
    return next(iter(found)) if len(found) == 1 else None


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


def flake_failure(log, test):
    """The lines of the first `FAIL:` or `ERROR:` block that log, a failed qk unittest gate's, gives for test, a test
    id: its heading, traceback and assertion, up to the line that ends it, so that a hardening issue quotes the failure
    and not only logs the queue later prunes (vocaro/queuekeeper#128). It is cut as a send-back's excerpt is
    (failed_lane_excerpt), FAILURE_LINE_LIMIT a line and FAILURE_EXCERPT_LIMIT in all; when it is longer, its heading
    stays and the lines nearest its end, where the assertion is. Empty when the log names no such block."""
    for match in FAILED_TEST.finditer(log):
        method, where = match.groups()
        if (where if where.endswith(f".{method}") else f"{where}.{method}") != test: continue
        end = lane_report.UNITTEST_END.search(log, match.end())
        lines = log[match.start():end.start() if end else len(log)].rstrip().splitlines()
        lines = [line if len(line) <= FAILURE_LINE_LIMIT else line[:FAILURE_LINE_LIMIT - 3] + "..." for line in lines]
        if sum(len(line) + 1 for line in lines) <= FAILURE_EXCERPT_LIMIT: return lines
        kept, remaining = [], FAILURE_EXCERPT_LIMIT - len(lines[0]) - 1 - len("[... 99999 lines left out ...]") - 1
        for line in reversed(lines[1:]):
            if len(line) + 1 > remaining: break
            kept.insert(0, line)
            remaining -= len(line) + 1
        return [lines[0], f"[... {len(lines) - 1 - len(kept)} lines left out ...]", *kept]
    return []


def hardening(checkout, flake, gh=None):
    """(the issue's URL, None) once the project's tracker has the flake: a comment on the first open issue a search
    made just before finds naming the test's name, or a new hardening issue; (None, why) when gh could not, which
    stops nothing (owner's decision of 2026-11-07, vocaro-engineering#231, and the rule that every flaky test gets a
    hardening issue). Its body quotes the test's failure from the gate's log (flake_failure), beside the logs' paths.
    gh(*arguments) runs gh for the project, gh_runner's by default; tests give their own."""
    gh = gh or gh_runner(checkout)
    name = flake["test"].rsplit(".", 1)[-1]
    try: quoted = flake_failure(Path(flake["gateLog"]).read_text(errors="replace"), flake["test"])
    except OSError: quoted = []
    if quoted:
        fence = "`" * max(3, 1 + max((len(run) for run in re.findall(r"`+", "\n".join(quoted))), default=0))
        failure = "The gate's log gave this failure:\n\n" + "\n".join([fence, *quoted, fence]) + "\n\n"
    else: failure = "The gate's log could not be read for its failure, or named no `FAIL:` or `ERROR:` block for it.\n\n"
    body = (f"The merge queue's gate failed `{flake['test']}` at {flake['commit'][:12]}, and it then passed alone "
            f"{len(flake['rerunLogs'])} of {len(flake['rerunLogs'])} times on that commit, so it is flaky. "
            f"{flake['outcome']}\n\n{failure}- Gate log: `{flake['gateLog']}`\n"
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
    """(the CPU busy share in percent, the busy limit it is judged by, the Mac read) of host, another Mac, by
    remote.busy_there, or with host None of this Mac, as admission reads it; None when it cannot be read."""
    if host:
        try: busy, limit = remote.busy_there(host)
        except remote.Unavailable: return None
        return None if busy is None else (busy, limit, host)
    try: limit = resources.busy_limit()
    except ValueError: return None
    busy = resources.cpu_busy(resources.slot_directory(), wait=True)[0]
    return None if busy is None else (busy, limit, "this Mac")


class Admission:
    """When a gate lane's run was admitted, so that the gate's budget judges the lane's own work and not its wait for
    admission (vocaro/queuekeeper#112): the time its wait record (lane_wait) was written saying the run was admitted, on
    this Mac or, as relayed, on another Mac, by this Mac's clock. The record is read every ADMISSION_POLL seconds
    until it says so; the time is the record's own, so the poll does not make it later. None until a reading sees it,
    as for a lane that ended within a poll of its admission. A measurement only: nothing a reading raises may end the
    gate."""
    def __init__(self, checkout, lane, began):
        self.path, self.began, self.at = lane_wait.path_of(checkout, lane), began, None
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self.every, name="gate admission", daemon=True)

    def look(self):
        # The first admission is the lane's: a nested run that waits and is admitted again rewrites the record later.
        if self.at is not None: return
        try: record = lane_wait.current(self.path, self.began)
        except Exception: return  # noqa: BLE001
        written = (record or {}).get("written")
        if record and record.get("admitted") is True and budgets.number(written): self.at = max(float(written), self.began)

    def every(self):
        while self.at is None and not self.stopped.is_set():
            self.look()
            self.stopped.wait(ADMISSION_POLL)

    def start(self):
        self.thread.start()
        return self

    def finish(self):
        """The time the lane was admitted, an epoch time, or None when no reading saw it."""
        self.stopped.set()
        if self.thread.ident is not None: self.thread.join(timeout=GH_TIMEOUT)
        return self.at


class BusySampler:
    """The CPU busy share of the Mac a gate lane runs on, read every BUSY_SAMPLE seconds while it runs once admitted,
    and once at its end when no reading was taken (vocaro-engineering#235). With an Admission, no reading is taken
    before the lane's admission, so that a loaded run is one whose own work met a busy Mac and not one that waited
    while the Mac was busy (vocaro/queuekeeper#112). The Mac is the one the lane runs on now (host). A reading that
    fails is left out."""
    def __init__(self, checkout, lane, began, admission=None):
        self.checkout, self.lane, self.began, self.admission = checkout, lane, began, admission
        self.readings = []
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self.every, name="gate busy share", daemon=True)

    def record(self):
        """The lane's wait record (lane_wait) as it stands now, or None."""
        try: return lane_wait.current(lane_wait.path_of(self.checkout, self.lane), self.began)
        except Exception: return None  # noqa: BLE001

    def host(self, record):
        """Another Mac the lane runs on now, or None for this Mac: the Mac its wait record names, which is the second
        Mac from the moment the lane is sent there (lane_wait.sent, relayed), or, when the record names none, as once
        the run has ended and removed it, another Mac its dispatch's record names (remote.dispatched). That record is
        filed only when the dispatch ends, so while the lane ran there it named no Mac, and every reading was this
        Mac's (vocaro/queuekeeper#132)."""
        mac = record.get("mac") if record else None
        if mac: return lane_wait.host_of(mac)
        return remote.dispatched(self.checkout, self.lane, self.began)

    def read(self, admitted=True):
        """One reading, unless admitted asks for the lane's admission first and no reading of it has seen it."""
        if admitted and self.admission is not None and self.admission.at is None: return
        # A measurement only: nothing a reading raises may end the gate or the round.
        try: found = busy_of(self.host(self.record()))
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
        if not self.readings: self.read(admitted=False)
        if not self.readings: return {}
        shares = [reading[0] for reading in self.readings]
        return {"cpuBusy": round(sum(shares) / len(shares), 1), "cpuBusyHighest": max(shares),
                "busyLimit": self.readings[-1][1], "busyOn": self.readings[-1][2], "busyReadings": len(shares)}


def load_of(host):
    """(the one-minute load, the CPU busy share in percent, the busy limit it is judged by) of host, another Mac, or
    with host None of this Mac, each None when it cannot be read. Another Mac that cannot be reached raises
    remote.Unavailable: its load is never this Mac's."""
    if host:
        answered, busy = remote.reading_there(host)
        load = answered.get("load")
        return (load if isinstance(load, (int, float)) else None), busy, float(answered["busyLimit"])
    found = busy_of(None)
    return (resources.one_minute_load(), *(found[:2] if found else (None, None)))


def load_words(lane, host, readings, waited=()):
    """What readings, load_of's or None where one failed, say of the load on the Mac lane ran on, for a rejection
    (vocaro-engineering#273), and plainly that it could not be read when none says. readings are those taken while the
    lane ran and waited those while it waited to start, which are said apart: the first count the lane's own work, a
    whole-host gate's nearly all of them, and only the second are of other work alone (vocaro/queuekeeper#12)."""
    where = lane_wait.farm_mac(host) if host else "this Mac"
    over = lane_report.readings_over
    def loads_of(of): return [reading[0] for reading in of if reading and reading[0] is not None]
    def shares_of(of):
        return [reading[1:] for reading in of if reading and reading[1] is not None and reading[2] is not None]
    figures, read = lane_report.load_figures(readings)
    said = f"While {lane} ran on {where}, {figures}"
    if read: said += ". Both count the lane's own work, so neither says what else that Mac was doing"
    before, held = loads_of(waited), shares_of(waited)
    if before or held:
        parts = ([f"the load was highest {max(before):g} over {over(len(before), waited)}"] if before else []) + (
            [f"the CPU was at most {max(share for share, _ in held):g}% busy, against that Mac's busy limit "
             f"of {held[-1][1]:g}%"] if held else [])
        said += f". While it waited to start, before any of its own work, {', and '.join(parts)}"
    return said


class LoadSampler(BusySampler):
    """The load of the Mac any lane runs on, read every BUSY_SAMPLE seconds while it runs, and once at the end of one
    that failed, so that a rejection can say it (vocaro-engineering#273). Each reading names the Mac it read, this Mac
    until the lane's dispatch names another Mac, and whether the lane's wait record (lane_wait) said it was waiting
    to start then; finish keeps only the readings of the Mac the lane ran on, those of its wait apart from those of
    its run (vocaro/queuekeeper#12). The Mac is the one the lane runs or waits on now, by the record (host). A
    measurement only: it decides nothing, and nothing a reading raises reaches the gate."""

    def look(self, host, waiting=False):
        try: reading = load_of(host)
        except Exception: reading = None  # noqa: BLE001
        self.readings.append((host, reading, waiting))

    def read(self):
        # Whether the lane waits to start now, for admission, a screen or a Mac. Unknown is not waiting.
        record = self.record()
        try: host = self.host(record)
        except Exception: return  # noqa: BLE001
        self.look(host, bool(record and record.get("kind")))

    def finish(self, host):
        """load_words for the lane, which ran on host, another Mac, or with host None on this Mac."""
        self.stopped.set()
        self.thread.join(timeout=GH_TIMEOUT)
        self.look(host)
        return load_words(self.lane, host, *([reading for read_on, reading, waiting in self.readings
                                              if read_on == host and waiting is wanted] for wanted in (False, True)))


HERE_MAC = lane_wait.HERE


def apart(mac, hosts, waits):
    """How the second gate of a pair, a bisect's newer half or the entries stacked on a batch, is kept off mac, the
    Mac that admitted the first gate's final lane as its wait record names it (Worker.admitted_mac), so that the two
    run on different Macs: (qk run's placement options, the farm Mac qk run leaves out of its offer, the words that
    say where it goes), or None when mac is neither this Mac nor one of hosts, the farm's Macs that take work now.

    Every Mac but mac is a candidate, this Mac among them, and qk run chooses among those as it does for any lane
    (remote.Offer.attempt). No rule here names the Mac the worker runs on (the owner's direction, 2026-11-17,
    vocaro/queuekeeper#211); until then the first gate of a bisect's pair ran only on this Mac, the second never on
    it, and a stacked gate ran on this Mac whenever the farm had one other Mac and the batch gated there.

    waits says whether the second gate may wait for a Mac. A stacked gate may, since the batch below it gates on
    meanwhile; a bisect's newer half may not (--if-free), since the round ends only when both halves have. What
    remains of where a command starts is not a rule but how a request waits: it waits in the queue of the Mac its
    process runs on, with a place in the others' queues for a whole-host lane (remote.Places). So a gate kept off
    this Mac (--offload-only) starts on another Mac now or not at all, whatever waits says."""
    if mac == HERE_MAC: return ("--offload-only",), None, "on another Mac if it can start them now"
    excluded = next((host for host in hosts if mac == lane_wait.farm_mac(host)), None)
    if excluded is None: return None
    if waits: return (), excluded, f"on a Mac other than {mac}, which gates the batch below them"
    return ("--if-free",), excluded, f"on a Mac other than {mac} if one can start them now"


def stuck(gate, at=None):
    """What `qk queue status` and the worker log say of a gate held up by dead guard entries, or None: its wait,
    since waitingSince, names a dead or uncertain entry and has lasted STUCK_SECONDS (#315)."""
    since = gate.get("waitingSince")
    if not isinstance(since, (int, float)) or not gate.get("stranded") or (at or time.time()) - since < STUCK_SECONDS:
        return None
    return (f"stuck: the gate has waited {ago(since, at)} for admission held up by dead or uncertain guard entries that "
            "admission has not recovered; `python3 scripts/qk admissions recover` lists them")


def latest(*times):
    """The latest of times that are known, or None when none is."""
    return max((at for at in times if at is not None), default=None)


class GateWatch:
    """The record of a running gate lane in the round's account (Queue.note): where it runs and what it waits for,
    by the lane's wait record (lane_wait) and its dispatch's record (remote.dispatched), and when its run here last
    moved, which a run of `qk unittest` says in its own folder (run_progress.moved) and a lane's command in its lane's
    log (run_progress.logs_moved), and neither in the gate's log, read
    every WATCH_POLL seconds. It says only that: the record is no verdict, which the lane's exit and receipt alone give. Nothing it
    reads or fails to read may end the gate."""
    def __init__(self, worker, lane, began, log):
        self.queue, self.checkout, self.role = worker.queue, worker.checkout, worker.role
        self.lane, self.began, self.log = lane, began, log
        # Whether the worker log has said it is stuck (stuck).
        self.said_stuck, self.say = False, worker.say
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self.every, name="gate watch", daemon=True)
        self.queue.note(lambda state: state.setdefault("gates", {}).__setitem__(self.role, {
            "lane": lane, "began": began, "entries": list(worker.gating), "head": worker.head(), "log": str(log),
            "mac": None, "waiting": None}))

    def look(self):
        from . import run_progress
        record = lane_wait.current(lane_wait.path_of(self.checkout, self.lane), self.began)
        waiting = lane_wait.words(record)
        # A lane still waiting for this Mac has no Mac yet: another Mac may still take it.
        where = record.get("mac") if record and not (waiting and record.get("mac") == HERE_MAC) else None
        try: host = remote.dispatched(self.checkout, self.lane, self.began)
        except Exception: host = None  # noqa: BLE001
        mac = lane_wait.farm_mac(host) if host else where
        since, stranded = record.get("since") if waiting else None, bool(waiting and record.get("stranded"))
        held_up = stuck({"waitingSince": since, "stranded": stranded})
        if held_up and not self.said_stuck:
            self.say(f"{self.lane}: {held_up}")
            self.queue.emit("gate.stuck", lane=self.lane, role=self.role, why=held_up, waitingSince=since)
        self.said_stuck = bool(held_up)
        # waitingOn is the Mac the wait is for, by the record's own words, which qk queue doctor reads (#89).
        self.queue.gate_note(self.role, mac=mac, waiting=waiting, waitingKind=record.get("kind") if waiting else None,
                             waitingOn=record.get("mac") if waiting else None, waitingSince=since, stranded=stranded,
                             progress=lane_progress.current(lane_wait.path_of(self.checkout, self.lane), self.began),
                             moved=latest(run_progress.moved(self.checkout, self.began),
                                          run_progress.logs_moved(self.checkout, self.began)))

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
        self.directory = common / "queuekeeper-queue"
        self.entries = self.directory / "entries"
        self.logs = self.directory / "logs"
        self.hold_file = self.directory / "hold"
        self.closed_file = self.directory / "closed.json"
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

    def emit(self, kind, **fields):
        """Write an event to the queue's journal (events), which explains the queue and never stops it."""
        return events.emit(self.directory, kind, **fields)

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
            current.pop("publishing", None)  # a settled entry's push is answered
            current.update(state=state, settled=time.time(), **fields)
            self.save(current)
        self.emit(f"entry.{state}", entry=current["id"], subject=current.get("subject"),
                  **{key: current.get(key) for key in EVENT_FIELDS})
        if state in (LANDED, REMOVED): git(self.root, "update-ref", "-d", f"refs/queuekeeper/queue/{entry['id']}")
        return current

    def record(self, entry, **fields):
        """Fields of a waiting entry's record, for the entry nobody has removed meanwhile."""
        with self.locked():
            current = self.read(entry["id"])
            if current and current["state"] in WAITING:
                current.update(fields)
                self.save(current)

    def mark_publishing(self, entries, marker):
        """Record marker, the push about to go (Worker.push), on each of entries still waiting."""
        for entry in entries: self.record(entry, publishing=marker)

    def drop_field(self, entries, key):
        """Drop key from the records of entries still waiting."""
        with self.locked():
            for entry in entries:
                current = self.read(entry["id"])
                if current and current["state"] in WAITING and key in current:
                    current.pop(key)
                    self.save(current)

    def mark(self, entries, state):
        """Queued or testing, for the entries nobody has removed meanwhile. An entry that goes back to the
        queue leaves behind the batch whose pass it held, and so that pass."""
        moved = []
        with self.locked():
            for entry in entries:
                current = self.read(entry["id"])
                if current and current["state"] in WAITING:
                    if current["state"] != state: moved.append(current["id"])
                    current["state"] = state
                    current.pop("passed", None)
                    if state != TESTING: current.pop("stopsWhenRemoved", None)
                    self.save(current)
        if moved: self.emit("entries.testing" if state == TESTING else "entries.requeued", entries=moved)

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
        recovery.write_hold(self.hold_file, why)
        print(f"queue: held: {why}", flush=True)
        self.emit("queue.held", why=why, **{"for": OWNER if why.startswith("for the owner") else SESSION})

    def closed(self):
        """Who closed the queue to new entries, when and why (close), or None while it is open. A record that cannot
        be read still closes the queue: whoever wrote it meant no entry to arrive."""
        try: found = json.loads(self.closed_file.read_text())
        except FileNotFoundError: return None
        except (OSError, ValueError): found = None
        if isinstance(found, dict) and isinstance(found.get("why"), str) and isinstance(found.get("by"), str): return found
        return {"schemaVersion": 1, "why": f"{self.closed_file} cannot be read", "by": "an unknown session", "at": None}

    def close(self, why, by):
        """Close the queue to new entries (vocaro/queuekeeper#32): add refuses each from now on, while the worker
        finishes the entries the queue has, and holds stay as they are. Under the entries' lock, so that an entry is
        either saved before this returns, and `qk queue status --idle` counts it, or refused. Returns the closure
        already there, which stays as it was, or None when this one was written."""
        with self.locked():
            already = self.closed()
            if already: return already
            closure = {"schemaVersion": 1, "why": why, "by": by, "at": round(time.time(), 3)}
            staged = self.closed_file.with_suffix(f".{os.getpid()}.tmp")
            staged.write_text(json.dumps(closure, indent=2) + "\n")
            os.replace(staged, self.closed_file)
        self.emit("queue.closed", why=why, by=by)
        return None

    def reopen(self):
        """Take new entries again, and return the closure that ended, or None when the queue was open."""
        with self.locked():
            was = self.closed()
            with contextlib.suppress(FileNotFoundError): self.closed_file.unlink()
        if was: self.emit("queue.reopened", by=was.get("by"), why=was.get("why"))
        return was

    @contextlib.contextmanager
    def worker(self, kept=None):
        """The repository's one worker, or None while another process is it. kept is the lock as a descriptor the
        worker this process replaces left open for it (Worker.move_to), which is then this worker's: the lock was
        never free between the two. While the lock is held, lock is its file."""
        path, lock = self.directory / "worker.lock", None
        if kept is not None:
            try:
                os.set_inheritable(kept, False)
                if os.path.samestat(os.fstat(kept), os.stat(path)): lock = os.fdopen(kept, "w")
                else: os.close(kept)
            except OSError: lock = None  # no descriptor of this process: the lock is taken as by any worker
        with (lock or path.open("w")) as lock:
            try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                yield False
                return
            self.lock = lock
            try: yield True
            finally: self.lock = None

    def working(self):
        with self.worker() as free: return not free

    def command(self, toolkit, *, if_waiting=False):
        """What runs this repository's worker on the toolkit at toolkit: `qk queue run` for the main checkout, from
        that toolkit's Python, as start starts one and as a worker starts itself again on a newer toolkit
        (Worker.move_to)."""
        script = ("import sys; sys.path.insert(0, sys.argv[1]); from queuekeeper.cli import main; "
                  "sys.exit(main(['--project', sys.argv[2], 'queue', 'run', *sys.argv[3:]]))")
        return [sys.executable, "-c", script, str(Path(toolkit) / "src/python"), str(self.main_checkout),
                *(["--if-waiting"] if if_waiting else [])]

    def launcher_label(self):
        """One login service per repository, shared by all its worktrees."""
        digest = hashlib.sha256(os.fsencode(self.directory.resolve())).hexdigest()[:24]
        return f"{LAUNCHER_BUNDLE}.{digest}"

    def launcher_plist(self, app):
        """Describe the signed service for this repository without installing it."""
        if sys.platform != "darwin": raise ValueError("the signed queue launcher requires macOS")
        app = Path(app).expanduser().resolve()
        executable = app / "Contents" / "MacOS" / LAUNCHER_PROGRAM
        if not executable.is_file(): raise ValueError(f"the queue launcher is missing at {executable}")
        checked = subprocess.run(["/usr/bin/codesign", "--verify", "--strict", LAUNCHER_REQUIREMENT, str(app)],
                                 capture_output=True, text=True, timeout=10)
        if checked.returncode: raise ValueError(f"the queue launcher needs an Apple signing identity: {checked.stderr.strip()}")
        toolkit = Path(text(TOOLKIT_ROOT, "rev-parse", "--path-format=absolute", "--git-common-dir")).parent
        variables = {key: os.environ[key] for key in ("PATH",) if key in os.environ}
        log = str(self.directory / "worker.log")
        return {
            "Label": self.launcher_label(),
            "AssociatedBundleIdentifiers": [LAUNCHER_BUNDLE],
            "ProgramArguments": [str(executable), *self.command(toolkit, if_waiting=True)],
            "WorkingDirectory": str(self.main_checkout),
            "EnvironmentVariables": variables,
            "RunAtLoad": True,
            "StartInterval": LAUNCHER_INTERVAL,
            "StandardOutPath": log,
            "StandardErrorPath": log,
        }

    def start_with_launcher(self, log):
        if sys.platform != "darwin": return False
        label = self.launcher_label()
        if not (Path.home() / "Library" / "LaunchAgents" / f"{label}.plist").is_file(): return False
        try:
            done = subprocess.run([LAUNCHCTL, "kickstart", f"gui/{os.getuid()}/{label}"],
                                  capture_output=True, text=True, timeout=10)
            if done.returncode == 0: return True
            error = done.stderr.strip() or f"launchctl exited {done.returncode}"
        except (OSError, subprocess.TimeoutExpired) as failure:
            error = str(failure)
        print(f"queue: signed launcher {label} could not start ({error}); starting a session worker", file=log, flush=True)
        return False

    def start(self):
        """Start a worker apart from this session, so that it outlives the session's turn and its
        task stop. One that finds another working exits at once."""
        if self.held() or not self.waiting() or self.working(): return
        with (self.directory / "worker.log").open("a") as log:
            if self.start_with_launcher(log): return
            started = subprocess.Popen(self.command(TOOLKIT_ROOT), cwd=self.main_checkout, stdin=subprocess.DEVNULL,
                                       stdout=log, stderr=subprocess.STDOUT, env=environment(), start_new_session=True)
            # It is meant to outlive this process, which must not wait for it, but one that ends first
            # stays a zombie child until reaped: a long-lived caller, `qk land --wait` or a test run, sees it.
            threading.Thread(target=started.wait, daemon=True).start()


def repository(root, named):
    """The repository `qk land --repo` names: a path, or the name of a project beside this one's
    main checkout, as a session fixing a dependency names it. The queue reads a project's configuration from
    origin/main, so a checkout whose working tree lags behind the commit that added queuekeeper.json is a
    project too (#188); a checkout with it in neither is named as a checkout, not as a wrong name."""
    candidates = [Path(named).expanduser(), Queue(root).main_checkout.parent / named]
    checkouts = []
    for path in candidates:
        top = git(path, "rev-parse", "--path-format=absolute", "--show-toplevel")
        if top.returncode: continue
        if (path / "queuekeeper.json").is_file(): return path.resolve()
        # A directory inside a checkout is not that checkout, whose origin/main would be another project's.
        if Path(top.stdout.strip()).resolve() != path.resolve(): continue
        if has_config(path, MAIN): return path.resolve()
        checkouts.append(path.resolve())
    if checkouts:
        raise ValueError(f"--repo {named}: {checkouts[0]} is a Git checkout, but neither its working tree nor its "
                         "origin/main, as last fetched there, has a queuekeeper.json: fetch it if the project "
                         "has adopted Queuekeeper since, or give another checkout's path")
    raise ValueError(f"--repo {named} names no project: give a checkout's path, or the name of a project beside "
                     f"{Queue(root).main_checkout}")


def land(root, args, directory=None):
    if getattr(args, "json", False):
        from . import agent_cli
        details = {"root": root}
        return agent_cli.execute(root, "land", lambda: _land(root, args, directory, details),
                                 reference=args.reference or "HEAD", details=details)
    return _land(root, args, directory)


def _land(root, args, directory=None, details=None):
    """`qk land`: add to this repository's queue or, with --repo, another's, whose own queue, lanes and
    worker take it exactly as they take that repository's own sessions' commits. With another
    repository's main checkout HEAD would be whatever it has checked out, so the commit is named.
    directory, the command's current directory, must be in this repository unless --repo names one:
    each project's launcher lands its own repository, so another project's `scripts/qk land` run from
    here would queue that project's HEAD, or report it landed (vocaro-engineering#210)."""
    elsewhere = None if args.repo or directory is None else other_repository(root, directory)
    if elsewhere is not None:
        if details is not None: details["refused"] = "current directory belongs to another repository"
        print(f"queuekeeper: this queuekeeper lands {root}, but the current directory is in {elsewhere}: run that "
              "checkout's own scripts/qk land, or name the commit with --repo", file=sys.stderr)
        return 1
    target = root
    if args.repo:
        target = repository(root, args.repo)
        if args.reference is None:
            raise ValueError(f"--repo lands a commit you name: `qk land --repo {args.repo} BRANCH`, since HEAD "
                             "there is another session's")
    if details is not None: details["root"] = target
    deadline = time.monotonic() + args.limit
    while True:
        try:
            identifier = add(target, args.reference or "HEAD", args.note, quiet_wait=args.wait,
                             front=getattr(args, "front", False), no_gate=getattr(args, "no_gate", False),
                             why=getattr(args, "why", None))
            break
        except Refused as error:
            if details is not None:
                details.update(refused=str(error), waitsFor=list(error.waits_for), closed=error.closed)
            print(f"queuekeeper: {error}", file=sys.stderr)
            if not args.wait or not error.waits_for: return 1
            # Refused for entries ahead alone: wait for them, then check again against the whole queue, which may
            # name an entry queued meanwhile (vocaro-engineering#302).
            print(f"qk land --wait: waits for {', '.join(error.waits_for)}, then checks again against the whole "
                  "queue", file=sys.stderr, flush=True)
            waited = wait_for(target, error.waits_for, max(0.0, deadline - time.monotonic()))
            if waited == resources.BUSY: return waited
    if details is not None:
        details["id"] = identifier
        details.pop("refused", None)
        details.pop("waitsFor", None)
        details.pop("closed", None)
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
    """After qk land's result, the gate over its budget and main past its release commit limit, by main's
    settings (budgets.py)."""
    # While this command waited, main may have landed settings validated by a newer toolkit.
    # The advisory checks read their own fields, so the waiter's schema must not reject main.
    try:
        # A first landing that came back leaves main with no configuration, and so nothing to warn of.
        if not has_config(root, MAIN): return
        config = json.loads(config_source(root, MAIN))
        if not isinstance(config, dict): raise ValueError("queuekeeper.json must be an object")
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"warning: could not read landing budget configuration: {error}", flush=True)
        return
    try:
        entries = Queue(root).all()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"warning: could not read queue entries for landing budget checks: {error}", flush=True)
        return
    for warning in budgets.landing_warnings(root, config, entries):
        print(f"warning: {warning}", flush=True)


def entry_slug(branch, subject, message=""):
    """A short ref-safe name from the branch (or subject for detached/main commits), with an issue when named.
    Issue references are labels only; this does not infer or change GitHub closing claims.
    """
    name = re.sub(r"^(?:refs/heads/)?(?:(?:codex|agent|feature|fix|bugfix)/)?", "", branch or "")
    if name in ("", "HEAD", "main", "master") or re.fullmatch(r"[0-9a-f]{7,40}", name): name = subject
    issue = re.search(r"#([1-9][0-9]*)\b", f"{branch or ''} {subject} {message}")
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    name = re.sub(r"[^a-z0-9]+", "-", name).strip("-")[:56].rstrip("-") or "change"
    return f"issue-{issue[1]}-{name}" if issue else name


def entry_identifier(queue, branch, subject, message=""):
    """Called under the queue lock: never reuse a name, including one belonging to a settled entry."""
    slug = entry_slug(branch, subject, message)
    # Four digits normally suffice. Expand if repeated collisions exhaust that space for a popular name.
    for digits in (4, 6, 8):
        for _ in range(100):
            candidate = f"qk{secrets.randbelow(10 ** digits):0{digits}d}-{slug}"
            if not (queue.entries / f"{candidate}.json").exists() and git(
                    queue.root, "show-ref", "--verify", "--quiet", f"refs/queuekeeper/queue/{candidate}").returncode == 1:
                return candidate
    raise RuntimeError("could not allocate a unique queue entry ID")


def add(root, reference, note=None, quiet_wait=False, front=False, no_gate=False, why=None):
    """Put the commits reference has and origin/main lacks in the queue, and return at once with the
    entry's ID, or None when there is nothing to queue. Whether it may be queued is what a round would
    find: origin/main's settings, and origin/main's lanes or, until it has any, the commit's, or the commit's
    settings while origin/main has no queuekeeper.json, as for a project's first landing (#10), and the round's
    integration of it after the entries ahead of it, which a conflict refuses before it is queued (check). front and
    no_gate, qk land's emergency options --front and --no-gate, and why, the reason --why gives (ahead), go
    in the entry; an entry already queued with the same tip takes them. A queue closed to new entries (Queue.close)
    refuses every new entry, one with --front or --no-gate and one that would replace its branch's earlier entry
    included, and leaves what it has as it was; a tip it already holds is no new entry, and is followed as before."""
    flags = ahead(front, no_gate, why)
    queue = Queue(root)
    reach(root, *FETCH, say=lambda words: print(f"queue: {words}", file=sys.stderr), check=True)
    tip = text(root, "rev-parse", "--verify", f"{reference}^{{commit}}")
    if has_config(root, MAIN): config = config_at(root, MAIN)
    elif has_config(root, tip):
        config = as_batch(config_at(root, tip))
        print(f"queue: origin/main has no queuekeeper.json yet, so the queue reads {tip[:12]}'s for this landing "
              "alone, and main's from then on", file=sys.stderr)
    else:
        raise ValueError(f"neither origin/main nor {tip[:12]} has a queuekeeper.json, so the queue has nothing to run: "
                         "commit one with a gate lane and land that first (README, Getting started)")
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
    # A closed queue takes nothing new, whatever its options, and replaces nothing (vocaro/queuekeeper#32): before the
    # checks, which cost a trial and a preflight run, and again under the lock that saves the entry.
    closure = queue.closed()
    if closure: raise closed_refusal(tip, closure)
    # A branch added again replaces what it added before; main names nobody's work in particular.
    replaced = [entry for entry in waiting
                if branch and branch not in ("main", "HEAD") and entry.get("branch") == branch]
    # A negated closing claim closes its issue on landing, whatever a gate finds (#286): refused before anything else.
    claims_before_queued(root, base, tip)
    # What a round would find, before anything is queued or replaced: a refused entry leaves the queue as it was. First
    # qk unittest's own checks before a run, which a gate holding it would fail in seconds and bisect blind (#290).
    checked_before_running(root, tip, gating)
    check(root, queue, {"id": "new", "tip": tip, "commits": commits},
          [] if front else round_order([entry for entry in waiting if entry not in replaced]), front)
    # Then the preflight lanes, on its commits replayed onto origin/main, which take seconds where a slip costs a round.
    preflight_before_queued(root, queue, tip, gating)
    # Which gate it will ride, found before anything is queued, so that no slip here leaves it queued unsaid (#118).
    # The round reads main's settings, or the batch's less what only main may say (settings), as gating stands for.
    riding = None if no_gate else rides(config if gating is config else as_batch(config_at(root, tip)), {
        path for path in text(root, "log", "--no-walk", "--format=", "--name-only", "--no-renames",
                              *commits).splitlines() if path})
    for entry in replaced:
        remove(queue, entry["id"], quiet=True)
        print(f"queue: replaced {entry['id']}, the branch's earlier commits", file=sys.stderr)
        if entry["state"] == TESTING: print(f"queue: {stopping(queue, entry)}", file=sys.stderr)
    now = time.time()
    subject = text(root, "log", "-1", "--format=%s", tip)
    message = text(root, "log", "--format=%B", f"{base}..{tip}")
    entry = {"id": None, "tip": tip, "base": base, "commits": commits, "branch": branch or None, "note": note,
             "subject": subject, "submitted": now, "state": QUEUED,
             "directory": str(root), **resources.coding_agent(), **({"replays": replays} if merges else {}), **flags}
    with queue.locked():
        # Closed since the check above: Queue.close takes this lock, so no entry is saved once it has returned.
        closure = queue.closed()
        if closure:
            raise closed_refusal(tip, closure, "".join(
                f"; {entry['id']}, the branch's earlier commits, was taken out before it closed" for entry in replaced))
        identifier = entry_identifier(queue, branch, subject, message)
        entry["id"] = identifier
        text(root, "update-ref", f"refs/queuekeeper/queue/{identifier}", tip)
        queue.save(entry)
    queue.emit("entry.queued", entry=identifier, subject=entry["subject"], commits=len(commits), branch=entry["branch"],
               note=note, **flags)
    print(f"queued {identifier}: {len(commits)} commit(s), {entry['subject']}")
    if riding: print(f"queue: {identifier} {riding}")
    for name in OPTIONS:
        if name in flags: print(f"queue: {identifier} {asked_for(name)}{because(flags)}")
    why = queue.held()
    if why: print(f"queue: {identifier} waits, since the queue is held {why}", file=sys.stderr)
    queue.start()
    if not quiet_wait:
        print("nothing else is needed for it to land; to be told at once if it comes back, run as a background "
              f"command: python3 {shlex.quote(str(root))}/scripts/qk queue wait {identifier}")
    return identifier


def rides(config, changed):
    """Which gate an entry whose own commits change the paths changed will ride, by config, the settings a round reads
    for it, for qk land's line: the docs lanes alone, which docs_lanes gives for an entry of documents, or the code
    gate, with the first path that puts it there and the docs lanes that run ahead of the gate for its documents
    (docs_also). A session that queued a README change with a stylesheet and screenshots expected a documentation
    landing, and found out only by waiting that it rode the code gate (vocaro/queuekeeper#118)."""
    changed = sorted(changed)
    docs = docs_lanes(config, changed)
    named = unread_named(config, changed) if docs else {}
    if docs: return (f"is documentation only: lands on {spoken(docs)}, not the code gate"
                     + (f", main's queue.unread.paths naming {named_said(named)}" if named else ""))
    queue = config.get("queue", {})
    if unread_declared(queue) is False:
        why = f"main's queue.{'unread' if 'unread' in queue else 'docsOnly'} is false, so every entry does"
    elif not changed: why = "its commits change no file"
    else: why = f"{next(path for path in changed if not docs_lanes(config, [path]))} is not documentation"
    also = [lane for lane in lanes_of(config, changed) if lane not in lanes_of(config)]
    selected, named = affected_lanes(config, changed), gate_lanes(config)
    narrowed = (f"; of the gate lanes, only {spoken(selected)}, which main's queue.affected selects for its paths"
                if selected != named else "")
    return f"rides the code gate: {why}" + narrowed + (f"; {spoken(also)} run{'s' if len(also) == 1 else ''} too, "
                                                       "ahead of it, for its documents" if also else "")


def spoken(lanes):
    """lanes as a line says them, such as the preflight and docs lanes."""
    named = lanes[0] if len(lanes) == 1 else ", ".join(lanes[:-1]) + " and " + lanes[-1]
    return f"the {named} lane{'s' if len(lanes) > 1 else ''}"


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
    """What an entry's option, a name of OPTIONS, does, for qk land's lines and `qk queue status`."""
    return ("is tested next, ahead of everything waiting, as a batch of its own" if name == "front" else
            "lands alone on the preflight lanes, without the gate, and is recorded as ungated")


def because(entry):
    """The reason --why gave an entry's options, for a line that names them."""
    return f", because {entry['aheadWhy']}" if entry.get("aheadWhy") else ""


def promote(queue, entry, flags):
    """Give a queued entry the options flags holds, as qk land does an entry already holding its tip. A testing entry
    that then holds --front and --no-gate both takes them too: the worker takes it out of its round while that round's
    gate waits to start, as for an entry arriving with them (Worker.first_ungated); otherwise it lands with its round,
    or first once that round queues it again (vocaro/queuekeeper#26). Any other option of a testing entry, and any of
    one that passed, changes nothing, and the line says how to land it alone without the gate."""
    with queue.locked():
        current = queue.read(entry["id"])
        testing = bool(current) and current["state"] == TESTING and all({**current, **flags}.get(name)
                                                                         for name in OPTIONS)
        if current and (current["state"] == QUEUED or testing):
            current.update(flags)
            queue.save(current)
    if current and (current["state"] == QUEUED or testing):
        for name in OPTIONS:
            if name in flags: print(f"queue: {entry['id']} now {asked_for(name)}{because(current)}")
        if testing:
            print(f"queue: {entry['id']} is testing: the worker takes it out of its round within about "
                  f"{REMOVAL_POLL:g}s while that round's gate waits to start, and gates the rest again on top of it; "
                  "a gate that has started finishes first, and it lands with that round if the round passes")
        return
    print(f"queue: {entry['id']} is {current['state'] if current else 'gone'}, so "
          f"{' and '.join(OPTIONS[name] for name in OPTIONS if name in flags)} changes nothing now", file=sys.stderr)
    if current and current["state"] == TESTING:
        print("queue: to land it alone without the gate, give --front and --no-gate together, which take it out of a "
              f"round whose gate waits to start, or run `qk queue remove {entry['id']}` and land it again with "
              "--no-gate; its round then gates again without it", file=sys.stderr)


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
    else: git(queue.root, "update-ref", "-d", f"refs/queuekeeper/queue/{identifier}")
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
    if entry["state"] in WAITING and entry.get("publishing"):
        line += f"\n         {unverified_said(entry['publishing'])}"
    if entry.get("docsOnly"):
        line += f"\n         docs lanes only ({', '.join(entry['docsOnly'])}): no gate of that commit"
        if entry.get("docsNamed"): line += f"; queue.unread.paths named {named_said(entry['docsNamed'])}"
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
    """What `qk queue status` says of the worker's round from its account (ROUND_FILE): whether it bisects and how
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
        if gate.get("waiting"):
            line += f"\n    waiting: {gate['waiting']}"
            if gate.get("waitingSince"): line += f", for {ago(gate['waitingSince'], at)}"
        for observation in gate.get("progress", []):
            if lane_progress.valid(observation, gate.get("began"), at):
                line += "\n    progress: " + lane_progress.words(observation)
        if stuck(gate, at): line += f"\n    {stuck(gate, at)}"
        if gate.get("log"): line += f"\n    log: {gate['log']}"
        lines.append(line)
    failure = state.get("failure")
    if isinstance(failure, dict):
        line = (f"  failed gate ({', '.join(failure.get('entries', [])) or 'no entry'}): {failure.get('lane')} lane "
                f"failed; {failure_words(failure, at, timed=True)}")
        if failure.get("log"): line += f"\n    log: {failure['log']}"
        if failure.get("rerunLog"): line += f"\n    rerun log: {failure['rerunLog']}"
        lines.append(line)
    if state.get("waiting"):
        lines.append(f"  waiting for {state['waiting']}"
                     + (f", for {ago(state['waitingSince'], at)}" if state.get("waitingSince") else ""))
        lines += [f"    held by {holder_line(holder, at)}" for holder in state.get("holders") or [] if isinstance(holder, dict)]
    return lines


def failure_words(failure, at=None, timed=False):
    """What the worker does with a gate that failed (Worker.rerun), by the round's account: reading its failure, its
    failed tests waiting to rerun alone, or the rerun that runs, which of how many, and what it waits for; with timed,
    how long it has run. A gate that failed is never said to have not started (vocaro/queuekeeper#41)."""
    tests = failure.get("tests")
    if not isinstance(tests, list): return "the worker reads its failure to tell a flake from a real one"
    if not failure.get("test"):
        return (f"{len(tests)} failed test{'' if len(tests) == 1 else 's'}, {', '.join(tests)}, to rerun alone up to "
                f"{failure.get('reruns')} times, which wait to start")
    said = f"rerunning {failure['test']} alone, rerun {failure.get('attempt')} of {failure.get('reruns')}"
    if len(tests) > 1: said += f", test {failure.get('number')} of {len(tests)}"
    if timed and isinstance(failure.get("began"), (int, float)): said += f", running {ago(failure['began'], at)}"
    if failure.get("waiting"): said += f"; waiting: {failure['waiting']}"
    return said


def rerun_waits(state):
    """state, the round's account, with what a running rerun of a failed test waits for (failure_words), as its
    admission writes it to the wait record the worker named for it (Worker.rerun_each, lane_wait)."""
    failure = (state or {}).get("failure")
    if isinstance(failure, dict) and failure.get("waitRecord"):
        try: failure["waiting"] = lane_wait.words(lane_wait.current(failure["waitRecord"], failure.get("began")))
        except Exception: failure["waiting"] = None  # noqa: BLE001
    return state


def holder_line(holder, at=None):
    """One holder of the shared checkout as described_holders recorded it, in words: what it is, its PID and how long
    it has run."""
    if holder.get("more"): return f"{holder['more']} more"
    since = holder.get("since")
    return f"{holder.get('what')}, PID {holder.get('pid')}" + \
        (f", running {ago(since, at)}" if isinstance(since, (int, float)) else "")


def checkout_holder(process, how, path):
    """What process, which runs from the shared checkout by how and path (disk.runners), is, in words: the queue worker
    of the project it serves, a build through the Apple plugin's wrapper, a `qk` command, or else its program and what
    ties it there. On 2026-11-15 and 2026-11-17 the worker's log named such holders only by PID and an argument, and
    `qk queue status` not at all (vocaro/queuekeeper#159)."""
    worker = WORKER_OF.search(process.args)
    if worker: return f"the queue worker for {Path(worker.group(1)).name}"
    shimmed = SHIMMED.search(process.args)
    if shimmed: return f"a lane's {shimmed.group(1)} through the Apple plugin's wrapper"
    command = QK_COMMAND.search(process.args)
    if command and command.group(1) == "serve": return "a dashboard (`qk serve`) that does not restart itself"
    if command: return f"a `qk {command.group(1)}` command"
    return f"{process.command or process.args.split(' ')[0] or 'a process'} ({how} {path})"


def started_at(pids, now=None):
    """{pid: when it started, as an epoch time} for each of pids that still runs, by ps's elapsed time."""
    if not pids: return {}
    try:
        listed = subprocess.run(["ps", "-o", "pid=,etime=", "-p", ",".join(str(pid) for pid in pids)],
                                capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError): return {}
    now, found = now or time.time(), {}
    for line in listed.splitlines():
        pid, _, etime = line.strip().partition(" ")
        seconds = offload_host.elapsed(etime.strip())
        if pid.isdigit() and seconds != float("inf"): found[int(pid)] = now - seconds
    return found


def described_holders(users):
    """What the round's account records of the processes that hold the shared checkout (Worker.stays), the first
    SHOWN_USERS of them: [{"pid", "what", "since"}], and {"more": N} for the rest."""
    shown = users[:SHOWN_USERS]
    since = started_at([process.pid for process, _, _ in shown])
    found = [{"pid": process.pid, "what": checkout_holder(process, how, path), "since": since.get(process.pid)}
             for process, how, path in shown]
    return found + ([{"more": len(users) - SHOWN_USERS}] if len(users) > SHOWN_USERS else [])


def snapshot(root, recent=5):
    """The queue's state as `qk queue status --json` prints it, which the visibility features read too:
    the worker, any hold, the round's account, the entries waiting, the last recent settled, the
    IDs of those landed ungated, whether the queue is idle (status's --idle), and who closed it to new entries and
    why, or None while it is open (Queue.closed)."""
    return reading(root, recent)[0]


def reading(root, recent=5):
    """(snapshot, the entries landed ungated that no gate on main has covered since)."""
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
    current = rerun_waits(queue.round_state()) if working else None
    for gate in (current or {}).get("gates", {}).values():
        if "progress" in gate:
            gate["progress"] = [record for record in gate["progress"] if lane_progress.valid(record, gate.get("began"))]
        if isinstance(gate.get("began"), (int, float)): gate["runningSeconds"] = round(time.time() - gate["began"])
        if stuck(gate): gate["stuck"] = stuck(gate)
    # Idle is no live worker and nothing queued, testing or passed (vocaro/queuekeeper#21). A worker between rounds
    # with nothing waiting is not idle: what ends it ends whatever it starts next. A held queue with entries waiting is
    # not idle either. The worker's lock is a flock, so a dead worker's lock file holds nothing and counts as none.
    return {"schemaVersion": 1, "worker": working, "held": why,
            "recovery": recovery.in_project(
                recovery.for_queue(recovery.read_hold(queue.hold_file, why), working=working, waiting=waiting), root),
            "round": current, "waiting": waiting, "recent": done,
            "ungated": [entry["id"] for entry in ungated], "idle": not working and not waiting,
            # Apart from a hold: a closed queue works on what it has, and is idle once that is done (#32).
            "closed": queue.closed()}, ungated


def status(root, as_json=False, recent=5, idle=False):
    """Print the queue's state, in words or as JSON. Its exit status is 0, or with idle, 0 only when the queue is
    idle (reading) and BUSY otherwise, so that a script can ask whether ending the worker would end a gate
    (vocaro/queuekeeper#21)."""
    state, ungated = reading(root, recent)
    result = 0 if not idle or state["idle"] else resources.BUSY
    if as_json:
        print(json.dumps(state, indent=2))
        return result
    working, why, current, waiting, done = (state[key] for key in ("worker", "held", "round", "waiting", "recent"))
    print(f"worker: {'running' if working else 'none'}" + (f"; held {why}" if why else ""))
    if state["closed"]:
        print(f"{closed_words(state['closed'])}; entries already in it finish, and `qk queue reopen` takes new ones again")
    if waiting and not working and not why: print("waiting with no worker: `qk queue resume` starts one")
    for line in round_lines(current or {}): print(line)
    for entry in ungated:
        print(f"ungated on main: {entry['id']} landed {entry['landedAs'][:12]} without the gate, which the next gate "
              "on main covers")
    for entry in waiting + done:
        print(describe(entry))
        if entry["state"] == QUEUED and entry.get("front") and entry.get("noGate"):
            line = ahead_of_round(current or {})
            if line: print(f"         {line}")
    return result


def ahead_of_round(state, at=None):
    """What an entry qk land --front --no-gate put first waits for, by the round's account (round_lines), or None
    between rounds: the gates that run, which finish first, or else the round whose gates wait to start, which the
    worker stops within REMOVAL_POLL seconds (Worker.first_ungated, vocaro/queuekeeper#26). A stacked gate holds up no
    landing."""
    gates = [gate for role, gate in (state.get("gates") or {}).items() if role != "stacked"]
    running = [gate for gate in gates if not gate.get("waiting")]
    if running:
        return "; ".join(f"waits for the running gate of {', '.join(gate.get('entries', [])) or 'no entry'}, which "
                         f"has run {ago(gate.get('began', 0), at)}" for gate in running)
    if gates:
        return (f"lands ahead of the round of {', '.join(state.get('entries', [])) or 'no entry'}, whose gate waits to "
                f"start: the worker stops it within about {REMOVAL_POLL:g}s")
    return None


def situation(queue, entry, state, at=None):
    """Where entry, still waiting, stands by the round's account (round_lines), as (what makes it this situation and
    no other, the words that follow its ID). state is the round's account, or None when no worker runs. The first
    leaves out times and counts that change while nothing else does, so that a wait says a situation once (Follow)."""
    marker = entry.get("publishing")
    if marker and (marker.get("unanswered") or state is None):
        return (PUBLICATION_UNVERIFIED,), unverified_said(marker)
    if marker: return ("pushing",), f"is being pushed as {marker['attempts'][-1]['head'][:12]}"
    if entry["state"] == PASSED: return ("passed",), "passed its gate and waits for the push"
    if state is None: return ("no worker",), f"is {entry['state']}, and no worker runs yet: the wait starts one"
    identifier, name = entry["id"], state.get("round")
    for role, gate in (state.get("gates") or {}).items():
        if identifier not in gate.get("entries", []): continue
        where = f"{gate.get('lane')} lane on {gate.get('mac') or 'no Mac yet'}"
        if gate.get("waiting"): where += f", waiting: {gate['waiting']}"
        key = ("gate", name, role, gate.get("lane"), gate.get("mac"), gate.get("waitingKind") or bool(gate.get("waiting")))
        count = entries_word(len(gate.get("entries", [])))
        if role == "stacked": return key, f"gates stacked on round {name}'s batch, {count}: {where}"
        if role in ("older half", "newer half"):
            suspects = (state.get("bisect") or {}).get("suspects")
            return key, (f"had its batch halved after a failure: the {role} of round {name}, {count}"
                         + (f" of {suspects}" if suspects else "") + f": {where}")
        return key, f"is in round {name}, {count}, whose gate runs: {where}"
    failure = state.get("failure") if isinstance(state.get("failure"), dict) else {}
    if identifier in failure.get("entries", []):
        # A gate that failed is being weighed, which no line may call a gate that has not started (#41).
        waits = f"; the round waits for {state['waiting']}" if state.get("waiting") else ""
        log = failure.get("rerunLog") or failure.get("log")
        key = ("failure", name, failure.get("test"), failure.get("attempt"), isinstance(failure.get("tests"), list),
               bool(failure.get("waiting")), bool(waits))
        return key, (f"is in round {name}, {entries_word(len(failure['entries']))}, whose gate failed in its "
                     f"{failure.get('lane')} lane: {failure_words(failure)}{waits}" + (f"; see {log}" if log else ""))
    if identifier in state.get("entries", []):
        count, suspects = entries_word(len(state["entries"])), (state.get("bisect") or {}).get("suspects")
        waits = f"; the round waits for {state['waiting']}" if state.get("waiting") else ""
        said = (f"had its batch halved after a failure: the older half of round {name}, {count} of {suspects}"
                if suspects else f"is in round {name}, {count}")
        return ("round", name, bool(suspects), bool(waits)), f"{said}, whose gate has not started{waits}"
    if entry["state"] == QUEUED and entry.get("front") and entry.get("noGate"):
        ahead = ahead_of_round(state, at)
        if ahead: return ("ahead", name, bool(state.get("gates"))), ahead
    ahead = [other for other in state.get("entries", []) if (queue.read(other) or {}).get("state") in WAITING]
    if name and ahead:
        return (("behind", name, tuple(ahead)),
                f"waits behind round {name} ({', '.join(ahead)}), which has run {ago(state.get('started', 0), at)}")
    return ("between", entry["state"]), f"is {entry['state']}, and the worker is between rounds"


def round_facts(state, identifier, at=None):
    """What `qk queue status` says of the round an entry waits on (round_lines), on one line: the round, and the
    gates that hold the entry, or every gate when it is in none of them; no log path, which status names."""
    gates = (state or {}).get("gates") or {}
    own = {role: gate for role, gate in gates.items() if identifier in gate.get("entries", [])}
    lines = round_lines({**(state or {}), "gates": own or gates}, at)
    parts = [part.strip() for line in lines for part in line.splitlines()
             if not part.strip().startswith(("log: ", "rerun log: "))]
    return "; ".join(parts)


class Follow:
    """What `qk land --wait` and `qk queue wait` print of an entry while it waits (vocaro/queuekeeper#41): a line
    each time its situation changes, never the same situation twice, and, after HEARTBEAT seconds with no line, a
    heartbeat that says what it still waits for, with the facts `qk queue status` shows of its round."""
    def __init__(self, identifier, clock=time.monotonic):
        self.identifier, self.clock = identifier, clock
        self.said, self.last = set(), clock()

    def look(self, queue, entry, state, at=None):
        """The line to print now for entry, or None."""
        key, words = situation(queue, entry, state, at)
        now = self.clock()
        if key not in self.said:
            self.said.add(key)
            self.last = now
            return f"{self.identifier} {words}"
        if now - self.last < HEARTBEAT: return None
        self.last = now
        facts = round_facts(state, self.identifier, at) if state else ""
        return f"heartbeat: {self.identifier} {words}" + (f"; {facts}" if facts else "")


# A line of a check's output that names what it found by PATH:LINE:, as qk docs check's and a preflight lane's do
# (Worker.main_fails).
FINDING = re.compile(r"^\S+:\d+:")


def refused_modules(line):
    """The test modules a refusal of qk unittest's names (parallel.refusals), by file name, whatever checkout it
    was printed in."""
    said = line.removeprefix("queuekeeper unittest: ")
    for ending in (" must import ", " does not parse "):
        if ending in said: return set(said.split(ending, 1)[0].split(", "))
    return set()


def refusal_key(line):
    """A refusal of qk unittest's without the directories its paths name, so that the same refusal printed in two
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
    """The logs of the newest DOCTOR_LOGS gates of lanes, qk unittest lanes, newest first, back to the last that gave
    a verdict, OK or FAILED: [(name, the refusal it printed or None, its qk unittest verdict or None)]. Leave out
    gates still running and stacked gates whose last lane no Mac admitted."""
    running = {gate.get("log") for gate in (queue.round_state() or {}).get("gates", {}).values()} if working else set()
    logs = []
    for lane in lanes:
        for path in queue.logs.glob(f"*-{lane}.log"):
            if str(path) in running: continue
            with contextlib.suppress(OSError): logs.append((path.stat().st_mtime, path.name, path))
    found = []
    for _, name, path in sorted(logs, reverse=True):
        if len(found) == DOCTOR_LOGS: break
        said = path.read_text(errors="replace")
        if said.endswith(STACK_ADMISSION_DECLINED + "\n"): continue
        summaries = UNITTEST_SUMMARY.findall(said)
        if summaries and "UNMEASURED: " not in summaries[-1]: break
        refused = REFUSAL.findall(said)
        found.append((name, refused[-1] if refused else None, summaries[-1] if summaries else None))
    return found


def doctor(root, as_json=False):
    """`qk queue doctor`: print diagnose's findings, and idle_waits', or that the queue is healthy. 0 when it finds the
    queue healthy, 1 when it finds anything."""
    findings, waiting, working = diagnose(root)
    hints = admission_hints(root) + checkout_hints(root) if working else []
    if working:
        held_back, unknown = idle_waits(root)
        findings, hints = findings + held_back, hints + unknown
    short, unknown = memory_findings()
    findings, hints = findings + short, hints + unknown
    if as_json:
        print(json.dumps({"healthy": not findings, "waiting": len(waiting), "worker": working, "findings": findings,
                          "hints": hints}, indent=2))
        return 1 if findings else 0
    if not findings:
        print(f"qk queue doctor: healthy: {entries_word(len(waiting))} waiting, worker "
              f"{'running' if working else 'not needed'}, and no gate since the last verdict ran nothing")
        for hint in hints: print(f"note: {hint}")
        return 0
    print(f"qk queue doctor: {len(findings)} finding{'' if len(findings) == 1 else 's'}, and it changed nothing:")
    for finding in findings:
        print(f"{finding['signal']}: {finding['diagnosis']}\n  next: {finding['remedy']}")
    for hint in hints: print(f"note: {hint}")
    return 1


def health_of(host):
    """offload_host.host_health's reading of host, another Mac, or with host None of this Mac; None when it cannot be
    read, as from another Mac that cannot be reached or whose toolkit predates the reading."""
    if not host: return offload_host.host_health()
    try: return remote.reading_there(host, health=True)[0].get("health")
    except remote.Unavailable: return None


def memory_findings(read=None):
    """(findings, notes): a finding for each Mac, this one and each of the farm's that take work now, that is short of
    memory (host_health.assessed), naming its largest processes and how the owner frees them; a note for a Mac whose
    reading could not be made. On 2026-11-15 another Mac's fseventsd held 34 of its 64 GB and swap was full, and a
    gate timed out there with no test failed. It changes nothing: stopping a process takes the owner's password."""
    read = read or health_of
    findings, notes = [], []
    try: hosts = farm.destinations()
    except ValueError: hosts = ()
    for mac, asked in ((lane_wait.HERE, None), *((lane_wait.farm_mac(host), host) for host in hosts)):
        try: reading = read(asked)
        except Exception: reading = None  # noqa: BLE001 - a reading never fails the doctor
        if reading is None:
            notes.append(f"whether {mac} is short of memory is unknown, as its processes could not be read there")
            continue
        found = host_health.assessed(reading, mac)
        if found:
            findings.append({"signal": "short of memory", "diagnosis": found[0], "remedy": found[1], "mac": mac,
                             "largest": (reading.get("largest") or [])[:host_health.NAMED]})
    return findings, notes


def admission_hints(root, at=None):
    """Where to look next for each running gate that waits for admission, from the round's account (GateWatch): a
    pointer to `qk admissions now`, which shows who holds and who waits on each Mac (vocaro-engineering#342). Doctor
    neither runs that report nor counts a wait as a finding: a gate waiting for admission is how a busy Mac looks."""
    try: state = Queue(root).round_state() or {}
    except Exception: return []  # noqa: BLE001 - a pointer never fails the doctor
    hints = []
    for role, gate in sorted((state.get("gates") or {}).items()):
        if not isinstance(gate, dict) or gate.get("waitingKind") != lane_wait.ADMISSION: continue
        since = gate.get("waitingSince")
        waited = f" {ago(since, at)}" if isinstance(since, (int, float)) else ""
        hints.append(f"the {role} gate's {gate.get('lane') or 'lane'} has waited{waited} for {gate.get('waiting')}; "
                     "`python3 scripts/qk admissions now` shows who holds and who waits for admission on each Mac")
    return hints


def checkout_hints(root, at=None):
    """A note, from the round's account, while the worker waits for the shared checkout to follow origin/main (catch_up):
    how long, and what holds it, in words (holder_line). Not a finding: the checkout follows by itself once they end."""
    try: state = Queue(root).round_state() or {}
    except Exception: return []  # noqa: BLE001 - a note never fails the doctor
    holders = [holder for holder in state.get("holders") or [] if isinstance(holder, dict)]
    if not holders or not state.get("waiting"): return []
    since = state.get("waitingSince")
    waited = f" {ago(since, at)}" if isinstance(since, (int, float)) else ""
    return [f"the worker has waited{waited} for {state['waiting']}; held by "
            + "; ".join(holder_line(holder, at) for holder in holders)]


def idle_waits(root, at=None, busy=None, read=None):
    """(findings, notes) for each running gate that has waited IDLE_WAIT_SECONDS or more for admission on a Mac whose
    CPU busy share is under its limit (vocaro/queuekeeper#89): what it waits behind, in the waiter's own words, and each
    holder there, with its command, PID, hold time, CPU use and where its work comes from (its repository and
    worktree), so that the session that owns it can be found and asked. busy reads the Mac's busy share, as the gate's
    busy sampler does (busy_of); read reads its holders, as `qk admissions now` does (holders_on). A Mac at or above
    its limit is busy, and its wait no finding: a gate waiting for admission is how a busy Mac looks. One whose busy
    share or holders cannot be read is said to be unknown, never idle. It changes nothing."""
    busy, read, at = busy or busy_of, read or holders_on, time.time() if at is None else at
    try: state = Queue(root).round_state() or {}
    except Exception: return [], []  # noqa: BLE001 - a reading never fails the doctor
    findings, notes = [], []
    for role, gate in sorted((state.get("gates") or {}).items()):
        if not isinstance(gate, dict) or gate.get("waitingKind") != lane_wait.ADMISSION: continue
        since, mac = gate.get("waitingSince"), gate.get("waitingOn")
        if not isinstance(since, (int, float)) or at - since < IDLE_WAIT_SECONDS: continue
        host = lane_wait.host_of(mac)
        if mac != lane_wait.HERE and host is None: continue  # a record from before waitingOn names no Mac to read
        what = f"the {role} gate's {gate.get('lane') or 'lane'} has waited {ago(since, at)} for {gate.get('waiting')}"
        try: found = busy(host)
        except Exception: found = None  # noqa: BLE001
        if found is None:
            notes.append(f"{what}; whether {mac} has spare CPU is unknown, as its busy share could not be read")
            continue
        share, limit = found[0], found[1]
        if share >= limit: continue
        found = read(host)
        holders, why = found[:2]
        # What admission reclaimed there lately holds nothing now: named apart, never as a holder (vocaro/queuekeeper#23).
        reclaimed = found[2] if len(found) > 2 else []
        named = ("unknown: " + why if holders is None else "; ".join(map(holder_words, holders))
                 if holders else "none recorded, as admission's guard stood when read")
        freed = (f". Reclaimed lately, holding nothing now: {'; '.join(map(reclaimed_words, reclaimed))}"
                 if reclaimed else "")
        findings.append({
            "signal": "held back on a Mac with spare CPU",
            "diagnosis": f"{what}, while {mac} is {share:.0f}% busy, under its {limit:g}% limit: what holds admission "
                         f"there holds it back, not load. Holders: {named}{freed} (vocaro/queuekeeper#89)",
            **({"reclaimed": reclaimed} if reclaimed else {}),
            "remedy": "ask the session that owns each holder, found by its worktree, whether its run is still needed: "
                      "stopping it is that session's to do; `python3 scripts/qk admissions now` shows the same",
            "gate": role, "mac": mac, "waitingSince": since, "cpuBusy": share, "busyLimit": limit, "holders": holders})
    return findings, notes


def holders_on(host):
    """(the holders of admission on host, another Mac, or with host None on this Mac, as `qk admissions now` reads
    them; None when they cannot be read, with why; and the holders admission reclaimed there lately, which hold
    nothing now, vocaro/queuekeeper#23)."""
    try: reading = remote.admissions_there(host) if host else offload_host.admissions_now()
    except Exception as error: return None, str(error), []  # noqa: BLE001 - unknown, never idle
    holders = reading.get("holders") if isinstance(reading, dict) else None
    reclaimed = reading.get("reclaimed") if isinstance(reading, dict) else None
    reclaimed = [item for item in reclaimed if isinstance(item, dict)] if isinstance(reclaimed, list) else []
    if not isinstance(holders, list):
        reasons = reading.get("reasons") if isinstance(reading, dict) else None
        return None, "; ".join(reasons or []) or "the reading named no holders", reclaimed
    return holders, None, reclaimed


def holder_words(holder):
    """A holder of admission as idle_waits names it: its command, PID, hold time, CPU use and origin."""
    held = holder.get("heldSeconds")
    process, cpu = holder.get("process"), holder.get("cpuPercent")
    # Admission frees such a holder at its next attempt (vocaro/queuekeeper#23).
    if process == "ended": used = "its process has ended, and admission frees its slots at its next attempt"
    elif process == "unknown": used = ("its process could not be checked, so its CPU use is unknown, and admission frees "
                                       "its slots at its next attempt")
    elif isinstance(cpu, (int, float)): used = f"CPU {cpu:g}% of one core, with what it started"
    else: used = "CPU use unknown"
    origin = resources.whence(holder.get("origin"))
    return (f"{holder.get('label') or 'an unnamed owner'} (PID {holder.get('pid')}, held "
            + (resources.duration(held) if isinstance(held, (int, float)) else "for an unknown time")
            + f", {used}, " + (f"from {origin}" if origin else "where its work comes from was not recorded") + ")")


def reclaimed_words(item):
    """A holder admission reclaimed lately, as idle_waits names it: its command, PID, slots, when and why."""
    seconds = item.get("reclaimedSeconds")
    where = "every slot" if item.get("kind") == "exclusive" else \
        ", ".join(f"slot {slot}" for slot in item.get("slots") or []) or "no slot its files named"
    condition = {"ended": "its process had ended", "unknown": "its process could not be checked"}.get(
        item.get("process"), "its process could not be confirmed running")
    return (f"{item.get('label') or 'an unnamed owner'} (PID {item.get('pid')}, {where}, reclaimed "
            + (f"{resources.duration(seconds)} ago" if isinstance(seconds, (int, float)) else "at an unknown time")
            + f": {condition}" + (f", {item['why']}" if item.get("why") else "") + ")")


def placement(root, name=None, as_json=False):
    """Inspect current capacity for a configured gate lane without dispatching or taking admission."""
    base = text(root, "rev-parse", MAIN)
    config = config_at(root, base)
    name = name or (config.get("queue", {}).get("lanes") or ["gate"])[0]
    lane = lane_to_run(config, name, root)
    if lane is None:
        raise ValueError(f"{name} is not a lane on {MAIN}; give the lane to inspect")
    lanes = config["lanes"]
    local = stays(lanes, name)
    foreground = bool(lane.get("foreground"))
    model = bool(lane.get("model"))
    whole_host, exclusive, screen = bool(lane.get("wholeHost")), lane.get("resource") == "exclusive", unlocked_screen(lanes, name)
    # The Macs the lane may go to: none for one that stays here, those that take foreground work for a foreground
    # lane, this Mac among them when its own settings say so, else all.
    hosts = () if local else remote.foreground_hosts() if foreground else farm.destinations()
    checked = []
    here = local or not foreground or offload_host.mac_settings()["takes"]["foreground"]
    candidates = hosts + ((None,) if here else ())
    for host in candidates:
        mac = lane_wait.farm_mac(host) if host else "this Mac"
        row = {"host": mac, **({"ssh": host} if host else {}),
               "checkedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        checked.append(row)
        try:
            reading = remote.capacity_there(host, diagnostic=True) if host else offload_host.capacity(diagnostic=True)
        except remote.Unavailable as error:
            row.update(status="unavailable", reason=getattr(error, "problem", str(error)))
            continue
        if reading.get("error"):
            row.update(status="unavailable", reason=reading["error"])
            continue
        slots = reading["slots"]
        # By each Mac's own settings too: the kinds of work it takes and the labels the lane needs (remote.unfit).
        reason = remote.spare(reading, mac, whole_host, exclusive, screen, model, needed_labels(lanes, name))
        row.update(guaranteedSlots=slots, maximumSlots=reading["slotsMax"], busySlots=reading["busy"],
                   blockers=reading.get("blockers", []), status="waiting" if reason else "eligible", reason=reason)
        if (foreground or model) and reason is None:
            kind = "foreground" if foreground else "model"
            row.update(status="unmeasured", reason=f"{kind} admission is separate from heavy-work capacity")
    report = {"lane": name, "commit": base, "wholeHost": whole_host, "exclusive": exclusive,
              "unlockedScreen": screen, "foreground": foreground, "model": model, "localOnly": local,
              "remoteHosts": list(hosts), "hosts": checked}
    if as_json:
        print(json.dumps(report, indent=2))
    else:
        print(f"{name} at {base[:12]} (whole host: {whole_host}, exclusive: {exclusive}, unlocked screen: {screen})")
        print(f"the farm's Macs: {', '.join(map(lane_wait.farm_mac, hosts)) or 'none'}"
              + (f"; local only: {local}" if local else ""))
        for row in checked:
            slots = ""
            if "busySlots" in row:
                busy = row["busySlots"]
                guaranteed = sum(slot < row["guaranteedSlots"] for slot in busy)
                slots = (f" ({guaranteed}/{row['guaranteedSlots']} guaranteed slots busy, "
                         f"{len(busy)}/{row['maximumSlots']} total)")
            print(f"{row['checkedAt']} {row['host']}: {row['status']}{slots}"
                  + (f" — {row['reason']}" if row["reason"] else ""))
            for blocker in row.get("blockers", []):
                print(f"  {blocker['state']} {blocker['kind']}: PID {blocker['pid']}, "
                      f"admission {blocker['identity']}, process started {blocker['processStart']}")
    return 0


def diagnose(root):
    """(findings, the waiting entries, whether a worker runs): read the queue's entries, its hold, its worker and its
    newest gate logs for the signs of a queue that is stuck rather than testing, and say for each what it means and
    what would heal it (vocaro-engineering#290): a hold; entries waiting with no worker (#287); gates that qk unittest
    refused before running anything, which give no failed test to rerun (#280), with the entry that brings the
    refusal; gates in a row with no verdict (#253, #268, #282); a waiting entry that qk unittest would refuse; and an
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
        found("held", f"the queue is held {held}", "deal with the cause the hold names, then run `qk queue resume`, "
              "unless the hold says it is for the owner")
    elif waiting and not working:
        found("no worker", f"{entries_word(len(waiting))} wait and no worker runs, so nothing gates them "
              "(vocaro-engineering#287)", "`qk queue resume` starts one")
    # Before a project's first landing, main has no configuration to be wrong (#10).
    try: suites = unittest_lanes(config := config_at(root, MAIN)) if has_config(root, MAIN) else {}
    except ValueError as error:
        suites = {}
        found("configuration", f"origin/main's queue lanes cannot be read: {error}", "fix queuekeeper.json on main")
    # Each waiting entry whose tree alone qk unittest refuses, as qk land now refuses before queuing: one queued
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
        diagnosis = (f"{len(same)} gate{'' if len(same) == 1 else 's'} since the last verdict ran nothing, as qk "
                     f"unittest refused {'it' if len(same) == 1 else 'them'} before running anything: {line} (logs: "
                     f"{', '.join(same)}). A refusal is no test failure, so no rerun can confirm one, and a bisect "
                     "through it is blind (vocaro-engineering#280)")
        if len(culprits) == 1:
            remedy = (f"{culprits[0]} brings it, as its tree alone is refused so: the worker sends it back by itself "
                      "from the next gate that holds it (vocaro-engineering#290), or take it out now with `qk queue "
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
              f"printing no qk unittest summary: {', '.join(blank)} (vocaro-engineering#253, #268, #282)",
              f"the worker runs it again up to queue.noVerdictRuns times and then holds; read {blank[0]} for the "
              "cause: a lane that reached its limit, or another Mac out of reach")
    for identifier, own in refusing.items():
        found("refused before running", f"qk unittest refuses {identifier}'s tree before running anything: "
              + "; ".join(own) + ". Every gate holding it runs nothing",
              f"take it out with `qk queue remove {identifier}`, fix it and land it again, which now checks this")
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
                      f"`qk queue remove {entry['id']}` and fix it first")
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


def dispatch_logs(directory):
    """The returned files of this exact dispatch, or what could not be read."""
    lines = []
    for name in ("run.log", "output.log"):
        path = Path(directory) / name
        try: present = path.is_file()
        except OSError: present = False
        lines.append(f"remote dispatch {name}: {path}" if present else
                     f"remote dispatch {name}: unavailable (not returned or pruned)")
    return lines


FAILURE_LINE_LIMIT = 500
FAILURE_EXCERPT_LIMIT = 4000
SWIFT_TESTING_ASSERTION = re.compile(
    lane_report.SWIFT_TESTING_ISSUE.pattern + r"(?::\d+)?: (?:Expectation|Requirement) failed\b[^\n]*", re.M)


def swift_test_assertions(log):
    """Distinct assertion lines for tests that Swift Testing reported failed."""
    if not lane_report.SWIFT_TESTING_FAILED_RUN.search(log):
        return []
    failed = set(lane_report.SWIFT_TESTING_FAILED.findall(log))
    return list(dict.fromkeys(issue[0] for issue in SWIFT_TESTING_ASSERTION.finditer(log) if issue[1] in failed))


def failed_lane_excerpt(lane_log, dispatch_output, lane, swift_assertions=()):
    """Read the outer unittest verdict or the dispatcher's lane report, with the confirmed assertions."""
    summaries = list(UNITTEST_SUMMARY.finditer(lane_log))
    summary = summaries[-1] if summaries else None
    assertions = []
    if summary and summary[1].startswith("FAILED: "):
        lines = [summary[0]]
        confirmed = set(failed_tests(lane_log)[0] or ())
        for failed in FAILED_TEST.finditer(lane_log, 0, summary.start()):
            method, where = failed.groups()
            identity = where if where.endswith(f".{method}") else f"{where}.{method}"
            if identity not in confirmed:
                continue
            end = lane_report.UNITTEST_END.search(lane_log, failed.end())
            traceback = lane_log[failed.end():end.start() if end else summary.start()]
            assertion = re.search(r"^AssertionError(?::[^\n]*)?$", traceback, re.M)
            if assertion:
                assertions.append(f"{failed[0]}: {assertion[0]}")
    else:
        # Do not repeat the report's inferred failed test names: for a group they can come from a nested fixture.
        rows = dispatch_output.splitlines()
        report = next((index for index in reversed(range(len(rows)))
                       if rows[index].startswith(f"qk run {lane}: failed")), None)
        lines = []
        if report is not None:
            lines.append(rows[report])
            if report + 1 < len(rows) and rows[report + 1].startswith("  tests: "):
                lines.append(rows[report + 1])
    assertions.extend(swift_assertions)
    lines.extend(assertions[:lane_report.SHOWN])
    if len(assertions) > lane_report.SHOWN:
        lines.append(f"{len(assertions) - lane_report.SHOWN} more assertions in the queue log")
    excerpt, remaining = [], FAILURE_EXCERPT_LIMIT
    for line in lines:
        limit = min(FAILURE_LINE_LIMIT, remaining)
        if limit < 4:
            break
        excerpt.append(line if len(line) <= limit else line[:limit - 3] + "...")
        remaining -= len(excerpt[-1]) + 1
    return excerpt


def followed(queue, looks):
    """Print what each (Follow, entry) of looks has to say now, by the round's account while a worker runs. Nothing
    here may end the wait: a line that cannot be made is left out."""
    try:
        state = rerun_waits(queue.round_state() or {}) if queue.working() else None
        for follow, entry in looks:
            line = follow.look(queue, entry, state)
            if line: print(line, flush=True)
    except Exception: pass  # noqa: BLE001


def wait(root, identifier, limit=WAIT_LIMIT):
    """Exit 0 once the entry lands, 1 when it comes back to its author, 75 while the queue is held or
    the limit passes with the entry still waiting. A worker a reboot ended starts again here."""
    queue = Queue(root)
    deadline = time.monotonic() + limit
    follow = Follow(identifier)
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
            elif entry["state"] == FAILED and (dispatch := entry.get("failedDispatch")):
                print(*dispatch["excerpt"], *dispatch_logs(dispatch["directory"]), sep="\n")
            return 0 if entry["state"] == LANDED else 1
        why = queue.held()
        if why:
            print(f"{identifier} is waiting, and the queue is held {why}")
            return resources.BUSY
        if time.monotonic() >= deadline:
            print(f"{identifier} is still {entry['state']} after {limit:g}s")
            return resources.BUSY
        followed(queue, [(follow, entry)])
        queue.start()
        time.sleep(POLL)


def wait_for(root, identifiers, limit=WAIT_LIMIT):
    """`qk queue wait ID ID ...`, as an qk land refusal names it: return once none of the entries is queued, in its
    gate or passed, so that one a `--front` entry sends back to queued keeps the wait going, saying where each ended
    and what is worth doing next: 0 each landed, so rebase; 1 one came back, so the commit it conflicted with may
    apply as it is; 75 while the queue is held or the limit passes with one still waiting (vocaro-engineering#304).
    `qk queue wait` with one ID is `wait`'s, which follows an entry of your own; qk land --wait waits here."""
    identifiers = list(dict.fromkeys(identifiers))
    queue = Queue(root)
    unknown = [identifier for identifier in identifiers if queue.read(identifier) is None]
    if unknown: raise ValueError(f"The queue has no entr{'y' if len(unknown) == 1 else 'ies'} {', '.join(unknown)}")
    deadline = time.monotonic() + limit
    follows = {identifier: Follow(identifier) for identifier in identifiers}
    while True:
        entries = [queue.read(identifier) or {"id": identifier, "state": REMOVED, "subject": "(no record)"}
                   for identifier in identifiers]
        waiting = [entry for entry in entries if entry["state"] in WAITING]
        if not waiting:
            for entry in entries: print(describe(entry))
            back = [entry["id"] for entry in entries if entry["state"] != LANDED]
            if not back:
                print("all landed: rebase onto origin/main, then `qk land` again")
                return 0
            print(f"{', '.join(back)} came back, so a commit that conflicted with {'them' if len(back) > 1 else 'it'} "
                  "may apply as it is: `qk land` again checks again")
            return 1
        said = ", ".join(f"{entry['id']} ({entry['state']})" for entry in waiting)
        why = queue.held()
        if why:
            print(f"{said} waiting, and the queue is held {why}")
            return resources.BUSY
        if time.monotonic() >= deadline:
            print(f"{said} still waiting after {limit:g}s")
            return resources.BUSY
        followed(queue, [(follows[entry["id"]], entry) for entry in waiting])
        queue.start()
        time.sleep(POLL)


def running():
    """The commit the worker's own code came from: HEAD of the toolkit checkout that scripts/qk resolved, or
    None where that is no repository."""
    head = git(TOOLKIT_ROOT, "rev-parse", "--verify", "-q", "HEAD")
    return head.stdout.strip() if head.returncode == 0 and head.stdout.strip() else None


def execute(command, env):
    """Replace this process with command in env, for a test to replace: a worker's move to a newer toolkit
    (Worker.move_to). It returns only when the exec failed."""
    os.execve(command[0], command, env)


def loads_from_working_directory(process):
    """Whether process may load toolkit code from its working directory without naming it: Python, which puts that
    directory first on its import path for `-c`, `-m` and its prompt, or a process the census could not name. Any
    other program there, such as a session's shell left in src/python or a tool a run started there (on 2026-11-11
    zsh and idevicesyslog, vocaro-engineering#384), runs nothing of the tree but what an argument names, and what
    it starts later reads one toolkit, the tree as it is then."""
    names = [process.command, os.path.basename(process.args.split(" ")[0])]
    return not any(names) or any(name.lower().startswith("python") for name in names)


def signed_launcher(app):
    """Whether app is the queue launcher, signed as the LaunchAgent requires it (Queue.launcher_plist)."""
    try:
        checked = subprocess.run(["/usr/bin/codesign", "--verify", "--strict", LAUNCHER_REQUIREMENT, str(app)],
                                 capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError): return False
    return checked.returncode == 0


def starts(toolkit, commit):
    """None when the toolkit at commit imports what a worker runs, else why not: a worker checks this in a
    process of its own, from commit's files, before it moves there (Worker.toolkit), so that a commit that does
    not start leaves the worker running on its own code rather than replacing it with nothing."""
    archive = subprocess.run(["git", "-C", str(toolkit), "archive", "--format=tar", commit, "src/python"],
                             capture_output=True, stdin=subprocess.DEVNULL, env=environment(), timeout=GIT_TIMEOUT)
    if archive.returncode:
        return f"`git archive` of it failed: {archive.stderr.decode(errors='replace').strip() or 'no output'}"
    with tempfile.TemporaryDirectory(prefix="queuekeeper-toolkit-") as folder:
        # By tar rather than tarfile, whose safe extraction Python 3.9 lacks; the archive is the toolkit's own.
        unpacked = subprocess.run(["tar", "-x", "-f", "-", "-C", folder], input=archive.stdout, capture_output=True,
                                  timeout=GIT_TIMEOUT)
        if unpacked.returncode:
            return f"its files could not be unpacked: {unpacked.stderr.decode(errors='replace').strip() or 'no output'}"
        script = ("import sys; sys.path.insert(0, sys.argv[1]); "
                  "import queuekeeper.cli, queuekeeper.merge_queue")
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


def kept_lock():
    """The worker's lock that the worker this one replaces left open for it, as its descriptor (Worker.move_to),
    taken out of the environment so that no lane inherits the name, or None."""
    kept = os.environ.pop(LOCK_VARIABLE, None)
    try: return int(kept) if kept else None
    except ValueError: return None


class Removal:
    """The cancel a gate's lanes run with (processes.run): set once cancel is, or once one of the entries ids that the
    gate tests has left the queue, as `qk queue remove` or a branch added again takes it out, which it looks for at
    most every REMOVAL_POLL seconds. gone names those entries (vocaro-engineering#247). At the same looks, first, when
    given, names an entry that goes ahead of the gate, as Worker.first_ungated does while the gate's lane waits to
    start (vocaro/queuekeeper#26); ahead then names it."""
    def __init__(self, queue, ids, cancel=None, first=None):
        self.queue, self.ids, self.cancel, self.first = queue, tuple(ids), cancel, first
        self.gone, self.looked, self.ahead = [], None, None

    def look(self):
        """Whether an entry of ids has left the queue, by its record now, or an entry goes ahead of the gate."""
        self.looked = time.monotonic()
        self.gone = [identifier for identifier in self.ids
                     if (self.queue.read(identifier) or {}).get("state") not in WAITING]
        if not self.gone and self.first is not None: self.ahead = self.first()
        return bool(self.gone or self.ahead)

    def is_set(self):
        if self.cancel is not None and self.cancel.is_set(): return True
        if self.gone or self.ahead: return True
        if not self.ids or (self.looked is not None and time.monotonic() - self.looked < REMOVAL_POLL): return False
        return self.look()


class Worker:
    def __init__(self, queue, progress=None):
        self.queue = queue
        self.checkout = queue.checkout
        self.limit = None  # how many of the oldest entries the next round takes, while bisecting
        self.suspects = 0  # how many of the oldest entries hold a commit that fails
        self.no_verdicts = 0  # the runs in a row that gave no verdict, held at no_verdict_runs
        self.locked_screens = 0  # the runs in a row a screen locked during, held at LOCKED_SCREEN_RUNS (older)
        # The commits whose gate --fail-fast cut short gated again once, as their failed tests all passed alone
        # (rerun), and whether the last rerun asked for that.
        self.regated, self.regate = set(), False
        if progress:  # what the worker this one replaces had reached (progress)
            self.limit, self.suspects = progress.get("limit"), progress.get("suspects", 0)
            self.no_verdicts = progress.get("no_verdicts", 0)
            self.locked_screens = progress.get("locked_screens", 0)
        # The lanes whose next run goes only to a Mac whose screen reads unlocked, since a locked screen left their
        # last run without a verdict (screen_locked, vocaro-engineering#293).
        self.unlocked = set(progress.get("unlocked", ())) if progress else set()
        self.sent_unlocked = False  # whether the gate's last lane went as qk run --unlocked-screen (gate)
        self.code = running()  # the commit this worker's code came from, which toolkit compares with the copy's
        self.rounds = 0  # the rounds this worker has finished, which run reads to count its tries
        self.deferred = None  # why follow last left the shared checkout behind for a run from it, or None
        self.holders = []  # the (process, how, path) that stays last found running from the shared checkout
        self.left = None  # why follow last left the shared checkout as it is, for any reason, or None
        self.timed_out = None  # why the last gate's lane reached its limit, or None (gate)
        self.failed_dispatch = None  # the last failed gate log and its dispatch evidence (gate)
        self.ran_on = None  # another Mac that ran the last lane the gate ran, or None (gate)
        self.environment = environment()  # what the lanes, the hook and the carry-over checks run in
        self.measured = None  # the passing gate run of the last round, which its landed entries record
        self.alone = False  # whether this round's batch is one entry an option of qk land put on its own (choose)
        self.ungating = False  # whether this round lands that entry without the gate (round, first_ungated)
        self.failed_lane = None  # the lane whose failure the last gate returned, or None (gate)
        self.failed_results = None  # that lane's results folder, where a failedTests lane names its failures (gate)
        self.failed_load = None  # load_words for that lane, or None (gate)
        self.tooling = None  # why Queuekeeper itself could not start for that lane, or None (tool_unstarted)
        self.probed = ""  # what starting Queuekeeper with and without the batch found (batch_stops_start)
        self.group_failed = []  # the lanes of the last group lane that did not pass (read_group)
        self.confirmed = None  # the failed tests the last rerun confirmed, which blame reads (rerun)
        self.added = None  # (entry, why) of the entry that adds the tests the last rerun passed alone, for blame (rerun)
        self.refused = None  # (lane, line, config) of the last gate qk unittest refused before running, for blame (rerun)
        self.unblamed = None  # {"key", "line"} of that refusal when blame found no one entry that brings it (blame)
        # {refusal_key: which gate} of the refusals that blamed no entry in the gates just before this one, the
        # previous gate's or a bisect's newer half beside it, which the same refusal again holds (older, Beside.weigh).
        self.refusals = {}
        self.blamed = False  # whether the last failed batch older settled sent back one entry by blame
        self.lane_now = None  # (lane, began, where) of the lane the gate runs or ran last (gate)
        self.checks = ()  # the lanes the gate runs unadmitted first, by the round's batch (quick_lanes, gate)
        self.stacked = None  # a Stack whose batch below landed, which the next round adopts as its own (after_stack)
        self.pending = None  # the Stack started on this round's batch while it gates (watched)
        self.jumped = {}  # by docs entry of a round of docs entries, the older entries it went ahead of (docs_ahead)
        self.stack_retry_after = 0  # monotonic time before which this round does not retry a declined stack
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
        if self.locked_screens: progress["locked_screens"] = self.locked_screens
        return progress

    def look(self, host, said, here="the host"):
        """(pressure, one-minute load, where it was read) of a Mac: host, another Mac, by
        remote.pressure_there, or with host None this Mac, which here names. Another Mac that cannot be read gives
        this Mac's reading, saying so when that starts, in said, and again once it can be read."""
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
        return resources.pressure(), resources.one_minute_load(), here

    def retry_macs(self, config, runner):
        """([each eligible Mac, None for this one], why them) for the settle wait before a retry. Placement can
        change while the lane waits for admission, so each look reads capacity anew."""
        lanes, lane = config.get("lanes", {}), runner.lane_now[0] if runner.lane_now else None
        if lane not in lanes: return [runner.ran_on], None
        try: hosts = farm.destinations()
        except ValueError: hosts = ()
        if not hosts: return [runner.ran_on], None
        if stays(lanes, lane): return [None], f"this Mac, since {lane} runs only there"
        # A foreground lane goes only to a Mac marked for it, where it waits for the screen, never by capacity.
        if lanes[lane].get("foreground"):
            return [runner.ran_on], f"where {lane} ran on a Mac that foreground lanes may take the screen of"
        return remote.retry_macs(hosts, lanes, lane, lane in self.unlocked)

    def settle(self, what, host=None, macs=None):
        """None once the host has spare capacity by admission's test (resources.pressure), so that the retry of
        what gave no verdict does not meet the load that voided it; else, past SETTLE_LIMIT, why it has none.
        With host, another Mac, the test reads that Mac (look). macs, instead, says at each look which Macs
        to read and why them (retry_macs), all of which must have settled. It logs what it saw, and a stop
        request ends it as one ends a lane (processes.Interrupted)."""
        start = reported = now()
        looks, seen, read = 0, None, None
        loads, said = [], {}
        while True:
            if processes.requested():
                raise processes.Interrupted(processes.requested(), f"while {what} waited for the host to settle")
            hosts, why = macs() if macs else ([host], None)
            if hosts != read:
                if why: self.say(f"{what} next on {why}")
                # One-minute loads of different Macs are not one series.
                loads, read = [], hosts
            found = [self.look(each, said, "this Mac" if why else "the host") for each in hosts]
            busy, load, where = next((each for each in found if each[0] is not None), found[-1])
            if busy is None: where = " and ".join(each[2] for each in found)
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
        it where it is. Then path is made afresh, and old stays, locked as retired, for `qk disk` to list."""
        listed = text(repository, "worktree", "list", "--porcelain").splitlines()
        if f"worktree {path}" in listed: return
        if path.exists():
            raise Hold(SESSION, f"{path} exists and is no worktree of {repository}: move it")
        path.parent.mkdir(parents=True, exist_ok=True)
        if f"worktree {old}" in listed:
            why = self.move(repository, old, path)
            if why is None: return
            self.say(f"{old} stays where it is, {why}: making {path} afresh and leaving {old} for removal")
            # Relocked as retired, so that `qk disk` can show the owner that nothing needs it.
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
        checkout that lags, or holds a session's changes, says nothing of the commits.
        Each worker has its own, so that moving it changes no other project's running
        gate. The toolkit's own queue tests its batch, and an explicit QUEUEKEEPER_ROOT stands.
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

    def pin(self):
        """Pin, for this round, the folders beside the project that its extensions load from, as main's
        queuekeeper.json names them: each at its origin/main, fetched now, in a checkout no session edits
        (pins.make), carried to every lane and the qk commands nested in them by QUEUEKEEPER_SIBLINGS. A lane
        otherwise loaded them from the shared checkouts beside the project as those stood when each command began, so
        that a landing there mid-round, or a session's edit, changed what the round's later lanes ran
        (vocaro-engineering#497). A folder an entry adds is not in main's list, and loads as it stands.

        Queuekeeper's origin/main and a folder's need not work together: a change across both lands in two steps.
        So a set this Queuekeeper has not started with before is tried first, as the command every lane begins
        with, in the checkout at origin/main (pin_starts). One that does not start is not adopted: the round runs
        with the folders at the commits the last set that started held, and the next round tries again. With no such
        set, or when that does not start either, the queue holds, sending nothing back, as for a lane Queuekeeper
        could not start for (vocaro/queuekeeper#158)."""
        queue = self.queue
        self.environment = {key: value for key, value in self.environment.items() if key != pins.VARIABLE}
        try: names = siblings_in(json.loads(config_source(queue.main_checkout, MAIN)))
        except (ValueError, AttributeError): return  # main's configuration is unreadable, which the round says itself
        if not names: return
        def fetch(clone):
            if git(clone, "remote", "get-url", "origin").returncode == 0: reach(clone, *FETCH, say=self.say, check=True)
        try: made = pins.make(queue.main_checkout, names, fetch=fetch)
        except pins.Unpinned as error:
            raise Hold(SESSION, f"a folder the project's extensions load from could not be pinned for the round: {error}")
        if not made: return
        kit = git(self.toolkit_root(), "rev-parse", "HEAD")
        found = {"queuekeeper": kit.stdout.strip() if kit.returncode == 0 else None,
                 "siblings": {name: pin["commit"] for name, pin in made.items()}}
        said = ", ".join(f"{name} at {pin['commit'][:12]}" for name, pin in made.items())
        last = self.pins_started()
        if found != last:
            self.drop_stacked("the folders the project's extensions load from are pinned afresh")
            log = queue.logs / f"{time.strftime('%Y%m%d-%H%M%S')}-pins.log"
            if self.pin_starts(made, log) is False:
                held = (last or {}).get("siblings") or {}
                earlier = {name: (pins.clone(queue.main_checkout, name), held[name], None) for name in made if name in held}
                why = (f"Queuekeeper does not start in the project at origin/main with {said}, each at its origin/main "
                       f"({log.with_name(f'{log.stem}-start.log')})")
                if not earlier or all(made[name]["commit"] == commit for name, (_, commit, _) in earlier.items()):
                    raise Hold(SESSION, f"{why}, and no earlier set started: bring Queuekeeper and those folders to "
                                        "commits that work together, which no entry of this queue can do")
                try: made = pins.make(queue.main_checkout, names, earlier)
                except pins.Unpinned as error: raise Hold(SESSION, f"{why}, and the earlier set could not be had: {error}")
                again = queue.logs / f"{time.strftime('%Y%m%d-%H%M%S')}-pins-earlier.log"
                said = ", ".join(f"{name} at {pin['commit'][:12]}" for name, pin in made.items())
                if self.pin_starts(made, again) is False:
                    raise Hold(SESSION, f"{why}, nor with {said}, which the last set that started held "
                                        f"({again.with_name(f'{again.stem}-start.log')}): bring Queuekeeper and those "
                                        "folders to commits that work together, which no entry of this queue can do")
                self.say(f"{why}: this round runs with {said}, which the last set that started held, and the next "
                         "round tries origin/main again")
            else:
                (queue.directory / PINS_STARTED).write_text(json.dumps(found) + "\n")
        self.say(f"folders beside the project: {said}")
        self.environment = {**self.environment, pins.VARIABLE: pins.encoded(made)}

    def pins_started(self):
        """The last pin set this queue's worker started Queuekeeper with: {"queuekeeper", "siblings"}, or None."""
        try: found = json.loads((self.queue.directory / PINS_STARTED).read_text())
        except (OSError, ValueError): return None
        return found if isinstance(found, dict) and isinstance(found.get("siblings"), dict) else None

    def pin_starts(self, made, log):
        """Whether Queuekeeper starts in the project at origin/main with the pin set made, as starts says of the
        checkout as it is: True, False when it exits TOOL_NOT_STARTED, or None when it says neither. The checkout is
        reset to origin/main first, as the round's integration resets it next: what an earlier batch left there may
        be what does not start."""
        self.reset(text(self.queue.main_checkout, "rev-parse", MAIN))
        command = toolkit_command(self.checkout, ["--version"], self.toolkit_root())
        try:
            status_ = processes.run(command, cwd=self.checkout, log_path=log.with_name(f"{log.stem}-start.log"),
                                    timeout=START_LIMIT, env={**self.environment, pins.VARIABLE: pins.encoded(made)})
        except (OSError, processes.TimedOut): return None
        return True if status_ == 0 else False if status_ == lane_results.TOOL_NOT_STARTED else None

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
        the round's entries are touched, so every entry's state is on disk, and the new process inherits the worker's
        lock, which is never free between the two. One that returns is a failed exec, which holds."""
        self.say(f"moving to the toolkit at {head[:12]}, origin/main, from {self.code[:12]}, which this worker was "
                 "started from: the next worker is started from the copy")
        self.drop_stacked("the worker moves to a newer toolkit")
        sys.stdout.flush()
        (self.queue.directory / MOVED).write_text(json.dumps({"from": self.code, "to": head, "at": time.time()}) + "\n")
        # The lock goes with the exec, as a copy of its descriptor that the new process inherits (kept_lock). Closed
        # with the exec and taken again by the new process, it was free for the seconds Python takes to start, and a
        # session's `qk land --wait` that looked then started a worker from the shared checkout, which took it: the
        # moved worker found it taken and exited without a word, and the other read the move as one that had
        # stopped (EPUBLib's and Pagepour's, 2026-11-15 21:54, vocaro/queuekeeper#158).
        lock = getattr(self.queue, "lock", None)
        kept = os.dup(lock.fileno()) if lock is not None else None
        if kept is not None: os.set_inheritable(kept, True)
        variables = {**environment(), PROGRESS_VARIABLE: json.dumps(self.progress()),
                     **({LOCK_VARIABLE: str(kept)} if kept is not None else {})}
        try: execute(self.queue.command(copy), variables)
        except OSError:
            if kept is not None: os.close(kept)
            self.unmoved()
            raise
        if kept is not None: os.close(kept)
        self.unmoved()
        raise RuntimeError(f"the worker could not start itself again from {copy}")

    def toolkits_own(self):
        """Whether this is the toolkit's own queue: the toolkit running the worker is a checkout of its repository."""
        common = git(TOOLKIT_ROOT, "rev-parse", "--path-format=absolute", "--git-common-dir")
        return common.returncode == 0 and common.stdout.strip() == \
            text(self.queue.main_checkout, "rev-parse", "--path-format=absolute", "--git-common-dir")

    def follows(self):
        """Whether this queue fast-forwards its main checkout after a landing: the toolkit's own, which every
        session's queuekeeper reads, and a project whose main says so (queue.followLandings), such as one whose
        checkout holds an extension other projects load."""
        if self.toolkits_own(): return True
        try: return config_at(self.queue.main_checkout, MAIN).get("queue", {}).get("followLandings") is True
        except ValueError: return False

    def follow(self, quiet=False):
        """After a landing in a queue that follows (follows), the toolkit's own among them, fast-forward the shared checkout, the toolkit's main
        checkout, which every session's queuekeeper and the next worker read, to origin/main. Only when it is on main,
        clean, holds no commit of its own (one landed through the queue, whose change origin/main holds under another
        SHA, is not), is behind, and no process started from it still runs there, since
        such a run would mix two toolkits (owner's decision, vocaro-engineering#104). Otherwise it is left as it
        is, and the log says why; nothing of this holds the queue. One left only for such a run is deferred, which
        catch_up tries again; quiet, for those tries, says only a fast-forward."""
        shared = self.queue.main_checkout
        self.deferred = self.left = None
        self.holders = []
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
        self.left = why
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
        # Sessions' worktrees are passed over by their folder too, so that a shell left naming one that has since
        # been removed holds nothing back (vocaro/queuekeeper#159).
        listed = text(shared, "worktree", "list", "--porcelain").splitlines()
        apart = [Path(line[len("worktree "):]) for line in listed[1:] if line.startswith("worktree ")]
        apart += [Path(shared) / ".claude" / "worktrees", Path(text(shared, "rev-parse", "--path-format=absolute", "--git-common-dir"))]
        try: users = disk.runners(disk.census(), shared, apart, placed=loads_from_working_directory)
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            return f"{UNKNOWN_USE}: {error}"
        launchers = self.launchers(users)
        # A process that restarts itself once the checkout moves, as the dashboard's server, holds nothing back
        # (restartable, vocaro/queuekeeper#159).
        moving = restartable.passed(shared)
        users = [use for use in users if not (use[0].pid in launchers and use[1] == "argument") and use[0].pid not in moving]
        self.holders = users
        if users:
            named = "; ".join(f"PID {process.pid} ({process.command or process.args.split(' ')[0] or '?'}), "
                              f"{how} {path}" for process, how, path in users[:SHOWN_USERS])
            more = f"; and {len(users) - SHOWN_USERS} more" if len(users) > SHOWN_USERS else ""
            return f"{IN_USE}: {named}{more}"
        return None

    @staticmethod
    def launchers(users):
        """The PIDs of the signed queue launchers among users. The LaunchAgent runs the launcher with a worker's
        command as its arguments, which name the shared checkout's src/python, and the launcher, a native program
        (src/python/queuekeeper/_native/queue-launcher), only spawns that command once and waits on it: by its arguments alone it would hold
        back the checkout, its own worker's included, as it did on 2026-11-10 and 2026-11-11 (vocaro-engineering#374,
        #384). The worker it started counts by its own arguments while it runs from the checkout, until move_to
        replaces it with a command from a toolkit copy. Only the launcher's executable in an app that verifies as
        signed is passed over; any other process with such arguments still counts."""
        found = {}
        for process, _, _ in users:
            match = LAUNCHER_ARGUMENTS.match(process.args)
            if match: found.setdefault(match.group(1), []).append(process.pid)
        return {pid for app, pids in found.items() if signed_launcher(app) for pid in pids}

    @staticmethod
    def unlanded(shared, head):
        """The commits of head that origin/main lacks and whose change it does not hold either, by
        `git cherry`'s patch identity: what a commit made in the checkout and landed through the queue,
        which gives it another SHA, does not leave."""
        listed = text(shared, "cherry", MAIN, head).splitlines()
        return [line[2:] for line in listed if line.startswith("+ ")]

    def catch_up(self):
        """After the worker's rounds, while nothing waits, try follow again every FOLLOW_POLL seconds for as long as
        only a process running from the shared checkout, or a census that failed, keeps it behind, so that it moves
        as soon as the last such run ends (owner's decision, 2026-11-11, vocaro-engineering#384); such a run ends by
        itself (#182). A checkout left for any other reason is a person's to settle. An entry that arrives, or a
        hold, ends the tries at once: the next round's landing follows again and comes back here. The tries target
        origin/main as the last round fetched it, so the worker's own code cannot change under them. It looks once
        first, since a worker that moved to a newer toolkit after a landing (move_to) has not followed itself. The
        log says the deferral when it begins, when what holds the checkout changes and when it ends, not each try,
        and the round's account says what the worker waits for, which `qk queue status` shows."""
        if self.queue.held() or not self.follows(): return
        shared = self.queue.main_checkout
        self.follow(quiet=True)
        deadline = None if FOLLOW_LIMIT is None else time.monotonic() + FOLLOW_LIMIT
        told, since = None, time.time()
        try:
            while self.deferred and (deadline is None or time.monotonic() < deadline):
                if self.deferred != told:
                    self.say(f"left {shared} behind for now, trying again every {FOLLOW_POLL}s until it moves: "
                             f"{self.deferred}")
                    told = self.deferred
                    holders = described_holders(self.holders)
                    self.queue.note(lambda state: state.update(
                        waiting="the shared checkout to follow origin/main once nothing runs from it", waitingSince=since,
                        holders=holders))
                pause(FOLLOW_POLL)
                if self.queue.held() or self.queue.waiting():
                    if told: self.say(f"stops trying to move {shared}: "
                                      f"{'the queue is held' if self.queue.held() else 'an entry waits'}")
                    return
                self.follow(quiet=True)
            if self.deferred:
                self.say(f"left {shared} behind after {FOLLOW_LIMIT}s of tries: {self.deferred}")
            elif told and self.left:
                self.say(f"left {shared} as it is: {self.left}")
        finally:
            if told: self.queue.note(lambda state: state.update(waiting=None, waitingSince=None, holders=None))

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
        ones `qk land --wait` waits for (vocaro-engineering#302)."""
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
        names as it does those of batch ahead of it. qk land's trial of a new entry runs this same code in a scratch
        checkout, settling nothing (Trial)."""
        self.reset(base)
        self.gating = ()
        applied = []
        for entry in batch:
            how = landing_proofs.contained(lambda *words: git(self.checkout, *words), base, entry["tip"])
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

    def gate_ended(self, lane, round_name, status_, log, seconds=None):
        """Tell the journal how a lane of a gate ended: its exit status, or WITHDRAWN for one stopped as its entries
        left the queue, and the Mac it ran on, where a record says."""
        # Keep every exact gate log on each entry, including successful, custom and documentation lanes.
        # The live round stops naming it when the lane ends; a terminal session or cursor may resume later.
        with contextlib.suppress(OSError), self.queue.locked():
            for identifier in self.gating:
                entry = self.queue.read(identifier)
                if entry is None: continue
                reference = {"lane": lane, "path": str(log), "mac": self.ran_where.removeprefix(" on ") or None,
                             "status": status_}
                logs = [item for item in entry.get("logs", []) if item.get("path") != str(log)]
                entry["logs"] = [*logs, reference]
                self.queue.save(entry)
        self.queue.emit("gate.ended", lane=lane, round=round_name, role=self.role, status=status_, log=str(log),
                        mac=self.ran_where.removeprefix(" on ") or None,
                        seconds=None if seconds is None else round(seconds))

    def gate(self, lanes, round_name, where=(), cancel=None, quick=None):
        """(status of the first of lanes that did not pass or 0, its log). ran_on is another Mac that ran
        that lane, by its dispatch's record (remote.dispatched), or None when it ran here or nowhere. A pass of
        the gate lane leaves its wall time, its own work and its wait for admission apart (budgets.timed), and the
        parts its log reports in self.measured, for the gate's budget (budgets.py). where is qk run's placement options
        for each lane, those that keep the second gate of a pair off the first gate's Mac (apart), and none
        otherwise, so that qk run chooses among every Mac. A set cancel ends the lane as its
        limit would (processes.Canceled), for a stacked gate thrown away (Stack). So does an entry it tests that leaves
        the queue, as `qk queue remove` takes it out, and the gate then returns WITHDRAWN, with self.withdrawn naming
        those entries (Removal, vocaro-engineering#247). A lane that still waits to start is stopped in the same way for
        an entry qk land --front --no-gate put first (first_ungated), and the gate returns WITHDRAWN with self.ahead
        naming it (vocaro/queuekeeper#26). lane_now is (the lane running, when it began, where), by which a gate
        stacked on this one goes to another Mac (Worker.admitted_mac). quick names the lanes run unadmitted first
        (quick_lanes, Worker.quick): self.checks, which the round sets for its batch, unless given."""
        log, self.ran_on, self.measured, self.timed_out, self.ran_where = None, None, None, None, ""
        self.failed_lane, self.failed_load, self.withdrawn, self.ahead = None, None, [], None
        self.failed_results = None
        self.failed_dispatch = None
        self.tooling = None
        self.altered_files = None
        removal = Removal(self.queue, self.gating, cancel, first=self.first_ungated)
        if quick is None: quick = self.checks
        for identifier in self.gating: self.queue.record({"id": identifier}, stopsWhenRemoved=REMOVAL_POLL)
        for lane in lanes:
            if removal.look() and removal.gone:
                self.withdrawn = removal.gone
                self.say(f"{lane} not started, as {', '.join(removal.gone)} left the queue")
                return WITHDRAWN, log
            # Only this lane's own wait, once it runs, lets an entry go ahead of the gate (first_ungated).
            removal.ahead = None
            log = self.queue.logs / f"{round_name}-{lane}.log"
            # A lane run here logs to its own file, and one on another Mac to its dispatch's run.log. What an
            # earlier round left in the lane's own file is not this one's.
            lane_log = self.checkout / ".build/queuekeeper" / lane / "run.log"
            with contextlib.suppress(OSError): lane_log.unlink()
            lane_wait.remove(lane_wait.path_of(self.checkout, lane))
            shutil.rmtree(lane_log.parent / "lanes", ignore_errors=True)
            tested = self.head()
            self.say(f"{lane} on {tested[:12]}")
            self.queue.emit("gate.began", lane=lane, round=round_name, role=self.role, head=tested,
                            entries=list(self.gating))
            if lane in quick:
                # In the round's account while it runs, as any lane is, with no wait to read (GateWatch).
                watch = GateWatch(self, lane, time.time(), log)
                try: status_ = self.quick(lane, log, removal)
                finally: watch.finish()
                if status_ is not None:
                    self.gate_ended(lane, round_name, status_, log)
                    if not status_: status_ = self.unaltered(lane, tested, log)
                    if not status_: continue
                    self.failed_lane = lane
                    # The check is Queuekeeper's own command, so its exit is Queuekeeper's and leaves no receipt.
                    if status_ == lane_results.TOOL_NOT_STARTED: self.tooling = self.tool_said(log, False)
                    return status_, log
            began = time.time()  # wall clock, to match the dispatch records' start times
            started = time.monotonic()  # for the gate's measured duration
            placed = tuple(where)
            self.lane_now = (lane, began, placed)
            admission = Admission(self.checkout, lane, began).start() if lane == budgets.GATE else None
            sampler = BusySampler(self.checkout, lane, began, admission).start() if lane == budgets.GATE else None
            loads = LoadSampler(self.checkout, lane, began).start()
            # A lane a locked screen left without a verdict goes only to a Mac whose screen reads unlocked (#293).
            screen = ["--unlocked-screen"] if lane in self.unlocked else []
            self.sent_unlocked = bool(screen)
            if screen: self.say(f"{lane}: only on a Mac whose screen reads unlocked, since a locked screen left its "
                                "last run without a verdict")
            # The project's launcher, or the toolkit's own qk where it has none (vocaro/queuekeeper#108).
            command = toolkit_command(self.checkout, ["run", *placed, *screen, lane], self.toolkit_root())
            if lane == DOCS and not self.has_lane(lane):
                self.say(f"{lane}: the project has no {lane} lane, so qk run runs the toolkit's: "
                         f"{shlex.join(self.implicit(lane))}")
            watch = GateWatch(self, lane, began, log).start()
            try:
                try:
                    status_ = processes.run(command, cwd=self.checkout, log_path=log, timeout=LANE_LIMIT,
                                            env={**self.environment, remote.QUEUE_GATE: "1"}, cancel=removal)
                finally: watch.finish()
            except processes.TimedOut as error:
                loads.stopped.set()
                if sampler: sampler.stopped.set()
                if admission: admission.stopped.set()
                self.unlocked.discard(lane)
                self.ran_on = remote.dispatched(self.checkout, lane, began)
                self.ran_where = self.mac(lane, began)
                if isinstance(error, processes.Canceled) and removal.gone:
                    self.withdrawn = removal.gone
                    self.say(f"{lane}: stopped{self.ran_where}, as {', '.join(removal.gone)} left the queue")
                    self.gate_ended(lane, round_name, WITHDRAWN, log, time.monotonic() - started)
                    return WITHDRAWN, log
                if isinstance(error, processes.Canceled) and removal.ahead:
                    self.ahead = removal.ahead
                    self.say(f"{lane}: stopped before it started, as {self.stopped_for()}")
                    self.gate_ended(lane, round_name, WITHDRAWN, log, time.monotonic() - started)
                    return WITHDRAWN, log
                self.timed_out = f"{lane} reached its limit: {error}"
                self.say(f"{lane}: unmeasured: {error}")
                self.gate_ended(lane, round_name, lane_results.UNMEASURED, log, time.monotonic() - started)
                return lane_results.UNMEASURED, log
            except BaseException:
                loads.stopped.set()
                if sampler: sampler.stopped.set()
                if admission: admission.stopped.set()
                raise
            loads.stopped.set()
            ended = time.monotonic()
            # It ran, unless no Mac admitted it or a host's configuration stopped it: it is placed as before next.
            if status_ not in UNRUN: self.unlocked.discard(lane)
            ran, self.ran_on = remote.dispatch(self.checkout, lane, began)
            # What the lane printed: here in its own file; on another Mac in its dispatch's run.log, whose qk
            # unittest summary names the failed tests that rerun reruns alone.
            printed = lane_log if ran is None else ran / "run.log"
            remote_failure = ran is not None and status_ and status_ not in NO_VERDICT
            dispatch_output, lane_text = "", ""
            if remote_failure:
                with contextlib.suppress(OSError):
                    dispatch_output = log.read_text(errors="replace")
            with contextlib.suppress(OSError), log.open("a") as kept:
                lane_text = printed.read_text(errors="replace")
                kept.write(lane_text)
            member_log, group_assertions = self.read_group(lane, printed.parent / "lanes", log,
                                                           include_swift=remote_failure)
            self.ran_where = self.mac(lane, began)
            busy = sampler.finish() if sampler else {}
            admitted = admission.finish() if admission else None
            self.say(f"{lane} exited {status_}{self.placed(lane, began)}")
            self.gate_ended(lane, round_name, status_, log, ended - started)
            if not status_: status_ = self.unaltered(lane, tested, log, ran)
            if status_ and self.altered_files is not None:
                self.failed_lane = lane
                return status_, log
            if status_:
                self.failed_lane = lane
                # Its results folder came back beside what it printed, here or from another Mac.
                self.failed_results = printed.parent / "results"
                self.tooling = self.tool_unstarted(lane, began, status_, ran, log)
                if status_ not in NO_VERDICT:
                    self.failed_load = loads.finish(self.ran_on)
                    if remote_failure:
                        swift_assertions = swift_test_assertions(lane_text) if member_log is None else group_assertions
                        excerpt = failed_lane_excerpt(lane_text if member_log is None else member_log,
                                                      dispatch_output, lane, swift_assertions)
                        self.failed_dispatch = (log, {"directory": str(ran), "excerpt": excerpt})
                        with contextlib.suppress(OSError), log.open("a") as kept:
                            kept.write("\n" + "\n".join([*group_assertions, *dispatch_logs(ran)]) + "\n")
                return status_, log
            if lane == budgets.GATE:
                with contextlib.suppress(OSError):
                    # A whole-host lane's busy share is its own work, which the budget does not take for a loaded host.
                    if busy and self.whole_host(lane): busy["wholeHost"] = True
                    self.measured = {**budgets.timed(ended - started, admitted, began),
                                     "parts": budgets.parts(log.read_text(errors="replace"))[:budgets.KEPT_PARTS], **busy}
                if self.measured: self.say(f"{lane} took {budgets.time_words(self.measured)}; {budgets.busy_words(busy)}")
                said = self.results_named(lane, ran, began)
                if said:
                    with contextlib.suppress(OSError), log.open("a") as kept: kept.write(said + "\n")
                    self.say(said)
                    if self.measured: self.measured.update(results=said, log=str(log))
        return 0, log

    def unaltered(self, lane, tested, log, ran=None):
        """0 when lane, which passed on tested, left the tracked tree as it found it, here and, when ran names its
        dispatch, on the Mac that ran it; else ALTERED, with self.altered_files saying what changed, which the batch's
        rejection names (rejection_facts). The queue pushes tested, so a lane that rewrote it ran on a tree that would
        not land."""
        where, files = "", altered(self.checkout, tested)
        if not files and ran is not None:
            files = altered_remotely(ran)
            if isinstance(files, str):
                self.say(f"{lane}: what it changed on {self.ran_on or 'another Mac'} is unknown, as {files}")
                files = []
            elif files: where = f" on {self.ran_on or 'another Mac'}"
        if not files: return 0
        self.altered_files = altered_words(lane, files, where)
        self.say(self.altered_files)
        with contextlib.suppress(OSError), log.open("a") as kept: kept.write(f"\nqueuekeeper: {self.altered_files}\n")
        return ALTERED

    def quick(self, lane, log, cancel=None):
        """The status of lane, one of a batch's quick lanes (quick_lanes), run in this checkout without
        admission, so that seconds of checks never wait behind heavy or whole-host work (vocaro-engineering#394 for a
        docs-only batch, vocaro/queuekeeper#169 for one that changes code): a
        preflight lane as its command, which must be one command (NOT_PREFLIGHT), or `qk docs check`, the project's own
        docs lane only when it runs exactly that. None for any other lane, or one that did not end within
        DOCS_CHECK_LIMIT, which qk run then runs admitted as before. With no lane of that name, the toolkit's check
        runs as qk run would run it (implicit). A set cancel stops it as it stops any lane (gate): None then too, and
        the admitted run that follows stops at once and says why."""
        command = self.quick_command(lane)
        if command is None: return None
        try:
            status_ = processes.run(command, cwd=self.checkout, log_path=log, timeout=DOCS_CHECK_LIMIT, cancel=cancel,
                                    env={**self.environment, PROJECT_CHECKOUT_VARIABLE: project_checkout(self.checkout)})
        except processes.Canceled: return None
        except processes.TimedOut as error:
            self.say(f"{lane}: did not end unadmitted within {DOCS_CHECK_LIMIT}s ({error}): running it admitted")
            return None
        except OSError as error:
            self.say(f"{lane}: could not run unadmitted ({error}): running it admitted")
            return None
        short = None
        if status_:
            # A check that failed on a full disk says nothing of the batch: qk run, admitted, judges it as the host's
            # (vocaro/queuekeeper#162).
            from . import disk
            with contextlib.suppress(ValueError): short = disk.short_of_space(disk.RUN_FREE, (disk.DATA_VOLUME, str(self.checkout)))
        if short:
            self.say(f"{lane}: exited {status_} unadmitted with the disk short of space ({short}): running it admitted")
            return None
        self.say(f"{lane} exited {status_} in the queue's checkout, unadmitted, as a check that takes seconds")
        return status_

    def quick_command(self, lane):
        """The command by which quick runs lane in the checkout as it is, or None when quick does not run it."""
        try: checkout = load(self.checkout)
        except (OSError, ValueError): return None
        # Main's preflight lanes, as gate's quick says, that the batch's own configuration lists too.
        if lane != DOCS and lane not in preflighted(checkout): return None
        configured = checkout.get("lanes", {}).get(lane)
        if configured is None:
            if lane != DOCS: return None
            command = self.implicit(lane)
        elif lane == DOCS and configured.get("command") != IMPLICIT_DOCS: return None
        elif not isinstance(configured, dict) or any(configured.get(key) for key in (*NOT_PREFLIGHT, *extensions.LANE_KEYS)):
            return None
        else: command = configured.get("command")
        return command if isinstance(command, list) and command else None

    def main_fails(self, base, log, config):
        """A sentence saying whether origin/main, base, fails the failed lane too without the entry the gate sent back,
        when that lane is a check that takes seconds, one of main's config's quick_lanes that quick runs, so that a session whose entry cannot pass alone,
        because main fails the same lane, looks for the fix main needs instead of adding the entry again: on 2026-11-15
        two of StudyWright's entries each fixed the lane the other failed, and five failed over four hours before a
        session saw that main failed both (vocaro/queuekeeper#180). It runs the lane's command at base in this
        checkout, unadmitted as quick does, and leaves the checkout at the head it found. "" when the lane is no such
        check, or the gate ran on base itself."""
        lane, head = self.failed_lane, self.head()
        if not lane or not base or head == base or lane not in quick_lanes(config, code=True) \
                or self.quick_command(lane) is None: return ""
        output = log.with_name(f"{log.stem}-origin-main.log")
        try:
            self.reset(base)
            command = self.quick_command(lane)
            if command is None: return ""
            status_ = processes.run(command, cwd=self.checkout, log_path=output, timeout=DOCS_CHECK_LIMIT,
                                    env={**self.environment, PROJECT_CHECKOUT_VARIABLE: project_checkout(self.checkout)})
        except (OSError, processes.TimedOut, subprocess.SubprocessError) as error:
            return f" Whether origin/main at {base[:12]} fails {lane} too could not be determined: {error}."
        finally: self.reset(head)
        self.say(f"{lane} exited {status_} on origin/main at {base[:12]}, without the entry sent back")
        if not status_: return f" origin/main at {base[:12]} passes {lane} without this entry."
        # A finding is a line that starts PATH:LINE:, as qk docs check's and a preflight lane's do.
        try:
            printed = set(log.read_text(errors="replace").splitlines())
            found = [line for line in output.read_text(errors="replace").splitlines() if FINDING.match(line)]
        except OSError: found = []
        same = sum(line in printed for line in found)
        count = f"{len(found)} finding{'' if len(found) == 1 else 's'}"
        words = ("" if not found else f", with the same {count}" if same == len(found)
                 else f", with {count}, {same} of them this entry's too")
        return (f" origin/main at {base[:12]} fails {lane} too, without this entry{words}: main may need a fix first, "
                f"perhaps another entry's, before this entry can land alone; see {output}.")

    def read_group(self, lane, folder, log, *, include_swift=False):
        """For a lane that is a group, whose own output gives only the end of a failed lane's log: add its qk unittest
        lane's whole log to log, which rerun and `qk queue doctor` read as any qk unittest gate's, and note in
        self.group_failed the lanes in folder, its lanes' logs and receipts, that did not pass (#289).
        Return the unittest member's log (empty if unavailable, None for a single lane) and the distinct Swift
        Testing assertions from failed members when requested."""
        self.group_failed = []
        try: lanes = load(self.checkout).get("lanes", {})
        except (OSError, ValueError): return None, []
        members = lanes.get(lane, {}).get("parallel") if isinstance(lanes.get(lane), dict) else None
        if not members: return None, []
        member = unittest_member(lanes, lane)
        member_log = ""
        if member:
            with contextlib.suppress(OSError), log.open("a") as kept:
                member_log = (folder / f"{member}.log").read_text(errors="replace")
                kept.write(member_log)
        assertions = []
        for name in members:
            try: status_ = json.loads((folder / f"{name}.json").read_text()).get("status")
            except (OSError, ValueError, AttributeError): status_ = None
            if status_ != 0: self.group_failed.append(name)
            if include_swift and name != member and isinstance(status_, int) and status_ and status_ not in NO_VERDICT:
                with contextlib.suppress(OSError):
                    assertions.extend(swift_test_assertions((folder / f"{name}.log").read_text(errors="replace")))
        return member_log, list(dict.fromkeys(assertions))

    def has_lane(self, lane):
        """Whether the checkout's own queuekeeper.json, which qk run reads, configures lane: where it does not, qk run
        runs the toolkit's docs check as that lane (lane_to_run)."""
        try: return lane in load(self.checkout).get("lanes", {})
        except (OSError, ValueError): return False

    def whole_host(self, lane):
        """Whether the checkout's own queuekeeper.json makes lane a whole-host lane, beside which admission admits no
        other heavy work. One that cannot be read says nothing, so no."""
        try: return load(self.checkout).get("lanes", {}).get(lane, {}).get("wholeHost") is True
        except (OSError, ValueError, AttributeError): return False

    def toolkit_root(self):
        """The toolkit this worker runs the project's lanes with: the copy that QUEUEKEEPER_ROOT names in its
        environment (toolkit), else its own."""
        return self.environment.get(TOOLKIT_VARIABLE) or str(TOOLKIT_ROOT)

    def implicit(self, lane):
        """The command of the toolkit's docs check in this checkout, as the qk run this worker starts runs
        it where the project has no such lane (implicit_command): by the project's launcher, else by the toolkit."""
        return implicit_command(self.checkout, lane, self.toolkit_root())

    def tool_unstarted(self, lane, began, status, ran, log):
        """Why Queuekeeper itself could not start for lane, which began at began and exited status, or None. It
        exited TOOL_NOT_STARTED, as cli.main does when composing its checkout or loading a project's extension raises
        before any command is read, and the run left no receipt of the lane, which qk run writes for every lane whose
        command it reaches: here, none in the lane's folder since began; on another Mac, in ran, its dispatch's
        folder, the work stage's exit with no receipt come back (offload_host returns one only when the run wrote it).
        A lane whose own command exits 70 leaves its receipt, and stays a failure. Such a run tested no commit: on
        2026-11-15 an adapter that three projects load from a shared sibling checkout refused the older Queuekeeper
        their workers ran, and each lane's traceback sent an entry back (vocaro/queuekeeper#158)."""
        if status != lane_results.TOOL_NOT_STARTED: return None
        if ran is not None:
            try: there = json.loads((ran / "source.json").read_text()).get("result")
            except (OSError, ValueError, AttributeError): return None
            if not isinstance(there, dict) or there.get("stage") != "work" or there.get("exitStatus") != status \
                    or "receipt" in there:
                return None
        else:
            with contextlib.suppress(OSError):
                if (self.checkout / ".build/queuekeeper" / lane / "source.json").stat().st_mtime >= began: return None
        return self.tool_said(log, ran is not None)

    def tool_said(self, log, there):
        """A hold's words for a lane that Queuekeeper could not start for, there on another Mac, which the hold
        names (mac), or on this Mac, where no receipt says so: the last line of log in which it says why
        (lane_results.TOOL_SAID), cut as an unmeasured test's line is."""
        try: said = [line for line in log.read_text(errors="replace").splitlines() if lane_results.TOOL_SAID in line]
        except OSError: said = []
        why = shortened(said[-1].partition(lane_results.TOOL_SAID)[2].strip()) if said else ""
        return (f"Queuekeeper itself did not start {'there' if there else 'on this Mac'}, before "
                f"the lane's command, with exit {lane_results.TOOL_NOT_STARTED}: " + (why or "its log says why"))

    def starts(self, log):
        """Whether Queuekeeper starts in this checkout as it is now, on this Mac: True when the command every lane
        begins with, the project's launcher or the toolkit's qk, loads the project's extensions and exits 0, False
        when it exits TOOL_NOT_STARTED, and None when it says neither, as one that did not end within START_LIMIT.
        It runs no lane and waits for no admission: `--version` is read once the extensions have loaded (cli.main).
        What it printed is kept beside log."""
        command = toolkit_command(self.checkout, ["--version"], self.toolkit_root())
        try:
            status_ = processes.run(command, cwd=self.checkout, log_path=log.with_name(f"{log.stem}-start.log"),
                                    timeout=START_LIMIT, env=self.environment)
        except (OSError, processes.TimedOut): return None
        return True if status_ == 0 else False if status_ == lane_results.TOOL_NOT_STARTED else None

    def batch_stops_start(self, base, head, log):
        """Whether the batch's own commits are why Queuekeeper could not start for the last gate's lane (tooling):
        on this Mac, with this Queuekeeper and the folders beside the project as they are, it starts in the project at
        base, without the batch, and not at head, with it. Then the lane's exit is a failure of the batch, as an entry
        that names an extension that does not load is, and the batch is bisected and an entry sent back as for any
        failed lane, where a hold would stop every project's entry behind it until a session found the one to remove
        (the owner's decision of 2026-11-15, vocaro/queuekeeper#158). Any other reading leaves the hold: it starts at
        neither, as on 2026-11-15, when a folder beside the project had moved; or the lane ran on another Mac and
        Queuekeeper starts here at head, so that the fault is in what that Mac holds. The checkout is at head when
        this is asked, and is left there. probed says what was found, for the hold's reason (start_fix)."""
        self.probed = ""
        if self.ran_on and self.starts(log) is not False:
            self.probed = "Queuekeeper starts on this Mac with the batch's commits, so the fault is in what that Mac holds"
            return False
        try:
            self.reset(base)
            without = self.starts(log)
        finally: self.reset(head)
        if without:
            self.probed = ""
            return True
        self.probed = ("Queuekeeper does not start on this Mac without the batch's commits either, so no entry is why"
                       if without is False else "")
        return False

    def start_fix(self):
        """What a session does about the lane the last gate could not start (unstarted), in a hold's reason."""
        if self.tooling:
            return ((f"{self.probed}: " if self.probed else "")
                    + "bring Queuekeeper and the folders the project's extensions load from to commits that work "
                    "together on that Mac, as `qk queue resume` does for the worker's own Queuekeeper by starting one "
                    "from the current checkout, or remove the entry whose commits changed what the project loads")
        return "make the lane's command start, or remove the entry whose commits took away what it runs"

    def unstarted(self, result):
        """Why the lane at which the last gate stopped with result could not start, or None: qk run exited
        NOT_STARTED and its receipt since the lane began says not-started, with why under notStarted
        (lane_report.not_started). The dispatch's receipt holds the lane's own from another Mac, as for
        receipt_error. A lane's own command that exits 127 leaves no such receipt, and stays a failure. Or Queuekeeper
        itself could not start for the lane (tool_unstarted, vocaro/queuekeeper#158)."""
        if self.tooling and result == lane_results.TOOL_NOT_STARTED: return self.tooling
        if result != lane_results.NOT_STARTED or not self.lane_now: return None
        lane, began = self.lane_now[0], self.lane_now[1]
        ran = remote.dispatch(self.checkout, lane, began)[0]
        paths = ([ran / "source.json"] if ran is not None else []) \
            + [self.checkout / ".build/queuekeeper" / lane / "source.json"]
        for path in paths:
            try:
                if path.stat().st_mtime < began: continue
                why = lane_report.not_started(json.loads(path.read_text()))
            except (OSError, ValueError):
                continue
            if why: return why
        return None

    def placed(self, lane, began):
        """Where the lane ran, for the log, which qk run chose by each Mac's capacity (vocaro-engineering#168):
        on another Mac, by its dispatch's record, or on this Mac, with why, by the receipt the run wrote here
        since began (cli.placement); nothing when neither says."""
        if self.ran_on: return f" on {self.ran_on}"
        receipt = self.checkout / ".build/queuekeeper" / lane / "source.json"
        try:
            if receipt.stat().st_mtime < began: return ""
            said = json.loads(receipt.read_text())
        except (OSError, ValueError):
            return ""
        if not isinstance(said, dict) or said.get("ranOn") != "this Mac": return ""
        return f" on this Mac ({said.get('hereBecause')})"

    def mac(self, lane, began):
        """The Mac the lane ran on, for a hold's reason (vocaro-engineering#185): ` on another Mac (HOST)` by its
        dispatch's record, ` on this Mac (NAME)` by the receipt the run wrote here since began, or nothing when
        neither says."""
        if self.ran_on: return f" on {lane_wait.farm_mac(self.ran_on)}"
        receipt = self.checkout / ".build/queuekeeper" / lane / "source.json"
        try:
            if receipt.stat().st_mtime < began: return ""
            said = json.loads(receipt.read_text())
        except (OSError, ValueError):
            return ""
        if not isinstance(said, dict) or said.get("ranOn") != "this Mac": return ""
        return f" on this Mac ({said.get('mac') or 'unnamed'})"

    def results_named(self, lane, ran, began):
        """The line that says where a passing lane's per-test results are, for its log and `qk land --wait`, naming
        them and copying none (vocaro-engineering#266); ran is the lane's dispatch, or None when it ran here. Results
        another Mac wrote came back with its dispatch, so they are on this Mac. Another Mac sends back all that
        the lane's command writes in its results/, so a dispatch with none is a command that wrote none there, such as
        a `qk unittest` lane's, and is said to be that, not a failure to send them (#38); a lane run here that wrote
        none says nothing, as before."""
        if ran is not None:
            folder = ran / remote.RESULTS
            kept = result_files(folder)
            if not kept:
                return (f"{lane} per-test results: none: its command wrote none in its results/ on "
                        f"{lane_wait.farm_mac(self.ran_on)}, which sends back all it writes there")
            return (f"{lane} per-test results: {kept} in {folder}, on this Mac, sent back by {lane_wait.farm_mac(self.ran_on)}; "
                    f"queuekeeper keeps the newest {remote.DISPATCHES_KEPT} dispatches of a lane")
        folder = self.checkout / ".build/queuekeeper" / lane / "results"
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

    def carry(self, config, batch, base, head, ran=None):
        """(new base, new head) with the batch on origin/main, when main moved past base only by files the
        lanes do not read and the batch does not touch, else None: unread files (carried_over), or, for a batch whose
        own paths selected the gate lanes ran, files whose queue.affected lanes are none of those (carried_by_rules).
        The patch must be the one that passed, and the documents and the project's
        carry-over checks must pass on the rebased tree, and its preflight lanes too after a carry by queue.affected,
        since every batch runs them."""
        new_base = text(self.checkout, "rev-parse", MAIN)
        batch_files = set(self.changed(base, head))
        moved = [path for path in self.changed(base, new_base) if path]
        ruled = [path for path in moved if not carried_over(path, config, batch_files)
                 and carried_by_rules(path, config, batch_files, ran)]
        refused = [path for path in moved if not carried_over(path, config, batch_files) and path not in ruled]
        if refused:
            self.say(f"main moved by {refused[0]}, which the result does not cover: gating again")
            return None
        if ruled:
            self.say(f"main moved by {', '.join(ruled[:5])}, which select by queue.affected none of the lanes the "
                     f"batch ran ({', '.join(ran)})")
        patch = text(self.checkout, "diff", base, head)
        self.reset(new_base)
        if any(self.pick(entry) for entry in batch):
            self.say("the batch no longer applies to main: gating again")
            return None
        if text(self.checkout, "diff", new_base, "HEAD") != patch:
            self.say("the batch's patch changed on main: gating again")
            return None
        checks = carry_checks(config, moved, base, self.implicit(DOCS))
        if ruled and preflighted(config): checks.append(toolkit_command(self.checkout, ["preflight"], self.toolkit_root()))
        if not self.carry_passes(checks): return None
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
        Any other landing records gatedOn, the base its gate ran on, by which `qk queue status` knows that a gate on
        main has passed over an earlier ungated landing (covered).
        Before each push its entries record it (publishing): a push cut off before its answer came, by a timeout, a
        dropped connection or the worker's death, may have landed, and is publication-unverified until a fetch of
        origin/main says. The fetch at the top of each try asks first, so that a push that landed before main moved
        on settles as landed rather than carrying over or gating commits main already holds, and the next round's
        start asks for what a hold or a dead worker left (reconcile)."""
        attempts = []
        try:
            pushed = self.pushing(config, batch, base, docs, ungated, flakes, attempts)
        except Hold as hold:
            # Only a remote out of reach leaves a push's answer unknown: any other hold had one, or pushed nothing.
            if hold.kind == CLEARS: self.unanswered(batch)
            else: self.queue.drop_field(batch, "publishing")
            raise
        if not pushed: self.queue.drop_field(batch, "publishing")
        return pushed

    def pushing(self, config, batch, base, docs, ungated, flakes, attempts):
        """push's tries, each attempt it pushes added to attempts."""
        head = self.head()
        gated_on = base
        changed = [path for path in text(self.checkout, "diff", "--name-only", "--no-renames", base,
                                         head).splitlines() if path]
        named = unread_named(config, changed) if docs else {}
        # The lanes the batch's own paths select, by which main moving under it may carry over (carried_by_rules).
        ran = None if docs or ungated or not config.get("queue", {}).get("affected") else lanes_of(config, changed)
        unsure = False
        checked = None
        covered = config.get("queue", {}).get("hookCoveredBy")
        # A lane that covers the hook covers it only when it ran: queue.affected may have left it out.
        skipped = [lane for lane in covered if lane not in lanes_of(config, changed)] \
            if covered and not docs and not ungated else []
        if skipped:
            self.say(f"the pre-push hook runs: main's queue.hookCoveredBy names {', '.join(skipped)}, which the "
                     "batch's paths did not select by queue.affected")
            covered = None
        for _ in range(CARRIES):
            reach(self.checkout, *FETCH, say=self.say, check=True)
            landed = self.landed_attempt(attempts)
            if landed:
                self.say(f"origin/main holds {landed['head'][:12]}, pushed before its answer came: it landed")
                self.settle_pushed(batch, landed["base"], landed["head"], gated_on, docs, named, ungated, self.measured)
                return True
            if git(self.checkout, "merge-base", "--is-ancestor", MAIN, head).returncode:
                carried = self.carry(config, batch, base, head, ran)
                if carried is None: return False
                base, head = carried
                if ungated: head = self.mark_ungated(ungated, base)
                if flakes: head = self.mark_flaky(flakes, base)
            if docs:
                self.say("no pre-push hook: documentation landing passed its docs lanes; code hooks are skipped")
            elif covered:
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
                if result: raise Hold(SESSION, f"the pre-push hook refused {head[:12]}, exit {result}; see {log}"
                                               + hooks_elsewhere(self.checkout))
                checked = head
            attempts.append({"head": head, "base": base, "at": time.time()})
            self.queue.mark_publishing(batch, {
                "batch": [entry["id"] for entry in batch], "attempts": attempts, "gatedOn": gated_on,
                **({"docsOnly": docs} if docs else {}), **({"docsNamed": named} if named else {}),
                **({"ungated": ungated} if ungated else {}), **({"gate": self.measured} if self.measured else {})})
            # A connection that dropped pushes again here, without running the hook again.
            try:
                pushed = reach(self.checkout, "push", "--no-verify", "origin", "HEAD:refs/heads/main", say=self.say)
                pushed, status = self.push_again(pushed)
            except subprocess.TimeoutExpired as error:
                raise Hold(CLEARS, f"{PUBLICATION_UNVERIFIED}: the push of {head[:12]} gave no answer in "
                                   f"{error.timeout:g}s, so whether origin/main took it is unknown until a fetch "
                                   "says") from error
            if pushed.returncode == 0:
                git(self.checkout, *FETCH)
                self.say(f"pushed {head[:12]}" + (", landed without the gate, which the next gate on main covers"
                                                  if ungated else ""))
                self.settle_pushed(batch, base, head, gated_on, docs, named, ungated, self.measured)
                return True
            said = pushed.stderr.strip()
            if "rejected" in said and ("fetch first" in said or "non-fast-forward" in said): continue
            if unreached(said):
                raise Hold(CLEARS, f"the push of {head[:12]} did not reach the remote in {len(BACKOFF) + 1} tries: "
                                   f"{said}; {PUBLICATION_UNVERIFIED} until a fetch says whether it landed")
            raise Hold(OWNER, f"the push of {head[:12]} was refused: {said or 'no output'}{status}")
        return False

    def settle_pushed(self, batch, base, head, gated_on, docs, named, ungated, gate, reconciled=False):
        """Settle batch as landed by head, pushed on base: each entry its own commit (landed), the push's event,
        the app kept and the shared checkout followed, as after any landing. reconciled says the push's answer
        never came, and a later fetch found head on origin/main (reconcile)."""
        own = self.landed(batch, base, head)
        self.queue.emit("batch.pushed", head=head, base=base, entries=[entry["id"] for entry in batch],
                        ungated=bool(ungated) or None, reconciled=reconciled or None)
        for entry in batch:
            commit, count = own.get(entry["id"], (head, 0))
            self.queue.settle(entry, LANDED, landedAs=commit, landedTip=head,
                              **({"landedCommits": count} if count else {}),
                              **({"docsOnly": docs} if docs else {}),
                              **({"docsNamed": named} if named else {}),
                              **({"ungated": ungated} if ungated else {}),
                              **({"gate": gate} if gate and not docs else {}),
                              **({} if docs or ungated else {"gatedOn": gated_on}))
        if not docs and not ungated: self.keep_app(head)
        if self.follows(): self.follow()

    def landed_attempt(self, attempts):
        """The newest of attempts, a push's {head, base, at}, whose head origin/main holds as last fetched: a push
        that landed, whatever came after it there. None when it holds none of them."""
        return next((attempt for attempt in reversed(attempts) if not git(
            self.queue.main_checkout, "merge-base", "--is-ancestor", attempt["head"], MAIN).returncode), None)

    def unanswered(self, batch):
        """Mark the push batch's entries record as one whose answer did not come (publication-unverified)."""
        for entry in batch:
            current = self.queue.read(entry["id"])
            if current and current["state"] in WAITING and current.get("publishing"):
                self.queue.record(entry, publishing={**current["publishing"], "unanswered": time.time()})

    def reconcile(self):
        """Settle what a push whose answer never came left publishing, as a timeout, a dropped connection or a worker
        that died mid-push leaves it, by a fetch of origin/main: entries whose push it holds land as that push would
        have landed them, without a push or a gate of commits main already has; the others forget it, and are pushed
        or gated as before. Whether it landed anything, a remote out of reach holding as for any fetch."""
        marked = [entry for entry in self.queue.waiting() if entry.get("publishing")]
        if not marked: return False
        reach(self.queue.main_checkout, *FETCH, say=self.say, check=True)
        main, landed = text(self.queue.main_checkout, "rev-parse", MAIN), False
        while marked:
            marker = marked[0]["publishing"]
            order = marker.get("batch", [])
            group = sorted((entry for entry in marked if entry["publishing"] == marker),
                           key=lambda entry: order.index(entry["id"]) if entry["id"] in order else len(order))
            marked = [entry for entry in marked if entry["publishing"] != marker]
            ids = ", ".join(entry["id"] for entry in group)
            attempts = marker.get("attempts", [])
            attempt = self.landed_attempt(attempts)
            if attempt is None:
                self.say(f"origin/main at {main[:12]} holds no push of {', '.join(a['head'][:12] for a in attempts)}, "
                         f"whose answer never came: {ids} did not land, and are pushed or gated as before")
                self.queue.drop_field(group, "publishing")
                continue
            self.say(f"origin/main at {main[:12]} holds {attempt['head'][:12]}, pushed before its answer came: "
                     f"{ids} landed")
            self.settle_pushed(group, attempt["base"], attempt["head"], marker.get("gatedOn"), marker.get("docsOnly"),
                      marker.get("docsNamed"), marker.get("ungated"), marker.get("gate"), reconciled=True)
            landed = True
        return landed

    def keep_app(self, head):
        """Start keeping the app built at head, the gated commit just pushed, in the background, when head's
        queuekeeper.json keeps one after landing (extensions.AFTER_LANDING): the round goes on while it builds, and a build
        that fails leaves the build kept before it. A docs-only or ungated landing keeps none. Head's settings, not
        main's at the batch's base as the gate's: keeping a build gates nothing, and so the commit that first gives
        `app` keeps its build too (Pagepour's, 2026-11-06, kept none)."""
        try:
            config = config_at(self.checkout, head)
        except ValueError as error:
            self.say(f"keeping no app build of {head[:12]}: {error}")
            return
        log = self.queue.logs / f"app-keep-{head[:12]}.log"
        for keep in extensions.AFTER_LANDING:
            try:
                started = keep(self.checkout, config, head, log, self.environment)
            except OSError as error:
                self.say(f"could not start keeping the app built at {head[:12]}: {error}")
                continue
            if started: self.say(f"keeping the app built at {head[:12]} in the background; see {log}")

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
                                    "origin", url], cwd=self.checkout, log_path=log, timeout=LANE_LIMIT,
                                   env={**self.environment, pre_push.QUEUE_VARIABLE: "1"})
        except processes.TimedOut as error:
            self.say(f"pre-push hook: unmeasured: {error}")
            return lane_results.UNMEASURED, log
        self.say(f"pre-push hook exited {result}")
        return result, log

    def rerun(self, config, applied, base, head, log, round_name):
        """The flakes a batch whose gate failed lands with, or None to bisect it as before (owner's decision of
        2026-11-07, vocaro-engineering#231). When the failed lane is qk unittest and its log names the failed tests,
        the worker waits for this Mac, where they run, to settle as for a retry, and reruns each alone on head up to
        RERUNS times. A rerun that fails makes the failure real. When every rerun passes, each test is a flake,
        recorded in the entries and on the tracker (hardening), and the batch lands only when none of its commits
        changes a file the test's module is or imports (imported_files); otherwise it bisects. In a batch of several
        entries, a rerun that fails goes on to rerun the other failed tests, so that blame can weigh them all. A test
        that passes alone but that origin/main at base lacks, as the batch adds it (added_test), has no history to be
        a flake by: it is recorded nowhere, and when every test that passed alone is such a test, the one entry that
        adds them is sent back by blame (adder, vocaro/queuekeeper#129). Beside an old test that passed alone, which
        is recorded as a flake, no entry explains them all, and the batch bisects (vocaro/queuekeeper#88).

        Meanwhile the round's account has a failure record (failure_note), which `qk queue status` and `qk land
        --wait` read: the failed lane, its entries and log, then the failed tests, and each rerun's test, count, log
        and wait record (vocaro/queuekeeper#41). It goes once the verdict is in."""
        entries = [entry["id"] for entry in applied]
        self.queue.note(lambda state: state.update(failure={"lane": self.failed_lane, "entries": entries,
                                                            "log": str(log), "since": time.time()}))
        try: return self.weigh_failure(config, applied, base, head, log, round_name)
        finally: self.queue.note(lambda state: state.pop("failure", None))

    def failure_note(self, **fields):
        """Set fields of the round's failure record (rerun), while it has one."""
        def change(state):
            if isinstance(state.get("failure"), dict): state["failure"].update(fields)
        self.queue.note(change)

    def weigh_failure(self, config, applied, base, head, log, round_name):
        """rerun's verdict."""
        self.confirmed = self.refused = self.added = None
        self.regate = False
        lane = self.failed_lane
        if self.altered_files:
            self.say(f"{lane} changed the tree it tested, which the same tree does again: no rerun")
            return None
        lanes = config.get("lanes", {})
        member = unittest_member(lanes, lane) if lane else None
        command = rerun_command(lanes[member].get("command")) if member else None
        if command is None:
            declared = declared_failures(lanes, lane, self.group_failed) if lane else None
            return self.rerun_declared(declared, applied, base, head, log, round_name) if declared else None
        others = [name for name in self.group_failed if name != member]
        if others:
            self.say(f"{lane}'s lane{'' if len(others) == 1 else 's'} {', '.join(others)} did not pass, which no rerun of "
                     "a Python test can clear: bisecting")
            return None
        try: said = log.read_text(errors="replace")
        except OSError as error:
            self.say(f"{lane}'s log cannot be read, so no failed test is rerun: {error}")
            return None
        tests, why = failed_tests(said)
        refusals = REFUSAL.findall(said)
        if tests is None and refusals and not UNITTEST_SUMMARY.findall(said):
            # A refusal is no test failure, and the same tree gives it again: blame reads it, with no rerun (#290).
            self.refused = (lane, refusals[-1], config)
            self.say(f"{lane} ran nothing, as qk unittest refused it before running anything, which no rerun would "
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
        self.failure_note(tests=list(tests), reruns=RERUNS)
        busy = self.settle("the failed tests run alone")
        if busy:
            self.say(f"the failed tests are not rerun, as {busy}: bisecting")
            return None
        ran = self.rerun_each(tests, lambda test: [*command, test], applied, head, log, round_name)
        if ran is None: return None
        flakes, real = ran
        top = self.suite_top(command)
        if real:
            if flakes:
                self.say(f"{', '.join(flake['test'] for flake in flakes)} passed alone beside the real failure, so "
                         "no commit is blamed")
            elif None in real.values():
                self.say("a rerun ended with no verdict, so no commit is blamed")
            else:
                self.confirmed = {"tests": real, "top": top, "command": command, "base": base, "head": head,
                                  "round": round_name}
            return None
        added = {}
        for flake in flakes:
            found = added_test(self.checkout, top, flake["test"], base)
            if found: added[flake["test"]] = found
        if added:
            one = len(added) == 1
            self.say(f"{', '.join(added)} passed alone, but origin/main has no such test, as the batch adds "
                     f"{'it' if one else 'them'}, so {'it is' if one else 'they are'} no flake and no issue is filed")
            flakes = [flake for flake in flakes if flake["test"] not in added]
            if not flakes:
                self.added = self.adder(applied, added)
                return None
        changed = {path for path in text(self.checkout, "log", "--format=", "--name-only", "--no-renames",
                                         f"{base}..{head}").splitlines() if path}
        touching = {}
        # A gate --fail-fast stopped at a failure that repeated alone there ran only part of the suite, so failures
        # that all pass alone here cannot land the batch: it gates again, once for its commit, and then bisects.
        short = cut_short(said)
        again = short and head not in self.regated
        # Old tests that passed alone beside a test the batch adds are flakes, recorded as any; but no one entry
        # explains every failed test, so none is blamed and the batch bisects (owner's decision of 2026-11-07;
        # whether to relax it is vocaro/queuekeeper#88).
        for test in added: touching[test] = "which the batch adds, so origin/main has no such test"
        for flake in flakes:
            files = imported_files(self.checkout, top, flake["test"])
            reached = sorted(changed & set(files)) if files is not None else None
            if reached is None: touching[flake["test"]] = "whose module the worker cannot find"
            elif reached: touching[flake["test"]] = f"which changes {', '.join(reached[:5])}, which it imports"
        self.keep_flakes(flakes, applied, touching,
                         "The batch was bisected, as it also adds a failed test that passed alone, which is no flake."
                         if added else
                         "The batch was bisected, as one of its commits changes the module of a failed test, or a file "
                         "it imports." if touching else
                         "The gate had stopped at this failure before the rest of the suite ran, so the batch gated "
                         "again." if again else
                         "The gate had stopped at this failure before the rest of the suite ran, again, so the batch "
                         "was bisected." if short else
                         f"No commit of the batch's {len(applied)} changes its module or a file it imports, so the "
                         "batch landed.")
        if touching:
            self.say(f"every failed test passed alone, but the batch reaches {'; '.join(f'{test}, {why}' for test, why in touching.items())}: bisecting")
            return None
        if short:
            self.regated.add(head)
            self.regate = again
            self.say(f"every failed test passed alone {RERUNS} times, but {lane} stopped at the first that failed again "
                     "alone there, before the rest of the suite ran: "
                     + ("gating the batch again" if again else "it did so before on this commit, so bisecting"))
            return None
        self.say(f"every failed test passed alone {RERUNS} times and no commit of the batch changes its module or a file "
                 "it imports: landing the batch")
        return flakes

    def rerun_each(self, tests, command_of, applied, head, log, round_name):
        """(the flakes of tests, {test: the log of the rerun that failed, or None}) once each is rerun alone on head,
        by command_of(test), up to RERUNS times, stopping at its first failure; or None once a lone entry's test failed
        again, which leaves nothing to weigh. Each rerun's admission writes what it waits for to a wait record of its
        own, which the round's failure record names (rerun_waits)."""
        if self.head() != head: self.reset(head)
        flakes, real = [], {}
        record = lane_wait.path_of(self.checkout, "rerun")
        for number, test in enumerate(tests, 1):
            logs = []
            for attempt in range(1, RERUNS + 1):
                path = self.queue.logs / f"{round_name}-rerun-{number}-{attempt}.log"
                logs.append(str(path))
                lane_wait.remove(record)
                self.failure_note(tests=list(tests), reruns=RERUNS, test=test, number=number, attempt=attempt,
                                  rerunLog=str(path), began=time.time(), waitRecord=str(record))
                try: status_ = processes.run(command_of(test), cwd=self.checkout, log_path=path, timeout=LANE_LIMIT,
                                             env={**self.environment, lane_wait.VARIABLE: str(record)})
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
        return flakes, real

    def keep_flakes(self, flakes, applied, touching, outcome):
        """Record flakes, with outcome, what became of the batch, on the tracker (hardening), in the queue's events and
        in each entry of applied; touching, the tests the batch reaches, is whether the batch did not land."""
        for flake in flakes:
            flake["outcome"] = outcome
            flake["issue"], flake["trackerError"] = hardening(self.checkout, flake)
            if flake["issue"] is None: del flake["issue"]
            else: del flake["trackerError"]
            self.say(f"flaky: {flake_words(flake)}; "
                     + (f"tracker: {flake['issue']}" if "issue" in flake else f"the tracker has no record: {flake['trackerError']}"))
        for flake in flakes:
            self.queue.emit("test.flaky", test=flake.get("test"), commit=flake.get("commit"), issue=flake.get("issue"),
                            landed=not touching, entries=[entry["id"] for entry in applied])
        for entry in applied:
            current = self.queue.read(entry["id"]) or entry
            self.queue.record(entry, flakes=current.get("flakes", []) + flakes)

    def rerun_declared(self, declared, applied, base, head, log, round_name):
        """rerun's verdict for a lane that is not qk unittest but whose failedTests name, in the JUnit XML it wrote to
        its results folder, the tests that failed, and how to rerun one alone (vocaro/queuekeeper#109). Each is rerun
        as a qk unittest lane's is, and when every rerun passes, each is a flake, recorded as any, and the batch lands
        only when none of its commits changes one of the lane's paths, the files its tests depend on; a lane that lists
        none bisects. A failure that a rerun confirms bisects, as no output names an entry to blame."""
        lane, settings = declared
        folder = self.failed_results
        # A group's lanes each write under results/LANE/.
        if folder is not None and lane != self.failed_lane: folder = folder / lane
        if folder is None:
            self.say(f"{lane}'s results folder is not known, so no failed test is rerun: bisecting")
            return None
        tests, why = junit_failures(folder / settings["results"])
        if tests is None:
            self.say(f"{lane} failed with no failed test to rerun alone ({why}): bisecting")
            return None
        if len(tests) > RERUN_MOST:
            self.say(f"{lane} failed {len(tests)} tests, more than the {RERUN_MOST} a flake would: bisecting")
            return None
        self.say(f"{lane} failed {len(tests)} test{'' if len(tests) == 1 else 's'} at {head[:12]}: {', '.join(tests)}; "
                 f"rerunning each alone up to {RERUNS} times on that commit before a bisect")
        self.failure_note(tests=list(tests), reruns=RERUNS)
        busy = self.settle("the failed tests run alone")
        if busy:
            self.say(f"the failed tests are not rerun, as {busy}: bisecting")
            return None
        template = settings["rerun"]
        ran = self.rerun_each(tests, lambda test: [word.replace("{test}", test) for word in template], applied, head,
                              log, round_name)
        if ran is None: return None
        flakes, real = ran
        if real:
            if flakes:
                self.say(f"{', '.join(flake['test'] for flake in flakes)} passed alone beside the real failure, so "
                         "no commit is blamed")
            return None
        paths = settings.get("paths") or []
        changed = [path for path in text(self.checkout, "log", "--format=", "--name-only", "--no-renames",
                                         f"{base}..{head}").splitlines() if path]
        reached = sorted({path for path in changed if any(names_path(path, key) for key in paths)})
        why = (None if paths and not reached else
               f"{lane}'s failedTests list no paths its tests depend on, so any change may reach them" if not paths else
               f"the batch changes {', '.join(reached[:5])}, which {lane}'s tests depend on")
        self.keep_flakes(flakes, applied, {flake["test"]: why for flake in flakes} if why else {},
                         f"The batch was bisected, as {why}." if why else
                         f"No commit of the batch's {len(applied)} changes a path {lane}'s tests depend on, so the batch "
                         "landed.")
        if why:
            self.say(f"every failed test passed alone, but {why}: bisecting")
            return None
        self.say(f"every failed test passed alone {RERUNS} times and no commit of the batch changes a path {lane}'s tests "
                 "depend on: landing the batch")
        return flakes

    def adder(self, applied, added):
        """(the one entry of applied that adds every test of added, {test: (its module's file, its class and method)},
        why), to send back while the rest gate again at once; or None to bisect (vocaro/queuekeeper#129). An entry adds
        a test when its commits change the test's module; among several that do, the one whose own tip defines the
        test. A lone entry is the one."""
        tests, plural = ", ".join(added), len(added) > 1
        found = set()
        for test, (relative, names) in added.items():
            candidates = applied if len(applied) == 1 else [entry for entry in applied
                                                            if relative in self.changed_by(entry)]
            if len(candidates) > 1:
                candidates = [entry for entry in candidates if defines(self.blob_at(entry["tip"], relative), names)]
            if len(candidates) != 1:
                self.say(f"{test}, which the batch adds, is added by "
                         f"{'no entry' if not candidates else f'{len(candidates)} entries'} of the batch's "
                         f"{len(applied)} that the worker can tell, so no commit is blamed: bisecting")
                return None
            found.add(candidates[0]["id"])
        if len(found) != 1:
            self.say(f"the tests the batch adds, {tests}, come from different entries, so no commit is blamed: bisecting")
            return None
        [identifier] = found
        entry = next(entry for entry in applied if entry["id"] == identifier)
        why = (f"{tests}, {'tests' if plural else 'a test'} this entry adds, failed the gate and then passed alone "
               f"{RERUNS} times on that commit. origin/main has no such test, so {'they have' if plural else 'it has'} "
               f"no history to call {'them' if plural else 'it'} flaky by, and no flaky-test issue is filed: fix "
               f"{'them' if plural else 'it'} before this entry lands")
        rest = len(applied) - 1
        self.say(f"blaming {identifier} ({entry.get('subject') or entry['tip'][:12]}), which adds {tests}; sending it "
                 "back" + (f" and gating the other {rest} at once instead of bisecting" if rest else ""))
        return entry, why

    def blob_at(self, commit, relative):
        """The text of relative, a repository path, at commit, read from the project's checkout as own_paths reads
        an entry's commits; "" when commit has no such file."""
        shown = git(self.queue.main_checkout, "show", f"{commit}:{relative}")
        return "" if shown.returncode else shown.stdout

    def blame(self, applied):
        """(the one entry of applied that every failed test the last rerun confirmed reaches, why), to send back while
        the rest gate again at once, sparing the bisect's gates; or None to bisect. A test reaches an entry whose
        commits change a path its failing rerun's output names, as a preflight lane's `PATH:LINE:` lines do, or a file
        its module is or imports (imported_files); the blame stands only once the tests pass without it
        (passes_without). Any doubt bisects: no confirmed failure, a module the worker cannot find, a test that reaches
        no entry or several, tests that reach different entries, or tests that fail without the entry too. A gate qk
        unittest refused before running anything is blamed by its refusal instead (blame_refusal)."""
        confirmed, self.confirmed = self.confirmed, None
        refused, self.refused, self.unblamed = self.refused, None, None
        added, self.added = self.added, None
        if refused: return self.blame_refusal(applied, *refused)
        if added: return added
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
        without = self.passes_without(entry, applied, confirmed)
        if without is None: return None
        why = f"{'; '.join(whys)}; {without}"
        self.say(f"blaming {identifier} ({entry.get('subject') or entry['tip'][:12]}): {why}; sending it back and "
                 f"gating the other {len(applied) - 1} at once instead of bisecting")
        return entry, why

    def passes_without(self, entry, applied, confirmed):
        """What blame says of the failed tests that confirmed holds, once each passed alone on the batch without entry,
        the one entry they reach: the other entries of applied picked in order on the gate's base, and each test run
        once as rerun ran it; or None to bisect (vocaro/queuekeeper#183). A test can read a file no import or failure
        names, as configuration it loads or sources it scans, so that another entry's change fails it; a blame stands
        only when the failure goes with entry. Any doubt bisects: a test that fails without entry too, a run with no
        verdict, or another entry that does not apply without it. The words name the other entries, which gate again
        at once, so that the session sent back knows what else was in the batch."""
        others = [other for other in applied if other["id"] != entry["id"]]
        named = ", ".join(other["id"] for other in others)
        tests = confirmed["tests"]
        self.reset(confirmed["base"])
        try:
            for other in others:
                why = self.pick(other)
                if why:
                    self.say(f"{entry['id']} is blamed, but {other['id']} does not apply without it ({why}), so the "
                             "blame cannot be confirmed: bisecting")
                    return None
            for number, test in enumerate(tests, 1):
                path = self.queue.logs / f"{confirmed['round']}-without-{number}.log"
                try: status_ = processes.run([*confirmed["command"], test], cwd=self.checkout, log_path=path,
                                             timeout=LANE_LIMIT, env=self.environment)
                except processes.TimedOut as error: status_ = f"unmeasured, {error}"
                except OSError as error: status_ = f"not started, {error}"
                if status_:
                    self.say(f"{entry['id']} is blamed, but {test} "
                             + (f"failed without it too, on the batch's other entries ({named}), "
                                if isinstance(status_, int) else f"gave no verdict without it ({status_}), ")
                             + f"so the blame is not confirmed: bisecting; see {path}")
                    return None
        finally:
            self.reset(confirmed["head"])
        one = len(tests) == 1
        return (f"{'it' if one else 'each'} passed alone without this entry, on the batch's other "
                f"{'entry' if len(others) == 1 else 'entries'}, {named}, which gate again at once")

    def own_paths(self, entry):
        """The paths entry's own commits change, as changed_by reads them, from the project's checkout, which every
        checkout of the queue shares its objects with and which is there before any round has made the queue's own.
        An entry that records no commits changes nothing, and so is no docs-only entry (docs_lanes)."""
        if not entry.get("commits"): return set()
        return {path for path in text(self.queue.main_checkout, "log", "--no-walk", "--format=", "--name-only",
                                      "--no-renames", *entry["commits"]).splitlines() if path}

    def changed_by(self, entry):
        """The paths entry's commits change, each by its name before and after a move."""
        return {path for path in text(self.checkout, "log", "--no-walk", "--format=", "--name-only", "--no-renames",
                                      *entry["commits"]).splitlines() if path}

    def blame_refusal(self, applied, lane, line, config):
        """(the one entry of applied that brings line, qk unittest's refusal of lane's gate before it ran anything,
        why), to send back while the rest gate again at once; or None, with unblamed set, to bisect (owner's decision
        of 2026-11-08, vocaro-engineering#290). A refusal is no test failure: the same tree gives it again, so no rerun
        confirms it. An entry brings it when its tree alone gives a refusal naming one of its modules (refused_alone),
        or its commits change one of them in the suite's directory (#280). Any doubt bisects: a refusal that names
        no module, no entry or several that bring it, or a tree qk unittest's checks cannot read."""
        modules = refused_modules(line)
        suite = unittest_lanes(config).get(lane)
        folder = os.path.normpath(suite[0]) if suite else None
        reached, doubt = {}, None
        for entry in applied if modules else []:
            try: own = refused_alone(self.queue.main_checkout, entry["tip"], config)
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
                doubt = f"qk unittest's checks cannot read {entry['id']}'s tree: {error}"
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
            self.say(f"qk unittest refused {lane} before running anything, and {because}, so no commit is blamed")
            return None
        [(identifier, reason)] = reached.items()
        entry = next(entry for entry in applied if entry["id"] == identifier)
        why = (f"qk unittest refused {lane} before running anything, which no rerun would change, and only "
               f"{identifier} brings the refusal, as {reason}: {line}")
        rest = len(applied) - 1
        self.say(f"blaming {identifier} ({entry.get('subject') or entry['tip'][:12]}): {why}; sending it back"
                 + (f" and gating the other {rest} at once instead of bisecting" if rest else ""))
        return entry, why

    def touched_words(self, entry, log, config):
        """Whether entry's commits change a file of the tests the failed lane's log, log, names: a test's module or a
        file it imports, as a flake's reach is judged (imported_files); and plainly that it could not be determined
        when the tests, their modules or the commits cannot be read. A log with no qk unittest summary is read by the
        summary qk run ends with instead (summary_words)."""
        said = Path(log).read_text(errors="replace")
        tests, why = failed_tests(said)
        unknown = "so whether this entry changes their files is unknown"
        lanes = config.get("lanes", {})
        settings = lanes.get(self.failed_lane)
        group = isinstance(settings, dict) and "parallel" in settings
        member = unittest_member(lanes, self.failed_lane)
        ended = lane_report.summary(said, self.failed_lane)
        # A group's log holds its qk unittest lane's own output, which says nothing of its other lanes: unless that
        # lane alone failed and named its tests, the group's summary names the failed tests of each of its lanes
        # (vocaro/queuekeeper#193).
        alone = member is not None and tests is not None and set(self.group_failed) <= {member}
        if ended is not None and (UNITTEST_SUMMARY.search(said) is None or (group and not alone)):
            words = self.summary_words(entry, ended)
            if not group or not self.group_failed: return words
            failed = self.group_failed
            return f"{self.failed_lane}'s lane{'' if len(failed) == 1 else 's'} {', '.join(failed)} did not pass. {words}"
        if tests is None: return f"Which tests failed could not be determined ({why}), {unknown}"
        more = len(tests) - REJECTION_SHOWN
        named = ", ".join(tests[:REJECTION_SHOWN]) + (f" and {more} more" if more > 0 else "")
        command = rerun_command(lanes.get(member or self.failed_lane, {}).get("command"))
        if command is None: return f"{named} failed, but {self.failed_lane} is no qk unittest suite to find them in, {unknown}"
        if not entry.get("commits"): return f"{named} failed, but this entry records no commits, {unknown}"
        changed, top = self.changed_by(entry), self.suite_top(command)
        modules, reached, lost = {}, set(), []
        for test in tests:
            # A module is parsed once, however many of its tests failed.
            place = test.rsplit(".", 1)[0]
            if place not in modules: modules[place] = imported_files(self.checkout, top, test)
            if modules[place] is None: lost.append(test)
            else: reached |= changed & set(modules[place])
        words = (f"This entry changes {', '.join(sorted(reached)[:REJECTION_SHOWN])}, a file of the failed tests {named}"
                 if reached else None)
        if lost:
            missing = f"The module of {', '.join(lost[:REJECTION_SHOWN])} cannot be found, {unknown}"
            return f"{words}. {missing}" if words else f"{named} failed. {missing}"
        return words or f"This entry changes no file of the failed tests {named}: neither their modules nor what those import"

    def summary_words(self, entry, ended):
        """touched_words for a failed lane whose log has no qk unittest summary but ends with qk run's
        (lane_report.summary, ended), as a Swift gate's does (vocaro/queuekeeper#83): each failed test as the summary
        names it, with the file and line it failed at, and whether entry's commits change that file. Only a Swift
        test's file is compared, placed in the repository by placed_file; what it tests is not. A test named with no
        such file, one not placed, and a failure the summary counts but does not name leave it unknown, as they say."""
        unknown = "so whether this entry changes their files is unknown"
        lane = self.failed_lane
        names = ended["names"]
        unnamed = max(ended["more"], (ended["failed"] or 0) - len(names))
        if not names:
            counted = f"; its tests: {ended['tests']}" if ended["tests"] else ""
            return f"Which tests failed could not be determined ({lane}'s summary names no failed test{counted}), {unknown}"
        shown = [f"{name} at {place}" if place else name for name, place in names]
        more = len(shown) - REJECTION_SHOWN + unnamed
        named = ", ".join(shown[:REJECTION_SHOWN]) + (f" and {more} more" if more > 0 else "")
        if not entry.get("commits"): return f"{named} failed, but this entry records no commits, {unknown}"
        changed = self.changed_by(entry)
        tracked = set(text(self.checkout, "ls-files", "-z").split("\0")) - {""}
        reached, lost, other = set(), [], []
        for (_name, place), words in zip(names, shown):
            path = place.rsplit(":", 1)[0] if place else None
            if not (path and path.endswith(".swift")):
                other.append(words)
                continue
            found = placed_file(path, tracked | changed)
            if found is None: lost.append(words)
            elif found in changed: reached.add(found)
        words = (f"This entry changes {', '.join(sorted(reached)[:REJECTION_SHOWN])}, a file of the failed tests {named}"
                 if reached else None)
        missing = []
        if lost: missing.append(f"The file of {', '.join(lost[:REJECTION_SHOWN])} cannot be placed in the repository")
        if other: missing.append(f"No Swift file, the only kind compared, is named for {', '.join(other[:REJECTION_SHOWN])}")
        if unnamed: missing.append(f"{lane}'s summary does not name {unnamed} more failed "
                                   + ("test" if unnamed == 1 else "tests"))
        if missing:
            missing = f"{'; '.join(missing)}, {unknown}"
            return f"{words}. {missing}" if words else f"{named} failed. {missing}"
        return words or (f"This entry changes no file of the failed tests {named}: only the files they failed in are "
                         "compared, not the code they test")

    def rejection_facts(self, entry, log, config, base=None):
        """The sentences a rejection of entry adds after its reason, so that its author can tell a change that broke
        the lane from a load-sensitive test (vocaro-engineering#273): the load during the failed lane, whether
        entry changes a file of the failed tests, and, for a check that takes seconds, whether origin/main, base,
        fails it too (main_fails). Only information: what it cannot read it says so, and nothing it raises reaches the
        rejection. A lane that passed but changed the tree it tested (unaltered) is said instead."""
        if self.altered_files: return f" {self.altered_files}."
        load = self.failed_load or "The load during the failed lane could not be determined"
        try: touched = self.touched_words(entry, log, config)
        except Exception as error:  # noqa: BLE001
            touched = f"Whether this entry changes a file of the failed tests could not be determined: {error}"
        try: main = self.main_fails(base, log, config)
        except Exception as error:  # noqa: BLE001
            main = f" Whether origin/main fails {self.failed_lane} too could not be determined: {error}."
        # A change to what the lanes load from the main checkout, which main's copy judged (vocaro/queuekeeper#186).
        try: copies = main_copies(config, self.changed_by(entry)) if entry.get("commits") else ""
        except Exception:  # noqa: BLE001
            copies = ""
        return f" {load}. {touched}" + (f".{main}" if main else "") + (f" {copies}." if copies else "")

    def settle_failure(self, entry, log, why, runner=None, **fields):
        """Keep only the dispatch of the gate that actually rejected this entry, with fields such as conflictWith."""
        dispatch = (self if runner is None else runner).failed_dispatch
        details = {"failedDispatch": dispatch[1]} if dispatch is not None and dispatch[0] == log else {}
        self.queue.settle(entry, FAILED, why=why, log=str(log), **details, **fields)

    def send_back(self, applied, log, entry, why, facts="", *, runner=None):
        """Settle entry, the one of applied that blame found, as failed, and queue the rest to gate again at once."""
        self.settle_failure(entry, log, f"the queue's lanes fail on this commit on top of origin/main: {why}. Fix "
                                       f"it and add it again.{facts}", runner)
        self.queue.mark([other for other in applied if other["id"] != entry["id"]], QUEUED)

    def suite_top(self, command):
        """The top-level directory of the suite command runs, as qk unittest takes it: -t, else -s, else the checkout."""
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

    def failed(self, applied, log, facts="", *, runner=None):
        """Bisect: the oldest half next. A lone entry that fails is the one to send back, saying facts
        (rejection_facts)."""
        if len(applied) == 1:
            why = f"the queue's lanes fail on this commit on top of origin/main: fix it and add it again.{facts}"
            self.say(f"{applied[0]['id']}: {why}")
            self.settle_failure(applied[0], log, why, runner)
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
        entry qk land --front put first, alone, one that lands without the gate too before any other
        (vocaro/queuekeeper#26); then the oldest entries, as many as a bisect allows, up to the first one qk land
        --no-gate lands alone, or that entry alone when it is the oldest. alone says whether the batch is one entry an
        option put on its own, whose outcome leaves a bisect where it was."""
        batch = waiting[:self.limit] if self.limit else waiting
        self.alone = False
        if any(entry["state"] == PASSED for entry in batch): return batch
        first = next((entry for entry in waiting if entry.get("front") and entry.get("noGate")), None) \
            or next((entry for entry in waiting if entry.get("front")), None)
        if first is None and batch and batch[0].get("noGate"): first = batch[0]
        if first is not None:
            self.alone = True
            return [first]
        return batch[:next((index for index, entry in enumerate(batch) if entry.get("noGate")), len(batch))]

    def round(self):
        queue = self.queue
        # What a push whose answer never came left, before anything is pushed or gated again.
        if not self.stacked and self.reconcile(): return True
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
        self.pin()
        if self.stacked: return self.adopted()
        if passed: return self.push_passed(passed)
        base = text(queue.main_checkout, "rev-parse", MAIN)
        self.jumped = {}
        if not self.alone: batch = self.docs_ahead(waiting, batch, base)
        applied = self.integrate(batch, base)
        if not applied:
            if not self.alone: self.limit, self.suspects = None, 0
            return True
        head = self.head()
        config = self.settings(base, head)
        queue.mark(applied, TESTING)
        self.say(f"testing {len(applied)} entr{'y' if len(applied) == 1 else 'ies'} at {head[:12]} on {base[:12]}"
                 + (" at the front of the queue, as qk land --front asked" if applied[0].get("front") else ""))
        ungated = self.ungated(applied, config)
        self.ungating = bool(ungated)
        # Without renames, so that a file moved into a docs path also names the path it left.
        changed = [path for path in text(self.checkout, "diff", "--name-only", "--no-renames", base, head).splitlines()
                   if path]
        docs = None if ungated else docs_lanes(config, changed)
        named = unread_named(config, changed) if docs else {}
        if docs: self.say(f"no code lane reads any path the batch changes, by main's queue.unread: running only "
                          f"{', '.join(docs)}, "
                          "which is no gate of the pushed commit"
                          + (f"; queue.unread.paths names {named_said(named)}" if named else ""))
        round_name = f"{time.strftime('%Y%m%d-%H%M%S')}-{head[:8]}"
        beside = self.beside(waiting, batch, base, config, docs, ungated, changed)
        # A batch that changes documents and code runs main's docs lanes too, both halves of a bisect alike (#122).
        lanes = beside.lanes if beside else None if docs or ungated else lanes_of(config, changed)
        also = [lane for lane in lanes if lane not in lanes_of(config)] if lanes else []
        if also:
            self.say(f"the batch changes documents beside code: running {', '.join(also)} as well as the gate, for "
                     "what only the docs lanes check")
        selected = affected_lanes(config, changed) if lanes and not beside else None
        if selected and selected != gate_lanes(config):
            left = [lane for lane in gate_lanes(config) if lane not in selected]
            self.say(f"by main's queue.affected, the batch's paths select {', '.join(selected)} of the gate lanes, "
                     f"not {', '.join(left)}")
        self.role = "older half" if beside else "batch"
        # The preflight lanes and the docs check run unadmitted first, whatever the batch changes (#169).
        self.checks = quick_lanes(config, code=not docs)
        if beside: beside.checks = self.checks
        self.begun(round_name, head, base, applied, beside)
        # A bisect's gates, and the gate of an entry qk land --front put on its own, stack nothing, but docs entries
        # still land beside them (docs_beside, #394).
        if beside:
            result, log = self.watched(self, config, lanes, base, head, [*applied, *beside.newer],
                                       lambda: beside.both(self, lanes, round_name), stacks=False)
        elif (self.limit or self.alone) and not (docs or ungated):
            result, log = self.watched(self, config, lanes, base, head, applied,
                                       lambda: self.gate(lanes, round_name), stacks=False)
        elif self.alone or docs or ungated:
            result, log = self.gate(ungated["lanes"] if ungated else docs or lanes, round_name)
        else:
            self.lane_now = None
            result, log = self.watched(self, config, lanes, base, head, applied,
                                       lambda: self.gate(lanes, round_name))
        self.prune()
        if beside and not result and beside.result[0] == 0 and self.together(config, base, applied, beside, lanes,
                                                                             round_name):
            return True
        if not beside:
            if self.back_in_line(applied, result): return True
            outcome = self.older(config, applied, base, head, docs, ungated, result, log, round_name)
            self.after_stack(outcome, self, config)
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

    def docs_ahead(self, waiting, batch, base):
        """This round's batch, once docs-only entries are kept out of code batches (vocaro-engineering#394): the
        docs-only entries that can go ahead (docs_first), in a round of their own on the docs lanes, when batch holds
        any other entry; else batch less the docs-only entries, which wait for a round of their own (without_docs),
        unless a bisect is going on; else batch. A round of docs entries is alone, so that its outcome leaves a bisect
        where it was, and jumped holds, by entry, the older entries each goes ahead of (back_in_line)."""
        try: config = config_at(self.queue.main_checkout, base)
        except ValueError: return batch
        docs = docs_first(waiting, config, self.own_paths)
        ids = {entry["id"] for entry in docs}
        if docs and any(entry["id"] not in ids for entry in batch):
            self.alone = True
            for entry in docs:
                older = waiting[:waiting.index(entry)]
                self.jumped[entry["id"]] = [other["id"] for other in older if other["id"] not in ids]
            rest = len(waiting) - len(docs)
            self.say(f"{len(docs)} docs-only entr{'y' if len(docs) == 1 else 'ies'} land{'s' if len(docs) == 1 else ''} "
                     f"first, on the docs lanes alone, never in a code batch; the other {rest} wait for the next round")
            return docs
        if self.limit: return batch
        code = without_docs(batch, config, self.own_paths)
        if code and len(code) < len(batch):
            self.say(f"the {len(batch) - len(code)} docs-only entr{'y' if len(batch) - len(code) == 1 else 'ies'} "
                     "of the batch wait for a round of their own on the docs lanes, behind an older entry that changes "
                     "the same paths")
            return code
        return batch

    def back_in_line(self, applied, result):
        """Whether a round of docs entries that went ahead of older entries (jumped), and failed its docs lanes, was
        queued again behind them: each such entry waits for those entries (waitsFor), since it may need one of them,
        as a link needs the file an older entry adds. One that failed with no older entry behind it fails as any does."""
        if not result or result in NO_VERDICT or result == WITHDRAWN: return False
        behind = {entry["id"]: self.jumped.get(entry["id"]) for entry in applied if self.jumped.get(entry["id"])}
        if not behind: return False
        for entry in applied:
            if entry["id"] in behind: self.queue.record(entry, waitsFor=behind[entry["id"]])
        self.queue.mark(applied, QUEUED)
        self.say(f"the docs lanes failed ahead of older entries: {', '.join(behind)} queued again behind them, on "
                 "top of which they fail or land")
        return True

    def docs_beside(self, config, applied):
        """Land the docs-only entries waiting behind applied, the batch gating now, beside its gate, on the docs lanes
        in a checkout of their own (DOCS_SUFFIX): each one that docs_first takes with applied's paths blocked, whose
        paths, unread by main's queue.unread and none of applied's, a passed batch carries over (carried_over), so
        that applied's pass then carries over what they add to main (Worker.carry). None goes while an entry put first
        by qk land, or a batch that passed, waits to go first. One whose docs lanes fail waits in line for applied and the older entries (waitsFor). None goes
        beside a project's first landing, since the docs lanes are main's and main has no configuration yet."""
        if not has_config(self.queue.main_checkout, MAIN): return
        waiting = self.queue.waiting()
        ids = {entry["id"] for entry in applied}
        if not ids <= {entry["id"] for entry in waiting}: return
        rest = [entry for entry in waiting if entry["id"] not in ids]
        if any(entry.get("front") or entry.get("noGate") or entry["state"] == PASSED for entry in rest): return
        gating = set().union(*(self.own_paths(entry) for entry in applied))
        docs = docs_first(rest, config, self.own_paths, blocked=gating, gating=ids)
        if not docs: return
        runner = Beside(self, self.queue.home / f"{self.queue.main_checkout.name}{DOCS_SUFFIX}",
                        "docs beside the gate")
        runner.prepare()
        reach(runner.checkout, *FETCH, say=runner.say, check=True)
        base = text(runner.checkout, "rev-parse", MAIN)
        landing_ = runner.integrate(docs, base)
        if not landing_: return
        head = runner.head()
        main = config_at(self.queue.main_checkout, base)
        lanes = docs_lanes(main, [path for path in text(runner.checkout, "diff", "--name-only", "--no-renames", base,
                                                        head).splitlines() if path])
        if not lanes: return
        self.queue.mark(landing_, TESTING)
        runner.say(f"{len(landing_)} docs-only entr{'y' if len(landing_) == 1 else 'ies'} at {head[:12]} on "
                   f"{base[:12]}: running only {', '.join(lanes)}, beside the gate of {len(applied)}, no code lane")
        result, log = runner.gate(lanes, f"{time.strftime('%Y%m%d-%H%M%S')}-{head[:8]}", quick=quick_lanes(main))
        if not result:
            current = [self.queue.read(entry["id"]) for entry in landing_]
            if any(entry is None or entry["state"] not in WAITING for entry in current):
                runner.say("an entry was removed during the run: queued again")
            else:
                runner.keep(landing_, base, head, lanes)
                if runner.push(main, landing_, base, lanes): return
            self.queue.mark(landing_, QUEUED)
            return
        if result in NO_VERDICT or result == WITHDRAWN:
            runner.say(f"no verdict (exit {result}): queued again")
            self.queue.mark(landing_, QUEUED)
            return
        for entry in landing_:
            older = [other["id"] for other in waiting[:waiting.index(entry)]
                     if other["id"] not in {each["id"] for each in landing_}]
            self.queue.record(entry, waitsFor=older)
        self.queue.mark(landing_, QUEUED)
        runner.say(f"the docs lanes failed beside the gate, exit {result}: queued again behind the batch gating and "
                   f"the older entries, on top of which they fail or land; see {log}")

    def docs_between(self, config, entries, old, new):
        """Whether new, a commit of main, differs from old only by documents the passed result of entries, gated on
        old, carries over (carried_over), as docs entries landed beside its gate make it differ."""
        files = set().union(*(self.own_paths(entry) for entry in entries))
        return all(carried_over(path, config, files) for path in self.changed(old, new) if path)

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
            state.pop("failure", None)
            state.setdefault("gates", {})
        self.queue.note(change)
        self.queue.emit("round.began", round=round_name, head=head, base=base, adopted=adopted or None,
                        entries=[entry["id"] for entry in applied], bisect=bisect)

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
            if runner.ahead:
                self.say(f"{runner.stopped_for()}, which stopped the gate before it started: the batch gates again on "
                         "top of it")
            else: self.say(f"{', '.join(runner.withdrawn)} left the queue, which stopped the gate: gating what is left")
            queue.mark(applied, QUEUED)
            return QUEUED
        if result in NO_VERDICT:
            queue.mark(applied, QUEUED)
            # This worker exits, as before a round, so the run counts toward no bound (vocaro-engineering#300). The
            # worker's own answer decides, so that a lane that merely exits with this status cannot end worker after
            # worker.
            if result == remote.LOST_ACCESS: self.connected()
            full = runner.out_of_space(result, log)
            if full:
                # A run again would fill the same disk, so the queue holds at once for a session to free space, and
                # the batch's commits are not blamed: on 2026-11-14 a gate that ran out of space hung for about 90
                # minutes, and on 2026-11-15 one whose log could not be written was sent back (vocaro/queuekeeper#162).
                self.no_verdicts = self.locked_screens = 0
                queue.hold(worded(SESSION, f"a lane ran out of disk space{runner.ran_where}: {full}. That is the "
                                           "host's, not the batch's, and no entry is sent back: free space with "
                                           "`python3 scripts/qk disk --delete`, or by removing what `python3 "
                                           f"scripts/qk disk` lists, then resume the queue; see {log}"))
                return NO_VERDICT_OUTCOME
            locked = runner.screen_locked(result, log)
            if locked:
                # The owner's decision of 2026-11-08 (vocaro-engineering#293): a run a locked screen voided does not
                # count toward the hold, and the lane's next run goes only to a Mac whose screen reads unlocked.
                lane, reading = locked
                self.unlocked.add(lane)
                self.say(f"no verdict (exit {result}){runner.ran_where}: {lane} ran behind a locked screen, the last "
                         f"reading {reading}, which queue.noVerdictRuns does not count; its next run goes only to a "
                         "Mac whose screen reads unlocked" + self.count_locked(runner, config, lane, log))
                if self.locked_screens >= LOCKED_SCREEN_RUNS:
                    queue.hold(worded(SESSION, f"{lane} ended behind a screen that locked during the run in "
                                               f"{self.locked_screens} runs in a row, the last reading {reading}, the "
                                               f"most LOCKED_SCREEN_RUNS allows ({LOCKED_SCREEN_RUNS}); see {log}"))
                    return NO_VERDICT_OUTCOME
                busy = self.settle("the lanes run", macs=lambda: self.retry_macs(config, runner))
                if busy: queue.hold(worded(SESSION, f"{lane} ran behind a locked screen, and {busy}; see {log}"))
                return NO_VERDICT_OUTCOME
            # A run that waited for an unlocked Mac and exited 75 ran nothing, so it says nothing of the row of locks;
            # any other cause ends the row.
            if not (result == resources.BUSY and runner.lane_now and runner.needs_unlocked(runner.lane_now[0], config)):
                self.locked_screens = 0
            self.no_verdicts += 1
            runs = no_verdict_runs(config)
            cause = runner.no_verdict_cause(result, log)
            gave = (f"the lanes gave no verdict in {self.no_verdicts} run{'' if self.no_verdicts == 1 else 's'} in a "
                    f"row, the last with exit {result}{runner.ran_where}, {cause}")
            # A shared host's configuration is the owner's; a host that stays busy, or tests left unmeasured, a
            # session reads, and resumes once the host has settled or the cause is gone.
            kind = OWNER if result == remote.MISCONFIGURED else SESSION
            if self.no_verdicts >= runs:
                queue.hold(worded(kind, f"{gave}, the most queue.noVerdictRuns allows ({runs}); see {log}"
                                        + runner.shared_timing(result, config)))
                return NO_VERDICT_OUTCOME
            self.say(f"no verdict (exit {result})" + (f" from {runner.ran_on}" if runner.ran_on else "")
                     + f", run {self.no_verdicts} of at most {runs}"
                     # What an unmeasured run could not measure, so that the worker's log says it before any hold does.
                     + (f": {cause}" if result == lane_results.UNMEASURED else ""))
            # Every retry waits, so that none starts into the load that voided the run before it, or another Mac's.
            busy = self.settle("the lanes run", macs=lambda: self.retry_macs(config, runner))
            if busy: queue.hold(worded(kind, f"{gave}, and {busy}; see {log}"))
            return NO_VERDICT_OUTCOME
        unstarted = runner.unstarted(result)
        if unstarted and runner.tooling and runner.batch_stops_start(base, head, log):
            # Queuekeeper starts without the batch and not with it: the batch's own commits stop it, which is a
            # failure of theirs and no fault of this Mac's, so they are bisected and sent back (#158).
            self.say(f"{runner.failed_lane} could not start{runner.ran_where}: {unstarted}; Queuekeeper starts on "
                     f"this Mac at {base[:12]}, without the batch, so the batch's commits stop it: a failure of theirs")
            unstarted = None
        if unstarted:
            # Its command never ran, so it says nothing of the commits, and running it again would not start it
            # either: no entry is sent back or bisected, and a session makes the lane start (vocaro/queuekeeper#108).
            queue.mark(applied, QUEUED)
            lane = runner.failed_lane
            self.say(f"{lane} could not start{runner.ran_where}: {unstarted}; no commit is blamed")
            queue.hold(worded(SESSION, f"{lane} could not start{runner.ran_where}: {unstarted}. Its command never ran, "
                                       "so this says nothing of the batch's commits and none is sent back: "
                                       f"{runner.start_fix()}; see {log}"))
            return NO_VERDICT_OUTCOME
        self.no_verdicts = self.locked_screens = 0
        if result:
            # A failure that a rerun alone does not repeat lands, when the batch cannot reach the test (#231).
            if not reran: flakes = runner.rerun(config, applied, base, head, log, round_name)
            if not flakes and runner.regate:
                queue.mark(applied, QUEUED)
                return QUEUED
            if not flakes:
                blamed = runner.blame(applied)
                if blamed:
                    self.send_back(applied, log, *blamed, runner.rejection_facts(blamed[0], log, config, base), runner=runner)
                    if not self.alone: self.limit, self.suspects = None, 0
                    self.blamed = True
                    return FAILED
                if runner.unblamed and runner.unblamed["key"] in before:
                    return self.refused_again(applied, runner.unblamed["line"], before[runner.unblamed["key"]], log)
                if runner.unblamed: self.refusals[runner.unblamed["key"]] = "the gate before it"
                self.failed(applied, log, runner.rejection_facts(applied[0], log, config, base) if len(applied) == 1 else "",
                            runner=runner)
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

    def count_locked(self, runner, config, lane, log):
        """Count toward LOCKED_SCREEN_RUNS the run of lane that log holds, which ended behind a locked screen, when
        the screen read unlocked at its start, as the owner decided on 2026-11-09 (vocaro-engineering#293): one that
        began behind the lock says nothing of a screen that locks during runs. Returns what the worker's log says."""
        if not runner.began_unlocked(lane, log, config):
            return "; it did not read unlocked at its start, so LOCKED_SCREEN_RUNS does not count it either"
        self.locked_screens += 1
        return (f"; {self.locked_screens} such run{'' if self.locked_screens == 1 else 's'} in a row, and "
                f"{LOCKED_SCREEN_RUNS} hold the queue")

    def refused_again(self, applied, line, other, log):
        """Hold the queue for a session, applied queued again, when qk unittest refused this gate before running
        anything as it refused other, the gate before it or a bisect's half beside it, and no one entry brings the
        refusal: a bisect would only send back, one by one, entries the refusal does not name, while the cause, on
        origin/main or in how entries combine, stays (owner's decision of 2026-11-08, vocaro-engineering#290). The
        hold's reason, which `qk queue status` and the worker log show, carries `qk queue doctor`'s diagnosis."""
        self.queue.mark(applied, QUEUED)
        if not self.alone: self.limit, self.suspects = None, 0
        try:
            findings = diagnose(self.queue.main_checkout)[0]
            diagnosis = "; ".join(f"{finding['signal']}: {finding['diagnosis']}; next: {finding['remedy']}"
                                  for finding in findings) or "healthy"
        except Exception as error:  # the diagnosis explains the hold, and never stops it
            diagnosis = f"it could not run: {error}"
        self.queue.hold(worded(SESSION, f"qk unittest refused this gate and {other} before running anything, the "
                                        f"same way, and no one entry of the batch brings the refusal, so a bisect "
                                        f"would only send back entries it does not name: {line}; see {log}; qk queue "
                                        f"doctor: {diagnosis}"))
        return QUEUED

    def adopted(self):
        """This round's batch is the Stack gated on the tip of the batch that landed before it: it gates on, with
        the entries that arrived meanwhile stacked on its tip in turn, and its result settles it as any round's
        does. One whose base is no longer origin/main, or whose lanes main's configuration no longer names, is
        thrown away, its entries queued again."""
        stacked, self.stacked = self.stacked, None
        base = text(self.queue.main_checkout, "rev-parse", MAIN)
        try:
            config = config_at(self.queue.main_checkout, base)
            lanes = lanes_of(config, stacked.paths)
        except ValueError as error:
            stacked.drop(f"main's configuration at {base[:12]} cannot be read: {error}")
            return True
        # Docs entries that landed beside the gates since leave its result standing, which its push carries over.
        if base != stacked.base and not self.docs_between(config, stacked.entries, stacked.base, base):
            stacked.drop(f"origin/main is {base[:12]}, not the tip {stacked.base[:12]} it was gated on")
            return True
        if lanes != stacked.lanes:
            stacked.drop(f"main at {base[:12]} names the lanes {', '.join(lanes)}, not those it ran")
            return True
        # What the gate ran on, which the push carries over the documents main has gained since (Worker.carry).
        base = stacked.base
        applied = [self.queue.read(entry["id"]) or entry for entry in stacked.entries]
        self.alone = self.ungating = False
        self.say(f"testing {len(applied)} entr{'y' if len(applied) == 1 else 'ies'} at {stacked.tip[:12]} on "
                 f"{base[:12]}, whose gate began on top of the batch that just landed")
        self.begun(stacked.name, stacked.tip, base, applied, adopted=True)
        result, log = self.watched(stacked.runner, config, lanes, base, stacked.tip, applied, stacked.wait)
        self.prune()
        if stacked.not_admitted():
            # The speculative offer never started. Gate the entries normally without charging a no-verdict run.
            stacked.drop(stacked.void())
            self.after_stack(QUEUED, stacked.runner)
            return True
        outcome = self.older(config, applied, base, stacked.tip, None, None, result, log, stacked.name,
                             runner=stacked.runner)
        self.after_stack(outcome, stacked.runner, config)
        return True

    def watched(self, runner, config, lanes, base, head, applied, gate, stacks=True):
        """gate(), the lanes of applied at head in runner's checkout, while a thread looks every STACK_POLL for
        entries that arrive behind them: docs-only entries it lands beside the gate (docs_beside), and, unless stacks
        is false, as for a bisect's gates, the others it gates stacked on head in the other checkout, on another Mac
        (stack), which after_stack keeps or throws away."""
        self.pending = None
        self.stack_retry_after = 0
        stop, said = threading.Event(), set()
        def stacker():
            nonlocal stacks
            docs = True
            while (docs or stacks) and not stop.wait(STACK_POLL):
                # Docs entries first, as they land on main and a stacked gate keeps its result over them (after_stack).
                if docs:
                    try: self.docs_beside(config, applied)
                    except Exception as error:  # a landing beside the gate is a saving, never the round's verdict
                        self.say(f"no docs entry lands beside the gate of {head[:12]}: {error}")
                        docs = False
                if stacks:
                    try: self.stack(runner, config, lanes, head, applied, stop, said)
                    except Exception as error:  # a stacked gate is a saving, never the round's verdict
                        self.say(f"no gate stacked on {head[:12]}: {error}")
                        stacks = False
        thread = threading.Thread(target=stacker, name="stacker", daemon=True)
        thread.start()
        try: return gate()
        finally:
            stop.set()
            thread.join()

    def stack(self, runner, config, lanes, head, applied, stop, said):
        """Start self.pending, the waiting entries not in applied, the batch at head, gated stacked on head, when
        there are any and none is pending: on any Mac but the one running the primary's final lane (admitted_mac,
        apart), in the other checkout. One that gave no verdict or stopped when an entry left the queue is thrown
        away. An admission decline delays the next attempt. An entry qk land put first or lands without the gate
        goes first on main. A batch with a withdrawn entry does not start a stack, as its own gate stops
        (Worker.gate, #247)."""
        if self.pending is not None:
            if not self.pending.done(): return
            why = self.pending.void()
            if why is None: return
            if self.pending.not_admitted(): self.stack_retry_after = time.monotonic() + STACK_ADMISSION_BACKOFF
            self.pending.drop(why)
            self.pending = None
        if time.monotonic() < self.stack_retry_after: return
        ids = {entry["id"] for entry in applied}
        waiting = self.queue.waiting()
        if not ids <= {entry["id"] for entry in waiting}: return
        fresh = [entry for entry in waiting if entry["id"] not in ids]
        if not fresh or any(entry.get("front") or entry.get("noGate") or entry["state"] != QUEUED for entry in fresh):
            return
        # A docs-only entry lands beside the gate or in a round of its own, never on a stacked code gate (#394).
        fresh = without_docs(fresh, config, self.own_paths)
        if not fresh: return
        mac = self.admitted_mac(runner, lanes[-1])
        if mac is None: return
        # The stacked entries' own lanes: main's docs lanes too when they change documents and code (#122).
        paths = set().union(*(self.own_paths(entry) for entry in fresh))
        lanes = lanes_of(config, paths)
        hosts = farm.destinations()
        # A foreground lane goes to a Mac marked for it, outside this placement, once the farm has Macs to choose among.
        if len(hosts) > 1 and any(config.get("lanes", {}).get(lane, {}).get("foreground") for lane in lanes): return
        # Any Mac but the batch's, this Mac among them, waiting for one as any gate does (apart).
        placed = apart(mac, hosts, waits=True)
        if placed is None: return
        where, excluded, said_where = placed
        # A lane that runs only on this Mac cannot leave it, and the batch gates here.
        why = offerable(config, lanes) if mac == HERE_MAC else None
        if why:
            if why not in said: self.say(f"the entries behind the batch wait for it, since {why}")
            said.add(why)
            return
        # No Mac that could take it has room: an attempt would only wait to be declined (STACK_ROOMLESS).
        busy = self.stack_room(hosts, excluded, where)
        if busy:
            if STACK_ROOMLESS not in said: self.say(f"the entries behind the batch wait, since {busy}")
            said.add(STACK_ROOMLESS)
            self.stack_retry_after = time.monotonic() + STACK_ADMISSION_BACKOFF
            return
        said.discard(STACK_ROOMLESS)
        other = Beside(self, self.queue.checkout if runner.checkout != self.queue.checkout else None, "stacked",
                       settles=False)
        if excluded is not None: other.environment = {**other.environment, remote.QUEUE_GATE_EXCLUDE: excluded}
        other.checks = quick_lanes(config, code=True)
        stacked = Stack(self, other, fresh, head, lanes, where, below=applied, paths=paths, said_where=said_where)
        if not stop.is_set() and stacked.begin(): self.pending = stacked

    def stack_room(self, hosts, excluded, where):
        """None when a Mac a stacked gate may go to (apart: hosts but excluded, and this Mac unless where keeps it
        off) has spare capacity by admission's test, its CPU under its heavy busy limit with normal memory pressure,
        as settle reads it (resources.pressure, remote.pressure_there); else why none has. A Mac that cannot be
        read has none."""
        macs = [host for host in hosts if host != excluded] + ([] if "--offload-only" in where else [None])
        said = []
        for host in macs:
            try: busy = resources.pressure() if host is None else remote.pressure_there(host)[0]
            except remote.Unavailable as error: busy = "unknown", str(error)
            if busy is None: return None
            said.append(f"on this Mac, {busy[1]}" if host is None else f"on {remote.called(host)}, {busy[1]}")
        return f"no Mac that may take them has spare capacity now ({'; '.join(said) or NO_ROOM})"

    def admitted_mac(self, runner, final_lane):
        """The Mac that admitted the primary's final lane. Earlier lanes can use a different Mac."""
        current = runner.lane_now
        if current is None or current[0] != final_lane: return None
        lane, began, _ = current
        record = lane_wait.current(lane_wait.path_of(runner.checkout, lane), began)
        if not record or record.get("admitted") is not True or not lane_wait.alive(record.get("pid")): return None
        return record.get("mac")

    def after_stack(self, outcome, runner, config=None):
        """Keep the pending Stack for the next round when the batch below it landed at exactly the tip it was
        stacked on, or at one that differs from it only by documents the stack's result carries over (docs_between),
        which docs entries landed beside the gate added (docs_beside), and the queue goes on; else throw it away."""
        pending, self.pending = self.pending, None
        if pending is None: return
        if pending.done() and pending.void():
            pending.drop(pending.void())
        elif outcome == LANDED and runner.head() == pending.base and not self.queue.held():
            self.say(f"the gate stacked on {pending.base[:12]} goes on as the next round, as that is origin/main now")
            self.stacked = pending
        elif outcome == LANDED and config is not None and not self.queue.held() \
                and self.docs_between(config, pending.entries, pending.base, runner.head()):
            self.say(f"the gate stacked on {pending.base[:12]} goes on as the next round, as origin/main is that tip "
                     "with documents its result carries over")
            self.stacked = pending
        else:
            pending.drop("the batch below it did not land at the tip it was stacked on")

    def drop_stacked(self, why):
        """Throw away every stacked gate, its entries queued again: before the worker moves or stops."""
        for name in ("pending", "stacked"):
            stacked = getattr(self, name, None)
            setattr(self, name, None)
            if stacked is not None: stacked.drop(why)

    def beside(self, waiting, batch, base, config, docs, ungated, changed=()):
        """The Beside that gates a bisect's newer half while batch, its older half, gates on another Mac than it
        (Beside.both), or None to gate batch alone, as before: no bisect is going on, batch is an entry an option put on its own, a docs-only or ungated
        round, a newer entry an option of qk land marked, a lane that runs only here (offerable), or a newer entry
        that does not apply to origin/main without the older half, which waits for it (vocaro-engineering#241). Its
        lanes, which both halves run, are lanes_of the paths of both, changed the older half's, so that a docs path in
        either runs the docs lanes in each (docs_also, vocaro/queuekeeper#122)."""
        if self.alone or docs or ungated or not self.limit or self.suspects <= len(batch): return None
        rest = waiting[len(batch):self.suspects]
        if not rest or any(entry.get("front") or entry.get("noGate") or entry["state"] == PASSED for entry in rest):
            return None
        lanes = lanes_of(config, set(changed).union(*(self.own_paths(entry) for entry in rest)))
        why = offerable(config, lanes)
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
        other.newer, other.tip, other.lanes = newer, other.head(), lanes
        self.queue.mark(newer, TESTING)
        self.say(f"testing the newer {len(newer)} entr{'y' if len(newer) == 1 else 'ies'} of the bisect at "
                 f"{other.tip[:12]} on {base[:12]} beside them, on a Mac other than theirs if one can start them now")
        return other

    def after_beside(self, beside, applied, outcome):
        """The bisect's next step once both halves are weighed, outcome the older half's (Worker.older) and
        beside.outcome the newer's (Beside.weigh): the newer half alone, halved, when only it fails; the older half,
        halved, when the newer landed beside it and the older gave no verdict; no bisect once the half that failed
        has been sent back, alone or by blame, or both passed (when both passed outright, Worker.together settled
        the bisect instead). Otherwise, as the older half's outcome left it."""
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

    def together(self, config, base, older, beside, lanes, round_name):
        """Whether the search for the fewest entries that fail together settled the bisect whose two halves, older
        here and beside.newer, both passed outright though all of them failed together (TOGETHER): False, to go on as
        before, when an entry left the queue meanwhile. The search gates the whole again, since main may have moved
        since its failure; when it passes now, every entry gates again. Otherwise it drops entries greedily while the
        rest still fail (fewest), and gates each one left alone: one that fails alone comes back alone, as a bisect
        sends one back; else each comes back naming the others, by ID and commit, and the rest gate again. A probe
        with no verdict, or stopped, ends the search as such a gate ends any round (Worker.older), and the bisect
        starts again from its halves."""
        entries = [*older, *beside.newer]
        current = [self.queue.read(entry["id"]) for entry in entries]
        if any(entry is None or entry["state"] not in WAITING for entry in current): return False
        runner = Beside(self, label=TOGETHER, settles=False)
        runner.checks = self.checks
        runner.prepare()
        order = {entry["id"]: index for index, entry in enumerate(entries)}
        # Each subset's verdict, by its entries' IDs: (True when it failed, False when it passed, None when its entries
        # do not apply to origin/main by themselves; its log; what rejection_facts reads of its gate).
        known = {frozenset(entry["id"] for entry in older): (False, None, {}),
                 frozenset(entry["id"] for entry in beside.newer): (False, beside.result[1], {})}
        probes = 0
        def fails(subset):
            nonlocal probes
            members = sorted(subset, key=lambda entry: order[entry["id"]])
            key = frozenset(entry["id"] for entry in members)
            if key not in known:
                runner.unreachable = []
                applied = runner.integrate(members, base)
                ids = ", ".join(entry["id"] for entry in members)
                if runner.unreachable or len(applied) != len(members):
                    runner.say(f"{ids} do not apply to origin/main by themselves, so none of them is dropped")
                    known[key] = (None, None, {})
                else:
                    probes += 1
                    head = runner.head()
                    runner.say(f"probe {probes}: gating {ids} at {head[:12]} on {base[:12]}")
                    result, log = runner.gate(lanes, f"{time.strftime('%Y%m%d-%H%M%S')}-{head[:8]}")
                    if result and (result in NO_VERDICT or result == WITHDRAWN or (
                            runner.unstarted(result)
                            and not (runner.tooling and runner.batch_stops_start(base, head, log)))):
                        raise Unsettled(members, result, log, head)
                    known[key] = (bool(result), log, {"failed_load": runner.failed_load, "failed_lane": runner.failed_lane,
                                                      "failed_dispatch": runner.failed_dispatch})
                    runner.say(f"probe {probes}: {ids} {'fail' if result else 'pass'}")
            return known[key][0] is True
        def failure(subset):
            """The log of the probe of subset, which failed, with runner's account of its gate put back."""
            failed, log, account = known[frozenset(entry["id"] for entry in subset)]
            for name, value in account.items(): setattr(runner, name, value)
            return log
        self.say(f"each half passed, though the {len(entries)} failed together: gating subsets of them for the fewest "
                 "that fail together")
        try:
            if not fails(entries):
                self.say(f"the {len(entries)} pass together on {base[:12]}: gating them again")
                self.queue.mark(entries, QUEUED)
                self.limit, self.suspects = None, 0
                return True
            fewest_ = fewest(entries, fails)
            alone = [entry for entry in fewest_ if len(fewest_) == 1 or fails([entry])]
        except Unsettled as unsettled:
            self.queue.mark(entries, QUEUED)
            self.say(f"the search for the entries that fail together ended without a verdict on "
                     f"{', '.join(entry['id'] for entry in unsettled.entries)}: the bisect starts again from its halves")
            self.older(config, unsettled.entries, base, unsettled.head, None, None, unsettled.result, unsettled.log,
                       round_name, reran=True, runner=runner)
            self.suspects, self.limit = len(entries), len(older)
            return True
        sent = alone or fewest_
        for entry in alone:
            log = failure([entry])
            why = (f"the queue's lanes fail on this commit on top of origin/main: fix it and add it again."
                   + runner.rejection_facts(entry, log, config, base))
            self.say(f"{entry['id']}: {why}")
            self.settle_failure(entry, log, why, runner)
        if not alone:
            log = failure(fewest_)
            for entry in fewest_:
                partners = [other for other in fewest_ if other["id"] != entry["id"]]
                why = together_words(partners) + runner.rejection_facts(entry, log, config, base)
                self.say(f"{entry['id']}: {why}")
                self.settle_failure(entry, log, why, runner,
                                    conflictWith=[{"id": other["id"], "sha": other["tip"]} for other in partners])
        rest = [entry for entry in entries if entry not in sent]
        self.queue.mark(rest, QUEUED)
        self.say(f"{len(sent)} of the {len(entries)} sent back after {probes} probe{'' if probes == 1 else 's'}"
                 + (f"; the other {len(rest)} gate again" if rest else ""))
        self.limit, self.suspects = None, 0
        return True

    def first_ungated(self):
        """The entry qk land --front --no-gate put first that goes ahead of this gate now, or None: one while the
        gate's lane waits to start, by its wait record (lane_wait), for admission, a screen or a Mac, so that a landing
        that needs only the preflight lanes and a push does not wait behind a gate that has not begun
        (vocaro/queuekeeper#26). The entry may be one this gate tests, given both options again since its round began
        (promote): the gate stops all the same, and the entry lands alone on the round after. An entry another gate
        tests is that gate's to stop. A lane that runs, or whose record says nothing yet, runs on, and the entry lands
        after it. An ungated round goes on as it is."""
        if not self.lane_now or self.ungating: return None
        gating = set(self.gating)
        first = next((entry for entry in self.queue.waiting() if entry.get("front") and entry.get("noGate")
                      and (entry["id"] in gating or entry["state"] == QUEUED)), None)
        if first is None: return None
        lane, began = self.lane_now[0], self.lane_now[1]
        record = lane_wait.current(lane_wait.path_of(self.checkout, lane), began)
        if not record or not record.get("kind"): return None
        self.ahead_wait = (lane, lane_wait.words(record))
        return first["id"]

    def stopped_for(self):
        """Why first_ungated stopped this gate, in words."""
        lane, waited = getattr(self, "ahead_wait", None) or ("its lane", None)
        return (f"{self.ahead} lands first without the gate, as qk land --front --no-gate asked, while {lane} waited "
                f"for {waited or 'its start'}")

    def ungated(self, applied, config):
        """What an ungated landing of applied records, the reason --why gave and the lanes it runs, when it is one
        entry qk land --no-gate put on its own; else None."""
        if not (self.alone and applied[0].get("noGate")): return None
        ungated = {"why": applied[0].get("aheadWhy") or "no reason was recorded", "lanes": ungated_lanes(config)}
        self.say(f"{applied[0]['id']} lands without the gate, as qk land --no-gate asked, because {ungated['why']}: "
                 f"running only {', '.join(ungated['lanes']) or 'no lane'}; the next gate on main covers it")
        return ungated

    def mark_ungated(self, ungated, base):
        """Add the UNGATED trailer to HEAD, an ungated landing's tip, and return the new HEAD; a HEAD at base, whose
        commits main already has, stays. The commit keeps its author, message and patch, by which Closeout finds its
        branch landed."""
        if self.head() == base: return base
        words = (f"qk land --no-gate ran {', '.join(ungated['lanes']) or 'no lane'} and the pre-push checks, not the "
                 f"gate, because {ungated['why']}; the next gate on main covers it")
        git(self.checkout, "commit", "--amend", "--no-verify", "--no-edit", "--allow-empty", "--trailer",
            f"{UNGATED}: {words}", hooks=False, check=True)
        return self.head()

    def no_verdict_cause(self, result, log):
        """Why the gate that exited result gave no verdict, for a hold's reason: the lane's limit it reached, the
        tests its log reports unmeasured, else the error its receipt records (#288), else the last line in which its log
        gives the project's own reason (lane_results.unmeasured_reasons, vocaro/queuekeeper#30), the reason a lane that
        was not run gave, such as a locked screen, else no admission, or a shared host's configuration."""
        if self.timed_out: return f"unmeasured: {self.timed_out}"
        if result == lane_results.UNMEASURED:
            try: said = log.read_text(errors="replace") if log else ""
            except OSError: said = ""
            found = lane_results.unmeasured_tests(said)
            if not found:
                error = self.receipt_error()
                if error: return f"unmeasured: {shortened(error)}"
                reasons = lane_results.unmeasured_reasons(said)
                if not reasons: return "unmeasured, with no test its log names"
                more = f" ({len(reasons)} such lines, the last)" if len(reasons) > 1 else ""
                return f"unmeasured, and its log says{more}: {shortened(reasons[-1])}"
            first = shortened(found[0])
            # The run's last reading of its screen, which queuekeeper writes for a lane whose result depends on it.
            screens = SCREEN_LINE.findall(said)
            return (f"unmeasured: {len(found)} test{'' if len(found) == 1 else 's'}, the first {first}"
                    + (f"; the last reading {screens[-1].strip()}" if screens else ""))
        if result == resources.BUSY:
            # The lane's own reason, such as a locked screen, when it gave one: a group gives none of its own when a
            # member was not run (vocaro/queuekeeper#200).
            try: said = log.read_text(errors="replace") if log else ""
            except OSError: said = ""
            refused = NOT_RUN_LINE.findall(said)
            return f"not run: {shortened(refused[-1].strip())}" if refused else "no admission: the host stayed at capacity"
        # A Mac whose provision lane failed before the lane says which Mac and where that lane's log is, this Mac
        # as much as the second (vocaro/queuekeeper#19).
        try: said = log.read_text(errors="replace") if log else ""
        except OSError: said = ""
        unprovisioned = [line for line in said.splitlines() if remote.UNPROVISIONED in line]
        if unprovisioned and result == remote.MISCONFIGURED:
            return unprovisioned[-1].partition(" not run: ")[2] or unprovisioned[-1]
        return "a shared host's configuration stopped it"

    def shared_timing(self, result, config):
        """What a hold after runs that gave no verdict adds when the last ended unmeasured (exit 3) in a lane that
        takes one slot of heavy-work admission and does not hold its Mac: admission looks only at the moment a run
        starts, so heavy work admitted or started beside it afterwards can void a timing in it, as mpm's gate's was
        on 2026-11-12 (vocaro/queuekeeper#28). The way to keep it off a timed part is exclusive admission of that
        part alone. Empty for any other run, or a lane that reached its
        limit."""
        if result != lane_results.UNMEASURED or self.timed_out or not self.lane_now: return ""
        lane = self.lane_now[0]
        settings = config.get("lanes", {}).get(lane)
        try:
            if not isinstance(settings, dict) or admission(settings) != "heavy" or settings.get("wholeHost"): return ""
        except ValueError:
            return ""
        return (f". {lane} takes one shared slot of heavy-work admission, which other heavy work can join after it "
                "starts: if a timing in it needs a quiet Mac, run that part alone under `qk admit --here --exclusive` "
                "from a `self` lane")

    def out_of_space(self, result, log):
        """Why the lane at which the last gate stopped with result, which gave no verdict, ran out of disk space, from
        the last line of its log that says so (lane_results.OUT_OF_SPACE), or None when it did not: an unmeasured run
        whose command the full disk stopped or failed, or whose log could not be written (vocaro/queuekeeper#162)."""
        if result != lane_results.UNMEASURED: return None
        try: said = log.read_text(errors="replace") if log else ""
        except OSError: return None
        found = [line.partition(lane_results.OUT_OF_SPACE)[2] for line in said.splitlines()
                 if lane_results.OUT_OF_SPACE in line]
        return shortened(found[-1]) if found else None

    def receipt_error(self):
        """The error this run's receipt records for the lane the last gate stopped at, on one line, or None when no
        receipt written since the lane began can be read or says one: the dispatch's, for a run on another Mac,
        else the one here. A dispatch's holds the lane's own receipt from that Mac, and that Mac's error, under
        result (remote.execute)."""
        if not self.lane_now: return None
        lane, began = self.lane_now[0], self.lane_now[1]
        ran = remote.dispatch(self.checkout, lane, began)[0]
        paths = ([ran / "source.json"] if ran is not None else []) \
            + [self.checkout / ".build/queuekeeper" / lane / "source.json"]
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
        which queuekeeper writes when a lane that depends on the screen ends unmeasured, else the log's last `screen:`
        line. A dispatch's receipt holds the lane's own, and so that reading, under result. No admission and a
        host's configuration ran nothing, and count as before (#185, #293)."""
        if result in UNRUN or not self.lane_now: return None
        lane, began = self.lane_now[0], self.lane_now[1]
        ran = remote.dispatch(self.checkout, lane, began)[0]
        receipts = ([(ran / "source.json", None, ("result", "receipt"))] if ran is not None else []) \
            + [(self.checkout / ".build/queuekeeper" / lane / "source.json", HERE_MAC, ())]
        for receipt, mac, within in receipts:
            try:
                if receipt.stat().st_mtime < began: continue
                after = json.loads(receipt.read_text())
                for key in (*within, "screenAfter"): after = after.get(key)
            except (OSError, ValueError, AttributeError):
                continue
            if isinstance(after, dict) and after.get("state"):
                if after["state"] != "locked": return None
                # This Mac with its name, and another by the name its receipt gives.
                return lane, offload_host.screen_line(after, f"{mac} ({after.get('mac') or 'unnamed'})" if mac
                                                      else after.get("mac") or "another Mac")
        try: said = log.read_text(errors="replace") if log else ""
        except OSError: said = ""
        screens = SCREEN_LINE.findall(said)
        return (lane, screens[-1].strip()) if screens and screens[-1].startswith("screen: locked") else None

    def needs_unlocked(self, lane, config):
        """Whether the gate's last run of lane went only to a Mac whose screen reads unlocked: as the worker asked, or
        as main's configuration declares it."""
        return self.sent_unlocked or unlocked_screen(config.get("lanes", {}), lane)

    def began_unlocked(self, lane, log, config):
        """Whether the gate's last run of lane read an unlocked screen at its start: by its placement, or by the first
        `screen:` line in its log, for a lane queuekeeper reads the screen for without placing it there (a display lane)."""
        if self.needs_unlocked(lane, config): return True
        try: said = log.read_text(errors="replace") if log else ""
        except OSError: said = ""
        screens = SCREEN_LINE.findall(said)
        return bool(screens) and screens[0].startswith("screen: unlocked")

    def settings(self, base, head):
        """The project's lanes and settings as they stand on main at base, so that no branch chooses its own gate,
        with the checkout left at head. Until main has a queuekeeper.json that names lanes, the batch's, less what
        only main may say (as_batch), for this round alone: the commit that adds a project's configuration, or first
        names its lanes, has to land (#10). Main then had no gate for the batch to loosen."""
        self.reset(base)
        config = self.judged(f"main at {base[:12]}") if has_config(self.checkout, base) else None
        self.reset(head)
        if config is not None:
            try: lanes_of(config)
            except ValueError: why = "names no lanes"
            else: return config
        else: why = "has no queuekeeper.json"
        self.say(f"main at {base[:12]} {why} yet: this round reads the batch's at {head[:12]}, all but its "
                 f"queue.{' and queue.'.join(MAIN_ONLY)}, and main's from the next landing on")
        return as_batch(self.judged(f"the batch at {head[:12]}"))

    def judged(self, what):
        """The checkout's configuration as this worker's code judges it: the toolkit at origin/main where a copy
        exists (toolkit, move_to), which the hold names. What it refuses, a session fixes, on main or in the
        toolkit, and nobody has to ask the owner."""
        try: return load(self.checkout)
        except (OSError, ValueError) as error:
            raise Hold(SESSION, f"{error}, judging {what} with the toolkit at "
                                f"{self.code[:12] if self.code else 'no commit'}") from error

    def keep(self, batch, base, head, docs, ungated=None, flakes=None):
        """Record a batch's pass before its push. A hold the push meets, a refusal or a remote out of reach, then
        leaves the batch passed rather than queued, and `qk queue resume` pushes head instead of gating the same
        tree again (vocaro-engineering#150): EPUBLib's queue spent an hour of another Mac doing that."""
        verdict = {"head": head, "base": base, "batch": [entry["id"] for entry in batch], "at": time.time()}
        if docs: verdict["docsOnly"] = docs
        if ungated: verdict["ungated"] = ungated
        if flakes: verdict["flakes"] = flakes
        if self.alone: verdict["alone"] = True
        if self.measured: verdict["gate"] = self.measured
        for entry in batch: self.queue.record(entry, state=PASSED, passed=verdict)
        self.queue.emit("batch.passed", head=head, base=base, entries=verdict["batch"])

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
        # What passed, not whatever the checkout holds: a worker that died mid-carry left part of a batch there.
        if self.head() != head: self.reset(head)
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
        """Raise NoLocalNetwork, before a round, when another Mac is configured and macOS denies this process the
        local network, as it does once the Claude session that started the worker ends: every gate would then go to
        this Mac, another Mac seeming away to this process alone, as from 13:48 to 16:28 on 2026-11-07
        (vocaro-engineering#282). A worker started by the next live session reaches it."""
        if not farm.destinations(): return
        lost = remote.local_network_lost()
        if lost: raise NoLocalNetwork(lost)


# What Beside.weigh records of a newer half that failed with several entries, which bisect once the older half has.
BISECT = "bisect"


class Unsettled(Exception):
    """A probe of Worker.together whose gate gave no verdict, was stopped or could not start: entries at head, and
    the gate's (result, log)."""
    def __init__(self, entries, result, log, head):
        super().__init__(f"exit {result}")
        self.entries, self.result, self.log, self.head = entries, result, log, head


def fewest(entries, fails):
    """The fewest of entries, oldest first, that still fail together, by fails, which says whether a subset does
    (Worker.together): greedy, dropping each run of entries the rest still fail without, the longest runs first and
    then shorter ones, down to single entries, so that every entry left is needed. One run of halves is each half of a
    bisect, whose verdicts are known, so a pair costs no probe here. Follows mergetrain's minimize_joint_failure
    (NOTICE), which drops one entry at a time."""
    kept = list(entries)
    size = len(kept) // 2
    while size:
        index = 0
        while index < len(kept) and len(kept) > 1:
            rest = kept[:index] + kept[index + size:]
            if rest and fails(rest): kept = rest
            else: index += size
        size //= 2
    return kept


def together_words(partners):
    """Why an entry that fails only with partners comes back (Worker.together), naming each by ID and commit."""
    named = ", ".join(f"{partner['id']} at {partner['tip']}" for partner in partners)
    one = len(partners) == 1
    return (f"the queue's lanes fail on this commit together with {named}, though each passes alone on top of "
            f"origin/main: the changes break each other, and {'that entry' if one else 'those entries'} came back too. "
            f"Rebase onto {'its commit' if one else 'their commits'} (refs/queuekeeper/queue/ID keeps each until its "
            f"entry is removed), fix how the changes combine, and add the result again; or wait for "
            f"{'it' if one else 'them'} to land and rebase onto origin/main.")


class Beside(Worker):
    """A bisect's newer half, gated in a checkout beside the worker's, on a Mac other than the older half's when one
    can start it now (owner's decision of 2026-11-07, vocaro-engineering#241; any two Macs since
    vocaro/queuekeeper#211). Both halves run on the same origin/main. What passes beside lands while the older half bisects on; when both pass outright, the
    worker searches for the entries that fail together (Worker.together), and otherwise, as when one passed only by
    its flakes, the older lands first and the newer gates again on the main that made, unless its result carries
    over (Worker.carry)."""
    def __init__(self, worker, checkout=None, label="newer half", settles=True):
        super().__init__(worker.queue)
        self.worker, self.label, self.settles = worker, label, settles
        self.role = label
        self.checkout = checkout or worker.queue.home / f"{worker.queue.main_checkout.name}{BESIDE_SUFFIX}"
        self.code, self.environment = worker.code, worker.environment
        self.unlocked = worker.unlocked  # the worker's own, which a lock either half meets adds to (screen_locked)
        self.regated = worker.regated  # the worker's own, so that each commit gates again at most once (rerun)
        self.unreachable = []  # the newer entries that apply to origin/main only after the older half (left_out)
        self.kept_for = []  # the entries whose changelog resolutions it recorded (noted)
        self.newer, self.tip = [], None  # the newer half on top of origin/main, and its commit (Worker.beside)
        self.lanes = None  # the lanes both halves run (Worker.beside)
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
        """Gate the newer half in this checkout while worker gates the older half, the two on different Macs, and
        return the older half's (status, log). The older half is offered as any gate is, to whichever Mac has room
        for it or admits it first. The newer half starts once a Mac has admitted the older half's final lane, and
        goes to any Mac but that one, this Mac among them, if one can start it now (apart). No such Mac, or an older
        half that ended before any Mac admitted its final lane, leaves the newer half with no verdict
        (resources.BUSY), which weigh queues again.

        Until vocaro/queuekeeper#211 the older half ran only on this Mac and the newer only on another. The newer
        half loses nothing by waiting for the older half's Mac: it runs the same lanes, so a Mac that could start
        it sooner would have taken the older half."""
        box, ended = {}, threading.Event()
        name = f"{time.strftime('%Y%m%d-%H%M%S')}-{self.tip[:8]}"
        self.round_name = name
        # What an earlier round's gate ran is not this one's (apart_from).
        worker.lane_now = None
        def gate():
            try:
                placed = self.apart_from(worker, lanes[-1], ended)
                if placed is None: return
                where, excluded, said_where = placed
                # Its own environment from here on: the worker's is the older half's too.
                if excluded is not None: self.environment = {**self.environment, remote.QUEUE_GATE_EXCLUDE: excluded}
                self.say(f"its lanes go {said_where.replace(' them ', ' it ')}")
                box["result"] = self.gate(lanes, name, where=where)
            except BaseException as error: box["error"] = error
        thread = threading.Thread(target=gate, name="newer half", daemon=True)
        thread.start()
        try: older = worker.gate(lanes, round_name)
        finally:
            ended.set()
            thread.join()
        error = box.get("error")
        if isinstance(error, processes.Interrupted):
            self.queue.mark(self.newer, QUEUED)
            raise error
        if error is not None: self.say(f"its lanes could not run: {error}")
        self.result = box.get("result") or (resources.BUSY, None)
        return older

    def apart_from(self, worker, final_lane, ended):
        """apart's answer for the Mac that admitted final_lane of worker's gate, the older half's, once one has, by
        that lane's wait record (admitted_mac), looked at every BESIDE_POLL seconds. Once ended is set, the older
        half's gate having ended, the Mac is the one that lane ran on, by the gate's own account (ran_on, ran_where),
        so that a lane shorter than a look still says its Mac. None when the lane ran nowhere, as when an earlier
        lane failed or no Mac admitted it, or ran on a Mac the farm no longer lists."""
        while True:
            mac = worker.admitted_mac(worker, final_lane)
            if mac is None and ended.is_set() and worker.lane_now and worker.lane_now[0] == final_lane:
                mac = lane_wait.farm_mac(worker.ran_on) if worker.ran_on else HERE_MAC if worker.ran_where else None
            if mac is not None:
                try: hosts = farm.destinations()
                except (OSError, ValueError): hosts = ()
                return apart(mac, hosts, waits=False)
            if ended.is_set(): return None
            ended.wait(BESIDE_POLL)

    def weigh(self, config, base, older_landed):
        """Settle the newer half by its lanes' result: send back the one entry that failed, queue several that failed
        to bisect next (BISECT), land a pass, or, once the older half landed, land it only when its result carries
        over, else queue it to gate again (PASSED). No verdict, another Mac declining among them, queues it."""
        result, log = self.result
        if result == WITHDRAWN:
            self.say(f"{self.stopped_for() if self.ahead else ', '.join(self.withdrawn) + ' left the queue'}, which "
                     "stopped its gate: queued again")
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
        unstarted = self.unstarted(result)
        if unstarted:
            # Never ran, as no verdict did: the older half meets the same and holds (Worker.older, #108).
            self.say(f"{self.failed_lane} could not start{self.ran_where}: {unstarted}; no commit is blamed: queued "
                     "again, behind the older half")
            self.queue.mark(self.newer, QUEUED)
            self.outcome = QUEUED
            return
        flakes = None
        if result:
            flakes = self.rerun(config, self.newer, base, self.tip, log, self.round_name)
            if not flakes and self.regate:
                self.queue.mark(self.newer, QUEUED)
                self.outcome = QUEUED
                return
            if not flakes:
                blamed = self.blame(self.newer)
                if self.unblamed and self.repeats(log):
                    self.outcome = QUEUED
                    return
                if blamed:
                    self.send_back(self.newer, log, *blamed, self.rejection_facts(blamed[0], log, config, base))
                    self.outcome = FAILED
                elif len(self.newer) == 1:
                    self.failed(self.newer, log, self.rejection_facts(self.newer[0], log, config, base))
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

    def failed(self, applied, log, facts=""):
        """The one newer entry that failed comes back, as a lone entry that fails does (Worker.failed)."""
        why = f"the queue's lanes fail on this commit on top of origin/main: fix it and add it again.{facts}"
        self.say(f"{applied[0]['id']}: {why}")
        self.settle_failure(applied[0], log, why)


class Stack:
    """Entries that arrived while a batch gates, gated stacked on its tip, head, in runner's checkout, on a Mac other
    than the batch's (where, with said_where its words; apart), in a thread of their own (owner's decision of
    2026-11-07, vocaro-engineering#241). The batch's push
    makes head origin/main, and then the next round adopts the stacked gate as its own (Worker.adopted); any other
    outcome throws it away (drop), canceling its lanes, with its entries queued again. It settles nothing itself."""
    def __init__(self, worker, runner, entries, base, lanes, where, below=(), paths=(),
                 said_where="on another Mac if it can start them now"):
        self.worker, self.runner, self.entries, self.base, self.lanes = worker, runner, entries, base, lanes
        self.paths = set(paths)  # what entries change, by which lanes_of chose lanes and adopted checks them
        self.where, self.said_where, self.tip, self.name = where, said_where, None, None
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
                        f"at {self.tip[:12]}, stacked on {self.base[:12]}, {self.said_where}")
        def gate():
            try:
                self.box["result"] = self.runner.gate(self.lanes, self.name, where=self.where, cancel=self.cancel)
                _, log = self.box["result"]
                if log is not None and self.not_admitted():
                    with contextlib.suppress(OSError), log.open("a") as kept:
                        kept.write("\n" + STACK_ADMISSION_DECLINED + "\n")
            except BaseException as error: self.box["error"] = error
        self.thread = threading.Thread(target=gate, name="stacked gate", daemon=True)
        self.thread.start()
        return True

    def done(self):
        return self.thread is None or not self.thread.is_alive()

    def peek(self):
        """(status, log) of a stacked gate that has ended, None for one whose lanes raised."""
        return self.box.get("result")

    def not_admitted(self):
        """Whether the stacked gate's last lane exited busy without running on any Mac."""
        result = self.peek()
        return result is not None and result[0] == resources.BUSY and not self.runner.ran_where

    def void(self):
        """Why a stacked gate that has ended cannot be kept, or None: it gave no verdict, its lanes raised, or an
        entry of it left the queue, which stopped them (Worker.gate)."""
        result = self.peek()
        if result is not None and result[0] == WITHDRAWN:
            if self.runner.ahead: return self.runner.stopped_for()
            return f"{', '.join(self.runner.withdrawn)} left the queue"
        if result is None or result[0] in NO_VERDICT:
            return f"it gave no verdict (exit {result[0] if result else resources.BUSY})"
        return None

    def stale(self, waiting):
        """Why the next round cannot adopt it, or None: an entry gone from the queue, or one qk land put first or
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
    """qk land's trial integration of a new entry (trial): the worker's own integrate, pick and changelog
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
    its gate, then those qk land --front put first, then the rest, each oldest first."""
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
    a merge in origin/main..tip brings in work of its own (landing_proofs.own_merges), as a merge of another branch does, or a
    commit conflicts. The merges are refreshes of the branch from main, as the desktop app's sync makes, which a
    queue must not put on main; the branch and its worktree are left as they are, and a commit main already has
    is passed over, as a round's pick does (vocaro-engineering#217)."""
    if landing_proofs.own_merges(lambda *words: git(root, *words), MAIN, tip) != []: return None
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
    """Why qk land queues nothing for a branch with merges, which linear could not replay. A merge that brings in
    commits origin/main lacks is another branch's work, which rebasing the branch's own commits would drop, so the
    refusal names those commits and the two ways to carry them (vocaro-engineering#262). Any other merge, as one
    whose replay conflicts, has only the branch's own commits to move."""
    read = lambda *words: git(root, *words)
    bringing = landing_proofs.own_merges(read, MAIN, tip)
    commits = landing_proofs.brought_in(read, MAIN, bringing) if bringing else None
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
    """What qk land says of a trial that let its entry through, line by line."""
    after = f" after {', '.join(found['against'])}" if found["against"] else ""
    lines = [f"checked in {elapsed:.1f}s: it applies onto origin/main at {base[:12]}{after}"
             + (", alone, as --front tests it" if front else "") + ", as a round would integrate it now"]
    lines += [f"a round would settle {kept}" for kept in found["kept"]]
    lines += [f"left out of the check, as a round would leave it: {identifier}, {why}"
              for identifier, why in found["left"].items()]
    return lines


def refusal(tip, found, base, root):
    """Why qk land queues nothing: the trial's conflict, with what it was checked against, its hunks, and the command
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
    words += (". An entry ahead that comes back may let it apply: `qk queue status` says, and `qk land` again "
              "checks again")
    if found["touching"]:
        many = len(found["touching"]) > 1
        words += (f". To be told when {'they leave' if many else 'it leaves'} the queue, run as a background command: "
                  f"python3 {shlex.quote(str(root))}/scripts/qk queue wait {' '.join(found['touching'])}, which says "
                  f"whether {'they all' if many else 'it'} landed, so rebase, or came back, so `qk land` may apply it "
                  "as it is" + (f"; `qk land --wait` waits for {'them' if many else 'it'} itself and checks again"
                                if found["waits"] else ""))
    return words


def run(root, *, if_waiting=False):
    """Be the repository's worker until nothing waits. A second worker exits at once. Anything
    unexpected holds the queue, with every entry still waiting, rather than guessing at a verdict; the hold's
    reason says whom it is for (worded). A cause that clears by itself, a remote out of reach past its backoff,
    runs the round again after each of CLEARING_WAITS first, this process staying the worker through the wait,
    and holds for a session only when the last try fails too."""
    # Its gate lanes inherit its signal dispositions, which must not depend on whether `qk land` ran as `&`.
    processes.default_background_signals()
    queue = Queue(root)
    progress, tries, lock = carried(), 0, kept_lock()
    while True:
        with queue.worker(lock) as free:
            lock = None
            if not free: return 0
            if if_waiting and (queue.held() or not queue.waiting()): return 0
            # A gate its worker's crash left running holds a slot and admission this worker needs.
            remote.stop_dead_dispatches()
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
                # Holding nothing: the next `qk land`, `qk queue wait` or `qk queue resume` starts a worker of its
                # session's, and a hold would wait for a session to resume it instead.
                queue.mark([entry for entry in queue.all() if entry["state"] == TESTING], QUEUED)
                worker.say(f"exits, holding nothing, since this worker cannot reach another Mac: {lost}; the next "
                           "`qk land`, `qk queue wait` or `qk queue resume` from a live session starts another")
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


def journal(root, as_json=False, since=None, kinds=(), following=False, out=print):
    """`qk queue events`: the journal's events (events), oldest first, as a line of words each or, with
    as_json, as the journal's own lines; then, when following, each new one as it is written, until interrupted."""
    queue, since, shown = Queue(root), since.timestamp() if since else None, set()
    def show(event):
        shown.add(event.get("id"))
        out(json.dumps(event) if as_json else events.words(event), flush=True)
    for event in events.read(queue.directory, since, kinds): show(event)
    if following:
        with contextlib.suppress(KeyboardInterrupt):
            # From the journal's start, less what is shown, so that nothing written since the reading is missed.
            for event in events.follow(queue.directory, kinds, whole=True):
                if event.get("id") not in shown and events.wanted(event, since): show(event)
    return 0


def passed_push(queue):
    """What a hold keeps of a batch that passed, for its reason: the push it waits for, and a push whose answer
    never came (publication-unverified)."""
    marked = next((entry for entry in queue.waiting() if entry.get("publishing")), None)
    unverified = f"; {unverified_said(marked['publishing'])}" if marked else ""
    passed = [entry for entry in queue.all() if entry["state"] == PASSED]
    # The resume's words last, which a hold's reason ends with before whom it is for.
    return unverified + (passed_words(passed) if passed else "")


def unverified_said(marker):
    """Words for a push whose answer never came, by its record (Worker.push's publishing)."""
    heads = ", ".join(attempt["head"][:12] for attempt in marker.get("attempts", []))
    return (f"{PUBLICATION_UNVERIFIED}: pushed as {heads or 'nothing'} without an answer; the worker's next round "
            "fetches origin/main and lands it if main holds that push, or pushes or gates it again if not")


def passed_words(passed):
    """passed_push's words for the entries of a batch that passed."""
    verdict = passed[0]["passed"]
    return (f"; passed {verdict['head'][:12]} on {verdict['base'][:12]} ({len(passed)} "
            f"entr{'y' if len(passed) == 1 else 'ies'}), which the resume pushes without gating again")


def resume(root):
    queue = Queue(root)
    was = queue.held()
    with contextlib.suppress(OSError): queue.hold_file.unlink()
    if was: queue.emit("queue.resumed", was=was)
    queue.start()
    print(f"{len(queue.waiting())} waiting; worker {'running' if queue.working() else 'not needed'}")
    return 0


def close(root, why, by=None):
    """`qk queue close --why TEXT`: refuse new entries from now on, for maintenance that needs the queue empty, which
    then waits for `qk queue status --idle` and reopens it (vocaro/queuekeeper#32). by names who closes it, the
    session and worktree the command ran in unless --by says. A queue already closed stays as it was closed, with
    status 1, so that two sessions do not each think the closure theirs and reopen under the other."""
    if not why or not why.strip(): raise ValueError("qk queue close needs --why TEXT, which each refused `qk land` is told")
    queue = Queue(root)
    by = " ".join((by or resources.whence(resources.work_origin()) or "an unnamed session").split())
    already = queue.close(" ".join(why.split()), by)
    if already:
        print(f"queuekeeper: the queue is already {closed_words(already)}; it stays as that closed it, and one "
              "`qk queue reopen` opens it for everyone", file=sys.stderr)
        return 1
    waiting = len(queue.waiting())
    print(f"{closed_words(queue.closed())}\n{waiting} waiting, which the worker finishes; `qk queue status --idle` "
          "exits 0 once none is left and no worker runs, and `qk queue reopen` takes new entries again")
    return 0


def reopen(root):
    """`qk queue reopen`: take new entries again. It touches no hold and starts no worker."""
    was = Queue(root).reopen()
    print(f"reopened: the queue was {closed_words(was)}" if was else "the queue was not closed")
    return 0


def land_arguments(parser):
    parser.add_argument("reference", nargs="?", help="the commit or branch; HEAD by default, and required with --repo")
    parser.add_argument("--repo", help="another project to land in: its path, or its name beside this project")
    parser.add_argument("--wait", action="store_true", help="follow it to its result and print the landed commit: "
                        "0 landed, 1 came back or was refused, as it conflicts with what is ahead of it, 75 held or "
                        "still waiting at the limit. Refused for entries ahead alone, it waits for them and checks "
                        "again")
    parser.add_argument("--limit", type=float, default=WAIT_LIMIT, metavar="SECONDS")
    parser.add_argument("--note", help="what the entry is, for `qk queue status`")
    parser.add_argument("--json", action="store_true", help="write one versioned JSON result; progress goes to standard error")
    options(parser)


def options(parser):
    """qk land's emergency options, in every repository, which `qk queue add` takes too."""
    parser.add_argument("--front", action="store_true", help="for an emergency, such as an important fix stuck behind "
                        "queued changes: test it next, ahead of everything waiting, as a batch of its own, once a gate "
                        "already running finishes. Again with a commit already queued, it moves that entry")
    parser.add_argument("--no-gate", action="store_true", help="for an emergency, such as an important fix held up by "
                        "an unrelated flaky test: land it alone on the project's preflight lanes and "
                        "the pre-push hook, without the gate, and record it as ungated, which the next gate on main "
                        "covers. Needs --why; tell the owner you used it and why. With --front it lands next: ahead "
                        "of a round whose gate waits to start, or once a gate already running finishes")
    parser.add_argument("--why", metavar="TEXT", help="the reason for --front or --no-gate, which --no-gate requires: "
                        "the entry, the log, the landed commit's trailer and `qk queue status` record it")


def add_arguments(parser):
    operations = parser.add_subparsers(dest="operation", required=True)
    adding = operations.add_parser("add", help="queue the commits a ref has and origin/main lacks, and return at once")
    adding.add_argument("reference", nargs="?", default="HEAD")
    adding.add_argument("--note", help="what the entry is, for `qk queue status`")
    adding.add_argument("--json", action="store_true", help="write one versioned JSON submission result")
    options(adding)
    state = operations.add_parser("status", help="say what is waiting, what is running, what came back and, when the "
                                  "queue is held, why and for whom")
    state.add_argument("--json", action="store_true")
    state.add_argument("--idle", action="store_true",
                       help=f"exit 0 only when no worker runs and no entry is queued, testing or passed, and "
                       f"{resources.BUSY} otherwise; the lines printed are the same")
    doctoring = operations.add_parser("doctor", help="read the queue for the signs that it is stuck rather than testing, "
                                      "such as gates that ran nothing, and say what each means and what heals it; it "
                                      "changes nothing: 0 healthy, 1 with findings")
    doctoring.add_argument("--json", action="store_true")
    healthy = operations.add_parser("health", help="say in one sentence whether the queue is healthy, by its progress, "
                                    "its pace, its Macs, its devices and what came back, and when the entries waiting "
                                    "should land; a sign it cannot read is unknown, never healthy: 0 healthy or quiet, "
                                    "1 needs attention, 3 cannot tell")
    healthy.add_argument("--json", action="store_true")
    placing =operations.add_parser("placement", help="probe current host capacity for a gate lane without queuing it")
    placing.add_argument("lane", nargs="?", help="lane on origin/main (default: the first configured gate lane)")
    placing.add_argument("--json", action="store_true")
    following = operations.add_parser("wait", help="follow an entry to its result, or several, such as the entries an "
                                       "qk land conflict names, until none waits: 0 landed, or each did, 1 came back, "
                                       "or one did, 75 held or still waiting at the limit")
    following.add_argument("id", nargs="+")
    following.add_argument("--limit", type=float, default=WAIT_LIMIT, metavar="SECONDS")
    following.add_argument("--json", action="store_true", help="write one versioned JSON result; progress goes to standard error")
    logs = operations.add_parser("logs", help="read a bounded portion of an entry's exact log with a continuation cursor")
    logs.add_argument("id", help="entry whose exact recorded logs are read, including settled entries")
    logs.add_argument("--json", action="store_true", help="one JSON chunk with content, artifact and nextCursor")
    logs.add_argument("--offset", type=int, default=0, help="starting byte offset (default: 0); use nextOffset to continue")
    logs.add_argument("--limit", type=int, default=16384, help="maximum bytes per read, 4..1048576 (default: 16384)")
    logs.add_argument("--artifact", help="recorded log kind or full path; full path resolves ambiguous kinds")
    logs.add_argument("--cursor", help="nextCursor from the previous read; detects replaced or truncated logs")
    result = operations.add_parser("result", help="read one entry and its durable result without starting a worker")
    result.add_argument("id", help="entry to look up directly, including old settled entries")
    result.add_argument("--json", action="store_true", help="one versioned result; exit 0 means lookup succeeded, not gate passed")
    told = operations.add_parser("events", help="print the queue's journal, one line for each change of its state: "
                                 "entries queued, testing, landed and sent back, rounds, gates, flaky tests and holds")
    told.add_argument("--json", action="store_true", help="each event as the journal holds it, one JSON object a line")
    told.add_argument("--follow", action="store_true", help="then print each new event as it happens, until interrupted")
    told.add_argument("--since", type=resources.local_time, metavar="WHEN", help="only events from WHEN, an ISO 8601 "
                      "date or time, local unless it names a zone")
    told.add_argument("--kind", action="append", default=[], metavar="KIND", help="only events of KIND, such as "
                      "entry.landed, or of every kind that begins with it when it ends in a dot, such as entry.; "
                      "repeatable")
    removing = operations.add_parser("remove", help="take an entry out; a gate testing it stops within "
                                   "seconds on whichever Mac runs it, and the rest of its batch gates again")
    removing.add_argument("id")
    operations.add_parser("resume", help="clear a hold and start the worker: any session does this itself once the "
                          "hold's cause is dealt with or gone, and asks the owner only for a hold that says it is his")
    closing = operations.add_parser("close", help="refuse new entries from now on, saying why and who closed it, "
                                    "while the worker finishes the entries the queue has; for maintenance that needs "
                                    "the queue empty, which waits for `status --idle` and then runs `reopen`: 0 "
                                    "closed, 1 already closed")
    closing.add_argument("--why", required=True, metavar="TEXT", help="the reason, which each refused `qk land` and "
                         "`qk queue status` say")
    closing.add_argument("--by", metavar="WHO", help="who closes it, for the same lines (default: the repository, "
                         "branch, worktree, agent session and Mac the command ran in)")
    operations.add_parser("reopen", help="take new entries again after `close`; it clears no hold")
    running = operations.add_parser("run", help="be the worker in the foreground; `add` starts one by itself")
    running.add_argument("--if-waiting", action="store_true", help="do nothing when the queue is empty or held")
    launcher = operations.add_parser("launcher-plist", help="print this queue's LaunchAgent for a signed launcher app")
    launcher.add_argument("app", type=Path)


def main(root, args):
    if args.operation == "logs":
        from . import agent_logs
        return agent_logs.main(root, args)
    if args.operation == "result":
        from . import agent_results
        return agent_results.result(root, args.id, args.json)
    if args.operation in {"add", "wait"} and getattr(args, "json", False):
        from . import agent_cli
        if args.operation == "add":
            details = {"root": root}
            def submit():
                details["id"] = add(root, args.reference, args.note, quiet_wait=True,
                                    front=args.front, no_gate=args.no_gate, why=args.why)
                return 0
            return agent_cli.execute(root, "queue.add", submit, reference=args.reference, details=details)
        return agent_cli.execute(root, "queue.wait",
                                 lambda: wait(root, args.id[0], args.limit) if len(args.id) == 1
                                 else wait_for(root, args.id, args.limit), identifiers=args.id)
    if args.operation == "add":
        try: add(root, args.reference, args.note, front=args.front, no_gate=args.no_gate, why=args.why)
        except Refused as error:
            print(f"queuekeeper: {error}", file=sys.stderr)
            return 1
        return 0
    if args.operation == "status": return status(root, args.json, idle=args.idle)
    if args.operation == "doctor": return doctor(root, args.json)
    if args.operation == "health":
        from . import health
        return health.main(root, args.json)
    if args.operation == "placement": return placement(root, args.lane, args.json)
    if args.operation == "wait":
        return wait(root, args.id[0], args.limit) if len(args.id) == 1 else wait_for(root, args.id, args.limit)
    if args.operation == "events": return journal(root, args.json, args.since, args.kind, args.follow)
    if args.operation == "remove": return remove(Queue(root), args.id)
    if args.operation == "resume": return resume(root)
    if args.operation == "close": return close(root, args.why, args.by)
    if args.operation == "reopen": return reopen(root)
    if args.operation == "launcher-plist":
        sys.stdout.write(plistlib.dumps(Queue(root).launcher_plist(args.app)).decode())
        return 0
    return run(root, if_waiting=args.if_waiting)
