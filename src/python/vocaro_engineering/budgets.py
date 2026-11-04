"""The warnings that keep the split between the landing gate and the release gate (guides/releasing.md,
Keeping the split and Checkpoints, vocaro-engineering#135), which `eng land` prints after its result and
`eng doctor` gives:

- the gate's median wall time over its recent passing runs in the merge queue exceeds the gate lane's
  budgetMinutes, naming its slowest parts;
- origin/main holds more than release.maxCommits commits since the last commit that passed every lane
  in release.lanes, a release or a checkpoint;
- and, from eng doctor alone, a direct-main gate with no budget, and a gate that takes a physical device.

The queue's worker records each landed entry's gate run (Worker.gate): its wall time as the queue saw
it, wherever the lane ran, any wait for admission included, and the parts its log reports with their
times.
"""
import re
import statistics
import subprocess
from pathlib import Path

GATE = "gate"
# The passing gate runs the median is taken over, newest first, and the fewest that judge a budget.
RECENT_RUNS = 10
FEWEST_RUNS = 3
# The parts a landed entry keeps, and those a warning names.
KEPT_PARTS = 10
NAMED_PARTS = 5
DEFAULT_MAX_COMMITS = 100
# What eng unittest and eng xctest print for each process, "== NAME: OK (3 tests, 1.2s)", and what a
# lane group prints for each of its lanes under "Lane times (...)": "  NAME     12s  PASS".
SHARD = re.compile(r"^== (.+?): .*\((?:[^()]*, )?(\d+(?:\.\d+)?)s\)$")
LANE_TIME = re.compile(r"^  (\S+)\s+(\d+(?:\.\d+)?)s\s+(?:PASS|FAIL|UNMEASURED)$")
SPLIT = "guides/releasing.md, Keeping the split"
CHECKPOINTS = "guides/releasing.md, Checkpoints"


def parts(log):
    """The parts a lane's log reports with their times, slowest first, as [name, seconds]."""
    found, grouped = {}, False
    for line in log.splitlines():
        if line.startswith("Lane times ("):
            grouped = True
            continue
        matched = SHARD.match(line) or (LANE_TIME.match(line) if grouped else None)
        if matched: found[matched.group(1)] = found.get(matched.group(1), 0.0) + float(matched.group(2))
        elif grouped and not line.startswith("  "): grouped = False
    return sorted(([name, round(seconds, 1)] for name, seconds in found.items()), key=lambda part: -part[1])


def entries(directory):
    """The merge queue's entries in directory, as Queue.all reads them, without making the queue's folders."""
    import json
    found = []
    for path in sorted(Path(directory).glob("*.json")) if Path(directory).is_dir() else []:
        try: found.append(json.loads(path.read_text()))
        except (OSError, ValueError): continue
    return found


def runs(entries):
    """The gate runs that landed entries record, newest first, one per landed commit: a batch's entries
    share the one run that passed them."""
    seen, found = set(), []
    for entry in sorted(entries, key=lambda entry: entry.get("settled", 0), reverse=True):
        run = entry.get("gate")
        if entry.get("state") != "landed" or not isinstance(run, dict) or entry.get("landedAs") in seen: continue
        seen.add(entry.get("landedAs"))
        found.append(run)
    return found


def minutes(seconds):
    return f"{seconds / 60:.1f} min"


def gate_warnings(config, entries):
    """A warning when the median of the gate's recent passing runs exceeds its budget. None without a
    budget, or with fewer than FEWEST_RUNS runs to judge it by."""
    budget = config.get("lanes", {}).get(GATE, {}).get("budgetMinutes")
    if budget is None: return []
    recent = runs(entries)[:RECENT_RUNS]
    if len(recent) < FEWEST_RUNS: return []
    median = statistics.median(run["seconds"] for run in recent)
    if median <= budget * 60: return []
    slowest = next((run["parts"] for run in recent if run.get("parts")), [])[:NAMED_PARTS]
    named = (f"; its slowest parts in its latest run that reports them: "
             + ", ".join(f"{name} {seconds:g}s" for name, seconds in slowest)) if slowest \
        else "; its log reports no parts with their times"
    return [f"gate's median wall time over its last {len(recent)} passing runs in the merge queue is "
            f"{minutes(median)}, over its budget of {budget} min (gate.budgetMinutes){named}. Propose on the "
            f"project's issue tracker which check to move to release.lanes; the owner approves a move or a "
            f"higher budget ({SPLIT})"]


def lane_commands(config, name):
    lane = config.get("lanes", {}).get(name, {})
    if "parallel" in lane:
        return [command for member in lane["parallel"] for command in lane_commands(config, member)]
    return [lane["command"]] if "command" in lane else []


def takes_device(command):
    """Whether a command reserves a physical device: a --device option, or `devices take` or `reserve`."""
    if any(word == "--device" or word.startswith("--device=") for word in command): return True
    return any(word == "devices" and command[index + 1:index + 2] in (["take"], ["reserve"])
               for index, word in enumerate(command))


def doctor_warnings(config):
    """eng doctor's own: a direct-main gate with no budget, and a gate that takes a physical device."""
    lanes = config.get("lanes", {})
    if GATE not in lanes: return []
    warnings = []
    if config.get("integration", "direct-main") == "direct-main" and "budgetMinutes" not in lanes[GATE]:
        warnings.append(f"gate has no budgetMinutes, so nothing warns when it outgrows its time: set it in the gate "
                        f"lane from the gate's measured time ({SPLIT})")
    if any(takes_device(command) for command in lane_commands(config, GATE)):
        warnings.append(f"gate takes a physical device, which makes it a release check: move that check to a lane "
                        f"of its own named in release.lanes ({SPLIT})")
    return warnings


def git(root, *arguments):
    return subprocess.run(["git", "-C", str(root), *arguments], capture_output=True, text=True, timeout=120,
                          stdin=subprocess.DEVNULL)


def checkouts(root):
    """Every worktree of the repository, each with its own lane receipts in .build/engineering."""
    listed = git(root, "worktree", "list", "--porcelain")
    paths = [Path(line[len("worktree "):]) for line in listed.stdout.splitlines() if line.startswith("worktree ")]
    return paths or [Path(root)]


def passed_lanes(root, places, lanes, commit):
    """The lanes of lanes with a pass of the whole lane at commit, as eng release counts one."""
    from . import release
    found = []
    for lane in lanes:
        for place in places:
            try:
                release.lane_passed(place, lane, commit)
            except release.NoPass:
                continue
            found.append(lane)
            break
    return found


def candidates(root, places, lanes):
    """The commits any receipt of a release lane is of: the second Mac's, kept in the common Git directory,
    and each checkout's last run of each lane."""
    import json
    from . import remote
    found = set()
    store = remote.store(root)
    if store.is_dir():
        found |= {path.name for path in store.iterdir() if re.fullmatch(r"[0-9a-f]{40}", path.name)
                  and any((path / f"{lane}.json").is_file() for lane in lanes)}
    for place in places:
        for lane in lanes:
            try: revision = json.loads((place / ".build/engineering" / lane / "source.json").read_text())["project"]["revision"]
            except (OSError, ValueError, KeyError, TypeError): continue
            if isinstance(revision, str): found.add(revision)
    return found


def commit_warnings(root, config, main="refs/remotes/origin/main"):
    """A warning when main, as last fetched, holds more than release.maxCommits commits since the last
    commit that passed every lane in release.lanes. None without release lanes or a main to count."""
    lanes = [lane for lane in config.get("release", {}).get("lanes", []) if lane in config.get("lanes", {})]
    if not lanes: return []
    limit = config.get("release", {}).get("maxCommits", DEFAULT_MAX_COMMITS)
    listed = git(root, "rev-list", main)
    if listed.returncode: return []
    order = {commit: index for index, commit in enumerate(listed.stdout.split())}
    places = checkouts(root)
    full, partial = None, None
    # Newest first: the first that passed every lane ends the search, and a newer one that passed some is
    # the checkpoint still missing lanes.
    for commit in sorted((c for c in candidates(root, places, lanes) if c in order), key=order.get):
        passed = passed_lanes(root, places, lanes, commit)
        if len(passed) == len(lanes):
            full = commit
            break
        if passed and partial is None: partial = (commit, [lane for lane in lanes if lane not in passed])
    count = int(git(root, "rev-list", "--count", f"{full}..{main}" if full else main).stdout.strip() or 0)
    if count <= limit: return []
    since = f"since {full[:12]}, the last commit that passed every release lane" if full \
        else "and none since adoption passed every release lane"
    missing = (f"; {partial[0][:12]}, a newer commit, has not passed {', '.join(partial[1])}") if partial else ""
    return [f"origin/main holds {count} commits {since} ({', '.join(lanes)}), over release.maxCommits, {limit}"
            f"{missing}. Run a checkpoint: each lane in release.lanes on main's tip, with no version, changelog "
            f"edit or tag ({CHECKPOINTS})"]


def landing_warnings(root, config, entries):
    """What eng land prints after its result, and eng doctor warns of too: the gate over its budget, and
    main past its commit limit. A check that cannot be made says so rather than stopping the land."""
    warnings = []
    for check in (lambda: gate_warnings(config, entries), lambda: commit_warnings(root, config)):
        try: warnings += check()
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            warnings.append(f"could not check the gate's budget and the release commit limit: {error}")
    return warnings
