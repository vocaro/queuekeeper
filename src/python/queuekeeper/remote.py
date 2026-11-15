"""Run a lane or an admitted command on the owner's second Mac, reached over SSH.

Nothing here takes a slot on this host. The exact commit, and the toolkit commit that runs it,
go to bare repositories on the second Mac by git over SSH, never through GitHub. A working tree
with uncommitted changes goes as a snapshot commit on HEAD, which no branch holds. offload_host.py,
sent along and run there by the host's own Python, checks them out in a persistent checkout,
so builds stay warm, and runs `qk run` or `qk admit` under the second Mac's own heavy-work
admission: that host's queue, not a private pool beside this one. A push sends only what the
second Mac's checkouts lack, which their refs name there. The log and the result come back to
this checkout's .build/queuekeeper/remote, each dispatch's in a directory of its own as well, with
what its preparation cost, and a lane group's lanes' logs and receipts with them once the run
there ends. A whole lane's result at a commit is also kept by commit in
the repository's Git directory, where every worktree of the repository can find it, and so is a
whole lane's run on this Mac at a commit with no working changes (keep_here); a snapshot's is not,
and queuekeeper says so when it sends one.

`qk run` sends every admitted lane to whichever Mac has spare capacity for it now,
the second Mac first, unless the lane says why it stays on this Mac, and `qk admit` a
command the same way, unless `--here` or its paths and devices keep it on this Mac; what the command
changed in the checkout's files that Git does not ignore comes back as a patch and is applied here
(brought_back), while its build products stay there (the owner's decision, 2026-11-07). Before it
offers the work it reads both Macs' capacity (offload_host.capacity, spare): the second Mac runs the work when it has a guaranteed
slot free, all of them for a whole-host lane, its CPU's busy share is under its limit and no whole-host lane
or exclusive work holds it, and then only if it can start the work now; when it has no spare
capacity and this Mac has, this Mac takes the work at once, with the second Mac's reason in the
receipt (Loaded); and while neither has, the work goes to whichever admits it first, offered to
the second Mac again and again (overflow). Both Macs are for work, and a session never asks the
owner which (the owner's decisions, 2026-10-28 and 2026-11-04, vocaro-engineering#168). First it
checks, within seconds, that the second Mac can be reached. When it cannot, because its name does not resolve, as when
this Mac is away from the second Mac's network, nothing answers on its ssh port or ssh cannot
connect, the work runs on this Mac at once (the owner's decision, 2026-10-27). A second Mac that
answers but is misconfigured, such as one that refuses the login or has another Xcode, stops the
work for the owner's decision (the toolkit's agents/core.md, Shared resources); only `--here` then runs it
here. A run that was sent and lost its connection is unmeasured and runs nowhere else.
`qk doctor` compares the two Macs and times how ssh connects to the second Mac, and reports a
second Mac it cannot reach as unchecked, and gives both the same simulator runtimes, as
`qk remote setup --runtimes` does, without asking the owner (the owner's decision, 2026-11-03).
Before it sends work, and
in `qk doctor`, it lists the processes there that earlier runs left running, which nothing
stops, and fast-forwards the second Mac's own toolkit checkout, ~/Development/queuekeeper, to
origin/main when that checkout is on main with no uncommitted changes, saying why it leaves it otherwise:
a dispatch goes on, and `qk doctor` reports an error (offload_host.update_toolkit, vocaro-engineering#203).
"""
import base64
import binascii
import calendar
import contextlib
import ctypes
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zlib

from . import disk, extensions, lane_results, lane_wait, offload_host, parallel, processes, resources, git_state
from .config import TOOLKIT_ROOT, exclusive_refusal, sibling_folders, unlocked_screen
from .resources import BUSY, FREE_WAIT

# The SSH destinations tried in turn when neither --host nor QUEUEKEEPER_OFFLOAD_HOST names one: aliases the
# owner defines in ~/.ssh/config, so no machine's name is written here. build-host, which the owner
# defined first, stays the default; second-mac serves once it is the one defined (guides/shared-host.md,
# Earlier names).
DEFAULT_HOST = "build-host"
ALIASES = (DEFAULT_HOST, "second-mac")
# The variable that names the second Mac's SSH destination, or none for this Mac only, and the name it
# had until vocaro-engineering#58, which queuekeeper refuses (named_host).
HOST_VARIABLE, OLD_HOST_VARIABLE = "QUEUEKEEPER_OFFLOAD_HOST", "VOCARO_BUILD_HOST"
GATE_HOSTS = "QUEUEKEEPER_GATE_HOSTS"
GATE_HOSTS_FILE = "gate-hosts"
# The second Mac's directory for repositories and checkouts, relative to its home. It keeps the build
# host's name: another would leave every warm checkout there behind.
ROOT = "Development/build-host"
# Persistent checkouts per project on the second Mac; more wait for one to come free.
CHECKOUTS = 4
# Where each dispatch's own results stay, unique to it, under .build/queuekeeper/remote/DISPATCHES/NAME/,
# beside NAME/, the latest; and how many of each name's stay: two for each run that can overlap another.
DISPATCHES = ".dispatches"
DISPATCH_HOST = "dispatch-host"
DISPATCHES_KEPT = 2 * CHECKOUTS
# Seconds ssh waits for each address the second Mac's name resolves to before it tries the next.
CONNECT_TIMEOUT = 10
# ssh closes a connection that has heard nothing from the second Mac for about ServerAliveInterval
# times ServerAliveCountMax seconds, two minutes (ssh_config(5)); a shorter silence leaves it open.
SSH = ["ssh", "-T", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={CONNECT_TIMEOUT}", "-o", "ServerAliveInterval=30",
       "-o", "ServerAliveCountMax=4"]
# What getaddrinfo is asked for under each of ssh's AddressFamily values.
FAMILIES = {"any": socket.AF_UNSPEC, "inet": socket.AF_INET, "inet6": socket.AF_INET6}
# Seconds of sleep below which a dispatch is not said to have slept: the two clocks compared may
# differ by a little without any.
SLEEP_NOTED = 5
# How ssh's lines tell the phase a connection timed out in (OpenSSH 10.3p1, against a listener on
# 127.0.0.1, 2026-10-27). A TCP connection that gets no answer in ConnectTimeout ends with the first;
# one that is made but brings no SSH banner in ConnectTimeout says the second, then the third; one that
# brings a banner and then nothing for ServerAliveInterval times ServerAliveCountMax says only the third.
NO_ANSWER = re.compile(r"^ssh: connect to host \S+ port \d+: Operation timed out$")
NO_BANNER = "Connection timed out during banner exchange"
TIMED_OUT = re.compile(r"^Connection to \S+ port \d+ timed out$")
# The refs that carry a commit into the second Mac's repositories. They keep the build host's name, as
# ROOT does: those repositories hold refs under it, which another name would orphan.
REF = "refs/queuekeeper/hosts/"
# Seconds a push of one commit to the second Mac may take before queuekeeper stops it. A push there once ran
# for over 50 minutes before it failed (vocaro-engineering#67); one that sends only what the second
# Mac lacks takes seconds, and a lost connection ends within ssh's two minutes.
SEND_TIMEOUT = 1800
# Seconds a stopped push has to end, with everything it started, before it is killed.
SEND_STOP_WAIT = 10
# What Git's receive-pack says when the ref a push creates already exists: another session's push of the
# same commit took it first (Git 2.54.0, files backend).
RACED = re.compile(r"cannot lock ref .*reference already exists")
# Pushes of one commit a send makes when each loses that race and the second Mac then names the commit
# neither by its pushed ref nor by a checkout's: the winning dispatch places the commit, which deletes
# the pushed ref, and names it as a checkout's a moment later (offload_host.place). A push that finds
# neither ref creates the pushed one again, sending nothing, so a second push settles it; a third covers
# a second dispatch that wins and places in that moment too. RACE_PAUSE seconds before each, times its
# number (vocaro-engineering#152).
RACE_PUSHES, RACE_PAUSE = 3, 0.5
# What `git push --progress` prints of the pack it sends (Git 2.54.0): its progress lines, the last
# line of writing it, with the bytes written as Git rounds them, and its count of objects.
PROGRESS = re.compile(r"^(Enumerating objects|Counting objects|Delta compression|Compressing objects|Writing objects|"
                      r"Total \d+)")
WRITTEN = re.compile(r"^Writing objects: 100% \(\d+/\d+\), ([\d.]+) (bytes|KiB|MiB|GiB)\b.*done\.$")
TOTAL = re.compile(r"^Total (\d+) \(delta \d+\), reused (\d+)")
UNITS = {"bytes": 1, "KiB": 1 << 10, "MiB": 1 << 20, "GiB": 1 << 30}
# What Git says when push.negotiate's exchange failed and the push went on without it, as it does when
# protocol v2 does not reach the second Mac.
NEGOTIATION_FAILED = "push negotiation failed"
# Seconds a stopped run has to end on the second Mac before the connection is closed.
STOP_WAIT = 120
# Seconds a run the dispatcher lost contact with may wait on the second Mac beyond its own timeouts: for a checkout
# (offload_host.CLAIM_TIMEOUT) or for its admission, whose wait `qk run` and `qk admit` bound by default at this.
UNATTENDED_WAIT = 5400 + 300
# Seconds between tries at a connection to collect a run from, and how long past that run's own bound a
# dispatcher keeps trying, in this Mac's waking time (time.monotonic stops for sleep on macOS).
REATTACH_PAUSE, REATTACH_GRACE = 15, 300
# Seconds between the dispatcher's heartbeats on the run's standard input, and the seconds with none after which the
# second Mac stops a run whose connection's end is a stop: one with a device lease, or one that has not said its
# dispatcher heard it start (offload_host.watch; vocaro-engineering#287). A Mac that crashed closes nothing. The
# silence outlasts ssh's own two-minute bound, so that whatever silence ssh survives, the run survives too.
HEARTBEAT_SECONDS, HEARTBEAT_SILENCE = 20, 180
# Seconds one connection may take to stop a dead dispatcher's run (stop_dead_dispatches).
STOP_DEAD_WAIT = 30
# The toolkit's repository, which its origin names for the second Mac's policy refresh.
UPSTREAM = "https://github.com/vocaro/queuekeeper.git"
# What a Git hook exports for its own repository must not reach another.
GIT_LOCATION = {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_PREFIX"}
# `qk run`'s status when the second Mac's configuration stopped a lane: sysexits' EX_CONFIG.
MISCONFIGURED = 78
# What says that a Mac's provision lane failed before a lane there, on either Mac (execute, cli.provisioned_here):
# the merge queue quotes the line that holds it as a hold's cause (merge_queue.Worker.no_verdict_cause).
UNPROVISIONED = "could not provision its checkout"
# `qk run`'s status when a queue worker's gate found its process without Local Network access and ran nothing, here
# or there: sysexits' EX_UNAVAILABLE. No verdict, on which the worker exits, holding nothing (vocaro-engineering#300).
LOST_ACCESS = 69
# Set by a queue worker on the `qk run` of a gate's lane, the one `qk run` that stops for LOST_ACCESS. That `qk run`
# takes it out of its environment, so that no lane it runs here, nor what that lane starts, stops for it.
QUEUE_GATE = "QUEUEKEEPER_QUEUE_GATE"
QUEUE_GATE_EXCLUDE = "QUEUEKEEPER_QUEUE_GATE_EXCLUDE"
# Seconds the check before a dispatch gives each address of the second Mac's name to answer on its ssh
# port, and then `ssh true` to finish there, login shell included: an offline second Mac costs a few
# seconds, where ssh's ConnectTimeout would take ten for each address.
REACH_TIMEOUT, REACH_SECONDS = 3, 30
# ssh's own lines when it could not connect at all, before any login: a name that does not resolve, an
# address that refuses or never answers, a connection closed during the banner exchange. Nothing it was
# to run reached the second Mac.
OFFLINE = re.compile(r"Could not resolve hostname|connect to host \S+ port \d+:|banner exchange|"
                     r"kex_exchange_identification:")
# ssh's own lines when it connected and the second Mac, or this Mac's ssh configuration, refused the login.
LOGIN = re.compile(r"Permission denied|Host key verification failed|Too many authentication failures|"
                   r"REMOTE HOST IDENTIFICATION HAS CHANGED|no matching host key type")
# ssh's own lines when the name it reached presented another key than the one known_hosts records for it,
# as when another machine took the second Mac's old .local name.
OTHER_KEY = re.compile(r"Host key verification failed|REMOTE HOST IDENTIFICATION HAS CHANGED")
# Seconds the search for a renamed second Mac browses Bonjour for Macs that offer ssh, then gives each
# one's name to resolve, and at most how many it tries. Answers this Mac's mDNSResponder holds come in
# under 0.1 s (macOS 27.0.1, 2026-11-01).
BROWSE_SECONDS, RESOLVE_SECONDS, CANDIDATES = 2, 2, 8
# What `dns-sd -B _ssh._tcp local.` prints of a Mac that offers ssh, and `dns-sd -L` of where it is. dns-sd
# pads an hour before 10 with a space (" 9:46:14.615  Add"), which hid every Mac each morning until
# 2026-11-03 (vocaro-engineering#129).
BROWSED = re.compile(r"^\s*\S+\s+Add\s+\d+\s+\d+\s+(\S+)\s+_ssh\._tcp\.\s+(.+?)\s*$")
RESOLVED = re.compile(r"can be reached at (\S+?)\.?:(\d+)\b")
# The second Mac found under a new .local name in this process, by destination: what it was renamed
# from and to, and the name its key is known by (rediscover).
RENAMED = {}
# The second Mac searched for in this process and not found, by destination: the names Bonjour offered
# ssh under that were asked for its key, none presenting it (rediscover).
UNFOUND = {}
# Set by offload_host.py for the work it runs: the name of the Mac that sent it, which a receipt records.
DISPATCHER = "QUEUEKEEPER_DISPATCHED_FROM"
# Seconds `qk doctor` waits for the second Mac to report its Xcodes and runtimes.
SURVEY_TIMEOUT = 180
# Seconds `qk doctor` waits for the census of the second Mac's leftover processes.
CENSUS_TIMEOUT = 60
# Where a leftover process is explained.
LEFTOVER_GUIDE = "guides/shared-host.md, Processes left over from runs"
# The author and committer of a snapshot commit, which records a working tree rather than anyone's
# work, and needs no identity configured in the session's git.
SNAPSHOT_IDENTITY = {"GIT_AUTHOR_NAME": "queuekeeper snapshot", "GIT_AUTHOR_EMAIL": "snapshot@queuekeeper.invalid",
                     "GIT_COMMITTER_NAME": "queuekeeper snapshot", "GIT_COMMITTER_EMAIL": "snapshot@queuekeeper.invalid"}


# What every project's gates use on the second Mac (guides/shared-host.md, Setting up the second Mac).
SETUP_FORMULAE = ["python", "uv", "shellcheck", "xcodegen", "zstd"]
# Where Homebrew's installer puts brew: Apple silicon, then Intel.
BREW = ["/opt/homebrew/bin/brew", "/usr/local/bin/brew"]
# The exit status of the setup script when the second Mac has no Homebrew.
NO_HOMEBREW = 4
FORMULA = re.compile(r"^[A-Za-z0-9][A-Za-z0-9@._+/-]*$")
MODULE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")

# How messages name this Mac beside the second Mac.
HERE = "this Mac"
# Where a runtime image copied from the other Mac is staged until simctl adds it: on the second Mac
# under ~/Development/build-host, and here among the toolkit's durable state, never in /private/tmp,
# which a reboot empties. Both are relative to the Mac's home.
RUNTIME_STAGING = f"{ROOT}/runtimes"
LOCAL_RUNTIME_STAGING = "Library/Application Support/queuekeeper/runtimes"
# Free bytes a runtime's copy or download must leave on the volume that receives it: room for simctl
# to copy the image once more where it cannot clone it, as its help says it may, and for the 8-14 GB
# building worktrees of the sessions that share the volume (the toolkit's agents/core.md, Concurrent work).
RUNTIME_MARGIN = 40 << 30
# The bytes a download is taken to need when no image says: more than the largest runtime image on
# either Mac, iOS 26.5's 8.5 GB (October 2026).
RUNTIME_ESTIMATE = 10 << 30
# The platforms whose runtimes `xcodebuild -downloadPlatform` downloads (Xcode 27.2's help).
DOWNLOADABLE = {"iOS", "watchOS", "tvOS", "visionOS"}
# A runtime as offload_host.toolchain names it: NAME (BUILD).
RUNTIME = re.compile(r"^(.*) \(([^()]+)\)$")


class Lost(RuntimeError):
    """The second Mac took the work, the connection ended, and no result came back on any connection after it:
    unmeasured, not failed."""


class Unavailable(RuntimeError):
    """The second Mac did not take the work, and nothing of it ran there."""


class Busy(Unavailable):
    """The second Mac could not start the work in time, or at once when the work asked it not to wait:
    its queue or every checkout is in use. A busy second Mac is no configuration problem, and this Mac
    takes the work while it is busy, if it can (overflow)."""


class Loaded(Busy):
    """The second Mac has no spare capacity for the work now, by its reading, while this Mac has, by its
    own (spare): nothing was offered, and this Mac takes the work. Its text is the reason, as the
    receipt's hereBecause gives it, naming the second Mac's CPU busy share when that is it (#168, #200).
    The second Mac is read again before each later offer (Offer.again), so that it takes the work once
    it has capacity before this Mac admits it."""


class Misconfigured(Unavailable):
    """The second Mac's configuration kept the work from running there: the owner's to fix, and
    no reason to run it on this Mac without the owner's decision (the toolkit's agents/core.md, Shared resources).
    problem says what is wrong, and fix what fixing it takes."""

    def __init__(self, problem, fix):
        super().__init__(f"{problem}; to fix it, {fix}")
        self.problem, self.fix = problem, fix


class Unreachable(Unavailable):
    """The second Mac could not be reached, so nothing was sent: its name did not resolve, nothing
    answered on its ssh port, or ssh could not connect. An offline second Mac is no configuration
    problem, and the work runs on this Mac instead (the owner's decision, 2026-10-27)."""

    def __init__(self, problem, fix):
        super().__init__(f"{problem}; to fix it, {fix}")
        self.problem, self.fix = problem, fix


class NoLocalNetwork(Unreachable):
    """This process cannot reach the second Mac though the second Mac may be up: macOS's Local Network privacy
    denies it, since the process macOS holds responsible for it is a Python of queuekeeper's own, not an app that holds the
    owner's grant (local_network_lost). Another process on this Mac may reach the second Mac all the while."""


def ssh_failure(host, said, *context, sent=False):
    """What ssh's own failure, its exit of 255, makes of the second Mac before any work started there. said
    is what ssh wrote, whose last line names the failure, and unreachable() describes it from said and
    context, such as asleep()'s reading when ssh started. A refused login is a configuration problem
    (Misconfigured). Before the work was sent, every other failure, or none said, is a second Mac that
    cannot be reached (Unreachable). Once it was sent, only one in which ssh never logged in is: a last
    line that says ssh never connected (OFFLINE), or no SSH banner at all (NO_BANNER); the rest stops
    the work as unreachable() says, since the work may have started there."""
    line = last_line(said)
    banner = NO_BANNER in [entry.strip() for entry in (said or "").splitlines()]
    if line and LOGIN.search(line):
        found = searched(host) if OTHER_KEY.search(line) else ""
        return Misconfigured(f"ssh reached {host} but could not log in: {line}{found}",
                             f"correct its entry in ~/.ssh/config, its host key in ~/.ssh/known_hosts or this Mac's key "
                             f"in its ~/.ssh/authorized_keys, until `ssh {host} true` succeeds without a prompt "
                             "(guides/shared-host.md, Setting up the second Mac)")
    stop = unreachable(host, said, *context)
    if sent and line and not OFFLINE.search(line) and not banner: return stop
    return Unreachable(stop.problem, stop.fix)


def reachable(host, found=None):
    """Check, before anything is sent, that ssh reaches host, within seconds when it cannot: its name
    resolves, an address of it answers on its ssh port within REACH_TIMEOUT, as connection() measures,
    and `ssh HOST true` finishes in its login shell within REACH_SECONDS. found is connection()'s
    measurement, when one was taken already. A host ssh reaches through a proxy is checked by ssh
    alone. Raise Unreachable when it cannot be reached, and Misconfigured when it answers but refuses
    the login or its login shell fails.

    A .local name that does not resolve, or that answers with another key, may be a second Mac that
    macOS renamed (rediscover): one found under a new name is checked there, and every connection of
    this process to host then goes to that name. A name an address of which answered on its ssh port
    reaches a Mac, so no rename explains a check that then failed, such as an `ssh HOST true` that did
    not finish in time, and none is searched for: what Bonjour lists says nothing of why (vocaro/queuekeeper#25)."""
    # An earlier search's finding says nothing of this check's failure, which is searched for afresh.
    UNFOUND.pop(host, None)
    try:
        check(host, found)
    except (Unreachable, Misconfigured) as stop:
        if isinstance(stop, Misconfigured) and not OTHER_KEY.search(stop.problem): raise
        # A process that macOS denies the local network reaches nothing there, so no rename is searched for.
        lost = local_network_lost() if isinstance(stop, Unreachable) else None
        if lost: raise NoLocalNetwork(f"{stop.problem}; {lost}", LOST_FIX) from None
        if getattr(stop, "answered", False): raise
        RENAMED.pop(host, None)
        if rediscover(host) is None:
            if not searched(host): raise
            raise type(stop)(stop.problem + searched(host), stop.fix) from None
        check(host)


def check(host, found=None):
    found = found if found is not None else connection(host, REACH_TIMEOUT)
    if "unresolved" in found:
        # In ssh's own words, so that unreachable() says what a .local name that does not resolve means.
        raise ssh_failure(host, f"ssh: Could not resolve hostname {found['hostname']}: {found['unresolved']}")
    addresses = found.get("addresses")
    if addresses is not None and not any(entry["outcome"] == "connected" for entry in addresses):
        tried = "; ".join(f"{entry['address']}: {entry['outcome']}" for entry in addresses)
        raise ssh_failure(host, f"nothing answered on port {found['port']} of {found['hostname']} "
                                f"({tried or 'it has no address'})")
    attempt = asleep()
    try:
        checked = ssh(host, "true", stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=REACH_SECONDS)
    except subprocess.TimeoutExpired:
        raise answered(ssh_failure(host, f"`ssh {host} true` did not finish in {REACH_SECONDS}s", attempt),
                       addresses) from None
    if checked.returncode == 255: raise answered(ssh_failure(host, checked.stderr, attempt), addresses)
    if checked.returncode:
        raise Misconfigured(f"{host}'s login shell could not run `true` (exit {checked.returncode}): "
                            f"{last_line(checked.stderr) or 'no output'}",
                            f"repair the login shell there, which every run uses, until `ssh {host} true` succeeds "
                            "(guides/shared-host.md, Setting up the second Mac)")


def answered(stop, addresses):
    """stop, marked as a failure after an address of the second Mac answered on its ssh port, when connection()
    measured one that did (addresses), so that reachable() searches for no rename (vocaro/queuekeeper#25). Another
    key is a Misconfigured, and is searched for, since the name may now be another machine's."""
    stop.answered = isinstance(stop, Unreachable) and addresses is not None
    return stop


# What a process that macOS's Local Network privacy denies says ssh does: it connects nowhere on this Mac's network,
# and its connections fail at once with EHOSTUNREACH, "No route to host" (vocaro-engineering#282).
LOST_FIX = ("start the process again from a live Claude session, or from Terminal, whose app holds the owner's Local "
            "Network grant: a queue worker exits for that, and the next `qk land` or `qk queue wait` starts another "
            "(guides/shared-host.md, Local Network access)")
# The executable names of a Python, as proc_pidpath gives them: Homebrew's Python.app runs .../MacOS/Python, and
# python3 and python3.14 run themselves.
PYTHON = re.compile(r"^[Pp]ython(\d+(\.\d+)*)?$")


def responsible_for(pid):
    """The PID that macOS holds responsible for pid, by whose app its privacy grants, Local Network access among
    them, are judged, and that app's executable, as (PID, path); None where macOS does not say.

    The Claude app starts each session's claude through its disclaimer helper, which makes that claude responsible for
    everything it starts, a detached process too (start_new_session). Once that claude exits, macOS holds the process
    responsible for itself, a Python, which no grant covers: its every connection on the local network then fails with
    EHOSTUNREACH, while the session's own processes still connect (vocaro-engineering#282, 2026-11-07). A launchd agent
    that runs a Python is held responsible for itself too, and is denied the same way."""
    try:
        system = ctypes.CDLL(None)
        find, path_of = system.responsibility_get_pid_responsible_for_pid, system.proc_pidpath
    except (OSError, AttributeError):
        return None
    find.argtypes, find.restype = [ctypes.c_int], ctypes.c_int
    path_of.argtypes, path_of.restype = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32], ctypes.c_int
    held = find(pid)
    if held <= 0: return None
    buffer = ctypes.create_string_buffer(4096)
    size = path_of(held, buffer, len(buffer))
    return held, buffer.raw[:size].decode(errors="replace") if size > 0 else ""


def local_network_lost(pid=None):
    """Why this process, or pid, probably has no Local Network access, for a connection to the second Mac that
    failed; None while an app other than a Python is responsible for it, or macOS does not say (responsible_for)."""
    pid = pid or os.getpid()
    found = responsible_for(pid)
    if found is None: return None
    held, path = found
    if not PYTHON.match(Path(path).name): return None
    whose = "itself" if held == pid else f"PID {held}, {path},"
    return (f"macOS holds {whose} responsible for this process (PID {pid}), a Python with no app of its own, so its "
            "Local Network privacy denies it the local network, as it does once the Claude session that started a "
            "detached process ends: other processes on this Mac may reach the second Mac all the while, which need "
            "not be away or asleep (vocaro-engineering#282)")


def moved(host):
    """The ssh options that reach host under the name rediscover found it renamed to, checking the key
    known_hosts holds for its old name; none while it keeps its name."""
    found = RENAMED.get(host)
    return ["-o", f"HostName={found['to']}", "-o", f"HostKeyAlias={found['keyAlias']}"] if found else []


def rediscover(host):
    """The new .local name of a second Mac that macOS renamed, or None, after saying what it found.

    macOS renames a Mac whose .local name another machine on the network claims, adding or raising a
    -N suffix, and the old name then resolves to nothing, or to that other machine. The second Mac
    still offers ssh over Bonjour, under its computer name, so each Mac that does is resolved to its
    current name and asked, as ssh would ask the old name, to run `true` while presenting the key
    known_hosts holds for the old name. That key is the second Mac's identity, not the name of either:
    exactly one Mac presenting it is the second Mac, and RENAMED records it for every later connection
    of this process. None found, or more than one, leaves the name as it is: a second Mac that is away."""
    settings = ssh_settings(host)
    old = (settings.get("hostname") or host).rstrip(".").lower()
    port = settings.get("port") or "22"
    if not old.endswith(".local"): return None
    alias = settings.get("hostkeyalias") or old
    here = offload_host.mac_name().lower()
    names = [name for name, offered in advertised() if offered == port and name not in {old, f"{here}.local"}]
    matches = [name for name in names[:CANDIDATES] if presents(host, name, alias)]
    UNFOUND.pop(host, None)
    if not matches: UNFOUND[host] = {"names": names[:CANDIDATES], "keyAlias": alias, "port": port}
    if len(matches) != 1:
        if len(matches) > 1:
            print(f"queuekeeper: {old} does not reach {host}, and {', '.join(matches)} all present its key, so queuekeeper chose none",
                  file=sys.stderr, flush=True)
        return None
    RENAMED[host] = {"from": old, "to": matches[0], "keyAlias": alias}
    print(f"queuekeeper: {host} was renamed: {old} no longer reaches it, and {matches[0]}, which presents the key known_hosts "
          f"holds for {alias}, does, so this run uses {matches[0]}; {renamed_fix(host)}", file=sys.stderr, flush=True)
    return matches[0]


def searched(host):
    """What the search for host found when it found no Mac presenting its key, as words to add to the
    problem; nothing when it was not searched for, or was found. A second Mac that Bonjour does not list
    is away or asleep, not renamed, and one that Bonjour lists under another name but that presents
    another key is another machine: either way no rename explains the failure."""
    found = UNFOUND.get(host)
    if found is None: return ""
    if not found["names"]:
        return (f"; no other Mac offers ssh on port {found['port']} over Bonjour (`dns-sd -B _ssh._tcp local.`), "
                f"so {host} was not renamed: it is away or asleep")
    return (f"; of the Macs offering ssh over Bonjour, {', '.join(found['names'])}, none presents the key known_hosts "
            f"holds for {found['keyAlias']}, so {host} was not renamed among them")


def renamed_fix(host):
    found = RENAMED[host]
    return (f"to keep it, set `HostName {found['to']}` under `Host {host}` in ~/.ssh/config, and `HostKeyAlias "
            f"{found['keyAlias']}` too, so a later rename needs no new known_hosts entry (guides/shared-host.md, "
            "Setting up the second Mac)")


def presents(host, name, alias):
    """Whether name, reached as host is, logs in while presenting the key known_hosts holds for alias:
    ssh refuses any other key, and adds none."""
    command = ["ssh", "-o", f"ConnectTimeout={REACH_TIMEOUT}", "-o", f"HostName={name}", "-o", f"HostKeyAlias={alias}",
               "-o", "StrictHostKeyChecking=yes", "-o", "UpdateHostKeys=no", "-o", "CheckHostIP=no", *SSH[1:], host,
               "true"]
    try:
        return subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, env=environment(),
                              timeout=REACH_SECONDS).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def advertised():
    """Each name and port that a Mac offering ssh over Bonjour resolves to, in the order browsed."""
    instances = []
    for line in watch(["dns-sd", "-B", "_ssh._tcp", "local."], BROWSE_SECONDS):
        found = BROWSED.match(line)
        if found and (found.group(2), found.group(1)) not in instances: instances.append((found.group(2), found.group(1)))
    names = []
    for instance, domain in instances:
        for line in watch(["dns-sd", "-L", instance, "_ssh._tcp", domain], RESOLVE_SECONDS, RESOLVED.search):
            found = RESOLVED.search(line)
            entry = (found.group(1).lower(), found.group(2)) if found else None
            if entry and entry not in names: names.append(entry)
    return names


def watch(command, seconds, until=None):
    """The lines command prints in seconds, or until one of them satisfies until; then it is stopped,
    since dns-sd browses and resolves until it is."""
    try:
        watching = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, text=True, errors="replace")
    except OSError:
        return []
    lines, found = queue.Queue(), []
    threading.Thread(target=lambda: [lines.put(line) for line in watching.stdout], daemon=True).start()
    deadline = time.monotonic() + seconds
    try:
        while (left := deadline - time.monotonic()) > 0:
            try: found.append(lines.get(timeout=left))
            except queue.Empty: break
            if until is not None and until(found[-1]): break
    finally:
        watching.kill()
        watching.wait()
    return found


def last_line(text):
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return lines[-1] if lines else None


def unreachable(host, detail, slept=None):
    """A second Mac that ssh could not reach, as ssh's lines, detail, say: the last names the failure.
    A .local name that did not resolve is multicast DNS, which only both Macs awake on one network
    answer. A timeout says whether the second Mac's address answered at all. slept is asleep()'s
    reading when ssh started, if it was read: a connection that this Mac slept through times out
    whatever the second Mac does, since a Mac asleep answers nothing, and that is said instead."""
    lines = [line.strip() for line in (detail or "").splitlines() if line.strip()]
    said = lines[-1] if lines else None
    unresolved = re.search(r"Could not resolve hostname (\S+?):", said or "")
    name = unresolved.group(1) if unresolved else None
    now = asleep() if slept is not None else None
    sleep = now - slept if now is not None else 0
    problem = f"{host} cannot be reached: {said or 'ssh exited 255'}{searched(host)}"
    connected = bool(TIMED_OUT.match(said or ""))
    if NO_ANSWER.match(said or ""):
        problem += f"; nothing answered ssh's TCP connection in ConnectTimeout's {CONNECT_TIMEOUT} s"
    elif connected:
        problem += ("; ssh's TCP connection was made, so the address answered, and then "
                    + (f"no SSH banner came in ConnectTimeout's {CONNECT_TIMEOUT} s" if NO_BANNER in lines
                       else "the SSH exchange timed out"))
    if sleep >= SLEEP_NOTED:
        problem += (f"; this Mac slept for {sleep:.0f} s while ssh waited, and a Mac asleep answers nothing, so the "
                    f"timeout says nothing of {host}")
        return Misconfigured(problem, f"keep this Mac awake while queuekeeper connects to {host}; `ssh {host} true` shows "
                                      f"whether {host} answers now (guides/shared-host.md, How a run reaches the second "
                                      "Mac)")
    remedy = (f"wake {host} and connect it to this Mac's network: {name} is a multicast DNS name, which resolves only "
              "while both Macs are awake on one network that passes multicast between them, as "
              f"`dscacheutil -q host -a name {name}` shows" if name and name.rstrip(".").lower().endswith(".local")
              else f"see whether {host} is overloaded or its sshd is stuck, as `ssh -v {host} true` shows" if connected
              else f"wake {host} and connect it to the network")
    return Misconfigured(problem, f"{remedy}, or correct its entry in ~/.ssh/config or its authorized_keys, until "
                                  f"`ssh {host} true` succeeds without a prompt (guides/shared-host.md, Setting up the "
                                  "second Mac)")


def no_toolkit(host, status, detail):
    return Misconfigured(f"{host} could not run the toolkit (exit {status}): {detail or 'no output'}",
                         f"give {host}'s login shell a python3 that runs it: Homebrew's, which `qk remote setup` "
                         "installs, or the system's, which Xcode's command line tools provide "
                         "(guides/shared-host.md, Setting up the second Mac)")


def configured():
    """The second Mac that `qk run` and `qk admit` send work to, or None for this Mac only.

    QUEUEKEEPER_OFFLOAD_HOST names it, and `none` turns it off (named_host). Otherwise it is the ssh alias
    that ~/.ssh/config defines (alias).
    """
    host = named_host()
    if host is not None:
        return None if host.strip().lower() in {"", "none", "off"} else host
    return alias()


def gate_hosts():
    """Ordered SSH aliases for queue gates, or None to use the ordinary single remote."""
    host = named_host()
    if host is not None and host.strip().lower() in {"", "none", "off"}: return None
    path = offload_host.admission_directory() / GATE_HOSTS_FILE
    try:
        value = path.read_text(encoding="utf-8")
        source = str(path)
    except FileNotFoundError:
        value = os.environ.get(GATE_HOSTS)
        if value is None: return None
        source = GATE_HOSTS
    except UnicodeError as error:
        raise ValueError(f"{path} must be UTF-8 text") from error
    hosts = tuple(part.strip() for part in value.split(","))
    if any(not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", alias) or alias.lower() in {"none", "off"}
           for alias in hosts) or len(hosts) != len(set(hosts)):
        raise ValueError(f"{source} needs a comma-separated list of distinct SSH aliases")
    return hosts


def named_host():
    """What QUEUEKEEPER_OFFLOAD_HOST names, or None when it is unset.

    VOCARO_BUILD_HOST, its name until vocaro-engineering#58, is refused when it is set at all, beside the
    new name too: it may say none, to keep work on this Mac, and ignoring it would let that work go to the
    second Mac. Only what reads the new name refuses it, before it chooses a Mac or reaches the second
    Mac, so a command that does neither, or names its destination with --host, runs as it did."""
    if OLD_HOST_VARIABLE in os.environ:
        raise ValueError(f"{OLD_HOST_VARIABLE} was {HOST_VARIABLE}'s name until vocaro-engineering#58, and is no "
                         f"longer read: unset it, and set {HOST_VARIABLE} instead, to the same value (none keeps work "
                         "on this Mac). queuekeeper chooses no Mac while it is set, since ignoring it could send work to the "
                         "second Mac that it was meant to keep here (guides/shared-host.md, Earlier names)")
    return os.environ.get(HOST_VARIABLE)


def alias():
    """The first alias of ALIASES that ~/.ssh/config defines, or None: `ssh -G` resolves an undefined
    alias to its own name."""
    for name in ALIASES:
        try:
            resolved = subprocess.run(["ssh", "-G", name], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return None
        names = [line.split(None, 1)[1] for line in resolved.stdout.splitlines() if line.startswith("hostname ")]
        if resolved.returncode == 0 and names and names[0] != name: return name
    return None


def environment():
    return {key: value for key, value in os.environ.items() if key not in GIT_LOCATION}


def git(root, *arguments, variables=None):
    result = subprocess.run(["git", "-C", str(root), *arguments], capture_output=True, text=True,
                            env=variables or environment(), timeout=120)
    if result.returncode:
        raise RuntimeError(f"git {' '.join(arguments)} in {root} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def snapshot(root):
    """The commit that holds the checkout's working tree, and HEAD: HEAD itself when the tree is
    HEAD's, otherwise a snapshot commit with HEAD as its parent, which no branch holds.

    `git add -A` takes the working tree into a temporary index, a copy of the checkout's own:
    tracked changes, deletions and nonignored untracked files, and no ignored file, as a commit
    would. The session's own index, HEAD, branch and working tree are untouched."""
    head = git(root, "rev-parse", "--verify", "HEAD^{commit}")
    index = Path(git(root, "rev-parse", "--path-format=absolute", "--git-path", "index"))
    with tempfile.TemporaryDirectory(prefix="eng-snapshot-") as directory:
        variables = {**environment(), "GIT_INDEX_FILE": str(Path(directory) / "index")}
        # The copy keeps the index's file stats, so git add reads only the files that changed, and its
        # mtime: git reads a file whose entry is no older than the index, in whole seconds, since one
        # rewritten at its size within that second keeps its stats. A newer copy would trust them
        # and snapshot what was staged.
        if index.is_file(): shutil.copy2(index, variables["GIT_INDEX_FILE"])
        else: git(root, "read-tree", head, variables=variables)
        git(root, "add", "-A", variables=variables)
        tree = git(root, "write-tree", variables=variables)
    if tree == git(root, "rev-parse", f"{head}^{{tree}}"): return head, head
    commit = git(root, "commit-tree", tree, "-p", head, "-m", f"Snapshot of the working tree on {head}",
                 variables={**environment(), **SNAPSHOT_IDENTITY})
    return commit, head


def toolkit_gap(toolkit):
    """What to say when the toolkit a dispatch sends lacks commits of its origin/main as last fetched, or None.

    The run there executes that toolkit, so a project change that needs newer toolkit code fails, and only as
    an error from wherever it first reaches for it: on 2026-11-06 BreezEPUB's driver stopped on "cannot import
    name 'capture_approvals'" while this Mac's shared checkout waited one landing behind for a process running
    from it to end (vocaro-engineering#209; guides/maintenance.md, The shared checkout follows each toolkit
    landing). A dispatch says it only when it sends the checkout's own tree: a clean checkout on main that is
    only behind sends origin/main instead (followed)."""
    count = git_state.behind(toolkit)
    if not count: return None
    return (f"the toolkit sent, {toolkit} at {git_state.git(toolkit, 'rev-parse', '--short=12', 'HEAD')}, is "
            f"{count} commit{'s' * (count != 1)} behind its origin/main (last landed: {git_state.last_landed(toolkit)}): "
            "a project change that needs newer toolkit code fails there; the shared checkout follows each landing "
            "once nothing runs from it")


def followed(toolkit, commit, head):
    """origin/main's commit, as last fetched, when the toolkit checkout is clean (commit, its snapshot, is
    head), on main, and only behind origin/main; otherwise None.

    A dispatch then sends origin/main rather than the checkout's HEAD: the shared checkout waits behind
    while a process runs from it, and a project change that needs a just-landed toolkit API failed on every
    run sent from this Mac until it followed (owner's decision, 2026-11-07, vocaro-engineering#227, which
    replaces #104's rule that a dispatch sends what a run here would read). A dirty checkout, one on another
    branch, one current, or one with commits of its own sends its own tree, as before."""
    if commit != head: return None
    if git_state.git(toolkit, "symbolic-ref", "--quiet", "--short", "HEAD") != "main": return None
    upstream = git_state.git(toolkit, "rev-parse", "--verify", "--quiet", f"{git_state.UPSTREAM}^{{commit}}")
    if not upstream or upstream == head: return None
    if git_state.git(toolkit, "merge-base", "--is-ancestor", head, upstream) is None: return None
    return upstream


def following_line(kit):
    """What a dispatch says when it sends origin/main's toolkit rather than the checkout's HEAD (followed)."""
    count = kit["behind"]
    return (f"sending the toolkit at origin/main's {kit['commit'][:12]}, not the shared checkout's HEAD "
            f"{kit['checkoutHead'][:12]}, which is clean, on main and {count} commit{'s' * (count != 1)} behind it "
            "(vocaro-engineering#227); a run on this Mac reads the checkout until it follows")


def kit_snapshot(kit):
    """Whether the toolkit sent is a snapshot of its checkout's working tree rather than its HEAD."""
    return bool(kit) and kit["commit"] != kit.get("head", kit["commit"])


def toolkit_identity(kit, commit):
    """A receipt's toolkit: the commit that ran, a snapshot when its checkout had changes, the HEAD it
    was made on, and the checkout's HEAD beside it, another commit only when origin/main was sent in its
    place (followed). The toolkit's own project runs as its own commit."""
    if not kit: return {"toolkit": commit}
    head = kit.get("head", kit["commit"])
    return {"toolkit": kit["commit"], "toolkitHead": head, "toolkitCheckoutHead": kit.get("checkoutHead", head),
            "toolkitSnapshot": kit_snapshot(kit)}


def moved_trees(root, offered):
    """The trees that work this Mac admitted after offering it to the second Mac would read in place of what it
    sent, as [(checkout, commit sent, what, the changes)]: a run here reads the checkout's working tree as it is
    when the run starts, and that tree is no longer the commit sent (vocaro-engineering#79). Empty for work for
    which nothing was sent, which runs the tree as it is."""
    sent = offered.sent if offered is not None else {}
    if sent.get("commit") is None: return []
    # The toolkit's tree too, which goes as a snapshot of its own when its checkout has changes. When
    # origin/main went in place of the checkout's HEAD (followed), a run here reads the checkout, as any run
    # here does, so the tree it must still hold is that HEAD's.
    kit = sent.get("kit") or {}
    kept = kit.get("checkoutHead") or kit.get("commit")
    moved = []
    for checkout, commit, what in [(root, sent["commit"], "working tree"),
                                   *([(TOOLKIT_ROOT, kept, "toolkit's working tree")] if kit else [])]:
        now, _head = snapshot(checkout)
        if git(checkout, "rev-parse", f"{now}^{{tree}}") == git(checkout, "rev-parse", f"{commit}^{{tree}}"): continue
        moved.append((checkout, commit, what, git(checkout, "diff", "--name-status", "--no-renames", commit, now)))
    return moved


def changed_since(root, offered):
    """Why work this Mac admitted after offering it to the second Mac would not run what it sent, or None
    (moved_trees). A lane then does not run here; `qk admit` runs its command from what it sent (sent_tree)."""
    moved = moved_trees(root, offered)
    return moved_reason(moved[0]) if moved else None


def moved_reason(move):
    """What changed in one of moved_trees's trees after it was sent, with the changed paths."""
    _checkout, commit, what, changes = move
    return (f"the {what} changed after {commit[:12]} was sent to the second Mac, and a run here would read "
            f"the changed tree, not what was sent:\n{changes}")


# The prefix of the temporary directory in which `qk admit` runs a command from what it sent (sent_tree).
SENT_TREE_PREFIX = "eng-sent-"


def add_worktree(repository, path, commit):
    """A detached worktree of repository at commit, made at path: (repository, path, its private Git directory)."""
    git(repository, "worktree", "add", "--detach", "--quiet", str(path), commit)
    return repository, path, Path(git(path, "rev-parse", "--absolute-git-dir"))


def remove_worktree(repository, path, private):
    """Remove a worktree add_worktree made, and its entry in the repository, whatever it holds."""
    removed = subprocess.run(["git", "-C", str(repository), "worktree", "remove", "--force", "--force", str(path)],
                             env=environment(), capture_output=True, timeout=120)
    if removed.returncode == 0 and not path.exists(): return
    shutil.rmtree(path, ignore_errors=True)
    shutil.rmtree(private, ignore_errors=True)


@contextlib.contextmanager
def sent_tree(root, sent, toolkit_moved=False):
    """The trees `qk admit` sent to the second Mac, laid out here as the second Mac lays them out, for this Mac
    to run the command from once the tree here is no longer what was sent (moved_trees; vocaro-engineering#276):
    (checkout, toolkit). The checkout is a detached worktree of the project at the commit sent, in a temporary
    directory under the main checkout's name, with the toolkit beside it, where a project's relative paths to
    the toolkit find it: the toolkit's checkout itself, by a symbolic link, while its tree is still what was
    sent, and otherwise a worktree of it at that commit too (toolkit_moved). The toolkit's own project is its own
    toolkit. Both worktrees and the directory are removed when the block ends, however it ends, and files the
    command left there that Git ignores, its build products among them, go with them."""
    base = Path(tempfile.mkdtemp(prefix=SENT_TREE_PREFIX)).resolve()
    made = []
    try:
        top = Path(git(root, "rev-parse", "--show-toplevel"))
        checkout = base / checkout_name(root)
        made.append(add_worktree(top, checkout, sent["commit"]))
        toolkit = checkout
        kit = sent.get("kit")
        if kit:
            toolkit = base / checkout_name(TOOLKIT_ROOT)
            if toolkit_moved:
                made.append(add_worktree(TOOLKIT_ROOT, toolkit, kit.get("checkoutHead") or kit["commit"]))
            else:
                toolkit.symlink_to(Path(TOOLKIT_ROOT).resolve(), target_is_directory=True)
        yield checkout, toolkit
    finally:
        for repository, path, private in reversed(made): remove_worktree(repository, path, private)
        shutil.rmtree(base, ignore_errors=True)


@contextlib.contextmanager
def sent_toolkit(sent):
    """A detached worktree of the toolkit at the commit a lane taken here after sending it would read (moved_trees),
    in a new temporary directory under the toolkit's name, for a lane to run with once the toolkit's tree here is no
    longer what was sent (vocaro/queuekeeper#33). The worktree and the directory are removed when the block ends,
    however it ends."""
    kit = sent["kit"]
    base = Path(tempfile.mkdtemp(prefix=SENT_TREE_PREFIX)).resolve()
    made = None
    try:
        made = add_worktree(TOOLKIT_ROOT, base / checkout_name(TOOLKIT_ROOT), kit.get("checkoutHead") or kit["commit"])
        yield made[1]
    finally:
        if made is not None: remove_worktree(*made)
        shutil.rmtree(base, ignore_errors=True)


def common_directory(root):
    return Path(git(root, "rev-parse", "--path-format=absolute", "--git-common-dir"))


def checkout_name(root):
    """The main checkout's directory name, from a linked worktree too."""
    common = common_directory(root)
    return common.parent.name if common.name == ".git" else Path(root).resolve().name


def origin(root):
    """The toolkit's origin for policy refresh, with GitHub SSH URLs normalized to HTTPS.

    The second Mac writes it into the repository that every checkout of the toolkit there shares,
    so a path, such as a clone's origin naming the checkout it was cloned from, is refused: there it
    names another checkout, and every session's policy refresh fails until a dispatch sends a URL
    again (vocaro-engineering#78). The fix names that checkout's own origin, when it has a URL, or
    UPSTREAM. The refusal is a RuntimeError, which send makes Unavailable."""
    try: url = git(root, "config", "--get", "remote.origin.url")
    except RuntimeError: return None
    if offload_host.local_url(url):
        source = Path(url[len("file://"):] if url.lower().startswith("file://") else url)
        if not source.is_absolute(): source = Path(root) / source
        try: upstream = git(source, "config", "--get", "remote.origin.url")
        except (RuntimeError, OSError): upstream = None
        if not upstream or offload_host.local_url(upstream): upstream = UPSTREAM
        raise RuntimeError(f"{root}'s origin is {url}, a path on this Mac, from which the second Mac cannot fetch the "
                           f"policy; set it with `git -C {shlex.quote(str(root))} remote set-url origin {upstream}` "
                           "and send the work again (the toolkit's guides/adoption.md, Locating the toolkit from a worktree)")
    return "https://github.com/" + url[len("git@github.com:"):] if url.startswith("git@github.com:") else url


def developer_directory():
    selected = os.environ.get("DEVELOPER_DIR") or offload_host.output(["xcode-select", "-p"])
    return selected if selected and Path(selected).is_dir() else None


def host_command(source, request):
    """Compress the self-contained host program before the login shell's second quoting layer.
    The request remains the next argument, and standard input remains the dispatch control channel."""
    packed = base64.b64encode(zlib.compress(source.encode("utf-8"))).decode("ascii")
    program = f"exec(__import__('zlib').decompress(__import__('base64').b64decode({packed!r})).decode('utf-8'))"
    return shlex.join(["python3", "-c", program, request])


def login(script):
    """Have the second Mac's login shell run script, so its PATH is Terminal's, Homebrew's included."""
    return f'exec "$SHELL" -l -c {shlex.quote(script)}'


def ssh(host, script, **options):
    """Run script in host's login shell. When ssh's captured words say the name did not resolve or answered
    with another key, ssh logged in nowhere and nothing ran, so a second Mac that macOS renamed is looked
    for (rediscover) and, once found, script runs there: every connection finds it, not only those after
    reachable(), such as a foreground lane's and `qk remote foreground`'s (vocaro-engineering#129)."""
    run = lambda: subprocess.run([*SSH, *moved(host), host, login(script)], env=environment(), **options)
    done = run()
    if done.returncode == 255 and host not in RENAMED and renamed_failure(done.stderr) and rediscover(host):
        done = run()
    return done


def renamed_failure(said):
    """Whether ssh's own words, said, are those of a name that a rename may explain: one that did not
    resolve, or that answered with another key than the one known_hosts holds for it."""
    line = last_line(said) if isinstance(said, str) else None
    return bool(line and (re.search(r"Could not resolve hostname", line) or OTHER_KEY.search(line)))


def send(root, host, repository, commit, advertised=None, sends=None):
    """Push one commit by name to a bare repository on the second Mac, without the push hooks:
    this is no publication, and a pre-push hook would run the gates this offloads. Return the push's
    record, which sends, a list, also gets before the push starts, so that a push that fails or is
    stopped leaves its record: its exit status and seconds, what its lines say it cost (pushed), its
    lines (said) beyond the progress and the ref's report, which it prints, and Git's push.negotiate
    setting, unset by default.

    advertised is what the repository there said its checkouts hold before the push
    (offload_host.advertise), or None when it said nothing. Git sends only what the second Mac lacks
    of the commits it advertises that this Mac has too, so the push is warm when this Mac has one
    of them and cold, sending the commit's whole history, when it has none.

    Sessions that send one commit at once race for its ref, and the push that loses fails with
    "reference already exists" (RACED). The commit is then there all the same, so that push has sent
    it, once the second Mac names it by the pushed ref or by a checkout's (holds). The winner's
    dispatch may already have placed it, deleting the pushed ref, and not yet named it as a
    checkout's; the push is then made again, up to RACE_PUSHES in all, and the record's attempts says
    how many it took and raced what the earlier ones said (vocaro-engineering#152). A ref that names
    another commit, or a failure that is no race, stops the send.
    A push still running after SEND_TIMEOUT seconds, or when a stop is requested, ends with
    everything it started, and nothing of it enters the second Mac's repository."""
    variables = {**environment(), "GIT_SSH_COMMAND": shlex.join([*SSH, *moved(host)])}
    known = advertised is not None and "commits" in advertised
    have = held(root, advertised["commits"]) if known and advertised["commits"] else []
    record = {"repository": repository, "commit": commit, "pushNegotiate": setting(root, "push.negotiate"),
              "advertised": len(advertised["commits"]) if known else None, "held": len(have),
              "transfer": ("warm" if have else "cold") if known else "unknown"}
    if sends is not None: sends.append(record)
    began, attempt = time.monotonic(), 0
    while True:
        attempt += 1
        if attempt > 1:
            record.setdefault("raced", []).append(record["said"])
            record["attempts"] = attempt
            time.sleep(RACE_PAUSE * (attempt - 1))
        if not push_once(root, host, repository, commit, variables, record, began): return record
        raced = any(RACED.search(line) for line in record["said"])
        found = holds(root, host, repository, commit, variables)
        if found["commit"]:
            record["race"] = "another session's push of the commit took its ref first"
            print(f"remote: {host} holds {commit[:12]} all the same: {record['race']}", file=sys.stderr, flush=True)
            return record
        if found["other"]:
            raise Misconfigured(f"{host} did not receive {commit[:12]} into ~/{repository}: its ref {REF}{commit} "
                                f"names {found['other'][:12]} instead",
                                f"delete that ref with `git --git-dir ~/{repository} update-ref -d {REF}{commit}` on "
                                f"{host} and send the work again (guides/shared-host.md, How a run reaches the second Mac)")
        if not raced or attempt >= RACE_PUSHES: break
        print(f"remote: {commit[:12]} lost the race for its ref on {host}, which names it nowhere yet "
              f"({found['seen']}); pushing it again", file=sys.stderr, flush=True)
    if raced:
        raise Misconfigured(f"{host} did not receive {commit[:12]} into ~/{repository}: each of its {attempt} pushes lost "
                            f"the race for the commit's ref to another session's push of it, and then {host} named "
                            f"it neither by that ref nor by a checkout's ({found['seen']})",
                            "send the work again, or start dispatches of one commit a few seconds apart; nothing on "
                            f"{host} needs repair (guides/shared-host.md, How a run reaches the second Mac)")
    raise Misconfigured(f"{host} did not receive {commit[:12]} into ~/{repository} (git push exited "
                        f"{record['status']}): {(record['said'] or ['no output'])[-1]}",
                        f"repair ~/{repository} on {host}, or free space there, until the push succeeds")


def push_once(root, host, repository, commit, variables, record, began):
    """Make one of send's pushes, updating its record, and return its exit status; a push stopped or
    timed out, SEND_TIMEOUT seconds after began, raises, as send says."""
    lines, started, stop = [], time.monotonic(), None
    pushing = subprocess.Popen(["git", "-C", str(root), "push", "--no-verify", "--progress",
                                "--receive-pack=git receive-pack", f"{host}:{repository}", f"{commit}:{REF}{commit}"],
                               env=variables, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               start_new_session=True)
    reader = threading.Thread(target=collect, args=(pushing.stdout, lines, started), daemon=True)
    reader.start()
    try:
        while stop is None:
            try:
                pushing.wait(timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                pass
            if processes.requested() is not None: stop = "stopped"
            elif time.monotonic() - began >= SEND_TIMEOUT: stop = "timedOut"
    finally:
        # In a session of its own, a terminal's interrupt reaches only queuekeeper, which ends the push here.
        if pushing.poll() is None: end(pushing)
    # What the push started could hold its output open; its record then ends with what came so far.
    reader.join(SEND_STOP_WAIT)
    if not reader.is_alive(): pushing.stdout.close()
    record.update(pushed(list(lines)), status=pushing.returncode, seconds=round(time.monotonic() - began, 3))
    if stop: record[stop] = True
    # A push that succeeds says nothing beyond its progress unless something is worth reading, such as
    # push.negotiate's failure.
    for line in record["said"]:
        if line != "Everything up-to-date": print(f"remote: git push said: {line}", file=sys.stderr, flush=True)
    if stop == "stopped":
        raise processes.Interrupted(processes.requested(), f"while sending {commit[:12]} to {host}, before the second "
                                                           "Mac started the run")
    if stop == "timedOut":
        raise Misconfigured(f"the push of {commit[:12]} into ~/{repository} on {host} did not finish in {SEND_TIMEOUT}s "
                            f"and was stopped, so nothing ran there: {sending(record)}",
                            f"see whether the link between the Macs is slow, as Wi-Fi can be, or what keeps {host} from "
                            "taking the pack, as its disk or load; a push that completes leaves what it sent there, "
                            "so the next sends only what is new (guides/shared-host.md, How a run reaches the second Mac)")
    return pushing.returncode


def holds(root, host, repository, commit, variables):
    """What the second Mac's repository names commit by, after a push of it failed: commit, whether its
    pushed ref or a checkout's (offload_host.CACHE) names it; other, the commit its pushed ref names
    instead, if any; and seen, what `git ls-remote` showed, as queuekeeper says it."""
    refs = [f"{REF}{commit}", f"{offload_host.CACHE}{commit}"]
    try:
        listed = subprocess.run(["git", "-C", str(root), "ls-remote", "--upload-pack=git upload-pack",
                                 f"{host}:{repository}", *refs], env=variables, capture_output=True,
                                text=True, timeout=REACH_SECONDS)
    except subprocess.TimeoutExpired:
        return {"commit": False, "other": None, "seen": f"`git ls-remote` did not finish in {REACH_SECONDS}s"}
    if listed.returncode:
        said = last_line(listed.stderr) or "no output"
        return {"commit": False, "other": None, "seen": f"`git ls-remote` exited {listed.returncode}: {said}"}
    named = {words[1]: words[0] for words in map(str.split, listed.stdout.splitlines()) if len(words) >= 2}
    pushed_ref = named.get(refs[0])
    return {"commit": commit in named.values(), "other": pushed_ref if pushed_ref not in (None, commit) else None,
            "seen": "; ".join(f"{ref} at {oid[:12]}" for ref, oid in sorted(named.items()))
                    or "`git ls-remote` listed neither ref"}


def collect(stream, lines, started):
    """Append each line a push writes to lines, as (seconds since started, text), when it arrives:
    Git ends a progress line with a carriage return and redraws it."""
    pending = b""
    while True:
        try: chunk = os.read(stream.fileno(), 65536)
        except (OSError, ValueError): chunk = b""
        if not chunk: break
        pending += chunk
        *complete, pending = re.split(rb"[\r\n]", pending)
        moment = round(time.monotonic() - started, 3)
        lines.extend((moment, line.decode(errors="replace").rstrip()) for line in complete if line.strip())
    if pending.strip(): lines.append((round(time.monotonic() - started, 3), pending.decode(errors="replace").rstrip()))


def end(process):
    """End a process that runs in a session of its own and everything it started: SIGTERM to its
    group, then SIGKILL to what is left of it after SEND_STOP_WAIT seconds."""
    with contextlib.suppress(ProcessLookupError, PermissionError): os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=SEND_STOP_WAIT)
    except subprocess.TimeoutExpired:
        pass
    # The group outlives its leader while ssh or pack-objects runs on.
    with contextlib.suppress(ProcessLookupError, PermissionError): os.killpg(process.pid, signal.SIGKILL)
    process.wait()


def pushed(lines):
    """What a push's lines, each (seconds since it started, text), say it cost, as far as they go.

    phases: connect, from the push's start to the first line of the pack Git makes, which follows the
    second Mac's ref advertisement and Git's search for what to send; pack, until Git began to write
    the pack; transfer, until it wrote the pack's last byte, which the second Mac indexes as it
    arrives; and receiver, until the second Mac reported the ref, which takes the rest of its indexing,
    its check that every object is there and the ref's update. A phase whose end the lines do not
    show is left out. objects and bytes are as Git reports them, the bytes to Git's rounding, and
    written as Git writes them; negotiationFailed says that push.negotiate's exchange failed and the
    push went on without it; said holds every line but the progress and the ref's report."""
    marks = [("connect", next((moment for moment, text in lines if PROGRESS.match(text)), None)),
             ("pack", next((moment for moment, text in lines if text.startswith("Writing objects")), None)),
             ("transfer", next((moment for moment, text in lines if WRITTEN.match(text)), None)),
             ("receiver", next((moment for moment, text in lines if text.startswith("To ")), None))]
    phases, previous = {}, 0.0
    for name, moment in marks:
        if moment is None or previous is None:
            previous = None
            continue
        phases[name], previous = round(moment - previous, 3), moment
    found = {"phases": phases, "negotiationFailed": any(NEGOTIATION_FAILED in text for _moment, text in lines),
             "said": [text for _moment, text in lines if not PROGRESS.match(text) and not text.startswith(("To ", " "))]}
    for _moment, text in lines:
        written, total = WRITTEN.match(text), TOTAL.match(text)
        if written:
            found.update(bytes=round(float(written.group(1)) * UNITS[written.group(2)]),
                         written=f"{written.group(1)} {written.group(2)}")
        if total: found.update(objects=int(total.group(1)), reused=int(total.group(2)))
    return found


def sending(record):
    """A push's record, as queuekeeper says it."""
    phases = ", ".join(f"{name} {seconds:.2f} s" for name, seconds in record.get("phases", {}).items())
    if record.get("objects") == 0 or "Everything up-to-date" in record.get("said", []):
        sent = "it sent nothing, the commit being there"
    else:
        sent = f"it sent {record['objects']} objects in {record.get('written', '0 bytes')}" if "objects" in record \
            else "Git reported no count of what it sent"
    advertised, have = record["advertised"], record["held"]
    commits = f"{advertised} commit{'' if advertised == 1 else 's'}"
    cache = {"warm": f"warm: the second Mac's checkouts hold {commits}, "
                     + ("which this Mac has" if have == advertised == 1 else f"{have} of them here too"),
             "cold": f"cold: the second Mac's checkouts hold {commits}, none of them here" if advertised
             else "cold: the second Mac's checkouts hold no commit",
             "unknown": "warm or cold unknown: the second Mac said nothing of what its checkouts hold"}[record["transfer"]]
    return f"{record['seconds']:.2f} s, {cache}; {sent}" + (f" ({phases})" if phases else "")


def preparing(preparation, phases, receipt):
    """What came before the work's queue, and then the queue's wait, as queuekeeper says it: preparation is
    execute's, phases the second Mac's, and receipt the work's own there, which gives queueSeconds."""
    here = preparation.get("seconds", {})
    parts = [f"{label} {here[key]:.2f} s" for key, label in
             (("staging", "staging"), ("connection", "the preparing connection"), ("send", "the pushes")) if key in here]
    there = [f"{label} {phases[key]:.2f} s" for key, label in
             (("claim", "the wait for a checkout"), ("place", "placing the commits"), ("provision", "the provision lane"))
             if key in phases]
    queued = (receipt or {}).get("queueSeconds")
    return ((f"sent at the first offer, after {', '.join(parts)}; " if preparation.get("offeredAgain")
             else f"before the queue: here, {', '.join(parts)}; ")
            + f"there, {', '.join(there) or 'nothing timed'}; "
            + (f"then the queue's wait, {queued:.2f} s" if queued is not None else "no wait in the queue recorded"))


def final_object(found):
    """The JSON object that a finished command's output ends with, or an empty one."""
    try: answered = json.loads(last_line(found.stdout))
    except (TypeError, ValueError): return {}
    return answered if isinstance(answered, dict) else {}


def setting(root, key):
    """A Git setting as the push in root reads it, or None when it is unset."""
    found = subprocess.run(["git", "-C", str(root), "config", "--get", key], capture_output=True, text=True,
                           env=environment(), timeout=60)
    return found.stdout.strip() if found.returncode == 0 else None


def held(root, commits):
    """Those of commits that root's repository holds as commits."""
    found = subprocess.run(["git", "-C", str(root), "cat-file", "--batch-check=%(objectname) %(objecttype)"],
                           input="".join(f"{commit}\n" for commit in commits), capture_output=True, text=True,
                           env=environment(), timeout=60)
    return [line.split()[0] for line in found.stdout.splitlines() if line.endswith(" commit")]


def store(root):
    return common_directory(root) / "queuekeeper/remote"


@contextlib.contextmanager
def results_lock(base, name):
    """An flock under which one dispatch of a name at a time files its results in base."""
    base.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(base / f".{name}.lock", os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def file_results(base, name, dispatch, staging=None, receipt=None, latest=True):
    """Keep a dispatch's results in a directory of their own under base/DISPATCHES/NAME/, named for
    the dispatch, and return it: staging's, moved there whole, or, for a dispatch that ran nothing,
    one that holds its receipt alone. With latest, base/NAME becomes a copy of them too, its files
    linked to the dispatch's own, in place of the previous dispatch's at once. Each name keeps its
    newest DISPATCHES_KEPT dispatches, naming each one removed.

    Dispatches of one name file their results in turn. Two that each removed base/NAME and renamed
    their own into its place could interleave, and the second rename then failed with "Directory not
    empty" (Errno 66) and left that dispatch's results unfiled (vocaro-engineering#67)."""
    with results_lock(base, name):
        runs = base / DISPATCHES / name
        runs.mkdir(parents=True, exist_ok=True)
        own, count = runs / dispatch, 1
        while own.exists():
            count += 1
            own = runs / f"{dispatch}.{count}"
        if staging is not None: staging.rename(own)
        else: own.mkdir()
        if receipt is not None: (own / "source.json").write_text(json.dumps(receipt, indent=2) + "\n")
        if latest:
            fresh, previous = (base / f".{name}.{os.getpid()}.{part}" for part in ("latest", "previous"))
            for path in (fresh, previous): shutil.rmtree(path, ignore_errors=True)
            shutil.copytree(own, fresh, copy_function=os.link)
            with contextlib.suppress(FileNotFoundError): (base / name).rename(previous)
            fresh.rename(base / name)
            shutil.rmtree(previous, ignore_errors=True)
        for old in sorted(runs.iterdir())[:-DISPATCHES_KEPT]:
            shutil.rmtree(old, ignore_errors=True)
            print(f"remote: removed {old}, each name keeping its newest {DISPATCHES_KEPT} dispatches", file=sys.stderr,
                  flush=True)
    return own


def passed(root, lane, revision="HEAD"):
    """The receipt of a passing run of the whole lane at exactly revision's commit, kept by commit in the
    repository's Git directory, or None: a run on the second Mac, whose ranOn is `second Mac`, or
    `build host` in a receipt written before vocaro-engineering#58, or a run on this Mac in any worktree
    of the repository, whose ranOn is `this Mac` (keep_here, vocaro-engineering#198). The receipt is the
    last such run's, on either Mac, so a later failure at the commit replaces a pass.

    A snapshot's run never counts, for HEAD or for the snapshot itself: no branch holds it. Nor
    does a run on a snapshot of the toolkit's working tree, nor a group's serial reference."""
    commit = git(root, "rev-parse", "--verify", f"{revision}^{{commit}}")
    try:
        receipt = json.loads((store(root) / commit / f"{lane}.json").read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(receipt, dict): return None
    result = receipt.get("result", {})
    whole = receipt.get("commit") == commit and receipt.get("lane") == lane and not receipt.get("arguments") \
        and not receipt.get("snapshot") and not receipt.get("toolkitSnapshot") and not receipt.get("serial")
    # A run on this Mac counts by its judged verdict, which a zero exit that ran too few tests is not.
    judged = receipt.get("ranOn") != HERE or result.get("outcome") == "passed"
    return receipt if whole and judged and result.get("exitStatus") == 0 and not result.get("stopped") else None


# A receipt's ranOn for a run on this Mac, as cli.placement writes it.
HERE = "this Mac"
# The working-changes digest config.source_identity gives a checkout with none.
CLEAN = hashlib.sha256().hexdigest()


# The most of a run's results/ kept by commit beside its receipt; a run that wrote more keeps them only in its
# dispatch's or checkout's results, and says so. A landing gate's iOS shards keep about 1 MB (vocaro-engineering#218).
KEPT_RESULTS_LIMIT = 64 * 1024 * 1024


def kept_results(kept, lane):
    """Where the store keeps, beside the lane's receipt at a commit, what its run wrote in results/."""
    return kept / f"{lane}.results"


# The rule by which qk disk reclaims kept results (vocaro-engineering#218): a lane's results stay at its newest
# KEPT_RESULTS_PER_LANE commits, and at every commit whose run is under KEPT_RESULTS_DAYS old. Receipts are never
# pruned: they are small, and eng release, checkpoints and `qk remote passed` read them.
KEPT_RESULTS_PER_LANE = 10
KEPT_RESULTS_DAYS = 14
KEPT_RESULTS_RULE = (f"keeps each lane's results at its newest {KEPT_RESULTS_PER_LANE} commits and at every commit "
                     f"kept in the last {KEPT_RESULTS_DAYS} days, and never removes a receipt")


def kept_results_at(folder):
    """When a commit's LANE.results/ was kept: its receipt's time, which keep replaces with it, else its own."""
    receipt = folder.parent / (folder.name[:-len(".results")] + ".json")
    try: return receipt.stat().st_mtime
    except OSError: return folder.stat().st_mtime


def all_kept_results(base):
    """Every LANE.results/ in a store, as {LANE: [folder, newest first]}."""
    found = {}
    if not Path(base).is_dir(): return found
    for commit in Path(base).iterdir():
        if commit.name.startswith(".") or not commit.is_dir() or commit.is_symlink(): continue
        for folder in commit.glob("*.results"):
            if folder.is_dir() and not folder.is_symlink():
                found.setdefault(folder.name[:-len(".results")], []).append(folder)
    for lane, folders in found.items():
        folders.sort(key=lambda folder: (kept_results_at(folder), folder.parent.name), reverse=True)
    return found


def results_past_rule(base, now=None):
    """The kept results of a store (store(root)) that KEPT_RESULTS_RULE removes."""
    now = time.time() if now is None else now
    return [folder for folders in all_kept_results(base).values() for index, folder in enumerate(folders)
            if index >= KEPT_RESULTS_PER_LANE and now - kept_results_at(folder) >= KEPT_RESULTS_DAYS * 86400]


def prune_results(base, now=None):
    """Remove the kept results past KEPT_RESULTS_RULE, leaving every receipt, and return their paths. Each is
    first renamed aside, so a run keeping that lane's results at that commit meanwhile keeps its own."""
    pruned = []
    for folder in results_past_rule(base, now):
        aside = folder.parent / f".{folder.name}.pruned.{os.getpid()}"
        try: folder.rename(aside)
        except OSError: continue
        shutil.rmtree(aside, ignore_errors=True)
        pruned.append(folder)
    return pruned


def prune_results_main(checkout, dry_run=False):
    """qk remote prune-results [CHECKOUT]: reclaim the repository's kept results by KEPT_RESULTS_RULE."""
    base = store(Path(checkout).resolve())
    if dry_run:
        gone = results_past_rule(base)
        for folder in gone: print(f"would prune {folder}")
    else:
        gone = prune_results(base)
        for folder in gone: print(f"pruned {folder}")
    print(f"{len(gone)} kept results {'past' if dry_run else 'pruned by'} the rule, which {KEPT_RESULTS_RULE}, in {base}")
    return 0


def keep(kept, lane, receipt, results=None):
    """Keep receipt as the lane's at the commit whose directory in the store is kept: whole or not at all,
    for a reader beside a run of the lane at this commit that ends at once. With results, the run's
    results/ folder, what it holds is kept beside it, in LANE.results/ (kept_results), in place of an
    earlier run's, and the receipt's keptResults names it: per-test lines and times that the second Mac's
    checkout slot or the next run here would overwrite (vocaro-engineering#218). A run that wrote nothing
    there, or more than KEPT_RESULTS_LIMIT, keeps none, and an earlier run's go."""
    kept.mkdir(parents=True, exist_ok=True)
    folder, pid = kept_results(kept, lane), os.getpid()
    fresh, previous = kept / f".{lane}.results.{pid}", kept / f".{lane}.results.{pid}.previous"
    for path in (fresh, previous): shutil.rmtree(path, ignore_errors=True)
    files = [path for path in Path(results).rglob("*") if path.is_file() and not path.is_symlink()] \
        if results is not None and Path(results).is_dir() else []
    size = sum(path.stat().st_size for path in files)
    if files and size <= KEPT_RESULTS_LIMIT:
        try:
            for path in files:
                (fresh / path.relative_to(results)).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, fresh / path.relative_to(results))
            receipt = {**receipt, "keptResults": folder.name}
        except OSError as error:
            shutil.rmtree(fresh, ignore_errors=True)
            print(f"remote: {lane}'s results were not kept by commit in {folder}: {error}", file=sys.stderr, flush=True)
    elif files:
        print(f"remote: {lane}'s results, {size} bytes, are more than the {KEPT_RESULTS_LIMIT} kept by commit, so "
              f"they stay only in {results}", file=sys.stderr, flush=True)
    partial = kept / f".{lane}.json.{pid}"
    partial.write_text(json.dumps(receipt, indent=2) + "\n")
    with contextlib.suppress(FileNotFoundError): folder.rename(previous)
    if fresh.is_dir(): fresh.rename(folder)
    partial.replace(kept / f"{lane}.json")
    shutil.rmtree(previous, ignore_errors=True)


def keep_here(root, lane, receipt, status, whole=True, results=None):
    """Keep a judged run of the whole lane on this Mac by commit in the repository's Git directory, beside
    the second Mac's, and return its path, or None when it is no commit's run. Every worktree reads it
    there, as eng release, a checkpoint and `qk remote passed` do: the merge queue's gate on this Mac ran
    in the queue's own checkout, whose receipt no session's checkout reads and the next round replaces
    (vocaro-engineering#198).

    It is kept only as a run on the second Mac is: the whole lane, with no arguments, --expect or --serial
    (whole), at the checkout's commit with no working changes, by a toolkit with none, and run for this
    Mac, not for a Mac that sent it (DISPATCHER), which keeps the receipt itself. status is the lane's
    judged status, 0 only for a pass. A run with no verdict, interrupted or past its timeout, is not
    kept, and leaves what the store holds. results is the run's results/, kept beside the receipt (keep)."""
    if not whole or receipt.get("arguments") or receipt.get("ranOn") != HERE or os.environ.get(DISPATCHER):
        return None
    project, toolkit = receipt.get("project") or {}, receipt.get("toolkit") or {}
    commit = project.get("revision")
    if not isinstance(commit, str) or not commit or project.get("workingChangesSHA256") != CLEAN \
            or toolkit.get("workingChangesSHA256") != CLEAN:
        return None
    kept = {"ranOn": HERE, "mac": receipt.get("mac"), "checkout": str(Path(root).resolve()), "commit": commit,
            "head": commit, "snapshot": False, "toolkit": toolkit.get("revision"),
            "toolkitHead": toolkit.get("revision"), "toolkitCheckoutHead": toolkit.get("revision"),
            "toolkitSnapshot": False, "lane": lane, "arguments": [],
            "result": {"exitStatus": status, "outcome": receipt.get("outcome"), "stopped": False,
                       "receipt": receipt}}
    try:
        directory = store(root) / commit
        keep(directory, lane, kept, results)
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"remote: {lane}'s result at {commit[:12]} was not kept by commit, so other worktrees cannot read "
              f"it: {error}", file=sys.stderr, flush=True)
        return None
    return directory / f"{lane}.json"


def ran_where(receipt):
    """Where a kept receipt's run ran, as eng release and `qk remote passed` say it."""
    if receipt.get("ranOn") == HERE:
        return f"on this Mac ({receipt.get('mac') or 'unnamed'}), in {receipt.get('checkout') or 'a worktree'}"
    return f"on the second Mac, {describe(receipt['result']['host'])}"


def uncounted(root, lane):
    """Why a run of the lane from root now would be no commit's pass, naming the working tree's changes that
    make it so, or None when the tree has none (vocaro-engineering#197). A run on such a tree runs a snapshot
    on the second Mac, or the changes here, and neither eng release, a checkpoint nor `qk remote passed`
    counts it."""
    try:
        changed = git(root, "diff", "--name-only", "HEAD").splitlines()
        changed += git(root, "ls-files", "--others", "--exclude-standard").splitlines()
    except (RuntimeError, subprocess.SubprocessError):
        return None
    changed = sorted({path for path in changed if path})
    if not changed: return None
    shown = ", ".join(changed[:5]) + (f" and {len(changed) - 5} more" if len(changed) > 5 else "")
    return (f"{lane} runs the working tree's uncommitted changes ({shown}), so its result is no commit's pass "
            "for eng release or a checkpoint: commit them, or keep a file no lane reads, such as release notes, "
            "out of the checkout until the lane has run, then run it again for a pass that counts "
            "(the toolkit's guides/releasing.md, Preparing a release)")


def write(stream, data):
    try: stream.buffer.write(data)
    except AttributeError: stream.write(data.decode(errors="replace"))
    stream.flush()


def describe(identity):
    xcode = (identity.get("xcode") or "no Xcode").splitlines()
    return (f"{identity.get('name')} ({identity.get('model')}, macOS {identity.get('macOS')} "
            f"({identity.get('build')}), {', '.join(xcode)})")


# What a lane's command writes under its results' results/ on the second Mac, such as speech transcripts, comes
# back whole into the results' results/ here, however the run ended. The command finds that directory in
# parallel.RESULTS_VARIABLE; a group's lanes each find results/LANE/ of the group's, which comes back with it.
RESULTS = "results/"
# What `qk admit`'s command changed on the second Mac in the checkout's files that Git does not ignore, as a binary
# patch against the commit sent, which comes back beside the dispatch's results (offload_host.gather_changes).
CHANGES = "changes.patch"
# `qk admit`'s status when its command ran on the second Mac and the changes it made to the checkout could not be
# applied here: the tree here changed after it was sent, the patch does not apply, or it did not come back. The
# patch is kept under UNAPPLIED_DIRECTORY. sysexits' EX_CANTCREAT, which no other queuekeeper status uses.
UNAPPLIED = 73
UNAPPLIED_DIRECTORY = ".build/queuekeeper/remote/unapplied"


def brought_back(root, sent, status, products=True):
    """`qk admit`'s or `qk remote admit`'s status once what its command changed on the second Mac is applied to the
    checkout here: its own status there, or UNAPPLIED, after saying why and where the patch is kept, when the changes
    cannot be applied.

    The patch applies only while the tree here is still the one sent (changed_since): a session that edited the
    checkout meanwhile keeps its edits, and the patch waits for it. git apply applies all of it or none. Files Git
    ignores, such as the command's build products, stay on the second Mac, which is said in one line when products:
    `qk admit` chose that Mac for the command, while `qk remote admit` was sent there on purpose."""
    ran = sent.get("ran")
    if not ran: return status
    if products:
        print(f"queuekeeper: the command ran on the second Mac, so its build products and other files Git ignores "
              f"stayed in {ran.get('checkout') or 'its checkout there'}; `qk admit --here` runs it on this Mac and "
              "keeps them here", file=sys.stderr, flush=True)
    changes = ran.get("changes")
    # The command did not run there, as when the second Mac declined it, or its provision lane failed.
    if changes is None: return status
    if changes.get("error"):
        print(f"queuekeeper: what the command changed in the checkout could not be gathered on the second Mac, so it stays "
              f"in {ran.get('checkout')} there: {changes['error']}", file=sys.stderr, flush=True)
        return UNAPPLIED
    files = changes.get("files") or []
    if not files: return status
    patch = Path(ran["results"]) / CHANGES
    named = ", ".join(files[:5]) + (f" and {len(files) - 5} more" if len(files) > 5 else "")
    if not ran.get("returned") or not patch.is_file():
        print(f"queuekeeper: the command changed {named} on the second Mac, and the patch did not come back; the second "
              f"Mac's copy is {ran.get('checkout')}/.build/queuekeeper/admit/{CHANGES}", file=sys.stderr, flush=True)
        return UNAPPLIED
    return apply_changes(root, sent["commit"], patch, named, "on the second Mac", Path(ran["results"]).name, status)


def apply_changes(root, sent_commit, patch, named, where, name, status):
    """status once the patch of what a command changed in a checkout of sent_commit, where says which, is applied
    to the checkout here, or UNAPPLIED once it is kept as UNAPPLIED_DIRECTORY/NAME.patch, after saying why: the tree
    here is no longer sent_commit's, or git apply refused it (brought_back)."""
    top = git(root, "rev-parse", "--show-toplevel")
    # The project's tree alone, as changed_since reads it: the toolkit's is not what the patch applies to.
    now, _head = snapshot(root)
    why = None
    if git(root, "rev-parse", f"{now}^{{tree}}") != git(root, "rev-parse", f"{sent_commit}^{{tree}}"):
        moved = git(root, "diff", "--name-status", "--no-renames", sent_commit, now)
        why = f"the working tree here changed after {sent_commit[:12]} was sent:\n{moved}"
    if why is None:
        done = subprocess.run(["git", "apply", "--binary", "--whitespace=nowarn", str(patch)], cwd=top,
                              env=environment(), capture_output=True, text=True)
        if done.returncode == 0:
            print(f"queuekeeper: applied here what the command changed {where}: {named}", file=sys.stderr, flush=True)
            return status
        why = f"git apply refused it: {last_line(done.stderr) or f'exit {done.returncode}'}"
    kept = Path(top) / UNAPPLIED_DIRECTORY / f"{name}.patch"
    kept.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(patch, kept)
    print(f"queuekeeper: what the command changed {where} ({named}) was not applied here: {why}\n"
          f"queuekeeper: the patch is {kept}; apply it with `git apply --binary {kept}` once the tree allows, or run the "
          f"command again (exit {UNAPPLIED}; guides/shared-host.md, Which Mac runs a lane)", file=sys.stderr, flush=True)
    return UNAPPLIED


def brought_back_here(root, sent_commit, checkout, status):
    """`qk admit`'s status once what its command changed in the worktree of what it sent (sent_tree) is applied to
    the checkout here, as brought_back applies what it changed on the second Mac, or UNAPPLIED. The patch is
    gathered as the second Mac gathers it (offload_host.gather_changes)."""
    name = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + f"-{os.getpid()}-here"
    with tempfile.TemporaryDirectory(prefix="eng-changes-") as directory:
        patch = Path(directory) / CHANGES
        try:
            files = offload_host.gather_changes(checkout, patch)
        except (OSError, RuntimeError) as error:
            print(f"queuekeeper: what the command changed in the worktree of what was sent could not be gathered, and goes "
                  f"with the worktree: {error}", file=sys.stderr, flush=True)
            return UNAPPLIED
        if not files: return status
        named = ", ".join(files[:5]) + (f" and {len(files) - 5} more" if len(files) > 5 else "")
        return apply_changes(root, sent_commit, patch, named, "in the worktree of what was sent", name, status)


def returned_path(path, returns):
    """Whether a file the second Mac sends back is one the plan asked for: one of returns by its exact path, or a
    file under one of them that ends in /, by a relative path that leaves it nowhere."""
    if not isinstance(path, str) or not returns: return False
    parts = Path(path).parts
    return any(path == entry if not entry.endswith("/") else path.startswith(entry) and ".." not in parts
               and len(parts) > len(Path(entry).parts) for entry in returns)


def group_files(config, lane):
    """What comes back from a lane group's run beside its own output and receipt: each of its lanes'
    log and receipt, as paths relative to the group's results, lanes/LANE.log and lanes/LANE.json.
    None for a lane that is no group."""
    members = config.get("lanes", {}).get(lane, {}).get("parallel") if lane is not None else None
    return [f"lanes/{member}{suffix}" for member in members for suffix in (".log", ".json")] if members else None


def asleep():
    """Seconds this Mac has slept since it started, or None where that cannot be read: a clock that runs
    through sleep less one that stops for it. On macOS those are CLOCK_MONOTONIC_RAW and CLOCK_UPTIME_RAW,
    which clock_gettime(3) describes as the same clock but for sleep; on Linux, CLOCK_BOOTTIME and
    CLOCK_MONOTONIC. Only a difference of two readings means anything."""
    for running, stopping in [("CLOCK_MONOTONIC_RAW", "CLOCK_UPTIME_RAW"), ("CLOCK_BOOTTIME", "CLOCK_MONOTONIC")]:
        if hasattr(time, running) and hasattr(time, stopping):
            try: return time.clock_gettime(getattr(time, running)) - time.clock_gettime(getattr(time, stopping))
            except OSError: return None
    return None


def heartbeat(channel, seconds):
    """Write a heartbeat to the second Mac's standard input every this many seconds until the connection ends, from
    a thread of its own, so that a stalled reader of this run's output does not read there as this Mac's crash."""
    while channel.poll() is None:
        time.sleep(seconds)
        if channel.poll() is None: tell(channel, b"")


def dispatch_records():
    """Where this Mac records its running journaled dispatches: durable, so that a record outlasts a reboot of this
    Mac, and beside its admission log, so that a test's HEAVY_SLOT_DIR keeps its own."""
    return resources.log_path().parent / "dispatchers"


def remember_dispatch(journal, host):
    """Record this process as a journaled run's dispatcher, and return the record's path, or None when it cannot be
    recorded: a run this Mac cannot then find again ends at its bound on the second Mac, as any unattended run does."""
    try:
        path, start = dispatch_records() / f"{journal}.json", resources.process_start(os.getpid())
        if start is None: raise OSError("this process's start time cannot be read")
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_suffix(".partial")
        partial.write_text(json.dumps({"journal": journal, "host": host, "pid": os.getpid(), "start": start}))
        partial.rename(path)
        return path
    except OSError as error:
        print(f"remote: this dispatch is not recorded, so a crash of it would not stop its run early: {error}",
              file=sys.stderr, flush=True)
        return None


def dispatcher_dead(pid, start):
    """Whether the dispatcher a record names is proven gone: no process has its PID, or one that started at another
    time does, as after a reboot. A process that started when it did is alive, however long it has been asleep, and
    a PID that cannot be read proves nothing."""
    try: return resources.process_start(pid) != start
    except (OSError, ValueError, subprocess.SubprocessError): return False


def stop_dead_dispatches():
    """Stop each run on the second Mac whose dispatcher on this Mac is proven dead, by its own journal alone, so that
    the run leaves admission and frees its checkout the usual way. Only a record the second Mac has answered for is
    removed: one it could not be asked about stays for a later dispatch or worker, and says nothing was stopped."""
    try: paths = sorted(dispatch_records().glob("*.json"))
    except OSError: return
    source = None
    for path in paths:
        try:
            record = json.loads(path.read_text())
            journal, host, pid, start = record["journal"], record["host"], record["pid"], record["start"]
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if not dispatcher_dead(pid, start): continue
        source = source or Path(offload_host.__file__).read_text()
        request = json.dumps({"reattach": {"root": ROOT, "id": journal, "stop": True}})
        try:
            done = ssh(host, host_command(source, request), stdin=subprocess.DEVNULL,
                       capture_output=True, text=True, timeout=STOP_DEAD_WAIT)
            state = final_object(done).get("stop") if done.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            state = None
        if state not in ("stopping", "ended", "gone"):
            print(f"remote: dispatcher {pid} of {journal} has died, and {host} could not be told to stop its run; "
                  "a later dispatch tries again", file=sys.stderr, flush=True)
            continue
        with contextlib.suppress(OSError): path.unlink()
        if state == "stopping":
            print(f"remote: dispatcher {pid} of {journal} has died; told {host} to stop its run", file=sys.stderr,
                  flush=True)


def lost(host, status, said, dispatched, slept, output, journal=None, gone=False):
    """What `qk remote` says of a run that returned no result: ssh's own last line, said, when it exits
    255 for a failure of its own; when the run was dispatched and lost, in UTC; and how long this Mac
    slept in between, of which slept is asleep()'s reading at the dispatch. journal is where the second Mac
    keeps a run that outlives its connection, and gone says that its process there ended with no result."""
    now, sleeping = time.time(), asleep()
    moment = lambda seconds: time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(seconds))
    cause = f"ssh exited {status}" + (f": {said}" if status == 255 and said else "")
    sleep = sleeping - slept if None not in (slept, sleeping) else 0
    return (f"No result from {host} ({cause}); dispatched at {moment(dispatched)} and lost at {moment(now)}"
            + (f", and this Mac slept for {sleep:.0f} s of that" if sleep >= SLEEP_NOTED else "")
            + (f". The run is unmeasured, and {host} stops it once its end of the connection closes. "
               if journal is None else
               f". The run is unmeasured: it ended on {host} and left no result in {journal}. " if gone else
               f". The run is unmeasured: no later connection collected it. {host} goes on with it until its bound "
               f"at most, and keeps what it said in {journal}. ")
            + f"Its output is in {output}")


def detached_seconds(config, lane, work):
    """How long a run on the second Mac may last with nobody attending it: its own timeouts, each lane's,
    N times the lane's for a --repeat series as qk run limits it there, or the time limit work's `qk admit`
    carries, with UNATTENDED_WAIT for what comes before them. queuekeeper stops each of them at its timeout there, and
    this bounds an queuekeeper that does not."""
    lanes = config.get("lanes", {})
    def own(name):
        entry = lanes.get(name, {})
        if "parallel" in entry: return sum(own(member) for member in entry["parallel"])
        return entry.get("timeout", processes.DEFAULT_LIMIT)
    head = work[:work.index("--")] if "--" in work else work
    limit = own(lane) * (lane_results.repeats_asked(work) or 1) if lane is not None else \
        float(head[head.index("--time-limit") + 1]) if "--time-limit" in head else processes.DEFAULT_LIMIT
    return limit + (own("provision") if "provision" in lanes and lane != "provision" else 0) + UNATTENDED_WAIT


def tell(channel, word):
    """Say a word to offload_host.py on the second Mac (offload_host.watch), if the connection still takes it."""
    try:
        channel.stdin.write(word + b"\n")
        channel.stdin.flush()
    except (OSError, ValueError):
        pass


def hang_up(channel):
    """Close channel's standard input once its connection has ended. A word it could no longer take, such as a
    heartbeat that woke after a busy decline, stays in the writer's buffer, and closing tries it once more: that
    fails with a broken pipe, which here means only that the second Mac's end has closed, as the connection's end
    already says. Left open, the writer's finalizer would try it again, in whichever thread drops the channel last,
    and print the failure as an ignored exception (vocaro-engineering#380). Any other failure is raised."""
    try: channel.stdin.close()
    except BrokenPipeError: pass


class Frames:
    """What one dispatch has heard from the second Mac, in order, across every connection that carried it: the
    lane's log and the output, kept in log and output, and what came back in staging, the files in returned.

    A later connection sends the run's journal from its start (offload_host.reattach), so rewind has the log and
    the output skip what they already hold, by the bytes of each, and the files come again whole."""

    KINDS = (b"L ", b"R ", b"O ", b"F ", b"D ", b"J ", b"E ", b"W ")

    def __init__(self, staging, log, output, returns, host=None, progress=None):
        self.staging, self.log, self.output, self.returns = staging, log, output, returns
        # The Mac whose `W` frames these are, and the lane's Progress, which each sees a frame once.
        self.host, self.progress = host, progress
        self.result, self.stray, self.receiving = None, None, None
        self.framed, self.journaled, self.gone, self.again = False, False, False, False
        # Whether offload_host.py started there, and the last line of anything else, such as the shell's.
        self.returned = []  # The files asked back that have arrived, in order.
        self.held, self.skip = {"log": 0, "output": 0, "wait": 0}, {"log": 0, "output": 0, "wait": 0}

    def once(self, which, data):
        """The part of data, the next bytes of the log or the output, not heard before a reconnection."""
        skipped = min(self.skip[which], len(data))
        self.skip[which] -= skipped
        self.held[which] += len(data) - skipped
        return data[skipped:]

    def rewind(self):
        """Forget what a replay of the journal brings again."""
        self.skip = dict(self.held)
        self.close()
        for path in self.returned: (self.staging / path).unlink(missing_ok=True)
        self.returned.clear()
        self.again = True

    def close(self):
        if self.receiving is not None: self.receiving.close()
        self.receiving = None

    def take(self, line):
        """Take one line from the connection, and return its kind."""
        kind, data = line[:2], line[2:]
        if kind in self.KINDS and not line.endswith(b"\n"): return None  # The connection cut it short.
        self.framed = self.framed or kind in self.KINDS
        if kind == b"L ":
            fresh = self.once("log", data)
            self.log.write(fresh)
            if fresh and self.progress is not None: self.progress.arrived(fresh)
        elif kind == b"R ":
            self.result = json.loads(data)
        elif kind == b"W ":
            fresh = self.once("wait", data)
            if fresh: lane_wait.relayed(fresh, self.host)
        elif kind == b"J ":
            self.journaled = True
        elif kind == b"E ":
            self.gone = True
        elif kind == b"F ":
            self.close()
            try: path = json.loads(data)["path"]
            except (ValueError, TypeError, KeyError): path = None
            # Only a file the plan asked back, by its exact path, and each once.
            if returned_path(path, self.returns) and path not in self.returned:
                (self.staging / path).parent.mkdir(parents=True, exist_ok=True)
                self.receiving = (self.staging / path).open("wb")
                self.returned.append(path)
        elif kind == b"D ":
            if self.receiving is not None:
                try:
                    self.receiving.write(base64.b64decode(data))
                except binascii.Error:
                    self.close()
                    damaged = self.returned.pop()
                    (self.staging / damaged).unlink()
                    print(f"remote: {damaged} came back damaged and was not kept", file=sys.stderr, flush=True)
        elif kind == b"O " or not self.again:
            if kind != b"O ": self.stray = last_line(line.decode(errors="replace")) or self.stray
            data = self.once("output", data if kind == b"O " else line)
            self.output.write(data)
            write(sys.stdout, data)
        return kind


# What a lane run on the second Mac says while it runs (vocaro-engineering#320). On 2026-11-08 a working 27-minute
# BreezEPUB run wrote nothing here, a session took it for a stalled one, and stopping it lost its iPad result. The
# lines count for nothing: they go to this Mac's stderr, never into run.log or output.log, which stay what the second
# Mac wrote. The bound: a heartbeat each PROGRESS_INTERVAL seconds, 41 over the longest lane timeout measured then
# (12,300 s), and at most PROGRESS_LINES relayed lines, each cut at PROGRESS_WIDTH characters; the run.log keeps them all.
PROGRESS_INTERVAL = 300
PROGRESS_LINES = 200
PROGRESS_WIDTH = 300
# A lane's log line that says what it has reached: a project's iOS script's (BreezEPUB's test-ios.sh, as of 2026-11-09)
# and the toolkit's own simulator pool's and qk simulator-tests's.
PROGRESS_LINE = re.compile(r"(?:Taking: |TOOK |Testing: |Passed: |Failed: |Unmeasured: |BUILD |BUILT |TEST |SHARD "
                           r"|PASS |FAIL |UNMEASURED )")
# What evidence.py counts wherever a log holds it, written in lower case in the copy a progress line shows, since a
# lane nested in another or the merge queue's gate log holds this output too: "Executed 6 tests" there is a second count.
COUNTED_PHRASE = re.compile(r"Executed (?=\d)|Test run with (?=\d)|Test skipped - Unmeasured:")
CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]+")


def progress_text(data):
    """The copy of a lane's log line (bytes) that a progress line shows: on one line, cut at PROGRESS_WIDTH, and such
    that no reader of a log takes it for a test count or an unmeasured test, or only its first word if it still would."""
    text = CONTROL_CHARACTERS.sub(" ", data.decode(errors="replace")).strip()[:PROGRESS_WIDTH]
    text = COUNTED_PHRASE.sub(lambda match: match.group(0).lower(), text)
    return text.split()[0] if lane_results.counted(text) is not None or lane_results.unmeasured_tests(text) else text


def spelled_seconds(seconds):
    """A span of seconds for a progress line: 45s, or 27m 03s."""
    seconds = int(seconds)
    return f"{seconds}s" if seconds < 60 else f"{seconds // 60}m {seconds % 60:02d}s"


def clock_line(config, lane, arguments=()):
    """The direct lane's timeout, the simulator default test allowance when known, and the heartbeat cadence."""
    settings = config.get("lanes", {}).get(lane, {})
    limits = f"its timeout is {parallel.lane_timeout(settings, arguments):g}s"
    words = settings.get("command")
    if not isinstance(words, list): words = []
    if words and re.fullmatch(r"python(?:3(?:\.\d+)?)?", Path(words[0]).name):
        words = words[1:]
    if len(words) >= 2 and Path(words[0]).name == "qk" and words[1] == "simulator-tests":
        allowance = extensions.test_allowance([*words[2:], *arguments])
        if allowance is not None:
            limits += f" and the default test allowance is set to {allowance}s"
    return f"{lane}'s log is arriving: {limits}; a heartbeat every {PROGRESS_INTERVAL:g}s says whether the log has grown"


class Progress:
    """A lane's run on the second Mac, as this Mac sees it arrive: its progress lines relayed with the time they came,
    and a heartbeat that says only what is known, whether its log grew. An open connection is no progress, so a heartbeat
    never says a run is working: a reader compares its numbers. Nothing here can end the run: a stderr that has closed
    ends these lines, not the lane (a redirect's reader that left, say). A group's lanes log apart, and those logs come
    back when it ends (group_files), so the heartbeat says it cannot tell. Nothing judges a stall yet: that needs each
    lane's slowest measured step, which leaves part of #320 open."""
    def __init__(self, lane, host, group, clock_said, clock=time.monotonic):
        self.lane, self.host, self.group, self.clock_said, self.clock = lane, host, group, clock_said, clock
        self.began = self.beaten = clock()
        self.lines, self.lines_at_beat, self.latest = 0, 0, None
        self.relayed, self.last, self.closed = 0, None, False

    def tell(self, kind, text):
        if self.closed: return
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        try: print(f"remote: {kind} {stamp}: {text}", file=sys.stderr, flush=True)
        except (OSError, ValueError): self.closed = True

    def arrived(self, data):
        """Note a line of the lane's log (bytes), and relay it if it is a progress line."""
        moment = self.clock()
        if not self.lines and not self.group: self.tell("clock", self.clock_said)
        self.lines, self.latest = self.lines + 1, moment
        text = progress_text(data)
        if not PROGRESS_LINE.match(text): return
        self.last = (text, moment)
        if self.relayed < PROGRESS_LINES:
            self.tell("progress", text)
        elif self.relayed == PROGRESS_LINES:
            self.tell("progress", f"{PROGRESS_LINES} progress lines shown; the heartbeat quotes only the latest from here, "
                                  "and the run's log holds them all")
        self.relayed += 1

    def beat(self):
        """Say what has arrived since the last heartbeat, when one is due. Called only when no line is arriving, so
        that a heartbeat does not fall inside a block of the run's own output, such as the lanes' times."""
        moment = self.clock()
        if moment - self.beaten < PROGRESS_INTERVAL: return
        self.beaten, grew, self.lines_at_beat = moment, self.lines - self.lines_at_beat, self.lines
        since = spelled_seconds(moment - self.began)
        if self.group:
            said = "its lanes log apart and those logs come back when it ends, so this cannot say whether they advance"
        elif self.latest is None:
            said = "no line of its log yet, so it has not started or has written nothing"
        elif grew:
            said = f"its log grew by {grew} line{'' if grew == 1 else 's'}, the latest {spelled_seconds(moment - self.latest)} ago"
        else:
            said = f"its log has not grown for {spelled_seconds(moment - self.latest)}, so nothing shows it moving"
        if self.last is not None:
            said += f"; last progress line {spelled_seconds(moment - self.last[1])} ago: {self.last[0]}"
        self.tell("heartbeat", f"{self.lane} on {self.host}, {since} in: {said}")


def execute(root, config, host, work, *, lane=None, arguments=(), expect=None, decline=False, lease=None,
            serial=False, subdirectory=None, sent=None, changes=False, slot=None, slot_wait=None):
    """Run queuekeeper's work on the second Mac and return its exit status, with the log and receipt kept here.

    work is the second Mac's queuekeeper arguments: run [--serial] [--expect N] LANE [-- ARGUMENTS] or
    admit [--exclusive | --model] -- COMMAND, which runs in the checkout's root, or in its subdirectory
    when one is named. It runs at HEAD, or at a snapshot commit of the working tree when that has
    uncommitted changes. A lane group's lanes' logs and receipts come back under the results' lanes/
    once the run there ends, however it ended (group_files), and what a lane wrote under its results'
    results/ comes back under the results' results/ (RESULTS). The second Mac waits for a checkout and
    for its own admission; with decline, it refuses work it cannot start now instead, and Busy says
    why, as it does when no checkout came free in time. Unreachable says that ssh could not reach the
    second Mac before the work was sent, and Misconfigured what of the second Mac's configuration kept
    the work from running there, and what fixing it takes. The second Mac's device record defers
    physical devices to this Mac's, and lease gives the run the reservations it carries from here.
    serial says the work runs a group's lanes in turn: the serial reference, whose result, like a
    snapshot's, is no commit's pass. sent, when given, keeps what the first call sent: a later call
    with it offers the same commits again, without a snapshot, the preparing connection or a push,
    since the second Mac holds them. With changes, what the command changed in the checkout's files that Git
    does not ignore comes back as a patch, CHANGES, beside the results (offload_host.gather_changes), and sent's
    ran says where the results are and what the patch holds (brought_back). With slot, only that checkout slot
    will do, and the second Mac waits for it for slot_wait seconds (offload_host.claim); the receipt's
    checkoutSlot says which was asked for and given, where it is, and how long the wait for it took.

    The receipt's preparation records what came before the second Mac's queue: the seconds of
    staging the commits, the preparing connection and the pushes here, each push's record (send),
    and both Macs' Git. The result's phases give the second Mac's wait for a checkout, placing the
    commits there and each queuekeeper command, and the work's own receipt its queue's wait. A dispatch's
    results stay in a directory of their own as well as in the latest (file_results); a push that
    fails or is stopped leaves its preparation there, with nothing in the latest.
    """
    toolkit = Path(TOOLKIT_ROOT).resolve()
    dispatch = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + f"-{os.getpid()}"
    name = lane if lane is not None else "admit"
    base = Path(root) / ".build/queuekeeper/remote"
    if sent:
        commit, head, project, kit, developer, local = (sent[key] for key in ("commit", "head", "project", "kit",
                                                                             "developer", "local"))
        preparation = {**sent["preparation"], "offeredAgain": True} if sent.get("preparation") else None
    else:
        preparation = {"git": {"here": offload_host.output(["git", "--version"])}, "seconds": {}, "sends": []}
        # Staging: the snapshot, when the working tree has changes, and the toolkit's commit.
        began = time.monotonic()
        try:
            commit, head = snapshot(root)
            directory = checkout_name(root)
            kit = None
            project = {"directory": directory, "repository": f"repositories/{directory}.git", "commit": commit,
                       "ref": f"{REF}{commit}"}
            if toolkit == Path(root).resolve():
                project["origin"] = origin(root)
            else:
                # The toolkit's working tree goes as the project's does, a snapshot when it has changes
                # (owner's decision, vocaro-engineering#104), but a clean checkout on main that is only
                # behind sends origin/main instead (owner's decision, vocaro-engineering#227).
                kit_commit, kit_head = snapshot(toolkit)
                kit_directory = checkout_name(toolkit)
                upstream = followed(toolkit, kit_commit, kit_head)
                sending_kit = upstream or kit_commit
                kit = {"directory": kit_directory, "repository": f"repositories/{kit_directory}.git",
                       "commit": sending_kit, "head": upstream or kit_head, "ref": f"{REF}{sending_kit}",
                       "origin": origin(toolkit)}
                if upstream:
                    kit.update(checkoutHead=kit_head, behind=git_state.behind(toolkit) or 0)
        except RuntimeError as error:  # Git cannot name a commit to send, so nothing is sent.
            raise Unavailable(str(error)) from error
        preparation["seconds"]["staging"] = round(time.monotonic() - began, 3)
        developer = developer_directory()
        local = offload_host.identity(developer)
    kept = store(root) / commit
    members = group_files(config, lane)
    returns = [*(members or []), RESULTS] if lane is not None else [CHANGES] if changes else None
    plan = {"root": ROOT, "checkouts": CHECKOUTS, "project": project, "toolkit": kit, "work": work,
            # The folders beside the project its extensions load from, which the second Mac links to its own.
            "siblings": sibling_folders(root),
            "decline": decline, "busy": BUSY,
            "unlockedScreen": lane is not None and unlocked_screen(config.get("lanes", {}), lane),
            "provision": "provision" in config.get("lanes", {}) and lane != "provision",
            "developerDir": developer, "xcode": local["xcode"],
            "log": f".build/queuekeeper/{lane}/run.log" if lane is not None else None,
            "wait": lane_wait.relative(lane) if lane is not None else None,
            "receipt": f".build/queuekeeper/{name}/source.json",
            "returns": {"directory": f".build/queuekeeper/{name}", "paths": returns} if returns else None,
            "changes": f".build/queuekeeper/{name}/{CHANGES}" if changes else None,
            "deviceRecord": local["name"], "deviceSetup": extensions.DEVICES.remote_setup(local["name"]), "lease": lease.plan(local["name"]) if lease is not None else None,
            "dispatcher": local["name"], "subdirectory": subdirectory, "silence": HEARTBEAT_SILENCE,
            # Its admissions there name the repository, branch, worktree and session here (resources.work_origin).
            **({"origin": sender} if (sender := resources.work_origin()) else {}),
            **({"checkoutSlot": slot, "checkoutWait": slot_wait} if slot is not None else {})}
    # A run that uses this Mac's device reservations ends with its connection (Lease). Any other outlives it, within
    # its own bound, for a later connection to collect (Frames, offload_host.Journal; vocaro-engineering#321).
    detached = detached_seconds(config, lane, work) if lease is None else None
    journal = f"{dispatch}-{uuid.uuid4().hex[:8]}"
    if detached is not None: plan["detach"] = {"id": journal, "seconds": detached}

    at = commit[:12] if commit == head else f"{commit[:12]}, a snapshot of the working tree on {head[:12]},"
    if kit_snapshot(kit):
        at += f" with the toolkit at {kit['commit'][:12]}, a snapshot of its working tree on {kit['head'][:12]},"
    if sent:
        print(f"remote: offering {at} to {host} again", file=sys.stderr, flush=True)
    else:
        # Before anything is sent, so that the slots and admission a crashed dispatcher's run holds come free for this.
        stop_dead_dispatches()
        # A snapshot's result is no commit's pass, which nothing else says until eng release stops for it
        # (vocaro-engineering#197).
        why = uncounted(root, lane) if lane is not None and commit != head and not arguments and expect is None \
            and not serial else None
        if why: print(f"remote: {why}", file=sys.stderr, flush=True)
        repositories = [f"{ROOT}/{part['repository']}" for part in (project, kit) if part is not None]
        # Two first dispatches of a project at once both create its repository, and one `git init` then
        # fails with "cannot mkdir ...: File exists"; once the other's has finished, a second leaves its
        # repository as it is.
        init = lambda path: f"git init --bare --quiet {shlex.quote(path)}"
        prepare = " && ".join([f"mkdir -p {shlex.quote(ROOT + '/repositories')}", *(
            f"{{ [ -d {shlex.quote(path)} ] || {init(path)} 2>/dev/null || {{ sleep 1; {init(path)}; }}; }}"
            for path in repositories)])
        print(f"remote: sending {at} to {host}", file=sys.stderr, flush=True)
        if kit is not None and kit.get("checkoutHead"):
            print(f"remote: {following_line(kit)}", file=sys.stderr, flush=True)
        elif kit is not None:
            gap = toolkit_gap(toolkit)
            if gap: print(f"remote: {gap}", file=sys.stderr, flush=True)
        # The connection that prepares the repositories also lists what earlier runs left running there,
        # before any work is sent: a connection to the second Mac can take seconds of its own. It has
        # each repository name the commits its checkouts hold, which the pushes then need not send.
        attempt, moment = asleep(), time.monotonic()
        # It also fast-forwards the second Mac's own toolkit checkout when it can, which no run there uses
        # (vocaro-engineering#203): bounded, and nothing of it stops the work.
        census = census_command(repositories, reap_request(root), update=update_request(),
                                siblings=sibling_requests(root))
        prepared = ssh(host, f"{prepare} || exit $?\n{census} || true",
                       stdin=subprocess.DEVNULL, capture_output=True, text=True)
        preparation["seconds"]["connection"] = round(time.monotonic() - moment, 3)
        # ssh's own failures, a name that does not resolve among them, exit 255. Nothing is sent yet.
        if prepared.returncode == 255: raise ssh_failure(host, prepared.stderr, attempt)
        if prepared.returncode:
            raise Misconfigured(f"{host} could not prepare ~/{ROOT} (exit {prepared.returncode}): "
                                f"{last_line(prepared.stderr) or 'no output'}",
                                f"make ~/{ROOT} writable there and install the Xcode this Mac uses, with its license "
                                "accepted and its first launch run (guides/shared-host.md, Setting up the second Mac)")
        for line in reap_lines(host, final_object(prepared).get("reap"), listing=False):
            print(f"remote: {line}", file=sys.stderr, flush=True)
        for line in leftover_lines(host, *read_census(prepared)):
            print(f"remote: {line}", file=sys.stderr, flush=True)
        answered = final_object(prepared)
        updated, _ = toolkit_line(host, answered.get("toolkitCheckout"))
        if updated: print(f"remote: {updated}", file=sys.stderr, flush=True)
        for found in answered.get("siblingCheckouts") or []:
            updated, _ = toolkit_line(host, found, what="checkout")
            if updated: print(f"remote: {updated}", file=sys.stderr, flush=True)
        advertised = answered.get("advertised") if isinstance(answered.get("advertised"), dict) else {}
        preparation["git"]["there"] = answered.get("git")
        moment = time.monotonic()
        pushes = [(root, repositories[0], commit), *([(toolkit, repositories[1], kit["commit"])] if kit else [])]
        try:
            for checkout, repository, pushing in pushes:
                record = send(checkout, host, repository, pushing, advertised.get(repository), preparation["sends"])
                print(f"remote: sent {pushing[:12]} to ~/{repository} in {sending(record)}", file=sys.stderr, flush=True)
        except (Misconfigured, processes.Interrupted) as error:
            # What the preparation cost and how it ended stay, unique to this dispatch; the latest results stay whole.
            receipt = {"host": host, "commit": commit, "head": head, "snapshot": commit != head,
                       **toolkit_identity(kit, commit), "lane": lane, "arguments": list(arguments),
                       "work": work, "local": local, "preparation": preparation, "stage": "preparation",
                       "outcome": "failed-or-interrupted", "error": str(error)}
            own = file_results(base, name, dispatch, receipt=receipt, latest=False)
            print(f"remote: nothing ran on {host}; the record of what was sent is {own / 'source.json'}",
                  file=sys.stderr, flush=True)
            raise
        preparation["seconds"]["send"] = round(time.monotonic() - moment, 3)
        if sent is not None:
            sent.update(commit=commit, head=head, project=project, kit=kit, developer=developer, local=local,
                        preparation=preparation)
    if processes.requested() is not None:
        raise processes.Interrupted(processes.requested(), "before the second Mac started the run")

    results = base / name
    # Results are staged, so a refusal leaves the previous run's whole.
    staging = results.with_name(f".{name}.{os.getpid()}")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    started, slept = time.time(), asleep()
    source = Path(offload_host.__file__).read_text()
    plan["requestAge"] = max(0, time.time() - resources.request_started())
    if sent is not None and "requestStarted" in sent:
        plan["requestStarted"] = sent["requestStarted"]
    command = [*SSH, *moved(host), host, login(host_command(source, json.dumps(plan)))]
    stopping = None
    record = remember_dispatch(journal, host) if detached is not None else None
    # A lane's log comes as it grows. An `qk admit` command's output comes on stdout, live, already.
    progress = Progress(name, host, bool(members), clock_line(config, lane, arguments)) if lane is not None else None
    lane_wait.sent(host)
    with (staging / "run.log").open("wb") as log, (staging / "output.log").open("wb") as output:
        frames = Frames(staging, log, output, returns, host, progress)

        def stop_wanted():
            """Note a stop that has been asked for, once, and whether one has."""
            nonlocal stopping
            bounded = lease is not None and lease.expired()
            if (processes.requested() is not None or bounded) and stopping is None:
                stopping = time.monotonic()
                if bounded:
                    lease.timed_out = True
                    print(f"remote: the run reached its {lease.run['boundSeconds']}s bound, which its device "
                          f"reservations set; asking {host} to stop it", file=sys.stderr, flush=True)
                else:
                    print(f"remote: stop requested; asking {host} to stop the run", file=sys.stderr, flush=True)
            return stopping is not None

        def carry(channel):
            """Take what channel brings, until its end, saying a stop to the second Mac when one comes."""
            lines, told = queue.Queue(), False
            def relay():
                for line in iter(channel.stdout.readline, b""): lines.put(line)
                lines.put(None)
            threading.Thread(target=relay, daemon=True).start()
            while True:
                if stop_wanted() and not told:
                    told = True
                    tell(channel, b"stop")
                if stopping is not None and time.monotonic() - stopping > STOP_WAIT and channel.poll() is None:
                    print(f"remote: {host} did not finish stopping in {STOP_WAIT}s; closing the connection",
                          file=sys.stderr, flush=True)
                    channel.terminate()
                try:
                    line = lines.get(timeout=0.1)
                except queue.Empty:
                    if progress is not None and stopping is None: progress.beat()
                    continue
                if line is None: break
                kind = frames.take(line)
                if kind == b"J ": tell(channel, b"attached")
                elif kind == b"R ": tell(channel, b"received")
            channel.wait()
            hang_up(channel)

        def collect():
            """Reconnect, as often as it takes, until a connection brings the run's result or says it has gone. This
            Mac's waking time counts, so that a sleep of any length leaves the tries it allows. A stop has its usual
            wait, within which a connection says it to the second Mac."""
            print(f"remote: the connection to {host} ended; the run goes on there, and this dispatch reconnects to "
                  "collect it", file=sys.stderr, flush=True)
            limit, tries = time.monotonic() + detached + REATTACH_GRACE, 0
            while frames.result is None and not frames.gone:
                if stop_wanted(): limit = min(limit, stopping + STOP_WAIT)
                if time.monotonic() >= limit: break
                if tries:
                    moment = time.monotonic()
                    while time.monotonic() - moment < REATTACH_PAUSE and not stop_wanted():
                        if progress is not None and stopping is None: progress.beat()
                        time.sleep(0.2)
                tries += 1
                frames.rewind()
                carry(subprocess.Popen(
                    [*SSH, *moved(host), host, login(host_command(source, json.dumps(
                        {"reattach": {"root": ROOT, "id": journal}})))],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=environment()))
            return tries

        channel = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   env=environment())
        if lease is not None: lease.connected(channel.pid)
        threading.Thread(target=heartbeat, args=(channel, HEARTBEAT_SECONDS), daemon=True).start()
        carry(channel)
        ended, said = channel.returncode, frames.stray
        lost_at, reconnections = time.time(), None
        if frames.result is None and frames.journaled: reconnections = collect()
        frames.close()
    # A run this dispatch could neither collect nor learn the end of stays recorded, for a later one to stop.
    if record is not None and (frames.result is not None or frames.gone or not frames.journaled):
        with contextlib.suppress(OSError): record.unlink()
    result, framed, returned = frames.result, frames.framed, frames.returned
    if result is None and not framed and stopping is None:
        # offload_host.py never said it started, so nothing ran there unless its first words were lost.
        shutil.rmtree(staging, ignore_errors=True)
        if ended == 255: raise ssh_failure(host, said, slept, sent=True)
        raise no_toolkit(host, ended, said)
    if sent is not None and result is not None and "requestStarted" in result:
        # An opaque timestamp on this host: send it back unchanged, even after a checkout refusal.
        # Reconstructing it from elapsed age on every retry would add connection latency each time.
        sent["requestStarted"] = result["requestStarted"]
    if result is not None and result.get("refused"):
        shutil.rmtree(staging, ignore_errors=True)
        if result.get("busy"): raise Busy(f"{host} did not take it: {result['refused']}")
        raise Misconfigured(f"{host} refused it: {result['refused']}",
                            result.get("fix") or f"repair on {host} what that names")
    if result is None:
        # A lost dispatch has no receipt. Keep its destination beside the log for queue diagnostics.
        with contextlib.suppress(OSError):
            (staging / DISPATCH_HOST).write_text(host + "\n")
        own = file_results(base, name, dispatch, staging)
        kept = f"~/{ROOT}/{offload_host.JOURNALS}/{journal}" if frames.journaled else None
        raise Lost(lost(host, ended, said, started, slept, own / "output.log", journal=kept, gone=frames.gone))
    # commit is what ran there: HEAD, or a snapshot of the working tree on it. ranOn and mac say where, as
    # a run on this Mac's receipt does, so that the share of each Mac's work can be counted.
    receipt = {"ranOn": "second Mac", "mac": (result.get("host") or {}).get("name"), "host": host,
               "commit": commit, "head": head, "snapshot": commit != head,
               **toolkit_identity(kit, commit), "lane": lane, "arguments": list(arguments),
               "expect": expect, "work": work, "local": local, "result": result, "dispatched": started,
               "preparation": preparation,
               **({"connection": {"lostAt": lost_at, "reconnections": reconnections, "ssh": ended}}
                  if reconnections is not None else {}),
               **({"serial": True} if serial else {}), **({"returned": returned} if members or returned else {}),
               **({"subdirectory": subdirectory} if subdirectory else {})}
    wait = (result.get("phases") or {}).get("claim")
    if slot is not None:
        receipt["checkoutSlot"] = {"requested": slot, "assigned": result.get("slot"), "path": result.get("checkout"),
                                   "waitSeconds": wait}
    own = file_results(base, name, dispatch, staging, receipt)
    if sent is not None:
        sent["ran"] = {"results": str(own), "checkout": result.get("checkout"), "changes": result.get("changes"),
                       "returned": CHANGES in returned}
    # A snapshot's result stays here: no branch holds it, so it is no commit's pass. Nor is a group's
    # serial reference: the group's pass is its lanes' side by side.
    if lane is not None and not arguments and expect is None and commit == head and not serial \
            and not kit_snapshot(kit):
        keep(kept, lane, receipt, own / RESULTS)
    if slot is not None and result.get("slot") is not None:
        print(f"remote: checkout slot {slot}, as requested, at {result.get('checkout')}, after {wait}s waiting for it",
              file=sys.stderr, flush=True)
    if preparation:
        print(f"remote: {preparing(preparation, result.get('phases') or {}, result.get('receipt'))}", file=sys.stderr,
              flush=True)
    if result.get("error"):
        raise RuntimeError(f"{host}: {result['error']}")
    status = result["exitStatus"]
    remote = result["host"]
    print(f"remote: {name} at {at} on {describe(remote)} in {result.get('checkout')}: "
          f"{'stopped' if result['stopped'] else f'exit {status}'}; results in {results}, and this dispatch's alone in "
          f"{own}", file=sys.stderr, flush=True)
    files = [path for path in returned if path.startswith(RESULTS)]
    if files:
        # A group's lanes each write under results/LANE/.
        group = config["lanes"][lane].get("parallel") or () if members else ()
        wrote = sorted({Path(path).parts[1] for path in files if len(Path(path).parts) > 2} & set(group))
        print(f"remote: {len(files)} file{'s' if len(files) != 1 else ''} the run wrote in its {RESULTS} came back in "
              f"{results / RESULTS}" + (f", from {', '.join(wrote)}" if wrote else ""), file=sys.stderr, flush=True)
        # This dispatch's own: another run of the lane from this checkout may since have filed its own as the latest.
        extensions.report_results(own / RESULTS)
    if members and result.get("stage") == "work":
        members = config["lanes"][lane]["parallel"]
        came = [member for member in members if f"lanes/{member}.log" in returned]
        if came:
            print(f"remote: the logs and receipts of {name}'s lanes are in {results / 'lanes'}: {', '.join(came)}",
                  file=sys.stderr, flush=True)
        if len(came) < len(members):
            print(f"remote: no log came back for {', '.join(member for member in members if member not in came)}",
                  file=sys.stderr, flush=True)
    if (remote.get("macOS"), remote.get("build")) != (local.get("macOS"), local.get("build")):
        print(f"remote: the second Mac runs macOS {remote.get('macOS')} ({remote.get('build')}) and this Mac "
              f"{local.get('macOS')} ({local.get('build')}): the result qualifies the second Mac's platform",
              file=sys.stderr, flush=True)
    if result.get("stage") == "provision" and status and not result["stopped"]:
        raise Misconfigured(f"{host} {UNPROVISIONED}: the provision lane exited {status} in "
                            f"{result.get('checkout')}, so {name} did not run there; its log is {own / 'run.log'}",
                            f"give {host} what provisioning needs, which that log names, such as a tool "
                            "`qk remote setup` installs or the git-credentials token for private dependencies "
                            "(guides/shared-host.md, Setting up the second Mac)")
    return exit_status(result)


def exit_status(result):
    """The status a dispatch returns for offload_host.py's result: the work's. A stop that came before the work
    began, once the provision lane had passed, leaves only that lane's 0, which says nothing of the work: such
    a run, like one stopped before any command, is 1 (vocaro-engineering#317)."""
    status = result.get("exitStatus")
    if status is None or (status == 0 and result.get("stopped") and result.get("stage") != "work"): return 1
    # A command a signal ended has a negative status; a shell reports it as 128 plus the signal.
    return 128 - status if status < 0 else status


def checkout_request(args):
    """The checkout slot --checkout-slot names and the seconds to wait for it, as (slot, wait), or (None, None)
    without one. A value the second Mac's pool does not hold is a refusal here, before anything is sent."""
    slot, wait = args.checkout_slot, args.checkout_wait
    if slot is None:
        if wait is not None: raise ValueError("--checkout-wait bounds the wait for a slot: add --checkout-slot")
        return None, None
    if not (slot.isascii() and slot.isdigit() and int(slot) < CHECKOUTS):
        raise ValueError(f"--checkout-slot {slot} names no checkout: the second Mac keeps slots 0 to {CHECKOUTS - 1}")
    if wait is None: wait = offload_host.CLAIM_TIMEOUT
    if not (math.isfinite(wait) and wait >= 0):
        raise ValueError("--checkout-wait needs a number of seconds, 0 or more")
    return int(slot), wait


# Seconds exclusive work sent with `qk remote admit --exclusive` is offered to the second Mac to start at once,
# before it takes a place in the second Mac's queue (exclusive_offers).
EXCLUSIVE_PARK_SECONDS = 900


def exclusive_offers(name, offer, park, park_after=None, clock=time.monotonic, sleep=time.sleep):
    """offer()'s status once the second Mac starts the exclusive work at once, or park()'s, its status after
    waiting in the second Mac's queue, once EXCLUSIVE_PARK_SECONDS of offers it declined have passed.

    Exclusive work queued on a Mac holds back every later request there while the slots it needs drain
    (resources.Admission, guides/shared-host.md). Queued from the start, a gate suite sent as exclusive work
    kept the second Mac at under 20% CPU, refusing every lane for 12 minutes while one lane drained, and those
    lanes all went to this Mac (2026-11-12). So it is offered first as `--if-free` work: the second Mac starts it
    only while every slot is free and no request queued ahead of it waits, carrying the work's arrival from the
    first offer (requestStarted), so it never starts before older work; a declined offer leaves no place behind,
    since the second Mac's admit leaves its place as its process ends. Offers come OVERFLOW_WAIT apart, doubling
    to OVERFLOW_WAIT_MAX. Work that keeps finding a slot busy would wait forever that way, so after park_after
    seconds it queues there at its first arrival, and slots drain toward it as before."""
    deadline = clock() + (EXCLUSIVE_PARK_SECONDS if park_after is None else park_after)
    window, said = OVERFLOW_WAIT, None
    while True:
        try:
            return offer()
        except Busy as busy:
            left = deadline - clock()
            if left <= 0: break
            if str(busy) != said:
                print(f"queuekeeper: {name} waits to start on the second Mac with every slot free, asked again every "
                      f"{OVERFLOW_WAIT_MAX}s or sooner, holding no place in its queue for {int(left)}s more: {busy}",
                      file=sys.stderr, flush=True)
                said = str(busy)
            sleep(min(window, left))
            window = min(2 * window, OVERFLOW_WAIT_MAX)
    print(f"queuekeeper: {name} takes its place in the second Mac's queue after {EXCLUSIVE_PARK_SECONDS if park_after is None else park_after:g}s "
          "of offers it declined, and its slots drain toward it", file=sys.stderr, flush=True)
    return park()


def not_run_if_busy(name, send):
    """send()'s status, or BUSY, after saying so, when a named slot's wait ran out: nothing ran, as with
    cli.admitted_here, and Busy raised would be queuekeeper's own error, exit 2."""
    try:
        return send()
    except Busy as busy:
        print(f"queuekeeper: {name} not run on the second Mac: {busy}", file=sys.stderr, flush=True)
        return BUSY


def commit_arguments(declarations):
    """Verify --git-commit REPOSITORY=SHA locally, before dispatch. Only commits qualify; no repository
    path or exemption needs to reach the second Mac, whose admitted command keeps the original argv."""
    commits = set()
    for declaration in declarations:
        repository, separator, commit = declaration.rpartition("=")
        if not separator or not repository or not re.fullmatch(r"[0-9a-fA-F]{40}", commit):
            raise ValueError("--git-commit needs REPOSITORY=SHA with a full 40-digit commit hash")
        try:
            result = subprocess.run(["git", "--no-replace-objects", "-C", repository, "cat-file", "-t", commit],
                                    env=environment(),
                                    capture_output=True, text=True, timeout=OBJECT_TIMEOUT)
        except (OSError, subprocess.SubprocessError) as error:
            raise ValueError(f"--git-commit could not verify {commit} in {repository}: {error}") from error
        if result.returncode or result.stdout.strip() != "commit":
            raise ValueError(f"--git-commit {commit} is not a commit in {repository}")
        commits.add(commit.upper())
    return commits


def main(root, config, args):
    if args.operation == "passed":
        receipt = passed(root, args.lane, args.commit)
        if receipt is None:
            print(f"No passing run of the whole {args.lane} lane at {args.commit} kept by commit, from either Mac")
            return 1
        print(f"{args.lane} passed at {receipt['commit']} {ran_where(receipt)}")
        if receipt.get("keptResults"):
            print(f"Its results are kept in {store(root) / receipt['commit'] / receipt['keptResults']}")
        return 0
    host = args.host or named_host() or alias() or DEFAULT_HOST
    if args.operation == "setup":
        return setup(host, args.formulae, args.imports or [], args.check, args.runtimes, root=root)
    if args.operation == "foreground": return foreground_report(host)
    slot, slot_wait = checkout_request(args)
    if args.operation == "run":
        lane = config.get("lanes", {}).get(args.lane)
        if lane is None: raise ValueError(f"No configured lane: {args.lane}")
        if args.expect is not None and args.expect < 1: raise ValueError("--expect needs at least 1 test")
        if args.serial and "parallel" not in lane:
            raise ValueError(f"--serial runs a lane group, and {args.lane} is not one")
        extra = args.passed or []
        # The second Mac runs a group's lanes in turn under its own admission, as `qk run --serial` does here.
        work = ["run", "--here", *(["--serial"] if args.serial else []),
                *(["--expect", str(args.expect)] if args.expect else []), args.lane, *(["--", *extra] if extra else [])]
        budget = lane.get("timeout") if "parallel" not in lane else None
        held = extensions.DEVICES.lease(args.device or [], args.device_owner, extra, root=root, budget=budget)
        # A leased run sent again, after a notification reached it, offers the same commits (carrying).
        sent = {}
        send = lambda: execute(root, config, host, work, lane=args.lane, arguments=extra, expect=args.expect,
                               lease=held, serial=args.serial, sent=sent, slot=slot, slot_wait=slot_wait)
        if slot is not None:
            sending = send
            send = lambda: not_run_if_busy(args.lane, sending)
        with processes.forwarding_signals():
            return extensions.DEVICES.carrying(held, root, host, work, args.lane, lambda: offering_here(
                f"qk run --here {args.lane}", send) if lane.get("foreground") else send())
    # Without a leading --, argparse collected the command, and a later -- belongs to it.
    command = args.command + ([] if args.passed is None else ["--", *args.passed]) if args.command else args.passed
    if not command:
        raise ValueError("Name the command: qk remote admit [--exclusive | --model] [--foreground] -- COMMAND")
    if args.time_limit is not None and args.time_limit <= 0:
        raise ValueError("--time-limit needs a positive number of seconds")
    if getattr(args, "app", None) and not args.foreground:
        raise ValueError("--app names the app under test of foreground work: add --foreground")
    kind =(["--exclusive"] if args.exclusive else ["--model"] if args.model else []) + \
        (["--foreground"] if args.foreground else []) + \
        [word for app in (getattr(args, "app", None) or []) for word in ("--app", app)] + \
        (["--time-limit", f"{args.time_limit:g}"] if args.time_limit is not None else [])
    refused = exclusive_refusal(config, command) if args.exclusive else None
    if refused: raise ValueError(f"qk remote admit --exclusive refused: {refused}")
    commits = commit_arguments(getattr(args, "git_commit", None) or [])
    held = extensions.DEVICES.lease(args.device or [], args.device_owner, command, root=root,
                                     budget=args.time_limit, commits=commits)
    work = ["admit", *kind, "--", *command]
    sent = {}
    # What the command changed in the checkout there comes back and is applied here, and the checkout there is
    # restored for the next run, as for `qk admit`'s offered command (brought_back; vocaro/queuekeeper#80).
    send = lambda: brought_back(root, sent, execute(root, config, host, work, lease=held, sent=sent, slot=slot,
                                                    slot_wait=slot_wait, changes=True), products=False)
    if args.exclusive and not args.foreground:
        # Offered while it can start at once, and only then queued there (exclusive_offers).
        parked = send
        free = ["admit", *kind, "--if-free", "--", *command]
        send = lambda: exclusive_offers("the command", lambda: brought_back(root, sent, execute(
            root, config, host, free, decline=True, lease=held, sent=sent, slot=slot, slot_wait=slot_wait,
            changes=True), products=False), parked)
    if slot is not None:
        sending = send
        send = lambda: not_run_if_busy("the command", sending)
    with processes.forwarding_signals():
        return extensions.DEVICES.carrying(held, root, host, work, "admit", lambda: offering_here(
            "qk admit --foreground -- COMMAND", send) if args.foreground else send())


def foreground_lane(root, config, lane, arguments, expect):
    """Run a foreground lane on the second Mac, as `qk remote run` does, where queuekeeper waits for its foreground and
    holds it for the run (foreground.slot). It never falls back to this Mac, the owner's workstation, whose
    foreground needs the owner's window first and then `qk run --here`: a second Mac that is not configured or
    cannot be reached stops the lane."""
    host = configured()
    if host is None:
        raise ValueError(f"{lane} takes a Mac's screen, and no second Mac is configured ({HOST_VARIABLE}, or the ssh "
                         f"alias {' or '.join(ALIASES)} in ~/.ssh/config) to run it on. This Mac's foreground needs a "
                         "window the owner agrees first (guides/shared-host.md, Attended Mac sessions), then "
                         f"`qk run --here {lane}`")
    print(f"queuekeeper: {lane} takes a Mac's screen, so it runs on the second Mac, under its foreground", file=sys.stderr,
          flush=True)
    work = ["run", "--here", *(["--expect", str(expect)] if expect else []), lane,
            *(["--", *arguments] if arguments else [])]
    with processes.forwarding_signals():
        return offering_here(f"qk run --here {lane}",
                             lambda: execute(root, config, host, work, lane=lane, arguments=arguments, expect=expect))


def there_lane(root, config, lane, arguments, expect, why):
    """Run a lane that must run on the second Mac there, as `qk remote run` does, where it waits for its
    admission. It never falls back to this Mac: a second Mac that cannot be reached stops the lane."""
    host = configured()
    if host is None: raise ValueError(f"{lane} runs only on the second Mac ({why}), and none is configured")
    print(f"queuekeeper: {lane} runs only on the second Mac ({why})", file=sys.stderr, flush=True)
    work = ["run", "--here", *(["--expect", str(expect)] if expect else []), lane,
            *(["--", *arguments] if arguments else [])]
    with processes.forwarding_signals():
        return execute(root, config, host, work, lane=lane, arguments=arguments, expect=expect)


def offering_here(here, send):
    """send()'s status, for foreground work bound for the second Mac. When the second Mac cannot be reached,
    the Unreachable it raises also says that the owner can approve a window on this Mac's foreground, within
    which here runs the work, since foreground work never moves to this Mac by itself: without that, a
    session read waking the second Mac as the only fix and stopped without asking (2026-10-31)."""
    try:
        return send()
    except Unreachable as away:
        raise Unreachable(away.problem, f"{away.fix}; or ask the owner for a window on this Mac's foreground, "
                                        "saying what would run there and for how long, since foreground work never "
                                        "moves to this Mac without one, and within it reserve mac-foreground and run "
                                        f"`{here}` (the toolkit's guides/second-mac-foreground.md, This Mac's foreground)") from None


def foreground_report(host):
    """Print what the second Mac lacks for foreground work started over SSH, each with the owner's fix, and who
    holds its foreground; exit 0 when it lacks nothing, MISCONFIGURED otherwise."""
    found = ask(host, {"foreground": True})
    entry = found.get("entry")
    holder = "no run holds it" if not entry or entry.get("state") == "released" else \
        entry.get("error") or f"{entry.get('ownerChatID')} holds it for {entry.get('purpose')}, " \
        f"expected release {entry.get('expectedReleaseBy')}"
    print(f"{found.get('mac')}'s foreground: {holder}")
    for line in crash_dialog_report(found.get("crashDialogs"), entry): print(line)
    if "captureApprovals" in found:  # a second Mac whose toolkit predates them says nothing
        for line in extensions.FOREGROUND.capture_lines(found["captureApprovals"]): print(line)
    for problem, fix in found.get("problems", []):
        print(f"- {problem}: {fix}")
    if found.get("problems"):
        print("These are the owner's settings to change, on the second Mac (guides/foreground.md); "
              "foreground work there stops until they are.")
        return MISCONFIGURED
    print("Ready for foreground work started over SSH.")
    return 0


def crash_dialog_report(found, entry):
    """What `qk remote foreground` says of the second Mac's crash-report dialogs and system alerts: a DialogType
    left off with no run holding the foreground, and every alert over the screen (crash_alerts.py). A second Mac
    whose toolkit predates them says nothing."""
    if not found: return []
    lines = []
    held = bool(entry) and entry.get("state") not in (None, "released")
    kept = found.get("record")
    if kept is not None and not held:
        before = kept.get("before") if "error" not in kept else None
        lines.append(f"- a foreground run that no longer runs left crash-report dialogs off; the next queuekeeper command "
                     f"there puts back com.apple.CrashReporter DialogType {before or 'unset'}"
                     if "error" not in kept else f"- {kept['error']}")
    elif found.get("value") == "none" and not held:
        lines.append("- crash-report dialogs are off with no run going (com.apple.CrashReporter DialogType none), "
                     "and no run left a value to put back: the owner keeps them on between runs, with "
                     "`defaults delete com.apple.CrashReporter DialogType` there")
    alerts = found.get("alerts")
    if alerts is None:
        lines.append("- its system alerts cannot be read (System Events needs accessibility)")
    for alert in alerts or []:
        x, y, width, height = alert["frame"]
        lines.append(f"- a system alert is over the screen: {alert['app']}'s window at x {x}, y {y}, "
                     f"{width}×{height}: {next(iter(alert['texts']), 'no text')!r}; a foreground run dismisses a "
                     "crash report itself and stops, unmeasured, on any other")
    return lines


def install_script(formulae):
    """Find Homebrew, put it first on the login shell's PATH in ~/.zprofile, and install formulae.

    Installing Homebrew itself needs the second Mac's password, which only the owner types, so a
    second Mac without it ends the script with NO_HOMEBREW and nothing changed."""
    candidates = " ".join(shlex.quote(candidate) for candidate in BREW)
    return f"""set -u
brew=$(command -v brew || true)
for candidate in {candidates}; do
    if [ -z "$brew" ] && [ -x "$candidate" ]; then brew=$candidate; fi
done
if [ -z "$brew" ]; then
    echo "setup: Homebrew is not installed on the second Mac. Its installer asks for the second Mac's password, so install it there yourself (https://brew.sh), then run qk remote setup again." >&2
    exit {NO_HOMEBREW}
fi
# Homebrew's bin must come before /usr/bin, or the system's python3 3.9 shadows Homebrew's, as it
# did in the build host's login shell, which found brew after /usr/bin (2026-10-26).
prefix=$("$brew" --prefix)
first=no
for entry in $(printf '%s' "$PATH" | tr ':' ' '); do
    if [ "$entry" = "$prefix/bin" ]; then first=yes; break; fi
    if [ "$entry" = /usr/bin ]; then break; fi
done
if [ "$first" = no ]; then
    line='eval "$('"$brew"' shellenv)"'
    grep -qxF "$line" "$HOME/.zprofile" 2>/dev/null || printf '\\n%s\\n' "$line" >> "$HOME/.zprofile"
    eval "$("$brew" shellenv)"
    echo "setup: put Homebrew first on the login shell's PATH in ~/.zprofile"
fi
echo "setup: brew install {' '.join(formulae)}"
"$brew" install {' '.join(shlex.quote(formula) for formula in formulae)}
"""


def verify_script(formulae, modules, developer=None, siblings=()):
    """One `ok` or `MISSING` line for each thing the gates need, read by a fresh login shell, as
    `qk remote` reads the second Mac; exit 1 when anything is missing. The last line is the
    version of the Xcode at developer, the path a run there uses, which the caller compares. siblings are
    (name, clone) for each folder beside the project that its extensions load from, whose checkout there
    setup only checks: making it is the owner's step, clone the command that does (vocaro/queuekeeper#117)."""
    lines = ["fail=0",
             "prefix=$(brew --prefix 2>/dev/null) || { echo \"MISSING Homebrew on the login shell's PATH\"; exit 1; }",
             'echo "ok      Homebrew at $prefix"',
             "python=$(command -v python3 || true)",
             'case "$python" in "$prefix"/*) echo "ok      python3 is $python, $(python3 --version 2>&1)" ;;',
             "    *) echo \"MISSING Homebrew's python3 first on the login shell's PATH (found ${python:-none})\"; fail=1 ;;",
             "esac",
             # Queuekeeper needs Python 3.14 or newer, on every Mac that runs it.
             "if python3 -c 'import sys; sys.exit(sys.version_info < (3, 14))' >/dev/null 2>&1; then "
             'echo "ok      python3 is 3.14 or newer"; '
             'else echo "MISSING python3 3.14 or newer (found $(python3 --version 2>&1))"; fail=1; fi']
    # `brew info` follows an alias such as python to its formula, where `brew list` refuses it. The
    # system's Python reads its JSON, which Homebrew's may not yet be there to do.
    installed = "/usr/bin/python3 -c 'import json, sys; sys.exit(not json.load(sys.stdin)[\"formulae\"][0][\"installed\"])'"
    for formula in formulae:
        lines.append(f'if brew info --json=v2 {formula} 2>/dev/null | {installed}; then echo "ok      {formula}"; '
                     f'else echo "MISSING {formula}"; fail=1; fi')
    for module in modules:
        lines.append(f'if python3 -c "import {module}" >/dev/null 2>&1; then echo "ok      import {module}"; '
                     f'else echo "MISSING import {module} in python3"; fail=1; fi')
    for name, clone in siblings:
        missing = (f"MISSING ~/Development/{name}, the checkout the project's extension loads from there, which "
                   f"queuekeeper does not create: make it there with {clone} "
                   "(guides/shared-host.md, Extension folders beside a project)")
        lines.append(f'if [ -d "$HOME/Development/"{shlex.quote(name)} ]; then '
                     f'echo {shlex.quote(f"ok      ~/Development/{name}")}; '
                     f'else echo {shlex.quote(missing)}; fail=1; fi')
    selected = f"DEVELOPER_DIR={shlex.quote(developer)} " if developer else ""
    lines += [f'echo "xcode   $({selected}xcrun xcodebuild -version 2>/dev/null | tr \'\\n\' \' \')"', 'exit $fail']
    return "\n".join(lines) + "\n"


def setup(host, formulae, modules, check, runtimes=False, root=None):
    """Install what the gates use on the second Mac, then check it as `qk remote` will see it.
    With check, only check. With runtimes, give both Macs the same simulator runtimes instead
    (match_runtimes). With root, it also checks that the second Mac has its own checkout of each folder
    beside the project that its extensions load from, and names the clone that makes one it lacks, which it
    leaves to the owner (vocaro/queuekeeper#117)."""
    if runtimes:
        if formulae or modules:
            raise ValueError("--runtimes evens out only the simulator runtimes; name formulae and "
                             "--import modules in a setup of their own")
        return match_runtimes(host, check)
    for name in formulae:
        if not FORMULA.match(name): raise ValueError(f"Not a Homebrew formula name: {name}")
    for name in modules:
        if not MODULE.match(name): raise ValueError(f"Not a Python module name: {name}")
    wanted = list(dict.fromkeys([*SETUP_FORMULAE, *formulae]))
    if not check:
        # Line by line through this process, so a long brew install shows as it goes.
        with subprocess.Popen([*SSH, *moved(host), host, login(install_script(wanted))], env=environment(), text=True,
                              errors="replace", stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as running:
            for line in running.stdout: print(line, end="", flush=True)
        installed = running.returncode
        if installed == NO_HOMEBREW: return 1
        if installed:
            print(f"queuekeeper: brew install on {host} exited {installed}", file=sys.stderr)
            return 1
    # A new login shell, so a PATH line just written to ~/.zprofile is read as qk remote reads it.
    # A run there uses the Xcode at this Mac's developer directory, and must find the same version.
    developer = developer_directory()
    here = " ".join((offload_host.output(["xcrun", "xcodebuild", "-version"], {**os.environ, "DEVELOPER_DIR": developer})
                     or "").split()) if developer else None
    siblings = [(name, sibling_clone(root, name)) for name in sibling_folders(root)] if root else []
    result = ssh(host, verify_script(wanted, modules, developer, siblings), capture_output=True, text=True)
    status = 0 if result.returncode == 0 else 1
    for line in result.stdout.splitlines():
        if line.startswith("xcode   "):
            there = " ".join(line[8:].split())
            if here and there != here:
                line, status = f"MISSING {here} at {developer}, as on this Mac (found {there or 'none'})", 1
            else:
                line = f"ok      {there or 'no Xcode'}" + (f" at {developer}" if developer else "")
        print(line)
    if result.returncode not in (0, 1):
        print(f"queuekeeper: could not check {host} (ssh exited {result.returncode}): {result.stderr.strip()}", file=sys.stderr)
    print(f"{host} is ready for qk remote" if status == 0 else f"{host} is not ready: install what is MISSING")
    return status


def gib(size):
    return f"{size / (1 << 30):.1f} GiB"


def runtime_differences(here, there, host):
    """What only this Mac's and only host's list of runtimes names, as `qk doctor` says it."""
    only = lambda side, other: [runtime for runtime in side if runtime not in other]
    return [f"only {name} has {', '.join(runtimes)}" for name, runtimes in
            [(HERE, only(here, there)), (host, only(there, here))] if runtimes]


def toolchains(host, developer):
    """Each Mac's offload_host.toolchain with the Xcode at this Mac's developer directory."""
    return {HERE: offload_host.toolchain(developer), host: survey(host, developer)}


def runtime_images(host, side, developer, clear=False):
    """A Mac's runtime images, the free space where a copy to it is staged, and what an earlier copy
    left staged there, which clear removes: offload_host.images and offload_host.stage on side. Each side
    is given the builds the toolkit pins (runtime_pins), which keep their users there though superseded."""
    if side == HERE:
        return {"images": offload_host.images(developer, runtime_pins()),
                **offload_host.stage(Path.home() / LOCAL_RUNTIME_STAGING, clear)}
    return ask(host, {"images": developer, "staging": RUNTIME_STAGING, "clear": clear, "pinned": runtime_pins()})


# What names runtime builds to keep: each returns {build: where}. The Apple extension adds its disk items' pins.
PINNED_BUILDS = []
# Seconds Git may take to say what kind of object a hash names.
OBJECT_TIMEOUT = 10


def runtime_pins():
    """The runtime builds something pins (PINNED_BUILDS), which offload_host cannot read on the
    second Mac, since it imports nothing but itself there: a superseded one keeps its users."""
    return sorted({build for pinned in PINNED_BUILDS for build in pinned()})


def runtime_lock():
    """An flock that keeps a second runtime copy from starting on this Mac while one runs, or None."""
    directory = Path.home() / LOCAL_RUNTIME_STAGING
    directory.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(directory / ".lock", os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        return None
    return descriptor


def runtime_plan(found, stores, host):
    """A step for each runtime that only one Mac lists: the runtime, its build, the Mac that has it
    (source), the one that lacks it (target), and how the two come to match: a delete from the source
    when no simulator but Xcode's defaults uses it there, so a runtime nobody uses costs neither Mac
    its space (none uses a build its identifier no longer resolves to, offload_host.images), else a
    copy of the source's image to the target when that can be read, else a download there by its
    build, else None, with why."""
    plan = []
    for source, target in ((HERE, host), (host, HERE)):
        for runtime in sorted(set(found[source]["runtimes"]) - set(found[target]["runtimes"])):
            matched = RUNTIME.match(runtime)
            name, build = matched.groups() if matched else (runtime, None)
            step = {"runtime": runtime, "build": build, "source": source, "target": target}
            held = [image for image in stores[target]["images"] if image["build"] == build]
            images = [image for image in stores[source]["images"] if image["build"] == build]
            readable = [image for image in images if image["file"]]
            platform = (name.split() or [""])[0]
            why = images[0]["unreadable"] if images else f"{source} lists no disk image of it"
            if build is None:
                step.update(how=None, why="its name gives no build")
            elif len(images) == 1 and images[0].get("users") == []:
                step.update(how="delete", size=images[0]["sizeBytes"], supersededBy=images[0].get("supersededBy"))
            elif held:
                step.update(how=None, why=f"{target} holds its image ({held[0]['state']}), which the Xcode there does "
                            "not list as available; see `xcrun simctl runtime list` there")
            elif readable:
                step.update(how="copy", image=readable[0], size=readable[0]["sizeBytes"])
            elif platform in DOWNLOADABLE:
                known = images and isinstance(images[0]["sizeBytes"], int)
                step.update(how="download", why=why, platform=platform,
                            size=images[0]["sizeBytes"] if known else RUNTIME_ESTIMATE,
                            architectures=images[0]["architectures"] if images else [])
            else:
                step.update(how=None, why=f"{why}, and xcodebuild downloads no {platform} runtime")
            plan.append(step)
    return plan


def reporter(runtime, size):
    """A copy's progress: a line at each tenth of the image. A requested stop ends the copy."""
    shown = [0]
    def report(done):
        if processes.requested() is not None:
            raise processes.Interrupted(processes.requested(), f"while copying {runtime}")
        if done - shown[0] >= size / 10 or done == size:
            shown[0] = done
            print(f"runtimes: {runtime}: {gib(done)} of {gib(size)} copied", flush=True)
    return report


def send_runtime(host, image, developer, report):
    """Send this Mac's image of a runtime to host, where offload_host.receive stages it and simctl adds
    it. Return None once it is added there, else why not."""
    request = {"receive": {"staging": RUNTIME_STAGING, "build": image["build"], "size": image["sizeBytes"],
                           "developerDir": developer}}
    command = [*SSH, *moved(host), host, login(host_command(Path(offload_host.__file__).read_text(),
                                             json.dumps(request)))]
    failure = None
    with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          env=environment()) as channel:
        try:
            with open(image["file"], "rb") as source:
                sent = 0
                for chunk in iter(lambda: source.read(offload_host.CHUNK), b""):
                    channel.stdin.write(chunk)
                    sent += len(chunk)
                    report(sent)
        except BrokenPipeError:
            pass  # The second Mac stopped reading, and its answer says why.
        except OSError as error:
            failure = f"reading {image['file']} failed: {error}"
        finally:
            # The end of its standard input ends the image there, and a short image is never added.
            with contextlib.suppress(OSError): channel.stdin.close()
        reply = channel.stdout.read().decode(errors="replace")
    if channel.returncode == 255: return f"{host} cannot be reached: {last_line(reply) or 'ssh exited 255'}"
    try:
        result = json.loads(last_line(reply))
    except (TypeError, ValueError):
        result = None
    if not isinstance(result, dict):
        return failure or f"{host} gave no answer (exit {channel.returncode}): {last_line(reply) or 'no output'}"
    return failure or result.get("failed")


def fetch_runtime(host, image, developer, report):
    """Fetch host's image of a runtime over SSH, then stage and add it here as offload_host.receive does
    there. Return None once it is added here, else why not."""
    # cat needs no login shell, whose profile could write into the image.
    command = [*SSH, *moved(host), host, shlex.join(["cat", image["file"]])]
    with subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          env=environment()) as channel:
        try:
            failure = offload_host.receive(channel.stdout, Path.home() / LOCAL_RUNTIME_STAGING, image["build"],
                                         image["sizeBytes"], developer, report)
        finally:
            channel.stdout.close()  # A stop ends cat too.
        error = channel.stderr.read().decode(errors="replace")
    if failure and channel.returncode:
        failure += (f"; {'ssh' if channel.returncode == 255 else 'cat'} exited {channel.returncode}: "
                    f"{last_line(error) or 'no output'}")
    return failure


def download_command(step):
    """xcodebuild's download of step's runtime by its build, for the architectures of its image."""
    architectures = set(step["architectures"])
    variant = ["-architectureVariant", "arm64"] if architectures == {"arm64"} else \
        ["-architectureVariant", "universal"] if {"arm64", "x86_64"} <= architectures else []
    return ["xcodebuild", "-downloadPlatform", step["platform"], "-buildVersion", step["build"], *variant]


def download_runtime(host, step, developer):
    """Download step's runtime with xcodebuild on the Mac that lacks it; return xcodebuild's status."""
    arguments = ["xcrun", *download_command(step)]
    if step["target"] == HERE:
        command, variables = arguments, {**os.environ, "DEVELOPER_DIR": developer}
    else:
        command = [*SSH, *moved(host), host, login(f"DEVELOPER_DIR={shlex.quote(developer)} {shlex.join(arguments)}")]
        variables = environment()
    with subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, errors="replace", env=variables) as running:
        for line in running.stdout:
            print(f"runtimes: {step['target']}: {line.rstrip()}", flush=True)
            if processes.requested() is not None: running.terminate()
    if processes.requested() is not None:
        raise processes.Interrupted(processes.requested(), f"while downloading {step['runtime']}")
    return running.returncode


def match_runtimes(host, check):
    """Bring this Mac and host to the union of the simulator runtimes that the Xcode at this Mac's
    developer directory lists, as `qk doctor` compares them, and return 0 once both list the same.

    A runtime only one Mac lists that no simulator but Xcode's defaults uses there is deleted from
    that Mac (the owner's decision, 2026-11-03). Any other goes to the other Mac as its own disk
    image, over SSH: staged there, and added with `simctl runtime add --move`, so no second copy
    stays. One whose image cannot be read is downloaded there with xcodebuild, by its build. A copy or
    download that would leave less than RUNTIME_MARGIN free where it lands is refused before it
    starts. Then both Macs' runtimes are listed again, and what still differs is named. With
    check, say what it would do and change nothing."""
    developer = developer_directory()
    if developer is None:
        print("runtimes: this Mac has no Xcode selected, so there are no runtimes to compare", flush=True)
        return 1
    lock = None
    if not check:
        lock = runtime_lock()
        if lock is None:
            print("runtimes: another `qk remote setup --runtimes` is running on this Mac; run it again once that "
                  "one ends", flush=True)
            return 1
    try:
        with processes.forwarding_signals():
            return bring_runtimes(host, developer, check)
    except Misconfigured as problem:
        print(f"runtimes: {problem}", flush=True)
        return 1
    finally:
        if lock is not None: os.close(lock)


def bring_runtimes(host, developer, check):
    """match_runtimes' work, once this Mac holds the lock that a change takes."""
    found = toolchains(host, developer)
    version = lambda text: ", ".join((text or "no Xcode").splitlines())
    if not found[HERE]["xcode"]:
        print(f"runtimes: the Xcode at {developer} does not run on this Mac", flush=True)
        return 1
    if found[HERE]["xcode"] != found[host].get("xcode"):
        there = version(found[host]["xcode"]) if found[host].get("xcode") else "missing"
        print(f"runtimes: the Xcode at {developer} is {version(found[HERE]['xcode'])} on this Mac and {there} on "
              f"{host}; install this Mac's Xcode at that path there first (guides/shared-host.md, Setting up the second "
              "Mac)", flush=True)
        return 1
    xcode = f"{version(found[HERE]['xcode'])} at {developer}"
    stores = {side: runtime_images(host, side, developer, clear=not check) for side in (HERE, host)}
    for side in (HERE, host):
        if found[side].get("runtimes") is None or stores[side]["images"] is None:
            print(f"runtimes: simctl did not list the runtimes on {side}", flush=True)
            return 1
        if stores[side]["left"] and not check:
            print(f"runtimes: removed {', '.join(stores[side]['left'])}, which an earlier copy left staged on {side}",
                  flush=True)
    plan = runtime_plan(found, stores, host)
    if not plan:
        runtimes = found[HERE]["runtimes"]
        print(f"runtimes: this Mac and {host} list the same {len(runtimes)} runtimes of {xcode}: "
              f"{', '.join(runtimes) or 'none'}", flush=True)
        return 0
    planned = {HERE: 0, host: 0}
    for step in plan:
        runtime, source, target = step["runtime"], step["source"], step["target"]
        if processes.requested() is not None:
            raise processes.Interrupted(processes.requested(), f"before {runtime} came to {target}")
        if step["how"] is None:
            print(f"runtimes: {runtime}: cannot come to {target}: {step['why']}", flush=True)
            continue
        if step["how"] == "delete":
            heading = f"runtimes: {runtime}, {gib(step['size']) if isinstance(step['size'], int) else 'unknown size'}:"
            unused = "no simulator but Xcode's defaults uses it" + (
                f": its identifier's simulators run {step['supersededBy']}" if step.get("supersededBy") else "")
            if check:
                print(f"{heading} would be deleted from {source}, where {unused}", flush=True)
                continue
            print(f"{heading} deleting from {source}, where {unused}", flush=True)
            failure = offload_host.delete_runtime(developer, step["build"], runtime_pins()) if source == HERE else \
                ask(host, {"deleteRuntime": {"developer": developer, "build": step["build"],
                                             "pinned": runtime_pins()}}).get("problem")
            print(f"runtimes: {runtime}: deleted from {source}" if failure is None else
                  f"runtimes: {runtime}: not deleted from {source}: {failure}", flush=True)
            continue
        size = step["size"]
        if check:
            free = stores[target]["free"] - planned[target]
            planned[target] += size
        else:
            # Measured again before each copy, which those before it have used space for.
            fresh = runtime_images(host, target, developer)
            if any(image["build"] == step["build"] for image in fresh["images"] or []):
                print(f"runtimes: {runtime}: {target} holds it now", flush=True)
                continue
            free = fresh["free"]
        heading = f"runtimes: {runtime}, {gib(size)}:"
        if free - size < RUNTIME_MARGIN:
            print(f"{heading} {'would be refused' if check else 'refused'} on {target}: {gib(free)} is free there, "
                  f"and {gib(size)} more would leave less than the {gib(RUNTIME_MARGIN)} it must keep free; free space "
                  "there, then run it again", flush=True)
            continue
        download = (f"with `{shlex.join(download_command(step))}`, since its image on {source} cannot be copied: "
                    f"{step['why']}") if step["how"] == "download" else None
        if check and download is None:
            print(f"{heading} would be copied from {source} to {target}, which has {gib(free)} free", flush=True)
        elif check:
            print(f"{heading} would be downloaded on {target}, which has {gib(free)} free, {download}", flush=True)
        elif download is None:
            print(f"{heading} copying from {source} to {target}", flush=True)
            failure = (send_runtime if target == host else fetch_runtime)(host, step["image"], developer,
                                                                            reporter(runtime, size))
            print(f"runtimes: {runtime}: added on {target}" if failure is None else
                  f"runtimes: {runtime}: not added on {target}: {failure}; nothing of it stays staged there", flush=True)
        else:
            print(f"{heading} downloading on {target} {download}", flush=True)
            status = download_runtime(host, step, developer)
            print(f"runtimes: {runtime}: {'downloaded' if status == 0 else f'xcodebuild exited {status}'} on {target}",
                  flush=True)
    if check:
        print(f"runtimes: {len(plan)} runtime(s) differ; `qk remote setup --runtimes` or `qk doctor` evens them out",
              flush=True)
        return 1
    # What both Macs list now decides the outcome, not what each step reported.
    found = toolchains(host, developer)
    for side in (HERE, host):
        if found[side].get("runtimes") is None:
            print(f"runtimes: simctl did not list the runtimes again on {side}", flush=True)
            return 1
    here, there = found[HERE]["runtimes"], found[host]["runtimes"]
    if here == there:
        print(f"runtimes: this Mac and {host} list the same {len(here)} runtimes of {xcode}: {', '.join(here)}",
              flush=True)
        return 0
    print(f"runtimes: the runtimes of {xcode} still differ: {'; '.join(runtime_differences(here, there, host))}",
          flush=True)
    return 1


def first(root, config, lane, arguments, expect, fallback=True, queue_gate=False, exclude=None):
    """Offer a lane to the second Mac to start now, once (Offer.make), and return the Offer.

    With fallback, the offer first reads both Macs' capacity for the lane (Offer.choose): a whole-host
    lane needs every guaranteed slot of its Mac, an exclusive one every slot. The second Mac runs the
    lane only if it can start it now: its queue admits the lane within FREE_WAIT (`qk run --if-free`
    there) and one of the project's checkouts there is free. A tree with uncommitted changes goes as a
    snapshot commit (execute), and so does the toolkit's. The second Mac cannot run a lane whose
    arguments name a physical device of this Mac's record, nor any lane when Git cannot name the
    commits to send. With queue_gate, for a queue worker's own `qk run` of a gate's lane, a process that lost Local
    Network access ends with LOST_ACCESS rather than the work running on this Mac (Offer.lost_access).
    A configured gate pool tries the listed aliases in order, excluding the active batch's host when stacking.
    """
    work = ["run", "--here", "--if-free", *(["--expect", str(expect)] if expect else []), lane,
            *(["--", *arguments] if arguments else [])]
    # A run that names a physical device uses it here, or there only by `qk remote run --device`.
    named = extensions.DEVICES.reserved_devices(arguments)
    refused = (f"its arguments name the physical device of {', '.join(named)}, which the second Mac uses only under "
               "a reservation `qk remote run --device` carries from here") if named else None
    settings = config.get("lanes", {}).get(lane, {})
    hosts = gate_hosts() if queue_gate else None
    offered = Offer(lane, f"`qk run --here {lane}`", lambda host, sent: execute(
        root, config, host, work, lane=lane, arguments=arguments, expect=expect, decline=True, sent=sent), refused,
        whole_host=bool(settings.get("wholeHost")), exclusive=settings.get("resource") == "exclusive",
        screen=unlocked_screen(config.get("lanes", {}), lane), queue_gate=queue_gate and (fallback or hosts is not None),
        hosts=None if hosts is None else tuple(host for host in hosts if host != exclude))
    offered.make(fallback)
    return offered


# A simulator's identifier, a UUID, as a word of uppercased text: a member of this Mac's pool.
SIMULATOR = re.compile(r"[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}")
# A destination's id= key, as `-destination id=UUID` or `platform=iOS Simulator,id=UUID` gives it, at the
# start of a word or after a comma or `=` of the destination: not the end of another key, such as --build-id=.
DESTINATION = re.compile(rf"(?:\A|[,=])ID=({SIMULATOR.pattern})(?=\Z|,)")
# What ends one command of a shell's words, after which a UUID no longer follows simctl.
SEPARATOR = re.compile(r"[;&|\n()]+")


def simulators(command):
    """The simulator identifiers command names, uppercased: a UUID where a simulator goes, as a destination's
    id=, any word after `simctl` in the same shell command, or --device's argument. One inside a path, such
    as an interpreter in a session's scratchpad (#116, #142), or in another word or argument names none."""
    found = set()
    for part in SEPARATOR.split(" ".join(map(str, command)).upper()):
        words = [word.strip("'\"") for word in part.split()]
        simctl = False
        for previous, word in zip([None, *words], words):
            found.update(DESTINATION.findall(word))
            value = word.removeprefix("--DEVICE=") if previous != "--DEVICE" else word
            if SIMULATOR.fullmatch(value) and (simctl or previous == "--DEVICE" or value != word):
                found.add(value)
            simctl = simctl or word == "SIMCTL" or word.endswith("/SIMCTL")
    return found


# Where the system tools a command starts or uses as an interpreter live, the same on both Macs: a path under one of
# these names no file of this Mac's alone (local_paths). /dev too, for /dev/null in a shell's redirection.
# A root itself, spelled without its slash as PATH=/usr/bin:/bin spells it, is shared too (#379), though /binary is not.
SHARED_PATHS = ("/usr/", "/bin/", "/sbin/", "/dev/", "/opt/homebrew/", "/Library/Developer/")
SHARED_ROOTS = tuple(path.rstrip("/") for path in SHARED_PATHS)
SHARED_PATTERNS = (re.compile(r"/Applications/Xcode[^/]*\.app(/|$)"),)
# What separates paths within one word of a command: a shell's words in `sh -c '…'`, an option's value after = or :.
PATH_SEPARATOR = re.compile(r"""[\s'"=:;&|()<>]+""")


def local_paths(command, top):
    """Why the second Mac cannot run command as it runs here because a word names a path of this Mac's alone, or
    None: an absolute path outside the checkout, such as a session's scratchpad, /private/tmp, /tmp or the toolkit's
    checkout, or one under ~, none of which the second Mac has. A path in the checkout says this checkout's path. The
    system tools a command starts or uses as an interpreter are on both Macs: a path under SHARED_PATHS or Xcode, and
    one that is what PATH resolves its name to."""
    for word in map(str, command):
        for part in PATH_SEPARATOR.split(word):
            if part.startswith("~"):
                return f"it names {part}, under this Mac's home, which the second Mac's checkout does not hold"
            # A URL's //host/path, split at its scheme's colon, is no path.
            if not part.startswith("/") or part == "/" or part.startswith("//"): continue
            if part.startswith(SHARED_PATHS) or part in SHARED_ROOTS: continue
            if any(pattern.match(part) for pattern in SHARED_PATTERNS): continue
            found = shutil.which(Path(part).name)
            if found and os.path.realpath(found) == os.path.realpath(part): continue
            resolved = Path(os.path.realpath(part))
            if resolved == top or top in resolved.parents:
                return f"it names this checkout's path {part}, which the second Mac's checkout is not"
            return f"it names {part}, a path outside the checkout, which the second Mac does not have"
    return None


# What a shell script writes rather than reads: the target of an output redirection (>, >>, 2>, &>, >|, attached or
# not; 2>&1 names no file), and the files tee writes, with its options, up to the next operator.
OUTPUT_REDIRECTION = re.compile(r"""(?:\d+|&)?>>?[|&]?\s*[^\s;&|()<>'"]*""")
TEE_OUTPUTS = re.compile(r"""(?:^|(?<=[\s|;&(]))tee(?:\s+[^\s;&|()<>]+)*""")


def ignored_inputs(command, top, directory):
    """Why the second Mac cannot run command as it runs here because a word names, relative to directory, a file
    in the checkout that exists now and Git ignores, such as a session's scratch script under .build (#390), or
    None. The commit sent leaves out every file Git ignores. A directory keeps nothing here: one Git ignores is most
    often build output the command makes there itself, such as DerivedData or a scratch path. A tracked file Git
    would otherwise ignore is sent, and so goes there. A script's output targets, after a redirection or tee, are
    no input: a log under .build that an earlier run wrote keeps nothing here; a < redirection's file is input."""
    top, directory, candidates = Path(top), Path(directory), {}
    for word in map(str, command):
        word = TEE_OUTPUTS.sub(" ", OUTPUT_REDIRECTION.sub(" ", word))
        for part in PATH_SEPARATOR.split(word):
            # An absolute path or one under ~ is local_paths'; an option is no path.
            if not part or part.startswith(("/", "~", "-")): continue
            path = Path(os.path.normpath(directory / part))
            try:
                relative = path.relative_to(top).as_posix()
            except ValueError:
                continue
            try:
                if relative != "." and path.is_file(): candidates.setdefault(relative, part)
            except OSError:
                continue
    if not candidates: return None
    found = subprocess.run(["git", "-C", str(top), "check-ignore", "-z", "--stdin"], env=environment(),
                           input="\0".join(candidates) + "\0", capture_output=True, text=True)
    ignored = [name for name in found.stdout.split("\0") if name in candidates] if found.returncode == 0 else []
    if not ignored: return None
    return (f"it names {candidates[ignored[0]]}, a file Git ignores, which the commit sent to the second Mac lacks; "
            "a command that runs or reads one runs here")


def local_environment(environment=None):
    """Why the second Mac cannot run a command as it runs here because of this Mac's environment, or None. Its login
    shell has none of this Mac's variables: an xcodebuild TEST_RUNNER_ variable would not reach the tests there.
    DEVELOPER_DIR goes with every dispatch, and the second Mac refuses the work as misconfigured when its Xcode
    there differs, which only an ssh call could compare beforehand; a command that selects one stays here."""
    environment = os.environ if environment is None else environment
    runner = sorted(key for key in environment if key.startswith("TEST_RUNNER_"))
    if runner:
        return f"its environment sets {', '.join(runner)}, which the second Mac's login shell does not have"
    if environment.get("DEVELOPER_DIR"):
        return (f"its environment selects DEVELOPER_DIR={environment['DEVELOPER_DIR']}, which the second Mac's Xcode "
                "may not match")
    return None


def first_command(root, config, command, kind):
    """Offer `qk admit`'s command to the second Mac to start now, once, as first() offers a lane,
    to run in the directory of its checkout that matches the current one here; return the Offer. The
    second Mac cannot run it as it runs here when its directory is outside the checkout or one Git
    ignores, which the commit sent lacks, or when its words name this checkout's path, a simulator by
    its identifier, or a hardware UDID (devices.named), all of which are this Mac's, a path of this Mac's
    alone (local_paths) or a file Git ignores (ignored_inputs), or when this Mac's environment sets TEST_RUNNER_ variables or DEVELOPER_DIR
    (local_environment); a device goes
    to the second Mac only under a reservation `qk remote admit --device` carries. kind is
    --exclusive or --model, if either, and --time-limit when the command has a limit of its own."""
    top, directory, words = Path(root).resolve(), Path.cwd().resolve(), " ".join(command)
    try:
        relative = directory.relative_to(top).as_posix()
    except ValueError:
        relative = None
    refused, named = None, sorted(extensions.DEVICES.named_devices(command, top))
    if relative is None:
        refused = f"its directory {directory} is outside the checkout {top}"
    elif relative != "." and subprocess.run(["git", "-C", str(top), "check-ignore", "-q", relative], env=environment(),
                                            capture_output=True).returncode == 0:
        refused = f"its directory {relative} is one Git ignores, which the second Mac's checkout lacks"
    elif str(top) in words:
        refused = f"it names this checkout's path {top}, which the second Mac's checkout is not"
    elif simulators(command):
        refused = "it names a simulator by its identifier, a member of this Mac's pool"
    elif named:
        refused = (f"it names the hardware UDID {', '.join(named)}, which the second Mac uses only under a "
                   "reservation `qk remote admit --device` carries from here")
    else:
        refused = local_paths(command, top) or ignored_inputs(command, top, directory) or local_environment()
    # The second Mac's admission takes it now or declines it, as `qk run --if-free` does a lane. There it runs
    # where it was sent, since it runs for the Mac that sent it (DISPATCHER).
    work = ["admit", *kind, "--if-free", "--", *command]

    def send(host, sent):
        # What the command changed in the checkout there comes back and is applied here (brought_back).
        status = execute(root, config, host, work, decline=True,
                         subdirectory=None if relative in (None, ".") else relative, sent=sent, changes=True)
        return brought_back(root, sent, status)
    offered = Offer("the command", "`qk admit --here`", send, refused, exclusive="--exclusive" in kind)
    offered.make()
    return offered


class Offer:
    """Work offered to the second Mac to start now, which it runs or declines before anything runs.

    send(host, sent) runs the work there (execute), keeping in sent what it sent, so that the work
    can be offered again without a second snapshot or push. refused is why the second Mac cannot run
    the work, if it cannot, and instead the command that runs it here when the owner so decides after a
    configuration problem stopped it. whole_host says the work needs every guaranteed slot of its Mac,
    exclusive every slot, and screen an unlocked console screen (spare). status, once made, is the work's status there, MISCONFIGURED or
    BUSY, or why the work runs on this Mac instead; busy is why the second Mac did not take it, as the
    receipt gives it, while it has not: declined as busy, or not offered while it has no spare capacity
    and this Mac has (Loaded); and None once it ran there or can no longer take it."""

    def __init__(self, name, instead, send, refused=None, whole_host=False, exclusive=False, screen=False,
                 queue_gate=False, hosts=None):
        self.started = resources.request_started()
        self.name, self.instead, self.send, self.refused = name, instead, send, refused
        self.whole_host, self.exclusive, self.screen, self.queue_gate = whole_host, exclusive, screen, queue_gate
        self.sent, self.host, self.status, self.busy, self.away = {}, None, None, None, None
        self.hosts = hosts
        # The offers again() made in a row that could not reach the second Mac, and when the last of them failed
        # (an epoch time): away is set only once there are UNREACHABLE_AFTER (vocaro/queuekeeper#25).
        self.failed, self.failed_at = 0, None
        self.remote_macs = "the build Macs" if hosts is not None else "the second Mac"
        # Whether each offer first reads both Macs' capacity (choose): an offer this Mac may take does, and
        # --offload-only's, which never runs the work here, does not. Set by make.
        self.choosing = False
        self.neither = None
        # This Mac's queue place of the work while it waits here (resources.Place), set by
        # its admission here: this Mac's capacity reading does not count it as a request queued ahead (#186).
        self.place = None

    def attempt(self):
        """Offer the work once: its status there, or Busy, Unreachable or Unavailable raised; Loaded, a Busy,
        when the second Mac has no spare capacity for it while this Mac has, with nothing offered."""
        if self.refused is not None: raise Unavailable(self.refused)
        with processes.forwarding_signals(), resources.request_order(self.started):
            if self.hosts is not None: return self.attempt_pool()
            # A second Mac that cannot be reached costs seconds here, and nothing is sent to it.
            reachable(self.host)
            # Reached again, it is no longer said to be unreachable, with its failed probes, while the offer goes on.
            if self.away is not None and self.busy is not None: lane_wait.waiting(lane_wait.OVERFLOW, self.busy)
            if self.choosing:
                reason = self.choose()
                if reason is not None: raise Loaded(reason)
            return self.send(self.host, self.sent)

    def attempt_pool(self):
        """Offer to each eligible Mac in order until one takes it. Keep prepared commits per destination."""
        refused = []
        for host in self.hosts:
            try:
                reachable(host)
                options = {"started": self.sent[host]["requestStarted"]} if "requestStarted" in self.sent.get(host, {}) else {}
                reason = spare(capacity_there(host, **options), host, self.whole_host, self.exclusive, self.screen, extra=True)
            except (NoLocalNetwork, Misconfigured):
                raise
            except Unavailable as error:
                refused.append(getattr(error, "problem", str(error)))
                continue
            if reason is not None:
                refused.append(reason)
                continue
            try:
                return self.send(host, self.sent.setdefault(host, {}))
            except (NoLocalNetwork, Misconfigured):
                raise
            except (Busy, Unreachable) as error:
                refused.append(error.problem if isinstance(error, Unreachable) else str(error))
        raise Loaded("; ".join(refused))

    def choose(self):
        """Why the work goes to this Mac's queue now rather than being offered, by both Macs' capacity for it
        (spare), or None to offer it: the second Mac has spare capacity, or neither Mac has, and then the
        work goes to whichever admits it first, the second Mac asked first, as before the choice existed. Both
        Macs are for work: a session uses the one with capacity and never asks the owner which
        (vocaro-engineering#168)."""
        options = {"started": self.sent["requestStarted"]} if "requestStarted" in self.sent else {}
        there = spare(capacity_there(self.host, **options), "the second Mac", self.whole_host, self.exclusive, self.screen)
        if there is None: return None
        reading = offload_host.capacity(own=self.place.identity if self.place is not None else None, before=self.started)
        here = (f"this Mac's capacity cannot be read: {reading['error']}" if reading.get("error")
                else spare(reading, HERE, self.whole_host, self.exclusive, self.screen))
        if here is None: return there
        if self.neither != (there, here):
            print(f"queuekeeper: neither Mac has spare capacity for {self.name} now ({there}; {here}), so it goes to "
                  "whichever admits it first, the second Mac asked first", file=sys.stderr, flush=True)
            self.neither = there, here
        return None

    def declined(self, busy):
        """Why the second Mac did not take the work, as the receipt and messages give it."""
        return str(busy) if isinstance(busy, Loaded) else f"the second Mac is busy: {busy}"

    def make(self, fallback=True):
        """Offer the work once and set status. Without fallback, for a caller that runs the work itself
        when the second Mac does not, every reason that it does not is BUSY, after saying why; the capacity
        of the Macs is not read for it, since the work never runs here (--offload-only)."""
        self.host = configured() if self.hosts is None else next(iter(self.hosts), None)
        self.choosing = fallback
        if self.host is None:
            if self.hosts is not None:
                self.status = "no other build Mac is eligible" if fallback else BUSY
                return self.status
            if not fallback:
                print(f"queuekeeper: {self.name} not run: no second Mac is configured ({HOST_VARIABLE}, or the ssh alias "
                      f"{' or '.join(ALIASES)} in ~/.ssh/config)", file=sys.stderr, flush=True)
            self.status = "no second Mac is configured" if fallback else BUSY
            return self.status
        try:
            self.status = self.attempt()
        except Misconfigured as stop:
            self.stopped(stop)
        except Busy as busy:
            # This Mac takes work the second Mac is too busy to start now, when it can (overflow).
            self.busy = said = self.declined(busy)
            print(f"queuekeeper: {f'trying {self.name} on this Mac' if fallback else f'{self.name} not run'}: {said}",
                  file=sys.stderr, flush=True)
            self.status = said if fallback else BUSY
        except Unavailable as reason:
            if self.lost_access(reason): return self.status
            said = reason.problem if isinstance(reason, Unreachable) else str(reason)
            print(f"queuekeeper: {f'running {self.name} on this Mac' if fallback else f'{self.name} not run'}: {said}",
                  file=sys.stderr, flush=True)
            self.status = said if fallback else BUSY
        return self.status

    def lost_access(self, reason):
        """Whether reason, why the second Mac did not take the work, ends a queue worker's gate: this process has lost
        Local Network access, as it does once the session that started the worker ends, so the second Mac need not be
        away, and every lane of the gate would run on this Mac (vocaro-engineering#300). Status is then LOST_ACCESS,
        and nothing runs here. Any other `qk run` keeps its fallback, and so does a second Mac that is only away."""
        if not self.queue_gate or not isinstance(reason, NoLocalNetwork): return False
        print(f"queuekeeper: {self.name} not run, here or there: {reason.problem}\nqueuekeeper: a queue worker's gate gives no verdict "
              f"for this, and the worker exits for a live session to start another (exit {LOST_ACCESS})",
              file=sys.stderr, flush=True)
        self.busy, self.status = None, LOST_ACCESS
        return True

    def stopped(self, stop):
        print(f"queuekeeper: {self.name} not run: {stop.problem}\nqueuekeeper: to fix it, {stop.fix}\nqueuekeeper: a misconfigured shared host "
              f"stops the work that depends on it (the toolkit's agents/core.md, Shared resources). Running {self.name} on this Mac "
              f"instead takes the owner's decision, then {self.instead}.", file=sys.stderr, flush=True)
        self.busy, self.status = None, MISCONFIGURED
        return MISCONFIGURED

    def again(self):
        """Offer the work to the second Mac once more, while it was busy: its status when it ran there,
        MISCONFIGURED when the second Mac's configuration stopped it, or None while the second Mac is
        still busy or cannot be reached, and from then on when it can no longer take the work. A second
        Mac that cannot be reached is asked again within reprobe() seconds, since one slow answer, or one
        that went offline during a long wait, may come back before it ends. It counts as unreachable only
        after UNREACHABLE_AFTER offers in a row could not reach it: one that times out once stays in
        rotation, as the busy Mac it was (vocaro/queuekeeper#25). away says why it could not be reached,
        while it counts as unreachable. LOST_ACCESS when a queue worker's gate lost Local Network access
        meanwhile (lost_access)."""
        if self.busy is None: return None
        try:
            self.status = self.attempt()
            self.busy = self.away = None
            self.failed, self.failed_at = 0, None
            return self.status
        except Misconfigured as stop:
            return self.stopped(stop)
        except Busy as busy:
            self.busy, self.away = self.declined(busy), None
            self.failed, self.failed_at = 0, None
        except Unreachable as away:
            if self.lost_access(away): return self.status
            self.failed, self.failed_at = self.failed + 1, time.time()
            if self.failed < UNREACHABLE_AFTER:
                print(f"queuekeeper: {self.name} asks the second Mac again within {self.reprobe()}s, as one offer that "
                      f"could not reach it does not make it unreachable: {away.problem}", file=sys.stderr, flush=True)
                return None
            if away.problem != self.away:
                print(f"queuekeeper: {self.name} waits for this Mac, and asks the second Mac again within {self.reprobe()}s, "
                      f"after {self.failed} offers in a row could not reach it: {away.problem}", file=sys.stderr, flush=True)
            self.away = self.status = away.problem
        except Unavailable as reason:
            said = str(reason)
            print(f"queuekeeper: {self.name} waits for this Mac alone: {said}", file=sys.stderr, flush=True)
            lane_wait.waiting(lane_wait.ALONE, said)
            self.busy, self.status = None, said
        return None

    def reprobe(self):
        """Seconds at most until the second Mac is asked again after failed offers in a row that could not reach it:
        REPROBE_WAIT after the first, doubling to REPROBE_WAIT_MAX."""
        return min(REPROBE_WAIT * 2 ** max(self.failed - 1, 0), REPROBE_WAIT_MAX)

    def probes(self):
        """The failed offers in a row and when the last failed, for the wait record (lane_wait.waiting)."""
        return {"failed": self.failed, "last": self.failed_at} if self.failed else None


# Seconds this Mac's admission waits between offers to the second Mac while both Macs are busy: at
# first, after its FREE_WAIT, and at most. The wait doubles from one offer to the next.
OVERFLOW_WAIT, OVERFLOW_WAIT_MAX = 15, 120
# Seconds at most until the second Mac is asked again after an offer could not reach it: at first, and after more
# offers in a row that could not. An offer that cannot reach it costs it nothing, since nothing is sent; one ssh
# probe that did not finish in 30 s while `ssh build-host true` answered in 0.2 s from a shell on the same Mac kept
# the second Mac out of rotation for about six minutes (2026-11-12, vocaro/queuekeeper#25).
REPROBE_WAIT, REPROBE_WAIT_MAX = 30, 60
# Offers in a row that could not reach the second Mac before it counts as unreachable.
UNREACHABLE_AFTER = 2
# Seconds `qk run` and `qk admit` wait in all for one Mac or the other to admit the work.
OVERFLOW_TIMEOUT = 5400


def overflow(offered, admit, timeout=OVERFLOW_TIMEOUT, once=False, refused=()):
    """This Mac's admission of work the second Mac declined as busy, the work's status when the second Mac
    took it after all or its configuration stopped it, or BUSY when neither Mac admitted it in timeout
    seconds, after saying so.

    admit(seconds) is this Mac's admission of the work, or None when none came in that time. The work
    comes here once the second Mac has not taken it, declined as busy or not offered while it had no
    spare capacity and this Mac had (Offer.choose): this Mac's admission waits FREE_WAIT, as the second
    Mac's did, then, while both Macs keep it waiting, OVERFLOW_WAIT doubling to OVERFLOW_WAIT_MAX, and
    between those waits both Macs are read again and the second Mac is offered the work again when the
    reading allows (Offer.again). The wait in this Mac's queue is the backoff between offers, so the
    work starts on whichever Mac admits it first, and the second Mac is asked again at least every
    OVERFLOW_WAIT_MAX seconds: each offer costs it a checkout claim, the project's provision lane and
    a policy refresh, so not every few seconds. After an offer that could not reach it, which cost it
    nothing, it is asked again within Offer.reprobe()'s seconds instead. Each of this Mac's waits that ends unadmitted is logged
    as withdrawn, as a declined offer is there. All kinds keep their arrival across local waits
    (resources.Place) and remote retries (elapsed age in the dispatch plan). A retained place holds
    later work back only for HEAVY_PLACE_SECONDS after its last local attempt (#360), so during each offer
    the place is kept holding while the lane's record says that the second Mac has not started it
    (Place.kept, lane_wait.taken_there): an offer can take longer than that, and a later whole-host gate
    then took the slots a waiting one had arrived first for (vocaro/queuekeeper#160). With once, as
    for --if-free, each Mac is asked once. refused holds why each of this Mac's waits ended unadmitted,
    the last of which the give-up line says (vocaro-engineering#270)."""
    deadline, window, said = time.monotonic() + timeout, FREE_WAIT, None
    while True:
        # Once the second Mac can no longer take it, this Mac's queue alone, for the rest of the time.
        bound = window if offered.busy is not None else deadline - time.monotonic()
        # An offer that could not reach the second Mac is followed soon by another (Offer.reprobe).
        if offered.busy is not None and offered.failed: bound = min(bound, offered.reprobe())
        if offered.away is not None:
            lane_wait.waiting(lane_wait.UNREACHABLE, offered.away, probes=offered.probes())
        elif offered.busy is not None: lane_wait.waiting(lane_wait.OVERFLOW, offered.busy)
        admission = admit(max(min(bound, deadline - time.monotonic()), 0))
        if admission is not None: return admission
        if once or time.monotonic() >= deadline:
            who = "no Mac" if offered.hosts is not None else "neither Mac"
            print(f"queuekeeper: {offered.name} not run: {who} admitted it {'now' if once else f'in {timeout:g}s'}"
                  + (f"; the second Mac cannot be reached: {offered.away}" if offered.away
                     else f"; {offered.busy}" if offered.busy else "")
                  + (f"; this Mac's last wait: {refused[-1]}" if refused else ""), file=sys.stderr, flush=True)
            return BUSY
        if offered.busy is not None and offered.away is None and offered.busy != said:
            print(f"queuekeeper: {offered.name} waits for whichever Mac admits it first, {offered.remote_macs} asked again every "
                  f"{OVERFLOW_WAIT_MAX}s or sooner: {offered.busy}, and this Mac has not admitted it",
                  file=sys.stderr, flush=True)
            said = offered.busy
        place = getattr(offered, "place", None)
        with place.kept(lambda: lane_wait.taken_there() is False) if place is not None else contextlib.nullcontext():
            status = offered.again()
        if status is not None: return status
        window = min(max(2 * window, OVERFLOW_WAIT), OVERFLOW_WAIT_MAX)


def ask(host, request, timeout=SURVEY_TIMEOUT, unsent=False):
    """offload_host.py's answer to request on the second Mac, run by its login shell as a run there is. A second
    Mac ssh cannot reach raises Misconfigured, which qk doctor and the merge queue's wait report; with unsent,
    for a question asked before work is sent (capacity_there), it raises as execute's preparing connection does
    (ssh_failure): Unreachable, so that the work runs on this Mac, unless the second Mac refused the login."""
    request = host_command(Path(offload_host.__file__).read_text(), json.dumps(request))
    attempt = asleep()
    failed = lambda said, *context: ssh_failure(host, said, *context) if unsent else unreachable(host, said, *context)
    try:
        found = ssh(host, request, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise failed(f"it did not answer in {timeout}s", attempt) from None
    except OSError as error:
        raise failed(str(error)) from None
    if found.returncode == 255: raise failed(found.stderr, attempt)
    try:
        return json.loads(last_line(found.stdout))
    except (TypeError, ValueError):
        raise no_toolkit(host, found.returncode, last_line(found.stderr)) from None


def survey(host, developer):
    """The second Mac's offload_host.toolchain, read in its login shell as a run there reads it."""
    return ask(host, {"toolchain": developer})


def screen_there(host):
    """The second Mac's console screen, as offload_host.screen reads it there, for `qk host screen --remote`. A
    second Mac that cannot be reached raises Misconfigured or Unreachable, as ask does."""
    return ask(host, {"screen": True}, timeout=CENSUS_TIMEOUT)


def recover_admission(host, owner=None):
    """Run the remote checkout's ordinary recovery in its login shell, without assuming sshd's PATH.
    No owner lists only; an owner goes through exactly the same census and refusal checks as a local call.
    A timeout leaves the outcome unknown: callers should list again before deciding what to do next."""
    launcher = '"$HOME"/' + shlex.quote(offload_host.TOOLKIT_CHECKOUT + "/bin/qk")
    script = f"python3 {launcher} admissions recover"
    if owner is not None: script += " " + shlex.quote(str(owner))
    found = ssh(host, script, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=CENSUS_TIMEOUT)
    if found.stdout: print(found.stdout, end="")
    if found.stderr: print(found.stderr, end="", file=sys.stderr)
    return found.returncode


def capacity_there(host, *, diagnostic=False, started=None):
    """The second Mac's capacity for heavy work, as offload_host.capacity reads it there, in its login shell as a
    run there reads it. A second Mac that cannot be reached raises Unreachable, as the connection that prepares
    a run does, and one that does not answer with a reading Unavailable: the work then runs on this Mac, saying so. One whose admission limits cannot
    be read refuses every request there, which is its owner's to fix (Misconfigured)."""
    request = {"capacity": "diagnostic" if diagnostic else True}
    if started is not None:
        request["requestStarted"] = started
    elif resources._request_started.get() is not None:
        request["requestAge"] = max(0, time.time() - resources.request_started())
    answered = ask(host, request, timeout=CENSUS_TIMEOUT, unsent=True)
    if isinstance(answered, dict) and answered.get("error"):
        raise Misconfigured(f"{host} could not read its admission limits: {answered['error']}",
                            f"correct the limits file or variable it names on {host} (guides/shared-host.md, "
                            "Heavy-work admission)")
    if not isinstance(answered, dict) or not all(isinstance(answered.get(key), int) for key in ("slots", "slotsMax")) \
            or not isinstance(answered.get("busy"), list) or not isinstance(answered.get("busyLimit"), (int, float)):
        raise Unavailable(f"{host} did not report its capacity: it answered {answered!r}")
    return answered


def admission_log_there(host, since):
    """The second Mac's admission log entries written at or after since, a datetime, as resources.read_log reads
    this Mac's, for `qk admissions usage`. A second Mac that cannot be reached or does not answer with its lines
    raises Unavailable, as capacity_there does."""
    answered = ask(host, {"admissionLog": since.timestamp()}, timeout=CENSUS_TIMEOUT, unsent=True)
    if not isinstance(answered, dict) or not isinstance(answered.get("lines"), list):
        raise Unavailable(f"{host} did not send its admission log: it answered {str(answered)[:200]!r}")
    return [entry for entry in map(resources.read_entry, answered["lines"]) if entry is not None]


# Seconds `qk admissions now` gives the second Mac in all, connection included, and the most bytes it reads back.
ADMISSIONS_BUDGET, ADMISSIONS_BYTES = 10, 1 << 20


def admissions_there(host, budget=ADMISSIONS_BUDGET):
    """Who holds and who waits for admission on the second Mac now, as offload_host.admissions_now reads it there, for
    `qk admissions now` (vocaro-engineering#342). One noninteractive connection, bounded in all by budget seconds: no
    search for a renamed Mac (ssh's rediscover), which could take longer, and nothing changed there. A second Mac that
    cannot be reached in time raises Unreachable, and one whose answer is no reading Unavailable."""
    script = host_command(Path(offload_host.__file__).read_text(),
                         json.dumps({"admissionsNow": offload_host.NOW_SCHEMA}))
    seconds = max(1, int(budget))
    options = [f"ConnectTimeout={seconds}" if option.startswith("ConnectTimeout=") else option for option in SSH]
    try:
        found = subprocess.run([*options, *moved(host), host, login(script)], env=environment(), stdin=subprocess.DEVNULL,
                               capture_output=True, text=True, timeout=budget)
    except subprocess.TimeoutExpired:
        raise Unreachable(f"{host} did not answer in {budget:g}s", "read it again once it is awake and on the network") from None
    except OSError as error:
        raise Unreachable(f"ssh to {host} could not start: {error}", "check ssh on this Mac") from None
    if found.returncode == 255:
        raise Unreachable(f"ssh could not reach {host}: {last_line(found.stderr) or 'it said nothing'}",
                          "check that it is awake and on the network (guides/shared-host.md, Second Mac)")
    if len(found.stdout) > ADMISSIONS_BYTES:
        raise Unavailable(f"{host} sent {len(found.stdout)} bytes, more than a reading's {ADMISSIONS_BYTES}")
    try: answered = json.loads(last_line(found.stdout) or "")
    except (TypeError, ValueError): answered = None
    if not (isinstance(answered, dict) and answered.get("schemaVersion") == offload_host.NOW_SCHEMA
            and answered.get("state") in ("observed", "partial", "unknown") and isinstance(answered.get("reasons"), list)):
        said = (answered or {}).get("error") if isinstance(answered, dict) else None
        raise Unavailable(f"{host} did not report its admissions: "
                          + (said or f"it answered {(last_line(found.stdout) or last_line(found.stderr) or 'nothing')[:200]!r}"))
    return answered


def screen_refusal(found, mac):
    """Why a Mac whose screen queuekeeper read as found (offload_host.screen) cannot take a lane that needs an unlocked
    screen, or None when its screen is unlocked. A reading that is missing or unknown refuses too: queuekeeper never
    guesses that a screen is unlocked (vocaro-engineering#185)."""
    if isinstance(found, dict) and found.get("state") == "unlocked": return None
    if not isinstance(found, dict) or found.get("state") not in ("locked", "unknown"):
        return f"the lane needs an unlocked screen, and {mac} did not report its screen"
    return f"the lane needs an unlocked screen, and {mac}'s is {found['state']} ({offload_host.screen_line(found)})"


def spare(reading, mac, whole_host=False, exclusive=False, screen=False, *, extra=False):
    """Why mac, read as reading (offload_host.capacity), has no spare capacity now for the work, or None while
    it has (vocaro-engineering#168). The work needs a guaranteed slot free there, every guaranteed slot for a
    whole-host lane and every slot up to the maximum for exclusive work, no whole-host lane or exclusive work
    running there, and no exclusive work queued ahead there (queuedExclusive), behind which admission holds every
    later request (vocaro-engineering#191). A free guaranteed slot is spare capacity however busy the Mac's CPU
    is, as admission admits work into one: the owner wants the second Mac loaded as much as possible, never idle
    beside a loaded Mac (owner's decision, 2026-11-07). While the
    Mac's admission keeps the guaranteed slots for a whole-host lane queued ahead (kept), a whole-host or
    exclusive lane waits its turn behind it and other work needs a slot beyond them free, as admission rules
    (vocaro-engineering#174, #186), and that slot only as admission takes one: while the CPU busy share is under
    the Mac's own HEAVY_BUSY_LIMIT (vocaro-engineering#200), once the Mac has settled for
    HEAVY_SETTLE_SECONDS since its last admission (sinceAdmission, settle) and while its memory pressure is
    normal (memory), as resources.Admission._extra_refusal rules (vocaro-engineering#201). The reason names the
    Mac and, for its CPU, the busy share read and the limit, and the request queued ahead; the receipt's hereBecause
    carries it. With screen, for a lane
    that needs an unlocked screen, a Mac whose screen is locked or unknown has none (screen_refusal).
    With extra, a pool may also offer ordinary work to a Mac with an eligible slot beyond the guaranteed ones."""
    if screen:
        refused = screen_refusal(reading.get("screen"), mac)
        if refused is not None: return refused
    slots, maximum, busy = reading["slots"], reading["slotsMax"], sorted(reading["busy"])
    # Each limit quoted with whose it is and where that Mac sets it; an older toolkit's reading says not where.
    said = lambda key, value: limit_words(reading, key, value, mac)
    running, holding = reading.get("wholeHost") or [], reading.get("exclusive") or []
    if running:
        return f"{mac} is running a whole-host lane: {running[0]}"
    if holding:
        return f"exclusive work holds {mac}: {holding[0]}"
    # An older toolkit's reading has no queuedExclusive, and holds nothing back by it.
    queued = reading.get("queuedExclusive")
    if isinstance(queued, str) and queued:
        return f"exclusive work is queued ahead on {mac}: {queued}"
    if exclusive and busy:
        return (f"exclusive work needs every slot of {mac} up to {said('slotsMax', maximum)} free, and {len(busy)} "
                f"{'is' if len(busy) == 1 else 'are'} busy")
    # An older toolkit's reading has no kept, and keeps nothing by it.
    kept = reading.get("kept") if isinstance(reading.get("kept"), str) and reading.get("kept") else None
    held = None
    if kept is not None:
        held = f"the guaranteed slots of {mac} are kept for a whole-host lane queued ahead: {kept}"
        if exclusive or whole_host:
            return f"{held}, which the lane would wait behind"
        if len([slot for slot in busy if slot >= slots]) >= maximum - slots:
            return f"{held}, and every slot beyond them up to {said('slotsMax', maximum)} is busy"
    taken = [slot for slot in busy if slot < slots]
    if whole_host and taken:
        return (f"a whole-host lane needs every guaranteed slot of {mac} (HEAVY_SLOTS={slots}) free, and {len(taken)} "
                f"{'is' if len(taken) == 1 else 'are'} busy")
    if not exclusive and not whole_host and kept is None and len(taken) >= slots:
        held = f"every guaranteed slot of {mac} is busy (HEAVY_SLOTS={slots})"
        if not extra or len(busy) >= maximum: return held
    # A guaranteed slot is admitted whatever the CPU and memory read, so they bound only a slot beyond them.
    if held is None:
        return None
    # An older toolkit's reading has no sinceAdmission or settle, and withholds nothing by them.
    since, settle = reading.get("sinceAdmission"), reading.get("settle")
    if all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in (since, settle)) \
            and since < settle:
        return (f"{held}, and the CPU of {mac} does not yet show the job admitted {int(since)}s ago, within "
                f"HEAVY_SETTLE_SECONDS={settle:g}")
    found, limit = reading.get("cpuBusy"), reading["busyLimit"]
    busy = found if isinstance(found, (int, float)) and not isinstance(found, bool) else None
    if busy is None:
        return f"the CPU busy share of {mac}, which {said('busyLimit', limit)} bounds, is unavailable"
    if busy >= limit:
        whence = limit_whence(reading, "busyLimit")
        return (f"{mac} is busy (CPU {busy:.0f}% busy, at or above its HEAVY_BUSY_LIMIT={limit:g}%"
                f"{', ' + whence if whence else ''})")
    # Beyond the guaranteed slots, memory pressure that is not normal, or cannot be read, withholds the slot, judged
    # as admission judges it; an older toolkit's reading has no memory, and withholds nothing by it.
    if "memory" in reading:
        level = reading["memory"] if isinstance(reading["memory"], int) and not isinstance(reading["memory"], bool) else None
        refused = resources.judged(busy, limit, lambda: level, said("busyLimit", limit))
        if refused is not None:
            return f"{held}, and on {mac} {refused[1]}"
    return None


def limit_from(reading, key):
    """(where the Mac whose reading this is, offload_host.capacity's or offload_host.pressure's, sets its limit key,
    that Mac's limits file), each None for an older toolkit's reading, which does not say
    (offload_host.limit_sources)."""
    sources, path = reading.get("limitsFrom"), reading.get("limitsFile")
    return (sources.get(key) if isinstance(sources, dict) else None), (path if isinstance(path, str) else None)


def limit_whence(reading, key):
    """Where the Mac whose reading this is sets its limit key, in words (resources.limit_source), or None."""
    return resources.limit_source(*limit_from(reading, key))


def limit_words(reading, key, value, mac):
    """resources.limit_words for mac's limit key, value, from where its reading says it is set (limit_from), so
    that a line quoting it says that it is that Mac's and where to look (vocaro/queuekeeper#29)."""
    return resources.limit_words(key, value, *limit_from(reading, key), mac)


def retry_macs(host, lanes, lane, screen=False):
    """([each Mac, the second Mac as host and this Mac as None], why them, to follow "next on") that a run of lanes'
    lane, about to start, could use, by each Mac's capacity now as Offer.choose reads it: the merge queue's settle wait
    before a retry reads these (vocaro-engineering#253). The Mac cannot be known for certain beforehand, since it is
    chosen as the run starts: the second Mac when it has spare capacity for the lane, this Mac when only it has, and
    whichever admits the lane first when neither has, so then both could take it. Even the second Mac's spare capacity
    is only its reading, and it can still decline the offer: then this Mac takes the run. screen says the lane needs an
    unlocked screen, for this run if not by its configuration."""
    settings = lanes[lane]
    needs = bool(settings.get("wholeHost")), settings.get("resource") == "exclusive", screen or unlocked_screen(lanes, lane)
    if isinstance(host, tuple):
        busy = []
        for candidate in host:
            try: reason = spare(capacity_there(candidate), candidate, *needs, extra=True)
            except Unavailable: continue
            if reason is None:
                return [candidate], f"{candidate}, the first build Mac with spare capacity for {lane}"
            busy.append(candidate)
        reading = offload_host.capacity()
        if not busy or (not reading.get("error") and spare(reading, HERE, *needs) is None):
            return [None], f"this Mac, since no build Mac reports spare capacity for {lane}"
        return [*busy, None], f"whichever Mac admits {lane} first, since none reports spare capacity"
    try: there = spare(capacity_there(host), "the second Mac", *needs)
    except Unavailable as away:
        return [None], f"this Mac, since {host}'s capacity cannot be read: {getattr(away, 'problem', away)}"
    if there is None: return [host], f"{host}, which has spare capacity for {lane} and is asked first"
    reading = offload_host.capacity()
    here = (f"this Mac's capacity cannot be read: {reading['error']}" if reading.get("error")
            else spare(reading, HERE, *needs))
    if here is None: return [None], f"this Mac, the one with spare capacity for {lane}: {there}"
    return [host, None], (f"whichever Mac admits {lane} first, since neither has spare capacity for it ({there}; {here}), "
                          "so both must settle")


def pressure_there(host):
    """The second Mac's pressure as resources.pressure gives this Mac's, judged by resources.judged with the
    limit it sets there (offload_host.pressure), and its one-minute load, None when unavailable, which the merge
    queue logs beside it: what the merge
    queue's settle wait reads before a lane's retry that could use it (retry_macs). A second Mac that cannot be reached,
    or that does not answer with its reading, raises Unavailable, and the wait reads this Mac instead."""
    answered, busy = reading_there(host)
    load = answered.get("load")
    load = load if isinstance(load, (int, float)) else None
    return resources.judged(busy, answered["busyLimit"], lambda: answered.get("memory"),
                            limit_words(answered, "busyLimit", answered["busyLimit"], host)), load


def reading_there(host, health=False):
    """(offload_host's pressure answer from the second Mac, its CPU busy share in percent or None when unavailable),
    or Unavailable when the Mac cannot be reached or does not answer with its busy limit. With health, the answer
    also carries its host_health reading under health, which a toolkit there older than it leaves out."""
    answered = ask(host, {"pressure": True, **({"health": True} if health else {})}, timeout=CENSUS_TIMEOUT)
    if not isinstance(answered, dict) or not isinstance(answered.get("busyLimit"), (int, float)):
        why = answered.get("error") if isinstance(answered, dict) and answered.get("error") else f"it answered {answered!r}"
        raise Unavailable(f"{host} did not report its CPU busy share: {why}")
    busy = answered.get("cpuBusy")
    return answered, busy if isinstance(busy, (int, float)) and not isinstance(busy, bool) else None


def busy_there(host):
    """(the second Mac's CPU busy share in percent, None when unavailable; the busy limit it sets there): what the merge
    queue records beside a gate's wall time when the gate ran there (vocaro-engineering#235). Unavailable as
    pressure_there raises it."""
    answered, busy = reading_there(host)
    return busy, float(answered["busyLimit"])


def dispatched(root, name, since):
    """The second Mac that ran the newest dispatch of name from root that started at or after since, an epoch
    time, or None when none did: the merge queue's record of where a lane it ran gave no verdict. A dispatch's
    directory is named for the second it started (execute); one that only prepared, whose receipt says so,
    ran nothing there. A lost dispatch records its destination beside the log."""
    return dispatch(root, name, since)[1]


def dispatch(root, name, since, pid=None):
    """(the directory of the newest dispatch of name from root that started at or after since and ran on the second
    Mac, that Mac), or (None, None): dispatched, with the directory whose run.log holds what the lane printed there.
    With pid, only a dispatch that process sent, whose directory execute names for it: two runs of one lane from one
    checkout side by side each send their own, and the newest since one began may be the other's
    (vocaro/queuekeeper#9)."""
    try: runs = sorted((Path(root) / ".build/queuekeeper/remote" / DISPATCHES / name).iterdir(), reverse=True)
    except OSError: return None, None
    for run in runs:
        try: started = calendar.timegm(time.strptime(run.name[:16], "%Y%m%dT%H%M%SZ"))
        except ValueError: continue
        if started < int(since): return None, None
        if pid is not None and run.name[16:].split(".")[0] != f"-{pid}": continue
        try: receipt = json.loads((run / "source.json").read_text())
        except FileNotFoundError:
            try: host = (run / DISPATCH_HOST).read_text().strip()
            except (OSError, ValueError): host = ""
            if host: return run, host
            receipt = {}
        except (OSError, ValueError): continue
        if not isinstance(receipt, dict) or receipt.get("stage") == "preparation": continue
        if receipt.get("host"): return run, receipt["host"]
        try: return run, configured()
        except ValueError: return None, None
    return None, None


def reap_request(root):
    """What a dispatch from root asks of offload_host.reap: the project's pool, besides those the second
    Mac's checkouts name."""
    pool = extensions.DEVICES.pool(root)
    return {"pools": [pool] if isinstance(pool, str) and pool else []}


def census_command(advertise=None, reap=None, update=None, stop_leftovers=True, siblings=None):
    """The second Mac's command that prints offload_host.leftovers, the census of what runs left
    running there, as its last line; with advertise, paths of repositories relative to the home
    there, it has each name the commits its checkouts hold, and says which, and its Git's version
    (offload_host.advertise); with reap, offload_host.reap's arguments, it first shuts down the stale pool
    simulators the census finds, and says which, and stops the leftovers it proves an ended run's, or with
    stop_leftovers false only lists them (offload_host.stop_leftovers); with update, update_request's, it
    fast-forwards the second Mac's own toolkit checkout when it can, and says what became of it
    (offload_host.update_toolkit); with siblings, sibling_requests', it does the same for each folder a project's
    extensions load from, which that Mac links beside a dispatched checkout."""
    request = {"leftovers": ROOT, **({"advertise": list(advertise)} if advertise else {}),
               **({"reap": reap} if reap is not None else {}), **({"toolkitCheckout": update} if update else {}),
               **({"siblingCheckouts": siblings} if siblings else {}),
               **({} if stop_leftovers else {"leftoverStops": "list"})}
    return host_command(Path(offload_host.__file__).read_text(), json.dumps(request))


def update_request(want=True):
    """What a connection asks of offload_host.update_toolkit on the second Mac (vocaro-engineering#203): its own
    toolkit checkout, and with want, the commit this Mac's origin/main names, which spares the fetch there when
    that checkout holds it already. Without want, or when this Mac names none, it fetches."""
    request = {"path": offload_host.TOOLKIT_CHECKOUT, "root": ROOT}
    if want:
        try: request["want"] = git(TOOLKIT_ROOT, "rev-parse", "--verify", "--quiet", "refs/remotes/origin/main^{commit}")
        except (RuntimeError, OSError, subprocess.SubprocessError): pass
    return request


def sibling_requests(root):
    """What a connection asks of offload_host.update_toolkit for each folder beside the project that its
    extensions load from (config.sibling_folders): the second Mac's own ~/Development/NAME, and the commit this Mac's
    checkout of it names as origin/main, which spares the fetch there when that checkout holds it already."""
    requests = []
    for name in sibling_folders(root):
        request = {"path": f"Development/{name}", "root": ROOT}
        here = (git_state.main_checkout(root) or Path(root)).resolve().parent / name
        try: request["want"] = git(here, "rev-parse", "--verify", "--quiet", "refs/remotes/origin/main^{commit}")
        except (RuntimeError, OSError, subprocess.SubprocessError): pass
        requests.append(request)
    return requests


def toolkit_line(host, found, what="toolkit checkout"):
    """What is said of offload_host.update_toolkit's answer from host, and whether it is the owner's to decide:
    (None, False) for a checkout already current, or one another connection is updating; (line, False) for one
    it updated; (line, True) for one it left as it is, or could not update, and why."""
    if not isinstance(found, dict) or found.get("state") in ("current", "busy"): return None, False
    where = f"{host}'s {what} ~/{found.get('path') or offload_host.TOOLKIT_CHECKOUT}"
    if found.get("state") == "updated":
        count = found.get("behind")
        return (f"updated {where} from {str(found.get('head'))[:12]} to {str(found.get('to'))[:12]}, "
                f"{count} commit{'s' * (count != 1)} of origin/main"), False
    at = f" at {found['head'][:12]}" if found.get("head") else ""
    return (f"left {where}{at} as it is: {found.get('reason') or 'it gave no reason'}. queuekeeper fast-forwards it to "
            "origin/main only on main, clean, with no commit of its own and nothing running from it; the owner "
            "decides what becomes of it (guides/shared-host.md, The second Mac's own toolkit checkout)"), True


def update_there(host, siblings=()):
    """offload_host.update_toolkit's answer from host, for `qk doctor` (cli.fix_hosts), which fetches whatever
    this Mac's origin/main says; one that cannot be had is an answer of state unchecked, saying why. With it,
    which of siblings, the folders a project's extensions load from (config.sibling_folders), host has no checkout
    of (offload_host.missing_siblings), asked on the same connection (vocaro/queuekeeper#117): a list, or None when
    none were asked of or host did not say."""
    request = {"toolkitCheckout": update_request(want=False), **({"siblingFolders": list(siblings)} if siblings else {})}
    try:
        answered = ask(host, request, timeout=CENSUS_TIMEOUT + offload_host.UPDATE_SECONDS)
    except Unavailable as error:
        return {"state": "unchecked", "reason": str(error)}, None
    found = answered.get("toolkitCheckout") if isinstance(answered, dict) else None
    missing = answered.get("missingSiblings") if isinstance(answered, dict) and siblings else None
    return (found if isinstance(found, dict) else {"state": "unchecked", "reason": f"it answered {answered!r}"},
            missing if isinstance(missing, list) else None)


def sibling_clone(root, name):
    """How the second Mac's own checkout of name, a folder beside the project at root that its extensions load
    from, is made: a `git clone` of the origin this Mac's checkout of it names, run there. queuekeeper does not run
    it; making that checkout is the owner's step (vocaro/queuekeeper#117)."""
    here = (git_state.main_checkout(root) or Path(root)).resolve().parent / name
    target = f"~/Development/{shlex.quote(name)}"
    try: origin = git(here, "remote", "get-url", "origin")
    except (RuntimeError, OSError, subprocess.SubprocessError): origin = ""
    if origin and not offload_host.local_url(origin): return f"git clone {shlex.quote(origin)} {target}"
    return f"git clone of the repository this Mac's {here} comes from, into {target}"


def read_census(found):
    """The census that a finished command's output ends with, and None; or None and why it is missing."""
    try:
        census = json.loads(last_line(found.stdout))
    except (TypeError, ValueError):
        return None, f"exit {found.returncode}: {last_line(found.stderr) or 'no output'}"
    if not isinstance(census, dict) or not isinstance(census.get("leftovers"), list):
        return None, census.get("error") if isinstance(census, dict) else f"it answered {census!r}"
    return census, None


def leftover_census(host):
    """The second Mac's census of leftover processes, read in its login shell as a run there reads
    it, and None; or None and why it is missing. A second Mac that cannot be reached raises; any
    other failure of the census is only reported, since it stops nothing."""
    attempt = asleep()
    try:
        found = ssh(host, census_command(), stdin=subprocess.DEVNULL, capture_output=True, text=True,
                    timeout=CENSUS_TIMEOUT)
    except subprocess.TimeoutExpired:
        return None, f"it did not answer in {CENSUS_TIMEOUT}s"
    except OSError as error:
        raise unreachable(host, str(error)) from None
    if found.returncode == 255: raise unreachable(host, found.stderr, attempt)
    return read_census(found)


def reap_there(host, request, stop_leftovers=True):
    """offload_host.reap's report from the second Mac, by the census's connection, with stop_leftovers as
    census_command takes it. A failure of any kind is the report's problem: nothing there was stopped that it
    does not name."""
    empty = {"stopped": [], "failed": [], "skipped": [], "eligible": [], "kept": [], "dryRun": request.get("dry_run")}
    try:
        found = ssh(host, census_command(reap=request, stop_leftovers=stop_leftovers), stdin=subprocess.DEVNULL, capture_output=True, text=True,
                    timeout=CENSUS_TIMEOUT + offload_host.REAP_SECONDS + offload_host.STOP_SECONDS)
    except subprocess.TimeoutExpired:
        return {**empty, "problem": f"it did not answer in "
                                    f"{CENSUS_TIMEOUT + offload_host.REAP_SECONDS + offload_host.STOP_SECONDS}s"}
    except OSError as error:
        return {**empty, "problem": str(error)}
    report = final_object(found).get("reap")
    if isinstance(report, dict): return report
    return {**empty, "problem": f"exit {found.returncode}: {last_line(found.stderr) or 'no output'}"}


def reap_lines(where, report, listing=True, held=False):
    """What is said of offload_host.reap's report for a Mac: each stop, failure, release and problem, and with
    listing, what it found and left running, as `qk simulator-pool reap` prints them. Nothing when it found
    nothing. A held entry whose process is gone is named with listing or held, and otherwise only when this
    reap stopped its simulator; its recover command runs on where unless where is HERE."""
    if not isinstance(report, dict): return []
    item_line = lambda item: f"{item['name']} ({item['udid']}), PID {item['pid']}"
    lines = [f"on {where}, stopped {item_line(item)}: {item['reason']} ({offload_host.REAP_LOG}; "
             f"{LEFTOVER_GUIDE})" for item in report.get("stopped", [])]
    lines += [f"on {where}, failed to stop {item_line(item)}, which is not tried again in this run: {item['error']}"
              for item in report.get("failed", [])]
    lines += [f"on {where}, did not stop {item_line(item)}: {item['hold']}" for item in report.get("skipped", [])]
    if report.get("problem"):
        lines.append(f"on {where}, stopped no further simulator: {report['problem']}")
    if listing:
        lines += [f"on {where}, would stop {item_line(item)}: {item['reason']}" for item in report.get("eligible", [])]
        lines += [f"on {where}, keeps {item_line(item)}: {item['hold']}" for item in report.get("kept", [])]
    lines += [f"on {where}, {offload_host.released_line(item)}" for item in report.get("released", [])]
    # The leftovers it stopped as an ended run's, and those it kept with the evidence missing (stop_leftovers).
    found = report.get("processes") if isinstance(report.get("processes"), dict) else {}
    named = lambda item: f"PID {item['pid']}, started {item['start']} ({item.get('executable') or item['command']})"
    lines += [f"on {where}, stopped {named(item)}" + (", which had ended before SIGTERM" if item.get("ended") else "")
              + f": {item['reason']} ({offload_host.STOP_LOG}; {LEFTOVER_GUIDE})" for item in found.get("stopped", [])]
    lines += [f"on {where}, failed to stop {named(item)}, which is not tried again in this run: {item['error']}"
              for item in found.get("failed", [])]
    lines += [f"on {where}, did not stop {named(item)}: {item['hold']}" for item in found.get("kept", [])]
    if found.get("problem"): lines.append(f"on {where}, stopped no leftover process: {found['problem']}")
    # Only a dry run, or `qk doctor`'s listing, has any: said whatever the listing.
    lines += [f"on {where}, would stop {named(item)}: {item['reason']}" for item in found.get("eligible", [])]
    # Before a dispatch, only an entry whose simulator this reap stopped; a listing names every one.
    stopped = {item["udid"] for item in report.get("stopped", [])}
    # One not yet due is named only by a listing: most are a take's, which its session still holds.
    lines += [f"on {where}, {offload_host.recoverable_line(item, None if where == HERE else where)}"
              for item in report.get("recoverable", [])
              if listing or not item.get("early") and (held or str(item.get("device") or "").upper() in stopped)]
    return lines


def device_users(host, udid, since):
    """What on the second Mac may still use the physical device a run there was given, since the
    epoch time since, as lines to show; none when nothing does (offload_host.device_users). A second
    Mac that cannot be reached or does not answer raises, and a recovery waits for it."""
    request = json.dumps({"device": {"udid": udid, "root": ROOT, "since": since}})
    attempt = asleep()
    try:
        found = ssh(host, host_command(Path(offload_host.__file__).read_text(), request),
                    stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=CENSUS_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise Unavailable(f"{host} did not list its processes in {CENSUS_TIMEOUT}s") from None
    except OSError as error:
        raise unreachable(host, str(error)) from None
    if found.returncode == 255: raise unreachable(host, found.stderr, attempt)
    census, missing = read_census(found)
    if census is None: raise Unavailable(f"{host} did not list its processes: {missing}")
    return ([f"PID {row['pid']}, started {row['start']}, names {udid}: {row['command']}" for row in census["naming"]]
            + [f"PID {row['pid']}, started {row['start']} since the run did, left over from a run: {row['command']}"
               for row in census["leftovers"]])


def leftover_lines(host, census, missing):
    """What `qk remote` prints of the census before it sends work: nothing when no process is left over."""
    if census is None: return [f"could not list the processes left over from runs on {host}: {missing}"]
    lines = [f"on {host}, {item}" for item in census.get("unchecked", [])]
    rows = census["leftovers"]
    if rows:
        lines.append(f"{host} runs {len(rows)} process{'es' if len(rows) > 1 else ''} left over from runs, which no "
                     f"live run or admission there accounts for. These were not stopped; tell the owner, unless a stale pool "
                     f"simulator among them is `qk simulator-pool reap`'s to stop, or a later cleanup proves one an "
                     f"ended run's ({LEFTOVER_GUIDE}):")
    for row in rows:
        written = row.get("written")
        rate = f", {disk.human(written / row['seconds'])}/s since it started" if written and row.get("seconds") else ""
        where = {"checkout": "a checkout", "temporary directory": "the temporary directory"}.get(
            row["place"], f"simulator {row.get('simulator') or 'with no device named'}")
        lines += [f"  PID {row['pid']}, parent {row['parent']}, started {row['start']}, has written "
                  f"{'an unknown amount' if written is None else disk.human(written)}{rate}, from {where}: {row['path']}",
                  f"    {row['command']}"]
        descendants = row.get("descendants") or []
        if descendants:
            most = descendants[0]
            total = sum(descendant["written"] or 0 for descendant in descendants)
            amount = "" if most["written"] is None else f" ({disk.human(most['written'])})"
            lines.append(f"    and {len(descendants)} descendant{'s' if len(descendants) > 1 else ''}, which have written "
                         f"{disk.human(total)}, the most PID {most['pid']}{amount}: {most['command']}")
    return lines


def ssh_settings(host):
    """What `ssh -G` says ssh would use to reach host, by lowercase keyword; empty when it says nothing."""
    try:
        found = subprocess.run(["ssh", "-G", host], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                               timeout=10, env=environment())
    except (OSError, subprocess.SubprocessError):
        return {}
    settings = {}
    for line in found.stdout.splitlines() if found.returncode == 0 else []:
        key, _, value = line.strip().partition(" ")
        settings.setdefault(key.lower(), value.strip())
    return settings


def reach(address, family, timeout):
    """The seconds a TCP connection to address took and None, or the seconds spent and why none was made."""
    started = time.monotonic()
    with socket.socket(family, socket.SOCK_STREAM) as attempt:
        attempt.settimeout(timeout)
        try:
            attempt.connect(address)
        except socket.timeout:
            return time.monotonic() - started, f"no answer in {timeout} s"
        except OSError as error:
            return time.monotonic() - started, error.strerror or str(error)
    return time.monotonic() - started, None


def address_name(family, address):
    """An address as ssh -v names it, with a link-local IPv6 address's interface."""
    name = address[0]
    if family == socket.AF_INET6 and "%" not in name and len(address) > 3 and address[3]:
        with contextlib.suppress(OSError): name += "%" + socket.if_indextoname(address[3])
    return name


def connection(host, timeout=CONNECT_TIMEOUT):
    """How ssh connects to host, as `qk doctor` reports it: the name and port `ssh -G` gives, and the
    addresses that name resolves to under its AddressFamily, in the order ssh tries them. Each is
    connected to in turn, as ssh does, for at most timeout seconds, until one answers; ssh tries none
    after that one. A host reached through a proxy is not measured, and unchecked says so; a name that
    does not resolve is reported in unresolved. A second Mac found renamed is measured under its new
    name (rediscover)."""
    settings = ssh_settings(host)
    name = RENAMED[host]["to"] if host in RENAMED else settings.get("hostname") or host
    family = settings.get("addressfamily") or "any"
    port = int(settings["port"]) if settings.get("port", "").isdigit() else 22
    found = {"hostname": name, "port": port, "addressFamily": family}
    proxy = next((settings[key] for key in ("proxyjump", "proxycommand") if settings.get(key, "none") != "none"), None)
    if proxy is not None:
        found["unchecked"] = f"how ssh connects to {host}, which it reaches through {proxy}"
        return found
    try:
        resolved = socket.getaddrinfo(name, port, FAMILIES.get(family, socket.AF_UNSPEC), socket.SOCK_STREAM)
    except (OSError, UnicodeError) as error:
        found["unresolved"] = str(error)
        return found
    found["addresses"], answered = [], False
    for kind, _type, _protocol, _canonical, address in resolved:
        entry = {"address": address_name(kind, address)}
        if answered:
            entry["outcome"] = "not tried: ssh connects to the one before it"
        else:
            seconds, failure = reach(address, kind, timeout)
            answered = failure is None
            entry.update(seconds=round(seconds, 3), outcome=failure or "connected")
        found["addresses"].append(entry)
    return found


def slow_connection(host, found, timeout=CONNECT_TIMEOUT):
    """The error `qk doctor` reports when the first addresses ssh tries for host do not answer and keep
    each connection waiting before a later one does, as the build host's link-local IPv6 address did,
    10 s for each connection, until 2026-10-27; or None."""
    tried = [entry for entry in found.get("addresses") or [] if "seconds" in entry]
    if len(tried) < 2 or tried[-1]["outcome"] != "connected": return None
    dead, used = tried[:-1], tried[-1]
    if sum(entry["seconds"] for entry in dead) < 1: return None  # Refused at once, as ssh moves on at once.
    ipv6 = ":" in used["address"]
    if all((":" in entry["address"]) != ipv6 for entry in dead):
        fix = (f"add `AddressFamily {'inet6' if ipv6 else 'inet'}` under `Host {host}` in ~/.ssh/config, so ssh tries "
               f"only {'IPv6' if ipv6 else 'IPv4'}, or make the {'IPv4' if ipv6 else 'IPv6'} addresses answer")
    else:
        fix = f"make those addresses answer, or name in `Host {host}` in ~/.ssh/config a HostName that resolves to one that does"
    plural = len(dead) > 1
    waits = "; ".join(f"{entry['address']}, {entry['outcome']} ({entry['seconds']:.1f} s)" for entry in dead)
    return (f"ssh tries {len(dead)} address{'es' if plural else ''} of {found['hostname']} that {'do' if plural else 'does'} "
            f"not answer before {used['address']}, which answered in {used['seconds']:.2f} s: {waits}. So each "
            f"connection to {host}, several in every run, first waits up to ConnectTimeout's {timeout} s for "
            f"{'each' if plural else 'it'}, and an ssh without ConnectTimeout, such as a plain `ssh {host}`, waits out "
            f"the system's TCP connection timeout; to fix it, {fix} (guides/shared-host.md, Setting up the second Mac)")


def compare_device_records(report, host, here, there):
    """Add to `qk doctor`'s report what the two Macs' device records say of physical devices, which
    only this Mac's record holds (guides/shared-host.md, Physical devices and the second Mac): this
    Mac's must defer them to no Mac, and the second Mac's must defer them to this Mac and hold none."""
    fix = "which Mac's record holds physical devices is the owner's decision"
    ours, theirs = here.get("deviceRecord") or {}, there.get("deviceRecord") or {}
    for side, found in [("this Mac", ours), (host, theirs)]:
        if found.get("error"): report["unchecked"].append(f"{side}'s device record {found.get('path')}: {found['error']}")
    if ours.get("deferredTo"):
        report["errors"].append(
            f"this Mac's device record defers physical devices to {ours['deferredTo']}, so it refuses the paired "
            f"devices that only it should hold; {fix}, and `devices defer --end` here restores it")
    if theirs.get("physical"):
        report["errors"].append(
            f"{host}'s device record holds the physical device of {', '.join(theirs['physical'])}, which only this "
            f"Mac's record may hold, so qk remote refuses every run there; release or recover it there")
    if theirs.get("deferredTo") not in (None, here.get("name")):
        report["errors"].append(
            f"{host}'s device record defers physical devices to {theirs['deferredTo']}, not this Mac "
            f"({here.get('name')}), so qk remote refuses every run there; {fix}, and `devices defer "
            f"{here.get('name')} --replace` there makes it this Mac")
    if not theirs.get("error") and theirs.get("deferredTo") is None:
        report["unchecked"].append(f"whether {host}'s device record refuses physical devices: it defers them to no "
                                   "Mac until the next qk remote run there")


def python_crash_notes(hosts):
    """Human-readable observations, separate from doctor's errors and its verdict."""
    notes = []
    for host, census in hosts.get("pythonCrashes", {}).items():
        label = "this Mac" if host == "local" else host
        for report in census.get("reports", []):
            frames = "; ".join(str(frame.get("symbol") or frame.get("imageOffset") or "unknown frame")
                               for frame in report.get("frames", [])) or "no frames recorded"
            notes.append(f"Python crash on {label}: {report.get('path', 'unknown path')}, "
                         f"{report.get('time') or 'unknown time'}, {report.get('signal') or 'unknown signal'}, "
                         f"parent {report.get('parentProcess') or 'unknown'}; {frames}")
        notes += [f"Python crash reports on {label}: {note}" for note in census.get("notes", [])]
    return notes


def compare_hosts():
    """What `qk doctor` reports of the second Mac: this Mac's and the second Mac's default Xcodes
    (`xcode-select -p`), the Xcode at this Mac's developer directory, which every run there uses,
    and the simulator runtimes it lists. A difference is an error, and what could not be compared is
    named in unchecked. A second Mac that cannot be reached, as when this Mac is away from its
    network, is no error, since work then runs here (the owner's decision, 2026-10-28): unchecked
    says why it was not compared, while one that answers but refuses the login is an error. The
    processes there that runs left running are listed in leftovers, which are no error: they say
    what to tell the owner. How ssh connects to it is in connection, and first addresses that keep
    each connection waiting are an error too, as are device records that do not keep physical
    devices in this Mac's alone. Differing runtimes also set runtimesDiffer, which `qk doctor` evens
    out (match_runtimes) before it compares again."""
    host = configured()
    report = {"host": host, "errors": [], "unchecked": [],
              "pythonCrashes": {"local": offload_host.python_crash_reports()}}
    if host is None:
        report["unchecked"].append(f"no second Mac is configured ({HOST_VARIABLE}, or the ssh alias "
                                   f"{' or '.join(ALIASES)} in ~/.ssh/config), so this Mac was compared with none")
        return report
    # Measured first, so that it also says why a second Mac that cannot be reached is not.
    reached = report["connection"] = connection(host)
    if reached.get("unchecked"): report["unchecked"].append(reached["unchecked"])
    slow = slow_connection(host, reached)
    if slow: report["errors"].append(slow)
    developer = developer_directory()
    here = report["local"] = offload_host.toolchain(developer)
    try:
        reachable(host, reached)
        if host in RENAMED: report["errors"].append(f"{host} was renamed from {RENAMED[host]['from']} to "
                                                    f"{RENAMED[host]['to']}, which queuekeeper finds again in each run; "
                                                    f"{renamed_fix(host)}")
    except Unreachable as away:
        report["unchecked"].append(f"{host}, which cannot be reached, so work runs on this Mac until it can: "
                                   f"{away.problem}")
        return report
    except Misconfigured as problem:
        report["errors"].append(str(problem))
        report["unchecked"].append(f"{host}'s default Xcode, its Xcode at this Mac's developer directory and its "
                                   "simulator runtimes, which it did not report")
        return report
    try:
        there = report["remote"] = survey(host, developer)
        report["pythonCrashes"][host] = there.get("pythonCrashes", {"notes": ["host did not report Python crashes"]})
    except Misconfigured as problem:
        report["errors"].append(str(problem))
        report["unchecked"].append(f"{host}'s default Xcode, its Xcode at this Mac's developer directory and its "
                                   "simulator runtimes, which it did not report")
        return report
    try:
        census, missing = leftover_census(host)
    except Misconfigured as problem:
        census, missing = None, problem.problem
    if census is None:
        report["unchecked"].append(f"the processes left over from runs on {host}, which it did not list: {missing}")
    else:
        report["leftovers"] = census["leftovers"]
        report["unchecked"] += [f"on {host}, {item}" for item in census.get("unchecked", [])]
    compare_device_records(report, host, here, there)
    version = lambda text: ", ".join((text or "no Xcode").splitlines())
    default = lambda side: (side.get("defaultDeveloperDir"), side.get("defaultXcode"))
    # When both Macs' default is this Mac's developer directory, the next check covers its version.
    shared = here.get("defaultDeveloperDir") == there.get("defaultDeveloperDir") == developer
    if default(here) != default(there) and not shared:
        report["errors"].append(
            f"the default Xcodes differ: `xcode-select -p` is {here.get('defaultDeveloperDir')} "
            f"({version(here.get('defaultXcode'))}) on this Mac and {there.get('defaultDeveloperDir')} "
            f"({version(there.get('defaultXcode'))}) on {host}, so whatever reads {host}'s default, such as "
            f"`ssh {host} xcodebuild -version` or a script without DEVELOPER_DIR, sees another Xcode than this Mac's; "
            "to fix it, the owner chooses the Xcode both Macs use and selects it with `sudo xcode-select -s PATH` "
            "on the Mac that differs")
    if developer is None or not here.get("xcode"):
        report["unchecked"].append("the Xcode at this Mac's developer directory and its simulator runtimes, since "
                                   "this Mac has no Xcode selected")
        return report
    if here["xcode"] != there.get("xcode"):
        report["errors"].append(
            f"the Xcode at this Mac's developer directory, {developer}, is {version(here['xcode'])} on this Mac and "
            f"{version(there.get('xcode')) if there.get('xcode') else 'missing'} on {host}, so qk remote refuses "
            f"every run there; to fix it, install this Mac's Xcode at that path on {host} (guides/shared-host.md, "
            "Setting up the second Mac), or select with DEVELOPER_DIR an Xcode both Macs have at the same path")
    if None in (here.get("runtimes"), there.get("runtimes")):
        report["unchecked"].append(f"the simulator runtimes, which simctl did not list on "
                                   f"{'this Mac' if here.get('runtimes') is None else host}")
    elif here["runtimes"] != there["runtimes"]:
        differences = runtime_differences(here["runtimes"], there["runtimes"], host)
        report["runtimesDiffer"] = True
        report["errors"].append(f"the simulator runtimes of the Xcode at {developer} differ: {'; '.join(differences)}; "
                                "`qk doctor` evens them out itself, deleting a runtime that no simulator but Xcode's "
                                "defaults uses and copying any other to the Mac that lacks it, and it could not here: "
                                "see its `runtimes:` lines, or run `python3 scripts/qk remote setup --runtimes` "
                                "(guides/shared-host.md, Setting up the second Mac)")
    return report
