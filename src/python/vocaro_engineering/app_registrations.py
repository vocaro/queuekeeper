"""Unregister the Mac apps a run built from LaunchServices and pluginkit when the run ends (#87).

Xcode registers a Mac app when it builds it, and macOS registers one when it launches, and neither
is undone. On 2026-10-31 the second Mac had 22 registered builds of one app, whose Quick Look
extensions shared their bundle identifiers and version, and the system ran an old copy's extension
instead of the build under test. So every outermost `eng run` and `eng admit` (around_run) watches
its descendants for `xcodebuild -derivedDataPath FOLDER` and, however it ends, runs `lsregister -u`
on each Mac app under those folders and `pluginkit -r` on each `Contents/PlugIns/*.appex` of it.

What a run built is what lies in a folder its own xcodebuild named: an app an incremental build or
`test-without-building` found up to date counts, because launching it registers it again. Nothing
outside the folder is touched, through a symbolic link or otherwise. A folder is left alone while
another live run names an overlapping one or a process outside this run's lineage names a path in
it. Nested runs leave the work to the outermost, so a run keeps its registrations until it ends.

Each run keeps a journal, written as soon as it sees a folder. SIGKILL runs
no cleanup, so the journal stays, its builds stay registered, and the next run on that Mac sweeps it
once its owner is gone and the folder is free. A cleanup that fails or is put off stays the same way.
Not covered: xcodebuild without -derivedDataPath, whose shared default folder belongs to no run (the
run says so); a copy of a build outside the folder; a folder removed before the cleanup, since
nothing is left to name to lsregister; and an xcodebuild shorter than one sample that names a folder
only in a script (guides/shared-host.md, Registered Mac builds).
"""
import contextlib
import json
import os
from pathlib import Path
import pwd
import re
import subprocess
import sys
import threading
import time
import uuid

from . import offload_host
from .resources import process_start

LSREGISTER = ("/System/Library/Frameworks/CoreServices.framework/Versions/Current/Frameworks/"
              "LaunchServices.framework/Versions/Current/Support/lsregister")
PLUGINKIT = "/usr/bin/pluginkit"
# Marks a nested run, whose outermost run does the work; the value is that run's PID.
RUN = "VOCARO_APP_REGISTRATIONS_RUN"
JOURNALS = "app-registrations"
SAMPLE_SECONDS = 0.5
TOOL_SECONDS = 30
PROCESSES = ["ps", "-axww", "-o", "pid=,ppid=,uid=,lstart=,args="]
# What a derived-data folder holds besides builds: much to walk, and no app anyone registers.
UNBUILT = {"Index.noindex", "ModuleCache.noindex", "SourcePackages", "Logs", "CompilationCache.noindex",
           "SDKStatCaches.noindex"}
ACTIONS = {"build", "build-for-testing", "test", "test-without-building", "archive", "analyze"}
# Where a command line's path ends, since ps does not quote one: at the next option, setting or action.
_PATH_END = r"(?=\s+-[A-Za-z]|\s+[A-Z][A-Z0-9_]*=|\s+(?:" + "|".join(sorted(ACTIONS)) + r")(?:\s|$)|\s*$)"
_FOLDER = re.compile(r"(?:^|\s)-derivedDataPath[ =](.+?)" + _PATH_END)
_RUN_FILE = re.compile(r"(?:^|\s)-xctestrun[ =](.+?)/Build/Products/[^/]*?" + _PATH_END)


def is_xcodebuild(args):
    """Whether a command line runs xcodebuild itself, not a script or a tool that mentions it."""
    words = args.split(None, 2)
    if words and Path(words[0]).name == "xcrun": words = words[1:]
    return bool(words) and Path(words[0]).name == "xcodebuild"


def named_folders(args):
    """The folders one xcodebuild command line names as it is written, relative ones included."""
    return [found.group(1) for pattern in (_FOLDER, _RUN_FILE) for found in pattern.finditer(args)]


def builds_unnamed(args):
    """Whether this xcodebuild builds or tests in the default folder, which no run owns."""
    return is_xcodebuild(args) and not named_folders(args) and bool(ACTIONS & set(args.split()))


def refusal(folder, cwd):
    """Why folder cannot be a run's derived data, or None. A path mistaken for one must not unregister
    a whole home or checkout, and the default folder holds every session's builds."""
    home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    if folder in (home, *home.parents): return "it holds the home folder"
    if folder == Path(cwd).resolve() or folder in Path(cwd).resolve().parents: return "it holds the run's own directory"
    if folder == home / "Library/Developer/Xcode/DerivedData": return "it is Xcode's folder for every session's builds"
    if (folder / ".git").exists(): return "it is a checkout"
    return None


def inside(path, folder):
    try:
        Path(path).resolve().relative_to(folder)
        return True
    except (OSError, ValueError):
        return False


def built(folder):
    """The Mac apps under a resolved folder and the extensions in their PlugIns folders, none reached
    through a symbolic link. An iOS app has no Contents folder, and LaunchServices does not list it."""
    apps, extensions = [], []
    for directory, names, _files in os.walk(folder):
        names[:] = [name for name in names if name not in UNBUILT and not os.path.islink(os.path.join(directory, name))]
        if not directory.endswith(".app"): continue
        app = Path(directory)
        if not (app / "Contents/Info.plist").is_file() or not inside(app, folder): continue
        apps.append(app)
        extensions += [path for path in sorted((app / "Contents/PlugIns").glob("*.appex"))
                       if not path.is_symlink() and inside(path, folder)]
    return sorted(apps), extensions


def overlapping(one, other):
    return one == other or one in other.parents or other in one.parents


def journal_directory():
    return offload_host.admission_directory() / JOURNALS


def read_journals(skip=None):
    """Every other run's journal, as (path, entry); one that cannot be read is entered as None."""
    found = []
    try: paths = sorted(journal_directory().glob("*.json"))
    except OSError: return found
    for path in paths:
        if path == skip: continue
        try:
            entry = json.loads(path.read_text())
            if not isinstance(entry, dict) or not isinstance(entry.get("folders"), list): entry = None
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            entry = None
        found.append((path, entry))
    return found


def live(entry, table):
    """Whether a journal's run still runs. Only a PID gone, or one with another start, shows it does not."""
    pid, start = entry.get("pid"), entry.get("start")
    if type(pid) is not int or pid not in table: return type(pid) is not int
    return not isinstance(start, str) or " ".join(start.split()) == table[pid]["start"]


def hold(folder, table, others, own):
    """Why folder's registrations must stay for now, or None."""
    for _path, entry in others:
        if entry is None: return "a run's journal cannot be read"
        if live(entry, table) and any(overlapping(folder, Path(other)) for other in entry["folders"]):
            return f"run PID {entry.get('pid')} shares it"
    pattern = re.compile(re.escape(str(folder)) + r"(?:/|\s|$)")
    for pid, process in table.items():
        if pid not in own and pattern.search(process["args"]): return f"PID {pid} names it"
    return None


def tool(command):
    """None when a registration tool succeeded, otherwise what went wrong."""
    try:
        done = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=TOOL_SECONDS)
    except (OSError, subprocess.SubprocessError) as error:
        return f"{Path(command[0]).name} did not finish: {error}"
    if done.returncode: return f"{Path(command[0]).name} exited {done.returncode}: {(done.stderr or '').strip()}"
    return None


def unregister(folder):
    """(apps unregistered, extensions removed, problems) for one folder."""
    apps, extensions = built(folder)
    problems = [problem for problem in (*(tool([PLUGINKIT, "-r", str(path)]) for path in extensions),
                                        *(tool([LSREGISTER, "-u", str(path)]) for path in apps)) if problem]
    return len(apps), len(extensions), problems


def settle(path, entry, say):
    """Unregister what a journal's run built, and remove the journal, unless a folder must wait or a tool
    failed: the journal then stays for the next run's sweep. It never raises."""
    try:
        listed = offload_host.listing(PROCESSES)
        if listed is None:
            say("unregistered nothing: the processes could not be listed; the next run tries again")
            return False
        table, others = offload_host.process_table(listed), read_journals(skip=path)
        own = set(offload_host.lineage(table, os.getpid()))
        waiting, apps, extensions = [], 0, 0
        for folder in dict.fromkeys(Path(item) for item in entry["folders"]):
            why = hold(folder, table, others, own)
            if why:
                waiting.append(f"{folder}: {why}")
                continue
            count, more, problems = unregister(folder)
            apps, extensions = apps + count, extensions + more
            waiting += [f"{folder}: {problem}" for problem in problems]
        if apps or extensions:
            say(f"unregistered {apps} app(s) and {extensions} extension(s) that run PID {entry.get('pid')} built")
        if waiting:
            say("left registered for the next run to sweep: " + "; ".join(waiting))
            return False
        path.unlink(missing_ok=True)
        return True
    except Exception as error:
        say(f"unregistered no further build: {type(error).__name__}: {error}; the next run tries again")
        return False


def sweep(say):
    """Settle the journals whose runs have gone, which a SIGKILL or a put-off cleanup left."""
    journals = read_journals()
    if not journals: return
    listed = offload_host.listing(PROCESSES)
    if listed is None: return
    table = offload_host.process_table(listed)
    for path, entry in journals:
        if entry is not None and not live(entry, table): settle(path, entry, say)


class Watch:
    """One outermost run's journal and its sampling of its own descendants."""

    def __init__(self, commands, cwd, say):
        self.cwd, self.say, self.folders, self.warned, self.noted = Path(cwd), say, [], set(), set()
        self.path = journal_directory() / f"{os.getpid()}-{uuid.uuid4().hex}.json"
        self.done, self.began = threading.Event(), time.time()
        self.start = process_start(os.getpid())
        self.thread = threading.Thread(target=self.watch, daemon=True)
        # Before the command starts, from its argv, which keeps a path's spaces that ps does not quote.
        for command in commands:
            if is_xcodebuild(" ".join(command[:2])):
                self.note([after for word, after in zip(command, command[1:]) if word == "-derivedDataPath"], self.cwd)

    def note(self, words, cwd):
        """Journal these folders an xcodebuild names, resolved against its directory."""
        for word in words:
            folder = (Path(cwd) / word).resolve()
            why = refusal(folder, self.cwd)
            if why:
                if folder not in self.warned:
                    self.warned.add(folder)
                    self.say(f"will not unregister what lies under {folder}: {why}")
            elif str(folder) not in self.folders:
                self.folders.append(str(folder))
                self.write()

    def write(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".writing")
        temporary.write_text(json.dumps({"pid": os.getpid(), "start": self.start, "began": self.began,
                                         "folders": self.folders}))
        os.replace(temporary, self.path)

    def sample(self):
        listed = offload_host.listing(PROCESSES, timeout=10)
        if listed is None: return
        table = offload_host.process_table(listed)
        children = {}
        for pid, process in table.items(): children.setdefault(process["parent"], []).append(pid)
        pending, seen = list(children.get(os.getpid(), ())), set()
        while pending:
            pid = pending.pop()
            if pid in seen: continue
            seen.add(pid)
            pending += children.get(pid, ())
            args = table[pid]["args"]
            if not is_xcodebuild(args): continue
            if named_folders(args):
                # Once for each build, and its directory only for a relative folder: lsof can take seconds.
                if (pid, args) in self.noted: continue
                words = named_folders(args)
                directory = self.cwd if all(word.startswith("/") for word in words) else self.directory(pid)
                if directory is None: continue
                self.noted.add((pid, args))
                self.note(words, directory)
            elif builds_unnamed(args) and "unnamed" not in self.warned:
                self.warned.add("unnamed")
                self.say("an xcodebuild names no -derivedDataPath, so what it builds stays registered; give it a "
                         "folder of its own (guides/shared-host.md, Registered Mac builds)")

    def directory(self, pid):
        """A process's working directory, against which its relative folder resolves, or None when it
        cannot be read: the next sample tries again, rather than guess at a folder."""
        listed = offload_host.listing(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fpn"], 10)
        return offload_host.working_directories(listed).get(pid)

    def watch(self):
        said = False
        while not self.done.is_set():
            try: self.sample()
            except Exception as error:
                if not said: self.say(f"cannot follow this run's builds: {type(error).__name__}: {error}")
                said = True
            self.done.wait(SAMPLE_SECONDS)

    def finish(self):
        self.done.set()
        self.thread.join(15)
        if self.path.exists():
            settle(self.path, json.loads(self.path.read_text()), self.say)


def nested():
    """Whether a run outside this one will do the work: its PID is this process's ancestor."""
    owner = os.environ.get(RUN)
    if not owner or not owner.isdigit(): return False
    listed = offload_host.listing(PROCESSES, timeout=10)
    return listed is not None and int(owner) in offload_host.lineage(offload_host.process_table(listed), os.getpid())


@contextlib.contextmanager
def around_run(commands, cwd):
    """Around a run's commands, each an argv: sweep what earlier runs left, follow what this run builds, and
    unregister it when the block ends, however it ends. A problem here is said and never changes the run's result."""
    say = lambda text: print(f"eng: {text}", file=sys.stderr, flush=True)
    watch = None
    try:
        if not nested():
            sweep(say)
            watch = Watch(commands, cwd, say)
    except Exception as error:
        say(f"cannot follow this run's builds: {type(error).__name__}: {error}")
    if watch is None:
        yield
        return
    before = os.environ.get(RUN)
    os.environ[RUN] = str(os.getpid())
    watch.thread.start()
    try:
        yield
    finally:
        if before is None: os.environ.pop(RUN, None)
        else: os.environ[RUN] = before
        try: watch.finish()
        except Exception as error:
            say(f"unregistered no further build: {type(error).__name__}: {error}; the next run tries again")
