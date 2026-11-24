"""Waits for script tests that a busy host slows but does not fail (#284).

Use a progress-bounded wait for work of variable length and a stretched timeout for a short command's hang
guard. On 2026-11-07, at load averages of 70 to 112, a BreezEPUB script test whose run takes 2 s on an idle Mac
outlived a fixed 30 s `communicate` while it was still working (vocaro/BreezEPUB 5ffdf3d5).
"""
import ast
import functools
import os
from pathlib import Path
import re
import subprocess
import threading
import time

from .resources import cpu_busy, slot_directory

# A literal `timeout=` of this many seconds or more on a wait in a test reads as one on the work, not on a cleanup.
FIXED_LEAST = 20
# The calls that wait on a child process or an event with a `timeout=`: subprocess's and Popen's, and threading's.
WAITS = {"communicate", "wait", "run", "check_output", "check_call", "call"}
EXEMPT = "wall-clock:"
# The stretch a project that cannot import Queuekeeper writes itself, from the standard library: the load per core,
# at least 1, which stretched reads too, without its doubt of a low load beside a saturated CPU (#342).
STDLIB_STRETCH = "max(1, os.getloadavg()[0] / os.cpu_count())"
# A literal `monotonic() + N` deadline in a script that does not parse; not `+ 2 * LIMIT`, whose bound is a name.
DEADLINE = re.compile(r"monotonic\(\)\s*\+\s*(?:\d+(?:\.\d*)?|\.\d+)(?![\w.]|\s*[*/])")
SHOWN = 5
STALL = 15  # seconds without progress, before stretching, a bound that has served


# A CPU busy share at or above SATURATED percent leaves no idle time to speak of, so the host's run queue is at least
# its core count, and a one-minute load under one per core beside it is a reading not to believe: build-host's
# getloadavg read 951 and then 307 27 s later, and 7.45 and then 250 12 s later, each time with the CPU 99 to 100%
# busy, which a true one-minute average cannot do (vocaro/queuekeeper#43). Such a load, or one that cannot be read,
# stretches by DOUBTED_STRETCH instead: 120 s for a 5 s bound, as voiceover state's STRETCH_LIMIT allows.
SATURATED = 95.0
DOUBTED_STRETCH = 24


@functools.lru_cache(maxsize=None)
def _slots(named):
    """Admission's slot directory, once for each HEAVY_SLOT_DIR: macOS names its default by a getconf call."""
    return slot_directory()


def busy_now():
    """The CPU's busy share in percent as admission reads it, from the samples every process on the host keeps
    (resources.cpu_busy, without waiting), or None when it cannot be read yet."""
    try:
        return cpu_busy(_slots(os.environ.get("HEAVY_SLOT_DIR")))[0]
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError):
        return None


def stretch_reading():
    """What a stretched bound is stretched by now: a dict of the one-minute load (`load`, None when it cannot be
    read), the core count (`cores`), the CPU's busy share in percent (`busy`, read only when the load is under one
    per core, else None) and the factor these give (`factor`), which a run records as the reading its bounds used.

    The factor is the load per core, at least 1. The load decides it rather than the busy share, which admission
    judges by (the owner's decision, 2026-11-06, vocaro-engineering#200), because a busy share stops at 100% and a
    bound must stretch by how far the CPU is oversubscribed beyond that: at loads of 145 to 590 on 18 cores, work
    that takes seconds on an idle Mac outlived bounds of 20 to 120 s (#81). The busy share checks a low load
    instead: at SATURATED or more, the load is doubted and the factor is DOUBTED_STRETCH (vocaro/queuekeeper#43),
    as it is for a load that cannot be read."""
    cores = os.cpu_count() or 1
    try:
        load = os.getloadavg()[0]
    except OSError:
        load = None
    busy = busy_now() if load is not None and load < cores else None
    if load is None or (busy is not None and busy >= SATURATED):
        factor = DOUBTED_STRETCH
    else:
        factor = max(1, load / cores)
    return {"load": None if load is None else round(load, 2), "cores": cores, "busy": busy, "factor": factor}


def stretch_words(reading):
    """A reading from stretch_reading, as a run's evidence says it."""
    load = "a one-minute load that could not be read" if reading["load"] is None \
        else f"a one-minute load of {reading['load']:g} on {reading['cores']} cores"
    busy = "" if reading["busy"] is None else f" with the CPU {reading['busy']:g}% busy"
    return f"stretched {reading['factor']:g} times, by {load}{busy}"


def stretched(seconds):
    """These seconds stretched by the host's one-minute load per core, as a bound on a stall is (#128), or by
    DOUBTED_STRETCH when that load reads under one per core beside a saturated CPU (stretch_reading).

    A test's wall-clock bound that only catches a hang, such as a subprocess's `timeout=`, an `--limit`, a
    wait for an event or a ceiling on elapsed time, takes this, and so does the life of a stand-in process
    that must outlive its test: at loads of 145 to 590 on 18 cores, work that takes seconds on an idle Mac
    outlived fixed bounds of 20 to 120 s (#81, #90, #111, #119, #120, #121). A bound that is the behavior
    under test stays fixed, with a comment, and stretches only its control (db166cf). These stay fixed for a
    reason stated here rather than at every site: an `Admission(timeout=0)`, which asks once and does not
    wait; a lower bound on elapsed time, which load only lengthens; a hang's stand-in that sleeps 60 s or
    more, which a fixed bound under test stops at a second or so, or which a quick in-process test kills; a
    take or slot refused before it waits; and a timeout a fake receives or a test reads back, which no clock
    enforces. The stretch is finite, so a test that really hangs still fails at it (test_host_isolation)."""
    return seconds * stretch_reading()["factor"]


def stretched_clock(clock=time.monotonic, factor=None, every=0.25):
    """A clock like clock that runs slower by the stretch (stretched), read again once every seconds of clock have
    passed since the last reading, so that a bound measured on it lengthens while a load that rises during the wait
    lasts, as wait_until's does. A bound stretched once, when it is set, keeps the factor of that moment: on
    2026-11-19 four `qk doctor` tests set their 60 s census and their toolkit update's limits at a load of 15 to 25
    on 18 cores, a factor of at most 1.4, and the load read 547 a minute later, so those ran out (vocaro/queuekeeper#306
    to #309).

    factor, a callable, gives the stretch, stretch_reading's factor by default; it is read under a lock and never
    below 1, so the clock never runs fast and its readings never go back. Each interval since the last reading
    counts at the factor read last, which is the newest. A reading that itself asks the time, as when the clock is
    subprocess's own and the reading runs a command, gets it at the factor before. The stretch is finite, so a bound
    on it still ends."""
    factor = factor or (lambda: stretch_reading()["factor"])
    lock = threading.RLock()
    began = clock()
    state = {"real": began, "value": began, "read": began, "factor": max(1, factor())}

    def now():
        with lock:
            real = clock()
            if real - state["read"] >= every:
                state["read"] = real
                state["factor"] = max(1, factor())
            real = max(real, state["real"])
            state["value"] += (real - state["real"]) / state["factor"]
            state["real"] = real
            return state["value"]
    return now


def wait_until(done, progress=None, stall=None, interval=0.1, pause=None):
    """Wait until done() is true; return False once progress() has returned the same value for stretched(stall).

    Without progress, the stall is the whole wait, a stretched deadline. Pass every sign the work is moving, such
    as files(...) and cpu(...) combined. The stretch is read again at each look, so a load that rises during
    the wait lengthens it. done() is asked once more after a stall, so work that ends on the last look counts.
    stall defaults to STALL as it reads when the call is made, so setting waiting.STALL shortens every default
    wait (#388).

    Between looks the wait calls pause(interval), time.sleep by default. A pause may return early when done() may
    now hold, such as when the child process it waits on exits, so the wait notices at once rather than up to an
    interval and a progress probe later (#395). Otherwise it lasts the whole interval: once its child has exited,
    a pause whose done() still fails sleeps out the rest of the interval, so the wait does not sample in a busy
    loop."""
    stall = STALL if stall is None else stall
    pause = pause or time.sleep
    last, still = None, time.monotonic()
    while not done():
        state = progress() if progress else None
        if progress and state != last:
            last, still = state, time.monotonic()
        elif time.monotonic() - still > stretched(stall):
            return bool(done())
        pause(interval)
    return True


def files(*roots):
    """A progress probe: each file under roots, with its size and modification time. A root that does not exist
    yet reads as empty, and a file removed while it is listed is left out."""
    def state():
        found = []
        for root in roots:
            for directory, _, names in os.walk(root):
                for name in names:
                    try:
                        status = os.lstat(os.path.join(directory, name))
                    except FileNotFoundError:
                        continue
                    found.append((directory, name, status.st_size, status.st_mtime_ns))
        return sorted(found)
    return state


def cpu(*names, exclude=()):
    """A progress probe: the CPU time, by process, of every process whose command line holds one of names and
    none of exclude.

    Exclude a process that polls while it waits. `processes.supervise` looks at its command every 0.05 s, so a
    supervisor's CPU time grows while the work under it is hung: a stall check that counted BreezEPUB's settings
    wrapper caught a child that never exited after about 620 s instead of 5 s (2026-11-07). Time is `ps`'s, in
    hundredths of a second, so a process that works less than that between looks reads as still."""
    names, exclude = [str(name) for name in names], [str(name) for name in exclude]
    def state():
        listing = subprocess.run(["ps", "-A", "-o", "pid=,time=,command="], capture_output=True, text=True,
                                 stdin=subprocess.DEVNULL).stdout
        return sorted(tuple(line.split(None, 2)[:2]) for line in listing.splitlines()
                      if any(name in line for name in names) and not any(name in line for name in exclude))
    return state


def combined(*probes):
    """One progress probe from several: it changes when any of them does."""
    return lambda: tuple(probe() for probe in probes)


def literal(node, least=0):
    return isinstance(node, ast.Constant) and type(node.value) in (int, float) and node.value >= least


def wait_timeout(node):
    """The timeout expression on a recognized wait call, if present."""
    if not isinstance(node, ast.Call):
        return None
    called = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)
    if called in WAITS:
        return next((word.value for word in node.keywords if word.arg == "timeout"), None)


def forwarded_default(function):
    """A fixed timeout default forwarded directly to a wait in the function's own scope."""
    args = function.args
    positional = args.posonlyargs + args.args
    defaults = list(zip(positional[len(positional) - len(args.defaults):], args.defaults, strict=True))
    defaults.extend(zip(args.kwonlyargs, args.kw_defaults, strict=True))
    default = next((value for arg, value in defaults if arg.arg == "timeout" and literal(value, FIXED_LEAST)), None)
    if default is None:
        return None
    pending = list(function.body)
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        if isinstance(timeout := wait_timeout(node), ast.Name) and timeout.id == "timeout":
            return default
        pending.extend(ast.iter_child_nodes(node))
    return None


def wait_lines(tree, lines):
    """The line numbers of literal wait timeouts, their forwarded defaults, and literal monotonic deadlines."""
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            default = forwarded_default(node)
            if default is not None:
                signature_end = max(node.lineno, node.body[0].lineno - 1, default.lineno)
                if not any(EXEMPT in line for line in lines[node.lineno - 1:signature_end]):
                    found.add(default.lineno)
            continue
        if isinstance(node, ast.Call):
            fixed = literal(wait_timeout(node), FIXED_LEAST)
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add) and isinstance(node.left, ast.Call):
            clock = node.left.func
            fixed = (getattr(clock, "attr", None) or getattr(clock, "id", None)) == "monotonic" and literal(node.right)
        else:
            continue
        if fixed and not any(EXEMPT in line for line in lines[node.lineno - 1:node.end_lineno]):
            found.add(node.lineno)
    return found


def fixture_lines(tree):
    """The file's line numbers of the fixed waits inside its fixtures' source: a module-level string of more than
    one line that parses as Python, however the test goes on to write it out, as FAKE and RECORDER are. A shebang
    script that does not parse, such as a shell wrapper, is searched for a literal deadline. Text that is neither,
    and a one-line string, is data a test compares."""
    found = set()
    for node in tree.body:
        value = node.value if isinstance(node, (ast.Assign, ast.AnnAssign)) else None
        if not (isinstance(value, ast.Constant) and isinstance(value.value, str) and "\n" in value.value.strip()):
            continue
        lines = value.value.splitlines()
        try:
            numbers = wait_lines(ast.parse(value.value), lines)
        except (SyntaxError, ValueError):
            numbers = () if not value.value.startswith("#!") else [
                number for number, line in enumerate(lines, 1) if DEADLINE.search(line) and EXEMPT not in line]
        # A string written with \n escapes shares one line of the file.
        found.update(min(value.lineno + number - 1, value.end_lineno) for number in numbers)
    return found


def fixed_in(text):
    """The line numbers of this test file's fixed waits, in its own code and in its fixtures' source. A file that does
    not parse has none: its own tests fail on it."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return set()
    return wait_lines(tree, text.splitlines()) | fixture_lines(tree)


def fixed_waits(root):
    """Each tracked test file's fixed wall-clock wait, as (path, line number), as fixed_in finds them. A bound written
    with `stretched(...)` or a name is not literal. A literal default passed straight to a wait counts too.
    A short `timeout=` is left alone, since short cleanup timeouts are common. A deliberately fixed deadline or
    timeout can carry a `# wall-clock: REASON` comment (#308)."""
    listed = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--", "*.py"], capture_output=True,
                            stdin=subprocess.DEVNULL)
    if listed.returncode:
        return []
    found = []
    for name in sorted(filter(None, listed.stdout.decode().split("\0"))):
        path = Path(name)
        # Recorded originals under evidence/ are kept as they ran, not maintained.
        if not (path.name.startswith("test") or {"tests", "test"} & set(path.parts[:-1])) or "evidence" in path.parts:
            continue
        try:
            text = (Path(root) / path).read_text(errors="replace")
        except OSError:
            continue
        found += [(name, number) for number in fixed_in(text)]
    return sorted(found)


def doctor_warnings(root):
    """qk doctor's warning for fixed wall-clock waits in a project's tests (#284)."""
    found = fixed_waits(root)
    if not found:
        return []
    shown = ", ".join(f"{name}:{number}" for name, number in found[:SHOWN])
    more = f" and {len(found) - SHOWN} more" if len(found) > SHOWN else ""
    return [f"{len(found)} test line(s) bound a wait by fixed wall-clock time, a `monotonic() + N` deadline or a "
            f"`timeout=` of {FIXED_LEAST} s or more (including defaults passed to a wait), which a busy host can "
            f"use up while the work still progresses: {shown}{more}. Use queuekeeper.waiting.wait_until with a "
            "progress probe for long or variable work. Use waiting.stretched for a short command's hang guard; "
            "a project that cannot import Queuekeeper multiplies that bound by the load per core instead, "
            f"`{STDLIB_STRETCH}`, from the standard library. "
            f"A bound that is the behavior under test or a cleanup's grace period says so with `# {EXEMPT} REASON`"]
