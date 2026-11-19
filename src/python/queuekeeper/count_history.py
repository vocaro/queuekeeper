"""A lane's executed-test count, held against the count of its last passing whole run on main, so that a gate
that silently lost a bundle, a module or a file's tests fails without a floor typed by hand (doc/guides/lanes.md,
Test counts from history).

Every judged whole run of a lane is kept by commit in the repository's Git directory (remote.keep_here, and
remote.execute for a run on another Mac), and its lane's receipt records `tests`, the counts its log reports. A whole
run is held against the newest such run that passed at a commit of main below it on the same Mac: a Mac whose tests
skip apart from another's has its own count. It fails when it executed fewer, unless a commit after that run says
`Tests-Removed: LANE`, or `*` for every lane. Additions need nothing. A run with arguments selects part of the lane
and is compared with no whole run.

Between 2026-10-01 and 2026-11-18 the projects committed about 100 edits to their lanes' minimumTests, 30 of them
Pagepour's, the number following the tests rather than guarding them.
"""
import json
from pathlib import Path
import subprocess

# The trailer by which a commit says its lane's count falls on purpose: `Tests-Removed: gate` or `Tests-Removed: *`.
TRAILER = "Tests-Removed"
EVERY_LANE = "*"
# How many commits of main below a run are searched for its last passing run, newest first.
DEPTH = 200
# The branch a run's history is read on, as the merge queue lands it: origin's first, else the local one.
MAINS = ("refs/remotes/origin/main", "refs/heads/main")


def counts(log_counts):
    """A receipt's `tests` for a log's (ran, skipped), as lane_results.counted reads them: the tests executed, which
    leave out the skipped, and those skipped; or None when the log reports no count."""
    if log_counts is None: return None
    ran, skipped = log_counts
    return {"executed": ran - skipped, "skipped": skipped}


def lane_receipt(kept):
    """The lane's own receipt within a run kept by commit: under result.receipt, for one kept here and one from another
    Mac alike."""
    result = kept.get("result") if isinstance(kept, dict) else None
    inner = result.get("receipt") if isinstance(result, dict) else None
    return inner if isinstance(inner, dict) else {}


def passing(kept, commit, lane, here):
    """Whether a run kept by commit is a pass of the whole lane at exactly commit: run with no arguments, not on a
    snapshot or a snapshot of the toolkit, nor a group's serial reference, and, for a run on this Mac (ranOn here),
    judged passed, which a zero exit that ran too few tests is not."""
    if not isinstance(kept, dict): return False
    result = kept.get("result") if isinstance(kept.get("result"), dict) else {}
    whole = kept.get("commit") == commit and kept.get("lane") == lane and not kept.get("arguments") \
        and not kept.get("snapshot") and not kept.get("toolkitSnapshot") and not kept.get("serial")
    judged = kept.get("ranOn") != here or result.get("outcome") == "passed"
    return whole and judged and result.get("exitStatus") == 0 and not result.get("stopped")


def git(root, *arguments):
    """git's output in root, or None when it fails: a run with no history to read is held against none."""
    try:
        done = subprocess.run(["git", "-C", str(root), *arguments], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def below(root, commit):
    """The commits of main at or below commit, newest first along main's first parents: those of a branch not yet
    landed are left out, since a run there passed no merge queue's check of its count."""
    for main in MAINS:
        base = git(root, "merge-base", commit, main)
        if base: return (git(root, "rev-list", "--first-parent", f"--max-count={DEPTH}", base) or "").split()
    return []


class Baseline:
    def __init__(self, commit, executed, mac, path):
        self.commit, self.executed, self.mac, self.path = commit, executed, mac, path


def baseline(root, store, lane, mac, commit, here):
    """The newest passing whole run of the lane at a commit of main at or below commit, on the Mac named mac, that
    recorded its count, as a Baseline, or None. A kept run written before counts were recorded is passed over."""
    if not mac or not commit: return None
    for older in below(root, commit):
        path = Path(store) / older / f"{lane}.json"
        try: kept = json.loads(path.read_text())
        except (OSError, ValueError): continue
        if not passing(kept, older, lane, here): continue
        inner = lane_receipt(kept)
        executed = (inner.get("tests") or {}).get("executed") if isinstance(inner.get("tests"), dict) else None
        if inner.get("mac") == mac and isinstance(executed, int) and not isinstance(executed, bool):
            return Baseline(older, executed, mac, path)
    return None


def acknowledged(root, since, commit, lane):
    """Whether a commit after since, up to commit, says `Tests-Removed:` the lane, or every lane."""
    said = git(root, "log", f"--format=%(trailers:key={TRAILER},valueonly,separator=%x2C)", f"{since}..{commit}")
    named = {name.strip() for line in (said or "").splitlines() for name in line.split(",")}
    return lane in named or EVERY_LANE in named


def dropped(root, store, lane, receipt, here, say=None):
    """Why a whole run of the lane whose own receipt is receipt failed, having executed fewer tests than its last
    passing whole run on main on the same Mac, or None when it did not, or no such run recorded a count. A run whose
    log reports no count at all is held as one that executed none. say, if given, is told of a fall a commit's
    trailer acknowledged."""
    tests = receipt.get("tests") if isinstance(receipt.get("tests"), dict) else {}
    executed = tests.get("executed", 0)
    commit = (receipt.get("project") or {}).get("revision")
    found = baseline(root, store, lane, receipt.get("mac"), commit, here)
    if found is None or executed >= found.executed: return None
    fewer = found.executed - executed
    words = (f"{lane} executed {executed} tests, {fewer} fewer than the {found.executed} its last passing whole run on "
             f"{found.mac} executed at {found.commit[:12]} ({found.path})")
    if acknowledged(root, found.commit, commit, lane):
        if say: say(f"{words}, which a commit since says with {TRAILER}: {lane}")
        return None
    return (f"{words}. If tests were removed on purpose, add the trailer `{TRAILER}: {lane}` to a commit after "
            f"{found.commit[:12]}; otherwise find what stopped running, such as a bundle, module or file the lane no "
            f"longer selects or whose tests now skip")
