"""Bounded supervision of the process group this invocation creates."""
import contextlib
import os
import json
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

# Seconds an observed descendant may outlive the root before it is stopped, and then before
# SIGTERM becomes SIGKILL. Xcode's DTServiceHub can outlive xcodebuild by minutes, ignoring SIGTERM.
LINGER, KILL_AFTER = 60, 10
# Seconds a stopped command's processes have after SIGKILL before the run is uncertain.
KILL_WAIT = 5
# Seconds beyond KILL_AFTER that SIGKILL waits while a live admission owner is among the processes,
# as a nested eng admit in a light lane's script is: it stops its own command, with a KILL_AFTER
# and KILL_WAIT of its own, then settles its guard entry, which SIGKILL would strand. It also
# bounds an owner that never settles.
SETTLE_WAIT = 30
# The signals forwarding_signals carries out as a supervised stop. SIGKILL cannot be caught.
STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
_stops = []


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


def session(pid):
    """A live process's session, or None once it has ended. ps -o sess prints 0 on macOS."""
    try: return os.getsid(pid)
    except OSError: return None


def survivors(spaces):
    """Every live process in these process groups and sessions, as (pid, group, session).

    The command and every process it was seen with have ended, and the kernel never gives a new
    process a PID that still names a group or session. An ID that is a live process's PID was
    therefore taken over after the command's group or session ended, and is skipped.
    """
    current = snapshot()
    spaces = spaces - current.keys()
    found = []
    for pid, (_parent, group, _birth) in current.items():
        sid = session(pid)
        if group in spaces or sid in spaces: found.append((pid, group, sid))
    return found


def describe(pids, known, root):
    """The processes still running after the root, with the parent and group each was first seen with."""
    pids = sorted(pids)
    listing = subprocess.run(["ps", "-o", "pid,ppid,pgid,lstart,args", "-p", ",".join(map(str, pids))],
                             capture_output=True, text=True).stdout.rstrip()
    seen = "; ".join(f"{pid} with parent {known[pid][0]}, group {known[pid][1]}" for pid in pids)
    return (f"supervise: stopping {len(pids)} process(es) still running {LINGER:g}s after the command "
            f"(PID {root}) exited, with SIGTERM, then SIGKILL after {KILL_AFTER:g}s; first seen: {seen}\n{listing}")


def owners(identities, environment):
    """Those of these {PID: start} processes that the environment's admission guard records as owners."""
    from vocaro_engineering.resources import guard_owners
    recorded = guard_owners(environment)
    return sorted(pid for pid, start in identities.items() if (pid, " ".join(start.split())) in recorded)


class NotStarted(OSError):
    """The command could not be started, so it left no process to wait for."""


class Interrupted(Exception):
    """A stop signal ended the run, leaving none of its observed processes running."""

    def __init__(self, signum, detail):
        super().__init__(f"Stopped by {signal.Signals(signum).name} {detail}")
        self.signum = signum


class TimedOut(TimeoutError):
    """The run's timeout expired, and then the command and every observed descendant ended."""


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


def supervise(command, *, cwd, stdout=None, timeout=None, env=None, on_stop=None):
    """Wait for the owned root and observed descendants, including separate process groups.

    PID start identities prevent signaling a reused PID. An uncertain run raises so admission
    retains its orphan guard. A confirmed terminal exit keeps its status. A run that fails, is
    stopped or times out is confirmed only once a census of the process groups and sessions it was
    seen using finds nothing left in them.
    Descendants still running LINGER seconds after the root exits are reported on stderr and
    stopped; the root's status stands once they are gone, and one that survives SIGKILL is
    still awaited. A timeout bounds only the root: one that exits in time keeps its status
    through that grace. A command that cannot start raises NotStarted, which admission releases.

    A stop that forwarding_signals records calls on_stop, then goes to the root's group and to
    each observed descendant, becoming SIGKILL after KILL_AFTER seconds. Once
    the root and every observed descendant have ended, the run raises Interrupted, which
    admission releases; any still running KILL_WAIT seconds after SIGKILL leave it uncertain.
    A timeout that expires while the root runs is carried out the same way with SIGTERM, without
    on_stop, and the run raises TimedOut, which admission also releases.
    Any SIGKILL waits up to SETTLE_WAIT seconds more while the root or an observed descendant is
    an admission owner the guard records: a nested admission stops its own command and then
    settles its entry, which SIGKILL would strand. After the first SIGTERM or stop signal, such an
    owner's new children are left to it.
    """
    if requested() is not None:
        raise Interrupted(requested(), "before its command started")
    environment = dict(os.environ) if env is None else env
    token = json.loads(environment.get("VOCARO_RESOURCE_TOKEN", "{}"))
    descriptors = tuple(item["fd"] for item in token.get("locks", []))
    try:
        child = subprocess.Popen(command, cwd=cwd, env=environment, stdout=stdout,
            stderr=subprocess.STDOUT if stdout is not None else None, start_new_session=True,
            pass_fds=descriptors)
    except (OSError, ValueError) as error:
        # Popen reaps a child whose exec failed before it raises; nothing else ran.
        raise NotStarted(f"Command did not start: {error}") from error
    # Each observed descendant's (parent, group, start) when first seen, for its identity and report.
    known, born, exited, stopping = {}, None, None, set()
    # The process groups and sessions the command was seen using, for the census at its end. The
    # root's own are its PID. Never every observed PID: the PID space can wrap during a long run.
    spaces = {child.pid}
    started = time.monotonic()
    # When the timeout expired and when a stop was first seen, whether the signal reached the
    # root's group, and who else had it.
    expired, stopped, group_forwarded, forwarded = None, None, False, set()
    # Whether the stop's or timeout's first signal went out, and when its SIGKILL began.
    signaled, killed = False, None
    # Each process seen as an admission owner, as (PID, start), and the PIDs SIGKILL was reported waiting for.
    recognized, reported = set(), set()

    def recognize(identities):
        """Those of these {PID: start} processes that are admission owners. One that has settled
        its guard entry is an owner until it ends."""
        recognized.update((pid, identities[pid]) for pid in owners(identities, environment))
        return {pid for pid, start in recognized if identities.get(pid) == start}

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
        while True:
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
            while True:
                added = {pid for pid, (parent, _group, _birth) in current.items()
                         if parent in parents and pid not in parents}
                if not added: break
                parents.update(added)
                known.update({pid: current[pid] for pid in added})
            live = {pid for pid, seen in known.items() if pid in current and current[pid][2] == seen[2]}
            for pid in live: spaces.update(space for space in (current[pid][1], session(pid)) if space is not None)
            identities = {pid: known[pid][2] for pid in live} | ({child.pid: born} if root_active else {})
            if timeout is not None and exited is None and expired is None and time.monotonic() - started >= timeout:
                expired = time.monotonic()
                print(f"supervise: timed out after {timeout:g}s; sending SIGTERM to the command (PID {child.pid}) "
                      f"and its observed descendants, then SIGKILL after {KILL_AFTER:g}s", file=sys.stderr, flush=True)
            signum = requested()
            if signum is not None and stopped is None:
                stopped = time.monotonic()
                # First, in case SIGKILL follows before the processes are gone.
                if on_stop is not None: on_stop()
                print(f"supervise: {signal.Signals(signum).name} received; forwarding it to the command "
                      f"(PID {child.pid}) and its observed descendants, then SIGKILL after {KILL_AFTER:g}s",
                      file=sys.stderr, flush=True)
            ending = min(moment for moment in (expired, stopped) if moment is not None) \
                if expired is not None or stopped is not None else None
            if ending is not None:
                # A later signal does not hasten SIGKILL: a task stop can deliver its SIGTERM twice.
                due = time.monotonic() - ending - KILL_AFTER
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
                     else f"Timed out after {timeout:g}s" if expired is not None
                     else f"{command[0]} exited with status {result}")
            if result is not None and not live:
                # Sampling cannot prove that a failed, stopped or timed-out command orphaned nothing
                # between two samples, so its groups and sessions must hold nothing. A passing
                # command has no census: a daemon it starts on purpose must not stop the host.
                left = survivors(spaces) if result or ending is not None else []
                if left:
                    raise RuntimeError(f"{cause}, but {len(left)} process(es) remain in process groups or "
                                       "sessions it was seen using: " + ", ".join(
                                           f"PID {pid} (group {group}, session {sid})" for pid, group, sid in left)
                                       + "; reservation retained")
                if signum is not None: raise Interrupted(signum, "after its command and observed descendants ended")
                if expired is not None:
                    raise TimedOut(f"Owned command timed out after {timeout:g}s: {command[0]}; it and its observed "
                                   "descendants ended")
                return result
            if killed is not None and time.monotonic() - killed > KILL_WAIT:
                raise RuntimeError(f"{cause}, but owned descendants have no confirmed terminal result; "
                                   "reservation retained")
            if result is not None and expired is None and stopped is None:
                if exited is None: exited = time.monotonic()
                overdue = time.monotonic() - exited - LINGER
                if overdue >= 0:
                    # SIGTERM once for each process, then SIGKILL, which cannot be caught, until it is gone.
                    fresh = {pid for pid in live if (pid, known[pid][2]) not in stopping}
                    if stopping: fresh = unowned(fresh, identities)
                    if fresh: print(describe(fresh, known, child.pid), file=sys.stderr, flush=True)
                    stopping.update((pid, known[pid][2]) for pid in fresh)
                    kill = overdue >= KILL_AFTER and not held(overdue - KILL_AFTER, identities)
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
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=limit)
            return
        except subprocess.TimeoutExpired:
            pass
    raise RuntimeError(f"Owned process {process.pid} has no confirmed terminal result")


def run(command, *, cwd, log_path, timeout=None, env=None, on_stop=None):
    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as log:
        return supervise(command, cwd=cwd, stdout=log, timeout=timeout, env=env, on_stop=on_stop)
