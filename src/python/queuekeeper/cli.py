"""Queuekeeper's command: a merge queue and build farm for one developer's agents, on that developer's own Macs."""
import argparse
import contextlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

from . import __version__, budgets, disk, docs, extensions, git_state, lane_coverage, lane_report, lane_results, lane_wait, merge_queue, offload_host, \
    parallel, processes, remote, resources, shims, spelling
from .config import TOOLKIT_ROOT, admission as admission_of, extensions_of, lane_to_run, load, source_identity, \
    display_lane_warnings, lane_devices, self_lane_warnings, stays, unadmitted, unlocked_screen
from .resources import BUSY, FREE_WAIT

# The commands that reach the hosts, before each of which extensions.HOST_COMMAND runs.
HOST_COMMANDS = {"run", "admit", "remote", "land", "queue", "disk"}


def fix_hosts(root, hosts):
    """What `doctor` fixes of remote.compare_hosts' report itself, the owner's no longer (the owner's decision,
    2026-11-03): what each extension fixes (extensions.HOST_FIXES), and the second Mac's own toolkit checkout,
    which it fast-forwards to origin/main when it can and reports in toolkitCheckout, an error when it cannot
    (remote.update_there, vocaro-engineering#203). tests/host_isolation.py replaces it, so that no test changes
    either Mac."""
    for fix in extensions.HOST_FIXES: hosts = fix(root, hosts)
    # The second Mac's own toolkit checkout, which it fast-forwards to origin/main when it can; one it must
    # leave as it is is an error, the owner's to decide (vocaro-engineering#203).
    if "remote" in hosts:
        found = hosts["toolkitCheckout"] = remote.update_there(hosts["host"])
        if found.get("state") == "unchecked":
            hosts.setdefault("unchecked", []).append(f"{hosts['host']}'s own toolkit checkout, which it did not "
                                                     f"report: {found.get('reason')}")
        else:
            line, owner = remote.toolkit_line(hosts["host"], found)
            if line: found["said"] = line
            if owner: hosts.setdefault("errors", []).append(line)
            elif line: print(f"remote: {line}", file=sys.stderr, flush=True)
    return hosts


def split_warnings(root, config):
    """The gate over its budget and main past its release commit limit, as queuekeeper land warns after its result,
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
    """Run work, then write its exit status or failure beside the source identities."""
    directory.mkdir(parents=True, exist_ok=True)
    try:
        result = work()
        receipt["exitStatus"] = result
    except BaseException as error:
        # A timeout is a wall-clock backstop that a busy host can reach, not a verdict on the work.
        receipt["outcome"] = "unmeasured" if isinstance(error, processes.TimedOut) else "failed-or-interrupted"
        receipt["error"] = str(error)
        raise
    finally:
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


def conclude(directory, receipt, status, **fields):
    """Record a completed run's verdict, the status queuekeeper run returns, beside its command's exit
    status: passed only for 0, so a zero exit that ran too few tests is failed."""
    outcome = "passed" if status == 0 else "unmeasured" if status == lane_results.UNMEASURED else \
        "build-failed" if compile_failed(receipt) else "failed"
    receipt.update(outcome=outcome, **fields)
    (directory / "source.json").write_text(json.dumps(receipt, indent=2) + "\n")


def judge(lane, result, log, extra=(), expect=None):
    """A lane's status and unmeasured tests from its command's status and its log. A completed run
    that executed too few tests raises ValueError."""
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
        try:
            lane_results.require_tests(log, minimum, expected=expect)
        except lane_results.NotRun as error:
            # Such as a corpus test canceled for a variable this lane's command does not set on its Mac
            # (vocaro-engineering#285). The lines give each test's reason: the cause an unmeasured run records.
            not_run = [str(error), *lane_results.canceled_tests(log)]
            print("queuekeeper: unmeasured:", *not_run, sep="\n", file=sys.stderr)
        except ValueError as error:
            # An unmeasured part may not have run at all, so the run stays unmeasured. A test the
            # host left unmeasured ran nothing, so it can also be what leaves a run short.
            if result == 0 and not lane_results.unmeasured_tests(log): raise
            print(f"queuekeeper: {error}", file=sys.stderr)
    unmeasured = lane_results.unmeasured_tests(log) if measured else []
    if unmeasured or not_run or result == lane_results.UNMEASURED:
        # A skip or cancellation leaves the command's own status at 0, and unmeasured is never green.
        if unmeasured:
            print(f"queuekeeper: unmeasured: {len(unmeasured)} test(s) reported unmeasured:", *unmeasured,
                  sep="\n", file=sys.stderr)
        return lane_results.UNMEASURED, list(dict.fromkeys([*not_run, *unmeasured]))
    return result, []


# A lane that says "display": true waits for what the display's refresh times, such as a scroll view's
# animation. Its command runs under a one-second caffeinate -u, which wakes a sleeping display, behind a
# lock too, and then caffeinate -d, which keeps an awake one on but wakes none, and no other assertion
# (guides/background-mac-testing.md, Lock screen and display sleep).
AWAKE = ["sh", "-c", 'caffeinate -u -t 1; exec caffeinate -d -- "$@"', "sh"]


def awake(lane, extra=()):
    """The command a lane runs, with extra appended: under AWAKE when the lane says "display": true,
    where the receipt's command shows the assertions. caffeinate -d execs the command in its own
    process and holds the assertion from a child of it, which ends when the command does, so the
    command keeps the run's PID and status and the assertion ends with it, however it ends (this Mac,
    macOS 27.0.1, 2026-10-28)."""
    command = [*lane["command"], *extra]
    return [*AWAKE, *command] if lane.get("display") else command


def lane_label(root, config, lane):
    """A lane run's admission label: its project's name, a space and the lane, such as `EPUBLib gate`,
    so that two projects' lanes of one name stay apart in waiting messages, the guard and the admission
    log (#51). A space, as in the labels callers pass, such as `warm push REV`, and in an `queuekeeper admit`
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
    """What a run's admission line says of its placement, which `queuekeeper admissions usage` counts: whether queuekeeper offered it
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
            # queuekeeper admit and queuekeeper run start every command they admit through processes.supervise (#315).
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
        # Raised, TimeoutError is an OSError, which queuekeeper run reports as its own error, exit 2 (#268).
        admission = admit(min(FREE_WAIT, timeout) if once else timeout)
        if admission is None:
            print(f"queuekeeper: {name} not run on {this_mac()}: {refused[-1]}", file=sys.stderr)
            return BUSY
        return admission
    except resources.AdmissionRecordError as error:
        print(f"queuekeeper: {name} not run on {this_mac()}: {error}", file=sys.stderr)
        return BUSY
    finally:
        # Admitted, admission took the place out of the queue; otherwise the work no longer waits here.
        if place is not None: place.leave()


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
    """`queuekeeper host screen [--remote]`: one line, or the reading as JSON, saying whether this Mac's or the second
    Mac's console screen is locked (offload_host.screen), exiting 0 unlocked, 1 locked and 2 unknown. The
    second Mac is read over SSH by the same code, so either Mac's answer is read, never assumed."""
    if args.host and not args.remote: raise ValueError("--host names the second Mac that --remote reads")
    if args.remote:
        host = args.host or remote.configured()
        if host is None:
            raise ValueError(f"no second Mac is configured ({remote.HOST_VARIABLE}, or the ssh alias "
                             f"{' or '.join(remote.ALIASES)} in ~/.ssh/config)")
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


def admitted_tree(root, name, offered, receipt):
    """None when work this Mac admitted may run, after recording in receipt the tree it reads, or 2 after
    saying why not. A run here reads the working tree as it is once admitted, not as it was when the
    work was offered or queued: work this Mac takes after sending the second Mac a commit runs only
    while the tree is still that commit's (remote.changed_since), and receipt's sent names that commit."""
    changed = remote.changed_since(root, offered)
    if changed is not None:
        print(f"queuekeeper: {name} not run on this Mac: {changed}\nqueuekeeper: restore the tree that was sent, or run it again to "
              "send the tree as it is now", file=sys.stderr, flush=True)
        return 2
    receipt["project"] = source_identity(root)
    if offered is not None and offered.sent.get("commit"): receipt["sent"] = offered.sent["commit"]
    return None


def from_sent(stack, root, sent, moved, receipt):
    """(checkout, toolkit) of the worktrees from which `queuekeeper admit` runs its command on this Mac, entered in stack,
    after recording them in receipt; or 2 after saying why not. A command this Mac takes after sending the second
    Mac a commit runs from that commit once the tree here is no longer that commit's (remote.moved_trees), in a
    temporary worktree at it, as the second Mac runs it, rather than from the changed tree or not at all: the
    session may go on editing meanwhile, and the result is that of what was sent (vocaro-engineering#276, whose
    command waited behind a gate here and then refused over an unrelated documentation edit). The worktrees go
    when the command ends, however it ends (remote.sent_tree)."""
    why = "; ".join(f"the {what} changed after {commit[:12]} was sent to the second Mac:\n{changes}"
                    for _checkout, commit, what, changes in moved)
    try:
        checkout, toolkit = stack.enter_context(remote.sent_tree(
            root, sent, any(what.startswith("toolkit") for _checkout, _commit, what, _changes in moved)))
    except (OSError, RuntimeError) as error:
        print(f"queuekeeper: the command not run on this Mac: {why}\nqueuekeeper: and no worktree of what was sent could be made "
              f"here: {error}", file=sys.stderr, flush=True)
        return 2
    print(f"queuekeeper: the command runs on this Mac from what was sent, {sent['commit'][:12]}, in a temporary worktree, "
          f"{checkout}, since {why}\nqueuekeeper: the session's tree stays as it is, and the worktree goes when the command "
          "ends", file=sys.stderr, flush=True)
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
    """Why `queuekeeper admit` runs its command on this Mac without offering it to the second Mac, or None to offer it there
    first, as a lane is (the owner's decision, 2026-11-07; guides/shared-host.md, Which Mac runs a lane)."""
    if args.here: return "queuekeeper admit --here"
    # Work the second Mac runs for the Mac that sent it runs there, where it was sent.
    if os.environ.get(remote.DISPATCHER): return "sent here by the Mac that dispatched it"
    # Foreground work holds the foreground of the Mac it runs on (foreground.py).
    if args.foreground: return "foreground work runs on the Mac whose foreground it holds"
    if inherited: return "nested in the admission it inherits"
    # The parts of a self lane share build products, so all run on the Mac where the lane runs (SELF_LANE).
    lane = os.environ.get(SELF_LANE)
    if lane: return f"a part of the self lane {lane}, whose parts share build products on the Mac where it runs"
    return None


# Set by queuekeeper run for the command of a self lane, to the lane's name: each `queuekeeper admit` its script runs stays on the
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
    if not command: raise ValueError("Name the command: queuekeeper admit [--exclusive | --model] [--here] -- COMMAND")
    if args.time_limit <= 0: raise ValueError("--time-limit needs a positive number of seconds")
    config = config if config is not None else load(root)
    kind = ["--exclusive"] if args.exclusive else ["--model"] if args.model else []
    # Only a limit of its own goes to the second Mac, whose toolkit may not take the option.
    if args.time_limit != processes.DEFAULT_LIMIT: kind += ["--time-limit", f"{args.time_limit:g}"]
    # Foreground work holds the foreground of the Mac it runs on: the second Mac's, which queuekeeper takes for it, or this
    # Mac's, which the session holds within the owner's window. queuekeeper remote admit --foreground sends it there.
    second = args.foreground and extensions.LANE.foreground_mac()
    if args.foreground and args.verify:
        raise ValueError("--verify never sends foreground work: send it to the second Mac with "
                         "queuekeeper remote admit --foreground -- COMMAND")
    if args.app and not args.foreground:
        raise ValueError("--app names the app under test of foreground work, whose crash-report alerts queuekeeper clears: "
                         "add --foreground")
    if args.foreground and not second: extensions.LANE.require_foreground()
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
        waiting = extensions.LANE.window_wait("the command", device_window, " ".join(command))
        allowed = waiting.seconds if waiting is not None else lambda seconds: seconds
        closed = extensions.LANE.window_closed("the command", waiting)
        if closed is not None: return closed
        # The second Mac's foreground first, so that a command waiting for it holds no slot.
        waited = extensions.LANE.foreground_held(stack, "the command", " ".join(command), allowed(args.timeout)) if second else None
        if waited is not None: return extensions.LANE.not_admitted("the command", waiting, waited)
        closed = extensions.LANE.window_closed("the command", waiting)
        if closed is not None: return closed
        admission = admitted_here(stack, "the command", " ".join(command), allowed(args.timeout), offered, args.if_free,
                                  args.exclusive, args.model, placement=admission_placement(offered, reason))
        if isinstance(admission, int): return extensions.LANE.not_admitted("the command", waiting, admission)
        sent = offered.sent if offered is not None else {}
        # Once the tree here is no longer what was sent, the command runs from what was sent (#276).
        moved = remote.moved_trees(root, offered)
        tree = from_sent(stack, root, sent, moved, receipt) if moved else None
        if isinstance(tree, int): return tree
        if tree is None:
            receipt["project"] = source_identity(root)
            if sent.get("commit"): receipt["sent"] = sent["commit"]
        receipt["queueSeconds"] = round(time.monotonic() - asked, 3)
        if device_window is not None: extensions.LANE.report_window(device_window, receipt["queueSeconds"])
        if offered is not None:
            receipt["hereBecause"] = offered.status
            fell_back("the command", offered)
        reap_here(root, inherited)
        def work():
            # Finalize the alert report before record() writes the receipt, while admission still holds.
            with resources.child_requests(), contextlib.ExitStack() as alert_stack:
                alerts = extensions.LANE.guarded_alerts(alert_stack, " ".join(command), receipt, args.app or ()) \
                    if args.foreground else None
                if isinstance(alerts, int): return alerts
                # The second Mac's Notification Center windows, under the foreground queuekeeper holds there (#204).
                blocked = extensions.LANE.cleared_notifications(receipt) if second else None
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
                with extensions.LANE.around_run([command], directory):
                    status = processes.supervise(command, cwd=directory, env=variables, on_stop=admission.interrupted,
                                                 timeout=args.time_limit,
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


def main(argv=None):
    report = lane_report.Report()
    try:
        # XcodeGen expands this selected, host-local path before generating its project (#373).
        with patch_environment(extensions.command_environment()):
            status = perform(argv, report)
    except Exception as error:
        report.finish(None, error)
        raise
    report.finish(status)
    return status


def perform(argv, report):
    """Run the command argv names, telling report of a lane run's progress for the summary that ends it."""
    # The project's extensions first, since they add commands: its folder is --project's when that comes first, as
    # a launcher gives it, else the current one, and a linked worktree's extensions are found from its main checkout.
    given = sys.argv[1:] if argv is None else list(argv)
    folder = Path(given[1] if given[:1] == ["--project"] and len(given) > 1 else
                  given[0].split("=", 1)[1] if given[:1] and given[0].startswith("--project=") else ".")
    extensions.load(extensions_of(folder, git_state.main_checkout(folder)))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("doctor")
    run = commands.add_parser("run", usage="%(prog)s [-h] [--expect N] [--repeat N [--erase|--template] [--only TEST]] "
        "lane [-- ARGUMENTS ...]",
        help="run a configured lane, admitting a heavy or exclusive one, on whichever Mac has spare capacity for "
        "it now, the second Mac first, and on this Mac while the second Mac has none or cannot be reached, unless "
        "the lane says why it stays on this Mac: queuekeeper run LANE [-- ARGUMENTS], which appends the arguments, such as "
        "a test filter, to the lane's command")
    run.add_argument("--expect", type=int, metavar="N", help="require exactly N executed tests: N selected with "
        "some skipped or canceled is unmeasured, exit 3, and any other count fails; without it, a run with arguments needs at least one in place of the lane's minimumTests")
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
        "run a locked screen left without a verdict (guides/maintenance.md, A run with no verdict)")
    run.add_argument("--repeat", type=int, metavar="N", help="run a lane of queuekeeper simulator-tests N times, on one build, "
        "each run on a member taken from the pool afresh, and report each run's outcome, test counts and one-minute "
        "load (guides/simulator-testing.md, Repeating a test to show a fix works). Its receipt is not a pass of the "
        "whole lane")
    reset = run.add_mutually_exclusive_group()
    reset.add_argument("--erase", action="store_true", help="with --repeat: erase the member before each run")
    reset.add_argument("--template", action="store_true", help="with --repeat: copy the member's kind's warmed "
        "template before each run; a run the pool could only erase is unmeasured")
    run.add_argument("--only", action="append", default=[], metavar="TEST", help="with --repeat: run only this test, "
        "in place of the lane's own selection; repeat for each")
    run.add_argument("lane")
    # Listed for help only: main passes everything after these to their own parsers.
    for name, (described, _) in extensions.COMMANDS.items(): commands.add_parser(name, help=described)
    for name, (described, add_arguments, _, _) in extensions.PARSED_COMMANDS.items():
        add_arguments(commands.add_parser(name, help=described))
    commands.add_parser("unittest", help="run a unittest suite's modules in processes of their own, side by side: "
        "queuekeeper unittest [-s START] [-p PATTERN] [-j JOBS] [-k PATTERN] [-v] [NAME ...]; queuekeeper unittest -h for the rest")
    admission =commands.add_parser("admit", help="run any other heavy command (a build, swift test, xcodebuild "
        "test, a simulator run, a gate suite) through heavy-work admission on whichever Mac has spare capacity for "
        "it, the second Mac first, as queuekeeper run places a lane: queuekeeper admit -- COMMAND; what it changes in the "
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
        "(guides/second-mac-foreground.md, Crash-report alerts)")
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
    placed.add_argument("--verify", action="store_true", help="what queuekeeper admit does without --here: offer the "
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
        "       %(prog)s recover [OWNER]\n       %(prog)s remove OWNER --why TEXT\n       %(prog)s limits\n"
        "       %(prog)s usage [--since WHEN] [--until WHEN] [--there LOG ...]\n       %(prog)s now [--json] [--here]",
        help="summarize this host's heavy-work admission "
        "log: for each kind and label, the requests admitted and withdrawn, how long they waited and held, and how "
        "admissions ended; queuekeeper admissions recover lists the guard's dead or uncertain entries, and queuekeeper admissions "
        "recover OWNER removes that owner's once a census against its process record finds nothing it ran still "
        "running; queuekeeper admissions remove OWNER --why TEXT removes by hand one recovery cannot prove finished once "
        "nothing its owner may have started is found running; queuekeeper admissions limits prints the admission limits in force and where each comes from; queuekeeper "
        "admissions usage shows both Macs' capacity now, the share of the last 3 hours each spent at each count of "
        "heavy jobs, and why this Mac's work ran here, never offered to the second Mac or declined by it; queuekeeper admissions "
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
        "SSH: queuekeeper remote run LANE [-- ARGUMENTS] or queuekeeper remote admit [--foreground] -- COMMAND; queuekeeper remote passed "
        "LANE says whether the whole lane passed there at a commit; queuekeeper remote foreground says whether its "
        "foreground is ready")
    operations = distant.add_subparsers(dest="operation", required=True)
    remote_run = operations.add_parser("run", usage="%(prog)s [-h] [--host HOST] [--serial] [--expect N] "
        "[--device KEY] [--device-owner SESSION] [--checkout-slot N] [--checkout-wait SECONDS] "
        "lane [-- ARGUMENTS ...]",
        help="run a configured lane on the second Mac at HEAD, or at a snapshot commit of uncommitted work")
    remote_admit = operations.add_parser("admit", help="run a heavy command on the second Mac, in its checkout's "
        "root, at HEAD or at a snapshot commit of uncommitted work")
    remote_setup = operations.add_parser("setup", help="install on the second Mac, with Homebrew, the tools "
        f"every project's gates use ({', '.join(remote.SETUP_FORMULAE)}) and the formulae named, then check them as "
        "queuekeeper remote sees the second Mac; Homebrew itself the owner installs")
    remote_setup.add_argument("formulae", nargs="*", metavar="formula", help="a further Homebrew formula to install")
    remote_setup.add_argument("--import", dest="imports", action="append", metavar="MODULE",
                              help="check that the second Mac's python3 imports MODULE (repeatable)")
    remote_setup.add_argument("--check", action="store_true", help="only check; install or copy nothing")
    remote_foreground = operations.add_parser("foreground", help="say what the second Mac lacks for foreground work, "
        "Mac VoiceOver and Mac UI tests, started over SSH, each with the owner's fix, and which run holds its "
        f"foreground; exit 0 when it lacks nothing, {remote.MISCONFIGURED} otherwise")
    remote_setup.add_argument("--runtimes", action="store_true", help="instead, give this Mac and the second Mac the "
        "same simulator runtimes of the Xcode at this Mac's developer directory, as queuekeeper doctor does: a runtime only "
        "one Mac has is deleted there when no simulator but Xcode's defaults uses it, and otherwise its disk image "
        "is copied over SSH to the Mac that lacks it; it exits 0 only when both list the same")
    for choice in (remote_run, remote_admit, remote_setup, remote_foreground):
        choice.add_argument("--host", help=f"the second Mac's SSH destination (default: {remote.HOST_VARIABLE}, "
                            f"else the ~/.ssh/config alias {' or '.join(remote.ALIASES)} that is defined, else "
                            f"{remote.DEFAULT_HOST})")
    for choice in (remote_run, remote_admit):
        choice.add_argument("--device", action="append", metavar="KEY", help="a physical device the run uses, "
                            "reserved in this Mac's device record and held, with no run active, by the session "
                            "(repeatable); the dispatch records the run in its entry and "
                            "stops it before the reservation ends")
        choice.add_argument("--device-owner", metavar="SESSION", help="the session holding --device (default: "
                            f"{extensions.LANE.device_owner})")
        choice.add_argument("--checkout-slot", metavar="N", help="run in this one of the second Mac's checkouts of the "
                            f"project, 0 to {remote.CHECKOUTS - 1}, waiting for it under the checkout's lock, however "
                            "many others are free, to repeat a check in a named checkout (guides/shared-host.md, A "
                            "named checkout slot); by default the free checkout nearest the commit")
        choice.add_argument("--checkout-wait", type=float, metavar="SECONDS", help="with --checkout-slot, how long to "
                            f"wait for it (default {offload_host.CLAIM_TIMEOUT}); then exit {BUSY} without running")
    remote_run.add_argument("--expect", type=int, metavar="N", help="require exactly N executed tests")
    remote_run.add_argument("--serial", action="store_true", help="run a lane group's lanes one after another, on the "
        "second Mac under its admission: the serial reference of a group that runs there, which counts for no commit")
    remote_run.add_argument("lane")
    kind = remote_admit.add_mutually_exclusive_group()
    kind.add_argument("--exclusive", action="store_true", help="hold every slot of the second Mac")
    kind.add_argument("--model", action="store_true", help="hold one slot and the model lock of the second Mac")
    remote_admit.add_argument("--foreground", action="store_true", help="hold the second Mac's foreground for the "
        "command, as Mac VoiceOver and Mac UI tests need, waiting while another run holds it")
    remote_admit.add_argument("--app", action="append", metavar="NAME", help="with --foreground, the app under "
        "test, as queuekeeper admit's")
    remote_admit.add_argument("--time-limit", type=float, metavar="SECONDS", help="seconds the command may run "
        f"there once admitted, as queuekeeper admit's (default {processes.DEFAULT_LIMIT})")
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
        "queuekeeper disk lists them")
    remote_prune.add_argument("checkout", nargs="?", type=Path, help="a checkout of the repository (default: the project's)")
    remote_prune.add_argument("--dry-run", action="store_true", help="list them; remove nothing")
    merge_queue.add_arguments(commands.add_parser("queue", help="land finished commits through the repository's one "
        "merge queue, which gates batches on origin/main and pushes them; a session adds its commit and moves on "
        "(guides/maintenance.md, The merge queue)"))
    merge_queue.land_arguments(commands.add_parser("land", help="land a finished commit through the merge queue of this "
        "repository, or with --repo of another, and return at once: the queue gates it on origin/main and pushes it"))
    documentation = commands.add_parser("docs", help="check the project's Markdown: each link's file, anchor and "
        "source line exists; exit 1 on any that does not")
    documentation.add_argument("operation", choices=["check"])
    documentation.add_argument("paths", nargs="*")
    spell = commands.add_parser("spelling", help="find British spellings in the project's text, identifiers "
        "included, and file names; exit 1 on any, 2 when nothing was checked")
    spell.add_argument("paths", nargs="*", help="files or directories; untracked files under them are read too")
    disk.add_arguments(commands.add_parser("disk", help="inventory reclaimable disk space; changes nothing without --delete"))
    hosts = commands.add_parser("host", help="read a Mac's state as queuekeeper reads it")
    reads = hosts.add_subparsers(dest="operation", required=True)
    screen = reads.add_parser("screen", help="say whether a Mac's console screen is locked, from both keys the "
        "IORegistry holds and the console user, and exit 0 unlocked, 1 locked or 2 unknown")
    screen.add_argument("--remote", action="store_true", help="read the second Mac's screen over SSH")
    screen.add_argument("--host", help="the second Mac's SSH name, when not the configured one")
    screen.add_argument("--json", action="store_true", help="print the reading as JSON")
    for name, (described, add_arguments, _) in extensions.HOST_READS.items():
        add_arguments(reads.add_parser(name, help=described))
    # Everything after the first -- belongs to the command. Python 3.9's argparse rejects options
    # before it and drops a later --, so argparse never sees it.
    argv = sys.argv[1:] if argv is None else list(argv)
    # unittest's options are its own runner's, which this parser would refuse. It reads no project.
    first = 2 if argv[:1] == ["--project"] else 1 if argv[:1] and argv[0].startswith("--project=") else 0
    if argv[first:first + 1] == ["unittest"]: return parallel.unittest_main(argv[first + 1:])
    project = argv[1] if first == 2 else argv[0].split("=", 1)[1] if first == 1 else None
    if argv[first:first + 1] and argv[first] in extensions.COMMANDS:
        return extensions.COMMANDS[argv[first]][1](argv[first + 1:], project)
    # Recovery, like the admission log, covers the whole host and reads no project.
    if argv[first:first + 2] == ["admissions", "recover"]: return resources.recover_main(argv[first + 2:])
    if argv[first:first + 2] == ["admissions", "remove"]: return resources.remove_main(argv[first + 2:])
    if argv[first:first + 2] == ["admissions", "limits"]: return resources.limits_main(argv[first + 2:])
    if argv[first:first + 2] == ["admissions", "usage"]: return resources.usage_main(argv[first + 2:])
    if argv[first:first + 2] == ["admissions", "now"]: return resources.now_main(argv[first + 2:])
    split = argv.index("--") if "--" in argv else len(argv)
    args = parser.parse_args(argv[:split])
    # Only the remote work itself reports its admission. Provisioning and nested commands do not.
    device_window = extensions.LANE.take_window()
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
    extensions.LANE.recover()
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
        # The store is the repository's, named by any checkout of it, and queuekeeper disk runs this for every project.
        if args.action == "remote" and args.operation == "prune-results":
            return remote.prune_results_main(args.checkout or root, args.dry_run)
        config = load(root)
        if args.action in {"run", "admit"} or (args.action == "remote" and args.operation in {"run", "admit"}):
            for check in extensions.BEFORE_LANE: check(root, config)
        if args.action in extensions.PARSED_COMMANDS: return extensions.PARSED_COMMANDS[args.action][2](root, args, config)
        if args.action == "doctor":
            required = {"git", "python3"}
            for needed in extensions.REQUIRED_COMMANDS: required |= set(needed(config))
            missing = sorted(name for name in required if not shutil.which(name))
            warnings = []
            unbounded = sorted(name for name, lane in config.get("lanes", {}).items()
                               if "command" in lane and "timeout" not in lane)
            if unbounded:
                warnings.append(f"{len(unbounded)} lane(s) declare no timeout and are stopped, unmeasured, after "
                                f"queuekeeper's default {processes.DEFAULT_LIMIT}s: {', '.join(unbounded)}; give each a timeout "
                                "chosen from its measured time (guides/parallel-tests.md, Bound every run)")
            if config.get("lanes") and "gate" not in config["lanes"] and not config.get("queue", {}).get("lanes") \
                    and config.get("integration", "direct-main") == "direct-main":
                warnings.append("no lane is named gate, which queuekeeper land runs: name the lane holding "
                                "what a commit must pass before it lands gate (guides/adoption.md, Lane names)")
            warnings += budgets.doctor_warnings(config)
            warnings += self_lane_warnings(root, config)
            warnings += display_lane_warnings(config)
            warnings += split_warnings(root, config)
            from .waiting import doctor_warnings as fixed_wait_warnings  # #284
            warnings += fixed_wait_warnings(root)
            warnings += lane_wait.admission_warnings(root)
            # A difference between the two Macs is an error; a second Mac that cannot be reached is unchecked.
            hosts = fix_hosts(root, remote.compare_hosts())
            for check in extensions.DOCTOR: warnings += check(root, config)
            test_lanes = lane_coverage.report(root, config)  # a warning at most, and never a failure (#289)
            warnings += lane_coverage.doctor_warnings(test_lanes)
            print(json.dumps({"project": config["name"], "queuekeeper": str(TOOLKIT_ROOT),
                "source": source_identity(TOOLKIT_ROOT), "extensions": list(extensions.LOADED),
                "missingCommands": missing, "warnings": warnings, "testLanes": test_lanes, "offload": hosts}, indent=2))
            return int(bool(missing or hosts["errors"]))
        elif args.action == "admit": return admit(root, args, config, device_window=device_window)
        elif args.action == "remote": return remote.main(root, config, args)
        elif args.action == "docs":
            failures = docs.check(root, args.paths or None)
            print("\n".join(failures) if failures else "Documentation checks passed")
            return int(bool(failures))
        elif args.action == "spelling": return spelling.check(root, config, args.paths)
        elif args.action == "run":
            report.begin(root, args.lane)
            if args.build_host_only:
                raise ValueError("--build-host-only was --offload-only's name until vocaro-engineering#58, and is no "
                                 "longer read: use --offload-only (guides/shared-host.md, Earlier names)")
            lane = lane_to_run(config, args.lane)
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
            # Lane arguments, so that queuekeeper simulator-tests finds them on whichever Mac runs the lane.
            extra = [*series, *(args.passed or [])]
            # A series' lane runs N times inside its one admission, within N times its limit (guides/simulator-testing.md).
            limit = parallel.lane_timeout(lane, extra)
            lane_wait.begin(root, args.lane)
            # A foreground lane takes a Mac's screen. The second Mac's is queuekeeper's to take for the run, below; this
            # Mac's, only the session holding mac-foreground within the owner's window runs it on, with --here.
            # Otherwise it goes to the second Mac and waits there for its foreground, never falling back to this
            # Mac (foreground.py).
            second = lane.get("foreground") and extensions.LANE.foreground_mac()
            if lane.get("foreground") and not second:
                if args.offload_only or not (args.here or lane.get("here")):
                    return remote.foreground_lane(root, config, args.lane, extra, args.expect)
                extensions.LANE.require_foreground()
            group = report.group = lane.get("parallel")
            if group is not None and (extra or args.expect):
                raise ValueError(f"{args.lane} runs a group of lanes, which takes no arguments or --expect; "
                                 "pass them to one of its lanes")
            if group is None and args.serial: raise ValueError(f"--serial runs a lane group, and {args.lane} is not one")
            # Why the lane runs only here, as its configuration says, or None: it goes to the second Mac.
            reason = stays(config["lanes"], args.lane)
            # Keep the queue's placement scope at the outer lane.
            queue_gate = bool(os.environ.pop(remote.QUEUE_GATE, None))
            exclude = os.environ.pop(remote.QUEUE_GATE_EXCLUDE, None)
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
            reads_screen = needs or any(config["lanes"][name].get("display") for name in (group or [args.lane]))
            policy_commit = refresh_policy(root) if resource != "self" else None
            directory = root / ".build/queuekeeper" / args.lane
            receipt = {"project": source_identity(root), "toolkit": source_identity(TOOLKIT_ROOT),
                       "policy": policy_commit, **placement(reason)}
            # A run of the whole lane, which a pass of counts for its commit once kept (concluded).
            whole = not extra and args.expect is None and not args.serial
            keys = lane_devices(lane)
            admission = None
            if whole and receipt["project"].get("workingChangesSHA256") != remote.CLEAN:
                # vocaro-engineering#197: nothing said so until eng release stopped for it.
                why = remote.uncounted(root, args.lane)
                if why: print(f"queuekeeper: {why}", file=sys.stderr, flush=True)
            def concluded(status, **fields):
                # The verdict in this checkout, and kept by commit for every worktree (vocaro-engineering#198).
                conclude(directory, receipt, status, **fields)
                remote.keep_here(root, args.lane, receipt, status, whole, results=directory / "results")
                # A lane's output goes to its log; the Mac that sent a dispatch prints these once they are back.
                if not os.environ.get(remote.DISPATCHER): extensions.LANE.report_results(directory / "results")
            def prepared(work):
                # What an earlier run left in results/ is not this run's, here as on the second Mac (remote.RESULTS).
                # The lane finds the directory in QUEUEKEEPER_RESULTS_DIR, and a group's lanes each find one of their own in it.
                shutil.rmtree(directory / "results", ignore_errors=True)
                report.ran_here(directory)
                if resource != "self":
                    for prepare in extensions.PREPARE_LANE: prepare(root, config)
                commands = [config["lanes"][name]["command"] for name in group] if group is not None else \
                    [lane["command"], *([lane["compileFirst"]] if "compileFirst" in lane else [])]
                try:
                    with resources.child_requests(), extensions.LANE.around_run(commands, root): return work()
                finally:
                    # Nested in another lane, its results go into that lane's too, however it ended (#218).
                    parallel.hand_up(directory / "results", args.lane)
            if group is None:
                command = awake(lane, extra)
                receipt.update(command=command, arguments=extra, timeout=limit)
                def execute(on_stop=None):
                    def work():
                        env = shims.environment(parallel.results_environment(directory / "results"), root)
                        kill_after = processes.FOREGROUND_KILL_AFTER if lane.get("foreground") else processes.KILL_AFTER
                        failed, left = compile_first(lane, root, directory, receipt, limit,
                                                     env, on_stop, kill_after)
                        if failed is not None: return failed
                        return processes.run(command, cwd=root, log_path=directory / "run.log", timeout=left,
                                             on_stop=on_stop, env=env, kill_after=kill_after,
                                             **({"stall": lane["stall"], "progress_path": directory / "results/progress.json"}
                                                if "stall" in lane else {}))
                    return record(directory, receipt, lambda: prepared(work))
            else:
                jobs = 1 if args.serial else lane.get("jobs", len(group))
                receipt.update(parallel=group, jobs=jobs)
                # Each lane's receipt shows the command it ran, under the display's assertions or not.
                members = [(name, {**config["lanes"][name], "command": awake(config["lanes"][name])}) for name in group]
                def execute(on_stop=None):
                    return record(directory, receipt, lambda: prepared(lambda: parallel.lanes(root, members, directory / "lanes", jobs,
                                  lambda member, status, log: judge(member, status, log)[0], receipt, on_stop,
                                  results_directory=directory / "results")))
            try:
                with processes.forwarding_signals():
                    if resource == "self":
                        if device_window is not None: extensions.LANE.report_window(device_window, 0)
                        scroll_bars_recorded(receipt, args.lane, inherited)
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
                                extensions.LANE.held_window(keys)
                            waiting = extensions.LANE.window_wait(args.lane, device_window,
                                                  lane_label(root, config, args.lane) if whole else None,
                                                  keys=keys or None,
                                                  budget=(lane.get("deviceRunMinutes", 0) * 60 or None) if whole else None,
                                                  fallback=limit if whole and keys else None)
                            allowed = waiting.seconds if waiting is not None else lambda seconds: seconds
                            closed = extensions.LANE.window_closed(args.lane, waiting)
                            if closed is not None: return closed
                            # The second Mac's foreground first, so that a run waiting for it holds no slot.
                            waited = extensions.LANE.foreground_held(stack, args.lane, lane_label(root, config, args.lane),
                                                     allowed(remote.OVERFLOW_TIMEOUT),
                                                     expected=lane.get("timeout")) if second else None
                            if waited is not None: return extensions.LANE.not_admitted(args.lane, waiting, waited)
                            closed = extensions.LANE.window_closed(args.lane, waiting)
                            if closed is not None: return closed
                            admission = admitted_here(stack, args.lane, lane_label(root, config, args.lane),
                                                      allowed(remote.OVERFLOW_TIMEOUT), offered, args.if_free,
                                                      resource == "exclusive", lane.get("model", False),
                                                      lane.get("wholeHost", False),
                                                      needs and not os.environ.get(resources.TOKEN),
                                                      admission_placement(offered, reason))
                            if isinstance(admission, int): return extensions.LANE.not_admitted(args.lane, waiting, admission)
                            stopped = admitted_tree(root, args.lane, offered, receipt)
                            if stopped is not None: return stopped
                            # The wait in the queue, apart from what came before it (remote.execute).
                            receipt["queueSeconds"] = round(time.monotonic() - asked, 3)
                            if device_window is not None: extensions.LANE.report_window(device_window, receipt["queueSeconds"])
                            if offered is not None:
                                receipt["hereBecause"] = offered.status
                                fell_back(args.lane, offered)
                            reap_here(root, inherited)
                            scroll_bars_recorded(receipt, args.lane, inherited)
                            alerts = extensions.LANE.guarded_alerts(stack, args.lane, receipt, lane.get("apps", ())) \
                                if lane.get("foreground") else None
                            if isinstance(alerts, int): return alerts
                            blocked = extensions.LANE.cleared_notifications(receipt) if second else None
                            if blocked is not None: return blocked
                            closed = extensions.LANE.window_closed(args.lane, waiting)
                            if closed is not None: return closed
                            if reads_screen: receipt["screen"] = screen_reading()
                            if inherited and needs and receipt["screen"]["state"] != "unlocked":
                                print(f"queuekeeper: {args.lane} not run on {this_mac()}: "
                                      f"{remote.screen_refusal(receipt['screen'], this_mac())}; "
                                      "release the outer admission before waiting for an unlocked screen and retry",
                                      file=sys.stderr, flush=True)
                                return BUSY
                            result = execute(admission.interrupted)
                            # A failed build ran no test an alert could have disturbed.
                            if alerts is not None and not compile_failed(receipt): result = alerts.judged(result)
            except processes.TimedOut as error:
                # The lane's timeout is its backstop, so a run a busy host slowed past it is
                # unmeasured (guides/measurement.md). Its log is incomplete and counts nothing.
                print(f"queuekeeper: unmeasured: {error}", file=sys.stderr)
                report.explain(error, complete=False)
                return lane_results.UNMEASURED
            # A group's lanes were each judged by their own logs.
            if "parallel" in lane:
                if reads_screen and result == lane_results.UNMEASURED: unmeasured_screen(args.lane, receipt)
                concluded(result)
                return result
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
            if status == 0 and whole and keys and admission is not None and extensions.LANE.device_lane_passed(directory / "results", keys):
                admission.device_lane_passed(keys)
            return status
        return 0
    except (remote.Lost, extensions.Unavailable) as error:
        print(f"queuekeeper: {error}", file=sys.stderr)
        report.explain(error)
        return lane_results.UNMEASURED
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"queuekeeper: {error}", file=sys.stderr)
        report.explain(error)
        return 2
    finally:
        lane_wait.finish()
