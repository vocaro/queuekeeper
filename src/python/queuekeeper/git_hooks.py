"""Which hooks a push by hand from this checkout runs, and the repair `qk doctor` makes of it.

A project that tracks its hooks points core.hooksPath at their folder. Git resolves a relative path in whichever
worktree runs the hook, so each worktree runs its own branch's copy. Two things break that, and neither says so:

- An absolute path aims every worktree at one checkout's hooks. A branch that edits a hook is then pushed under
  the main checkout's older copy.
- A linked worktree can carry a value of its own (extensions.worktreeConfig), which beats the repository's. Claude
  Code writes one when it creates a worktree: the repository's relative value, made absolute against the main
  checkout (vocaro/mpm, audited 2026-08-22).

And Git skips a hooks folder that does not exist without a word, and the push succeeds: an ungated push that looks
like a gated one. So `doctor` puts the path back to the relative one, in this worktree and in the repository, and
warns of what it cannot fix. It changes nothing in a project with no core.hooksPath, or with one outside the
repository, such as a person's own global hooks.

A fresh clone has no core.hooksPath at all, and so runs no hook. A project names its hooks folder in queuekeeper.json's
gitHooks, and `doctor` then sets the path where it is unset. Without that key, a tracked pre-push hook in a hooks
folder that runs `qk pre-push` is the project's own, and `doctor` says that the path is unset, changing nothing
(vocaro/queuekeeper#131).

Moved from vocaro/mpm's tools/repository/install-hooks.sh (vocaro/queuekeeper#6)."""
from pathlib import Path
import re

from . import git_state

KEY = "core.hooksPath"
# The hooks whose file Git skips silently when it is not executable.
HOOKS = ("pre-push", "pre-commit", "commit-msg", "post-checkout", "post-merge")


def within(path, checkouts):
    """path relative to the checkout of checkouts that holds it, or None when none does. The longest checkout
    is tried first, as a linked worktree may sit inside the main checkout."""
    for checkout in sorted(checkouts, key=lambda found: len(str(found)), reverse=True):
        try: return Path(path).relative_to(checkout)
        except ValueError: continue
    return None


def relative(value, root, checkouts):
    """What value, a core.hooksPath, is relative to a checkout: itself when relative already, or its part below
    the worktree of the repository that holds it. None for a path outside every worktree."""
    if not Path(value).is_absolute(): return Path(value)
    found = within(value, [Path(root), *checkouts])
    return found if found is not None and str(found) != "." else None


# A hook that hands the push to Queuekeeper, as the one-line hook the merge queue's setup gives does.
RUNS_QK = re.compile(r"\bqk\s+pre-push\b")


def tracked_hooks(root, git=git_state.git):
    """The folder of a tracked pre-push hook that runs `qk pre-push`, in a folder whose name says hooks, or None."""
    for path in (git(root, "ls-files", "--", "pre-push", "*/pre-push") or "").splitlines():
        folder = Path(path).parent
        if "hooks" not in folder.name.lower(): continue
        try: text = (root / path).read_text(errors="replace")
        except OSError: continue
        if RUNS_QK.search(text): return folder
    return None


def unset(root, declared, git=git_state.git, *, repair=True):
    """The warnings for a checkout with no core.hooksPath anywhere, as a fresh clone has, after setting the one
    declared, the project's gitHooks."""
    if declared:
        if not (root / declared).is_dir():
            return [f"{KEY} is unset and queuekeeper.json's gitHooks names {declared}, which does not exist here: "
                    "a push by hand from this checkout runs no hook and succeeds"]
        if not repair:
            return [f"{KEY} is unset, so a push by hand from this checkout runs no hook: "
                    f"run `git config {KEY} {declared}`"]
        if git(root, "config", "--local", KEY, declared) is not None:
            return [f"repaired {KEY}: it was unset, so a push by hand from this checkout ran no hook, and is now "
                    f"{declared}, the folder queuekeeper.json's gitHooks names"]
        return [f"{KEY} is unset, so a push by hand from this checkout runs no hook, and setting it to {declared} "
                f"failed: run `git config {KEY} {declared}`"]
    folder = tracked_hooks(root, git)
    if folder is None: return []
    return [f"{KEY} is unset, so a push by hand from this checkout runs no hook and succeeds, though {folder}/pre-push "
            f"runs qk pre-push: run `git config {KEY} {folder}`, or name {folder} in queuekeeper.json's gitHooks so "
            "that doctor sets it"]


def differs(effective, declared, root, checkouts):
    """A warning when core.hooksPath, effective, is not the folder the project declares; none when it declares no
    folder."""
    if not declared or not effective or relative(effective, root, checkouts) == Path(declared): return []
    return [f"{KEY} is {effective}, but queuekeeper.json's gitHooks names {declared}: a push by hand from this "
            f"checkout runs no hook of the project's; run `git config {KEY} {declared}` if it should"]


def check(root, declared=None, git=git_state.git, *, repair=True):
    """The warnings for the checkout at root, after repairing what can be repaired. Each repair is a warning too,
    so that the change is said, and begins with "repaired". declared is the project's gitHooks, the folder of its
    tracked hooks, or None."""
    root = Path(root).resolve()
    if git(root, "rev-parse", "--show-toplevel") is None: return []
    if not git(root, "config", "--get", KEY): return unset(root, declared, git, repair=repair)
    checkouts = [Path(entry["worktree"]) for entry in git_state.worktrees(root)]
    found = []
    # Without extensions.worktreeConfig, --worktree reads and writes the repository's own value: asking it there
    # would take the shared value for an override, and clearing that would remove the installation.
    separate = git(root, "config", "--bool", "--get", "extensions.worktreeConfig") == "true"
    shared = git(root, "config", "--local", "--get", KEY)
    own = git(root, "config", "--worktree", "--get", KEY) if separate else None
    wanted = relative(shared, root, checkouts) if shared else None
    # A person's own hooks, set for every repository or outside this one, are not the project's.
    if own is None and (shared is None or wanted is None):
        return differs(git(root, "config", "--get", KEY), declared, root, checkouts)
    if shared and Path(shared).is_absolute() and wanted is not None:
        if not repair:
            found.append(f"{KEY} is the absolute {shared}, so every worktree runs that one checkout's hooks: "
                         f"use the relative path {wanted} to run each worktree's own hooks")
        elif (root / wanted).is_dir() and git(root, "config", "--local", KEY, str(wanted)) is not None:
            found.append(f"repaired {KEY}: the repository's value was the absolute {shared}, which ran one "
                         f"checkout's hooks for every worktree, and is now {wanted}, each worktree's own")
            shared = str(wanted)
        else:
            found.append(f"{KEY} is the absolute {shared}, so every worktree runs that one checkout's hooks and not "
                         f"its own branch's; this checkout has no {wanted} to point it at instead")
    if own is not None:
        mine = relative(own, root, checkouts)
        target = mine if shared is None else wanted
        if repair and target is not None and (shared is None or mine == wanted) and (root / target).is_dir():
            # The override names the same folder, in another checkout or as an absolute path.
            if shared is None: git(root, "config", "--local", KEY, str(target))
            if git(root, "config", "--worktree", "--unset-all", KEY) is not None:
                found.append(f"repaired {KEY}: this worktree's own value, {own}, hid the repository's and is "
                             f"cleared; a push from here now runs {target}, its own copy")
                own = None
        if own is not None:
            found.append(f"this worktree has its own {KEY}, {own}, which hides the repository's "
                         f"{shared or 'unset value'}: clear it with `git config --worktree --unset-all {KEY}` if a "
                         "push from here should run the project's hooks")
    effective = git(root, "config", "--get", KEY)
    if not effective: return found
    found += differs(effective, declared, root, checkouts)
    folder = root / effective  # an absolute value stays itself
    if not folder.is_dir():
        return found + [f"{KEY} names {effective}, which does not exist here: Git skips a missing hooks folder "
                        "without a word, so a push by hand from this checkout runs no hook and succeeds"]
    for name in HOOKS:
        hook = folder / name
        if hook.is_file() and not hook.stat().st_mode & 0o111:
            found.append(f"{effective}/{name} is not executable, so Git skips it without a word: chmod +x it")
    return found
