"""What a lane's run reports: its exit statuses, and the tests its log counts as run, skipped and unmeasured.

It reads unittest, pytest, XCTest and Swift Testing logs."""
import re


# XCTest prints each suite's total on the line after its result, its skips within it. swift test
# and xcodebuild run each bundle under its own top suite, All tests, or Selected tests when filtered.
SKIPPED = r"(?:, with (\d+) tests? skipped)?"
SUITE_TOTAL = re.compile(r"^Test Suite '([^'\n]*)' (?:passed|failed) at [^\n]*\n\s*Executed (\d+) tests?\b" + SKIPPED, re.M)
EXECUTED = re.compile(r"Executed (\d+) tests?\b" + SKIPPED)
# A repeat series recaps each device's result after that result has already been printed.
SERIES_RUN_RECAP = re.compile(r"^[ \t]*SERIES \S+ run \d+ of \d+:[^\n]*$", re.M)
# Swift Testing's total counts a test it skipped or that canceled itself, and names neither: each
# has a line of its own instead (Swift 6.4, October 2026).
SWIFT_TESTING_RUN = re.compile(r"Test run with (\d+) tests?(?: in \d+ suites?)? (?:passed|failed)")
SWIFT_TESTING_SKIP = re.compile(r'^\S+ Test \S.*? (?:skipped(?:\.|: ".*")?|was cancelled after [^\n]*)$', re.M)
# unittest's total, then its result line, which names its skips: OK (skipped=2).
RAN = re.compile(r"^Ran (\d+) tests?\b[^\n]*\n\s*(?:(?:OK|FAILED)(?: \(([^)\n]*)\))?)?", re.M)
# pytest's last line, a line of its own, framed in "=" unless -q, and indented by a wrapper that
# indents its steps: 1 failed, 10468 passed, 24 skipped in 589.31s (0:09:49), or no tests ran in
# 0.01s. pytest-xdist prints it alike (pytest 8.4.2, pytest-xdist 3.8.0, November 2026). Every item
# must be one pytest names, so a line that merely says "passed" is no count.
PYTEST_ITEM = r"\d+ (?:failed|passed|skipped|deselected|xfailed|xpassed|warnings?|errors?)"
PYTEST_SUMMARY = re.compile(r"^[ \t]*(?:=+ )?((?:" + PYTEST_ITEM + r")(?:, " + PYTEST_ITEM + r")*|no tests ran)"
                            r" in \d+(?:\.\d+)?s(?: \([^)\n]*\))?(?: =+)?[ \t]*$", re.M)
# The tests that ran, and so count with their skips among them: not deselected ones, warnings, or
# errors, which pytest counts apart from the tests (a fixture's, or a module's that collected none).
PYTEST_RAN = ("failed", "passed", "skipped", "xfailed", "xpassed")


def pytest_counts(text):
    """(ran, skipped) for each pytest run whose summary line the log holds."""
    for match in PYTEST_SUMMARY.finditer(text):
        items = dict((word, int(n)) for n, word in re.findall(r"(\d+) (\w+)", match.group(1)))
        yield sum(items.get(word, 0) for word in PYTEST_RAN), items.get("skipped", 0)


def counted(text):
    """(ran, skipped) over the counts a log reports, or None when it reports no count.

    Each XCTest bundle's top-level total, each Swift Testing run, each unittest run and each pytest
    run. A wrapper's own "Executed N tests" line, one per simulator for example, counts only when
    the log holds no XCTest totals, which it would repeat. XCTest's, unittest's and Swift Testing's
    totals include their skips; XCTest and unittest name them apart, and Swift Testing gives each
    skipped or canceled test a line. pytest names each outcome apart, its skips among them."""
    text = SERIES_RUN_RECAP.sub("", text)
    suites = list(SUITE_TOTAL.finditer(text))
    top = [(int(match.group(2)), int(match.group(3) or 0)) for match in suites
           if match.group(1) in ("All tests", "Selected tests")]
    within = {match.start(2) for match in suites}
    wrapper = [(int(match.group(1)), int(match.group(2) or 0)) for match in EXECUTED.finditer(text)
               if match.start(1) not in within]
    counts = top or wrapper
    runs = [int(n) for n in SWIFT_TESTING_RUN.findall(text)]
    if runs: counts.append((sum(runs), len(SWIFT_TESTING_SKIP.findall(text))))
    for match in RAN.finditer(text):
        skipped = re.search(r"\bskipped=(\d+)", match.group(2) or "")
        counts.append((int(match.group(1)), int(skipped.group(1)) if skipped else 0))
    counts.extend(pytest_counts(text))
    if not counts: return None
    return sum(ran for ran, _ in counts), sum(skipped for _, skipped in counts)


def executed_tests(text):
    """The tests a log reports run, not counting those that skipped, or None when it reports no
    count. A run whose every selected test skipped establishes no coverage."""
    found = counted(text)
    return None if found is None else found[0] - found[1]


def skipped_tests(text):
    """The tests a log reports skipped, by the counts executed_tests reads; 0 when it reports none."""
    found = counted(text)
    return 0 if found is None else found[1]


class NotRun(ValueError):
    """A run selected exactly the tests it was expected to run, and some of them skipped or canceled themselves:
    what they would have shown is unknown, so the run is unmeasured, neither failed nor passed."""


def canceled_tests(text):
    """The distinct lines in which Swift Testing reports a test skipped or canceled, each with its reason."""
    return list(dict.fromkeys(match.group(0).strip() for match in SWIFT_TESTING_SKIP.finditer(text)))


def require_tests(text, minimum=1, expected=None):
    observed = executed_tests(text)
    skipped = skipped_tests(text)
    # Skipped tests ran nothing, so they count toward no minimum.
    beside = f" ({skipped} skipped, which do not count)" if skipped else ""
    if expected is not None and observed != expected:
        message = f"Expected exactly {expected} executed tests; observed {observed or 0}{beside}"
        # Any other total selected the wrong tests, whatever the host had.
        raise (NotRun if observed is not None and observed + skipped == expected else ValueError)(message)
    if observed is None or observed < minimum:
        raise ValueError(f"Expected at least {minimum} executed tests; observed {observed or 0}{beside}")
    return observed


# The exit status of a run whose unmeasured results are all that stand between it and a pass.
UNMEASURED = 3
# The exit status of a failed lane whose log shows that no test started and names what failed, such as its project's
# generation (lane_report.before_tests, vocaro-engineering#393). It is a failure, as any status but 0 and the no-verdict
# ones is: it tells a setup or build that stopped the run apart from a test that failed, not a defect from the host.
BEFORE_TESTS = 4
# The exit status of qk run when the lane's command could not start: the system could not start it, or the interpreter
# it names has no file to run (cli.unstartable), as a shell's 127 says a command was not found. The receipt's outcome,
# not-started, and its notStarted say so too, which is what the merge queue reads, since a lane's own command may exit
# 127 (vocaro/queuekeeper#108).
NOT_STARTED = 127

# A test the host left unmeasured skips (XCTest, unittest) or cancels itself (Swift Testing) with a
# reason that begins "Unmeasured:", and swift test and xcodebuild still exit 0. unittest prints the
# reason only when verbose. pytest prints it in its short summary under -rs (or -ra, -rA), folding
# the skips that share a file, line and reason into one line that names no test,
# SKIPPED [2] tests/test_x.py:15: Unmeasured: ..., unless --no-fold-skipped gives each its own:
# SKIPPED tests/test_x.py::test_y[1] - Skipped: Unmeasured: ... (pytest 8.4.2, with and without
# pytest-xdist 3.8.0, November 2026). Its -v lines truncate the reason, and under xdist omit it.
UNMEASURED_TEST = re.compile(r"^.*:\d+: -\[[^\]\n]+\] : Test skipped - Unmeasured: .*$"
                             # qk simulator-tests' line for one a result bundle records: DEVICE: -[TEST] : ...
                             r"|^\S+: -\[[^\]\n]+\] : Test skipped - Unmeasured: .*$"
                             r"|^\S+ Test .* was cancelled after [^:\n]*: \"Unmeasured: .*\"$"
                             r"|^.* \.\.\. skipped 'Unmeasured: .*'$"
                             r"|^[ \t]*SKIPPED \S+::.*? - (?:Skipped: )?Unmeasured: .*$"
                             # A folded line still makes the run unmeasured, though it names no test.
                             r"|^[ \t]*SKIPPED \[\d+\] [^\n]*?: Unmeasured: .*$", re.M)


# A project's own account of what it could not measure, for a run that ends unmeasured with no test its log reports so,
# such as a gate whose timing check voided itself under load: a line of the lane's log that begins `Unmeasured:`, the
# word a test's own unmeasured reason begins with, after any spaces or tabs. On 2026-11-12 mpm's gate named the check
# in a line of its own that no hold quoted, and a reader had to open a log of several hundred lines to learn it
# (vocaro/queuekeeper#30).
UNMEASURED_REASON = re.compile(r"^[ \t]*Unmeasured:[ \t]*\S.*$", re.M)

SERIES = re.compile(r"^Repeat series: asked (\d+) repeats?\b", re.M)


def repeats_asked(arguments):
    """N of a `--repeat N` among a lane's arguments, the series qk run asked for; None when none was asked for, and 0
    when N is no count, which a run reads as a series of none."""
    arguments = list(arguments)
    if "--repeat" not in arguments: return None
    try: return int(arguments[arguments.index("--repeat") + 1])
    except (IndexError, ValueError): return 0


def series_reported(text):
    """How many repeats a log's series says were asked for, from qk simulator-tests' summary, or None when the log
    reports no series: a command that ignored --repeat ran it once at most, whatever its status says."""
    found = SERIES.findall(text)
    return int(found[-1]) if found else None


def unmeasured_reasons(text):
    """The distinct lines in which a lane's log gives its project's own reason for a run left unmeasured, in order."""
    return list(dict.fromkeys(match.group(0).strip() for match in UNMEASURED_REASON.finditer(text)))


def unmeasured_tests(text):
    """The distinct lines in which a log reports a test unmeasured, in order. A wrapper that
    repeats them in a summary adds none."""
    return list(dict.fromkeys(match.group(0).strip() for match in UNMEASURED_TEST.finditer(text)))
