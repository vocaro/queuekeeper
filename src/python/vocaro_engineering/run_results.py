"""Kept run results: each test's outcome and seconds, for a lane whose script runs xcodebuild itself.

    eng results keep --log LOG --bundle BUNDLE --name NAME --status STATUS [--results DIRECTORY]

A script calls `keep` once per run, on a pass as on a failure. It writes what `eng simulator-tests` keeps of
a run (matrix.kept), simulator-tests/NAME.log and NAME.json, in DIRECTORY, or else the lane's results/ that
`eng run` names in ENG_RESULTS_DIR and brings back from the second Mac, and prints one line per test case.
STATUS is the exit status of the script's xcodebuild. A lane's output goes to its log, so `eng run` prints
these lines again from results/ when the lane ends, here or, for a run on the second Mac, once its results
have come back (report); it leaves out what `eng simulator-tests` kept, whose lanes print their own.

It exits 0 once it has read the bundle's outcomes, whether or not a lane gave it a results directory; 3, the
status eng reads as unmeasured, when the bundle is missing, unreadable or holds no test case, which NAME.json's
bundleUnread and the output say; and 1 when nothing could be written or NAME is already kept in that directory.
A result bundle is never copied, and nothing is kept outside the lane's results/, whose rules bound it.
"""
import argparse
from collections import Counter
import json
import os
from pathlib import Path
import re
import sys

from . import evidence
from .parallel import RESULTS_VARIABLE
from .simulators import matrix

# The order of a run's totals.
OUTCOMES = ("passed", "failed", "skipped", "expected failure", "unmeasured")


# The value of a kept record's keptBy that report prints.
KEPT_BY = "eng results keep"


def case_line(name, case):
    seconds = "no time" if case["seconds"] is None else f"{case['seconds']:.3f}s"
    reason = f" ({case['reason']})" if case.get("reason") else ""
    return f"{name}: {case['outcome']} {seconds} {case['test']}{reason}"


def lines(record):
    """What to print of a kept record: a line per test case and the totals, or why no test's outcome is known."""
    name, status = record["run"], record["status"]
    if record.get("bundleUnread"):
        return [f"UNMEASURED {name}: no test's outcome is known, whatever xcodebuild's status {status} says: "
                f"{record['bundleUnread']}"]
    totals = Counter(case["outcome"] for case in record["tests"])
    return [*(case_line(name, case) for case in record["tests"]),
            f"{name}: " + ", ".join(f"{totals[outcome]} {outcome}" for outcome in OUTCOMES) + f" (xcodebuild status {status})"]


def report(results, out=print):
    """Print the records `keep` left under a lane's results/, a group's lanes' among them; never one another
    command kept."""
    for path in sorted(Path(results).rglob(f"{matrix.KEPT}/*.json")):
        try:
            record = json.loads(path.read_text())
            if record.get("keptBy") != KEPT_BY: continue
            found = lines(record)
        except (OSError, ValueError, KeyError, TypeError) as error:
            out(f"eng: {path} could not be read: {error!r}")
            continue
        out(f"eng: per-test results in {path}:")
        for line in found: out(line)


def keep(log, bundle, name, status, results=None, out=print):
    """Keep a run's outcomes as `kept` does and print them; return the exit status the module's text gives."""
    results = results if results is not None else os.environ.get(RESULTS_VARIABLE)
    if results and (Path(results) / matrix.KEPT / f"{name}.json").exists():
        print(f"eng results: {name} is already kept in {Path(results) / matrix.KEPT}; give each run a name of its own",
              file=sys.stderr, flush=True)
        return 1
    log, bundle = Path(log), Path(bundle)
    record = matrix.bundle_record(bundle) if bundle.is_dir() else None
    cases = matrix.test_cases(record) if record is not None else []
    unread = (f"no result bundle at {bundle}" if not bundle.is_dir() else
              f"{matrix.UNREAD}: {bundle}" if record is None else None if cases else f"{matrix.NO_CASES}: {bundle}")
    folder = matrix.kept(name, log, record, status, None, results, unread, KEPT_BY)
    if results and folder is None: return 1
    for line in lines({"run": name, "status": status, "tests": cases, "bundleUnread": unread}): out(line)
    out(f"{name}: kept in {folder}" if folder else f"{name}: not kept: no lane gave it a results directory ({RESULTS_VARIABLE} is unset)")
    return evidence.UNMEASURED if unread is not None else 0


def main(argv):
    parser = argparse.ArgumentParser(prog="eng results", description=__doc__.split("\n")[0],
                                     epilog=__doc__.split("\n\n", 2)[2], formatter_class=argparse.RawDescriptionHelpFormatter)
    actions = parser.add_subparsers(dest="action", required=True)
    keeping = actions.add_parser("keep", help="keep and print a test run's per-test outcomes and seconds")
    keeping.add_argument("--log", required=True, type=Path, help="the run's xcodebuild log")
    keeping.add_argument("--bundle", required=True, type=Path, help="the run's result bundle")
    keeping.add_argument("--name", required=True, help="the run's name in results/, such as iPhone-17-Pro")
    keeping.add_argument("--status", required=True, type=int, help="the exit status of the run's xcodebuild")
    keeping.add_argument("--results", type=Path, help=f"where to keep them (default: {RESULTS_VARIABLE}, the lane's results/)")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[\w.-]+", args.name): parser.error("--name must be a plain name")
    return keep(args.log, args.bundle, args.name, args.status, args.results)
