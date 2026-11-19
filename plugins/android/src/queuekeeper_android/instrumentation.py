"""What `adb shell am instrument -r` reports of each test, read as it streams.

`am instrument -r -w` prints a block of INSTRUMENTATION_STATUS lines for each test's start and another for its end,
ended by an INSTRUMENTATION_STATUS_CODE: 1 when the test starts, 0 when it passed, -2 when an assertion failed,
-1 when it raised, -3 when it was ignored and -4 when an assumption failed. The run ends with an
INSTRUMENTATION_RESULT block and INSTRUMENTATION_CODE, or with INSTRUMENTATION_FAILED when the process under test
died, in which case the test that was running has no end (AndroidJUnitRunner 1.6, Android 16).

The raw output carries no times, so each test's seconds are read from when its start and end lines arrived (Run.feed
takes the moment of each line). A value may span lines: a stack's lines follow its `stack=` key until the next key.
"""
from dataclasses import dataclass, field
import re
import time

STATUS = "INSTRUMENTATION_STATUS: "
STATUS_CODE = "INSTRUMENTATION_STATUS_CODE: "
RESULT = "INSTRUMENTATION_RESULT: "
CODE = "INSTRUMENTATION_CODE: "
FAILED = "INSTRUMENTATION_FAILED: "
# Each status code's outcome, in the words the kept results use (run_results.OUTCOMES).
OUTCOMES = {0: "passed", -1: "failed", -2: "failed", -3: "skipped", -4: "skipped"}
STARTED = 1
KEY = re.compile(r"([A-Za-z_][\w.-]*)=(.*)", re.S)


@dataclass
class Run:
    """One instrumentation run's tests, fed its output a line at a time."""
    tests: list = field(default_factory=list)
    # The values of the block being read, the key whose value the next unkeyed line continues, and when each
    # running test started.
    block: dict = field(default_factory=dict)
    key: str = None
    started: dict = field(default_factory=dict)
    expected: int = None
    finished: bool = False
    crashed: str = None
    result: dict = field(default_factory=dict)
    in_result: bool = False

    def feed(self, line, at=None):
        at = time.monotonic() if at is None else at
        line = line.rstrip("\r\n")
        for prefix, target in ((STATUS, "status"), (RESULT, "result")):
            if line.startswith(prefix):
                self.in_result = target == "result"
                found = KEY.fullmatch(line[len(prefix):])
                if found:
                    self.key = found.group(1)
                    (self.result if self.in_result else self.block)[self.key] = found.group(2)
                return
        if line.startswith(STATUS_CODE):
            self._ended_block(int(line[len(STATUS_CODE):].strip() or 0), at)
            return
        if line.startswith(CODE):
            self.finished, self.key = True, None
            return
        if line.startswith(FAILED):
            self.crashed, self.key = line[len(FAILED):].strip(), None
            return
        if self.key is not None:
            values = self.result if self.in_result else self.block
            values[self.key] += "\n" + line

    def _ended_block(self, code, at):
        block, self.block, self.key = self.block, {}, None
        if "numtests" in block:
            try: self.expected = int(block["numtests"])
            except ValueError: pass
        name = test_name(block)
        if name is None: return
        if code == STARTED:
            self.started[name] = at
            return
        began = self.started.pop(name, None)
        outcome = OUTCOMES.get(code, "failed")
        case = {"test": name, "outcome": outcome, "seconds": None if began is None else round(at - began, 3)}
        stack = block.get("stack", "").strip()
        if stack: case["reason"] = stack.splitlines()[0][:500]
        if code == -4: case["reason"] = "assumption failed" + (f": {case['reason']}" if stack else "")
        self.tests.append(case)

    def close(self, at=None):
        """End the run: a test that started and never ended, because the process under test died or the run was
        stopped, is unmeasured."""
        at = time.monotonic() if at is None else at
        why = f"the instrumentation ended before it did: {self.crashed}" if self.crashed else \
            "the instrumentation ended before it did"
        for name, began in self.started.items():
            self.tests.append({"test": name, "outcome": "unmeasured", "seconds": round(at - began, 3), "reason": why})
        self.started = {}
        return self

    @property
    def complete(self):
        """Whether the run ended normally and reported every test it announced."""
        ended = len(self.tests)
        return self.finished and self.crashed is None and (self.expected is None or ended >= self.expected)

    def counts(self):
        """(ran, skipped, failed, unmeasured)."""
        outcomes = [case["outcome"] for case in self.tests]
        return (len(outcomes), outcomes.count("skipped"), outcomes.count("failed"), outcomes.count("unmeasured"))

    def summary(self):
        """The line lane_results.counted reads as a wrapper's count: `Executed N tests, with K tests skipped`."""
        ran, skipped, failed, unmeasured = self.counts()
        plural = lambda n: "test" if n == 1 else "tests"
        line = f"Executed {ran} {plural(ran)}, with {skipped} {plural(skipped)} skipped and {failed} " \
               f"{'failure' if failed == 1 else 'failures'}"
        if unmeasured: line += f" ({unmeasured} unmeasured)"
        return line


def test_name(block):
    """class#method, as a test filter names a test, or None for a block that names none."""
    if "class" not in block or "test" not in block: return None
    return f"{block['class'].strip()}#{block['test'].strip()}"


def parse(text):
    """The run that text, a whole instrumentation output, reports; times are unknown."""
    run = Run()
    for line in text.splitlines():
        run.feed(line, at=0.0)
    run.close(at=0.0)
    for case in run.tests:
        case["seconds"] = None
    return run


def command(package, runner, *, shard=None, tests=(), arguments=None):
    """The `am instrument` words that run package's tests under runner, raw and waiting, optionally one of
    AndroidJUnitRunner's shards, (index, count), or only the class or class#method filters tests names."""
    words = ["am", "instrument", "-r", "-w"]
    if shard is not None:
        index, count = shard
        words += ["-e", "numShards", str(count), "-e", "shardIndex", str(index)]
    if tests:
        words += ["-e", "class", ",".join(tests)]
    for key, value in (arguments or {}).items():
        words += ["-e", key, str(value)]
    return [*words, f"{package}/{runner}"]
