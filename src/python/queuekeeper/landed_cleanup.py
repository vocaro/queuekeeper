"""What a landing leaves behind: the local branch it came from, deleted once proven landed, and the linked worktrees
that held landed work, reported and never removed.

A session that landed used to close out by hand: delete its branch, check by patch-id that origin/main holds its
commits, and look through its worktree for evidence before letting it go. `branches` does the first two, by jjq's
rule (`cleanup_residue_inner`, BSD-2-Clause): only a candidate the queue recorded is touched, every deletion is
logged with how to restore it, and anything uncertain is kept, saying why. A local branch is deleted only when all
of these hold:

- the queue's newest entry from it has landed, and origin/main, as last fetched, holds the commit it landed as;
- its tip is exactly the tip that entry queued (or, for a branch refreshed by merges of main, the tip it replayed),
  or the branch has no merge of its own and every commit of it that origin/main lacks has an equal patch there
  (`git cherry`), since the queue rebases what it lands;
- it is not main, master or origin's default branch, and no worktree has it checked out, or is rebasing or
  bisecting it.

It is deleted with `git update-ref -d` naming the tip that was checked, so a branch that moved meanwhile stays.

`worktrees` lists each linked worktree whose branch landed by that same rule, or that is detached at a commit
origin/main holds, and says whether it is disposable: no change, untracked file or operation under way, no
process with its working directory, an open file or an argument inside it, and no ignored file but caches. Results,
receipts, logs and captures under build folders are evidence, and `git worktree remove` would delete them without
a word, so nothing here removes a worktree: its disposal goes through the project's documented path. A locked
worktree is left out: the queue worker's checkout, a retired one, and qk land's scratch worktrees, which are
worktree_ownership's.
"""
import os
from pathlib import Path

from . import disk, worktree_ownership as wo

MAIN = wo.MAIN
LANDED = "landed"
DELETED, KEPT = "deleted", "kept"
PROTECTED = {"main", "master", "HEAD"}
# What a worktree's own Git directory names while a branch is checked out, rebased or bisected there.
HOLDING = ("HEAD", "rebase-merge/head-name", "rebase-apply/head-name", "BISECT_START")
# Ignored files that hold nothing but what a tool makes again. Anything else ignored may be evidence.
CACHE_NAMES = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".DS_Store", ".hypothesis"}
CACHE_SUFFIXES = (".pyc", ".pyo")
SHOWN = 5


def newest_by_branch(entries):
    """The newest entry the queue holds from each branch that has ever landed one, by branch name."""
    landed = {entry.get("branch") for entry in entries if entry.get("state") == LANDED}
    newest = {}
    for entry in entries:
        branch = entry.get("branch")
        if branch not in landed or not branch: continue
        if branch not in newest or (entry.get("submitted") or 0) >= (newest[branch].get("submitted") or 0):
            newest[branch] = entry
    return newest


def protected(root, git):
    """The branch names never deleted: main, master and the branch origin's HEAD names."""
    names = set(PROTECTED)
    found = git(root, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if found.returncode == 0 and found.stdout.strip().startswith("origin/"):
        names.add(found.stdout.strip()[len("origin/"):])
    return names


def holders(root, git):
    """{ref: worktree path} for each branch some worktree of root's repository has checked out, or is rebasing or
    bisecting, read from each worktree's own Git directory, as Git's own refusal to delete such a branch reads it."""
    common = wo.common_dir(root, git)
    listed = git(root, "worktree", "list", "--porcelain")
    main = next((line[len("worktree "):] for line in listed.stdout.splitlines() if line.startswith("worktree ")),
                str(common.parent))
    places = [(common, main), *((Path(each["admin"]), each["path"]) for each in wo.registrations(common))]
    found = {}
    for directory, path in places:
        for name in HOLDING:
            try: value = (Path(directory) / name).read_text().strip()
            except OSError: continue
            if value.startswith("ref: "): value = value[len("ref: "):]
            if name == "BISECT_START" and value and not value.startswith("refs/"): value = f"refs/heads/{value}"
            if value.startswith("refs/heads/"): found.setdefault(value, path)
    return found


def landed_content(root, branch, tip, entry, git):
    """(how, None) when the branch at tip holds nothing origin/main lacks, as entry's landing proves it: "exact"
    for the tip the entry recorded, "contained" for a tip origin/main holds, or "patch-equivalent"; else
    (None, why it stays)."""
    if entry.get("state") != LANDED:
        return None, f"its newest entry, {entry.get('id')}, is {entry.get('state')}"
    landed = entry.get("landedAs")
    if not landed or git(root, "merge-base", "--is-ancestor", landed, MAIN).returncode:
        return None, f"origin/main as last fetched does not hold {entry.get('id')}'s landed commit {str(landed)[:12]}"
    if tip in {entry.get("tip"), entry.get("replays")} - {None}: return "exact", None
    if git(root, "merge-base", "--is-ancestor", tip, MAIN).returncode == 0: return "contained", None
    merges = git(root, "rev-list", "--merges", f"{MAIN}..{tip}")
    if merges.returncode: return None, f"its history cannot be read: {merges.stderr.strip() or 'no output'}"
    if merges.stdout.split():
        return None, (f"it moved from the tip {entry.get('id')} queued, and has merges origin/main lacks, which no "
                      "patch comparison can prove landed")
    compared = git(root, "cherry", MAIN, tip)
    if compared.returncode:
        return None, f"its commits cannot be compared with origin/main: {compared.stderr.strip() or 'no output'}"
    missing = [line[2:].strip() for line in compared.stdout.splitlines() if line.startswith("+ ")]
    if missing:
        return None, (f"{len(missing)} commit(s) of it, {missing[0][:12]} first, have no equal patch on origin/main")
    return "patch-equivalent", None


def judge(root, branch, entry, places, names, git):
    """What deleting branch would be: a result dict with outcome DELETED (meaning it may go) or KEPT, or None when
    the branch is not there."""
    ref = f"refs/heads/{branch}"
    found = git(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    if found.returncode: return None
    tip = found.stdout.strip()
    result = {"branch": branch, "tip": tip, "entry": entry.get("id")}
    if branch in names: return {**result, "outcome": KEPT, "why": f"{branch} is a main branch, which is never deleted"}
    if ref in places: return {**result, "outcome": KEPT, "why": f"it is checked out at {places[ref]}"}
    how, why = landed_content(root, branch, tip, entry, git)
    if why: return {**result, "outcome": KEPT, "why": why}
    return {**result, "outcome": DELETED, "how": how,
            "why": {"exact": f"its tip is the one {entry['id']} queued, which landed as {entry['landedAs'][:12]}",
                    "contained": "origin/main holds its tip",
                    "patch-equivalent": "every commit of it has an equal patch on origin/main"}[how],
            "restore": ["git", "branch", branch, tip]}


def branches(root, entries, *, only=None, dry_run=False, emit=None, git=wo.run_git):
    """Delete each local branch the queue's entries prove landed, as this module's docstring says, or with only,
    that branch alone; return a result dict for each branch looked at that is still there. With dry_run nothing is
    deleted, and DELETED says what would be. emit(kind, **fields) journals each deletion."""
    newest = newest_by_branch(entries)
    if only is not None: newest = {only: newest[only]} if only in newest else {}
    if not newest: return []
    places, names = holders(root, git), protected(root, git)
    results = []
    for branch, entry in sorted(newest.items()):
        result = judge(root, branch, entry, places, names, git)
        if result is None: continue
        if result["outcome"] == DELETED and not dry_run:
            done = git(root, "update-ref", "-d", f"refs/heads/{branch}", result["tip"])
            if done.returncode:
                result = {**result, "outcome": KEPT, "why": f"`git update-ref -d` refused: "
                                                             f"{done.stderr.strip() or 'no output'}"}
                result.pop("restore", None)
            elif emit: emit("branch.deleted", branch=branch, tip=result["tip"], entry=result["entry"], how=result["how"])
        results.append(result)
    return results


def branch_words(result, dry_run=False):
    if result["outcome"] == DELETED:
        return (f"{'would delete' if dry_run else 'deleted'} branch {result['branch']} at {result['tip'][:12]}: "
                f"{result['why']}" + ("" if dry_run else f"; restore with `{' '.join(result['restore'])}`"))
    return f"kept branch {result['branch']}: {result['why']}"


def cache(path):
    parts = Path(path.rstrip("/")).parts
    return bool(parts) and (any(part in CACHE_NAMES for part in parts) or parts[-1].endswith(CACHE_SUFFIXES))


def contents(path, git):
    """(changes, ignored, why) for the worktree at path: its changed and untracked paths, its ignored paths, and why
    they cannot be read or an operation is under way, or None."""
    # Folders collapsed, so that a build folder of thousands of ignored files is one line.
    status = git(path, "status", "--porcelain=v1", "--ignored=traditional", "--untracked-files=normal")
    if status.returncode: return [], [], f"its status cannot be read: {status.stderr.strip() or 'no output'}"
    changes, ignored = [], []
    for line in status.stdout.splitlines():
        (ignored if line.startswith("!! ") else changes).append(line[3:])
    for name in wo.IN_PROGRESS:
        found = git(path, "rev-parse", "--git-path", name)
        if found.returncode: return changes, ignored, "whether an operation is under way cannot be read"
        named = Path(found.stdout.strip())
        if not named.is_absolute(): named = Path(path) / named
        if named.exists(): return changes, ignored, f"an operation is under way ({name})"
    return changes, ignored, None


def worktrees(root, entries, *, census=None, git=wo.run_git):
    """Each linked worktree that holds only landed work, as this module's docstring says, as a dict: path, branch
    (None when detached), head, entry (the landed entry that proves it, for a branch), disposable, why (what keeps
    it, empty when disposable) and evidence (ignored paths other than caches, the first few, with evidenceCount)."""
    common = wo.common_dir(root, git)
    newest = newest_by_branch(entries)
    census = census or wo.Census()
    found = []
    for each in wo.registrations(common):
        # A locked worktree is a tool's or the owner's, said to stay: the queue worker's checkout, a retired one
        # `qk disk` lists, qk land's scratch worktrees, which worktree_ownership recovers.
        if each["reason"] is not None: continue
        path = each["path"]
        if not os.path.isdir(path): continue
        head = each["head"]
        if head.startswith("ref: refs/heads/"):
            branch = head[len("ref: refs/heads/"):]
            if branch not in newest: continue
            tip = git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}")
            if tip.returncode: continue
            commit = tip.stdout.strip()
            how, _ = landed_content(root, branch, commit, newest[branch], git)
            if how is None: continue
            row = {"path": path, "branch": branch, "head": commit, "entry": newest[branch]["id"]}
        elif head and not head.startswith("ref: "):
            if git(root, "merge-base", "--is-ancestor", head, MAIN).returncode: continue
            row = {"path": path, "branch": None, "head": head, "entry": None}
        else: continue
        why = []
        changes, ignored, trouble = contents(path, git)
        if trouble: why.append(trouble)
        if changes: why.append(f"it holds {len(changes)} change(s) or untracked file(s), such as {changes[0]}")
        evidence = [item for item in ignored if not cache(item)]
        if evidence:
            why.append(f"it holds {len(evidence)} ignored path(s) other than caches, such as {evidence[0]}, which may "
                       "be evidence")
        try: users = census.users(path)
        except RuntimeError as error: why.append(str(error))
        else:
            if users: why.append(disk.described((users[0], "working directory, an open file or an argument")))
        found.append({**row, "disposable": not why, "why": why, "evidence": evidence[:SHOWN],
                      "evidenceCount": len(evidence)})
    return found


def worktree_words(row):
    what = f"on {row['branch']}, landed by {row['entry']}" if row["branch"] else \
        f"detached at {row['head'][:12]}, which origin/main holds"
    if row["disposable"]:
        return (f"worktree {row['path']} ({what}) is disposable: nothing removed it; dispose of it by the project's "
                "documented path")
    return f"worktree {row['path']} ({what}) is kept: {'; '.join(row['why'])}"
