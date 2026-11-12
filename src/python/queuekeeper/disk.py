"""Inventory of the disk space Queuekeeper's own files take on a host, and removal only of what nobody owns.

`qk disk` measures what Queuekeeper leaves: each repository's lane results kept by commit, and what an extension
adds (the Apple extension: simulators and runtimes, Xcode's DerivedData, the SwiftPM cache and kept app builds).
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
guides/shared-host.md § Reclaiming disk space has the rules.
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

from .config import TOOLKIT_ROOT

CANDIDATE, OWNER, KEPT = "candidate", "owner", "kept"
STATUS_ORDER = {CANDIDATE: 0, OWNER: 1, KEPT: 2}
# The categories, in the order a report lists them and --delete removes them. An extension adds its own (extend).
CATEGORIES = {"kept-results": "Lane results kept by commit beside the receipts (pruned by their rule)"}
# --delete removes every candidate category without asking (the owner's decision, 2026-11-05, widening that
# of 2026-11-03 from simulators and runtimes to every candidate): a candidate is what the rules prove nobody
# owns, so it needs no second judgment; what only the owner can judge is an owner item, never removed.
SEPARATORS = re.compile(r"[\s=,;\"']+")
DATA_VOLUME = "/System/Volumes/Data"
# The lock reason the merge queue gives a checkout of its old layout that it could not move while a process
# used it, once it has made the checkout afresh in its place (merge_queue.Worker.place).
RETIRED = "retired by the merge queue"


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


@dataclass(frozen=True)
class Process:
    pid: int
    command: str = ""
    cwd: str = ""
    paths: tuple = ()
    args: str = ""


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


class Sizes:
    """Allocated sizes from du, primed by one pass over each large root."""

    def __init__(self, run=subprocess.run, measure=True):
        self.run, self.measure, self.known, self.unreadable = run, measure, {}, []

    def prime(self, root, depth):
        if not self.measure or not os.path.isdir(root): return
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
        if not self.measure: return 0
        key = canonical(path)
        if key not in self.known:
            self._read(self.run(["du", "-skx", str(path)], capture_output=True, text=True))
            self.known.setdefault(key, 0)
        return self.known[key]


@dataclass
class Host:
    """Everything the inventory reads from the machine, so tests can supply it. An extension's items read more,
    from a subclass it names in extend."""
    home: Path
    development: Path
    now: float
    min_idle: float
    processes: list
    temp_roots: list = field(default_factory=list)
    offload_host: str = None  # the second Mac, when this Mac sends work to one
    run: object = subprocess.run

    @classmethod
    def current(cls, min_idle, development=None, run=subprocess.run):
        home = account_home()
        temp = [(Path("/private/tmp"), 6)]
        user_temp = run(["getconf", "DARWIN_USER_TEMP_DIR"], capture_output=True, text=True).stdout.strip()
        if user_temp: temp.append((Path(os.path.realpath(user_temp)), 2))
        return cls(home=home, development=Path(development or home / "Development"), now=time.time(),
                   min_idle=min_idle, processes=census(run), temp_roots=temp, offload_host=second_mac(), run=run)


# What an extension adds to the inventory (extend): the functions that list its items, those that name the
# folders one du pass measures, and the Host its items read.
EXTENSION_ITEMS, EXTENSION_PRIMES, EXTENSION_HOSTS = [], [], []


def extend(categories, items, prime=None, host=None):
    """Add an extension's disk items: `categories` maps each of its categories to its title; `items(host, usage,
    sizes, wanted)` lists the items of those in `wanted`; `prime(host)` gives the (folder, depth) pairs worth one
    du pass each; `host` is a subclass of Host that reads what its items need. Adding the same items twice
    changes nothing."""
    if items in EXTENSION_ITEMS: return
    CATEGORIES.update(categories)
    EXTENSION_ITEMS.append(items)
    if prime: EXTENSION_PRIMES.append(prime)
    if host: EXTENSION_HOSTS.append(host)


def host_class():
    """The Host the inventory reads: the last an extension named, or this module's."""
    return EXTENSION_HOSTS[-1] if EXTENSION_HOSTS else Host


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
    if idle < host.min_idle:
        return Item(category, str(path), KEPT, f"{reason}, but changed {age(idle)} ago", sizes.of(path), None, idle)
    return Item(category, str(path), CANDIDATE, f"{reason}; no process; idle {age(idle)}", sizes.of(path),
                ["rm", "-rf", "--", str(path)], idle)


def project_workspace(host, workspace):
    """Whether a workspace lies where the projects build: their checkouts or the temporary roots."""
    return any(within(workspace, root) for root in [host.development, *(root for root, _ in host.temp_roots)])


def kept_results(host, usage, sizes):
    """Each repository's lane results kept by commit beside its receipts, .git/vocaro-engineering/remote/COMMIT/
    LANE.results/ (remote.keep), counted, with those past their rule (remote.KEPT_RESULTS_RULE) as one candidate,
    which `eng remote prune-results` removes, leaving every receipt (vocaro-engineering#218)."""
    from . import remote  # remote imports this module
    items = []
    for main in main_checkouts(host):
        base = main / ".git/vocaro-engineering/remote"
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
                              f"{remote.KEPT_RESULTS_RULE}", sum(sizes.of(folder) for folder in gone),
                              ["python3", str(TOOLKIT_ROOT / "bin/qk"), "remote", "prune-results", str(main)], idle))
        else:
            items.append(Item("kept-results", str(base), KEPT, f"{counted}; none past the rule, which "
                              f"{remote.KEPT_RESULTS_RULE}", sum(sizes.of(folder) for folders in found.values() for folder in folders),
                              None, idle))
    return items


def second_mac():
    """The second Mac this Mac offloads to, or None: here alone, or on the second Mac itself."""
    from . import remote  # remote imports this module
    try: return remote.configured()
    except ValueError: return None


def inventory(host, sizes, categories=None):
    usage = Usage(host.processes)
    wanted = set(categories or CATEGORIES)
    items = []
    if "kept-results" in wanted: items += kept_results(host, usage, sizes)
    for listed in EXTENSION_ITEMS: items += listed(host, usage, sizes, wanted)
    return items


# Reporting

def report(items, volume, unreadable, min_size, out=print):
    if volume:
        total, used, free = volume
        out(f"Data volume {DATA_VOLUME}: {human(used)} used of {human(total)}, {human(free)} free.")
    out("This inventory changed nothing. Sizes are allocated sizes from du; APFS clones share blocks, "
        "so removing one can free less.\n")
    for category, title in CATEGORIES.items():
        group = sorted((item for item in items if item.category == category),
                       key=lambda item: (STATUS_ORDER[item.status], -item.size))
        if not group: continue
        out(title)
        hidden = {}
        for item in group:
            if item.size < min_size:
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
    return Path(home) / "Library/Application Support/vocaro-engineering/disk-reclaimed.jsonl"


def outside_checkouts(path):
    """A removal target must be an absolute path, reached through no link, below no working tree."""
    path = Path(path)
    if not path.is_absolute() or canonical(os.path.realpath(path)) != canonical(path): return False
    return not any((ancestor / ".git").exists() for ancestor in [path, *path.parents])


def record_removal(ledger, entry, out):
    out(f"{entry['outcome']}: {entry['path']}")
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with open(ledger, "a") as handle: handle.write(json.dumps(entry) + "\n")


def removal_entry(item, answered):
    entry = {"time": datetime.datetime.now(datetime.timezone.utc).isoformat(), "category": item.category,
             "path": item.path, "size": item.size, "reason": item.reason, "command": item.command}
    if answered: entry["approval"] = answered
    return entry


def delete(items, fresh, ledger, out=print, run=subprocess.run, approval=None):
    """Remove the candidates category by category, each checked again first, without asking: a
    candidate is what the rules prove nobody owns (the owner's decision, 2026-11-05). approval, the
    text given to --yes ("" when given bare), names in each ledger entry who asked for the run."""
    removed = 0
    answered = None if approval is None else "--yes" + (f": {approval}" if approval else "")
    for category, title in CATEGORIES.items():
        group = [item for item in items if item.category == category and item.status == CANDIDATE]
        if not group: continue
        out(f"Removing {len(group)} {title.lower()} candidates, {human(sum(item.size for item in group))}"
            + (f", on the approval {answered}." if answered else "."))
        current = {item.path: item for item in fresh(category)}
        for item in group:
            now = current.get(item.path)
            entry = removal_entry(item, answered)
            if now is None or now.status != CANDIDATE or now.command != item.command:
                entry["outcome"] = "skipped: " + (now.reason if now else "no longer present")
            elif item.command[0] == "rm" and not outside_checkouts(item.command[-1]):
                entry["outcome"] = "skipped: inside a repository's working tree or behind a link"
            else:
                result = run(item.command, capture_output=True, text=True, timeout=3600, env=git_environment())
                entry["outcome"] = "removed" if result.returncode == 0 else f"failed ({result.returncode}): {result.stderr.strip()[:300]}"
                removed += result.returncode == 0
            record_removal(ledger, entry, out)
    out(f"Removed {removed} items; the record of every decision is in {ledger}.")
    return removed


def add_arguments(parser):
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--dry-run", action="store_true", help="list only what --delete would remove, and remove nothing")
    action.add_argument("--delete", action="store_true", help="remove every candidate, checking each again first, "
                        "without asking; owner items are only listed, with their commands")
    parser.add_argument("--yes", nargs="?", const="", metavar="WHO",
                        help="with --delete, name in each ledger entry who asked for this run (such as 'owner, "
                        "2026-11-05 chat'); --delete asks nobody since 2026-11-05, so this only records the ask")
    parser.add_argument("--json", action="store_true", help="print the items as JSON")
    parser.add_argument("--min-idle-hours", type=float,
                        help="count an item idle only after this many unchanged hours (default 24)")
    parser.add_argument("--min-size-mb", type=float, default=100.0, help="list smaller items only as totals")
    parser.add_argument("--development", type=Path, help="directory holding the projects (default ~/Development)")


def prime_items(host, sizes):
    """One du pass over each folder that holds many small items; everything else is measured alone."""
    for prime in EXTENSION_PRIMES:
        for root, depth in prime(host):
            if root: sizes.prime(root, depth)


def execute(args, run=subprocess.run, out=print):
    if args.yes is not None and not args.delete:
        print("eng disk: --yes names who asked for --delete's removals, so it needs --delete", file=sys.stderr)
        return 2
    def collect(categories=None, measure=True):
        idle_hours = 24.0 if args.min_idle_hours is None else args.min_idle_hours
        host = host_class().current(idle_hours * 3600, args.development, run)
        sizes = Sizes(run, measure)
        if measure:
            print("Measuring the items listed.", file=sys.stderr)
            prime_items(host, sizes)
        return host, sizes, inventory(host, sizes, categories)
    host, sizes, items = collect()
    try:
        usage = shutil.disk_usage(DATA_VOLUME)
        volume = (usage.total, usage.used, usage.free)
    except OSError:
        volume = None
    if args.dry_run: items = [item for item in items if item.status == CANDIDATE]
    if args.json:
        out(json.dumps({"volume": volume, "items": [asdict(item) for item in items]}, indent=2))
    elif args.dry_run:
        plan(items, out)
    else:
        report(items, volume, sizes.unreadable, args.min_size_mb * 1024 * 1024, out)
    if args.delete:
        ledger = ledger_path(host.home)
        delete(items, lambda category: collect([category], measure=False)[2], ledger, out=out, run=run,
               approval=args.yes)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="eng disk", description=__doc__.split("\n")[0])
    add_arguments(parser)
    try:
        return execute(parser.parse_args(argv))
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"eng disk: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
