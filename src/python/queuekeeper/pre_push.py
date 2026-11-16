"""`qk pre-push`: a pre-push hook that runs the lanes the project's configuration names for what a push changes.

A project's pre-push hook is then one line, and holds no second copy of what its lanes and queue.unread say:

    #!/bin/sh
    exec python3 scripts/qk pre-push "$@"

Git gives the hook the remote's name and URL, and on standard input a line per ref pushed. The paths the push
changes pick the lanes, as they do for a landing: a push that changes only paths no code lane reads, by
queue.unread and its Markdown defaults, runs the docs lanes, and any other runs queue.lanes, or gate. The
lanes run with `qk run` in this checkout, so they are admitted and placed as always.

A push that changes no path runs nothing and says so: its files are ones the remote already has, as when a merge
keeps a landed branch's commits and has the remote's own tree (vocaro/queuekeeper#165). Paths that Git could not
list are not such a push, and run queue.lanes, or gate.

The push is refused when a lane fails, exit 1, and when one ends unmeasured, exit 3: nothing ran, which is not a
pass. QUEUEKEEPER_PREPUSH=skip runs nothing and says so, as `git push --no-verify` does without a word.

The merge queue runs the lanes itself before it pushes, so under its worker the hook runs nothing (QUEUE_VARIABLE).
A project need not list queue.hookCoveredBy for it.

Moved from the tier choice in vocaro/mpm's tools/repository/hooks/pre-push (vocaro/queuekeeper#6)."""
import os
from pathlib import Path
import subprocess
import sys

from . import git_state, lane_results
from .config import TOOLKIT_ROOT

ZERO = "0" * 40
# Set to skip to run nothing.
VARIABLE = "QUEUEKEEPER_PREPUSH"
# Set by the merge queue's worker around the project's hook: the worker has run the lanes for what it pushes.
QUEUE_VARIABLE = "QUEUEKEEPER_QUEUE_PUSH"
FAILED = 1


def say(line):
    print(f"queuekeeper pre-push: {line}", file=sys.stderr, flush=True)


def pushed(root, lines, remote="origin", git=git_state.git):
    """(the commits pushed, the paths they change) for the hook's input. A ref that is deleted pushes nothing. A new
    branch is compared with the remote's main, and one with no such base changes every file it has. Both sides of
    a rename are paths. The paths are None when Git could not list a ref's, which is not a push that changes none."""
    commits, changed, unread = [], set(), False
    for line in lines:
        fields = line.split()
        if len(fields) != 4 or set(fields[1]) == {"0"}: continue
        _, local, _, there = fields
        commits.append(local)
        base = there if set(there) != {"0"} and git(root, "cat-file", "-e", f"{there}^{{commit}}") is not None else None
        for upstream in (f"refs/remotes/{remote}/main", git_state.UPSTREAM):
            base = base or git(root, "merge-base", local, upstream)
        listed = (git(root, "diff", "--name-only", "--no-renames", base, local) if base
                  else git(root, "ls-tree", "-r", "--name-only", local))
        unread = unread or listed is None
        changed |= set((listed or "").splitlines())
    return commits, None if unread else sorted(changed)


def lanes_for(config, changed):
    """The lanes a push that changes these paths runs: the docs lanes for a docs-only one, else the gate's, with the
    docs lanes first for one that changes documents too, as the queue runs them (merge_queue.docs_also)."""
    from . import merge_queue
    docs = merge_queue.docs_lanes(config, changed)
    if docs: return docs, "it changes only documentation paths"
    queue = config.get("queue", {})
    named = list(queue.get("lanes") or (["gate"] if "gate" in config.get("lanes", {}) else []))
    also = merge_queue.docs_also(config, changed, named) if named else []
    if also: return also + named, "it changes code paths and documentation paths"
    return named, "it changes code paths"


def run(root, lane):
    """The exit status of `qk run LANE` in root, outside the environment Git gives a hook."""
    command = [sys.executable, str(TOOLKIT_ROOT / "bin/qk"), "--project", str(root), "run", lane]
    return subprocess.run(command, cwd=root, env=git_state.environment(), stdin=subprocess.DEVNULL).returncode


def main(root, config, arguments, stream=None, environ=None, run=run):
    environ = os.environ if environ is None else environ
    if environ.get(VARIABLE) == "skip":
        say(f"skipped ({VARIABLE}=skip): nothing was verified")
        return 0
    if environ.get(QUEUE_VARIABLE):
        say("the merge queue ran the lanes for this push; nothing more runs")
        return 0
    remote = arguments[0] if arguments else "origin"
    commits, changed = pushed(root, (sys.stdin if stream is None else stream).read().splitlines(), remote)
    if not commits: return 0
    if changed == []:
        say("nothing runs: the push changes no path, so its files are ones the remote already has")
        return 0
    unread, changed = changed is None, changed or []
    lanes, why = lanes_for(config, changed)
    if not lanes:
        say("the project has no gate lane and no queue.lanes, so nothing was verified")
        return 0
    head = git_state.git(root, "rev-parse", "HEAD")
    if head not in commits:
        say(f"the lanes run on this checkout, at {(head or 'no commit')[:12]}, which is not the commit pushed "
            f"({', '.join(commit[:12] for commit in commits)})")
    if unread: why = "the paths it changes could not be read, so it runs as a code change"
    else: why = f"{why} ({len(changed)} path(s))"
    say(f"running {', '.join(lanes)}: {why}")
    for lane in lanes:
        status = run(Path(root), lane)
        if status == lane_results.UNMEASURED:
            say(f"BLOCKED: {lane} was unmeasured, so nothing was verified; that is not a red tree, and not a green "
                f"one. Run it again, or push anyway with {VARIABLE}=skip git push")
            return lane_results.UNMEASURED
        if status:
            say(f"BLOCKED: {lane} failed (exit {status}). To push anyway: {VARIABLE}=skip git push")
            return FAILED
    return 0
