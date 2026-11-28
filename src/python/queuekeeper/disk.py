"""Inventory of the disk space Queuekeeper's own files take on a host, and removal only of what nobody owns.

`qk disk` measures what Queuekeeper leaves: each repository's lane results kept by commit, the merge queue's
checkouts, and what a plugin
adds (the Apple plugin: simulators and runtimes, Xcode's DerivedData, the SwiftPM cache and kept app builds).
What sessions and the owner's workflow leave (worktrees, scratch folders, failure records, evidence archives) is
the toolkit's `eng disk` (decision E of vocaro/vocaro-engineering#403). It classifies each item:

- candidate: the rules prove that no session's live work is in it. `--delete` removes it, checking
  it again first, without asking and on a terminal or not: any session runs it when the volume
  runs short (the owner's decision, 2026-11-05, widening that of 2026-11-03, which had asked him
  for every category but simulators and runtimes). `--yes WHO` only names, in the ledger, who
  asked for the run.
- owner: nothing shows a session using it, but only the owner can say whether it is still
  wanted. The command that would remove it is printed; `--delete` never runs it.
- kept: it is in use, owned, of unclear ownership, or holds work or evidence.

`--dry-run` lists what `--delete` would remove. Without --delete nothing is changed.
Progress goes to stderr (Progress), so stdout's report and --json are unchanged, and stdout is line-buffered,
so a run whose output goes to a file or a pipe shows each line as it happens (vocaro/queuekeeper#212).
"""
import argparse
from dataclasses import asdict, dataclass, field
import datetime
import json
import os
from pathlib import Path
import pwd
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time

from .config import TOOLKIT_ROOT, qk_said

CANDIDATE, OWNER, KEPT = "candidate", "owner", "kept"
STATUS_ORDER = {CANDIDATE: 0, OWNER: 1, KEPT: 2}
# The categories, in the order a report lists them and --delete removes them. A plugin adds its own (extend).
CATEGORIES = {"kept-results": "Lane results kept by commit beside the receipts (pruned by their rule)",
              "pinned-checkouts": "Checkouts runs are pinned to, of the folders projects' plugins load from",
              "pull-request-checkouts": "Checkouts qk pr check keeps per open pull request (removed by qk pr check)",
              "queue-workers": "The merge queue's checkouts, beside the main checkouts in queuekeeper-workers/"}
# --delete removes every candidate category without asking (the owner's decision, 2026-11-05, widening that
# of 2026-11-03 from simulators and runtimes to every candidate): a candidate is what the rules prove nobody
# owns, so it needs no second judgment; what only the owner can judge is an owner item, never removed.
# The categories whose every item a report lists by its path, however small.
ALWAYS_LISTED = {"pull-request-checkouts", "queue-workers"}
SEPARATORS = re.compile(r"[\s=,;\"']+")
DATA_VOLUME = "/System/Volumes/Data"
# The lock reason the merge queue gives a checkout of its old layout that it could not move while a process
# used it, once it has made the checkout afresh in its place (merge_queue.Worker.place).
RETIRED = "retired by the merge queue"
# How long a checkout of a project's merge queue goes unchanged before `qk disk` lists it as the owner's to judge:
# its project is still there, so only the owner can say whether the room is worth a cold rebuild (queue_workers).
# StudyWright's newer-half checkout held 2.5 GiB unchanged for 12 days on 2026-11-27 (vocaro/queuekeeper#407).
WORKER_IDLE = 7 * 86400
# What git ls-remote prints when the repository a remote names no longer exists, as against a dropped connection
# or a refused key, which prove nothing (remote_gone).
GONE_REMOTE = re.compile(r"repository not found|repository '[^']*' not found|could not be found|does not exist", re.I)


@dataclass
class Item:
    category: str
    path: str
    status: str
    reason: str
    size: int = 0
    command: list = None
    idle: float = None
    location: str = None  # the directory on disk, when the path is a device's name
    # The working tree a folder a tool generates lies in, such as a Unity project's Library: --delete removes it
    # there only while git ignores it and tracks nothing in it (generated_in_worktree).
    worktree: str = None


@dataclass(frozen=True)
class Process:
    pid: int
    command: str = ""
    cwd: str = ""
    paths: tuple = ()
    args: str = ""


# The free space, in GiB, below which work that fills the disk does not start (START_FREE) and a command running in
# an admission is stopped as unmeasured (RUN_FREE): each its environment variable and its default. On 2026-11-14 an
# EPUBLib gate ran out of space part way through and then hung for about 90 minutes, holding the queue and the whole
# host, with nothing checking free space before it started (vocaro/queuekeeper#162). Builds and result bundles take
# tens of GiB, so a whole-host, exclusive or building lane waits for START_FREE before admission; RUN_FREE is what a
# command needs to go on writing at all. START_FREE was 30 GiB for every project, sized for Xcode builds and result
# bundles, and kept a Unity lane that writes little waiting over an hour with 11 to 26 GiB free (vocaro/queuekeeper#288):
# its default is now 10 GiB, five times RUN_FREE, which RUN_FREE still guards mid-run, and a project or lane that
# writes more says so with "disk": {"startFreeGB": N} in queuekeeper.json (config.start_free), which the variable
# overrides when it is set.
START_FREE = ("QUEUEKEEPER_START_FREE_GB", 10.0)
RUN_FREE = ("QUEUEKEEPER_RUN_FREE_GB", 2.0)
GIB = 1024 ** 3
# The free space, in GiB, below which `qk disk` loosens the idle limits of its rules (LOW_FREE), and the free space
# the loosened rules then aim for (GOAL_FREE): each its environment variable and its default. The standard limits
# keep a week of a pool's kinds and two weeks of an unused runtime, sized for a disk with room; on 2026-11-26 the
# first Mac had 46 GiB free and 157 GiB of simulators, nearly all kept by those limits, so `qk disk` offered 5 GiB
# (owner's decision, 2026-11-26: when space is at a premium, say under 50 GiB, delete more). Under LOW_FREE the
# limits shorten only as far as freeing GOAL_FREE needs, the least recently used items first (loosen); aiming above
# the trigger keeps one loosened run from leaving the volume just under it again. No rule that keeps an item for
# a use, a process, a reservation or an owner's decision loosens, and no limit falls below its floor (IDLE_FLOOR,
# or the floor a rule names).
LOW_FREE = ("QUEUEKEEPER_DISK_LOW_GB", 50.0)
GOAL_FREE = ("QUEUEKEEPER_DISK_GOAL_GB", 100.0)
IDLE_FLOOR = 3600.0


def volume_usage(path=DATA_VOLUME):
    """(total, used, free) bytes of the volume that holds path, or None when it cannot be read."""
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return None
    return usage.total, usage.used, usage.free


def free_floor(setting, configured=None):
    """A free-space floor in force, (GiB, its variable, where it comes from): its environment variable when set, else
    configured, a (GiB, where) the project's settings give (config.start_free), else its default. A variable that is
    no number at or above 0 raises ValueError naming it."""
    variable, default = setting
    raw = os.environ.get(variable)
    if raw is None and configured is not None: return float(configured[0]), variable, configured[1]
    if raw is None: return default, variable, "the default"
    try:
        value = float(raw)
    except ValueError:
        value = -1.0
    if not value >= 0:
        raise ValueError(f"{variable}={raw} is not a number of GiB at or above 0")
    return value, variable, "set in the environment"


def floor_words(floor, variable, whence):
    """A floor free_floor gives, in words that say where it comes from: its variable's, or a setting's."""
    if whence in ("the default", "set in the environment"): return f"{variable}={floor:g} GiB ({whence})"
    return f"{floor:g} GiB ({whence}; {variable} overrides it)"


def short_of_space(setting, paths=(DATA_VOLUME,), configured=None, root=None):
    """Why the volumes that hold paths have less free space than setting's floor (free_floor, with configured), in
    words that name the volume, what it has free, the floor and where it comes from, or None when each has enough. A
    volume whose usage cannot be read is never short: a missing reading stops no work. The commands it names run
    Queuekeeper as the project at root runs it (config.qk_words)."""
    floor, variable, whence = free_floor(setting, configured)
    seen = set()
    for path in paths:
        try:
            device = os.stat(path).st_dev
        except OSError:
            continue
        if device in seen: continue
        seen.add(device)
        found = volume_usage(path)
        if found is None or found[2] >= floor * GIB: continue
        return (f"the volume of {path} has {human(found[2])} free, below {floor_words(floor, variable, whence)}; "
                f"`{qk_said('disk --delete', root)}` removes what nobody owns, and `{qk_said('disk', root)}` lists "
                "what takes the space")
    return None


def account_home():
    """The account's home from the password database, so a private HOME joins the same view."""
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def canonical(path):
    """Compare /tmp and /var paths with the /private paths lsof reports."""
    path = os.path.normpath(str(path))
    for alias in ("/tmp", "/var", "/etc"):
        if path == alias or path.startswith(alias + "/"): return "/private" + path
    return path


def within(path, root):
    path, root = canonical(path), canonical(root).rstrip("/")
    return path == root or path.startswith(root + "/")


def human(size):
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB": return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024


def age(seconds):
    if seconds is None: return "unknown"
    if seconds < 2 * 3600: return f"{seconds / 60:.0f} min"
    if seconds < 2 * 86400: return f"{seconds / 3600:.0f} h"
    return f"{seconds / 86400:.1f} days"


def census(run=subprocess.run):
    """Every process this account can see, with its working directory, open paths and arguments."""
    listing = run(["lsof", "-nP", "-w", "-F", "pcfn"], capture_output=True, text=True, timeout=600).stdout
    found, entry, descriptor = {}, None, None
    for line in listing.splitlines():
        tag, value = line[:1], line[1:]
        if tag == "p": entry = found.setdefault(int(value), {"command": "", "cwd": "", "paths": set()})
        elif entry is None: continue
        elif tag == "c": entry["command"] = value
        elif tag == "f": descriptor = value
        elif tag == "n" and value.startswith("/"):
            if descriptor == "cwd": entry["cwd"] = value
            else: entry["paths"].add(value)
    arguments = {}
    for line in run(["ps", "-Aww", "-o", "pid=,args="], capture_output=True, text=True, timeout=120).stdout.splitlines():
        pid, _, args = line.strip().partition(" ")
        if pid.isdigit(): arguments[int(pid)] = args.strip()
    # Without a census nothing can be shown unused, so fail rather than report candidates.
    if not found or not arguments: raise RuntimeError("could not list processes with lsof and ps")
    own = os.getpid()
    return [Process(pid, entry["command"], entry["cwd"], tuple(sorted(entry["paths"])), arguments.get(pid, ""))
            for pid, entry in found.items() if pid != own] + \
           [Process(pid, args=args) for pid, args in arguments.items() if pid not in found and pid != own]


class Usage:
    """Answers which live process uses a path, from one census of the host."""

    def __init__(self, processes):
        self.by_path = {}
        self.arguments = [(process, process.args.upper()) for process in processes]
        for process in processes:
            uses = [(process.cwd, "working directory")] + [(name, "open file") for name in process.paths]
            uses += [(token, "argument") for token in SEPARATORS.split(process.args) if token.startswith("/")]
            for value, how in uses:
                value = canonical(value) if value else ""
                # Register every ancestor, so a lookup finds any use at or below a path.
                while value not in ("", "/") and value not in self.by_path:
                    self.by_path[value] = (process, how)
                    value = os.path.dirname(value)

    def of(self, path):
        return self.by_path.get(canonical(os.path.realpath(path))) or self.by_path.get(canonical(path))

    def naming(self, text):
        text = text.upper()
        return next((process for process, arguments in self.arguments if text in arguments), None)


def users(processes, path, apart=()):
    """The processes whose working directory, an open file or an argument is at or below path, and not
    below any of apart: what a process started from a checkout still holds there."""
    def inside(value):
        return bool(value) and within(value, path) and not any(within(value, other) for other in apart)
    return [process for process in processes
            if any(inside(value) for value in [process.cwd, *process.paths,
                                               *(token for token in SEPARATORS.split(process.args) if token.startswith("/"))])]


def runners(processes, root, apart=(), placed=lambda process: True):
    """(process, how, path) for each process that may run code from root's own tree: its working directory, an open
    file or an argument is below root, not at root itself and not below any of apart, where an argument relative to
    its working directory counts when it names a file there, or a folder by a path with a slash. A process whose only tie is root itself, a
    session's project root passed as an argument or a helper it started there, runs nothing from the tree; a run
    from root, such as `python3 scripts/qk`, names a file below it (vocaro-engineering#182). A working directory
    counts only for a process placed says may load code from it; its relative arguments still resolve there."""
    top = canonical(root)
    def below(value):
        return bool(value) and canonical(value) != top and within(value, root) \
            and not any(within(value, other) for other in apart)
    found = []
    for process in processes:
        uses = [(process.cwd, "working directory")] if placed(process) else []
        uses += [(name, "open file") for name in process.paths]
        for token in SEPARATORS.split(process.args):
            if token.startswith("/"): uses.append((token, "argument"))
            elif token and not token.startswith("-") and process.cwd and within(process.cwd, root):
                # A bare word that happens to name a folder there, as a session's options name `skills`, is not a path.
                named = os.path.join(process.cwd, token)
                if os.path.isfile(named) or ("/" in token and os.path.exists(named)): uses.append((named, "argument"))
        hit = next(((how, value) for value, how in uses if below(value)), None)
        if hit: found.append((process, *hit))
    return found


def described(use):
    process, how = use
    return f"PID {process.pid} ({process.command or process.args.split(' ')[0] or '?'}) has its {how} inside"


class Progress:
    """Progress lines on stderr while `qk disk` works, at most one each interval apart from the forced ones, which
    mark the start of a pass or a removal. On 2026-11-17 `eng disk --delete`, the toolkit's copy of this inventory,
    ran for over half an hour with three lines in its log, and only its ledger showed how far it had got
    (vocaro/queuekeeper#212)."""
    INTERVAL = 10.0

    def __init__(self, write=None, interval=INTERVAL, clock=time.monotonic):
        self.write = write or (lambda line: print(line, file=sys.stderr, flush=True))
        self.interval, self.clock, self.last = interval, clock, None

    def __call__(self, line, force=False):
        now = self.clock()
        if not force and self.last is not None and now - self.last < self.interval: return
        self.last = now
        self.write(line)


def quiet(line, force=False):
    """No progress, for an inventory nobody watches."""


class Sizes:
    """Allocated sizes from du, primed by one pass over each large root. progress hears of each pass (begin), each
    du priming pass and, throttled, each item checked."""

    def __init__(self, run=subprocess.run, measure=True, progress=quiet):
        self.run, self.measure, self.known, self.unreadable = run, measure, {}, []
        self.progress, self.stage, self.checked = progress, "", 0

    def begin(self, stage):
        """Start a pass of the inventory, such as a category's, whose items checked are then counted."""
        self.stage, self.checked = stage, 0
        self.progress(f"Inventorying {stage}.", force=True)

    def prime(self, root, depth, place=""):
        if not self.measure or not os.path.isdir(root): return
        self.progress(f"Measuring {root} in one du pass{place}.", force=True)
        result = self.run(["du", "-k", "-x", "-d", str(depth), str(root)], capture_output=True, text=True)
        self._read(result)

    def _read(self, result):
        for line in result.stdout.splitlines():
            kilobytes, _, path = line.partition("\t")
            if kilobytes.isdigit(): self.known[canonical(path)] = int(kilobytes) * 1024
        # An iCloud placeholder answers "Resource deadlock avoided" and takes no local space.
        self.unreadable += [line[4:].rsplit(": ", 1)[0] for line in result.stderr.splitlines()
                            if line.startswith("du: ") and not line.endswith("Resource deadlock avoided")]

    def of(self, path):
        self.checked += 1
        self.progress(f"Inventorying {self.stage or 'the items'}: {self.checked} items checked, now {path}.")
        if not self.measure: return 0
        key = canonical(path)
        if key not in self.known:
            self._read(self.run(["du", "-skx", str(path)], capture_output=True, text=True))
            self.known.setdefault(key, 0)
        return self.known[key]


@dataclass
class Host:
    """Everything the inventory reads from the machine, so tests can supply it. A plugin's items read more,
    from a subclass it names in extend."""
    home: Path
    development: Path
    now: float
    min_idle: float
    processes: list
    temp_roots: list = field(default_factory=list)
    offload_host: str = None  # the first of the farm's Macs, when this Mac sends work to one
    offload_hosts: tuple = ()  # the farm's Macs that take work now, by their SSH destinations, in order
    run: object = subprocess.run
    # The idle limit, in seconds, the loosened rules use when the volume is short of space (loosen), or None for
    # the standard limits.
    cutoff: float = None

    def window(self, standard, floor=IDLE_FLOOR):
        return window(self, standard, floor)

    @classmethod
    def current(cls, min_idle, development=None, run=subprocess.run):
        home = account_home()
        temp = [(Path("/private/tmp"), 6)]
        user_temp = run(["getconf", "DARWIN_USER_TEMP_DIR"], capture_output=True, text=True).stdout.strip()
        if user_temp: temp.append((Path(os.path.realpath(user_temp)), 2))
        return cls(home=home, development=Path(development or home / "Development"), now=time.time(),
                   min_idle=min_idle, processes=census(run), temp_roots=temp, offload_host=farm_mac(), offload_hosts=farm_macs(),
                   run=run)


def window(host, standard, floor=IDLE_FLOOR):
    """The idle limit, in seconds, of a rule whose standard limit is standard: that, or under pressure the host's
    loosened cutoff, never above standard and never below floor (or standard, when that is lower). A host with no
    cutoff, such as a test's stand-in, keeps the standard limits."""
    cutoff = getattr(host, "cutoff", None)
    if cutoff is None: return standard
    return max(min(floor, standard), min(standard, cutoff))


# What a plugin adds to the inventory (extend): the functions that list its items, those that name the
# folders one du pass measures, and the Host its items read.
PLUGIN_ITEMS, PLUGIN_PRIMES, PLUGIN_HOSTS = [], [], []
PLUGIN_CATEGORIES = {}  # each of PLUGIN_ITEMS, to the categories it lists, which its progress line names


def extend(categories, items, prime=None, host=None):
    """Add a plugin's disk items: `categories` maps each of its categories to its title; `items(host, usage,
    sizes, wanted)` lists the items of those in `wanted`; `prime(host)` gives the (folder, depth) pairs worth one
    du pass each; `host` is a subclass of Host that reads what its items need. Adding the same items twice
    changes nothing."""
    if items in PLUGIN_ITEMS: return
    CATEGORIES.update(categories)
    PLUGIN_ITEMS.append(items)
    PLUGIN_CATEGORIES[items] = list(categories)
    if prime: PLUGIN_PRIMES.append(prime)
    if host: PLUGIN_HOSTS.append(host)


def host_class():
    """The Host the inventory reads: the last a plugin named, or this module's."""
    return PLUGIN_HOSTS[-1] if PLUGIN_HOSTS else Host


def git_environment():
    # A hook's exported GIT_DIR and its relatives would point every command at one repository.
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment["GIT_OPTIONAL_LOCKS"] = "0"  # status must not refresh another session's index
    return environment


def git(run, cwd, *arguments, timeout=300):
    return run(["git", "-C", str(cwd), *arguments], capture_output=True, text=True, timeout=timeout,
               env=git_environment())


def reflog_activity(path):
    """Read the event times, so a maintenance rewrite does not make an old reflog new."""
    try: info = path.lstat()
    except OSError: return 0
    if not stat.S_ISREG(info.st_mode): return info.st_mtime
    latest = 0
    try:
        with path.open("rb") as log:
            for line in log:
                header = line.rstrip(b"\n").split(b"\t", 1)[0]
                match = re.fullmatch(rb"[0-9a-f]{40,64} [0-9a-f]{40,64} .+> ([0-9]{1,12}) [+-][0-9]{4}",
                                     header)
                if not match: return info.st_mtime
                latest = max(latest, int(match[1]))
    except OSError:
        return info.st_mtime
    return latest or info.st_mtime


def last_activity(path):
    """The newest change to a directory, its top-level entries and, for a worktree, its Git state."""
    path = Path(path)
    paths = [path]
    times = []
    try: paths += list(path.iterdir())
    except OSError: pass
    try:
        text = (path / ".git").read_text()
        if text.startswith("gitdir:"):
            gitdir = Path(text.split(":", 1)[1].strip())
            gitdir = gitdir if gitdir.is_absolute() else path / gitdir
            paths += [gitdir / "HEAD", gitdir / "index"]
            times.append(reflog_activity(gitdir / "logs/HEAD"))
    except OSError: pass
    for candidate in paths:
        try: times.append(candidate.lstat().st_mtime)
        except OSError: pass
    return max(times) if times else 0.0


def main_checkouts(host):
    if not host.development.is_dir(): return []
    return sorted(path for path in host.development.iterdir() if (path / ".git").is_dir())


def cache_item(host, usage, sizes, category, path, reason):
    idle = host.now - last_activity(path)
    use = usage.of(path)
    if use: return Item(category, str(path), KEPT, "in use: " + described(use), sizes.of(path), None, idle)
    if idle < window(host, host.min_idle):
        return Item(category, str(path), KEPT, f"{reason}, but changed {age(idle)} ago", sizes.of(path), None, idle)
    return Item(category, str(path), CANDIDATE, f"{reason}; no process; idle {age(idle)}", sizes.of(path),
                ["rm", "-rf", "--", str(path)], idle)


def project_workspace(host, workspace):
    """Whether a workspace lies where the projects build: their checkouts or the temporary roots."""
    return any(within(workspace, root) for root in [host.development, *(root for root, _ in host.temp_roots)])


def kept_results(host, usage, sizes):
    """Each repository's lane results kept by commit beside its receipts, .git/queuekeeper/remote/COMMIT/
    LANE.results/ and earlier attempts' LANE.attempts/ (remote.keep), counted, with those past their rule
    (remote.KEPT_RESULTS_RULE) as one candidate, which `qk remote prune-results` removes, leaving every receipt
    (vocaro-engineering#218)."""
    from . import remote  # remote imports this module
    items = []
    for main in main_checkouts(host):
        base = main / ".git/queuekeeper/remote"
        found = remote.all_kept_results(base)
        if not found: continue
        counted = ", ".join(f"{lane} at {len(folders)} commit{'s' if len(folders) != 1 else ''}"
                            for lane, folders in sorted(found.items()))
        gone = remote.results_past_rule(base, host.now)
        idle = host.now - max(remote.kept_results_at(folder) for folders in found.values() for folder in folders)
        use = usage.of(base)
        if use:
            items.append(Item("kept-results", str(base), KEPT, f"{counted}; in use: {described(use)}", sizes.of(base),
                              None, idle))
        elif gone:
            items.append(Item("kept-results", str(base), CANDIDATE, f"{counted}; {len(gone)} past the rule, which "
                              f"{remote.KEPT_RESULTS_RULE}", sum(sizes.of(part) for folder in gone for part in remote.kept_folders(folder)),
                              ["python3", str(TOOLKIT_ROOT / "bin/qk"), "remote", "prune-results", str(main)], idle))
        else:
            items.append(Item("kept-results", str(base), KEPT, f"{counted}; none past the rule, which "
                              f"{remote.KEPT_RESULTS_RULE}", sum(sizes.of(part) for folders in found.values() for folder in folders
                                                       for part in remote.kept_folders(folder)),
                              None, idle))
    return items


def pinned_checkouts(host, usage, sizes):
    """This Mac's checkouts of the folders projects' plugins load from, one for each commit a run pinned
    (offload_host.pinned_checkout), counted, with those no run has used for offload_host.PIN_DAYS days and no process
    uses as one candidate, which `qk disk --prune-pins` removes. A commit's ready file left with no checkout beside
    it, once the checkout was removed some other way, is counted and offered too (vocaro/queuekeeper#360)."""
    from . import offload_host  # which imports nothing of the toolkit
    root = offload_host.pins_root()
    if not root.is_dir(): return []
    found = [path for path in sorted(root.glob("*/*")) if path.is_dir() and not path.is_symlink()]
    listed = offload_host.stale_pins(now=host.now)
    markers = [ready for checkout, ready, _ in listed if orphaned_pin(checkout)]
    if not found and not markers: return []
    stale = [checkout for checkout, _, _ in listed if not orphaned_pin(checkout) and not usage.of(checkout)]
    counted = f"{len(found)} checkout{'s' if len(found) != 1 else ''} of {len({path.parent for path in found})} folder(s)"
    if markers:
        counted += f", and {len(markers)} ready file{'s' if len(markers) != 1 else ''} with no checkout left"
    rule = f"one stays until no run has used it for {offload_host.PIN_DAYS} days"
    idle = host.now - max(path.stat().st_mtime for path in found + markers)
    if stale or markers:
        past = f"{len(stale) or 'none'} past the rule: {rule}"
        return [Item("pinned-checkouts", str(root), CANDIDATE, f"{counted}; {past}",
                     sum(sizes.of(path) for path in stale + markers),
                     ["python3", str(TOOLKIT_ROOT / "bin/qk"), "disk", "--prune-pins"], idle)]
    return [Item("pinned-checkouts", str(root), KEPT, f"{counted}; none past the rule: {rule}",
                 sum(sizes.of(path) for path in found), None, idle)]


def pull_request_checkouts(host, usage, sizes):
    """Each checkout qk pr check keeps for a pull request, .git/queuekeeper-pr-checks/NUMBER in a project's main
    checkout, with its build output (vocaro/queuekeeper#397). Each is kept: the next qk pr check removes one once GitHub
    says its pull request is merged or closed (pull_requests.dispose), which is no fact this inventory can read."""
    items = []
    for main in main_checkouts(host):
        folder = main / ".git/queuekeeper-pr-checks"
        if not folder.is_dir(): continue
        for path in sorted((path for path in folder.iterdir() if path.is_dir() and path.name.isdigit()),
                           key=lambda path: int(path.name)):
            use = usage.of(path)
            reason = (f"{main.name} pull request {path.name}'s checkout, kept so that its checks build warm; the next "
                      "qk pr check removes it once GitHub says the pull request is merged or closed"
                      + (f"; in use: {described(use)}" if use else ""))
            items.append(Item("pull-request-checkouts", str(path), KEPT, reason, sizes.of(path), None,
                              host.now - last_activity(path)))
    return items


def gitdir_of(checkout):
    """The Git directory a linked checkout's .git file names, or None when it has none."""
    try: text = (Path(checkout) / ".git").read_text()
    except OSError: return None
    if not text.startswith("gitdir:"): return None
    gitdir = Path(text.split(":", 1)[1].strip())
    return gitdir if gitdir.is_absolute() else Path(checkout) / gitdir


def common_of(gitdir):
    """The common Git directory of a linked checkout's Git directory, which its commondir file names."""
    try: named = Path((gitdir / "commondir").read_text().strip())
    except OSError: return Path(os.path.normpath(gitdir.parent.parent))
    return Path(os.path.normpath(named if named.is_absolute() else gitdir / named))


def waiting_entries(main):
    """The IDs of the entries in main's merge queue that still wait: queued, testing or passed."""
    from . import merge_queue  # which imports this module
    found = []
    for path in sorted((main / ".git/queuekeeper-queue/entries").glob("*.json")):
        try: state = json.loads(path.read_text()).get("state")
        except (OSError, ValueError, AttributeError): state = merge_queue.QUEUED  # unreadable: count it as waiting
        if state in merge_queue.WAITING: found.append(path.stem)
    return found


def remote_gone(run, main):
    """The words saying that main's origin names a repository that no longer exists, or None: when main has no origin,
    the remote answers, or the failure says nothing of the repository, such as a dropped connection or a refused
    key."""
    try:
        url = git(run, main, "remote", "get-url", "origin", timeout=30)
        if url.returncode != 0 or not url.stdout.strip(): return None
        listed = run(["git", "ls-remote", "--", url.stdout.strip(), "HEAD"], capture_output=True, text=True, timeout=60,
                     env={**git_environment(), "GIT_TERMINAL_PROMPT": "0"})
    except (OSError, subprocess.SubprocessError): return None
    if listed.returncode == 0 or not GONE_REMOTE.search(listed.stderr or ""): return None
    return f"its origin, {url.stdout.strip()}, says the repository is not found"


def queue_workers(host, usage, sizes):
    """The merge queue's checkouts of each project, DEVELOPMENT/queuekeeper-workers/NAME/ (merge_queue.Queue.home):
    the worker's checkout, the toolkit's copy and those beside them for a bisect's newer half, a stacked gate or a
    docs round. The queue locks each (merge_queue.CHECKOUT_REASON), so no sweep took them, and on 2026-11-27 the
    workers of four projects whose repositories were deleted stayed (vocaro/queuekeeper#407).

    A project's whole folder is a candidate when its repository is gone, no process uses the folder and it is idle:
    no checkout under DEVELOPMENT names the project and its own checkout's Git directory is gone, so no queue entry
    can wait in it, or its main checkout's origin says the repository is not found and no entry waits there. A
    project that is still there keeps its checkouts; each unchanged for WORKER_IDLE is the owner's item, with what
    removing it costs, since only the owner can weigh the room against the next cold build."""
    from . import merge_queue  # which imports this module
    root = host.development / merge_queue.QUEUES
    if not root.is_dir(): return []
    items = []
    for home in sorted(path for path in root.iterdir() if path.is_dir() and not path.is_symlink()):
        name, main = home.name, host.development / home.name
        children = sorted(path for path in home.iterdir() if path.is_dir() and not path.is_symlink())
        idle = host.now - max([last_activity(home), *map(last_activity, children)])
        use = usage.of(home)
        present = (main / ".git").is_dir()
        own = gitdir_of(home / name)
        gone = None
        if not present and not (own and own.exists()):
            gone = (f"no checkout under {host.development} names {name}, and its checkout's Git directory is gone, "
                    "so no queue entry can wait for it")
        elif present and not use and idle >= window(host, host.min_idle) and not waiting_entries(main):
            # Asked once a host, as loosen lists the inventory again at each limit it tries.
            remotes = host.__dict__.setdefault("gone_remotes", {})
            if main not in remotes: remotes[main] = remote_gone(host.run, main)
            gone = remotes[main]
            if gone: gone += ", and no entry waits in its queue"
        if gone:
            words = f"{name}'s merge queue checkouts, whose repository is gone: {gone}"
            if use:
                items.append(Item("queue-workers", str(home), KEPT, f"{words}; in use: {described(use)}",
                                  sizes.of(home), None, idle))
            elif idle < window(host, host.min_idle):
                items.append(Item("queue-workers", str(home), KEPT, f"{words}; but changed {age(idle)} ago",
                                  sizes.of(home), None, idle))
            else:
                items.append(Item("queue-workers", str(home), CANDIDATE, f"{words}; no process; idle {age(idle)}; "
                                  "`qk queue reclaim` in the toolkit then forgets its copy's registration",
                                  sizes.of(home), ["rm", "-rf", "--", str(home)], idle))
            continue
        for child in children:
            items.append(worker_checkout(host, usage, sizes, name, child))
    return items


def worker_role(name, child):
    """What the merge queue of project name uses its checkout child for, and so when it makes it again."""
    from . import merge_queue  # which imports this module
    roles = {name: "every gate", f"{name}{merge_queue.BESIDE_SUFFIX}": "a bisect's newer half or a stacked gate",
             f"{name}{merge_queue.DOCS_SUFFIX}": "a docs round beside a code gate",
             f"{name}{merge_queue.ISOLATION_STACK_SUFFIX}": "an isolation stack's probes"}
    return roles.get(child.name, "its rounds, as the copy of the toolkit they run")


def worker_checkout(host, usage, sizes, name, child):
    """One checkout of a project's merge queue that is still there: kept while it is used or changed within
    WORKER_IDLE, else the owner's item, with the command that removes it and its registration."""
    idle = host.now - last_activity(child)
    use = usage.of(child)
    words = f"{name}'s merge queue checkout for {worker_role(name, child)}"
    if use: return Item("queue-workers", str(child), KEPT, f"{words}; in use: {described(use)}", sizes.of(child), None, idle)
    if idle < WORKER_IDLE:
        return Item("queue-workers", str(child), KEPT, f"{words}; changed {age(idle)} ago", sizes.of(child), None, idle)
    gitdir = gitdir_of(child)
    command = None if gitdir is None else ["git", "--git-dir", str(common_of(gitdir)), "worktree", "remove", "--force",
                                           "--force", str(child)]
    size = sizes.of(child)
    return Item("queue-workers", str(child), OWNER, f"{words}; unchanged for {age(idle)}, no process; removing it frees "
                f"{human(size)} and costs {name}'s queue a fresh worktree and a cold build the next time it needs this "
                "checkout, which it makes again by itself", size, command, idle)


def orphaned_pin(checkout):
    """Whether offload_host.stale_pins listed checkout only for its ready file, its commit's folder being gone."""
    return not checkout.parent.exists() and not checkout.parent.is_symlink()


def prune_pins(out=print):
    """qk disk --prune-pins: remove the pinned checkouts past their rule that no process uses, and the ready files
    left with no checkout beside them."""
    from . import offload_host
    usage = Usage(host_class().current(0).processes)
    pruned = markers = 0
    for found in offload_host.stale_pins():
        orphan = orphaned_pin(found[0])
        if not orphan and usage.of(found[0]): continue
        if not offload_host.remove_pin(*found): continue
        if orphan:
            markers += 1
            out(f"pruned {found[1]}, which had no checkout beside it")
        else:
            pruned += 1
            out(f"pruned {found[0]}")
    out(f"{pruned} pinned checkout{'s' if pruned != 1 else ''} pruned, unused for {offload_host.PIN_DAYS} days, "
        f"in {offload_host.pins_root()}")
    if markers: out(f"{markers} ready file{'s' if markers != 1 else ''} with no checkout left pruned")
    return 0


def farm_macs():
    """The SSH destinations of the farm's Macs this Mac offloads to now, in order."""
    from . import farm
    try: return farm.destinations()
    except ValueError: return ()


def farm_mac():
    """Another Mac this Mac offloads to, or None: here alone, or on another Mac itself."""
    from . import remote  # remote imports this module
    try: return remote.configured()
    except ValueError: return None


def inventory(host, sizes, categories=None):
    usage = Usage(host.processes)
    wanted = set(categories or CATEGORIES)
    items = []
    if "kept-results" in wanted:
        sizes.begin("kept-results")
        items += kept_results(host, usage, sizes)
    if "pinned-checkouts" in wanted:
        sizes.begin("pinned-checkouts")
        items += pinned_checkouts(host, usage, sizes)
    if "pull-request-checkouts" in wanted:
        sizes.begin("pull-request-checkouts")
        items += pull_request_checkouts(host, usage, sizes)
    if "queue-workers" in wanted:
        sizes.begin("queue-workers")
        items += queue_workers(host, usage, sizes)
    for listed in PLUGIN_ITEMS:
        named = [category for category in PLUGIN_CATEGORIES.get(listed, ()) if category in wanted]
        if named: sizes.begin(", ".join(named))
        items += listed(host, usage, sizes, wanted)
    return items


# Loosening under pressure

@dataclass
class Pressure:
    """How short of space the volume is and how far loosen shortened the idle limits: free, low and goal in bytes;
    standard, what the standard rules' candidates take; needed, what more reaching goal needs; cutoff, the idle
    limit chosen (None when nothing was loosened); reached, what the candidates take at that limit."""
    free: int
    low: int
    goal: int
    standard: int = 0
    needed: int = 0
    cutoff: float = None
    reached: int = 0

    def words(self):
        """The pressure in words, for the report and each item it made a candidate."""
        short = f"{human(self.free)} free is under {human(self.low)}"
        if self.cutoff is None:
            return f"{short}, but the standard rules' candidates reach {human(self.goal)} free" if not self.needed \
                else f"{short}, but no rule's idle limit can shorten further"
        return (f"{short}, so idle limits shorten to {age(self.cutoff)}, the least recently used first, "
                f"to remove {human(self.needed)} more of allocated size toward {human(self.goal)} free; clones share "
                "blocks, so it can free less")


def candidate_size(items):
    return sum(item.size for item in items if item.status == CANDIDATE)


def loosen(host, sizes, items, free, low, goal, categories=None):
    """When free is under low, the inventory again with each idle limit shortened (Host.window) as little as
    freeing goal needs, and the Pressure; else items as they are. Each limit tried is the idle time of an item
    only a loosened rule makes a candidate, so each step adds the next least recently used; looser limits only
    add candidates, so a bisection finds the longest that is enough, or the loosest when none is."""
    pressure = Pressure(free, low, goal, candidate_size(items))
    pressure.needed = max(0, goal - free - pressure.standard)
    if free >= low or not pressure.needed: return items, pressure
    progress, sizes.progress = sizes.progress, quiet
    try:
        def at(cutoff):
            host.cutoff = cutoff
            return inventory(host, sizes, categories)
        known = {(item.category, item.path) for item in items if item.status == CANDIDATE}
        limits = sorted({item.idle for item in at(0.0) if item.status == CANDIDATE and item.idle
                         and (item.category, item.path) not in known}, reverse=True)
        if not limits:
            host.cutoff = None
            return items, pressure
        chosen, lowest, highest = len(limits) - 1, 0, len(limits) - 1
        while lowest <= highest:
            middle = (lowest + highest) // 2
            if candidate_size(at(limits[middle] - 1)) - pressure.standard >= pressure.needed:
                chosen, highest = middle, middle - 1
            else:
                lowest = middle + 1
        loosened = at(limits[chosen] - 1)
    finally:
        sizes.progress = progress
    pressure.cutoff, pressure.reached = host.cutoff, candidate_size(loosened)
    for item in loosened:
        if item.status == CANDIDATE and (item.category, item.path) not in known:
            item.reason = f"{item.reason}; loosened: {pressure.words()}"
    return loosened, pressure


def pressure_limits(args):
    """(low, goal) in bytes: the flags', else the variables', else the defaults; goal is never under low."""
    def setting(given, variable):
        if given is not None: return given
        try: return float(os.environ.get(variable[0], variable[1]))
        except ValueError: return variable[1]
    low = max(0.0, setting(args.low_free_gb, LOW_FREE))
    return int(low * GIB), int(max(low, setting(args.goal_free_gb, GOAL_FREE)) * GIB)


# Reporting

def report(items, volume, unreadable, min_size, out=print, pressure=None):
    if volume:
        total, used, free = volume
        out(f"Data volume {DATA_VOLUME}: {human(used)} used of {human(total)}, {human(free)} free.")
    if pressure and pressure.free < pressure.low:
        out(f"Short of space: {pressure.words()}.")
    out("This inventory changed nothing. Sizes are allocated sizes from du; APFS clones share blocks, "
        "so removing one can free less.\n")
    for category, title in CATEGORIES.items():
        group = sorted((item for item in items if item.category == category),
                       key=lambda item: (STATUS_ORDER[item.status], -item.size))
        if not group: continue
        out(title)
        hidden = {}
        for item in group:
            # A kept pull-request checkout is listed whatever its size, as the user guide says (vocaro/queuekeeper#400).
            if item.size < min_size and category not in ALWAYS_LISTED:
                count, size = hidden.get(item.status, (0, 0))
                hidden[item.status] = (count + 1, size + item.size)
                continue
            out(f"  {item.status:9} {human(item.size):>10}  {item.path}")
            out(f"            {item.reason}")
            if item.command: out(f"            $ {shlex.join(item.command)}")
        for status, (count, size) in hidden.items():
            out(f"  {status:9} {human(size):>10}  and {count} smaller items")
        out("")
    if unreadable:
        roots = {}
        for path in unreadable:
            parts = Path(path).parts
            roots[str(Path(*parts[:5]))] = roots.get(str(Path(*parts[:5])), 0) + 1
        top = sorted(roots.items(), key=lambda pair: -pair[1])[:8]
        out("Unreadable during measurement: " + "; ".join(f"{root} ({count})" for root, count in top))
    for status, label in ((CANDIDATE, "Candidates"), (OWNER, "Owner decisions"), (KEPT, "Kept")):
        chosen = [item for item in items if item.status == status]
        out(f"{label}: {len(chosen)} items, {human(sum(item.size for item in chosen))}.")


def plan(items, out=print):
    """What --delete would remove, category by category: every candidate and its command."""
    chosen = [item for item in items if item.status == CANDIDATE]
    out("Dry run: nothing was removed. Each item is checked again before removal.\n")
    for category, title in CATEGORIES.items():
        group = sorted((item for item in chosen if item.category == category), key=lambda item: -item.size)
        if not group: continue
        out(f"{title}: {len(group)} items, {human(sum(item.size for item in group))}")
        for item in group:
            out(f"  {human(item.size):>10}  {item.path}")
            out(f"            $ {shlex.join(item.command)}")
        out("")
    out(f"In all: {len(chosen)} items, {human(sum(item.size for item in chosen))}.")


def ledger_path(home):
    return Path(home) / "Library/Application Support/queuekeeper/disk-reclaimed.jsonl"


def outside_checkouts(path):
    """A removal target must be an absolute path, reached through no link, below no working tree."""
    path = Path(path)
    if not path.is_absolute() or canonical(os.path.realpath(path)) != canonical(path): return False
    return not any((ancestor / ".git").exists() for ancestor in [path, *path.parents])


def generated_in_worktree(path, worktree, run=subprocess.run):
    """Why --delete may not remove path, a folder a tool generates in the working tree worktree, or None when it may:
    an absolute path reached through no link, inside worktree and no repository nested in it, that git ignores and
    tracks nothing in. Such a folder holds no work a commit could keep, and the tool makes it again, so the rule
    that keeps --delete out of working trees, which guards their tracked and untracked work, does not keep it
    (vocaro/queuekeeper#291)."""
    path, worktree = Path(path), Path(worktree)
    if not path.is_absolute() or canonical(os.path.realpath(path)) != canonical(path): return "behind a link"
    if canonical(path) == canonical(worktree) or not within(path, worktree):
        return f"not inside the working tree {worktree}"
    nearest = next((ancestor for ancestor in [path, *path.parents] if (ancestor / ".git").exists()), None)
    if nearest is None or canonical(nearest) != canonical(worktree):
        return f"inside a working tree other than {worktree}"
    try:
        tracked = git(run, worktree, "ls-files", "-z", "--", str(path), timeout=120)
        ignored = git(run, worktree, "check-ignore", "-q", "--", str(path), timeout=120)
    except (OSError, subprocess.SubprocessError) as error:
        return f"git could not say whether it tracks or ignores it ({error})"
    if tracked.returncode != 0: return f"git could not list what it tracks in it: {tracked.stderr.strip()[:200]}"
    if tracked.stdout: return "git tracks files in it"
    if ignored.returncode != 0: return "git does not ignore it, so it may hold work"
    return None


def removal_refused(item, run=subprocess.run):
    """Why --delete may not run item's rm, or None: its target lies in no working tree and behind no link, or is a
    folder a tool generates in item.worktree that git ignores and tracks nothing in (generated_in_worktree)."""
    if item.command[0] != "rm" or outside_checkouts(item.command[-1]): return None
    if item.worktree and item.command[-1] == item.path:
        refused = generated_in_worktree(item.path, item.worktree, run)
        return refused and f"a generated folder in a working tree, but {refused}"
    return "inside a repository's working tree or behind a link"


def record_removal(ledger, entry, out):
    out(f"{entry['outcome']}: {entry['path']}")
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with open(ledger, "a") as handle: handle.write(json.dumps(entry) + "\n")


def removal_entry(item, answered):
    entry = {"time": datetime.datetime.now(datetime.timezone.utc).isoformat(), "category": item.category,
             "path": item.path, "size": item.size, "reason": item.reason, "command": item.command}
    if answered: entry["approval"] = answered
    return entry


def free_space(path=DATA_VOLUME):
    """The free bytes of the volume that holds path, or None when it cannot be read."""
    usage = volume_usage(path)
    return usage[2] if usage else None


def delete(items, fresh, ledger, out=print, run=subprocess.run, approval=None, progress=quiet, free=free_space):
    """Remove the candidates category by category, each checked again first, without asking: a
    candidate is what the rules prove nobody owns (the owner's decision, 2026-11-05). approval, the
    text given to --yes ("" when given bare), names in each ledger entry who asked for the run.
    progress hears of each category's check and of each removal before it starts.

    The space freed is measured, not summed: free() reads the volume's free bytes before and after each
    removal, which its ledger entry records, and around the whole run, which the last lines report beside the
    allocated size removed. On 2026-11-27 a run removed 46.8 GiB of allocated size and the volume's usage fell
    about 6 GiB: the simulators it removed were APFS clones of their templates, sharing most of their blocks,
    and a gate wrote meanwhile (vocaro/queuekeeper#377)."""
    removed, allocated = 0, 0
    answered = None if approval is None else "--yes" + (f": {approval}" if approval else "")
    started = free()
    for category, title in CATEGORIES.items():
        group = [item for item in items if item.category == category and item.status == CANDIDATE]
        if not group: continue
        out(f"Removing {len(group)} {title.lower()} candidates, {human(sum(item.size for item in group))}"
            + (f", on the approval {answered}." if answered else "."))
        progress(f"Checking the {category} candidates again.", force=True)
        current = {item.path: item for item in fresh(category)}
        for place, item in enumerate(group, 1):
            progress(f"Removing {place} of {len(group)} {category} candidates: {item.path}", force=True)
            now = current.get(item.path)
            entry = removal_entry(item, answered)
            if item.location and not os.path.lexists(item.location):
                # Removed with an earlier item, as a pool's template goes with its kind's last member.
                entry["outcome"] = "skipped: already removed with an earlier item"
            elif now is None or now.status != CANDIDATE or now.command != item.command or now.worktree != item.worktree:
                entry["outcome"] = "skipped: " + (now.reason if now else "no longer present")
            elif refused := removal_refused(item, run):
                entry["outcome"] = "skipped: " + refused
            else:
                before = free()
                result = run(item.command, capture_output=True, text=True, timeout=3600, env=git_environment())
                after = free()
                entry["outcome"] = "removed" if result.returncode == 0 else f"failed ({result.returncode}): {result.stderr.strip()[:300]}"
                entry["freeBefore"], entry["freeAfter"] = before, after
                entry["freed"] = None if before is None or after is None else after - before
                removed += result.returncode == 0
                allocated += item.size if result.returncode == 0 else 0
            record_removal(ledger, entry, out)
    out(f"Removed {removed} items; the record of every decision is in {ledger}.")
    ended = free()
    if started is not None and ended is not None:
        out(f"Freed {human(max(0, ended - started))} of free space ({human(started)} to {human(ended)} free); the "
            f"removed items' allocated size was {human(allocated)}. APFS clones share blocks, and other work writes "
            "meanwhile, so the two differ.")
    else:
        out(f"The volume's free space could not be read, so only the removed items' allocated size is known: "
            f"{human(allocated)}.")
    return removed


def add_arguments(parser):
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--dry-run", action="store_true", help="list only what --delete would remove, and remove nothing")
    action.add_argument("--delete", action="store_true", help="remove every candidate, checking each again first, "
                        "without asking; owner items are only listed, with their commands")
    action.add_argument("--prune-pins", action="store_true", help="remove the checkouts runs were pinned to that no "
                        "run has used for a week and no process uses, and nothing else")
    parser.add_argument("--yes", nargs="?", const="", metavar="WHO",
                        help="with --delete, name in each ledger entry who asked for this run (such as 'owner, "
                        "2026-11-05 chat'); --delete asks nobody since 2026-11-05, so this only records the ask")
    parser.add_argument("--json", action="store_true", help="print the items as JSON")
    parser.add_argument("--min-idle-hours", type=float,
                        help="count an item idle only after this many unchanged hours (default 24)")
    parser.add_argument("--min-size-mb", type=float, default=100.0, help="list smaller items only as totals")
    parser.add_argument("--low-free-gb", type=float, help="below this much free space, shorten the rules' idle "
                        f"limits to delete more, least recently used first (default ${LOW_FREE[0]} or "
                        f"{LOW_FREE[1]:g}; 0 keeps the standard limits)")
    parser.add_argument("--goal-free-gb", type=float, help="the free space the shortened limits aim for "
                        f"(default ${GOAL_FREE[0]} or {GOAL_FREE[1]:g})")
    parser.add_argument("--development", type=Path, help="directory holding the projects (default ~/Development)")


def prime_items(host, sizes):
    """One du pass over each folder that holds many small items; everything else is measured alone."""
    roots = [(root, depth) for prime in PLUGIN_PRIMES for root, depth in prime(host) if root]
    for place, (root, depth) in enumerate(roots, 1):
        sizes.prime(root, depth, f", {place} of {len(roots)}")


def execute(args, run=subprocess.run, out=print):
    if args.yes is not None and not args.delete:
        print("queuekeeper disk: --yes names who asked for --delete's removals, so it needs --delete", file=sys.stderr)
        return 2
    # A run whose output goes to a file or a pipe shows each line as it is printed, not when a block fills.
    if callable(getattr(sys.stdout, "reconfigure", None)): sys.stdout.reconfigure(line_buffering=True)
    if args.prune_pins: return prune_pins(out)
    progress = Progress()
    def collect(categories=None, measure=True, cutoff=None):
        idle_hours = 24.0 if args.min_idle_hours is None else args.min_idle_hours
        if measure: progress("Listing the account's processes with lsof and ps.", force=True)
        host = host_class().current(idle_hours * 3600, args.development, run)
        host.cutoff = cutoff
        sizes = Sizes(run, measure, progress if measure else quiet)
        if measure: prime_items(host, sizes)
        return host, sizes, inventory(host, sizes, categories)
    host, sizes, items = collect()
    volume = volume_usage(DATA_VOLUME)
    pressure = None
    if volume:
        pressure = Pressure(volume[2], *pressure_limits(args))
        if pressure.free < pressure.low: progress(f"Short of space: {human(pressure.free)} free is under "
                                                  f"{human(pressure.low)}; finding how far to shorten idle limits.",
                                                  force=True)
        items, pressure = loosen(host, sizes, items, pressure.free, pressure.low, pressure.goal)
    if args.dry_run: items = [item for item in items if item.status == CANDIDATE]
    if args.json:
        out(json.dumps({"volume": volume, "pressure": pressure and asdict(pressure),
                        "items": [asdict(item) for item in items]}, indent=2))
    elif args.dry_run:
        if pressure and pressure.free < pressure.low: out(f"Short of space: {pressure.words()}.")
        plan(items, out)
    else:
        report(items, volume, sizes.unreadable, args.min_size_mb * 1024 * 1024, out, pressure)
    if args.delete:
        ledger = ledger_path(host.home)
        cutoff = host.cutoff
        delete(items, lambda category: collect([category], measure=False, cutoff=cutoff)[2], ledger, out=out, run=run,
               approval=args.yes, progress=progress)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="qk disk", description=__doc__.split("\n")[0])
    add_arguments(parser)
    try:
        return execute(parser.parse_args(argv))
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"queuekeeper disk: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
