"""What Git says of a checkout: how far it is behind origin/main, whether it is off main, and its worktrees."""
import os
from pathlib import Path
import re
import shutil
import subprocess

UPSTREAM = "refs/remotes/origin/main"
GIT_TIMEOUT = 10
# A release's tag, as tools/release.py makes it: vX.Y.Z.
RELEASE_TAG = re.compile(r"v(\d+)\.(\d+)\.(\d+)")


# What each git that PATH finds answers to `git rev-parse --local-env-vars`: a list built into Git, so asked once in a
# process rather than before each Git command, which started a process of its own every time (vocaro/queuekeeper#257).
LOCAL_VARIABLES = {}


def local_variables(timeout=None):
    """The variables that aim Git at a repository, GIT_DIR and the like, as `git rev-parse --local-env-vars` names
    them. A failed answer is not kept; git that cannot start raises as subprocess.run does."""
    found = shutil.which("git")
    if found in LOCAL_VARIABLES: return LOCAL_VARIABLES[found]
    done = subprocess.run(["git", "rev-parse", "--local-env-vars"], capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, timeout=timeout)
    names = frozenset(done.stdout.split())
    if found and done.returncode == 0 and names: LOCAL_VARIABLES[found] = names
    return names


def environment():
    """The environment without a hook's GIT_DIR and the like, which would aim Git at another checkout."""
    try:
        names = local_variables(GIT_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        names = frozenset()
    return {key: value for key, value in os.environ.items() if key not in names}


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


def off_main(toolkit):
    """What to say when toolkit, the Queuekeeper this process loaded, is a main checkout whose HEAD is not on main:
    on another branch, or detached at a commit that neither main nor origin/main holds; otherwise None.

    Every project's launcher loads Queuekeeper from the shared checkout, ~/Development/queuekeeper, so while it is on
    a work branch any qk run by hand in any project runs that branch's code. On 2026-11-15 a session left it on its
    work branch for 45 minutes, and nothing said so (vocaro/queuekeeper#184). A linked worktree, which a session's
    own checkout and each queue worker's copy are, is never the shared checkout and says nothing. HEAD is read from
    its file, so a checkout on main, every run's case, costs no Git process."""
    try:
        head = (Path(toolkit) / ".git/HEAD").read_text().strip()
    except OSError:  # no repository, or a linked worktree, whose .git is a file
        return None
    if head == "ref: refs/heads/main": return None
    if head.startswith("ref: refs/heads/"):
        commit = git(toolkit, "rev-parse", "--short=12", "HEAD") or "no commit yet"
        where = f"on branch {head.removeprefix('ref: refs/heads/')} at {commit}"
    else:
        if any(git(toolkit, "merge-base", "--is-ancestor", head, ref) is not None
               for ref in ("refs/heads/main", UPSTREAM)): return None
        where = f"detached at {head[:12]}, a commit main lacks"
    return (f"Queuekeeper loads from {toolkit}, {where}, not main, so this runs code main may not hold. Every "
            "project's launcher loads Queuekeeper from the shared checkout: put it back on main and work on "
            "Queuekeeper in a linked worktree (vocaro/queuekeeper#184)")



def release(checkout):
    """The release tag, such as v1.1.0, that checkout, a main checkout of Queuekeeper, is detached at, or None.

    The install steps check out the newest release's tag (vocaro/queuekeeper#351). A checkout on a branch, main
    among them, follows that branch, and one detached at a commit no release tag names is no release. When several
    release tags name the commit, the highest is its release. HEAD is read from its file, so a checkout on a branch
    costs no Git process."""
    try:
        head = (Path(checkout) / ".git/HEAD").read_text().strip()
    except OSError:  # no repository, or a linked worktree, whose .git is a file
        return None
    if head.startswith("ref:"): return None
    tags = [tag for tag in (git(checkout, "tag", "--points-at", "HEAD") or "").split() if RELEASE_TAG.fullmatch(tag)]
    return max(tags, key=lambda tag: tuple(map(int, RELEASE_TAG.fullmatch(tag).groups())), default=None)


def shared_changes(toolkit):
    """Warn when the loaded main checkout has working changes. Linked worktrees are for edits."""
    if not (Path(toolkit) / ".git/HEAD").is_file(): return None
    if git(toolkit, "--no-optional-locks", "status", "--porcelain", "--untracked-files=normal"):
        return (f"Queuekeeper loads from {toolkit}, whose main checkout has uncommitted changes. Projects using "
                "this checkout can run those changes. Inspect them with git status in that checkout, preserve them "
                "in a linked worktree, and keep the main checkout clean (vocaro/queuekeeper#179)")
    return None


def worktrees(checkout):
    """Each worktree of checkout's repository, from `git worktree list --porcelain`, as a dict of its
    fields (worktree, HEAD, branch, detached, locked, prunable); the main checkout comes first."""
    listed = git(checkout, "worktree", "list", "--porcelain")
    entries = []
    for block in (listed or "").split("\n\n"):
        fields = dict((line.split(" ", 1) + [""])[:2] for line in block.splitlines() if line.strip())
        if "worktree" in fields: entries.append(fields)
    return entries


def main_checkout(root):
    """The main checkout of the repository that root is a linked worktree of, or None.

    A linked worktree's .git is a file naming its directory inside the repository's .git, whose
    commondir file leads back to that .git: the parent of `git rev-parse --git-common-dir`.
    It is read without running Git, which script tests may replace with fake commands on PATH.
    """
    try:
        line = (Path(root) / ".git").read_text().strip()
    except OSError:  # the main checkout's .git is a directory
        return None
    if not line.startswith("gitdir:"):
        return None
    private = Path(root) / line.removeprefix("gitdir:").strip()
    try:
        common = private / (private / "commondir").read_text().strip()
    except OSError:  # a submodule's .git file names a repository of its own
        return None
    # A worktree of a bare repository, such as a dispatched checkout on another Mac, has no main checkout.
    common = common.resolve()
    return common.parent if common.name == ".git" else None
