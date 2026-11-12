"""What Git says of a checkout: how far it is behind origin/main, and its worktrees.

Copied unchanged from the toolkit's toolkit_copy.py, which keeps the rest: which copy of the toolkit
`scripts/eng` runs (vocaro/vocaro-engineering#403)."""
import os
import subprocess

UPSTREAM = "refs/remotes/origin/main"
GIT_TIMEOUT = 10


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


def behind(toolkit):
    """How many commits origin/main has that this copy's HEAD lacks, or None without origin/main."""
    count = git(toolkit, "rev-list", "--count", f"HEAD..{UPSTREAM}")
    return int(count) if count and count.isdigit() else None


def last_landed(toolkit):
    return git(toolkit, "log", "-1", "--format=%s", UPSTREAM) or "unknown"


def worktrees(checkout):
    """Each worktree of checkout's repository, from `git worktree list --porcelain`, as a dict of its
    fields (worktree, HEAD, branch, detached, locked, prunable); the main checkout comes first."""
    listed = git(checkout, "worktree", "list", "--porcelain")
    entries = []
    for block in (listed or "").split("\n\n"):
        fields = dict((line.split(" ", 1) + [""])[:2] for line in block.splitlines() if line.strip())
        if "worktree" in fields: entries.append(fields)
    return entries
