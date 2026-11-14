"""Waits for script tests that a busy host slows but does not fail (#284; apple-engineering's guides/app-testing.md, Waits bound stalls).

A test that waits on a child process waits for the state it checks, and fails only once that state's progress
has stood still for a bound stretched by the host's load. A fixed wall-clock bound on the whole run fails on a
loaded host: on 2026-11-07, at load averages of 70 to 112, a BreezEPUB script test whose run takes 2 s on an
idle Mac outlived a 30 s `communicate` while it was still working (vocaro/BreezEPUB 5ffdf3d5).
"""
import ast
import functools
import os
from pathlib import Path
import re
import subprocess
import time

from .resources import cpu_busy, slot_directory

# A literal `timeout=` of this many seconds or more on a wait in a test reads as one on the work, not on a cleanup.
FIXED_LEAST = 20
# The calls that wait on a child process or an event with a `timeout=`: subprocess's and Popen's, and threading's.
WAITS = {"communicate", "wait", "run", "check_output", "check_call", "call"}
EXEMPT = "wall-clock:"
# A literal `monotonic() + N` deadline in a script that does not parse; not `+ 2 * LIMIT`, whose bound is a name.
DEADLINE = re.compile(r"monotonic\(\)\s*\+\s*(?:\d+(?:\.\d*)?|\.\d+)(?![\w.]|\s*[*/])")
SHOWN = 5
STALL = 15  # seconds without progress, before stretching; apple-engineering's guides/app-testing.md, Waits bound stalls: "15 s has served"


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


def wait_lines(tree, lines):
    """The line numbers of this parsed source's fixed waits: a `timeout=` of FIXED_LEAST or more literal seconds on
    a call in WAITS, or a `monotonic() + N` deadline of any literal length. A call or deadline with a
    `# wall-clock: REASON` comment on one of its lines is left out."""
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            called = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)
            fixed = called in WAITS and any(word.arg == "timeout" and literal(word.value, FIXED_LEAST)
                                            for word in node.keywords)
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
    with `stretched(...)` or a name is not literal. A short `timeout=` is left alone, since short cleanup timeouts are
    common; a deadline rarely is one, and one that is says so with `# wall-clock: REASON` (#308)."""
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
            f"`timeout=` of {FIXED_LEAST} s or more, which a busy host can use up while the work still progresses: "
            f"{shown}{more}. Wait on the state the test checks with queuekeeper.waiting.wait_until and a "
            "progress probe, or stretch the bound with waiting.stretched; a bound that is the behavior under test, "
            f"or a cleanup's grace period, says so with `# {EXEMPT} REASON` "
            "(apple-engineering's guides/app-testing.md, Waits bound stalls)"]
