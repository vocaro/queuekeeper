"""Source identities, positive test counts, and path-safe evidence receipts."""
import hashlib
import json
from pathlib import Path
import re
import subprocess


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# XCTest prints each suite's total on the line after its result, its skips within it. swift test
# and xcodebuild run each bundle under its own top suite, All tests, or Selected tests when filtered.
SKIPPED = r"(?:, with (\d+) tests? skipped)?"
SUITE_TOTAL = re.compile(r"^Test Suite '([^'\n]*)' (?:passed|failed) at [^\n]*\n\s*Executed (\d+) tests?\b" + SKIPPED, re.M)
EXECUTED = re.compile(r"Executed (\d+) tests?\b" + SKIPPED)
# Swift Testing's total counts a test it skipped or that canceled itself, and names neither: each
# has a line of its own instead (Swift 6.4, October 2026).
SWIFT_TESTING_RUN = re.compile(r"Test run with (\d+) tests?(?: in \d+ suites?)? (?:passed|failed)")
SWIFT_TESTING_SKIP = re.compile(r'^\S+ Test \S.*? (?:skipped(?:\.|: ".*")?|was cancelled after [^\n]*)$', re.M)
# unittest's total, then its result line, which names its skips: OK (skipped=2).
RAN = re.compile(r"^Ran (\d+) tests?\b[^\n]*\n\s*(?:(?:OK|FAILED)(?: \(([^)\n]*)\))?)?", re.M)


def counted(text):
    """(ran, skipped) over the counts a log reports, or None when it reports no count.

    Each XCTest bundle's top-level total, each Swift Testing run and each unittest run. A
    wrapper's own "Executed N tests" line, one per simulator for example, counts only when the
    log holds no XCTest totals, which it would repeat. XCTest's, unittest's and Swift Testing's
    totals include their skips; XCTest and unittest name them apart, and Swift Testing gives each
    skipped or canceled test a line."""
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


def require_tests(text, minimum=1, expected=None):
    observed = executed_tests(text)
    skipped = skipped_tests(text)
    # Skipped tests ran nothing, so they count toward no minimum.
    beside = f" ({skipped} skipped, which do not count)" if skipped else ""
    if expected is not None and observed != expected:
        raise ValueError(f"Expected exactly {expected} executed tests; observed {observed or 0}{beside}")
    if observed is None or observed < minimum:
        raise ValueError(f"Expected at least {minimum} executed tests; observed {observed or 0}{beside}")
    return observed


# The exit status of a run whose unmeasured results are all that stand between it and a pass.
UNMEASURED = 3

# A test the host left unmeasured skips (XCTest, unittest) or cancels itself (Swift Testing) with a
# reason that begins "Unmeasured:", and swift test and xcodebuild still exit 0. unittest prints the
# reason only when verbose.
UNMEASURED_TEST = re.compile(r"^.*:\d+: -\[[^\]\n]+\] : Test skipped - Unmeasured: .*$"
                             # eng simulator-tests' line for one a result bundle records: DEVICE: -[TEST] : ...
                             r"|^\S+: -\[[^\]\n]+\] : Test skipped - Unmeasured: .*$"
                             r"|^\S+ Test .* was cancelled after [^:\n]*: \"Unmeasured: .*\"$"
                             r"|^.* \.\.\. skipped 'Unmeasured: .*'$", re.M)


def unmeasured_tests(text):
    """The distinct lines in which a log reports a test unmeasured, in order. A wrapper that
    repeats them in a summary adds none."""
    return list(dict.fromkeys(match.group(0).strip() for match in UNMEASURED_TEST.finditer(text)))


def contained(root, relative):
    root = Path(root).resolve()
    relative = Path(relative)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"Unsafe evidence path: {relative}")
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"Evidence path escapes its root: {relative}")
    return path


def receipt(root, paths):
    return {"schemaVersion": 1, "files": {str(p): sha256(contained(root, p)) for p in paths}}


def verify(root):
    root = Path(root)
    value = json.loads((root / "receipt.json").read_text())
    if value.get("schemaVersion") != 1 or not isinstance(value.get("files"), dict) or not value["files"]:
        raise ValueError("Expected a nonempty version 1 evidence receipt")
    for name, expected in value["files"].items():
        if sha256(contained(root, name)) != expected:
            raise ValueError(f"Evidence changed: {name}")
    return len(value["files"])


def export_xcresults(bundles, destination):
    """Export serially; xcresulttool can contend across simultaneous exports."""
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    for index, bundle in enumerate(bundles):
        target = destination / f"{index:03d}-{Path(bundle).stem}"
        subprocess.run(["xcrun", "xcresulttool", "export", "attachments", "--path", str(bundle),
                        "--output-path", str(target)], check=True, timeout=300)
