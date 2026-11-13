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

Moved from vocaro/mpm's tools/repository/install-hooks.sh (vocaro/queuekeeper#6)."""
from pathlib import Path

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


def check(root, git=git_state.git):
    """The warnings for the checkout at root, after repairing what can be repaired. Each repair is a warning too,
    so that the change is said."""
    root = Path(root).resolve()
    if git(root, "rev-parse", "--show-toplevel") is None or not git(root, "config", "--get", KEY): return []
    checkouts = [Path(entry["worktree"]) for entry in git_state.worktrees(root)]
    found = []
    # Without extensions.worktreeConfig, --worktree reads and writes the repository's own value: asking it there
    # would take the shared value for an override, and clearing that would remove the installation.
    separate = git(root, "config", "--bool", "--get", "extensions.worktreeConfig") == "true"
    shared = git(root, "config", "--local", "--get", KEY)
    own = git(root, "config", "--worktree", "--get", KEY) if separate else None
    wanted = relative(shared, root, checkouts) if shared else None
    # A person's own hooks, set for every repository or outside this one, are not the project's.
    if own is None and (shared is None or wanted is None): return []
    if shared and Path(shared).is_absolute() and wanted is not None:
        if (root / wanted).is_dir() and git(root, "config", "--local", KEY, str(wanted)) is not None:
            found.append(f"repaired {KEY}: the repository's value was the absolute {shared}, which ran one "
                         f"checkout's hooks for every worktree, and is now {wanted}, each worktree's own")
            shared = str(wanted)
        else:
            found.append(f"{KEY} is the absolute {shared}, so every worktree runs that one checkout's hooks and not "
                         f"its own branch's; this checkout has no {wanted} to point it at instead")
    if own is not None:
        mine = relative(own, root, checkouts)
        target = mine if shared is None else wanted
        if target is not None and (shared is None or mine == wanted) and (root / target).is_dir():
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
    folder = root / effective  # an absolute value stays itself
    if not folder.is_dir():
        return found + [f"{KEY} names {effective}, which does not exist here: Git skips a missing hooks folder "
                        "without a word, so a push by hand from this checkout runs no hook and succeeds"]
    for name in HOOKS:
        hook = folder / name
        if hook.is_file() and not hook.stat().st_mode & 0o111:
            found.append(f"{effective}/{name} is not executable, so Git skips it without a word: chmod +x it")
    return found
