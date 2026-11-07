"""Which copy of the toolkit `scripts/eng` runs, in the toolkit's own repository (vocaro-engineering#189).

It also lists, for `eng doctor`, leftover worktrees of the toolkit that would run old code (#220).

A session worktree of vocaro-engineering (.claude/worktrees/*) holds a toolkit copy of its own, which
falls behind origin/main as other sessions land: on 2026-11-05 one 63 commits behind ran `eng disk`
rules two days old, found nothing to delete on a Mac at 20 GiB free, and said nothing. Project
repositories are not affected: their launcher finds the toolkit beside their main checkout, from a
worktree too (integrations/shared.py), and that shared checkout follows main (#182).

choose decides, before any of the package is imported, so it imports nothing of it: scripts/eng loads
this file by its path.

- A copy that is not behind origin/main, or whose repository has no origin/main, runs silently.
- A linked worktree's copy that is behind, changes no toolkit file of its own (TOOLKIT_PATHS, against
  its merge base with origin/main, uncommitted and untracked files included) and runs a command that
  acts on state the host shares (HOST_COMMANDS) runs the main checkout's copy instead, when that copy
  is nearer origin/main, and says so in one line. Host state is everyone's, so the newest rules apply.
- Any other copy that is behind runs its own code, with one line naming the gap: a worktree testing its
  own toolkit change must run that change (`eng unittest`, its lanes, `eng land` of its branch), and a
  stale copy's tests and lanes belong to its own commit, which a newer runner may not read.

VOCARO_ENGINEERING_ROOT, which names the copy outright, skips all of this.
"""
import os
from pathlib import Path
import subprocess

UPSTREAM = "refs/remotes/origin/main"
# What the runner itself is made of. A change to tests, guides or agents leaves the runner main's.
TOOLKIT_PATHS = ("src/python", "scripts/eng", "integrations")
# Commands that act on what every session on the host shares: its disk, admission slots and log, the
# merge queue, both Macs and their keychains, the simulator pool, the device record, the kept app builds and the
# policy file.
HOST_COMMANDS = frozenset({"admissions", "admit", "app", "device-pass", "disk", "host", "keychain", "land", "policy", "queue",
                           "remote", "simulator-pool"})
GIT_TIMEOUT = 10
# Set to off, `eng doctor` lists no leftover worktrees: tests/host_isolation.py sets it, so that no doctor test
# reads the host's own worktrees of the toolkit.
LEFTOVER_SWITCH = "VOCARO_LEFTOVER_WORKTREES"


def environment():
    """The environment without a hook's GIT_DIR and the like, which would aim Git at another checkout."""
    try:
        names = subprocess.run(["git", "rev-parse", "--local-env-vars"], capture_output=True, text=True,
                               stdin=subprocess.DEVNULL, timeout=GIT_TIMEOUT).stdout.split()
    except (OSError, subprocess.SubprocessError):
        names = []
    return {key: value for key, value in os.environ.items() if key not in set(names)}


def git(directory, *arguments):
    """Git's standard output, stripped, or None when it failed."""
    try:
        done = subprocess.run(["git", "-C", str(directory), *arguments], capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, env=environment(), timeout=GIT_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def command(argv):
    """The subcommand argv names, skipping --project and its value, or None."""
    words = iter(argv)
    for word in words:
        if word == "--project":
            next(words, None)
        elif not word.startswith("-"):
            return word
    return None


def main_checkout(toolkit):
    """The main checkout of the repository toolkit is a linked worktree of, or None."""
    own = git(toolkit, "rev-parse", "--path-format=absolute", "--git-dir")
    common = git(toolkit, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if own is None or common is None or Path(own).resolve() == Path(common).resolve():
        return None
    return Path(common).resolve().parent


def behind(toolkit):
    """How many commits origin/main has that this copy's HEAD lacks, or None without origin/main."""
    count = git(toolkit, "rev-list", "--count", f"HEAD..{UPSTREAM}")
    return int(count) if count and count.isdigit() else None


def own_changes(toolkit):
    """The toolkit files this copy changes against its merge base with origin/main, committed or not,
    untracked ones included; None when that cannot be read, which counts as changes."""
    base = git(toolkit, "merge-base", "HEAD", UPSTREAM)
    if base is None: return None
    changed = git(toolkit, "diff", "--name-only", base, "--", *TOOLKIT_PATHS)
    untracked = git(toolkit, "ls-files", "--others", "--exclude-standard", "--", *TOOLKIT_PATHS)
    if changed is None or untracked is None: return None
    return sorted({line for line in (changed + "\n" + untracked).splitlines() if line.strip()})


def last_landed(toolkit):
    return git(toolkit, "log", "-1", "--format=%s", UPSTREAM) or "unknown"


def gap_line(toolkit, count):
    return (f"eng: this toolkit copy ({toolkit}) is {count} commit{'s' * (count != 1)} behind origin/main (last "
            f"landed: {last_landed(toolkit)}); `git merge --ff-only origin/main` there brings it up to date")


def choose(toolkit, argv):
    """(the toolkit checkout to run, the line to say first or None) for scripts/eng in toolkit, run with argv."""
    toolkit = Path(toolkit).resolve()
    count = behind(toolkit)
    if not count: return toolkit, None
    name = command(argv)
    main = main_checkout(toolkit)
    changes = own_changes(toolkit)
    if name in HOST_COMMANDS and main is not None and changes == [] \
            and (main / "src/python/vocaro_engineering/cli.py").is_file():
        theirs = behind(main)
        if theirs is not None and theirs < count:
            return main, (f"eng: `{name}` acts on state this host shares, so it runs the main checkout's toolkit "
                          f"({main}, {theirs} behind origin/main): this worktree's copy is {count} behind and "
                          "changes no toolkit file; VOCARO_ENGINEERING_ROOT=. runs this copy instead")
    line = gap_line(toolkit, count)
    if changes:
        shown = ", ".join(changes[:3]) + (f" and {len(changes) - 3} more" if len(changes) > 3 else "")
        line += f"; it runs its own code, since it changes {shown}"
    return toolkit, line


def worktrees(checkout):
    """Each worktree of checkout's repository, from `git worktree list --porcelain`, as a dict of its
    fields (worktree, HEAD, branch, detached, locked, prunable); the main checkout comes first."""
    listed = git(checkout, "worktree", "list", "--porcelain")
    entries = []
    for block in (listed or "").split("\n\n"):
        fields = dict((line.split(" ", 1) + [""])[:2] for line in block.splitlines() if line.strip())
        if "worktree" in fields: entries.append(fields)
    return entries


def working_directories():
    """The working directory of every live process lsof can see, or None when lsof cannot say."""
    try:
        done = subprocess.run(["lsof", "-nP", "-a", "-d", "cwd", "-Fn"], capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=GIT_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    if not done.stdout: return None
    return {Path(line[1:]) for line in done.stdout.splitlines() if line.startswith("n/")}


def leftover_worktrees(toolkit, directories=None):
    """Worktrees of the toolkit's repository nobody needs: detached, at a commit behind origin/main and
    reachable from it, unlocked, with nothing uncommitted or untracked, and no live process's working
    directory (when lsof can say). A session that sets VOCARO_ENGINEERING_ROOT to one runs old toolkit
    code; one found on 2026-11-06 was three days behind. Each is (path, commit, commits behind); the main
    checkout is never one. directories stands in for working_directories in tests."""
    found = []
    for entry in worktrees(toolkit)[1:]:
        path = Path(entry["worktree"])
        if "detached" not in entry or "locked" in entry or "prunable" in entry or not path.is_dir(): continue
        head = entry.get("HEAD", "")
        if git(path, "merge-base", "--is-ancestor", head, UPSTREAM) is None: continue
        count = git(path, "rev-list", "--count", f"{head}..{UPSTREAM}")
        if not count or not count.isdigit() or int(count) == 0: continue
        if git(path, "status", "--porcelain", "--untracked-files=all") != "": continue
        found.append((path, head, int(count)))
    if found:
        directories = working_directories() if directories is None else directories
        if directories:
            found = [item for item in found
                     if not any(d == item[0].resolve() or item[0].resolve() in d.parents for d in directories)]
    return found


def ignored_kept(path):
    """The ignored paths in a worktree, collapsed as `git status --ignored` shows them, that are not Python's
    caches: `git worktree remove` deletes them with it, and eng's results or an agent's evidence under
    .build are not caches (agents/core.md). None when they cannot be read."""
    listed = git(path, "status", "--porcelain", "--ignored", "--untracked-files=normal")
    if listed is None: return None
    return [line[3:] for line in listed.splitlines() if line.startswith("!! ")
            and not line.rstrip("/").endswith("__pycache__") and not line.endswith(".pyc")]


def leftover_line(path, head, count):
    line = (f"{path} is a leftover worktree of the toolkit: detached at {head[:12]}, {count} commit"
            f"{'s' * (count != 1)} behind origin/main and already in it, clean and unused; a session pointed at it "
            f"through VOCARO_ENGINEERING_ROOT runs old toolkit code. `git worktree remove {path}` removes it "
            "(a landed leftover needs no owner, agents/core.md)")
    kept = ignored_kept(path)
    if kept is None:
        return line + ", but its ignored files could not be read: look before removing it"
    if kept:
        return line + (f", and with it the ignored {', '.join(kept)}, which it deletes too: archive any results or "
                       "evidence there first (guides/skills.md, Ignored files)")
    return line


def doctor_warnings(toolkit, directories=None):
    """`eng doctor`'s warnings for a toolkit copy behind origin/main, from any project, and for leftover
    worktrees of the toolkit's repository, which it lists and never removes."""
    count = behind(toolkit)
    warnings = [gap_line(Path(toolkit).resolve(), count)] if count else []
    if os.environ.get(LEFTOVER_SWITCH) == "off": return warnings
    return warnings + [leftover_line(*item) for item in leftover_worktrees(toolkit, directories)]
