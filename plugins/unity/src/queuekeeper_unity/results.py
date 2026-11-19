"""Each test's outcome and seconds from a Unity test run, printed when its lane ends.

`qk unity test` keeps Unity's results.xml, NUnit's form, in the lane's results folder under unity/. A lane's output
goes to its log, so `qk run` prints a line per test from that file when the lane ends, here or, for a run on another
Mac, once its results have come back, as it does for the Apple plugin's kept runs."""
from collections import Counter
from pathlib import Path
import xml.etree.ElementTree as ET

# Under a lane's results folder, where `qk unity test` keeps a run.
FOLDER = "unity"
FILE = "results.xml"
# NUnit's results, in the words Queuekeeper prints, and the order of a run's totals.
OUTCOMES = {"Passed": "passed", "Failed": "failed", "Skipped": "skipped", "Inconclusive": "inconclusive"}


def cases(results):
    """Each test case in a results.xml, as {test, outcome, seconds, reason}. seconds is None when the file gives
    none, and reason is the first line of a message, or None."""
    found = []
    for case in ET.parse(results).getroot().iter("test-case"):
        try: seconds = float(case.get("duration"))
        except (TypeError, ValueError): seconds = None
        result = case.get("result") or "no result"
        message = case.find(".//message")
        said = (message.text or "").strip() if message is not None else ""
        found.append({"test": case.get("fullname") or case.get("name") or "?", "outcome": OUTCOMES.get(result, result.lower()),
                      "seconds": seconds, "reason": said.splitlines()[0][:160] if said else None})
    return found


def lines(found, name=FOLDER):
    """A line per test case and the totals."""
    said = []
    for case in found:
        seconds = "no time" if case["seconds"] is None else f"{case['seconds']:.3f}s"
        reason = f" ({case['reason']})" if case["reason"] and case["outcome"] != "passed" else ""
        said.append(f"{name}: {case['outcome']} {seconds} {case['test']}{reason}")
    totals = Counter(case["outcome"] for case in found)
    order = [*OUTCOMES.values(), *sorted(set(totals) - set(OUTCOMES.values()))]
    said.append(f"{name}: " + ", ".join(f"{totals[outcome]} {outcome}" for outcome in order
                                        if totals[outcome] or outcome in ("passed", "failed")))
    return said


def report(results, out=print):
    """Print the runs `qk unity test` kept under a lane's results/, a group's lanes' among them."""
    for path in sorted(Path(results).rglob(f"{FOLDER}/{FILE}")):
        try: said = lines(cases(path))
        except (OSError, ET.ParseError) as error:
            out(f"queuekeeper: {path} could not be read: {error!r}")
            continue
        out(f"queuekeeper: per-test results in {path}:")
        for line in said: out(line)


def register():
    """Have `qk run` print them when a lane ends."""
    from queuekeeper import plugins
    if report not in plugins.REPORT_RESULTS: plugins.REPORT_RESULTS.append(report)
