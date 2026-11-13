"""Bounded supervision of the process group this invocation creates."""
import contextlib
import os
import json
from pathlib import Path
import re
import select
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid

# How ps prints a process's start (lstart), with its spacing collapsed.
STARTED = "%a %b %d %H:%M:%S %Y"
# Seconds an observed descendant may outlive the root before it is stopped, and then before
# SIGTERM becomes SIGKILL. Xcode's DTServiceHub can outlive xcodebuild by minutes, ignoring SIGTERM.
LINGER, KILL_AFTER = 60, 10
# A program inside an Xcode, by where its command line starts, other than xcodebuild itself: the
# DTServiceHub that xcodebuild starts for a test run, or the ibtoold a build starts. Once launchd has
# adopted every descendant still running and each is such a program, they are stopped without LINGER:
# on 2026-11-04 every one of 44 such stops in the queue's lane logs had waited it out, each holding its
# lane and heavy slot about 70 s after the work ended (vocaro-engineering#154).
XCODE_PROGRAM = re.compile(r"[^\s'\"]*/Xcode[^/\s'\"]*\.app/Contents/(?!Developer/usr/bin/xcodebuild(?:\s|$))\S")
# Seconds a foreground run has between SIGTERM and SIGKILL instead: it restores what it changed on a Mac
# that keeps its state, VoiceOver first, then collects what it heard (guides/foreground.md).
# On 2026-10-30 a stop at KILL_AFTER left VoiceOver on there. Chosen to cover a session's restoration and
# a lane's bounded collection of its end state; not measured.
FOREGROUND_KILL_AFTER = 120
# Seconds a stopped command's processes have after SIGKILL before the run is uncertain.
KILL_WAIT = 5
# Seconds queuekeeper lets a lane or an `qk admit` command run that declares no limit of its own, so that no run
# waits forever on a hang: BreezEPUB's tests waited on a thumbnail request from 08:13 on 2026-10-31 with
# none. More than twice the longest admission the log held from 2026-10-27 to 2026-10-31, 53 min of
# Pagepour's tests; a lane that runs longer says so with its own timeout.
DEFAULT_LIMIT = 7200
# Seconds beyond KILL_AFTER that SIGKILL waits while a live admission owner is among the processes,
# as a nested qk admit in a self lane's script is: it stops its own command, with a KILL_AFTER
# and KILL_WAIT of its own, then settles its guard entry, which SIGKILL would strand. It also
# bounds an owner that never settles.
SETTLE_WAIT = 30
# The signals forwarding_signals carries out as a supervised stop. SIGKILL cannot be caught.
STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
_stops = []
# The signals a POSIX shell starts a `cmd &` job with ignored when it has no job control, as a non-interactive
# zsh or sh does. Every process inherits an ignored signal through exec (vocaro/BreezEPUB#47).
BACKGROUND_IGNORED = (signal.SIGINT, signal.SIGQUIT)
# Seconds supervise waits for a held command's exec once on_start has released it, which covers an
# interpreter start on a busy host; not measured. Past it the run goes on, and a failed exec then shows
# only as status 127, not NotStarted.
GATE_WAIT = 30
# What a command with an on_start starts as: it keeps the PID and start time through the exec, and runs
# nothing unless on_start returns, which a launcher's death prevents by closing the gate. The release is
# the command's environment, since macOS's framework Python adds to its own. An exec failure goes back
# on the second descriptor, which a successful exec closes. Python ignores these two signals at its
# start, and the command must not inherit that, as it does not from subprocess.
GATE = """
import json, os, signal, sys
gate, told = int(sys.argv[1]), int(sys.argv[2])
release = b""
while part := os.read(gate, 65536): release += part
if not release.endswith(b"\\n"): os._exit(1)
os.close(gate)
os.set_inheritable(told, False)
for name in ("SIGPIPE", "SIGXFSZ"): signal.signal(getattr(signal, name), signal.SIG_DFL)
try: os.execvpe(sys.argv[3], sys.argv[3:], json.loads(release))
except Exception as error:
    os.write(told, f"{sys.argv[3]}: {error}".encode())
    os._exit(127)
"""


def snapshot():
    # A stop sent to this whole process tree, as an agent's task stop sends it, can end ps too.
    for _attempt in range(3):
        listed = subprocess.run(["ps", "-axo", "pid=,ppid=,pgid=,lstart="], capture_output=True, text=True)
        if listed.returncode >= 0: break
    if listed.returncode:
        raise subprocess.CalledProcessError(listed.returncode, listed.args, listed.stdout, listed.stderr)
    output = listed.stdout
    result = {}
    for line in output.splitlines():
        parts = line.split(maxsplit=3)
        if len(parts) == 4:
            result[int(parts[0])] = (int(parts[1]), int(parts[2]), parts[3])
    return result


def descendants(current, roots):
    """The processes of a snapshot below these PIDs by parent, whatever their process group or session, as
    {PID: (parent, group, start)}; the roots themselves are not included."""
    found, frontier = {}, set(roots)
    while frontier:
        frontier = {pid for pid, (parent, _group, _birth) in current.items()
                    if parent in frontier and pid not in found and pid not in roots}
        found.update({pid: current[pid] for pid in frontier})
    return found


def session(pid):
    """A live process's session, or None once it has ended. ps -o sess prints 0 on macOS."""
    try: return os.getsid(pid)
    except OSError: return None


def birth_time(start):
    """A start as ps prints it (lstart), in seconds since the epoch, or None."""
    try: return time.mktime(time.strptime(" ".join(start.split()), STARTED))
    except (AttributeError, TypeError, ValueError, OverflowError): return None


def survivors(spaces, closed=None):
    """Every live process in these process groups and sessions, as (pid, group, session).

    The command and every process it was seen with have ended, and the kernel never gives a new
    process a PID that still names a group or session. An ID that is a live process's PID was
    therefore taken over after the command's group or session ended, and is skipped.

    closed gives, for a group or session, when the first sample found nothing of the command in it
    (time.time()). A process born after that is not counted for it: the kernel can give the ID's PID
    to a newcomer once the group is empty, and the newcomer can make a group of its own and exit, as
    the owner's Riot Client launcher did on 2026-11-07 (#240). A leftover born before is still found,
    and so is one born after that the command's own unseen leftover is in, by that leftover.
    """
    current = snapshot()
    spaces = spaces - current.keys()
    closed = closed or {}
    found = []
    for pid, (_parent, group, birth) in current.items():
        sid = session(pid)
        born = birth_time(birth)
        if any(space in spaces and (space not in closed or born is None or born <= closed[space])
               for space in (group, sid)):
            found.append((pid, group, sid))
    return found


def describe(pids, known, root, kill_after=KILL_AFTER, helpers=False):
    """The processes still running after the root, with the parent and group each was first seen with;
    with helpers, Xcode's own programs stopped without the grace."""
    pids = sorted(pids)
    listing = subprocess.run(["ps", "-o", "pid,ppid,pgid,lstart,args", "-p", ",".join(map(str, pids))],
                             capture_output=True, text=True).stdout.rstrip()
    seen = "; ".join(f"{pid} with parent {known[pid][0]}, group {known[pid][1]}" for pid in pids)
    when = (f"Xcode program(s) that launchd adopted when the command (PID {root}) exited, without waiting "
            f"{LINGER:g}s," if helpers else f"process(es) still running {LINGER:g}s after the command (PID {root}) exited,")
    return (f"supervise: stopping {len(pids)} {when} with SIGTERM, then SIGKILL after {kill_after:g}s; first seen: "
            f"{seen}\n{listing}")


def xcode_helper(parent, command):
    """Whether a descendant still running after the root is one of Xcode's own programs that launchd has
    adopted, which nothing of the run waits on (XCODE_PROGRAM)."""
    return parent == 1 and bool(XCODE_PROGRAM.match(command or ""))


def commands(pids):
    """The command line of each of these processes that ps lists, by PID."""
    listed = subprocess.run(["ps", "-o", "pid=,args=", "-p", ",".join(map(str, sorted(pids)))],
                            capture_output=True, text=True).stdout
    return {int(parts[0]): parts[1] for parts in (line.split(maxsplit=1) for line in listed.splitlines())
            if len(parts) == 2 and parts[0].isdigit()}


def owners(identities, environment):
    """Those of these {PID: start} processes that the environment's admission guard records as owners."""
    from queuekeeper.resources import guard_owners
    recorded = guard_owners(environment)
    return sorted(pid for pid, start in identities.items() if (pid, " ".join(start.split())) in recorded)


def held_locks(environment):
    """The descriptors of the admission locks that the environment's token names and this process
    holds, for the command to inherit: each open on the lock file its entry names, which names the
    entry's owner.

    A token can reach a process without its descriptors, as a plain subprocess call starts its
    child, and its numbers then name nothing or another file; `qk unittest` started each module's
    process so until it passed these on too. Passed on, a closed one stopped a lane from starting
    with EBADF, and an open one gave the command that file (#70). The command keeps the token, so a
    nested admission in it refuses the token rather than taking a slot.
    """
    try:
        locks = json.loads(environment.get("QUEUEKEEPER_RESOURCE_TOKEN") or "{}").get("locks", [])
    except (ValueError, AttributeError):
        return ()
    held = []
    for item in locks if isinstance(locks, list) else []:
        try:
            opened, named = os.fstat(item["fd"]), os.stat(item["path"])
            if (opened.st_dev, opened.st_ino) == (named.st_dev, named.st_ino) and \
                    item["nonce"] in os.pread(item["fd"], 4096, 0).decode(errors="replace"):
                held.append(item["fd"])
        except (KeyError, TypeError, ValueError, OSError):
            continue
    return tuple(held)


class NotStarted(OSError):
    """The command could not be started, so it left no process to wait for."""


class Interrupted(Exception):
    """A stop signal ended the run, leaving none of its observed processes running."""

    def __init__(self, signum, detail):
        super().__init__(f"Stopped by {signal.Signals(signum).name} {detail}")
        self.signum = signum


class TimedOut(TimeoutError):
    """The run's timeout expired, and then the command and every observed descendant ended."""


class Canceled(TimedOut):
    """The caller canceled the run (supervise's cancel), and then the command and every observed descendant ended,
    as after a timeout: the merge queue throws away a gate stacked on a batch that did not land."""


class Stalled(TimedOut):
    """A lane's explicit quiet bound stopped it, and ownership cleanup confirmed every process ended."""


def default_background_signals():
    """Put back the default for each of BACKGROUND_IGNORED this process ignores, so that the commands it starts do
    not inherit how it was launched: a merge-queue worker that `qk land &` started passes SIG_IGN for SIGINT to every
    gate lane, and a test that sends its helper SIGINT then fails. Only from the main thread, as signal requires;
    a handler someone installed stays."""
    if threading.current_thread() is not threading.main_thread(): return
    for number in BACKGROUND_IGNORED:
        if signal.getsignal(number) == signal.SIG_IGN: signal.signal(number, signal.SIG_DFL)


def request_stop(signum, _frame=None):
    """Record a stop for supervision to carry out. It never raises: an exception raised between
    publishing an admission and entering its context would strand the admission's entry."""
    _stops.append(signum)


def requested():
    """The first stop signal received while forwarding_signals is in effect, or None."""
    return _stops[0] if _stops else None


@contextlib.contextmanager
def forwarding_signals():
    """Carry out SIGTERM, SIGINT and SIGHUP as a supervised stop, then end by the first one.

    Supervision forwards the signal to the command, and admission is left, before this process
    ends by the same signal. A signal the caller ignores, as nohup ignores SIGHUP, stays ignored.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = {}
    for number in STOP_SIGNALS:
        if signal.getsignal(number) not in (signal.SIG_IGN, None):
            previous[number] = signal.signal(number, request_stop)
    try:
        yield
    finally:
        for number, handler in previous.items(): signal.signal(number, handler)
        if _stops:
            number = _stops[0]
            _stops.clear()
            with contextlib.suppress(OSError, ValueError):
                sys.stdout.flush()
                sys.stderr.flush()
            signal.signal(number, signal.SIG_DFL)
            os.kill(os.getpid(), number)
            raise SystemExit(128 + number)


def released(on_start, pid, environment, go, told):
    """Run on_start for the held process, then release it through GATE; why its exec failed, or nothing."""
    try:
        try:
            on_start(pid)
            # A stop can have ended it already, which the supervision sees.
            with contextlib.suppress(BrokenPipeError), os.fdopen(os.dup(go), "wb") as gate:
                gate.write(json.dumps(environment).encode() + b"\n")
        finally:
            os.close(go)
        ready, _, _ = select.select([told], [], [], GATE_WAIT)
        return os.read(told, 4096).decode(errors="replace") if ready else ""
    finally:
        os.close(told)


def supervise(command, *, cwd, stdout=None, timeout=None, env=None, on_stop=None, on_start=None,
              kill_after=KILL_AFTER, cancel=None, progress=None):
    """Wait for the owned root and observed descendants, including separate process groups.

    on_start(pid) runs once the root's process exists and before its command does, so the command
    finds whatever on_start records (#94); if it raises, or this process dies first, the command
    never runs.

    PID start identities prevent signaling a reused PID. An uncertain run raises so admission
    retains its orphan guard. A confirmed terminal exit keeps its status. A run that fails, is
    stopped or times out is confirmed only once a census of the process groups and sessions it was
    seen using finds nothing left in them.
    Descendants still running LINGER seconds after the root exits are reported on stderr and
    stopped, or at once when each is a program inside an Xcode that launchd adopted, such as
    DTServiceHub (xcode_helper); the root's status stands once they are gone, and one that survives SIGKILL is
    still awaited. A timeout bounds only the root: one that exits in time keeps its status
    through that grace. A command that cannot start raises NotStarted, which admission releases.

    A stop that forwarding_signals records calls on_stop, then goes to the root's group and to
    each observed descendant, becoming SIGKILL after kill_after seconds (KILL_AFTER unless the caller
    gives more, as a foreground run does). Once the root and every observed descendant have ended,
    the run raises Interrupted, which admission releases; any still running KILL_WAIT seconds after
    SIGKILL leave it uncertain.
    A timeout that expires while the root runs is carried out the same way with SIGTERM, without
    on_stop, and the run raises TimedOut, which admission also releases.
    With an opt-in progress monitor, a confirmed quiet bound uses that same stop and cleanup,
    raising Stalled only once the owned tree ended. Missing samples never establish a stall.
    Any SIGKILL waits up to SETTLE_WAIT seconds more while the root or an observed descendant is
    an admission owner the guard records: a nested admission stops its own command and then
    settles its entry, which SIGKILL would strand. After the first SIGTERM or stop signal, such an
    owner's new children are left to it.

    The command inherits the admission lock descriptors that the token in env names and this
    process holds (held_locks), so that nested work reuses the admission.

    Inside an admission this process holds, the admission's process record gets the command before
    it starts, then its root, each process and each process group or session at the first sample
    that sees it, when each group or session is found without the command or used again, and a
    confirmed end, so that `qk admissions recover` can run this census once this process has gone.
    """
    if requested() is not None:
        raise Interrupted(requested(), "before its command started")
    environment = dict(os.environ) if env is None else env
    descriptors = held_locks(environment)
    from queuekeeper.resources import holding
    admission, key = holding(environment), uuid.uuid4().hex

    def note(**observed):
        """Append what was seen of this command to the process record, if it runs in an admission held here."""
        if admission is not None: admission.observe({"command": key, **observed})

    # First, so that a record without this command's root says it may have started.
    note(argv=[str(part) for part in command])
    launch, passed, held_gate, go, told, tell = command, descriptors, None, None, None, None
    if on_start is not None:
        held_gate, go = os.pipe()
        told, tell = os.pipe()
        launch = [sys.executable, "-I", "-S", "-c", GATE, str(held_gate), str(tell), *command]
        passed = (*descriptors, held_gate, tell)
    try:
        child = subprocess.Popen(launch, cwd=cwd, env=environment, stdout=stdout,
            stderr=subprocess.STDOUT if stdout is not None else None, start_new_session=True,
            pass_fds=passed)
    except (OSError, ValueError) as error:
        # Popen reaps a child whose exec failed before it raises; nothing else ran.
        if on_start is not None: os.close(go); os.close(told)
        note(settled="not-started")
        raise NotStarted(f"Command did not start: {error}") from error
    finally:
        if on_start is not None: os.close(held_gate); os.close(tell)
    note(root=child.pid, spaces=[child.pid])
    # Each observed descendant's (parent, group, start) when first seen, for its identity and report.
    known, born, exited, stopping = {}, None, None, set()
    # When the first SIGTERM went to descendants that outlived the root, which starts their kill_after.
    lingered = None
    # Each descendant's command line once the root has exited, by (PID, start): read once, for xcode_helper.
    lines = {}
    # The process groups and sessions the command was seen using, for the census at its end. The
    # root's own are its PID. Never every observed PID: the PID space can wrap during a long run.
    spaces = {child.pid}
    # When the first sample found nothing of the command in each of them, by group or session.
    closed = {}
    started = time.monotonic()
    # When the timeout expired and when a stop was first seen, whether the signal reached the
    # root's group, and who else had it.
    expired, stopped, group_forwarded, forwarded = None, None, False, set()
    canceled, stalled = False, None
    # Whether the stop's or timeout's first signal went out, and when its SIGKILL began.
    signaled, killed = False, None
    # Each process seen as an admission owner, as (PID, start), and the PIDs SIGKILL was reported waiting for.
    recognized, reported = set(), set()
    # Each process the process record holds, as (PID, start).
    noted = set()

    def recognize(identities):
        """Those of these {PID: start} processes that are admission owners. One that has settled
        its guard entry is an owner until it ends."""
        recognized.update((pid, identities[pid]) for pid in owners(identities, environment))
        return {pid for pid, start in recognized if identities.get(pid) == start}

    def progress_controls(identities):
        ignored = recognize(identities)
        # Opt-in role declaration, never a name/CPU heuristic. root_active established this
        # exact child's start identity; its descendant work still supplies CPU and output.
        # No descendants means no readable work, not evidence of idleness. Serial callers
        # and primary-process computation must retain the ordinary policy.
        if progress.settings.get("primaryControl") is True:
            ignored.add(child.pid)
        return ignored

    def unowned(pids, identities):
        """These processes but the children of an admission owner among these processes. The owner
        forwards a stop to its command itself, and its other children are its own samples, which a
        signal would end: three in a row fail its supervision and strand its entry."""
        parents = {known[pid][0] for pid in pids} & identities.keys()
        found = recognize({pid: identities[pid] for pid in parents}) if parents else set()
        return {pid for pid in pids if known[pid][0] not in found}

    def held(beyond, identities):
        """Whether SIGKILL, due this many seconds ago, waits for an admission owner among these processes."""
        if beyond >= SETTLE_WAIT: return False
        found = recognize(identities)
        if found - reported:
            reported.update(found)
            print(f"supervise: admission owner(s) PID {', '.join(map(str, sorted(found)))} still running; SIGKILL "
                  f"waits up to {SETTLE_WAIT:g}s more for them to stop their commands, settle their guard entries "
                  "and end", file=sys.stderr, flush=True)
        return bool(found)

    try:
        if on_start is not None:
            failure = released(on_start, child.pid, environment, go, told)
            if failure:
                note(settled="not-started")
                raise NotStarted(f"Command did not start: {failure}")
        while True:
            # Before the sample, so that a process born after it is born after this moment too: lstart
            # drops the fraction of its second.
            moment = time.time()
            current = snapshot()
            row = current.get(child.pid)
            if born is None and child.returncode is None and row is not None and row[:2] == (os.getpid(), child.pid):
                born = row[2]
            root_active = child.returncode is None and row == (os.getpid(), child.pid, born)
            parents = ({child.pid} if root_active else set()) | {
                pid for pid, seen in known.items() if pid in current and current[pid][2] == seen[2]}
            if root_active:
                for pid, (_parent, group, _birth) in current.items():
                    if pid not in parents and group == child.pid:
                        known[pid] = current[pid]
                        parents.add(pid)
            below = descendants(current, parents)
            parents.update(below)
            known.update(below)
            live = {pid for pid, seen in known.items() if pid in current and current[pid][2] == seen[2]}
            sampled = {space for pid in live for space in (current[pid][1], session(pid)) if space is not None}
            identities = {pid: known[pid][2] for pid in live} | ({child.pid: born} if root_active else {})
            # When each group or session was first found without the command (survivors).
            using = sampled | ({child.pid} if root_active else set())
            reopened = closed.keys() & using
            newly_closed = spaces - using - closed.keys()
            for space in reopened:
                del closed[space]
            closed.update(dict.fromkeys(newly_closed, moment))
            if admission is not None:
                # Record a renewed use in the same line as any new process that shows it.
                fresh, opened = sorted(set(identities.items()) - noted), sorted(sampled - spaces)
                closed_updates = [(space, None) for space in sorted(reopened)]
                closed_updates.extend((space, moment) for space in sorted(newly_closed))
                if fresh or opened or closed_updates:
                    note(processes=[(pid, " ".join(start.split())) for pid, start in fresh], spaces=opened,
                         closed=closed_updates)
                noted.update(fresh)
            spaces |= sampled
            if progress is not None and root_active and expired is None and stopped is None:
                # A nested admission's polling is control work, not evidence its command progresses.
                observation = progress.last
                detail = progress.observe(identities, progress_controls(identities))
                if progress.last is not observation: note(progress=progress.last)
                if detail and child.poll() is None:
                    # Recheck CPU/output immediately before committing to the stop. A completed
                    # command keeps its real verdict, and a new observation can invalidate the interval.
                    detail = progress.observe(identities, progress_controls(identities), force=True)
                    if detail and child.poll() is None:
                        expired, stalled = time.monotonic(), detail
                        print(f"supervise: stalled: {detail}; sending SIGTERM to the command (PID {child.pid}) "
                              f"and its observed descendants, then SIGKILL after {kill_after:g}s",
                              file=sys.stderr, flush=True)
            if timeout is not None and exited is None and expired is None and time.monotonic() - started >= timeout:
                expired = time.monotonic()
                print(f"supervise: timed out after {timeout:g}s; sending SIGTERM to the command (PID {child.pid}) "
                      f"and its observed descendants, then SIGKILL after {kill_after:g}s", file=sys.stderr, flush=True)
            if cancel is not None and exited is None and expired is None and cancel.is_set():
                # Carried out as a timeout is, and reported as the caller's cancel.
                expired, canceled = time.monotonic(), True
                print(f"supervise: canceled; sending SIGTERM to the command (PID {child.pid}) and its observed "
                      f"descendants, then SIGKILL after {kill_after:g}s", file=sys.stderr, flush=True)
            signum = requested()
            if signum is not None and stopped is None:
                stopped = time.monotonic()
                # First, in case SIGKILL follows before the processes are gone.
                if on_stop is not None: on_stop()
                print(f"supervise: {signal.Signals(signum).name} received; forwarding it to the command "
                      f"(PID {child.pid}) and its observed descendants, then SIGKILL after {kill_after:g}s",
                      file=sys.stderr, flush=True)
            ending = min(moment for moment in (expired, stopped) if moment is not None) \
                if expired is not None or stopped is not None else None
            if ending is not None:
                # A later signal does not hasten SIGKILL: a task stop can deliver its SIGTERM twice.
                due = time.monotonic() - ending - kill_after
                if killed is None and due >= 0 and not held(due, identities):
                    killed = time.monotonic()
                kill = killed is not None
                number = signal.SIGKILL if kill else signum if signum is not None else signal.SIGTERM
                # The stop's or timeout's signal once for each process, then SIGKILL until it is gone.
                if root_active and (kill or not group_forwarded):
                    try: os.killpg(child.pid, number)
                    except (ProcessLookupError, PermissionError): pass
                    group_forwarded = True
                fresh = {pid for pid in live if (pid, known[pid][2]) not in forwarded}
                for pid in live if kill else unowned(fresh, identities) if signaled else fresh:
                    try: os.kill(pid, number)
                    except (ProcessLookupError, PermissionError): pass
                forwarded.update((pid, known[pid][2]) for pid in live)
                signaled = True
            result = child.poll()
            cause = (f"Stopped by {signal.Signals(signum).name}" if signum is not None
                     else "Canceled" if canceled else f"Stalled: {stalled}" if stalled is not None
                     else f"Timed out after {timeout:g}s" if expired is not None
                     else f"{command[0]} exited with status {result}")
            if result is not None and not live:
                # Sampling cannot prove that a failed, stopped or timed-out command orphaned nothing
                # between two samples, so its groups and sessions must hold nothing. A passing
                # command has no census: a daemon it starts on purpose must not stop the host.
                left = []
                if result or ending is not None:
                    # A group or session the last sample still found the root in closed once poll found it gone.
                    final_closures = dict.fromkeys(sorted(spaces - closed.keys()), time.time())
                    if final_closures:
                        note(closed=list(final_closures.items()))
                        closed.update(final_closures)
                    left = survivors(spaces, closed)
                if left:
                    raise RuntimeError(f"{cause}, but {len(left)} process(es) remain in process groups or "
                                       "sessions it was seen using: " + ", ".join(
                                           f"PID {pid} (group {group}, session {sid})" for pid, group, sid in left)
                                       + "; reservation retained")
                # Confirmed, so recovery has nothing of this command to check.
                note(settled="stopped" if signum is not None else "stalled" if stalled is not None
                     else "timed-out" if expired is not None else "exited")
                if signum is not None: raise Interrupted(signum, "after its command and observed descendants ended")
                if canceled:
                    raise Canceled(f"Owned command was canceled: {command[0]}; it and its observed descendants ended")
                if stalled is not None:
                    raise Stalled(f"Owned command stalled: {command[0]}; {stalled}; it and its observed descendants ended")
                if expired is not None:
                    raise TimedOut(f"Owned command timed out after {timeout:g}s: {command[0]}; it and its observed "
                                   "descendants ended")
                return result
            if killed is not None and time.monotonic() - killed > KILL_WAIT:
                raise RuntimeError(f"{cause}, but owned descendants have no confirmed terminal result; "
                                   "reservation retained")
            if result is not None and expired is None and stopped is None:
                if exited is None: exited = time.monotonic()
                helpers = False
                if lingered is None:
                    unread = {pid for pid in live if (pid, known[pid][2]) not in lines}
                    if unread:
                        read = commands(unread)
                        lines.update({(pid, known[pid][2]): read.get(pid) for pid in unread})
                    # Xcode's own programs left to launchd need no grace; anything else beside them keeps it.
                    helpers = all(xcode_helper(current[pid][0], lines[(pid, known[pid][2])]) for pid in live)
                overdue = time.monotonic() - exited - (0 if helpers or lingered is not None else LINGER)
                if overdue >= 0:
                    # SIGTERM once for each process, then SIGKILL, which cannot be caught, until it is gone.
                    fresh = {pid for pid in live if (pid, known[pid][2]) not in stopping}
                    if stopping: fresh = unowned(fresh, identities)
                    if fresh: print(describe(fresh, known, child.pid, kill_after, helpers), file=sys.stderr, flush=True)
                    stopping.update((pid, known[pid][2]) for pid in fresh)
                    # Timed from that SIGTERM, not from LINGER: a sample a busy host delays past
                    # LINGER and kill_after together must still send SIGTERM first (#90).
                    if lingered is None: lingered = time.monotonic()
                    due = time.monotonic() - lingered - kill_after
                    kill = due >= 0 and not held(due, identities)
                    for pid in live if kill else fresh:
                        try: os.kill(pid, signal.SIGKILL if kill else signal.SIGTERM)
                        except (ProcessLookupError, PermissionError): pass
            time.sleep(0.05)
    finally:
        stop(child)


def stop(process):
    if process.poll() is not None:
        return
    for sig, limit in [(signal.SIGINT, 10), (signal.SIGTERM, 5), (signal.SIGKILL, 5)]:
        try:
            os.killpg(process.pid, sig)
        # macOS says EPERM for a group whose only member has ended and is not yet reaped.
        except (ProcessLookupError, PermissionError):
            pass
        try:
            process.wait(timeout=limit)
            return
        except subprocess.TimeoutExpired:
            pass
    raise RuntimeError(f"Owned process {process.pid} has no confirmed terminal result")


def bounded(command, *, cwd=None, timeout, pass_fds=(), kill_after=KILL_AFTER):
    """Run command in a session of its own, its output in a file, and return (status, output): status
    is None once it ran past timeout seconds, after which it was stopped (end_tree). For many short
    commands side by side, such as test processes, where supervise's sampling would cost too much: the
    processes are listed only once the timeout has expired, so a descendant that left the root's tree
    before then, as a daemon does, is not followed."""
    with tempfile.TemporaryFile("w+", errors="replace") as output:
        child = subprocess.Popen(command, cwd=cwd, stdout=output, stderr=subprocess.STDOUT,
                                 start_new_session=True, pass_fds=pass_fds)
        try:
            status = child.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            status = None
            try: end_tree(child, kill_after)
            except BaseException:
                stop(child)
                raise
        except BaseException:
            stop(child)
            raise
        output.seek(0)
        return status, output.read()


def end_tree(child, kill_after=KILL_AFTER):
    """Stop a timed-out root, its process group and every descendant listed below it by parent in its own group
    or session, as swiftpm-testing-helper runs below `swift test` (vocaro-engineering#387): SIGTERM, then SIGKILL
    after kill_after seconds, or as soon as each has ended. The descendants are listed before the first signal,
    so that one the root's end leaves to launchd is still known, and again at each poll, so that one born
    since is found below a live one. Each is known by PID and start, and a PID that names another process
    by then is never signaled. RuntimeError when the root, or any such descendant, survives SIGKILL."""
    known, signaled = {}, set()

    def sweep(number):
        """List the processes, note the descendants, and send number to each one outside the root's group that has
        not had it yet (SIGKILL again each time); those still running, by PID."""
        current = snapshot()
        alive = lambda: {pid for pid, start in known.items() if current.get(pid, (0, 0, None))[2] == start}
        # Unreaped, the root keeps its PID, so that the snapshot's listing under it is the root's own.
        roots = alive() | ({child.pid} if child.returncode is None else set())
        known.update({pid: row[2] for pid, row in descendants(current, roots).items()})
        outside = {pid for pid in alive() if current[pid][1] != child.pid}
        for pid in outside:
            if number == signal.SIGKILL or (pid, known[pid]) not in signaled:
                with contextlib.suppress(ProcessLookupError, PermissionError): os.kill(pid, number)
                signaled.add((pid, known[pid]))
        return outside

    # Listed first: SIGTERM can end the root and leave its other descendants to launchd.
    outside = sweep(signal.SIGTERM)
    with contextlib.suppress(ProcessLookupError, PermissionError): os.killpg(child.pid, signal.SIGTERM)
    deadline = time.monotonic() + kill_after
    while time.monotonic() < deadline:
        with contextlib.suppress(subprocess.TimeoutExpired): child.wait(timeout=0.2)
        outside = sweep(signal.SIGTERM)
        if child.returncode is not None and not outside: break
    # The root may have gone on SIGTERM and left the rest of its group running.
    with contextlib.suppress(ProcessLookupError, PermissionError): os.killpg(child.pid, signal.SIGKILL)
    deadline = time.monotonic() + KILL_WAIT
    while outside := sweep(signal.SIGKILL):
        if time.monotonic() >= deadline:
            raise RuntimeError(f"Timed-out command {child.pid}'s descendant(s) PID "
                               f"{', '.join(map(str, sorted(outside)))} survived SIGKILL")
        time.sleep(0.05)
    child.wait(timeout=KILL_WAIT)


def run(command, *, cwd, log_path, timeout=None, env=None, on_stop=None, kill_after=KILL_AFTER, cancel=None,
        stall=None, progress_path=None):
    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if stall is not None:
        from .progress import Monitor
        monitor = Monitor(stall, path, progress_path)
    else:
        monitor = None
    with path.open("w") as log:
        try:
            result = supervise(command, cwd=cwd, stdout=log, timeout=timeout, env=env, on_stop=on_stop,
                               kill_after=kill_after, cancel=cancel, **({"progress": monitor} if monitor else {}))
        except BaseException as error:
            if monitor is not None:
                monitor.finish(type(error).__name__, settled=isinstance(error, (TimedOut, Interrupted, NotStarted)))
            raise
        else:
            if monitor is not None: monitor.finish(f"exit-{result}")
            return result
