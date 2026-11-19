"""`qk unity`: a Unity project's tests, a command run while its editor is closed, and a worktree's editor.

    qk unity test [--platform EditMode|PlayMode] [--results DIR]
    qk unity closed -- COMMAND
    qk unity ready [--exclusive] -- COMMAND
    qk unity open [--ensure | --status]
    qk unity editor
    qk unity run --log LOG -- COMMAND

Each exits 3, unmeasured, when nothing ran for a reason that says nothing of the work: no editor on this Mac, an
editor holding the project, a licensing helper that would not stop, a run that hung. The merge queue tries such a
run again and then holds it, and never sends the commit back as failed."""
import argparse
import datetime
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

from queuekeeper import config as configuration, plugins
from queuekeeper.lane_results import UNMEASURED
from queuekeeper.parallel import RESULTS_VARIABLE
from . import batch, editor, host, licensing, project as projects, results as results_kept, settings

FAILED, USAGE = 1, 2
COMPILE_ERROR = re.compile(r"error CS\d+: .*")
# The seconds `closed` waits for the editor it asked to quit. An editor with unsaved changes asks about them and
# stays, and it is never force-killed.
CLOSE_WAIT = 90
# In a linked worktree's git directory, once `open` has opened the worktree's editor or found it open.
MARKER = "queuekeeper-unity-editor-opened"
# The variable by which a session that needs no editor declines `open`: QUEUEKEEPER_UNITY_OPEN=0 (vocaro/mpm#102).
OPEN_VARIABLE = "QUEUEKEEPER_UNITY_OPEN"


def say(line):
    print(f"queuekeeper unity: {line}", flush=True)


def warn(line):
    print(f"queuekeeper unity: {line}", file=sys.stderr, flush=True)


def unmeasured(reason):
    warn(f"unmeasured: {reason}; nothing ran")
    return UNMEASURED


def found_editor(project):
    """(binary, None) for the project's editor after saying which it is, or (None, the status to exit with)."""
    binary, source, want = editor.find(project)
    if binary is None:
        return None, unmeasured(f"this Mac has no Unity editor for {project} (it pins {want or 'no version'}); install "
                                f"it with Unity Hub, or set ${editor.VARIABLE} to an editor's binary")
    (say if editor.matches(binary, want) else warn)(editor.described(binary, source, want))
    return binary, None


def ready(project):
    """None once a batch run may start on project: no editor has it open, no stale lock is left, and no orphaned
    licensing helper waits for the next editor. Otherwise the status to exit with, after saying why."""
    try: running = projects.on(project, projects.editors())
    except projects.Unreadable as error: return unmeasured(str(error))
    if running:
        return unmeasured(f"Unity is running on {project} (process {', '.join(str(pid) for pid, _ in running)}); close "
                          "that editor, or run the command under `qk unity closed --`")
    try: projects.clear_stale_lock(project, say=warn)
    except projects.Held as held: return unmeasured(str(held))
    try: licensing.repair(say=say)
    except licensing.Unrepaired as error: return unmeasured(str(error))
    return None


def results_folder(named):
    if named: folder = Path(named)
    elif os.environ.get(RESULTS_VARIABLE): folder = Path(os.environ[RESULTS_VARIABLE]) / results_kept.FOLDER
    else: folder = Path(tempfile.mkdtemp(prefix="queuekeeper-unity-"))
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def report(results):
    """Print a test run's counts and each test that did not pass, and return whether none failed. The count is
    said as "Executed N tests", which a lane's minimumTests reads."""
    root = ET.parse(results).getroot()
    total, passed, failed = (int(root.get(key, "0")) for key in ("total", "passed", "failed"))
    skipped = int(root.get("skipped", "0")) + int(root.get("inconclusive", "0"))
    say(f"{passed}/{total} passed, {failed} failed")
    print(f"Executed {total} tests" + (f", with {skipped} tests skipped" if skipped else ""), flush=True)
    for case in root.iter("test-case"):
        if case.get("result") in ("Passed", "Skipped", "Inconclusive"): continue
        print(f"  {case.get('result')}: {case.get('fullname')}")
        message = case.find(".//message")
        if message is not None and (message.text or "").strip():
            print(f"     {message.text.strip().splitlines()[0][:160]}")
    return failed == 0


def test(project, args):
    """Run the project's tests in a batch editor. No -nographics: without a graphics device Unity never loads
    compute shaders, and compiling and opening a project that has them fails."""
    binary, status = found_editor(project)
    if binary is None: return status
    status = ready(project)
    if status is not None: return status
    folder = results_folder(args.results)
    results, log = folder / "results.xml", folder / "tests.log"
    results.unlink(missing_ok=True)
    ended = batch.run([str(binary), "-batchmode", "-projectPath", str(project), "-runTests", "-testPlatform",
                       args.platform, "-testResults", str(results), "-logFile", str(log)],
                      log, project, quiet=batch.quiet(), say=warn)
    if ended == batch.HUNG: return unmeasured(f"the {args.platform} run hung and was ended ({log})")
    text = log.read_text(errors="replace") if log.exists() else ""
    errors = sorted(set(COMPILE_ERROR.findall(text)))
    if errors:
        warn(f"FAIL: {len(errors)} compile error(s)")
        for line in errors[:20]: print(f"  {line}", file=sys.stderr)
        return FAILED
    if not results.exists():
        if licensing.never_started(text):
            return unmeasured("Unity licensing did not initialize; if it persists, open the editor by hand once to "
                              "refresh licensing")
        warn(f"FAIL: no test results were written (editor exit {ended}, {log})")
        for line in text.splitlines()[-20:]: print(f"  {line}", file=sys.stderr)
        return FAILED
    return 0 if report(results) else FAILED


def closed(project, command):
    """Close the project's editor, run command, and open the editor again when one was closed. The editor is asked
    with SIGTERM and never force-killed: one that stays, for a prompt about unsaved changes, ends this unmeasured
    with nothing run."""
    binary, status = found_editor(project)
    if binary is None: return status
    try: open_editors = projects.interactive(projects.on(project, projects.editors()))
    except projects.Unreadable as error: return unmeasured(str(error))
    closing = open_editors[0][0] if open_editors else None
    if closing is None: say(f"no editor open on {project}; running directly")
    else:
        say(f"closing editor pid={closing} (SIGTERM)")
        try: os.kill(closing, signal.SIGTERM)
        except ProcessLookupError: pass
    waited, wait = 0, CLOSE_WAIT
    while True:
        try: left = projects.on(project, projects.editors())
        except projects.Unreadable as error: return unmeasured(str(error))
        if not left: break
        if waited >= wait:
            return unmeasured(f"the editor still holds {project} after {wait}s (a prompt about unsaved changes?) and "
                              "is not force-killed")
        time.sleep(2)
        waited += 2
    if waited: say(f"editor closed after about {waited}s")
    not_reopened = f"; the editor this closed (pid {closing}) was not opened again" if closing else ""
    # The file itself, so that an editor started with another spelling of the project's path cannot hide.
    try: projects.clear_stale_lock(project, say=warn)
    except projects.Held as held: return unmeasured(f"{held}{not_reopened}")
    try: licensing.repair(say=say)
    except licensing.Unrepaired as error: return unmeasured(f"{error}{not_reopened}")
    say(f"running: {' '.join(command)}")
    try: status = subprocess.run(command).returncode
    except OSError as error:
        warn(f"could not run {command[0]}: {error}")
        status = USAGE
    say(f"command exit={status}")
    if closing is not None:
        say("opening the editor again; it is not ready until its import ends")
        editor.launch(binary, project)
    return status


def which_editor(project):
    """Print the binary of the project's editor, alone on standard output, for a script that starts Unity itself.
    Which editor it is, and whether it is the pinned one, goes to standard error."""
    binary, source, want = editor.find(project)
    if binary is None:
        return unmeasured(f"this Mac has no Unity editor for {project} (it pins {want or 'no version'}); install it "
                          f"with Unity Hub, or set ${editor.VARIABLE} to an editor's binary")
    warn(editor.described(binary, source, want))
    print(binary, flush=True)
    return 0


def bounded(project, command, log):
    """Run command, a Unity batch run on project that writes log (-logFile), and end it when the log goes silent
    (batch.run). The status is the command's own, or batch.HUNG, 124, for a run that was ended: a script that
    runs many units in one editor knows, and this does not, whether such a run had given its verdict."""
    try: return batch.run(command, log, project, quiet=batch.quiet(), say=warn)
    except OSError as error:
        warn(f"could not run {command[0]}: {error}")
        return USAGE


def replace(command):
    """Become command, which keeps this process's PID and gives its own status."""
    sys.stdout.flush()
    sys.stderr.flush()
    os.execvp(command[0], command)


def idle():
    """None when no editor on this Mac is known to be in Play mode, else the status to exit with, after saying
    which is. An editor whose Play state cannot be asked is not held against the run."""
    try: playing = host.in_play()
    except projects.Unreadable as error: return unmeasured(str(error))
    if not playing: return None
    named = ", ".join(f"process {entry['pid']} on {entry['project']}" for entry in playing)
    return unmeasured(f"a Unity editor is in Play mode ({named}), which uses the GPU this exclusive work measures "
                      "on; leave Play mode in it")


def ready_then(project, command, exclusive=False):
    """Run command once a batch run may start on project (ready), as a lane that says "unity": true does. It ends
    unmeasured, with command not run, when this Mac has no editor or one may not start, and for exclusive work
    when an editor on this Mac is in Play mode (idle)."""
    binary, status = found_editor(project)
    if binary is None: return status
    status = ready(project)
    if status is None and exclusive: status = idle()
    if status is not None: return status
    try: replace(command)
    except OSError as error:
        warn(f"could not run {command[0]}: {error}")
        return USAGE


def git_directories(root):
    """(this worktree's git directory, the repository's), or None outside a repository."""
    ran = subprocess.run(["git", "-C", str(root), "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir"],
                         capture_output=True, text=True)
    found = ran.stdout.split("\n")[:2]
    return (Path(found[0]), Path(found[1])) if ran.returncode == 0 and len(found) == 2 and all(found) else None


def mark(marker, project, result):
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    marker.write_text(f"project={project}\nhandled={stamp}\nresult={result}\n")


def seed_layout(project, version, template):
    """Give a project with no window layout of its own the template's, before its first launch. Without a
    project-level layout Unity takes the layout whichever editor on the Mac saved last. One a worktree already
    has is never replaced."""
    target = project / f"UserSettings/Layouts/default-{version.split('.')[0]}.dwlt"
    if template is None or not template.is_file() or target.exists(): return
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(template.read_bytes())
        say(f"seeded the window layout from {template.name}")
    except OSError as error:
        warn(f"could not seed the window layout into {target} ({error}); opening anyway")


def open_editor(root, project, mode, layout, environ=None):
    """Open a linked worktree's editor once. `once` is for a post-checkout hook: it does nothing in the main
    checkout, after the first time, or when the session declined it. `ensure` opens it now. `status` only says
    whether one is open, exiting 0 when it is and 1 when not."""
    environ = os.environ if environ is None else environ
    directories = git_directories(root)
    if directories is None:
        warn(f"{root} is not in a git repository")
        return USAGE
    own, common = directories
    if mode == "once":
        if environ.get(OPEN_VARIABLE) == "0":
            say(f"not opening an editor: {OPEN_VARIABLE}=0")
            return 0
        if own == common:
            say("main checkout; an editor is opened by itself only for a linked worktree")
            return 0
        if (own / MARKER).exists():
            say("already handled for this worktree")
            return 0
    try: is_open = bool(projects.interactive(projects.on(project, projects.editors())))
    except projects.Unreadable as error: return unmeasured(f"{error}, so no editor was opened")
    if is_open:
        if mode != "status": mark(own / MARKER, project, "already-open")
        say(f"editor already open for {project}")
        return 0
    if mode == "status":
        say(f"no editor open for {project}")
        return FAILED
    binary, status = found_editor(project)
    if binary is None: return status
    try: licensing.repair(say=say)
    except licensing.Unrepaired as error: return unmeasured(str(error))
    seed_layout(project, editor.pinned(project) or "", layout)
    try: editor.launch(binary, project)
    except (OSError, subprocess.CalledProcessError) as error:
        return unmeasured(f"the Unity launch failed for {project} ({error})")
    mark(own / MARKER, project, "launched")
    say(f"opened Unity for {project}")
    return 0


def main(arguments, project=None):
    arguments = list(arguments)
    command = None
    if "--" in arguments:
        split = arguments.index("--")
        arguments, command = arguments[:split], arguments[split + 1:]
    parser = argparse.ArgumentParser(prog="qk unity", description="Run a Unity project's editor unattended. "
                                     "Exit 3 means nothing ran, for a reason that says nothing of the work.")
    actions = parser.add_subparsers(dest="action", required=True)
    tests = actions.add_parser("test", help="run the project's tests in a batch editor and report each that failed")
    tests.add_argument("--platform", choices=["EditMode", "PlayMode"], default="EditMode")
    tests.add_argument("--results", metavar="DIR", help="where results.xml and tests.log go (default: the lane's "
                       "results folder, or a temporary one)")
    actions.add_parser("closed", help="close the project's editor, run COMMAND, and open the editor again: "
                       "qk unity closed -- COMMAND")
    readiness = actions.add_parser("ready", help="run COMMAND once a batch run may start on the project, and exit 3 "
                                   "with nothing run when one may not, as a lane that says \"unity\": true does: "
                                   "qk unity ready -- COMMAND")
    readiness.add_argument("--exclusive", action="store_true", help="also exit 3 while an editor on this Mac, on any "
                           "project, is in Play mode, as an exclusive lane does")
    opened = actions.add_parser("open", help="open this linked worktree's editor, once; for a post-checkout hook, "
                                f"which a session declines with {OPEN_VARIABLE}=0")
    how = opened.add_mutually_exclusive_group()
    how.add_argument("--ensure", action="store_true", help="open it now, whether or not it was opened before")
    how.add_argument("--status", action="store_true", help="only say whether one is open: exit 0 when it is, 1 when not")
    actions.add_parser("editor", help="print the binary of the editor the project pins, for a script that starts "
                       "Unity itself; exit 3 when this Mac has none")
    running = actions.add_parser("run", help="run COMMAND, a Unity batch run that writes LOG, and end it when LOG "
                                 "has been silent for ten minutes, exiting 124: qk unity run --log LOG -- COMMAND")
    running.add_argument("--log", required=True, type=Path, help="the file the run's -logFile names")
    args = parser.parse_args(arguments)
    if (command is not None) != (args.action in ("closed", "ready", "run")) or command == []:
        parser.error("closed, ready and run take the command after --, and only they do")
    root = Path(project or ".").resolve()
    try: config = configuration.load(root)
    except (OSError, ValueError) as error:
        warn(f"{root / configuration.FILE}: {error}")
        return USAGE
    found = settings.project(root, config)
    if not (found / "ProjectSettings").is_dir():
        warn(f"no Unity project at {found}: name its folder in {configuration.FILE}, as \"unity\": {{\"project\": \"lab\"}}")
        return USAGE
    if args.action == "test": return test(found, args)
    if args.action == "editor": return which_editor(found)
    if args.action == "run": return bounded(found, command, args.log)
    if args.action == "closed": return closed(found, command)
    if args.action == "ready": return ready_then(found, command, args.exclusive)
    return open_editor(root, found, "ensure" if args.ensure else "status" if args.status else "once",
                       settings.layout(root, config))


def register():
    """Add the command to the core."""
    # The batch run is heavy work, so one started outside an admission is admitted first (cli.bare_runner).
    plugins.RUNNERS.add(("unity", "test"))
    plugins.COMMANDS["unity"] = (
        "run a Unity project's editor unattended: qk unity test runs its tests in a batch editor, qk "
        "unity closed -- COMMAND runs COMMAND with its editor closed and opens it again, and qk unity open "
        "opens a linked worktree's editor once; -h for the rest", main)
