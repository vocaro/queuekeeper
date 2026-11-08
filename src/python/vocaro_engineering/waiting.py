"""Waits for script tests that a busy host slows but does not fail (#284; guides/app-testing.md, Waits bound stalls).

A test that waits on a child process waits for the state it checks, and fails only once that state's progress
has stood still for a bound stretched by the host's load. A fixed wall-clock bound on the whole run fails on a
loaded host: on 2026-11-07, at load averages of 70 to 112, a BreezEPUB script test whose run takes 2 s on an
idle Mac outlived a 30 s `communicate` while it was still working (vocaro/BreezEPUB 5ffdf3d5).
"""
import ast
import os
from pathlib import Path
import subprocess
import time

# A literal bound of this many seconds or more on a wait in a test reads as one on the work, not on a cleanup.
FIXED_LEAST = 20
# The calls that wait on a child process or an event with a `timeout=`: subprocess's and Popen's, and threading's.
WAITS = {"communicate", "wait", "run", "check_output", "check_call", "call"}
EXEMPT = "wall-clock:"
SHOWN = 5
STALL = 15  # seconds without progress, before stretching; guides/app-testing.md, Waits bound stalls: "15 s has served"


def stretched(seconds):
    """These seconds stretched by the host's one-minute load per core, as a bound on a stall is (#128).

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
    return seconds * max(1, os.getloadavg()[0] / (os.cpu_count() or 1))


def wait_until(done, progress=None, stall=STALL, interval=0.1):
    """Wait until done() is true; return False once progress() has returned the same value for stretched(stall).

    Without progress, the stall is the whole wait, a stretched deadline. Pass every sign the work is moving, such
    as files(...) and cpu(...) combined. The stretch is read again at each look, so a load that rises during
    the wait lengthens it. done() is asked once more after a stall, so work that ends on the last look counts."""
    last, still = None, time.monotonic()
    while not done():
        state = progress() if progress else None
        if progress and state != last:
            last, still = state, time.monotonic()
        elif time.monotonic() - still > stretched(stall):
            return bool(done())
        time.sleep(interval)
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


def fixed_waits(root):
    """Each tracked test file's line that bounds a wait by FIXED_LEAST or more literal wall-clock seconds, as
    (path, line number): a literal `timeout=` on a call in WAITS, or a `monotonic() + N` deadline. A bound written
    with `stretched(...)` or a name is not literal, and a call or deadline that says why it is fixed with a
    `# wall-clock: REASON` comment on one of its lines is left out. A file that does not parse is skipped:
    its own tests fail on it."""
    listed = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--", "*.py"], capture_output=True,
                            stdin=subprocess.DEVNULL)
    if listed.returncode:
        return []
    def literal(node):
        return isinstance(node, ast.Constant) and type(node.value) in (int, float) and node.value >= FIXED_LEAST
    found = []
    for name in sorted(filter(None, listed.stdout.decode().split("\0"))):
        path = Path(name)
        # Recorded originals under evidence/ are kept as they ran, not maintained.
        if not (path.name.startswith("test") or {"tests", "test"} & set(path.parts[:-1])) or "evidence" in path.parts:
            continue
        try:
            text = (Path(root) / path).read_text(errors="replace")
            tree = ast.parse(text)
        except (OSError, SyntaxError, ValueError):
            continue
        lines = text.splitlines()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                called = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)
                fixed = called in WAITS and any(word.arg == "timeout" and literal(word.value) for word in node.keywords)
            elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add) and isinstance(node.left, ast.Call):
                clock = node.left.func
                fixed = (getattr(clock, "attr", None) or getattr(clock, "id", None)) == "monotonic" and literal(node.right)
            else:
                continue
            if fixed and not any(EXEMPT in line for line in lines[node.lineno - 1:node.end_lineno]):
                found.append((name, node.lineno))
    return sorted(set(found))


def doctor_warnings(root):
    """eng doctor's warning for fixed wall-clock waits in a project's tests (#284)."""
    found = fixed_waits(root)
    if not found:
        return []
    shown = ", ".join(f"{name}:{number}" for name, number in found[:SHOWN])
    more = f" and {len(found) - SHOWN} more" if len(found) > SHOWN else ""
    return [f"{len(found)} test line(s) bound a wait by a fixed {FIXED_LEAST} s or more of wall-clock time, which a "
            f"busy host can use up while the work still progresses: {shown}{more}. Wait on the state the test "
            "checks with vocaro_engineering.waiting.wait_until and a progress probe, or stretch the bound with "
            f"waiting.stretched; a bound that is the behavior under test says so with `# {EXEMPT} REASON` "
            "(guides/app-testing.md, Waits bound stalls)"]
