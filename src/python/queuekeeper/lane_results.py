"""What a lane's run reports: its exit statuses, and the tests its log counts as run, skipped and unmeasured.

It reads unittest, pytest, XCTest and Swift Testing logs."""
import re


# XCTest prints each suite's total on the line after its result, its skips within it. swift test
# and xcodebuild run each bundle under its own top suite, All tests, or Selected tests when filtered.
SKIPPED = r"(?:, with (\d+) tests? skipped)?"
SUITE_TOTAL = re.compile(r"^Test Suite '([^'\n]*)' (?:passed|failed) at [^\n]*\n\s*Executed (\d+) tests?\b" + SKIPPED, re.M)
# A wrapper's own line counts like XCTest's, or names its skips beside a count that already leaves them out, as
# qk simulator-tests's line for a device does: `PASS iPhone-17-Pro: Executed 57 tests, 5 skipped in 106s`
# (vocaro/queuekeeper#250). The second group is skips within the count, the third skips beside it.
EXECUTED = re.compile(r"Executed (\d+) tests?\b(?:, with (\d+) tests? skipped|, (\d+) skipped\b)?")
# A repeat series recaps each device's result after that result has already been printed.
SERIES_RUN_RECAP = re.compile(r"^[ \t]*SERIES \S+ run \d+ of \d+:[^\n]*$", re.M)
# qk simulator-tests' line for a device or build that failed, `FAIL KIND: DETAIL; log: PATH`, and the last lines of
# that log it quotes under it, each indented by four spaces. The device's line counts its tests already: read again,
# the quoted Swift Testing run line made one filtered test that failed `2 ran, and the log does not say how many of
# them failed` (vocaro/queuekeeper#81).
QUOTED_TAIL = re.compile(r"^(FAIL [^\n]*; log: [^\n]*\n)((?:    [^\n]*(?:\n|$))+)", re.M)
# A lane group quotes a failed member's log tail this way. An outer command can also print the full member
# log, so the tail is diagnostic output only (vocaro/queuekeeper#324).
GROUP_TAIL = re.compile(r"^((?:FAIL|UNMEASURED|NOT RUN) [^\n]+ \(\d+s, status -?\d+\): [^\n]+\.log\n)"
                        r"(?:    [^\n]*(?:\n|$))+", re.M)
# Swift Testing's total counts a test it skipped or that canceled itself, and names neither: each
# has a line of its own instead (Swift 6.4, October 2026). A case of a parameterized test that canceled
# itself has none, and is printed as passed: qk xctest's Apple plugin writes its test such a line from the
# event stream (xctest_shards.canceled_cases, vocaro/queuekeeper#312).
SWIFT_TESTING_RUN = re.compile(r"Test run with (\d+) tests?(?: in \d+ suites?)? (?:passed|failed)")
SWIFT_TESTING_SKIP = re.compile(r'^\S+ Test (?P<test>\S.*?) '
                                r'(?:skipped(?:\.|: "(?P<reason>.*)")?|(?P<cancellation>was cancelled after [^\n]*))$',
                                re.M)
XCTEST_SKIP_DETAILS = re.compile(r"^\S.*?: (?P<test>-\[[^\]\n]+\]) : Test skipped(?: - (?P<reason>.*))?$", re.M)
UNITTEST_SKIP_DETAILS = re.compile(r"^\S+ \((?P<test>[^)\n]+)\) \.\.\. skipped (?P<reason>['\"].*['\"])$", re.M)
PYTEST_SKIP_DETAILS = re.compile(r"^[ \t]*SKIPPED (?P<test>\S+::.*?) - (?:Skipped: )?(?P<reason>\S.*)$", re.M)
SKIP_EVIDENCE_ITEMS = 256
SKIP_EVIDENCE_FIELD = 512
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
# qk unittest's line over a process's output, `== NAME: OK (3 tests, 1.2s)`, and that output, up to the next
# process's line or the run's summary (parallel.unittest_main). qk xctest's line names its skips before the seconds.
UNITTEST_PROCESS = re.compile(r"^== \S[^\n]* \(\d+ tests, [\d.]+s\)\n.*?(?=^== |^queuekeeper unittest: )", re.M | re.S)


def pytest_counts(text):
    """(ran, skipped) for each pytest run whose summary line the log holds."""
    for match in PYTEST_SUMMARY.finditer(text):
        items = dict((word, int(n)) for n, word in re.findall(r"(\d+) (\w+)", match.group(1)))
        yield sum(items.get(word, 0) for word in PYTEST_RAN), items.get("skipped", 0)


def outside_unittest(text):
    """text without the output of each process of a qk unittest run. A test prints into it what the runner it fakes
    would, `Executed 1 test, with 0 failures` for one, and unittest's own lines alone count that process's tests:
    read as another runner's, each such line added a test to the 2563 a gate ran (vocaro/queuekeeper#153). The
    output of a last process that nothing follows stays, as whatever a cut-off log holds after it would go with it."""
    return UNITTEST_PROCESS.sub("", text)


def unquoted(text, keep=False):
    """Discard diagnostic tails for test counts. With keep, unindent simulator tails to name failed tests."""
    if not keep: return QUOTED_TAIL.sub(r"\1", GROUP_TAIL.sub(r"\1", text))
    return QUOTED_TAIL.sub(lambda match: match.group(1) + re.sub(r"^    ", "", match.group(2), flags=re.M), text)


def counted(text):
    """(ran, skipped) over the counts a log reports, or None when it reports no count.

    Each XCTest bundle's top-level total, each Swift Testing run, each unittest run and each pytest
    run. A wrapper's own "Executed N tests" line, one per simulator for example, counts only when
    the log holds no XCTest totals, which it would repeat. XCTest's, unittest's and Swift Testing's
    totals include their skips; XCTest and unittest name them apart, and Swift Testing gives each
    skipped or canceled test a line. A wrapper's line names its skips within its count or beside it
    (EXECUTED). pytest names each outcome apart, its skips among them. Within a qk unittest
    process's output only unittest's own total counts (outside_unittest)."""
    whole = text
    text = outside_unittest(unquoted(SERIES_RUN_RECAP.sub("", text)))
    suites = list(SUITE_TOTAL.finditer(text))
    top = [(int(match.group(2)), int(match.group(3) or 0)) for match in suites
           if match.group(1) in ("All tests", "Selected tests")]
    within = {match.start(2) for match in suites}
    wrapper = [(int(match.group(1)) + int(match.group(3) or 0), int(match.group(2) or match.group(3) or 0))
               for match in EXECUTED.finditer(text) if match.start(1) not in within]
    counts = top or wrapper
    runs = [int(n) for n in SWIFT_TESTING_RUN.findall(text)]
    if runs: counts.append((sum(runs), len(SWIFT_TESTING_SKIP.findall(text))))
    for match in RAN.finditer(whole):
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


def skip_evidence(text, skipped):
    """Bound the reported skip details for a private group artifact, leaving the count parser unchanged."""
    text = unquoted(SERIES_RUN_RECAP.sub("", text))
    runner_output = outside_unittest(text)
    entries, seen = [], set()
    truncated, repeated = False, False
    for runner, pattern, source in (("xctest", XCTEST_SKIP_DETAILS, runner_output),
                                    ("swift-testing", SWIFT_TESTING_SKIP, runner_output),
                                    ("unittest", UNITTEST_SKIP_DETAILS, text),
                                    ("pytest", PYTEST_SKIP_DETAILS, runner_output)):
        for match in pattern.finditer(source):
            if len(entries) == SKIP_EVIDENCE_ITEMS:
                truncated = True
                break
            fields = match.groupdict()
            test, reason = fields["test"], fields.get("reason") or fields.get("cancellation")
            if runner == "unittest" and reason is not None:
                reason = reason[1:-1]
            if len(test) > SKIP_EVIDENCE_FIELD:
                truncated = True
                continue
            identity = runner, test
            if identity in seen:
                repeated = True
                continue
            seen.add(identity)
            entry = {"runner": runner, "test": test}
            if reason is not None:
                if len(reason) > SKIP_EVIDENCE_FIELD: truncated = True
                entry["reason"] = reason[:SKIP_EVIDENCE_FIELD]
            entries.append(entry)
    coverage = ("matches-count" if not truncated and not repeated and len(entries) == skipped else
                "aggregate-only" if not entries and not truncated else "partial")
    return {"reported": skipped, "coverage": coverage, "truncated": truncated, "skips": entries,
            **({"repeatedIdentities": True} if repeated else {})}


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
# The exit status of any qk command when Queuekeeper itself could not start: composing its checkout's plugins or
# loading one of the project's plugins raised, before the command was read, so no lane's command ran (cli.main):
# sysexits' EX_SOFTWARE. On 2026-11-15 the adapter three projects load as a plugin moved to a commit that refused
# the older Queuekeeper their queue workers ran, every lane ended in that traceback with exit 1, and the queue sent at
# least nine entries back as failing "on this commit" (vocaro/queuekeeper#158). The line that begins TOOL_SAID says
# why. A lane's own command may exit 70 too, so the merge queue also reads that the run left no receipt of the lane
# (merge_queue.Worker.tool_unstarted).
TOOL_NOT_STARTED = 70
TOOL_SAID = "queuekeeper: could not start: "
# The line qk run prints, on standard error, when a lane's run ran out of disk space: its command was stopped, a
# write of queuekeeper's own found the disk full, or it failed with the disk short of QUEUEKEEPER_RUN_FREE_GB. The run
# is unmeasured, the host's fault and not the commits' (vocaro/queuekeeper#162), and the merge queue holds on it at once
# rather than retry into a full disk (merge_queue.Worker.out_of_space).
OUT_OF_SPACE = "queuekeeper: out of disk space: "
# What a lane's refusal says when it waited for free disk space before admission and none came (cli.space_wait), which
# its `not run on` line carries into the merge queue's hold (merge_queue.Worker.no_verdict_cause).
SHORT_OF_SPACE = "is short of disk space"

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
