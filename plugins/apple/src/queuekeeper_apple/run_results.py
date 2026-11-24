"""Kept run results: each test's outcome and seconds, for a lane whose script runs xcodebuild itself.

    qk results keep --log LOG --bundle BUNDLE --name NAME --status STATUS [--results DIRECTORY]
    qk results history LANE TEST

A script calls `keep` once per run, on a pass as on a failure. It writes what `qk simulator-tests` keeps of
a run (matrix.kept), simulator-tests/NAME.log and NAME.json, in DIRECTORY, or else the lane's results/ that
`qk run` names in QUEUEKEEPER_RESULTS_DIR and brings back from another Mac, and prints one line per test case.
STATUS is the exit status of the script's xcodebuild. A lane's output goes to its log, so `qk run` prints
these lines again from results/ when the lane ends, here or, for a run on another Mac, once its results
have come back (report); it leaves out what `qk simulator-tests` kept, whose lanes print their own.
Use --activity-timing-prefix PREFIX to keep activities titled "PREFIX N s" under each test. The number
in the title measures the wait. An unreadable activity tree is reported separately from the test outcome.

It exits 0 once it has read the bundle's outcomes, whether or not a lane gave it a results directory; 3, the
status queuekeeper reads as unmeasured, when the bundle is missing, unreadable or holds no test case, which NAME.json's
bundleUnread and the output say; and 1 when nothing could be written or NAME is already kept in that directory.
A result bundle is never copied, and nothing is kept outside the lane's results/, whose rules bound it.

`history` prints each kept outcome of one test in a lane's retained results, then its flake history: its failure
rate over the runs that measured it, the share of commits at which one result file both failed and passed it across
attempts, and the flakes the merge queue proved for it (flakes.proven).
"""
import argparse
from collections import Counter, defaultdict
from functools import partial
import json
import os
from pathlib import Path
import re
import sys

from queuekeeper import lane_results, remote
from queuekeeper.parallel import RESULTS_VARIABLE
from . import flakes
from .simulators import matrix

# The order of a run's totals.
OUTCOMES = ("passed", "failed", "skipped", "expected failure", "unmeasured")


# The value of a kept record's keptBy that report prints.
KEPT_BY = "qk results keep"


def case_line(name, case):
    seconds = "no time" if case["seconds"] is None else f"{case['seconds']:.3f}s"
    reason = f" ({case['reason']})" if case.get("reason") else ""
    return f"{name}: {case['outcome']} {seconds} {case['test']}{reason}"


def activity_timings(bundle, title_pattern, test_id):
    """Read only the selected, named timings across a test's activity trees."""
    def walk(activities):
        for activity in activities:
            title = activity["title"]
            match = title_pattern.fullmatch(title)
            if match:
                yield {"title": title, "seconds": float(match[1])}
            yield from walk(activity.get("childActivities", []))

    result = matrix.bundle_json(bundle, "activities", "--test-id", test_id, timeout=30)
    try:
        return [timing for run in result["testRuns"] for timing in walk(run["activities"])]
    except (KeyError, TypeError):
        return None


def lines(record):
    """What to print of a kept record: a line per test case and the totals, or why no test's outcome is known."""
    name, status = record["run"], record["status"]
    if record.get("bundleUnread"):
        return [f"UNMEASURED {name}: no test's outcome is known, whatever xcodebuild's status {status} says: "
                f"{record['bundleUnread']}"]
    totals = Counter(case["outcome"] for case in record["tests"])
    found = []
    for case in record["tests"]:
        found.append(case_line(name, case))
        prefix = f"{name}: {case['test']}:"
        activities = case.get("activities", [])
        if activities is None:
            found.append(f"{prefix} activity timings could not be read")
        else:
            found.extend(f"{prefix} {activity['title']}" for activity in activities)
    found.append(f"{name}: " + ", ".join(f"{totals[outcome]} {outcome}" for outcome in OUTCOMES)
                 + f" (xcodebuild status {status})")
    return found


def report(results, out=print):
    """Print the records `keep` left under a lane's results/, a group's lanes' among them; never one another
    command kept."""
    for path in sorted(Path(results).rglob(f"{matrix.KEPT}/*.json")):
        try:
            record = json.loads(path.read_text())
            if record.get("keptBy") != KEPT_BY: continue
            found = lines(record)
        except (OSError, ValueError, KeyError, TypeError) as error:
            out(f"queuekeeper: {path} could not be read: {error!r}")
            continue
        out(f"queuekeeper: per-test results in {path}:")
        for line in found: out(line)


def keep(log, bundle, name, status, results=None, out=print, *, activity_prefixes=()):
    """Keep a run's outcomes as `kept` does and print them; return the exit status the module's text gives."""
    results = results if results is not None else os.environ.get(RESULTS_VARIABLE)
    if results and (Path(results) / matrix.KEPT / f"{name}.json").exists():
        print(f"queuekeeper results: {name} is already kept in {Path(results) / matrix.KEPT}; give each run a name of its own",
              file=sys.stderr, flush=True)
        return 1
    log, bundle = Path(log), Path(bundle)
    record = matrix.bundle_record(bundle) if bundle.is_dir() else None
    reader = None
    if activity_prefixes:
        prefixes = "|".join(map(re.escape, activity_prefixes))
        title_pattern = re.compile(rf"(?:{prefixes}) ([0-9]+(?:\.[0-9]+)?) s")
        reader = partial(activity_timings, bundle, title_pattern)
    cases = matrix.test_cases(record, reader) if record is not None else None
    unread = (f"no result bundle at {bundle}" if not bundle.is_dir() else
              f"{matrix.UNREAD}: {bundle}" if record is None else None if cases else f"{matrix.NO_CASES}: {bundle}")
    folder = matrix.kept(name, log, record, status, None, results, unread, KEPT_BY, cases=cases)
    if results and folder is None: return 1
    for line in lines({"run": name, "status": status, "tests": cases, "bundleUnread": unread}): out(line)
    out(f"{name}: kept in {folder}" if folder else f"{name}: not kept: no lane gave it a results directory ({RESULTS_VARIABLE} is unset)")
    return lane_results.UNMEASURED if unread is not None else 0


def rate(part, whole):
    return f"{100 * part / whole:.0f}%" if whole else "no rate"


def history(root, lane, test, out=print):
    """Print observations of an exact test ID, Xcode identifier URL or TARGET/SUITE/METHOD selector in one lane's
    retained results: each run's at each commit, a rerun at the same commit as an attempt of its own
    (remote.kept_runs). Then its flake history: how often it failed of the runs that measured it, at how many
    commits one result file both failed and passed it across attempts, and the flakes the merge queue proved,
    the tests that failed a gate and passed alone (flakes.proven, vocaro/queuekeeper#2)."""
    try: folders = remote.all_kept_results(remote.store(root)).get(lane, [])
    except (OSError, RuntimeError) as error:
        print(f"queuekeeper results: cannot read the project's retained results: {error}", file=sys.stderr)
        return 1
    outcomes, seen, names = defaultdict(Counter), defaultdict(lambda: defaultdict(set)), {test}
    for folder in folders:
        runs = remote.kept_runs(folder)
        for attempt, results in runs:
            # A commit with one kept run prints as it did before attempts were kept.
            where = folder.parent.name[:12] + (f" attempt {attempt}" if len(runs) > 1 or attempt > 1 else "")
            for path in sorted(results.rglob(f"{matrix.KEPT}/*.json")):
                try:
                    record = json.loads(path.read_text())
                    if not isinstance(record, dict): raise ValueError("expected a result record")
                    if record.get("keptBy") not in (KEPT_BY, "qk simulator-tests") or record.get("bundleUnread"): continue
                    file = str(path.relative_to(results))
                    found = [(case.get("identifierURL"), case["outcome"], case_line(file, case))
                             for case in record["tests"]
                             if test in (case["test"], case.get("identifierURL"), flakes.selector(case.get("identifierURL")))]
                except (OSError, ValueError, KeyError, TypeError) as error:
                    out(f"queuekeeper: {path} could not be read: {error!r}")
                    continue
                for identity, outcome, line in found:
                    key = identity if isinstance(identity, str) and identity else None
                    outcomes[key][outcome] += 1
                    # The same result file at one commit, across its attempts: the same device or shard, the same code.
                    seen[key][(folder.parent.name, file)].add(outcome)
                    names.add(flakes.selector(key) or test)
                    out(f"{where} {line}")
    separate = len(outcomes) > 1
    if not outcomes:
        out(f"{lane}: no retained result names {test}")
    elif separate:
        out(f"{lane}: retained observations for {test} have different recorded identities")
    for identity, counts in sorted(outcomes.items(), key=lambda item: item[0] or ""):
        heading = f"{lane} ({identity or 'without an identifier URL'})" if separate else lane
        out(f"{heading}: " + ", ".join(f"{counts[outcome]} {outcome}" for outcome in OUTCOMES))
        measured = counts["passed"] + counts["failed"]
        commits = {commit for commit, _ in seen[identity]}
        mixed = {commit for (commit, _), found in seen[identity].items() if {"passed", "failed"} <= found}
        out(f"{heading}: failed {counts['failed']} of {measured} measured runs ({rate(counts['failed'], measured)}); "
            f"failed and passed in one result file at {len(mixed)} of {len(commits)} commits "
            f"({rate(len(mixed), len(commits))})")
    out("Showing every kept attempt at each retained commit; a commit kept before attempts were kept shows only "
        "its latest run.")
    proved = [commit for name, commit in flakes.proven(root) if name in names]
    out(f"{test}: {len(proved)} flake{'' if len(proved) == 1 else 's'} the merge queue proved, failing a gate and "
        "passing alone on every rerun, in any lane" + (f": at {', '.join(proved)}" if proved else ""))
    return 0


def main(argv, project=None):
    parser = argparse.ArgumentParser(prog="qk results", description=__doc__.split("\n")[0],
                                     epilog=__doc__.split("\n\n", 2)[2], formatter_class=argparse.RawDescriptionHelpFormatter)
    actions = parser.add_subparsers(dest="action", required=True)
    keeping = actions.add_parser("keep", help="keep and print a test run's per-test outcomes and seconds")
    keeping.add_argument("--log", required=True, type=Path, help="the run's xcodebuild log")
    keeping.add_argument("--bundle", required=True, type=Path, help="the run's result bundle")
    keeping.add_argument("--name", required=True, help="the run's name in results/, such as iPhone-17-Pro")
    keeping.add_argument("--status", required=True, type=int, help="the exit status of the run's xcodebuild")
    keeping.add_argument("--results", type=Path, help=f"where to keep them (default: {RESULTS_VARIABLE}, the lane's results/)")
    keeping.add_argument("--activity-timing-prefix", action="append", default=[], metavar="PREFIX",
                         help='keep test activities titled "PREFIX N s" (repeatable)')
    historical = actions.add_parser("history", help="show a test's outcomes in a lane's retained Xcode results, and "
                                    "its flake history")
    historical.add_argument("lane", help="lane that kept the results")
    historical.add_argument("test", help="exact test ID, as printed by qk results keep, identifierURL from kept JSON, "
                            "or TARGET/SUITE/METHOD, as the merge queue names a flaky Xcode test")
    args = parser.parse_args(argv)
    if args.action == "history": return history(Path(project or ".").resolve(), args.lane, args.test)
    if not re.fullmatch(r"[\w.-]+", args.name): parser.error("--name must be a plain name")
    if any(not prefix.strip() for prefix in args.activity_timing_prefix):
        parser.error("--activity-timing-prefix must be nonempty")
    return keep(args.log, args.bundle, args.name, args.status, args.results, activity_prefixes=args.activity_timing_prefix)
