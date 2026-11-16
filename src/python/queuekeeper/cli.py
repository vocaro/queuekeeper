"""Queuekeeper's command: a merge queue and build farm for one developer's agents, on that developer's own Macs."""
import argparse
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
import time
import traceback

from . import pins, __version__, budgets, checkout, disk, docs, extensions, farm, git_hooks, git_state, host_health, lane_coverage, lane_report, lane_results, lane_wait, merge_queue, offload_host, \
    parallel, pre_push, preflight, processes, remote, resources, run_progress, shims, spelling
from .config import FILE as CONFIG_FILE, PROJECT_CHECKOUT_VARIABLE, TOOLKIT_ROOT, admission as admission_of, exclusive_refusal, extensions_of, lane_to_run, load, \
    source_identity, lane_devices, legacy_spelling_warning, legacy_unread_warning, project_checkout, \
    self_lane_warnings, sibling_folders, sibling_variables, stays, unadmitted, unlocked_screen
from .resources import BUSY, FREE_WAIT
from . import visibility

# The commands that reach the hosts, before each of which extensions.HOST_COMMAND runs.
HOST_COMMANDS = {"run", "admit", "remote", "land", "queue", "disk"}


def fix_hosts(root, hosts):
    """What `doctor` fixes of remote.compare_hosts' report itself, the owner's no longer (the owner's decision,
    2026-11-03): what each extension fixes (extensions.HOST_FIXES), and the second Mac's own toolkit checkout,
    which it fast-forwards to origin/main when it can and reports in toolkitCheckout, an error when it cannot
    (remote.update_there, vocaro-engineering#203). On the same connection it asks which folder beside the project
    that its extensions load from the second Mac has no checkout of, each an error naming the clone that makes it,
    which it leaves to the owner; unchecked when the second Mac is not reached (vocaro/queuekeeper#117).
    tests/host_isolation.py replaces it, so that no test changes either Mac."""
    for fix in extensions.HOST_FIXES: hosts = fix(root, hosts)
    siblings = sibling_folders(root)
    # The second Mac's own toolkit checkout, which it fast-forwards to origin/main when it can; one it must
    # leave as it is is an error, the owner's to decide (vocaro-engineering#203).
    if "remote" in hosts:
        found, missing = remote.update_there(hosts["host"], siblings)
        hosts["toolkitCheckout"] = found
        # Each folder the project's extensions load from that the second Mac has no checkout of, which every lane
        # sent there would fail to load; making it is the owner's step (vocaro/queuekeeper#117).
        if siblings and missing is None:
            hosts.setdefault("unchecked", []).append(
                f"whether {hosts['host']} has its own checkout of {', '.join(siblings)}, beside the project's checkout "
                "for its extensions to load from, which it did not report")
        for name in missing or []:
            hosts.setdefault("errors", []).append(
                f"{hosts['host']} has no ~/Development/{name}, which the project's extension loads from there, so "
                f"every lane sent there fails to load it; make it there with {remote.sibling_clone(root, name)} "
                "(guides/shared-host.md, Extension folders beside a project)")
        if found.get("state") == "unchecked":
            hosts.setdefault("unchecked", []).append(f"{hosts['host']}'s own toolkit checkout, which it did not "
                                                     f"report: {found.get('reason')}")
        else:
            line, owner = remote.toolkit_line(hosts["host"], found)
            if line: found["said"] = line
            if owner: hosts.setdefault("errors", []).append(line)
            elif line: print(f"remote: {line}", file=sys.stderr, flush=True)
    elif siblings:
        why = "none is configured" if hosts.get("host") is None else f"{hosts['host']} did not answer the comparison"
        hosts.setdefault("unchecked", []).append(
            f"whether the second Mac has its own checkout of {', '.join(siblings)}, beside the project's checkout for "
            f"its extensions to load from, which was not asked since {why}")
    return hosts


def split_warnings(root, config):
    """The gate over its budget and main past its release commit limit, as qk land warns after its result,
    in a project with a Git repository (budgets.py)."""
    try:
        common = subprocess.run(["git", "-C", str(root), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                                capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return []
    if common.returncode: return []
    return budgets.landing_warnings(root, config, budgets.entries(Path(common.stdout.strip()) / "queuekeeper-queue/entries"))


def refresh_policy(root, stream=None):
    """What the project's policy reader read before a run, for its receipt, or None when none is registered
    (extensions.POLICY)."""
    return extensions.policy(root, stream)


def record(directory, receipt, work):
    """Run work, then write its exit status or failure beside the source identities, and under load the load of this
    Mac while it ran (lane_report.RunLoad), however it ended (vocaro/queuekeeper#133)."""
    directory.mkdir(parents=True, exist_ok=True)
    load = lane_report.RunLoad().start()
    try:
        result = work()
        receipt["exitStatus"] = result
    except BaseException as error:
        # A timeout is a wall-clock backstop that a busy host can reach, not a verdict on the work.
        receipt["outcome"] = "unmeasured" if isinstance(error, processes.TimedOut) or getattr(error, "timed_out", False) \
            else "failed-or-interrupted"
        receipt["error"] = str(error)
        raise
    finally:
        receipt["load"] = load.finish()
        (directory / "source.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return result


def compile_failed(receipt):
    """Whether the run's "compileFirst" step ended with a failed build."""
    return receipt.get("compile", {}).get("outcome") == "failed"


def compile_first(lane, root, directory, receipt, limit, env, on_stop, kill_after):
    """A lane's "compileFirst" command, run inside the lane's one admission and environment, so that its xcodebuild or
    swift has the stale-module rebuild the lane's own has (shims.py). Returns (status, seconds left): the
    status of a failed build with None, its output staying in run.log, or None with what remains of limit for the
    lane's command, which shares the lane's one timeout. The step's output moves to compile.log once it has passed, so
    that run.log holds only the tests queuekeeper reads. A build that fails never reads as unmeasured, whatever status it
    exited with; one that reaches the limit raises TimedOut, which is. A lane without the step has its whole limit."""
    compiled = directory / "compile.log"
    compiled.unlink(missing_ok=True)
    command = lane.get("compileFirst")
    if command is None: return None, limit
    log = directory / "run.log"
    step = receipt["compile"] = {"command": command}
    began = time.monotonic()
    status = processes.run(command, cwd=root, log_path=log, timeout=limit, on_stop=on_stop, env=env, kill_after=kill_after)
    seconds = time.monotonic() - began
    step.update(exitStatus=status, seconds=round(seconds, 3))
    if status == 0:
        os.replace(log, compiled)
        step.update(outcome="passed", log=compiled.name)
        return None, max(limit - seconds, 1)
    step.update(outcome="failed", log=log.name)
    with log.open("a") as file:
        print(f"queuekeeper: the build failed: compileFirst exited {status}, and no test ran", file=file)
    print(f"queuekeeper: the build failed: compileFirst exited {status}, and no test ran; its log is {log}", file=sys.stderr, flush=True)
    return (1 if status == lane_results.UNMEASURED else status), None


def unstartable(command, cwd):
    """Why command cannot start in cwd, or None. An interpreter SCRIPT_RUNNERS names, whose first argument is no option
    and names nothing in cwd, has no file to run, as python3 had no scripts/qk in apple-engineering's checkout on
    2026-11-13 and exited 2, the status of a failure (vocaro/queuekeeper#108). The check reads the file system, not
    what the interpreter prints. A command the system cannot start at all raises processes.NotStarted instead."""
    if len(command) < 2 or not SCRIPT_RUNNERS.fullmatch(Path(command[0]).name): return None
    script = command[1]
    if script.startswith("-") or (Path(cwd) / script).exists(): return None
    return f"{command[0]} has no {script} to run in {cwd}"


def not_started(directory, receipt, lane, why):
    """Record that the lane's command could not start, why, in its receipt as notStarted and at the end of its log,
    which would otherwise hold an earlier run's, and return NOT_STARTED, which the lane then exits with."""
    receipt["notStarted"] = why
    words = f"queuekeeper: {lane} could not start: {why}"
    with contextlib.suppress(OSError), (directory / "run.log").open("a") as log: print(words, file=log)
    print(words, file=sys.stderr, flush=True)
    return lane_results.NOT_STARTED


def conclude(directory, receipt, status, **fields):
    """Record a completed run's verdict, the status qk run returns, beside its command's exit
    status: passed only for 0, so a zero exit that ran too few tests is failed, and not-started for a command that never
    started (not_started)."""
    outcome = "passed" if status == 0 else "unmeasured" if status == lane_results.UNMEASURED else \
        "not-started" if receipt.get("notStarted") else "build-failed" if compile_failed(receipt) else "failed"
    receipt.update(outcome=outcome, **fields)
    (directory / "source.json").write_text(json.dumps(receipt, indent=2) + "\n")


def judge(lane, result, log, extra=(), expect=None):
    """A lane's status and unmeasured tests from its command's status and its log. A completed run
    that executed too few tests raises ValueError, as does one that executed any count but its minimumTests when the
    lane says "exactTests": true, so that a count typed by hand cannot drift from the tests the lane selects
    (vocaro/queuekeeper#77)."""
    measured = result in (0, lane_results.UNMEASURED)
    not_run = []
    repeats = lane_results.repeats_asked(extra)
    # --expect counts one run's tests, and a series' log adds up all of its runs.
    expect = expect * repeats if repeats and expect else expect
    if repeats is not None and measured and lane_results.series_reported(log) != repeats:
        # The command ignored --repeat, or stopped before its series ended: however it ended, the series did not happen.
        cause = f"--repeat {repeats} was asked for, and the lane's log reports no series of {repeats} runs"
        print(f"queuekeeper: unmeasured: {cause}", file=sys.stderr)
        return lane_results.UNMEASURED, [cause]
    if measured and (lane.get("minimumTests") or expect):
        # Arguments such as a filter select part of the lane, which must still run a test.
        minimum = 1 if extra or expect else lane["minimumTests"]
        # The whole lane's count is exact where the lane says so, as --expect makes a filtered run's.
        exact = lane.get("exactTests") and not extra and not expect
        try:
            lane_results.require_tests(log, minimum, expected=minimum if exact else expect)
        except lane_results.NotRun as error:
            # Such as a corpus test canceled for a variable this lane's command does not set on its Mac
            # (vocaro-engineering#285). The lines give each test's reason: the cause an unmeasured run records.
            not_run = [str(error), *lane_results.canceled_tests(log)]
            print("queuekeeper: unmeasured:", *not_run, sep="\n", file=sys.stderr)
        except ValueError as error:
            # An unmeasured part may not have run at all, so the run stays unmeasured. A test the
            # host left unmeasured ran nothing, so it can also be what leaves a run short.
            if exact:
                # The figure to set, for a count that changed because the lane's tests did.
                observed = lane_results.executed_tests(log) or 0
                error = ValueError(f'{error}. The lane says "exactTests": true, so its minimumTests, {minimum}, is the '
                                   f"count of the tests it selects: if these {observed} are the tests it should run, set "
                                   f"its minimumTests to {observed}, and otherwise correct what it selects")
            if result == 0 and not lane_results.unmeasured_tests(log): raise error
            print(f"queuekeeper: {error}", file=sys.stderr)
    unmeasured = lane_results.unmeasured_tests(log) if measured else []
    if unmeasured or not_run or result == lane_results.UNMEASURED:
        # A skip or cancellation leaves the command's own status at 0, and unmeasured is never green.
        if unmeasured:
            print(f"queuekeeper: unmeasured: {len(unmeasured)} test(s) reported unmeasured:", *unmeasured,
                  sep="\n", file=sys.stderr)
        return lane_results.UNMEASURED, list(dict.fromkeys([*not_run, *unmeasured]))
    return result, []


def lane_command(lane, extra=()):
    """The command a lane runs, with extra appended, as the keys extensions add to a lane make it
    (extensions.lane_command), such as the Mac desktop extension's "display". The receipt records it so."""
    return extensions.lane_command(lane, [*lane["command"], *extra])


def lane_label(root, config, lane):
    """A lane run's admission label: its project's name, a space and the lane, such as `EPUBLib gate`,
    so that two projects' lanes of one name stay apart in waiting messages, the guard and the admission
    log (#51). A space, as in the labels callers pass, such as `warm push REV`, and in an `qk admit`
    command line; no configured project name or lane has one, and a colon would read as one more field
    after the `: ` that waiting messages put before a label. The name is queuekeeper.json's, else the
    checkout's; a linked worktree's directory is named for its session, so it takes its main checkout's."""
    name = config.get("name") or (git_state.main_checkout(root) or root).name
    return f"{name} {lane}"


def placement(reason):
    """Where a run ran, for its receipt, so that the share of heavy work each Mac ran can be counted:
    ranOn, `this Mac` or `second Mac`, the name of the Mac, and on this Mac hereBecause, why the run
    ran here rather than on the second Mac. Work the second Mac runs for this Mac says so, and names
    the Mac that sent it (remote.DISPATCHER)."""
    sender = os.environ.get(remote.DISPATCHER)
    if sender: return {"ranOn": "second Mac", "mac": offload_host.mac_name(), "dispatchedFrom": sender}
    return {"ranOn": "this Mac", "mac": offload_host.mac_name(), "hereBecause": reason}


def admission_placement(offered, reason):
    """What a run's admission line says of its placement, which `qk admissions usage` counts: whether queuekeeper offered it
    to the second Mac first (offered) and why it runs here (because), or the Mac that sent it (remote.DISPATCHER)."""
    sender = os.environ.get(remote.DISPATCHER)
    if sender: return {"dispatchedFrom": sender}
    # With no second Mac configured, Offer.make gives that reason before checking local eligibility.
    declined = offered is not None and (offered.host is None or offered.refused is None)
    return {"offered": declined, "because": reason}


def this_mac():
    """The Mac this queuekeeper runs on, as messages name it: `this Mac (NAME)`, or `the second Mac (NAME)` for work the
    second Mac runs for the Mac that sent it (remote.DISPATCHER), so that a log never leaves the Mac to be guessed
    (vocaro-engineering#185)."""
    return f"{'the second Mac' if os.environ.get(remote.DISPATCHER) else 'this Mac'} ({offload_host.mac_name()})"


# Seconds between readings of this Mac's screen while a lane that needs it unlocked waits for it.
SCREEN_POLL = 30
# A test's own report that a lock left it unmeasured, such as Pagepour's `Unmeasured: the screen is locked`.
SCREEN_LOCKED = re.compile(r"(?i)\bscreen is locked\b")


def screen_reading(stream=None):
    """This Mac's screen, read now (offload_host.screen) and said in one line naming the Mac (this_mac)."""
    found = offload_host.screen()
    print(f"queuekeeper: {offload_host.screen_line(found, this_mac())}", file=stream or sys.stderr, flush=True)
    return found


def scroll_bars_recorded(receipt, lane, inherited):
    """Record in a lane's receipt which scroll-bar kind this Mac gives as it starts, and say so in one line naming the
    Mac (offload_host.scroll_bars, vocaro-engineering#229). A lane's log says it once for its Mac: work nested in
    an admission or a self lane says nothing of its own, nor does the provision lane a dispatch runs before its work,
    though each still records the reading."""
    found = receipt["scrollBars"] = offload_host.scroll_bars()
    if inherited or os.environ.get(SELF_LANE) or (lane == "provision" and os.environ.get(remote.DISPATCHER)): return
    print(f"queuekeeper: {offload_host.scroll_bars_line(found, this_mac())}", file=sys.stderr, flush=True)


def screen_wait(name, seconds, said):
    """None once this Mac's screen reads unlocked, within seconds, for a lane that needs it unlocked; otherwise why
    not, after the wait. Each new reading that refuses is said once (said keeps the last); the lane waits instead
    of running unmeasured (vocaro-engineering#185)."""
    deadline = time.monotonic() + max(seconds, 0)
    while True:
        found = offload_host.screen()
        if found["state"] == "unlocked": return None
        refused, key = remote.screen_refusal(found, this_mac()), (found["state"], found.get("reason"))
        lane_wait.waiting(lane_wait.SCREEN, refused)
        if not said or said[-1] != key:
            print(f"queuekeeper: {name} waits: {refused}", file=sys.stderr, flush=True)
            said.append(key)
        left = deadline - time.monotonic()
        if left <= 0: return refused
        time.sleep(min(SCREEN_POLL, left))


def admitted_here(stack, name, label, timeout, offered=None, once=False, exclusive=False, model=False, whole_host=False,
                  screen=False, placement=None):
    """This Mac's heavy-work admission of the work label names, entered in stack, or the work's status
    instead: its status on the second Mac when the second Mac took it after all, MISCONFIGURED, or
    BUSY when no admission came, after saying so. Work the second Mac did not take (offered), declined
    as busy or not offered while it had no spare capacity and this Mac had, goes to whichever Mac admits
    it first, this Mac asked first (remote.overflow). Other work waits in this Mac's queue for timeout
    seconds, or at most FREE_WAIT with once. A whole-host lane takes every guaranteed slot,
    and every request keeps one place across local waits and its age across remote offers (resources.Place). A
    lane that needs an unlocked screen (screen) is admitted only while this Mac's screen reads unlocked, and waits
    for it as for a slot (screen_wait). Its admission line says placement (admission_placement)."""
    refused, said = [], []
    place = resources.Place()
    if offered is not None: place.started = getattr(offered, "started", place.started)
    # Its own place is no request queued ahead of it when queuekeeper reads this Mac's capacity for it (#186).
    if offered is not None: offered.place = place

    def admit(seconds):
        if screen:
            began = time.monotonic()
            locked = screen_wait(name, seconds, said)
            if locked is not None:
                refused.append(locked)
                return None
            seconds = max(seconds - (time.monotonic() - began), 0)
        try:
            # qk admit and qk run start every command they admit through processes.supervise (#315).
            return stack.enter_context(resources.Admission(label, exclusive=exclusive, timeout=seconds, model=model,
                                                           whole_host=whole_host, place=place, placement=placement,
                                                           supervised=True))
        except TimeoutError as error:
            # Admission published nothing but the place, which the finally below leaves.
            refused.append(error)
            return None
    try:
        if offered is not None and offered.busy is not None:
            return remote.overflow(offered, admit, timeout, once, refused)
        # A wait that runs out is BUSY, as with once: nothing ran, which the merge queue counts as no verdict.
        # Raised, TimeoutError is an OSError, which qk run reports as its own error, exit 2 (#268).
        admission = admit(min(FREE_WAIT, timeout) if once else timeout)
        if admission is None:
            print(f"queuekeeper: {name} not run on {this_mac()}: {refused[-1]}", file=sys.stderr)
            declined(refused[-1])
            return BUSY
        return admission
    except resources.AdmissionRecordError as error:
        print(f"queuekeeper: {name} not run on {this_mac()}: {error}", file=sys.stderr)
        return BUSY
    finally:
        # Admitted, admission took the place out of the queue; otherwise the work no longer waits here.
        if place is not None: place.leave()


def declined(reason):
    """Write why admission declined the work where the Mac that sent it reads it back, when it was sent here
    (offload_host.declined_reason); otherwise nothing. Nothing written stops the decline."""
    path = os.environ.get(offload_host.DECLINED)
    if not path: return
    with contextlib.suppress(OSError):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(f"{reason}\n")


def fell_back(name, offered):
    """Say in one line, before the run starts, that work offered to the second Mac runs on this Mac, and why, so
    that the second Mac's lines a declined offer left above are not read as the run's (vocaro-engineering#185)."""
    above = "; the second Mac's lines above are from the offer it did not take" if offered.sent else ""
    print(f"queuekeeper: {name} runs on {this_mac()}, not the second Mac: {offered.status}{above}", file=sys.stderr, flush=True)


def unmeasured_screen(name, receipt):
    """Read the screen again once a lane whose result depends on it ends unmeasured, and say on which Mac, so
    that the verdict and the hold it leads to name the Mac whose screen was read (vocaro-engineering#185)."""
    print(f"queuekeeper: {name} unmeasured on {this_mac()}", file=sys.stderr, flush=True)
    receipt["screenAfter"] = screen_reading()


SCREEN_STATUS = {"unlocked": 0, "locked": 1, "unknown": 2}


def host_screen(args):
    """`qk host screen [--remote]`: one line, or the reading as JSON, saying whether this Mac's or the second
    Mac's console screen is locked (offload_host.screen), exiting 0 unlocked, 1 locked and 2 unknown. The
    second Mac is read over SSH by the same code, so either Mac's answer is read, never assumed."""
    if args.host and not args.remote: raise ValueError("--host names the second Mac that --remote reads")
    if args.remote:
        host = remote.chosen(args.host)
        try:
            found = remote.screen_there(host)
        except remote.Unavailable as error:
            found = {"mac": host, "at": time.strftime("%H:%M:%S"), "state": "unknown",
                     "reason": f"{host} could not be read: {getattr(error, 'problem', None) or error}"}
        if not isinstance(found, dict) or found.get("state") not in SCREEN_STATUS:
            found = {"mac": host, "at": time.strftime("%H:%M:%S"), "state": "unknown",
                     "reason": f"{host} answered {found!r}"}
        where = f"the second Mac ({found.get('mac')})"
    else:
        found, where = offload_host.screen(), this_mac()
    print(json.dumps(found, indent=2) if args.json else offload_host.screen_line(found, where))
    return SCREEN_STATUS[found["state"]]


# Set to off, work that runs on this Mac does not reap first (reap_here). tests/host_isolation sets it, so that no
# run a test makes, in its process or a child, stops a simulator or releases a reservation of this Mac.
REAP_SWITCH = "QUEUEKEEPER_REAP_BEFORE_RUN"
# The status of a command that refused how it was called, as argparse's does: lane_report.before_tests leaves it as it
# is, since it already tells a refusal apart from a test's failure (vocaro-engineering#393).
USAGE = 2
# The interpreters whose first argument, when it is no option, is the file they run (unstartable).
SCRIPT_RUNNERS = re.compile(r"(python|pypy)(\d+(\.\d+)*)?|(ba|z|da|k)?sh")


# Where a run of a lane on this Mac keeps its log, receipt and results while another run of that lane from the same
# checkout holds .build/queuekeeper/LANE: .build/queuekeeper/RUNS/LANE/TIME-PID/, as each dispatch keeps its own
# (remote.DISPATCHES); and how many of them each lane keeps.
RUNS = ".runs"
RUNS_KEPT = remote.DISPATCHES_KEPT


def lane_directory(stack, root, lane):
    """Where this run of lane on this Mac keeps its log, receipt and results: .build/queuekeeper/LANE, which one run
    at a time holds, by an flock that stack releases when the run ends. A run that starts while another run of the
    lane from this checkout holds it keeps a directory of its own, named for its start and its process as a
    dispatch's is, and says so, and its summary names that directory's log and receipt (lane_report). Two runs of one
    lane here from one checkout shared the lane's: the second emptied the first's results and wrote over its log, so
    that the first read the second's tests as its own (vocaro/queuekeeper#9). Each lane keeps its newest RUNS_KEPT
    such directories and any whose process still runs, and each one removed is named."""
    base = Path(root) / ".build/queuekeeper"
    runs = base / RUNS / lane
    runs.mkdir(parents=True, exist_ok=True)
    lock = os.open(runs / ".lock", os.O_RDWR | os.O_CREAT, 0o644)
    stack.callback(os.close, lock)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return base / lane
    except BlockingIOError:
        own = runs / (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + f"-{os.getpid()}")
    own.mkdir(exist_ok=True)
    print(f"queuekeeper: another run of {lane} from this checkout is running on this Mac, so this run keeps its log, "
          f"receipt and results in {own}, not {base / lane}", file=sys.stderr, flush=True)
    for old in sorted(path for path in runs.iterdir() if path.is_dir())[:-RUNS_KEPT]:
        process = old.name.rpartition("-")[2]
        if process.isdigit() and lane_wait.alive(int(process)): continue
        shutil.rmtree(old, ignore_errors=True)
        print(f"queuekeeper: removed {old}, each lane keeping its newest {RUNS_KEPT} runs beside another", file=sys.stderr,
              flush=True)
    return own


def reap_here(root, inherited):
    """Before heavy work runs on this Mac, the reap that runs before work is sent to the second Mac (remote.execute):
    the stale simulators of the project's pool and of the pools its checkouts name shut down, and the pool
    reservations reap proves dead released (offload_host.reap, remote.reap_request), within REAP_SECONDS. Each stop,
    failure, release and problem goes to standard error as a dispatch says them (remote.reap_lines), and the work
    runs all the same; the lines are returned. Work a dispatch sent here was reaped by that dispatch, and work that
    inherited an admission (inherited) by the run it is nested in, so neither reaps again (vocaro-engineering#156)."""
    if os.environ.get(REAP_SWITCH, "").strip().lower() == "off": return []
    if inherited or os.environ.get(remote.DISPATCHER): return []
    # offload_host.reap never raises; it says what it could not read or do in its report's problem.
    report = offload_host.reap(remote.ROOT, **remote.reap_request(root))
    lines = remote.reap_lines(remote.HERE, report, listing=False)
    for line in lines: print(f"queuekeeper: {line}", file=sys.stderr, flush=True)
    return lines


def admitted_tree(stack, root, name, offered, receipt):
    """The checkout a lane this Mac admitted runs in, after recording in receipt the tree it reads, or 2 after
    saying why it cannot run. A run in root reads the working tree as it is once admitted, not as it was when the
    work was offered or queued, so a lane this Mac takes after sending the second Mac a commit runs in root only
    while the tree is still that commit's (remote.moved_trees), and receipt's sent names that commit.

    Once the project's tree is no longer what was sent, the lane runs from what was sent, in a temporary worktree
    at that commit with the toolkit beside it that QUEUEKEEPER_ROOT names, entered in stack, as `qk admit` runs its
    command (from_sent), and receipt's sentTree says so. It used to end with exit 2 there, after its wait for a
    slot: on 2026-11-13 a lane the second Mac declined waited 104 s here and then refused over a documentation
    edit the session made meanwhile (vocaro/queuekeeper#136).

    When only the toolkit's tree changed, the lane runs in root with the toolkit as sent, from a temporary worktree
    of it that QUEUEKEEPER_ROOT names, entered in stack (remote.sent_toolkit), and receipt's sentToolkit says so. A
    session editing Queuekeeper's checkout, or a landing that moved it, otherwise ended with exit 2 a gate that had
    waited here for a Mac (vocaro/queuekeeper#33)."""
    moved = remote.moved_trees(root, offered)
    if any(not what.startswith("toolkit") for _checkout, _commit, what, _changes in moved):
        tree = from_sent(stack, root, offered.sent, moved, receipt, name)
        if isinstance(tree, int): return tree
        stack.enter_context(patch_environment({"QUEUEKEEPER_ROOT": str(tree[1])}))
        return tree[0]
    for _checkout, commit, what, changes in moved:
        why = f"the {what} changed after {commit[:12]} was sent to the second Mac:\n{changes}"
        try: toolkit = stack.enter_context(remote.sent_toolkit(offered.sent))
        except (OSError, RuntimeError) as error:
            print(f"queuekeeper: {name} not run on this Mac: {why}\nqueuekeeper: and no worktree of the toolkit that was "
                  f"sent could be made here: {error}", file=sys.stderr, flush=True)
            return 2
        print(f"queuekeeper: {name} runs on this Mac with the toolkit that was sent, {commit[:12]}, from a temporary "
              f"worktree, {toolkit}, since {why}\nqueuekeeper: the toolkit's checkout stays as it is, and the worktree "
              "goes when the run ends", file=sys.stderr, flush=True)
        stack.enter_context(patch_environment({"QUEUEKEEPER_ROOT": str(toolkit)}))
        receipt["sentToolkit"] = {"toolkit": str(toolkit), "commit": commit, "changes": changes}
    receipt["project"] = source_identity(root)
    if offered is not None and offered.sent.get("commit"): receipt["sent"] = offered.sent["commit"]
    return root


def from_sent(stack, root, sent, moved, receipt, name="the command"):
    """(checkout, toolkit) of the worktrees from which `qk admit` runs its command on this Mac, or `qk run` the
    lane name, entered in stack, after recording them in receipt; or 2 after saying why not. Work this Mac takes
    after sending the second Mac a commit runs from that commit once the tree here is no longer that commit's
    (remote.moved_trees), in a temporary worktree at it, as the second Mac runs it, rather than from the changed
    tree or not at all: the session may go on editing meanwhile, and the result is that of what was sent
    (vocaro-engineering#276, whose command waited behind a gate here and then refused over an unrelated
    documentation edit; vocaro/queuekeeper#136, the same for a lane). The worktrees go when the work ends, however
    it ends (remote.sent_tree)."""
    why = "; ".join(f"the {what} changed after {commit[:12]} was sent to the second Mac:\n{changes}"
                    for _checkout, commit, what, changes in moved)
    try:
        checkout, toolkit = stack.enter_context(remote.sent_tree(
            root, sent, any(what.startswith("toolkit") for _checkout, _commit, what, _changes in moved)))
    except (OSError, RuntimeError) as error:
        print(f"queuekeeper: {name} not run on this Mac: {why}\nqueuekeeper: and no worktree of what was sent could be made "
              f"here: {error}", file=sys.stderr, flush=True)
        return 2
    print(f"queuekeeper: {name} runs on this Mac from what was sent, {sent['commit'][:12]}, in a temporary worktree, "
          f"{checkout}, since {why}\nqueuekeeper: the session's tree stays as it is, and the worktree goes when "
          f"{'the command' if name == 'the command' else 'the run'} ends", file=sys.stderr, flush=True)
    receipt["project"] = source_identity(checkout)
    receipt["sent"] = sent["commit"]
    receipt["sentTree"] = {"checkout": str(checkout), "toolkit": str(toolkit),
                           "moved": [{"tree": what, "commit": commit, "changes": changes}
                                     for _checkout, commit, what, changes in moved]}
    return checkout, toolkit


def matching_directory(root):
    """The current directory relative to the checkout root's top level, which the command runs in: "." outside it."""
    top = Path(remote.git(root, "rev-parse", "--show-toplevel")).resolve()
    try:
        return Path.cwd().resolve().relative_to(top)
    except ValueError:
        return Path(".")


def provision_sent(root, config, checkout, toolkit, variables, limit, on_stop=None):
    """0 once the project's provision lane, if it has one, has run in the worktree of what was sent, as it runs in
    the second Mac's checkout before the command; otherwise its status, after saying where its log is."""
    if "provision" not in config.get("lanes", {}): return 0
    log = Path(root) / ".build/queuekeeper/admit/provision.log"
    status = processes.run([sys.executable, str(toolkit / "bin/qk"), "--project", str(checkout), "run", "--here",
                            "provision"], cwd=checkout, log_path=log, timeout=limit, env=variables,
                            on_stop=on_stop)
    if status:
        print(f"queuekeeper: the command not run: the provision lane exited {status} in the worktree of what was sent; its "
              f"log is {log}", file=sys.stderr, flush=True)
    return status


def here_because(args, inherited):
    """Why `qk admit` runs its command on this Mac without offering it to the second Mac, or None to offer it there
    first, as a lane is (the owner's decision, 2026-11-07; guides/shared-host.md, Which Mac runs a lane)."""
    if args.here: return "qk admit --here"
    # Work the second Mac runs for the Mac that sent it runs there, where it was sent.
    if os.environ.get(remote.DISPATCHER): return "sent here by the Mac that dispatched it"
    # Foreground work holds the foreground of the Mac it runs on (foreground.py).
    if args.foreground: return "foreground work runs on the Mac whose foreground it holds"
    if inherited: return "nested in the admission it inherits"
    # The parts of a self lane share build products, so all run on the Mac where the lane runs (SELF_LANE).
    lane = os.environ.get(SELF_LANE)
    if lane: return f"a part of the self lane {lane}, whose parts share build products on the Mac where it runs"
    return None

def provisioned_here(root, config, lane, receipt, inherited, on_stop=None, tree=None):
    """None once the project's provision lane, if it has one, has run in this checkout before lane runs on this Mac,
    as it runs in the second Mac's checkout before a lane sent there (offload_host); otherwise the status lane ends
    with, after saying why it did not run. A bare `qk run gate` that stayed here ran only the gate, so it passed or
    failed by the Mac it landed on, and a queue gate that fell back here found a checkout its provision lane had
    prepared on the other Mac, or on none (vocaro/queuekeeper#19). It runs as `qk run --here provision`, a lane of
    its own with its own receipt and log, nested in the admission lane holds, or admitted itself before a self lane.
    Not before the provision lane itself, nor for work nested in an admission or a self lane, which the run around
    it provisioned, nor for work a dispatch sent here, which provisioned this checkout as a step of its own.

    A provision lane that fails here ends lane as one that fails on the second Mac does (remote.execute): with
    remote.MISCONFIGURED, this Mac's environment and no verdict on lane's commit, which the merge queue holds for
    and does not send back (the owner's decision, 2026-11-15). One that got no admission or ended unmeasured ran
    nothing to judge, and lane ends as it did.

    tree is the worktree of what was sent, when lane runs from it (admitted_tree): the provision lane runs there, as
    it does before `qk admit`'s command (provision_sent). Its own log goes with the worktree, so what qk run said
    of it, kept in root, is the log named."""
    if lane == "provision" or "provision" not in config.get("lanes", {}): return None
    if inherited or os.environ.get(SELF_LANE) or os.environ.get(remote.DISPATCHER): return None
    # With the toolkit the lane runs with: the one that was sent, once its checkout has moved (admitted_tree).
    toolkit = Path(receipt.get("sentTree", {}).get("toolkit") or receipt.get("sentToolkit", {}).get("toolkit")
                   or TOOLKIT_ROOT)
    said = root / ".build/queuekeeper" / lane / "provision.log"
    sent, root = tree is not None, root if tree is None else tree
    with resources.child_requests():
        status = processes.run([sys.executable, str(toolkit / "bin/qk"), "--project", str(root), "run", "--here",
                                "provision"], cwd=root, log_path=said, on_stop=on_stop)
    if not status:
        # The provision lane reported into this lane's wait record, as on the second Mac (lane_wait.begin); the
        # record is this process's again, whose start a stacked gate reads (merge_queue.Worker.admitted_mac).
        lane_wait.running()
        return None
    log = said if sent else root / ".build/queuekeeper/provision/run.log"
    if status in (BUSY, lane_results.UNMEASURED):
        print(f"queuekeeper: {lane} not run on this Mac: its provision lane gave no verdict here (exit {status}); what "
              f"qk run said of it is in {said}", file=sys.stderr, flush=True)
        return status
    print(f"queuekeeper: {lane} not run: {this_mac()} {remote.UNPROVISIONED}: the provision lane exited {status} in "
          f"{f'the worktree of what was sent, {root}' if sent else root}, so {lane} did not run here; its log is {log}\n"
          f"queuekeeper: to fix it, give this Mac what provisioning needs, which that log names, and run `qk run {lane}` "
          f"again; what qk run said of the provision lane is in {said}", file=sys.stderr, flush=True)
    return remote.MISCONFIGURED


# Set by qk run for the command of a self lane, to the lane's name: each `qk admit` its script runs stays on the
# Mac where the lane runs, since one part builds what the next runs (config.unadmitted).
SELF_LANE = "QUEUEKEEPER_SELF_LANE"


@contextlib.contextmanager
def patch_environment(values):
    """os.environ with values set, for the commands started meanwhile, and as it was afterwards."""
    before = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in before.items():
            if value is None: os.environ.pop(key, None)
            else: os.environ[key] = value


def admit(root, args, config=None, *, device_window=None):
    # A command goes to whichever Mac has spare capacity for it, the second Mac first, as a lane does, and runs there
    # in the checkout's directory that matches the current one; what it changed in the checkout's files that Git does
    # not ignore comes back (remote.brought_back). --here, its paths and devices, nested and foreground work, and a
    # self lane's parts keep it here (guides/shared-host.md, Which Mac runs a lane).
    # Without a leading --, argparse collected the command, and a later -- belongs to it.
    command = args.command + ([] if args.passed is None else ["--", *args.passed]) if args.command else args.passed
    if not command: raise ValueError("Name the command: qk admit [--exclusive | --model] [--here] -- COMMAND")
    if args.time_limit <= 0: raise ValueError("--time-limit needs a positive number of seconds")
    config = config if config is not None else load(root)
    refused = exclusive_refusal(config, command) if args.exclusive else None
    if refused: raise ValueError(f"qk admit --exclusive refused: {refused}")
    kind = ["--exclusive"] if args.exclusive else ["--model"] if args.model else []
    # Only a limit of its own goes to the second Mac, whose toolkit may not take the option.
    if args.time_limit != processes.DEFAULT_LIMIT: kind += ["--time-limit", f"{args.time_limit:g}"]
    # Foreground work holds the foreground of the Mac it runs on: the second Mac's, which queuekeeper takes for it, or this
    # Mac's, which the session holds within the owner's window. qk remote admit --foreground sends it there.
    second = args.foreground and extensions.FOREGROUND.foreground_mac()
    if args.foreground and args.verify:
        raise ValueError("--verify never sends foreground work: send it to the second Mac with "
                         "qk remote admit --foreground -- COMMAND")
    if args.app and not args.foreground:
        raise ValueError("--app names the app under test of foreground work, whose crash-report alerts queuekeeper clears: "
                         "add --foreground")
    if args.foreground and not second: extensions.FOREGROUND.require_foreground()
    # Nested work runs inside the admission it inherits, here.
    inherited = bool(os.environ.get(resources.TOKEN))
    reason = here_because(args, inherited)
    offered = None
    if reason is None:
        # Whichever Mac has spare capacity for it, the second Mac first (remote.first_command, admitted_here).
        offered = remote.first_command(root, config, command, kind)
        if not isinstance(offered.status, str): return offered.status
        reason = offered.status
    # The policy goes to stderr so that stdout carries only the command's own output.
    receipt = {"project": source_identity(root), "toolkit": source_identity(TOOLKIT_ROOT),
               "policy": refresh_policy(root, sys.stderr), "command": command,
               "directory": str(Path.cwd()), "exclusive": args.exclusive, "model": args.model,
               "foreground": args.foreground, "timeLimit": args.time_limit, **placement(reason)}
    with processes.forwarding_signals(), contextlib.ExitStack() as stack:
        asked = time.monotonic()
        waiting = extensions.DEVICES.window_wait("the command", device_window, " ".join(command))
        allowed = waiting.seconds if waiting is not None else lambda seconds: seconds
        closed = extensions.DEVICES.window_closed("the command", waiting)
        if closed is not None: return closed
        # The second Mac's foreground first, so that a command waiting for it holds no slot.
        waited = extensions.FOREGROUND.foreground_held(stack, "the command", " ".join(command), allowed(args.timeout),
                                                       again=shlex.join(["qk", "admit", "--foreground", "--", *command])) \
            if second else None
        if waited is not None: return extensions.DEVICES.not_admitted("the command", waiting, waited)
        closed = extensions.DEVICES.window_closed("the command", waiting)
        if closed is not None: return closed
        admission = admitted_here(stack, "the command", " ".join(command), allowed(args.timeout), offered, args.if_free,
                                  args.exclusive, args.model, placement=admission_placement(offered, reason))
        if isinstance(admission, int): return extensions.DEVICES.not_admitted("the command", waiting, admission)
        sent = offered.sent if offered is not None else {}
        # Once the tree here is no longer what was sent, the command runs from what was sent (#276).
        moved = remote.moved_trees(root, offered)
        tree = from_sent(stack, root, sent, moved, receipt) if moved else None
        if isinstance(tree, int): return tree
        if tree is None:
            receipt["project"] = source_identity(root)
            if sent.get("commit"): receipt["sent"] = sent["commit"]
        receipt["queueSeconds"] = round(time.monotonic() - asked, 3)
        if device_window is not None: extensions.DEVICES.report_window(device_window, receipt["queueSeconds"])
        if offered is not None:
            receipt["hereBecause"] = offered.status
            fell_back("the command", offered)
        reap_here(root, inherited)
        def work():
            # Finalize the alert report before record() writes the receipt, while admission still holds.
            with resources.child_requests(), contextlib.ExitStack() as alert_stack:
                alerts = extensions.FOREGROUND.guarded_alerts(alert_stack, " ".join(command), receipt, args.app or ()) \
                    if args.foreground else None
                if isinstance(alerts, int): return alerts
                # The second Mac's Notification Center windows, under the foreground queuekeeper holds there (#204).
                blocked = extensions.FOREGROUND.cleared_notifications(receipt) if second else None
                if blocked is not None: return blocked
                for prepare in extensions.PREPARE_LANE: prepare(root, config)
                directory, variables = Path.cwd(), None
                if tree is not None:
                    # In the worktree's directory that matches the current one, as on the second Mac, after its
                    # provision lane, with the toolkit beside it (remote.sent_tree).
                    checkout, toolkit = tree
                    directory = checkout / matching_directory(root)
                    variables = {**os.environ, "QUEUEKEEPER_ROOT": str(toolkit)}
                    provisioned = provision_sent(root, config, checkout, toolkit, variables, args.time_limit,
                                                 admission.interrupted)
                    if provisioned: return provisioned
                # A foreground command restores what it changed on the Mac before SIGKILL, VoiceOver first.
                with extensions.around_run([command], directory):
                    status = processes.supervise(here_command(command), cwd=directory, env=variables,
                                                 on_stop=admission.interrupted, timeout=args.time_limit,
                                                 kill_after=processes.FOREGROUND_KILL_AFTER if args.foreground
                                                 else processes.KILL_AFTER)
                    status = alerts.judged(status) if alerts is not None else status
                # What it changed in the worktree's files that Git does not ignore, applied here as from the second Mac.
                if tree is not None: status = remote.brought_back_here(root, sent["commit"], tree[0], status)
                return status
        try:
            return record(root / ".build/queuekeeper/admit", receipt, work)
        except processes.TimedOut as error:
            # A backstop for a hang that a busy host can also reach, so the command is unmeasured.
            print(f"queuekeeper: unmeasured: {error}", file=sys.stderr)
            return lane_results.UNMEASURED


# What a runner takes to run with no admission, on this Mac, as it is started: the one way around admission and
# placement, which the caller has to write out.
UNADMITTED = "--unadmitted"
# The words that start each of the core's runners (extensions.RUNNERS holds the extensions').
RUNNERS = {("unittest",)}


def bare_runner(argv, first):
    """(argv, whether it names a runner that no admission holds), for argv whose command starts at first. A runner
    is a command that runs tests or other heavy work itself, as a lane's command does: `qk unittest`, and
    those the extensions name (extensions.RUNNERS). Under `qk run` or `qk admit` it inherits their
    admission. Started by itself it would take no slot and run on this Mac whatever else ran there, so it is admitted
    first (admitted_runner). --unadmitted, which is taken out of argv, and a request for help run it as it is."""
    words = argv[first:]
    own = words[:words.index("--")] if "--" in words else words
    if not any(tuple(words[:len(runner)]) == runner for runner in RUNNERS | extensions.RUNNERS): return argv, False
    if UNADMITTED in own:
        at = first + own.index(UNADMITTED)
        return argv[:at] + argv[at + 1:], False
    return argv, not (os.environ.get(resources.TOKEN) or "-h" in own or "--help" in own)


def runner_command(root, words):
    """The command line that runs a runner's words again, in the current directory, written so that the second Mac
    can run it too: the project's launcher by its path from here, else the toolkit's own command with the project."""
    launcher = root / "scripts/qk"
    if launcher.is_file(): return ["python3", os.path.relpath(launcher), *words]
    return ["python3", str(TOOLKIT_ROOT / "bin/qk"), "--project", os.path.relpath(root), *words]


# The command of the runner that admitted_run is running again, while admit has it: the one command whose python3
# this Mac replaces (here_command).
RUN_AGAIN = None


def here_command(command):
    """command as this Mac runs it. A runner's command to run it again (runner_command) names python3 for the second
    Mac, whose PATH finds a supported one. On this Mac that name can find an older one: on 2026-11-14 Python 3.14,
    started by its path, ran `scripts/qk unittest`, whose admitted child was Xcode's Python 3.9 and exited before it
    collected a test, so the run was unmeasured (vocaro/queuekeeper#152). Here that command runs under the
    interpreter that runs this process, which has started Queuekeeper. Every other command, such as one the caller
    wrote after `qk admit --`, runs as written, and so does what goes to the second Mac."""
    if RUN_AGAIN is not None and command == RUN_AGAIN and sys.executable: return [sys.executable, *command[1:]]
    return command


def admitted_runner(root, argv, first):
    """Run the runner argv names, from first, under a heavy-work admission, and return its status. In a project it
    runs as `qk admit -- COMMAND` runs any command: on whichever Mac has spare capacity for it, the second Mac
    first. A folder with no queuekeeper.json has no second Mac to match, so the runner waits for a slot on this Mac
    and runs in this process; BUSY says no slot came. Its first line names the folder that keeps its progress, and
    everything it prints, whatever its standard output is, such as a pipe into `tail` (vocaro/queuekeeper#22)."""
    with run_progress.admitted(["qk", *argv[first:]]) as outcome:
        outcome["status"] = admitted_run(root, argv, first)
    if argv[first:first + 1] == ["unittest"] and outcome.get("output"):
        # Admission's artifact checks and returned-products notes run after the runner. Repeat its
        # verdict last, indented so log readers do not count another suite (vocaro/queuekeeper#139).
        with contextlib.suppress(OSError):
            summary = None
            with outcome["output"].open(errors="replace") as output:
                for line in output:
                    if re.match(r"^queuekeeper unittest: \d+ tests ", line): summary = line.rstrip()
            if summary:
                sys.stdout.flush()
                print(f"  {summary} (command exit {outcome['status']})", file=sys.stderr, flush=True)
    return outcome["status"]


def admitted_run(root, argv, first):
    words = argv[first:]
    if (root / CONFIG_FILE).is_file():
        global RUN_AGAIN
        previous, RUN_AGAIN = RUN_AGAIN, runner_command(root, words)
        try: return main([*argv[:first], "admit", "--", *RUN_AGAIN])
        finally: RUN_AGAIN = previous
    label = " ".join(["qk", *words])
    try:
        with resources.Admission(label): return main(argv)
    except TimeoutError as error:
        print(f"queuekeeper: {words[0]} not run on {this_mac()}: {error}", file=sys.stderr)
        return BUSY


def main(argv=None):
    report = lane_report.Report()
    try:
        # The project's extensions first: they add commands, and what they add to a command's environment, such as
        # the host-local path XcodeGen expands before generating a project (#373), is read once they are loaded.
        try:
            compose_checkout()
            refused = pin_run(argv)
            if refused is not None: return refused
            # Before the extensions load: one loaded from its pinned folder finds further folders by these.
            os.environ.update(pins.exported(pins.held()))
            load_extensions(argv)
        except Exception as error:  # noqa: BLE001
            return could_not_start(error)
        with patch_environment(extensions.command_environment()):
            status = perform(argv, report)
    except Exception as error:
        report.finish(None, error)
        raise
    report.finish(status)
    return status


def could_not_start(error):
    """Say that Queuekeeper could not start, after error's traceback, and return TOOL_NOT_STARTED, which the command
    then exits with. Composing this checkout's plugins or loading one of the project's extensions raised error, before
    any command was read: no lane ran and no commit was tested. That is a fault of the set this process was put
    together from, this Queuekeeper and the folders the project's extensions load from, which need not be the
    project's commit: on 2026-11-15 a sibling checkout's adapter refused the older Queuekeeper a queue worker ran,
    each lane ended in the traceback alone with exit 1, as a failed test does, and the queue sent the entries back
    (vocaro/queuekeeper#158). The status and the line tell the two apart."""
    traceback.print_exception(error)
    why = " ".join(f"{type(error).__name__}: {error}".split())
    print(f"{lane_results.TOOL_SAID}{why}", file=sys.stderr)
    print("queuekeeper: that is Queuekeeper or one of the project's extensions failing to load, before the command "
          f"ran, so nothing ran and this says nothing of the project's commits (exit {lane_results.TOOL_NOT_STARTED}): "
          f"Queuekeeper is {TOOLKIT_ROOT}, and the extensions load from the folders the project's queuekeeper.json "
          "names; bring them to commits that work together", file=sys.stderr, flush=True)
    return lane_results.TOOL_NOT_STARTED


def hooks_path(root, config):
    """`qk doctor --hooks`: what a push by hand from this checkout runs, put right where it can be, and nothing else
    doctor checks, so that a session-start hook asks it quickly and parses no JSON (vocaro/queuekeeper#131)."""
    found = git_hooks.check(root, config.get("gitHooks"))
    left = [finding for finding in found if not finding.startswith("repaired ")]
    for finding in found: print(finding if finding not in left else f"warning: {finding}")
    if not found:
        value = git_state.git(root, "config", "--get", git_hooks.KEY)
        print(f"fine: {git_hooks.KEY} is {value}" if value else f"fine: {git_hooks.KEY} is unset, and the project "
              "declares no hooks of its own")
    return int(bool(left))


def compose_checkout():
    """Put this checkout's separately packaged plugins on the import path, for this process and the Python children
    it starts (checkout.activate), when Queuekeeper runs from a source checkout: one whose root has the workspace
    manifest. A launcher that composed it already changes nothing here; one that put only src/python on the path, as
    every project's scripts/qk did before the plugins moved out of the core, would otherwise find no
    queuekeeper_apple to load (vocaro-engineering#497). An installed package has no manifest beside it, and its
    plugins come from distribution metadata."""
    if (checkout.ROOT / checkout.MANIFEST).is_file(): checkout.activate(checkout.ROOT)


SIBLING = "--sibling"


def pin_run(argv):
    """Pin, for a command that runs a lane, `qk run` or `qk remote run`, the folders beside the project that its
    extensions load from, before they load: each at its origin/main, or as `--sibling NAME=WHERE` chooses, in
    QUEUEKEEPER_SIBLINGS for this process and every command the lane starts (pins). A run of hours otherwise loaded
    whatever landed in such a folder meanwhile, in its later lanes' nested qk commands (vocaro-engineering#497).

    A command already under a pin set keeps it and makes none: a lane of the merge queue's round, of a checkpoint, of
    a dispatch on the second Mac, or nested in another lane. Under one, --sibling is refused, so that no lane of the
    queue chooses what it is judged with. Any other command pins nothing and loads the folders as they stand.
    Returns None to go on, or the status to exit with, having said why; a set that cannot be made raises
    pins.Unpinned, which ends the command as one that could not start."""
    given = sys.argv[1:] if argv is None else list(argv)
    first = 2 if given[:1] == ["--project"] else 1 if given[:1] and given[0].startswith("--project=") else 0
    words = given[first:given.index("--") if "--" in given else len(given)]
    if words[:1] != ["run"] and words[:2] != ["remote", "run"]: return None
    asked = [words[index + 1] for index, word in enumerate(words[:-1]) if word == SIBLING] \
        + [word.split("=", 1)[1] for word in words if word.startswith(f"{SIBLING}=")]
    if pins.held() is not None:
        if not asked: return None
        print(f"queuekeeper: {SIBLING} {asked[0]} refused: this command runs under a pin set already, {pins.VARIABLE}, "
              "as a lane of the merge queue, of a checkpoint or of another lane does, and that set stands. Choose a "
              "folder's commit where the run starts", file=sys.stderr, flush=True)
        return USAGE
    root = Path(given[1] if first == 2 else given[0].split("=", 1)[1] if first == 1 else ".").resolve()
    names = sibling_variables(root)
    try: choices = pins.chosen(root, list(names), asked)
    except ValueError as error:
        print(f"queuekeeper: {error}", file=sys.stderr, flush=True)
        return USAGE
    if not names: return None
    made = pins.make(root, names, choices,
                     say=lambda text: print(f"queuekeeper: this run loads {text}", file=sys.stderr, flush=True))
    if made: os.environ[pins.VARIABLE] = pins.encoded(made)
    return None


def unsendable(lane):
    """Why the second Mac cannot run lane under this run's pin set, or None: a folder chosen with --sibling at a
    commit no branch of its origin holds, which that Mac could not fetch. Nothing but the commit is sent."""
    local = pins.local(pins.held())
    if not local: return None
    return (f"{' and '.join(local)} {'is' if len(local) == 1 else 'are'} pinned for this run at a commit its origin "
            f"does not hold, which the second Mac cannot fetch; push it to run {lane} there")


def load_extensions(argv):
    """Load the extensions of the project argv names: its folder is --project's when that comes first, as a launcher
    gives it, else the current one, and a linked worktree's extensions are found from its main checkout."""
    given = sys.argv[1:] if argv is None else list(argv)
    folder = Path(given[1] if given[:1] == ["--project"] and len(given) > 1 else
                  given[0].split("=", 1)[1] if given[:1] and given[0].startswith("--project=") else ".")
    extensions.load(extensions_of(folder, git_state.main_checkout(folder)))


def perform(argv, report):
    """Run the command argv names, telling report of a lane run's progress for the summary that ends it. The
    project's extensions are loaded already (load_extensions)."""
    # The visibility features their owner turned on, which add commands too (visibility).
    visibility.load()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="action", required=True)
    doctor = commands.add_parser("doctor")
    doctor.add_argument("--hooks", action="store_true", help="check and repair core.hooksPath alone, as a session-start "
        "hook asks: one line for each finding, each beginning repaired or warning, or one beginning fine; exit 0 "
        "unless a warning is left (guides/maintenance.md)")
    hook = commands.add_parser("pre-push", help="a pre-push hook: run the lanes the project names for the paths a push "
        "changes, the docs lanes for a docs-only push, queue.lanes or gate for any other and none for a push that "
        "changes no path, and refuse the push "
        "when one fails or is unmeasured; a project's hook is `exec python3 scripts/qk pre-push \"$@\"`")
    hook.add_argument("remote", nargs="*", help="the remote's name and URL, as Git gives a pre-push hook")
    run = commands.add_parser("run", usage="%(prog)s [-h] [--expect N] [--repeat N [--erase|--template] [--only TEST]] "
        "lane [-- ARGUMENTS ...]",
        help="run a configured lane, admitting a heavy or exclusive one, on whichever Mac has spare capacity for "
        "it now, the second Mac first, and on this Mac while the second Mac has none or cannot be reached, unless "
        "the lane says why it stays on this Mac: qk run LANE [-- ARGUMENTS], which appends the arguments, such as "
        "a test filter, to the lane's command")
    run.add_argument("--expect", type=int, metavar="N", help="require exactly N executed tests: N selected with "
        "some skipped or canceled is unmeasured, exit 3, and any other count fails; without it, a run with arguments needs at least one in place of the lane's minimumTests, which a whole run must reach, or equal where the lane says \"exactTests\": true")
    where = run.add_mutually_exclusive_group()
    where.add_argument("--here", action="store_true", help="run the lane on this Mac rather than on the second Mac: "
        "for a build whose products the session uses here, or, with the owner's decision, while the second Mac is "
        f"misconfigured (exit {remote.MISCONFIGURED})")
    where.add_argument("--offload-only", action="store_true", help="offer the lane to the second Mac, which runs it "
        f"only if it can start it now, and never run it here: exit {BUSY} when the second Mac declines it, cannot be "
        f"reached or none is configured, {remote.MISCONFIGURED} when it is misconfigured, else the lane's status "
        "there; for a caller that holds or will take its own admission here")
    # Its name until vocaro-engineering#58, which main refuses, naming the new one, beside --here too.
    run.add_argument("--build-host-only", action="store_true", help=argparse.SUPPRESS)
    run.add_argument("--if-free", action="store_true", help=f"run only if the second Mac's heavy-work admission, or "
        f"else this Mac's, admits the lane within {FREE_WAIT}s; otherwise exit {BUSY} without running it")
    run.add_argument("--serial", action="store_true", help="run a lane group's lanes one after another, on this "
        "Mac: the reference a failure seen only side by side is checked against")
    run.add_argument("--unlocked-screen", action="store_true", help="place the lane only on a Mac whose screen reads "
        "unlocked, and wait for one, as \"unlockedScreen\": true does: the merge queue's next run of a lane whose "
        "run a locked screen left without a verdict (guides/maintenance.md, A run with no verdict blames nobody)")
    run.add_argument("--repeat", type=int, metavar="N", help="run a lane of qk simulator-tests N times, on one build, "
        "each run on a member taken from the pool afresh, and report each run's outcome, test counts and one-minute "
        "load (apple-engineering's guides/simulator-testing.md, Repeating a test to show a fix works). Its receipt is not a pass of the "
        "whole lane")
    reset = run.add_mutually_exclusive_group()
    reset.add_argument("--erase", action="store_true", help="with --repeat: erase the member before each run")
    reset.add_argument("--template", action="store_true", help="with --repeat: copy the member's kind's warmed "
        "template before each run; a run the pool could only erase is unmeasured")
    run.add_argument("--only", action="append", default=[], metavar="TEST", help="with --repeat: run only this test, "
        "in place of the lane's own selection; repeat for each")
    run.add_argument(SIBLING, action="append", default=[], metavar="NAME=WHERE", help="load the project's extension "
        "in the folder NAME beside it from WHERE for this run, in place of NAME's origin/main: a commit of this Mac's "
        "checkout of NAME, or a checkout's path, such as a session's worktree, taken as its working tree stands. A "
        "commit that NAME's origin does not hold runs on this Mac only. Refused in a lane of the merge queue, which "
        "judges a project with its folders' origin/main (guides/shared-host.md, Extension folders beside a project)")
    run.add_argument("lane")
    # Listed for help only: main passes everything after these to their own parsers.
    for name, (described, _) in extensions.COMMANDS.items(): commands.add_parser(name, help=described)
    for name, (described, add_arguments, _, _) in extensions.PARSED_COMMANDS.items():
        add_arguments(commands.add_parser(name, help=described))
    commands.add_parser("unittest", help="run a unittest suite's modules in processes of their own, side by side: "
        "qk unittest [-s START] [-p PATTERN] [-j JOBS] [-k PATTERN] [-v] [NAME ...]; qk unittest -h for the rest. "
        "Started outside an admission it is admitted and placed first, as qk admit would run it; --unadmitted "
        "runs it with none, on this Mac")
    commands.add_parser("progress", help="say how far each long run started here has got, from another shell: qk "
        "unittest's processes finished, failed and running, and when it last moved, or an admitted runner's output; "
        "qk progress [--all] [RUN ...]")
    admission =commands.add_parser("admit", help="run any other heavy command (a build, swift test, xcodebuild "
        "test, a simulator run, a gate suite) through heavy-work admission on whichever Mac has spare capacity for "
        "it, the second Mac first, as qk run places a lane: qk admit -- COMMAND; what it changes in the "
        "checkout's files that Git does not ignore comes back and is applied here, while its build products stay "
        "on the Mac that ran it. Add --here for a command whose products this session uses here afterwards, "
        "--model for a generative-model run, and --exclusive only for work that needs the host to itself")
    kind = admission.add_mutually_exclusive_group()
    kind.add_argument("--exclusive", action="store_true", help="hold every slot, and the model with them, for the "
                      "timed run alone, built and set up beforehand as heavy work: a whole-machine or timing "
                      "measurement, or a model run that is timed or compared with a baseline")
    kind.add_argument("--model", action="store_true", help="hold one slot and the host's model lock, which one "
                      "model run holds at a time: on-device generation, on this Mac or in a simulator, another local "
                      "generative model on the GPU, or Core ML whose compute units include the GPU")
    admission.add_argument("--foreground", action="store_true", help="the command takes the screen, keyboard, "
        "pointer, focus or speech, as Mac VoiceOver and Mac UI tests do: on the second Mac queuekeeper holds its foreground for "
        "the command, waiting while another run holds it; on this Mac the session must hold mac-foreground within "
        "the owner's window (guides/shared-host.md, Attended Mac sessions)")
    admission.add_argument("--app", action="append", metavar="NAME", help="with --foreground, the app under test, "
        "as a crash-report alert names its process; repeat for each. A failed run during which queuekeeper cleared such an "
        "alert for another process, one that stood longer than one look, is unmeasured; one for NAME stays failed "
        "(guides/foreground.md, Crash-report alerts)")
    placed = admission.add_mutually_exclusive_group()
    placed.add_argument("--here", action="store_true", help="run the command on this Mac, never offering it to the "
        "second Mac: for a command whose products this session uses here afterwards, such as build output, derived "
        "data, files Git ignores or an app it then launches. Without it, the command goes to the second Mac first, "
        "to run in its checkout's directory that matches this one, at HEAD or a snapshot commit, and stays here when "
        "its directory is outside the checkout or one Git ignores, it names this checkout's path, an existing file "
        "Git ignores (such as a scratch script under .build, which the commit sent lacks), a simulator or "
        "device, it is nested in an admission or a self lane's part, or the second Mac has no spare capacity or "
        f"cannot be reached. What it changed there in files Git does not ignore is applied here; exit {remote.UNAPPLIED} "
        "says it could not be, and names the patch kept")
    # The default since the owner's decision of 2026-11-07, still accepted from callers that pass it.
    placed.add_argument("--verify", action="store_true", help="what qk admit does without --here: offer the "
        "command to the second Mac first")
    admission.add_argument("--if-free", action="store_true", help=f"run only if heavy-work admission admits the "
        f"command within {FREE_WAIT}s, on the second Mac or here; otherwise exit {BUSY} without running it")
    admission.add_argument("--timeout", type=float, default=5400, help="seconds to wait for admission (default 5400)")
    admission.add_argument("--time-limit", type=float, default=processes.DEFAULT_LIMIT, metavar="SECONDS",
        help=f"seconds the command may run once admitted before it and its processes are stopped and it exits "
        f"{lane_results.UNMEASURED}, unmeasured (default {processes.DEFAULT_LIMIT}); a backstop for a hang")
    admission.add_argument("command", nargs=argparse.REMAINDER, help="the command and its arguments, after --; "
        "it runs in the current directory")
    logged = commands.add_parser("admissions", usage="%(prog)s [-h] [--since WHEN] [--until WHEN] [log ...]\n"
        "       %(prog)s recover [--host HOST] [OWNER]\n       %(prog)s remove OWNER --why TEXT\n       %(prog)s limits\n"
        "       %(prog)s usage [--since WHEN] [--until WHEN] [--there LOG ...]\n       %(prog)s now [--json] [--here]",
        help="summarize this host's heavy-work admission "
        "log: for each kind and label, the requests admitted and withdrawn, how long they waited and held, and how "
        "admissions ended; qk admissions recover lists the guard's dead or uncertain entries, and qk admissions "
        "recover OWNER removes that owner's once a census against its process record finds nothing it ran still "
        "running; qk admissions remove OWNER --why TEXT removes by hand one recovery cannot prove finished once "
        "nothing its owner may have started is found running; qk admissions limits prints the admission limits in force and where each comes from; qk "
        "admissions usage shows both Macs' capacity now, the share of the last 3 hours each spent at each count of "
        "heavy jobs, and why this Mac's work ran here, never offered to the second Mac or declined by it; qk admissions "
        "now shows who holds and who waits for admission on each Mac now, how long and why",
        description="Summarize the admission log, or, with recover, list the guard's dead or uncertain entries and "
        "recover one: its owner, named by PID or identity, has ended, and nothing of the commands its process "
        "record leaves unsettled still runs. A live owner's entry, an entry without a process record and a "
        "guard that is unreadable or not in admission's format are left for inspection (guides/shared-host.md, "
        "Lock reclamation).")
    logged.add_argument("--since", type=resources.local_time, metavar="WHEN", help="count requests made from WHEN, "
                        "an ISO 8601 date or time, local unless it names a zone")
    logged.add_argument("--until", type=resources.local_time, metavar="WHEN", help="count requests made before WHEN")
    logged.add_argument("logs", nargs="*", type=Path, metavar="log", help="a copy of a log, such as the second Mac's, "
                        "to read instead of this host's; name the previous generation first")
    distant = commands.add_parser("remote", help="run a lane or other heavy command on the owner's second Mac over "
        "SSH: qk remote run LANE [-- ARGUMENTS] or qk remote admit [--foreground] -- COMMAND; qk remote passed "
        "LANE says whether the whole lane passed there at a commit; qk remote foreground says whether its "
        "foreground is ready")
    operations = distant.add_subparsers(dest="operation", required=True)
    remote_run = operations.add_parser("run", usage="%(prog)s [-h] [--host HOST] [--serial] [--expect N] "
        "[--device KEY] [--device-owner SESSION] [--checkout-slot N] [--checkout-wait SECONDS] "
        "lane [-- ARGUMENTS ...]",
        help="run a configured lane on the second Mac at HEAD, or at a snapshot commit of uncommitted work")
    remote_admit = operations.add_parser("admit", help="run a heavy command on the second Mac, in its checkout's "
        "root, at HEAD or at a snapshot commit of uncommitted work; what it changes there in files Git does not "
        f"ignore is applied here, as for qk admit, and exit {remote.UNAPPLIED} says it could not be, and names the "
        "patch kept")
    remote_setup = operations.add_parser("setup", help="install on the second Mac, with Homebrew, the tools "
        f"every project's gates use ({', '.join(remote.SETUP_FORMULAE)}) and the formulae named, then check them as "
        "qk remote sees the second Mac; Homebrew itself the owner installs")
    remote_setup.add_argument("formulae", nargs="*", metavar="formula", help="a further Homebrew formula to install")
    remote_setup.add_argument("--import", dest="imports", action="append", metavar="MODULE",
                              help="check that the second Mac's python3 imports MODULE (repeatable)")
    remote_setup.add_argument("--check", action="store_true", help="only check; install or copy nothing")
    remote_foreground = operations.add_parser("foreground", help="say what the second Mac lacks for foreground work, "
        "Mac VoiceOver and Mac UI tests, started over SSH, each with the owner's fix, and which run holds its "
        f"foreground; exit 0 when it lacks nothing, {remote.MISCONFIGURED} otherwise")
    remote_setup.add_argument("--runtimes", action="store_true", help="instead, give this Mac and the second Mac the "
        "same simulator runtimes of the Xcode at this Mac's developer directory, as qk doctor does: a runtime only "
        "one Mac has is deleted there when no simulator but Xcode's defaults uses it, and otherwise its disk image "
        "is copied over SSH to the Mac that lacks it; it exits 0 only when both list the same")
    for choice in (remote_run, remote_admit, remote_setup, remote_foreground):
        choice.add_argument("--host", help="the farm's Mac to reach, by its name in `qk farm list`, or an SSH "
                            "destination (default: the first Mac of the farm that takes work now)")
    for choice in (remote_run, remote_admit):
        choice.add_argument("--device", action="append", metavar="KEY", help="a physical device the run uses, "
                            "reserved in this Mac's device record and held, with no run active, by the session "
                            "(repeatable); the dispatch records the run in its entry and "
                            "stops it before the reservation ends")
        choice.add_argument("--device-owner", metavar="SESSION", help="the session holding --device (default: "
                            f"{extensions.DEVICES.device_owner})")
        choice.add_argument("--checkout-slot", metavar="N", help="run in this one of the second Mac's checkouts of the "
                            f"project, 0 to {remote.CHECKOUTS - 1}, waiting for it under the checkout's lock, however "
                            "many others are free, to repeat a check in a named checkout (guides/shared-host.md, A "
                            "named checkout slot); by default the free checkout nearest the commit")
        choice.add_argument("--checkout-wait", type=float, metavar="SECONDS", help="with --checkout-slot, how long to "
                            f"wait for it (default {offload_host.CLAIM_TIMEOUT}); then exit {BUSY} without running")
    remote_run.add_argument("--expect", type=int, metavar="N", help="require exactly N executed tests")
    remote_run.add_argument("--serial", action="store_true", help="run a lane group's lanes one after another, on the "
        "second Mac under its admission: the serial reference of a group that runs there, which counts for no commit")
    remote_run.add_argument(SIBLING, action="append", default=[], metavar="NAME=WHERE", help="as qk run's; the "
        "commit must be one NAME's origin holds")
    remote_run.add_argument("lane")
    kind = remote_admit.add_mutually_exclusive_group()
    kind.add_argument("--exclusive", action="store_true", help="hold every slot of the second Mac")
    kind.add_argument("--model", action="store_true", help="hold one slot and the model lock of the second Mac")
    remote_admit.add_argument("--foreground", action="store_true", help="hold the second Mac's foreground for the "
        "command, as Mac VoiceOver and Mac UI tests need, waiting while another run holds it")
    remote_admit.add_argument("--app", action="append", metavar="NAME", help="with --foreground, the app under "
        "test, as qk admit's")
    remote_admit.add_argument("--time-limit", type=float, metavar="SECONDS", help="seconds the command may run "
        f"there once admitted, as qk admit's (default {processes.DEFAULT_LIMIT})")
    remote_admit.add_argument("--git-commit", action="append", metavar="REPOSITORY=SHA", help="verify a full "
        "foreign commit hash in a local repository, so its exact positional argument names no hardware device "
        "(repeatable); device options and known device identifiers still require reservations")
    remote_admit.add_argument("command", nargs=argparse.REMAINDER, help="the command and its arguments, after --")
    remote_passed = operations.add_parser("passed", help="exit 0 when the whole lane passed on the second Mac at "
        "exactly this commit; a snapshot's run never counts")
    remote_passed.add_argument("lane")
    remote_passed.add_argument("--commit", default="HEAD")
    remote_prune = operations.add_parser("prune-results", help="remove the lane results kept by commit beside the "
        f"receipts in the repository's Git directory that the rule reclaims, which {remote.KEPT_RESULTS_RULE}; "
        "qk disk lists them")
    remote_prune.add_argument("checkout", nargs="?", type=Path, help="a checkout of the repository (default: the project's)")
    remote_prune.add_argument("--dry-run", action="store_true", help="list them; remove nothing")
    merge_queue.add_arguments(commands.add_parser("queue", help="land finished commits through the repository's one "
        "merge queue, which gates batches on origin/main and pushes them; a session adds its commit and moves on "
        "(guides/maintenance.md, The merge queue)"))
    merge_queue.land_arguments(commands.add_parser("land", help="land a finished commit through the merge queue of this "
        "repository, or with --repo of another, and return at once: the queue gates it on origin/main and pushes it"))
    documentation = commands.add_parser("docs", help="check the project's Markdown: each link's file, anchor and "
        "source line exists, and, with queuekeeper.json's docs.citations, each citation of a document by path and "
        "section title or anchor in its Markdown and source names a heading or bold lead-in of it; exit 1 on any "
        "that does not (guides/maintenance.md, Docs check)")
    documentation.add_argument("operation", choices=["check"])
    documentation.add_argument("paths", nargs="*")
    preflights = commands.add_parser("preflight", help="run the project's preflight lanes (queue.preflight) here, "
        "without admission, as qk land runs them before it queues an entry; exit 1 on a finding, 3 when one could not "
        "run (doc/merge_queue.md, Preflight lanes)")
    preflights.add_argument("lanes", nargs="*", help="only these of the preflight lanes, in the order given")
    spell = commands.add_parser("spelling", help="find British spellings in the project's text, identifiers "
        "included, and file names, and text that calls this command by its old name; exit 1 on any, 2 when nothing "
        "was checked")
    spell.add_argument("paths", nargs="*", help="files or directories; untracked files under them are read too")
    disk.add_arguments(commands.add_parser("disk", help="inventory reclaimable disk space; changes nothing without --delete"))
    commands.add_parser("farm", help="the Macs beside this one that take work: list, add DESTINATION [--name NAME], "
                        "rename NAME NEW, pause NAME, resume NAME, remove NAME")
    commands.add_parser("visibility", help="turn the features that show the queue to its owner on and off: "
                        "status, enable FEATURE, disable FEATURE")
    hosts = commands.add_parser("host", help="read a Mac's state as queuekeeper reads it")
    reads = hosts.add_subparsers(dest="operation", required=True)
    screen = reads.add_parser("screen", help="say whether a Mac's console screen is locked, from both keys the "
        "IORegistry holds and the console user, and exit 0 unlocked, 1 locked or 2 unknown")
    screen.add_argument("--remote", action="store_true", help="read the second Mac's screen over SSH")
    screen.add_argument("--host", help="the farm's Mac to read, by its name in `qk farm list`, or an SSH "
                        "destination, when not the first that takes work now")
    screen.add_argument("--json", action="store_true", help="print the reading as JSON")
    for name, (described, add_arguments, _) in extensions.HOST_READS.items():
        add_arguments(reads.add_parser(name, help=described))
    # Everything after the first -- belongs to the command. Python 3.9's argparse rejects options
    # before it and drops a later --, so argparse never sees it.
    argv = sys.argv[1:] if argv is None else list(argv)
    # unittest's options are its own runner's, which this parser would refuse. It reads no project.
    first = 2 if argv[:1] == ["--project"] else 1 if argv[:1] and argv[0].startswith("--project=") else 0
    project = argv[1] if first == 2 else argv[0].split("=", 1)[1] if first == 1 else None
    # A runner started outside an admission is admitted and placed first, as a lane's command is (bare_runner).
    argv, bare = bare_runner(argv, first)
    if bare: return admitted_runner(Path(project or ".").resolve(), argv, first)
    if argv[first:first + 1] == ["unittest"]: return parallel.unittest_main(argv[first + 1:])
    if argv[first:first + 1] == ["progress"]: return run_progress.main(argv[first + 1:], project)
    if argv[first:first + 1] and argv[first] in extensions.COMMANDS:
        return extensions.COMMANDS[argv[first]][1](argv[first + 1:], project)
    # The farm's Macs are the owner's and this Mac's, and no project's.
    if argv[first:first + 1] == ["farm"]: return farm.main(argv[first + 1:])
    # The switch is the owner's and this Mac's, and reads no project.
    if argv[first:first + 1] == ["visibility"]: return visibility.main(argv[first + 1:])
    # Recovery, like the admission log, covers the whole host and reads no project.
    if argv[first:first + 2] == ["admissions", "recover"]: return resources.recover_main(argv[first + 2:])
    if argv[first:first + 2] == ["admissions", "remove"]: return resources.remove_main(argv[first + 2:])
    if argv[first:first + 2] == ["admissions", "limits"]: return resources.limits_main(argv[first + 2:])
    if argv[first:first + 2] == ["admissions", "usage"]: return resources.usage_main(argv[first + 2:])
    if argv[first:first + 2] == ["admissions", "now"]: return resources.now_main(argv[first + 2:])
    split = argv.index("--") if "--" in argv else len(argv)
    args = parser.parse_args(argv[:split])
    # Only the remote work itself reports its admission. Provisioning and nested commands do not.
    device_window = extensions.DEVICES.take_window()
    args.passed = argv[split + 1:] if split < len(argv) else None
    if args.passed is not None and (args.action, getattr(args, "operation", None)) not in {
            ("run", None), ("admit", None), ("remote", "run"), ("remote", "admit")}:
        parser.error("only run and admit take arguments after --")
    # What the project's tooling does before each command that reaches the hosts, such as offering the second Mac
    # the evidence an offload found it unreachable for (extensions.HOST_COMMAND).
    if args.action in HOST_COMMANDS:
        for started in extensions.HOST_COMMAND: started()
    root = args.project.resolve()
    # What a run that died left changed on this Mac, the next command puts back.
    extensions.recover()
    # What a lane's run holds until it ends, however it ends: its directory (lane_directory).
    held = contextlib.ExitStack()
    try:
        # The inventory, the skill links and the admission log cover the whole host, so they need no project
        # configuration.
        if args.action == "disk": return disk.execute(args)
        # Such a command of an extension's too, as a keychain's Mac and its session are the host's.
        if args.action in extensions.PARSED_COMMANDS and not extensions.PARSED_COMMANDS[args.action][3]:
            return extensions.PARSED_COMMANDS[args.action][2](root, args, None)
        # A Mac's screen is the host's, not a project's.
        if args.action == "host" and args.operation in extensions.HOST_READS:
            return extensions.HOST_READS[args.operation][2](args)
        if args.action == "host": return host_screen(args)
        if args.action == "admissions": return resources.report(args.logs or None, args.since, args.until)
        # The queue reads a project's settings from its commits, and never from this working tree,
        # which for its worker is a main checkout that may lag.
        if args.action == "land": return merge_queue.land(root, args, Path.cwd())
        if args.action == "queue": return merge_queue.main(root, args)
        # The store is the repository's, named by any checkout of it, and qk disk runs this for every project.
        if args.action == "remote" and args.operation == "prune-results":
            return remote.prune_results_main(args.checkout or root, args.dry_run)
        config = load(root)
        if args.action in {"run", "admit"} or (args.action == "remote" and args.operation in {"run", "admit"}):
            for check in extensions.BEFORE_LANE: check(root, config)
        if args.action in extensions.PARSED_COMMANDS: return extensions.PARSED_COMMANDS[args.action][2](root, args, config)
        if args.action == "doctor" and args.hooks: return hooks_path(root, config)
        if args.action == "doctor":
            required = {"git", "python3"}
            for needed in extensions.REQUIRED_COMMANDS: required |= set(needed(config))
            missing = sorted(name for name in required if not shutil.which(name))
            warnings = []
            unbounded = sorted(name for name, lane in config.get("lanes", {}).items()
                               if "command" in lane and "timeout" not in lane
                               and name not in config.get("queue", {}).get("preflight", []))
            if unbounded:
                warnings.append(f"{len(unbounded)} lane(s) declare no timeout and are stopped, unmeasured, after "
                                f"queuekeeper's default {processes.DEFAULT_LIMIT}s: {', '.join(unbounded)}; give each a timeout "
                                "chosen from its measured time (guides/parallel-tests.md, Bound every run)")
            if config.get("lanes") and "gate" not in config["lanes"] and not config.get("queue", {}).get("lanes") \
                    and config.get("integration", "direct-main") == "direct-main":
                warnings.append("no lane is named gate, which qk land runs: name the lane holding "
                                "what a commit must pass before it lands gate (the toolkit's guides/adoption.md, Lane names)")
            # A project whose spelling check rests on the legacy spelling lane, which a later release removes (#69).
            if legacy_spelling_warning(config): warnings.append(legacy_spelling_warning(config))
            # A project still on queue.docsOnly or queue.carryOver, which queue.unread replaces.
            if legacy_unread_warning(config): warnings.append(legacy_unread_warning(config))
            warnings += budgets.doctor_warnings(config)
            warnings += self_lane_warnings(root, config)
            warnings += split_warnings(root, config)
            from .waiting import doctor_warnings as fixed_wait_warnings  # #284
            warnings += fixed_wait_warnings(root)
            warnings += lane_wait.admission_warnings(root)
            # What a push by hand from this checkout runs, put right where it can be (git_hooks).
            warnings += git_hooks.check(root, config.get("gitHooks"))
            # A difference between the two Macs is an error; a second Mac that cannot be reached is unchecked.
            hosts = fix_hosts(root, remote.compare_hosts())
            for check in extensions.DOCTOR: warnings += check(root, config)
            # Each Mac short of memory, with what holds it (host_health); the second Mac's rides on its survey.
            warnings += host_health.warnings((hosts.get("local") or {}).get("hostHealth") or offload_host.host_health(),
                                             "this Mac")
            if "remote" in hosts and "hostHealth" not in hosts["remote"]:
                hosts.setdefault("unchecked", []).append(f"whether {hosts['host']} is short of memory, which its "
                                                         "toolkit checkout did not report before it was updated")
            elif "remote" in hosts: warnings += host_health.warnings(hosts["remote"]["hostHealth"], hosts["host"])
            test_lanes = lane_coverage.report(root, config)  # a warning at most, and never a failure (#289)
            warnings += lane_coverage.doctor_warnings(test_lanes)
            print(json.dumps({"project": config["name"], "queuekeeper": str(TOOLKIT_ROOT),
                "source": source_identity(TOOLKIT_ROOT), "extensions": list(extensions.LOADED),
                "missingCommands": missing, "warnings": warnings, "notes": remote.python_crash_notes(hosts),
                "testLanes": test_lanes, "offload": hosts}, indent=2))
            return int(bool(missing or hosts["errors"]))
        elif args.action == "pre-push": return pre_push.main(root, config, args.remote)
        elif args.action == "admit": return admit(root, args, config, device_window=device_window)
        elif args.action == "remote": return remote.main(root, config, args)
        elif args.action == "docs":
            failures = docs.check(root, args.paths or None, citations=config.get("docs", {}).get("citations", False))
            print("\n".join(failures) if failures else "Documentation checks passed")
            return int(bool(failures))
        # The subcommand names are this parser's, so a new one is covered as it is added (vocaro/queuekeeper#35).
        elif args.action == "spelling": return spelling.check(root, config, args.paths, commands=tuple(commands.choices))
        elif args.action == "preflight": return preflight.run(root, config, args.lanes)
        elif args.action == "run":
            report.begin(root, args.lane)
            # The project's main checkout, from which its lane's launcher finds the tools beside it, here and in the
            # worker's checkout alike (vocaro/queuekeeper#69).
            os.environ[PROJECT_CHECKOUT_VARIABLE] = project_checkout(root)
            if args.build_host_only:
                raise ValueError("--build-host-only was --offload-only's name until vocaro-engineering#58, and is no "
                                 "longer read: use --offload-only (guides/shared-host.md, Earlier names)")
            lane = lane_to_run(config, args.lane, root)
            if lane is None: raise ValueError(f"No configured lane: {args.lane}")
            if args.expect is not None and args.expect < 1: raise ValueError("--expect needs at least 1 test")
            if args.unlocked_screen:
                # Placed from here on as a lane that says "unlockedScreen": true is, on whichever Mac runs it
                # (vocaro-engineering#293). A self lane runs where it starts, and its script places its parts.
                if unadmitted(lane):
                    print(f"queuekeeper: {args.lane} runs where it starts, as a self lane, so --unlocked-screen does not place "
                          "it", file=sys.stderr, flush=True)
                else: lane["unlockedScreen"] = True
            if args.repeat is not None and args.repeat < 1:
                raise ValueError("--repeat needs at least 1 run: a series of none shows nothing")
            if args.repeat is None and (args.erase or args.template or args.only):
                raise ValueError("--erase, --template and --only say how a series runs: give --repeat N too")
            series = [] if args.repeat is None else ["--repeat", str(args.repeat), *(["--erase"] if args.erase else []),
                                                     *(["--template"] if args.template else []),
                                                     *[word for test in args.only for word in ("--only", test)]]
            # Lane arguments, so that qk simulator-tests finds them on whichever Mac runs the lane.
            extra = [*series, *(args.passed or [])]
            # A lane that says it takes none refuses them here, before it is staged, admitted or sent to the second
            # Mac. On 2026-11-13 a lane's own script refused two -only-testing arguments only after the run had
            # been sent there (vocaro/queuekeeper#137).
            if extra and lane.get("arguments") is False:
                raise ValueError(f"{args.lane} takes no arguments, as its lane says (\"arguments\": false), and was "
                                 f"given {shlex.join(extra)}; nothing ran. Run it without them: qk run {args.lane}")
            # A series' lane runs N times inside its one admission, within N times its limit (guides/simulator-pool.md).
            limit = parallel.lane_timeout(lane, extra)
            lane_wait.begin(root, args.lane)
            # A foreground lane takes a Mac's screen. The second Mac's is queuekeeper's to take for the run, below; this
            # Mac's, only the session holding mac-foreground within the owner's window runs it on, with --here.
            # Otherwise it goes to the second Mac and waits there for its foreground, never falling back to this
            # Mac (foreground.py).
            second = lane.get("foreground") and extensions.FOREGROUND.foreground_mac()
            # What keeps the lane from the second Mac under this run's pin set, or None (--sibling).
            unsent = unsendable(args.lane)
            if lane.get("foreground") and not second:
                if args.offload_only or not (args.here or lane.get("here")):
                    if unsent: raise ValueError(f"{args.lane} not run: it runs on the second Mac, and {unsent}")
                    return remote.foreground_lane(root, config, args.lane, extra, args.expect)
                extensions.FOREGROUND.require_foreground()
            group = report.group = lane.get("parallel")
            if group is not None and (extra or args.expect):
                raise ValueError(f"{args.lane} runs a group of lanes, which takes no arguments or --expect; "
                                 "pass them to one of its lanes")
            if group is None and args.serial: raise ValueError(f"--serial runs a lane group, and {args.lane} is not one")
            # Why the lane runs only here, as its configuration says, or None: it goes to the second Mac.
            reason = stays(config["lanes"], args.lane)
            # Or why it runs only on the second Mac, as a key an extension added says. It waits there for its
            # admission and never falls back to this Mac, where its result would not be the one asked for.
            placed = extensions.placement(config["lanes"], args.lane) if reason is None else None
            if placed and placed[0] == extensions.THERE:
                if args.here or args.serial or os.environ.get(resources.TOKEN):
                    raise ValueError(f"{args.lane} runs only on the second Mac ({placed[1]}), so it cannot run here"
                                     + (" with --here" if args.here else ""))
                if unsent: raise ValueError(f"{args.lane} not run: it runs only on the second Mac ({placed[1]}), and {unsent}")
                return remote.there_lane(root, config, args.lane, extra, args.expect, placed[1])
            # Keep the queue's placement scope at the outer lane.
            queue_gate = bool(os.environ.pop(remote.QUEUE_GATE, None))
            exclude = os.environ.pop(remote.QUEUE_GATE_EXCLUDE, None)
            if args.offload_only and unsent:
                raise ValueError(f"{args.lane} not run: --offload-only never runs it here, and {unsent}")
            if args.offload_only:
                if reason is not None:
                    raise ValueError(f"{args.lane} stays on this Mac ({reason}), so --offload-only cannot offer it "
                                     "to the second Mac")
                if args.serial: raise ValueError("--serial runs a lane group here, and --offload-only never does")
                # The caller runs the lane under its own admission when the second Mac declines it.
                return remote.first(root, config, args.lane, extra, args.expect, fallback=False,
                                    queue_gate=queue_gate, exclude=exclude).status
            # Nested work runs inside the admission it inherits, here. The serial reference runs here too.
            inherited = bool(os.environ.get(resources.TOKEN))
            reason = reason or ("--here" if args.here else "--serial runs the serial reference here" if args.serial
                                else "nested in the admission it inherits" if inherited else None)
            if reason is None and unsent:
                reason = unsent
                print(f"queuekeeper: {args.lane} runs on this Mac: {unsent}", file=sys.stderr, flush=True)
            offered = None
            if reason is None:
                # Whichever Mac has spare capacity for the lane, the second Mac first, and while neither has,
                # whichever admits the lane first (remote.first, admitted_here).
                offered = remote.first(root, config, args.lane, extra, args.expect, queue_gate=queue_gate,
                                       exclude=exclude)
                if not isinstance(offered.status, str): return offered.status
                reason = offered.status
            # Every lane is admitted, a light one as heavy work, except a self lane, whose script admits its parts.
            resource = admission_of(lane)
            # A lane whose result depends on the screen has it read on the Mac that runs it, at its start and again
            # when it ends unmeasured (vocaro-engineering#185). A nested lane rechecks its own requirement:
            # the outer admission may not have placed work for an unlocked screen (#366).
            needs = unlocked_screen(config["lanes"], args.lane)
            reads_screen = needs or any(extensions.reads_screen(config["lanes"][name]) for name in (group or [args.lane]))
            policy_commit = refresh_policy(root) if resource != "self" else None
            directory = lane_directory(held, root, args.lane)
            receipt = {"project": source_identity(root), "toolkit": source_identity(TOOLKIT_ROOT),
                       "policy": policy_commit, **placement(reason)}
            # The commit of each folder beside the project that this run loads its extensions from (pins).
            if pins.held(): receipt["siblings"] = pins.recorded(pins.held())
            # A run of the whole lane, which a pass of counts for its commit once kept (concluded).
            whole = not extra and args.expect is None and not args.serial
            keys = lane_devices(lane)
            admission = None
            # Where the lane's command runs: this checkout, or the worktree of what was sent (admitted_tree). Its
            # receipt, logs and results stay in this checkout's directory either way.
            tree = root
            if whole and receipt["project"].get("workingChangesSHA256") != remote.CLEAN:
                # vocaro-engineering#197: nothing said so until eng release stopped for it.
                why = remote.uncounted(root, args.lane)
                if why: print(f"queuekeeper: {why}", file=sys.stderr, flush=True)
            def concluded(status, **fields):
                # The verdict in this checkout, and kept by commit for every worktree (vocaro-engineering#198).
                conclude(directory, receipt, status, **fields)
                # A worktree of a snapshot is clean at a commit no branch holds, which is no commit's run to keep.
                counts = whole and not (tree != root and offered.sent["commit"] != offered.sent.get("head"))
                remote.keep_here(root, args.lane, receipt, status, counts, results=directory / "results")
                # A lane's output goes to its log; the Mac that sent a dispatch prints these once they are back.
                if not os.environ.get(remote.DISPATCHER): extensions.report_results(directory / "results")
            def prepared(work):
                # What an earlier run left in results/ is not this run's, here as on the second Mac (remote.RESULTS).
                # The lane finds the directory in QUEUEKEEPER_RESULTS_DIR, and a group's lanes each find one of their own in it.
                shutil.rmtree(directory / "results", ignore_errors=True)
                report.ran_here(directory)
                if resource != "self":
                    for prepare in extensions.PREPARE_LANE: prepare(tree, config)
                commands = [config["lanes"][name]["command"] for name in group] if group is not None else \
                    [lane["command"], *([lane["compileFirst"]] if "compileFirst" in lane else [])]
                try:
                    with resources.child_requests(), extensions.around_run(commands, tree): return work()
                finally:
                    # Nested in another lane, its results go into that lane's too, however it ended (#218).
                    parallel.hand_up(directory / "results", args.lane)
            if group is None:
                command = lane_command(lane, extra)
                receipt.update(command=command, arguments=extra, timeout=limit)
                def execute(on_stop=None):
                    def work():
                        env = shims.environment(parallel.results_environment(directory / "results"), tree)
                        kill_after = processes.FOREGROUND_KILL_AFTER if lane.get("foreground") else processes.KILL_AFTER
                        failed, left = compile_first(lane, tree, directory, receipt, limit,
                                                     env, on_stop, kill_after)
                        if failed is not None: return failed
                        why = unstartable(lane["command"], tree)
                        if why: return not_started(directory, receipt, args.lane, why)
                        try:
                            return processes.run(command, cwd=tree, log_path=directory / "run.log", timeout=left,
                                                 on_stop=on_stop, env=env, kill_after=kill_after,
                                                 **({"stall": lane["stall"], "progress_path": directory / "results/progress.json"}
                                                    if "stall" in lane else {}))
                        except processes.NotStarted as error:
                            return not_started(directory, receipt, args.lane, str(error))
                    return record(directory, receipt, lambda: prepared(work))
            else:
                jobs = 1 if args.serial else lane.get("jobs", len(group))
                receipt.update(parallel=group, jobs=jobs)
                # Each lane's receipt shows the command it ran, as its own keys make it.
                members = [(name, {**config["lanes"][name], "command": lane_command(config["lanes"][name])}) for name in group]
                def execute(on_stop=None):
                    return record(directory, receipt, lambda: prepared(lambda: parallel.lanes(tree, members, directory / "lanes", jobs,
                                  lambda member, status, log: judge(member, status, log)[0], receipt, on_stop,
                                  results_directory=directory / "results")))
            try:
                with processes.forwarding_signals():
                    if resource == "self":
                        if device_window is not None: extensions.DEVICES.report_window(device_window, 0)
                        scroll_bars_recorded(receipt, args.lane, inherited)
                        unprovisioned = provisioned_here(root, config, args.lane, receipt, inherited)
                        if unprovisioned is not None: return unprovisioned
                        # Each part its script admits stays on this Mac, which holds what an earlier part built.
                        lane_wait.running()
                        with patch_environment({SELF_LANE: args.lane}):
                            result = execute()
                    else:
                        with contextlib.ExitStack() as stack:
                            asked = time.monotonic()
                            # The window the session holds the lane's devices for, here or leased by the Mac that
                            # sent it. Arguments such as a filter make a run no earlier one measures.
                            device_window = device_window if device_window is not None else \
                                extensions.DEVICES.held_window(keys)
                            waiting = extensions.DEVICES.window_wait(args.lane, device_window,
                                                  lane_label(root, config, args.lane) if whole else None,
                                                  keys=keys or None,
                                                  budget=(lane.get("deviceRunMinutes", 0) * 60 or None) if whole else None,
                                                  fallback=limit if whole and keys else None)
                            allowed = waiting.seconds if waiting is not None else lambda seconds: seconds
                            closed = extensions.DEVICES.window_closed(args.lane, waiting)
                            if closed is not None: return closed
                            # The second Mac's foreground first, so that a run waiting for it holds no slot.
                            waited = extensions.FOREGROUND.foreground_held(stack, args.lane, lane_label(root, config, args.lane),
                                                     allowed(remote.OVERFLOW_TIMEOUT),
                                                     expected=lane.get("timeout"),
                                                     again=f"qk run {args.lane}") if second else None
                            if waited is not None: return extensions.DEVICES.not_admitted(args.lane, waiting, waited)
                            closed = extensions.DEVICES.window_closed(args.lane, waiting)
                            if closed is not None: return closed
                            admission = admitted_here(stack, args.lane, lane_label(root, config, args.lane),
                                                      allowed(remote.OVERFLOW_TIMEOUT), offered, args.if_free,
                                                      resource == "exclusive", lane.get("model", False),
                                                      lane.get("wholeHost", False),
                                                      needs and not os.environ.get(resources.TOKEN),
                                                      admission_placement(offered, reason))
                            if isinstance(admission, int): return extensions.DEVICES.not_admitted(args.lane, waiting, admission)
                            tree = admitted_tree(stack, root, args.lane, offered, receipt)
                            if isinstance(tree, int): return tree
                            # The wait in the queue, apart from what came before it (remote.execute).
                            receipt["queueSeconds"] = round(time.monotonic() - asked, 3)
                            if device_window is not None: extensions.DEVICES.report_window(device_window, receipt["queueSeconds"])
                            if offered is not None:
                                receipt["hereBecause"] = offered.status
                                fell_back(args.lane, offered)
                            reap_here(root, inherited)
                            scroll_bars_recorded(receipt, args.lane, inherited)
                            alerts = extensions.FOREGROUND.guarded_alerts(stack, args.lane, receipt, lane.get("apps", ())) \
                                if lane.get("foreground") else None
                            if isinstance(alerts, int): return alerts
                            blocked = extensions.FOREGROUND.cleared_notifications(receipt) if second else None
                            if blocked is not None: return blocked
                            closed = extensions.DEVICES.window_closed(args.lane, waiting)
                            if closed is not None: return closed
                            if reads_screen: receipt["screen"] = screen_reading()
                            if inherited and needs and receipt["screen"]["state"] != "unlocked":
                                print(f"queuekeeper: {args.lane} not run on {this_mac()}: "
                                      f"{remote.screen_refusal(receipt['screen'], this_mac())}; "
                                      "release the outer admission before waiting for an unlocked screen and retry",
                                      file=sys.stderr, flush=True)
                                return BUSY
                            unprovisioned = provisioned_here(root, config, args.lane, receipt, inherited,
                                                             admission.interrupted, tree if tree != root else None)
                            if unprovisioned is not None: return unprovisioned
                            result = execute(admission.interrupted)
                            # A failed build ran no test an alert could have disturbed.
                            if alerts is not None and not compile_failed(receipt): result = alerts.judged(result)
            except processes.TimedOut as error:
                # The lane's timeout is its backstop, so a run a busy host slowed past it is
                # unmeasured (the toolkit's guides/measurement.md). Its log is incomplete and counts nothing.
                print(f"queuekeeper: unmeasured: {error}", file=sys.stderr)
                report.explain(error, complete=False)
                return lane_results.UNMEASURED
            # A group's lanes were each judged by their own logs.
            if "parallel" in lane:
                if reads_screen and result == lane_results.UNMEASURED: unmeasured_screen(args.lane, receipt)
                concluded(result)
                return result
            # A command that never started says nothing of the code it would have run (vocaro/queuekeeper#108).
            if receipt.get("notStarted"):
                concluded(lane_results.NOT_STARTED)
                return lane_results.NOT_STARTED
            # The command has a confirmed terminal result. Coverage validation cannot turn
            # that completed process into an orphan reservation.
            log = (directory / "run.log").read_text(errors="replace")
            try:
                status, unmeasured = judge(lane, result, log, extra, args.expect)
            except ValueError as error:
                concluded(2, error=str(error))
                raise
            if status == lane_results.UNMEASURED and (reads_screen or SCREEN_LOCKED.search(log)):
                unmeasured_screen(args.lane, receipt)
            # A failure whose log shows no test started, such as a project that could not be generated, exits apart
            # from a test's failure, and stays a failure (vocaro-engineering#393). A compileFirst build that failed
            # is already told apart, a status that gives no verdict keeps its meaning, and a failure record in its
            # results is a test's. A command that refused its usage, status 2, already says so apart from a test.
            before = lane_report.before_tests(log) if status > 0 and status != USAGE and status not in merge_queue.NO_VERDICT \
                and not compile_failed(receipt) and not lane_report.records(directory / "results") else None
            if before is not None: status = lane_results.BEFORE_TESTS
            concluded(status, **({"unmeasured": unmeasured} if status == lane_results.UNMEASURED else {}),
                      **({"beforeTests": before} if before is not None else {}))
            if status == 0 and whole and keys and admission is not None and extensions.DEVICES.device_lane_passed(directory / "results", keys):
                admission.device_lane_passed(keys)
            return status
        return 0
    except (remote.Lost, extensions.Unavailable) as error:
        print(f"queuekeeper: {error}", file=sys.stderr)
        report.explain(error)
        return lane_results.UNMEASURED
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"queuekeeper: {error}", file=sys.stderr)
        # A timeout whose cleanup could not confirm every process ended keeps its reservation, and is still a
        # backstop a busy host reached, not a verdict on the commit (processes.Unconfirmed).
        if isinstance(error, processes.Unconfirmed) and error.timed_out:
            print("queuekeeper: unmeasured: the lane reached its limit", file=sys.stderr)
            report.explain(error, complete=False)
            return lane_results.UNMEASURED
        report.explain(error)
        return 2
    finally:
        held.close()
        lane_wait.finish()
