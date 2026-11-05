"""Inventory of disk space on the shared host, and removal only of what nobody owns.

`eng disk` measures what the projects in ~/Development leave on the host: their linked worktrees,
simulators and runtimes, the build caches their builds fill, and the scratch builds and results
their sessions leave in the temporary directories. It reads nothing else, and classifies each item:

- candidate: the rules prove that no session's live work is in it. `--delete` removes it after
  the owner confirms its category on a terminal, checking it again first. Simulators and runtimes,
  which Xcode makes or downloads again, it removes without asking, on a terminal or not (the
  owner's decision, 2026-11-03). `--yes`, given only on the owner's approval of that run, answers
  for him, so an agent can carry an approval he gave in chat into a run without a terminal.
- owner: nothing shows a session using it, but only the owner can say whether it is still
  wanted. The command that would remove it is printed; `--delete` never runs it. An evidence
  archive's command moves it to the second Mac, and eng's offload retry runs it by itself once the
  archive is quiet and unused (evidence_offload).
- kept: it is in use, owned, of unclear ownership, or holds work or evidence.

`--dry-run` lists what `--delete` would remove. Without --delete nothing is changed.
guides/shared-host.md § Reclaiming disk space has the rules.
"""
import argparse
from dataclasses import asdict, dataclass, field
import datetime
import glob
import json
import os
from pathlib import Path
import plistlib
import pwd
import re
import shlex
import shutil
import subprocess
import sys
import time

from . import devices as device_record, failure_records as failures, ignored, landing
from .config import TOOLKIT_ROOT
from .simulators import pool as simulator_pool

CANDIDATE, OWNER, KEPT = "candidate", "owner", "kept"
STATUS_ORDER = {CANDIDATE: 0, OWNER: 1, KEPT: 2}
CATEGORIES = {"worktree": "Linked worktrees", "simulator": "Simulators", "runtime": "Simulator runtimes",
              "derived-data": "Xcode DerivedData", "scratch": "Scratch builds outside checkouts",
              "swiftpm-cache": "SwiftPM shared cache", "results": "Result directories",
              "failures": "Kept failure records (pruned by their rule)",
              "evidence": "Evidence archives (offload to the second Mac)"}
# The categories --delete removes without asking: Xcode makes simulators and downloads runtimes again, so
# deleting one is no longer the owner's call (the owner's decision, 2026-11-03).
UNASKED = {"simulator", "runtime"}
# A runtime no device uses is removed once it has gone unused this long, as a pool member is pruned: a
# lane may make its devices on it next.
RUNTIME_IDLE_DAYS = 14
UUID = re.compile(r"[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}", re.IGNORECASE)
SEPARATORS = re.compile(r"[\s=,;\"']+")
# Xcode names a workspace's DerivedData folder NAME-<28 letters>; other folders are shared caches.
WORKSPACE_FOLDER = re.compile(r".+-[a-z]{28}")
# What an xcodebuild leaves in a folder it writes no info.plist for, when it resolves packages or tests
# without building: logs, resolved package checkouts and test results, and no build products to lose.
UNBUILT = frozenset({"Logs", "SourcePackages", "TestResults"})
# A Build folder that holds nothing but ProfileData holds a test run's coverage data, not build products:
# xcodebuild test-without-building without -derivedDataPath leaves one per run (vocaro-engineering#158).
COVERAGE_ONLY = frozenset({"ProfileData"})
# Ignored files under a directory of one of these names may be evidence; ignored.py, which Closeout
# reads too, says which ignored files are caches a tool rebuilds and keeps a worktree for any other.
EVIDENCE_NAMES = ignored.EVIDENCE_NAMES
# A durable evidence archive in the projects directory, such as pdfreflowlib-measurements, or the
# archive Closeout used to keep in a main checkout; each belongs on the second Mac.
EVIDENCE_ARCHIVE = re.compile(r".+-(evidence|measurements)|session-evidence")
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
    """Everything the inventory reads from the machine, so tests can supply it."""
    home: Path
    development: Path
    now: float
    min_idle: float
    processes: list
    simulators: dict = None
    runtimes: dict = None
    default_devices: frozenset = frozenset()
    device_directory: Path = None
    records: dict = field(default_factory=dict)
    temp_roots: list = field(default_factory=list)
    session_scratch: Path = None
    pools: frozenset = frozenset()
    offload_host: str = None  # the second Mac, when this Mac sends work to one
    run: object = subprocess.run

    @classmethod
    def current(cls, min_idle, development=None, run=subprocess.run):
        home = account_home()
        devices = home / "Library/Developer/CoreSimulator/Devices"
        temp = [(Path("/private/tmp"), 6)]
        user_temp = run(["getconf", "DARWIN_USER_TEMP_DIR"], capture_output=True, text=True).stdout.strip()
        if user_temp: temp.append((Path(os.path.realpath(user_temp)), 2))
        return cls(home=home, development=Path(development or home / "Development"), now=time.time(),
                   min_idle=min_idle, processes=census(run), simulators=_json(run, "list", "-j"),
                   runtimes=_json(run, "runtime", "list", "-j"), default_devices=default_devices(devices),
                   device_directory=devices, records=device_records(), temp_roots=temp,
                   session_scratch=Path(f"/private/tmp/claude-{os.getuid()}"),
                   pools=pools(development or home / "Development", run),
                   offload_host=second_mac(), run=run)


def _json(run, *arguments):
    try:
        result = run(["xcrun", "simctl", *arguments], capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        return json.loads(result.stdout) if result.returncode == 0 else None
    except ValueError:
        return None


def pools(development, run=subprocess.run):
    """The simulator pools the projects' main checkouts name in engineering.json, in the working
    tree or at origin/main, since a main checkout can lag the project's main."""
    if not Path(development).is_dir(): return frozenset()
    found = set()
    for path in Path(development).iterdir():
        if not (path / ".git").is_dir(): continue
        found.add(simulator_pool.configured(path))
        try:
            text = git(run, path, "show", "origin/main:engineering.json", timeout=30).stdout
            found.add(json.loads(text).get("simulatorPool"))
        except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
            pass
    return frozenset(filter(None, found))


def default_devices(directory):
    """The devices CoreSimulator made for each runtime, which any session may use."""
    return simulator_pool.default_devices(directory)


def device_records():
    """Every device reservation record's text, or None when a record exists but cannot be read.

    The devices module owns the paths. The account's record is read even under an override, so an
    isolated record never hides a real reservation, and legacy records still count."""
    paths = list(dict.fromkeys([device_record.record_path(), device_record.default_path()]))
    paths += sorted(map(Path, glob.glob(device_record.LEGACY)))
    records = {}
    for path in paths:
        if not path.exists(): continue
        try: records[str(path)] = path.read_text(errors="replace")
        except OSError: records[str(path)] = None
    return records


def git_environment():
    # A hook's exported GIT_DIR and its relatives would point every command at one repository.
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment["GIT_OPTIONAL_LOCKS"] = "0"  # status must not refresh another session's index
    return environment


def git(run, cwd, *arguments, timeout=300):
    return run(["git", "-C", str(cwd), *arguments], capture_output=True, text=True, timeout=timeout,
               env=git_environment())


def last_activity(path):
    """The newest change to a directory, its top-level entries and, for a worktree, its Git state."""
    path = Path(path)
    paths = [path]
    try: paths += list(path.iterdir())
    except OSError: pass
    try:
        text = (path / ".git").read_text()
        if text.startswith("gitdir:"):
            gitdir = Path(text.split(":", 1)[1].strip())
            gitdir = gitdir if gitdir.is_absolute() else path / gitdir
            paths += [gitdir / "HEAD", gitdir / "index", gitdir / "logs/HEAD"]
    except OSError: pass
    times = []
    for candidate in paths:
        try: times.append(candidate.lstat().st_mtime)
        except OSError: pass
    return max(times) if times else 0.0


# Linked worktrees

def parse_worktrees(text):
    entries = []
    for line in text.splitlines():
        if line.startswith("worktree "): entries.append({"worktree": line[len("worktree "):]})
        elif entries and line:
            key, _, value = line.partition(" ")
            entries[-1][key] = value
    return entries


def landing_base(run, main):
    """The commit origin/main named when this inventory read it, read once per repository."""
    for ref in ("refs/remotes/origin/HEAD", "refs/remotes/origin/main"):
        result = git(run, main, "rev-parse", "--verify", "--quiet", ref + "^{commit}")
        if result.returncode == 0 and result.stdout.strip():
            name = git(run, main, "rev-parse", "--abbrev-ref", ref).stdout.strip() or ref
            return result.stdout.strip(), name
    return None, None


def landed(run, main, head, base, name):
    """(whether HEAD's work is in BASE, why, how), by the proof Closeout's landed.sh uses
    (landing.contained): contained in it, an equal patch there for every commit, or a merge that
    would change nothing. The inventory never fetches, so it has no pull request proof."""
    short = f"HEAD {head[:10]}"
    how = landing.contained(lambda *arguments: git(run, main, *arguments), base, head)
    if how == landing.ANCESTOR: return True, f"{short} is in {name} {base[:10]}", how
    if how == landing.CONTENT_MERGED:
        return True, f"merging {short} into {name} {base[:10]} would change nothing", how
    # The rest only says why, or how many: the verdict is how's.
    merges = git(run, main, "rev-list", "--merges", f"{base}..{head}")
    marks = git(run, main, "rev-list", "--cherry-mark", "--right-only", "--no-merges", f"{base}...{head}")
    commits = marks.stdout.split()
    if how == landing.PATCH_EQUIVALENT:
        return True, f"each of the {len(commits)} commits on {short} has an equal patch in {name}", how
    if merges.returncode or marks.returncode: return False, f"could not compare {short} with {name}", how
    if merges.stdout.strip(): return False, f"merge commits on {short} are not in {name} {base[:10]}", how
    pending = [commit for commit in commits if not commit.startswith("=")]
    return False, f"{len(pending)} commits on {short} are not in {name} {base[:10]}", how


def session_root(host, main, path):
    for root in (Path(main) / ".claude/worktrees", Path(main) / ".codex/worktrees", host.home / ".codex/worktrees"):
        if within(path, root) and canonical(path) != canonical(root): return root
    return None


def worktree(host, usage, sizes, main, entry, base, name):
    path = entry["worktree"]
    def item(status, reason, command=None, idle=None):
        return Item("worktree", path, status, reason, sizes.of(path) if os.path.isdir(path) else 0, command, idle)
    if "prunable" in entry or not os.path.isdir(path):
        return item(OWNER, "registered, but its directory is gone; `git worktree prune` forgets every such entry",
                    ["git", "-C", str(main), "worktree", "prune"])
    if "locked" in entry and entry["locked"].startswith(RETIRED):
        use = usage.of(path)
        if use: return item(KEPT, f"{entry['locked']}, but still in use: " + described(use))
        return item(OWNER, f"{entry['locked']}, and no process uses it; it holds only what the queue's lanes "
                           "left, each round's logs being in the queue's own", ["git", "-C", str(main), "worktree",
                                                                              "remove", "--force", "--force", path])
    if "locked" in entry: return item(KEPT, "locked" + (f": {entry['locked']}" if entry["locked"] else ""))
    use = usage.of(path)
    if use: return item(KEPT, "in use: " + described(use))
    status = git(host.run, path, "status", "--porcelain", "--untracked-files=normal")
    if status.returncode: return item(KEPT, "git status failed: " + status.stderr.strip()[:200])
    changed = status.stdout.splitlines()
    if changed: return item(KEPT, f"dirty: {len(changed)} modified or untracked paths")
    try: evidence = ignored.keeps(path)
    except (ignored.Refusal, OSError) as error: return item(KEPT, f"could not list its ignored files: {error}")
    if evidence: return item(KEPT, "ignored files that may be evidence: " + ", ".join(evidence[:3]))
    if not entry.get("HEAD") or not base: return item(KEPT, "no HEAD or no origin/main to compare it with")
    done, why, how = landed(host.run, main, entry["HEAD"], base, name)
    if not done: return item(KEPT, "unlanded: " + why)
    # Removing a detached worktree removes the last ref to commits origin/main holds only under other
    # IDs. Whether evidence cites them takes the project's open issues, which the inventory never reads.
    if how != landing.ANCESTOR and "branch" not in entry:
        return item(KEPT, f"landed ({why}), but its detached HEAD is the last ref to commits origin/main does not "
                          "reach, and whether the project's open issues cite them takes the network; Closeout's "
                          "landed.sh checks")
    idle = host.now - last_activity(path)
    if idle < host.min_idle: return item(KEPT, f"clean and landed ({why}), but changed {age(idle)} ago", idle=idle)
    command = ["git", "-C", str(main), "worktree", "remove", path]
    reason = f"clean, unlocked, no process; {why}; idle {age(idle)}"
    if session_root(host, main, path) is None:
        return item(OWNER, reason + "; outside the session worktree directories, so a persistent or shared checkout",
                    command, idle)
    return item(CANDIDATE, reason, command, idle)


def unregistered(host, usage, sizes, mains, registered):
    """Directories in the session worktree directories that no repository has registered."""
    directories = []
    for root in [main / ".claude/worktrees" for main in mains] + [main / ".codex/worktrees" for main in mains]:
        if root.is_dir(): directories += [child for child in root.iterdir() if child.is_dir() and not child.is_symlink()]
    codex = host.home / ".codex/worktrees"
    if codex.is_dir():
        for group in codex.iterdir():
            if group.is_dir() and not group.is_symlink():
                directories += [child for child in group.iterdir() if child.is_dir() and not child.is_symlink()]
    items = []
    for directory in directories:
        key = canonical(os.path.realpath(directory))
        if key in registered or any(within(path, key) for path in registered): continue
        def item(status, reason, command=None):
            return Item("worktree", str(directory), status, reason, sizes.of(directory), command,
                        host.now - last_activity(directory))
        use = usage.of(directory)
        if use:
            items.append(item(KEPT, "not a registered worktree, but in use: " + described(use)))
            continue
        gitdir = None
        try:
            text = (directory / ".git").read_text()
            gitdir = Path(text.split(":", 1)[1].strip()) if text.startswith("gitdir:") else None
        except OSError: pass
        if (directory / ".git").is_dir():
            items.append(item(KEPT, "a separate repository, not a linked worktree"))
        elif gitdir is not None and gitdir.exists():
            items.append(item(KEPT, f"registered with a repository outside {host.development}: {gitdir}"))
        else:
            items.append(item(OWNER, "no repository has registered it, so Git cannot show whether it holds "
                                     "uncommitted work", ["rm", "-rf", "--", str(directory)]))
    return items


def main_checkouts(host):
    if not host.development.is_dir(): return []
    return sorted(path for path in host.development.iterdir() if (path / ".git").is_dir())


def checkouts(host):
    """Every main checkout in the projects directory and every registered worktree on disk."""
    paths = main_checkouts(host)
    for main in list(paths):
        listing = git(host.run, main, "worktree", "list", "--porcelain")
        paths += [Path(entry["worktree"]) for entry in parse_worktrees(listing.stdout)[1:]
                  if os.path.isdir(entry["worktree"])]
    return paths


def encoded(path):
    """Claude Code names a session's scratch directory after its checkout, each other character a hyphen."""
    return re.sub(r"[^A-Za-z0-9-]", "-", str(path))


def worktrees(host, usage, sizes):
    mains = main_checkouts(host)
    items, registered = [], set()
    for main in mains:
        listing = git(host.run, main, "worktree", "list", "--porcelain")
        if listing.returncode:
            items.append(Item("worktree", str(main), KEPT, "git worktree list failed: " + listing.stderr.strip()[:200]))
            continue
        entries = parse_worktrees(listing.stdout)[1:]  # the first is the main worktree
        base, name = landing_base(host.run, main)
        for entry in entries:
            registered.add(canonical(os.path.realpath(entry["worktree"])))
            items.append(worktree(host, usage, sizes, main, entry, base, name))
    return items + unregistered(host, usage, sizes, mains, registered)


# Simulators and runtimes

def disposable_name(name):
    """A disposable device carries a UUID in its unique name; a pool or default device never does."""
    return bool(UUID.search(name))


def idle_since(stamp, now):
    """Seconds since an ISO 8601 stamp such as simctl's lastUsedAt, or None when there is none to read."""
    if not stamp: return None
    try: return now - datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError): return None


def last_used(device, directory, now):
    idle = idle_since(device.get("lastUsedAt") or device.get("lastBootedAt"), now)
    if idle is not None: return idle
    return now - last_activity(directory) if directory and os.path.isdir(directory) else None


def prune_command(pool, udid):
    """Pruning reserves the member, checks it again and records the deletion (guides/simulator-testing.md)."""
    return ["python3", str(TOOLKIT_ROOT / "bin/eng"), "simulator-pool", "--pool", pool, "--owner", "eng disk",
            "prune", udid]


def pool_members(host):
    """Each pool member's UDID: its pool, the member and the pool's members."""
    found = {}
    for pool in host.pools:
        every = simulator_pool.members(pool, host.simulators)
        found.update({member.udid: (pool, member, every) for member in every})
    return found


def pool_templates(host):
    """Each pool's warmed templates by UDID: the pool and the template."""
    return {template.udid: (pool, template) for pool in host.pools
            for template in simulator_pool.templates(pool, host.simulators)}


def record_entries(host):
    """Every readable record's entries, as one record for the pruning rule."""
    entries = {}
    for text in host.records.values():
        try: value = json.loads(text) if text else {}
        except ValueError: continue
        if isinstance(value, dict) and isinstance(value.get("devices"), dict): entries.update(value["devices"])
    return {"devices": entries}


def simulators(host, usage, sizes):
    if host.simulators is None:
        return [Item("simulator", "simctl", KEPT, "`xcrun simctl list -j` failed, so no simulator can be shown unowned")]
    unreadable = [path for path, text in host.records.items() if text is None]
    pooled, entries, templated = pool_members(host), record_entries(host), pool_templates(host)
    now = datetime.datetime.fromtimestamp(host.now, datetime.timezone.utc)
    items, listed = [], set()
    for runtime, devices in host.simulators.get("devices", {}).items():
        for device in devices:
            udid, name = device["udid"].upper(), device.get("name", "")
            listed.add(udid)
            data = device.get("dataPath")
            directory = os.path.dirname(data) if data else (str(host.device_directory / udid) if host.device_directory else "")
            size = sizes.of(directory) if directory and os.path.isdir(directory) else \
                int(device.get("dataPathSize") or 0) + int(device.get("logPathSize") or 0)
            idle = last_used(device, directory, host.now)
            label = f"{udid} {name} ({runtime.rsplit('.', 1)[-1]})"
            def item(status, reason, command=None):
                return Item("simulator", label, status, reason, size, command, idle, directory or None)
            command = ["xcrun", "simctl", "delete", udid]
            # A record names a device by UDID or by its name as a JSON string.
            record = next((path for path, text in host.records.items() if text and (udid in text.upper() or
                          (name and json.dumps(name).upper() in text.upper()))), None)
            use = usage.of(directory) if directory else None
            process = usage.naming(udid)
            if device.get("state") != "Shutdown": items.append(item(KEPT, f"{device.get('state')}: a session is using it"))
            elif unreadable: items.append(item(KEPT, "a device record could not be read: " + ", ".join(unreadable)))
            elif use: items.append(item(KEPT, "in use: " + described(use)))
            elif process: items.append(item(KEPT, f"named in the arguments of PID {process.pid}"))
            # The pruning rule reads the record's entries for a member itself.
            elif udid in pooled:
                pool, member, every = pooled[udid]
                allowed, why = simulator_pool.prunable(member, every, simulator_pool.entries_for(member, entries), now)
                items.append(item(CANDIDATE, f"a member of the {pool} pool eligible for pruning: {why}",
                                  prune_command(pool, udid)) if allowed else
                             item(KEPT, f"a member of the {pool} pool, kept: {why}"))
            elif udid in templated:
                pool, template = templated[udid]
                items.append(item(KEPT, f"the {pool} pool's warmed template of its kind, which members are reset "
                                        "from and new members cloned from") if device.get("isAvailable", True) else
                             item(CANDIDATE, f"the {pool} pool's warmed template of its kind, unavailable, its runtime "
                                             "gone; the next new member of the kind warms another",
                                  prune_command(pool, udid)))
            elif record: items.append(item(KEPT, f"named in the device record {record}"))
            elif disposable_name(name):
                if idle is not None and idle >= host.min_idle:
                    gone = "" if device.get("isAvailable", True) else "; unavailable, its runtime is gone"
                    items.append(item(CANDIDATE, f"a disposable device (unique name) left shut down and unreferenced; "
                                                 f"last used {age(idle)} ago{gone}", command))
                else: items.append(item(KEPT, "a disposable device with no recorded use" if idle is None
                                        else f"a disposable device last used {age(idle)} ago"))
            elif not device.get("isAvailable", True):
                items.append(item(CANDIDATE, f"unavailable ({device.get('availabilityError', 'runtime missing')}), "
                                             "so nothing can run on it", command))
            elif udid in host.default_devices:
                items.append(item(CANDIDATE, "Xcode's default device for its runtime, which Xcode makes again; a "
                                             "script that picks a device by name fills it instead of a pool member",
                                  command))
            else:
                items.append(item(KEPT, "a named device in no project's pool, perhaps made by hand; "
                                        "only its owner removes it"))
    if host.device_directory and host.device_directory.is_dir():
        for directory in host.device_directory.iterdir():
            if directory.is_dir() and UUID.fullmatch(directory.name) and directory.name.upper() not in listed:
                use = usage.of(directory)
                items.append(Item("simulator", str(directory), KEPT if use else CANDIDATE,
                                  "in use: " + described(use) if use else "a device directory CoreSimulator no longer lists",
                                  sizes.of(directory), None if use else ["rm", "-rf", "--", str(directory)],
                                  host.now - last_activity(directory)))
    return items


def runtimes(host):
    if host.runtimes is None or host.simulators is None: return []
    used = {}
    for runtime, devices in host.simulators.get("devices", {}).items():
        used[runtime] = used.get(runtime, 0) + len(devices)
    items = []
    for key, image in host.runtimes.items():
        identifier = image.get("runtimeIdentifier", "")
        label = f"{image.get('identifier', key)} {identifier.rsplit('.', 1)[-1]} ({image.get('build', '?')})"
        size, stamp = int(image.get("sizeBytes") or 0), image.get("lastUsedAt")
        if used.get(identifier):
            items.append(Item("runtime", label, KEPT, f"{used[identifier]} devices use it", size))
        elif image.get("deletable") is False:
            items.append(Item("runtime", label, KEPT, "not deletable: it came with Xcode", size))
        else:
            idle = idle_since(stamp, host.now)
            command = ["xcrun", "simctl", "runtime", "delete", image.get("identifier", key)]
            if idle is not None and idle < RUNTIME_IDLE_DAYS * 86400:
                items.append(Item("runtime", label, KEPT, f"no device uses it, but it was used {age(idle)} ago, within "
                                  f"{RUNTIME_IDLE_DAYS} days; a lane may create its devices on it next", size, idle=idle))
            else:
                items.append(Item("runtime", label, CANDIDATE, f"no device uses it; last used {stamp or 'never'}, "
                                  f"over {RUNTIME_IDLE_DAYS} days ago or never; xcodebuild downloads it again", size,
                                  command, idle))
    return items


# Build caches and results

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


def derived_data(host, usage, sizes):
    root = host.home / "Library/Developer/Xcode/DerivedData"
    if not root.is_dir(): return []
    items = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.is_symlink(): continue
        if not WORKSPACE_FOLDER.fullmatch(child.name):
            items.append(Item("derived-data", str(child), KEPT, "shared by every Xcode build", sizes.of(child)))
            continue
        try:
            with open(child / "info.plist", "rb") as handle:
                workspace = plistlib.load(handle).get("WorkspacePath")
        except (OSError, plistlib.InvalidFileException, ValueError):
            workspace = None
        if workspace and not project_workspace(host, workspace): continue  # another workspace's build
        if not workspace:
            names = sorted(entry.name for entry in child.iterdir())
            held = ", ".join(names[:5]) or "nothing"
            built = sorted(entry.name for entry in (child / "Build").iterdir()) if "Build" in names \
                and (child / "Build").is_dir() and not (child / "Build").is_symlink() else None
            if set(names) - {"Build"} <= UNBUILT and ("Build" not in names or built == sorted(COVERAGE_ONLY)):
                # No project to trace it to, but nothing in it a build could need again: a candidate once
                # idle and unused, as a folder whose workspace is gone.
                what = "no build products" if "Build" not in names else "Build holding only coverage data"
                items.append(cache_item(host, usage, sizes, "derived-data", child,
                                        f"records no workspace; holds only {held}, {what}"))
                continue
            use = usage.of(child)
            items.append(Item("derived-data", str(child), KEPT if use else OWNER,
                              "in use: " + described(use) if use else f"records no workspace; holds {held}",
                              sizes.of(child), None if use else ["rm", "-rf", "--", str(child)],
                              host.now - last_activity(child)))
        elif os.path.exists(workspace):
            items.append(Item("derived-data", str(child), KEPT, f"its workspace {workspace} exists", sizes.of(child)))
        else:
            items.append(cache_item(host, usage, sizes, "derived-data", child, f"its workspace {workspace} is gone"))
    return items


def swiftpm_cache(host, usage, sizes):
    root = host.home / "Library/Caches/org.swift.swiftpm"
    items = []
    for kind, what in (("repositories", "a cached clone"), ("artifacts", "a cached binary artifact")):
        directory = root / kind
        if directory.is_dir():
            items += [cache_item(host, usage, sizes, "swiftpm-cache", child, what + " SwiftPM fetches again when needed")
                      for child in sorted(directory.iterdir()) if child.is_dir() and not child.is_symlink()]
    return items


def archive_folders(development):
    """The durable evidence archives in development that belong on the second Mac, each with the path
    `eng evidence offload --to` names for it there, or None for the same path under its home: a folder
    named as EVIDENCE_ARCHIVE says that is not a Git checkout, and a main checkout's
    .build/session-evidence, which goes to the shared session-evidence folder there, not into that
    project's checkout. `eng disk` lists them, and the offload's retry offers them unasked."""
    development = Path(development)
    if not development.is_dir(): return []
    folders = [(path, None) for path in sorted(development.iterdir())
               if path.is_dir() and not path.is_symlink() and EVIDENCE_ARCHIVE.fullmatch(path.name)
               and not (path / ".git").exists()]
    return folders + [(path, f"Development/session-evidence/{main.name}")
                      for main in sorted(path for path in development.iterdir() if (path / ".git").is_dir())
                      for path in [main / ".build/session-evidence"] if path.is_dir() and not path.is_symlink()]


def evidence_archives(host, sizes):
    """Durable evidence kept here when a second Mac could hold it. Offloading deletes it here only once
    every file verified there, and eng's retry offloads each by itself once it is quiet and unused
    (evidence_offload.retry)."""
    if not host.offload_host or not host.development.is_dir(): return []
    command = ["python3", "scripts/eng", "evidence", "offload"]
    folders = [(path, [*command, str(path), *(["--to", to] if to else [])])
               for path, to in archive_folders(host.development)]
    # A folder whose offload found the second Mac unreachable is listed wherever it is, with what eng does next.
    from . import evidence_offload  # it imports remote, which imports this module
    try: queued = evidence_offload.Queue(host.home).read()["folders"]
    except (OSError, ValueError): queued = {}
    listed = {canonical(path) for path, _ in folders}
    folders += [(Path(path), [*command, path, *(["--to", entry["to"]] if entry.get("to") else []),
                              *(["--keep"] if entry.get("keep") else [])])
                for path, entry in sorted(queued.items()) if canonical(path) not in listed and Path(path).is_dir()]
    queued = {canonical(path): entry for path, entry in queued.items()}

    def reason(path):
        said = (f"durable evidence; it belongs on {host.offload_host} (guides/evidence.md): offloading moves it, "
                "deleting it here once it verified there")
        entry = queued.get(canonical(path))
        if not entry:
            return (f"{said}; eng offloads it by itself once nothing in it has changed for "
                    f"{evidence_offload.QUIET // 3600} h and no process uses it")
        if evidence_offload.reoffered(entry):
            return f"{said}; a retry of its offload found the second Mac away, and eng offers it again once it answers: {entry['failed']}"
        if entry.get("failed"): return f"{said}; a retry of its offload failed, so it waits for a person: {entry['failed']}"
        return f"{said}; its offload found the second Mac unreachable on {entry.get('queued')}, and eng retries it"
    return [Item("evidence", str(path), OWNER, reason(path), sizes.of(path), offload, host.now - last_activity(path))
            for path, offload in folders]


def failure_records(host, usage, sizes):
    """Each checkout's kept failure records, .build/failures/NAME/RECORD/, counted, with those past their
    rule (failure_records.RULE) as one candidate, which `eng failures prune` removes. `eng failures keep`
    prunes by the same rule as it keeps a record, on either Mac."""
    items = []
    for checkout in checkouts(host):
        found = failures.records(checkout)
        if not found: continue
        path = Path(checkout) / failures.FAILURES
        kept = [record for records in found.values() for record in records]
        names = ", ".join(f"{name} ({len(records)})" for name, records in found.items())
        counted = f"{len(kept)} record{'s' if len(kept) != 1 else ''} of {names}"
        gone = failures.past_rule(checkout, host.now)
        idle = host.now - last_activity(path)
        use = usage.of(path)
        if use: items.append(Item("failures", str(path), KEPT, f"{counted}; in use: {described(use)}", sizes.of(path),
                                  None, idle))
        elif gone:
            items.append(Item("failures", str(path), CANDIDATE, f"{counted}; {len(gone)} past the rule, which "
                              f"{failures.RULE}", sum(sizes.of(record) for record in gone),
                              ["python3", str(TOOLKIT_ROOT / "bin/eng"), "failures", "prune", str(checkout)], idle))
        else:
            items.append(Item("failures", str(path), KEPT, f"{counted}; none past the rule, which {failures.RULE}",
                              sizes.of(path), None, idle))
    return items


def second_mac():
    """The second Mac this Mac offloads to, or None: here alone, or on the second Mac itself."""
    from . import remote  # remote imports this module
    try: return remote.configured()
    except ValueError: return None


def shape(names):
    if "workspace-state.json" in names: return "SwiftPM scratch"
    if "Build" in names and names & {"info.plist", "Logs", "SourcePackages", "Index.noindex", "ModuleCache.noindex"}:
        return "Xcode derived data"
    return None


def owner_record(directory):
    """The PID and start time a results directory records for its run, if it records one."""
    try: pid, _, started = (Path(directory) / "owner").read_text().strip().partition(" ")
    except (OSError, UnicodeDecodeError): return None
    return (int(pid), started.strip()) if pid.isdigit() else None


def run_is_live(run, pid, started):
    current = " ".join(run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True).stdout.split())
    # A reused PID has another start time. A record without one keeps a running PID's run live.
    return bool(current) and (not started or current == " ".join(started.split()))


def live_session(host, sessions, path):
    """Why a path in a Claude Code session's scratch may still be that session's, if it may."""
    if not host.session_scratch or not within(path, host.session_scratch): return None
    relative = os.path.relpath(canonical(path), canonical(host.session_scratch)).split(os.sep)
    name = relative[0] if relative and relative[0] != "." else ""
    if name in sessions: return sessions[name]
    # A session whose worktree was removed can still be running in its old directory.
    for process in host.processes:
        cwd = encoded(process.cwd) if process.cwd else ""
        if name and (cwd == name or cwd.startswith(name + "-")):
            return f"whose PID {process.pid} ({process.command}) still works in its checkout"
    return None


def temp_items(host, usage, sizes):
    """Scratch builds and results found under the temporary roots, outside every checkout."""
    found = []
    def walk(directory, depth):
        try: entries = list(os.scandir(directory))
        except OSError: return
        names = {entry.name for entry in entries}
        kind = shape(names)
        if ".git" in names: pass  # a checkout: the worktree rules decide about it
        elif kind: found.append(("scratch", directory, kind))
        elif "owner" in names and owner_record(directory): found.append(("results", directory, "record"))
        elif depth > 0:
            for entry in entries:
                if not entry.is_dir(follow_symlinks=False): continue
                if entry.name.endswith(".xcresult"): found.append(("results", entry.path, "bundle"))
                else: walk(entry.path, depth - 1)
    for root, depth in host.temp_roots:
        walk(str(root), depth)
    sessions = {encoded(checkout): f"whose checkout {checkout} still exists"
                for checkout in checkouts(host)} if host.session_scratch and found else {}
    items = []
    for category, path, kind in found:
        session = live_session(host, sessions, path)
        if category == "scratch" and session:
            items.append(Item("scratch", path, KEPT, f"{kind} in the scratch of a session {session}",
                              sizes.of(path), None, host.now - last_activity(path)))
        elif category == "scratch":
            items.append(cache_item(host, usage, sizes, "scratch", path, f"{kind} outside every checkout"))
        if category == "scratch": continue
        use = usage.of(path)
        record = owner_record(path) if kind == "record" else None
        idle = host.now - last_activity(path)
        if use: items.append(Item("results", path, KEPT, "in use: " + described(use), sizes.of(path), None, idle))
        elif record and run_is_live(host.run, *record):
            items.append(Item("results", path, KEPT, f"its run, PID {record[0]}, is still in progress", sizes.of(path), None, idle))
        else:
            what = "a finished run's results directory; its project prunes these by count" if record else "a test result bundle"
            items.append(Item("results", path, OWNER, f"{what}; keep it while it is worth reading", sizes.of(path),
                              ["rm", "-rf", "--", path], idle))
    return items


def inventory(host, sizes, categories=None):
    usage = Usage(host.processes)
    wanted = set(categories or CATEGORIES)
    items = []
    if "worktree" in wanted: items += worktrees(host, usage, sizes)
    if "simulator" in wanted: items += simulators(host, usage, sizes)
    if "runtime" in wanted: items += runtimes(host)
    if "derived-data" in wanted: items += derived_data(host, usage, sizes)
    if "swiftpm-cache" in wanted: items += swiftpm_cache(host, usage, sizes)
    if "evidence" in wanted: items += evidence_archives(host, sizes)
    if "failures" in wanted: items += failure_records(host, usage, sizes)
    if wanted & {"scratch", "results"}: items += [item for item in temp_items(host, usage, sizes) if item.category in wanted]
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
    out("Dry run: nothing was removed. `eng disk --delete` would offer to remove these, asking once for each "
        "category and checking each item again first.\n")
    for category, title in CATEGORIES.items():
        group = sorted((item for item in chosen if item.category == category), key=lambda item: -item.size)
        if not group: continue
        out(f"{title}: {len(group)} items, {human(sum(item.size for item in group))}")
        for item in group:
            out(f"  {human(item.size):>10}  {item.path}")
            out(f"            $ {shlex.join(item.command)}")
        out("")
    out(f"In all: {len(chosen)} items, {human(sum(item.size for item in chosen))}.")


# Removal

def ledger_path(home):
    return Path(home) / "Library/Application Support/vocaro-engineering/disk-reclaimed.jsonl"


def outside_checkouts(path):
    """A removal target must be an absolute path, reached through no link, below no working tree."""
    path = Path(path)
    if not path.is_absolute() or canonical(os.path.realpath(path)) != canonical(path): return False
    return not any((ancestor / ".git").exists() for ancestor in [path, *path.parents])


def delete(items, fresh, ledger, ask=input, out=print, run=subprocess.run, terminal=None, approval=None):
    """Remove candidates category by category after a fresh check of each: those of UNASKED at once,
    any other category once the owner confirms it on a terminal. Off a terminal, the others stay,
    unless approval (the text given to --yes, "" when none) answers for the owner: each is then
    removed as if he had typed y, and the ledger records the approval."""
    terminal = sys.stdin.isatty() if terminal is None else terminal
    removed = 0
    for category, title in CATEGORIES.items():
        group = [item for item in items if item.category == category and item.status == CANDIDATE]
        if not group: continue
        answered = None  # who approved removing the category, when the owner had to
        if category not in UNASKED and approval is not None:
            answered = "--yes" + (f": {approval}" if approval else "")
            out(f"Removing {len(group)} {title.lower()} candidates without asking, on the owner's approval ({answered}).")
        elif category not in UNASKED:
            answered = "y on a terminal"
            if not terminal:
                out(f"Kept every {title.lower()} candidate: removing them asks the owner on a terminal.")
                continue
            answer = ask(f"Remove {len(group)} {title.lower()} candidates, {human(sum(item.size for item in group))}? [y/N] ")
            if answer.strip().lower() not in ("y", "yes"):
                out(f"Kept every {title.lower()} candidate.")
                continue
        current = {item.path: item for item in fresh(category)}
        for item in group:
            now = current.get(item.path)
            entry = {"time": datetime.datetime.now(datetime.timezone.utc).isoformat(), "category": category,
                     "path": item.path, "size": item.size, "reason": item.reason, "command": item.command}
            if answered: entry["approval"] = answered
            if now is None or now.status != CANDIDATE or now.command != item.command:
                entry["outcome"] = "skipped: " + (now.reason if now else "no longer present")
            elif item.command[0] == "rm" and not outside_checkouts(item.command[-1]):
                entry["outcome"] = "skipped: inside a repository's working tree or behind a link"
            else:
                result = run(item.command, capture_output=True, text=True, timeout=3600, env=git_environment())
                entry["outcome"] = "removed" if result.returncode == 0 else f"failed ({result.returncode}): {result.stderr.strip()[:300]}"
                removed += result.returncode == 0
            out(f"{entry['outcome']}: {item.path}")
            ledger.parent.mkdir(parents=True, exist_ok=True)
            with open(ledger, "a") as handle: handle.write(json.dumps(entry) + "\n")
    out(f"Removed {removed} items; the record of every decision is in {ledger}.")
    return removed


def add_arguments(parser):
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--dry-run", action="store_true", help="list only what --delete would remove, and remove nothing")
    action.add_argument("--delete", action="store_true", help="remove candidates: simulators and runtimes at once, "
                        "any other category after the owner confirms it on a terminal")
    parser.add_argument("--yes", nargs="?", const="", metavar="APPROVAL",
                        help="with --delete, remove every candidate category without asking, as if the owner had "
                        "typed y to each, on a terminal or not; give it only on the owner's explicit approval of "
                        "this run, naming that approval (such as 'owner, 2026-11-04 chat'), which the ledger "
                        "records. It never runs an owner item's command")
    parser.add_argument("--json", action="store_true", help="print the items as JSON")
    parser.add_argument("--min-idle-hours", type=float, default=24.0,
                        help="how long a candidate must have been unchanged (default 24)")
    parser.add_argument("--min-size-mb", type=float, default=100.0, help="list smaller items only as totals")
    parser.add_argument("--development", type=Path, help="directory holding the projects (default ~/Development)")


def prime_items(host, sizes):
    """One du pass over each folder that holds many small items; everything else is measured alone."""
    for root, depth in ((host.home / "Library/Developer/Xcode/DerivedData", 1), (host.device_directory, 1),
                        (host.home / "Library/Caches/org.swift.swiftpm", 2)):
        if root: sizes.prime(root, depth)


def execute(args, run=subprocess.run, out=print):
    if args.yes is not None and not args.delete:
        print("eng disk: --yes answers the questions --delete asks, so it needs --delete", file=sys.stderr)
        return 2
    def collect(categories=None, measure=True):
        host = Host.current(args.min_idle_hours * 3600, args.development, run)
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
        delete(items, lambda category: collect([category], measure=False)[2], ledger_path(host.home), out=out, run=run,
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
